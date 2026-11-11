# SPDX-License-Identifier: MIT-0
"""Weekly series from the staged weekly aggregates: the three-state rule (design §3b, §3d).

Adapted from the source engine's series builder; its promotion plan and filled-week invariant
are removed, and missing days get the three-state rule instead of a fill.

A day is complete for the set when a pinned sales publication covers it (traffic publications
for glance views). A row exists only on a covered day, because each row comes from the
publication of its own day. So, per ASIN, target and week:

- `observed` days = rows with a non-null value;
- `unknown` days = rows with a null value, plus uncovered days, plus (when the ASIN is inactive
  that week) covered days with no row;
- `inferred_zero` days = covered days with no row while the ASIN is active.

A week with any unknown day is `unknown`; all seven inferred-zero is `inferred_zero` (value 0);
otherwise `observed` (the sum of its observed days). The ASIN is **active** at week w when it has
an observed day for any target in the `activity_window_weeks` weeks ending w.

A **set gap** is a week with an uncovered sales day (unknown for every ASIN); a **product gap**
is an unknown week of one series whose days are all covered. Only a product-gap run longer than
`max_unknown_run_weeks` restarts a series' context; set-gap weeks inside a run neither count nor
break it (R3-04).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum

from domain.forecasting.boundary import (
    IntervalV1,
    WeeklyTargetAggregateRowV1,
    WeeklyTrafficAggregateRowV1,
)
from domain.forecasting.models import WeekState
from domain.forecasting.policy import MONEY_TARGETS, SUNDAY, TARGETS, MissingWeekPolicy

DAYS_PER_WEEK = 7
_DAY = timedelta(days=1)
_WEEK = timedelta(days=DAYS_PER_WEEK)


# ---- days ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class DayCounts:
    """One (ASIN, target, week)'s aggregate counts and the week's covered day count."""

    days_with_row: int
    days_with_null: int
    complete_days: int

    def __post_init__(self) -> None:
        if (
            not 0
            <= self.days_with_null
            <= self.days_with_row
            <= self.complete_days
            <= DAYS_PER_WEEK
        ):
            raise ValueError("expected 0 <= days_with_null <= days_with_row <= complete_days <= 7")


@dataclass(frozen=True)
class DayStates:
    """How many of a week's seven days are in each state."""

    observed: int
    inferred_zero: int
    unknown: int


def classify_days(counts: DayCounts, *, active: bool) -> DayStates:
    """Apply the day-state table of design §3b to one week's counts."""
    observed = counts.days_with_row - counts.days_with_null
    no_row_covered = counts.complete_days - counts.days_with_row
    inferred_zero = no_row_covered if active else 0
    unknown = DAYS_PER_WEEK - observed - inferred_zero
    return DayStates(observed, inferred_zero, unknown)


@dataclass(frozen=True)
class WeekCell:
    """One series week: its state, its value (None when unknown) and its inferred-zero days."""

    state: WeekState
    value: Decimal | None
    inferred_zero_days: int


def roll_up_weeks(days: DayStates, value_sum: Decimal | None) -> WeekCell:
    """Roll one week's day states up to a week state and value (design §3b week roll-up)."""
    if days.unknown:
        return WeekCell(WeekState.UNKNOWN, None, days.inferred_zero)
    if days.observed == 0:
        return WeekCell(WeekState.INFERRED_ZERO, Decimal(0), days.inferred_zero)
    return WeekCell(WeekState.OBSERVED, value_sum or Decimal(0), days.inferred_zero)


def is_active(observed_days: Sequence[int], week: int, window_weeks: int) -> bool:
    """Whether any of the `window_weeks` weeks ending `week` holds an observed day (any target)."""
    start = max(0, week - window_weeks + 1)
    return any(observed_days[index] > 0 for index in range(start, week + 1))


# ---- gaps and context ---------------------------------------------------------------------


class GapKind(StrEnum):
    """Whether a week is a gap, and whose."""

    NONE = "none"
    SET_GAP = "set_gap"
    PRODUCT_GAP = "product_gap"


