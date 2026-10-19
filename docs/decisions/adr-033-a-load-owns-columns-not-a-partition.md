# ADR-033: A load owns a partition's worth of one contract's columns, not the partition

Date: 2026-08-21
Status: Accepted

Narrows [ADR-022](adr-022-contract-decomposition-by-dataset.md)'s consequence for the one fact several contracts write, and changes the write half of the pipeline [load-path](../capabilities/load-path/architecture.md) §5a describes. The restatement guarantee that motivated partition overwrite is unchanged.

## Context

A fact load is an overwrite rather than an append, and it has to be: both sources restate history, so the same window arrives repeatedly with corrected figures, and appending would accumulate every revision as extra rows until every total climbed on its own. So a delivery clears the partition it supersedes and writes its rows — and clearing comes from the manifest rather than from the rows, so a delivery that mapped to zero rows still empties a window whose figures were all withdrawn.

That is correct for **seven of the eight curated tables**, because each is written by exactly one contract. A partition and a contract's ownership are the same thing there.

`fct_ad_exposure` is written by **four**. `amc-dsp-impressions` supplies impressions, spend and reach; `amc-dsp-clicks` supplies clicks; `amc-dsp-views` supplies the viewability measures; `amc-reach-frequency` supplies the sponsored-ads side of impressions, clicks, spend and reach. Its Iceberg partition spec is `(date_key, marketplace_key)`, identity-transformed, generated from the contract's own `partition_keys`.

**So each of the four deletes the other three.** Verified on the deployed dev environment, 2026-08-21: after landing all four deliveries and loading them in sequence, the table held six rows rather than twenty-four — one contract's worth — with `viewable_impressions` null everywhere, because the only contract supplying it was not the last to write. Which measures survive depends on arrival order, which in production is a scheduling accident.

The composed statement makes the cause plain. For this fact the predicate binds only the two partition keys:

```sql
DELETE FROM "fct_ad_exposure"
WHERE "date_key" BETWEEN DATE '2026-08-10' AND DATE '2026-08-15'
  AND "marketplace_key" = 'ATVPDKIKX0DER'
```

Nothing in it distinguishes the calling contract, so it cannot leave a sibling's rows alone.

The same instinct already exists one level down: `_partition_predicate` refuses to compose a delete for a partition key it cannot bound, on the grounds that a missing term deletes *across* that key. This is that argument applied one level up — the delete is missing a term for *which contract's columns* it is entitled to remove.

## Decision

**A load's unit of ownership is a partition's worth of the columns one contract declares. Not the partition.**

A fact written by one contract keeps partition overwrite unchanged. A fact written by several is written in two statements:

```sql
-- 1. Supersede only what this contract owns: its columns, and only the rows it could have
--    written. The partition alone is not that second thing — see "Ownership is narrower
--    than the partition" below.
UPDATE <fact> SET <this contract's measures> = NULL
WHERE <partition predicate> AND <ownership predicate>

-- 2. Merge this contract's rows on the fact's full grain.
MERGE INTO <fact> AS t USING <rows> AS s
  ON  t.<grain column> = s.<grain column>   -- every grain column, not the partition keys
WHEN MATCHED THEN UPDATE SET <this contract's columns> = s.<...>
WHEN NOT MATCHED THEN INSERT (...) VALUES (...)
```

### Ownership is narrower than the partition

Stating this separately because the first version of this decision got it wrong, and the error
survived a passing test suite.

A partition of `fct_ad_exposure` holds rows from all four contracts. So a withdraw scoped to the
partition alone reaches a **sibling's** rows — and for a column two contracts both write, it nulls
figures the load never mentioned. **Observed on deployed dev**: `amc-reach-frequency` loaded last
and left the six `amazon_dsp` rows with no impressions, clicks or spend, having withdrawn all three
across the whole window before merging only its own sponsored rows. The table read as
half-populated, and nothing failed.

So the withdraw also carries a term for **every grain column whose values the contract can state**:

```sql
-- amc-dsp-impressions, which stamps both as literals
AND "ad_product_key" = 'amazon_dsp' AND "channel_key" = 'amazon_dsp'

-- amc-reach-frequency, which reads both from ad_product_type
AND "ad_product_key" IN ('sponsored_products', 'sponsored_brands', 'sponsored_display',
                         'sponsored_television')
AND "channel_key"    IN (…the same four…)
```

A contract states its values two ways, and they are one claim at different widths: a **literal** is
a domain of one, and a declared **`value_domain`** is a domain of several. The second is new, and it
exists because `channel_key` on the sponsored-ads contract is *read* rather than stamped —
`sponsored_ads_traffic.ad_product_type` is documented as exactly four values
([amc.md](../sources/amc.md) §4, `[verified]`), which is enough to bound it.

A grain column the contract cannot bound contributes no term. `campaign_key` is unbounded — no
source enumerates campaigns — and inventing a term would withdraw *less* than the contract owns,
leaving superseded figures standing. Unbounded is the honest answer, not the lenient one.

Which strategy applies is **derived from the model, never configured**: it is a property of how many contracts target a fact, and the generated table inventory already states that. A per-table setting would be a second place for the same fact to be recorded, and it would be wrong the first time a fifth contract began writing the exposure fact.

Step 1 nulls rather than deletes, and that is the whole decision in one line. Deleting would take rows a sibling contributed; nulling withdraws precisely the figures this contract published and leaves every other column of the row intact.

The `ON` clause matches on the fact's **full grain** rather than its partition keys. Partition keys identify a file layout; the grain identifies a row. Matching on the partition would update every row in it.

**Capability verified before deciding**, against the deployed Iceberg v2 tables on Athena engine version 3 (2026-08-21): `MERGE INTO` with a `WHEN MATCHED` branch, with both branches, and a column-scoped `UPDATE … SET col = NULL`, each executed successfully.

