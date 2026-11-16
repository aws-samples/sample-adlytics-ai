# SPDX-License-Identifier: MIT-0
"""Backtest scores planners read, on hand-computed cases (ported), plus unknown weeks (R3-03)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from domain.forecasting.models import AccuracyGap, SkipReason
from domain.forecasting.scoring import (
    BacktestScoreRequest,
    mase_scale,
    pinball,
    score_backtest,
    scored_week_set,
)

pytestmark = pytest.mark.unit

# History before the holdout: week-over-week changes 2, 1, 4, so the MASE scale is 7 / 3.
HISTORY: tuple[float | None, ...] = (10.0, 12.0, 11.0, 15.0)
ACTUALS: tuple[float | None, ...] = (14.0, 16.0, 25.0)


def _d(*values: float) -> tuple[Decimal, ...]:
    return tuple(Decimal(str(v)) for v in values)


P10, P50, P90 = _d(12.0, 15.0, 18.0), _d(13.0, 18.0, 20.0), _d(15.0, 20.0, 22.0)


def _request(  # noqa: PLR0913 -- keyword overrides of one fixture
    *,
    history: tuple[float | None, ...] = HISTORY,
    actuals: tuple[float | None, ...] = ACTUALS,
    p10: tuple[Decimal, ...] = P10,
    p50: tuple[Decimal, ...] = P50,
    p90: tuple[Decimal, ...] = P90,
    min_pairs: int = 3,
) -> BacktestScoreRequest:
    return BacktestScoreRequest(
        history=history,
        actuals=actuals,
        p10=p10,
        p50=p50,
        p90=p90,
        scored_weeks=scored_week_set(actuals),
        min_scale_pairs=min_pairs,
    )


def test_mase_is_the_mean_absolute_p50_error_over_the_mean_week_over_week_change() -> None:
    """Errors 1, 2, 5 (mean 8/3) over the scale 7/3: 8/7 = 1.142857, stored as 1.1429."""
    assert mase_scale(HISTORY, 3).scale == pytest.approx(7 / 3)
    score = score_backtest(_request()).score
    assert score is not None
    assert score.mase == "1.1429"


def test_pinball_loss_weights_under_and_over_forecasts_by_the_quantile() -> None:
    assert pinball(0.1, 14.0, 12.0) == pytest.approx(0.2)
    assert pinball(0.9, 14.0, 15.0) == pytest.approx(0.1)
    assert pinball(0.5, 16.0, 18.0) == pytest.approx(1.0)


def test_wql_is_twice_the_summed_pinball_loss_over_the_summed_actuals_averaged() -> None:
    """Pinball sums 1.0, 4.0 and 3.2 over actuals summing to 55: (16.4 / 55) / 3 = 0.0994."""
    score = score_backtest(_request()).score
    assert score is not None
    assert (score.wql, score.weeks_in_range, score.weeks_scored, score.mase_gap) == (
        "0.0994",
        2,
        3,
        None,
    )


def test_weeks_in_range_counts_actuals_inside_p10_to_p90_ends_included() -> None:
    edges = _request(p10=_d(14.0, 15.0, 18.0), p50=_d(15.0, 15.0, 20.0), p90=_d(15.0, 16.0, 25.0))
    score = score_backtest(edges).score
    assert score is not None
    assert score.weeks_in_range == 3


def test_an_unchanging_history_leaves_mase_empty_and_says_why() -> None:
    score = score_backtest(_request(history=(5.0, 5.0, 5.0, 5.0))).score
    assert score is not None
    assert (score.mase, score.mase_gap, score.wql) == (
        None,
        AccuracyGap.HISTORY_UNCHANGING,
        "0.0994",
    )


def test_an_all_zero_holdout_makes_the_model_invalid() -> None:
    outcome = score_backtest(
        _request(actuals=(0.0, 0.0, 0.0), p10=_d(0, 0, 0), p50=_d(0, 0, 0), p90=_d(1, 1, 1))
    )
    assert (outcome.score, outcome.invalid_reason) == (None, SkipReason.HOLDOUT_ALL_ZERO)


def test_an_unordered_scored_week_makes_the_model_invalid_but_an_unscored_one_does_not() -> None:
    crossed = _request(p10=_d(12.0, 19.0, 18.0))
    assert score_backtest(crossed).invalid_reason is SkipReason.BACKTEST_BAND_UNORDERED
    # Control: the same crossing in a week whose actual is unknown is outside the scored set.
    unscored = _request(actuals=(14.0, None, 25.0), p10=_d(12.0, 19.0, 18.0))
    assert score_backtest(unscored).score is not None


# ---- R3-03: unknown weeks ---------------------------------------------------------------


def test_one_unknown_history_week_gives_the_known_pairs_scale() -> None:
    """Pairs (10,12) and (11,15) only: changes 2 and 4, scale 3; the NaN week joins nothing."""
    scale = mase_scale((10.0, 12.0, None, 11.0, 15.0), 2)
    assert (scale.scale, scale.gap) == (3.0, None)


def test_eleven_known_pairs_give_mase_scale_unknown_at_the_twelve_pair_minimum() -> None:
    history: tuple[float | None, ...] = tuple(float(v % 3) for v in range(12))  # 11 pairs
    assert mase_scale(history, 12).gap is AccuracyGap.MASE_SCALE_UNKNOWN
    # Control: one more known week makes 12 pairs and a scale.
    assert mase_scale((*history, 1.0), 12).gap is None


def test_a_constant_known_history_with_gaps_is_history_unchanging() -> None:
    history: tuple[float | None, ...] = (4.0, 4.0, None, 4.0, 4.0, 4.0)
    assert mase_scale(history, 3).gap is AccuracyGap.HISTORY_UNCHANGING


def test_unknown_holdout_weeks_are_left_out_of_every_score() -> None:
    outcome = score_backtest(_request(actuals=(14.0, None, 25.0)))
    assert outcome.score is not None
    assert outcome.score.weeks_scored == 2
    with pytest.raises(ValueError, match="known actual"):
        BacktestScoreRequest(HISTORY, (None, 1.0, 1.0), P10, P50, P90, (0, 1, 2), 3)
