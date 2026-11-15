# Retail Health — operational design

Shared publisher mechanics and failure semantics are owned by the
[preset artifact publisher](../preset-artifact-publisher.md). Completion clocks, cohort obligations,
and objective evaluation are owned by [Data-Flow Completion and Service Levels](../../concepts/data-flow-completion-and-service-levels.md).
The page applies the authorization and data-protection boundaries from the
[Campaign operational design](../campaign-portfolio/operational-design.md) without copying them.

## 1. Latency and capacity

| Path | Budget | What meets it |
|---|---|---|
| `POST /api/presentation/retail-health` | 1 s | Baked-artifact serve path under ADR-070: bounded pointer-roster read and one artifact fetch; no request-time preset measurement. |
| `POST /api/presentation/retail-health` with `product_ads: true` (Products and ads) | 1 s | The same path plus the Products and ads part: one index-shard read, then the part and its custody, all baked; no per-row request ([Products and ads](../products-and-ads/README.md) §2). |

- **Current bake envelope:** each declared `1d`, `7d`, or `14d` variant requires its equal prior,
  executes one grouped 35-plan measurement batch, then reads product hierarchy and reported-total
  evidence before reducing the artifact. `30d` remains withdrawn by ADR-070's snapshot bound.
- The publisher's five-minute Lambda ceiling is an execution limit, not this page's completion target.
  The accepted target and scale-tier population belong to the future objective registry.
- Product hierarchy, reported totals, distributor-view rules, and reconciliation are full artifact
  dependencies even though `pages.yaml` currently declares only the visual metric cohort. The page
  cannot claim cohort readiness until the generated dependency contract includes them.

## 2. Failure containment specific to this page

| Failure | User-visible result |
|---|---|
| Source cohort incomplete | Current: the baker returns a typed unavailable terminal and the invocation is spent; the last-good pointer continues serving with its loaded-as-of stamp. Target (ADR-075): the generation remains open and no bake is scheduled. |
| Reconciliation mismatch | A dated discrepancy state-note carrying both figures; the page still renders every independently answerable element. The review must decide whether this is successful-with-warning or blocks the freshness objective. |
| Reported-total or hierarchy publication missing | New artifact is unavailable; existing by-product or last-good evidence remains qualified rather than treated as fresh completion. |
| A shown product has no identity effective by the window end (first observed later, as after a backfill) | The product keeps every figure, its rank and its share of the totals, and is labelled by its display ASIN (title and parent both the ASIN), as New customers' gateway does. Its title appears in the first window whose end is on or after its first observation. Owner decision B-2, 2026-11-14. |
| After the render-format-13 deploy, before the page's next bake (the CURRENT pointer still names a format-12 body) | Retail health, Products and ads and Overview serve that preset's designed window absence ("not yet published", the preset marked unavailable); a tile focus or deep dive over it is the domain refusal `ADLY-PRESENTATION-0002`, never the `ADLY-PAGE_CONTEXT-0004` 500. The bake that follows the deploy publishes format 13 and replaces it; no reader of the format-12 body remains. Any other unreadable body stays the malformed-artifact refusal. |
| Sourcing-view request context | Ordered figures render the designed withheld state, never zero. |
| Prompted insight unavailable | Deterministic page renders; optional AI objective records unavailable separately. |

Identity day boundary (L-2): an identity is effective in a window when its `valid_from` UTC day is
on or before the window end and its `valid_to` UTC day, if any, is after it
(`CanonicalRetailProductHierarchy.hierarchy`). Window ends are Vendor Pacific days, and a UTC
day is never earlier than the Pacific day of the same instant, so both sides can be one day late:

- A product first observed late on the window-end Pacific day (after 16:00 PST or 17:00 PDT) shows its ASIN for that window.
- A title replaced late on the window-end Pacific day (after 16:00 PST or 17:00 PDT) still shows the replaced title for that window.

Each case changes one window's label only, never a figure.

## 3. Observability and alarms

Current publisher telemetry records bake source/persist timing and queue execution, but the publisher
stack has no implemented queue-age, DLQ, bake-duration, or publication-lag alarms. Required page-specific
signals are cohort wait reason, reconciliation mismatch, pointer lag, last-good continuity, and
objective-versioned freshness. These remain acceptance requirements, not current controls.

## 4. Cost envelope

Deterministic bake cost is one grouped measurement batch plus hierarchy/control reads and one immutable
body/pointer attempt per page/variant execution. Global enqueue, retries, no-op/superseded attempts, and
incomplete cohorts add waste and must remain separate usage categories.

Retail Health currently performs no publication-time model generation: prompted slots in the baked body
are typed unavailable placeholders, while movers are deterministic. Parent metric-focus may generate one
revision-keyed prompted slot at request time. Any future Retail precompute path is an explicit optional-AI
objective and must reuse the deterministic artifact rather than remeasure the cohort.
