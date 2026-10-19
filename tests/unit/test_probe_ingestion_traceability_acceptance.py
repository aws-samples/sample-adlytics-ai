# SPDX-License-Identifier: MIT-0
"""Items 13-14: the `--acceptance-verdicts` gatherers, over retained-artifact fixtures.

No AWS: the live collectors are injected, and every builder reads the same artifact shapes the
live reads retain. Fictional data only.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import pathlib
import sys
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "validation"))
sys.path.insert(0, str(_ROOT))

from validation import probe_ingestion_traceability_acceptance as acceptance  # noqa: E402
from validation import probe_turn_latency as latency  # noqa: E402

gate = acceptance.gate  # the one gate module the gatherers raise and judge with

pytestmark = pytest.mark.unit

_EXECUTION = "exec-1"
_EARLIER = "exec-0"
_CODE_SHA = "c" * 40
_BAKE_START = datetime(2026, 10, 15, 10, 10, tzinfo=UTC)
_BAKE_END = datetime(2026, 10, 15, 10, 30, tzinfo=UTC)


def _nanos(instant: datetime) -> str:
    return str(int(instant.timestamp() * 1_000_000_000))


def _span(name: str, span_id: str, parent: str | None, **attributes: object) -> dict[str, object]:
    return {
        "name": name,
        "traceId": "t" * 32,
        "spanId": span_id,
        "parentSpanId": parent,
        "startTimeUnixNano": _nanos(_BAKE_START + timedelta(minutes=1)),
        "endTimeUnixNano": _nanos(_BAKE_START + timedelta(minutes=2)),
        "attributes": {
            "run_id": "run-a",
            "task_id": "task",
            "cohort_revision": "c-a",
            **attributes,
        },
    }


def _cohort_spans() -> list[dict[str, object]]:
    run = _span("bake.run", "r", None, outcome="completed")
    run["startTimeUnixNano"] = _nanos(_BAKE_START)
    # The runtime's two finalize spans: arm precompute, then finalize.
    armed = _span("bake.finalize", "fa", "y", outcome="graph_bake_precompute_armed")
    final = _span("bake.finalize", "f", "y", outcome="graph_bake_finalized")
    final["endTimeUnixNano"] = _nanos(_BAKE_END)
    return [
        run,
        _span("bake.source_pin", "sp", "r"),
        _span("bake.fan_out", "fo", "r"),
        _span("bake.task", "t1", "x", page="p", variant="1d"),
        _span("bake.phase", "ph1", "t1", phase="plan"),
        _span("bake.phase", "ph3", "t1", phase="query"),
        _span("bake.phase", "ph4", "t1", phase="render"),
        _span("bake.phase", "ph2", "t1", phase="write"),
        armed,
        final,
        _span("precompute.run", "pr", "z"),
        _span("insight.precompute.plan", "pl", "pr"),
        _span("insight.model.call", "m", "pr"),
        _span("artifact.write", "w", "t1", bytes=120),
        _span("athena.query", "q", "t1", query_id="q-1", bytes_scanned=4096, state="SUCCEEDED"),
    ]


def _promoter_spans() -> list[dict[str, object]]:
    # x9's shape: an idle pass (no domain child) precedes the pass that owns the attempt.
    return [
        {
            "name": "execution_graph.promoter_pass",
            "traceId": "i" * 32,
            "spanId": "pi",
            "endTimeUnixNano": "1",
            "attributes": {"aws_request_id": "req-idle", "outcome": "completed"},
        },
        {
            "name": "execution_graph.promoter_pass",
            "traceId": "p" * 32,
            "spanId": "pp",
            "endTimeUnixNano": "1",
            "attributes": {"aws_request_id": "req-1", "outcome": "completed"},
        },
        {
            "name": "execution_graph.promoter_domain",
            "traceId": "p" * 32,
            "spanId": "pd",
            "parentSpanId": "pp",
            "endTimeUnixNano": "1",
            "attributes": {
                "aws_request_id": "req-1",
                "domain": "d-1",
                "cohort_revision": "c-a",
                "outcome": "elected",
            },
        },
    ]


def _artifacts(**overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "schema_version": "trace-artifacts-v1",
        "acceptance_execution_id": _EXECUTION,
        "cohorts": [
            {
                "cohort_revision": "c-a",
                "run_id": "run-a",
                "grant_id": "grant-a",
                "window_start": "2026-10-15T10:00:00+00:00",
                "window_end": "2026-10-15T11:00:00+00:00",
            }
        ],
        "roster": ["p#1d"],
        "phases": ["plan", "write"],
        "retrievals": {
            "c-a": {"status": "complete", "truncated": False, "spans": _cohort_spans()},
            "promoter": {"status": "complete", "truncated": False, "spans": _promoter_spans()},
        },
        "athena_executions": [
            {
                "query_execution_id": "q-1",
                "workgroup": "bake",
                "state": "SUCCEEDED",
                "submitted_at": "2026-10-15T10:12:00+00:00",
                "data_scanned_bytes": 4096,
            }
        ],
        "request_rosters": {"grant-a": [["k-1", "requested"], ["k-2", "no_request"]]},
        "task_summaries": [
            {
                "phase": "task_summary",
                "run_id": "run-a",
                "page": "p",
                "variant": "1d",
                "task_id": "task",
                "effect_kind": "written",
                "written_bytes": 120,
                "pairs": 1,
                "query_executions": 1,
                "scanned_bytes": 4096,
                "statistics_unavailable": 0,
            }
        ],
        "slot_generations": {"run-a": 1},
        "transitions": [
            {"aws_request_id": "req-1", "domain": "d-1", "drained_cohort_revision": "c-a"}
        ],
    }
    document.update(overrides)
    return document


# --- Trace completeness ----------------------------------------------------------------------


def test_live_shaped_trace_evidence_passes_and_every_paired_control_is_rejected() -> None:
    result = acceptance.trace_receipt_result(_artifacts())

    assert result["passed"] is True, result
    controls = cast("dict[str, dict[str, object]]", result["negative_controls"])
    assert set(acceptance.TRACE_REQUIRED_CONTROLS) <= set(controls)
    assert all(control["rejected"] for control in controls.values())


def test_the_pass_controls_mutate_the_pass_that_owns_an_attempt_not_an_idle_one() -> None:
    evidence, _ = acceptance.trace_evidence(_artifacts())
    controls = acceptance.trace_negative_controls(evidence)
    for name in (
        "omit:execution_graph.promoter_pass",
        "request:execution_graph.promoter_pass",
        "other-pass:execution_graph.promoter_domain",
    ):
        assert controls[name]["rejected"] is True, (name, controls[name])
    # Paired control: x9's old selection omitted the first pass span, an idle one; the verdict
    # rightly ignores idle passes, so that mutation passed and the control was accepted.
    idle = tuple(span for span in evidence.spans if span.get("spanId") != "pi")
    mutated = acceptance.TraceCompletenessEvidence(
        evidence.cohorts, evidence.promoter_attempts, idle, evidence.retrieval_complete
    )
    assert acceptance.trace_completeness_verdict(mutated)["passed"] is True


def test_the_expectation_comes_from_records_outside_the_spans() -> None:
    evidence, failures = acceptance.trace_evidence(_artifacts())
    assert failures == []
    (expected,) = evidence.cohorts
    assert expected.bake_task_keys == ("p#1d",)
    assert expected.task_writes == (("task", True),)
    assert (expected.artifact_writes, expected.artifact_bytes) == (1, (120,))
    assert (expected.athena_queries, expected.athena_succeeded_ids) == (1, frozenset({"q-1"}))
    assert expected.athena_bytes_scanned == 4096
    assert expected.model_requests == 1  # the run's generated slots, not its roster
    assert evidence.promoter_attempts == (("req-1", "d-1", "c-a"),)


def _summary(**changes: object) -> list[dict[str, object]]:
    (row,) = cast("list[dict[str, object]]", _artifacts()["task_summaries"])
    return [{**row, **changes}]


def test_workgroup_executions_outside_the_cohort_never_join_its_expectation() -> None:
    # x5: the old probe attributed every BAKE-workgroup execution by time window (935 for 302
    # cohort queries). Other executions in the workgroup must not change the verdict.
    executions = [
        *cast("list[object]", _artifacts()["athena_executions"]),
        {
            "query_execution_id": "q-other",
            "workgroup": "bake",
            "state": "SUCCEEDED",
            "submitted_at": "2026-10-15T12:00:00+00:00",
            "data_scanned_bytes": 10,
        },
    ]
    assert acceptance.trace_receipt_result(_artifacts(athena_executions=executions))["passed"]


@pytest.mark.parametrize(
    ("changes", "failure"),
    [
        ({"query_executions": 2}, "athena.query: 1 spans, expected 2"),
        ({"scanned_bytes": 4097}, "athena.query: statistics differ"),
        ({"pairs": 2}, "artifact.write: 1 spans, expected 2"),
        ({"written_bytes": 121}, "artifact.write: written bytes differ"),
        ({"effect_kind": "unchanged", "pairs": 0}, "phases differ"),
    ],
)
def test_each_count_is_bound_to_the_task_summary(changes: dict[str, object], failure: str) -> None:
    result = acceptance.trace_receipt_result(_artifacts(task_summaries=_summary(**changes)))
    failures = cast("dict[str, list[str]]", result["positive"])["failures"]
    assert result["passed"] is False and any(failure in item for item in failures), failures


_REQUESTLESS_SUMMARY: dict[str, object] = {
    "phase": "task_summary",
    "run_id": "run-a",
    "page": "u",
    "variant": "1d",
    "task_id": "task-u",
    "effect_kind": "unavailable",
    "written_bytes": 0,
    "pairs": 0,
    "query_executions": 0,
    "executed_distinct": 0,
    "scanned_bytes": 0,
    "statistics_unavailable": 0,
    "phase_plan_ms": 0,
    "phase_query_ms": 0,
    "phase_render_ms": 0,
    "phase_write_ms": 0,
}


def _with_requestless_task(
    summary: dict[str, object] | None = None, phases: tuple[str, ...] = ("plan",)
) -> dict[str, object]:
    """d2-live-20261016-r2's shape: a `no_request` row beside the written one, plan only."""
    task = _span("bake.task", "tu", "x", page="u", variant="1d")
    task["attributes"]["task_id"] = "task-u"  # type: ignore[index]
    extra = [task]
    for number, phase in enumerate(phases):
        span = _span("bake.phase", f"pu{number}", "tu", phase=phase)
        span["attributes"]["task_id"] = "task-u"  # type: ignore[index]
        extra.append(span)
    retrievals = cast("dict[str, dict[str, object]]", _artifacts()["retrievals"])
    retrievals["c-a"]["spans"] = [*_cohort_spans(), *extra]
    rows = cast("list[dict[str, object]]", _artifacts()["task_summaries"])
    return _artifacts(
        roster=["p#1d", "u#1d"],
        retrievals=retrievals,
        task_summaries=[*rows, {**_REQUESTLESS_SUMMARY, **(summary or {})}],
    )


