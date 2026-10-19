# ADR-081: Make every turn traceable and attributable

Date: 2026-09-26

Status: Accepted 2026-09-26 — partly implemented; current state in the
[Agent Quality Loop implementation status](../capabilities/agent-quality-loop/low-level-design.md#record-family-storage-mandated-by-adr-080),
and, for §3 and §4 (deployed and dev accepted 2026-09-28), the
[Protected Quality Review API](../capabilities/protected-quality-review-api/README.md) and the
[reviewer access log](../concepts/observability.md#38-reviewer-access-log-adr-081). §1's state is
in the same implementation status, and §2's in the
[turn reference](../capabilities/conversational-runtime/low-level-design.md#turn-reference-and-correlation-adr-081).
It amends
[ADR-059](adr-059-create-a-bounded-agent-quality-audit-loop.md) (a public support reference, terminal
coverage, and retention), [ADR-060](adr-060-protect-quality-review-retrieval-with-an-explicit-operator-authorization-boundary.md)
(reviewer access is logged with the reviewer), and
[ADR-077](adr-077-carry-the-end-user-identity-to-every-hop-through-token-exchange.md) §6 (one entitled
review section returns the principal). The mandated contracts are owned by the
[Agent Quality Loop design](../capabilities/agent-quality-loop/low-level-design.md) (receipts and
retention), the [Protected Quality Review API](../capabilities/protected-quality-review-api/low-level-design.md)
(the attribution section), [observability](../concepts/observability.md) (the terminal log line and
the access log), and the
[Conversational Runtime design](../capabilities/conversational-runtime/low-level-design.md) (the
stream reference and correlation). Dated validation is owned by
[validation results](../../validation/RESULTS.md).

## Context

An investigation starts from something the customer already has: a support ticket, an alarm, a log
line. Today that trail breaks in four places:

- **The user sees no reference.** The chat stream's `done` and `refused` events carry only a schema
  version and a kind [verified 2026-09-26, stream contract], and
  [ADR-059](adr-059-create-a-bounded-agent-quality-audit-loop.md) decided there is no public audit
  reference. A user reporting a bad answer cannot say which turn it was.
- **The correlation identifier is the client's.** The browser generates one and sends it on every
  request, and the route, the authorizer, and the auth handler adopt it [verified 2026-09-26,
  `frontend/lib/session.ts`, `src/adapters/inbound/conversation/server.py`,
  `src/adapters/inbound/auth_handlers/authorizer.py`]. It cannot be trusted as an identifier.
- **Some terminals leave no receipt.** An inbound guardrail intervention, a refused exchange, and a
  client that disconnects before the turn is accepted end without one, although ADR-059 requires a
  receipt for every terminal class.
- **The principal is stored but unreadable.**
  [ADR-077](adr-077-carry-the-end-user-identity-to-every-hop-through-token-exchange.md) §6 stamps the
  verified Cognito `sub` on the receipt and keeps it out of every review projection, so "who asked
  this" has no answer short of a table read.

Two controls are missing around that last answer. Reviewer access is not logged, because
[ADR-060](adr-060-protect-quality-review-retrieval-with-an-explicit-operator-authorization-boundary.md)
forbids retaining the subject. And receipts have no declared retention, so identifiable records are
kept indefinitely.

## Decision

**Every turn leaves a receipt and a reference the user can quote. An entitled auditor can read who
asked. Every review access is logged with its reviewer. Every identifiable record expires after a
declared retention.**

### 1. Every terminal writes a receipt

- An inbound guardrail intervention, a refused exchange, and a disconnect before acceptance each
  have a closed terminal outcome and write a receipt. The receipt write runs on every exit path.
- A failed turn keeps the trajectory it reached.
- A killed function still leaves no receipt. That gap is measured by audit coverage rather than
  closed, because closing it needs a second write per turn.

### 2. A support reference and a server-minted correlation

- Every response that ends a turn with a receipt carries the turn's `audit_id` as a support
  reference: the stream's `done` and `refused` events, and the pre-stream terminal response an
  inbound guardrail intervention returns. The chat UI shows it. An `audit_id` is an opaque
  server-issued value that grants nothing by itself; retrieval still requires review entitlement
  ([ADR-078](adr-078-serve-quality-review-to-programmatic-callers-through-the-cloudfront-edge.md)).
  This reverses ADR-059's "no public audit reference".
- Each request gets exactly one correlation identifier, minted at its first server hop: the
  authorizer on authorized routes, which passes it to the handler in its context, and the handler
  itself on routes without an authorizer. It is returned in a response header. The browser stops
  sending one, and any client-supplied value is ignored.
- The conversation terminal log line carries `audit_id`, `correlation_id`, and the terminal outcome
  for every terminal class, and a saved CloudWatch Logs Insights query ships with the stack. Those
  identifiers are the selectors the review API accepts, so one log line is enough to fetch the turn.

### 3. Entitled attribution

- The `turn` route's `attribution` section returns the receipt's principal, only to a caller in both
  `quality-reviewer` and `audit-attribution`. Every other caller gets the turn without it.
- The principal stays out of logs, metrics, traces, index attributes, and every other section, as
  ADR-077 §6 requires.

### 4. Reviewer access is logged

- The authorizer sets a `reviewer` key in its context, whose value is the verified subject, and an
  `attribution` flag saying whether the caller may receive the principal, only on the review routes.
  The HTTP API stage writes a JSON access log with the request identifier, route key, status, and
  those two keys
  ([HTTP API logging variables](https://docs.aws.amazon.com/apigateway/latest/developerguide/http-api-logging-variables.html))
  [verified]. On every other route the key is empty, so the access log never records an end user.
  The log format never references the context's general `subject` key.
- The log group lives in the retained agent-quality stack, has deletion protection
  ([ADR-079](adr-079-enable-online-evaluation-only-through-a-verifying-gate.md) §1), and keeps its
  records at least as long as receipts plus the TTL deletion lag. Synth fails otherwise.
- A refused request carries no reviewer key, so failed access attempts are counted but not
  attributed.
- This reverses ADR-060's rule that logs never retain the subject, for the reviewer access log only.
  It still never retains selectors, correlation identifiers, or content.

### 5. Declared retention

- Receipt retention is a required per-environment configuration value with no default.
- Every record of a turn, in every table
  [ADR-080](adr-080-confine-each-audit-and-evaluation-record-family-to-one-writer.md) names, carries
  a TTL expiry computed from the receipt's creation time and that retention.
- TTL deletion runs some time after expiry
  ([how TTL works](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/howitworks-ttl.html))
  [snippet], so every reader treats an expired record as absent.
- Records written before this change get their expiry from the one-time backfill that
  [ADR-080](adr-080-confine-each-audit-and-evaluation-record-family-to-one-writer.md) §5 orders
  before its resource policy.

### 6. Retired in the same slices

The browser's correlation-identifier generator and header, every server hop's adoption of a
client-supplied correlation value, and the client-header clause of the request contracts.

## Consequences

- A user can quote a reference, an operator can find it in the logs, and an investigator can fetch
  the whole turn with it. None of those steps grants access by itself.
- The access log holds reviewer identities for the retention period. Its readers are an access
  question for the account, stated in [observability](../concepts/observability.md).
- Records expire. Evidence needed beyond the retention must be exported before it expires; this
  decision provides no legal-hold mechanism.


## Alternatives considered

| Alternative | Why rejected |
|---|---|
| Stamp the principal on a tool's log line | Makes CloudWatch the attribution record with a two-week retention, against ADR-059. |
| Store only a keyed hash of the principal | Breaks ADR-077's raw-`sub` receipt and cannot erase one user. |
| A pseudonymous per-user index | Answers "what did user X do", which no customer has asked for yet; revisit when one does. |
| An admission-time receipt plus a terminal companion | Covers a killed function too, at two writes per turn and a contract change. |
| Log the authorizer subject on every route | Records every end user's identity in a log, against ADR-077 §6. |
| A per-access review table | Stronger than an access log, but more code for the same accountability question. |
| Write-once backup vaults | Conflicts with declared expiry. |

## Review trigger

Revisit when a customer needs legal hold, per-user queries, or erasure of one user, or when audit
coverage shows killed-function gaps that matter.
