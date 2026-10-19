# Agent Quality Loop

| | |
|---|---|
| **Level** | L2 — capability overview |
| **Status** | Current repository quality boundary: immutable trace-anchored terminal audit, structural processing, and the protected evaluation admission/delivery/writer chain linking one result per evaluator (the V4 dimensions; the V3 judge was retired 2026-09-27). Dated acceptance scope is owned by [validation results](../../../validation/RESULTS.md). |
| **Last updated** | 2026-09-29 |
| **Owns** | Content-minimized terminal-turn audit receipts, asynchronous structural findings, evaluation-linkage companions, restricted read-model behavior, and reviewed scenario candidates. |
| **Conventions** | [Documentation conventions](../../conventions.md) |

## 1. Purpose and scope

The Agent Quality Loop makes one conversational terminal decision retrievable and improvable without making application telemetry a conversation-transcript store. It records server-derived authority, publication, and terminal facts; links complete turns to separately protected evaluation; and supplies reviewed fictional scenario candidates for regression work.

Natural-language release remains in the [Conversational Runtime](../conversational-runtime/README.md). Protected evaluation custody, telemetry, and reviewer access remain in [Observability](../../concepts/observability.md). This capability does not own browser feedback, customer access to audit records, raw conversation custody, or automatic prompt, model, tool, or policy changes.

## 2. Where this fits

This capability refines the L1 Agent Quality Loop boundary in the [system architecture](../../architecture.md#capability-index). It is an internal customer-account capability adjacent to the managed Harness release boundary and Conversation API, not a browser feature or a response-authority path.

## 3. Black-box contract

| | |
|---|---|
| **Responsibility** | Persist immutable content-minimized terminal receipts; derive structural findings; link selected protected evaluation through immutable companions; expose a restricted redacted read model; and emit reviewed fictional scenario candidates. |
| **Inbound** | Server-authored terminal outcome, the anchored tool trajectory, opaque references, trace identity, deployed version identities, a normalized correlation identifier, and allowlisted per-evaluator result fields. |
| **Outbound** | `ConversationAuditRecord.v6`, `AuditFinding.v3`, `TurnDeliveryRecord.v1`, evaluation admission/delivery/writer companions and one `EvaluationResult.v3` per evaluator, bounded aggregate measures, a redacted read model, and fictional scenario candidates for human review. |
| **Owner** | Agent Quality Loop; no customer browser is a direct consumer. |
| **Implementation boundary** | Conversation domain contracts and outbound ports; DynamoDB, encrypted queue, protected CloudWatch subscription ingress, and asynchronous processing adapters; retained quality and evaluation-linkage stacks. |
| **Relevant qualities** | Retrievability, immutable attribution, content minimization, least privilege, asynchronous processing, versioned comparison, and no autonomous policy change. |

An audit receipt observes a governed terminal decision. It cannot authorize a tool, alter a budget, hold or release an answer, or turn an invalid response into a valid one. A failed audit write is a bounded, measurable coverage failure; it is not a second publication-authority gate.

Three mandated changes shape this capability: one table and one writer for each per-turn record family ([ADR-080](../../decisions/adr-080-confine-each-audit-and-evaluation-record-family-to-one-writer.md)), evaluation enabled only through a verifying gate ([ADR-079](../../decisions/adr-079-enable-online-evaluation-only-through-a-verifying-gate.md)), and a receipt for every terminal with a declared retention ([ADR-081](../../decisions/adr-081-make-every-turn-traceable-and-attributable.md)). The [low-level design](low-level-design.md#record-family-storage-mandated-by-adr-080) states each one, and its implementation status states what is implemented and deployed.

## Documentation map

- [Architecture](architecture.md) owns the capability components and its asynchronous runtime flow.
- [Low-level design](low-level-design.md) owns contracts, exact-read mechanics, and current linkage invariants.
- [Data-Flow Completion and Service Levels](../../concepts/data-flow-completion-and-service-levels.md) owns evaluation-movement completion clocks and objective results.
- [ADR-059](../../decisions/adr-059-create-a-bounded-agent-quality-audit-loop.md) owns the decision and history for the retained audit/evaluation loop; ADR-079, ADR-080, and ADR-081 amend it.
- [Protected Quality Review API](../protected-quality-review-api/README.md) and [ADR-060](../../decisions/adr-060-protect-quality-review-retrieval-with-an-explicit-operator-authorization-boundary.md) own the separate entitled operator boundary for this capability's redacted read model.

Current acceptance conclusions are owned by the [validation results](../../../validation/RESULTS.md#gate-5-event-driven-semantic-evaluation-acceptance--2026-09-03). The dev probe attested `completed` evaluation companions for one capability terminal and one clarification terminal using the deployed source subscription; no raw protected content entered its reported evidence.
