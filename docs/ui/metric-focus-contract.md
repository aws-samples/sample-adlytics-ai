# Metric-focus contract

| | |
|---|---|
| **Type** | Shared UI interface concept |
| **Status** | Design baseline, revision `2026-09-17.2`; §3's result grammar revised at `2026-10-01.1` to `MetricFocus.v3` (two-place monetary display values and per-entry units, [ADR-084](../decisions/adr-084-display-monetary-values-at-currency-scale-from-a-server-display-value.md)) and at `2026-10-07.1` to `MetricFocus.v4` (server change blocks, [ADR-121](../decisions/adr-121-declare-change-direction-and-sentiment-on-the-server.md)); §3 revised at `2026-10-05.1` (driver scope, share basis, chips and drivers summary; design §4.6, C2/C3); §4's interpretation-absence copy revised at `2026-09-23.1` to defer to the [insight-presentation contract](insight-presentation-contract.md). Supersedes the shallow single-slot focus dialog shipped with the campaign detail: the composition below is the mandated target for every focusable surface. |
| **Owns** | The focused-metric dialog: its purpose, composition, deterministic driver decomposition, prompted interpretation slot, request/result grammar, designed states, and shared acceptance |
| **Parent** | [Decision Workspace UI](README.md) |

## 1. Purpose and boundary

One metric, one resolved window, one question: **what is this figure, how did it move, what
drove the move, and what is worth checking next.** The focus dialog is a lens over evidence
the page has already been served — never a new analytical query, never a browser calculation,
and never a causal claim.

Every metric tile on a focusable surface opens the same dialog composition. The three
focusable surfaces and their owners:

| Surface | Opens from | Decomposition grain | Owning LLD |
|---|---|---|---|
| Campaign portfolio | Portfolio outcome tiles | Per-campaign contribution | [Campaign Portfolio](campaign-portfolio/low-level-design.md) |
| Campaign detail | Detail metric tiles | Per-day steps and ratio components | [Campaign Portfolio](campaign-portfolio/low-level-design.md) |
| Retail health | Retail outcome tiles | Per-product contribution | [Retail health](retail-health/low-level-design.md) |

This contract owns the shared grammar and composition. Each page LLD owns its route, its
focusable metric roster, its decomposition semantics, and its typed absences. Asynchronous
first-fill and continuation outcomes conform to [Data-Flow Completion and Service Levels](../concepts/data-flow-completion-and-service-levels.md); deterministic dialog serving remains a synchronous route objective.

## 2. Composition

The dialog renders exactly these blocks, top to bottom. Every figure is a returned server
value; the browser never derives one. Monetary figures display at two places as the
[Decision Workspace UI](README.md) §8 states (revised `2026-10-01.1`).

1. **Identity head.** Metric display name, its one-line plain-language definition (served
   from the metric dictionary entry, never hardcoded), the resolved window, and scope pills
   (page scope, measurement basis, distributor view where applicable). Current value large,
   prior value, server-computed delta chip. A metric switcher (‹ ›) hops to the neighboring
   focusable metric without closing the dialog.
2. **Visual evidence.** Full-width current-versus-aligned-prior series at the served display
   grain, with axis labels, legend, and a single returned value rendering as a visible dot.
   An exact-values disclosure renders the same series as a table.
3. **Facts strip.** Three to five server-computed observations rendered as labeled values,
   not prose: peak day, trough day, largest single-day move, and the window total or average
   as the metric's unit dictates. Facts are part of the focus payload, computed at the same
   custody as the artifact they describe.
