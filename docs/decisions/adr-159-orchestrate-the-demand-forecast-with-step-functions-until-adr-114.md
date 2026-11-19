# ADR-159: Orchestrate the demand forecast with Step Functions until ADR-114

Date: 2026-11-11
Status: Accepted (2026-11-11), approval DR-8. Built in dev by slices 1 and 2 of the demand
forecast; not yet deployed outside dev or live-accepted. Relates to
[ADR-114](adr-114-run-curve-and-forecast-jobs-as-execution-graph-workloads.md), which stays
Proposed.

## Context

A forecast run checks its input, builds it, runs one SageMaker Processing job per shard, gates and
publishes. ADR-114 would run such jobs under the execution-graph control plane, but it is not
accepted or deployable outside dev. Waiting for it would block the solution indefinitely.

## Decision

1. **One Standard Step Functions state machine runs a forecast** (`ForecastStateMachine` in
   `infra/lib/stacks/demand-forecast-stack.ts`): select input, build it, a `Forecast` map of
   Processing jobs, a `Publish` map, then commit.
2. **A daily schedule checks readiness.** An EventBridge Scheduler schedule starts the machine
   each day. A check with no new complete input records a no-op receipt and changes nothing.
3. **Operator runs use the same machine.** `make demand-forecast-run` starts it with an operator
   reason and the same protections as a scheduled run.
4. **Migrate when ADR-114 is accepted** and deployable outside dev. The domain and the store do not
   depend on Step Functions, so only the orchestration changes.

## Consequences

- Each run and each shard is visible in the Step Functions console and in the run receipts.
- A later migration to the execution-graph control plane is expected work.
- Shadow runs from the owner's 2026-10-07 run requirements are deferred until that migration.

## Alternatives

- Wait for ADR-114: blocks the solution with no date.
- Run every step in one Lambda: no per-shard retry, and the 15-minute limit is shorter than a run.
