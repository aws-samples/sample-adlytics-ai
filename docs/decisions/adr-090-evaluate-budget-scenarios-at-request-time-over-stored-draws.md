# ADR-090: Evaluate budget scenarios at request time over stored draws
Date: 2026-10-07
Status: Proposed. Not accepted or implemented. The owner chose per-channel totals on 2026-10-07, in the UI design review, which also raised the two drawing and wording findings below. The rest of the design was approved on the owner's behalf on 2026-10-07, under authority the owner delegated; the owner hasn't reviewed the final design yet. It would amend [ADR-001](adr-001-chronos-deployment.md)'s review trigger on arbitrary scenario inputs and the forecasting low-level design's exclusion of request-time scenarios; both stay in force until this is accepted. The design is summarised in [Response curves and budget simulation](../concepts/response-curves-and-budget-simulation.md).
## Context
A brand manager proposes new budgets and expects an answer in seconds. Fitting a curve takes minutes and runs offline (ADR-001, [ADR-094](adr-094-run-offline-modelling-on-sagemaker-processing-without-a-vpc.md)). Scenario inputs are arbitrary, so they can't all be baked ahead the way page presets are ([ADR-070](adr-070-serve-declared-preset-windows-from-publish-time-artifacts.md)).
Curves for different channels are fitted separately, and their attributed revenue isn't additive across channels in any way this design can defend. The UI design review of 2026-10-07 also found two reading problems:
- the page didn't say how each curve state is drawn;
- one sketch read as both "cuts are modelled" and "nothing is modelled".

This ADR records two linked choices: request-time arithmetic, and per-channel totals. They are accepted or reversed together, because the totalling rule is part of the arithmetic the route performs.
## Decision
**The scenario route computes each answer by domain arithmetic over stored posterior draws and one published revision triple. It never fits, never calls a model or an ad platform, and never stores a scenario.**
1. **The revision triple.** The snapshot publisher bakes a scenario-input snapshot, a list artifact in ordered segments ([ADR-071](adr-071-reach-the-full-campaign-estate-through-baked-ordered-segments.md)) and per-campaign detail artifacts, and advances one pointer. The route reads only pointer-selected artifacts.
2. **One scenario, step by step.** Each proposed budget goes through the five steps in [Response curves and budget simulation](../concepts/response-curves-and-budget-simulation.md) §3.5, from expected spend to the Chronos-2 baseline of [ADR-092](adr-092-forecast-the-chronos-2-baseline-with-known-in-advance-covariates.md). Campaigns that can't be modelled are kept at today's spend and listed with a reason.
3. **Per-channel totals.**
   - Attributed and modelled outcomes (revenue change, modelled ROAS) are totalled only per channel, or per cross-channel cluster ([ADR-093](adr-093-treat-overlap-clusters-and-sharing-pools-as-joint-units.md)). There is no plan-wide revenue change and no plan-wide modelled ROAS.
   - Spend and net budget changes may be totalled across channels.
   - A cross-channel net effect is a labelled later proposal, not v1.
4. **Intervals.** Draws are joint only within one fit. A total over separate fits shows the wider of the independent and comonotone bands, with a label saying so. Any gain beyond observed spend sits outside the total, on its own line.
5. **Curve state and scenario outcome are separate.**
   - The *Curve* column says what the curve supports: "fit", "pooled", "weak · cuts only" or "held".
   - The *This change* column starts with "Modelled:" or "Not modelled:" and one reason.
   - "Kept at today's spend" is the one name for a frozen row.
6. **Each curve state is drawn only where it may be read.**
   - Every state shows the observed spend range and markers for current and proposed spend.
   - A fitted curve shows its band up to the highest observed spend, then a dashed median with no band.
   - A weak curve is drawn only up to current spend, with "Not modelled for increases".
   - A held curve draws nothing, with the reason.