def test_a_requestless_task_records_one_plan_and_no_round() -> None:
    # d2-live-20261016-r2: every reach/retention/audiences task was `no_request`; the runtime
    # planned it, bound nothing and opened no query/render round, so plan-only is complete.
    result = acceptance.trace_receipt_result(_with_requestless_task())
    assert result["passed"] is True, result
    (expected,) = acceptance.trace_evidence(_with_requestless_task())[0].cohorts
    assert expected.requestless_tasks == frozenset({"task-u"})
    controls = cast("dict[str, dict[str, object]]", result["negative_controls"])
    assert set(acceptance.TRACE_REQUESTLESS_CONTROLS) <= set(controls)
    assert all(controls[name]["rejected"] for name in acceptance.TRACE_REQUESTLESS_CONTROLS)


@pytest.mark.parametrize(
    "phases",
    [(), ("plan", "plan"), ("plan", "query", "render"), ("plan", "query"), ("plan", "write")],
)
def test_a_requestless_task_with_a_missing_or_extra_phase_fails(phases: tuple[str, ...]) -> None:
    result = acceptance.trace_receipt_result(_with_requestless_task(phases=phases))
    failures = cast("dict[str, list[str]]", result["positive"])["failures"]
    assert any("task task-u phases differ" in item for item in failures), failures


def test_a_requestless_plan_under_another_parent_fails() -> None:
    artifacts = _with_requestless_task()
    retrievals = cast("dict[str, dict[str, Any]]", artifacts["retrievals"])
    for span in retrievals["c-a"]["spans"]:
        if span["spanId"] == "pu0":
            span["parentSpanId"] = "t1"  # the written task's span: same class, another task
    positive = cast("dict[str, list[str]]", acceptance.trace_receipt_result(artifacts)["positive"])
    assert any("a phase under another task" in item for item in positive["failures"]), positive
    for span in retrievals["c-a"]["spans"]:
        if span["spanId"] == "pu0":
            span["parentSpanId"] = "0" * 16
    positive = cast("dict[str, list[str]]", acceptance.trace_receipt_result(artifacts)["positive"])
    assert any("bake.phase: wrong parent" in item for item in positive["failures"]), positive


@pytest.mark.parametrize(
    "summary",
    [
        {"effect_kind": "unchanged"},
        {"query_executions": 1},
        {"executed_distinct": 1},
        {"phase_query_ms": 3},
        {"phase_render_ms": 1},
        {"phase_query_ms": None},
        {"executed_distinct": False},
    ],
)
def test_a_summary_that_is_not_requestless_still_requires_a_round(
    summary: dict[str, object],
) -> None:
    # Paired control: only an unavailable summary recording zero executions and zero round time
    # relaxes the round; any other record (or a missing field) keeps at-least-one.
    evidence, _ = acceptance.trace_evidence(_with_requestless_task(summary))
    (expected,) = evidence.cohorts
    assert "task-u" not in expected.requestless_tasks
    failures = cast("list[str]", gate.trace_completeness_verdict(evidence)["failures"])
    assert any("task task-u phases differ" in item for item in failures), failures


