# ADR-092: Forecast the Chronos-2 baseline with known-in-advance covariates and a pacing-spend target
Date: 2026-10-07
Status: Proposed. Not accepted or implemented; acceptance waits on the hosted run-time test. On 2026-10-07 the owner accepted the recommendation to keep scenario rows when only the baseline level fails. Weekly-anchored backtests were chosen on the owner's behalf the same day, under authority the owner delegated, and the rest of the design was approved the same way; the owner hasn't reviewed the final design yet. It would amend [ADR-001](adr-001-chronos-deployment.md)'s target set and the forecasting low-level design's artifact contract; both stay in force until this is accepted. ADR-001's offline decision stands. The design is summarised in [Response curves and budget simulation](../concepts/response-curves-and-budget-simulation.md).
## Context
The simulator adds a curve's modelled change to a baseline: what each campaign spends and earns over the next 90 days if nothing changes. ADR-001 already decides an offline daily Chronos forecast of two ad-attributed outcomes. The simulator needs three more things:
- **Expected spend under the current budget,** to turn a budget into spend.
- **A daily mean it can sum.** A sum of daily medians isn't the median of the sum.
- **Covariates that obey "known at the origin".** A future promotion counts only if it was scheduled by the forecast date.

Spend must never be a future covariate, or the baseline stops meaning "no change".
## Decision
**One daily Chronos-2 forecast per series, with known-in-advance covariates only, a new internal `pacing_spend` target, and a stored quantile grid with a daily mean.**
1. **Targets.**
   - `attributed_revenue_14d` and `attributed_purchases_14d`, as ADR-001 decides.
   - `pacing_spend` for sponsored campaigns. This is budget-basis spend on the campaign-timezone day, with capped days set to NaN. It is internal to the simulator and not shown or exposed to the agent.
2. **Covariates.**
   - Known in the future: day of week, fixed holidays, announced peak events, and scheduled promotions and coupons. Each one obeys the as-of-vintage rule. Promotions and coupons stay `reconstructed` until status snapshots exist.
   - Past only: one exposure measure, past spend for the outcome targets, and start-of-day availability.
   - Never: spend, budget, or any field scaled by the outcome.
3. **Context and horizon.** 180 to 365 days of context, 90 days ahead. Every tuning window ends before the earliest publication origin.
4. **Artifact.** Per series and day: the 21-level quantile grid (or p10, p50 and p90 when only those return), `mean_d` and `mean_method`. Every summed baseline uses daily means; p50 is for display only. Expected spend under a cap is taken inside the `pacing_spend` distribution.
5. **Backtest and publish rule.** Rolling-origin backtests run against two baselines from statsmodels: SeasonalNaive, which repeats the value from the same point one season back, and ETS, exponential smoothing of error, trend and seasonality. A series publishes only if, at each horizon, its weighted quantile loss is no worse than the better baseline by more than a policy margin. Otherwise it is `held(backtest_fail)`, and the last accepted generation stays readable as stale.
6. **Weekly-anchored backtests.** The cache key covers the series, horizon, origin, a digest of the context and covariate vintages, a digest of the known-future values fed in, and the model configuration. It doesn't cover the cross-learning group.
7. **Two baseline checks, two rules.** Each is validated per volume tier against noise-free expected sums.
   - **Mean-method failure removes rows.** If the daily-mean method fails in a tier and no available method passes, that tier's rows leave the scenario row set, with the reason `baseline_mean_unvalidated`. They are kept at today's spend and stay out of plan ROAS and every total until a later policy version passes.
   - **Level failure keeps rows (owner, 2026-10-07).** If only the forecast level fails, the rows stay in scenario totals for the modelled change, which depends on the curves and expected spend, not on the baseline level. Only figures built on the level (plan ROAS and its consistency check) are withheld, as `baseline_level_unvalidated`.
