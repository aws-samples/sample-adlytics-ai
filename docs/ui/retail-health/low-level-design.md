# Retail health — low-level design

## 6. Wire, artifact, and renderer contracts

### Exact API inputs

| Route | Method/auth | Accepted body | Explicitly forbidden |
|---|---|---|---|
| `/api/presentation/retail-health` | `POST`, custom request authorizer | `{session_id}`, optionally `temporal_selection: TemporalSelection.v1` restricted to `omitted` or a declared preset, `sort: {field, direction}` over the artifact-declared sortable columns, and `product_ads: true` (exactly `true`, never with `sort`, because the part keeps the baked row order; [Products and ads](../products-and-ads/README.md) sends it). | owner, canonical product selectors, raw ASINs as filters, selection reference, caller comparator, raw `visible_window`, custom temporal selection ([ADR-070](../../decisions/adr-070-serve-declared-preset-windows-from-publish-time-artifacts.md)), undeclared sort fields, unknown fields. |
| `/api/presentation/retail-metric-focus` (added 2026-09-17, [shared metric-focus contract](../metric-focus-contract.md)) | `POST`, same authorizer | `{session_id, metric, temporal_selection}` where `metric` is one of the page's declared focusable metrics and the selection is restricted to `omitted` or a declared preset. The server derives the focus payload — definition line, facts, per-product contribution drivers, and the `retail-metric-focus` prompted slot — from the same baked artifact the page was served from; no live measurement. | any window or grain field beyond the preset selection, product selectors, custody references, unknown fields. |

The page follows the shared [temporal-control contract](../temporal-control-contract.md):
`PresentationResult.v2` with `TemporalControl.v1` and either the page artifact or
`window_unavailable`. There is no child route: product rows are display rows, not selection
capabilities.

### Window policy

Candidate presets `1d`, `7d`, `14d`; default `7d`; comparison `required_equal_prior`;
display grains `period`, `day`; `custom.availability` always `"unavailable"`. `30d` is not a
candidate, so the control returns it unavailable. The bake's chunked two-pass snapshot would read
it (ADR-070 amendment, 2026-10-25); declaring it is this page's own decision. Window
resolution anchors to the newest published coverage of this page's vendor sources, never to
wall-clock today. Default resolution uses the shared bake-time ladder owned by the
[preset artifact publisher](../preset-artifact-publisher.md); every declared preset must pass
the build-time bake-ability check.

### `RetailHealthArtifact.v2`

Version 2 (2026-09-17) added the derived returns-and-margin section below; the wire `kind`
stays `retail_health` and both strict decoders validate the widened field sets, so the
encoder and browser decoder always land in one deploy.

> **Amended 2026-10-01 ([ADR-084](../../decisions/adr-084-display-monetary-values-at-currency-scale-from-a-server-display-value.md), B4).** Every totals and product-row comparison `current` and
> `prior` carries a server-derived `display_value`: two places for currency (ties away from
> zero), `null` otherwise. Trend points and `shipped_revenue_spark` entries keep the exact
> five-field object with no `display_value`; they are plotted, never shown as text. The
> artifact stays at version 2; retained bodies evolve through render format 4, which also
> carries the two-place movers text. The domain decoder accepts a retained body in which no
> comparison value carries `display_value`, and refuses a mixed body or a wrong value (the
> tolerant canonical rule), until every environment's CURRENT pointer is at render format 4
> and one `PAGE_TTL` (1 h) has passed since the last ApiStack deploy.

