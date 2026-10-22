# ADR-078: Serve quality review to programmatic callers through the CloudFront edge

Date: 2026-09-26

Status: Accepted 2026-09-26 — partly implemented. Deployed and dev accepted 2026-09-28 and
2026-09-29: §2's two routes and groups, with the old groups and levels removed, §3's credential
binding, and §5's route-key authorization and edge refusal of non-canonical paths. Pending: §1's web
ACL and origin-verify header, with §5's normalizing WAF rules and direct-endpoint refusal (edge
entry), and §4's command-line client. Current state is in the
[Protected Quality Review API](../capabilities/protected-quality-review-api/README.md). It supersedes
[ADR-060](adr-060-protect-quality-review-retrieval-with-an-explicit-operator-authorization-boundary.md)'s
route set, per-level groups, one role per route, no-list rule, browser-only callers, and rejection
of a bearer-token API; callers stay human. It amends
[ADR-007](adr-007-auth-cookie-bff.md) (a second credential on review routes) and
[ADR-008](adr-008-frontend-hosting.md) (a web ACL and an origin-verify header on the
distribution). Attribution and reviewer access logging are owned by
[ADR-081](adr-081-make-every-turn-traceable-and-attributable.md). The mandated contracts are owned by
[authentication](../concepts/authentication.md) (credentials and edge entry) and the
[Protected Quality Review API](../capabilities/protected-quality-review-api/low-level-design.md)
(routes, sections, storage access). Dated validation is owned by
[validation results](../../validation/RESULTS.md).
> **Refined 2026-09-28 — region handling at the edge.** In us-east-1 the web ACL ARN is an ordinary
> in-region read; only another application region needs the cross-region read, which the
> stack-composition rules already allow, so no steering amendment is needed. Live acceptance runs in
> us-east-1 only.
> **Refined 2026-09-28 — refusal at ingress.** §5's permanent probe found spellings API Gateway folds
> into a route; §5 now states that CloudFront refuses them, and why.

