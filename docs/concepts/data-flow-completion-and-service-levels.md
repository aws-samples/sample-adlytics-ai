# Concept — Data-Flow Completion and Service Levels

| | |
|---|---|
| **Level** | L2 — crosscutting concept |
| **Status** | Accepted by [ADR-074](../decisions/adr-074-define-authority-backed-data-flow-completion-and-service-level-objectives.md); development observation-only measurement is authorized, while evaluated SLO activation remains gated on accepted objectives. |
| **Last updated** | 2026-09-25 |
| **Owns** | Reusable flow-completion semantics, stage clocks, SLI/SLO evaluation, child obligations, and continuity-versus-freshness rules. |
| **Applied by** | [Source Acquisition](../capabilities/source-acquisition/README.md), [Load Path](../capabilities/load-path/README.md), [Publication State](../capabilities/publication-state/README.md), [Preset Artifact Publisher](../ui/preset-artifact-publisher.md), [Metric Focus](../ui/metric-focus-contract.md), [Conversational Runtime](../capabilities/conversational-runtime/README.md), [Agent Quality Loop](../capabilities/agent-quality-loop/README.md), and [Operational Data-Flow Status](../capabilities/operational-data-flow-status/README.md). |
| **Conventions** | [Documentation conventions](../conventions.md) |

## 1. What this concept is

This concept defines when repository-owned data movement is complete and how its latency,
reliability, freshness, availability, and resource use are evaluated. It applies to one logical flow
and its declared child obligations, not to an infrastructure invocation or queue message.

Lives elsewhere: movement capabilities own their durable authorities and transitions;
`contracts/operations/data-flows.yaml` is the active registry for concrete surface measurement
definitions and references the versioned dependency rosters owned by generated page contracts;
[Operational Data-Flow Status](../capabilities/operational-data-flow-status/README.md) owns collection,
projection, reconciliation, and operator APIs; [Observability](observability.md) owns telemetry;
[Provenance Chain](provenance-chain.md) owns analytical custody; page operational designs own
request-path engineering budgets, while the governed registry alone owns accepted SLO targets; and
[validation results](../../validation/RESULTS.md) own dated attainment evidence.

## 2. The rule

**Never claim completion or SLO attainment from submission, process exit, heartbeat, queue depth,
message acknowledgement/deletion, provider-request acceptance, status projection, or continued
last-good serving.** Completion requires exact re-attestation of the owning durable terminal plus every
required child obligation. SLO evaluation is a separate immutable result over declared clocks.

### Identity and result classes

One logical obligation has one operator-safe `flow_id`. A retry keeps that flow, creates a new
`execution_id`, and preserves predecessor lineage. A child has its own `flow_id`, immutable parent
membership, and the causation event that created it. Exact duplicate delivery reuses the existing flow
and creates no second observation or objective sample; any transport, invocation, read, or no-op write
that actually runs is still attributed to that flow's `duplicate_waste` usage category.

A derived companion flow fingerprints every immutable payload-defining input: schema and registry
version, surface and authority kind, canonical workload identity, clock-start instant, start
stage/state, and complete evaluation binding (mode plus measurement references). Its terminal event
additionally fingerprints the flow identity and complete terminal disposition (state, outcome, stage,
authority evidence, time, reason, and progress). Two byte-identical observations reuse one identity;
changing any immutable evidence creates a new flow/event identity rather than colliding with an
existing receipt. Event IDs never identify only a subset of the evidence they protect.

Every registered surface declares `evaluation_mode: observation_only | evaluated`.
`observation_only` records descriptive authority-backed measurements and never produces an attainment
result. `evaluated` references an accepted objective and adds immutable per-flow and window judgments.
Changing modes creates a new registry version; historical observations are not retroactively scored.

Result classes remain separate:

| Class | Closed values | Meaning |
|---|---|---|
| Lifecycle | `observed`, `queued`, `running`, `retrying`, `delayed`, `blocked`, `failed`, `completed`, `superseded`, `unknown` | Where the obligation is now in either mode. |
| Completion outcome | `succeeded`, `completed_no_change`, `completed_refused`, `exhausted`, `superseded` | What durable terminal decision occurred in either mode. |
| Baseline observation | No attainment value | What was measured under one versioned clock/evidence contract while `observation_only`. |
| Objective sample result | `met`, `missed`, `unknown`, `not_applicable` | How one logical flow evaluated against one activated objective version. |
| SLO window result | `met`, `missed`, `unknown` | Whether a complete evaluated window attained the aggregate objective. |

