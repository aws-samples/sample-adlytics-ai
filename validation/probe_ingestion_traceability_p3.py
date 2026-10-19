# SPDX-License-Identifier: MIT-0
"""P3 (slice S3) for the traceability probe runner: dispatch, claim, settlement and slot.

Invariants (I):

- the store-level live tests (`tests/integration/test_execution_graph_store_live.py`) against
  the probe table pair only: atomic READY/outbox, BF-2, BF-10, the V-10 race, V-9, V-11, V-13 and
  V-20. They run through the fail-closed JUnit check: any skip, error, failure or count other
  than `STORE_LIVE_TESTS` fails the probe;
- `RUN#` `TransactionConflict` count under the generated maximum parallel claims per run, with
  zero claims exhausting retry (R2-7). One that exhausts it is the S3 stop rule;
- V-15 live IAM readback: every worker role is implicitly denied on the probe tables, the probe
  role is denied on the live table and allowed exactly its generated set on the probe tables,
  and `ConditionCheckItem` on the mode item is allowed only with the mode item's leading key;
- the P0-P2 invariants in their SHADOW form, with P1's sole-writer rule in its S3 form.

Slice state (S), recorded once: the dispatcher and the other workers are deployed and made zero
graph sends; one probe retrieval's unit commits through the new TableWriter after the S3 deploy,
with C4 activations zero (a legacy envelope in SHADOW).

S3 deployed the worker schedules disabled. S6 part 2 enables them (§2.11 row 9) and this probe
judges the deployed SHA, so the schedule check is in its S6 form: every schedule reads ENABLED,
and the mode-first handlers prove they are no-ops outside GRAPH by zero worker errors since the
deploy plus the unchanged live graph table (`zero_graph_sends_no_epoch1_work`,
`live_graph_table_unchanged_but_receipts`). Invocations are recorded, not gated: they are the
one-minute schedule firing.
"""

# The P3 probe composes the runner's own private P0-P2 helpers.
# pyright: reportPrivateUsage=false
from __future__ import annotations

import argparse
import json
import os
import subprocess
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, cast

import probe_ingestion_traceability_gate as gate

__all__ = [
    "STORE_LIVE_FILE",
    "STORE_LIVE_TESTS",
    "WORKERS",
    "census_has_no_graph_work",
    "iam_isolation_verdict",
    "probe_p3",
    "run_conflicts_verdict",
    "schedule_verdict",
    "worker_errors_verdict",
]

STORE_LIVE_FILE: Final = "tests/integration/test_execution_graph_store_live.py"
STORE_LIVE_TESTS: Final = 14
"""The expected test count of the store file (the V-10 race runs in both orders)."""
WORKERS: Final = ("Dispatcher", "FenceSettler", "Promoter", "NodeVerifier", "Release")
"""The graph-authority workers: S3's four and, from S4, the release issuer (deployed disabled)."""
_GRAPH_WORK_FAMILIES: Final = frozenset(
    {
        "RUN",
        "TOKEN",
        "LOCK",
        "EXEC_CLAIM",
        "SLOT",
        "FINGERPRINT",
        "HEAD",
        "BLOCK",
        # S4 release families: no live grant exists before GRAPH.
        "GRANT",
        "LINEAGE",
        "RELEASE_ORDER",
        "RESERVATION_SET",
        "TARGET",
        "COHORT",
        "SERVING",
    }
)
_GRAPH_WORK_DUE: Final = frozenset(
    {
        "DUE/OUTBOX",
        "DUE/VERIFY",
        "DUE/FENCE",
        "DUE/INSTANTIATE",
        "DUE/PROMOTE",
        "DUE/WAITER",
        "DUE/RELEASE",
        "DUE/APPLY",
    }
)
_PROBE_ACTIONS: Final[tuple[str, ...]] = (
    "dynamodb:GetItem",
    "dynamodb:Query",
    "dynamodb:PutItem",
    "dynamodb:UpdateItem",
    "dynamodb:DeleteItem",
    "dynamodb:ConditionCheckItem",
)
_MODE_PK: Final = "MODE#ingestion"
_OPERATOR_ONLY_WRITES: Final = ("dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem")
"""The generated `OPERATOR_ONLY_DENIED_ACTIONS` every graph worker is explicitly denied."""
_OPERATOR_ONLY_KEYS: Final = {"mode": _MODE_PK, "command": "COMMAND#probe-isolation"}


