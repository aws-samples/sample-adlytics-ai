# Platform operations

| | |
|---|---|
| **Level** | Delivery page package (proposal) |
| **Status** | Proposal draft 3 (2026-09-24) — not yet accepted; on acceptance the package gains its full design docs (high-level, operational, low-level) and joins the design-manifest roster with a roster-revision bump. No implementation is authorized by this document. |
| **Owns** | The operator-facing platform-health surface: page freshness, pipeline, data-movement status, job runs, job speed, and the contracts roster, projected read-only from the ADR-073 status control plane |
| **Parent** | [Decision Workspace UI](../README.md) |

Includes the [mockup](mockup.html) and the data-support verification below. Draft 3
incorporates the outcomes of the 2026-09-24 adversarial review: a **page
freshness** strip answers the customer's central question first; the hero is
**triage-first** (a named attention list, calm when healthy) instead of a raw aggregate
enum; a **pipeline strip** replaces the duplicate jobs spotlight; declared-but-silent
surfaces collapse to one line per family; the runs list adopts the **hybrid refusal
view** (by-design completions visible as a summarized success-variant line, never styled
as errors); the drill-in becomes a dialog; the mockup now renders adverse states
(a blocked surface, a failed run, an overdue feed); and every count carries its
denominator and window.

## Intent (2026-09-24)

Platform-level details, proactively surfaced: whether the data behind each workspace
page is current and expected feeds are properly landing; which data contracts are
active; the status and health of ingestion and AI insight jobs; visibility into
individual job runs (what is running and what has run); key latency metrics that tie
back to business outcomes (agentic latency, ingestion speed); "surface the health of
the platform proactively to the customer."

## Stances carried from the accepted decisions

- **One status authority, two projections.** ADR-073's projection is the single source;
  this portal is the operator projection. Customer-facing proactive cues (a banner on a
  serving page when its upstream is degraded) are a second projection of the same
  authority — a later slice, never a second health computation.
- **Health is declared, never browser-inferred.** The page renders server-owned
  `state`, `reading`, and `next_action` verbatim (ADR-073 §12). No client mapping of
  reason codes, no client thresholds, no client aggregation, no client-derived
  durations — and no client judgment of which refusals are routine: the by-design
  classification is a served property of the reading. One presentation carve-out:
  rendering a served absolute timestamp relatively ("3 min ago") is display formatting,
  permitted when the absolute value is served on the same record and surfaced with the
  element — inline for headline claims (the pulse observation, the attention items, the
  freshness strip), or through the element's drill-in for table cells. A duration
  between two events is a measurement and is never client-computed.
- **Honest coverage.** The registry is deliberately incomplete during observation-only
  rollout; the page must render `overall_coverage` and the registry composition
  prominently and may never claim complete coverage (ADR-073 Consequences).
- **Content-free by construction.** The API already excludes customer rows, prompts,
  AWS identifiers, and provider diagnostics; the portal introduces no new data class.
- **Entitlement.** `operations-status-read` (dedicated Cognito group), sitting beside
  Prompt Administration under ADMIN · OPERATORS ONLY. Not implied by other admin roles.

## Composition (see mockup)

1. **Platform pulse, triage-first** — the hero answers "does anything need my
   attention", with a named attention list (each item linking to its section) when
   something does and one calm line when nothing does. Coverage and last-observation
   pills (relative + absolute time) beside it; no raw aggregate enum, no filler tiles.
2. **Page freshness** — one row per workspace page: serving-data-through date, latest
   delivery landed, published-elapsed, and a feed-overdue treatment when an expected
   feed has not arrived. The customer-outcome anchor: it says whether expected feeds
   are properly landing and how quickly a landed delivery became servable. *Requires
   the currency read (see gaps).*
3. **Pipeline** — delivery → ingestion → publication → bake → insights as connected
   stages, each stage its registry family colored by its worst member, each opening its
   family in the table. Stages are dependent, so one glance answers "where is my data
   stuck". Replaces the former jobs spotlight (which duplicated the table's facts).
