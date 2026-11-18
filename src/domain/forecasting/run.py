# SPDX-License-Identifier: MIT-0
"""One shard's forecast run: three models per target, the backtest, then the gate (§4a, §5).

Adapted from the source engine's forecast run; its contract-keyed suppliers and lake staging are
replaced by the boundary records (design §4e).

Per target, in order:

1. Eligibility from each series' context (design §3d): an ASIN with no observed day in the
   activity window ending `input_end` is `ASIN_INACTIVE`; a money series whose currency codes mix
   is `CURRENCY_MIXED`; fewer than `min_context_weeks + holdout_weeks` known weeks, or fewer than
   `min_context_weeks` known weeks before the holdout, is `TARGET_VALUE_MISSING` when the
   target's history holds a declared-null value and `INSUFFICIENT_CONTEXT` otherwise; fewer than
   `min_scored_holdout_weeks` known holdout actuals is `HOLDOUT_UNKNOWN`. Every model carries the
   reason and the series is withheld with it.
2. Backtest (design §5b): Chronos-2 forecasts the holdout from the weeks before it, in the
   declared batches (design §4c); SeasonalNaive and ETS forecast it one series at a time (§5a),
   their bands censored at zero. ETS needs `ets_min_known_weeks` known weeks before the origin.
3. One scored week set per series (design §3d): the known holdout actuals and, while
   SeasonalNaive has a holdout band, its known lags. If those lags leave fewer than
   `min_scored_holdout_weeks` weeks, SeasonalNaive is `SEASONAL_LAG_UNKNOWN` and the others are
   scored on the known actuals. A bad shape, a non-finite scored value, an unordered scored week
   or an all-zero holdout makes a model not valid.
4. Forecast origin: every valid model forecasts the 13 weeks after `input_end` from the full
   context, stored under the numeric policy; shape, finiteness, an unordered band, then a negative
   value make it not publishable (design §4d). SeasonalNaive with an unknown lag in those weeks
   is `SEASONAL_LAG_UNKNOWN_AT_ORIGIN`; an ETS fit that fails there is `ETS_FIT_FAILED`.
5. The gate (`gate.choose_model`, design §5c) picks the shown model from the three evaluations.

A target whose every series in this shard is withheld still gives a candidate: one shard can't
decide coverage, so the commit step checks that every target has a shown series across all shards
(design §6c, `held_quality(target_empty)`). The models are reached through `BatchForecaster` and
`EtsForecaster`, which the job binds to its model adapters; domain code never imports a port or a
model library.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from domain.forecasting.batching import Origin, SeriesKeyRequest, plan_batches, series_sort_key
from domain.forecasting.boundary import (
    BacktestScoreV1,
    BandsV1,
    CandidateSeriesV1,
    ForecastSetV1,
    ModelEvaluationV1,
    RecentTargetWeekV1,
    RecentWeekV1,
    TargetCandidateV1,
    WeekStateCountsV1,
)
from domain.forecasting.checks import (
    check_forecast_shape,
    first_negative_week,
    first_unordered_week,
)
from domain.forecasting.comparators import (
    EtsRequest,
    EtsResult,
    SeasonalNaiveRequest,
    censor_at_zero,
    seasonal_naive,
)
from domain.forecasting.gate import GateRequest, choose_model
from domain.forecasting.identity import display_map
from domain.forecasting.models import (
    ForecastBatchRequest,
    ForecastBatchResult,
    ModelName,
    QuantileForecast,
    SeriesHistory,
    SkipReason,
    WeekState,
)
from domain.forecasting.numeric import stored_text, stored_value
from domain.forecasting.policy import MONEY_TARGETS, TARGETS, DemandForecastPolicy
from domain.forecasting.scoring import (
    BacktestOutcome,
    BacktestScoreRequest,
    score_backtest,
    scored_week_set,
)
from domain.forecasting.weekly_series import (
    SeriesWeeks,
    TargetWeeks,
    WeekCell,
    WeeklySeriesResult,
)

RECENT_WEEKS = 13
"""Weeks every candidate carries in `recent_weeks` (design §4e)."""
_KNOWN = frozenset({WeekState.OBSERVED, WeekState.INFERRED_ZERO})


class BatchForecaster(Protocol):
    """One model call over one planned batch (the job binds this to its model adapter)."""

    def __call__(self, request: ForecastBatchRequest, /) -> ForecastBatchResult:
        """Return one forecast per request series, in request order."""
        ...


class EtsForecaster(Protocol):
    """One ETS fit and forecast of one series (the job binds this to its ETS adapter)."""

    def __call__(self, request: EtsRequest, /) -> EtsResult:
        """Return the uncensored forecast, or `ETS_FIT_FAILED` for an unconverged fit."""
        ...


class ForecastRunError(Exception):
    """The shard can't produce a candidate; `code` is the job's failure category."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class ForecastRunRequest:
    """One shard's classified series, the policy, the set and the models to call."""

    shard_index: int
    forecast_set: ForecastSetV1
    series: WeeklySeriesResult
    policy: DemandForecastPolicy
    forecaster: BatchForecaster
    ets: EtsForecaster

    def __post_init__(self) -> None:
        if not self.series.series:
            raise ValueError("a shard run needs at least one series")
        if len(self.series.week_starts) < RECENT_WEEKS:
            raise ValueError(f"the week grid must hold at least {RECENT_WEEKS} weeks")


