# Reach and funnel — high-level design

## 4. Solution strategy

One tab, one baked artifact, no child view, served from publish-time artifacts through the
[preset artifact publisher](../preset-artifact-publisher.md). The tab offers calendar presets only, so every
figure is one exact complete period that AMC counted
([ADR-100](../../decisions/adr-100-serve-periodic-metrics-only-for-complete-measured-periods.md)). Shared
shell, date control, tables, dialogs, change chips, coverage sheet and assistant context follow the
[information architecture](../information-architecture/README.md#6-shared-patterns). This document records
only what's specific to this tab.

### Presentation API role

| Operation | Route | Browser intent | Server responsibility |
|---|---|---|---|
| Reach and funnel | `POST /api/presentation/reach` [proposal] | Session; optional `TemporalSelection.v2` restricted to `omitted` or a declared calendar preset | Resolve the calendar preset to one complete period with `resolve_calendar_preset`, serve that period's baked artifact, and build link text at serve time |
| Reach metric focus | `POST /api/presentation/reach-metric-focus` [proposal] | Session, a declared focusable metric, the same selection | Derive the focus payload from the baked artifact, with the association caveat on top for full-funnel and frequency subjects |

Both routes are new API Gateway routes and need their own `infra/` approval.

### Composition (top to bottom, per the mockup)

1. Hub header: "Reach and audiences" eyebrow, tab strip (Reach and funnel, Audiences), h1 "Reach and
   funnel", the description "How many shoppers your ads reached, how often, and what they did next", the
   date control (Latest measured week, month, quarter), then the scope band.
2. **Overlap between DSP and sponsored ads**: three tiles on Amazon account IDs ("Saw both", "Saw both, all
   ID types", "Saw DSP only"), a three-part bar with its table equivalent, and the window note.
3. **Full funnel by exposure group**: the caveat block, then rates per group in a table.
4. **Insights row**: `reach-overlap` and `full-funnel-association` cards, and `frequency-saturation` when
   it names a bucket.
5. **Frequency**: the caveat block, purchaser rate by number of ads seen (chart plus exact values), and
   return per channel by frequency with the platform view beside it.
6. **About these figures**.

"Also changed" sits after the insights row when a governed alert rule covers one of this tab's findings. No
rule does in v1, so it isn't rendered.

### Data lineage

| Section | Fact | Packs and build slice |
|---|---|---|
| Overlap, full funnel | The exposure-group outcome fact (exposure group × identity space, per complete period) | Pack 5, slice S2.2 |
| Frequency | The exposure-frequency fact (frequency bucket × identity space; window and credit rows) | Packs 6 and 7, slice S3.2 |

Neither fact is in the [canonical model](../../canonical-model.md) yet; each joins it, with its columns, in the
slice that ships its first pack. Both follow the shopper-aggregate rules already in the domain: closed enumerations write
`not_returned` gaps, loss is graded per slice, and distinct counts are never summed across deliveries
([ADR-099](../../decisions/adr-099-treat-distinct-shopper-counts-as-a-new-kind-of-fact.md),
[ADR-101](../../decisions/adr-101-model-shopper-groupings-as-disjoint-axes-with-one-window-per-shopper.md)).

### Build slices and dependencies

| Needs | Why |
|---|---|
| Temporal transport v2 in the browser | Every preset on this tab is a calendar preset |
| P-BO-1 sub-gate criteria and P-BO-5 | The packs are gated on the live AMC probes; until they pass on a real instance, figures are synthetic in dev only |
| Packs 5, 6 and 7 with their facts | The tab's three parts |
| Shared patterns ([ADR-119](../../decisions/adr-119-serve-window-coverage-as-one-server-written-evidence-status-block.md) to [ADR-122](../../decisions/adr-122-declare-next-step-targets-as-a-typed-union-checked-against-navigation.md)) | Change chips, coverage, next steps and page context |
| A route approval | The two new routes |
