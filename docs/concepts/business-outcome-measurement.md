# Concept — Business outcome measurement

| | |
|---|---|
| **Level** | L2 — concept |
| **Status** | Proposed — the owner approved the design on 2026-10-06. Partly implemented in code, with two outcome packs, one new fact and no page yet (2026-10-09): the segment tables are transcribed, the US ASI segment marketplace is pinned, the paid-feature state and catalog refusal exist in the domain, the media taxonomy is at version 2 with the `amazon_ads` profile and `exposure_source_sets`, and `make validate-contracts` refuses a pack that outputs a canonical taxonomy key, an attributed-events sum above campaign grain, and a source-reported share in a curated column. `make lint-boundaries` refuses a runtime import of a producer. Every metric declares how it aggregates across networks, with only spend additive, and the planner accounts the network axes per operand from source platforms derived at load. A query pack may declare its suppression policy, period grains, cadence lag, governed input parameters and expected bar, and the Finalizer checks period alignment and re-resolves each parameter. The shopper-aggregate rules exist in the domain (`src/domain/shopper_aggregates/`): closed enumerations writing `not_returned` gaps, loss grading, the bar and repeated-total checks, and disjoint-axis sums; distinct counts are kept out of the withheld residual and the control total, and are typed absent like ratios. Periodic serving exists on the server: the planner serves a periodic metric only at one exact complete period (ISO week, calendar month or quarter, UTC), aligns a daily-additive operand to that period's days, and refuses any figure that combines two shopper-aggregate deliveries except the three declared cases; page schemas accept the `declared_period` and `as_of_vintage` page times, calendar presets, `prior_calendar_period`, `vintage_policy` and section `declared_periods`; `resolve_calendar_preset`, the designed period texts and the chat period rule exist in the domain. The pilot pack is in (2026-10-09, not deployed): `amc-new-to-brand-buyers-platform` writes `fct_new_to_brand_buyers`, distinct new-to-brand and attributed buyers per complete ISO week and calendar month at platform grain, under `discard_suppressed_rows`, with its missing platform cell written `not_returned`; `buyers_new_to_brand_12mo`, `buyers_attributed` and `cost_per_new_to_brand_buyer_12mo` are the first periodic metrics, and chat answers them at a complete measured period. Its first live AMC run is pending. The browser's temporal transport v2 is pending, so the committed page declaration stays at `temporal_control_version` 1. The page packages for the Scorecard, Reach and funnel, Audiences, Retention and Repeat purchase by product are designed at UI manifest revision `2026-10-08.1`, approved under the owner's UI/IA approval of 2026-10-07; no page is rendered yet. The Scorecard's server side exists: `NetworkScorecardArtifact` v1 (`src/domain/presentation/scorecard.py`), whose types hold attributed figures only on one channel of one network (the deduplicated network headline is the one figure above a channel) and total spend only inside one marketplace and currency; its materializer, registered with the baker and waiting on the page entry; and the deterministic `network-scorecard` v1 insight contract. New-to-brand step 1, gateway products and step 2 live on the New customers tab (IA, OD-4), not the Scorecard, designed in the [New customers](../ui/new-customers/README.md) package at manifest revision `2026-10-09.1`. Their server side exists (2026-10-10, not deployed): `NewCustomersArtifact` v2 (`src/domain/presentation/new_customers.py`) and its materializer, registered with the baker and waiting on the page entry; `amc-attributed-conversion` query pack version 2 with the nullable halo-inclusive `purchases_new_to_brand_incl_halo_12mo`, whose pack-version-1 deliveries, re-lands and reprocesses still load with it NULL; the planner's pack-v2 coverage gate, which chat, page bakes and insights inherit; and the deterministic `portfolio-acquisition` v1 insight contract. Nothing serves the tab yet: it waits on the UI sub-slice, temporal transport v2 and the route approval, and pack version 2's first live AMC run is pending. Step 2 there is new buyers for the whole Amazon Ads network in one marketplace, and from package revision `2026-10-09.2` new buyers per channel under it (pack 2, owner S-3 revision, 2026-10-09). Pack 2 is in (2026-10-10, not deployed): `amc-new-to-brand-buyers-channel` writes the channel grain set of `fct_new_to_brand_buyers`, one row per ad product per complete week and month, read in chat and the bake against the ad-product roster (ad products with spend in the period), so a rostered ad product with no row reads "not available"; its first live AMC runs are pending. Per-campaign new buyers (pack 3) and DSP reach (pack 4) are chat-only, with no page. Pack 3 is in (2026-10-10, not deployed): `amc-new-to-brand-buyers-campaign` writes the campaign grain set of `fct_new_to_brand_buyers`, one row per campaign per complete calendar month, read in chat against the campaign roster (campaigns with spend in the month), so a rostered campaign with no row reads "not available"; a roster of 20 or more with more than a fifth missing offers no campaign figure and points to new buyers per channel; its first live AMC run is pending. DSP reach is in (2026-10-09, not deployed): `amc-dsp-reach` writes `reach_users` for the Amazon DSP rows of `fct_ad_exposure` per campaign per UTC day under `reject_delivery`, the union null-fills it where another writer lacks it ([ADR-109](../decisions/adr-109-null-fill-non-additive-measures-in-multi-writer-facts.md)), and chat answers it only for days with a current pack 4 publication ([canonical model](../canonical-model.md#fct_ad_exposure--domain-exposure)). The Media Efficiency API's reach slots stay unavailable. Its first live AMC run is pending. Pack 5 is in (2026-10-11, not deployed): `amc-exposure-groups` writes `fct_exposure_group_outcome` and its `_withheld` residual, shoppers per exposure group and identity space per complete week and month with their 14-day outcomes, under the shared exposure fence (byte-identical to the 2026-10-09 probe text, checked by a contract test), declared expected cells (3 groups × 2 identity spaces, graded per identity space, never withdrawn) and a declared touch-only pack variant (manual, off). Every metric over it is `causal: false`, and chat answers carry "association, not cause". The Reach and funnel server side for overlap and funnel exists (`ReachAndFunnelArtifact` v1, `src/domain/presentation/reach.py`) with its materializer, registered with the baker and waiting on the page entry, and the `reach-overlap` and `full-funnel-association` v1 insight contracts. The fence result is a structural check, not the exact pack text, so AC-E1.5 stays "pending Step B". Its first live AMC runs are pending. Packs 6 and 7 are in (2026-10-11, not deployed): `amc-exposure-frequency-platform` and `-channel` write `fct_exposure_frequency` and its residual, shoppers per frequency bucket and identity space per complete month and quarter, at platform grain and per ad product, under the same fence, with declared buckets and the coarse scheme as a declared variant that switches on automatically from the period after a degraded slice and stays on until Step B. Pack 7 ships without its credit branch (P-BO-1 criterion 6b not run), so per-channel return reads "Not available per channel" beside the platform figure; the credit branch is pending Step B. The Reach and funnel server side now bakes the frequency section and the `frequency-saturation` v1 insight. Their first live AMC runs are pending. Packs 9, 13 and 8 are in (2026-10-11, not deployed): `amc-dsp-segment-names` and `amc-segment-metadata` (paid ASI, under the pinned US `segment_marketplace_id`) write `dim_audience_segment` in two namespaces, `amc_dsp_behavior` and `amc_asi`, kept separate with no cross-namespace key because P-BO-4 step 1 failed; each segment carries its provider, Amazon or third party, from the governed classification (the P-BO-3 first pass accepted for dev; human review pending), and one no rule covers reads "Provider not recorded". Their names and descriptions are the first free text loaded through AMC's backslash-escaped CSV dialect. `amc-audience-performance` (C1-a) writes `fct_segment_delivery` and its residual per DSP campaign and matched segment per complete calendar month, chat only, read against the campaign's segment roster (its segments returned the month before), graded and never withdrawn; no figure is summed across segments or campaigns. Packs 10-12 are in (2026-10-13, not deployed): `amc-segment-buyer-profile-inmarket` and `-lifestyle` (paid ASI, the shared fence then Amazon account IDs only, the month's first-Thursday snapshot range derived by the planner and re-derived by the Finalizer) and `amc-segment-buyer-profile-dsp-exposed` write `fct_segment_profile` per complete calendar month and profile path, with each delivery's `profiled_total` row a declared cell; chat answers the segment counts, the totals and the shares and index, paired per segment inside one delivery; `AudienceProfileArtifact` v1 and its materializer fail closed until the `audiences` page entry exists; `segment-over-index` v1 names segments over B_min with a Bonferroni-corrected bound. Pack 13's paid-feature state follows ADR-107. The Audiences page comes later. Their first live AMC runs are pending. Packs 14 and 15 are in (2026-10-11, not deployed): `amc-acquisition-cohorts` and `amc-acquisition-cohorts-arp` (paid ARP) write `fct_shopper_acquisition` and its residual per cohort vintage (the 13 calendar months ending with its `as_of_month`), monthly cohorts by acquisition channel group with their repeat buyers in follow-up months 1 to 12, under `discard_suppressed_rows` with declared expected cells, absent cohorts kept out of the loss denominator, the repeated-total and currency checks, and the calendar-quarter variant switching on automatically from the vintage after a degraded one (sticky until Step B; pack 15 follows pack 14). Pack 15's cohort set is pack 14's text byte for byte, and it reads its own `arp_marketplace_name` parameter. Chat answers per-cell counts and standard-path rates at one vintage and one path, and the ARP rates per cell, pairing both paths under the cohort-size check through the rule the Retention bake reads (owner decision 2026-10-12); without a pack 15 delivery only the ARP rates are refused. The Retention server side exists (`RetentionCohortArtifact` v1, `src/domain/presentation/retention.py`) with its materializer, registered with the baker and waiting on the page entry, and the `retention-shift` v1 insight contract; the Repeat purchase by product pointer waits on pack 16. Their first live AMC runs are pending. |
| **Last updated** | 2026-10-11 |
| **Owns** | The rules every business-outcome figure follows: distinct shopper counts as their own kind of fact, complete measured periods, disjoint shopper groupings with one window per shopper, outcomes kept inside one network, association-only labels, paid-feature states, and "not available" over hiding. |
| **Applied by** | [Source Acquisition](../capabilities/source-acquisition/README.md) · [Data Layer](../capabilities/data-layer/README.md) · [Load Path](../capabilities/load-path/README.md) · [Query Path](../capabilities/query-path/README.md) · [Preset Artifact Publisher](../ui/preset-artifact-publisher.md) · [Conversational Runtime](../capabilities/conversational-runtime/README.md) |
| **Conventions** | [Documentation conventions](../conventions.md) |

## 1. What this concept is

Seven customer outcomes extend Adlytics without a new source system. Each is measured inside one
advertising network, and shopper analysis runs only inside Amazon Marketing Cloud (AMC).

| Outcome | What it serves | Rules it leans on |
|---|---|---|
| A1 per-network ROI scorecard, with TACoS | Spend, attributed revenue and ROAS per channel and ad product, a deduplicated network headline, TACoS, and a cross-network spend row | One network per outcome; basis labels |
| B1/B2 new-to-brand acquisition and gateway products | New-to-brand purchases and buyers, cost per new buyer, and products that bring new buyers, one channel at a time | Distinct counts; complete periods |
| D1/D2 deduplicated reach and frequency | Shoppers who saw DSP only, sponsored ads only, or both; a frequency curve; DSP reach per campaign-day | Disjoint groupings; one window per shopper |
| E1 full-funnel contribution | Search, page-view and purchase rates per exposure group | Association, not cause |
| C2 with C1 audience segment profile | Which Amazon audiences over-index among buyers; DSP segment performance | Overlapping segments; paid-feature states |
| B3 repeat-purchase cohorts | Cohort triangle and retention curve by acquisition group; repeat purchase per product from Brand Analytics | Cohorts; levels only for reported shares |
| G2 in-app anomaly alerts | Direction-only alerts when a page republishes | Event-driven over baked findings |

This concept owns the rules those outcomes share. **Lives elsewhere:** AMC table and column facts belong to
[AMC](../sources/amc.md) and the column registry; Brand Analytics facts to
[Vendor Central](../sources/vendor-central.md); field-level columns to the [canonical model](../canonical-model.md);
the temporal wire grammar to the [temporal control contract](../ui/temporal-control-contract.md); page
layouts and navigation to the [UI page contracts](../ui/README.md#2-approved-page-roster); why each rule was chosen to the ADRs in §5.

## 2. The rule

> **A distinct shopper count is read only from the one AMC delivery that counted it, at its exact period and
> grain, and an attributed outcome is never totalled across networks.**

The invariants that keep it true:

- **Clean room.** Shopper IDs stay inside AMC. Only privacy-thresholded totals and source labels land. No
  shopper-level row is stored, and customer data never leaves the customer's AWS account.
- **Read-only.** Nothing is written to Amazon or any other system: no audience activation, no budget or bid
  change.
- **Not available over hiding.** A missing cell, product, segment, period or paid figure reads "not
  available" with a plain reason. It never reads 0 and is never silently dropped.
- **One network.** Spend may total across networks within one marketplace and currency. Attributed sales and
  outcomes never do, and each ROI figure is labelled with its basis and window.
- **One window per shopper, for reach, frequency and the full funnel.** A shopper's group, frequency, spend
  and outcomes all come from the 14 days from their first ad in the period. Cohorts use the acquisition
  date instead ([ADR-104](../decisions/adr-104-define-acquisition-cohorts-by-amazons-new-to-brand-flag.md)).
- **One period per page.** A page shows one resolved period and names it.
- **Association is labelled.** A figure that compares non-random groups carries "association, not cause".

## 3. How it works

**Component diagram — shopper-aggregate path from AMC to a served figure; data-custody viewpoint, component depth.**

![Component diagram: shopper-aggregate path from AMC to a served figure. Shopper query packs count distinct shoppers inside the customer AMC instance and return only thresholded totals and source labels. The customer-owned producer lands them, the Finalizer checks periods and parameters, the load path grades expected cells, and the planner serves each figure at its exact period and grain. No path leads back to Amazon.](diagrams/business-outcome-flow.svg)

> **Takeaway.** Shopper IDs never leave AMC; everything after the query pack works on thresholded totals,
> and every figure is planned at the exact period and grain AMC counted.

Each served grain set and period is its own extraction, never derived from another. Several grain sets
may share one AMC execution through `UNION ALL`, which is the first lever against AMC's differencing guard
([ADR-099](../decisions/adr-099-treat-distinct-shopper-counts-as-a-new-kind-of-fact.md) point 11). Each
grain set run on its own adds to that guard's load. Pack 3 (new buyers per campaign) adds 1 execution per
marketplace per month on `amazon_attributed_events_by_conversion_time`. Packs 1 and 2 already read that
table 5 to 6 times each a month (4 or 5 weeks plus 1 month), so its business-outcome load goes from 10–12
to 11–13 executions a month per marketplace [arithmetic], before `amc-attributed-conversion`'s own cadence
and pack 14's monthly vintage. Pack 3 is the last pack the shed order drops; only the monthly
platform-grain packs (1, 5 and 6) are never shed. Whether AMC accepts this schedule is [unverified] until
probe P-BO-1 Step B runs one month of it. The load
path discards withheld rows, keeps the exact
rows that survive, and writes every expected cell that didn't come back as `not_returned`. The table below
is what a reader sees for each cell state.

| Cell or source state | What the reader sees |
|---|---|
| Returned | The figure, with its basis and window label |
| Expected but not returned | "Not available", with "too few shoppers, or none" |
| Breakdown of 20 or more cells, too lossy, with a coarser view | Each cell reads "not available" with a pointer to the coarser view |
| Period not landed yet | "Figures for {period} are expected after {date}" |
| Period didn't arrive | "Figures for {period} didn't arrive", with the latest available period |
| Paid feature never observed | "Needs the Amazon Insights paid feature", never a claim that the customer isn't subscribed. The agent's catalog refuses such a metric as `not_observed_in_deployment`, after every platform-level check |
| Per-channel return without credited revenue | "Not available per channel", with the platform figure beside it |

## 4. Rejected alternatives

- Deriving coarser shopper counts from one finest extraction —
  [ADR-099](../decisions/adr-099-treat-distinct-shopper-counts-as-a-new-kind-of-fact.md)
- Rejecting a whole delivery when any row is withheld —
  [ADR-099](../decisions/adr-099-treat-distinct-shopper-counts-as-a-new-kind-of-fact.md)
- Rolling windows for shopper counts —
  [ADR-100](../decisions/adr-100-serve-periodic-metrics-only-for-complete-measured-periods.md)
- Groups from every touch in the period, and any-ad revenue over one channel's spend —
  [ADR-101](../decisions/adr-101-model-shopper-groupings-as-disjoint-axes-with-one-window-per-shopper.md)
- `exact_only` network axes without per-operand accounting —
  [ADR-106](../decisions/adr-106-keep-outcome-metrics-inside-one-network-by-type.md)
- Hiding paid panels, or a subscription flag in configuration —
  [ADR-107](../decisions/adr-107-derive-paid-feature-availability-from-delivery-evidence.md)
- A scheduled alert evaluator —
  [ADR-108](../decisions/adr-108-raise-in-app-alerts-when-page-artifacts-publish.md)

## 5. Decisions

| Decision | Owner |
|---|---|
| Distinct shopper counts are a new kind of fact; amends grain viability and suppression | [ADR-099](../decisions/adr-099-treat-distinct-shopper-counts-as-a-new-kind-of-fact.md) |
| Periodic metrics only for complete measured periods; temporal transport v2 | [ADR-100](../decisions/adr-100-serve-periodic-metrics-only-for-complete-measured-periods.md) |
| Disjoint groupings, one window per shopper, credited per-channel return | [ADR-101](../decisions/adr-101-model-shopper-groupings-as-disjoint-axes-with-one-window-per-shopper.md) |
| Association-only flag and caveat | [ADR-102](../decisions/adr-102-flag-association-only-metrics-and-require-their-caveat.md) |
| Audience segments as an overlapping, namespaced dimension | [ADR-103](../decisions/adr-103-model-audience-segments-as-a-source-namespaced-overlapping-dimension.md) |
| Cohorts by Amazon's new-to-brand flag | [ADR-104](../decisions/adr-104-define-acquisition-cohorts-by-amazons-new-to-brand-flag.md) |
| Brand Analytics levels only; reported shares source-aligned | [ADR-105](../decisions/adr-105-keep-source-reported-shares-source-aligned-and-publish-brand-analytics-levels-only.md) |
| Outcomes inside one network by type | [ADR-106](../decisions/adr-106-keep-outcome-metrics-inside-one-network-by-type.md) |
| Paid-feature availability from delivery evidence | [ADR-107](../decisions/adr-107-derive-paid-feature-availability-from-delivery-evidence.md) |
| In-app alerts when page artifacts publish | [ADR-108](../decisions/adr-108-raise-in-app-alerts-when-page-artifacts-publish.md) |
| Null-fill for non-additive measures in multi-writer facts | [ADR-109](../decisions/adr-109-null-fill-non-additive-measures-in-multi-writer-facts.md) |
| Query-pack parameters, period policy and cadence lag | [ADR-110](../decisions/adr-110-declare-query-pack-parameters-period-policy-and-cadence-lag.md) |
| No runtime import of producers or ad-platform clients | [ADR-111](../decisions/adr-111-forbid-runtime-imports-of-producers-and-ad-platform-clients.md) |

The docs that own the facts these ADRs rely on change in the implementing changes, not here. The
[canonical model](../canonical-model.md), [answerable questions](../answerable-questions.md), the
[AMC](../sources/amc.md) and [Vendor Central](../sources/vendor-central.md) source pages,
[business context](../business-context.md), the [Data Layer](../capabilities/data-layer/README.md) README and
the [decision coverage registry](../decision-coverage-registry.md) each change with the fact, pack, probe
result or answer they describe. The [temporal control contract](../ui/temporal-control-contract.md) changes
with periodic serving. The [UI page roster](../ui/README.md) gained the five page packages at manifest revision `2026-10-08.1`, and the [New customers](../ui/new-customers/README.md) package at `2026-10-09.1`; `2026-10-09.2` revises New customers, Audiences, Retention and Reach and funnel for wave 2b.

## 6. Open items

Every probe is a read-only AMC query and an onboarding prerequisite. Access to a real AMC instance isn't
confirmed, and development uses synthetic seed data. A probe that can't exercise its condition records
"inconclusive", never "pass". Probes on a customer's instance run under that deployment's identity, and
their raw output stays in the customer's account.

| Item | What resolves it | Blocks |
|---|---|---|
| P-BO-1: are surviving counts exact under suppression, including groupings computed per shopper; which `SECONDS_BETWEEN` convention AMC uses; does credit double count; does a derived label keep the 2-shopper bar | Sandbox queries Q0 to Q9, then one month of the real schedule on the first customer instance. **Step A status, 2026-10-08** ([results](../../validation/RESULTS.md#business-outcome-probes-p-bo-1-step-a-and-p-bo-2--2026-10-08)): the convention (t2 − t1), the derived-label bar (2), and the cohort sum pass, and the month index passes provisionally, pending owner confirmation of the post-cohort scope; no query suppressed a row, so exactness under suppression is inconclusive; the Sandbox lacks `dsp_impressions.campaign_country`, so the pack 5–7 references and AC-E1.5 can't run there. **Status, 2026-10-09** ([results](../../validation/RESULTS.md#fence-structural-check-pack-14-at-acquisition-date-grain-p-bo-3-and-p-bo-4--2026-10-09)): the packs 5–7 fence passed criteria 5, 6 and 8 on a structural check, not the exact pack text, so AC-E1.5 stays "pending Step B"; pack 14 at acquisition-date grain passed sub-gate 2 (0c, 5c), and criterion 7's pack 14 half failed, so packs 14–15 ship monthly cohorts with the calendar-quarter alternative [inference: read on the acquisition-date text]; criterion 7's pack 5 half is inconclusive | Every distinct-count pack; a failure narrows grains and falls back to rejecting withheld deliveries |
| P-BO-2: can AMC expand a segment-ID array within 6 hours | One Sandbox query, confirmed on a Standard instance. **Status, 2026-10-08:** fail on criterion 3 on the Sandbox (provisional); every criterion passes on the internal Standard instance (validation-instance evidence); route C1-a, the per-segment table | The array route for DSP segment performance; the per-segment table is the fallback |
| P-BO-3: does the DSP segment table hold demographic segments | One customer-instance query and a reviewer check. **Status, 2026-10-09** ([results](../../validation/RESULTS.md#fence-structural-check-pack-14-at-acquisition-date-grain-p-bo-3-and-p-bo-4--2026-10-09)): present on the validation instance (validation-instance evidence, not customer); the first pass is accepted for dev (owner, 2026-10-09), and segments are labelled by provider, Amazon or third party | Nothing; demographics otherwise need paid features |
| P-BO-4: is a DSP segment ID the same as an ASI segment ID | One customer-instance query. **Status, 2026-10-09** ([results](../../validation/RESULTS.md#fence-structural-check-pack-14-at-acquisition-date-grain-p-bo-3-and-p-bo-4--2026-10-09)): step 1 fail on the validation instance, so the DSP and ASI namespaces stay separate and no C1–C2 link is drawn | Any link between DSP segment performance and the audience profile |
| P-BO-5: is every DSP campaign's country filled in | One customer-instance query | Exact DSP overlap per marketplace; a small pack reports excluded impressions otherwise |
| Integer ASI `segment_marketplace_id` value for a marketplace other than the US | The US value is pinned from Amazon's documentation (2026-10-08; [AMC](../sources/amc.md) "Paid features"). Another marketplace is pinned from documentation, or one observed value under a narrow rule | The paid audience packs for that marketplace |
| Open decision 1: paid features without a subscription, hide or show as available. Default: show, from delivery evidence, never hidden | The owner's answer; the default stands until then. If "hide", paid panels aren't rendered and the agent answers "not supported here"; the paid-feature state and catalog reason go away | Nothing; the standard paths ship either way |
| Open decision 2: ask early customers to enrol in Audience Segment Insights and Amazon Retail Purchases before 2026-12-31. Default: yes, a customer action at onboarding | The owner's answer. If "no", the full audience profile and the all-purchase repeat path stay in the "needs the paid feature" state for every customer | Nothing; the standard paths ship either way |
| Alert evaluator `infra/` change (open decision 3) | Deferred by the owner on 2026-10-07 (UI decision OD-7). v1 ships the fallback: an "Also changed" section on each tab with qualifying findings and the Overview's "What changed" block, with no inbox and no `infra/` change. The evaluator comes later as a control-plane workload | The G2 inbox and badge only |
| Open decision 5: do Persona Builder demographics fit the clean-room rule. Default: yes, as Amazon aggregates | The owner's answer. If "no", the audience profile excludes demographic segments through a governed exclusion list applied at load, and a later demographic profile (C3) is dropped | Nothing in the top seven outcomes |
| Brand Analytics landing | The forecasting design's governed API operation pack is implemented | Repeat purchase per product |
