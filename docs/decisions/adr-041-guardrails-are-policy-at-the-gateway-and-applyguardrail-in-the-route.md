# ADR-041 — Gateway action policy and route content policy

| | |
|---|---|
| **Status** | Accepted — amended 2026-09-29 by [ADR-083](adr-083-stream-the-agent-answer-and-govern-it-by-audit-and-evaluation.md): the route applies content policy to the question only, with no outbound Guardrail and no evidence-bound release (deployment state in ADR-083's status line). Amended 2026-09-30: the input policy is calibrated, not defaulted ([below](#amendment-2026-09-30--calibrated-input-policy)). |
| **Date** | 2026-09-05 |
| **Decision** | Gateway provides managed tool connectivity and action-level policy; purpose-built tools enforce semantics; the Conversation API applies input/output content policy and evidence-bound release. |
| **Related** | [ADR-006](adr-006-agentcore-agentic-layer.md) · [ADR-042](adr-042-guardrails-at-the-gateway-do-not-reach-a-lambda-target.md) · [ADR-056](adr-056-publish-conversation-as-evidence-bound-semantic-blocks.md) |

## Context

The managed Harness invokes purpose-built tools through AgentCore Gateway. Gateway is useful for IAM-authenticated connectivity, target routing, credential handling, and action-level policy. It is not the semantic authority for analytical requests.

The current MCP Lambda target probe established that Gateway policy has no tool input or output payload context. A Gateway policy therefore cannot validate metric, grain, filter, period, evidence, or result semantics. [verified 2026-08-24; ADR-042]

## Decision

The control placement is:

| Layer | Owner | Responsibility |
|---|---|---|
| Input policy | Conversation API | Apply content policy after identity verification and before Harness invocation. |
| Tool connection | Gateway | IAM-authenticated managed MCP connectivity and action-level policy. |
| Tool semantics | Purpose-built tool server | Resolve semantic input, enforce scope and contracts, compile trusted execution, and shape qualified evidence. |
| Release policy | Conversation API | Validate evidence-bound publication, apply output content policy, write audit, and emit one closed public stream. |

Gateway tools remain behind the Harness. The model sees only declared tool schemas. Tool schemas do not carry identity, scope authority, SQL, query plans, evidence references, or budget overrides.

### Amendment 2026-09-30 — calibrated input policy

The input policy's strengths are set by replaying a fictional question set, and a deploy is accepted only if the deployed guardrail stays inside the calibrated result. The current configuration is owned by the [Conversational Runtime LLD](../capabilities/conversational-runtime/low-level-design.md#inbound-guardrail); the evidence is in [validation results](../../validation/RESULTS.md#inbound-guardrail-calibration--2026-09-30).

- **Why the false-positive side wins.** The Classic tier with prompt attack at `HIGH` blocked 7 of 38 benign questions, including formatting and correction requests, and missed 3 of 16 attacks. A filter that declines ordinary questions gets switched off. A direct prompt attack by an entitled user reaches only that user's session, because each tool verifies identity and scope on the server ([ADR-077](adr-077-carry-the-end-user-identity-to-every-hop-through-token-exchange.md), [ADR-054](adr-054-scope-analytical-visibility-to-the-customer-deployment.md)), so the inbound filter is a first screen, not the boundary.
- **Why Standard tier at `LOW`.** It blocked none of the 38 and missed 1 of 16 on the same set. It is also the only tier that detects system-prompt leakage attempts. The cost is assessment latency (median 549 ms against 228 ms for Classic over the same 54 sequential calls from one client) and processing anywhere in the guardrail profile's geography.
- **Why an acceptance replay, not a deploy-time gate.** The replay calls the deployed guardrail. Running it inside CloudFormation would need a custom resource holding its own `ApplyGuardrail` grant and running 54 assessments on every stack update. `make accept-guardrail` runs after the deploy instead, and the intervention-rate alarm watches the same signal in live traffic. A failed replay does not roll the deploy back; a custom-resource gate remains open if that is needed.
- **Why refusals name their checks.** Without the filter and confidence, a false positive cannot be told from an attack except by replaying the user's text, which the observability rules forbid keeping. The label carries the check and confidence, never the matched value.

## Consequences

- Gateway action policy supplements but never replaces tool-side semantic enforcement.
- Current MCP Lambda target payloads are not a Guardrail/Policy inspection channel.
- The Conversation API must retain input/output policy and evidence-bound release even when Harness manages tool iteration.
- A future Gateway target type with usable request/response policy context requires a new decision before moving semantic enforcement.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| Gateway policy validates analytical request/result content | Current MCP Lambda target policy cannot inspect that content. |
| Harness prompt instructs semantic safety | Prompt guidance cannot establish authority or trusted execution. |
| Route performs analytical computation | The route is trusted ingress/release, not the query or semantic-resolution boundary. |

## Review trigger

Reopen when Gateway policy gains documented, verifiable tool payload context for the target type in use, or when a different Gateway target contract is adopted.
