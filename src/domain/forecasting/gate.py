# SPDX-License-Identifier: MIT-0
"""Which model one series and target shows: the `gate` v1 decision (design §5c; owner decision 4).

Show Chronos-2 only where it beats SeasonalNaive and ETS on the backtest; otherwise show the
simpler model. The decision reads only the three recorded evaluations and the gate policy, so the
publisher can re-run it from a candidate and refuse one that disagrees (§6c `gate_mismatch`).

A model is **valid** when it has a backtest score and no validity reason (§5b), and
**publishable** when it is valid and its forecast at the origin passed (`origin_status`).

0. A series-level reason (eligibility, §3d) carried by every evaluation means the series never
   reached the gate: it is withheld with that reason.
1. to 2. Validity and publishability are the caller's evaluations.
3. Chronos-2 is shown when all three models are valid, `WQL_chronos < (1 - margin) x
   min(WQL_seasonal_naive, WQL_ets)`, and Chronos-2 is publishable.
4. Else the publishable valid simple model with the lower WQL is shown; on a tie, SeasonalNaive
   (`simplicity_order`). `gate_reason` records why step 3 failed, first match:
   `CHRONOS_NOT_VALID(<reason>)`, `COMPARATOR_UNAVAILABLE(<model>[,<model>])`,
   `CHRONOS_NOT_BETTER`, `CHRONOS_NOT_PUBLISHABLE(<check>)`.
5. Else nothing is shown: `ACCURACY_NOT_TESTABLE` when no simple model is valid, else
   `FORECAST_NOT_PUBLISHABLE` with `gate_reason` `FORECAST_NOT_PUBLISHABLE(<check>)`, the check
   of the valid simple model step 4 would have shown.

WQL is compared on the stored four-place values, the ones the candidate records.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from domain.forecasting.boundary import ModelEvaluationV1
from domain.forecasting.models import ModelName, SkipReason

SERIES_REASONS = frozenset(
    {
        SkipReason.ASIN_INACTIVE,
        SkipReason.CURRENCY_MIXED,
        SkipReason.TARGET_VALUE_MISSING,
        SkipReason.INSUFFICIENT_CONTEXT,
        SkipReason.HOLDOUT_UNKNOWN,
    }
)
"""Reasons decided for the series before any model runs (design §3d); each model carries it."""
SIMPLE_MODELS = (ModelName.SEASONAL_NAIVE, ModelName.ETS)
"""The simple models in `simplicity_order`, simpler first."""


@dataclass(frozen=True)
class GateRequest:
    """One series and target's three evaluations, and the gate's Chronos-2 margin."""

    evaluations: tuple[ModelEvaluationV1, ...]
    margin: float

    def __post_init__(self) -> None:
        models = sorted(evaluation.model.value for evaluation in self.evaluations)
        if models != sorted(model.value for model in ModelName):
            raise ValueError("the gate needs exactly one evaluation per model")
        if not 0 <= self.margin < 1:
            raise ValueError(f"margin must be at least 0 and below 1, got {self.margin!r}")
        for evaluation in self.evaluations:
            scored = evaluation.backtest_score is not None
            if scored == (evaluation.validity_reason is not None):
                raise ValueError(f"{evaluation.model}: exactly one of score and validity reason")
            if not scored and evaluation.origin_status != "not_attempted":
                raise ValueError(f"{evaluation.model}: an invalid model is never forecast")
            if scored and evaluation.origin_status == "not_attempted":
                raise ValueError(f"{evaluation.model}: a valid model is forecast at the origin")
            if (evaluation.origin_status == "not_publishable") != (
                evaluation.origin_failure is not None
            ):
                raise ValueError(f"{evaluation.model}: origin_failure names a failed origin only")


@dataclass(frozen=True)
class GateDecision:
    """The shown model or the withheld reason, and why Chronos-2 is not shown."""

    chosen_model: ModelName | None
    withheld_reason: SkipReason | None
    gate_reason: str | None


def _series_reason(evaluations: dict[ModelName, ModelEvaluationV1]) -> SkipReason | None:
    reasons = {evaluation.validity_reason for evaluation in evaluations.values()}
    if not reasons & SERIES_REASONS:
        return None
    if len(reasons) != 1:
        raise ValueError("a series-level reason must be carried by every model")
    (reason,) = reasons
    return reason


def _wql(evaluation: ModelEvaluationV1) -> Decimal:
    score = evaluation.backtest_score
    if score is None:
        raise ValueError(f"{evaluation.model}: an invalid model has no WQL")
    return Decimal(score.wql)


def _lowest(candidates: list[ModelEvaluationV1]) -> ModelEvaluationV1:
    """Return the lower WQL; `min` keeps the first of equals, and candidates are simpler first."""
    return min(candidates, key=_wql)


def _why_not_chronos(evaluations: dict[ModelName, ModelEvaluationV1], margin: float) -> str | None:
    """Step 3: None when Chronos-2 is shown, else the first reason it is not."""
    chronos = evaluations[ModelName.CHRONOS2]
    if chronos.validity_reason is not None:
        return f"CHRONOS_NOT_VALID({chronos.validity_reason.value})"
    unavailable = [m.value for m in SIMPLE_MODELS if evaluations[m].validity_reason is not None]
    if unavailable:
        return f"COMPARATOR_UNAVAILABLE({','.join(unavailable)})"
    best = min(_wql(evaluations[m]) for m in SIMPLE_MODELS)
    if not _wql(chronos) < (Decimal(1) - Decimal(repr(margin))) * best:
        return "CHRONOS_NOT_BETTER"
    if chronos.origin_status != "publishable":
        return f"CHRONOS_NOT_PUBLISHABLE({chronos.origin_failure})"
    return None


def choose_model(request: GateRequest) -> GateDecision:
    """Apply design §5c steps 3 to 5 to one series and target's evaluations."""
    evaluations = {evaluation.model: evaluation for evaluation in request.evaluations}
    series_reason = _series_reason(evaluations)
    if series_reason is not None:
        return GateDecision(None, series_reason, None)
    why = _why_not_chronos(evaluations, request.margin)
    if why is None:
        return GateDecision(ModelName.CHRONOS2, None, None)
    valid = [evaluations[m] for m in SIMPLE_MODELS if evaluations[m].validity_reason is None]
    publishable = [e for e in valid if e.origin_status == "publishable"]
    if publishable:
        return GateDecision(_lowest(publishable).model, None, why)
    if not valid:
        return GateDecision(None, SkipReason.ACCURACY_NOT_TESTABLE, None)
    check = _lowest(valid).origin_failure
    return GateDecision(
        None, SkipReason.FORECAST_NOT_PUBLISHABLE, f"FORECAST_NOT_PUBLISHABLE({check})"
    )
