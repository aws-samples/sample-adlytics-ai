# ADR-038: The conversation route streams from a Lambda Function URL behind CloudFront

Date: 2026-08-22
Status: Accepted — amended 2026-09-25 and 2026-09-26 ([ADR-077](adr-077-carry-the-end-user-identity-to-every-hop-through-token-exchange.md))
Implements the transport half of [Q-03](../decision-coverage-registry.md#question-register-q-nn), which settled the *shape* — SSE, not
WebSocket — but assumed API Gateway could carry it. Supersedes nothing. Does not amend
[ADR-007](adr-007-auth-cookie-bff.md): every property of the cookie design is preserved, and this
records what it cost to preserve them.

> **Amended 2026-09-25 by [ADR-077](adr-077-carry-the-end-user-identity-to-every-hop-through-token-exchange.md) — the Harness hop's fourth control.**
> The fourth control below ("Cognito JWT authorizer on the harness, same token as bearer") and the
> Consequences passage beginning "The caller's own access token is the credential on the onward hop",
> with the consequences that follow it, describe the design live-verified on 2026-08-23. It was
> removed on 2026-09-04 without a decision: the current route invokes the Harness with the
> Conversation Function's IAM role, and those role grants are the live credential. ADR-077 §2
> restores the bearer hop, pending probes and implementation. The current route is owned by
> [Conversational Runtime §5](../capabilities/conversational-runtime/architecture.md#5-key-runtime-flow).

> **Amended 2026-09-26 by [ADR-077](adr-077-carry-the-end-user-identity-to-every-hop-through-token-exchange.md) — the fourth control is restored.**
> ADR-077 slice S2 is deployed to dev: the route again forwards the verified access token as the
> only Harness credential, and the Harness again declares the `customJWTAuthorizer`, so the fourth
> control and the passage "The caller's own access token is the credential on the onward hop" with
> its three consequences apply again. The current adapter and its names are owned by the
> [caller identity contract](../capabilities/conversational-runtime/low-level-design.md#6-interfaces-and-contracts). The dead grants are
> out: the Conversation Function role holds no `InvokeHarness`, `InvokeAgentRuntime`, or Memory
> grant, and its explicit `Deny` now also covers `InvokeHarness` and `InvokeAgentRuntime`. The
> 2026-09-25 marker above is historical. Live validation is owned by
> [validation results](../../validation/RESULTS.md#identity-chain-slice-s2-acceptance--2026-09-26).

> **Amended 2026-09-26 by [ADR-077](adr-077-carry-the-end-user-identity-to-every-hop-through-token-exchange.md) — the bearer is the route-minted token (S3).**
> ADR-077 slice S3 is deployed to dev: the route no longer forwards the caller's access token to
> the Harness. It exchanges that token, once per turn, for a single-turn Harness token and presents
> that as the bearer, and the Harness authorizer trusts the token service instead of this pool, so
> it refuses the caller's Cognito token. The fourth control and the onward-hop passage below are
> corrected in place (the onward-hop passage completed 2026-09-26 after a documentation review).
> Live validation is owned by
> [validation results](../../validation/RESULTS.md#identity-chain-slice-s3-acceptance--2026-09-26).

## Context

Q-03 decided in 2026-08 that mid-generation interruption is out of V1 scope, and concluded that
"AgentCore SSE is sufficient and WebSocket is fully out". [authentication](../concepts/authentication.md) §6 recorded
its Option 1 as "Browser → API GW (HTTP, cookie auth works) → AgentCore … → relay stream back over
the same HTTP response".

**That option does not exist.** Three facts, each verified 2026-08-22 and each individually
sufficient:

- **API Gateway response streaming is REST-API-only.** The November 2025 launch is titled "response
  streaming for REST APIs" and the documentation states it works for all REST API endpoint types.
  This repository serves `/api/*` from an `apigwv2.HttpApi`.
- **A Python Lambda cannot stream natively.** Lambda supports response streaming on Node.js managed
  runtimes; other languages need a custom runtime or the Lambda Web Adapter.
- **HTTP API's 29-second integration timeout is not adjustable.** Only Regional and private REST APIs
  can raise it, to 90 seconds through an account-level service quota and beyond that by support case.

The timeout is the harder wall, and it is not about streaming at all. Measured against the deployed
harness: quick chat completes in 9.8s, and a deep dive in **22.4–25.7s** over two runs. Three seconds
of headroom, on five ASINs of fixture data, is not a margin — and real data raises duration through
every term, since more rows mean more input tokens and longer generations. A turn exceeding the
timeout does not degrade: API Gateway returns 504, the user waited 29 seconds for an error, and the
model tokens are already paid for.

**Buffering is not a fallback.** Time to first text is 4.4s for quick chat and 14.9–18.4s for a deep
dive; total is 9.8s and 22–26s. Under SSE a reader starts reading at the first fragment, so the turn
is acceptable *because* it begins speaking early. Removing streaming does not make a 25-second turn
slightly worse — it replaces "reading from 15s" with "silence until 25s", which is the UX the
transport was chosen to avoid.

## Decision

**The conversation route is a Lambda Function URL with `AuthType: AWS_IAM` and
`InvokeMode: RESPONSE_STREAM`, reached only through the existing CloudFront distribution using Origin
Access Control with SigV4 signing. Every other route continues through API Gateway unchanged.**

One distribution, two backends, split by path: `/*` to S3, `/api/*` to the HTTP API, `/api/chat/*` to
the Function URL. The Function URL is the only route that bypasses API Gateway, and it bypasses it
because API Gateway cannot carry it.

**The Lambda Web Adapter supplies the streaming a Python runtime cannot.** It is an AWS-maintained
Rust extension distributed as a layer, `LambdaAdapterLayerArm64`, matching the arm64 architecture
every function here uses. It converts the Lambda event to an HTTP request against a local port, so
the function is an ordinary HTTP server. **No ASGI server and no web framework are added**: the
adapter speaks HTTP 1.1/1.0, so the standard library is sufficient and the SSE relay emits only
validated `ConversationStream.v3` public frames. The block contract and release safety rules are
owned by the [Conversational Runtime low-level design](../capabilities/conversational-runtime/low-level-design.md).

**Identity is verified inside the function, by the same domain code the authorizer uses.** A Function
URL admits no API Gateway authorizer, so the conversation handler calls
`domain/identity/authorization.authorize` directly — one implementation, a second call site. Because
POST is a mutating method, the existing CSRF double-submit and `Sec-Fetch-Site` checks apply to this
route without being written again.

**Four independent controls, each answering a different question.** This is the security posture the
decision has to earn, so it is enumerated rather than implied:

| Control | Question it answers |
|---|---|
| `AuthType: AWS_IAM` on the Function URL | is this request from a principal we trust? |
| OAC with SigV4 signing, resource policy scoped to the distribution | is it from *our* distribution rather than the internet? |
| `authorize()` on `adl_session` in the function | who is asking? |
| Cognito JWT authorizer on the harness, same token as bearer (removed 2026-09-04; restored 2026-09-26 by [ADR-077](adr-077-carry-the-end-user-identity-to-every-hop-through-token-exchange.md) §2; since S3 the same day, a token-service JWT authorizer with the route-minted token as bearer) | does the agent agree who is asking? |

**Caching is disabled on this behaviour, and that is a security control rather than a performance
choice.** A cached authenticated response would serve one user's answer to another. The cache policy
must also not forward `Authorization`: CloudFront replaces that header with its own SigV4 signature,
and preserving a client value makes the function reject the request.

## Consequences

**The browser must hash every request body.** With a Function URL origin, `PUT` and `POST` require
the caller to compute SHA-256 of the body and send it as `x-amz-content-sha256`; Lambda does not
accept unsigned payloads. This is the real cost of the decision and it is permanent: every client,
including tests and any future one, carries the obligation. It is roughly five lines of
`crypto.subtle.digest`, and it belongs in exactly one place in the frontend so no caller can forget
it.

**The failure mode when it is wrong is misleading**, which is worse than its size. A missing or
incorrect hash produces `403 InvalidSignatureException`, which reads as an authentication problem
rather than a body-hashing one. Whatever wraps the call must fail with a message naming the real
cause.

**A GET route was considered to avoid it and is rejected on privacy.** No body means no payload hash,
but the question would then travel in a URL — into CloudFront access logs, browser history and
referrer headers. Users' commercial questions in logs is worse than hashing a body.

**Duration ceases to be a design constraint.** Lambda's own timeout bounds a turn, up to 15 minutes,
against 29 seconds through API Gateway. The per-turn budget therefore comes from
`agent.<mode>.timeoutSeconds` and `maxIterations` — a declared property of the harness, which is
[G-04](../decision-coverage-registry.md#g-04--no-waf-rate-limiting-or-per-user-usage-admission)'s budget — rather than
from a transport limit nobody chose.

**WAF is unaffected.** It attaches to CloudFront, which fronts every route, so the protection G-04
requires is available identically before and after this decision. WAF cannot attach to an HTTP API at
all, so this is not a capability that was traded away.

**A second cookie-verification call site is a real cost and is bounded.** The rule that matters is
that exactly one module decides who is asking, and that still holds — `domain/identity` is the only
implementation, and `session_handler`'s warning against a second identity *source* is respected. What
has doubled is the number of adapters calling it, not the number of answers it can give.

**CloudFront needs two Lambda permissions, and granting one produces a 403 that names nothing.**
Since October 2025 a new Function URL requires both `lambda:InvokeFunctionUrl` and
`lambda:InvokeFunction` [verified 2026-08-22,
[Lambda function URL auth](https://docs.aws.amazon.com/lambda/latest/dg/urls-auth.html)]. A probe
granting only the first returned 403 for every request through CloudFront while succeeding for an IAM
user — the user's identity-based policy already carried `InvokeFunction`, and a service principal has
none. The two statements differ in more than their action: one is conditioned on
`lambda:FunctionUrlAuthType`, the other on `lambda:InvokedViaFunctionUrl`, so neither is a general
grant to invoke the function.

`FunctionUrlOrigin.withOriginAccessControl` emits only the `InvokeFunctionUrl` statement [verified
2026-08-22 by reading aws-cdk-lib 2.265.0]. The obvious construct therefore synthesises cleanly,
deploys cleanly, and fails every request. Both statements are written by hand in `ApiStack` and
asserted in `infra/test/conversation-route.test.ts`, which is also what keeps a future CDK bump from
silently producing a third.

**The edge must evaluate the chat pattern before `/api/*`.** CloudFront takes the first behaviour
whose pattern matches rather than the most specific one, so `/api/*` listed first swallows every
conversation and sends it to API Gateway — into the exact 29-second timeout this decision exists to
escape, presenting as a chat that always fails slowly rather than as a misrouted one.

**`x-amz-content-sha256` cannot be forwarded, only supplied.** CloudFront refuses it in an origin
request policy allowlist with `InvalidArgument`, because it consumes the header as signing input
[verified 2026-08-22]. So the browser obligation above is not avoidable by configuring the edge; the
header has to come from the client and be consumed at the edge.

**The caller's own access token is the credential on the onward hop, not the function's role.**
(Removed 2026-09-04; restored 2026-09-26 by
[ADR-077](adr-077-carry-the-end-user-identity-to-every-hop-through-token-exchange.md) §2. Between
those dates this passage and the three consequences below it were historical. Amended 2026-09-26 by
ADR-077 slice S3: the bearer is now the single-turn token the route mints from the caller's
verified access token, and the authorizer names the token service rather than this pool.) The
harness declares a `customJWTAuthorizer` naming the token service's issuer, the Harness audience,
and the route client, so SigV4 is refused outright — *"This harness requires OAuth Bearer token
authentication"* [verified 2026-08-22] — and so is the caller's Cognito token. The route exchanges
the `adl_session` access token it has just verified for a single-turn Harness token bound to that
user and presents it as the bearer, so the harness authorizes the **end user** rather than merely
the function that called it. That is stronger than a role-based hop: a bug in this route cannot
invoke the agent as somebody else, because minting a Harness token requires that user's live
token. The exchange and the claims are owned by
[Conversational Runtime low-level design §6](../capabilities/conversational-runtime/low-level-design.md#6-interfaces-and-contracts).

Three consequences follow, and the first was got wrong before it was got right.

*The client cannot be built once.* A client cached at module scope would hold one user's token and
present it for the next user's turn. The unsigned bearer client (`bearer_harness_client`) is built
per request for the minted token, and `AgentCoreManagedHarness` holds no credential of its own.

*An empty token fails against the wrong subsystem.* Suppressing SigV4 and sending
`Authorization: Bearer` with nothing after it makes the service parse that header *as* a SigV4
credential, answering `AccessDeniedException: Authorization header requires 'Credential' parameter`.
That names IAM, so it reads as a missing grant; establishing that it was not cost a policy simulation
and two deploys. The constructor now refuses a blank token, so the failure names the credential.

*The IAM grants on the function's role are consequently dead, and now demonstrably so.* A full
authenticated turn succeeded on 2026-08-23 with the bearer token as the only credential:
`bedrock-agentcore:InvokeHarness` and `InvokeAgentRuntime` are granted and were never consulted. The
condition for removing them is therefore discharged and they should come out. (They came out
2026-09-26 with ADR-077 S2.) The paired
explicit `Deny` on `InvokeAgentRuntimeCommand` and `InvokeAgentRuntimeCommandShell` stays regardless:
this function talks to AgentCore, so a broad policy attached to its role later is a realistic threat,
and a `Deny` cannot be overridden by a subsequent `Allow`.

**Verified end to end 2026-08-23.** A quick-chat turn returned 200 with **200 streamed chunks**, first
text at 4.72 s, last at 13.09 s, median inter-chunk gap 0.02 s — token-by-token, not buffered, which
is the property this whole transport was chosen for. The turn called one tool and answered from it.

The latency breakdown from the same turn: 12,883 ms total, first text at 4,541 ms, longest gap 4,541 ms
attributed to `start`. So the dominant wait is admission plus the model's first decision rather than the
tool, and the trace corroborates it — the second `chat` span alone was 5,459 ms of a 10,755 ms agent
invocation. That is the diagnosis §3.2 of observability exists to produce.

**The JWT authorizer makes an automated end-to-end agent test impossible today**, which is a
testability cost rather than a security one. Every path to the agent — this route, and
`validation/probe_harness.py` — needs a live user access token, valid for fifteen minutes, obtainable
only by a human completing a browser login: the app client permits `ALLOW_REFRESH_TOKEN_AUTH` and
nothing else. So there is no unattended test of a real turn.

**Amended 2026-08-23.** This paragraph named a second app client permitting `ADMIN_USER_PASSWORD_AUTH`
as the fix, held back only because widening what may authenticate to the agent is a decision rather
than a convenience. That framing was wrong: the objection is fidelity, not permission. A test client
authenticating by a flow no real user takes would exercise a path the product does not have, so it
would prove less than the route adopted instead — one browser login, held as a refresh cookie in the
environment, exchanged for an access token through the deployed `/api/auth/refresh` exactly as the
browser does it. What remains open is the unattended half, not the tested half. See
[G-41](../decision-coverage-registry.md#g-41--the-agent-cannot-be-tested-end-to-end-without-a-human-completing-a-browser-login--closed-2026-08-23).

## Alternatives rejected

**API Gateway HTTP API, buffered.** Cannot stream, and 29 seconds is below the measured deep dive
plus any margin. Rejected on both counts.

**API Gateway REST API with `responseTransferMode: STREAM`.** Genuinely viable and the closest call:
it streams, it keeps one authorizer, and it needs no body hashing. Rejected on operational cost
rather than on capability. Raising the timeout past 29 seconds requires a change in **four** places —
account service quota, API method, stage deployment, Lambda timeout — and this repository is deployed
into the customer's own account, so that becomes a step every customer performs and a support burden
forever, with a missed step surfacing as a 504 that looks like our defect. Ninety seconds of headroom
after that work, against fifteen minutes for free.

One thing would reverse this, and it was not established: whether `STREAM` exempts a REST API from the
integration wait, since bytes begin flowing immediately. If it does, REST needs no quota change and
wins outright. It is a half-hour spike and is recorded as the review trigger below rather than
assumed either way.

**API Gateway WebSocket.** Rejected before this decision by Q-03, and re-examined here because it
looks like the obvious answer to a long-lived response. It is not: the 29-second integration timeout
applies per route, so a 25-second turn still needs an async worker pushing to the connection, a
connection registry, four route handlers and reconnect logic. Cookie auth is additionally blocked
because the `$connect` authorizer event carries no cookies, and the workaround puts a single-use
ticket in a `wss://` query string — a credential in a URL, which is the same objection that rejected
a GET route. It buys bidirectionality that Q-03 descoped.

**Function URL with `AuthType: NONE` plus a secret CloudFront header.** Removes the body-hash
obligation entirely, and the function would still verify the cookie, so a leaked header yields no
data. Rejected because it yields *invocation*: the endpoint becomes publicly callable and the
exposure is cost amplification against the customer's bill, which is precisely what G-04 exists to
prevent. Trading an IAM control for a shared secret to save five lines of frontend code is the wrong
direction.

**A Node.js conversation function using `streamifyResponse`.** Streams natively with no adapter.
Rejected because cookie verification would have to be reimplemented in TypeScript, which is the
second identity *implementation* this decision exists to avoid — and two implementations of "who is
asking" is the defect, not two call sites of one.

## Review trigger

Any of these reopens it:

- `responseTransferMode: STREAM` is confirmed to exempt a REST API from the integration timeout, which
  removes the per-customer quota step and makes REST strictly better.
- Lambda adds native response streaming for the Python managed runtime, which removes the adapter.
- Mid-generation interruption becomes a product requirement, which reopens Q-03 and transport together.
- CloudFront is found to buffer rather than forward an SSE stream, which would remove the benefit this
  decision was made for. **Not yet verified**, and it should be measured before the origin is wired:
  compare time-to-first-byte at the Function URL directly against the same request through
  CloudFront.
