# SPDX-License-Identifier: MIT-0
"""The shaping seam: each slot is shaped in its own worker and its raw rows released (ADR-143)."""

from __future__ import annotations

import gc
import weakref
from dataclasses import dataclass, field, replace
from typing import Any

import pytest
from publication_support import StubPublications
from test_fold_answer_parity import (
    _request as _breakdown,  # pyright: ignore[reportPrivateUsage]
)
from test_fold_answer_parity import (
    _rows as _plan_rows,  # pyright: ignore[reportPrivateUsage]
)
from test_retail_health_preset_source import (
    CONTRACTS,
    METRIC_CATALOG,
    NOW,
    PERIOD,
    PRIOR,
    RetailQueries,
    _products,  # pyright: ignore[reportPrivateUsage]
)

from adapters.inbound.tool_handlers.measurement import (
    PUBLISH_TIME_ONLY,
    MeasurementExecutionTooling,
    PresentationMeasurementAnswerSlot,
    PresentationMeasurementExecutionResult,
    execute_presentation_batch,
    execute_presentation_query_batch,
    prepare_batch,
)
from adapters.jobs.preset_artifact_publisher.retail_source import (
    RetailHealthMeasurementBatchRequest,
    retail_health_measurement_batch,
)
from adapters.outbound.athena.query import (
    _suppression_from,  # pyright: ignore[reportPrivateUsage]
)
from domain.measurement.answers import AnswerRequest, answer_measurement
from domain.measurement.folds import Fold, ObservedKeys
from domain.narrative.shaped_response import ShapedResponse
from domain.shared_kernel.errors import InvariantError
from ports.outbound.publication_selection import (
    PresentationEvidenceInspectionPlan,
    PresentationEvidenceInspectionRequest,
    PresentationEvidenceInspectionResult,
)
from ports.outbound.query import (
    BAKE_MAX_ROWS,
    FoldRead,
    PeriodSeriesRequest,
    QueryRequest,
    QueryResult,
    RowSink,
)

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-143")]


class _Tracked(QueryResult):
    """A raw result a test can watch die: the frozen dataclass plus a weak-reference slot."""

    __slots__ = ("__weakref__",)


@dataclass
class _WatchedQueries:
    """Answers through the Retail fake; checks earlier raw results are gone as each query runs."""

    inner: RetailQueries
    refs: list[weakref.ref[QueryResult]] = field(default_factory=list[weakref.ref[QueryResult]])
    alive_at_query: list[int] = field(default_factory=list[int])

    def run(self, request: QueryRequest) -> QueryResult:
        gc.collect()
        self.alive_at_query.append(sum(1 for ref in self.refs if ref() is not None))
        result = self.inner.run(request)
        tracked = _Tracked(
            rows=result.rows,
            bytes_scanned=result.bytes_scanned,
            truncated=result.truncated,
            suppression=result.suppression,
            population=result.population,
        )
        self.refs.append(weakref.ref(tracked))
        return tracked


def _batch() -> tuple[Any, PresentationEvidenceInspectionResult]:
    prepared = retail_health_measurement_batch(
        RetailHealthMeasurementBatchRequest(METRIC_CATALOG, CONTRACTS, PERIOD, PRIOR, NOW, 2)
    ).prepared
    inspection = StubPublications().inspect(
        PresentationEvidenceInspectionRequest(
            plans=tuple(
                PresentationEvidenceInspectionPlan(plan.role, plan.logical)
                for plan in prepared.plans
            ),
            contracts=prepared.contracts,
            now=prepared.now,
        )
    )
    return prepared, inspection


def _old_shaping(
    prepared: Any, inspection: PresentationEvidenceInspectionResult, queries: Any
) -> tuple[ShapedResponse | None, ...]:
    """Shape in the base's order: execute every raw slot, then shape each as a breakdown."""
    tooling = MeasurementExecutionTooling(queries=queries)
    queried = execute_presentation_query_batch(prepared, inspection, tooling)
    operations = tuple(operation for operation in prepared.operations for _ in operation.plans)
    shaped: list[ShapedResponse | None] = []
    for operation, expected, slot in zip(operations, prepared.plans, queried.slots, strict=True):
        if slot.result is None or slot.coverage is None:
            shaped.append(None)
            continue
        contract_ids = tuple(
            sorted({item.contract_id for item in operation.request.contributing_contracts})
        )
        shaped.append(
            answer_measurement(
                AnswerRequest(
                    plan=expected.logical,
                    context=operation.request.context,
                    rows=slot.result.rows,
                    suppression=slot.result.suppression,
                    coverage=slot.coverage,
                    applied_defaults=operation.applied,
                    additive=True,
                    truncated=slot.result.truncated,
                    contract_ids=contract_ids,
                    qualified_dq=slot.qualified_dq,
                )
            )
        )
    return tuple(shaped)


