# SPDX-License-Identifier: MIT-0
"""One shard's forecast run: Chronos-2 forecast and backtest per target, no gate (slice 1).

Adapted from the source engine's forecast run; its contract-keyed suppliers and lake staging are
replaced by the boundary records (design §4e), and comparators and the gate arrive in slice 2.

Per target, in order:

1. Eligibility from each series' context (design §3d): an ASIN with no observed day in the
   activity window ending `input_end` is `ASIN_INACTIVE`; a money series whose currency codes mix
   is `CURRENCY_MIXED`; fewer than `min_context_weeks + holdout_weeks` known weeks, or fewer than
   `min_context_weeks` known weeks before the holdout, is `TARGET_VALUE_MISSING` when the
   target's history holds a declared-null value and `INSUFFICIENT_CONTEXT` otherwise; fewer than
   `min_scored_holdout_weeks` known holdout actuals is `HOLDOUT_UNKNOWN`.
2. Backtest: Chronos-2 forecasts the holdout from the weeks before it, in the declared batches
   (design §4c). A bad shape or a non-finite value, an unordered scored week or an all-zero
   holdout makes Chronos-2 not valid (design §5b).
3. Forecast origin: every valid series is forecast 13 weeks from `input_end` and stored under the
   numeric policy; shape, finiteness, an unordered band, then a negative value make it not
   publishable (design §4d).
4. Candidate (slice 1 rule): `chronos2` when valid and publishable, else withheld with
   Chronos-2's own reason; `seasonal_naive` and `ets` are recorded `MODEL_NOT_RUN`.

A target whose every series in this shard is withheld still gives a candidate: one shard can't
decide coverage, so the commit step checks that every target has a shown series across all shards
(design §6c, `held_quality(target_empty)`, slice 2). The model is reached through
`BatchForecaster`, which the job binds to its model adapter; domain code never imports a port or
a model library.
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
from domain.forecasting.scoring import BacktestScoreRequest, score_backtest, scored_week_set
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


class ForecastRunError(Exception):
    """The shard can't produce a candidate; `code` is the job's failure category."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class ForecastRunRequest:
    """One shard's classified series, the policy, the set and the model to call."""

    shard_index: int
    forecast_set: ForecastSetV1
    series: WeeklySeriesResult
    policy: DemandForecastPolicy
    forecaster: BatchForecaster

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
    """One target's Chronos-2 outcome per ASIN and the model calls it made."""

    outcome: dict[str, _Chronos]
    calls: int


@dataclass(frozen=True)
class _Bands:
    p10: tuple[Decimal, ...]
    p50: tuple[Decimal, ...]
    p90: tuple[Decimal, ...]


@dataclass(frozen=True)
class _Chronos:
    """Chronos-2's evaluation of one series and target, and its forecast when publishable."""

    evaluation: ModelEvaluationV1
    reason: SkipReason | None
    bands: BandsV1 | None
    scored_weeks: int


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


def _not_run(model: ModelName) -> ModelEvaluationV1:
    return ModelEvaluationV1(
        model=model,
        backtest_score=None,
        validity_reason=SkipReason.MODEL_NOT_RUN,
        origin_status="not_attempted",
        origin_failure=None,
    )


def _chronos(
    reason: SkipReason | None,
    score: BacktestScoreV1 | None = None,
    origin: tuple[str, str | None] = ("not_attempted", None),
) -> ModelEvaluationV1:
    status, failure = origin
    return ModelEvaluationV1(
        model=ModelName.CHRONOS2,
        backtest_score=score,
        validity_reason=reason,
        origin_status=status,  # pyright: ignore[reportArgumentType]
        origin_failure=failure,
    )


@dataclass(frozen=True)
class _Eligible:
    history: SeriesHistory
    context: SeriesHistory
    actuals: tuple[float | None, ...]
    scored: tuple[int, ...]


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
        scored=scored,
    )


