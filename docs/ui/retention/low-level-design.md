# Retention — low-level design

## 6. Wire, artifact, and renderer contracts

Marker key: [repo] is read in this worktree on 2026-10-08 with `file:line`; [proposal] is a choice made in
this package that the build slice may still change through review.

### Exact API inputs

| Route | Method and auth | Accepted body | Explicitly forbidden |
|---|---|---|---|
| `/api/presentation/retention` [proposal] | `POST`, the custom request authorizer | `{session_id}` only: the page serves its latest vintage | any `temporal_selection` (a `vintage` included), presets, custom ranges, cohort months, follow-up months, group or path selectors, unknown fields |
| `/api/presentation/retention-metric-focus` [proposal] | `POST`, same authorizer | `{session_id, metric, temporal_selection}` with a declared focusable metric | any cohort, cell, group or grain field, unknown fields |

**Latest vintage only (owner decision 2026-10-14, integration gate IG-004).** The route serves one vintage,
the latest with a current standard-path publication. The handler decodes the selection against an empty
offered roster, so it refuses every `vintage` selection before any retained-store read, the served month
included. The response's `VintageControl.v2` is `fixed` and carries no roster
([temporal control contract](../temporal-control-contract.md)). The page shows the server's static label
"Cohorts through {vintage}" in place of the mockups' "Cohorts as of" select. Any query string, including
`?asof=`, gets the explicit zero-fetch URL-state error. A bounded historical vintage roster is deferred work,
taken up when the owner asks for historical vintages.

### Page declarations

New page id `retention`, route `/retention/`, governed label "Retention", in the Customers hub with tab
order 2.

| File | Entry |
|---|---|
| `presentation/pages/v1/pages.yaml` | `mode: parallel`, because standard rows are `ad_attributed` and paid-path rows `retail_actual`, so the page holds two bases and its boundary section; `grain.time: as_of_vintage`, dimensions `[marketplace]` |
| `presentation/pages/v2/pages.yaml` | `mode: selectable`, `boundary_required: true`, `navigation: {hub_id: customers, tab_label: Retention, tab_description: Cohorts of new customers your ads brought in, and when they buy again, order: 2}`. `vintage_policy: {selector: as_of_month, default: latest_with_current_publication, comparison: none}`. `vintage_policy` implies `as_of_vintage`; vintage declarations have no `display_grains` key. Artifact `RetentionCohortArtifact` v1, outcomes `[complete, empty, insufficient]` |

