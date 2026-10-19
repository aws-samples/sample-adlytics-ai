# SPDX-License-Identifier: MIT-0
"""Unit contract for the dev-only reprocess telemetry acceptance probe."""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "validation"))
sys.path.insert(0, str(_ROOT))
from validation import probe_reprocess_telemetry as probe  # noqa: E402

pytestmark = pytest.mark.unit

_TRACE_ID = "a" * 32
_ROOT_SPAN_ID = "1" * 16
_EXECUTION_ID = "b" * 32
_DIGEST = "sha256:" + "c" * 64
_OPERATION_ID = "fictional-reprocess-operation"


def _result(stdout: str, returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess[str]:
    # A-SUBPROCESS-001: a result value; starts no process; see docs/security-dispositions.md
    # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
    return subprocess.CompletedProcess(
        args=[sys.executable, "-m", "adapters.jobs.reprocess_semantic_v2"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _event(  # noqa: PLR0913 — one keyword per lifecycle field under test
    phase: str,
    *,
    outcome: str,
    stage: str,
    completed: int,
    total: int,
    timestamp: str,
    tracing: bool | None = None,
) -> dict[str, object]:
    event: dict[str, object] = {
        "message": "operation lifecycle",
        "service": "adlytics-reprocess-semantic-v2",
        "timestamp": timestamp,
        "level": "INFO",
        "location": "_lifecycle:1",
        "operation": "reprocess.semantic_v2",
        "operation_id": _OPERATION_ID,
        "component": "reprocess_semantic_v2.operator",
        "phase": phase,
        "outcome": outcome,
        "execution_id": _EXECUTION_ID,
        "stage": stage,
        "completed": completed,
        "total": total,
        "trace_id": _TRACE_ID,
        "span_id": _ROOT_SPAN_ID,
    }
    if phase not in {"start", "identity"}:
        event["plan_digest"] = _DIGEST
    if tracing is not None:
        event["tracing"] = tracing
    return event


def _lifecycle_output(
    *,
    progress_counts: tuple[int, ...] = (0, 1, 2),
    heartbeat: bool = False,
    terminal_timestamp: str = "2026-08-30T12:00:10+00:00",
) -> str:
    events = [
        _event(
            "start",
            outcome="started",
            stage="start",
            completed=0,
            total=0,
            timestamp="2026-08-30T12:00:00+00:00",
            tracing=True,
        )
    ]
    for ordinal, completed in enumerate(progress_counts, start=1):
        events.append(
            _event(
                "progress",
                outcome="completed",
                stage="plan" if completed == 0 else "apply",
                completed=completed,
                total=2,
                timestamp=f"2026-08-30T12:00:0{ordinal}+00:00",
            )
        )
    if heartbeat:
        events.append(
            _event(
                "heartbeat",
                outcome="unchanged",
                stage="apply",
                completed=progress_counts[-1],
                total=2,
                timestamp="2026-08-30T12:00:31+00:00",
            )
        )
    events.append(
        _event(
            "terminal",
            outcome="completed",
            stage="acceptance",
            completed=2,
            total=2,
            timestamp=terminal_timestamp,
        )
    )
    return "\n".join(json.dumps(event) for event in events)


def _accepted_lifecycle() -> probe._LifecycleAcceptance:  # pyright: ignore[reportPrivateUsage]
    return probe._LifecycleAcceptance(  # pyright: ignore[reportPrivateUsage]
        operation_id=_OPERATION_ID,
        execution_id=_EXECUTION_ID,
        predecessor_execution_id=None,
        plan_digest=_DIGEST,
        trace_id=_TRACE_ID,
        root_span_id=_ROOT_SPAN_ID,
        completed=2,
        total=2,
        progress_records=3,
        heartbeat_records=0,
    )


def _lineage(stage: str, completed: int, total: int) -> dict[str, object]:
    return {
        "operation_id": _OPERATION_ID,
        "execution_id": _EXECUTION_ID,
        "predecessor_execution_id": "none",
        "plan_digest": _DIGEST,
        "stage": stage,
        "completed": completed,
        "total": total,
    }


def _span(  # noqa: PLR0913 — one keyword per span document field under test
    ordinal: int,
    name: str,
    attributes: Mapping[str, object],
    *,
    parent: str | None,
) -> dict[str, object]:
    document: dict[str, object] = {
        "traceId": _TRACE_ID,
        "spanId": f"{ordinal:016x}",
        "name": name,
        "attributes": dict(attributes),
    }
    if parent is not None:
        document["parentSpanId"] = parent
    return document


def _documents() -> list[dict[str, object]]:
    root_id = _ROOT_SPAN_ID
    apply_id = "3" * 16
    delivery_id = "4" * 16
    ingest_id = "5" * 16
    partition_id = "6" * 16
    return [
        {
            "traceId": _TRACE_ID,
            "spanId": root_id,
            "name": "reprocess.semantic_v2",
            "attributes": _lineage("acceptance", 2, 2),
        },
        _span(2, "reprocess.semantic_v2.plan", _lineage("plan", 1, 1), parent=root_id),
        {
            "traceId": _TRACE_ID,
            "spanId": apply_id,
            "parentSpanId": root_id,
            "name": "reprocess.semantic_v2.apply",
            "attributes": _lineage("apply", 2, 2),
        },
        {
            "traceId": _TRACE_ID,
            "spanId": delivery_id,
            "parentSpanId": apply_id,
            "name": "reprocess.semantic_v2.delivery",
            "attributes": {**_lineage("apply", 2, 2), "contract_ids": ["fictional-contract"]},
        },
        {
            "traceId": _TRACE_ID,
            "spanId": ingest_id,
            "parentSpanId": delivery_id,
            "name": "ingest.delivery",
            "attributes": {
                **_lineage("apply", 2, 2),
                "contract_ids": ["fictional-contract"],
            },
        },
        {
            "traceId": _TRACE_ID,
            "spanId": partition_id,
            "parentSpanId": ingest_id,
            "name": "ingest.partition",
            "attributes": {
                **_lineage("apply.partition", 1, 1),
                "contract_ids": ["fictional-contract"],
                "bucket_starts": ["2026-08-30"],
            },
        },
        _span(
            7,
            "athena.statement",
            {
                "operation_kind": "merge",
                "logical_table": "fictional_table",
                "query_id": "fictional-query-reference",
                "queue_millis": 1,
                "planning_millis": 2,
                "engine_millis": 3,
                "total_millis": 6,
            },
            parent=partition_id,
        ),
        _span(
            8,
            "reprocess.semantic_v2.acceptance",
            {**_lineage("acceptance", 2, 2), "accepted": True},
            parent=root_id,
        ),
    ]


def test_dry_plan_parser_requires_one_digest_and_paired_nonempty_delivery_lines() -> None:
    output = (
        "SEMANTIC V2 REPROCESS PLAN\n"
        f"  digest:    {_DIGEST}\n"
        "  manifest fictional/path version=fake checksum=fake\n"
        "  object   fictional/path version=fake bytes=1 checksum=fake\n"
    )

    parsed = probe.parse_dry_plan(_result(output))

    assert parsed.digest == _DIGEST
    assert parsed.delivery_count == 1
    with pytest.raises(Exception, match="plan_digest_cardinality"):
        probe.parse_dry_plan(_result(output + f"  digest: {_DIGEST}\n"))
    with pytest.raises(Exception, match="plan_empty_or_ambiguous"):
        probe.parse_dry_plan(_result(f"  digest: {_DIGEST}\n"))
    with pytest.raises(Exception, match="subprocess_failed"):
        probe.parse_dry_plan(_result(output, returncode=1))


def test_lifecycle_parser_accepts_exact_short_monotonic_completion() -> None:
    accepted = probe.parse_lifecycle(
        _result(_lifecycle_output()),
        operation_id=_OPERATION_ID,
        expected_digest=_DIGEST,
        expected_predecessor=None,
    )

    assert accepted.completed == accepted.total == 2
    assert accepted.progress_records == 3
    assert accepted.heartbeat_records == 0
    assert accepted.trace_id == _TRACE_ID


def test_lifecycle_parser_rejects_regression_and_missing_long_run_heartbeat() -> None:
    with pytest.raises(Exception, match="progress_not_monotonic"):
        probe.parse_lifecycle(
            _result(_lifecycle_output(progress_counts=(0, 2, 1, 2))),
            operation_id=_OPERATION_ID,
            expected_digest=_DIGEST,
            expected_predecessor=None,
        )
    with pytest.raises(Exception, match="heartbeat_missing"):
        probe.parse_lifecycle(
            _result(_lifecycle_output(terminal_timestamp="2026-08-30T12:00:31+00:00")),
            operation_id=_OPERATION_ID,
            expected_digest=_DIGEST,
            expected_predecessor=None,
        )


def test_lifecycle_parser_rejects_ambiguous_terminal_and_prohibited_content() -> None:
    output = _lifecycle_output()
    terminal = _event(
        "terminal",
        outcome="completed",
        stage="acceptance",
        completed=2,
        total=2,
        timestamp="2026-08-30T12:00:10+00:00",
    )
    with pytest.raises(Exception, match="terminal_cardinality"):
        probe.parse_lifecycle(
            _result(output + "\n" + json.dumps(terminal)),
            operation_id=_OPERATION_ID,
            expected_digest=_DIGEST,
            expected_predecessor=None,
        )
    unsafe = json.loads(output.splitlines()[0])
    unsafe["payload"] = "fictional"
    with pytest.raises(Exception, match=r"unbounded_field_present|prohibited_field_present"):
        probe.parse_lifecycle(
            _result(json.dumps(unsafe) + "\n" + "\n".join(output.splitlines()[1:])),
            operation_id=_OPERATION_ID,
            expected_digest=_DIGEST,
            expected_predecessor=None,
        )


def test_span_hierarchy_accepts_required_trace_and_athena_timings() -> None:
    accepted = probe.validate_span_hierarchy(_documents(), _accepted_lifecycle())

    assert accepted.required_name_count == 8
    assert accepted.athena_statement_count == 1
    assert accepted.span_count == 8


def test_span_hierarchy_rejects_wrong_fixed_parent() -> None:
    documents = _documents()
    plan = next(item for item in documents if item["name"] == "reprocess.semantic_v2.plan")
    plan["parentSpanId"] = "4" * 16

    with pytest.raises(Exception, match="fixed_parent_mismatch"):
        probe.validate_span_hierarchy(documents, _accepted_lifecycle())


@pytest.mark.parametrize("forbidden_key", ["sql", "parameters", "rows"])
def test_span_hierarchy_rejects_forbidden_attributes(forbidden_key: str) -> None:
    documents = _documents()
    athena = next(item for item in documents if item["name"] == "athena.statement")
    attributes = cast("dict[str, object]", athena["attributes"])
    attributes[forbidden_key] = "SELECT value FROM fictional_table"

    with pytest.raises(Exception, match="prohibited_field_present"):
        probe.validate_span_hierarchy(documents, _accepted_lifecycle())


def test_span_hierarchy_rejects_missing_athena_timing_and_negative_timing() -> None:
    missing = _documents()
    missing_athena = next(item for item in missing if item["name"] == "athena.statement")
    cast("dict[str, object]", missing_athena["attributes"]).pop("query_id")
    with pytest.raises(Exception, match="athena_timing_attribute_missing"):
        probe.validate_span_hierarchy(missing, _accepted_lifecycle())

    negative = _documents()
    negative_athena = next(item for item in negative if item["name"] == "athena.statement")
    cast("dict[str, object]", negative_athena["attributes"])["queue_millis"] = -1
    with pytest.raises(Exception, match="athena_timing_attribute_invalid"):
        probe.validate_span_hierarchy(negative, _accepted_lifecycle())


@pytest.mark.parametrize(
    ("environment", "region"),
    [("production", "us-east-1"), ("dev", "us-west-2")],
)
def test_main_refuses_non_dev_target_before_any_aws_path(
    capsys: pytest.CaptureFixture[str], environment: str, region: str
) -> None:
    exit_code = probe.main(
        [
            "--profile",
            "explicit-profile",
            "--environment",
            environment,
            "--region",
            region,
            "--expected-account",
            "000000000000",
            "--data-stack",
            "Adlytics-DataV2-dev",
            "--observability-stack",
            "Adlytics-Observability-dev",
            "--bucket",
            "fictional-bucket",
            "--window-start",
            "2026-08-30",
            "--window-end",
            "2026-08-30",
            "--operation-id",
            _OPERATION_ID,
            "--contract",
            "fictional-contract",
            "--retrieved-token",
            "20260830T120000Z",
        ]
    )

    assert exit_code == 2
    summary = json.loads(capsys.readouterr().out)
    assert summary == {
        "outcome": "dev_target_required",
        "stage": "target_validation",
        "status": "failed",
        "trace_id_present": False,
    }
