// SPDX-License-Identifier: MIT-0
/**
 * Typed per-environment configuration.
 *
 * Deliberately NOT cdk.json context: a typo in a context key is a runtime
 * `undefined`, while a typo here is a build error. See ADR-005. The one thing
 * context still carries is the environment *name* (`-c env=<name>`), because
 * selecting which configuration to build is exactly what context is for.
 *
 * The environment is a parameter, not a type. There is one configuration and one
 * code path, and both take a name: `-c env=sandbox` is as legal as `-c env=dev`.
 * A `'dev' | 'prod'` union made this repository's own two names look like the only
 * ones that exist, so every customer's first change was a source edit — and the
 * committed literals were whichever values the last bring-up happened to leave.
 */

import * as logs from 'aws-cdk-lib/aws-logs';
import { SELECTED_ARMS } from '../generated/alarm-destinations';
import { ATHENA_START_BUDGET, BAKE_TASKS } from '../generated/execution-graph-protocol';

/**
 * The placeholder that means "a human still has to supply this".
 *
 * Declared before the configuration that uses it, so it can be referenced rather
 * than restated as a bare string.
 */
export const UNSET = 'UNSET';

/**
 * What an environment name is allowed to be.
 *
 * The name is not cosmetic. It becomes a segment of every SSM parameter path
 * (`/adlytics/<env>/...`), part of the Secrets Manager secret name, and the tail of
 * every stack ID (`Adlytics-<Area>-<env>`). An unchecked name does not fail where it
 * was typed; it fails as a CloudFormation validation error on a name nobody chose,
 * or worse, resolves to a parameter path that merely looks absent.
 *
 * Verified against the AWS API references on 2026-08-19:
 *
 *  - CloudFormation `StackName`: alphanumeric (case sensitive) and hyphens only,
 *    must start with an alphabetic character, no longer than 128 characters.
 *    https://docs.aws.amazon.com/AWSCloudFormation/latest/APIReference/API_CreateStack.html
 *  - SSM `PutParameter` `Name`: only the characters `a-zA-Z0-9_.-` plus `/` as the
 *    hierarchy separator, no spaces, at most 15 hierarchy levels, and at most 1011
 *    characters for the portion a caller supplies. Names are case sensitive.
 *    https://docs.aws.amazon.com/systems-manager/latest/APIReference/API_PutParameter.html
 *
 * So the intersection of the two is alphanumerics and hyphens, starting with a
 * letter. Lowercase is stricter than either service demands and is imposed anyway,
 * because SSM parameter names are case sensitive: `/adlytics/Dev/api/id` and
 * `/adlytics/dev/api/id` are two different parameters, and a producer and consumer
 * disagreeing on the case reads as missing configuration rather than as a typo.
 */
export const ENV_NAME_PATTERN = /^[a-z][a-z0-9-]*$/;

/**
 * Not a service limit — the shortest derived identifier is what binds.
 *
 * A stack name allows 128 characters, which would leave about 110 here. A bare
 * Lambda function name allows 64 (verified 2026-08-19,
 * https://docs.aws.amazon.com/lambda/latest/api/API_CreateFunction.html), and
 * ApiStack builds `adlytics-<env>-AuthorizerFunction`, spending 29 characters
 * before the name. 24 leaves room for the next construct without anyone having to
 * recompute this, and no plausible environment name needs more.
 */
export const ENV_NAME_MAX_LENGTH = 24;

/**
 * Reject a name that cannot survive being turned into an AWS identifier.
 *
 * Called from every function that embeds the name in one, rather than at the app
 * entrypoint only, so there is no path that produces a malformed parameter path.
 */
function assertValidEnvName(envName: string): void {
  if (!ENV_NAME_PATTERN.test(envName) || envName.length > ENV_NAME_MAX_LENGTH) {
    throw new Error(
      `Invalid environment name ${JSON.stringify(envName)}. ` +
        'Must be lowercase letters, digits and hyphens, must start with a letter, ' +
        `and must be at most ${ENV_NAME_MAX_LENGTH} characters ` +
        `(pattern ${ENV_NAME_PATTERN.source}). ` +
        'The name becomes an SSM parameter path segment and the tail of every stack ID, ' +
        'so anything else is rejected here rather than at deploy.',
    );
  }
}

/**
 * The single public origin, and how the edge is wired to reach the API.
 *
 * No custom domain. CloudFront fronts both the static frontend and the API, so
 * the browser only ever talks to one host — which makes the session cookies
 * same-origin rather than merely same-site and removes the custom-domain
 * requirement that F-03 originally concluded was mandatory.
 */
export interface EdgeConfig {
  /**
   * Whether the distribution should route `/api/*` to the HTTP API.
   *
   * False on the first pass, when no API exists yet. The ordering is forced and
   * cannot be designed away without a custom domain: Cognito's callback URL needs
   * the CloudFront host, CloudFront needs the API as an origin, and the API needs
   * the user pool. Something has to be created before its consumer knows its name.
   *
   *   pass 1  apiOriginAttached: false -> deploy Frontend only
   *   pass 2  apiOriginAttached: true  -> deploy Auth, Api, then Frontend again
   *
   * The distribution's own domain is deliberately *not* configuration. FrontendStack
   * publishes it to `/adlytics/<env>/frontend/public-base-url` and the other stacks
   * read it at deploy, so no human copies a hostname between passes. This flag is
   * the one thing that cannot come from a parameter: on pass 1 the API parameter
   * does not exist yet, and a dynamic reference to a missing parameter fails the
   * deploy outright rather than resolving to nothing.
   *
   * A custom domain later removes the two-pass permanently, because every hostname
   * is then known before anything is built.
   */
  readonly apiOriginAttached: boolean;

  /**
   * Whether the distribution should route the chat path to the conversation Function URL.
   *
   * A second flag rather than reusing `apiOriginAttached`, and the two are genuinely independent.
   * They gate reads of parameters produced at different times: `apiOriginAttached` gates
   * `/adlytics/<env>/api/id`, which exists as soon as the HTTP API does, while this gates
   * `/adlytics/<env>/api/conversation-function-url`, which does not exist until the conversation
   * function has been created. Sharing one flag would mean an environment that has an API but no
   * conversation function cannot attach either — and a dynamic reference to the missing parameter
   * fails the deploy outright rather than resolving to nothing.
   *
   * Ships `false` for the same reason `apiOriginAttached` does.
   */
  readonly conversationOriginAttached: boolean;
  /**
   * ADR-078: the per-source-IP request ceiling, per five minutes, of the distribution's web ACL
   * rate rule. Required per deployment with no default, from
   * `ADLYTICS_EDGE_WAF_RATE_LIMIT_PER_FIVE_MINUTES`: the right ceiling depends on how many people
   * share one egress address, which only the deployer knows. The dev deployment declares 2000.
   */
  readonly wafRateLimitPerFiveMinutes: number;
  /**
   * The origin-verify secret version the distribution's custom header resolves, from
   * `ADLYTICS_ORIGIN_VERIFY_SECRET_VERSION_ID`. Absent, the reference is versionless. Set only
   * during a rotation (authentication.md, edge entry): CloudFormation re-reads a dynamic reference
   * only when the resource holding it changes, so pinning the new version is what makes the
   * redeploy resolve it.
   */
  readonly originVerifySecretVersionId?: string;
  /** Longest an authorizer or auth handler container holds the origin-verify values. */
  readonly originVerifyMaxCacheAgeSeconds: number;
  /**
   * Shortest interval between two re-reads a mismatched value triggers. Below the maximum age, so
   * a warm container meets a rotated value by re-reading, never by refusing until expiry.
   */
  readonly originVerifyRereadIntervalSeconds: number;
}

/** Managed login and the lifetimes of the cookies it produces. */
export interface AuthConfig {
  /**
   * Prefix for the Cognito-hosted login domain, giving
   * `<prefix>.auth.<region>.amazoncognito.com`.
   *
   * A prefix domain is sufficient, and that is not obvious: the session cookies
   * are set by our own API on the shared parent domain, and the hop to the
   * provider is a top-level navigation. So the provider's own hostname never
   * needs to be same-site, which removes a custom domain and its us-east-1
   * certificate from the critical path.
   */
  readonly loginDomainPrefix: string;

  /** How long the refresh and CSRF cookies live. Feeds ADL_REFRESH_COOKIE_MAX_AGE_SECONDS. */
  readonly refreshCookieMaxAgeSeconds: number;

  /** Cognito's own refresh token validity. Must not be shorter than the cookie. */
  readonly refreshTokenValidityDays: number;

  /**
   * Dev only. Enables USER_PASSWORD_AUTH on the confidential BFF client and
   * ADMIN_USER_PASSWORD_AUTH on the public review client, for synthetic validation identities.
   */
  readonly qualityReviewValidationPasswordAuthEnabled: boolean;
  /**
   * ADR-078: the per-source-IP request ceiling, per five minutes, of the user pool's own web ACL
   * rate rule. Required per deployment with no default, from
   * `ADLYTICS_USER_POOL_WAF_RATE_LIMIT_PER_FIVE_MINUTES`. Separate from the edge ceiling because a
   * sign-in is a few requests and a working session is hundreds. The dev deployment declares 300.
   */
  readonly wafRateLimitPerFiveMinutes: number;
  /**
   * Cookie scope, passed through to the backend as ADL_COOKIE_SCOPE.
   *
   * `host_only` is the only legal value on an AWS-provided hostname:
   * `cloudfront.net` is on the Public Suffix List, so a browser drops a cookie
   * that names it as a Domain — and drops it silently, which makes it the least
   * diagnosable failure available.
   */
  readonly cookieScope: 'host_only' | 'parent_domain';

  /** Registrable parent with a leading dot. Only for `parent_domain`. */
  readonly cookieDomain: string;
}

/**
 * Encryption at rest, and how long landed source objects are kept.
 *
 * Decided in ADR-018. The encryption field is the one place in this configuration
 * where a default is correct rather than dangerous: unlike a URL or an account ID,
 * both values are valid and neither silently points somewhere unintended.
 */
export interface DataConfig {
  /**
   * `s3_managed` is SSE-S3: no KMS, no per-request charge, no key policy.
   *
   * Deliberately not the AWS *managed* KMS key, which is the worst of the three —
   * it charges per KMS call on a workload that makes an enormous number of `GET`s,
   * and its key policy is AWS-controlled, so it buys no authorization layer over
   * SSE-S3.
   *
   * `kms_cmk` is the intended end state and is not implemented yet: the buckets
   * would encrypt correctly while no consumer holds `kms:Decrypt`, which surfaces
   * as an opaque `AccessDenied` at first query rather than at synth. Selecting it
   * fails the build until all required V2 grants exist. See ADR-018.
   */
  readonly encryption: 's3_managed' | 'kms_cmk';

  /**
   * Days before a landed object moves to cheaper storage.
   *
   * Restatement re-lands recent partitions on a rolling 30–45 day re-pull, so
   * objects younger than this are genuinely hot. Glacier Instant Retrieval keeps
   * millisecond access, because reprocessing from raw is rare and a restore job in
   * the way of it would be worse than the storage saving.
   */
  readonly rawTransitionAfterDays: number;

  /**
   * Days before a landed object is deleted. `'never'` keeps the audit trail.
   *
   * Default is `'never'`, and that is a decision rather than an omission. Raw is
   * the only artefact that proves what upstream actually delivered, and the
   * manifest is authoritative about what arrived — deleting the object it describes
   * undercuts that. Raw is also small: daily pre-aggregated AMC output and vendor
   * JSON for one advertiser is megabytes, so an expiry saves little while the
   * transition above captures most of the benefit.
   *
   * The consequence, accepted knowingly: raw is then a permanent copy, which puts
   * it in backup scope (G-12).
   *
   * A defensible cap is 400 days — past AMC's 13-month rolling retention, so
   * anything expiring could not have been re-extracted anyway. That loses audit
   * history older than the source itself and loses no recoverable data.
   */
  readonly rawExpireAfterDays: number | 'never';

  /** Days before a superseded version of a landed object is deleted. */
  readonly rawExpireNoncurrentVersionsAfterDays: number;

  /**
   * Days before an abandoned multipart upload is cleaned up.
   *
   * Not optional in practice. An interrupted upload leaves parts that are billed as
   * storage and appear in no object listing, so on a bucket receiving automated
   * writes the cost accrues invisibly.
   */
  readonly abortIncompleteUploadsAfterDays: number;

  /**
   * Days before an Athena result file is deleted.
   *
   * Athena writes a result object per query and never cleans up after itself. These
   * are disposable by definition — the curated table is the source of truth — so
   * this is purely a cost dial.
   */
  readonly athenaResultsExpireAfterDays: number;
}

/** AgentCore Runtime model configuration and per-turn resource ceilings. */
/**
 * One server-selected Runtime budget: which model answers and what one turn may spend.
 *
 * Runtime policy selects a bounded execution configuration after planning. The public endpoint does
 * not accept a mode or model override, so the selected configuration cannot be caller-controlled.
 */
export interface AgentModeConfig {
  /**
   * Bedrock model this mode reasons and narrates with.
   *
   * `UNSET` and read from the environment, for the same reason as `account` and
   * `loginDomainPrefix`: it is specific to one deployment and cannot be guessed here. Bedrock
   * model access is granted per account, the model catalogue differs by region, and the choice
   * is a cost decision the customer owns. A value committed in this file would name a model
   * their account may not have enabled — which fails at the first question rather than at
   * synth, and reads as the platform being broken rather than as unconfigured.
   */
  readonly modelId: string;

  /**
   * Ceiling on tool-call rounds in one turn.
   *
   * This and `maxTokens` are [G-04](../../../docs/decision-coverage-registry.md)'s per-turn budget, declared
   * rather than built around a hand-written loop. An authenticated loop is a cost-amplification
   * vector billed to the customer, so quick chat is capped hard: one tool call is the expected
   * shape and a question needing more should offer a deep dive rather than silently spend.
   */
  readonly maxIterations: number;

  /** Total output tokens across every model call in one invocation. */
  readonly maxTokens: number;

  /**
   * Wall-clock ceiling on one invocation.
   *
   * Well under AgentCore's own 15-minute synchronous limit, because the binding constraint is a
   * person waiting rather than the service.
   */
  readonly timeoutSeconds: number;
}

export interface AgentConfig {
  /** Initial planning configuration, retained only as server-side policy. */
  readonly quickChat: AgentModeConfig;

  /** Deeper server-selected execution configuration for plans that require it. */
  readonly deepDive: AgentModeConfig;

  /**
   * How long a raw conversation event is kept, in days. 7 to 365.
   *
   * **Chosen here because it cannot be corrected later.** AgentCore applies the expiry per event
   * at write time, so changing this affects only future events — everything already written keeps
   * the expiry it was written with and cannot be extended
   * ([Conversational Runtime LLD](../../../docs/capabilities/conversational-runtime/low-level-design.md) §6,
   * AgentCore platform constraints). A value discovered to be too
   * short is therefore not a config change, it is data already gone.
   *
   * Also the retention half of [G-08](../../../docs/decision-coverage-registry.md), which requires a stated
   * retention rather than an inherited default.
   */
  readonly memoryExpirationDays: number;
  /**
   * The model the long-term memory strategy's extraction and consolidation overrides run on.
   * From `ADLYTICS_MEMORY_MODEL_ID`.
   *
   * **Deliberately no fallback to the chat models** (ADR-147 decision 2). A fallback would move
   * memory onto whatever model the chat moves to, including one that refuses the request
   * parameters the memory service sends, and extraction runs in the background, so that failure
   * would be silent.
   */
  readonly memoryModelId: string;
}

/** Per-method API Gateway stage throttle for one public token-service route. */
export interface MethodThrottleConfig {
  /** Steady-state requests per second across all callers. */
  readonly rateLimit: number;
  /** Maximum concurrent burst above the steady rate. */
  readonly burstLimit: number;
}

/**
 * The token service ([ADR-077](../../../docs/decisions/adr-077-carry-the-end-user-identity-to-every-hop-through-token-exchange.md)
 * §3): the route exchange and the public issuer.
 */
export interface TokenServiceConfig {
  /**
   * Seconds added to one turn's lifetime when minting a Harness token, covering clock skew
   * between the token service and the Harness authorizer. ADR-077 §2.
   */
  readonly clockSkewSeconds: number;
  /**
   * Throttle per public route, each on its own method so the key set is never starved by a flood
   * on another route (ADR-077 Consequences, "Availability").
   */
  readonly publicRouteThrottle: {
    readonly discovery: MethodThrottleConfig;
    readonly jwks: MethodThrottleConfig;
    /** The RFC 8693 exchange AgentCore Identity calls for a Gateway token (ADR-077 S4). */
    readonly token: MethodThrottleConfig;
  };
  /** AWS WAF rate-based rule limit: requests per source IP per five-minute window. */
  readonly wafRateLimitPerFiveMinutes: number;
}

