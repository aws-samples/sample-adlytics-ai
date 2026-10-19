# ADR-073: Project all data movement into an operator status control plane

Date: 2026-09-19
Status: Accepted — development observation-only rollout authorized; evaluated SLO activation remains gated

## Context

Repository-owned data moves through several independent asynchronous surfaces: producer registration,
manifest landing, Semantic V2 ingestion and publication, preset publication, insight precompute and
continuation, evaluation delivery and result linkage, and agent-quality processing. Each surface has
some combination of immutable authority, queue state, attempt records, lifecycle logs, metrics, and
traces, but there is no complete operator view. An operator must know stack topology and correlate
separate AWS consoles to answer whether work was observed, queued, running, retrying, blocked,
completed, or lost.

The measured ingestion drain on 2026-09-19 exposed the operational consequence. Queue depth showed
work moving, structured lifecycle records showed completed deliveries, and the exact provider
diagnostic showed an Iceberg commit collision. No single interface joined those signals or explained
that the event-source mapping had been disabled while two invocations were still finishing. Waiting
on process existence or queue depth alone would have reported false progress or false completion.

A generic log-search API would not solve this. Logs and traces are diagnostic evidence with finite
retention, not lifecycle authority. Existing ingestion, evaluation, and continuation queues can contain
protected or customer-derived content and must not be inspected by a status projector. A dedicated
status queue can carry only the closed content-free lifecycle contract, but it is still a finite
transport buffer rather than permanent history. Copying every durable record into a second database
would create competing authority. Completeness and operator usability therefore have to be designed as
contracts rather than inferred from available telemetry.

## Decision

**Create a content-minimized operational data-flow control plane whose closed surface registry covers
every repository-owned asynchronous movement path, whose dedicated FIFO status queue feeds a derived
and reconcilable projection, and whose protected API is self-describing for operators.**

1. **A closed registry defines completeness.** `contracts/operations/data-flows.yaml` will declare
   every repository-owned movement surface, its owner, trigger, workload transport, durable authority,
   lifecycle vocabulary, correlation keys, status producer, expected telemetry, reconciliation reader,
   freshness policy, and operator runbook reference. A build check will compare the registry with
   deployed EventBridge targets, workload and status SQS event sources and DLQs, DynamoDB stream
   consumers, scheduled dispatchers, and asynchronous continuation/enqueue adapters. Existing
   EventBridge resources remain registered workload surfaces; EventBridge is not the status-event bus.
   An unregistered surface or a registry entry with no implementation will fail the check, so “all
   flows” is falsifiable.
2. **External extraction is an explicit boundary, not a silent gap.** AMC/Vendor extraction remains
   outside the application. The registry includes one boundary entry stating that repository
   visibility begins when an immutable landing or registration is observed. The API reports
   `external_boundary` rather than implying visibility into work the deployment does not perform.
3. **Authority stays where it is.** Raw S3 versions, registration status, append-only attempts,
   fenced publications and `CURRENT`, artifact pointers, insight cache, evaluation outbox/audit
   companions, and quality records remain the only authorities for their facts. Operational events
   contain operator-safe identities and bounded state only. The projection is disposable/rebuildable
   in meaning, even when retained for operator history; it can never authorize serving, publication,
   retry, redrive, or deletion.
4. **Every logical activity has an operator-safe identity.** `flow_id` is a canonical UUID for one
   logical ingestion, replay, recovery, publication, evaluation, or other registered movement activity.
   A retry keeps `flow_id`, creates a new `execution_id`, and names `predecessor_execution_id`. Every
   transition has an immutable `event_id` and names `predecessor_event_id` within the flow. A child
   activity names `parent_flow_id` and the exact `causation_event_id` that created it. Random UUIDs are
   allocated before the corresponding authoritative write and persisted with its content-free companion;
   adapters for pre-existing authorities derive deterministic UUIDs from a versioned namespace and an
   internal authority digest so reconciliation reproduces the same identity. UUIDs are safe API handles,
   not authority or provider selectors.
