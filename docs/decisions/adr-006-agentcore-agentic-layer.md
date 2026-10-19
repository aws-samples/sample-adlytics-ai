# ADR-006 — AgentCore managed Harness and governed tools

| | |
|---|---|
| **Status** | Accepted — amended 2026-09-25 and 2026-09-26 ([ADR-077](adr-077-carry-the-end-user-identity-to-every-hop-through-token-exchange.md)) |
| **Date** | 2026-09-05 |
| **Decision** | Managed Harness owns ordinary conversation, multi-tool iteration, and final user-facing prose. The Conversation API owns trusted ingress and release; Gateway exposes purpose-built tools; tool servers own semantic enforcement, trusted execution, customer-specific factual authority, and every figure. |
| **Related** | [Conversational Runtime](../capabilities/conversational-runtime/README.md) · [ADR-032](adr-032-tools-are-shaped-by-operation-not-by-metric.md) · [ADR-041](adr-041-guardrails-are-policy-at-the-gateway-and-applyguardrail-in-the-route.md) · [ADR-056](adr-056-publish-conversation-as-evidence-bound-semantic-blocks.md) · [ADR-077](adr-077-carry-the-end-user-identity-to-every-hop-through-token-exchange.md) |

> **Proposed amendment, 2026-10-07.** [ADR-120](adr-120-carry-page-context-to-chat-as-a-server-redeemed-reference.md) (Proposed) would add a redeemed page context to the Harness turn (`HarnessTurnV2`) as quoted data in a second text block. This decision stays in force, unchanged, until ADR-120 is accepted.

> **Amended 2026-09-25 by [ADR-077](adr-077-carry-the-end-user-identity-to-every-hop-through-token-exchange.md) — identity ownership.**
> The Conversation API still verifies the user first, but verified identity is no longer confined to
> it: ADR-077 mandates that the verified user identity is carried to the Harness, Gateway, and every
> tool as signed claims, and that no hop accepts an AWS role in place of the user. The Gateway's IAM
> invocation was then the current, pre-ADR-077 inbound model, not the permanent one. The current
> boundary is owned by
> [Conversational Runtime §5](../capabilities/conversational-runtime/architecture.md#5-key-runtime-flow).

> **Amended 2026-09-26 by [ADR-077](adr-077-carry-the-end-user-identity-to-every-hop-through-token-exchange.md) — every hop carries the user.**
> ADR-077 slices 2–5 are deployed to dev. The Harness trusts only a single-turn token the route
> mints from the user's verified token, the Gateway is `CUSTOM_JWT` against a token the Harness
> exchanges from it, and every tool verifies that token. The Gateway sentence in the decision below
> is corrected in place.


> **Amended 2026-09-27 by [ADR-082](adr-082-govern-the-agent-by-evaluation-and-audit-not-text-heuristics.md).** "Tools compute, the model narrates" now reads: tools
> retrieve and compute governed figures, and the agent may reason over them (arithmetic, comparison,
> ranking, a labelled projection) but never fabricates one. Evaluation and audit measure that; no
> text heuristic enforces it. The rest of this ADR's authority boundary (identity, scope, SQL,
> plans, evidence, execution limits, platform capability) is unchanged.

## Context

The product needs a conversational layer that can make multiple governed tool calls without letting a model create authority. The former repository-owned planning, admission, amendment, redemption, and direct-operation loop duplicated the managed agent-loop responsibility while requiring the model to generate internal protocol state. Recent dev evidence showed that this increased failure modes without improving semantic tool correctness.

Current AgentCore documentation states that Harness supports declarative AgentCore Gateway tools and manages invocation, credentials, and results. [Harness tools](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness-tools.html) [verified 2026-09-05]. Multi-tool iteration is therefore a managed capability, not a reason to retain a custom loop.

The platform invariant remains: **tools compute, the model narrates.** Tool-authored results are mandatory authority for customer-specific facts and all figures; first-class Harness prose naturally explains and combines them. A model may choose an allowlisted operation, but it cannot establish identity, scope, SQL, query plans, evidence, values, execution limits, or platform capability. Safe no-tool social responses and unmappable unsupported-metric explanations perform no computation and create no customer-specific facts or figures.

## Decision

### Managed primary path

