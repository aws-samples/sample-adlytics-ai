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
   | `COMPLETE_RESERVE_MS` | 35,000 ms | The release's waiting `COMPLETE#` items, so its `RELEASE#` phase keeps at least 25 s |
   | `SERVING_STAGE_RESERVE_MS` | 35,000 ms | Serving candidate reads and every alias after a pass's first, so units collection keeps its 10 s share (`UNITS_COLLECT_RESERVE_MS` + 10 s) |
   | `UNITS_COLLECT_RESERVE_MS` | 25,000 ms | Units collection, so the grant phase keeps at least 15 s |
   | `ITEM_RESERVE_MS` | 10,000 ms | Table grants, serving arming, the release's applies, winners and triggers |

   The derivation is in each constant's docstring: about 11 ms per store call, measured on dev on
   2026-11-13. The largest item is about 40 calls, and up to 8 re-reads repeat it. The deploy
   step confirms the reserves from the REPORT durations.
3. **Oldest first, so ordering is unchanged.**
   - The promoter collects units `ARM#` items in D41 order (`root_clock_started_at`, then
     `run_id`). Every run it does not reach is younger than every run it collected, so each table
     grant still goes to that table's oldest PENDING unit.
   - Table grants run oldest head first, so no table can starve behind another by name.
   - The promoter arms the serving `ARM#` of each domain's ACTIVE winner before any other item,
     from the head `coalesce_serving` already read. Its finalizer is the only way the head moves
     on, so a serving backlog can never hide it in a later shard. The other serving items follow
     oldest first.
   - The release runs `APPLY#`, then `COMPLETE#`, across every shard, then `RELEASE#` oldest first.
     Each phase reads its prefix in every shard just before it runs.
   - In `COMPLETE#` the release reads each domain's head once and visits its ACTIVE winner first.
     The other runs wait for the promoter's alias drain, so visiting them is a poll. They go in
     grant-ordinal order and stop at `COMPLETE_RESERVE_MS`, so a `COMPLETE#` backlog cannot take
     the time that issues new grants.
4. **No cursor.** Whatever a pass leaves stays in its `DUE#` shard, and the next pass reads it
   again strongly. Every commit is still one conditioned transaction, so stopping early loses no
   item and repeats none. A cursor would add writes on the path a timeout kills.
5. **Where the reserves live.** They are Python constants, like `INSTANTIATE_GRACE`. In the contract,
   a changed bound is a new protocol version, and a new `serving_coalescing` key would change the
   generated projection under `infra/`, which slice 1 has not approved.
6. **Visibility.** The terminal line of each pass gains `deferred`: the items it left unstarted.
   The promoter's waiter phase still reads each shard after its budget has stopped it, three cheap
   queries, so the count covers every shard. The promoter's terminal line also gains
   `grants_deferred`, `serving_staged` (candidates staged this pass) and `serving_unstaged`
   (candidates left unread). The function-errors alarms clear
   because passes stop timing out. open-flows-past-ceiling and PromoteObligationAge stay the
   backlog alarms, unchanged.
