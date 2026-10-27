# SPDX-License-Identifier: MIT-0
"""A folded answer equals the held one over the same rows (ADR-143, design 03 §3.4, ADR-152).

For every eligible plan the catalog declares (a simple one-measure metric with no join), a fold
that collects every figure returns the held answer's figures, units, suppression, truncation and
metadata, built by the one metadata builder. A group-streamed read (`GroupSink`) hands on the
same figures, one group at a time, so an entity-streamed page never shows other figures than a
held read would.
"""

from __future__ import annotations

import pathlib
import threading
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from functools import cache
from typing import Any, cast

import pytest

from adapters.inbound.tool_handlers.measurement import (
    MeasurementTool,
    MeasurementToolRequest,
    prepare_batch,
)
from adapters.jobs.contract_validation.loader import load_contracts
from adapters.jobs.preset_artifact_publisher.group_stream import GroupSink
from adapters.outbound.athena.query import (
    _suppression_from,  # pyright: ignore[reportPrivateUsage]
)
from adapters.outbound.metric_dictionary import load_catalog
from domain.catalog.capability import MetricKind
from domain.catalog.freshness import Coverage
from domain.measurement.answers import (
    AnswerRequest,
    MeasurementContext,
    answer_measurement,
    fold_answer,
)
from domain.measurement.figures import Figure
from domain.measurement.folds import FOLD_INELIGIBLE, ObservedKeys
from domain.measurement.groups import KeyedGroup
from domain.measurement.planning import (
    CURRENCY_COLUMN,
    MEASURE_ALIAS,
    QueryPlan,
    measure_alias,
    withheld_alias,
    withheld_rows_alias,
)
from domain.shared_kernel.errors import InvariantError
from domain.shared_kernel.provenance import FreshnessStatus, MeasurementBasis
from domain.shared_kernel.values import DateRange
from ports.outbound.query import BAKE_MAX_ROWS, QueryRow, RowCeiling

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-143")]

ROOT = pathlib.Path(__file__).resolve().parents[2]
WINDOW = DateRange(date(2026, 8, 1), date(2026, 8, 7))
NOW = datetime(2026, 8, 10, tzinfo=UTC)
_GRAINS = (("product", "marketplace"), ("campaign", "marketplace"), ("marketplace",))


@cache
def _catalog_and_contracts() -> tuple[object, tuple[object, ...]]:
    return load_catalog(ROOT), tuple(contract for _, contract in load_contracts(ROOT))


def _request(metric: str, grains: tuple[str, ...]) -> MeasurementToolRequest:
    catalog, contracts = _catalog_and_contracts()
    return MeasurementToolRequest(
        tool=MeasurementTool.METRIC_BY_GRAIN,
        catalog=catalog,  # type: ignore[arg-type]
        contracts=contracts,  # type: ignore[arg-type]
        metric_name=metric,
        grains=grains,
        window=WINDOW,
        computed_at=NOW,
        correlation_id="fold-parity",
        max_rows=BAKE_MAX_ROWS,
        row_ceiling=RowCeiling.BAKE,
    )


@cache
def eligible_plans() -> tuple[QueryPlan, ...]:
    """Every catalog metric's first plannable breakdown that a fold may read."""
    catalog, _ = _catalog_and_contracts()
    plans: list[QueryPlan] = []
    for metric in cast("Any", catalog).metrics:
        name = cast("str", metric.name)
        for grains in _GRAINS:
            try:
                current = prepare_batch((_request(name, grains),)).operations[0].current
            except Exception:  # noqa: BLE001, S112 -- a metric this breakdown doesn't serve
                continue
            if (
                current.metric.kind is MetricKind.SIMPLE
                and len(current.measures) == 1
                and current.join is None
                and current.roster is None
            ):
                plans.append(current)
            break
    return tuple(plans)


def _rows(plan: QueryPlan) -> tuple[QueryRow, ...]:
    """Three groups: two measured, one withheld whole when the fact can withhold."""
    rows: list[QueryRow] = []
    for index in range(3):
        row: dict[str, str | int | Decimal | None] = {
            column: f"{column}-{index % 2}-{index}" for column in plan.group_by
        }
        for column in plan.carried_columns:
            row[column] = "USD" if column == CURRENCY_COLUMN else f"{column}-value"
        row[measure_alias(0)] = Decimal(10 * (index + 1))
        if plan.suppression_columns:
            withheld = index == 2
            row[measure_alias(0)] = None if withheld else row[measure_alias(0)]
            row[withheld_alias(0)] = Decimal(5) if withheld else Decimal(0)
            row[withheld_rows_alias(0)] = 2 if withheld else 0
        rows.append(row)
    return tuple(rows)


@dataclass
class _Collect:
    """A fold that keeps every figure, so the held and folded figures compare one to one."""

    max_kept: int = 1_000
    overflowed: bool = False
    figures: list[Figure] = field(default_factory=list[Figure])

    def add(self, figure: Figure) -> None:
        self.figures.append(figure)

    def value(self) -> tuple[Figure, ...]:
        return tuple(self.figures)


