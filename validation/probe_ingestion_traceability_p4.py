# SPDX-License-Identifier: MIT-0
"""P4 (slice S4) for the traceability probe runner: release, the published-input head, attribution.

Invariants (I), re-run by every later slice:

- the release live tests (`tests/integration/test_execution_graph_release_live.py`) against the
  probe table only, each in its own probe namespace: BF-1's refusal then a grant; BF-8's `R`
  after `R+1`; `R` after a legacy-COMPLETED newer retrieval (the real legacy head, read-only,
  over the P0 probe retrieval's window); and V-28's injected apply-time refusal. They run through
  the fail-closed JUnit check: any skip, error, failure or count other than
  `RELEASE_LIVE_TESTS` fails the probe;
- attribution, not equality (R2-16): every change to a real semantic CURRENT pointer or preset
  artifact CURRENT pointer during the probe window maps to a legacy unit (a semantic publication
  by the legacy canonical publisher) or to the publication-stream bake it triggers, by its writer
  identity. No live grant exists before GRAPH, so the live graph table holds no release family at
  all; and no changed pointer carries probe identity (`integration-probe`) or a probe scope;
- the P0-P3 invariants in their SHADOW form, with the sole-writer rule and V-15 in their S4 form
  (the release issuer is the fifth graph-authority worker). P3's schedule check is in its S6
  form: every worker schedule reads ENABLED (§2.11 row 9) and no worker errored since the deploy.

P4 has no slice-state assertion.
"""

# The P4 probe composes the runner's and P3's private helpers.
# pyright: reportPrivateUsage=false
from __future__ import annotations

import argparse
import json
import os
import subprocess
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, cast

import probe_ingestion_traceability_gate as gate
import probe_ingestion_traceability_p3 as p3

__all__ = [
    "RELEASE_FAMILIES",
    "RELEASE_LIVE_FILE",
    "RELEASE_LIVE_TESTS",
    "attribution_verdict",
    "probe_p4",
]

RELEASE_LIVE_FILE: Final = "tests/integration/test_execution_graph_release_live.py"
RELEASE_LIVE_TESTS: Final = 6
RELEASE_FAMILIES: Final = frozenset(
    {
        "GRANT",
        "LINEAGE",
        "RELEASE_ORDER",
        "RESERVATION_SET",
        "TARGET",
        "COHORT",
        "SERVING",
        "DUE/RELEASE",
        "DUE/APPLY",
        "BLOCK",
        "HEAD",
    }
)
"""Live-table families only release writes; before GRAPH every count is zero."""
LEGACY_PUBLISHER: Final = "canonical-curation"
_PROBE_MARKERS: Final = ("integration-probe", "AuthorityScope")