def census_has_no_graph_work(census: Mapping[str, int]) -> bool:
    """Whether the live table holds no epoch-1 work: no run, node, lock, outbox or obligation."""
    return not any(census.get(name, 0) for name in (_GRAPH_WORK_FAMILIES | _GRAPH_WORK_DUE))


def schedule_verdict(rules: Mapping[str, str]) -> dict[str, object]:
    """Every worker schedule exists and reads ENABLED (S6 part 2, §2.11 row 9)."""
    expected = {f"ExecutionGraph{name}Schedule" for name in WORKERS}
    found = {name for name in rules if any(name.endswith(item) for item in expected)}
    return {
        "rules": dict(rules),
        "passed": len(found) == len(expected)
        and all(state == "ENABLED" for state in rules.values()),
    }


def worker_errors_verdict(
    invocations: Mapping[str, float], errors: Mapping[str, float]
) -> dict[str, object]:
    """Every worker reports an error count, and each is zero: mode-first no-ops outside GRAPH."""
    return {
        "invocations": dict(invocations),
        "errors": dict(errors),
        "passed": set(errors) == set(invocations)
        and bool(errors)
        and all(value == 0 for value in errors.values()),
    }


def iam_isolation_verdict(decisions: Mapping[str, Mapping[str, str]]) -> dict[str, object]:
    """Fold simulated decisions, keyed `<principal>|<resource kind>[#n]`, into V-15's verdict.

    `<worker>|probe`: every action denied. `probe|live`: every action denied. `probe|probe`:
    exactly the generated action set allowed. `<writer>|mode_item`: GetItem and ConditionCheckItem
    allowed under the mode item's leading key. `<writer>|other_item`: ConditionCheckItem denied.
    `<requester>|requester_mode_item` (S6, the delivery handler): the mode item readable, and
    every write to it explicitly denied.
    `<worker>|operator_only`: every `MODE#`/`COMMAND#` write explicitly denied, even inside a
    transaction (S4-6).
    """
    failures: list[str] = []
    for key, actions in decisions.items():
        principal, _, kind = key.partition("|")
        kind = kind.partition("#")[0]
        allowed = {action for action, decision in actions.items() if decision == "allowed"}
        if kind == "probe":
            wanted: set[str] = set(_PROBE_ACTIONS) if principal == "probe" else set()
            ok = allowed == wanted
        elif kind == "live":
            ok = not allowed
        elif kind == "mode_item":
            ok = {"dynamodb:GetItem", "dynamodb:ConditionCheckItem"} <= allowed
        elif kind == "other_item":
            ok = "dynamodb:ConditionCheckItem" not in allowed
        elif kind == "requester_mode_item":
            ok = {"dynamodb:GetItem", "dynamodb:ConditionCheckItem"} <= allowed and all(
                actions.get(action) == "explicitDeny" for action in _OPERATOR_ONLY_WRITES
            )
        elif kind == "operator_only":
            ok = all(actions.get(action) == "explicitDeny" for action in _OPERATOR_ONLY_WRITES)
        else:
            ok = False
        if not ok:
            failures.append(key)
    return {
        "decisions": {name: dict(value) for name, value in decisions.items()},
        "failures": failures,
        "passed": not failures and bool(decisions),
    }


def _stack_roles(session: Any, stack: str, fragments: Sequence[str]) -> dict[str, str]:
    """Map each function logical-ID fragment to its execution role ARN."""
    resources = gate.stack_resources(session.client("cloudformation"), stack)
    lambdas = session.client("lambda")
    found: dict[str, str] = {}
    for item in resources:
        if item["ResourceType"] != "AWS::Lambda::Function":
            continue
        logical = str(item["LogicalResourceId"])
        for fragment in fragments:
            if fragment in logical and fragment not in found:
                configuration = lambdas.get_function_configuration(
                    FunctionName=str(item["PhysicalResourceId"])
                )
                found[fragment] = str(configuration["Role"])
    return found


