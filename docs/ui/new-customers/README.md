# New customers

| | |
|---|---|
| **Level** | Delivery page package |
| **Status** | Built; live-accepted on dev 2026-10-18 ([D2 live acceptance](../../../validation/RESULTS.md#d2-pages-and-graph-control-plane-live-acceptance-on-dev--2026-10-18)). Owner ratification of this package and of revision `2026-10-09.2` is pending. Revision history: [design manifest](../design-manifest.yaml). |
| **Design revision** | `2026-10-09.2` |
| **Owns** | New-to-brand purchases per channel, new buyers and cost per new buyer for complete weeks and months, for the whole network and per channel, gateway products one channel at a time, and this page's insight |
| **Parent** | [Decision Workspace UI](../README.md) · [Information architecture](../information-architecture/README.md) (Customers hub) |

## 1. Purpose and scope

New customers is the first tab of the Customers hub, at `/new-customers/`. It answers "are my ads winning
shoppers who haven't bought from my brand before?" for one resolved window. New to brand uses Amazon's 12-month lookback
([verified] in the AMC documentation, rechecked 2026-10-08; owned by
[Amazon Marketing Cloud, attributed measures](../../sources/amc.md#attributed-measures--verified-column-names)).
The tab shows:

- **New-to-brand purchases per channel.** One row per ad product with its channel group: new-to-brand
  purchases, revenue, revenue share, the change in share, and cost per new-to-brand purchase.
- **Insights.** The `portfolio-acquisition` card, each inside one channel.
- **New buyers.** Distinct new-to-brand buyers and cost per new buyer, for the whole Amazon Ads network in
  one marketplace, then the same two figures per channel, with no total row. Calendar presets only; on a
  rolling preset the section reads its absence text.
- **Gateway products.** The products new-to-brand shoppers bought first, one channel at a time, brand halo
  included.

The same rules as the [Scorecard](../scorecard/README.md) hold: attributed figures stay inside one channel,
with no all-channel total, and every return-style figure carries its basis label. A withheld or missing figure
reads "Not available" with a plain reason, never zero
([Business outcome measurement](../../concepts/business-outcome-measurement.md)).

Out of scope: new buyers per campaign. The assistant answers them in chat (business outcomes pack 3); a
Campaigns-page surface is a later option. The tab points to Audiences and Retention.

## 2. Documents

- [Mockup](mockup.html), the implementation target, with one static file per review state (fictional
  values). See "Mockup notes" below.
- [High-level design](high-level-design.md): API role, composition, data lineage and build slices.
- [Low-level design](low-level-design.md): page declarations, the time policy, artifact, labels, states,
  insight contract, acceptance and open items.

## 3. Mockup notes

The mockup files are static renders of the approved information-architecture New customers mockup, one per
review state, with no scripts ([durable mockup policy](../README.md#7-durable-mockup-policy)), on the shared
[`hub-workspace.css`](../mockups/hub-workspace.css). They're rendered with the same rule slice E used for the
`2026-10-08.1` packages. The approved mockup's own "Mockup only" note stays in the 30-day and calendar
states: there the table, insight and gateway products keep the 7-day figures.

Differences from the approved mockup:

1. **No Alerts bell or inbox.** The owner deferred the alert evaluator on 2026-10-07 (OD-7).
2. **The date control checks the state's own preset** in each state file, as the approved mockup's script
   does when that state loads.

The low-level design differs from the approved mockup in eight points that the build follows, each recorded
in its open items for owner ratification (S-1, and S-B1 for point 8):

1. The rolling-preset absence text uses the shared designed wording.
2. The New buyers basis label is built from the metric dictionary.
3. The week and month cost-per-new-buyer labels share one form.
4. The step-1 caption names "excludes brand halo", and the gateway column reads "New-to-brand purchases, brand
   halo included".
5. The scope band's preset label is the server's "7D", not "(7 days)".
6. Gateway products list at most 10 per channel, with the foot "And {N} more products."
7. The change-in-share column isn't rendered on calendar presets.
8. New buyers adds a per-channel table under the platform tiles (revision `2026-10-09.2`). Only the month
   state, `mockup-last-complete-month.html`, draws it, with fictional figures marked "Mockup only".

## 4. Implemented delivery boundary

Built locally in the "New customers" sub-slice of Slice 9 of the
[delivery slices](../information-architecture/delivery-slices.md#3-slices) (2026-10-11), not yet deployed: the
server half from business outcomes wave 2b (the `NewCustomersArtifact` v2 bake for the four presets, with its
coverage block and share change through the shared builders), `POST /api/presentation/new-customers`
(owner-approved for dev, S-9), and the `/new-customers/` page in the Customers hub with the designed states in
§6 of the [LLD](low-level-design.md). Prompt administration lists `portfolio-acquisition` only while the
`new-customers` tab is in the session's manifest.

Not built: the Audiences and Retention pointer lines render only once those tabs are in the manifest, which
they aren't yet. The server never serves the declared `insufficient` outcome, so it has no rendered state; that
is an owner item. The two halves merge into integration together.
