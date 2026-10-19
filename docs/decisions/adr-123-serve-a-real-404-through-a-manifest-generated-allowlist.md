# ADR-123: Serve a real 404 through a manifest-generated allowlist
Date: 2026-10-07
Status: Proposed. Part of the UI and information architecture design. The owner decided on 2026-10-05
that unknown paths get a real 404 page through `StaticRouteRewrite`, and approved that `infra/` change.
The status-code rule in decision 3 was put to the owner as an open decision with a recommendation and
taken as recommended, without owner objection. Not implemented.

## Context

An unknown path returns raw Amazon Simple Storage Service (Amazon S3) XML today, as the UX review of
the dev deployment (2026-10-05) found. The `StaticRouteRewrite` CloudFront Function appends
`index.html` to every extensionless path, so an unknown path asks Amazon S3 for a key that doesn't
exist [repo: `infra/lib/stacks/frontend-stack.ts:143-165`]. The Amazon CloudFront distribution
deliberately has no custom error responses, because they apply to the whole distribution and would
turn the authorizer's 403, which triggers a token refresh, into a 200 with HTML
[repo: `infra/lib/stacks/frontend-stack.ts:196-201`].

## Decision

**`StaticRouteRewrite` rewrites only the routes in the generated route manifest. Every other
extensionless path is served `/404.html`, a static page in the app shell.**

1. **Allowlist.** At synth, the stack reads the committed allowlist projection that
   [ADR-115](adr-115-organise-the-app-into-customer-question-hubs-from-one-navigation-file.md) generates
   under `contracts/generated/`. Manifest routes and `/` rewrite to their `index.html`; any other
   extensionless path rewrites to `/404.html`; paths with an extension pass through, as today.
2. **The page.** h1 "Page not found", "This page doesn't exist or has moved.", a primary "Go to
   Overview" button and the customer hub list. The button names the tab the root route serves, so
   it reads "Go to Campaigns" until Overview ships at `/`. Signed in, it has the full shell and the
   docked assistant with no context chip. A signed-out visitor to an unknown path is sent to sign-in,
   as the frontend session rules require (`.kiro/steering/frontend-auth-session.md`). The signed-out
   404 shell shows only briefly before the redirect, or when session status is unavailable. It has no
   assistant, Alerts or Sign out, and the top bar offers "Sign in". (Orchestrator decision,
   2026-10-07, under the owner's delegation. A signed-out 404 that stays on screen would need an
   exception to those rules, and that's left to the owner.)
3. **Status code.** Return 404 if CloudFront Functions can set it with the page body; otherwise serve
   with 200 and record that. A CloudFront Function can generate or modify a response's `statusCode`,
   and a viewer-response function that sets no body returns the cached or origin body
   [verified 2026-10-07: Amazon CloudFront Developer Guide, "Event structure" for CloudFront Functions,
   response object, https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/functions-event-structure.html].
   So the candidate is a viewer-response function on the default behavior that sets 404 when the
   request was rewritten to `/404.html`. The guide describes the event's request object as the
   request CloudFront received from the viewer, and doesn't say whether a viewer-request rewrite
   shows in the viewer-response event [verified 2026-10-07: same page, "Request object"]. Whether it
   does is still [unverified], so the built function doesn't depend on it: it resolves the event's
   URI with the same allowlist as the rewrite, and both the rewritten `/404.html` and the original
   unknown path resolve to the not-found page. It changes only a 200, so a 304 or an Amazon S3 403
   passes. The dev probe records the status the viewer gets; if it isn't 404, the fallback applies.
4. **No distribution-wide error responses.** They stay out, as today.

## Consequences

- An unknown path shows a designed page instead of XML.
- A route can't be reached unless it's in the manifest, so a page can't ship without its manifest
  entry. The both-ways build test keeps the two in step.
- If the status stays 200, crawlers and monitors see unknown paths as found. That's recorded on the
  slice, not hidden.

## Alternatives considered

- **Distribution-wide custom error responses for 403 and 404.** They'd rewrite the authorizer's 403
  that the client depends on.
- **A Lambda@Edge origin-response function.** More moving parts and cost for a static page.

## Review trigger

Reopen if a route must be served without being in the manifest.
