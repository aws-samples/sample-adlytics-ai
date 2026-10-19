# Load Path — write discipline (low-level design)

| | |
|---|---|
| **Type** | Capability low-level design companion |
| **Status** | Accepted 2026-09-19; implemented as Stage 1 of [ADR-075](../../decisions/adr-075-generate-page-artifacts-once-per-dependency-cohort.md) and live-accepted on dev 2026-09-21 for single-day deliveries ([validation results](../../../validation/RESULTS.md), ADR-075 Stage 1 write discipline accepted on dev entry). The queued writer topology (`projectionHandoffState: 'QUEUED_ACTIVE'` in `infra/lib/config/index.ts`) is active on dev since 2026-09-22 UTC, the same day wide-window acceptance closed ([validation results](../../../validation/RESULTS.md), Stage 1 queued writer topology switched on and wide-window acceptance closed entry). Mandated by ADR-075 §6, whose amendments record this design's refinement history. |
| **Owns** | The two-phase semantic ingestion mechanism: authority-first publication, table-scoped Iceberg write units, FIFO admission, the per-table lease fence, merge consolidation, single verification per unit, write-plan lifecycle, and atomic projection finalization with the servable projection pointer |
| **Parent** | [Load Path architecture §5a](architecture.md#5a-write-discipline) |

[Load Path low-level design](low-level-design.md) owns interfaces, gates, and immutable-storage rules.
[Publication State](../publication-state/README.md) owns attempts, leases, publications, and `CURRENT`.
[Operational Data-Flow Status](../operational-data-flow-status/README.md) owns how the identities
carried here are projected for operators. [Data-Flow Completion and Service Levels](../../concepts/data-flow-completion-and-service-levels.md#clock-contract)
owns what an *applied* transition means for clocks; [generation coordination](../../ui/generation-coordination.md#3-dependency-router)
owns how the router recognizes one. This document adds only the write mechanism.

## 1. Problem this design removes

The deployed development sidecar (`src/adapters/jobs/ingest_delivery/semantic_sidecar.py`) does,
per delivery and inside one Lambda invocation: parse once, then for each contract materialize the
source-aligned publication (immutable S3 object, then Iceberg projection with a verifying readback),
then for each publication group acquire a partition lease, materialize candidate projections, then
successful projections, then run the fenced pointer transaction. Every Iceberg projection runs a
readback, one or more batched merges, and a second readback, one after another, with Athena result
reuse disabled. The partition lease protects a partition; the Iceberg commit conflicts on the physical
table, so two deliveries that touch one table collide on commit at Lambda concurrency two (2026-09-19
measurement, [validation results](../../../validation/RESULTS.md)).

Three facts of the deployed path shape the replacement:

1. Immutable S3 bytes are the authority and are written before the Iceberg projection
   (`ReplicatedSourceAlignedStorage`, `ReplicatedCanonicalStorage` in
   `src/adapters/outbound/semantic_storage.py`). The projection is a derived, exactly verifiable copy;
   `.publication()` and `.rows()` read S3 only. A canonical S3 object carries its full envelope plus
   business rows. `source_records` always projects a physical envelope record before any source
   business rows; `canonical_records` projects only canonical business rows.
2. The fencing token is part of the publication model, not only of the pointer: `PublicationCandidate`
   and `SuccessfulPublication` carry `fencing_token`, `publish_candidate` requires a live `LeaseGrant`,
   the Iceberg envelope stores it, and `commit_group` conditions the lease check and the `CURRENT`
   forward condition on it. Publication authoring and the pointer transaction therefore stay under one
   live partition lease in one invocation.
3. Every publish decision — pack readiness, identity, partitioning, control binding, prepublication
   evaluation, stale split, dimension history — is computed without Iceberg readback.

Only the Iceberg projection leaves the delivery invocation. Authority (S3 bytes, attempts,
publications, `CURRENT`) is published exactly as today. `CURRENT` no longer by itself means
"queryable": a **servable projection pointer** on the `CURRENT` item records which publication the
Iceberg current table has attested, and every serving and routing reader reads that pointer. Authority
advances in phase 1; servability and unit attestation advance together in phase 2.

## 2. Two phases and a trailing servable pointer

**Component diagram: Load Path write discipline — authority and intent in the delivery invocation, plan admission by the scheduled reconciler, projection by table-scoped writers (component depth, development deployment).**

![Component diagram: two-phase semantic ingestion with table-scoped write units and atomic projection finalization. A delivery invocation on the standard ingestion queue parses once, writes immutable S3 authority, advances CURRENT, and submits one durable generation-independent plan intent. A scheduled PlanIntentReconciler claims each due intent, assigns the next generation, persists the write plan with its recorded lifecycle, and alone sends and admits one write unit per physical Iceberg table to a FIFO queue keyed by table. A table writer acquires the table lease, reads immutable objects, runs consolidated merges, verifies once, and uses one DynamoDB transaction to check the exact lease, advance required servable pointers, attest the unit, and increment the plan. It releases the lease only after that transaction and then attempts plan completion. Serving and the dependency router read the servable pointer.](diagrams/write-discipline.svg)

> **Takeaway.** Authority is published once under the live partition lease; each physical table has
> one committing writer; pointer visibility and unit attestation cannot separate because the exact
> table-lease owner performs them in one transaction.

### 2.1 Phase 1 — prove feasibility, publish authority, and submit the plan intent

> **Amendment, 2026-09-22 — dimension-history emission and delivery bucket-span bounds
> (ADR-075 Stage 1 wide-window feasibility).** Two roster-composition rules close the structural
> refusal class the 2026-09-21/22 wide-window acceptance measured (`M(u)`=11–61 against the
> 10-attempt unit cap for the three derived tables; 211 of 457 landed deliveries refused, every
> refusal typed and pre-authority).
>
> 1. **One dimension-history emission per delivery.** A canonical contract that targets a
>    changing dimension (`valid_from` in its target columns) contributes exactly one canonical
>    partition per delivery and scope — the delivery's **latest** measurement bucket. The
>    history builder folds the prior complete history plus every observation the delivery
>    carries for that scope in ascending bucket order, each bucket applying its own
>    `valid_from` boundary; the delivery's earlier buckets emit no dimension partition. This
>    multi-boundary ascending fold is the interpretation this amendment pins: the superseded
>    per-bucket fold combined prior
>    published history with only one bucket's observations, so within one wide delivery the
>    latest bucket's partition — the one `CURRENT` serves — never contained the earlier
>    buckets' version transitions. The
>    [Slowly Changing Dimensions](../../concepts/slowly-changing-dimensions.md)
>    complete-candidate rule is unchanged: the single emitted candidate is complete and
>    internally valid, byte-identical reprocessing converges per that concept's rule (and is
>    byte-identical under the forward-only cadence, where no later delivery enters the fold's
>    ≤-bucket-end eligibility set for an already-emitted bucket), and `CURRENT` serving
>    already selects the scope's latest window end. The superseded behavior — re-emitting the
>    full history into every bucket's partition — multiplied bytes, statements, and S3
>    authority once per bucket for zero information gain and alone accounted for
>    `M(u)`=45–61 on `dim_campaign`.
> 2. **Bounded bucket span at the landing seam.** Every landed delivery spans at most
>    **3 populated measurement buckets per derived physical table**, counted from the
>    delivery's rows: a bucket counts only when the delivery carries at least one row for it,
>    and a window position with no rows contributes no bucket. A wider source window — a
>    30-day trailing AMC re-pull, tenant onboarding, a recovery replay — lands as an ordered
>    oldest-first series of chunked deliveries, each with its own operation identity, manifest,
>    and immutable objects, which is exactly the shape of ordinary source cadence. The
>    landing/generation seam owns the split ([dev seeding](../../concepts/dev-seeding.md) for
>    fixtures; source acquisition for production connectors); this feasibility callback and the
>    §11 caps stay deterministic and state-blind — same bytes, same verdict, independent of
>    table state. The bound's arithmetic is pinned from the 2026-09-22 classification pass's
>    typed refusal events (300-event sample, per-unit clean-attempt requirements over ≤30-bucket
>    deliveries): `fct_ad_exposure` required ≤31 attempts ≈ 1 merge per bucket,
>    `fct_attributed_conversion` ≤61 ≈ 2 merges per bucket, and `dim_campaign`'s requirement
>    (median 45, up to 61) was the rule-1 history multiplication, not a bucket rate. A 3-bucket chunk therefore measures at
>    most 6 merges + 1 verification = 7 accepted attempts against the unit cap of 10 — headroom
>    of one extra merge per bucket — and stays well inside 32 per plan (a chunked delivery has
>    the shape of the ordinary 1–3-bucket deliveries that completed throughout the burst). The
>    bound is re-argued before any estate change that pushes a derived table past ~3 merges per
>    bucket — about 78 canonical rows per bucket per table, from the measured packing of the
>    §6 renderer at the derived tables' ~34-column grain, where ~26 rows fit one statement
>    under the 900-parameter ceiling then in force (34 parameters per row; §11's 2026-10-10
>    amendment removed that ceiling, so more rows now fit one statement and this bound is
>    conservative). Raising the
>    §11 caps instead was rejected: any wider window outgrows any cap, and the caps carry the
>    reviewed recovery envelope. Delta-aware measurement (measuring only records absent from
>    the table) remains a future lever needing its own review of the determinism trade, not
>    part of this amendment.
> 3. **Series ordering and mid-series failure.** Fact partitions are order-independent
>    (`CURRENT` is per scope and bucket), so chunked fact deliveries may complete in any order.
>    The rule-1 dimension fold is order-dependent: each chunk folds the previously published
>    scope history, so the series lands oldest-first and per-table FIFO under the QUEUED
>    topology preserves that order end to end. A chunk that fails terminally does not block
>    its later siblings. When the failed chunk's buckets fall inside the source's declared
>    `restatement_window_days`, its dimension observations are recovered by the restatement
>    cadence (the next run re-emits the window, chunked the same way). For a replay deeper
>    than the restatement window — the onboarding and recovery cases — that cadence never
>    returns, and a late redrive of the older chunk re-asserts its fact partitions exactly
>    while its dimension partition is never selected by latest-window-end `CURRENT` (the same
>    semantics an out-of-order ordinary delivery has today): recovering those version
>    transitions requires the operator to re-land a window that extends from the failed
>    chunk's buckets through the scope's current latest bucket, so the fold re-emits them at
>    a servable window end. A deep-replay chunk failure is therefore an operator disposition,
>    never a silent acceptance. The dev landing seam's re-land, the campaign-dimension seal, is
>    owned by [dev seeding](../../concepts/dev-seeding.md) §1.

Runs in the existing ingestion function on the existing standard queue. `_prepare_delivery`,
`_bind_controls`, and `_prepared_groups` first produce a side-effect-free `PreparedProjectionPlan`.
An explicit `validate_projection_feasibility(prepared_plan)` callback runs after sidecar prepare,
control binding, and group planning, but before the first partition/table lease acquisition, attempt
write, immutable-object write, publication write, or `CURRENT` mutation. Nothing before this callback
holds publication authority.

The callback knows the exact source-object roster, each object's exact projected source/canonical
record counts and projected partition counts, the exact group/member roster, the exact pointer
count per physical table, the closed automatic invocation kind, and every zero-row pointer's complete
typed attempt-history observation selected under the status-owned
[Operational Data-Flow Status § Stage-1 event projection](../operational-data-flow-status/low-level-design.md#stage-1-event-projection)
contract. It consumes the sole environment configuration, `AppConfig.ingestion: IngestionConfig`,
through the dedicated frozen accessors owned in §11; no broad `Settings`, domain object, port, handler,
or adapter-local constant may own or override these values. It calls the single §6 existing ordering
helper/packer for every unit, never a preflight-only implementation. If `M(u)` is the resulting deterministic merge
count for unit `u`, clean feasibility is exactly
`clean_attempts = Σ(u in complete admitted roster)(M(u) + 1) <= maxAcceptedAttemptsPerPlan = 32`;
the `+1` is exactly one final verification for each unit. Every `M(u)+1` must also be `<=10`. The sum is
over the complete roster that will be persisted, not only the current or largest unit, and is evaluated
before authority. An empty roster follows the existing no-plan path and contributes zero. Any over-budget
clean roster is refused before mutation even when every individual unit and transaction fits.

The callback calls the single §3.2 `Stage1DynamoByteOracle` with the exact same typed requests used by
each runtime builder. That oracle uses the production `TypeSerializer` to encode `META`, units, outbox
records, statement-intent descriptors, zero-row commit additions including pointer execution identity,
topology source/digest, and queued/inline finalization transactions. Missing, truncated, reordered, or
mismatched required attempt history is refused here before mutation; absence is never substituted as
root proof. Plan-intent feasibility encodes the exact frozen maps for `SubmitPlanIntentRequest`,
`ClaimPlanIntentRequest`, `PersistPlanRequest`, `ReschedulePlanIntentRequest`, `AdmitUnitRequest`,
`FailAdmissionRequest`, `FailUnitRequest`, and `CompletePlanRequest`. Every intent-creation transaction
puts one `META` plus all zero-to-15 `UNIT#` rows and consumes the exact owner snapshot: it condition-checks
an existing owner or optionally puts the absent-owner bootstrap. It has exact replay only. Ordinary plan
persistence contains at most 20 actions. A planned-plan replacement has exactly one owner update and may
contain exactly 100 actions; `CompletePlanRequest` may contain exactly 51 actions. The attempt-33
`FailUnitRequest` branch replaces its ordinary trigger with the successor intent `META` plus all successor
`UNIT#` puts; the trigger and the maximum-size `META`/`UNIT#` puts have independent action-count and byte
fixtures. If any maximum fixture exceeds its limit, preflight refuses before publication of the original
root authority. Exact replay and collision readback use the same frozen maps. The envelope keeps three quantities
separate: transaction action count; §3.2 `conservative_service_item_bytes` for every exact pre-state and
resulting target item; and complete `wire_json_bytes` for serialization/request-completeness calibration
only. It accepts at most 100 distinct-item actions, at most 400000 conservative service-item bytes for
any pre-state or result, and at most 4000000 summed per-target maxima across the transaction. Complete
request wire bytes are pinned but never used as proof of DynamoDB's 4 MB item aggregate. The same proof
covers the one simultaneous §11 profile: 15 units, 4 group members,
37 pointers/object references for the current unit, at most 10 reserved or accepted attempts for that
unit, at most 32 across its plan, 200000 statement-envelope bytes, 128-byte internal
identities, and 256-byte partition identities. This profile is one constructible workload, not
independently combinable maxima. The clean current unit's 37
references must pack through §6 into at most nine merge statements under the byte bound, followed by
exactly one verification; the complete-roster formula above must also pass. Configuration/startup fails if any operation-specific frozen maximum exceeds an action or
application service-item bound, if any frozen one-beyond is accepted, or if a production builder differs
from the independently reviewed request/result maps. An in-range request whose distinct-target aggregate
is over the application envelope is likewise refused before mutation, with no split fallback. For an identity minted only after lease acquisition, its constructor
publishes a maximum encoded length and the callback substitutes that conservative bound; every
constructor rejects a larger identity. The same bounded group/member limit is used for operator no-plan
finalization. Any infeasible delivery is refused deterministically here, before any authority mutation.
Every later conditional-conflict rebuild selects one of the pre-encoded branches within this envelope.
Once this callback accepts, neither plan persistence nor queued/inline finalization may return a
permanent capacity refusal; an encoding outside the proof is an invariant defect and retries without
advancing additional authority.

After feasibility succeeds, `_publish_prepared_group` retains `_GroupAcquisition`, `_candidate_stage`,
`_completed_group`, quarantine, stale skip, `validate_group`, and `commit_group`, with these rules:

1. **The switched Lambda path is S3-only.** `s3_only_semantic_storage(...)` implements the source and
   canonical write ports with S3 materialization and attestation only. It runs no Athena statement.
   The inline composition used before the final switch and by operators follows §§5–6 and §9.
2. **`CURRENT` is an `Update`, not a `Put`.** It sets authority attributes under the existing
   first/supersede conditions and never names the three servable attributes on a non-zero supersede.
   A first non-zero publication uses the composition seed: S3-only uses `("NONE", 0, now)` and an
   inline projection uses the publication identity and token after projection.
3. **Projection cardinality is exact.** Every source object joins a source unit because
   `source_records` emits one envelope record even with no business rows. Candidate/current canonical
   objects with no business rows emit no `canonical_records`, join no canonical unit, and contribute
   zero canonical records.
4. **Zero-row current publication is atomic authority and servability.** For a successful canonical
   publication with zero projected current records, `commit_group` requires the exact typed, complete
   append-only attempt-history observation accepted by the status-owned
   [Operational Data-Flow Status § Stage-1 event projection](../operational-data-flow-status/low-level-design.md#stage-1-event-projection)
   rule. It applies the existing `_attempt_groups`/`map_attempt_history` semantics and asks the linked
   status contract to derive the required public topology and pointer execution-source proof from the
   same terminal successful attempt; no caller identity or locally reproduced enum is accepted. Missing,
   truncated, reordered, or publication/partition/instant-mismatched history fails before mutation;
   there is no root fallback. The publication identity, fencing token, and `projected_at` are then seeded
   in the same `CURRENT` authority update/`commit_group` transaction, never a follow-up. That transaction
   also creates the deterministic publication-partition `semantic_projection_servable` event defined in
   §10 with transition version, exact topology, pointer outcome, and pending delivery state. Event and
   pointer both commit or neither commits, and feasibility includes the complete typed observation,
   digest, action, and `maxGroupMembers` bound.
5. **Envelope attestation admits the servable attributes** (§2.3).

After all groups reach their terminal state, the handler uses the already-proved unit set and:

6. **Submits the complete plan intent before transport.** The handler constructs the sole canonical
   intent authority defined in §3 from the exact origin and complete zero-to-15-unit roster. Pending
   intent identity and state are generation-independent: `SubmitPlanIntentRequest` carries no generation
   and its `META` stores none. The request carries the exact root/source plan keys, plan digest, origin
   kind and source identity or failure transition, and the complete canonical roster. Its transaction
   puts the intent `META` and every `UNIT#` row and consumes the exact owner snapshot by condition-
   checking the existing owner or optionally putting the absent-owner bootstrap. `META` plus all `UNIT#`
   rows must reconstruct byte-for-byte the same `intent_authority_tuple`; exact replay requires every
   byte, and any occupied key with different bytes is a collision. The same complete tuple reuses the
   same intent. Any different grouping, unit key, table kind, merge count, object kind/reference,
   checksum, publication identity, partition key, fencing token, origin, or plan identity creates a
   distinct `intent_id` even against the same unchanged owner snapshot; each remains independently
   claimable. The source-delivery message acknowledges only when the mandatory strongly consistent
   classifier in §5.3 proves one of its four complete outcomes. Before the intent commit or on any
   inconsistent or partial readback, the handler raises and leaves the source unacknowledged; a durable
   handoff may be replayed and then acknowledged. The retained intent remains independently recoverable
   by `PlanIntentReconciler` after the source message reaches its DLQ.
7. **Claims and persists one plan before transport.** `PlanIntentReconciler` queries
   `stage1-plan-intent-ready` with partition `ready_status="ready"` and sort bound
   `ready_order <= <20-digit now>#~`, then strongly proves each result is an exact due
   `state=pending` intent `META` with `ready_order=<20-digit due epoch-micros>#<intent_id>`.
   `ClaimPlanIntentRequest` CASes the monotonic owner `revision` and intent snapshot atomically,
   computes `claimed_generation=owner.last_generation+1`, sets exactly `reserved_intent_id`, the
   byte-identical `reserved_intent_digest` (both equal `intent_id`), and `reserved_generation` on the owner, changes the selected intent
   `pending→claimed`, stores the same `claimed_generation`, and removes `ready_status` and `ready_order`.
   `ReschedulePlanIntentRequest` never invents or changes generation while the intent remains claimed.
   A claimed→pending retry clears the exact owner reservation,
   removes `claimed_generation`, and restores canonical retry, due, ready, and audit fields; a later
   claim assigns the then-current `owner.last_generation+1`. `PersistPlanRequest` derives
   the §3 plan-materialization internal execution identity from `intent_id` plus `claimed_generation`
   and the normal plan key from that identity. It requires exact
   equality among `owner.reserved_generation`, intent `claimed_generation`, and
   `owner.last_generation+1`, creates the plan `META` and units, and atomically swaps the owner. The
   owner sets paired `active_plan_key`/`active_intent_id`, advances `last_plan_key` and
   `last_generation` to `claimed_generation`, advances `revision`, clears all `reserved_*` fields, and
   stores `latest_internal_execution_id` byte-equal to the resulting plan `HEAD`. The persisted plan
   `META` carries `root_plan_key`, `generation=claimed_generation`, source/predecessor plan key,
   `origin_intent_id`, `roster_digest`, and complete reconstructable intent-authority bytes. For every
   unit it also derives the stable public unit `flow_id`; plan `META` and the plan-recorded lifecycle
   authority persist `child_outcome_reduction_version=2` and
   `required_child_expectations`, an unsigned-UTF-8-key-sorted map from each exact unit flow ID to
   `claimed_generation`. The map is internal authority, is reattested from plan `META`/event by the
   status reducer, and is never added to the public event wire. Every Stage-1 unit lifecycle authority
   carries `owner_generation=claimed_generation` copied from that unit's exact owner payload. Exact
   claim or persistence replay
   retains the assigned generation. Queue send starts only after persistence commits. At most one plan
   `META` has `status=planned`, and it is exactly the plan named by
   `SUCCESSOR_OWNER.active_plan_key`.
8. **Admits every active unit or reschedules the same intent.** `PlanIntentReconciler` alone performs
   every semantic-write queue send and admission. Each successful send is followed by
   `AdmitUnitRequest`, which changes that unit admission `pending→admitted` and increments the plan
   admission count exactly once. The last admission changes the intent `claimed→admitted`, retains its
   `claimed_generation`, records the admitted lifecycle transition, and leaves no ready fields. After every send, admission, terminal
   write, ambiguous response, and process-death boundary, source-delivery handling strongly rereads and
   applies §5.3; a transport or write return never authorizes source acknowledgment. Outcome 2 permits
   only the exact persistence replay. Queue send/admission continuation is independently replayable by
   `PlanIntentReconciler`, but until outcome 3 or another listed outcome is proved the source handler
   raises and leaves the delivery unacknowledged. If classification proves the exact
   `admission_failed` transfer, `FailAdmissionRequest` has atomically terminalized the candidate plan,
   cleared the owner's paired active fields, preserved owner/plan-`HEAD` agreement with the persistence-
   cleared reservation absent, and changed that same intent `claimed→pending` with its immutable origin
   bytes unchanged, removed `claimed_generation`, and restored the exact retry schedule,
   `ready_status="ready"`, and `ready_order=<20-digit due epoch-micros>#<intent_id>`. Already-sent unit
   messages acknowledge that terminal candidate; none may execute it. The next claim assigns the
   then-current owner `last_generation+1`.
   No admission failure creates another intent or fans out from a terminal predecessor.

An empty unit set creates no plan or queue message. Its zero-row `CURRENT` pointer and durable event
already exist in the same authority transaction. The receipt carries `write_plan_key = "none"`;
`PUBLISHED` continues to mean authority published.

A redelivery is a new execution (`sha256(message_id | receive_count)`), so attempt ids, lease token,
and publication ids differ. It re-publishes P′ with `supersedes = P` and submits an origin
`source_delivery` intent. Exact complete-authority replay reuses the intent; any changed canonical
intent member creates a distinct intent. Every submitted intent has consumed an owner snapshot and is independently
recoverable. `PlanIntentReconciler` alone claims due intents and persists the next generation. In-flight
old writers fail owner/plan conditions and acknowledge explicit plan replacement. P′ becomes servable
when its current-table unit finalizes.

### 2.2 Phase 2 — project and atomically finalize one table unit

Runs in `table_writer` with batch size one. For one unit:

1. **Claim.** Strongly read `SUCCESSOR_OWNER`, the plan `HEAD`, unit, plan `META`, and origin intent
   `META`; require exact owner/head agreement, `active_plan_key` equality, paired `active_intent_id`, and
   plan-generation equality. A unit belonging to a terminal or replaced plan acknowledges; incomplete
   admission is retryable; an attested unit evaluates plan completion. Otherwise `ClaimUnitRequest`
   conditions that exact owner-active/head-agreeing plan and generation, admitted origin intent, unit
   state, and writer receive identity, then sets `queued→writing` with its writer execution id,
   redrives `failed→writing` while decrementing `failed_count` exactly once, or reclaims a `writing`
   unit left by a dead writer (§8) under this receive's execution — only when the unit binds no
   nonterminal attempt and the prior holder's lease is absent, expired, or released, proven by one
   leases-table condition in the same transaction; a live prior holder makes the message retryable
   with `TABLE_BUSY` visibility and no lifecycle event, and a foreign `writing` unit still bound to a
   nonterminal attempt is retryable with `TABLE_ATTEMPT_PENDING` visibility and no lifecycle event
   until the reconciler completes that attempt. The leases condition is disjunctive and carries no
   observed pre-state, exactly like the absent-or-clear fence condition. Every branch creates
   `semantic_unit_started` with its atomic head effect. That lifecycle redrive decrement is not an
   accepted-attempt reservation decrement; permanent reservation counters never decrease. The event
   means only that this exact durable claim exists. It carries no lease holder/token and does not mean
   that Athena work may start.
2. **Fence.** Query the table-attempt registry (§5) before lease acquisition. Any nonterminal attempt
   returns retryable `TABLE_ATTEMPT_PENDING`; no statement starts. Otherwise acquire the table lease. The acquisition transaction proves the registry still clear and may create
   `semantic_unit_lease_acquired` under the plan key. That optional event means only that this claimed
   unit obtained the exact table lease holder/token while the attempt fence was clear; it does not mean
   a statement was submitted. `TABLE_BUSY` runs no statement and is retryable. Either refusal is
   recorded only through the unit tag of `RecordStage1RefusalRequest`, against the exact claimed unit
   and observed claim state; recording it does not alter that claim or any lease/fence state.
3. **Read authority.** Fetch every immutable object, verify its checksum, and decode a typed
   `ProjectionUnitSource`. Source decoding always supplies the envelope projection record even when
   its business-row collection is empty. Canonical decoding supplies only its business rows. A
   mismatch is an invariant failure and is never repaired from Iceberg.
4. **Consolidate and merge** (§6), sequentially.
5. **Verify once.** One readback compares every physical projection record in the unit with the
   immutable objects. A retry may first exclude already exact objects.
6. **Pre-resolve current pointers while the lease remains held.** For every pointer named by a current
   unit, strongly read `CURRENT`. If its projected token is already equal to or newer than the unit's
   token, record `projection_superseded` in the pending unit result and omit that pointer update. If an
   advance is still required, include an update whose condition matches the exact observed
   `projected_publication_id`/`projected_fencing_token` state (including `NONE` or the migration's
   defined missing state) and whose target token is greater. All writers of this field are monotonic,
   so an omitted equal/newer pointer cannot regress. A pointer conditional conflict rebuilds the
   whole transaction after strong re-read; it never converts a newer pointer into a unit failure.
7. **Finalize once.** `DynamoProjectionCoordinator` executes one DynamoDB transaction containing:
   - a condition check that `TABLE#<physical table id>` still has the exact holder and fencing token
     from step 2 and is unexpired;
   - every still-required pointer update from step 6;
   - the `SettleStage1AthenaUsageRequest` expansion for every referenced accepted Athena attempt,
     conditioned on exact terminal evidence and the now-durable unit/pointer disposition, changing each
     attempt `unsettled→settled` and putting exactly one deterministic categorized `USAGE#` contribution
     or unavailable-evidence authority;
   - the owned unit transition `writing→attested`, conditioned on the exact plan, unit, writer
     execution id, current lease token, and every referenced attempt being settled by this transaction
     or already byte-identically settled, recording physical row count, verified digest, lease token,
     ordered table-attempt references, pointer outcomes, and `attested_at`;
   - plan `META.attested_count += 1` plus the complete settled-attempt count/digest update, conditioned
     on plan `status = planned` and full admission — which immutably proves the origin intent's
     `admitted` state without a separate intent transaction member —
     `attested_count < unit_count`, the unit not previously attested, and the prior aggregate matching
     the exact strong-read attempt evidence;
   - one deterministic unit-finalized outbox item carrying the attestation and pointer outcomes from
     which the lifecycle projector emits `semantic_unit_attested` and any
     `semantic_projection_servable` records.

   Source and candidate units run the same transaction without pointer updates. The prepublication
   feasibility callback (§2.1) has already encoded the worst-case lease check, exact pointer count,
   attempt-settlement updates and Athena `USAGE#` puts, unit update, `META` update, lifecycle-head
   actions, and outbox items for every unit; finalization therefore has no permanent post-authority
   capacity-rejection path. A lease, unit, plan, attempt-settlement, or pointer conflict aborts all
   finalization effects and is retried from claim; a pointer conflict follows step 6's re-read/rebuild
   rule. There is no standalone settlement, `advance_servable`, then `attest` operational path.
8. **Release or park the table lease.** After step 7 succeeds or aborts with a clear `FENCE`, release
   in `finally` conditioned on exact holder/token. If §5 reaches the Lambda safety deadline without
   confirmed provider terminality, park rather than release; only provider-terminal
   `CompleteTableAttemptRequest`, ordinarily called by the reconciler for a parked attempt, may clear
   that exact parked safety state.
9. **Complete the plan.** After a successful finalization and lease release, `CompletePlanRequest`
   uses an at-most-51-action transaction to conditionally change plan `META.status planned→completed`
   only when `admitted_count = unit_count` — full admission immutably proves the origin intent's
   `admitted` state, so no completion member conditions the intent directly —
   `attested_count = unit_count`, `failed_count = 0`, every plan unit carries
   `owner_generation=plan.generation`, the persisted `child_outcome_reduction_version=2` and sorted
   `required_child_expectations` still reconstruct from those units, and every accepted Athena attempt
   referenced by every unit is already byte-identically settled; it sets `terminal_at`, clears the
   owner's paired active fields, preserves `latest_internal_execution_id` byte-equal to plan `HEAD`, and
   creates the deterministic `semantic_plan_terminal(status=completed)` outbox item carrying the exact
   internal expectation proof for status reattestation. The map is not added to the public event wire.
   `CompletePlanRequest` may not classify or settle work that a unit finalization omitted. The same
   operation is the sole completion path for an already-attested redelivery. A death after finalization
   but before completion is therefore recoverable without repeating pointer work; a death after
   completion but before event dispatch is recovered from the pending outbox item.

### 2.3 Envelope attestation and readers

The `CURRENT` item carries the authority attributes and, after Stage 1, the three servable attributes.
Two attestation rules apply:

**Authority pair (amended).** `DynamoPublicationStore` keeps its existing authority meaning and permits
the three servable attributes and any `staged#<grant_id>` entry as extra fields; exact replay excludes
them from authority item equality.

**Servable pair (new).** `DynamoCurrentPublications` resolves the servable publication. It reads
`CURRENT`; `projected_publication_id = "NONE"` is valid absence. Otherwise it reads the named immutable
publication under the same partition and requires publication id and fencing token equality. Every
serving and routing reader constructs this adapter; only sidecar authority compositions construct
`DynamoPublicationStore`.

Before §2.5 migration completes, an absent projected id is legacy and resolves to the authority
publication, truthful because every pre-switch publication is inline-projected. After migration,
missing is `PUBLICATION_UNREADABLE`: every `DynamoCurrentPublications` read except the retiring
`current_for_trigger` now refuses it, and resolves staged entries through their window pointers
([Publication State architecture §4](../publication-state/architecture.md#4-internal-structure)). The stream trigger rule is owned by
[generation coordination §3](../../ui/generation-coordination.md#3-dependency-router).

### 2.4 What does not move

Gate semantics, immutable byte formats, the publication model and fencing token, attempt event kinds,
`commit_group` member conditions, the partition lease, and serving's Iceberg current table are
unchanged. Serving learns a projected publication identity only from a zero-row identity seeded in the
phase-1 authority transaction or the atomic queued/inline finalization transaction.

### 2.5 Stage 1 rollout order

Tolerance and migration are already deployed in development; those two deployed slices do **not** have
the table lease or durable provider-attempt protocol. Stage 1 therefore uses four non-overlapping
slices. Tolerance and migration keep the delivery Lambda as the sole automatic inline projection writer;
`ingest_delivery` and `reprocess_semantic_v2` remain manual pre-fence inline operators. Pre-switch fence
adapts all three inline writers to the common table lease, durable provider-attempt protocol,
feasibility bound, lease-held inline finalizer, and status-owned identity contract while no queue writer
or S3-only automatic path exists. Before any amended writer or reconciler is enabled,
`table-attempt-reconcile` and `stage1-plan-intent-ready` must be read back `ACTIVE`; the latter must be
read back as `KEYS_ONLY` with partition key `ready_status` and sort key `ready_order`. An exhaustive
read-only all-page verification must prove that only pending intent `META` rows carry
`ready_status="ready"` and `ready_order=<20-digit due epoch-micros>#<intent_id>`, that claimed
and admitted intents carry neither field, that every row in the reserved `TABLE_ATTEMPT#` and
`PROVIDER_RECONCILE_BLOCK#` namespaces — the amended inline delivery writer is deployed and active
during the verification window, so canonical rows it created are legitimate — recomputes as exact
canonical amended-builder output. Canonicity is per closed form: a nonterminal pair must satisfy the
§3.1 `core_valid` predicate with canonical reconciliation metadata; a terminal `ATTEMPT` must retain
the same immutable core with attempt id, client token, and envelope digest reproduced byte-for-byte
from immutable fields, exactly one closed provider-terminal state, and its quantity/evidence fields in
their closed forms — provider-observed values validate against their closed field forms, never by
recomputation; a clear `FENCE` must be exactly the closed clear form with no reconciliation attribute;
a `RUN#` row must reproduce `run_id` from its immutable tuple and carry exactly one closed run state;
a `USAGE#<attempt id>` row must sit beside a settled terminal `ATTEMPT#` of the same attempt id with
its immutable settlement payload validating against the closed contribution or unavailable-evidence
field forms and the concept-owned category vocabulary — settlement quantities, like provider-observed
values, validate by closed form, never by recomputation;
and a `PROVIDER_RECONCILE_BLOCK#` case must reproduce its deterministic block id and digests. The
verification must also prove that no
reconciliation attribute named in §3.1 exists on any other row. Recomputation is provenance: no
pre-amendment writer of these namespaces ever existed, no pre-amendment principal holds write
authority, and a row that fails exact recomputation cannot have been produced by an amended builder.
Every page is read through completion;
a partial scan/query, pagination/read failure, malformed response, non-canonical reserved row, or stray
reconciliation
attribute blocks enablement. There is no adoption, repair, backfill, or compatibility mode. IAM and
credentialed denial prove every pre-amendment runtime/operator role lacks write authority for both
reserved prefixes; only the amended typed builders may ever create them, before or after the
verification.

The manual operators each occupy one row of the
[static batch start budget](../query-path/architecture.md#the-static-batch-start-budget), so their
concurrency is a runbook rule. Per environment, run at most one `ingest_delivery` process at a time
(row `ingest_operator`), and at most one process at a time from `reprocess_semantic_v2`,
`validation/probe_amc_current_invariants.py`, `validation/probe_ingestion_traceability_gate.py` and
`validation/probe_vendor_seed_invariants.py` (row `reprocess_operator`). Both rows are 1 and are
enforced by convention only: a second process on one row exceeds the budget, and the budget readback
reports both rows as `asserted: runbook`.

The verification has one owner: the read-only operator module `adapters.jobs.verify_stage1_preswitch`
with exactly three closed command modes — `verify-pre-switch`, `verify-edge`, and `verify-drain` — run
by the deploying operator through
the same two-session bootstrap contract as the other Stage-1 operator entry points against the dedicated
`Stage1PreswitchVerificationRole`. Its ambient session resolves and verifies exactly these typed
Config-owned parameter paths before assuming that role for 900 seconds:
`stage1PreswitchVerificationRoleArn` at
`/adlytics/{environment}/ingestion/stage1-preswitch-verification-role-arn`,
`semanticWriteQueueArn` at `/adlytics/{environment}/ingestion/semantic-write-queue-arn`,
`semanticWriteDlqArn` at `/adlytics/{environment}/ingestion/semantic-write-dlq-arn`, and
`deliveryFunctionArn` at `/adlytics/{environment}/ingestion/delivery-function-arn`, plus the
existing typed DataV2 publications-table parameter; it describes exactly
`Adlytics-DataV2-<environment>` and `Adlytics-Ingestion-<environment>`, verifies
account/partition/Region and exact resource ownership, and accepts no default, derived, enumerated, or
caller-supplied resource identity. `IngestionStack` owns that role; it is the sole named exception to
the no-`Scan` rule and receives exactly: read-only `dynamodb:Scan` and `dynamodb:GetItem` on the exact
publications-table ARN; `dynamodb:Query` on the three Stage-1 GSI ARNs; `dynamodb:DescribeTable` on the
exact publications-table ARN; `sqs:GetQueueAttributes` on the
exact semantic-write queue and DLQ ARNs; and `lambda:GetFunctionConcurrency` plus
`lambda:GetFunction` on the exact delivery function ARN only — table-writer quiescence is proven by
queue counts plus durable authority, so no table-writer Lambda grant exists. `verify-pre-switch` also
owns the required GSI readback: it proves each Stage-1 GSI `ACTIVE` with the exact pinned key schema
and projection through `DescribeTable` before its page verification, and a missing, backfilling, or
mismatched index is a blocking result. It has no write, index
management, other-table, other-service, or wildcard grant, and no runtime principal may assume it. Each
run reads every page to completion and emits one complete structured verification report — pages read,
per-check pass/fail, and every violating key — whose retained output is the §13 acceptance evidence for
the gate; a thrown error, partial page set, or any failed check is a blocking result. Wherever this
contract requires quiet "executions", execution evidence is exactly: the semantic-write queue and DLQ
visible plus in-flight message counts from `GetQueueAttributes`; the delivery function's rendered
concurrency from `GetFunctionConcurrency`; and durable nonterminal authority — nonterminal plan
`META`/unit state, nonterminal `RUN#`, nonterminal `ATTEMPT#`, and nonclear `FENCE` rows — read from
the publications table. No Lambda execution-listing or CloudWatch API exists in the grant set or the
contract; durable authority is the execution ledger. `verify-edge` owns the forward-edge evidence: for
target `INLINE_QUIESCED` it proves delivery-function concurrency zero and every automatic-writer
attempt terminal with its fence clear; for target `QUEUED_ACCUMULATE` it proves no old-version
delivery execution through the execution evidence above and zero Athena statements from the queued
delivery composition. That statement-ledger proof uses only persisted immutable facts: Begin
reservations are the sole statement path, every delivery-composition attempt is owner-bound to a
`TABLE_ATTEMPT#<table>/RUN#` item with immutable `writer_kind=transitional_delivery` and an immutable
creation instant, and the `QUEUED_QUIESCED` commit instant is the retained
`ACTIVE.last_transition_at` of that transition (readable from the handoff `STATE` row and its
`COMMITTED` audit). The proof is exactly: no `transitional_delivery` `RUN#` row has a creation
instant postdating that retained commit instant, no `ATTEMPT#` row is owner-bound to such a run, and
no `ATTEMPT#` row owner-bound to any `transitional_delivery` run carries a write-once `submitted_at`
postdating that same retained commit instant — closing the straddling execution whose run predates the
commit; a nonterminal straddling attempt is already nonquiet execution evidence; for target
`QUEUED_ACTIVE` it re-proves the same statement-ledger absence for the delivery composition. Each
edge transition is submitted only with its retained passing `verify-edge` report. `verify-drain` is
the sole §2.5 drain-observation enforcement point: it performs the strongly consistent all-clear reads
over the semantic-write queue and DLQ, delivery and table-writer executions, affected plans, attempts,
and both pending and blocked provider fences, owns the 300-second uninterrupted-quiet timer with its
reset-on-nonquiet rule, and re-runs the whole bootstrap above before every read after the first, so
no read outlives its 900-second session (a passing window needs at least two complete reads, and one
read of the full table can take most of a session); a re-run that resolves different targets is a
blocking result. Its passing report is a precondition the deploying operator must satisfy
before submitting the `QUEUED_DRAIN→QUEUED_QUIESCED` deployment. The handoff provider itself validates
only the state-machine edge under its pinned `PROJECTION_HANDOFF#`-scoped IAM and never reads queues,
functions, plans, or fences; a drain transition deployed without a passing `verify-drain` report is an
operator-procedure violation that the §13 acceptance record must surface, not a provider-side check.

The final switch is controlled only by one required typed tagged CDK input on the existing
`IngestionStack`:

```text
projectionHandoffState:
  INLINE_ACTIVE | INLINE_QUIESCED | QUEUED_QUIESCED |
  QUEUED_ACCUMULATE | QUEUED_ACTIVE | QUEUED_DRAIN
```

A private resolver accepts only that tag and derives the complete configuration; no caller can supply
its three controls independently:

| Tagged state | Projection composition | Delivery mapping | Semantic-writer mapping |
|---|---|---|---|
| `INLINE_ACTIVE` | `inline` | `true` | `false` |
| `INLINE_QUIESCED` | `inline` | `false` | `false` |
| `QUEUED_QUIESCED` | `queued` | `false` | `false` |
| `QUEUED_ACCUMULATE` | `queued` | `true` | `false` |
| `QUEUED_ACTIVE` | `queued` | `true` | `true` |
| `QUEUED_DRAIN` | `queued` | `false` | `true` |

Synthesis rejects a missing or unknown tag and any legacy or independent `projectionMode`,
`deliveryMappingEnabled`, or `semanticWriteMappingEnabled` input. The two scheduled reconcilers and the
`Stage1LifecycleProducer` stream mapping and recovery schedule are outside the three resolver controls
and are governed by one separate required typed boolean, `IngestionConfig.stage1RecoveryEnabled`,
consumed by both owning stacks: it renders the `Enabled` state of `ProviderAttemptReconcilerSchedule`,
`PlanIntentReconcilerSchedule`, the producer's publications stream mapping, and its reconcile schedule,
with no other trigger control, imperative toggle, or composition-derived gating. The pre-switch fence
slice deploys first with `stage1RecoveryEnabled=false` — creating `Stage1PreswitchVerificationRole` and
every function while all four triggers stay disabled — then, only after the retained passing
`verify-pre-switch` report, redeploys with `stage1RecoveryEnabled=true`. It remains `true` in every
later slice and handoff state, keeping inline parked-attempt recovery live throughout the `INLINE_*`
states; synthesis rejects any `QUEUED_*` handoff state combined with `stage1RecoveryEnabled=false`.
`IngestionStack` owns exactly two
event-source mappings and their rendered `Enabled` properties: the delivery-queue ARN → delivery-
function ARN mapping and semantic-write-queue ARN → `table_writer` function ARN mapping receive only
the resolver's derived booleans. The resolver alone selects inline replicated storage or queued S3-only
authority composition. The delivery-mapping control also renders the delivery function's reserved
concurrency: exactly `0` whenever the delivery mapping is disabled and unset otherwise, so
`GetFunctionConcurrency` returns exactly `0` in every quiesced and drain state and the
`INLINE_QUIESCED` evidence requires exactly that rendered zero. There is no Lambda mapping API
mutation, imperative mapping toggle, runtime
environment override, fallback writer, second mapping, or combination outside these six rows.

The retained publications table holds the sole deployment-handoff authority at
`partition_key=PROJECTION_HANDOFF#<environment>, record_key=STATE`. `protocol_version` is exactly `1`,
`revision` is a nonnegative integer and never resets, and `disposition` is exactly
`ACTIVE|PREPARED|ROLLING_BACK|DECOMMISSIONING|DECOMMISSIONED`. The complete closed forms are:

| Disposition | Required fields and meaning |
|---|---|
| `ACTIVE` | `protocol_version`, `disposition`, one six-state `state`, `revision`, current `stack_identity`, `last_transition_id`, and `last_transition_at` — the 20-digit UTC epoch-microsecond instant commit captured once immediately before its conditioned `STATE` write (bootstrap and every transition; replay preserves the retained value byte-for-byte). This is the only form that claims the six-state controls are deployed. |
| `PREPARED` | `protocol_version`, disposition, `mode=transition|bootstrap|recreate`, transition `token`, `requested_state`, `expected_revision`, new `stack_identity`, `stack_update_identity`, all three resolver controls, `prepared_at` (the 20-digit instant prepare captured once immediately before its conditioned CAS), and a byte-exact `prior_snapshot`. The snapshot is `ABSENT`, or the complete prior `ACTIVE`/`DECOMMISSIONED` row; this form has no active-state claim. |
| `ROLLING_BACK` | Every `PREPARED` field plus rollback owner and instant. It claims no target state and exists only until the exact prior snapshot is restored. |
| `DECOMMISSIONING` | `protocol_version`, disposition, prior six-state `state`, prior active `revision`, same `stack_identity`, prior `last_transition_id`, deterministic `decommission_token`, `decommissioning_at` (captured once immediately before the conditioned CAS), and the complete prior `ACTIVE` snapshot. It truthfully means stack deletion started but application resources may remain. |
| `DECOMMISSIONED` | `protocol_version`, disposition, `revision=prior revision+1`, deleted `stack_identity`, `decommission_token` as `last_transition_id`, `decommissioned_at` (captured once immediately before the conditioned write), and the complete prior active state/transition history. It has no `state` field and makes no active-state claim. |

The same partition retains immutable sibling records with
`record_key=AUDIT#<token>#<phase>`, where `phase` is exactly
`PREPARED|COMMITTED|ROLLING_BACK|ROLLED_BACK|DECOMMISSIONING|DECOMMISSIONED`. Each audit payload is the
canonical byte encoding of protocol version, environment, token, phase, mode, stack identity, from/to
disposition/state/revision, stack-update identity when applicable, the owning `STATE` transition
instant, and
complete resolver/prior-snapshot
facts. Every mutating disposition row persists its own 20-digit transition instant — `prepared_at` on
`PREPARED`, `last_transition_at` on `ACTIVE`, the rollback instant on `ROLLING_BACK`,
`decommissioning_at` on `DECOMMISSIONING`, and `decommissioned_at` on `DECOMMISSIONED` — captured once
immediately before its conditioned `STATE` write, and each audit copies the owning row's instant, so a
retry that recreates a missing audit reads the instant back from the completed `STATE` row and remains
byte-identical for every phase. `ROLLED_BACK` is the one phase whose owning `ROLLING_BACK` row no
longer exists when its audit is ensured; its audit instant is exactly the rollback instant retained in
the immutable `AUDIT#<token>#ROLLING_BACK` sibling — which the protocol guarantees exists before any
restore mutation — so the recreate-missing-audit retry reads that sibling and remains byte-identical
in every rollback mode, including first-bootstrap restoration to `ABSENT`. The provider creates an absent audit only by conditional `UpdateItem`; an occupied key must be
byte-identical. For every mutating phase the conditioned `STATE` write happens first; an idempotent
retry of that mutation conditions the completed state first. The callback succeeds only after the
corresponding audit exists. A retry that observes the completed mutation with a missing audit creates
that byte-identical audit and then succeeds; any mismatch fails. Same-state `replay` is a read-only
validation mode, not a mutating phase: it creates, updates, checks, or repairs no audit and leaves the
complete `STATE` item and audit keyspace byte-identical. Audit records have no TTL and are never deleted
or expired.

#### Closed CloudFormation callback protocol

`IngestionStack` deploys one private provider Lambda serving exactly three in-stack custom resources:
`ProjectionHandoffPrepare`, `ProjectionHandoffCommit`, and `ProjectionHandoffDeletionGuard`. The public
`projectionHandoffState` tag and six-state resolver above remain the sole target interface. The three
private property maps are closed; `ServiceToken` is the only CloudFormation-injected property:

```text
ProjectionHandoffPrepare.ResourceProperties = {
  ServiceToken, protocolVersion: 1, resourceKind: "prepare",
  environment, stackIdentity: {Ref: "AWS::StackId"}, requestedState,
  projectionComposition: "inline"|"queued",
  deliveryMappingEnabled: true|false,
  semanticWriterMappingEnabled: true|false
}
ProjectionHandoffCommit.ResourceProperties = {
  ServiceToken, protocolVersion: 1, resourceKind: "commit",
  environment, stackIdentity: {Ref: "AWS::StackId"}, requestedState,
  projectionComposition, deliveryMappingEnabled, semanticWriterMappingEnabled,
  mode: {Fn::GetAtt: [ProjectionHandoffPrepare, mode]},
  transitionToken: {Fn::GetAtt: [ProjectionHandoffPrepare, transitionToken]},
  fromDisposition: {Fn::GetAtt: [ProjectionHandoffPrepare, fromDisposition]},
  fromState: {Fn::GetAtt: [ProjectionHandoffPrepare, fromState]},
  expectedRevision: {Fn::GetAtt: [ProjectionHandoffPrepare, expectedRevision]}
}
ProjectionHandoffDeletionGuard.ResourceProperties = {
  ServiceToken, protocolVersion: 1, resourceKind: "decommission",
  environment, stackIdentity: {Ref: "AWS::StackId"}
}
```

The maps above are written in template JSON; on the wire CloudFormation delivers every custom-resource
property value as text, so the provider's closed wire form is `protocolVersion: "1"` and
`deliveryMappingEnabled`/`semanticWriterMappingEnabled: "true"|"false"`, and it accepts exactly those
renderings: any other spelling, and any native JSON number or boolean (which the wire never carries),
is a retyped field. The provider rejects extra, missing, retyped, blank, noncanonical, or independently
supplied fields and requires `stackIdentity` to equal top-level `StackId`. Its exact top-level
allowlist is the official CloudFormation request envelope plus the top-level `ServiceToken` a
Lambda-backed callback carries: `RequestType`, `ServiceToken` (which must equal
`ResourceProperties.ServiceToken`), `ResponseURL`, `StackId`, `RequestId`, `ResourceType`,
`LogicalResourceId`, `ResourceProperties`, plus `PhysicalResourceId` for Update/Delete and
`OldResourceProperties` for Update. Create rejects the latter two; Update requires and validates both;
Delete requires `PhysicalResourceId` and rejects `OldResourceProperties`. `RequestType` is exactly
`Create|Update|Delete`; stack/request/logical/physical identifiers are nonblank and callback-specific;
`ResourceProperties` and any `OldResourceProperties` must each match the resource's closed schema.

For a prepare Create/Update callback, prepare strongly reads `STATE`, validates the complete closed callback
and property maps, resolves the exact three controls for `requestedState`, and computes the following
fresh candidate identities:

```text
stack_update_identity = sha256(canonical_json([
  "projection-handoff-stack-update/v1", StackId, LogicalResourceId, RequestId
]))
candidate_transition_token = sha256(canonical_json([
  "projection-handoff-transition/v1", environment, stackIdentity,
  fromDisposition, fromState, requestedState, expectedRevision,
  stack_update_identity
]))
```

Canonical JSON is UTF-8, array order preserved, strings unchanged, booleans lowercase, integers in
minimal decimal, and no insignificant whitespace. Prepare alone computes these candidate values. For
`transition|bootstrap|recreate`, `transitionToken=candidate_transition_token`, and the candidate stack-
update identity and transition token become the prepared mutation identities. For `replay`, the fresh
values may be computed only as non-authoritative diagnostics and must be discarded before authority
selection: `transitionToken` is exactly the strongly read same-stack `ACTIVE.last_transition_id`, and
neither fresh value is persisted, returned, adopted as physical identity, or granted mutation authority. Commit never recomputes, discovers, or adopts a token:
`Fn::GetAtt` is the exact propagation seam, and commit verifies the complete prepared or active snapshot
and all three resolver controls against its properties before any mutation. The effective token-specific
physical IDs are exactly `projection-handoff:v1:<environment>:prepare:<transitionToken>` and
`projection-handoff:v1:<environment>:commit:<transitionToken>`. Therefore replay returns the existing
physical IDs `projection-handoff:v1:<environment>:prepare:<ACTIVE.last_transition_id>` and
`projection-handoff:v1:<environment>:commit:<ACTIVE.last_transition_id>` byte-for-byte. The deletion-
guard physical ID is `projection-handoff:v1:<environment>:decommission:<sha256(stackIdentity)>`.

> **Amendment, 2026-10-19 — first-deployment states and failed-create rollback.** The first
> deployment of a new environment (fresh-environment run 1, a new `ENV` from a clean clone,
> 2026-10-19, operator-local record) failed at `ProjectionHandoffPrepare` with `ADLY-HANDOFF-0001`.
> The shipped `projectionHandoffState` is `QUEUED_ACTIVE`, the state dev reached through the four
> forward edges, and `ABSENT` permitted only `INLINE_ACTIVE`. The refused Create's rollback Delete
> then failed too, because the failed-create physical ID was outside every closed form, so the stack
> ended `ROLLBACK_FAILED`. Two rules changed; every listed edge and every other guard is unchanged.
> (1) `ABSENT` bootstrap and `DECOMMISSIONED` recreation accept `QUEUED_ACTIVE` as well as
> `INLINE_ACTIVE`. The edges exist to hand an automatic inline writer's work to the queue without two
> writers overlapping. Neither source has a writer to hand over. `ABSENT` means no Ingestion stack
> ever committed in the environment, so there is no prior projection write and no inline backlog.
> `DECOMMISSIONED` is written only by prepare Delete, which runs after every application resource of
> the old stack is deleted (all of them depend on prepare). The new stack creates its queues, both
> mappings, and the table writer between prepare and commit, so `QUEUED_ACTIVE` is coherent from its
> first commit. No per-environment value exists: every environment ships the same state.
> (2) The failed-create marker and its Delete, below.

Prepare Create/Update strongly reads `STATE` and selects exactly one mode. `ABSENT` permits only
`bootstrap` to one of the two first-deployment states, `INLINE_ACTIVE` or `QUEUED_ACTIVE`, with
`fromDisposition=ABSENT`, `fromState=ABSENT`, and
`expectedRevision=0`. `ACTIVE` with the same requested state is `replay` only when the row is the
complete exact `ACTIVE` form, its `stack_identity` equals callback `StackId`, its nonblank canonical
`last_transition_id` is present, and the complete incoming controls equal the resolver row for that
state. `ACTIVE` with a listed edge is `transition` and records its complete snapshot.
`DECOMMISSIONED` permits only `recreate` to a first-deployment state (`INLINE_ACTIVE` or
`QUEUED_ACTIVE`), only when callback `StackId` differs from
the deleted stack identity, and preserves the decommissioned revision as `expectedRevision`. Any other
state/disposition, same-stack recreation, control mismatch, or unlisted edge fails. Except for replay,
prepare CASes the exact source row to `PREPARED`; it then ensures the `PREPARED` audit. Replay performs
no `STATE` or `AUDIT` mutation. Its Create/Update `Data` is byte-exactly
`{protocolVersion:1, mode:"replay", transitionToken:ACTIVE.last_transition_id,
fromDisposition:"ACTIVE", fromState:ACTIVE.state, expectedRevision:<ACTIVE.revision as canonical decimal
text>}`. The non-replay `Data` shape remains exactly `{protocolVersion:1, mode, transitionToken,
fromDisposition, fromState, expectedRevision}`; `fromState` is one six-state value, `ABSENT`, or
`DECOMMISSIONED`, and `expectedRevision` is canonical decimal text.

Commit Create/Update accepts only those six values through its prepare `Fn::GetAtt` properties. For
`transition|bootstrap|recreate`, it conditions the byte-exact `PREPARED` row, requested state,
resolver controls, source snapshot, expected revision, token, and stack identity; then writes `ACTIVE`
with the requested state and same stack identity. Transition/recreation revision is
`expectedRevision+1`; first bootstrap revision is `0`; after mutation it ensures the `COMMITTED` audit.
For `replay`, commit validates the complete closed callback and properties, independently strongly reads
the complete exact same-stack `ACTIVE` row, requires requested state and all three incoming controls to
match its resolver row, and requires its `Fn::GetAtt` values byte-for-byte to equal
`mode="replay"`, `transitionToken=ACTIVE.last_transition_id`, `fromDisposition="ACTIVE"`,
`fromState=ACTIVE.state`, and `expectedRevision=ACTIVE.revision` as canonical decimal text. It performs
no `STATE` or `AUDIT` mutation and returns physical ID
`projection-handoff:v1:<environment>:commit:<ACTIVE.last_transition_id>`. Commit Create/Update `Data`
is exactly `{protocolVersion:1, mode, transitionToken, disposition:"ACTIVE", state, revision,
outcome:"committed"|"replayed"}` with canonical decimal revision; replay therefore returns the retained
`ACTIVE.last_transition_id`, state, and revision with `outcome="replayed"`. A delayed callback whose
fresh candidate identity differs cannot mutate, persist, or return that candidate in replay mode.

[The official CloudFormation custom-resource request/response contract](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/crpg-ref-requesttypes-update.html)
[verified] (2026-09-19) states that Create/Update response `Data` is addressable through `Fn::GetAtt`;
changing `PhysicalResourceId` intentionally creates a replacement and causes an old-resource Delete
callback; and Delete responses do not support `Data`. Accordingly every successful Delete returns the
incoming `PhysicalResourceId` unchanged and no `Data`; its outcome exists only in immutable audit and
structured provider telemetry. Old replacement Deletes are expected and are stale-token no-ops after
the replacement commits.

A failed Create still answers with a physical ID, and CloudFormation's rollback sends a Delete for
it. Once prepare has decided its token, or commit's `ACTIVE` write has returned, a failed Create names
the token-specific closed-form ID, so the ordinary token-bound Delete below owns the rollback. A Create
that fails before that point names the failed-create marker
`projection-handoff:v1-failed:<RequestId>` (`unknown` when the RequestId is not letters, digits, and
hyphens). The marker cannot collide with an environment's IDs, including an environment named
`failed`. Its Delete is accepted for exactly that form, never on Update, and keeps the delete order:

- Guard marker Delete is a zero-`STATE`/zero-`AUDIT` no-op; guard Create only validates.
- Commit marker Delete takes the token from its `Fn::GetAtt` properties. If the same token is still
  `PREPARED`, the `ACTIVE` write never landed and the Delete is a no-op; prepare's token-bound Delete
  restores the prior snapshot last. Otherwise it applies the token-bound commit Delete below.
- Prepare marker Delete restores a same-stack `PREPARED` or `ROLLING_BACK` row exactly as prepare
  Delete does (an orphan an earlier provider wrote without naming its token), and is a no-op for
  any other row.

Every other physical ID outside the closed forms still fails.

Delete behavior is closed and token-bound:

- Commit Delete first parses its token-specific physical ID. If the exact same-stack `ACTIVE` row still
  has that token as `last_transition_id`, rollback owns it: CAS to `ROLLING_BACK`, then ensure the
  `ROLLING_BACK` audit. If the same-stack row is `DECOMMISSIONING` and its complete prior `ACTIVE`
  snapshot has that incoming token as `last_transition_id`, commit Delete is an exact no-op so stack
  deletion can continue. After a newer transition commits, an old replacement Delete whose token no
  longer equals same-stack `ACTIVE.last_transition_id` is a stale zero-`STATE`/zero-`AUDIT` no-op. A
  completed rollback token or callback from a deleted stack after different-stack recreation is also a
  stale no-op. A current-token malformed snapshot, resolver mismatch, or impossible same-stack
  disposition fails and alarms.
- Prepare Delete first parses its token-specific physical ID. After a newer transition commits, an old
  replacement Delete whose token does not equal same-stack `ACTIVE.last_transition_id` is a stale zero-
  `STATE`/zero-`AUDIT` no-op. For its current `PREPARED` or `ROLLING_BACK` token, prepare Delete first
  establishes `ROLLING_BACK` and its audit, then restores the byte-exact prior snapshot and ensures
  `ROLLED_BACK`. Transition rollback restores prior `ACTIVE` without revision change. First-bootstrap
  rollback alone conditionally deletes the exact token-bound item to restore `ABSENT`. Failed recreation
  restores the complete prior `DECOMMISSIONED` row, including its revision and history. A transient
  state/audit failure remains retryable; no other token can be changed.
- Prepare Delete that sees same-stack `DECOMMISSIONING` after application deletion may proceed only
  when the complete prior `ACTIVE` snapshot has the incoming prepare token as `last_transition_id`; it
  conditions that snapshot and the decommission token, writes `DECOMMISSIONED` at prior revision plus
  one, removes the active `state` claim, and ensures `DECOMMISSIONED` audit. A stale old replacement or
  deleted-stack token is a zero-`STATE`/zero-`AUDIT` no-op; a current inconsistency fails.

DeletionGuard Create only validates the exact properties and arms the guard; it does not mutate state.
Update requires byte-identical old/new properties and is an exact no-op. Delete derives
`decommission_token=sha256(canonical_json(["projection-handoff-decommission/v1", environment,
stackIdentity, state, revision, last_transition_id]))`, CASes only the complete same-stack `ACTIVE` row
to `DECOMMISSIONING`, and ensures that audit before succeeding. Exact retry fills a missing identical
audit. A same-token completed decommission is replay; a callback from an older deleted StackId after
recreation is a stale no-op. Same-stack `PREPARED|ROLLING_BACK`, a malformed projection-handoff
`STATE` row, or any current
inconsistency fails rather than claiming deletion is complete. Interrupted deletion therefore remains
truthfully `DECOMMISSIONING` until CloudFormation retries; no callback resets revision or infers state
from surviving resources.

Every destroyable application resource depends on `ProjectionHandoffPrepare`.
`ProjectionHandoffCommit` depends on every controlled resource, and
`ProjectionHandoffDeletionGuard` depends on commit and every destroyable non-provider application
resource. Provider Lambda/service-token resources are excluded from that explicit application set but
remain after all three callbacks through the service-token dependency. [The official `DependsOn`
contract](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/aws-attribute-dependson.html)
[verified] (2026-09-19) states that a dependency creates B before A and deletes A before B. The resulting
delete order begins with guard; commit Delete then no-ops in the matching decommission state while application
resources are deleted; prepare is last and records `DECOMMISSIONED`. Guard therefore records
`DECOMMISSIONING` before destruction and prepare records the truthful terminal disposition after it. There is no retained
resource added to `IngestionStack`: the DataV2 publications table remains `RETAIN`, Ingestion
application resources remain `DESTROY`, and `DEPLOY_ORDER` is unchanged.

The provider role alone receives the exact retained table ARN with
`ForAllValues:StringEquals:{"dynamodb:LeadingKeys":["PROJECTION_HANDOFF#<environment>"]}` and only
`dynamodb:GetItem` plus `dynamodb:UpdateItem`, with `dynamodb:DeleteItem` added solely for the private
token-bound first-bootstrap rollback. Audit creation uses conditional `UpdateItem`, never `PutItem`. The role has no `PutItem`,
`Query`, `Scan`, batch or transaction action, table-management action, event-source-mapping API, other
service, wildcard action/resource, or broader leading key. The delete builder requires the complete
`fromDisposition=ABSENT` bootstrap snapshot; no decommission, ordinary transition, commit, deployment,
runtime, or operator path can emit `DeleteItem`. This three-resource protocol remains an internal
`IngestionStack` dependency, not a new stack, `DEPLOY_ORDER` edge, deployment stage, runtime control
plane, or independently invocable layer.

The only normal forward edges are `INLINE_ACTIVE→INLINE_QUIESCED→QUEUED_QUIESCED→
QUEUED_ACCUMULATE→QUEUED_ACTIVE`. At `INLINE_QUIESCED`, read-only evidence requires delivery-function
concurrency zero and every automatic-writer attempt/fence terminal and clear. `QUEUED_QUIESCED` changes
composition only while both mappings remain disabled. `QUEUED_ACCUMULATE` may publish authority and
accumulate admitted semantic-write backlog but cannot project; evidence requires no old-version delivery
execution and zero Athena statements from the queued delivery composition. Only then may
`QUEUED_ACTIVE` enable the semantic writer. The two manual operators remain fenced in every state.

The only normal rollback edges are `QUEUED_ACTIVE→QUEUED_DRAIN→QUEUED_QUIESCED→
INLINE_QUIESCED→INLINE_ACTIVE`. `QUEUED_DRAIN` disables new delivery but deliberately leaves queued
composition and the semantic writer enabled. It may advance to `QUEUED_QUIESCED` only after the
semantic-write queue and DLQ, delivery/table-writer executions, affected plans, attempts, and both
pending and blocked provider fences first produce one strongly consistent all-clear observation and
then remain quiet for the uninterrupted
`drainObservationIntervalSeconds=300` owned by §11. The `verify-drain` mode of
`adapters.jobs.verify_stage1_preswitch` is the sole owner of that observation: its timer starts only at
that all-clear read, resets
to zero on any nonquiet evidence or incomplete/failed read, denies a passing report before 300 elapsed
seconds, and permits it at exactly 300 seconds only after a final strongly consistent all-clear read;
the deploying operator may submit the transition only with that passing report retained as acceptance
evidence.
Only then may composition return to inline. Rollback never purges or supersedes queued work and never
terminalizes an owner merely to permit rollback; the drain completes admitted authority through its
normal owner path. No state enables overlapping automatic inline and queued projection writers.

An emergency park may move only `QUEUED_ACTIVE→QUEUED_QUIESCED` or
`QUEUED_DRAIN→QUEUED_QUIESCED`. This is a fail-closed availability action, not completed rollback. While
semantic queue/DLQ backlog, nonterminal executions/plans/attempts, or pending/blocked fences exist, the
only legal restoration edge is `QUEUED_QUIESCED→QUEUED_DRAIN`; inline composition and delivery remain
forbidden. After restoration, normal drain and its observation interval decide whether rollback may
continue. There is no purge, supersession, synthetic terminalization, or direct emergency restoration
to active delivery. The one-time migration replay is reported only under the sanitized manual
comparison-evidence label `migration_replay`, derived from the migration job's returned
`stamped`/`already`/`failed`/`unstamped` counters plus a bounded status-stream observation window. It is
not a `UsageByCategory.v1` category or runtime event field, and every before/after window excludes the
migration instant.

Handoff acceptance pins golden canonical JSON bytes for every property map, old-property map, candidate
stack-update/transition and decommission token input, `Data` object, audit payload, and physical ID.
Unit/provider tests cover exact Create/Update/Delete envelopes for prepare, commit, and guard; every six-
state resolver row and legal edge; same-state `replay`; transition/bootstrap/recreate; missing/extra/
retyped fields; prepared-snapshot or control mismatch; changed request/logical/stack identity; concurrent
CAS; changed physical-ID replacement; old-resource Delete; delayed stale callbacks before and after
different-stack recreation; and current inconsistency failure. They prove commit consumes only prepare
`Fn::GetAtt` data and never recomputes or adopts a token, every successful Delete echoes the incoming
physical ID with no `Data`, and stale old replacement Deletes after a transition leave both `STATE` and
the complete audit keyspace byte-identical.

For same-state replay, golden tests begin from byte-pinned `ACTIVE` and complete audit-keyspace
snapshots after both an ordinary transition and different-stack recreation. In each case prepare validates
the complete closed envelope/properties and resolver controls, and returns byte-exact
`{protocolVersion:1, mode:"replay", transitionToken:ACTIVE.last_transition_id,
fromDisposition:"ACTIVE", fromState:ACTIVE.state, expectedRevision:<ACTIVE.revision as canonical decimal
text>}` with physical ID `projection-handoff:v1:<environment>:prepare:<ACTIVE.last_transition_id>`.
Commit receives those exact values through `Fn::GetAtt`, validates the exact active state, revision,
stack, token, and controls, and returns byte-exact `{protocolVersion:1, mode:"replay",
transitionToken:ACTIVE.last_transition_id, disposition:"ACTIVE", state:ACTIVE.state,
revision:<ACTIVE.revision>, outcome:"replayed"}` with physical ID
`projection-handoff:v1:<environment>:commit:<ACTIVE.last_transition_id>`. Tests assert zero
`UpdateItem`/`DeleteItem` calls, byte-identical `STATE` and audit keyspace before and after both
callbacks, stable state/revision/`last_transition_id`, and no audit create/check/repair. Reordered,
retried, or delayed same-state callbacks may compute different fresh candidates for diagnostics but
cannot persist, return, or mutate under them.

Deployment failure injection covers before prepare state write, state-write-before-audit, after prepare,
before/after commit, controlled-resource rollback, commit Delete, prepare Delete, bootstrap DeleteItem,
and recreation rollback. It proves mutation retry fills only byte-identical missing audits; ordinary
rollback restores exact `ACTIVE`; bootstrap rollback restores `ABSENT`; failed recreation restores exact
`DECOMMISSIONED`; revisions never reset; and an old or delayed candidate token cannot mutate a newer
transition. Full stack-delete acceptance runs from each replay baseline and pins both the replay-to-full-
delete path and replay-to-interrupted-`DECOMMISSIONING`-to-retry-to-`DECOMMISSIONED` path. It asserts
that current prepare and commit Delete tokens both equal the replayed `ACTIVE.last_transition_id`, every
Delete response echoes its incoming physical ID with no `Data`, state/revision remain stable until the
guard transition, and the guard, commit, controlled-resource, and prepare boundaries preserve exact
ordered behavior. Failure is injected before guard CAS, after `DECOMMISSIONING`, during commit/
application/prepare deletion, and after `DECOMMISSIONED`; each retry converges in dependency order, an
interruption remains byte-exactly `DECOMMISSIONING`, missing mutation audits alone are repaired byte-
identically, and the retained terminal row is byte-exactly `DECOMMISSIONED` with no active-state claim,
prior revision plus one, and every immutable audit retained. Recreation succeeds only under a different
StackId to `INLINE_ACTIVE` or `QUEUED_ACTIVE`; replay then retains that recreated transition token, and delayed callbacks
or candidate identities from the deleted or current stack are stale read-only no-ops and cannot affect
it.

Synth and credentialed IAM tests prove exactly one provider and three custom resources; the closed
property/GetAtt/dependency graph; every destroyable application resource behind prepare and ahead of
guard; provider-resource exclusion with service-token retention; exact table/leading-key
`GetItem|UpdateItem`; and bootstrap-only `DeleteItem`. They deny Put/Query/Scan/batch/transactions,
mapping APIs, all other services, wildcards, and mutations by deployment/runtime/operator identities.
After every successful edge, acceptance strongly reads authority, audit, resolver composition, exact
mapping source/function ARN and enabled state, Lambda concurrencies, queues/DLQ, executions, plans,
attempts, fences, and status flows under the §11 drain-observation contract. Stack success or synth alone is never
behavior evidence: deployed tests must exercise callback replacement, replay after ordinary transition
and recreation, rollback, replay-to-complete-delete, replay-to-interrupted-delete/retry, recreation, and
failure paths and read back every claimed state.

### 2.6 Identity-mapping claim check (amendment, 2026-10-09)

**Why.** The `source.<contract>.identity.v1` rule result listed every selected identity mapping
inline, and publication evidence is copied into both DynamoDB publication records, which the
`commit_group` guard bounds at `MAX_PUBLICATION_ITEM_BYTES` (32,000). The list grows with campaigns
and products per bucket. In dev, one `amc-attributed-conversion` bucket held 79 mappings (57
campaigns) and produced a 33,762-byte record. Stage 1 refused every such delivery
deterministically, and three receives later it was dead-lettered.

**Rule.** The rule result carries only a claim: `count`, the `sha256` of one canonical document
holding the full list, and the document schema `adlytics.identity-mappings.v1`
(`domain/semantics/identity_evidence.py`). The claim is the only form any record carries. The
superseded inline form is refused by `parse_identity_claim`, never decoded.

- **Store.** The document is written to the Semantic V2 bucket at
  `immutable/identity-evidence/<sha256>.json`. The key is the document's content address, so equal
  lists share one object.
- **Write order.** The sidecar writes every document a candidate's evidence names
  (`_write_identity_evidence`) with `If-None-Match: *`, before that candidate's S3 object. The
  successful object and the fenced `commit_group` follow, as before.
  - An existing key is accepted only when its bytes are identical. Other bytes refuse the delivery
    before any candidate object is written.
  - The documents to write are derived from the candidate's own evidence, so a claim with no
    prepared document can't be published.
- **Readers fail closed.** `IdentityEvidencePort.read` fetches the document and recomputes the
  digest. There's no raw read and no fallback to a partial list. It raises:
  - `ADLY-IDENTITY_EVIDENCE-0002` when the document is absent (S3 `404`);
  - `ADLY-IDENTITY_EVIDENCE-0003` on a digest mismatch;
  - `-0001` on any malformed claim or document.

  The one runtime reader is completed-delivery replay (`_completed_group`). It attests every named
  document beside the candidate and successful objects.
  - The ingestion role holds no `s3:ListBucket` on this prefix, so a missing object surfaces in
    Lambda as `AccessDenied`. That's a retryable `SEMANTIC_STORAGE` dependency failure, which
    still fails closed.
- **IAM.** Only the ingestion function's role is granted the prefix, with exactly
  `s3:GetObject` and `s3:PutObject` (`ingestion-stack.ts` `grantSemanticV2`).
  - **Where the grant lives.** It's in its own customer-managed policy, `IdentityEvidencePolicy`,
    attached to that role alone, and the function depends on it, so new code never runs before
    its grant exists. Adding the statement inline failed the deploy of 2026-10-09, which rolled
    back: the role's inline policies then totalled 10,186 of IAM's 10,240-character aggregate
    role-policy limit. For the same reason the path's S3, Athena and Glue grants live in
    `SemanticV2StoragePolicy`, and `infra/test/iam-policy-size.test.ts` holds every role's inline
    aggregate under the limit with headroom and refuses a CDK overflow policy.
  - **CDK pins** (`ingestion-stack.test.ts`):
    - the exact action set on the prefix;
    - across every stack the app builds, no other IAM statement reaches it, through the prefix or
      any S3 wildcard above it.
  - **The operator jobs** `ingest_delivery` and `reprocess_semantic_v2` write the documents under
    the operator's own `--profile` session.
    - `SemanticProjectionOperatorRole` is deliberately outside the grant.
    - A delivery run under that role refuses at the claim-check `PutObject`, which fails closed.
- **Distinct refusals at the write.**
  - `ADLY-IDENTITY_EVIDENCE-0004`: the content-addressed key already holds other bytes.
  - `ADLY-IDENTITY_EVIDENCE-0005`: a candidate's evidence names a claim whose document the
    delivery didn't prepare.

  Both refuse before any candidate object is written.
- **Reprocess acceptance** (`attest_reprocess`) reads every named document through the same
  verifying port, as replay does.
- **Retention and teardown.** The objects follow the Semantic V2 bucket: no lifecycle rule, and
  `RemovalPolicy.RETAIN`, like every `immutable/` prefix. They are never deleted or overwritten. A
  document orphaned by a refused delivery is harmless, because it is content-addressed and
  idempotent. A destroy-all keeps the bucket, so a fresh deploy that reuses it reuses the objects.
- **Records published before this amendment** keep their inline detail, which is immutable history.
  No reader decodes that form.
  - A completed-delivery replay of such a record under the same execution would fail the existing
    evidence-equality check (`ADLY-SEMANTIC_SIDECAR-0001`).
  - A redelivery gets a new receive count, so a new execution, fence and publication, and is
    unaffected. The same holds for every new landing.
- **Size.** The claim is fixed-size, so the record no longer depends on mapping count. With the
  committed fixtures, a 57-campaign conversion bucket and a 5,000-mapping synthetic list both
  measure under 16,000 bytes, and the superseded inline form of the same bucket measures over the
  bound (`test_semantic_sidecar.py`).

## 3. Write plan record and state machine

Plan authority uses only the existing `publications` DynamoDB table. Each `plan_digest` has exactly
one owner at `partition_key=WRITE_PLAN#<plan_digest>, record_key=SUCCESSOR_OWNER`. Its complete mutable
field set is a monotonic `revision`; `root_plan_key` (absent only before the first plan);
`last_plan_key` and `last_generation` (with no `last_plan_key` and `last_generation=-1` before the
root); optional paired `active_plan_key` and `active_intent_id`; optional reservation trio
`reserved_intent_id`, byte-identical `reserved_intent_digest`, and `reserved_generation`; and
`latest_internal_execution_id`, when a plan exists, byte-equal to the execution identity in that plan's
`HEAD`. `reserved_intent_digest` is exactly the selected `intent_id`, not a second digest constructor.
The active pair is both present or both absent, and the reservation trio is all present or all absent.
The owner has no other claim field or active-generation field.

Each submitted intent is exactly one partition at
`partition_key=WRITE_PLAN#<plan_digest>#INTENT#<intent_id>` containing one `record_key=META` and at most
15 `record_key=UNIT#<unit_key>` rows. Nothing else is stored in that partition. There is exactly one
canonical intent authority. A canonical unit entry is:

```text
[unit_key, table_kind, merge_count,
 [[kind, reference, content_checksum, publication_id, partition_key-or-null, fencing_token]
  in canonical object order]]
```

Unit entries are ordered by ascending unsigned UTF-8 bytes of `unit_key`; the shorter byte string sorts
first after an equal prefix. Each unit's object entries use the canonical object order defined in §6.
`merge_count` is the exact nonnegative integer for that physical unit. `content_checksum` is exactly
the 64 lowercase hex digits of the immutable object's SHA-256; the `sha256:` prefix a publication
renders is stripped at roster construction and every roster reader compares digest bytes only.
`partition_key-or-null` is the Publication-State-owned `PartitionIdentity.identity` bounded by §11
`maxPartitionIdentityBytes`, not by the internal-identity cap. The sole roster and intent
constructors are:

```text
intent_roster_tuple = ["stage1-plan-intent-roster/v1", <ordered canonical unit entries>]
roster_digest = canonical_authority_digest(intent_roster_tuple)
intent_authority_tuple = ["stage1-plan-intent/v1", root_plan_key-or-null,
  source_plan_key-or-null, plan_digest, origin_kind,
  origin_source_identity_or_failure_transition, intent_roster_tuple]
intent_id = canonical_authority_digest(intent_authority_tuple)
```

`origin_kind` is exactly `source_delivery|plan_attempt_budget`; its following member is
exactly that origin's source identity or failure transition. Origin bytes are immutable intent
authority: an admission-failure transfer returns the exact same intent to `pending` and never rewrites
`origin_kind`, its member, or any other hashed tuple input — the transfer exists only in the intent's
mutable state/scheduling fields and the plan's `admission_failed` lifecycle event. No separate object
digest, alternate roster constructor, omitted roster member, caller order, or normalization exists.

`plan_digest` has exactly one constructor. For `origin_kind=source_delivery`, the delivery's stable
source identity and the plan digest are:

```text
source_delivery_identity_tuple = ["stage1-source-delivery/v1",
  [[manifest_object_key, manifest_version_id] ...]]
origin_source_identity = canonical_authority_digest(source_delivery_identity_tuple)
plan_digest = canonical_authority_digest(["stage1-plan-digest/v1", origin_source_identity])
```

The inner pair list contains every manifest object of the delivery, ordered by ascending unsigned UTF-8
bytes of `manifest_object_key` and then `manifest_version_id`; both members are the exact versioned S3
identities already required by ingestion, and both digests render as 64 lowercase hex characters. A
redelivery of the same source message names the same manifest objects and versions, so it reproduces the
same `origin_source_identity` and `plan_digest` while its roster members (publication identities,
fencing tokens) may differ; that is exactly the "stable across delivery retries" property the status
contract's `plan_tuple` consumes. `origin_source_identity` is also the intent tuple's
`source_delivery` origin member. An admission-failure transfer retains its same intent and therefore the
same `plan_digest`; a `plan_attempt_budget` successor intent copies the source plan's `plan_digest`
byte-for-byte inside the attempt-33 `FailUnitRequest` transaction. The `plan_attempt_budget` origin
member — the "exact source-plan failure transition" — likewise has exactly one constructor:

```text
plan_attempt_budget_origin_tuple = ["stage1-plan-attempt-budget/v1", source_plan_key]
origin member = canonical_authority_digest(plan_attempt_budget_origin_tuple)
```

`source_plan_key` is the exact plan key of the plan the same transaction terminalizes, so one
terminal predecessor deterministically produces one successor origin member and exact replay reproduces
the identical `intent_id`. The committed vector (fictional synthetic members, encoding/digest behavior
only) is:

```text
budget tuple JSON = ["stage1-plan-attempt-budget/v1","WRITE_PLAN#2222#exec-g7"]
budget tuple UTF-8 length = 59
origin member = 4a8f41b7fc1bd98dfb2e082b3ef5f8fee64af9b166c9c46589e090b736f1e378
```

No other derivation, prefix form
(such as `sha256:`), truncation, or operation-scoped digest exists. Intent `META` contains
the exact root/source plan keys, plan digest, `intent_id`, `roster_digest`, origin kind and exact source
identity or failure transition, state in `pending|claimed|admitted`, optional
`claimed_generation`, and canonical retry/due/ready/audit fields. The canonical scheduling and audit
fields are exactly `retry_count` (nonnegative integer), `due_at` (20-digit UTC epoch-microsecond text),
the derived pending-only pair `ready_status`/`ready_order`, `created_at`, and `last_transition_at`; no
other scheduling, cursor, or audit attribute exists on the intent. Creation for `source_delivery` and
the attempt-33 `plan_attempt_budget` successor seeds `retry_count=0` and `due_at` equal to the creating
transaction's one captured UTC epoch-microsecond instant, so a fresh intent is immediately due.
`ReschedulePlanIntentRequest` and `FailAdmissionRequest` compute their delay from the pre-increment
count as exactly `min(60 * 2^min(retry_count, 4), 900)` seconds, set
`due_at = <captured instant> + delay`, and then increment `retry_count` by exactly one in the same
transition; the restored `ready_order` embeds that new due instant. There is no blocked intent state:
every reconciliation condition is either retryable — rescheduled indefinitely under the capped-backoff
formula — or an invariant/corruption failure that fails closed with no mutation, surfaces through the
reconciler's structured error and alarm, and leaves the exact claimed or pending authority for
operator investigation. Each immutable intent `UNIT#` row
contains exactly one canonical unit entry. `META` plus every `UNIT#` row must reconstruct the same
`intent_roster_tuple` and `intent_authority_tuple`; exact replay requires all bytes. The same objects
with different grouping, unit key, table kind, merge count, kind, reference, checksum, publication ID,
partition key, or fencing token therefore produce a distinct `roster_digest` and `intent_id`, as do any
root/source/plan/origin changes.

The committed constructor vector uses fictional synthetic member values — like the status contract's
fictional fixtures — to pin encoding, ordering, and digest behavior only; member-form validation (the
64-lowercase-hex `origin_source_identity`, plan-key segment forms, and identity caps) applies to
production inputs, never to this vector's synthetic members:

```text
intent_roster_tuple JSON = ["stage1-plan-intent-roster/v1",[["db.campaigns","current",2,[["manifest","s3://authority/a","0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef","pub-001",null,17]]]]]
intent_roster_tuple UTF-8 length = 182
roster_digest = e7294e98228c1dd05947ca0655c8b6015a98244d67e0c544873525e802448d66
intent_authority_tuple JSON = ["stage1-plan-intent/v1",null,null,"2222222222222222222222222222222222222222222222222222222222222222","source_delivery","delivery-001",["stage1-plan-intent-roster/v1",[["db.campaigns","current",2,[["manifest","s3://authority/a","0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef","pub-001",null,17]]]]]]
intent_authority_tuple UTF-8 length = 318
intent_id = 8595e699935a8ec7ee6d3d87d3e0214300e09831da6a4808f10c9fab4174269b
```

Creation, readback, reservation, replay, and reconciliation use this shared vector. Negative fixtures
change each tuple member independently; additionally they regroup the same object into another unit,
change only `merge_count`, reorder units/objects, and substitute null/string/integer representations.
Every semantic change yields a distinct intent; noncanonical ordering or typed drift is rejected rather
than hashed.

`ClaimPlanIntentRequest` atomically assigns
`claimed_generation=SUCCESSOR_OWNER.last_generation+1`. The internal execution identity is exactly
`canonical_authority_digest(["stage1-plan-materialization/v1", intent_id, claimed_generation])`, and the
normal plan key remains exactly `WRITE_PLAN#<plan_digest>#<internal_execution_id>`. Every persisted plan
`META` carries `root_plan_key`, `generation=claimed_generation`, source/predecessor plan key,
`origin_intent_id`, and `roster_digest`; it and its unit rows reconstruct the same intent authority. Its
`HEAD` carries that exact internal execution identity, and the owner's
`latest_internal_execution_id` must match it byte-for-byte.

The plan `META` complete settled-attempt aggregate is `settled_attempt_count` plus, once any accepted
attempt has settled, `settled_attempt_digest` over the complete settled set:

```text
settled_attempt_digest = canonical_authority_digest(["stage1-plan-settled-attempts/v1",
  [[physical_table_id, attempt_id, usage_authority_digest] ...]])
```

Members are unique, cover between 1 and 32 settled accepted attempts, and are ordered by ascending
unsigned UTF-8 bytes of `physical_table_id` and then `attempt_id`; `settled_attempt_digest` is absent
while no attempt has settled. No other aggregate derivation exists.

| Item | Closed fields | Exact transitions |
|---|---|---|
| `SUCCESSOR_OWNER` | Only the owner fields defined above; `reserved_intent_digest` is byte-equal to `reserved_intent_id=intent_id` | `SubmitPlanIntentRequest` condition-checks the exact existing revision or optionally bootstraps the absent owner without assigning a generation. `ClaimPlanIntentRequest` CASes `revision`, computes `last_generation+1`, and installs `reserved_intent_id`, the identical `reserved_intent_digest`, and `reserved_generation`. `ReschedulePlanIntentRequest` CASes and clears that trio. `PersistPlanRequest` requires the reservation generation to equal the intent's `claimed_generation` and current `last_generation+1`, performs the sole active-plan swap, advances `last_plan_key`, `last_generation`, `latest_internal_execution_id`, and `revision`, and clears the reservation. `FailAdmissionRequest` and terminal plan disposition clear the paired active fields only while preserving exact plan-`HEAD` agreement. |
| intent `META` | Exact intent-authority non-roster inputs, `intent_id`, `roster_digest`, origin/source identity or failure transition, state, optional state-bound `claimed_generation`, retry/due/ready/audit; only pending has `ready_status="ready"` and `ready_order=<20-digit due epoch-micros>#<intent_id>` | create `pending` with both ready fields and no generation; claim `pending→claimed`, assign `claimed_generation`, and remove both ready fields; successful full admission `claimed→admitted` retains that generation with neither ready field; pre-persistence retry and definitive admission failure change `claimed→pending`, remove `claimed_generation`, and restore both ready fields from the exact due; there is no blocked state, permanent-refusal transition, or requeue path — non-retryable conditions fail closed without mutation. Exact replay retains the exact current assignment and changes nothing. |
| intent `UNIT#<unit_key>` | Exactly one canonical unit entry, including its ordered complete object tuple list | Created with `META`, immutable thereafter, maximum 15. `META` plus all units reconstruct the canonical authority tuple; exact replay requires every byte. |

`SubmitPlanIntentRequest` creates generation-independent `META` plus the complete unit roster and
consumes the owner in the same transaction through an existing-owner condition check or optional
absent-owner bootstrap. Exact replay of the complete `intent_authority_tuple` reuses the same intent;
any changed canonical member creates a distinct queued intent even against the same owner snapshot. No
creation path omits the owner action, no creation transaction has a partial-replay or merge branch, and
submission neither derives nor stores a generation.

`ClaimPlanIntentRequest` strongly reads the due pending intent, exact owner revision, optional active
plan and intent, and plan `HEAD`, then atomically computes
`claimed_generation=owner.last_generation+1`, CASes the owner using only `reserved_intent_id`, the
byte-identical `reserved_intent_digest` (both equal `intent_id`), and
`reserved_generation=claimed_generation`, changes that intent
`pending→claimed`, stores the same `claimed_generation`, and removes `ready_status` and `ready_order`.
Exact replay returns the same assignment. `ReschedulePlanIntentRequest` consumes the exact reservation
and never invents or changes generation while the intent remains claimed. A retry changes the same
intent `claimed→pending`, removes `claimed_generation`, and restores the canonical retry schedule plus
`ready_status="ready"` and `ready_order=<20-digit due epoch-micros>#<intent_id>`; there is no blocked
intent state or permanent-refusal transition — a non-retryable condition is an invariant failure that
fails closed with no mutation and surfaces through the reconciler's structured error. The next claim
assigns the then-current `owner.last_generation+1`.

`PersistPlanRequest` consumes the exact reservation and claimed intent and requires
`owner.reserved_generation = intent.claimed_generation = owner.last_generation+1`. It derives execution
and plan identity from that assigned generation, creates the plan `META` and execution units, writes the
lifecycle authority and `HEAD`, and atomically swaps the owner. Ordinary persistence has at most 20
actions. Replacement of an existing planned plan has exactly one owner update and at most 100 actions;
it terminalizes the predecessor and installs the candidate in one transaction. Persistence advances
`owner.last_generation` to `claimed_generation` and clears the reservation; exact replay retains that
assigned generation. There is never more than one plan `META` with `status=planned`, and it is exactly
the plan named by `SUCCESSOR_OWNER.active_plan_key`. No terminal predecessor can fan out: all later
intents remain pending until the single owner claim and swap serialize them into consecutive
generations.

`AdmitUnitRequest` records one successful queue admission exactly once. Its last admission changes the
origin intent `claimed→admitted`, retains `claimed_generation`, and leaves neither ready field. An
intent that reaches `admitted` never leaves that state, and the final admission commits atomically with
`admitted_count` reaching `unit_count`; any later transaction that conditions the plan `META`
full-admission counters has therefore immutably proven the origin intent's state and carries no
separate intent transaction member.
`FailAdmissionRequest` atomically terminalizes the candidate plan as `admission_failed`, clears the
owner's paired active fields, preserves owner/plan-`HEAD` agreement, and changes that same intent
`claimed→pending` with its immutable origin bytes unchanged, removes `claimed_generation`, and restores the exact
retry schedule, `ready_status="ready"`, and
`ready_order=<20-digit due epoch-micros>#<intent_id>`. It creates no successor intent. The next claim
assigns the then-current owner `last_generation+1`; it does not reuse the failed candidate's generation.
Source-delivery acknowledgment is governed only by the strongly consistent four-outcome classifier in §5.3 after every
send, admission, terminal write, ambiguous response, and process-death boundary; no SDK or transaction
return authorizes it. An admitted unit message acknowledges only after `AdmitUnitRequest` commit or
exact replay; already-sent unit messages for a failed admission acknowledge only after
`FailAdmissionRequest` commit or exact replay has made the candidate non-executable and the same intent
recoverable.

`FailUnitRequest` owns plan-attempt-budget exhaustion. Rejected ordinal 33 does not create another Begin
reservation. Its transaction replaces the ordinary failure trigger with one origin
`plan_attempt_budget` successor intent `META` plus its complete `UNIT#` roster, consumes the same owner,
and records the exact source-plan failure transition. The trigger fixture and the maximum-size
`META`/`UNIT#` put fixtures independently prove bytes and actions. The failed unit message acknowledges
only after that transaction commits or exact replay proves the failure transition and successor intent.
That retained intent remains recoverable by `PlanIntentReconciler` even after the source queue message
reaches its DLQ. A terminal predecessor transition can create only this deterministic successor; replay
cannot create another.

Only `state=pending` intent `META` rows carry `ready_status="ready"` and
`ready_order=<20-digit due epoch-micros>#<intent_id>`, making them members of the sparse
`stage1-plan-intent-ready` GSI; claimed and admitted rows carry neither field and are absent
from that GSI. `PlanIntentReconciler` is the sole scheduled claimant and continues independently of source
queue retention or DLQ state. There is no scan, cursor, TTL, sweeper, alternate recovery record,
manifest/object-body reread, or second intent for one exact source identity/failure transition.

### 3.1 Owner-side public lifecycle heads

`DynamoStage1LifecycleHeadStore` consumes an opaque typed lifecycle/event/`HEAD` proof from
[Operational Data-Flow Status § Stage-1 event projection](../operational-data-flow-status/low-level-design.md#stage-1-event-projection).
That contract alone owns every public identity, classification, key-construction, ordering, predecessor,
topology, and projection-mapping definition carried by the proof. The opaque
proof carries the exact `STATUS_FLOW#...`/`HEAD` key, deterministic `EVENT#` key and immutable payload,
complete prior and resulting head tuples, and replay evidence required by this write mechanism; no Load
Path caller, projector, enum, matrix, or constructor may supply or reproduce them. For every Stage-1
unit event, the proof also carries `owner_generation` copied from the exact plan-unit owner payload and
requires it to equal the persisted plan generation; plan lifecycle proofs carry the internal
`child_outcome_reduction_version=2` and sorted required-child expectation map for status reattestation,
without adding either field to `OperationalDataFlowEvent.v1`. Publication-pointer roots remain headless.

For an ordinary owner transition, the transactional writer conditions the proof's complete prior head
or declared root absence, writes the proof's one event authority, and performs exactly one conditional
head create/update in the same transaction as owner state. The local contract owns only atomicity,
owner-plus-`HEAD` byte agreement, replay, and race handling. It does not interpret or allocate any public
identity or predecessor field.

A claimed pending intent replacing the current planned plan is the sole compound lifecycle-head
transition. Before constructing it, the owner strongly reads the exact reservation trio and revision,
the target intent `META` and complete units, the source plan `META`, every source plan unit and
referenced attempt, the old `HEAD`, and both opaque typed event proofs. A nonterminal or unsettled source
attempt defers the whole persistence transaction only while the exact reservation remains and may use
`ReschedulePlanIntentRequest`. Any mismatch between strong-read evidence and the source plan `META`
immutable roster digest or complete settled-attempt count/digest is collision or corruption and has no
persistence, reschedule, or other effect. Usage category selection and precedence are not
defined here; every earlier settlement must already conform to
[Data-Flow Completion and Service Levels § Stage 1 usage attribution](../../concepts/data-flow-completion-and-service-levels.md#stage-1-usage-attribution).
The transaction conditions the source plan `META` aggregate proof, terminalizes it to exactly
`status=superseded` with the operation's terminal time, creates the target plan `META` and units,
writes the two contiguous lifecycle events supplied by the status contract,
advances the head once from `N` to `N+2`, performs exactly one owner update to install the target active
plan/intent pair, advances last-plan/generation/execution fields, and clears the reservation. Its fixed
members are exactly six — the one owner update, the source `META` terminalization, the target `META`
put, the two event puts, and the one head write; beside the target `UNIT#` puts, every other member is
a condition check: one exact-snapshot check per source plan unit, one per referenced source attempt
requiring its terminal state, `athena_usage_state=settled`, and byte-identical
`usage_authority_digest`, and one per settlement authority requiring the byte-identical `USAGE#`
payload and immutable payload digest. The old event remains at `N+1`; the new recorded event is
`N+2`. No transaction exposes an intermediate owner
or head at `N+1`, and no other transition may contain two lifecycle events.

Every head update conditions the complete opaque previously read head tuple and replaces it with the
candidate tuple supplied by the linked status contract. Before mutation, each path strongly reads every
status-supplied deterministic event key it would put. An ordinary exact replay succeeds only when its
immutable event, owner-state effect, and resulting head tuple are byte-identical. A compound exact replay
succeeds only when both owner-state effects and both immutable events are byte-identical and the head is
either the exact `N+2` result or has advanced from that result through one contiguous immutable event
chain. Either replay performs no head mutation, including after a later head advance, and preserves
delivery state.

Compatibility is fail closed. A Stage-1 authority lacking the persisted public identity/head proof,
using another canonical tuple or namespace version, or disagreeing with recomputation is not legacy
input to adopt or repair; it is an invariant failure. Because no pre-amendment writer of these
lifecycle authorities ever existed — every retained record was created by the amended owner exception,
including window records from the active inline delivery writer — the pre-switch gate must prove that
every existing record carries valid recomputable proof and that no proof-less, wrong-version, or
recomputation-divergent record exists, before enabling the queued writer or
producer. Any future tuple/namespace migration requires a separately versioned owner contract and an
atomic migration decision; projection never supplies compatibility.

The transactional writer can never produce only one compound owner effect or event. Readback of only
one event, one plan-state effect, or an `N+2` head without the matching owner swap is an invariant
partial-state failure. A conditional conflict strongly rereads `SUCCESSOR_OWNER`, the reserved intent,
the active plan and origin intent, both event keys, and the complete head, then returns byte-exact
replay, resumes a still-valid reservation, or returns the committed winner. It never branches a
sequence, splits the compound pair, changes a pending intent identity, or reassigns a claimed generation.
Exact replay of a valid claim or persistence retains its assigned generation and ordering.

Every feasibility envelope in §2.1 includes the status-supplied `STATUS_FLOW#...`/`HEAD`
create-or-update action, its condition bytes, resulting item size, and deterministic event key/payload
for every ordinary plan, unit, and inline-run transition. It includes both
`RecordStage1RefusalRequest` variants: the unit and `META` condition checks for a unit refusal, or the
run condition check for an inline refusal, plus the event/head actions and encoded bytes. It separately
includes every frozen exact ordinary `PersistPlanRequest` map under the 20-action application cap,
every planned-plan replacement map with exactly one owner update under the 100-action ceiling, and every
`CompletePlanRequest` map under the 51-action cap. Fixtures include exact owner reservation and
plan-`HEAD` agreement, intent and plan states, source plan roster/settled-attempt aggregate conditions,
event puts, and the final head update. The attempt-33 trigger and successor `META`/`UNIT#` puts have
independent byte/action fixtures. Zero-row
pointer flows remain single-transition roots created atomically with their pointer event; they need no
sequencing head. The status projector validates and reduces the owner-persisted opaque proof; it never
asks Load Path to allocate or reinterpret public ordering or identity.

A unit failure never changes origin intent `state`. Its closed failure disposition maps one-to-one onto
the four public typed unit reasons owned by the status contract's `semantic_unit_failed`
[mapping row](../operational-data-flow-status/low-level-design.md#stage-1-event-projection); no other
disposition or reason exists. There is one failure path after claim and table-lease
acquisition. It requires every referenced statement attempt for the table to be terminal, the attempt
`FENCE` to be clear, and the final owner disposition to be durable in the same transaction.
`FailUnitRequest` conditions plan `UNIT.status = writing`, the exact `writer_execution_id`, plan
`META.status = planned` with full admission — which immutably proves the origin intent's `admitted`
state without an intent transaction member — and the exact unexpired table-lease holder/token; expands
`SettleStage1AthenaUsageRequest` for every referenced accepted attempt under the concept-owned category
mapping; changes the unit to `failed`; increments `failed_count` once; updates the plan `META` complete
settled-attempt count/digest; creates its deterministic failure outbox and head advance; and commits all
new Athena `USAGE#` authorities atomically. In the ordinary branch the unit condition rides its own
unit `UpdateItem`; in the attempt-33 branch the unit item carries no transaction member, because the
exact unexpired lease condition — whose queued holder is the §5 hash of
`[plan_key, unit_key, writer_execution_id]` — plus lease-fenced exclusivity over unit transitions
proves the unit's `writing` state and writer binding transitively, under the same
no-transaction-member convention as the intent proof. At rejected ordinal 33 it instead terminalizes the plan and
creates the one deterministic origin `plan_attempt_budget` successor intent `META` and complete intent
`UNIT#` roster in that same transaction, without another Begin reservation. That branch's exactly-one
owner action is one owner `UpdateItem` that advances `revision` and clears the paired active fields,
mirroring `FailAdmissionRequest`. It changes no pointer, lease, fence, or sibling state. There is no `queued→failed` transition. A displaced or expired writer
can neither fail the current owner's unit nor alter counters. Queued/writing siblings remain executable
unless the plan was terminalized. `ClaimUnitRequest` redrive changes an ordinary failed unit back to
`writing` and decrements `failed_count` once. That lifecycle counter decrement is not a reservation
decrement; accepted-attempt reservation counters remain permanent. Consequently no unit is
acknowledged or lost because a sibling failed, and completion requires full admission,
`attested_count = unit_count`, `failed_count = 0`, and plan `status=planned`; full admission
immutably proves the origin intent's `admitted` state, so no completion member conditions the intent
directly.

`verified_digest` is exactly
`canonical_authority_digest(["stage1-unit-verified-digest/v1", <checksums>])` over the unit's verified
immutable checksums, unique and ordered by ascending unsigned UTF-8 bytes; at least one exists.
Units carry references and identities, never rows. Stage 1 retains the sole intent `META`, units, runs, attempts,
lifecycle `EVENT#` items, Athena `USAGE#` authorities/evidence, lifecycle `HEAD` items, and `FENCE`
records indefinitely. None receives an `expires_at` or other DynamoDB TTL attribute; Stage 1 does not enable
DynamoDB TTL and has no retention sweeper. Pending, delivered, and blocked lifecycle or usage authority
and every immutable payload remain retained. This is independent of the table lease's operational `expires_at`: lease expiry may permit a later lease holder
only when `FENCE` is clear, and it never authorizes deletion of retained authority.

Provider attempts are table-scoped rather than plan-scoped so queued and no-plan operator writers see
one fence. `DynamoTableAttemptStore` owns them in the publications table at canonical keys
`partition_key=TABLE_ATTEMPT#<physical table id>, record_key=ATTEMPT#<attempt id>` and matching
`record_key=FENCE`.

For a pending-GSI candidate and its strongly read optional snapshots,
`core_valid(table_id,fence,attempt)` is true exactly when both items exist and all of the following hold;
reconciliation fields are excluded from this predicate:

1. the supplied `table_id` is canonical and both items have the exact canonical `TABLE_ATTEMPT#<table
   id>` partition key; the fence sort key is exactly `FENCE`, the attempt sort key is exactly
   `ATTEMPT#<attempt id>`, and the fence names that attempt;
2. FENCE is exactly one nonterminal safety form: `active` requires table/attempt/holder/token and forbids
   `parked_at`; `parked` requires the same fields plus one canonical `parked_at`. No query, owner,
   ordinal, statement, envelope, result, or reconciliation field participates in the safety form;
3. ATTEMPT is exactly one nonterminal form. `intent` requires immutable core plus absent query,
   submission, evidence, cancellation, park, terminal, quantity, and usage fields. `submitted` additionally
   requires `valid_query_execution_id`, defined throughout this document as a value whose type is String
   and that contains at least one non-whitespace character, plus canonical `submitted_at` and the
   write-once closed `sanitized_start_evidence` form, and forbids
   cancellation, park, terminal, quantity, and usage fields. `cancel_requested` requires that same valid
   query identity, canonical submission, evidence, and `cancel_requested_at`, and forbids park, terminal, quantity,
   and usage fields. `parked` requires the immutable core and canonical `parked_at`, permits only the
   exact absent-query-and-absent-evidence pair or the exact
   valid-query/write-once-submission/write-once-evidence triple, permits a write-once
   cancellation instant only with the valid-query triple, and forbids terminal, quantity, and usage fields;
4. immutable table id, attempt id, exactly one owner binding, ordinal, statement kind, holder, fencing
   token, writer/operator execution id, canonical start envelope, client request token, and stored
   envelope digest are present and valid; recomputing the attempt id, client token, and envelope digest
   from those immutable fields reproduces the stored bytes;
5. table id, attempt id, holder, and fencing token are byte-equal across FENCE and ATTEMPT; FENCE
   `active` requires ATTEMPT `intent|submitted|cancel_requested`, while FENCE `parked` requires ATTEMPT
   `parked` and a byte-equal park instant; and
6. exactly one owner binding is valid: either one existing plan unit under
   `partition_key=<SUCCESSOR_OWNER.active_plan_key>, record_key=UNIT#<physical table id>` with that
   plan's `record_key=META`, its admitted origin intent `META`, and exact writing execution, ordinal,
   current-attempt, holder/token, unit reservation count `1..10`, and plan reservation count `1..32`,
   or one existing inline `TABLE_ATTEMPT#<table>/RUN#<run id>` in exact
   `open|cancel_requested|reconciled` state with matching execution, ordinal, current attempt,
   holder/token, and reservation count `1..10`. The named attempt carries the exact permanent
   reservation identity, and the owner counters and permanent-slot state must reproduce that
   reservation byte-for-byte. Both or neither owner, an unreserved attempt, an out-of-range counter, or
   a permanent-slot mismatch is invalid.

`core_invalid(table_id,fence,attempt)` is the closed complement of `core_valid` for every pending-GSI
candidate: malformed projected key syntax; base keys that do not resolve; absent FENCE or named ATTEMPT;
bad key/table/attempt syntax or equality; missing, extra, retyped, or invalid FENCE safety-core fields;
missing, extra, retyped, or invalid ATTEMPT core fields; FENCE/ATTEMPT state disagreement; other state/
required/forbidden-field disagreement; immutable constructor/digest disagreement; pair binding or park-
instant disagreement; or zero, multiple, missing, malformed, or mismatched owner bindings. No
reconciliation attribute can make core invalid or valid.

For a core-valid pair, reconciliation is a separate closed form. `reconciliation_valid` is true only
for one of:

- pending: `reconcile_status="pending"`, canonical
  `reconcile_after_or_block=<20-digit UTC epoch-micros>#<binding digest>`, nonnegative
  `reconcile_attempt_count`, optional complete prior requeue audit, and no block fields; or
- blocked: `reconcile_status="blocked"`, no pending due/count fields, and the complete block audit:
  deterministic `reconcile_block_id`, reason exactly
  `MALFORMED_RECONCILIATION_METADATA|SAME_SNAPSHOT_RESCHEDULE_REJECTED`, canonical owner-observed block
  instant, exact observation digest, one canonical lowercase UUIDv4 recovery reference, and
  `reconcile_after_or_block=BLOCK#<recovery ref>`.

`repairable_reconciliation_metadata` is true exactly when the candidate came from the pending GSI, its
projected base keys resolve to the strongly read pair, `core_valid` is true, `reconciliation_valid` is
false, and every malformed, missing, extra, retyped, or wrong-valued field belongs only to the pending/
blocked reconciliation field set; no key, safety-core, attempt-core, immutable, pair, or owner field may
be malformed. The block observation retains the exact presence/type/value snapshot for every
reconciliation field, including absent fields.

The binding digest is
`sha256(canonical_json([physical_table_id, attempt_id, lease_holder, fencing_token]))`. The remaining
blocked-audit and requeue values each have exactly one constructor:

```text
observation_digest = canonical_authority_digest(["stage1-reconcile-observation/v1", <variant>,
  [[name, tag, value-or-null] for every reconciliation field in exactly this order:
   reconcile_status, reconcile_after_or_block, reconcile_attempt_count, requeued_from_case_ref,
   requeued_at, reconcile_block_id, reconcile_block_reason, reconcile_blocked_at,
   reconcile_observation_digest, reconcile_recovery_ref]])
reconcile_block_id = canonical_authority_digest(["stage1-reconcile-block/v1",
  binding_digest, reason, observation_digest, blocked_at])
reconcile_recovery_ref = UUIDv4-form rendering of
  sha256(UTF8("stage1-reconcile-block/v1#recovery-ref#" + reconcile_block_id))
requeue_id = canonical_authority_digest(["stage1-reconcile-requeue/v1",
  reconcile_recovery_ref, operation_id])
```

`<variant>` is exactly `malformed_reconciliation_metadata|same_snapshot_reschedule_rejected`, so the
two §3.2 observation forms never collide. Each observation member's `tag` is exactly
`absent|malformed|text|integer|boolean`; `absent` and `malformed` carry null, and the typed tags carry
the exact retained value. The recovery-reference rendering takes the digest's first 32 hex characters,
sets zero-indexed position 12 to `4` and position 16 to the character of `89ab` selected by the 33rd
hex digit modulo 4, and hyphenates as `8-4-4-4-12`, so the reference derives from the block identity
alone and exact replay reproduces identical bytes. No other derivation exists. A canonical
blocked FENCE's transaction creates the retained protected case
`partition_key=PROVIDER_RECONCILE_BLOCK#<reconcile_recovery_ref>, record_key=CASE` with
`state=blocked`, exact table/attempt/block/binding selectors, binding digest, safe closed reason,
blocked time, retained malformed-observation digest when applicable, and empty requeue audit. It has no
TTL, malformed field values, partial audit, query envelope, provider result, customer content, or owner
content. The opaque recovery reference embeds no selector and is never reused; a later reblock allocates
a new reference/case and leaves the old case `requeued` as audit. `state=active|parked` is unchanged by
pending/blocked reconciliation: blocked is discovery isolation, never provider terminality or permission
to write. A `clear` fence carries no reconciliation attributes. `FENCE` has no expiry or quarantine-
duration field. Each attempt immutably stores the canonical Athena start request envelope and digest;
this adapter-owned envelope contains no result rows or credentials and replays the identical
`StartQueryExecution` after process loss. §5–6 own transitions.

Before the pre-switch fence, `DataV2Stack` adds the sparse GSI
`table-attempt-reconcile` to the publications table:

```text
partition key = reconcile_status (String; indexed values exactly "pending" | "blocked")
sort key = reconcile_after_or_block (String; pending is <20-digit epoch-micros>#<binding digest>;
           blocked is BLOCK#<opaque reconcile_recovery_ref>)
projection = KEYS_ONLY
```

Only `FENCE` items in `active|parked` populate the index. Pending and blocked rows share this one
attempt-reconcile GSI but occupy separate partitions; the reconciler queries only `pending`. `KEYS_ONLY`
projects only base and index keys. `BeginTableAttemptRequest` creates a core-valid active pending pair;
`AcquireTableFenceRequest` rejects every active/parked pair unless `core_valid` and
`reconciliation_valid`; live submission, cancellation, parking, recovery submission, rescheduling,
typed blocking, and requeue each preserve `core_valid` by their closed condition/write sets;
`CompleteTableAttemptRequest` alone atomically makes ATTEMPT terminal and FENCE clear. No other writer,
raw transaction, direct update, migration, or adoption path may write these prefixes. Therefore
core-invalid state is unconstructable from the verified pre-switch all-canonical namespaces by
induction over
every authorized typed mutation: the §2.5 verification proves every existing reserved row recomputes
as exact canonical builder output (the base case), and every authorized mutation preserves canonicity
(the step). Pending work is either reconciliation-valid or
`repairable_reconciliation_metadata`; blocked work is canonical and requeue always consumes that
canonical block output and returns canonical pending. Only provider-terminal completion clears safety.
`DataV2Stack` must read this exact GSI as `ACTIVE` before the pre-switch fence enables any amended writer
or reconciler. No table scan, persisted cursor, caller selector, adoption/backfill bypass, or parallel
discovery path exists. The attempt names exactly one queued unit or the operator run/table item below; it is the
sole authority for query id, provider terminal state, `query_count`, `scanned_bytes`, and `engine_ms`.

Every inline writer creates one durable accounting item per run and physical table before that table's
first statement:
`partition_key=TABLE_ATTEMPT#<physical table id>, record_key=RUN#<run id>`, where
`run_id=sha256(UTF8(canonical_json([writer_kind, operation_id, execution_id, physical_table_id])))` and
`writer_kind ∈ {transitional_delivery, ingest_delivery_operator,
reprocess_semantic_v2_operator}`. Immutable fields are that tuple, the public status flow/execution
bindings from §10, table kind, ordered statement-intent kinds, pre-encoded pointer-finalization payload,
and creation instant. Mutable fields are
`state ∈ {open,cancel_requested,reconciled,terminal}`, current attempt id, next ordinal, exact
`accepted_attempt_reservations`, terminal outcome/instant, retained attempt references, and status/usage
outbox dispositions. Inline Begin reserves only while `accepted_attempt_reservations < 10`; every
successful Begin reservation permanently consumes one slot, including ambiguous acceptance.

Creation is `absent→open`; exact replay requires byte-identical immutable fields and resumes the current
ordinal. With a durable `valid_query_execution_id`, `open|reconciled→cancel_requested` records only that
cancellation was requested while the current attempt remains nonterminal; without that identity the
transition is forbidden. After observing that attempt's provider terminal, the direct path
returns to `open` and advances the ordinal; the reconciler changes
`open|cancel_requested→reconciled`, advances the same ordinal, and clears only the exact terminal
attempt's fence/parked lease. `reconciled` is nonterminal and resumable: exact replay reacquires a new
lease after the now-clear fence and continues the next statement or pointer finalization. For a current
table, only lease-held `finalize_inline` may change `open|reconciled→terminal`, and it atomically commits
all pointer outcomes, publication events, run terminal, and closing outboxes. Source/candidate runs use
the same terminal transition without pointers after their final statement. A provider `CANCELLED`
result is retained on its attempt and determines retry/final disposition; cancellation request alone
never terminalizes the run.

Every run transition creates or exactly replays a deterministic internal event/outbox:
`semantic_inline_run_recorded`, `semantic_inline_run_cancel_requested`,
`semantic_inline_run_reconciled`, or `semantic_inline_run_terminal`. `PersistInlineRunRequest` owns
atomic `RUN#` creation plus `semantic_inline_run_recorded` and the exact lifecycle-head advance before
that run's first statement. A pre-statement `TABLE_BUSY`/`TABLE_ATTEMPT_PENDING` uses only the inline
tag of `RecordStage1RefusalRequest` against the exact open or reconciled run to create
`semantic_inline_run_refused`; it never pretends the run is terminal and never changes run state. The
run item is an index and lifecycle/outbox authority, not a second query ledger. It stores attempt
references but never copies provider quantity values. Status projection joins those references to
terminal attempt items using the status LLD; Athena-only Stage-1 usage identity and category meaning are
owned by
[Data-Flow Completion and Service Levels](../../concepts/data-flow-completion-and-service-levels.md#stage-1-usage-attribution).
All run, attempt, event/outbox, usage, head, and fence records follow the indefinite-retention rule
above. Successful asynchronous delivery removes only the sparse-index attributes from an outbox; it
never deletes or expires the base item or immutable payload. Reconciliation resumes pending delivery
without re-running statements.

### 3.2 Coordinator authorization and typed request boundary

`DynamoProjectionCoordinator` is the named narrow exception to ordinary adapter prefix ownership.
Every principal that calls one of its builders receives separate IAM statements against the exact
environment's publications-table ARN and leases-table ARN; no statement uses a wildcard resource.
DynamoDB authorizes a transaction by checking the underlying action for each member, so these policies
never grant `dynamodb:TransactGetItems` or `dynamodb:TransactWriteItems` as IAM actions. The complete
action vocabulary is only `dynamodb:GetItem`, `dynamodb:PutItem`, `dynamodb:UpdateItem`,
`dynamodb:ConditionCheckItem`, and — solely for the `STATUS_FLOW#*` root-clock readback below —
`dynamodb:Query`, with only the subset a builder emits for each key family.

> **Amendment (2026-09-21, root-clock readback).** Every successor lifecycle transition must repeat
> the flow's applied root `clock_started_at` byte-for-byte (the status reducer rejects a differing
> clock as `flow clock changed`), and the closed 12-field `HEAD` deliberately carries no clock. The
> durable owner timestamp is therefore the flow's own retained root `EVENT#` item, which each owner
> strongly queries under its `STATUS_FLOW#<flow_id>` partition before deriving a successor proof.
> This adds one `dynamodb:Query` statement with leading key `STATUS_FLOW#*` to the transitional
> delivery, table-writer, plan-intent-reconciler, and provider-attempt-reconciler principals. It is
> the only `Query` in this vocabulary; the no-`Scan` rule and every other row are unchanged.

Every action has a `ForAllValues:StringLike` `dynamodb:LeadingKeys` condition split by exact resource
and key family. Direct strongly consistent reads use a dedicated `dynamodb:GetItem` statement without
an enclosing-operation condition. The sole direct mutation uses a separate publications-table
`dynamodb:UpdateItem` statement with leading key `TABLE_ATTEMPT#*` and no
`dynamodb:EnclosingOperation` condition: `RecordSubmittedAttemptRequest` for live-path principals. A
transaction-read member uses underlying `dynamodb:GetItem` plus
`StringEquals: {"dynamodb:EnclosingOperation": "TransactGetItems"}`; a transaction-write member uses
its underlying `PutItem`, `UpdateItem`, or `ConditionCheckItem` plus
`StringEquals: {"dynamodb:EnclosingOperation": "TransactWriteItems"}`. No transaction-scoped statement
can authorize the same action outside its enclosing transaction.

| Exact resource and leading partition keys | Direct strong-read action | Direct typed mutation | Transaction-read member | Transaction-write members |
|---|---|---|---|---|
| leases-table ARN; `TABLE#*` | `GetItem` | — | `GetItem` | `UpdateItem`, `ConditionCheckItem` |
| publications-table ARN; `TABLE_ATTEMPT#*` | `GetItem` | `UpdateItem` — only `RecordSubmittedAttemptRequest` | `GetItem` | `PutItem`, `UpdateItem`, `ConditionCheckItem` |
| publications-table ARN; `PROVIDER_RECONCILE_BLOCK#*` | `GetItem` | — | — | `PutItem`, `UpdateItem`, `ConditionCheckItem` |
| publications-table ARN; `WRITE_PLAN#*` | `GetItem` | — | `GetItem` | `PutItem`, `UpdateItem`, `ConditionCheckItem` |
| publications-table ARN; `STATUS_FLOW#*` | `GetItem`; `Query` — only the root-clock readback of the flow's retained root `EVENT#` item | — | `GetItem` | `PutItem`, `UpdateItem`, `ConditionCheckItem` — `HEAD` only, required by the claim/reschedule/admission-failure plan-`HEAD` agreement members |
| publications-table ARN; one statement per exact registered canonical-dataset prefix `<dataset-key>|*` | `GetItem` | — | `GetItem` | `PutItem`, `UpdateItem` |

The exact direct statement, instantiated with the environment's concrete retained publications-table
ARN, is:

```json
{
  "Effect": "Allow",
  "Action": "dynamodb:UpdateItem",
  "Resource": "<exact-publications-table-arn>",
  "Condition": {
    "ForAllValues:StringLike": {
      "dynamodb:LeadingKeys": ["TABLE_ATTEMPT#*"]
    }
  }
}
```

It deliberately has no `dynamodb:EnclosingOperation` member.

The table is a maximum vocabulary, not a grant copied to every caller. A principal's statements omit
action/prefix combinations its allowed builders cannot emit. The direct `TABLE_ATTEMPT#*` mutation
statement exists on the transitional delivery, table-writer, and `SemanticProjectionOperatorRole` only
for `RecordSubmittedAttemptRequest`; no scheduled reconciler receives a direct mutation statement.
`RecoverParkedAttemptSubmissionRequest` instead uses transaction-enclosed FENCE `ConditionCheckItem`
and ATTEMPT `UpdateItem` members. Transaction-read `GetItem` is present only when that builder uses
`TransactGetItems`; otherwise its direct-read statement is the sole `GetItem` grant. There is no `Scan`,
`Query`, `BatchWriteItem`, `DeleteItem`, table-management action, wildcard ARN, wildcard leading key, or
other unconditioned write in the coordinator vocabulary. IAM constrains table ARN, partition-key prefix,
underlying action, and enclosing operation where the service supports those conditions; it does **not**
claim to constrain `record_key`, condition clauses, update clauses, or individual attributes. The
private direct-mutation builder therefore supplies the second boundary for
`RecordSubmittedAttemptRequest`; the coordinator's recovery builder supplies the transactional boundary
for the parked-preserving recovery fields. Callers cannot vary either operation's sort key, field set,
condition, transition, or expression.

Private typed builders are the second enforcement layer. Every multi-item or cross-family mutation in
this design is a method of `DynamoProjectionCoordinator`; no caller composes a transaction and no
transaction is implied by prose. The only plan-intent typed operations are
`SubmitPlanIntentRequest`, `ClaimPlanIntentRequest`, `PersistPlanRequest`,
`ReschedulePlanIntentRequest`, `AdmitUnitRequest`, `FailAdmissionRequest`, `FailUnitRequest`, and
`CompletePlanRequest`. Table-writer execution separately uses `ClaimUnitRequest`; inline lifecycle uses
`PersistInlineRunRequest`; both execution families use the closed `RecordStage1RefusalRequest`.
`SettleStage1AthenaUsageRequest` is a private closed sub-builder that may be expanded only inside the
final owner-disposition builders named below; it has no independently callable store method.
`RecordSubmittedAttemptRequest` is the sole non-transactional mutation in this list and remains an
ordinary single-item `DynamoTableAttemptStore` operation. `RecoverParkedAttemptSubmissionRequest` is a
`DynamoProjectionCoordinator` transaction that condition-checks the exact parked FENCE snapshot and
updates the ATTEMPT atomically. `Stage1DynamoByteOracle` is the one shared pure encoder/measurement
helper invoked by feasibility and by every coordinator/store builder immediately before its SDK call;
no builder owns a second estimator.

Every request accepts only enums and validated plan, unit, run, partition, table, lease, attempt,
lifecycle-head, pointer, and expected-state identities. Constructors reject inconsistent identity
tuples. Builders privately construct every table name, partition key, `record_key`, condition/update
expression, expression-name map, and expression-value map; callers cannot supply a raw key, sort key,
SQL, expression string, transaction member, or attribute map. Before an SDK call, the common validator
expands the request and rejects a table ARN, partition prefix, sort-key form, action, field set, state
transition, owner binding, or fence binding outside these matrices.

| Request | Closed caller-supplied fields | Builder-derived keys and mutation |
|---|---|---|
| `SubmitPlanIntentRequest` | exact root/source plan keys, plan digest, origin and exact source identity/failure transition, immutable maximum-15-unit canonical roster from which the builder derives `intent_roster_tuple`, `roster_digest`, `intent_authority_tuple`, and `intent_id`, due/audit values from which it derives `ready_status="ready"` and `ready_order=<20-digit due epoch-micros>#<intent_id>`, and exact owner snapshot; no generation or caller-supplied digest field | Derives the sole exact generation-independent `stage1-plan-intent/v1` authority; puts one pending intent `META` with both ready fields and no generation plus every intent `UNIT#`; consumes the owner through an exact condition or optional absent-owner bootstrap. Exact tuple replay is a no-op; any listed field change is a distinct intent. |
| `ClaimPlanIntentRequest` | exact due pending intent with no generation and both ready fields, owner revision/`last_generation`, optional active plan/intent and plan-`HEAD` observation, operation instant | Atomically computes `claimed_generation=owner.last_generation+1`, CASes owner revision to install only `reserved_intent_id`, byte-identical `reserved_intent_digest`, and `reserved_generation`, changes the intent `pending→claimed`, stores the identical `claimed_generation`, and removes both ready fields. Exact replay returns the same assignment. |
| `ReschedulePlanIntentRequest` | exact owner reservation including `reserved_generation`, intent `claimed_generation` and immutable bytes, retry/due/audit values from which the builder derives pending ready fields, and complete owner/plan-`HEAD` observation | Never invents or changes generation while claimed. Clears the reservation and changes the same intent `claimed→pending`, removes `claimed_generation`, and restores both ready fields; no blocked state or permanent-refusal transition exists — non-retryable conditions fail closed without mutation. Exact replay preserves current assignment or resulting schedule/audit/ready bytes. |
| `PersistPlanRequest` | exact claimed intent and owner reservation with `intent.claimed_generation=owner.reserved_generation=owner.last_generation+1`; source/predecessor plan evidence; target plan canonical intent authority and roster digest; exact lifecycle proofs and `HEAD` | Derives the exact `stage1-plan-materialization/v1` internal execution identity and plan key from `intent_id` plus `claimed_generation`, creates plan `META`/units, and atomically swaps the owner. Ordinary maps are `<=20` actions. Planned-plan replacement has exactly one owner update and is `<=100` actions. It clears `reserved_*`, installs paired active fields, advances `last_generation` to `claimed_generation` plus last/root/execution fields, and retains the assigned generation on exact replay. |
| `AdmitUnitRequest` | exact owner-active plan/intent, plan's assigned generation, plan and unit admission counters, exact §4 canonical body bytes, physical-unit group, and deduplication digest, and admission observation | Changes one plan unit `pending→admitted` exactly once; the last admission changes the same intent `claimed→admitted`, requires and retains matching `claimed_generation`, leaves both ready fields absent, and writes the admitted lifecycle effect. |
| `FailAdmissionRequest` | exact active candidate plan and same claimed intent including its assigned `claimed_generation`, unit admission observations, owner/plan-`HEAD` agreement, terminal proof, and retry schedule | Atomically terminalizes the candidate as `admission_failed`, clears paired owner active fields, preserves owner/plan-`HEAD` agreement, and changes the same intent `claimed→pending` with its immutable origin bytes unchanged, removes `claimed_generation`, and restores `ready_status="ready"` plus `ready_order=<20-digit due epoch-micros>#<intent_id>`. It never creates another intent partition; the next claim assigns the then-current owner generation. |
| `ClaimUnitRequest` | exact owner-active/head-agreeing plan and origin intent; plan `META` in planned status; plan unit in `queued|failed|writing`; writer execution and queue receive identity; observed `failed_count`; for a `writing` unit the exact observed prior `writer_execution_id` and its derived queued lease holder; operation instant; opaque lifecycle/head proof | Conditions the owner/`HEAD`, planned plan `META`, admitted origin intent, unit state, and receive identity; changes `queued→writing` or `failed→writing`, decrementing `failed_count` exactly once only for redrive; or reclaims `writing→writing` under a different writer execution only when the unit binds no nonterminal attempt (`current_attempt_id` absent) and the prior holder's table lease is absent, expired, or released — one leases-table `ConditionCheckItem` in the same transaction. Every branch records the new writer identity and creates the unit-started event with one head effect. Permanent Begin reservation counters never decrement. |
| `FailUnitRequest` | exact plan/unit/writer/lease identity, closed failure disposition, ordered terminal-attempt observations, plan `META`, origin intent, fence, lease, lifecycle/head proof, and pre-encoded settlement authorities; attempt-budget exhaustion additionally requires permanent reservations exactly 32, rejected ordinal 33, the exact deterministic successor intent identity/roster, and source-plan failure transition | Ordinary failure updates unit/counters and settlement authority. Rejected ordinal 33 creates no Begin reservation: the transaction replaces the ordinary failure trigger with the one origin `plan_attempt_budget` successor intent `META` plus every successor `UNIT#`, consumes the owner, and terminalizes the source plan. Trigger and maximum intent-item puts have independent byte/action fixtures; exact replay cannot fan out. |
| `PersistInlineRunRequest` | closed writer kind, operation/execution/table identities, table kind, immutable statement/finalization roster, creation instant, and exact observed head; no caller public identity, topology, pointer source, surface, status, or table-kind mapping | Consumes every registry/status-owned value from the linked event-projection contract, then creates the run, recorded event authority, and run head atomically before the first statement. |
| `RecordStage1RefusalRequest` | closed tag `unit|inline`; reason `TABLE_BUSY|TABLE_ATTEMPT_PENDING`; refusal instant; exact claimed unit identity plus observed claim and plan for `unit`, or exact open/reconciled run identity plus observed run for `inline`; exact observed lifecycle head | Derives condition checks for the exact owner items, the owning flow's `STATUS_FLOW#` partition deterministic refusal `EVENT#`, and exact same-partition owner `HEAD`; puts the event and advances only that head atomically. |
| `RecoverParkedAttemptSubmissionRequest` | exact strongly read ATTEMPT and FENCE versions; attempt/table/owner identity; holder and fencing token; `parked_at`; immutable envelope digest and client token; recovered identical `valid_query_execution_id`; write-once `submitted_at` | Derives one transaction with a `ConditionCheck` on the complete parked FENCE snapshot and an `Update` on the complete parked ATTEMPT snapshot. It sets only the recovered identical valid query identity, write-once submission instant, and write-once closed `sanitized_start_evidence` form while both remain parked; any reschedule/block/requeue or other FENCE-only race aborts the transaction. |
| `BlockTableAttemptReconcileRequest` | one `ValidNonterminalAttemptPair`, exactly one typed observation variant—`MalformedReconciliationMetadataObservation` with the exact reconciliation-field presence/type/value snapshot, or `SameSnapshotRescheduleRejectedObservation` with the exact rejected/reread snapshot—and owner block instant | Derives reason, observation/binding digests, deterministic block id, opaque recovery ref, blocked GSI key, and protected case. It conditions exact valid core plus the supplied malformed reconciliation snapshot, replaces only reconciliation fields with canonical blocked form, removes malformed partial reconciliation audit while retaining its digest in the case, and creates the absent protected case atomically. It never requires a valid old pending tuple. |
| `RequeueBlockedTableAttemptReconcileRequest` | exact function-resolved canonical block output: protected case, blocked FENCE, and named ATTEMPT snapshots; function-owned `operation_id`, sanitized resolution reason, and requeue instant | Derives deterministic requeue id and canonical pending due key/count; requires byte equality across canonical case/FENCE/ATTEMPT selectors and blocked generation; atomically changes FENCE blocked→pending and case blocked→requeued audit. No repair or correction step exists. It rejects caller selectors, stale/terminal generations, and raw keys/maps/expressions. |
| `CompletePlanRequest` | exact owner-active plan/intent and owner/plan-`HEAD` agreement; planned plan `META` with canonical intent roster, `child_outcome_reduction_version=2`, sorted generation-bound `required_child_expectations`, and complete settled-attempt count/digest; complete ordered unit observations carrying matching `owner_generation` and accepted-attempt observations with byte-identical settlement authorities; completion instant. The conditioned full-admission counters immutably prove the origin intent's `admitted` state; no intent transaction member exists | At most 51 actions. Requires full admission, every unit attested at the plan generation, zero failures, reconstructable expectation authority, and every accepted attempt terminal/already settled with matching aggregates. Changes only plan `META.status planned→completed`, terminal time, paired owner active fields, exact owner/plan-`HEAD` agreement, and terminal lifecycle authority carrying the internal expectation proof; it leaves intent state admitted and adds no expectation field to the public wire. |
| `SettleStage1AthenaUsageRequest` | closed owner-disposition tag and exact durable pointer/disposition outcome; ordered referenced terminal-attempt observations; expected owner state; pre-encoded deterministic authority payloads | Derives each attempt's unsettled→settled update and exactly one immutable `USAGE#` contribution or unavailable-evidence item per accepted Athena attempt; callers never supply a category. |

`PersistInlineRunRequest` exact replay is a no-op only when the run, recorded event, and resulting head
are byte-identical; any occupied run/event key or head transition with different immutable identity,
roster, payload, sequence, or predecessor is an invariant collision. `RecordStage1RefusalRequest`
requires the exact currently observed claim/run state and is similarly a no-op only for byte-identical
event/head replay, including after a later contiguous head advance. It changes no lease, `FENCE`,
provider attempt, pointer, owner terminal state, or counter. A stale owner/head observation or changed
reason/payload conflicts rather than being rebuilt against a different owner.

The table-attempt item has immutable table/attempt/owner identities, ordinal, statement kind, canonical
start envelope, envelope digest, client request token, permanent reservation identity, and sanitized
start evidence. Its mutable state graph is exactly `intent→submitted`,
`submitted→cancel_requested`, `intent|submitted|cancel_requested→parked`, `parked→parked` only
to attach the recovered identical `valid_query_execution_id` and write-once submission instant or, when
that valid identity is already durable, the write-once cancellation instant, and
`submitted|cancel_requested|parked→terminal`. `intent` after Begin means acceptance is ambiguous, never
that no request was accepted, and cannot be cancelled. A cancellation first requested after parking
requires a durable valid query identity, keeps `state=parked`, and sets
the write-once cancellation instant. Terminal records carry only provider state
`SUCCEEDED|FAILED|CANCELLED`, terminal instant, exact available scanned-byte/engine-time evidence,
missing-quantity flags, and `athena_usage_state=unsettled|settled`. Every Begin reservation permanently
counts; no state or operation releases or decrements it. A settled attempt also stores the immutable
`owner_disposition` tag, concept-derived `usage_category`, and `usage_authority_digest`. `FENCE` is
clear, active, or parked with the existing exact bindings; only a valid nonblank query execution
identity followed by a provider-terminal observation permits `CompleteTableAttemptRequest` to clear it.

| Typed operation and owner | Exact keys and conditions | Exact writes | Underlying actions |
|---|---|---|---|
| `SubmitPlanIntentRequest` — coordinator | exact root/source plan keys, plan digest, origin/source identity or failure transition, immutable maximum-15-unit canonical roster, due/audit values, and exact owner snapshot; no generation or digest input | Derive the sole `intent_roster_tuple`, `roster_digest`, `intent_authority_tuple`, and `intent_id`; put exactly one pending `META` with no generation, `ready_status="ready"`, and `ready_order=<20-digit due epoch-micros>#<intent_id>` plus every canonical `UNIT#` row. Consume an existing owner with one `ConditionCheckItem`, or optionally bootstrap an absent owner with one `PutItem`. Exact complete-authority replay changes nothing; changed authority produces a distinct key, while noncanonical bytes reject. | intent `META`/units `PutItem`; exactly one owner `ConditionCheckItem` or bootstrap `PutItem` |
| `ClaimPlanIntentRequest` — coordinator, scheduled `PlanIntentReconciler` only | exact due pending intent with no generation and both ready fields, owner revision/`last_generation`, optional active plan/intent and plan-`HEAD`, operation instant | In one transaction compute `claimed_generation=owner.last_generation+1`; CAS the owner revision to install exactly `reserved_intent_id`, byte-identical `reserved_intent_digest`, and `reserved_generation`; change intent `pending→claimed`, store the identical `claimed_generation`, and remove both ready fields. Exact replay returns the same assignment; a competing reservation is classified after a complete strong reread. | owner and intent `UpdateItem`; optional active plan/head `ConditionCheckItem` |
| `ReschedulePlanIntentRequest` — coordinator, scheduled `PlanIntentReconciler` only | exact reservation including `reserved_generation`, matching intent `claimed_generation` and immutable bytes, owner/plan-`HEAD` agreement, retry/due/audit values from which the builder derives pending ready fields | Never invent or change generation while claimed. Consume the exact reservation and change intent `claimed→pending`, removing `claimed_generation` and restoring canonical due plus both ready fields; no blocked state or permanent-refusal transition exists — non-retryable conditions fail closed without mutation. Exact replay preserves bytes. | owner/intent `UpdateItem`; plan head `ConditionCheckItem` when a plan exists |
| `PersistPlanRequest` — coordinator | exact owner reservation and claimed target intent with `reserved_generation=claimed_generation=last_generation+1`; target plan canonical intent authority and roster digest; optional source planned plan complete unit/attempt evidence and aggregate proof; deterministic lifecycle proofs and exact head | Derive the exact internal execution tuple and plan key from `intent_id` plus `claimed_generation`; atomically create plan `META`/units, write lifecycle authority/head, clear reservation, advance `last_generation` to `claimed_generation`, and swap owner. Ordinary persistence is `<=20` actions. Planned replacement terminalizes the source plan and installs the target with exactly one owner update and `<=100` actions. Exact replay retains the assigned generation. | owner/plan/head mutations plus unit/event puts; replacement adds source-plan actions and a second event, never a second owner action |
| `AdmitUnitRequest` — coordinator | exact owner-active plan/intent and plan generation, planned plan `META`, claimed intent `META` with matching `claimed_generation` and both ready fields absent, unit and admission counters, exact §4 canonical body bytes/group/deduplication digest, event/head on final admission | Admit one plan unit once; final admission changes intent `claimed→admitted`, retains `claimed_generation`, leaves both ready fields absent, and writes one event/head effect. | plan unit/plan `META`/intent/owner/head `UpdateItem`; final event `PutItem` |
| `FailAdmissionRequest` — coordinator, `PlanIntentReconciler` only | exact active candidate plan, same claimed intent and its `claimed_generation`, unit admission observations, owner/plan-`HEAD` agreement, terminal proof, and retry schedule | Atomically terminalize candidate as `admission_failed`, clear paired owner active fields, and change the same intent `claimed→pending` with its immutable origin bytes unchanged; remove `claimed_generation` and restore `ready_status="ready"` plus `ready_order=<20-digit due epoch-micros>#<intent_id>`. No successor intent is created; reclaim assigns the then-current owner generation. | owner/plan `META`/intent/head `UpdateItem`; terminal event `PutItem` |
| `ClaimUnitRequest` — coordinator, table-writer only | exact `SUCCESSOR_OWNER` active plan/intent pair and copied plan-head agreement; planned plan `META`; admitted origin intent `META`; plan unit in `queued|failed|writing`; writer execution and receive identity; failed-count observation; for `writing` the exact prior `writer_execution_id`, absent `current_attempt_id`, and the prior queued holder `sha256(canonical_json([plan_key, unit_key, prior_writer_execution_id]))`; opaque event/head proof | Condition owner/plan `HEAD`, planned plan `META`, admitted intent, and exact unit state/receive; update unit to `writing` with the new writer execution; for `failed` only decrement plan `META.failed_count` once; for `writing` additionally condition the leases `TABLE#<physical table id>` item as absent, `holder <> <prior holder>`, or `expires_at <= <operation instant>` and change nothing else — a live prior holder aborts the reclaim, which the writer returns retryable with `TABLE_BUSY` visibility and no lifecycle event; put the started event under the new execution and advance one head. Exact same-execution replay performs no second update/decrement/event. Permanent reservation counters never decrement. | owner/intent/plan `META`/unit `ConditionCheckItem|UpdateItem` as required; leases `ConditionCheckItem` for the `writing` branch only; event `PutItem`; head `PutItem|UpdateItem` |
| `PersistInlineRunRequest` — coordinator | absent or byte-identical run; deterministic event key; exact run head; validated writer/operation/execution/table tuple and immutable roster | Consume public identity/topology/table-kind/status values only from the linked event-projection contract; atomically put run, event authority, and one run-head effect. | run/event `PutItem`; head `PutItem|UpdateItem` |
| `RecordStage1RefusalRequest` — coordinator | closed `unit|inline` tag; deterministic owner `EVENT#`; exact owner `HEAD`; unit variant condition-checks the active plan unit and plan `META`, admitted origin intent, and exact owner-active pair; inline variant condition-checks the exact `TABLE_ATTEMPT#<table>/RUN#<run id>` execution/current-attempt tuple in `open|reconciled`; reason only `TABLE_BUSY|TABLE_ATTEMPT_PENDING` | Put the deterministic unit or inline refusal event and advance its exact owner head. Owner items are condition-only. No owner-state, lease, fence, attempt, provider, pointer, counter, terminal, or usage mutation. | plan unit/plan `META`/intent, or run, `ConditionCheckItem`; event `PutItem`; head `PutItem|UpdateItem` |
| `AcquireTableFenceRequest` — coordinator | leases `TABLE#<physical table id>`; publications `TABLE_ATTEMPT#<same table>/FENCE`; the optional claimed-unit or inline lease-acquired event at the status-supplied `STATUS_FLOW#<owning flow UUID>/EVENT#<authority_digest>` key, plus the status-supplied exact same-partition `HEAD` key | Lease only: set exact holder and expiry and increment the monotonic fencing token when absent, expired, or already held by that holder. `FENCE` is condition-only and must be absent or exactly `clear` with no active attempt; any observed `active|parked` fence must carry one exact valid pending or blocked reconciliation disposition or the request fails as an invariant before returning busy. The optional tagged owner-lifecycle variant may put only `semantic_unit_lease_acquired` plus its exact head create/update; it is forbidden for an unclaimed unit or any other event. | leases `UpdateItem`; publications `ConditionCheckItem`; optional owner event `PutItem` and head `PutItem|UpdateItem` |
| `ReleaseTableFenceRequest` — coordinator | exact lease and matching `FENCE` | Set only lease `expires_at` to the canonical released instant and remove `parked_at` when present, conditioned on exact holder/token. `FENCE` is condition-only and must be absent or exactly clear. Holder and token are retained; no owner, event, attempt, or pointer changes. | leases `UpdateItem`; publications `ConditionCheckItem` |
| `BeginTableAttemptRequest` — coordinator | exact unexpired lease; absent `TABLE_ATTEMPT#<table>/ATTEMPT#<attempt id>`; matching absent/clear `FENCE`; exactly one owner with an unreserved ordinal, either queued `partition_key=<SUCCESSOR_OWNER.active_plan_key>, record_key=UNIT#<physical table id>` plus that plan's `record_key=META` and admitted origin intent, or inline `TABLE_ATTEMPT#<table>/RUN#<run id>`; queued unit count `<10` and queued plan count `<32`, or inline run count `<10` | Put the immutable attempt in `intent` with its exact permanent reservation identity; update `FENCE` from absent/clear to `active` with exact attempt/holder/token plus `reconcile_status="pending"`, `reconcile_attempt_count=0`, and initial due equal to the lease expiry's UTC epoch microseconds; bind only owner `current_attempt_id=<attempt id>`. Queued Begin increments plan UNIT and plan META reservation counters atomically; inline Begin increments RUN atomically. Exact replay observes the same occupied attempt/permanent slot and increments nothing. No Begin occurs beyond either applicable cap. | leases `ConditionCheckItem`; attempt `PutItem`; fence, owner, and queued `META` `UpdateItem` as applicable |
| `RecordSubmittedAttemptRequest` — `DynamoTableAttemptStore`, live path only | one exact `ATTEMPT#<attempt id>` | Conditional `intent→submitted`; set only `query_execution_id`, `submitted_at`, the write-once closed `sanitized_start_evidence` form, and `state`, requiring the immutable envelope digest and client token and a `valid_query_execution_id`, with either no prior query id or the identical valid id and byte-identical evidence; changed evidence bytes classify as invariant collision exactly like a changed id/time/digest/token. Exact replay is a no-op; a different, non-String, blank, or changed id/time/digest/token is an invariant collision. | publications direct `UpdateItem` |
| `RecoverParkedAttemptSubmissionRequest` — coordinator, scheduled provider-attempt reconciler only | exact strongly read parked `ATTEMPT#<attempt id>` and matching parked `FENCE`; identical attempt/table/owner, holder, fencing token, `parked_at`, envelope digest, client token, pending reconciliation snapshot, and absent-or-identical valid query identity/submission instant | One transaction condition-checks the complete parked FENCE snapshot and conditionally updates the complete parked ATTEMPT snapshot to set only the recovered identical `valid_query_execution_id`, write-once `submitted_at`, and the write-once closed `sanitized_start_evidence` form; both remain `parked`. A concurrent FENCE-only reschedule, block, or requeue aborts all effects. Exact replay with the same valid id/time is a no-op; non-String, blank, changed id/time/digest/token, or stale/mismatched pair conflicts. | FENCE `ConditionCheckItem`; ATTEMPT `UpdateItem`, both enclosed by `TransactWriteItems` |
| `RescheduleTableAttemptReconcileRequest` — coordinator, scheduled `ProviderAttemptReconciler` only | exact strongly read `active|parked` `FENCE`, its named nonterminal attempt, pending reconcile keys/count `c`, and candidate-local failure or still-nonterminal provider observation; one captured UTC operation instant | Condition the complete fence/attempt binding and old reconcile tuple; retain `reconcile_status="pending"` and any exact prior requeue audit; set `new_count=c+1`; replace only the due GSI key with operation-instant plus the exact delay `60,120,240,480,900` seconds for old count `c=0,1,2,3,>=4`. No jitter, floating point, or overflow is permitted. No lease, attempt state/evidence, owner, ordinal, pointer, lifecycle, usage, block, or terminal field changes. Exact replay preserves operation instant/count/due bytes; an ambiguous write strongly rereads and reuses that same request. Rejection against the unchanged observed snapshot must be followed by typed block. | attempt `ConditionCheckItem`; fence `UpdateItem` |
| `BlockTableAttemptReconcileRequest` — coordinator, scheduled `ProviderAttemptReconciler` only | one exact `ValidNonterminalAttemptPair`; exactly one `MalformedReconciliationMetadataObservation` or `SameSnapshotRescheduleRejectedObservation`; owner block instant | Condition exact canonical keys, complete `core_valid` FENCE/ATTEMPT/owner fields, and the observation's byte-exact reconciliation presence/type/value snapshot. Derive all reason/digest/block/ref/case values; replace only reconciliation fields with canonical blocked form, remove malformed partial audit while retaining its digest in the protected case, and atomically put that case. Core, lease, attempt, owner, ordinal, pointer, lifecycle, usage, and safety state are unchanged. Exact replay requires identical canonical blocked/case bytes. | attempt and owner `ConditionCheckItem`; fence `UpdateItem`; protected case `PutItem` |
| `RequeueBlockedTableAttemptReconcileRequest` — coordinator, `ProviderFenceRecoveryFunction` only | exact canonical block output resolved server-side: protected case, blocked core-valid FENCE/ATTEMPT, operation id, sanitized resolution reason, and one captured UTC requeue instant | Require byte equality across case and canonical blocked pair. Atomically replace blocked reconciliation fields with canonical pending `reconcile_attempt_count=0` and due exactly equal to the requeue instant's epoch microseconds, retain complete requeue audit, and change case `blocked→requeued`. The returned candidate is immediately eligible. Exact replay preserves the same instant/count/due bytes and returns `already_requeued`; stale, terminal, changed-operation, or consumed replay rejects. No repair/correction, provider call, owner effect, or FENCE clear is accepted. | attempt and owner `ConditionCheckItem`; fence and protected case `UpdateItem` |
| `RequestAttemptCancellationRequest` — coordinator | exact nonterminal attempt with durable `valid_query_execution_id` and submission instant, active/parked matching `FENCE`, and exactly one bound owner; for inline runs only, optional deterministic `semantic_inline_run_cancel_requested` event and exact head | Change attempt `submitted→cancel_requested`, or retain `parked`, and set write-once `cancel_requested_at`. Reject an absent, non-String, blank, or non-durable query identity before mutation. `FENCE` remains `active` or `parked` with unchanged bindings. A queued owner remains `writing` and is condition-only. The inline tagged variant additionally changes the run `open|reconciled→cancel_requested` per §3.1 — so no further Begin is accepted against the cancelled run — and puts only the cancellation event with its exact head advance. Queued units have no invented cancellation lifecycle event. Exact replay preserves the first instant. | attempt `UpdateItem`; fence `ConditionCheckItem`; queued owner `ConditionCheckItem` or inline run `UpdateItem`; optional event `PutItem` and head `PutItem|UpdateItem` |
| `ParkAttemptRequest` — coordinator | exact lease, attempt in `intent|submitted|cancel_requested|parked`, and matching `active|parked` `FENCE` with complete pending reconcile tuple and old count `c`; one captured UTC park-operation instant for a real active→parked transition | Lease: set only `parked_at`, preserving exact holder/token/expiry. Attempt: change a nonparked state to `parked` and set write-once `parked_at`, preserving query id, cancellation instant, and immutable start envelope. For real `active→parked`, FENCE retains pending status, sets `new_count=c+1`, and replaces due with operation-instant plus exactly `60,120,240,480,900` seconds for old `c=0,1,2,3,>=4`; exact parked replay preserves the original instant/count/due. No jitter, floating point, overflow, owner, pointer, lifecycle-head, or terminal field change exists. | lease, attempt, and fence `UpdateItem` |
| `CompleteTableAttemptRequest` — coordinator | exact `submitted|cancel_requested|parked` attempt with durable `valid_query_execution_id`, matching active/parked `FENCE`, exactly one bound owner with unchanged permanent reservation counters/slot, optional reconciled-inline event/head, and exact provider-terminal observation | Transition once to terminal with `SUCCEEDED|FAILED|CANCELLED` and exact known/missing Athena quantities; clear only the matching FENCE and reconciliation fields; append the attempt reference, clear `current_attempt_id`, and increment ordinal once. Reservation counters never decrement. It chooses no usage category and writes no usage authority. | attempt/fence/owner `UpdateItem`; optional event `PutItem`; head `PutItem|UpdateItem` |
| `FailUnitRequest` — coordinator with `SettleStage1AthenaUsageRequest` | exact active plan/generation, unexpired lease, clear matching FENCE, plan `META`/unit whose conditioned full-admission counters immutably prove the origin intent's `admitted` state, event/head proof, terminal attempt roster, and optional attempt-33 proof requiring permanent reservations exactly 32, rejected ordinal 33, and the exact deterministic successor intent tuple/roster/source-plan failure transition | Settle referenced attempts and ordinarily fail only the owned unit. Attempt-33 creates no Begin reservation: replace the ordinary trigger with the successor origin `plan_attempt_budget` intent `META` plus all intent `UNIT#` puts, consume the owner, terminalize the source plan, and retain exact replay. Trigger and maximum intent-item puts have independent byte/action fixtures. | lease/fence checks; plan unit (ordinary branch only — attempt-33 has no unit member, proven transitively by the lease check)/`META`/attempt/head updates; lifecycle, usage/evidence, successor intent `META`/units puts; exactly one owner action |
| `FinalizeUnitRequest` — coordinator with `SettleStage1AthenaUsageRequest` | exact unexpired lease; clear matching `FENCE`; active plan `META` — whose conditioned full-admission counters immutably prove the origin intent's `admitted` state, so no intent transaction member exists — and `UNIT#<physical table id>`; each validated canonical partition `/CURRENT`; one unit terminal event and exact opaque unit lifecycle/head proof; one headless pointer event per outcome carrying the linked status proof; every referenced accepted Athena attempt terminal and either unsettled or byte-identically settled | Atomically settle attempts and put usage/evidence authority; advance required `CURRENT` pointers; change plan unit `writing→attested`; increment plan `META.attested_count` and update its complete settled-attempt count/digest; put the unit event and advance only the unit head; put each headless pointer event. Lease and fence are condition-only. Plan completion is separate. | lease/fence `ConditionCheckItem`; current/plan unit/plan `META`/attempt `UpdateItem`; lifecycle and usage/evidence `PutItem`; exactly one unit-head `PutItem|UpdateItem`; no intent member; no pointer-head key/action |
| `FinalizeInlineRequest` — coordinator with `SettleStage1AthenaUsageRequest` | exact unexpired lease and clear matching `FENCE`; exact `RUN#<run id>`; validated canonical partition `/CURRENT`; one run terminal event and exact opaque run lifecycle/head proof; one headless pointer event per outcome carrying the linked status proof; every referenced accepted Athena attempt terminal and either unsettled or byte-identically settled | Atomically settle referenced attempts and put their deterministic usage/evidence authorities; update required `CURRENT` pointers; change `RUN# open|reconciled→terminal`; put the run terminal event and advance only the run head; put each status-supplied headless pointer event without a pointer-head action. Lease and fence are condition-only. The run cannot become terminal before settlement. | lease/fence `ConditionCheckItem`; current/run/attempt `UpdateItem`; lifecycle and usage/evidence `PutItem`; exactly one run-head `PutItem|UpdateItem`; no pointer-head key/action |
| `CompletePlanRequest` — coordinator, metadata-only | exact owner-active plan/intent and owner/plan-`HEAD` agreement; exact planned plan `META` whose canonical roster, `child_outcome_reduction_version=2`, sorted required-child generation map, and complete settled-attempt count/digest match complete plan-unit and accepted-attempt observations; every unit carries `owner_generation=plan.generation`; deterministic terminal event with the same internal expectation proof; counters prove `admitted_count=attested_count=unit_count` and `failed_count=0`, which immutably proves the origin intent's `admitted` state without an intent transaction member | In at most 51 actions, condition every plan unit attested at the expected generation and every accepted attempt terminal with already-settled bytes; change plan `META.status planned→completed`; clear paired owner active fields while retaining exact head agreement and matching `latest_internal_execution_id`; put the terminal lifecycle event and advance only the plan head. The intent remains admitted; the public event wire remains v1 without the internal map. Exact replay is a no-op. | unit/attempt `ConditionCheckItem`; owner/plan `META`/head `UpdateItem`; event `PutItem`; no intent member |

`SettleStage1AthenaUsageRequest` accepts the closed durable owner-disposition and pointer outcomes,
then validates and derives the category exclusively through the mapping and precedence owned by
[Data-Flow Completion and Service Levels](../../concepts/data-flow-completion-and-service-levels.md#stage-1-usage-attribution);
callers cannot supply or override a category. `FailUnitRequest`, `FinalizeUnitRequest`, and
`FinalizeInlineRequest` settle their exact referenced attempts in their owner-disposition transaction.
A planned-plan replacement requires every predecessor-plan accepted attempt already terminal and
byte-identically settled, then preserves those categories unchanged; it has no settlement expansion or
later reclassification path.
`CompletePlanRequest` is not a final owner-disposition builder: it requires every referenced accepted
attempt already terminal and byte-identically settled and never expands
`SettleStage1AthenaUsageRequest`. A unit or run cannot enter its disposition/terminal state, and a plan
cannot complete or be superseded, until every required accepted attempt is terminal and settled by its
owning disposition transaction or already byte-identically settled. Reconciliation that observes only
provider terminality never guesses a category.

For each accepted Athena attempt, settlement changes only `athena_usage_state=unsettled→settled`, stores
the derived category/disposition/authority digest, and puts exactly one deterministic immutable
contribution when both quantities are known or one unavailable-evidence `USAGE#` when either is missing.
That item's key is exactly `partition_key=TABLE_ATTEMPT#<physical table id>,
record_key=USAGE#<attempt id>` — one deterministic key per accepted attempt beside its `ATTEMPT#` record,
whichever member kind settlement selects.
Exact replay with the same owner outcome, category, known/missing quantities, and payload is a no-op;
any category or payload change conflicts. Every Begin-created attempt remains retained and must reach a
valid query identity plus provider-terminal evidence before settlement. The prepublication feasibility proof includes the largest final owner transaction: all fixed owner
actions, exactly one unit or run lifecycle-head action, every headless pointer `EVENT#` put, and one
attempt update plus one `USAGE#` put for every maximum referenced accepted attempt. With `p` pointer
outcomes, `n` accepted attempts, `s` additional branch-specific actions, plan units `u`, and total
plan attempts `t`, the exact action counts are:

```text
FinalizeUnitRequest:        F = 6 + 2p + 2n + s; p=37, n=10, s=0 => 100
FinalizeInlineRequest:      F = 5 + 2p + 2n + s; p=37, n=10, s=0 => 99
FailUnitRequest:            F = 6 + 2n + s;       n=10, s=0       => 26
SubmitPlanIntentRequest:    F = 2 + u;             u=15             => 17
ReschedulePlanIntentRequest:F = 3                                    => 3
attempt-33 FailUnitRequest: F = 7 + 2n + u;        n=10, u=15      => 42
CompletePlanRequest:        F = u + t + 4;         u=15, t=32      => 51
ordinary persistence:       F = u_s + 5;           u_s=15             => 20
planned replacement:        F = u_p + u_s + 6 + t + n; 15+15+6+32+32 => 100
```

The three-action plan-intent reschedule map is exactly the owner and intent updates plus the plan-`HEAD`
condition in §3.2. Its fixtures admit only the exact-reservation `claimed→pending`
transition, preserve immutable intent tuple and roster bytes, and reject
any unlisted state transition.

For attempt 33, the successor intent `META` put plus `u` intent-unit puts replace the one ordinary
failure trigger (the unit update); they do not append to it. The branch's seven fixed members are the
lease and fence condition checks, the plan `META` terminalization, exactly one owner action absent from
the ordinary branch — the same owner consumption `SubmitPlanIntentRequest` counts — the terminal event
put, the head advance, and the successor intent `META` put; `u` successor `UNIT#` puts and `2n`
settlement actions complete `F = 7 + 2n + u`. The branch carries no plan `UNIT#` member: the lease
condition check — whose queued holder is the §5 hash of `[plan_key, unit_key, writer_execution_id]` —
plus lease-fenced exclusivity over unit transitions proves the unit's `writing` state and writer
binding transitively, per §3.1's no-transaction-member convention. The transaction terminalizes the
source plan and records the exact source-plan failure transition. The trigger and maximum-size successor `META` and
`UNIT#` puts have independent byte/action fixtures. The retained successor is claimed through the same
reservation CAS as every other pending intent. Planned-plan replacement consumes that already-created
intent, requires every predecessor accepted attempt terminal and already settled, and performs exactly
one owner update. The independently reviewed request maps pin each actual supported branch: ordinary
persistence stays within 20 actions, planned replacement stays within 100 actions, and
`CompletePlanRequest` is the exact `u+t+4=51` operation with zero settlement writes or bytes. Neither proof includes a pointer-head
key, condition, read, action, byte allowance, or IAM member. The production serializer and oracle check
action count, `wire_json_bytes`, and `conservative_service_item_bytes` for every formula at the same
simultaneous profile before any authority mutation. A request over any formula, 100 actions, 400000 per
pre-state/result item, or 4000000 summed distinct-target maxima is refused rather than split into a second
settlement or completion path. Complete transaction wire bytes are pinned only for request completeness. Separate constructor proofs pin the complete encoded ATTEMPT/FENCE conditions and
maximum FENCE/case items for reschedule, block, and requeue, including retained block/requeue audit
fields. Reschedule uses
one attempt condition plus one fence update; block adds exactly one protected-case put; requeue uses one
attempt condition plus exact FENCE and protected-case updates. Each fits transaction/item limits before
runtime and introduces only the key-family actions in the coordinator matrix.

`CompletePlanRequest` remains the only builder that changes an active plan `status planned→completed`
and puts its terminal lifecycle event; `PersistPlanRequest` owns the sole reserved-intent owner-swap
replacement transaction.
Authority publication transactions stay in their existing typed publication builder because they do
not touch `TABLE#`, `TABLE_ATTEMPT#`, `WRITE_PLAN#`, or `STATUS_FLOW#` coordinator families. Every
other transaction named in §§2–10 maps to one row above or one named write-plan request; there is no
raw transaction escape hatch in a store, Lambda, operator, fixture, or reconciler.

Every typed writer that reads or conditions a nonterminal pair first requires `core_valid`; a
core-invalid pair returns only `CORRUPT_PROVIDER_CORE_AUTHORITY` and issues no SDK mutation. Finalization
requires the exact unexpired lease holder/token, clear matching fence, allowed owner state,
and exact unit or run head predecessor; each pointer root is headless and has only its absent-event
creation condition. Parking requires the current holder/token and matching nonterminal fence.
Attempt completion requires provider terminality for that same attempt before clearing its fence and
advancing its owner ordinal. Release is impossible while a fence is active and can clear `parked_at`
only for the exact holder/token after fence clearance.
Cross-prefix, cross-sort-key, cross-field, wrong-state, wrong-owner, or wrong-fence requests fail locally
before any SDK invocation.

#### Shared DynamoDB byte oracle

`Stage1DynamoByteOracle` is the shared production serialization and feasibility seam, but it keeps three
quantities distinct:

1. `action_count` is the number of distinct-item `Put`, `Update`, `Delete`, and `ConditionCheck` actions.
2. `wire_json_bytes(value)` is exactly the length of the current canonical low-level JSON map after
   DynamoDB's ordinary low-level wire conversion (including standard base64 text for `B`/`BS`):
   `len(json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8"))`.
   For an item, `value` is the low-level map produced by applying the production
   `boto3.dynamodb.types.TypeSerializer` to every attribute value and then performing that wire
   conversion; for a request it is the complete converted low-level SDK request map. It pins builder
   serialization, request completeness, and calibration only. It is never called the AWS service item
   size and complete request wire bytes are never used as proof of the service's 4 MB item aggregate.
3. `conservative_service_item_bytes(item) = wire_json_bytes({name:
   TypeSerializer.serialize(value) for name, value in item.items()})`, with the same low-level wire
   conversion. This is the admitted upper bound for the item's service-counted bytes.

The third quantity is conservative for every admitted DynamoDB type under AWS's current
[item-size calculation](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/CapacityUnitCalculations.html)
and [item constraints](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/Constraints.html).
By type induction, JSON emits every top-level and nested map attribute name, and its ASCII escaping is
never shorter than the name's UTF-8 bytes; the same holds for string values. Base64 plus JSON syntax is
never shorter than the raw binary bytes AWS counts. A serialized number's decimal/sign/exponent text
plus its `N` wrapper is no shorter than AWS's estimate of one byte per two significant digits plus one.
`BOOL` and `NULL` wrappers exceed the documented one value byte. Empty and nonempty `L`/`M` wrappers
exceed the documented three-byte container overhead, and each encoded element contributes at least the
documented one-byte element overhead before the inductive child bound. `SS`, `NS`, and `BS` array/type
syntax adds bytes beyond the sum of their scalar values, for which the string, number, and binary bounds
already hold. Therefore `conservative_service_item_bytes(item)` is greater than or equal to the
service-counted item size for every admitted finite nesting of `S`, `N`, `B`, `BOOL`, `NULL`, `L`, `M`,
`SS`, `NS`, and `BS`. Unsupported tags, floating-point values, non-finite or otherwise indeterminate
numbers, empty sets, invalid nesting, and any value the production serializer or this proof cannot
classify are rejected before mutation.

AWS documents that `TransactWriteItems` groups at most 100 actions over distinct items and that the
aggregate size of the transaction's items cannot exceed 4 MB; it also names `Put`, `Update`, `Delete`,
and `ConditionCheck` as the action kinds
([transaction API](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/transaction-apis.html)).
For every distinct transaction target, the oracle requires an exact strong pre-state already carried by
the typed request; absence is the exact size zero. It reconstructs the exact resulting item, with absence
as zero after `Delete`, the put item after `Put`, the expression-applied item after `Update`, and the
unchanged pre-state after `ConditionCheck`. It computes:

```text
target_upper_bound = max(
    conservative_service_item_bytes(exact pre-state item) or 0,
    conservative_service_item_bytes(exact resulting item) or 0,
)
```

Every nonzero pre-state and result must be `<=400000`; the sum of `target_upper_bound` over all distinct
targets must be `<=4000000`; and `action_count` must be `<=100`. These application limits are
deliberately below the binary service limits of 409600 and 4194304 bytes. Taking the maximum covers
either pre-state or resulting-item interpretation for every touched item and includes condition-only and
deleted items rather than treating them as size-free. If an exact strong pre-state is unavailable, the
operation refuses before mutation. The builder also rejects two actions targeting one item.

For serialization completeness, the oracle separately constructs the exact low-level
`TransactWriteItems` request passed to the SDK: `TransactItems`, every `TableName`, key, operation
member, condition/update expression, expression-name map, serialized expression-value map,
`ReturnValuesOnConditionCheckFailure` when present, `ClientRequestToken`, and every other emitted SDK
request member. Only transport headers and credentials outside that request are excluded.
Nontransactional operations pin their complete emitted request by the same `wire_json_bytes` rule.
Preflight calls the request builder and this helper; every runtime builder calls the same helper on its
final request immediately before dispatch. Neither may reconstruct, trim, or independently estimate a
map.

Static committed v1 fixture files under
`tests/fixtures/stage1-dynamodb-byte-oracle/<typed-operation>/<branch>-v1.json` own exact typed inputs,
exact strong pre-states, independently reviewed expected low-level request maps, expected resulting maps,
wire totals, per-item conservative totals, aggregate total, action count, and accepted/refused outcome.
They cover maximum and one-beyond acceptance for every real operation branch, including
`SubmitPlanIntentRequest`, the attempt-33 `FailUnitRequest` expansion, every frozen exact ordinary and
planned-replacement `PersistPlanRequest` map under the respective 20- and 100-action caps, owner
reservation CAS, exact three-action intent reschedule/block/requeue branches, and
`FailAdmissionRequest`. They also cover `ClaimUnitRequest` queued claim and failed redrive, exact
replay/collision readback, every finalizer/failure/completion branch, the transactional parked-submission
recovery branch, and the sole direct submission operation. The attempt-33 fixtures separately pin the
replaced trigger and every maximum-size successor intent `META`/`UNIT#` put; the expansion adds exactly
one owner action absent from the ordinary branch and has no additive trigger action.
Fixtures are never regenerated during tests. A test-only reference checker imports and calls no
production oracle or builder; from the frozen maps it computes `wire_json_bytes` with only the standard
library and computes conservative totals directly by the documented recursive proof above. Production
builders and `Stage1DynamoByteOracle` must independently match those frozen maps and expectations.
Common-mode omission tests remove or alter every request member and required pre-state/result member in
turn and require a mismatch. Any fixture change requires an explicit reviewed fixture-version and design
update. Preflight and runtime may consume expected values; they never author them.

Two reproducible calibration fixtures pin only the wire serializer and remain wire fixtures, not service-
size or transaction-aggregate evidence. `item-v1` is the TypeSerializer output for
`{"partition_key":"BYTE_ORACLE_ITEM_V1","record_key":"R","payload":"x"*N}`—exactly
`{"partition_key":{"S":"BYTE_ORACLE_ITEM_V1"},"record_key":{"S":"R"},"payload":{"S":"x"*N}}`.
It has `wire_json_bytes=400000` at `N=399913` and `400001` at `N=399914`. `transaction-v1` is exactly:

```text
ClientRequestToken = "byte-oracle-transaction-v1"
TransactItems = 11 Put members, in ordinal order 00..10
for every Put: TableName = "T" repeated 193 times
for ordinals 00..09:
  Item = TypeSerializer({partition_key: "TX#<two-digit ordinal>", record_key: "R",
                         payload: "x" repeated 363000 times})
for ordinal 10:
  Item = TypeSerializer({partition_key: "TX#10#123456789", record_key: "R",
                         payload: "x" repeated N times})
no other request or Put member is present
```

Ten 363000-byte payloads plus final `N=366632` total 3996632 payload bytes and produce
`wire_json_bytes=4000000`; final `N=366633` totals 3996633 payload bytes and produces
`wire_json_bytes=4000001`. The key labels, 193-character valid table name, member order, and absence of
optional members are part of the fixture. Startup and acceptance reproduce these four wire totals with
the production TypeSerializer only as serializer fixtures; they make no AWS item-size or 4 MB aggregate
claim.

Coordinator grants are attached directly to each runtime execution role and narrowed again by the
request types that runtime can call:

| Principal | Allowed typed operations |
|---|---|
| phase-1 delivery Lambda role, including the transitional pre-switch composition | `SubmitPlanIntentRequest` only among intent/admission operations; transitional inline composition also receives `PersistInlineRunRequest`, inline `RecordStage1RefusalRequest`, and the table-fence/attempt/finalize operations listed below. It has no semantic-write queue send, `AdmitUnitRequest`, `FailAdmissionRequest`, `stage1-plan-intent-ready` GSI query, claim/reschedule, or `PersistPlanRequest` authority. |
| table-writer Lambda role | `ClaimUnitRequest`, `FailUnitRequest`, and `CompletePlanRequest`; unit `RecordStage1RefusalRequest`; table-fence/attempt/finalize operations listed below; only fail/finalize disposition builders may expand usage settlement; attempt 33 may create the deterministic successor intent inside `FailUnitRequest`, but this role cannot query or claim pending intents |
| scheduled `PlanIntentReconciler` Lambda role | exact `stage1-plan-intent-ready` GSI discovery and strong reads; `ClaimPlanIntentRequest`, `ReschedulePlanIntentRequest`, `PersistPlanRequest`, `AdmitUnitRequest`, and `FailAdmissionRequest`; exact queue send; no Athena/provider, S3/manifest/publication/CURRENT, inline, lease/FENCE, direct attempt mutation, unit claim/final disposition, or settlement authority |
| scheduled `ProviderAttemptReconciler` Lambda role | transactional `RecoverParkedAttemptSubmissionRequest` only for an exact parked pair; `RequestAttemptCancellationRequest`, `ParkAttemptRequest`, `RescheduleTableAttemptReconcileRequest`, `BlockTableAttemptReconcileRequest`, reconciler-mode `CompleteTableAttemptRequest`, and `ReleaseTableFenceRequest` after exact fence clearance; no direct mutation, blocked-fence requeue, live `RecordSubmittedAttemptRequest`, refusal, final owner-disposition, or settlement authority |
| `ProviderFenceRecoveryFunction` Lambda role | private `RequeueBlockedTableAttemptReconcileRequest` only after exact server-side case/FENCE/ATTEMPT resolution; no provider call, completion/clear, pending discovery, owner, finalization, or settlement authority |
| `ProviderFenceRecoveryOperatorRole` | `lambda:InvokeFunction` on only the exact unqualified recovery function ARN; no DynamoDB or semantic/provider service action |
| `SemanticProjectionOperatorRole` | only the inline operator row below; no provider-reconcile block discovery/requeue, direct blocked case/FENCE/ATTEMPT recovery, or provider-terminal/FENCE-clear action |
| `Stage1LifecycleProducer` Lambda role | no coordinator request; exact lifecycle/settled-Athena-usage outbox read plus status-owned delivery-mark and poison-block operations only, never requeue, owned by the status LLD |

No shared intermediary role, developer profile, or runtime `AssumeRole` path supplies these grants. Each
role's coordinator statements receive only the exact ARN, leading-key, underlying-action, and
enclosing-operation combinations needed by its row; omitting an operation removes its unique
combinations. Finalizer policy generation accounts for exactly two transaction-enclosed `STATUS_FLOW#*`
members—the owner terminal event `PutItem` and the queued unit head or inline run head
`PutItem|UpdateItem` already required by that principal—
and no pointer-head member, condition, key family, or additional permission. Because IAM cannot express
the flow-kind suffix, the closed finalizer builder enforces that sole legal owner head and rejects every
pointer-head request before SDK invocation. `PlanIntentReconciler` alone receives `dynamodb:Query` on
`<publications-table-arn>/index/stage1-plan-intent-ready` with `dynamodb:LeadingKeys=ready`; only its
claim, persistence, and exact-reservation reschedule builders receive the corresponding intent and
planned-plan replacement grants. The phase-1 delivery role receives only the exact owner-consumed
intent-submission operation. The reconciler alone receives exact queue `SendMessage`,
`AdmitUnitRequest`, and `FailAdmissionRequest`: every semantic-write queue send and every admission or
admission-failure transition, first-attempt or recovered, is one reconciler path. No other principal can
query the index, and the phase-1 delivery caller cannot send a unit message, admit a unit, fail an
admission, claim, reschedule, or persist a pending intent. The table writer may create the deterministic
`plan_attempt_budget` successor only inside its attempt-33 `FailUnitRequest`; that transaction consumes
the owner and replaces its ordinary failure trigger with intent `META` and `UNIT#` puts. The
`ProviderAttemptReconciler` separately receives `dynamodb:Query` on exactly
`<publications-table-arn>/index/table-attempt-reconcile` and queries only the `pending` partition for
due-fence discovery; it has no blocked-partition query, table `Scan`, cursor,
`ExclusiveStartKey` persistence, or other index grant. `ProviderFenceRecoveryFunction` separately
receives `dynamodb:Query` on that same exact GSI ARN with `dynamodb:LeadingKeys=blocked`; no other
principal may query the blocked partition. It resolves returned base keys to protected cases server-side
and never returns a base key or selector. The discovery grants are not interchangeable and are not part
of the coordinator action vocabulary. `SemanticProjectionOperatorRole` receives neither GSI query nor
protected case/FENCE/ATTEMPT recovery access. Stage-1 outbox recovery is
independently owned by the status LLD: its producer receives `dynamodb:Query` on only the exact
`stage1-outbox-pending` GSI ARN and has no publications-table `Scan` or control-cursor permission.

`IngestionStack` deploys one `ProviderFenceRecoveryFunction` as the sole provider-block discovery and
requeue broker. Its invocation and result wire schemas are versioned and closed, symmetric to the
status-side recovery pair:

```text
ProviderFenceRecoveryInvocation.v1 =
  {"schema_version": "1", "mode": "list", "limit": integer 1..100, "cursor"?: opaque bounded token}
| {"schema_version": "1", "mode": "requeue", "recovery_ref": canonical lowercase UUIDv4,
   "operation_id": bounded nonblank operation id,
   "resolution_reason": nonblank sanitized reason}

ProviderFenceRecoveryResult.v1 =
  {"schema_version": "1", "mode": "list",
   "cases": [{"recovery_ref", "safe_reason", "blocked_at"} ...], "next_cursor"?: opaque token}
| {"schema_version": "1", "mode": "requeue", "recovery_ref",
   "outcome": "requeued" | "already_requeued"}
```

Extra, missing, retyped, blank, or unknown-mode fields reject before any client exists. Failures return
only one closed error code — `invalid_invocation | case_not_found | stale_case | conflict |
dependency_failure` — with no selector, key, or raw provider content; no other error vocabulary exists.

List queries only `reconcile_status=blocked` on the existing `table-attempt-reconcile` GSI with the
requested limit and resolves every returned base FENCE to its exact protected case before emitting it.
Its result contains only `{recovery_ref, safe_reason, blocked_at}` entries and an optional opaque cursor.
The cursor is exactly the last returned canonical recovery UUID, treated only as an opaque lexical lower
bound: the next call validates its closed UUID form and queries
`reconcile_after_or_block > BLOCK#<cursor>` with the same blocked partition and limit. It does not use or
encode `ExclusiveStartKey`, a base partition key, record key, table ID, attempt ID, block ID, holder,
fencing token, binding digest, or provider/owner selector; it needs no key, cursor store, mutation, or
additional permission. Concurrent insert/requeue can change a later page, so clients continue until no
cursor and may relist from the start; case/FENCE equality—not cursor possession—is recovery authority.
Missing, malformed, stale, or mismatched FENCE/case rows fail closed and are not sanitized into an
apparently recoverable entry.

Requeue strongly reads the canonical protected case and its exact canonical blocked FENCE/ATTEMPT
pair, then passes that block output to `RequeueBlockedTableAttemptReconcileRequest`. The transaction
returns canonical pending metadata and marks the case `requeued`; it performs no correction or repair.
Exact replay returns `already_requeued`; competing, stale, terminal, consumed, or changed operations
reject. A later block allocates a new case. Safety remains `active|parked`; provider-terminal completion
alone clears it.

The function role receives only `dynamodb:Query` on the exact blocked partition of the exact GSI ARN;
direct strongly consistent `dynamodb:GetItem` on the exact retained publications-table ARN with
`PROVIDER_RECONCILE_BLOCK#*`, `TABLE_ATTEMPT#*`, and `WRITE_PLAN#*` leading keys (the last only for the
queued owner snapshot the requeue transaction condition-checks); and transaction-enclosed case/FENCE/
ATTEMPT/owner `ConditionCheckItem|UpdateItem` actions required by the private requeue row. It has no pending-
partition query, `Scan`, direct mutation, `PutItem`, `DeleteItem`, batch access, semantic object/plan/
run/pointer/usage/lifecycle authority, Athena, Glue, S3, provider call, completion, FENCE-clear, role-
assumption, or wildcard authority. The scheduled reconciler retains pending-only query and receives the
transaction-enclosed `PROVIDER_RECONCILE_BLOCK#*` `PutItem` needed by the block row, but no case read/
update or blocked requeue. `ProviderFenceRecoveryOperatorRole` has same-account trust and only
`lambda:InvokeFunction` on the exact unqualified function ARN; it has no DynamoDB or other service
action.

Config owns and publishes exactly these typed paths; this design names their contracts without creating
another path owner:

- `providerFenceRecoveryOperatorRoleArn` at
  `/adlytics/{environment}/ingestion/provider-fence-recovery-operator-role-arn`;
- `providerFenceRecoveryFunctionArn` at
  `/adlytics/{environment}/ingestion/provider-fence-recovery-function-arn`.

The authorized operator entry point is exactly the module `adapters.jobs.requeue_provider_fence`,
named for parity with the status-side `adapters.jobs.requeue_stage1_outbox`; no other module, alias, or
script invokes the broker. It has only `list` and `requeue` command modes. Both require explicit
`--profile <non-default-profile> --region <region> --expected-account <12-digits> --env <environment>`;
list additionally requires `--limit 1..100` and accepts only `--cursor <opaque-token>`, while requeue
requires `--operation-id`, `--recovery-ref`, and `--resolution-reason`. Validation completes before any
client exists. The ambient session creates only STS, SSM, and CloudFormation clients: it calls caller
identity, reads exactly the two paths above plus the typed DataV2 publications-table parameter, and
describes exactly `Adlytics-DataV2-<environment>` and `Adlytics-Ingestion-<environment>`. It verifies
account/partition/Region, nonblank unique parameter ownership, exact table/GSI ownership by DataV2, and
exact unqualified role/function ownership by Ingestion; then it assumes only the verified invoke-only
role for exactly 900 seconds with a bounded operation-derived session name.

The assumed session creates only Lambda and synchronously invokes only the verified unqualified
function with the selected closed variant. Ambient invoke, direct table clients, second-role assumption,
default or implicit identity values, resource enumeration/guessing, renamed/duplicate parameters,
qualified/foreign ARNs, selector-bearing cursor/result/error, and any fallback all fail closed. Logs and
CLI output contain only operation ID, opaque recovery ref/cursor, safe reason/time, and closed outcome or
error code. Deployment orders the broker, typed parameters, role, and protected-case-capable block builder before
enabling the scheduled reconciler. Enablement uses the exhaustive pre-switch all-canonical-namespace
and
stray-attribute verification in §2.5; any partial read or non-canonical/malformed result blocks
enablement,
and no adoption path exists.

Local `ingest_delivery` and `reprocess_semantic_v2` operators, plus credentialed integration fixtures,
use the dedicated `SemanticProjectionOperatorRole`. `IngestionStack` gives that role exactly the
inline-operator row: `PersistInlineRunRequest`, inline `RecordStage1RefusalRequest`,
`AcquireTableFenceRequest`, `ReleaseTableFenceRequest`, `BeginTableAttemptRequest`, live
`RecordSubmittedAttemptRequest`, inline `RequestAttemptCancellationRequest`, `ParkAttemptRequest`,
`CompleteTableAttemptRequest`, and `FinalizeInlineRequest` with its private settlement sub-builder. It
receives no provider-reconcile GSI query, protected-case read, blocked FENCE/ATTEMPT recovery,
`RequeueBlockedTableAttemptReconcileRequest`, queued-plan mutation, parked-submission recovery, poison-
block creation, authority repair, or scheduled-reconciliation discovery permission. Its trust policy has exactly the deployment account principal
`arn:${AWS::Partition}:iam::<account-id>:root`, requires `aws:PrincipalAccount=<account-id>`, and has no
service or cross-account principal. An admitted operator identity separately needs `sts:AssumeRole` on
that exact role ARN; trust alone grants no caller permission.

At the pre-switch amendment, `ingest_delivery` is a one-manifest operator only. Every invocation must
supply both `--key <retained-manifest-key>` and `--version-id <retained-version-id>`; argument parsing
rejects neither, either alone, or `--predecessor-execution-id` without both before bootstrap or any AWS
client construction. The operator has no no-key discovery mode and never calls `ListObjectsV2` or
`HeadObject`; its raw-bucket role grant remains only `s3:GetObjectVersion` on the three declared source
roots, with no `s3:ListBucket`, unversioned `s3:GetObject`, or discovery grant. This is a scope reduction,
not a dual-path migration: the no-key path is removed in the same pre-switch implementation slice.
Existing ingestion Lambda landing and fixture landing do not invoke this operator and are unchanged.
A bulk backfill is permitted only from an externally reviewed, exact immutable manifest of `(key,
version_id)` pairs; automation invokes this explicit one-manifest mode once per pair and may not discover,
select, or replace entries from S3.

`IngestionStack` publishes the role ARN through typed `PARAMS.semanticProjectionOperatorRoleArn` at
`/adlytics/<environment>/ingestion/semantic-projection-operator-role-arn`. `PARAMS` and `paramPath()`
remain the sole path owner. Each CLI and credentialed integration fixture requires an explicit
non-default profile, environment, Region, and 12-digit expected account and constructs two sessions
with a hard client boundary:

| Session | Exact clients and calls | Forbidden |
|---|---|---|
| ambient bootstrap session | STS `GetCallerIdentity`; SSM `GetParameter` for every typed DataV2 resource parameter and `semanticProjectionOperatorRoleArn`; CloudFormation `DescribeStackResources` for exactly `Adlytics-DataV2-<environment>` and `Adlytics-Ingestion-<environment>`; STS `AssumeRole` on the verified role ARN | S3, Athena, Glue, DynamoDB, coordinator composition, lease/attempt/provider submission, and every semantic read or mutation |
| assumed semantic session | exact-version raw S3 reads; semantic immutable/projection S3; BAKE-workgroup Athena; Glue catalog/table mutation required by Iceberg; bake-result S3; attempts, leases, publications, lifecycle heads/outboxes, and coordinator calls | SSM, CloudFormation, developer-profile fallback, a second role, unversioned raw-object read, or resources outside the verified deployment |

Before assuming the role, the ambient session requires caller account equality, resolves the typed
parameters `semanticV2RawBucketName`, `semanticV2BucketName`,
`semanticV2AthenaResultsBucketName`, `semanticV2AthenaWorkgroupBake`,
`semanticV2SourceDatabase`, `semanticV2CandidateDatabase`, `semanticV2CurrentDatabase`,
`semanticV2AttemptsTableName`, `semanticV2LeasesTableName`,
`semanticV2PublicationsTableName`, and `semanticProjectionOperatorRoleArn`, and rejects blank or
unexpected values. It verifies that the DataV2 stack owns every data parameter and resolved S3 bucket,
Athena workgroup, Glue database, and DynamoDB table; verifies that the Ingestion stack owns the role-ARN
parameter and the `AWS::IAM::Role` whose physical role name is the ARN resource; and requires the ARN's
partition and account to match the explicit Region/account. Resolution never enumerates account
resources and never derives a name as a fallback.

The operator role does not inherit `IngestionStack.grantRawRead` wholesale. Its operator-specific raw
statement is exactly `s3:GetObjectVersion` on the three declared source roots; there is no
`s3:ListBucket`, unversioned `s3:GetObject`, or other raw discovery/read action. It otherwise reuses,
without broadening, the exact service/resource shapes already owned by
`IngestionStack.grantSemanticV2`: semantic immutable-object get/put prefixes; Iceberg projection
object/multipart and prefix-bounded list access; bake-result object/multipart access, a read of
pre-cutover batch results, and prefix-bounded list access over both; start/get/results/stop on the
BAKE workgroup and get/results/stop on the retained batch workgroup
([Athena workgroups](../query-path/architecture.md#athena-workgroups-and-the-batch-start-budget));
the catalog plus the three exact
database/table families' existing Glue read/update actions; and exact attempts, leases, publications,
index, lifecycle, and coordinator DynamoDB actions further narrowed by this section's
prefix/action/enclosing-operation matrix. There is no `Resource: "*"`, wildcard table/bucket/workgroup/
database ARN, unconditioned semantic bucket listing, managed policy, or action family broader than
those grant shapes. Bootstrap permissions are separate and contain only the exact typed SSM parameter
ARNs, the two named stack ARNs, caller identity, and the one role ARN.

Tracing bootstrap may use the explicit ambient session before any client is made; no semantic operation
may. After successful ownership verification and `AssumeRole`, the caller creates one temporary-
credential `boto3.Session` and constructs **every** raw-reader, semantic S3, Athena, Glue, DynamoDB, and
coordinator client from it. The exact raw manifest and object reads always pass their retained
`VersionId`. Missing or bad parameters, ownership mismatch, account/partition mismatch, trust denial,
assume failure, expired temporary credentials, or any accidental ambient semantic-client construction
fails before semantic access or mutation.

## 4. Write queue and admission contract

| Property | Value | Why |
|---|---|---|
| Queue | `adlytics-<env>-semantic-write.fifo` with a FIFO DLQ | FIFO schedules per-table ownership. |
| `MessageGroupId` | physical `unit_key` | Every mutation of one physical unit is serialized; unrelated units proceed independently. |
| `MessageDeduplicationId` | `canonical_authority_digest(["stage1-write-unit-message/v1", plan_key, unit_key])` | `plan_key` already binds `intent_id` and assigned generation, so exact resend collapses while G+1 cannot collapse into G. |
| Body | `canonical_authority_bytes({"schema_version":"1","plan_key":plan_key,"plan_digest":plan_digest,"origin_intent_id":intent_id,"generation":claimed_generation,"unit_key":unit_key})` | Exactly these six internal owner fields; no public unit execution, causation, operation, predecessor, flow, or queue-receive field exists before writer claim. |
| Batch size | 1 | Failure and ordering units are identical. |
| Visibility timeout | derived `960` seconds from `tableWriterTimeoutSeconds + semanticWriteVisibilityMarginSeconds` | Greater than the writer timeout and within the SQS ceiling. |
| `maxReceiveCount` | `3`, from `semanticWriteMaxReceiveCount` | Exhaustion moves only that unit message to the DLQ. |
| Reserved/mapping maximum concurrency | `8`, from `tableWriterConcurrency` | Shared across unrelated table groups; not a correctness remedy. |
| Encryption | SQS-managed (`SSE-SQS`) on both queue and DLQ | Bodies carry only internal owner identifiers; managed encryption without key custody. |
| Message retention | `1209600` seconds (14 days) on both queue and DLQ | Exceeds every reconcile/drain window; retained intents keep units recoverable regardless of queue retention. |
| DLQ alarm | `ApproximateNumberOfMessagesVisible >= 1`, statistic `Maximum`, one 60-second evaluation period, missing data `notBreaching` | An exhausted unit message is an operator redrive signal, never silent. |

The object body is canonical-authority JSON, so key order in the displayed source object is not wire
order; exactly the six named fields survive encoding. Phase-1 feasibility preflight (§2.1) and
`PlanIntentReconciler` call the same pure constructor; the reconciler alone passes its exact body bytes,
physical `unit_key` group, and deduplication digest to SQS and `AdmitUnitRequest`, while preflight only
encodes them. There is no shorthand or adapter-local formula. The committed vector uses fictional
synthetic member values to pin encoding and digest behavior only; member-form validation (the 64-hex
`plan_digest` and plan-key segment forms) applies to production inputs, never to these synthetic
members:

```text
body JSON = {"generation":7,"origin_intent_id":"8595e699935a8ec7ee6d3d87d3e0214300e09831da6a4808f10c9fab4174269b","plan_digest":"2222222222222222222222222222222222222222222222222222222222222222","plan_key":"WRITE_PLAN#2222#exec-g7","schema_version":"1","unit_key":"db.campaigns"}
body UTF-8 length = 267
body digest = a6555c645b6a896c38c098f4197a47747b291ebb7064362a1d426fcc632e0454
dedup tuple JSON = ["stage1-write-unit-message/v1","WRITE_PLAN#2222#exec-g7","db.campaigns"]
dedup tuple UTF-8 length = 73
MessageDeduplicationId = 7708aa64f547dd52815c30960bd182fb1896bbf2849ca5ad3a24c8a76b513ea6
MessageGroupId = db.campaigns
```

Send success is not plan admission until the unit admission transaction succeeds. Re-sending the exact
body is safe under the exact deduplication ID. The writer receive identity has exactly one constructor:
`writer_execution_id = canonical_authority_digest(["stage1-writer-execution/v1", sqs_message_id,
receive_count])`, where `sqs_message_id` is the exact received `MessageId` and `receive_count` is the
exact `ApproximateReceiveCount` as a JSON integer. It renders as 64 lowercase hex characters within the
128-byte identity cap, is byte-stable for one exact receive, differs across receives, and is the sole
value fed to the unit lifecycle head's execution selector, the queued lease holder, and every
`writer_execution_id` condition; no prefix, truncation, or adapter-local variant exists. After receive,
before claim or acknowledgement, the
writer strongly reads `SUCCESSOR_OWNER`, plan `META`, unit, origin intent, and plan `HEAD`; it requires
the body to reproduce the active `plan_key`, `plan_digest`, `origin_intent_id`, assigned `generation`,
and `unit_key` byte-for-byte. A stale generation, replaced plan, different canonical body, unknown or
extra field, or any owner/head/unit disagreement executes no unit and follows the exact stale/retry
classification. The writer never infers admission from message presence; it checks durable counters and
unit admission status. Ambiguous send/admission responses and death before or after either boundary
return through the same strong-read classifier and resend only this constructor.

The writer receives Athena/Glue/S3 permissions, immutable-object read, transaction access to the exact
plan/`CURRENT`/lease items, and SQS receive/delete plus `sqs:ChangeMessageVisibility` on only the write
queue ARN. The delivery function receives only plan-intent submission permission; `PlanIntentReconciler`
alone receives queue send and admission permissions. Neither
role receives broader key-space ownership from the coordinator exception.

## 5. Table lease and durable provider-attempt fence

Table leases live in the existing `leases` table under `TABLE#<physical table id>`.
`DynamoProjectionCoordinator` owns every acquire, release, park, and reconciler lease mutation through
the §3.2 request types; no other adapter writes a `TABLE#` key, and no standalone lease write path
exists. A queued holder
is `sha256(UTF8(canonical_json([plan_key, unit_key, writer_execution_id])))`; an inline holder is
`sha256(UTF8(canonical_json(["inline", writer_kind, operation_id, execution_id,
physical_table_id])))`. Acquisition increments the fencing token under expiry/holder conditions and,
through the coordinator's same transaction, condition-checks that the table-attempt `FENCE` names no
nonterminal attempt. Release requires the exact holder/token and a clear `FENCE`.

Queued claims and all inline operators strongly read and classify table authority before acquisition.
Refusal precedence is closed: core-invalid authority is the hard failure; core-valid `active|parked`
FENCE is `TABLE_ATTEMPT_PENDING` regardless of lease state; a valid blocked reconciliation form is also
`TABLE_ATTEMPT_PENDING`; only absent/clear FENCE plus another holder's unexpired lease is `TABLE_BUSY`.
A conditional race strongly rereads and reclassifies. The acquisition condition closes the read/acquire
race. An expired or parked lease alone never permits work while `FENCE` remains active. No merge,
verification, or other Athena writer statement may start until the provider terminal for the fenced
attempt has been observed and the terminal transaction has cleared that exact fence. FIFO schedules
queued order; the lease and indefinitely active durable attempt fence provide correctness across queue
and operator paths.

Every Athena statement, including verification, follows this protocol:

1. Immediately before Begin, a Lambda captures monotonic `m0` and remaining budget `r0` in integer
   milliseconds. It sets invocation deadline `I=m0+r0`, safety deadline `S=I-60000`, and cancellation
   deadline `Q=S-60000`. It requires `r0>120000`; `r0<=120000` starts no attempt and returns the typed
   retryable insufficient-budget result. A full 900000-ms budget gives offsets
   `Q/S/I=780000/840000/900000` from `m0`; 120001 ms accepts with `Q=m0+1`, while 120000 rejects.
   Process deadlines and every comparison/sleep use monotonic time only.
2. Before Begin, run the closed local typed validator over the statement kind, exact owner/lease tuple,
   canonical start envelope, token, parameter types/count, rendered SQL bytes, and all §11 bounds. Any
   validation failure starts no attempt, reserves nothing, and returns the typed local refusal. Then
   derive `attempt_id` through the one byte-exact constructor over the immutable owner/table/lease/
   ordinal inputs and use it as the Athena client request token:

   ```text
   attempt_identity_tuple = ["stage1-table-attempt/v1",
     owner_kind, owner_ref, physical_table_id, lease_holder, fencing_token, ordinal]
   attempt_id = canonical_authority_digest(attempt_identity_tuple)
   client_request_token = attempt_id
   envelope_digest = sha256 of the stored canonical start envelope's exact UTF-8 bytes
   reservation_identity = canonical_authority_digest(["stage1-attempt-reservation/v1", attempt_id])
   ```

   `owner_kind` is exactly `queued_unit | inline_run`; `owner_ref` is the exact active `plan_key` for a
   queued unit or the exact `run_id` for an inline run; `fencing_token` and `ordinal` are JSON
   integers; all three digests render as 64 lowercase hex characters, the first two satisfying the
   Athena client-request-token form; and §3.1 clause 4's recomputation reproduces exactly these bytes.
   `reservation_identity` is the permanent-slot identity Begin writes byte-identically to the
   attempt's `reservation_identity` and the owner's `reserved_slot_identity`, so exact Begin replay
   reproduces the identical slot bytes. No other derivation, truncation, or prefix form exists. `BeginTableAttemptRequest` atomically persists the envelope, FENCE,
   `current_attempt_id`, and permanent reservation before any SDK call. Queued Begin increments UNIT and
   META under `<10` and `<32`; inline Begin increments RUN under `<10`. Exact Begin replay increments
   nothing. Unit reservation 11 follows the existing unit failure path. Queued reservation 33 performs
   no Athena call and uses `FailUnitRequest` to settle the source unit's mixed accepted-attempt roster,
   terminalize the source plan, write its failure event/head, and create the one deterministic origin
   `plan_attempt_budget` successor intent partition. Successor intent `META` plus all `UNIT#` puts
   replace the ordinary failure trigger rather than append to it; the transaction consumes the owner.
   The trigger and maximum-size successor items have independent byte/action fixtures. The failed unit
   message acknowledges only after transaction commit or exact replay proves both the failure transition
   and successor intent; otherwise it remains retryable, while the pending intent is independently
   recoverable after the unit reaches its DLQ. Inline reservation 11 uses the existing unavailable finalization path without a
   pointer or queued intent.
3. The dedicated Athena start client calls the SDK with the persisted envelope and `MaxAttempts=1`
   exactly once per invocation. Its complete public result is exactly:

   ```text
   StartTableAttemptResult =
     Accepted(query_execution_id: valid_query_execution_id, sanitized_evidence)
     | AcceptanceAmbiguous(reason, sanitized_evidence)
   ```

   Here `valid_query_execution_id` means exactly a String containing at least one non-whitespace
   character. Such an identity returns `Accepted`. Every exception after Begin—including `ClientError`,
   transport, timeout, and unknown exception classes—plus every malformed SDK result, missing field,
   wrong type, or blank query execution identity returns `AcceptanceAmbiguous`; this contract makes no
   SDK/service-error classification claim. `sanitized_evidence` is one closed two-field form —
   `{provider_status_code: integer or null, provider_error_class: throttling | service_unavailable |
   network_timeout | response_malformed | unknown | none}` — with no free text, message, request ID, or
   provider payload; `Accepted` always carries `provider_error_class=none`, every unmapped exception or
   result class is exactly `unknown`, and the ambiguous `reason` is exactly
   the selected `provider_error_class` value. The attempt's durable sanitized start evidence is written
   only together with the valid query identity — by `RecordSubmittedAttemptRequest` on the live
   `Accepted` path or by `RecoverParkedAttemptSubmissionRequest` on parked recovery — and is
   byte-immutable thereafter; an `AcceptanceAmbiguous` outcome stores nothing on the attempt, and its
   evidence is emitted only through the shared reporting seam. The field is therefore forbidden in
   `intent` and in the absent-query parked pair and required with the valid-query pair, exactly as the
   §3.1 core forms state. The dedicated Stage-1 Athena client permits exactly one SDK
   internal attempt and one `StartQueryExecution` call per invocation.
   `RecordSubmittedAttemptRequest` may change `intent→submitted` only for `Accepted` and requires the
   immutable envelope/token plus the exact valid identity; it writes the valid query identity,
   `submitted_at`, and the closed `sanitized_start_evidence` form together, write-once. Both outcomes retain the reservation, FENCE,
   and owner `current_attempt_id`; `AcceptanceAmbiguous` also retains the attempt in `intent`. Only while
   that attempt remains `intent` and time is before `S` may live exact replay call the same one-attempt
   client with the identical stored envelope/token; a later invocation may replay under the same rule.
   Once parked, only the provider-attempt reconciler may replay that exact envelope and attach an
   identical recovered valid identity. `PlanIntentReconciler` never starts provider work. Only a durable
   valid query execution identity followed by provider-terminal observation may progress.
4. Poll with terminal-first precedence on every loop. First process an already returned provider
   terminal. Otherwise, if no valid query execution identity is durable, retain the intent, permanent
   reservation, FENCE, and `current_attempt_id`: at `now>=Q` issue no cancellation, no
   `StopQueryExecution`, and no provider poll, and before `S` continue only the already permitted exact
   stored-envelope/client-token Start replay subject to the one-Start-call-per-invocation limit; at
   `now>=S`, park without cancellation, Stop, provider poll, or another Start. If a valid identity is
   durable, then after terminal-first processing: at `now>=S`, park without a new poll; otherwise at
   `now>=Q` with cancellation not yet durable, persist it and then call `StopQueryExecution` exactly once
   for that identity; if cancellation is already durable, issue no second Stop and continue bounded
   polling or sleeping; otherwise poll or sleep, bounded to the next `Q` or `S`. Cancellation persistence
   and Stop are forbidden without the durable valid identity. Cancellation never clears FENCE, and Stop
   success is not terminality.
5. On a provider terminal, call `CompleteTableAttemptRequest`. Its transaction records exact terminal
   facts and known/missing Athena quantities, advances the bound owner reference/ordinal once, retains
   accepted-attempt reservation usage, and clears only the fence naming that attempt. It chooses no
   usage category and creates no `USAGE#`; final owner disposition performs settlement atomically. The
   owner may release the lease only after completion clears FENCE. If terminality remains unknown at
   `S`, call `ParkAttemptRequest` and return its typed retryable result; do not attest, fail, clear the
   fence, release the lease, or start another statement for that table.

Durable schedule timestamps capture UTC once as integer epoch microseconds
`floor(unix_epoch_nanoseconds/1000)`, use integer addition only, and render exactly 20 zero-padded digits;
there is no jitter or floating-point arithmetic. A candidate is due at `now>=deadline`, queried with
`reconcile_after_or_block <= <20-digit now>#~`. Begin sets count zero and due exactly to
`lease.expires_at` converted to epoch microseconds, avoiding a race with a healthy lease owner. A real
active→park transition or reschedule from old count `c` captures one UTC operation instant, sets
`new_count=c+1`, and computes delay `60` seconds for `c=0`, `120` for `c=1`, `240` for `c=2`, `480` for
`c=3`, and `900` for every `c>=4`; due is operation instant plus that delay. Exact parked replay retains
the original instant/count/due. An ambiguous write strongly rereads and retries the same captured
instant and bytes. Requeue sets count zero and due exactly to `requeue_at`, so it is immediately
eligible. Overflow is rejected before mutation.

`DataV2Stack` owns discovery through `table-attempt-reconcile`; the concrete runtime is one
`ProviderAttemptReconciler` Lambda in `IngestionStack`. It uses `AppConfig.pythonRuntime`,
`AppConfig.lambdaArchitecture`, the typed dependencies-layer ARN, and the repository log-retention
setting; its
artifact is the existing built Lambda asset, never synth-built. EventBridge invokes it every minute
while enabled under §2.5's `stage1RecoveryEnabled` contract. It
has
memory `512 MiB`, timeout `120 seconds`, reserved concurrency `1`, and required typed
`tableAttemptReconcileBatchSize=10`. Each run issues one eventually consistent ascending `Query` with
`reconcile_status="pending"`, sort key
`reconcile_after_or_block <= <20-digit now epoch-micros>#~`, and `Limit=10`, starting at the pending
partition's first key. It never queries `blocked`, supplies/stores/follows
`ExclusiveStartKey`/`LastEvaluatedKey`, or owns a cursor. For every returned base key it performs a
strongly consistent FENCE read, then a strong exact ATTEMPT read. An already-clear fence is a no-op. It
validates projected key syntax, resolves the base pair, and applies the closed core/reconciliation
decision before any provider or coordinator call; unresolved or malformed core follows the hard-failure
branch, while only repairable reconciliation metadata enters typed block.

The reconciler makes exactly one provider observation per due candidate and never continuously polls one
candidate. For a core-valid active FENCE with a query id, it observes once: terminal completes; a
nonterminal observation before lease expiry reschedules and never replays Start; at or after lease
expiry it optionally persists due cancellation and calls Stop, then parks. Active without a query id
parks without calling Start. For a core-valid parked FENCE missing a query id, it replays only the
immutable envelope/client token, calls transactional `RecoverParkedAttemptSubmissionRequest` with the
identical recovered id, write-once submission instant, and write-once closed evidence form, and
proceeds only after its complete parked
FENCE `ConditionCheck` plus ATTEMPT `Update` commit. A concurrent FENCE-only reschedule, block, or
requeue aborts recovery and forces strong reread before any provider observation. A parked
terminal observation completes and releases the matching parked lease. A parked nonterminal computes
`cancel_due=submitted_at+780 seconds`; at or after that due instant it persists cancellation if needed
and calls Stop once, then reschedules, while before due it only reschedules. Any candidate-local
provider/dependency failure reschedules. Cancellation never clears safety.

Only after `SUCCEEDED`, `FAILED`, or `CANCELLED` does `CompleteTableAttemptRequest` record terminal
facts/quantities, retain accepted-attempt budget, advance the owning queued-unit reference or inline run
to resumable `reconciled`, and clear that exact FENCE and sparse-index attributes; the reconciler then
releases the matching parked lease. It creates no usage authority because provider terminality alone
does not establish owner disposition or category. It never declares a current inline run terminal or
omits pointer finalization: exact run replay must acquire a new lease and execute `finalize_inline`.
There is no elapsed-time or lease-expiry escape hatch.

A reschedule is valid only for a `core_valid` pair with canonical pending metadata and uses the exact
captured-instant/count/due rule above. For a `core_valid` pair with
`repairable_reconciliation_metadata`, the reconciler constructs
`MalformedReconciliationMetadataObservation` from the exact presence/type/value snapshot and invokes
`BlockTableAttemptReconcileRequest`; it does not call Athena or require a valid old pending tuple. If a
reschedule rejects and a strong reread is byte-identical, it constructs
`SameSnapshotRescheduleRejectedObservation`; if changed, it re-evaluates. The typed block transaction
canonicalizes only reconciliation metadata and creates the protected case. A transient or ambiguous
block call is strongly reread and retried with the same typed request until blocked, clear, or changed;
then the batch continues.

A `core_invalid` candidate takes the hard-failure branch: clear is not attempted, the reconciler emits
the sanitized alarm/error `CORRUPT_PROVIDER_CORE_AUTHORITY`, fails the invocation unhandled, and makes
zero Athena, coordinator, block, case, requeue, clear, lease, or owner mutation. It promises no ordinary
recovery or liveness for that table. External corruption recovery is outside this contract and may exist
only as separately reviewed retained-table disaster recovery. The runtime decision is otherwise closed:
clear is a no-op; core-valid plus valid pending follows normal recover/reschedule/complete processing;
core-valid plus repairable metadata typed-blocks; and byte-identical reschedule rejection typed-blocks.

Every core-valid pending candidate that cannot remain normally pending therefore either receives a
later canonical pending due key or atomically becomes a canonical block with protected case. Process
loss after block cannot erase discovery. `ProviderFenceRecoveryFunction` lists canonical blocks and
requeues only the exact canonical block output; requeue returns canonical pending with no repair step.
It never accepts or exposes selectors, calls Athena, changes safety or owner state, or clears FENCE.
Regardless of reconciliation form, only provider-terminal `CompleteTableAttemptRequest` clears FENCE.

`IngestionStack` passes the exact reconciler subset from §11—publications table, leases table,
BAKE workgroup ([Athena workgroups](../query-path/architecture.md#athena-workgroups-and-the-batch-start-budget)),
batch size `10`, base backoff `60`, and maximum backoff `900`—in addition to standard
tracing. Startup resolves and validates `AppConfig.ingestion` before creating any AWS client and rejects
missing, unrecognized, boolean, non-integer, nonpositive, or cross-field-invalid values. The function receives the repository ADOT
layer/configuration and exact export grant. Its role receives `dynamodb:Query` only on the exact GSI ARN
and only with `dynamodb:LeadingKeys=pending`; direct strongly consistent `dynamodb:GetItem` only on the
exact publications and leases table ARNs and required key families; no direct mutation; and transaction-
enclosed underlying `ConditionCheckItem|PutItem|UpdateItem` combinations only for its named coordinator
requests. `RecoverParkedAttemptSubmissionRequest` specifically receives FENCE `ConditionCheckItem` and
ATTEMPT `UpdateItem` enclosed by `TransactWriteItems`. For `BlockTableAttemptReconcileRequest`, this
includes exact `TABLE_ATTEMPT#*` FENCE/ATTEMPT members and the transaction-enclosed
`PROVIDER_RECONCILE_BLOCK#*` protected-case `PutItem`; all other requests remain limited to their exact
`TABLE#`, `TABLE_ATTEMPT#`, owner, `EVENT#`, and `STATUS_FLOW#` key families. It
has no protected-case read/update, blocked-partition query, or blocked→pending authority. It
receives `athena:StartQueryExecution`, `athena:GetQueryExecution`, and
`athena:StopQueryExecution` only on the typed batch-workgroup ARN. Statement replay receives exactly
`glue:GetDatabase|GetTable|GetTables|GetPartitions|UpdateTable|BatchCreatePartition|CreatePartition|UpdatePartition|DeletePartition|BatchDeletePartition`
on the account catalog and the three typed source-aligned/candidate/current database and table ARNs;
`s3:GetObject|PutObject|DeleteObject|AbortMultipartUpload|ListMultipartUploadParts` on the three physical
semantic table prefixes; prefix-conditioned `s3:ListBucket` and `s3:GetBucketLocation` on that semantic
bucket; and `s3:GetObject|PutObject|AbortMultipartUpload`, prefix-conditioned `s3:ListBucket`, and
`s3:GetBucketLocation` on the typed batch-results prefix/bucket. It has no `Scan`, cursor/control item,
other semantic or result prefix, immutable-authority object write, batch write, lifecycle delivery,
final-disposition settlement, pointer finalization, mapping update, wildcard action/resource, or
operator-role assumption.

The repository timing contract is derived from §11, never independently configured: function timeout
is 900 seconds; cleanup reserve is 60000 ms; lease duration is 930 seconds; queue visibility is
`timeout+margin=960` seconds; and refusal visibility caps at `lease+margin=990` seconds. Lambda derives
`Q/S/I` from one monotonic capture exactly as above. Operators require explicit canonical UTC `Z`
`--query-deadline` and `--safety-deadline` values with at most microsecond precision; there are no
defaults, relative offsets, non-`Z` forms, leap seconds, or excess fractional digits. Before Begin they
capture wall UTC and monotonic once, set the virtual operator envelope `Iop=wall_now+900 seconds`, and
require `Q>wall_now`, `Q+60 seconds<=S`, and `S+60 seconds<=Iop`. They translate Q, S, and Iop once by
integer wall-to-monotonic deltas and use the identical terminal/Q/S loop without consulting wall time
again. Lease expiry remains availability-only and never clears provider safety.

For either retryable refusal, the owner first commits `RecordStage1RefusalRequest`. Inline then returns
retryable with no SQS operation. Queued handling captures one UTC `now` for classification and
visibility calculation. `TABLE_BUSY` targets the matching lease expiry; active+pending
`TABLE_ATTEMPT_PENDING` targets `max(matching lease expiry, reconcile due)`; parked+pending targets the
reconcile due; valid blocked authority uses `990` seconds directly. For target cases, with signed
microsecond arithmetic:

```text
remaining = max(0, ceil((target - now) microseconds / 1000000))
visibility = min(990, max(1, remaining + 60))
```

The exact boundary results are: negative target `60`, zero `60`, `+1µs` `61`, `+929s` `989`, `+930s`
`990`, and `+930s+1µs` `990`. After recording, queued handling calls `ChangeMessageVisibility` exactly
once and only then raises or returns the retryable unacknowledged result. A visibility failure or
ambiguous response propagates unhandled as `TABLE_WRITER/3`, while preserving the already-recorded
public refusal reason; it performs no compensating zero-visibility call, unit failure, acknowledgment,
owner mutation, lease mutation, or fence mutation. No statement starts for any refusal.

### 5.3 Plan-intent reconciliation

`IngestionStack` deploys exactly one scheduled `PlanIntentReconciler` Lambda and no delivery-side
attempt-budget recovery path. It uses the existing built asset, typed runtime/architecture/layer, and
repository log retention; memory is `512 MiB`, timeout `120` seconds, reserved concurrency `1`, and
EventBridge invokes it every minute while enabled under §2.5's `stage1RecoveryEnabled` contract.
Typed settings are `planIntentReconcileBatchSize` (§11),
`planIntentReconcileBaseBackoffSeconds=60`, and `planIntentReconcileMaxBackoffSeconds=900`; startup
rejects every other value, boolean, default, missing, nonpositive, or non-integer representation before
a client exists. Delay is exactly `min(60 * 2^min(c,4), 900)` seconds from one captured UTC epoch-
microsecond instant, integer arithmetic, 20-digit zero padding, no jitter, and overflow refusal.

Each run issues one eventually consistent ascending `Query` on `stage1-plan-intent-ready`, partition
`ready_status="ready"`, sort bound `ready_order <= <20-digit now>#~`, `Limit` equal to
`planIntentReconcileBatchSize` (§11), from the first key,
with no scan or pagination state. Every returned key has
`ready_order=<20-digit due epoch-micros>#<intent_id>`. For each result it strongly reads the intent
`META` and complete
maximum-15-unit roster, `SUCCESSOR_OWNER`, optional active plan and origin intent, every referenced
active-plan attempt, opaque lifecycle event/head proofs, and exact plan `HEAD`. It reproduces the active
plan `META` immutable roster digest and complete settled-attempt count/digest from those reads. Missing,
changed, incomplete, owner/head-disagreeing, or digest-mismatched authority is an invariant failure. It
never reads a manifest or object body, republishes authority, mutates `CURRENT`, or calls Athena. A
pending intent is recoverable regardless of the source queue message or source DLQ state.

`PlanIntentReconciler` replays only the exact stored plan-intent authority through typed builders; the
provider-attempt reconciler alone replays a parked Athena start envelope when applicable.

For a due generation-independent pending intent, `ClaimPlanIntentRequest` CASes the exact owner revision
and intent snapshot in one transaction, computes `claimed_generation=owner.last_generation+1`, stores
`reserved_intent_id`, byte-identical `reserved_intent_digest`, and `reserved_generation` on the owner, and stores the
same `claimed_generation` while changing the intent `pending→claimed`. `PersistPlanRequest` consumes that
reservation only when `owner.reserved_generation = intent.claimed_generation =
owner.last_generation+1`, then derives execution and plan keys from `intent_id` plus
`claimed_generation`. With no active plan it performs ordinary persistence. With an active planned plan
it performs the one atomic planned-plan replacement after every predecessor accepted attempt is
terminal and already settled. A transient pre-persistence dependency uses
`ReschedulePlanIntentRequest`; while the intent remains claimed that request cannot invent or change its
assigned generation. Its `claimed→pending` retry clears the reservation, removes `claimed_generation`,
and restores the canonical later due, `ready_status="ready"`, and
`ready_order=<20-digit due epoch-micros>#<intent_id>`. No blocked intent state or permanent-refusal
transition exists; a non-retryable condition fails closed without mutation. The next claim
assigns the then-current owner `last_generation+1`. After every submit, claim, persistence, queue send,
admission, terminal write, SDK success, timeout, ambiguous response, condition race, and process-death
boundary, source-delivery handling performs a strongly consistent read of the exact intent `META`, its
complete intent-unit roster, `SUCCESSOR_OWNER`, the optional exact candidate plan `META` and units,
origin intent, lifecycle event, aggregates, and plan `HEAD`. It may acknowledge the source delivery only
when that closed read proves exactly one of these four outcomes:

1. The exact intent is `state=pending`, has no `claimed_generation`, and is indexed with
   `ready_status="ready"` plus `ready_order=<20-digit due epoch-micros>#<intent_id>` matching its exact
   due and generation-independent identity.
2. The exact intent is `state=claimed`, its `claimed_generation` is present, and the owner's complete
   reservation trio `reserved_intent_id`, byte-identical `reserved_intent_digest`, and `reserved_generation`
   recoverably names that exact intent with `reserved_generation=claimed_generation`.
3. The exact intent is `state=admitted`, retains the plan's assigned `claimed_generation`, and the
   owner's paired `active_plan_key`/`active_intent_id` binds it to the exact candidate plan with
   owner/plan-`HEAD` agreement.
4. The exact candidate plan is `admission_failed`, and the same atomic transition proves the exact
   intent pending transfer with `claimed_generation` absent, both correct ready fields, the owner active pair clear, the immutable
   admission-failed lifecycle event, and the exact resulting plan `HEAD`.

Before the intent commit, or when the readback is missing, inconsistent, partial, matches no outcome, or
matches more than one, handling raises and leaves the source delivery unacknowledged. A durable handoff
may be replayed and then acknowledged. No SDK response, transaction return, timeout classification, or
transport result is acknowledgment authority. Reconciliation remains independent after the source
message reaches its DLQ.

The same strong read also governs continuation without changing source-delivery acknowledgment
semantics. Outcome 1 may retry the same claim and assign only the then-current next generation. Outcome
2 may resume only the byte-identical `PersistPlanRequest` for the retained claim assignment. Outcome 3
resumes only unit or completion recovery. Outcome 4 leaves the same generation-independent pending
intent for later claim. A different winning reservation is adopted only after a complete reread. Any
incomplete replacement, changed immutable intent or unit bytes, mismatched/absent claimed versus
reserved generation, predecessor settlement mismatch, owner/plan-`HEAD` disagreement, second planned
plan `META`, wrong active pair, differing event bytes, or malformed ready fields is collision or
corruption and permits no alternate-key recovery, send, admission, reschedule, acknowledgment, or
mutation. A stale claimant cannot persist or reschedule after another claim or owner advance; it rereads
and returns the exact winner or fails closed.

Admission is restart-safe. Each pending plan unit uses only the exact §4 body, physical-unit group,
and `stage1-write-unit-message/v1` deduplication constructor, then `AdmitUnitRequest`;
`PlanIntentReconciler` alone sends and admits, and phase-1 feasibility preflight invokes the same pure
constructor without sending. Writers strongly reread the active owner,
plan/unit/origin-intent authority, and plan `HEAD` and reject a stale generation or any different body
before claim. Writers require full admission and owner-active equality. After each queue send or
admission write, including a transient failure or ambiguous response, source-delivery handling performs
the closed strong read above. A complete read showing only partial admission matches no acknowledgment
outcome, so the source handler raises and leaves the delivery unacknowledged; `PlanIntentReconciler` may
independently replay the remaining byte-identical sends/admissions. A terminal admission write uses
`FailAdmissionRequest` to atomically set the candidate `admission_failed`, clear the paired owner active
fields, change the same intent `claimed→pending` with its immutable origin bytes unchanged, remove
`claimed_generation`, restore both correct ready fields, write the lifecycle event, and advance the
plan `HEAD`. It creates no successor intent. The next claim assigns the then-current owner generation;
process death after the failed admission cannot reuse the failed candidate generation. Process death at
any claim, persistence, send, admission, terminal write, response, or acknowledgment boundary returns
through the same classifier. Already-sent unit-message acknowledgment retains the unit-scoped
semantics in §§2.1 and 3.

Only this role receives `dynamodb:Query` on
`<publications-table-arn>/index/stage1-plan-intent-ready` with `dynamodb:LeadingKeys=ready`, direct
strong reads for exact `WRITE_PLAN#*` and `STATUS_FLOW#*` keys, transaction-member actions for
`ClaimPlanIntentRequest`, `PersistPlanRequest`, `ReschedulePlanIntentRequest`, `AdmitUnitRequest`, and
`FailAdmissionRequest`, plus `sqs:SendMessage` on the exact semantic-write queue ARN. It has no S3,
Athena, Glue, manifest queue, publication pointer, lease/FENCE, provider block, scan, batch, delete,
wildcard-resource, or operator-assume-role grant. Other runtime roles cannot query this index or
claim/reschedule pending intents. The phase-1 delivery caller has only `SubmitPlanIntentRequest`; it has
no semantic-write queue send, admission, admission-failure, `stage1-plan-intent-ready` query, claim,
reschedule, or persistence authority. The table writer may create the one deterministic successor intent only inside
its attempt-33 `FailUnitRequest`; that creation consumes the owner and remains independently recoverable.

## 6. Consolidation, verification, and exact Stage-1 Athena accounting

Within one unit, the existing helper invariant and packer own the statement roster used by feasibility
and runtime. Before or during the one production `ordered_records` call for an object, reject every
repeated declared table grain, including repetitions whose remaining values are byte-identical. Distinct
grains are sorted exactly once by the existing typed table-grain order and passed directly to the
production renderer; no duplicate-record collapse, record-byte equality rule, or new record canonicalizer
exists.

Before record ordering, object keys are compared by `(kind, reference)`. Two objects with the same key
may deduplicate only when their object reference, checksum, publication identity, and encoded object
authority are byte-identical; any difference rejects. After exact-object deduplication, canonical object
order is ascending unsigned UTF-8 byte lexicographic order of
`(kind.encode("utf-8"), reference.encode("utf-8"))`; comparison is byte by byte in `0..255`, with the
shorter byte string first after an equal prefix. Caller/container order never participates and no
implementation may invent another tie-breaker.

Packing is deterministic maximal-contiguous-prefix, whole-object greedy packing:

```text
for object in canonical_objects:
    candidate = render(current whole objects + object)
    if current is nonempty and candidate exceeds the statement byte bound:
        emit(render(current)); current = []
    if render(object alone) fits the byte bound:
        append the whole object to current; continue
    if current is nonempty: emit(render(current)); current = []
    while object has remaining ordered records:
        fragment = the longest nonempty contiguous prefix whose render fits the byte bound
        if no one-record prefix fits: refuse before authority
        emit(render(fragment)); remove exactly that prefix
# fragments from a split object are emitted immediately and never share a statement with a prior/next object
if current is nonempty: emit(render(current))
```

`render` is the one production merge renderer; the bound applies to the exact UTF-8 bytes of the
merge's canonical start envelope (SQL text plus every encoded parameter, §11), not an estimate. There is no bin packing, backtracking, caller order, fragment reordering,
cross-object split fragment co-packing, or implementation-selected tie. The simultaneous maximum of 37
object references must produce at most nine merges under the 200000-byte bound, followed by
exactly one verification, and §2.1's sum across every unit must be `<=32`. Execute merges sequentially
through §5. Retry readback may exclude only a byte-exact whole object after every one of that object's
identities matches; it may not exclude a record, fragment, checksum-only match, or same-identity/different-
byte object. Final verification always covers every declared identity in the complete unit, including
identities from excluded exact objects and every split fragment. Insert-only merge on the publication
grain makes retries idempotent; every accepted pre-read, retry, failed, or cancelled statement still
consumes the same 10-per-unit and 32-per-plan reservation caps.

Each accepted provider terminal records `query_count=1`. Its `scanned_bytes` comes only from
`DataScannedInBytes`, and `engine_ms` only from `EngineExecutionTimeInMillis`, on that terminal
`GetQueryExecution` result. This applies identically to `SUCCEEDED`, `FAILED`, and `CANCELLED`, whether
observed by the writer, operator, or reconciler. `FAILED` and `CANCELLED` are failed attempts for durable
unit/run disposition, but `failed_count` is not a usage quantity. If the provider omits either quantity,
the attempt records that quantity as unknown; no adapter supplies zero, elapsed wall time, or a local
estimate. Final owner disposition then settles this Athena evidence under the shared Stage-1 mapping.
Stage-1 per-flow usage authority is explicitly Athena-only: this mechanism creates no Lambda, queue, or
storage contribution identity or `USAGE#` record. Those resources remain the already accepted ADR-074
companion/CloudWatch before-and-after performance comparison.

Local validation refusal occurs before Begin and therefore creates no attempt or reservation. After
Begin, `Accepted` and `AcceptanceAmbiguous` both retain the permanent reservation, intent, FENCE, and
`current_attempt_id`; absence of a valid query execution identity is never evidence of rejection. The
live path may replay the identical stored envelope only before its safety deadline, then parks. The
provider-attempt reconciler replays an exact parked envelope when applicable. Only a valid nonblank
query execution identity and terminal `SUCCEEDED|FAILED|CANCELLED` observation create terminal usage
evidence. Attempt terminal/accounted conditions make direct and reconciler processing idempotent. A
failed statement is terminally recorded before unit failure, whose disposition transaction settles the
mixed accepted-attempt roster solely under the concept-owned mapping. Clean first-attempt expectation
remains `Σ merges + one verify per unit`; retries and failed/cancelled work report exact Athena usage only
after that owner settlement.

Athena result reuse remains disabled; the design does not depend on it.

## 7. Servability and supersession

For publications P1 then P2, authority may reach P2 while servability remains at the last attested
publication. FIFO normally projects P1 then P2. If P1's unit fails, P2 may still finalize to P2; P1's
failed unit remains recoverable. If an older unit reaches finalization after a newer projected token,
step 6 omits that pointer as `projection_superseded` and still atomically attests the unit. A newer
pointer conflict can therefore never strand an otherwise verified unit.

## 8. Failure, retry, and recovery

| Situation | Behaviour | Recovery |
|---|---|---|
| Statement fails | Close exact attempt accounting; retry while receives remain; on exhaustion the current writer uses the exact unexpired lease-conditioned failure transaction to change only its unit to `failed` and increment `failed_count` | DLQ redrive performs `failed→writing` and decrements `failed_count`; siblings continue throughout |
| Writer reaches local query deadline | After terminal-first processing, a durable valid query ID permits cancellation persistence followed by `StopQueryExecution`; without that ID, retain intent/reservation/FENCE/current attempt and issue no cancellation, Stop, or provider poll, permitting only the bounded exact Start replay before `S` | Stop success remains nonterminal. Direct path or scheduled reconciler confirms provider terminality and records exact accounting; an ID-absent attempt parks at `S` without cancellation or Stop. |
| Writer reaches Lambda safety deadline | Terminal-first processing still wins. Otherwise an ID-present or ID-absent exact lease/attempt fence is parked and remains active without expiry; no new provider poll, cancellation, Stop, or Start occurs at `S`, the unit stays `writing` and retryable, and no writer starts on that table | Bounded reconciler obtains a valid ID when permitted, observes provider terminality, accounts once, then atomically clears the exact fence and parked lease |
| Reconciler reads core-invalid provider pair | Emit sanitized `CORRUPT_PROVIDER_CORE_AUTHORITY`; fail invocation unhandled with zero Athena/coordinator/block/case/requeue/clear/lease/owner mutation | No ordinary recovery or liveness promise; only separately reviewed retained-table disaster recovery may address external corruption |
| Reconciler reads core-valid repairable metadata or byte-identical reschedule rejection | Typed block replaces only reconciliation metadata with canonical blocked form and atomically creates the protected case; provider safety and owner state remain unchanged | Invoke-only broker lists and requeues the canonical block output to canonical pending; no correction step; provider-terminal completion alone clears FENCE |
| Writer dies before finalization with no provider attempt | No pointer, unit, or `META` finalization effect exists | Redelivery reclaims the `writing` unit through the `ClaimUnitRequest` writing branch once the dead writer's lease is absent, expired, or released, then resumes |
| Writer dies after finalization and before completion | Pointer advances, unit attest, and `attested_count` already exist together | Attested branch runs the sole completion predicate and acknowledges |
| `TABLE_BUSY` or `TABLE_ATTEMPT_PENDING` | Apply core/FENCE/lease precedence, start no statement, record the exact public refusal, and do not acknowledge. Queued handling computes the one-capture target visibility and calls `ChangeMessageVisibility` once; inline performs no SQS operation. | `TABLE_BUSY` retries after matching lease expiry or explicit release; `TABLE_ATTEMPT_PENDING` retries only after confirmed-terminal fence clearance. Visibility failure/ambiguity propagates `TABLE_WRITER/3` unhandled with no compensation, unit failure, or owner/fence effect; only this message can exhaust. |
| Unit reaches DLQ | Unit is `failed` or `writing`; active intent remains `admitted`; other units continue | Redrive the exact unit; no phase 1 rerun is required |
| Post-Begin start returns `AcceptanceAmbiguous` | Permanent reservation, intent, FENCE, and `current_attempt_id` remain; blank/malformed result or any exception is never treated as rejection. At `Q` without a durable valid ID, issue no cancellation, Stop, or provider poll. | Before `S`, live exact replay may use only the stored envelope/token and at most one Start call per invocation; a later invocation may replay. At `S` with no ID, park without cancellation, Stop, provider poll, or another Start. Only durable valid query ID plus terminal observation progresses. |
| Queued Begin requests plan attempt 33 | Local preflight makes zero Athena calls and creates no Begin reservation. `FailUnitRequest` terminalizes the source plan and replaces its ordinary failure trigger with one deterministic origin `plan_attempt_budget` successor intent `META` plus all intent `UNIT#` puts; the transaction consumes the owner. Trigger and maximum intent-item puts have independent byte/action fixtures. The failed unit message acknowledges only after commit or exact replay. | `PlanIntentReconciler` recovers the pending intent independently after the unit reaches its DLQ and uses only retained intent/owner authority. |
| Two distinct intents submit against one unchanged owner snapshot | Both generation-independent pending `META` records may commit and coexist; neither stores or derives a generation. | The single owner CAS serializes claims. The first successful claim/persist receives `G=last_generation+1`; after that owner advance, the next claim/persist receives `G+1`. At every boundary at most one plan is active/planned. |
| Process dies after a claim commits | Owner reservation and claimed intent atomically retain the same assigned generation; pending-ready fields are absent. | Strong reread matches §5.3 outcome 2 and resumes exact persistence or exact reschedule. No other claimant can reuse or alter that generation. |
| Stale claimant runs after another claim, reschedule, requeue, or owner advance | Its reservation revision, `reserved_generation`, intent state, or `claimed_generation` no longer matches. | It performs no mutation; complete strong reread returns the exact winner/current pending state or fails closed. It cannot write old generation bytes. |
| Intent reconciliation dies, times out, races, or replays | Exact intent, ready fields, optional `claimed_generation`, `SUCCESSOR_OWNER`, optional candidate plan and origin intent, lifecycle event, aggregates, and plan `HEAD` remain authoritative at every boundary. | Strongly reread all authority and apply the four outcomes in §5.3; before intent commit or on partial/inconsistent readback, raise and leave the source delivery unacknowledged. Exact claim/persist replay retains the assigned generation; a durable handoff may replay before acknowledgment. |
| Intent admission terminal write completes or is ambiguous | `FailAdmissionRequest` may have atomically set the candidate `admission_failed`, transferred the same intent `claimed→pending` with `claimed_generation` removed and both correct ready fields restored, cleared the owner active pair, written the lifecycle event, and advanced plan `HEAD`. | Strongly reread and acknowledge the source delivery only if §5.3 outcome 4 is proved; otherwise raise and leave it unacknowledged. The same pending intent remains the only retry authority, and its next claim receives the then-current owner generation. |
| Reconciler queue send/admission returns success, transient failure, or ambiguity after persistence | No return is acknowledgment authority. | Strongly reread and apply §5.3. Only one complete outcome may acknowledge a held source delivery; a complete read showing partial admission matches no outcome, so a source handler holding the message raises unacknowledged while `PlanIntentReconciler` independently replays the remaining deterministic sends/admissions on a later run. |
| Reconciler queue send/admission terminally fails | The reconciler invokes `FailAdmissionRequest`; no unit was executable. Already-sent unit messages acknowledge only after commit or exact replay makes the candidate non-executable. | A held source delivery strongly rereads and requires §5.3 outcome 4; `PlanIntentReconciler` retries the same pending intent. |
| Phase 1 receives a source delivery | `SubmitPlanIntentRequest` creates one pending intent `META` with both correct ready fields and its complete maximum-15-unit roster while consuming the exact owner snapshot. | Strongly reread and apply §5.3. A source delivery acknowledges only after exactly one complete outcome is proved; before intent commit or on partial/inconsistent readback it raises and remains unacknowledged. Reconciliation remains independent after source DLQ. |
| `PlanIntentReconciler` replaces an active planned plan | After reserving the pending intent, it verifies predecessor terminal settlement and plan `META` roster/settled-attempt aggregates, then uses a frozen exact map within the 100-action ceiling to terminalize predecessor, create target plan, update the owner exactly once, put both events, and advance head once. | Nonterminal/unsettled work may reschedule while the exact reservation remains. Aggregate mismatch or partial/conflicting state fails closed. |
| Owner-swap transaction conflicts | Strongly reread owner, reserved intent, active plan/origin intent, events, and head. | Return committed winner, resume exact reservation/replay, or fail collision; never create an alternate intent. |
| Pointer observed equal/newer | Omit advance, record `projection_superseded`, attest unit in finalization transaction | None; servability is already at least as new |
| Pointer condition races | Entire transaction aborts; strong re-read classifies omit versus advance | Retry while holding exact lease; newer pointer is not a unit failure |
| Immutable checksum mismatch | Account no nonexistent Athena work; fail only the unit | Investigate, then redrive; never repair from projection |

A unit failure is never a plan-wide terminal transition. Plan terminal states are only completed,
superseded, and pre-execution admission failure.

## 9. Operator and transitional inline paths

Before the final switch, the delivery Lambda and the two operators
`ingest_delivery/__main__.py` and `reprocess_semantic_v2/__main__.py` are inline writers. In the
pre-switch-fence slice all three use the same prepared-plan feasibility callback, replicated
S3-plus-Iceberg storage, coordinator-owned lease fence, and table-attempt protocol as the queued
writer. The
Lambda uses its directly attached, builder-scoped execution-role grants; each local operator performs
the §3.2 typed-parameter/account-check/assume-role bootstrap before composing any semantic client.
After the final switch, the delivery Lambda ceases to be an inline writer; the two operators retain
this contract. In `GRAPH`, `reprocess_semantic_v2 --apply` (which now requires
`--graph-store-stack`) makes one strong read of the authority mode before any write and refuses
with `REPROCESS_REQUIRES_GRAPH_RUN` (`ADLY-REPROCESS-0001`), because its `inline_projection=True`
commits would advance servability outside `publish`; a graph-native reprocess is an `on_demand`
`data_correction` run request (D22).

For each physical table an inline writer:

1. proves the exact table group/member, statement, pointer, event, and encoded transaction bounds before
   its first authority mutation, using §11's same `IngestionConfig` fields and joint-envelope proof;
2. calls `PersistInlineRunRequest` with only its closed `writer_kind`; the private builder consumes the
   public identity, topology, table-kind mapping, and registered lifecycle meaning exclusively from
   [Operational Data-Flow Status § Stage-1 event projection](../operational-data-flow-status/low-level-design.md#stage-1-event-projection),
   then atomically creates or exactly replays the run/table accounting item, event authority, and head
   before any statement; callers cannot supply or reproduce status-owned definitions;
3. on a nonterminal table attempt, writes the deterministic retryable refusal only through the inline
   tag of `RecordStage1RefusalRequest` against the exact open/reconciled run and head, then returns
   without acquiring a lease or starting a statement; otherwise acquires the exact table lease and
   executes every merge/verify statement through §5, with each attempt linked to that run item;
4. for current-table work, calls `DynamoProjectionCoordinator.finalize_inline` while the exact finalizer
   and table lease are held. One transaction condition-checks the lease and exact observed pointers,
   expands `SettleStage1AthenaUsageRequest` for every referenced terminal accepted attempt using the
   final run/pointer outcome, advances all required pointers, creates the deterministic publication-
   partition event defined in §10 for each pointer outcome, and terminalizes the run item only after all
   references are settled. The usage authorities, pointer outcomes,
   `semantic_projection_servable` pending event, and run terminal therefore commit together. Source and
   candidate runs terminalize the same item without pointer updates. No plan key or unit event is
   invented for this no-plan path;
5. releases the lease only after a provider terminal clears `FENCE`; at a safety deadline it parks the
   exact lease/fence and returns, leaving the run open for §5 reconciliation.

Inline writers process tables one at a time in lexical physical-table order and never hold multiple
table leases. `TABLE_BUSY` and `TABLE_ATTEMPT_PENDING` use bounded backoff and return a typed retryable
outcome. Source and candidate tables still take the lease even though they have no pointers. Receipts
carry `write_plan_key = "inline"`. The post-run outside-lease pointer path does not exist.

## 10. Identity and observability

[Operational Data-Flow Status § Stage-1 event projection](../operational-data-flow-status/low-level-design.md#stage-1-event-projection)
is the exclusive owner of every registry/status identity, topology, execution-source, table-kind,
surface, status, ordering, predecessor, and event-key definition. This document consumes only its opaque
typed proofs and does not name or reproduce their schemas, tuples, versions, enums, matrices, mappings,
or constructors. [Data-Flow Completion and Service Levels § Stage 1
usage attribution](../../concepts/data-flow-completion-and-service-levels.md#stage-1-usage-attribution)
is likewise the exclusive owner of owner-disposition category mapping and precedence. Stage-1 durable
keys and digests described here are internal authority identities, never public status definitions.

The queued `TABLE_WRITER_PROCESS` and `TABLE_WRITER_UNIT` spans carry the **unit** owner's persisted
public `flow_id`, `parent_flow_id`, `causation_event_id`, `execution_id`, and optional
`predecessor_execution_id`, plus `internal_plan_key`, `writer_execution_id`, and the closed progress
fields. `TABLE_WRITER_UNIT` is the business boundary that records the unit's final attested/failed
outcome and closes only after unit finalization or exact failure; it alone may add `table_kind`.

A conditional plan completion opens `TABLE_WRITER_PLAN_TERMINAL` inside the current unit's lexical
trace scope but carries only the persisted plan-owner public identities selected by the linked status
contract plus `internal_plan_key`; it never inherits unit flow or writer identity. Its trace parent is a
local trace-tree relationship, not a redefinition of public topology. No-op and winning terminal checks
use the identity already persisted before the span opens.

Trace creation never copies internal operation/plan/unit digests into public fields and never asks the
status projector to mint identity. Plan-intent reconciliation telemetry uses only
`internal_plan_key`, `intent_id`, `generation`, `owner_revision`, closed claim/outcome code, and the
source-acknowledgment digest; it emits no manifest/object content, queue receipt, SQL, provider result,
or public identity input. The closed claim/outcome code is exactly one of
`claimed | persisted | sent | admitted | admission_failed | rescheduled | exact_replay | stale_winner |
invariant_failure`; no other value is emitted or stored, and the vocabulary is scoped to
`PlanIntentReconciler` actions only — phase-1 delivery telemetry emits the source-acknowledgment digest
and the proved §5.3 outcome ordinal (1–4), never a claim/outcome code. The source-acknowledgment
digest has exactly one constructor:

```text
source_acknowledgment_tuple = ["stage1-source-acknowledgment/v1", intent_id,
  proved_outcome_ordinal, owner_revision]
source_acknowledgment_digest = canonical_authority_digest(source_acknowledgment_tuple)
```

`proved_outcome_ordinal` is the JSON integer 1–4 of the exactly-one proved §5.3 outcome and
`owner_revision` is the `SUCCESSOR_OWNER.revision` JSON integer observed by that same closed strong
read, so the digest is deterministic per classifier read, correlates acknowledgment decisions without
disclosing manifest, roster, or owner content, and renders as 64 lowercase hex characters. No other
derivation exists, and both telemetry consumers emit this exact value. The six Stage-1 components without a
`spans.yaml` row — `PlanIntentReconciler`, `ProviderAttemptReconciler`, `ProviderFenceRecoveryFunction`,
the projection-handoff provider, `Stage1LifecycleProducer`, and `Stage1OutboxRecoveryFunction` — are
span-free by design: they emit structured operation logs, errors, and metrics only through the shared
reporting seam, receive the repository ADOT layer solely for SDK export of that telemetry, and declare
no repository span constant; adding one requires a reviewed `spans.yaml` row first. The table-writer and
inline-run span families remain the only Stage-1 trace boundaries. The remaining opaque locators are named explicitly by the executable contract:
`internal_plan_key`, `internal_delivery_execution_id`, `writer_execution_id`, and the inline
`internal_operation_id`, each only on boundaries that need it. Progress remains `stage`, `completed`,
and `total`.

Every inline writer opens one `TABLE_INLINE_RUN` span per physical-table run beneath its current
operator process. It carries only the persisted public identity fields selected by the linked status
contract plus safe internal operation/writer/table locators already declared by the executable span
contract. Trace/log callers cannot supply public identities or topology. The span opens after owner
persistence and before the first statement; exact replay reuses persisted identity.

The shared reporting seam emits the same persisted public proof without interpreting it. Unit records
use unit owner identity; plan-terminal records use plan owner identity; inline records use run owner
identity. Internal locators remain only on their declared boundaries. No trace or log layer may infer
public topology, status, surface, pointer source, or table-kind mapping. Operators read lifecycle
counters from the strongly consistent intent `META`, not traces.

Lifecycle delivery is authority-backed rather than post-commit best effort. Every plan, unit, and
inline state transition writes its deterministic `EVENT#` item and conditionally advances the §3.1
lifecycle head in the same transaction. Zero-row no-plan pointer transitions write the publication-
partition event keys defined in §§2.1 and 9. Publications-table stream delivery and sparse-index
recovery for both lifecycle and usage authorities are owned by the concrete `Stage1LifecycleProducer`
runtime in
[Operational Data-Flow Status §6.2](../operational-data-flow-status/low-level-design.md#stage-1-lifecycle-delivery-runtime).
That producer validates the linked contract proof, sends to the status FIFO, and uses only the status-
owned delivery and poison-block operations; it never requeues. Block fallback is exhaustive wherever
projection classification applies: plan owner/intent, unit, inline-run, and pointer lifecycle authorities
each retain their immutable outbox, remove ready delivery keys, record a sanitized blocked disposition,
and allow unrelated authorities to continue. No family is dropped, retried forever outside durable
state, or converted to another family's mapping. The separately authorized operational path alone may
requeue a byte-identical blocked authority after cause resolution. Lifecycle owners receive no block/
requeue permission, and delivery performs no provider-attempt reconciliation.

For every no-plan pointer outcome, the internal durable key remains the publication-partition
`EVENT#` authority defined by this write mechanism. The finalizer obtains the complete pointer public
identity, topology, and execution-source proof only from
[Operational Data-Flow Status § Stage-1 event projection](../operational-data-flow-status/low-level-design.md#stage-1-event-projection),
persists that proof immutably with the pointer outcome, and excludes internal source material from
public payloads, logs, metrics, and status keys. No local enum, matrix, constructor, or fallback source
exists.

The same-receive queued rebuild reuses its writer identity. A queued finalization transaction abort with
no event or attestation permits later legal reclaim with a new writer. Inline retry for the same RUN
always reuses its immutable execution identity; only a separately accepted RUN may differ while no
pointer event exists. A zero-row full abort with no successful attempt/publication/event permits a new
attempt. Once the pointer event exists, its execution-source pair and resulting public identity are
authoritative forever through delivery and newer pointers; any recomputed mismatch fails. Outbox fields begin with `outbox_state="pending"`,
`outbox_status="pending"`, and the deterministic `outbox_order` owned by the status LLD. Delivery or
poison blocking removes both sparse-index fields while setting the corresponding terminal outbox state
and retaining the immutable payload; delivery additionally retains the write-once
`authority_observed_at` set by first lifecycle projection. Exact owner replay equality excludes mutable
outbox-state facts, while public event replay uses the persisted observation instant byte-for-byte.

A pointer retry strongly reads this key before re-evaluating `CURRENT`. If the event exists
and its immutable fields match, its recorded pointer outcome is authoritative: return that outcome and
resume or confirm its existing delivery state without resetting it or rebuilding the pointer decision.
A mismatched immutable payload is an invariant failure. If the event is absent, resolve the pointer and
transact its exact outcome with `attribute_not_exists(record_key)` event creation. On transaction
conflict, read the event first and apply the same rule; only an absent event permits pointer re-read and
transaction rebuild. Thus retry after `DELIVERED` or after a newer pointer appears cannot rewrite the
original outcome.

Plan-intent persistence writes the status-owned recorded lifecycle authority and advances its head
before the first queue send; delivery remains asynchronous. Admission, terminalization, unit, inline,
and pointer transactions write only the event authority required by their linked status mapping, without
restating its event names or meanings here. Owner-assigned order survives reversed delivery through the
status-owned reducer. Durable intent/unit counters remain local lifecycle authority. Athena quantities
come only from terminal table-attempt records; owner-disposition settlement uses exclusively the linked
usage mapping and exact replay preserves category/payload. Provider reconciliation and lifecycle
delivery never infer category or resource use from telemetry. Stage 1 creates no per-flow Lambda, queue,
or storage usage authority.

## 11. Resource and limit summary

`AppConfig.ingestion: IngestionConfig` is the sole per-environment owner. It is one frozen nested object
that includes `projectionHandoffState` and the following fields; CDK and Python consume the same values.
All integers are positive and booleans are rejected.

| `IngestionConfig` field | Reviewed value | Runtime/CDK name and consumers |
|---|---:|---|
| `projectionHandoffState` | one §2.5 six-state tag | sole public `IngestionStack` handoff input |
| `stage1RecoveryEnabled` | typed boolean per the §2.5 enablement contract | renders the two reconciler schedules and the `Stage1LifecycleProducer` stream mapping/schedule `Enabled` states; `false` only for the first pre-switch-fence deployment before the retained passing `verify-pre-switch` report |
| `maxUnits` | 15 | `ADL_STAGE1_MAX_UNITS`; prepared-plan validator |
| `maxAcceptedAttemptsPerUnit` | 10 | `ADL_STAGE1_MAX_ACCEPTED_ATTEMPTS_PER_UNIT`; queued UNIT and inline RUN reservation conditions |
| `maxAcceptedAttemptsPerPlan` | 32 | `ADL_STAGE1_MAX_ACCEPTED_ATTEMPTS_PER_PLAN`; complete-roster clean-attempt feasibility formula and queued META reservation condition |
| `maxGroupMembers` | 4 | `ADL_STAGE1_MAX_GROUP_MEMBERS`; grouping and operator no-plan finalization |
| `maxPointersPerTable` | 37 | `ADL_STAGE1_MAX_POINTERS_PER_TABLE`; queued/inline finalizers |
| `maxObjectRefsPerUnit` | 37 | `ADL_STAGE1_MAX_OBJECT_REFS_PER_UNIT`; unit encoder |
| `athenaMaxStatementBytes` | 200000 | `ADL_STAGE1_ATHENA_MAX_STATEMENT_BYTES`; §6 statement packer and §5 step-2 validator, measured as the UTF-8 bytes of the statement's canonical start envelope (SQL text plus every encoded parameter). The only per-statement bound; there is no parameter-count bound (2026-10-10 amendment below). |
| `maxEncodedIdentityBytes` | 128 | `ADL_STAGE1_MAX_ENCODED_IDENTITY_BYTES`; every internal identity constructor, measured as UTF-8 bytes |
| `maxPartitionIdentityBytes` | 256 | `ADL_STAGE1_MAX_PARTITION_IDENTITY_BYTES`; the Publication-State-owned `PartitionIdentity.identity` carried as the roster `partition_key-or-null` member and as pointer partition keys, measured as UTF-8 bytes; the roster constructor and feasibility refuse a longer identity before authority |
| `tableWriterTimeoutSeconds` | 900 | `ADL_TABLE_WRITER_TIMEOUT_SECONDS`; table-writer Lambda timeout |
| `tableWriterMemoryMiB` | 2048 | table-writer Lambda memory; CDK-only, no runtime consumer |
| `tableWriterCleanupReserveMs` | 60000 | `ADL_TABLE_WRITER_CLEANUP_RESERVE_MS`; table-writer runtime |
| `tableWriterConcurrency` | 8 | `ADL_TABLE_WRITER_CONCURRENCY`; Lambda reserved concurrency and event-source mapping maximum concurrency. Raised from the initial reviewed `2` on 2026-09-21 with the §14 wait-time evidence: the wide-window run advanced 22 distinct physical tables at most 2 at a time; per-table fencing, not this value, owns correctness. |
| `semanticWriteMaxReceiveCount` | 3 | `ADL_TABLE_WRITER_MAX_RECEIVE_COUNT`; queue redrive and writer runtime |
| `tableLeaseDurationSeconds` | 930 | `ADL_TABLE_LEASE_DURATION_SECONDS`; queued and inline acquisition |
| `semanticWriteVisibilityMarginSeconds` | 60 | `ADL_TABLE_WRITER_VISIBILITY_MARGIN_SECONDS`; derives queue visibility `960` and busy override cap `990` |
| `tableAttemptReconcileBatchSize` | 10 | `ADL_TABLE_ATTEMPT_RECONCILE_BATCH_SIZE`; pending GSI query limit |
| `tableAttemptReconcileBaseBackoffSeconds` | 60 | `ADL_TABLE_ATTEMPT_RECONCILE_BASE_BACKOFF_SECONDS`; reconciler |
| `tableAttemptReconcileMaxBackoffSeconds` | 900 | `ADL_TABLE_ATTEMPT_RECONCILE_MAX_BACKOFF_SECONDS`; reconciler |
| `planIntentReconcileBatchSize` | 50 | `ADL_PLAN_INTENT_RECONCILE_BATCH_SIZE`; `stage1-plan-intent-ready` query limit. Raised from the initial reviewed `10` on 2026-10-10 (amendment below). |
| `planIntentReconcileBaseBackoffSeconds` | 60 | `ADL_PLAN_INTENT_RECONCILE_BASE_BACKOFF_SECONDS`; intent reconciler |
| `planIntentReconcileMaxBackoffSeconds` | 900 | `ADL_PLAN_INTENT_RECONCILE_MAX_BACKOFF_SECONDS`; intent reconciler |
| `drainObservationIntervalSeconds` | 300 | `ADL_PROJECTION_HANDOFF_DRAIN_OBSERVATION_SECONDS`; handoff provider/read-only acceptance |

Queue visibility, refusal visibility, Lambda safety/cancellation deadlines, both reconciliation due times,
and merge row count are derived outcomes, not settings. Validation requires each reconcile max at least
its base, the table-attempt batch exactly 10 and the plan-intent batch exactly 50, cleanup reserve exactly 60000 and less than timeout in milliseconds,
lease greater than timeout, attempt caps exactly 10 per unit and 32 per queued plan, and derived queue
visibility `timeout+margin=960` within the SQS ceiling. Runtime uses only the exact integer clock and
deadline formulas in §§5 and 5.3; it never accepts Lambda deadlines or schedule instants as
configuration. The shared concurrency and receive values must render byte-identically to every listed
consumer. Drain timing follows §2.5's strongly read all-clear/reset rule.

Python implementation belongs only in `adapters/config.py` as frozen
`stage1_delivery_settings`, `table_writer_settings`, `table_attempt_reconciler_settings`,
`plan_intent_reconciler_settings`, `provider_fence_recovery_settings`, and
`projection_handoff_settings` accessors over `AppConfig.ingestion`. Each entry point resolves its
narrow accessor before creating clients; the delivery accessor names no queue, because the delivery
Lambda never sends a semantic-write message, and every consumer's environment renders the whole
object so a strict accessor never fails a container at import against a subset. These
values do not enter broad `Settings`, domain, or ports, and no runtime reads environment variables
directly.

The §3.2 `Stage1DynamoByteOracle` separately validates at most 100 distinct-item transaction actions,
`conservative_service_item_bytes<=400000` for every exact pre-state and result, and the sum of per-target
pre/result maxima `<=4000000` for the same simultaneous profile: 15 units, 4 group members, 37
pointers/object references, 10 reserved/accepted attempts on the current unit, 32 across the plan,
200000 statement-envelope bytes, 128-byte internal identities, and 256-byte partition
identities. Complete request `wire_json_bytes` pins serialization and request completeness only. Configuration/startup consumes every static,
independently reviewed operation maximum/one-beyond fixture and requires production builders and the
oracle to match its frozen maps, wire totals, conservative per-item/aggregate totals, action count, and
outcome. A request may be inside every scalar cap yet exceed an action or conservative aggregate formula;
it is refused before mutation with no split fallback. No cap is advertised as independently simultaneous
with a different profile.

The reviewed Stage-1 values reuse the adapter's existing 200000-byte limit and the
current fixture/catalog maxima where present; the remaining entries are reviewed Stage-1 contract
values. Implementation removes arbitrary source-local constants after all consumers use this object.
Infrastructure resources remain: `table-attempt-reconcile` is a DataV2-owned sparse GSI;
`stage1-plan-intent-ready` is the DataV2-owned `KEYS_ONLY` sparse GSI with partition key
`ready_status`, sort key `ready_order`, and membership only for pending intent `META` carrying the exact
ready fields; `ProviderAttemptReconciler`, `PlanIntentReconciler`,
`ProviderFenceRecoveryFunction`, invoke-only
`ProviderFenceRecoveryOperatorRole`, queue, mappings, and handoff provider are IngestionStack-owned;
Stage-1 authority remains indefinite with no TTL or sweeper. The reviewed fixed deployment values are:
`ProviderFenceRecoveryFunction` and the projection-handoff provider each at memory `512 MiB`, timeout
`60` seconds, reserved concurrency `1`; the two scheduled reconcilers at `512 MiB`/`120` seconds/`1` as
§§5 and 5.3 own; and the table writer at `tableWriterMemoryMiB=2048` with its §11 timeout and
concurrency.

> **Amendment, 2026-10-10 (write throughput).** Two §11 values changed; no other value did.
>
> 1. **`athenaMaxParametersPerStatement` (was `900`) is removed, and `athenaMaxStatementBytes`
>    is the only per-statement bound, measured on the canonical start envelope.** Athena's
>    `StartQueryExecution` documents no maximum count for `ExecutionParameters` (each entry is at
>    most 1024 characters) and a 262144-character `QueryString`. The 900 cap was this adapter's
>    own. At the derived tables' ~34-column grain it held ~26 rows per statement, so an AMC unit
>    paid ~3 `MERGE` statements for tens of rows. Each is its own Iceberg commit with a ~4 s p50
>    fixed cost: on dev on 2026-10-10, 3-merge units took a p50 of ~20 s against ~6.7 s for
>    1-merge units. Without a count bound, the byte bound must cover the parameters too. The
>    envelope is exactly what the start client sends and what the `ATTEMPT` item stores, so 200000
>    envelope bytes keeps `QueryString` under Athena's ceiling and the attempt item inside the
>    §3.2 400000-byte item bound. The §6 packer and the §5 step-2 validator use that one measure.
>    A record whose own merge exceeds it is refused before authority as `ATHENA_START/2`, never
>    truncated. A unit of tens of rows now fits one merge; the §2.1 feasibility arithmetic is
>    unchanged.
> 2. **`planIntentReconcileBatchSize` is raised from `10` to `50`.** The one-minute tick with a
>    query limit of 10 sent a fixed ~20 units/min and used ~2.3 s of its 120 s timeout (dev,
>    2026-10-10). Once the writer stops paying ~3 commits per unit, that tick would cap
>    throughput. At 50 intents a tick takes an estimated ~11 s, inside the timeout and the
>    discovery port's 1..100 limit. The per-intent reads and transactions are unchanged, and the
>    table-attempt reconciler batch stays `10`. Backlog still cannot grow silently: intents
>    admitted faster than the writer drains wait as semantic-write FIFO messages, where
>    `semantic-write-oldest` alarms once the oldest exceeds
>    `semanticWriteMaxReceiveCount × (timeout + margin)` = 2880 s, instead of waiting unalarmed in
>    the ready index. During a paced backfill that alarm can fire for queue depth rather than for
>    a stuck unit; this amendment does not change its threshold.

## 12. Decision provenance

[ADR-075 §6](../../decisions/adr-075-generate-page-artifacts-once-per-dependency-cohort.md) owns why
semantic projection is consolidated within a table, serialized across writers, and switched without
overlap. This document owns only the current mechanism and acceptance contract.

## 13. Acceptance for Stage 1

Measured under ADR-074 before/after windows on the same fixtures:

1. A maximum-size prepared delivery with exact projected counts and maximum constructor identity sizes
   passes deterministic encoded plan/unit/outbox/statement-intent/finalization bounds, including every
   ordinary lifecycle-head action and both refusal owner-condition variants. Queued and inline
   finalization each budget exactly one unit/run head action plus pointer `EVENT#` puts; no pointer-head
   key, read, condition, action, IAM member, or byte allowance exists. The proof includes one attempt
   settlement update and one Athena `USAGE#` put per maximum referenced accepted attempt in every final
   owner disposition. Plan-intent fixtures cover exact owner/plan-`HEAD` agreement, source
   acknowledgment, reservation CAS, every intent-creation path, and every ordinary or planned-
   replacement `PersistPlanRequest` map. Ordinary persistence is `<=20` actions; planned replacement
   has exactly one owner update and is `<=100` actions. Replacement strongly reads every predecessor
   plan unit/attempt, requires terminal byte-identical settlement, and conditions the predecessor plan
   `META` immutable roster digest plus complete settled-attempt count/digest; it performs no per-ATTEMPT
   settlement write. A separate maximum `CompletePlanRequest` proof contains exactly 51 actions and
   includes every owner/plan/intent/unit/attempt/event/head observation. The accepted simultaneous
   profile remains 15 units, 4 group members, 37 pointers/object references, 10 current-unit and 32
   total permanent attempt reservations, 200000 statement-envelope bytes, 128-byte internal
   identities, and 256-byte partition identities. Exact formulas remain `FinalizeUnit=100`, `FinalizeInline=99`, `FailUnit=26`,
   `SubmitPlanIntentRequest=2+u=17`, `ReschedulePlanIntentRequest=3`, attempt-33
   `FailUnit=7+2n+u=42`, and `CompletePlan=u+t+4=51`: generation assignment changes fields, not action
   families or counts. Claim still uses one owner update and one intent update plus optional active-plan/
   head condition checks; Persist retains its existing ordinary/replacement action envelopes. Pending
   intent `META` and intent-creation request maps omit generation; claim result maps add matching
   `reserved_generation`/`claimed_generation`; reschedule/requeue and admission-failure result maps
   remove `claimed_generation`; persistence maps condition both assigned fields and advance
   `last_generation` to that value. Attempt-33 successor intent `META` plus its unit
   puts replace one ordinary failure trigger; they do not append to it, and the transaction consumes
   the same owner action. The trigger and maximum-size intent-item puts have independent fixtures.

   For every real operation branch, static committed v1 fixtures independently own maximum and
   one-beyond typed inputs. `SubmitPlanIntentRequest` and attempt-33 `FailUnitRequest` have separate
   fixtures; attempt-33 independently fixtures its replaced trigger and each maximum successor intent
   `META`/`UNIT#` put rather than reusing ordinary failure or plan-persistence expectations. Exact
   strong pre-states, expected low-level request/result maps,
   `wire_json_bytes`, every pre/result `conservative_service_item_bytes`, per-target maxima, aggregate,
   action count, and outcome. The test-only reference checker computes those expectations directly from
   frozen maps without importing or calling production builders or `Stage1DynamoByteOracle`; production
   builders and the oracle must match. Acceptance rejects action 101, any pre-state or result above
   400000, any distinct-target maximum aggregate above 4000000, absent exact pre-state, duplicate target,
   unsupported/indeterminate values, and every maximum/one-beyond outcome mismatch before mutation.
   Common-mode tests omit or alter each expression, key, table name, operation member, condition,
   expression name/value, `ReturnValuesOnConditionCheckFailure`, `ClientRequestToken`, pre-state,
   resulting map, and other emitted member in turn and require failure. Fixture changes without an
   explicit reviewed fixture-version/design update fail. The `item-v1` and `transaction-v1` calibration
   vectors reproduce only the four §3.2 `wire_json_bytes` totals; neither vector is service-size or
   transaction-aggregate evidence. Complete request wire bytes are never accepted as proof of the AWS
   4 MB aggregate.

For every unit, fixtures permute caller object/map insertion order and still produce the same unsigned-
UTF-8 `(kind, reference)` object order, one existing typed `ordered_records` order over distinct grains,
rendered statement bytes, parameters, fragments, and merge count. A repeated grain with identical
remaining values and a repeated grain with differing values both reject before or during that sole
production ordering call. Distinct-grain order is stable. Exact duplicate object keys deduplicate
stably only when reference, checksum, publication identity, and encoded object authority are all byte-
identical; a difference in any field rejects. Fixtures also reject a one-record fragment over either
limit, caller tie invention, bin packing, backtracking, and co-packing a split fragment with either
adjacent object. No record canonicalizer or duplicate-record collapse is exercised or permitted.
Boundary fixtures prove maximal prefixes. The 37-reference clean pack uses at most nine merges plus one verification. The
complete accepted roster proves `Σ(M(u)+1)<=32` and every term `<=10`; a roster whose deterministic
clean sum is 33 refuses before authority even if every unit independently fits. Retry fixtures exclude
only byte-exact whole objects, while final verification covers every unit identity.

One-beyond fixtures refuse 16 units, 5 group members, 11 unit attempts,
33 intent attempts, 38 pointers, 38 refs, 200001 bytes, 901 parameters, and 129 identity bytes before any
lease, attempt, object, publication, or `CURRENT` mutation. They pin deterministic refusal for a
one-beyond `SubmitPlanIntentRequest` with 16 intent units, 102 actions for a one-beyond `FinalizeUnit`,
101 for a one-beyond `FinalizeInline`, and every ordinary or planned-replacement persistence map that
exceeds its respective 20- or 100-action cap. An in-range but jointly over-envelope fixture also
refuses with no split fallback. Any owner race forces a complete reread; only a frozen exact
`PersistPlanRequest` map may proceed. The accepted case cannot later receive a permanent
transaction-capacity refusal.
2. Every source object produces a source unit containing its envelope even with zero business rows;
   zero-row candidate/current canonical objects produce no canonical unit.
3. A zero-row successful publication accepts only the complete typed attempt-history observation whose
   append-only `_attempt_groups`/`map_attempt_history` reduction ends in the exact successful event for
   the same publication attempt, partition, and published instant. It persists the exact topology and
   pointer execution-source proof returned by
   [Operational Data-Flow Status § Stage-1 event projection](../operational-data-flow-status/low-level-design.md#stage-1-event-projection)
   while seeding authority/projected identity/token and the pointer event in the same transaction.
   Missing, truncated, reordered, partial, mismatched, or caller-supplied status proof fails before
   mutation; there is no fallback. Injected transaction failure leaves none of them; producer death after commit is
   recovered by stream or pending-event reconciliation without a plan key. Retries before delivery,
   after `DELIVERED`, and after a newer pointer appears preserve the original immutable topology,
   payload, pointer outcome, and delivery envelope.
4. `SubmitPlanIntentRequest` atomically puts one generation-independent intent `META`, all zero-to-15
   canonical intent `UNIT#` rows, and exactly one owner action: a condition check of the existing owner
   or optional absent-owner bootstrap. It accepts no generation or digest input and stores no generation.
   Fixtures pin the §3 roster/intent JSON, lengths, digests, unsigned-UTF-8 unit/object ordering, and
   exact `META`+`UNIT#` reconstruction. Exact complete-tuple replay reuses the intent. Independent
   negative fixtures vary grouping, unit key, table kind, merge count, object kind/reference,
   `content_checksum`, `publication_id`, null/non-null `partition_key`, `fencing_token`, root/source plan
   key, plan digest, origin kind, and origin source/failure transition; each semantic variation produces
   a distinct `intent_id`, while reordered/noncanonical or typed-drift input is rejected. A decisive
   race fixture starts at owner `last_generation=G-1`, commits two distinct pending intents from that
   exact snapshot, proves both generation-free authorities coexist, claims/persists one as `G`, then
   claims/persists the other as `G+1`; every intermediate observation has at most one reservation and at
   most one active/planned plan. Claim atomically stores
   `owner.reserved_intent_id=owner.reserved_intent_digest=intent_id` and
   `owner.reserved_generation=intent.claimed_generation=owner.last_generation+1`; Persist requires those
   exact equalities, derives execution/key through the §3 plan-materialization identity, advances
   `last_generation` to the assigned value, and clears the reservation.
   Exact claim/persist replay retains the assigned generation.

   Source-delivery acknowledgment is decided only by the §5.3 strongly consistent classifier. Tests
   inject every submit, claim, persist, send, admission, terminal-write, success, ambiguous-response,
   and process-death boundary—including death immediately after claim—and permit acknowledgment only
   when the read proves exactly one of: generation-free pending intent with both correct ready fields;
   claimed intent recoverably named by the owner reservation with equal assigned generation; admitted
   intent bound by the owner active pair to the exact candidate plan and assigned generation; or exact
   candidate `admission_failed` with the same atomic intent pending transfer, `claimed_generation`
   removed, owner active clear, lifecycle event, and plan `HEAD`. Before intent commit and on missing,
   partial, inconsistent, zero-outcome, or multiple-outcome readback, handling raises and leaves the
   source unacknowledged. A durable handoff may be replayed and then acknowledged; reconciliation remains
   independent after source DLQ. `PlanIntentReconciler` alone claims due pending intents. A stale
   claimant after a winning claim, reschedule, requeue, persistence, or owner advance cannot satisfy the
   exact revision/reservation/state/generation conditions, writes no old-generation bytes, and only
   returns the strongly reread winner/current state or fails closed.

   `PersistPlanRequest` atomically creates the plan and swaps the owner before queue send. Ordinary
   persistence is within 20 actions; planned-plan replacement has exactly one owner update and is within
   100 actions. Queue admission does not wait for status delivery. Send/admission and terminal-write
   return values never classify authority; every continuation follows the same strong read.
   `FailAdmissionRequest` atomically sets the candidate `admission_failed`, clears paired owner active
   fields, and changes the same intent `claimed→pending`, removes `claimed_generation`, and restores both
   correct ready fields. A requeue/reclaim fixture proves the failed generation remains plan history,
   the intent receives the then-current `owner.last_generation+1` on its next claim, no partially
   admitted candidate executes, and no successor intent is created.
5. Owner/intent acceptance exhausts the exact owner at
   `WRITE_PLAN#<plan_digest>/SUCCESSOR_OWNER`, intent partition
   `WRITE_PLAN#<plan_digest>#INTENT#<intent_id>`, sole `intent_roster_tuple` and
   `intent_authority_tuple`, claim-assignment rule, and internal execution tuple defined in §3.
   Fixtures pin monotonic `revision`; pre-root `last_generation=-1`; optional `root_plan_key`;
   `last_plan_key`; paired `active_plan_key`/`active_intent_id`; reservation trio with
   `reserved_intent_id=reserved_intent_digest=intent_id` plus `reserved_generation`; and
   `latest_internal_execution_id` matching plan `HEAD`, with no other claim or active-generation owner
   fields. Intent `META.state` is exactly `pending|claimed|admitted`; its origin is exactly
   `source_delivery|plan_attempt_budget` and immutable across every state transfer; pending has no
   `claimed_generation`; claimed and admitted require the exact assigned value; only pending carries
   `ready_status="ready"` and `ready_order=<20-digit due epoch-micros>#<intent_id>` while claimed and
   admitted carry neither ready field. Each `UNIT#<unit_key>` stores exactly one canonical
   unit entry; `META` plus the complete sorted unit set reconstructs the pinned authority tuple and no
   separate object digest exists. At most one plan `META` has `status=planned`, exactly matching owner
   `active_plan_key`.

   `ClaimPlanIntentRequest` atomically computes and stores the identical reserved/claimed generation
   while removing both ready fields; `ReschedulePlanIntentRequest` never changes generation while the
   intent remains claimed and removes it on retry before any later reclaim;
   `AdmitUnitRequest` changes the intent `claimed→admitted` only on full admission and retains its
   assigned generation with both ready fields absent; `FailAdmissionRequest` changes that same intent
   `claimed→pending`, removes `claimed_generation`, and records the exact retry schedule plus both
   correct ready fields; and `CompletePlanRequest` is the exact 51-action metadata-only completion.
   Failure injection at every transaction member leaves all effects absent, and exact replay changes
   nothing, including retaining any already-assigned claimed generation. Plan-authority fixtures derive
   the sorted `required_child_expectations` map from every unit flow to the exact plan generation,
   persist it with `child_outcome_reduction_version=2` on plan `META` and lifecycle authority, and
   reattest it from `META`/event without changing the public event wire. Every unit lifecycle authority
   copies `owner_generation` from the exact unit owner. `CompletePlanRequest` rejects a missing, null,
   prior, future, unsorted, extra, or mismatched map/generation and emits only the exact internal proof
   consumed by the status-owned parent gate. Joint acceptance with Operational Status starts from a
   successful G child head, delivers G+1 plan completion first, proves it remains awaiting, advances the
   same child flow head with G+1, and only then permits release; replay and both race orders converge.

   Attempt-33 preflight proves zero Athena SDK calls and no new Begin reservation. `FailUnitRequest`
   terminalizes the source plan and creates one deterministic origin `plan_attempt_budget` successor
   intent. Its `META` plus complete `UNIT#` puts replace the ordinary failure trigger rather than append
   to it. Trigger and maximum-size intent-item puts have independent byte/action fixtures; any
   over-limit maximum refuses before original root authority. The failed unit message acknowledges only
   after commit or exact replay, and intent recovery remains available after the unit reaches its DLQ. A terminal
   predecessor can produce only this exact successor, so replay cannot fan out. FIFO acceptance pins
   the exact §4 six-field canonical body JSON/length/digest, the two-member deduplication tuple and
   digest, and physical `unit_key` group. Phase-1 preflight encoding and reconciliation sending produce
   byte-identical values;
   exact resend collapses, while G and G+1 have distinct `plan_key`, body, and deduplication ID. Fixtures
   inject send-success/admission ambiguity and death before/after send or admission and resume only
   through the same constructor/classifier. Writer fixtures strongly read owner, plan, unit, origin
   intent, and head and reject extra/missing/changed fields, stale generation/body, replaced plan,
   mismatched admission, and cross-unit group before execution. Pending intent authority contains no
   generation or public unit execution/causation/operation identity.
6. For every merge/verification statement, local typed validation completes before Begin. Begin stores
   the exact envelope, permanent reservation, FENCE, and `current_attempt_id` before the dedicated start
   client runs with SDK `MaxAttempts=1`. Acceptance fixtures assert the public result is exactly
   `Accepted(query_execution_id, sanitized_evidence) | AcceptanceAmbiguous(reason, sanitized_evidence)`.
   A valid identity is exactly a String containing at least one non-whitespace character; every
   exception after Begin, malformed result, missing/wrong-typed field, or blank identity is ambiguous,
   with no service-error classification. Both retain all Begin authority. Live replay uses only the
   exact stored envelope/token and only before safety deadline; then it parks. Parked recovery uses only transactional
   `RecoverParkedAttemptSubmissionRequest`: the coordinator condition-checks the complete exact parked
   FENCE snapshot and updates the ATTEMPT in the same transaction to attach the identical valid identity
   and write-once submission instant. A concurrent FENCE-only reschedule/block/requeue aborts both
   effects and forces strong reread. Only that identity plus provider-terminal observation progresses.
   Golden schedule fixtures use one captured UTC nanosecond value, floor to epoch microseconds, integer
   addition, and 20-digit zero padding. They pin Begin count zero/due at lease expiry; requeue count
   zero/due at `requeue_at`; and old-count delays `c0=60`, `c1=120`, `c2=240`, `c3=480`, `c>=4=900`
   seconds with exact byte replay, ambiguous-reread reuse, no jitter, float, or overflow. Lambda tests
   capture monotonic `m0` and remaining `r0` immediately before Begin; pin
   `Q=m0+r0-120000`, `S=m0+r0-60000`, `I=m0+r0`; prove full-budget offsets
   `780000/840000/900000` ms, 120001 accepts with `Q+1ms`, and 120000 starts no attempt. Loop tests
   preserve terminal-first precedence in all four deadline cases. With a durable valid ID at `Q`, a
   returned terminal wins; otherwise cancellation persists before exactly the permitted Stop call, Stop
   success remains nonterminal, and FENCE remains set. With a durable valid ID at `S`, a returned
   terminal wins; otherwise the attempt parks with no new provider poll. Without a durable valid ID at
   `Q`, intent/reservation/FENCE/current attempt remain unchanged, cancellation and Stop/provider poll
   are absent, and only exact stored-envelope/token Start replay before `S` is permitted, with at most
   one Start call per invocation and replay allowed in a later invocation. Without a valid ID at `S`,
   the attempt parks with no cancellation, Stop, provider poll, or Start. Tests bound earlier sleeps and
   polls to Q/S and prove cancellation never clears FENCE. Operator tests reject absent/default/offset/non-Z/leap-second/excess-precision values,
   capture wall plus monotonic once, require `Q>now`, `Q+60s<=S`, `S+60s<=Iop=now+900s`, translate once
   to integer monotonic units, and run the same loop.
7. A provider attempt still nonterminal at the safety deadline leaves an indefinitely active/parked
   `FENCE`. Contract fixtures exhaust `core_valid`, the closed `core_invalid` complement, canonical
   pending/blocked reconciliation forms, and every `repairable_reconciliation_metadata` presence/type/
   value malformation. Core-invalid fixtures are injected only by a privileged out-of-contract fixture;
   each emits sanitized `CORRUPT_PROVIDER_CORE_AUTHORITY`, fails the invocation unhandled, and proves
   zero Athena/coordinator/block/case/requeue/clear/lease/owner effects. No ordinary recovery/liveness
   claim is asserted.

   Reconciler timing acceptance covers active-with-id terminal and nonterminal before/at lease expiry,
   active-without-id parking without Start, parked-without-id exact recovery Start, parked terminal
   completion/release, parked nonterminal before/at `submitted_at+780s`, candidate-local failures, and
   one provider observation per due candidate with no continuous poll. It proves Stop follows a durable
   due cancellation, nonterminal/failure paths reschedule once, terminal completion preserves the
   permanent reservation, and no path decrements any reservation counter.

   Every repairable metadata fixture constructs `MalformedReconciliationMetadataObservation`, conditions
   exact valid core and the malformed field snapshot, replaces only reconciliation fields with canonical
   blocked form, retains the malformed snapshot digest while removing partial audit, and creates the
   protected case atomically. Same-snapshot reschedule rejection uses its distinct typed observation.
   Tests prove reason/digests/block/ref/case are builder-derived, no valid old pending tuple is required,
   core bytes never change, later pending candidates continue, and ambiguous calls converge by strong
   reread. Each repairable fixture then lists the opaque case, requeues canonical block output directly
   to canonical pending with no correction step, resumes provider reconciliation, and reaches normal
   provider-terminal completion. Reblock receives a fresh case and retains prior audit.

   `ProviderFenceRecoveryFunction` is the sole blocked discovery/requeue path. Acceptance creates more
   than one result page, loses the reconciler after atomic block, and proves list output/logs disclose
   only opaque recovery ref, safe reason/time, and cursor. Requeue accepts only recovery ref, operation
   ID, and sanitized reason, resolves the canonical block output server-side, and changes only blocked
   reconciliation metadata to canonical pending plus case audit. Exact replay before consumption returns
   `already_requeued`; terminal/consumed replay rejects. Concurrent list/requeue and competing requeues
   permit one mutation. Pagination remains opaque and selector-free. Only provider-terminal
   `CompleteTableAttemptRequest` clears safety; acquisition and every queued/inline writer remain denied
   beforehand.

   Constructor/operation tests prove the induction: pre-switch reserved rows all recompute as exact
   canonical builder output; Begin creates
   `core_valid`; every authorized direct/transaction mutation preserves it; Complete atomically reaches
   terminal ATTEMPT plus clear FENCE; and no other role can write either reserved prefix. Exhaustive
   all-page pre-switch verification rejects partial reads, any non-canonical reserved row, or
   reconciliation attribute
   elsewhere, with no adoption. Timing/config tests enforce every §11 value and the exact UTC-
   microsecond schedule, reservation, Q/S/I, operator translation, and queued-visibility formulas in
   §5.
8. Synth assertions prove each coordinator statement attached to a runtime or operator role uses the
   exact publications/leases table ARNs, no wildcard resource or unconstrained leading key, and only
   underlying `GetItem`, `PutItem`, `UpdateItem`, and `ConditionCheckItem` actions in the minimum per-
   prefix/per-operation subsets. No coordinator IAM statement contains `TransactGetItems` or
   `TransactWriteItems` as an action. Direct strong `GetItem` is separate and has no enclosing-operation
   condition; transaction-read `GetItem` is enclosed by `TransactGetItems`; every transaction-write
   item action is enclosed by `TransactWriteItems`. The sole direct-mutation statement is exactly
   publications-table `UpdateItem` with `dynamodb:LeadingKeys=TABLE_ATTEMPT#*` and no
   `dynamodb:EnclosingOperation`; transitional delivery, table writer, and
   `SemanticProjectionOperatorRole` receive it only for live `RecordSubmittedAttemptRequest`.
   `ProviderAttemptReconciler` receives no direct mutation. Its
   `RecoverParkedAttemptSubmissionRequest` grant consists only of transaction-enclosed FENCE
   `ConditionCheckItem` and ATTEMPT `UpdateItem`. Credentialed positive acceptance proves the live
   request changes only an exact `intent` attempt's `query_execution_id`, `submitted_at`, write-once
   closed `sanitized_start_evidence` form, and `state`,
   while recovery atomically preserves the complete parked FENCE snapshot and changes only the exact
   parked ATTEMPT's absent recovered id/time/evidence triple. A concurrent FENCE-only reschedule/block/requeue aborts
   recovery. Denial acceptance proves cross-table/family access and operation swapping are unavailable;
   builder tests reject another sort key, field set, condition, state, stale pair, id/time, digest,
   token, holder, or fencing token before mutation. Cancellation-builder tests additionally reject
   absent, non-String, blank, or non-durable query identity before any transaction or Stop call; IAM
   grants do not substitute for that typed boundary. Direct use of every transaction-only write
   combination remains denied.
   Finalizer IAM/rendered-builder tests prove one legal `STATUS_FLOW#*` head member for the unit or run
   and no pointer-head member, key, condition, action, capacity allowance, or additional grant.

   Separate `dynamodb:LeadingKeys` conditions cover only `TABLE#`, `TABLE_ATTEMPT#`,
   `PROVIDER_RECONCILE_BLOCK#`, `WRITE_PLAN#`, `STATUS_FLOW#`, and each exact registered canonical-
   dataset prefix. `DataV2Stack` synth proves
   `table-attempt-reconcile` is `KEYS_ONLY`, indexes only active/parked FENCE rows with closed pending or
   blocked reconciliation state in separate partitions, and a deployment readback proves `ACTIVE`
   before pre-switch. `IngestionStack` synth pins table-writer timeout `900`, reserved and mapping
   maximum concurrency `2`, event-source batch `1`, queue visibility `960`, receive count `3`, lease
   `930`, cleanup reserve `60000`, reconcile batch/base/max `10/60/900`, drain `300`, and each consumer's
   exact §11 environment subset. It proves exactly one `ProviderAttemptReconciler`, one-minute schedule,
   `512 MiB`, `120-second` timeout, reserved concurrency `1`, repository ADOT export, and only the
   required resource plus §11 environment names. Its discovery statement is `dynamodb:Query` on only the exact GSI ARN
   and pending leading key; blocked query, table `Scan`, cursor/control item, `ExclusiveStartKey`, and
   every other index are denied. Its direct/transaction DynamoDB grants include only the named §5
   operations and the transaction-enclosed protected-case put for block; its three exact batch-workgroup
   Athena actions and exact statement result/data access remain limited to §5. Case reads/updates,
   blocked requeue, finalization/settlement, lifecycle delivery, mapping mutation, wildcard policies,
   and operator-role assumption remain denied. The same stack proves exactly one `ProviderFenceRecoveryFunction`, one same-
   account invoke-only `ProviderFenceRecoveryOperatorRole`, and the two exact typed config paths. The
   function alone has blocked-GSI `Query`, direct protected-case/FENCE/ATTEMPT reads, and transaction-
   enclosed exact case/FENCE/ATTEMPT requeue actions; pending query, `Scan`, direct mutation, semantic/
   plan/run/pointer/usage/lifecycle authority, Athena/Glue/S3/provider/completion/clear actions, and role
   assumption are denied. The operator role has only exact unqualified-function invoke and zero
   DynamoDB. Credentialed allow/deny tests exercise every granted action and reject wrong partition,
   key family, table, index, function qualifier, action, or enclosing operation. The status LLD independently grants its lifecycle producer `Query` on only
   `stage1-outbox-pending`; the two indexes and permissions are not interchangeable. Credentialed tests
   additionally prove the declared direct read, transaction read, exact-index query, Athena calls, and
   each complete multi-table coordinator write succeeds; wrong prefixes/tables/workgroup/index,
   omitted actions/resources, wildcard/unconditioned policies, and direct use of transaction-only write
   combinations are denied.

   `stage1-plan-intent-ready` synth/credentialed tests prove `KEYS_ONLY`, partition key `ready_status`,
   sort key `ready_order`, Query partition `ready_status="ready"`, sort bound
   `ready_order <= <20-digit now>#~`, and IAM `dynamodb:LeadingKeys=ready` on only
   `<publications-table-arn>/index/stage1-plan-intent-ready`. Fixtures prove only `state=pending` intent
   `META` carries no `claimed_generation`, carries `ready_status="ready"` plus
   `ready_order=<20-digit due epoch-micros>#<intent_id>`, and that claimed/admitted carry an exact
   `claimed_generation` with neither ready field while blocked carries neither generation nor ready
   field. They also prove exact owner and intent key forms, one `META` and maximum 15
   `UNIT#` rows per intent partition, `ACTIVE` readback of the exact key schema/projection before queued
   enablement, and denial for every other principal, index, partition value, or leading key.
   Exactly one `PlanIntentReconciler` has Query plus minimum
   `ClaimPlanIntentRequest`, `PersistPlanRequest`, `ReschedulePlanIntentRequest`, `AdmitUnitRequest`,
   `FailAdmissionRequest`, and queue-send grants. The phase-1 delivery role separately has only
   `SubmitPlanIntentRequest`; it cannot send a unit message, admit a unit, fail an admission, query,
   claim, reschedule, or persist an intent. The table writer can create the deterministic successor only
   through attempt-33 `FailUnitRequest`, whose transaction consumes the owner. Other principals cannot
   query, claim, reschedule, submit, or persist intents. The generation correction broadens no grant or
   action family: Submit still has intent `PutItem` plus exactly one owner `ConditionCheckItem` or
   bootstrap `PutItem`; Claim alone has the owner/intent updates that assign reserved/claimed generation;
   Persist alone may condition that assignment and advance `last_generation`; reschedule and admission
   failure may only remove `claimed_generation` through their existing owner/intent action rows.
   Positive and deny fixtures prove those exact field/action/enclosing-operation boundaries for every
   principal. Operation fixtures pin owner/plan-`HEAD` agreement, every creation origin,
   generation-independent pending item/request bytes, matching claim-time
   `reserved_generation`/`claimed_generation`, ordinary persistence within 20 actions, planned
   replacement with exactly one owner update within 100 actions, the two-distinct-intent same-snapshot
   sequence into consecutive generations, admission-failure requeue/reclaim, death immediately after
   claim, stale-claimant refusal, exact assigned-generation replay, the four-outcome strongly consistent
   source-delivery acknowledgment classifier at every send, admission, terminal-write, ambiguous-
   response, and process-death boundary, source-DLQ-independent reconciliation, same-intent admission
   retry, and denial of second-owner/intent/raw transaction access. They prove no acknowledgment before
   intent commit or on partial/inconsistent readback and allow a durable handoff to replay before
   acknowledgment.

   Start-client tests prove local validation precedes Begin, the dedicated SDK client has
   `MaxAttempts=1`, and its result union has only `Accepted` and `AcceptanceAmbiguous`. Only a String
   containing at least one non-whitespace character is a valid accepted query identity; every post-Begin
   exception, malformed result, missing/wrong-typed field, or blank identity is ambiguous and retains all
   reservation/FENCE/owner state. Tests make no provider-error classification assertion.

   Refusal tests pin precedence: core-valid active/parked or blocked FENCE always
   `TABLE_ATTEMPT_PENDING`; only clear/absent FENCE with another holder's unexpired lease is
   `TABLE_BUSY`; core-invalid is the hard failure; conditional races reread. At one captured `now`,
   queued visibility targets lease expiry for busy, `max(lease expiry,reconcile due)` for active pending,
   reconcile due for parked pending, and direct `990` for blocked. Golden signed-microsecond fixtures pin
   negative=`60`, zero=`60`, `+1µs=61`, `+929s=989`, `+930s=990`, and `+930s+1µs=990`. They prove the
   refusal event commits first; queued handling calls `ChangeMessageVisibility` once then returns
   unacknowledged, while inline performs no SQS call. Success, failure, and ambiguous-response fixtures
   preserve the public reason; failure/ambiguity propagates `TABLE_WRITER/3` unhandled and performs no
   zero-visibility compensation, acknowledgment, unit failure, owner mutation, or fence mutation.

   Synth identity assertions prove each Lambda receives only its §3.2 operation row and that the
   lifecycle producer receives no coordinator operation. Unit/integration suites exercise
   `SubmitPlanIntentRequest`, `ClaimPlanIntentRequest`, `PersistPlanRequest`,
   `ReschedulePlanIntentRequest`, `AdmitUnitRequest`, `FailAdmissionRequest`, `ClaimUnitRequest`,
   `FailUnitRequest`, `CompletePlanRequest`, and every `PersistInlineRunRequest`, `RecordStage1RefusalRequest` unit/inline tag,
   `AcquireTableFenceRequest`, `ReleaseTableFenceRequest`, `BeginTableAttemptRequest`, live
   `RecordSubmittedAttemptRequest`, reconciler-only transactional
   `RecoverParkedAttemptSubmissionRequest`,
   `RequestAttemptCancellationRequest`, `ParkAttemptRequest`, reconciler-only
   `BlockTableAttemptReconcileRequest`, recovery-function-only
   `RequeueBlockedTableAttemptReconcileRequest`, `RescheduleTableAttemptReconcileRequest`,
   `CompleteTableAttemptRequest`, `FinalizeUnitRequest`, `FinalizeInlineRequest`, metadata-only
   `CompletePlanRequest`, and final-disposition expansion of `SettleStage1AthenaUsageRequest` only from
   its named owners. `ClaimUnitRequest` fixtures pin active plan-unit keys, exact
   owner-active/plan-`HEAD` agreement, planned plan `META`, admitted origin intent, unit generation/state,
   writer receive identity, queued claim, failed redrive, one-time `failed_count` decrement, lifecycle
   event/head actions, transaction IAM members, byte-oracle map, exact replay, and race winner; they
   prove the lifecycle decrement never changes permanent reservation counters. Transactional parked-recovery fixtures pin
   the complete parked FENCE condition plus ATTEMPT update and every FENCE-only race. The suites reject
   malformed sort keys/fields, cross-owner tuples, wrong states/ordinals, changed submission
   ids/times/digests/tokens, mismatched holder/token/attempt fences, category/payload changes, duplicate
   usage authority, and any caller-supplied transaction fragment before the SDK; every transaction in
   §§2–10 is accounted for by a typed builder. Credentialed
   refusal tests prove the unit tag has only the required `WRITE_PLAN#*` unit/`META`
   `ConditionCheckItem` members and the inline tag only the required `TABLE_ATTEMPT#*` run condition;
   removing either grant is denied, and neither tag receives an owner `UpdateItem`.

   `IngestionStack` creates exactly one `SemanticProjectionOperatorRole` with same-account trust and
   only the inline operator operation row plus the existing exact semantic service-resource shapes and
   raw `s3:GetObjectVersion` on the three source roots. It receives no provider-reconcile GSI/case read,
   blocked FENCE/ATTEMPT recovery, or requeue operation. Separate recovery-bootstrap tests require the
   exact list/requeue modes and arguments, reject default/implicit identity before client construction,
   and prove ambient credentials create only STS/SSM/CloudFormation clients; read exactly the two
   recovery paths plus the typed publications-table path; describe exactly DataV2 and Ingestion; verify
   unique account/partition/Region/stack/resource ownership; and assume the exact invoke-only role for
   900 seconds. The assumed session creates only Lambda and invokes only the verified unqualified
   function. Tests reject ambient invoke/table access, a second role, missing/renamed/duplicate paths,
   malformed/foreign/qualified ARNs, selector exposure, resource guessing, and fallback. Bootstrap
   deployment tests enable the reconciler only after broker/case-builder deployment and prove no blocked
   FENCE lacks its exact protected case; malformed or legacy rows block enablement without adoption.
   Inline-operator tests verify every resolved parameter, raw/data resource, and role/parameter owner;
   reject blank, renamed, foreign-account/partition, or unowned values; and prove the ambient session
   cannot create or use semantic clients. `ingest_delivery` parser acceptance requires both `--key` and `--version-id` and
   rejects neither, either alone, and predecessor lineage without both before any client exists. Tests
   prove no accepted path calls `ListObjectsV2` or `HeadObject`, the operator role has no raw
   `ListBucket` or unversioned `GetObject`, and each assumed-session raw read calls `GetObjectVersion`
   with the supplied version. Bulk-backfill acceptance consumes an externally reviewed exact pair list
   and invokes the same single-manifest mode once per item; it has no discovery fallback. Ingestion
   Lambda and fixture landing behavior remains unchanged. After assumption, exact-version raw reads and
   every semantic S3/Athena/Glue/Dynamo/coordinator call use the returned temporary session. Wrong
   account, untrusted caller, unauthorized identity, failed assumption, or an expired session produces
   no semantic access or mutation. Two deliveries to one table then produce zero
   `ICEBERG_COMMIT_ERROR`; queue and inline paths contend on the same typed lease/fence protocol.
9. Clean first-attempt statements equal `Σ merges + one verify per unit`.
   `CompleteTableAttemptRequest` requires a durable `valid_query_execution_id` plus provider-terminal
   observation, records exact known/missing quantities, advances owner reference/ordinal, clears the
   matching FENCE, and never changes permanent reservation counters or chooses a usage category.
   Category mapping and precedence are tested only by reference to
   [Data-Flow Completion and Service Levels § Stage 1 usage attribution](../../concepts/data-flow-completion-and-service-levels.md#stage-1-usage-attribution);
   Load Path does not reproduce that matrix. Acceptance imports its mixed accepted roster,
   recovery/redrive, and exact replay scenarios: each final owner-disposition transaction settles every
   referenced terminal accepted attempt under that owner; recovery/redrive attribution follows the
   concept; byte-exact replay emits no second authority and changed category/payload conflicts.
   `CompletePlanRequest` requires active-owner equality, admitted state, full attestation, zero failures,
   and already-settled attempt bytes, then writes only completed META, terminal event, owner/head
   agreement. Plan-intent replacement likewise requires every attempt already terminal and settled and
   performs no settlement. Begin tests prove local invalid input creates no reservation; every completed
   Begin permanently increments UNIT/META or RUN once, exact replay never increments, and accepted,
   ambiguous, succeeded, failed, cancelled, retry, and pre-read paths never decrement. Attempt-33
   preflight makes no Athena call and creates the independently recoverable intent through the existing
   unit failure/settlement transaction. Missing provider quantities remain immutable unavailable
   evidence under the concept owner; no late delta or reconciliation authority exists. Stage 1 creates
   no Lambda, queue, or storage per-flow contribution.
10. Killing a writer before finalization leaves neither servable advance nor attestation; after
    finalization it leaves both. Every queued, inline, and zero-row cell persists the complete pointer
    identity/topology/source proof returned by
    [Operational Data-Flow Status § Stage-1 event projection](../operational-data-flow-status/low-level-design.md#stage-1-event-projection),
    without reproducing or accepting alternate source values here. Retry matrices are exercised from
    that owner contract. Queued finalization advances only the unit head, inline finalization only the
    run head, and pointer events remain headless.
11. A current unit facing an equal/newer pointer omits it and attests. A pointer race re-reads and
    retries without regression or unit failure. Lease, pointer, unit, `META`, headless pointer event,
    unit/run event, and the single legal unit/run head effect are one transaction within the pre-proved
    pointer bound. IAM, builders, and tests reject any pointer `HEAD` read, condition, action, or key.
12. Failing one unit never acknowledges unfinished siblings. Siblings attest independently; DLQ
    redrive changes the failed unit back to writing; completion requires full admission, every unit
    attested, and `failed_count = 0`.
13. Handoff acceptance executes the complete §2.5 contract. Synth pins the sole public
    `projectionHandoffState`, six resolver rows, exactly one provider and the three prepare/commit/guard
    resources, closed properties and `Fn::GetAtt` seam, effective-token physical IDs including retained
    replay IDs, application/provider dependency sets, unchanged `DEPLOY_ORDER`, DataV2 `RETAIN`, and
    Ingestion `DESTROY`. Golden-byte tests pin request/old-request validation, candidate stack-update/
    transition and decommission hashes, every response `Data` object, incoming-ID/no-Data Delete
    responses, all five STATE dispositions, and all six mutating audit phases. State/callback tests
    exhaust same-state replay, all legal/illegal edges, concurrent CAS, bootstrap, different-stack
    recreation, old replacement Delete, delayed stale callbacks, current inconsistency, and exact
    rollback snapshots without revision reset.

    Replay acceptance starts after an ordinary transition and separately after different-stack
    recreation. It pins prepare and commit physical IDs to the pre-replay `ACTIVE.last_transition_id`;
    pins byte-exact prepare `Data` to replay mode, retained token, `fromDisposition=ACTIVE`, active
    `fromState`, and active `expectedRevision`; and pins byte-exact commit `Data` to that retained token,
    active state/revision, and replay outcome. It snapshots the complete `STATE` item and audit keyspace
    before and after both callbacks, proves zero mutation calls and stable state/revision/token, and
    proves delayed fresh-candidate callbacks cannot mutate, persist, or return their candidates. From
    each replay baseline, acceptance executes replay→full delete and replay→interrupted
    `DECOMMISSIONING`→retry→`DECOMMISSIONED`; prepare and commit Delete guards must both match the
    replayed `ACTIVE.last_transition_id`, official Delete responses echo the incoming physical ID with
    no `Data`, and stale old replacement Deletes after transition are byte-stable no-ops.

    Credentialed failure injection exercises state-before-audit recovery; before/after prepare and
    commit; controlled-resource rollback; bootstrap deletion; failed recreation; guard, commit,
    application, and prepare deletion boundaries; interrupted deletion; completed decommission; and
    recreation followed by deleted-stack callbacks. It reads back every claimed state/audit and rendered
    control, proving `ABSENT`, restored `ACTIVE`, truthful `DECOMMISSIONING`, final
    `DECOMMISSIONED` without an active-state claim, prior+1 revision, and recreated `INLINE_ACTIVE` or
    `QUEUED_ACTIVE` under a different StackId. Stack success, synth, or one snapshot is not behavior evidence. IAM acceptance
    permits only exact-table/leading-key `GetItem|UpdateItem` and bootstrap-only `DeleteItem`, and denies
    Put/Query/Scan/batch/transactions, mappings, other services, wildcards, or another principal. The
    Drain acceptance starts only from a strongly read all-clear, denies at 299 seconds, permits at
    exactly 300 seconds after final all-clear, and separately resets the timer for queue, DLQ,
    execution, plan, attempt, pending-fence, blocked-fence, incomplete-read, and failed-read evidence.
    Existing no-purge, no-supersession, and no-overlapping-writer cases remain required. The complete
    structured reports of every `verify-pre-switch`, `verify-edge`, and `verify-drain` run are
    themselves retained
    acceptance evidence: pre-switch enablement acceptance (including the `stage1RecoveryEnabled`
    false→true redeploy) requires the passing `verify-pre-switch`
    report (including its `DescribeTable` GSI proofs and all-page results), each forward edge requires
    its passing `verify-edge` report, the
    `QUEUED_DRAIN→QUEUED_QUIESCED` acceptance requires the passing `verify-drain` report, and the
    acceptance record must surface as a violation any such transition deployed without its retained
    passing report.
14. Every transitional-delivery, ingest-operator, and reprocess-operator run uses
    `PersistInlineRunRequest` with its closed writer kind and no caller public identity or topology. The
    builder consumes identity, topology, pointer source, table-kind mapping, and surface/status meaning
    only from [Operational Data-Flow Status § Stage-1 event projection](../operational-data-flow-status/low-level-design.md#stage-1-event-projection),
    then atomically persists run, event authority, and head before the first statement. Exact replay
    requires byte-identical owner/event/head and immutable roster. Provider terminal observation leaves
    the run resumable until lease-held finalization settles all referenced attempts and commits pointers.
    All owner, attempt, lifecycle, usage, head, and fence authority is retained indefinitely; delivery or
    block removes only sparse keys and preserves immutable payload.
15. Claim and lease-acquisition lifecycle authorities prove only their corresponding durable owner
    transitions; their public meaning is tested through the linked status mapping, not redefined here.
    Neither can be used as provider-work or servability evidence.
16. Every plan owner/intent, unit, inline-run, and pointer transition persists exactly the event/head
    effects required by its linked status contract in the same owner transaction. `PersistPlanRequest`
    and `PersistInlineRunRequest` accept no caller status definitions; finalizers and zero-row commit
    consume the linked pointer/topology proof. Owner-swap replacement is the sole two-event transaction
    and advances the head once `N→N+2`. Exact replay requires byte-identical owner state, event authority,
    and exact-or-later contiguous head proof. The pre-switch gate rejects missing or drifted proof rather
    than adopting it.

    Delivery block fallback is acceptance-tested independently for plan owner/intent, unit, inline-run,
    and pointer lifecycle authority. Each malformed or unprojectable authority becomes a retained,
    sanitized blocked outbox through the status-owned operation; unrelated authorities continue; only
    the separately authorized byte-identical requeue path may resume it. No family may disappear,
    spin outside durable state, borrow another mapping, or mutate owner/provider safety while blocked.
17. Queued, plan-terminal, and inline spans/logs carry only their owner-persisted public proof plus the
    executable contract's allowed internal locators. Lexical trace parentage never rewrites public
    topology. Tests prove unit and plan records do not inherit one another's identities, internal SQL/
    rows/provider quantities are absent, and all registry/status field meanings are validated only by
    the linked status owner.

## 14. Risks and open questions

- Physical table count may exceed `tableWriterConcurrency=8`; changing the reviewed value requires
  measured wait-time evidence and cannot weaken per-table fencing (the 2026-09-21 raise from `2`
  carried the wide-window measurement as that evidence).
- FIFO deduplication is not the idempotency authority; durable admission and unit state are.
- Authority may lead servability until current-table finalization; readiness measures that gap.
- Changing any reviewed §11 identity, item, group/member, pointer, or transaction bound requires updating
  the prepublication encoder proof before deployment.
- Attempt records add bounded coordination writes but prevent failed, cancelled, and timed-out Athena
  work from disappearing from usage evidence.
