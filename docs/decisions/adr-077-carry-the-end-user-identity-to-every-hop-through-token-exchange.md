# ADR-077: Carry the end-user identity to every hop through token exchange

Date: 2026-09-25

Status: Accepted 2026-09-25; amended 2026-09-26 (§2, §3, §6: the Harness trusts only a
route-minted token; §4: a custom resource owns the Harness; §5: the propagated header channel;
§6: the audit receipt names the principal) — implemented and live-validated in dev; §6 amended
2026-09-26 by ADR-081 (one attribution section; decided, not yet implemented). Dated validation of each
slice is owned by [validation results](../../validation/RESULTS.md). The deployed boundary is the one
[Conversational Runtime §5](../capabilities/conversational-runtime/architecture.md#5-key-runtime-flow)
describes as current.

> **Amended 2026-09-26 — S2 landed; P1 and P5 settled.** The route now forwards the verified
> Cognito access token as the only Harness credential, the Harness declares the `customJWTAuthorizer`
> §2 names, the Conversation Function role holds no AgentCore invoke or Memory grant, and `actorId`
> is derived from `sub` alone. The Harness-to-Gateway and Gateway-to-tool hops still run under
> service roles until S4–S5 land. P1 showed that the underlying runtime refuses SigV4 once the
> Harness is JWT-authorized, so the revisit condition under "Unknowns" did not trigger and this
> decision stands. Dated conclusions are owned by
> [validation results](../../validation/RESULTS.md#identity-chain-slice-s2-acceptance--2026-09-26).

> **Amended 2026-09-26 — the Harness trusts only a route-minted token (§2, §3, §6).** S2 made the
> user's own Cognito access token a Harness credential, and a signed-in user can copy it from their
> browser. The Harness binds neither `actorId` nor `runtimeSessionId` to the bearer: a direct
> invocation with one persona's token and the other persona's actor was accepted and wrote Memory
> under the other persona [verified 2026-09-26, H-RUNTIME-INVOKE-001]. AWS also documents that every
> `InvokeHarness` caller may override the system prompt, tools, skills, and model parameters, and
> that caller authorization and input validation are the customer's responsibility
> ([harness security](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness-security.html))
> [verified 2026-09-26]. A user token that reaches the Harness therefore bypasses the route's
> Guardrail, audit, and request construction. The amended §2 makes the route the only possible
> caller: the Harness accepts only a short-lived token the token service mints for the route, and
> minting it requires both the route's IAM role and a live user token. §3 gains that second entry
> point; §6 records why the actor derivation stays key-free. An adversarial review the same day
> set four owner decisions, folded in below: a Harness token lives for one turn, not for the user
> token's remaining life (§2); an explicit resource-policy Deny confines the route exchange to the
> Conversation Function role (§3); the public issuer is a regional REST API with AWS WAF attached
> (§3); and the Gateway is not claimed to record the user in CloudTrail (§6).

> **Amended 2026-09-26 — a custom resource owns the Harness (§4).** CloudFormation cannot declare
> the `TOKEN_EXCHANGE` grant §4 requires on the Harness's Gateway tool, while the control-plane API
> accepts it ([AgentCore platform constraints](../capabilities/conversational-runtime/low-level-design.md#agentcore-platform-constraints)).
> `HarnessStack` therefore replaces `CfnHarness` with `Custom::AgentCoreHarness`, a Lambda-backed
> custom resource that owns the whole Harness through CreateHarness, UpdateHarness, and
> DeleteHarness. It is the Harness's only owner. The switch replaces the Harness under a new name,
> so the Harness ARN changes, and `ApiStack` must be redeployed right after `HarnessStack`
> because it reads that ARN at deploy time. Chat turns fail between the two deploys. Memory is
> passed by ARN and is not affected.
> **Removal trigger:** when the CloudFormation registry schema's Harness
> `OAuthCredentialProvider.GrantType` enum includes `TOKEN_EXCHANGE`, return to `CfnHarness`. That
> switch replaces the Harness again. **Rejected:** keeping `CfnHarness` and patching only `tools`
> from a custom resource beside it. That gives the Harness two owners, and every `CfnHarness`
> update would revert the grant.

> **Amended 2026-09-26 — slices 4 and 5 landed; P2, P3, and P4 settled (§5).** Every hop now
> carries the user. P2 showed one agent exchange per turn, made when the Harness loads its Gateway
> tools, with the route-minted Harness token as the subject and no audience, resource, or actor
> parameter. It also showed which three principals read the agent client secret, and the secret's
> resource policy now denies every other principal. P3 showed that a Lambda target receives the
> interceptor's `Authorization` header in its client context, so §5's header channel is the one
> built and the reserved-argument channel is not. §5 also now reads the tool verification key
> from KMS rather than a deploy-time key set: neither path touches the public issuer, and reading
> it from KMS adds no deploy-time artifact. P4 met its warm budgets, and a later idle run the same
> day met its cold budgets. Dated conclusions are owned by
> [validation results](../../validation/RESULTS.md#identity-chain-slice-4-and-5-acceptance--2026-09-26).

> **Amended 2026-09-26 — the audit receipt names the principal (§6).** §6 said tool audit records
> carry the principal, but no tool writes an audit record, and
> [ADR-059](adr-059-create-a-bounded-agent-quality-audit-loop.md) holds that CloudWatch
> telemetry excludes identity and cannot be the audit record. The principal is therefore stamped
> by the route on the conversation audit receipt, which is already retained, immutable, and
> links every tool call in the turn through its trajectory. The receipt contract becomes
> `ConversationAuditRecord.v4` with a `principal` that every `managed-harness.v3` receipt carries;
> earlier receipts are read with none. The same day the agent client secret's resource policy was
> narrowed from a CloudFormation execution role name pattern to the exact role (§3's residual
> trust), and `/token` re-reads the secret on a mismatch at most once a minute.
> **Rejected:** stamping the principal on each tool's terminal log record. It would make
> CloudWatch the attribution record, with a two-week retention, against ADR-059.
> **Rejected:** a durable audit row written by each tool. It adds IAM and a write to every tool
> call, and it would duplicate what the receipt's trajectory already records.

> **Amended 2026-09-26 by [ADR-081](adr-081-make-every-turn-traceable-and-attributable.md) — one
> section will return the principal (§6; decided, not yet implemented).** §6 keeps the principal out
> of every review projection, and that remains the deployed behaviour. One review section,
> `attribution`, will return it, only to callers in both `quality-reviewer` and `audit-attribution`,
> and every such read will be logged with its reviewer. The principal stays out of logs, metrics,
> traces, index attributes, and every other section.

Amends: [ADR-006](adr-006-agentcore-agentic-layer.md) (identity ownership: the verified identity is
no longer confined to the Conversation API), [ADR-007](adr-007-auth-cookie-bff.md) (current identity
boundary and the dev-only validation-identity exception),
[ADR-038](adr-038-the-conversation-route-streams-from-a-function-url.md) (the Harness hop's fourth
control, restored), and
[ADR-054](adr-054-scope-analytical-visibility-to-the-customer-deployment.md) (the trusted subject
transport it names as a prerequisite now exists; analytical scope stays deployment-wide).

## Context

The product's agent acts on behalf of a signed-in user. Today that user is verified once, at the
Conversation API, and every later hop runs as an AWS role. The Conversation Function's IAM role
invokes the managed Harness with SigV4. The Harness reaches the Gateway with its execution role
(`outboundAuth: awsIam`). The Gateway invokes Lambda tools with its service role. The user survives
only as an `actorId` the route asserts. AWS's on-behalf-of guidance names this pattern
"service-account impersonation" and calls it the textbook confused deputy
([OBO token exchange with AgentCore Gateway](https://aws.amazon.com/blogs/machine-learning/implement-on-behalf-of-token-exchange-for-multi-tenant-agents-with-amazon-bedrock-agentcore-gateway/))
[verified 2026-09-25]. Its concrete consequence is recorded as H-RUNTIME-INVOKE-001 (dated
evidence: [identity-chain slice S2 acceptance](../../validation/RESULTS.md#identity-chain-slice-s2-acceptance--2026-09-26)):
any principal in the account holding the Harness invoke actions can act as any user.

The loss happened at two hops and two times, and no decision recorded either:

- **Gateway and tools, 2026-08-22.** The Gateway became IAM-authenticated so that the CDK default
  would not create a retained Cognito pool inside a destroyable stack, and the Harness was wired to
  it with SigV4. The design then still promised propagation.
- **Route to Harness, 2026-09-04.** A working `customJWTAuthorizer` on the Harness and the route's
  bearer forwarding (live-verified 2026-08-23, ADR-038) were deleted with the stack that held them.
  The managed Harness returned on 2026-09-07 without the authorizer. The tests that asserted JWT
  inbound were deleted with the code, and ADR-038 and ADR-007 kept describing it.

The Well-Architected Agentic AI Lens asks that when an agent acts on behalf of a user, the user
context propagates as signed token claims through the call chain without the agent assuming the
user's credentials
([AGENTSEC03](https://docs.aws.amazon.com/wellarchitected/latest/agentic-ai-lens/agentsec03.html))
[verified 2026-09-25].

Three platform facts shape the options [all verified 2026-09-25]:

- A Harness accepts a JWT inbound authorizer, and per-user identity propagation to downstream tools
  requires it; SigV4 callers get none
  ([harness security](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness-security.html)).
- A Harness's Gateway tool credential is `awsIam`, `none`, or `oauth`. The only user-bound OAuth
  grant is `TOKEN_EXCHANGE` (RFC 8693), and there is no pass-through
  ([HarnessGatewayOutboundAuth](https://docs.aws.amazon.com/bedrock-agentcore-control/latest/APIReference/API_HarnessGatewayOutboundAuth.html),
  [OAuthCredentialProvider](https://docs.aws.amazon.com/bedrock-agentcore-control/latest/APIReference/API_OAuthCredentialProvider.html)).
  Amazon Cognito user pools issue no token-exchange grant
  ([Cognito grants](https://docs.aws.amazon.com/cognito/latest/developerguide/federation-endpoints-oauth-grants.html)).
- A Gateway Lambda target supports only the Gateway service role as its outbound credential, and
  its documented event carries no caller identity
  ([outbound auth](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-outbound-auth.html),
  [Lambda targets](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-add-target-lambda.html)).
  A REQUEST interceptor may read the inbound headers and rewrite the tool arguments
  ([interceptors](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-interceptors.html)).

This repository is an aws-samples solution. The decision therefore puts security first and, within
that, the least bespoke infrastructure: no VPC, no VPC Lattice, no private APIs, and no third-party
identity provider.

## Decision

### 1. One identity chain

The verified Cognito user reaches every hop as signed token claims. No hop admits an AWS role as a
substitute for the user, and no application role holds a credential that can act as an arbitrary
user. The token is the only path that works; an attempt at any hop without it is refused by that
hop.

### 2. Route to Harness: a route-minted token (amended 2026-09-26)

The Conversation API verifies the user's Cognito access token, exchanges it at the token service
(§3) for a Harness token, and invokes the Harness with that token as bearer. The Harness declares a
`customJWTAuthorizer` naming the token service's discovery document, the Harness audience, and the
route as the only allowed client. It therefore refuses SigV4, a Cognito token, and a token minted
for the Gateway. The Conversation Function holds no Harness or Runtime invoke permission.

The route is the only caller, and it alone builds the request. It derives `actorId` from the
verified `sub` and `runtimeSessionId` per conversation (§6), and sends no per-call override (`systemPrompt`, `tools`,
`allowedTools`, `skills`, `model`, or limits).

Each turn gets its own Harness token, and nothing caches it. Its lifetime is the longest a turn can
run, the Conversation Function timeout, plus a clock-skew allowance. It is independent of the
user token's remaining life: the user token only has to be unexpired when the route verifies it
and when the token service mints. A Harness token can therefore outlive the user token, or a
sign-out, by at most one turn. There is no minimum-lifetime refusal on the route. Neither token
enters domain types or Harness messages, and the minted token never leaves the route.

### 3. A token-exchange service of our own, in its own stack (amended 2026-09-26)

Because Cognito has no token-exchange grant, the repository runs a minimal RFC 8693 issuer. It lives
in a dedicated destroyable `TokenServiceStack` with its own small regional REST API, deployed after
`AuthStack` and before `AgentStack`. Its issuer is that API's own URL. Hosting it on the existing
API was rejected because `DEPLOY_ORDER` creates the Gateway and Harness before `ApiStack`, and the
edge attaches `/api/*` only on its second pass.

It has two entry points on two functions built from one package, and each issues exactly one
audience:

- **Route exchange: a function with no route, invoked directly.** Within one account an IAM
  identity policy alone can grant `lambda:InvokeFunction`, so the function carries an
  `AWS::Lambda::ResourcePolicy` with an explicit Deny for every principal except the Conversation
  Function role [verified 2026-09-26,
  [AWS::Lambda::ResourcePolicy](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-lambda-resourcepolicy.html)].
  The subject token must be a Cognito access token that passes the same verification the route
  uses. The result is a Harness-audience token whose client is the route. This hop has no shared
  secret and no public route.
- **Public issuer: a regional REST API.** It serves the discovery document and the key set, which
  the Harness and Gateway authorizers fetch, and, from slice S4, the agent exchange at `/token`.
  AWS WAF is attached to the API stage, because WAF does not attach to an HTTP API and a
  CloudFront distribution in front would leave the API's own endpoint as a bypass. The agent
  client authenticates with a client secret, which is what AgentCore's credential provider
  supports. The subject token must be a Harness-audience token of the service's own issuance. The
  result is a Gateway-audience token whose client is the agent. `/token` never accepts a Cognito
  token.

Separate functions keep the route exchange's resource policy from being overwritten by the API's
invoke permission, which AWS warns against mixing, and keep a flood on the public API from
starving chat turns of concurrency. The key policy, the secret's resource policy, and the route
function's resource policy are written as Denies that exempt only the principals each names, by
role ARN, so they deploy before those roles exist. The principals are owned by
[Conversational Runtime low-level design §6](../capabilities/conversational-runtime/low-level-design.md#6-interfaces-and-contracts)
(amended 2026-09-26).

Every minted token is bound to the subject's `sub`, is signed by one KMS key, and has a bounded
lifetime: a Harness token lives for one turn (§2), and a Gateway token never outlives its Harness
token. The service issues no refresh tokens. The public routes are unauthenticated at API Gateway,
like the existing sign-in routes, and are throttled per route. The claim contract, signing, and
client registration are owned by
[Conversational Runtime low-level design §6](../capabilities/conversational-runtime/low-level-design.md#6-interfaces-and-contracts).

### 4. Harness to Gateway: the exchanged token

The Harness's Gateway tool uses `oauth` with `TOKEN_EXCHANGE` against a credential provider
registered for the token service. The Gateway uses `CUSTOM_JWT` inbound authorization against the
token service's discovery document and the configured audience. The Harness execution role loses
its Gateway IAM invoke grant.

### 5. Gateway to tools: the signed bearer, verified by every tool (amended 2026-09-26)

One Gateway REQUEST interceptor passes the exchanged bearer token to the target in the
`Authorization` header and removes every `_adl_` argument the model supplied. It makes no trust
decision of its own. AWS documents that an `Authorization` header returned by a REQUEST interceptor
is forwarded to the target
([Gateway headers](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-headers.html))
[verified 2026-09-26]. Probe P3 showed that a Lambda target receives it inside its client context,
under a key AWS does not document [verified 2026-09-26, dev]. Every tool reads the bearer only from
there, verifies its signature, issuer, audience, client, algorithm, and expiry before anything else
reads the call, and refuses with a typed error when it is missing or invalid.

A bare claims field was rejected. Only a signature lets the tool fail closed when the interceptor
is detached or misconfigured, or when the tool is invoked directly with a fabricated identity.

Tools read the verification key with `kms:GetPublicKey` on the signing key, once per container, and
never fetch it from the public issuer. A tool cold start therefore makes no call to an endpoint that
can be flooded. An unknown `kid` is refused, not refreshed, so a key rotation is a redeploy.

**Rejected 2026-09-26: a reserved `_adl_caller_assertion` argument** set by the interceptor. It was
the channel to build if a Lambda target had not received the header. P3 showed that it does, so the
argument channel and its schema reservation were not built. Relying on an undocumented key has a
cost: if AWS changes it, every tool refuses every call, which fails closed.

### 6. Traceability and Memory

- **Audit (amended 2026-09-26):** the conversation audit receipt carries the raw Cognito `sub` the
  route verified as the turn's principal. It is an opaque identifier, not a secret, and resolves
  to a user in Cognito. The receipt is retained and immutable, and its trajectory records every
  tool call in the turn, so it is the solution's attribution record for the turn and for each
  tool call in it. Tools record only that their caller verified; the principal is never a
  telemetry field, an aggregate dimension, or part of a review projection. Gateway data events
  reach CloudTrail only through a data-event trail [snippet 2026-09-26], and the solution
  declares none.
- **Memory:** it is per user. `actorId` becomes a versioned function of the verified `sub` alone;
  the Harness session stays per conversation. Preferences and learned facts persist across a
  user's conversations and sign-ins. Records keyed by the former per-conversation actor are not
  migrated.
- **The actor derivation is key-free (amended 2026-09-26).** The Harness does not bind `actorId`
  to the bearer, so isolation rests on who can call it. Under §2 only the route can, and the route
  derives the actor from the verified `sub`. The actor therefore has to be derived by trusted code,
  not kept secret, and a plain versioned hash is sufficient. A keyed or randomly mapped actor
  would add a key or a table whose loss or rotation strands every user's memory.

### 7. Validation runs as real users on the real path

Live acceptance and probes sign in as synthetic dev Cognito personas and call the product through
`/api/chat`, so every hop runs as it does in production. The personas and the dev-only password
sign-in they use are the existing Quality Review mechanism, extended with two chat personas; synth
refuses the flag outside dev. Code-level tests verify tokens through an injected key generated
inside the test. Production composition reads the key only from KMS (§5, amended 2026-09-26).

Every test asserts a specific condition and fails when that condition changes:

- A negative case names the exact refusal and the layer that refused.
- Each negative varies one attribute of a known-good request that is asserted to succeed first in
  the same run.
- A refusal is shown to have had no side effect.
- Security-critical infrastructure properties are compared exactly.

### 8. Out of scope

- **Per-user analytical entitlements.** ADR-054's deployment-wide `AuthorizedAnalyticalScope`
  stays; this decision supplies the trusted subject transport ADR-054 requires first.
- **Cedar policies on user claims at the Gateway.** Possible once the Gateway sees user claims;
  deferred.
- **Memory access keyed on the token.** A Gateway Memory connector with a Cedar rule requiring
  `actorId` to equal the token's `sub` would enforce memory isolation without trusting the route
  ([Memory fine-grained access control](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/memory-gateway-fgac.html))
  [verified 2026-09-26]. The Harness's built-in memory calls Memory with its execution role, so
  adopting it means serving memory through Gateway tools instead; deferred.
- **Private networking** for the token service or tools.

## Consequences

**Security posture.** The confused-deputy path closes. The Conversation Function can no longer
drive the agent as anyone: it can obtain a Harness token only by presenting a live token of the
user who is calling it. A signed-in user cannot reach the Harness outside the route, so the route's
Guardrail, audit, and request construction cannot be bypassed. The Harness reaches tools only with
a token minted from that Harness token. Every tool verifies it. Its status is owned by
H-RUNTIME-INVOKE-001 (amended 2026-09-26: resolved once the chain was live-validated in dev).

**Residual trust.** An administrator who can change the route function's resource policy, read the
agent client secret, or change the KMS key policy can still mint tokens; each of those is a logged
policy change, not an invocation. So can one who publishes a version of the route-exchange
function and invokes it, because a Lambda resource policy applies to the unqualified function it
is attached to [snippet 2026-09-26]; that too is a logged write, and the stack publishes no
versions. Each of the three policies denies every principal but the ones it names. The secret's
policy names the exact CDK CloudFormation execution role this stack deploys with, because the
credential provider's deployment handler reads the secret, so any principal that can deploy
through that role can also read the secret (amended 2026-09-26). A
leaked Harness token grants more than the product does: until it expires, its holder can name any
actor and send per-call overrides. That is why it lives for one turn and is never cached, logged,
or returned.

**A new public surface.** The token service's public routes are reachable from the internet, like
every OAuth token endpoint, behind AWS WAF and per-route throttles.

- **What minting requires:** a Harness token needs the Conversation Function role and a valid,
  unexpired Cognito access token. A Gateway token needs the agent client secret and a valid Harness
  token. `/token` authenticates the client before any KMS call, so an unauthenticated flood never
  reaches signing.
- **What a minted token grants:** it is bound to that same user and to one audience. A Gateway
  token reaches only the tools, as that user. A Harness token is more powerful (see residual
  trust) and lives for one turn.
- **Availability:** a flood that WAF does not stop can throttle `/token` and fail tool calls, and
  the discovery document and key set are throttled separately so authorizer key fetches are not
  starved by it.
- **The real risk:** defects in bespoke validation code. It is addressed by a small surface, reuse
  of the existing verifier, hostile-input tests with real cryptography, mutation review, and route
  throttling.
- **Key management.** The signing key and client secret join the key-management scope G-07 tracks.

**Deployment.**

- **Topology.** `TokenServiceStack` enters `DEPLOY_ORDER`, the README stack table, and teardown.
- **Existing environments.** The Harness inbound authorizer and the credential provider changed in
  place. The Gateway (its authorizer type cannot change in place) and the Harness (now owned by a
  custom resource) are replaced, each under a new name and ARN (amended 2026-09-26).
- **Brief outage windows.** The Harness inbound authorizer (`HarnessStack`) and the route's
  exchange (`ApiStack`) switch in separate stacks, as do the Gateway (`AgentStack`) and the Harness
  outbound credential (`HarnessStack`); turns or tool calls fail between each pair of deploys. Each
  pair is deployed back to back, not in `DEPLOY_ORDER` sequence, which places other stacks between
  them. No dual path is kept.

**Operations.**

- Every turn adds one route exchange (a Lambda invocation and a KMS signature), and every tool call
  may add an agent exchange and an interceptor invocation. The exchange count is recorded by P2;
  the added latency is measured by P4 against warm and cold budgets declared before it runs, in
  `BUDGETS` in `validation/probe_identity_latency.py`.
- Removing the minimum-lifetime refusal removes chat's extra refresh round trip near the end of
  each user token's life.
- The Conversation Function's unused Memory grants are removed, because under per-user memory they
  would read every user's memory.

**Tests and probes.**

- Direct SigV4 Harness probes move to the persona bearer path.
- The in-process tool integration test uses the injected key set.
- A permanent identity-chain probe proves the token is the only working path and that one user
  cannot recall another's memory. After the amendment it also proves that the Harness refuses a
  Cognito token and a token signed by a foreign key, and that the route exchange refuses an
  administrator-class principal by its resource-policy Deny rather than by a missing grant. From
  slice 5 it also proves the Gateway, tool, secret, and client-authentication refusals. Each case
  names the layer that refused it. Validation of the subject token inside the route exchange and at
  `/token` is covered by tests with real cryptography, because only the Conversation Function can
  invoke the one and only the agent client's secret can reach the other (amended 2026-09-26).
- A unit test pins the exact `InvokeHarness` request keys the route sends, so an override field
  cannot be added without failing it.

**Unknowns this decision depends on.** Probes P1–P5 settle them. Each runs in place against the dev
stacks as the first live step of the slice that depends on it. There is no separate probe
environment, because dev has no users to protect, and a failed probe stops that slice before its
remaining changes land:

- **P1:** SigV4 refusal on both the Harness ARN and its underlying runtime ARN. Settled
  2026-09-26: both refuse SigV4.
- **P2:** the exchange request shape and frequency, including that the Harness presents its
  inbound (route-minted) token as the subject token. Settled 2026-09-26: one exchange per turn, with
  the route-minted token as the subject.
- **P3:** whether a Lambda target receives an interceptor-set `Authorization` header (§5); only if
  it does not, whether the reserved argument survives Gateway schema validation. Settled
  2026-09-26: it does, in the invocation's client context.
- **P4:** added latency, against declared budgets. Settled 2026-09-26: warm and cold budgets met.
- **P5:** cross-conversation Memory recall. Settled 2026-09-26: one user recalls across
  conversations and another does not.

If P1 shows that the underlying runtime accepts SigV4, this decision is revisited before
implementation.

## Alternatives considered

- **OpenAPI targets behind a public HTTP API with a JWT authorizer**, the AWS on-behalf-of
  reference. Rejected: it adds a public tool surface without WAF, a 30-second integration cap, and
  more change.
- **OpenAPI targets on a private REST API through VPC Lattice.** Rejected as bespoke
  infrastructure for this solution.
- **A custom AgentCore Runtime that forwards the bearer itself.** Rejected: it reverses ADR-006's
  managed Harness and adds code to own.
- **A third-party identity provider with native token exchange.** Rejected: an external dependency
  unfit for a Cognito-only sample.
- **An AWS-signed actor token (`AWS_IAM_ID_TOKEN_JWT`) for the agent exchange.** Deferred: it is an
  actor-token mode of AgentCore's token exchange rather than a client-authentication method
  ([on-behalf-of token exchange](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/on-behalf-of-token-exchange.html))
  [verified 2026-09-26], and it requires an account-level outbound web identity federation
  setting.
- **An HTTP API for the public issuer.** Rejected 2026-09-26: AWS WAF does not attach to it
  [snippet 2026-09-26].
- **A CloudFront distribution with WAF in front of the issuer.** Rejected: the API's own endpoint
  stays reachable without a custom domain, and origin access control on a Function URL requires a
  body hash on every `POST`, which an OAuth client does not send
  ([CloudFront OAC for Lambda](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/private-content-restricting-access-to-lambda.html))
  [verified 2026-09-26].
- **One function for both entry points.** Rejected: its resource-policy Deny and the API's invoke
  permission would share one policy, and a public flood would compete with chat turns for
  concurrency.
- **A minted Harness token that lives as long as the user token.** Superseded 2026-09-26: it
  widened a leaked token's window from one turn to up to the user token's full life, and its
  minimum-lifetime refusal cost chat an extra refresh round trip.
- **Interceptor-injected claims without a signature.** Rejected: it cannot fail closed.
- **The user's own Cognito token as the Harness credential** (the original §2). Superseded
  2026-09-26: any signed-in user could invoke the Harness directly, choose the actor, and send
  overrides.
- **The route signs the Harness token itself.** Rejected: it would save one invocation per turn,
  but the route role would hold `kms:Sign` and could mint a token for any `sub` without a live
  user token, which reopens the confused deputy.
- **Restricting the Harness to a Gateway caller (`allowedWorkloadConfiguration`).** Not available:
  it is supported only for AgentCore Runtime targets [snippet 2026-09-26], and the Harness JWT
  authorizer has no such field [verified 2026-09-26, CloudFormation schema in aws-cdk-lib 2.265].
- **Keeping tokens server-side (a token-handler BFF) so the browser never holds one.** Rejected: it
  rewrites ADR-007's authorizer and refresh path, and a Harness that trusts Cognito would still
  accept any Cognito token that leaked from elsewhere.
- **A keyed (KMS HMAC) or randomly mapped `actorId`.** Rejected under §6: once only the route can
  call the Harness, it adds a key or table without closing a remaining path.
- **Returning the Harness to SigV4 until S3.** Rejected: it restores the confused deputy that S2
  closed.

## Review trigger

- Revisit when AgentCore documents a Harness outbound pass-through, a caller-identity field for
  Lambda targets, or Cognito support for token exchange.
- Revisit if probe P1, P3, or P5 disproves an assumption above, or if tools start refusing every
  call because the Gateway stops delivering the propagated header under the key P3 observed.
