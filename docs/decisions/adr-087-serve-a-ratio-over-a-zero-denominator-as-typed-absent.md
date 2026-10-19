# ADR-087: Serve a ratio over a zero denominator as typed absent

Date: 2026-10-05
Status: Accepted 2026-10-05 (orchestrator decision under owner UX decisions #4 and #8 of
2026-10-05). Implemented in the repository on `feat/insight-data-ux-refresh`; not deployed.
Contract change: flagged to the build reviewers.

> **Widened by [ADR-099](adr-099-treat-distinct-shopper-counts-as-a-new-kind-of-fact.md) (Proposed, owner-approved design, 2026-10-08).** An `exact_distinct` metric with no row is typed absent too: too few shoppers or none, never 0. Point 1's "which metrics" is read from the dictionary in one place, `Metric.absence_is_typed` (`src/domain/catalog/capability.py`). The campaign portfolio's metrics are unchanged: none is a distinct count, so point 2's construction rule and the wire stay as they are.

## Context

The shaped dev seed (ADR-086) adds a pause and a launch, the first zero-delivery days in
serving. The local serve-shape check (design §3.3 check 2) rendered them through the
publisher's renderer and found attributed ROAS served as `0` for a campaign with no spend, and
CTR as `0` with no impressions. The governed query is right: a quotient over a zero denominator
has no value, so the covered ratio answer omits that campaign. The portfolio reducer then read
the omission as `0`, a rule that is correct only for additive metrics. A ROAS of 0.00 claims the
campaign spent and earned nothing back, which is a different, false finding.

## Decision

**A portfolio ratio whose row is absent from a fully covered ratio answer is typed absent
(`null`), never 0. Additive metrics keep absence = 0.**

1. **Which metrics.** A metric is a ratio when the metric dictionary declares it one
   (`MetricKind.RATIO` on the plan's metric), never by its name. Today that is attributed ROAS
   and CTR on rows. Portfolio totals are reduced from operands and are always defined.
2. **The value.** `CampaignPortfolioMetricValue.value` may be `None` only when its units are a
   ratio; any other `None` is refused at construction and decode. A comparison with an undefined
   side carries `absolute_change: null` and `percentage_change: null`, and nothing else.
3. **Wire.** The `CampaignPortfolioArtifact` payload's metric `value` and comparison
   `absolute_change` become nullable at artifact version 1. Retained bodies evolve through the
   ADR-070 render format, campaign 5 → 6, so a format-6 body never shares a revision with a
   format-5 body. A format-5 body carries no null and decodes unchanged.
4. **Order.** Sorting by a ratio puts an undefined value after every number in both
   directions.
5. **Model inputs.** An undefined ratio is never a finding operand, so it never reaches the
   model as 0. Divergence considers only campaigns whose ROAS is defined in both windows.
6. **Display.** The browser renders `—` with the accessible name "Not applicable: no spend in
   this window" (or "no impressions" for CTR), and renders no change chip. It refuses a null on
   any non-ratio value.

## Consequences

- Pause and launch rows show `—` for ROAS and CTR on zero-delivery windows, where they showed
  `0.00`. Spend, purchases and impressions still show a measured 0.
- **Deploy window.** The browser before this change refuses a payload with a null ratio and
  shows the page as unavailable, never as 0. The API deploys first and the frontend right
  after (plan D3), so the window lasts one frontend build; a format-6 body is only written once
  the publisher deploys.
- **Campaign detail is unchanged.** The detail summary takes exactly one row per slot. A launch
  campaign whose 14D prior has no conversion rows has no baked detail (the renderer refuses the
  exact-row answer, a closed outcome). The interactive detail path reads the same empty covered
  answer as `EMPTY_COVERED` and serves the ADR-072 insufficient-evidence state.
- Insight revisions over a portfolio that contains a null ratio change once, because the
  measured payload changes.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| Remove the pause and launch events | Loses the zero-delivery states FR-3.2 requires, and keeps the false 0 for any real paused campaign. |
| Decide ratio-ness by metric name | Drifts the moment a ratio is added or renamed; the metric dictionary already declares it. |
| Bump the artifact version | Would refuse every retained body until a rebake (the ADR-084 reasoning). |
| Serve 0 with a qualifier | Still a false number on the tile and in the model's inputs. |

## Review trigger

Revisit when the server-declared change semantics of owner decision #4 land (direction and a
"not meaningful" flag), which may subsume the null change fields, or when another surface
serves a ratio from a grouped answer that can omit rows.
