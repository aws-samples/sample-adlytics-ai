# ADR-156: Classify forecast weeks as observed, inferred zero or unknown

Date: 2026-11-11
Status: Accepted (2026-11-11), approval DR-5. Built in dev by slice 1 of the demand forecast; not
yet deployed outside dev or live-accepted.

## Context

The weekly Vendor demand forecast reads daily Vendor Central sales by ASIN. A missing row can mean a
day with no sales, a day Amazon never sent, or a product that left the catalogue. Filling every gap
with zero would teach the models demand that never happened. Dropping gap weeks and joining the rest
would pair weeks that are not adjacent.

## Decision

1. **Three day states, versioned as policy.** A day is `observed` when a by-ASIN row carries the
   value, `inferred_zero` when there is no row but the day is complete for the forecast set and the
   product was active in the trailing `missing_weeks.activity_window_weeks`, and `unknown` otherwise.
   A day is complete only when one of the pinned sales publications has it as its bucket.
2. **Weeks roll up from days.** Seven inferred-zero days make an inferred-zero week (0); any observed
   day and no unknown day make an observed week (the sum); any unknown day makes an unknown week.
   `src/domain/forecasting/weekly_series.py` owns both rules.
3. **Unknown weeks are never filled.** They reach Chronos-2 and ETS as NaN, and SeasonalNaive skips
   a pair with an unknown week. Every model is scored on one common week set.
4. **Only a product's own gap restarts its history.** A run of unknown weeks longer than
   `missing_weeks.max_unknown_run_weeks` that belongs to the product restarts its context after the
   gap. A gap in the Vendor days, which every product shares, never restarts one.
5. **The policy is a contract.** `contracts/forecasting/demand-forecast.yaml` `missing_weeks` holds
   the values with a version, and every run records that version.

## Consequences

- A forecast never rests on a week the source did not describe.
- One unlanded Vendor day makes that week unknown for every product until the day lands.
- Every generation records each series' observed, inferred-zero and unknown week counts, and the
  `DemandForecastInferredZeroShare` alarm watches the set-wide inferred-zero share.

## Alternatives

- Treat every missing day as zero: invents demand for days Amazon did not report.
- Drop unknown weeks and join the rest: pairs weeks that are not adjacent.
- Restart a product's history at any gap, the set's included: one missing Vendor day would reset
  every product.