def classify_gap_weeks(
    states: Sequence[WeekState], set_gaps: Sequence[bool]
) -> tuple[GapKind, ...]:
    """Mark set-gap weeks, other unknown weeks as product gaps, and the rest as none."""
    return tuple(
        GapKind.SET_GAP
        if set_gap
        else GapKind.PRODUCT_GAP
        if state is WeekState.UNKNOWN
        else GapKind.NONE
        for state, set_gap in zip(states, set_gaps, strict=True)
    )


def model_context(
    states: Sequence[WeekState], gaps: Sequence[GapKind], max_unknown_run_weeks: int
) -> int:
    """Return the first week of the series' context; `len(states)` means an empty one (§3d).

    The context starts at the first observed week, and after the most recent product-gap run of
    more than `max_unknown_run_weeks` product-gap weeks. Set-gap weeks inside a run neither
    count nor end it; a known week ends it.
    """
    first_observed = next(
        (week for week, state in enumerate(states) if state is WeekState.OBSERVED), len(states)
    )
    start = first_observed
    run = 0
    for week, gap in enumerate(gaps):
        if gap is GapKind.PRODUCT_GAP:
            run += 1
            if run > max_unknown_run_weeks:
                start = max(start, week + 1)
        elif gap is GapKind.NONE:
            run = 0
    if start < len(states):
        # The context restarts after the run, at its first observed week.
        start = next(
            (week for week in range(start, len(states)) if states[week] is WeekState.OBSERVED),
            len(states),
        )
    return start


# ---- the whole shard ----------------------------------------------------------------------


@dataclass(frozen=True)
class TargetWeeks:
    """One target's weeks for one ASIN, oldest first, and its context start."""

    target: str
    weeks: tuple[WeekCell, ...]
    gaps: tuple[GapKind, ...]
    context_start: int
    currency_code: str | None
    currency_mixed: bool


@dataclass(frozen=True)
class SeriesWeeks:
    """One ASIN's four targets and its glance-views covariate on the shared week grid."""

    canonical_asin: str
    targets: dict[str, TargetWeeks]
    covariate: tuple[float | None, ...]


@dataclass(frozen=True)
class WeeklySeriesRequest:
    """One shard's aggregates, the coverage calendar and the window they sit in."""

    input_end: date
    history_weeks: int
    sales_coverage: tuple[IntervalV1, ...]
    traffic_coverage: tuple[IntervalV1, ...]
    target_rows: tuple[WeeklyTargetAggregateRowV1, ...]
    traffic_rows: tuple[WeeklyTrafficAggregateRowV1, ...]
    missing_weeks: MissingWeekPolicy

    def __post_init__(self) -> None:
        if (self.input_end.weekday() + 1) % DAYS_PER_WEEK != SUNDAY:
            raise ValueError("input_end must be a Saturday, the last day of a week")
        if self.history_weeks < 1:
            raise ValueError("history_weeks must be at least 1")


@dataclass(frozen=True)
class WeeklySeriesResult:
    """Every ASIN of the shard, sorted by canonical ASIN, on one week grid."""

    week_starts: tuple[date, ...]
    set_gaps: tuple[bool, ...]
    series: tuple[SeriesWeeks, ...]


def week_grid(input_end: date, history_weeks: int) -> tuple[date, ...]:
    """Return the Sunday starts of the `history_weeks` weeks ending `input_end`, oldest first."""
    last = input_end - timedelta(days=DAYS_PER_WEEK - 1)
    return tuple(last - _WEEK * (history_weeks - 1 - index) for index in range(history_weeks))


def _covered_days(intervals: Iterable[IntervalV1], starts: Sequence[date]) -> tuple[int, ...]:
    days: set[date] = set()
    first, last = starts[0], starts[-1] + _WEEK - _DAY
    for interval in intervals:
        day = max(interval.start, first)
        while day <= min(interval.end, last):
            days.add(day)
            day += _DAY
    return tuple(
        sum(1 for offset in range(DAYS_PER_WEEK) if start + _DAY * offset in days)
        for start in starts
    )


