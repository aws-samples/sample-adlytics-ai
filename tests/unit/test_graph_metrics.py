# SPDX-License-Identifier: MIT-0
"""The V3 bake's phase telemetry (plan 4.7) and its trace contract (Addendum A2).

Spans are recorded through a fake `span` patched into each owning module, so the exact attribute
set every span opens and records is compared with `contracts/observability/spans.yaml`. Every
check sits beside a paired control that fails: a dropped span, an identifier as a metric
dimension, a missing correlation id, and a telemetry failure that must not change the effect.
Every identity is fictional.
"""

from __future__ import annotations

from collections.abc import Generator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from graph_bake_v3_support import Bake, roster

from adapters.jobs.preset_artifact_publisher import graph_bake
from adapters.jobs.preset_artifact_publisher.graph_bake import query_rollup, task_node_name
from adapters.jobs.trace_contract.contract import load_trace_contract
from adapters.outbound import preset_artifacts as artifacts_module
from adapters.outbound.athena import query as athena_query
from adapters.outbound.athena.execution import StartedStatement, StatementOutcome
from adapters.outbound.athena.query import (
    INTERACTIVE_WAIT,
    AthenaQueryAdapter,
    AthenaQueryTarget,
    ResultTransport,
)
from adapters.outbound.telemetry import graph_metrics
from adapters.outbound.telemetry.graph_metrics import (
    BakeTaskReading,
    bake_task_datapoints,
    emit_bake_trace_signal,
    emit_serving_withheld,
)
from adapters.outbound.telemetry.span_context import (
    QueryStatistics,
    admitted,
    collecting,
    correlated,
)
from domain.operations.execution_graph_dispatch import NodeState
from domain.operations.execution_graph_release import BakeEffectKind
from domain.shared_kernel.errors import DependencyError, ErrorCode
from domain.shared_kernel.provenance import Suppression


class _NoWithholding:
    """Stands in for the adapter's withholding accumulator over a stub plan."""

    def __init__(self, _plan: object) -> None:
        return None

    def add(self, _row: object) -> None:
        return None

    def result(self) -> Suppression:
        return Suppression(rows_suppressed=False)


pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-075")]

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = {span.name: span for span in load_trace_contract(ROOT).spans}
BAKE_SPANS = (
    "bake.run",
    "bake.source_pin",
    "bake.fan_out",
    "bake.task",
    "bake.phase",
    "bake.finalize",
)


@dataclass
class Spans:
    """A `span` stand-in recording each opened span's name and every attribute it carried."""

    opened: list[tuple[str, dict[str, object]]] = field(
        default_factory=list[tuple[str, dict[str, object]]]
    )

    @contextmanager
    def __call__(self, name: str, **attributes: object) -> Generator[Any]:
        carried = dict(attributes)
        self.opened.append((name, carried))

        def record(**more: object) -> None:
            carried.update(more)

        yield SimpleNamespace(record=record)

    def named(self, name: str) -> list[dict[str, object]]:
        return [carried for opened, carried in self.opened if opened == name]


def _traced(monkeypatch: pytest.MonkeyPatch) -> Spans:
    spans = Spans()
    for module in (graph_bake, athena_query, artifacts_module):
        monkeypatch.setattr(module, "span", spans)
    return spans


def _served(bake: Bake) -> str:
    bake.seed_predecessors()
    run = bake.granted()
    bake.fan_out(run)
    assert {result.node_state for result in bake.all_tasks(run)} == {NodeState.SUCCEEDED}
    assert bake.finalize(run).node_state is NodeState.SUCCEEDED
    return run


def require_trace_complete(spans: Spans, run: str, keys: tuple[str, ...]) -> None:
    """A2.4: one `bake.task` per roster task, each span carrying this run's correlation ids."""
    tasks = spans.named("bake.task")
    missing = sorted(set(keys) - {f"{item['page']}#{item['variant']}" for item in tasks})
    assert not missing, f"roster tasks without a bake.task span: {missing}"
    for name, carried in spans.opened:
        if name in BAKE_SPANS:
            assert carried.get("run_id") == run, f"{name} without run_id"
            assert carried.get("task_id"), f"{name} without task_id"
            assert carried.get("cohort_revision"), f"{name} without cohort_revision"