/**
 * The insight narration model seam — [ADR-076](../../../docs/decisions/adr-076-tier-insight-narration-across-card-and-deep-dive-models.md)
 * Phase 1.
 *
 * Separable from the chat agent's models on purpose: the insight envelope (forced tool
 * use, no sampling parameters) and the chat harness (pinned `temperature: 0`) make
 * different assumptions, so repointing one family must never repoint the other. The
 * runtime seam (`ADL_INSIGHT_QUICK/DEEP_MODEL_ID`) predates this config; Phase 1 adds
 * only the operator override and IAM. Absent an override, each value falls back to the
 * corresponding agent model, which is the pre-ADR-076 behaviour exactly.
 */
export interface InsightModelConfig {
  /** Card-route generation model (an inference-profile id, e.g. `us.anthropic.claude-sonnet-5`). */
  readonly quickModelId: string;

  /** Deep-dive-route generation model (an inference-profile id, e.g. `us.anthropic.claude-opus-5`). */
  readonly deepModelId: string;
}

/**
 * AgentCore Policy on the tool gateway — [ADR-041](../../../docs/decisions/adr-041-guardrails-are-policy-at-the-gateway-and-applyguardrail-in-the-route.md)'s
 * layer 2.
 *
 * **Two enforcement switches exist and they are not the same one.** This is the one on the
 * *gateway*, which decides whether the engine's verdict is acted on at all. Each individual
 * `CfnPolicy` carries its own `enforcementMode`, whose CloudFormation default is `ACTIVE` — so a
 * policy left unstated contributes to the decision while the gateway may or may not honour it.
 * Both are set explicitly for exactly that reason. ADR-041 said "enforcement mode is
 * configuration" before this distinction was known; the ADR records the correction.
 */
export interface GatewayGuardrailConfig {
  /**
   * Whether a policy engine is created and attached to the tool gateway.
   *
   * Off by default, like every other `envFlag`. Unlike the others the reason is not cost: a
   * policy engine is the strongest control in the four-layer model, and it ships off only because
   * it is region-gated on a narrower set than AgentCore itself, so a checkout that enabled it by
   * default would fail synth in most regions rather than deploying conservatively.
   *
   * **It must be set on every deploy once it has been set on one.** Absent means false, and false
   * means the engine is removed — the same trap `edge.apiOriginAttached` documents, and the reason
   * the deploy flags are exported together rather than remembered individually.
   */
  readonly enabled: boolean;

  /**
   * Whether the gateway acts on the engine's verdict, or merely records it.
   *
   * Ships `LOG_ONLY`, and the promotion to `ENFORCE` is deliberate rather than a default. A
   * policy engine in `ENFORCE` denies every action no policy explicitly permits, so the base
   * permit policy is what stands between a promotion and every tool call failing while all eleven
   * stacks report healthy. ADR-015 leaves no pipeline to catch that, which is why the safe value
   * is the shipped one.
   *
   * `LOG_ONLY` is also the only mode in which a threshold can be calibrated: AWS's own guidance
   * is to run real traffic through it and build a confusion matrix from the logged scores before
   * enforcing anything.
   */
  readonly enforcement: 'LOG_ONLY' | 'ENFORCE';
}

/** Shared provisioning and sampling settings for the online-evaluation control plane. */
export interface OnlineEvaluationConfig {
  /**
   * Whether the result link and the current evaluation family (V4) exist. Dev-only: V4 was never
   * promoted beyond dev, and the V3 family it superseded was retired 2026-09-27 (ADR-079).
   */
  readonly provisioned: boolean;

  /** Whole percentage of eligible traces evaluated. */
  readonly samplingPercentage: number;

  /** Inactivity interval after which the service closes a session for evaluation. */
  readonly sessionTimeoutMinutes: number;
}

/**
 * The evaluation quality alarms (ADR-079 §3 and §5). Design constants rather than per-deployment
 * values: they define what the alarms mean, and `EvaluationQualityStack` renders them.
 */
export interface EvaluationQualityConfig {
  /**
   * One alarm per evaluator on the share of its verdicts that carry its worst label. Evaluated
   * over one period, and only once at least `minimumVerdicts` verdicts landed in it: fewer leave
   * the alarm without data, so a quiet period never reads as healthy.
   */
  readonly worstLabel: {
    readonly periodHours: number;
    readonly minimumVerdicts: number;
    readonly shareThreshold: number;
  };
  /**
   * Turns admitted to the outbox, none dispatched, and no row withheld as not enabled, in each of
   * `periods` consecutive periods. Three, so a breach needs one whole period between two others
   * with admissions and no dispatch: CloudWatch evaluates the unfinished newest period too, and a
   * dispatch lags its admission by minutes.
   */
  readonly noDispatch: { readonly periodMinutes: number; readonly periods: number };
}

/** V4 execution request; V4 provisioning follows `onlineEvaluation`. */
export interface OnlineEvaluationV4Config {
  /**
   * Whether the V4 evaluation gate is asked to enable execution (ADR-079). A request, not a
   * state: the gate enables only after every readback and calibration holds. Dev-only.
   */
  readonly executionRequested: boolean;
}

/** Customer-owned transport and trace source for exact released responses. */
export interface EvaluationInputConfig {
  /**
   * Whether the plane exists. The route admits every released turn to the outbox whenever it
   * does; the dispatcher's release records decide what reaches evaluation (ADR-079).
   */
  readonly provisioned: boolean;
}

/** Development-only operational data-flow observation infrastructure. */
/**
 * Traceability FR-10: the exact-one-of staffed destination. The email arm uses the existing
 * in-account ingestion alarm topic; its endpoint is a live input to the destination verifier,
 * never configuration, so no address reaches a template or the repository. The topic arm imports
 * an existing confirmed topic in the target account and region.
 */
export type StaffedAlarmDestination =
  | { readonly arm: 'ExistingInAccountTopicEmail' }
  | { readonly arm: 'ExistingConfirmedSnsTopic'; readonly topicArn: string };

/**
 * Resolve the destination the environment's owner selected. The topic ARN is accepted only for
 * the topic arm: a value beside the email arm would be two destinations, so synth refuses it.
 */
export function staffedAlarmDestinationConfig(
  envName: string,
  topicArn: string | undefined = process.env.ADLYTICS_STAFFED_ALARM_TOPIC_ARN,
): StaffedAlarmDestination | undefined {
  const selected = (SELECTED_ARMS as Record<string, string>)[envName];
  const supplied = topicArn !== undefined && topicArn.trim() !== '';
  if (selected === undefined) {
    if (supplied) throw new Error(`No staffed alarm destination arm is selected for ${envName}.`);
    return undefined;
  }
  if (selected === 'ExistingInAccountTopicEmail') {
    if (supplied) {
      throw new Error('ADLYTICS_STAFFED_ALARM_TOPIC_ARN conflicts with the selected email arm.');
    }
    return { arm: 'ExistingInAccountTopicEmail' };
  }
  if (!supplied || !/^arn:aws[a-z-]*:sns:[a-z0-9-]+:[0-9]{12}:[A-Za-z0-9_-]{1,256}$/.test(topicArn)) {
    throw new Error('ADLYTICS_STAFFED_ALARM_TOPIC_ARN must name an SNS topic for the topic arm.');
  }
  return { arm: 'ExistingConfirmedSnsTopic', topicArn };
}

/**
 * Stream consumer retry bound: whichever limit a record reaches first ends its retries, and the
 * batch's stream metadata goes to the mapping's on-failure queue. A transient read or send
 * failure clears well inside an hour; a deterministic one never clears, so retrying it until
 * the 24 h stream retention only holds its shard.
 */
export interface AsyncFailureBoundsConfig {
  readonly streamMaxRetryAttempts: number;
  readonly streamMaxRecordAgeSeconds: number;
}

export const ASYNC_FAILURE_BOUNDS: AsyncFailureBoundsConfig = Object.freeze({
  streamMaxRetryAttempts: 100,
  streamMaxRecordAgeSeconds: 3600,
});

export interface OperationalStatusConfig {
  readonly provisioned: boolean;
  readonly stage1LifecycleReconcileBatchSize: 10;
  readonly stage1OutboxMaxConsecutiveFailedDeliveryAttempts: 3;
}

export const PROJECTION_HANDOFF_STATES = [
  'INLINE_ACTIVE',
  'INLINE_QUIESCED',
  'QUEUED_QUIESCED',
  'QUEUED_ACCUMULATE',
  'QUEUED_ACTIVE',
  'QUEUED_DRAIN',
] as const;

export type ProjectionHandoffState = (typeof PROJECTION_HANDOFF_STATES)[number];

/** Reviewed Stage-1 deployment and runtime limits from Load Path §11. */
export interface IngestionConfig {
  readonly projectionHandoffState: ProjectionHandoffState;
  /**
   * Load Path §2.5: the one typed boolean that renders the two reconciler schedules and the
   * Stage-1 lifecycle producer's stream mapping and recovery schedule. `false` only for the first
   * pre-switch-fence deployment before the retained passing `verify-pre-switch` report; every
   * `QUEUED_*` handoff state requires `true`.
   */
  readonly stage1RecoveryEnabled: boolean;
  /**
   * S6 legacy disablement (`design.md`, "Legacy cutover", step 6): the one typed boolean that
   * renders only the `PlanIntentReconcilerSchedule` rule and the reconciler role's coordinator
   * write actions. It stays `true` through the drain, so the reconciler finishes pre-CAS work;
   * a later separate commit sets it `false` for an environment only after its post-`GRAPH`
   * enumeration reads zero (P6(d')). Never set through a `-c` context override.
   */
  readonly legacyPlanIntentReconcileEnabled: boolean;
  readonly maxUnits: 15;
  readonly maxAcceptedAttemptsPerUnit: 10;
  readonly maxAcceptedAttemptsPerPlan: 32;
  readonly maxGroupMembers: 4;
  readonly maxPointersPerTable: 37;
  readonly maxObjectRefsPerUnit: 37;
  /** ADR-149 decision 4: one unit's rows, from probe P-4/P-5 (writer memory and time). */
  readonly maxRowsPerUnit: 50000;
  /** ADR-149 decision 4: one delivery's rows, from probe P-5 (delivery function memory). */
  readonly maxRowsPerDelivery: 20000;
  readonly athenaMaxStatementBytes: 200000;
  readonly maxEncodedIdentityBytes: 128;
  readonly maxPartitionIdentityBytes: 256;
  readonly tableWriterTimeoutSeconds: 900;
  readonly tableWriterMemoryMiB: 2048;
  readonly tableWriterCleanupReserveMs: 60000;
  readonly tableWriterConcurrency: 8;
  readonly tableLeaseDurationSeconds: 930;
  readonly semanticWriteVisibilityMarginSeconds: 60;
  readonly tableAttemptReconcileBatchSize: 10;
  readonly tableAttemptReconcileBaseBackoffSeconds: 60;
  readonly tableAttemptReconcileMaxBackoffSeconds: 900;
  readonly planIntentReconcileBatchSize: 50;
  readonly planIntentReconcileBaseBackoffSeconds: 60;
  readonly planIntentReconcileMaxBackoffSeconds: 900;
  readonly drainObservationIntervalSeconds: 300;
}

export type ProjectionHandoffControls = Readonly<{
  projectionComposition: 'inline' | 'queued';
  deliveryMappingEnabled: boolean;
  semanticWriterMappingEnabled: boolean;
}>;

const HANDOFF_CONTROLS: Readonly<Record<ProjectionHandoffState, ProjectionHandoffControls>> = {
  INLINE_ACTIVE: Object.freeze({
    projectionComposition: 'inline',
    deliveryMappingEnabled: true,
    semanticWriterMappingEnabled: false,
  }),
  INLINE_QUIESCED: Object.freeze({
    projectionComposition: 'inline',
    deliveryMappingEnabled: false,
    semanticWriterMappingEnabled: false,
  }),
  QUEUED_QUIESCED: Object.freeze({
    projectionComposition: 'queued',
    deliveryMappingEnabled: false,
    semanticWriterMappingEnabled: false,
  }),
  QUEUED_ACCUMULATE: Object.freeze({
    projectionComposition: 'queued',
    deliveryMappingEnabled: true,
    semanticWriterMappingEnabled: false,
  }),
  QUEUED_ACTIVE: Object.freeze({
    projectionComposition: 'queued',
    deliveryMappingEnabled: true,
    semanticWriterMappingEnabled: true,
  }),
  QUEUED_DRAIN: Object.freeze({
    projectionComposition: 'queued',
    deliveryMappingEnabled: false,
    semanticWriterMappingEnabled: true,
  }),
};

/** Resolve the sole public handoff tag; legacy independent controls are never accepted. */
export function resolveProjectionHandoffState(value: unknown): ProjectionHandoffControls {
  if (typeof value !== 'string' || !PROJECTION_HANDOFF_STATES.includes(value as ProjectionHandoffState)) {
    throw new Error(
      `ingestion.projectionHandoffState must be exactly one of ${PROJECTION_HANDOFF_STATES.join(', ')}.`,
    );
  }
  return HANDOFF_CONTROLS[value as ProjectionHandoffState];
}

export function validateIngestionConfig(config: IngestionConfig): void {
  const expected: Readonly<
    Record<
      Exclude<
        keyof IngestionConfig,
        'projectionHandoffState' | 'stage1RecoveryEnabled' | 'legacyPlanIntentReconcileEnabled'
      >,
      number
    >
  > = {
    maxUnits: 15,
    maxAcceptedAttemptsPerUnit: 10,
    maxAcceptedAttemptsPerPlan: 32,
    maxGroupMembers: 4,
    maxPointersPerTable: 37,
    maxObjectRefsPerUnit: 37,
    // ADR-149 (2026-10-22): the row ceilings design §4.6 derives from probe P-4/P-5; they
    // replace the per-unit statement cap's SQL-size refusal. Write-discipline §11 carries them.
    maxRowsPerUnit: 50000,
    maxRowsPerDelivery: 20000,
    athenaMaxStatementBytes: 200000,
    maxEncodedIdentityBytes: 128,
    maxPartitionIdentityBytes: 256,
    tableWriterTimeoutSeconds: 900,
    tableWriterMemoryMiB: 2048,
    tableWriterCleanupReserveMs: 60000,
    // Raised 2 → 8 on 2026-09-21 with the wide-window wait-time evidence the §14 risk
    // requires: 22 distinct physical tables advanced at most 2 at a time while same-table
    // fencing (not this value) owned correctness. Write-discipline §11 carries the same row.
    tableWriterConcurrency: 8,
    tableLeaseDurationSeconds: 930,
    semanticWriteVisibilityMarginSeconds: 60,
    tableAttemptReconcileBatchSize: 10,
    tableAttemptReconcileBaseBackoffSeconds: 60,
    tableAttemptReconcileMaxBackoffSeconds: 900,
    // Raised 10 → 50 on 2026-10-10 so the one-minute intent reconciler feeds the faster
    // table writer rather than capping it. Write-discipline §11 carries the amendment.
    planIntentReconcileBatchSize: 50,
    planIntentReconcileBaseBackoffSeconds: 60,
    planIntentReconcileMaxBackoffSeconds: 900,
    drainObservationIntervalSeconds: 300,
  };
  const controls = resolveProjectionHandoffState(config?.projectionHandoffState);
  const recovery = (config as unknown as Record<string, unknown>)?.stage1RecoveryEnabled;
  if (typeof recovery !== 'boolean') {
    throw new Error('ingestion.stage1RecoveryEnabled must be the typed boolean enablement control.');
  }
  const legacyReconcile = (config as unknown as Record<string, unknown>)
    ?.legacyPlanIntentReconcileEnabled;
  if (typeof legacyReconcile !== 'boolean') {
    throw new Error(
      'ingestion.legacyPlanIntentReconcileEnabled must be the typed boolean disablement control.',
    );
  }
  if (controls.projectionComposition === 'queued' && !recovery) {
    throw new Error(
      'ingestion.stage1RecoveryEnabled must be true for every QUEUED_* handoff state; false is '
      + 'permitted only for the first pre-switch-fence INLINE deployment.',
    );
  }
  for (const [key, reviewed] of Object.entries(expected)) {
    const value = (config as unknown as Record<string, unknown>)?.[key];
    if (typeof value !== 'number' || !Number.isInteger(value) || value !== reviewed) {
      throw new Error(`ingestion.${key} must be the reviewed positive integer ${reviewed}.`);
    }
  }
  const supplied = config as unknown as Record<string, unknown>;
  for (const forbidden of ['projectionMode', 'deliveryMappingEnabled', 'semanticWriteMappingEnabled']) {
    if (forbidden in supplied) throw new Error(`Legacy independent ingestion.${forbidden} is forbidden.`);
  }
  if (config.tableWriterCleanupReserveMs >= config.tableWriterTimeoutSeconds * 1000) {
    throw new Error('Table-writer cleanup reserve must be below its timeout.');
  }
  if (config.tableLeaseDurationSeconds <= config.tableWriterTimeoutSeconds) {
    throw new Error('Table lease duration must exceed the table-writer timeout.');
  }
  const visibility = config.tableWriterTimeoutSeconds + config.semanticWriteVisibilityMarginSeconds;
  if (visibility !== 960 || visibility > 43200) {
    throw new Error('Semantic-write visibility must derive to 960 seconds within the SQS ceiling.');
  }
}

