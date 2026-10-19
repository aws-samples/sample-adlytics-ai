---
inclusion: auto
name: backend-observability-lambda
description: Use when changing or diagnosing backend adapter telemetry, structured lifecycle or error reporting, ADOT tracing, Lambda correlation or flush behavior, provider failure diagnostics, diagnostic redaction, or error metrics.
---

# Adapter telemetry and failure reporting

The current telemetry contract is owned by docs/concepts/observability.md. Error
classification is owned by ADR-009 and ADR-010. Use the shared provider failure
extractor in `adapters.outbound.aws_errors` and the shared reporting seam in
`adapters.outbound.telemetry.reporting`.

## Tracing

- Trace through the repository ADOT instrumentation path. The banned
  alternatives (Powertools Tracer, OTel baggage, logger.exception) are gated by
  make check-observability-bans, not restated here.
- Flush buffered spans before a handler returns.
- State at container initialisation whether tracing export is active.
- Propagate only W3C carrier fields the outbound request contract and inbound
  adapter both support. Never imply continuity across an AgentCore Gateway Lambda
  target; it is a trace and correlation boundary.
- Mint the turn correlation identifier once at conversation ingress and carry it
  as a structured field on every record the turn produces — tool custody, spans,
  audit receipt, admission, outbox, and evaluation envelope. Because the Gateway
  boundary breaks trace continuity, this field is the only join key that
  reconstructs a turn end to end.
- Never mint a second correlation scope mid-turn and never substitute a
  provider-issued identifier for the turn correlation identifier in a record
  that owns one. Record provider identifiers alongside it, not instead of it.

## Structured lifecycle levels

- Assign log levels per the level table in docs/concepts/observability.md.
- Route lifecycle records through `report_operation` or `report_lifecycle`.
  Never introduce an ad hoc lifecycle `Logger` call in an adapter.
- Route every failure through `report_error` before returning an error response,
  refusal, retry, or partial-batch failure.

## Provider failure diagnostics

- Translate SDK exceptions into the repository error taxonomy after adapter-owned
  retry and expected-control-flow handling is exhausted.
- Use `aws_failure_context` when an AWS SDK exception is available. Preserve every
  field aws_failure_context in src/adapters/outbound/aws_errors.py returns; that
  function owns the set.
- Treat a provider diagnostic as the authoritative failure boundary. Do not replace
  it with an inferred action/resource pair or an uncorrelated simulator result.
- Include operational identifiers needed to diagnose the operation when available:
  correlation ID, audit ID, admission reference, actor/owner/session identifier,
  principal ARN, resource ARN, provider request ID, provider action, and bounded
  business/request metadata.
- Never put infrastructure identifiers in a user-visible description.

## Redaction boundary

- Redact or omit tokens, cookies, authorization headers, passwords, secret
  values, private keys, access keys, credential objects, and raw request/header
  containers.
- Never log a credential-bearing field merely because it appears inside a provider
  message. Use the shared diagnostic-text redactor.
- Do not dump raw objects when named structured fields are available. Preserve the
  relevant metadata as separate structured fields instead.

## Metrics

- Emit the repository error metric on every error path before forming the response.
- Build metric dimensions only from enums, governed configuration vocabularies, or
  fixed adapter constants.
- Never dimension metrics on correlation, trace/span, audit, identity, session,
  entity, source, query, publication, provider request ID, or other per-request
  identifiers.
- Preserve the alarm behavior owned by the error taxonomy: dependency and
  invariant failures alarm; domain and validation outcomes remain product signals.

## Removal criterion

Remove this steering only when static checks enforce the documented adapter
telemetry projections, provider failure extraction, and lifecycle routing without
agent guidance.