7. **Staged serving election (added 2026-11-13, serve-rca.md §3).** An election reads every
   candidate (about 10 strong reads, 101.5 ms each on dev), and on dev 3,502 of them took about
   400 s, so every 60 s pass was killed before its commit. Now:
   - Candidates are read in grant-ordinal order while `SERVING_STAGE_RESERVE_MS` remains. When the
     budget stops the reads, the validated reads go into `ELECTION#<domain>#<stage_id>#<page>`
     items in the domain's due shard, each created once and split to fit one item. A later pass
     reads only the candidates no page holds, and the commit runs once every candidate is read.
   - The commit evaluates the same functions over the same complete roster, in the same order, as
     one unpaged read: `validate_serving_candidates`, `serving_gap`, the newest winner, the cohort
     and candidate digests, the coordinate proof and the live-frontier ConditionChecks. A roster
     small enough for one pass writes no page, so it commits exactly as before.
   - `stage_id` digests the head version. A waiting candidate (GRANTED, publish SUCCEEDED,
     precompute WAITING_INPUT, bake PENDING, no claim, no binding) changes only through this
     promoter's own serving transitions, and each one moves the head version. Writer census: a
     grant fence and a pause need no `grant_id`; completion, the unarmed settlement, the pin and the
     finalizer act on the armed winner; a claim needs a READY node, and only the election arms one.
     A page under another version is dead and is deleted.
   - A publication that settles during staging is one more unstaged candidate, so it joins the
     roster; its frontier move also fails a commit read before it, as before. Staging converges
     while a pass reads more candidates (about 200) than settle in a minute (at most 22 on dev).
   - A pass that completes the roster with candidate reads of its own reads the head, frontier
     and walk again after them, keeps those reads in memory, and reads only the candidates that
     settled meanwhile before it commits. The commit ConditionChecks the frontier it read, and
     about 22 publications settle a minute on dev, so a frontier read before the candidate reads
     loses the commit to any settlement during them; read after them, it is exposed only to the
     delta and the commit. No cutoff decides it (review round 1 replaced a 5,246 ms cutoff taken
     from a whole measured attempt, not an iteration). The cost is one fresh iteration per
     election, about 0.8 s and 65 reads [inference from the dev rates]. A refused commit writes
     nothing; the next pass re-reads what this pass had not written as pages.
   - Each marker's frontier coordinate is read once with its candidate, so the final transition's
     coordinate proof no longer reads every successor's marker again. A settled marker never
     changes.
   - The page is a read cache of immutable facts, not a cursor: the commit still re-proves `ARM#`,
     `COMPLETE#`, the head and the frontier. The stuck-flow sweep declares `ELECTION` as a
     non-flow kind; the candidates' `COMPLETE#` items stay the flows it counts.
   - Worst case for `SERVING_STAGE_RESERVE_MS`: when a waiter backlog runs its phase to
     `PROMOTE_RESERVE_MS`, serving gets 5 s a pass, about 40 candidates (33 on the final path).
     That still outruns 22 settlements a minute, but a roster of thousands then takes hours
     (`budget.py`) [inference from the dev rates].
8. **Several aliases a pass.** A drain orders its cohort newest grant first from the walk's
   `COMPLETE#` ordinals and reads only the alias it drains next. The first alias of a pass keeps
   the `next_election_min_remaining_ms` guard alone; each further alias, and the final alias's
   successor reads, wait for `SERVING_STAGE_RESERVE_MS`. A non-final drain ConditionChecks the head
   and never moves it, so one head read serves every drain of the pass.
   The guard's 10,493 ms was measured before staging. The pass that commits a fully staged final
   election (the iteration, the `ELECTION#` page query, the head and the 52-member transaction)
   is measured by `validation/run_fresh_election_timing.py --staged-successors N`, which seeds
   the probe table with every successor already on pages. It runs on dev at N = 3,502 and
   N = 5,417 before merge (step s1-serve-deploy); the contract takes `ceil(2 * max(total_ms))`
   over both, and a guard above `PROMOTE_RESERVE_MS` (40,000 ms) is refused, because a fully
   staged commit must fit in every arm pass.
9. **The roster bound.** `serving_candidate_roster_max` is 5,417: the DynamoDB item limit of the
   five items that hold one entry per member, derived over every state those items reach under
   every production writer (contract comment; `serve-fix/r2/roster-bound.json`, 2026-11-14). The
   direct binding binds at 399,937 of 400,000 oracle bytes. A larger roster is refused with
   `EXEC_GRAPH-45` and reported; nothing is dropped. Paging the lists (OQ-28) would remove it.
