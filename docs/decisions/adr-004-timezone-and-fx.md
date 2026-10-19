# ADR-004 — Timezone alignment and FX policy

| | |
|---|---|
| **Status** | Accepted |
| **Date** | 2026-08-18 |
| **Decision** | **Align each fact to its source platform's business day and carry `source_timezone` on every fact — do not normalise to one global zone. Store money in native currency with a dated rate table, and convert only in the semantic layer.** Multi-marketplace is assumed in scope. |
| **Related** | [canonical-model](../canonical-model.md) §7 · [sources/amc](../sources/amc.md) §6 · [sources/vendor-central](../sources/vendor-central.md) §6 · [Q-13](../decision-coverage-registry.md#question-register-q-nn) |

## Context

Two structural decisions from [Q-13](../decision-coverage-registry.md#question-register-q-nn), items 5 and 6. Both are far cheaper to settle before curation is written than after, because both are irreversible once data is stored converted.

The customer may sell in more than one marketplace. That is not confirmed, so the model must accommodate it without paying much for the possibility.

**The governing constraint is that we receive pre-aggregated daily rows, not instants.** Vendor Central delivers a day bucket. AMC delivers a day bucket. Neither gives us the underlying events, so a day bucket cannot be decomposed and reassigned to a different zone. That single fact decides the timezone question.

## Decision

### Timezone — align to the source's business day, and say which one

Each fact is stored on the day boundary its source used, with a `source_timezone` column stating which. No global normalisation.

| Source | Day boundary | Configurable? |
|---|---|---|
| Vendor Central | **PST, always** — irrespective of the vendor's location or the store's local time zone | **no** |
| AMC | UTC by default; every table also carries advertiser-timezone variants and an `advertiser_timezone` column | yes, via `timeWindowTimeZone` |

> **Converting a pre-aggregated day bucket to another zone is fabrication, not conversion.** Reassigning a PST-bucketed day to UTC requires knowing how the day's activity was distributed within it, which is exactly the information the aggregation destroyed. Storing the source boundary and labelling it is the only honest option.

Vendor Central is immovable at PST, so any global target zone would require fabricating vendor days. That rules out global UTC and global Pacific alike.

**Multi-marketplace makes this sharper, not just broader.** Vendor reports are PST regardless of marketplace, so a UK marketplace's retail day is still bucketed to Pacific — diverging from the local business day by up to eight hours. `source_timezone` therefore is not bookkeeping; it is the only thing that makes a cross-marketplace figure interpretable.

**Consequence for joins:** weekly and coarser grains are safe. Day-grain joins between advertising and retail carry a known offset and must be labelled as approximate wherever surfaced.

#### The DST sub-decision, and its honest status

Amazon's documentation says **"PST"**, not "US/Pacific", and is **silent on daylight saving**. **[unverified]**

Default: interpret it as `America/Los_Angeles` — Pacific civil time, DST-observing — on the reasoning that retail reporting for a US-headquartered business almost certainly follows civil time rather than a fixed offset. **This is a working assumption, not a finding.**

The test that would settle it: compare a week boundary spanning a DST transition against the Vendor Central UI for the same period. Until run, do not encode a fixed `UTC-8` anywhere, because unwinding it later means restating history.

### FX — native currency, dated rates, convert late

| Rule | |
|---|---|
| Storage | **native currency only.** `currency_code` travels with every monetary column |
| Rates | a dated `fx_rate` table, grain currency-pair × date |
| Conversion | **semantic layer only, never at ingestion** |
| As-of basis | the rate **as of the fact's own date**, not query time |

Converting at ingestion is irreversible: it destroys the ability to restate when a published rate is corrected, and the ability to answer the same question on a different rate basis. This is the same principle as not storing ROAS — store what is measured, derive what is asked.

The as-of choice matters more than it looks. Using the rate as of the fact's date means a historical figure stays stable across re-queries. Using the current rate would make last quarter's revenue change every morning, which is indefensible in a reporting product.

**Neither AMC nor Vendor Central supplies FX**, so the rate table needs an external source. That is a new dependency this ADR creates rather than resolves — see consequences.

**Multi-marketplace is nearly free because the structure already exists.** `dim_marketplace` carries `country_code` and `default_currency_code`, every fact carries `marketplace_key` in its grain, and currency already travels with every amount. A single-marketplace customer pays essentially nothing: the rate table stays empty and no conversion is ever requested.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| **Normalise everything to UTC** | Requires fabricating vendor day boundaries from buckets that cannot be decomposed |
| **Normalise everything to Pacific** | Same fabrication in the other direction for AMC, and no help at all for non-US marketplaces, where Pacific is nobody's business day |
| **Store instants instead of buckets** | Not available. Neither source emits event-level data to us |
| **Convert currency at ingestion** | Irreversible. Cannot restate on rate correction, cannot re-answer on a different basis |
| **Single fixed budget rate** | Defensible for planning, wrong for reporting, and unreconstructible once applied |
| **Rate as of query time** | Historical figures would move daily |
| **Fixed `UTC-8` for vendor data** | Encodes an unverified DST position in stored data |
| **Defer FX until multi-marketplace is confirmed** | The expensive part is the *storage* decision, and that has to be right from the first load. Deferring the rate table is fine; deferring native-currency storage is not |

## Consequences

- **An FX rate source must be chosen and funded.** Options are a commercial rate API or customer-supplied rates. Recorded as an open item; not blocking until the second marketplace appears.
- The `fx_rate` table needs an explicit provenance and as-of contract of its own — which rate, published by whom, captured when.
- Every cross-marketplace rollup must declare that it converted and on which date's rate. Metrics that convert carry that in their definition, alongside `mixes_basis`.
- Day-grain advertising-to-retail comparisons are permanently approximate. The narration layer must be able to say so rather than presenting them as exact.
- `source_timezone` is mandatory on every fact from the first load. Adding it later means backfilling a column whose correct value may no longer be knowable.
- The DST interpretation is an unverified assumption sitting under stored data. It is cheap now and expensive after a year of history.

## Review trigger

- the DST comparison against the Vendor Central UI is run, which either confirms or overturns `America/Los_Angeles`
- a second marketplace is onboarded, making the FX source urgent rather than theoretical
- either source begins supplying event-level rather than pre-aggregated data, which would reopen the timezone decision entirely
- a customer requires a fixed budget rate for planning, which would mean a second rate basis rather than a replacement