def test_a_query_span_naming_no_retained_succeeded_execution_fails() -> None:
    executions = [{**cast("list[dict[str, object]]", _artifacts()["athena_executions"])[0]}]
    executions[0]["state"] = "FAILED"
    result = acceptance.trace_receipt_result(_artifacts(athena_executions=executions))
    failures = cast("dict[str, list[str]]", result["positive"])["failures"]
    assert any("query ids differ" in item for item in failures)


def test_model_calls_are_expected_only_for_generated_slots() -> None:
    # x5: every slot was a cache hit, so zero model-call spans is correct.
    spans = [s for s in _cohort_spans() if s["name"] != "insight.model.call"]
    retrievals = cast("dict[str, dict[str, object]]", _artifacts()["retrievals"])
    retrievals["c-a"] = {"status": "complete", "truncated": False, "spans": spans}
    none = _artifacts(retrievals=retrievals, slot_generations={"run-a": 0})
    no_generation = acceptance.trace_receipt_result(none)
    assert no_generation["passed"] is True, no_generation
    assert no_generation["missing_controls"] == []
    # Paired control: a recorded generation with no span fails.
    missing = _artifacts(retrievals=retrievals, slot_generations={"run-a": 1})
    assert acceptance.trace_receipt_result(missing)["passed"] is False
    with pytest.raises(gate.ProbeError, match="slot generations"):
        acceptance.trace_evidence(
            {k: v for k, v in _artifacts().items() if k != "slot_generations"}
        )


def test_one_finalize_span_or_one_phase_round_short_fails() -> None:
    for drop in ("fa", "ph4"):
        spans = [s for s in _cohort_spans() if s["spanId"] != drop]
        retrievals = cast("dict[str, dict[str, object]]", _artifacts()["retrievals"])
        retrievals["c-a"] = {"status": "complete", "truncated": False, "spans": spans}
        assert acceptance.trace_receipt_result(_artifacts(retrievals=retrievals))["passed"] is False


def test_a_transition_without_its_domain_child_fails() -> None:
    transitions = [
        *cast("list[object]", _artifacts()["transitions"]),
        {"aws_request_id": "req-9", "domain": "d-1", "drained_cohort_revision": "c-b"},
    ]
    result = acceptance.trace_receipt_result(_artifacts(transitions=transitions))
    assert result["passed"] is False
    assert result["attempt_failures"] == ["transition req-9 has no promoter-domain child"]


def test_an_incomplete_retrieval_or_uncovered_roster_fails() -> None:
    retrievals = cast("dict[str, dict[str, object]]", _artifacts()["retrievals"])
    retrievals["c-a"]["status"] = "pending"
    assert acceptance.trace_receipt_result(_artifacts(retrievals=retrievals))["passed"] is False
    roster = acceptance.trace_receipt_result(_artifacts(roster=["p#1d", "q#7d"]))
    assert roster["passed"] is False and "cover the roster" in str(roster["reasons"])


def test_the_bake_interval_is_proven_from_the_retained_telemetry() -> None:
    start, end = acceptance.bake_interval(_artifacts(), "run-a")
    assert (start, end) == (_BAKE_START.isoformat(), _BAKE_END.isoformat())
    spans = [span for span in _cohort_spans() if span["name"] != "bake.finalize"]
    retrievals = {"c-a": {"status": "complete", "spans": spans}}
    with pytest.raises(gate.ProbeError, match="successful finalize"):
        acceptance.bake_interval(_artifacts(retrievals=retrievals), "run-a")


def _binding(kind: str, cohort: str) -> dict[str, object]:
    return {"binding_kind": {"S": kind}, "cohort_revision": {"S": cohort}}


def _run(grant: str, start: str, end: str) -> dict[str, object]:
    return {
        "grant_id": {"S": grant},
        "root_clock_started_at": {"S": start},
        "terminal_at": {"S": end},
    }


def test_control_cohorts_serialize_the_chain_windows_by_election() -> None:
    control: dict[str, object] = {
        "run_ids": {"s": "run-s", "a0": "run-a0", "a1": "run-a1", "b0": "run-b0"},
        "observations": {
            "single_root_terminal": {"run-s": {}},
            "waves_terminal": {"run-a0": {}, "run-a1": {}, "run-b0": {}},
            "a_snapshot_before": {"elected_run_ids": ["run-a0", "run-a1"]},
            "b_active": {"elected_run_ids": ["run-b0"]},
        },
    }
    items: dict[tuple[str, str], dict[str, object]] = {
        ("RUN#run-s", "RUN"): _run("g-s", "2026-10-15T10:00:00+00:00", "2026-10-15T10:20:00+00:00"),
        ("RUN#run-a0", "RUN"): _run(
            "g-a0", "2026-10-15T10:21:00+00:00", "2026-10-15T10:50:00+00:00"
        ),
        ("RUN#run-a1", "RUN"): _run(
            "g-a1", "2026-10-15T10:21:00+00:00", "2026-10-15T10:50:00+00:00"
        ),
        ("RUN#run-b0", "RUN"): _run(
            "g-b0", "2026-10-15T10:30:00+00:00", "2026-10-15T11:20:00+00:00"
        ),
        ("SERVING#g-s", "BINDING"): _binding("direct", "c-s"),
        ("SERVING#g-a0", "BINDING"): _binding("alias", "c-a"),
        ("SERVING#g-a1", "BINDING"): _binding("direct", "c-a"),
        ("SERVING#g-b0", "BINDING"): _binding("direct", "c-b"),
    }
    cohorts = acceptance.control_cohorts(control, lambda pk, sk: items.get((pk, sk)))
    assert [(c.cohort_revision, c.run_id) for c in cohorts] == [
        ("c-s", "run-s"),
        ("c-a", "run-a1"),
        ("c-b", "run-b0"),
    ]
    # B launched while A was ACTIVE, but its chain window starts where A's ended.
    assert cohorts[2].window_start == "2026-10-15T10:50:00+00:00"
    items[("SERVING#g-a0", "BINDING")] = _binding("direct", "c-a")
    with pytest.raises(gate.ProbeError, match="2 direct bindings"):
        acceptance.control_cohorts(control, lambda pk, sk: items.get((pk, sk)))


class _Athena:
    """Two newest-first pages of BAKE executions; reads stop at the first older one."""

    def __init__(self) -> None:
        self.pages_read = 0
        at = datetime(2026, 10, 15, 10, 20, tzinfo=UTC)
        self.executions = {
            "q-new": at,
            "q-in": at - timedelta(minutes=10),
            "q-old": at - timedelta(hours=3),
            "q-older": at - timedelta(hours=4),
        }

    def get_paginator(self, name: str) -> Any:
        assert name == "list_query_executions"
        athena = self

        class _Pages:
            def paginate(self, **kwargs: Any) -> Any:
                if kwargs["WorkGroup"].endswith("-bake"):
                    for page in (["q-new", "q-in"], ["q-old"], ["q-older"]):
                        athena.pages_read += 1
                        yield {"QueryExecutionIds": page}

        return _Pages()

    def batch_get_query_execution(self, QueryExecutionIds: list[str]) -> dict[str, Any]:  # noqa: N803
        return {
            "QueryExecutions": [
                {
                    "QueryExecutionId": item,
                    "Status": {"State": "SUCCEEDED", "SubmissionDateTime": self.executions[item]},
                    "Statistics": {"DataScannedInBytes": 7},
                }
                for item in QueryExecutionIds
            ]
        }


