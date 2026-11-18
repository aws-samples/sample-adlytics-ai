# SPDX-License-Identifier: MIT-0
"""Hand-built weekly aggregates for the demand-forecast domain tests (fictional ASINs)."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from functools import cache
from pathlib import Path

from adapters.outbound.demand_forecast_policy import load_policy
from domain.forecasting.boundary import (
    IntervalV1,
    WeeklyTargetAggregateRowV1,
    WeeklyTrafficAggregateRowV1,
)
from domain.forecasting.comparators import EtsRequest, EtsResult
from domain.forecasting.models import (
    ForecastBatchRequest,
    ForecastBatchResult,
    QuantileForecast,
    SeriesHistory,
    SkipReason,
)
from domain.forecasting.policy import MONEY_TARGETS, TARGETS, DemandForecastPolicy
from domain.forecasting.weekly_series import (
    WeeklySeriesRequest,
    WeeklySeriesResult,
    build_weekly_series,
    week_grid,
)

ROOT = Path(__file__).resolve().parents[2]
INPUT_END = date(2026, 10, 3)  # a Saturday
WEEKS = 104
STARTS = week_grid(INPUT_END, WEEKS)
FULL = (IntervalV1(start=STARTS[0], end=INPUT_END),)


@cache
def policy() -> DemandForecastPolicy:
    """Return the repository's policy contract, read once."""
    return load_policy(ROOT / "contracts/forecasting/demand-forecast.yaml").policy


def asin(n: int) -> str:
    """Return a fictional canonical ASIN."""
    return f"amazon_asin:b0test{n:04d}"


def target_row(  # noqa: PLR0913 -- one aggregate row with keyword overrides
    product: str,
    week: int,
    target: str,
    *,
    value: float | None,
    rows: int = 7,
    nulls: int = 0,
    currency: str | None = "USD",
) -> WeeklyTargetAggregateRowV1:
    """One weekly target aggregate at grid week `week`."""
    money = target in MONEY_TARGETS
    return WeeklyTargetAggregateRowV1(
        schema_version=1,
        canonical_asin=product,
        week_start=STARTS[week],
        target=target,  # pyright: ignore[reportArgumentType]
        value_sum=None if value is None else Decimal(str(value)),
        days_with_row=rows,
        days_with_null=nulls,
        min_currency=currency if money else None,
        max_currency=currency if money else None,
    )


def product_rows(
    product: str, weeks: Iterable[int], value: float | Callable[[int, str], float]
) -> list[WeeklyTargetAggregateRowV1]:
    """Full seven-day rows for every target in `weeks`, valued `value` or `value(week, target)`."""
    return [
        target_row(product, w, t, value=value(w, t) if callable(value) else value)
        for w in weeks
        for t in TARGETS
    ]


def traffic_rows(product: str, weeks: Iterable[int]) -> list[WeeklyTrafficAggregateRowV1]:
    """Seven-day glance-view rows of 100 a week."""
    return [
        WeeklyTrafficAggregateRowV1(
            schema_version=1,
            canonical_asin=product,
            week_start=STARTS[w],
            glance_views_sum=100,
            days_with_traffic_row=7,
        )
        for w in weeks
    ]


def series(  # noqa: PLR0913 -- the rows and three keyword overrides
    rows: Iterable[WeeklyTargetAggregateRowV1],
    *,
    traffic: Iterable[WeeklyTrafficAggregateRowV1] = (),
    sales_coverage: tuple[IntervalV1, ...] = FULL,
    traffic_coverage: tuple[IntervalV1, ...] = FULL,
) -> WeeklySeriesResult:
    """Classify the rows on the 104-week grid ending INPUT_END."""
    return build_weekly_series(
        WeeklySeriesRequest(
            input_end=INPUT_END,
            history_weeks=WEEKS,
            sales_coverage=sales_coverage,
            traffic_coverage=traffic_coverage,
            target_rows=tuple(rows),
            traffic_rows=tuple(traffic),
            missing_weeks=policy().missing_weeks,
        )
    )


def without_day(day: date) -> tuple[IntervalV1, ...]:
    """Full coverage except one unlanded day."""
    return (
        IntervalV1(start=STARTS[0], end=day - timedelta(days=1)),
        IntervalV1(start=day + timedelta(days=1), end=INPUT_END),
    )


def _flat(item: SeriesHistory, horizon: int, band: tuple[float, float]) -> QuantileForecast:
    level, spread = band
    return QuantileForecast(
        item.canonical_asin,
        item.target,
        (level - spread,) * horizon,
        (level,) * horizon,
        (level + spread,) * horizon,
    )


def _mean(item: SeriesHistory) -> float:
    known = [v for v in item.values if v is not None]
    return sum(known) / len(known)


@dataclass
class FakeModel:
    """Chronos-2 stand-in: each series flat at its mean known value, p10 and p90 two units off.

    `shift(asin, context_weeks)` moves one series' level, so a test can move only the forecast
    origin (104 context weeks) and leave the backtest (91) alone.
    """

    shift: Callable[[str, int], float] = lambda _asin, _weeks: 0.0
    calls: list[tuple[int, int]] = field(default_factory=list[tuple[int, int]])

    def __call__(self, request: ForecastBatchRequest, /) -> ForecastBatchResult:
        self.calls.append((len(request.series), request.horizon_weeks))
        return ForecastBatchResult(
            tuple(
                _flat(
                    item,
                    request.horizon_weeks,
                    (_mean(item) + self.shift(item.canonical_asin, len(item.values)), 2.0),
                )
                for item in request.series
            )
        )


@dataclass
class FakeEts:
    """ETS stand-in: flat at the mean known value, p10 and p90 `spread` units off.

    The band is wider than FakeModel's, so on the same level Chronos-2 has the lower WQL. Series
    in `unconverged` return `ETS_FIT_FAILED`, as the adapter does on a `ConvergenceWarning`.
    """

    spread: float = 5.0
    unconverged: frozenset[str] = frozenset()
    shift: Callable[[str, int], float] = lambda _asin, _weeks: 0.0

    def __call__(self, request: EtsRequest, /) -> EtsResult:
        item = request.series
        if item.canonical_asin in self.unconverged:
            return EtsResult(None, SkipReason.ETS_FIT_FAILED)
        level = _mean(item) + self.shift(item.canonical_asin, len(item.values))
        return EtsResult(_flat(item, request.horizon_weeks, (level, self.spread)), None)
