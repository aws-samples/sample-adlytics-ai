# ADR-025: An SCD2 plan is applied close-first, because Athena has no transaction

Date: 2026-08-19
Status: Accepted

## Context

Building the write path for `dim_product` — the adapter that applies the `MergePlan` from `domain/curation/scd2.py` — turned up a constraint the merge decision does not have to think about.

**One changed attribute requires two writes.** Closing the version in force means setting its `valid_to` and its `is_current`; opening the successor means inserting a row. Both must happen for the dimension to be correct.

**A single `MERGE INTO` cannot do it.** SQL's `MERGE` permits one action per matched row, and SCD2 needs two against the same match: update the old, insert the new. This is not an Athena limitation — it is what `MERGE` means. So the write is an `UPDATE` followed by an `INSERT`.

**Athena has no transaction across statements** [verified 2026-08-19]. Each statement is its own Iceberg snapshot commit. There is therefore a moment between the two writes in which the table is in neither the before state nor the after state, and a failure, a timeout or a killed job can leave it there.

Two orders are available and they fail differently:

| Order | State after an interruption | How it presents |
|---|---|---|
| Close, then open | The product has **no row in force** | It disappears from every breakdown that joins the dimension |
| Open, then close | The product has **two rows in force** | Every fact row for it joins twice, so its figures roughly double |

The second is the failure mode this platform exists to prevent. A doubled figure is individually plausible, violates no rule, produces no null and reads as a good quarter. The first is an absence, and an absence is something a reader can notice.

They also differ in how they are detected and repaired. Two rows in force is caught by `merge_scd2` itself — `_index` refuses a current state containing two rows for one key — but *only on the next delivery*, which may be a day later, and every question asked in between is answered wrongly. No row in force is repaired by the next delivery automatically: the merge sees no current version and inserts one.

## Decision

**Close first, then open. The order is a correctness property of the write path, not an implementation detail.**

`MergeStatements.ordered` states it in one place — closes then inserts — and `AthenaDimensionAdapter.apply` runs that sequence and nothing else. A caller cannot reorder it because it never sees the two halves separately.

Two supporting decisions fall out of the same reasoning:

**Each half is one statement, however many rows it touches.** One `UPDATE` closing every version the plan closes, one `INSERT` opening every version it opens. A statement per row would multiply the un-transacted window by the number of rows as well as committing one Iceberg snapshot per row, leaving a table whose metadata outweighs its data.

**A plan that changes nothing sends no statement at all.** An `UPDATE ... WHERE` matching no rows still commits a snapshot, so a re-run proving it had nothing to do would grow the table's metadata every time — turning G-01's idempotency into unbounded growth of a different kind.

## Consequences

**The recoverable state is the safe one, and it self-heals.** An interrupted apply leaves a gap in that product's history rather than an overlap, and the next delivery closes the gap by opening a new version. Nothing has to be manually repaired for the dimension to become correct again.

**The gap is real and is not silently repaired.** Between the closed `valid_to` and the next delivery's `valid_from` there is an interval with no version in force, so an as-was join over a date in that interval finds no row for that product. Re-running the *same* load does not close the gap — it opens a version starting at the new `loaded_at`. That is visible in the data (a product whose history has a hole) rather than announced, which is the residual this decision accepts. Closing it properly needs the run ledger, which can tell a retry that it is a retry and reuse the original `loaded_at`.

**The one-row-in-force invariant is now asserted against a real table**, not inferred. `tests/integration/test_dimension_merge.py` checks it across the whole dimension rather than only the product it changed, so a previously interrupted run is caught by the next test run rather than by the next load.

**Counts are read off the plan, never from the engine.** Athena reports no affected-row count for DML, so `MergeOutcome` reports what the plan said it would do. That is honest but weaker than it looks: it confirms the plan was applied without error, not that the engine matched the rows expected. The integration tests therefore assert through a subsequent read.

**`closing_values` had to be added to the domain.** Closing a version writes `valid_to` *and* `is_current`, and the second half was stated only in a comment in the contract YAML that no code read. An adapter that set `valid_to` alone would produce exactly the two-rows-in-force state this decision is arranged to avoid, so the pair now lives in `scd2.py` where the merge decision lives.

## Alternatives rejected

| Rejected | Why |
|---|---|
| **Open first, then close** | The natural order if you think of a load as "add the new data", and it fails towards a duplicated figure. It also delays detection to the next delivery, so every answer in between is wrong and plausible |
| **One `MERGE INTO` doing both** | Not expressible. `MERGE` allows one action per matched row and SCD2 needs an update *and* an insert against the same match |
| **A doubled source so one `MERGE` handles both** | The standard trick: emit each changed row twice, once keyed to match and once not. Trino requires that each target row match at most one source row, so the two copies have to be distinguished by a key the target does not share — which means synthesising a key column purely to defeat the engine's own safety check. A generated statement nobody can read is worse than an ordering rule |
| **Write to a staging table and swap** | A real answer to atomicity, and it buys it by making every load rewrite the whole dimension. It also needs a second table per dimension in the stack, and `ALTER TABLE ... RENAME` on a Glue Iceberg table is not the atomic swap the pattern assumes |
| **Verify after writing and repair** | Turns one un-transacted pair into a three-statement sequence with its own windows, and the repair itself can be interrupted. The invariant check belongs where it already is — in `merge_scd2`, refusing to compute a plan against a broken current state |
| **Accept the duplicate and filter it at read time** | Would mean every query joining a dimension carries a "pick one version" subquery, and the query that forgot would double silently. The same argument the sentinel `valid_to` was chosen over NULL for |

## Review trigger

Revisit when the run ledger exists. A ledger keyed on `(contract_id, window, manifest_checksum)` can recognise a retry and hand it the original `loaded_at`, which closes the history gap this decision accepts — a re-run would then re-open the version at exactly the instant the interrupted run closed the old one, leaving no interval uncovered.

Also revisit if Athena gains multi-statement transactions over Iceberg, which would make the ordering argument unnecessary rather than merely mitigated.