def test_the_seam_shapes_the_same_answers_the_whole_batch_shaping_gave() -> None:
    prepared, inspection = _batch()
    products = _products(12)
    executed = execute_presentation_batch(
        prepared,
        inspection,
        MeasurementExecutionTooling(queries=RetailQueries(products, PERIOD)),
    )
    before = _old_shaping(prepared, inspection, RetailQueries(products, PERIOD))
    assert all(isinstance(slot, PresentationMeasurementAnswerSlot) for slot in executed.slots)
    for slot, answer in zip(executed.slots, before, strict=True):
        assert slot.answer is not None and answer is not None
        assert slot.answer.data == answer.data
        assert slot.answer.narrative == answer.narrative
        assert slot.answer.metadata.qualifiers == answer.metadata.qualifiers


def test_a_slots_raw_result_is_unreachable_once_its_worker_returns() -> None:
    """At concurrency 1, every earlier raw result is gone by the time the next query starts.

    Paired control: the raw execution path (`execute_presentation_query_batch`, whose one caller
    shapes nothing itself) keeps every raw result until the batch returns.
    """
    prepared, inspection = _batch()
    watched = _WatchedQueries(RetailQueries(_products(12), PERIOD))
    execute_presentation_batch(prepared, inspection, MeasurementExecutionTooling(queries=watched))
    assert len(watched.alive_at_query) > 2
    assert set(watched.alive_at_query) == {0}

    control = _WatchedQueries(RetailQueries(_products(12), PERIOD))
    execute_presentation_query_batch(
        prepared, inspection, MeasurementExecutionTooling(queries=control)
    )
    assert control.alive_at_query[-1] == len(control.alive_at_query) - 1


# --- Fold slots through the seam (design 03 §3.4) ---------------------------------------------

_FOLDED_METRIC = "units_shipped"
"""A simple one-measure metric with no withholding or currency: the plainest eligible fold."""
_ASIN_KEYS = (("amazon_asin-0-0",), ("amazon_asin-0-2",), ("amazon_asin-1-1",))
"""The product keys `_plan_rows` gives a `(product, marketplace)` breakdown, sorted."""


def _asins() -> Fold[Any]:
    return ObservedKeys(("amazon_asin",), BAKE_MAX_ROWS)


def _markets() -> Fold[Any]:
    return ObservedKeys(("marketplace_key",), BAKE_MAX_ROWS)


@dataclass
class _FoldingQueries:
    """A folding port over `_plan_rows`; records each statement it runs, by kind and metric."""

    statements: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])

    def run(self, request: QueryRequest) -> QueryResult:
        plan = request.plan.logical
        self.statements.append(("run", plan.metric.name))
        rows = _plan_rows(plan)
        return QueryResult(
            rows=rows, bytes_scanned=0, truncated=False, suppression=_suppression_from(plan, rows)
        )

    def run_series(self, request: PeriodSeriesRequest) -> QueryResult:
        raise AssertionError(f"no series read in this batch: {request}")

    def fold(self, request: QueryRequest, sink: RowSink) -> FoldRead:
        plan = request.plan.logical
        self.statements.append(("fold", plan.metric.name))
        rows = _plan_rows(plan)
        for row in rows:
            sink.add(row)
        return FoldRead(
            rows_read=len(rows),
            truncated=False,
            bytes_scanned=0,
            suppression=_suppression_from(plan, rows),
        )

    def fold_series(self, request: PeriodSeriesRequest, sink: RowSink) -> FoldRead:
        raise AssertionError(f"no series read in this batch: {request, sink}")


