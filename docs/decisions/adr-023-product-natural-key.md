# ADR-023: Facts join `dim_product` on the natural key, not a surrogate

Date: 2026-08-19
Status: Accepted

> **One mechanism named below was withdrawn by [ADR-028](adr-028-a-validated-plan-replaces-the-template-registry.md), later the same day.** This ADR twice says the validity predicate "belongs in the allowlisted templates". There is no template registry: a tool passes a validated plan and the adapter composes the statement, so the predicate belongs in composition. The decision itself — natural key, no surrogate — and its cost are unchanged, and the point being made is unchanged too: the fan-out this key permits must be prevented by code, not by a caller remembering.

## Context

[canonical-model](../canonical-model.md) §3 declared `product_key` as a **surrogate** on `dim_product`, and every fact contract named its key column `product_key`. Writing the `dim_product` load exposed that the surrogate did not exist: `dim_product` declared no `product_key` column at all, and all four facts mapped `product_key` **straight from an ASIN string** — `salesByAsin[].asin` and `trafficByAsin[].asin` on the vendor side, `tracked_asin` and `asin` on the AMC side. Each contract's own comment said "resolved against `dim_product` on the `amazon_asin` namespace".

So the name claimed a level of indirection nothing implemented. The generated schema confirmed it: `dim_product`'s columns were `marketplace_key, amazon_asin, amazon_parent_asin, brand, brand_code, is_current, product_title, valid_from, valid_to` — no surrogate to point at.

Two ways to close the gap: drop the surrogate and name the column what it is, or build the surrogate the document promised. This decision is not "is a surrogate good dimensional practice" — it is, and this ADR departs from it deliberately for reasons specific to this platform.

## Decision

**A fact carries `amazon_asin` and joins `dim_product` on `(amazon_asin, marketplace_key)`.** There is no surrogate. `product_key` is renamed to `amazon_asin` on `fct_attributed_conversion`, `fct_product_page_views`, `fct_retail_purchase_amc` and `fct_retail_sales`, and the surrogate row is removed from `dim_product`.

The deciding reason is that **the caller chooses as-was or as-is, rather than the load choosing once.**

`dim_product` is SCD2 because a renamed brand is a new version, not a correction — overwriting would make last quarter's brand rollup disagree with itself. With a surrogate, the fact load resolves each ASIN to the version current at the fact's date, so a Q1 fact points at Q1's brand permanently. That answers "the brand as it was" and nothing else. With the natural key the predicate decides:

```sql
AND p.is_current                                              -- the brand as it is now
AND f.date_key >= p.valid_from AND f.date_key < p.valid_to    -- the brand as it was then
```

A brand manager plausibly wants both, and tools run allowlisted templates, so encoding either predicate is cheap. Losing one of the two answers to save a join column is the wrong trade.

Three supporting reasons:

- **The surrogate's real prize is deferred.** Cross-retailer identity is what a surrogate uniquely buys, and the deferred-work record notes `dim_product.gtin` has no confirmed source and that Amazon-only identity is unaffected. The model's stated extension path is a namespaced column per platform on the same row, not a surrogate mapping table.
- **Debuggability suits the platform's posture.** A quarantined row holding `B07FZ8S74R` can be checked against Amazon; one holding `8471223` needs a join before it means anything. This product treats a plausible wrong answer as the primary risk.
- **Less machinery in the pipeline that does not exist yet.** A surrogate adds a dimension lookup inside every fact load, a load-order dependency so facts cannot be written before `dim_product` is current, a surrogate that must be *looked up rather than generated* because restatement re-lands partitions on a 30–45 day window, and a rule for which version a fact resolves to.

Storage and join cost are **not** a factor either way. At these volumes — pre-aggregated daily output, megabytes — with Parquet dictionary encoding on a heavily repeated ten-character string, int-versus-string is noise.

## Consequences

- Every fact carrying `amazon_asin` also carries `marketplace_key` in its grain, verified across all four, so the composite join is available everywhere and no fact needs widening.
- A query that forgets the validity predicate gets a row per version rather than one row. That is a fan-out a surrogate would have prevented, and it is the real cost of this decision. It belongs in the allowlisted templates, not in a caller's memory.
- `marketplace_key` stays in `dim_product`'s key. Whether it must is **[unverified]** — see [Q-16](../decision-coverage-registry.md#question-register-q-nn).
- Renaming a canonical column changes its Iceberg field id, so this is free today and a migration with a backfill once `dim_product` holds history ([data-layer](../capabilities/data-layer/low-level-design.md) §9). Doing it before any load is the cheap moment.

## Alternatives rejected

| Alternative | Why not |
|---|---|
| **Build the surrogate as declared** | Fixes the document by adding machinery rather than fixing the name. Costs a lookup in every fact load and permanently forecloses as-is reporting, to buy cross-retailer identity that is deferred |
| Keep the name `product_key` mapping an ASIN | The name says surrogate and the value is a natural key. This is the state that produced the confusion |
| Surrogate on `dim_product` only, facts keep the ASIN | Two keys for one thing, and nothing joins on the surrogate. It would exist to satisfy a document |
| Hash the ASIN into a numeric key | A deterministic surrogate is still opaque in a quarantine dump and still loses as-is, while adding a collision class |

## Review trigger

Reopen when a **second retail platform** is scheduled. That is the case where facts from different sources would otherwise carry different key columns, and it is the one thing the surrogate does that a namespaced column does not. The migration is mechanical in that direction: facts hold the natural key, so the surrogate resolves and backfills.

Reopen sooner if the validity-predicate fan-out shows up as a real defect in a template rather than a hypothetical — that would be evidence the load should have resolved the version after all.
