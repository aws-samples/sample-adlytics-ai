# SPDX-License-Identifier: MIT-0
"""Retention: `RetentionCohortArtifact` v1 (docs/ui/retention/low-level-design.md).

One cohort vintage (`as_of_month`, the 13 calendar months ending with it) per artifact, built from
the planned per-cell answers of packs 14 and 15 (ADR-104, design §6.6). Rendering-free: every
text is a server field and the browser formats nothing.

- **Triangle.** For every cohort the vintage serves and every follow-up month (or quarter), per
  acquisition group: the rate and the cohort size. A `not_returned` cell is "Not available" with
  `WITHHELD_CELL_LEGEND` (a hatch, never colour alone); a follow-up the vintage hasn't reached is
  "Not yet"; an absent cohort (no row at any k for its group) reads its own text instead of gaps.
  The grid shows one group at a time, so the standard path binds the all-ID `repeat_buyer_rate`
  with the identity caveat; the paid path binds the ARP rate.
- **ARP rate (D-8 (b)).** Pack 15 repeat buyers over pack 14 `cohort_buyers_account_ids` for the
  same vintage, cohort and group, withheld with "cohort sizes changed between runs" when pack 15's
  own count of the cohort differs, or the numerator exceeds the denominator, and with "Cohort grain
  changed in {period}." when the two deliveries' cohort grains differ.
- **Curve.** One line per group, the cumulative repeat rate on Amazon account IDs only (a
  page-schema rule refuses an all-ID rate there): at follow-up k, the D-3 sum of every cohort's
  cumulative account-ID repeat buyers over the sum of their account-ID cohort sizes, inside one
  delivery, and only where every cohort that has reached k came back for the group.
- **Quarterly state.** A vintage on calendar-quarter cohorts labels its rows "Q{n} {year}", its
  columns "Quarter 1" to "Quarter 3", and shows the grain note.

The withheld-cell legend replaces the approved mockup's wording (review E-B-3): a `not_returned`
cell claims neither a suppressed cell nor a measured zero (ADR-099 point 8).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import TYPE_CHECKING, Final, cast

from domain.data_contracts.contract import CohortGrain, CohortTriangle
from domain.measurement.periodic import COHORT_SIZES_CHANGED, arp_ratio_withheld_reason
from domain.presentation.insights import (
    InsightAbsenceClass,
    InsightOutputClass,
    InsightSlot,
    InsightSlotId,
    InsightSlotState,
    ShiftPolicy,
    insight_slots_payload,
)
from domain.semantics.calendar_periods import CalendarPeriod
from domain.semantics.time import TimeGrain
from domain.shopper_aggregates.cohorts import (
    QUARTER_MONTHS,
    TriangleCohort,
    cohort_grain_changed_text,
    triangle_cohorts,
)
from domain.shopper_aggregates.loss import cell_loss_rule_id

if TYPE_CHECKING:  # pragma: no cover
    from domain.semantics.publication import SuccessfulPublication

WITHHELD_CELL_LEGEND: Final = "Not available: too few shoppers bought again, or none"
"""The legend under the cohort grid for a hatched `not_returned` cell."""

RETENTION_PAGE_ID: Final = "retention"
RETENTION_ARTIFACT_TYPE: Final = "RetentionCohortArtifact"
RETENTION_ARTIFACT_VERSION: Final = 1
STANDARD_PATH: Final = "standard"
ARP_PATH: Final = "arp"
ACQUISITION_GROUPS: Final = (
    ("dsp_only", "Won by DSP only"),
    ("sponsored_only", "Won by sponsored ads only"),
    ("dsp_and_sponsored", "Won by both"),
)
"""The acquisition channel group axis's governed display names (resolves the LLD open item)."""
RETENTION_INSIGHT_ROSTER: Final = (InsightSlotId.RETENTION_SHIFT,)
NOT_AVAILABLE_TEXT: Final = "Not available"
NOT_YET_TEXT: Final = "Not yet"
EMPTY_MARKET_TEXT: Final = "No cohorts were measured for this vintage."
INSUFFICIENT_MARKET_TEXT: Final = "There is not enough cohort evidence to show Retention safely."
_SEVERAL_MARKETPLACES: Final = 2
"""From this many markets, each market names its marketplace (as New customers)."""
ABSENT_COHORT_TEXT: Final = "No shoppers were won this way in {month}, or too few to show."
PAID_CALLOUT: Final = (
    "All Amazon purchases by these cohorts, not only ad-touched ones, need the Amazon Retail "
    "Purchases paid feature."
)
PAID_GRID_LABEL: Final = "All Amazon purchases · Amazon account IDs only"
CURVE_LABEL: Final = "Cumulative repeat rate · Amazon account IDs"
QUARTERLY_NOTE: Final = (
    "Cohorts are grouped by quarter from {quarter}, because too many monthly cells had too few "
    "shoppers."
)
IDENTITY_CAVEAT: Final = (
    "Counts Amazon shopper IDs of every type: a shopper won under a device or browser ID can't be "
    "matched to a repeat purchase, so this rate can understate repeat."
)
BOUNDARY: Final = (
    "A cohort is the shoppers whose first new-to-brand purchase in these 13 months fell in one "
    "calendar month, grouped by how the ads won them that day.",
    "The standard path counts ad-touched repeat purchases only, so it understates true repeat.",
    "On an account with several brands, a shopper won for one brand who buys another counts as a "
    "repeat: these are repeats from this advertiser.",
)
_PERCENT: Final = Decimal(100)
_RATE_QUANTUM: Final = Decimal("0.0001")
_POINTS_QUANTUM: Final = Decimal("0.1")
_MINUS: Final = "\u2212"
_MONTH_NAMES: Final = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)  # fmt: skip
_FOLLOW_UPS: Final = {CohortGrain.MONTH: 12, CohortGrain.QUARTER: 3}
SHIFT_MONTHS: Final = (1, 3)
"""`retention-shift` v1 reads the month-1 and month-3 rates; a quarterly vintage has neither."""


class RetentionOutcome(StrEnum):
    """The page entry's declared outcomes."""

    COMPLETE = "complete"
    EMPTY = "empty"
    INSUFFICIENT = "insufficient"


class CohortCellState(StrEnum):
    """What one cell of the triangle shows."""

    VALUE = "value"
    NOT_RETURNED = "not_returned"
    """Hatched "Not available" with `WITHHELD_CELL_LEGEND`, never colour alone."""
    WITHHELD = "withheld"
    """An ARP rate the cohort-size or cohort-grain check withheld, with its reason."""
    NOT_YET = "not_yet"
    """A follow-up month (or quarter) the vintage hasn't reached."""


