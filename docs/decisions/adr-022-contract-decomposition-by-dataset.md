# ADR-022: Contracts decompose by dataset and grain, not by analysis topic

Date: 2026-08-19
Status: Accepted

> **Proposed amendment, 2026-10-06.** [ADR-109](adr-109-null-fill-non-additive-measures-in-multi-writer-facts.md) (Proposed) would return DSP distinct reach to `fct_ad_exposure` as its own writer, `amc-dsp-reach`, with non-additive union columns null-filled. Until it's accepted, the 2026-08-24 amendment below stands.

> **Audience-performance amendment, 2026-10-11.** The segment tables were transcribed on 2026-10-08, so `amc-audience-performance` is now written (pack 8, `fct_segment_delivery`, [ADR-103](adr-103-model-audience-segments-as-a-source-namespaced-overlapping-dimension.md)); the consequence below that it isn't is superseded. `amc-organic-vs-ad` stays unwritten.
> **Producer-pack amendment, 2026-08-24.** Dataset decomposition remains current. `amc-dsp-impressions` now contributes impressions and fee-inclusive spend only; `spend_media_only` and DSP distinct reach were removed until their source semantics are safe. The need for separate impression, click and view datasets is unchanged.

## Context

[ADR-003](adr-003-canonical-model-decomposition.md) lists twelve contracts as a sanity check that its domains are source-independent. That check succeeded and the domain decomposition stands. But the roster it used names contracts after **analysis topics** — `amc-new-to-brand`, `amc-time-to-convert`, `amc-path-to-purchase`, `amc-customer-loyalty` — because it was drawn from the AMC playbook and instructional-query topics recorded in [sources/amc](../sources/amc.md) §8.

The `data-contract-definition` steering says a contract is **one source dataset at one grain producing one curated table**. An analysis topic is none of those three things. New-to-brand is not a dataset and not a grain; it is nine columns on `amazon_attributed_events_by_conversion_time` that [canonical-model](../canonical-model.md) §4 already assigns to `fct_attributed_conversion`. Writing `amc-new-to-brand` as its own contract would mean two contracts reading the same table at the same grain writing the same fact, which is exactly what the rule forbids.

**Three consequences of the topic-based roster, all discovered by trying to write the contracts:**

**It omits datasets the model depends on.** No DSP contract appears in the twelve, yet `fct_ad_exposure.spend_media_only` and `viewable_impressions` have no source other than DSP tables. The omission is invisible in a topic list because "DSP delivery" is not an analysis topic — it is where half the delivery data lives.

**It hides that one fact needs several datasets.** `dsp_impressions` has **no `clicks` column** and `dsp_clicks` has **no `impressions` column** [verified]. DSP delivery is therefore irreducibly three datasets — impressions and spend, clicks, views — at one grain. A topic called "reach and frequency" conceals that entirely.

**It under-counts the vendor side.** The vendor steering requires every non-real-time report to split into its aggregate and by-ASIN arrays, "never one". Two vendor contracts in the roster cover the by-ASIN halves of two reports, so the catalogue grain of both is missing — and [canonical-model](../canonical-model.md) §6 already marks `salesAggregate[]` as "a different fact".

A retired implementation ledger referred to numbered contracts even though no document numbers them. **No document numbers the contracts**, and the former digits referred to nothing.

## Decision

**A contract is named for the dataset it reads and the grain it produces, never for a question it helps answer.** Analysis topics are properties of *metrics* and *tools*, which are declared in `contracts/shared/metric-dictionary.yaml` and [answerable-questions](../answerable-questions.md). One dataset can serve many topics, and a topic can need several datasets; neither is a contract boundary.

**The ADR-003 roster is superseded as a roster.** Its domain decomposition and its four-mechanism extension model are unaffected — only the twelve names are replaced. The retired contract numbering is withdrawn as meaningless.

**The set becomes what the sources actually contain**, recorded in [canonical-model](../canonical-model.md) §4a as the contract-to-fact map.

**A canonical column has exactly one of four provenances**, and the contract says which:

| Provenance | Meaning | Example |
|---|---|---|
| source column | read from the dataset | `impressions` |
| derived | computed by the extraction SQL from named columns | `reach_users` from `user_id` |
| extraction option | the platform supplies it because the workflow asked | the three suppression columns |
| **literal** | stamped during mapping; no source exists | `channel_key = 'amazon_dsp'` |

Literal is new here. DSP tables carry **no `ad_product_type` column at all** — AMC's convention is that a NULL `ad_product_type` on the attributed-events tables *means* DSP — so `channel_key` on a DSP contract cannot be mapped from anything. It is knowledge we hold, and declaring it as a literal makes that visible instead of leaving the pipeline to invent it.

### DSP marketplace resolves through `campaign_country`