In evaluated mode, a flow may complete correctly after its deadline; lifecycle is `completed` while its
objective sample remains `missed`. Missing or unreadable evidence yields `unknown`, never `met`.
`completed_refused` and `exhausted` are terminal but never successful completion of a required child.
Window attainment is a separate immutable aggregate over evaluated samples. Observation-only mode
retains the same lifecycle, outcome, evidence, and child-success facts but attaches none of those
attainment labels.

### Clock contract

Each measurement contract declares one start authority, one completion authority, one clock, and one
measure/unit. An evaluated objective references that immutable measurement version and adds a typed
target plus window policy. Retries, throttling, queueing, owner-controlled delay, and dependency wait
never reset or pause an already-started clock in either mode.

| Clock | Starts | Stops | Answers |
|---|---|---|---|
| Repository-observed delivery | First exact immutable registration or canonical manifest observation, as declared by the flow | Owner terminal for that delivery | Did the repository decide the observed delivery? |
| Semantic readiness | Canonical manifest first observed | Required semantic `CURRENT` publications are applied (servable projection attested) and re-attested, or the root reaches another terminal | When did exact source evidence become current and queryable? |
| Customer freshness | First applied semantic `CURRENT` transition registered to an immutable page/window generation | Exact target artifact pointer advances to that generation | When could the customer see the changed source cohort? |
| Cohort processing | Last required source child becomes ready | Exact target artifact pointer advances | How efficiently did the platform process a complete cohort? |
| AI enhancement | Deterministic artifact revision becomes authoritative | Every eligible AI child is `filled`, verified `silent`, or terminally unavailable | When did optional narration settle? |
| Request serving | Authenticated request accepted | Response releases the exact selected revision | How long did serving take? |