def test_athena_executions_are_read_by_paginator_inside_the_window_only() -> None:
    athena = _Athena()
    window = (datetime(2026, 10, 15, 10, 0, tzinfo=UTC), datetime(2026, 10, 15, 10, 15, tzinfo=UTC))

    found = acceptance.athena_executions(athena, "dev", window)

    assert [item["query_execution_id"] for item in found] == ["q-in"]
    assert found[0]["workgroup"] == "adlytics-dev-v2-bake"
    assert athena.pages_read == 2  # the page holding an older execution ends the read


class _Logs:
    def __init__(self, rows: list[list[dict[str, str]]], matched: int) -> None:
        self.rows, self.matched = rows, matched

    def start_query(self, **kwargs: Any) -> dict[str, str]:
        assert "task_summary" in kwargs["queryString"]
        return {"queryId": "q"}

    def get_query_results(self, queryId: str) -> dict[str, Any]:  # noqa: N803
        assert queryId == "q"
        return {
            "status": "Complete",
            "results": self.rows,
            "statistics": {"recordsMatched": self.matched},
        }


def test_task_summaries_keep_only_the_named_runs_and_refuse_a_cut_read() -> None:
    def row(run: str) -> list[dict[str, str]]:
        line = {"phase": "task_summary", "run_id": run, "page": "p", "variant": "1d"}
        return [{"field": "@message", "value": json.dumps(line)}]

    window = (datetime(2026, 10, 15, 10, 0, tzinfo=UTC), datetime(2026, 10, 15, 11, 0, tzinfo=UTC))
    logs = _Logs([row("run-a"), row("run-x"), [{"field": "@message", "value": "not json"}]], 3)
    found = acceptance.task_summaries(logs, "group", window, ["run-a"])
    assert [item["run_id"] for item in found] == ["run-a"]
    with pytest.raises(gate.ProbeError, match="completeness unproven"):
        acceptance.task_summaries(_Logs([row("run-a")], 2), "group", window, ["run-a"])


# --- Athena admission ------------------------------------------------------------------------


def _function(reserved: int | None = None, value: object = None) -> dict[str, object]:
    properties: dict[str, object] = {}
    if reserved is not None:
        properties["ReservedConcurrentExecutions"] = reserved
    if value is not None:
        properties["Environment"] = {"Variables": {acceptance.GENERAL_BATCH_VARIABLE: str(value)}}
    return {"Type": "AWS::Lambda::Function", "Properties": properties}


def _templates(general: int = 8) -> dict[str, dict[str, Any]]:
    return {
        "Adlytics-Api-dev": {"Resources": {"ChatFn": _function(value=general)}},
        "Adlytics-Agent-dev": {"Resources": {"AgentFn": _function(value=general)}},
        "Adlytics-Ingestion-dev": {
            "Resources": {
                "IngestionFunctionFixture": _function(2, general),
                "Stage1TableWriterFixture": _function(8, general),
                "Stage1ProviderAttemptReconcilerFixture": _function(1, general),
            }
        },
        "Adlytics-Publisher-dev": {"Resources": {"BakerFixture": _function(2, 10)}},
        "Adlytics-InsightPrecompute-dev": {"Resources": {"Worker": _function(2, 4)}},
    }


def _receipt(**overrides: Any) -> dict[str, object]:
    values: dict[str, Any] = {
        "env_text": "OTHER=1\nADLYTICS_ATHENA_QUERY_BATCH_MAX_CONCURRENCY=8\n",
        "synthesized": _templates(),
        "deployed": _templates(),
        "readback": acceptance.general_values(_templates()),
        "operators": 1,
        "code_sha": _CODE_SHA,
        "environment": "dev",
        "acceptance_execution_id": _EXECUTION,
    }
    values.update(overrides)
    return acceptance.admission_receipt(**values)


def test_the_static_rows_are_derived_from_the_templates() -> None:
    assert acceptance.static_rows(_templates(), 1) == gate.ATHENA_STATIC_CALLER_ROWS
    assert acceptance.operator_processes(_ROOT) == 1


def test_explicit_8_everywhere_passes_and_only_consuming_stacks_are_read() -> None:
    receipt = _receipt()
    assert set(cast("dict[str, object]", receipt["synthesized"])) == {
        "Adlytics-Agent-dev/AgentFn",
        "Adlytics-Api-dev/ChatFn",
        "Adlytics-Ingestion-dev/IngestionFunctionFixture",
        "Adlytics-Ingestion-dev/Stage1ProviderAttemptReconcilerFixture",
        "Adlytics-Ingestion-dev/Stage1TableWriterFixture",
    }
    assert gate.athena_admission_config_verdict(receipt)["passed"] is True


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"env_text": "OTHER=1\n"}, "absent"),
        ({"readback": {"Adlytics-Api-dev/ChatFn": 7}}, "deployed_readback_mismatch"),
        ({"deployed": _templates(general=6)}, "deployed_readback_mismatch"),
        (
            {"synthesized": {**_templates(), "Adlytics-Publisher-dev": {"Resources": {}}}},
            "static_rows_or_reserve_changed",
        ),
        (
            {
                "env_text": f"{acceptance.ENV_VARIABLE}=6\n",
                "synthesized": _templates(6),
                "deployed": _templates(6),
                "readback": acceptance.general_values(_templates(6)),
            },
            "unproven_non_8",
        ),
    ],
)
def test_every_admission_mismatch_is_not_proven(overrides: dict[str, Any], reason: str) -> None:
    verdict = gate.athena_admission_config_verdict(_receipt(**overrides))
    assert verdict["passed"] is False
    assert reason in cast("list[str]", verdict["reasons"])


def test_the_env_file_value_is_explicit_or_absent() -> None:
    assert acceptance.env_file_value(f"{acceptance.ENV_VARIABLE}=8") == ("explicit_env", 8)
    assert acceptance.env_file_value("") == ("absent", None)
    assert acceptance.env_file_value(f"{acceptance.ENV_VARIABLE}=0x8") == ("explicit_env", None)
    twice = f"{acceptance.ENV_VARIABLE}=8\n{acceptance.ENV_VARIABLE}=8"
    assert acceptance.env_file_value(twice) == ("absent", None)


# --- Hold-clear matrix and the whole mode ----------------------------------------------------


def _write(path: pathlib.Path, document: dict[str, object]) -> pathlib.Path:
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _sources(
    directory: pathlib.Path, *, failed: frozenset[str] = frozenset()
) -> dict[str, pathlib.Path]:
    matrix = acceptance.load_hold_clear_matrix()
    sources: dict[str, pathlib.Path] = {}
    for row in cast("list[dict[str, object]]", matrix["rows"]):
        criterion = str(row["criterion_id"])
        if row["status"] == "retired" or row["command"] in acceptance.MATRIX_COMMAND_RECEIPTS:
            continue
        sources[criterion] = _write(
            directory / f"{criterion}.json", {"passed": criterion not in failed}
        )
    return sources


def _pairs(sources: dict[str, pathlib.Path]) -> list[str]:
    return [f"{criterion}={path}" for criterion, path in sources.items()]


