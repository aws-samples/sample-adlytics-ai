# SPDX-License-Identifier: MIT-0
"""Forecast fitness checks, ported from the source engine's check tests.

A broken band that passed would publish a forecast with crossed or non-finite quantiles.
"""

from __future__ import annotations

import math
from decimal import Decimal

import pytest

from domain.forecasting.checks import (
    check_forecast_shape,
    first_negative_week,
    first_unordered_week,
)
from domain.forecasting.models import QuantileForecast, SkipReason

pytestmark = pytest.mark.unit


def _forecast(p10: list[float], p50: list[float], p90: list[float]) -> QuantileForecast:
    return QuantileForecast(
        "amazon_asin:b0fixtur01", "units_ordered", tuple(p10), tuple(p50), tuple(p90)
    )


def test_a_whole_finite_forecast_passes_the_shape_check() -> None:
    assert check_forecast_shape(_forecast([1.0] * 3, [2.0] * 3, [3.0] * 3), 3).reason is None


def test_a_short_band_is_refused_as_shape_invalid() -> None:
    check = check_forecast_shape(_forecast([1.0] * 2, [2.0] * 3, [3.0] * 3), 3)
    assert check.reason is SkipReason.FORECAST_SHAPE_INVALID


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_a_non_finite_value_is_refused(bad: float) -> None:
    check = check_forecast_shape(_forecast([1.0, bad, 1.0], [2.0] * 3, [3.0] * 3), 3)
    assert check.reason is SkipReason.FORECAST_NOT_FINITE


def test_the_first_unordered_week_is_found_and_equal_bands_are_ordered() -> None:
    assert first_unordered_week([1, 2, 2], [1, 3, 2], [1, 4, 1]) == 2
    assert first_unordered_week([1, 1], [1, 1], [1, 1]) is None


def test_the_first_negative_week_reads_stored_decimals() -> None:
    zero = Decimal("0.0000")
    assert first_negative_week([zero, Decimal("-0.0002")], [zero, zero], [zero, zero]) == 1
    assert first_negative_week([zero], [zero], [zero]) is None