8. **Batches.** Series run as independent parallel batches. Each cross-learning call has one origin, one horizon and one target ([ADR-114](adr-114-run-curve-and-forecast-jobs-as-execution-graph-workloads.md)).
## Consequences
- ADR-001's target set grows by one internal spend target, which needs the Ads API (ADR-089).
- At 5,000 campaigns, weekly anchoring is projected to cut Chronos-2 compute by about 79% to 94%, but only while every source restates at most 28 days back and no reconstructed promotion or coupon covariate is active. With reconstructed covariates active, and restatements still within 28 days, the projected cut is at least about 33% (Evidence). Backtests then score origins up to six days old.
- Forecasts lag a season they have never seen twice. That is why a level failure withholds only level-based figures.
- The mean method already fails in the prototype at 90 days in the lowest tier and at the publication origins at 7 and 30 days, so some tiers' rows can leave the row set at first.
- The forecasting `chronos-forecasting` and `torch` CPU stack sits in its own exactly pinned dependency group, installed only in the job image.
## Alternatives considered
| Alternative | Why not, or why |
|---|---|
| **Chronos-2, in-process on CPU, zero-shot with covariates** | **Chosen** (owner, 2026-10-06) |
| AutoGluon-TimeSeries | Needs Python below 3.14, so a separate image; used only for its baselines, which plain Python also gives |
| Amazon Forecast | Closed to new customers |
| SageMaker Canvas | Console-first, not a versioned pipeline contract |
| Daily-sliding backtests | Every origin moves daily, so all backtest inferences recompute every day |
| **Weekly-anchored backtests with a cache** | **Chosen** (on the owner's behalf, 2026-10-07): origins anchor to the newest complete ISO week. On the other six days only the live inference runs, plus any cached origins a restatement reaches |
## Open questions
- **A seasonal input for the level problem** (the curve fit's per-channel seasonal index, or a longer Ads API v1 history) is the next thing to test. It is not run yet.
- The baselines' win was measured against hand-built comparators. It is re-scored against the pinned statsmodels comparators before the margin is cited.
- Whether the vendor coupon report keeps deactivated coupons is [unverified] until a planned probe checks it. Until it is checked, coupon covariates stay `reconstructed`.
## Evidence
- Chronos-2 is zero-shot with past and known-future covariates, an 8,192-step context and up to 1,024-step horizons. `chronos-forecasting` requires Python 3.10 or later [verified 2026-10-05, Chronos-2 model card and PyPI].
- AutoGluon-TimeSeries 1.6.3 supports Python 3.10 to below 3.14 [verified 2026-10-05, PyPI]. Amazon Forecast is closed to new customers [verified 2026-10-05, developer guide].
- `torch` 2.14.1 has cp314 manylinux wheels for x86_64 and aarch64 [verified 2026-10-06, PyPI JSON].
- Offline prototype on fictional data, local Apple-silicon runs, 2026-10-06 ([prototype summary](../../validation/response-curve-prototype.md), Baseline forecast):
  - **mean method, shown sound on phase-spread origins only** (origins spread from late February to late June): where the recent level matched the truth, the forecast mean was within ±5% except the lowest tier at 90 days (−5.1%). At the eight window-end publication origins, those rows ran +8% to +38% high at 7 and 30 days and passed only at 90;
  - **level, failed in every tier and horizon:** per 90-day origin, the level error ran from about −19% to +41%;
  - against the prototype's own comparators, the median weighted quantile loss ratio was about 0.71 to 0.94 across targets and origin sets.
- Weekly versus daily anchoring at 5,000 campaigns is about 7.5 to 26.1 against about 125.7 Chronos-2 job-hours a month, a cut of about 79% to 94% (projection, arithmetic on those local timings; [prototype summary](../../validation/response-curve-prototype.md), Hosting projection). The low end recomputes no cached origin; the high end recomputes the four newest 7-day origins every day. The range holds only while every source restates at most 28 days back, which is [unverified] for Ads API v1 reports, and no reconstructed promotion or coupon covariate is active. With such covariates active and restatements still within 28 days, the Chronos-2 figure can reach about 83.7 job-hours, still at least about 33% below daily-sliding. A source that restates further back can push it toward the daily-sliding figure.
## Review trigger
A hosted run outside the projected job-hours; the seasonal-input test; or a source that restates more than 28 days back.
