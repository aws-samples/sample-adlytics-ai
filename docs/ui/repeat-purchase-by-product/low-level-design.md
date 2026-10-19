# Repeat purchase by product — low-level design

## 6. Wire, artifact, and renderer contracts

Marker key: [repo] is read in this worktree on 2026-10-08 with `file:line`; [proposal] is a choice made in
this package that the build slice may still change through review.

### Exact API inputs

| Route | Method and auth | Accepted body | Explicitly forbidden |
|---|---|---|---|
| `/api/presentation/repeat-purchase` [proposal] | `POST`, the custom request authorizer | `{session_id}`, optionally `temporal_selection: TemporalSelection.v2` restricted to `omitted` or `last_complete_month` | ASIN filters, report periods, rolling presets, custom ranges, a comparator, unknown fields |

### Page declarations

Page id `repeat-purchase-by-product`, route `/repeat-purchase/`, governed label "Repeat purchase by product",
in the Customers hub with tab order 3. The route is flat, like every existing route.

| File | Entry |
|---|---|
| `presentation/pages/v1/pages.yaml` | `mode: single_basis` (every row is `retail_actual`), `grain.time: declared_period`, dimensions `[marketplace]`. Metric slots for the contract 16 levels (orders, unique customers, repeat purchase revenue; names fixed when the contract is written), each with `period_grains: [month]` and the `time_grain` exact selector |
| `presentation/pages/v2/pages.yaml` | `mode: selectable`, `boundary_required: true`, `navigation: {hub_id: customers, tab_label: Repeat purchase by product, tab_description: Orders, customers and repeat revenue per product, reported by Amazon Brand Analytics, order: 3}`. `temporal_policy`: `candidate_presets: [last_complete_month]`, `default_preset: last_complete_month`, custom disabled, `comparison_policy: none`, `display_grains: [declared_period]`. Artifact `RepeatPurchaseByProductArtifact` v1, outcomes `[complete, empty, insufficient]`. Sections: `workspace-scope`; `repeat-purchase` (`declared_periods: [last_complete_month]`, the anchor); `retention-pointer` [proposal]; `evidence-trace`; `boundary`. Action `{id: open-retention, target_page: retention}` [proposal] for the pointer |

The page id is the tab id every inbound pointer uses: Retail health, Retail product detail and Retention
render their pointer from the session's navigation manifest by tab id `repeat-purchase-by-product`. The
`retention-pointer` line renders the same way, by tab id `retention`; its `actions` entry is the dangling-target
check only. The rule is in the
[Retail health page action](../retail-health/low-level-design.md#page-action-repeat-purchase-by-product-added-2026-10-08-design-revision-2026-10-081).
The page lists every product for one month and takes no ASIN filter, so an inbound pointer names this page,
never a per-product or month-by-month view.

The business outcomes design kept this page out of the main navigation. The owner's OD-4 acceptance of
2026-10-07 puts it in navigation, which this declaration follows.

### Window policy

The only anchor reads contract 16, a periodic contract, so `resolve_calendar_preset` picks the latest month
with a current MONTH-report publication and names it ("August 2026", fictional)
[repo: `src/domain/presentation/calendar_presets.py:194`]. With none, the page shows the result-`none` text.
There's no comparison, so no equal-prior window is needed. WEEK reports stay out until the contract pins
their boundaries; QUARTER reports land but aren't shown in v1. Adding `last_complete_quarter` later is a
change to this package only.

### `RepeatPurchaseByProductArtifact` v1

| Section | Content | Rendering responsibility |
|---|---|---|
| Repeat purchase | Per ASIN, server-ordered: display title, ASIN, `orders`, `unique_customers`, `repeat_purchase_revenue` with its currency and server `display_value` | Table, title first and ASIN second; the foot line; no total row for unique customers |
| Pointer | The page action's governed label | "For repeat buying by shoppers your ads brought in, by acquisition month, see Retention." |
| Evidence trace | The two reported shares per ASIN from the source-aligned row, labelled "reported by Amazon", and the report's `reportSpecification` dates | Inside "About these figures"; never a table column |
| Evidence status | `EvidenceStatus.v2` | Coverage sheet |
| Page context and starter questions | As the other tabs | Assistant |
| Boundary | "Amazon Brand Analytics counts every order of the product in the month, whether or not an ad was involved." and "This page isn't joined to advertising data." | "About these figures" |

**The reported shares.** The approved mockup doesn't draw them. The business outcomes design and ADR-105 keep
them as evidence labelled "reported by Amazon", and the governed source-aligned row holds them, so under the
owner's render-more principle they render inside "About these figures" [proposal], never as a table column.
That keeps the levels-only table the owner approved (copy fix R5-1, "levels only, no rate column").

### Designed states

| Condition | What renders |
|---|---|
| No Brand Analytics access, or the report never landed | Page-level "Needs Amazon Brand Analytics access", "This tab reads the Brand Analytics repeat purchase report. To turn it on, the account needs:" with the two prerequisites; no scope band and no date control |
| No complete month published yet | The result-`none` text from the server |
| A product listed last month and missing this month | Not applicable: Brand Analytics returns the products it returns; there's no roster on this page |

### Insights and "Also changed"

No insight contract and no alert rule binds this page in v1, so it has no insights row and no "Also changed"
section, and the Overview's "What changed" card doesn't list it.

### Materializer

New, under the ADR-075 pattern. It bakes on contract 16 publication transitions.

## 7. Concepts applied

- [Business outcome measurement](../../concepts/business-outcome-measurement.md)
- [Preset artifact publisher](../preset-artifact-publisher.md)
- [Hybrid temporal-control contract](../temporal-control-contract.md)

## 8. Decisions

- [ADR-105: Brand Analytics levels only](../../decisions/adr-105-keep-source-reported-shares-source-aligned-and-publish-brand-analytics-levels-only.md)
- [ADR-100: complete measured periods](../../decisions/adr-100-serve-periodic-metrics-only-for-complete-measured-periods.md)
- [ADR-115: hubs from one navigation file](../../decisions/adr-115-organise-the-app-into-customer-question-hubs-from-one-navigation-file.md)
- [ADR-124: the render-more rule](../../decisions/adr-124-render-governed-data-and-retire-a-surface-only-on-evidence.md)

## 9. Delivery prerequisites and acceptance

| Concern | Required proof |
|---|---|
| Page declaration | v1 and v2 entries pass `make validate-page-schemas`, including the `declared_period` page time with one anchor |
| Levels only | An artifact test fails on a share in the table, a recomputed share, or a sum of unique customers |
| Time | `resolve_calendar_preset` picks the latest month with a current MONTH report, and `none` with none |
| Access state | The "Needs Amazon Brand Analytics access" state renders with no scope band |
| Retail health link | The Retail health page action targets this page and passes the dangling-target check in the same change; with this tab in the session's manifest, a frontend test finds the Retail health, product detail and Retention pointer links to `/repeat-purchase/` |
| Conformance | Rule 4 of the [delivery slices](../information-architecture/delivery-slices.md#2-rules-for-every-slice) against every mockup state |
| Live | Deployed dev bake observed end to end and recorded in `validation/RESULTS.md`, once a report lands |
