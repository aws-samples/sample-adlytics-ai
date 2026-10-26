// SPDX-License-Identifier: MIT-0
import * as cdk from 'aws-cdk-lib';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as actions from 'aws-cdk-lib/aws-cloudwatch-actions';
import * as events from 'aws-cdk-lib/aws-events';
import * as targets from 'aws-cdk-lib/aws-events-targets';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';

import {
  AppConfig,
  ProjectionHandoffControls,
  paramPath,
  semanticV2AthenaWorkgroupName,
  semanticV2DatabaseName,
  semanticV2StagingDatabaseName,
  semanticV2StateTableName,
  validateIngestionConfig,
} from '../config';
import { PROVIDER_ATTEMPT_RECONCILER_RESERVED_CONCURRENCY } from '../config/athena-start-budget';
import { STAGING_TABLE_NAMES } from '../constructs/data-lake/staging-table';
import { ROUTES } from '../generated/execution-graph-protocol';
import { dataBucketName } from '../mixins/storage';
import { grantSpanExport, tracingEnvironment } from '../telemetry';
import {
  ExecutionGraphAuthority,
  GraphWorkerName,
  grantDrainSeqWrite,
  grantGraphWorker,
  grantModeItemRead,
} from './execution-graph-authority';
import { buildGraphAlarms, grantBakeContributionRead, grantDueShardRead } from './execution-graph-alarms';
import { arrivalAlarm } from '../constructs/observability/async-failure';

/**
 * The semantic-write queue's maxReceiveCount, read from the generated execution-graph protocol
 * (`routes.semantic_write`, design §2.1). TableWriter's final-receive test reads the same value
 * through `ADL_TABLE_WRITER_MAX_RECEIVE_COUNT`, so the queue and its claimer cannot drift.
 */
const SEMANTIC_WRITE_MAX_RECEIVE_COUNT = ROUTES.semantic_write.max_receive_count;
const TABLE_ATTEMPT_RECONCILE_INDEX = 'table-attempt-reconcile';
const PLAN_INTENT_READY_INDEX = 'stage1-plan-intent-ready';
const OUTBOX_PENDING_INDEX = 'stage1-outbox-pending';

/**
 * The ingestion operations that each own an `Errors` alarm (`alarmingErrorsQuery`); validation
 * probes and dispositions name these alarms. Every other operation is covered by the catch-all
 * `alarming-errors-by-operation` alarm, which excludes exactly these.
 */
export const INGESTION_ERROR_OPERATIONS = [
  // S5: an incomplete sweep or an unreadable shard is an invariant or dependency error.
  'execution_graph.stuck_flow_detector',
  'finalize_registration',
  // S6 (V-19): a delivery refused during the drain on its last allowed receive.
  'ingest.drain_last_receive',
  'ingest.handler',
  'ingest_delivery',
  'projection_handoff.callback',
  'semantic_table_writer',
  'stage1_plan_intent.reconcile',
  'stage1_provider_attempt.reconcile',
  'stage1_provider_fence.recovery',
] as const;

/** The oldest a healthy queue message can be: every receive's visibility, end to end. */
/**
 * §2.1/§2.11 row 1: the dispatcher's delivery, bake and precompute route queues, by the
 * deterministic names their own stacks give them, so no cross-stack import is needed.
 */
export function graphRouteQueueNames(config: AppConfig): readonly [string, string, string] {
  return [
    `adlytics-${config.envName}-ingestion`,
    `adlytics-${config.envName}-preset-bake.fifo`,
    `adlytics-${config.envName}-insight-precompute.fifo`,
  ];
}

export function queueAgeCeilingSeconds(maxReceiveCount: number, visibilitySeconds: number): number {
  return maxReceiveCount * visibilitySeconds;
}

/**
 * Every canonical dataset the contract declares (`contracts/generated/v2-tables.json`
 * `canonicalCandidateTables`, equal to `canonicalCurrentTables`), sorted. Each publication,
 * lease and pointer-event partition key starts `<dataset>|`, so every `dynamodb:LeadingKeys`
 * grant on those partitions is built from this one list. `canonical-dataset-allowlist.test.ts`
 * pins it to the contract, so a new canonical table can't ship without its grants.
 */
export const CANONICAL_DATASET_PREFIXES = [
  'dim_audience_segment',
  'dim_campaign',
  'dim_product',
  'fct_ad_exposure',
  'fct_ad_exposure_withheld',
  'fct_attributed_conversion',
  'fct_attributed_conversion_withheld',
  'fct_deduplicated_temporal_attribution',
  'fct_deduplicated_temporal_attribution_withheld',
  'fct_exposure_frequency',
  'fct_exposure_frequency_withheld',
  'fct_exposure_group_outcome',
  'fct_exposure_group_outcome_withheld',
  'fct_new_to_brand_buyers',
  'fct_product_page_views',
  'fct_product_page_views_reported_total',
  'fct_retail_purchase_amc',
  'fct_retail_purchase_amc_withheld',
  'fct_retail_sales',
  'fct_retail_sales_reported_total',
  'fct_segment_delivery',
  'fct_segment_delivery_withheld',
  'fct_segment_profile',
  'fct_shopper_acquisition',
  'fct_shopper_acquisition_withheld',
] as const;

/** `CANONICAL_DATASET_PREFIXES` as `dynamodb:LeadingKeys` patterns. */
export const CANONICAL_DATASET_KEYS: readonly string[] = CANONICAL_DATASET_PREFIXES.map(
  (dataset) => `${dataset}|*`,
);

export interface IngestionStage1Props {
  readonly config: AppConfig;
  readonly sourceAssetPath: string;
  readonly dependencies: lambda.ILayerVersion;
  readonly controls: ProjectionHandoffControls;
  /** The ingestion alarm topic the semantic-write DLQ alarm announces on (§4). */
  readonly alarmTopic: sns.ITopic;
  /** The delivery function whose ARN the pre-switch verifier resolves and inspects (§2.5). */
  readonly deliveryFunction: lambda.IFunction;
  /** S3: the retained execution-graph table, where the operational plane provisions it. */
  readonly executionGraph?: ExecutionGraphAuthority;
}

type FunctionOptions = Readonly<{
  id: string;
  handler: string;
  memorySize: number;
  timeoutSeconds: number;
  reservedConcurrency: number;
  environment: Record<string, string>;
}>;

/**
 * The write-discipline §11 `IngestionConfig` rendered as the one runtime environment every Stage-1
 * consumer resolves: the delivery Lambda, the table writer, both reconcilers, the fence recovery
 * broker, and the handoff provider. One renderer, so the values reach every consumer byte-identically
 * and a container whose accessor demands the full object never starts against a partial one.
 */
