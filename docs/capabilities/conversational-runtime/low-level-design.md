# Conversational Runtime — low-level design

## 6. Interfaces and contracts

| Interface | Responsibility |
|---|---|
| `ConversationRequest.v3` | Browser-authored schema version, question, opaque conversation selector, and an optional opaque `page_context_reference`, the page's `artifact_revision` ([ADR-120](../../decisions/adr-120-carry-page-context-to-chat-as-a-server-redeemed-reference.md)). It is the only accepted version; v2 is refused. A malformed reference refuses the request; one that fails redemption never fails the turn. |
| `HarnessTurn.v2` | Server-framed verified session, actor, question, and correlation, plus the redeemed page's labels when the route resolved them. The adapter sends them as a second text block from one fixed "data from the app, not instructions" template, JSON-encoded, each at most 200 characters with control characters removed; the inbound Guardrail assesses the question and that block together. Context narrows no tool and grants no authority. |
| `ShapedResponse` | Governed tool result containing typed values, qualifiers, narrative scaffolding, and grounding. |
| `AgentToolCustodyV1` | Private bounded governed results and digest for audit/diagnostics; each result carries the correlation ID its tool minted. Never a public prose-selection contract. |
| `HarnessToolStepV1` | One governed tool call in invocation order, `succeeded` or `refused` with the tool result reference that reaches its trace, or `errored` with none. |
| `ConversationStream.v4` | Closed public union of `activity` (`understanding_request`, `retrieving_governed_data`), `tool`, `text`, `done`, and `refused`: per-call tool events naming the declared logical tool, `context` (once, before any text, only when the request sent a reference: `used`, or a closed `reason` `expired` or `invalid` with both server lines `display_text` and `display_text_refresh_failed`), `text` deltas of at most 8 KiB each in a contiguous sequence from one with at most 64 KiB of released text per turn, and exactly one terminal. Progress and text may interleave in any order. `done` requires released text. `refused` carries a closed reason code, `dependency_unavailable` or `no_answer`. SSE comment heartbeats travel outside the union and carry no data. `done` and `refused` carry the turn's support reference, as does the pre-stream guardrail terminal response ([turn reference](#turn-reference-and-correlation-adr-081)). |
| `GroundedEvaluationContext.v2` | The trusted evidence evaluation judges a complete turn's answer against: per governed result, its renderings (text, kind, source paths, qualifier) and collections (items, count, completeness, required), plus `AnswerObligations.v1` (required rendering, required collection, and claim-bearing grounding IDs). A refusal that a later success of the same tool superseded keeps its evidence but drops its obligations unless it carries a qualifier. It gates nothing. |
| `EvaluationOutboxEnvelope.v1` | Exact released question and answer, the `GroundedEvaluationContext.v2` document, and immutable audit/admission/delivery identity, retained only in evaluation evidence custody. |
| `ConversationAuditRecord.v6` | Content-minimized terminal receipt with trace ID, the page reference the turn was sent and its context status (never label text), the governed tool trajectory, custody and prompt digests, Guardrail version, the `conversation-stream.v4` response contract, the `answer.markdown.v1` answer format, the released character count, typed failure codes, outcomes, and the verified principal (ADR-077 §6). Stored v4 and v3 receipts, which name `public-block-stream.v3` and may carry plan digests, block kinds, and finalizer steps, are read as v6 with no released characters; v5 receipts read with no page context; v2 receipts also read with no principal. |

Retired by [ADR-083](../../decisions/adr-083-stream-the-agent-answer-and-govern-it-by-audit-and-evaluation.md): `PresentationPlan.v2`, the `presentation_publish` finalizer and its `PresentationFinalizerResult.v3`, `ConversationStream.v3` emission (the frontend still decodes it during the rollout), semantic verification and materialization, and the per-block outbound Guardrail.

### Harness lifecycle

1. The adapter forwards every assistant text delta as the Harness delivers it, including text written before and between tool calls. When a later assistant message starts with text after earlier text was sent, a paragraph break (`\n\n`) precedes it, so the released text reads as written. Reasoning content is dropped and never leaves the adapter.
2. Catalog and measurement tool inputs remain private and are discarded.
3. A governed tool result is decoded when its result message closes: success strictly as `ShapedResponse`, a typed refusal as its bounded reason and grounded refusal projection. Raw result transport is not logged or publicly serialized. Each call yields `requested` and `completed` tool events and one trajectory step.
4. An error-status governed result is a recoverable tool-attempt failure, not a typed refusal and not evidence. The adapter bounds and discards its raw content, appends an `errored` trajectory step with no result reference, and continues reading while the model recovers in its own loop.
5. Every stream event is checked against the Converse message grammar: one message at a time, blocks opened before their deltas, and a stop reason that matches the message content. A grammar violation, a service error event, released text past 64 KiB, or a stream that closes before `end_turn` or with an unanswered tool call is a dependency failure. Text already sent stays on screen, and the receipt keeps the trajectory reached so far.
6. The turn is one `InvokeHarness` call. There is no finalizer and no server-authored repair message; `end_turn` closes the lifecycle.

