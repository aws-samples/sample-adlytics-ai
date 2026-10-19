# ADR-034: A withheld residual is keyed on its delivery, not on its row

Date: 2026-08-21
Status: Accepted

> **Proposed amendment, 2026-10-06.** [ADR-099](adr-099-treat-distinct-shopper-counts-as-a-new-kind-of-fact.md) (Proposed) would let a pack that declares `discard_suppressed_rows` discard withheld rows: their additive measures still go to the residual, and their distinct counts don't. This decision stays in force, unchanged, until ADR-099 is accepted.

Narrows [ADR-033](adr-033-a-load-owns-columns-not-a-partition.md)'s unit of ownership for one kind of row, and makes [ADR-019](adr-019-grain-viability-is-measured.md)'s "the coarse total survives intact" true rather than aspirational. Closes [G-31](../decision-coverage-registry.md#gap-register-g-nn).

## Context

AMC nulls each output column independently, **against that column's own threshold**. Measured on the sandbox, 22,075 flagged rows [verified 2026-08-21, `validation/probe_suppression_nulls.py`]:

| Distinct users behind the row | `campaign_id_string` (LOW, bar 2) | Rows |
|---|---|---|
| **1** | **NULL** | 20,813 |
| 2 or more | populated | 1,262 |

`postal_code` (HIGH, bar 100) was NULL in all of them. The metrics survive throughout — 1,523 flagged rows carried non-zero purchases.

**Our contracts map only NONE and LOW columns**, so the bar is uniformly 2 and the outcome is binary: a row with two or more users keeps every dimension, and a row with one loses every dimension. There is no partially-keyed row to model.

That one-user row is the problem. Every AMC contract maps three LOW columns into its grain — `campaign_id_string` → `campaign_key`, `event_date_utc` → `date_key`, a marketplace column → `marketplace_key` — and all three are non-nullable in the generated row models. So the row fails validation and **the whole partition quarantines**, for data behaving exactly as Amazon documents.

It cannot simply be dropped either. ADR-019's entire derivation rests on those measures surviving: *"summing every row, including the nulled ones, gives the true total."* Discard the residual and every coarse total is short by an unstated amount.

### Why the obvious fix fails, and the cheaper one too

**Make the three columns nullable.** Tested against a scratch Iceberg table partitioned exactly like `fct_ad_exposure` [verified 2026-08-21]:

| Question | Result |
|---|---|
| Does Iceberg accept NULL in an identity partition column? | **Yes** |
| Does ADR-033's partition predicate select such a row? | **No** — 1 of 2 rows returned |
| Does the merge match it with plain equality? | **No, it duplicates** — `NULL = NULL` is NULL, not true |
| Does `IS NOT DISTINCT FROM` match it in a `MERGE ON`? | **Yes** |

Storage was never the obstacle. This is:

**Two windows' residuals are the same row.** The columns AMC nulls are precisely the ones identifying the window and marketplace, so the residual for one window and the residual for any other share a single all-NULL grain. The NULL-safe merge demonstrated it — one source row matched *both* existing all-NULL rows and updated both. A residual could not be attributed to the period it came from, and each delivery would overwrite the last. Not a repair; a silent collision.

**Stamp the window's first day into `date_key` instead.** Genuinely tempting, because it needs no new table: residuals for different windows would then differ, the partition predicate would select them, and only `campaign_key` would stay null. Rejected because it is a fabrication that a date-grouped answer would report. A week's withheld impressions would all land on the Monday, and "impressions by day" would show an inflated first day with nothing indicating why.

## Decision

**A withheld residual is a fact about a delivery, so it is keyed on the delivery.** It goes in a sibling table per suppressible fact, whose grain is what the manifest declares:

```
fct_<name>_withheld
  grain      contract_id, marketplace_key, period_start, period_end, option_set_hash
  provenance basis_key, source_timezone
  measures   the fact's own measure columns, all nullable
  partition  (marketplace_key, period_start), identity
```

**Every grain column comes from the manifest or from the contract's identity, and therefore cannot be nulled.** That is the whole point: AMC withheld the row's dimensions, but *we* authored the delivery's, because we landed it. The NULL problem does not need solving — it needs moving to a table whose key suppression cannot reach.

**The table is derived, never declared.** Any fact whose contracts declare a `suppression` block gets one, computed from the merged fact the way the DQ rulesets already are. A per-contract opt-in would be a second place for the same fact to be recorded, and it would be wrong the first time a contract added suppression.

**`contract_id` is in the grain**, so the four contracts writing `fct_ad_exposure` own provably disjoint residual rows. ADR-033's separability question is answered by construction rather than by an ownership predicate.

**The write is a delete-then-insert scoped to the exact delivery key**, not a partition overwrite and not a column-scoped merge. One delivery produces at most one residual row — AMC merges identical-after-nulling rows, and for our column set every fully-nulled row is identical — so several arriving is aggregated into one. A redelivery with nothing withheld deletes and inserts nothing, which is how the residual is withdrawn when suppression stops.

**Nothing reads it yet.** Phase 1 makes ingestion correct and the residual queryable. Combining it into an answer is [G-30](../decision-coverage-registry.md#gap-register-g-nn), and deliberately separate: the residual is the withheld *quantity* that gap needs, so landing it first is what makes that work possible rather than speculative.

> **Amendment, 2026-09-25 — G-30 closed without reading the residual.** [ADR-035](adr-035-suppression-is-a-quantity-not-a-flag.md) closed G-30 on 2026-08-21 by folding suppression over the fact, and deliberately keeps this residual out of answers (its "The withheld residual does not join an answer yet"). No query path under `src/` reads a `*_withheld` table as of 2026-09-25; joining the residual into an answer is ADR-035's review trigger, not open G-30 work.

## Consequences

**A one-user row stops quarantining a partition.** The failure this closes is total — no data at all for the window — so this is the highest-value part.

**A coarse total becomes `fact sum + residual`, and only then is it exact.** ADR-019 claimed the coarse total survives intact. It does, but not from the fact alone: the residual carries the part whose dimensions were withheld. That claim is narrowed here rather than left to be discovered.

**The residual is honestly unattributable and the schema says so.** It has a window rather than a date and no campaign at all, so no query can accidentally break it down by something it does not carry. Compare the rejected date-stamping option, where the schema would have implied a precision the value does not have.

**A second table per suppressible fact.** Three today. They are narrow and hold one row per delivery, so the cost is schema surface rather than storage — and the alternative was a wrong number.

**The residual is per contract, which is right here and was wrong in [G-28](../decision-coverage-registry.md#gap-register-g-nn).** That gap removed per-delivery diagnostics from a conformed fact because a row several deliveries assembled cannot hold one delivery's value. The residual has the opposite shape: `contract_id` is in its grain, so each delivery's value has its own row and nothing is overwritten. The same fact recorded at the grain it belongs to.

**Suppression's meaning is now split across two tables, deliberately.** `rows_suppressed` in the fact says "this row's figures were re-aggregated after nulling"; the residual says "these figures could not be attributed at all". Both are needed, and conflating them was what made the model unable to hold the second.

## Alternatives rejected

| Rejected | Why |
|---|---|
| **Nullable grain columns on the fact** | Residuals collide across windows — measured, not reasoned |
| **Stamp the window's first day into `date_key`** | Fabricates a date; a date-grouped answer reports it |
| **Refuse the row** | Quarantines a partition for correct data, and discards the residual ADR-019 depends on |
| **Filter suppressed rows at extraction** | Loses the total permanently, contradicts ADR-019's basis, and we do not run extraction |
| **Store the residual in the freshness registry** | Tempting — it already holds per-delivery observations keyed on `contract_id`. Rejected because a residual is a *measure*, and a measure read from DynamoDB has to be added to a SQL result by hand. Two homes for one figure is how the two come to disagree |
| **One conformed residual table for every fact** | A sparse union of every fact's measures, and a reader would have to know which columns apply to which fact |

## Review trigger

Revisit if a contract ever maps a MEDIUM or HIGH column. The bar becomes 100, the binary all-or-nothing property is lost, and a partially-keyed residual — some dimensions surviving, others not — becomes reachable. That row belongs in neither table as designed here.