> **Amended 2026-10-07 ([ADR-121](../../decisions/adr-121-declare-change-direction-and-sentiment-on-the-server.md)).** Every
> totals and product-row comparison carries the server's `change` block in place of
> `percentage_change`, and the tiles render it as the change chip, with the tone from the
> metric dictionary's `favourable_direction`. Return rate and fulfilment rate change in
> percentage points. Conversion changes in percent, because the page shows it rescaled as units
> per 1,000 glance views, never as a percentage (`PRESENTATION_RESCALED_RATES`); the approved
> mockup reads it "+0.1%". The Amazon margin proxy reads "Up from a loss" after a negative
> prior. Retained bodies evolve through render format 5: a format-4 body carries the percentage
> and no `change` key, decodes unchanged, is served in that baked form (never with
> `change: null`), and its chips read "Change unavailable" until its pointer moves.
> **Retired 2026-10-09:** the retained decode is gone from both sides, so a format-4 body is
> refused (owner revision of 2026-10-08: the API, a full re-bake and the browser deploy
> together).
> **Amended 2026-10-07 ([ADR-119](../../decisions/adr-119-serve-window-coverage-as-one-server-written-evidence-status-block.md)).** Render
> format 6 adds the window's `evidence_status` coverage block to a complete body, one entry per
> served tile in totals order (`null` on an empty or insufficient body). A missing or partial day
> withholds the figure's change on tiles and product rows; a partial day's count names the
> products that reported, and a ratio leaves the day out of both parts
> ([insight presentation contract](../insight-presentation-contract.md) §2.4). A format-5 body
> carries no key and reads as uncovered. **Retired 2026-10-09:** the format-4 retained decode
> and the format-5 block-less read are gone from both sides. A body without the key is refused,
> and so is a complete body whose block is `null` (owner revision of 2026-10-08).
> **Amended 2026-10-07 ([ADR-122](../../decisions/adr-122-declare-next-step-targets-as-a-typed-union-checked-against-navigation.md)).** Render
> format 7 moves card next steps to `NextStep.v2`. The body bytes are format 6's; the new
> revision makes every cached insight slot re-fill
> ([insight presentation contract](../insight-presentation-contract.md) §2.3).
> **Amended 2026-10-08 ([ADR-125](../../decisions/adr-125-serve-retail-product-detail-as-a-baked-child-of-retail-health.md)).** Render
> format 8 adds ordered units, average selling price and revenue per glance view to the totals,
> shown in the four tile groups that product detail shares, plus the return-reasons and paid
> AMC retail-purchases statuses. The same bake writes one `RetailProductDetail` v1 child per
> displayed row and a private parent custody that closes the child set; the parent is
> `selectable_parent_child`, each row carries a `selection_reference`, and
> `POST /api/presentation/retail-health-product` reads a child only through the CURRENT
> pointer's custody. A format-8 body with rows and no custody is the malformed-artifact refusal,
> never rows that open nothing. A format-7 body decodes as retained: its rows serve as text and
> open no detail until its pointer moves. **Retired 2026-10-09:** that read is gone from both
> sides, so a body without the two fixed statuses is refused (owner revision of 2026-10-08: the
> API, a full re-bake and the browser deploy together). **Amended by ADR-145 (render format
> 10):** the parent custody names the served products only; each child's digests live in the
> stage-built sibling index, the stage refuses children that aren't exactly the served rows before
> any write, and the detail route reads the child through the covering index shard. A ref the index
> doesn't list is the malformed-artifact refusal. The three new tiles change the compared-metric roster,
> so Campaigns moves to render format 10 with the same body bytes. The request-time
> `retail-product` path, its route and the `retail-detail` entries retire in this release.
> **Amended 2026-10-27 (perf follow-up A2).** Render format 12 writes one `RetailProductAds` v1
> part (`pages/retail-health/{variant}/{revision}/product-ads.json`, with a private custody that
> binds its bytes) in the same closed child set as the product-detail children, so the stage
> refuses a publication without it before any write. For each served row, in row order, it
> carries the row's child ASIN, its conversion, and its ad channels (current purchases, current
> revenue and the revenue's change) or the ad section's typed absence, each copied from that row's
> child (`retail_product_ads`). A request with `product_ads: true` gets the same page plus
> `page.product_ads`: the part, read through the parent custody's index and checked against the
> body's window and rows; `null` when the body has no rows. A listed part whose bytes, window or
> rows disagree is the malformed-artifact refusal. A format-11 publication has no part: such a
> request is served as that preset's `window_unavailable`, the preset marked unavailable, until the
> page's next bake. A request without the field is served exactly as before. The body bytes are
> format 11's.

