# Conversational Runtime

| | |
|---|---|
| **Level** | L2 — capability overview |
| **Status** | Current managed-Harness repository path. Dated validation evidence and its limits are owned by [validation results](../../../validation/RESULTS.md). End-to-end identity chain by [ADR-077](../../decisions/adr-077-carry-the-end-user-identity-to-every-hop-through-token-exchange.md) is deployed in dev at every hop, from the route through the Harness and Gateway to each tool, with per-user Memory ([architecture §5](architecture.md#5-key-runtime-flow)). |
| **Owns** | Verified turn handling, managed Harness invocation, purpose-built tool orchestration, streamed answer release, terminal audit capture, and the `ConversationStream.v4` stream. |
| **Conventions** | [Documentation conventions](../../conventions.md) |

## 1. Purpose and scope

Conversational Runtime accepts one verified customer turn and invokes an AgentCore managed Harness with all governed tools globally available under the existing deployment-wide `AuthorizedAnalyticalScope`. Harness owns conversational and multi-tool iteration and produces the concise natural answer shown to the user. Repository-owned tools resolve the user's model-authored semantic candidates, execute governed computation, and remain the mandatory authority for customer-specific facts and all figures; Harness may discuss and combine qualified results but never creates data, SQL, scope, or platform-capability authority. Harness may instead answer a social message or explain that an unrecognized or unsupported metric request cannot be mapped without invoking a tool, but only when the answer makes no customer-specific claim or figure, runs no substitute query or tool, preserves the user's intent, and asks for clarification or offers next steps. A typed tool refusal is equally valid.

It does not own source facts, deterministic figures, page visuals, crosscutting authentication, telemetry, or evaluation execution. A repository-owned AgentCore Runtime is not a default conversational loop. It is introduced only for a documented tool-hosting or control capability that the managed Harness proof cannot supply.

## 2. Where this fits

This capability refines the managed Harness, Tool Gateway, Domain Tools, Conversation API, Conversation Memory, and Answer Grounding containers in the L1 [architecture](../../architecture.md#capability-index).

## 3. Black-box contract

| | |
|---|---|
| **Inbound** | Verified actor, conversation identity, and complete `ConversationRequest.v3`, and the page labels redeemed from its reference ([ADR-120](../../decisions/adr-120-carry-page-context-to-chat-as-a-server-redeemed-reference.md)). |
| **Outbound** | Public activity stages, named governed tool-call events, and the agent's Markdown answer as contiguous `text` deltas released as the model writes them, then one `done` after the turn's receipt is written, or `refused`, inside one `ConversationStream.v4`; SSE comment heartbeats cover idle gaps and carry no data. |
| **Guarantee** | Tools are mandatory authority for customer-specific facts and all figures. A safe no-tool social response or unmappable unsupported-metric limitation makes no such claim or figure, runs no substitute query or tool, preserves intent, and asks for clarification or offers next steps; typed tool refusal remains equally valid. The API applies the inbound Guardrail, validates the Harness lifecycle and stream bounds, and never rewrites or gates the answer text; every turn leaves a content-free receipt, and a complete turn's exact released text is admitted to evaluation ([ADR-083](../../decisions/adr-083-stream-the-agent-answer-and-govern-it-by-audit-and-evaluation.md)). |
| **Implementation boundary** | Conversation adapters, managed Harness, governed Gateway tool contracts, semantic resolvers, answer streaming, audit, and typed frontend stream and Markdown rendering. |

## Documentation map

- [Architecture](architecture.md) owns managed-loop topology, Gateway placement, tool flow, Runtime exception criteria, and migration boundary.
- [Low-level design](low-level-design.md) owns wire contracts, authority rules, decisions, and capability-proof requirements.
- [Data-Flow Completion and Service Levels](../../concepts/data-flow-completion-and-service-levels.md) applies to evaluation handoff/outbox obligations; it does not redefine the interactive response lifecycle.
- [ADR-039](../../decisions/adr-039-assembled-context-replaces-the-declared-prompt.md) and [ADR-058](../../decisions/adr-058-plan-every-turn-and-treat-page-context-as-additive.md) retain the superseded context-design rationale.
- [Validation results](../../../validation/RESULTS.md) own sanitized acceptance conclusions.
