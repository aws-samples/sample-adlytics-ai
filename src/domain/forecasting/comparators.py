# SPDX-License-Identifier: MIT-0
"""The two simple models Chronos-2 must beat: SeasonalNaive here, ETS through its port (§5a).

SeasonalNaive is pure Python. Its p50 for a forecast week is the value 52 weeks earlier, and
its band adds the 10th and 90th percentile seasonal residuals, each less the median residual,
so it is ordered by construction. The residuals are `y[s] - y[s - 52]` over every pair in the
history where both weeks are known. Percentiles interpolate linearly between order statistics.

- A week whose lag is unknown holds NaN in every band and `lag_known` False. The caller never
  scores it (§3d) and, at the forecast origin, it makes SeasonalNaive not publishable
  (`SEASONAL_LAG_UNKNOWN_AT_ORIGIN`, §5c).
- With fewer weeks than one season plus `min_residuals`, known or not, SeasonalNaive is
  unavailable as `HISTORY_TOO_SHORT_FOR_SEASON`. With enough weeks but fewer known pairs, it is
  `SEASONAL_RESIDUALS_TOO_FEW`. At the defaults a backtest needs a 78-week context: 52 + 13
  residual weeks before the 13-week holdout.

`censor_at_zero()` gives the quantiles of `max(0, Y)` for both simple models, never Chronos-2.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from domain.forecasting.models import QuantileForecast, SeriesHistory, SkipReason

SEASON_WEEKS = 52
"""The SeasonalNaive lag: the same week a year earlier (design §5a)."""


def _check_horizon(horizon_weeks: int, maximum: int | None = None) -> None:
    if isinstance(horizon_weeks, bool) or horizon_weeks < 1:
        raise ValueError(f"horizon_weeks must be at least 1, got {horizon_weeks!r}")
    if maximum is not None and horizon_weeks > maximum:
        raise ValueError(f"horizon_weeks must be at most {maximum}, got {horizon_weeks!r}")


@dataclass(frozen=True)
class SeasonalNaiveRequest:
    """One series' weeks before the origin, oldest first, and the weeks to forecast after it."""

    series: SeriesHistory
    horizon_weeks: int
    min_residuals: int

    def __post_init__(self) -> None:
        # Every forecast week's lag must fall inside the history, so the horizon is one season.
        _check_horizon(self.horizon_weeks, SEASON_WEEKS)
        if isinstance(self.min_residuals, bool) or self.min_residuals < 1:
            raise ValueError(f"min_residuals must be at least 1, got {self.min_residuals!r}")


@dataclass(frozen=True)
class SeasonalNaiveResult:
    """The uncensored SeasonalNaive forecast and which weeks' lags are known, or why not."""

    forecast: QuantileForecast | None
    lag_known: tuple[bool, ...]
    unavailable_reason: SkipReason | None

    def __post_init__(self) -> None:
        if (self.forecast is None) == (self.unavailable_reason is None):
            raise ValueError("exactly one of forecast and unavailable_reason is set")
        if self.forecast is None and self.lag_known:
            raise ValueError("an unavailable SeasonalNaive has no lag weeks")


def _quantile(ordered: list[float], level: float) -> float:
    """Linear interpolation between order statistics (position `(n - 1) x level`)."""
    position = (len(ordered) - 1) * level
    lower = math.floor(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def seasonal_naive(request: SeasonalNaiveRequest) -> SeasonalNaiveResult:
    """Forecast `horizon_weeks` after the history from the value 52 weeks earlier (§5a)."""
    series = request.series
    values = series.values
    if len(values) < SEASON_WEEKS + request.min_residuals:
        return SeasonalNaiveResult(None, (), SkipReason.HISTORY_TOO_SHORT_FOR_SEASON)
    residuals = sorted(
        current - lagged
        for lagged, current in zip(values, values[SEASON_WEEKS:], strict=False)
        if lagged is not None and current is not None
    )
    if len(residuals) < request.min_residuals:
        return SeasonalNaiveResult(None, (), SkipReason.SEASONAL_RESIDUALS_TOO_FEW)
    median = _quantile(residuals, 0.5)
    low = _quantile(residuals, 0.1) - median
    high = _quantile(residuals, 0.9) - median
    start = len(values) - SEASON_WEEKS
    lags = values[start : start + request.horizon_weeks]
    p50 = tuple(math.nan if lag is None else lag for lag in lags)
    forecast = QuantileForecast(
        series.canonical_asin,
        series.target,
        tuple(value + low for value in p50),
        p50,
        tuple(value + high for value in p50),
    )
    return SeasonalNaiveResult(forecast, tuple(lag is not None for lag in lags), None)


def censor_at_zero(forecast: QuantileForecast) -> QuantileForecast:
    """Return the quantiles of `max(0, Y)`: p10, p50 and p90 each floored at 0 (R2-06).

    Monotone, so an ordered band stays ordered; NaN stays NaN for the finite check to refuse.
    """

    def floor(band: tuple[float, ...]) -> tuple[float, ...]:
        return tuple(value if math.isnan(value) else max(value, 0.0) for value in band)

    return QuantileForecast(
        forecast.canonical_asin,
        forecast.target,
        floor(forecast.p10),
        floor(forecast.p50),
        floor(forecast.p90),
    )


@dataclass(frozen=True)
class EtsRequest:
    """One series' context for the state-space ETS fit, unknown weeks as `None` (§3d, §5a)."""

    series: SeriesHistory
    horizon_weeks: int

    def __post_init__(self) -> None:
        _check_horizon(self.horizon_weeks)


@dataclass(frozen=True)
class EtsResult:
    """The uncensored ETS forecast (mean and its 80% interval), or why the fit is refused."""

    forecast: QuantileForecast | None
    failure: SkipReason | None

    def __post_init__(self) -> None:
        if (self.forecast is None) == (self.failure is None):
            raise ValueError("exactly one of forecast and failure is set")
