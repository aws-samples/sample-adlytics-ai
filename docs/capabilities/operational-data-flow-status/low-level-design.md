# Operational Data-Flow Status — low-level design

This companion owns complete movement-surface coverage, wire contracts, operator workflows, and
acceptance. The [capability overview](README.md) owns the black-box interface; the
[architecture](architecture.md) owns component boundaries; [ADR-073](../../decisions/adr-073-project-all-data-movement-into-an-operator-status-control-plane.md)
owns the decision rationale.

| **Status** | Accepted 2026-09-19. The Stage-0 observation surfaces are live on dev since 2026-09-19 ([validation results](../../../validation/RESULTS.md)); the Stage-1 event projection, lifecycle producer, and outbox recovery (`infra/lib/stacks/operational-status-stage1.ts`, `src/adapters/jobs/operational_status/`) shipped with [ADR-075](../../decisions/adr-075-generate-page-artifacts-once-per-dependency-cohort.md) Stage 1, live-accepted on dev 2026-09-21. ADR-075's amendments record this design's refinement history. |

## 6. Interfaces and contracts

### 6.1 Complete surface registry

`contracts/operations/data-flows.yaml` is the sole roster. Every entry declares `surface`, `owner`,
`boundary`, `trigger`, `workload_transport`, `authority`, `source_observation`, `status_producer`,
`states`, `correlation`, `telemetry`, `reconciliation`, `freshness`, `retention`,
`completion_contract`, versioned `dependency_roster_ref`, `evaluation_mode`, `measurements`, optional
`objectives`, `operator_definition`, and implementation `status`. The meaning of completion,
measurement, and objective declarations is owned by
[Data-Flow Completion and Service Levels](../../concepts/data-flow-completion-and-service-levels.md);
this registry owns each concrete measurement definition, evaluation mode, objective value, and
dependency-contract reference, never the cohort contents. Measurement and objective versions are immutable and retained for historical evaluation; no active
version may replace a referenced historical definition. Optional fields are enumerated by schema; there is
no generic metadata block. `workload_transport` describes how customer work moves. Every active entry
uses the capability-wide direct status FIFO route; it does not redefine that route per surface.

The first registry version contains every currently known asynchronous surface:

| Surface | Start → terminal boundary | Durable authority | Source observation for status production | Reconciliation | Initial status |
|---|---|---|---|---|---|
| `external_source_delivery` | External extraction → registration/landing observed | External system until landing | Boundary declaration only | None; report external boundary | `external_boundary` |
| `producer_registration` | Registration object → finalized/rejected manifest | Registration status + immutable raw versions | Registration terminal companion/log until stream exists | Exact attempt read | `planned` |
| `raw_manifest_landing` | Data objects → manifest-last marker observed | Versioned raw S3 | Existing S3 EventBridge Object Created notification, then exact re-attestation | Exact VersionId/head attestation | `planned` |
| `ingestion_transport` | Manifest queued → acknowledged/DLQ | SQS workload transport state | Queue metrics and alarm state | Queue attributes/metrics | `planned` |
| `semantic_source_materialization` | Exact source read → source-aligned immutable projection | Immutable source S3 + Iceberg | Ingestion lifecycle companion | Exact source reference readback | `planned` |
| `semantic_candidate_publication` | Parsed partitions → candidate/quarantine | Candidate S3 + Iceberg + attempt ledger | Attempt transition companion | Exact attempt/candidate read | `planned` |
| `semantic_current_publication` | Successful bytes → fenced `CURRENT`/skip/fail | Publication/attempt tables + successful S3/Iceberg | Existing publications stream after strong re-attestation | Exact CURRENT/immutable pair | `planned` |
| `semantic_projection_plan` | Write plan recorded → completed, superseded, or admission failed | Write-plan metadata | Stage-1 lifecycle outbox | Exact write-plan metadata and lifecycle-head readback | `active` |
| `semantic_projection_unit` | Admitted table unit claimed → attested or exhausted | Write-plan unit | Stage-1 lifecycle outbox | Exact write-plan unit and lifecycle-head readback | `active` |
| `semantic_inline_table_run` | Inline table run recorded → terminal disposition | Inline table run | Stage-1 lifecycle outbox | Exact inline table-run and lifecycle-head readback | `active` |
| `semantic_servable_pointer` | Servable-pointer disposition → atomic projection finalization | Current servable pointer | Stage-1 lifecycle outbox | Exact current servable-pointer readback | `active` |
| `page_cohort_readiness` | First relevant CURRENT for page/variant generation → complete dependency contract/wait/fail | Referenced versioned page dependency contract + exact required CURRENT children + cohort companion | Required-child transition companion | Exact dependency-contract version and child CURRENT readback | `planned` |
| `preset_enqueue` | CURRENT transition → bake intent accepted/ignored | Re-attested CURRENT + FIFO intent | Enqueue lifecycle companion + workload FIFO aggregate | CURRENT-derived declared intents | `planned` |
| `preset_bake` | Bake intent → published/unavailable/retry/DLQ | Artifact body/custody + pointer | Bake terminal companion + workload queue metrics | Exact pointer/body/custody attestation | `planned` |
| `insight_precompute` | CURRENT transition → cache filled/refused/retry | Immutable insight cache | Per-publication terminal companion required | CURRENT + cache-key read | `planned` |
| `insight_continuation` | Continuation requested → cache filled/retry/DLQ | Immutable insight cache | Aggregate workload transport + terminal companion only | Exact cache key | `planned` |
| `evaluation_handoff` | Released audit → pending retained outbox | Audit/admission/outbox transaction | Audit KEYS_ONLY plus companion read | Exact audit/admission/outbox read | `planned` |
| `evaluation_dispatch` | Pending/leased outbox → delivered/retried | Outbox + delivery receipt | Content-free delivery receipt | Exact outbox/receipt read | `planned` |
| `evaluation_input_write` | Queue delivery → protected writer receipt/DLQ | Writer receipt in audit boundary | Aggregate workload transport + writer companion | Exact writer receipt | `planned` |
| `evaluation_result_ingress` | Managed result log → linked/ignored/retry | Admitted audit/writer evidence | Content-free ingress result companion | Exact admission/writer read | `planned` |
| `evaluation_result_persist` | Projection queue → evaluator result persisted/DLQ | Immutable evaluator result companion | Audit KEYS_ONLY plus result read | Exact evaluator result read | `planned` |
| `agent_quality_processing` | Audit stream key → findings/aggregate terminal | Audit/findings table | Existing KEYS_ONLY stream + terminal companion | Exact audit/finding read | `planned` |

The registry also declares Campaign parent, ordered-segment, displayed-detail, live-tail-detail, and
metric-focus routes, plus Retail Health parent and metric-focus routes, as distinct
`synchronous_not_movement` measurement surfaces. Their authority is the authenticated response bound to
the exact artifact pointer/revision or declared live-detail evidence. They are excluded from
asynchronous coverage counts, included in observation-only route baselines, and eligible for route SLO
evaluation only after objective activation.

A generated infrastructure inventory extracts EventBridge rules/targets, workload SQS event sources and
DLQs, DynamoDB stream consumers, scheduled rules, and registered continuation/enqueue transports from
synth. It separately requires exactly one dedicated status FIFO queue, its dedicated DLQ, and its
projector event-source mapping. `make check-data-flow-coverage` fails when either side has an unmatched
entry or when an EventBridge target appears between a status producer and the status queue. Existing S3
EventBridge ingestion remains a registered workload route. An intentionally excluded synchronous
request path must be declared `synchronous_not_movement` with an owning capability; it cannot be
omitted. A registry entry remains visible as incomplete until `active` or `external_boundary`.

### 6.2 Identity and event contract

All public lifecycle identities use canonical lowercase hyphenated UUID text. Their meanings are closed:

- `flow_id`: one logical movement activity from observed start through authority-backed terminal state.
- `execution_id`: one attempt to advance that flow; a retry creates a new value.
- `predecessor_execution_id`: the immediately previous attempt for the same flow, absent only on the
  first execution.
- `event_id`: one immutable transition. A persisted companion may allocate it before commit; a
  re-attesting adapter derives it as UUIDv5 from the contract namespace, internal authority digest,
  transition sequence, and state so reconciliation reproduces it.
- `predecessor_event_id`: the immediately previous transition in the same flow, absent only on the root
  event. It is the optimistic projection fence.
- `parent_flow_id`: the enclosing logical flow for topology when one transition creates separately
  tracked work; absent only for root or external-boundary flows. A topology link does not itself enroll
  that flow in the parent's child-reduction or counting contract.
- `causation_event_id`: the exact parent-flow event that created this flow or triggered this transition;
  absent only when no repository-owned causation exists.

New owner interfaces normally allocate UUIDv4 `flow_id` and `execution_id` values before the
authoritative transaction and persist them atomically in authority or its content-free companion/outbox.
Retry preserves `flow_id`, allocates a new `execution_id`, and persists `predecessor_execution_id`.
Deterministic UUIDv5 allocation is permitted only for an explicitly named owner interface whose
versioned canonical tuple and UUID namespace are owned by this contract and whose authoritative
transaction atomically persists the derived identities. `DynamoStage1LifecycleHeadStore` is that named
exception for the Stage-1 plan, unit, inline-run, and publication-pointer tuples below. An adapter over a pre-existing
authority that cannot persist a new identity may also derive stable UUIDv5 values from the versioned
contract namespace and an internal one-way authority digest. No unnamed owner, projector, or adapter may
choose a local tuple or namespace. Reprocessing and reconciliation preserve every previously persisted
or contract-derived identity. UUIDs identify the status model only; no API accepts one as a source,
provider, AWS, publication, or attempt selector.

```text
OperationalDataFlowEvent.v1 {
  schema_version: "1"
  registry_version: nonblank version
  event_id: UUID
  predecessor_event_id: optional UUID
  flow_id: UUID
  parent_flow_id: optional UUID
  causation_event_id: optional UUID
  execution_id: UUID
  predecessor_execution_id: optional UUID
  transition_sequence: positive integer
  surface: registered surface enum
  stage: nonblank owner stage
  state: common state enum
  outcome: optional succeeded | completed_no_change | completed_refused | exhausted | superseded
  occurred_at: UTC instant
  clock_started_at: optional UTC instant
  required_child_flow_ids: bounded sorted UUID tuple
  optional_child_flow_ids: bounded sorted UUID tuple
  progress: optional {completed, total, unit}
  authority: {kind, opaque_digest, observed_at}
  reason_code: optional nonblank code
  evaluation_binding: {mode: observation_only, exact registered measurement_refs}
}
```

This is the exact executable v1 event shape. Dependency-roster references, usage contributions, and
latency summaries are separate projection records; adding any of them to an event is rejected. The
separate usage record below carries resource quantities.

```text
OperationalDataFlowSurfaceObservation.v1 {
  schema_version: "1"
  registry_version: nonblank version
  observation_id: deterministic UUID
  surface: registered surface enum
  observed_at: UTC instant
  source_sequence: monotonic source observation token
  transport: optional {visible, in_flight, delayed, oldest_age_ms, retrying, dlq}
  compute: optional {running, errors, throttles}
  provider_latency: optional {count, total_ms, max_ms}
  evidence: complete | incomplete | stale | unavailable
  reason_code: optional registered reason enum
  open_operational_case_count: nonnegative integer
}
```

```text
OperationalDataFlowUsageContribution.v1 {
  schema_version: "1"
  registry_version: nonblank version
  contribution_id: UUID
  flow_id: UUID
  execution_id: UUID
  evaluation_binding: DataFlowEvaluationBinding.v1
  reconciles_disposition_id: optional UUID
  usage: UsageByCategory.v1 with exactly one populated category; ordinary contributions require a positive quantity, while a reconciliation delta contains only the disposition's formerly missing Athena fields and may carry exact zero
  observed_at: UTC instant
}

OperationalDataFlowUsageEvidenceDisposition.v1 {
  schema_version: "1"
  registry_version: nonblank version
  disposition_id: UUID
  flow_id: UUID
  execution_id: UUID
  evaluation_binding: DataFlowEvaluationBinding.v1
  category: productive | retry | duplicate_waste | no_change | refused | unavailable | superseded
  resource: athena
  known_usage: UsageUnits.v1 with athena.query_count exactly 1 and only exact currently known scanned_bytes/engine_ms
  missing_quantities: nonempty bounded tuple of scanned_bytes | engine_ms
  evidence: unavailable
  observed_at: UTC instant
}
```

Progress is monotonic within one execution and never advances on heartbeat. A root/planning authority
registers the original clock start and complete bounded required/optional child sets in the event; any
dependency-roster reference is retained in the separate observation/sample authority because it is not
an `OperationalDataFlowEvent.v1` field. Later transitions and usage contributions repeat the evaluation
binding byte-identically. Observation-only bindings reject objective references; evaluated bindings
require accepted objectives that each reference an exact measurement in the binding. A mode/objective
mismatch is an invalid message, never a projection fallback. Child sets may never be altered. Child
membership is declared only by the root's explicit child tuples and is never inferred from
`parent_flow_id` or `causation_event_id`; every declared child must name that parent in
`parent_flow_id`. Stage-1 unit and inline roots emit empty required and optional child tuples even though
pointer flows retain non-null topology links to them. Usage is never embedded in a lifecycle
event. Actual work sends a separate `OperationalDataFlowUsageContribution.v1`; work with a known
provider terminal but missing provider quantity sends the separate retained
`OperationalDataFlowUsageEvidenceDisposition.v1`. Both bind to the existing flow/measurements and any
activated objectives without creating a lifecycle transition or second objective sample.
`UsageUnits.v1`, `UsageByCategory.v1`, and the unavailable-evidence semantics are the exact closed
contracts owned by the shared concept; producers cannot add units or substitute currency. Usage fields contain operational units only, never monetary values or customer
identities. Generic payload/details, arbitrary strings, source selectors, infrastructure identifiers, raw provider messages, workload queue bodies, and content fields are
rejected.

### Stage-1 event projection