def require_task_phases_complete(spans: Spans) -> None:
    """Require every recorded task to carry the contract-closed phase set."""
    by_task: dict[object, set[object]] = {}
    for carried in spans.named("bake.phase"):
        by_task.setdefault(carried["task_id"], set()).add(carried["phase"])
    for task in spans.named("bake.task"):
        assert by_task.get(task["task_id"], set()) == set(graph_bake.TASK_PHASES), (
            f"{task['task_id']} has incomplete bake phases"
        )


# --- A2.1: the span contract ------------------------------------------------------------------


def test_every_bake_span_records_within_its_declared_set_and_opens_its_required_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spans = _traced(monkeypatch)
    bake = Bake(roster=roster(3))
    run = _served(bake)

    counts = {name: len(spans.named(name)) for name in BAKE_SPANS}
    assert counts == {
        "bake.run": 1,
        "bake.source_pin": 1,
        "bake.fan_out": 1,
        "bake.task": 3,
        "bake.phase": 12,  # plan, query, render and write for each written task
        "bake.finalize": 2,  # plan 5.3: the first delivery arms precompute, the second finalizes
    }
    assert {item["phase"] for item in spans.named("bake.phase")} == set(graph_bake.TASK_PHASES)
    require_task_phases_complete(spans)
    for name, carried in spans.opened:
        declared = CONTRACT[name]
        assert set(carried) <= set(declared.allowed_attributes), name
        assert set(declared.required_attributes) <= set(carried), name
    require_trace_complete(spans, run, bake.roster.keys)
    (task,) = [item for item in spans.named("bake.task") if item["variant"] == "1d"]
    assert (task["effect_kind"], task["outcome"], task["query_count"], task["pairs"]) == (
        "written",
        "graph_bake_task_written",
        2,
        1,
    )
    assert task["planned_query_count"] == 2 and task["budget_seconds"] == 240


def test_the_declared_bake_span_sets_are_exact() -> None:
    assert set(CONTRACT["bake.task"].required_attributes) == {
        "run_id",
        "task_id",
        "cohort_revision",
        "page",
        "variant",
        "lane",
        "attempt",
        "planned_query_count",
        "budget_seconds",
    }
    assert {"queue_millis", "engine_millis", "service_processing_millis"} <= set(
        CONTRACT["athena.query"].allowed_attributes
    )
    for name in (*BAKE_SPANS, "artifact.write"):
        assert name in CONTRACT
    for name in BAKE_SPANS:
        assert {"run_id", "task_id", "cohort_revision"} <= set(
            CONTRACT[name].required_attributes
        ), name


def test_control_a_dropped_task_span_fails_the_completeness_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spans = _traced(monkeypatch)
    bake = Bake(roster=roster(2))
    run = _served(bake)
    require_trace_complete(spans, run, bake.roster.keys)  # complete as recorded

    dropped = Spans([item for item in spans.opened if item[1].get("variant") != "2d"])
    with pytest.raises(AssertionError, match=r"without a bake.task span: \['campaigns#2d'\]"):
        require_trace_complete(dropped, run, bake.roster.keys)
    uncorrelated = Spans([(name, {**carried, "run_id": None}) for name, carried in spans.opened])
    with pytest.raises(AssertionError, match="without run_id"):
        require_trace_complete(uncorrelated, run, bake.roster.keys)

    missing_render = Spans(
        [
            item
            for item in spans.opened
            if not (item[0] == "bake.phase" and item[1].get("phase") == "render")
        ]
    )
    with pytest.raises(AssertionError, match="incomplete bake phases"):
        require_task_phases_complete(missing_render)


# --- A2.1/A2.2: athena.query and artifact.write -----------------------------------------------


def _nothing(*_arguments: object) -> None:
    return None


