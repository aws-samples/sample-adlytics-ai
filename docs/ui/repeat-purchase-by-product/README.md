# Repeat purchase by product

| | |
|---|---|
| **Level** | Delivery page package |
| **Status** | New at `2026-10-08.1`, built from the UI and information-architecture mockups and design. Approved under the owner's UI/IA approval of 2026-10-07 (`owner-decisions-ui.md`). Carried forward unchanged at `2026-10-09.1` and `2026-10-09.2`. Not implemented: its contract, fact, artifact, materializer and route arrive in their own build slice, after the Brand Analytics landing path exists |
| **Design revision** | `2026-10-09.2` |
| **Owns** | Orders, unique customers and repeat purchase revenue per product for one complete month, as reported by Amazon Brand Analytics |
| **Parent** | [Decision Workspace UI](../README.md) · [Information architecture](../information-architecture/README.md) (Customers hub) |

## 1. Purpose and scope

Repeat purchase by product is the third tab of the Customers hub, at `/repeat-purchase/`. Its description
reads "Orders, customers and repeat revenue per product, reported by Amazon Brand Analytics". It shows the
report's levels for the latest complete month, one row per product.

- **Levels only.** The page shows orders, unique customers and repeat purchase revenue. The two shares Amazon
  reports stay in the source-aligned layer; they're never recomputed, and where shown they're evidence
  labelled "reported by Amazon"
  ([ADR-105](../../decisions/adr-105-keep-source-reported-shares-source-aligned-and-publish-brand-analytics-levels-only.md)).
- **Not added across products.** Unique customers can't be summed across products or periods.
- **All repeat purchases.** Brand Analytics counts every order of the product, whether or not an ad was
  involved. That differs from [Retention](../retention/README.md), which counts ad-touched repeats of the
  cohorts ads brought in. Each page points to the other and says which repeats it counts.
- **No join to advertising data.** The page never joins a product to AMC cohorts or campaigns.

Without Brand Analytics access, the tab reads "Needs Amazon Brand Analytics access" and lists the onboarding
prerequisites. It's in navigation either way (OD-4, 2026-10-07), so the absence is visible rather than
hidden. [Retail health](../retail-health/README.md) links here through a declared page action.

## 2. Documents

- [Mockup](mockup.html), the implementation target, with one static file per review state (fictional
  values).
- [High-level design](high-level-design.md): API role, composition, data lineage and build slice.
- [Low-level design](low-level-design.md): page declarations, artifact, states and acceptance.

## 3. Mockup notes

The mockup files are static renders of the approved UI/IA Repeat purchase by product mockup, one per review
state, with no scripts ([durable mockup policy](../README.md#7-durable-mockup-policy)), on the shared
[`hub-workspace.css`](../mockups/hub-workspace.css). The one difference from the approved mockup: no Alerts
bell or inbox (OD-7 deferred, 2026-10-07). This tab has no insight contract, so it never has an "Also
changed" section.

## 4. Implemented delivery boundary

Nothing is built. The page arrives with the business outcomes slice S4.3, which needs the forecasting
design's governed API operation pack for the Brand Analytics report, and Slice 9 of the
[delivery slices](../information-architecture/delivery-slices.md#3-slices). Its new API route needs its own
`infra/` approval.
