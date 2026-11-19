# ADR-157: Show Chronos-2 only where it beats both simple models

Date: 2026-11-11
Status: Accepted (2026-11-11), approval DR-6 (owner decision 4). Built in dev by slice 2 of the
demand forecast; not yet deployed outside dev or live-accepted. For this solution it replaces
[ADR-092](adr-092-forecast-the-chronos-2-baseline-with-known-in-advance-covariates.md) point 5,
which stays Proposed for the ad-attributed baseline.

## Context

The owner decided: show Chronos-2 for a series only where it beats SeasonalNaive and ETS on the
backtest, otherwise show the simpler model. A planner must also see a tested accuracy for every
forecast shown, and demand can't be negative.

## Decision

1. **One 13-week holdout per series.** Each available model forecasts the held-out weeks from the
   weeks before them and is scored by WQL over p10, p50 and p90. MASE is reported, never decisive.
2. **Chronos-2 needs all three models valid.** It is shown only when Chronos-2, SeasonalNaive and ETS
   are all valid on the holdout, Chronos-2's WQL is lower than both by the policy margin, and its
   13-week forecast passes the numeric checks. Otherwise the record names why
   (`COMPARATOR_UNAVAILABLE`, `CHRONOS_NOT_BETTER` or `CHRONOS_NOT_PUBLISHABLE`).
3. **Else the better simple model.** The valid, publishable simple model with the lower WQL is shown;
   on a tie, SeasonalNaive.
4. **Else nothing.** No valid simple model is `ACCURACY_NOT_TESTABLE`; none publishable is
   `FORECAST_NOT_PUBLISHABLE`. The product is listed with its reason.
5. **Simple models' ranges never go below zero.** Their p10, p50 and p90 are censored at zero in the
   backtest and at the origin, so the score is the score of what is shown. Chronos-2 is never
   censored; a negative Chronos-2 forecast is not publishable.
6. **The rule is policy.** `contracts/forecasting/demand-forecast.yaml` `gate` holds the metric,
   margin, simplicity order and fallbacks; `src/domain/forecasting/gate.py` `choose_model()` applies
   them. A change is a new policy version and a new owner approval.

## Consequences

- The page and the tools state only the shown model's own accuracy.
- Products with short or intermittent history get a simple model where one is valid, rather than
  nothing.
- A single holdout is a weaker test than ADR-092's rolling origins. It is the protocol the source
  engine validated, and it is recorded here as a known limit.

## Alternatives

- Compare only the comparators that could be scored: would show Chronos-2 when it beats ETS alone,
  which changes owner decision 4.
- Show an untested simple model when none can be scored: a forecast without a tested accuracy.
