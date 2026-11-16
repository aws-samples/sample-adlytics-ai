# SPDX-License-Identifier: MIT-0
"""Values the demand-forecast domain reasons about (adapted from the source engine's models).

Series are keyed by T's canonical product key (`amazon_asin:<lowercase>`), never a warehouse
column name. A `None` week is an `unknown` week (design §3b), passed to Chronos-2 as NaN.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

CHRONOS2_MODEL_ID = "amazon/chronos-2"
"""The model identity recorded in the run fingerprint and run report (R-11's Hugging Face ID)."""


class ModelName(StrEnum):
    """The three candidate models of a target (design §5a)."""

    CHRONOS2 = "chronos2"
    SEASONAL_NAIVE = "seasonal_naive"
    ETS = "ets"


class SkipReason(StrEnum):
    """Why a model or a series produced no publishable forecast (design §3, §5)."""

    ASIN_INACTIVE = "ASIN_INACTIVE"
    """No observed day for any target in the `activity_window_weeks` weeks ending `input_end`."""
    TARGET_VALUE_MISSING = "TARGET_VALUE_MISSING"
    """Too little known context, and the target's history holds a declared-null value."""
    INSUFFICIENT_CONTEXT = "INSUFFICIENT_CONTEXT"
    HOLDOUT_UNKNOWN = "HOLDOUT_UNKNOWN"
    HOLDOUT_ALL_ZERO = "HOLDOUT_ALL_ZERO"
    BACKTEST_BAND_UNORDERED = "BACKTEST_BAND_UNORDERED"
    SEASONAL_LAG_UNKNOWN = "SEASONAL_LAG_UNKNOWN"
    SEASONAL_LAG_UNKNOWN_AT_ORIGIN = "SEASONAL_LAG_UNKNOWN_AT_ORIGIN"
    """A forecast week's `y[t - 52]` is unknown, so SeasonalNaive is not publishable (§5c)."""
    SEASONAL_RESIDUALS_TOO_FEW = "SEASONAL_RESIDUALS_TOO_FEW"
    HISTORY_TOO_SHORT_FOR_SEASON = "HISTORY_TOO_SHORT_FOR_SEASON"
    """Fewer weeks than one season plus `min_seasonal_residuals`, known or not (§5a)."""
    ETS_INSUFFICIENT_KNOWN = "ETS_INSUFFICIENT_KNOWN"
    ETS_FIT_FAILED = "ETS_FIT_FAILED"
    """The state-space ETS fit raised a `ConvergenceWarning`; it is never scored or shown (§5a)."""
    ACCURACY_NOT_TESTABLE = "ACCURACY_NOT_TESTABLE"
    """No simple model is valid, so nothing is shown (gate `no_valid_simple_model`, §5c)."""
    FORECAST_NOT_PUBLISHABLE = "FORECAST_NOT_PUBLISHABLE"
    """A simple model is valid but none is publishable; `gate_reason` names the check (§5c)."""
    FORECAST_SHAPE_INVALID = "FORECAST_SHAPE_INVALID"
    FORECAST_NOT_FINITE = "FORECAST_NOT_FINITE"
    FORECAST_BAND_UNORDERED = "FORECAST_BAND_UNORDERED"
    FORECAST_NEGATIVE = "FORECAST_NEGATIVE"
    PRODUCT_KEY_INVALID = "PRODUCT_KEY_INVALID"
    DISPLAY_ASIN_COLLISION = "DISPLAY_ASIN_COLLISION"
    CURRENCY_MIXED = "CURRENCY_MIXED"
    MODEL_NOT_RUN = "MODEL_NOT_RUN"
    """Slice 1 only: the comparators are not run until slice 2's gate; retired with it."""


class AccuracyGap(StrEnum):
    """Why a valid backtest has no MASE (design §5b); MASE is reported, never gated on."""

    HISTORY_UNCHANGING = "HISTORY_UNCHANGING"
    MASE_SCALE_UNKNOWN = "MASE_SCALE_UNKNOWN"


class WeekState(StrEnum):
    """The three states of a target week (design §3b)."""

    OBSERVED = "observed"
    INFERRED_ZERO = "inferred_zero"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class SeriesHistory:
    """One series' weekly context for one target, oldest first; `None` is an unknown week.

    `covariate` holds glance views week for week with `values` (past covariate only).
    """

    canonical_asin: str
    target: str
    values: tuple[float | None, ...]
    covariate: tuple[float | None, ...]

    def __post_init__(self) -> None:
        if not self.values:
            raise ValueError(f"{self.canonical_asin}: a series needs at least one week")
        if len(self.covariate) != len(self.values):
            raise ValueError(f"{self.canonical_asin}: covariate length differs from the target")
        for value in (*self.values, *self.covariate):
            if value is not None and not math.isfinite(value):
                raise ValueError(f"{self.canonical_asin}: a known week must be finite")


@dataclass(frozen=True)
class ForecastBatchRequest:
    """The series of one model call (one target, one origin), with the horizon in weeks.

    With `cross_learning`, the series of one call inform each other, so batch membership is
    policy (design §4c). The caller plans batches; the adapter never re-slices them.
    """

    series: tuple[SeriesHistory, ...]
    horizon_weeks: int
    cross_learning: bool

    def __post_init__(self) -> None:
        if not self.series:
            raise ValueError("a batch needs at least one series")
        if self.horizon_weeks < 1:
            raise ValueError("horizon_weeks must be at least 1")
        if len({series.target for series in self.series}) != 1:
            raise ValueError("a batch never mixes targets")


@dataclass(frozen=True)
class QuantileForecast:
    """One series' forecast for one target: p10, p50 and p90 per future week, nearest first."""

    canonical_asin: str
    target: str
    p10: tuple[float, ...]
    p50: tuple[float, ...]
    p90: tuple[float, ...]


@dataclass(frozen=True)
class ForecastBatchResult:
    """The model's forecasts for one batch, in the request's series order."""

    forecasts: tuple[QuantileForecast, ...]