@dataclass(frozen=True)
class ForecastRunResult:
    """Every ASIN's candidate, in canonical order, and the counts the run report carries."""

    candidates: tuple[CandidateSeriesV1, ...]
    counts_by_reason: dict[str, int]
    week_state_counts: WeekStateCountsV1
    inferred_zero_days: int
    model_calls: int


@dataclass(frozen=True)
class _Answers:
    """One pass's forecasts by canonical ASIN and the number of model calls it made."""

    forecasts: dict[str, QuantileForecast]
    calls: int


@dataclass(frozen=True)
class _TargetRun:
    """One target's candidate per ASIN and the Chronos-2 calls it made."""

    candidates: dict[str, TargetCandidateV1]
    calls: int


@dataclass(frozen=True)
class _Bands:
    p10: tuple[Decimal, ...]
    p50: tuple[Decimal, ...]
    p90: tuple[Decimal, ...]


@dataclass(frozen=True)
class _Eligible:
    history: SeriesHistory
    context: SeriesHistory
    actuals: tuple[float | None, ...]
    known: tuple[int, ...]


@dataclass(frozen=True)
class _Holdout:
    """A model's holdout band, or why it has none at the backtest origin."""

    forecast: QuantileForecast | None
    reason: SkipReason | None
    lag_known: tuple[bool, ...] | None = None


@dataclass(frozen=True)
class _ScoredSet:
    """One eligible series and the holdout positions every model is scored on."""

    item: _Eligible
    weeks: tuple[int, ...]


@dataclass(frozen=True)
class _Judged:
    """One series and target's three evaluations, the valid models' origins, the scored weeks."""

    evaluations: tuple[ModelEvaluationV1, ...]
    origins: dict[ModelName, _Origin]
    scored_weeks: int


@dataclass(frozen=True)
class _Scored:
    """One series' backtest per model on the common scored week set."""

    outcomes: dict[ModelName, BacktestScoreV1 | SkipReason]
    scored_weeks: int


@dataclass(frozen=True)
class _Origin:
    """A valid model's origin result: its stored bands, or the check it failed."""

    bands: _Bands | None
    failure: SkipReason | None


def _values(cells: Sequence[WeekCell]) -> tuple[float | None, ...]:
    return tuple(None if cell.value is None else float(cell.value) for cell in cells)


