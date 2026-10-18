# SPDX-License-Identifier: MIT-0
"""The shaping seam: each slot is shaped in its own worker and its raw rows released (ADR-143)."""

from __future__ import annotations

import gc
import weakref
from dataclasses import dataclass, field
from typing import Any

import pytest
from publication_support import StubPublications
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
    MeasurementExecutionTooling,
    PresentationMeasurementAnswerSlot,
    execute_presentation_batch,
    execute_presentation_query_batch,
)
from adapters.jobs.preset_artifact_publisher.retail_source import (
    RetailHealthMeasurementBatchRequest,
    retail_health_measurement_batch,
)
from domain.measurement.answers import AnswerRequest, answer_measurement
from domain.narrative.shaped_response import ShapedResponse
from ports.outbound.publication_selection import (
    PresentationEvidenceInspectionPlan,
    PresentationEvidenceInspectionRequest,
    PresentationEvidenceInspectionResult,
)
from ports.outbound.query import QueryRequest, QueryResult

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
        )
        self.refs.append(weakref.ref(tracked))
        return tracked


def _batch() -> tuple[Any, PresentationEvidenceInspectionResult]:
    prepared = retail_health_measurement_batch(
        RetailHealthMeasurementBatchRequest(METRIC_CATALOG, CONTRACTS, PERIOD, PRIOR, NOW)
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