def _simulate(
    iam: Any, role: str, resource: str, context: Sequence[Mapping[str, object]] = ()
) -> dict[str, str]:
    arguments: dict[str, object] = {
        "PolicySourceArn": role,
        "ActionNames": list(_PROBE_ACTIONS),
        "ResourceArns": [resource],
    }
    if context:
        arguments["ContextEntries"] = list(context)
    results = iam.simulate_principal_policy(**arguments)["EvaluationResults"]
    return {str(item["EvalActionName"]): str(item["EvalDecision"]) for item in results}


def _leading_key(value: str) -> list[dict[str, object]]:
    return [
        {
            "ContextKeyName": "dynamodb:LeadingKeys",
            "ContextKeyValues": [value],
            "ContextKeyType": "stringList",
        }
    ]


_TRANSACTION: Final = (
    {
        "ContextKeyName": "dynamodb:EnclosingOperation",
        "ContextKeyValues": ["TransactWriteItems"],
        "ContextKeyType": "string",
    },
)


def _iam_isolation(session: Any, environment: str, live_arn: str) -> dict[str, object]:
    """V-15: simulate the worker, writer, API and probe principals against both table kinds."""
    account = live_arn.split(":")[4]
    region = live_arn.split(":")[3]
    probe_arns = [
        f"arn:aws:dynamodb:{region}:{account}:table/adlytics-{environment}-{suffix}"
        for suffix in ("execution-graph-probe", "execution-graph-probe-legacy")
    ]
    ingestion = _stack_roles(
        session,
        f"Adlytics-Ingestion-{environment}",
        [*(f"ExecutionGraph{name}" for name in WORKERS), "TableWriter", "IngestionFunction"],
    )
    api = _stack_roles(
        session, f"Adlytics-Api-{environment}", ["RunRequestsFunction", "OperationalStatusFunction"]
    )
    processor = _stack_roles(
        session, f"Adlytics-OperationalStatusProcessor-{environment}", ["GraphProjector"]
    )
    iam = session.client("iam")
    decisions: dict[str, dict[str, str]] = {}
    for name, role in {**ingestion, **api, **processor}.items():
        for index, arn in enumerate(probe_arns):
            decisions[f"{name}#{index}|probe"] = _simulate(iam, role, arn)
    probe_role = f"arn:aws:iam::{account}:role/adlytics-{environment}-execution-graph-probe"
    decisions["probe|live"] = _simulate(iam, probe_role, live_arn)
    for index, arn in enumerate(probe_arns):
        decisions[f"probe|probe#{index}"] = _simulate(iam, probe_role, arn)
    writer = ingestion["TableWriter"]
    decisions["TableWriter|mode_item"] = _simulate(iam, writer, live_arn, _leading_key(_MODE_PK))
    decisions["TableWriter|other_item"] = _simulate(iam, writer, live_arn, _leading_key("RUN#x"))
    # S6 (V-19, S1A-7): the delivery handler admits runs as the run-request API does, so it
    # writes run-request items; instead, every write to the mode item is explicitly denied.
    decisions["IngestionFunction|requester_mode_item"] = _simulate(
        iam, ingestion["IngestionFunction"], live_arn, _leading_key(_MODE_PK)
    )
    # S3V-9: the promoter condition-checks the hold revision on the mode item.
    promoter = ingestion["ExecutionGraphPromoter"]
    decisions["ExecutionGraphPromoter|mode_item"] = _simulate(
        iam, promoter, live_arn, _leading_key(_MODE_PK)
    )
    # S4-6: every worker, the release issuer included, is explicitly denied the operator-only
    # writes, also inside the transaction its family grant allows.
    for name in WORKERS:
        role = ingestion[f"ExecutionGraph{name}"]
        for label, key in _OPERATOR_ONLY_KEYS.items():
            decisions[f"ExecutionGraph{name}|operator_only#{label}"] = _simulate(
                iam, role, live_arn, [*_leading_key(key), *_TRANSACTION]
            )
    verdict = iam_isolation_verdict(decisions)
    verdict["roles"] = {**ingestion, **api, **processor}
    return verdict


