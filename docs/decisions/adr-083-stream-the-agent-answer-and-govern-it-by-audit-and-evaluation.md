# ADR-083: Stream the agent's answer and govern it by audit and evaluation

Date: 2026-09-29
Status: Accepted 2026-09-29 — implemented, deployed, and dev accepted 2026-09-30, except the `client_disconnected` receipt, which is unit-tested only because dev cannot produce it through CloudFront ([validation results](../../validation/RESULTS.md#streamed-markdown-answer-adr-083--2026-09-30))

## Context

A chat turn shows one progress label and then the whole answer at once. The transport is not the
cause: the Function URL, CloudFront, the server's per-event flush, and the frontend parser all pass
events as they arrive ([ADR-038](adr-038-the-conversation-route-streams-from-a-function-url.md),
[ADR-065](adr-065-release-public-blocks-when-the-plan-is-verified.md)). The design withholds it:

- The agent authors the whole answer as the argument of one tool call, `presentation_publish`,
  and the managed Harness delivers tool input in one burst at the end of the block. Assistant text,
  by contrast, arrives token by token [verified 2026-09-14, ADR-065 Context, repository evidence].
- The Conversation API then verifies the whole plan against tool custody, materializes rendering
  references into blocks, runs the outbound Guardrail on every block, writes the receipt, and only
  then emits all blocks together (ADR-065 §2).

ADR-065's event-stream timeline (2026-09-14) put a single-tool turn's first model output at 6.7 s
and the start of its answer, the finalizer input, at 11.1 s; a two-tool turn with one repair
started its corrected answer at 21.6 s. A separate probe through the adapter and block runtime,
with the outbound Guardrail bypassed and a recording audit fake, saw the previously failing
question release its first public block at 14.9 s (`validation/RESULTS.md`, 2026-09-14).

[ADR-082](adr-082-govern-the-agent-by-evaluation-and-audit-not-text-heuristics.md) already moved
figure correctness from a pre-release check to evaluation after release. The owner decided on
2026-09-29 to extend that reasoning to the whole answer: remove the pre-release machinery, let the
agent answer in its own words and format, and make the audit and evaluation feedback loop the
mitigation.

## Decision

**The agent streams its answer as Markdown text, the browser renders it as it arrives, and nothing
gates it before the user sees it. Every turn leaves a receipt with the exact released text, and
evaluation and audit measure what was released.**

1. **Streamed answer.** The Harness adapter forwards every assistant text delta as it arrives,
   including text written before and between tool calls. Reasoning content stays private. The
   public stream becomes `ConversationStream.v4`: `activity`, `tool`, `text` (a contiguous,
   bounded delta sequence), and one `done` or `refused` terminal. Tool and activity events may
   interleave with text. The 64 KiB bound on released text is unchanged.
2. **The agent chooses the format.** Paragraphs, headings, lists, tables, emphasis, inline code,
   quotes, and links, in GitHub-flavoured Markdown. The browser renders it with a Markdown-to-React
   renderer that never renders raw HTML and never loads images; links open in a new tab with
   `noopener noreferrer` and only for `http`, `https`, and `mailto`.
3. **Retired pre-release machinery.** The `presentation_publish` finalizer, `PresentationPlan.v2`,
   semantic verification and materialization, the server repair round, the pre-tool preface rule,
   the per-block outbound Guardrail, and the audit-before-release ordering. The inbound Guardrail
   is unchanged.
4. **Receipt after the stream, before `done`.** The receipt, and for an admitted turn the
   evaluation admission and outbox, are written when the Harness turn ends, carrying the exact
   concatenated text the user received, so `done` still carries the support reference
   ([ADR-081](adr-081-make-every-turn-traceable-and-attributable.md) §2). A client that disconnects
   mid-stream still leaves a receipt, with the terminal outcome `client_disconnected`, and a turn
   that fails keeps the trajectory it reached.
5. **Governed evidence stays.** Governed tools still return typed data, qualifiers, refusals, and
   their grounding projection. The agent narrates them; it no longer references rendering IDs. The
   grounding projection and its answer obligations (mandatory qualifiers, required collections,
   claim-bearing results) become evaluation evidence: the grounded-correctness evaluator already
   refuses its top rating to an answer that omits a required qualifier or collection.
6. **The feedback loop is the control.** The agent-mode regression run gates every prompt, tool,
   or model change (ADR-079 §5). Released turns are admitted to evaluation, the per-evaluator
   alarms of ADR-079 §5 watch the verdicts, and the review API exposes each turn's receipt and
   results.

## Consequences

- Text appears as the model writes it. The first words of a tool-using turn appear when the model
  writes them before its first tool call, and the answer itself from when the model starts it.
  The acceptance measurement in `validation/RESULTS.md` owns the actual figures.
- Nothing can be retracted. An unsupported figure, a missing caveat, or harmful text composed from
  data-borne content can reach the screen. For a complete turn each is found after release by
  evaluation or review. A turn that showed text and then failed or lost its client is recorded by
  its receipt and delivery record only: its text is not admitted to evaluation, because evaluation
  judges the exact answer the user received and that answer was cut short.
  The outbound Guardrail was the only automated control on model-composed text drawn from tool
  data ([ADR-042](adr-042-guardrails-at-the-gateway-do-not-reach-a-lambda-target.md)); the owner
  accepted that on 2026-09-29, and G-05 (data-borne prompt injection) remains open.
- A function killed mid-stream leaves text the user saw with no receipt. Audit coverage measures
  that gap, as ADR-081 §1 already accepts for a killed function.
- A viewer who closes the page does not end the turn: through CloudFront the route's writes keep
  succeeding, so the turn completes and is recorded, delivered, and evaluated as complete. A
  `client_disconnected` receipt is written only when the route's own connection breaks.
- Receipts gain the `conversation-stream.v4` response contract, a released character count, and
  a trajectory of governed steps only. Stored `public-block-stream.v3` receipts stay readable as
  written. Readers deploy before the route that writes the new shape.
- The grounded-evaluation context loses its verification digests and becomes
  `GroundedEvaluationContext.v2`; the evaluator prompt names the new version and the calibration
  gate reruns on deploy. The calibration corpus gains cases that omit a mandatory qualifier, omit
  a required collection, and present a top-N list as complete, each read through a V2 document
  whose shape a test holds to the production encoder, so the gate measures the obligations that
  no longer block release.
- The first calibration of the required-collection case showed the evaluator rating an answer
  that omitted a required collection as `grounded`. The grounded-correctness prompt now checks
  each answer obligation explicitly; without that case the gap would have stayed invisible.
- Nothing before release stops text from the user's long-term memory reaching an answer. At
  acceptance a fictional preference left in the regression persona's memory by the identity-chain
  probe labelled its answers, and evaluation rated the label unsupported. The probe now removes
  its records and the regression run clears them before it starts; with clean memory the
  regression run matched 6 of 7 against a baseline of 5.
- Text written in separate assistant messages (a sentence before a tool call, then the answer)
  is joined with a paragraph break, so the released text and the evaluated text read as written.
- Rollout order: the frontend first, then the receipt readers, then the Harness prompt and the
  route back to back, and the Gateway finalizer target last. Between the Harness and route
  deploys, the old route receives plain text and refuses the turn.
- The chat UI renders Markdown. The ADR-082 lint rule against raw-HTML rendering stays in force:
  the renderer builds React elements from a Markdown syntax tree.

## Supersedes and amends

- Supersedes [ADR-056](adr-056-publish-conversation-as-evidence-bound-semantic-blocks.md) (plan
  publication and native block vocabulary), the release gate of
  [ADR-061](adr-061-bind-agent-authored-presentations-to-governed-evidence.md), and
  [ADR-065](adr-065-release-public-blocks-when-the-plan-is-verified.md).
- Amends [ADR-059](adr-059-create-a-bounded-agent-quality-audit-loop.md) and
  [ADR-063](adr-063-record-delivery-disposition-for-released-turns.md) (receipt after the stream,
  not before release), [ADR-041](adr-041-guardrails-are-policy-at-the-gateway-and-applyguardrail-in-the-route.md)
  and ADR-042 (no outbound Guardrail and no streaming hold),
  [ADR-064](adr-064-retire-planner-era-contracts-and-anchor-the-audit-trajectory.md) (no finalizer
  step), [ADR-066](adr-066-turns-default-disclose-and-repair-instead-of-failing.md) §5 (no repair
  round) and §7 (text between tool calls streams rather than being discarded), and [ADR-082](adr-082-govern-the-agent-by-evaluation-and-audit-not-text-heuristics.md)
  §3 (the structural plan checks become evaluation obligations).

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| Keep the plan and parse it incrementally | The Harness delivers tool input in one burst, so there is nothing to parse early (ADR-065). |
| Stream prose and keep server-rendered tables through inline references | Keeps a reference contract and a materializer for a formatting need a Markdown renderer meets. |
| Keep the outbound Guardrail as a sentence-buffered gate | Adds a per-sentence call and a retraction path for a control the owner judged not worth its cost for an entitled reader of their own data. |
| Run the outbound Guardrail once after the stream as an audit signal | Offered; the owner chose to drop it. |

## Review trigger

Revisit when evaluation shows an unsupported-claim or missing-qualifier rate the owner does not
accept, when a reader other than the data's owner can see a released answer, or when G-05 closes
with a control that needs a pre-release hook.
