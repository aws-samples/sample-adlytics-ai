# ADR-061: Bind agent-authored presentations to governed evidence

Date: 2026-09-10
Status: Accepted — post-Harness verification, in-loop repair, and V4 dev accepted; V1 compatibility path retired by ADR-064; release moved to plan acceptance by ADR-065; one server-authored repair round added by ADR-066; the release gate (semantic verification of the presentation plan before release, with its repair) superseded 2026-09-29 by [ADR-083](adr-083-stream-the-agent-answer-and-govern-it-by-audit-and-evaluation.md), which keeps the governed evidence as evaluation evidence (deployment state in ADR-083's status line)


> **Amended 2026-09-27 by [ADR-082](adr-082-govern-the-agent-by-evaluation-and-audit-not-text-heuristics.md).** The numeric-literal rule (free paragraphs and
> collection introductions may not contain a digit after a governed call, in the finalizer and the
> post-Harness verifier) is superseded and is removed with its prompt and tool-description text.
> Evidence references, mandatory qualifiers, required collections, and claim selection stay
> deterministic.

## Context

[ADR-056](adr-056-publish-conversation-as-evidence-bound-semantic-blocks.md) restored natural agent narration by placing the complete response in a typed `PresentationPlan.v1`. Governed tools remained authoritative, but the finalizer checked only block structure. A deployed metric-inventory response consequently preserved the correct tool-provided total while inventing category counts that disagreed with its own listed content. The plan was valid JSON and safe public text, so neither structural validation nor the outbound content Guardrail could identify the factual contradiction.

The model must remain free to choose governed tools, organize an explanation, and author transitions, interpretation, and follow-up prose. That freedom does not require the model to copy values, calculate totals, define collection membership, or decide whether a partial result is complete. Prompt instructions alone cannot prove that those operations remained tool-owned.

## Decision

**Agent-authored presentation remains flexible, while every data-bearing value, collection, recommendation, and material qualifier is bound to a current-turn governed evidence reference and verified before release.**

Governed tools return a typed evidence manifest alongside their existing response. Claims carry server-formatted display values and semantic scope. Collections carry server-owned labels, ordered members, and a closed completeness state. Material qualifiers and recommendations carry their own references. Counts are derived from collection membership and are never model-authored.

The managed agent authors `PresentationPlan.v2`. Free conversational text remains available for connective and explanatory prose. Quantitative statements use claim references; exhaustive lists use collection references; governed tables use structure references; required caveats use qualifier references. The server materializes referenced factual atoms into the existing `ConversationStream.v3` public blocks, so the browser contract does not change.

A pure semantic verifier receives the original question, answer obligations, current-turn tool custody, and the proposed plan. It rejects unknown or cross-turn references, scope mismatches, unsupported factual literals, incomplete collections presented as exhaustive, omitted or repeated collection members, missing material qualifiers, and unsatisfied obligations. Its output is a closed result with deterministic violation codes and content-bound plan, evidence, and binding digests.

The first migration stage runs the verifier after Harness completion, where decoded custody and the captured plan already coexist. A failed verification withholds publication. The second stage was to move the same verifier into `presentation_publish` using an authenticated evidence receipt, allowing bounded repair within the managed Harness; that receipt channel was tried, found unreliable, and retired ([ADR-064](adr-064-retire-planner-era-contracts-and-anchor-the-audit-trajectory.md)), so in-loop repair covers only the plan-intrinsic rules described under implementation status. The Conversation API re-verifies the accepted plan before Guardrail assessment and publication. Exactly one accepted finalizer remains required, and no governed tool may run after acceptance.

Private model reasoning is neither factual authority nor an audit requirement. Audit retains content-free obligation, evidence, binding, verification, and repair metadata. Exact question, released answer, and protected evaluation evidence remain in the separately governed evaluation custody boundary.

[ADR-062](adr-062-route-claims-by-semantic-authority.md) refines which claims require governed evidence by routing on claim authority; evidence binding and verification for governed claims are unchanged.

Evaluation separates deterministic correctness from model-scored usefulness. Runtime semantic verification is the release gate. Reference-based fictional regression evaluates known outcomes before deployment. Online evaluation monitors helpfulness, consistency, and newly observed failure patterns; a soft score can never override a failed factual invariant.

## Consequences

The agent retains natural narration and dynamic tool use, but it can no longer create a count-bearing category or value by writing an unrestricted string. Tool interfaces become task-shaped rather than flat API projections, and their evidence manifests become part of the versioned agent-computer interface.

Publication adds semantic verification and, after authenticated receipts are available, a bounded repair cycle. This increases contract surface, latency on failed first attempts, and audit metadata. It avoids a deterministic prose template: only factual atoms and collection membership are server-owned, while explanation and composition remain agent-authored.

A free-text qualitative assertion cannot receive deterministic factual authority merely by citing a reference. Common interpretations such as increase, decrease, highest, lowest, complete, partial, and unavailable therefore use typed semantics. More nuanced supported prose may receive an additional model critique, but that critique is supplemental and cannot replace deterministic verification.

`PresentationPlan.v1` and the existing scalar evaluator were migration-only compatibility paths. Both are retired: Plan V2 with semantic verification and exact evidence binding is the only publication route, and each evaluator's result is linked as its own record ([ADR-064](adr-064-retire-planner-era-contracts-and-anchor-the-audit-trajectory.md)). *(Amended 2026-09-27: the V3 judge that was linked beside the V4 dimensions is retired; see [ADR-079](adr-079-enable-online-evaluation-only-through-a-verifying-gate.md).)*

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| Add more prompt warnings | A warning influenced the failed response but could not prove compliance. |
| Use a stronger model for every turn | It may reduce mistakes but cannot make factual provenance deterministic. |
| Parse arbitrary final prose into claims after generation | Claim extraction is probabilistic and can miss implied, compressed, or contradictory assertions. |
| Restore exact-render-only responses | It guarantees factual text by removing the natural narration required by the product. |
| Let an online LLM judge gate release | Online evaluation is asynchronous and lacks per-turn reference truth; it detects rather than prevents. |
| Introduce a general multi-agent system | It adds coordination and cost before the single-agent evidence boundary is correct. |

## Implementation status

Post-Harness semantic verification, server materialization, task-shaped catalog collections, protected grounded evaluation context, and multidimensional Evaluation V4 are implemented and accepted in dev. Plan V1 was first actively rejected and is now undecodable: its contract family was deleted by [ADR-064](adr-064-retire-planner-era-contracts-and-anchor-the-audit-trajectory.md). Pre-tool updates are buffered into the final assessed and audited block set. AgentCore actor memory is versioned and conversation-scoped so an incompatible plan contract cannot retrieve failed plans from another conversation.

Authenticated self-contained evidence receipts were deployed as the proposed in-loop mechanism, but both available models copied even compact opaque MAC tokens unreliably and owner-wide semantic memory replayed stale tokens. Direct current-invocation Harness custody is therefore the sole semantic authority; the receipt adapter, its settings, its prompt sentence, and its KMS key were removed by ADR-064 (2026-09-14). The finalizer validates Plan V2 structure plus the plan-intrinsic subset of the semantic rules: when a plan references evidence, it proves a governed call happened this turn, so a numeric literal in free prose or a collection introduction is returned as a repairable violation without needing the grounding itself (2026-09-13, after a dev turn placed a returned date in free prose and only failed post-Harness). The Conversation API still verifies full custody-bound semantics and fails closed before publication. Full in-loop repair for evidence-dependent rules such as claim selection remains pending until a trusted shared turn context or a server-issued current-turn custody reference is available.

Evaluation V4 produced all three dimensions for exact live grounded turns and correctly scored a fictional inconsistent response through on-demand evaluation. One exact online result arrived after the original acceptance polling window; its audit, delivery, writer, and eventual three evaluator results are all verified, so the delay is an observed latency characteristic rather than missing evaluation custody.

## Review trigger

Revisit when AgentCore supplies a trusted shared turn context to every Gateway target, native output schemas can bind evidence references without the finalizer tool, a new public artifact type needs factual materialization, or human-calibrated evidence shows that the bounded critic or repair policy no longer meets the accepted quality and latency targets.
