# ADR-054: Scope analytical visibility to the customer deployment

Date: 2026-08-24
Status: Accepted — clarified 2026-08-24; amended 2026-09-25 and 2026-09-26 ([ADR-077](adr-077-carry-the-end-user-identity-to-every-hop-through-token-exchange.md))

> **Amended 2026-09-25 by [ADR-077](adr-077-carry-the-end-user-identity-to-every-hop-through-token-exchange.md) — trusted subject transport.**
> ADR-077 provides the trusted subject transport this decision names as a prerequisite for
> per-subject scope: a signed caller assertion verified in every tool. It is pending probes and
> implementation. Analytical scope remains deployment-wide, and the governed assignment model this
> decision also requires is still absent.

> **Amended 2026-09-26 by [ADR-077](adr-077-carry-the-end-user-identity-to-every-hop-through-token-exchange.md) — the transport exists.**
> The trusted subject transport is deployed and live-validated in dev: every tool verifies a signed
> caller token before it reads the call. It attributes the call and does not narrow the scope.
> Analytical scope remains deployment-wide, and the governed assignment model is still absent. The
> decision paragraph below is corrected in place.

## Context

The platform is deployed once per customer and supports multiple authenticated users. The verified
Cognito subject scopes the Harness actor and Memory namespace, but the current Gateway Lambda target
receives no subject or session assertion. What had not been recorded consistently was whether two
users in the same customer deployment may see different analytical populations.

The current open question stated both that row-level authorization remained undecided and that all
users saw account data. No accepted ADR recorded the latter. This ambiguity becomes visible as soon
as entity resolution exists: a campaign-name search can disclose names and advertiser relationships
before a measurement query runs.

Per-subject analytical restrictions would require both a trusted subject transport to the enforcing
adapter and a source of assignments, administration behavior, resolution filtering, query
enforcement, audit semantics, and cache isolation. No customer requirement, trusted transport, or
governed assignment source currently defines those restrictions.

## Decision

**The initial product authorizes analytical data at the customer-deployment boundary: every
authenticated user in one deployment may resolve and measure every analytical entity published by
that deployment.**

The authenticated subject remains the identity for route access, conversation, preferences, audit
where available, and Memory. It is never an analytical advertiser, brand, campaign, or
commerce-scope key, and the model never supplies it. Since 2026-09-26 the subject reaches each
Gateway Lambda target as a token the tool verifies
([ADR-077](adr-077-carry-the-end-user-identity-to-every-hop-through-token-exchange.md)); it
attributes the call and does not narrow the scope.

Every resolution and measurement adapter constructs an `AuthorizedAnalyticalScope` from packaged,
server-owned deployment configuration before reading candidates or data. That scope is the complete
customer deployment and does not depend on Gateway metadata or a model-authored field. The type and
enforcement boundary exist even though all users currently receive the same scope.

Entity search filters by authorized scope before matching, counting, or returning candidates.
Measurement validates the resolved canonical keys against the same scope. No-match and unauthorized
outcomes use the same customer-facing wording so search cannot be used to enumerate hidden entities
if narrower authorization is introduced later.

Objectives remain customer-authored analytical context, not authorization. Campaign, channel, or
account objective assignments never grant visibility.

A deployment requiring different analytical visibility by user is unsupported until this decision is
revisited and a trusted subject transport plus governed assignment model are accepted. It must not
be approximated with prompt instructions, frontend filtering, source credentials, Gateway operation
metadata, or IAM alone.

## Consequences

The first semantic-resolution implementation needs no user-to-brand or user-to-campaign assignment
store. Search, measurement, and presentation APIs still share one explicit authorization boundary and
cannot accidentally treat absence of restrictions as absence of authorization.

All users in the deployment can see campaign, advertiser, product, brand, marketplace, and metric
information available to that deployment. Customer onboarding and product documentation must state
that boundary.

A future per-subject design can narrow `AuthorizedAnalyticalScope` without exposing identity to the
model, but it must first add a trusted subject transport to the enforcing adapters. It will also
require its own assignment source, administration, adapter-enforced selectors, evaluation cases,
audit behavior, and cache-key review.

Athena or Lake Formation fine-grained controls are not introduced for the initial decision. The
application's adapter boundary remains responsible for analytical scope; infrastructure credentials
authenticate the Harness runtime role and cannot express a Cognito subject's business assignment.
[Agentic-layer §5](../capabilities/conversational-runtime/architecture.md#5-key-runtime-flow) owns the current end-to-end identity and
Gateway topology.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| Leave visibility implicit | Entity search would make the ambiguity user-visible and security-relevant |
| Add brand/campaign restrictions now | No accepted customer requirement, trusted subject transport, or assignment source defines them |
| Let the model pass a user, role, or tenant argument | The argument is forgeable and cannot establish trusted identity |
| Filter only in the frontend | Direct API/tool calls would bypass it and candidate counts could still leak hidden entities |
| Infer visibility from source credentials | Physical access scope does not prove analytical ownership or user authorization |

## Review trigger

Revisit when a customer requires different analytical visibility for users in one deployment, before
a deployment hosts more than one customer, or before an external sharing role is introduced.
