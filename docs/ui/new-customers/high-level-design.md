# New customers — high-level design

## 4. Solution strategy

One tab, one baked artifact per preset, no child view. Every declared preset is served from a publish-time
artifact through the shared [preset artifact publisher](../preset-artifact-publisher.md), as Campaigns and
Retail health are ([ADR-070](../../decisions/adr-070-serve-declared-preset-windows-from-publish-time-artifacts.md)).
Shell, navigation, date control, tables, dialogs, change chips, coverage sheet and assistant context are the
shared patterns of the [information architecture](../information-architecture/README.md#6-shared-patterns)
([ADR-118](../../decisions/adr-118-render-every-page-from-one-set-of-shared-patterns-and-extended-tokens.md)).
This document records only what's specific to New customers.

The tab mixes two time kinds on one window, as the business outcomes design lets a `period` page do
([ADR-100](../../decisions/adr-100-serve-periodic-metrics-only-for-complete-measured-periods.md)): step 1 and
gateway products are daily-additive and render on every preset; new buyers are distinct counts that exist only
for complete ISO weeks and calendar months, so their section binds only the two calendar presets.

### Presentation API role

| Operation | Route | Browser intent | Server responsibility |
|---|---|---|---|
| New customers | `POST /api/presentation/new-customers` [proposal] | Session; optional `TemporalSelection.v2` restricted to `omitted` or a declared preset | Resolve the shared control, serve the revision-current baked artifact for that preset, resolve the new-buyers pending or missing text for the request date, and build pointer text at serve time |

The route is a new API Gateway route, so it's an `infra/` change that needs its own approval
([delivery slices](../information-architecture/delivery-slices.md#2-rules-for-every-slice) rule 5). The
approved mockup opens no metric-focus dialog, so the tab has no metric-focus route.

### Composition (top to bottom, per the mockup)

1. Hub header: "Customers" eyebrow, tab strip (New customers, Retention, Repeat purchase by product), h1
   "New customers", the description "Shoppers buying from your brand for the first time in 12 months", the
   date control, then the scope band.
2. **New-to-brand purchases**: the per-channel table, one row per ad product with its channel group, and the
   table foot. No all-channel total.
3. **Insights row**: the `portfolio-acquisition` cards, each inside one channel.
4. **New buyers**: "New buyers" and "Cost per new buyer" on a calendar preset, then the same two figures per
   channel in a table with no total row (revision `2026-10-09.2`); the absence callout on a rolling preset.
5. **Gateway products**: the channel selector, then that channel's products with the two captions.
6. Two pointer lines, each only when the session's navigation manifest lists its tab: Audiences, then
   Retention.
7. **About these figures** disclosure.

The tab has no "Also changed" section: no default alert rule covers `portfolio-acquisition`
([ADR-108](../../decisions/adr-108-raise-in-app-alerts-when-page-artifacts-publish.md)), and the alert
evaluator is deferred (OD-7).

### What changed from the business outcomes design (owner-approved amendments)

The owner's OD-4 (2026-10-07) moved new-to-brand step 1, step 2 and gateway products off the Scorecard to this
tab; the Scorecard keeps one pointer line. Against design §6.2, the approved mockup also:

- shows step 1 per ad product with a change-in-share column, and no campaign table or `ntb_rate_12mo` column;
- shows the platform-grain "New buyers" and "Cost per new buyer" for step 2, not beside the new-to-brand
  purchase figures, and no campaign new buyers.

The owner's S-3 revision (2026-10-09) replaced "park" for the per-channel and campaign new-buyer packs.
Revision `2026-10-09.2` adds per-channel new buyers under the platform tiles, which the approved mockup
doesn't draw; owner ratification is stop item S-B1. Campaign new buyers are answered in chat only.

### Data lineage

| Section | Source | Metrics |
|---|---|---|
| New-to-brand purchases | `fct_attributed_conversion` (Amazon Marketing Cloud attributed events, by conversion date) and Amazon Ads delivery for spend | `purchases_new_to_brand_12mo`, `revenue_new_to_brand_12mo`, `ntb_revenue_share`, `cost_per_ntb` [repo: `contracts/shared/metric-dictionary.yaml:429`, `:450`, `:929`, `:1131`] |
| New buyers | The platform-grain new-to-brand buyers pack (business outcomes pack 1, one delivery per ISO week and calendar month) and Amazon Ads delivery for spend | `buyers_new_to_brand_12mo`, `cost_per_new_to_brand_buyer_12mo` (design §6.2; added in wave 2) |
| New buyers per channel | The per-channel new-to-brand buyers pack (business outcomes pack 2, grain set `channel`, one delivery per ISO week and calendar month), Amazon Ads delivery for spend, and the ad-product roster from `fct_ad_exposure` (design §4.5) | The same two metrics at grain set `channel` (wave 2b) |
| Gateway products | `fct_attributed_conversion` at `amc-attributed-conversion` pack version 2, which adds the halo-inclusive new-to-brand purchase column from `new_to_brand_total_purchases` [repo: `contracts/shared/amc-columns.yaml:2648`]; that column counts promoted and brand-halo new-to-brand purchases together ([verified] in the AMC documentation, rechecked 2026-10-08; owned by [Amazon Marketing Cloud, attributed measures](../../sources/amc.md#attributed-measures--verified-column-names)) | `purchases_new_to_brand_incl_halo_12mo` (design §6.2; added in wave 2) |

The halo-inclusive column exists only on deliveries produced at pack version 2 or later. A day counts as covered
by the pack version of the delivery published for it, not by whether it has rows, so a window with any day
published from an older pack isn't covered for gateway products, and the section reads its coverage text rather
than an undercount ([low-level design](low-level-design.md#gateway-coverage)).

### Build slices and dependencies

| Needs | Why |
|---|---|
| The new-to-brand buyers pack, its fact, the periodic metrics and their load wiring | New buyers and cost per new buyer |
| `amc-attributed-conversion` pack version 2, its pack-v1 policy and the coverage gate | Gateway products |
| `NewCustomersArtifact` v2, its materializer and the `portfolio-acquisition` v1 insight contract | The whole tab |
| The per-channel new-to-brand buyers pack and the ad-product roster (wave 2b) | New buyers per channel |
| Periodic serving, server half (built) and temporal transport v2, browser half (pending) | The two calendar presets need `temporal_control_version` 2 ([ADR-100](../../decisions/adr-100-serve-periodic-metrics-only-for-complete-measured-periods.md)), and the tab ships only with them, so it waits on both |
| Shared patterns: change block, coverage block, page context | [ADR-119](../../decisions/adr-119-serve-window-coverage-as-one-server-written-evidence-status-block.md) to [ADR-121](../../decisions/adr-121-declare-change-direction-and-sentiment-on-the-server.md) |
| The UI sub-slice: page entries, navigation entry, browser decoder and tab | The UI workstream owns `presentation/` page entries and `frontend/` |
| A route approval | The new route above |
