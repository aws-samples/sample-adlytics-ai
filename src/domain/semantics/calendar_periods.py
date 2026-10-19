# SPDX-License-Identifier: MIT-0
"""Complete calendar periods: ISO weeks, calendar months and calendar quarters, all in UTC.

ADR-100. A cohort vintage (`TimeGrain.CUSTOM`, ADR-104) is the fourth periodic grain: the 13 whole
calendar months a cohort pack reads, ending on the last day of its `as_of_month`. Vintages overlap
(each starts one month after the previous one), so a vintage's previous period is the vintage one
`as_of_month` earlier, never the one ending the day before it starts. A shopper count exists only
for the exact period AMC counted it over, so every periodic figure is keyed on one of these, never
on a run of days. This module is the one home of the
period arithmetic: alignment, the period containing a day, the previous period, and the
server-built label a page and an answer show. Pure: no clock, no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from domain.semantics.time import TimeGrain
from domain.shared_kernel.values import DateRange

__all__ = [
    "CALENDAR_GRAINS",
    "PERIODIC_GRAINS",
    "VINTAGE_MONTHS",
    "CalendarPeriod",
    "CalendarPeriodError",
    "day_label",
    "grain_list_text",
    "grain_noun",
    "is_complete_period",
    "latest_complete_period",
    "longest_grain_days",
    "period_containing",
    "shift_months",
    "vintage_ending",
]

CALENDAR_GRAINS = (TimeGrain.WEEK, TimeGrain.MONTH, TimeGrain.QUARTER)
"""The grains a periodic fact may be measured at, in canonical order (ADR-100, ADR-110)."""
PERIODIC_GRAINS = (*CALENDAR_GRAINS, TimeGrain.CUSTOM)
"""Every periodic grain: the calendar grains, then the cohort vintage (ADR-104)."""
VINTAGE_MONTHS = 13
"""A cohort vintage's whole calendar months: AMC's 13-month rolling retention (design §6.6)."""

_MONTHS_PER_QUARTER = 3
_DECEMBER = 12
_WEEK_DAYS = 7
_MONTH_ABBREVIATIONS = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)  # fmt: skip
_MONTH_NAMES = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)  # fmt: skip
_EN_DASH = "\u2013"
_NOUNS = {
    TimeGrain.WEEK: "week",
    TimeGrain.MONTH: "month",
    TimeGrain.QUARTER: "quarter",
    TimeGrain.CUSTOM: "cohort vintage",
}


class CalendarPeriodError(ValueError):
    """A period isn't one whole ISO week, calendar month, calendar quarter or cohort vintage."""


def _require_calendar_grain(grain: TimeGrain) -> None:
    if grain not in PERIODIC_GRAINS:
        raise CalendarPeriodError(
            f"{grain.value} isn't a calendar grain; use week, month, quarter or custom (a cohort "
            "vintage)."
        )


