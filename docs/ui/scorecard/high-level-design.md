# Scorecard — high-level design

## 4. Solution strategy

One tab, one baked artifact, no child view. Every declared preset is served from a publish-time artifact
through the shared [preset artifact publisher](../preset-artifact-publisher.md), as Campaigns and Retail
health are ([ADR-070](../../decisions/adr-070-serve-declared-preset-windows-from-publish-time-artifacts.md)).
Shell, navigation, date control, tables, dialogs, change chips, coverage sheet and assistant context are the
shared patterns of the [information architecture](../information-architecture/README.md#6-shared-patterns)
([ADR-118](../../decisions/adr-118-render-every-page-from-one-set-of-shared-patterns-and-extended-tokens.md)).
This document records only what's specific to the Scorecard.

The page needs no new source, pack, contract or fact. Every figure is a metric that the dictionary already
declares and the planner already serves at the grains below.

### Presentation API role

| Operation | Route | Browser intent | Server responsibility |
|---|---|---|---|
| Scorecard | `POST /api/presentation/scorecard` [proposal] | Session; optional `TemporalSelection.v2` restricted to `omitted` or a declared preset | Resolve the shared control, serve the revision-current baked artifact for that preset, and build link text from target pointers at serve time |
| Scorecard metric focus | `POST /api/presentation/scorecard-metric-focus` [proposal] | Session, a declared focusable metric, the same temporal selection; for a ranking row, `channel` and `model` ([ADR-126](../../decisions/adr-126-render-the-scorecard-ranking-under-each-attribution-model.md) decision 1) | Derive the focus payload from the same baked artifact, with no live measurement |

Both routes are new API Gateway routes, so each is an `infra/` change that needs its own approval
([delivery slices](../information-architecture/delivery-slices.md#2-rules-for-every-slice) rule 5).

### Composition (top to bottom, per the mockup)

The tab follows the shared tab order (scope band, headline, insights, "Also changed", primary table,
"About these figures"), with the network card as its headline:

1. Hub header: "Performance" eyebrow, tab strip (Scorecard, Campaigns), h1 "Scorecard", the description
   "Return on ad spend per network and channel", the date control, then the scope band.
2. **Network card**, one per network × marketplace: the deduplicated ROAS headline at the server's default
   attribution model, "Ad spend, all channels", the model selector "Attribution model for deduplicated
   ROAS" with its server note, and "Does the ranking hold under every model?".
3. **Insights row**: the `network-scorecard` cards, each inside one network and one channel.
4. **Also changed**: findings above an alert threshold that no insight card already narrates. Skipped when
   there are none.
5. **By channel** table: spend, Amazon-attributed revenue, ROAS and ROAS with brand halo per channel, and a
   link per row to Campaigns filtered to that channel.
6. One pointer line to Customers › New customers, and, only when the route manifest lists the Budget
   simulator, a second pointer line to it.
7. **Ad spend and retail sales**: the TACoS row and the spend-across-networks row.
8. **About these figures** disclosure.

### What changed from the business outcomes design (owner-approved amendments)

The owner accepted five business outcomes amendments on 2026-10-07 (OD-4). Three apply to this page:

- New-to-brand and gateway products move to Customers › New customers. One pointer line replaces them.
- The reach-and-funnel summary band is removed, so reach and the full funnel have one home,
  [Reach and funnel](../reach-and-funnel/README.md).
- The channel-group subtotal rows are dropped. A "display" or "search" subtotal ROAS would be an attributed
  ratio across channels.

The render-more extension adds the ranking under each model
([ADR-126](../../decisions/adr-126-render-the-scorecard-ranking-under-each-attribution-model.md)), and the
model selector stays (owner, 2026-10-07).

### Data lineage

Every figure reads existing governed facts: Amazon Ads delivery and attributed conversions per channel and ad
product, the deduplicated temporal attribution fact under each registered model, and Vendor Central shipped
revenue for TACoS. The metrics are `ad_spend`, `attributed_revenue_14d`, `roas_attributed_14d`,
`roas_attributed_incl_halo_14d`, `roas_deduplicated_incl_halo_14d` and `tacos`
[repo: `contracts/shared/metric-dictionary.yaml:112`, `:273`, `:1009`, `:1069`, `:1091`, `:1381`]. Only
`ad_spend` aggregates across networks
([ADR-106](../../decisions/adr-106-keep-outcome-metrics-inside-one-network-by-type.md)).

### Build slice and dependencies

| Needs | Why |
|---|---|
| Periodic serving, server half (built) and temporal transport v2, browser half (pending) | The calendar presets "Latest measured week" and "Latest measured month" need `temporal_control_version` 2 ([ADR-100](../../decisions/adr-100-serve-periodic-metrics-only-for-complete-measured-periods.md)) |
| Shared patterns: change block, coverage block, next steps, page context | [ADR-119](../../decisions/adr-119-serve-window-coverage-as-one-server-written-evidence-status-block.md) to [ADR-122](../../decisions/adr-122-declare-next-step-targets-as-a-typed-union-checked-against-navigation.md) |
| Campaigns channel filter | Channel rows link to `/campaigns/?channel=…` only where Campaigns declares that filter ([ADR-127](../../decisions/adr-127-serve-the-campaigns-channel-filter-from-render-only-siblings-of-one-bake.md)) |
| Pointer `resolved_window` | Link text names the target's dates ([ADR-116](../../decisions/adr-116-give-every-tab-one-time-model-and-one-window.md)) |
| A route approval | The two new routes above |