v2 sections: `workspace-scope`, `path`, `triangle`, `retention-curve`, `retention-insights` [proposal],
`also-changed` [proposal], `repeat-purchase-pointer` [proposal], `evidence-trace`, `boundary`. The pointer is
a declared page action, `{id: open-repeat-purchase-by-product, target_page: repeat-purchase-by-product}`,
whose target the schema checks against known pages [repo: `src/domain/presentation/page_schemas.py:161`,
`:314`]. So it lands in the same change as, or after, the Repeat purchase by product page entry: the S4.1
slice ships Retention without the pointer, and S4.3 adds it. The browser renders the line from the session's
navigation manifest by tab id `repeat-purchase-by-product`, not from the `actions` entry, under the rule in
the [Retail health page action](../retail-health/low-level-design.md#page-action-repeat-purchase-by-product-added-2026-10-08-design-revision-2026-10-081).

### `RetentionCohortArtifact` v1

| Section | Content | Rendering responsibility |
|---|---|---|
| Marketplace | `marketplace_text`: "{label} marketplace" from the governed marketplace display label (the Scorecard's short name, else the governed name), as New customers. Null with one market; with two or more, every market carries a distinct one, and either rule broken is a malformed artifact | With two or more markets, a visible heading per market that also leads each section's and the triangle region's accessible name; one market renders as the approved mockup |
| Cohort grain | `cohort_grain`, `month` or `quarter`, for the vintage, and the grain note when it's `quarter` (see "Quarterly cohorts") | The scope band names it; the grid and curve label their axes by it |
| Marketplace outcome | `outcome` plus `absence_text`: null for `complete`, "No cohorts were measured for this vintage." for `empty`, and "There is not enough cohort evidence to show Retention safely." for `insufficient` | The browser renders the server text directly and never maps an outcome to copy |
| Path | The standard path, and the paid path with its paid-feature state | Segmented control ("Repeat after an ad" / "Any repeat purchase") when the paid path has delivered; otherwise the "Available with the paid feature" callout ("All Amazon purchases by these cohorts, not only ad-touched ones, need the Amazon Retail Purchases paid feature.") |
| Triangle | For every cohort month *m* in the vintage's window and month *k* = 1 to 12 with *m* + *k* ≤ the vintage, per acquisition group: `repeat_buyer_rate` and the cohort size `cohort_buyers_new_to_brand_12mo`; on the paid path the ARP rates. Every value cell in a row carries the same cohort-size value and complete server phrase (for example “1,200 new customers”); the browser uses the first available value cell and never appends or formats copy. | Heat map with a table equivalent; one group at a time through the "Acquired through" selector; a `not_returned` cell is hatched and reads "Not available", never colour alone; a future cell reads "Not yet" |
| Repeat curve | `cumulative_repeat_rate_account_ids` per acquisition group and month since acquisition, from the extracted cumulative measure | One line per group, unit "Amazon account IDs", zero baseline, exact-values table |
| Insights | Fixed roster `retention-shift` v1 | Insights row |
| Pointer | The page action's governed label | One pointer line |
| Evidence status | Exactly one `EvidenceStatus.v2` block; each missing-market slot carries its marketplace key plus server-owned figure, role, reason, and recovery text | One coverage surface for the returned slots, rendered verbatim; a missing or duplicated block is refused |
| Page context and starter questions | As the other tabs | Assistant |
| Boundary | Fixed statements: what a cohort is, ad-touched repeat only on the standard path, "from this advertiser" on multi-brand accounts | "About these figures" |

**Rates on the grid and the curve.** The grid shows one group at a time, so it binds the all-ID
`repeat_buyer_rate` with the generic identity caveat. The curve compares groups, so it binds only the
account-ID rates. A page-schema test fails a binding of an all-ID rate on the curve. Nothing is summed across
*k*: cumulative repeat buyers are their own extracted count.

**The repeat curve isn't in the approved mockup.** The business outcomes design specifies it, its data is
governed, and the owner's render-more principle keeps it
([ADR-124](../../decisions/adr-124-render-governed-data-and-retire-a-surface-only-on-evidence.md)). It sits after
the paid-path row and before the insights row [proposal]. Its conformance is checked against this document and
the shared chart pattern.

### Labels (server text)

| Figure | Label |
|---|---|
| Standard-path grid | "{group} · ad-touched repeat only. Understates true repeat. Month 0 repeats are excluded." |
| Paid-path grid | "All Amazon purchases · Amazon account IDs only" |
| Curve | "Cumulative repeat rate · Amazon account IDs" (served); rendered as "Bought again by then · signed-in shoppers" through the glossary by its metric id (UI refresh, UX sweep fix 7, T1L-9). Since then the curve renders first, above the cohort grid. From UI refresh 3.5 (readout A.8) the curve is a zero-based line chart, one line per served group with its own dash pattern, its top axis label the largest served rate and its legend each group's served last point; the served points sit in a closed "Show values". The grid's heading reads "By cohort" with one line on why it reads differently: "Each cell counts all shoppers, including device IDs, in that period only; the curve above counts signed-in shoppers and adds up every period so far, so the two read differently." The boundary disclosure reads "How we count" The scope band and the quarterly grain note read as one plain line |
| Scope band | "Cohorts as of {vintage}, grouped by {month or quarter}: acquired {first} to {last}, with repeat {months or quarters} through {last}." (revised at `2026-10-09.2` to name the cohort grain) |
| Pointer | "For all repeat purchases per product, reported by Amazon Brand Analytics, see Repeat purchase by product." |

The pointer wording is the information architecture's, which names the difference between the two pages'
repeat counts; it replaces the business outcomes design's shorter line.

### Designed states

| Condition | What renders |
|---|---|
| Absent cohort (a cohort month and group with no row at any *k*) | "No shoppers were won this way in {month}, or too few to show.", never hatched gaps, and outside the loss denominator. On a quarterly vintage {month} is the cohort quarter's label |
| A cell that didn't come back (`not_returned`) | Hatched "Not available" with the legend "Not available: too few shoppers bought again, or none". The cell claims neither suppressed nor empty (design D-5), so the legend never says Amazon withheld it. This replaces the approved mockup's wording (review E-B-3, 2026-10-08, owner ratification pending); the server constant `WITHHELD_CELL_LEGEND` [repo: `src/domain/presentation/retention.py`] holds the text |
| Paid path not observed | The paid-feature callout; the standard path renders |
| Paid path: cohort sizes changed between runs | The ARP rate is withheld with "cohort sizes changed between runs" |
| Paid path: the two operands' cohort grains differ (the serve-time backstop; the load quarantines a mismatched pack 15 delivery) | The ARP rate is withheld with "Cohort grain changed in {period}.", {period} being the as-of month of the marketplace's first quarterly vintage (see "Quarterly cohorts") |
| No vintage published | Page-level absence: [ABS-RET-1](../page-absence-copy.md#abs-ret-1); no scope band |
| A vintage built on calendar-quarter cohorts | The quarterly state below |
| A comparison of two vintages across the grain switch (chat, or any later comparison) | Refused with "Cohort grain changed in {period}." (D-8 (a)) |

### Quarterly cohorts (added at `2026-10-09.2`)

The 2026-10-09 probe found that AMC withholds cohort cells per data source
([results](../../../validation/RESULTS.md#s-6-retention-pack-14-at-acquisition-date-grain)), so design §8's
"criterion 7 fails" row applies to packs 14 and 15 [inference: read on the acquisition-date text]: monthly cohorts ship, and a pre-built alternative built
on calendar-quarter cohorts waits beside them (business outcomes wave 2b, plan D10).

- **Switch.** Per marketplace, from the next vintage after any `acquisition_source_set` slice of a pack 14
  (standard path) delivery passes the degrade threshold (design §4.4, §6.6). The load decides it from the
  previous vintage's published pack 14 loss evidence, so no person chooses. The switch is sticky until Step
  B: later vintages for that marketplace stay quarterly, and switching back needs an owner decision then.
- **One grain for both paths.** Pack 15 (the paid path) follows pack 14's cohort grain for the same
  marketplace and vintage; its own loss never switches it [proposal]. The ARP rates divide pack 15 repeat
  buyers by pack 14's `cohort_buyers_account_ids` for the same cell, so both operands must share one grain.
  The load derives pack 15's expected variant from pack 14's published evidence for that marketplace and
  quarantines a pack 15 delivery of the other grain with `PACK_VARIANT_MISMATCH`, naming the expected variant
  (plan D10). The serve-time check is the backstop: if the two operands still differ, the ARP rate is
  withheld with "Cohort grain changed in {period}.", where {period} is the as-of month of the marketplace's
  first quarterly vintage, never with "cohort sizes changed between runs", and the standard path renders.
- **Cells.** For as-of month *a*, a quarterly cell (*q*, *j*) is a complete calendar cohort quarter *q* and a
  follow-up quarter *j* = 1 to 3, × 3 groups. It's served only when follow-up quarter *q* + *j* has ended by
  the end of *a* and the span from *q*'s first month to the end of *q* + *j* lies inside the one 13-month
  window the vintage reads (design §6.6). A cohort quarter that hasn't ended by the end of *a* isn't a row,
  and a follow-up quarter that hasn't ended is never served as a full quarter: it reads "Not yet". A cohort
  quarter whose first month precedes the vintage's window (when *a* isn't a quarter's first month, the
  earliest quarter is only partly inside it) isn't a row and isn't an expected cell, so it never counts
  toward the loss that drives the switch. A `not_returned` cell inside the rule is the hatched "Not
  available". The scope band's "acquired {first quarter}" names the first cohort quarter wholly inside the
  window, and "repeat quarters through {last quarter}" names the last follow-up quarter that has ended.
- **No Quarter 4 column [proposal].** Design §8 names follow-up quarters 1–4, but a cohort quarter plus four
  follow-up quarters spans 15 months, beyond AMC's 13-month rolling retention
  ([AMC](../../sources/amc.md#7-freshness-and-restatement)), so no Quarter 4 cell is ever observed
  [inference: 3 + 12 = 15 months]. The grid and the curve stop at Quarter 3, the last quarter the window can
  serve. Keeping a Quarter 4 column with a fixed line naming the 13-month limit is the §10 alternative.
- **Grid.** Cohort rows are calendar quarters, labelled "Q{n} {year}" (for example "Q3 2026"); follow-up
  columns are quarters 1 to 3, labelled "Quarter 1" to "Quarter 3", with no Quarter 4 column. Quarter 0
  repeats are excluded, as month 0 repeats are. A future cell reads "Not yet", and a `not_returned` cell keeps its hatch and legend. The
  standard-path grid label reads "Quarter 0 repeats are excluded." in place of the month sentence.
- **Curve.** The x axis is quarters since acquisition, from 1, with points only for cells the cell rule
  serves, up to Quarter 3. The account-ID rule is unchanged.
- **Note.** Under the scope band: "Cohorts are grouped by quarter from {quarter}, because too many monthly
  cells had too few shoppers." {quarter} is the calendar quarter holding the as-of month of the first
  quarterly vintage for that marketplace, for example "Q4 2026" [proposal]. It shows on every quarterly
  vintage.
- **Scope band.** "Cohorts as of {vintage}, grouped by quarter: acquired {first quarter} to {last quarter},
  with repeat quarters through {last quarter}."
- **Across the switch.** A vintage on quarterly cohorts and one on monthly cohorts are never compared. Any
  such comparison is refused with "Cohort grain changed in {period}.", where {period} is the as-of month
  of the first quarterly vintage (D-8 (a), as the frequency bucket-scheme refusal does). The vintage
  selector still lists both kinds; selecting one shows only that vintage.
- **Insight.** `retention-shift` v1 reads month-1 and month-3 rates, which a quarterly vintage doesn't have,
  so it's silent on a quarterly vintage [proposal]. A later contract version can name a quarter-1 shift.

### Insight contract: `retention-shift` v1

Perspective class. The change in month-1 and month-3 repeat rates between the latest two complete cohorts of
one vintage, same path and same group, so it never compares groups. A shift is named only when both cohorts
have at least `C_min` shoppers in the rate's denominator (policy, proposed 200), the 95% interval of the
difference excludes zero, and the change meets the contract's materiality. Silent when either cell is
`not_returned`. Calibration: two 40-shopper cohorts at 10% and 20% name nothing; summing repeat rates across
groups is rejected.

### "Also changed"

The alerts-declined path applies (OD-7). No default alert rule covers `retention-shift`
([ADR-108](../../decisions/adr-108-raise-in-app-alerts-when-page-artifacts-publish.md)), so in v1 this tab has
no "Also changed" section. A later rule brings it in after the insights row with no page change.

### Materializer

New, under the ADR-075 pattern. It bakes one artifact per vintage on pack 14 (and pack 15) publication
transitions, and bakes its deterministic findings.

## 7. Concepts applied

- [Business outcome measurement](../../concepts/business-outcome-measurement.md)
- [Preset artifact publisher](../preset-artifact-publisher.md)
- [Hybrid temporal-control contract](../temporal-control-contract.md)

## 8. Decisions

- [ADR-104: acquisition cohorts by Amazon's new-to-brand flag](../../decisions/adr-104-define-acquisition-cohorts-by-amazons-new-to-brand-flag.md)
- [ADR-101: disjoint groupings](../../decisions/adr-101-model-shopper-groupings-as-disjoint-axes-with-one-window-per-shopper.md)
- [ADR-107: paid-feature availability](../../decisions/adr-107-derive-paid-feature-availability-from-delivery-evidence.md)
- [ADR-116: one time model and one window per tab](../../decisions/adr-116-give-every-tab-one-time-model-and-one-window.md)
- [ADR-124: the render-more rule](../../decisions/adr-124-render-governed-data-and-retire-a-surface-only-on-evidence.md)

## 9. Delivery prerequisites and acceptance

| Concern | Required proof |
|---|---|
| Page declaration | v1 and v2 entries pass `make validate-page-schemas`; the v2 entry declares exactly one of `temporal_policy` and `vintage_policy` |
| Vintages | Latest only: every `vintage` selection and every query string is refused before a fetch or read, each paired with the selection-free request that serves the latest vintage |
| Identity rule | The curve binds account-ID rates only; an all-ID binding there fails the page-schema check |
| Cells | Unit tests: an absent cohort renders its text; a `not_returned` cell renders hatched "Not available" with exactly the `WITHHELD_CELL_LEGEND` legend; a sponsored-only advertiser gets an intact triangle |
| Quarterly cohorts | Unit tests, each paired with a monthly vintage: a quarterly vintage labels rows by quarter and columns exactly "Quarter 1", "Quarter 2" and "Quarter 3" with no Quarter 4 column, shows exactly the grain note and names the grain in the scope band; a comparison of a monthly and a quarterly vintage is refused with exactly "Cohort grain changed in {period}.", and two monthly vintages compare; `retention-shift` is silent on a quarterly vintage. Cells: at an as-of month inside a quarter, the current cohort quarter isn't a row and an unended follow-up quarter reads exactly "Not yet", paired with a quarter-end as-of month where the same follow-up quarter is served; at an as-of month that isn't a quarter's first month, the partly-inside earliest quarter isn't a row and adds no expected cell, paired with an as-of month that is a quarter's first month, whose earliest quarter is wholly inside and is a row. Switch: a pack 14 slice over the degrade threshold switches both packs' next vintage for that marketplace, paired with a pack 15-only loss that switches neither |
| Paid path across the grain switch | Unit tests: a pack 15 delivery whose grain differs from the variant derived from pack 14's evidence is quarantined with `PACK_VARIANT_MISMATCH`, paired with a matching delivery that loads; an ARP rate whose pack 14 and pack 15 operands differ in cohort grain is withheld with exactly "Cohort grain changed in {period}.", paired with same-grain operands (monthly, then quarterly) whose rate is served |
| Paid path | The cohort-size check withholds an ARP rate; the paid-feature callout renders unobserved |
| Insight | `retention-shift` v1 with its calibration corpus |
| Conformance | Rule 4 of the [delivery slices](../information-architecture/delivery-slices.md#2-rules-for-every-slice) against every mockup state, and against this document for the curve and the quarterly state, which no mockup draws |
| Live | Deployed dev bake observed end to end and recorded in `validation/RESULTS.md`; real AMC figures wait on the probes |

## 10. Open items

| Item | What resolves it | Blocks |
|---|---|---|
| Group labels. The approved mockup labels the acquisition groups "Saw DSP only", "Saw sponsored ads only" and "Saw both", which are the exposure-group labels of Reach and funnel. The acquisition group is about credit on the acquisition day, not about ads seen, and ADR-104 describes it as "won by" | The governed display names of the acquisition-group axis, set in the S4.1 slice's taxonomy change | Nothing; no figure changes |
| Quarterly-cohort state (added at `2026-10-09.2`, owner scope for packs 14–15). The approved mockup has monthly cohorts only. This document adds the quarter labels, the note "Cohorts are grouped by quarter from {quarter}, because too many monthly cells had too few shoppers.", the refusal "Cohort grain changed in {period}.", and a silent `retention-shift` on a quarterly vintage | Owner ratification of manifest revision `2026-10-09.2` (wave 2b stop item S-B1) | Nothing; the build uses this document |
| Scope band grain (revised at `2026-10-09.2`). The scope band now names the cohort grain ("grouped by month" or "grouped by quarter"); the approved mockup's band doesn't | Owner ratification (S-B1) | Nothing |
| Switching back to monthly cohorts. The switch is sticky until Step B | An owner decision at Step B (wave 2b stop item S-B6) | Nothing |
| No Quarter 4 column [proposal] (added at `2026-10-09.2`). Design §8 names follow-up quarters 1–4, but a cohort quarter plus four follow-up quarters spans 15 months, beyond AMC's 13-month retention, so no Quarter 4 cell is ever observed [inference]. This document drops the column (quarters 1–3). The alternative keeps it with a fixed line naming the 13-month limit | Owner ratification (S-B1) | Nothing; the build uses this document |
| One grain for both paths [proposal]. Pack 15 follows pack 14's cohort grain per marketplace and vintage, rather than switching on its own loss, so the ARP rate never pairs a quarter's cohort size with a month's repeat buyers | Owner ratification (S-B1) | Nothing; the build uses this document |
