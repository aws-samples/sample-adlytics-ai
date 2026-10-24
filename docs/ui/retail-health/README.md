# Retail health

| | |
|---|---|
| **Level** | Delivery page package |
| **Status** | Built; live-accepted on dev 2026-09-24 ([dated result](../../../validation/RESULTS.md#2026-09-24--command-center-revision-2026-09-234-live-acceptance-and-visual-conformance)). The UI refresh added its answer sentence and the Ads beside retail strip (2026-10-22). A customer deployment can't load its Vendor Central data yet ([README Known limits](../../../README.md#known-limits)). Revision history: [design manifest](../design-manifest.yaml). |
| **Design revision** | `2026-10-09.2` |
| **Owns** | The 1P vendor retail surface: shipped/ordered outcomes, returns, traffic, reconciliation, and their insight slots |
| **Parent** | [Decision Workspace UI](../README.md) |

> **Delivery decision (2026-09-15).** Retail health is the workspace's 1P vendor surface,
> built entirely on governed data the lake already curates: shipped and ordered sales, COGS,
> customer returns, glance views, the vendor's separately delivered control totals, and the
> product hierarchy dimension. It is the **second consumer of the
> [preset artifact publisher](../preset-artifact-publisher.md)**, which generalizes per
> ADR-070's review trigger. It renders retail actuals as context beside advertising outcomes
> and never joins them causally.

## 1. Purpose and scope

Retail health lets a practitioner triage 1P retail movement — what Amazon retail actually
shipped, ordered, returned, and how much traffic products drew — with prior-window deltas,
purpose-typed insight slots, a daily trend, and a hierarchy-labelled product table. Its
distinctive evidence strength is **reconciliation**: the lake carries the vendor's reported
control totals separately from the by-product rows, so the page proves the rows sum to the
vendor's own total, and renders a dated discrepancy when they do not.

Two source truths shape the page and are rendered, never hidden:

- **Distributor-view coherence.** Traffic has no distributor-view split, so a
  units-per-glance-view conversion rate is coherent only under the Manufacturing view; and the
  source withholds ordered figures under the Sourcing view. Both render as designed states
  (a caveat line and a typed-absent tile), never as zeros
  (see [the vendor-central source record](../../sources/vendor-central.md)).
- **Basis boundary.** Retail actuals and ad-attributed outcomes are different measurement
  bases. This page carries a visible boundary statement and no campaign column, ROAS, or
  attribution join.

## 2. Documents

- [Mockup](mockup.html) — the mandated implementation target, in the shared command-center
  visual language (fictional values).
- [High-level design](high-level-design.md) — API role, composition, and serving topology.
- [Low-level design](low-level-design.md) — exact route contract, artifact shape, window
  policy, insight roster, and acceptance.
- [Operational design](operational-design.md) — budgets, bake envelope, failure containment,
  observability, and cost.

## 3. Implemented delivery boundary
The repository declares the `RetailHealthArtifact.v2` codec (2026-09-17: derived
returns-and-margin slots and per-product return-rate and Amazon-margin columns), its
preset-only V2 page binding, strict browser decoder, and parent-only frontend route. The
retained Vendor materializer is registered with the publisher: every declared preset bakes
from retained truth, with the four derived slots reduced from measured operands at render.
The `/api/presentation/retail-metric-focus` route serves `MetricFocus.v3` from the same baked
body; every tile opens the shared focus dialog. Generated Vendor fixtures use the governed
reporting-lens defaults (`MANUFACTURING`, `RETAIL`, `DAY`), land through the fixture bundle,
and serve the live development pages. Deployed acceptance is recorded in
`validation/RESULTS.md` (2026-09-16 retail pages; 2026-09-17 narrative sections, derivations,
and metric focus).
