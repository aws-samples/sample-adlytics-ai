# Generation materialization
| | |
|---|---|
| **Type** | Shared UI publisher low-level design |
| **Status** | Accepted 2026-09-19; mandated by [ADR-075](../decisions/adr-075-generate-page-artifacts-once-per-dependency-cohort.md) §4–5, not yet implemented. ADR-075 Stages 3–4 implement only what this document accepts; Stage 2 is owned by [generation coordination](generation-coordination.md). |
| **Owns** | The immutable generation materialization record, its store and retention, the materializer's execution order and checkpoints, the consumer contracts (parent renderer, ordered segments, preset insight slots, operational measurement), the metric-dictionary preset-reuse declaration, and the bakeability budget split |
| **Parent** | [Preset artifact publisher — Target pipeline](preset-artifact-publisher.md#target-pipeline) |
[Generation coordination](generation-coordination.md) owns when a materialization may start and how
its outcome reaches the generation and the pointer. Each page LLD owns what its artifact contains.
[ADR-068](../decisions/adr-068-fill-declared-insight-slots-with-direct-structured-model-calls.md)
owns how a slot becomes a model call; this document owns what the slot reads.
## 1. Problem this design removes
The deployed Campaign renderer (`render_portfolio` in `campaign_artifact_renderer.py`) builds the
portfolio batch, inspects evidence, executes the batch, derives identities, and reduces — in one
function, with nothing persisted between execution and reduction. The Campaign Insight Precompute
worker (`RetainedPublicationPlanner.requests_for`) re-executes the portfolio batch plus per-campaign
detail and delivery batches for every candidate preset to warm model slots. Two measurement families
run for one preset, and every preset variant measures its own window even where three windows share
the same daily rows.
## 2. Record shape
```text
GenerationMaterialization.v1 = {
  version: 1,
  page: string, variant: string,
  generation_key: string,                     // GENERATION#… record_key (coordination §4.1)
  contract_version: "sha256:…",
  render_format: uint,
  revision: string,                           // PresetArtifactRevision from source_roster + render_format
  source_roster: [ { dataset, partition, publication_id, source_revision } ],   // sorted
  source_roster_digest: "sha256:…",
  windows: { target: { start, end }, prior: { start, end } | null, grain: "day" },
  population: {
    selections: [ identity… ],                // authorized cohort the page reduces over
    boundless_total: uint, truncated: bool,
    sort: { key, direction, regime: "complete_reorder" | "cohort_selection" }
  },
  slots: [ {
    slot_id: string, plan_id: string, metric_id: string,
    window_ref: "target" | "prior", grain: string,
    derivation: "queried" | "derived_sum" | "derived_operands",
    derived_from: [ slot_id… ],               // empty for queried
    answer: ShapedAnswer                      // values, units, coverage, holes, suppression, basis, grain
  } ],
  findings: [ { finding_id, kind, subject, metric_id, window_ref, values, rank } ],
  custody_inputs: { body_inputs_digest, segment_ordering_digests: { ordering: digest } },
  measurement: {
    statements: uint, athena_seconds: number, queried_slots: uint, derived_slots: uint,
    plan_timings: [ { plan_id, seconds, statements } ]
  },
  produced_at: timestamp, execution_id: string, flow_ids: [string…]
}
```
The record is what a renderer needs and nothing it does not: `population`, `windows`, `slots`
(labels and shaped answers), `findings`, and identities. It carries no capabilities, no session
custody, no prompt text, and no model output. `ShapedAnswer` is the existing executed answer type
returned by `execute_presentation_batch`; the record serializes it without reinterpretation.
`findings` are deterministic decompositions the page LLD declares (movers, contribution ranks,
period-over-period deltas). They are computed from `slots` by pure domain functions and stored so
insight slots and operators read the same numbers the artifact shows.
## 3. Store and retention
| Property | Value |
|---|---|
| Location | Preset artifact bucket, `materializations/{page}/{variant}/{revision}.json`, beside `pages/{page}/{variant}/{revision}.json` |
| Immutability | Put-if-absent; a replay with different bytes for the same key is an invariant failure |
| Digest | `materialization_digest = sha256(body)`; recorded on the generation as `materialization_ref = {key, digest}` |
| Size bound | `MAX_MATERIALIZATION_BYTES = 4_000_000`; exceeding it is `materialization_budget_exceeded`, never truncation |
| Retention | Retained as long as the page body of the same revision. Today body objects under `pages/…` are never removed (`publisherArtifactLifecycle` expires noncurrent object versions only; the revision TTL applies to `REVISION#` pointer items, not bytes), so materializations are retained indefinitely with them. A future body removal policy applies to `materializations/…` in the same change; ADR-075's review trigger on the store's size bound is measured on this prefix |
| Access | Materializer: `s3:PutObject`/`GetObject` on `materializations/*`. Presentation (serve-time first-fill) and the insight-fill child: `s3:GetObject` on exact keys. Operators: get through the protected status routes, never listed. These grants are added to `PublisherStack` and the presentation function beside the existing `pages/*` grants |
One materialization exists per `(page, variant, revision)`. Two generations that derive the same
revision (a restatement that produced identical identities) share it: the second finds the key
present with an equal digest and records `materialization_reused`.
## 4. Materializer execution
One invocation receives one dispatch message (coordination §5) for one page with one or more frozen
generations. For each generation, after the conditional claim:
1. **Resolve** windows from the generation's recorded target and prior periods — never re-resolved
   from live coverage, so the materialization matches the generation that requested it.
2. **Reproduce the expected revision** from the generation's `revisions` roster and the page's
   `render_format`. A mismatch with `expected_revision` is `revision_mismatch`; the generation was frozen
   against a roster this code cannot reproduce. `pointer_stale` is reserved for step 9.
3. **Plan** the measurement batch for the widest claimed variant in the message, plus exact-window
   plans for every metric and variant that §6 does not allow to derive. A narrower variant's slot is
   derivable only when its target and prior windows are both contained in the widest variant's
   retrieved range; `resolve_temporal` may resolve different target ends per preset (a `14d` window
   needs 28 covered days, a `7d` window 14), and a variant whose windows fall outside the retrieved
   range is queried exactly. The population browse (`population.selections`) runs once per variant:
   the active cohort of a 14-day window is not the cohort of a 1-day window.
4. **Execute** the batch once through the existing bounded, position-preserving Athena thread pool.
5. **Checkpoint: superseded?** Re-read each claimed generation; a `superseded` one is `abandoned`.
6. **Derive** each remaining variant's slots per §6 and compute findings by the page's pure domain
   functions.
7. **Persist** each variant's materialization (§3); record `materialization_ref` on its generation.
8. **Render** the parent body, ordered segments, and custody manifest from the record alone (§5.1);
   persist bodies and custody; **checkpoint** superseded again.
9. **Advance** the pointer in one transaction with the generation terminal (coordination §4.2).
10. **Enqueue** the optional insight-fill child for the advanced revision (§5.3). Its failure never
    reopens the generation.
Steps 3–4 run once per message; steps 5–10 run per variant. A budget failure in step 4 fails every
generation in the message with `materialization_budget_exceeded`; a failure in 8–9 fails only that
variant with `render_budget_exceeded` or `pointer_stale`.
## 5. Consumer contracts
### 5.1 Parent renderer and ordered segments
The page renderer splits into `materialize(request) → GenerationMaterialization.v1` (adapter: plans,
executes, derives, persists) and `render(record) → artifact bodies` (pure domain: today's
`reduce_portfolio` and the Retail reducer, taking `record.population`, `record.slots`,
`record.windows`, and `record.findings` in place of the live `executed.slots`). The ordered-segment
renderer under [ADR-071](../decisions/adr-071-reach-the-full-campaign-estate-through-baked-ordered-segments.md)
reads `record.population` and the row slots for every segment ordering; it executes no query.
`render` must be deterministic: identical record bytes yield identical body bytes, which is what
lets `custody_inputs` be attested before the pointer moves.
### 5.2 Operational measurement
`record.measurement` is the ADR-074 usage contribution for the materialization surface: statements,
Athena seconds, queried versus derived slots, and per-plan timings. The materializer emits it as the
`preset_bake` usage sample keyed by generation, so the before/after comparison ADR-075 §9 requires
reads one field, not a log.
### 5.3 Preset insight slots
> **Amendment, 2026-09-22 — interim insight-fill child ahead of Stage 3.** The child described
> below ships ahead of the materializer: the deployed preset baker enqueues it after each Campaign
> and Retail Health pointer advance (`preset_artifact_publisher/insight_fill.py`), deriving the
> advanced revision's declared requests (`portfolio_insight_request_derivation` for `campaigns`,
> `retail_insight_request_derivation` for `retail-health`) from the just-baked parent artifact —
> the same bytes the serve path derives from, so the warmed revision-keyed cache entries are
> byte-identical to a serve-time first-fill — and admitting them to the existing durable
> continuation queue. It executes no Athena, never gates the pointer, and swallows every failure
> (ADR-074). When Stage 3 lands, its findings source moves to the persisted
> `GenerationMaterialization.v1` record as this section mandates, and the Insight Precompute
> stream consumer is retired in that same change; until then the precompute worker remains
> deployed and converges with the child at the conditional cache write.

