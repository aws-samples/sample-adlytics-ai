# ADR-082: Govern the agent by evaluation and audit, not text heuristics

Date: 2026-09-27

Status: Accepted 2026-09-27 by the owner — deployed and live-accepted in dev 2026-09-28. The acceptance used the released V4 evaluators: all seven fictional regression questions were scored, six matched, none regressed against the pre-change baseline, and the undeclared-metric case improved. It supersedes the numeric-prose rule
of [ADR-061](adr-061-bind-agent-authored-presentations-to-governed-evidence.md), the public-text
markup rule behind [ADR-056](adr-056-publish-conversation-as-evidence-bound-semantic-blocks.md)'s
block constructors, and the wire-token and citation-subset gates of
[ADR-068](adr-068-fill-declared-insight-slots-with-direct-structured-model-calls.md) and
[ADR-076](adr-076-tier-insight-narration-across-card-and-deep-dive-models.md) decision 2. It amends
the "tools compute, the model narrates" invariant as
[ADR-006](adr-006-agentcore-agentic-layer.md), ADR-049, and ADR-056 state it, and withdraws the
pre-publication authority check of
[ADR-062](adr-062-route-claims-by-semantic-authority.md). The evaluation and audit controls it
relies on are owned by [ADR-059](adr-059-create-a-bounded-agent-quality-audit-loop.md),
[ADR-078](adr-078-serve-quality-review-to-programmatic-callers-through-the-cloudfront-edge.md), and
[ADR-079](adr-079-enable-online-evaluation-only-through-a-verifying-gate.md). Amended 2026-09-29 by
[ADR-083](adr-083-stream-the-agent-answer-and-govern-it-by-audit-and-evaluation.md): the structural plan checks of §3 become evaluation obligations, and nothing checks the
answer before release (deployment state in ADR-083's status line).

## Context

The platform invariant has been that tools compute and the model narrates: no number reaches a user
unless a tool produced it. The implementation enforces that, and several related rules, by
inspecting the text the model writes:

- **Numbers in free text.** The conversation verifier rejects any digit in a free paragraph or
  collection introduction once a governed tool was called (`unsupported_numeric_literal`). The
  agent prompt and the `presentation_publish` description carry sentences that exist only to
  satisfy it. The agent therefore cannot say "spend rose 12% week over week" from two weekly totals
  a tool returned; it can only select renderings a tool wrote.
- **Markup and links.** Nine regular expressions reject markdown, HTML, URLs, and domain-like tokens
  in every public text block, including a sentence that names "Amazon.com". The rejection is not
  repairable within the turn, so the answer is not released.
- **Insight wire tokens.** Page insights are refused when their text contains any of nine internal
  enum words, although their inputs already present those states in customer wording.
- **Insight citations.** Page insights are refused when their declared citations include any figure
  the server did not supply, which refuses every derived figure.

These checks guess at meaning from surface text. Each has refused natural analyst language, each
needs its own prompt instructions and tests, and none can tell a correct inference from a
fabrication. The system already has the controls that can: every released turn is audited, every
dispatched turn is scored by three calibrated evaluators (grounded correctness, internal
consistency, helpfulness), and the review API lets the deploying customer read both. The owner
decided on 2026-09-27 that the agent is governed by prompts, evaluation, and audit, and that
heuristic text checks are removed.

The browser renders every agent-authored and insight string as a React text node: no
`dangerouslySetInnerHTML`, markdown renderer, or link detection exists in the frontend application
code, so markup and URLs in model text display as inert text [verified 2026-09-27, frontend
source search of `components/`, `features/`, `lib/`, `app/`].

## Decision

**The agent may reason over governed figures as an analyst does and never fabricates one. What the
model writes is governed by its prompts and measured by evaluation and audit, never inferred by
pattern-matching its text. Structural checks of references and contracts remain.**

### 1. The figure rule

- Every number the agent states is returned by a governed tool this turn or validly inferred from
  those numbers: arithmetic, comparison, ranking, or a projection labelled as one.
- An inferred figure carries the qualifiers of the figures it came from, and never combines figures
  of incompatible basis, grain, window, or currency.
- A number from outside the turn's evidence (general knowledge, memory, or invention) is a
  fabrication. A number the user supplied as a premise may be reasoned over as that premise.
- Publication does not block on this rule. The grounded-correctness evaluator judges every figure
  against the turn's evidence, the internal-consistency evaluator judges the arithmetic, and the
  audit receipt and review API expose both.
- Page insights follow the same rule: a narration may state figures derived from its supplied
  findings.

### 2. Removed

- The numeric-literal rule in the conversation verifier and the finalizer, its violation code, and
  the prompt and tool-description sentences that exist only to satisfy it.
- The markup, HTML, and URL regular expressions on public text. Length and count bounds stay. The
  frontend's plain-text rendering is the markup and link boundary, and a lint rule that forbids
  raw-HTML rendering in frontend application code makes it structural.
- The insight wire-token gate and the insight citation-subset gate, with the port guarantee that
  returned text is "verified". The insight inputs keep presenting internal states in customer
  wording, because that is an input contract, not a text check.
- The ADR-062 pre-publication authority check between block classes, which was decided and never
  built. Leakage between classes is measured by evaluation.

Each removal deletes the code, its tests, its prompt text, and its documentation in the same slice.
No path keeps the old check as a fallback.

### 3. Kept: structural checks

These check references and contracts, never the meaning of text: a copied rendering or collection
identifier must exist; mandatory qualifier renderings and required collections must be used; a
claim selection must be made where the tool requires one; plan JSON, block kinds, sizes, and
identifier formats must be valid; an insight's output class must match its declaration. The
mandatory-qualifier rule is what keeps coverage, freshness, suppression, and limitations attached to
figures, including inferred ones.

### 4. The measurement that replaces the checks

- **Evaluation covers inference.** Grounded correctness treats a figure correctly derived from the
  evidence as grounded, and a turn that makes no platform or customer claim as "Not Applicable"
  (owner decision 2026-09-27). Its calibration corpus gains cases for a correct derived percentage,
  a wrong one, a derivation across incompatible bases, a labelled projection, and a claim-free
  clarification. Insight calibration gains the matching derived-figure cases. The dev probe
  of 2026-09-27 settled how: grounded correctness uses a categorical scale, which records a label
  and no score, and the Agent Quality Loop design records the probe's findings.
- **The agent-mode regression path is built before the prompt change is promoted.** ADR-079 §5
  mandates it: a versioned fictional question set run through the deployed conversation route
  before and after a prompt, tool, or model change, scored by the released evaluators, with
  expected-versus-actual outcomes recorded. The evaluation-validity steering forbids promoting a
  prompt change without it.
- **Customers can audit the agent themselves.** The review API
  ([ADR-078](adr-078-serve-quality-review-to-programmatic-callers-through-the-cloudfront-edge.md))
  serves each turn's audit receipt, findings, and per-evaluator results inside the customer's
  account. Per-evaluator score metrics with a dashboard and alarms (ADR-079 §5,
  [quality over time](../capabilities/agent-quality-loop/low-level-design.md#quality-over-time-adr-079))
  make fabrication and inference quality trendable.

## Consequences

- The agent can say what an analyst would say about the figures it retrieved, and page insights can
  state the changes their findings imply.
- A fabricated or miscalculated figure can now be released. It is found by evaluation after release,
  not blocked before it; the probability is managed by prompts and calibration, and the rate is
  measured. Before this change such a figure could not be released in free text, and a correct
  inference could not be released either.
- The evaluation plane becomes load-bearing for correctness, not advisory. A turn that is not
  dispatched to evaluation has no figure check at all, so sampling, dispatch, and evaluator
  availability matter more than before.
- Historical audit records that carry `unsupported_numeric_literal` stay readable: audit violation
  codes are stored as bounded strings, not decoded into the removed enumeration.
- A plan's binding digest changes for plans that the numeric rule used to reject, so a stored digest
  from before this change is not re-verifiable against new code; nothing re-verifies stored
  digests.
- The evaluation-validity steering's layering, where a soft score never overrides a failed
  deterministic invariant, still holds for the structural checks that remain. Figure correctness
  is no longer a deterministic invariant.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| Keep the numeric-literal rule and add a list of allowed derivations | A second heuristic over the same text; still cannot tell a correct inference from a fabrication. |
| Let tools pre-compute every derivation the agent might state | Reduces the agent to a rendering selector and grows the tool surface with every question shape. |
| Build ADR-062's pre-publication authority check | Depends on the model labelling its own blocks honestly; a mislabelled block passes, so evaluation is needed either way, and the check adds a contract layer. |
| Keep the markup regexes as defense in depth | The frontend renders text inertly; the regexes refuse ordinary prose and make answers unreleasable. |
| Keep the insight gates for page insights only | The same heuristics with the same false refusals; insights are calibrated and evaluated like the agent. |

## Review trigger

Revisit when evaluation shows a fabrication rate the owner does not accept, when a rendering surface
that interprets markup or links is added, or when a customer-facing surface releases model text
without evaluation dispatch.