def _forecast(  # noqa: PLR0913 -- one batched model pass over the run's own state
    request: ForecastRunRequest,
    *,
    target: str,
    origin: Origin,
    histories: dict[str, SeriesHistory],
    horizon: int,
) -> _Answers:
    """Forecast every history in the declared batches; refuse an answer out of order."""
    asins = sorted(histories)
    keys = [
        series_sort_key(
            SeriesKeyRequest(
                target,
                origin,
                request.forecast_set.marketplace_key,
                request.forecast_set.distributor_view,
                request.forecast_set.selling_program,
                asin,
            )
        )
        for asin in asins
    ]
    engine = request.policy.engine
    out: dict[str, QuantileForecast] = {}
    batches = plan_batches(keys, engine.batch_size).batches
    for batch in batches:
        members = tuple(histories[asins[index]] for index in batch)
        result = request.forecaster(ForecastBatchRequest(members, horizon, engine.cross_learning))
        answered = [(f.canonical_asin, f.target) for f in result.forecasts]
        if answered != [(m.canonical_asin, m.target) for m in members]:
            raise ForecastRunError(
                "FORECAST_SHAPE_INVALID", f"{target}: the model didn't answer every series in order"
            )
        out.update((forecast.canonical_asin, forecast) for forecast in result.forecasts)
    return _Answers(out, len(batches))


def _stored(forecast: QuantileForecast) -> _Bands:
    return _Bands(
        tuple(stored_value(v) for v in forecast.p10),
        tuple(stored_value(v) for v in forecast.p50),
        tuple(stored_value(v) for v in forecast.p90),
    )


def _evaluation(
    model: ModelName,
    outcome: BacktestScoreV1 | SkipReason,
    origin: _Origin | None = None,
) -> ModelEvaluationV1:
    if isinstance(outcome, SkipReason):
        return ModelEvaluationV1(
            model=model,
            backtest_score=None,
            validity_reason=outcome,
            origin_status="not_attempted",
            origin_failure=None,
        )
    if origin is None:
        raise ValueError(f"{model}: a valid model is forecast at the origin")
    failure = origin.failure
    return ModelEvaluationV1(
        model=model,
        backtest_score=outcome,
        validity_reason=None,
        origin_status="publishable" if failure is None else "not_publishable",
        origin_failure=None if failure is None else failure.value,
    )


def _eligibility(
    request: ForecastRunRequest, item: SeriesWeeks, weeks: TargetWeeks
) -> _Eligible | SkipReason:
    eligibility = request.policy.eligibility
    holdout = eligibility.holdout_weeks
    if not item.active_at_end:
        return SkipReason.ASIN_INACTIVE
    if weeks.target in MONEY_TARGETS and weeks.currency_mixed:
        return SkipReason.CURRENCY_MIXED
    cells = weeks.weeks[weeks.context_start :]
    known = sum(1 for cell in cells if cell.state in _KNOWN)
    before = (
        sum(1 for cell in cells[:-holdout] if cell.state in _KNOWN) if len(cells) > holdout else 0
    )
    if known < eligibility.min_context_weeks + holdout or before < eligibility.min_context_weeks:
        if weeks.value_missing_weeks:
            return SkipReason.TARGET_VALUE_MISSING
        return SkipReason.INSUFFICIENT_CONTEXT
    values = _values(cells)
    covariate = item.covariate[weeks.context_start :]
    actuals = values[-holdout:]
    scored = scored_week_set(actuals)
    if len(scored) < eligibility.min_scored_holdout_weeks:
        return SkipReason.HOLDOUT_UNKNOWN
    asin = item.canonical_asin
    return _Eligible(
        history=SeriesHistory(asin, weeks.target, values[:-holdout], covariate[:-holdout]),
        context=SeriesHistory(asin, weeks.target, values, covariate),
        actuals=actuals,
        known=scored,
    )


