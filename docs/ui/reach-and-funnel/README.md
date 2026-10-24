# Reach and funnel

| | |
|---|---|
| **Level** | Delivery page package |
| **Status** | Built; week and month live-accepted on dev 2026-10-18, quarter not ([D2 live acceptance](../../../validation/RESULTS.md#d2-pages-and-graph-control-plane-live-acceptance-on-dev--2026-10-18)). Owner ratification of revision `2026-10-09.2` is pending. Revision history: [design manifest](../design-manifest.yaml). |
| **Design revision** | `2026-10-09.2` |
| **Owns** | How many shoppers the ads reached in one complete period, how the DSP and sponsored audiences overlap, what each exposure group did next, and how purchase rates move with the number of ads seen |
| **Parent** | [Decision Workspace UI](../README.md) · [Information architecture](../information-architecture/README.md) (Reach and audiences hub) |

## 1. Purpose and scope

Reach and funnel is the first tab of the Reach and audiences hub, at `/reach/`. It answers "who did my ads
reach, how often, and what did they do next?" for one complete week, month or quarter. It has three parts:

- **Overlap.** Shoppers grouped by the ads they saw within 14 days of their first ad in the period: DSP
  only, sponsored ads only, or both.
- **Full funnel.** Search rate, purchaser rate and revenue per shopper for each of those groups.
- **Frequency.** Purchaser rate by the number of ads seen, and return per channel by frequency.

Every shopper count comes from Amazon Marketing Cloud (AMC) as a privacy-thresholded total, for one exact
complete period. Shopper IDs never leave AMC
([Business outcome measurement](../../concepts/business-outcome-measurement.md)). The groups aren't random,
so every full-funnel and frequency figure carries the caveat "This shows association, not cause"
([ADR-102](../../decisions/adr-102-flag-association-only-metrics-and-require-their-caveat.md)), inside the
visual and never as a footnote.

Out of scope here: reach per campaign-day, which moved to campaign detail's Reach slot on the 1-day preset
(OD-4, 2026-10-07), and audience segments, which live on [Audiences](../audiences/README.md).

## 2. Documents

- [Mockup](mockup.html), the implementation target, with one static file per review state (fictional
  values).
- [High-level design](high-level-design.md): API role, composition, data lineage and build slices.
- [Low-level design](low-level-design.md): page declarations, sections and their periods, artifact, labels,
  states, insight contracts and acceptance.

## 3. Mockup notes

The mockup files are static renders of the approved UI/IA Reach and funnel mockup, one per review state, with
no scripts ([durable mockup policy](../README.md#7-durable-mockup-policy)), on the shared
[`hub-workspace.css`](../mockups/hub-workspace.css). The one difference from the approved mockup: no Alerts
bell or inbox, because the owner deferred the alert evaluator on 2026-10-07 (OD-7). The approved mockup
draws no "Also changed" section, and none renders in v1, because no governed alert rule covers this page's
findings yet (low-level design, "Also changed").

## 4. Implemented delivery boundary

Nothing is built. The page arrives with the business outcomes slices S2.2 (overlap and funnel) and S3.2
(frequency), and Slice 9 of the [delivery slices](../information-architecture/delivery-slices.md#3-slices).
Its new API route needs its own `infra/` approval.