5. **Queue messages are content-free and idempotent.** `OperationalStatusQueueMessage.v1` is a closed
   tagged union. `OperationalDataFlowEvent.v1` carries the lifecycle UUIDs, registered measurement and
   optional objective context, dependency context, child registration, surface/stage/state, occurred-at, bounded progress,
   authority kind/digest, latency fields, an optional categorized usage contribution, and a closed
   reason code. `OperationalDataFlowUsageContribution.v1` carries one separately idempotent
   contribution identity, existing flow/execution/objective references, exactly one populated category
   in the shared `UsageByCategory.v1` shape, and bounded operational quantities; it records later work without creating another lifecycle event or
   objective sample. `OperationalDataFlowSurfaceObservation.v1` carries one registered surface,
   monotonic observation identity/sequence, aggregate transport, compute, and provider-latency signals,
   evidence completeness, and a closed reason code. No arm has a generic payload or details map, and no
   arm contains source rows, measurements, workload queue bodies, object keys, SQL,
   campaign/product/customer identity, prompt/question/answer/evaluation content, credentials, raw
   provider diagnostics, AWS identifiers, trace IDs, query IDs, publication IDs, or attempt IDs.
6. **Status producers send directly to one dedicated FIFO SQS queue.** After exact authority
   re-attestation, a component or transactional-outbox dispatcher sends the closed queue union directly
   to the encrypted status queue. Lifecycle events use `MessageGroupId=flow_id` and
   `MessageDeduplicationId=event_id`; usage contributions use the same flow group and their own
   `contribution_id`; aggregate observations use one ordered group per surface and their deterministic
   observation ID. Producers receive exact `sqs:SendMessage` access to that queue only
   and serialize transitions for each flow before sending. The queue has an explicit retention,
   visibility timeout, receive bound, and dedicated DLQ; these are transport recovery controls, not
   lifecycle history. The projector uses partial-batch failure reporting and acknowledges only
   conditionally persisted events. A valid successor whose predecessor is absent is durably parked in a
   bounded per-flow pending slot and acknowledged rather than blocking the FIFO head; applying the
   missing predecessor drains the contiguous parked sequence conditionally. Invalid, conflicting, or
   over-bound gaps fail and alarm. DynamoDB conditions and durable idempotency remain correctness
   controls because SQS deduplication is not permanent history.
7. **Project only after the durable transition.** A direct post-commit send alone is not sufficient:
   a crash between authority commit and `SendMessage` would create a silent gap. A DynamoDB-owned
   transition uses a content-free outbox record in the same transaction and a dispatcher sends it.
   Existing minimal notifications are reused where they can be re-attested: S3 Object Created through
   the existing ingestion EventBridge route, registration terminal state, publication `CURRENT` stream,
   audit KEYS_ONLY stream, and pointer transitions. Other owners add a transactional companion or an
   exact-read adapter. No EventBridge status bus is introduced.
8. **Reconciliation closes event gaps.** A bounded scheduled reconciler compares per-authority cursors
   with each registered owner and sweeps only live `PENDING` and `AWAITING_CHILDREN` indexes so chains
   and parent completions cannot remain stranded. Applying a pending event conditionally removes its
   live index entry while retaining immutable receipt evidence. The reconciler sends missing
   content-free events through the same direct FIFO path with their original grouping and deduplication
   identities and invokes the same conditional reducer for already parked work. A missing predecessor can therefore be
   applied ahead of its already parked successor; it is never enqueued behind a retry-blocked FIFO head.
   An unreadable authority, over-bound pending gap, stream-position loss, transport-retention expiry,
   deployment replacement, or missing logs projects `unknown` rather than silently becoming `healthy`.
9. **Transport health is aggregate.** Workload queue depth, in-flight work, oldest age,
   receives/deletes, retry/DLQ counts, Lambda errors/throttles/concurrency, and provider latency remain
   aggregate surface signals. Per-flow projection writes create idempotent contribution records in
   sharded time buckets; a separate reducer derives surface summaries from those commutative
   contributions. Content-free aggregate observers send the closed surface-observation arm through one
   FIFO lane per surface; its monotonic source sequence advances the latest transport/compute/provider
   snapshot. Independent `flow_id` groups never contend on one ordered surface cursor or update a
   shared aggregate in the flow transaction. The projector reads only the dedicated content-free status
   queue; it never reads a workload queue or any DLQ body.
10. **A dedicated operator entitlement protects access.** The API uses the existing HttpOnly cookie
    BFF and custom authorizer but introduces `operations-status-read`, sourced from a dedicated Cognito
    group. It is not implied by prompt administration or quality-review access. The API Lambda has
    read-only access to the operational projection only—never queues, logs, traces, raw storage,
    analytical tables, or protected evaluation stores.