def _manifest_entries(
    directory: pathlib.Path, execution: str, sources: dict[str, pathlib.Path]
) -> dict[str, Any]:
    _write(
        directory / acceptance.MATRIX_MANIFEST,
        acceptance.matrix_manifest(execution, _pairs(sources), ()),
    )
    entries, status = acceptance.load_matrix_manifest(directory, execution)
    assert status == "bound"
    return entries


def test_matrix_entries_bind_by_document_or_by_the_run_manifest(tmp_path: pathlib.Path) -> None:
    matrix = acceptance.load_hold_clear_matrix()
    _write(
        tmp_path / "observed-cohort-verdict.json",
        {"acceptance_execution_id": "other", "passed": True},
    )
    inside = _write(tmp_path / "HIST-6.json", {"passed": True})
    outside = tmp_path.parent / f"{tmp_path.name}-elsewhere.json"
    _write(outside, {"passed": True})
    sources = {"HIST-5": outside, "HIST-6": inside}
    manifest = _manifest_entries(tmp_path, _EXECUTION, sources)
    entries, _ = acceptance.matrix_evidence(matrix, tmp_path, _EXECUTION, sources, manifest)
    assert entries["HIST-1"]["execution_id"] == "other"
    assert entries["HIST-5"]["execution_id"] == "unbound"  # recorded, but outside the execution
    assert entries["HIST-6"]["execution_id"] == _EXECUTION
    assert entries["HIST-6"]["binding"] == "manifest"
    verdict = gate.hold_clear_matrix_verdict(matrix, entries, execution_id=_EXECUTION)
    assert "evidence_other_execution:HIST-1" in cast("list[str]", verdict["reasons"])
    assert "evidence_other_execution:HIST-5" in cast("list[str]", verdict["reasons"])
    assert "evidence_other_execution:HIST-6" not in cast("list[str]", verdict["reasons"])
    assert "evidence_absent:HIST-4" in cast("list[str]", verdict["reasons"])
    with pytest.raises(gate.ProbeError):
        acceptance.parse_matrix_sources(["HIST-5=a", "HIST-5=b"])


def test_the_hist17_row_names_its_out_of_scope_alarms_with_their_states(
    tmp_path: pathlib.Path,
) -> None:
    matrix = acceptance.load_hold_clear_matrix()
    scoped = {"alarm_window": [{"alarm": "adlytics-test-evaluation", "state": "ALARM"}]}
    source = _write(
        tmp_path / "alarm-window-verdict.json",
        {"acceptance_execution_id": _EXECUTION, "passed": True, "out_of_graph_scope": scoped},
    )
    plain = _write(
        tmp_path / "HIST-6.json", {"acceptance_execution_id": _EXECUTION, "passed": True}
    )

    entries, _ = acceptance.matrix_evidence(
        matrix, tmp_path, _EXECUTION, {"HIST-17": source, "HIST-6": plain}, {}
    )
    verdict = gate.hold_clear_matrix_verdict(matrix, entries, execution_id=_EXECUTION)

    assert entries["HIST-17"]["out_of_graph_scope"] == scoped
    assert "out_of_graph_scope" not in entries["HIST-6"]
    assert verdict["out_of_graph_scope"] == {"HIST-17": scoped}
    # Listing them never decides the row: HIST-17 is not among the failures.
    assert "evidence_failed:HIST-17" not in cast("list[str]", verdict["reasons"])


def test_location_alone_never_binds_a_matrix_document(tmp_path: pathlib.Path) -> None:
    """Negative controls: an unmanifested, re-written, or other-execution manifest is unbound."""
    matrix = acceptance.load_hold_clear_matrix()
    source = _write(tmp_path / "HIST-6.json", {"passed": True})
    sources = {"HIST-6": source}

    unmanifested, _ = acceptance.matrix_evidence(matrix, tmp_path, _EXECUTION, sources, {})
    assert unmanifested["HIST-6"]["execution_id"] == "unbound"

    manifest = _manifest_entries(tmp_path, _EXECUTION, sources)
    _write(source, {"passed": True, "copied": "from another execution"})  # digest now differs
    rewritten, _ = acceptance.matrix_evidence(matrix, tmp_path, _EXECUTION, sources, manifest)
    assert rewritten["HIST-6"]["execution_id"] == "unbound"

    other = tmp_path / "other"
    other.mkdir()
    _write(other / "HIST-6.json", {"passed": True})
    _write(
        other / acceptance.MATRIX_MANIFEST,
        acceptance.matrix_manifest(_EARLIER, _pairs({"HIST-6": other / "HIST-6.json"}), ()),
    )
    assert acceptance.load_matrix_manifest(other, _EXECUTION) == ({}, "other_execution")
    assert acceptance.load_matrix_manifest(tmp_path / "absent", _EXECUTION) == ({}, "absent")
    named = acceptance.matrix_manifest(_EXECUTION, [f"HIST-6={source}"], ["HIST-6"])
    assert cast("dict[str, Any]", named["entries"])["HIST-6"]["origin"] == "operator"


_QUERY = "48f1b741-cef2-4abd-9223-fbf1ca06ec0a"
_SELECTION_KEYS = [f"campaign:{index:02x}" for index in range(30)]
_ACCEPTED = "a" * 64
# pragma: allowlist nextline secret (A-ENTROPY-IDENTIFIER-001)
_WINDOW = "amc/dsp_impressions/marketplace=ATVPDKIKX0DER/window=2026-10-01_2026-10-03/options=x/"
_TARGET = latency.window_unit(_WINDOW)
_PLANNED_KEY = (
    "amc/dsp_impressions/window=2026-09-19_2026-09-21/retrieved=20261006T092520Z/manifest.json"
)


def _selection(**changes: object) -> dict[str, object]:
    """Return this execution's governed outside-list selection receipt."""
    receipt = latency.selection_receipt(
        execution_id=_EXECUTION,
        environment="dev",
        generation=_ACCEPTED,
        observed=latency.SelectionObservation(
            query_execution_id=_QUERY,
            query_completed_at="2026-10-15T09:05:00+00:00",
            generation_keys=tuple(_SELECTION_KEYS),
            browse_current_on="2026-10-15",
            catalog_keys=tuple(_SELECTION_KEYS),
            catalog_truncated=False,
            target_unit=str(changes.pop("target_unit", _TARGET)),
            target_window=str(changes.pop("target_window", _WINDOW)),
            target_generation=_ACCEPTED,
            read_at="2026-10-15T09:05:05+00:00",
        ),
    )
    receipt.update(changes)
    return receipt


def _selection_bytes(**changes: object) -> bytes:
    return json.dumps(_selection(**changes), indent=1, sort_keys=True).encode()


def _prompt_digest(item: str) -> str:
    manifest = latency.load_chat_manifest(acceptance.CHAT_PROMPT_MANIFEST)
    return latency.filled_prompt_sha256(latency.with_outside_list_item(manifest, item))


