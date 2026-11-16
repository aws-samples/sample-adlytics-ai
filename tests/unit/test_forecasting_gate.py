# SPDX-License-Identifier: MIT-0
"""The per-series gate (design §5c; owner decision 4) and how unknown weeks reach it (§3d).

A wrong branch would show Chronos-2 where it did not beat both simple models, show a model whose
forecast failed its own checks, or withhold a series that has a tested simple model; a wrong
scored week set would compare the models on different weeks.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from forecast_series_support import (
    FULL,
    INPUT_END,
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
    BacktestScoreV1,
    ForecastSetV1,
    IntervalV1,
    ModelEvaluationV1,
    TargetCandidateV1,
    WeeklyTargetAggregateRowV1,
)
from domain.forecasting.gate import GateDecision, GateRequest, choose_model
from domain.forecasting.models import ModelName, SkipReason
from domain.forecasting.run import ForecastRunRequest, run_forecast

pytestmark = pytest.mark.unit

C, SN, ETS = ModelName.CHRONOS2, ModelName.SEASONAL_NAIVE, ModelName.ETS
SET = ForecastSetV1(
    marketplace_key="amazon_us", distributor_view="MANUFACTURING", selling_program="RETAIL"
)


def _valid(model: ModelName, wql: str, failure: SkipReason | None = None) -> ModelEvaluationV1:
    return ModelEvaluationV1(
        model=model,
        backtest_score=BacktestScoreV1(
            wql=wql, mase="1.0000", mase_gap=None, weeks_in_range=10, weeks_scored=13
        ),
        validity_reason=None,
        origin_status="publishable" if failure is None else "not_publishable",
        origin_failure=None if failure is None else failure.value,
    )


def _invalid(model: ModelName, reason: SkipReason) -> ModelEvaluationV1:
    return ModelEvaluationV1(
        model=model,
        backtest_score=None,
        validity_reason=reason,
        origin_status="not_attempted",
        origin_failure=None,
    )


def _gate(*evaluations: ModelEvaluationV1, margin: float = 0.0) -> GateDecision:
    return choose_model(GateRequest(evaluations, margin))


NEG = SkipReason.FORECAST_NEGATIVE
LAG_AT_ORIGIN = SkipReason.SEASONAL_LAG_UNKNOWN_AT_ORIGIN


@pytest.mark.parametrize(
    ("evaluations", "expected"),
    [
        pytest.param(
            (_valid(C, "0.1000"), _valid(SN, "0.2000"), _valid(ETS, "0.3000")),
            GateDecision(C, None, None),
            id="chronos2-beats-both-and-is-publishable",
        ),
        pytest.param(
            (_valid(C, "0.2000"), _valid(SN, "0.2000"), _valid(ETS, "0.3000")),
            GateDecision(SN, None, "CHRONOS_NOT_BETTER"),
            id="a-tie-with-the-best-simple-model-is-not-better",
        ),
        pytest.param(
            (_valid(C, "0.1000"), _valid(SN, "0.3000"), _valid(ETS, "0.2000")),
            GateDecision(C, None, None),
            id="beats-the-lower-of-the-two",
        ),
        pytest.param(
            (_valid(C, "0.2500"), _valid(SN, "0.3000"), _valid(ETS, "0.2000")),
            GateDecision(ETS, None, "CHRONOS_NOT_BETTER"),
            id="beats-one-comparator-only-shows-the-lower-simple-model",
        ),
        pytest.param(
            (_valid(C, "0.5000"), _valid(SN, "0.3000"), _valid(ETS, "0.3000")),
            GateDecision(SN, None, "CHRONOS_NOT_BETTER"),
            id="equal-simple-models-show-seasonal-naive",
        ),
        pytest.param(
            (
                _valid(C, "0.1000"),
                _invalid(SN, SkipReason.HISTORY_TOO_SHORT_FOR_SEASON),
                _valid(ETS, "0.3000"),
            ),
            GateDecision(ETS, None, "COMPARATOR_UNAVAILABLE(seasonal_naive)"),
            id="chronos2-best-but-seasonal-naive-invalid-shows-ets",
        ),
        pytest.param(
            (_valid(C, "0.1000"), _valid(SN, "0.3000"), _invalid(ETS, SkipReason.ETS_FIT_FAILED)),
            GateDecision(SN, None, "COMPARATOR_UNAVAILABLE(ets)"),
            id="chronos2-best-but-ets-invalid-shows-seasonal-naive",
        ),
        pytest.param(
            (
                _valid(C, "0.1000"),
                _invalid(SN, SkipReason.SEASONAL_RESIDUALS_TOO_FEW),
                _invalid(ETS, SkipReason.ETS_FIT_FAILED),
            ),
            GateDecision(None, SkipReason.ACCURACY_NOT_TESTABLE, None),
            id="no-valid-simple-model-withholds",
        ),
        pytest.param(
            (_valid(C, "0.1000", NEG), _valid(SN, "0.3000"), _valid(ETS, "0.2000")),
            GateDecision(ETS, None, "CHRONOS_NOT_PUBLISHABLE(FORECAST_NEGATIVE)"),
            id="chronos2-best-but-negative-at-the-origin-shows-the-better-simple-model",
        ),
        pytest.param(
            (_valid(C, "0.5000"), _valid(SN, "0.2000", LAG_AT_ORIGIN), _valid(ETS, "0.3000")),
            GateDecision(ETS, None, "CHRONOS_NOT_BETTER"),
            id="seasonal-naive-better-but-not-publishable-shows-ets",
        ),
        pytest.param(
            (_valid(C, "0.1000"), _valid(SN, "0.2000", LAG_AT_ORIGIN), _valid(ETS, "0.3000")),
            GateDecision(C, None, None),
            id="a-comparator-needs-only-its-backtest-to-let-chronos2-show",
        ),
        pytest.param(
            (
                _valid(C, "0.5000"),
                _valid(SN, "0.2000", LAG_AT_ORIGIN),
                _valid(ETS, "0.3000", NEG),
            ),
            GateDecision(
                None,
                SkipReason.FORECAST_NOT_PUBLISHABLE,
                "FORECAST_NOT_PUBLISHABLE(SEASONAL_LAG_UNKNOWN_AT_ORIGIN)",
            ),
            id="valid-but-none-publishable-names-the-lower-wql-models-check",
        ),
        pytest.param(
            (
                _invalid(C, SkipReason.BACKTEST_BAND_UNORDERED),
                _valid(SN, "0.4000"),
                _valid(ETS, "0.3000"),
            ),
            GateDecision(ETS, None, "CHRONOS_NOT_VALID(BACKTEST_BAND_UNORDERED)"),
            id="an-invalid-chronos2-shows-the-lower-simple-model",
        ),
        pytest.param(
            tuple(_invalid(m, SkipReason.INSUFFICIENT_CONTEXT) for m in (C, SN, ETS)),
            GateDecision(None, SkipReason.INSUFFICIENT_CONTEXT, None),
            id="a-series-level-reason-withholds-with-itself",
        ),
    ],
)
def test_the_gate_branches_of_design_5c(
    evaluations: tuple[ModelEvaluationV1, ...], expected: GateDecision
) -> None:
    assert _gate(*evaluations) == expected


@pytest.mark.parametrize(
    ("chronos", "shown"),
    [("0.9000", SN), ("0.8999", C)],
)
def test_the_margin_needs_wql_strictly_below_one_minus_margin_times_the_best(
    chronos: str, shown: ModelName
) -> None:
    decision = _gate(_valid(C, chronos), _valid(SN, "1.0000"), _valid(ETS, "1.1000"), margin=0.1)
    assert decision.chosen_model is shown


@pytest.mark.parametrize(
    ("evaluations", "message"),
    [
        ((_valid(C, "0.1000"), _valid(SN, "0.2000")), "exactly one evaluation per model"),
        (
            (
                _invalid(C, SkipReason.INSUFFICIENT_CONTEXT),
                _valid(SN, "0.2000"),
                _valid(ETS, "0.3000"),
            ),
            "carried by every model",
        ),
        (
            (
                _valid(C, "0.1000").model_copy(update={"origin_status": "not_attempted"}),
                _valid(SN, "0.2000"),
                _valid(ETS, "0.3000"),
            ),
            "a valid model is forecast at the origin",
        ),
    ],
)
def test_the_gate_refuses_evaluations_it_cannot_judge(
    evaluations: tuple[ModelEvaluationV1, ...], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        _gate(*evaluations)


# Gap positions (design §3d). Week offsets count back from input_end: offset 0 is grid week 103.
# The steady series repeats every 4 weeks, so SeasonalNaive's lag-52 forecast is exact (WQL 0)
# and is shown whenever it is valid and publishable; ETS is shown when it is not.


def _week(offset: int) -> int:
    return len(STARTS) - 1 - offset


def _steady(weeks: range = range(104)) -> list[WeeklyTargetAggregateRowV1]:
    return product_rows(asin(1), weeks, lambda w, _t: 10.0 + (w % 4))


def _ordered_unknown_at(*offsets: int) -> list[WeeklyTargetAggregateRowV1]:
    """Units ordered declared null in the given weeks (a product gap of each)."""
    weeks = {_week(offset) for offset in offsets}
    rows = [
        r
        for r in _steady()
        if not (r.target == "units_ordered" and r.week_start in {STARTS[w] for w in weeks})
    ]
    return rows + [target_row(asin(1), w, "units_ordered", value=None, nulls=7) for w in weeks]


def _ordered(
    rows: list[WeeklyTargetAggregateRowV1], sales_coverage: tuple[IntervalV1, ...] = FULL
) -> TargetCandidateV1:
    (candidate,) = run_forecast(
        ForecastRunRequest(
            shard_index=0,
            forecast_set=SET,
            series=series(rows, sales_coverage=sales_coverage),
            policy=policy(),
            forecaster=FakeModel(),
            ets=FakeEts(),
        )
    ).candidates
    return candidate.targets["units_ordered"]


def _states(target: TargetCandidateV1) -> dict[ModelName, tuple[SkipReason | None, str]]:
    return {e.model: (e.validity_reason, e.origin_status) for e in target.evaluations}


ALL_SHOWABLE = {C: (None, "publishable"), SN: (None, "publishable"), ETS: (None, "publishable")}


@pytest.mark.parametrize(
    ("offset", "scored", "states", "shown"),
    [
        pytest.param(0, 12, ALL_SHOWABLE, SN, id="holdout-newest"),
        pytest.param(12, 12, ALL_SHOWABLE, SN, id="holdout-oldest"),
        pytest.param(13, 13, ALL_SHOWABLE, SN, id="last-week-before-the-holdout"),
        pytest.param(
            39,
            13,
            {**ALL_SHOWABLE, SN: (None, "not_publishable")},
            ETS,
            id="lag-of-the-last-forecast-week",
        ),
        pytest.param(
            51,
            13,
            {**ALL_SHOWABLE, SN: (None, "not_publishable")},
            ETS,
            id="lag-of-the-first-forecast-week",
        ),
        pytest.param(52, 12, ALL_SHOWABLE, SN, id="lag-of-the-newest-holdout-week"),
        pytest.param(64, 12, ALL_SHOWABLE, SN, id="lag-of-the-oldest-holdout-week"),
    ],
)
def test_one_unknown_week_never_blocks_chronos2_and_moves_only_its_own_rule(  # noqa: PLR0913, PLR0917 -- one parametrized row
    offset: int,
    scored: int,
    states: dict[ModelName, tuple[SkipReason | None, str]],
    shown: ModelName,
) -> None:
    target = _ordered(_ordered_unknown_at(offset))
    assert (target.scored_weeks, _states(target), target.chosen_model) == (scored, states, shown)
    assert {e.backtest_score.weeks_scored for e in target.evaluations if e.backtest_score} == {
        scored
    }


def test_an_unknown_lag_at_the_origin_records_seasonal_lag_unknown_at_origin() -> None:
    target = _ordered(_ordered_unknown_at(45))
    seasonal = next(e for e in target.evaluations if e.model is SN)
    assert seasonal.origin_failure == "SEASONAL_LAG_UNKNOWN_AT_ORIGIN"
    assert target.gate_reason == "CHRONOS_NOT_BETTER"


def test_four_unknown_holdout_weeks_make_the_series_untestable() -> None:
    target = _ordered(_ordered_unknown_at(0, 3, 6, 9))
    assert (target.chosen_model, target.withheld_reason) == (None, SkipReason.HOLDOUT_UNKNOWN)
    assert {e.validity_reason for e in target.evaluations} == {SkipReason.HOLDOUT_UNKNOWN}


def _set_gap(*offsets: int) -> tuple[IntervalV1, ...]:
    """Sales coverage missing every day of the given weeks (a set gap)."""
    weeks = sorted(_week(offset) for offset in offsets)
    first, last = STARTS[weeks[0]], STARTS[weeks[-1]] + timedelta(days=6)
    return (
        IntervalV1(start=STARTS[0], end=first - timedelta(days=1)),
        IntervalV1(start=last + timedelta(days=1), end=INPUT_END),
    )


@pytest.mark.parametrize(
    ("offsets", "scored"),
    [
        pytest.param((13, 14, 15), 13, id="offsets-13-to-15"),
        pytest.param((60, 61, 62), 10, id="offsets-60-to-62"),
    ],
)
def test_a_three_week_set_gap_keeps_the_context_and_all_three_models_valid(
    offsets: tuple[int, ...], scored: int
) -> None:
    uncovered = {STARTS[_week(offset)] for offset in offsets}
    rows = [row for row in _steady() if row.week_start not in uncovered]
    target = _ordered(rows, _set_gap(*offsets))
    assert (target.scored_weeks, _states(target), target.chosen_model) == (scored, ALL_SHOWABLE, SN)


def test_a_three_week_product_gap_restarts_the_context_so_seasonal_naive_is_too_short() -> None:
    target = _ordered(_ordered_unknown_at(60, 61, 62))
    assert _states(target)[SN] == (SkipReason.HISTORY_TOO_SHORT_FOR_SEASON, "not_attempted")
    assert (target.chosen_model, target.gate_reason) == (
        ETS,
        "COMPARATOR_UNAVAILABLE(seasonal_naive)",
    )


def test_comparator_bands_on_an_intermittent_series_are_censored_at_zero_and_chronos2_is_not() -> (
    None
):
    rows = product_rows(asin(1), range(104), lambda w, _t: 6.0 if (w * 7) % 5 == 0 else 0.0)
    target = _ordered(rows)
    assert target.chosen_model is ETS
    assert target.chosen_forecast is not None
    # FakeEts's p10 is the mean less 5, below zero before censoring.
    assert set(target.chosen_forecast.p10) == {"0.0000"}
    chronos = next(e for e in target.evaluations if e.model is C)
    assert (chronos.origin_status, chronos.origin_failure) == (
        "not_publishable",
        "FORECAST_NEGATIVE",
    )