## Consequences

**Order stops mattering, which is the point.** Four deliveries arriving in any sequence leave the same table, because each writes only its own columns. That removes a class of defect that presents as missing data rather than as an error.

**A partition can be honestly half-loaded.** `fct_ad_exposure` with impressions but no viewability is now a real and correct state — one contract has landed and another has not. Freshness already reports per contract, because the registry keys on `contract_id`, so a tool reading coverage across a fact already reduces several observations to the narrowest ([ADR-031](adr-031-a-control-total-failure-quarantines-every-grain.md)'s rule applied to a second input). Nothing there changes.

**Two contracts writing one measure must be provably separable, and that is now a gate.** `amc-dsp-impressions` and `amc-reach-frequency` both supply `impressions` and `spend`. What keeps them apart is that `channel_key` is in the grain and their domains for it are disjoint. This began as a note here, which was not enough — the withdraw defect above is exactly what an unasserted invariant permits. `check_shared_measures_are_separable` now refuses any pair of contracts writing one fact that share a measure and cannot be told apart by a grain column **both** bound. A column only one side bounds proves nothing, because the unbounded side may produce the same value.

**Delivery metadata is exempt from that rule, and the exemption is a known gap.** AMC synthesises `suppression_reason` and `distinct_user_count` into every delivery, so any two contracts on one fact necessarily share them. They describe the delivery rather than the row, so a row assembled from three deliveries has three suppression stories and one column to hold them — the last load wins. That ambiguity predates this decision and narrowing a predicate cannot fix it, so the rule excludes them and [gap register](../decision-coverage-registry.md#gap-register-g-nn) carries the question.

**Step 1 can leave a row with every measure null.** It happens when a contract supersedes a grain it no longer delivers and no sibling covers that row. Such a row is not harmful to a sum, but it is a row in a grouped answer with nothing in it. Left in place deliberately for now rather than swept: a delete pass would have to distinguish "no contract has anything to say about this grain" from "this contract has nothing to say", and the second is the ordinary half-loaded state. Recorded in [gap register](../decision-coverage-registry.md#gap-register-g-nn).

**Two statements per fact load rather than two.** Same count, and the merge scans the partition it matches against rather than only writing, so a load costs more than an overwrite. Acceptable: correctness of a conformed fact against the cost of one grouped scan per delivery.

**Only one fact changes behaviour today.** Seven tables keep the path they had, and their tests are unchanged — which is what makes this safe to land against a live dev environment.

## Alternatives rejected

| Rejected | Why |
|---|---|
| **Put a dataset discriminator in the grain and the partition**, so each contract owns its own partitions and overwrite survives | It changes what a row *is*. `clicks` and `impressions` would land on different rows at the same date, channel, campaign and marketplace, so every same-fact ratio breaks: `ctr` would have to sum across the dataset dimension, and [ADR-024](adr-024-plans-account-for-every-grain-column.md) refuses exactly that — correctly, because summing across a grain column is a claim it is addition |
| **Split `fct_ad_exposure` into four facts and conform at read time** | Genuinely tempting now that cross-fact drill-across exists, and it removes the class of problem rather than handling it. It fails on `ad_spend`: one column conformed from two datasets, so reading it needs a **union** across facts, and drill-across is a join. Building union machinery is a larger change than this decision, and it would make the platform's most-used denominator the most complex read in it |
| **Keep the delete and scope it by the columns this contract owns being non-null** | Reads as a smaller change and is not one. A row where every one of this contract's measures is legitimately zero is indistinguishable from a row it never wrote, so the predicate deletes rows it does not own — the same defect with a narrower blast radius |
| **Order the contracts so the widest writes last** | Depends on every contract's column set being a superset of the ones before it, which is false — nothing supplies viewability except `amc-dsp-views`. It also makes correctness a property of a load schedule, which is the thing that failed here |
| **One contract per fact, by merging the four AMC contracts into one** | Reverses [ADR-022](adr-022-contract-decomposition-by-dataset.md). One contract covers one source dataset at one grain, and four datasets in one contract cannot declare four sets of source paths or be quarantined independently |

## Verification

Proven against the deployed dev environment on 2026-08-21, after the ownership predicate was added. Figures are the generated fixtures', so what matters is the shape and the invariance, not the magnitudes.

| Property | Evidence |
|---|---|
| All four contracts coexist | 12 rows. The six `amazon_dsp` rows carry impressions, clicks, spend, viewability, measurability, reach and media-only spend — all four contracts' columns on one row |
| A half-loaded fact is honest, not wrong | The six sponsored rows carry impressions, clicks, spend and reach, and leave viewability null. No sponsored dataset supplies it |
| **Order does not matter** | Loaded sponsored→views→impressions→clicks, the reverse of the natural key order: `12 rows, 924 impressions, 1092 clicks, 376.7400 spend, 1533 viewable, 2394 reach` — identical to the forward order, figure for figure |
| A second identical load changes nothing | Same six figures again. No duplicate grain, no row accumulation |
| No orphan rows | Zero rows with every measure null, so residual (2) below is currently unrealised |

## Review trigger

Revisit if a fact acquires a second contract whose grain columns are all unbounded. `check_shared_measures_are_separable` will refuse it, and the fix is either a `value_domain` the source documents or a modelling change — not a wider predicate.

Revisit the null-row residual if a grouped answer is ever observed carrying one, rather than pre-emptively.

Revisit the suppression-column exemption when a customer asks why a row says suppressed. That is the question that makes the ambiguity concrete, and answering it needs a per-dataset suppression column or an explicit decision to report the coarsest.