def _query_readback(query: str = _QUERY, **changes: object) -> dict[str, object]:
    """Return the selection query as Athena re-reads it: the receipt's exact facts."""
    return {
        "query_execution_id": query,
        "state": "SUCCEEDED",
        "workgroup": "adlytics-dev-v2-interactive",
        "query_sha256": latency.selection_sql("dev", _WINDOW)[1],
        "completed_at": "2026-10-15T09:05:00+00:00",
        "keys": list(reversed(_SELECTION_KEYS)),
        **changes,
    }


def _chat(phase: str, start: datetime) -> dict[str, object]:
    rows: list[dict[str, object]] = []
    begin = start
    for prompt in ("p1", "p2", "p3", "p4", "p5", "outside_bounded_campaign"):
        row: dict[str, object] = {
            "prompt_id": prompt,
            "request_started_at": begin.isoformat(),
            "request_ended_at": (begin + timedelta(milliseconds=1000)).isoformat(),
            "latency_ms": 1000,
            "terminal_class": "answer",
            "governed_answer": True,
            "governed_generation": _ACCEPTED,
            "http_status": 200,
            "auth_outcome": "authenticated",
            "quota_refused": False,
            "throttled": False,
            "governed_tool_calls": ["measure"] if prompt == "outside_bounded_campaign" else [],
        }
        if phase == "during_bake":
            row["head_active_receipt"] = {"state": "ACTIVE", "cohort_revision": "c-a"}
        rows.append(row)
        begin += timedelta(seconds=10)
    return {
        "schema_version": "chat-latency-sample-v1",
        "phase": phase,
        "acceptance_execution_id": _EXECUTION,
        "environment": "dev",
        "account": "000000000000",
        "origin": "https://app.invalid",
        "persona_id": "probe-a",
        "prompt_manifest_path": "validation/fixtures/chat-bake-acceptance-v2.json",
        "prompt_manifest_sha256": "f" * 64,
        "sample_count": 6,
        "accepted_generation": _ACCEPTED,
        "sample_started_at": start.isoformat(),
        "sample_ended_at": rows[-1]["request_ended_at"],
        "preflight_completed_at": "2026-10-15T09:00:00+00:00",
        "first_root_launch_at": "2026-10-15T10:00:00+00:00",
        "target_bake_run_id": "run-a",
        "target_cohort_revision": "c-a",
        "bake_run_started_at": _BAKE_START.isoformat(),
        "bake_finalize_ended_at": _BAKE_END.isoformat(),
        "outside_list_item": "campaign:1d",
        "outside_selection_sha256": hashlib.sha256(_selection_bytes()).hexdigest(),
        "outside_selection_query_execution_id": _QUERY,
        "generation_unit": _TARGET,
        "prompt_filled_sha256": _prompt_digest("campaign:1d"),
        "outside_list_absent_from_bounded_list": True,
        "outside_list_present_in_generation": True,
        "rows": rows,
    }


def _execution(
    directory: pathlib.Path, *, failed: frozenset[str] = frozenset()
) -> acceptance.VerdictInputs:
    directory.mkdir(parents=True)
    sources = _sources(directory, failed=failed)
    _manifest_entries(directory, _EXECUTION, sources)
    _write(
        directory / acceptance.CONTROL_RECEIPT,
        {
            "acceptance_execution_id": _EXECUTION,
            "observations": {"a_snapshot_before": {"winner_run_id": "run-a"}},
            "planned": [{"roster": "wave_a", "label": "a-1", "key": _PLANNED_KEY}],
        },
    )
    (directory / acceptance.OUTSIDE_SELECTION).write_bytes(_selection_bytes())
    _write(
        directory / acceptance.CHAT_BASELINE,
        _chat("paired_no_bake", datetime(2026, 10, 15, 9, 10, tzinfo=UTC)),
    )
    _write(
        directory / acceptance.CHAT_DURING,
        _chat("during_bake", datetime(2026, 10, 15, 10, 15, tzinfo=UTC)),
    )
    _write(
        directory / "observed-cohort-verdict.json",
        {"acceptance_execution_id": _EXECUTION, "passed": True},
    )
    return acceptance.VerdictInputs(
        execution_id=_EXECUTION,
        evidence_dir=directory,
        accepted_generation=_ACCEPTED,
        env_file=directory / ".env",
        cdk_out=directory,
        worktree=_ROOT,
        matrix_sources=sources,
        code_sha=_CODE_SHA,
        environment="dev",
    )


def test_the_mode_writes_one_passing_receipt_per_verdict(tmp_path: pathlib.Path) -> None:
    inputs = _execution(tmp_path / "exec-1")
    collected: list[str] = []

    def trace(control: object) -> dict[str, object]:
        assert isinstance(control, dict)
        collected.append("trace")
        return _artifacts()

    def admission() -> dict[str, object]:
        collected.append("admission")
        return _receipt()

    written = acceptance.run_acceptance_verdicts(
        inputs,
        collect_trace=trace,
        collect_admission=admission,
        collect_selection=_query_readback,
        evidence_root=tmp_path,
    )

    assert set(written) == set(acceptance.RECEIPTS)
    for name, path in written.items():
        document = json.loads(path.read_text())
        assert document["acceptance_execution_id"] == _EXECUTION
        assert document["passed"] is True, (name, document["result"])
        assert all(len(item["sha256"]) == 64 for item in document["inputs"].values())
    assert collected == ["trace", "admission"]
    assert (inputs.evidence_dir / acceptance.TRACE_ARTIFACTS).is_file()
    # Receipts are written once; a second run is refused rather than overwriting evidence.
    with pytest.raises(gate.ProbeError, match="refusing to overwrite"):
        acceptance.run_acceptance_verdicts(
            inputs,
            collect_trace=trace,
            collect_admission=admission,
            collect_selection=_query_readback,
            evidence_root=tmp_path,
        )


def test_a_reused_execution_id_and_a_failed_row_are_not_proven(tmp_path: pathlib.Path) -> None:
    earlier = tmp_path / "earlier"
    earlier.mkdir()
    _write(
        earlier / acceptance.RECEIPTS["paired_bake_chat_verdict"],
        {"acceptance_execution_id": _EXECUTION, "passed": True},
    )
    inputs = _execution(tmp_path / "exec-1", failed=frozenset({"HIST-5"}))

    written = acceptance.run_acceptance_verdicts(
        inputs,
        collect_trace=lambda _control: _artifacts(),
        collect_admission=_receipt,
        collect_selection=_query_readback,
        evidence_root=tmp_path,
    )

    chat = json.loads(written["paired_bake_chat_verdict"].read_text())
    assert chat["passed"] is False
    assert "execution_id_reused" in chat["result"]["reasons"]
    matrix = json.loads(written["hold_clear_matrix_verdict"].read_text())
    assert "evidence_failed:HIST-5" in matrix["result"]["reasons"]


def test_a_control_for_another_execution_is_refused(tmp_path: pathlib.Path) -> None:
    inputs = _execution(tmp_path / "exec-1")
    _write(inputs.evidence_dir / acceptance.CONTROL_RECEIPT, {"acceptance_execution_id": "other"})
    with pytest.raises(gate.ProbeError, match="another acceptance execution"):
        acceptance.run_acceptance_verdicts(
            inputs,
            collect_trace=lambda _control: _artifacts(),
            collect_admission=_receipt,
            collect_selection=_query_readback,
            evidence_root=tmp_path,
        )