def _answer_request(
    plan: QueryPlan, rows: tuple[QueryRow, ...], *, truncated: bool
) -> AnswerRequest:
    return AnswerRequest(
        plan=plan,
        context=MeasurementContext(computed_at=NOW, correlation_id="fold-parity"),
        rows=rows,
        suppression=_suppression_from(plan, rows),
        coverage=Coverage(
            fact_name=plan.fact,
            window=WINDOW,
            freshness=FreshnessStatus.FRESH,
            measurement_basis=plan.measurement_basis[0]
            if plan.measurement_basis
            else MeasurementBasis.RETAIL_ACTUAL,
            valid_grains=plan.grain.dimensions,
            intervals=(WINDOW,),
        ),
        applied_defaults={},
        additive=True,
        truncated=truncated,
        qualified_dq=("dq-fictional-caveat",),
    )


def test_the_catalog_declares_eligible_plans_of_each_kind() -> None:
    plans = eligible_plans()
    assert len(plans) >= 20
    assert any(plan.suppression_columns for plan in plans)
    assert any(CURRENCY_COLUMN in plan.carried_columns for plan in plans)


@pytest.mark.parametrize("truncated", [False, True])
def test_every_eligible_fold_equals_its_held_answer(truncated: bool) -> None:
    """Figures, units, suppression, truncation and metadata, for every eligible plan."""
    for plan in eligible_plans():
        rows = _rows(plan)
        held = answer_measurement(_answer_request(plan, rows, truncated=truncated))
        folded = fold_answer(_answer_request(plan, rows, truncated=truncated), rows, _Collect())
        figures = tuple(
            {**dict(figure.dimensions), MEASURE_ALIAS: figure.value} for figure in folded.value
        )
        assert figures == held.data.values, plan.metric.name
        assert folded.units == held.data.units, plan.metric.name
        assert folded.truncated is held.metadata.truncated is truncated
        assert folded.metadata.suppression == held.metadata.suppression
        assert folded.rows_read == len(rows)
        # One builder: the metadata differs only in what a fold counts as its row count, and in
        # the grounding a held envelope attaches to its own narrative sentences (a fold has none).
        assert (
            replace(
                folded.metadata,
                row_count=held.metadata.row_count,
                grounding=held.metadata.grounding,
            )
            == held.metadata
        )


def _streamed(plan: QueryPlan, rows: tuple[QueryRow, ...]) -> list[KeyedGroup[Figure]]:
    """Fold the rows through a `GroupSink` on a producer thread and drain its groups here."""
    sink = GroupSink(plan.group_by[:1], 1_000, threading.Event())
    producer = threading.Thread(
        target=fold_answer, args=(_answer_request(plan, rows, truncated=False), rows, sink)
    )
    producer.start()
    groups: list[KeyedGroup[Figure]] = []
    while isinstance(item := sink.take(producer.is_alive), KeyedGroup):
        groups.append(item)
    producer.join()
    return groups


def test_every_eligible_group_streamed_read_hands_on_the_held_figures() -> None:
    """Each row's figure reaches its group in read order, withheld figures included."""
    for plan in eligible_plans():
        rows = _rows(plan)
        held = answer_measurement(_answer_request(plan, rows, truncated=False))
        groups = _streamed(plan, rows)
        figures = tuple(
            {**dict(figure.dimensions), MEASURE_ALIAS: figure.value}
            for group in groups
            for figure in group.members
        )
        assert figures == held.data.values, plan.metric.name
        # Every group is keyed by its own leading column's value.
        (column,) = plan.group_by[:1]
        assert all(
            dict(figure.dimensions)[column] == group.key[0]
            for group in groups
            for figure in group.members
        ), plan.metric.name


def test_a_fold_that_overflows_is_truncated_like_a_truncated_read() -> None:
    plan = next(plan for plan in eligible_plans() if not plan.suppression_columns)
    rows = _rows(plan)
    folded = fold_answer(
        _answer_request(plan, rows, truncated=False), rows, ObservedKeys(plan.group_by, 1)
    )
    assert folded.truncated and folded.metadata.truncated
    assert len(folded.value) == 1


def test_a_fold_over_an_ineligible_plan_is_refused_when_the_batch_is_prepared() -> None:
    """Paired with the eligible plans above: a ratio stays held; folding it is a defect."""
    with pytest.raises(InvariantError) as raised:
        prepare_batch(
            (
                replace(
                    _request("asp_shipped", ("product", "marketplace")),
                    reduction=_Collect,
                ),
            )
        )
    assert raised.value.code == FOLD_INELIGIBLE
    # The same request held is accepted.
    assert prepare_batch((_request("asp_shipped", ("product", "marketplace")),))
