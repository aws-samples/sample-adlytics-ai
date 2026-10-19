# New customers — low-level design

## 6. Wire, artifact, and renderer contracts

Marker key: [repo] is read in this worktree on 2026-10-09 with `file:line`; [proposal] is a choice made in
this package that the build slice may still change through review. Metric ids marked "(wave 2)" don't exist
yet; the business outcomes wave 2 slices add them with exactly these ids (design §6.2).

### Exact API inputs

| Route | Method and auth | Accepted body | Explicitly forbidden |
|---|---|---|---|
| `/api/presentation/new-customers` [proposal] | `POST`, the custom request authorizer | `{session_id}`, optionally `temporal_selection: TemporalSelection.v2` restricted to `omitted` or a declared preset | custom ranges, a caller comparator, raw windows, channel, ad product, campaign or product selectors (the artifact carries every channel), unknown fields |

The page follows the shared [temporal-control contract](../temporal-control-contract.md):
`PresentationResult.v2` with the control and either the artifact or `window_unavailable`.

### Page declarations

New page id `new-customers`, route `/new-customers/`, governed label "New customers", in the Customers hub with
tab order 1 ([ADR-115](../../decisions/adr-115-organise-the-app-into-customer-question-hubs-from-one-navigation-file.md)).
A page with shopper slots carries a v1 and a v2 entry (design §4.7). The UI workstream adds both.

| File | Entry |
|---|---|
| `presentation/pages/v1/pages.yaml` | `mode: single_basis`, because every figure is Amazon-attributed (`ad_attributed`) [repo: `src/domain/presentation/page_schemas.py:721`]. `grain.time: period`, dimensions `[marketplace]`. Metric slots for step 1, gateway products and step 2. The two step-2 slots declare `declared_periods: [last_complete_week, last_complete_month]` and carry the `time_grain` selector, so they're left out of the three v1 period checks under rules (i) to (iii) [repo: `src/domain/presentation/page_schemas.py:500-545`]; every other slot is daily-additive and runs them as today |
| `presentation/pages/v2/pages.yaml` | `mode: selectable`, `boundary_required: true`, `navigation: {hub_id: customers, tab_label: New customers, tab_description: Shoppers buying from your brand for the first time in 12 months, order: 1}`. `temporal_policy` below. Artifact `NewCustomersArtifact` v2, outcomes `[complete, empty, insufficient]` |