def _copied_from_earlier(tmp_path: pathlib.Path) -> acceptance.VerdictInputs:
    """Fill exec-1's directory with otherwise-passing inputs exec-0 produced."""
    inputs = _execution(tmp_path / "exec-1")
    directory = inputs.evidence_dir
    _write(directory / acceptance.TRACE_ARTIFACTS, _artifacts(acceptance_execution_id=_EARLIER))
    for name, phase, start in (
        (acceptance.CHAT_BASELINE, "paired_no_bake", datetime(2026, 10, 15, 9, 10, tzinfo=UTC)),
        (acceptance.CHAT_DURING, "during_bake", datetime(2026, 10, 15, 10, 15, tzinfo=UTC)),
    ):
        _write(directory / name, {**_chat(phase, start), "acceptance_execution_id": _EARLIER})
    _write(directory / acceptance.ATHENA_ADMISSION, _receipt(acceptance_execution_id=_EARLIER))
    _write(directory / acceptance.SELECTION_READBACK, _query_readback())
    (directory / acceptance.MATRIX_MANIFEST).unlink()  # exec-0's sources carry no manifest here
    return inputs


def test_inputs_copied_from_another_execution_are_not_proven(tmp_path: pathlib.Path) -> None:
    """Negative controls: every otherwise-passing input exec-0 produced is refused for exec-1."""
    inputs = _copied_from_earlier(tmp_path)

    def never(*_args: object) -> dict[str, object]:
        raise AssertionError("a retained input was re-collected")

    written = acceptance.run_acceptance_verdicts(
        inputs,
        collect_trace=never,
        collect_admission=never,
        collect_selection=never,
        evidence_root=tmp_path,
    )

    results = {name: json.loads(path.read_text()) for name, path in written.items()}
    assert all(item["disposition"] == "NOT_PROVEN" for item in results.values())
    trace = results["trace_completeness_verdict"]["result"]
    assert trace["positive"]["passed"] is True  # the copied evidence itself would pass ...
    assert trace["binding_failures"] == ["execution_mismatch:trace_artifacts"]  # ... if bound
    assert results["paired_bake_chat_verdict"]["result"]["binding_failures"] == [
        "execution_mismatch:trace_artifacts",
        "execution_mismatch:baseline",
        "execution_mismatch:during_bake",
    ]
    admission = results["athena_admission_config_verdict"]["result"]
    assert admission["reasons"] == []
    assert admission["binding_failures"] == ["execution_mismatch:admission_receipt"]
    matrix = results["hold_clear_matrix_verdict"]["result"]
    assert matrix["manifest_status"] == "absent"
    assert "evidence_other_execution:HIST-5" in matrix["reasons"]


def test_only_the_chat_samples_of_another_execution_are_not_proven(
    tmp_path: pathlib.Path,
) -> None:
    inputs = _execution(tmp_path / "exec-1")
    for name, phase, start in (
        (acceptance.CHAT_BASELINE, "paired_no_bake", datetime(2026, 10, 15, 9, 10, tzinfo=UTC)),
        (acceptance.CHAT_DURING, "during_bake", datetime(2026, 10, 15, 10, 15, tzinfo=UTC)),
    ):
        _write(
            inputs.evidence_dir / name,
            {**_chat(phase, start), "acceptance_execution_id": _EARLIER},
        )

    written = acceptance.run_acceptance_verdicts(
        inputs,
        collect_trace=lambda _control: _artifacts(),
        collect_admission=_receipt,
        collect_selection=_query_readback,
        evidence_root=tmp_path,
    )

    chat = json.loads(written["paired_bake_chat_verdict"].read_text())
    assert chat["result"]["reasons"] == []  # the pair agrees with itself and passes the p95 rule
    assert chat["disposition"] == "NOT_PROVEN"
    assert chat["result"]["binding_failures"] == [
        "execution_mismatch:baseline",
        "execution_mismatch:during_bake",
    ]
    assert json.loads(written["trace_completeness_verdict"].read_text())["passed"] is True


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"code_sha": "d" * 40}, "admission_code_sha_mismatch"),
        ({"environment": "prod"}, "admission_environment_mismatch"),
        (
            {"collection": {"acceptance_execution_id": _EXECUTION}},
            "admission_collection_unidentified",
        ),
        ({"schema_version": None}, "admission_schema"),
    ],
)
def test_a_retained_admission_receipt_is_bound_to_this_code_and_environment(
    tmp_path: pathlib.Path, change: dict[str, object], reason: str
) -> None:
    inputs = _execution(tmp_path / "exec-1")
    _write(inputs.evidence_dir / acceptance.ATHENA_ADMISSION, {**_receipt(), **change})

    written = acceptance.run_acceptance_verdicts(
        inputs,
        collect_trace=lambda _control: _artifacts(),
        collect_admission=_receipt,
        collect_selection=_query_readback,
        evidence_root=tmp_path,
    )

    admission = json.loads(written["athena_admission_config_verdict"].read_text())
    assert admission["disposition"] == "NOT_PROVEN"
    assert admission["result"]["binding_failures"] == [reason]


def test_the_gate_cli_requires_the_acceptance_inputs(tmp_path: pathlib.Path) -> None:
    code = gate.main(
        [
            "--acceptance-verdicts",
            "--profile",
            "fictional",
            "--env",
            "dev",
            "--region",
            "us-east-1",
            "--expected-account",
            "111122223333",
            "--evidence-dir",
            str(tmp_path),
        ]
    )
    assert code == 1


def _chat_verdict(inputs: acceptance.VerdictInputs, tmp_path: pathlib.Path) -> dict[str, Any]:
    written = acceptance.run_acceptance_verdicts(
        inputs,
        collect_trace=lambda _control: _artifacts(),
        collect_admission=_receipt,
        collect_selection=_query_readback,
        evidence_root=tmp_path,
    )
    return cast("dict[str, Any]", json.loads(written["paired_bake_chat_verdict"].read_text()))


def test_the_chat_verdict_retains_the_selection_receipt_and_derives_its_item(
    tmp_path: pathlib.Path,
) -> None:
    inputs = _execution(tmp_path / "exec-1")
    chat = _chat_verdict(inputs, tmp_path)
    assert chat["disposition"] == "PASS", chat["result"]
    retained = chat["inputs"]["outside_selection"]
    assert retained["sha256"] == hashlib.sha256(_selection_bytes()).hexdigest()