def shift_months(month_start: date, months: int) -> date:
    """Return the first day of the month `months` after (or before, when negative) a month."""
    index = month_start.year * 12 + month_start.month - 1 + months
    return date(index // 12, index % 12 + 1, 1)


def _month_end(start: date) -> date:
    following = (
        date(start.year + 1, 1, 1)
        if start.month == _DECEMBER
        else date(start.year, start.month + 1, 1)
    )
    return following - timedelta(days=1)


def is_complete_period(grain: TimeGrain, start: date, end: date) -> bool:
    """Whether `[start, end]` (inclusive UTC days) is one whole period of `grain`.

    An ISO week runs Monday to Sunday; a month runs from its 1st to its last day; a quarter starts
    on 1 January, April, July or October and ends on the last day of its third month.
    """
    _require_calendar_grain(grain)
    if grain is TimeGrain.WEEK:
        return start.isoweekday() == 1 and end == start + timedelta(days=_WEEK_DAYS - 1)
    if start.day != 1:
        return False
    if grain is TimeGrain.MONTH:
        return end == _month_end(start)
    if grain is TimeGrain.CUSTOM:
        return end == _month_end(shift_months(start, VINTAGE_MONTHS - 1))
    if (start.month - 1) % _MONTHS_PER_QUARTER:
        return False
    return end == _month_end(date(start.year, start.month + _MONTHS_PER_QUARTER - 1, 1))


@dataclass(frozen=True, slots=True, order=True)
class CalendarPeriod:
    """One complete calendar period. Constructing an incomplete one is refused."""

    start: date
    end: date
    grain: TimeGrain

    def __post_init__(self) -> None:
        if not is_complete_period(self.grain, self.start, self.end):
            raise CalendarPeriodError(
                f"{self.start.isoformat()} to {self.end.isoformat()} isn't one complete "
                f"{grain_noun(self.grain)} (UTC)."
            )

    @property
    def window(self) -> DateRange:
        """The period's inclusive days."""
        return DateRange(self.start, self.end)

    def previous(self) -> CalendarPeriod:
        """Return the period of the same grain that ends the day before this one starts.

        A cohort vintage's previous period is the vintage one `as_of_month` earlier: vintages
        overlap, so the one ending the day before this starts is twelve vintages back.
        """
        if self.grain is TimeGrain.CUSTOM:
            return vintage_ending(shift_months(self.as_of_month, -1))
        return period_containing(self.grain, self.start - timedelta(days=1))

    @property
    def as_of_month(self) -> date:
        """A cohort vintage's `as_of_month`: the first day of its last month (ADR-104)."""
        if self.grain is not TimeGrain.CUSTOM:
            raise CalendarPeriodError(f"a {grain_noun(self.grain)} has no as_of_month.")
        return self.end.replace(day=1)

    @property
    def label(self) -> str:
        """The server-built name a page header and an answer show (ADR-100 point 6).

        Weeks name their dates ("Week of 15-21 Sep 2026", with an en dash), months their name
        ("September 2026"), quarters their number and months ("Q3 2026 (Jul-Sep)"). The browser
        never formats a period.
        """
        if self.grain is TimeGrain.WEEK:
            start, end = self.start, self.end
            end_text = f"{end.day} {_MONTH_ABBREVIATIONS[end.month - 1]} {end.year}"
            if start.year != end.year:
                start_text = f"{start.day} {_MONTH_ABBREVIATIONS[start.month - 1]} {start.year}"
            elif start.month != end.month:
                start_text = f"{start.day} {_MONTH_ABBREVIATIONS[start.month - 1]}"
            else:
                start_text = str(start.day)
            return f"Week of {start_text}{_EN_DASH}{end_text}"
        if self.grain is TimeGrain.MONTH:
            return f"{_MONTH_NAMES[self.start.month - 1]} {self.start.year}"
        if self.grain is TimeGrain.CUSTOM:
            return f"cohorts as of {_MONTH_NAMES[self.end.month - 1]} {self.end.year}"
        quarter = (self.start.month - 1) // _MONTHS_PER_QUARTER + 1
        first = _MONTH_ABBREVIATIONS[self.start.month - 1]
        last = _MONTH_ABBREVIATIONS[self.end.month - 1]
        return f"Q{quarter} {self.start.year} ({first}{_EN_DASH}{last})"


def period_containing(grain: TimeGrain, day: date) -> CalendarPeriod:
    """Return the complete period of `grain` that contains `day`."""
    _require_calendar_grain(grain)
    if grain is TimeGrain.WEEK:
        start = day - timedelta(days=day.weekday())
        return CalendarPeriod(start, start + timedelta(days=_WEEK_DAYS - 1), grain)
    if grain is TimeGrain.MONTH:
        start = day.replace(day=1)
        return CalendarPeriod(start, _month_end(start), grain)
    if grain is TimeGrain.CUSTOM:
        return vintage_ending(day.replace(day=1))
    first_month = (day.month - 1) // _MONTHS_PER_QUARTER * _MONTHS_PER_QUARTER + 1
    start = date(day.year, first_month, 1)
    last = date(day.year, first_month + _MONTHS_PER_QUARTER - 1, 1)
    return CalendarPeriod(start, _month_end(last), grain)


def vintage_ending(as_of_month: date) -> CalendarPeriod:
    """Return the cohort vintage whose `as_of_month` is the month starting `as_of_month`."""
    if as_of_month.day != 1:
        raise CalendarPeriodError(f"{as_of_month.isoformat()} isn't the first day of a month.")
    return CalendarPeriod(
        shift_months(as_of_month, -(VINTAGE_MONTHS - 1)), _month_end(as_of_month), TimeGrain.CUSTOM
    )


def latest_complete_period(grain: TimeGrain, on_or_before: date) -> CalendarPeriod:
    """Return the latest complete period of `grain` ending on or before `on_or_before`."""
    containing = period_containing(grain, on_or_before)
    return containing if containing.end == on_or_before else containing.previous()


def day_label(day: date) -> str:
    """Name one day the way period labels do: '16 Oct 2026'."""
    return f"{day.day} {_MONTH_ABBREVIATIONS[day.month - 1]} {day.year}"


def grain_noun(grain: TimeGrain, *, plural: bool = False) -> str:
    """'week', 'month' or 'quarter', optionally plural."""
    _require_calendar_grain(grain)
    noun = _NOUNS[grain]
    return f"{noun}s" if plural else noun


def grain_list_text(grains: tuple[TimeGrain, ...]) -> str:
    """'months and quarters', built from declared grains in canonical order, never typed."""
    if not grains:
        raise CalendarPeriodError("a grain list needs at least one grain.")
    nouns = [grain_noun(grain, plural=True) for grain in PERIODIC_GRAINS if grain in grains]
    if len(nouns) == 1:
        return nouns[0]
    return f"{', '.join(nouns[:-1])} and {nouns[-1]}"


_LONGEST_FIXED_GRAIN_DAYS = {
    TimeGrain.DAY: 1,
    TimeGrain.WEEK: _WEEK_DAYS,
    TimeGrain.MONTH: 31,
    TimeGrain.QUARTER: 92,
}
_VINTAGE_LONGEST_DAYS = max(
    vintage_ending(date(2024 + offset // 12, offset % 12 + 1, 1)).window.days
    for offset in range(48)
)
"""The longest 13-month cohort vintage over four years, a leap February included (397)."""


def longest_grain_days(grain: TimeGrain) -> int:
    """Return the longest instance of one bucket grain in days (a bake horizon's overlap shift)."""
    if grain is TimeGrain.CUSTOM:
        return _VINTAGE_LONGEST_DAYS
    return _LONGEST_FIXED_GRAIN_DAYS[grain]
