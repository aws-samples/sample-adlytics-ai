# ADR-007 — Cognito with a cookie-based BFF and one identity chain

| | |
|---|---|
| **Status** | Accepted — amended 2026-09-25 (denial semantics; [ADR-077](adr-077-carry-the-end-user-identity-to-every-hop-through-token-exchange.md)) and 2026-09-26 ([ADR-077](adr-077-carry-the-end-user-identity-to-every-hop-through-token-exchange.md) slices 2–5; [ADR-078](adr-078-serve-quality-review-to-programmatic-callers-through-the-cloudfront-edge.md), decided, not yet implemented) |
| **Date** | 2026-08-18 |
| **Last amended** | 2026-09-26 |
| **Decision** | **Cognito is the sole identity provider. Tokens are held server-side and never reach JavaScript; the browser carries an `HttpOnly` session cookie. A custom Lambda authorizer validates every API request.** The verified Cognito subject scopes the Harness actor and Memory. The route exchanges the user's verified access token for a single-turn Harness token and invokes the Harness with that token as the only credential; the Harness exchanges it for a Gateway token, and every tool verifies that token before it acts ([ADR-077](adr-077-carry-the-end-user-identity-to-every-hop-through-token-exchange.md)). |
| **Related** | [authentication](../concepts/authentication.md) · [Conversational Runtime §5](../capabilities/conversational-runtime/architecture.md#5-key-runtime-flow) · [ADR-006](adr-006-agentcore-agentic-layer.md) · [ADR-054](adr-054-scope-analytical-visibility-to-the-customer-deployment.md) · [ADR-077](adr-077-carry-the-end-user-identity-to-every-hop-through-token-exchange.md) · [finding register](../decision-coverage-registry.md#finding-register-f-nn) F-02, F-03 · [Q-08](../decision-coverage-registry.md#question-register-q-nn) |

> **Amended 2026-09-26 by [ADR-078](adr-078-serve-quality-review-to-programmatic-callers-through-the-cloudfront-edge.md) — a second credential on review routes only (decided, not yet implemented).**
> The browser path does not change: tokens never reach JavaScript, and the browser carries only the
> `HttpOnly` session cookie. The two quality-review routes will also accept an `Authorization: Bearer`
> access token from a public app client that uses authorization code with PKCE and a localhost-only
> callback, for investigators calling from a terminal. The same Lambda authorizer will verify both
> credentials: the cookie only with the BFF client, the bearer only with the public client and only
> on those routes, and CSRF on the cookie only. The "API Gateway built-in JWT authorizer" rejection
> below still holds. In the dev-only identities section below, the validation password flag will
> also enable IAM-authenticated `ADMIN_USER_PASSWORD_AUTH` on the public client, and that section's
> "may never call `AdminInitiateAuth`" will admit that one call for the synthetic personas on the
> public client. The provisioner will assign `quality-reviewer` and `audit-attribution` in place
> of the three retired groups, and the persona set becomes a reviewer, an attributor in both
> groups, an attributor in `audit-attribution` only, and a denied persona, beside the two chat
> personas.

> **Amended 2026-09-25 by [ADR-077](adr-077-carry-the-end-user-identity-to-every-hop-through-token-exchange.md) — current identity boundary and dev validation identities.**
> Two statements here were wrong about current behavior and are corrected in place: the Cognito JWT
> does not authenticate the route's Harness invocation (the Harness authorizer was removed on
> 2026-09-04; the route invokes with the Conversation Function's IAM role), and the dev validation
> section did not record the existing dev-only `USER_PASSWORD_AUTH` flow. ADR-077 mandates the
> user-token hop to the Harness, token exchange onward, and two more dev chat personas on the same
> validation mechanism; all of it is pending probes and implementation.

> **Amended 2026-09-26 by [ADR-077](adr-077-carry-the-end-user-identity-to-every-hop-through-token-exchange.md) — the user-token Harness hop is restored.**
> ADR-077 slice S2 is deployed to dev: the Cognito access token again authenticates the route's
> Harness invocation, the route refuses a token too close to expiry for the turn with this ADR's
> 403, and the two chat personas exist. The current identity boundary and dev validation identities
> sections below are corrected in place; token exchange onward remains pending.

> **Amended 2026-09-26 by [ADR-077](adr-077-carry-the-end-user-identity-to-every-hop-through-token-exchange.md) — the route exchanges the user token (S3).**
> ADR-077 slice S3 is deployed to dev: the route exchanges the verified Cognito access token, once
> per turn, for a single-turn Harness token and presents that as the Harness bearer; the Harness
> refuses a Cognito token. The near-expiry refusal is removed. Chat denials follow this ADR's split:
> 401 only when no `adl_session` cookie arrives, and 403 for every other identity failure,
> including a refused exchange. The S2 marker above is historical; the current identity boundary
> section below is corrected in place, and the Gateway and tool hops remain pending. Live validation
> is owned by [validation results](../../validation/RESULTS.md#identity-chain-slice-s3-acceptance--2026-09-26).

> **Amended 2026-09-26 by [ADR-077](adr-077-carry-the-end-user-identity-to-every-hop-through-token-exchange.md) — the user reaches every tool (slices 4 and 5).**
> The Harness exchanges its route-minted token for a Gateway token, the Gateway admits only that
> token, and every tool verifies it before it reads the call. Tool-target authorization is now a hop
> in the user chain. The context and current identity boundary sections below are corrected in
> place; the S3 marker above is historical. Live validation is owned by
> [validation results](../../validation/RESULTS.md#identity-chain-slice-4-and-5-acceptance--2026-09-26).

## Context

The platform is multi-user, single-tenant per customer AWS account. Every user exists in Cognito.

The token-storage question is the one with a security consequence. A single-page app that holds an access token in `localStorage` or in a JavaScript-reachable variable makes any successful XSS a token theft, and therefore a full session compromise. Refresh tokens make it worse, because the attacker gets persistence rather than a window.

The second question is where verified identity remains authoritative. It scopes route access, the Harness actor, conversation continuity, and Memory, and it reaches each Lambda tool target as a token the tool verifies; [Conversational Runtime §5](../capabilities/conversational-runtime/architecture.md#5-key-runtime-flow) owns that current topology.

## Decision

### Tokens live server-side; the browser gets a cookie

The browser holds an `HttpOnly`, `Secure`, `SameSite=Lax` session cookie. The API exchanges it for Cognito tokens server-side. **No token is ever readable from JavaScript.**

This makes XSS a serious bug rather than a session compromise. It does not make XSS harmless — see the CSP consequence below.

A **custom Lambda authorizer** validates the cookie on every request, rather than API Gateway's built-in JWT authorizer, because the identity source is a cookie rather than an `Authorization` header. Detailed flow, cookie attributes and CSRF handling are owned by [authentication](../concepts/authentication.md); this ADR records only the decision and its consequences.

> **Amended 2026-09-25 — this ADR now owns the measured denial semantics and the
> verified Cognito token-shape facts.** Both were previously recorded in the retired
> auth-flow document (measured and verified there 2026-08-18); the 2026-09-04 concept
> rewrite removed that home, so the durable record moves here.
>
> **Denial is not uniform, by measurement and then by decision** [verified 2026-08-18
> against the deployed dev stack]. The authorizer's identity source is
> `$request.header.Cookie`. When no `Cookie` header is present, API Gateway rejects
> the request itself with **401** and the authorizer is never invoked; only a request
> carrying a cookie reaches the authorizer, whose rejection is **403**. The split was
> kept deliberately rather than flattened to a uniform 403 (dropping the identity
> source would have restored uniformity): 401 and 403 already mean exactly this in
> HTTP — credentials not presented versus credentials rejected — and the earlier
> uniformity argument was inference, not measurement. Consequences the frontend
> depends on: 401 on an authenticated route means there is no session to refresh
> (go to login); 403 means attempt one refresh, then retry once. **An expired session
> yields 403, not 401**, because `adl_refresh` outlives `adl_session` and keeps a
> `Cookie` header present — that lifetime ordering is what makes refresh reachable,
> nothing in cookie-building code enforces it, and the integration tier asserts it.
> Retrying a 403 from an unauthenticated route (`/auth/refresh` answers 403 on CSRF
> failure) is an infinite loop and is forbidden.
>
> **Amended 2026-09-28 by [ADR-078](adr-078-serve-quality-review-to-programmatic-callers-through-the-cloudfront-edge.md)
> — the API's no-credential 401 is withdrawn until the edge slice.** The review routes admit a
> bearer that carries no cookie, so the cookie cannot remain the authorizer's identity source, and
> an HTTP API authorizer cannot declare "cookie or bearer". The authorizer therefore has no identity
> source, and every request to an authorized API route reaches it: a request carrying no credential
> is now refused **403**, and each such request costs an authorizer invocation. The frontend's rule
> still converges: a 403 triggers one refresh, which fails without cookies, and the browser then
> begins login, one round trip later than before. The conversation route is unaffected: it
> enforces its own 401 when no `adl_session` cookie arrives.
>
> **Amended again by the ADR-078 edge slice (2026-09-29): the identity source is the origin-verify
> header.** CloudFront adds it to every API request, so a browser request with no credential
> still reaches the authorizer and is refused **403**; the frontend rule above is unchanged, and
> the API answers **401** only to a request that bypassed CloudFront without the header
> ([authentication](../concepts/authentication.md), edge entry).
>
> **Amended 2026-10-05 by the ADR-078 path-scoped body-size exclusion — an HTML 403 is an edge
> refusal.** The edge web ACL answers a block with its own HTML page and a 403, so the refresh rule
> above would resend a blocked request and then begin login, losing whatever the page holds. A 403
> whose media type is `text/html`, on the first or the retried request, is therefore returned to
> the caller with no refresh and no login, and the page shows its own error state. Every other 403,
> including one with no content type, keeps the refresh-then-retry rule, so an expired session is
> never misread as an edge block. The rule fails toward the earlier behaviour: if the block page
> stopped being HTML, a block would refresh as before.
>
> **Cognito token-shape facts** [verified 2026-08-18 against the Cognito developer
> guide, "Verifying a JSON Web Token"]: an access token has **no `aud` claim** — its
> audience is `client_id`, and validating `aud` on it either rejects every request or
> passes silently, an authorization defect no test failure announces. A user pool has
> **two signing key pairs** (access and ID tokens); there is no single pool key to
> pin. **Keys may rotate**: cache the JWKS keyed by `kid`, and treat a correct `iss`
> with an unrecognised `kid` as rotation — refresh once and retry before rejecting.
> These are vendor facts that can move; re-verify against current Cognito
> documentation in any change that depends on them, and update this amendment's date
> when re-verified.

### The login screen is Cognito managed login, with branding

Added 2026-08-18, closing [Q-15](../decision-coverage-registry.md#question-register-q-nn). Managed login with the branding editor — CSS settings and image assets up to a 2 MB request, against the classic hosted UI's 135 KB.

A login UI of our own is possible and is explicitly left open as a later upgrade. It is not taken now for one reason above the others: **`InitiateAuth` cannot sign in a federated user**, so a custom UI forecloses corporate SSO against Okta or Entra ID unless the managed flow is kept alongside it. For a product sold to companies that is a product capability, not an implementation detail. Managed login also handles MFA, password reset, email verification and account recovery, and keeps plaintext passwords out of our code entirely.

**Two constraints exist so the upgrade stays cheap.** Both are cheap now and expensive to retrofit:

- **Cookie issuance lives in exactly one place.** The managed-login callback and any future credentials handler must end in the same "issue three cookies" step, so adding a login path never means reimplementing session creation.
- **The frontend never assumes a redirect.** Login is "navigate to wherever config says", so replacing a redirect with an in-app form is a routing and config change.

> **However it is built, authentication happens server-side.** Amplify JS and `amazon-cognito-identity-js` return tokens to JavaScript, which reverts this entire ADR with nothing failing and no test catching it. Every `InitiateAuth` and `RespondToAuthChallenge` call belongs in the BFF.

### Custom domains are a hard requirement, not a preference

`SameSite` is evaluated at the **registrable domain** level, not the origin. `app.customer.com` to `api.customer.com` is same-site and works. `app.customer.com` to `abc.execute-api.us-east-1.amazonaws.com` is **cross-site**, so the browser silently omits the cookie and every request 401s.

> **This design does not degrade without custom domains — it fails, and it fails silently.** The fallback is `SameSite=None; Secure`, which makes CSRF defence mandatory rather than belt-and-braces: double-submit token plus `Origin` and `Sec-Fetch-Site` validation on every mutating request. See F-02 and F-03.

### Current identity boundary — amended 2026-08-24, corrected 2026-09-25 and 2026-09-26

The Cognito JWT authenticates the browser's route request. The route exchanges it, once per turn,
at the ADR-077 token service for a single-turn Harness token, which is the only credential on the
route's Harness invocation; the Harness refuses SigV4 and a Cognito token. Chat denials follow the
split above: 401 only when no `adl_session` cookie arrives, 403 for every other identity failure.
The route derives the Memory actor from the verified
subject alone and the Harness session per conversation. (Corrected 2026-09-25: the Cognito JWT
authenticated the route's Harness invocation when live-verified on 2026-08-23 under
[ADR-038](adr-038-the-conversation-route-streams-from-a-function-url.md) and stopped doing so when
the Harness authorizer was removed on 2026-09-04. Corrected 2026-09-26:
[ADR-077](adr-077-carry-the-end-user-identity-to-every-hop-through-token-exchange.md) §2 restored it
in slice S2, and slice S3 the same day replaced the forwarded Cognito token with the route-minted
token.)
The user's identity also authenticates the later Gateway tool call (corrected 2026-09-26, ADR-077
slices 4 and 5). The Harness exchanges its token for a Gateway token, the Gateway admits only that
token, and every Lambda tool verifies it before it reads the call.
[Conversational Runtime §5](../capabilities/conversational-runtime/architecture.md#5-key-runtime-flow) is the sole current owner of
the complete route, Harness, Memory, Gateway, and tool-target topology.

The local consequence for this ADR is that browser, route, Harness, Gateway, and tool
authentication are one user chain. The verified user is attribution, not entitlement. **Never accept a user,
actor, tenant or brand identifier as a tool parameter:** a model-authored value cannot establish
identity. Analytical adapters use the deployment-wide server-owned scope accepted by
[ADR-054](adr-054-scope-analytical-visibility-to-the-customer-deployment.md).

### Historical identity-to-tool design — superseded 2026-08-24

This section preserves the original 2026-08-18 decision history and is **not the deployed boundary**. It said that one identity source was enforced at every boundary, with the same Cognito token authenticating Gateway inbound JWT, Outbound Auth reaching the Lambda target on behalf of that user, and adapter code enforcing row-level scope from propagated identity. It also described identity as travelling beside the request and treated Gateway "authenticate only" mode as the selected mechanism.

That design superseded an even earlier manual-propagation assumption in [ADR-006](adr-006-agentcore-agentic-layer.md), but deployment evidence later showed that the Harness of that time called the IAM-authenticated Gateway with its execution role and that the Lambda target received no Cognito subject or session assertion. The historical prohibition on model-authored identity remains correct. The side channel proposed then did not exist in that path; since 2026-09-26 a signed bearer reaches the target instead ([ADR-077](adr-077-carry-the-end-user-identity-to-every-hop-through-token-exchange.md)).

### Dev-only Quality Review validation identities — amended 2026-09-03, corrected 2026-09-25 and 2026-09-26

The dev Quality Review acceptance harness may persist only six `invalid.example` Cognito users whose
usernames begin with `adlytics-quality-review-`: four Quality Review personas and, since 2026-09-26,
two group-less chat personas. Their generated password is held only in an exact dev
SSM `SecureString`; the provisioner may use `AdminSetUserPassword` solely to set that already-stored
password permanent for those exact synthetic identities. It may assign only the three documented Quality
Review groups and may never call `AdminInitiateAuth`, enable any other password sign-in, reset a
non-prefixed user, list pool identities, or emit credentials/tokens.

The only password sign-in is `USER_PASSWORD_AUTH` on the BFF app client, used solely by those
`adlytics-quality-review-` synthetic personas in dev. It is enabled only by the dev-only
`ADLYTICS_QUALITY_REVIEW_VALIDATION_PASSWORD_AUTH` flag, which synth refuses for any environment other
than `dev`. (Corrected 2026-09-25: this section previously said the harness may never enable a password
grant, which did not record this existing flow.)
[ADR-077](adr-077-carry-the-end-user-identity-to-every-hop-through-token-exchange.md) §7 extends the same
dev-only mechanism with the two chat personas (provisioned 2026-09-26), which identity-chain probes
sign in as.

Browser validation still uses Cognito managed login and the existing authorization-code/BFF callback, so
tokens remain outside JavaScript and no general admin-token-minting path is introduced.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| **Token in `localStorage` or JS memory** | Any XSS becomes full session compromise. The entire reason this ADR exists |
| **API Gateway built-in JWT authorizer** | Reads the `Authorization` header. Our credential is a cookie, so it does not apply |
| **Identity as a tool argument** | Simplest to build and the most dangerous option here. A model-authored value is not a trusted identity assertion |
| **IAM-based per-user authorization** | IAM authenticates the Harness execution role at Gateway and has no Cognito-user row granularity |
| **Cognito as an MCP authorization server for external clients** | Cognito is an OIDC provider, not an MCP-compliant authorization server. Not a constraint for us, since our own frontend reaches tools through our API rather than as an MCP client |
| **Default API Gateway hostnames** | Cross-site under `SameSite`. Cookies are never sent |
| **A login UI of our own, now** | Forecloses federated SSO, since `InitiateAuth` cannot serve a federated user, and moves MFA, password reset, verification, recovery and credential-stuffing defence into our code. Deferred rather than rejected — see the upgrade constraints above |
| **A browser-side custom login UI** | Returns tokens to JavaScript and silently reverts this ADR. The most dangerous option here precisely because nothing would appear to break |
| **Classic hosted UI** | First-generation surface, no passkey support, and branding limited to a 135 KB CSS-and-logo upload |

## Consequences

- **A registrable parent domain is a prerequisite for the first authenticated request**, not a phase-5 nicety. This promotes `D-07`.
- **CSP becomes load-bearing.** Cookies remove token theft from the XSS blast radius but leave request forgery from within the page. A strict `Content-Security-Policy` is required defence-in-depth, which is one reason frontend hosting must expose response-headers policy control — see [ADR-008](adr-008-frontend-hosting.md).
- **The authorizer cache TTL is a revocation staleness window.** A revoked or disabled user retains route access for the TTL. It is not a tool-target identity control.
- Cognito managed login requires the Essentials tier or above.
- Cognito bearer tokens default to 60-minute validity, so the server-side exchange must handle refresh without a user-visible interruption.
- If CloudFront fronts the API, the cache policy must explicitly forward the `Cookie` header, and authenticated paths must use `CachingDisabled`. Otherwise the authorizer receives nothing.
- Per-user Memory and deployment-wide analytical visibility are settled by [ADR-054](adr-054-scope-analytical-visibility-to-the-customer-deployment.md). Per-subject analytical scope remains unsupported without a trusted identity transport and governed assignments.

## Review trigger

- login look-and-feel becomes a hard requirement, such as per-customer white-labelling. The upgrade is a custom form through the BFF, and it must keep the managed flow alongside if federated SSO is wanted
- a customer requires SSO against their own IdP, which settles the login-UI question permanently in favour of the managed flow
- custom domains prove unavailable in the customer account, forcing `SameSite=None` and mandatory CSRF defence
- an external MCP client needs direct tool access, which Cognito cannot authorize without a façade
- per-subject analytical visibility becomes required, forcing an accepted trusted identity transport and assignment design
- the Harness or Gateway authentication boundary changes, requiring the current identity chain to be reverified