class PaidPathState(StrEnum):
    """Whether pack 15 (Amazon Retail Purchases) has delivered for the vintage (ADR-107)."""

    OBSERVED = "observed"
    NOT_OBSERVED = "not_observed"


type CellKey = tuple[date, int, str]
"""(cohort's first day, follow-up count, acquisition channel group)."""


@dataclass(frozen=True, slots=True)
class CohortDelivery:
    """One path's planned per-cell answers for one vintage and marketplace.

    `cells` holds every row the answers returned: a returned cell maps each measure to its value,
    a `not_returned` row to None. A cohort and group with no row at all is an absent cohort.
    `checks` is pack 15's own count of each cohort and group (`cohort_buyers_account_ids_check`),
    read from its delivery's load evidence; empty on the standard path.
    """

    path: str
    grain: CohortGrain
    cells: Mapping[CellKey, Mapping[str, Decimal | None]]
    checks: Mapping[tuple[date, str], Decimal] = field(
        default_factory=dict[tuple[date, str], Decimal]
    )

    def has_cohort(self, cohort: date, group: str) -> bool:
        """Whether any row came back for the cohort and group."""
        return any(key[0] == cohort and key[2] == group for key in self.cells)

    def value(self, key: CellKey, measure: str) -> Decimal | None:
        """Return a returned cell's measure, None when the cell or the measure didn't come back."""
        row = self.cells.get(key)
        return None if row is None else row.get(measure)


@dataclass(frozen=True, slots=True)
class RetentionVintage:
    """Everything one marketplace's artifact is built from."""

    period: CalendarPeriod
    marketplace_key: str
    standard: CohortDelivery | None
    paid: CohortDelivery | None = None
    first_quarterly: date | None = None
    """The as-of month of the marketplace's first quarterly vintage, when it has one."""

    def __post_init__(self) -> None:
        if self.period.grain is not TimeGrain.CUSTOM:
            raise ValueError("a Retention artifact reads one cohort vintage.")


@dataclass(frozen=True, slots=True)
class RetentionFigure:
    """One shown value: a rate (0 to 1) or a count, with its server text."""

    value: Decimal
    text: str


@dataclass(frozen=True, slots=True)
class RetentionCell:
    """One triangle cell for one group."""

    offset: int
    column_label: str
    state: CohortCellState
    rate: RetentionFigure | None = None
    cohort_size: RetentionFigure | None = None
    text: str | None = None


@dataclass(frozen=True, slots=True)
class RetentionRow:
    """One cohort's row: its cells, or the absent-cohort text."""

    cohort: date
    label: str
    cells: tuple[RetentionCell, ...] = ()
    absent_text: str | None = None


@dataclass(frozen=True, slots=True)
class RetentionGroupGrid:
    """One acquisition group's triangle on one path."""

    group: str
    label: str
    grid_label: str
    rows: tuple[RetentionRow, ...]


@dataclass(frozen=True, slots=True)
class CurvePoint:
    """One point of one group's repeat curve: a rate, or the reason it's not shown."""

    offset: int
    label: str
    rate: RetentionFigure | None
    text: str | None = None


@dataclass(frozen=True, slots=True)
class CurveLine:
    """One acquisition group's cumulative repeat curve on Amazon account IDs."""

    group: str
    label: str
    points: tuple[CurvePoint, ...]


def _rate(numerator: Decimal, denominator: Decimal) -> RetentionFigure:
    value = (numerator / denominator).quantize(_RATE_QUANTUM)
    return RetentionFigure(value, f"{(value * _PERCENT).quantize(_POINTS_QUANTUM):f}%")


def _count(value: Decimal) -> RetentionFigure:
    return RetentionFigure(value, f"{value:,f} new customers")


def month_label(month: date) -> str:
    """'September 2026': the label a month carries in every Retention text."""
    return f"{_MONTH_NAMES[month.month - 1]} {month.year}"


def quarter_label(start: date) -> str:
    """'Q3 2026': a cohort quarter's row label (Retention LLD "Quarterly cohorts")."""
    return f"Q{(start.month - 1) // QUARTER_MONTHS + 1} {start.year}"


def _cohort_label(grain: CohortGrain, start: date) -> str:
    return month_label(start) if grain is CohortGrain.MONTH else quarter_label(start)


def _column_label(grain: CohortGrain, offset: int) -> str:
    return f"Month {offset}" if grain is CohortGrain.MONTH else f"Quarter {offset}"


def _triangle(grain: CohortGrain) -> CohortTriangle:
    return CohortTriangle("cohort_month", "months_since_acquisition", grain, _FOLLOW_UPS[grain])


def vintage_cohorts(period: CalendarPeriod, grain: CohortGrain) -> tuple[TriangleCohort, ...]:
    """Return the cohorts a vintage serves at its grain, oldest first (`triangle_cohorts`)."""
    return triangle_cohorts(_triangle(grain), (period.start, period.end))


def standard_grid_label(group_label: str, grain: CohortGrain) -> str:
    """Return the standard-path grid's label (Retention LLD "Labels")."""
    unit = "Month" if grain is CohortGrain.MONTH else "Quarter"
    return (
        f"{group_label} · ad-touched repeat only. Understates true repeat. {unit} 0 repeats are "
        "excluded."
    )


def arp_rate(  # noqa: PLR0913 -- one D-8 (b) cell: two deliveries' values and their grains
    repeat: Decimal | None,
    cohort_account_ids: Decimal | None,
    check: Decimal | None,
    *,
    grains: tuple[CohortGrain, CohortGrain],
    first_quarterly: str,
) -> RetentionFigure | str | None:
    """Return one ARP cell's rate, the reason it's withheld, or None when it didn't come back.

    `grains` is pack 14's and pack 15's cohort grain. A grain mismatch is the serve-time
    backstop to the load's variant check, and reads "Cohort grain changed in {period}.", never
    "cohort sizes changed between runs".
    """
    if grains[0] is not grains[1]:
        return cohort_grain_changed_text(first_quarterly)
    if repeat is None or cohort_account_ids is None:
        return None
    if check is None or cohort_account_ids == 0:
        return COHORT_SIZES_CHANGED
    reason = arp_ratio_withheld_reason(int(repeat), int(cohort_account_ids), int(check))
    if reason is not None:
        return reason
    return _rate(repeat, cohort_account_ids)


ARP_DENOMINATOR: Final = "cohort_buyers_account_ids"
"""The ARP rates' denominator: pack 14's account-ID cohort size (D-8 (b))."""
ARP_CHECK_COLUMN: Final = "cohort_buyers_account_ids_check"
"""Pack 15's own count of each cohort: an integrity column, never a dictionary metric."""