7. **Modelled ROAS** sums revenue and spend over one row set, on the fee-inclusive spend basis of [ADR-017](adr-017-roas-definition.md), and lists the rows it leaves out. The figures are declared in the metric dictionary by [ADR-096](adr-096-declare-internal-and-modelled-metrics-in-the-metric-dictionary.md), not here.
8. **Selection and caching.** Campaigns are named only by server-minted capabilities, redeemed before any cache read. The in-memory cache key holds the triple, the owner and the redeemed request. There is no durable scenario store.
9. **Declared page and contracts.** The `/simulator` page and its versioned request and result contracts are declared in `pages.yaml`, through two page-schema extensions: a `forecast_horizon_policy` and page-level `request_contracts`. The route runs no measurement query, so it isn't a live-measurement path under ADR-070. The simulator is a separate publication, outside ADR-075's page dependency contract.
10. **`numpy` in its own layer.** The draw arithmetic needs `numpy`. It ships in a dedicated layer attached only to the simulator presentation function and the two publishers, never the shared layer. This is a named exception to `pyproject.toml`'s rule against heavy dependencies in interactive functions, and is cited against [ADR-005](adr-005-compute-runtime-baseline.md). It is to be accepted once its import cost and the dev cold start are measured.
## Consequences
- The proposed latency budget is a 3 s p95 for up to 200 changed campaigns, to be measured in dev against a 5,000-campaign synthetic artifact set.
- Until the owner's legal read of the Ads API terms, capped campaigns can't model increases. Uncapped campaigns gain little from extra budget, because it adds only the small expected spend from the upper tail of the spend forecast. And weakly identified curves drive cuts only ([ADR-113](adr-113-publish-a-response-curve-only-after-held-out-identification-and-fragility-gates.md)). So most v1 plans show mainly the cost of cuts, and the page says so. In the prototype, 0% of reallocations had any in-total gain in two world sets and 2.8% in a third ([prototype summary](../../validation/response-curve-prototype.md), Simulator).
- Nothing on the page, in the result or in the artifacts adds attributed revenue across channels.
## Alternatives considered
| Alternative | Why not, or why |
|---|---|
| Fit or sample at request time | Minutes per request, and a model call on a user path |
| Pre-compute a grid of scenarios | The space of budget vectors is too large, and a grid hides interpolation |
| **Pure arithmetic over published draws and a published snapshot** | **Chosen** |
| One plan-wide revenue total across channels | Refused by the owner (2026-10-07) |
| **Per-channel revenue and ROAS totals; spend totalled across channels** | **Chosen** (owner, 2026-10-07) |
## Open questions
- **DSP budget scenarios** wait on the owner, after a planned check of DSP spend against budget ([Response curves and budget simulation §6](../concepts/response-curves-and-budget-simulation.md#6-open-items)). Until then, DSP rows show curves only and are never in a total.
- **Scenario saving and agent-run scenarios** are not in v1.
## Evidence
- `pyproject.toml` says heavy analytics dependencies go in a layer or image, never in an interactive function [verified 2026-10-06, repository file `pyproject.toml`, lines 57 to 68]. Every Lambda reads one shared dependencies layer today [verified 2026-10-06, `infra/lib/stacks/publisher-stack.ts` and `infra/lib/config/index.ts`].
- `numpy` isn't a runtime dependency of the repository today [verified 2026-10-06, `pyproject.toml`].
- The owner's per-channel decision and the two UI findings are dated 2026-10-07, from the UI design review.
- Offline prototype on fictional data, 2026-10-06 ([prototype summary](../../validation/response-curve-prototype.md), Simulator): the safety rules held over 10,000 random scenarios. Totals over several campaigns covered the truth in 50.0% of 44 totals, against a tolerance of 67.9% to 92.1%, and overstated losses by a median 24%; the central estimate, not how bands were combined, was the larger cause.
## Review trigger
The 3 s budget missed in dev; a need for durable or shared scenarios; or a defensible rule for combining separately fitted channel curves.
