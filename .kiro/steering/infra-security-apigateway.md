---
inclusion: fileMatch
fileMatchPattern: ["**/infra/lib/constructs/api/**", "**/infra/lib/constructs/edge/**", "**/infra/lib/stacks/api-stack.ts", "**/infra/lib/stacks/auth-stack.ts", "**/infra/lib/stacks/edge-security-stack.ts", "**/infra/lib/stacks/frontend-stack.ts", "**/infra/lib/stacks/token-service-stack.ts"]
description: Governs CDK API edge wiring, Lambda authorizer caching, same-origin cookie authentication, CloudFront forwarding, Cognito restrictions, and edge WAF when editing API infrastructure.
---

# API edge infrastructure

Constraints and their evidence: ADR-007 (including its 2026-09-25 amendment)
and ADR-008; F-03 in docs/decision-coverage-registry.md carries the hostname
evidence. Authorizer type, identity source, cache TTL and route
authorization are asserted by infra/test/auth-stack.test.ts; that suite is the
gate, so this file states only what it cannot check.

## The authorizer

- Never narrow the identity source without lowering the cache TTL in the same
  change, and never raise the TTL while the identity source is the whole Cookie
  header. The two are one decision: the TTL is a revocation staleness window, and
  keying on the whole header means any unrelated cookie change already evicts the
  entry. Both current values are asserted by the test suite.
- Any non-zero TTL must be stated to the customer as the window in which a
  revoked session still works.

## One origin, or cookie auth breaks silently

SameSite is evaluated at the registrable domain, and cloudfront.net is a public
suffix (F-03 in docs/decision-coverage-registry.md, verified 2026-08-18), so
cookies are host-only and the API must be same-origin with the app. One
CloudFront distribution serves both, which is what removes F-03's custom-domain
requirement.

- Never serve an authenticated path from a second origin, a default execute-api
  hostname, or any host that differs from the app's. Splitting them makes every
  authenticated request cross-site and the browser omits the cookie — no error,
  no log line, just an unauthenticated request.
- Never introduce a custom domain for the API alone. Moving off host-only cookies
  means changing auth.cookieScope and the cookie builder together.
- If CloudFront fronts the API, forward the Cookie header explicitly and use
  CachingDisabled on authenticated paths. Otherwise the authorizer receives
  nothing.

## Cognito

- Never assume the pool's feature plan supports managed login or passkeys. The
  tier requirement is a vendor pricing fact that moves: verify it against current
  Cognito documentation in the change that depends on it, and record the verified
  requirement as a dated ADR-007 amendment.
- Never provision an identity pool for browser access. Identity pools hand AWS
  credentials to the browser, which is the opposite of this design.
- Never put a client secret in a CDK construct property that lands in the
  synthesised template. Use Secrets Manager and reference it.

## Token service

- The token-service routes are the only unauthenticated routes outside the
  sign-in set. Each is throttled and declared in the TokenServiceStack's own
  unauthenticated set, never added to ApiStack's (ADR-077).
- The token-service REST API is its own edge: attach its WAF to its stage, never
  to a CloudFront distribution in front of it (ADR-077 §3).

## General

- Attach WAF at the edge, not per route.
