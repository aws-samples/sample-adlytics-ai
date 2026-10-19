# ADR-011 — Scoped business objectives as advisory context

| | |
|---|---|
| **Status** | Retired — the business-objectives feature was removed |
| **Date** | 2026-08-18 |
| **Decision** | **Customer-authored objectives are scoped and resolve by precedence — campaign over channel over account. They are advisory by default, stored in DynamoDB rather than the lake, and attached to rows inside the tool that returns them.** A response carrying more than one objective sets `mixes_objective`, exactly as it sets `mixes_basis`. |
| **Related** | [ADR-039](adr-039-assembled-context-replaces-the-declared-prompt.md) · [Conversational Runtime](../capabilities/conversational-runtime/architecture.md) §4 · [ADR-006](adr-006-agentcore-agentic-layer.md) · [Q-06](../decision-coverage-registry.md#question-register-q-nn) |

## Context

The former context assembly was specified as a purely technical assembler — intent, schema slice, metric semantics, capability, token budget. That is necessary but it is not what the business needs from it.

What is actually wanted: business users supply the strategy the agent should reason *with*. Advertising goals such as customer acquisition, ROAS efficiency or defending share against a new entrant, plus which products carry more strategic weight than others.

The worked example that drove this: **a campaign performing well is not a reason to move budget away from a flagship product.** Raw performance data cannot express that. Only the customer knows it.

**And there is no single answer per account.** Different campaigns have different jobs. An upper-funnel DSP campaign running customer acquisition is *supposed* to show worse ROAS than a Sponsored Products efficiency campaign. One strategy document per account cannot represent that, which is what makes scoping the central decision here rather than a refinement.

## Decision

### Objectives are scoped, and resolve by precedence

```
account         maximise_roas          the default posture
  channel dsp   customer_acquisition   upper funnel has a different job
    campaign X  defend_share           new competitor in coffee
```

Most specific wins. Account scope is mandatory and acts as the fallback, so **only exceptions are ever configured** — with two hundred campaigns nobody fills in two hundred forms.

V1 scopes are account, channel and campaign. Product tier is deferred.

### Advisory, not enforced

Objectives are reference context to aid reasoning, not constraints the platform enforces. A recommendation may run counter to a stated objective.

> **This creates an expectation problem worth naming.** A user who writes "flagship budget is protected" and later reads a recommendation to cut flagship spend will experience that as a broken promise, even though nothing promised enforcement. Two mitigations, both cheap: surface when strategy influenced an answer (the provenance work in [G-21](../decision-coverage-registry.md#gap-register-g-nn)), and label it *context* or *guidance* in the UI rather than *rules*, so the wording matches the behaviour.

A per-item `binding` flag is modelled but unused in V1. When a customer needs a genuine guardrail, domain code enforces that item in recommendation ranking. Advisory items never gate anything.

### The comparison problem, and why it matters most

If campaigns have different objectives, ranking them on one metric is frequently meaningless. "Your worst campaigns by ROAS" is a misleading answer when three of them are acquisition campaigns where ROAS is not the success measure.

This is structurally identical to `mixes_basis`. So it gets the same treatment: `ShapedResponse.Metadata` carries the objectives present, `mixes_objective` is **derived** from the distinct set, and narration must surface it.

**This is the highest-value part of the feature**, and it only works because objectives are scoped. A global objective could never flag a heterogeneous comparison.

### Storage is DynamoDB, not the canonical model

Objectives are **configuration**, not measured facts. They change when a human edits a form; canonical facts change when data lands. Putting them in Iceberg would mean a `MERGE` into the lake to record a config edit — the wrong machinery for the write pattern.

No objective dimension in the lake, no data contract, no curation job. Read through the existing `KeyValuePort` with a new namespace.

### Two read paths, for two different scopes

| What | When | Why there |
|---|---|---|
| Per-campaign objectives | inside the tool, after the query, before the response is built | there may be hundreds; only the ones in the answer are fetched |
| Account and channel posture, plus notes | injected into the prompt at context assembly, once per turn | small, and needed even for questions that return no rows — "what should I focus on this quarter?" touches no facts |

The per-campaign path is deliberately **inside a single tool invocation**, not a second tool the agent calls:

```
1. Athena     → campaign_key, spend, roas
2. DynamoDB   → BatchGetItem objectives for those campaign_keys
3. resolve precedence
4. attach objective to each row
5. set mixes_objective when they differ
   → ShapedResponse
```

> **The agent sees one tool call, and there is no code path that returns a number without its objective.** A separate objectives tool would let the agent skip the second call, or pair the results wrongly. Same reasoning as currency travelling with every monetary column.

### Free text is bounded, not retrieved

A `notes` field, capped at 2000 characters per scope, injected verbatim. It carries the nuance a form cannot — "defending share in coffee, do not optimise pure ROAS there."

No chunking, no embeddings, no vector store, no retrieval step. The cap is what makes that possible: notes always fit in the prompt, so they are always present.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| **One strategy document per account** | Cannot express per-campaign objectives, which is the actual requirement. It would also make every comparison look homogeneous when it is not |
| **Free-text document upload with RAG** | Needs an embedding model, a vector store and a chunking strategy. Worse, retrieval can miss the relevant passage, so the reference is silently absent. It is also the highest-value prompt-injection vector in the platform ([G-05](../decision-coverage-registry.md#gap-register-g-nn)) |
| **Objectives as a dimension in the canonical model** | Wrong write pattern: a config edit would trigger a lake `MERGE`. Revisit only if aggregation across more campaigns than fit one response is needed |
| **A separate objectives tool the agent calls** | The agent could skip it or mispair the results. Decoration inside the tool makes omission impossible |
| **Enforced constraints in V1** | Not what was asked for, and enforcing advice the customer meant loosely is its own failure. The `binding` flag leaves the door open |
| **Product tiers in V1** | Real, but the flagship case is partly served by campaign scope. Defer rather than widen the first cut |

## Consequences

- `ShapedResponse.Metadata` gains `objectives` and a derived `mixes_objective`. Every tool returning campaign-grain data must populate it.
- A new pure-domain module, `src/domain/strategy/`, owns the objective vocabulary and precedence resolution. It is testable with no AWS.
- The former context-assembly design gained a **strategy layer** alongside schema, semantics and capability; this retired behavior remains historical rationale only.
- One `BatchGetItem` per tool call over the campaigns in the result. Negligible, and cacheable in the container — but a cache is a staleness window, so a short TTL rather than container lifetime.
- Objectives are customer-authored text and identifiers entering a prompt. The threat model is far weaker than arbitrary upload, because the author is an authenticated user of the account describing their own business, but grounding checks still apply.
- No objective history in V1. "What was our objective last quarter?" is unanswerable until assignments become slowly-changing.

## Review trigger

- aggregation by objective is needed across more campaigns than fit in one tool response, which forces objective into the lake as a real dimension
- a customer needs a genuine guardrail rather than advice, activating the `binding` flag and enforcement in ranking
- product-level strategic tiers become necessary, adding a fourth scope
- notes routinely exceed the cap, which is a signal to add a structured field rather than to adopt RAG
- someone asks what an objective was historically, which makes assignments slowly-changing
