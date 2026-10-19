# ADR-065: Release public blocks when the plan is verified, not when the turn ends

Date: 2026-09-14
Status: Superseded 2026-09-29 by [ADR-083](adr-083-stream-the-agent-answer-and-govern-it-by-audit-and-evaluation.md), which streams the answer as it is written (deployment state in ADR-083's status line). Previously accepted — implemented and dev observed: on the previously failing acceptance question the
first public block left at 14.9 s against `end_turn` at 18.6 s (`validation/RESULTS.md`); the turn
self-healing built on this release point is ADR-066

## Context

A managed-Harness turn reaches the browser as one `activity` event and then silence until every
block and `done` arrive within a millisecond of each other. The transport is not the cause: the
Function URL streams, CloudFront forwards, the server flushes per event, and the frontend renders
each event as it arrives ([ADR-038](adr-038-the-conversation-route-streams-from-a-function-url.md)).
The content is withheld by `ManagedConversationRuntime.run`, which by design waits for the Harness
turn to end before it materializes the plan, Guardrails each block, writes the audit receipt, and
emits ([ADR-056](adr-056-publish-conversation-as-evidence-bound-semantic-blocks.md),
[ADR-059](adr-059-create-a-bounded-agent-quality-audit-loop.md),
[ADR-061](adr-061-bind-agent-authored-presentations-to-governed-evidence.md)).

Two fictional dev turns were measured on 2026-09-14 by reading the `InvokeHarness` event stream
directly and recording only event kinds, arrival times, and byte counts
(`validation/probe_harness_stream_timeline.py`):

| Phase | Single-tool turn | Two-tool turn with one repair |
|---|---|---|
| First model output (pre-tool text) | 6.7 s | 6.0 s |
| Governed tool results complete | 9.7 s | 13.4 s |
| Finalizer input starts | 11.1 s | 15.2 s (rejected), 21.6 s (repaired) |
| Finalizer input complete | 14.7 s | 19.7 s, 24.3 s |
| Finalizer Gateway result | 16.4 s | 20.3 s, 24.8 s |
| Closing assistant message ends the turn | 18.5 s | 26.5 s |

Two facts in those runs decide the design. First, the finalizer's `toolUse.input` is streamed as
token-sized fragments (176 and 339 of them, 2–12 bytes each) but the Harness delivers them as one
burst: a single leading fragment when the tool call starts, then every other fragment within
roughly 100 ms at the end of the content block. Nothing about the plan can be read while the model
is writing it. Assistant text, by contrast, does arrive token by token. Second, once the plan is
complete the turn still spends about 1.7 s on the finalizer's Gateway round trip and 1.3–2.1 s on a
closing assistant message the user never sees. The Conversation API already holds everything it
needs at the moment the plan is complete: every governed result precedes the finalizer by
construction, and the API's own verifier is a strict superset of the plan-intrinsic check the
finalizer performs in-loop.

A third observation is a robustness gap rather than a latency one. CloudFront's origin read
timeout on the conversation behaviour is sixty seconds of idle, and the runtime can be silent for
most of a deep dive; a 64 s turn was observed in dev on the same day. The server has an unused
`HEARTBEAT` comment frame and the frontend parser already skips SSE comment lines.

## Decision

**Release the verified block set the moment the plan is complete and verified against completed
custody, write the receipt before the first block as before, and let the rest of the Harness turn
finish behind an answer the user already has.**

1. The Harness adapter decodes the finalizer's plan when its `toolUse` content block stops,
   binds it to the custody it has already decoded from the preceding tool results, and runs the
   full semantic verifier there. An accepted plan is yielded to the runtime as a plan completion
   event carrying custody, plan, verification, and the trajectory so far. A rejected plan is
   recorded as a rejected finalizer step with the verifier's own violation codes and the adapter
   keeps reading; the model's repaired plan, if the API accepts it, is the one released. The
   adapter continues to consume the stream to `end_turn` after a plan is released and yields a
   turn-ended event; a finalizer result or later plan that disagrees with what the API released
   is a contract skew reported as an error, never a retraction.
2. The runtime materializes the accepted plan, Guardrails every block individually, writes the
   receipt (or the receipt, admission, and outbox transactionally) and only then emits blocks, all
   at plan completion. The audit-before-release rule of ADR-059 and ADR-063 is unchanged; what
   moves is the moment at which the receipt can be written. `done` is emitted after the Harness
   turn has ended, so the Harness lifecycle and its memory writes are not cut short.
3. The finalizer step's anchor in the trajectory becomes content-bound. An accepted finalizer step
   is anchored by the verifier's binding digest of the released plan and its evidence; a rejected
   one by the digest of the plan it rejected. The Gateway message reference that ADR-064 used
   arrives only after release and was already recorded as a weak anchor.
4. One activity stage is added to `ConversationStream.v3`: `composing_response`, emitted when the
   finalizer tool call starts, so the interval between "retrieving governed data" and "assessing
   publication" is visible. The stage order is `retrieving_governed_data`, `composing_response`,
   `assessing_publication`, `preparing_public_response`; each remains optional and strictly
   ordered. The frontend must accept the stage before the API emits it.
5. The server writes an SSE comment heartbeat while it waits on the runtime for more than fifteen
   seconds. The frame carries no data, the frontend discards it, and it keeps the idle gap under
   the edge timeout on every turn regardless of how long the Harness thinks.

## Consequences

For the measured turns, content leaves at roughly 15.7 s instead of 19.5 s and 25.3 s instead of
27.5 s: the finalizer round trip and the closing message move behind the answer. The pre-tool
preface stays in the released set, as ADR-061 decided, so it now appears at plan completion rather
than at turn end. Token-level progressive release is not available while the service buffers tool
input, and this decision does not pretend otherwise; the review trigger below names the change
that would reopen it.

The adapter decodes governed results as they arrive instead of at the end, so a malformed tool
result fails the turn at the same point it did before but is detected earlier. The verifier runs
once per finalizer attempt in the adapter and once more in `materialize_verified_plan`, as today.
A semantic rejection is no longer surfaced as a dependency failure by the adapter: it is a rejected
step, and a turn that ends without an accepted plan is the publication refusal it always was.

Anything the Harness does after the API released the answer cannot change what the user saw or
what the receipt says. If the Harness stream fails during its closing message, the turn still ends
with `done`, the failure is logged against the correlation ID, and the delivery record is written
as before. The Quality Review audit projection shows the new finalizer anchor; a reviewer who wants
the Gateway message reference no longer finds it in the receipt.

The heartbeat adds one idle timer per active turn to the server and nothing to the contract.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| Parse the plan incrementally from `toolUse.input` fragments and release blocks as each is verified | The Harness delivers the fragments as one burst at the end of the block, so there is nothing to read early. Even if it did stream, a later entry can fail a whole-plan rule (a mandatory qualifier not yet selected), so an earlier claim would be shown without the caveat that qualifies it, which [ADR-062](adr-062-route-claims-by-semantic-authority.md) and the tool contract forbid. |
| Have the model author the plan as streamed assistant text instead of a finalizer tool call | Text does stream token by token, but it gives up the strict schema, the in-loop repair, and the sole-finalizer invariant ADR-061 depends on, for a gain bounded by the same whole-plan rules. |
| Release the pre-tool preface as soon as it arrives | It is a public block released before any receipt exists; ADR-061 already decided to hold it into the audited set, and nothing in the measurements changes that trade. |
| Write the receipt after the Harness turn ends and rely on the delivery record | Leaves a window in which a released answer has no receipt if the function is interrupted during the finalizer round trip or closing message; ADR-059 forbids exactly that. |
| Emit `done` at plan completion and close the Harness stream | Ends the invocation before the Harness has finished its turn and memory writes; the effect on session continuity is unverified and the saving is a progress indicator, not content. |
| Drop the outbound Guardrail to shorten the tail | It costs under a second of a 20–60 s turn and is the only control over text the model composed from data-borne content ([ADR-041](adr-041-guardrails-are-policy-at-the-gateway-and-applyguardrail-in-the-route.md), G-05). |

## Review trigger

- AgentCore delivers `toolUse.input` fragments incrementally rather than at block end, which
  reopens per-block release for entries whose whole-plan obligations are already satisfied.
- The Harness exposes a native output schema for the final message, which would let the plan
  arrive as streamed structured output without the finalizer round trip.
- A reviewer needs the Gateway message reference of the accepted finalizer call, which would
  argue for an attestation companion written after the turn ends.
- The measured gain in `validation/RESULTS.md` falls short of the projection above.
