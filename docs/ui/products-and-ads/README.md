# Retail health › Products and ads
| | |
|---|---|
| **Level** | Delivery page package |
| **Status** | Built; on `main` since PR #12; captured on dev 2026-10-23. A composed tab ([ADR-151](../../decisions/adr-151-compose-a-customer-tab-from-served-page-presentations.md) amendment) |
| **Owns** | The tab's columns, the source of each figure, its answer template, its states and its browser-owned copy |
| **Parent** | [Decision Workspace UI](../README.md) · [Information architecture](../information-architecture/README.md) (Retail health hub) · [Retail health](../retail-health/README.md) |

## 1. Purpose
Products and ads shows, for each product Retail health serves, its shipped sales and page views beside
what one chosen ad channel credits to it, for the Retail health window (readout A.10). It answers which
products to advertise, feature or fix, in the customer's own catalog and campaign tools. The two
systems sit side by side: nothing is added, divided or ranked, and there's no product return on ad
spend, because ad spend has no product grain.

## 2. Sources
`ProductsAndAdsRouteAdapter` makes one request: the served Retail health presentation for the
selected preset, with Retail health's own window control and URL, and `product_ads: true`. The page
comes back with `page.product_ads`, the `RetailProductAds` part the same bake wrote beside the
product-detail children (Retail health render format 12, [Retail health LLD](../retail-health/low-level-design.md)).
For each served row, in row order, the part carries the row's `ad_attributed` channels (current
purchases, current revenue and the revenue's change) or its typed absence, and its conversion, all
copied from that row's baked `RetailProductDetail`
([ADR-125](../../decisions/adr-125-serve-retail-product-detail-as-a-baked-child-of-retail-health.md)).
Nothing is computed at serve time or in the browser. The browser decoder (`productsAndAdsPresentationV2`)
refuses a part whose window or rows aren't the parent's, so the tab never shows one row's credit
beside another product. A body with no rows serves `product_ads: null`.

Before render format 12 the tab requested each row's product detail, four at a time, after the
parent, so a load made one request per served row plus the parent (51 at 50 rows, a projection
from `RETAIL_HEALTH_MAX_ROWS`). At format 12 a load is one request at any row count. Dev paced
passes of 9 loads of dev's 7 products, timed from navigation start to the end of the last
presentation response [measured on dev, 2026-10-27,
[results](../../../validation/RESULTS.md#products-and-ads-part-retail-health-render-format-12-on-dev--2026-10-27)]:

| Render format | Requests per load | Browser p50 | Browser p95 | Parent request end p50 | Bytes per load |
|---|---|---|---|---|---|
| 11, one detail per row | 8 | 1,739 ms | 5,405.6 ms | 703 ms | 256,407 |
| 12, after the new revisions | 1 | 824 ms | 993.2 ms | 824 ms | 85,005 |

At format 12 the tab ends with its one request. That request's serve time, which now includes the
part's three reads (the index shard, then the part and its custody), was 315 ms p50 against 196 ms
for the format-11 parent request; Retail health's own request served at 311 ms p50 before and 289 ms
after, with its body unchanged.

| Column | Slot | Served field |
|---|---|---|
| Product | `pa-{asin}` | RHA `rows[].title`, `child_asin` |
| Shipped sales, Change | `pa-{asin}-rev` | RHA `rows[].comparisons[retail_revenue_shipped]` current and change (withheld per coverage) |
| Product page views | `pa-{asin}-views` | RHA `rows[].comparisons[glance_views]` current |
| Units per 1,000 page views | `pa-{asin}-conv` | the part's `rows[].conversion` (the detail's `totals[conversion_rate_retail]` current); the column renders only when a row serves it |
| Ad-credited sales · {channel} | `pa-{asin}-ad-rev` | the part's `rows[].channels[channel].revenue` (`attributed_revenue_14d`) and `revenue_change` |
| Ad-credited purchases | `pa-{asin}-purchases` | the part's `rows[].channels[channel].purchases` |
| First-time purchases | `pa-{asin}-ntb` | not served at product grain: reads "Not measured yet" |

The channel switch lists the channels the part credits, in the order first served. The table's note
carries the part's served `basis` when any row credits a channel. No halo-sales column renders: nothing serves
`attributed_revenue_halo_14d` at product grain.

## 3. Answer template
"{N} products shipped in {window}; {first title} shipped most, {revenue}, {up/down} {change} on
{prior window}." from the served `total_count` and the first served row, which the server orders by
shipped revenue. A withheld or absent change drops the last clause. An empty or insufficient body
shows Retail health's ABS-RH-3 or ABS-RH-4 copy; a page-level absence shows ABS-RH-1 or ABS-RH-2
([Page absence copy](../page-absence-copy.md)). A publication baked before render format 12 is a
page-level absence on this tab ([upgrade behaviour](../preset-artifact-publisher.md#first-bake-and-recovery)).

## 4. Copy
Browser-owned (`PRODUCTS_AND_ADS_COPY`, `PRODUCTS_AND_ADS_LINES`): "Two systems side by side. They never
add up or divide into each other."; "There's no return on ad spend per product, because ad spend isn't
recorded per product."; a channel the row doesn't credit: "No credit served for this channel."; a
row's own served ad `reason` reads as served. A part that fails to load or decode fails the whole tab
with its page issue, as any page request does; there is no per-row failure state.
