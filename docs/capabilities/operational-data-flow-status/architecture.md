# Operational Data-Flow Status — architecture

This companion owns component boundaries and runtime flows. The [capability overview](README.md) owns
the black-box interface; the [low-level design](low-level-design.md) owns exact contracts and acceptance;
[ADR-073](../../decisions/adr-073-project-all-data-movement-into-an-operator-status-control-plane.md)
owns the status decision rationale; [ADR-074](../../decisions/adr-074-define-authority-backed-data-flow-completion-and-service-level-objectives.md)
and its [concept](../../concepts/data-flow-completion-and-service-levels.md) own the completion,
measurement, and SLO semantics this projection enforces.

## 4. Internal structure

The capability separates transition authority, content-free status production, FIFO transport, derived
projection, and operator presentation. Producers never write the browser projection directly. A
producer or transactional-outbox dispatcher re-attests the owning authority and sends the closed status
event directly to the dedicated FIFO queue. The projector consumes only that queue. A reconciler makes
missed notifications and stale projection cursors visible by publishing repairs through the same queue.
Existing workload transport—including S3 EventBridge delivery into ingestion—remains outside this
status transport boundary and is unchanged.

**Component diagram — Operational Data-Flow Status direct FIFO projection and operator API; authority viewpoint, component depth.** Content-free producers and reconciliation publish directly to one dedicated FIFO queue, an idempotent projector maintains the operational read model, and an independently entitled API reads only that model.

![Component diagram: Operational Data-Flow Status direct FIFO projection and operator API. Status producers re-attest movement authorities and send content-free events directly to a dedicated FIFO SQS queue. The queue invokes an idempotent projector and redrives exhausted messages to a dedicated dead-letter queue. The projector conditionally updates a DynamoDB operational projection. A separately entitled operator calls a protected API that reads only that projection. A scheduled reconciler sends repairs through the same queue.](diagrams/flow-status.svg)

> **Takeaway.** SQS is only the content-free delivery buffer: conditional projection and authority reconciliation preserve correctness, while the protected API remains isolated from transports and source data.

The FIFO queue, its dedicated DLQ, and projector/reconciler compute belong to a disposable processor
stack; queue retention and redrive policy are explicit deployment settings. The operational projection
has a retained history lifecycle and belongs in a dedicated retained store stack. The route Lambda
remains in `ApiStack`. Cross-stack resource names use typed SSM parameters; no construct reference or
CloudFormation export crosses a stack boundary.

## 5. Key runtime flows

### Transition projection

1. A registered movement owner commits durable state and persists the status UUIDs in the same
   content-free companion or outbox when transactional coupling is available.
2. The status producer re-attests that authority and sends the closed queue union directly to the FIFO:
   lifecycle events and usage contributions use `MessageGroupId=flow_id`, while content-free aggregate
   workload observations use one ordered group per registered surface. Lifecycle events deduplicate by
   `event_id`; usage contributions deduplicate independently by `contribution_id`.
3. The SQS event source invokes the projector. It validates schema and registry versions. A lifecycle
   event is idempotently applied or parked through the per-flow predecessor contract; a usage
   contribution conditionally appends to the existing flow/measurement and optional objective usage
   aggregate without advancing
   lifecycle; a surface observation conditionally advances only its monotonic aggregate-observation
   snapshot.
4. Applying a missing predecessor drains the now-contiguous parked sequence and advances the flow
   snapshot. Each applied lifecycle event also writes one idempotent sharded surface contribution; each
   accepted usage contribution updates its categorized resource-unit aggregate. Separate commutative
   reducers derive summaries without imposing cross-flow order.
5. The projector acknowledges only durably applied, parked, or exact-idempotent messages. Invalid,
   conflicting, or over-bound lifecycle gaps and invalid usage/observation records retry and may reach
   the dedicated DLQ; they never overwrite the projection or imply lifecycle completion.

### Operator read

1. The cookie authorizer verifies the session and exact `operations-status-read` route entitlement.
2. The route performs a bounded summary read, one exact canonical `flow_id` UUID read, or one
   cursor-bounded event query from the projection.
3. The server returns only the operational flow/execution/event/parent/causation UUIDs plus registered
   lifecycle, completion outcome, continuity, measurement-version observations, optional
   objective-version results, evidence, reason, reading, and next-action fields with `no-store` and
   observation/freshness state.
4. The browser or CLI renders the response verbatim and polls; it never reads AWS resources or derives
   health.

### Reconciliation

1. The scheduled reconciler reads one registry surface's per-authority cursor and bounded live
   `PENDING`/`AWAITING_CHILDREN` indexes within its page/time budget.
2. It re-attests the owning authority, sends only missing deterministic events through the same FIFO,
   and invokes the same conditional reducer for already parked work. Applying a predecessor removes its
   successor from the live pending index; exact child-terminal markers release an eligible parent.
3. An unavailable authority, over-bound pending gap, or unrecoverable retention loss projects `unknown`
   with a reconciliation reason; absence never becomes complete evidence.