def arp_cohort_checks(
    publications: Iterable[SuccessfulPublication], contract_id: str
) -> dict[tuple[date, str], Decimal]:
    """Read pack 15's own count of each cohort and group from its publications' load evidence.

    The count is in the cell-loss evidence's `repeated_totals` (design §6.6, review DS-13), so it's
    never served as a figure. Chat and the Retention bake both read it here.
    """
    rule = cell_loss_rule_id(contract_id)
    found: dict[tuple[date, str], Decimal] = {}
    for publication in publications:
        for result in publication.evidence.results:
            if result.rule_id != rule:
                continue
            record = json.loads(result.detail)
            totals = record.get("repeated_totals", {}).get(ARP_CHECK_COLUMN)
            if not isinstance(totals, dict):
                continue
            for key, value in cast("dict[str, str]", totals).items():
                cohort, group = key.split("|")
                found[(date.fromisoformat(cohort), group)] = Decimal(value)
    return found


def arp_cell_rate(  # noqa: PLR0913 -- one cell of two deliveries: both paths, key, operand
    standard: CohortDelivery,
    paid: CohortDelivery,
    key: CellKey,
    *,
    numerator: str,
    first_quarterly: str,
) -> RetentionFigure | str | None:
    """Return one ARP rate cell from the two paths' deliveries: the one rule chat and the bake read.

    `numerator` is the paid path's repeat measure (`cohort_repeat_buyers` for
    `repeat_buyer_rate_arp`, `cohort_repeat_buyers_cumulative` for `cumulative_repeat_rate_arp`);
    the denominator is always pack 14's `cohort_buyers_account_ids`, and the check pack 15's own
    count of the cohort and group (owner decision 2026-10-12: chat answers every rate the
    Retention bake computes, from the same definition).
    """
    return arp_rate(
        paid.value(key, numerator),
        standard.value(key, ARP_DENOMINATOR),
        paid.checks.get((key[0], key[2])),
        grains=(standard.grain, paid.grain),
        first_quarterly=first_quarterly,
    )


@dataclass(frozen=True, slots=True)
class _GridRequest:
    vintage: RetentionVintage
    delivery: CohortDelivery
    grain: CohortGrain


def _future_cells(grain: CohortGrain, served: tuple[int, ...]) -> tuple[RetentionCell, ...]:
    return tuple(
        RetentionCell(
            offset, _column_label(grain, offset), CohortCellState.NOT_YET, text=NOT_YET_TEXT
        )
        for offset in range(1, _FOLLOW_UPS[grain] + 1)
        if offset not in served
    )


def _standard_cell(delivery: CohortDelivery, key: CellKey, grain: CohortGrain) -> RetentionCell:
    label = _column_label(grain, key[1])
    if key not in delivery.cells:
        return RetentionCell(key[1], label, CohortCellState.NOT_RETURNED, text=NOT_AVAILABLE_TEXT)
    repeat = delivery.value(key, "cohort_repeat_buyers")
    size = delivery.value(key, "cohort_buyers_new_to_brand_12mo")
    if repeat is None or size is None or size == 0:
        return RetentionCell(key[1], label, CohortCellState.NOT_RETURNED, text=NOT_AVAILABLE_TEXT)
    return RetentionCell(
        key[1], label, CohortCellState.VALUE, rate=_rate(repeat, size), cohort_size=_count(size)
    )


def _paid_cell(request: _GridRequest, standard: CohortDelivery, key: CellKey) -> RetentionCell:
    grain = request.grain
    label = _column_label(grain, key[1])
    delivery = request.delivery
    first = request.vintage.first_quarterly
    if key not in delivery.cells and standard.grain is delivery.grain:
        return RetentionCell(key[1], label, CohortCellState.NOT_RETURNED, text=NOT_AVAILABLE_TEXT)
    figure = arp_cell_rate(
        standard,
        delivery,
        key,
        numerator="cohort_repeat_buyers",
        first_quarterly=month_label(first or request.vintage.period.as_of_month),
    )
    if figure is None:
        return RetentionCell(key[1], label, CohortCellState.NOT_RETURNED, text=NOT_AVAILABLE_TEXT)
    if isinstance(figure, str):
        return RetentionCell(key[1], label, CohortCellState.WITHHELD, text=figure)
    size = standard.value(key, "cohort_buyers_account_ids")
    return RetentionCell(
        key[1],
        label,
        CohortCellState.VALUE,
        rate=figure,
        cohort_size=None if size is None else _count(size),
    )


def _grids(request: _GridRequest) -> tuple[RetentionGroupGrid, ...]:
    vintage, delivery, grain = request.vintage, request.delivery, request.grain
    standard = vintage.standard
    cohorts = vintage_cohorts(vintage.period, grain)
    grids: list[RetentionGroupGrid] = []
    for group, group_label in ACQUISITION_GROUPS:
        rows: list[RetentionRow] = []
        for cohort in cohorts:
            label = _cohort_label(grain, cohort.start)
            anchor = standard if delivery.path == ARP_PATH and standard is not None else delivery
            if not anchor.has_cohort(cohort.start, group):
                rows.append(
                    RetentionRow(
                        cohort.start, label, absent_text=ABSENT_COHORT_TEXT.format(month=label)
                    )
                )
                continue
            cells = tuple(
                _standard_cell(delivery, (cohort.start, offset, group), grain)
                if delivery.path == STANDARD_PATH or standard is None
                else _paid_cell(request, standard, (cohort.start, offset, group))
                for offset in cohort.offsets
            )
            rows.append(
                RetentionRow(cohort.start, label, (*cells, *_future_cells(grain, cohort.offsets)))
            )
        grids.append(
            RetentionGroupGrid(
                group,
                group_label,
                standard_grid_label(group_label, grain)
                if delivery.path == STANDARD_PATH
                else PAID_GRID_LABEL,
                tuple(rows),
            )
        )
    return tuple(grids)


