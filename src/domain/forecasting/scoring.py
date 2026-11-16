# SPDX-License-Identifier: MIT-0
"""Backtest scores on one holdout (adapted from the source engine's backtest scoring; design §5b).

The holdout is the last `holdout_weeks` weeks of a series' context; each model forecasts them
from the weeks before. Every model is scored on one **scored week set**: the holdout weeks whose
actual is known (and, while SeasonalNaive competes, whose seasonal lag is known; slice 2).

- WQL: for each quantile q in (0.1, 0.5, 0.9), 2 x the sum of pinball losses over the scored
  weeks ÷ the sum of |actual|, averaged over the three. Undefined when every scored actual is 0.
- MASE: mean |actual - p50| over the scored weeks ÷ the mean |y[t] - y[t-1]| over adjacent
  pairs before the holdout where **both** weeks are known (R3-03). With fewer than
  `min_scale_pairs` such pairs MASE is missing as `MASE_SCALE_UNKNOWN`; a known scale of 0 is
  `HISTORY_UNCHANGING`. Weeks are never joined across a gap.
- weeks_in_range: scored weeks whose actual is inside p10 to p90, ends included.

Scores are stored as four-place decimal strings (the numeric policy's storage rule).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from itertools import pairwise

from domain.forecasting.boundary import BacktestScoreV1
from domain.forecasting.checks import first_unordered_week
from domain.forecasting.models import AccuracyGap, SkipReason
from domain.forecasting.numeric import stored_text
from domain.forecasting.policy import QUANTILES


@dataclass(frozen=True)
class MaseScale:
    """The naive scale over known adjacent pairs, or the gap that leaves MASE missing."""

    scale: float | None
    gap: AccuracyGap | None


def mase_scale(history: Sequence[float | None], min_scale_pairs: int) -> MaseScale:
    """Mean absolute week-over-week change over adjacent pairs where both weeks are known."""
    changes = [
        abs(current - previous)
        for previous, current in pairwise(history)
        if previous is not None and current is not None
    ]
    if len(changes) < min_scale_pairs:
        return MaseScale(None, AccuracyGap.MASE_SCALE_UNKNOWN)
    scale = sum(changes) / len(changes)
    if scale <= 0:
        return MaseScale(None, AccuracyGap.HISTORY_UNCHANGING)
    return MaseScale(scale, None)


def scored_week_set(
    actuals: Sequence[float | None], lag_known: Sequence[bool] | None = None
) -> tuple[int, ...]:
    """Holdout positions with a known actual (and a known seasonal lag, when lags are given)."""
    return tuple(
        week
        for week, actual in enumerate(actuals)
        if actual is not None and (lag_known is None or lag_known[week])
    )


def pinball(quantile: float, actual: float, forecast: float) -> float:
    """Return the pinball loss of `forecast` at `quantile` for `actual`."""
    if actual >= forecast:
        return quantile * (actual - forecast)
    return (1 - quantile) * (forecast - actual)


@dataclass(frozen=True)
class BacktestScoreRequest:
    """One model's holdout band (stored values) against the actuals, on the scored week set."""

    history: tuple[float | None, ...]
    actuals: tuple[float | None, ...]
    p10: tuple[Decimal, ...]
    p50: tuple[Decimal, ...]
    p90: tuple[Decimal, ...]
    scored_weeks: tuple[int, ...]
    min_scale_pairs: int

    def __post_init__(self) -> None:
        widths = {len(self.actuals), len(self.p10), len(self.p50), len(self.p90)}
        if len(widths) != 1:
            raise ValueError("actuals and every band must cover the same holdout weeks")
        if any(self.actuals[week] is None for week in self.scored_weeks):
            raise ValueError("a scored week must have a known actual")


@dataclass(frozen=True)
class BacktestOutcome:
    """A valid model's score, or the reason it is not valid for the gate."""

    score: BacktestScoreV1 | None
    invalid_reason: SkipReason | None


def score_backtest(request: BacktestScoreRequest) -> BacktestOutcome:
    """Score a holdout band; an unordered scored week or an all-zero holdout makes it invalid."""
    weeks = request.scored_weeks

    def pick(band: tuple[Decimal, ...]) -> tuple[Decimal, ...]:
        return tuple(band[week] for week in weeks)

    p10, p50, p90 = pick(request.p10), pick(request.p50), pick(request.p90)
    if first_unordered_week(p10, p50, p90) is not None:
        return BacktestOutcome(None, SkipReason.BACKTEST_BAND_UNORDERED)
    actuals = tuple(float(request.actuals[week] or 0.0) for week in weeks)
    weight = sum(abs(actual) for actual in actuals)
    if weight == 0:
        return BacktestOutcome(None, SkipReason.HOLDOUT_ALL_ZERO)
    losses = [
        2
        * sum(pinball(q, actual, float(value)) for actual, value in zip(actuals, band, strict=True))
        / weight
        for q, band in zip(QUANTILES, (p10, p50, p90), strict=True)
    ]
    wql = sum(losses) / len(losses)
    scale = mase_scale(request.history, request.min_scale_pairs)
    mase: str | None = None
    if scale.scale is not None:
        error = sum(abs(a - float(m)) for a, m in zip(actuals, p50, strict=True)) / len(actuals)
        mase = stored_text(Decimal(repr(error / scale.scale)))
    in_range = sum(
        1 for actual, low, high in zip(actuals, p10, p90, strict=True) if low <= actual <= high
    )
    return BacktestOutcome(
        BacktestScoreV1(
            wql=stored_text(Decimal(repr(wql))),
            mase=mase,
            mase_gap=scale.gap,
            weeks_in_range=in_range,
            weeks_scored=len(weeks),
        ),
        None,
    )
