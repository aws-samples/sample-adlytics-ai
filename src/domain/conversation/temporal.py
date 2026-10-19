# SPDX-License-Identifier: MIT-0
"""Today's date, and the periods a user means when they say "last week".

The temporal half of the context engine's base layer. Pure: a date goes in and named ranges
come out, so every boundary is asserted without a clock.

**This exists because the agent asked what day it was.** A real turn on 2026-08-23 met "what was
ad spend last week?" with a question back about today's date — the model has no clock, nothing
injected one, and a tool argument is a date range. Build order 2.10 records that turn as this
increment's first acceptance test.

**The ranges are computed here rather than by the model, and that is the tool contract rather
than a convenience.** The base prompt forbids the model calculating, and a date range is
arithmetic on a value it was handed. Leaving it to infer one puts the model one off-by-one away
from answering a different question than it was asked, silently, because a window is not a
figure a reader would check. So the periods arrive named and already resolved, and the prompt
tells it to use these rather than derive its own.

**Every trailing period ends yesterday, never today.** Today's data does not exist yet: a
delivery covers a closed business day, and the freshest observation in dev is a day behind by
construction. A "last 7 days" including today would ask for a day no source has reported, and
the answer would be a six-day figure presented as seven.

**"Last week" is genuinely ambiguous and the label resolves it rather than the code choosing
silently.** A trailing seven days and a calendar week are different questions with different
answers, and Amazon's own reports do not agree on which day a week starts. Both are offered,
each says which it is, and neither is called "last week" alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from domain.semantics.analytical_resolution import normalize_exact_text
from domain.semantics.calendar_periods import latest_complete_period
from domain.semantics.time import TimeGrain
from domain.shared_kernel.values import DateRange

__all__ = [
    "DAYS_IN_WEEK",
    "ReferencePeriod",
    "TemporalError",
    "reference_period_candidates",
    "reference_periods",
    "temporal_lines",
]

DAYS_IN_WEEK = 7


class TemporalError(Exception):
    """A date cannot be turned into reference periods."""


@dataclass(frozen=True, slots=True)
class ReferencePeriod:
    """One named period, already resolved to dates.

    The label is part of the value rather than a key in a mapping, because it is the half that
    disambiguates: "the 7 days ending yesterday" and "last week" resolve to different ranges and
    a reader has to be able to tell which one they were given.
    """

    label: str
    window: DateRange

    def __post_init__(self) -> None:
        if not self.label.strip():
            raise TemporalError("A reference period must be named; the name is what resolves it.")


def reference_periods(today: date) -> tuple[ReferencePeriod, ...]:
    """Resolve common periods against the latest closed day before ``today``.

    Ordered shortest first, so a reader scanning for a period finds the common ones before the
    long ones. Current calendar periods are offered only when they contain at least one closed day;
    Monday therefore has no current week, and the first has no current month.
    """
    latest_closed_day = today - timedelta(days=1)
    # `date.weekday()` is Monday-based, so the distance back to Monday *is* the weekday and no
    # offset is needed. This subtracted a named `MONDAY = 0` until 2026-08-23, which read as
    # deliberate and was arithmetic that could not be wrong — mutation proved it by replacing the
    # operator five different ways and producing an identical result every time. The assumption it
    # was documenting is pinned by a test instead, where it can actually fail.
    this_monday = today - timedelta(days=today.weekday())
    last_monday = this_monday - timedelta(days=DAYS_IN_WEEK)
    first_of_month = today.replace(day=1)
    last_of_prior_month = first_of_month - timedelta(days=1)

    periods = [
        ReferencePeriod("yesterday", DateRange(latest_closed_day, latest_closed_day)),
        ReferencePeriod("the 7 days ending yesterday", _trailing(latest_closed_day, DAYS_IN_WEEK)),
        ReferencePeriod("the 14 days ending yesterday", _trailing(latest_closed_day, 14)),
        ReferencePeriod("the 30 days ending yesterday", _trailing(latest_closed_day, 30)),
        ReferencePeriod(
            "last week (calendar, Monday to Sunday)",
            DateRange(last_monday, last_monday + timedelta(days=DAYS_IN_WEEK - 1)),
        ),
    ]
    if this_monday <= latest_closed_day:
        periods.append(
            ReferencePeriod(
                "this week so far (from Monday)", DateRange(this_monday, latest_closed_day)
            )
        )
    if first_of_month <= latest_closed_day:
        periods.append(
            ReferencePeriod("this month so far", DateRange(first_of_month, latest_closed_day))
        )
    periods.append(
        ReferencePeriod(
            "last month", DateRange(last_of_prior_month.replace(day=1), last_of_prior_month)
        )
    )
    # The calendar's last complete quarter, by the rule a page's `last_complete_quarter` preset
    # uses, so a quarterly question (Reach frequency) never needs model date arithmetic.
    periods.append(
        ReferencePeriod(
            "last quarter", latest_complete_period(TimeGrain.QUARTER, latest_closed_day).window
        )
    )
    return tuple(periods)


def reference_period_candidates(today: date, reference: str) -> tuple[ReferencePeriod, ...]:
    """Return only named periods whose labels match after exact normalization."""
    expected = normalize_exact_text(reference)
    if not expected:
        return ()
    return tuple(
        period
        for period in reference_periods(today)
        if normalize_exact_text(period.label) == expected
    )


def _trailing(last_day: date, days: int) -> DateRange:
    """Build a range of `days` days ending on `last_day`, inclusive at both ends."""
    return DateRange(last_day - timedelta(days=days - 1), last_day)


def temporal_lines(today: date) -> tuple[str, ...]:
    """Render today's date and every reference period as prompt text.

    A rendered block rather than a structure the caller formats, for the same reason the
    measurement narrative is built in the domain: the wording is the part that has to be exactly
    right, and it should be asserted character by character without an AWS account.
    """
    periods = reference_periods(today)
    lines = [
        f"Today is {today.isoformat()} ({today.strftime('%A')}). Dates are business days as the "
        "source reported them; a tool result states which timezone closed the day.",
        "",
        "These periods are already resolved. Use one of these rather than working out dates "
        "yourself, and if a question does not match any of them, pass the explicit dates the "
        "person gave you:",
    ]
    width = max(len(period.label) for period in periods)
    lines.extend(
        f"  {period.label:<{width}}  {period.window.start.isoformat()} to "
        f"{period.window.end.isoformat()}"
        for period in periods
    )
    lines.extend(
        (
            "",
            "No period ending today is offered, because a day is only reported once it has "
            "closed upstream. Asking about today returns an absence rather than a partial "
            "figure.",
        )
    )
    return tuple(lines)