def _curve(vintage: RetentionVintage, delivery: CohortDelivery) -> tuple[CurveLine, ...]:
    """Return the cumulative repeat rate on Amazon account IDs per group, pooled in one delivery.

    At follow-up k, the D-3 sum over every cohort that has reached k of its cumulative account-ID
    repeat buyers, over the sum of their account-ID cohort sizes. Shown only where every such
    cohort came back for the group: a not-returned cell or an absent cohort leaves the point
    "Not available", never a partial sum.
    """
    grain = delivery.grain
    cohorts = vintage_cohorts(vintage.period, grain)
    lines: list[CurveLine] = []
    for group, group_label in ACQUISITION_GROUPS:
        points: list[CurvePoint] = []
        for offset in range(1, _FOLLOW_UPS[grain] + 1):
            reached = [cohort for cohort in cohorts if offset in cohort.offsets]
            label = _column_label(grain, offset)
            if not reached:
                continue
            numerator = denominator = Decimal(0)
            complete = True
            for cohort in reached:
                key = (cohort.start, offset, group)
                repeat = delivery.value(key, "cohort_repeat_buyers_cumulative_account_ids")
                size = delivery.value(key, "cohort_buyers_account_ids")
                if repeat is None or size is None:
                    complete = False
                    break
                numerator += repeat
                denominator += size
            if not complete or denominator == 0:
                points.append(CurvePoint(offset, label, None, NOT_AVAILABLE_TEXT))
                continue
            points.append(CurvePoint(offset, label, _rate(numerator, denominator)))
        lines.append(CurveLine(group, group_label, tuple(points)))
    return tuple(lines)


# --- insight: retention-shift v1 -----------------------------------------------------------

_DETERMINISTIC: Final = "deterministic-v1"
_EVIDENCE_LINE: Final = "Found by a fixed rule from the figures above. Not written by an AI model."


@dataclass(frozen=True, slots=True)
class ShiftFinding:
    """One named shift: a group's month-k rate, latest cohort against the one before it."""

    group: str
    offset: int
    latest: date
    earlier: date
    latest_rate: Decimal
    earlier_rate: Decimal

    @property
    def points(self) -> Decimal:
        """The change in rate points (percentage points)."""
        return ((self.latest_rate - self.earlier_rate) * _PERCENT).quantize(_POINTS_QUANTUM)


def _shift_operands(delivery: CohortDelivery, key: CellKey) -> tuple[Decimal, Decimal] | None:
    """Return a cell's repeat buyers and the rate's denominator on the standard path, or None."""
    repeat = delivery.value(key, "cohort_repeat_buyers")
    size = delivery.value(key, "cohort_buyers_new_to_brand_12mo")
    if repeat is None or size is None or size == 0:
        return None
    return repeat, size


def shift_named(
    pair: tuple[tuple[Decimal, Decimal], tuple[Decimal, Decimal]], policy: ShiftPolicy
) -> bool:
    """Whether a shift between two cohorts' rates is named (design §6.6, review DS5-7).

    Both denominators hold at least `cohort_min` shoppers, and the 95% interval of the
    difference, (r2 - r1) +/- z * sqrt(r1(1 - r1)/n1 + r2(1 - r2)/n2), excludes zero. Materiality
    is the contract's, applied by the caller.
    """
    (x1, n1), (x2, n2) = pair
    if min(n1, n2) < policy.cohort_min:
        return False
    r1, r2 = x1 / n1, x2 / n2
    spread = policy.z * (r1 * (1 - r1) / n1 + r2 * (1 - r2) / n2).sqrt()
    difference = r2 - r1
    return difference - spread > 0 or difference + spread < 0


def retention_shifts(vintage: RetentionVintage, policy: ShiftPolicy) -> tuple[ShiftFinding, ...]:
    """Every named month-1 and month-3 shift on the standard path, one group at a time.

    Compares the latest two cohorts that have reached month k, same group, never across groups
    or paths. Silent when either cell is `not_returned`, and on a quarterly vintage.
    """
    standard = vintage.standard
    if standard is None or standard.grain is not CohortGrain.MONTH:
        return ()
    cohorts = vintage_cohorts(vintage.period, CohortGrain.MONTH)
    found: list[ShiftFinding] = []
    for offset in SHIFT_MONTHS:
        reached = [cohort.start for cohort in cohorts if offset in cohort.offsets]
        if len(reached) < 2:  # noqa: PLR2004 -- two cohorts compared
            continue
        earlier, latest = reached[-2], reached[-1]
        for group, _label in ACQUISITION_GROUPS:
            before = _shift_operands(standard, (earlier, offset, group))
            after = _shift_operands(standard, (latest, offset, group))
            if before is None or after is None or not shift_named((before, after), policy):
                continue
            found.append(
                ShiftFinding(
                    group, offset, latest, earlier, after[0] / after[1], before[0] / before[1]
                )
            )
    return tuple(found)


@dataclass(frozen=True, slots=True)
class ShiftSlotRequest:
    """The named shifts that cleared materiality, and the provenance."""

    vintage: RetentionVintage
    findings: tuple[ShiftFinding, ...]
    artifact_revision: str
    silence_reason: str


def retention_shift_slot(request: ShiftSlotRequest) -> InsightSlot:
    """Render `retention-shift` v1: the largest named shift, one group, standard path.

    Deterministic, no prompt or model. Silent when nothing is named, which includes every
    quarterly vintage.
    """
    if not request.findings:
        return InsightSlot(
            InsightSlotId.RETENTION_SHIFT,
            InsightOutputClass.PERSPECTIVE,
            InsightSlotState.SILENT,
            None,
            None,
            request.artifact_revision,
            _DETERMINISTIC,
            absence_class=InsightAbsenceClass.EVALUATED_QUIET,
            absence_reason=request.silence_reason,
        )
    finding = max(request.findings, key=lambda item: (abs(item.points), -item.offset))
    label = dict(ACQUISITION_GROUPS)[finding.group]
    direction = "rose" if finding.points > 0 else "fell"
    points = f"{abs(finding.points):f}"
    return InsightSlot(
        InsightSlotId.RETENTION_SHIFT,
        InsightOutputClass.PERSPECTIVE,
        InsightSlotState.FILLED,
        f"For shoppers {label[0].lower()}{label[1:]}, the month-{finding.offset} repeat rate "
        f"{direction} "
        f"{points} points from the {month_label(finding.earlier)} cohort to the "
        f"{month_label(finding.latest)} cohort. Ad-touched repeat only, from this advertiser.",
        _EVIDENCE_LINE,
        request.artifact_revision,
        _DETERMINISTIC,
    )


