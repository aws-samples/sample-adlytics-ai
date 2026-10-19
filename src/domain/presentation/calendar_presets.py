# SPDX-License-Identifier: MIT-0
"""Resolve a calendar preset to the newest period that has landed (ADR-100 points 4 to 6).

Packs 5 to 7 land 16 days after their period ends, so the calendar's last complete week is never
published when a page first shows it. `last_complete_week`, `last_complete_month` and
`last_complete_quarter` therefore resolve to the latest complete period for which every anchor
has data, and the page names that period. Every section on the page then uses it: one window per
page.

This module also builds the designed texts a page shows around that rule: the resolved period's
label, pending or missing text for a non-anchor section, the result-`none` text, and the absence
text on a preset outside a section's `declared_periods`. Each is built from declared values
(grains, server preset labels, dates), never typed into copy and never read from model text.

Pure domain. Publications, coverage and `as_of` arrive from adapters; no clock is read.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum

from domain.capabilities.paid_features import (
    PaidFeatureState,
    PeriodState,
    ResolvePeriodStateRequest,
    resolve_period_state,
)
from domain.presentation.page_schemas import (
    TemporalPresetId,
    calendar_preset_grain,
    is_calendar_preset,
)
from domain.presentation.temporal import temporal_preset_label
from domain.semantics.calendar_periods import (
    CalendarPeriod,
    day_label,
    grain_list_text,
    grain_noun,
    latest_complete_period,
)
from domain.semantics.time import TimeGrain
from domain.shared_kernel.values import DateRange

__all__ = [
    "DEFAULT_CADENCE_LAG_DAYS",
    "LOOKBACK_MONTHS",
    "AbsenceSubject",
    "DailyOperandCoverage",
    "PeriodicContract",
    "PresetSection",
    "ResolveCalendarPresetRequest",
    "ResolveCalendarPresetResult",
    "SectionPeriodRequest",
    "SectionPeriodState",
    "SectionPeriodStatus",
    "declared_period_absence_text",
    "no_complete_period_text",
    "resolve_calendar_preset",
    "section_period_state",
]

LOOKBACK_MONTHS = 13
"""How far back the search for a landed period goes (ADR-100 point 5)."""

DEFAULT_CADENCE_LAG_DAYS = 2
"""A pack's lag when it declares none (ADR-110), and so a daily operand's."""


@dataclass(frozen=True, slots=True)
class PeriodicContract:
    """One periodic contract a section reads, with its declared `cadenceLagDays`."""

    contract: str
    cadence_lag_days: int

    def __post_init__(self) -> None:
        if not self.contract.strip() or self.cadence_lag_days < 0:
            raise ValueError("a periodic contract needs a name and a non-negative lag.")


@dataclass(frozen=True, slots=True)
class PresetSection:
    """One section bound on the requested preset, and the periodic contracts it reads.

    Only periodic contracts go here: a daily publication carries DAY buckets only, so daily
    operands enter through `daily_coverage` (ADR-100 point 5).
    """

    section_id: str
    period_anchor: bool
    periodic_contracts: tuple[PeriodicContract, ...] = ()


@dataclass(frozen=True, slots=True)
class DailyOperandCoverage:
    """The days one daily operand of the page has a current publication for (D-6)."""

    contract: str
    covered: tuple[DateRange, ...]
    cadence_lag_days: int = DEFAULT_CADENCE_LAG_DAYS

    def covers(self, period: CalendarPeriod) -> bool:
        """Whether every day of `period` has a current publication."""
        day = period.start
        while day <= period.end:
            if not any(interval.start <= day <= interval.end for interval in self.covered):
                return False
            day += timedelta(days=1)
        return True


@dataclass(frozen=True, slots=True)
class ResolveCalendarPresetRequest:
    """Everything `resolve_calendar_preset` reads. `as_of` is the publisher's clock date."""

    preset: TemporalPresetId
    as_of: date
    sections: tuple[PresetSection, ...]
    """Every section bound on this preset. The anchors are those with `period_anchor`; if
    none has it, every one of them anchors."""
    publications: frozenset[tuple[str, CalendarPeriod]]
    """(contract, exact period) for every current publication of a periodic contract."""
    daily_coverage: tuple[DailyOperandCoverage, ...] = ()

    def __post_init__(self) -> None:
        if not is_calendar_preset(self.preset):
            raise ValueError(f"{self.preset.value} isn't a calendar preset.")
        if not self.sections:
            raise ValueError("a calendar preset resolves for at least one bound section.")


@dataclass(frozen=True, slots=True)
class ResolveCalendarPresetResult:
    """The resolved period with its label, or `none` with when the first is expected."""

    grain: TimeGrain
    period: CalendarPeriod | None
    expected_after: date
    """For a period: when its slowest anchor was due. For `none`: when the first is expected."""

    @property
    def label(self) -> str | None:
        """The server-built name the header and subtitle show, e.g. "September 2026"."""
        return None if self.period is None else self.period.label

    @property
    def none_text(self) -> str | None:
        """The designed text for result `none` (ADR-100 point 5)."""
        if self.period is not None:
            return None
        return no_complete_period_text(self.grain, self.expected_after)


def _anchors(sections: tuple[PresetSection, ...]) -> tuple[PresetSection, ...]:
    anchors = tuple(section for section in sections if section.period_anchor)
    return anchors or sections


def _lag(anchors: tuple[PresetSection, ...], daily: tuple[DailyOperandCoverage, ...]) -> int:
    """Return the slowest declared lag the anchors depend on."""
    lags = [
        contract.cadence_lag_days for section in anchors for contract in section.periodic_contracts
    ]
    lags.extend(operand.cadence_lag_days for operand in daily)
    return max(lags, default=DEFAULT_CADENCE_LAG_DAYS)


