# Response-curve, baseline-forecast and simulator prototype (synthetic)

| | |
|---|---|
| **Status** | Reviewed offline prototype. An independent review approved these results on 2026-10-06 with no blocking finding. Nothing here is implemented. |
| **Run** | 2026-10-06 (rounds 2 to 4). One arithmetic correction, 2026-10-07, noted under [Hosting projection](#hosting-projection). |
| **Evidence class** | Method evidence on generated, fictional data only. It isn't source, deployment, hosted-run or acceptance evidence. No fixture is committed, so the fixture classes of [ADR-051](../docs/decisions/adr-051-classify-fixture-provenance-and-evidentiary-status.md) don't apply. |
| **Design** | [Response curves and budget simulation](../docs/concepts/response-curves-and-budget-simulation.md) |
| **Cited by** | [ADR-090](../docs/decisions/adr-090-evaluate-budget-scenarios-at-request-time-over-stored-draws.md) · [ADR-092](../docs/decisions/adr-092-forecast-the-chronos-2-baseline-with-known-in-advance-covariates.md) · [ADR-093](../docs/decisions/adr-093-treat-overlap-clusters-and-sharing-pools-as-joint-units.md) · [ADR-094](../docs/decisions/adr-094-run-offline-modelling-on-sagemaker-processing-without-a-vpc.md) · [ADR-112](../docs/decisions/adr-112-fit-campaign-response-curves-with-a-hierarchical-adstock-and-hill-model.md) · [ADR-113](../docs/decisions/adr-113-publish-a-response-curve-only-after-held-out-identification-and-fragility-gates.md) |

## What this is

The prototype tested the proposed methods before any product code exists. A generator built fictional worlds of campaigns with known true response curves. The prototype fitted curves, ran Chronos-2 backtests and ran the simulator's rules on them, then scored each result against the known truth.

- **No customer data, and no AWS or Amazon call.** Every input is generated and fictional.
- **Local runs only.** Every time is a measurement on one Apple M5 laptop (10 cores, 24 GB, macOS arm64, Python 3.14.6). Nothing ran on SageMaker or Linux.
- **Not re-runnable from this repository.** The prototype code, raw outputs and review records stay local and aren't committed. This page is their tracked summary. It holds the main criteria, including the figures the Proposed ADRs cite and the variant tests that failed. Other rows, mostly diagnostics that feed these, stay in the local report. Figures come from the prototype's results report, with its qualifiers, with two exceptions: the [Hosting projection](#hosting-projection) is the design's arithmetic on the prototype's projection output, and the middle-tier floor's q comes from the round-4 floor output.
- **Repeatability is narrow.** One pair of cold 50-campaign fits from fresh caches, on macOS, gave identical diagnostics. Linux wasn't run, and the full reproduction script was checked by a dry run only.

## How to read a result

Each criterion and its bar come from the design's prototype plan. A verdict is PASS or FAIL against that bar.

- **Units.** A "world" is one generated estate of about 60 campaigns. The main set has 10 worlds, other sets 3 to 5, and each budget-cut cell 3.
- **Coverage.** An 80% band should hold the truth 80% of the time. The tolerance is 80% ± 2 standard errors (the larger of the binomial and the world-clustered one), and a verdict needs at least 20 units.
- **On the bar.** For budget cuts, a 90% interval that crosses a bar can't be told apart from passing or failing.

## Response curves

| Criterion | Result | Verdict |
|---|---|---|
| Convergence | Group R-hat (which compares the sampler's chains and should be near 1) ≤ 1.01 and effective sample size ≥ 400 in 2 of 10 worlds. The between-channel spread barely moved from its prior, so it is set by the prior. | FAIL |
| Interval coverage | 64.1% coverage against a tolerance of 73.7% to 86.3%, with marginal ROAS a median 13.4% high, in the best-case worlds | FAIL |
| Pooling helps thin campaigns | On 40 flat campaigns in 5 worlds, median absolute relative error was 0.40 pooled against 0.25 unpooled. Pooling's benefit wasn't shown. | FAIL |
| Linear truths get no increases | 0 of 60 linear-truth campaigns could drive increases | PASS (rests on the identification and contraction gates) |
| Held-out benchmark gate on its own | It false-passed 20% to 28% of linear-truth campaigns, against a proposed 10% bar, with 28% to 36% power on saturating ones. The fixed ladder of stricter settings ended at its last step, the campaign-level test switched off. | FAIL |
| Saturating truths reach a usable state | 2.8% reached `fitted` or `pooled_only` (16.7% of those with at least 2 counted spend events), against 50% | FAIL |
| Intended label | 63.0% of campaigns got their intended label, against 90% | FAIL |
| Hidden demand | Where hidden demand moved spend and revenue together, marginal ROAS read 14% to 35% high, with 28% to 42% coverage. The main fit didn't move in the predicted direction, and the two capping fits didn't bracket the truth. | FAIL |
| Stock outage | A 60-day outage product was frozen on 60 of 60 days, but renormalising moved its weight 13% to 43% | FAIL as written; holding the weight fixes it |
| Peak sensitivity | The peak flag fired on 8.6% of 70 campaigns. Under the envelope bound, 14% of cuts read more than 20% favourable. | FAIL |
| Fragile campaign flagged | Flagged `unstable` in 6 of 10 worlds against 80%, with 2.5% false alarms | FAIL |
| Seasonal shape | Correlation of the fitted and true season 0.986 for the tied-Hill form of the earlier block, which the prototype picked as the starting value, weakly. The linear form reached 0.989 and no seasonal term 0.982. | PASS |
| Coverage with an additive season | In a world set with an additive seasonal part, every curve was either covered or demoted. No curve in that set reached `fitted`, so the pass is vacuous. Raw coverage was 60.6%, with a median bias of −0.9%. | PASS (vacuous) |
| Capping data missing, detection off | 300 campaigns: 61.1% coverage against a tolerance of 74.5% to 85.5% | FAIL |
| Capping data missing, hourly heuristic only | 300 campaigns: 61.1% coverage against 72.4% to 87.7%, with a median bias of +16.0% | FAIL |
| Rolling-refit stability | The rolling part passed in world 1. In world 2 all three group spreads (κ, s and α) moved beyond their 2-MCSE (Monte Carlo standard error) bounds: κ by 0.0154 against 0.0085, s by 0.0085 against 0.0043, α by 0.0141 against 0.0130. World 2's refits had group effective sample sizes of 117 to 401, under the convergence bar of 400, so their bounds are the less reliable ones. The shard part failed in world 1. The prototype reuses no posterior, so the moving data window is a candidate cause; that wasn't tested. | FAIL: rolling part in world 2, shard part in world 1 |

Across all gates, 372 of 600 curves ended `weakly_identified`.

## Budget cuts and the late-hour assumption

A cut's error is e = (p50 loss − true loss) ÷ true loss. Bar (a): the median e is at least −5%. Bar (b): at most 20% of cuts have e below −20%, which would understate the loss badly. Each cell holds 89 to 97 cuts from 45 to 49 campaigns in 3 worlds. The 90% intervals come from 2,000 bootstrap resamples that **resample campaigns only**. Resampling worlds as well widens them.

At flat hours (late-hour factor f = 0), reading every cut with the constant-ROAS line as well as the curve:

| Spend input | Median e [90%] | Share below −20% [90%] | Reading |
|---|---|---|---|
| The simulator's own spend model, which the product uses | −5.9% [−12.8, +1.2] | 31.5% [22.6, 41.2] | Bar (a) on the bar. Bar (b) fails at campaign level, and is on the bar when worlds are resampled too ([15.1, 46.2]). |
| The generator's true expected spend | −0.9% [−7.9, +5.9] | 18.0% [9.6, 27.0] | On both bars |

- **The curve alone** fails bar (b) at f = 0 in 99% to 100% of resamples.
- **The break-even late-hour factor is unresolved.** Under the late-higher hourly profile with true spend, bar (b) breaks even inside the tested grid in 67% of resamples. Among those, the 5th to 95th percentile is 0.10 to 0.40.
- **Outside the late-hour cells: PASS.** In the main world set and two other generated sets, the shipped constant-ROAS reading passed both bars, with the spend model and with true spend. These cuts came from curves in every state, because the gated curves gave too few cuts to score. Whether a curve reads zero at zero spend was untested, because every channel had fewer than 20 low-spend weeks.
- **The lowest volume tier** (under 5 orders a week) rests on 5 to 7 campaigns, so its freeze is a caution, not a measured failure.
- **The middle-tier floor.** The 80th percentile of measured understatement in tier 2 at flat hours was q = 0.2708 (44 cuts). The design sizes the floor as p50 ÷ (1 − q), about 1.37 × p50 (arithmetic). The prototype's own output used 1 + q, about 1.27 ×, which the design later corrected.
- **The volume guard** reads a low-volume cut by budget arithmetic. For capped tier-1 campaigns in the default family (16 campaigns), it cut the share of cuts understated by more than 20% from 18.8% to 0%. It also raised overstatement: the p90 cut-size error went from 0% to +52.6% in one small group, and from +5.8% to +17.2% in another. Its 14-day threshold was chosen on the same worlds, and it wasn't run through the scenario totals.

## Simulator

| Criterion | Result | Verdict |
|---|---|---|
| Safety rules | A property test over 10,000 random scenarios: expected spend never above the allowance or demand bound, a cut's whole loss always in the total, no increase read past observed spend. A separate test: a total that includes a display-only curve is refused. | PASS |
| Multi-campaign totals | 80% bands covered the truth in 50.0% of 44 totals, against a tolerance of 67.9% to 92.1%. The median total overstated losses by 24%. Draw alignment changed almost nothing; the central estimate is the larger cause. | FAIL |
| Sharing pools | Sharing shrank the change in 41.5% of worlds in the simulator, against 65.9% in the truth | FAIL |
| Overlap clusters | A move between campaigns in one cluster was flagged and summed nothing: PASS. Cluster curves, 4 cluster units per world in worlds 1, 3 and 5, true expected spend as input. Under single credit, the p50's error against the pool's true change and the 80% band's coverage were: 0.7× budget −32% (10 units, 40% covered), pause −1.1% (12, 58%), 1.3× budget +29% (8, 25%). Under double credit, in worlds 3 and 5 (world 1's fit had a stuck chain and was left out), the errors were −65%, −35% and +64%. | rule PASS; cluster-curve scoring FAIL |
| Increases in totals | 0% of reallocations had any in-total gain in the main world set and one other set, and 2.8% in a third | reported |
| Who sets the cut loss (round 2, reused unchanged since) | With the constant-ROAS reading on, share of in-total cut loss set by each reading. Default worlds, cut-only plans: the curve 79%, the line 21%. Default worlds, mixed plans: the curve 41%, the line 57%. Budget-lever cell with late hours earning less, at a late-hour factor of 0.5, cut-only plans: the line 86%. | reported |

## Baseline forecast

| Criterion | Result | Verdict |
|---|---|---|
| Output shape | All 21 quantile levels returned and missing values accepted; the largest quantile crossing was 3.2% of the series maximum | FAIL (strict) |
| Beats simple baselines | Median weighted quantile loss ratio to the better of SeasonalNaive (the value from the same point one season back) and ETS (exponential smoothing of error, trend and seasonality): revenue 0.874 to 0.897, `pacing_spend` 0.714 to 0.856, phase-spread origins 0.901 to 0.943. The comparators are the prototype's own, not the pinned statsmodels ones, so the margin is likely overstated. | PASS against the prototype's comparators |
| No look-ahead | Every scored window is complete, and tuning windows end before the publication origins. A promotion planned before an origin counts at that origin even if it was cancelled later. | PASS |
| Early-peak refusal | 23 of 23 injected early peaks refused | PASS |
| Mean method | Where the recent level matched the truth, on phase-spread origins (spread from late February to late June), the forecast mean was within ±5% except tier 1 at 90 days (−5.1%). At the eight window-end publication origins, those rows ran +8% to +38% high at 7 and 30 days and passed only at 90. | PASS on phase-spread origins only |
| Level | Per 90-day origin, the level error ran from about −19% to +41%, and 1 of 9 origins per tier was within ±5%. Chronos-2's context never holds a season twice, so it lags the season. | FAIL in every tier and horizon |

## Hosting projection

At 5,000 campaigns, job-hours are a projection: arithmetic on the local timings and on AWS list prices read on 2026-10-05 and 2026-10-06. Host speed and job start-up are [unverified] until the hosted run-time test.

**Correction, 2026-10-07.** The first projection counted 9 Chronos-2 inferences per series per day. The backtest scores three horizons on separate origin sets, so the true count is 25. The figures below use 25.

| Backtest anchoring | Chronos-2 job-hours a month | Total job-hours a month | One-host bound | Start budget per job |
|---|---|---|---|---|
| Daily-sliding origins | about 125.7 | about 147.3 | about 167.7 h | about 4.3 min |
| Weekly-anchored, no restatement reaching a cached origin | about 7.5 | about 29.1 | about 76.2 h | about 10.0 min |
| Weekly-anchored, daily restatement reaching the four newest 7-day origins | about 26.1 | about 47.7 | about 90.6 h | about 9.1 min |
| Weekly-anchored, reconstructed promotion covariates active (upper bound) | about 83.7 | about 105.3 | about 135.2 h | about 6.3 min |

- Every total includes the curve fits and about 10.2 job-hours of quarterly Chronos-2 tuning. Without the tuning, the daily-sliding total is about 137.1.
- Against daily-sliding, weekly anchoring cuts Chronos-2 job-hours by about 79% to 94% (arithmetic: 1 − 26.1 ÷ 125.7 and 1 − 7.5 ÷ 125.7). That range holds only if every source restates at most 28 days back and no reconstructed promotion or coupon covariate is active. With reconstructed covariates active, and restatements still within 28 days, the cut is at least about 33% (1 − 83.7 ÷ 125.7). If a source restates further back, the figure can approach the daily-sliding one.
- The one-host bound is the SageMaker billed hours at which SageMaker Processing on ml.c5.2xlarge costs the same as Fargate ARM64 at local speed plus its private network.
- One scenario inside the weekly range, 44.1 job-hours, costs about $17.98 a month on ml.c5.2xlarge, against about $35.83 for Fargate with the network.

## Not proven here

- Behaviour on real customer data, and every Amazon API behaviour the design depends on.
- The data contracts, source packs and load path. The prototype read only its own fixtures.
- Hosted run time, start-up, memory at full batch size, and Linux packaging.
- Double counting in the product's own refit path, because the prototype reuses no posterior.
- Every result depends on the generator's fictional choices: spend ranges, edit and promotion frequency, noise and shock sizes.