def _adapter(monkeypatch: pytest.MonkeyPatch, outcome: StatementOutcome | Exception) -> Any:
    def composed(*_arguments: object) -> object:
        return SimpleNamespace(sql="s", parameters=())

    def started(*_arguments: object, **_keywords: object) -> StartedStatement:
        return StartedStatement("execution-1", 0)

    def fetched(*_arguments: object) -> Iterator[list[str | None]]:
        return iter([])

    monkeypatch.setattr(athena_query, "_refuse_withheld_only", _nothing)
    monkeypatch.setattr(athena_query, "_refuse_unreadable_suppression", _nothing)
    monkeypatch.setattr(athena_query, "compose", composed)
    monkeypatch.setattr(athena_query, "start_counted", started)
    monkeypatch.setattr(athena_query, "iter_pages", fetched)
    monkeypatch.setattr(athena_query, "_SuppressionAccumulator", _NoWithholding)

    def completion(
        _client: object, _execution: str, *, limit: float, wait_policy: str | None
    ) -> StatementOutcome:
        del limit, wait_policy
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(athena_query, "await_completion", completion)
    return AthenaQueryAdapter(
        AthenaQueryTarget(
            "fictional-bake", "fictional-current", 0, INTERACTIVE_WAIT, ResultTransport.PAGED
        ),
        cast("Any", object()),
    )


def _request() -> Any:
    logical = SimpleNamespace(
        metric=SimpleNamespace(name="spend"),
        facts_read=("ads",),
        selected_columns=(),
        result_aliases=(),
    )
    return SimpleNamespace(plan=SimpleNamespace(logical=logical), max_rows=10, ranked=None)


def test_an_execution_under_a_task_records_statistics_admission_wait_and_correlation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spans = _traced(monkeypatch)
    stats = StatementOutcome(4096, False, 31, 7, 5, 47, service_processing_millis=4)
    adapter = _adapter(monkeypatch, stats)

    with correlated("run-1", "task-1", "cohort-1"), collecting() as collected, admitted(250):
        adapter.run(_request())

    (inside,) = spans.named("athena.query")
    assert {
        key: inside[key]
        for key in ("run_id", "task_id", "cohort_revision", "admission_wait_millis")
    } == {
        "run_id": "run-1",
        "task_id": "task-1",
        "cohort_revision": "cohort-1",
        "admission_wait_millis": 250,
    }
    assert (inside["state"], inside["queue_millis"], inside["engine_millis"]) == (
        "SUCCEEDED",
        5,
        31,
    )
    assert inside["service_processing_millis"] == 4 and inside["statistics_unavailable"] is None
    assert inside["workgroup"] == "fictional-bake" and inside["statement_kind"] == "measurement"
    assert collected == [QueryStatistics("SUCCEEDED", 5, 7, 31, 4, 47, 4096, 250, True)]
    for _name, carried in spans.opened:
        assert set(carried) <= set(CONTRACT["athena.query"].allowed_attributes)


def test_a_serve_time_query_without_cohort_correlation_runs_and_records_no_cohort_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spans = _traced(monkeypatch)
    stats = StatementOutcome(4096, False, 31, 7, 5, 47, service_processing_millis=4)
    adapter = _adapter(monkeypatch, stats)

    adapter.run(_request())

    (served,) = spans.named("athena.query")
    assert served["state"] == "SUCCEEDED"
    assert {key: served.get(key) for key in ("run_id", "task_id", "cohort_revision")} == {
        "run_id": None,
        "task_id": None,
        "cohort_revision": None,
    }
    assert {"run_id", "task_id", "cohort_revision"}.isdisjoint(
        CONTRACT["athena.query"].required_attributes
    )


def test_a_failed_execution_keeps_its_span_with_the_terminal_state_and_no_statistics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spans = _traced(monkeypatch)
    failure = DependencyError(
        ErrorCode("ATHENA", 1), "failed", context={"state": "FAILED", "query_id": "x"}
    )
    adapter = _adapter(monkeypatch, failure)

    with (
        correlated("run-1", "task-1", "cohort-1"),
        collecting() as collected,
        pytest.raises(DependencyError),
    ):
        adapter.run(_request())

    (failed,) = spans.named("athena.query")
    assert (failed["state"], failed["statistics_unavailable"], failed["query_id"]) == (
        "FAILED",
        "execution_not_succeeded",
        "execution-1",
    )
    assert [item.statistics_available for item in collected] == [False]


