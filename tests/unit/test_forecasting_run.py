# SPDX-License-Identifier: MIT-0
"""One shard's slice 1 run (Chronos-2, no gate) with a fake model.

A wrong candidate rule would show a forecast that failed its own checks, or none where one
passed; a wrong recent-week record would mislabel stock-outs on the page.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace

import pytest
from forecast_series_support import (
    STARTS,
    asin,
    policy,
    product_rows,
    series,
    target_row,
)

from domain.forecasting.boundary import (
    CandidateSeriesV1,
    ForecastSetV1,
    WeeklyTargetAggregateRowV1,
)
from domain.forecasting.models import (
    ForecastBatchRequest,
    ForecastBatchResult,
    ModelName,
    QuantileForecast,
    SkipReason,
)
from domain.forecasting.policy import TARGETS, DemandForecastPolicy
from domain.forecasting.run import (
    ForecastRunError,
    ForecastRunRequest,
    ForecastRunResult,
    run_forecast,
)
from domain.forecasting.weekly_series import WeeklySeriesResult

pytestmark = pytest.mark.unit

SET = ForecastSetV1(
    marketplace_key="amazon_us", distributor_view="MANUFACTURING", selling_program="RETAIL"
)


@dataclass
class FakeModel:
    """Forecast each series flat at its mean known value, p10 and p90 two units either side.

    `shift(asin, context_weeks)` moves one series' level, so a test can move only the forecast
    origin (104 context weeks) and leave the backtest (91) alone.
    """

    shift: Callable[[str, int], float] = lambda _asin, _weeks: 0.0
    calls: list[tuple[int, int]] = field(default_factory=list[tuple[int, int]])

    def __call__(self, request: ForecastBatchRequest, /) -> ForecastBatchResult:
        self.calls.append((len(request.series), request.horizon_weeks))
        forecasts: list[QuantileForecast] = []
        for item in request.series:
            known = [v for v in item.values if v is not None]
            level = sum(known) / len(known) + self.shift(item.canonical_asin, len(item.values))
            h = request.horizon_weeks
            forecasts.append(
                QuantileForecast(
                    item.canonical_asin,
                    item.target,
                    (level - 2,) * h,
                    (level,) * h,
                    (level + 2,) * h,
                )
            )
        return ForecastBatchResult(tuple(forecasts))


def _run(
    result: WeeklySeriesResult, model: FakeModel, run_policy: DemandForecastPolicy | None = None
) -> ForecastRunResult:
    return run_forecast(
        ForecastRunRequest(
            shard_index=0,
            forecast_set=SET,
            series=result,
            policy=run_policy or policy(),
            forecaster=model,
        )
    )


def _steady(n: int, weeks: range = range(104)) -> list[WeeklyTargetAggregateRowV1]:
    return product_rows(
        asin(n), weeks, lambda w, t: 10.0 + (w % 4) + (1.0 if t == "units_ordered" else 0.0)
    )


def test_a_valid_publishable_series_shows_chronos2_with_three_evaluations_and_13_recent_weeks() -> (
    None
):
    result = _run(series(_steady(1)), FakeModel())
    (candidate,) = result.candidates
    CandidateSeriesV1.model_validate(candidate.model_dump(mode="json", by_alias=True))
    ordered = candidate.targets["units_ordered"]
    assert (ordered.chosen_model, ordered.withheld_reason, ordered.gate_reason) == (
        ModelName.CHRONOS2,
        None,
        None,
    )
    by_model = {e.model: e for e in ordered.evaluations}
    assert by_model[ModelName.CHRONOS2].origin_status == "publishable"
    assert by_model[ModelName.CHRONOS2].backtest_score is not None
    for model in (ModelName.SEASONAL_NAIVE, ModelName.ETS):
        assert (by_model[model].validity_reason, by_model[model].origin_status) == (
            SkipReason.MODEL_NOT_RUN,
            "not_attempted",
        )
    assert ordered.chosen_forecast is not None
    assert len(ordered.chosen_forecast.p50) == 13
    assert len(candidate.recent_weeks) == 13
    # Ordered units exceed shipped units by 1 every week, so each recent week is a stock-out week.
    assert {week.stockout for week in candidate.recent_weeks} == {True}
    assert candidate.stockout_weeks_last_13 == 13
    assert candidate.display_asin == "B0TEST0001"
    assert candidate.currency_code == "USD"
    assert result.counts_by_reason["units_ordered.chosen.chronos2"] == 1


def test_a_short_launch_series_is_withheld_insufficient_context_and_never_forecast() -> None:
    model = FakeModel()
    result = _run(series([*_steady(1), *_steady(2, range(84, 104))]), model)
    short = result.candidates[1].targets["units_shipped"]
    assert (short.chosen_model, short.withheld_reason) == (None, SkipReason.INSUFFICIENT_CONTEXT)
    chronos = next(e for e in short.evaluations if e.model is ModelName.CHRONOS2)
    assert (chronos.validity_reason, chronos.origin_status, chronos.backtest_score) == (
        SkipReason.INSUFFICIENT_CONTEXT,
        "not_attempted",
        None,
    )
    # Every call carried only the one eligible series: 4 targets x (backtest + forecast).
    assert model.calls == [(1, 13)] * 8


def test_a_negative_origin_forecast_is_withheld_not_publishable_with_its_backtest_kept() -> None:
    model = FakeModel(shift=lambda a, weeks: -100.0 if (a, weeks) == (asin(2), 104) else 0.0)
    result = _run(series([*_steady(1), *_steady(2)]), model)
    target = result.candidates[1].targets["units_ordered"]
    assert (target.chosen_model, target.withheld_reason) == (None, SkipReason.FORECAST_NEGATIVE)
    chronos = next(e for e in target.evaluations if e.model is ModelName.CHRONOS2)
    assert (chronos.origin_status, chronos.origin_failure) == (
        "not_publishable",
        "FORECAST_NEGATIVE",
    )
    assert chronos.validity_reason is None
    assert chronos.backtest_score is not None


def test_a_target_with_no_series_to_show_fails_the_shard_forecast_target_empty() -> None:
    with pytest.raises(ForecastRunError) as refused:
        _run(series(_steady(1, range(90, 104))), FakeModel())
    assert refused.value.code == "FORECAST_TARGET_EMPTY"


def test_batches_follow_the_policy_batch_size() -> None:
    small = policy()
    two = replace(small, engine=replace(small.engine, batch_size=2))
    model = FakeModel()
    _run(series([*_steady(1), *_steady(2), *_steady(3)]), model, two)
    assert model.calls == [(2, 13), (1, 13), (2, 13), (1, 13)] * len(TARGETS)


def test_a_model_answering_out_of_order_fails_the_shard() -> None:
    class Swapped(FakeModel):
        def __call__(self, request: ForecastBatchRequest, /) -> ForecastBatchResult:
            return ForecastBatchResult(tuple(reversed(super().__call__(request).forecasts)))

    with pytest.raises(ForecastRunError) as refused:
        _run(series([*_steady(1), *_steady(2)]), Swapped())
    assert refused.value.code == "FORECAST_SHAPE_INVALID"


def test_an_unknown_recent_unit_week_leaves_stockout_null() -> None:
    rows = [r for r in _steady(1) if (r.week_start, r.target) != (STARTS[103], "units_shipped")]
    rows.append(target_row(asin(1), 103, "units_shipped", value=None, nulls=7))
    (candidate,) = _run(series(rows), FakeModel()).candidates
    last = candidate.recent_weeks[-1]
    assert last.stockout is None
    assert last.targets["units_shipped"].value is None
    assert candidate.stockout_weeks_last_13 == 12