# --- the artifact --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RetentionMarket:
    """One marketplace's path, triangles, curve and insights for the vintage."""

    marketplace_key: str
    cohort_grain: CohortGrain | None
    paid_state: PaidPathState
    standard: tuple[RetentionGroupGrid, ...]
    paid: tuple[RetentionGroupGrid, ...]
    curve: tuple[CurveLine, ...]
    insights: tuple[InsightSlot, ...]
    first_quarterly: date | None = None
    marketplace_label: str | None = None
    """The marketplace's short or governed name, which names its sections with two shown."""

    def __post_init__(self) -> None:
        if tuple(slot.id for slot in self.insights) != RETENTION_INSIGHT_ROSTER:
            raise ValueError("the Retention insight roster is retention-shift.")
        if self.marketplace_label is not None and not self.marketplace_label.strip():
            raise ValueError("a Retention marketplace label is nonblank.")

    @property
    def outcome(self) -> RetentionOutcome:
        """Complete with any shown rate, insufficient when only gaps came back, else empty."""
        if not self.standard:
            return RetentionOutcome.EMPTY
        cells = [cell for grid in self.standard for row in grid.rows for cell in row.cells]
        if any(cell.state is CohortCellState.VALUE for cell in cells):
            return RetentionOutcome.COMPLETE
        return RetentionOutcome.INSUFFICIENT


def build_market(
    vintage: RetentionVintage,
    insights: tuple[InsightSlot, ...],
    marketplace_label: str | None = None,
) -> RetentionMarket:
    """Build one marketplace's sections from its planned deliveries."""
    standard = vintage.standard
    paid = vintage.paid
    paid_state = PaidPathState.NOT_OBSERVED if paid is None else PaidPathState.OBSERVED
    if standard is None:
        return RetentionMarket(
            vintage.marketplace_key,
            None,
            paid_state,
            (),
            (),
            (),
            insights,
            vintage.first_quarterly,
            marketplace_label,
        )
    return RetentionMarket(
        marketplace_key=vintage.marketplace_key,
        cohort_grain=standard.grain,
        paid_state=paid_state,
        standard=_grids(_GridRequest(vintage, standard, standard.grain)),
        paid=() if paid is None else _grids(_GridRequest(vintage, paid, standard.grain)),
        curve=_curve(vintage, standard),
        insights=insights,
        first_quarterly=vintage.first_quarterly,
        marketplace_label=marketplace_label,
    )


@dataclass(frozen=True, slots=True)
class RetentionCohortArtifact:
    """`RetentionCohortArtifact` v1 for one cohort vintage. Every text is a server field."""

    period: CalendarPeriod
    markets: tuple[RetentionMarket, ...]

    def __post_init__(self) -> None:
        if self.period.grain is not TimeGrain.CUSTOM:
            raise ValueError("a Retention artifact reads one cohort vintage.")
        keys = tuple(market.marketplace_key for market in self.markets)
        if len(set(keys)) != len(keys):
            raise ValueError("a Retention artifact carries one section set per marketplace.")
        if len(self.markets) >= _SEVERAL_MARKETPLACES and any(
            market.marketplace_label is None for market in self.markets
        ):
            raise ValueError("with two marketplaces each market names its marketplace.")

    @property
    def outcome(self) -> RetentionOutcome:
        """The page entry's declared outcome for this vintage."""
        outcomes = {market.outcome for market in self.markets}
        if RetentionOutcome.COMPLETE in outcomes:
            return RetentionOutcome.COMPLETE
        if RetentionOutcome.INSUFFICIENT in outcomes:
            return RetentionOutcome.INSUFFICIENT
        return RetentionOutcome.EMPTY

    @property
    def vintage_label(self) -> str:
        """'September 2026': the vintage's label."""
        return month_label(self.period.as_of_month)

    @property
    def page_context_label(self) -> str:
        """The assistant chip's label (ADR-120)."""
        return f"Retention · cohorts as of {self.vintage_label}"

    @property
    def starter_questions(self) -> tuple[str, ...]:
        """Starter questions name the vintage and never claim a cause."""
        return (
            f"Which acquisition group's cohorts bought again most by month 3, as of "
            f"{self.vintage_label}?",
        )


def scope_band(period: CalendarPeriod, grain: CohortGrain) -> str | None:
    """Return the scope band's text, or None when the vintage serves no cohort.

    'Cohorts as of {vintage}, grouped by {month or quarter}: acquired {first} to {last}, with
    repeat {months or quarters} through {last}.' (Retention LLD "Labels").
    """
    cohorts = vintage_cohorts(period, grain)
    if not cohorts:
        return None
    vintage = month_label(period.as_of_month)
    if grain is CohortGrain.MONTH:
        return (
            f"Cohorts as of {vintage}, grouped by month: acquired {month_label(cohorts[0].start)} "
            f"to {month_label(cohorts[-1].start)}, with repeat months through {vintage}."
        )
    ended = max(_quarter_after(cohort.start, max(cohort.offsets)) for cohort in cohorts)
    return (
        f"Cohorts as of {vintage}, grouped by quarter: acquired {quarter_label(cohorts[0].start)} "
        f"to {quarter_label(cohorts[-1].start)}, with repeat quarters through "
        f"{quarter_label(ended)}."
    )


