# ADR-063: Record delivery disposition for released turns

Date: 2026-09-10
Status: Accepted — implemented; the disposition is served by the quality-review audit projection (ADR-064) and dev acceptance is recorded in `validation/RESULTS.md`; blocks are released at plan acceptance since ADR-065, so the disposition observes the released set while the Harness turn finishes behind it; amended 2026-09-29 by [ADR-083](adr-083-stream-the-agent-answer-and-govern-it-by-audit-and-evaluation.md): the receipt is written after the stream, not before release, and the emitted count counts streamed text events (deployment state in ADR-083's status line)

## Context

[ADR-059](adr-059-create-a-bounded-agent-quality-audit-loop.md) persists the immutable audit receipt, admission linkage, and content-bearing evaluation outbox record transactionally before any public block is streamed. That ordering is deliberate: a crash after release must never produce an unaudited answer. Its consequence is that a persisted release does not prove delivery. The SSE relay closes a mid-stream failure with a safe terminal after some blocks were already emitted, and a client can disconnect after persistence, so a complete evaluation record can exist for an answer the user only partially received. Today no record distinguishes released-and-delivered from released-and-truncated, which contaminates any analysis joining evaluation scores to what users experienced.

## Decision

**After the public stream closes, the conversation route records one bounded delivery disposition keyed by the turn's audit identifier, and evaluation analysis joins it rather than assuming delivery.**

The relay already observes the facts needed: whether the terminal was reached, whether a refusal closed the stream, and how many blocks were emitted. The route writes a companion record after the stream closes carrying only closed states — delivered, truncated, or aborted — the emitted block count, the audit identifier, and a timestamp. It contains no conversation content, so the receipt's content-free property and ADR-060's retrieval boundary are unchanged. The audit receipt itself remains immutable; the disposition is a separate record, not a mutation.

The write is best-effort with the same asymmetry ADR-059 gives trace writes: a failed disposition write never converts a delivered answer into a failure and never blocks the stream, and its absence is read as unknown delivery, not as delivered.

Evaluation continues to run on every persisted released answer, because the answer was genuinely produced, verified, and released; delivery does not change its quality as a response. The disposition exists so the feedback loop can filter or flag turns where the user did not receive what was evaluated.

The disposition records server-observed delivery only. Emission that the server completed but the network or client silently dropped is not observable without an acknowledgement protocol, which this decision does not introduce.

## Consequences

Per-turn analysis can separate response quality from delivery experience, and a "temporarily unavailable" complaint can be checked against whether the turn was truncated mid-stream. The route gains one post-stream write and the quality storage boundary gains one small record family with the same encryption, immutability, and retention discipline as findings.

A disposition can be absent for turns that predate this decision or whose write failed, so consumers must treat absence as unknown rather than delivered.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| Persist the outbox record after delivery completes | A crash after delivery would release an unaudited, unevaluated answer, inverting the audit-before-release invariant. |
| Gate or discard evaluation when delivery is incomplete | The released answer is real quality signal, and a best-effort delivery record is too weak to gate a durable evaluation pipeline. |
| Mutate the audit receipt with a delivery field | Receipts are conditionally immutable writes by ADR-059; reopening them for late updates weakens every attestation built on them. |
| A client acknowledgement protocol | Proves true receipt but adds a browser contract and a write amplification this gap does not justify; revisit if server-observed delivery proves insufficient. |

## Review trigger

Revisit when truncated deliveries are frequent enough to justify client acknowledgement, when a multi-turn quality contract changes what a delivered turn means, or when the disposition record needs dimensions beyond closed delivery states.