def _seasonal(request: ForecastRunRequest, series: SeriesHistory, horizon: int) -> _Holdout:
    result = seasonal_naive(
        SeasonalNaiveRequest(series, horizon, request.policy.eligibility.min_seasonal_residuals)
    )
    if result.forecast is None:
        return _Holdout(None, result.unavailable_reason)
    return _Holdout(censor_at_zero(result.forecast), None, result.lag_known)


def _ets(request: ForecastRunRequest, series: SeriesHistory, horizon: int) -> _Holdout:
    known = sum(1 for value in series.values if value is not None)
    if known < request.policy.eligibility.ets_min_known_weeks:
        return _Holdout(None, SkipReason.ETS_INSUFFICIENT_KNOWN)
    result = request.ets(EtsRequest(series, horizon))
    if result.forecast is None:
        return _Holdout(None, result.failure)
    return _Holdout(censor_at_zero(result.forecast), None)


def _score(
    request: ForecastRunRequest, on: _ScoredSet, holdout: _Holdout
) -> BacktestScoreV1 | SkipReason:
    """Score one model's holdout band on the scored weeks; only those weeks must be finite."""
    if holdout.forecast is None:
        return holdout.reason or SkipReason.FORECAST_SHAPE_INVALID
    item, scored = on.item, on.weeks
    forecast = holdout.forecast
    weeks = request.policy.eligibility.holdout_weeks
    if any(len(band) != weeks for band in (forecast.p10, forecast.p50, forecast.p90)):
        return SkipReason.FORECAST_SHAPE_INVALID
    picked = QuantileForecast(
        forecast.canonical_asin,
        forecast.target,
        tuple(forecast.p10[week] for week in scored),
        tuple(forecast.p50[week] for week in scored),
        tuple(forecast.p90[week] for week in scored),
    )
    shape = check_forecast_shape(picked, len(scored))
    if shape.reason is not None:
        return shape.reason
    bands = _stored(picked)
    outcome: BacktestOutcome = score_backtest(
        BacktestScoreRequest(
            history=item.history.values,
            actuals=tuple(item.actuals[week] for week in scored),
            p10=bands.p10,
            p50=bands.p50,
            p90=bands.p90,
            scored_weeks=tuple(range(len(scored))),
            min_scale_pairs=request.policy.eligibility.min_context_weeks - 1,
        )
    )
    if outcome.score is None:
        return outcome.invalid_reason or SkipReason.BACKTEST_BAND_UNORDERED
    return outcome.score


def _backtest(request: ForecastRunRequest, item: _Eligible, chronos: QuantileForecast) -> _Scored:
    """Design §3d and §5b: the common scored week set, then each model's score on it."""
    horizon = request.policy.eligibility.holdout_weeks
    holdouts = {
        ModelName.CHRONOS2: _Holdout(chronos, None),
        ModelName.SEASONAL_NAIVE: _seasonal(request, item.history, horizon),
        ModelName.ETS: _ets(request, item.history, horizon),
    }
    full = check_forecast_shape(chronos, horizon)
    outcomes: dict[ModelName, BacktestScoreV1 | SkipReason] = {}
    if full.reason is not None:
        outcomes[ModelName.CHRONOS2] = full.reason
    scored = item.known
    lags = holdouts[ModelName.SEASONAL_NAIVE].lag_known
    if lags is not None:
        with_lags = scored_week_set(item.actuals, lags)
        if len(with_lags) >= request.policy.eligibility.min_scored_holdout_weeks:
            scored = with_lags
        else:
            outcomes[ModelName.SEASONAL_NAIVE] = SkipReason.SEASONAL_LAG_UNKNOWN
    for model, holdout in holdouts.items():
        if model not in outcomes:
            outcomes[model] = _score(request, _ScoredSet(item, scored), holdout)
    return _Scored(outcomes, len(scored))