v2 sections [proposal for the ids the information architecture didn't name], in render order:

| Section | `declared_periods` | `period_anchor` | Binds |
|---|---|---|---|
| `workspace-scope` | — | — | the scope band |
| `new-to-brand` | — | — | step 1 |
| `new-customers-insights` | — | — | `portfolio-acquisition` v1 |
| `new-to-brand-step-2` | `[last_complete_week, last_complete_month]` | `false` | `buyers_new_to_brand_12mo`, `cost_per_new_to_brand_buyer_12mo`, at grain set `platform` for the tiles and `channel` for the per-channel table |
| `gateway-products` | — | — | `purchases_new_to_brand_incl_halo_12mo` |
| `pointers` | — | — | the two pointer lines |
| `evidence-trace` | — | — | coverage sheet |
| `boundary` | — | — | "About these figures" |

### Time policy

| Field | Value |
|---|---|
| `candidate_presets` | `[7d, 30d, last_complete_week, last_complete_month]`; custom disabled |
| `default_preset` | `7d` |
| `comparison_policy` | `optional_equal_prior`, which applies on the rolling presets only. A calendar preset shows no comparison: `status: not_applicable`, reason `calendar_period_selected` |
| `display_grains` | `[period]` |
| `temporal_control_version` | 2, because of the calendar presets and the section `declared_periods` [repo: `src/domain/presentation/page_schemas.py:1196-1204`] |

- **Rolling presets** resolve as on the other tabs: the latest window whose every day is covered.
- **Calendar presets** resolve through `resolve_calendar_preset` [repo: `src/domain/presentation/calendar_presets.py:194`].
  The anchors are `new-to-brand` and `gateway-products`, which read daily operands only, so the period *P*
  resolves on daily coverage alone: the latest complete ISO week or calendar month (UTC) whose every day is
  covered. The materializer passes `new-to-brand-step-2` with `period_anchor: false`, so a late new-buyers
  delivery shows its pending text for *P* and never holds back the rest of the tab. Every section then shows
  *P*; nothing on the page shows another period.
- **The step-2 section isn't an anchor**, which differs from the information architecture's §9 sketch. With it
  anchoring, the week or month the whole tab shows would wait on the new-buyers pack, so step 1 and gateway
  products would show an older period than their data supports. This follows the Reach and funnel
  `frequency` precedent (design §4.7 item 4).
- Calendar presets need temporal transport v2 in the browser, so the tab ships with it and not before (§10).
  There's no rolling-only declaration of this page: a version-1 control refuses section `declared_periods`
  [repo: `src/domain/presentation/page_schemas.py:1197-1204`], and the step-2 absence text would point at a
  preset the page doesn't offer.

### Pointer lines

The `pointers` section holds two governed lines. Each renders only when the session's navigation manifest
lists its target tab, by tab id, which is the target's page id (the rule is in the
[Retail health page action](../retail-health/low-level-design.md#page-action-repeat-purchase-by-product-added-2026-10-08-design-revision-2026-10-081)):

| Order | Text | Tab id | Link |
|---|---|---|---|
| 1 | "Which audience segments your buyers belong to is on Reach and audiences › Audiences." | `audiences` | the words "Reach and audiences › Audiences" link to `/audiences/` |
| 2 | "Whether new customers buy again is on Customers › Retention." | `retention` | the words "Customers › Retention" link to `/retention/` |

Neither line is a declared page action, because the schema refuses an action whose target page isn't declared
[repo: `src/domain/presentation/page_schemas.py:314`], and neither target page exists yet. So the tab can ship
with neither line showing, and each line appears with no page change when its target's page entry lands.

### `NewCustomersArtifact` v2

Revised at `2026-10-09.2` (business outcomes wave 2b, plan D5; owner S-3 revision, 2026-10-09). v2 adds
the per-channel new buyers to step 2 and replaces v1 in the same change: v1 was built on the server but never
served, because its page entry doesn't exist, so no v1 serializer or decoder stays beside v2. The v2 additions are all in step 2:

- `new_buyers.per_channel`: `{state, text, rows, note}`. `state` is `rows`, `no_channels`, `pending` or `missing`;
  `rows` is a list, each row with `ad_product_label`, `channel_group_label`, `new_buyers` and
  `cost_per_new_buyer`, each figure a value or a typed absence `{reason, text}`; `note` is the degraded
  note, present only when the roster's loss is graded degraded.

Every other field is v1's, unchanged.


Every figure is either a value, with the server's `display_value` (currency at two decimal places,
[ADR-084](../../decisions/adr-084-display-monetary-values-at-currency-scale-from-a-server-display-value.md)),
its unit and its basis label, or a typed absence `{reason, text}`. The browser never computes a value, a
share, a change or a total. Each section is per `marketplace_key × currency_code`; with two marketplaces each
section repeats once per marketplace, never one table across two [proposal].

**Step 1: `new_to_brand`.** One row per governed ad product with delivery in the window, in taxonomy order.

| Field | Column | Metric id | Unit | Nullable, with reason |
|---|---|---|---|---|
| `ad_product_label`, `channel_group_label` | Channel, with its channel group under it | — | — | no |
| `ntb_purchases` | New-to-brand purchases | `purchases_new_to_brand_12mo` | count | no. In a covered window an ad product with delivery and no attributed-conversion row reads 0, a true zero, as on the Scorecard [repo: `src/adapters/jobs/preset_artifact_publisher/scorecard_source.py:489-508`] |
| `ntb_revenue` | New-to-brand revenue | `revenue_new_to_brand_12mo` | currency | no; 0 in the same case |
| `ntb_revenue_share` | New-to-brand revenue share | `ntb_revenue_share` | rate, shown as a percentage | typed absence when attributed revenue is zero, including an ad product with no attributed-conversion row |
| `ntb_revenue_share_change` | Change in share | the server `change` block on `ntb_revenue_share` ([ADR-121](../../decisions/adr-121-declare-change-direction-and-sentiment-on-the-server.md)) | percentage points | rolling presets only. On a calendar preset the field is absent with `calendar_period_selected` and the column isn't rendered [proposal]; with no share for the window or its prior, it's a typed absence |
| `cost_per_ntb_purchase` | Cost per new-to-brand purchase | `cost_per_ntb` | currency | typed absence when new-to-brand purchases are zero, including an ad product with no attributed-conversion row |

There's no total row and no channel-group subtotal row: one purchase can be credited to more than one channel
([ADR-106](../../decisions/adr-106-keep-outcome-metrics-inside-one-network-by-type.md)).

**Insights: `insights`.** The fixed roster `portfolio-acquisition` v1, with slot states `filled`, `silent` and
`unavailable` ([insight-presentation contract](../insight-presentation-contract.md)).

**Step 2: `new_buyers`.**

| Field | Tile | Metric id | Unit | Nullable, with reason |
|---|---|---|---|---|
| `state` | — | — | — | one of `figures`, `outside_declared_periods`, `pending`, `missing`. A calendar preset with no complete period measured is the page-level `no_complete_period_measured` state, which carries no artifact |
| `text` | the callout or the section line | — | — | present for every state but `figures`, with the server text from "Designed states". `pending` and `missing` texts are built at serve time from `expected_after` and `latest_available_period` |
| `action` | the callout's button | — | — | `{label, preset}`, present only for `outside_declared_periods`: `{"Show Latest measured week", last_complete_week}` |
| `new_buyers` | New buyers | `buyers_new_to_brand_12mo` (wave 2): distinct shoppers, `exact_distinct`, periodic, `period_grains: [week, month]` | count | present only when `state` is `figures`; then absent with `not_returned` when the period's row didn't come back |
| `cost_per_new_buyer` | Cost per new buyer | `cost_per_new_to_brand_buyer_12mo` (wave 2): `ad_spend` ÷ `buyers_new_to_brand_12mo`, spend aligned to the days of *P* (D-6) | currency | as `new_buyers`; absent with `not_returned` when `new_buyers` is, and with `no_new_buyers` when `new_buyers` came back 0 |
| `expected_after`, `latest_available_period` | — | — | — | baked for the per-request pending or missing choice ([ADR-107](../../decisions/adr-107-derive-paid-feature-availability-from-delivery-evidence.md), design §3e) |
| `per_channel.state` | — | — | — | present only when `state` is `figures`: `rows`, `no_channels`, `pending` or `missing`. The per-channel figures come from their own pack (business outcomes pack 2), whose delivery for *P* can land after the platform pack's, so the table has its own pending and missing states and never holds back the tiles. `no_channels` applies only once the per-channel delivery for *P* has landed; before that, `pending` or `missing` applies, even with an empty roster |
| `per_channel.text` | the line that replaces the table | — | — | present for `no_channels`, `pending` and `missing`; the last two built at serve time from the per-channel delivery's own `expected_after` and `latest_available_period`, with the texts in "Designed states" |
| `per_channel.rows` | the per-channel table | — | — | present only for `rows`; the rows are below |
| `per_channel.note` | the line under the table | — | — | `DEGRADED_NOTE` [repo: `src/domain/shopper_aggregates/loss.py:24`] when the roster loss is graded degraded; otherwise absent |

The two tiles are for the whole Amazon Ads network in one marketplace (grain set `platform`), and they stay.
The per-channel table under them breaks the same period *P* down by ad product. Neither shows new buyers per
campaign; per-campaign new buyers are answered in chat only (business outcomes pack 3).

**Per-channel rows: `new_buyers.per_channel.rows[]`.** For the period *P*, one row per ad product on the
ad-product roster: the ad products with `ad_spend > 0` in `fct_ad_exposure` for *P*, the marketplace and the
platform (design §4.5), in taxonomy order, the same order as step 1.

| Field | Column | Metric id | Unit | Nullable, with reason |
|---|---|---|---|---|
| `ad_product_label`, `channel_group_label` | Channel, with its channel group under it | — | — | no |
| `new_buyers` | New buyers | `buyers_new_to_brand_12mo` at grain set `channel` (one row per ad product), `exact_distinct`, periodic | count | typed absence `not_returned` when the roster's ad product has no row for *P* |
| `cost_per_new_buyer` | Cost per new buyer | `cost_per_new_to_brand_buyer_12mo` at grain set `channel`: that ad product's `ad_spend` aligned to the days of *P* (D-6) ÷ its `buyers_new_to_brand_12mo` | currency | absent with `not_returned` when `new_buyers` is; absent with `no_ad_spend` when the ad product is off the roster; absent with `no_new_buyers` when its `new_buyers` came back 0 |

- **No total or subtotal row.** One shopper can buy after ads on more than one channel, so per-channel new
  buyers don't add up to the platform tile, and the table never shows a sum. The foot says so (labels below).
- **A rostered ad product with no row** stays in the table: both figures read "Not available" with
  "too few shoppers, or none" (design §4.5). A suppressed row and a true zero look the same, so the text
  claims neither.
- **A returned ad product that isn't on the roster** (a row for *P* with no ad spend in *P*) is listed in
  taxonomy order with its new buyers; its cost per new buyer is a typed absence with "No ad spend for
  {channel} in {period label}." [proposal]
- **Graded only.** The roster has at most five entries, the Amazon Ads ad products in
  [the media taxonomy](../../../contracts/shared/media-taxonomy.yaml), so it's under the 20-entry
  withdrawal minimum and is never withdrawn (design §4.4; `WITHDRAW_MIN_CELLS` [repo:
  `src/domain/shopper_aggregates/loss.py:22`]). Its loss, roster entries with no row ÷ roster entries, is
  computed at bake. When it's graded degraded, `per_channel.note` carries `DEGRADED_NOTE`.
- **An empty roster.** When no ad product had ad spend in *P* and the landed per-channel delivery for *P* has
  no row (before it lands, `pending` or `missing` applies), the roster's
  loss is 0 ÷ 0, which grades "not degraded", so no note shows. `per_channel.state` is `no_channels` and the
  line "No channel had ad spend in {period label}." [proposal] replaces the table; the tiles render. A
  returned row with an empty roster is listed as above, under `rows`.
- On a rolling preset, or when the platform delivery for *P* is pending or missing, the section shows its
  callout or line and no table, as in v1, even when the per-channel delivery for *P* has landed [proposal].

**Gateway products: `gateway`.** One entry per ad product that has a step-1 row, in the same order; the
artifact carries every channel, so the selector switches with no re-request.

| Field | Column | Metric id | Unit | Nullable, with reason |
|---|---|---|---|---|
| `channel_label` | the selector option | — | — | no |
| `state` | — | — | — | `rows`, `no_ntb_purchases` or `not_covered` |
| `text` | the line that replaces the table | — | — | present for `no_ntb_purchases` and `not_covered`, with the server text from "Designed states" |
| `rows[].asin`, `rows[].title` | Product, with the ASIN under it | — | — | the title comes from the effective-title resolver, as on Repeat purchase by product |
| `rows[].ntb_purchases` | New-to-brand purchases, brand halo included | `purchases_new_to_brand_incl_halo_12mo` (wave 2) at ad product × `tracked_asin`, summed across that channel's campaigns only | count | no; a product with no purchases isn't listed |
| `rows[].share_of_channel` | Share of channel new-to-brand purchases | the row's `purchases_new_to_brand_incl_halo_12mo` ÷ the same metric at that ad product's grain, built in the domain from the two planned answers | rate, shown as a percentage | no |
| `more_count` | the foot line "And {N} more products." | — | count | absent when every product is listed |
| `first_covered_date` | — | — | date | present when `state` is `not_covered` and the window's last day is covered (see "Gateway coverage") |

- Rows are ordered by new-to-brand purchases, highest first, then by ASIN; at most 10 per channel [proposal].
  The shares use the channel's whole total, so the listed shares can add up to less than 100%.
- The share's denominator is `purchases_new_to_brand_incl_halo_12mo` at the ad product, never step 1's
  `ntb_purchases`, which excludes brand halo. The mockup's shares are fictional and aren't a check of this.
- `state` is `no_ntb_purchases` when `purchases_new_to_brand_incl_halo_12mo` at the ad product is zero or has
  no row for a covered window, whatever step 1 shows. A channel with no advertised new-to-brand purchases can
  still list products bought through brand halo.

### Gateway coverage

The halo-inclusive column exists only on `amc-attributed-conversion` deliveries produced at query pack
version 2 or later. A day is covered for gateway products when the delivery currently published for that day
carries `query_pack_version` 2 or later in its landed manifest [repo:
`src/domain/shared_kernel/provenance.py:111`]. Row presence plays no part: a covered day with no purchases is
covered and counts zero [proposal].

- `first_covered_date` is the earliest day from which every day through the window's last day is covered.
  Every uncovered day of the window lies before it, so "This window starts earlier" stays true even when a
  pack-version-1 re-run lands after version 2 has been published.
- A window is covered when every one of its days is. Otherwise every channel's entry is `not_covered`, never
  a partial list.
- The metric is `exact_only` on `channel` and `ad_product` (design §6.2), so there's no all-channel product
  list and no product total across channels.

**Other fields.**

| Field | Content |
|---|---|
| `pointers` | The two governed lines above, each with its tab id |
| `evidence_status` | `EvidenceStatus.v2` for every row and tile with a change chip ([ADR-119](../../decisions/adr-119-serve-window-coverage-as-one-server-written-evidence-status-block.md)) |
| `page_context_label`, `page_context_expires_at`, `starter_questions` | "New customers · {window label}" and one starter, "Which channel had the highest new-to-brand share in {window label}?" ([ADR-120](../../decisions/adr-120-carry-page-context-to-chat-as-a-server-redeemed-reference.md)) |
| `boundary` | The two fixed statements under "About these figures" below |

### Labels and fixed text (server-built)

| Where | Text |
|---|---|
| Scope band, rolling preset | "Showing {window} ({preset label}) compared with {prior window}. Data through {date}." {preset label} is the server label from `temporal_preset_label`, "7D" or "30D" [repo: `src/domain/presentation/temporal.py:63-71`] |
| Scope band, calendar preset | "Showing {period label} ({preset label}). No comparison for calendar periods." {preset label} is "Latest measured week" or "Latest measured month" from the same function |
| Step 1 heading and table caption | "New-to-brand purchases"; caption "New-to-brand purchases per channel", then "Each row names its channel group. Amazon-attributed · 14-day · conversion date · new to brand in the last 12 months · excludes brand halo · {window label}". The halo scope is read from `Metric.includes_brand_halo` [repo: `src/domain/catalog/capability.py:361`]. Wave 2 declares `includes_brand_halo: false` on `purchases_new_to_brand_12mo`, which declares none today [repo: `contracts/shared/metric-dictionary.yaml:429`], and `includes_brand_halo: true` on `purchases_new_to_brand_incl_halo_12mo`. The Scorecard, by contrast, maps each metric id to a basis kind in code [repo: `src/domain/presentation/scorecard.py:244-249`] |
| Gateway column header | "New-to-brand purchases, brand halo included", so it never reads as step 1's halo-excluding count |
| New-to-brand revenue share basis, in the column header | "New-to-brand revenue (12-month lookback) ÷ attributed revenue (14-day)". The share uses two windows by construction and never reads as one [repo: `contracts/shared/metric-dictionary.yaml:945-950`] |
| Step 1 foot | "Attributed sales aren't added across channels, because one purchase can be credited to more than one channel." |
| New buyers basis | "Ad-attributed buyers · 14-day attribution · conversion date · advertised products · {period label}" (design §3b), followed by the metric's identity caveat (design §3f) |
| Cost per new buyer basis | "Ad spend ÷ new buyers · {period label} · {ad spend} ÷ {new buyers}" |
| Per-channel table caption | "New buyers per channel", then "Each row names its channel group." and the New buyers basis above |
| Per-channel cost column header | "Cost per new buyer", with the basis "Ad spend ÷ new buyers · {period label}" |
| Per-channel table foot | "New buyers aren't added across channels, because one shopper can buy after ads on more than one channel." |
| Gateway caption title | "Products new-to-brand shoppers bought first, {channel}" |
| Gateway captions, verbatim from design §6.2 | "Includes products bought because of brand halo, not only the products you advertised." and "Credited per channel. One purchase can be credited to more than one channel or campaign, so channels aren't added together." |
| About these figures | "Amazon flags a purchase as new to brand when the shopper hasn't bought from the brand in the previous 12 months." and "New-to-brand revenue share divides a 12-month new-to-brand numerator by 14-day attributed revenue, so it uses two windows." |

### Designed states

Every text is a server field. The browser never maps a reason code. A typed absence on a figure renders
"Not available" with its `text` under it; the Field column names where each text travels.

| Condition | Field | What renders |
|---|---|---|
| Step 1, a covered window where an ad product with delivery has no attributed-conversion row | `ntb_purchases`, `ntb_revenue`, `ntb_revenue_share`, `cost_per_ntb_purchase` | A true zero, never a missing figure: `ntb_purchases` and `ntb_revenue` read 0, and the share and cost take the two zero texts below. Step 1 has no "didn't come back" state, because a window resolves only when every day is covered |
| Step 1, attributed revenue is zero | `ntb_revenue_share` | "Not available" with "No attributed revenue in this window, so there's no share." [proposal] |
| Step 1, the window or its prior has no share (rolling presets) | `ntb_revenue_share_change` | "Not available" with "No new-to-brand revenue share for {prior window or window}, so there's no change." [proposal] |
| Step 1, new-to-brand purchases are zero | `cost_per_ntb_purchase` | "Not available" with "No new-to-brand purchases in this window." [proposal]. Step 1 keeps the row |
| Rolling preset (`7d`, `30d`) in the New buyers section | `new_buyers.text`, `new_buyers.action` | One callout text from `declared_period_absence_text` with subject "New buyers" (plural) [repo: `src/domain/presentation/calendar_presets.py:306`]: "New buyers are measured for complete weeks and months. Choose Latest measured week to see them.", with the button "Show Latest measured week", which selects that preset. No tile renders |
| Calendar preset, new-buyers row for *P* didn't come back (`not_returned`) | `new_buyers.new_buyers`, `new_buyers.cost_per_new_buyer`: each `{reason, text}` | Both tiles read "Not available" with the reason "too few shoppers, or none" (design §4.5). The tile never says the figure was withheld, because a suppressed row and a true zero look the same |
| Calendar preset, the new-buyers row for *P* came back with 0 new buyers (`no_new_buyers`) | `new_buyers.cost_per_new_buyer`: `{reason, text}` | New buyers read 0; cost per new buyer reads "Not available" with "No new buyers in {period label}." (approved, owner ratified 2026-10-10), because its spend ÷ 0 has no value. The per-channel table renders |
| Calendar preset, the new-buyers delivery for *P* hasn't landed and is still due | `new_buyers.text` | "Figures for {period label} are expected after {date}." [repo: `src/domain/presentation/calendar_presets.py:287`] |
| Calendar preset, the new-buyers delivery for *P* is past due | `new_buyers.text` | "Figures for {period label} didn't arrive." [repo: `src/domain/presentation/calendar_presets.py:290`] |
| Per-channel table, a rostered ad product with no row for *P* (`not_returned`) | `per_channel.rows[].new_buyers`, `per_channel.rows[].cost_per_new_buyer`: each `{reason, text}` | The row stays; both cells read "Not available" with "too few shoppers, or none" (design §4.5) |
| Per-channel table, a returned ad product with no ad spend in *P* | `per_channel.rows[].cost_per_new_buyer` | "Not available" with "No ad spend for {channel} in {period label}." [proposal]; new buyers render |
| Per-channel table, a rostered ad product whose row for *P* came back with 0 new buyers (`no_new_buyers`) | `per_channel.rows[].cost_per_new_buyer` | The row stays; new buyers read 0; cost per new buyer reads "Not available" with "No new buyers for {ad product} in {period label}." (approved, owner ratified 2026-10-10). Every other row and section renders |
| Per-channel table, an empty roster and the landed per-channel delivery for *P* has no row | `per_channel.state` `no_channels`, `per_channel.text` | "No channel had ad spend in {period label}." [proposal] in place of the table; no degraded note (0 ÷ 0 grades not degraded); the tiles render |
| Per-channel table, roster loss graded degraded | `per_channel.note` | `DEGRADED_NOTE`, "Some breakdowns had too few shoppers and aren't shown.", under the table. Never withdrawn |
| Per-channel table, the per-channel delivery for *P* hasn't landed and is still due | `per_channel.text` | "Figures per channel for {period label} are expected after {date}." [proposal]; the tiles render |
| Per-channel table, the per-channel delivery for *P* is past due | `per_channel.text` | "Figures per channel for {period label} didn't arrive." [proposal]; the tiles render |
| Calendar preset with no complete period measured | the page's `window_unavailable` | The `no_complete_period_measured` page state: "No complete {week or month} has been measured yet. The first is expected after {date}." [repo: `src/domain/presentation/calendar_presets.py:218-222`]; no artifact and no scope band |
| Gateway products, the window isn't covered and its last day is | `gateway[].text`, with `first_covered_date` | "Gateway products are measured from {first covered date}. This window starts earlier, so they aren't shown. They appear once every day of a window is measured." [proposal] |
| Gateway products, the window's last day isn't covered and an earlier day has been | `gateway[].text` | "Gateway products aren't measured for every day of this window, so they aren't shown. They appear once every day of a window is measured." [proposal] |
| Gateway products, no day has been covered | `gateway[].text` | "Gateway products aren't measured yet. They appear once every day of a window is measured." [proposal] |
| Gateway products, a covered window with no halo-inclusive new-to-brand purchases for the channel | `gateway[].text` | "No new-to-brand purchases were credited to {channel} in this window." [proposal]. Decided on `purchases_new_to_brand_incl_halo_12mo`, never on step 1 |
| Insight slot silent or unavailable | `insights` slot state | The insight-presentation contract's absence states. On a calendar preset there's no comparison, so `ntb-share-mover` is silent |
| Loading | — | Skeletons in the order of the sections, and a status "Loading New customers" for assistive technology |
| No published body for the preset (page-level) | the page's `window_unavailable` | Page-level absence: [ABS-NC-1](../page-absence-copy.md#abs-nc-1) (rolling) or [ABS-NC-2](../page-absence-copy.md#abs-nc-2) (calendar), with [ABS-ALT](../page-absence-copy.md#abs-alt) when another preset is published; no artifact and no scope band |

### Insight contract: `portfolio-acquisition` v1

Output class perspective (design §6.2) with no prompt: the card text comes from a fixed rule, and the dialog
says "Found by a fixed rule from the figures above. Not written by an AI model." One deterministic finding,
`ntb-share-mover`: the change in `ntb_revenue_share` for one ad product inside the Amazon Ads network between
the window and its equal prior window, with materiality per horizon following the `portfolio-exposure`
pattern. It reads step-1 daily metrics, so design D-8 doesn't apply. It's silent on calendar presets.

- **Card.** Kind "New-to-brand share mover"; title "{ad product} new-to-brand share {rose or fell} {n} points", with "point" when *n* is 1;
  body "Inside {ad product} only, {window} compared with {prior window}."; "Read more ›" and "Ask the
  assistant".
- **Dialog.** "What stands out": the new-to-brand revenue share with its change, the new-to-brand revenue and
  the attributed revenue, then the basis "New-to-brand revenue (12-month lookback) ÷ attributed revenue
  (14-day): two windows, never one". "How this is calculated" names Amazon Marketing Cloud as the source, the
  basis, and the window: the attributed-revenue window and its prior, with each purchase looking back 12 months
  for the new-to-brand flag.
- **Calibration.** A known-correct single-channel mover; a rejection case that adds two channels' shares; a
  rejection case that sums channel buyer counts.

### "Also changed"

The alerts-declined path applies (OD-7). No default alert rule covers `portfolio-acquisition`
([ADR-108](../../decisions/adr-108-raise-in-app-alerts-when-page-artifacts-publish.md)), so in v1 this tab has
no "Also changed" section. A later rule brings it in after the insights row with no page change.

### Materializer

New, under the ADR-075 pattern. Per preset and per marketplace it plans every figure through the measurement
layer: the step-1 batch at ad-product grain with the prior window, the step-2 periodic figures for *P* at
grain set `platform` and, for the per-channel table, at grain set `channel` with the ad-product roster
applied, and the gateway batch at ad product × `tracked_asin` with its channel totals. It also bakes the deterministic findings.
It's registered with the publisher and fails closed until the `new-customers` v2 page entry exists. Today only
Campaigns and Retail health have real materializers; other pages reach the typed unavailable fallback
([preset artifact publisher](../preset-artifact-publisher.md)).

## 7. Concepts applied

- [Business outcome measurement](../../concepts/business-outcome-measurement.md)
- [Preset artifact publisher](../preset-artifact-publisher.md)
- [Hybrid temporal-control contract](../temporal-control-contract.md)
- [Insight presentation contract](../insight-presentation-contract.md)
- [Theme preference and restrained glass](../theme-preference-contract.md)

## 8. Decisions

- [ADR-099: distinct shopper counts as a new kind of fact](../../decisions/adr-099-treat-distinct-shopper-counts-as-a-new-kind-of-fact.md)
- [ADR-100: serve periodic metrics only for complete measured periods](../../decisions/adr-100-serve-periodic-metrics-only-for-complete-measured-periods.md)
- [ADR-106: keep outcome metrics inside one network by type](../../decisions/adr-106-keep-outcome-metrics-inside-one-network-by-type.md)
- [ADR-108: raise in-app alerts when page artifacts publish](../../decisions/adr-108-raise-in-app-alerts-when-page-artifacts-publish.md) (deferred; fallback path)
- [ADR-115](../../decisions/adr-115-organise-the-app-into-customer-question-hubs-from-one-navigation-file.md), [ADR-116](../../decisions/adr-116-give-every-tab-one-time-model-and-one-window.md), [ADR-118](../../decisions/adr-118-render-every-page-from-one-set-of-shared-patterns-and-extended-tokens.md) to [ADR-121](../../decisions/adr-121-declare-change-direction-and-sentiment-on-the-server.md)

## 9. Delivery prerequisites and acceptance

| Concern | Required proof |
|---|---|
| Page declaration | v1 and v2 entries pass `make validate-page-schemas`; the step-2 section declares `declared_periods` and `period_anchor: false`; the generated bindings and route manifest list `/new-customers/` |
| Artifact and decoders | `NewCustomersArtifact` v2 exact serializer and strict browser decoder: basis labels, typed absences, `change` blocks, `EvidenceStatus.v2`, step-2 states including `per_channel`, gateway states. No v1 type or serializer remains |
| No cross-channel total | An artifact test fails on a step-1 total or subtotal row, a per-channel new-buyers total or subtotal row, a gateway product total across channels, or a starter question that states one |
| Per-channel new buyers | Unit tests, each paired with a row that has its figures: a rostered ad product with no row gives both cells "too few shoppers, or none"; one of four roster entries missing is graded degraded with `DEGRADED_NOTE` and never withdrawn; a returned ad product with no spend gives the no-spend text; a returned ad product with spend and 0 new buyers gives `no_new_buyers` with its text while the rest of the artifact bakes, paired with a row with new buyers (a numeric cost) and a row that didn't come back (`not_returned`), and the tiles the same way; a pending and a missing per-channel delivery give their texts while the tiles render; an empty roster with no row gives `no_channels`, exactly the no-channels line and no note, paired with an empty roster and one returned row, which gives `rows`; an empty roster before the per-channel delivery for *P* lands gives `pending`, never `no_channels`; the per-channel figures are read at grain set `channel`, never derived from the platform row |
| Step 1 absences | Unit tests, each paired with a row that has the figure: zero attributed revenue, a missing prior share and zero new-to-brand purchases each give exactly their text above. An ad product with spend and no attributed-conversion row in a covered window gives `ntb_purchases` 0, `ntb_revenue` 0 and the two zero texts; paired with a row that has purchases, which gives every figure |
| Step 1 halo label | Unit tests: the caption reads "excludes brand halo" from `includes_brand_halo: false` on `purchases_new_to_brand_12mo`; the gateway metric declares `true` (paired) |
| Step 2 | Unit tests: a rolling preset gives `new_buyers.text` and `new_buyers.action` exactly as above; a calendar preset gives both tiles; `not_returned` gives "too few shoppers, or none"; pending and missing give their texts; no complete period gives the page state with no artifact |
| Gateway coverage | Unit tests: a window with one pack-version-1 day gives the coverage text naming the first covered date, and a fully covered window gives the rows (paired); a covered day with no purchase rows counts as covered; a pack-version-1 last day gives the "every day" text |
| Gateway no purchases | Unit tests: step-1 new-to-brand purchases zero with halo-inclusive purchases above zero gives rows; both zero gives the "No new-to-brand purchases were credited" text (paired) |
| Pointer lines | A frontend test: with no `audiences` tab in the session's manifest, no Audiences line renders; with it, the line links to `/audiences/`. The same pair for `retention` |
| Insight contract | `portfolio-acquisition` v1 with its calibration corpus, shipped through the registry path |
| Materializer | Fails closed with no page entry; with an injected entry, the baked figures equal the planned answers |
| Conformance | Rule 4 of the [delivery slices](../information-architecture/delivery-slices.md#2-rules-for-every-slice) against every mockup state in this package, and against this document for the copy points in §10. Only `mockup-last-complete-month.html` draws the per-channel table; on the week state this document governs it. The step-1 table, the insights row and gateway products in `mockup-30d.html`, `mockup-last-complete-week.html` and `mockup-last-complete-month.html` keep the 7-day figures, change column and insight card ("Mockup only"), so this document governs those regions there: no change column and a silent `ntb-share-mover` on a calendar preset. No figure in any mockup is a check of a computation |
| Live | Deployed dev bake observed end to end and recorded in `validation/RESULTS.md`; real Amazon Marketing Cloud figures wait on the owner-approved runs |

## 10. Open items

The UI workstream owns the first three.

| Item | What resolves it | Blocks |
|---|---|---|
| Temporal transport v2, browser half | The carried transport v2 slice | The tab. It ships with all four presets or not at all (§6, time policy) |
| The `new-customers` API route | An `infra/` approval with the UI sub-slice (business outcomes wave 2, stop item S-9) | The tab |
| Prompt-admin pairing test | The UI sub-slice's test that Prompt administration lists `portfolio-acquisition` only when the `new-customers` tab is in the session's manifest | Listing the contract before the tab exists |
| Rolling-preset absence copy. The approved mockup reads "New buyers are counted for complete weeks and months"; the shared designed text reads "measured" (design §4.7 item 5) | Owner ratification of this package (S-1) | Nothing; the build uses the shared text |
| New buyers basis label. The approved mockup's tile reads "Distinct shoppers new to brand in 12 months · {period} · shoppers (Amazon account IDs and other IDs)"; this document uses the design §3b label built from the dictionary | Owner ratification (S-1) | Nothing |
| Cost per new buyer label. The approved mockup's week state omits the operands its month state shows; this document shows them on both | Owner ratification (S-1) | Nothing |
| Halo scope labels. The approved mockup's step-1 caption has no halo scope and heads both tables "New-to-brand purchases"; this document adds "conversion date" and "excludes brand halo" to the step-1 caption and heads the gateway column "New-to-brand purchases, brand halo included" (design §3b) | Owner ratification (S-1) | Nothing; the build uses this document's labels |
| Scope band preset label. The approved mockup reads "(7 days)"; the server label is "7D" | Owner ratification (S-1) | Nothing; the build uses the server label |
| Gateway list bound. The approved mockup lists every product; this document lists at most 10 per channel with the foot "And {N} more products." | Owner ratification (S-1) | Nothing |
| Change in share on calendar presets. The approved mockup's week and month states show the column; this document doesn't render it there, because calendar presets have no comparison (design §4.7) | Owner ratification (S-1) | Nothing |
| Per-channel new buyers (revised at `2026-10-09.2`, owner S-3 revision, 2026-10-09). The approved mockup has no per-channel table; this revision adds it under the tiles, with the caption "New buyers per channel", the columns "New buyers" and "Cost per new buyer", no total row, and the foot "New buyers aren't added across channels, because one shopper can buy after ads on more than one channel." `mockup-last-complete-month.html` draws it | Owner ratification of manifest revision `2026-10-09.2` (wave 2b stop item S-B1) | Nothing; the build uses this document |
| Per-channel absence copy. A rostered ad product with no row reads "Not available" with "too few shoppers, or none"; a returned ad product with no spend reads "No ad spend for {channel} in {period label}."; the per-channel pending and missing lines read "Figures per channel for {period label} are expected after {date}." and "Figures per channel for {period label} didn't arrive."; a degraded roster shows `DEGRADED_NOTE`; an empty roster reads "No channel had ad spend in {period label}." | Owner ratification (S-B1) | Nothing |
| No-new-buyers copy (owner ratified 2026-10-10, FEAT-004 review A2-1). A returned row can hold 0 new buyers above the privacy bar, so cost per new buyer has no value: the per-channel cell reads "No new buyers for {ad product} in {period label}." and the tile "No new buyers in {period label}.", reason `no_new_buyers`; new buyers read 0 | Approved: owner ratified 2026-10-10 | Nothing |
| Per-channel table behind the platform tiles [proposal]. When the platform step-2 delivery for *P* is pending or missing, the section shows that line and no per-channel table, even when the per-channel delivery has landed. Reason: `per_channel` nests under `state: figures`, and a table without the tiles has no platform figure to read against. The alternative renders the table beside the tiles' pending or missing line | Owner ratification (S-B1) | Nothing; the build uses this document |
