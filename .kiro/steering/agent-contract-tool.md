---
inclusion: fileMatch
fileMatchPattern: ["**/src/adapters/inbound/tool_handlers/**"]
description: Governs tool-handler response envelopes, identity boundaries, in-tool decoration, SQL construction, response sizing, pagination, and collocation when editing tool-handler code.
---

# Tool contract

Envelope shape is owned by docs/capabilities/conversational-runtime/architecture.md. Decisions:
ADR-006 and ADR-007.

## The envelope

A tool returns ShapedResponse and nothing else. Its required fields, the
mixes_basis derivation, and the truncation-narrative pairing
are all raised by __post_init__ in src/domain/narrative/shaped_response.py — a
response missing them cannot be constructed, so none of that is stated here.

- Numbers are pre-formatted by the tool: rounded, scaled, unit-labelled. The
  tools-compute invariant itself is owned by meta-ai-collaboration.
- Write every registered qualifier and approved factual-context rendering as
  final user-facing text the agent can carry verbatim. Evaluation checks the
  released answer against them; release semantics are owned by
  docs/capabilities/conversational-runtime/architecture.md section 5.
- recommendations come from domain code. The model presents them; it never
  invents them.
- Never narrate a figure without the coverage window, freshness and suppression
  that qualify it. The envelope carries them; a narration that omits them is
  where a stale or partial answer starts reading as a current and complete one.

## Identity is never a parameter

Which field names are refused is ToolContract.__post_init__ (FORBIDDEN_MODEL_AUTHORED_FIELDS) in
src/adapters/inbound/tool_handlers/schemas.py, gated by make check-tool-schemas.

- Never read end-user identity from a model-authored tool argument. The only
  identity a tool accepts is the bearer the Gateway propagates to it, verified
  before any other use of the call. The channel and the checks are owned by
  docs/capabilities/conversational-runtime/low-level-design.md §6 (ADR-077).
- Enforce ADR-054's deployment-wide AuthorizedAnalyticalScope from server-owned
  adapter composition. Never introduce per-subject restrictions without an
  accepted assignment model and authenticated non-model target channel.

## In-tool decoration

- Context that decorates a response's rows — governed display labels today, any
  future advisory context — is resolved inside the tool, after the query and
  before the response is built. Never fetch it by having the agent call a second
  tool: it could skip the call, or pair the context with the wrong rows.
  ADR-067 applies this to labels; retired ADR-011 records the original
  rationale.

## SQL

- Never execute model-authored SQL. A tool passes a validated plan and the
  adapter composes the statement; see ADR-028.
- Never build a query string from a model-supplied value, including a page token.
  Interpolate an identifier only when a contract or the metric dictionary
  resolved it, and rebuild it from that source rather than using the caller's
  copy. Bind every value.
- Never assume a placeholder is bound. Athena parses ExecutionParameters as SQL
  expressions, so a value reaching one must already be a quoted literal; the
  Athena composition adapter (src/adapters/outbound/athena/composition.py) owns
  that verified behavior and its encoding.

## Size

ADR-002 records the Gateway response ceiling and its design consequence. The row ceiling is enforced by QueryRequest in the query port.

- Never answer by paginating. Aggregate to a coarser grain or refuse, because a
  breakdown of hundreds of rows is already more than a narration can say anything
  true about, so transport was never the binding constraint.
- Never introduce pagination without a per-turn page cap and server-validated
  opaque tokens. An unsigned token carrying query state is a model-editable query,
  and an uncapped one lets the model decide when to stop.

## Collocation

- Keep each tool independently testable. Collocation is a deployment choice and
  must not become a coupling in code.
