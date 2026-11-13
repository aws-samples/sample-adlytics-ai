# ADR-162: Bound each graph-worker pass by its invocation's remaining time

Date: 2026-11-13
Status: Proposed (2026-11-13). The owner chose option 1 of the graph-stall ruling: bound each
promoter and release pass by the remaining Lambda time, with no `infra/` change.

## Context

The promoter and the release are scheduled Lambdas that run every minute with a 60 s timeout
(`bake_tasks.worker_pass_seconds`). Before this change neither read its remaining time, except
the promoter's serving election (`next_election_min_remaining_ms`).

On dev on 2026-11-13 the D2 re-seed left 3,455 open runs at once
(`.agents/tasks/demand-forecast/build-s1/evidence/graph-stall/rca.md`):

- `arm_pass` read the run and nodes of every units `ARM#` item, then made its only commit,
  `arm_unit_groups`. The reads took more than 60 s, so every pass was killed before that commit.
  No write unit was armed for five hours, and only 100 of 765 Vendor days could be served.
- `release_shard` polled 3,455 `NOT_READY` triggers in fixed `DUE#0`-to-`DUE#15` order. The
  applies and completions in the last shards were never reached.
- The stall could not end by itself, because no other writer removes a units `ARM#` item.
- Every unit test used a constant clock, so no test could see a pass run past its timeout.

The load-path handlers were already bounded: the loader and TableWriter read the remaining
time, and ADR-144 bounds the baker.

## Decision

1. **Read the remaining time before each item.** The promoter and the release build one
   `PassBudget` from the Lambda context (`src/adapters/jobs/execution_graph/budget.py`). A loop
   starts no new item below its reserve. An invalid reading is `EXEC_GRAPH-35` and ends the pass.
   A missing remaining-time function is `EXEC_GRAPH-34`.
2. **Reserves.** Each is measured as time remaining, not time used:

   | Reserve | Value | Where it stops |
   |---|---|---|
   | `PROMOTE_RESERVE_MS` | 40,000 ms | The promoter's waiter phase (`promote_shard`), so the arm pass keeps at least 40 s |
   | `UNITS_COLLECT_RESERVE_MS` | 25,000 ms | Units collection, so the grant phase keeps at least 15 s |
   | `ITEM_RESERVE_MS` | 10,000 ms | Table grants, serving arming and every release item |

   The derivation is in each constant's docstring: about 11 ms per store call, measured on dev on
   2026-11-13. The largest item is about 40 calls, and up to 8 re-reads repeat it. The deploy
   step confirms the reserves from the REPORT durations.
3. **Oldest first, so ordering is unchanged.**
   - The promoter collects units `ARM#` items in D41 order (`root_clock_started_at`, then
     `run_id`). Every run it does not reach is younger than every run it collected, so each table
     grant still goes to that table's oldest PENDING unit.
   - Table grants run oldest head first, so no table can starve behind another by name.
   - The release runs `APPLY#`, then `COMPLETE#`, across every shard, then `RELEASE#` oldest first.
     Each phase reads its prefix in every shard just before it runs.
4. **No cursor.** Whatever a pass leaves stays in its `DUE#` shard, and the next pass reads it
   again strongly. Every commit is still one conditioned transaction, so stopping early loses no
   item and repeats none. A cursor would add writes on the path a timeout kills, and the stuck-flow
   sweep would refuse a new `DUE#` item kind.
5. **Where the reserves live.** They are Python constants, like `INSTANTIATE_GRACE`. In the contract,
   a changed bound is a new protocol version, and a new `serving_coalescing` key would change the
   generated projection under `infra/`, which slice 1 has not approved.
6. **Visibility.** The terminal line of each pass gains `deferred`: the items it left unstarted.
   The promoter's terminal line also gains `grants_deferred`. The function-errors alarms clear
   because passes stop timing out. open-flows-past-ceiling and PromoteObligationAge stay the
   backlog alarms, unchanged.

Decided by (1) and (3) of the
[decision-weighting rubric](../delivery-practice.md#5-the-decision-weighting-rubric). A killed pass
loses committed progress on a path that has no other writer. A budget check per item is the
smallest change that ends the stall.

## Will be enforced by

- `tests/unit/test_execution_graph_pass_budget.py`, using a metered Lambda clock that charges
  each store call and kills the invocation at zero:
  - A 30-run units backlog drains in two passes, oldest 15 first. Each pass returns before the
    kill, and every unit is armed exactly once. On the pre-fix code this test is killed with
    zero commits.
  - A pass killed before any one of its 74 store calls loses no unit and arms none twice.
  - Under the metered clock, a small backlog leaves exactly the same store state as under an
    ample clock.
  - With time for only one grant, the table with the oldest head goes first.
  - With 40 `NOT_READY` triggers in `DUE#0`, the `APPLY#` and `COMPLETE#` in `DUE#15` are
    applied and completed first, and the oldest 17 triggers are polled.
  - Invalid and rising readings are refused with `EXEC_GRAPH-35`.
  - The reserves are ordered and each is under one pass.
- `tests/unit/test_execution_graph_promoter.py`: a waiter reached under the promote reserve
  waits for the next pass, and the next pass promotes it once.
- The existing promoter, release, unit-batch, bake and whole-chain suites pass unchanged, so an
  ample budget changes nothing.

## Consequences

- A backlog drains over several passes instead of stalling. At the dev rates, one pass collects
  about 900 runs (30 s at 32 ms a run) and grants each table its oldest 24 or fewer [inference
  from rca.md §3].
- Serving still drains one alias per domain per pass (rca.md F3). The budget does not change that.
- The release completes a run it applied in the same pass, but not a run it granted in its
  `RELEASE#` phase. That run's `COMPLETE#` waits for the next pass, about a minute, which is less
  than its post-grant work takes anyway.
- The dispatcher, fence settler and node verifier keep the unbounded `_shards` loop. The
  dispatcher reached 34.1 s in D2.

## Reversibility

Delete the budget checks. No stored item, contract or `infra/` value changes, so a reversal needs
no migration.

## Alternatives considered

| Alternative | Why not, or why |
|---|---|
| Raise the two Lambdas' timeouts | Needs `infra/` approval, and any fixed timeout is outgrown by a larger backlog |
| A durable cursor per pass | Adds writes on the path a kill interrupts, and a new `DUE#` kind breaks the stuck-flow sweep |
| Concurrent per-item reads (bounded fan-out) | Raises capacity, but convergence does not need it. It can come later, with the bound from configuration |
| Reserves in the contract | A changed bound is a new protocol version, and the generated `infra/` projection would move |
| **A per-item budget, oldest first, no cursor** | **Chosen** |

## Review trigger

- A graph worker's REPORT duration within 10 s of its timeout.
- A worker timeout changed in `infra/`.
- The dispatcher, fence settler or node verifier approaching its timeout.