def _origin_failure(forecast: QuantileForecast, horizon: int) -> _Origin:
    shape = check_forecast_shape(forecast, horizon)
    if shape.reason is not None:
        return _Origin(None, shape.reason)
    bands = _stored(forecast)
    if first_unordered_week(bands.p10, bands.p50, bands.p90) is not None:
        return _Origin(None, SkipReason.FORECAST_BAND_UNORDERED)
    if first_negative_week(bands.p10, bands.p50, bands.p90) is not None:
        return _Origin(None, SkipReason.FORECAST_NEGATIVE)
    return _Origin(bands, None)


def _comparator_origin(
    request: ForecastRunRequest, model: ModelName, context: SeriesHistory
) -> _Origin:
    horizon = request.policy.eligibility.horizon_weeks
    if model is ModelName.SEASONAL_NAIVE:
        forecast = _seasonal(request, context, horizon)
        if forecast.lag_known is not None and not all(forecast.lag_known):
            return _Origin(None, SkipReason.SEASONAL_LAG_UNKNOWN_AT_ORIGIN)
    else:
        forecast = _ets(request, context, horizon)
    if forecast.forecast is None:
        return _Origin(None, forecast.reason)
    return _origin_failure(forecast.forecast, horizon)


def _decide(request: ForecastRunRequest, judged: _Judged) -> TargetCandidateV1:
    evaluations = judged.evaluations
    decision = choose_model(GateRequest(evaluations, request.policy.gate.margin))
    shown: BandsV1 | None = None
    if decision.chosen_model is not None:
        bands = judged.origins[decision.chosen_model].bands
        if bands is None:
            raise ValueError(f"{decision.chosen_model}: the gate chose an unpublishable model")
        shown = BandsV1(
            p10=tuple(stored_text(v) for v in bands.p10),
            p50=tuple(stored_text(v) for v in bands.p50),
            p90=tuple(stored_text(v) for v in bands.p90),
        )
    return TargetCandidateV1(
        chosen_model=decision.chosen_model,
        withheld_reason=decision.withheld_reason,
        chosen_forecast=shown,
        gate_reason=decision.gate_reason,
        scored_weeks=judged.scored_weeks,
        evaluations=evaluations,
    )


def _run_target(request: ForecastRunRequest, target: str) -> _TargetRun:
    candidates: dict[str, TargetCandidateV1] = {}
    eligible: dict[str, _Eligible] = {}
    for item in request.series.series:
        checked = _eligibility(request, item, item.targets[target])
        if isinstance(checked, SkipReason):
            evaluations = tuple(_evaluation(model, checked) for model in ModelName)
            candidates[item.canonical_asin] = _decide(request, _Judged(evaluations, {}, 0))
        else:
            eligible[item.canonical_asin] = checked
    eligibility = request.policy.eligibility
    backtests = _forecast(
        request,
        target=target,
        origin=Origin.BACKTEST,
        histories={asin: e.history for asin, e in eligible.items()},
        horizon=eligibility.holdout_weeks,
    )
    scored = {
        asin: _backtest(request, e, backtests.forecasts[asin]) for asin, e in eligible.items()
    }
    chronos_valid = {
        asin: eligible[asin].context
        for asin, s in scored.items()
        if isinstance(s.outcomes[ModelName.CHRONOS2], BacktestScoreV1)
    }
    horizon = eligibility.horizon_weeks
    chronos_origins = _forecast(
        request, target=target, origin=Origin.FORECAST, histories=chronos_valid, horizon=horizon
    )
    for asin, series_scores in scored.items():
        origins: dict[ModelName, _Origin] = {}
        for model, outcome in series_scores.outcomes.items():
            if not isinstance(outcome, BacktestScoreV1):
                continue
            if model is ModelName.CHRONOS2:
                origins[model] = _origin_failure(chronos_origins.forecasts[asin], horizon)
            else:
                origins[model] = _comparator_origin(request, model, eligible[asin].context)
        evaluations = tuple(
            _evaluation(model, series_scores.outcomes[model], origins.get(model))
            for model in ModelName
        )
        judged = _Judged(evaluations, origins, series_scores.scored_weeks)
        candidates[asin] = _decide(request, judged)
    return _TargetRun(candidates, backtests.calls + chronos_origins.calls)


