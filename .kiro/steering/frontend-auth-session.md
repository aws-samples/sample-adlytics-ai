---
inclusion: fileMatch
fileMatchPattern: ["frontend/lib/**"]
description: Governs browser cookie-only sessions, CSRF propagation, refresh and retry behavior, correlation headers, and same-origin API configuration when editing the frontend client layer.
---

# Frontend session handling

Boundary concept: docs/concepts/authentication.md. Decision and measured denial
semantics: ADR-007 (2026-09-25 amendment).

## There is no token in JavaScript

By design, not by convention. The browser holds cookies it cannot read.

- Never read, store, forward or log an access or refresh token. There is none to
  read, and code that looks for one signals the design was misunderstood.
- Never use localStorage or sessionStorage for anything auth-related.
- Never decode a JWT client-side. If the UI needs the user's name or scopes, the
  API returns them from a session endpoint.
- Send every API request with credentials included, so cookies are attached.

## CSRF

- Read the adl_csrf cookie and echo it in a request header on every mutating
  request. It is the one auth cookie JavaScript may read, and that is its only
  purpose.
- Never send the CSRF value in a query parameter or request body.

## Expired-session handling

Denial is not uniform. 401 means no Cookie header reached API Gateway, so the
authorizer never ran and there is no session to refresh. 403 means a cookie
arrived and was rejected. Measured, not assumed — ADR-007 owns the dated
measurement (2026-09-25 amendment).

- On a 401 from an authenticated route, navigate to login. Never refresh: there
  are no credentials to renew.
- On a 403 from an authenticated route, call the refresh endpoint once, then retry
  the original request once.
- An expired session yields 403, not 401, because adl_refresh outlives
  adl_session and keeps a Cookie header present. Never shorten adl_refresh below
  adl_session; that routes expiry to login instead of refresh.
- Never retry on a 403 from an unauthenticated route. /auth/refresh answers 403
  when CSRF fails, and retrying that is an infinite loop.
- Never loop. Two failures means the session is gone: navigate to login.
- Never retry a mutating request without re-reading the CSRF cookie first. Refresh
  rotates it, so the value held before the refresh is already stale.
- Handle the refresh race: concurrent denials must share one refresh attempt, not
  fire several.

## Correlation

- Generate a W3C traceparent at the edge and send it on every request. Never send
  a correlation ID: the server mints one per request and returns it in a response
  header (ADR-081).
- Treat the traceparent as an untrusted transport hint; browser-to-backend trace
  continuity exists only where the backend explicitly extracts it.
- Never log a cookie value, a full request header set, or a response body that
  could contain user data.

## Configuration

The app and the API are served from one CloudFront distribution, so every call is
same-origin and there is no backend configuration to inject. That is the strongest
version of the no-hardcoded-hostname rule: nothing can point at the wrong host
because nothing names a host.

- Call the API with relative paths only. Never build an absolute URL to our own
  API, and never read a hostname from the environment to construct one.
- Never introduce a build-time backend config value without reinstating the
  validation this section used to carry. The guarantee holds only while no
  hostname is named; an absolute URL brings back every failure mode it removed.
- Never write localhost outside a local development file that no gate or build reads.
- Never construct a Cognito authorize or logout URL by hand from string pieces.
- Never assume login begins with a redirect. Navigate to the login path the app
  exposes, so replacing managed login with an in-app form stays a server-side
  change. See ADR-007.
- The API path prefix must match the backend routing table and the CloudFront
  behaviour pattern. Three places agree on it; change them together.
- The upload runs after the infrastructure deploy, because the bucket and
  distribution IDs come from SSM. See ADR-012.
