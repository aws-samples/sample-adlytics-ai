---
inclusion: fileMatch
fileMatchPattern: ["contracts/prompts/**", "src/domain/conversation/**", "src/domain/evaluation/**", "src/adapters/outbound/agentcore/**", "src/adapters/inbound/tool_handlers/schemas.py"]
description: Governs semantic-authority routing, refusal evidence, answer claim rules, tool-description authority, and audit capture when editing agent prompts, tool schemas, conversation or evaluation domain code, or the AgentCore harness adapter.
---

# Semantic authority

Decision and rationale: docs/decisions/adr-062-route-claims-by-semantic-authority.md.
Tool envelope, identity, SQL, and size rules are owned by agent-contract-tool
and are not restated here.

## Authority routes claims, subjects do not

- Route every claim by its authority class, never by its subject. The closed
  set of authority classes is owned by ADR-062. Never encode a metric name,
  topic, or term list in a prompt, tool description, or evaluator to steer
  routing.
- Require a governed tool only for a claim about this platform or this
  customer's data. Never require one for a general explanation or a
  hypothetical, even when the question names a declared metric.
- Resolve every customer-facing metric reference through one shared resolver.
  The set of resolvable surfaces is owned by the metric dictionary schema.
  Never give one tool a stricter matching surface than another.

## Refusals are evidence

- A typed refusal retains its grounding and a publishable rendering. Never
  strip grounding from a refused result and never map a typed refusal to a
  generic failure message.
- A refusal blocks only the claims that depended on it. Never let a refused or
  failed supplemental dependency invalidate an independently answerable part of
  the answer.
- A required dependency that fails gets an explicit limitation in the
  response. Never substitute a model-generated value for a governed result
  that was refused, failed, or suppressed.

## Claims in the answer

- State each governed claim together with the qualifiers its tool returned.
  Publication does not block on it (ADR-083); evaluation withholds its top
  rating from an answer that omits a mandatory qualifier or required
  collection.
- General-knowledge text never carries a platform capability claim or a
  customer figure. A derived figure comes only from this turn's governed
  results or a number the user supplied as a premise; never introduce a figure
  from outside them.
- Publish conclusions, assumptions, and uncertainty. Never publish
  chain-of-thought.

## Tool descriptions

- A tool description states what the tool is authoritative for, never what the
  model should know or explain from general knowledge.
- Never hand-write a capability status into a tool description. Wording about
  a deferred or unsupported capability derives from the contract that owns
  that status, so shipping the capability updates the description with it.

## Audit serves the feedback loop

- Every governed tool step records its typed disposition and the result
  reference that reaches its trace.
- Every audit record stamps the configuration provenance it ran under. The
  provenance field set is owned by ADR-062.
- Capture customer content only with a declared purpose, sensitivity tier,
  retention, and access boundary, inside the evaluation custody boundary.
  Never place customer content in the audit receipt.

## Validation

- Test routing with holdout concepts the implementation has never seen,
  written as question families spanning general, platform, customer, mixed,
  and ambiguous intent. Never assert routing correctness against a fixed term
  list.