def _store_tests(
    target: gate.Target, arguments: argparse.Namespace, stamp: str
) -> dict[str, object]:
    """Run the store file once through the fail-closed JUnit check."""
    evidence_dir = Path(arguments.evidence_dir)
    xml = evidence_dir / f"P3-store-live-{stamp}.xml"
    report = evidence_dir / f"P3-run-conflicts-{stamp}.json"
    environment = {
        **os.environ,
        "AWS_PROFILE": target.profile,
        "AWS_REGION": target.region,
        "ADL_TEST_ENV": target.environment,
        "ADL_P3_CONFLICT_REPORT": str(report),
    }
    completed = subprocess.run(  # noqa: S603 -- fixed argv, explicit inputs only
        [
            "uv",
            "run",
            "pytest",
            "-m",
            "integration",
            STORE_LIVE_FILE,
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
        totals: dict[str, object] = dict(gate.junit_verdict(text, STORE_LIVE_TESTS))
        totals["passed"] = completed.returncode == 0
    except gate.ProbeError as error:
        totals = {"passed": False, "error": str(error)}
    conflicts = cast(
        "dict[str, object]", json.loads(report.read_text()) if report.is_file() else {}
    )
    return {
        "junit": str(xml),
        "junit_sha256": gate.hashlib.sha256(text.encode()).hexdigest() if text else None,
        "returncode": completed.returncode,
        "tail": completed.stdout[-1500:],
        "totals": totals,
        "run_conflicts": conflicts,
        "passed": bool(totals.get("passed")) and run_conflicts_verdict(conflicts),
    }


def run_conflicts_verdict(report: Mapping[str, object]) -> bool:
    """R2-7: every parallel claim committed, none exhausted, and the margin is recorded.

    The store test measures `exhausted` (claims that raised the stop rule) and `max_attempts`
    (the most attempts one claim took); neither is a literal (S3V-10).
    """
    bound = report.get("bound")
    attempts = report.get("max_attempts")
    return (
        report.get("exhausted") == 0
        and report.get("parallel_claims") == bound
        and report.get("committed") == bound
        and isinstance(attempts, int)
        and isinstance(bound, int)
        and attempts >= 1
    )


def _schedules(session: Any, environment: str) -> dict[str, object]:
    events = session.client("events")
    rules: dict[str, str] = {}
    for name in WORKERS:
        rule = f"adlytics-{environment}-ExecutionGraph{name}Schedule"
        rules[rule] = str(events.describe_rule(Name=rule).get("State"))
    return schedule_verdict(rules)


def _worker_invocations(session: Any, environment: str, since: datetime) -> dict[str, object]:
    """Sum each worker's Lambda invocations and errors since the deploy; zero errors passes."""
    cloudwatch = session.client("cloudwatch")
    now = datetime.now(UTC)
    sums: dict[str, dict[str, float]] = {"Invocations": {}, "Errors": {}}
    for name in WORKERS:
        function = f"adlytics-{environment}-ExecutionGraph{name}"
        for metric, counts in sums.items():
            data = cloudwatch.get_metric_statistics(
                Namespace="AWS/Lambda",
                MetricName=metric,
                Dimensions=[{"Name": "FunctionName", "Value": function}],
                StartTime=since,
                EndTime=now,
                Period=60 * 60 * 24,
                Statistics=["Sum"],
            )
            counts[function] = sum(float(point["Sum"]) for point in data.get("Datapoints", []))
    return {
        "since": since.isoformat(),
        **worker_errors_verdict(sums["Invocations"], sums["Errors"]),
    }


def _ingestion_updated(session: Any, environment: str) -> datetime:
    # aws-pagination: an exact StackName returns one stack.
    described = session.client("cloudformation").describe_stacks(
        StackName=f"Adlytics-Ingestion-{environment}"
    )["Stacks"][0]
    return cast("datetime", described.get("LastUpdatedTime") or described["CreationTime"])


def _probe_unit(
    target: gate.Target, arguments: argparse.Namespace, session: Any, deployed: datetime
) -> dict[str, object]:
    """One `--no-twin` probe retrieval through the legacy chain and the new TableWriter."""
    worktree = Path(arguments.worktree).resolve()

    def land(*, dry_run: bool) -> dict[str, object]:
        request = gate.LandRequest(
            worktree,
            arguments.operation_id,
            arguments.raw_stack,
            arguments.bucket,
            dry_run,
            no_twin=True,
        )
        return gate.parse_probe_report(gate._land(target, request))

    report = land(dry_run=True)
    days = gate._window_days(str(report["key"]))
    before = gate._window_rows(session, target.environment, days)
    start = max(datetime.now(UTC) - timedelta(seconds=60), deployed)
    real = land(dry_run=False)
    if real["key"] != report["key"]:
        raise gate.ProbeError("the real put chose a different key than the dry run")
    versions = gate._exact_versions(session.client("s3"), arguments.bucket, str(real["key"]))
    if len(versions) != 1:
        raise gate.ProbeError("the probe key does not have exactly one version")
    digest = gate._plan_digest(str(real["key"]), versions[0])
    legacy = gate._legacy_readback(
        session, target.environment, digest, start, int(arguments.wait_seconds)
    )
    after = gate._window_rows(session, target.environment, days)
    equal = before["rows_sha256"] is not None and before["rows_sha256"] == after["rows_sha256"]
    contention = gate.contention_verdict(
        cast("dict[str, object]", legacy["raised_receive_findings"]),
        gate.ContentionOutcomes(
            cast("dict[str, object]", legacy["plan_state"]),
            int(cast("int", legacy["c4_activations"])),
            equal,
        ),
    )
    return {
        "created_keys": [real["data_key"], real["key"]],
        "version": versions[0],
        "legacy": legacy,
        "rows_before": before,
        "rows_after": after,
        "row_equality": equal,
        "contention": contention,
        "after_deploy": start >= deployed,
        "passed": bool(legacy["passed"])
        and equal
        and bool(contention["passed"])
        and start >= deployed,
    }


def probe_p3(target: gate.Target, arguments: argparse.Namespace) -> int:  # noqa: PLR0915
    """Run P3 and write its evidence; nonzero on any failed check."""
    evidence = gate.Evidence("P3", gate._utc_stamp())
    try:
        session, ready = gate._preflight(target, arguments, evidence)
        headers = gate.diff_header_check(
            [Path(path) for path in arguments.diff_file],
            cast("str | None", evidence.document.get("deployed_sha")),
        )
        evidence.document["diff_headers"] = headers
        p1 = json.loads(Path(arguments.p1_evidence).read_text())
        p2 = json.loads(Path(arguments.p2_evidence).read_text())
        if not (p1.get("passed") and p2.get("passed")):
            raise gate.ProbeError("the P1 or P2 evidence did not pass")
        key = str(p1["p0_probe_key"])
        versions = gate._exact_versions(session.client("s3"), arguments.bucket, key)
        if len(versions) != 1:
            raise gate.ProbeError("the P1 manifest key does not have exactly one version")
        table = gate._store_table(session, target.environment, str(arguments.store_stack))
        table_arn = str(
            # aws-pagination: DynamoDB DescribeTable reads one named table.
            session.client("dynamodb").describe_table(TableName=table)["Table"]["TableArn"]
        )
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
        census_start = gate._table_census(session, table)
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
        explain_status, explained = gate._explain(reader, {**reference, "limit": 25})
        head = session.client("s3").head_object(
            Bucket=arguments.bucket, Key=key, VersionId=versions[0]
        )
        last_modified = cast("datetime", head["LastModified"]).astimezone(UTC).isoformat()
        traversal = gate._traverse(reader, reference, 1)
        # S3V2-2: P2's receipt, root-clock and traversal invariants, against the deployed Api.
        explanation = gate.explanation_invariants(
            gate.ExplanationRead(
                explained,
                traversal,
                census_start.get("RECEIPT", 0),
                gate.p1_request_ids(p1),
                last_modified,
            )
        )
        oversize_status, oversize = gate._explain(reader, {**reference, "cursor": "A" * 2049})
        invalid_start = datetime.now(UTC) - timedelta(seconds=5)
        invalid_status, invalid = gate._explain(reader, {"flow_id": "not-a-uuid"})
        metric = gate._error_metric_count(
            session, target.environment, invalid_start, int(arguments.wait_seconds)
        )
        graphs = cast("list[dict[str, object]]", explained.get("graphs", []))
        baseline = set(gate.parse_baseline_graphs(arguments.baseline_graph))
        invariants["p2"] = {
            "explain_status": explain_status,
            "graph_ids": sorted(str(item.get("graph_id")) for item in graphs),
            "explanation": explained,
            "manifest_last_modified": last_modified,
            "traversal": traversal,
            "explanation_invariants": explanation,
            "oversize": {"status": oversize_status, "body": oversize},
            "invalid": {"status": invalid_status, "body": invalid},
            "error_metric": metric,
            "read_role_iam": gate._explanation_role_iam(session, target.environment, table_arn),
        }
        evidence.document["invariants"] = invariants
        stamp = str(evidence.started_at)
        store = _store_tests(target, arguments, stamp)
        evidence.document["store_live"] = store
        isolation = _iam_isolation(session, target.environment, table_arn)
        evidence.document["iam_isolation"] = isolation
        deployed = _ingestion_updated(session, target.environment)
        evidence.document["ingestion_updated"] = deployed.isoformat()
        schedules = _schedules(session, target.environment)
        evidence.document["schedules"] = schedules
        invocations = _worker_invocations(session, target.environment, deployed)
        evidence.document["worker_invocations"] = invocations
        if not ready:
            raise gate.ProbeError("asset readback, import scan or alarm inventory failed; no put")
        unit = _probe_unit(target, arguments, session, deployed)
        evidence.document["probe_unit"] = unit
        census_end = gate._table_census(session, table)
        evidence.document["census"] = {"start": census_start, "end": census_end}
        p2_checks = cast("dict[str, object]", invariants["p2"])
        checks: dict[str, bool] = {
            "preflight": ready,
            "diff_headers_name_the_deployed_sha": bool(headers["passed"]),
            "mode_shadow": mode.get("mode") == "SHADOW",
            "table_invariants": bool(
                cast("dict[str, object]", invariants["table_invariants"])["passed"]
            ),
            "sole_writer_s3_form": bool(
                cast("dict[str, object]", invariants["sole_writer"])["passed"]
            ),
            "p1_refusals_still_refuse_and_write_nothing": bool(
                cast("dict[str, object]", invariants["refusals"])["passed"]
            ),
            "shadow_graphs_byte_identical": bool(
                cast("dict[str, object]", invariants["shadow_graphs"])["passed"]
            ),
            "p2_explanation_shadow_epoch_0": explain_status == 200
            and {str(g["graph_id"]) for g in graphs} == baseline
            and all(g["authority_epoch"] == 0 and g["label"] == "shadow" for g in graphs),
            **{f"p2_{name}": passed for name, passed in explanation.items()},
            "p2_oversize_cursor_capacity_limited": (oversize_status, oversize)
            == (413, {"error": "CURSOR_CAPACITY_LIMITED"}),
            "p2_invalid_one_error_metric": invalid_status == 400
            and cast("dict[str, object]", p2_checks["error_metric"])["settled_count"] == 1,
            "p2_read_role_has_no_graph_write": bool(
                cast("dict[str, object]", p2_checks["read_role_iam"])["passed"]
            ),
            "store_live_junit_clean_and_no_claim_exhausted": bool(store["passed"]),
            "v15_live_iam_isolation": bool(isolation["passed"]),
            "worker_schedules_enabled": bool(schedules["passed"]),
            "zero_worker_errors_since_deploy": bool(invocations["passed"]),
            "zero_graph_sends_no_epoch1_work": census_has_no_graph_work(census_end),
            "live_graph_table_unchanged_but_receipts": {
                name: count for name, count in census_end.items() if name not in {"RECEIPT"}
            }
            == {name: count for name, count in census_start.items() if name not in {"RECEIPT"}},
            "probe_unit_commits_through_the_new_table_writer": bool(unit["passed"]),
        }
        evidence.document["checks"] = checks
        evidence.document["passed"] = all(checks.values())
    except Exception as error:
        evidence.document["passed"] = False
        evidence.document["error"] = f"{type(error).__name__}: {error}"[:2000]
    path = evidence.write(target)
    print(f"evidence: {path}")
    return 0 if evidence.document.get("passed") else 1
