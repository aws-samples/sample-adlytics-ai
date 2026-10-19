# ADR-060 — Protect quality-review retrieval with an explicit operator authorization boundary

Date: 2026-09-03
Status: Accepted — superseded in part, deployed and dev accepted 2026-09-28; current state is the
[Protected Quality Review API](../capabilities/protected-quality-review-api/README.md). [ADR-078](adr-078-serve-quality-review-to-programmatic-callers-through-the-cloudfront-edge.md)
supersedes the three-route set, the per-level groups, one role per route, "cannot list or
time-search", the browser-only caller rule, and the rejection of a bearer-token API; callers stay
human Cognito users, and no service principal is introduced.
[ADR-081](adr-081-make-every-turn-traceable-and-attributable.md) supersedes "never retain subject" for
the reviewer access log only. Authorization before storage access, the POST selector, the
non-disclosing refusal, the no-scan rule, and the exclusion of protected content stand.

> **Superseded in part 2026-09-28 — deployed.** The two routes, two groups, one role, and the bearer
> credential below replaced the three routes in dev on 2026-09-28, and the three old groups were
> removed on 2026-09-29; the web ACL and origin-verify header followed the same day. The
> three routes this ADR describes are no longer deployed.

> **Amended 2026-09-26 — two routes, two credentials, the edge (decided).** The
> three per-level routes and groups below will be replaced by `turn` and `turns` behind groups
> `quality-reviewer` and `audit-attribution`, served by one role, because the separation bought no
> data boundary: the findings and evaluation roles held the same table-wide `GetItem` and `Query`,
> and the audit role table-wide `GetItem` [verified 2026-09-26, deployed IAM]. Callers will be a browser session or a
> bearer token from a public PKCE client, and every call will enter through CloudFront and WAF.
> ADR-078 owns the decision; until it lands, the routes below are the deployed ones.

> **Refined 2026-09-14 — projection content.** The three routes and their authorization boundary are
> unchanged. What each projection carries is owned by
> [ADR-064](adr-064-retire-planner-era-contracts-and-anchor-the-audit-trajectory.md): `audit` is what
> happened and its chain of custody, `findings` is the defect pattern beside the trajectory step that
> produced it, and `evaluation` is one result per evaluator behind the turn outcome and protected chain.

## Context

[ADR-059](adr-059-create-a-bounded-agent-quality-audit-loop.md) created immutable content-minimized
terminal audit records and a restricted storage read model. An exact audit identifier or normalized
correlation lookup is a data-access primitive, not a user authorization model. The existing
[authentication flow](../concepts/authentication.md) already gives Cognito users HttpOnly cookie sessions and a custom
REQUEST authorizer, but it previously carried only subject/correlation context and had no quality-review
entitlement or route-specific data boundary.

Quality review has three distinct record families: the terminal audit receipt, structural findings, and
evaluation-linkage state. Giving one generic handler or reader access to all three creates needless
blast radius. Direct table access, a customer chat route, CloudWatch/trace access, broad search, or a
protected evaluation envelope would widen access beyond the retained redacted boundary.

## Decision

**Create three POST Quality Review routes behind the existing cookie-session custom authorizer, with one
independent Cognito entitlement and one least-privilege Lambda role per route.**

| Route | Required level | Exact backend reads |
|---|---|---|
| `/api/quality-review/audit` | `audit` | exact audit receipt or bounded correlation-index discovery plus exact attestation |
| `/api/quality-review/findings` | `findings` | audit reads plus bounded `FINDING#` partition query after receipt attestation |
| `/api/quality-review/evaluation` | `evaluation` | audit reads plus exact admission, delivery, writer, and result companion reads |

Cognito groups `quality-review-audit`, `quality-review-findings`, and
`quality-review-evaluation` are the entitlement source. A pre-token-generation V2 Lambda trigger
projects their union into the signed access-token `quality_review_levels` array. The existing custom
authorizer verifies the cookie access token and standard claims first, validates this closed claim, maps
the exact request path to one required level, and denies before a route handler accesses storage. Group
assignment and removal remain a Cognito administrator responsibility; a refreshed session carries the
current group-derived levels. Human Cognito users are the only v1 callers; no bearer-token or service
principal is introduced.

All routes accept exactly one opaque audit ID or normalized correlation selector in a POST body. The
response is a versioned redacted projection only. Invalid selectors are distinct validation outcomes;
absent data, non-unique correlation, and unauthorized discovery receive the same non-disclosing response.
No response grants or implies access to referenced protected records. Logs and metrics retain only
bounded route/outcome categories; they never retain subject, claim, selector, correlation, or content.

## Consequences

The platform gains programmatic, route-specific quality review for Cognito users without granting them
AWS credentials or a generic quality-store role. Each route needs its own handler, test coverage,
least-privilege IAM, and source-record projection allowlist. It adds a retained Cognito group lifecycle
and a token trigger; token refresh/re-authentication is required before group changes reach a session.

The API intentionally cannot list, time-search, free-text search, mutate, access queues/logs, or retrieve
protected question/answer/evaluator material. It trades flexible browsing for non-enumerability and a
small, auditable data surface.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| Give reviewers direct audit-table access | Bypasses caller authorization, redaction, non-enumerability, and route-level least privilege. |
| One broad review endpoint and role | Gives every route access to records it does not need. |
| Extend a customer chat route | Blends customer and quality-review trust boundaries. |
| Use CloudWatch logs/traces as the review database | Those sinks are not the retained audit contract and may hold protected content. |
| Return protected evaluation envelopes | Widens raw-content custody and duplicates a separately governed source. |
| Bearer-token API or direct user-pool JWT authorizer | Breaks the established HttpOnly BFF-cookie boundary. |

## Review trigger

Revisit this decision when a new review record family, reviewer class, service caller, claim source,
retention/legal access policy, multi-turn record, or protected-content expansion is proposed.
