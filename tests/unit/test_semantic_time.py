# SPDX-License-Identifier: MIT-0
"""Semantic V2 extraction coverage and row-measurement prerequisites.

These component tests enforce the time boundary ADR-046 requires. Full SA-05 additionally exercises
source-aligned and canonical publication and remains a later cross-capability test.
"""

from __future__ import annotations

from datetime import date

import pytest

from domain.semantics.time import (
    BusinessCalendarKey,
    ExtractionCoverage,
    FactTimeClass,
    FactTimePolicy,
    MeasurementBucket,
    SemanticTimeError,
    TimeGrain,
)
from domain.shared_kernel.values import DateRange

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-046")]

ISO_CALENDAR = BusinessCalendarKey("iso_gregorian", week_starts_on=0)
VENDOR_CALENDAR = BusinessCalendarKey("amazon_vendor_sunday", week_starts_on=6)


def _bucket(start: date, end: date, grain: TimeGrain) -> MeasurementBucket:
    return MeasurementBucket(
        window=DateRange(start, end),
        grain=grain,
        calendar=ISO_CALENDAR,
        source_timezone="UTC",
    )


def _vendor_week(start: date, end: date) -> MeasurementBucket:
    return MeasurementBucket(
        window=DateRange(start, end),
        grain=TimeGrain.WEEK,
        calendar=VENDOR_CALENDAR,
        source_timezone="PST",
    )


def test_multi_day_extraction_can_contain_daily_measurements() -> None:
    coverage = ExtractionCoverage(DateRange(date(2026, 8, 1), date(2026, 8, 15)))
    bucket = _bucket(date(2026, 8, 7), date(2026, 8, 7), TimeGrain.DAY)
    coverage.require_contains(bucket)


def test_admissible_days_are_plain_ranges_and_invent_no_calendar_or_timezone() -> None:
    coverage = ExtractionCoverage(DateRange(date(2026, 8, 1), date(2026, 8, 3)))
    assert coverage.admissible_daily_windows() == (
        DateRange(date(2026, 8, 1), date(2026, 8, 1)),
        DateRange(date(2026, 8, 2), date(2026, 8, 2)),
        DateRange(date(2026, 8, 3), date(2026, 8, 3)),
    )


def test_bucket_outside_extraction_coverage_is_refused() -> None:
    coverage = ExtractionCoverage(DateRange(date(2026, 8, 1), date(2026, 8, 15)))
    bucket = _bucket(date(2026, 8, 16), date(2026, 8, 16), TimeGrain.DAY)
    with pytest.raises(SemanticTimeError, match="outside extraction coverage"):
        coverage.require_contains(bucket)


def test_daily_bucket_is_exactly_one_day() -> None:
    with pytest.raises(SemanticTimeError, match="DAY bucket spans 2 days"):
        _bucket(date(2026, 8, 1), date(2026, 8, 2), TimeGrain.DAY)


def test_weekly_bucket_is_exactly_seven_days() -> None:
    with pytest.raises(SemanticTimeError, match="rather than seven"):
        _vendor_week(date(2026, 8, 2), date(2026, 8, 9))


def test_weekly_bucket_must_start_on_its_governed_calendar_boundary() -> None:
    with pytest.raises(SemanticTimeError, match="starts on weekday"):
        _vendor_week(date(2026, 8, 3), date(2026, 8, 9))


def test_weekly_bucket_requires_a_week_start_rule() -> None:
    with pytest.raises(SemanticTimeError, match="requires a calendar week-start rule"):
        MeasurementBucket(
            window=DateRange(date(2026, 8, 2), date(2026, 8, 8)),
            grain=TimeGrain.WEEK,
            calendar=BusinessCalendarKey("unresolved_week"),
            source_timezone="PST",
        )


def test_daily_fact_accepts_daily_bucket() -> None:
    policy = FactTimePolicy(
        time_class=FactTimeClass.DAILY_ADDITIVE,
        allowed_grains=frozenset({TimeGrain.DAY}),
    )
    policy.require_accepts(_bucket(date(2026, 8, 1), date(2026, 8, 1), TimeGrain.DAY))


def test_daily_fact_refuses_weekly_total() -> None:
    policy = FactTimePolicy(
        time_class=FactTimeClass.DAILY_ADDITIVE,
        allowed_grains=frozenset({TimeGrain.DAY}),
    )
    weekly = _vendor_week(date(2026, 8, 2), date(2026, 8, 8))
    with pytest.raises(SemanticTimeError, match="received week"):
        policy.require_accepts(weekly)


def test_periodic_fact_accepts_week_without_claiming_days() -> None:
    policy = FactTimePolicy(
        time_class=FactTimeClass.PERIODIC,
        allowed_grains=frozenset({TimeGrain.WEEK, TimeGrain.MONTH}),
    )
    policy.require_accepts(_vendor_week(date(2026, 8, 2), date(2026, 8, 8)))


def test_periodic_fact_cannot_be_configured_to_accept_day() -> None:
    with pytest.raises(SemanticTimeError, match="cannot accept DAY"):
        FactTimePolicy(
            time_class=FactTimeClass.PERIODIC,
            allowed_grains=frozenset({TimeGrain.DAY, TimeGrain.WEEK}),
        )