const DEFAULT_INGESTION_CONFIG: IngestionConfig = Object.freeze({
  // Edge 4 of the guarded INLINE → QUEUED switch (write-discipline §2.5): the semantic writer
  // mapping enables and accumulated per-table units drain in parallel under the lease fence.
  // Edges 1–3 passed their gates with STATE readbacks (2026-09-22, revisions 1→3); the
  // QUEUED_ACTIVE verify-edge gate (statement-ledger re-proof) passed before this flip.
  projectionHandoffState: 'QUEUED_ACTIVE',
  // The pre-switch fence deployed with this false; the retained passing `verify-pre-switch`
  // report (2026-09-21, dev) authorized the flip. It stays true in every later handoff state.
  stage1RecoveryEnabled: true,
  legacyPlanIntentReconcileEnabled: true,
  maxUnits: 15,
  maxAcceptedAttemptsPerUnit: 10,
  maxAcceptedAttemptsPerPlan: 32,
  maxGroupMembers: 4,
  maxPointersPerTable: 37,
  maxObjectRefsPerUnit: 37,
  maxRowsPerUnit: 50000,
  maxRowsPerDelivery: 20000,
  athenaMaxStatementBytes: 200000,
  maxEncodedIdentityBytes: 128,
  maxPartitionIdentityBytes: 256,
  tableWriterTimeoutSeconds: 900,
  tableWriterMemoryMiB: 2048,
  tableWriterCleanupReserveMs: 60000,
  tableWriterConcurrency: 8,
  tableLeaseDurationSeconds: 930,
  semanticWriteVisibilityMarginSeconds: 60,
  tableAttemptReconcileBatchSize: 10,
  tableAttemptReconcileBaseBackoffSeconds: 60,
  tableAttemptReconcileMaxBackoffSeconds: 900,
  planIntentReconcileBatchSize: 50,
  planIntentReconcileBaseBackoffSeconds: 60,
  planIntentReconcileMaxBackoffSeconds: 900,
  drainObservationIntervalSeconds: 300,
});

/**
 * S6 legacy disablement per environment (`IngestionConfig.legacyPlanIntentReconcileEnabled`).
 * Dev ships `true` with the S6 build; its P6(d') commit changes the dev value and nothing else.
 */
const LEGACY_PLAN_INTENT_RECONCILE: Readonly<Record<string, boolean>> = Object.freeze({
  dev: true,
});

export function legacyPlanIntentReconcileEnabled(envName: string): boolean {
  return LEGACY_PLAN_INTENT_RECONCILE[envName] ?? true;
}

export type LandingConfig =
  | {
      /** Producer onboarding is absent unless an operator explicitly enables it. */
      readonly enabled: false;
    }
  | {
      /** Producer onboarding and its cross-account trust are explicitly enabled. */
      readonly enabled: true;

      /** Exact cross-account IAM role that may assume the destination-owned landing role. */
      readonly pipelineRoleArn: string;

      /** Deployment-specific confused-deputy value required by the landing role trust policy. */
      readonly externalId: string;
    };

export type EnabledLandingConfig = Extract<LandingConfig, { readonly enabled: true }>;

export interface AppConfig {
  /** Validated by `assertValidEnvName`. Any name that passes is legal. */
  readonly envName: string;
  readonly account: string;
  readonly region: string;

  /** ADR-007 and the CloudFront single-origin design. */
  readonly edge: EdgeConfig;

  /** ADR-018: encryption at rest, and raw-zone retention. */
  readonly data: DataConfig;

  /** ADR-050: false-by-default optional producer onboarding and exact external trust. */
  readonly landing: LandingConfig;

  /** ADR-005: every Lambda is arm64. Held here so no stack can diverge. */
  readonly lambdaArchitecture: 'arm64';
  readonly pythonRuntime: 'python3.14';

  /** ADR-007: Cognito managed login and the cookie session it feeds. */
  readonly auth: AuthConfig;

  /** ADR-006 and Q-02: the managed agent loop, and the budget one turn may spend. */
  readonly agent: AgentConfig;

  /** ADR-077 §3: the token service's lifetime and public-route limits. */
  readonly tokenService: TokenServiceConfig;

  /** ADR-076 Phase 1: the insight narration models, overridable independently of the agent's. */
  readonly insight: InsightModelConfig;

  /** ADR-041: layer 2, a Cedar policy engine evaluating the tool gateway. */
  readonly gatewayGuardrails: GatewayGuardrailConfig;

  /** Optional automatic AgentCore evaluation; disabled and unprovisioned as shipped. */
  readonly onlineEvaluation: OnlineEvaluationConfig;
  readonly evaluationQuality: EvaluationQualityConfig;

  /** Grounded multidimensional V4 execution request; provisioning follows `onlineEvaluation`. */
  readonly onlineEvaluationV4: OnlineEvaluationV4Config;

  /** Optional customer-owned post-release evaluation source. */
  readonly evaluationInput: EvaluationInputConfig;

  /** ADR-075 Stage-1 projection limits and sole deployment handoff input. */
  readonly ingestion: IngestionConfig;

  /** Development-only authority-backed operational status observation slice. */
  readonly operationalStatus: OperationalStatusConfig;

  /**
   * Traceability FR-10: the staffed alarm destination, exactly one arm, or undefined where no
   * environment owner selected one (`contracts/operations/alarm-destinations.yaml`).
   */
  readonly staffedAlarmDestination: StaffedAlarmDestination | undefined;
  /** The retry bound every stream event source mapping sets (`AsyncFailureBoundsAspect`). */
  readonly asyncFailureBounds: AsyncFailureBoundsConfig;

  /** Athena requires an explicit scan cutoff — architecture.md section 8. */
  readonly athenaBytesScannedCutoff: number;

  /**
   * How stale a reused Athena result may be, in minutes. Zero disables reuse.
   *
   * A per-query setting rather than a workgroup one, because `AWS::Athena::WorkGroup` has
   * no result-reuse property — so it is passed to the runtime instead of configured on the
   * workgroup. Athena's own ceiling is seven days; a reused answer is presented as
   * current, which makes this a correctness window rather than a cost dial (F-14).
   */
  readonly athenaResultReuseMaxAgeMinutes: number;

  /**
   * Maximum independent interactive Athena reads one CHAT request may execute at once. Also the
   * value passed to ingestion; no batch budget reads it (the start budget below owns those caps).
   * Required per deployment, with no default, from `ADLYTICS_ATHENA_QUERY_BATCH_MAX_CONCURRENCY`
   * (D7): a whole number from 1 to the contract's CHAT reserve.
   */
  readonly athenaQueryBatchMaxConcurrency: number;

  /**
   * The static Athena start budget (plan S1, `athena-start-budget.ts`). Only Baker and insight
   * precompute have an in-process start cap a value can change; every other batch caller is
   * sequential by construction. The two limits must equal the contract `athena_start_budget`
   * block, the one home of both: synth refuses a config that differs, so neither can loosen it.
   */
  readonly athenaStartBudget: {
    readonly bakerStartCap: number;
    readonly insightStartCap: number;
    readonly batchAthenaBudget: number;
    readonly chatReserveActiveDml: number;
  };

  /**
   * Whether to turn on CloudWatch Transaction Search for the whole account.
   *
   * **Account-wide, and it changes span-ingestion pricing for every workload in the
   * account — including ones this repository did not deploy.** That is why it ships
   * `false` and why it is a stated choice rather than something a deploy assumes. A
   * customer disabling it keeps metrics and correlated logs and loses distributed
   * traces; the instrumentation is unconditional, so re-enabling is this flag and a
   * redeploy rather than a code change.
   *
   * It gates AgentCore's spans specifically: without Transaction Search, AgentCore
   * cannot deliver spans to the per-agent log group at all [verified 2026-08-19].
   * ADR-009 records that a span destination cannot be migrated once an agent exists,
   * which is why `ObservabilityStack` deploys before `AgentStack`.
   */
  readonly transactionSearchEnabled: boolean;

  /**
   * What percentage of spans to index as trace summaries, 0 to 100.
   *
   * A second, independent cost dial and the one to reach for first. Ingestion captures
   * spans; *indexing* is what makes them searchable as trace summaries, and AWS's own
   * guidance is to ingest broadly and index a small percentage. So a customer who finds
   * tracing expensive should lower this before turning ingestion off, because it
   * preserves the spans themselves.
   *
   * Ignored entirely when `transactionSearchEnabled` is false, since there is no
   * configuration to attach it to.
   */
  readonly transactionSearchIndexingPercentage: number;

  /**
   * Fraction of traces to record, 0 to 1. Passed to the SDK as `ADL_TRACE_SAMPLE_RATIO`.
   *
   * **A different dial from `transactionSearchIndexingPercentage`, and the two are easy to
   * conflate.** This one decides whether a span is *created and exported* at all; that one decides
   * whether an exported span is *searchable as a trace summary*. Lowering this loses evidence;
   * lowering that loses only the index over evidence you still hold.
   *
   * Ships at 1 — every trace recorded — because Transaction Search exists to capture 100% of spans
   * so traces are never broken, and a broken trace is worse than an absent one when investigating:
   * the missing hop reads as a fast hop. AWS notes collector-less export defaults to recording
   * everything and that this can be many times the ingestion of a sampled setup, so a deployment
   * with high volume lowers this deliberately — after lowering the indexing rate first.
   */
  readonly traceSampleRatio: number;

  /**
   * Days every record of a turn is kept after its receipt's creation (ADR-081 §5).
   *
   * Required per deployment with no default, from `ADLYTICS_RECEIPT_RETENTION_DAYS`: how long a
   * principal-bearing receipt is kept is a decision for whoever operates the deployment, not a
   * value this repository can choose for them. Absent, it is `NaN`, the typed `UNSET` for a
   * number, and synth refuses. Each record writer receives it as `ADL_RECEIPT_RETENTION_DAYS`
   * and stamps `expires_at` from it, and each record table expires items on that attribute.
   */
  readonly receiptRetentionDays: number;

  /**
   * Days added to `receiptRetentionDays` for DynamoDB's TTL deletion lag when sizing the reviewer
   * access log (ADR-081 §4, observability §3.8). DynamoDB deletes an expired item "typically within
   * a few days" and states no bound, so this is a margin, not a guarantee.
   */
  readonly receiptTtlLagMarginDays: number;

  /**
   * Typed as the CDK enum rather than a bare number, so an arbitrary day count
   * that CloudWatch does not accept is a build error instead of a deploy failure.
   */
  readonly logRetentionDays: logs.RetentionDays;

  /**
   * Retention of the stack-owned online-evaluation result groups (ADR-079, observability §3.7).
   * Typed as the CDK enum for the same reason as `logRetentionDays`; each gate asserts the group
   * reads back this value before it enables evaluation.
   */
  readonly evaluationResultRetentionDays: logs.RetentionDays;
  /**
   * Retention of the log group AgentCore creates for the managed Harness runtime, which holds the
   * system prompt, each conversation, and raw tool results (observability §3.4). The Harness
   * custom resource applies it before it reports the Harness ready (infra-composition-stacks).
   */
  readonly harnessRuntimeLogRetentionDays: logs.RetentionDays;
  /**
   * Seconds after a turn's dispatch decision past which the evaluation review reports a
   * dispatched turn with no verdict as `overdue` rather than still pending (ADR-079). The
   * evaluation review route receives it as `ADL_EVALUATION_RESULT_DEADLINE_SECONDS`.
   */
  readonly evaluationResultDeadlineSeconds: number;
}

/**
 * The configuration this repository ships, for any environment name.
 *
 * Every value that differs per deployment is `UNSET`, and `getConfig` refuses to
 * return a configuration that still contains one. Nothing here may ever be
 * replaced with a working local value: a committed account number deploys a
 * customer's stack into this project's account, and a committed login prefix is
 * globally unique, so their first deploy collides with our live one over a name
 * they did not choose and cannot see.
 *
 * The non-`UNSET` values are the conservative end of every choice rather than a
 * "dev" or "prod" profile, because there is no way to know which a given name is:
 * the smaller Athena cutoff, span destination off (ADR-009 makes it unmigratable
 * once created), the shorter log retention, and no API origin at the edge. A
 * customer raises them deliberately; none of them can cause a first deploy to fail
 * or to cost more than expected.
 */
/**
 * Read a boolean from the environment, using a caller-owned safe fallback.
 *
 * Most policy/cost flags default off. A defaulted identifier would still be unsafe; this helper is
 * only for booleans whose safe state is explicitly known by the caller.
 *
 * `yes`, `1` and `on` are refused as deliberately as `banana`. An operator who writes one has a
 * model of this function that is wrong, and accepting it teaches them the wrong thing about
 * every other flag. The rejected value is left out of the message: it came from the environment
 * and an error string reaches logs.
 */
function envFlag(key: string, fallback = false): boolean {
  const raw = process.env[key];
  if (raw === undefined || raw === '') return fallback;
  if (raw === 'true') return true;
  if (raw === 'false') return false;
  throw new Error(`${key} must be exactly "true" or "false" when set.`);
}

/**
 * Read a whole percentage from the environment, or fail rather than clamp.
 *
 * Clamping an out-of-range value would deploy a number the operator did not ask for and report
 * success, which for a cost dial is the wrong direction to fail in.
 */
/**
 * Read a 0..1 fraction from the environment, or fail rather than clamp.
 *
 * Separate from `envPercentage` on purpose: the two units are one of the easiest things to mix up
 * here, and `0.05` read as a percentage — or `5` read as a ratio — both silently deploy a rate
 * nobody chose. Refusing an out-of-range value is what makes the unit mistake visible.
 */
function envRatio(key: string, fallback: number): number {
  const raw = process.env[key];
  if (raw === undefined || raw === '') return fallback;
  const parsed = Number(raw);
  if (!Number.isFinite(parsed) || parsed < 0 || parsed > 1) {
    throw new Error(`${key} must be a fraction between 0 and 1 when set, not a percentage.`);
  }
  return parsed;
}

function envPercentage(key: string, fallback: number): number {
  const raw = process.env[key];
  if (raw === undefined || raw === '') return fallback;
  const parsed = Number(raw);
  if (!Number.isInteger(parsed) || parsed < 0 || parsed > 100) {
    throw new Error(`${key} must be a whole number between 0 and 100 when set.`);
  }
  return parsed;
}

/**
 * Read a required positive whole number of days from the environment, with no fallback.
 *
 * Absent or empty returns `NaN`, which `getConfig` reports as unset alongside every other gap,
 * the way `UNSET` works for a string. A value that is present but not a positive whole number
 * fails immediately rather than being clamped; the rejected value is left out of the message
 * because it came from the environment and an error string reaches logs.
 */
function envRequiredDays(key: string): number {
  const raw = process.env[key];
  if (raw === undefined || raw.trim() === '') return Number.NaN;
  if (!/^[1-9][0-9]{0,5}$/.test(raw)) {
    throw new Error(`${key} must be a positive whole number of days when set.`);
  }
  return Number(raw);
}

/**
 * Read a required AWS WAF rate-rule limit from the environment, with no fallback.
 *
 * Absent or empty returns `NaN`, reported as unset like `envRequiredDays`. Present, it must be a
 * whole number AWS WAF accepts for a rate-based rule, 10 to 2,000,000,000; the rejected value is
 * left out of the message for the same reason.
 */