> **Refined 2026-10-05 — path-scoped body-size exclusion.** The core rule set's
> `SizeRestrictions_BODY` refused a legitimate deep-tier prompt create (bodies of 8,130 to 8,529
> bytes) on 2026-10-04. By owner decision of 2026-10-04, `POST /api/prompt-registry/create-version`
> with an empty query string is the one exempt request shape, with a 32,768-byte ceiling. This
> applies to every environment deployed from the commit.
> - Inside `AWSManagedRulesCommonRuleSet`, only `SizeRestrictions_BODY` is overridden to count;
>   every other rule in the group keeps blocking on every route, create-version included. A
>   scope-down of the group was rejected because it would strip XSS, LFI and SSRF inspection from
>   the route.
> - Two custom size rules after the group take over the refusal, and neither reads a label:
>   `PromptCreateBodyCeiling` refuses a body over 32,768 bytes on the exact route, and
>   `BodySizeOutsidePromptCreate` refuses a body over 8,192 bytes (`GT 8192`, the managed rule's
>   own bound) on every other request. The route match is exact method, exact path and an empty
>   query string, all with no text transformation, so any other spelling falls under the 8 KB rule.
> - The ACL inspects the first 32 KB of a CloudFront body, and both rules set
>   `oversizeHandling: MATCH` on their body field, so a body over the inspection limit is refused
>   rather than measured as the limit. A synth guard refuses a body-size statement without it.
> - The prompt-admin handler refuses a decoded body over the same ceiling with
>   `413 request_too_large` before parsing, as defence in depth.
> - **Amendment of §1's mode rule.** Both new rules block from the first deploy. A rule that only
>   re-imposes a refusal the ACL already applies in block mode does not start in count, because a
>   count period would open the 8 KB refusal on every route. Every other new rule still starts in
>   count.
> - **Rollback trigger.** After the edge deploy, a non-exempt body over 8 KB that reaches API Gateway,
>   or a body over 32 KB on the exempt route that does, redeploys the prior edge commit at once.
>   Dated evidence is in [validation results](../../validation/RESULTS.md).
> **Amended 2026-10-22 by [ADR-150](adr-150-expose-the-governed-tools-to-amazon-quick-through-a-user-authorized-mcp-gateway.md)
> — a third credential binding (§3; demo slice, dev only).** An access token issued to the Amazon
> Quick MCP client is valid only at the external MCP gateway; every app route refuses it. The two
> bindings above are unchanged. The current table is
> [authentication's credential binding](../concepts/authentication.md#credential-binding-adr-078).

## Context

A customer investigating an incident, or answering an auditor, needs the record of one turn
without going through a browser: the receipt, its structural findings, and its evaluation
verdicts. [ADR-060](adr-060-protect-quality-review-retrieval-with-an-explicit-operator-authorization-boundary.md)
built that record's API for human Cognito users with a browser session only: three POST routes,
one Cognito group and one Lambda role per route, and the cookie authorizer as the only credential
path. It introduced no bearer-token caller.

Three facts, checked 2026-09-26, make that surface both awkward and weaker than it looks:

- **The per-route separation is nominal.** The findings and evaluation roles hold the same
  `dynamodb:GetItem` and `dynamodb:Query` over the whole audit table, and the audit role holds
  `GetItem` over the whole table [verified 2026-09-26, deployed IAM]. Three routes, three groups,
  and three functions buy no data separation. They do make an investigator call three endpoints for
  one turn.
- **Nothing at the edge filters API traffic.** The CloudFront distribution has no web ACL, and the
  HTTP API's default `execute-api` endpoint is enabled [verified 2026-09-26, deployed state]. An
  HTTP API cannot attach AWS WAF, so the documented pattern is WAF on CloudFront plus a secret
  origin header the API checks
  ([AWS blog](https://aws.amazon.com/blogs/compute/restricting-access-on-http-api-gateway-endpoint-with-lambda-authorizer/))
  [verified].
- **The HTTP API cannot be made unreachable from the internet.** Disabling the default endpoint only
  moves callers to a custom domain, which is public too
  ([disable the default endpoint](https://docs.aws.amazon.com/apigateway/latest/developerguide/http-api-disable-default-endpoint.html))
  [snippet]. CloudFront VPC origins accept only Application Load Balancers, Network Load Balancers,
  and EC2 instances
  ([VPC origins](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/private-content-vpc-origins.html))
  [snippet]. "Restricted" can therefore mean refused, not unreachable.

## Decision

**Every customer-facing API request enters through the CloudFront distribution and its AWS WAF
web ACL, and the API refuses any request that did not. Behind that edge, quality review is two
routes. They accept the browser session cookie, or a bearer token from a public PKCE client that
no other route accepts, and return one turn's sections filtered by the caller's entitlement.**

### 1. The edge is the only way in

- The distribution carries a `CLOUDFRONT`-scope web ACL whose rules start in count mode and move to
  block only after a full live probe run through the distribution shows no match on legitimate
  traffic. A managed rule that matches legitimate traffic is excluded for the affected path only,
  with the evidence recorded. *(Amended 2026-10-05:)* a rule that only re-imposes a refusal the
  ACL already applies in block mode starts in block (see the path-scoped body-size exclusion
  marker above).
- A `CLOUDFRONT`-scope web ACL can be created only in us-east-1
  ([CreateWebACL](https://docs.aws.amazon.com/waf/latest/APIReference/API_CreateWebACL.html))
  [verified]. A new edge-security stack, always deployed to us-east-1 and ordered before the
  frontend stack, owns the web ACL and publishes its ARN as an SSM parameter in us-east-1. When the
  application region is us-east-1, the frontend stack reads it like any other parameter. In another
  region it reads that one parameter through a CDK SDK-call custom resource pinned to us-east-1,
  because a deploy-time parameter reference resolves only in the stack's own region [unverified];
  the stack-composition rules already allow a cross-region parameter read. A synth test covers a
  non-us-east-1 region; live acceptance is in us-east-1 only (owner decision, 2026-09-28).
- Every rule on both web ACLs must reach block, or a recorded path-scoped exclusion, before the
  edge slice is accepted. The per-source-IP rate limit is a typed per-environment value with no
  default.
- The user pool gets its own regional web ACL, associated directly with the user pool, because the
  Cognito managed login and API endpoints the PKCE login and the dev probes call sit outside
  CloudFront; there is no API layer in front of Cognito. Cognito supports one associated web ACL per
  user pool
  ([WAF with a user pool](https://docs.aws.amazon.com/cognito/latest/developerguide/user-pool-waf.html))
  [verified]. It follows the same count-then-block rule.
- CloudFront adds a secret origin-verify header to every request it forwards to the API. The
  authorizer's identity source is that header, so API Gateway answers a request without it with a
  401 and never invokes the authorizer
  ([identity sources](https://docs.aws.amazon.com/apigateway/latest/developerguide/http-api-lambda-authorizer.html))
  [verified]. The authorizer then compares the value, in constant time, before any other check.
- Because every caller now presents the same identity source, API Gateway's authorizer cache would
  share one caller's decision with every other caller. The cache setting is removed, and synth
  refuses any authorizer result TTL above zero.
- The auth handler's four unauthenticated routes (login, callback, refresh, logout) have no
  authorizer, so the handler performs the same comparison first.
- The value is a Secrets Manager secret owned by the stack that owns the distribution. It reaches
  the distribution config, so anyone who can read that config can read it; that is accepted,
  because the header is one layer, not the credential.
- Rotation is an operator action coupled to a deploy, never automatic. The operator writes a new
  secret version whose current value is new and whose previous value is the old current, then
  redeploys the frontend stack pinned to that version, so CloudFormation resolves the new value.
  Both checks read the secret at cold start, hold it for at most a typed maximum cache age, and
  re-read it once, rate-limited to an interval shorter than that age, when a value matches neither
  current nor previous. Warm functions therefore pick up a new value without refusing traffic.
  After the redeploy is verified, a second version drops the previous value, and it stops being
  accepted once the maximum cache age has passed.
- CloudFormation resolves a version-pinned Secrets Manager dynamic reference inside a CloudFront
  origin custom header [verified 2026-09-29, dev rotation acceptance,
  [validation results](../../validation/RESULTS.md#edge-entry-adr-078-slice-7--2026-09-29)].
- CloudFront, WAF, and access logging never record the origin-verify, `authorization`, or `cookie`
  headers.
- A request that guesses the header name still reaches the authorizer. The stage throttle bounds
  the total cost of such traffic but does not isolate it from legitimate traffic, and an alarm
  counts origin-verify refusals.
- Scope: customer-facing APIs only. The chat Function URL is already reachable only through
  CloudFront, by origin access control with `AWS_IAM` auth [verified 2026-09-26, deployed state].
  The token service's regional REST API is out of scope: its callers are AWS services at its issuer
  URL, and it has its own regional web ACL and per-route throttles
  ([ADR-077](adr-077-carry-the-end-user-identity-to-every-hop-through-token-exchange.md) §3). The
  Cognito endpoints the PKCE login uses are AWS-managed and sit outside this edge.

### 2. Two review routes and two groups

- `POST /api/quality-review/turn` returns one turn, selected by exactly one `audit_id` or
  normalized `correlation_id`, as sections the caller is entitled to.
- `POST /api/quality-review/turns` lists the turns created in a bounded time window, with
  closed-value filters, a page limit, and a cursor. It never returns the principal.
- Two Cognito groups replace the three per-level groups: `quality-reviewer` reads both routes, and
  `audit-attribution` adds the `attribution` section of
  [ADR-081](adr-081-make-every-turn-traceable-and-attributable.md). `audit-attribution` alone grants
  nothing; a caller needs `quality-reviewer` too.
- One function and one role serve both routes. The role reads every review table and nothing else.
- `turns` reads a sparse index on the receipt table that only receipts written after this change
  populate. It projects the non-identity row fields, so a row costs no second read. Turns from
  before the change are not listed; `turn` still reads them by selector.
- The body stays a POST selector, so selectors never reach URL or query telemetry.
- Every record the `turn` route returns carries its expiry, so live acceptance of retention runs
  through the API rather than a direct table read.
- **Group migration, in order inside the slice:** create the two new groups; move every member of
  the three old groups with a provisioning script; verify the old groups are empty; then remove
  them. A token that still carries an old review level fails closed, and its holder signs in
  again. The dev persona set becomes a reviewer, an attributor in both groups, an attributor in
  `audit-attribution` only (to prove it grants nothing), and a denied persona.
- **Retired in the same slice:** the three per-level routes and functions with their roles, log
  groups, and alarms; the three groups; the three review levels in the pre-token trigger, the
  token claim vocabulary, and the authorizer's path map; the authorizer cache setting; and the
  probes that exercise the old routes or read review tables directly.

### 3. One credential, bound to one client

- A request carries exactly one credential. The `adl_session` cookie is valid only with the BFF
  client's `client_id`, on every authorized route. An `Authorization: Bearer` access token is valid
  only with the public client's `client_id`, and only on the two review routes. Any other
  combination, and a request carrying both an `adl_session` cookie and a bearer, is denied.
- Both credentials pass the same token verification: signature by `kid`, `iss`, `exp`, and
  `token_use = access`.
- CSRF double-submit and origin checks apply to the cookie credential. A browser never attaches a
  bearer token by itself, so the bearer path has no cross-site forgery exposure.
- The public client uses authorization code with PKCE and has no secret
  ([authorization endpoint](https://docs.aws.amazon.com/cognito/latest/developerguide/authorization-endpoint.html))
  [verified]. Its only OAuth flow is authorization code, its only scope is `openid`, and a token
  from that flow never carries `aws.cognito.signin.user.admin`, so a CLI-held token cannot call
  Cognito's own user APIs (the dev-only password flow differs; see authentication).
  Outside dev its only explicit auth flow is refresh.
- Its only callback is `http://localhost:<fixed port>/callback`; Cognito allows HTTP only for
  localhost
  ([app client settings](https://docs.aws.amazon.com/cognito/latest/developerguide/user-pool-settings-client-apps.html))
  [verified].
- Its access tokens have the BFF client's lifetime and its refresh validity is the shortest Cognito
  allows. The authorizer verifies tokens locally, so revoking a token or removing a group ends
  access only when the current access token expires. A bearer token never reaches browser
  JavaScript.
- In dev only, the validation password flag keeps `USER_PASSWORD_AUTH` on the confidential BFF
  client, which needs the client secret, and adds IAM-authenticated `ADMIN_USER_PASSWORD_AUTH` on
  the public client, so live probes can call the bearer path without a browser. A password flow that
  needs neither a secret nor IAM credentials is never enabled. Whether the admin flow works on a
  client without a secret is [unverified] and is the first check of the implementing slice; if it
  does not, a dev-only confidential probe client admitted on review routes is the replacement.

### 4. A thin command-line client

The repository ships a command-line client for investigators. It runs the PKCE loopback login,
holds tokens in memory only, calls only the CloudFront URL, and offers `turn`, `turns`, and
`from-log` (resolve the identifiers in one structured log line, then fetch the turn). It is a
client of the API above, not a second path to the data.

### 5. Authorization hardening

- The authorizer chooses the required entitlement from the matched route key, not from the raw path,
  and denies by default any path under a protected prefix that maps to no entitlement.
- The review levels travel in the authorizer context, and the handler rechecks every section it
  returns against them. The `attribution` omission has unit and live tests of its own, because the
  handler's filter is the only barrier inside a role that can read every principal.
- A permanent probe sends non-canonical path variants and requires every one to be refused.
- Refusal happens at ingress (2026-09-28). API Gateway folds `//`, `.` and `..` segments, `%2F`,
  and a percent-encoded character into a route, observed in dev
  ([validation results](../../validation/RESULTS.md#review-api-slice-6-commit-a--2026-09-28)), while
  anything that matches raw path text, such as a path-scoped WAF rule or exclusion, would not. So a
  CloudFront viewer-request function on every API behaviour refuses, with 400, a path holding an
  empty segment, a dot segment, a backslash, or any percent escape; no API route needs one. Envoy's
  edge guidance names this class path confusion, has every layer see one normalized form, and
  rejects escaped slashes outright when clients are expected to send normalized paths
  ([Envoy edge best practices](https://www.envoyproxy.io/docs/envoy/latest/configuration/best_practices/edge))
  [verified 2026-09-28]; our only clients send canonical paths, so refusal gives every layer one
  form without normalizing anything. It meets
  [CWE-180](https://cwe.mitre.org/data/definitions/180.html)'s concern, a check made on a form that
  a later layer canonicalizes differently [verified 2026-09-28]. Every WAF rule that matches a path also applies `NORMALIZE_PATH` and
  `URL_DECODE` ([text transformations](https://docs.aws.amazon.com/waf/latest/developerguide/waf-rule-statement-transformation.html))
  [verified 2026-09-28], and the origin-verify header refuses the same spellings sent straight to
  `execute-api`. The authorizer still chooses entitlement from the route key alone.

## Consequences

- An investigator fetches a whole turn in one call from a terminal or script, using identifiers the
  structured logs already carry, and every such call passes WAF.
- One review function holds read access to every review table. That is the access the three roles
  already held together, stated once, and it makes the handler's section filter security-critical.
- The authorizer gains a second credential branch. Its tests must cover each client-credential
  crossing as separately as the cookie branch is covered today.
- The origin-verify header protects against casual bypass, not against someone who has read the
  distribution config.
- `turns` reverses ADR-060's "cannot list or time-search". Enumeration stays bounded by the window,
  the page limit, and the entitlement.
- A machine caller with no human, such as a scheduled audit export, still has no path. That needs a
  client-credentials or IAM principal, which is a different identity class and its own decision.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| Keep three routes and add bearer support to each | Keeps the nominal separation and makes an investigator call three endpoints per turn. |
| The managed HTTP API JWT authorizer for bearer callers | One authorizer per route, so bearer callers would need their own routes: two paths to the same data. |
| A custom domain with the default endpoint disabled, as the lock | Recommended to deployers as production hardening alongside origin-verify (2026-09-28), not as its replacement. The custom domain is itself public, so the API still would not be CloudFront-only; it also makes every deployer own a domain and certificate. |
| Migrate to a REST API to attach WAF directly | A regional web ACL on the API duplicates the edge ACL and changes the API type without changing who can reach it. |
| No identity source, with the origin check inside the authorizer | Every direct request would invoke the authorizer, including requests with no header at all. |
| Automatic secret rotation | The distribution learns a value only on deploy, so a second rotation between deploys would refuse every request. |
| Refusing synth outside us-east-1 | Would stop a deployer from using another region for the sake of one parameter read (owner decision, 2026-09-28). |
| CDK cross-region construct references for the web ACL ARN | A construct reference across a stack boundary, which the stack composition rules forbid. The chosen read keeps the value an SSM parameter, as every other exchanged value is. |
| `USER_PASSWORD_AUTH` on the public client for probes | The public client has no secret, so anyone holding its identifier could password-spray the pool outside WAF. |
| Client-credentials (M2M) or SigV4 callers now | A non-human principal has no Cognito `sub` and changes what access logs record. Deferred until an integration needs it. |
| Accept every spelling API Gateway folds into a route | Authorization keyed on the route key already grants a variant nothing more, but any layer that matches raw path text would disagree with the router about what a request is. |
| Refuse non-canonical paths in the authorizer from the raw path | Depends on API Gateway handing the authorizer the original spelling, which no contract states, and leaves the edge and every WAF rule seeing the spelling unrefused. |
| No command-line client | Each customer would script the PKCE flow, and registering third-party tool callbacks widens the user pool's trust. |

## Review trigger

Revisit when a machine caller is required, when AWS offers private CloudFront origins for API
Gateway, when a managed rule's false positives cannot be excluded per path, or when a review record
family is added that one role should not read.