def _recent_weeks(request: ForecastRunRequest, item: SeriesWeeks) -> tuple[RecentWeekV1, ...]:
    starts = request.series.week_starts
    first = len(starts) - RECENT_WEEKS
    weeks: list[RecentWeekV1] = []
    for week in range(first, len(starts)):
        cells = {target: item.targets[target].weeks[week] for target in TARGETS}
        targets = {
            target: RecentTargetWeekV1(
                state=cell.state,
                value=None if cell.value is None else stored_text(cell.value),
            )
            for target, cell in cells.items()
        }
        shipped, ordered = cells["units_shipped"], cells["units_ordered"]
        stockout: bool | None = None
        if shipped.state is WeekState.OBSERVED and ordered.state is WeekState.OBSERVED:
            stockout = (shipped.value or 0) < (ordered.value or 0)
        weeks.append(RecentWeekV1(week_start=starts[week], targets=targets, stockout=stockout))  # pyright: ignore[reportArgumentType]
    return tuple(weeks)


def _context_counts(item: SeriesWeeks) -> tuple[Counter[WeekState], int]:
    counts: Counter[WeekState] = Counter()
    inferred_days = 0
    for weeks in item.targets.values():
        for cell in weeks.weeks[weeks.context_start :]:
            counts[cell.state] += 1
            inferred_days += cell.inferred_zero_days
    return counts, inferred_days


def run_forecast(request: ForecastRunRequest) -> ForecastRunResult:
    """Forecast and backtest every target of the shard; return its candidates and counts."""
    displays = display_map(item.canonical_asin for item in request.series.series).by_canonical
    runs = {target: _run_target(request, target) for target in TARGETS}
    per_target = {target: run.candidates for target, run in runs.items()}
    reasons: Counter[str] = Counter()
    for target, outcome in per_target.items():
        for candidate in outcome.values():
            if candidate.chosen_model is not None:
                reasons[f"{target}.chosen.{candidate.chosen_model.value}"] += 1
            elif candidate.withheld_reason is not None:
                reasons[f"{target}.withheld.{candidate.withheld_reason.value}"] += 1
    candidates: list[CandidateSeriesV1] = []
    totals: Counter[WeekState] = Counter()
    inferred_days = 0
    for item in request.series.series:
        recent = _recent_weeks(request, item)
        counts, days = _context_counts(item)
        totals.update(counts)
        inferred_days += days
        money = {item.targets[t].currency_code for t in MONEY_TARGETS} - {None}
        candidates.append(
            CandidateSeriesV1(
                schema_version=1,
                canonical_asin=item.canonical_asin,
                display_asin=displays[item.canonical_asin],
                shard_index=request.shard_index,
                targets={  # pyright: ignore[reportArgumentType]
                    target: per_target[target][item.canonical_asin] for target in TARGETS
                },
                recent_weeks=recent,
                week_state_counts=_state_counts(counts),
                stockout_weeks_last_13=sum(1 for week in recent if week.stockout is True),
                currency_code=next(iter(money)) if len(money) == 1 else None,
            )
        )
    return ForecastRunResult(
        candidates=tuple(candidates),
        counts_by_reason=dict(sorted(reasons.items())),
        week_state_counts=_state_counts(totals),
        inferred_zero_days=inferred_days,
        model_calls=sum(run.calls for run in runs.values()),
    )


def _state_counts(counts: Counter[WeekState]) -> WeekStateCountsV1:
    return WeekStateCountsV1(
        observed=counts[WeekState.OBSERVED],
        inferred_zero=counts[WeekState.INFERRED_ZERO],
        unknown=counts[WeekState.UNKNOWN],
    )