def _executed(
    *slots: tuple[str, Any], queries: Any = None
) -> tuple[PresentationMeasurementExecutionResult, _FoldingQueries]:
    """Prepare, inspect and execute one breakdown per `(metric, reduction or None)` slot."""
    prepared = prepare_batch(
        tuple(
            replace(_breakdown(metric, ("product", "marketplace")), reduction=reduction)
            for metric, reduction in slots
        )
    )
    inspection = StubPublications().inspect(
        PresentationEvidenceInspectionRequest(
            plans=tuple(
                PresentationEvidenceInspectionPlan(plan.role, plan.logical)
                for plan in prepared.plans
            ),
            contracts=prepared.contracts,
            now=prepared.now,
        )
    )
    port = _FoldingQueries() if queries is None else queries
    tooling = MeasurementExecutionTooling(queries=port, query_batch_max_concurrency=2)
    return execute_presentation_batch(prepared, inspection, tooling), port


def test_a_fold_slot_streams_once_and_keeps_only_its_summary() -> None:
    """Paired: the same read held keeps its answer and runs held; folded, only the keys survive."""
    folded, port = _executed((_FOLDED_METRIC, _asins))
    (slot,) = folded.slots
    assert slot.answer is None and slot.folded is not None
    assert slot.folded.value == _ASIN_KEYS
    assert slot.folded.rows_read == 3 and slot.folded.truncated is False
    assert port.statements == [("fold", _FOLDED_METRIC)]

    held, control = _executed((_FOLDED_METRIC, None))
    (kept,) = held.slots
    assert kept.folded is None and kept.answer is not None
    assert len(kept.answer.data.values) == 3
    assert control.statements == [("run", _FOLDED_METRIC)]


def test_a_fold_slot_without_a_folding_port_is_a_defect() -> None:
    @dataclass
    class _HeldOnly:
        inner: _FoldingQueries = field(default_factory=_FoldingQueries)

        def run(self, request: QueryRequest) -> QueryResult:
            return self.inner.run(request)

    with pytest.raises(InvariantError) as raised:
        _executed((_FOLDED_METRIC, _asins), queries=_HeldOnly())
    assert raised.value.code == PUBLISH_TIME_ONLY


def test_equal_fold_slots_share_one_statement_each_with_its_own_fold() -> None:
    """Two folds of one request run one statement; each slot keeps its own fold's summary.

    Paired control: fold slots over two different requests run two statements.
    """
    same, port = _executed((_FOLDED_METRIC, _asins), (_FOLDED_METRIC, _asins))
    assert port.statements == [("fold", _FOLDED_METRIC)]
    assert [slot.folded.value for slot in same.slots if slot.folded] == [_ASIN_KEYS, _ASIN_KEYS]

    mixed, shared = _executed((_FOLDED_METRIC, _asins), (_FOLDED_METRIC, _markets))
    assert shared.statements == [("fold", _FOLDED_METRIC)]
    assert [slot.folded.value for slot in mixed.slots if slot.folded] == [
        _ASIN_KEYS,
        (("marketplace_key-0-0",), ("marketplace_key-0-2",), ("marketplace_key-1-1",)),
    ]

    _, control = _executed((_FOLDED_METRIC, _asins), ("units_ordered", _asins))
    assert sorted(control.statements) == [("fold", "units_ordered"), ("fold", _FOLDED_METRIC)]


@pytest.mark.parametrize("held_first", [True, False])
def test_a_held_and_a_fold_slot_on_one_request_share_one_statement(held_first: bool) -> None:
    """Each slot is shaped as it declared, whichever leads, from one statement.

    The held answer is the held-only batch's, and the fold's summary the fold-only batch's.
    """
    held_slot, fold_slot = (_FOLDED_METRIC, None), (_FOLDED_METRIC, _asins)
    order = (held_slot, fold_slot) if held_first else (fold_slot, held_slot)
    executed, port = _executed(*order)
    assert port.statements == [("fold", _FOLDED_METRIC)]
    held = next(slot for slot in executed.slots if slot.answer is not None)
    folded = next(slot for slot in executed.slots if slot.folded is not None)
    assert held.folded is None and folded.answer is None

    (alone,) = _executed(held_slot)[0].slots
    assert alone.answer is not None and held.answer is not None
    assert held.answer.data == alone.answer.data
    assert held.answer.narrative == alone.answer.narrative
    assert held.answer.metadata.truncated is alone.answer.metadata.truncated is False
    assert folded.folded is not None and folded.folded.value == _ASIN_KEYS