def test_a_stage_call_traces_each_body_custody_chain_with_its_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from preset_artifact_support import template_of  # noqa: PLC0415
    from test_preset_artifacts import (  # noqa: PLC0415 -- the body and recording-store fixtures
        RecordingBodies,
        RecordingPointers,
        _body,  # pyright: ignore[reportPrivateUsage]
        _pointer,  # pyright: ignore[reportPrivateUsage]
    )

    from domain.presentation.preset_artifacts import (  # noqa: PLC0415
        PresetArtifactPointerAdvanceRequest,
        PresetArtifactPublicationRequest,
    )

    spans = _traced(monkeypatch)
    body = _body()
    request = PresetArtifactPublicationRequest(
        body, template_of(PresetArtifactPointerAdvanceRequest(_pointer(body)))
    )
    events: list[str] = []
    store = artifacts_module.PresetArtifactPersistence(
        RecordingBodies(events), RecordingPointers(events)
    )

    with correlated("run-1", "task-1", "cohort-1"):
        store.stage(request, remaining_ms=lambda: 600_000, tail_seconds=30.0)

    (chain,) = spans.named("artifact.write")
    assert chain == {
        "object_kind": "parent",
        "bytes": len(body.payload_json.encode()),
        "run_id": "run-1",
        "task_id": "task-1",
        "cohort_revision": "cohort-1",
        "outcome": "attested",
    }
    assert events == ["body"]  # stage never touches the pointer


def test_a_legacy_artifact_write_outside_a_cohort_writes_and_records_no_cohort_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from preset_artifact_support import template_of  # noqa: PLC0415
    from test_preset_artifacts import (  # noqa: PLC0415 -- the body and recording-store fixtures
        RecordingBodies,
        RecordingPointers,
        _body,  # pyright: ignore[reportPrivateUsage]
        _pointer,  # pyright: ignore[reportPrivateUsage]
    )

    from domain.presentation.preset_artifacts import (  # noqa: PLC0415
        PresetArtifactPointerAdvanceRequest,
        PresetArtifactPublicationRequest,
    )

    spans = _traced(monkeypatch)
    body = _body()
    events: list[str] = []
    store = artifacts_module.PresetArtifactPersistence(
        RecordingBodies(events), RecordingPointers(events)
    )

    store.stage(
        PresetArtifactPublicationRequest(
            body, template_of(PresetArtifactPointerAdvanceRequest(_pointer(body)))
        ),
        remaining_ms=lambda: 600_000,
        tail_seconds=30.0,
    )

    (chain,) = spans.named("artifact.write")
    assert {key: chain.get(key) for key in ("run_id", "task_id", "cohort_revision")} == {
        "run_id": None,
        "task_id": None,
        "cohort_revision": None,
    }
    assert (chain["outcome"], events) == ("attested", ["body"])


# --- A2.3: the per-task summary roll-up ---------------------------------------------------------


def test_the_query_rollup_reports_count_total_p50_and_max_and_counts_unavailable() -> None:
    def stat(
        timings: tuple[int, int, int], *, available: bool = True, throttled: int = 0
    ) -> QueryStatistics:
        queue, engine, total = timings
        return QueryStatistics(
            "SUCCEEDED", queue, 0, engine, 0, total, 10, 1, available, throttled_starts=throttled
        )

    rolled = query_rollup(
        (
            stat((5, 30, 40), throttled=2),
            stat((1, 10, 12)),
            stat((9, 50, 70), available=False, throttled=1),
        )
    )

    assert rolled == {
        "query_executions": 3,
        "statistics_unavailable": 1,
        "scanned_bytes": 30,
        "admission_wait_ms_total": 3,
        "throttled_starts": 3,
        "queue_ms_total": 15,
        "queue_ms_p50": 5,
        "queue_ms_max": 9,
        "engine_ms_total": 90,
        "engine_ms_p50": 30,
        "engine_ms_max": 50,
        "total_ms_total": 122,
        "total_ms_p50": 40,
        "total_ms_max": 70,
    }
    assert query_rollup(())["query_executions"] == 0  # control: no executions, no statistics


