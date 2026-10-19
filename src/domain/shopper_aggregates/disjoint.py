# SPDX-License-Identifier: MIT-0
"""Sums across a disjoint shopper axis, inside one fully returned delivery (design D-3, D-9).

Each shopper falls in exactly one exposure group, frequency bucket, identity space or acquisition
channel group, so a sum across every value of one such axis is exact. The planner allows it from
the axis rule (`disjoint_within_extraction`); whether every value came back is a property of the
rows, so `sum_disjoint` checks it when the cells are summed.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from domain.semantics.compatibility import (
    DISJOINT_AXES,
    AggregationKind,
    AggregationPolicy,
    AxisBehavior,
    AxisPolicy,
    SemanticAxis,
)
from domain.shopper_aggregates.cells import CellState

_SHOPPER_KINDS = frozenset({AggregationKind.SUM, AggregationKind.EXACT_DISTINCT})


@dataclass(frozen=True, slots=True)
class ShopperPolicyRequest:
    """A shopper-aggregate measure's algebra and the axes its fact exposes."""

    kind: AggregationKind
    axes: frozenset[SemanticAxis]


def shopper_aggregate_policy(request: ShopperPolicyRequest) -> AggregationPolicy:
    """Build the one axis rule for a shopper-aggregate measure (D-3 for counts, D-9 for sums).

    Disjoint axes are `disjoint_within_extraction`; every other axis, including time, channel,
    ad product, campaign and segment, is `exact_only`. So pack 7 purchases can't be summed across
    ad products (one purchase sits in every ad-product window the shopper had), and nothing is
    summed across periods.
    """
    if request.kind not in _SHOPPER_KINDS:
        raise ValueError(
            f"a shopper-aggregate measure is sum or exact_distinct, not {request.kind}."
        )
    policies = {
        axis: AxisPolicy(
            aggregation=(
                AxisBehavior.DISJOINT_WITHIN_EXTRACTION
                if axis in DISJOINT_AXES
                else AxisBehavior.EXACT_ONLY
            ),
            groupable=True,
            filterable=True,
            alignable=True,
        )
        for axis in request.axes
    }
    return AggregationPolicy(
        kind=request.kind,
        available_axes=request.axes,
        axes=policies,
        ecosystem_aggregation=AxisBehavior.EXACT_ONLY,
    )


class DisjointSumRefusalError(ValueError):
    """The cells can't be summed: a gap, a second delivery, or an incomplete axis."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class DisjointCell:
    """One value of a disjoint axis from one delivery, with its measure (`None` when absent)."""

    delivery: str
    """The delivery's identity: contract, period, marketplace and grain set (and for frequency
    by channel, the channel), as the caller keys it."""
    value: str
    state: CellState
    measure: Decimal | None


@dataclass(frozen=True, slots=True)
class DisjointSumRequest:
    """The axis summed across, every value it declares, and the cells to sum."""

    axis: SemanticAxis
    values: frozenset[str]
    cells: tuple[DisjointCell, ...]


def sum_disjoint(request: DisjointSumRequest) -> Decimal:
    """Sum one disjoint axis inside one delivery, or refuse.

    Refused, with a reason a caller can render as "not available": the axis isn't disjoint; the
    cells come from more than one delivery; a value is missing, repeated or undeclared; or any
    cell is `not_returned`, because a total over the rest would be short by an unknown amount.
    """
    if request.axis not in DISJOINT_AXES:
        raise DisjointSumRefusalError(
            "axis_not_disjoint", f"{request.axis.value} doesn't put each shopper in one value."
        )
    deliveries = {cell.delivery for cell in request.cells}
    if len(deliveries) != 1:
        raise DisjointSumRefusalError(
            "not_one_delivery", f"the cells come from {len(deliveries)} deliveries, not one."
        )
    values = [cell.value for cell in request.cells]
    if len(set(values)) != len(values) or frozenset(values) != request.values:
        raise DisjointSumRefusalError(
            "axis_incomplete",
            f"{request.axis.value} needs exactly one cell per value "
            f"({', '.join(sorted(request.values))}).",
        )
    gaps = sorted(cell.value for cell in request.cells if cell.state is CellState.NOT_RETURNED)
    if gaps:
        raise DisjointSumRefusalError(
            "not_returned",
            f"{request.axis.value} {', '.join(gaps)} didn't come back, so the total isn't "
            "available.",
        )
    measures = [cell.measure for cell in request.cells]
    if any(measure is None for measure in measures):
        raise DisjointSumRefusalError(
            "not_returned", f"a returned {request.axis.value} cell carries no measure."
        )
    return sum((measure for measure in measures if measure is not None), start=Decimal(0))