10. **No reader of a cohort reads its members (added 2026-11-14, review round 1 AGG-1).** Three
   callers used to read every member one at a time inside a fixed time budget: the Release's
   unarmed settlement (`direct_binding`, 60 s pass), the precompute consumer (`direct_binding`
   and `_cohort_eligible`, 30 s) and the bake finalizer (900 s). At about 10 ms a read, a 3,502
   cohort took about 35 s, past the precompute's timeout [projection]. Now the election records
   what they read:
   - the cohort's sorted, distinct contributor roots, in the winner's `RUN#<winner>/SERVING_ROSTER`
     item, created once by the election transaction (one more member: 43 on activation, 52 on the
     final transition). The promoter already writes `RUN#*` and every reader already reads it, so
     no IAM changes;
   - the cohort's publish effect on the winner's bake node: the sum of the verified member
     effects (`advanced_count`, `advanced_curation_count`), how many members' records did not
     re-hash, and the first such member. Each candidate's record is verified when it is read for
     the election; staging carries the result on its page. An unverified member never refuses
     the election (review round 2, AGG-R2-3): the consumer that cannot decide without it raises
     `GRAPH_POST_GRANT/8` for that cohort alone ([ADR-140](adr-140-a-bake-that-skips-the-finalizer-settles-its-cohort.md)
     §2). A verified count is never negative, and every consumer's eligibility is a count above
     zero, so the sum is eligible exactly when some verified member is.

   `read_serving_execution` reads the run, the bake node and the roster: three reads whatever the
   cohort's size.

   **Legacy winners (review round 2, AGG-R2-1).** A winner elected before this change, by the
   deployed code or by an old promoter during the stack update's mixed-version window, has no
   roster item and no recorded effect. `read_serving_execution` returns its context with both
   absent instead of skipping it, so the promoter still arms it and counts it on the terminal
   line (`serving_legacy_winners`). Its two member readers then read as the pre-§10 code did:
   `direct_binding` reads each member's run for its root, and `_cohort_eligible` reads members'
   effects (logged `graph_legacy_serving_cohort`). Every pre-§10 election refused a roster above
   512 (`serving_candidate_roster_max` on main `e4832f94` and the deployed `a232957c`), so that
   is the most such a cohort holds: about 5 s of run reads and 10 s of effect reads at about
   10 ms a read, inside the precompute's 30 s, as before [projection]. A legacy cohort above 512
   cannot have been elected and is refused, `EXEC_GRAPH-45` with reason `legacy_roster`, before
   any member read. No deploy ordering or migration step is needed.

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
  - With 40 `NOT_READY` triggers in `DUE#0`, the `APPLY#` and the winner's `COMPLETE#` in
    `DUE#15` are applied and completed first, and the oldest 10 triggers are polled.
  - With 60 waiting `COMPLETE#` items in `DUE#0`, more than one pass can poll, the winner in
    `DUE#15` is completed and a ready trigger is granted in the same pass. The 5 lowest ordinals
    are polled and 55 items are deferred.
  - With 45 waiting serving `ARM#` items in `DUE#0..14`, more than one pass can visit, the
    winner's finalizer in `DUE#15` is armed first.
  - A waiter in a later shard, left after the promote reserve stopped the pass, is counted in
    `deferred` and promoted once by the next pass.
  - Each of these three has a control with an ample clock, and each fails on the round-1 code
    (`evidence/graph-stall/fix-round-2-repro.txt`).
  - Invalid and rising readings are refused with `EXEC_GRAPH-35`.
  - The reserves are ordered and each is under one pass.
- `tests/unit/test_execution_graph_promoter.py`: a waiter reached under the promote reserve
  waits for the next pass, and the next pass promotes it once. Below the guard the final alias
  waits; at the guard with no successor it drains (42 members); successors are read only at the
  stage reserve, and then elect in that pass (52 members).
- `tests/unit/test_execution_graph_serving_stage.py`, at dev's per-call rates:
  - A roster of 1,030 (twice the old 512 cap) is staged over at least 3 passes, never killed,
    and leaves exactly the store an unbounded pass leaves: same winner, roster and digests.
  - A small roster elects in one pass and writes no page (control).
  - A pass killed before any store call of its first or second pass converges to that state.
  - A publication that arrives while staging joins the roster and wins.
  - A publication that settles during a pass's candidate reads, on a clock that never moves, is
    read again and that pass commits; without the second read the commit fails with
    `SERVING_FRONTIER_CHANGED`.
  - A final alias with 1,030 successors stages them over at least 3 `STAGED` passes, then drains
    and elects in one transaction, leaving no page and exactly the store an unbounded pass
    leaves. A final-path pass killed before any store call of its first or second pass
    converges to that state.
  - A head version change kills the stage; its pages are deleted and re-read.
  - A cohort of 1,029 aliases drains at least 2 a pass over at least 3 passes, and the first pass
    reads only the runs it aliases.
  - A member whose effect does not verify, staged on a page over at least 3 passes, is counted
    on the winner's bake node exactly as an unbounded pass counts it.
- `tests/unit/test_execution_graph_dispatch.py`: 5,417 candidates are accepted and 5,418 are
  refused with `EXEC_GRAPH-45`.
