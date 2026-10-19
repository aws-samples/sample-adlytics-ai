# Protected Quality Review API

| | |
|---|---|
| **Level** | L2 — protected operator capability |
| **Status** | Implemented and dev accepted (2026-09-28): two Cognito-entitled routes, `turn` and `turns`, serve cookie and programmatic bearer callers ([validation results](../../../validation/RESULTS.md#review-api-slice-6-commit-a--2026-09-28)). The web ACL and origin-verify header ADR-078 also mandates are deployed and dev accepted (2026-09-29, [validation results](../../../validation/RESULTS.md#edge-entry-adr-078-slice-7--2026-09-29)); the command-line client is not yet implemented. |
| **Last updated** | 2026-09-28 |
| **Owns** | Cognito-user authorization of the review routes, the entitled sections of one turn, the turn listing, and their data access. |
| **Conventions** | [Documentation conventions](../../conventions.md) |

## 1. Purpose and scope

The Protected Quality Review API gives an entitled Cognito user a consistent programmatic view of
one turn's quality records, and a bounded listing of turns, without making audit storage,
evaluation logs, or protected source content a browser data plane. A browser user presents the
HttpOnly cookie session; a programmatic user presents a bearer from the public PKCE client. The
custom authorizer verifies either, chooses the entitlement from the matched route, and the review
function returns only the sections that entitlement allows.

It does not own quality-record persistence, evaluation transport/result sinks, raw
conversation/evaluation custody, or customer conversation routes. Those facts remain with the
[Agent Quality Loop](../agent-quality-loop/README.md),
[Observability](../../concepts/observability.md), and
[authentication flow](../../concepts/authentication.md).

## 2. Where this fits

This capability refines the Protected Quality Review API row in the [L1 capability index](../../architecture.md#capability-index).
It is a separate internal-facing API boundary adjacent to the Agent Quality Loop. Callers are human
Cognito users, on the cookie session or a bearer as
[authentication](../../concepts/authentication.md#credential-binding-adr-078) binds them; browser
JavaScript never receives a token.

## 3. Black-box contract

| Route | Inbound | Outbound |
|---|---|---|
| `POST /api/quality-review/turn` | One exact `audit_id` or correlation selector | One turn's sections, filtered by the caller's entitlement |
| `POST /api/quality-review/turns` | A bounded time window, filters, a page limit, and a cursor | One page of turn summaries and a cursor; never the principal |

A correlation that discovers zero or multiple receipts has the same non-disclosing outcome as an
absent or unauthorized record. A response never carries raw question/answer/prompt/evidence/evaluator
content, a CloudWatch record, a queue message, an opaque reference capable of expanding access, or a
mutation operation. Entitlements, sections, paging, and storage access are owned by the
[low-level design](low-level-design.md#authorization-contract-adr-078). A repository command-line
client, when built, is a client of this contract, not a second path to the data.

## Documentation map

- [Architecture](architecture.md) owns the Cognito claim, authorizer, route, and backend boundaries.
- [Low-level design](low-level-design.md) owns exact request/response contracts and refusal semantics.
- [ADR-060](../../decisions/adr-060-protect-quality-review-retrieval-with-an-explicit-operator-authorization-boundary.md)
  owns why this is a separate authorization/data boundary.
- [ADR-078](../../decisions/adr-078-serve-quality-review-to-programmatic-callers-through-the-cloudfront-edge.md)
  and [ADR-081](../../decisions/adr-081-make-every-turn-traceable-and-attributable.md) own why the
  contract has two routes, a bearer credential, and an attribution section.
- [Agent Quality Loop](../agent-quality-loop/README.md) owns the immutable records consumed here.