def _qualifies(request: ResolveCalendarPresetRequest, period: CalendarPeriod) -> bool:
    contracts = {
        contract.contract
        for section in _anchors(request.sections)
        for contract in section.periodic_contracts
    }
    return all((contract, period) in request.publications for contract in contracts) and all(
        operand.covers(period) for operand in request.daily_coverage
    )


def _earliest_end(as_of: date) -> date:
    """Return the day `LOOKBACK_MONTHS` months before `as_of`, clamped to the month."""
    months = as_of.year * 12 + as_of.month - 1 - LOOKBACK_MONTHS
    year, month = divmod(months, 12)
    month += 1
    day = as_of.day
    while True:
        try:
            return date(year, month, day)
        except ValueError:
            day -= 1


def resolve_calendar_preset(request: ResolveCalendarPresetRequest) -> ResolveCalendarPresetResult:
    """Return the latest complete period ending on or before `as_of` that every anchor has.

    A period qualifies when every periodic contract the anchors read has a current publication
    for exactly that period, and every daily operand covers each of its days. An anchor set with
    no periodic contract resolves on daily coverage alone. The search looks back at most 13
    months. If nothing qualifies, the result is `none`, and `expected_after` is when the newest
    complete period is due: its end plus the slowest lag the anchors depend on. A period
    qualifies only when every anchor has it, so the slowest anchor decides that date.
    """
    grain = calendar_preset_grain(request.preset)
    anchors = _anchors(request.sections)
    lag = timedelta(days=_lag(anchors, request.daily_coverage))
    newest = latest_complete_period(grain, request.as_of)
    earliest_end = _earliest_end(request.as_of)
    candidate = newest
    while candidate.end >= earliest_end:
        if _qualifies(request, candidate):
            return ResolveCalendarPresetResult(grain, candidate, candidate.end + lag)
        candidate = candidate.previous()
    return ResolveCalendarPresetResult(grain, None, newest.end + lag)


def no_complete_period_text(grain: TimeGrain, expected_after: date) -> str:
    """'No complete month has been measured yet. The first is expected after 16 Oct 2026.'."""
    return (
        f"No complete {grain_noun(grain)} has been measured yet. The first is expected after "
        f"{day_label(expected_after)}."
    )


class SectionPeriodStatus(StrEnum):
    """What a section shows for the page's resolved period."""

    PUBLISHED = "published"
    PENDING = "pending"
    MISSING = "missing"


@dataclass(frozen=True, slots=True)
class SectionPeriodState:
    """A non-anchor section's state for period P, and its designed text when not published."""

    status: SectionPeriodStatus
    text: str | None


@dataclass(frozen=True, slots=True)
class SectionPeriodRequest:
    """One non-anchor section, the page's period P, the publications and the adapter's date."""

    section: PresetSection
    period: CalendarPeriod
    publications: frozenset[tuple[str, CalendarPeriod]]
    as_of: date


def section_period_state(request: SectionPeriodRequest) -> SectionPeriodState:
    """Resolve published, pending or missing for one section, per request (design §4.7 item 4).

    A standard pack reuses the paid-feature resolver with the "needs the paid feature" row
    switched off: `expected_after = P.end + cadenceLagDays`, pending through that day, missing
    after it. The texts name the period, never "last week".
    """
    section, period, publications = request.section, request.period, request.publications
    published = all(
        (contract.contract, period) in publications for contract in section.periodic_contracts
    )
    lag = max(
        (contract.cadence_lag_days for contract in section.periodic_contracts),
        default=DEFAULT_CADENCE_LAG_DAYS,
    )
    latest = max(
        (
            published_period.window
            for contract, published_period in publications
            if contract in {item.contract for item in section.periodic_contracts}
        ),
        key=lambda window: window.end,
        default=None,
    )
    baked = PaidFeatureState(
        feature_observed=True,
        period_published=published,
        expected_after=period.end + timedelta(days=lag),
        latest_available_period=period.window if published else latest,
    )
    resolved = resolve_period_state(ResolvePeriodStateRequest(baked, request.as_of))
    if resolved.state is PeriodState.PUBLISHED:
        return SectionPeriodState(SectionPeriodStatus.PUBLISHED, None)
    if resolved.state is PeriodState.PENDING:
        return SectionPeriodState(
            SectionPeriodStatus.PENDING,
            f"Figures for {period.label} are expected after {day_label(resolved.expected_after)}.",
        )
    return SectionPeriodState(
        SectionPeriodStatus.MISSING, f"Figures for {period.label} didn't arrive."
    )


@dataclass(frozen=True, slots=True)
class AbsenceSubject:
    """What a section measures, as its declaration names it, e.g. 'Frequency'."""

    text: str
    plural: bool

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValueError("an absence subject needs text.")


def declared_period_absence_text(
    subject: AbsenceSubject,
    period_grains: tuple[TimeGrain, ...],
    declared_periods: tuple[TemporalPresetId, ...],
) -> str:
    """Build the text a section shows on a preset outside its `declared_periods` (§4.7 item 5).

    "{Subject} {is|are} measured for complete {grains}. Choose {preset label} to see {it|them}."
    The grains come from the metric's declared `period_grains`, the preset label from the
    server-declared label of the section's first declared preset in canonical order.
    """
    if not declared_periods:
        raise ValueError("a section with no declared periods shows no absence text.")
    first = min(declared_periods, key=lambda preset: tuple(TemporalPresetId).index(preset))
    verb, pronoun = ("are", "them") if subject.plural else ("is", "it")
    return (
        f"{subject.text} {verb} measured for complete {grain_list_text(period_grains)}. "
        f"Choose {temporal_preset_label(first)} to see {pronoun}."
    )
