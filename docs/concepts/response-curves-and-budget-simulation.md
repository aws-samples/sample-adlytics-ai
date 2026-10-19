# Concept — Response Curves and Budget Simulation

| | |
|---|---|
| **Level** | L2 — crosscutting concept |
| **Status** | Proposed. Not accepted or implemented. The design was approved on the owner's behalf on 2026-10-07, under authority the owner delegated; the owner hasn't reviewed the final design yet. The ADRs in §5 are Proposed. |
| **Last updated** | 2026-10-07 |
| **Owns** | How campaign response curves, the baseline forecast and the read-only budget simulator fit together: what each needs, how each is checked, how they combine, and what every label means. |
| **Applied by** | [Saturation](../capabilities/saturation/README.md) · [Forecasting](../capabilities/forecasting/README.md) · [Source Acquisition](../capabilities/source-acquisition/README.md) · [Load Path](../capabilities/load-path/README.md) · [Data Layer](../capabilities/data-layer/README.md) · [Operational Data-Flow Status](../capabilities/operational-data-flow-status/README.md) |
| **Conventions** | [Documentation conventions](../conventions.md) |

## 1. What this concept is

A brand manager asks: "What happens to my attributed sales if I move budget between campaigns?" Three parts answer it, built in this order:

| Part | Question it answers | Runs |
|---|---|---|
| Response curve, one per campaign | What changes if spend changes? | Weekly, offline |
| Baseline forecast, one per series | What happens over the next 90 days if nothing changes? | Daily, offline |
| Budget simulator | What does this set of proposed budgets do? | On request, arithmetic only |

This concept owns how the three fit together and what their labels mean.

**Lives elsewhere:**
- why each choice was made, in the ADRs of §5;
- AMC and Vendor Central facts, in the [source references](../sources/);
- Amazon Ads API facts, until an Amazon Ads source reference ships with the first Ads API contract, each in one ADR's Evidence: Change history and budget fields in ADR-088; report formats and the accounts list in ADR-089; the DSP forecast API and OAuth scopes in ADR-091; portfolio sharing in ADR-093; permissions in ADR-097; budget averaging and daily overspend in ADR-112;
- table and column shapes, in the [canonical model](../canonical-model.md) once the contracts ship;
- job orchestration and the capability topology, in the Saturation and Forecasting packages once built (proposed in ADR-094 and ADR-114);
- the prototype's main results and figures, in the [prototype summary](../../validation/response-curve-prototype.md).

## 2. The rule

> **The simulator never writes to an ad platform, never fits or calls a model, and never shows a figure its curve can't support. Every figure it shows is labelled attributed, not incremental.**

The invariants that keep it true:
- **Read-only.** No component writes to Amazon or any other platform. No design proposes an automatic budget change.
- **Delivered spend, not budget.** Curves learn from what was spent. Budgets matter only to turn a proposed budget into expected spend, and to flag days a campaign ran out.
- **Unknown is never zero.** A suppressed or unavailable value is empty. A paused campaign's spend is a true zero.
- **No figure past its support.** An increase is never read past the highest spend the campaign has shown. A cut below the lowest, or from above the highest, observed spend is read cautiously. Inside the observed range a cut's loss can still read too small; the prototype measured how often (§3.8).
- **Per channel.** Attributed revenue and modelled ROAS are totalled per channel, never across channels. Spend may be totalled.
- **Gated or not at all.** A curve or forecast that fails its checks is held or labelled, never shown silently.

## 3. How it works

**Container diagram — read-only path from Amazon sources to the budget simulator; data-flow viewpoint, container depth.**