Preset-artifact insight slots (Campaign portfolio narrative, Retail movers narrative, and any slot a
page LLD declares as preset-served) take their inputs from `record.findings` and `record.slots` by
revision. Each slot's deterministic Gate-1 predicate (required evidence inputs, finding kinds,
per-basis materiality, silence condition) is owned by its contract in `contracts/insights/`; the
committed [generated matrix](generated/insight-deterministic-matrix.md) projects all of them for
review and is drift-gated by `make check-insight-matrix`. The request derivation functions (`portfolio_insight_request_derivation`,
`detail_insight_request_derivation` today) are re-pointed to accept a `GenerationMaterialization.v1`
in place of an artifact plus live contracts; they remain pure. The insight-fill child:
- is enqueued after the pointer advances, keyed by `(page, variant, revision)`;
- reads the materialization by exact key and digest, executes no Athena, and writes the revision-keyed
  insight cache exactly as serve-time first-fill does today;
- is optional under ADR-074: its terminal never gates the pointer, and its absence at serve time falls
  back to the existing first-fill path, which now also reads the materialization rather than remeasuring.
Campaign Insight Precompute as a publications-stream consumer is retired in the same change that ships
this child. Request-time tiers — live detail tail and metric-focus first generation — are outside this
record and keep their own request-path budgets.
## 6. Preset reuse declaration
The metric dictionary gains one optional field per metric, `preset_reuse`, validated by
`src/adapters/outbound/metric_dictionary.py` (added to `_METRIC_KEYS`):
| Value | Meaning | Validation |
|---|---|---|
| `sum_daily` | Narrower presets may be derived by summing retrieved daily-grain rows | Requires `type: simple`, `aggregation: sum`, `time_class: daily_additive`, `time_grain: day`, and `non_additive` absent or false (so `exact_distinct` and non-additive metrics such as `reach_users` and `returns_value_estimate_retail` cannot declare it) |
| `recompute_from_operands` | Derived per preset by applying the ratio or product to operand values that are themselves derived where the operand's declaration allows it and queried for that exact preset otherwise | Requires `type: ratio` (`numerator`/`denominator`) or `type: product` (`operands`); every operand metric carries an explicit `preset_reuse` of any value. `cost_per_reach` (numerator `sum_daily`, denominator `reach_users` `exact_window`) is therefore admissible: spend is summed, reach is queried, the ratio is recomputed |
| `exact_window` | Queried per exact window; never derived | Valid for `simple` and `ratio`; the only admissible value (explicit or by absence) for `aggregation: exact_distinct`. Not admissible for `type: product`: the measurement planner refuses a live product query (`NOT_YET_COMPUTABLE`), so a product must declare `recompute_from_operands` and the validator refuses `exact_window` or absence on it |
| absent | Treated as `exact_window` | — |
Further rules the materializer enforces at derivation time, independent of the declaration:
1. A derived cell is produced only when no contributing daily cell is suppressed or a hole; otherwise
   the metric is queried for that exact preset and the slot records `derivation: queried`.
