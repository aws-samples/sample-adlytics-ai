# Answerable questions — what Semantic V2 can and cannot tell a customer

| | |
|---|---|
| **Status** | Deterministic authorized campaign/entity/metric/period/operation resolution is implemented and passes local tests [verified 2026-08-24]. [Validation results](../validation/RESULTS.md) record a development deployment with AMC query-pack execution against an internal demo instance (2026-08-29) and authenticated browser acceptance (from 2026-09-15); none of that is customer-volume or production acceptance. A deployment is answerable only where its required successful current publications and exact selected coverage exist. |
| **Last updated** | 2026-10-01 |
| **Related** | [canonical-model](canonical-model.md) §5 · `contracts/shared/metric-dictionary.yaml` · [semantic resolution](concepts/semantic-resolution.md) · [ADR-053](decisions/adr-053-resolve-campaign-identity-and-hierarchy-before-measurement.md) · [Conversational Runtime](capabilities/conversational-runtime/architecture.md) §5 |

## 1. Why this document exists

Two different questions get asked as one, and conflating them is the whole source of ambiguity about what the platform can do:

| Question | Determined by | Changes when |
|---|---|---|
| **Can the data layer *ever* answer this?** | the sources, and the canonical model over them | never — no amount of building fixes a structural refusal |
| **Can it answer this *today*?** | which contracts have loaded | every contract we write |

"We can't do ROAS by campaign against retail sales" and "we can't do ROAS yet" sound alike and are nothing alike. The first is permanent and must be said to customers. The second is a sequencing fact about us.

So every question below lands in one of four buckets, and the axis is **what kind of work stands between the question and an answer**:

| | Bucket | What remains | Who owns it |
|---|---|---|---|
| **A** | Answerable — execution only | build what is already designed. No decision, no research, no new modelling | engineering, on a schedule |
| **B** | Blocked on something that is not building | a funding decision, a live probe, a transcription, a specification nobody has written, or a capability that does not exist yet | named per row |
| **C** | Answerable, with an obligatory caveat | nothing — but the number is incomplete without a sentence | the narration layer |
| **D** | **Never answerable from these sources** | nothing will change it | say so plainly and offer the nearest true thing |

> **A and B are not "contract written" versus "contract missing".** That was the original axis and it stopped discriminating once every writable contract was written. **Everything** in A and B alike waits on the foundation — Iceberg tables, the curation pipeline, `dim_product` loaded — so that is a shared precondition rather than a distinction. What separates them is whether anything *other than building* is still outstanding.

**Bucket D is the most valuable section here.** A product that refuses precisely is trusted; one that answers everything is not.

## 2. What is actually contracted today

**Thirteen contracts, covering every dataset both sources expose that the model can use.** The full map is [canonical-model](canonical-model.md) §4a.

This is the shared precondition for buckets A and B alike, which is why it sits above both rather than inside either.

