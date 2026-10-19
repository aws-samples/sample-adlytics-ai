# ADR-056 — Publish agent-authored narrative as governed semantic blocks

| | |
|---|---|
| **Status** | Superseded 2026-09-29 by [ADR-083](adr-083-stream-the-agent-answer-and-govern-it-by-audit-and-evaluation.md): plan publication and the native block vocabulary are retired (deployment state in ADR-083's status line). Previously accepted. |
| **Date** | 2026-09-10 |
| **Decision** | Governed tools remain authoritative for data and figures; the managed agent authors and synthesizes the natural-language response inside a bounded `PresentationPlan`. A schema-enforcing finalizer validates native block structure, while [ADR-061](adr-061-bind-agent-authored-presentations-to-governed-evidence.md) owns current-turn evidence binding, semantic verification, and bounded repair. The Conversation API Guardrails, audits, and streams accepted wording without rewriting connective prose. |
| **Related** | [Conversational Runtime](../capabilities/conversational-runtime/README.md) · [ADR-006](adr-006-agentcore-agentic-layer.md) · [ADR-041](adr-041-guardrails-are-policy-at-the-gateway-and-applyguardrail-in-the-route.md) |


> **Amended 2026-09-27 by [ADR-082](adr-082-govern-the-agent-by-evaluation-and-audit-not-text-heuristics.md).** The product invariant is restated there: the agent
> reasons over governed figures and never fabricates one. The public-text markup and URL regular
> expressions are removed; block constructors keep their length and count bounds, and the
> frontend's plain-text rendering is the markup and link boundary.

## Context

The former path buffered arbitrary final text and attempted to fit it into one strict paragraph. Model formatting could therefore discard an otherwise valid tool-backed answer, while successful turns could still expose an unreadable wall of prose or internal-style metric names.

The product invariant is **tools compute, the model narrates**. Governance must constrain tool authority and public transport without reducing the agent to a deterministic rendering selector.

## Decision

Harness dynamically selects and iterates over the governed catalog and measurement tools. Tool servers resolve semantic intent, execute trusted plans, and return typed `ShapedResponse` results or typed limitations. The Harness adapter retains bounded, content-minimized custody metadata and grounded projections for audit and diagnostics; that custody does not dictate or rewrite the agent’s prose.

After its governed tool work, the agent authors the complete user-facing response as a `PresentationPlan` (originally `PresentationPlan.v1`; the V1 family is retired and `PresentationPlan.v2` under [ADR-061](adr-061-bind-agent-authored-presentations-to-governed-evidence.md) is the only decodable plan since [ADR-064](adr-064-retire-planner-era-contracts-and-anchor-the-audit-trajectory.md)). Supported native blocks are `paragraph`, `grouped_list`, `governed_table`, and `notice` with `notice|limitation` classification. Text, transitions, headings, captions, list items, and table presentation are agent-authored. Figures must come from governed tool results, and the prompt requires customer-readable labels and preservation of material qualifiers.

Because the current managed Harness resource has no native output-schema property, `presentation_publish` is the final schema-enforcement tool. It has no data permissions. It strictly decodes the agent-authored plan and returns a content-free acceptance plus server-derived plan digest. Failed structural attempts may be retried privately by the managed Harness; the adapter accepts exactly one successful finalizer after data-tool iteration and ignores the private terminal acknowledgement. The API independently decodes the same captured plan, materializes native blocks without rewriting wording, validates block bounds/public-text safety, and applies the outbound Guardrail to each block.

The API no longer emits a fixed preface. It may publish one bounded non-factual pre-tool update only when the agent actually emits it. Public activity remains a closed friendly projection of allowlisted lifecycle stages plus per-call tool events that name the repository's own declared logical tool — a deliberate transparency choice, since the closed server-owned name discloses product capability, never data. Identifiers, tool inputs, outputs, reasoning, telemetry, and provider errors stay private.

For selected evaluation, the API transactionally persists the immutable audit receipt, content-free admission linkage, and content-bearing retained outbox record before final public release. A scheduled disposable dispatcher leases due work, sends the existing evaluation envelope to SQS, retries failures with bounded backoff, and atomically records delivery. Evaluation no longer depends on generator resumption after `done`.

## Consequences

- The agent retains natural narration, synthesis, and explanatory prose.
- Native block structure replaces Markdown as the presentation contract.
- Formatting variability cannot invalidate a valid response merely because it used list/table semantics.
- Tool custody and plan digests improve auditability without becoming prose-selection controls.
- The finalizer adds one small no-data tool call and can reject structurally malformed plans; it does not evaluate factual correctness or redact prose.
- Outbound Guardrail intervention, dependency failure, malformed finalizer transport, and actual typed tool limitations retain explicit failure paths. Ordinary prose is not compared to exact rendering references.
- The retained evaluation outbox contains released question/answer content, uses managed encryption and PITR, never expires before delivery, and receives TTL only after delivery.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| Prompt-only JSON final text | The Harness does not enforce its shape and may add fences or surrounding prose. |
| Exact grounded-rendering selection | It prevents the natural narration and synthesis required by the product. |
| Raw Markdown/HTML rendering | It expands the browser attack and compatibility surface and recreates formatting brittleness. |
| Post-terminal direct SQS send | The stream consumer may stop at `done`, stranding evaluation work. |

## Review trigger

Revisit when the managed Harness exposes native structured-output configuration, when the native block vocabulary must expand, or when the evaluation retention policy changes.
