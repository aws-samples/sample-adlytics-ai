---
inclusion: fileMatch
fileMatchPattern: ["src/adapters/inbound/auth_handlers/**"]
description: Governs Cognito-backed cookie sessions, authorizer verification, CSRF, refresh rotation, and logout revocation when editing backend authentication handlers.
---

# Auth handlers

Boundary concept: docs/concepts/authentication.md. Decision, SameSite analysis,
and verified token-shape facts: ADR-007 (with its 2026-09-25 amendment).
Session rules and the identity model belong in domain/identity; these handlers
are the adapter around them.

## Tokens never reach JavaScript

That property is the entire point of this design. Everything below protects it.

- Never put a token in a response body, a URL, a query parameter, a redirect
  fragment, or a log line.
- Never return the raw Cognito response to the caller. Exchange happens here and
  only the cookies leave.
- Refresh happens server-side. The refresh token never reaches the browser as
  readable data.
- Never authenticate from the browser. Amplify JS and amazon-cognito-identity-js
  return tokens to JavaScript, which reverts this design silently — nothing fails
  and no test catches it. Every InitiateAuth and RespondToAuthChallenge call
  happens here, server-side. This holds whether login uses Cognito managed login
  or a UI of our own. See ADR-007.
- Never issue a token to browser JavaScript. A bearer token is obtained only by a
  non-browser client through the public PKCE client ADR-078 names.

## Keep the login UI swappable

Which login surface is in use is an ADR-007 decision, not a fact for this file.
These two rules make swapping it cheap and are expensive to retrofit.

- Issue cookies in exactly one place. Every login path, present or future, ends in
  the same session-creation step. Never duplicate cookie issuance per path.
- Never assume a redirect is how login starts. How the frontend begins login is
  owned by frontend-auth-session.

## The cookies

Cookie attributes are built by the cookie policy in src/domain/identity/cookies.py,
from the configured cookie scope; SameSite is fixed there. Never set or override
Domain, Secure, Path or SameSite at a call site, and never assume a scope: a host with a public suffix cannot carry a parent-domain
cookie, so the scope is a config decision the policy reads.

Cookie names, attributes, and lifetimes are declared in src/domain/identity;
never restate a TTL or attribute set here or at a call site.

- Never put anything sensitive in adl_csrf: JavaScript can read it, by design.
- adl_txn carries the in-flight login's state and PKCE verifier and exists only
  between /auth/login and /auth/callback.

Rules specific to adl_txn:

- Clear it on both the success and failure paths of the callback. A verifier that
  outlives its single use is a credential with no remaining purpose.
- Never issue it from build_session_cookies. It is not part of a session, and
  bundling it there would make it outlive the exchange.
- Never relax it to non-HttpOnly. A script that could read the verifier could
  complete an intercepted code exchange, which is the one thing PKCE prevents.

## What the authorizer verifies

Order matters — never validate claims before the signature.

0. Compare the CloudFront origin-verify value before anything else, and deny on
   mismatch.
1. Extract exactly one credential: the adl_session cookie, or an Authorization
   Bearer token. Deny a request that carries an adl_session cookie and a bearer,
   or neither.
2. Select the signing key by kid from the Cognito JWKS, then verify the signature.
3. Validate iss, exp, that token_use is access, and the credential's client: the
   cookie only with the BFF client, the bearer only with the public client and
   only on the review routes ADR-078 names.
4. For mutating methods on the cookie credential, validate CSRF by double-submit.

Then return allow with a context carrying the subject and correlation ID.

- Never use the built-in HTTP API JWT authorizer. It reads a bearer token from a
  header or query parameter and has no cookie support. This must be a REQUEST
  type Lambda authorizer.
- Never trust an identity value from a request body, query parameter or custom
  header. The only identity sources are the adl_session cookie and the standard
  Authorization header, verified as above.
- Every unauthenticated auth route performs the origin-verify comparison itself
  before any other work; never assume the authorizer covered a route it is not
  attached to.
- adl_session carries an access token, so its audience claim is client_id. Never
  validate aud on it — an access token has no aud claim; ADR-007's 2026-09-25
  amendment owns that verified vendor fact and its failure modes.
- A user pool signs access tokens and ID tokens with two different key pairs
  (verified vendor fact, ADR-007 amendment). Never assume a pool has one signing
  key; always select by kid.
- Cache the JWKS keyed by kid. Treat a correct iss with an unrecognised kid as
  key rotation: refresh once and retry before rejecting. A pinned key fails
  closed on rotation and takes the whole platform down.
- Never emit an allow policy broader than the route being authorized.

## Refresh and revocation

- Rotate adl_session on every refresh. A reused session cookie after refresh is
  a replay window.
- On logout, revoke at Cognito as well as clearing the cookies. Clearing cookies
  alone leaves a token that still validates.
- Never refresh in a loop. One refresh attempt per request, then fail.

## CSRF

- Validate the double-submit token on every mutating request, not only when
  SameSite is None. Belt and braces here is cheap.
- The browser echoes adl_csrf in the header named by CSRF_HEADER in
  src/domain/identity/authorization.py. Never read it from a body or a query
  parameter.
- Also validate Sec-Fetch-Site, falling back to Origin when it is absent. Treat
  neither being present on a cookie-credentialed mutating request as a denial.
- Never accept a mutating request whose CSRF token is absent. Absent is a
  failure, not a default.
- /auth/refresh and /auth/logout cannot sit behind the session authorizer — one
  runs with an expired access token, the other is tearing the session down. Both
  therefore perform these checks themselves. Never assume the authorizer covered
  a route it cannot be attached to.