2. Datasets in the `control_total` role of the dependency contract are always `exact_window`; a
   control-total reconciliation is never derived.
3. `time_class` and `aggregation` are never consulted as a reuse signal. A ratio's `daily_additive`
   describes its operands, not the ratio.
4. A derived variant's `revision` and `source_roster` are built from that variant's own required
   partitions (the wide retrieval roster is not the variant's identity).
5. Attempting to derive a metric whose effective `preset_reuse` is `exact_window` is an invariant
   failure, not a fallback.
Coverage of the declaration: every metric a page's dependency contract names in a `metric_source` or
`ratio_operand` role must carry an explicit `preset_reuse` value; `make check-page-dependencies` fails
otherwise. Metrics no artifact-served page uses may leave it absent.
## 7. Bakeability budget split
`make check-bakeability` currently proves `plans × PER_QUERY_SECONDS / concurrency ≤
BAKER_TIMEOUT_SECONDS` and estimates body bytes against `MAX_PRESET_ARTIFACT_BYTES`. It gains, per
page:
| Budget | Bound | Estimate |
|---|---|---|
| Materialization (per message) | `MATERIALIZATION_BUDGET_SECONDS` (initial 240) | `ceil((widest-variant plans + Σ exact-window plans of other variants) / DEFAULT_QUERY_BATCH_CONCURRENCY) × PER_QUERY_SECONDS`, the gate's existing rounding |
| Render and persist (per variant) | `RENDER_BUDGET_SECONDS` (initial 45) | Bodies + segments + custody + materialization puts at the declared body count (`campaign_bake_object_count` is per variant), plus one transaction; the message total is `variants × RENDER_BUDGET_SECONDS` |
| Materialization size (per variant) | `MAX_MATERIALIZATION_BYTES` | Slots × bounded population rows × encoded answer estimate |
| Total | `MATERIALIZER_TIMEOUT_SECONDS` (initial 420) ≥ materialization + 3 × render + 30 s claim/checkpoint margin, leaving 15 s slack at the initial constants | — |

The initial constants are expected **not** to pass on first run: with three variants per message and
every metric defaulting to `exact_window`, `retail-health` is up to 105 plans (14 waves, 420 s) and
`campaigns` up to 78 (300 s) against 240 s. The gate passing is the evidence that `sum_daily` and
`recompute_from_operands` have been declared widely enough; control totals stay `exact_window` by rule
2. The constants are then tuned against the Stage 0 measured per-plan seconds, never against the
Lambda ceiling.
The gate also reports, per page, how many plans the reuse declaration removes, so a declaration
change that silently loses derivation shows up as a budget change in review.
## 8. Decisions recorded here
| Decision | Rationale | Rejected |
|---|---|---|
| One record per variant, derived variants included | Revision-bound consumers read one key; variant identity stays its own roster | One record per page holding every variant (a variant's revision would depend on others' rosters) |
| Record beside the body under the same revision closure | One removal policy; readable wherever the body is | Separate bucket or DynamoDB (size, second policy) |
| `preset_reuse` is opt-in per metric with `exact_window` default | Refusal is the safe default; ADR-075 §5 requires explicit declaration | Inferring from `time_class`/`aggregation` |
| Findings persisted, not recomputed by consumers | Insight slots and operators quote the numbers the artifact shows | Recomputing findings from slots at fill time (a second implementation of the same maths) |
| Insight fill as an optional post-pointer child | AI never blocks deterministic readiness (ADR-068, ADR-074) | Filling before the pointer advances |
| Render is pure over the record | Determinism makes custody attestable and replay testable | Letting render re-query for "fresh" values |
## 9. Acceptance for Stages 3–4
1. For one Campaign generation, Athena statements equal the widest variant's plan count plus declared
   exact-window plans; no second measurement family runs.
2. `render(record)` on a persisted record reproduces the deployed body bytes for the same revision.
3. A `sum_daily` metric's 7d value equals the sum of its 7 daily cells; a `recompute_from_operands`
   ratio equals numerator over denominator of the derived operands; an `exact_distinct` metric is
   queried per variant; a ratio with one `exact_window` operand (`cost_per_reach`) sums the numerator,
   queries the denominator, and recomputes; a `product` metric declaring `recompute_from_operands`
   recomputes from its operands.
4. A suppressed daily cell forces that metric's affected preset to `derivation: queried`.
5. The insight-fill child reads the materialization, executes zero Athena statements, and populates
   the revision-keyed cache; a failed child leaves the pointer advanced.
6. `make check-bakeability` reports the split budgets and fails when a page's materialization estimate
   exceeds its bound.
7. Campaign Insight Precompute's stream mapping is removed in the same deployment that enables the
   insight-fill child.
## 10. Risks and open questions
- Retail movers today are derived from a bounded row set; declaring them as `findings` requires the
  Retail LLD to name their exact derivation, which is a page-owned change to the
  [Retail Health low-level design](retail-health/low-level-design.md). Stage 3 may complete for `campaigns` before `retail-health`; `retail-health` stays on the
  Stage 2 transitional consumer until its findings derivation is named, and the stage is not accepted
  until both pages consume the record.
- The status events and usage samples this document emits are contracts for the ADR-073 control plane;
  until its transport exists they are structured lifecycle logs.
- `MAX_MATERIALIZATION_BYTES` at 4 MB assumes the bounded population; the 1,000-campaign scale tier
  must be re-estimated by the gate before that tier is accepted.
- `PER_QUERY_SECONDS = 30` is a planning constant, not a measured figure; the Stage 0 baseline
  supplies the measured per-plan seconds and the constant is revisited against them.