def _origin_failure(
    forecast: QuantileForecast, horizon: int
) -> tuple[SkipReason | None, _Bands | None]:
    shape = check_forecast_shape(forecast, horizon)
    if shape.reason is not None:
        return shape.reason, None
    bands = _stored(forecast)
    if first_unordered_week(bands.p10, bands.p50, bands.p90) is not None:
        return SkipReason.FORECAST_BAND_UNORDERED, None
    if first_negative_week(bands.p10, bands.p50, bands.p90) is not None:
        return SkipReason.FORECAST_NEGATIVE, None
    return None, bands


def _run_target(request: ForecastRunRequest, target: str) -> _TargetRun:
    eligibility = request.policy.eligibility
    outcome: dict[str, _Chronos] = {}
    eligible: dict[str, _Eligible] = {}
    for item in request.series.series:
        checked = _eligibility(request, item, item.targets[target])
        if isinstance(checked, SkipReason):
            outcome[item.canonical_asin] = _Chronos(_chronos(checked), checked, None, 0)
        else:
            eligible[item.canonical_asin] = checked
    holdout = eligibility.holdout_weeks
    backtests = _forecast(
        request,
        target=target,
        origin=Origin.BACKTEST,
        histories={asin: e.history for asin, e in eligible.items()},
        horizon=holdout,
    )
    valid: dict[str, BacktestScoreV1] = {}
    for asin, item in eligible.items():
        forecast = backtests.forecasts[asin]
        shape = check_forecast_shape(forecast, holdout)
        if shape.reason is not None:
            outcome[asin] = _Chronos(_chronos(shape.reason), shape.reason, None, len(item.scored))
            continue
        bands = _stored(forecast)
        scored = score_backtest(
            BacktestScoreRequest(
                history=item.history.values,
                actuals=item.actuals,
                p10=bands.p10,
                p50=bands.p50,
                p90=bands.p90,
                scored_weeks=item.scored,
                min_scale_pairs=eligibility.min_context_weeks - 1,
            )
        )
        if scored.score is None:
            reason = scored.invalid_reason
            outcome[asin] = _Chronos(_chronos(reason), reason, None, len(item.scored))
            continue
        valid[asin] = scored.score
    horizon = eligibility.horizon_weeks
    origins = _forecast(
        request,
        target=target,
        origin=Origin.FORECAST,
        histories={asin: eligible[asin].context for asin in valid},
        horizon=horizon,
    )
    for asin, score in valid.items():
        reason, bands = _origin_failure(origins.forecasts[asin], horizon)
        scored_weeks = len(eligible[asin].scored)
        if reason is not None or bands is None:
            evaluation = _chronos(
                None, score, ("not_publishable", reason.value if reason else None)
            )
            outcome[asin] = _Chronos(evaluation, reason, None, scored_weeks)
            continue
        shown = BandsV1(
            p10=tuple(stored_text(v) for v in bands.p10),
            p50=tuple(stored_text(v) for v in bands.p50),
            p90=tuple(stored_text(v) for v in bands.p90),
        )
        outcome[asin] = _Chronos(
            _chronos(None, score, ("publishable", None)), None, shown, scored_weeks
        )
    return _TargetRun(outcome, backtests.calls + origins.calls)


def _target_candidate(chronos: _Chronos) -> TargetCandidateV1:
    evaluations = (
        chronos.evaluation,
        _not_run(ModelName.SEASONAL_NAIVE),
        _not_run(ModelName.ETS),
    )
    shown = chronos.bands is not None
    return TargetCandidateV1(
        chosen_model=ModelName.CHRONOS2 if shown else None,
        withheld_reason=None if shown else chronos.reason,
        chosen_forecast=chronos.bands,
        gate_reason=None,
        scored_weeks=chronos.scored_weeks,
        evaluations=evaluations,
    )


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
    per_target = {target: run.outcome for target, run in runs.items()}
    reasons: Counter[str] = Counter()
    for target, outcome in per_target.items():
        for chronos in outcome.values():
            if chronos.bands is not None:
                reasons[f"{target}.chosen.{ModelName.CHRONOS2.value}"] += 1
            elif chronos.reason is not None:
                reasons[f"{target}.withheld.{chronos.reason.value}"] += 1
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
                    target: _target_candidate(per_target[target][item.canonical_asin])
                    for target in TARGETS
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