The Stage-1 `EVENT#` records and lifecycle heads owned by
[Load Path write discipline §§3.1 and 10](../load-path/write-discipline.md#31-owner-side-public-lifecycle-heads)
are internal authorities, not `OperationalDataFlowEvent.v1` documents. As the capability-wide named
UUIDv5 owner exception, `DynamoStage1LifecycleHeadStore` derives the public identities from the exact
versioned tuples and `operational-data-flow-v1` namespace below before each state transaction and
persists them atomically with state and outbox, plus a lifecycle head for sequenced plan, unit, and
inline-run flows. Publication-pointer roots persist the same proof in their one immutable event and need
no head. The lifecycle projector recomputes those expected
UUIDs from the immutable authority, verifies the persisted values and head-assigned
sequence/predecessors, and reduces them; it never allocates, adopts, or repairs identity and never
substitutes an internal operation, plan, publication, attempt, or SHA-256 identifier into a public UUID
field. Stage-1 compatibility is exact-contract compatibility: authorities without the persisted public
UUID/head proof, with a different tuple or namespace version, or with a derived/persisted mismatch are
rejected rather than upgraded in projection. The pre-switch rollout must prove no legacy Stage-1
lifecycle authority requires adoption. The three existing semantic surfaces remain the Stage-0 mapping with their IDs and meanings unchanged:
`semantic_source_materialization`, `semantic_candidate_publication`, and
`semantic_current_publication`. Stage 1 has exactly four registry/status-owned surfaces and no Stage-0
fallback or table-kind selection: every plan event binds `semantic_projection_plan`, every queued unit
event binds `semantic_projection_unit`, every inline run event binds `semantic_inline_table_run`, and
every servable-pointer event binds `semantic_servable_pointer`.

This document is the sole owner of the Stage-1 UUIDv5 name tuples. Let
`canonical_authority_bytes(x)=json.dumps(x, ensure_ascii=True, sort_keys=True,
separators=(",", ":"), allow_nan=False).encode("utf-8")`,
`canonical_authority_digest(x)=sha256(canonical_authority_bytes(x))`, and
`U(kind, parts...)=UUIDv5` using the registry's `operational-data-flow-v1` namespace and the same
unit-separator name encoding as the executable `derived_uuid` function. `canonical_json` below means
the ASCII JSON text decoded from `canonical_authority_bytes` and is not an alternate encoder. Object
keys are sorted lexicographically by the JSON encoder, arrays preserve input order, and JSON integers
use Python/JSON integer decimal rendering; booleans are booleans rather than integers and non-finite
numbers are rejected. `ensure_ascii=True` emits every non-ASCII code point as lowercase `\uXXXX` escapes
under the production encoder; supplementary code points use the corresponding UTF-16 high/low surrogate
pair (for example 😀 is `\ud83d\ude00`). No Unicode normalization occurs before escaping, so canonically
equivalent but code-point-distinct strings remain different. UTF-8 is applied only to the resulting
ASCII JSON text. `D(x)` below is exactly `canonical_authority_digest(x)`, never a strings-unchanged or
literal-non-ASCII variant. The following internal flow tuples are byte-exact and include their shown
discriminator; no owner or referencing document may add, omit, reorder, normalize, or substitute an
element:

```text
plan_tuple    = ["plan", plan_digest]
unit_tuple    = ["unit", plan_digest, unit_key]
inline_tuple  = ["inline_run", writer_kind, operation_id, physical_table_id]
pointer_tuple = [partition_identity, publication_id, fencing_token]
```

Owner-derived public identities are:

- plan flow `U("flow", "semantic_projection_plan", D(plan_tuple))`; each internal delivery
  execution selects plan execution `U("execution", plan_flow_id,
  D([internal_delivery_execution_id]))` through the plan lifecycle head. `internal_delivery_execution_id`
  is exactly the plan-materialization internal execution identity that `PersistPlanRequest` derives
  under [Load Path §3](../load-path/write-discipline.md#3-write-plan-record-and-state-machine) from
  `intent_id` plus `claimed_generation` and stores in the plan `HEAD` and
  `SUCCESSOR_OWNER.latest_internal_execution_id`; both origins (`source_delivery` and
  `plan_attempt_budget`) produce it through that one derivation, and no source-delivery redelivery
  execution, queue receipt, or operation identifier ever feeds this UUID;
- unit flow `U("flow", "semantic_projection_unit", D(unit_tuple))`; each writer receive selects unit
  execution `U("execution", unit_flow_id, D([writer_execution_id]))` through the unit lifecycle head;
- inline run flow `U("flow", "semantic_inline_table_run", D(inline_tuple))`; each `RUN#` record selects
  inline execution `U("execution", inline_flow_id, D([internal_execution_id]))` through the inline
  lifecycle head;
- publication-pointer flow `U("flow", "semantic_servable_pointer", D(pointer_tuple))`; pointer
  execution `U("execution", pointer_flow_id, D([internal_pointer_execution_id]))`, where the nonblank
  input and source kind are selected only by the closed matrix below; and
- event `U("event", surface, authority_digest, decimal_transition_sequence, state)`, exactly matching
  the registry event-name tuple. `authority_digest` has exactly one constructor over the immutable owner
  transition fields in the `EVENT#` payload, excluding the persisted public UUID proof and mutable
  outbox delivery/block fields:

  ```text
  event_authority_tuple = ["stage1-event-authority/v1", surface, internal_key,
    transition_id, [lifecycle_state, completion_outcome-or-null, typed_reason-or-null],
    authority_time]
  authority_digest = canonical_authority_digest(event_authority_tuple)
  ```

  `surface` is the family's fixed surface identifier. `internal_key` is the family's closed internal
  subject array: plan `[plan_key]`; queued unit `[plan_key, unit_key]`; inline run
  `[writer_kind, operation_id, physical_table_id, run_id]`; pointer
  `[partition_identity, publication_id, fencing_token]`. `transition_id` is the exact owner transition
  name from this section's mapping table (for example `semantic_unit_attested`). The outcome array
  carries the projected lifecycle state, the completion outcome or `null` when nonterminal, and the
  typed reason or `null` when the transition has none. `authority_time` is the 20-digit UTC
  epoch-microsecond owner authority time — the same durable instant that feeds `outbox_order`. The
  committed vector (fictional synthetic members, encoding/digest behavior only) is:

  ```text
  unit tuple JSON = ["stage1-event-authority/v1","semantic_projection_unit",["WRITE_PLAN#2222#exec-g7","db.campaigns"],"semantic_unit_attested",["completed","succeeded",null],"00001789200000000000"]
  unit tuple UTF-8 length = 178
  authority_digest = 504f6193e35e4826abe1a40ee6b4d9cb8ee266d23dda76f828d2ce7611adcd59
  ```

  Exact replay
  therefore recomputes and verifies every public UUID without exposing an internal selector. This
  identity digest is distinct from `immutable_payload_digest`, which is `canonical_authority_digest`
  over the exact complete stored payload document — for `EVENT#` the retained event document and for
  `USAGE#` the settled contribution or evidence-disposition document whose identity the completion/SLO
  concept owns — and which exact-replay payload equality ranges over.

Pointer execution identity is independent of topology. The owner EVENT persists this internal-only
closed source:

```text
PointerExecutionSource.v1 {
  internal_pointer_execution_source_kind:
    queued_unit_writer_execution | inline_run_execution | semantic_publication_attempt
  internal_pointer_execution_id: validated nonblank UTF-8 string
}
```

| Pointer owner path | Exact pointer execution source |
|---|---|
| Queued nonzero | `queued_unit_writer_execution`; exact UNIT `writer_execution_id` conditioned in the same writing→attested finalization. |
| Inline nonzero | `inline_run_execution`; exact immutable RUN `internal_execution_id`, which is the RUN tuple's existing `execution_id`. |
| Automatic zero-row/no-run | `semantic_publication_attempt`; exact `terminal_success.attempt_id.value` from the same complete validated history used for topology. |
| Transitional zero-row/no-run | Same `semantic_publication_attempt` rule. |
| Manual ingest zero-row/no-run | Same `semantic_publication_attempt` rule. |
| Reprocess zero-row/no-run | Same `semantic_publication_attempt` rule. |

The source kind and ID are immutable pointer-EVENT authority fields included in immutable payload
equality — they are covered by `immutable_payload_digest`, and the derived pointer execution UUID
independently pins the source pair, so recomputation mismatch still fails — while the event
`authority_digest` tuple deliberately carries no execution-source slot; both fields are excluded from
the public event/API, FIFO payload, spans, logs, metrics, and
status keys. Same-receive queued rebuild reuses its writer ID. A queued transaction abort with no event
or attestation permits a later legal reclaim and new writer ID. Inline replay of the same RUN reuses its
immutable ID; only a separately accepted RUN may differ while the pointer event remains absent. A
zero-row full abort with no terminal success/publication/event permits a new attempt. Once an event
exists, its source pair and derived execution UUID are authoritative forever through delivery and newer
pointers; recomputation mismatch fails.

Compatibility is byte equality against these exact arrays and constructors, not semantic similarity.
Contract fixtures pin canonical JSON bytes, `D` inputs, and resulting flow/execution/event UUID text for
all four tuples; they fail when a discriminator is missing, an element is reordered or differently
typed, the pointer tuple changes, another namespace/name encoder is used, or persisted proof differs
from recomputation. Referencing designs link here and do not restate these arrays.

One shared non-ASCII/map-order fixture pins the active helper before any constructor-specific fixture:

```text
value insertion order A = {"b":"é😀","a":["東京",1]}
value insertion order B = {"a":["東京",1],"b":"é😀"}
canonical ASCII bytes = {"a":["\u6771\u4eac",1],"b":"\u00e9\ud83d\ude00"}
UTF-8 length = 49
UTF-8 hex = 7b2261223a5b225c75363737315c7534656163222c315d2c2262223a225c75303065395c75643833645c7564653030227d
canonical_authority_digest = e5538692fcf9b5b794a29e642174ec8d27eb8027187a517d26432456c3dc599c
```

Both insertion orders must produce those exact bytes, length, hex, and digest. The owner-persistence and
projector-recomputation suites import this one vector and run it through the shared helper before their
flow-tuple, execution-source, event-authority, mapped-successful-attempt/topology-source, and
`[partition_key, record_key, immutable_payload_digest]` outbox-order fixtures. Each suite must fail if it
uses literal UTF-8, uppercase escapes, a single supplementary code point instead of the surrogate pair,
normalizes Unicode, reorders an array, preserves map insertion order, changes integer typing/rendering,
or accepts NaN/infinity. The namespace, tuple discriminators, event contract, and canonical encoder are
unchanged; the fixed Stage-1 surfaces and their derived UUID fixtures are part of this amendment.

The repository encoder's fictional plan, unit, and inline-run fixtures are byte-pinned. Each fixture
pins the canonical tuple bytes and hex, tuple digest, complete flow-name bytes, flow UUID, canonical
execution-source bytes and digest, complete execution-name bytes, and execution UUID:

```text
plan tuple JSON = ["plan","plan-fixture"]
plan tuple UTF-8 hex = 5b22706c616e222c22706c616e2d66697874757265225d
plan tuple digest = a42a667a3f95d72195e67a677f6e91e88bd0d1a04b9f2115a14b8ed8e44c7a30
plan flow-name UTF-8 hex = 6f7065726174696f6e616c2d646174612d666c6f772d76311f666c6f771f73656d616e7469635f70726f6a656374696f6e5f706c616e1f61343261363637613366393564373231393565363761363737663665393165383862643064316130346239663231313561313462386564386534346337613330
plan flow UUID = 68cb3192-c252-5fed-bbe8-8f0ab98f74b6
plan execution source JSON = ["delivery-execution-fixture"]
plan execution source digest = 3117dfa773bd243e0b9bebae22777c0824b6054a0439553b40e8de57fe350068
plan execution-name UTF-8 hex = 6f7065726174696f6e616c2d646174612d666c6f772d76311f657865637574696f6e1f36386362333139322d633235322d356665642d626265382d3866306162393866373462361f33313137646661373733626432343365306239626562616532323737376330383234623630353461303433393535336234306538646535376665333530303638
plan execution UUID = 76db826e-6da1-57ba-b170-75d627f053a3

unit tuple JSON = ["unit","plan-fixture","unit-fixture"]
unit tuple UTF-8 hex = 5b22756e6974222c22706c616e2d66697874757265222c22756e69742d66697874757265225d
unit tuple digest = e69f60fb12ae2063334958c723d5ff1df37e616108e35d71fc9d89f0f0e3178f
unit flow-name UTF-8 hex = 6f7065726174696f6e616c2d646174612d666c6f772d76311f666c6f771f73656d616e7469635f70726f6a656374696f6e5f756e69741f65363966363066623132616532303633333334393538633732336435666631646633376536313631303865333564373166633964383966306630653331373866
unit flow UUID = 960cfeca-0bad-57fd-b4b0-383e15d59085
unit execution source JSON = ["writer-execution-fixture"]
unit execution source digest = a9c44e642237b2ba312eb237cbab24828a2318c0e3f3a63c81307afd628108ab
unit execution-name UTF-8 hex = 6f7065726174696f6e616c2d646174612d666c6f772d76311f657865637574696f6e1f39363063666563612d306261642d353766642d623462302d3338336531356435393038351f61396334346536343232333762326261333132656232333763626162323438323861323331386330653366336136336338313330376166643632383130386162
unit execution UUID = ef642f3b-65f1-580f-b01b-e2b7102b98f9

inline tuple JSON = ["inline_run","ingest_delivery","operation-fixture","physical-table-fixture"]
inline tuple UTF-8 hex = 5b22696e6c696e655f72756e222c22696e676573745f64656c6976657279222c226f7065726174696f6e2d66697874757265222c22706879736963616c2d7461626c652d66697874757265225d
inline tuple digest = eae5ef239de0adb548e9b5cdba5bd2e4f1030be3347e8e8d4742cfb064de1479
inline flow-name UTF-8 hex = 6f7065726174696f6e616c2d646174612d666c6f772d76311f666c6f771f73656d616e7469635f696e6c696e655f7461626c655f72756e1f65616535656632333964653061646235343865396235636462613562643265346631303330626533333437653865386434373432636662303634646531343739
inline flow UUID = ac30cd31-fad5-5dcb-a821-fe91c54eafc0
inline execution source JSON = ["inline-execution-fixture"]
inline execution source digest = 2e4d0f1005a45f22ac86b4baeb44464377347678f6d719610dda093071109a55
inline execution-name UTF-8 hex = 6f7065726174696f6e616c2d646174612d666c6f772d76311f657865637574696f6e1f61633330636433312d666164352d356463622d613832312d6665393163353465616663301f32653464306631303035613435663232616338366234626165623434343634333737333437363738663664373139363130646461303933303731313039613535
inline execution UUID = 8a2fdf66-8d89-517a-88c2-7f09be54ef7b
```

The repository encoder's fictional pointer fixture is byte-pinned:

```text
pointer JSON = ["fictional.dataset|2026-09-19|whole","publication-fixture",7]
pointer digest = cbc1dc176f7b0c0070098053c58b227dcffd392fa0f17ca8673d1a59c2d7a01b
flow-name UTF-8 hex = 6f7065726174696f6e616c2d646174612d666c6f772d76311f666c6f771f73656d616e7469635f7365727661626c655f706f696e7465721f63626331646331373666376230633030373030393830353363353862323237646366666433393266613066313763613836373364316135396332643761303162
flow UUID = e4de2f19-58db-5f51-b924-b4e5118d4db3
```

Execution rows are `cell | kind | source JSON | source digest | execution-name UTF-8 hex | UUID`:

```text
queued_nonzero | queued_unit_writer_execution | ["writer-execution-fixture"] | a9c44e642237b2ba312eb237cbab24828a2318c0e3f3a63c81307afd628108ab | 6f7065726174696f6e616c2d646174612d666c6f772d76311f657865637574696f6e1f65346465326631392d353864622d356635312d623932342d6234653531313864346462331f61396334346536343232333762326261333132656232333763626162323438323861323331386330653366336136336338313330376166643632383130386162 | bf1bc15f-7b72-54db-8c7a-50682ce6810e
inline_nonzero | inline_run_execution | ["inline-execution-fixture"] | 2e4d0f1005a45f22ac86b4baeb44464377347678f6d719610dda093071109a55 | 6f7065726174696f6e616c2d646174612d666c6f772d76311f657865637574696f6e1f65346465326631392d353864622d356635312d623932342d6234653531313864346462331f32653464306631303035613435663232616338366234626165623434343634333737333437363738663664373139363130646461303933303731313039613535 | 6f03579d-6e36-5f49-a906-e69c979df18e
automatic_zero | semantic_publication_attempt | ["attempt-automatic-fixture"] | bd5b9376b1c00b75cb8554187f3a8ba6ee3a6b5a6a3b697addbe0d3217062d3c | 6f7065726174696f6e616c2d646174612d666c6f772d76311f657865637574696f6e1f65346465326631392d353864622d356635312d623932342d6234653531313864346462331f62643562393337366231633030623735636238353534313837663361386261366565336136623561366133623639376164646265306433323137303632643363 | 7251cadf-6717-57b7-836e-fe92905d346c
transitional_zero | semantic_publication_attempt | ["attempt-transitional-fixture"] | 750142b0d1aebfa52015559a2f84db231a840f7600525aab19090db2096528d4 | 6f7065726174696f6e616c2d646174612d666c6f772d76311f657865637574696f6e1f65346465326631392d353864622d356635312d623932342d6234653531313864346462331f37353031343262306431616562666135323031353535396132663834646232333161383430663736303035323561616231393039306462323039363532386434 | 816f564b-1294-5293-939f-536f6ba247cf
manual_zero | semantic_publication_attempt | ["attempt-manual-fixture"] | 9295fcec957ca1679a2b85a1af21b67b7c1b11bdbcd440504173f6aa025e36a0 | 6f7065726174696f6e616c2d646174612d666c6f772d76311f657865637574696f6e1f65346465326631392d353864622d356635312d623932342d6234653531313864346462331f39323935666365633935376361313637396132623835613161663231623637623763316231316264626364343430353034313733663661613032356533366130 | 9b38b5d0-eb39-5686-ae6a-0893727e3a04
reprocess_zero | semantic_publication_attempt | ["attempt-reprocess-fixture"] | 7da7945e1507b813e8d4bcedd29290489b207381bffaf409778730a3e2048a93 | 6f7065726174696f6e616c2d646174612d666c6f772d76311f657865637574696f6e1f65346465326631392d353864622d356635312d623932342d6234653531313864346462331f37646137393435653135303762383133653864346263656464323932393034383962323037333831626666616634303937373837333061336532303438613933 | 1a45fed2-aada-5d0c-bdfa-f22b1874c34f
```

Negative fixtures independently change one UTF-8 byte, source kind, namespace, tuple order, or unit-
separator name encoding and must change the digest/name/UUID or fail validation. The four flow families
are a closed tagged product: each plan tuple bound to any surface other than
`semantic_projection_plan`, each unit tuple bound to any surface other than
`semantic_projection_unit`, each inline tuple bound to any surface other than
`semantic_inline_table_run`, and each pointer tuple bound to any surface other than
`semantic_servable_pointer` is a cross-family binding failure before persistence. The twelve wrong
family/surface pairs are rejection fixtures, not alternate accepted UUIDs. Persisted drift is never
adopted.

#### Stage-1 durable authority keys and lifecycle heads

This document also solely owns Stage-1 durable lifecycle key construction in the retained publications
table; [Load Path §3.1](../load-path/write-discipline.md#31-owner-side-public-lifecycle-heads) consumes
these keys through its opaque proof and may not reproduce them. The constructors are byte-exact:

- Every plan, queued-unit, and inline-run flow has exactly one lifecycle head at
  `partition_key = STATUS_FLOW#<flow UUID text>`, `record_key = HEAD`, where `<flow UUID text>` is the
  canonical lowercase hyphenated text of that flow's `U("flow", ...)` value above.
- Every plan, queued-unit, and inline-run lifecycle event is retained in that same `STATUS_FLOW#`
  partition at `record_key = EVENT#<authority_digest>`, where `authority_digest` is exactly the event
  digest defined above, rendered as 64 lowercase hex characters.
- Every publication-pointer event is retained under its exact publication partition — the same
  dataset-prefixed `partition_key` that holds the publication's `CURRENT` authority — at
  `record_key = EVENT#<authority_digest>`. Pointer flows are headless and never create a `STATUS_FLOW#`
  partition or `HEAD` record.

Because `authority_digest` covers only the immutable owner transition fields, each event key is
deterministic before its transaction and exact replay targets the same key. The producer resolves
retained authority from the base-table keys its stream image or `KEYS_ONLY` index entry already
carries. The projector's join is wire-derived for the three `STATUS_FLOW#` families: for every plan,
queued-unit, and inline-run lifecycle event the public member's
`authority.opaque_digest` carries exactly this identity `authority_digest`, so the projector derives
`STATUS_FLOW#<flow_id>` / `EVENT#<authority.opaque_digest>` from the received member alone,
without any scan, index cursor, or internal-selector disclosure. A `current_servable_pointer` member
never needs this reload: pointer events are headless single-transition roots outside child reduction
and plan gating, so the projector reduces them from the received member through the ordinary applied
event path with no retained-authority read; their `authority.opaque_digest` carries the same identity
digest for recomputation only. This same join is the approved
retained-authority source for Stage-1 projection dispatch: the public wire deliberately omits
`owner_generation` and `required_child_expectations`, so the projector derives the retained key from
the received member, strongly reads the
retained authority for required-unit terminals and plan terminals, re-attests it under this section's
recomputation rules — including recomputing the
identity digest from the retained internal fields and rejecting any mismatch with the wire value — and
only then constructs
its internal child-outcome or plan-terminal request.

The lifecycle `HEAD` item's complete closed field set is exactly:

```text
head_contract_version          = 1
surface                        = the flow's fixed family surface identifier
flow_id                        = public flow UUID, byte-equal to the partition-key suffix
current_execution_id           = public execution UUID currently selected by this head
current_internal_execution_id  = the exact internal execution selector whose D([...]) feeds
                                 current_execution_id: the plan-materialization internal execution
                                 identity for plans, writer_execution_id for queued units, and the RUN
                                 internal_execution_id for inline runs
predecessor_execution_id       = public execution UUID superseded by current_execution_id, or null for
                                 the flow's first execution
latest_event_id                = public event UUID of the newest applied owner transition
latest_authority_digest        = that event's authority_digest
latest_transition_sequence     = JSON integer; the first event is exactly 1 and every successor is
                                 exactly the predecessor plus one
latest_state                   = that event's projected lifecycle state
parent_flow_id                 = public parent flow UUID or null, copied byte-for-byte from the flow's
                                 root topology source and immutable thereafter
causation_event_id             = public causation event UUID or null, immutable with parent_flow_id
```

The "complete opaque previously read head tuple" conditioned by every Load Path §3.1 head transaction,
head byte-equality replay, and compound `N→N+2` replacement all range over exactly these fields; no
additional attribute exists on the head item. Execution succession is decided only here: when an
incoming transition's internal execution selector equals `current_internal_execution_id`, the persisted
public execution is reused; when it differs, the new public execution is derived from the tuple above
and the superseded value moves to `predecessor_execution_id`. The head assigns each new event
`transition_sequence = latest_transition_sequence + 1` and
`predecessor_event_id = latest_event_id` before dispatch; the root event has sequence `1` and no
predecessor.

#### Stage-1 topology source

`stage1-topology-v1` is the sole rule for Stage-1 `parent_flow_id` and `causation_event_id`. Topology is
independent of root child-set declaration: a non-null relationship identifies lineage but never by
itself authorizes child-outcome reduction, generation/latest records, counts, measurements, objectives,
or migration. Absence is never itself proof that a flow is a root. Every owner transaction constructs
exactly one closed source:

```text
Stage1TopologySource.v1 {
  topology_contract: "stage1-topology-v1"
  topology_source_kind: legitimate_root | prior_owner_event | plan_admission |
    same_transaction_terminal | semantic_attempt_success
  root_discriminator: optional automatic_ingestion_plan |
    transitional_delivery_inline | ingest_delivery_operator_inline |
    reprocess_semantic_v2_operator_inline | graph_unit_inline  # one per execution set
  parent_flow_id: optional UUID
  causation_event_id: optional UUID
  topology_source_digest: optional 64-lowercase-hex SHA-256
}
```

The five variants have exact required and forbidden fields:

| Variant | Required fields and authority | Forbidden fields |
|---|---|---|
| `legitimate_root` | exactly one root discriminator; both relationship fields absent | source digest and every source proof |
| `prior_owner_event` | `causation_event_id=head.latest_event_id`; `parent_flow_id` copied byte-for-byte from the complete old owner head and therefore possibly absent; `topology_source_digest=canonical_authority_digest(["stage1-topology-prior-owner-event/v1", <complete old owner head authority>])` | root discriminator, any caller-selected relationship, partial/projected head |
| `plan_admission` | `parent_flow_id` is the exact plan flow; `causation_event_id` is the exact `semantic_plan_admitted` event; `topology_source_digest=canonical_authority_digest(["stage1-topology-plan-admission/v1", <exact plan META authority>, <complete plan head authority>, <exact semantic_plan_admitted event authority>])` | root discriminator, absent relationship, non-admitted/wrong-plan event, partial plan proof |
| `same_transaction_terminal` | both relationship UUIDs from the exact terminal owner event created in the same transaction | root discriminator, source digest, prior-transaction event |
| `semantic_attempt_success` | both relationship UUIDs from the exact successful attempt event and `topology_source_digest=canonical_authority_digest(["stage1-topology-semantic-attempt-success/v1", <complete validated mapped successful-attempt authority>])` | root discriminator, partial history, non-success terminal |

The constructor consumes the complete authority named in its row and derives every persisted value;
callers supply none of the relationship UUIDs or digests independently. Unknown kinds/discriminators,
missing required authority, any forbidden or cross-variant field, digest mismatch, or caller-selected UUID
fails before mutation. Parent and causation are jointly absent only for `legitimate_root`; only
`prior_owner_event` may have absent parent with present causation. A future durable delivery parent
requires a separately versioned topology contract and migration.

The constructive source matrix is exact:

| Stage-1 owner transition | Required topology source |
|---|---|
| First automatic queued `semantic_plan_recorded` | `legitimate_root` with `automatic_ingestion_plan`. |
| Later/compound-successor `semantic_plan_recorded`, plus every `semantic_plan_admitted`, `semantic_plan_admission_failed`, and `semantic_plan_terminal` | `prior_owner_event` from the complete old plan head. |
| First transitional automatic, manual `ingest_delivery`, `reprocess_semantic_v2`, or GRAPH `write_unit` execution-set inline `semantic_inline_run_recorded` | `legitimate_root` with respectively `transitional_delivery_inline`, `ingest_delivery_operator_inline`, `reprocess_semantic_v2_operator_inline`, or `graph_unit_inline`. |
| Every later inline event, including a later recorded event in the same flow | `prior_owner_event` from the complete old inline head. |
| First `semantic_unit_started` for a unit flow | `plan_admission` from exact plan `META`, complete plan head, and exact `semantic_plan_admitted` event. |
| Every later unit event, including retry `semantic_unit_started` | `prior_owner_event` from the complete old unit head. |
| Queued nonzero `semantic_projection_servable` | `same_transaction_terminal`: parent is the current unit flow and causation is the exact `semantic_unit_attested` event created in the same finalization transaction. |
| Inline nonzero `semantic_projection_servable` | `same_transaction_terminal`: parent is the current run flow and causation is the exact `semantic_inline_run_terminal` event created in the same finalization transaction. |
| Any zero-row/no-run pointer from automatic, transitional, manual, or reprocess publication | `semantic_attempt_success` from complete validated append-only attempt history ending in the exact successful event bound to the same publication attempt, partition, and published instant. |

For `prior_owner_event`, the topology constructor and head-transition constructor consume the same
complete old-head proof but derive independent fields: topology derives parent/causation/digest, while
the head transition derives next sequence, `predecessor_event_id`, execution reuse/succession, and
`predecessor_execution_id`. Neither accepts the other's outputs as authority. For `plan_admission`, a
first unit head is absent; its topology comes only from the exact plan proof, while its predecessor event
remains absent as required for the unit flow root.

For the zero-row/no-run row, the typed source observation is the complete ordered authority consumed by
the existing `_attempt_groups`/`map_attempt_history` semantics. That append-only attempt history—not the
status projection—is authoritative. Its terminal successful event's persisted public `flow_id` and
`event_id` become parent and causation. Missing, truncated, reordered, partial, wrong-terminal,
publication-attempt/partition/published-instant-mismatched, or derived/persisted UUID/digest-mismatched
history fails before any owner, pointer, event, or outbox mutation; it never falls back to a root. Replay
must reproduce the exact source kind, discriminator/relationships, and required/forbidden digest bytes.

For ordinary plan, unit, and inline transitions, the head chooses whether the current internal
execution reuses the persisted public execution or creates a predecessor-linked one, assigns the next
sequence and predecessor event, and persists those values before dispatch. Planned-predecessor plan
replacement is the sole compound transition: from an old head at sequence `N`, one owner transaction
creates the predecessor's terminal event at `N+1` and the successor's recorded event at `N+2`, then
moves the head directly to the latter. The terminal event remains in the predecessor public execution
and repeats that execution's existing `predecessor_execution_id`; the recorded event uses the new
successor public execution and names the superseded execution as `predecessor_execution_id`. Its
`predecessor_event_id` is the terminal event, whose predecessor is the old head event. All later events
in the successor execution repeat that execution predecessor. Pointer flows are one owner-created root
event and require no head. The projector validates the compound pair and final-head proof as one owner
commit; it rejects one event without the other, a derived/persisted mismatch, absent proof, branch,
changed immutable identity/payload/predecessor, or occupied sequence collision rather than allocating
or repairing either execution.

`plan_digest` is stable across delivery retries, so they remain executions of one plan flow. `unit_key`
is stable for one physical table obligation, so redelivery remains one unit flow. One inline logical
operation/table is one flow across exact replay and predecessor-linked CLI/reconciler executions. Table
kind remains authority data but never selects a Stage-1 unit or inline-run surface: queued units always
use `semantic_projection_unit` and inline runs always use `semantic_inline_table_run`. A new
publication/token is a new pointer flow. The durable event's transition version is the public
`transition_sequence`; the adapter requires it to start at one and be contiguous per public flow,
derives `predecessor_event_id` from the immediately preceding immutable event, and refuses a branch or
occupied sequence slot. A new execution names the immediately preceding public execution; events in the
same execution repeat its predecessor-execution value. The status reducer may park an out-of-order
successor but never invent a predecessor.

The registered-surface and disposition mapping is closed:

| Stage-1 family | Public flow/surface | Stage; state/outcome | Parent and causation |
|---|---|---|---|
| `semantic_plan_recorded` | plan; `semantic_projection_plan` | `projection_plan_recorded`; `queued` | First plan-flow event uses `stage1-topology-v1` `legitimate_root` with `root_discriminator=automatic_ingestion_plan` and both relationships absent. A later/compound successor recorded event uses `prior_owner_event` from the complete old plan head. In planned-predecessor replacement it is compound sequence `N+2`, uses the new successor execution, names the superseded execution as `predecessor_execution_id`, and names the `N+1` terminal event as `predecessor_event_id`. Root required children are sorted unit-flow UUIDs; internal projection also reattests the generation expectations defined in §6.3. |
| `semantic_plan_admitted` | same plan | `projection_plan_admitted`; `queued` | `prior_owner_event`: parent copied exactly from the old plan head (possibly absent), causation is that head's latest event, and predecessor derives separately from the same proof. |
| `semantic_plan_admission_failed` | same plan | `projection_plan_admission_failed`; `failed/exhausted`, reason `plan_admission_failed` | `prior_owner_event` from the complete old plan head. |
| `semantic_unit_started` | unit; `semantic_projection_unit` | `projection_unit_claimed`; `running`; progress `0/1 table_unit` | First claim uses `plan_admission`: parent is the plan flow and causation is exact `semantic_plan_admitted`, while the new unit head has no predecessor event. Each retry claim uses `prior_owner_event` from the complete old unit head. Every unit root declares empty `required_child_flow_ids` and `optional_child_flow_ids`; pointer flows are topology-linked independent flows, not children. Source/candidate roots likewise declare none. Every unit event carries internal `owner_generation` from the exact unit owner payload. |
| `semantic_unit_lease_acquired` | same unit | `projection_unit_lease_acquired`; `running`; progress unchanged | `prior_owner_event` from the complete old unit head. |
| `semantic_unit_refused` | same unit | `projection_unit_refused`; `retrying`, no outcome; reason `table_busy` or `table_attempt_pending` | `prior_owner_event` from the complete old unit head. Refusal is nonterminal and cannot satisfy a child. |
| `semantic_unit_attested` | same unit | `projection_unit_attested`; `completed/succeeded`; progress `1/1 table_unit` | `prior_owner_event` from the complete old unit head. Independently visible pointer flows retain exact topology but do not enter this unit's child history, heads, selectors, counts, measurements, objectives, release, or migration. |
| `semantic_unit_failed` | same unit | `projection_unit_failed`; `failed/exhausted`, typed unit reason exactly one of `statement_retries_exhausted`, `plan_attempt_budget_exhausted`, `checksum_mismatch`, `verification_mismatch`; no other value is stored, mapped, or served | `prior_owner_event` from the complete old unit head. |
| `semantic_inline_run_recorded` | inline run; `semantic_inline_table_run` | `inline_run_open`; `running`; progress `0/1 table_run` | First flow event uses `stage1-topology-v1` `legitimate_root` selected only from owner writer kind: transitional automatic → `transitional_delivery_inline`; manual `ingest_delivery` → `ingest_delivery_operator_inline`; reprocess → `reprocess_semantic_v2_operator_inline`; GRAPH `write_unit` (writer kind `graph_unit`, one flow per execution set of a unit batch, not parented to the graph run's root flow; each member node's effect record carries its set's flow ID) → `graph_unit_inline`. Both relationships are absent. Any later recorded event in that flow uses `prior_owner_event`. Every inline root declares empty `required_child_flow_ids` and `optional_child_flow_ids`; pointer flows are topology-linked independent flows, not children. |
| `semantic_inline_run_cancel_requested` | same inline run | `inline_run_cancel_requested`; `running`, no outcome | `prior_owner_event` from the complete old inline head; request is not terminality. |
| `semantic_inline_run_refused` | same inline run | `inline_run_refused`; `retrying`, no outcome; reason `table_busy` or `table_attempt_pending` | `prior_owner_event` from the complete old inline head. |
| `semantic_inline_run_reconciled` | same inline run | `inline_run_reconciled`; `retrying`, no outcome; reason is exact provider terminal | `prior_owner_event` from the complete old inline head. This is resumable provider-attempt reconciliation, not pointer/run terminality. |
| `semantic_inline_run_terminal` | same inline run | success → `completed/succeeded`; exact/no-change → `completed/completed_no_change`; refused → `completed/completed_refused`; unavailable/failed/cancelled → `failed/exhausted`; superseded → `superseded/superseded` | `prior_owner_event` from the complete old inline head. Current-table terminal creation is atomic with pointer outcomes. |
| queued `semantic_projection_servable` | one pointer flow per pointer outcome; `semantic_servable_pointer` | advance/zero-row seed → `projection_servable`, `completed/succeeded`; equal/newer pointer → `projection_superseded`, `superseded/superseded` | `stage1-topology-v1` joined source: parent is the current-unit flow and causation is the exact `semantic_unit_attested` event created by the same finalization transaction; `topology_source_kind=same_transaction_terminal`. It is the root transition of its own pointer flow; the relationship does not declare it as a unit child. |
| inline `semantic_projection_servable` | pointer flow; `semantic_servable_pointer` | same pointer disposition | `stage1-topology-v1` joined source: parent is the inline current-run flow and causation is its exact `semantic_inline_run_terminal` event from the same finalization transaction; `topology_source_kind=same_transaction_terminal`. It is not an inline-run child. |
| zero-row `semantic_projection_servable` | pointer flow; `semantic_servable_pointer` | same pointer disposition | `stage1-topology-v1` requires `topology_source_kind=semantic_attempt_success`: parent/causation and source digest derive from the complete validated append-only attempt history ending in the exact successful event bound to the same publication attempt, partition, and published instant. Missing or mismatched history fails; no synthetic plan/unit/run relation and no root fallback is permitted. |
| `semantic_plan_terminal` | same plan | completed → `projection_plan_terminal`, `completed/succeeded`; superseded → `projection_plan_terminal`, `superseded/superseded` | `prior_owner_event` from the complete old plan head. Ordinary completion derives predecessor separately from that same proof. In planned-predecessor replacement, the superseded terminal is compound sequence `N+1`, remains in the predecessor execution, preserves that execution's existing `predecessor_execution_id`, and names the old head event as `predecessor_event_id`; the atomic `N+2` successor-recorded event immediately follows it. Plan completion waits for its generation-bound required unit children. |

For every row, `occurred_at` and `clock_started_at` come from durable owner timestamps, never
projection time; retries preserve the root clock. The root transition's clock is its own
`occurred_at`; every successor transition reads the clock back from the flow's retained root
`EVENT#` payload (a strongly consistent owner query of its own `STATUS_FLOW#` partition, authorized
per Load Path write discipline §3.2) and repeats it byte-for-byte — the closed `HEAD` item
deliberately carries no clock field. `authority.kind` is `write_plan_meta`,
`write_plan_unit`, `inline_table_run`, or `current_servable_pointer`; for every Stage-1 lifecycle event
`authority.opaque_digest` is exactly the event's identity `authority_digest` defined in §6.2 — the same
one-way value already embedded in the public event UUID name, so the wire discloses nothing new — and
never the whole-payload `immutable_payload_digest`, which remains internal to the retained item and its
outbox/case machinery. On first projection, the dispatcher uses only the typed
`RecordStage1AuthorityObservationRequest` to conditionally set `authority_observed_at` once on the
internal outbox, reads it back, and uses that immutable instant for `authority.observed_at`; delivery-
envelope replay may update attempts/timestamps but never this field.
The evaluation binding is reconstructed byte-for-byte from the fixed family surface: plan events carry
`stage1_plan_settlement_latency@1`, queued-unit events carry `stage1_unit_settlement_latency@1`, inline-
run events carry `stage1_inline_run_settlement_latency@1`, and servable-pointer events carry
`stage1_servable_pointer_decision_count@1`, all in observation-only mode. Table kind cannot alter the
binding, and a tuple, authority kind, event family, or persisted UUID presented with any other Stage-1
surface is rejected as a cross-family binding failure. A missing/mismatched
surface, timestamp, binding, predecessor, `stage1-topology-v1` source kind/root discriminator/source
digest, parent, causation, or public UUID is a rejected message, not a best-effort projection. Every
mapping row must construct the exact source-matrix variant. Parent and causation are jointly absent only
for a persisted legitimate-root discriminator; `prior_owner_event` copies the old head's parent exactly
and may therefore carry absent parent with required causation; the other three non-root variants require
both relationships. Projection never converts missing required source authority into a root.

One internal finalization may therefore project one unit event plus multiple independent pointer-flow
events. The terminal owner event, pointer authority, pointer `EVENT#`, and outbox entries still commit
atomically; that co-commit establishes exact causation without declaring pointer child membership.
Pointer flows preserve their headless identity and lifecycle projection but never consume child-reducer
transaction capacity. The producer sends each public event with `MessageGroupId=flow_id` and
`MessageDeduplicationId=event_id`; status usage uses its separate contribution ID. The DynamoDB event
record remains the durable idempotency authority after FIFO deduplication expires. Exact duplicate
internal outbox delivery emits no second lifecycle event, while a real retry/refusal has a new public
execution/event and remains visible. The owner also supplies the same persisted public identities to
the registered spans and shared structured-log seam: unit records/spans use the unit flow, inline
records/spans use the run flow, and `semantic_plan_terminal` plus
`TABLE_WRITER_PLAN_TERMINAL` use the plan flow. A lexical unit→plan-terminal trace parent never changes
public `parent_flow_id`; each API flow UUID locates only its corresponding history, traces, and logs.

#### Stage-1 lifecycle delivery runtime

`DataV2Stack` adds one sparse GSI to the retained publications table before the Stage-1 pre-switch:

```text
index name = stage1-outbox-pending
partition key = outbox_status (String; the only indexed value is "pending")
sort key = outbox_order (String)
projection = KEYS_ONLY
```

`KEYS_ONLY` projects exactly the table `partition_key` and `record_key` plus the two index keys; no
payload, digest, status identity, usage quantity, source selector, or customer content is projected.
Every Stage-1 `EVENT#` and `USAGE#` authority has
`outbox_state ∈ {pending, delivered, blocked}`. Only `pending` items carry both sparse-index attributes:
`outbox_status="pending"` and
`outbox_order=<20-digit UTC epoch-microsecond authority time>#<64-lowercase-hex
canonical_authority_digest([partition_key, record_key, immutable_payload_digest])>`. The durable owner occurrence
time is used, never dispatcher time, so replay is byte-identical and equal-time items are ordered by an
opaque digest. For an `EVENT#` authority that owner time is the event's immutable authority timestamp;
for a `USAGE#` contribution or evidence disposition it is exactly the settled disposition's `observed_at`
instant persisted by the owner settlement transaction, never the settlement transaction's own clock read,
the provider terminal instant, or a dispatcher instant. Delivery changes `outbox_state` to `delivered`; poison closure changes it to `blocked`.
Both terminal transitions remove `outbox_status` and `outbox_order`, so neither state remains in the
sparse index. Delivered and blocked authorities retain their immutable payload and complete delivery or
blocking facts indefinitely; other records never populate the index.

The DataV2 pre-switch deployment must create this exact index and read it back as `ACTIVE` before any
amended Stage-1 authority writer or `Stage1LifecycleProducer` event-source/schedule is enabled. This is
one replacement path: no full-table lifecycle scan or operational-status control cursor is deployed in
parallel or retained for rollback.

`OperationalStatusProcessorStack` deploys one `Stage1LifecycleProducer` Lambda. It reads the existing
typed `semanticV2PublicationsStreamArn` parameter, sends to the stack's existing status FIFO, and owns
both stream delivery and scheduled sparse-index recovery for the complete Stage-1 outbox set. Both
producer triggers render their `Enabled` state from the
[Load Path §2.5 `stage1RecoveryEnabled` enablement contract](../load-path/write-discipline.md#25-stage-1-rollout-order);
no other control exists. These are
the only Stage-1 status-delivery entry points. Phase 1 never directly invokes this producer, waits for a
delivery response, or polls status before queue send/admission. This is not the provider-attempt reconciler in the write-
discipline design: that worker queries only the `table-attempt-reconcile` sparse GSI, strongly re-reads
exact nonterminal `FENCE` authority, calls Athena, and clears provider fences. `Stage1LifecycleProducer`
never calls Athena, and no separately deployed usage producer exists.

One pure `deliver_stage1_authority(DeliverStage1AuthorityRequest)` domain use case accepts a closed
tagged authority kind: `lifecycle_event` requires an `EVENT#` item and maps it to exactly one
`OperationalDataFlowEvent.v1`; `usage_contribution` requires a fully owner-disposition-settled Athena
`USAGE#` contribution authority and maps it to exactly one
`OperationalDataFlowUsageContribution.v1`; and `usage_evidence_disposition` requires a fully settled
Athena `USAGE#` evidence authority and maps it to exactly one
`OperationalDataFlowUsageEvidenceDisposition.v1`. A Stage-1 contribution with
`reconciles_disposition_id`, any Stage-1 delta authority, prefix/kind disagreement, unknown kind,
unsettled terminal attempt state, non-Athena Stage-1 authority, or a payload that validates as more than
one member is rejected. The producer never derives a category from provider terminality. The use case
derives the exact public queue member, FIFO IDs, and conditional delivery-mark request. It never
converts usage into a lifecycle event or allocates a second contribution/disposition identity.

The adapter handler has a closed tagged invocation mode: a DynamoDB `Records` envelope is `stream`; an
EventBridge scheduled event from this function's named rule is `reconcile`; a mixed or unknown shape is
rejected. Both modes strongly read each exact base-table authority and invoke the same use case and
delivery-state adapter—there is no second mapping or delivery path. `EVENT#` reads additionally verify
owner-assigned public UUIDs, ordering, and immutable digest, invoke only
`RecordStage1AuthorityObservationRequest` to persist the first observation instant, and strongly reread
it before reconstructing the event. `USAGE#` reads preserve the owner-
disposition settlement's `observed_at`, category, known/missing quantities, and contribution/disposition
identity byte-for-byte; they do not acquire a lifecycle observation instant or derive category from the
attempt's provider-terminal record.

The publications event-source mapping starts at `LATEST`, uses `NEW_IMAGE`, reports partial batch
failures, and filters to `INSERT|MODIFY` records whose `NewImage.outbox_status.S` is exactly `pending`
and whose `NewImage.record_key.S` has prefix `EVENT#` or `USAGE#`. The handler additionally requires the
closed `authority_kind` to match that prefix. The image is only a trigger: before sending, the producer
strongly reads the exact key and validates the immutable payload/digest and all three outbox states.
Ignored nonmatching, already-delivered, and already-blocked records succeed without a send. A transient
dependency failure below threshold returns its DynamoDB `SequenceNumber` in `batchItemFailures`;
success, successful immediate block, and threshold-reaching block do not, so later batch records remain
processable.

The scheduled mode runs from one rate rule every minute with reserved concurrency one and processes at
most the typed positive `STAGE1_LIFECYCLE_RECONCILE_BATCH_SIZE`. Each run issues one eventually
consistent `Query` to `stage1-outbox-pending` with `outbox_status="pending"`,
`ScanIndexForward=true`, and `Limit=<batch size>`, beginning at the pending partition's first key. It
never supplies or persists an `ExclusiveStartKey` or `LastEvaluatedKey`. For each projected key in order,
the producer strongly reads the base item and invokes the same delivery path. A success leaves the
sparse index as `delivered`; a poison authority leaves it as `blocked`; in either case the producer
continues with later candidates already returned by that query. A transient dependency failure below
the configured blocking threshold leaves the authority pending and stops that scheduled run, preserving
ordered retry pressure without allowing a permanently invalid authority to stall unrelated later
outboxes.

GSI reads are eventually consistent. A newly pending authority may therefore be absent from one
scheduled query, and a delivered or blocked authority may remain briefly as a stale index entry. These
cases cause delay or a terminal-state no-op only: the stream remains a prompt path, every candidate is
strongly read from the base table, a stale entry cannot resend a terminal item, and the next scheduled
run again queries from the pending start. Eventual consistency is never authority for payload or
delivery state and cannot cause a pending item to be delivered or blocked without its typed conditional
update.

For any pending member, the producer sends the exact closed queue member first. Only after send success
does the typed delivery-mark operation conditionally require the same key, immutable digest, authority
kind, `outbox_state="pending"`, `outbox_status="pending"`, and exact `outbox_order`; it removes the two
sparse-index attributes, sets `outbox_state="delivered"` and `delivered_at`, and preserves the immutable
payload. For an item carrying the exact builder-owned generation-specific `requeued_from_block={case_ref,
affected_surfaces}` marker, the same transaction conditions that complete marker byte-for-byte, requires
each named surface aggregate's `requeued_pending` count to be positive, decrements it once, removes the
marker, and advances each aggregate source sequence. A missing, changed, stale-generation, malformed,
underflowing, or partial aggregate snapshot fails closed with no delivery mark or counter effect.
Evidence returns complete only when all three block/requeue counts are zero. An exact strong read in
`delivered` with matching immutable authority is an already-delivered success. Send-before-mark death
may resend, but FIFO plus the projector's durable idempotency makes the duplicate harmless; mark-before-
send is forbidden.

Before lifecycle-event send, `RecordStage1AuthorityObservationRequest` accepts exactly
`partition_key`, `record_key`, immutable payload digest, `authority_kind=lifecycle_event`, expected
`outbox_order`, and candidate `observed_at`. Its private direct `UpdateItem` conditions the exact
`EVENT#` key/digest/kind, `outbox_state/status="pending"`, order, and
`attribute_not_exists(authority_observed_at)`, then sets only `authority_observed_at`. After a
conditional conflict it strongly reads: an identical persisted instant is exact replay, an existing
different instant or changed authority is an invariant failure, and a terminal outbox with the matching
persisted instant reuses it. The producer always rereads before constructing the event, so death after
this mutation cannot change observation time or send an event built from an unpersisted value. Callers
cannot supply a table, key syntax, condition, update expression, or attribute map.

`Stage1LifecycleProducer` also owns two private typed failure-state operations:

```text
RecordStage1OutboxDeliveryFailureRequest {
  partition_key, record_key, immutable_payload_digest
  authority_kind: lifecycle_event | usage_contribution | usage_evidence_disposition
  expected_outbox_order, expected_consecutive_failed_delivery_attempts
  failed_at
  dependency_error_class: queue_send_failed | delivery_mark_failed
}
BlockStage1OutboxRequest {
  partition_key, record_key, immutable_payload_digest
  authority_kind: lifecycle_event | usage_contribution | usage_evidence_disposition
  expected_outbox_order, expected_consecutive_failed_delivery_attempts
  expected_requeued_from_block: absent | exact {case_ref, affected_surfaces}
  blocked_at
  block_error_class: queue_send_failed | delivery_mark_failed | schema_validation_failed |
    immutable_digest_mismatch | prefix_kind_mismatch | forbidden_stage1_delta |
    state_invariant_violation
}
```

`requeued_from_block={case_ref, affected_surfaces}` is a private, generation-specific marker: `case_ref`
is the protected case that requeued this exact authority generation and `affected_surfaces` is its sorted,
unique registered-surface tuple. Only the recovery builder creates it. No producer, recovery caller, or
operator can supply an aggregate delta, surface tuple, case selector, key expression, or marker update.
The builders derive table/key syntax, expressions, final count, affected registered surfaces,
`reason_code`, evidence state, aggregate source sequence, observation identity, and every update member;
callers cannot supply them. `queue_send_failed|delivery_mark_failed` map to
`reason_code=stage1_outbox_blocked_dependency` and `evidence=incomplete`. The remaining error classes
map to `reason_code=stage1_outbox_blocked_invalid` and `evidence=unavailable`. No other value is stored
or logged.

A dependency failure uses the reviewed threshold `3`. From consecutive count `0`, the first failure
uses `RecordStage1OutboxDeliveryFailureRequest` and atomically stores `1` while leaving the authority
pending. From count `1`, the next failure uses the same below-threshold request and stores `2`, still
pending. From count `2`, the next failure would reach `3`, so it invokes `BlockStage1OutboxRequest`
instead and atomically stores final count `3` with the block/case/membership/aggregate effects. Every
operation conditions the exact key, immutable digest, authority kind, pending state/status, outbox
order, and prior count. Exact replay returns the existing byte-identical result and never increments.
Requeue begins a fresh generation-specific failure series at count `0`; repeated dependency poison
follows `0→1→2→blocked(3)` again. Validation, invariant, immutable-digest, prefix/kind, or forbidden
Stage-1 delta failures invoke `BlockStage1OutboxRequest` immediately regardless of count and preserve
the current dependency-attempt count rather than inventing one. A changed authority cannot inherit the
prior series.

One `BlockStage1OutboxRequest` transaction contains exactly one conditional authority `UpdateItem`,
one protected-case `PutItem`, one safe-membership `PutItem`, and one aggregate-state `UpdateItem` per
builder-derived affected surface. Its two closed branches are selected only by the optional
`expected_requeued_from_block` snapshot. The absent branch requires
`attribute_not_exists(requeued_from_block)`, conditions the exact key/digest/kind/pending/order and
expected prior count, and increments only the mapped blocked counter. The present branch requires byte
identity with the complete generation-specific marker, requires every affected aggregate's
`requeued_pending` count to be positive, decrements that count once, increments the newly mapped blocked
counter once, and removes `requeued_from_block` in the same transaction. Both branches remove
`outbox_status`/`outbox_order`; set `outbox_state="blocked"`, `blocked_at`, mapped reason/error class,
and final failed-attempt count; retain the immutable payload indefinitely; and create a fresh protected
case and safe membership. The builder derives the branch, marker, surfaces, and all aggregate arithmetic
from strong authority and aggregate snapshots; callers supply none of them. A missing, unexpected,
changed, stale-generation, malformed, or surface-mismatched marker; aggregate underflow; or incomplete
member set fails closed before mutation. Delivery and reblock are the only marker consumers, so their
race has one winner: the loser rereads and returns exact terminal replay or stale authority, never a
second decrement. The builder allocates each new `case_ref` as a canonical lowercase UUIDv4 with no
embedded meaning, sorts and deduplicates `affected_surfaces`, and conditionally creates exactly:

```text
partition_key = CASE#<case_ref>
sort_key = STAGE1_OUTBOX
state = blocked
affected_surfaces = <sorted unique registered Stage-1 surfaces>
blocked_partition_key = <exact publications-table partition key>
blocked_record_key = <exact publications-table record key>
immutable_payload_digest = <exact blocked authority digest>
authority_kind = lifecycle_event | usage_contribution | usage_evidence_disposition
created_at = <owner block instant>
blocked_at = <same owner block instant>
reason_code = stage1_outbox_blocked_dependency | stage1_outbox_blocked_invalid
block_audit = [{state: blocked, at: <owner instant>, reason_code: <mapped reason>}]
requeue_audit = []
```

This protected case has no payload, outbox order, customer content, provider/publication/attempt
identifier, or public lifecycle UUID and is absent-before-create. Its exact blocked key/digest/kind are
protected operational authority readable only inside `Stage1OutboxRecoveryFunction`; API, UI, alarms,
logs, traces, operator credentials, and ordinary CLI surfaces never receive them. In the same
transaction the builder creates the sole safe discovery membership:

```text
partition_key = STAGE1_OUTBOX_OPEN_CASES
sort_key = OPEN#<20-digit blocked epoch-micros>#<case_ref>
case_ref = <same canonical lowercase UUIDv4>
affected_surfaces = <same sorted unique tuple>
state = blocked
blocked_at = <same owner instant>
reason_code = <same mapped safe reason>
reading = <server-owned sanitized reading>
next_action = requeue_stage1_outbox | escalate | no_self_service
```

The membership contains only those fields: no internal selector, digest, authority kind, payload,
provider/publication/attempt identity, or public lifecycle UUID. Its partition is the one safe query lane
for all open cases and preserves concurrent cases independently.

Fixed `STATUS_OBSERVATION#<surface>/STAGE1_OUTBOX_BLOCKS` aggregates contain only complete
`blocked_dependency`, `blocked_invalid`, and `requeued_pending` counters plus monotonic source/report
state, observed time, evidence, and sanitized latest reason/error summary. They contain no scalar
`case_ref` and no membership, flow, event, execution, authority, provider, publication, attempt,
internal key, payload, or authority digest. A first block increments only the mapped blocked counter for
every builder-derived affected surface. Requeue atomically decrements that blocked counter and increments
`requeued_pending` while installing the exact generation-specific marker on the authority. Successful
delivery requires and removes that exact marker while decrementing `requeued_pending`; reblock requires
and removes it while decrementing `requeued_pending`, incrementing the newly mapped blocked counter, and
creating a fresh UUIDv4 case and membership. The old case remains retained as `requeued` audit and is
never reopened. If immutable authority cannot safely select its one fixed family surface, the builder uses all four
registered Stage-1 surfaces, sorted exactly as `semantic_inline_table_run`,
`semantic_projection_plan`, `semantic_projection_unit`, `semantic_servable_pointer`; it never falls
back to any Stage-0 surface. Immediate validation/invariant blocks preserve the current failed-
attempt count without inventing a dependency attempt. Every transition conditions all authority, case,
membership, marker, and aggregate members and rejects replay drift, stale generations, race loss,
counter underflow, or partial surface membership. A conditional conflict retries from strong reads and
returns only an exact already-delivered result, an exact already-blocked result with its byte-identical
fresh case/membership and aggregate effects, or stale/changed authority. Transaction atomicity leaves
all members unchanged on conflict; blocked authority can never exist without its protected case, safe
membership, and complete affected-surface accounting.

The producer's scheduled mode strongly reads those fixed aggregate items independently of the pending-
outbox query. For each new `source_sequence`, it emits three values from the same durable snapshot: the
deterministic `OperationalDataFlowSurfaceObservation.v1` with
`open_operational_case_count=blocked_dependency+blocked_invalid`; the source-ID-free
`Stage1BlockedOutboxCount` gauge used by an alarm whose statistic is `Maximum` — the metric lives in the
repository's one `Adlytics` namespace with exactly the dimensions `environment` and `surface`, and the
alarm evaluates `Maximum >= 1` over one 60-second period with missing data treated as `notBreaching`,
one alarm per registered Stage-1 surface; and either a sanitized
`STAGE1_OUTBOX_BLOCKED` structured error keyed by `report_id` when a blocked count is nonzero or a
`STAGE1_OUTBOX_RECOVERY_PENDING` structured status while only requeued delivery remains. The gauge is
the sum of
blocked and requeued-pending counts, so blocked evidence is nonzero until successful redelivery. Queue,
metric, or log failure leaves `report_state=pending` and retries the same report on the next schedule.
After all three emissions succeed, `RecordStage1OutboxAggregateReportRequest(surface,
expected_source_sequence, report_id, reported_at)` conditionally changes only
`report_state=pending→reported`; changed sequence or report ID conflicts. Send-before-mark death may
repeat the same observation, gauge timestamp/value, or structured `report_id`; projector idempotency,
Maximum-gauge semantics, and report-ID log deduplication make those repeats harmless. Every aggregate
mutation by block, requeue, or requeued delivery installs a new sequence/report ID and resets report
state to pending. The block path may emit the same report promptly but never relies on that attempt for
visibility. Thus blocking, process death immediately after the transaction, and scheduled resend cannot
lose the alarm, structured error, or UI/API evidence. A requeued poison authority begins a new
consecutive-failure series and may block again.

Every Stage-1 base authority, protected case/audit, and
`STATUS_OBSERVATION#<surface>/STAGE1_OUTBOX_BLOCKS` aggregate is retained indefinitely and receives no
TTL attribute. Safe open-case membership is the sole exception: successful requeue conditionally
deletes it because it is discovery state, while the protected case remains the audit authority.
Delivery or blocking removes only the base authority's sparse-index attributes; immutable payload,
state-transition facts, counters, aggregate evidence, and owning plan/unit/run/attempt/head/fence records
remain. Stage 1 enables no DynamoDB TTL and has no retention sweeper. No customer content or raw owner item is copied to the queue, index,
logs, metrics, spans, or aggregate observation.

The function name is the `Stage1LifecycleProducer` variant of the existing typed
`operationalStatusFunctionName` helper. It uses `AppConfig.pythonRuntime`,
`AppConfig.lambdaArchitecture`, `config.logRetentionDays`, and the typed dependencies-layer ARN exactly
as the adjacent status functions do. Its fixed deployment values are memory `512 MiB`, timeout
`60 seconds`, reserved concurrency `1`, stream batch size `10`, and stream parallelization factor `1`.
`OperationalStatusConfig.stage1LifecycleReconcileBatchSize` has reviewed value exactly `10` and
`OperationalStatusConfig.stage1OutboxMaxConsecutiveFailedDeliveryAttempts` has reviewed value exactly
`3`. Both are required positive integer settings; booleans and runtime defaults are rejected. The stack
renders their decimal forms as `ADL_STAGE1_LIFECYCLE_RECONCILE_BATCH_SIZE=10` and
`ADL_STAGE1_OUTBOX_MAX_CONSECUTIVE_FAILED_DELIVERY_ATTEMPTS=3`. The remaining environment is exactly
`ADL_OPERATIONAL_STATUS_QUEUE_URL`, `ADL_SEMANTIC_PUBLICATIONS_TABLE`, and
`ADL_OPERATIONAL_STATUS_TABLE`; the GSI name is the typed constant `stage1-outbox-pending`. The event-
source mapping reads the typed stream ARN at synth and uses exactly `dynamodb:DescribeStream`,
`dynamodb:GetRecords`, `dynamodb:GetShardIterator`, and `dynamodb:ListStreams` on that ARN; the ARN is
not runtime configuration. Synth and startup validation reject a missing, boolean, zero, negative,
non-integer, defaulted, or value-different batch size/threshold before client construction.

The function also receives the repository ADOT export grant. Its non-stream IAM is split across the
exact publications- and operational-status-table ARNs. Publications-table direct
`dynamodb:GetItem` covers `WRITE_PLAN#*`, `TABLE_ATTEMPT#*`, and one exact registered canonical-dataset
prefix `<dataset-key>|*` per dataset. A separate direct strongly consistent `dynamodb:GetItem` statement
on the exact operational-status-table ARN has
`ForAllValues:StringLike: {"dynamodb:LeadingKeys": ["STATUS_OBSERVATION#*"]}` and is attached only to
`Stage1LifecycleProducer`; its closed builder permits only
`record_key=STAGE1_OUTBOX_BLOCKS`. The three and only three use sites are the scheduled aggregate
report, block conditional aggregate reads, and requeued-delivery aggregate reads. It grants no index,
`CASE#*`, `STAGE1_OUTBOX_OPEN_CASES`, `Query`, `Scan`, or batch read.

Direct `dynamodb:UpdateItem` without an enclosing-operation condition is limited to publications
authority prefixes for ordinary delivery-mark, `RecordStage1AuthorityObservationRequest`, or below-
threshold `RecordStage1OutboxDeliveryFailureRequest`, and status `STATUS_OBSERVATION#*` only for
`RecordStage1OutboxAggregateReportRequest`. Transaction-member `dynamodb:UpdateItem` covers the same
exact publications authority prefixes plus operational-status `STATUS_OBSERVATION#*` and requires
`dynamodb:EnclosingOperation=TransactWriteItems`; transaction-member `dynamodb:PutItem` covers only
operational-status `CASE#*` and `STAGE1_OUTBOX_OPEN_CASES` for `BlockStage1OutboxRequest` under that
condition. Closed builders constrain `EVENT#`/`USAGE#`, fixed aggregate keys, protected case keys, and
`OPEN#<epoch-micros>#<case_ref>` safe memberships because IAM cannot. A separate statement grants
`dynamodb:Query` on exactly `<publications-table-arn>/index/stage1-outbox-pending`; another grants
`sqs:SendMessage` on the one status queue ARN. The producer has no publications-table `Scan`, direct
case or safe-membership read/write, status control item, cursor, attempt-fence, Athena, Glue, S3,
projection-reducer, status-DLQ, or wildcard permission. No IAM action names `TransactWriteItems`.

`OperationalStatusProcessorStack` also deploys one dedicated `Stage1OutboxRecoveryFunction` with the
repository runtime, architecture, dependencies layer, log retention, ADOT configuration, and exact
unqualified function ARN. Its fixed deployment values are memory `512 MiB`, timeout `60 seconds`, and
reserved concurrency `1`. Its service-side contracts are closed:

```text
Stage1OutboxRecoveryInvocation.v1 {
  schema_version: "1"
  case_ref: canonical lowercase UUIDv4
  resolution_reason: nonblank sanitized reason
}
Stage1OutboxRecoveryResult.v1 {
  schema_version: "1"
  case_ref: same canonical lowercase UUIDv4
  outcome: requeued | already_requeued
}
```

No invocation/result field may carry a table, internal key, digest, authority kind, payload, state,
order, attribute map, or expression. The function performs `RequeueStage1OutboxRequest` server-side. It
strongly reads exact `CASE#<case_ref>/STAGE1_OUTBOX`, derives and strongly reads the exact
`STAGE1_OUTBOX_OPEN_CASES/OPEN#<blocked-epoch-micros>#<case_ref>` membership, then strongly reads the
protected authority and every affected surface aggregate. It requires the complete protected case,
safe membership, authority, and aggregate snapshots to agree and the case to list a sorted unique
surface tuple. Missing/wrong/malformed/mismatched state fails closed without mutation or selector
disclosure.

One transaction conditionally changes authority `blocked→pending`, removes blocked fields, resets its
failure count, atomically sets the new generation-specific
`requeued_from_block={case_ref, affected_surfaces}` marker from the protected case, and restores a
stable derived pending order without changing immutable payload; changes the protected case
`blocked→requeued`, appends a complete requeue audit containing the function-owned instant and sanitized
reason, and retains its block audit/protected selectors; deletes the exact safe membership under full
field equality; and for every affected surface atomically decrements the mapped blocked counter,
increments `requeued_pending`, advances source/report state, and retains no scalar case reference. It
requires each mapped blocked count to be positive and the authority marker to be absent; caller input
contains no delta or surface tuple. A concurrent aggregate conflict, underflow, stale generation, or
partial member set is retryable and leaves authority, case, membership, marker, and all aggregates
unchanged. Exact replay returns `already_requeued` only when pending authority, exact marker, requeued
case/audit, absent membership, and every aggregate effect are byte-identical; delivered, reused,
underflowing, or changed cases reject. Ordinary delivery and a later reblock are the only consumers and
remove that exact marker while decrementing `requeued_pending`. Reblock maps its new error class,
creates a new case/membership, and never reopens the old case.

The function execution role alone receives the minimum direct strongly consistent `dynamodb:GetItem`
statements on the exact publications authority prefixes and exact status `CASE#*`,
`STAGE1_OUTBOX_OPEN_CASES`, and `STATUS_OBSERVATION#*` leading keys. Its transaction-enclosed grants are
only publications authority `UpdateItem`, status protected-case and aggregate `UpdateItem`, and safe-
membership `DeleteItem`, each on the exact table ARN and leading key required above. It has no direct
mutation, index `Query`, `Scan`, batch read/write, queue, stream, Athena, Glue, S3, projection-reducer,
status-DLQ, semantic owner, wildcard action/resource, or role assumption. Closed builders enforce sort
keys, fields, conditions, actions, and all equality because IAM cannot.

`Stage1OutboxRecoveryOperatorRole` is same-account and grants only `lambda:InvokeFunction` on the exact
unqualified `Stage1OutboxRecoveryFunction` ARN. It has no DynamoDB action or table/index ARN and cannot
invoke an alias, version, or other function. Neither `Stage1LifecycleProducer` nor
`SemanticProjectionOperatorRole` can assume or use it. `OperationalStatusProcessorStack` produces two
typed `PARAMS` contracts, owned only by `PARAMS`/`paramPath()`:

- `stage1OutboxRecoveryOperatorRoleArn` at
  `/adlytics/{environment}/operations/stage1-outbox-recovery-operator-role-arn`;
- `stage1OutboxRecoveryFunctionArn` at
  `/adlytics/{environment}/operations/stage1-outbox-recovery-function-arn`.

The authorized CLI is exactly:

```text
uv run python -m adapters.jobs.requeue_stage1_outbox \
  --profile <non-default-profile> --region <region> --expected-account <12-digits> \
  --env <environment> --operation-id <bounded-operation-id> \
  --case-ref <canonical-lowercase-uuidv4> \
  --resolution-reason <nonblank-sanitized-reason>
```

All arguments are required and validated before any client exists. The ambient boto3 session may create
only STS, SSM, and CloudFormation clients. It calls STS `GetCallerIdentity`; performs exactly four SSM
`GetParameter` reads for the two recovery paths above,
`/adlytics/{environment}/data/v2/semantic-publications-table`, and
`/adlytics/{environment}/operations/status-projection-table-name`; and calls CloudFormation
`DescribeStackResources` only for `Adlytics-DataV2-{env}`,
`Adlytics-OperationalStatusStore-{env}`, and `Adlytics-OperationalStatusProcessor-{env}`. It verifies
caller account equals `--expected-account`; each parameter is nonblank, uniquely owned by its named
stack, and resolves a resource in that account/partition/region; the role and function ARNs are same-
account/partition/region and the function ARN is unqualified; and each table has one unique owning stack
resource. It then calls STS `AssumeRole` on only the verified operator role for exactly 900 seconds with
a bounded IAM-safe session name deterministically derived from `--operation-id`.

The assumed temporary session may create only a Lambda client and synchronously invoke only the exact
verified unqualified function ARN with `Stage1OutboxRecoveryInvocation.v1`. It may not create STS, SSM,
CloudFormation, or DynamoDB clients, assume a second role, or invoke an alias/version. Ambient invoke,
ambient/assumed direct table access, default profile, implicit region/account/environment, resource
listing/guessing, renamed/missing/duplicate parameters, stack/resource ownership mismatch, malformed or
foreign ARN, selector fallback, and any validation/assume/invoke/result-schema failure all fail closed
before recovery and print/log only operation id, opaque case ref, and closed outcome/error code. There
is no fallback session or resource inference.

The queue body is the closed tagged union `OperationalStatusQueueMessage.v1`: exactly one
`OperationalDataFlowEvent.v1`, `OperationalDataFlowSurfaceObservation.v1`,
`OperationalDataFlowUsageContribution.v1`, or
`OperationalDataFlowUsageEvidenceDisposition.v1`. Flow producers use `MessageGroupId=flow_id` and
lifecycle `MessageDeduplicationId=event_id`; usage contributions and evidence dispositions use the same
group with `MessageDeduplicationId=contribution_id` or `disposition_id`. Aggregate observers use
`MessageGroupId=surface#{surface}` and `MessageDeduplicationId=observation_id`, giving each surface one
ordered observation lane without imposing order across flows. Producers call `SendMessage` directly on
the dedicated encrypted FIFO queue; content-based deduplication is not a correctness mechanism. Their
roles receive `sqs:SendMessage` for that queue ARN only. The queue declares retention, encryption,
visibility timeout, receive bound, and a same-kind dedicated DLQ explicitly; visibility exceeds the
projector's maximum execution plus retry margin. The DLQ declares its own retention and alarms. No
status event routes through EventBridge.

The projector event source enables partial-batch response. Producer arrival order is not lifecycle
ordering authority: owner-assigned `transition_sequence` and `predecessor_event_id`, validated by the
reducer, are the proof. FIFO grouping reduces reordering but stream/scheduled discovery may deliver a
successor—including compound plan `N+2`—before its predecessor. The projector validates each event and
conditionally persists it on arrival. A root event has `transition_sequence=1` and no
`predecessor_event_id` and applies against an absent flow snapshot. Every non-root event must name a
predecessor. If that referenced predecessor is not yet applied, the projector atomically writes an
immutable pending receipt plus a conditionally created live `PENDING` index entry and acknowledges the
queue record; it does not hold the FIFO head. An exact redelivery whose predecessor is still absent may
acknowledge the existing pending disposition. If the predecessor is now applied, the redelivery must run
the contiguous reduction before acknowledgement. When a predecessor is applied, the same
transactionally fenced reducer drains the now-contiguous pending chain up to the declared transaction
action and byte budgets. Invalid, conflicting, or over-bound gaps fail and alarm. Exhausted records
redrive to the status DLQ and make evidence incomplete; they never create a terminal flow state. Redrive
is an explicit operator action after the cause is resolved. SQS deduplication is finite, so the DynamoDB
event record remains the durable idempotency gate.

### 6.3 Projection state

The projector performs one conditional DynamoDB transaction per arrival/reduction. It first reads the
current flow snapshot and a bounded contiguous pending chain. The transaction then atomically creates
the immutable received-event/idempotency record and exactly one durable disposition:

- for a noncontiguous event, one immutable pending receipt plus a live `PENDING` index entry keyed by
  `flow_id` and `transition_sequence`, conditioned absent or already owned by the same `event_id`, with
  no applied history;
- for each contiguous event applied immediately or drained from pending, one immutable
  applied-transition record and one idempotent sharded surface contribution;
- for each formerly pending event, one immutable applied marker plus a conditional delete of its live
  `PENDING` index entry owned by that `event_id`; and
- one latest-flow snapshot update to the final applied event, conditioned on the exact pre-reduction
  snapshot event and sequence (or on snapshot absence for the root reduction).

There is no durable `RECEIVED`-only state: receipt and pending/applied disposition commit together. An
exact retry reads the disposition before acknowledging: an applied event is complete; a still-gapped
pending event remains parked; and a now-contiguous pending event resumes reduction. The drain bound is
derived before the transaction from the declared DynamoDB action-count and encoded byte limits. The
same invocation continues bounded reductions while its execution budget permits. Any remainder stays
in the live `PENDING` index. The scheduled reconciler queries only that live index and invokes the same
conditional reducer directly; applied entries have already been removed, so bounded pages cannot be
consumed by historical pending receipts and progress does not depend on another source event.

Pending evidence remains immutable. The event-history API queries only the `APPLIED` key space/index,
so a parked successor cannot appear as lifecycle history before its predecessor. Parking writes neither
a flow-snapshot advancement nor a surface contribution. A separate reducer derives surface summaries
from commutative sharded flow contributions. Surface-observation messages bypass flow predecessor state:
one conditional transaction writes an immutable observation keyed by `observation_id` and advances that
surface's latest-observation pointer only when `source_sequence` increases. Usage-contribution and
usage-evidence messages also bypass lifecycle predecessor state. A contribution transaction creates the
immutable record keyed by `contribution_id` and updates the flow/objective categorized usage aggregate.
An evidence-disposition transaction creates the immutable record keyed by `disposition_id`, validates
that its category is the attempt's durable owner-disposition-settled category, adds its `known_usage` to
that category exactly once, and creates/updates the current evidence-state item for that disposition as
`unavailable` with the exact missing-quantity set. The required `athena.query_count=1` therefore enters
the aggregate once even when provider quantities are missing; exact known scanned/engine values enter
in the same idempotent disposition transaction and are never interpreted as zero.

A generic reconciliation contribution is reduced only when the contributing capability's versioned
contract explicitly authorizes `reconciles_disposition_id`; it must condition-check the immutable
disposition and fixed category, add no second query count, and remain idempotent. Stage 1 never has that
authority: its lifecycle producer rejects every Stage-1 contribution with
`reconciles_disposition_id`, and an unavailable Stage-1 disposition keeps its current evidence state
`unavailable` permanently. Its fixed category, `query_count=1`, and quantities known at owner settlement
remain aggregated exactly once; later scanned-byte or engine-time evidence cannot create a delta,
change that aggregate, or mutate the evidence state.

Repeated ordinary contribution/disposition IDs are no-ops; a new contribution for work caused by an
exact duplicate increments `duplicate_waste` under the original flow without changing lifecycle
history or its immutable objective sample. The summary API combines the reduced flow contribution with this latest aggregate
transport/compute/provider observation and exposes stale or missing observation evidence explicitly. The reducer never depends
on a total order across independent `flow_id` FIFO groups and never shares the flow snapshot transaction.
A failed flow condition is a typed conflict, stale transition, exact already-applied outcome, or parked
predecessor gap after consistent readback; it is never blindly retried. A new table declares managed encryption, on-demand
billing, point-in-time recovery, and retention/removal policy explicitly. Nonterminal flow state is not
expired. Terminal snapshot and transition retention is an explicit operator-history policy and outlives
the finite status queue; after history expiry the API returns the closed non-disclosing absence outcome.
The underlying movement authority, not this retained projection, remains the source of truth.

Lifecycle, completion-outcome, measurement, SLO-result, eligibility, and continuity semantics come from
[Data-Flow Completion and Service Levels](../../concepts/data-flow-completion-and-service-levels.md).
The projector enforces the registry's selected mode and contract mechanically; it does not reinterpret
an owner outcome, infer attainment, or attach a target to observation-only data.

Common projected states are `unknown`, `observed`, `queued`, `running`, `retrying`, `delayed`, `blocked`,
`failed`, `completed`, and `superseded`. Terminal does not mean successful: `blocked`, `failed`,
`completed`, and `superseded` remain distinct. Aggregate lifecycle precedence is `failed`, `blocked`, `delayed`,
`retrying`, `running`, `queued`, `observed`, `completed`, then `unknown`, while stale or incomplete
evidence forces `unknown` regardless of a prior completed lifecycle state. A parent's root/planning
transition immutably registers its discriminated evaluation binding, original clock, dependency roster,
bounded required/optional child sets, and `child_outcome_reduction_version=2`. Stage 1 specializes this
contract narrowly: only a plan root declares children, its required tuple contains exactly its sorted
queued-unit flow IDs, and its optional tuple is empty. The plan's internal projection additionally stores
`required_child_expectations`, a map sorted by ascending unsigned UTF-8 bytes of canonical unit-flow UUID
from every required unit flow ID to the exact plan `owner_generation`. Stage-1 unit and inline roots
store empty required and optional tuples and no child expectation map. Pointer flow IDs are forbidden in
all three roots' child declarations and expectation maps. For each plan map entry the release authority
is derived without another lookup as
`FLOW#<parent_flow_id>` / `CHILD_OUTCOME_GENERATION_HEAD_V2#<child_flow_id>#<20-digit-owner_generation>`;
the expectation cannot name or substitute another key. The projector reconstructs and reattests that
complete map and every derived key from the exact plan `META`, plan-recorded internal lifecycle
authority, and event authority; neither is present on the public `OperationalDataFlowEvent.v1` wire. A
missing, null, unsorted, extra, partial, pointer-bearing, mismatched, negative, or greater-than-20-digit
plan expectation map fails closed; any nonempty unit/inline child tuple or expectation map fails closed.
Non-Stage1 child families must declare and persist their own separately versioned expectation contract
before using child gating; they cannot silently use a null generation or this Stage-1 map.

Child completion reduction applies only to a child ID explicitly declared by its parent's versioned
expectation contract. For Stage 1, this means required plan→queued-unit outcomes only. A pointer terminal
is ineligible even when it has non-null `parent_flow_id` and `causation_event_id`: it writes no
`CHILD_OUTCOME_V2` history, generation head, or latest selector. Admitted child completion uses retained
immutable history, one generation-addressable completion head, and an optional child-latest selector
under the parent partition. The exact keys are:

```text
partition_key = FLOW#<parent_flow_id>
history sort_key = CHILD_OUTCOME_V2#<child_flow_id>#<20-digit-transition-sequence>
generation-head sort_key = CHILD_OUTCOME_GENERATION_HEAD_V2#<child_flow_id>#<20-digit-owner_generation>
latest-selector sort_key = CHILD_OUTCOME_LATEST_V2#<child_flow_id>
```

Each key number is unsigned decimal rendered without a sign or separators and left-padded with ASCII
zeroes to exactly 20 digits. The transition text exists only in the history key and the generation text
exists only in the generation-head key; persisted fields and digest members remain JSON integers. Every
immutable Stage-1 unit history item stores these exact projected fields in addition to its keys:
`child_outcome_reduction_version=2`, `parent_flow_id`, `child_flow_id`, `child_event_id`,
`child_transition_sequence`, `child_execution_id`, `owner_generation`, `lifecycle_state`,
`completion_outcome`, and `outcome_digest`. `owner_generation` is copied from the exact Stage-1 unit
owner payload and is a required nonnegative integer representable by the 20-digit key component; it is
never inferred from flow, execution, arrival order, the parent, or the latest selector. The item may
also retain the event's `occurred_at` and authority kind and opaque digest as non-digest evidence. Only
authority-reattested terminal required-unit outcomes admitted by the plan expectation contract enter this
Stage-1 reduction; pointer lifecycle events and nonterminal lifecycle events remain in their own applied
event history and do not create child-outcome history, a completion head, or a latest selector. The exact
Stage-1 digest definition is:

```text
child_outcome_digest_tuple = ["operational-data-flow-child-outcome/v2", parent_flow_id,
  child_flow_id, child_event_id, child_transition_sequence, child_execution_id,
  owner_generation, lifecycle_state, completion_outcome]
outcome_digest = canonical_authority_digest(child_outcome_digest_tuple)
```

`canonical_authority_digest` is the existing byte-pinned helper defined in §6.2 (`D` is only its short
name). The discriminator, nine-member order, UUID strings, integer `child_transition_sequence`, integer
`owner_generation`, and exact lifecycle and completion values are normative. No member may be omitted,
reordered, renamed, normalized, or substituted; in particular, the 20-digit key text is never hashed in
place of the JSON integer, and generation is never null. History items are immutable and retained under
the operator-history policy, including terminal failures and outcomes superseded by later child
evidence.

Each `CHILD_OUTCOME_GENERATION_HEAD_V2#<child>#<generation>` stores its referenced immutable history
sort key and the same exact `child_outcome_reduction_version`, `parent_flow_id`, `child_flow_id`,
`child_event_id`, `child_transition_sequence`, `child_execution_id`, `owner_generation`,
`lifecycle_state`, `completion_outcome`, and `outcome_digest` bytes as that terminal history item. Its key
and payload generation must agree. It is mutable only among terminal outcomes for that exact child and
`owner_generation`, through exact old-head compare-and-set. First creation conditions head absence;
every advance conditions byte equality of the previously read referenced history key and every projected
field, requires the same generation and a strictly greater integer `child_transition_sequence` already
proven contiguous by applied lifecycle history, and installs the complete new head. No generation can
create, overwrite, regress, or fence another generation's head.

`CHILD_OUTCOME_LATEST_V2#<child>` is a separate mutable selector solely for current API counts and
visibility. It copies the generation head's complete bytes and references that head and immutable
history, but advances only when `(owner_generation, child_transition_sequence)` is lexicographically
higher than the exact old selector tuple. First creation conditions absence and every update conditions
all old selector bytes. It is never parent-completion, retry, reconciliation, objective, or historical
measurement authority; its absence, lag, or a newer generation cannot make an expected generation head
eligible or ineligible.

One admitted required-unit reduction transaction conditionally creates exactly one immutable terminal
history item, creates or updates exactly one generation head, and includes at most one latest-selector
create/update when the candidate tuple is higher. Transaction capacity and item budgets count history, generation head, and
the optional selector. Exact replay requires existing history, generation-head, and any same-candidate
selector bytes to match and is a complete no-op. An occupied history key with different bytes, digest drift, stale or skipped generation head, regression,
branch, same-sequence different event, changed parent/child/event/execution/generation/state/outcome
authority, or equal latest-selector tuple with different candidate bytes fails closed. A lower candidate
tuple does not target or regress the selector and may still atomically create/advance its own generation
head. A latest-selector CAS loser observes no partial history or generation-head write, rereads the
selector and exact generation head, then retries the full reduction transaction without a selector
update when a higher tuple won, converges to exact replay, or fails stale.

A bounded reducer drain preserves contiguous lifecycle order and runs one such transaction per newly
applied admitted required-unit terminal outcome. It never places two operations for the same generation
head or latest selector
in one transaction, never skips an intermediate authority to manufacture contiguity, and rereads exact
old bytes before a later outcome. Thus multiple terminal revisions for one generation advance that
head in separate fenced transactions, while outcomes for G and G+1 update distinct generation heads;
the latest selector may overtake in either direction only by its higher-tuple rule.

The committed child-outcome byte fixture pins the integer/key distinction and the exact digest contract:

```text
parent_flow_id = 11111111-1111-4111-8111-111111111111
child_flow_id = 22222222-2222-4222-8222-222222222222
child_event_id = 33333333-3333-4333-8333-333333333333
child_transition_sequence = 7
history key sequence text = 00000000000000000007
child_execution_id = 44444444-4444-4444-8444-444444444444
owner_generation = 7
lifecycle_state = completed
completion_outcome = succeeded
history sort_key = CHILD_OUTCOME_V2#22222222-2222-4222-8222-222222222222#00000000000000000007
generation-head sort_key = CHILD_OUTCOME_GENERATION_HEAD_V2#22222222-2222-4222-8222-222222222222#00000000000000000007
latest-selector sort_key = CHILD_OUTCOME_LATEST_V2#22222222-2222-4222-8222-222222222222
canonical tuple JSON = ["operational-data-flow-child-outcome/v2","11111111-1111-4111-8111-111111111111","22222222-2222-4222-8222-222222222222","33333333-3333-4333-8333-333333333333",7,"44444444-4444-4444-8444-444444444444",7,"completed","succeeded"]
canonical tuple UTF-8 length = 226
canonical tuple UTF-8 hex = 5b226f7065726174696f6e616c2d646174612d666c6f772d6368696c642d6f7574636f6d652f7632222c2231313131313131312d313131312d343131312d383131312d313131313131313131313131222c2232323232323232322d323232322d343232322d383232322d323232323232323232323232222c2233333333333333332d333333332d343333332d383333332d333333333333333333333333222c372c2234343434343434342d343434342d343434342d383434342d343434343434343434343434222c372c22636f6d706c65746564222c22737563636565646564225d
outcome_digest = 03c5405af6fcc02c643a572d8f308cd0f29a31a4bb7cdaaf1295359a8695e440
```

Reducer creation, rebuild, replay, compare-and-set, and head/history validation use this one shared
vector through the existing canonical authority digest helper. The generation-head and latest-selector
keys are not digest members, so generation-addressable authority changes neither the committed 226-byte
canonical tuple nor its digest. Negative vectors reject every omitted or reordered member and every
typed drift, including `"7"`, `"00000000000000000007"`, or `7.0` in place of integer transition
sequence `7`, and null, string, float, prior, or future values in place of integer
`owner_generation=7`; the history key rejects any sequence component and the generation head rejects any
generation component other than the corresponding exact 20-digit text.

For each parent-bound measurement contract, the projector resolves its registered eligible-success policy
only against exact generation heads derived from that parent's versioned expectations and counts
declared, successful, and terminal expectation-backed children separately. For Stage 1 this population
is only a plan's required queued units, using the head whose key embeds the exact `owner_generation`
mapped for that child flow in `required_child_expectations`; unit and inline roots have no child
population, and pointer dispositions cannot affect their samples or objectives. No shared latest value
is consulted. Activated objectives reference that same immutable measurement policy; they do not
redefine child success. Earlier required-unit failure and success history remains queryable internally
and each generation's terminal authority remains directly addressable after another generation
advances. The separate latest selector drives only current API child counts/visibility for
expectation-backed children: current failure is terminal but unsuccessful, current success replaces that
current view, and a missing or malformed selector makes the current view unknown without affecting any
parent. A previously written objective miss is immutable: a later matching generation head may release
an awaiting plan but cannot rewrite that historical miss, and a historical API read never substitutes
the latest selector for its expectation-addressed head.

Only a plan `completed` event is gated on Stage-1 children. If it arrives before every required queued
unit's exact expected generation head is eligible-successful, one transaction creates its immutable
received/idempotency record, an immutable awaiting-child disposition, and an owned live
`AWAITING_CHILDREN` index entry; it remains absent from applied history. A generation-G head therefore
leaves a G+1 Stage-1 plan awaiting, even when the latest selector still shows G success. `blocked`,
`failed`, and `superseded` plan terminals apply immediately after their own authority re-attestation and
remain visible even when a required unit never succeeds. Unit and inline terminals are never child-gated;
their same-transaction pointer flows are topology only.

Initial evaluation, exact retry, generation-head stream delivery, and scheduled reconciliation use the
same bounded release read for a Stage-1 plan: one strongly consistent root/snapshot read followed by one
bounded `BatchGetItem` whose table request sets `ConsistentRead=true` and names exactly the required
queued-unit `CHILD_OUTCOME_GENERATION_HEAD_V2#<child>#<20-digit-expected-generation>` keys derived from
`required_child_expectations`. No optional pointer generation-head read exists. They never read
`CHILD_OUTCOME_LATEST_V2` for completion. No scan, query, per-child read loop, history-cardinality count,
immutable-history walk, v1-marker read, shared-head read, or unbounded fallback is permitted. The plan
root read includes byte-exact `child_outcome_reduction_version=2` and sorted
`required_child_expectations` reattested from plan `META`/event. A missing head, undeclared or nonderived
key, malformed or wrong-version head, key/payload parent-child-generation disagreement,
missing/noninteger generation, or digest/history-key mismatch is ineligible and fails closed.

Plan release is conditioned on the root/snapshot and every required generation head remaining byte-
identical to that exact read, including exact expected `owner_generation`, `child_transition_sequence`,
`child_event_id`, referenced immutable history key, and `outcome_digest`; only eligible-successful heads
for every exact expectation release completion and conditionally delete the wait-index entry. A
successful G head can never release G+1 because the latter reads a different absent or unsuccessful key.
A delayed G parent still reads G's immutable-addressed generation head and can release after G+1 has
advanced both its own generation head and the API latest selector. Duplicate, replayed, reverse-order,
and competing child reductions converge through immutable history, per-generation exact old-head
compare-and-set, parent event idempotency, and conditional re-read. If either parent trigger is withheld,
the scheduled reconciler derives and reads the same expected keys and reaches the same result; no later
child event or selector regression is required.

The internal reduction has a hard version boundary. Public `OperationalDataFlowEvent.v1`, API v1
schemas, URLs, and completion vocabulary remain unchanged, but v2 reducers write and read only
`CHILD_OUTCOME_V2` history, `CHILD_OUTCOME_GENERATION_HEAD_V2` completion heads, and optional
`CHILD_OUTCOME_LATEST_V2` API selectors for explicitly declared, expectation-backed children. They never
write or read the superseded shared `CHILD_OUTCOME_HEAD_V2`, and never dual-write, mix, copy, or adopt v1
parent/child markers. Before cutover, a stopped v1 reducer is followed by an idempotent bounded rebuild
from immutable applied v1 public-event history and immutable root child declarations plus the owning
family's exact versioned expectation authority—not from v1 marker or shared-head contents—into absent v2
history, one exact-CAS head per observed child generation, and at most one highest-tuple latest selector
per child. Stage-1 rebuild considers only plan-declared required unit IDs, reattests
`required_child_expectations` from exact plan `META`/event, and copies each admitted unit event's
`owner_generation` from its exact unit-owner authority. It skips every pointer event and topology link;
no pointer child history, generation head, latest selector, expectation, or migration record is created.
For required units it validates the unchanged digest tuple, retains all terminal revisions, groups them
by child and owner generation, and advances each generation head in sequence order without targeting one
key twice in a transaction.

Cutover requires every applicable required plan→unit expectation with an applied terminal unit outcome
to derive one byte-valid generation head, including retained predecessor generations; every latest
selector, when present, must reference the byte-valid greatest `(owner_generation,
child_transition_sequence)` required-unit head. Pointer flows remain preserved in ordinary applied flow
history and are outside this check. No awaiting plan may have unresolved rebuild work and no v1 writer
lease may remain. Rebuild replay is an exact no-op, conflicting v2 bytes or a superseded shared-head
dependency fail migration, and rollback stops before any v2 write is accepted. The v2 reducer then
starts with no dual-read period. Once v2 processing starts, v1 markers and old shared heads are ignored
forever and their presence cannot satisfy, block, regress, or seed a generation head or latest selector.

The deterministic measurement reducer reads the root's registered measurement/version/start and exact
owner/child authority timestamps through registered reconciliation readers. At an authority-backed
terminal or unavailable evidence it writes one immutable `DataFlowObservationSample.v1`. A separate
descriptive reducer closes a baseline window at its measurement-defined population cutoff after
registered authority cursors cross the window end; unreadable or lagging evidence finalizes it as
`incomplete`, never complete by inference. The immutable window identity includes measurement/version, dependency
reference, bounds, and revision. A terminal sample reconciled after cutoff creates a successor correction
that names the prior window and increments revision; it never mutates historical aggregates. Active
elapsed time stays visible on the flow snapshot but outside baseline percentiles. Baseline records
contain no target, attainment result, error budget, burn rate, or threshold alarm input.

For `evaluated` surfaces only, the deterministic objective evaluator reads the observation's exact
measurement version, the root's registered objective version, the retained
immutable registry definition addressed by that exact objective reference/version, and exact owner/child
authority timestamps through their registered reconciliation readers. It never substitutes the active
registry version during evaluation or rebuild; a missing or digest-mismatched historical definition
produces `unknown` evidence rather than recalculation under newer semantics. It writes one rebuildable
immutable `DataFlowObjectiveSample.v1` projection when the flow is successfully decided, becomes late, becomes unknowable, or is timely superseded. For a late nonterminal
flow, the sample records `decision_reason=deadline_elapsed`, preserves the current lifecycle state, and
has no `completion_outcome`; a later owner terminal remains visible in the flow snapshot but cannot
rewrite the historical miss. `usage_at_decision` snapshots accepted contributions at that instant;
subsequent contributions advance only the separately idempotent current usage aggregate. A cost
objective is not decided before its registered usage-freeze authority. An evaluated-mode scheduled
deadline sweep evaluates active objective samples without inventing lifecycle completion; observation-only
surfaces have no deadline sweep. A separate SLO window reducer groups
samples by objective version and evaluation window and writes `DataFlowObjectiveWindowResult.v1`; it
never changes an immutable flow sample. Status projection remains derived authority for neither data nor
serving.

The projection stores no source rows, customer/business analytical measurement values,
user/customer/entity labels, source object keys, workload queue bodies, SQL,
prompt/question/answer/evaluation content, raw error descriptions, AWS resource names, ARNs, provider
request IDs, trace IDs, query IDs, publication IDs, or attempt IDs. It may store only registry-declared
operational clock values, descriptive latency aggregates, closed counts, and `UsageUnits.v1` quantities
needed by the status contract; these carry no customer dimensions.
Internal authority references are one-way opaque digests. Only the defined status UUIDs are returned by
the API, and none is accepted as an AWS or authority selector.

### 6.4 Operator API

#### `GET /api/operations/data-flow-definitions`

Returns schema and registry versions, observation policy, canonical UUID form and relationship
semantics, registered evaluation modes, measurement references/versions, optional objective
references/versions and result vocabulary, all registered surfaces,
stage/state/reason/next-action vocabularies, operator-safe descriptions, and runbook anchors. It is the
machine-readable usage guide and source for UI/CLI labels.

Stage 1 definitions return a `runbook_anchor` for every listed surface and recovery case. Values are
repository-relative strings, not deployment URLs, and the mapping is static and exact:

| Definition key | Exact `runbook_anchor` |
|---|---|
| surface `semantic_projection_plan` | `docs/capabilities/load-path/write-discipline.md#8-failure-retry-and-recovery` |
| surface `semantic_projection_unit` | `docs/capabilities/load-path/write-discipline.md#8-failure-retry-and-recovery` |
| surface `semantic_inline_table_run` | `docs/capabilities/load-path/write-discipline.md#9-operator-and-transitional-inline-paths` |
| surface `semantic_servable_pointer` | `docs/capabilities/load-path/write-discipline.md#23-envelope-attestation-and-readers` |
| case `stage1_outbox_blocked_dependency` | `docs/capabilities/operational-data-flow-status/low-level-design.md#stage-1-lifecycle-delivery-runtime` |
| case `stage1_outbox_blocked_invalid` | `docs/capabilities/operational-data-flow-status/low-level-design.md#stage-1-lifecycle-delivery-runtime` |
| case `provider_fence_recovery` | `docs/capabilities/load-path/write-discipline.md#5-table-lease-and-durable-provider-attempt-fence` |

The definitions builder accepts no fallback or caller-selected anchor. A missing key, missing or blank
anchor, unknown surface/case, non-repository-relative value, fragment mismatch, or mapping drift rejects
the definitions response. Contract tests pin every key/string pair above and the corresponding unknown
and missing cases.

#### `GET /api/operations/data-flows`

Accepts only closed non-sensitive query filters: `surface`, `state`, and `freshness`. Returns:

```text
OperationalDataFlowSummary.v1 {
  observed_at
  reconciled_at
  evidence: complete | incomplete | stale | unavailable
  registry: {version, active, incomplete, external_boundaries}
  overall_coverage: complete | incomplete
  aggregate_lifecycle_state
  surfaces[]: {
    surface, evaluation_mode, state, freshness, active, queued, retrying, blocked, failed,
    completed_rate, latency, evidence_distribution,
    baseline_windows[]: {
      window_id, predecessor_window_id, revision,
      measurement_ref, measurement_version, dependency_roster_ref,
      window_start, window_end, population_cutoff, finalized_at,
      completeness, late_sample_count,
      samples: {total, by_lifecycle, by_outcome, by_evidence},
      observed: {minimum, maximum, mean, p50, p90, p95, p99, unit},
      usage: UsageByCategory.v1
    },
    objective_windows[]: {
      objective_ref, objective_version, dependency_roster_ref,
      window_start, window_end, evaluated_at,
      sample_target: {measure, comparator, value, unit},
      samples: {met, missed, unknown, not_applicable},
      observed_percentile, miss_fraction, unknown_fraction,
      slo_result, burn: {short_window, long_window}
    },
    reason_code, open_operational_case_count: nonnegative integer, reading, next_action
  }
}
```

Counts and rates are operational aggregates, not customer measurements. `open_operational_case_count`
is the surface's `blocked_dependency + blocked_invalid` snapshot and never identifies a case.
`observation_only` surfaces return descriptive `baseline_windows` and an empty `objective_windows`
array; the server never maps baseline evidence into SLO result vocabulary. Evaluated surfaces return
both. This route returns no per-flow, authority, provider, AWS, publication, attempt, trace, query, or
case identifier.

#### `GET /api/operations/data-flow-cases`

Accepts required integer `limit` in `1..100` and optional server-issued opaque `cursor`; every other
filter or selector rejects. The route performs one `ConsistentRead=true` DynamoDB `Query` only on base-
table partition `STAGE1_OUTBOX_OPEN_CASES`, in ascending `OPEN#<epoch-micros>#<case_ref>` order, and
returns:

```text
OperationalDataFlowCasePage.v1 {
  cases: [{
    case_ref: canonical lowercase UUIDv4
    affected_surfaces: sorted unique registered Stage-1 surfaces
    state: blocked
    blocked_at: UTC instant
    reason_code: stage1_outbox_blocked_dependency | stage1_outbox_blocked_invalid
    reading: server-owned sanitized reading
    next_action: requeue_stage1_outbox | escalate | no_self_service
  }]
  next_cursor: optional opaque cursor
}
```

The cursor discloses no partition/sort key or internal identity. This route cannot read `CASE#*`,
authority tables, aggregates, or any other status partition and never returns internal selectors,
digests, authority kinds, payloads, provider/publication/attempt IDs, or lifecycle UUIDs. Conditional
membership deletion removes a successfully requeued case from subsequent pages.

#### `GET /api/operations/data-flows/{flow_id}`

Accepts one canonical UUID path parameter and performs one exact keyed lookup. It returns:

```text
OperationalDataFlow.v1 {
  flow_id
  parent_flow_id
  causation_event_id
  execution_id
  predecessor_execution_id
  latest_event_id
  latest_sequence
  surface, evaluation_mode, stage, state, outcome
  completion_outcome: optional succeeded | completed_no_change | completed_refused | exhausted | superseded
  continuity: {state, served_revision_relation}
  observation_samples[]: {
    measurement_ref, measurement_version, dependency_roster_ref,
    started_at, decided_at, elapsed_ms, sample_value: {measure, value, unit},
    lifecycle_state, completion_outcome, evidence,
    required_children: {declared, successful, terminal},
    optional_children: {declared, successful, terminal},
    usage_at_decision: UsageByCategory.v1
  }
  objective_samples[]: {
    objective_ref, objective_version, measurement_ref, measurement_version, dependency_roster_ref,
    sample_target: {measure, comparator, value, unit},
    lifecycle_state,
    completion_outcome: optional succeeded | completed_no_change | completed_refused | exhausted | superseded,
    decision_reason, sample_result,
    started_at, decided_at, elapsed_ms, sample_value: {measure, value, unit}, evidence,
    required_children: {declared, successful, terminal},
    optional_children: {declared, successful, terminal},
    usage_at_decision: UsageByCategory.v1
  }
  usage: UsageByCategory.v1
  progress
  authority: {kind, observed_at}
  evidence, freshness, observed_at, reconciled_at
  reason_code, reading, next_action
  required_children: {declared, successful, terminal}
  optional_children: {declared, successful, terminal}
  events_url
}
```

The API derives current `required_children` and `optional_children` declared/successful/terminal counts
only for expectation-backed child sets, using the root declaration and one bounded strongly consistent
`BatchGetItem` of the corresponding `CHILD_OUTCOME_LATEST_V2` selectors. It counts each declared child
at most once and never reads retained history. For Stage 1, plan counts describe only required queued
units whose selected `owner_generation` equals that flow's exact root expectation; the optional counts
are zero. Stage-1 unit and inline required and optional counts are both zero. Pointer flows are fetched
through their own flow IDs and exact topology, never through a parent child count or latest selector. A
selected predecessor-generation unit success counts as neither current successful nor current terminal
for a successor plan. Other families apply their separately versioned expectation contracts. These
counts are descriptive current visibility, not a completion predicate: release, retry, scheduled
recovery, parent-bound measurements, objectives, and historical reads use exact generation heads only
for declared expectation-backed children. The API exposes neither expectations, owner generation,
history, selector/head keys, digests, reduction version, nor compare-and-set state. A current-generation
required-unit failure remains terminal but unsuccessful; a later higher-tuple success replaces that
unit's current count without deleting any generation head or history or rewriting an immutable
observation/objective sample. After G+1 overtakes delayed G, G's completed snapshot and immutable samples
retain their exact decision while this explicitly current required-unit count may show only the newer
selector.

An `observation_only` flow returns observation samples and an empty objective-sample array. An
evaluated flow retains its observation samples and adds objective samples; clients never infer
attainment from descriptive fields.

Malformed, absent, expired, or unauthorized identifiers receive one closed non-disclosing absence
outcome. The route cannot accept time-range, free-text, infrastructure, source, customer, or authority
selectors.

#### `GET /api/operations/data-flows/{flow_id}/events`

Accepts only a server-issued opaque `cursor` and a bounded `limit`. It queries only immutable applied
transitions from the `APPLIED` history key space/index and returns stable `transition_sequence` order
with `event_id`, `predecessor_event_id`, execution lineage, stage/state, progress, authority observation,
reason, reading, and next action. A parked successor remains absent until its predecessor permits
contiguous application; clients never infer pending state from a sequence gap. The cursor carries no
customer or infrastructure identity. Missing/invalid cursor and flow access use the same non-disclosing
absence contract.

#### Initiation receipt

A repository-owned interface that initiates asynchronous data work returns:

```text
DataFlowInitiationReceipt.v1 {
  flow_id: UUID
  status_url: "/api/operations/data-flows/{flow_id}"
}
```

Submission proves only that the owner durably recorded the start; it does not imply queued, running, or
completed state. Externally initiated work receives the same identity at the first repository-observed
boundary.

Every response sets `Cache-Control: no-store`, includes evidence completeness and observation time, and
serves plain-language `reading` and `next_action` beside their closed codes. Client code renders those
words verbatim and never derives health.

### 6.5 Authorization and least privilege

Cognito group `operations-status-read` is projected into a dedicated signed operator entitlement. The
custom authorizer maps only the exact definitions, summary, safe case-list, flow, and event routes to
that entitlement after normal session verification. API readers can `GetItem`, bounded `Query`, or
bounded `BatchGetItem` only on the projection table/indexes needed by their route. Current flow-detail
child counts permit one exact root/snapshot `GetItem` plus one bounded `BatchGetItem` over only its
declared expectation-backed `CHILD_OUTCOME_LATEST_V2` selector keys with `ConsistentRead=true`;
historical parent-bound views instead derive and read only their exact declared expectation-backed
`CHILD_OUTCOME_GENERATION_HEAD_V2` keys. For Stage-1 unit and inline detail there are no child selector
or generation-head keys to request; pointer history is read only through the pointer flow's ordinary
event route. No API role can read `CHILD_OUTCOME_V2` history, v1 markers, superseded shared heads,
undeclared heads/selectors, or internal digest/compare-and-set fields. The case-list route
alone receives `dynamodb:Query` on the exact base status-table ARN with
`dynamodb:LeadingKeys=STAGE1_OUTBOX_OPEN_CASES` and always uses `ConsistentRead=true`. API roles are
explicitly denied `CASE#*` reads, safe-lane mutation, and authority-table access. They have no `Scan`, write, CloudWatch, SQS,
EventBridge, S3, Athena, Glue, semantic-state, artifact-custody, evaluation-input, or audit-content
access. Status producers receive only exact `sqs:SendMessage` on the status queue. The projector
receives only event-source consumption, exact status-DLQ redrive plumbing, and conditioned projection
writes; the reconciler receives bounded authority reads plus exact status-queue send.

### 6.6 Operator use contract

The browser and CLI share five executable workflows:

1. **Triage overall movement.** Read definitions once, then summary. Check registry completeness and
   evidence freshness before lifecycle state. Select the highest-precedence actionable lifecycle state
   or incomplete/stale evidence condition.
2. **Follow one flow.** Use only a UI/API-issued `flow_id`; call `flow status {flow_id}` or the exact
   flow route; read lifecycle, completion outcome, continuity, observation samples, and—only when the
   surface is evaluated—objective results under the shared
   [completion/SLO semantics](../../concepts/data-flow-completion-and-service-levels.md).
3. **Explain lineage.** Use `flow explain {flow_id}` or the event route. `execution_id` and
   `predecessor_execution_id` explain retries; `predecessor_event_id` explains transition order;
   `parent_flow_id` and `causation_event_id` explain spawned work.
4. **Diagnose and watch.** `flow watch {flow_id}` polls the snapshot/event cursor with bounded backoff.
   Compare queue, compute, provider, and authority-transition latency. Residual time stays labelled
   residual. Use the same `flow_id` with separately authorized logs/traces tools; the status API does
   not return raw trace, query, provider, or infrastructure identifiers.
5. **Handle failure and validate recovery.** Read served reason and next-action code. V1 may direct
   `observe`, `inspect_diagnostic`, `retry_via_owner`, `requeue_stage1_outbox`, `escalate`, or
   `no_self_service`; it never performs the action itself. Summary reports only
   `open_operational_case_count`. Page all open cases through the safe case-list route with explicit
   bounded limit; each blocked case exposes only opaque canonical case reference, sorted affected
   surfaces, blocked time/state, safe reason, server-owned reading, and next action. Definitions and the
   protected runbook explain threshold and requeue preconditions. UI, API, ordinary CLI output, alarms,
   and examples never expose or accept internal key, digest, or kind.

   After cause resolution, an authorized operator runs the exact bootstrap command above with explicit
   profile/Region/account/environment/operation/case/reason. Ambient credentials verify four typed
   parameters and three owning stacks, assume only the invoke-only role for 900 seconds, and the
   temporary Lambda-only session synchronously invokes the exact unqualified recovery function. The
   function—not operator credentials—reads protected case/membership/authority/aggregate state and
   performs the conditional transaction. Recovery requires the selected membership to disappear, the
   protected case audit to be `requeued`, per-surface blocked counts to decrement and
   `requeued_pending` to increment, a newer aggregate observation, idempotent outbox delivery, and any
   owner-specific acceptance. A successful invoke, zero queue snapshot, or summary-count change alone
   cannot close recovery.

The UI/API/CLI documentation includes copy-paste requests using placeholders, response examples for
every common state, a state-transition glossary, “what this does not prove” notes, and an escalation
table naming the owning capability—not infrastructure resources or individuals.

## 7. Concepts applied

- [Authentication](../../concepts/authentication.md)
- [Data-Flow Completion and Service Levels](../../concepts/data-flow-completion-and-service-levels.md)
- [Observability](../../concepts/observability.md)
- [Provenance Chain](../../concepts/provenance-chain.md)
- [Load Path](../load-path/README.md)
- [Publication State](../publication-state/README.md)
- [Agent Quality Loop](../agent-quality-loop/README.md)

## 8. Decisions

- [ADR-007](../../decisions/adr-007-auth-cookie-bff.md)
- [ADR-009](../../decisions/adr-009-observability-instrumentation.md)
- [ADR-012](../../decisions/adr-012-stack-composition-and-config.md)
- [ADR-043](../../decisions/adr-043-a-landed-manifest-is-the-trigger-and-serialisation-is-the-correctness-requirement.md)
- [ADR-047](../../decisions/adr-047-separate-attempts-leases-publications-and-coverage.md)
- [ADR-059](../../decisions/adr-059-create-a-bounded-agent-quality-audit-loop.md)
- [ADR-060](../../decisions/adr-060-protect-quality-review-retrieval-with-an-explicit-operator-authorization-boundary.md)
- [ADR-073](../../decisions/adr-073-project-all-data-movement-into-an-operator-status-control-plane.md)
- [ADR-074](../../decisions/adr-074-define-authority-backed-data-flow-completion-and-service-level-objectives.md)
- [ADR-075](../../decisions/adr-075-generate-page-artifacts-once-per-dependency-cohort.md)

## 9. Acceptance conditions and rollout gates

Development observation-only implementation is authorized by accepted ADR-073 and ADR-074. Its first
slice records the current topology so that [ADR-075](../../decisions/adr-075-generate-page-artifacts-once-per-dependency-cohort.md)
can be compared before and after under identical clocks. It may
start before objective values or complete page dependency rosters are accepted, provided each active
surface has a versioned measurement contract and authority reader, every absent surface remains
explicitly incomplete, and no target, objective result, error budget, burn rate, target alarm, healthy
SLO, or complete-coverage claim is produced.

Observation-only acceptance requires the applicable infrastructure, identity, ordering, authority,
content-safety, authorization, API, and reconciliation conditions below, plus exact confirmation that
objective arrays are empty and baseline records contain no target/result fields. Conditions specifically
about deadlines, `met`/`missed` arithmetic, objective windows, or burn rates are deferred.

Evaluated activation remains blocked until every concrete objective and dependency-contract open item
for that surface is accepted.

Shared rollout conditions, enforced as applicable to the active mode:

- Registry schema and generated infrastructure inventory have no unmatched surfaces; the inventory
  proves existing S3 EventBridge ingestion remains and no EventBridge target exists in the status path.
- Every surface is `active` or explicitly incomplete/external in API and UI; any planned, unsupported,
  or missing entry keeps `registry.incomplete` nonzero and `overall_coverage=incomplete`. Observation-only
  responses expose no overall health field.
- Infrastructure proves the dedicated encrypted FIFO queue, same-kind DLQ, explicit retention,
  visibility, receive/redrive policy, alarms, exact producer IAM, projector event-source mapping, and
  partial-batch response.
- Stage-1 lifecycle infrastructure proves `DataV2Stack` deploys the exact
  `stage1-outbox-pending` GSI on the retained publications table with String keys `outbox_status` and
  `outbox_order`, `KEYS_ONLY` projection, and `ACTIVE` readback before the pre-switch enables amended
  writers or the producer. Every Stage-1 outbox has `outbox_state=pending|delivered|blocked`; only
  pending `EVENT#`/`USAGE#` authorities carry both index keys, and delivery or blocking removes both.
  No lifecycle full-table scan, control item, cursor, `ExclusiveStartKey`, or `LastEvaluatedKey`
  persistence exists.
- Producer infrastructure proves exactly one `Stage1LifecycleProducer` and one dedicated
  `Stage1OutboxRecoveryFunction` in `OperationalStatusProcessorStack`, with no separate Stage-1 usage
  producer. Producer assertions cover typed stream resolution; `LATEST`/`NEW_IMAGE` pending
  `EVENT#|USAGE#` filter and `ReportBatchItemFailures`; schedule/sizing/bounds/environment; exact stream,
  publications, queue, metric, and log grants; and exact transaction-enclosed block writes. They also
  prove the producer's direct strongly consistent `dynamodb:GetItem` on the exact status-table ARN with
  `LeadingKeys=STATUS_OBSERVATION#*` only, and builder restriction to exactly three uses: scheduled
  report, block conditional aggregate reads, and requeued-delivery aggregate reads. Tests deny indexes,
  cases, safe lane, other sort keys, `Query`, `Scan`, and batch reads.

  Recovery infrastructure asserts the two exact typed PARAMS paths and unqualified function ARN;
  service contracts with only version/case/reason or version/case/outcome; an invoke-only same-account
  `Stage1OutboxRecoveryOperatorRole` with `lambda:InvokeFunction` on that exact unqualified ARN and zero
  DynamoDB; and a function role with only exact direct case/membership/authority/all-aggregate reads and
  transaction-enclosed authority/case/aggregate updates plus membership delete. Alias/version/other-
  function invocation, direct mutations, scans/queries, batch access, wildcard resources/actions, and
  every unrelated service are denied. Producer block IAM alone may transactionally put protected case
  and safe membership plus update authority/aggregates; it cannot read either case partition. Direct
  Phase-1 invocation/wait/poll wiring remains denied.
- Handler/domain tests drive stream and schedule envelopes through the same closed tagged use case and
  cover `EVENT#` lifecycle, ordinary fully settled Athena `USAGE#` contribution, and fully settled
  Athena `USAGE#` evidence-disposition authorities. They require exact prefix/kind agreement, owner-
  disposition settlement, strong base reads, typed write-once
  `RecordStage1AuthorityObservationRequest`, status/usage union mapping and FIFO IDs, failed stream
  `SequenceNumber`s, send-before-mark, and `pending→delivered` removal of both sparse-index fields only
  after send success. They reject mixed modes, malformed images, terminal-but-unsettled attempts, every
  Stage-1 `reconciles_disposition_id`/delta authority, non-Athena Stage-1 usage, changed immutable
  payloads, ambiguous kinds, and customer-content canaries and prove an already-delivered replay is a
  no-op. First-observation tests condition exact key/digest/kind/pending/order, write only
  `authority_observed_at`, reread before mapping, preserve it across death/replay/delivery, and reject a
  changed instant or caller-supplied expression. Acceptance proves provider terminality alone creates no `USAGE#` or usage queue member; after
  owner settlement, stream delivery or sparse-GSI recovery sends exactly one ordinary contribution or
  unavailable disposition with the same identity. A Stage-1 unavailable disposition's category,
  `query_count=1`, known quantities, and unavailable current-evidence state remain unchanged forever.
- Scheduled-recovery tests query from the pending start with `Limit=batch` and no cursor. Successes
  become delivered and leave the index. Validation/invariant failures immediately invoke
  `BlockStage1OutboxRequest` regardless of count. Dependency failures pin the reviewed sequence:
  count `0→1` and `1→2` use `RecordStage1OutboxDeliveryFailureRequest` and remain pending; the next
  failure uses atomic Block and stores final count `3`. Exact replay at each state does not increment;
  requeue resets the new generation to `0`; repeated poison follows the same sequence and reblocks with
  a fresh case. Synth asserts batch `10`, threshold `3`, and exact decimal environment rendering;
  startup rejects missing, boolean, defaulted, non-integer, nonpositive, or different values before a
  client exists. Contract tests exhaust every exact supplied field, builder-derived field, and closed
  reason/error mapping and reject unknown classes or caller-supplied expressions. The
  cross-table block transaction conditions exact authority and creates atomically: one protected
  `CASE#<case_ref>/STAGE1_OUTBOX` with canonical lowercase UUIDv4, sorted unique affected surfaces,
  complete block/requeue audit and protected selectors; one safe
  `STAGE1_OUTBOX_OPEN_CASES/OPEN#<epoch-micros>#<case_ref>` membership containing only case reference,
  surfaces, blocked state/time, safe reason, server reading, and next action; and every affected
  aggregate increment with no scalar case reference. A safely classified plan, queued-unit, inline-run,
  or servable-pointer authority affects exactly its fixed family surface. An immutable authority whose
  family cannot be classified safely affects exactly the sorted four-surface tuple
  `semantic_inline_table_run`, `semantic_projection_plan`, `semantic_projection_unit`,
  `semantic_servable_pointer`; tests reject omitted/duplicate/reordered members and every Stage-0 or old
  three-surface fallback. A/B concurrency tests block two authorities on the
  same surface in either transaction order, page both memberships, preserve both protected cases, and
  independently requeue either without losing or misattributing the other. Aggregate conflicts retry and
  leave authority/case/membership/all aggregates unchanged. After a block, that invocation continues through later returned
  candidates; tests place a valid unrelated authority after poison and prove it delivers. Eventual-consistency tests prove a delayed new entry is retried
  later and stale delivered/blocked entries become strong-read no-ops. Death at observation-before-send,
  send-before-mark, block-before-aggregate, aggregate-before-observation, and mark completion converges
  through the atomic aggregate item, scheduled fixed-key reread, durable state, and queue idempotency.
  Deployed acceptance creates lifecycle, ordinary owner-settled usage, unavailable-evidence, transient-
  failure, and poison authorities; reads lifecycle's write-once `authority_observed_at`; observes each
  valid FIFO/projected union member; proves poison does not stall later unrelated outboxes; and confirms
  delivered/blocked items retain immutable payload indefinitely while only sparse keys are removed. It
  observes the Maximum-gauge alarm, deterministic report-ID structured error, and aggregate surface
  `incomplete|unavailable` evidence with no source/internal IDs even after injected producer death
  immediately after block. Scheduled recovery rereads the fixed aggregate, repeats an unreported report
  harmlessly, and conditionally marks only its exact source sequence/report ID after all three emissions.
  No Stage-1 item has a TTL attribute and no DynamoDB TTL/sweeper is enabled.
- Recovery and API tests prove summary exposes only `open_operational_case_count`; the case-list route
  requires `limit=1..100`, accepts only an optional opaque cursor, performs `ConsistentRead=true` Query
  on `STAGE1_OUTBOX_OPEN_CASES`, paginates A/B concurrent cases, returns only
  `OperationalDataFlowCasePage.v1`, and is denied every `CASE#*`/authority read, scan, and mutation.
  UI/API/ordinary CLI/logs never reveal or accept protected key/digest/kind.

  CLI/bootstrap tests require all seven flags, reject default profile and implicit values before client
  construction, and prove the ambient session creates only STS/SSM/CloudFormation clients; calls caller
  identity, exactly four named SSM paths, and exactly the DataV2/OperationalStatusStore/
  OperationalStatusProcessor stack descriptions; verifies unique owner/account/partition/region and
  unqualified role/function ARNs; and assumes exactly the verified role for 900 seconds with an
  operation-derived bounded session name. The temporary session creates only Lambda and synchronously
  invokes the exact verified unqualified function. Tests reject missing/renamed/duplicate parameters,
  ownership mismatch, foreign/malformed/qualified ARNs, ambient invoke, direct table access, second
  assume, resource guessing, alias/version, fallback, expired credentials, and malformed result.

  Function tests strongly read the exact protected case, derived safe membership, authority, and all
  affected aggregates; then atomically change authority blocked→pending, protected case
  blocked→requeued with full audit, conditionally delete membership, set the exact generation-specific
  marker, and decrement/increment each surface's blocked/requeued-pending counters. For each affected
  surface, tests pin `(blocked_dependency, blocked_invalid, requeued_pending)` through the complete
  dependency sequence `000→100→001→010→001→000`: first dependency block, requeue, invalid reblock with
  a fresh case/membership, second requeue, and successful delivery. Symmetric invalid-first and
  dependency-reblock cases, including same-class reblock, prove the mapped blocked bucket changes while
  total gauge remains one until final delivery and then becomes zero. Every step advances source
  sequence, installs a new report identity with `report_state=pending`, and emits or retries the
  corresponding observation/structured report; report completion conditions only its exact source
  sequence/report ID. Tests prove aggregate conflict or injected failure at every transaction member
  leaves authority, old/new cases, membership, marker, and all surfaces unchanged; no partial member
  can commit.

  Replay/race tests require the exact marker and aggregate snapshots: stale-generation block or delivery,
  changed marker/surface tuple, underflow, missing aggregate, and caller-supplied delta/surface input
  fail closed. Concurrent delivery versus reblock permits exactly one complete transaction; the loser
  rereads to exact delivered/blocked replay or stale authority and cannot decrement pending twice.
  Requeue replay returns only the closed identical `already_requeued` outcome before delivery; replay
  after pending consumption or terminal delivery rejects. The old case remains `requeued` audit, every
  reblock uses a new opaque case reference, and no case reopens. Credentialed IAM proves the operator
  role is invoke-only with zero DynamoDB, while only the function role can perform the minimum direct
  reads and transaction actions. Separately, the provider-attempt reconciler uses only its sparse GSI
  query defined by the write-discipline design. No repeated-poison change adds IAM actions, resources,
  principals, or leading-key scope.
- Contract tests prove canonical UUID validation, ordinary new-owner UUIDv4 allocation, the explicitly
  named `DynamoStage1LifecycleHeadStore` UUIDv5 exception, atomic identity persistence, exact replay
  preservation, retry execution lineage, transition predecessor fencing, parent/causation relationships,
  initiation receipts, and measurement-version/observation projection. Owner persistence and projector
  recomputation consume the same committed Unicode/map-order vector and independently reproduce exact
  canonical bytes `{"a":["\u6771\u4eac",1],"b":"\u00e9\ud83d\ude00"}`, length 49, hex
  `7b2261223a5b225c75363737315c7534656163222c315d2c2262223a225c75303065395c75643833645c7564653030227d`,
  and digest `e5538692fcf9b5b794a29e642174ec8d27eb8027187a517d26432456c3dc599c` from both insertion
  orders. Flow, execution, event, topology-source, and outbox-order digest tests each consume that vector
  through `canonical_authority_digest` and prove owner/projector helper agreement; none may carry a
  copied expected encoder. Repository-encoder fixtures pin the literal plan, unit, inline-run, pointer,
  and execution-source JSON bytes, SHA-256 digests, unit-separator name bytes, and resulting UUIDs
  printed in §6.2. They reproduce exactly pointer flow `e4de2f19-58db-5f51-b924-b4e5118d4db3` and
  queued, inline, automatic, transitional, manual, and reprocess executions
  `bf1bc15f-7b72-54db-8c7a-50682ce6810e`, `6f03579d-6e36-5f49-a906-e69c979df18e`,
  `7251cadf-6717-57b7-836e-fe92905d346c`, `816f564b-1294-5293-939f-536f6ba247cf`,
  `9b38b5d0-eb39-5686-ae6a-0893727e3a04`, and `1a45fed2-aada-5d0c-bdfa-f22b1874c34f`.
  Fixtures reject one-byte, source-kind, namespace, tuple-order, separator, and
  persisted/recomputed drift; every unlisted `internal_pointer_execution_id` source; and all twelve
  cross-family tuple/surface bindings. The internal source kind/ID must affect immutable payload
  equality (`immutable_payload_digest`) and the derived pointer execution UUID — never the event
  `authority_digest` tuple — while remaining absent from public events/APIs, FIFO bodies, spans, logs,
  metrics, and
  status keys.
- `stage1-topology-v1` acceptance constructs one valid `Stage1TopologySource.v1` for every mapping
  row: first automatic plan root; later and compound-successor plan-recorded events; every admitted,
  admission-failed, and terminal plan event; first and later unit starts plus every other unit event;
  first transitional/manual/reprocess inline roots plus every later inline event; queued/inline same-
  transaction pointers; and every zero-row semantic-attempt pointer. Root fixtures pin each exact
  discriminator and absent relationships/digest. `prior_owner_event` fixtures pin complete old-head
  bytes, parent copied exactly including absent parent, causation equal to `head.latest_event_id`, and
  the exact `stage1-topology-prior-owner-event/v1` digest. `plan_admission` fixtures pin exact plan
  `META`, complete plan head, admitted event, plan parent, causation, and exact digest; the first unit
  predecessor remains absent. Same-transaction fixtures pin exact terminal owner event and forbid a
  digest; semantic-attempt fixtures pin complete ordered `_attempt_groups`/`map_attempt_history` bytes
  and the exact `stage1-topology-semantic-attempt-success/v1` digest.

  Cross-variant negative fixtures add every forbidden field, omit every required field, combine a root
  discriminator with source proof, substitute prior-owner proof for plan admission or same-transaction
  proof, use the wrong owner/plan/admitted/terminal event, truncate/reorder source bytes, supply a caller
  UUID/digest, or mismatch persisted/recomputed identity. Every case fails before mutation with no root
  fallback. Head tests independently derive predecessor/sequence/execution fields from the same old-head
  proof and reject any disagreement with topology derivation. Queued and inline same-transaction pointer
  fixtures assert that non-null parent and causation do not enroll a pointer in child reduction or
  counting. Pointer retry fixtures prove same-receive queued rebuild reuse, no-event transaction abort
  followed by legal queued reclaim, same-RUN inline reuse, different ID only for a separately accepted
  RUN while the pointer event is absent, and zero-row new attempt only after a full abort with no terminal
  success/publication/event. Once `EVENT` exists, delivery/newer-pointer replay reuses it forever and any
  source-kind/ID/public-UUID mismatch fails.

  Reverse-delivery tests park a pointer event arriving before its exact parent terminal event, leave that
  pointer flow's applied history/snapshot/aggregate unchanged, then drain it once when that parent
  arrives; duplicate and replay delivery reduce once. They assert that drain writes no child-outcome
  history, generation head, latest selector, parent child count, parent measurement, or parent objective.
  Protected-ID canaries prove no internal ID becomes a public UUID. Live acceptance selects each public
  flow UUID, verifies the exported trace, and matches its structured terminal record on that same public
  UUID and persisted topology. Adding a future durable delivery parent fails contract/version checks
  until a separately versioned topology migration is accepted.
- Stage-1 mapping tests cover a first recorded plan, later same-flow plan events, and the planned-
  predecessor compound pair. Every plan row binds only `semantic_projection_plan`, every queued-unit
  row only `semantic_projection_unit`, every inline-run row only `semantic_inline_table_run`, and every
  servable-pointer row only `semantic_servable_pointer`; table kind cannot select a surface, and any
  Stage-0 or cross-family binding fails before persistence. Plan roots declare only their sorted required
  queued-unit IDs and an empty optional tuple. Every unit and inline root declares empty required and
  optional tuples; pointer topology never changes those bytes. Tests prove plan persistence commits the
  recorded event/head before transport and that Phase 1 contains no synchronous producer invoke,
  delivery wait, or status poll. Finalization interruption fixtures prove the unit/run terminal, pointer
  authority, headless pointer `EVENT#`, and all outbox entries commit atomically even though no pointer
  child projection exists; no standalone pointer or terminal can survive an aborted transaction.
  Pre-switch compatibility acceptance proves no legacy Stage-1 lifecycle authority exists without the
  named exception's persisted UUID/head proof; projection rejects rather than adopts or repairs one. For
  the compound pair they require terminal `N+1` to retain the predecessor execution and its prior
  execution predecessor, recorded `N+2` to use a new execution whose predecessor is the superseded
  execution, and the event predecessor chain and final owner head to be contiguous. Tests deliberately
  deliver recorded/admitted/failure sequences and compound `N+2`/`N+1` in reverse order; successors park
  and cannot enter applied history or aggregates until the exact predecessor arrives, after which each
  event reduces once. One event without the other, an intermediate/split head, changed UUID/payload/
  predecessor, a branch, or an occupied sequence is rejected and alarmed; the projector never allocates
  or repairs either execution.
- FIFO tests prove root application against absent snapshot, duplicate delivery, projector retry,
  poison-message DLQ behavior, explicit redrive, and convergence after SQS deduplication has expired.
  Owner sequence/predecessor identity—not producer arrival order—is the ordering authority. A successor
  delivered before its withheld predecessor is durably parked and acknowledged without advancing the
  flow snapshot, applied-history API, or surface aggregate; applying the predecessor drains the exact
  contiguous sequence and creates each applied record and contribution once, without a DLQ cycle.
  A chain beyond one transaction continues in the same invocation when budget permits and otherwise
  resumes from the scheduled `PENDING`-index sweep without another source event.
- Atomicity tests interrupt every arrival disposition boundary and prove there is no `RECEIVED`-only
  state. An exact pending retry acknowledges only while its predecessor remains absent; once contiguous,
  it resumes reduction before acknowledgement. Applied entries are conditionally removed from the live
  `PENDING` index while immutable pending receipts remain retained.
- Event replay is durably idempotent; parked or stale transitions cannot regress a snapshot;
  conflicting predecessor/sequence and over-bound gaps are refused and alarmed rather than blindly
  retried.
- Concurrent independent flows write idempotent sharded contributions without contending on one surface
  cursor; reducer replay produces the same aggregate regardless of cross-flow arrival order.
- Aggregate-observation tests admit only the closed surface message, preserve monotonic source sequence,
  reject customer/per-flow identity, and make missing or stale transport/compute/provider evidence
  explicit in the summary without granting the API direct CloudWatch access.
- Reconciliation restores a deliberately withheld predecessor through the same FIFO path, drains its
  parked successor, and marks an unreadable authority `unknown`.
- Protected-content and customer-data canaries never appear in status events, queue/DLQ bodies,
  projection items, API responses, metrics, logs, or traces.
- Each exact route denies absent/wrong entitlements before projection access; rendered IAM has no scan,
  mutation, raw-data, transport, log, trace, or authority-table access.
- Definitions enumerate every UUID relationship and every state/reason/action returned by summary,
  flow, or event routes; server-owned readings exist for each code.
- Operator cold-start acceptance completes the five documented workflows with `flow status`,
  `flow watch`, and `flow explain` without repository or AWS topology knowledge.
- Required-child acceptance is scoped to Stage-1 plan→required queued-unit outcomes. It pins
  `child_outcome_reduction_version=2`, the exact
  `FLOW#<parent>/CHILD_OUTCOME_V2#<child>#<20-digit-sequence>` history key,
  `FLOW#<parent>/CHILD_OUTCOME_GENERATION_HEAD_V2#<child>#<20-digit-owner_generation>` completion-head
  key, and `FLOW#<parent>/CHILD_OUTCOME_LATEST_V2#<child>` current-API selector key. It pins this exact
  Stage-1 required-unit tuple hashed only by the existing canonical authority digest helper:
  `["operational-data-flow-child-outcome/v2", parent_flow_id, child_flow_id, child_event_id,
  child_transition_sequence, child_execution_id, owner_generation, lifecycle_state,
  completion_outcome]`. It proves transition sequence and owner generation are JSON integers while only
  their respective keys use exact 20-digit text. History and generation head carry exact child event/
  execution IDs, `owner_generation`, lifecycle state, completion outcome, referenced history key, and
  digest; the selector additionally references the generation head. The generation-head and selector
  keys are not digest members, so the committed 226-byte tuple and digest fixture remain unchanged while
  their exact new key strings are pinned. Negative fixtures reject every omitted/reordered member, UUID/
  value substitution, generation omission/null/prior/future/string/float drift, transition integer/
  string/float drift, and malformed or mismatched fixed-width key text.

  Acceptance proves byte equality from generation head to retained history and, when present, from latest
  selector to generation head. It covers first generation-head creation, multiple immutable terminal
  failure/success revisions within one required-unit generation, distinct G and G+1 heads, exact replay,
  and strict old-head compare-and-set fencing. Every admitted required-unit reduction transaction
  contains one history create, one generation-head create/update, and zero or one latest-selector
  create/update, with all operations included in transaction capacity/item-budget fixtures. Queued and
  inline pointer terminals are rejected from child reduction before budgeting and create none of those
  records. Same-sequence payload drift, occupied history, stale writer, competing writers, branch, gap,
  regression, head/history mismatch, key/payload generation mismatch, and changed parent, child, event,
  execution, generation, lifecycle state, completion outcome, timestamp, or authority digest fail closed
  without partial mutation. Selector fixtures permit only a higher `(owner_generation,
  child_transition_sequence)`, fence exact old bytes, and prove a loser retries without regressing
  another generation. Reverse-order delivery parks through normal lifecycle ordering; a bounded drain
  submits one contiguous admitted terminal outcome per transaction and never targets the same generation
  head or selector twice in one transaction.

  Stage-1 plan-root tests persist and reattest `child_outcome_reduction_version=2` plus a canonical
  sorted `required_child_expectations` map containing exactly every required queued-unit flow ID mapped
  to the plan generation, using exact plan `META`, plan event, and unit-owner authorities; every entry
  directly derives the exact generation-head key and the public event wire contains only its existing
  required child ID tuple. Plan optional tuples are empty. Missing, null, unsorted, partial, extra,
  pointer-bearing, mismatched, negative, over-width, or nonderivable maps and unit generation mismatches
  fail. Unit and inline roots reject every nonempty required/optional child tuple and every expectation
  map. Non-Stage1 parents must present their own explicit versioned expectation authority; a missing
  contract or silent null generation is rejected.

  Plan-gate tests park only an early plan `completed` event through atomic immutable/live awaiting
  disposition and make initial evaluation, exact retry, generation-head-triggered reevaluation, and
  scheduled reconciliation use exactly one strongly consistent root/snapshot read plus one bounded
  strongly consistent `BatchGetItem` of expectation-derived required-unit generation heads. They reject
  latest-selector/shared-head reads, optional pointer-head reads, scans, queries, per-child loops, v1
  marker reads, history walks, undeclared/missing/malformed heads, count-only completion, wrong-version
  expectations, and any expected generation/sequence/event/history-key/digest changed after the read.
  Plan completion applies only when every required exact-generation unit head is eligible-successful,
  and the transaction conditions the complete root/snapshot and exact head bytes before deleting the
  live wait entry.

  Two decisive overtaking fixtures run with duplicate and reverse stream delivery plus transaction
  retry. In the first, successful G exists before the G+1 parent completion: G+1 reads only its absent
  head and remains awaiting even if the latest selector shows G success, then releases only after an
  eligible G+1 child outcome. In the second, successful G and its immutable history/head exist, G+1 then
  advances its distinct head and latest selector, and delayed G parent completion still reads G's exact
  head and releases. A scheduled-recovery variant withholds the delayed-G parent/child trigger, runs the
  reconciler, and proves the same bounded G key releases without a query, scan, history walk, selector
  regression, or another G event. Concurrent child update versus parent release and all race losers
  converge through exact re-read or fail stale, never through another generation's authority.

  Current API count/visibility fixtures for Stage-1 plans derive only required-unit counts from
  `CHILD_OUTCOME_LATEST_V2`: a higher G+1 unit terminal replaces G in that current view, current failure
  is terminal but unsuccessful, and missing/malformed selector evidence is unknown. Plan optional counts
  are zero; unit and inline required and optional counts are all zero. Pointer outcomes remain visible by
  their own flow IDs and exact topology but cannot change child counts, measurements, objectives, retry,
  release, or historical reads and have no child latest selector. Parent-bound measurements, objectives,
  retry, release, historical reads, and historical API/objective samples for plans use
  expectation-derived required-unit generation heads and never the selector. A plan `failed`, `blocked`,
  or `superseded` terminal remains immediately visible regardless of required-unit state; later
  expected-generation success may release an awaiting plan but never rewrites an immutable historical
  observation or objective miss.

  Migration acceptance stops v1 reduction and rebuilds v2 history, one generation head per observed
  required-unit generation, and at most one greatest-tuple API selector per required unit only from
  immutable applied public event history, immutable plan declarations, and exact
  `required_child_expectations`; Stage-1 generation values come only from plan/unit owner authority.
  Pointer events and topology links remain in ordinary flow history but are skipped by child migration
  and create no expectation, child history, generation head, or selector. It verifies every applicable
  plan→required-unit expectation, including retained predecessor generations, resolves to a byte-valid
  generation head before cutover and each selector references the greatest tuple. Rebuild replay is a
  no-op; conflicting v2 bytes, superseded shared-head dependence, unresolved awaiting work, missing
  expectation authority, or a live v1 writer blocks cutover. There is no v1/v2 dual read, dual write,
  marker adoption, shared-head fallback, silent-null generation, optional pointer expectation, or mixed
  release. After the v2 writer starts, v1 markers and shared heads are ignored permanently. Public
  event/API schemas, routes, status vocabulary, and wire version remain v1 throughout.
- Completion conformance rejects submission, heartbeat, process exit, provider acceptance, queue
  acknowledgement/deletion, projector acknowledgement, and last-good continuity as proof of the new
  flow's completion. Retry preserves the original clock. Observation-only mode returns descriptive
  evidence without attainment vocabulary; evaluated-mode conformance additionally proves late recovery
  remains missed and unknown evidence never enters the attainment numerator.
- Snapshot polling recovers from disconnect; any later SSE path only invalidates and reconnects through
  the snapshot.
- Observation performance acceptance reports projection/reconciliation lag, measurement-versioned
  descriptive distributions, outcome/evidence populations, retry counts, categorized usage,
  continuity-versus-freshness state, and evidence completeness without UUID metric dimensions or
  customer measurements. Evaluated activation adds objective-versioned attainment distributions,
  unknown/not-applicable rates, and burn behavior.

Local risks remain: authority streams and logs have finite retention; enabling new streams can change
cost and deployment behavior; operational-status projection metadata has its separately owned retention
policy; and exact flow/execution/event/parent/causation identity is not yet persisted consistently across
publisher, insight, and evaluation authorities. This capability does not activate or own Stage-1 source-
authority retention; the write-discipline contract owns that rule, and tuning it is a future review
trigger rather than this capability's acceptance or pending-owner blocker. Each affected registry entry
remains incomplete until it can mint or deterministically preserve the full identity contract; no
surface may be omitted.
