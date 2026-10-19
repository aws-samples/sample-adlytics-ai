# ADR-093: Treat overlap clusters and sharing pools as joint units in reallocation totals
Date: 2026-10-07
Status: Proposed. Not accepted or implemented. The response-curve and forecasting design was approved on the owner's behalf on 2026-10-07, under authority the owner delegated; the owner hasn't reviewed the final design yet. The design is summarised in [Response curves and budget simulation](../concepts/response-curves-and-budget-simulation.md).
## Context
Two kinds of campaign group break the "add up each campaign's change" rule of [ADR-090](adr-090-evaluate-budget-scenarios-at-request-time-over-stored-draws.md):
- **Overlap clusters.** Campaigns that advertise the same products compete for the same shoppers. AMC may credit one conversion to several of them, so moving budget between them can't be read off separate curves.
- **Sharing pools.** A portfolio can share unspent budget between its campaigns, so one campaign's budget changes what its neighbours can spend.

Compatibility across platforms and currencies is already decided ([ADR-052](adr-052-separate-ecosystem-platform-channel-and-ad-product.md), [ADR-048](adr-048-make-metric-compatibility-and-axis-aware-aggregation-runtime-semantics.md)). This ADR decides only the joint-unit rules.
## Decision
**A cluster or a sharing pool is one unit in a reallocation total.**
1. **Cluster-total changes** use a cluster curve: the same model fitted with the cluster as the unit, combined spend as x and combined outcome as y.
2. **Moves within a cluster** get no summed figure. The page says why.
3. **Clusters** are built from configured-product overlap, and capped at a policy size that fits one fit shard. A cluster that can't be split under that cap is held as `held(cluster_too_large)`.
4. **Sharing pools** are evaluated jointly. The pool's sharing allowance comes from the versioned budget-rule functions of [ADR-088](adr-088-model-budget-pacing-and-pool-state-as-platform-neutral-snapshots.md).
5. **Pools and clusters are never split** across fit shards ([ADR-114](adr-114-run-curve-and-forecast-jobs-as-execution-graph-workloads.md)).
6. **A cluster may span channels.** Its total is then its own group under ADR-090's per-channel rule, never added to a channel's total.
## Consequences
- One more fit per shard that holds a cluster. It is counted in the cost projection.
- Cluster curves carry `combined_outcome_may_double_count` until AMC's multi-campaign credit rule is read.
- On synthetic data neither joint reading was accurate yet (Evidence). Cluster curves were biased and their bands held the pool's true change too rarely, and the sharing allowance shrank changes less often than the truth did. Cluster units take the same gates and the same constant-ROAS cut reading as every other curve ([ADR-113](adr-113-publish-a-response-curve-only-after-held-out-identification-and-fragility-gates.md)), so these errors are handled the same way as single-campaign curve errors. They aren't fixed by this decision.
## Alternatives considered
| Alternative | Why not, or why |
|---|---|
| Sum separate campaign curves | Double counts shared credit, and ignores pool sharing |
| Refuse any scenario that touches a cluster or pool | Loses the common case of moving budget out of a group |
| **Evaluate clusters and pools as joint units** | **Chosen** |
## Open questions
- **Keyword and target overlap** needs a source not yet found; a planned probe looks for one. Until then, clusters use product (ASIN) overlap only.
- AMC's multi-campaign credit rule, which is [unverified], and sharing under the 25% average-daily-budget opt-out are listed in [Response curves and budget simulation §6](../concepts/response-curves-and-budget-simulation.md#6-open-items).
## Evidence
- Portfolios can share leftover budget between their campaigns [verified 2026-10-05, Amazon Ads portfolio-sharing guide].
- Whether AMC credits one conversion to several overlapping campaigns is [unverified]; no AMC page read for this design states the rule.
- Offline prototype on fictional data, local Apple-silicon runs, 2026-10-06 ([prototype summary](../../validation/response-curve-prototype.md), Simulator). These are measurements on fictional data with small samples, not on customer data:
  - **Moves within a cluster (Decision 2): PASS.** A move between campaigns in one cluster was flagged and summed nothing.
  - **Cluster-curve scoring (Decision 1): FAIL.** 4 cluster units per world, in worlds 1, 3 and 5, with the true expected spend as input to isolate the curve. Under single credit, the p50's error against the pool's true change, and how often the 80% band held it, were: a 0.7× budget −32% (10 units, 40% covered); a pause −1.1% (12 units, 58%); a 1.3× budget +29% (8 units, 25%). Under double credit, in worlds 3 and 5 only (world 1's fit had a stuck chain and was left out), the errors against the pool were −65%, −35% and +64%.
  - **Sharing pools (Decision 4): FAIL.** Sharing shrank the change in 41.5% of worlds in the simulator, against 65.9% in the truth.
## Review trigger
AMC's credit rule is confirmed; a keyword-overlap source appears; or clusters routinely exceed the shard cap.