def _target_weeks(  # noqa: PLR0913 -- one ASIN's rows and the shared grid
    target: str,
    *,
    rows: dict[int, WeeklyTargetAggregateRowV1],
    covered: Sequence[int],
    active: Sequence[bool],
    set_gaps: Sequence[bool],
    policy: MissingWeekPolicy,
) -> TargetWeeks:
    cells: list[WeekCell] = []
    currencies: set[str] = set()
    for week, complete in enumerate(covered):
        row = rows.get(week)
        counts = DayCounts(
            row.days_with_row if row else 0, row.days_with_null if row else 0, complete
        )
        cells.append(
            roll_up_weeks(classify_days(counts, active=active[week]), row and row.value_sum)
        )
        if row is not None and target in MONEY_TARGETS:
            currencies.update(code for code in (row.min_currency, row.max_currency) if code)
    states = [cell.state for cell in cells]
    gaps = classify_gap_weeks(states, set_gaps)
    return TargetWeeks(
        target=target,
        weeks=tuple(cells),
        gaps=gaps,
        context_start=model_context(states, gaps, policy.max_unknown_run_weeks),
        currency_code=next(iter(currencies)) if len(currencies) == 1 else None,
        currency_mixed=len(currencies) > 1,
    )


def build_weekly_series(request: WeeklySeriesRequest) -> WeeklySeriesResult:
    """Classify every ASIN's weeks for the four targets and glance views (design §3b, §3d)."""
    starts = week_grid(request.input_end, request.history_weeks)
    index = {start: week for week, start in enumerate(starts)}
    sales_covered = _covered_days(request.sales_coverage, starts)
    traffic_covered = _covered_days(request.traffic_coverage, starts)
    set_gaps = tuple(days < DAYS_PER_WEEK for days in sales_covered)
    by_asin: dict[str, dict[str, dict[int, WeeklyTargetAggregateRowV1]]] = {}
    for row in request.target_rows:
        week = index.get(row.week_start)
        if week is None:
            continue
        cell = by_asin.setdefault(row.canonical_asin, {}).setdefault(row.target, {})
        if week in cell:
            raise ValueError(f"{row.canonical_asin}: two {row.target} rows for {row.week_start}")
        cell[week] = row
    traffic: dict[str, dict[int, WeeklyTrafficAggregateRowV1]] = {}
    for row in request.traffic_rows:
        week = index.get(row.week_start)
        if week is not None:
            traffic.setdefault(row.canonical_asin, {})[week] = row
    window = request.missing_weeks.activity_window_weeks
    series: list[SeriesWeeks] = []
    for asin in sorted(by_asin):
        targets_rows = by_asin[asin]
        observed_days = [
            sum(
                (rows[week].days_with_row - rows[week].days_with_null) if week in rows else 0
                for rows in targets_rows.values()
            )
            for week in range(len(starts))
        ]
        active = [is_active(observed_days, week, window) for week in range(len(starts))]
        targets = {
            target: _target_weeks(
                target,
                rows=targets_rows.get(target, {}),
                covered=sales_covered,
                active=active,
                set_gaps=set_gaps,
                policy=request.missing_weeks,
            )
            for target in TARGETS
        }
        covariate = _covariate(traffic.get(asin, {}), traffic_covered, active)
        series.append(SeriesWeeks(asin, targets, covariate))
    return WeeklySeriesResult(starts, set_gaps, tuple(series))


def _covariate(
    rows: dict[int, WeeklyTrafficAggregateRowV1], covered: Sequence[int], active: Sequence[bool]
) -> tuple[float | None, ...]:
    """Glance views per week: known only on a fully covered week; no row while active is 0."""
    values: list[float | None] = []
    for week, complete in enumerate(covered):
        row = rows.get(week)
        if complete < DAYS_PER_WEEK:
            values.append(None)
        elif row is None or row.days_with_traffic_row == 0:
            values.append(0.0 if active[week] else None)
        elif row.glance_views_sum is None or (
            row.days_with_traffic_row < DAYS_PER_WEEK and not active[week]
        ):
            values.append(None)
        else:
            values.append(float(row.glance_views_sum))
    return tuple(values)
