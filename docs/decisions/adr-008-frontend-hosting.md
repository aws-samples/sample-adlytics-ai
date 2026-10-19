# ADR-008 — Frontend on CloudFront + S3 + OAC, in CDK

| | |
|---|---|
| **Status** | Accepted — amended 2026-09-26 by [ADR-078](adr-078-serve-quality-review-to-programmatic-callers-through-the-cloudfront-edge.md) (deployed and dev accepted 2026-09-29) |
| **Date** | 2026-08-18 |
| **Decision** | **The single-page app is a Next.js static export served from S3 through CloudFront with Origin Access Control, defined entirely in CDK. Amplify Hosting is not used.** |
| **Related** | [architecture](../architecture.md) §8 · [repo-structure](../repo-structure.md) §5 · [ADR-007](adr-007-auth-cookie-bff.md) · [finding register](../decision-coverage-registry.md#finding-register-f-nn) F-18 |

> **Amended 2026-09-26 by [ADR-078](adr-078-serve-quality-review-to-programmatic-callers-through-the-cloudfront-edge.md) — the distribution becomes the only way in (deployed and dev accepted 2026-09-29, [validation results](../../validation/RESULTS.md#edge-entry-adr-078-slice-7--2026-09-29)).**
> The distribution carries a `CLOUDFRONT`-scope AWS WAF web ACL, and every request it forwards to
> the API carries an origin-verify header that the API checks, so every customer-facing API
> request passes WAF and a request sent straight to the API Gateway hostname is refused.

## Context

The original brief specified Amplify Hosting for the Next.js static export. That was an inherited assumption rather than an evaluated choice, and evaluating it produced `F-18`. This ADR records the outcome, promoting `D-05`.

The application is a single-page app with no server-side rendering and no Next.js route handlers. Everything the frontend needs at runtime it gets from the API. That removes the main reason teams reach for Amplify in the first place.

## Decision

Static export (`output: 'export'`) to S3, fronted by CloudFront with **Origin Access Control** — not the legacy Origin Access Identity. The whole thing is a CDK stack alongside every other stack.

Backend configuration — Cognito IDs, API base URL — is injected at build time from CDK outputs via SSM.

### Why not Amplify Hosting

| Concern | Amplify Hosting | CloudFront + S3 + OAC |
|---|---|---|
| CDK maturity | **L1 only** | stable L2 |
| Response-headers policy | **not controllable** | full control |
| Cache policy | **not controllable** | full control |
| WAF attachment | **not direct** | direct |
| CDK outputs into the build | manual seam | SSM at build time |
| WebSocket proxying | **cannot** | not applicable, but no dead end |
| PR previews | built in | CI deploys to a preview prefix |

> **The decisive one is header control, because of how [ADR-007](adr-007-auth-cookie-bff.md) works.** Tokens are held server-side specifically to keep XSS from becoming session theft. That removes token exfiltration from the blast radius but leaves request forgery from within a compromised page, and the defence for that is a strict `Content-Security-Policy` — which requires a response-headers policy on the distribution. Amplify Hosting manages a distribution we do not control, so adopting it would weaken the exact threat model the cookie approach was chosen to address.

Secondary but real: Amplify would be the only L1 construct in an otherwise L2 infrastructure, and it introduces a second deployment mechanism for a build that is `s3 sync` plus an invalidation. For a stack that ships into a customer's AWS account, fewer moving parts and one deployment path is worth more than managed previews.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| **Amplify Hosting** | See the table above. Its genuine advantages — PR previews, managed domains — do not outweigh losing header and cache control given ADR-007, and it adds tooling for a static export that does not need it |
| **S3 static website hosting, no CloudFront** | No HTTPS on a custom domain, no OAC, no WAF, no security headers. Also breaks `SameSite` cookies, which need the app and API under one registrable parent domain |
| **Server-side rendering on Lambda@Edge or CloudFront Functions** | The app has no SSR requirement. Adds cold starts and a second runtime to reason about for no gain |
| **Origin Access Identity** | Legacy. OAC is the current mechanism and supports SSE-KMS |

## Consequences

- Frontend deployment is `s3 sync` plus a CloudFront invalidation, owned by CI. Roughly twenty lines, and it stays in the same pipeline as everything else.
- **No PR preview environments out of the box.** If they become valuable, CI deploys to a preview prefix behind the same distribution rather than adopting a second hosting product.
- CSP, HSTS and the other security headers become ours to author. That is the point, but it is also work that Amplify would have partly defaulted for us.
- The app and API must sit under one registrable parent domain for cookies to be sent at all — the same prerequisite ADR-007 depends on, from a different direction.
- Cache behaviour must be explicit: static assets cached hard and immutably, `index.html` not cached, and any authenticated path through the distribution using `CachingDisabled` with `Cookie` forwarded.

## Review trigger

- the app acquires a genuine server-rendering requirement, which would reopen the hosting model entirely
- PR previews become a stated team requirement rather than a convenience
- the customer's account restricts CloudFront or WAF in a way that changes the tradeoff
