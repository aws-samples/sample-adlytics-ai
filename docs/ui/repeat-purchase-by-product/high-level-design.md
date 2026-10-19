# Repeat purchase by product — high-level design

## 4. Solution strategy

One tab, one baked artifact, served from publish-time artifacts through the
[preset artifact publisher](../preset-artifact-publisher.md). The only preset is "Latest measured month" and
there's no comparison, so the page shows no date control: the scope band names the month and says "This tab
has no comparison." This document records only what's specific to this tab.

### Presentation API role

| Operation | Route | Browser intent | Server responsibility |
|---|---|---|---|
| Repeat purchase by product | `POST /api/presentation/repeat-purchase` [proposal] | Session; optional `TemporalSelection.v2` restricted to `omitted` or `last_complete_month` | Resolve the latest month with a current MONTH-report publication, serve its baked artifact |

The page declares no focusable metric, so it has no metric-focus route in v1. The route is a new API Gateway
route and needs its own `infra/` approval.

### Composition (top to bottom, per the mockup)

1. Hub header: "Customers" eyebrow, tab strip (New customers, Retention, Repeat purchase by product), h1
   "Repeat purchase by product", the description, then the scope band.
2. **By product**: one table, "Orders, customers and repeat revenue per product", captioned "Amazon Brand
   Analytics · {month} · all repeat purchases, not only ad-touched", with the product title first and its
   ASIN second, and the foot line "Unique customers can't be added across products."
3. One pointer line: "For repeat buying by shoppers your ads brought in, by acquisition month, see
   Retention."
4. **About these figures**, which also holds the two shares Amazon reports, labelled "reported by Amazon".

### Data lineage

`fct_repeat_purchase_reported` (ASIN × report period × marketplace, basis `retail_actual`, explicit periodic),
written from the Brand Analytics repeat purchase report through contract 16 (and 16b if the report has an
aggregate array). Product titles come from the effective-title resolver. The reported shares are read from the
source-aligned row only.

### Build slice and dependencies

| Needs | Why |
|---|---|
| The forecasting design's governed API operation pack, implemented | The report lands through it; until then this page waits and the rest of the repeat-purchase outcome ships |
| Periodic serving and temporal transport v2 | The `last_complete_month` preset on the `declared_period` page time |
| Customer prerequisites | A vendor or seller account with Amazon Brand Registry and the Brand Analytics role for the Selling Partner API |
| A route approval | The new route |
