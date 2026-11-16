# SPDX-License-Identifier: MIT-0
"""SeasonalNaive, zero-censoring and the ETS request and result values (design §5a).

A wrong lag or residual would score and show a comparator band that isn't the declared model;
a missed censor would publish negative demand; a short history would let Chronos-2 compete
against a comparator that can't be tested.
"""

from __future__ import annotations

import math

import pytest

from domain.forecasting.comparators import (
    EtsRequest,
    EtsResult,
    SeasonalNaiveRequest,
    SeasonalNaiveResult,
    censor_at_zero,
    seasonal_naive,
)
from domain.forecasting.models import QuantileForecast, SeriesHistory, SkipReason

pytestmark = pytest.mark.unit

SEASON = 52
MIN_RESIDUALS = 13
HORIZON = 13


def _series(values: list[float | None]) -> SeriesHistory:
    return SeriesHistory(
        "amazon_asin:b0fixtur01", "units_ordered", tuple(values), tuple(1.0 for _ in values)
    )


def _request(values: list[float | None]) -> SeasonalNaiveRequest:
    return SeasonalNaiveRequest(_series(values), HORIZON, MIN_RESIDUALS)


def _ramp() -> list[float | None]:
    """One season of `i`, then `2j` for the next 13 weeks: the residuals are exactly 0 to 12."""
    return [float(i) for i in range(SEASON)] + [2.0 * j for j in range(MIN_RESIDUALS)]


def test_p50_is_the_value_52_weeks_earlier_and_the_band_adds_the_residual_spread() -> None:
    values = _ramp()
    result = seasonal_naive(_request(values))
    assert result.unavailable_reason is None
    assert result.forecast is not None
    start = len(values) - SEASON
    assert result.forecast.p50 == tuple(values[start : start + HORIZON])
    # Residuals 0..12: q0.1 = 1.2, q0.5 = 6, q0.9 = 10.8 by linear interpolation.
    assert result.forecast.p10 == pytest.approx([p50 - 4.8 for p50 in result.forecast.p50])
    assert result.forecast.p90 == pytest.approx([p50 + 4.8 for p50 in result.forecast.p50])
    assert result.lag_known == (True,) * HORIZON


def test_a_backtest_needs_a_78_week_context_at_the_defaults() -> None:
    holdout = 13
    context = 78
    enough = [float(week % 7) for week in range(context)][:-holdout]
    short = [float(week % 7) for week in range(context - 1)][:-holdout]
    assert seasonal_naive(_request(enough)).unavailable_reason is None
    assert seasonal_naive(_request(short)) == SeasonalNaiveResult(
        None, (), SkipReason.HISTORY_TOO_SHORT_FOR_SEASON
    )


def test_a_residual_pair_with_an_unknown_week_is_skipped() -> None:
    values = _ramp()
    values[SEASON] = None  # The first residual pair loses its later week: 12 remain.
    assert seasonal_naive(_request(values)) == SeasonalNaiveResult(
        None, (), SkipReason.SEASONAL_RESIDUALS_TOO_FEW
    )
    longer = [*_ramp(), 24.0]
    longer[SEASON] = None  # 66 weeks: one pair lost, 13 remain.
    assert seasonal_naive(_request(longer)).unavailable_reason is None


def test_a_week_with_an_unknown_lag_is_nan_and_marked_unknown() -> None:
    values = _ramp()
    start = len(values) - SEASON
    values[start + 2] = None
    result = seasonal_naive(_request(values))
    assert result.forecast is not None
    assert result.lag_known == tuple(week != 2 for week in range(HORIZON))
    for band in (result.forecast.p10, result.forecast.p50, result.forecast.p90):
        assert math.isnan(band[2])
        assert all(math.isfinite(value) for week, value in enumerate(band) if week != 2)


def test_censoring_floors_every_quantile_at_zero_and_keeps_the_band_ordered() -> None:
    # An intermittent product: last year's week was 0 and the residuals spread both ways.
    values: list[float | None] = [0.0] * SEASON + [
        float(v) for v in (1, 3, 2, 5, 4, 2, 6, 3, 4, 7, 1, 2, 6)
    ]
    raw = seasonal_naive(_request(values)).forecast
    assert raw is not None
    # Residual q0.1 - q0.5 = 1.2 - 3 under a lag of 0 units.
    assert raw.p10 == pytest.approx([-1.8] * HORIZON)
    censored = censor_at_zero(raw)
    for low, mid, high in zip(censored.p10, censored.p50, censored.p90, strict=True):
        assert 0 <= low <= mid <= high
    assert censored.p90 == raw.p90


def test_censoring_leaves_nan_for_the_finite_check() -> None:
    forecast = QuantileForecast(
        "amazon_asin:b0fixtur01", "units_ordered", (math.nan,), (-1.0,), (2.0,)
    )
    censored = censor_at_zero(forecast)
    assert math.isnan(censored.p10[0])
    assert (censored.p50, censored.p90) == ((0.0,), (2.0,))


def test_requests_refuse_a_horizon_outside_one_season() -> None:
    with pytest.raises(ValueError, match="at most 52"):
        SeasonalNaiveRequest(_series(_ramp()), 53, MIN_RESIDUALS)
    with pytest.raises(ValueError, match="at least 1"):
        SeasonalNaiveRequest(_series(_ramp()), 0, MIN_RESIDUALS)
    with pytest.raises(ValueError, match="min_residuals"):
        SeasonalNaiveRequest(_series(_ramp()), HORIZON, 0)
    with pytest.raises(ValueError, match="at least 1"):
        EtsRequest(_series(_ramp()), 0)


def test_results_hold_exactly_one_of_a_forecast_and_a_reason() -> None:
    forecast = QuantileForecast("amazon_asin:b0fixtur01", "units_ordered", (1.0,), (2.0,), (3.0,))
    with pytest.raises(ValueError, match="exactly one"):
        EtsResult(forecast, SkipReason.ETS_FIT_FAILED)
    with pytest.raises(ValueError, match="exactly one"):
        EtsResult(None, None)
    with pytest.raises(ValueError, match="exactly one"):
        SeasonalNaiveResult(None, (), None)
    with pytest.raises(ValueError, match="no lag weeks"):
        SeasonalNaiveResult(None, (True,), SkipReason.SEASONAL_RESIDUALS_TOO_FEW)