### Narrative and publication rules

- The agent authors wording, explanations, transitions, and the Markdown format of the answer.
- Every figure the agent states is returned by a governed tool this turn or validly derived from those figures, under the figure rule of [ADR-082](../../decisions/adr-082-govern-the-agent-by-evaluation-and-audit-not-text-heuristics.md); publication does not block on it, and evaluation and audit measure it. The agent must preserve material tool-authored coverage, freshness, suppression, basis, scope, and limitation information; the grounded-correctness evaluator withholds its top rating from an answer that omits a mandatory qualifier or required collection.
- The API never compares text to renderings, never rewrites it, and applies no outbound Guardrail. Its only checks on released text are the stream's size and sequence bounds.
- The browser renders the text as Markdown under the [rendering rule](architecture.md#release-semantics).
- The receipt, and for a complete turn the admission and outbox, are written after the Harness turn ends and before `done`. A turn whose model ended without text is a governed refusal (`no_answer`); a Harness or stream failure is a dependency failure (`dependency_unavailable`). A client that disconnects mid-stream leaves a `client_disconnected` receipt counting the text it was sent.

### Evaluation outbox

Only a complete turn is admitted. For a complete turn with the evaluation plane provisioned, the request path performs one DynamoDB transaction containing:

1. immutable content-minimized audit `RECORD`;
2. immutable content-free `EVALUATION_ADMISSION` linkage;
3. retained content-bearing pending outbox row keyed by `admission_ref`, carrying the exact concatenated text the stream released and its `GroundedEvaluationContext.v2`.

If the evaluation envelope cannot be built from the released pair (for example, an answer of whitespace only or one past the envelope's size bound), the receipt is written without admission and the refusal is reported; the turn still ends with `done`.

The outbox ready GSI is KEYS_ONLY. A scheduled dispatcher queries bounded due work, conditionally leases pending or expired items, sends the existing schema-v2 SQS evaluation envelope, and then atomically marks the outbox delivered while writing the audit delivery receipt. Send failures return the owned lease to pending with bounded backoff. SQS plus DynamoDB is at-least-once; the stable delivery reference and downstream writer identities are derived from admission custody rather than SQS message ID.

The outbox table uses managed encryption, on-demand billing, point-in-time recovery, and RETAIN policy. Pending and leased rows never expire. Delivered rows are removed from the ready index and receive TTL according to evaluation-evidence retention.

The route's outbox grant and table name render whenever the evaluation plane is provisioned, so the route admits every released turn and has no evaluation setting of its own ([ADR-079](../../decisions/adr-079-enable-online-evaluation-only-through-a-verifying-gate.md)). The dispatcher decides what reaches evaluation: it reads the Agent Quality Loop's per-family [release records](../agent-quality-loop/low-level-design.md#release-records-and-dispatch-adr-079) on every run and gives every row it does not send a terminal state and expiry ([dispatch rule](../agent-quality-loop/low-level-design.md#evaluation-states-dispatch-and-provenance-adr-079)). ADR-080 adds the receipt's creation time to the envelope ([record-family storage](../agent-quality-loop/low-level-design.md#record-family-storage-mandated-by-adr-080)). All three are specified there, not here.

### Turn reference and correlation (ADR-081)

This section is the only home of the support reference and correlation rule ([ADR-081](../../decisions/adr-081-make-every-turn-traceable-and-attributable.md) §2). It is deployed and dev accepted ([validation results](../../../validation/RESULTS.md#turn-reference-and-correlation-adr-081-slices-8a-and-8b--2026-09-30), 2026-09-30), except the refused-exchange receipt, which is covered by unit tests only.

- **One server-minted correlation per request.** Each request gets exactly one correlation identifier, a UUID4 minted at its first server hop. On authorized HTTP API routes that hop is the authorizer, which passes the value to the handler in its context. On routes without an authorizer, the conversation route and the auth handler's four routes, the handler mints it. No hop reads a client-supplied value, and every handler response returns the value in the `x-correlation-id` response header (`src/adapters/inbound/correlation.py` for the HTTP API handlers, the route's own header for the conversation stream and its pre-stream responses). An authorizer denial is answered by API Gateway itself and carries none.
- **Support reference.** Every response that ends a turn with a receipt carries the turn's `audit_id`: the stream's `done` and `refused` events, and the `ConversationTerminal.v2` response an inbound guardrail intervention returns. The field is `null` only when the receipt write failed, or when the runtime could not produce its terminal (the relay refused the runtime's `done`, or the runtime's generator had already finished); the receipt is then found by the response header's correlation. The chat UI shows it as plain text, `Reference: <audit_id>`, under the answer or refusal. It grants nothing by itself; retrieval still needs [review entitlement](../protected-quality-review-api/low-level-design.md#authorization-contract-adr-078).
- **A refused exchange** answers ADR-007's undifferentiated 403, so its body carries no reference; the receipt exists and is found by the correlation in the response header.
- **Contract order.** `audit_id` is an optional key in each decoder, so the frontend that accepts it deploys before the route that sends it.
- **Retired:** the browser's correlation-identifier generator and its `x-correlation-id` request header, the forwarded `x-correlation-id` in the conversation origin request policy's header allowlist (`infra/lib/stacks/frontend-stack.ts`), and every server hop's adoption of a client-supplied value.
- Receipt coverage for every terminal class is owned by the [Agent Quality Loop](../agent-quality-loop/low-level-design.md#terminal-coverage-and-retention-mandated-by-adr-081), and the terminal log line by [observability](../../concepts/observability.md#39-turn-terminal-log-adr-081).

Live dev acceptance:

1. A fictional answered turn's `done` event, a fictional refused turn's `refused` event, and a fictional inbound guardrail intervention's pre-stream terminal response each carry an `audit_id` equal to that turn's receipt key, and the chat UI shows it.
2. A request sent with a client `x-correlation-id` receives a different server-minted value in the response header, and the receipt and terminal log line carry the server value.
3. On an authorized route, the authorizer's log line and the handler's log line for one request carry the same correlation identifier, equal to the response header.
4. The deployed frontend sends no `x-correlation-id` header.

### Caller identity contract (mandated by ADR-077; deployed end to end)

This is the contract [ADR-077](../../decisions/adr-077-carry-the-end-user-identity-to-every-hop-through-token-exchange.md)
implementation must satisfy. Every check fails closed. Every entry point and hop below is deployed
to dev and live-validated, except the refusals the validation results name as covered by unit or
infrastructure tests only
([slice 3](../../../validation/RESULTS.md#identity-chain-slice-s3-acceptance--2026-09-26),
[slices 4 and 5](../../../validation/RESULTS.md#identity-chain-slice-4-and-5-acceptance--2026-09-26),
[audit principal](../../../validation/RESULTS.md#identity-chain-audit-principal-and-hardening--2026-09-26)).

**Token service entry points.** The issuer is the public issuer's REST API stage URL. Each exchange
entry point issues exactly one audience:

| Entry point | Contract |
|---|---|
| `/.well-known/openid-configuration` (public) | Discovery document for the issuer. |
| `/jwks.json` (public) | Key set built from the KMS signing key's public key. Throttled separately from `/token`. |
| Route exchange (its own function, invoked directly, not routed) | An `AWS::Lambda::ResourcePolicy` denies `lambda:InvokeFunction` to every principal whose `aws:PrincipalArn` is not the Conversation Function role. The request carries the subject token, and at most a flat string map under `headers`, the trace carrier the SDK's instrumentation adds; any other member is refused. Issues a Harness-audience token. |
| `/token` (public) | RFC 8693 token exchange: grant `urn:ietf:params:oauth:grant-type:token-exchange`, `subject_token_type` `urn:ietf:params:oauth:token-type:jwt` or `urn:ietf:params:oauth:token-type:access_token`, client authentication `CLIENT_SECRET_POST`, checked before any KMS call. One registered client, the agent. Its secret is generated by Secrets Manager at deploy time and referenced with `clientSecretSource: EXTERNAL` [verified 2026-09-25, [CustomOauth2ProviderConfigInput](https://docs.aws.amazon.com/bedrock-agentcore-control/latest/APIReference/API_CustomOauth2ProviderConfigInput.html)]. Issues a Gateway-audience token. No refresh tokens. |

The public issuer is a regional REST API with AWS WAF attached to its stage: a rate-based rule per
source IP and the AWS managed IP-reputation rule group. Three resource-side Denies name, by
`aws:PrincipalArn`, the only principals they exempt, so they deploy before those roles exist:

- **KMS key policy, `kms:Sign`:** the route-exchange and issuer roles.
- **Route-exchange function policy, `lambda:InvokeFunction`:** the Conversation Function role.
- **Agent client secret, `secretsmanager:GetSecretValue`:** the three principals probe P2 observed
  reading it [verified 2026-09-26, dev]: the issuer role, which compares the presented secret; the
  Harness execution role, which AgentCore Identity uses to present it at `/token`; and the exact
  CDK CloudFormation execution role the stack deploys with, taken from its synthesizer, which the
  credential provider's deployment handler uses. `/token` re-reads the secret on a mismatch at
  most once a minute per container, so a wrong secret under the public client id cannot turn a
  flood into store reads.

The signing key's public half is not a secret: `/jwks.json` publishes it, and the issuer and the
tool roles may read it with `kms:GetPublicKey`.

**Route exchange validation:** the subject token is a Cognito access token passing the same
verification the route uses (signature, `iss`, `client_id`, `token_use=access`, `exp`).

**`/token` validation:** client credentials; grant and token types; no actor parameter; an
`audience` or `resource`, when sent, equals the configured Gateway audience (AgentCore sends
neither, and `scope` is not interpreted); the subject token is a token of this issuer with the Harness
audience and the route client, passing signature, `iss`, `aud`, `alg`, and `exp`. A Cognito subject
token is refused.

**Minted token**, signed by a KMS asymmetric RSA `SIGN_VERIFY` key; header `alg` `RS256` and a `kid`
identifying the signing key. Asymmetric KMS keys do not rotate automatically, so a rotation is a new
key and a new `kid` [verified 2026-09-25,
[KMS automatic rotation](https://docs.aws.amazon.com/kms/latest/developerguide/rotating-keys-enable.html)].

| Claim | Value |
|---|---|
| `iss` | Token-service issuer |
| `sub` | Subject token's Cognito `sub` |
| `aud` | The Harness audience (route exchange) or the configured Gateway audience (`/token`) |
| `act.sub`, `client_id` | The route client (route exchange) or the agent client (`/token`) |
| `iat` | Issue time |
| `exp` | Harness token: `iat` + the Conversation Function timeout + a clock-skew allowance, independent of the subject's `exp`. Gateway token: not later than its Harness subject token's `exp` |
| `jti` | Unique token identifier |

**Per hop:**

- **Route:** exchanges the verified Cognito access token through the route exchange and presents
  the minted Harness token as the bearer, on an unsigned client built per request
  (`bearer_harness_client`); `AgentCoreManagedHarness` has no SigV4 default. The request carries
  exactly the session header, the trace-context header, `harnessArn`, `qualifier`, `actorId`, and
  `messages`; no override field is ever sent. One exchange per turn; the minted token is never
  cached, logged, or returned. The exchange client is built once per process. Denials follow
  ADR-007: 401 `{"error":"unauthorized"}` only when no `adl_session` cookie arrived, and 403
  `{"error":"forbidden"}` for every other identity failure (an expired or invalid token, CSRF,
  Origin, or a refused exchange), so the browser refreshes once and retries. The Conversation
  Function role holds no `bedrock-agentcore` Allow and explicitly denies
  `InvokeHarness`, `InvokeAgentRuntime`, `InvokeAgentRuntimeCommand`, and
  `InvokeAgentRuntimeCommandShell` on every resource.
- **Harness:** `customJWTAuthorizer` with the token-service discovery URL, `allowedAudience` = the
  Harness audience, and `allowedClients` = the route client, so a Cognito token and a
  Gateway-audience token are both refused. The Gateway tool uses `outboundAuth.oauth` with
  `grantType: TOKEN_EXCHANGE`, the credential provider ARN, and one nominal scope. The Harness is
  owned by the `Custom::AgentCoreHarness` resource (platform constraints below). It exchanges once
  per turn, when it loads its Gateway tools, presenting its inbound token as the subject
  [verified 2026-09-26, P2].
- **Gateway:** `CUSTOM_JWT` with the token-service discovery URL, the configured audience, and the
  agent client; one REQUEST interceptor with `passRequestHeaders: true`. MCP `initialize`,
  `notifications/initialized`, and `tools/list` reach it without a bearer, and every `tools/call`
  carries one [verified 2026-09-26, P3].
- **Interceptor:** on `tools/call`, passes the bearer token from `Authorization` to the target in
  the `Authorization` header, deletes every argument whose name starts with `_adl_` (the model
  never supplies one), and refuses with HTTP 401 and JSON-RPC error `-32001` when the bearer is
  absent or malformed. Every other method passes through unchanged. It never logs arguments or
  headers. Each tool target's terminal record names, without values, the client context and
  argument keys it received.
- **Tool:** reads the bearer only from the channel P3 observed, the `Authorization` entry of
  `client_context.custom["bedrockAgentCorePropagatedHeaders"]` (a map or JSON of one; exactly one
  such header in any case, `Bearer <token>`) [verified 2026-09-26, dev]. Before any other use of
  the call, it verifies `alg` RS256, the pinned `kid`, the signature, `exp` (inclusive), `iss`,
  `aud` = the Gateway audience, and `client_id` = `act.sub` = the agent client, and produces
  `CallerPrincipal(subject, actor)` or the tool's own typed refusal with code `ADLY-CALLER-0001`
  and terminal `result_class` `caller_unverified`. The key set is the signing key's public half,
  read with `kms:GetPublicKey` on the key's ARN (SSM `tokenSigningKeyArn`) on the container's first
  call and cached, never fetched from the public issuer; an unknown `kid` is refused, not
  refreshed, so a rotation is a redeploy. Neither the bearer nor a claim is logged; the terminal
  record carries only `caller_verified`.
- **Audit receipt:** the route stamps the verified `sub` on the turn's `ConversationAuditRecord.v6`
  as `principal`; every `managed-harness.v3` receipt carries one and no earlier generation may.
  The receipt's trajectory attributes each tool call in the turn to that principal. The principal
  never reaches the Harness (it receives only the actor digest), a terminal log, a metric, or a
  quality-review projection.
- **Memory:** `actorId` is a versioned function of `sub` alone (namespace `user-memory-v1`,
  `actor_id_for`); `runtimeSessionId` is per conversation. Records under the former
  per-conversation namespace (`semantic-publication-v2`) are not migrated. The strategies, their
  namespaces and how the Harness retrieves them are in [Long-term memory](#long-term-memory).

**External MCP gateway** ([ADR-150](../../decisions/adr-150-expose-the-governed-tools-to-amazon-quick-through-a-user-authorized-mcp-gateway.md);
built, off in every environment and refused outside dev; not yet deployed). A second entry to the
same two tool functions, for Amazon Quick. Setup and the demo are in
[Use Adlytics in Amazon Quick](../../amazon-quick.md).

- **Gateway:** `CUSTOM_JWT` with the user pool's discovery URL, `allowedClients` = the MCP client
  (SSM `mcpClientId`), `allowedScopes` = `openid`, and one custom claim, `quality_review_levels`
  CONTAINS `mcp_tool_use`; MCP 2025-06-18 and 2025-03-26, no search type, and the
  `external_client_rule` from `contracts/generated/tools.json` as its instructions. A regional web
  ACL is associated with it, with `FAIL_CLOSE`. Its tool targets are named `catalog` and
  `measurement`, as in-app, and attach only once every web ACL rule blocks; each tool definition's
  description ends with the same rule.
- **Interceptor:** its own function on `require_bearer_handler`, the same module's stricter entry
  point: every JSON-RPC method needs a bearer and is forwarded with it, or is refused with HTTP 401
  and JSON-RPC `-32001`. Its terminal record states the real `bearer_present` for every method.
- **Tool:** with `ADL_MCP_CLIENT_ID` set, the unverified header's `kid` selects the check. The token
  service's `kid` runs the check above, unchanged. Any other `kid` is verified against the user
  pool's key set (cached per container, one refresh on an unknown `kid`): signature, `exp`
  (inclusive), `iss` = the pool issuer, `client_id` = the MCP client, and `token_use=access`. The
  level `mcp_tool_use` is then required, or the refusal is `ADLY-CALLER-0001` with the cause
  `entitlement_missing`. A key-set read failure refuses with `key_set_unavailable` and reports the
  dependency failure. The principal is `CallerPrincipal(sub, the MCP client)`. Without
  `ADL_MCP_CLIENT_ID`, any token but the token service's is refused as `assertion_refused`.
- **Audit:** this path has no route, so no conversation audit receipt and no evaluation. That is the
  owner-accepted risk ADR-150 records.

### Long-term memory

The retained `MemoryStack` declares three long-term strategies. The Harness derives one retrieval
entry per active strategy: topK 10 and relevance 0.2, each pinned to the strategy's ID
[verified 2026-10-18, dev `GetHarness`;
[Harness memory](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness-memory.html)].

| Strategy | Kind | Namespace | Retrieved into |
|---|---|---|---|
| `user_preference` | Managed | `/preferences/{actorId}` | Every conversation of the user |
| `advertiser_context` | Built-in with overrides | `/context/{actorId}` | Every conversation of the user |
| `summary` | Managed | `/summaries/{actorId}/{sessionId}` | Only its own conversation |

- **Recalled memory is context, never evidence** ([ADR-006](../../decisions/adr-006-agentcore-agentic-layer.md),
  and the figure rule under [narrative and publication rules](#narrative-and-publication-rules)).
  Recall may tell the agent who the user is and how they like answers. A figure the agent states
  still comes from a governed tool this turn. The prompt says so, and evaluation measures it.
  Nothing filters recalled text.
- Long-term records have no expiry today. Only events expire, at `agent.memoryExpirationDays`.
- **Set by [ADR-147](../../decisions/adr-147-treat-long-term-memory-as-context-never-evidence.md),
  deployed to dev and accepted live on 2026-10-18:**
  - `semantic` is replaced by `advertiser_context`, a built-in with overrides under
    `/context/{actorId}`. The `semantic` strategy's `/semantics/{actorId}` namespace was retired
    at cutover, and its records are not migrated. Its extraction and consolidation prompts
    (`contracts/prompts/v1/memory-context-*.md`) never store a measured result, and it runs on
    one model from `ADLYTICS_MEMORY_MODEL_ID`. A goal the user typed may be kept, attributed
    to the user, never as a measured result (ADR-147 amendment, 2026-10-19).
  - The memory's existing execution role gains invoke rights on that model's profile only.
  - A strategy-revision parameter makes every strategy change update the Harness.
  - The agent prompt states the context-not-evidence rule explicitly.
  - Record expiry is proposed, not decided.

### AgentCore platform constraints

Platform behaviors the retained Memory stack, the Harness stack, and the Gateway policy engine are
built around. Each is recorded with the evidence it had when it was first written down.

- **Memory event expiry is fixed at write time.** AgentCore Memory applies the configured event
  expiry to each event when it is written; changing it affects only future events, and existing
  events keep their original expiry and cannot be extended. The retention window is therefore a
  deploy-time decision, not a correctable setting. [verified 2026-09-25,
  [Create an AgentCore Memory](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/memory-create-a-memory-store.html)]
- **Long-term strategies do not backfill.** Only events created after a strategy reaches `ACTIVE`
  are processed, so a strategy added later is blind to earlier conversations. Every long-term
  strategy is declared with the environment, before real traffic. [verified 2026-09-25,
  [Enable long-term memory](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/long-term-enabling-long-term-memory.html)]
- **Event metadata is not encrypted with a customer-managed key**, so nothing sensitive belongs in
  event metadata, and adding a key to the memory would not change that. [unverified]
- **A strategy change on an attached memory needs `UpdateHarness`.** The Harness stores the
  retrieval configuration it derived, keyed by namespace and pinned to strategy IDs. AWS requires
  `UpdateHarness` after strategies are added or removed to refresh it [verified 2026-10-18,
  [Harness memory](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness-memory.html)
  and dev `GetHarness`].
- **The default semantic prompt keeps figures.** Its extraction treats tool and JSON payloads as
  facts about the user, and its fixed output schema asks for "specific numbers". A built-in with
  overrides replaces the instructions but never the schema. It needs a memory execution role and
  bills the model to this account [verified 2026-10-18,
  [semantic system prompt](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/memory-system-prompt.html),
  [custom strategies](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/long-term-configuring-custom-strategies.html)].
- **Long-term record expiry is API-only and forward-only.** `recordExpirySpecification`:
  - takes at least 31 days;
  - must agree between the semantic and user preference strategies, and between summary and
    episodic;
  - applies only to records created after it is set.

  `AWS::BedrockAgentCore::Memory` has no such property, and neither has aws-cdk-lib 2.273.0
  [verified 2026-10-18,
  [record time-to-live](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/long-term-record-ttl.html),
  CloudFormation template reference].
- **A Gateway's authorizer type cannot change in place.** The service refuses the update with
  "Authorizer type cannot be updated for an existing gateway", although the CloudFormation
  reference lists `AuthorizerType` as "No interruption"; moving IAM to `CUSTOM_JWT` replaces the
  gateway under a new name, and every consumer of its ARN must follow [verified 2026-09-26,
  dev deploy].
- **The Harness's Gateway-tool `TOKEN_EXCHANGE` grant is API-only.** CloudFormation's Harness
  schema accepts only `CLIENT_CREDENTIALS` and `AUTHORIZATION_CODE` and early validation rejects
  the value, while the control-plane API accepts it [verified 2026-09-26, change-set early
  validation and `UpdateHarness`]. The Harness stack therefore owns the Harness through a custom
  resource
  ([ADR-077, amended 2026-09-26](../../decisions/adr-077-carry-the-end-user-identity-to-every-hop-through-token-exchange.md)).
- **A custom OAuth2 credential provider needs an authorization endpoint even for token
  exchange.** With only a discovery URL whose document omits one, AgentCore Identity refuses the
  provider at use with "Provider with no Authorization Endpoint information"; explicit
  authorization-server metadata naming a nominal endpoint is accepted [verified 2026-09-26].
- **Harness built-in tools are closed only by `allowedTools`.** The built-in `shell` (runs bash) and
  `file_operations` (views, creates, and edits files) tools are available in every Harness session
  unless `allowedTools` restricts them, and an omitted `allowedTools` admits both
  ([Harness tools](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness-tools.html),
  recorded 2026-08-23). A dev deep dive selected `shell` unprompted before the allowlist was
  narrowed [verified 2026-08-23]. Built-in tools do not traverse the Gateway, so no Gateway control
  applies to them. The Harness stack therefore declares `allowedTools` as exactly the generated
  Gateway tools.
- **Guardrails in AgentCore Policy are region-gated.** They are supported in five regions — US East
  (N. Virginia), Europe (London), Europe (Stockholm), Asia Pacific (Sydney), and Asia Pacific
  (Tokyo) — and in no other region the availability table lists [verified 2026-08-24, the
  availability table in
  [guardrails in policies](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy-guardrails-in-policies.html)].
  Enabling the Gateway policy engine anywhere else is refused at synth rather than deployed as a
  control that evaluates nothing.

### Deployment sequence

`DEPLOY_ORDER` is authoritative. The retained Evaluation Evidence stack precedes Evaluation Input and the disposable outbox dispatcher; the dispatcher precedes the API producer. HarnessStack, with its immutable prompt, precedes ApiStack. ApiStack reads the Harness ARN at deploy time, so any HarnessStack change that replaces the Harness must be followed immediately by an ApiStack deploy; chat turns fail between the two.

The one-time ADR-083 migration deploys out of that order: the frontend, which decodes V3 and V4, first; then the stacks that read receipts, so the v5 contract is readable before it is written; then EvaluationV4Stack, whose gate recalibrates against `GroundedEvaluationContext.v2`; then HarnessStack and ApiStack back to back; and AgentStack, which removes the finalizer's Gateway target, last. Between the Harness and Api deploys the old route receives plain text and refuses the turn.

The one-time move of the [inbound Guardrail](#inbound-guardrail) to the Standard tier (2026-09-30) also deploys out of order: ApiStack first, which grants the Conversation function `ApplyGuardrail` on the guardrail profile, then AgentStack, which switches the guardrail to the profile. The other way round, the route would lack the profile grant between the two deploys and, failing closed, answer every question with a pre-stream 503; that order was not exercised. Run `make accept-guardrail` after AgentStack.

### Inbound Guardrail

The route applies one Bedrock guardrail to the question with `ApplyGuardrail` (`source=INPUT`, version `DRAFT`) after admission and before the token exchange; an intervention ends the turn with the `input_intervened` terminal response and an `input_policy_intervention` receipt. Configuration (`infra/lib/constructs/agent/route-guardrail.ts`):

| Policy | Setting |
|---|---|
| Content filter tier | Standard, through the source Region's guardrail profile (`us.guardrail.v1:0` from us-east-1), so assessment may run in any of the profile's US Regions |
| Prompt attack, misconduct (input) | `LOW`: only high-confidence detections block |
| Violence, hate, sexual, insults (input) | `MEDIUM` |
| Sensitive information | `BLOCK` for card number, US SSN, email, phone, AWS access and secret key, password |
| Contextual grounding | None: a question has no grounding source |

- Each refusal writes one `input_guardrail` line naming the checks that fired ([observability §3.11](../../concepts/observability.md#311-inbound-guardrail-refusal-log)).
- The alarm `adlytics-<env>-guardrail-intervention-rate` fires when more than a fifth of at least 20 assessments in an hour intervene.
- `make accept-guardrail` replays the fictional calibration set against the deployed guardrail after each `AgentStack` deploy and fails unless it is Standard tier, ran the full set, blocks no benign case, and misses at most one attack, a jailbreak ([route Guardrail acceptance](../../../validation/README.md#running-route-guardrail-acceptance)).

### Latency budget

For a turn with one governed tool call, measured from the request at the route:

| Milestone | Budget | Dev acceptance, 2026-09-30 |
|---|---|---|
| First streamed text | 8 s | 5.1–8.6 s over six warm turns; one exceeded the budget |
| Answer text after the tool result | 18 s | 10.0–17.7 s over four warm turns |
| Terminal (`done` or `refused`) | 25 s | 12.5–21.9 s over six warm turns |

The first request after a deploy is slower (first text at 14.6 and 15.9 s). The first text is the model's own sentence before its tool call; the answer starts when the model writes it after the tool result. Before the Harness starts, the route spends its own time on the inbound Guardrail and the token exchange. The Standard-tier [inbound Guardrail](#inbound-guardrail) took a median 511 ms against 190 ms for the Classic tier in place when the table above was measured, which adds roughly 0.3 s to every milestone (one round of five turns; [validation results](../../../validation/RESULTS.md#calibrated-inbound-guardrail-deployed--2026-10-01)). After that the time is the model's and the tools': the route forwards each delta the moment it arrives, and its only work before `done` is the receipt write. A change that adds time to this path states where it comes from against this table ([validation results](../../../validation/RESULTS.md#streamed-markdown-answer-adr-083--2026-09-30)). Each turn's timing line ([observability §3.10](../../concepts/observability.md#310-turn-timing-log)) places its milestones, and `validation/probe_turn_latency.py` joins them with the route and tool spans, the Harness runtime's own spans (same trace) and metrics, and the Lambda reports into a per-hop breakdown ([turn latency breakdown](../../../validation/RESULTS.md#turn-latency-breakdown--2026-09-30)).

### Acceptance

Fictional dev acceptance must prove dynamic governed tool selection, natural agent narration streamed as `text` events before the turn ends, Markdown rendering under the rendering rule, no raw internal metric identifiers in broad discovery output, and eventual dispatcher delivery. Each complete turn's receipt must name `conversation-stream.v4` and `answer.markdown.v1`, hold only governed trajectory steps each with its reference, record no plan or blocks, and count exactly the characters the stream released, with the receipt and outbox written before `done`. The `client_disconnected` receipt is unit-tested only: through CloudFront a viewer who leaves does not break the route's connection, so dev cannot produce one from outside. It must also attest the deployed prompt digest, that the Harness's allowed tools and the Gateway declare no finalizer, exact stack identities, and absence of direct SQS send permission on the Conversation function. Every admitted response requires outbox delivery and a writer receipt; an evaluator result is optional because the online evaluator applies its configured sampling percentage.

## 7. Concepts applied

- [Authentication](../../concepts/authentication.md)
- [Data-Flow Completion and Service Levels](../../concepts/data-flow-completion-and-service-levels.md) — applies to evaluation handoff and outbox movement, not the interactive response clock.
- [Publication State](../publication-state/README.md)
- [Query Path](../query-path/README.md)
- [Observability](../../concepts/observability.md)

## 8. Decisions

- [ADR-006](../../decisions/adr-006-agentcore-agentic-layer.md)
- [ADR-038](../../decisions/adr-038-the-conversation-route-streams-from-a-function-url.md)
- [ADR-041](../../decisions/adr-041-guardrails-are-policy-at-the-gateway-and-applyguardrail-in-the-route.md)
- [ADR-042](../../decisions/adr-042-guardrails-at-the-gateway-do-not-reach-a-lambda-target.md)
- [ADR-061](../../decisions/adr-061-bind-agent-authored-presentations-to-governed-evidence.md) (governed evidence; its release gate is superseded by ADR-083)
- [ADR-066](../../decisions/adr-066-turns-default-disclose-and-repair-instead-of-failing.md) (tool defaults and disclosure; its repair round is superseded by ADR-083)
- [ADR-074](../../decisions/adr-074-define-authority-backed-data-flow-completion-and-service-level-objectives.md)
- [ADR-077](../../decisions/adr-077-carry-the-end-user-identity-to-every-hop-through-token-exchange.md)
- [ADR-079](../../decisions/adr-079-enable-online-evaluation-only-through-a-verifying-gate.md) ([current state](../agent-quality-loop/low-level-design.md#record-family-storage-mandated-by-adr-080))
- [ADR-081](../../decisions/adr-081-make-every-turn-traceable-and-attributable.md) ([turn reference](#turn-reference-and-correlation-adr-081))
- [ADR-082](../../decisions/adr-082-govern-the-agent-by-evaluation-and-audit-not-text-heuristics.md)
- [ADR-083](../../decisions/adr-083-stream-the-agent-answer-and-govern-it-by-audit-and-evaluation.md)
- [ADR-147](../../decisions/adr-147-treat-long-term-memory-as-context-never-evidence.md) (proposed, deployed to dev; [long-term memory](#long-term-memory))
- [ADR-150](../../decisions/adr-150-expose-the-governed-tools-to-amazon-quick-through-a-user-authorized-mcp-gateway.md) (proposed, dev-only demo flag; [caller identity contract](#caller-identity-contract-mandated-by-adr-077-deployed-end-to-end))
