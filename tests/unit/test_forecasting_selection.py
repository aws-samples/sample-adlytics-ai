# SPDX-License-Identifier: MIT-0
"""Window pins and input fingerprint: a wrong rule re-runs daily or misses a restatement."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from domain.forecasting.selection import (
    HeldCategory,
    SelectionWindowRequest,
    WindowPin,
    window_pins,
)
from domain.shared_kernel.values import DateRange

pytestmark = pytest.mark.unit

FIRST = date(2024, 10, 6)  # a Sunday
LAST = date(2026, 10, 3)  # a Saturday, 104 weeks later


def _pins(first: date, last: date, digest: str = "a" * 64) -> tuple[WindowPin, ...]:
    days = (last - first).days + 1
    return tuple(
        WindowPin(
            partition=f"day={first + timedelta(days=n)}",
            publication_id=f"pub-{n}",
            fencing_token=1,
            digest=digest,
            bucket=DateRange(first + timedelta(days=n), first + timedelta(days=n)),
        )
        for n in range(days)
    )


def _request(
    pins: tuple[WindowPin, ...], today: date, covered_end: date = LAST
) -> SelectionWindowRequest:
    covered = (DateRange(FIRST, covered_end),)
    return SelectionWindowRequest(
        sales=pins,
        traffic=pins,
        covered_sales=covered,
        covered_traffic=covered,
        selection_window=DateRange(today - timedelta(days=999), today),
    )


def test_input_end_is_the_newest_wholly_covered_saturday() -> None:
    result = window_pins(_request(_pins(FIRST, LAST), LAST + timedelta(days=5)))
    assert result.held is None
    assert result.input_end == LAST
    assert result.window == DateRange(LAST - timedelta(days=727), LAST)
    assert len(result.sales) == 728


def test_a_new_day_after_input_end_keeps_the_fingerprint() -> None:
    base = window_pins(_request(_pins(FIRST, LAST), LAST + timedelta(days=5)))
    newer = window_pins(
        _request(
            _pins(FIRST, LAST + timedelta(days=3)),
            LAST + timedelta(days=5),
            covered_end=LAST + timedelta(days=3),
        )
    )
    assert newer.input_end == LAST
    assert newer.input_fingerprint == base.input_fingerprint


def test_a_restated_day_inside_the_window_changes_the_fingerprint() -> None:
    pins = list(_pins(FIRST, LAST))
    base = window_pins(_request(tuple(pins), LAST + timedelta(days=5)))
    old = pins[400]
    pins[400] = WindowPin(old.partition, "pub-restated", 2, "b" * 64, old.bucket)
    restated = window_pins(_request(tuple(pins), LAST + timedelta(days=5)))
    assert restated.input_fingerprint != base.input_fingerprint


@pytest.mark.parametrize("lag", [3, 8, 10, 40])
def test_unchanged_publications_give_one_fingerprint_at_any_lag(lag: int) -> None:
    reference = window_pins(_request(_pins(FIRST, LAST), LAST + timedelta(days=3)))
    result = window_pins(_request(_pins(FIRST, LAST), LAST + timedelta(days=lag)))
    assert result.input_fingerprint == reference.input_fingerprint


def test_a_272_day_lag_runs_and_a_273_day_lag_is_too_old() -> None:
    assert window_pins(_request(_pins(FIRST, LAST), LAST + timedelta(days=272))).held is None
    old = window_pins(_request(_pins(FIRST, LAST), LAST + timedelta(days=273)))
    assert old.held is HeldCategory.INPUT_END_TOO_OLD
    assert old.input_fingerprint is None


def test_no_complete_week_is_held() -> None:
    covered_end = FIRST + timedelta(days=5)
    result = window_pins(
        _request(_pins(FIRST, covered_end), covered_end + timedelta(days=3), covered_end)
    )
    assert result.held is HeldCategory.NO_COMPLETE_WEEK