```text
verified Conversation API
  → managed Harness
  → optional Gateway/tool iteration when customer-specific facts or figures require authority
  → qualified result or typed limitation, or safe no-tool social/unmappable-metric limitation
  → Harness-authored PresentationPlan.v2 through the no-data presentation finalizer, repaired in-loop until accepted
  → API lifecycle validation, per-public-block outbound Guardrail, and wording-preserving release
  → atomic audit and evaluation-outbox admission, then validated ConversationStream.v3
```

Harness owns model turns, multi-tool iteration, and the concise natural answer shown to the user. Gateway owns managed MCP connectivity, user-bound `CUSTOM_JWT` inbound authorization (IAM invocation until 2026-09-26; [ADR-077](adr-077-carry-the-end-user-identity-to-every-hop-through-token-exchange.md)), target routing, and action-level policy. Purpose-built tool servers own semantic resolution, trusted execution, every customer-specific fact, and every figure. They preserve explicit semantic intent and author required freshness, coverage, suppression, measurement-basis, scope, and other qualifiers. Harness may discuss and combine these governed results and explain typed limitations without weakening intent; its free-form prose cannot become SQL, scope, data, or factual authority. It may make no tool call for a social message or to explain that an unrecognized or unsupported metric request cannot be mapped only when it makes no customer-specific claim or figure, executes no substitute query or tool, preserves intent, and asks for clarification or offers next steps. Typed tool refusal remains equally valid.

The Conversation API owns verified identity, input policy, managed lifecycle validation, per-public-block outbound Guardrail assessment, wording-preserving structured block release into `ConversationStream.v3`, terminal audit, durable evaluation-outbox admission, and the public stream. The agent authors the natural prose inside a schema-validated native block plan; the API neither selects exact tool renderings nor semantically rewrites allowed narration.

### Tool authority

Tools are shaped by operation, not metric. The globally available tool inventory is generated from the catalog and measurement contracts. Each tool accepts only model-selectable semantic candidates and must derive canonical metric, grain, filter, period, coverage, and deployment scope from server-owned contracts. It rejects model-supplied identity, tenant scope, SQL, query plans, evidence references, and budget overrides. The server compiles any query and returns qualified facts or a typed clarification/refusal.

Gateway policy does not replace in-tool semantic enforcement. ADR-041 owns the current MCP Lambda target payload-inspection limitation.

### Runtime exception

A repository-owned AgentCore Runtime is not the default conversational loop. It is permitted only for a named tool capability whose managed-Harness proof establishes that a required managed facility is absent. Multiple tool calls, iteration, tool credentials, and tool results are not exception criteria.

## Consequences

- The primary conversation path removes custom planner, assessor, plan admission, amendment, redemption, direct Runtime-to-measurement transport, and turn-ledger orchestration.
- Catalog and measurement tools, their generated schemas, semantic resolvers, least-privilege IAM, qualifier shaping, and trusted query compilation remain repository-owned authority for customer-specific facts and all figures.
- The managed-Harness capability proof must establish Gateway invocation when governed computation is required, explicit-intent and qualifier preservation, typed-limitation behavior, safe no-tool social and unmappable unsupported-metric handling, lifecycle validation, per-public-block outbound assessment and structured `ConversationStream.v3` release, terminal audit correlation, and evaluation linkage for fixed fictional turns. It accepts safe no-tool publication with `operation_outcome=not_attempted`, `publication=published`, and `terminal=complete`, or typed tool refusal with `operation_outcome=refused`; it rejects substitute success, customer-specific no-tool claims, figures, and intent weakening.
- AgentCore Memory may provide scoped continuity, but it is not factual evidence or analytical authority.
- Direct command and shell capabilities remain denied unless separately approved.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| Repository-owned conversational loop | Duplicates managed multi-tool orchestration and introduces model-authored internal protocol state. |
| Generic query tool | Concentrates too much semantic authority in one ambiguous surface and weakens operation-specific validation. |
| Identity or scope as a model tool argument | A model cannot establish trusted identity or analytical authority. |
| Gateway policy as semantic enforcement | Current MCP Lambda target policy cannot inspect tool inputs or outputs. |

## Review trigger

Reopen this decision when a tool capability requires a managed-Harness facility that a controlled proof
cannot supply, when final prose cannot preserve required tool qualifiers and explicit intent, when
no-tool handling would extend beyond social or unmappable unsupported-metric limitations or permit
customer-specific claims, figures, substitute operations, or intent weakening, or when Gateway/tool
result behavior changes materially.
