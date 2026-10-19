# ADR-113: Publish a response curve only after held-out, identification and fragility gates
Date: 2026-10-07
Status: Proposed. Not accepted or implemented. On 2026-10-07 the owner accepted the recommended interim late-hour assumption below. The rest of the design was approved on the owner's behalf on 2026-10-07, under authority the owner delegated; the owner hasn't reviewed the final design yet. Policy starting values need the statistical review before activation. The design is summarised in [Response curves and budget simulation](../concepts/response-curves-and-budget-simulation.md).
## Context
A curve from [ADR-112](adr-112-fit-campaign-response-curves-with-a-hierarchical-adstock-and-hill-model.md) can converge and still be wrong. It can fit the past and fail on recent weeks. It can learn its bend from a few weeks where demand moved spend and revenue together. It can shift from week to week for no reason in the data. And it describes the lever spend moved by, mostly demand, bids and costs, not budgets.
That last point matters for cuts. At unchanged bids, a lower budget removes the clicks of the last hours of each capped day, bought at the campaign's own bids. If those late hours earn the day's average, the revenue it gives up is close to the cut times the campaign's average return. Whether they do is [unverified] (point 7). A bending curve reads a smaller loss. A reader who trusts that smaller loss cuts too much.
## Decision
**A curve is used only within what its gates allow, every gate runs on every curve, and every sponsored budget cut is read cautiously.**
1. **Gates**, numbered for reference below:
   - **Gate 1, eligibility:** enough active weeks and spend variation, and little suppression.
   - **Gate 2, known capping.**
   - **Gate 3, convergence:** R-hat (which compares the sampler's chains and should be near 1), effective sample size and divergences.
   - **Gate 4, held-out floor** against a naive forecast.
   - **Gate 5, held-out benchmark** against a refitted constant-ROAS line of the same structure.
   - **Gate 6, identification:** events that moved spend and weren't reactions to performance, plus collinearity and season checks.
   - **Gate 7, contraction** from the group.
   - **Gate 8a, fragility:** the full fit against the held-out refit.
   - **Gate 8b, publication fence** against the last published revision.

   Thresholds are versioned policy.
2. **States.** The state is the most severe outcome, in the order `held` > `insufficient_variation` > `unstable` > `weakly_identified` > `pooled_only` > `fitted`. Flags such as `capping_sensitive` and `peak_sensitive` accumulate.
3. **Total eligibility.** Each published curve carries what it may drive: `none`, `decrease_within_support`, or `increase_and_decrease`.
   - `held`, `insufficient_variation` and `unstable` drive nothing.
   - `weakly_identified`, `shard_sensitive`, and a curve whose held-out test passed only on its channel's pooled weeks (point 4) drive decreases only.
   - `pooled_only` and `fitted` drive increases and decreases within support (point 5), with or without the capping and peak flags.

   The snapshot publisher may only lower eligibility, never raise it.
4. **Held-out decision rule.** The rule is fixed:
   - a paired weekly difference in continuous ranked probability score (CRPS) has to beat a policy margin, and the lower bound of a moving-block bootstrap has to sit above zero;
   - ties fail;
   - with too few or too flat held-out weeks, the test pools the channel, and a pooled pass allows cuts only.

   Gate 5 follows a fixed ladder of four steps, stopping at the first that keeps its false-pass rate under the bar: (1) an 80% bound with a minimum held-out length `k_min` of 2 × the bootstrap block length `l_b`; (2) 80% with `k_min` = 3 × `l_b`; (3) 90%, with whichever `k_min` false-passed less; (4) the campaign-level test switched off (`gate5_campaign_level = off`), so every campaign uses the channel-pooled path, a gate 5 pass gives at most `decrease_within_support`, and no curve is offered increases until a later policy version passes the gate's own false-pass test. The starting value is `gate5_campaign_level = on`, step 1. The gate's false-pass test is re-run at onboarding on the customer's data, and the statistical review approves the step before activation. When the ladder raises the held-out length, gates 4 and 8a, which share that refit, are re-scored first.
5. **Support.** Increases never read past the highest adstocked spend the campaign has shown. A cut below the lowest, or from above the highest, is read cautiously, so no part of its loss is read off the curve's unsupported shape. This doesn't stop a cut inside the observed range from being understated (Evidence).
6. **Every sponsored budget cut uses a conservative envelope with the constant-ROAS line.** While `budget_cut_comparator = constant_roas`, the larger loss of the curve and the line is used, and the band is widened to cover both. The policy may move to `none` only after a budget-lever test shows the curve alone is close enough in every hourly profile.
7. **The late-hour assumption.** How much a day's last hours return is [unverified]. It is a versioned policy, `late_hour_factor`. The interim value is **f = 0** (late hours earn the day's average), **labelled an assumption**. A failure that comes from this assumption is keyed to the factor across every volume tier. A failure that comes from low volume is keyed to the tier.
8. **Cautious freezes stay.** Sponsored cuts are frozen as `budget_cut_unvalidated` in the lowest volume tier (under 5 orders a week) everywhere, and in the middle tier at flat hours. The middle tier may instead take a loss-side band floor, sized p50 ÷ (1 − q), with q the 80th percentile of the measured understatement. It is labelled "range widened for testing bias".
9. **The volume guard is a proposal.** Below a minimum number of days with an outcome, a cut's spend is read by budget arithmetic. It isn't adopted until the guarded spend model has run through the scenario totals.
## Consequences
- Which reading sets an in-total cut loss, the curve or the constant-ROAS line, varies, and the measured evidence is mixed. On synthetic data the curve set most of the loss in cut-only plans in the default worlds, and the line set most of it in mixed plans and in a budget-lever cell where late hours earn less (Evidence). So the curve supplies the gates, the support bounds, the increases it may read and the detail chart, and it also sets many cut losses.
- Many curves start `weakly_identified` and drive cuts only, so few increases are modelled at first. If the onboarding test ends the gate 5 ladder at step 4, as the synthetic test did, no increase is modelled at all until a later policy version passes.
- Every state, flag and eligibility is a stable code with plain text. The page shows them ([ADR-090](adr-090-evaluate-budget-scenarios-at-request-time-over-stored-draws.md)).
- The fragility gate reuses the held-out refit. The constant-ROAS line is fitted for every curve unit, cluster units included. Neither adds a separate fit.
## Alternatives considered
| Alternative | Why not, or why |
|---|---|
| Publish every converged curve | Converged curves can still fail on held-out weeks or be weakly identified |
| Stop at the first failing gate | A weak curve could then skip the stability test, though it can still drive cuts |
| **Run every gate on every curve, set the most severe state, and scope use by total eligibility** | **Chosen** |
| Read every cut off the curve alone | On synthetic data the curve alone understated cut losses in almost every resample |
## Open questions
- **A planned probe of intraday return and out-of-budget serving** may replace f = 0 with a measured value. Where f is below the break-even point, the reading stands. Above it, the reading is scaled or frozen.
- **Rolling-refit stability** failed in one of two synthetic worlds. In world 2 all three group spreads moved beyond their sampling-error bounds, and its refits converged less well than the convergence bar asks. The shard part failed in world 1. The moving data window is a candidate cause, to test against a fixed-window reference.
- Policy starting values and other shared open items are listed in [Response curves and budget simulation §6](../concepts/response-curves-and-budget-simulation.md#6-open-items).
## Evidence
Offline prototype on fictional data, 2026-10-06, local Apple-silicon runs; approved by an independent review ([prototype summary](../../validation/response-curve-prototype.md), Budget cuts). The 90% intervals resample campaigns only, and widen when worlds are resampled too. At flat hours (f = 0), under the shipped constant-ROAS reading:
- **With the simulator's own spend model, which the product uses:** the median cut-loss error was −5.9% [−12.8%, +1.2%], and 31.5% [22.6%, 41.2%] of cuts were understated by more than 20%. The share bar fails at campaign level, and is on the bar when worlds are resampled too.
- **With the generator's true expected spend:** −0.9% [−7.9%, +5.9%] and 18.0% [9.6%, 27.0%], on both bars, neither a pass nor a fail.
- With the curve alone, the share-of-understated-cuts bar failed at f = 0 in 99% to 100% of resamples.
- The break-even late-hour factor is unresolved. Under the late-higher hourly profile with true spend, the share bar breaks even inside the tested grid in 67% of resamples; among those, its 5th to 95th percentile ran from 0.10 to 0.40.
- The lowest volume tier rested on 5 to 7 campaigns, so its freeze is a caution, not a measured failure.
- The middle-tier floor's q was 0.2708 at flat hours, giving a floor of about 1.37 × p50 (arithmetic: 1 ÷ (1 − 0.2708)).
- The volume guard cut the share of capped tier-1 cuts in the default family (16 campaigns) understated by more than 20% from 18.8% to 0%. It raised overstatement elsewhere: the p90 cut-size error went from 0% to +52.6% in one small group. Its 14-day threshold was chosen in-sample, and it wasn't run through the scenario totals.

Who set the in-total cut loss, from the same prototype with the constant-ROAS reading on ([prototype summary](../../validation/response-curve-prototype.md), Simulator):
- in the default worlds, cut-only plans: the curve 79%, the line 21%;
- in the default worlds, mixed plans: the curve 41%, the line 57%;
- in the budget-lever cell where late hours earn less, at a late-hour factor of 0.5, cut-only plans: the line 86%.

Tests of the gates themselves, from the same prototype ([prototype summary](../../validation/response-curve-prototype.md), Response curves). Most failed or fell short of their bars:
- **Gate 8a, fragility:** a fragile campaign was flagged `unstable` in 6 of 10 worlds, against a bar of 80%, with 2.5% false alarms.
- **Peak flag:** `peak_sensitive` fired on 8.6% of 70 campaigns, and under the envelope 14% of cuts still read more than 20% too favourable.
- **Gate 5 on its own:** it false-passed 20% to 28% of linear-truth campaigns, with 28% to 36% power on saturating ones. The proposed bar is a false-pass rate of at most 10%. The ladder reached step 4, the campaign-level test switched off, so under point 4 no curve in those worlds would be offered increases. This is a synthetic result; the starting value stays step 1 until the onboarding re-run.
- **Linear truths:** 0 of 60 linear-truth campaigns could drive increases. That pass rests on gates 6 and 7, not on gate 5.
- **Rolling-refit stability:** passed in world 1. In world 2, κ, s and α all moved beyond their 2-MCSE (Monte Carlo standard error) bounds, s by about twice its bound, and the refits had group effective sample sizes of 117 to 401, against the bar of 400. The shard part failed in world 1.
## Review trigger
The intraday probe's result; a hosted run showing a different share of curves passing; or a lift result that contradicts the constant-ROAS reading of cuts.
