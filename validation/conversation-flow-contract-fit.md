# Conversation-flow contract fit
> **Amendment, 2026-09-25 — the conversation path was implemented on the managed Harness.** The
> target this review authorized was built on the managed Harness instead of the repository-owned
> planner loop ([ADR-006](../docs/decisions/adr-006-agentcore-agentic-layer.md)); the boundaries below
> that name an initial admitted plan, amendment, or single-use redemption describe that retired loop.
> [ADR-063](../docs/decisions/adr-063-record-delivery-disposition-for-released-turns.md),
> [ADR-064](../docs/decisions/adr-064-retire-planner-era-contracts-and-anchor-the-audit-trajectory.md),
> [ADR-066](../docs/decisions/adr-066-turns-default-disclose-and-repair-instead-of-failing.md), and
> [ADR-067](../docs/decisions/adr-067-release-governed-display-names-never-canonical-keys.md) record the
> implemented behavior, and dev acceptance is recorded in [RESULTS.md](RESULTS.md). The status row below
> is historical as of its date.

| | |
|---|---|
| **Status** | Approved synthetic contract-fit review; target implementation remains pending |
| **Date** | 2026-09-01 |
| **Evidence class** | `presentation_synthetic` under [ADR-051](../docs/decisions/adr-051-classify-fixture-provenance-and-evidentiary-status.md) |
| **Authoritative contracts** | [Conversational Runtime](../docs/capabilities/conversational-runtime/README.md) · [Query Path](../docs/capabilities/query-path/README.md) · [Semantic Resolution](../docs/concepts/semantic-resolution.md) · [UI](../docs/ui/README.md) |

## Conclusion

The reviewed page-hydration, conversation, and technical specimens fit the accepted owner contracts. P1–P8, C1–C17, and T1–T7 are approved as synthetic contract-fit evidence, authorizing implementation of the target conversational runtime, query, frontend, and test scope.

This conclusion proves scenario intent and contract fit only. It does not prove source fidelity, query admissibility, runtime behavior, deployment, browser behavior, security custody, accessibility, model quality, or acceptance. Returned non-production evidence remains required under its owning validation plan.

## Approved boundaries

- Deterministic server operations, not the model or browser, create figures, ranking, comparison, value states, and provenance.
- Typed context, evidence, qualifications, refusal, assessment, and stream envelopes preserve the accepted custody and publication boundaries.
- Direct factual work stops after its initial admitted plan. Result-conditioned work requires an append-only amendment, bounded budget, single-use redemption, and recorded evidence before publication.
- The browser stages a response and commits only after `done`; malformed, duplicate, refused, or post-terminal streams do not publish staged content.
- Synthetic examples use fictional values only and never become runtime or acceptance evidence.

## Implementation and acceptance boundary

The target remains incomplete until the owning packages implement the approved codecs, planning, evidence, publication, stream, rendering, and regression coverage; the named non-production execution completes; and returned runtime, browser, audit, and protected-evaluator evidence is read. Record those conclusions in [RESULTS.md](RESULTS.md), not here.