export function stage1LimitsEnvironment(config: AppConfig): Record<string, string> {
  const value = config.ingestion;
  return {
    ADL_PROJECTION_HANDOFF_STATE: value.projectionHandoffState,
    ADL_STAGE1_MAX_UNITS: String(value.maxUnits),
    ADL_STAGE1_MAX_ACCEPTED_ATTEMPTS_PER_UNIT: String(value.maxAcceptedAttemptsPerUnit),
    ADL_STAGE1_MAX_ACCEPTED_ATTEMPTS_PER_PLAN: String(value.maxAcceptedAttemptsPerPlan),
    ADL_STAGE1_MAX_GROUP_MEMBERS: String(value.maxGroupMembers),
    ADL_STAGE1_MAX_POINTERS_PER_TABLE: String(value.maxPointersPerTable),
    ADL_STAGE1_MAX_OBJECT_REFS_PER_UNIT: String(value.maxObjectRefsPerUnit),
    ADL_STAGE1_MAX_ROWS_PER_UNIT: String(value.maxRowsPerUnit),
    ADL_STAGE1_MAX_ROWS_PER_DELIVERY: String(value.maxRowsPerDelivery),
    ADL_STAGE1_ATHENA_MAX_STATEMENT_BYTES: String(value.athenaMaxStatementBytes),
    ADL_STAGE1_MAX_ENCODED_IDENTITY_BYTES: String(value.maxEncodedIdentityBytes),
    ADL_STAGE1_MAX_PARTITION_IDENTITY_BYTES: String(value.maxPartitionIdentityBytes),
    ADL_TABLE_WRITER_TIMEOUT_SECONDS: String(value.tableWriterTimeoutSeconds),
    ADL_TABLE_WRITER_CLEANUP_RESERVE_MS: String(value.tableWriterCleanupReserveMs),
    ADL_TABLE_WRITER_CONCURRENCY: String(value.tableWriterConcurrency),
    ADL_TABLE_WRITER_MAX_RECEIVE_COUNT: String(SEMANTIC_WRITE_MAX_RECEIVE_COUNT),
    ADL_TABLE_LEASE_DURATION_SECONDS: String(value.tableLeaseDurationSeconds),
    ADL_TABLE_WRITER_VISIBILITY_MARGIN_SECONDS: String(
      value.semanticWriteVisibilityMarginSeconds,
    ),
    ADL_TABLE_ATTEMPT_RECONCILE_BATCH_SIZE: String(value.tableAttemptReconcileBatchSize),
    ADL_TABLE_ATTEMPT_RECONCILE_BASE_BACKOFF_SECONDS: String(
      value.tableAttemptReconcileBaseBackoffSeconds,
    ),
    ADL_TABLE_ATTEMPT_RECONCILE_MAX_BACKOFF_SECONDS: String(
      value.tableAttemptReconcileMaxBackoffSeconds,
    ),
    ADL_PLAN_INTENT_RECONCILE_BATCH_SIZE: String(value.planIntentReconcileBatchSize),
    ADL_PLAN_INTENT_RECONCILE_BASE_BACKOFF_SECONDS: String(
      value.planIntentReconcileBaseBackoffSeconds,
    ),
    ADL_PLAN_INTENT_RECONCILE_MAX_BACKOFF_SECONDS: String(
      value.planIntentReconcileMaxBackoffSeconds,
    ),
    ADL_PROJECTION_HANDOFF_DRAIN_OBSERVATION_SECONDS: String(
      value.drainObservationIntervalSeconds,
    ),
  };
}

/**
 * ADR-149 (ING-IAM): the staging grants of one principal, written into its own
 * `<Principal>StagingPolicy` so no existing policy's size moves.
 *
 * `write: true` adds `s3:PutObject` (the stager; a 412 HEAD needs only `s3:GetObject`). Athena
 * reads staged rows with the caller's credentials, so every principal that starts or replays a
 * MERGE or verification also lists `<table>/<key>/` (conditioned on exactly the 54 table prefixes),
 * resolves the bucket region and plans the staging table. No `s3:DeleteObject` (the lifecycle
 * expires objects), no KMS (SSE-S3), no workgroup and no Glue write. No reader role calls this.
 */
export function grantStagingStorage(
  config: AppConfig,
  principal: iam.IGrantable,
  options: { readonly write: boolean },
  into: iam.ManagedPolicy,
): void {
  const stack = cdk.Stack.of(into);
  const bucket = `arn:${stack.partition}:s3:::${dataBucketName(config, 'v2-staging')}`;
  const database = semanticV2StagingDatabaseName(config.envName);
  const glueArn = (resource: string): string =>
    `arn:${stack.partition}:glue:${stack.region}:${stack.account}:${resource}`;
  into.addStatements(
    new iam.PolicyStatement({
      actions: options.write ? ['s3:PutObject', 's3:GetObject'] : ['s3:GetObject'],
      resources: [`${bucket}/*`],
    }),
    new iam.PolicyStatement({
      actions: ['s3:ListBucket'],
      resources: [bucket],
      conditions: {
        StringLike: { 's3:prefix': STAGING_TABLE_NAMES.map((table) => `${table}/*`) },
      },
    }),
    new iam.PolicyStatement({ actions: ['s3:GetBucketLocation'], resources: [bucket] }),
    new iam.PolicyStatement({
      actions: ['glue:GetDatabase', 'glue:GetTable'],
      resources: [glueArn('catalog'), glueArn(`database/${database}`), glueArn(`table/${database}/*`)],
    }),
  );
  const role = principal.grantPrincipal;
  if (!(role instanceof iam.Role)) {
    throw new Error('grantStagingStorage attaches its policy to the principal\'s own role.');
  }
  into.attachToRole(role);
}

/**
 * ADR-149: the staging bucket and database, for the two runtimes that stage and verify (the
 * TableWriter and the delivery function). Every other function resolves `Settings` with both
 * blank, so none of them gains a name it has no grant for.
 */
export function stagingEnvironment(config: AppConfig): Record<string, string> {
  return {
    ADL_SEMANTIC_V2_STAGING_BUCKET: dataBucketName(config, 'v2-staging'),
    ADL_SEMANTIC_V2_STAGING_DATABASE: semanticV2StagingDatabaseName(config.envName),
  };
}

/** The handoff custom resources and the provider subtree `orderHandoff` excludes. */
interface HandoffResources {
  readonly prepare: cdk.CfnResource;
  readonly commit: cdk.CfnResource;
  readonly guard: cdk.CfnResource;
  readonly providerRoot: Construct;
}

/** ADR-075 destroyable Stage-1 execution, recovery, and handoff foundations. */
export class IngestionStage1 extends Construct {
  public readonly semanticWriteQueue: sqs.Queue;
  public readonly semanticWriteDeadLetterQueue: sqs.Queue;
  public readonly tableWriter: lambda.Function;
  public readonly providerAttemptReconciler: lambda.Function;
  public readonly planIntentReconciler: lambda.Function;
  public readonly providerFenceRecoveryFunction: lambda.Function;
  public readonly semanticProjectionOperatorRole: iam.Role;
  public readonly providerFenceRecoveryOperatorRole: iam.Role;
  public readonly stage1PreswitchVerificationRole: iam.Role;
  /** S3: the graph-authority workers, present only where the graph store is provisioned. */
  public readonly graphWorkers: Partial<Record<GraphWorkerName, lambda.Function>> = {};
  /** S5: the stuck-flow detector, present only where the graph store is provisioned. */
  public stuckFlowDetector?: lambda.Function;

  private readonly stack: cdk.Stack;
  private readonly handoff: HandoffResources;

