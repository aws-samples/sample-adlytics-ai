# Audiences

| | |
|---|---|
| **Level** | Delivery page package |
| **Status** | New at `2026-10-08.1`, built from the UI and information-architecture mockups and design. Approved under the owner's UI/IA approval of 2026-10-07 (`owner-decisions-ui.md`). Carried forward unchanged at `2026-10-09.1`. Revised at `2026-10-09.2` (owner decision 2 of 2026-10-09 and P-BO-4): each segment is labelled by its provider, Amazon or third party, and the page states that DSP behaviour segments and Amazon audience segments use different IDs and are never linked; approved under the same UI/IA approval, with owner ratification pending (business outcomes wave 2b, stop item S-B1). Not implemented: its packs, facts, dimension, artifact, materializer, route and insight contract arrive in their own build slice |
| **Design revision** | `2026-10-09.2` |
| **Owns** | Which Amazon audience segments buyers belong to compared with the shoppers the ads reached, and how DSP campaigns did per segment |
| **Parent** | [Decision Workspace UI](../README.md) · [Information architecture](../information-architecture/README.md) (Reach and audiences hub) |

## 1. Purpose and scope

Audiences is the second tab of the Reach and audiences hub, at `/audiences/`. For the latest complete
month, it shows:

- **Segment profile.** For each audience segment, its share of buyers, its share of reached shoppers, and
  the index between them. Two profiles exist: "Amazon audience segments (paid)", which needs the Audience
  Segment Insights paid feature, and "DSP-exposed buyers only", on standard AMC tables.
- **DSP campaigns by segment.** For one DSP campaign, impressions, CPM and ROAS per segment.

Each segment is labelled by its provider, "Amazon" or "Third party", never all as Amazon's. DSP behaviour
segments and Amazon audience segments use different IDs, so the page never links them or compares them as
one list ([low-level design](low-level-design.md#6-wire-artifact-and-renderer-contracts)).

Segments overlap: one shopper can belong to many. So segment figures never add up, shares never total 100%,
and no "all other" row exists
([ADR-103](../../decisions/adr-103-model-audience-segments-as-a-source-namespaced-overlapping-dimension.md)).
The page describes an audience the customer can build in their own tools. Adlytics doesn't create or
activate audiences: the platform is read-only.

A paid feature that hasn't delivered reads "Available with the paid feature", never hidden and never a claim
that the customer isn't subscribed
([ADR-107](../../decisions/adr-107-derive-paid-feature-availability-from-delivery-evidence.md)).

## 2. Documents

- [Mockup](mockup.html), the implementation target, with one static file per review state (fictional
  values).
- [High-level design](high-level-design.md): API role, composition, data lineage and build slice.
- [Low-level design](low-level-design.md): page declarations, sections, artifact, roster, states, insight
  contract and acceptance.

## 3. Mockup notes

The mockup files are static renders of the approved UI/IA Audiences mockup, one per review state, with no
scripts ([durable mockup policy](../README.md#7-durable-mockup-policy)), on the shared
[`hub-workspace.css`](../mockups/hub-workspace.css). The one difference from the approved mockup: no Alerts
bell or inbox, because the owner deferred the alert evaluator on 2026-10-07 (OD-7).

## 4. Implemented delivery boundary

Nothing is built. The page arrives with the business outcomes slice S3.1 (packs 8 to 13) and Slice 9 of the
[delivery slices](../information-architecture/delivery-slices.md#3-slices). Its new API route needs its own
`infra/` approval. The paid profile is free to query until 2026-12-31 [verified 2026-10-05, AMC paid
features overview, carried from the business outcomes design], which is why S3.1 is the paid-feature slice
that matters most.