`fct_ad_exposure`'s grain includes `marketplace_key`, and **no DSP table carries `marketplace_name`** [verified]. `sponsored_ads_traffic` does, which is why the first AMC contract was scoped to it. The candidates on the DSP side:

| Column | On | Why not / why |
|---|---|---|
| `segment_marketplace_id` | impressions only | Describes the *segment*, not the event. Semantics not given by the page **[unverified]** |
| `iso_country_code` | impressions, clicks | Where the **viewer** was. Absent from `dsp_views` entirely |
| `postal_code`, `city_name` | impressions, clicks | `HIGH` — cannot be compared to a literal anywhere, including a join |
| **`campaign_country`** | **all three** | The country the **campaign** targets. `LOW` |

`campaign_country` is chosen because it is the attribute of the *spend*: money is spent by a campaign, and a campaign is set up against a marketplace. Viewer country answers a different question and would attribute a UK viewer's impression on a US campaign to the wrong marketplace. It is also the only candidate present on all three DSP tables, so the three contracts resolve marketplace identically.

**It resolves through the conformed dimension, not directly.** `dim_marketplace` already carries `country_code`, so `campaign_country` is looked up there exactly as vendor `asin` is looked up in `dim_product`. Absorbing a per-source natural key is what a conformed dimension is for. **An unresolvable lookup quarantines the partition** — the same failure behaviour as [ADR-021](adr-021-money-column-shape.md)'s currency assertion, and for the same reason: a row whose marketplace cannot be established has no honest value to write.

Whether `campaign_country` is populated for every DSP campaign is **[unverified]**; the page gives no note. That is why it is asserted at load rather than assumed, and the probe that would settle it is recorded as deferred work.

## Consequences

- **The contract set is larger and each contract is smaller.** More files, each mechanically checkable, and no contract's scope depends on how someone frames a question.
- **A topic that needs several datasets is assembled in the semantic layer**, which is where `UNION ALL` at a shared aggregate grain already belongs. The alternative — a contract that reads three tables — would hide a three-way merge inside one field-mapping table.
- **`fct_ad_exposure` is written by four contracts**, one per dataset. They contribute different measures at the same grain, so the load is a merge on the grain rather than an append. [load-path](../capabilities/load-path/architecture.md) §5a owns that.
- **`amc-organic-vs-ad` is not written.** Its three `conversions_all` money columns carry `status: disputed`, so the gate refuses them, and it additionally requires a customer holding an AMC Paid Features subscription ([Q-07](../decision-coverage-registry.md#question-register-q-nn), reframed as a prerequisite rather than a funding decision). Recorded as deferred work rather than written and left failing.
- **`amc-audience-performance` is not written.** It needs the segment tables, which sit in `not_yet_transcribed` in the registry, and an allowlist gate refuses an untranscribed table by design.
- **Amazon's playbook SQL remains the reference for the extraction**, per [sources/amc](../sources/amc.md) §8. This decision changes how contracts are *bounded*, not where the SQL comes from.

## Alternatives rejected

**Keep the topic roster and let contracts overlap.** Rejected because it breaks the one-grain rule outright: two contracts writing the same fact from the same table at the same grain have no defined merge, and whichever ran last would win silently.

**One contract per fact.** Tidier to read, and wrong: `fct_ad_exposure` needs four datasets whose column sets genuinely differ, and a single contract would have to declare four source tables and hide the merge in its field mapping. The gate's registry lookup already resolves first-match-wins across `source_tables`, which would make a column silently resolve against the wrong table.

**One contract per fact per channel.** Closer, but it still merges impressions, clicks and views for DSP into one contract, and those are three datasets with three different grains — `dsp_views` is one row per *view event*, not per impression.

**Use `iso_country_code` for DSP marketplace.** Rejected on semantics and on coverage: it is the viewer's country rather than the campaign's, and it does not exist on `dsp_views`, so viewable impressions could not resolve a marketplace at all.

**Drop `marketplace_key` from the DSP contracts.** Rejected because it is in the fact's declared grain. A row missing a grain column is a grain violation, and the gate's `keys-are-mapped` rule catches it — correctly.

**Give DSP its own subtype fact without marketplace.** Available under ADR-003's mechanism 3, and rejected because it would fragment the one fact whose whole purpose is a conformed cross-channel spend total. `ad_spend` would then have to union two facts at different grains.

## Review trigger

- a probe shows `campaign_country` is sparsely populated or does not correspond to a marketplace, which would reopen the DSP marketplace resolution
- the paid-feature subscription is funded, which makes `amc-organic-vs-ad` writable once its money-scale dispute is settled
- the segment tables are transcribed, which unblocks audience contracts
- a new source arrives whose datasets do not map cleanly onto one grain each