  constructor(scope: Construct, id: string, props: IngestionStage1Props) {
    super(scope, id);
    this.stack = cdk.Stack.of(this);
    validateIngestionConfig(props.config.ingestion);

    const { config, controls } = props;
    const ingestion = config.ingestion;
    const publications = semanticV2StateTableName(config.envName, 'publications');
    const leases = semanticV2StateTableName(config.envName, 'leases');
    const attempts = semanticV2StateTableName(config.envName, 'attempts');
    const publicationsArn = this.tableArn(publications);
    const leasesArn = this.tableArn(leases);
    const attemptsArn = this.tableArn(attempts);
    const pendingIndexArn = `${publicationsArn}/index/${TABLE_ATTEMPT_RECONCILE_INDEX}`;
    const planIntentIndexArn = `${publicationsArn}/index/${PLAN_INTENT_READY_INDEX}`;

    this.semanticWriteDeadLetterQueue = new sqs.Queue(this, 'SemanticWriteDeadLetterQueue', {
      queueName: `adlytics-${config.envName}-semantic-write-dlq.fifo`,
      fifo: true,
      contentBasedDeduplication: false,
      encryption: sqs.QueueEncryption.SQS_MANAGED,
      enforceSSL: true,
      retentionPeriod: cdk.Duration.days(14),
    });
    this.semanticWriteQueue = new sqs.Queue(this, 'SemanticWriteQueue', {
      queueName: `adlytics-${config.envName}-semantic-write.fifo`,
      fifo: true,
      contentBasedDeduplication: false,
      encryption: sqs.QueueEncryption.SQS_MANAGED,
      enforceSSL: true,
      retentionPeriod: cdk.Duration.days(14),
      visibilityTimeout: cdk.Duration.seconds(
        ingestion.tableWriterTimeoutSeconds + ingestion.semanticWriteVisibilityMarginSeconds,
      ),
      deadLetterQueue: {
        queue: this.semanticWriteDeadLetterQueue,
        maxReceiveCount: SEMANTIC_WRITE_MAX_RECEIVE_COUNT,
      },
    });

    this.tableWriter = this.functionWithLogs(props, {
      id: 'TableWriter',
      handler: 'adapters.inbound.ingestion_handlers.table_writer.handler',
      memorySize: ingestion.tableWriterMemoryMiB,
      timeoutSeconds: ingestion.tableWriterTimeoutSeconds,
      reservedConcurrency: ingestion.tableWriterConcurrency,
      environment: {
        ...this.semanticEnvironment(config),
        ...stagingEnvironment(config),
        ...this.stage1Limits(config),
        ADL_SEMANTIC_WRITE_QUEUE_URL: this.semanticWriteQueue.queueUrl,
        ...this.graphEnvironment(props),
      },
    });
    // TableWriter's S3, Athena and Glue grants live in one customer-managed policy on its role
    // only: with the graph grants (§2.11 row 2) its default policy passes CDK's own estimate and
    // spills into an unnamed overflow policy, which `test/iam-policy-size.test.ts` refuses. The
    // statements are unchanged; only their policy moved, and the function waits for it.
    const writerRole = this.tableWriter.role;
    if (writerRole === undefined) {
      throw new Error('TableWriter must own an execution role.');
    }
    const writerStorage = new iam.ManagedPolicy(this, 'TableWriterStoragePolicy', {
      description: 'Semantic V2 objects, batch results, query workgroup and catalog for TableWriter.',
      roles: [writerRole],
    });
    this.grantSemanticStorage(config, this.tableWriter, writerStorage);
    this.tableWriter.node.addDependency(writerStorage);
    const writerStaging = new iam.ManagedPolicy(this, 'TableWriterStagingPolicy', {
      description: 'ADR-149 staged table units: write once, read, list and plan for TableWriter.',
    });
    grantStagingStorage(config, this.tableWriter, { write: true }, writerStaging);
    this.tableWriter.node.addDependency(writerStaging);
    this.addDirectReads(this.tableWriter, publicationsArn, ['TABLE_ATTEMPT#*', 'WRITE_PLAN#*', 'STATUS_FLOW#*', ...this.datasetKeys()]);
    this.addDirectReads(this.tableWriter, leasesArn, ['TABLE#*']);
    this.addFlowRootClockQuery(this.tableWriter, publicationsArn);
    this.addPlanPartitionQuery(this.tableWriter, publicationsArn);
    this.addDirectAttemptMutation(this.tableWriter, publicationsArn);
    this.addTransactionWrites(this.tableWriter, publicationsArn, ['TABLE_ATTEMPT#*', 'WRITE_PLAN#*', 'STATUS_FLOW#*', ...this.datasetKeys()]);
    this.addTransactionWrites(this.tableWriter, leasesArn, ['TABLE#*']);
    this.addAttemptTableAccess(this.tableWriter, attemptsArn);
    this.addQueueConsumer(this.tableWriter, this.semanticWriteQueue.queueArn);

    const writerMapping = new lambda.CfnEventSourceMapping(this, 'SemanticWriteMapping', {
      functionName: this.tableWriter.functionName,
      eventSourceArn: this.semanticWriteQueue.queueArn,
      enabled: controls.semanticWriterMappingEnabled,
      batchSize: 1,
      scalingConfig: { maximumConcurrency: ingestion.tableWriterConcurrency },
      // The writer returns a retryable unit in batchItemFailures; without this Lambda deletes it.
      functionResponseTypes: ['ReportBatchItemFailures'],
    });
    writerMapping.addResourceDependency(
      this.semanticWriteQueue.node.defaultChild as cdk.CfnResource,
    );

    this.providerAttemptReconciler = this.functionWithLogs(props, {
      id: 'ProviderAttemptReconciler',
      handler: 'adapters.jobs.provider_attempt_reconciler.handler',
      memorySize: 512,
      timeoutSeconds: 120,
      reservedConcurrency: PROVIDER_ATTEMPT_RECONCILER_RESERVED_CONCURRENCY,
      environment: {
        ...this.semanticEnvironment(config),
        ...this.stage1Limits(config),
        ADL_TABLE_ATTEMPT_RECONCILE_BATCH_SIZE: String(ingestion.tableAttemptReconcileBatchSize),
        ADL_TABLE_ATTEMPT_RECONCILE_BASE_BACKOFF_SECONDS: String(
          ingestion.tableAttemptReconcileBaseBackoffSeconds,
        ),
        ADL_TABLE_ATTEMPT_RECONCILE_MAX_BACKOFF_SECONDS: String(
          ingestion.tableAttemptReconcileMaxBackoffSeconds,
        ),
        ADL_TABLE_LEASE_DURATION_SECONDS: String(ingestion.tableLeaseDurationSeconds),
      },
    });
    this.addIndexQuery(this.providerAttemptReconciler, pendingIndexArn, 'pending');
    // §5: strong reads of FENCE/ATTEMPT/RUN#, the queued owner (unit, plan META, origin intent),
    // the inline run head, and the lease; no protected-case read or blocked-partition query.
    this.addDirectReads(this.providerAttemptReconciler, publicationsArn, [
      'TABLE_ATTEMPT#*',
      'WRITE_PLAN#*',
      'STATUS_FLOW#*',
    ]);
    this.addDirectReads(this.providerAttemptReconciler, leasesArn, ['TABLE#*']);
    this.addFlowRootClockQuery(this.providerAttemptReconciler, publicationsArn);
    this.addTransactionActions(
      this.providerAttemptReconciler,
      publicationsArn,
      ['TABLE_ATTEMPT#*', 'WRITE_PLAN#*'],
      ['dynamodb:UpdateItem', 'dynamodb:ConditionCheckItem'],
    );
    // The inline reconciled event and head advance ride the completion transaction.
    this.addTransactionWrites(this.providerAttemptReconciler, publicationsArn, ['STATUS_FLOW#*']);
    this.addTransactionActions(
      this.providerAttemptReconciler,
      publicationsArn,
      ['PROVIDER_RECONCILE_BLOCK#*'],
      ['dynamodb:PutItem'],
    );
    this.addTransactionWrites(this.providerAttemptReconciler, leasesArn, ['TABLE#*']);
    this.addAttemptTableAccess(this.providerAttemptReconciler, attemptsArn);
    // Parked-submission recovery replays the immutable start envelope: Start plus the statement
    // replay's Glue/S3 grants, on the same typed resources as the table writer (§5).
    this.grantSemanticStorage(config, this.providerAttemptReconciler);
    this.grantReplayPersistedBatchEnvelopes(config, this.providerAttemptReconciler);
    // ADR-149: a replayed MERGE or verification reads staged rows; the reconciler never stages.
    const reconcilerStaging = new iam.ManagedPolicy(this, 'ProviderAttemptReconcilerStagingPolicy', {
      description: 'ADR-149 staged table units: read, list and plan for a replayed statement.',
    });
    grantStagingStorage(config, this.providerAttemptReconciler, { write: false }, reconcilerStaging);
    this.providerAttemptReconciler.node.addDependency(reconcilerStaging);

    this.planIntentReconciler = this.functionWithLogs(props, {
      id: 'PlanIntentReconciler',
      handler: 'adapters.jobs.plan_intent_reconciler.handler',
      memorySize: 512,
      timeoutSeconds: 120,
      reservedConcurrency: 1,
      environment: {
        ...this.semanticEnvironment(config),
        ...this.stage1Limits(config),
        ADL_SEMANTIC_WRITE_QUEUE_URL: this.semanticWriteQueue.queueUrl,
        ADL_STAGE1_MAX_UNITS: String(ingestion.maxUnits),
        ADL_PLAN_INTENT_RECONCILE_BATCH_SIZE: String(ingestion.planIntentReconcileBatchSize),
        ADL_PLAN_INTENT_RECONCILE_BASE_BACKOFF_SECONDS: String(
          ingestion.planIntentReconcileBaseBackoffSeconds,
        ),
        ADL_PLAN_INTENT_RECONCILE_MAX_BACKOFF_SECONDS: String(
          ingestion.planIntentReconcileMaxBackoffSeconds,
        ),
        // S6 (V-17b): the reconciler's one strong mode read per invocation.
        ...this.graphEnvironment(props),
      },
    });
    this.addIndexQuery(this.planIntentReconciler, planIntentIndexArn, 'ready');
    this.addDirectReads(this.planIntentReconciler, publicationsArn, ['WRITE_PLAN#*', 'STATUS_FLOW#*']);
    this.addFlowRootClockQuery(this.planIntentReconciler, publicationsArn);
    this.addPlanPartitionQuery(this.planIntentReconciler, publicationsArn);
    this.addTransactionReads(this.planIntentReconciler, publicationsArn, ['WRITE_PLAN#*', 'STATUS_FLOW#*']);
    // S6 legacy disablement (design.md, "Legacy cutover", step 6): the same typed boolean that
    // renders the reconciler's schedule renders its coordinator write actions, so both stay
    // through the drain (it finishes pre-CAS work) and both go only in P6(d')'s own commit.
    if (ingestion.legacyPlanIntentReconcileEnabled) {
      this.addTransactionWrites(this.planIntentReconciler, publicationsArn, ['WRITE_PLAN#*', 'STATUS_FLOW#*']);
      if (props.executionGraph !== undefined) {
        grantDrainSeqWrite(this.planIntentReconciler, publicationsArn);
      }
    }
    this.addQueueSender(this.planIntentReconciler, this.semanticWriteQueue.queueArn);
    if (props.executionGraph !== undefined) {
      // S3 item 7: TableWriter's per-batch mode read and its V-13(a) finalize check; the
      // reconciler's mode read (its S6 drain-wait). Read-class actions on the mode item only.
      // S6 part 2 (§2.11 row 2): TableWriter's graph-unit and `publish` claims, settlements and
      // fences ride the same grant as the workers', in the TableWriter family.
      grantGraphWorker(this.tableWriter, props.executionGraph, 'TableWriter', {
        modeConditionCheck: true,
      });
      this.grantGraphPublish(this.tableWriter, publicationsArn);
      grantModeItemRead(this.planIntentReconciler, props.executionGraph, false);
      // S6 (V-17b): TableWriter's legacy path counts on DRAIN_SEQ after a draining read.
      grantDrainSeqWrite(this.tableWriter, publicationsArn);
      this.buildGraphWorkers(props, props.executionGraph, publicationsArn);
      this.buildStuckFlowDetector(props, props.executionGraph);
    }

    this.providerFenceRecoveryFunction = this.functionWithLogs(props, {
      id: 'ProviderFenceRecoveryFunction',
      handler: 'adapters.jobs.provider_fence_recovery.handler',
      memorySize: 512,
      timeoutSeconds: 60,
      reservedConcurrency: 1,
      environment: {
        ...this.semanticEnvironment(config),
        ...this.stage1Limits(config),
      },
    });
    this.addIndexQuery(this.providerFenceRecoveryFunction, pendingIndexArn, 'blocked');
    // The private requeue row condition-checks the exact bound owner: a queued owner is the
    // plan unit under WRITE_PLAN#, an inline owner the RUN# under TABLE_ATTEMPT# (§3.2).
    this.addDirectReads(this.providerFenceRecoveryFunction, publicationsArn, [
      'PROVIDER_RECONCILE_BLOCK#*',
      'TABLE_ATTEMPT#*',
      'WRITE_PLAN#*',
    ]);
    this.addTransactionActions(
      this.providerFenceRecoveryFunction,
      publicationsArn,
      ['PROVIDER_RECONCILE_BLOCK#*', 'TABLE_ATTEMPT#*', 'WRITE_PLAN#*'],
      ['dynamodb:UpdateItem', 'dynamodb:ConditionCheckItem'],
    );

    // §2.5: the two reconciler schedules render only the typed `stage1RecoveryEnabled` boolean;
    // the handoff composition never gates them.
    const schedulesEnabled = ingestion.stage1RecoveryEnabled;
    for (const [ruleId, fn, enabled] of [
      ['ProviderAttemptReconcilerSchedule', this.providerAttemptReconciler, schedulesEnabled],
      // S6: only this rule also reads the legacy-disablement boolean (step 6).
      [
        'PlanIntentReconcilerSchedule',
        this.planIntentReconciler,
        schedulesEnabled && ingestion.legacyPlanIntentReconcileEnabled,
      ],
    ] as const) {
      new events.Rule(this, ruleId, {
        ruleName: `adlytics-${config.envName}-${ruleId}`,
        schedule: events.Schedule.rate(cdk.Duration.minutes(1)),
        enabled,
        targets: [new targets.LambdaFunction(fn)],
      });
    }

    this.providerFenceRecoveryOperatorRole = this.invokeOnlyRole(
      'ProviderFenceRecoveryOperatorRole',
      this.providerFenceRecoveryFunction,
    );
    this.semanticProjectionOperatorRole = this.buildSemanticProjectionOperatorRole(
      config,
      publicationsArn,
      leasesArn,
      attemptsArn,
    );
    this.stage1PreswitchVerificationRole = this.buildPreswitchVerificationRole(
      publicationsArn,
      props.deliveryFunction,
    );
    this.buildSemanticWriteDlqAlarm(config, props.alarmTopic);
    this.buildSemanticWriteAgeAlarm(config, props.alarmTopic);

    this.publishParameters(config, props.deliveryFunction);
    this.handoff = this.buildHandoffResources(props, controls, publicationsArn);
  }