function envRequiredRateLimit(key: string): number {
  const raw = process.env[key];
  if (raw === undefined || raw.trim() === '') return Number.NaN;
  const parsed = /^[1-9][0-9]{1,9}$/.test(raw) ? Number(raw) : Number.NaN;
  if (!(parsed >= 10 && parsed <= 2_000_000_000)) {
    throw new Error(`${key} must be a whole number of requests from 10 to 2000000000 when set.`);
  }
  return parsed;
}
/**
 * Read a required base-10 whole number in `[min, max]` from the environment, with no fallback.
 *
 * Absent or empty returns `NaN`, reported as unset like `envRequiredDays`. Present, it must be a
 * base-10 whole number without a sign, fraction, exponent or leading zero, inside the range; the
 * rejected value is left out of the message for the same reason.
 */
function envRequiredWholeNumberInRange(key: string, min: number, max: number): number {
  const raw = process.env[key];
  if (raw === undefined || raw.trim() === '') return Number.NaN;
  const parsed = /^[1-9][0-9]{0,9}$/.test(raw) ? Number(raw) : Number.NaN;
  if (!(parsed >= min && parsed <= max)) {
    throw new Error(`${key} must be a whole number from ${min} to ${max} when set.`);
  }
  return parsed;
}

/** An optional Secrets Manager version id, omitted entirely when absent. */
function optionalVersionId(key: string): { originVerifySecretVersionId?: string } {
  const raw = process.env[key];
  if (raw === undefined || raw.trim() === '') return {};
  // Secrets Manager version ids are UUIDs; anything else would fail at deploy as an opaque
  // dynamic-reference error rather than here.
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(raw)) {
    throw new Error(`${key} must be a Secrets Manager version id when set.`);
  }
  return { originVerifySecretVersionId: raw };
}
function onlineEvaluationConfig(envName: string): OnlineEvaluationConfig {
  const provisioned = envFlag('ADLYTICS_ONLINE_EVALUATION_PROVISIONED');
  if (provisioned && envName !== 'dev') {
    throw new Error('Online evaluation may be provisioned only for ENV=dev.');
  }
  return {
    provisioned,
    samplingPercentage: envPercentage('ADLYTICS_ONLINE_EVALUATION_SAMPLE_PERCENT', 10),
    sessionTimeoutMinutes: 1,
  };
}

function onlineEvaluationV4Config(envName: string): OnlineEvaluationV4Config {
  const executionRequested = envFlag('ADLYTICS_ONLINE_EVALUATION_V4_REQUESTED');
  if (executionRequested && envName !== 'dev') {
    throw new Error('Online evaluation V4 execution may be requested only for ENV=dev.');
  }
  return { executionRequested };
}

function operationalStatusConfig(envName: string): OperationalStatusConfig {
  const provisioned = envFlag('ADLYTICS_OPERATIONAL_STATUS_PROVISIONED');
  if (provisioned && envName !== 'dev') {
    throw new Error('Operational status may be provisioned only for ENV=dev.');
  }
  return {
    provisioned,
    stage1LifecycleReconcileBatchSize: 10,
    stage1OutboxMaxConsecutiveFailedDeliveryAttempts: 3,
  };
}

export function defaultConfig(envName: string): AppConfig {
  assertValidEnvName(envName);

  return {
    envName,

    // The CDK env vars are the account and region of the profile the CLI was given,
    // so `--profile` remains the single statement of where a deploy lands. Absent,
    // this is UNSET and fails at synth rather than defaulting to somewhere.
    account: process.env.CDK_DEFAULT_ACCOUNT ?? UNSET,
    region: process.env.CDK_DEFAULT_REGION ?? UNSET,

    edge: {
      // False is the only value that can ship. True makes FrontendStack read
      // /adlytics/<env>/api/id, which does not exist on a first deploy, and a
      // dynamic reference to a missing parameter fails as an opaque CloudFormation
      // parameter error rather than as a missing prerequisite.
      //
      // **From the environment rather than edited in place, and that fixes a real defect.** These
      // are per-pass rather than per-deployment, so the second pass used to mean editing this file,
      // deploying, and reverting. Whoever forgot the revert committed `true`; whoever remembered it
      // left the committed value disagreeing with the live distribution — which is what happened,
      // and the drift is silent because a redeploy from a clean checkout *detaches* a working
      // origin. `envFlag` defaults to false, so a checkout still ships conservative and the second
      // pass is now a variable on the deploy command instead of a diff nobody meant to keep.
      apiOriginAttached: envFlag('ADLYTICS_API_ORIGIN_ATTACHED'),
      // Its own flag and its own pass: this gates a parameter that does not exist until the
      // conversation function has been created, which is later than the HTTP API existing.
      conversationOriginAttached: envFlag('ADLYTICS_CONVERSATION_ORIGIN_ATTACHED'),
      wafRateLimitPerFiveMinutes: envRequiredRateLimit('ADLYTICS_EDGE_WAF_RATE_LIMIT_PER_FIVE_MINUTES'),
      ...optionalVersionId('ADLYTICS_ORIGIN_VERIFY_SECRET_VERSION_ID'),
      // Five minutes bounds how long a dropped previous value is still accepted after rotation
      // step 4; one minute bounds the Secrets Manager reads a flood of wrong values can cause.
      originVerifyMaxCacheAgeSeconds: 300,
      originVerifyRereadIntervalSeconds: 60,
    },

    data: {
      // SSE-S3, per ADR-018. Shippable because it is complete and costs nothing;
      // kms_cmk is the deliberate later step and refuses to synth today.
      encryption: 's3_managed',
      rawTransitionAfterDays: 30,
      // Keeping the audit trail is the conservative direction. A customer choosing
      // to delete source objects should do so knowingly.
      rawExpireAfterDays: 'never',
      rawExpireNoncurrentVersionsAfterDays: 90,
      abortIncompleteUploadsAfterDays: 7,
      athenaResultsExpireAfterDays: 30,
    },

    landing: envFlag('ADLYTICS_PRODUCER_ONBOARDING_ENABLED')
      ? {
          enabled: true,
          pipelineRoleArn: process.env.ADLYTICS_LANDING_PIPELINE_ROLE_ARN ?? UNSET,
          externalId: process.env.ADLYTICS_LANDING_EXTERNAL_ID ?? UNSET,
        }
      : { enabled: false },

    lambdaArchitecture: 'arm64',
    pythonRuntime: 'python3.14',

    auth: {
      // Globally unique across every AWS account, like an S3 bucket name, and
      // deliberately not derived from envName for that reason: a value that must
      // differ per deployment cannot be computed from something that does not.
      // It is user-visible during login and moving it later relocates the OAuth
      // endpoints, so it is worth choosing once.
      // From the environment, for the same reason as `account` and `region` above: it is
      // specific to one deployment and must never be committed. A Cognito domain prefix is
      // globally unique across all AWS accounts, so a value shipped in this file would make
      // every customer's first deploy collide with ours over a name they did not choose and
      // cannot see. Absent, this stays UNSET and synth refuses — a mistyped variable name
      // fails loudly rather than resolving to something.
      loginDomainPrefix: process.env.ADLYTICS_LOGIN_DOMAIN_PREFIX ?? UNSET,
      refreshCookieMaxAgeSeconds: 30 * 24 * 60 * 60,
      refreshTokenValidityDays: 30,
      qualityReviewValidationPasswordAuthEnabled: envFlag(
        'ADLYTICS_QUALITY_REVIEW_VALIDATION_PASSWORD_AUTH',
      ),
      wafRateLimitPerFiveMinutes: envRequiredRateLimit(
        'ADLYTICS_USER_POOL_WAF_RATE_LIMIT_PER_FIVE_MINUTES',
      ),
      cookieScope: 'host_only',
      cookieDomain: '',
    },

    agent: {
      // Model ids come from the environment, never committed. See AgentModeConfig.modelId:
      // Bedrock model access is per-account, so a value shipped here names a model the customer
      // may not have. Which model belongs to which mode is a decision, and it is recorded in
      // docs/capabilities/conversational-runtime/low-level-design.md §8 rather than as a string here.
      //
      // The budgets are the conservative end, like every other shipped value. A customer raises
      // them deliberately; none of them can make a first deploy cost more than expected.
      quickChat: {
        modelId: process.env.ADLYTICS_QUICK_CHAT_MODEL_ID ?? UNSET,
        maxIterations: 8,
        maxTokens: 4096,
        timeoutSeconds: 120,
      },
      // Higher across the board because a deep dive is multi-step by definition, and still well
      // inside AgentCore's synchronous ceiling — a Step Functions caller is capped at 15 minutes
      // regardless, so a Runtime timeout past that only burns cost after the caller has gone.
      //
      // Tightened 2026-08-23 from 24 iterations and 600s, once a real turn could be driven and
      // G-04 stopped being theoretical. These are ceilings for a runaway turn, and a ceiling
      // twentyfold above the measurement is not a ceiling. Measured: a deep dive completes in
      // 22-26s over two tool calls, and the untuned version was 38-47s over six — so 12 iterations
      // still admits that worse shape and 180s is roughly fourfold its slowest observation.
      // The conversation function's own timeout derives from this figure in api-stack, so the
      // request a user waits on shortens with it rather than needing a second edit.
      deepDive: {
        modelId: process.env.ADLYTICS_DEEP_DIVE_MODEL_ID ?? UNSET,
        maxIterations: 12,
        maxTokens: 16384,
        timeoutSeconds: 180,
      },
      // 90 days, which is also the service default — stated rather than inherited because it is
      // unfixable after the fact, and an inherited retention is not a decided one. Long enough
      // that a conversation resumed after a quarter still has its history, short enough that a
      // customer is not holding a year of conversational content they never chose to keep.
      memoryExpirationDays: 90,
      memoryModelId: process.env.ADLYTICS_MEMORY_MODEL_ID ?? UNSET,
    },

    tokenService: {
      // Added to the turn lifetime, so a Harness token minted at the start of the longest turn
      // is still valid at the Harness authorizer despite skew between the two clocks.
      clockSkewSeconds: 30,
      // The Harness authorizer fetches these documents and caches them, so real traffic is a
      // handful of requests; the ceiling bounds a flood rather than serving load.
      publicRouteThrottle: {
        discovery: { rateLimit: 10, burstLimit: 20 },
        jwks: { rateLimit: 10, burstLimit: 20 },
        // One exchange per Gateway token AgentCore Identity requests on a turn's behalf, so this
        // is sized above the key routes; it still bounds a flood rather than serving load.
        token: { rateLimit: 20, burstLimit: 40 },
      },
      // Per source IP per five minutes. Far above any authorizer's fetch rate, and the managed
      // IP-reputation rule group runs alongside it.
      wafRateLimitPerFiveMinutes: 1000,
    },

    insight: {
      // Fall back to the agent values when no override is exported — the pre-ADR-076
      // wiring exactly, so a deployment that has not decided on the insight family yet
      // changes nothing. When neither is set, both the insight and agent paths are
      // reported unset at synth. An override exported as the empty string does not fall
      // back (`??` is nullish-only); it reads as unset and synth refuses — fail closed.
      quickModelId:
        process.env.ADLYTICS_INSIGHT_QUICK_MODEL_ID ??
        process.env.ADLYTICS_QUICK_CHAT_MODEL_ID ??
        UNSET,
      deepModelId:
        process.env.ADLYTICS_INSIGHT_DEEP_MODEL_ID ??
        process.env.ADLYTICS_DEEP_DIVE_MODEL_ID ??
        UNSET,
    },

    gatewayGuardrails: {
      enabled: envFlag('ADLYTICS_GATEWAY_GUARDRAILS'),
      // `LOG_ONLY`, and it is a literal rather than an env var on purpose. Promoting to `ENFORCE`
      // is a decision that needs the base permit policy verified and thresholds calibrated
      // against real traffic first, so it should cost a commit that can be reviewed rather than a
      // variable somebody exports once and forgets. ADR-041.
      enforcement: 'LOG_ONLY',
    },

    onlineEvaluation: onlineEvaluationConfig(envName),
    // One worst label in five verdicts over six hours is a trend worth a look, not a gate
    // (agent-evaluation-validity steering: a soft score never gates a release).
    evaluationQuality: {
      worstLabel: { periodHours: 6, minimumVerdicts: 5, shareThreshold: 0.2 },
      noDispatch: { periodMinutes: 30, periods: 3 },
    },
    onlineEvaluationV4: onlineEvaluationV4Config(envName),

    evaluationInput: {
      provisioned: envFlag('ADLYTICS_EVALUATION_INPUT_PROVISIONED'),
    },

    ingestion: Object.freeze({
      ...DEFAULT_INGESTION_CONFIG,
      legacyPlanIntentReconcileEnabled: legacyPlanIntentReconcileEnabled(envName),
    }),

    operationalStatus: operationalStatusConfig(envName),
    staffedAlarmDestination: staffedAlarmDestinationConfig(envName),
    // The same for every environment: the bound is a failure-class property, not a size.
    asyncFailureBounds: ASYNC_FAILURE_BOUNDS,

    athenaBytesScannedCutoff: 10 * 1024 * 1024 * 1024,
    athenaResultReuseMaxAgeMinutes: 60,
    // Required per deployment with no fallback (D7). Eight keeps the 10–16-query campaign detail
    // cohort to at most two waves while remaining far below the dev account's 200 active-DML-query
    // quota, verified 2026-09-15; the bound is the CHAT reserve. The static start budget below and
    // insight precompute's 2 x 4 never read this value.
    athenaQueryBatchMaxConcurrency: envRequiredWholeNumberInRange(
      'ADLYTICS_ATHENA_QUERY_BATCH_MAX_CONCURRENCY',
      1,
      ATHENA_START_BUDGET.chat_reserve_active_dml,
    ),
    // Q = 10, the cap every bake budget is certified at; insight 2 x 4. The limits are the contract's.
    athenaStartBudget: {
      bakerStartCap: BAKE_TASKS.certified_query_cap,
      insightStartCap: 4,
      batchAthenaBudget: ATHENA_START_BUDGET.batch_athena_budget,
      chatReserveActiveDml: ATHENA_START_BUDGET.chat_reserve_active_dml,
    },
    transactionSearchEnabled: envFlag('ADLYTICS_TRANSACTION_SEARCH'),
    // Low on purpose. Indexing is the expensive half and the guidance is to ingest broadly
    // and index sparsely, so the shipped value is one a customer raises deliberately rather
    // than one that surprises them on their first bill. Inert while ingestion is off.
    transactionSearchIndexingPercentage: envPercentage('ADLYTICS_SPAN_INDEXING_PERCENT', 5),
    // One, not a fraction. See `traceSampleRatio`: the cheap dial is indexing, and a partial trace
    // is worse than no trace when the missing hop is the slow one.
    traceSampleRatio: envRatio('ADLYTICS_TRACE_SAMPLE_RATIO', 1),
    // No default: a committed value would decide for every deployment how long its
    // principal-bearing receipts are kept (ADR-081 §5). Absent, synth refuses.
    receiptRetentionDays: envRequiredDays('ADLYTICS_RECEIPT_RETENTION_DAYS'),
    // A week over DynamoDB's documented "within a few days"; the access log then outlives every
    // receipt it attributes access to.
    receiptTtlLagMarginDays: 7,
    logRetentionDays: logs.RetentionDays.TWO_WEEKS,
    // The same conservative end as every other group; a deployment that must keep verdict
    // evidence longer raises it deliberately.
    evaluationResultRetentionDays: logs.RetentionDays.TWO_WEEKS,
    // Ninety days, the owner's choice on 2026-10-01: long enough to diagnose a reported turn a
    // quarter later, and equal to the conversation memory it mirrors (`memoryExpirationDays`).
    harnessRuntimeLogRetentionDays: logs.RetentionDays.THREE_MONTHS,
    // Thirty minutes. A deployment whose verdicts routinely arrive later raises it deliberately.
    evaluationResultDeadlineSeconds: 1800,
  };
}

/* -------------------------------------------------------------------------
 * Cross-stack parameter contract (ADR-012)
 *
 * Every value one stack needs from another travels through SSM, never a
 * CloudFormation export. Exports cannot be updated while imported — the
 * "deadly embrace" — and cannot cross regions, which CloudFront's us-east-1
 * certificate requirement makes mandatory.
 *
 * This registry is the only way to name a parameter. A typo becomes a compile
 * error rather than a deploy-time miss that reads as absent config.
 * ---------------------------------------------------------------------- */