| Section | Content | Rendering responsibility |
|---|---|---|
| Totals | Compare slots, each current + equal prior with server-computed absolute and percentage change: shipped revenue, shipped units, customer returns, glance views, ordered revenue, COGS shipped, and the conversion slot below. Exact units/basis/grain/period on every value. | Tiles grouped into the page's four narrative sections (below); delta chips; exact values, no client arithmetic. |
| Conversion slot | Units per 1K glance views, server-computed, carried only when the artifact's distributor view is Manufacturing; typed absent otherwise with its plain-words reason. | Tile with the coherence caveat line, or its designed absence. |
| Sourcing absence | The ordered-figures-withheld-under-Sourcing state as a typed slot, never a zero. | The dashed typed-absent tile. |
| Derived returns and margin (2026-09-17) | Return rate, estimated revenue withheld by returns (labeled estimate), the Amazon margin proxy, and fulfilment rate — exact server-computed compare slots at total grain, plus per-product return rate and Amazon margin on each product row. Typed absences per distributor view and selling program. | Tiles inside their narrative sections with delta chips and caveat lines; the product table's return-rate and Amazon-margin columns; never client arithmetic. |
| Insights | Fixed roster: `demand-shape`, `returns-pressure`, `ordered-vs-shipped` (Perspective, prompted per ADR-068/069), `retail-movers` (Observed, fully deterministic — no prompt, no cache entry). Slot states `filled` / `silent` / `unavailable` with revision and prompt-version stamps. | The four-slot insight row with designed absence states. |
| Trend | Dated daily rows carrying every chart-switchable metric (shipped revenue, shipped units, glance views, customer returns) for current and aligned prior, so the metric switcher is a client column selection with no re-request. | Full-width chart with prior overlay, axis labels, legend. |
| Product rows | Server-ordered, bounded (initially 50, typed truncation beyond): display title, child ASIN, parent ASIN from `dim_product`, shipped revenue + delta, units, returns, glance views, spend spark-style shipped-revenue series, freshness qualifiers. At any catalog size the page shows the top 50 by current shipped revenue (then product key) with the stated total of every product. One ranked statement picks the 50 and states the product count and the two product sums over the same population; the 50's rows, series and product detail's ad figures are read for those 50 only, so the bake runs 49 plans at every preset whatever the catalog size ([ADR-143](../../decisions/adr-143-size-bake-reads-by-a-bake-row-ceiling-separate-from-chat.md), [ADR-152](../../decisions/adr-152-read-page-sized-answers-in-sql-with-no-arbitrary-row-ceiling.md)). | The product table; the browser never reorders. |
| Reconciliation | Per-window outcome comparing by-product sums (the ranked statement's population sums over every product) to `*_reported_total` rows for shipped revenue and units: `reconciled` or a dated `discrepancy` with both figures. | The reconciliation state-note; a discrepancy renders dated and explicit, never a silent blend. |
| Boundary | Fixed basis-boundary statements. | Visible boundary state-note. |

### Declared-metric prerequisite

Every rendered figure maps to a declared metric in
`contracts/shared/metric-dictionary.yaml`. The conversion slot (units per 1K glance views)
and the reconciliation comparison require **new declared metric/derivation entries before any
handler code** — metric definitions never live in code. Shipped/ordered revenue and units,
COGS, customer returns, and glance views are already declared.

### Derived returns-and-margin metrics (added 2026-09-17)

Four server-derived metrics extend the page, each requiring its dictionary entry before any
handler code, each carried on the artifact as an exact server-computed value at total and —
where declared — product grain. The mockup's section tiles and the product table's return
rate and Amazon-margin columns render exclusively from these:

Three of the section's ratios are **already declared** — the page reuses those keys exactly
and never declares a `*_retail` duplicate (one fact, one home):

| Existing dictionary key (reused) | Derivation (as declared) | Honest semantics the UI must carry (from the entry's notes) |
|---|---|---|
| `return_rate` | `customer_returns ÷ units_shipped`; total and per product. | Returns lag the period they belong to, so a recent window understates it — an operational drag indicator, never a cohort rate. Null under the FRESH selling program — typed absence, never a zero. |
| `fulfilment_rate` | `units_shipped ÷ units_ordered`; total and per product. | MANUFACTURING view only — `units_ordered` is null under SOURCING, so the whole metric is a typed absence there. |
| `revenue_per_glance_view` | `retail_revenue_shipped ÷ glance_views`; per product, served inside the revenue and glance-views focus dialogs rather than as a page column. | MANUFACTURING and RETAIL lens only, per the entry's compatibility qualifiers. |

One metric is genuinely new and requires its dictionary entry before any handler code:

| New dictionary key | Derivation | Honest semantics the UI must carry |
|---|---|---|
| `returns_value_estimate_retail` | `customer_returns × asp_shipped` (both operands already declared) — returned units at the window's average selling price; total and per product. | **An estimate, visibly labeled as one.** The source reports returned units, not returned dollars, and revenue fields are already net of returns ([vendor-central source doc](../../sources/vendor-central.md)) — this figure sizes the deduction that already happened and is never subtracted from displayed revenue. Assumes the window's ASP mix. |

**Margin gate lifted (2026-09-17).** `shippedCogs` semantics were verified against the
official Vendor Central report schema and recorded in
[vendor-central.md](../../sources/vendor-central.md): `retail_revenue_shipped − cogs_shipped`
is **Amazon's** retail margin on the vendor's products, never the vendor's own. The tile and
column ship as the declared `amazon_margin_proxy` — "what Amazon retains on your shipped units
before its own costs" — and every rendering keeps the whose-margin framing the dictionary
entry carries. It is the one retail value that may be negative (revenue below wholesale cost is
a real figure). In the product table it renders as a currency difference, matching the
governed derivation and the mockup's footnote rather than the mockup's illustrative percentage
cells.

The returns-value estimate introduced the dictionary's **product-of-operands** metric kind
(`type: product`, ADR-032 amendment): governed for meaning and the definition line, refused by
the live planner, and reduced by the baker from its measured operands exactly like the
portfolio ROAS reconstruction.

### Narrative metric sections (added 2026-09-17)

The page's tiles never render as one undifferentiated grid. They group into four labeled
sections that read top to bottom as the vendor's story, each with a one-line purpose caption
and — where the fixed insight roster maps to it — its interpretation card as the last cell of
the group:

| Section | Tiles | Paired insight slot |
|---|---|---|
| Demand & conversion | Glance views · units per 1K glance views | `demand-shape` |
| Sales & fulfillment | Shipped revenue · shipped units · ordered revenue · fulfilment rate (+ the Sourcing typed-absence tile) | `ordered-vs-shipped` |
| Returns drag | Customer returns · return rate · estimated revenue withheld | `returns-pressure` |
| Profitability | COGS shipped · Amazon margin proxy | — |

`retail-movers` is product-grain and renders inside the Products panel beside the table, not
in a metric section. Its three products are ranked in the query by |current − prior| shipped
revenue over every product, nonzero only, the lower product key on an exact tie; the domain's
movers rule orders and phrases them (ADR-152). Section titles and captions are fixed page copy (no temporal words);
the mockup is the exact composition reference.

### Metric focus (added 2026-09-17)

Every outcome and derived tile opens the shared focus dialog
([metric-focus contract](../metric-focus-contract.md)): identity head with the dictionary
definition line, series with exact values, server-computed facts, per-product contribution
drivers (each contributor's exact change and share of the move), the `retail-metric-focus`
prompted slot (new insight contract under `contracts/insights/` with a fictional calibration
corpus, shipped only through the registry path per ADR-068/069), a deterministic next-check
chip, an assistant seed chip, and the basis boundary. Ratio metrics (return rate, fulfilment rate,
conversion, traffic yield) additionally decompose into their declared numerator and
denominator movements. The dialog payload derives entirely from the served baked artifact —
serving it must not open a measurement path.

### Page action: Repeat purchase by product (added 2026-10-08, design revision `2026-10-08.1`)

The page gains one link, to [Repeat purchase by product](../repeat-purchase-by-product/README.md), as a
declared page action on the v2 entry:

```yaml
actions:
  - id: open-repeat-purchase-by-product
    target_page: repeat-purchase-by-product
```

- **A link only.** It binds no metric and no period. The presets, the `required_equal_prior` comparison, the
  insight roster, the materializer and `RetailHealthArtifact` don't change, and the page gets no v1 entry. A
  Brand Analytics report period is never shown here.
- **Where it renders.** One pointer line after the product table and before the basis boundary: "Repeat
  purchases per product, from Amazon Brand Analytics: Repeat purchase by product." The link text is the
  target's governed label, with no dates, like the other pointer lines in the information architecture.
- **How it renders.** The browser renders the line from the session's navigation manifest, not from the
  `actions` entry. The shared pointer component shows it only when the manifest lists a tab whose id is the
  target page id [repo, `feat/ui-ia-impl` at `0357c0d3`: `frontend/components/RetailHealthView.tsx:49-52`], and a page's tab
  id is its page id [same commit: `src/adapters/jobs/presentation_validation/navigation.py:158`].
  So the pointer's tab id is exactly `repeat-purchase-by-product`, the page id the business outcomes design
  fixes (§6.6). The `actions` entry is the server-side declaration and its dangling-target check; the browser
  doesn't read it. The same rule holds for the Retail product detail pointer and the Retention pointer.
- **When it lands.** The schema refuses an action whose target page isn't declared
  [repo: `src/domain/presentation/page_schemas.py:314`], so the action lands in the same change as the
  `repeat-purchase-by-product` page entry (business outcomes slice S4.3). Until then the manifest doesn't
  list the tab, so no dead link renders.

### Ideal order (UI refresh 3.6, readout A.9)
The page reads: the answer, "Where sales leak", "Ads beside retail" (7 days), the trend, the products,
the insights, "More figures", the reconciliation, the paid feature and About these figures. The strip
also links "Each product beside its ad credit ›" to [Products and ads](../products-and-ads/README.md)
when the session lists it.

### Ads beside retail (UI refresh 2.5, OD-UX-13, added 2026-10-22)
On the 7-day preset, `RetailHealthRouteAdapter` also requests the served 7-day Scorecard and settles
it on its own, so it never blocks the page. The "Ads beside retail" section shows that body's
`cards[0].spend` (`ad_spend`) and its `tacos` figure with the served mixed-basis label, the
Scorecard's window, and an "Open Scorecard ›" link when the session lists the Scorecard. No return
and no attributed sales render here, so the served `boundary` stays true (plan D7). On 1 day, 14 days
and 30 days the section reads one line, "Ad spend beside retail sales is shown for 7 days.", with the
link. A failed Scorecard request reads [ABS-OV-1](../page-absence-copy.md#abs-ov-1) naming the
Scorecard; a served Scorecard absence reads its own served text. The words are
`ADS_BESIDE_RETAIL_COPY` (`frontend/components/RetailHealthView.tsx`).
### Product freshness (UI refresh, UX sweep fix 2, 2026-10-22; T2L-1)
The products table has no per-row status column. Above the table one closed chip, "Data notes for these
products" (`RETAIL_FRESHNESS_CHIP`), opens to each distinct served `freshness_qualifiers` string once,
with "See why" opening the page's Coverage sheet. Rows whose qualifiers differ only by load time no
longer print a paragraph of near-identical sentences; the browser never parses them. A product
title breaks between words, never inside one, in a column at least 22 characters wide.
### Insight-slot prerequisite

The three prompted slots require insight contracts under `contracts/insights/` with declared
inputs, silence conditions, output classes, and fictional calibration corpora (ADR-068,
ADR-069) before generation exists. `retail-movers` declares no prompt, exactly like the
portfolio movers slot.

## 7. Concepts applied

- [Preset artifact publisher](../preset-artifact-publisher.md)
- [Hybrid temporal-control contract](../temporal-control-contract.md)
- [Shared visual and temporal policy](../README.md#4-visual-evidence-and-temporal-controls)
- [Theme preference and restrained glass](../theme-preference-contract.md)

## 8. Decisions

- [ADR-070 — serve declared preset windows from publish-time artifacts](../../decisions/adr-070-serve-declared-preset-windows-from-publish-time-artifacts.md)
- [ADR-068 — fill declared insight slots with direct structured model calls](../../decisions/adr-068-fill-declared-insight-slots-with-direct-structured-model-calls.md)
- [ADR-069 — version insight prompts in a calibration-gated registry](../../decisions/adr-069-version-insight-prompts-in-a-calibration-gated-registry.md)

## 9. Delivery prerequisites and acceptance

| Concern | Required proof |
|---|---|
| Declared metrics | Conversion and reconciliation entries in the metric dictionary; every artifact figure maps to a declared metric. |
| Page declaration | `presentation/pages/v2/pages.yaml` entry with presets only, required prior, day grain; generated schema and bindings; bake-ability check passes. |
| Artifact + decoder | `RetailHealthArtifact.v2` exact serializer, strict frontend decoder validating field sets, units, basis, grain, comparison, reconciliation outcome, and typed absences. Amended 2026-10-01 ([ADR-084](../../decisions/adr-084-display-monetary-values-at-currency-scale-from-a-server-display-value.md)): comparisons require `display_value` and trend points and spark entries refuse it; the domain decoder applies the tolerant canonical rule until render format 4 is CURRENT everywhere. |
| Publisher integration | Page sources registered with the stream consumer; measurement batch registered with the baker; artifacts served with no live-measurement branch; the deploy backfill produces this page's first artifacts without waiting for a publication. |
| Dev acceptance data | Delivered: the generated vendor fixtures carry real enumerated option values, are in the landing bundle, and publish through event-driven ingestion (accepted 2026-09-16, `validation/RESULTS.md`); dev coverage serves live retail pages. |
| Insight slots | Three prompted contracts plus deterministic movers; calibration corpora; slot states in the artifact. |
| Distributor-view truths | Manufacturing-only conversion and Sourcing-withheld absence render as designed states, never zeros, with tests. |
| Reconciliation | Matching totals render reconciled; a seeded mismatch renders a dated discrepancy with both figures. |
| Basis boundary | No campaign identifier, ROAS, or attribution join anywhere in artifact or renderer; boundary statement rendered. |
| Page action (2026-10-08) | `open-repeat-purchase-by-product` lands with the `repeat-purchase-by-product` page entry; `make validate-page-schemas` passes; the presets, comparison, artifact version and insight roster are unchanged. A frontend test with that tab in the session's manifest finds the pointer link to `/repeat-purchase/` on Retail health and on product detail, so a tab-id mismatch fails loudly rather than hiding the link. |
| Frontend | Command-center composition per the mockup; hand-registered route (`declaredPageIds`, `routeNames`, and route literal types in `frontend/lib/page-bindings.ts` — routing is not generated); window-derived wording; designed absence states; axe-clean; unit + e2e coverage. |
| Acceptance | Deployed dev bake observed end to end (publish → queue → artifact → served page) and recorded in `validation/RESULTS.md`. |
