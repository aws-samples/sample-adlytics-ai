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
parent: on dev's 7 products the last detail ended at 1,763 ms median against 754 ms for the parent,
and at 50 rows a load would make 51 requests [measured on dev, 2026-10-26 paced pass; 51 is a
projection from `RETAIL_HEALTH_MAX_ROWS`]. The part removes those requests. It adds three
sequential S3 reads to the parent request (the index shard, then the part and its custody), about
81–125 ms at the 27.0–41.8 ms per read dev measured on the Retail health and product-detail
serve paths [projection, derived from that pass's spans, perf review F7]; the tab's p50 after the change is to be measured on dev.

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
([Page absence copy](../page-absence-copy.md)). A preset whose publication was baked before render
format 12 has no part, so the tab serves it as that preset's `window_unavailable` with the preset
marked unavailable until Retail health's next bake; Retail health itself still serves the preset
([preset artifact publisher](../preset-artifact-publisher.md), "First bake and recovery").

## 4. Copy
Browser-owned (`PRODUCTS_AND_ADS_COPY`, `PRODUCTS_AND_ADS_LINES`): "Two systems side by side. They never
add up or divide into each other."; "There's no return on ad spend per product, because ad spend isn't
recorded per product."; a channel the row doesn't credit: "No credit served for this channel."; a
row's own served ad `reason` reads as served. A part that fails to load or decode fails the whole tab
with its page issue, as any page request does; there is no per-row failure state.