/**
 * The prefix every API route carries.
 *
 * Must agree with API_PREFIX in the Python routing table. CloudFront prepends an
 * origin path rather than stripping one, so the prefix stays in the route itself
 * and there is no rewrite at the edge.
 *
 * Both the cache behaviour and the HTTP API routes derive from this. They were
 * once stated independently, and the routes were declared without the prefix
 * while the dispatcher expected it — a 404 from API Gateway before any function
 * ran, invisible to every test because each side was self-consistent.
 */
export const API_PREFIX = '/api';
/**
 * ADR-078 §1 path-scoped body-size exclusion (2026-10-05): the create-version body ceiling, in bytes.
 * The prompt-admin handler's `MAX_BODY_BYTES` and the admin page's `PROMPT_BODY_MAX_BYTES` equal it
 * (asserted by `infra/test/edge-security.test.ts`).
 */
export const PROMPT_CREATE_VERSION_MAX_BODY_BYTES = 32_768;
/** The managed `SizeRestrictions_BODY` limit the custom edge rule re-imposes off the exempt route. */
export const EDGE_DEFAULT_MAX_BODY_BYTES = 8_192;

/**
 * The token-service REST API stage (ADR-077 §3). Part of the issuer URL, so renaming it changes
 * every minted token's `iss` and the discovery URL every authorizer names.
 */
export const TOKEN_SERVICE_STAGE = 'v1';

/** The CloudFront cache-behaviour pattern that selects the API origin. */
export const API_PATH_PATTERN = `${API_PREFIX}/*`;

/**
 * The path the conversation route answers, and the behaviour pattern that selects it.
 *
 * Under `API_PREFIX` so the frontend calls one origin with one path convention, but matched by its
 * own behaviour because it goes to a different origin entirely.
 *
 * **This pattern must be listed before `API_PATH_PATTERN`.** CloudFront takes the first behaviour
 * whose pattern matches, not the most specific one, so `/api/*` listed first would swallow every
 * chat request and send it to API Gateway — where it would hit the fixed 29s integration timeout
 * that ADR-038 chose a Function URL to escape. `FrontendStack` orders them, and
 * `infra/test/conversation-route.test.ts` asserts the order rather than trusting it.
 */
export const CONVERSATION_PATH = `${API_PREFIX}/chat`;
export const CONVERSATION_PATH_PATTERN = `${CONVERSATION_PATH}*`;

/** Presentation-owned validated block stream; must precede the generic API behaviour. */
/**
 * The closed set of system inference-profile geography prefixes. Source: `aws bedrock
 * list-inference-profiles --type-equals SYSTEM_DEFINED` in us-east-1, eu-west-1, ap-northeast-1,
 * ap-southeast-2, ca-central-1 and ap-south-1, read 2026-10-04; `us-gov` is kept from the earlier
 * list and is not queryable from a commercial account [unverified]. A model ID shaped like a
 * profile with a geography not listed here is refused at synth (`modelInvokeStatements`).
 *
 * If a Region's system profiles use a prefix not listed here, confirm it with
 * `aws bedrock list-inference-profiles --type-equals SYSTEM_DEFINED --region <region>`, confirm
 * the profile ARN shape `arn:<partition>:bedrock:<region>:<account>:inference-profile/<prefix>.<model>`,
 * then add the prefix here.
 */
export const PROFILE_GEOGRAPHIES = ['us', 'eu', 'apac', 'global', 'jp', 'au', 'ca', 'in', 'us-gov'] as const;

/**
 * Bedrock model-provider namespaces, the first segment of every bare foundation-model ID. Source:
 * `aws bedrock list-foundation-models` in the same six Regions, read 2026-10-04. It serves only
 * the synth-time refusal of a profile ID with an unlisted geography: a provider missing here can
 * never cause a false refusal, it only loses that catch, and the call then fails closed.
 */
export const BEDROCK_PROVIDER_NAMESPACES = [
  'ai21', 'amazon', 'anthropic', 'cohere', 'deepseek', 'google', 'meta', 'minimax', 'mistral',
  'moonshot', 'moonshotai', 'nvidia', 'openai', 'qwen', 'stability', 'twelvelabs', 'writer', 'xai',
  'zai',
] as const;

const PROFILE_PREFIX = new RegExp(
  `^(${[...PROFILE_GEOGRAPHIES].sort((a, b) => b.length - a.length).join('|')})\\.`,
);

/** Strip a system inference-profile geography prefix for foundation-model IAM. */
export function bedrockBaseModelId(modelId: string): string {
  return modelId.replace(PROFILE_PREFIX, '');
}

/** Whether a configured model ID is a system inference profile (it has a listed geography prefix). */
export function isInferenceProfileId(modelId: string): boolean {
  return bedrockBaseModelId(modelId) !== modelId;
}

export type ParamArea =
  | 'api'
  | 'auth'
  | 'frontend'
  | 'data'
  | 'agent'
  | 'insight'
  | 'evaluation'
  | 'agent_quality'
  | 'operations'
  | 'publisher'
  // The token service's issuer and route exchange (ADR-077 §3). Separate from `auth`, which is the
  // retained Cognito boundary; everything here is destroyable.
  | 'identity'
  // Separate from `data`, which is where the lake's own names live. This area is about the *trigger*
  // that fills the lake, and the two have opposite blast radiuses — every `data` parameter points at
  // something RETAIN, and everything here is rebuildable.
  | 'ingestion'
  // ADR-078: the edge's `CLOUDFRONT`-scope web ACL, written in us-east-1 whatever the application
  // region, because that is the only region such an ACL can exist in.
  | 'edge';

interface ParamSpec {
  readonly area: ParamArea;
  readonly name: string;
  /** Which stack writes it. Documented so the deploy order stays derivable. */
  readonly producedBy: string;
}

/** Complete cross-language SSM path layout; generated consumers receive this exact sequence. */
export const PARAMETER_PATH_SEGMENTS = [
  'adlytics',
  '{environment}',
  '{area}',
  '{name}',
] as const;

export const PARAMS = {
  apiBaseUrl: { area: 'api', name: 'base-url', producedBy: 'ApiStack' },
  publicBaseUrl: { area: 'frontend', name: 'public-base-url', producedBy: 'FrontendStack' },
  apiId: { area: 'api', name: 'id', producedBy: 'ApiStack' },
  userPoolId: { area: 'auth', name: 'user-pool-id', producedBy: 'AuthStack' },
  userPoolClientId: { area: 'auth', name: 'user-pool-client-id', producedBy: 'AuthStack' },
  // ADR-078: the public PKCE client whose access tokens the authorizer admits as a bearer on the
  // two review routes only.
  userPoolPublicClientId: { area: 'auth', name: 'public-client-id', producedBy: 'AuthStack' },
  userPoolClientSecretName: {
    area: 'auth',
    name: 'user-pool-client-secret-name',
    producedBy: 'AuthStack',
  },
  authDomain: { area: 'auth', name: 'domain', producedBy: 'AuthStack' },
  // ADR-077 §3: the issuer is the public REST API stage URL, with no trailing slash. The Harness
  // (and, from S4, the Gateway) authorizer builds its discovery URL from it.
  tokenIssuerUrl: { area: 'identity', name: 'token-issuer-url', producedBy: 'TokenServiceStack' },
  // The route exchange is invoked directly by the Conversation Function, never through a route.
  tokenRouteExchangeFunctionArn: {
    area: 'identity',
    name: 'token-route-exchange-function-arn',
    producedBy: 'TokenServiceStack',
  },
  // ADR-077 S4: the AgentCore OAuth2 credential provider that exchanges a Harness token for a
  // Gateway token at the issuer's `/token`, and the agent client secret it authenticates with.
  tokenCredentialProviderArn: {
    area: 'identity',
    name: 'token-credential-provider-arn',
    producedBy: 'TokenServiceStack',
  },
  agentClientSecretArn: {
    area: 'identity',
    name: 'agent-client-secret-arn',
    producedBy: 'TokenServiceStack',
  },
  // ADR-077 §5: tools read the verification key with kms:GetPublicKey, never from the public issuer.
  tokenSigningKeyArn: {
    area: 'identity',
    name: 'token-signing-key-arn',
    producedBy: 'TokenServiceStack',
  },
  frontendBucketName: { area: 'frontend', name: 'bucket-name', producedBy: 'FrontendStack' },
  distributionId: { area: 'frontend', name: 'distribution-id', producedBy: 'FrontendStack' },
  // Always in us-east-1 (EDGE_SECURITY_REGION), never the application region.
  edgeWebAclArn: { area: 'edge', name: 'web-acl-arn', producedBy: 'EdgeSecurityStack' },
  semanticV2RawBucketName: {
    area: 'data',
    name: 'v2/raw-bucket',
    producedBy: 'DataV2Stack',
  },
  semanticV2BucketName: {
    area: 'data',
    name: 'v2/bucket',
    producedBy: 'DataV2Stack',
  },
  semanticV2AthenaResultsBucketName: {
    area: 'data',
    name: 'v2/athena-results-bucket',
    producedBy: 'DataV2Stack',
  },
  semanticV2AthenaWorkgroupInteractive: {
    area: 'data',
    name: 'v2/athena-workgroup-interactive',
    producedBy: 'DataV2Stack',
  },
  semanticV2AthenaWorkgroupBatch: {
    area: 'data',
    name: 'v2/athena-workgroup-batch',
    producedBy: 'DataV2Stack',
  },
  semanticV2AthenaWorkgroupBake: {
    area: 'data',
    name: 'v2/athena-workgroup-bake',
    producedBy: 'DataV2Stack',
  },
  semanticV2AthenaWorkgroupInsightPrecompute: {
    area: 'data',
    name: 'v2/athena-workgroup-insight-precompute',
    producedBy: 'DataV2Stack',
  },
  semanticV2AttemptsTableName: {
    area: 'data',
    name: 'v2/semantic-attempts-table',
    producedBy: 'DataV2Stack',
  },
  semanticV2AttemptsStreamArn: {
    area: 'data',
    name: 'v2/semantic-attempts-stream-arn',
    producedBy: 'DataV2Stack',
  },
  semanticV2LeasesTableName: {
    area: 'data',
    name: 'v2/semantic-leases-table',
    producedBy: 'DataV2Stack',
  },
  semanticV2PublicationsTableName: {
    area: 'data',
    name: 'v2/semantic-publications-table',
    producedBy: 'DataV2Stack',
  },
  semanticV2PublicationsStreamArn: {
    area: 'data',
    name: 'v2/semantic-publications-stream-arn',
    producedBy: 'DataV2Stack',
  },
  semanticV2SourceDatabase: {
    area: 'data',
    name: 'v2/source-aligned-glue-database',
    producedBy: 'DataV2Stack',
  },
  semanticV2CandidateDatabase: {
    area: 'data',
    name: 'v2/canonical-candidate-glue-database',
    producedBy: 'DataV2Stack',
  },
  semanticV2CurrentDatabase: {
    area: 'data',
    name: 'v2/canonical-current-glue-database',
    producedBy: 'DataV2Stack',
  },
  publisherArtifactBucketName: {
    area: 'publisher',
    name: 'artifact-bucket-name',
    producedBy: 'PublisherArtifactStoreStack',
  },
  publisherArtifactPointerTableName: {
    area: 'publisher',
    name: 'artifact-pointer-table-name',
    producedBy: 'PublisherArtifactStoreStack',
  },
  publisherArtifactPointerTableArn: {
    area: 'publisher',
    name: 'artifact-pointer-table-arn',
    producedBy: 'PublisherArtifactStoreStack',
  },
  publisherBakeQueueUrl: {
    area: 'publisher',
    name: 'bake-queue-url',
    producedBy: 'PublisherStack',
  },
  publisherBakeQueueArn: {
    area: 'publisher',
    name: 'bake-queue-arn',
    producedBy: 'PublisherStack',
  },
  // Published unsubscribed. The topic is the seam a customer attaches their own destination to, and
  // a subscription in the template would be our address in their account.
  ingestionAlarmTopicArn: {
    area: 'ingestion',
    name: 'alarm-topic-arn',
    producedBy: 'IngestionStack',
  },
  landingRoleArn: {
    area: 'ingestion',
    name: 'landing-role-arn',
    producedBy: 'IngestionStack',
  },
  landingIncomingPrefix: {
    area: 'ingestion',
    name: 'incoming-prefix',
    producedBy: 'IngestionStack',
  },
  registrationStatusTableName: {
    area: 'ingestion',
    name: 'registration-status-table',
    producedBy: 'IngestionStack',
  },
  providerFenceRecoveryOperatorRoleArn: {
    area: 'ingestion',
    name: 'provider-fence-recovery-operator-role-arn',
    producedBy: 'IngestionStack',
  },
  providerFenceRecoveryFunctionArn: {
    area: 'ingestion',
    name: 'provider-fence-recovery-function-arn',
    producedBy: 'IngestionStack',
  },
  semanticProjectionOperatorRoleArn: {
    area: 'ingestion',
    name: 'semantic-projection-operator-role-arn',
    producedBy: 'IngestionStack',
  },
  // Load Path §2.5: the four typed paths the read-only pre-switch verifier resolves before it
  // assumes its dedicated role.
  stage1PreswitchVerificationRoleArn: {
    area: 'ingestion',
    name: 'stage1-preswitch-verification-role-arn',
    producedBy: 'IngestionStack',
  },
  semanticWriteQueueArn: {
    area: 'ingestion',
    name: 'semantic-write-queue-arn',
    producedBy: 'IngestionStack',
  },
  semanticWriteDlqArn: {
    area: 'ingestion',
    name: 'semantic-write-dlq-arn',
    producedBy: 'IngestionStack',
  },
  deliveryFunctionArn: {
    area: 'ingestion',
    name: 'delivery-function-arn',
    producedBy: 'IngestionStack',
  },
  operationalStatusProjectionTableName: {
    area: 'operations',
    name: 'status-projection-table-name',
    producedBy: 'OperationalStatusStoreStack',
  },
  // S1 control-plane authority: the retained execution-graph store (Option B, S2 v1 core).
  executionGraphTableName: {
    area: 'operations',
    name: 'execution-graph-table-name',
    producedBy: 'ControlPlaneAuthorityStoreStack',
  },
  executionGraphTableArn: {
    area: 'operations',
    name: 'execution-graph-table-arn',
    producedBy: 'ControlPlaneAuthorityStoreStack',
  },
  // S2: the live table's stream, read only by the status v2 graph projector.
  executionGraphTableStreamArn: {
    area: 'operations',
    name: 'execution-graph-table-stream-arn',
    producedBy: 'ControlPlaneAuthorityStoreStack',
  },
  operationalStatusProjectionTableArn: {
    area: 'operations',
    name: 'status-projection-table-arn',
    producedBy: 'OperationalStatusStoreStack',
  },
  operationalStatusQueueUrl: {
    area: 'operations',
    name: 'status-queue-url',
    producedBy: 'OperationalStatusProcessorStack',
  },
  operationalStatusQueueArn: {
    area: 'operations',
    name: 'status-queue-arn',
    producedBy: 'OperationalStatusProcessorStack',
  },
  stage1OutboxRecoveryOperatorRoleArn: {
    area: 'operations',
    name: 'stage1-outbox-recovery-operator-role-arn',
    producedBy: 'OperationalStatusProcessorStack',
  },
  stage1OutboxRecoveryFunctionArn: {
    area: 'operations',
    name: 'stage1-outbox-recovery-function-arn',
    producedBy: 'OperationalStatusProcessorStack',
  },
  pageContextTableName: {
    area: 'agent',
    name: 'page-context-table',
    producedBy: 'AgentStack',
  },
  measurementFunctionArn: {
    area: 'agent',
    name: 'measurement-function-arn',
    producedBy: 'AgentStack',
  },
  guardrailId: { area: 'agent', name: 'guardrail-id', producedBy: 'AgentStack' },
  guardrailArn: { area: 'agent', name: 'guardrail-arn', producedBy: 'AgentStack' },
  guardrailVersion: { area: 'agent', name: 'guardrail-version', producedBy: 'AgentStack' },
  harnessArn: { area: 'agent', name: 'harness-arn', producedBy: 'HarnessStack' },
  harnessPromptDigest: {
    area: 'agent',
    name: 'harness-prompt-digest',
    producedBy: 'HarnessStack',
  },
  agentQualityAuditTableName: {
    area: 'agent_quality',
    name: 'audit-table',
    producedBy: 'AgentQualityStack',
  },
  insightPromptRegistryTableName: {
    area: 'insight',
    name: 'prompt-registry-table',
    producedBy: 'InsightPromptRegistryStack',
  },
  insightPromptRegistryTableArn: {
    area: 'insight',
    name: 'prompt-registry-table-arn',
    producedBy: 'InsightPromptRegistryStack',
  },
  insightCacheTableName: {
    area: 'insight',
    name: 'cache-table',
    producedBy: 'InsightCacheStack',
  },
  insightCacheTableArn: {
    area: 'insight',
    name: 'cache-table-arn',
    producedBy: 'InsightCacheStack',
  },
  insightPromptCalibrationQueueUrl: {
    area: 'insight',
    name: 'prompt-calibration-queue-url',
    producedBy: 'InsightPromptCalibrationStack',
  },
  insightPromptCalibrationQueueArn: {
    area: 'insight',
    name: 'prompt-calibration-queue-arn',
    producedBy: 'InsightPromptCalibrationStack',
  },
  insightContinuationQueueUrl: {
    area: 'insight',
    name: 'continuation-queue-url',
    producedBy: 'InsightContinuationStack',
  },
  insightContinuationQueueArn: {
    area: 'insight',
    name: 'continuation-queue-arn',
    producedBy: 'InsightContinuationStack',
  },
  agentQualityAuditTableArn: {
    area: 'agent_quality',
    name: 'audit-table-arn',
    producedBy: 'AgentQualityStack',
  },
  agentQualityAuditStreamArn: {
    area: 'agent_quality',
    name: 'audit-stream-arn',
    producedBy: 'AgentQualityStack',
  },
  agentQualityAggregateTableName: {
    area: 'agent_quality',
    name: 'aggregate-table',
    producedBy: 'AgentQualityStack',
  },
  agentQualityAggregateTableArn: {
    area: 'agent_quality',
    name: 'aggregate-table-arn',
    producedBy: 'AgentQualityStack',
  },
  // ADR-080 §1: each per-turn record family that is not a receipt has its own one-writer table.
  agentQualityFindingsTableName: {
    area: 'agent_quality',
    name: 'findings-table',
    producedBy: 'AgentQualityStack',
  },
  agentQualityFindingsTableArn: {
    area: 'agent_quality',
    name: 'findings-table-arn',
    producedBy: 'AgentQualityStack',
  },
  // ADR-080 §4: each record table's KEYS_ONLY stream feeds its item-change pipe.
  agentQualityFindingsStreamArn: {
    area: 'agent_quality',
    name: 'findings-stream-arn',
    producedBy: 'AgentQualityStack',
  },
  // ADR-081 §4: the retained reviewer access log the HTTP API stage writes.
  agentQualityReviewerAccessLogGroupArn: {
    area: 'agent_quality',
    name: 'reviewer-access-log-group-arn',
    producedBy: 'AgentQualityStack',
  },
  gatewayId: { area: 'agent', name: 'gateway-id', producedBy: 'AgentStack' },
  // The MCP endpoint an agent connects to. Published alongside the id because a consumer needs the
  // URL and deriving it from the id would encode the service's own hostname format here.
  gatewayUrl: { area: 'agent', name: 'gateway-url', producedBy: 'AgentStack' },
  evaluationInputQueueUrl: {
    area: 'evaluation',
    name: 'input-queue-url',
    producedBy: 'EvaluationInputStack',
  },
  evaluationInputQueueArn: {
    area: 'evaluation',
    name: 'input-queue-arn',
    producedBy: 'EvaluationInputStack',
  },
  evaluationInputLogGroupName: {
    area: 'evaluation',
    name: 'input-log-group',
    producedBy: 'EvaluationEvidenceStack',
  },
  evaluationOutboxTableName: {
    area: 'evaluation',
    name: 'outbox-table',
    producedBy: 'EvaluationEvidenceStack',
  },
  evaluationOutboxTableArn: {
    area: 'evaluation',
    name: 'outbox-table-arn',
    producedBy: 'EvaluationEvidenceStack',
  },
  evaluationDeliveriesTableName: {
    area: 'evaluation',
    name: 'deliveries-table',
    producedBy: 'EvaluationEvidenceStack',
  },
  evaluationDeliveriesTableArn: {
    area: 'evaluation',
    name: 'deliveries-table-arn',
    producedBy: 'EvaluationEvidenceStack',
  },
  evaluationWriterReceiptsTableName: {
    area: 'evaluation',
    name: 'writer-receipts-table',
    producedBy: 'EvaluationEvidenceStack',
  },
  evaluationWriterReceiptsTableArn: {
    area: 'evaluation',
    name: 'writer-receipts-table-arn',
    producedBy: 'EvaluationEvidenceStack',
  },
  evaluationVerdictsTableName: {
    area: 'evaluation',
    name: 'verdicts-table',
    producedBy: 'EvaluationEvidenceStack',
  },
  evaluationVerdictsTableArn: {
    area: 'evaluation',
    name: 'verdicts-table-arn',
    producedBy: 'EvaluationEvidenceStack',
  },
  evaluationDeliveriesStreamArn: {
    area: 'evaluation',
    name: 'deliveries-stream-arn',
    producedBy: 'EvaluationEvidenceStack',
  },
  evaluationWriterReceiptsStreamArn: {
    area: 'evaluation',
    name: 'writer-receipts-stream-arn',
    producedBy: 'EvaluationEvidenceStack',
  },
  evaluationVerdictsStreamArn: {
    area: 'evaluation',
    name: 'verdicts-stream-arn',
    producedBy: 'EvaluationEvidenceStack',
  },
  evaluationInputServiceName: {
    area: 'evaluation',
    name: 'input-service-name',
    producedBy: 'EvaluationInputStack',
  },
  // ADR-079: the stack-owned result group the gate binds and the result ingress subscribes to.
  evaluationV4ResultLogGroupName: {
    area: 'evaluation',
    name: 'result-log-group-v4',
    producedBy: 'EvaluationEvidenceStack',
  },
  // Conversation state. Published by a RETAIN stack and read by destroyable Runtime generations.
  memoryArn: { area: 'agent', name: 'memory-arn', producedBy: 'MemoryStack' },
  // A digest of the declared long-term strategies. The Harness carries it so a strategy change
  // updates the Harness's retrieval configuration in the same deploy pass (ADR-147).
  memoryStrategyRevision: {
    area: 'agent',
    name: 'memory-strategy-revision',
    producedBy: 'MemoryStack',
  },
  // The conversation route's Function URL, which `FrontendStack` attaches as a CloudFront origin.
  // Published rather than derived: the URL contains a service-generated identifier, so building it
  // here would be this repository reconstructing a hostname format Lambda owns.
  //
  // This is the second half of the two-pass the API origin already needs — `FrontendStack` deploys,
  // `ApiStack` reads the distribution id and publishes this, then `FrontendStack` deploys again to
  // attach it. See `EdgeConfig.conversationOriginAttached`.
  // The one Python layer every function attaches, as a *version* ARN. Published rather than
  // derived: a LayerVersion's ARN carries a version number the service assigns, so it cannot be
  // built from a name — and pinning it is what makes a function's dependency set reproducible.
  dependenciesLayerArn: {
    area: 'api',
    name: 'dependencies-layer-arn',
    producedBy: 'DependenciesStack',
  },
  conversationFunctionUrl: {
    area: 'api',
    name: 'conversation-function-url',
    producedBy: 'ApiStack',
  },
} as const satisfies Record<string, ParamSpec>;