  /**
   * Orders every destroyable application resource in the stack between prepare and commit/guard.
   * The stack calls it last, after every resource it adds (including the function alarms), because
   * a resource created afterwards would escape the ordering.
   */
  public orderHandoff(): void {
    const { prepare, commit, guard, providerRoot } = this.handoff;
    const excluded = new Set<cdk.CfnResource>([
      prepare,
      commit,
      guard,
      ...providerRoot.node.findAll().filter((node): node is cdk.CfnResource => node instanceof cdk.CfnResource),
    ]);
    const applicationResources = this.stack.node.findAll().filter(
      (node): node is cdk.CfnResource => node instanceof cdk.CfnResource && !excluded.has(node),
    );
    for (const resource of applicationResources) {
      resource.addResourceDependency(prepare);
      commit.addResourceDependency(resource);
      guard.addResourceDependency(resource);
    }
    guard.addResourceDependency(commit);
  }

  /**
   * §2.5: the sole named exception to the no-`Scan` rule. Read-only over the exact publications
   * table and its three Stage-1 GSIs, the two semantic-write queue ARNs, and the delivery
   * function's concurrency; no runtime principal may assume it.
   */
  private buildPreswitchVerificationRole(
    publicationsArn: string,
    deliveryFunction: lambda.IFunction,
  ): iam.Role {
    const role = new iam.Role(this, 'Stage1PreswitchVerificationRole', {
      assumedBy: new iam.AccountPrincipal(this.stack.account).withConditions({
        StringEquals: { 'aws:PrincipalAccount': this.stack.account },
      }),
      description: 'Same-account read-only pre-switch verifier for the Stage-1 fence (§2.5).',
      // The operator assumes it for exactly 900 s (§2.5); IAM's minimum max-session is one hour.
    });
    role.addToPolicy(new iam.PolicyStatement({
      actions: ['dynamodb:Scan', 'dynamodb:GetItem', 'dynamodb:DescribeTable'],
      resources: [publicationsArn],
    }));
    role.addToPolicy(new iam.PolicyStatement({
      actions: ['dynamodb:Query'],
      resources: [
        `${publicationsArn}/index/${TABLE_ATTEMPT_RECONCILE_INDEX}`,
        `${publicationsArn}/index/${PLAN_INTENT_READY_INDEX}`,
        `${publicationsArn}/index/${OUTBOX_PENDING_INDEX}`,
      ],
    }));
    role.addToPolicy(new iam.PolicyStatement({
      actions: ['sqs:GetQueueAttributes'],
      resources: [this.semanticWriteQueue.queueArn, this.semanticWriteDeadLetterQueue.queueArn],
    }));
    role.addToPolicy(new iam.PolicyStatement({
      actions: ['lambda:GetFunctionConcurrency', 'lambda:GetFunction'],
      resources: [deliveryFunction.functionArn],
    }));
    return role;
  }

