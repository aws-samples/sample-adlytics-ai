# Scorecard

| | |
|---|---|
| **Level** | Delivery page package |
| **Status** | Built; live-accepted on dev 2026-10-18 ([D2 live acceptance](../../../validation/RESULTS.md#d2-pages-and-graph-control-plane-live-acceptance-on-dev--2026-10-18)). The UI refresh added the channel cards, on `main` since PR #8. Revision history: [design manifest](../design-manifest.yaml). |
| **Design revision** | `2026-10-09.2` |
| **Owns** | Return on ad spend per network and channel: the deduplicated network headline, the ranking under each attribution model, the channel table, TACoS and spend across networks, and this page's insight and "Also changed" rows |
| **Parent** | [Decision Workspace UI](../README.md) · [Information architecture](../information-architecture/README.md) (Performance hub) |

## 1. Purpose and scope

The Scorecard answers "is my ad spend paying back, by network and channel?" for one resolved window.
It's the first tab of the Performance hub, at `/scorecard/`. Each advertising network gets its own card
per marketplace. Inside the Amazon network, the card shows a deduplicated return that credits each
purchase once, and a table shows each channel's spend and Amazon-attributed return.

Three rules shape every figure on the page:

- **One network per outcome.** Spend may be totalled across channels, and across networks within one
  marketplace and currency. Attributed sales and returns are never totalled across channels or networks
  ([ADR-106](../../decisions/adr-106-keep-outcome-metrics-inside-one-network-by-type.md)). The table has
  no attributed total row and no channel-group subtotal rows.
- **A basis on every return.** Each return figure carries its basis label: who attributed it, the window,
  the date basis, and whether brand halo is included.
- **Not available over hiding.** A withheld or missing figure reads "Not available" with a plain reason,
  never zero ([Business outcome measurement](../../concepts/business-outcome-measurement.md)).

Out of scope here: new-to-brand customers and gateway products, which live on Customers › New customers,
and reach, which lives on [Reach and funnel](../reach-and-funnel/README.md). The page points to both.

## 2. Documents

- [Mockup](mockup.html), the implementation target, with one static file per review state (fictional
  values). See "Mockup notes" below.
- [High-level design](high-level-design.md): API role, composition, data lineage and build slice.
- [Low-level design](low-level-design.md): page declarations, artifact, sections, states, insight
  contract and acceptance.

## 3. Mockup notes

The mockup files are static renders of the approved UI/IA Scorecard mockup, one per review state, with no
scripts (the [durable mockup policy](../README.md#7-durable-mockup-policy)). They share the stylesheet
[`hub-workspace.css`](../mockups/hub-workspace.css), a copy of the approved mockups' stylesheet.

Two differences from the approved mockup, both required by binding owner decisions:

1. **No Alerts bell or inbox.** The owner deferred the alert evaluator on 2026-10-07 (OD-7), so the
   alerts-declined path applies. The approved mockup drew the alerts-approved path.
2. **An "Also changed" section** between the insights row and the channel table, the declined path's
   section in the tab order of the information architecture. Its one fictional row is direction-only.

The approved mockup's "insight dialog opened from an alert" state belongs to the alerts-approved path, so
it isn't rendered.

## 4. Implemented delivery boundary

Built locally in sub-slice 9a of the
[delivery slices](../information-architecture/delivery-slices.md#3-slices) (2026-10-10), not yet deployed:
the baked artifact for the four presets, `POST /api/presentation/scorecard` (owner-approved for dev), the
`/scorecard/` page with the ranking under each model, insight cards, "Also changed" from the governed
alert rules, the channel table with links to Campaigns and the "Ad spend and retail sales" section.

Not built, all moved to sub-slice 9b: the metric focus ("Details ›" openers and the ranking's "By day"
column) and its `scorecard-metric-focus` route; the month state's "Channel return" card
(`channel-roas-leader`), which needs a new level finding kind and a materiality calibration
(the [LLD](low-level-design.md) §6 and §10 "or the card doesn't render" outcome, orchestrator
decision of 2026-10-10).

Mockup amendments awaiting the owner's visual sign-off (orchestrator decisions of 2026-10-10; the LLD and
the shared contracts govern where the mockups differ):

1. Arrival from campaign detail outlines and tags the channel row only. The mockup's callout above the
   network card ("Showing the channel of {campaign} · {channel}") needs a campaign name neither the URL
   nor the body carries. Candidate for 9b: a channel-only server callout.
2. The month state renders its two served cards. The "Channel return" card moves to 9b (above).
3. The insight dialog is the shared dialog of the
   [insight presentation contract](../insight-presentation-contract.md) §3: the narration, no deep band
   (`network-scorecard` declares none) and the deterministic provenance line. The mockup's "Readings"
   and "How to tell" need campaign-grain data, and its "Written by an AI model" line contradicts a
   deterministic slot. Candidate for 9b: a served stat group and a "How this is calculated" disclosure.