def test_each_task_ends_with_one_summary_line_and_one_metric_reading() -> None:
    bake = Bake(roster=roster(2))
    readings: list[BakeTaskReading] = []
    logger = MagicMock()
    bake.runtime.metrics, bake.runtime.logger = readings.append, logger
    bake.runtime.memory_peak_mb = lambda: 128.0
    _served(bake)

    summaries = [
        call.kwargs["extra"]
        for call in logger.info.call_args_list
        if call.kwargs["extra"]["phase"] == "task_summary"
    ]
    assert len(summaries) == 2
    assert {
        "phase_plan_ms",
        "phase_query_ms",
        "phase_render_ms",
        "phase_write_ms",
        "query_executions",
        "throttled_starts",
        "deadline_clamped",
    } <= set(summaries[0])
    assert [(item.page, item.variant, item.effect_kind) for item in readings] == [
        ("campaigns", "1d", "written"),
        ("campaigns", "2d", "written"),
    ]
    assert {item.memory_peak_mb for item in readings} == {128.0}


# --- plan 4.7: metrics dimensions and non-fatal telemetry ---------------------------------------

_CLOSED_DIMENSIONS = {"Page", "Variant", "EffectKind"}


def require_closed_dimensions(points: tuple[Any, ...]) -> None:
    """Fail on any dimension that is not a closed enum (an identifier is never a dimension)."""
    for point in points:
        names = {name for name, _value in point.dimensions}
        assert names == _CLOSED_DIMENSIONS, f"{point.name} dimensions {sorted(names)}"
        kinds = {kind.value for kind in BakeEffectKind} | {"failed"}
        assert dict(point.dimensions)["EffectKind"] in kinds, point.name


def test_bake_task_metrics_carry_closed_dimensions_only() -> None:
    reading = BakeTaskReading("campaigns", "7d", "written", 12.5, 300.0, 26, 3)
    points = bake_task_datapoints(reading)

    assert [point.name for point in points] == [
        "GraphBakeTaskSeconds",
        "GraphBakeTaskMemoryPeakMb",
        "GraphBakeQueryCount",
        "GraphBakeWritePairs",
    ]
    require_closed_dimensions(points)
    # Control: a run identifier as a dimension is refused by the same check.
    leaked = tuple(
        SimpleNamespace(name=point.name, dimensions=(*point.dimensions, ("RunId", "run-1")))
        for point in points
    )
    with pytest.raises(AssertionError, match="RunId"):
        require_closed_dimensions(leaked)


def test_a_telemetry_failure_is_counted_and_never_changes_the_effect() -> None:
    def failing(_reading: BakeTaskReading) -> None:
        raise RuntimeError("metrics export failed")

    effects: dict[str, str | None] = {}
    for label, metrics in (("control", None), ("failing", failing)):
        bake = Bake(roster=roster(1))
        logger = MagicMock()
        signals: list[tuple[str, int]] = []
        bake.runtime.logger = logger
        bake.runtime.trace_signal = lambda name, count, into=signals: into.append((name, count))
        if metrics is not None:
            bake.runtime.metrics = metrics
        else:
            bake.runtime.metrics = lambda _reading: None
        run = _served(bake)
        effects[label] = bake.node(run, task_node_name(bake.roster.keys[0])).effect_digest
        warnings = [
            call.kwargs["extra"]
            for call in logger.warning.call_args_list
            if call.kwargs["extra"]["outcome"] == "telemetry_unavailable"
        ]
        if metrics is None:
            assert (bake.runtime.telemetry_failures, warnings, signals) == (0, [], [])
        else:
            assert bake.runtime.telemetry_failures == 1
            assert [item["cause"] for item in warnings] == ["RuntimeError"]
            assert signals == [("GraphBakeTelemetryLoss", 1)]  # A2: the alarmed loss count
        assert bake.run_item(run)["state"]["S"] == "SUCCEEDED"
    assert effects["failing"] == effects["control"] is not None


# --- A2: the telemetry-loss and trace-completeness alarm signals ---------------------------------


