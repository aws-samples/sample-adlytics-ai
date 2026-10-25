# Generation coordination
| | |
|---|---|
| **Type** | Shared UI publisher low-level design |
| **Status** | Accepted 2026-09-19; mandated by [ADR-075](../decisions/adr-075-generate-page-artifacts-once-per-dependency-cohort.md) §1–3, §7–8, not yet implemented. ADR-075 Stage 2 implements only what this document accepts, including its transitional consumer (§5.1). Hard preconditions: the Stage 0 observation baseline exists for `preset_enqueue` and `preset_bake`, and Stage 1 (servable projection pointer, trigger rule, migration) is deployed — §3 is written against servable semantics that do not exist before it. |
| **Owns** | The generated page dependency contract, the dependency router, the generation record and its conditional transitions, the no-change decision, dispatch transport to the materializer, and the pointer-transaction identity fields |
| **Parent** | [Preset artifact publisher — Target pipeline](preset-artifact-publisher.md#target-pipeline) |
Generation *identity and lifecycle semantics* — what a generation is, when it is open, frozen,
superseded, or predecessor-linked — are owned by
[Data-Flow Completion and Service Levels §Clock contract](../concepts/data-flow-completion-and-service-levels.md#clock-contract).
This document owns how those semantics are recorded and enforced. What a materialization contains
and who reads it is owned by [Generation materialization](generation-materialization.md).
## 1. Problem this design removes
The deployed enqueue Lambda (`src/adapters/jobs/preset_artifact_publisher/transport.py`,
`load_declared_bake_intents`) sends every declared page/variant — seven pages, most with four presets —
to the bake queue for every canonical `CURRENT` transition. The bake message is `(page, variant)`
only, so transport deduplication cannot recognize a publication cohort. Of the declared pages only
`campaigns` and `retail-health` (`custom.enabled: false`, presets `1d`/`7d`/`14d`, default `7d`)
have materializers; every other intent consumes an invocation and terminates `unavailable`. A real
bake resolves windows, browses the population, executes the full measurement batch, derives the
revision, and only then learns whether the pointer would move.
Two coverage gaps make a dependency-driven router impossible today: Campaign coverage is derived from
the legacy `campaign-attribution-detail` metric list (`insight_precompute/planning.py`), and Retail
coverage is derived from V2 visual metrics only while the renderer also measures eight totals, the
`dim_product` hierarchy, and `fct_retail_sales_reported_total`. Neither page has a single declared,
generated roster of everything its artifact depends on.
## 2. Generated page dependency contract
### 2.1 Sources and generator
The contract is generated, never hand-maintained, from three owners:
| Input | Owner | Contributes |
|---|---|---|
| `presentation/pages/v2/pages.yaml` | Page declarations | Which pages are artifact-served (`custom.enabled: false` and a registered materializer), their presets, comparison policy, artifact types and versions |
| `contracts/shared/metric-dictionary.yaml` | Metric dictionary | For every visual metric: source fact, ratio operands, aggregation, time class, and the preset-reuse declaration defined in [Generation materialization §6](generation-materialization.md#6-preset-reuse-declaration) |
| `presentation/pages/v2/dependencies/<page>.yaml` (new, one per artifact-served page) | Page LLD owner | Non-visual dependencies: identity dimensions, hierarchies, control totals, custody requirements, render-format version |
`make generate-page-dependencies` writes `contracts/generated/page-dependencies.json`;
`make check-page-dependencies` fails when the committed projection is stale, when a declared
artifact-served page lacks a dependencies file, or when a dependencies file names a dataset the V2
table catalog does not define. The [preset-reuse coverage rule](generation-materialization.md#6-preset-reuse-declaration)
activates in this gate at Stage 3, when the dictionary field and its validator exist; at Stage 2 the
gate checks structure and dataset validity only. Both targets join `make check` beside the existing
generated-artifact gates.
### 2.2 Shape
```text
PageDependencyContract.v1 = {
  contract_version: "sha256:…",              // digest of the generated body; the roster version
  pages: [ {
    page: "campaigns",
    render_format: 4,
    materializer: "campaign_artifact_renderer",
    variants: [ { variant: "7d", comparison: "required_equal_prior" }, … ],
    dependencies: [ {
      dataset: "fct_attributed_conversion",  // V2 catalog dataset id
      roles: ["metric_source", "ratio_operand"],
      required: true,
      window: "target_and_prior" | "target" | "as_of_target_end" | "whole",
      metrics: ["attributed_revenue_14d", "roas_attributed_14d", …],  // dictionary ids; empty for non-metric roles
      coverage: { sources: [ { source_system, source_dataset } ], scopes: [ … ], selectors: { … } }
    }, … ]
  }, … ]
}
```
Roles are the closed set `metric_source`, `ratio_operand`, `identity_dimension`, `hierarchy`,
`control_total`, `custody`. `window` states which resolved periods the dataset must cover for the
generation to freeze; `whole` declares a dataset delivered whole and covered by existence (identity
dimensions and hierarchies). `required: false` is permitted only for roles the page LLD marks optional
(none today); an optional dependency never blocks freezing and never enters the expected revision.

**Roster membership rule.** A dataset is a required roster member exactly when its publications enter
the renderer's bound publication identities (the set evidence inspection binds, from which the
deployed revision derives). That includes withheld sibling tables when inspection binds them and
excludes browse or label reads that bind nothing. The rule is mechanical: the §2.3 conformance step
compares roster dataset sets against the page's real plans, and acceptance item 9 proves the revision
equality on live data — a wrong membership decision fails one of the two, never silently skews the
expected revision. Each dependency also carries the generator-emitted coverage binding: the
`(source_system, source_dataset)` pairs, publication scopes, and any per-variant selectors the page's
coverage requirements declare — the router's coverage input is this binding, not a legacy page list.
### 2.3 Conformance
A page's materializer may query only datasets its contract names. The bakeability gate
(`make check-bakeability`) gains a conformance step: the datasets referenced by each page's
measurement plans must be a subset of the page's dependency roster, and every `required` dependency
must be referenced by at least one plan or a declared custody/hierarchy input. This closes the gap the
completion concept records: a page can claim a complete cohort objective only once this check passes.
## 3. Dependency router
Replaces the enqueue Lambda on the same publications `CURRENT` stream mapping
(`ReportBatchItemFailures`; every failed record returns its `SequenceNumber`).
For each stream record:
1. **Recognize an applied transition.** The publications stream is `NEW_IMAGE`-only with no filter,
   so a record does not say which attribute changed. The router (and, once Stage 1 ships, the deployed
   enqueue and insight-precompute consumers) processes a record only when `record_key == "CURRENT"` and
   `NEW_IMAGE.projected_publication_id == NEW_IMAGE.publication_id` — the servable advance that made the
   current authority queryable ([write discipline §2.3](../capabilities/load-path/write-discipline.md#23-envelope-attestation-and-readers)).
   Every other record (authority-only advances, servable advances to an already superseded
   publication, `"NONE"`) is acknowledged as `authority_only`. Before the Stage 1 servable-pointer
   migration completes, a record with **no** `projected_publication_id` attribute is a legacy authority
   advance and is processed as an applied transition (the deployed path projects inline); after the
   migration the attribute is always present and an absent one is a decode failure. Records produced by
   the migration stamp itself are processed once and recorded under `migration_replay`, and the ADR-074
   comparison windows exclude the migration instant. This is the trigger rule; no other document
   restates it. A servable advance that lands after authority moved on is therefore not routed; the
   newer publication's own advance follows.
2. **Re-attest.** Strongly read the servable publication through
   `DynamoCurrentPublications.current_for_trigger`; a record whose publication is no longer servable is
   acknowledged with usage category `superseded_transition`. A publication id already present in the
   target cohort's `revisions[dataset]` for that partition is `duplicate_transition` and never opens,
   joins, or supersedes anything — but before acknowledging, the router re-dispatches the cohort's
   generation if it is `frozen` with `dispatched_at` absent, closing the crash window between freeze
   and send: the retried stream batch re-enters here and completes the dispatch.
3. **Map.** Look up the publication's dataset in `page-dependencies.json`. No page names it → acknowledge
   with category `unrouted`. This is where global fan-out is retired: unregistered pages are absent from
   the contract and receive nothing.
4. **Resolve target periods.** For each affected `(page, variant)`, run the page's window policy
   (`resolve_temporal`) against the **common coverage** of the page's required day-grained
   dependencies after the transition — the same `common_intervals` input the deployed bakers pass
   (`catalog.py`, `source_coverage.bounds.intervals`). The coverage input is each dependency's
   generator-emitted coverage binding (§2.2). Partitions whose servable pointer is `"NONE"` are
   omitted from coverage. The resolution yields the target period and,
   where the comparison policy requires it, the prior period. Consequences: a leading dataset's new
   day does not move the target period until every day-grained dependency covers it, so its
   transition is `out_of_window` until then; when the last day-grained dependency arrives, the period
   moves and the new generation is complete for every day-grained dependency by construction. The
   publication's partition bucket must intersect a window the dependency's `window` field names;
   otherwise the transition is irrelevant to that variant (category `out_of_window`, also for a
   restatement older than the widest preset plus its prior). The artifact-bake policy is
   `MAX_PRESET_SNAPSHOT_PARTITIONS` per dataset, held equal to Publication State's exact-read bound
   (1,000, the chunked two-pass snapshot, ADR-070 2026-10-25); the bakeability gate refuses a
   declared preset whose target plus prior day-partitions per pack exceed it (184 for a compared
   quarter, 60 for `30d`). A page declaration still chooses its presets; a wider bound never adds one.
5. **Open or join** the generation for `(page, variant, target period, contract_version)` (§4.2). If
   the page/variant's `OPEN#` item names an open generation for a *different* cohort key, that
   generation is first closed `abandoned` with `terminal_reason = period_moved` (§4.2): its target
   period can no longer be resolved, so it can never freeze.
6. **Evaluate completeness** (§4.3). If every required dependency is satisfied, **freeze**, derive the
   **expected revision**, and either terminate `completed_no_change` or **dispatch** (§5).
The router coalesces the records of one stream batch before acting: records resolving to the same
cohort fold into one open/join/freeze evaluation and at most one dispatch per cohort per invocation,
so a multi-partition delivery costs one generation cycle per batch rather than one supersession per
record; §5's deduplication id ("generations frozen by the same router execution") assumes exactly
this. Within one record's evaluation, the per-variant coverage reads are independent I/O and run
through the configured bounded fan-out, memoized within the invocation. The router is deterministic
and holds no lease; every transition is a conditional write, and a conditional failure is re-read and
re-evaluated once before the record is reported as a batch failure.
Because day-grained completeness holds at open time, the `open` state persists only while a
whole-delivered dependency (an identity dimension, hierarchy, or control total) lags; for pages whose
non-day-grained dependencies are already current, open and freeze happen in one router execution.
## 4. Generation record
### 4.1 Key space
Generation items live in the existing preset artifact pointer table (`PublisherArtifactStoreStack`;
partition key `page_variant_key`, sort key `record_key`) so the pointer advance and the generation
terminal are one transaction. Serving reads exact `CURRENT` keys and is unaffected.
| `record_key` | Purpose | Fields |
|---|---|---|
| `OPEN#<contract_version>` | One per page/variant/roster version: which cohort key, if any, holds the open generation | `open_cohort_key` (nullable), `open_seq`, `updated_at` |
| `COHORT#<target_start>#<target_end>#<contract_version>` | Cohort head: one per page/variant/target period/roster version | `latest_seq`, `open_seq` (nullable), `updated_at` |
| `GENERATION#<target_start>#<target_end>#<contract_version>#<seq, 6 digits>` | One generation | `state`, `transition_execution_id` (last router execution that moved it), `started_at`, `frozen_at`, `dispatched_at`, `terminal_at`, `terminal_reason`, `prior_start`, `prior_end`, `required` (dataset → `window`), `revisions` (dataset → ordered list of `{partition, publication_id, source_revision}` covering the required windows), `flow_ids` (set), `causation_publication_id`, `causation_flow_id`, `predecessor_seq`, `superseded_by_seq`, `expected_revision`, `pointer_revision_at_freeze`, `materialization_ref`, `pointer_outcome`, `usage` (map of ADR-074 categories → counts), `claim_execution_id`, `claim_expires_at`, `expires_at` (epoch seconds, the table's existing TTL attribute) |
Generation `state` is the closed set `open`, `frozen`, `materializing`, `completed_advanced`,
`completed_no_change`, `failed`, `superseded`, `abandoned`. `revisions` is stored inline: at most
`2 × preset_days` partitions per dataset (28 for `14d`) times the page's dataset count, each entry
three short strings, well inside the item bound. The customer-freshness clock is
`started_at` → the pointer's `advanced_at`; `frozen_at` starts the cohort-processing clock.
### 4.2 Transitions

**Focused Module diagram: generation state machine — conditional transitions in the pointer table (module depth).**

![Focused Module diagram: generation lifecycle states and the writers that move them. A relevant CURRENT transition opens a generation or joins the open one. When every required dependency is covered the router freezes it and derives the expected revision. Equal to the current pointer ends in completed_no_change. Otherwise the generation is dispatched and the materializer moves it to materializing, then to completed_advanced in one transaction with the pointer, or to failed. A relevant transition after freezing supersedes a non-terminal generation, whose materializer abandons its work, and opens a successor.](diagrams/generation-coordination.svg)

> **Takeaway.** Every arrow is one conditional DynamoDB write keyed on the state it leaves; measurement is reachable only from `frozen` after the no-change gate says no.

"Materializer" below names the consumer of the dispatch queue: the transitional baker at Stage 2
(§5.1) and the generation materializer from Stage 3; `materialization_ref` is written only by the
latter.

| From → to | Writer | Condition and effect |
|---|---|---|
| ∅ → `open` | Router | Head `latest_seq = :seen` (create head if absent, `attribute_not_exists`); put generation `seq = :seen + 1` with `started_at = now`, `causation_*` from this transition, `flow_ids = {flow}`, `revisions[dataset]` set; head `latest_seq += 1`, `open_seq = seq`; `OPEN#` item `open_cohort_key = this cohort`, conditioned `open_cohort_key` absent or equal. One transaction. |
| `open` → `abandoned` (`period_moved`) | Router | The `OPEN#` item names a different cohort key than the one this transition resolves; condition `state = open AND seq = head.open_seq`; set `terminal_at`, `terminal_reason = period_moved`, `expires_at`; head `open_seq = null`; `OPEN#` `open_cohort_key = null`. Runs before the new cohort's ∅ → `open` in the same router execution. Owned semantic: [clock contract](../concepts/data-flow-completion-and-service-levels.md#clock-contract). |
| `open` → `open` (join) | Router | Generation `state = open`; add `flow_ids`, set or replace `revisions[dataset]` entries for the covered partitions. `started_at` untouched. |
| `open` → `frozen` | Router | Every required dependency covered (§4.3) and `state = open`; set `frozen_at`, `expected_revision`, `pointer_revision_at_freeze`; head `open_seq = null` conditioned `open_seq = seq`; `OPEN#` `open_cohort_key = null`. |
| `frozen` → `completed_no_change` | Router | `expected_revision == pointer_revision_at_freeze`; set `terminal_at`, `terminal_reason = no_change`, `expires_at`. No dispatch. |
| `frozen` → `frozen` + dispatch | Router | Send the generation message (§5); set `dispatched_at` conditioned `attribute_not_exists(dispatched_at)`. Send-then-mark: a duplicate send collapses on the deduplication id, and the materializer's claim is conditional. |
| `frozen` → `materializing` | Materializer | `state = frozen OR (state = materializing AND claim_expires_at < :now)`; set `claim_execution_id = :me`, `claim_expires_at = now + consumer timeout − 30 s` (the Stage 2 or Stage 3 constant in §5.1 / materialization §7; the deduction keeps the claim expiring before the message's visibility does, so a crashed consumer's redelivery finds the claim already expired instead of burning a receive). The FIFO group per page with batch size 1 and visibility longer than the function timeout is what prevents two live claimants in normal operation; the expiring claim makes the invariant hold without relying on the transport. |
| `materializing` → `completed_advanced` | Materializer | One transaction: generation terminal with `materialization_ref`, `pointer_outcome = ADVANCED`; pointer `CURRENT` advance with the fields in §6; `REVISION#` history item. Conditioned `state = materializing AND claim_execution_id = :me`. |
| `materializing` → `completed_no_change` | Materializer | Pointer already at `expected_revision` (raced by a reconcile); `pointer_outcome = NO_OP`. |
| `materializing` → `failed` | Materializer | Typed `terminal_reason` (`insufficient_evidence`, `revision_mismatch` — the consumer cannot reproduce `expected_revision` from its bound publications, `materialization_budget_exceeded`, `render_budget_exceeded`, `pointer_stale` — the pointer conditional failed, `claim_lost` — the generation conditional failed because the claim expired and was taken over, `dispatch_exhausted`). Last-good pointer untouched. |
| `frozen`/`materializing` → `superseded` | Router | A relevant transition arrives for the same cohort key while `state ∈ {frozen, materializing}`; set `superseded_by_seq`, `terminal_at`, `expires_at`; open the successor in the same transaction with `predecessor_seq`. |
| `materializing` → `abandoned` | Materializer | At each checkpoint (after measurement, before persist, before pointer) it re-reads its state; `superseded` → stop, set `abandoned`, record usage `abandoned_materialization`. Its conditional pointer transaction can no longer succeed regardless. |
| terminal → successor `open` | Router | Predecessor terminal: successor created with `predecessor_seq`; no supersession. |
Terminal items carry `expires_at = terminal_at + 30 days`. Head items do not expire. Non-terminal
items never expire; a stuck `materializing` generation is reached by redelivery (§5) and exhaustion
yields `failed` with `dispatch_exhausted`.
### 4.3 Completeness
A required dependency is covered when, for every day partition in the windows its `window` field
names (target, target and prior, or the single as-of day), the dataset has a `CURRENT` publication.
The router evaluates this from a strongly consistent read of the dataset's `CURRENT` items over the
window — the same bounded coverage read the current baker performs, now performed once per
transition instead of once per bake — and records the covering `{partition, publication_id,
source_revision}` list in `revisions[dataset]`. Datasets declaring `window: whole` (dimensions and
hierarchies delivered whole) cover every window when their single `CURRENT` publication exists.
### 4.4 Expected revision
At freeze the router computes `expected_revision =
PresetArtifactRevision.from_publication_identities(sorted publication ids across every required
dataset in revisions, render_format)`. This is the same derivation the current renderer uses after
measurement, applied before it; the materializer must reproduce the identical value from its exact
source roster or fail `revision_mismatch`. The roster digest `sha256(sorted publication ids)` is
stored on the generation and written to the pointer (§6). The deployed renderer builds its revision
from the publications bound by evidence inspection over its plans, not from a declared roster; the two
agree only when the dependency contract's `required` datasets and `window` fields reproduce exactly the
set inspection binds. Acceptance item 9 tests that equality on the fixture cohort, and §2.3's
conformance check is the design-time guard.
## 5. Dispatch transport
| Property | Value |
|---|---|
| Queue | `adlytics-<env>-generation.fifo` with DLQ `adlytics-<env>-generation-dlq.fifo`; replaces `preset-bake.fifo` in the same change |
| `MessageGroupId` | `<page>`. One page's variants share evidence retrieval under the declared preset-reuse rule; serializing per page lets one materializer invocation serve every variant frozen by one router execution. Distinct pages proceed in parallel. |
| `MessageDeduplicationId` | `sha256(page, sorted generation record keys)` |
| Body | `{"page", "generations": [{"page_variant_key","record_key"}], "flow_ids", "causation_publication_id"}` — identity only |
| Batch size | 1; `reportBatchItemFailures: true`; `maxConcurrency` = configured page concurrency (initial 2) |
| Visibility | consumer timeout + 60 s (360 s at Stage 2; materialization §7 at Stage 3) |
| `maxReceiveCount` | 4 (§5.1); the exhausting invocation marks each carried non-terminal generation `failed` / `dispatch_exhausted` before the message reaches the DLQ |
The router's access profile resembles the baker's, not the retired enqueue Lambda's: stream read,
strong reads and the `scope-current` GSI query on the publications table for coverage, read/write on
the pointer table's generation key spaces, and send on this queue — each granted per function, least
privilege. Its timeout is budgeted from the bounded coverage reads per batch, not from the enqueue's
seconds.

A message carries only generations frozen by the same router execution for one page. The consumer
claims each generation independently; a generation already terminal or superseded at claim is skipped
with usage `superseded_dispatch`, so a message is never re-dispatched and a late supersession costs no
measurement.

### 5.1 Stage 2 consumer

Stage 2 ships the router, the generation record, and this queue, and retires the enqueue Lambda and
`preset-bake.fifo` in the same change. Until the Stage 3 materializer exists, the **existing baker**
is adapted as the transitional consumer of `generation.fifo`: it reads the per-page message, and for
each carried generation claims it (§4.2), resolves windows from the generation's recorded periods,
measures and renders exactly as today for that variant, reproduces the revision and compares it with
`expected_revision` (`revision_mismatch` on disagreement), and advances the pointer in the one
transaction with the generation terminal carrying the §6 fields. It performs no cross-variant
derivation and persists no materialization.

Its budget: the transitional consumer keeps the baker's constants — `STAGE2_CONSUMER_TIMEOUT_SECONDS =
BAKER_TIMEOUT_SECONDS` (300), visibility 360 s, claim expiry 270 s (consumer timeout − 30 s, §4.2) —
and the bakeability gate continues
to prove one variant per invocation. It processes the carried generations sequentially in message order
and, before each, compares the remaining invocation time with that variant's gate estimate
(`ceil(plans / concurrency) × PER_QUERY_SECONDS` plus `STAGE2_PERSIST_ALLOWANCE_SECONDS = 30`, a new
constant beside the gate's in `bakeability.py` covering body/custody/sibling puts and the terminal
transaction); when the remainder is
insufficient it returns the message as a batch failure so the FIFO redelivery resumes with the
generations that are still `frozen` (terminal ones are skipped at claim). `maxReceiveCount` for the
generation queue is therefore `max variants per page + 1` = 4, and the exhaustion rule marks only
non-terminal carried generations `dispatch_exhausted`. Stage 3 replaces the consumer's internals with
[generation materialization](generation-materialization.md) §4 and its budgets without changing the
message, the claim, or the terminal transaction. No deployment runs both the enqueue/bake path and the
router path.
## 6. Pointer transaction identity fields
The `CURRENT` and `REVISION#` items gain, in the same transaction that advances them:
| Field | Value |
|---|---|
| `generation_key` | The generation's `record_key` |
| `source_roster_digest` | §4.4 digest |
| `advanced_at` | Materializer clock at transaction time (customer-freshness stop) |
| `outcome` | `ADVANCED` for a moved pointer. `NO_OP` and `STALE` outcomes are recorded on the generation only, because the pointer item did not change. |
| `contract_version` | Dependency contract version the generation used |
The serve path already returns the selected artifact revision; echoing `generation_key` at the
serving boundary is Stage 5 work (no receipt object exists in code today), and Stage 2's obligation
ends at the pointer items.

`preset_artifacts.py` decodes a pointer item by exact equality with its serializer, so the new fields
are optional on `PresetArtifactPointer` (absent on every item written before this design) and both
serializers accept their absence. `contract_version` is packaged with the router, consumer, and
reconcile functions at deploy (as `publisherDeploymentGeneration` is today), never read from S3 at
runtime.
## 7. Reconcile (first bake, deploy, contract change)
A new `contract_version` means new cohort keys and no generations. `generation_reconcile` (invoked by
the deploy custom resource that today drives the backfill, and callable by an operator) walks every
artifact-served page/variant, resolves target periods from current coverage, and runs §3 steps 4–5 as
if one synthetic transition had named every required dataset. Reconcile generations carry
`causation_flow_id = reconcile:<execution id>`; their no-change and dispatch behaviour is identical.
Reconcile never bypasses the no-change gate, so a redeploy with an unchanged contract and unchanged
sources costs revision comparisons, not bakes.
## 8. Amplification bounds (ADR-075 §7)
Per source transition and affected variant: at most one open-or-join write, at most one freeze, at most
one dispatch. Per generation: at most one materialization. The router records every non-productive path
in `usage`: `authority_only`, `migration_replay`, `unrouted`, `out_of_window`, `duplicate_transition`,
`superseded_transition`, `superseded_dispatch`, `period_moved`, `abandoned_materialization`,
`no_change`, and `dispatch_retry` (recorded by the consumer when a message's approximate receive
count exceeds one). These are this design's fine-grained categories, not the ADR-074 set itself; the
ledger's closed `UsageByCategory.v1` receives them through a declared mapping: `no_change → no_change`;
`duplicate_transition`, `authority_only`, `migration_replay` → `duplicate_waste`;
`superseded_transition`, `superseded_dispatch`, `period_moved`, `abandoned_materialization` →
`superseded`; `dispatch_retry` → `retry`; `unrouted`, `out_of_window` → `refused`; productive
terminals → `productive`. The Stage 0 baseline records the current path's equivalents on the
`preset_enqueue` and `preset_bake` surfaces ADR-075 §9 names, so before/after rows
compare like categories.
## 9. Status events
| Event | Emitted by | Carries |
|---|---|---|
| `router_acknowledged` | Router | stream record identity (partition, publication id, sequence number), usage category, affected page/variant when mapped, and for `out_of_window` the resolved-vs-required windows — the evidence an operator needs to answer "why did this transition produce nothing". Until the ADR-073 control plane exists these are structured lifecycle logs; the durable ledger arrives with it. |
| `generation_opened`, `generation_joined`, `generation_frozen`, `generation_no_change`, `generation_dispatched`, `generation_superseded`, `generation_abandoned_period_moved` | Router | page, variant, target period, contract version, seq, flow ids, causation publication, expected revision |
| `generation_claimed`, `generation_abandoned`, `generation_terminal` | Materializer | generation key, execution id, terminal reason, pointer outcome, materialization ref |
All carry the operation, execution, and predecessor identity the observability steering requires and
never carry measurements or rows. For Stage 2 a transition's flow identity **is its publication id**:
no richer flow identity exists on the semantic path yet, and the publication id joins the generation
to the write plan (whose `semantic_plan_recorded` event lists committed publication ids) and through
it to the landing. `flow_ids` and `causation_flow_id` therefore carry publication ids until the
ADR-073 flow registry propagates landing flow identity onto publications, at which point the fields
carry that value without a schema change. Router, consumer, and reconcile each run under a registered
root span with per-generation child spans; span identities are registered in
`contracts/observability/spans.yaml` and validated by the trace-contract gate, and the reconcile
custom resource carries the packaged `contract_version` as its trigger property.
## 10. Decisions recorded here
| Decision | Rationale | Rejected |
|---|---|---|
| Generation items in the pointer table | Pointer advance and generation terminal are one transaction; one RETAIN store, one IAM surface | A separate coordination table (two-phase commit between tables) |
| Cohort head item with `open_seq` | Open-or-join is one conditional transaction without a scan | Querying generations by prefix and choosing the newest (races between routers) |
| FIFO group per page, not per variant | One invocation serves every variant frozen together and shares declared-reusable retrieval | Per-variant groups (three retrievals for one day's data) |
| Send-then-mark dispatch | Deduplication id and conditional claim make a duplicate send harmless; mark-then-send can lose the message | Outbox for dispatch (unnecessary here: the message is derivable and idempotent) |
| No-change decided by the router | It holds the frozen roster and a strong pointer read; measurement never starts | Letting the materializer decide (it has already been invoked) |
| Dependency contract generated from three owners | Visual metrics from the dictionary, non-visual inputs from the page owner, presets from pages.yaml; one generated projection, one version | Hand-maintained roster (drifts from the renderer) |
## 11. Acceptance for Stage 2
1. A `CURRENT` transition for a dataset no artifact-served page names produces zero messages.
2. A transition for one day of one Campaign dataset produces at most one generation per affected variant, and one dispatch message for the page.
3. Zero-measurement paths: a replayed stream record naming a publication already in the cohort ends `duplicate_transition`; a reconcile over an unchanged roster ends `completed_no_change`; both run zero Athena statements. (A genuine re-publication mints a new publication id, changes the expected revision, and correctly dispatches — the no-change gate is identity-based, not content-based.)
4. A late whole-delivered required dependency (control total or dimension) joins an open generation without resetting `started_at`; a leading day-grained dataset's new day is `out_of_window` until the cohort's common coverage moves.
5. A transition after freeze supersedes a `materializing` generation; its materializer abandons before persisting, and the successor advances the pointer once.
6. The pointer item carries `generation_key`, `source_roster_digest`, `advanced_at`, `outcome`, `contract_version`; the Stage 5 serving-receipt echo is out of Stage 2 scope.
7. `make check-page-dependencies` fails when a renderer plan references an undeclared dataset.
8. Before/after usage rows for enqueue and bake are populated from the Stage 0 baseline and this design's categories.
9. For the fixture cohort, the router's `expected_revision` equals the revision the consumer reproduces from its bound publications for every variant; a disagreement fails the stage, not the generation.
10. The transitional Stage 2 consumer advances the pointer with the §6 fields and existing pointer items still decode.
11. Facts for day D+1 arriving before day D's control total abandon D's open generation with `period_moved` and the control total joins D+1's generation; the `OPEN#` item never names two cohorts.
12. An authority-only stream record produces no router work (`authority_only`); a replayed servable record for a publication already in the cohort is `duplicate_transition`.
## 12. Risks and open questions
- Common coverage means an `open` generation exists only while a whole-delivered dependency lags. If the next day's facts arrive before that dependency, the period moves and the open generation is `abandoned` (`period_moved`); the late dependency then joins the new cohort. A page whose control total habitually trails its facts by more than a day will show a run of abandoned generations, which is the honest measurement of that source's lag, not a defect of the router.
- Datasets declaring `window: whole` are covered by existence, not by day; a page whose hierarchy must be as-of a date needs a `window: as_of_target_end` declaration and a dated dimension publication, which no page needs today.
- Per-page FIFO grouping bounds page parallelism to page count (two today); it is a ceiling, not a target.
- The status events in §9 are contracts for the ADR-073 control plane; until its queue and routes exist they are emitted as structured lifecycle logs, and route-level acceptance belongs to that stage.
