# SPDX-License-Identifier: MIT-0
"""One shard's run: three models per target, the backtest and the gate, with fake models.

A wrong candidate rule would show a forecast that failed its own checks, or none where one
passed; a wrong recent-week record would mislabel stock-outs on the page.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from forecast_series_support import (
    STARTS,
    FakeEts,
    FakeModel,
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


def _run(  # noqa: PLR0913, PLR0917 -- the series, the model and two overrides
    result: WeeklySeriesResult,
    model: FakeModel,
    run_policy: DemandForecastPolicy | None = None,
    ets: FakeEts | None = None,
) -> ForecastRunResult:
    return run_forecast(
        ForecastRunRequest(
            shard_index=0,
            forecast_set=SET,
            series=result,
            policy=run_policy or policy(),
            forecaster=model,
            ets=ets or FakeEts(),
        )
    )


def _steady(n: int, weeks: range = range(104)) -> list[WeeklyTargetAggregateRowV1]:
    return product_rows(
        asin(n), weeks, lambda w, t: 10.0 + (w % 4) + (1.0 if t == "units_ordered" else 0.0)
    )


def _spiky(n: int, weeks: range = range(104)) -> list[WeeklyTargetAggregateRowV1]:
    """Flat at 10 with spikes of 16 on even weeks of the first year only.

    Last year's spikes mislead SeasonalNaive and FakeEts's band is wider than FakeModel's, so
    Chronos-2 has the lowest WQL.
    """
    return product_rows(asin(n), weeks, lambda w, _t: 16.0 if w < 52 and w % 2 == 0 else 10.0)


def test_a_seasonal_series_shows_seasonal_naive_with_every_model_scored_and_13_recent_weeks() -> (
    None
):
    result = _run(series(_steady(1)), FakeModel())
    (candidate,) = result.candidates
    CandidateSeriesV1.model_validate(candidate.model_dump(mode="json", by_alias=True))
    ordered = candidate.targets["units_ordered"]
    # The series repeats every 4 weeks, so last year's value is exact: WQL 0 beats Chronos-2.
    assert (ordered.chosen_model, ordered.withheld_reason, ordered.gate_reason) == (
        ModelName.SEASONAL_NAIVE,
        None,
        "CHRONOS_NOT_BETTER",
    )
    by_model = {e.model: e for e in ordered.evaluations}
    assert {m: (e.validity_reason, e.origin_status) for m, e in by_model.items()} == dict.fromkeys(
        ModelName, (None, "publishable")
    )
    seasonal = by_model[ModelName.SEASONAL_NAIVE].backtest_score
    assert seasonal is not None
    assert (seasonal.wql, seasonal.weeks_scored, ordered.scored_weeks) == ("0.0000", 13, 13)
    assert ordered.chosen_forecast is not None
    # Shown units ordered are last year's: 11 + (week mod 4) for grid weeks 104 to 116.
    assert ordered.chosen_forecast.p50[:4] == ("11.0000", "12.0000", "13.0000", "14.0000")
    assert len(candidate.recent_weeks) == 13
    # Ordered units exceed shipped units by 1 every week, so each recent week is a stock-out week.
    assert {week.stockout for week in candidate.recent_weeks} == {True}
    assert candidate.stockout_weeks_last_13 == 13
    assert candidate.display_asin == "B0TEST0001"
    assert candidate.currency_code == "USD"
    assert result.counts_by_reason["units_ordered.chosen.seasonal_naive"] == 1


def test_chronos2_shows_where_it_beats_both_simple_models_on_the_backtest() -> None:
    result = _run(series(_spiky(1)), FakeModel())
    ordered = result.candidates[0].targets["units_ordered"]
    assert (ordered.chosen_model, ordered.gate_reason) == (ModelName.CHRONOS2, None)
    wql = {e.model: e.backtest_score.wql for e in ordered.evaluations if e.backtest_score}
    assert wql[ModelName.CHRONOS2] < min(wql[ModelName.SEASONAL_NAIVE], wql[ModelName.ETS])
    assert ordered.chosen_forecast is not None
    # FakeModel's origin forecast is the 104-week context mean: (26 x 16 + 78 x 10) / 104.
    assert ordered.chosen_forecast.p50[0] == "11.5000"
    assert result.counts_by_reason["units_ordered.chosen.chronos2"] == 1


def test_a_series_under_78_weeks_cant_show_chronos2_and_shows_ets() -> None:
    result = _run(series(_spiky(1, range(30, 104))), FakeModel())
    ordered = result.candidates[0].targets["units_ordered"]
    seasonal = next(e for e in ordered.evaluations if e.model is ModelName.SEASONAL_NAIVE)
    assert seasonal.validity_reason is SkipReason.HISTORY_TOO_SHORT_FOR_SEASON
    assert (ordered.chosen_model, ordered.gate_reason) == (
        ModelName.ETS,
        "COMPARATOR_UNAVAILABLE(seasonal_naive)",
    )


def test_a_constant_short_series_with_an_unconverged_ets_is_withheld_accuracy_not_testable() -> (
    None
):
    rows = product_rows(asin(1), range(44, 104), 5.0)
    result = _run(series(rows), FakeModel(), ets=FakeEts(unconverged=frozenset({asin(1)})))
    ordered = result.candidates[0].targets["units_ordered"]
    assert (ordered.chosen_model, ordered.withheld_reason, ordered.chosen_forecast) == (
        None,
        SkipReason.ACCURACY_NOT_TESTABLE,
        None,
    )
    reasons = {e.model: e.validity_reason for e in ordered.evaluations}
    assert reasons == {
        ModelName.CHRONOS2: None,
        ModelName.SEASONAL_NAIVE: SkipReason.HISTORY_TOO_SHORT_FOR_SEASON,
        ModelName.ETS: SkipReason.ETS_FIT_FAILED,
    }
    assert result.counts_by_reason["units_ordered.withheld.ACCURACY_NOT_TESTABLE"] == 1


def test_too_few_known_weeks_before_the_origin_make_ets_insufficient_known() -> None:
    base = policy()
    strict = replace(base, eligibility=replace(base.eligibility, ets_min_known_weeks=92))
    ordered = _run(series(_steady(1)), FakeModel(), strict).candidates[0].targets["units_ordered"]
    ets = next(e for e in ordered.evaluations if e.model is ModelName.ETS)
    # The backtest history holds 91 weeks, one short of 92.
    assert (ets.validity_reason, ets.origin_status) == (
        SkipReason.ETS_INSUFFICIENT_KNOWN,
        "not_attempted",
    )


def test_a_short_launch_series_is_withheld_insufficient_context_and_never_forecast() -> None:
    model = FakeModel()
    result = _run(series([*_steady(1), *_steady(2, range(84, 104))]), model)
    short = result.candidates[1].targets["units_shipped"]
    assert (short.chosen_model, short.withheld_reason) == (None, SkipReason.INSUFFICIENT_CONTEXT)
    assert {(e.validity_reason, e.origin_status, e.backtest_score) for e in short.evaluations} == {
        (SkipReason.INSUFFICIENT_CONTEXT, "not_attempted", None)
    }
    # Every call carried only the one eligible series: 4 targets x (backtest + forecast).
    assert model.calls == [(1, 13)] * 8


def test_a_negative_chronos2_origin_shows_the_better_simple_model_with_the_backtest_kept() -> None:
    model = FakeModel(shift=lambda a, weeks: -100.0 if (a, weeks) == (asin(2), 104) else 0.0)
    result = _run(series([*_spiky(1), *_spiky(2)]), model)
    target = result.candidates[1].targets["units_ordered"]
    assert (target.chosen_model, target.gate_reason) == (
        ModelName.ETS,
        "CHRONOS_NOT_PUBLISHABLE(FORECAST_NEGATIVE)",
    )
    chronos = next(e for e in target.evaluations if e.model is ModelName.CHRONOS2)
    assert (chronos.origin_status, chronos.origin_failure) == (
        "not_publishable",
        "FORECAST_NEGATIVE",
    )
    assert chronos.validity_reason is None
    assert chronos.backtest_score is not None
    assert result.candidates[0].targets["units_ordered"].chosen_model is ModelName.CHRONOS2


def test_a_shard_with_no_series_to_show_still_gives_every_candidate_withheld() -> None:
    model = FakeModel()
    result = _run(series(_steady(1, range(90, 104))), model)
    (candidate,) = result.candidates
    assert {t.withheld_reason for t in candidate.targets.values()} == {
        SkipReason.INSUFFICIENT_CONTEXT
    }
    assert result.counts_by_reason == {f"{t}.withheld.INSUFFICIENT_CONTEXT": 1 for t in TARGETS}
    assert model.calls == []


def test_one_shard_empty_for_a_target_leaves_coverage_to_the_run_level_check() -> None:
    # Two shards of one run: shard 1 holds only a launch product, so no target shows a series
    # there; both shards still give candidates, and shard 0 shows its forecasts (design §6c).
    shards = (series(_steady(1)), series(_steady(2, range(90, 104))))
    results = [
        run_forecast(
            ForecastRunRequest(
                shard_index=index,
                forecast_set=SET,
                series=shard,
                policy=policy(),
                forecaster=FakeModel(),
                ets=FakeEts(),
            )
        )
        for index, shard in enumerate(shards)
    ]
    assert results[0].counts_by_reason["units_ordered.chosen.seasonal_naive"] == 1
    assert results[1].counts_by_reason["units_ordered.withheld.INSUFFICIENT_CONTEXT"] == 1
    assert [c.shard_index for r in results for c in r.candidates] == [0, 1]


def test_a_product_with_no_row_in_the_last_activity_window_is_asin_inactive() -> None:
    result = _run(series([*_steady(1), *_steady(2, range(80))]), FakeModel())
    gone = result.candidates[1]
    assert {t.withheld_reason for t in gone.targets.values()} == {SkipReason.ASIN_INACTIVE}
    assert result.counts_by_reason["units_ordered.withheld.ASIN_INACTIVE"] == 1


def test_a_declared_null_run_that_leaves_too_little_context_is_target_value_missing() -> None:
    rows = [
        r for r in _steady(1) if not (r.target == "revenue_shipped" and r.week_start >= STARTS[50])
    ]
    rows += [target_row(asin(1), w, "revenue_shipped", value=None, nulls=7) for w in range(50, 104)]
    (candidate,) = _run(series(rows), FakeModel()).candidates
    assert candidate.targets["revenue_shipped"].withheld_reason is SkipReason.TARGET_VALUE_MISSING
    assert candidate.targets["units_ordered"].chosen_model is ModelName.SEASONAL_NAIVE


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