def _rewrite_samples(directory: pathlib.Path, **changes: object) -> None:
    for name in (acceptance.CHAT_BASELINE, acceptance.CHAT_DURING):
        path = directory / name
        _write(path, {**json.loads(path.read_text()), **changes})


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("absent", "outside_selection_absent"),
        ("hand-written-pair", "outside_selection_invalid"),
        ("bake-workgroup", "outside_selection_invalid"),
        ("other-sql", "outside_selection_invalid"),
        ("other-execution", "outside_selection_invalid"),
        ("item-inside-prefix", "outside_selection_invalid"),
        ("receipt-rewritten", "baseline_outside_selection_sha256_mismatch"),
        (
            "query-differs-between-phases",
            "during_bake_outside_selection_query_execution_id_mismatch",
        ),
        ("samples-name-another-item", "baseline_outside_list_item_mismatch"),
    ],
)
def test_an_unbound_or_failing_selection_receipt_is_not_proven(
    tmp_path: pathlib.Path, change: str, reason: str
) -> None:
    inputs = _execution(tmp_path / "exec-1")
    directory = inputs.evidence_dir
    receipt = directory / acceptance.OUTSIDE_SELECTION
    rewritten: dict[str, dict[str, object]] = {
        "hand-written-pair": {"items": _SELECTION_KEYS[:25], "selected_item": "campaign:1d"},
        "bake-workgroup": _selection(workgroup="adlytics-dev-v2-bake"),
        "other-sql": _selection(sql="SELECT 'campaign:1d' AS campaign_key"),
        "other-execution": _selection(acceptance_execution_id=_EARLIER),
        "item-inside-prefix": _selection(selected_item=_SELECTION_KEYS[0]),
        "receipt-rewritten": _selection(read_at="2026-10-15T09:02:00+00:00"),
    }
    if change == "absent":
        receipt.unlink()
    elif change in rewritten:
        _write(receipt, rewritten[change])
    elif change == "query-differs-between-phases":
        path = directory / acceptance.CHAT_DURING
        _write(
            path,
            {
                **json.loads(path.read_text()),
                "outside_selection_query_execution_id": "00000000-0000-4000-8000-000000000000",
            },
        )
    else:
        # The samples agree with each other on another item; only the receipt decides.
        _rewrite_samples(directory, outside_list_item=_SELECTION_KEYS[0])
    chat = _chat_verdict(inputs, tmp_path)
    assert chat["disposition"] == "NOT_PROVEN"
    assert any(item.startswith(reason) for item in chat["result"]["binding_failures"]), chat[
        "result"
    ]


def _rebind(directory: pathlib.Path, receipt: dict[str, object], **sample: object) -> None:
    """Replace the receipt and re-bind both samples to its digest, so only the change counts."""
    raw = json.dumps(receipt, indent=1, sort_keys=True).encode()
    (directory / acceptance.OUTSIDE_SELECTION).write_bytes(raw)
    _rewrite_samples(
        directory,
        outside_selection_sha256=hashlib.sha256(raw).hexdigest(),
        generation_unit=receipt["target_unit"],
        **sample,
    )


def _planned_unit() -> str:
    from domain.operations.ingestion_root import window_identity  # noqa: PLC0415

    return window_identity(_PLANNED_KEY).conflict_unit


def test_the_rebinding_helper_is_itself_neutral(tmp_path: pathlib.Path) -> None:
    inputs = _execution(tmp_path / "exec-1")
    _rebind(inputs.evidence_dir, _selection())
    assert _chat_verdict(inputs, tmp_path)["disposition"] == "PASS"


def _change_control(directory: pathlib.Path, **changes: object) -> None:
    path = directory / acceptance.CONTROL_RECEIPT
    document = {**json.loads(path.read_text()), **changes}
    _write(path, {key: value for key, value in document.items() if value is not None})


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("target-unit-planned", "target_unit_planned_by_control"),
        ("control-plans-nothing", "control_planned_keys_absent"),
        ("sample-used-another-unit", "during_bake_generation_unit_mismatch"),
        ("receipt-generation-not-accepted", "outside_selection_invalid"),
        ("sample-prompt-differs", "baseline_prompt_filled_sha256_mismatch"),
        ("fabricated-query", "readback_unreadable"),
        ("stale-before-preflight", "readback_outside_execution"),
        ("other-workgroup", "readback_workgroup_mismatch"),
        ("other-results", "readback_keys_mismatch"),
        ("failed-query", "readback_not_succeeded"),
    ],
)
def test_the_target_unit_and_the_query_readback_bind_the_chat_verdict(
    tmp_path: pathlib.Path, change: str, reason: str
) -> None:
    inputs = _execution(tmp_path / "exec-1")
    directory = inputs.evidence_dir
    overrides: dict[str, dict[str, object]] = {
        "stale-before-preflight": {"completed_at": "2026-10-15T08:59:00+00:00"},
        "other-workgroup": {"workgroup": "adlytics-dev-v2-bake"},
        "other-results": {"keys": _SELECTION_KEYS[:29]},
        "failed-query": {"state": "FAILED", "keys": []},
    }

    def readback(query: str) -> dict[str, object]:
        if change == "fabricated-query":
            raise LookupError(f"no such query {query}")
        return _query_readback(query, **overrides.get(change, {}))

    if change == "target-unit-planned":
        planned_window = _PLANNED_KEY.split("retrieved=", 1)[0]
        _rebind(directory, _selection(target_unit=_planned_unit(), target_window=planned_window))
    elif change == "control-plans-nothing":
        _change_control(directory, planned=None)
    elif change == "sample-used-another-unit":
        path = directory / acceptance.CHAT_DURING
        _write(path, {**json.loads(path.read_text()), "generation_unit": "c" * 64})
    elif change == "receipt-generation-not-accepted":
        inputs = dataclasses.replace(inputs, accepted_generation="c" * 64)
    elif change == "sample-prompt-differs":
        path = directory / acceptance.CHAT_BASELINE
        _write(path, {**json.loads(path.read_text()), "prompt_filled_sha256": "0" * 64})
    elif change == "stale-before-preflight":
        _rebind(directory, _selection(query_completed_at="2026-10-15T08:59:00+00:00"))
    written = acceptance.run_acceptance_verdicts(
        inputs,
        collect_trace=lambda _control: _artifacts(),
        collect_admission=_receipt,
        collect_selection=readback,
        evidence_root=tmp_path,
    )
    chat = json.loads(written["paired_bake_chat_verdict"].read_text())
    assert chat["disposition"] == "NOT_PROVEN"
    failures = chat["result"]["binding_failures"]
    assert any(item.startswith(reason) for item in failures), failures


def test_a_receipt_field_of_the_wrong_type_is_not_proven_never_a_crash(
    tmp_path: pathlib.Path,
) -> None:
    inputs = _execution(tmp_path / "exec-1")
    _rebind(inputs.evidence_dir, {**_selection(), "generation_keys": [["campaign:00"]]})
    chat = _chat_verdict(inputs, tmp_path)
    assert chat["disposition"] == "NOT_PROVEN"
    assert any(
        item.startswith("outside_selection_invalid") for item in chat["result"]["binding_failures"]
    )


def test_a_probe_shaped_pair_passes_without_the_d2_absence_leg(tmp_path: pathlib.Path) -> None:
    inputs = _execution(tmp_path / "exec-1")
    _rewrite_samples(
        inputs.evidence_dir,
        outside_list_absent_from_bounded_list=None,
        outside_list_absence_status="MOVED_TO_D2",
    )
    assert _chat_verdict(inputs, tmp_path)["disposition"] == "PASS"
    other = _execution(tmp_path / "again" / "exec-1")
    _rewrite_samples(other.evidence_dir, outside_list_present_in_generation=False)
    refused = _chat_verdict(other, tmp_path / "again")
    assert refused["disposition"] == "NOT_PROVEN"
    assert "outside_list_unproven" in refused["result"]["reasons"]