4. **Drivers.** The deterministic decomposition of the window-over-window change at the
   surface's grain: the top contributors by absolute change, each with its exact value,
   delta chip, and share of its basis (2026-10-05). On the additive grains (campaign,
   product) a served scope label sits above the list ("Largest changes among the N campaigns
   with the most ad spend in this window, of M in the portfolio"), and a summary block below
   it states what the listed drivers, the rest of the N, and the M − N outside moved, plus
   the direction counts. A share is of the overall change when the in-scope and outside nets
   move the same way, of the in-scope net when they oppose (so no driver reads as moving
   against the portfolio when it moved with it), and absent when the in-scope changes cancel
   out. A driver that moved against its basis, or outgrew it, shows a served chip ("Moved
   against …", "Larger than …") in place of the share. The card and deep input carry the
   same strings, from one home in `metric_focus.py`. Ratio metrics (CPC, CTR, ROAS, conversion,
   return rate) additionally decompose into their declared numerator and denominator
   movements, because a ratio's "why" is arithmetic before it is narrative.
5. **Interpretation.** One prompted insight slot (`metric-focus` for the campaign surfaces;
   `retail-metric-focus` for retail) filled through the versioned, calibration-gated registry
   ([ADR-069](../decisions/adr-069-version-insight-prompts-in-a-calibration-gated-registry.md)),
   grounded exclusively in the same findings the dialog displays
   ([ADR-068](../decisions/adr-068-fill-declared-insight-slots-with-direct-structured-model-calls.md)).
   Output class Perspective (or Observed where the slot declares it); silent and unavailable
   are designed states with the shared absence copy, never an empty card.
6. **Next step.** At most two controls: a deterministic next-check chip when the drivers
   support one (for example "Open the focus view for clicks" when clicks dominate a CPC
   move), and an assistant seed chip that hands the exact scope to the workspace assistant.
   *Amended 2026-10-05 (FR-11; ADR-076 next-step marker):* when the interpretation offers a
   served next step that this dialog can open, its control replaces the next-check chip, so
   the dialog still shows at most two controls and never repeats a metric. No interpretation
   offers one until the gated card-next-step prompt ships. The dialog opens a metric step on its
   own tab, and an "Ask the assistant" step closes it first
   ([insight presentation contract](insight-presentation-contract.md) §2.3).
7. **Basis boundary.** Rendered when the metric's basis could be misread across pages (retail
   actuals beside ad-attributed outcomes), using the owning artifact's boundary text.

## 3. Request and result grammar

The dialog never issues a measurement. Its payload derives from evidence that already exists
for the resolved window:

- **Campaign detail** keeps its capability-bound route: `POST` with
  `{session_id, selection_reference, metric}`. The response widens to carry the definition
  line, facts, and drivers beside the existing series and insight slot.
- **Campaign portfolio and retail health** gain sibling parent-scope routes accepting
  `{session_id, metric, temporal_selection}` restricted to `omitted` or a declared preset
  ([ADR-070](../decisions/adr-070-serve-declared-preset-windows-from-publish-time-artifacts.md)):
  the server reads the same baked artifact the page was served from and derives facts and
  drivers from its rows server-side. No live measurement path exists for a preset window.
- The prompted slot generates on demand at request time through the registry's active
  version and caches per (page, window, metric, artifact revision) — the campaign
  metric-focus generation path is the precedent. Regeneration happens only when the
  underlying artifact revision changes.

The focus result is versioned and exact — strict decoders on both sides mean the shape below
is the contract, not an illustration:

```text
MetricFocus.v4 = {
  focus_version: 4,
  metric: { id, label, definition, unit: {kind, currency_code}, basis, grain },
  period, prior_period,
  current, prior,                     # exact metric values with unit/basis/grain/period,
                                      # plus display_value: two places for currency, else null
  change: Change,                     # ADR-121: { direction, sentiment, unit,
                                      #   display_text, not_meaningful }
  series: { points: [{date, current, prior}], display_grain } | null,
  facts: [{ id, label, value, qualifier? }],          # closed per-surface fact roster
  drivers: {
    grain: "campaign" | "day" | "ratio_component" | "product",
    scope: { count, population, label, share_suffix | null } | null,   # null exactly on
    share_basis: "portfolio" | "scope" | "none" | null,                # ratio_component, day
    entries: [{ name, change, units: {kind, currency_code}, display_change,
                share_of_move | null, delta: Change,
                moves_against_basis: bool, chip: string | null }],      # chip replaces share
  } | { absent_reason },              # plain words, never a raw code
  drivers_summary: { listed_change, other_in_scope_change, outside_scope_change,
                     rose, fell, flat, lines: [string] } | null,         # changes {value, display}
                                                                        # or null when omitted
  insight: { slot_id, output_class, state, text?, evidence_line?,
             prompt_version, artifact_revision,
             absence_class?, absence_reason? },   # ADR-068 absence taxonomy (2026-09-23)
  neighbors: [metric ids],            # the switcher roster, page-declared order
  next_check: { metric_id, label } | null,
  boundary: string | null,
}
```

Every value carries its unit/basis/grain/period exactly as the owning artifact does; typed
absences replace any block that cannot be served. Each driver entry's `units` are its own, so
a ratio component may differ from the focused metric (Clicks under CPC is a count); its
`display_change` is two places for a currency entry and null otherwise. `MetricFocus.v4`
(revised `2026-10-07.1`, [ADR-121](../decisions/adr-121-declare-change-direction-and-sentiment-on-the-server.md))
replaces the head `{ absolute, percentage }` and each entry's `delta_percentage` with the
server's change block, whose tone comes from the metric dictionary's `favourable_direction`;
it retires `MetricFocus.v3` in the same release, because nothing retains a focus response. A
bundle left open across the API deploy reads the focus as unavailable until it reloads. `MetricFocus.v3`
(revised `2026-10-01.1`, [ADR-084](../decisions/adr-084-display-monetary-values-at-currency-scale-from-a-server-display-value.md))
retires `MetricFocus.v2` in the same release, with no dual decoding. The existing shallow campaign focus
response (an unversioned `{session_id, temporal, metric, insight}` envelope) is superseded
by this shape and retired in the same release that ships it — no dual decoding.

## 4. Designed states

- **Absent series** (one-day window on a series-requiring surface): the facts and drivers
  blocks render from the single returned values; the chart renders the dot form. Never a
  blank panel.
- **Partial coverage**: covered days chart normally beside the dated gap region; facts and
  drivers state the covered span they derive from.
- **Silent or unavailable interpretation**: rendered under the
  [insight-presentation contract](insight-presentation-contract.md), which owns absence
  rendering wherever an insight slot appears — the served reason verbatim with the
  treatment its served class selects, never a generic fallback line (revised
  `2026-09-23.1`; the previous hardcoded silent copy is retired).
- **Absent decomposition** (a metric with no declared drivers): the drivers block names, in
  plain words, why there is nothing to decompose — never a raw reason code.
- A metric the page declares non-focusable renders a static tile with no opener; a tile is
  never a dead button.

## 5. Interaction and accessibility

Opened from a tile button labeled "Open the focus view for {metric}". The page behind dims
and goes inert; focus is trapped; Escape and the ✕ control close and return focus to the
opening tile; ‹ › switch metrics without closing. On compact layouts the dialog presents as a
full-height sheet. All content meets the shared theme, contrast, and keyboard rules owned by
the [Decision Workspace UI](README.md) and the
[theme contract](theme-preference-contract.md); the dialog is axe-clean on every surface.

## 6. Continuity rule

The three surfaces render the same block order, the same absence copy, the same switcher and
close behavior, and the same visual language. A user who has focused a campaign metric knows
how to read a retail focus without relearning anything. Page-specific content varies only
inside blocks 3, 4, and 7, where the decomposition grain and boundary are page-owned.

## 7. Acceptance

- Every tile on the three surfaces opens the dialog; no tile seeds the assistant as its
  primary action (the assistant seed lives inside the dialog's next-step block).
- Dialog content for a preset window is byte-derivable from the served/baked artifact plus
  the registry's active prompt version — proven by serving with the measurement path
  disabled.
- The interpretation slot ships with a fictional calibration corpus and reaches every
  environment exclusively through create-version → calibration → activate.
- Deployed browser acceptance drives: open from tile, switcher hop, Escape-restores-focus,
  axe-clean, and one designed-absence rendering per surface.