def _quarter_after(start: date, quarters: int) -> date:
    index = start.year * 12 + start.month - 1 + quarters * QUARTER_MONTHS
    return date(index // 12, index % 12 + 1, 1)


def grain_note(market: RetentionMarket) -> str | None:
    """Return the quarterly note under the scope band, on every quarterly vintage."""
    if market.cohort_grain is not CohortGrain.QUARTER or market.first_quarterly is None:
        return None
    return QUARTERLY_NOTE.format(quarter=quarter_label(market.first_quarterly))


def _figure(figure: RetentionFigure | None) -> dict[str, str] | None:
    return None if figure is None else {"value": f"{figure.value:f}", "text": figure.text}


def _grid_payload(grids: tuple[RetentionGroupGrid, ...]) -> list[dict[str, object]]:
    return [
        {
            "group": grid.group,
            "label": grid.label,
            "grid_label": grid.grid_label,
            "rows": [
                {
                    "cohort": row.cohort.isoformat(),
                    "label": row.label,
                    "absent_text": row.absent_text,
                    "cells": [
                        {
                            "offset": cell.offset,
                            "column_label": cell.column_label,
                            "state": cell.state.value,
                            "rate": _figure(cell.rate),
                            "cohort_size": _figure(cell.cohort_size),
                            "text": cell.text,
                        }
                        for cell in row.cells
                    ],
                }
                for row in grid.rows
            ],
        }
        for grid in grids
    ]


def _marketplace_text(artifact: RetentionCohortArtifact, market: RetentionMarket) -> str | None:
    """Name a market's sections when the page shows two or more, else none (as New customers).

    With one marketplace the page reads as the approved mockups; with two, every section of a
    market says which marketplace it measures, so no two landmarks or headings share a name.
    """
    if len(artifact.markets) < _SEVERAL_MARKETPLACES or market.marketplace_label is None:
        return None
    return f"{market.marketplace_label} marketplace"


def _market_payload(
    artifact: RetentionCohortArtifact, market: RetentionMarket
) -> dict[str, object]:
    period = artifact.period
    grain = market.cohort_grain
    return {
        "marketplace_key": market.marketplace_key,
        "marketplace_text": _marketplace_text(artifact, market),
        "outcome": market.outcome.value,
        "absence_text": {
            RetentionOutcome.COMPLETE: None,
            RetentionOutcome.EMPTY: EMPTY_MARKET_TEXT,
            RetentionOutcome.INSUFFICIENT: INSUFFICIENT_MARKET_TEXT,
        }[market.outcome],
        "cohort_grain": None if grain is None else grain.value,
        "scope_band": None if grain is None else scope_band(period, grain),
        "grain_note": grain_note(market),
        "path": {
            "standard": {"available": market.cohort_grain is not None},
            "paid": {
                "state": market.paid_state.value,
                "callout": PAID_CALLOUT
                if market.paid_state is PaidPathState.NOT_OBSERVED
                else None,
            },
        },
        "triangle": {
            "legend": WITHHELD_CELL_LEGEND,
            "identity_caveat": IDENTITY_CAVEAT,
            "standard": _grid_payload(market.standard),
            "paid": _grid_payload(market.paid),
        },
        "curve": {
            "label": CURVE_LABEL,
            "metric": "cumulative_repeat_rate_account_ids",
            "lines": [
                {
                    "group": line.group,
                    "label": line.label,
                    "points": [
                        {
                            "offset": point.offset,
                            "label": point.label,
                            "rate": _figure(point.rate),
                            "text": point.text,
                        }
                        for point in line.points
                    ],
                }
                for line in market.curve
            ],
        },
        "insights": insight_slots_payload(market.insights),
    }


def _evidence_status(artifact: RetentionCohortArtifact) -> dict[str, object]:
    """`EvidenceStatus.v2`: a marketplace with no standard delivery for the vintage."""
    slots: list[dict[str, object]] = [
        {
            "marketplace_key": market.marketplace_key,
            "slot_label": "Cohorts",
            "role_label": "This vintage",
            "missing_dates": [
                {"start": artifact.period.start.isoformat(), "end": artifact.period.end.isoformat()}
            ],
            "reason_text": EMPTY_MARKET_TEXT,
            "recovery_text": None,
        }
        for market in artifact.markets
        if market.cohort_grain is None
    ]
    return {"version": 2, "slots": slots}


def retention_artifact_payload(artifact: RetentionCohortArtifact) -> dict[str, object]:
    """Serialize the artifact for the baked body; the UI's strict v1 decoder reads exactly this."""
    period = artifact.period
    return {
        "type": RETENTION_ARTIFACT_TYPE,
        "version": RETENTION_ARTIFACT_VERSION,
        "outcome": artifact.outcome.value,
        "vintage": {
            "as_of_month": period.as_of_month.isoformat()[:7],
            "label": artifact.vintage_label,
            "start": period.start.isoformat(),
            "end": period.end.isoformat(),
        },
        "markets": [_market_payload(artifact, market) for market in artifact.markets],
        "boundary": list(BOUNDARY),
        "evidence_status": _evidence_status(artifact),
        "page_context_label": artifact.page_context_label,
        "page_context_expires_at": None,
        "starter_questions": list(artifact.starter_questions),
    }


_ARTIFACT_FIELDS: Final = {
    "boundary",
    "evidence_status",
    "markets",
    "outcome",
    "page_context_expires_at",
    "page_context_label",
    "starter_questions",
    "type",
    "version",
    "vintage",
}
_MARKET_FIELDS: Final = {
    "absence_text",
    "cohort_grain",
    "curve",
    "grain_note",
    "insights",
    "marketplace_key",
    "marketplace_text",
    "outcome",
    "path",
    "scope_band",
    "triangle",
}
_INSIGHT_BASE_FIELDS: Final = {
    "artifact_revision",
    "evidence_line",
    "id",
    "output_class",
    "prompt_version",
    "state",
    "text",
}


def _payload_object(value: object, fields: set[str], label: str) -> dict[str, object]:
    if type(value) is not dict or set(cast("dict[object, object]", value)) != fields:
        raise ValueError(f"{label} fields are invalid.")
    return cast("dict[str, object]", value)


def _payload_array(value: object, label: str) -> list[object]:
    if type(value) is not list:
        raise ValueError(f"{label} must be an array.")
    return cast("list[object]", value)


def _payload_text(value: object, label: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{label} must be nonblank text.")
    return value


def _payload_date(value: object, label: str) -> date:
    raw = _payload_text(value, label)
    parsed = date.fromisoformat(raw)
    if parsed.isoformat() != raw:
        raise ValueError(f"{label} must be an ISO date.")
    return parsed


def _payload_figure(value: object, label: str) -> tuple[str, str]:
    item = _payload_object(value, {"text", "value"}, label)
    raw = _payload_text(item["value"], f"{label} value")
    try:
        finite = Decimal(raw).is_finite()
    except InvalidOperation as error:
        raise ValueError(f"{label} value must be decimal text.") from error
    if not finite:
        raise ValueError(f"{label} value must be finite.")
    return raw, _payload_text(item["text"], f"{label} text")


def _require_insight_payload(value: object) -> None:
    if type(value) is not dict:
        raise ValueError("retention insight must be an object.")
    raw = cast("dict[str, object]", value)
    state = raw.get("state")
    fields: set[str] = set(_INSIGHT_BASE_FIELDS)
    if state == "silent":
        fields.update({"absence_class", "absence_reason"})
    item = _payload_object(raw, fields, "retention insight")
    if item["id"] != "retention-shift" or item["output_class"] != "perspective":
        raise ValueError("retention insight identity is invalid.")
    _payload_text(item["artifact_revision"], "retention insight revision")
    _payload_text(item["prompt_version"], "retention insight prompt version")
    if state == "filled":
        _payload_text(item["text"], "retention insight text")
        _payload_text(item["evidence_line"], "retention insight evidence")
    elif state == "silent":
        if item["text"] is not None or item["evidence_line"] is not None:
            raise ValueError("a silent retention insight carries no display text.")
        _payload_text(item["absence_class"], "retention insight absence class")
        _payload_text(item["absence_reason"], "retention insight absence reason")
    else:
        raise ValueError("retention insight state is invalid.")


def _require_cell_payload(  # noqa: PLR0913, PLR0917 -- one cell plus its roster context
    value: object, offset: int, grain: CohortGrain, path: str
) -> tuple[str, str] | None:
    item = _payload_object(
        value,
        {"cohort_size", "column_label", "offset", "rate", "state", "text"},
        "retention cell",
    )
    if item["offset"] != offset or item["column_label"] != _column_label(grain, offset):
        raise ValueError("retention cell follow-up roster is invalid.")
    state = item["state"]
    if state == CohortCellState.VALUE.value:
        _payload_figure(item["rate"], "retention cell rate")
        cohort_size = _payload_figure(item["cohort_size"], "retention cell cohort size")
        if item["text"] is not None:
            raise ValueError("a retention value cell carries no status text.")
        return cohort_size
    allowed = {
        CohortCellState.NOT_RETURNED.value,
        CohortCellState.NOT_YET.value,
        *(() if path == STANDARD_PATH else (CohortCellState.WITHHELD.value,)),
    }
    if state not in allowed or item["rate"] is not None or item["cohort_size"] is not None:
        raise ValueError("retention cell state is invalid for its path.")
    _payload_text(item["text"], "retention cell status text")
    return None


def _require_grid_payloads(  # noqa: PLR0912 -- closed row/cell roster matrix
    value: object, grain: CohortGrain, path: str
) -> bool:
    grids = _payload_array(value, f"retention {path} grids")
    if len(grids) != len(ACQUISITION_GROUPS):
        raise ValueError("retention grids must carry every acquisition group.")
    expected_rows: tuple[str, ...] | None = None
    any_value = False
    for raw, (group, _label) in zip(grids, ACQUISITION_GROUPS, strict=True):
        grid = _payload_object(raw, {"grid_label", "group", "label", "rows"}, "retention grid")
        if grid["group"] != group:
            raise ValueError("retention grid group roster is invalid.")
        _payload_text(grid["label"], "retention grid label")
        _payload_text(grid["grid_label"], "retention grid description")
        rows = _payload_array(grid["rows"], "retention rows")
        cohorts: list[str] = []
        previous: date | None = None
        for raw_row in rows:
            row = _payload_object(
                raw_row, {"absent_text", "cells", "cohort", "label"}, "retention row"
            )
            cohort = _payload_date(row["cohort"], "retention cohort")
            if previous is not None and cohort <= previous:
                raise ValueError("retention cohort rows must be unique and oldest first.")
            previous = cohort
            cohorts.append(cohort.isoformat())
            _payload_text(row["label"], "retention row label")
            cells = _payload_array(row["cells"], "retention cells")
            if row["absent_text"] is not None:
                _payload_text(row["absent_text"], "retention absent cohort text")
                if cells:
                    raise ValueError("an absent retention cohort carries no cells.")
                continue
            if len(cells) != _FOLLOW_UPS[grain]:
                raise ValueError("retention row follow-up roster does not match its grain.")
            cohort_sizes: set[tuple[str, str]] = set()
            for offset, cell in enumerate(cells, start=1):
                cohort_size = _require_cell_payload(cell, offset, grain, path)
                if cohort_size is not None:
                    cohort_sizes.add(cohort_size)
                    any_value = True
            if len(cohort_sizes) > 1:
                raise ValueError("retention row cohort-size figures must match.")
        roster = tuple(cohorts)
        if expected_rows is None:
            expected_rows = roster
        elif roster != expected_rows:
            raise ValueError("retention grids must carry the same cohort roster.")
    return any_value


def _require_curve_payload(value: object, grain: CohortGrain | None) -> None:
    curve = _payload_object(value, {"label", "lines", "metric"}, "retention curve")
    _payload_text(curve["label"], "retention curve label")
    if curve["metric"] != "cumulative_repeat_rate_account_ids":
        raise ValueError("retention curve metric is invalid.")
    lines = _payload_array(curve["lines"], "retention curve lines")
    if grain is None:
        if lines:
            raise ValueError("an empty retention market carries no curve.")
        return
    if len(lines) != len(ACQUISITION_GROUPS):
        raise ValueError("retention curve must carry every acquisition group.")
    for raw, (group, _label) in zip(lines, ACQUISITION_GROUPS, strict=True):
        line = _payload_object(raw, {"group", "label", "points"}, "retention curve line")
        if line["group"] != group:
            raise ValueError("retention curve group roster is invalid.")
        _payload_text(line["label"], "retention curve line label")
        points = _payload_array(line["points"], "retention curve points")
        if len(points) != _FOLLOW_UPS[grain]:
            raise ValueError("retention curve follow-up roster does not match its grain.")
        for offset, raw_point in enumerate(points, start=1):
            point = _payload_object(
                raw_point, {"label", "offset", "rate", "text"}, "retention curve point"
            )
            if point["offset"] != offset or point["label"] != _column_label(grain, offset):
                raise ValueError("retention curve follow-up roster is invalid.")
            if point["rate"] is None:
                _payload_text(point["text"], "retention curve status text")
            else:
                _payload_figure(point["rate"], "retention curve rate")
                if point["text"] is not None:
                    raise ValueError("a retention curve value carries no status text.")


def _require_market_payload(  # noqa: PLR0912 -- validates the closed market state matrix
    value: object,
) -> tuple[str, RetentionOutcome, bool, str | None]:
    item = _payload_object(value, _MARKET_FIELDS, "retention market")
    marketplace = _payload_text(item["marketplace_key"], "retention marketplace")
    marketplace_text = (
        None
        if item["marketplace_text"] is None
        else _payload_text(item["marketplace_text"], "retention marketplace text")
    )
    try:
        outcome = RetentionOutcome(item["outcome"])
        grain = None if item["cohort_grain"] is None else CohortGrain(item["cohort_grain"])
    except TypeError, ValueError:
        raise ValueError("retention market outcome or grain is invalid.") from None
    absence = (
        None
        if item["absence_text"] is None
        else _payload_text(item["absence_text"], "retention market absence text")
    )
    expected_absence = {
        RetentionOutcome.COMPLETE: None,
        RetentionOutcome.EMPTY: EMPTY_MARKET_TEXT,
        RetentionOutcome.INSUFFICIENT: INSUFFICIENT_MARKET_TEXT,
    }[outcome]
    if absence != expected_absence:
        raise ValueError("retention market absence text does not match its outcome.")
    scope = (
        None
        if item["scope_band"] is None
        else _payload_text(item["scope_band"], "retention scope band")
    )
    note = (
        None
        if item["grain_note"] is None
        else _payload_text(item["grain_note"], "retention grain note")
    )
    if (grain is None) != (scope is None) or (note is not None) != (grain is CohortGrain.QUARTER):
        raise ValueError("retention grain text is invalid.")
    path = _payload_object(item["path"], {"paid", "standard"}, "retention path")
    standard = _payload_object(path["standard"], {"available"}, "retention standard path")
    if type(standard["available"]) is not bool or standard["available"] != (grain is not None):
        raise ValueError("retention standard path availability is invalid.")
    paid = _payload_object(path["paid"], {"callout", "state"}, "retention paid path")
    if paid["state"] not in {state.value for state in PaidPathState}:
        raise ValueError("retention paid path state is invalid.")
    expected_callout = PAID_CALLOUT if paid["state"] == PaidPathState.NOT_OBSERVED.value else None
    if paid["callout"] != expected_callout:
        raise ValueError("retention paid path callout is invalid.")
    triangle = _payload_object(
        item["triangle"], {"identity_caveat", "legend", "paid", "standard"}, "retention triangle"
    )
    _payload_text(triangle["identity_caveat"], "retention identity caveat")
    _payload_text(triangle["legend"], "retention legend")
    if grain is None:
        if _payload_array(triangle["standard"], "retention standard grids") or _payload_array(
            triangle["paid"], "retention paid grids"
        ):
            raise ValueError("an empty retention market carries no grids.")
        any_value = False
    else:
        any_value = _require_grid_payloads(triangle["standard"], grain, STANDARD_PATH)
        paid_expected = paid["state"] == PaidPathState.OBSERVED.value
        if paid_expected:
            _require_grid_payloads(triangle["paid"], grain, ARP_PATH)
        elif _payload_array(triangle["paid"], "retention paid grids"):
            raise ValueError("an unavailable retention paid path carries no grids.")
    derived = (
        RetentionOutcome.EMPTY
        if grain is None
        else RetentionOutcome.COMPLETE
        if any_value
        else RetentionOutcome.INSUFFICIENT
    )
    if outcome is not derived:
        raise ValueError("retention market outcome does not match its standard evidence.")
    _require_curve_payload(item["curve"], grain)
    insights = _payload_array(item["insights"], "retention insights")
    if len(insights) != 1:
        raise ValueError("retention insight roster is invalid.")
    _require_insight_payload(insights[0])
    return marketplace, outcome, grain is None, marketplace_text


def _require_marketplace_texts(texts: list[str | None]) -> None:
    """One market reads as the mockups; two or more each name a distinct marketplace."""
    several = len(texts) >= _SEVERAL_MARKETPLACES
    if (any(text is None for text in texts) if several else texts != [None]) or len(
        set(texts)
    ) != len(texts):
        raise ValueError("retention marketplace texts do not match the market count.")


def require_retention_artifact_payload(  # noqa: PLR0912 -- validates the full wire matrix
    value: object,
) -> dict[str, object]:
    """Refuse a retained Retention body outside the encoder's full semantic matrix."""
    item = _payload_object(value, _ARTIFACT_FIELDS, "retention artifact")
    if item["type"] != RETENTION_ARTIFACT_TYPE or item["version"] != RETENTION_ARTIFACT_VERSION:
        raise ValueError("retention artifact identity is invalid.")
    vintage = _payload_object(
        item["vintage"], {"as_of_month", "end", "label", "start"}, "retention vintage"
    )
    month = _payload_text(vintage["as_of_month"], "retention vintage month")
    if (
        len(month) != 7  # noqa: PLR2004 -- canonical YYYY-MM width
        or month[4] != "-"
        or not month[:4].isdigit()
        or month[5:] not in {f"{value:02d}" for value in range(1, 13)}
    ):
        raise ValueError("retention vintage month must be YYYY-MM.")
    start = _payload_date(vintage["start"], "retention vintage start")
    end = _payload_date(vintage["end"], "retention vintage end")
    if end < start or end.isoformat()[:7] != month:
        raise ValueError("retention vintage window is invalid.")
    _payload_text(vintage["label"], "retention vintage label")
    markets = [
        _require_market_payload(raw) for raw in _payload_array(item["markets"], "retention markets")
    ]
    if not markets or len({marketplace for marketplace, *_rest in markets}) != len(markets):
        raise ValueError("retention markets must be nonempty and unique.")
    _require_marketplace_texts([text for *_rest, text in markets])
    outcomes = {outcome for _marketplace, outcome, _missing, _text in markets}
    derived = (
        RetentionOutcome.COMPLETE
        if RetentionOutcome.COMPLETE in outcomes
        else RetentionOutcome.INSUFFICIENT
        if RetentionOutcome.INSUFFICIENT in outcomes
        else RetentionOutcome.EMPTY
    )
    if item["outcome"] != derived.value:
        raise ValueError("retention artifact outcome does not match its markets.")
    boundary = _payload_array(item["boundary"], "retention boundary")
    if len(boundary) != len(BOUNDARY):
        raise ValueError("retention boundary roster is invalid.")
    for text in boundary:
        _payload_text(text, "retention boundary text")
    evidence = _payload_object(item["evidence_status"], {"slots", "version"}, "retention evidence")
    if evidence["version"] != 2:  # noqa: PLR2004 -- EvidenceStatus.v2 wire literal
        raise ValueError("retention evidence version is invalid.")
    slots = _payload_array(evidence["slots"], "retention evidence slots")
    missing_markets = [marketplace for marketplace, _outcome, missing, _text in markets if missing]
    if len(slots) != len(missing_markets):
        raise ValueError("retention evidence slots do not match missing markets.")
    for raw, marketplace in zip(slots, missing_markets, strict=True):
        slot = _payload_object(
            raw,
            {
                "marketplace_key",
                "missing_dates",
                "reason_text",
                "recovery_text",
                "role_label",
                "slot_label",
            },
            "retention evidence slot",
        )
        if (
            slot["marketplace_key"] != marketplace
            or slot["slot_label"] != "Cohorts"
            or slot["role_label"] != "This vintage"
            or slot["reason_text"] != EMPTY_MARKET_TEXT
            or slot["recovery_text"] is not None
        ):
            raise ValueError("retention evidence slot is invalid.")
        dates = _payload_array(slot["missing_dates"], "retention missing dates")
        if len(dates) != 1:
            raise ValueError("retention evidence slot requires one missing range.")
        missing = _payload_object(dates[0], {"end", "start"}, "retention missing range")
        if missing != {"start": start.isoformat(), "end": end.isoformat()}:
            raise ValueError("retention evidence range does not match its vintage.")
    _payload_text(item["page_context_label"], "retention page context label")
    if item["page_context_expires_at"] is not None:
        raise ValueError("a baked retention artifact carries no page-context expiry.")
    starters = _payload_array(item["starter_questions"], "retention starter questions")
    if len(starters) != 1:
        raise ValueError("a baked retention artifact carries its one governed starter.")
    _payload_text(starters[0], "retention starter question")
    return item