11. **Snapshot REST is authoritative for V1.** `GET /api/operations/data-flows` returns the current
    summary and closed surface catalog; `GET /api/operations/data-flows/{flow_id}` returns one exact
    bounded snapshot; and `GET /api/operations/data-flows/{flow_id}/events` returns cursor-bounded
    transitions. Owner interfaces that initiate work return `flow_id` plus `status_url`. Responses are
    versioned and `no-store`. Polling is the initial browser and CLI mechanism. If measured freshness
    later requires push, one-way SSE may announce invalidation; reconnect always reloads the REST
    snapshot. Browsers receive no direct EventBridge, SQS, or other AWS transport access, and WebSockets
    are not introduced.
12. **The API teaches its own use.** Every response carries observation time, evidence completeness,
    freshness, state, closed reason code, served plain-language reading, and one closed next-action code.
    `GET /api/operations/data-flow-definitions` returns the registered surface/stage/state vocabulary,
    UUID field and relationship semantics, and operator-safe runbook links. The operator CLI exposes
    `flow status`, `flow watch`, and `flow explain` over the same REST contract. The UI and CLI render
    server-owned readings; they do not infer health or map internal errors.
13. **Completion and measurement semantics are shared.** Under
    [ADR-074](adr-074-define-authority-backed-data-flow-completion-and-service-level-objectives.md), an
    `observation_only` surface may project authority-backed lifecycle, completion outcome, continuity,
    descriptive measurements, and usage without targets or attainment results. An `evaluated` surface
    may additionally project objective and SLO results only after its registered objective activates.
    Neither mode may derive completion from transport state, and observation mode may never substitute
    descriptive performance for a healthy/unhealthy claim.
14. **Diagnostics remain separate.** The operator-safe `flow_id` is the only status-to-diagnostics join.
    Authorized logs and traces carry that identity beside their internal trace/query/provider identifiers;
    raw provider messages and infrastructure names do not cross the API. The status API neither invents
    nor returns a second diagnostic reference. Metrics and traces explain latency; durable authority
    proves lifecycle state.

## Consequences

Operators gain one consistent answer to “where is the data, what is it waiting on, what evidence
supports that state, and what should I do next?” without AWS credentials or knowledge of internal
resource names. Every initiated activity returns or deterministically acquires a UUID that supports an
exact status lookup. The closed registry prevents new queues, streams, or dispatchers from becoming
invisible. Reconciliation makes event loss observable and permits the read model to be rebuilt.

The capability adds a dedicated entitlement, encrypted FIFO status queue and DLQ, retained operational
history, disposable projector/reconciler compute, a protected read API, registry/checker generation,
and operator documentation. The status queue has finite transport retention; the DynamoDB projection
owns bounded queryable history, while underlying authorities remain the only source of lifecycle truth.
Because the projection contains operational metadata, its retention and export remain explicit even
though it contains no customer rows or protected text. Only the defined operational UUIDs cross the API;
exact volumes and authority, provider, infrastructure, customer, publication, attempt, trace, and query
identifiers remain excluded.

Development observation-only implementation is incremental by surface. Each active slice may expose
measurements while every absent or planned surface keeps registry and overall coverage explicitly
incomplete. No observation-only release may emit SLO attainment, target alarms, or a complete-coverage
claim. Evaluated activation remains separately gated by ADR-074 objective acceptance; even then, release
is not allowed to claim complete coverage until every registry entry is either `active` or explicitly
`external_boundary`.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| Let operators use AWS consoles | Requires topology knowledge, joins no lifecycle authority, and cannot enforce complete surface coverage. |
| Build a log-search API | Logs are finite diagnostic evidence, may contain protected content, and cannot reconstruct missed transitions reliably. |
| Route status events through EventBridge before SQS | Adds routing infrastructure without a second status-event consumer; direct FIFO send gives per-flow ordering, explicit backpressure, and one durable retry boundary. Existing workload EventBridge routes remain unchanged. |
| Expose an AWS event transport directly to the browser | Requires browser AWS credentials and leaks raw infrastructure or source-event identity. |
| Copy every authority record into one database | Duplicates customer/protected content and creates competing lifecycle authority. |
| WebSocket-first dashboard | Adds bidirectional connection state without a demonstrated requirement; status is server-to-client and snapshot-oriented. |
| Best-effort event after every write | A crash between authority commit and event send creates a silent permanent gap. |
| Implement only ingestion first without a complete registry | Delivers a useful page while preserving the original blind spots; incremental implementation is allowed only beneath a complete declared registry. |

## Review trigger

Revisit when a new asynchronous surface, authority family, operator class, service caller, mutation
operation, protected-content requirement, cross-account aggregation requirement, status-event consumer,
or sub-poll-interval freshness objective is proposed.