![Container diagram: read-only path from Amazon sources to the budget simulator. First-party connectors check every call against an operation allowlist, receive read-only API responses from Amazon and hand results to the Finalizer and load path, which copies the bytes unchanged into the raw zone and loads curated tables. Control-plane run plans release exact inputs, submit network-isolated SageMaker Processing jobs for curve fits and Chronos-2, gate the results and publish artifacts. The simulator API answers a brand manager's scenario from published artifacts only and has no path back to any ad platform.](diagrams/response-simulation-flow.svg)

> **Takeaway.** Data reaches the simulator only through read-only connectors and gated, published artifacts, and nothing on that path writes back to an ad platform.

### 3.1 Response curves

For one campaign, plot weekly spend against the attributed revenue that followed. Early dollars buy a lot; later dollars buy less, as the campaign reaches the shoppers it can reach. The curve is a smooth line through that pattern, with a band showing how sure the fit is. Its slope at today's spend is the **marginal ROAS**: what the next dollar is expected to return.

The model has three parts:
- **adstock**, which carries an ad's effect into later weeks;
- **Hill saturation**, which gives the diminishing returns;
- **a hierarchy**, campaign → channel → channel group, so a campaign with little spend variation leans on similar ones and is labelled `pooled_only`.

Season, promotions and peaks scale the ad's response. The curve passes through zero spend.

### 3.2 The baseline forecast

Chronos-2 is a pretrained time-series model. For each campaign it reads the last 180 to 365 days and forecasts 90 days ahead, as a grid of quantiles with a daily mean. It forecasts three things:
- attributed revenue;
- attributed purchases;
- for sponsored campaigns, the spend the current budget would produce.

It is given only facts known on the forecast date: day of week, holidays, announced peak events, and promotions already scheduled. It is never told future spend, so its job is "if nothing changes".

### 3.3 What data they need, and in what format

| Need | Why | Comes from |
|---|---|---|
| Delivered spend per campaign-week | The curve's x | AMC, already contracted |
| Attributed revenue per campaign-week, 14-day window | The curve's y and a forecast target | AMC, already contracted |
| Budget, pacing, portfolio and capping state | Turn a budget into expected spend; flag days that ran out | Amazon Ads API, a new source |
| Change history | Count budget and bid changes that weren't reactions to performance | Amazon Ads API |
| Campaign → advertised-product links | Overlap clusters and the stock mask, which leaves out campaign-days when too few of the advertised products could be bought | Amazon Ads API exports |
| Start-of-day stock, promotions and coupons | Controls and known-in-advance covariates | Vendor Central over SP-API |
| Peak-event calendar | Seasonality and covariates | Operator input |

**Format rules:**
- One row per campaign per week for curves, Monday to Sunday on the AMC day. One regular daily series for forecasts.
- Every value carries its basis, currency, timezone and source.
- Budgets are snapshots, valid from when they were seen, never summed over time ([ADR-088](../decisions/adr-088-model-budget-pacing-and-pool-state-as-platform-neutral-snapshots.md)).
- Every source arrives the same way: a versioned contract, a landing in the raw zone, and event-driven loading ([ADR-089](../decisions/adr-089-onboard-api-sources-through-governed-operation-packs-and-resolve-campaigns-at-read-time.md)).
- Ads API rows are matched to AMC campaigns when they're read, not when they're loaded.

### 3.4 How they are tuned and validated

Tuning is a small set of versioned settings, never hand edits per customer: adstock length, prior widths, pooling levels, held-out window and publication thresholds. The offline prototype set their starting values on synthetic data. A statistical review approves them before activation.

Every gate runs on every curve. The gates check that the curve can:
- **converge**;
- **beat two benchmarks on held-out recent weeks**: a naive forecast, and a straight line (constant ROAS);
- **show identifying evidence**: budget or bid changes that weren't reactions to performance;
- **stay stable** when its last weeks are removed.

A curve is then used only as far as its gates allow (§3.6):
- a curve that is held, lacks spend variation or is unstable drives nothing;
- a curve with thin identifying evidence, one whose held-out test passed only on its channel's pooled weeks, or one in a channel split across fit shards whose shards disagree or can't all be compared (shard-sensitive), still drives cuts, but not increases;
- a `fitted` or `pooled_only` curve may drive increases and cuts within support, while the campaign-level held-out test is on.

That test starts on. If, at onboarding, the held-out benchmark gate still passes too many straight-line campaigns at its strictest setting, the test is switched off. Every curve then takes the channel-pooled path, and no curve drives increases until a later policy version passes that check. On synthetic data the prototype reached that last step ([ADR-113](../decisions/adr-113-publish-a-response-curve-only-after-held-out-identification-and-fragility-gates.md)).

A forecast publishes only if, at each horizon, its backtested quantile loss is no worse than two simple baselines by more than a policy margin. The baselines are SeasonalNaive, which repeats the value from the same point one season back, and ETS, exponential smoothing of the error, trend and seasonality. Backtests are anchored weekly and cached. So each day runs the live forecast, plus any cached backtest origins that a data restatement reaches.

### 3.5 How the simulator combines them

For each proposed budget, the simulator:
1. turns the budget into expected daily spend with the platform's budget, pacing and portfolio rules;
2. converts that to the curve's spend basis;
3. reads the change in attributed revenue off the curve, once per posterior draw, within support;
4. spreads it over the horizon through the carryover, scaled by season;
5. adds it to the baseline.

Totals and intervals come from the draws, per channel. Campaigns it can't model are **kept at today's spend** and listed with a reason. A gain beyond observed spend sits on its own line, outside the total. Overlap clusters and sharing pools are joint units. Every sponsored cut is also read on the straight line, and the larger loss is used. Which of the two sets the loss varies. In the prototype's default worlds the curve set most of the loss in cut-only plans, and the line set most of it in mixed plans. In one budget-lever test cell, where late hours earn less, the line set most of the cut-only loss ([prototype summary](../../validation/response-curve-prototype.md), Simulator).

### 3.6 What the labels mean

| Label | Meaning |
|---|---|
| **Attributed, not incremental** | Every figure is Amazon's 14-day attribution of conversions to ads. It's a credit rule, not a measured causal effect. It answers "how does attributed revenue move with spend", not "what would I lose if I stopped". |
| **fit** (`fitted`) | The curve passed every gate on its own evidence. It may drive increases and cuts within support. |
| **pooled** (`pooled_only`) | The curve leans on similar campaigns in its channel. It may drive increases and cuts within support, like a fitted curve. |
| **weak · cuts only** (`weakly_identified`) | The curve converged, but its evidence for a shape is thin. It drives cuts only, and is drawn only up to current spend, with "Not modelled for increases". |
| **held** | No curve this week, with the reason. The campaign is kept at today's spend. |
| **Beyond observed spend** (extrapolated) | Past the highest spend the campaign has shown, the shape comes mostly from the model's assumptions. It is drawn dashed with no band and never enters a total. |
| **Modelled: / Not modelled:** | Whether this row's change is in the plan, kept apart from the curve's own state. |
| **Range widened** | The band covers several fits, or testing showed this kind of cut tended to be understated. |
| **Amazon DSP forecast · not in total** | Amazon's own model, shown beside ours for context only, once the terms allow. |

### 3.7 The read-only boundary

Only two controls can stop a write to Amazon:
- the customer's Viewer grant;
- each connector's pre-send operation allowlist.

The OAuth scope isn't one of them, because the Ads API has no read-only scope ([ADR-091](../decisions/adr-091-show-amazon-dsp-forecasts-side-by-side-and-gate-amazon-estimates-on-a-terms-record.md)). Relying on these two controls is a proposed default, adopted under the delegated approval and still open for the owner. Its sign-off for each account type (vendor, seller, DSP advertiser) also waits on a probe of the Viewer grant ([ADR-097](../decisions/adr-097-ship-one-first-party-connector-per-source-as-its-own-stack.md)).

The Finalizer checks the same allowlist after landing, but can only refuse a landing. Connectors are their own stacks, each with its own landing role, and `src/` never imports one. The model jobs run with network isolation and no VPC. The simulator has no outbound port to any ad platform.

### 3.8 What the prototype showed, and what's still pending

An offline prototype ran on generated, fictional data on 2026-10-06, on one Apple-silicon laptop. An independent review approved the results. The [prototype summary](../../validation/response-curve-prototype.md) holds the main criteria and their figures.

**It showed:**
- no AWS or Amazon call is made;
- the simulator's safety rules hold: losses stay in totals, increases never pass observed spend, display-only curves stay out of totals;
- forecast backtests don't see the future;
- the chosen host is cheaper in the projection.

**It failed** (the summary gives each result):
- convergence, in most worlds;
- pooling's benefit for thin campaigns, which wasn't shown;
- curve accuracy: marginal ROAS read high and bands held the truth too rarely, worst when hidden demand moves spend and revenue together;
- getting saturating curves to a usable state, and giving campaigns their intended label;
- flagging fragile campaigns often enough;
- flagging peak-sensitive campaigns, so some cuts still read too favourable;
- curve accuracy when capping data is missing, with or without the hourly heuristic;
- the held-out benchmark gate on its own, which passed too many straight-line campaigns;
- rolling-refit stability: the rolling part in world 2 and the shard part in world 1;
- coverage of totals over several campaigns;
- cluster curves, which were biased and whose bands held the pool's true change too rarely;
- sharing pools, where the simulator shrank the change less often than the truth did;
- the forecast's strict output shape, because some quantiles crossed;
- the forecast level, which lags a season the model hasn't seen twice.

**It didn't settle:**
- whether cuts read on the straight line are close enough at flat hours, which sits on the bar with true spend and fails at campaign level with the simulator's spend model;
- the late-hour factor, so f = 0 is an interim assumption;
- whether runs repeat beyond one cold-fit pair on macOS.

**Still pending:**
- the hosted run-time and throughput tests;
- the live contract and load path;
- every Amazon API probe;
- the owner's legal read of the Ads API terms.

## 4. Rejected alternatives

| Alternative | Decision |
|---|---|
| Fit curves on budget instead of spend | [ADR-112](../decisions/adr-112-fit-campaign-response-curves-with-a-hierarchical-adstock-and-hill-model.md) |
| Read every cut off the curve alone | [ADR-113](../decisions/adr-113-publish-a-response-curve-only-after-held-out-identification-and-fragility-gates.md) |
| Sum attributed revenue across channels | [ADR-090](../decisions/adr-090-evaluate-budget-scenarios-at-request-time-over-stored-draws.md) |
| Blend Amazon's DSP curve into totals | [ADR-091](../decisions/adr-091-show-amazon-dsp-forecasts-side-by-side-and-gate-amazon-estimates-on-a-terms-record.md) |
| Hold Ads API rows until AMC knows the campaign | [ADR-089](../decisions/adr-089-onboard-api-sources-through-governed-operation-packs-and-resolve-campaigns-at-read-time.md) |
| AWS Batch on Fargate in a new VPC | [ADR-094](../decisions/adr-094-run-offline-modelling-on-sagemaker-processing-without-a-vpc.md) |
| A Step Functions workflow per run | [ADR-114](../decisions/adr-114-run-curve-and-forecast-jobs-as-execution-graph-workloads.md) |

## 5. Decisions

| Decision | Owner |
|---|---|
| Budget, pacing and pool state as platform-neutral snapshots | [ADR-088](../decisions/adr-088-model-budget-pacing-and-pool-state-as-platform-neutral-snapshots.md) |
| Governed operation packs; campaigns resolved at read time | [ADR-089](../decisions/adr-089-onboard-api-sources-through-governed-operation-packs-and-resolve-campaigns-at-read-time.md) |
| Scenarios at request time over stored draws; per-channel totals | [ADR-090](../decisions/adr-090-evaluate-budget-scenarios-at-request-time-over-stored-draws.md) |
| Amazon DSP forecast side by side; terms record | [ADR-091](../decisions/adr-091-show-amazon-dsp-forecasts-side-by-side-and-gate-amazon-estimates-on-a-terms-record.md) |
| Chronos-2 baseline with known-in-advance covariates | [ADR-092](../decisions/adr-092-forecast-the-chronos-2-baseline-with-known-in-advance-covariates.md) |
| Overlap clusters and sharing pools as joint units | [ADR-093](../decisions/adr-093-treat-overlap-clusters-and-sharing-pools-as-joint-units.md) |
| SageMaker Processing, x86_64, no VPC | [ADR-094](../decisions/adr-094-run-offline-modelling-on-sagemaker-processing-without-a-vpc.md) |
| Expected producer deliveries | [ADR-095](../decisions/adr-095-expect-producer-deliveries-on-a-declared-schedule.md) |
| Internal and modelled metrics | [ADR-096](../decisions/adr-096-declare-internal-and-modelled-metrics-in-the-metric-dictionary.md) |
| One first-party connector per source | [ADR-097](../decisions/adr-097-ship-one-first-party-connector-per-source-as-its-own-stack.md) |
| Same-account connectors, one landing role each | [ADR-098](../decisions/adr-098-allow-same-account-connectors-with-one-landing-role-per-producer.md) |
| Hierarchical adstock-and-Hill curves | [ADR-112](../decisions/adr-112-fit-campaign-response-curves-with-a-hierarchical-adstock-and-hill-model.md) |
| Curve gates and total eligibility | [ADR-113](../decisions/adr-113-publish-a-response-curve-only-after-held-out-identification-and-fragility-gates.md) |
| Curve and forecast runs as execution-graph workloads | [ADR-114](../decisions/adr-114-run-curve-and-forecast-jobs-as-execution-graph-workloads.md) |

## 6. Open items

| Item | What resolves it | Blocks |
|---|---|---|
| Legal read of the Amazon Ads API terms (what each answer changes: [ADR-091](../decisions/adr-091-show-amazon-dsp-forecasts-side-by-side-and-gate-amazon-estimates-on-a-terms-record.md)) | The owner's own read | Storing and showing any Amazon estimate |
| DSP budget scenarios | The DSP spend-against-budget check, then the owner | DSP rows in totals |
| Service-level values | The measured refit day, then the owner | Evaluated service levels |
| Dev-only weaker controls (one execution role; network isolation checked in code; detail in [ADR-114](../decisions/adr-114-run-curve-and-forecast-jobs-as-execution-graph-workloads.md)) | The owner's re-decision | Activation beyond dev |
| AMC's multi-campaign credit rule | Reading the rule; if single credit is confirmed, the flag is removed | Removing `combined_outcome_may_double_count` from cluster curves |
| Sharing under the 25% average-daily-budget opt-out | Validation of the sharing allowance | Sharing for opted-out campaigns, which stays a flagged non-sharing allowance |
| Hosted run-time and throughput tests | A dev deploy | Quoting any cost or run time |
| Late-hour factor (interim f = 0) | A probe of intraday return and out-of-budget serving | Relaxing the straight-line cut reading |
| Whether Ads API DSP campaign and advertiser IDs equal AMC's | Recorded fixtures | DSP Ads API rows resolving |
| Seasonal input for the forecast level | A prototype run | Plan ROAS while the level test fails |
| Policy starting values: priors, adstock length and pooling levels, and gate thresholds | Statistical review, with the owner | Activation |