export type ParamKey = keyof typeof PARAMS;

/**
 * The single source of every cross-stack parameter path.
 *
 * Never build one of these by string concatenation at a call site — that is how
 * a producer and a consumer end up disagreeing by one character, which presents
 * as missing configuration rather than as a typo.
 *
 * The name is validated here too, not only in `getConfig`. This is the point where
 * a bad name would become a malformed SSM path, and a hand-built AppConfig reaches
 * it without passing through `getConfig`.
 */
export function paramPath(envName: string, key: ParamKey): string {
  assertValidEnvName(envName);
  const spec = PARAMS[key];
  const values: Readonly<Record<string, string>> = {
    '{environment}': envName,
    '{area}': spec.area,
    '{name}': spec.name,
  };
  return `/${PARAMETER_PATH_SEGMENTS.map((segment) => values[segment] ?? segment).join('/')}`;
}

/**
 * Name of the Secrets Manager secret holding the app client secret.
 *
 * Derived rather than passed through SSM so a consumer can build the IAM resource
 * ARN at synth time. A deploy-time SSM token cannot be used in an IAM policy
 * resource reliably, and granting a wildcard instead would defeat the point.
 */
export function clientSecretName(envName: string): string {
  assertValidEnvName(envName);
  return `adlytics/${envName}/auth/bff-client-secret`;
}

/** The only region a `CLOUDFRONT`-scope web ACL can be created in (ADR-078). */
export const EDGE_SECURITY_REGION = 'us-east-1';
/**
 * The header CloudFront adds to every request it forwards to the HTTP API (ADR-078). The Python
 * side declares the same name in `domain/identity/origin_verify.py`; a test holds them equal.
 */
export const ORIGIN_VERIFY_HEADER = 'x-adl-origin-verify';
/**
 * Name of the Secrets Manager secret holding the origin-verify values, `{"current", "previous"}`.
 * Derived for the same reason as `clientSecretName`: the reader's IAM resource is built at synth.
 */
export function originVerifySecretName(envName: string): string {
  assertValidEnvName(envName);
  return `adlytics/${envName}/edge/origin-verify`;
}
export type SemanticV2DatabaseLayer = 'source-aligned' | 'candidate' | 'current';

/** Physical database for one isolated Semantic V2 storage layer. */
export function semanticV2DatabaseName(
  envName: string,
  layer: SemanticV2DatabaseLayer,
): string {
  assertValidEnvName(envName);
  const normalized = envName.replace(/-/g, '_');
  const suffix = layer === 'source-aligned' ? 'source_aligned' : layer;
  return `adlytics_${normalized}_v2_${suffix}`;
}

/**
 * The execution classes kept apart by F-14 and SEMANTIC-M1: CHAT reads on `interactive`, every
 * batch DML caller on `bake`, the insight precompute worker on `insight-precompute`. `batch` is
 * retained for pre-cutover terminal reads and the reconciler's persisted-envelope replay.
 */
export type AthenaWorkgroupKind = 'interactive' | 'batch' | 'bake' | 'insight-precompute';

/** Physical workgroup for one isolated Semantic V2 execution class. */
export function semanticV2AthenaWorkgroupName(
  envName: string,
  kind: AthenaWorkgroupKind,
): string {
  assertValidEnvName(envName);
  return `adlytics-${envName}-v2-${kind}`;
}

/**
 * Athena engine version every workgroup pins.
 *
 * Pinned rather than left on `AUTO`, and this is a correctness requirement rather than a
 * stability preference: `MERGE INTO` is transactional, Iceberg-only and **engine v3 only**,
 * and the curated write path depends on it. `AUTO` lets Athena choose, so a future default
 * could silently remove the statement the load is built on.
 */
export const ATHENA_ENGINE_VERSION = 'Athena engine version 3';

/** The three independent authorities that make up Semantic V2 publication state. */
export type SemanticStateTableKind = 'attempts' | 'leases' | 'publications';

/** Name of an isolated Semantic V2 state authority. */
export function semanticV2StateTableName(
  envName: string,
  kind: SemanticStateTableKind,
): string {
  assertValidEnvName(envName);
  return `adlytics-${envName}-v2-semantic-${kind}`;
}

export function producerRegistrationStatusTableName(envName: string): string {
  assertValidEnvName(envName);
  return `adlytics-${envName}-producer-registration-status`;
}

export function pageContextTableName(envName: string): string {
  assertValidEnvName(envName);
  return `adlytics-${envName}-page-context`;
}

/** Physical names for the retained operational projection and disposable transport. */
/** The retained execution-graph table, and its dev probe pair (V-15). */

export function executionGraphTableName(envName: string, suffix: string): string {
  assertValidEnvName(envName);
  return `adlytics-${envName}-${suffix}`;
}

export function operationalStatusProjectionTableName(envName: string): string {
  assertValidEnvName(envName);
  return `adlytics-${envName}-operational-status`;
}

export function operationalStatusQueueName(envName: string, deadLetter = false): string {
  assertValidEnvName(envName);
  return `adlytics-${envName}-operational-status${deadLetter ? '-dlq' : ''}.fifo`;
}

export function operationalStatusFunctionName(
  envName: string,
  kind:
    | 'AttemptsProducer'
    | 'GraphProjector'
    | 'MetricReconciler'
    | 'Projector'
    | 'Reconciler'
    | 'Stage1LifecycleProducer'
    | 'Stage1OutboxRecoveryFunction',
): string {
  assertValidEnvName(envName);
  return `adlytics-${envName}-OperationalStatus${kind}`;
}

/** Retained immutable prompt versions and activation history, isolated from disposable caches. */
export function insightPromptRegistryTableName(envName: string): string {
  assertValidEnvName(envName);
  return `adlytics-${envName}-insight-prompt-registry`;
}

/** Retained revision-keyed presentation insight cache isolated from disposable consumers. */
export function insightCacheTableName(envName: string): string {
  assertValidEnvName(envName);
  return `adlytics-${envName}-insight-cache`;
}

/**
 * The managed Harness's name. `_agent` since the custom resource took ownership (ADR-077, amended
 * 2026-09-26): the retired `CfnHarness` held `_harness` and is deleted in the same deploy that
 * creates this one, so reusing its name would collide on create.
 */
export function harnessName(envName: string): string {
  assertValidEnvName(envName);
  return `adlytics_${envName.replace(/-/g, '_')}_agent`;
}

/**
 * The Harness provider's role. Named so the provider's reach is legible in CloudTrail; nothing
 * else names it.
 */
export function harnessProviderRoleName(envName: string): string {
  assertValidEnvName(envName);
  return `adlytics-${envName}-harness-provider`;
}

/**
 * The Harness execution role. Named because the agent client secret's resource policy admits it
 * by `aws:PrincipalArn`, and TokenServiceStack deploys before the role exists (ADR-077 §3).
 */
export function harnessExecutionRoleName(envName: string): string {
  assertValidEnvName(envName);
  return `adlytics-${envName}-harness-execution`;
}

/**
 * The `aud` of a route-minted Harness token, and the one audience the Harness authorizer allows.
 * ADR-077 §2. One helper so the issuer and the authorizer cannot disagree by a character.
 */
export function harnessTokenAudience(envName: string): string {
  assertValidEnvName(envName);
  return `adlytics-${envName}-harness`;
}

/**
 * The `client_id` and `act.sub` of a route-minted Harness token, and the one client the Harness
 * authorizer allows. ADR-077 §2.
 */
export function routeClientId(envName: string): string {
  assertValidEnvName(envName);
  return `adlytics-${envName}-conversation-route`;
}

/**
 * The `aud` of an exchanged Gateway token, and the one `audience` the issuer's `/token` accepts.
 * ADR-077 S4. One helper so the issuer and the Gateway authorizer cannot disagree by a character.
 */
export function gatewayTokenAudience(envName: string): string {
  assertValidEnvName(envName);
  return `adlytics-${envName}-gateway`;
}

/**
 * The OAuth client AgentCore Identity authenticates as at the issuer's `/token`, and the
 * `client_id` and `act.sub` of the Gateway token it receives. ADR-077 S4.
 */
export function agentClientId(envName: string): string {
  assertValidEnvName(envName);
  return `adlytics-${envName}-agent`;
}

/**
 * Secrets Manager name of the agent client's secret (JSON key `client_secret`).
 *
 * Generated at deploy by TokenServiceStack; the issuer compares against it and the credential
 * provider presents it, so neither ever holds it in a template or an environment variable.
 */
export function agentClientSecretName(envName: string): string {
  assertValidEnvName(envName);
  return `adlytics/${envName}/identity/agent-client-secret`;
}

/**
 * Name of the AgentCore OAuth2 credential provider registered for the agent client.
 *
 * Deterministic because the Harness execution role's GetResourceOauth2Token grant names the
 * provider's token-vault ARN, and the provider lives in TokenServiceStack.
 */
export function tokenCredentialProviderName(envName: string): string {
  assertValidEnvName(envName);
  return `adlytics-${envName}-token-service`;
}

/**
 * Physical name of the Conversation Function's role.
 *
 * Deterministic because TokenServiceStack's route-exchange resource policy denies every principal
 * whose `aws:PrincipalArn` is not this role, and it deploys before ApiStack creates the role.
 */