def test_a_failing_loss_count_still_logs_and_never_changes_the_effect() -> None:
    def failing(_reading: BakeTaskReading) -> None:
        raise RuntimeError("metrics export failed")

    def unavailable(_name: str, _count: int) -> None:
        raise RuntimeError("emf unavailable")

    bake = Bake(roster=roster(1))
    logger = MagicMock()
    bake.runtime.logger, bake.runtime.metrics, bake.runtime.trace_signal = (
        logger,
        failing,
        unavailable,
    )
    run = _served(bake)

    warnings = [
        call.kwargs["extra"]
        for call in logger.warning.call_args_list
        if call.kwargs["extra"]["outcome"] == "telemetry_unavailable"
    ]
    assert len(warnings) == 1 and bake.runtime.telemetry_failures == 1
    assert bake.run_item(run)["state"]["S"] == "SUCCEEDED"


def _statistics(state: str, *, available: bool) -> QueryStatistics:
    return QueryStatistics(state, 1, 1, 1, 1, 1, 1, 0, available)


def _report(bake: Bake, statistics: tuple[QueryStatistics, ...]) -> list[tuple[str, int]]:
    signals: list[tuple[str, int]] = []
    bake.runtime.trace_signal = lambda name, count: signals.append((name, count))
    bake.runtime.metrics = lambda _reading: None
    sent = SimpleNamespace(page="campaigns", variant="1d", run_id="run-1", node_id="task-1")
    report = graph_bake._TaskReport(  # pyright: ignore[reportPrivateUsage]
        cast("Any", SimpleNamespace(body=sent)),
        graph_bake._TaskObservation(),  # pyright: ignore[reportPrivateUsage]
        statistics,
        cast("Any", SimpleNamespace(outcome="graph_bake_task_written")),
        1.0,
    )
    graph_bake._report_task(bake.runtime, report)  # pyright: ignore[reportPrivateUsage]
    return signals


def test_a_succeeded_execution_without_statistics_is_counted_as_trace_incomplete() -> None:
    absent = _statistics("SUCCEEDED", available=False)
    assert _report(Bake(roster=roster(1)), (absent, absent)) == [("GraphBakeTraceIncomplete", 2)]
    # Controls: complete statistics, and a failed execution recorded by its terminal state (A2.2),
    # are complete traces and count nothing.
    complete = _statistics("SUCCEEDED", available=True)
    failed = _statistics("FAILED", available=False)
    assert _report(Bake(roster=roster(1)), (complete, failed)) == []


def test_the_a2_signals_carry_closed_dimensions_and_refuse_another_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emitted: list[tuple[str, float, list[tuple[str, str]]]] = []

    @contextmanager
    def single_metric(**kwargs: Any) -> Generator[Any]:
        dimensions: list[tuple[str, str]] = []

        def add_dimension(name: str, value: str) -> None:
            dimensions.append((name, value))

        yield SimpleNamespace(add_dimension=add_dimension)
        emitted.append((kwargs["name"], kwargs["value"], dimensions))

    monkeypatch.setattr(graph_metrics, "single_metric", single_metric)
    emit_bake_trace_signal("GraphBakeTelemetryLoss", 1)
    emit_bake_trace_signal("GraphBakeTraceIncomplete", 3)
    assert emitted == [
        ("GraphBakeTelemetryLoss", 1, [("Protocol", "v1"), ("AuthorityScope", "live")]),
        ("GraphBakeTraceIncomplete", 3, [("Protocol", "v1"), ("AuthorityScope", "live")]),
    ]
    with pytest.raises(ValueError, match="closed names"):
        emit_bake_trace_signal("GraphBakeRunId", 1)  # control: an open name is refused
    assert len(emitted) == 2


@pytest.mark.covers("ADR-139")
def test_the_serving_withheld_gauge_carries_only_the_closed_dimensions_and_its_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emitted: list[tuple[str, float, list[tuple[str, str]]]] = []

    @contextmanager
    def single_metric(**kwargs: Any) -> Generator[Any]:
        dimensions: list[tuple[str, str]] = []

        def add_dimension(name: str, value: str) -> None:
            dimensions.append((name, value))

        yield SimpleNamespace(add_dimension=add_dimension)
        emitted.append((kwargs["name"], kwargs["value"], dimensions))

    monkeypatch.setattr(graph_metrics, "single_metric", single_metric)
    emit_serving_withheld(1)
    emit_serving_withheld(0)
    live = [("Protocol", "v1"), ("AuthorityScope", "live")]
    assert emitted == [("GraphServingWithheld", 1, live), ("GraphServingWithheld", 0, live)]
