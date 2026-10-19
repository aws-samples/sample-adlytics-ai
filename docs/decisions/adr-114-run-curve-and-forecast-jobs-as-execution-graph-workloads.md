# ADR-114: Run curve and forecast jobs as execution-graph workloads
Date: 2026-10-07
Status: Proposed. Not accepted or implemented. The owner set the requirements in Context on 2026-10-07. The rollback default below, and the rest of the design, were approved on the owner's behalf the same day, under authority the owner delegated; the owner hasn't reviewed the final design yet. It depends on an execution-graph control plane that isn't in the repository yet. **The service-level proposals stay observation-only until the owner accepts them.** The design is summarised in [Response curves and budget simulation](../concepts/response-curves-and-budget-simulation.md).
## Context
A weekly curve refit or a daily forecast is several steps:
- check inputs are ready;
- build inputs;
- fit or forecast per shard or batch;
- gate;
- publish a snapshot.

The first design ran them as a Step Functions Map with its own retries and leases, outside the authority that runs ingestion. On 2026-10-07 the owner asked for something else, under the same control-plane authority as ingestion:
- every run and every node readable with its state and reason;
- on-demand runs with the same protections as scheduled ones;
- latency measurable from landing to publication;
- structured logs everywhere, including inside network-isolated jobs;
- one admission rule for scheduled and on-demand runs;
- shadow runs, if they are operationally efficient;
- rollback, for safety when issues keep recurring.

That control plane is a separate design, not yet in the repository. It isn't approved yet, and its binding review findings apply to its implementation plan. On 2026-10-07 the owner chose its minimal v1 core, so this decision uses only what that core provides.

Two runs can overlap: a newer input release, an on-demand request, a data correction. Without one rule, they race to publish.
## Decision
**Every curve refit, baseline forecast, snapshot, rollback, shadow and tuning run is a chain of control-plane plans. One admission rule decides every request, whoever sends it.**
1. **Plans.** An inputs plan does readiness and the input build, always for the whole platform, and releases an exact input manifest. A compute plan pins that release and runs shard fits ([ADR-112](adr-112-fit-campaign-response-curves-with-a-hierarchical-adstock-and-hill-model.md)) or series batches ([ADR-092](adr-092-forecast-the-chronos-2-baseline-with-known-in-advance-covariates.md)) as managed SageMaker nodes ([ADR-094](adr-094-run-offline-modelling-on-sagemaker-processing-without-a-vpc.md)). It also runs every unit's gates, then a set gate, one check over all of the plan's candidates that refuses any a newer run, correction or rollback has overtaken, then a per-unit apply, which advances each unit's published pointer separately. A snapshot plan fixes the exact unit revisions it will serve (its pins) when its build starts, not when it was requested.
2. **One request route.** A schedule, a finished upstream release, a finished plan and an operator all send the same `RunRequest.v1`. EventBridge Scheduler is only a requester. A routing table sends each (run type, reason) to the inputs stage or the compute stage. Agents can't send requests in v1.
3. **One admission rule (owner, 2026-10-07).** Each request carries an input fingerprint: exact ingestion release revisions, model configuration and scope. It is decided per conflict unit, the smallest piece of work two runs can race on: one shard for curves, one series batch for forecasts.
   - **Join:** the same fingerprint is already running or done, so no new compute.
   - **Replace:** newer inputs arrive before compute starts.
   - **Queue, latest wins:** newer inputs arrive while jobs run. At most one waits.
   - **Replace even mid-compute:** a released data correction, a rollback or an operator cancel. Its jobs are stopped.
   - **No starvation.** A run that has started compute finishes unless its inputs are invalidated.
   - **A receipt for every request,** with outcome, reason and requester.

   Each unit has an admission head, the record of which run is active and which one waits. Before a job is sent, a send permit checks and writes that head, and sends nothing if a newer request has won. So the permit is the single point where admission and dispatch meet.
4. **Monotonic publication.** A generation is one immutable set of a unit's outputs from one run. A unit's pointer advances only to a generation with a newer order key. The order key compares, in turn: the rollback epoch, which a rollback advances so its restored generation outranks the ones it replaces; the data window; the input release; then the admission ordinal, the order in which requests were admitted. A late finisher with older inputs is recorded and gated, then marked `superseded`. A correction, rollback or cancel sets a floor on each unit it reaches, the oldest input that unit may still publish. Floors are checked at the gates and at every apply. Every unit gate runs on any terminal state, so partial failure never strands a plan. A snapshot never pins a half-applied plan.
5. **Two configuration identities.** Model configuration (policy, images and every knob that changes an output) enters fingerprints, order and rollback guards. Execution configuration (polling, retries, timeouts, sizing, retention) is recorded but never does.
6. **Throughput.**
   - Shards fit in deterministic parallel lanes, each with a fresh compile cache.
   - Chronos-2 runs as independent parallel series batches.
   - Sharing pools and overlap clusters are never split across shards ([ADR-093](adr-093-treat-overlap-clusters-and-sharing-pools-as-joint-units.md)).
   - Capacity pools follow the account's Processing quota.
