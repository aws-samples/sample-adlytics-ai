# Source — Amazon Marketing Cloud (AMC)

| | |
|---|---|
| **Level** | L2 — source reference |
| **Status** | Schema research plus accepted live structural evidence through 2026-08-21; eight governed AMC pack executions and later structural acceptance remain pending ([validation results](../../validation/RESULTS.md#pending-acceptance--2026-08-24)) |
| **Researched** | 2026-08-17 |
| **Owns** | every fact about AMC. No other doc in this repo restates these; they link here. |
| **Depended on by** | [data-layer](../capabilities/data-layer/README.md) · [architecture](../architecture.md) · six AMC data contracts and governed query packs |

## 1. What this is

AMC is Amazon's privacy-safe advertising clean room. It holds advertising event-level signals for a single advertiser and exposes them through SQL queries that must aggregate before returning results. It is the source for everything we know about advertising *exposure and attribution*.

It is **not** a source of retail truth. See [vendor-central](vendor-central.md) for that, and §9 here for the boundary.

## 2. Provenance

Compiled by research sub-agents on 2026-08-17. The docs site is a client-rendered SPA, so plain fetches return a JS shell; the authoritative Markdown was retrieved from the docs content CDN.

```
https://d3a0d0y2hgofx6.cloudfront.net/en-us/guides/amazon-marketing-cloud/<path>.md
sitemap: https://d3a0d0y2hgofx6.cloudfront.net/sitemap.xml   (~1,000 URLs)
```

The `en-us/` prefix is required — omitting it 404s. Sitemap `lastmod` is a uniform build stamp (all entries `2026-08-13`), so it is useless as a content-change signal. This matters for drift detection; see §10.

**Confidence markers used below:** **[verified]** page content was fetched and read · **[snippet]** search snippet only · **[unverified]** could not confirm.

**Dated connectivity evidence makes a new `[unverified]` claim settleable rather than blocked.** Discovery reached the configured AMC instance and refreshed its credentials on 2026-08-21. That historical result does not establish current reachability: re-check authentication before submitting a new probe, then record the execution ID and poll it as [validation/README.md](../../validation/README.md) requires. Results and the account are recorded in [validation/RESULTS.md](../../validation/RESULTS.md).

What the instance can and cannot answer is the distinction that matters: a **structural** question — does this column exist, is it outputtable, what does it contain — is as good there as anywhere, because schema and threshold classification do not vary by advertiser. A **volumetric** one is worthless there, and [ADR-019](../decisions/adr-019-grain-viability-is-measured.md) settles that by measuring grain viability per customer from their own loads instead.

Not independently re-verified by a human. Before the ten contracts are committed, the CJA and CLTV playbook SQL should be read directly, because the claim that they correctly encode cost divisors and brand-halo rules is load-bearing.

## 3. Available data

Every table is flat — no nesting. Each column carries a name, data type, column type (`METRIC`/`DIMENSION`), and an **aggregation threshold**. `CREATE TABLE` and materialised tables are not supported; AMC is query-only. **[verified]**

| Family | Tables | Notes |
|---|---|---|
| **DSP traffic** | `dsp_impressions` (**108** cols), `dsp_clicks` (107), `dsp_views` (76), `dsp_video_events_feed` (15 documented) | `dsp_clicks` is a subset of impressions **by row, not by column** — 95 shared columns agree exactly, but it adds a `click_*` family and drops `total_cost`, `impression_cost` and three fee columns, so **DSP spend is not reachable from it**. `dsp_views` `event_type` ∈ `MEASURABLE_IMP`, `VIEW`, `UNMEASURABLE_IMP`, `SYNTHETIC_VIEW` — see the dispute in §10. `dsp_video_events_feed` documents **only its 15 video columns** and inherits the whole `dsp_impressions` surface |
| **DSP × segments** | `dsp_impressions_by_matched_segments`, `dsp_impressions_by_user_segments` | the `dsp_impressions` columns plus four `behavior_segment_*` columns; **each impression appears once per segment**, so a sum of impressions or cost over these tables counts an impression once per segment. `behavior_segment_matched` is always 1 on the matched table and 1 or 0 on the user table. Docs warn these time out over extended periods. **[verified]** 2026-10-08, transcribed into the registry |
| **Sponsored ads** | `sponsored_ads_traffic` (77 cols) | one row per impression, one per click. Filter on `ad_product_type` |
| **Conversions** | `conversions` (30 cols), `conversions_with_relevance` | ad-attributed = traffic event within the prior 28 days. `_with_relevance` repeats a conversion per relevant campaign — this is the multi-touch table |
| **Attribution** | `amazon_attributed_events_by_conversion_time`, `amazon_attributed_events_by_traffic_time` (240 cols each) | same schema, different event-selection semantics — see §7 |
| **Amazon Live** | `amazon_live_traffic` | carousel-ad grain |

Every column of the DSP, sponsored-ads, conversion, attributed-events and ARP tables in the
registry matched its page on name, type, kind and threshold when the pages were re-read on
2026-10-08 [verified]; the registry records this as `columns_rechecked`.

**`campaign_country`.** All three DSP pages document it as a LOW `STRING` taken from the
campaign-level country setting, with the example value `'US'` [verified 2026-10-08]. On
`dsp_impressions` it resolves on the STANDARD demo instance and is absent from the Sandbox schema
(§8, live dialect facts; §10). Its **observed representation and population remain pending**: the
2026-08-26 run has no accepted conclusion in
[validation results](../../validation/RESULTS.md#campaign_country-observed-representation--run-2026-08-26),
so neither this document nor the registry records an observed value.

### Deduplicated temporal attribution — verified structure

[verified 2026-08-29] Recovery-first Sandbox and Standard probes kept `conversion_id` and `user_id`
inside AMC, joined unique purchase conversions to DSP impressions, DSP clicks, and sponsored
traffic, and emitted aggregates only. A 14-day traffic extension plus actual UTC event timestamps
supports chronological first-touch, last-touch, and equal linear credit. Each model reconciled to
the same matched conversion total with aggregation controls on and no suppressed result. Exact
executions and totals are owned by [validation results](../../validation/RESULTS.md#deduplicated-temporal-attribution-is-structurally-viable--runs-2026-08-29).

The AMC dialect accepts `ROW_NUMBER` partitioned by a sensitive conversion identity and accepts
`COUNT(column) OVER (...)`; it rejects windowed `COUNT(*)` because a COUNT window expression must
have one value expression. Sensitive identifiers never appeared in final output.

[verified 2026-08-29] Currency-scoped revenue allocation also reconciled for first, last, and
linear models on Sandbox and Standard. The unique `conversions` source exposes
`total_product_sales`, so this evidence is halo-inclusive. It does not establish the
advertised-products-only `product_sales` numerator used by the existing default ROAS.

The temporal query was independently derived from Amazon's public off-Amazon campaign-measurement
pattern, which joins conversions to traffic by user, orders qualifying traffic by age, and retains
one aggregate attribution result. Content was rephrased for licensing compliance; the source is the
[Off-Amazon conversions playbook](https://advertising.amazon.com/API/docs/en-us/guides/amazon-marketing-cloud/playbooks/off-amazon-conversions).

### Attributed measures — verified column names

On `amazon_attributed_events_by_conversion_time` and `_by_traffic_time` (identical schema). All **[verified]** 2026-08-17, all threshold `NONE`. Each also has `_clicks` and `_views` siblings splitting click- from view-attributed.

| Concern | Promoted ASINs | Brand halo | Both |
|---|---|---|---|
| Revenue | `product_sales` | `brand_halo_product_sales` | `total_product_sales` |
| Units | `units_sold` | `brand_halo_units_sold` | `total_units_sold` |
| Purchases | `purchases` | `brand_halo_purchases` | `total_purchases` |
| NTB purchases | `new_to_brand_purchases` | `new_to_brand_brand_halo_purchases` | `new_to_brand_total_purchases` |
| NTB revenue | `new_to_brand_product_sales` | `new_to_brand_brand_halo_product_sales` | `new_to_brand_total_product_sales` |
| NTB units | `new_to_brand_units_sold` | `new_to_brand_brand_halo_units_sold` | `new_to_brand_total_units_sold` |

Documented identities: `total_product_sales = product_sales + brand_halo_product_sales`, and `combined_sales = total_product_sales + off_amazon_product_sales`.

`conversions` always contains `1`. `new_to_brand` is a `BOOLEAN` **dimension** (LOW), separate from the metric family. **NTB lookback is 12 months**, stated consistently across every NTB column.

> **Spend is not in this table.** `amazon_attributed_events_*` carries `click_cost`, `supply_cost`, `audience_fee`, `platform_fee` and `bid_price` but **no `total_cost`, no `impression_cost`, no `spend`**. ROAS cannot be computed inside it. Spend must come from `dsp_impressions` / `sponsored_ads_traffic` and be combined at a shared aggregate grain — Amazon's own playbook does this with `UNION ALL` and zero-filled columns. A row-level join instead double-counts spend across the one-to-many traffic-to-conversion relationship.

> **Column names do not follow a convention — pin them as literals.** `purchases` and `units_sold` are plural while `detail_page_view` and `add_to_cart` are singular; the view-attributed variant stacks to `detail_page_view_views`; halo NTB columns repeat the word brand (`new_to_brand_brand_halo_purchases`). `dsp_impressions` prefixes time columns `impression_*` where `sponsored_ads_traffic` uses `event_*`, and DSP's `line_item` mirrors sponsored ads' `ad_group` with identical values. Do not derive these names from a pattern.

### Paid features

Separate subscription datasets. **[verified]**

| Table | Gives us | Relevant to |
|---|---|---|
| `conversions_all` | `exposure_type` ∈ `ad-exposed` / `non-ad-exposed` / `pixel` | **the only route to organic-vs-ad-attributed** |
| `amazon_retail_purchases` (ARP) | **26** cols, **60 months** of retail purchases, incl. `asin_parent` and `asin_brand`. **No revenue column at all** — every ARP sales figure is `unit_price` × `purchase_units_sold`, derived rather than selected. Page is `datasources/amazon_retail_purchase_paid` (singular), and its schema table has **no Metric/Dimension column**, so no column here carries a `kind` | repurchase, loyalty, and the AMC↔Vendor reconciliation anchor |
| `audience_segments_<region>_inmarket` / `_lifestyle` (+ `_snapshot`), `segment_metadata` | Audience Segment Insights (ASI): audience segment membership, one table per region (`amer`, `apac`, `eu`) and category, each with a monthly `_snapshot` view taken the first Thursday of the month. Page is `datasources/audience_segment_membership_paid`. See "ASI segment marketplace" below | audience performance |
| `amazon_brand_store_page_views`, `amazon_brand_store_engagement_events` | store engagement | — |
| `amazon_pvc_*`, `amazon_your_garage_all`, `experian_vehicle_purchases` | other 1P/3P signals | not in scope |
| `ncs_cpg_insights_stream` | — | **discontinued** |

> **Commercial cliff.** From 2026-06-01, all Amazon 1P paid-feature signals — Amazon Insights (which carries `conversions_all` through Flexible Shopping Insights, and Audience Segment Insights), ARP, Prime Video Insights, Prime Video Channel Insights, Amazon Your Garage and Brand Store Insights — are queryable **at no cost through 2026-12-31** in every region where AMC paid features are available. Eligible advertisers still subscribe on the AMC Paid Features page; existing subscriptions auto-convert to $0. 3P paid features keep their fee. Three of our ten contracts depend on this. **[verified 2026-10-08]** ([paid features overview](https://advertising.amazon.com/API/docs/en-us/guides/amazon-marketing-cloud/datasources/paid_features_overview); the region wording changed from an earlier read that named NA, FE, AU and EU)

ARP eligibility requires Vendor Central or Seller Central accounts associated with the Ads accounts on the instance. ARP's scope is documented to match *Vendor Central → Reports → Retail Analytics → Sales → Distributor View = MANUFACTURING*, which makes it our only documented reconciliation anchor to retail. **[verified]**

**ASI segment marketplace.** **[verified]** 2026-10-08, from the
[segment membership page](https://advertising.amazon.com/API/docs/en-us/guides/amazon-marketing-cloud/datasources/audience_segment_membership_paid)
and the [off-Amazon conversions playbook](https://advertising.amazon.com/API/docs/en-us/guides/amazon-marketing-cloud/playbooks/off-amazon-conversions):

- The page sets three query rules: one region and one category table per query, a `segment_marketplace_id`
  filter on every query, and one snapshot per analysis when joining to traffic or conversions.
  The marketplace filter isn't enforced at compile time: the 2026-10-08 presence reads on the
  STANDARD demo instance carried no filter and succeeded
  ([results](../../validation/RESULTS.md#paid-feature-presence-and-forced-suppression--2026-10-08)).
  The rule still binds the packs; that a read without it succeeds is not permission.
- `segment_id` is unique only within `segment_marketplace_id`, so `segment_metadata` joins on both columns.
- `segment_marketplace_id` is `LONG` on the ASI tables and `INTEGER` on `dsp_impressions`.
- The page states no values. The playbook filters `audience_segments_amer_inmarket` on
  `segment_marketplace_id = 1` and labels it the US marketplace ID. That value is the governed
  `amc.segment_marketplace_id` alias for `amazon_us` in `contracts/shared/commerce-identities.yaml`. No
  other marketplace's value is documented.
- Live, on the STANDARD demo instance, 2026-10-09 (P-BO-4; validation-instance evidence)
  [verified; [results](../../validation/RESULTS.md#fence-structural-check-pack-14-at-acquisition-date-grain-p-bo-3-and-p-bo-4--2026-10-09)]:
  - Both `amer` snapshot tables returned segments under `segment_marketplace_id = 1`.
  - DSP `behavior_segment_id` (`dsp_impressions_by_matched_segments`) isn't the ASI `segment_id`.
    Too few IDs matched on either category table, and names appeared under different IDs. The two
    namespaces stay separate.
  - The in-market snapshot's membership held only `adUserId` that week. The lifestyle table's ID
    space wasn't read.

### Audience mirror tables

Every analytics table has a `_for_audiences` twin used only for audience creation. Distinct namespace — do not treat as the same asset. **[verified]**

### Not available

Demographics (age, marital status, income). Amazon Ad Server (`adserver_*`) tables — AAS was sunset Q4 2024. **[verified]**

Scope of "not available": no table carries a shopper's age, marital status or income as a column.
DSP segment membership is different. On the STANDARD demo instance, `dsp_impressions_by_user_segments`
held unsuppressed segments whose names denote an age band, a gender and a household-income band
(P-BO-3, run 2026-10-09 over September 2026) [verified, validation instance;
[results](../../validation/RESULTS.md#fence-structural-check-pack-14-at-acquisition-date-grain-p-bo-3-and-p-bo-4--2026-10-09)].
The design's reviewer check was the probing agent's first pass, pending a human reviewer's
amendment or acceptance.
Some of them are third-party provider segments, not Amazon's own [inference from the names].

## 4. Identifiers

The most consequential section for schema design. Threshold classes are explained in §5.

| Column | Class | Semantics |
|---|---|---|
| `user_id` | **VERY_HIGH** | Pseudonymous. **Mixes ID spaces** — `user_id_type` (LOW) ∈ `adUserId` \| `sisDeviceId` \| `adBrowserId` \| `NULL`. `NULL` when Amazon cannot resolve an identifier. Usable inside a CTE for `COUNT(DISTINCT …)`; **can never appear in output**. |
| `campaign_id` / `campaign_id_string` | LOW | **Use `campaign_id_string`.** On DSP both carry identical values (`LONG` and `STRING`) and 18-digit IDs do not survive JSON integer precision. On `sponsored_ads_traffic` they are **two different identifiers from two different systems** — `campaign_id` is the Ads API ID (`'123456789012345'`), `campaign_id_string` is the ads-console ID and is **not numeric** (`'A087654321ABC2Y3WXYZ'`). Both are populated, so joining on the wrong one drops rows silently instead of erroring. `conversions_with_relevance` explicitly recommends the string form. **[verified]** |
| `advertiser_id` | LOW | `LONG` |
| `advertiser`, `campaign` (**names**) | **LOW** | **Outputtable. Settled by live probe 2026-08-19** [verified]. A final `SELECT campaign_id_string, campaign, advertiser, SUM(impressions) … GROUP BY` ran to `SUCCEEDED` against AMC sandbox instance `SANDBOX_INSTANCE` **with aggregation controls on**, returning real advertiser and campaign names. An `INTERNAL` column cannot appear in a `GROUP BY`, so the query would have been rejected. This row previously recorded `INTERNAL` and never outputtable, researched 2026-08-17 — **that was wrong**, and seven data-source pages read 2026-08-19 already contradicted it. Names do not need an external metadata source; see §9 |
| `traffic_event_id` | **VERY_HIGH** | On the attributed-events tables, and the join target for `sponsored_ads_traffic.event_id` / `amazon_live_traffic.event_id`. Because it is VERY_HIGH the join must live in a CTE. Sponsored ads and Amazon Live only |
| `event_id` | **VERY_HIGH** | On `sponsored_ads_traffic`, `amazon_live_traffic` and ARP. On ARP many `event_id` values map to one `purchase_id` |
| `device_id`, `ip_address` | **VERY_HIGH** | On the DSP tables. Pseudonymous; CTE use only |
| `household_id` | **VERY_HIGH** | On the DSP tables, `sponsored_ads_traffic` and `amazon_live_traffic`. **Data only from 2026-04-10 onward, coverage varying by region** — a hard availability floor inside the 13-month window, so an absent household is not a household of one. Changes if a user moves household |
| `household_size` | **HIGH** | A `STRING` with buckets `1`, `2`, `3`, `4+` and `NULL`, so it will not cast to a number. Demographic in character, and queryable — the "no demographics" note below is about age, income and marital status only |
| `impression_id` | **VERY_HIGH** | On `dsp_views`, carrying the value the other DSP tables expose as `request_tag`. Same concept, different column name per table |
| `origin_session_id`, `purchase_session_id` | **VERY_HIGH** | ARP only. The add-to-cart session and the purchase session — a genuine confusable pair for funnel work |
| `ad_product_type` | LOW | ∈ `sponsored_products`, `sponsored_brands`, `sponsored_display`, `sponsored_television`. **`NULL` means DSP/display** in the attributed-events tables. Filtering on it is mandatory to avoid mixing display into sponsored-ads numbers. |
| `tracked_asin` | LOW | Conversion-side ASIN. **Includes brand-halo products, not just advertised ones.** Populated **only** when `event_category = purchase`. |
| `tracked_item` | LOW | **Polymorphic** — an ASIN, a brand store ID, a search keyword, or an off-Amazon conversion-definition ID depending on event type. Never join to an ASIN without filtering to purchase events first. |
| `creative_asin` | — | On `sponsored_ads_traffic` — the **advertised** ASIN, distinct from `tracked_asin`. |
| `conversion_id` | **VERY_HIGH** | Unique per row in `conversions`; **repeats** in `conversions_with_relevance`. Not outputtable. |
| `request_tag` | **VERY_HIGH** | Links impression/view/click/conversion, but docs explicitly warn **duplicates occur and it is not recommended as a join key**. Prefer `UNION ALL`, or `user_id` inside a CTE. |
| `asin` / `asin_parent` (ARP) | — | **The only AMC source of a parent ASIN.** |
| `no_3p_trackers` | NONE | boolean, third-party-tracking eligibility |

**Identity resolution for uploaded 1P data is deterministic only** — hashed PII (8 fields), MAID (IDFA/AAID), or partner ExternalIds. No cookie or IP resolution. Hashed values are dropped after resolution. `countryCode` (ISO 3166 alpha-2) is required or records are excluded. **[verified]**

**Not documented:** whether `user_id` is salted per instance, or whether it rotates over time. The docs never state stability-over-time. **Do not design anything assuming multi-year `user_id` continuity** — which matters because ARP's 60-month window plus 13-month ad data would otherwise tempt long-window loyalty analysis that silently depends on it. **[unverified]**

## 5. Constraints — the aggregation thresholds

This is the constraint that reshapes the whole data layer. **[verified]**, re-read 2026-10-08 ([aggregation threshold](https://advertising.amazon.com/API/docs/en-us/guides/amazon-marketing-cloud/aggregation-threshold)). That page's own MEDIUM examples are `bid_price` and `click_cost`; the other examples below come from the data-source pages.

| Class | Min distinct users | Literal filters allowed | Can appear in output | Examples |
|---|---|---|---|---|
| `NONE` | 1 | yes | yes | `impressions`, `clicks` |
| `LOW` | 2 | yes | yes | `campaign_id`, `ad_id`, `device_type` |
| `MEDIUM` | 100 | yes | yes | `bid_price`, `click_cost`, **`event_dt_utc`** |
| `HIGH` | 100 | **no** | yes | `postal_code`, `city_name` |
| `VERY_HIGH` | — | no | **never** | `user_id`, `conversion_id`, `request_tag`, `purchase_id` |
| `INTERNAL` | — | — | **never** | `marketplace_id` on ARP |

### Query restrictions that follow

- **No `SELECT *`** on any table. `SELECT * FROM DSP_IMPRESSIONS` is given as an explicitly prohibited example.
- Every query must contain **at least one aggregation**.
- Comparing a `HIGH`/`VERY_HIGH` column — **or anything derived from one** — to a literal **rejects the query**. This covers comparison expressions, join keys, and group-by keys. Literals injected through `CUSTOM_PARAMETER` count too; you cannot launder them.
- **Cross-execution differencing guard.** Before running, AMC checks the query against prior executions and rejects it if too many workflows have already run against the same data sources over the same or overlapping time windows. Repeatedly varying a filter to isolate a sensitive value is blocked. **Do not build a vary-the-filter-and-diff pipeline**, and consolidate into fewer, wider workflows.

### What happens to rows below threshold — two modes

**Component diagram — AMC sub-threshold row handling; AMC service-boundary viewpoint, component depth.** Everything below happens before any file reaches us, so the mode is chosen by the extraction options and cannot be corrected afterwards.

![Component diagram: AMC sub-threshold row handling. Inside the Amazon Marketing Cloud service boundary, a result row passes a privacy threshold. Rows at threshold emit unchanged; rows below threshold are silently nulled by default or nulled, re-aggregated, and marked when the discriminator option is set.](diagrams/amc-threshold.svg)

> **Takeaway.** The default destroys *knowledge*, not metrics. A sub-threshold row keeps its measures and loses the dimension that identified it, with no column saying so — so a NULL cannot be told from a genuinely absent value.

**Corrected 2026-08-21 by live probe** (`validation/probe_suppression_nulls.py`, sandbox `SANDBOX_INSTANCE`). This section previously said the default *drops* sub-threshold rows and their metrics vanish. It does not. Measured on `amazon_attributed_events_by_conversion_time` grouped by `campaign_id_string, postal_code` over a 14-day window:

| | Rows returned | Flag column | Merging |
|---|---|---|---|
| No discriminator | **28,332** | absent | none |
| Discriminator set | **22,075** | `values_removed` | identical-after-nulling rows merged |

More rows without the parameter, not fewer, and 1,944 of them carried non-zero `purchases`. Nothing was dropped in either mode. The discriminator buys the flag and the merge, not the survival of the metrics.

### Which column gets nulled — per column, against its own threshold [verified 2026-08-21]

The decisive result, because it settles whether a curated grain column can arrive NULL. Flagged rows cross-tabulated by `distinct_users_used` against whether `campaign_id_string` (LOW, bar of 2) survived:

| Distinct users | `campaign_id_string` | Rows |
|---|---|---|
| **1** | **NULL** | 20,813 |
| 2 | populated | 1,202 |
| 3 | populated | 45 |
| 4 | populated | 2 |
| 5 | populated | 1 |
| 100 | populated | 12 |

`postal_code` (HIGH, bar of 100) was NULL in every row. So **each column is nulled independently against its own threshold** — a one-user row loses even its LOW columns, while a two-user row keeps them and still loses its HIGH ones.

**Consequence for this repository.** Every AMC contract maps `campaign_id_string` into `campaign_key`, `event_date_utc` into `date_key` and a marketplace column into `marketplace_key` — all LOW, all in the grain, all declared NOT NULL. A one-user row nulls all three, and `date_key` with `marketplace_key` are the Iceberg partition keys, so such a row cannot be placed in a partition at all. Recorded as [G-31](../decision-coverage-registry.md#gap-register-g-nn).

### `filteredReasonColumn` — the observed format differs from the documented one [verified 2026-08-21]

Actual values from the probe:

```
1 sandbox.amazon_attributed_events_by_conversion_time users < 100 required
2 sandbox.amazon_attributed_events_by_conversion_time users < 100 required
```

No quotes around the data source, a `sandbox.` prefix on this instance, and no `(path i of n)` segment. One flagged row carried an empty reason and another the count without the `< N required` suffix, so the format is not reliably parseable — do not build a parser against it.

**Amended 2026-10-08.** The [forced-suppression runs](../../validation/RESULTS.md#paid-feature-presence-and-forced-suppression--2026-10-08) (`dsp_impressions`, every grouped source column LOW so the bar was 2, the table read once per query, Sandbox and STANDARD) reproduced the unquoted form and the `sandbox.` prefix: every suppressed row read `<users> [sandbox.]dsp_impressions users < 2 required`, with `<users>` equal to the row's user count [verified]. They did **not** reproduce the empty or the truncated reason, and they didn't exercise what this probe ran: attributed events, a HIGH key, or the `values_removed` option name. Nor did either probe read a source more than once or produce an effective or equivalent count type. So the anomalies are unexplained rather than gone, and the rule stands: no production or runtime code may parse the reason, and a search of `src/` on 2026-10-08 found none that does. The one exception is the fixture conformance check, which holds a fixture's suppressed rows to the 2026-10-08 form. That tests our fixtures against what was observed in that scope, not a claim about what AMC always writes.

**What does survive is the part [G-28](../decision-coverage-registry.md#gap-register-g-nn) rests on: the value names its own data source.** A reason therefore belongs to one dataset, never to a row that several datasets assembled.

### Three optional output columns

Setting any of these **changes the output schema**, so they must be declared in every contract. **[verified]**

| Parameter | Adds | Note |
|---|---|---|
| `filteredMetricsDiscriminatorColumn` | a boolean column, named by you | `TRUE` = values were removed. Also changes row semantics (see above) |
| `distinctUserCountColumn` | the distinct-user count AMC actually used | **Capped** at 2 or 100 depending on the strictest output column; may differ from `COUNT(DISTINCT user_id)` |
| `filteredReasonColumn` | why the row was filtered | Format: `<count> '<data source>' (path <i> of <n>) <count type> < <required> required`, e.g. `97 'dsp_impressions' users < 100 required`. `NULL` when nothing was filtered |

**Docs against live, re-read 2026-10-08** ([create workflow](https://advertising.amazon.com/API/docs/en-us/guides/amazon-marketing-cloud/reporting/create-workflow), [aggregation threshold](https://advertising.amazon.com/API/docs/en-us/guides/amazon-marketing-cloud/aggregation-threshold); live from [forced suppression, 2026-10-08](../../validation/RESULTS.md#paid-feature-presence-and-forced-suppression--2026-10-08)):

- Agree: the user count is capped at 2 or 100 by the strictest output column, and the page warns it may differ from a plain distinct count. Live, the count read 2 on every unsuppressed row of a LOW-only `dsp_impressions` grouping on both instance types [verified]. The reason column is `NULL` when nothing was filtered; live, it was blank on unsuppressed rows [verified].
- Live only, the same runs, which read `dsp_impressions` once per query with every grouped source column LOW, on both instance types [verified]: with the option names every governed pack sets (`rows_suppressed`, `suppression_reason`, `distinct_user_count`), the three columns followed every selected column in the order `distinct_user_count`, `suppression_reason`, `rows_suppressed`, which isn't the order the request named them in. The create-workflow page states no order [verified 2026-10-08], and whether other option names change it is [unverified]. Every `time_window_start` and `time_window_end` cell read `YYYY-MM-DDTHH:MM:SS.000Z`. A suppressed row's user count equalled the count in its reason, its source-column keys were `NULL`, and its measures and CTE-derived labels survived.
- Disagree, recorded in §10: the reason text. The create-workflow page documents `<count> '<data source>' (path <i> of <n>) <count type> < <required> required`, adding the path segment only when a query uses the same data source more than once, with a count type of standard, effective or equivalent [verified 2026-10-08]. In the same runs every suppressed row read `<users> dsp_impressions users < 2 required` (a Sandbox prefixes `sandbox.`): no quotes, no path segment, count type `users` [verified]. The 2026-08-21 probe saw the same unquoted form plus an empty and a truncated reason (amended above). No run has read a source twice, reached a MEDIUM or HIGH bar on `dsp_impressions`, or produced an effective or equivalent count, so this is an observation in one scope, not a settled format.
- The registry's `suppression_output` block is the machine-readable form of the observed order, reason form and timestamp form, scoped to these runs. The fixture generator and its conformance check read it.
- Disagree, recorded in §10: the create-workflow page says the default **filters sub-threshold rows out completely**, while the aggregation-threshold page's postal-code example returns rows with the dimension `NULL`, and the 2026-08-21 Sandbox probe above dropped nothing.
- Disagree, recorded in §10: the create-workflow page says the discriminator **re-aggregates rows that match after nulling**, and the 2026-08-21 probe merged them, but the 2026-10-08 forced-suppression runs returned one suppressed row per withheld cell on both instance types.
- Live, 2026-10-09, two multi-source Sandbox queries: the packs 5–7 fence structural check (structural check, not the exact pack text) and pack 14 at acquisition-date grain ([results](../../validation/RESULTS.md#fence-structural-check-pack-14-at-acquisition-date-grain-p-bo-3-and-p-bo-4--2026-10-09)) [verified]:
  - The reason took the documented form, with a quoted source and a `(path <i> of <n>)` segment. The count type was still `users`.
  - **Disagree, recorded in §10:** on both texts, the bar applied **per data-source path**. A row holding two or more distinct users was withheld when fewer than two reached it through one path. On pack 14 those reasons named a purchase-side source, `conversions`; on the fence structural check (structural check, not the exact pack text), a group row with two or more shoppers and two or more purchasers was withheld through a touch-side path, `sponsored_ads_traffic`. That fence row falls outside criterion 7's {0, 1} band, so criterion 7's pack 5 half stays inconclusive. The [aggregation-threshold page](https://advertising.amazon.com/API/docs/en-us/guides/amazon-marketing-cloud/aggregation-threshold), re-read 2026-10-08, words the LOW rule per row.
  - On suppressed rows, CTE labels derived from source dimension columns were `NULL`, as were purchase-side sums and every currency column. A label derived from impression counts survived, and so did distinct counts and exposure-side sums.
  - These observations scope, and don't contradict, the single-source shape above.

**Design consequence:** any contract whose totals must reconcile *must* set the discriminator, and the curated schema must carry all three columns. Without them you cannot distinguish zero from suppressed. And because re-aggregation merges rows, a declared primary key can legitimately collapse — either include the flag in the key or document that the key is unique only among unfiltered rows.

### Non-additive distinct reach under suppression — pending accepted live validation

Accepted live validation of distinct reach under suppression is pending. Until that evidence is read,
the governed reach pack and local load assertion fail closed when any `rows_suppressed` value is
true. This is local capability policy, not an empirical AMC finding; status is tracked in
[validation results](../../validation/RESULTS.md#pending-acceptance--2026-08-24).

P-BO-1 step A (Sandbox, 2026-10-08) found every unsuppressed `sponsored_ads_traffic` reach row equal
to its controls-off reference at hour grain (the Q1r/Q2r re-run), but no row suppressed, so suppressed-row
nulling and exactness under suppression stay **unexercised** (AC-P.2: inconclusive, not pass)
[verified; [results](../../validation/RESULTS.md#business-outcome-probes-p-bo-1-step-a-and-p-bo-2--2026-10-08)].
The candidate pack 5, 6, 7 and 14 texts returned a derived-label bar of 2 on every unsuppressed
row (criterion 11; pack 14 on the Sandbox, the others on the STANDARD demo instance), so CTE-derived
labels kept the LOW bar there [verified]. None of those queries suppressed a row, so exactness under
suppression stays unexercised.

On 2026-10-09 the Sandbox exercised it on two multi-source texts
([results](../../validation/RESULTS.md#fence-structural-check-pack-14-at-acquisition-date-grain-p-bo-3-and-p-bo-4--2026-10-09))
[verified]:

- Pack 14 with one added acquisition-date key: 0c exercised, 5c pass.
- The packs 5–7 fence without its `campaign_country` filter, a structural check and not the exact
  pack text: criterion 5 pass. AC-E1.5 stays pending Step B.

Surviving rows were exact in both. Criterion 7's pack 14 half failed (the bar applies per source
path, above), so packs 14–15 keep `discard_suppressed_rows`, with the quarterly-cohort alternative
on the degrade threshold (design §8) [inference: read on the acquisition-date text].

## 6. Time, money, currency

**Timezone.** UTC by default. **Not every table carries both representations** — corrected 2026-08-19 [verified]. The traffic tables, `conversions_with_relevance` and the attributed-events tables carry a UTC family alongside an advertiser-timezone family plus an `advertiser_timezone` column (e.g. `America/New_York`). But **`conversions`, `conversions_all`, `amazon_retail_purchases` and `amazon_live_traffic` are UTC-only** and carry no `advertiser_timezone` column at all, so code reaching for an advertiser-tz twin fails on those four. Query windows can be overridden with `timeWindowTimeZone`.

**Column prefixes vary by table and cannot be generalised.** `dsp_impressions` uses `impression_*`; `dsp_views` uses `event_*`; `dsp_clicks` carries **both families at once**; ARP uses `purchase_*`; the attributed-events tables carry four families (`conversion_event_*`, `traffic_event_*`, `click_*`, `impression_*`). The MEDIUM/LOW asymmetry below holds in every one of them.

> **Threshold asymmetry that costs rows.** `event_dt_utc` (full timestamp) is **MEDIUM** — 100 distinct users. `event_date_utc` / `event_hour_utc` / `event_day_utc` are **LOW** — 2 users. **Selecting a full timestamp in final output drops far more rows than selecting a date.** Prefer the date variant, and never mix UTC and advertiser-tz columns in one query.

**Money — the divisor is a property of the column, not of the table or the platform.** **[verified]** — every row below read from the data-source reference pages, 2026-08-17.

This is the single highest-risk area in the whole source. Getting a divisor wrong is a 3-to-8 order-of-magnitude error that produces plausible-looking numbers.

**Which column is spend**

| Table | Spend column | Type | Threshold | Divisor | Scope |
|---|---|---|---|---|---|
| `dsp_impressions` | **`total_cost`** | `DOUBLE` | NONE | **÷ 100,000** | total delivered cost, **inclusive of all fees** |
| `dsp_impressions` | `impression_cost` | `LONG` | NONE | ÷ 100,000 | media cost. On a live probe 2026-08-21 it came back **exactly equal to `total_cost`**, which settles nothing: either the synthetic data carries no fees or the column includes them. **"Excludes fees" is [unverified]** — downgraded 2026-08-19: the page states fee inclusion for `total_cost` and says nothing about fee scope for this column |
| `sponsored_ads_traffic` | **`spend`** | `LONG` | NONE | **÷ 100,000,000** | total advertising cost. The **only** monetary column in the table |
| `amazon_attributed_events_*` | *(none)* | — | — | — | **no `total_cost`, no `spend`, no `impression_cost`** |

`total_cost` is the only cost column typed `DOUBLE`; every other is `LONG`. Cast accordingly.

**Full divisor table**

| Divisor | Unit | Columns |
|---|---|---|
| **÷ 100,000** | millicents | `total_cost`, `impression_cost`, `campaign_budget_amount`, `campaign_flight_budget_amount`, `line_item_budget_amount`, `click_cost` |
| **÷ 100,000,000** | microcents | `supply_cost`, `audience_fee`, `platform_fee`, `third_party_fees`, `managed_service_fee`, `ocm_fee`, `bid_price`, `spend` |
| **none** | local currency, `DECIMAL(38,4)` | `product_sales`, `brand_halo_product_sales`, `total_product_sales`, `combined_sales`, `off_amazon_product_sales`, `purchase_unit_price`, every `new_to_brand_*_sales` |

> **The millicent divisor is now [verified] by live probe, 2026-08-21.** `dsp_impressions` over a seven-day sandbox window returned `impressions = 281,004` and `total_cost = 304,502,058`. Under ÷ 100,000 that is **$3,045.02 and a CPM of $10.84**, which is an ordinary DSP CPM. Under ÷ 100,000,000 it is $3.05 and a CPM of **$0.011**, which no advertiser pays. Only one divisor produces a figure a human would recognise, and it is the documented one. This was the highest-consequence unverified claim in this document: a wrong divisor here is a 1000× error on `ad_spend` and on every ROAS built from it, and it would have looked entirely plausible.
>
> **The microcent half is still [unverified].** The probe that would have settled it — reading the fee columns beside the cost columns and checking that `total_cost − impression_cost` balances against the fees — was `REJECTED` on 2026-08-21 and returned no `statusMessage`. So the *asymmetry* is half confirmed: millicents is verified for the cost columns, microcents remains documented-but-unprobed for the fees, `bid_price` and `spend`.
>
> **A media-only spend field has two prerequisites, not one, and neither is satisfied.** It would map `impression_cost`, so its fee scope must first be verified on a real advertiser instance; the sandbox returned `impression_cost` exactly equal to `total_cost`, which cannot distinguish media cost from synthetic data with no fees.
>
> The second prerequisite is structural. Sponsored ads publishes no equivalent, so any media-only metric must require `channel_key: DSP`; otherwise a cross-channel sum reports DSP-only cost under an all-channel name. The current `amc-dsp-impressions` contract and governed producer pack therefore omit `impression_cost`, and no `spend_media_only` metric is declared. Both prerequisites must hold before either returns.

> **The trap is inside `dsp_impressions`, not between platforms.** One table mixes both scales: `total_cost` and `impression_cost` are millicents while every fee column and `bid_price` are microcents. A divisor keyed on table or platform makes every DSP fee figure wrong by exactly 1000×, in the direction that makes fees look negligible — and fee-inclusive ROAS still reconciles against `total_cost`, so the decomposition is garbage while the headline looks right.

> **Never divide attributed sales.** `product_sales` and its family are `DECIMAL(38,4)` already in local currency (example value `'12.99'`). Costs in the *same* table are scaled integers. Applying a cost divisor to revenue is a 5-to-8 order-of-magnitude error in the numerator of every ROAS.

Amazon's own playbook SQL confirms the asymmetry directly, unioning the two platforms: `spend / 100000000 as cost … from sponsored_ads_traffic` against `total_cost / 100000 as cost … from dsp_impressions`. The AMC SQL function reference carries the same note inline: spend is reported as microcents, divide by 100,000,000.

**`winning_bid_cost` — the documentation contradicts itself. Do not model it. [unverified]**

It is **absent from the `dsp_impressions` and attributed-events schema pages**, yet `playbooks/media_performance_optimization.md` selects it from `dsp_impressions` and from `amazon_attributed_events_by_traffic_time` (dividing by 1e5) [verified 2026-10-08]. An earlier read of `aggregation-threshold.md` named it as a MEDIUM example; on 2026-10-08 that page no longer mentions it, so no page now states its threshold. The docs give no way to tell which page is stale. Resolving this needs a probe against a live instance.

Normalise at ingestion and record the scale factor applied.

**Currency.** DSP tables have `currency_iso_code` + `currency_name`; conversions have `purchase_currency`; ARP has `currency_code`. **No FX conversion is provided.** Never store a bare amount.

**Multi-marketplace.** The attributed-events tables carry **both** `traffic_event_marketplace_name` and `conversion_event_marketplace_name` — traffic and conversion can occur in different marketplaces on the same row. ARP has `marketplace_id` (**INTERNAL**, unusable) and `marketplace_name` (usable).

## 7. Freshness and restatement

| Property | Value |
|---|---|
| Retention | **13 months rolling.** Backfilled at instance creation; older records age out |
| ARP retention | **60 months** — Amazon explicitly warns that failing to account for the mismatched window against other AMC datasets leads to inaccurate analysis |
| Data availability SLA | **48 hours.** Recommended practice: query a window ending 48h before today |
| Sponsored Brands traffic | only from **2022-12-02**. Keywords/search terms from instance creation or 13 months ago, whichever is sooner |
| Young instances | typically have data from 0–28 days before creation |

**Restatement is documented, not incidental.** **[verified]**

| Table | Event selection | Restatement |
|---|---|---|
| `amazon_attributed_events_by_traffic_time` | traffic events in window; conversions up to **30 days after** | Conversion range is **automatically extended 30 days past the window end**, and the docs state *the output of a workflow using this table may change over time* |
| `amazon_attributed_events_by_conversion_time` | conversions in window; traffic up to **14 days before** | — |

The attribution window itself is campaign-determined and cannot be changed in AMC.

**Modeled conversions** are present in the attributed tables, and **grains below ad-line level are `NULL`** for them. To exclude, join on user/traffic identifiers or any sub-ad-line dimension. Note `user_id IS NULL` does **not** mean modeled.

Both ARP and the conversion tables restate: ARP is documented as retail-pipeline-sourced *including restatements*, and the CONV tables as advertising-pipeline-sourced and *subject to restatements and advertising augmentation*.

## 8. Access mechanics

Base `https://advertising-api.amazon.com`. Headers: `Authorization`, `Amazon-Advertising-API-ClientID`, `Amazon-Advertising-API-AdvertiserId`, `Amazon-Advertising-API-MarketplaceId`. **[verified]**

```
POST   /amc/reporting/{instanceId}/workflows                  saved workflow
POST   /amc/reporting/{instanceId}/workflowExecutions          run saved OR ad hoc
GET    /amc/reporting/{instanceId}/workflowExecutions/{id}
GET    /amc/reporting/{instanceId}/workflowExecutions/{id}/downloadUrls
DELETE /amc/reporting/{instanceId}/workflowExecutions/{id}      also clears queue
POST   /amc/reporting/{instanceId}/schedules
```

> ⚠️ **Instance-level AMC APIs were retired 2024-08-01 and return `410`.** After migrating, the connected S3 bucket policy must be updated or results stop arriving. **[verified]** — date corrected 2026-08-19 against the [AMC overview page](https://advertising.amazon.com/API/docs/en-us/guides/amazon-marketing-cloud/overview); this previously read 2026, which looks like the research year transcribed by mistake.

### Reaching an instance — the order is forced

All **[verified]** 2026-08-19 by working probes in `validation/`.

**Discovery is two-phase and cannot be collapsed.** `GET /amc/instances` rejects a request carrying no entity ID with `400 {"details":"entityId provided is null"}`. The entity ID comes from `GET /amc/accounts`, which needs no AMC headers:

```
GET /amc/accounts   ->  {"amcAccounts":[{"accountId":"ENTITY…","accountName":…,"marketplaceId":…}]}
GET /amc/instances  ->  with Amazon-Advertising-API-AdvertiserId set to that accountId
                        {"instances":[{"instanceId":…,"instanceType":"STANDARD"|"SANDBOX",…}]}
```

So `accountId` is what the `Amazon-Advertising-API-AdvertiserId` header wants, and `marketplaceId` arrives on the same object. `instanceType` is how you tell a sandbox from a production instance.

**`GET /amc/reporting/{instanceId}/workflowExecutions` requires a filter.** Unfiltered it returns `400 {"details":"Must provide either workflowId or minCreationTime."}`. `minCreationTime` takes a bare `yyyy-MM-ddTHH:mm:ss`. This is the call that recovers execution IDs after a client dies, and executions continue server-side regardless.

**`REJECTED` is a status, and §8's list above omits it.** Observed on an execution whose SQL did not compile. So the terminal set is at least `SUCCEEDED`, `FAILED`, `CANCELLED`, `REJECTED` — and a `REJECTED` execution can carry **no `statusMessage` at all**, which makes a submitted-then-rejected query hard to diagnose from the execution record alone.

### Sandbox-only request parameters

Both **[verified]** from the [sandbox guide](https://advertising.amazon.com/API/docs/en-us/guides/amazon-marketing-cloud/amc-sandbox), and both load-bearing for any validation query.

| Parameter | Effect |
|---|---|
| `requireSyntheticData` | **Must be `true`** on every sandbox execution and schedule |
| `disableAggregationControls` | Sandbox only. `false` simulates the advertiser experience; `true` returns event-level rows, which no advertiser instance permits |

> **A `200` on submission means accepted, not compiled.** Amazon documents that omitting `requireSyntheticData` can return `200 OK` and then fail to compile, so the submitting call proves nothing — poll `GET …/workflowExecutions/{id}` for a terminal status. For a probe testing whether a column may be selected, that poll *is* the result.

**Sandbox accepts non-aggregated SQL that an advertiser instance rejects.** So a query passing in sandbox is not evidence it is production-legal. Schema and semantics validate there; SQL admissibility does not.

**SQL must be a single line** — no newlines or tabs in the JSON `sqlQuery`.

**Output is CSV**, which matches our pipeline assumption. Two delivery paths: a presigned download URL, or a connected S3 bucket with this layout:

```
amc-<instanceName>-<hash>/workflow=<workflowId>/schedule=<daily|weekly|adhoc>/
    YYYY-MM-DD-<workflowId>-ver1.csv
    + a metadata folder containing the SQL that ran
```

The metadata folder gives free lineage — capture it. Ad hoc executions land under `transient_*` prefixes. One CSV per execution; no documented multi-part output.

**Quoting isn't RFC 4180.** In a presigned-URL result on the STANDARD demo instance, a double quote inside a quoted segment description was escaped as a backslash-quote (`\"`), not as a doubled quote, so a standard CSV reader split those rows into extra fields. A reader that treats the backslash as an escape parsed every row [verified 2026-10-09; [results](../../validation/RESULTS.md#fence-structural-check-pack-14-at-acquisition-date-grain-p-bo-3-and-p-bo-4--2026-10-09)]. Whether any docs page describes the quoting is [unverified]. Any loader of AMC free-text columns, such as names and descriptions, must read the backslash escape or fail closed on a row whose field count is wrong.

**What this repository does with it.** Every AMC CSV read in `src/` goes through one dialect, `src/adapters/amc_csv.py`: backslash escape, no doubled quote. Before parsing, it refuses with `CSV_ESCAPE_AMBIGUOUS`, naming the line and column, a quoted field holding RFC 4180's doubled quote, a backslash followed by anything but a quote or a backslash, and a quote outside a quoted field, because each reads as a different value under the two dialects. How AMC writes a literal backslash is [unverified], so `\\` is the only spelling accepted for one. The Finalizer rejects such a result (`CSV_ESCAPE_AMBIGUOUS`, a rejected registration); at load each contract of the delivery is blocked, nothing publishes, and the result counts in `IngestPartitions` `Action=blocked`, the BlockedPartitions alarm's series. The fixture generator writes the observed form, a quoted value with `\"` inside, and one generated campaign name carries it (`amc-campaign-dsp.csv`).

**Time window parameters.** `timeWindowType` ∈ `EXPLICIT` \| `MOST_RECENT_DAY` \| `MOST_RECENT_WEEK` \| `CURRENT_MONTH` \| `PREVIOUS_MONTH`. **Default is `MOST_RECENT_DAY`** — omitting it silently gives a one-day window. `timeWindowStart`/`End` required when `EXPLICIT`, format `yyyy-MM-dd'T'HH:mm:ss`. `timeWindowTimeZone` defaults to UTC.

**Limits.** Execution timeout 21600s (**6 hours**). Concurrency: **10 parallel per instance**, 200 per account (API); queue depth unlimited at account level. Executions are async and *often take at least 15 minutes*. `expireTime` ~24h after creation. Statuses `PENDING` → `RUNNING` → `SUCCEEDED` \| `FAILED` \| `CANCELLED` \| **`REJECTED`** — the last is undocumented in the status list and was observed on a query that failed to compile, sometimes with no `statusMessage` (see below). Rate limits exist and return `429`, but **AMC explicitly declines to publish numbers** and recommends token-bucket with exponential backoff and jitter rather than coding to a limit.

**Scheduling.** `aggregationPeriod` (`Daily`/`Weekly`), `aggregationStartDay`, `aggregationHourUtc`, `scheduleEnabled`. Results reach S3 **~6–7 hours after** the scheduled run time. Scheduled reports **omit invalid traffic**. A workflow referenced by a schedule cannot be deleted.

**No idempotency.** There is no idempotency key, and re-running is actively discouraged — subject to the differencing guard, and for `by_traffic_time` it will legitimately return different numbers.

### SQL dialect

**The docs never name an underlying engine** — no mention of Spark, Trino, Presto, Athena or Hive. It is presented as "AMC SQL" with a published BNF grammar. Do not assume engine-specific behaviour. **[verified]**

| Restriction | Detail |
|---|---|
| No `SELECT *` | on any table |
| No `ORDER BY` at query level | only inside a window spec |
| No `RIGHT JOIN` | use `LEFT JOIN` with tables swapped |
| No `GETDATE()` | use `CAST('today' AS DATE)` / `CAST('now' AS TIMESTAMP)` |
| **No mergeable distinct count** | The aggregate list on the function reference, re-read 2026-10-08, is `APPROX_COUNT_DISTINCT`, `AVG`, `COUNT`, `COLLECT_SORTED`, `MIN`, `MAX`, `SUM` [verified]. An earlier read found no `APPROX_COUNT_DISTINCT`; it is now documented as an approximate count with "a small margin of error", and no HLL or sketch type is documented. Reach stays exact `COUNT(DISTINCT user_id)`, computed inside AMC at every grain wanted, and is **non-additive**: there is no sketch to export and merge downstream |
| `RANK` quirk | misbehaves when referenced by alias in `HAVING`; wrap in a subquery. Live deduplication probes additionally established that windowed `COUNT` requires a value expression: use `COUNT(column) OVER (...)`, never `COUNT(*) OVER (...)` |

Useful functions: `APPROX_PERCENTILE`, `PERCENTILE`, `MEDIAN`, `SECONDS_BETWEEN(t1,t2)`, `DATE_TRUNC`, `EXTEND_TIME_WINDOW('<table>','P14D','P1D')`, `BUILT_IN_PARAMETER('TIME_WINDOW_START'|'TIME_WINDOW_END')`, `CUSTOM_PARAMETER('name')`, CTEs, `UNION ALL`, regex functions, `STR_TO_MAP`.

**`EXTEND_TIME_WINDOW` is query-scoped** [verified 2026-10-08, function reference]: once a CTE applies it to a table, every later reference to that table in the query reads the extended window too. A CTE that must stay inside the reporting period needs its own filter on `BUILT_IN_PARAMETER('TIME_WINDOW_START')` and `('TIME_WINDOW_END')`. All three arguments must be `STRING` (limitations page). `CUSTOM_PARAMETER` and `BUILT_IN_PARAMETER` are on the expressions page, not the function reference; `BUILT_IN_PARAMETER` supports only `TIME_WINDOW_START` and `TIME_WINDOW_END` [verified 2026-10-08].

**Live dialect facts, 2026-10-08** (P-BO-1 Q0 and P-BO-2 on the Sandbox and the STANDARD demo
instance; [validation results](../../validation/RESULTS.md#business-outcome-probes-p-bo-1-step-a-and-p-bo-2--2026-10-08)):

- `SECONDS_BETWEEN(t1, t2)` is **signed as t2 − t1** [verified]: +3600 for (earlier, later),
  −3600 for (later, earlier). The function reference says the same.
- A **unary minus is rejected** at compile time ("Unsupported scalar operation type
  MINUS_PREFIX") [verified]. Write an absolute gap as `ABS(SECONDS_BETWEEN(a, b))`; `ABS` is in
  the function reference and evaluates correctly.
- `CAST('YYYY-MM-DD HH:MM:SS' AS TIMESTAMP)` compiles and evaluates [verified].
- **Array expansion:** `EXPLODE(array)` in a derived table's `SELECT` yields one row per element
  and compiles on both instance types with controls on [verified]. `UNNEST` is not in the function
  reference or the grammar.
- With `disableAggregationControls = true`, the three suppression output columns are absent
  from the header [verified]. A `UNION ALL` branch carrying `NULL` in an integer column widens
  that column to a decimal (`1` reads `1.0000000000`) [verified].
- An **ad hoc** execution accepts `inputParameters` on its `workflow` object, with values in the
  execution's `parameterValues`; the supplied value is what `CUSTOM_PARAMETER` reads [verified,
  Sandbox]. The create-workflow page documents `inputParameters` on saved workflows; the ad hoc
  page shows only `sqlQuery` on the `workflow` object, so this ad hoc form is live evidence the
  docs don't state (read 2026-10-08).
- `dsp_impressions.campaign_country` is **absent from the Sandbox schema** [verified, Sandbox]:
  a one-column read, `COUNT(campaign_country)` from plain `dsp_impressions` with controls on, was
  `REJECTED` with "Column 'campaign_country' not found in any table"
  (`probe_bo_campaign_country_schema.py`). The same week's pack 5 text compiles and runs on
  STANDARD, so the column exists on that advertiser instance. The `dsp_impressions` page lists
  it without any Sandbox caveat, so the Sandbox gap is an open question (§10), not a settled
  scope difference. A query that filters the column can't be validated on a Sandbox. The
  column's observed representation is a separate, still-pending question (§3).

**Use `BUILT_IN_PARAMETER` to stamp the window into the output** — it is the only clean way to make an AMC CSV self-describing about which period it covers.

**Parameterisation:** declare `inputParameters` (name, `dataType`, `columnType`, optional `defaultValue`, description) on the workflow and override per execution via `parameterValues`. Types: `BINARY, BOOLEAN, BYTE, DATE, DOUBLE, FLOAT, INTEGER, LONG, SHORT, STRING, TIMESTAMP`, plus `ARRAY` of those. **Use this rather than string-templating SQL.**

### Reusable SQL — what is public and what is not

| Source | Full SQL public, no login? |
|---|---|
| **Playbooks** (9) | **Yes** — complete runnable SQL, and they **embed IQ SQL verbatim** |
| `amc-sql/examples.md`, SQL optimisation handbook | **Yes** |
| **Instructional Queries / use case library** | **No** — AMC console authentication required; every doc link is tagged "link only available for AMC UI users" |
| Templates / Solutions (no-code) | No — console only |
| Paid-feature example queries | No — IQ-gated; only schemas are public |
| Amazon/AWS GitHub | No AMC SQL published. `amzn/ads-advanced-tools-docs` is MIT-0 but contains **zero `.sql` files** |

The nine playbooks: Customer journey analytics (CJA), Customer long-term value (CLTV), Programmatic audience framework, Off-Amazon conversions, Optimal frequency analysis, Lookalike audiences for promotional events, Media performance optimization, Enhanced scoring for overlapping AMC audiences, Keyword clustering. **[verified]**

CJA states outright that its queries are leveraged from the IQ library ("Path to conversion by campaign group"), which is why the gated library is effectively readable through a public channel.

**Coverage against our ten contracts:**

| Contract | Status | Amazon asset |
|---|---|---|
| path-to-purchase | **Full** | CJA playbook — two verbatim IQs plus Python to build Sankey pairs |
| reach & frequency | **Full** | Optimal frequency playbook — baseline, optimal-frequency, and audience-creation queries |
| time-to-conversion | **Full** | Promotional events playbook, "Flight time analysis", with published output table |
| customer loyalty / CLTV | **Full** | CLTV playbook — SQL, formula glossary, 2×2 segmentation, expected output shape |
| format / multi-touch attribution | **Structurally verified** | First-touch, last-touch, and equal linear temporal allocation reconcile on Sandbox and Standard; current revenue evidence is halo-inclusive only |
| campaign / media overlap | Partial | SQL optimisation handbook publishes a display-vs-STV overlap query, both non-optimised and optimised |
| audience performance | Partial | Programmatic audience framework, off-Amazon segment queries |
| new-to-brand | Partial | No standalone query; the logic is worked inside CJA's `converted` CTE |
| organic vs ad-attributed | Partial | Public only for **off-Amazon**; the on-Amazon version needs `conversions_all` (paid, no published query) |
| repurchase behaviour | **None** | No public reference query. ARP plus CLTV ratio definitions are the only scaffolding |

**IQ body structure is consistently parseable:** `-- Instructional Query: <Name>` header, an `Instructions:` block-comment preamble, named fences (`/* Campaign Group Start */` … `/* Query End */`), sequential activation markers (`/*[1 OF 5]: Uncomment this line…*/`), inline metric-substitution guidance, unit-conversion notes, and a terminal `-- FINAL SELECT` with aliased output columns. Playbook pages add: description → **Tables used** → collapsible SQL → **Sample output** (naming every result column) → **Interpret results**.

> **This is the highest-leverage finding.** Playbook sample-output tables name every result column, and the `datasources/*` pages give per-column types and thresholds. Cross-reference the two and curated schemas can be derived **without executing a query**. Note that two CJA sample-output tables are internally inconsistent — treat the final `SELECT` as authoritative.

### Validation and introspection

- **There is no validate-without-execute API.** No dry-run flag, no validate endpoint, no `EXPLAIN`. Workflow creation returns `200 OK` but nothing documents whether it parses the SQL. **[verified]**
- **Sandbox is Amazon's answer.** Synthetic data mimicking real schema and statistical properties, 13 months backfilled, both Amazon Ads and paid-feature signals, and **aggregation controls can be turned off** so non-aggregated output is inspectable. Named use cases include query iteration and software development without advertiser-instance access. Limitations: no real campaign/ASIN identifiers, no outliers, and it will not reveal production-scale timeouts. **[verified]**
- **Sandbox dataset list** [verified 2026-10-08, [sandbox guide](https://advertising.amazon.com/API/docs/en-us/guides/amazon-marketing-cloud/amc-sandbox)]: the DSP, sponsored-ads, conversion and attributed-events tables and `conversions_all` are available in synthetic form; `audience_segments_*` (ASI) are **not**; ARP is not listed at all. The list is per dataset and makes no column-level promise (§10). The guide also notes the subscription capability is disabled on a Sandbox.
- ⚠️ **Instances created via the instant self-service APIs do not include a Sandbox.** This makes instance type a prerequisite procurement decision.
- **Programmatic column list:** the AMC Reporting API has a data-sources operation returning name, data type, and metric/dimension per column **[snippet]**; the Amazon Ads MCP server exposes `get_data_sources` **[snippet]**. Whether either returns the **aggregation threshold** is **[unverified]** — the OpenAPI spec is not on the content CDN. The public `datasources/*` pages are currently the only confirmed source of threshold class, and need no credentials.

### Instance types

| Type | Notes |
|---|---|
| **Standard** | Amazon Ads events for one advertiser, mapped to DSP accounts and/or sponsored ads entities |
| **Sandbox** | synthetic signals; every AMC account gets one — **except instant-access instances** |
| **Collaboration** | AMC on AWS Clean Rooms; requires an AdTech AE |
| **Instant access** | self-service, exactly one sponsored ads account, **cannot add DSP**, **no sandbox, no collaboration instance** |

**Cross-instance queries are prohibited.** Instances are isolated.

## 9. What AMC cannot supply

| Concept | Where it must come from |
|---|---|
| ~~Campaign and advertiser names~~ | **AMC supplies them.** Corrected 2026-08-19: `advertiser` and `campaign` are `LOW` and outputtable, verified by live probe — see §4. This row previously sent them to an external campaign-metadata source, which is not needed |
| **Parent ASIN** | **Vendor Data Kiosk** — [ADR-020](../decisions/adr-020-vendor-sources-split-by-capability.md). ARP's `asin_parent` is a paid feature and is no longer the plan |
| **Product brand name** | **Vendor Data Kiosk `brand`** — corrected 2026-08-19. This row sent brand to ARP's `asin_brand`, a paid feature, or to a catalogue. Data Kiosk exposes `brand` and `brandCode` as group-by dimensions on the same query that supplies the ASIN hierarchy ✓, so neither is needed. [ADR-020](../decisions/adr-020-vendor-sources-split-by-capability.md) |
| Cost of goods, inventory | Vendor Central or a catalogue source |
| Retail sales truth | [vendor-central](vendor-central.md) — different measurement system |
| Demographics | not as shopper attributes; as DSP segment membership only, where present (§3 "Not available") |
| Row-level or person-level output | **nowhere — structurally impossible.** Any person-level analysis happens inside AMC and emits aggregates |

> **Brand and parent ASIN exist in exactly one place: the paid ARP table.** **[verified]** 2026-08-17 — checked `amazon_attributed_events_*`, `sponsored_ads_traffic`, `dsp_impressions` and `conversions_with_relevance`; none carries either. What they do carry is a child ASIN (`tracked_asin`, `creative_asin`). Brand membership is *implied* by the `brand_halo_*` metric family but the brand is never named, so **you cannot group by brand or by parent ASIN** without ARP or an external catalogue. Two false friends to avoid: `device_make` is the **device** manufacturer, and `product_line` is Amazon's campaign categorisation, not a merchant brand. `conversions_with_relevance.halo_code` includes `VARIATIONAL_ASIN`, but that is a relationship *classification*, not a parent ASIN value. ARP additionally holds 60 months of history against 13 elsewhere, and the docs explicitly warn against joining across that mismatch.

> **The join-grain limit.** AMC keys advertising data on campaign and ASIN. Vendor Central has **no campaign dimension at all**. So the finest grain at which advertising and retail facts can be joined is **ASIN × date × marketplace**. Actual retail sales can never be attributed to a campaign from these two sources — only ad-attributed sales by campaign (AMC) and actual retail sales by ASIN (vendor), compared at ASIN grain. This is a hard limit on what the product can answer and should be confirmed with whoever owns the product definition.

## 10. Open items

| Item | What would resolve it |
|---|---|
| ~~Are `advertiser` and `campaign` names outputtable?~~ | **CLOSED 2026-08-19 — they are.** Probe `validation-name_columns_outputtable` succeeded with aggregation controls on and returned real names. §4 and §9 corrected; all 15 occurrences in `contracts/shared/amc-columns.yaml` are now plain `LOW`; `canonical-model.md` §3 and §6 corrected, and the `data-contract-amc` steering amended |
| ~~**Is `dsp_views.event_type` `VIEWABLE_IMP` or `VIEW`?**~~ **SETTLED 2026-08-21 by live probe.** The values are `MEASURABLE_IMP`, `VIEW` and `SYNTHETIC_VIEW`. **`VIEWABLE_IMP` does not exist** — §3 recorded it and was wrong. The page's `UNMEASURABLE_IMP` did not appear in this data, which is not proof it is absent. Registry marker dropped | Done. Read viewability from `viewable_impressions`, never by filtering `event_type` |
| **Can deduplicated attribution exclude brand halo?** Temporal first, last, and linear allocation is live-validated, but `conversions` supplies only halo-inclusive `total_product_sales`; the existing advertised-product-only `product_sales` table has no conversion identity | Requires a documented conversion-identity path to `product_sales`, or a separate explicitly halo-inclusive metric family |
| **Why is the `conversions_all` money-scale probe still not running?** `REJECTED` again on 2026-08-21, with no `statusMessage`, so this is now twice. `REJECTED` means the query did not compile. `purchase_currency` as a group key is the leading suspect; the paid-feature table needing a subscription flag even for its synthetic form is the other | Re-run selecting the money columns alone, with no `GROUP BY`, to separate a grouping problem from a table-access one |
| **What scale do `conversions_all`'s money columns use?** That page types `combined_sales`, `off_amazon_product_sales` and `off_amazon_conversion_value` as **`LONG`** where `conversions` and `conversions_with_relevance` type the same columns `DECIMAL(38,4)`. The page also contradicts itself, defining `combined_sales` as a sum of a column it types as a decimal. A `LONG` money column in AMC normally means a scaled integer, so applying no divisor risks a five-order-of-magnitude error. All three carry `scale_divisor: null` and `status: disputed` | A live probe. **Blocks any contract reading revenue from `conversions_all`**, which is the `amc-organic-vs-ad` contract |
| **Is `amazon_live` a value of `ad_product_type`?** §4 gives a closed set of four sponsored values plus `NULL` for DSP. The `amazon_live_traffic` page states the column always holds `amazon_live` there and tells you to filter the attributed-events tables on it. A gate built on the four-value set rejects every Amazon Live row | A live probe, or one `SELECT DISTINCT ad_product_type` |
| **What currency are the attributed-events cost columns in?** `audience_fee`, `bid_price`, `click_cost` and `campaign_budget_amount` each direct you to `currency_name` for the currency, and **there is no `currency_name` or `currency_iso_code` column on that table** [verified]. The only currency column is `purchase_currency`, described as the currency of the purchase. This collides directly with "never store a bare monetary amount" | A live probe, or carrying currency from the traffic-side table in the UNION |
| Does the Reporting API data-sources operation return aggregation thresholds? | ~30 min with an authenticated session. If yes, contracts can be generated from the API with no doc scraping |
| Is `user_id` salted per instance, and does it rotate? | Amazon; undocumented. Until answered, no multi-year identity assumptions |
| Licensing of published playbook SQL | **Legal review.** No stated licence; governed by the [Advertising API License Agreement](https://advertising.amazon.com/API/docs/license-agreement), whose body is SPA-rendered and was not retrievable **[snippet]**. "Program Materials" appears to include sample code. Amazon tells you to customise IQs anyway, so **derive rather than ship verbatim** |
| Drift detection | **No changelog, no per-page dates, no feed.** IQ URLs are opaque 64-hex hashes with no version segment. We must hash the CDN pages on a schedule or we will not notice rewrites |
| Does `winning_bid_cost` exist on `dsp_impressions`? | **A probe against a live instance.** Absent from the schema pages, but selected by the media-performance playbook (§6, re-read 2026-10-08). The docs are internally inconsistent and no page is dated |
| **Is the Sandbox schema narrower than the documented one?** The `dsp_impressions` page lists `campaign_country` (LOW) and `city_name` (HIGH) [verified 2026-10-08], and the sandbox guide says Sandbox signals mimic the structure of advertiser signals. Live, the Sandbox rejected both with "not found in any table" (`city_name` 2026-08-21, `campaign_country` 2026-10-08). `campaign_country` resolves on the STANDARD demo instance (2026-10-08); `city_name` has not been read there. The guide publishes dataset-level availability only, so these may be documented Sandbox gaps or schema drift; nothing establishes which | A STANDARD read of `city_name`. Until then it stays `status: disputed`, and no Sandbox step can validate a query that filters `campaign_country` |
| **Does the default (no discriminator) drop sub-threshold rows?** The create-workflow page says they are filtered out completely. The aggregation-threshold page's own postal-code example returns them with the dimension `NULL`, and the 2026-08-21 Sandbox probe (§5) dropped nothing | A STANDARD run of the same query without the discriminator. All nine governed packs set `filteredMetricsDiscriminatorColumn`, so none depends on the answer |
| **What does the reason column hold outside the observed scope?** The create-workflow page documents a quoted data source, a `(path i of n)` segment when a source is used more than once, and a standard, effective or equivalent count type. Live, 2026-10-08 (`dsp_impressions`, LOW, one reference per query) returned an unquoted form with no path segment on every suppressed row; 2026-08-21 (attributed events, HIGH) returned the same form plus an empty and a truncated reason (§5). Live, 2026-10-09 (two multi-source Sandbox queries, LOW) returned the documented quoted form with a path segment, but the count type was still `users` (§5) | Runs that vary one factor at a time: a MEDIUM or HIGH bar, an effective or equivalent count. Until then nothing outside the fixture conformance check may parse the reason |
| **Is the bar applied per row or per data-source path?** The [aggregation-threshold page](https://advertising.amazon.com/API/docs/en-us/guides/amazon-marketing-cloud/aggregation-threshold) (re-read 2026-10-08) says a LOW column is output only when the row holds at least two distinct users. Live, 2026-10-09 (Sandbox), rows holding two or more distinct users were withheld when one source path contributed fewer than two, and their reasons named that path (§5). This held on pack 14 at acquisition-date grain and on the packs 5–7 fence structural check (structural check, not the exact pack text; it doesn't settle packs 5–7, and AC-E1.5 stays pending Step B), where the path was a touch table (`sponsored_ads_traffic`), not a purchase-side one | A STANDARD run of a multi-source pack. Until then design §5.4's suppression-risk claims for a multi-source pack assume per-path suppression, on touch-side paths too, and no single-source finding is read as covering it. Pack decisions follow criterion 7 per half, as in the [results](../../validation/RESULTS.md#fence-structural-check-pack-14-at-acquisition-date-grain-p-bo-3-and-p-bo-4--2026-10-09): design §8's "criterion 7 fails" row applies to packs 14–15 only, and packs 5–7 follow its "criterion 7 inconclusive" row, with their alternatives kept ready until Step B. Design §8's pack 5 touch-only alternative doesn't assume a touch-side withhold [inference] |
| **Does the discriminator merge rows that match after nulling?** The create-workflow page says it re-aggregates them, and the 2026-08-21 Sandbox probe (attributed events, HIGH `postal_code`) merged them. The 2026-10-08 forced-suppression runs (`dsp_impressions`, LOW keys only) returned one suppressed row per withheld cell on both instance types | A run that varies one factor at a time (table, threshold class, option names). Until then a key may be unique only among unsuppressed rows (§5, design consequence), and nothing may assume either behaviour |
| ~~Volumetric feasibility of our intended grains~~ | **Settled — measured, not answered up front.** [ADR-019](../decisions/adr-019-grain-viability-is-measured.md). Structural derivability is provable from §5; volume is not, and no sandbox can supply it — synthetic data has no realistic distribution, and our `STANDARD` instance is part of the same internal sandbox account rather than a real advertiser's. Each customer's loads measure their own suppression rate |

## 11. Citations

All URLs are the human-readable docs pages; the machine-readable Markdown is the same path under `https://d3a0d0y2hgofx6.cloudfront.net/en-us/`.

**Core**
- [AMC overview](https://advertising.amazon.com/API/docs/en-us/guides/amazon-marketing-cloud/overview)
- [How AMC works](https://advertising.amazon.com/API/docs/en-us/guides/amazon-marketing-cloud/how-amc-works)
- [FAQs](https://advertising.amazon.com/API/docs/en-us/guides/amazon-marketing-cloud/faqs)
- [Instance management](https://advertising.amazon.com/API/docs/en-us/guides/amazon-marketing-cloud/admin/instance-management)
- [Sandbox](https://advertising.amazon.com/API/docs/en-us/guides/amazon-marketing-cloud/amc-sandbox)

**Privacy and thresholds**
- [Aggregation threshold](https://advertising.amazon.com/API/docs/en-us/guides/amazon-marketing-cloud/aggregation-threshold)

**Data sources**
- [Data sources overview](https://advertising.amazon.com/API/docs/en-us/guides/amazon-marketing-cloud/datasources/overview)
- [Paid features overview](https://advertising.amazon.com/API/docs/en-us/guides/amazon-marketing-cloud/datasources/paid_features_overview)

Schema pages read column-by-column for the spend, measure and identifier verification (2026-08-17, page `lastmod` 2026-08-13):

- [`dsp_impressions`](https://advertising.amazon.com/API/docs/en-us/guides/amazon-marketing-cloud/datasources/dsp_impressions)
- [`sponsored_ads_traffic`](https://advertising.amazon.com/API/docs/en-us/guides/amazon-marketing-cloud/datasources/sponsored_ads_traffic)
- [`amazon_attributed_events_by_conversion_time`](https://advertising.amazon.com/API/docs/en-us/guides/amazon-marketing-cloud/datasources/amazon_attributed_events_by_conversion_time) — **`_by_traffic_time` has no page of its own; that URL 404s.** This one page documents both tables and states they share a schema
- [`amazon_retail_purchase_paid`](https://advertising.amazon.com/API/docs/en-us/guides/amazon-marketing-cloud/datasources/amazon_retail_purchase_paid)
- [`conversions_with_relevance`](https://advertising.amazon.com/API/docs/en-us/guides/amazon-marketing-cloud/datasources/conversions_with_relevance)
- [`dsp_impressions_by_segments`](https://advertising.amazon.com/API/docs/en-us/guides/amazon-marketing-cloud/datasources/dsp_impressions_by_segments) and [`audience_segment_membership_paid`](https://advertising.amazon.com/API/docs/en-us/guides/amazon-marketing-cloud/datasources/audience_segment_membership_paid) — the five segment tables, read 2026-10-08
- [Media performance optimization playbook](https://advertising.amazon.com/API/docs/en-us/guides/amazon-marketing-cloud/playbooks/media_performance_optimization) — the cross-platform spend `UNION ALL` and the `winning_bid_cost` reference

Re-read from the content CDN on 2026-10-08: every schema page above except `conversions_with_relevance`, plus [`dsp_clicks`](https://advertising.amazon.com/API/docs/en-us/guides/amazon-marketing-cloud/datasources/dsp_clicks), [`dsp_views`](https://advertising.amazon.com/API/docs/en-us/guides/amazon-marketing-cloud/datasources/dsp_views), [`conversions`](https://advertising.amazon.com/API/docs/en-us/guides/amazon-marketing-cloud/datasources/conversions) and [`conversions_all_paid`](https://advertising.amazon.com/API/docs/en-us/guides/amazon-marketing-cloud/datasources/conversions_all_paid); the aggregation-threshold, paid-features, sandbox, create-workflow and ad hoc workflow pages; and the SQL [expressions](https://advertising.amazon.com/API/docs/en-us/guides/amazon-marketing-cloud/amc-sql/expressions), functions and limitations pages.

**SQL**
- [AMC SQL overview](https://advertising.amazon.com/API/docs/en-us/guides/amazon-marketing-cloud/amc-sql/overview)
- [Basics](https://advertising.amazon.com/API/docs/en-us/guides/amazon-marketing-cloud/amc-sql/basics) · [Functions](https://advertising.amazon.com/API/docs/en-us/guides/amazon-marketing-cloud/amc-sql/functions) · [Limitations](https://advertising.amazon.com/API/docs/en-us/guides/amazon-marketing-cloud/amc-sql/limitations) · [Examples](https://advertising.amazon.com/API/docs/en-us/guides/amazon-marketing-cloud/amc-sql/examples)
- [Query optimisation handbook](https://advertising.amazon.com/API/docs/en-us/guides/amazon-marketing-cloud/amc-sql/how-to-opt-amc-sql-queries)

**Workflows**
- [Create workflow](https://advertising.amazon.com/API/docs/en-us/guides/amazon-marketing-cloud/reporting/create-workflow)
- [Execute workflow](https://advertising.amazon.com/API/docs/en-us/guides/amazon-marketing-cloud/reporting/execute-workflow)
- [Ad hoc workflow](https://advertising.amazon.com/API/docs/en-us/guides/amazon-marketing-cloud/reporting/ad-hoc-workflow)
- [Schedule workflow](https://advertising.amazon.com/API/docs/en-us/guides/amazon-marketing-cloud/reporting/schedule-workflow)
- [Get your results](https://advertising.amazon.com/API/docs/en-us/guides/amazon-marketing-cloud/reporting/get-your-results)

**Reusable SQL**
- [Playbooks overview](https://advertising.amazon.com/API/docs/en-us/guides/amazon-marketing-cloud/playbooks/playbooks_overview)
- [Use case library](https://advertising.amazon.com/marketing-cloud/use-cases) (console) · [IQ library launch note, 2021-10-26](https://advertising.amazon.com/en-ca/resources/whats-new/amc-instructional-query-library)

**Legal**
- [Advertising API License Agreement](https://advertising.amazon.com/API/docs/license-agreement) **[snippet]**
- [API policy](https://advertising.amazon.com/API/docs/policy/en_US) **[snippet]**
