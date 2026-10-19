# Protected Quality Review API — architecture

This companion owns the protected review boundary's components and request flow. The
[capability overview](README.md) summarizes the routes as a black box; the
[low-level design](low-level-design.md) owns the routes, their entitlements, and their schemas. Diagram notation is owned by the [documentation conventions](../../conventions.md).

## 4. Internal structure

**Component diagram — Protected Quality Review API authorization topology, request-path viewpoint, component depth.** One authorizer verifies either credential and chooses the entitlement from the matched route; one review function returns only the sections those verified levels allow, and the stage records who asked.

![Component diagram: Protected Quality Review API authorization topology. An entitled reviewer sends a cookie or bearer request to CloudFront, whose function refuses a non-canonical API path; a planned web ACL and origin-verify header are shown dashed. The HTTP API stage sends the route key and credential to the route authorizer, which verifies the review-level claim the Cognito token trigger adds to the Cognito-signed token. The stage then invokes the review function with the authorizer context, the function reads the quality record tables with key-bounded reads, and the stage writes each request's reviewer and attribution flag to the reviewer access log.](diagrams/quality-review.svg)

> **Takeaway.** Only the authorizer turns a credential into review levels, only the review function
> turns levels into sections, and neither ever grants a reviewer AWS credentials or a broad
> quality-store role.

Cognito groups `quality-reviewer` and `audit-attribution` are the entitlement source. The
pre-token-generation V2 trigger converts group membership into the closed access-token
`quality_review_levels` claim. Cognito applies V2 trigger customizations to human access tokens in
Essentials/Plus pools ([AWS documentation](https://docs.aws.amazon.com/cognito/latest/developerguide/user-pool-lambda-pre-token-generation.html)).
Group assignment and removal is the Cognito administrator lifecycle; refreshed sessions receive
current group-derived levels. The rules are the
[authorization contract](low-level-design.md#authorization-contract-adr-078), and the stage's record
is the [reviewer access log](../../concepts/observability.md#38-reviewer-access-log-adr-081).

### Edge and command-line client (ADR-078; the command-line client not yet implemented)
- **Edge.** The CloudFront distribution refuses a non-canonical API path (deployed in dev
  2026-09-28), passes every request through its web ACL, and adds the origin-verify header the
  API requires (deployed and dev accepted 2026-09-29,
  [edge entry](../../concepts/authentication.md#edge-entry-adr-078)).
- **Command-line client.** It runs the PKCE loopback login, holds tokens in memory only, calls only
  the CloudFront URL, and offers `turn`, `turns`, and `from-log`.

## 5. Key runtime flow

1. CloudFront refuses a non-canonical API path and forwards the request to the HTTP API.
2. API Gateway and the authorizer admit the request only as
   [authentication](../../concepts/authentication.md#credential-binding-adr-078) defines.
3. The authorizer chooses the entitlement from the matched route key and passes the verified
   review levels, and the access log's reviewer and attribution keys, in its context.
4. The review function rechecks every section against those levels before returning it, and never
   follows a returned reference into protected content.
5. The stage access log records the request.

The rules for steps 3 and 4 are in the [authorization contract](low-level-design.md#authorization-contract-adr-078).
