# SPDX-License-Identifier: MIT-0
"""Loss as missing expected cells over expected cells, and how it grades (ADR-099 point 9).

ADR-019's row ratio can't say which cells a withheld row came from, so these packs count cells.
ADR-019's thresholds (5% degrade, 20% withdraw) were set for many rows; on a 6-cell enumeration
one missing cell is 16.7%. So withdrawal needs two more things: 20 or more expected cells, and a
coarser served grain set to point the reader to. Per-slice grading never withdraws. Load and bake
both call `grade_loss`, so they can't disagree.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from domain.shopper_aggregates.grain_sets import GrainSet

DEGRADE_THRESHOLD = Decimal("0.05")
WITHDRAW_THRESHOLD = Decimal("0.20")
"""ADR-019's starting values: a grain degrades or withdraws when its loss exceeds these."""
WITHDRAW_MIN_CELLS = 20
"""Below this many expected cells one small missing entry could blank a table (review DS6-3)."""
DEGRADED_NOTE = "Some breakdowns had too few shoppers and aren't shown."
CELL_LOSS_RULE_SUFFIX = "cell-loss.v1"
"""Rule id suffix of the publication evidence that records a closed enumeration's loss."""


def cell_loss_rule_id(contract_id: str) -> str:
    """Return a contract's cell-loss evidence rule id: `suppression:{contract}.cell-loss.v1`."""
    return f"suppression:{contract_id}.{CELL_LOSS_RULE_SUFFIX}"


class LossScope(StrEnum):
    """What the loss is computed over."""

    SLICE = "slice"
    """One `identity_space` slice (packs 5 to 7) or `acquisition_source_set` slice (14 and 15)
    of a delivery. Graded only: a missing cell renders as a gap and the rest render."""

    DELIVERY = "delivery"
    """A closed enumeration over the whole delivery, checked at load."""

    ROSTER = "roster"
    """A roster (campaigns with spend, ad products with spend, last month's segments), counted
    per delivery at bake."""


class LossGrade(StrEnum):
    """How a slice, delivery or roster reads after its loss."""

    NOT_DEGRADED = "not_degraded"
    DEGRADED = "degraded"
    WITHDRAWN = "withdrawn"
    """Not offered. Its cells still render "not available", with a pointer to the coarser view."""


@dataclass(frozen=True, slots=True)
class LossRequest:
    """Cell counts for one graded set, and the coarser grain set it could point to."""

    expected: int
    missing: int
    scope: LossScope
    coarser_served: GrainSet | None = None

    def __post_init__(self) -> None:
        if self.expected < 0 or not 0 <= self.missing <= self.expected:
            raise ValueError("missing cells must fall within the expected cells.")


@dataclass(frozen=True, slots=True)
class GrainSetLoss:
    """The recorded loss and grade, for the freshness registry and the renderer."""

    expected: int
    missing: int
    grade: LossGrade
    pointer: GrainSet | None = None
    """The coarser served grain set a withdrawn set points to; `None` unless withdrawn."""

    @property
    def ratio(self) -> Decimal:
        """Return missing over expected; zero when nothing was expected (all cohorts absent)."""
        return Decimal(self.missing) / Decimal(self.expected) if self.expected else Decimal(0)

    @property
    def note(self) -> str | None:
        """Return the server-built text a degraded or withdrawn set shows, or `None`."""
        if self.grade is LossGrade.WITHDRAWN and self.pointer is not None:
            # The reason a withdrawn cell's "not available" carries, as design §4.4 words it.
            return f"too few shoppers in this breakdown; see {self.pointer.label}"
        if self.grade is LossGrade.DEGRADED:
            return DEGRADED_NOTE
        return None


def grade_loss(request: LossRequest) -> GrainSetLoss:
    """Grade one set's loss: withdraw only a 20-plus-cell set that has a coarser served set."""
    loss = GrainSetLoss(request.expected, request.missing, LossGrade.NOT_DEGRADED)
    withdrawable = (
        request.scope is not LossScope.SLICE
        and request.expected >= WITHDRAW_MIN_CELLS
        and request.coarser_served is not None
    )
    if withdrawable and loss.ratio > WITHDRAW_THRESHOLD:
        return GrainSetLoss(
            request.expected, request.missing, LossGrade.WITHDRAWN, request.coarser_served
        )
    if loss.ratio > DEGRADE_THRESHOLD:
        return GrainSetLoss(request.expected, request.missing, LossGrade.DEGRADED)
    return loss