def _junit_file(
    target: gate.Target,
    arguments: argparse.Namespace,
    file: str,
    expected: int,
    extra: Mapping[str, str],
) -> dict[str, object]:
    """Run one integration file once through the fail-closed JUnit check."""
    stamp = gate._utc_stamp()
    xml = Path(arguments.evidence_dir) / f"P4-release-live-{stamp}.xml"
    environment = {
        **os.environ,
        "AWS_PROFILE": target.profile,
        "AWS_REGION": target.region,
        "ADL_TEST_ENV": target.environment,
        **extra,
    }
    completed = subprocess.run(  # noqa: S603 -- fixed argv, explicit inputs only
        [
            "uv",
            "run",
            "pytest",
            "-m",
            "integration",
            file,
            f"--junitxml={xml}",
            "-p",
            "no:cacheprovider",
        ],
        cwd=Path(arguments.worktree).resolve(),
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    text = xml.read_text() if xml.is_file() else ""
    try:
        totals: dict[str, object] = dict(gate.junit_verdict(text, expected))
        totals["passed"] = completed.returncode == 0
    except gate.ProbeError as error:
        totals = {"passed": False, "error": str(error)}
    return {
        "junit": str(xml),
        "junit_sha256": gate.hashlib.sha256(text.encode()).hexdigest() if text else None,
        "returncode": completed.returncode,
        "tail": completed.stdout[-2000:],
        "totals": totals,
        "passed": bool(totals.get("passed")),
    }


def _scan(session: Any, table: str, key: str) -> dict[str, dict[str, Any]]:
    """Every CURRENT record of one pointer table, by partition, as plain JSON-able items."""
    found: dict[str, dict[str, Any]] = {}
    for page in (
        session.client("dynamodb")
        .get_paginator("scan")
        .paginate(
            TableName=table,
            ConsistentRead=True,
            FilterExpression="record_key = :current",
            ExpressionAttributeValues={":current": {"S": "CURRENT"}},
        )
    ):
        for item in page.get("Items", []):
            found[str(item[key]["S"])] = item
    return found


def _parameter(session: Any, name: str) -> str:
    return str(session.client("ssm").get_parameter(Name=name)["Parameter"]["Value"])


def _pointer_tables(session: Any, environment: str) -> dict[str, tuple[str, str]]:
    return {
        "semantic": (
            _parameter(session, f"/adlytics/{environment}/data/v2/semantic-publications-table"),
            "partition_key",
        ),
        "artifact": (
            _parameter(session, f"/adlytics/{environment}/publisher/artifact-pointer-table-name"),
            "page_variant_key",
        ),
    }


def _snapshot(session: Any, environment: str) -> dict[str, dict[str, dict[str, Any]]]:
    return {
        name: _scan(session, table, key)
        for name, (table, key) in _pointer_tables(session, environment).items()
    }


def _publisher(item: Mapping[str, Any]) -> str | None:
    """The writer identity of a semantic CURRENT record: its publication's publisher.

    The record embeds the publication document (`semantic_state.py`, `_publication_data`), whose
    `publisher` names the writer; the legacy sidecar publishes as `canonical-curation`.
    """
    return LEGACY_PUBLISHER if f'"{LEGACY_PUBLISHER}"' in json.dumps(item) else None


def attribution_verdict(  # noqa: PLR0913 -- one pure verdict over the window's readbacks
    before: Mapping[str, Mapping[str, Mapping[str, Any]]],
    after: Mapping[str, Mapping[str, Mapping[str, Any]]],
    *,
    census_before: Mapping[str, int],
    census_after: Mapping[str, int],
    backfill_invocations: float,
) -> dict[str, object]:
    """Attribute every pointer change in the window by writer identity (R2-16).

    A semantic CURRENT change must be a legacy publication (the legacy canonical publisher). An
    artifact CURRENT change must follow a legacy publication in the same window (the stream
    trigger) or the deploy backfill. No release family exists on the live table, so nothing can
    map to a graph grant, and no changed record may carry probe identity.
    """
    changed: dict[str, list[str]] = {}
    unattributed: list[str] = []
    probe_identity: list[str] = []
    for kind in ("semantic", "artifact"):
        old, new = before.get(kind, {}), after.get(kind, {})
        names = sorted(key for key, item in new.items() if old.get(key) != item)
        changed[kind] = names
        for name in names:
            text = json.dumps(new[name], sort_keys=True)
            if any(marker in text for marker in _PROBE_MARKERS):
                probe_identity.append(f"{kind}:{name}")
            if kind == "semantic" and _publisher(new[name]) != LEGACY_PUBLISHER:
                unattributed.append(f"{kind}:{name}")
    if changed["artifact"] and not changed["semantic"] and backfill_invocations == 0:
        unattributed.extend(f"artifact:{name}" for name in changed["artifact"])
    release_live = {
        name: count
        for name, count in {**census_before, **census_after}.items()
        if name in RELEASE_FAMILIES and (census_before.get(name, 0) or census_after.get(name, 0))
    }
    return {
        "changed": changed,
        "changed_counts": {kind: len(names) for kind, names in changed.items()},
        "unattributed": unattributed,
        "probe_identity": probe_identity,
        "live_release_families": release_live,
        "backfill_invocations": backfill_invocations,
        "passed": not unattributed and not probe_identity and not release_live,
    }


def _backfill_invocations(session: Any, environment: str, since: datetime) -> float:
    data = session.client("cloudwatch").get_metric_statistics(
        Namespace="AWS/Lambda",
        MetricName="Invocations",
        Dimensions=[
            {"Name": "FunctionName", "Value": f"adlytics-{environment}-PresetArtifactBackfill"}
        ],
        StartTime=since,
        EndTime=datetime.now(UTC),
        Period=60 * 60 * 24,
        Statistics=["Sum"],
    )
    return sum(float(point["Sum"]) for point in data.get("Datapoints", []))


def probe_p4(target: gate.Target, arguments: argparse.Namespace) -> int:  # noqa: PLR0915
    """Run P4 and write its evidence; nonzero on any failed check."""
    evidence = gate.Evidence("P4", gate._utc_stamp())
    try:
        session, ready = gate._preflight(target, arguments, evidence)
        headers = gate.diff_header_check(
            [Path(path) for path in arguments.diff_file],
            cast("str | None", evidence.document.get("deployed_sha")),
        )
        evidence.document["diff_headers"] = headers
        p1 = json.loads(Path(arguments.p1_evidence).read_text())
        p2 = json.loads(Path(arguments.p2_evidence).read_text())
        p3_doc = json.loads(Path(arguments.p3_evidence).read_text())
        if not (p1.get("passed") and p2.get("passed") and p3_doc.get("passed")):
            raise gate.ProbeError("the P1, P2 or P3 evidence did not pass")
        if not ready:
            raise gate.ProbeError("asset readback, import scan or alarm inventory failed")
        key = str(p1["p0_probe_key"])
        versions = gate._exact_versions(session.client("s3"), arguments.bucket, key)
        if len(versions) != 1:
            raise gate.ProbeError("the P1 manifest key does not have exactly one version")
        table = gate._store_table(session, target.environment, str(arguments.store_stack))
        table_arn = str(
            # aws-pagination: DynamoDB DescribeTable reads one named table.
            session.client("dynamodb").describe_table(TableName=table)["Table"]["TableArn"]
        )
        window_start = datetime.now(UTC)
        census_start = gate._table_census(session, table)
        pointers_before = _snapshot(session, target.environment)

        release = _junit_file(
            target,
            arguments,
            RELEASE_LIVE_FILE,
            RELEASE_LIVE_TESTS,
            {"ADL_P4_RAW_BUCKET": str(arguments.bucket), "ADL_P4_LEGACY_KEY": key},
        )
        evidence.document["release_live"] = release

        # P0-P3 invariants in their S4 form.
        admit = (
            "admit",
            "--manifest-key",
            key,
            "--version-id",
            versions[0],
            "--reason",
            "operator_rerun",
            "--raw-stack",
            str(arguments.raw_stack),
            "--bucket",
            str(arguments.bucket),
        )
        _, mode = gate._operator(target, arguments, "show-mode")
        # The ingestion count is the provisioned writer set's (`_p1_sole_writer`).
        expected_writers = {f"Adlytics-Api-{target.environment}": 1}
        invariants: dict[str, object] = {
            "mode": mode,
            "table_invariants": gate._p1_table_invariants(
                session, table, str(arguments.store_stack)
            ),
            "sole_writer": gate._p1_sole_writer(session, target.environment, expected_writers),
            "refusals": gate._p1_refusal_invariants(target, arguments, admit, table, session),
            "shadow_graphs": gate._baseline_graphs(
                session, table, gate.parse_baseline_graphs(arguments.baseline_graph)
            ),
        }
        reader = gate._persona_api(session, target.environment, str(arguments.status_persona))
        reference = {"manifest": {"key": key, "version_id": versions[0]}}
        # S6: P6 admits other roots into the live table; P1's root is read on its own.
        root = gate.root_receipts(session, table, gate.p1_request_ids(p1))
        explain_status, explained = gate._explain(reader, {**reference, "limit": 25})
        head = session.client("s3").head_object(
            Bucket=arguments.bucket, Key=key, VersionId=versions[0]
        )
        last_modified = cast("datetime", head["LastModified"]).astimezone(UTC).isoformat()
        explanation = gate.explanation_invariants(
            gate.ExplanationRead(
                explained,
                gate._traverse(reader, reference, 1),
                int(cast("int", root["receipts"])),
                gate.p1_request_ids(p1),
                last_modified,
            )
        )
        oversize_status, oversize = gate._explain(reader, {**reference, "cursor": "A" * 2049})
        invalid_start = datetime.now(UTC) - timedelta(seconds=5)
        invalid_status, _invalid = gate._explain(reader, {"flow_id": "not-a-uuid"})
        metric = gate._error_metric_count(
            session, target.environment, invalid_start, int(arguments.wait_seconds)
        )
        graphs = cast("list[dict[str, object]]", explained.get("graphs", []))
        baseline = set(gate.parse_baseline_graphs(arguments.baseline_graph))
        store = p3._store_tests(target, arguments, str(evidence.started_at))
        isolation = p3._iam_isolation(session, target.environment, table_arn)
        deployed = p3._ingestion_updated(session, target.environment)
        schedules = p3._schedules(session, target.environment)
        invocations = p3._worker_invocations(session, target.environment, deployed)
        evidence.document["invariants"] = invariants
        evidence.document["p2"] = {
            "root": root,
            "explain_status": explain_status,
            "explanation_invariants": explanation,
            "oversize_status": oversize_status,
            "invalid_status": invalid_status,
            "error_metric": metric,
        }
        evidence.document["p3"] = {
            "store_live": store,
            "iam_isolation": isolation,
            "ingestion_updated": deployed.isoformat(),
            "schedules": schedules,
            "worker_invocations": invocations,
        }

        census_end = gate._table_census(session, table)
        pointers_after = _snapshot(session, target.environment)
        attribution = attribution_verdict(
            pointers_before,
            pointers_after,
            census_before=census_start,
            census_after=census_end,
            backfill_invocations=_backfill_invocations(session, target.environment, window_start),
        )
        attribution["window"] = {
            "start": window_start.isoformat(),
            "end": datetime.now(UTC).isoformat(),
        }
        evidence.document["attribution"] = attribution
        evidence.document["census"] = {"start": census_start, "end": census_end}
        checks: dict[str, bool] = {
            "preflight": ready,
            "diff_headers_name_the_deployed_sha": bool(headers["passed"]),
            "release_live_junit_clean": bool(release["passed"]),
            "attribution_invariant": bool(attribution["passed"]),
            "mode_shadow": mode.get("mode") == "SHADOW",
            "table_invariants": bool(
                cast("dict[str, object]", invariants["table_invariants"])["passed"]
            ),
            "sole_writer_s4_form": bool(
                cast("dict[str, object]", invariants["sole_writer"])["passed"]
            ),
            "p1_refusals_still_refuse_and_write_nothing": bool(
                cast("dict[str, object]", invariants["refusals"])["passed"]
            ),
            "shadow_graphs_byte_identical": bool(
                cast("dict[str, object]", invariants["shadow_graphs"])["passed"]
            ),
            "p2_explanation_shadow_epoch_0": explain_status == 200
            and gate.root_graphs_verdict(graphs, root, baseline),
            **{f"p2_{name}": passed for name, passed in explanation.items()},
            "p2_oversize_cursor_capacity_limited": (oversize_status, oversize)
            == (413, {"error": "CURSOR_CAPACITY_LIMITED"}),
            "p2_invalid_one_error_metric": invalid_status == 400
            and cast("dict[str, object]", metric)["settled_count"] == 1,
            "p3_store_live_junit_clean_and_no_claim_exhausted": bool(store["passed"]),
            "p3_v15_live_iam_isolation": bool(isolation["passed"]),
            "p3_worker_schedules_enabled": bool(schedules["passed"]),
            "p3_zero_worker_errors_since_deploy": bool(invocations["passed"]),
            "p3_zero_graph_work_on_the_live_table": p3.census_has_no_graph_work(census_end),
        }
        evidence.document["checks"] = checks
        evidence.document["passed"] = all(checks.values())
    except Exception as error:
        evidence.document["passed"] = False
        evidence.document["error"] = f"{type(error).__name__}: {error}"[:2000]
    path = evidence.write(target)
    print(f"evidence: {path}")
    return 0 if evidence.document.get("passed") else 1