export function conversationFunctionRoleName(envName: string): string {
  assertValidEnvName(envName);
  return `adlytics-${envName}-conversation-function`;
}

/** Route-exchange role; the signing key policy denies `kms:Sign` to all but it and the issuer. */
export function tokenRouteExchangeRoleName(envName: string): string {
  assertValidEnvName(envName);
  return `adlytics-${envName}-token-route-exchange`;
}

/**
 * Public issuer role. It reads the signing key's public half for the key set and, from S4, signs
 * the Gateway tokens its `/token` exchange mints; the key policy admits only it and the route
 * exchange to sign.
 */
export function tokenIssuerRoleName(envName: string): string {
  assertValidEnvName(envName);
  return `adlytics-${envName}-token-issuer`;
}

/**
 * The Conversation Function timeout: the deep-dive budget plus 30 seconds for the route's own work.
 *
 * One fact with two readers. ApiStack sets the function timeout from it, and TokenServiceStack
 * derives a Harness token's lifetime from it (ADR-077 §2: a token lives for the longest a turn
 * can run), so a turn budget change moves both.
 */
export function conversationFunctionTimeoutSeconds(config: AppConfig): number {
  return config.agent.deepDive.timeoutSeconds + 30;
}

/** Physical names for the isolated retained Agent Quality boundary and its processor. */
export function agentQualityAuditTableName(envName: string): string {
  assertValidEnvName(envName);
  return `adlytics-${envName}-agent-quality-audit`;
}

export function agentQualityAggregateTableName(envName: string): string {
  assertValidEnvName(envName);
  return `adlytics-${envName}-agent-quality-aggregate`;
}

/** ADR-080 §1: the findings table, whose only writer is the agent-quality processor. */
export function agentQualityFindingsTableName(envName: string): string {
  assertValidEnvName(envName);
  return `adlytics-${envName}-agent-quality-findings`;
}

/** ADR-080 §1: evaluation delivery receipts, whose only writer is the outbox dispatcher. */
export function evaluationDeliveriesTableName(envName: string): string {
  assertValidEnvName(envName);
  return `adlytics-${envName}-evaluation-deliveries`;
}

/** ADR-080 §1: evaluation writer receipts, whose only writer is the evaluation writer. */
export function evaluationWriterReceiptsTableName(envName: string): string {
  assertValidEnvName(envName);
  return `adlytics-${envName}-evaluation-writer-receipts`;
}

/** ADR-080 §1: evaluation verdicts, whose only writer is the result linker. */
export function evaluationVerdictsTableName(envName: string): string {
  assertValidEnvName(envName);
  return `adlytics-${envName}-evaluation-verdicts`;
}

/*
 * Fixed role names for every writer and reader of the ADR-080 record tables. Deterministic so a
 * table's resource policy can name a role by `aws:PrincipalArn` before the role's stack deploys,
 * as the route-exchange policy names the Conversation Function role (ADR-077 §3).
 */
export function agentQualityProcessorRoleName(envName: string): string {
  assertValidEnvName(envName);
  return `adlytics-${envName}-agent-quality-processor`;
}

export function evaluationOutboxDispatcherRoleName(envName: string): string {
  assertValidEnvName(envName);
  return `adlytics-${envName}-evaluation-outbox-dispatcher`;
}

export function evaluationWriterRoleName(envName: string): string {
  assertValidEnvName(envName);
  return `adlytics-${envName}-evaluation-writer`;
}

export function evaluationResultIngressRoleName(envName: string): string {
  assertValidEnvName(envName);
  return `adlytics-${envName}-evaluation-result-ingress`;
}

export function evaluationResultLinkerRoleName(envName: string): string {
  assertValidEnvName(envName);
  return `adlytics-${envName}-evaluation-result-linker`;
}

/** The one quality-review function serves both review routes under this role (ADR-078). */
export function qualityReviewFunctionRoleName(envName: string): string {
  assertValidEnvName(envName);
  return `adlytics-${envName}-quality-review`;
}

/**
 * Fixed function names for the functions that run under a role a record-table policy names, so
 * the function-change alarm (ADR-080 §4) can name each before its stack deploys.
 */
export function conversationFunctionName(envName: string): string {
  assertValidEnvName(envName);
  return `adlytics-${envName}-ConversationFunction`;
}

export function evaluationWriterFunctionName(envName: string): string {
  assertValidEnvName(envName);
  return `adlytics-${envName}-EvaluationWriter`;
}

export function qualityReviewFunctionName(envName: string): string {
  assertValidEnvName(envName);
  return `adlytics-${envName}-QualityReview`;
}

/** The ARN of a fixed-name role, buildable before the role's stack deploys. */
export function fixedRoleArn(partition: string, account: string, roleName: string): string {
  return `arn:${partition}:iam::${account}:role/${roleName}`;
}

export function agentQualityProcessorFunctionName(envName: string): string {
  assertValidEnvName(envName);
  return `adlytics-${envName}-AgentQualityProcessor`;
}

export function evaluationResultIngressV3FunctionName(envName: string): string {
  assertValidEnvName(envName);
  return `adlytics-${envName}-EvaluationResultIngressV3`;
}

export function evaluationResultLinkerV3FunctionName(envName: string): string {
  assertValidEnvName(envName);
  return `adlytics-${envName}-EvaluationResultLinkerV3`;
}

export function evaluationResultProjectionV3QueueName(envName: string): string {
  assertValidEnvName(envName);
  return `adlytics-${envName}-evaluation-result-projection-v3`;
}
/** The projection queue's dead-letter queue, named once for its producer and the dashboard. */
export function evaluationResultProjectionV3DeadLetterQueueName(envName: string): string {
  return `${evaluationResultProjectionV3QueueName(envName)}-dlq`;
}

/**
 * A current online-evaluation family, each with its own gate, result group, and release record
 * (ADR-079). V3 was retired 2026-09-27 when V4 superseded it.
 */
export type EvaluationFamily = 'v4';

export const EVALUATION_FAMILIES: readonly EvaluationFamily[] = ['v4'];

/** The retained reviewer access log group (ADR-081 §4, observability §3.8). */
export function reviewerAccessLogGroupName(envName: string): string {
  assertValidEnvName(envName);
  return `/adlytics/${envName}/agent-quality/reviewer-access`;
}

/** CloudWatch Logs' finite retention values, ascending. `INFINITE` is not a day count. */
const FINITE_LOG_RETENTION_DAYS: readonly logs.RetentionDays[] = Object.values(logs.RetentionDays)
  .filter((value): value is logs.RetentionDays =>
    typeof value === 'number' && value !== logs.RetentionDays.INFINITE)
  .sort((a, b) => a - b);

/**
 * The reviewer access log's retention: the smallest CloudWatch value that covers the receipt
 * retention plus the TTL-lag margin (ADR-081 §4). Throws, failing synth, when none does or when
 * either input is not a positive whole number of days.
 */
export function reviewerAccessLogRetention(
  config: Pick<AppConfig, 'receiptRetentionDays' | 'receiptTtlLagMarginDays'>,
): logs.RetentionDays {
  const { receiptRetentionDays: receipts, receiptTtlLagMarginDays: margin } = config;
  if (!Number.isInteger(receipts) || receipts < 1 || !Number.isInteger(margin) || margin < 0) {
    throw new Error('The reviewer access log needs whole-day receipt retention and TTL-lag margin.');
  }
  const floor = receipts + margin;
  const covering = FINITE_LOG_RETENTION_DAYS.find((days) => days >= floor);
  if (covering === undefined) {
    throw new Error(
      `No CloudWatch retention covers ${floor} days (receipt retention plus TTL-lag margin), ` +
      'so the reviewer access log would expire before the receipts it attributes access to.',
    );
  }
  return covering;
}

/**
 * The stack-owned result group of one family, outside the service-reserved
 * `/aws/bedrock-agentcore/evaluations/` prefix (observability §3.7).
 */
export function evaluationResultLogGroupName(envName: string, family: EvaluationFamily): string {
  assertValidEnvName(envName);
  return `/adlytics/${envName}/evaluation/results-${family}`;
}

/**
 * The family's release record. Written only by the gate provider at runtime, so no stack produces
 * it and it is not in PARAMS; every stack that names it goes through this helper.
 */
export function evaluationReleaseParameterName(envName: string, family: EvaluationFamily): string {
  assertValidEnvName(envName);
  return `/adlytics/${envName}/evaluation/release-${family}`;
}

/**
 * The gate-owned online-evaluation configuration name. `[a-zA-Z][a-zA-Z0-9_]{0,47}`, so hyphens
 * are substituted. New names: the retired CloudFormation configurations were `..._helpfulness_v3`
 * and `..._quality_v4`, and a gate create must never collide with them.
 */
export function onlineEvaluationGateName(envName: string, family: EvaluationFamily): string {
  assertValidEnvName(envName);
  return `adlytics_${envName.replace(/-/g, '_')}_released_${family}`;
}

/** The provider behind one family's `Custom::AgentCoreOnlineEvaluation`, fixed for the tamper rules. */
export function evaluationGateProviderFunctionName(envName: string, family: EvaluationFamily): string {
  assertValidEnvName(envName);
  return `adlytics-${envName}-EvaluationGate-${family}`;
}

/** The gate provider's role, the only principal the release-record rule does not alarm on. */
export function evaluationGateProviderRoleName(envName: string, family: EvaluationFamily): string {
  assertValidEnvName(envName);
  return `adlytics-${envName}-evaluation-gate-${family}`;
}

/** The role the evaluation service assumes for one family; it writes only the owned group. */
export function evaluationExecutionRoleName(envName: string, family: EvaluationFamily): string {
  assertValidEnvName(envName);
  return `adlytics-${envName}-evaluation-execution-${family}`;
}

/** The result ingress's subscription filter on one family's owned group. */
export function evaluationResultSubscriptionName(envName: string, family: EvaluationFamily): string {
  assertValidEnvName(envName);
  return `adlytics-${envName}-evaluation-result-ingress-${family}`;
}

export function evaluationOutboxTableName(envName: string): string {
  assertValidEnvName(envName);
  return `adlytics-${envName}-evaluation-outbox`;
}

export function evaluationOutboxDispatcherFunctionName(envName: string): string {
  assertValidEnvName(envName);
  return `adlytics-${envName}-EvaluationOutboxDispatcher`;
}

export function evaluationInputQueueName(envName: string): string {
  assertValidEnvName(envName);
  return `adlytics-${envName}-evaluation-input`;
}
/** The input queue's dead-letter queue, named once for its producer and the quality dashboard. */
export function evaluationInputDeadLetterQueueName(envName: string): string {
  return `${evaluationInputQueueName(envName)}-dlq`;
}

export function evaluationInputLogGroupName(envName: string): string {
  assertValidEnvName(envName);
  return `/adlytics/${envName}/evaluation/released-responses`;
}

export function evaluationInputServiceName(envName: string): string {
  assertValidEnvName(envName);
  return `adlytics-${envName}-released-response`;
}

/**
 * Name of the AgentCore Memory holding conversation state.
 *
 * `MemoryName` is `[a-zA-Z][a-zA-Z0-9_]{0,47}`, so no hyphens are permitted and an environment
 * name may contain them. Separate helper rather than a shared one because the Runtime and Memory
 * limits differ — 48 characters here against 48 for Runtime — and one helper would silently bind
 * unrelated service constraints.
 */
export function memoryName(envName: string): string {
  assertValidEnvName(envName);
  return `adlytics_${envName.replace(/-/g, '_')}_conversations`;
}

/**
 * Name of the AgentCore Gateway carrying the tools.
 *
 * A helper because two places now need it: `AgentStack` creates the gateway, and the policy-engine
 * grant has to name the gateway's ARN without a `GetAtt` — see `gateway-policy.ts` for why a token
 * there is a dependency cycle. It was a literal at the single call site until the second reader
 * appeared, which is the point at which a literal becomes two homes for one fact.
 *
 * Hyphenated because `GatewayName` accepts hyphens. Renamed from `adlytics-<env>-tools` on
 * 2026-09-26 because the gateway had to be replaced: the service refuses "Authorizer type cannot
 * be updated for an existing gateway" although the CloudFormation reference lists the authorizer
 * as an in-place update (ADR-077 slice 4). The new name shares no prefix with the old one, so
 * the name-prefix policy pattern in `gateway-policy.ts` never matches the gateway it replaced.
 */
export function toolGatewayName(envName: string): string {
  assertValidEnvName(envName);
  return `adlytics-${envName}-agent-tools`;
}

/**
 * Physical name of the AgentStack-owned Bedrock guardrail.
 *
 * The `-agent` suffix is historical: an API-owned guardrail named `adlytics-<env>-conversation`
 * coexisted with this one during the ownership bridge and has since been removed. The suffix
 * stays because renaming a guardrail replaces it, and Runtime and API routes apply this one
 * through the canonical SSM identity.
 */
export function agentRouteGuardrailName(envName: string): string {
  assertValidEnvName(envName);
  return `adlytics-${envName}-conversation-agent`;
}

/**
 * What a gateway policy is for. Becomes part of its name, so these are an interface.
 *
 * One kind, because one statement is expressible. A content-inspecting policy was written, probed
 * and removed: a guardrail cannot be scoped to a Lambda target on an MCP gateway (ADR-042). A second
 * kind returns when there is a second statement.
 */
export type GatewayPolicyKind = 'base_permit';

/**
 * Name of the AgentCore Policy Engine evaluating the tool gateway.
 *
 * The third name in this family needing the hyphen substitution, and the constraint was found the
 * expensive way rather than read: a `Policy` name is `^[A-Za-z][A-Za-z0-9_]*$`, and the hyphenated
 * form was rejected by change-set validation rather than by synth `[verified 2026-08-24, the
 * validation error on `Adlytics-Agent-dev`]`. The same constrained name transformation is why it
 * for the same reason it lives here rather than in the construct: a transformation at a call site
 * is what these helpers exist to prevent.
 *
 * Underscores throughout rather than only where `envName` forces them, so the engine and its
 * policies read as one family in a console listing.
 */
export function policyEngineName(envName: string): string {
  assertValidEnvName(envName);
  return `adlytics_${envName.replace(/-/g, '_')}_tool_policy`;
}

/**
 * Name of one Cedar policy on the tool gateway.
 *
 * Same constraint and the same substitution as `policyEngineName`. Separate helper because the
 * kind is load-bearing: a policy's name is what a denial annotation reports — AWS's own example of
 * an enforced refusal quotes the policy name back to the caller — so these names reach a
 * diagnostic and should say which control fired.
 */
export function gatewayPolicyName(envName: string, kind: GatewayPolicyKind): string {
  assertValidEnvName(envName);
  return `adlytics_${envName.replace(/-/g, '_')}_${kind}`;
}

/**
 * Version of the AWS-published Lambda Web Adapter layer.
 *
 * Pinned rather than tracking a latest, for the same reason the Python runtime is: the adapter
 * translates the invocation into an HTTP request and converts the streamed response back, so a
 * version moving underneath a deploy changes the transport of every conversation. A bump is a
 * deliberate, reviewable change.
 */
export const LAMBDA_WEB_ADAPTER_VERSION = 28;

/**
 * Account publishing the Lambda Web Adapter layer in AWS commercial regions.
 *
 * AWS-owned and identical in every commercial region, which is why the region is a parameter and
 * this is not. It is not a customer value and has no business being configurable.
 */
const LAMBDA_WEB_ADAPTER_ACCOUNT = '753240598075';

/**
 * ARN of the Lambda Web Adapter layer for this region, arm64 variant.
 *
 * A helper rather than a literal at the call site, because the region varies and a hand-built ARN
 * naming the wrong one fails at **function init** — the worst place to discover a packaging
 * mistake, and indistinguishable from an application error in the logs.
 *
 * Refuses the China partition rather than building an ARN for it. AWS publishes the adapter in
 * `cn-north-1` and `cn-northwest-1` as **x86_64 only** — there is no arm64 variant
 * [verified 2026-08-22 against the adapter's zip-packages guide] — and ADR-005 makes every
 * function here arm64. So a China deploy is a real incompatibility, not a missing string, and it
 * fails at synth with the reason instead of at init with a layer-not-found. Supporting it means
 * deciding whether the conversation function alone may be x86_64, which is a decision rather than
 * a lookup.
 */
