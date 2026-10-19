# ADR-021: One currency column per fact, with a load-time agreement assertion

Date: 2026-08-19
Status: Accepted

> **Producer-pack amendment, 2026-08-24.** The one-currency-per-fact decision is unchanged. Current `fct_ad_exposure` carries fee-inclusive `spend` only: `impression_cost` is omitted because its fee scope remains unverified and sponsored ads has no equivalent. Current `fct_retail_purchase_amc` carries aggregate `revenue_purchased` and `units_purchased`, not event-level `unit_price`, because a stored price at daily ASIN grain would be an aggregatable ratio. [canonical-model](../canonical-model.md) owns the current field set.

## Context

Money reaches us in two different shapes, and the canonical model has to pick one.

[sources/vendor-central](../sources/vendor-central.md) §3 recorded this as an open decision — *"flattening to `<field>_amount` + `<field>_currency` is a decision to make once and apply consistently — there are nine money-ish fields across the report family"* [verified]. The `data-contract-vendor` steering then compressed that sentence into an imperative, which read as though the decision had been made. It had not. Meanwhile [canonical-model](../canonical-model.md) §6 answered it the other way, mapping `shippedRevenue.currencyCode` to a single shared `fct_retail_sales.currency_code`. Two documents disagreed because an open question had been written down as a rule.

**The asymmetry between the two sources is what makes this non-obvious.**

| Fact | Money columns | Currency values the source gives us |
|---|---|---|
| `fct_ad_exposure` | `spend`, `spend_media_only` | **one** — `currency_iso_code` |
| `fct_attributed_conversion` | `revenue_attributed`, `..._incl_halo`, `..._new_to_brand_12mo` | **one** — `purchase_currency` |
| `fct_retail_sales` | `revenue_shipped`, `cogs_shipped`, `revenue_ordered` | **three** — one per `Amount` object |

All [verified]. AMC hands over one currency per table, so per-field currency columns there would mean fabricating three columns from one source value — inventing structure that does not exist upstream. Vendor Central hands over one per amount, so a single shared column discards structure that does exist. **Neither shape is right on both sources**, which is why the question deadlocked rather than resolving.

**The risk is narrower than it first appears, and it is worth separating two cases that get conflated.**

*Cross-row* mixed currency — a US and a UK marketplace inside one rollup — is unaffected by this decision. Both shapes keep each row's own currency and both require the semantic layer to convert via the dated `fx_rate` table ([ADR-004](adr-004-timezone-and-fx.md)). Nothing is lost either way, and this is the case that actually occurs.

*Within-row* divergence — `shippedRevenue` in USD beside `shippedCogs` in EUR on the same ASIN, day and marketplace — is the only case a shared column cannot represent. `fct_retail_sales` carries `marketplace_key` in its grain and `dim_marketplace` carries `default_currency_code`, so one row is one marketplace. For divergence to occur, Amazon would have to report cost and revenue for the same transaction set in different currencies. Implausible, and **[unverified]** — the documentation neither promises nor forbids it.

So the real question is not which shape is prettier. It is what to do about an assumption we cannot verify from documentation.

## Decision

**One `currency_code` column per fact.** Uniform across both sources, which keeps `fct_cross_channel_performance` — derived by merging ad and retail facts — from having to reconcile two different conventions. `contracts/shared/metric-dictionary.yaml` already assumes this with `currency: { kind: currency, currency_from: source_column }`, singular, and stays as it is.

**Every load asserts that all source currency values in a row agree.** Where several amounts collapse onto one `currency_code`, the contract names the source paths that must be equal:

```yaml
currency_agreement:
  - salesByAsin[].shippedRevenue.currencyCode
  - salesByAsin[].shippedCogs.currencyCode
  - salesByAsin[].orderedRevenue.currencyCode
```

**The gate derives what that list should contain rather than trusting it.** For every monetary field whose source path ends in `.amount`, the sibling `.currencyCode` path is computed; if two or more distinct siblings collapse onto one currency column, all of them must appear. A contract cannot satisfy this rule by declaring a shorter list, because the expected set comes from its own field mappings.

**On disagreement the partition quarantines and alarms.** Not a dropped row and not a coerced value — the same behaviour as a failed header assertion, and owned by [load-path](../capabilities/load-path/architecture.md) §5 rather than restated per contract. Yesterday's good partition stays live.

**The rule does not fire on per-field currency.** A contract that gives each amount its own currency column has nothing to assert, so a future source needing that shape is not obstructed.

## Consequences

**The unverifiable assumption becomes continuously tested instead of permanently assumed.** This is the whole point. If the assertion never fires across a year of loads, that is stronger evidence than any amount of documentation reading would have produced. If it fires once, we learn something no design discussion would have surfaced.

**The failure mode moves from silent to loud.** Previously a mixed-currency row would have been resolved to `shippedRevenue`'s currency and then converted at the wrong rate in the semantic layer, producing a plausible wrong number. Now it stops the load.

**The model stays uniform, so the semantic layer stays simple.** Every money metric resolves its fact's `currency_code`. A consequence found while building the query planner: **`currency_code` is on the fact and in the grain of none of them**, so a query has to select it deliberately. `QueryPlan.carried_columns` is where that is recorded — the column labels a figure rather than slicing it, so it belongs in the `SELECT` and the `GROUP BY` but never in the answer's reported grain. Grouping by it also makes a genuine within-result currency mix arrive as extra rows, which can be refused, instead of collapsing silently into one mislabelled total. Per-field currency would have made currency resolution a property of each measure rather than each fact, touching every money metric in the dictionary.

**We store less than we were given, and that is a real cost.** Two of the three vendor currency values are asserted and discarded rather than persisted. If Amazon ever does emit mixed currencies within a row, loads quarantine until the schema migrates to per-field columns — we cannot serve those rows in the meantime. That is the trade: optimise for the overwhelmingly likely case, fail loudly in the unlikely one, rather than paying schema cost on every row forever against a case that probably never happens.

**One more assertion for the producer that operates extraction (outside this repository) to be aware of, though not to implement.** The assertion is ours, at load time, on data the producer has already landed. It adds nothing to its extraction work.

## Alternatives rejected

**Per-field `<field>_currency`, as the steering read.** The most defensive shape, and it removes the assumption outright by storing what the source gave us. Rejected on cost and coherence: roughly nine extra columns across the vendor facts, nearly all duplicating each other on every row; and applied only to vendor it makes the model non-uniform, so the derived cross-channel fact needs a convention anyway. Applied to AMC as well for uniformity, it fabricates columns from a single source value, which would make the canonical model describe structure Amazon does not have.

**Shared column, assumption left implicit.** The status quo in `canonical-model.md`, and the cheapest option. Rejected because it destroys the evidence needed to detect its own failure. Having dropped the other two currency values, no downstream check can tell whether they ever disagreed — the same class of problem as AMC suppression without the discriminator, where the information is gone before anything can look at it.

**Assert, but warn and coerce instead of quarantining.** Rejected because a warning on a money column is a number we would not stand behind being served anyway. If the currencies disagree, no single `currency_code` is correct for the row, so there is nothing honest to serve.

**Store per-field currency only where the source provides it, shared elsewhere.** Rejected as the worst of both: the model becomes non-uniform *and* the reader has to know which convention applies per fact.

## Review trigger

Reopen if the agreement assertion fires on real vendor data, which means within-row divergence is real and the schema must migrate to per-field currency columns. Also reopen if a new source arrives whose money shape is neither of these two — a source with currency at the document level and amounts at the row level would need its own answer.