7. **Shadow runs.**
   - They fit and gate a candidate configuration against the live generation's input release.
   - They run on idle capacity only, one job at a time.
   - They never publish or take the snapshot lease, the one-at-a-time right to publish a platform's simulator snapshot, and their artifacts expire.
   - Reproduce mode re-runs a past generation exactly.
8. **Rollback.**
   - Only a human operator with `operations-forecast-rollback` can start one.
   - It is a forward publication of an earlier immutable generation, restoring what a named snapshot served, all or nothing.
   - It writes a config or data guard that only a declared fix, a released correction or a human clear lifts.
9. **Rollback below a correction: default no.** A rollback that would serve data a later correction replaced is refused (`refused(rollback_below_floor)`), with nothing committed. The exception is when the operator names that correction as bad data in a data-mode rollback. For a model defect that predates a correction, the path is to activate the previous model configuration as a new one and refit on corrected data.
10. **Observability.**
    - Jobs log only through the project's reporting seam.
    - Each execution writes a timing manifest; spans are synthesized from it and the provider job record, so tracing works under network isolation.
    - Every clock in the latency chain names its authority, the system whose record gives that time. A time no authority can give is recorded as a closed gap, a named reason the time is missing, never as zero.
    - Lineage runs both ways: from a served figure back to the source deliveries it used, and from a source correction forward to every output it touched.
    - One alarm destination, the existing ingestion alarm topic or a sibling.
11. **Entitlements, behind the existing Cognito authorizer:** `operations-forecast-request`, `operations-forecast-rollback` (human only) and `operations-forecast-diagnostics-read`. No route is unauthenticated.
12. **No cost guards in v1** (owner, 2026-10-07). Route throttling, a security control, still applies.
## Consequences
- This replaces the forecasting architecture's Scheduler, Step Functions and `CreateProcessingJob.sync` flow, and its forecast-run lease.
- Forecasting supplies declarations to the control plane: workload templates, which describe each plan's steps, and executor profiles, which describe how each managed job is launched (image, instance and network isolation). It doesn't own the control plane's handling of them.
- These control-plane gaps are **activation prerequisites:**
  - the forecasting parts of the run-request rule;
  - a capacity pool for forecasting's Lambda nodes;
  - a provider timeout must reach `FAILED`;
  - a retrying unit keeps its lock;
  - a stop request that arrives before the job exists.

  Other gaps have stated fallbacks.
- Rollback is a late delivery slice, after the core run path is live-validated. Its remaining review findings must be fixed and tested before it is enabled, the rollback floor check first.
- Throughput, start-up and billed hours per job kind are measured by the hosted throughput benchmark before any figure is quoted.
## Alternatives considered
| Alternative | Why not, or why |
|---|---|
| Step Functions per run, with its own retries and lease | A second workflow authority whose retries and timeouts can disagree with the graph's, and cancellation outside it |
| A forecasting-only scheduler | Duplicates admission, capacity and status the control plane already has |
| **Chains of control-plane plans, admitted by one rule for every requester** | **Chosen** |
## Open questions
- **Service-level proposals** (one row for each capacity-pool size, since the number of concurrent job slots depends on the account's quota; labelled per-shard or per-run) stay observation-only until the measured refit day. The owner accepts values after that.
- **Weaker controls accepted on the owner's behalf for the dev build only (2026-10-07),** to be re-decided by the owner before activation and not accepted for production:
  - one execution role for live and shadow profiles, so live and shadow outputs are separated by fixed prefixes rather than IAM;
  - network isolation checked in code, not IAM ([ADR-094](adr-094-run-offline-modelling-on-sagemaker-processing-without-a-vpc.md)).
- The control-plane design isn't approved and may still change. If it does, this decision is re-checked against the new revision.
## Evidence
- The control-plane design chooses direct managed-job submission and rejects a second workflow authority. It isn't approved yet, and its binding findings apply to its implementation plan [unverified in this repository: read 2026-10-07 from the control-plane design's local working files, which aren't committed].
- `CreateProcessingJob` supports the Step Functions `.sync` pattern [verified 2026-10-06, Step Functions developer guide]. That pattern was considered and rejected above.
- The `ml.c5.2xlarge for processing job usage` quota is 8 in the dev account and 0 by AWS default [verified 2026-10-07, Service Quotas API, us-east-1].
- The job-execution part of this design isn't fully approved. Its remaining findings, most of them in the rollback mechanism, are binding plan items with named tests [unverified in this repository: the review records are local working files, read 2026-10-07].
## Review trigger
A control-plane revision that changes a consumed element; the hosted throughput benchmark; or the owner's decision on the service-level proposals or the dev-only controls.
