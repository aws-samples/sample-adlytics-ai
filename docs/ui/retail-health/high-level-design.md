# Retail health — high-level design

## 4. Solution strategy

One route, one artifact, no children: the page is a single parent surface (no selection
capability, no detail redemption) whose every window variant is served from a publish-time
artifact through the shared [preset artifact publisher](../preset-artifact-publisher.md).
Composition, shell, theme, temporal control, and insight-slot mechanics are the shared
command-center language ([Decision Workspace UI](../README.md),
[temporal-control contract](../temporal-control-contract.md),
[ADR-068](../../decisions/adr-068-fill-declared-insight-slots-with-direct-structured-model-calls.md));
this document records only what is specific to this page.

### Presentation API role

| Operation | Route | Browser intent | Server responsibility |
|---|---|---|---|
| Retail health | `POST /api/presentation/retail-health` | Session, optional preset `TemporalSelection.v1`, optional declared sort. | Resolve the shared control (preset-only), serve the revision-current baked artifact for the resolved variant, order product rows, respond. No capabilities are minted: the page has no child redemption. |

### Composition (top to bottom, per the mockup)

1. Title band with scope pills (product count, basis, distributor view, reconciliation and
   freshness state) and preset window cards.
2. Four purpose-typed insight slots: demand-shape, returns-pressure, ordered-vs-shipped
   (each Perspective), and retail-movers (Observed, fully deterministic like the portfolio
   movers — no prompt, no cache entry).
3. Outcome tiles in two rows: shipped revenue, shipped units, customer returns, glance views;
   then ordered revenue, COGS shipped, the Manufacturing-coherent conversion tile, and the
   typed-absent Sourcing-view tile.
4. Full-width daily trend panel with metric segments and prior overlay.
5. Reconciliation state-note: by-product rows against the vendor's reported control totals.
6. Server-ordered product table with hierarchy-labelled rows, deltas, spark series, and
   coverage pills.
7. Pointer line to [Repeat purchase by product](../repeat-purchase-by-product/README.md), the declared page
   action `open-repeat-purchase-by-product` (added at `2026-10-08.1`).
8. Basis-boundary state-note.

### Data lineage

Every value derives from the five curated Vendor Central / Data Kiosk facts and the product
dimension already governed by the data layer: `fct_retail_sales`,
`fct_retail_sales_reported_total`, `fct_product_page_views`,
`fct_product_page_views_reported_total`, `dim_product`. The page introduces **no new source
acquisition**; inventory remains out of scope until its own acquisition slice exists
([roster prerequisites](../README.md#3-capability-coverage-and-prerequisites)).