A semantic `CURRENT` transition is *applied* when its servable projection is attested — the instant
readers can query the publication's rows — as recorded by the
[Load Path write discipline](../capabilities/load-path/write-discipline.md#23-envelope-attestation-and-readers); the semantic
readiness clock stops on that instant, not on the authority advance.

A page/window generation is identified by page, variant, resolved target period, dependency-roster
version, and generation sequence. The first applied relevant semantic `CURRENT` transition creates it
and freezes `started_at`. A generation is *open* until every required dependency has a compatible
revision; while open, later required transitions for the same target period join it without resetting
the clock. Once the cohort is complete the generation is *frozen*: its dependency revisions are fixed
and materialization may begin. A relevant transition that arrives after freezing — including a
re-publication of a dataset already in the frozen cohort — creates a successor generation with its own
`started_at`; the successor supersedes the frozen predecessor if the predecessor has not yet reached a
terminal outcome, and follows it as a predecessor-linked generation if it has. Superseded work is recorded,
never repeated, and never allowed to regress the pointer. A transition that resolves a different target
period creates a different generation; an *open* generation whose page/variant now resolves a different
target period can never freeze and is *abandoned* (`period_moved`), a terminal distinct from
supersession because no successor for its own period exists.

External extraction time precedes repository authority and is `external_boundary`. It may be included
only when the external producer supplies an accepted immutable timestamp contract; it is never inferred
from landing time.

### Required and optional children

A parent registers a complete bounded child set before terminal evaluation. Required children must
reach the objective's declared successful outcomes; terminal count alone is insufficient. Optional
children are visible and measurable but do not gate deterministic parent completion.

For data-served pages:

1. delivery children establish exact immutable source evidence;
2. semantic children establish required successful `CURRENT` publications;
3. a page/window cohort child establishes every declared metric and non-visual artifact dependency;
4. artifact children establish every required body, custody object, sibling, and pointer;
5. AI children are optional unless a future page contract explicitly promotes a slot; and
6. synchronous serving is a separate acceptance objective, not an asynchronous movement child.

Only a parent `completed` proposal waits for required children. `failed`, `blocked`, and `superseded`
remain immediately visible. A failed new generation may coexist with healthy last-good serving; serving
continuity does not satisfy the failed generation's freshness objective.

### Cohort contract

A page/window cohort is a versioned declaration, not a runtime guess. The future generated page
dependency contract under `presentation/pages/v2/` is the sole owner of every metric source, ratio
operand, identity dimension, hierarchy/control input, comparison window, custody requirement, and
render-format dependency needed to build the target artifact. The operations registry owns which
objective applies to a surface and references that dependency version; it never copies the roster.

`pages.yaml` and the metric dictionary can derive visual metric sources, but they do not currently
express every non-visual artifact dependency. A page cannot claim a complete cohort objective until its
full dependency declaration is reviewed and generated from one owner.

### Measurement, SLI, and SLO contract

Observation-only measurements and evaluated SLI results both use authority timestamps and registered
eligibility. The active registry owns concrete measurement definitions; a future evaluated registry
version may add accepted objective values. This concept owns their meaning. Observation-only mode persists `DataFlowObservationSample.v1` and
`DataFlowBaselineWindow.v1` only. Those records contain no target, result, deadline decision, error
budget, or burn rate. Evaluated mode references the exact measurement version and may additionally
persist objective samples and SLO windows.

```text
DataFlowEvaluationBinding.v1 =
  {mode: observation_only, measurement_refs}
  | {mode: evaluated, measurement_refs, objective_refs}

DataFlowMeasurement.v1 {
  measurement_ref, measurement_version
  flow_kind, measure, unit
  start_authority, completion_authority
  dependency_roster_ref
  required_child_policy, optional_child_policy
  eligible_outcomes, excluded_outcomes
  baseline_window, baseline_finalization_delay
}

DataFlowObjective.v1 {
  objective_ref, objective_version
  measurement_ref, measurement_version
  target_kind
  sample_target: {measure, comparator, value, unit}
  window_percentile, evaluation_window, minimum_sample
  allowed_miss_fraction, maximum_unknown_fraction, burn_windows
  freshness_policy_ref, cost_policy_ref
}

UsageUnits.v1 {
  athena: optional {query_count: uint64, scanned_bytes: uint64, engine_ms: uint64}
  lambda: optional {invocation_count: uint64, billed_ms: uint64, memory_mb_ms: uint64}
  queue: optional {request_count: uint64, message_count: uint64, payload_bytes: uint64}
  storage: optional {read_count: uint64, write_count: uint64, bytes_read: uint64, bytes_written: uint64}
  model: optional {call_count: uint64, input_tokens: uint64, output_tokens: uint64}
}

UsageByCategory.v1 {
  productive, retry, duplicate_waste, no_change, refused, unavailable, superseded:
    optional UsageUnits.v1
}

DataFlowObservationSample.v1 {
  measurement_ref, measurement_version, flow_id
  dependency_roster_ref
  started_at, decided_at, elapsed_ms
  sample_value: {measure, value, unit}
  lifecycle_state
  completion_outcome: optional succeeded | completed_no_change | completed_refused | exhausted | superseded
  evidence: complete | incomplete | stale | unavailable
  required_children: {declared, successful, terminal}
  optional_children: {declared, successful, terminal}
  usage_at_decision: UsageByCategory.v1
}

DataFlowBaselineWindow.v1 {
  window_id, predecessor_window_id: optional, revision
  measurement_ref, measurement_version
  dependency_roster_ref
  window_start, window_end, population_cutoff, finalized_at
  completeness: complete | incomplete
  late_sample_count
  samples: {total, by_lifecycle, by_outcome, by_evidence}
  observed: {minimum, maximum, mean, p50, p90, p95, p99, unit}
  usage: UsageByCategory.v1
}

DataFlowObjectiveSample.v1 {
  objective_ref, objective_version, flow_id
  measurement_ref, measurement_version
  dependency_roster_ref
  sample_target: {measure, comparator, value, unit}
  started_at, decided_at, elapsed_ms
  sample_value: {measure, value, unit}
  lifecycle_state
  completion_outcome: optional succeeded | completed_no_change | completed_refused | exhausted | superseded
  decision_reason: owner_terminal | deadline_elapsed | evidence_unknown | timely_supersession
  sample_result: met | missed | unknown | not_applicable
  evidence: complete | incomplete | stale | unavailable
  required_children: {declared, successful, terminal}
  optional_children: {declared, successful, terminal}
  usage_at_decision: UsageByCategory.v1
}

DataFlowUsageContribution.v1 {
  contribution_id, flow_id, execution_id
  evaluation_binding: DataFlowEvaluationBinding.v1
  reconciles_disposition_id: optional
  usage: UsageByCategory.v1
  observed_at
}

DataFlowUsageEvidenceDisposition.v1 {
  disposition_id, flow_id, execution_id
  evaluation_binding: DataFlowEvaluationBinding.v1
  category: productive | retry | duplicate_waste | no_change | refused | unavailable | superseded
  resource: athena
  known_usage: UsageUnits.v1 with athena.query_count exactly 1 and only exact currently known scanned_bytes/engine_ms
  missing_quantities: nonempty bounded tuple of scanned_bytes | engine_ms
  evidence: unavailable
  observed_at
}

DataFlowObjectiveWindowResult.v1 {
  objective_ref, objective_version
  measurement_ref, measurement_version
  dependency_roster_ref
  sample_target: {measure, comparator, value, unit}
  window_start, window_end, evaluated_at
  samples: {met, missed, unknown, not_applicable}
  observed_percentile, miss_fraction, unknown_fraction
  slo_result: met | missed | unknown
  burn: {short_window, long_window}
}
```

`DataFlowEvaluationBinding.v1` is a discriminated union, not optional-field convention. Observation-only
bindings reject any objective reference. Evaluated bindings require at least one accepted objective;
every objective must name exactly one measurement reference/version carried by the same binding. Events
and usage contributions must repeat the root flow's binding byte-for-byte or be rejected.

A baseline window closes at the measurement contract's deterministic population cutoff: `window_end`
plus `baseline_finalization_delay`. `window_id` is derived from measurement/version,
dependency reference, bounds, and revision. Finalization writes one immutable record after registered
authority cursors cross the window end or marks it `incomplete` when exact evidence cannot be attested.
A terminal sample reconciled after the cutoff never mutates that record: it creates an immutable
successor with `predecessor_window_id`, incremented revision, `late_sample_count`, and a newly attested
population. The summary points to the latest revision while history retains every prior cut. Active
flows remain excluded from percentiles in every revision.

Every usage quantity is a nonnegative `uint64`. In an ordinary contribution or aggregate, an omitted
resource or quantity field means zero. In an unavailable-evidence disposition, a field listed in
`missing_quantities` is unresolved rather than zero; `known_usage` is aggregated exactly once under
that disposition's fixed category. An aggregate `UsageByCategory.v1` may populate any closed category,
while an ordinary contribution must populate exactly one category with at least one positive quantity.
A contribution with `reconciles_disposition_id` is a generic exception only when the contributing
owner's versioned contract explicitly authorizes it: it must repeat that disposition's flow, execution,
evaluation binding, and category; contain all and only the previously missing Athena fields with no
query count; and may carry provider-reported zero because its purpose is exact evidence closure as well
as aggregation. The closed quantity names are raw billable or
reconcilable resource units, never currency: Athena queries/scanned bytes/engine time, Lambda
invocations/billed time/memory-time, queue requests/messages/payload bytes, storage reads/writes/bytes,
and model calls/input/output tokens. Overflow, a negative value, an unknown quantity, or a producer unit
that cannot be reconciled makes usage evidence `unknown` rather than truncating or coercing it.

Observation samples freeze only when the owner reaches an authority-backed terminal or evidence becomes
unavailable; active elapsed time stays on the live flow snapshot and is excluded from descriptive
percentiles so censored work cannot look fast. Baseline windows report their full sample count, closed
lifecycle/outcome/evidence distributions, fixed descriptive percentiles, and resource usage regardless
of population size. A small or biased population is visible evidence quality, never a health result.
`DataFlowUsageEvidenceDisposition.v1` is the retained, idempotent representation of known work whose
provider usage is only partially reconciled: `evidence=unavailable` is the closed historical wire value,
`known_usage` preserves the attempt's fixed-category `query_count=1` and any exact provider quantities
already present, and `missing_quantities` distinguishes the unresolved fields from true zero or no
work. A later exact delta may complete current evidence only when another contributing owner's
versioned contract explicitly authorizes that use of the generic field; the Stage-1 rule below does not.

The observation, objective-sample, and window records contain no customer identity and are not metric
dimensions. Aggregates use only registered low-cardinality surface, measurement, objective, environment,
and result vocabularies. Usage is a
separate append-only stream: each actual execution unit has a stable `contribution_id`, so lifecycle
event idempotency cannot erase work performed because of a later duplicate delivery. Contributions bind
to the original flow and measurement, plus the objective when evaluated, but never create another
latency sample or mutate immutable `usage_at_decision`; a current usage summary may advance as later contributions arrive. A cost objective
is decided only after its registered usage-freeze authority closes.

### Stage 1 usage attribution

Stage-1 per-flow usage authority is Athena-only. Its durable table-attempt record is the sole authority
for an accepted query; queued-unit and operator-run records identify the owning public flow/execution
and reference attempts. Stage 1 creates no per-flow Lambda, queue, or storage contribution identities or
records. Those resource performance comparisons remain the already accepted ADR-074 companion/
CloudWatch before-and-after evidence; they are not reconstructed as Stage-1 usage.

Let `U` be the operational-status UUIDv5 constructor. One settled provider attempt has contribution id
`U("usage", "stage1-provider-attempt", attempt_id, category)`. Direct processing, provider-attempt
reconciliation, and exact owner replay reproduce the same ID when they reach the same final owner
settlement; they never create a second contribution merely because another process observed terminality.
When one atomic plan replacement terminalizes a planned predecessor and records its successor, work
already settled for the predecessor remains attributed to its original public execution and category;
later successor work belongs to the new predecessor-linked execution. The replacement transaction
submits no Athena request and therefore creates no Athena contribution of its own.

Provider terminality fixes only `query_count=1` and the exact present-or-missing
`DataScannedInBytes`/`EngineExecutionTimeInMillis` evidence for `SUCCEEDED`, `FAILED`, and `CANCELLED`.
It does not choose a category. Category becomes immutable only when a durable final owner disposition
selects it through the closed matrix below and atomically settles the referenced attempt. A process that
only observes provider terminality—including scheduled reconciliation—cannot guess or emit category.
Prior settled attempts never change category.

At settlement, complete evidence creates one contribution with `query_count=1`, exact
`scanned_bytes`, and exact `engine_ms`; provider-reported zero is valid. If either provider quantity is
absent, settlement instead creates deterministic disposition id
`U("usage-evidence", "stage1-provider-attempt", attempt_id)` with the selected category, missing
quantity names, and `evidence=unavailable`. Its `known_usage.athena` contains `query_count=1` and each
exact provider quantity already present; missing fields are omitted, not zero. The attempt does not also
emit an initial contribution, so no query or known quantity can be counted twice.

A Stage-1 unavailable-evidence disposition is final for that accepted attempt. The historical
disposition and its current evidence state remain `unavailable`; no later observation, provider-attempt
reconciler, lifecycle producer, status discoverer, operator, or other principal may create a Stage-1
contribution with `reconciles_disposition_id`, add a late scanned-byte or engine-time delta, or mutate the
disposition/evidence state. The fixed category, `query_count=1`, and each quantity known at settlement
enter the aggregate exactly once. Values absent at settlement remain unavailable rather than becoming
zero or being completed later. Generic reconciliation fields remain part of the shared contract only
for another owner whose versioned contract explicitly authorizes them; Stage 1 rejects that authority.

The category is selected from the durable final owner disposition:

| Durable final Stage-1 owner disposition | `UsageByCategory.v1` category | Athena rule |
|---|---|---|
| First execution's accepted attempt contributes to an attested source/candidate unit or an advanced current pointer, and the durable owner disposition is successful | `productive` | Settle once with exact terminal provider quantities. |
| First execution's accepted attempt verifies an already exact table and durable pointer outcome is no advance because nothing changed | `no_change` | Settle once; work occurred even though authority did not change. |
| An accepted attempt belongs only to an equal/newer pointer outcome, or unresolved accepted work is closed by plan/run supersession before it can advance authority | `superseded` | Settle once at the superseding disposition. Earlier settled attempts keep their prior categories. |
| Any accepted attempt in an execution created for recovery or redrive | `retry` | Recovery/redrive execution is always retry regardless of later outcome. |
| Any accepted attempt settled by a retryable unit/run failure | `retry` | Settle every accepted attempt as retry, including `SUCCEEDED`. |
| Any accepted attempt settled by closing the unit/run exhausted or unavailable | `unavailable` | Settle every accepted attempt as unavailable, including an earlier `SUCCEEDED`. Cancellation request alone emits nothing. |
| Exact replay of an already settled attempt/token/event/run | unchanged | Emit no second Athena authority; changed category or payload conflicts. |
| `TABLE_BUSY`, `TABLE_ATTEMPT_PENDING`, feasibility/policy/input rejection, queue/admission/dependency failure, terminal supersession, zero-row publication, inline completion without provider acceptance, or exact collision before provider acceptance | none | Absence of an accepted provider request creates no statement or accounting row, regardless of lifecycle or pointer outcome. |

**Durable owner-disposition precedence.** Any accepted attempt, including `SUCCEEDED`,
settled by retryable unit/run failure is `retry`; any accepted attempt, including earlier `SUCCEEDED`,
settled by closing exhausted/unavailable is `unavailable`; recovery/redrive execution is always `retry`
regardless of later outcome. Otherwise, an accepted first-execution attempt follows its durable successful,
no-change, or superseding owner disposition. Once settled, no later owner outcome can reclassify the
attempt.

Without an accepted provider request, no statement or Stage-1 accounting row exists. Each accepted
attempt settles into exactly one category only when the final owner disposition is durable. Productive,
retry, no-change, unavailable, and superseded Athena totals are therefore disjoint; replay cannot add a
second contribution. `failed_count` remains a write-plan lifecycle counter, not a usage quantity.

Measurements and SLOs use these rules:

- **Observation-only baseline:** report exact sample values, evidence, outcomes, child counts, and usage
  under the measurement version. Descriptive windows never contain `met`, `missed`, `unknown`,
  `not_applicable`, an error budget, or a burn rate. An unavailable authority remains visible as
  evidence/outcome data; it is not silently excluded or converted into an SLO result.
- **Evaluated per-flow sample:** `sample_value` uses the referenced measurement's closed measure/unit. For latency objectives it
  is `decided_at - started_at`; a retry never changes `started_at`. The sample is `met` only when an
  eligible successful outcome satisfies the typed target. Eligible failed/exhausted/out-of-target
  outcomes are `missed`; unreadable evidence is `unknown`. If a latency deadline elapses while the flow
  is nonterminal, `decision_reason=deadline_elapsed`, `completion_outcome` is absent, and the immutable
  sample is `missed`; any later owner terminal updates lifecycle visibility but not that historical
  objective result.
- **Window evaluation:** `not_applicable` samples are counted but excluded. Below `minimum_sample`, or
  above the objective's maximum unknown fraction, the window is `unknown`. Otherwise the window is
  `met` only when the configured percentile of eligible numeric sample values satisfies the typed
  target **and** miss fraction is within its allowed bound; either breach is `missed`.
- **Error budget:** the allowed miss fraction defines the budget. Short/long burn windows evaluate the
  same eligible denominator; unknown samples never consume or replenish budget and instead block a
  healthy result when their configured bound is exceeded.
- **Freshness:** newest fully satisfiable source generation versus newest servable artifact generation.
- **Route availability:** successful typed responses over eligible requests, separately from freshness.
- **AI settlement and success:** every eligible slot terminal contributes to settlement latency. `filled`
  and verified `silent` are successful; `unavailable` is a settled but missed success sample. A slot
  omitted by its deterministic predicate is `not_applicable`; continuation-pending remains undecided.
- **Cost efficiency:** usage per successfully advanced target revision, with retry, duplicate, no-op,
  refused, unavailable, and superseded work reported separately so waste cannot disappear into the
  denominator.

Exact duplicates contribute no second observation sample or, when evaluated, objective sample, but
retain actual duplicate usage. An
equal-revision rules reprocess is a new predecessor-linked flow and remains eligible. A strictly older
source skip is `completed_no_change`: it may meet a processing-decision objective but never a
fresh-publication objective. Supersession before an objective deadline and before target publication is
`not_applicable` for freshness; a flow already late remains `missed`. Recovery can change lifecycle from
failed to completed, but never retroactively change a historical `missed` result to `met`.

## 3. How it works

**Concept diagram — Authority-backed completion, baseline measurement, and optional service-level evaluation; obligation viewpoint, concept depth.** One root flow closes only through required authority-backed children, while descriptive measurement, optional evaluated AI, and last-good serving remain separate results.

![Concept diagram: Authority-backed completion and service-level evaluation. An immutable landing starts a root flow. Semantic authority feeds a source cohort gate, which permits artifact publication and pointer commitment. The pointer enables serving. Optional AI enhancement begins from the deterministic artifact but does not gate its completion. Authority timestamps feed baseline measurement and, only for activated objectives, service-level evaluation. Last-good serving is distinct from freshness of the new flow.](diagrams/flow-slo.svg)

> **Takeaway.** Deterministic artifact readiness, baseline measurement, optional evaluated AI, last-good continuity, and SLO attainment are separate facts; only durable authorities can close them.

At flow start, the owner persists identity, measurement reference, clock origin, complete bounded child
policy, and optional activated objective reference beside authority or in a transactional content-free
companion. Owners append immutable outcomes; the status projector re-attests and reduces them without
becoming authority. Required child success permits parent completion. In observation-only mode the
measurement reducer freezes a descriptive terminal sample and updates baseline windows without judging
it. In evaluated mode the objective evaluator additionally freezes one immutable result when the flow
completes, misses its deadline, becomes unknowable, or becomes not applicable through timely
supersession.

### Milestone completion matrix

| Milestone | Starts | Successful authority | Wait/failure behavior | Blocks new deterministic serving? |
|---|---|---|---|---|
| Registration | Exact immutable registration version observed | Finalized status plus exact canonical result and manifest versions | `processing` waits; content refusal is `completed_refused`; exhausted dependency retry is not success | Yes before first canonical manifest; never removes last-good serving |
| Raw delivery | Exact canonical manifest version first observed | Versioned manifest and every bound object checksum/VersionId re-attest | Malformed, missing, or substituted evidence fails; external extraction remains outside authority | Yes for the new source generation |
| Semantic source | Exact delivery accepted | Immutable source S3 plus exact Iceberg projection | Source-only may complete without canonical children; identity/conformance/dependency failure does not | Yes when the target cohort requires it |
| Semantic publication | Candidate group starts under fenced lease | Exact successful bytes, immutable publication, `SUCCEEDED` attempt, and fenced `CURRENT` | Older revision is `completed_no_change`; quarantine/failure preserves last good; retry keeps root clock | Yes for a fresh target generation |
| Page/window cohort | First relevant source `CURRENT` changes | Every versioned metric and non-visual dependency has exact compatible current/prior evidence | Missing children are `blocked_dependency`/waiting, not failure or success; corrupt evidence fails | Yes for that target revision; prior pointer may keep serving |
| Artifact publication | Eligible cohort creates target revision | Every required body/custody/sibling attests and forward-only pointer names the revision | No-op is `completed_no_change`; persistence/DLQ failure leaves prior pointer; bytes without pointer are incomplete | Yes; pointer is the deterministic commit point |
| AI enhancement | Deterministic artifact revision becomes authoritative | Every eligible slot is immutable `filled` or verified `silent`; `unavailable` is terminal but not success | Continuation admission is queued, not complete; failure never blocks deterministic page | No by default |
| Serving acceptance | Authenticated request accepted | Returned revision equals exact selected pointer and all body/custody/cache evidence agrees | Typed precompute wait is not serving success for the new revision; prior coherent revision is continuity only | Final synchronous acceptance, not movement authority |

### Acceptance scenarios

| Scenario | Required conclusion |
|---|---|
| Happy path | Root becomes `completed` only after exact required-child success and target-authority readback; objective result uses the original clock. |
| Missing companion/authority | Reconcile from equivalent exact authority or mark evidence `unknown`; never infer terminal success. |
| Partial multi-contract delivery | Completed children remain visible; root and page cohort remain waiting with the absent child named by closed reason. |
| Mixed accepted-attempt roster | Settle every accepted attempt from the durable owner disposition: a retryable unit/run failure makes all accepted attempts `retry`, while closing exhausted/unavailable makes all accepted attempts `unavailable`, including earlier `SUCCEEDED` attempts. |
| Exact duplicate | Reuse one flow/event/observation sample and, when evaluated, one objective sample; no extra completion, pointer, or attainment contribution. Attribute any actual transport, invocation, read, or no-op write to `duplicate_waste`. |
| Equal-revision rules reprocess | New predecessor-linked eligible flow may republish and supersede; it is not an exact duplicate. |
| Strictly older delivery | Terminal `completed_no_change`/skip with `CURRENT` and freshness unchanged. |
| Recovery and redrive | Preserve `flow_id`, original start, and execution predecessor; every accepted attempt in the recovery/redrive execution is `retry` regardless of later outcome, and a late recovery cannot change `missed` to `met`. |
| Owner replay | Exact replay reproduces the same accepted-attempt settlement identity, category, and payload; it creates no second statement or accounting row, while any conflict is rejected. |
| Workload or status DLQ | Evidence becomes failed/incomplete under the owning surface policy; a DLQ record never creates completion. |
| Artifact failure with last good | New flow fails or retries while the prior exact revision remains available; continuity and freshness results differ. |
| AI filled | Immutable cache key, prompt version, artifact revision, verification, and text agree. |
| AI silent | Verified silence is a successful AI outcome with no invented prose and no continuation. |
| AI unavailable | Designed absence is terminal and nonblocking but does not enter the AI-success numerator. |
| Continuation | Queue admission is `queued`; completion requires exact cache readback or a declared terminal failure. |
| Supersession during work | Late old work cannot regress the pointer; timely obsolete flow is `not_applicable`, while already-late work remains missed. |
| Pointer gate | Required immutable bytes without the exact pointer are incomplete; replay/no-op and stale-token refusal are distinct. |
| Causal served revision | Response, capability parent, body/custody/siblings, and insight cache all name one selected revision. |
| Unknown evidence | Last-good serving may continue, but the new lifecycle and SLO result remain `unknown`. |
| Cost attribution | Retry, refused, no-op, unavailable, and superseded usage remains visible; aggregate usage reconciles without UUID metric dimensions. |

The first observation-only implementation acceptance uses fictional data to exercise identity,
authority-backed completion, child policy, evidence, usage, and descriptive baseline records without any
attainment result. Adapter integration uses real development S3, DynamoDB, and SQS resources for
conditional write, ordering, retry, and DLQ behavior. A governed development probe joins one exact root
through the currently implemented slice and returned measurements. The remaining scenario rows become
evaluated-mode acceptance only after their objectives and dependencies activate. Dated results stay in
validation; this concept never records attainment.

## 4. Rejected alternatives

| Alternative | Decision |
|---|---|
| Treat queue drain, successful invocation, or submitted provider work as completion | Rejected by [ADR-074](../decisions/adr-074-define-authority-backed-data-flow-completion-and-service-level-objectives.md). |
| Make last-good serving prove freshness of a failed generation | Rejected by ADR-074; continuity and freshness remain separate. |
| Let each capability define its own SLO arithmetic | Rejected by ADR-074; concrete values vary, but semantics and denominators must not. |
| Put completion authority in the status projection | Rejected by [ADR-073](../decisions/adr-073-project-all-data-movement-into-an-operator-status-control-plane.md). |
| Make AI generation a universal artifact gate | Rejected by ADR-074 and [ADR-068](../decisions/adr-068-fill-declared-insight-slots-with-direct-structured-model-calls.md). |

## 5. Decisions

| Decision | Owner |
|---|---|
| Authority-backed completion, clocks, child policy, and objective evaluation | [ADR-074](../decisions/adr-074-define-authority-backed-data-flow-completion-and-service-level-objectives.md) |
| Content-minimized status projection, reconciliation, and operator API | [ADR-073](../decisions/adr-073-project-all-data-movement-into-an-operator-status-control-plane.md) |
| Attempts, leases, successful publications, and exact coverage | [ADR-047](../decisions/adr-047-separate-attempts-leases-publications-and-coverage.md) |
| Revision-keyed optional model insight slots | [ADR-068](../decisions/adr-068-fill-declared-insight-slots-with-direct-structured-model-calls.md) |
| Publish-time preset artifact serving | [ADR-070](../decisions/adr-070-serve-declared-preset-windows-from-publish-time-artifacts.md) |
| Stage-1 semantic projection and usage constraints | [ADR-075](../decisions/adr-075-generate-page-artifacts-once-per-dependency-cohort.md) |

## 6. Open items

| Item | What resolves it | Blocks |
|---|---|---|
| Concrete typed sample targets, percentiles, evaluation windows, sample floors, allowed miss fractions, maximum unknown fractions, and burn policy | Product acceptance of objective values in the future flow registry | Evaluated SLO activation and attainment claims; observation-only baseline does not wait |
| Complete Campaign and Retail Health cohort declarations, including non-visual hierarchy, control, custody, and insight dependencies | One generated dependency contract reviewed against page materializers | Cohort-ready implementation |
| Cost attribution model, pricing-version policy, shared-cost treatment, and reconciliation tolerance | Product/FinOps decision recorded beside objective values | Monetary cost SLO; usage SLIs may proceed |
| External extraction clock and obligation | Source owner supplies an immutable timestamp and responsibility contract | Any source-extraction-to-serving SLO |