| Fact | Contracts | State |
|---|---|---|
| `fct_ad_exposure` | `amc-reach-frequency`, `amc-dsp-impressions`, `amc-dsp-clicks`, `amc-dsp-views`, `amc-dsp-reach` | **contracted, all four channels**; DSP reach (`amc-dsp-reach`) not deployed, its first live AMC run PENDING |
| `fct_attributed_conversion` | `amc-attributed-conversion` | **contracted** — unblocks ROAS, halo, NTB |
| `fct_deduplicated_temporal_attribution` | `amc-deduplicated-temporal-attribution` | **contracted, capability-conditional** — exact first-touch, last-touch, or linear model; suppressed deliveries are unavailable |
| `fct_retail_sales` | `vendor-sales-by-asin` | **contracted** |
| `fct_product_page_views` | `vendor-traffic-by-asin` | **contracted** — unblocks conversion rate |
| `dim_product` | `vendor-product-hierarchy` | **contracted** — hierarchy *and brand* |
| `dim_campaign` | `amc-campaign-dsp`, `amc-campaign-sponsored` | **contracted** — supplies effective-dated campaign aliases and identity projection |
| 3 reconciliation control totals | `vendor-sales-reported-total`, `vendor-traffic-reported-total`, `amc-retail-purchases` | **contracted** |
| `fct_new_to_brand_buyers` | `amc-new-to-brand-buyers-platform`, `amc-new-to-brand-buyers-channel`, `amc-new-to-brand-buyers-campaign` | **contracted, deployed; publication to `CURRENT` accepted on dev with synthetic data 2026-10-17. The measurement is not proven: the dev lake holds no buyer rows, so no new-to-brand buyer figure has been answered from it** ([D2 live acceptance](../validation/RESULTS.md#d2-pages-and-graph-control-plane-live-acceptance-on-dev--2026-10-18)) — distinct buyers per complete week or month at platform grain and per ad product, and per campaign per complete calendar month; their first live AMC runs are PENDING |
| `fct_exposure_group_outcome` | `amc-exposure-groups` | **contracted, deployed; accepted on dev with synthetic data 2026-10-17** — shoppers per exposure group (DSP only, sponsored ads only, both) and identity space per complete week or month, with their 14-day searches, page views, purchases and sales; chat answers the per-cell counts and DSP and sponsored window spend (each in its own currency, never added together) at one complete period, each with "association, not cause", and rates on Amazon account IDs only; overlap shares and the funnel are computed by the Reach and funnel bake. Its first live AMC runs are PENDING, and AC-E1.5 is pending Step B |
| `fct_exposure_frequency` | `amc-exposure-frequency-platform`, `amc-exposure-frequency-channel` | **contracted, deployed; accepted on dev with synthetic data 2026-10-17** — shoppers per frequency bucket (impressions in each shopper's 14-day window) and identity space per complete month or quarter, at platform grain and per ad product, with their purchases and sales; chat answers the per-bucket counts and each window spend at one complete period in one bucket scheme, never compares two schemes, and refuses return by frequency per channel (per-channel return uses credited revenue, not built); its first live AMC runs are PENDING |
| `fct_segment_delivery`, `dim_audience_segment` | `amc-audience-performance`; `amc-dsp-segment-names`, `amc-segment-metadata` | **contracted, deployed; accepted on dev with synthetic data 2026-10-17** — per DSP campaign and matched DSP behaviour segment per complete calendar month, impressions, spend, shoppers reached, and purchases and sales attributed to the same campaign; segments named from `dim_audience_segment` with their provider (Amazon, third party, or "Provider not recorded"); DSP and ASI segments use different IDs and are never linked; chat only; their first live AMC runs are PENDING |
| `fct_segment_profile` | `amc-segment-buyer-profile-inmarket`, `amc-segment-buyer-profile-lifestyle` (paid ASI); `amc-segment-buyer-profile-dsp-exposed` | **contracted, deployed; accepted on dev with synthetic data 2026-10-17** — per complete calendar month and profile path (`asi_in_market`, `asi_lifestyle`, `dsp_exposed_only`), the ad-touched buyers, reached shoppers and new-to-brand buyers per audience segment and the delivery's profiled totals; shares and the index inside one delivery, never summed or compared across paths; read against the path's segment roster; their first live AMC runs are PENDING |
| `fct_shopper_acquisition` | `amc-acquisition-cohorts`, `amc-acquisition-cohorts-arp` | **contracted, deployed; accepted on dev with synthetic data 2026-10-17** — acquisition cohorts per cohort vintage (the 13 calendar months ending with its `as_of_month`): shoppers won per calendar month by acquisition channel group (won by DSP only, by sponsored ads only, or by both), and how many bought again in each of the 12 following months. The standard path counts ad-touched repeats only; the paid ARP path counts every Amazon purchase, for Amazon account IDs only. Chat answers the per-cell counts and the standard-path rates at one vintage and one path, and the ARP rates per cell by pairing both paths, as the Retention bake does (owner decision 2026-10-12); cohorts switch to calendar quarters automatically after a degraded vintage. Their first live AMC runs are PENDING |

### 2a. What a customer must have — capability tiers

**This is a template deployed into the customer's own account, so what it can answer depends on what that customer has access to**, not on anything we fund. A deployment discovers its own capability rather than assuming one.

| Tier | Customer needs | Unlocks |
|---|---|---|
| **Retail only** | Vendor Central reports landed in S3 | what sold, what Amazon paid, returns, glance views, and retail conversion rate for a product resolved from an exact ASIN or effective title plus marketplace; brand and parent selection or rollups remain unavailable |
| **+ Advertising** | an AMC instance with the ads accounts mapped to it | spend, impressions, clicks, viewability, reach, frequency, **ROAS**, halo, new-to-brand — most of the product |
| **+ Paid features** | an **AMC Paid Features subscription**, theirs not ours | organic-versus-ad-attributed split, 60 months of retail history, the AMC-to-vendor reconciliation anchor |

**Advertising is where most of the product lives.** Retail-only still answers exact ASIN-and-marketplace retail questions; advertising unlocks advertising measurement, and the three paid-feature capabilities remain subscription-conditional. Vendor brand and parent attributes require no paid subscription, but their selectors, joins, and rollups are not implemented.

Two consequences worth stating plainly:

**No capability is descoped by a customer lacking the subscription.** The paid-feature contracts stay in the repository and simply do not run for that deployment. This is the same shape as [ADR-019](decisions/adr-019-grain-viability-is-measured.md), where a grain's viability is measured per customer rather than declared once — capability is a property of the deployment.

**A retail-only deployment is coherent, not crippled.** It answers product-and-marketplace retail
questions from an exact ASIN or one effective title alias, while refusing advertising questions and
brand or parent selection and rollups with a stated reason rather than an error. Worth knowing
because it is likely the easiest deployment for a first customer: Vendor Central access is far more
common than a mapped AMC instance.

The deployment-time prerequisites are in the [repository README](../README.md).

> **Contracted, published, and selected are different claims.** A contract is a governed source mapping. A successful publication is immutable data that passed every required gate. A question can read it only while its canonical scope points to that success through `CURRENT` and the selected publications exactly cover the requested buckets without holes. Attempts and failed candidates are operational evidence, never availability.

**The Semantic V2 resolver, tools, and contracts are tested locally and run in a development deployment.** Deterministic resolver, tool, contract, and release-gate tests pass locally [verified 2026-08-24]. Development deployment and real-resource execution (2026-08-29) and authenticated browser acceptance (from 2026-09-15) are recorded in [validation results](../validation/RESULTS.md). None of that shows that all 50 metrics execute through a deployment, and each deployment still needs its own required successful current publications. Local test coverage does not show that a particular deployment has exact selected coverage, and it does not make an absent customer capability or structurally invalid grain answerable; the tiers and refusals below still apply per deployment.

**Managed extraction remains outside this repository's product boundary.** A deployment supplies eight governed AMC packs and a destination-owned registration/Finalizer path, but never holds Amazon credentials or executes workflows. Once an external pipeline lands a conforming result and registration, canonical manifest finalization, attempts, gates, immutable successes, `CURRENT`, and exact selected coverage are the sole path to a readable answer.

## 3. Bucket A — answerable, execution only

Every row in this bucket has a governed fact, metric, and grain. Semantic V2 executes every declared metric against strongly selected successful `CURRENT` publications; any missing current scope, incomplete governed sibling set, or coverage hole refuses before Athena. What remains for a particular customer is delivery of the required source capability and exact current coverage, not a second serving path.

**Three grain sets, named once.** Every metric below groups by one of them. `contracts/shared/metric-dictionary.yaml` is authoritative — these names are a reading aid over it, not a second copy of it.

| Set | Dimensions |
|---|---|
| **exposure** | date · channel · campaign · ad_product · marketplace |
| **conversion** | exposure **+ product** |
| **retail** | date · product · marketplace |

Retail has no channel and no campaign, and exposure has no product. Those two absences explain almost every refusal in §6.

| Question a customer asks | Metric | Grain | Condition |
|---|---|---|---|
| **"What was my ROAS?"** | `roas_attributed_14d` | **exposure** | not by product — see D-8 |
| "What was my deduplicated Amazon Ads ROAS under first touch, last touch, or linear attribution?" | `roas_deduplicated_incl_halo_14d` | conversion date × channel × marketplace | one exact `attribution_model_key`; halo-inclusive numerator; no campaign or product grain; availability requires an unsuppressed delivery |
| "What revenue did my ads drive?" | `attributed_revenue_14d` | conversion | |
| "What lift do my ads create on products I'm not advertising?" | `attributed_revenue_halo_14d` | conversion | halo is **not** organic — see D-7 |
| "What's my ROAS if I count halo?" | `roas_attributed_incl_halo_14d` | exposure | must show the default alongside |
| "How much of my business is new customers?" | `ntb_rate_12mo` | conversion | 12-month lookback, in the name |
| "What did I spend on advertising?" | `ad_spend` | exposure | DSP **and** sponsored; fee-inclusive |
| "How many impressions did I get?" | `impressions` | exposure | |
| "How many clicks?" | `clicks` | exposure | |
| "What was my click-through rate?" | `ctr` | exposure | |
| "What share of my ads were viewable?" | `viewable_impressions` ÷ `measurable_impressions` | exposure | **DSP only** — sponsored publishes no viewability |
| "How many distinct shoppers saw my ads?" | `reach_users` | exposure | **only at the grain extracted, and only when every delivered row is unsuppressed** — never summed, see D-6 |
| "How often did the average shopper see it?" | `avg_frequency` | exposure | same restriction as `reach_users` |
| **"What was DSP reach for campaign X on 5 October?"** (a day DSP reach is measured) | `reach_users` | Amazon DSP · campaign · one UTC day · marketplace | **chat only**, one campaign on one day, and only when the question names Amazon DSP (a breakdown that doesn't lists sponsored reach only); answered only for days with a current `amc-dsp-reach` publication, and a day before the first one is refused naming it; a later day without one (a refused delivery, or a day still inside the two-day cadence lag) is refused naming no day, by design; a week or several campaigns is refused (D-6); no DSP-plus-sponsored total. DSP average frequency (`avg_frequency`) is answered on the same days; DSP cost per reached shopper (`cost_per_reach`) is refused, because DSP spend and DSP reach are separate deliveries and only spend carries a currency |
| **"How many new buyers did I get last month?"** | `buyers_new_to_brand_12mo` | marketplace · one complete ISO week or calendar month | **complete-period rule**: answered only for a complete measured week or month, named in the answer; if the latest one hasn't landed, the answer says so and uses the latest measured one; a rolling or partial window ("the last 7 days") is refused with the latest measured period; never summed across periods; a period with too few shoppers, or none, reads "not available", never 0 |
| **"Which products do new-to-brand shoppers buy first on Sponsored Products?"** | `purchases_new_to_brand_incl_halo_12mo` | one channel and ad product · product · marketplace | **one channel at a time**, never summed across channels or ad products (one purchase can be credited to more than one channel); includes brand-halo products; answered only over a window whose every day is published from `amc-attributed-conversion` pack version 2 or later, otherwise refused naming the first fully covered date |
| "How many shoppers bought with ad attribution?" | `buyers_attributed` | same | same complete-period rule |
| "What did each new buyer cost?" | `cost_per_new_to_brand_buyer_12mo` | same | same rule; spend sums exactly that period's days and is refused when any day lacks current spend (D-6); divides by distinct buyers, not NTB purchases |
| **"How many new buyers did each channel bring last month?"** | `buyers_new_to_brand_12mo`, `buyers_attributed`, `cost_per_new_to_brand_buyer_12mo` | ad product (with or without channel) · marketplace · one complete ISO week or calendar month | **same complete-period rule, per ad product**: read from the per-channel extraction (grain set `channel`), never derived from the whole-network figure, and **never added across channels**, because one shopper can buy after ads on more than one (a request for the sum is refused); one marketplace at a time; every ad product with ad spend in the period is listed, and one with no row reads "not available: too few shoppers, or none"; a channel alone is refused, ask by ad product; cost per new buyer divides that ad product's spend over the period's days by its own new buyers |
| **"How many new buyers did each campaign bring last month?"** | `buyers_new_to_brand_12mo`, `buyers_attributed`, `cost_per_new_to_brand_buyer_12mo` | campaign (with or without its ad product) · marketplace · one complete calendar month | **chat only, complete calendar months only**: read from the per-campaign extraction (grain set `campaign`), never derived from the per-channel figure, and **never added across campaigns** (a request for the sum is refused); a week is refused naming the latest measured month; one marketplace at a time; every campaign with ad spend in the month is listed by name, and one with no row reads "not available: too few shoppers, or none"; when 20 or more campaigns had spend and more than a fifth have no row, no campaign figure is offered, each reads "not available: too few shoppers in this breakdown; see new buyers per channel", and a comparison or ranking over that month is refused; cost per new buyer divides that campaign's spend over the month's days by its own new buyers |
| **"Which segments did DSP campaign X reach last month?"** | `segment_reach`, `segment_impressions`, `segment_spend`, `segment_purchasers`, `segment_purchases`, `segment_attributed_revenue`, `segment_cpm`, `segment_roas_attributed`, `segment_purchase_rate` | one DSP campaign · audience segment · marketplace · one complete calendar month | **chat only, complete calendar months only, one campaign at a time** (a segment breakdown without a campaign is refused naming it): each segment is named with its provider, Amazon or third party, or "Provider not recorded", never all as Amazon's; a segment whose name hasn't arrived reads by its ID; **segment figures never add up** (impressions, spend and shoppers repeat once per matched segment), so a sum across segments or campaigns is refused; a segment the campaign reached the month before with no row this month reads "not available: too few shoppers this month, and it was listed last month"; the campaign's first month says segments with too few shoppers aren't listed; ratios read one campaign and segment row; segment return (sales over spend) is refused in chat, because sales and spend each carry their own currency. Shows which segments perform, not which line item to change |
| **"Which audience segments do last month's buyers over-index in?"** | `segment_buyer_share`, `segment_exposed_share`, `segment_index`, `segment_buyers`, `segment_exposed_shoppers`, `segment_buyers_new_to_brand`, `profiled_buyers_total`, `profiled_exposed_total` | audience segment · marketplace · one profile path · one complete calendar month | **chat, and the Audiences page once its entry lands; complete calendar months only, one profile path at a time** — shares and the index are paired per segment inside one delivery by the measurement tool; a delivery whose profiled total is missing answers every share "not available"; a sum of shares or a figure without a profile path is refused; the paid paths need Audience Segment Insights (ADR-107); first live runs PENDING |
| **"Do the shoppers my ads win come back?"** | `cohort_buyers_new_to_brand_12mo`, `cohort_repeat_buyers`, `cohort_repeat_buyers_cumulative`, `repeat_buyer_rate`, `cumulative_repeat_rate`, their `_account_ids` forms, `cohort_repeat_purchases`, `cohort_repeat_revenue` | cohort month · months since acquisition · acquisition channel group · marketplace · one cohort vintage (`as_of_month`, the latest by default) · one path (`cohort_path`) | **one vintage and one path at a time**: the standard path counts ad-touched repeats from this advertiser only and says it understates true repeat; the ARP path (paid) counts every Amazon purchase for Amazon account IDs only. Cohort sizes repeat on every month's row and are never added across months, and cumulative repeat buyers are their own count. A cell AMC didn't return reads "not available"; a cohort with no shoppers won that way reads "No shoppers were won this way in {month}, or too few to show." Rates compared across acquisition groups read Amazon account IDs. The ARP rates are refused in chat, offering their two operands; Retention shows them. A comparison of a monthly and a quarterly vintage is refused ("Cohort grain changed in {period}.") |
| "What did I sell?" | `retail_revenue_shipped`, `units_shipped` | retail | net of returns, and restates |
| "What did Amazon pay me for it?" | `cogs_shipped` | retail | |
| "What was ordered versus shipped?" | `units_ordered`, `revenue_ordered` | retail | **`MANUFACTURING` only** — null under `SOURCING` |
| "How many units came back?" | `customer_returns` | retail | null under `FRESH` |
| "How many people viewed my product pages?" | `glance_views` | retail | |
| "What's my retail conversion rate?" | `conversion_rate_retail` | retail | **`MANUFACTURING` only** |
| **"How did this product perform?"** | any product-valid retail or conversion metric filtered by an exact product key resolved from an ASIN or effective title alias plus marketplace | conversion or retail | ambiguous titles require clarification; brand and parent lookup remains blocked below |
| "What's my blended ROAS?" | `roas_blended` | **date · marketplace only** | mixes basis — see D-11 and §5 |
| "Which campaign was that?" | exact canonical `campaign_key` filter resolved from an authorized readable campaign name or effective-dated alias | exposure or conversion | implemented; a deployment still needs the required successful current campaign-entity publication and exact selected measurement coverage. Direct advertiser filtering is not supported |

> **Brand and parent attributes are source-capable, not yet query-resolved.** The Data Kiosk query
> supplying the ASIN hierarchy publishes title aliases, `brand`, `brandCode`, and `parentAsin` under
> SCD2 validity. The implemented resolver can map an effective title alias to one exact product key
> before measurement, without a query-time dimension join. Brand and parent selection or rollup still
> require governed joins and remain Bucket B.

## 4. Bucket B — blocked on something that is not building

Grouped by the *kind* of blocker, because the kind determines who can clear it. None of these is waiting on modelling work we could simply do.

**Customer-side access we cannot provide**

| Question | Blocked on |
|---|---|
| "What's my organic versus ad-driven split?" | an **AMC Paid Features subscription the customer holds** — see §2a. Not our decision and not a funding question ([Q-07](decision-coverage-registry.md#question-register-q-nn) is reframed). Also needs the probe below, so a subscribed customer is still blocked until that runs |

**A live probe against AMC**

| Question | Blocked on |
|---|---|
| "What's my organic versus ad-driven split?" | the three `conversions_all` money columns are `status: disputed`; a `LONG` where sibling tables say `DECIMAL(38,4)` risks a five-order-of-magnitude error. The gate refuses them until settled |

**Work on the column registry**

| Question | Blocked on |
|---|---|
| "Which audience segments performed?" | the audience query packs and their facts, which aren't written yet ([business outcome measurement](concepts/business-outcome-measurement.md)). The five segment tables were transcribed into the registry on 2026-10-08, so the allowlist no longer refuses them |

**Deployment acceptance and publication state**

Campaign semantic resolution is implemented under accepted [ADR-053](decisions/adr-053-resolve-campaign-identity-and-hierarchy-before-measurement.md); [semantic resolution](concepts/semantic-resolution.md) owns its contract. Development deployment and browser acceptance of the platform are recorded in [validation results](../validation/RESULTS.md), but no recorded run exercises the campaign clarification below in a deployment. Each deployment still needs the required successful current governed campaign-entity publication.

| Question | Blocked on |
|---|---|
| "Which Northstar Home Goods campaign do you mean?" | a recorded deployed acceptance of the campaign clarification path, plus the required successful current campaign-entity publication in that deployment. ADR-053 acceptance and resolver implementation are no longer blockers |
| "Show Brand X" | a model-visible brand selector and effective-dated measurement join; direct brand filtering remains unavailable |
| "Roll my variations up to the parent" | an effective-dated `dim_product` join and parent selector in the query plan; the source hierarchy is loaded but current measurement plans group facts directly by child ASIN |

**A specification nobody has written**

| Question | Blocked on |
|---|---|
| "What's a customer worth?" | a lifetime-value definition; acquisition cohorts and their repeat purchases are contracted (`fct_shopper_acquisition`, [ADR-104](decisions/adr-104-define-acquisition-cohorts-by-amazons-new-to-brand-flag.md)), but no value per customer is. Deliberately not invented |
| "How does my funnel path look?" | `fct_conversion_path` needs the output columns of the CJA playbook SQL. `conversions_with_relevance` is transcribed and ready; the query shape is the gap |

**A capability that does not exist yet**

| Question | Blocked on |
|---|---|
| "Am I overspending on this campaign?" | fitted saturation curves — deferred ML capability |
| "What will next month look like?" | Chronos forecast paths — deferred ML capability |
| "What was the incremental effect?" | `fct_incremental_effect` — deferred ML capability |

> **Two of these are one probe away from A**, and it is the same probe: settling the `conversions_all` money scale would clear the modelling half of organic-versus-ad, leaving only the funding decision. Worth pairing with the recorded reach-suppression probe on the next live-instance session.

## 5. Bucket C — answerable, with a sentence that is not optional

The number is correct. Presenting it without the caveat is the failure mode.

| Question | Caveat the narration must carry | Why |
|---|---|---|
| "What's my blended ROAS?" | "this divides *all* retail revenue by *all* ad spend — it measures whether advertising grows total demand, not what advertising earned" | `mixes_basis: true`. Two measurement systems that never reconcile ([ADR-002](decisions/adr-002-canonical-data-model.md)) |
| "What's my ROAS including halo?" | the default ROAS must be shown **alongside** it | Reads higher by construction. Presented alone it absorbs the halo invisibly |
| "Compare my ad spend to my sales, by day" | "day-level comparison is approximate — ad data is UTC, retail is Pacific" | Up to 8 hours of offset, permanent. Weekly and coarser are safe ([ADR-004](decisions/adr-004-timezone-and-fx.md)) |
| "What did I sell last week?" *(recent period)* | "these figures are net of returns and will move as returns arrive" | Vendor revenue restates after publication |
| "What's my retail conversion rate?" | only computed under `MANUFACTURING` | The traffic report has no `distributorView`, so a `SOURCING` pull cannot be rate-ed against it |
| Any breakdown where suppression is material | "12% of rows were withheld by Amazon's privacy thresholds; totals will not reconcile" | Measured per customer, per partition ([ADR-019](decisions/adr-019-grain-viability-is-measured.md)) |
| Any figure spanning marketplaces | which date's FX rate was used | No source supplies FX ([ADR-004](decisions/adr-004-timezone-and-fx.md)) |

## 6. Bucket D — never answerable from AMC and Vendor Central

**These do not become answerable by building more.** Each needs a different source, or is forbidden by Amazon's privacy model. The right response is to say so and offer the nearest true thing.

**D-1 · "What were my actual retail sales for campaign X?"**
Vendor Central has **no campaign dimension at all**. Advertising and retail can only be joined at ASIN × date × marketplace and no finer, so actual retail sales can never be attributed to a campaign.
*Offer instead:* attributed ROAS by campaign, which uses the sales Amazon credits to each campaign; retail revenue by product; or blended ROAS at date and marketplace.

**D-2 · "How did brand X perform?" is structurally supportable but not resolved in the current query path**
Data Kiosk publishes brand, stable brand code, and parent ASIN under SCD2 validity, so the source and
canonical model can support brand and variation rollups. Effective-dated title aliases can resolve
one exact child product before measurement; current plans still do not join brand or parent
attributes or group facts by them. Brand and parent behavior is a buildable Bucket-B capability
rather than a permanent source refusal.
*Offer instead:* performance for an exact ASIN or one unambiguous effective title until governed
brand and parent joins exist.

**D-3 · "Show me results for shopper 12345" / any per-person view**
`user_id` is `VERY_HIGH` and can **never** appear in output. It is legal only inside a CTE for aggregate counting. This is Amazon's privacy model, not a configuration.
*Offer instead:* aggregate reach and frequency.

**D-4 · "Break this down by age / income / household status"**
AMC does not carry demographics. `household_size` exists as a bucketed string, and nothing else.

**D-5 · "Which specific ad caused this specific purchase?"**
AMC gives attributed aggregates, not a per-event causal chain. `request_tag` links events but the docs warn duplicates occur and it is explicitly not recommended as a join key.

**D-6 · "What was my reach last month?"** — *when reach was extracted daily*
Reach is a distinct count and **cannot be summed**. Monday's distinct shoppers plus Tuesday's is not two days' distinct shoppers. AMC has no approximate-distinct function, so no sketch can be exported and merged. Monthly reach requires a monthly extraction.
*Offer instead:* reach at the grain that was extracted, stated plainly; or a new extraction at the grain asked for.

**D-7 · "What's my true organic versus paid split?"**
Brand halo is **not** organic — it is ad-exposed demand on non-advertised same-brand products. A genuine organic split needs `conversions_all.exposure_type`, a paid feature. Conflating the two overstates organic.
*This is B rather than D for a customer who holds the subscription* — see §2a. Listed here because the halo/organic confusion is the trap, and halo is available on every deployment with advertising access.

**D-8 · "What's my ad spend by product?" / "What's my ROAS by product?"**
Cross-channel spend is now answerable — all four exposure datasets are contracted. **By product is not.** `fct_ad_exposure` has no product dimension: its grain is date × channel × campaign × ad_product × marketplace. DSP impressions carry no ASIN, so a product grain cannot be conformed across channels.
This was caught by `make check-lineage`: three ROAS metrics declared a `product` grain their denominator cannot supply, which would have divided product-level revenue by spend broadcast across products — computable, plausible, wrong.
*Offer instead:* ROAS by campaign, or attributed revenue by product, or retail revenue by product. A promoted-product exposure fact keyed on `creative_asin` would make it real for sponsored ads only; recorded as deferred work.

**D-11 · "What's my blended ROAS by product?" or "by channel?"**
`roas_blended` is valid at **date and marketplace only**. Retail revenue carries no channel and no campaign; ad spend carries no product. A ratio across two measurement systems is valid only where both systems share a dimension, and here that is two dimensions rather than four.
*Offer instead:* blended ROAS at marketplace level, plus attributed ROAS by campaign, with the basis difference stated.

**D-9 · "How am I doing at other retailers?"**
The canonical platform/ecosystem taxonomy accepted in
[ADR-052](decisions/adr-052-separate-ecosystem-platform-channel-and-ad-product.md) is implemented,
but no non-Amazon source contract, pack, or publication is loaded. Other retailers therefore
remain unloaded-capability refusals rather than zero.

**D-10 · "What did my competitor spend?"**
Neither source contains any competitor data.

## 6a. How a question becomes an answer

The current release begins at a deterministic semantic-resolution boundary. It
resolves only authorized campaign/entity/metric/period/operation meaning to exact canonical filters
and includes effective-dated aliases, opaque page selection, final-window reauthorization, and
resolved-only mapping. [Semantic resolution](concepts/semantic-resolution.md) owns that contract;
[ADR-053](decisions/adr-053-resolve-campaign-identity-and-hierarchy-before-measurement.md) owns the
decision. Each deployment's required successful current publications remain a separate condition;
deployment acceptance evidence is in [validation results](../validation/RESULTS.md).

**Component diagram — local request resolution and measurement gates; request-resolution viewpoint, component depth.**

![Component diagram: local request resolution and measurement gates. Customer language is resolved and authorized before a canonical request passes four measurement gates. Each failed gate ends in a typed clarification or measurement refusal; a successful request produces a grounded answer.](diagrams/questions-flow.svg)

> **Takeaway.** Semantic resolution fails closed before measurement; a particular deployment
> still needs its required current publications before a question is answerable.

The two grain gates are different things that share a name. The structural one is a property of the metric and is identical for every customer. The measured one is derived from that customer's own suppression rates and can move between loads ([ADR-019](decisions/adr-019-grain-viability-is-measured.md)).

## 7. Defects this document surfaced

Writing this document, then writing the contracts it implied, produced five corrections. All are fixed; they are recorded because each was a plausible-looking wrong number waiting to happen, and because `make check-lineage` now exists to catch the class rather than the instance — for the ratio shapes it resolves, which is not all of them ([gap register](decision-coverage-registry.md#gap-register-g-nn) G-23).

| # | Defect | How it would have presented |
|---|---|---|
| 1 | Three ROAS metrics declared a `product` grain `ad_spend` cannot supply | Product-level revenue divided by spend broadcast across products |
| 2 | `roas_blended` also declared `channel`, which retail revenue has no dimension for | A channel breakdown of a figure with no channel |
| 3 | `canonical-model.md` declared **no dimension keys on any fact** — only measures | A schema that cannot be generated, and no way to check a contract's keys |
| 4 | `brand` was recorded as needing paid ARP or a catalogue | A capability refused for years that was free in a query we already planned |
| 5 | `amc-reach-frequency` named the suppression columns `is_suppressed` / `distinct_users` where the model says `rows_suppressed` / `distinct_user_count` | One concept under two names across two facts |

**Defect 1 and 2 are the same mistake:** a ratio is valid only where **both** operands are, and nothing was comparing them. The gate now does, and it found more than a read-through had — including that `attributed_revenue_14d` was *under*-declared, missing the `ad_product` grain its fact does carry.

**Defect 3 was the most consequential.** Every fact stated its grain in prose — "one row per date × product × marketplace" — and then listed only measures in its column table. So `date_key`, `product_key` and `marketplace_key` existed in the sentence and nowhere in the schema. Since `data-layer.md` §10 says the Iceberg DDL is *generated from the contract*, that would have generated tables with no keys.

**A promoted-product exposure fact would make "ROAS by product" real** for sponsored ads, which carry `creative_asin`. The model already anticipates the shape — `fct_attributed_conversion_amazon` is described as promoted-product grain for exactly this reason. DSP is the harder half, because its impressions carry no ASIN. Recorded as deferred work rather than built, since it changes a grain rather than adding a column.

## 8. What each tool family needs

The five families in [Conversational Runtime](capabilities/conversational-runtime/README.md), against the buckets above.

| Family | Reads | Contracted? | Still needs |
|---|---|---|---|
| `catalog` | Glue catalog + selected `CURRENT` publication state | **n/a — needs no fact** | **built.** Reports capability and exact selected coverage |
| `measurement` | all four facts | **yes, complete** | **built.** Executes declared metrics against strongly selected V2 publications |
| `diagnostics` | `fct_ad_exposure` + `fct_attributed_conversion` + `fct_retail_sales` | **yes, complete** | the domain decomposition maths |
| `saturation` | fitted params from DynamoDB | no | deferred ML model fitting |
| `forecasting` | precomputed paths from DynamoDB | no | deferred ML capability |

> **Three of the five families are fully contracted, and two of those are built.** `diagnostics` decomposes variance across spend, CVR, AOV and traffic — every input is a contracted fact. What it waits on is its own maths, not more source work.

`catalog` was the one to build first, and this document is effectively its content: it reports what can be asked, at what grain, and why anything else is refused. Without it the agent guesses at its own capabilities, which is where confident wrong answers come from.

**What a tool must carry that this document establishes:** the structural refusals in §6 as stated reasons, the obligatory caveats in §5 as required narration, and the distinction between a permanent refusal and a this-customer-this-load one.

## 9. Relationship to Q-11

[Q-11](decision-coverage-registry.md#question-register-q-nn) asks for the O1–O20 outcome list and is open. §3 to §6 are a derived answer to it: rather than enumerating desirable outcomes and discovering later which are computable, this starts from what the model supports and names the rest as refusals with reasons.

If the intended outcome list contains something absent here, that is the useful signal — it means either a source we have not evaluated, or an expectation that needs resetting early.