export function lambdaWebAdapterLayerArn(region: string): string {
  if (region.startsWith('cn-')) {
    throw new Error(
      `The Lambda Web Adapter has no arm64 layer in ${region}; AWS publishes x86_64 only there. ` +
        'The conversation route cannot deploy to a China region without allowing that one ' +
        'function to be x86_64 — see lambdaWebAdapterLayerArn in infra/lib/config.',
    );
  }
  return `arn:aws:lambda:${region}:${LAMBDA_WEB_ADAPTER_ACCOUNT}:layer:LambdaAdapterLayerArm64:${LAMBDA_WEB_ADAPTER_VERSION}`;
}

/**
 * Deploy order. SSM removes CloudFormation's inferred ordering, so this is now
 * the pipeline's responsibility and is stated once, here.
 */
export const DEPLOY_ORDER = [
  // First, because it depends on nothing and every stack that declares a Lambda reads its output.
  // It replaced a LayerVersion declared separately inside ApiStack and AgentStack over the same
  // asset — one closure published twice, on independently drifting version streams.
  'DependenciesStack',
  // Permanent retained Semantic V2 storage and state.
  'DataV2Stack',
  // Development-only retained operational read model precedes its disposable projector and API reader.
  'OperationalStatusStoreStack',
  // Reads the attempts stream and writes only the retained operational projection above.
  'OperationalStatusProcessorStack',
  // Retained execution-graph authority store; precedes the API that admits run requests into it.
  'ControlPlaneAuthorityStoreStack',
  // Retained preset artifacts and pointers are independent of their disposable bake transport.
  'PublisherArtifactStoreStack',
  // The updated writer follows its retained target before any canonical reader. For an existing
  // deployment, retained evidence must be reprocessed and its publications validated manually
  // after this stack and before AgentStack or ApiStack; this stack-only list cannot enforce that
  // operational gate.
  'IngestionStack',
  // ADR-078: the distribution's web ACL, in us-east-1 whatever the application region, before
  // the FrontendStack that reads its ARN.
  'EdgeSecurityStack',
  // FrontendStack appears before AuthStack because the CloudFront domain is what
  // Cognito's callback URL must point at, and it does not exist until the
  // distribution does. On the first pass it deploys without the API origin; on the
  // second it is deployed again to attach it. See EdgeConfig.apiOriginAttached.
  'FrontendStack',
  'AuthStack',
  // ADR-077 §3: the route exchange verifies Cognito tokens, so it follows AuthStack. HarnessStack
  // reads the issuer URL and ApiStack the route-exchange ARN, so it precedes both, and it precedes
  // AgentStack because the Gateway authorizer names the same issuer from slice S4. Its policies
  // name deterministic role ARNs, so it deploys before the Conversation Function role exists.
  'TokenServiceStack',
  // Retained conversation state is independent of destroyable request-serving stacks.
  'MemoryStack',
  // Immutable prompt versions and activation events must exist before their disposable seed and API consumers.
  'InsightPromptRegistryStack',
  'InsightPromptRegistrySeedStack',
  // Retained minimized quality receipts and aggregate-ready measurements are independent of the
  // destroyable Runtime; their stream consumer is separately destroyable.
  'AgentQualityStack',
  'AgentQualityProcessorStack',
  // Before AgentStack, not last. Moved 2026-08-19: it holds the account-wide Transaction
  // Search configuration, and AgentCore cannot deliver spans to a per-agent log group unless
  // that is already on. A span destination is fixed when the agent is created and turning
  // ingestion on afterwards does not migrate spans already delivered elsewhere, so an
  // observability stack deployed after its span producers is the wrong way round.
  'ObservabilityStack',
  'AgentStack',
  'HarnessStack',
  // Retained customer-owned evaluation evidence must exist before its disposable transport and all
  // downstream consumers. The service name remains an Input-stack contract because the writer emits it.
  'EvaluationEvidenceStack',
  // ADR-080 §4: destroyable tamper detection reads every record table's stream ARN, so it follows
  // the last retained stack that owns one, and the alarm topic IngestionStack publishes. It names
  // functions and tables by fixed names, so it needs none of the stacks after it.
  'RecordIntegrityStack',
  'EvaluationInputStack',
  'EvaluationOutboxDispatchStack',
  // ADR-079: the result ingress and its subscription filters precede the gate stacks, which
  // assert those filters by readback. It reads only the owned group names EvaluationEvidenceStack
  // publishes, and takes each result's admissible configuration from the turn's delivery receipt at
  // runtime, so nothing it needs comes from a later stack.
  'EvaluationResultLinkV3Stack',
  'InsightCacheStack',
  'InsightContinuationStack',
  // Queued calibration follows its retained registry and publishes the queue consumed by ApiStack.
  'InsightPromptCalibrationStack',
  // Reads Semantic V2 publication updates, the retained artifact-store contracts, and — for
  // the §5.3 insight-fill child — the continuation queue and prompt registry parameters above.
  // Moved after InsightContinuationStack 2026-09-22 for exactly that read; nothing between the
  // old and new positions consumes a PublisherStack-produced parameter.
  'PublisherStack',
  'ApiStack',
  'InsightPrecomputeStack',
  // The evaluation gate. It binds its family's owned group and asserts the ingress filter above.
  'EvaluationV4Stack',
  // ADR-079 §3, §5: the dashboard and alarms over what the stacks above deploy. Last, and it
  // reads only parameters and fixed names produced earlier.
  'EvaluationQualityStack',
] as const;

/**
 * Reject a configuration that has not been filled in, naming every gap at once.
 *
 * `UNSET` is a placeholder, not a default. Letting one through would synthesise a
 * template that deploys somewhere unintended and looks fine doing it — which is
 * the specific failure this project has hit repeatedly with localhost URLs.
 *
 * Reporting all of them together is deliberate: discovering the next missing value
 * on the following synth turns one correction into an afternoon of them.
 */
/**
 * A configuration value that cannot be deployed with, and which kind of problem it is.
 *
 * The two kinds read completely differently to an operator and must not be
 * presented alike. `unset` is the expected state of a fresh checkout. `unresolved`
 * means a value was read from the wrong place, which is a fault.
 */
export interface ConfigProblem {
  readonly path: string;
  readonly kind: 'unset' | 'unresolved';
}

/**
 * Why a particular value cannot ship, for the values where that is not obvious.
 *
 * Only the keys a human has to think about. A missing entry falls back to a
 * generic line, so this never has to be exhaustive.
 */
const UNSET_HINTS: Readonly<Record<string, string>> = {
  'agent.quickChat.modelId':
    'comes from ADLYTICS_QUICK_CHAT_MODEL_ID; the Quick Runtime bootstrap model',
  'agent.deepDive.modelId':
    'comes from ADLYTICS_DEEP_DIVE_MODEL_ID; the server-selected deep Runtime model',
  'agent.memoryModelId':
    'comes from ADLYTICS_MEMORY_MODEL_ID; the long-term memory override model, no fallback',
  'insight.quickModelId':
    'comes from ADLYTICS_INSIGHT_QUICK_MODEL_ID, falling back to ADLYTICS_QUICK_CHAT_MODEL_ID ' +
    '(an empty-string export does not fall back); the card-tier insight narration model',
  'insight.deepModelId':
    'comes from ADLYTICS_INSIGHT_DEEP_MODEL_ID, falling back to ADLYTICS_DEEP_DIVE_MODEL_ID ' +
    '(an empty-string export does not fall back); the deep-dive insight narration model',
  'auth.loginDomainPrefix':
    'comes from ADLYTICS_LOGIN_DOMAIN_PREFIX; globally unique across all AWS accounts, ' +
    'and visible in the login URL',
  'landing.pipelineRoleArn':
    'comes from ADLYTICS_LANDING_PIPELINE_ROLE_ARN; exact external IAM role trusted to land data',
  'landing.externalId':
    'comes from ADLYTICS_LANDING_EXTERNAL_ID; deployment-specific value supplied to AssumeRole',
  athenaQueryBatchMaxConcurrency:
    'comes from ADLYTICS_ATHENA_QUERY_BATCH_MAX_CONCURRENCY; the general Athena query-batch ' +
    'admission, a whole number from 1 to the CHAT reserve (D7), with no default',
  receiptRetentionDays:
    'comes from ADLYTICS_RECEIPT_RETENTION_DAYS; whole days every record of a turn is kept ' +
    'after its receipt (ADR-081 §5), with no default',
  'edge.wafRateLimitPerFiveMinutes':
    'comes from ADLYTICS_EDGE_WAF_RATE_LIMIT_PER_FIVE_MINUTES; per-source-IP requests per five ' +
    'minutes the distribution web ACL admits (ADR-078), with no default',
  'auth.wafRateLimitPerFiveMinutes':
    'comes from ADLYTICS_USER_POOL_WAF_RATE_LIMIT_PER_FIVE_MINUTES; per-source-IP requests per ' +
    'five minutes the user pool web ACL admits (ADR-078), with no default',
  account: 'comes from CDK_DEFAULT_ACCOUNT, i.e. the profile passed to the CLI',
  region: 'comes from CDK_DEFAULT_REGION, i.e. the profile passed to the CLI',
};

/** One problem as a short phrase, shared by the message and the operator rendering. */
function describeProblem(problem: ConfigProblem): string {
  return problem.kind === 'unset'
    ? `${problem.path} is unset`
    : `${problem.path} resolved to localhost`;
}

/**
 * An incomplete configuration, raised so the entrypoint can present it as a guard
 * rather than as a crash.
 *
 * A distinct type on purpose. Tripping this guard is an expected outcome with a
 * known fix, so it prints a message and no stack trace; anything else thrown during
 * synth is a defect and keeps its trace. Before this existed both looked identical,
 * and a fresh checkout reported a missing login prefix as an uncaught exception.
 */
export class ConfigurationIncompleteError extends Error {
  constructor(
    readonly envName: string,
    readonly problems: readonly ConfigProblem[],
  ) {
    // The message stays single-line and names every path, so it is useful on its own
    // in a log or a generic catch. `format()` is the operator-facing rendering; the
    // two exist because a stack trace gets attached to this one and must not be the
    // thing a human reads first.
    super(
      `Configuration for "${envName}" is not usable — ` +
        `${problems.length} ${problems.length === 1 ? 'problem' : 'problems'}: ` +
        problems.map(describeProblem).join(', '),
    );
    this.name = 'ConfigurationIncompleteError';
    // Required for `instanceof` to survive the class-to-function downlevelling that
    // some TS targets apply to a subclassed built-in.
    Object.setPrototypeOf(this, ConfigurationIncompleteError.prototype);
  }

  /** True when every problem is simply a value nobody has filled in yet. */
  get isFreshCheckout(): boolean {
    return this.problems.every((problem) => problem.kind === 'unset');
  }

  /**
   * Operator-facing text. Deliberately not the `Error.message`: that gets a stack
   * trace attached when it surfaces uncaught, which is what made a guard read as a
   * failure.
   */
  format(): string {
    const width = this.problems.reduce((max, p) => Math.max(max, p.path.length), 0);
    const lines: string[] = [
      this.isFreshCheckout
        ? `Configuration for "${this.envName}" is not filled in yet. Nothing was synthesised.`
        : `Configuration for "${this.envName}" did not resolve. Nothing was synthesised.`,
      '',
    ];

    if (this.isFreshCheckout) {
      lines.push(
        'This is expected on a fresh checkout. Every value that differs per deployment',
        'ships as UNSET on purpose: a committed account number or login prefix would aim',
        'your deploy at resources you do not own.',
        '',
      );
    }

    for (const problem of this.problems) {
      const hint =
        problem.kind === 'unset'
          ? (UNSET_HINTS[problem.path] ?? 'no value supplied')
          : 'resolved to localhost, so this was read from the wrong place';
      lines.push(`  ${problem.path.padEnd(width)}   ${hint}`);
    }

    // Stated so a reader knows the list above is complete rather than the first
    // failure. Discovering the next gap on the following synth turns one correction
    // into an afternoon of them. Pointless when there is only one, hence the guard.
    if (this.problems.length > 1) {
      lines.push('', `All ${this.problems.length} are listed above, not just the first.`);
    }

    lines.push(
      '',
      'Values live in infra/lib/config/index.ts.',
      'Deploy order is DEPLOY_ORDER in infra/lib/config/index.ts.',
    );
    return lines.join('\n');
  }
}

function findUnresolved(value: unknown, path: string, found: ConfigProblem[]): void {
  // `NaN` is the typed `UNSET` for a required number; no configured number is legitimately NaN.
  if (typeof value === 'number' && Number.isNaN(value)) {
    found.push({ path, kind: 'unset' });
    return;
  }
  if (typeof value === 'string') {
    if (value === UNSET || value.trim() === '') {
      found.push({ path, kind: 'unset' });
    } else if (/(^|\.)localhost($|:)|127\.0\.0\.1/.test(value)) {
      found.push({ path, kind: 'unresolved' });
    }
    return;
  }
  if (value && typeof value === 'object') {
    for (const [key, nested] of Object.entries(value as Record<string, unknown>)) {
      findUnresolved(nested, path ? `${path}.${key}` : key, found);
    }
  }
}

/** The re-read interval must be shorter than the cache age, or rotation refuses warm traffic. */
function assertOriginVerifyCache(config: AppConfig): void {
  const { originVerifyMaxCacheAgeSeconds: age, originVerifyRereadIntervalSeconds: interval } =
    config.edge;
  if (!(Number.isInteger(interval) && Number.isInteger(age) && interval > 0 && interval < age)) {
    throw new Error(
      'edge.originVerifyRereadIntervalSeconds must be a positive whole number of seconds below ' +
        'edge.originVerifyMaxCacheAgeSeconds.',
    );
  }
}
function assertOnlineEvaluation(config: AppConfig): void {
  const input = config.evaluationInput;
  const evaluation = config.onlineEvaluation;
  const evaluationV4 = config.onlineEvaluationV4;
  if (!evaluation.provisioned) {
    if (evaluationV4.executionRequested) {
      throw new Error(
        'Online evaluation V4 execution requires ADLYTICS_ONLINE_EVALUATION_PROVISIONED=true.',
      );
    }
    return;
  }
  if (!config.transactionSearchEnabled) {
    throw new Error('Online evaluation requires ADLYTICS_TRANSACTION_SEARCH=true.');
  }
  if (!input.provisioned) {
    throw new Error('Online evaluation requires the released-response evaluation input plane.');
  }
  if (evaluation.samplingPercentage < 1) {
    throw new Error('Provisioned online evaluation requires a non-zero whole sampling percentage.');
  }
}

function assertQualityReviewValidationAuth(config: AppConfig): void {
  if (config.auth.qualityReviewValidationPasswordAuthEnabled && config.envName !== 'dev') {
    throw new Error(
      'ADLYTICS_QUALITY_REVIEW_VALIDATION_PASSWORD_AUTH may be enabled only for ENV=dev.',
    );
  }
}

function assertLandingTrust(config: AppConfig): void {
  if (!config.landing.enabled) return;

  const exactRole = /^arn:(aws|aws-us-gov):iam::(\d{12}):role\/[A-Za-z0-9+=,.@_\/-]+$/;
  const match = exactRole.exec(config.landing.pipelineRoleArn);
  if (match === null) {
    throw new Error(
      'ADLYTICS_LANDING_PIPELINE_ROLE_ARN must be one exact IAM role ARN; ' +
        'account root, STS sessions, wildcards, and service principals are refused.',
    );
  }
  if (match[2] === config.account) {
    throw new Error(
      'ADLYTICS_LANDING_PIPELINE_ROLE_ARN must belong to a different AWS account.',
    );
  }
}

export function getConfig(envName: string): AppConfig {
  const config = defaultConfig(envName);

  const problems: ConfigProblem[] = [];
  findUnresolved(config, '', problems);

  // One value is legitimately empty, and only in a stated condition. Listing the
  // condition rather than exempting the key outright keeps the guard honest: if the
  // condition changes, the value becomes required again.
  const legitimatelyEmpty: string[] = [];

  // Host-only cookies carry no Domain attribute at all — required on an
  // AWS-provided hostname, since cloudfront.net is a public suffix.
  if (config.auth.cookieScope === 'host_only') {
    legitimatelyEmpty.push('auth.cookieDomain');
  }

  // Scoped to `unset` deliberately. A key being allowed to be empty never makes it
  // allowed to hold a localhost value.
  const remaining = problems.filter(
    (problem) => !(problem.kind === 'unset' && legitimatelyEmpty.includes(problem.path)),
  );

  if (remaining.length > 0) {
    throw new ConfigurationIncompleteError(envName, remaining);
  }

  assertLandingTrust(config);
  assertOnlineEvaluation(config);
  assertQualityReviewValidationAuth(config);
  assertOriginVerifyCache(config);
  return config;
}
