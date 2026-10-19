# SPDX-License-Identifier: MIT-0
"""Answer a chat question about a periodic metric at a complete measured period (ADR-100 point 9).

The model never computes dates and nothing here reads its text: the question's period arrives as
a governed reference period already resolved to dates (`domain.conversation.temporal`), and this
function decides what a periodic metric can say about it.

- "last month" is the calendar's last complete month. It resolves through the same
  `resolve_calendar_preset` a page uses, and the answer names the period it used. If that month
  hasn't landed yet (packs 5 to 7 land 16 days after period end), the answer says so and uses
  the latest measured month.
- A complete period that was measured is answered as asked.
- "the 7 days ending yesterday", or any window that isn't one whole period, is refused with the
  latest measured complete week (or the metric's first declared grain) as the alternative.
- A cohort vintage (`custom`, ADR-104) has no calendar preset: its latest measured period is the
  newest vintage every contract published, and the answer names it ("cohorts as of September
  2026").
- A question naming no period reads the latest measured complete period
  (`resolve_default_period`), not the 7-day coverage default a daily metric uses, which is
  rarely one whole period; the answer says no period was named and names the one it used.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from domain.measurement.periodic import PeriodRefusal, PeriodRefusalReason, select_served_period
from domain.presentation.calendar_presets import (
    PeriodicContract,
    PresetSection,
    ResolveCalendarPresetRequest,
    resolve_calendar_preset,
)
from domain.presentation.page_schemas import TemporalPresetId
from domain.semantics.calendar_periods import (
    PERIODIC_GRAINS,
    CalendarPeriod,
    grain_list_text,
    grain_noun,
    is_complete_period,
    latest_complete_period,
)
from domain.semantics.time import TimeGrain

if TYPE_CHECKING:  # pragma: no cover — typing only
    from datetime import date

    from domain.shared_kernel.values import DateRange

__all__ = ["ChatPeriodAnswer", "ChatPeriodRequest", "resolve_chat_period", "resolve_default_period"]

_PRESETS = {
    TimeGrain.WEEK: TemporalPresetId.LAST_COMPLETE_WEEK,
    TimeGrain.MONTH: TemporalPresetId.LAST_COMPLETE_MONTH,
    TimeGrain.QUARTER: TemporalPresetId.LAST_COMPLETE_QUARTER,
}


@dataclass(frozen=True, slots=True)
class ChatPeriodRequest:
    """A periodic metric, the question's resolved window, and what has been published."""

    metric_name: str
    period_grains: tuple[TimeGrain, ...]
    window: DateRange
    as_of: date
    contracts: tuple[PeriodicContract, ...]
    publications: frozenset[tuple[str, CalendarPeriod]]


@dataclass(frozen=True, slots=True)
class ChatPeriodAnswer:
    """The period the answer reads, or None, and the sentence that names it."""

    period: CalendarPeriod | None
    sentence: str
    substituted: bool = False
    """The question named the calendar's latest period, and it hasn't landed yet."""


NO_VINTAGE_TEXT = "No cohort vintage has been measured yet."


def _latest_measured(
    request: ChatPeriodRequest, grain: TimeGrain
) -> tuple[CalendarPeriod | None, str]:
    if grain is TimeGrain.CUSTOM:
        vintages = [
            period
            for _contract, period in request.publications
            if period.grain is TimeGrain.CUSTOM and _published(request, period)
        ]
        return (max(vintages), "") if vintages else (None, NO_VINTAGE_TEXT)
    result = resolve_calendar_preset(
        ResolveCalendarPresetRequest(
            preset=_PRESETS[grain],
            as_of=request.as_of,
            sections=(PresetSection("answer", True, request.contracts),),
            publications=request.publications,
        )
    )
    return result.period, (result.none_text or "")


def _published(request: ChatPeriodRequest, period: CalendarPeriod) -> bool:
    return all((item.contract, period) in request.publications for item in request.contracts)


def resolve_chat_period(request: ChatPeriodRequest) -> ChatPeriodAnswer:
    """Return the period a periodic answer reads and the sentence naming it, or refuse."""
    window = request.window
    grain = next(
        (
            item
            for item in PERIODIC_GRAINS
            if item in request.period_grains and is_complete_period(item, window.start, window.end)
        ),
        None,
    )
    if grain is None:
        return _refuse_rolling(request)
    asked = CalendarPeriod(window.start, window.end, grain)
    if _published(request, asked):
        return ChatPeriodAnswer(asked, f"Figures are for {asked.label}.")
    if asked != latest_complete_period(grain, request.as_of):
        served = frozenset(
            period
            for contract, period in request.publications
            if contract in {item.contract for item in request.contracts}
            and _published(request, period)
        )
        select_served_period(request.metric_name, asked, served)
    latest, none_text = _latest_measured(request, grain)
    if latest is None:
        return ChatPeriodAnswer(None, f"{asked.label} hasn't been measured yet. {none_text}")
    return ChatPeriodAnswer(
        latest,
        f"{asked.label} hasn't been measured yet, so this answer uses the latest measured "
        f"{grain_noun(grain)}, {latest.label}.",
        substituted=True,
    )


def _default_grain(request: ChatPeriodRequest) -> TimeGrain:
    """Return the week when the metric has one, else its first declared grain."""
    return TimeGrain.WEEK if TimeGrain.WEEK in request.period_grains else request.period_grains[0]


def resolve_default_period(request: ChatPeriodRequest) -> ChatPeriodAnswer:
    """Return the latest measured complete period for a question that named none.

    The window the governed coverage default chose is ignored: it is the 7 days ending at the
    latest covered day, shaped for a daily metric. The grain is the one a refusal offers (the
    week, else the metric's first declared grain); a cohort metric reads its newest vintage.
    """
    grain = _default_grain(request)
    latest, none_text = _latest_measured(request, grain)
    if latest is None:
        return ChatPeriodAnswer(None, none_text or "No complete period has been measured yet.")
    noun = "cohort vintage" if grain is TimeGrain.CUSTOM else grain_noun(grain)
    return ChatPeriodAnswer(
        latest,
        f"The question named no period, so this answer uses the latest measured {noun}, "
        f"{latest.label}.",
    )


def _refuse_rolling(request: ChatPeriodRequest) -> ChatPeriodAnswer:
    window = request.window
    grain = _default_grain(request)
    latest, none_text = _latest_measured(request, grain)
    alternative = (
        f"The latest measured {grain_noun(grain)} is {latest.label}." if latest else none_text
    )
    raise PeriodRefusal(
        request.metric_name,
        PeriodRefusalReason.PERIOD_NOT_COMPLETE,
        f"{request.metric_name} is measured for complete "
        f"{grain_list_text(request.period_grains)} only, so {window.start.isoformat()} to "
        f"{window.end.isoformat()} can't be answered. {alternative}",
        latest,
    ).to_error()
