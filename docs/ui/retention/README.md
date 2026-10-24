# Retention

| | |
|---|---|
| **Level** | Delivery page package |
| **Status** | Built; live-accepted on dev 2026-10-18 ([D2 live acceptance](../../../validation/RESULTS.md#d2-pages-and-graph-control-plane-live-acceptance-on-dev--2026-10-18)). Owner ratification of revision `2026-10-09.2` is pending. Revision history: [design manifest](../design-manifest.yaml). |
| **Design revision** | `2026-10-09.2` |
| **Owns** | Cohorts of new customers the ads brought in and when they buy again: the cohort grid per acquisition group, the cross-group repeat curve, and their vintages |
| **Parent** | [Decision Workspace UI](../README.md) · [Information architecture](../information-architecture/README.md) (Customers hub) |

## 1. Purpose and scope

Retention is the second tab of the Customers hub, at `/retention/`. It answers "do the new customers my ads
brought in come back?". A cohort is the shoppers whose first new-to-brand purchase in 12 months fell in one
month, credited to ads
([ADR-104](../../decisions/adr-104-define-acquisition-cohorts-by-amazons-new-to-brand-flag.md)). The tab shows,
for one vintage ("Cohorts as of September 2026", fictional):

- **The cohort grid.** For each cohort month, the share that bought again in each month after acquisition,
  for one acquisition group at a time.
- **The repeat curve.** Cumulative repeat rate by month since acquisition, one line per acquisition group,
  on Amazon account IDs.

Two paths exist and never form one series. The standard path counts ad-touched repeat purchases only, so it
understates true repeat. The Amazon Retail Purchases path, a paid feature, counts all Amazon purchases on
Amazon account IDs. A cell that didn't come back is hatched and reads "Not available", never 0: too few shoppers bought again, or none.

Cohorts are monthly. When too many monthly cells come back not available for a marketplace, because they had too few shoppers, later vintages there
group cohorts by calendar quarter, with follow-up quarters, and say so. A monthly vintage and a
quarterly one are never compared
([low-level design](low-level-design.md#quarterly-cohorts-added-at-2026-10-092)).

The tab points to [Repeat purchase by product](../repeat-purchase-by-product/README.md), which counts every
repeat purchase per product from Amazon Brand Analytics.

## 2. Documents

- [Mockup](mockup.html), the implementation target, with one static file per review state (fictional
  values).
- [High-level design](high-level-design.md): API role, composition, data lineage and build slices.
- [Low-level design](low-level-design.md): page declarations, the vintage policy, artifact, states, insight
  contract and acceptance.

## 3. Mockup notes

The mockup files are static renders of the approved UI/IA Retention mockup, one per review state, with no
scripts ([durable mockup policy](../README.md#7-durable-mockup-policy)), on the shared
[`hub-workspace.css`](../mockups/hub-workspace.css). The one difference from the approved mockup: no Alerts
bell or inbox (OD-7 deferred, 2026-10-07). The approved mockup doesn't draw the repeat curve; the low-level
design keeps it under the owner's render-more principle.

## 4. Implemented delivery boundary

Nothing is built. The page arrives with the business outcomes slices S4.1 (standard path) and S4.2 (the paid
path), and Slice 9 of the [delivery slices](../information-architecture/delivery-slices.md#3-slices). Its new
API route needs its own `infra/` approval.
