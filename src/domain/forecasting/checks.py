# SPDX-License-Identifier: MIT-0
"""Whether a forecast is fit to publish (adapted from the source engine's forecast checks).

Typed results replace the source's `Result` types. Order (design §4d): shape and finiteness on
the raw values, then, on stored values, unordered bands before negative values.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from domain.forecasting.models import QuantileForecast, SkipReason


@dataclass(frozen=True)
class ShapeCheck:
    """The result of `check_forecast_shape`: `reason` is None when the forecast passes."""

    reason: SkipReason | None
    detail: str | None = None


def check_forecast_shape(forecast: QuantileForecast, horizon_weeks: int) -> ShapeCheck:
    """Pass only when every quantile has `horizon_weeks` values and every value is finite."""
    bands = (forecast.p10, forecast.p50, forecast.p90)
    if any(len(band) != horizon_weeks for band in bands):
        return ShapeCheck(
            SkipReason.FORECAST_SHAPE_INVALID,
            f"{forecast.canonical_asin}: expected {horizon_weeks} weeks",
        )
    if not all(math.isfinite(value) for band in bands for value in band):
        return ShapeCheck(SkipReason.FORECAST_NOT_FINITE, forecast.canonical_asin)
    return ShapeCheck(None)


def first_unordered_week(
    p10: Sequence[float | Decimal], p50: Sequence[float | Decimal], p90: Sequence[float | Decimal]
) -> int | None:
    """Return the first week where p10 <= p50 <= p90 fails, or None when all are ordered.

    Chronos-2 doesn't order its quantile heads, so a near-zero series can cross; such a forecast
    is withheld, never sorted.
    """
    for week, (low, mid, high) in enumerate(zip(p10, p50, p90, strict=True)):
        if not low <= mid <= high:
            return week
    return None


def first_negative_week(
    p10: Sequence[float | Decimal], p50: Sequence[float | Decimal], p90: Sequence[float | Decimal]
) -> int | None:
    """Return the first week holding a value below zero, or None."""
    for week, values in enumerate(zip(p10, p50, p90, strict=True)):
        if any(value < 0 for value in values):
            return week
    return None
