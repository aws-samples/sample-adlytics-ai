# SPDX-License-Identifier: MIT-0
"""Extraction coverage and row measurement time are different values.

ADR-046. An extraction may request fifteen days and return fifteen daily rows, one weekly total, or
nothing for some days. The request interval is provenance; the bucket on each row is semantics. A
canonical daily fact accepts only daily buckets and an explicit periodic fact never masquerades as a
daily row whose date happens to be the period start.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING

from domain.shared_kernel.values import DateRange

if TYPE_CHECKING:  # pragma: no cover — typing only
    from collections.abc import Iterable

__all__ = [
    "BusinessCalendarKey",
    "ExtractionCoverage",
    "FactTimeClass",
    "FactTimePolicy",
    "MeasurementBucket",
    "ObservationTimeBasis",
    "SemanticTimeError",
    "TimeGrain",
]

WEEK_DAYS = 7
"""Number of inclusive business days in a week, named for bucket-shape validation."""


class SemanticTimeError(ValueError):
    """A measurement interval contradicts its declared time semantics."""


class TimeGrain(StrEnum):
    """What interval one source measurement row represents."""

    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    QUARTER = "quarter"
    YEAR = "year"
    CUSTOM = "custom"


class ObservationTimeBasis(StrEnum):
    """Whether a bucket is source business time or this system's first durable observation."""

    SOURCE_BUSINESS_DATE = "source_business_date"
    SYSTEM_FIRST_OBSERVED = "system_first_observed"


class FactTimeClass(StrEnum):
    """How a canonical fact behaves through time."""

    DAILY_ADDITIVE = "daily_additive"
    PERIODIC = "periodic"
    SNAPSHOT = "snapshot"


@dataclass(frozen=True, slots=True)
class BusinessCalendarKey:
    """Calendar governing week labels, separate from source timezone.

    `week_starts_on` follows `date.weekday()` (Monday zero). It is mandatory for weekly evidence;
    accepting a seven-day interval with no boundary rule would call an arbitrary Wednesday-to-
    Tuesday span a calendar week.
    """

    value: str
    week_starts_on: int | None = None

    def __post_init__(self) -> None:
        if not self.value.strip():
            raise SemanticTimeError("business calendar key cannot be empty.")
        if self.week_starts_on is not None and self.week_starts_on not in range(WEEK_DAYS):
            raise SemanticTimeError("week_starts_on must be in the inclusive range 0..6.")


@dataclass(frozen=True, slots=True)
class ExtractionCoverage:
    """Inclusive interval one source execution claims it attempted to cover."""

    window: DateRange

    def contains(self, bucket: MeasurementBucket) -> bool:
        """Whether the row's whole measurement interval sits inside the extraction claim."""
        return self.window.covers(bucket.window)

    def require_contains(self, bucket: MeasurementBucket) -> None:
        """Refuse source-business time outside coverage; technical observation is independent."""
        if bucket.observation_basis is ObservationTimeBasis.SYSTEM_FIRST_OBSERVED:
            return
        if not self.contains(bucket):
            raise SemanticTimeError(
                f"measurement bucket {bucket.window} lies outside extraction coverage "
                f"{self.window}."
            )

    def admissible_daily_windows(self) -> tuple[DateRange, ...]:
        """Return date partitions a multi-day extraction could contain, without inventing meaning.

        These are plain ranges, not `MeasurementBucket` values: the extraction interval can
        establish admissible dates and cannot establish the source timezone or calendar of rows it
        has not read. The later compiler supplies those from verified profile and row evidence.
        """
        return tuple(DateRange(day, day) for day in _days(self.window))


@dataclass(frozen=True, slots=True)
class MeasurementBucket:
    """Exact interval represented by one measurement row or aggregate."""

    window: DateRange
    grain: TimeGrain
    calendar: BusinessCalendarKey
    source_timezone: str
    observation_basis: ObservationTimeBasis = ObservationTimeBasis.SOURCE_BUSINESS_DATE

    def __post_init__(self) -> None:
        if not self.source_timezone.strip():
            raise SemanticTimeError("measurement bucket requires its source timezone.")
        _validate_shape(self.window, self.grain, self.calendar)


@dataclass(frozen=True, slots=True)
class FactTimePolicy:
    """Time classes a canonical fact accepts before publication."""

    time_class: FactTimeClass
    allowed_grains: frozenset[TimeGrain]

    def __post_init__(self) -> None:
        if not self.allowed_grains:
            raise SemanticTimeError("a fact time policy requires at least one allowed grain.")
        if self.time_class is FactTimeClass.DAILY_ADDITIVE and self.allowed_grains != frozenset(
            {TimeGrain.DAY}
        ):
            raise SemanticTimeError("a daily additive fact accepts exactly DAY buckets.")
        if self.time_class is FactTimeClass.PERIODIC and TimeGrain.DAY in self.allowed_grains:
            raise SemanticTimeError(
                "an explicit periodic fact cannot accept DAY; daily measurements belong in a "
                "daily additive fact."
            )

    def accepts(self, bucket: MeasurementBucket) -> bool:
        """Whether this fact class may publish the measurement bucket without changing meaning."""
        return bucket.grain in self.allowed_grains

    def require_accepts(self, bucket: MeasurementBucket) -> None:
        """Refuse a measurement bucket whose grain does not belong in this fact class."""
        if not self.accepts(bucket):
            expected = ", ".join(sorted(grain.value for grain in self.allowed_grains))
            raise SemanticTimeError(
                f"{self.time_class.value} fact accepts {expected}; received {bucket.grain.value} "
                f"for {bucket.window}."
            )


def _validate_shape(
    window: DateRange, grain: TimeGrain, business_calendar: BusinessCalendarKey
) -> None:
    days = window.days
    if grain is TimeGrain.DAY and days != 1:
        raise SemanticTimeError(f"DAY bucket spans {days} days: {window}.")
    if grain is TimeGrain.WEEK:
        if days != WEEK_DAYS:
            raise SemanticTimeError(f"WEEK bucket spans {days} days rather than seven: {window}.")
        if business_calendar.week_starts_on is None:
            raise SemanticTimeError("WEEK bucket requires a calendar week-start rule.")
        if window.start.weekday() != business_calendar.week_starts_on:
            raise SemanticTimeError(
                f"WEEK bucket starts on weekday {window.start.weekday()}, while "
                f"{business_calendar.value} starts on {business_calendar.week_starts_on}."
            )
    if grain is TimeGrain.MONTH:
        last = calendar.monthrange(window.start.year, window.start.month)[1]
        if window.start.day != 1 or window.end != date(window.start.year, window.start.month, last):
            raise SemanticTimeError(f"MONTH bucket is not one complete calendar month: {window}.")
    if grain is TimeGrain.QUARTER:
        if window.start.day != 1 or window.start.month not in {1, 4, 7, 10}:
            raise SemanticTimeError(f"QUARTER bucket has no quarter boundary at {window.start}.")
        end_month = window.start.month + 2
        expected = date(
            window.start.year,
            end_month,
            calendar.monthrange(window.start.year, end_month)[1],
        )
        if window.end != expected:
            raise SemanticTimeError(f"QUARTER bucket is incomplete: {window}.")
    if grain is TimeGrain.YEAR and (
        window.start != date(window.start.year, 1, 1)
        or window.end != date(window.start.year, 12, 31)
    ):
        raise SemanticTimeError(f"YEAR bucket is not one complete calendar year: {window}.")


def _days(window: DateRange) -> Iterable[date]:
    for offset in range(window.days):
        yield window.start + timedelta(days=offset)
