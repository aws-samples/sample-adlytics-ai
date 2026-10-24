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
`ProductsAndAdsRouteAdapter` requests the served Retail health presentation for the selected preset,
with Retail health's own window control and URL, then each row's baked product detail
(`RetailProductDetail`, [ADR-125](../../decisions/adr-125-serve-retail-product-detail-as-a-baked-child-of-retail-health.md))
over the row's `selection_reference`, at most four in flight (`frontend/lib/products-and-ads.ts`). A
detail is used only when its `parent_revision`, `period` and `product.child_asin` match the row's
parent; any other answer, or a failed request, reads as that row's absence. On dev on 2026-10-22 a
read-only fetch of one 7-day detail served `ad_attributed` rows for Amazon DSP and Sponsored Products.

| Column | Slot | Served field |
|---|---|---|
| Product | `pa-{asin}` | RHA `rows[].title`, `child_asin` |
| Shipped sales, Change | `pa-{asin}-rev` | RHA `rows[].comparisons[retail_revenue_shipped]` current and change (withheld per coverage) |
| Product page views | `pa-{asin}-views` | RHA `rows[].comparisons[glance_views]` current |
| Units per 1,000 page views | `pa-{asin}-conv` | the detail's `totals[conversion_rate_retail]`; the column renders only when a detail serves it |
| Ad-credited sales · {channel} | `pa-{asin}-ad-rev` | the detail's `ad_attributed.rows[channel].revenue` (`attributed_revenue_14d`) current and change |
| Ad-credited purchases | `pa-{asin}-purchases` | the detail's `ad_attributed.rows[channel].purchases` current |
| First-time purchases | `pa-{asin}-ntb` | not served at product grain: reads "Not measured yet" |

The channel switch lists the channels the served details credit, in the order first served. The
table's note carries the served `ad_attributed.basis`. No halo-sales column renders: nothing serves
`attributed_revenue_halo_14d` at product grain.

## 3. Answer template
"{N} products shipped in {window}; {first title} shipped most, {revenue}, {up/down} {change} on
{prior window}." from the served `total_count` and the first served row, which the server orders by
shipped revenue. A withheld or absent change drops the last clause. An empty or insufficient body
shows Retail health's ABS-RH-3 or ABS-RH-4 copy; a page-level absence shows ABS-RH-1
([Page absence copy](../page-absence-copy.md)).

## 4. Copy
Browser-owned (`PRODUCTS_AND_ADS_COPY`, `PRODUCTS_AND_ADS_LINES`): "Two systems side by side. They never
add up or divide into each other."; "There's no return on ad spend per product, because ad spend isn't
recorded per product."; a row whose detail is refused or failed: "This product's ad figures can't be
shown right now."; a row with no capability: "Ad figures open once this window is baked again."; a
channel the detail doesn't credit: "No credit served for this channel."; a detail's own
`ad_attributed.reason` reads as served.
