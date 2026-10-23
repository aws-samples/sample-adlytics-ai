# Overview › Growth check
| | |
|---|---|
| **Level** | Delivery page package |
| **Status** | Built on `feat/ui-refresh` (UI refresh slice 2.2, 2026-10-22); not deployed yet. A composed tab ([ADR-151](../../decisions/adr-151-compose-a-customer-tab-from-served-page-presentations.md)) |
| **Owns** | The Growth check's bands, the source of each figure, and its answer template |
| **Parent** | [Decision Workspace UI](../README.md) · [Information architecture](../information-architecture/README.md) (Overview hub) |
## 1. Purpose
The Growth check answers "is my advertising growing my business?" at `/` for the last 7 days, the one
preset the Scorecard, Retail health and New customers all declare. It shows no date control: the
window is a fixed label from the Scorecard's served window and prior. Each source serves its own 7-day
anchor, so a band whose served `period` isn't the labelled window (or that has no label, because the
Scorecard isn't served) names its own window: the charts in their footnote, `ov-tile-retail` under its
value (`ov-tile-retail-window`), and the New customers band in a footnote (`ov-ntb-window`). Each band
links to its source tab on the same window (`?preset=7d` where the source's default is another preset).
## 2. Bands and the source of each figure
Every figure is a served value. The view adds, ranks and recomputes nothing.

| Band | Slot | Source (7 days) | Served field |
|---|---|---|---|
| Answer | `answer` | Scorecard, Retail health | default model `roas_deduplicated_incl_halo_14d` value and prior display; `retail_revenue_shipped` change; `ad_spend` change |
| Tiles | `ov-tile-spend`, `ov-tile-roas`, `ov-tile-tacos` | Scorecard | `cards[0].spend`; `cards[0].headline.models[default]`; `tacos[card]` |
| Tiles | `ov-tile-retail` | Retail health | `totals[retail_revenue_shipped]` current, prior, change |
| Daily ad spend | `ov-chart-spend` | Campaigns, all channels | `trend.points[].spend`, zero-based |
| Daily shipped sales | `ov-chart-retail` | Retail health | `trend[].current/prior` shipped revenue, zero-based |
| New customers by channel | `ov-ntb-{ad_product}` | New customers | `markets[0].new_to_brand.rows[]` `ntb_revenue_share`, its change, and `cost_per_ntb_purchase`; never summed |
| What changed | `ov-changed` | Scorecard; Retail health | up to two `insight_cards[].headline`; the filled `retail-movers` text |
| Can you rely on these? | `ov-trust-reconciled`, `ov-trust-through` | Retail health; each source | `reconciliation.{outcome,as_of}`; each served `period.end`, one line per date |

The Overview reads one network card. With two marketplaces' cards it names none and the band points
to the Scorecard.
## 3. Answer template
"Amazon Ads returned {roas} USD for every 1 USD spent in {window}, compared with {prior_roas} USD in
{prior_window}. Shipped retail sales {rose/fell} {retail_chg} while ad spend {rose/fell} {spend_chg}."
Each clause drops on its own (`frontend/lib/overview.ts`). "while" joins the two changes only when the
retail and ad windows are the same days. Otherwise they are two sentences, each naming its window:
"Shipped retail sales {rose/fell} {retail_chg} in {retail_window}. Ad spend {rose/fell} {spend_chg} in
{window}." With no return figure the first sentence is "Return on ad spend isn't available
for {window}: {served absence}".
## 4. States
A band whose source request fails shows ABS-OV-1; a source served as `window_unavailable`, `empty` or
`insufficient` shows that source's own text ([Page absence copy](../page-absence-copy.md)). The tab is
never a page-level absence. The assistant reads the Scorecard's served page context.
