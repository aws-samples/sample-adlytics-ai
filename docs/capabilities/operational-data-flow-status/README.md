# Operational Data-Flow Status

| | |
|---|---|
| **Level** | L2 — protected operator capability |
| **Status** | Accepted by [ADR-073](../../decisions/adr-073-project-all-data-movement-into-an-operator-status-control-plane.md); observation-only on dev, live since 2026-09-19 ([validation results](../../../validation/RESULTS.md)); evaluated SLO activation gated by ADR-074. |
| **Owns** | Complete movement-surface registry, content-free lifecycle projection, protected operator snapshot API, and operator use contract. |
| **Conventions** | [Documentation conventions](../../conventions.md) |

> **Revision proposed, 2026-10-07.** [ADR-095](../../decisions/adr-095-expect-producer-deliveries-on-a-declared-schedule.md) (Proposed) would have each producer dataset declare when it is expected, and add one expected-delivery slot per dataset, source scope and period to the lifecycle projection. A slot is a precursor clock, never an authority. See [Response curves and budget simulation](../../concepts/response-curves-and-budget-simulation.md). This package changes when it is accepted and built.

## 1. Purpose and scope

Operational Data-Flow Status lets an entitled operator answer four questions consistently: what work
was observed, where it is now, what evidence supports that state, and what action is available. It
covers every repository-owned asynchronous data movement surface through one closed registry and one
content-minimized lifecycle vocabulary.

It does not move data, authorize publication, retry/delete/redrive work, expose source rows or protected
content, or replace any source authority. External extraction remains outside the application boundary;
visibility begins at registration or immutable landing and says so explicitly.

## 2. Where this fits

This capability refines the operational visibility boundary beside [Load Path](../load-path/README.md),
[Publication State](../publication-state/README.md), preset publication, insight processing, and the
[Agent Quality Loop](../agent-quality-loop/README.md). The [L1 architecture](../../architecture.md#capability-index)
owns only its black-box placement.

## 3. Black-box contract

| Interface | Contract |
|---|---|
| Surface registry | Closed versioned catalog of every repository-owned asynchronous movement path, its authority, lifecycle, telemetry, status producer, reconciliation, and operator guide. |
| Event input | Producers or transactional-outbox dispatchers send the closed `OperationalStatusQueueMessage.v1` union directly to one dedicated FIFO SQS queue after durable transition and exact re-attestation. It admits one per-flow lifecycle event, one separately idempotent categorized usage contribution, or one content-free aggregate surface observation. Existing S3 EventBridge delivery remains a workload trigger, not a status bus. |
| Projection | Idempotent current snapshot and bounded transition history in DynamoDB; derived, reconcilable, and never serving authority. The queue is a finite transport buffer, not history. |
| Observation and SLO results | Every active development surface projects authority-backed lifecycle, completion, continuity, descriptive measurement samples/windows, and usage. An `observation_only` surface has no target or attainment result. An `evaluated` surface may additionally project objective-versioned SLO results conforming to [Data-Flow Completion and Service Levels](../../concepts/data-flow-completion-and-service-levels.md); this capability invents neither targets nor semantics. |
| Initiation receipt | An owner interface that starts work returns operator-safe `flow_id` and `status_url`; retry executions retain that flow identity. |
| Summary API | `GET /api/operations/data-flows` returns observation/freshness, completeness, aggregate lifecycle state, and every registered surface; observation-only responses contain no overall health field. |
| Flow API | `GET /api/operations/data-flows/{flow_id}` accepts one canonical UUID and returns the bounded snapshot, execution lineage, evidence, and next action. |
| Event API | `GET /api/operations/data-flows/{flow_id}/events` returns cursor-bounded transitions in stable order. |
| Definition API | `GET /api/operations/data-flow-definitions` returns UUID relationships plus surface, stage, state, reason, next-action, and runbook vocabulary so clients do not invent semantics. |
| Authorization | Existing HttpOnly cookie BFF plus distinct `operations-status-read` Cognito entitlement; API role reads only the projection. |
| Diagnostics | Operational lifecycle UUIDs are API-safe handles. Raw logs, traces, provider messages, resource names, authority references, and internal provider/publication/attempt identifiers remain outside the API. |

### Operator question map

| Operator question | Start here | Stop when |
|---|---|---|
| Is data moving? | Summary API: aggregate lifecycle state, backlog trend, completed rate, observation freshness. | Every required surface is current or explicitly external. |
| Where is one flow? | `flow status {flow_id}` or the exact flow API with the UI/API-issued UUID. | An authority-attested terminal state or one actionable blocked/failed reason is present. |
| What led here? | Cursor-bounded event API or `flow explain {flow_id}`. | Execution retry lineage, parent flow, and causation event account for the transition. |
| Why is it slow? | Surface latency breakdown and the same `flow_id` in separately authorized diagnostics. | Queueing, compute, provider, or authority transition is identified; no residual is mislabeled. |
| What should I do? | Served reason reading and closed next-action code linked to the operator guide. | The action is executable or explicitly requires a different authorized owner. |
| Can I trust the status? | Evidence completeness, authority kind, observed-at, reconciled-at, and freshness. | Missing/stale evidence is `unknown`, never healthy. |

## Documentation map

- [Architecture](architecture.md) owns projector, reconciliation, API, authorization, and source boundaries.
- [Low-level design](low-level-design.md) owns the complete surface matrix, wire contracts, operator
  workflows, acceptance conditions, and rollout gates.
- [Operator tools](operator-tools.md) owns how to run the execution-graph operator CLI, the
  probe-input producer, the producer CLI and the validation probe runner in your own environment.
- [Data-Flow Completion and Service Levels](../../concepts/data-flow-completion-and-service-levels.md)
  owns reusable completion, clock, child-obligation, and SLO evaluation semantics.
- [ADR-074](../../decisions/adr-074-define-authority-backed-data-flow-completion-and-service-level-objectives.md)
  owns why those semantics are shared and concrete objective values are registry-governed.
- [ADR-073](../../decisions/adr-073-project-all-data-movement-into-an-operator-status-control-plane.md)
  owns why completeness is registry-checked and why the projection never becomes authority.
