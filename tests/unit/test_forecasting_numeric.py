# SPDX-License-Identifier: MIT-0
"""Forecast storage and display rounding (design §4d): a wrong rule publishes a wrong figure."""

from __future__ import annotations

from decimal import Decimal

import pytest

from domain.forecasting.numeric import (
    display_percent_change,
    display_whole,
    stored_text,
    stored_value,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("raw", "stored"),
    [
        (0.00005, "0.0000"),  # half to even: down to the even 0
        (0.00015, "0.0002"),  # half to even: up to the even 2
        (-0.00004, "0.0000"),  # a negative zero is stored as 0.0000
        (-0.00005, "0.0000"),
        (-0.00015, "-0.0002"),  # below zero after rounding: not publishable
        (12.5, "12.5000"),
    ],
)
def test_stored_values_have_four_places_half_to_even_and_no_negative_zero(
    raw: float, stored: str
) -> None:
    assert stored_text(stored_value(raw)) == stored


def test_a_non_finite_value_is_refused_before_it_is_stored() -> None:
    with pytest.raises(ValueError, match="finite"):
        stored_value(float("nan"))
    with pytest.raises(ValueError, match="finite"):
        stored_value(Decimal("Infinity"))


@pytest.mark.parametrize(("value", "shown"), [("2.5", 3), ("-2.5", -3), ("2.4999", 2), ("-0.4", 0)])
def test_display_is_whole_half_away_from_zero_and_never_minus_zero(value: str, shown: int) -> None:
    assert display_whole(Decimal(value)) == shown
    assert str(display_whole(Decimal(value))) != "-0"


def test_a_13_week_total_is_rounded_once_after_the_sum_of_stored_weeks() -> None:
    """13 weeks of 0.4 units: the total shows 5 units, the sum of shown weeks would show 0."""
    weeks = [stored_value(0.4)] * 13
    assert display_whole(sum(weeks, Decimal(0))) == 5
    assert sum(display_whole(week) for week in weeks) == 0


def test_percent_change_is_whole_percent_and_absent_from_a_zero_base() -> None:
    assert display_percent_change(Decimal("12.0000"), Decimal("8.0000")) == 50
    assert display_percent_change(Decimal("1.0000"), Decimal("0.0000")) is None
