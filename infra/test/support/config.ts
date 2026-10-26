// SPDX-License-Identifier: MIT-0
/**
 * A complete configuration for stack tests, built here rather than read from
 * lib/config.
 *
 * What the repository ships is deliberately `UNSET`, for every environment name,
 * which `getConfig` refuses — see config.test.ts. Supplying values in a test is what
 * lets the wiring be asserted without committing an account number that would then
 * look authoritative.
 *
 * Shared rather than duplicated per test file because it is the shape of `AppConfig`
 * itself: adding one field to the interface otherwise breaks every stack test
 * separately, which is exactly what happened when `data` was introduced.
 *
 * Both `edge` flags are true here and false as shipped: the fully attached state is the one worth
 * asserting, because it is the only one where the edge actually routes to an origin. A test config
 * with them false would synthesise a distribution with no API and no chat behaviour, and every
 * assertion about either would pass by describing nothing.
 *
 * Not named `*.test.ts`, so jest's `testMatch` does not collect it as a suite.
 */

import * as logs from 'aws-cdk-lib/aws-logs';
import type { AppConfig } from '../../lib/config';
import { ATHENA_START_BUDGET, BAKE_TASKS } from '../../lib/generated/execution-graph-protocol';

export const TEST_CONFIG: AppConfig = {
  envName: 'dev',
  account: '111122223333',
  region: 'eu-west-1',
  edge: {
    apiOriginAttached: true,
    conversationOriginAttached: true,
    wafRateLimitPerFiveMinutes: 2000,
    originVerifyMaxCacheAgeSeconds: 300,
    originVerifyRereadIntervalSeconds: 60,
  },
  data: {
    encryption: 's3_managed',
    rawTransitionAfterDays: 30,
    rawExpireAfterDays: 'never',
    rawExpireNoncurrentVersionsAfterDays: 90,
    abortIncompleteUploadsAfterDays: 7,
    athenaResultsExpireAfterDays: 30,
    stagingExpireAfterDays: 7,
  },
  landing: {
    enabled: true,
    pipelineRoleArn: 'arn:aws:iam::444455556666:role/adlytics-source-pipeline',
    externalId: 'test-deployment-external-id',
  },
  auth: {
    loginDomainPrefix: 'adlytics-dev',
    refreshCookieMaxAgeSeconds: 2592000,
    refreshTokenValidityDays: 30,
    qualityReviewValidationPasswordAuthEnabled: false,
    wafRateLimitPerFiveMinutes: 300,
    cookieScope: 'host_only',
    cookieDomain: '',
  },
  // A real-looking model id rather than the shipped `UNSET`, for the same reason as the account
  // number above: the wiring cannot be asserted without one, and committing a working value in
  // lib/config would name a model a customer's account may not have access to.
  agent: {
    quickChat: {
      modelId: 'test.quick-chat-model-v1:0',
      maxIterations: 8,
      maxTokens: 4096,
      timeoutSeconds: 120,
    },
    deepDive: {
      modelId: 'test.deep-dive-model-v1:0',
      maxIterations: 24,
      maxTokens: 16384,
      timeoutSeconds: 600,
    },
    memoryExpirationDays: 90,
    memoryModelId: 'test.memory-model-v1:0',
  },
  // Deliberately different from the shipped values, so an assertion that a stack reads the config
  // cannot pass by coincidence with a literal in the stack.
  tokenService: {
    clockSkewSeconds: 45,
    publicRouteThrottle: {
      discovery: { rateLimit: 11, burstLimit: 22 },
      jwks: { rateLimit: 13, burstLimit: 26 },
      token: { rateLimit: 17, burstLimit: 34 },
    },
    wafRateLimitPerFiveMinutes: 1500,
  },
  // Deliberately DIFFERENT from the agent values above: the fixture pins that the insight
  // seam and the chat seam are separable (ADR-076 Phase 1), so a stack that accidentally
  // wires an insight Lambda to the agent model reads as a test failure, not a coincidence.
  insight: {
    quickModelId: 'test.insight-quick-model-v1:0',
    deepModelId: 'test.insight-deep-model-v1:0',
  },
  lambdaArchitecture: 'arm64',
  pythonRuntime: 'python3.14',
  // 10 MB, Athena's own floor. It was 1024, which no deploy would have accepted — the
  // workgroup's synth-time check found that, which is the point of checking at synth.
  athenaBytesScannedCutoff: 10 * 1024 * 1024,
  athenaResultReuseMaxAgeMinutes: 60,
  athenaQueryBatchMaxConcurrency: 8,
  athenaStartBudget: {
    bakerStartCap: BAKE_TASKS.certified_query_cap,
    insightStartCap: 4,
    batchAthenaBudget: ATHENA_START_BUDGET.batch_athena_budget,
    chatReserveActiveDml: ATHENA_START_BUDGET.chat_reserve_active_dml,
  },
  // Off, matching what the repository ships. Note the region above is `eu-west-1`, which does not
  // support guardrails in policy — so a test that enables this must also move the region, and that
  // pairing is deliberate: it makes the region gate impossible to leave unexercised.
  gatewayGuardrails: {
    enabled: false,
    enforcement: 'LOG_ONLY',
  },
  evaluationQuality: {
    worstLabel: { periodHours: 6, minimumVerdicts: 5, shareThreshold: 0.2 },
    noDispatch: { periodMinutes: 30, periods: 3 },
  },
  onlineEvaluation: {
    provisioned: false,
    samplingPercentage: 10,
    sessionTimeoutMinutes: 1,
  },
  onlineEvaluationV4: {
    executionRequested: false,
  },
  evaluationInput: {
    provisioned: false,
  },
  ingestion: {
    projectionHandoffState: 'INLINE_ACTIVE',
    stage1RecoveryEnabled: false,
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
  },
  operationalStatus: {
    provisioned: false,
    stage1LifecycleReconcileBatchSize: 10,
    stage1OutboxMaxConsecutiveFailedDeliveryAttempts: 3,
  },
  // The arm dev selects (contracts/operations/alarm-destinations.yaml): the ingestion topic.
  staffedAlarmDestination: { arm: 'ExistingInAccountTopicEmail' },
  asyncFailureBounds: { streamMaxRetryAttempts: 100, streamMaxRecordAgeSeconds: 3600 },
  transactionSearchEnabled: false,
  transactionSearchIndexingPercentage: 5,
  // 1, matching the shipped value: every trace recorded, so an assertion about tracing describes
  // the configuration a deployment actually gets.
  traceSampleRatio: 1,
  // 90, the dev deployment's declared retention. Nothing ships a value (ADR-081 §5); a stack reads
  // this and renders it, so a test asserting 90 is asserting what dev deploys.
  receiptRetentionDays: 90,
  receiptTtlLagMarginDays: 7,
  logRetentionDays: logs.RetentionDays.TWO_WEEKS,
  evaluationResultRetentionDays: logs.RetentionDays.TWO_WEEKS,
  harnessRuntimeLogRetentionDays: logs.RetentionDays.THREE_MONTHS,
  evaluationResultDeadlineSeconds: 1800,
};

/** Top-level overrides. Nested areas are replaced wholesale, never merged. */
export function configWith(overrides: Partial<AppConfig>): AppConfig {
  return { ...TEST_CONFIG, ...overrides };
}
