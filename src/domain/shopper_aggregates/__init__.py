# SPDX-License-Identifier: MIT-0
"""The shopper-aggregate fact family: distinct shopper counts and their sums (ADR-099, ADR-101).

A distinct count can't be summed or derived from a finer grain, and a withheld row carries no
cell identity. So every served grain set is its own extraction, expected cells that didn't come
back are written as `not_returned`, loss is missing expected cells over expected cells, and a
sum is exact only across a disjoint axis inside one fully returned delivery.

Pure rules with no AWS, clock or file access. The load path, the materializer and the planner
call them; no pack, contract or fact uses them yet (the first is the S2.1 pilot).
"""

from domain.shopper_aggregates.cells import (
    CELL_STATE_COLUMN,
    CellState,
    ClosedEnumeration,
    EnumerationAxis,
    EnumerationOutcome,
    EnumerationRequest,
    SliceCells,
    fill_closed_enumeration,
)
from domain.shopper_aggregates.checks import (
    BarOutcome,
    BarRequest,
    BarVerdict,
    RepeatedTotal,
    RepeatedTotalRequest,
    check_bar,
    check_repeated_totals,
)
from domain.shopper_aggregates.disjoint import (
    DisjointCell,
    DisjointSumRefusalError,
    DisjointSumRequest,
    ShopperPolicyRequest,
    shopper_aggregate_policy,
    sum_disjoint,
)
from domain.shopper_aggregates.findings import (
    ShopperAggregateContractError,
    ShopperAggregateFinding,
)
from domain.shopper_aggregates.grain_sets import GrainSet, ServedGrainSets
from domain.shopper_aggregates.loss import (
    DEGRADE_THRESHOLD,
    DEGRADED_NOTE,
    WITHDRAW_MIN_CELLS,
    WITHDRAW_THRESHOLD,
    GrainSetLoss,
    LossGrade,
    LossRequest,
    LossScope,
    grade_loss,
)

__all__ = [
    "CELL_STATE_COLUMN",
    "DEGRADED_NOTE",
    "DEGRADE_THRESHOLD",
    "WITHDRAW_MIN_CELLS",
    "WITHDRAW_THRESHOLD",
    "BarOutcome",
    "BarRequest",
    "BarVerdict",
    "CellState",
    "ClosedEnumeration",
    "DisjointCell",
    "DisjointSumRefusalError",
    "DisjointSumRequest",
    "EnumerationAxis",
    "EnumerationOutcome",
    "EnumerationRequest",
    "GrainSet",
    "GrainSetLoss",
    "LossGrade",
    "LossRequest",
    "LossScope",
    "RepeatedTotal",
    "RepeatedTotalRequest",
    "ServedGrainSets",
    "ShopperAggregateContractError",
    "ShopperAggregateFinding",
    "ShopperPolicyRequest",
    "SliceCells",
    "check_bar",
    "check_repeated_totals",
    "fill_closed_enumeration",
    "grade_loss",
    "shopper_aggregate_policy",
    "sum_disjoint",
]