- `tests/unit/test_serving_roster_callers.py`: an elected snapshot padded to 5,417 members that
  have no items drives the Release's unarmed settlement (60 s), the precompute consumer (30 s, a
  curated loser making a zero-curation winner eligible) and the bake finalizer (900 s) under a
  metered clock at dev's per-call rates. Each binds all 5,417 roots, reads no member, and makes
  exactly as many store calls as its paired small cohort (46, 28 and 266). A legacy winner (no
  roster, no recorded effect) is armed and counted by the promoter, finalized with its own root,
  bound by the Release from its members' runs, and decided by the precompute consumer from its
  members' effects; a legacy cohort of 513 is refused with `EXEC_GRAPH-45` before any member
  read, and one of 512 reads its members (control). The widest direct binding at 5,417 roots,
  every contract bake roster row unavailable at the longest reason, fits 400,000 oracle bytes;
  one more roster row does not (control).
- `tests/unit/test_graph_bake.py`: a member whose publish effect does not re-hash is counted at
  the election, which commits; the winner's bake then fails `post_grant_invariant` for that
  cohort alone (U2). A verified curated member decides past an unverified one. A legacy winner
  reads its members and fans out; a legacy unverifiable member fails only its own cohort.
- `tests/unit/test_run_fresh_election_timing.py`: a pre-staged rehearsal sample commits from its
  `ELECTION#` pages, reads no successor again (an unstaged control reads them every attempt),
  and a staged guard above 40,000 ms is refused. A foreign dead-stage `ELECTION#` page refuses
  the sample and stays byte-identical, and the preflight finds it (AGG-R3-1).
- The existing promoter, release, unit-batch, bake and whole-chain suites pass unchanged, so an
  ample budget changes nothing.

## Consequences

- A backlog drains over several passes instead of stalling. At the dev rates, one pass collects
  about 900 runs (30 s at 32 ms a run) and grants each table its oldest 24 or fewer [inference
  from rca.md §3].
- Serving drains about 130 to 250 aliases a pass and reads about 200 candidates a pass, so D2's
  3,502 waiting runs elect in about 18 passes and drain in about 14 to 27 [projection from the
  dev rates; confirmed by the deploy step from the terminal lines].
- Each staged pass adds one page write per about 1,100 candidates and one `ELECTION#` query per
  election attempt. Binding and eligibility read three items whatever the cohort's size (§10).
- The release reads each `COMPLETE#` domain's head once a pass: one item and 32 frontier shards,
  about 0.4 s a domain at the dev rate. With no `COMPLETE#` item it reads none.
- While a `COMPLETE#` backlog lasts, the release polls at most about 25 s of waiting runs a pass,
  and the same lowest ordinals each time. Those runs are closed by the alias drain, not by the
  release, so the order does not delay them.
- The release completes a run it applied in the same pass, but not a run it granted in its
  `RELEASE#` phase. That run's `COMPLETE#` waits for the next pass, about a minute, which is less
  than its post-grant work takes anyway.
- The dispatcher, fence settler and node verifier keep the unbounded `_shards` loop. The
  dispatcher reached 34.1 s in D2.

## Reversibility

Delete the budget checks. Staged pages are read caches: deleting every `ELECTION#` item loses
only reads, which the next pass repeats. The roster bound is a contract value; lowering it again
refuses rosters between the two values.

## Alternatives considered

| Alternative | Why not, or why |
|---|---|
| Raise the two Lambdas' timeouts | Needs `infra/` approval, and any fixed timeout is outgrown by a larger backlog |
| A durable cursor per pass | Adds writes on the path a kill interrupts. The serving stage is not one: it caches immutable facts and the commit re-proves the rest |
| Elect from the oldest 512 | Changes the winner, fails the coordinate proof, and the rest could never be elected (serve-rca.md §3.1) |
| Concurrent per-item reads (bounded fan-out) | Raises capacity, but convergence does not need it. It can come later, with the bound from configuration |
| Reserves in the contract | A changed bound is a new protocol version, and the generated `infra/` projection would move |
| **A per-item budget, oldest first, no cursor** | **Chosen** |

## Review trigger

- A graph worker's REPORT duration within 10 s of its timeout.
- A worker timeout changed in `infra/`.
- The dispatcher, fence settler or node verifier approaching its timeout.