  /**
   * A unit message older than its whole retry envelope (every receive's visibility) is stuck,
   * not retrying: tracing report I2, 2026-10-06. The threshold is derived, never a literal.
   */
  private buildSemanticWriteAgeAlarm(config: AppConfig, topic: sns.ITopic): void {
    const ingestion = config.ingestion;
    const visibility = ingestion.tableWriterTimeoutSeconds + ingestion.semanticWriteVisibilityMarginSeconds;
    const alarm = new cloudwatch.Alarm(this, 'SemanticWriteOldest', {
      alarmName: `adlytics-${config.envName}-semantic-write-oldest`,
      alarmDescription:
        'A semantic-write unit message is older than every receive its queue allows, so it is '
        + 'stuck rather than retrying. Inspect the plan intent and the unit before redriving.',
      metric: this.semanticWriteQueue.metricApproximateAgeOfOldestMessage({
        period: cdk.Duration.minutes(1),
        statistic: cloudwatch.Stats.MAXIMUM,
      }),
      threshold: queueAgeCeilingSeconds(SEMANTIC_WRITE_MAX_RECEIVE_COUNT, visibility),
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      evaluationPeriods: 1,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    alarm.addAlarmAction(new actions.SnsAction(topic));
  }

  /** §4: an exhausted unit message is an operator redrive signal, never silent. */
  private buildSemanticWriteDlqAlarm(config: AppConfig, topic: sns.ITopic): void {
    arrivalAlarm(this, 'SemanticWriteDeadLettered', {
      alarmName: `adlytics-${config.envName}-semantic-write-dead-lettered`,
      alarmDescription:
        'A semantic-write unit message exhausted its three receives. The unit stays recoverable '
        + 'from its retained plan intent; redrive after the table-attempt authority is inspected.',
      queue: this.semanticWriteDeadLetterQueue,
      action: new actions.SnsAction(topic),
    });
  }

  private functionWithLogs(
    props: IngestionStage1Props,
    options: FunctionOptions,
  ): lambda.Function {
    const functionName = `adlytics-${props.config.envName}-${options.id}`;
    const logGroup = new logs.LogGroup(this, `${options.id}Logs`, {
      logGroupName: `/aws/lambda/${functionName}`,
      retention: props.config.logRetentionDays,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    const role = new iam.Role(this, `${options.id}ExecutionRole`, {
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      description: `Least-privilege execution role for ${functionName}.`,
    });
    role.addToPolicy(new iam.PolicyStatement({
      actions: ['logs:CreateLogStream', 'logs:PutLogEvents'],
      resources: [`${logGroup.logGroupArn}:*`],
    }));
    const fn = new lambda.Function(this, options.id, {
      functionName,
      runtime: new lambda.Runtime(props.config.pythonRuntime, lambda.RuntimeFamily.PYTHON),
      architecture: { arm64: lambda.Architecture.ARM_64 }[props.config.lambdaArchitecture],
      code: lambda.Code.fromAsset(props.sourceAssetPath),
      handler: options.handler,
      layers: [props.dependencies],
      environment: options.environment,
      memorySize: options.memorySize,
      timeout: cdk.Duration.seconds(options.timeoutSeconds),
      reservedConcurrentExecutions: options.reservedConcurrency,
      role,
      logGroup,
    });
    grantSpanExport(fn);
    return fn;
  }

  private semanticEnvironment(config: AppConfig): Record<string, string> {
    return {
      ...tracingEnvironment(config),
      ADL_ENVIRONMENT: config.envName,
      ADL_AWS_REGION: config.region,
      ADL_COGNITO_USER_POOL_ID: 'unused-by-stage1',
      ADL_COGNITO_CLIENT_ID: 'unused-by-stage1',
      ADL_COGNITO_LOGIN_DOMAIN: `auth.${config.envName}.invalid`,
      ADL_APP_BASE_URL: `https://app.${config.envName}.invalid`,
      ADL_API_BASE_URL: `https://api.${config.envName}.invalid`,
      ADL_COOKIE_SCOPE: config.auth.cookieScope,
      ADL_COOKIE_DOMAIN: config.auth.cookieDomain,
      ADL_REFRESH_COOKIE_MAX_AGE_SECONDS: String(config.auth.refreshCookieMaxAgeSeconds),
      ADL_RAW_BUCKET: dataBucketName(config, 'semantic-v2-raw'),
      ADL_ATHENA_WORKGROUP: semanticV2AthenaWorkgroupName(config.envName, 'interactive'),
      ADL_ATHENA_RESULT_REUSE_MAX_AGE_MINUTES: '0',
      ADL_ATHENA_QUERY_BATCH_MAX_CONCURRENCY: String(config.athenaQueryBatchMaxConcurrency),
      ADL_SEMANTIC_ATTEMPTS_TABLE: semanticV2StateTableName(config.envName, 'attempts'),
      ADL_SEMANTIC_LEASES_TABLE: semanticV2StateTableName(config.envName, 'leases'),
      ADL_SEMANTIC_PUBLICATIONS_TABLE: semanticV2StateTableName(config.envName, 'publications'),
      ADL_SEMANTIC_V2_BUCKET: dataBucketName(config, 'semantic-v2'),
      ADL_SEMANTIC_V2_ATHENA_BAKE_WORKGROUP: semanticV2AthenaWorkgroupName(config.envName, 'bake'),
      ADL_SEMANTIC_V2_SOURCE_DATABASE: semanticV2DatabaseName(config.envName, 'source-aligned'),
      ADL_SEMANTIC_V2_CANDIDATE_DATABASE: semanticV2DatabaseName(config.envName, 'candidate'),
      ADL_SEMANTIC_V2_CURRENT_DATABASE: semanticV2DatabaseName(config.envName, 'current'),
    };
  }

  private graphEnvironment(props: IngestionStage1Props): Record<string, string> {
    const graph = props.executionGraph;
    return graph === undefined ? {} : { ADL_EXECUTION_GRAPH_TABLE: graph.tableName };
  }

  /**
   * S3: the four graph-authority workers (dispatcher, fence settler, promoter, node verifier),
   * one function and one least-privilege role each. S3 deployed their one-minute schedules
   * disabled; S6 part 2 enables them (§2.11 row 9). Each handler reads the mode first and does
   * nothing outside GRAPH, so an invocation in any other mode sends and writes nothing.
   */
  private buildGraphWorkers(
    props: IngestionStage1Props,
    graph: ExecutionGraphAuthority,
    publicationsArn: string,
  ): void {
    const { config } = props;
    const workers: readonly [GraphWorkerName, string][] = [
      ['Dispatcher', 'dispatcher_handler'],
      ['FenceSettler', 'fence_settler_handler'],
      ['Promoter', 'promoter_handler'],
      ['NodeVerifier', 'node_verifier_handler'],
      ['Release', 'release_handler'],
    ];
    for (const [name, handler] of workers) {
      const fn = this.functionWithLogs(props, {
        id: `ExecutionGraph${name}`,
        handler: `adapters.jobs.execution_graph.workers.${handler}`,
        // Strong reads and small transactions over at most sixteen due shards; 512 MiB keeps
        // the Python cold start short, and one minute bounds a pass below its schedule.
        memorySize: 512,
        timeoutSeconds: 60,
        reservedConcurrency: 1,
        environment: {
          ...this.semanticEnvironment(config),
          ...this.stage1Limits(config),
          ADL_SEMANTIC_WRITE_QUEUE_URL: this.semanticWriteQueue.queueUrl,
          ADL_EXECUTION_GRAPH_TABLE: graph.tableName,
          ...(name === 'Dispatcher' ? this.graphRouteQueueUrls(config) : {}),
        },
      });
      grantGraphWorker(fn, graph, name);
      if (name === 'Dispatcher') {
        // The only graph sender: exact persisted bytes to the semantic write FIFO.
        this.addQueueSender(fn, this.semanticWriteQueue.queueArn);
        // §2.11 row 1: the other three routes, by the deterministic names the env carries.
        fn.addToRolePolicy(new iam.PolicyStatement({
          sid: 'SendGraphRouteEnvelopes',
          actions: ['sqs:SendMessage'],
          resources: graphRouteQueueNames(config).map(
            (queueName) => `arn:${this.stack.partition}:sqs:${this.stack.region}:${this.stack.account}:${queueName}`,
          ),
        }));
      }
      if (name === 'NodeVerifier') {
        // Re-attests the node's table attempt under the existing provider fence.
        this.addDirectReads(fn, publicationsArn, ['TABLE_ATTEMPT#*']);
      }
      if (name === 'Release') {
        // S4 (BF-8): the legacy head reads the window's retrievals and their exact versions in
        // the raw zone, and each newer retrieval's successor owner and plan META, strongly.
        this.addDirectReads(fn, publicationsArn, ['WRITE_PLAN#*']);
        const raw = dataBucketName(config, 'semantic-v2-raw');
        fn.addToRolePolicy(new iam.PolicyStatement({
          sid: 'ListRawRetrievalsForLegacyHead',
          actions: ['s3:ListBucket', 's3:ListBucketVersions'],
          resources: [`arn:${this.stack.partition}:s3:::${raw}`],
        }));
      }
      // §2.11 row 9: enabled. Each handler reads the mode first and does nothing outside GRAPH
      // (`workers.py`), so an enabled schedule in SHADOW sends and writes nothing.
      new events.Rule(this, `ExecutionGraph${name}Schedule`, {
        ruleName: `adlytics-${config.envName}-ExecutionGraph${name}Schedule`,
        schedule: events.Schedule.rate(cdk.Duration.minutes(1)),
        enabled: true,
        targets: [new targets.LambdaFunction(fn)],
      });
      this.graphWorkers[name] = fn;
    }
  }

  /**
   * S6 §2.1 (plan B2): the dispatcher's other route queues, by their deterministic names, so the
   * routing code never reads an unset variable. `delivery` is the ingestion stack's standard
   * queue, `bake` the Publisher's FIFO, and `precompute` InsightPrecompute's FIFO (slice 5i).
   * Their send grant is §2.11 row 1 (`SendGraphRouteEnvelopes`), over the same names.
   */
  private graphRouteQueueUrls(config: AppConfig): Record<string, string> {
    const url = (queueName: string): string =>
      `https://sqs.${this.stack.region}.${this.stack.urlSuffix}/${this.stack.account}/${queueName}`;
    const [delivery, bake, precompute] = graphRouteQueueNames(config);
    return {
      ADL_GRAPH_DELIVERY_QUEUE_URL: url(delivery),
      ADL_GRAPH_BAKE_QUEUE_URL: url(bake),
      ADL_GRAPH_PRECOMPUTE_QUEUE_URL: url(precompute),
    };
  }

  /**
   * §2.11 row 2b: `publish` on the publications table. Staging and the fold are direct
   * `UpdateItem`s on the dataset keys (no `EnclosingOperation`); the flip updates
   * `GENERATION#<U>` inside its `TransactWriteItems`; the pointer pre-read and the
   * classification read are `GetItem` and `TransactGetItems` on `GENERATION#*`.
   */
  private grantGraphPublish(fn: lambda.Function, publicationsArn: string): void {
    fn.addToRolePolicy(new iam.PolicyStatement({
      sid: 'StageAndFoldGrantPublications',
      actions: ['dynamodb:UpdateItem'],
      resources: [publicationsArn],
      conditions: { 'ForAllValues:StringLike': { 'dynamodb:LeadingKeys': this.datasetKeys() } },
    }));
    fn.addToRolePolicy(new iam.PolicyStatement({
      sid: 'FlipGenerationPointers',
      actions: ['dynamodb:UpdateItem'],
      resources: [publicationsArn],
      conditions: {
        'ForAllValues:StringLike': { 'dynamodb:LeadingKeys': ['GENERATION#*'] },
        StringEquals: { 'dynamodb:EnclosingOperation': 'TransactWriteItems' },
      },
    }));
    fn.addToRolePolicy(new iam.PolicyStatement({
      sid: 'ReadGenerationPointers',
      actions: ['dynamodb:GetItem'],
      resources: [publicationsArn],
      conditions: { 'ForAllValues:StringLike': { 'dynamodb:LeadingKeys': ['GENERATION#*'] } },
    }));
  }

  /**
   * S5: the stuck-flow detector and the graph protocol alarms. Unlike the five workers its
   * schedule is enabled in every mode: it only reads (a strong `Query` of the `DUE#` shards, the
   * mode item and, plan 6.1, the precompute contribution of a readiness-stamped winner), and in `LEGACY` and `SHADOW` a healthy sweep of the live table reads zero.
   */
  private buildStuckFlowDetector(props: IngestionStage1Props, graph: ExecutionGraphAuthority): void {
    const { config } = props;
    const fn = this.functionWithLogs(props, {
      id: 'ExecutionGraphStuckFlowDetector',
      handler: 'adapters.jobs.execution_graph.stuck_flow.handler',
      // Sixteen strongly consistent shard queries, the mode GetItem and one contribution GetItem
      // per readiness-stamped winner a pass.
      memorySize: 512,
      timeoutSeconds: 60,
      reservedConcurrency: 1,
      environment: {
        ...this.semanticEnvironment(config),
        ADL_EXECUTION_GRAPH_TABLE: graph.tableName,
      },
    });
    grantModeItemRead(fn, graph, false);
    grantDueShardRead(fn, graph);
    grantBakeContributionRead(fn, graph);
    new events.Rule(this, 'ExecutionGraphStuckFlowDetectorSchedule', {
      ruleName: `adlytics-${config.envName}-ExecutionGraphStuckFlowDetectorSchedule`,
      schedule: events.Schedule.rate(cdk.Duration.minutes(1)),
      targets: [new targets.LambdaFunction(fn)],
    });
    buildGraphAlarms(this, config, props.alarmTopic);
    this.stuckFlowDetector = fn;
  }

  private stage1Limits(config: AppConfig): Record<string, string> {
    return stage1LimitsEnvironment(config);
  }

  private grantSemanticStorage(
    config: AppConfig,
    principal: lambda.Function | iam.Role,
    into?: iam.ManagedPolicy,
  ): void {
    const semantic = dataBucketName(config, 'semantic-v2');
    const results = dataBucketName(config, 'v2-results');
    // `into` holds the statements in a named managed policy instead of the role's default one.
    const grant = (statement: iam.PolicyStatement): void => {
      if (into === undefined) principal.grantPrincipal.addToPrincipalPolicy(statement);
      else into.addStatements(statement);
    };
    grant(new iam.PolicyStatement({
      actions: ['s3:GetObject', 's3:PutObject', 's3:DeleteObject', 's3:AbortMultipartUpload', 's3:ListMultipartUploadParts'],
      resources: [
        `arn:${this.stack.partition}:s3:::${semantic}/immutable/source-aligned/*`,
        `arn:${this.stack.partition}:s3:::${semantic}/immutable/canonical/candidate/*`,
        `arn:${this.stack.partition}:s3:::${semantic}/immutable/canonical/successful/*`,
        `arn:${this.stack.partition}:s3:::${semantic}/source-aligned/*`,
        `arn:${this.stack.partition}:s3:::${semantic}/canonical/candidate/*`,
        `arn:${this.stack.partition}:s3:::${semantic}/canonical/current/*`,
      ],
    }));
    // SEMANTIC-M1 (plan 2.4): the BAKE results prefix carries the principal's former
    // `batch/*` set minus `s3:DeleteObject`; `batch/*` keeps only the terminal read.
    grant(new iam.PolicyStatement({
      actions: ['s3:GetObject', 's3:PutObject', 's3:AbortMultipartUpload', 's3:ListMultipartUploadParts'],
      resources: [`arn:${this.stack.partition}:s3:::${results}/bake/*`],
    }));
    grant(new iam.PolicyStatement({
      actions: ['s3:GetObject'],
      resources: [`arn:${this.stack.partition}:s3:::${results}/batch/*`],
    }));
    grant(new iam.PolicyStatement({
      actions: ['s3:ListBucket'],
      resources: [`arn:${this.stack.partition}:s3:::${semantic}`],
      conditions: {
        StringLike: {
          's3:prefix': [
            'source-aligned/*',
            'canonical/candidate/*',
            'canonical/current/*',
          ],
        },
      },
    }));
    grant(new iam.PolicyStatement({
      actions: ['s3:ListBucket'],
      resources: [`arn:${this.stack.partition}:s3:::${results}`],
      conditions: { StringLike: { 's3:prefix': ['bake/*', 'batch/*'] } },
    }));
    grant(new iam.PolicyStatement({
      actions: ['s3:GetBucketLocation'],
      resources: [
        `arn:${this.stack.partition}:s3:::${semantic}`,
        `arn:${this.stack.partition}:s3:::${results}`,
      ],
    }));
    this.addAthenaObservation(config, principal, true, into);
    const databases = ['source-aligned', 'candidate', 'current'].map(
      (layer) => semanticV2DatabaseName(config.envName, layer as 'source-aligned' | 'candidate' | 'current'),
    );
    grant(new iam.PolicyStatement({
      actions: ['glue:GetDatabase', 'glue:GetTable', 'glue:GetTables', 'glue:GetPartitions', 'glue:UpdateTable', 'glue:BatchCreatePartition', 'glue:CreatePartition', 'glue:UpdatePartition', 'glue:DeletePartition', 'glue:BatchDeletePartition'],
      resources: [
        `arn:${this.stack.partition}:glue:${this.stack.region}:${this.stack.account}:catalog`,
        ...databases.flatMap((database) => [
          `arn:${this.stack.partition}:glue:${this.stack.region}:${this.stack.account}:database/${database}`,
          `arn:${this.stack.partition}:glue:${this.stack.region}:${this.stack.account}:table/${database}/*`,
        ]),
      ],
    }));
  }

  private addAthenaObservation(
    config: AppConfig,
    principal: lambda.Function | iam.Role,
    includeStart = false,
    into?: iam.ManagedPolicy,
  ): void {
    const workgroupArn = (kind: 'bake' | 'batch'): string =>
      `arn:${this.stack.partition}:athena:${this.stack.region}:${this.stack.account}:workgroup/${semanticV2AthenaWorkgroupName(config.envName, kind)}`;
    const observe = ['athena:GetQueryExecution', 'athena:GetQueryResults', 'athena:StopQueryExecution'];
    // SEMANTIC-M1: starts (when granted) and observation on BAKE; pre-cutover `batch`
    // executions stay observable, but nobody here starts one.
    const statements = [
      new iam.PolicyStatement({
        actions: [...(includeStart ? ['athena:StartQueryExecution'] : []), ...observe],
        resources: [workgroupArn('bake')],
      }),
      new iam.PolicyStatement({ actions: observe, resources: [workgroupArn('batch')] }),
    ];
    for (const statement of statements) {
      if (into === undefined) principal.grantPrincipal.addToPrincipalPolicy(statement);
      else into.addStatements(statement);
    }
  }

  /**
   * AGG-6 residual (plan D5): only the reconciler replays persisted Stage-1 envelopes, and an
   * unsubmitted one may still name `batch`, so it alone keeps Start on `batch` and the results
   * write set the replayed execution needs. One Sid group, removed together once
   * `probe_athena_workgroup_migration.py` reports `removal_trigger_met` everywhere.
   */
  private grantReplayPersistedBatchEnvelopes(config: AppConfig, principal: lambda.Function): void {
    const results = dataBucketName(config, 'v2-results');
    principal.addToRolePolicy(new iam.PolicyStatement({
      sid: 'ReplayPersistedBatchEnvelopes',
      actions: ['athena:StartQueryExecution'],
      resources: [
        `arn:${this.stack.partition}:athena:${this.stack.region}:${this.stack.account}:workgroup/${semanticV2AthenaWorkgroupName(config.envName, 'batch')}`,
      ],
    }));
    principal.addToRolePolicy(new iam.PolicyStatement({
      sid: 'ReplayPersistedBatchEnvelopesResults',
      actions: ['s3:GetObject', 's3:PutObject', 's3:AbortMultipartUpload', 's3:ListMultipartUploadParts'],
      resources: [`arn:${this.stack.partition}:s3:::${results}/batch/*`],
    }));
  }

  private addAttemptTableAccess(fn: lambda.Function, tableArn: string): void {
    fn.addToRolePolicy(new iam.PolicyStatement({
      actions: ['dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:UpdateItem'],
      resources: [tableArn],
    }));
  }

  private addDirectReads(
    principal: lambda.Function | iam.Role,
    resource: string,
    leadingKeys: readonly string[],
  ): void {
    principal.grantPrincipal.addToPrincipalPolicy(
      this.dynamoStatement(['dynamodb:GetItem'], resource, leadingKeys),
    );
  }

  private addTransactionReads(fn: lambda.Function, resource: string, leadingKeys: readonly string[]): void {
    fn.addToRolePolicy(this.dynamoStatement(
      ['dynamodb:GetItem'],
      resource,
      leadingKeys,
      'TransactGetItems',
    ));
  }

  private addTransactionWrites(fn: lambda.Function | iam.Role, resource: string, leadingKeys: readonly string[]): void {
    fn.grantPrincipal.addToPrincipalPolicy(this.dynamoStatement(
      ['dynamodb:PutItem', 'dynamodb:UpdateItem', 'dynamodb:ConditionCheckItem'],
      resource,
      leadingKeys,
      'TransactWriteItems',
    ));
  }

  private addTransactionActions(
    principal: lambda.Function | iam.Role,
    resource: string,
    leadingKeys: readonly string[],
    actions: readonly ('dynamodb:PutItem' | 'dynamodb:UpdateItem' | 'dynamodb:ConditionCheckItem')[],
  ): void {
    principal.grantPrincipal.addToPrincipalPolicy(
      this.dynamoStatement(actions, resource, leadingKeys, 'TransactWriteItems'),
    );
  }

  private addDirectAttemptMutation(fn: lambda.Function | iam.Role, resource: string): void {
    fn.grantPrincipal.addToPrincipalPolicy(
      this.dynamoStatement(['dynamodb:UpdateItem'], resource, ['TABLE_ATTEMPT#*']),
    );
  }

  private addIndexQuery(fn: lambda.Function, resource: string, leadingKey: 'pending' | 'blocked' | 'ready'): void {
    fn.addToRolePolicy(this.dynamoStatement(['dynamodb:Query'], resource, [leadingKey]));
  }

  /** Root-clock readback: successor lifecycle events strongly query the retained root
   * `EVENT#` item under the flow's own `STATUS_FLOW#` partition to repeat its clock. */
  private addFlowRootClockQuery(fn: lambda.Function, publicationsArn: string): void {
    fn.addToRolePolicy(this.dynamoStatement(['dynamodb:Query'], publicationsArn, ['STATUS_FLOW#*']));
  }

  /** Plan roster readback: `plan_unit_items` strongly queries every `UNIT#` item under one
   * exact `WRITE_PLAN#` partition (§2.2 claim, §3 roster, plan completion). Table-scoped
   * `GetItem` cannot serve it, and the first live queued intent proved the gap
   * (AccessDenied, 2026-09-22). */
  private addPlanPartitionQuery(fn: lambda.Function, publicationsArn: string): void {
    fn.addToRolePolicy(this.dynamoStatement(['dynamodb:Query'], publicationsArn, ['WRITE_PLAN#*']));
  }

  private dynamoStatement(
    actions: readonly string[],
    resource: string,
    leadingKeys: readonly string[],
    enclosingOperation?: 'TransactGetItems' | 'TransactWriteItems',
  ): iam.PolicyStatement {
    return new iam.PolicyStatement({
      actions: [...actions],
      resources: [resource],
      conditions: {
        'ForAllValues:StringLike': { 'dynamodb:LeadingKeys': [...leadingKeys] },
        ...(enclosingOperation === undefined
          ? {}
          : { StringEquals: { 'dynamodb:EnclosingOperation': enclosingOperation } }),
      },
    });
  }

  private addQueueConsumer(fn: lambda.Function, queueArn: string): void {
    fn.addToRolePolicy(new iam.PolicyStatement({
      actions: ['sqs:ReceiveMessage', 'sqs:DeleteMessage', 'sqs:GetQueueAttributes', 'sqs:ChangeMessageVisibility'],
      resources: [queueArn],
    }));
  }

  private addQueueSender(fn: lambda.Function, queueArn: string): void {
    fn.addToRolePolicy(new iam.PolicyStatement({ actions: ['sqs:SendMessage'], resources: [queueArn] }));
  }

  private invokeOnlyRole(id: string, fn: lambda.Function): iam.Role {
    const role = new iam.Role(this, id, {
      assumedBy: new iam.AccountPrincipal(this.stack.account).withConditions({
        StringEquals: { 'aws:PrincipalAccount': this.stack.account },
      }),
      description: `Same-account invoke-only operator for ${fn.functionName}.`,
    });
    role.addToPolicy(new iam.PolicyStatement({
      actions: ['lambda:InvokeFunction'],
      resources: [fn.functionArn],
    }));
    return role;
  }

  private buildSemanticProjectionOperatorRole(
    config: AppConfig,
    publicationsArn: string,
    leasesArn: string,
    attemptsArn: string,
  ): iam.Role {
    const role = new iam.Role(this, 'SemanticProjectionOperatorRole', {
      assumedBy: new iam.AccountPrincipal(this.stack.account).withConditions({
        StringEquals: { 'aws:PrincipalAccount': this.stack.account },
      }),
      description: 'Same-account role for the two explicit-version semantic projection operators.',
    });
    role.addToPolicy(new iam.PolicyStatement({
      actions: ['s3:GetObjectVersion'],
      resources: ['amc', 'vendor_central', 'vendor_data_kiosk'].map(
        (source) => `arn:${this.stack.partition}:s3:::${dataBucketName(config, 'semantic-v2-raw')}/${source}/*`,
      ),
    }));
    this.grantSemanticStorage(config, role);
    // ADR-149 (D7): the operator jobs write through the same staged path as the TableWriter.
    const operatorStaging = new iam.ManagedPolicy(this, 'SemanticProjectionOperatorStagingPolicy', {
      description: 'ADR-149 staged table units for the semantic projection operators.',
    });
    grantStagingStorage(config, role, { write: true }, operatorStaging);
    this.addDirectReads(role, publicationsArn, [
      'TABLE_ATTEMPT#*',
      'STATUS_FLOW#*',
      ...this.datasetKeys(),
    ]);
    this.addDirectReads(role, leasesArn, ['TABLE#*']);
    this.addDirectAttemptMutation(role, publicationsArn);
    this.addTransactionWrites(role, publicationsArn, [
      'TABLE_ATTEMPT#*',
      'STATUS_FLOW#*',
      ...this.datasetKeys(),
    ]);
    this.addTransactionWrites(role, leasesArn, ['TABLE#*']);
    role.addToPolicy(new iam.PolicyStatement({
      actions: ['dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:UpdateItem'],
      resources: [attemptsArn],
    }));
    return role;
  }

  private publishParameters(config: AppConfig, deliveryFunction: lambda.IFunction): void {
    for (const [id, key, value] of [
      ['ProviderFenceRecoveryOperatorRoleArnParameter', 'providerFenceRecoveryOperatorRoleArn', this.providerFenceRecoveryOperatorRole.roleArn],
      ['ProviderFenceRecoveryFunctionArnParameter', 'providerFenceRecoveryFunctionArn', this.providerFenceRecoveryFunction.functionArn],
      ['SemanticProjectionOperatorRoleArnParameter', 'semanticProjectionOperatorRoleArn', this.semanticProjectionOperatorRole.roleArn],
      ['Stage1PreswitchVerificationRoleArnParameter', 'stage1PreswitchVerificationRoleArn', this.stage1PreswitchVerificationRole.roleArn],
      ['SemanticWriteQueueArnParameter', 'semanticWriteQueueArn', this.semanticWriteQueue.queueArn],
      ['SemanticWriteDlqArnParameter', 'semanticWriteDlqArn', this.semanticWriteDeadLetterQueue.queueArn],
      ['DeliveryFunctionArnParameter', 'deliveryFunctionArn', deliveryFunction.functionArn],
    ] as const) {
      new ssm.StringParameter(this, id, {
        parameterName: paramPath(config.envName, key),
        stringValue: value,
      });
    }
  }

  private buildHandoffResources(
    props: IngestionStage1Props,
    controls: ProjectionHandoffControls,
    publicationsArn: string,
  ): HandoffResources {
    const config = props.config;
    const providerRoot = new Construct(this, 'ProjectionHandoffProviderResources');
    const functionName = `adlytics-${config.envName}-ProjectionHandoffProvider`;
    const logGroup = new logs.LogGroup(providerRoot, 'Logs', {
      logGroupName: `/aws/lambda/${functionName}`,
      retention: config.logRetentionDays,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    const role = new iam.Role(providerRoot, 'ExecutionRole', {
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      description: 'Private projection-handoff authority provider.',
    });
    role.addToPolicy(new iam.PolicyStatement({
      actions: ['logs:CreateLogStream', 'logs:PutLogEvents'],
      resources: [`${logGroup.logGroupArn}:*`],
    }));
    role.addToPolicy(new iam.PolicyStatement({
      actions: ['dynamodb:GetItem', 'dynamodb:UpdateItem', 'dynamodb:DeleteItem'],
      resources: [publicationsArn],
      conditions: {
        'ForAllValues:StringEquals': {
          'dynamodb:LeadingKeys': [`PROJECTION_HANDOFF#${config.envName}`],
        },
      },
    }));
    const provider = new lambda.Function(providerRoot, 'Function', {
      functionName,
      runtime: new lambda.Runtime(config.pythonRuntime, lambda.RuntimeFamily.PYTHON),
      architecture: { arm64: lambda.Architecture.ARM_64 }[config.lambdaArchitecture],
      code: lambda.Code.fromAsset(props.sourceAssetPath),
      handler: 'adapters.jobs.projection_handoff.handler',
      layers: [props.dependencies],
      environment: {
        ...tracingEnvironment(config),
        ...this.stage1Limits(config),
        ADL_ENVIRONMENT: config.envName,
        ADL_SEMANTIC_PUBLICATIONS_TABLE: semanticV2StateTableName(config.envName, 'publications'),
      },
      memorySize: 512,
      timeout: cdk.Duration.seconds(60),
      reservedConcurrentExecutions: 1,
      role,
      logGroup,
    });
    grantSpanExport(provider);

    const baseProperties = {
      protocolVersion: 1,
      environment: config.envName,
      stackIdentity: cdk.Aws.STACK_ID,
    } as const;
    const targetProperties = {
      ...baseProperties,
      requestedState: config.ingestion.projectionHandoffState,
      projectionComposition: controls.projectionComposition,
      deliveryMappingEnabled: controls.deliveryMappingEnabled,
      semanticWriterMappingEnabled: controls.semanticWriterMappingEnabled,
    } as const;
    const prepare = new cdk.CustomResource(this, 'ProjectionHandoffPrepare', {
      resourceType: 'Custom::ProjectionHandoffPrepare',
      serviceToken: provider.functionArn,
      properties: { ...targetProperties, resourceKind: 'prepare' },
    });
    const commit = new cdk.CustomResource(this, 'ProjectionHandoffCommit', {
      resourceType: 'Custom::ProjectionHandoffCommit',
      serviceToken: provider.functionArn,
      properties: {
        ...targetProperties,
        resourceKind: 'commit',
        mode: prepare.getAttString('mode'),
        transitionToken: prepare.getAttString('transitionToken'),
        fromDisposition: prepare.getAttString('fromDisposition'),
        fromState: prepare.getAttString('fromState'),
        expectedRevision: prepare.getAttString('expectedRevision'),
      },
    });
    const guard = new cdk.CustomResource(this, 'ProjectionHandoffDeletionGuard', {
      resourceType: 'Custom::ProjectionHandoffDeletionGuard',
      serviceToken: provider.functionArn,
      properties: { ...baseProperties, resourceKind: 'decommission' },
    });

    return {
      prepare: prepare.node.defaultChild as cdk.CfnResource,
      commit: commit.node.defaultChild as cdk.CfnResource,
      guard: guard.node.defaultChild as cdk.CfnResource,
      providerRoot,
    };
  }

  private tableArn(tableName: string): string {
    return `arn:${this.stack.partition}:dynamodb:${this.stack.region}:${this.stack.account}:table/${tableName}`;
  }

  private datasetKeys(): string[] {
    return [...CANONICAL_DATASET_KEYS];
  }
}