4. **Data movement by owner family** — the registered surfaces grouped by their
   registry owner (served by the definitions catalog): state pill, current-flow-state
   counts, completed-with-denominator over a stated window, cadence-evaluated freshness
   ("expected hourly · observed 4 min ago", the absolute observation time served on the
   record and surfaced through the row's runs view), plain-words reading, and next
   action.
   Declared-but-silent surfaces collapse to one honest expandable line per family.
   *Counts and freshness require the summary semantics fix (see gaps).*
5. **Runs** — list-detail with the table: selecting a
   surface shows its recent runs, newest first — run identity, started (relative +
   absolute), state, outcome, duration — with the stated window, shown-of-total, and
   retention. A run is one logical flow (retries add executions, never rows). **Hybrid
   refusal view:** runs that did or attempted real work render as rows; by-design
   completions stay visible as one summarized, success-styled line with an expand —
   a distinct kind of success, never an error, and never hidden. Which outcomes are
   by-design is a served classification (see gaps).
6. **Run drill-in** — opens as a dialog per the shared command-center dialog pattern
   (Escape returns focus to the opening row): snapshot (state, stage, outcome,
   continuity, reading, next action) and the event timeline, all served fields.
7. **Job speed, tied to outcomes** — designed absences until their serving arrives:
   ingestion→servable durations (measurement serving), SLO attainment (ADR-074
   activation), agentic latency (surface registration).
8. **Active data contracts** — the governed source roster with feeds-which-page, last
   delivered, expected cadence, and landing status; the same landing facts as the
   freshness strip at contract grain. *Requires the roster projection (see gaps).*

## Data-support verification [verified 2026-09-24 against the live dev API and handler]

Method: authenticated reads of `/api/operations/data-flows`, `/data-flow-definitions`,
one flow snapshot, and its events, exactly as `validation/probe_operational_status.py`
performs them (raw responses retained in ignored evidence,
`validation/evidence/operational-status-2026-09-24.json`), plus a source read of the
deployed summary handler (`src/adapters/inbound/operational_status/handler.py`).

| Mockup element | Serving field(s) | Verdict |
|---|---|---|
| Pulse: coverage + registry composition | summary `overall_coverage`, `registry` | Served today |
| Pulse: last observation (relative + absolute) | summary `observed_at` (`reconciled_at` is currently served as the same value — one fact, rendered once) | Served today |
| Pulse: attention list | — | **Gap (summary semantics fix)**: a truthful attention list needs per-surface current-flow-state aggregation. The deployed V1 summary aggregates the **last 100 lifecycle events** per surface: `state` is the most frequent recent event state (a surface with one blocked flow among many completions renders `completed`), counts are event frequencies not current flows, and `freshness` is `current` iff the surface has any event among its last 100 retained — no recency bound at all, so a surface whose newest event is a month old still serves `current`; the registry's declared cadence is never evaluated [verified in the handler]. The projection holds per-flow snapshots, so this is a server aggregation change, not new authority |
| Page freshness strip | — | **Gap (currency read)**: data-through, latest landing, and published-elapsed are authority-backed facts (artifact pointers, publications, landings) the status API does not project; a serving extension at page grain |
| Pipeline stages | definitions catalog `owner` + the same per-surface states as the table | Served today for membership; stage state inherits the summary-semantics fix |
| Surface rows: state, counts, completed-with-denominator, cadence freshness | summary per-surface fields | **Gap (summary semantics fix)** — same defect as the attention list; the columns as designed cannot be honestly filled by the last-100-events aggregation |
| Surface rows: reading, next action | per-surface `reading`/`next_action` | Served today, but at summary grain the handler serves a generic reading and `reason_code: null` [verified]; concrete readings live at flow grain until the semantics fix |
| Runs list per surface | — | **Gap (first slice)**: cursor-bounded per-surface flow index route; the projection already retains the per-flow items it would page over |
| Run detail (snapshot + event timeline) | flow route + events route (all fields verified live) | Served today |
| By-design refusal classification | flow/summary `reading` + reason vocabulary | **Gap (readings extension)**: the served reading must classify a refusal as "by design — nothing to do" vs "needs attention"; the reason-code vocabulary exists, the classification does not. Browser judgment is forbidden by stance |
| Run duration / job speed | flow `observation_samples` / `objective_samples`; `baseline_windows[]` | **Gap (measurement serving)**: sample arrays empty on the sampled live flow; baseline windows carry bookkeeping, not values. ADR-074 permits descriptive measurements in observation mode |
| SLO attainment tiles | per-surface `objective_windows[]` | Structurally served, **empty until ADR-074 objective activation** — designed absence |
| Agentic latency | — | **Gap**: no registered surface measures the chat runtime; needs a registry extension (new declared surface + status producer) |
| Vocabulary and self-teaching | `/data-flow-definitions` | Served today |
| Runbook links | — | **Gap**: declared by ADR-073 §12, no runbook field served; registry + definitions extension |
| Active data contracts panel | — | **Gap (roster projection)**: no API serves the governed contract roster; a build-time projection through the same protected API, now also carrying last-delivered / expected-cadence landing facts shared with the currency read |

## Open decisions

1. Approve draft 3's composition and look as the implementation target, including the
   triage-first hero, hybrid refusal treatment, and adverse-state designs.
2. Disposition the serving extensions, in the proposed priority order:
   **(a) summary semantics fix** — current-flow-state aggregation, policy-evaluated
   freshness, per-surface concrete readings (precedes everything the table and
   attention list claim); **(b) currency read** — page-grain freshness facts (the
   customer-outcome anchor); **(c) flow index route**
   with **(d)** the by-design refusal classification on readings; **(e) descriptive-
   measurement serving** for durations; **(f)** contracts roster projection,
   runbook links, agentic-latency surface registration. The first UI slice can ship
   against (a)–(c) with the rest as designed absences.
3. Confirm the customer-facing proactive cues stay a separate later slice on the same
   authority.
