// SPDX-License-Identifier: MIT-0
import * as cdk from 'aws-cdk-lib';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as actions from 'aws-cdk-lib/aws-cloudwatch-actions';
import * as events from 'aws-cdk-lib/aws-events';
import * as targets from 'aws-cdk-lib/aws-events-targets';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as sources from 'aws-cdk-lib/aws-lambda-event-sources';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';

import {
  AppConfig,
  EnabledLandingConfig,
  ProjectionHandoffControls,
  paramPath,
  producerRegistrationStatusTableName,
  resolveProjectionHandoffState,
  semanticV2AthenaWorkgroupName,
  semanticV2DatabaseName,
  semanticV2StateTableName,
} from '../config';
import { INGESTION_LOAD_RESERVED_CONCURRENCY } from '../config/athena-start-budget';
import {
  CANONICAL_DATASET_KEYS,
  INGESTION_ERROR_OPERATIONS,
  IngestionStage1,
  queueAgeCeilingSeconds,
  stage1LimitsEnvironment,
} from './ingestion-stage1';
import { ROUTES } from '../generated/execution-graph-protocol';
import { dataBucketName } from '../mixins/storage';
import { applyStandardTags } from '../mixins/tagging';
import {
  AdmissionRequesterTargets,
  executionGraphAuthority,
  grantAdmissionRequester,
  grantDrainSeqWrite,
  grantModeItemRead,
} from './execution-graph-authority';
import { buildPostGrantInvariantAlarm } from './execution-graph-alarms';
import { arrivalAlarm, staffedAlarmTopic } from '../constructs/observability/async-failure';
import {
  CATCH_ALL_RUNBOOK,
  alarmingErrorsByOperationQuery,
  alarmingErrorsQuery,
  errorAlarmSuffix,
} from '../constructs/observability/error-alarms';
import { PAGE_OPERATIONS } from '../constructs/observability/page-route-alarms';
import { applyFunctionErrorAlarms } from '../mixins/function-error-alarms';
import { METRIC_RECONCILER_OPERATION } from './operational-status-processor-stack';
import { grantSpanExport } from '../telemetry';

/**
 * What turns a landed delivery into a load.
 *
 * DESTROY throughout. Every resource here is a trigger, and a trigger is rebuildable — the
 * delivery it reacts to is in the RETAIN Semantic V2 raw bucket and the rows it writes are in
 * RETAIN Semantic V2 storage, so losing this stack loses the *reaction*, never the data. Re-landing a manifest
 * replays it.
 *
 * **The chain, and why each hop exists.**
 *
 * `DataV2Stack` publishes the Semantic V2 raw bucket's object events to EventBridge — a boolean
 * with no knowledge of any consumer. That inversion is not stylistic: the raw bucket is early in
 * `DEPLOY_ORDER` and RETAIN, so a direct S3-to-SQS notification would need a queue ARN from a stack
 * deploying after it, and SSM cannot run backwards. ADR-012 calls that cycle a wrong boundary.
 *
 * A rule filters on the manifest suffix, because ADR-030 makes the manifest the completion marker:
 * the data objects land first and the manifest last, so reacting to a data object would start a
 * load against a delivery that is still arriving.
 *
 * A queue sits between the rule and the function, and this is the load-bearing hop. It buys three
 * things an EventBridge-to-Lambda target cannot: bounded concurrency, a redrive policy with an
 * inspectable backlog, and a dead-letter queue holding the actual message. The event source and
 * function carry the same concurrency ceiling, so Lambda never receives more queue work than it
 * has execution slots for.
 *
 * Standard rather than FIFO. A FIFO group key would have to be the target table, but an S3 key
 * encodes `source_system/source_dataset`, so deriving the table needs the contract registry and
 * therefore a router function in front. Ordering is not what makes a delivery safe in any case: the
 * watermark decision, conditional lease, and atomic publication transaction do. An out-of-order
 * arrival is correct rather than merely tolerated.
 */
export interface IngestionStackProps extends cdk.StackProps {
  readonly config: AppConfig;
  /** Directory holding the Python source tree. */
  readonly sourceAssetPath: string;
}

/**
 * The load function's `inline_ingestion` row of the static Athena start budget (S1), and also the
 * ProducerFinalizer's reservation and both event sources' maximum concurrency.
 */
const RESERVED_CONCURRENCY = INGESTION_LOAD_RESERVED_CONCURRENCY;
/**
 * Two execution slots, matched exactly by each SQS event source's maximum concurrency.
 *
 * Lambda's SQS maximum-concurrency contract starts at two. Giving the function fewer reserved
 * slots than its event source can invoke lets polling throttles consume receive attempts before the
 * handler runs, which can dead-letter valid work. Matching both ceilings prevents that failure and
 * still bounds cost and downstream pressure.
 *
 * Correctness does not depend on serial execution: exact version/checksum binding, conditional
 * leases, immutable candidates, and the atomic publication transaction protect concurrent and
 * out-of-order deliveries. The shared value is asserted on both the function and event source so a
 * future throughput change cannot silently widen only one side.
 */

const BATCH_SIZE = 1;
/**
 * One message per invocation.
 *
 * With concurrency bounded a larger batch is unnecessary for throughput and turns one poison
 * delivery into N retried siblings. Keeping the retry unit equal to the delivery unit is what makes
 * redrive mean anything.
 */

const MAX_RECEIVE_COUNT = ROUTES.delivery.max_receive_count;
/**
 * Attempts before a delivery message is dead-lettered, read from the generated execution-graph
 * protocol (`routes.delivery`), which the delivery claimer's final-receive test also reads, so the
 * queue and its claimer cannot drift (design §2.1).
 *
 * Three, because the failures that reach here are already split by design. A transient failure —
 * a throttle, an Athena timeout, a function dying mid-merge — recovers on the next attempt, and the
 * ledger makes that safe: the interrupted attempt reads `STARTED` and the retry decides
 * `APPLY_RETRY`. A rejected landing is corrupt bytes and will fail identically forever, so a high
 * count only delays the alarm.
 *
 * A blocked delivery reaches neither path. It is a *decided* outcome recorded in V2 state, so the
 * handler returns success and the message is deleted — replaying it would be refused as
 * `SKIP_ALREADY_REJECTED` anyway, which would spend three attempts and then misrepresent a decision
 * as undelivered work.
 */

const VISIBILITY_MARGIN_SECONDS = 60;
/**
 * How much longer a message stays invisible than the function may run.
 *
 * A margin, not a multiple, and the distinction is worth stating because the usual advice is six
 * times the function timeout. That advice assumes a short function: applied to a fifteen-minute one
 * it puts ninety minutes between attempts and three hours before a message dead-letters, which turns
 * automatic recovery from a throttle into something slower than a human noticing.
 *
 * What the value actually has to satisfy is narrow. The function timeout is the longest an
 * invocation can hold a message, so anything above it prevents the failure that matters — a message
 * becoming visible while an invocation is still working on it, which is one delivery loaded twice
 * concurrently and the exact thing this visibility margin exists to prevent. A fixed minute over
 * both sufficient and the fastest safe retry.
 */

const TIMEOUT_SECONDS = 900;
/**
 * Lambda's maximum, chosen deliberately rather than by default.
 *
 * `BATCH_MAX_WAIT_SECONDS` in the load path is 600 *per statement*, and a fact issues roughly four
 * statements plus a dimension read — so a delivery can legitimately outlast this. Measured reality
 * is 10 to 16 seconds, which is a long way off, but the ceiling is not structurally excluded and
 * pretending otherwise would be the claim that stops being true without anything failing.
 *
 * What makes Lambda acceptable to be wrong about is the recovery: a timeout leaves the ledger at
 * `STARTED`, which is the `APPLY_RETRY` path, which redrive supplies for free. So the failure mode
 * of outgrowing this is a retry, not data loss. `ingest.delivery` carries the duration, so moving to
 * Step Functions becomes a measurement rather than a guess.
 */

const MEMORY_MB = 1024;
/**
 * Uniform with the rest of the platform, and sized for parsing rather than for waiting.
 *
 * Most of a load's wall clock is spent polling Athena, where memory buys nothing. The CPU-bound part
 * is real though: every delivered row is parsed, mapped and validated through a generated pydantic
 * model, and CPU scales with memory. Below this the parse dominates a small delivery; above it
 * nothing improves, because the remainder is waiting.
 */

const MANIFEST_SUFFIX = 'manifest.json';
const INCOMING_PREFIX = 'incoming/';
const REGISTRATION_SUFFIX = 'registration.json';
const FINALIZER_TIMEOUT_SECONDS = 300;
/**
 * The object whose arrival means a delivery is complete.
 *
 * Stated here as well as in `domain/curation/landing.MANIFEST_NAME`, and the duplication is
 * unavoidable rather than sloppy: an EventBridge pattern is synthesised from TypeScript and the
 * domain constant is Python, so there is no expression that reads one from the other. The pair is
 * covered by an end-to-end test that lands a real delivery, which fails loudly if they diverge.
 */

const REGISTRATION_MAX_RECEIVE_COUNT = 3;
/** Attempts before a producer-registration message is dead-lettered. It is not a graph route. */

interface ProducerResources {
  readonly registrationQueue: sqs.Queue;
  readonly registrationDeadLetterQueue: sqs.Queue;
  readonly registrationStatusTable: dynamodb.Table;
  readonly landingRole: iam.Role;
  readonly finalizer: lambda.Function;
}

export class IngestionStack extends cdk.Stack {
  public readonly queue: sqs.Queue;
  public readonly deadLetterQueue: sqs.Queue;
  public readonly alarmTopic: sns.Topic;
  /** FR-10: the selected staffed destination; the ingestion topic itself for the email arm. */
  public readonly staffedTopic: sns.ITopic;

  constructor(scope: Construct, id: string, props: IngestionStackProps) {
    super(scope, id, props);
    applyStandardTags(this, props.config);

    const { config } = props;
    const legacyInputs = [
      props as unknown as Record<string, unknown>,
      config as unknown as Record<string, unknown>,
    ];
    for (const forbidden of [
      'projectionMode',
      'deliveryMappingEnabled',
      'semanticWriteMappingEnabled',
    ]) {
      if (legacyInputs.some((input) => forbidden in input)) {
        throw new Error(`Legacy independent ${forbidden} input is forbidden.`);
      }
    }
    const handoff = resolveProjectionHandoffState(config.ingestion?.projectionHandoffState);

    this.deadLetterQueue = new sqs.Queue(this, 'DeadLetterQueue', {
      queueName: `adlytics-${config.envName}-ingestion-dlq`,
      retentionPeriod: cdk.Duration.days(14),
      enforceSSL: true,
    });

    this.queue = new sqs.Queue(this, 'DeliveryQueue', {
      queueName: `adlytics-${config.envName}-ingestion`,
      visibilityTimeout: cdk.Duration.seconds(TIMEOUT_SECONDS + VISIBILITY_MARGIN_SECONDS),
      retentionPeriod: cdk.Duration.days(14),
      enforceSSL: true,
      deadLetterQueue: {
        queue: this.deadLetterQueue,
        maxReceiveCount: MAX_RECEIVE_COUNT,
      },
    });

    this.alarmTopic = this.buildAlarmTopic(config);
    // FR-10 (S5): every alarm acts on the selected staffed destination only.
    this.staffedTopic = staffedAlarmTopic(this, config, this.alarmTopic);

    const dependencies = this.dependenciesLayer(config);
    // S3: the retained execution-graph table, where the operational plane provisions it.
    const graph = executionGraphAuthority(config, this);
    const ingestion = this.buildFunction(props, dependencies, handoff);
    if (graph !== undefined) {
      // V-13(b): every source-delivery submission condition-checks the mode item (and S6's
      // ingress reads it), so the handler gets the table and the two read-class mode actions.
      ingestion.addEnvironment('ADL_EXECUTION_GRAPH_TABLE', graph.tableName);
      grantModeItemRead(ingestion, graph, true);
      // S6 (V-19): the handler is the `delivery_event` requester (SHADOW recording and GRAPH
      // admission), holds a drained delivery with ChangeMessageVisibility, and counts on
      // DRAIN_SEQ like every legacy family-changing writer (V-17b).
      grantAdmissionRequester(ingestion, graph, this.admissionTargets(config));
      grantDrainSeqWrite(ingestion, this.publicationsArn(config));
      ingestion.addToRolePolicy(new iam.PolicyStatement({
        sid: 'HoldDrainedDeliveries',
        actions: ['sqs:ChangeMessageVisibility'],
        resources: [this.queue.queueArn],
      }));
      // S6 part 2 (§2.11 row 3, AGG3-8/AGG4-10): the `materialize_raw` claim path's
      // `read_nodes` is a strong `Query` of `RUN#<run_id>`. `WRITER_ACTIONS` already holds the
      // strong `GetItem`s and every write, so this is the handler's one new statement. The role's
      // inline policies already sit at IAM's 10,240-character aggregate limit (see
      // `IdentityEvidencePolicy`), so it lives in its own customer-managed policy on this role.
      // The function depends on it, as it does on its inline default policy.
      if (ingestion.role === undefined) {
        throw new Error('the delivery function must own an execution role.');
      }
      const runNodes = new iam.ManagedPolicy(this, 'GraphRunNodesPolicy', {
        description: 'Strong Query of an execution-graph run partition (S6 part 2, row 3).',
        roles: [ingestion.role],
        statements: [new iam.PolicyStatement({
          sid: 'QueryGraphRunNodes',
          actions: ['dynamodb:Query'],
          resources: [graph.tableArn],
          conditions: { 'ForAllValues:StringLike': { 'dynamodb:LeadingKeys': ['RUN#*'] } },
        })],
      });
      ingestion.node.addDependency(runNodes);
      // §2.11 row 5b: the dispatcher, TableWriter, the baker and precompute all feed it.
      buildPostGrantInvariantAlarm(this, config, this.staffedTopic);
    }
    this.grantLoad(config, ingestion);
    grantSpanExport(ingestion);
    ingestion.addEventSource(
      new sources.SqsEventSource(this.queue, {
        batchSize: BATCH_SIZE,
        // A disabled delivery mapping omits ScalingConfig: the quiesced states render the
        // function's reserved concurrency 0 (the §2.5 evidence), and the service rejects a
        // mapping whose MaximumConcurrency exceeds that reservation.
        ...(handoff.deliveryMappingEnabled ? { maxConcurrency: RESERVED_CONCURRENCY } : {}),
        enabled: handoff.deliveryMappingEnabled,
        // S6 (V-19): a delivery the drain leaves waiting is reported as a batch item failure,
        // so it stays unacknowledged without failing the invocation; its visibility is already
        // set to the drain-wait expiry. The batch size stays one.
        reportBatchItemFailures: true,
      }),
    );

    this.buildRule(config);
    this.buildCoreAlarms(config, ingestion);
    this.publishCoreParameters(config);

    if (config.landing.enabled) {
      const producer = this.buildProducerResources(props, dependencies, config.landing);
      if (graph !== undefined) {
        // S3 item 7: the registration finalizer reads the mode item (its S6 `schedule` request).
        grantModeItemRead(producer.finalizer, graph, false);
        // S6 (V-19): after its manifest is stored, the finalizer is the `schedule` requester.
        producer.finalizer.addEnvironment('ADL_EXECUTION_GRAPH_TABLE', graph.tableName);
        grantAdmissionRequester(producer.finalizer, graph, this.admissionTargets(config));
      }
      this.buildRegistrationRule(config, producer.registrationQueue);
      this.buildProducerAlarms(config, producer);
      this.publishProducerParameters(config, producer);
    }

    const stage1 = new IngestionStage1(this, 'Stage1', {
      config,
      sourceAssetPath: props.sourceAssetPath,
      dependencies,
      controls: handoff,
      alarmTopic: this.staffedTopic,
      deliveryFunction: ingestion,
      ...(graph === undefined ? {} : { executionGraph: graph }),
    });
    // After every function above is in the tree. This stack owns the topic, so the staffed
    // switch is the in-stack one rather than the published parameter.
    applyFunctionErrorAlarms(this, config, new actions.SnsAction(this.staffedTopic));
    // Last: ADR-075 orders every application resource, the alarms included, around the handoff.
    stage1.orderHandoff();
  }

  /**
   * Where a failure this platform cannot recover from is announced.
   *
   * **The first alerting in the platform, and it ships with no subscription.** A topic ARN is a seam
   * a customer subscribes to; a destination baked in here would be our address in their account. So
   * the ARN is published to SSM and the subscription is theirs to add — which means these alarms are
   * visible in the console and silent until someone opts in. That is a smaller lie than a
   * dead-letter queue nobody is ever told about, and it is stated rather than implied.
   */
  private buildAlarmTopic(config: AppConfig): sns.Topic {
    const topic = new sns.Topic(this, 'AlarmTopic', {
      topicName: `adlytics-${config.envName}-ingestion-alarms`,
      displayName: 'Adlytics ingestion failures',
      enforceSSL: true,
    });
    // `enforceSSL` gives the topic an explicit policy, which replaces SNS's default owner access, so
    // CloudWatch alarms in this account could not publish: every alarm action failed [verified
    // 2026-09-27, alarm history "Failed to execute action"]. Admit only this account's alarms.
    topic.addToResourcePolicy(new iam.PolicyStatement({
      sid: 'AllowThisAccountsAlarms',
      principals: [new iam.ServicePrincipal('cloudwatch.amazonaws.com')],
      actions: ['sns:Publish'],
      resources: [topic.topicArn],
      conditions: {
        StringEquals: { 'aws:SourceAccount': this.account },
        ArnLike: { 'aws:SourceArn': `arn:${this.partition}:cloudwatch:${this.region}:${this.account}:alarm:*` },
      },
    }));
    return topic;
  }

  private buildProducerResources(
    props: IngestionStackProps,
    dependencies: lambda.ILayerVersion,
    landing: EnabledLandingConfig,
  ): ProducerResources {
    const { config } = props;
    const registrationDeadLetterQueue = new sqs.Queue(
      this,
      'RegistrationDeadLetterQueue',
      {
        queueName: `adlytics-${config.envName}-registration-dlq`,
        retentionPeriod: cdk.Duration.days(14),
        enforceSSL: true,
      },
    );
    const registrationQueue = new sqs.Queue(this, 'RegistrationQueue', {
      queueName: `adlytics-${config.envName}-registration`,
      visibilityTimeout: cdk.Duration.seconds(
        FINALIZER_TIMEOUT_SECONDS + VISIBILITY_MARGIN_SECONDS,
      ),
      retentionPeriod: cdk.Duration.days(14),
      enforceSSL: true,
      deadLetterQueue: {
        queue: registrationDeadLetterQueue,
        maxReceiveCount: REGISTRATION_MAX_RECEIVE_COUNT,
      },
    });
    const registrationStatusTable = new dynamodb.Table(this, 'RegistrationStatusTable', {
      tableName: producerRegistrationStatusTableName(config.envName),
      partitionKey: { name: 'attempt_id', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
      // Not rebuildable: each item is the terminal decision for one registration object version,
      // the finalizer's idempotence record, and the only home of a rejection reason (O-DDB-PITR-001).
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    const landingRole = this.buildLandingRole(config, landing);
    const finalizer = this.buildFinalizerFunction(props, dependencies);
    this.grantFinalization(config, finalizer);
    grantSpanExport(finalizer);
    finalizer.addEventSource(
      new sources.SqsEventSource(registrationQueue, {
        batchSize: BATCH_SIZE,
        maxConcurrency: RESERVED_CONCURRENCY,
        reportBatchItemFailures: false,
      }),
    );

    return {
      registrationQueue,
      registrationDeadLetterQueue,
      registrationStatusTable,
      landingRole,
      finalizer,
    };
  }

  private buildLandingRole(config: AppConfig, landing: EnabledLandingConfig): iam.Role {
    const trustedPipeline = new iam.ArnPrincipal(landing.pipelineRoleArn).withConditions({
      StringEquals: { 'sts:ExternalId': landing.externalId },
    });
    const role = new iam.Role(this, 'AdlyticsLandingRole', {
      roleName: `adlytics-${config.envName}-landing`,
      description:
        'Destination-owned role assumed by the configured external pipeline for incoming writes.',
      assumedBy: trustedPipeline,
    });
    role.addToPolicy(
      new iam.PolicyStatement({
        actions: ['s3:PutObject'],
        resources: [
          `arn:${this.partition}:s3:::${dataBucketName(config, 'semantic-v2-raw')}/${INCOMING_PREFIX}*`,
        ],
      }),
    );
    return role;
  }

  private dependenciesLayer(config: AppConfig): lambda.ILayerVersion {
    return lambda.LayerVersion.fromLayerVersionArn(
      this,
      'PythonDependencies',
      ssm.StringParameter.valueForStringParameter(
        this,
        paramPath(config.envName, 'dependenciesLayerArn'),
      ),
    );
  }

  /**
   * The function that loads one delivery.
   *
   * The log group is declared rather than left to `logRetention`, which provisions a Node singleton
   * to call `PutRetentionPolicy` at deploy time and lands an x86_64 function in a stack where every
   * function is arm64 — the same reason `ApiStack` declares its own.
   */
  private buildFunction(
    props: IngestionStackProps,
    dependencies: lambda.ILayerVersion,
    handoff: ProjectionHandoffControls,
  ): lambda.Function {
    const { config } = props;
    const functionName = `adlytics-${config.envName}-IngestionFunction`;

    const logGroup = new logs.LogGroup(this, 'IngestionFunctionLogs', {
      logGroupName: `/aws/lambda/${functionName}`,
      retention: config.logRetentionDays,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    const executionRole = new iam.Role(this, 'IngestionExecutionRole', {
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      description: 'Least-privilege execution role for governed Semantic V2 ingestion.',
    });
    executionRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ['logs:CreateLogStream', 'logs:PutLogEvents'],
        resources: [`${logGroup.logGroupArn}:*`],
      }),
    );

    return new lambda.Function(this, 'IngestionFunction', {
      functionName,
      runtime: new lambda.Runtime(config.pythonRuntime, lambda.RuntimeFamily.PYTHON),
      architecture: this.lambdaArchitecture(config),
      code: lambda.Code.fromAsset(props.sourceAssetPath),
      handler: 'adapters.inbound.ingestion_handlers.load.handler',
      layers: [dependencies],
      environment: this.handlerEnvironment(config),
      memorySize: MEMORY_MB,
      timeout: cdk.Duration.seconds(TIMEOUT_SECONDS),
      // Load Path §2.5: the delivery-mapping control also renders this function's reserved
      // concurrency — exactly 0 whenever the mapping is disabled, so `GetFunctionConcurrency`
      // returns 0 in every quiesced and drain state; otherwise the two ordinary slots.
      reservedConcurrentExecutions: handoff.deliveryMappingEnabled ? RESERVED_CONCURRENCY : 0,
      role: executionRole,
      logGroup,
    });
  }

  private lambdaArchitecture(config: AppConfig): lambda.Architecture {
    return { arm64: lambda.Architecture.ARM_64 }[config.lambdaArchitecture];
  }

  private buildFinalizerFunction(
    props: IngestionStackProps,
    dependencies: lambda.ILayerVersion,
  ): lambda.Function {
    const { config } = props;
    const functionName = `adlytics-${config.envName}-ProducerFinalizer`;
    const logGroup = new logs.LogGroup(this, 'ProducerFinalizerLogs', {
      logGroupName: `/aws/lambda/${functionName}`,
      retention: config.logRetentionDays,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    const executionRole = new iam.Role(this, 'ProducerFinalizerExecutionRole', {
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      description: 'Least-privilege execution role for registered source finalization.',
    });
    executionRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ['logs:CreateLogStream', 'logs:PutLogEvents'],
        resources: [`${logGroup.logGroupArn}:*`],
      }),
    );
    return new lambda.Function(this, 'ProducerFinalizer', {
      functionName,
      runtime: new lambda.Runtime(config.pythonRuntime, lambda.RuntimeFamily.PYTHON),
      architecture: this.lambdaArchitecture(config),
      code: lambda.Code.fromAsset(props.sourceAssetPath),
      handler: 'adapters.inbound.producer_handlers.finalizer.handler',
      layers: [dependencies],
      environment: {
        ...this.handlerEnvironment(config),
        ADL_REGISTRATION_STATUS_TABLE: producerRegistrationStatusTableName(config.envName),
      },
      memorySize: MEMORY_MB,
      timeout: cdk.Duration.seconds(FINALIZER_TIMEOUT_SECONDS),
      reservedConcurrentExecutions: RESERVED_CONCURRENCY,
      role: executionRole,
      logGroup,
    });
  }

  /**
   * Everything the handler needs, derived at synth and never read from SSM at runtime.
   *
   * Every name here is a pure function of the environment name through `dataBucketName`,
   * `semanticV2AthenaWorkgroupName`, `semanticV2DatabaseName`, and `semanticV2StateTableName`.
   * Consequently this function needs **no `ssm:GetParameter` at all** — unlike a CLI, which may
   * resolve published parameters because a developer at a terminal has none of this context.
   *
   * The Cognito and cookie keys are here for a value none of them serves: `settings()` resolves
   * configuration as a whole, so a container missing any required key fails at import. Withholding
   * one would not be an authorization boundary, it would be a container that cannot start. What this
   * function may touch is settled by the IAM below.
   *
   * `ADL_COGNITO_CLIENT_SECRET_ID` is deliberately absent, the same as the tool functions: this is
   * not the BFF, it loads through `settings_without_client_secret`, and naming a secret it has no
   * grant to read would imply it has something to do with authentication.
   */
  private handlerEnvironment(config: AppConfig): Record<string, string> {
    return {
      ADL_ENVIRONMENT: config.envName,
      ADL_AWS_REGION: config.region,
      // Placeholders that satisfy `Settings` without describing anything real. A load never
      // authenticates a user, and inventing a plausible pool id would be worse than an obvious
      // stand-in: the next reader would try to find it.
      ADL_COGNITO_USER_POOL_ID: 'unused-by-ingestion',
      ADL_COGNITO_CLIENT_ID: 'unused-by-ingestion',
      ADL_COGNITO_LOGIN_DOMAIN: `auth.${config.envName}.invalid`,
      ADL_APP_BASE_URL: `https://app.${config.envName}.invalid`,
      ADL_API_BASE_URL: `https://api.${config.envName}.invalid`,
      ADL_COOKIE_SCOPE: config.auth.cookieScope,
      ADL_COOKIE_DOMAIN: config.auth.cookieDomain,
      ADL_REFRESH_COOKIE_MAX_AGE_SECONDS: String(config.auth.refreshCookieMaxAgeSeconds),
      // Permanent V2 landing and execution surfaces. The generic interactive setting serves
      // measurement composition; ingestion reads its explicit V2 storage-layer settings below.
      ADL_RAW_BUCKET: dataBucketName(config, 'semantic-v2-raw'),
      ADL_ATHENA_WORKGROUP: semanticV2AthenaWorkgroupName(config.envName, 'interactive'),
      ADL_SEMANTIC_ATTEMPTS_TABLE: semanticV2StateTableName(config.envName, 'attempts'),
      ADL_SEMANTIC_LEASES_TABLE: semanticV2StateTableName(config.envName, 'leases'),
      ADL_SEMANTIC_PUBLICATIONS_TABLE: semanticV2StateTableName(config.envName, 'publications'),
      ADL_SEMANTIC_V2_BUCKET: dataBucketName(config, 'semantic-v2'),
      ADL_SEMANTIC_V2_ATHENA_BAKE_WORKGROUP: semanticV2AthenaWorkgroupName(
        config.envName,
        'bake',
      ),
      ADL_SEMANTIC_V2_SOURCE_DATABASE: semanticV2DatabaseName(
        config.envName,
        'source-aligned',
      ),
      ADL_SEMANTIC_V2_CANDIDATE_DATABASE: semanticV2DatabaseName(config.envName, 'candidate'),
      ADL_SEMANTIC_V2_CURRENT_DATABASE: semanticV2DatabaseName(config.envName, 'current'),
      // Zero, and this is a correctness setting rather than a cost one. A load must never see a
      // cached result: it would report a write that did not happen.
      ADL_ATHENA_RESULT_REUSE_MAX_AGE_MINUTES: '0',
      ADL_ATHENA_QUERY_BATCH_MAX_CONCURRENCY: String(config.athenaQueryBatchMaxConcurrency),
      // The whole §11 object, including the handoff state the delivery Lambda resolves into its
      // projection composition. Its accessor is strict over the full object, and a subset here
      // would fail every delivery container at import rather than any one request.
      ...stage1LimitsEnvironment(config),
      POWERTOOLS_METRICS_NAMESPACE: 'Adlytics',
      POWERTOOLS_LOG_LEVEL: 'INFO',
      ADL_TRACING_ENABLED: String(config.transactionSearchEnabled),
      ADL_TRACE_SAMPLE_RATIO: String(config.traceSampleRatio),
    };
  }

  private grantFinalization(config: AppConfig, fn: lambda.Function): void {
    const raw = dataBucketName(config, 'semantic-v2-raw');
    fn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['s3:GetObjectVersion'],
        resources: [`arn:${this.partition}:s3:::${raw}/${INCOMING_PREFIX}*`],
      }),
    );
    fn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['s3:GetObject', 's3:PutObject'],
        resources: [`arn:${this.partition}:s3:::${raw}/amc/*`],
      }),
    );
    fn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:UpdateItem'],
        resources: [
          `arn:${this.partition}:dynamodb:${this.region}:${this.account}:table/${producerRegistrationStatusTableName(config.envName)}`,
        ],
      }),
    );
  }

  /**
   * Exactly what a load needs, one statement per service, on names derived at synth.
   *
   * The inverse of `AgentStack.grantCuratedRead`, and the contrast is the point: a tool reads the
   * catalogue and the lake and may never write either, while this writes both and must never see
   * the objectives table.
   */
  /** The raw zone and coordinator table an automatic requester's admission reads (S6). */
  private admissionTargets(config: AppConfig): AdmissionRequesterTargets {
    return {
      rawBucketArn: `arn:${this.partition}:s3:::${dataBucketName(config, 'semantic-v2-raw')}`,
      publicationsArn: this.publicationsArn(config),
    };
  }

  private publicationsArn(config: AppConfig): string {
    return this.formatArn({
      service: 'dynamodb',
      resource: 'table',
      resourceName: semanticV2StateTableName(config.envName, 'publications'),
    });
  }

  private grantLoad(config: AppConfig, fn: lambda.Function): void {
    this.grantRawRead(config, fn);
    this.grantSemanticV2(config, fn);
  }

  /** Read only from the permanent Semantic V2 landing bucket. */
  private grantRawRead(config: AppConfig, fn: lambda.Function): void {
    const raw = dataBucketName(config, 'semantic-v2-raw');

    // The handler always names and verifies an immutable VersionId. It cannot read a current head,
    // enumerate the bucket, erase evidence, or inspect the optional producer's incoming prefix.
    // These are the three source-system roots declared by the current data contracts; adding a
    // source requires extending this grant with its contract change rather than widening to `*`.
    fn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['s3:GetObjectVersion'],
        resources: ['amc', 'vendor_central', 'vendor_data_kiosk'].map(
          (source) => `arn:${this.partition}:s3:::${raw}/${source}/*`,
        ),
      }),
    );
  }

  /** Permanent Semantic V2 mutation authority, with no retired serving-plane access. */
  private grantSemanticV2(config: AppConfig, fn: lambda.Function): void {
    const semanticBucket = dataBucketName(config, 'semantic-v2');
    const resultsBucket = dataBucketName(config, 'v2-results');
    const attempts = semanticV2StateTableName(config.envName, 'attempts');
    const leases = semanticV2StateTableName(config.envName, 'leases');
    const publications = semanticV2StateTableName(config.envName, 'publications');
    const databases = [
      semanticV2DatabaseName(config.envName, 'source-aligned'),
      semanticV2DatabaseName(config.envName, 'candidate'),
      semanticV2DatabaseName(config.envName, 'current'),
    ];
    const role = fn.role;
    if (role === undefined) {
      throw new Error('the delivery function must own an execution role.');
    }
    // Every S3, Athena and Glue grant of this path lives in one customer-managed policy attached to
    // this role only. The role's inline aggregate reached IAM's 10,240-character limit (2026-10-09:
    // 10,186 live), and past CDK's own estimate the default policy spills silently into an
    // unnamed managed policy; `test/iam-policy-size.test.ts` holds every role under the limit with
    // headroom and refuses any spill. The statements are unchanged; only their policy moved.
    const storage = new iam.ManagedPolicy(this, 'SemanticV2StoragePolicy', {
      description: 'Semantic V2 objects, batch results, query workgroup and catalog for delivery.',
      roles: [role],
    });

    storage.addStatements(
      new iam.PolicyStatement({
        actions: ['s3:GetObject', 's3:PutObject'],
        resources: [
          `arn:${this.partition}:s3:::${semanticBucket}/immutable/source-aligned/*`,
          `arn:${this.partition}:s3:::${semanticBucket}/immutable/canonical/candidate/*`,
          `arn:${this.partition}:s3:::${semanticBucket}/immutable/canonical/successful/*`,
        ],
      }),
    );
    // Write-discipline §2.6: identity-mapping claim-check documents, written once under their
    // SHA-256 (If-None-Match) and read back only through digest verification. The role's inline
    // policies already sit at IAM's 10,240-character aggregate role-policy limit (2026-10-09:
    // 10,186), so this one statement lives in its own customer-managed policy, attached to this
    // role only.
    const identity = new iam.ManagedPolicy(this, 'IdentityEvidencePolicy', {
      description: 'Write-once claim-check documents of identity mappings (write-discipline 2.6).',
      roles: [role],
      statements: [
        new iam.PolicyStatement({
          actions: ['s3:GetObject', 's3:PutObject'],
          resources: [`arn:${this.partition}:s3:::${semanticBucket}/immutable/identity-evidence/*`],
        }),
      ],
    });
    storage.addStatements(
      new iam.PolicyStatement({
        actions: [
          's3:GetObject',
          's3:PutObject',
          's3:DeleteObject',
          's3:AbortMultipartUpload',
          's3:ListMultipartUploadParts',
        ],
        resources: [
          `arn:${this.partition}:s3:::${semanticBucket}/source-aligned/*`,
          `arn:${this.partition}:s3:::${semanticBucket}/canonical/candidate/*`,
          `arn:${this.partition}:s3:::${semanticBucket}/canonical/current/*`,
        ],
      }),
    );
    storage.addStatements(
      new iam.PolicyStatement({
        actions: ['s3:ListBucket'],
        resources: [`arn:${this.partition}:s3:::${semanticBucket}`],
        conditions: {
          StringLike: {
            's3:prefix': [
              'source-aligned/*',
              'canonical/candidate/*',
              'canonical/current/*',
            ],
          },
        },
      }),
    );
    storage.addStatements(
      new iam.PolicyStatement({
        actions: ['s3:GetBucketLocation'],
        resources: [`arn:${this.partition}:s3:::${semanticBucket}`],
      }),
    );
    // SEMANTIC-M1: load starts on BAKE and writes its own results prefix; pre-cutover `batch`
    // executions stay readable (terminal reads only, no write and no start).
    storage.addStatements(
      new iam.PolicyStatement({
        actions: ['s3:GetObject', 's3:PutObject', 's3:AbortMultipartUpload'],
        resources: [`arn:${this.partition}:s3:::${resultsBucket}/bake/*`],
      }),
    );
    storage.addStatements(
      new iam.PolicyStatement({
        actions: ['s3:GetObject'],
        resources: [`arn:${this.partition}:s3:::${resultsBucket}/batch/*`],
      }),
    );
    storage.addStatements(
      new iam.PolicyStatement({
        actions: ['s3:ListBucket'],
        resources: [`arn:${this.partition}:s3:::${resultsBucket}`],
        conditions: {
          StringLike: { 's3:prefix': ['bake/*', 'batch/*'] },
        },
      }),
    );
    storage.addStatements(
      new iam.PolicyStatement({
        actions: ['s3:GetBucketLocation'],
        resources: [`arn:${this.partition}:s3:::${resultsBucket}`],
      }),
    );
    storage.addStatements(
      new iam.PolicyStatement({
        actions: [
          'athena:StartQueryExecution',
          'athena:GetQueryExecution',
          'athena:GetQueryResults',
          'athena:StopQueryExecution',
        ],
        resources: [
          `arn:${this.partition}:athena:${this.region}:${this.account}:workgroup/${semanticV2AthenaWorkgroupName(config.envName, 'bake')}`,
        ],
      }),
    );
    storage.addStatements(
      new iam.PolicyStatement({
        actions: [
          'athena:GetQueryExecution',
          'athena:GetQueryResults',
          'athena:StopQueryExecution',
        ],
        resources: [
          `arn:${this.partition}:athena:${this.region}:${this.account}:workgroup/${semanticV2AthenaWorkgroupName(config.envName, 'batch')}`,
        ],
      }),
    );
    storage.addStatements(
      new iam.PolicyStatement({
        actions: [
          'glue:GetDatabase',
          'glue:GetTable',
          'glue:GetTables',
          'glue:GetPartitions',
          'glue:UpdateTable',
          'glue:BatchCreatePartition',
          'glue:CreatePartition',
          'glue:UpdatePartition',
          'glue:DeletePartition',
          'glue:BatchDeletePartition',
        ],
        resources: [
          `arn:${this.partition}:glue:${this.region}:${this.account}:catalog`,
          ...databases.flatMap((database) => [
            `arn:${this.partition}:glue:${this.region}:${this.account}:database/${database}`,
            `arn:${this.partition}:glue:${this.region}:${this.account}:table/${database}/*`,
          ]),
        ],
      }),
    );
    // R041-1: the function waits for both managed policies, as it waits for its default policy, so
    // new code never runs before its grant exists (fail-closed AccessDenied on a create, or on an
    // update that ships code and policy together).
    fn.node.addDependency(storage, identity);
    const attemptsArn = `arn:${this.partition}:dynamodb:${this.region}:${this.account}:table/${attempts}`;
    const leasesArn = `arn:${this.partition}:dynamodb:${this.region}:${this.account}:table/${leases}`;
    const publicationsArn = `arn:${this.partition}:dynamodb:${this.region}:${this.account}:table/${publications}`;
    // Partition identities of the retained publication path (write discipline §2.4, "what does
    // not move"): the sidecar's attempts, leases, CURRENT/PUBLICATION# items, and the transitional
    // zero-row servable advance are all keyed by `<dataset>|...`. Stage-1 authority adds its own
    // prefixes on the publications table and `TABLE#` on the leases table.
    const canonicalKeys = [...CANONICAL_DATASET_KEYS];
    const publicationKeys = ['TABLE_ATTEMPT#*', 'WRITE_PLAN#*', 'STATUS_FLOW#*', ...canonicalKeys];
    const retained = this.grantRetainedPublicationPath(fn, {
      attemptsArn,
      leasesArn,
      publicationsArn,
      canonicalKeys,
    });
    // The dataset-keyed publication grants grow by one key per canonical table, so they live in
    // a named managed policy rather than the inline default policy, which has to stay under the
    // aggregate inline limit (`test/iam-policy-size.test.ts`; past CDK's estimate it would spill
    // into an unnamed overflow policy). The statements are unchanged; only their policy moved.
    const publicationKeyed = new iam.ManagedPolicy(this, 'SemanticPublicationKeysPolicy', {
      description: 'Dataset-keyed semantic publication reads and writes for delivery.',
      roles: [role],
      statements: [
        new iam.PolicyStatement({
          actions: ['dynamodb:GetItem'],
          resources: [publicationsArn],
          conditions: {
            'ForAllValues:StringLike': { 'dynamodb:LeadingKeys': publicationKeys },
          },
        }),
        new iam.PolicyStatement({
          actions: ['dynamodb:GetItem'],
          resources: [publicationsArn],
          conditions: {
            'ForAllValues:StringLike': { 'dynamodb:LeadingKeys': publicationKeys },
            StringEquals: { 'dynamodb:EnclosingOperation': 'TransactGetItems' },
          },
        }),
        // Direct (non-transactional) updates: Stage-1 attempt records, and the transitional
        // zero-row servable advance on a CURRENT item (§2.1 rule 4 follow-up in `load.py`).
        new iam.PolicyStatement({
          actions: ['dynamodb:UpdateItem'],
          resources: [publicationsArn],
          conditions: {
            'ForAllValues:StringLike': {
              'dynamodb:LeadingKeys': ['TABLE_ATTEMPT#*', ...canonicalKeys],
            },
          },
        }),
        new iam.PolicyStatement({
          actions: ['dynamodb:PutItem', 'dynamodb:UpdateItem', 'dynamodb:ConditionCheckItem'],
          resources: [publicationsArn],
          conditions: {
            'ForAllValues:StringLike': { 'dynamodb:LeadingKeys': publicationKeys },
            StringEquals: { 'dynamodb:EnclosingOperation': 'TransactWriteItems' },
          },
        }),
      ],
    });
    // R041-1: new code never runs before its grant exists.
    fn.node.addDependency(publicationKeyed);
    // Root-clock readback: every successor lifecycle event repeats the flow's durable root
    // clock, which the owner strongly queries from the retained `EVENT#` items under its own
    // `STATUS_FLOW#` partition (operational-status LLD §6.4).
    fn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['dynamodb:Query'],
        resources: [publicationsArn],
        conditions: {
          'ForAllValues:StringLike': { 'dynamodb:LeadingKeys': ['STATUS_FLOW#*'] },
        },
      }),
    );
    // Leases: Stage-1 table fences are read directly under `TABLE#`; their transactional
    // mutation, and every dataset-keyed partition lease, are granted on the retained path policy.
    fn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['dynamodb:GetItem'],
        resources: [leasesArn],
        conditions: {
          'ForAllValues:StringLike': { 'dynamodb:LeadingKeys': ['TABLE#*'] },
        },
      }),
    );
    // Statements moved out of the default policy must exist in their new policies before the
    // default policy drops them, or a running invocation sees AccessDenied mid-deploy. So the
    // default policy updates only after both destination policies.
    const defaultPolicy = role.node.tryFindChild('DefaultPolicy');
    if (defaultPolicy === undefined) {
      throw new Error('the delivery role must carry its default policy.');
    }
    defaultPolicy.node.addDependency(publicationKeyed, retained);
  }

  /**
   * The retained publication path's own authority (write discipline §2.4, "what does not move").
   *
   * A second inline policy rather than more statements on the default one: the default policy
   * already carries the Stage-1 key-conditioned grants and sits near the inline-policy size
   * quota, past which CDK silently moves the overflow into a managed policy that this stack's
   * handoff ordering and IAM tests never see. Keeping the retained grants in their own named
   * policy keeps every statement inline, visible, and attributable to the path that needs it.
   */
  private grantRetainedPublicationPath(
    fn: lambda.Function,
    arns: Readonly<{
      attemptsArn: string;
      leasesArn: string;
      publicationsArn: string;
      canonicalKeys: readonly string[];
    }>,
  ): iam.Policy {
    const role = fn.role;
    if (role === undefined) {
      throw new Error('the delivery function must own an execution role.');
    }
    return new iam.Policy(this, 'RetainedPublicationPathPolicy', {
      roles: [role],
      statements: [
        // Attempt history is keyed by attempt id, not by dataset: STARTED/terminal appends
        // (PutItem), the idempotent replay read (Query by attempt id), and exact-replay
        // attestation (GetItem inside TransactGetItems).
        new iam.PolicyStatement({
          actions: ['dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:Query'],
          resources: [arns.attemptsArn],
        }),
        // Dimension history discovers the scope's CURRENT partitions through the GSI.
        new iam.PolicyStatement({
          actions: ['dynamodb:Query'],
          resources: [`${arns.publicationsArn}/index/scope-current`],
        }),
        // Partition leases are acquired and released with direct conditional updates keyed by
        // the canonical partition identity; their commit-time condition check rides the
        // publication transaction (the next statement).
        new iam.PolicyStatement({
          actions: ['dynamodb:UpdateItem'],
          resources: [arns.leasesArn],
          conditions: {
            'ForAllValues:StringLike': { 'dynamodb:LeadingKeys': [...arns.canonicalKeys] },
          },
        }),
        // The partition lease's commit-time check and the Stage-1 table fence, both inside the
        // publication transaction.
        new iam.PolicyStatement({
          actions: ['dynamodb:UpdateItem', 'dynamodb:ConditionCheckItem'],
          resources: [arns.leasesArn],
          conditions: {
            'ForAllValues:StringLike': {
              'dynamodb:LeadingKeys': ['TABLE#*', ...arns.canonicalKeys],
            },
            StringEquals: { 'dynamodb:EnclosingOperation': 'TransactWriteItems' },
          },
        }),
      ],
    });
  }

  /**
   * The rule that turns a landed manifest into a queued message.
   *
   * On the default event bus, because that is where S3 delivers and a custom bus would need a rule
   * forwarding to it for no gain.
   *
   * The pattern filters on the bucket *and* the suffix. Filtering only on the suffix would match a
   * manifest in any bucket in the account, which is a rule that works until someone else's bucket
   * has one.
   */
  private buildRule(config: AppConfig): void {
    new events.Rule(this, 'ManifestLanded', {
      ruleName: `adlytics-${config.envName}-manifest-landed`,
      description:
        'A landed manifest means a delivery is complete (ADR-030). Enqueues it for curation.',
      eventPattern: {
        source: ['aws.s3'],
        detailType: ['Object Created'],
        detail: {
          bucket: {
            name: [dataBucketName(config, 'semantic-v2-raw')],
          },
          object: { key: [{ suffix: MANIFEST_SUFFIX }] },
        },
      },
      targets: [
        new targets.SqsQueue(this.queue, {
          // No `deadLetterQueue` on the target. This one is about EventBridge failing to *deliver*
          // to the queue, which is a different failure from the handler refusing the delivery, and
          // conflating them in one queue would make a redrive replay two unrelated things.
          retryAttempts: 3,
        }),
      ],
    });
  }

  private buildRegistrationRule(config: AppConfig, registrationQueue: sqs.Queue): void {
    new events.Rule(this, 'RegistrationLanded', {
      ruleName: `adlytics-${config.envName}-registration-landed`,
      description: 'An incoming registration is complete and ready for manifest finalization.',
      eventPattern: {
        source: ['aws.s3'],
        detailType: ['Object Created'],
        detail: {
          bucket: { name: [dataBucketName(config, 'semantic-v2-raw')] },
          object: {
            key: [
              {
                wildcard: `${INCOMING_PREFIX}*/*/*/${REGISTRATION_SUFFIX}`,
              },
            ],
          },
        },
      },
      targets: [new targets.SqsQueue(registrationQueue, { retryAttempts: 3 })],
    });
  }

  /**
   * What a human is told about, and what is deliberately kept as a product signal.
   *
   * A dead-lettered message is undelivered work. A blocked delivery is a terminal V2 decision,
   * so it is counted rather than queued. Queue age catches work that stays open past the protocol
   * ceiling. The shared `Errors` metric pages only for invariant and dependency failures, per fixed
   * ingestion operation and, through the catch-all, for every other operation; domain and
   * validation outcomes remain product signals.
   */
  private buildCoreAlarms(config: AppConfig, fn: lambda.Function): void {
    const action = new actions.SnsAction(this.staffedTopic);

    // Arrivals, not standing depth: a letter left in the queue must not hold the alarm in ALARM
    // and hide the next one (ADR-133).
    arrivalAlarm(this, 'DeadLetteredDeliveries', {
      alarmName: `adlytics-${config.envName}-ingestion-dead-lettered`,
      alarmDescription:
        'A delivery was never loaded. Corrupt bytes, or a load that failed three times. ' +
        'The message holds the manifest key; the ledger says whether an attempt started.',
      queue: this.deadLetterQueue,
      action,
    });

    // Tracing report I2 (owner, 2026-10-06): a delivery older than every receive's visibility is
    // stuck, not retrying; and an invariant or dependency failure in any ingestion function pages.
    const oldest = new cloudwatch.Alarm(this, 'OldestDelivery', {
      alarmName: `adlytics-${config.envName}-ingestion-oldest`,
      alarmDescription:
        'A delivery marker is older than every receive its queue allows, so it is stuck rather '
        + 'than retrying.',
      metric: this.queue.metricApproximateAgeOfOldestMessage({
        period: cdk.Duration.minutes(1),
        statistic: cloudwatch.Stats.MAXIMUM,
      }),
      threshold: queueAgeCeilingSeconds(
        MAX_RECEIVE_COUNT,
        TIMEOUT_SECONDS + VISIBILITY_MARGIN_SECONDS,
      ),
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      evaluationPeriods: 1,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    oldest.addAlarmAction(action);

    for (const operation of INGESTION_ERROR_OPERATIONS) {
      const suffix = errorAlarmSuffix(operation);
      const alarming = new cloudwatch.Alarm(this, `AlarmingIngestionErrors-${suffix}`, {
        alarmName: `adlytics-${config.envName}-ingestion-alarming-errors-${suffix}`,
        alarmDescription:
          `Ingestion operation ${operation} emitted an invariant or dependency failure through the `
          + 'shared Errors metric (ADR-009). Domain and validation refusals remain product signals.',
        metric: new cloudwatch.MathExpression({
          expression: alarmingErrorsQuery(operation),
          period: cdk.Duration.minutes(5),
        }),
        threshold: 1,
        comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
        evaluationPeriods: 1,
        treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      });
      alarming.addAlarmAction(action);
    }

    // ADR-133 clause 4: every other operation that reports through `report_error`, including the
    // dynamic ones (per tool, per route), reaches the staffed destination through one
    // multi-time-series alarm with one contributor per Operation. The operations with their own
    // alarm are excluded so one failure pages once; the metric reconciler's alarm exists exactly
    // when its stack is provisioned. Limits [verified 2026-10-10,
    // https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/alarm-limits.html]: a query
    // returning more than 500 time series evaluates as PARTIAL_DATA, and at most 100 contributors
    // are tracked in ALARM ("100+" beyond that, the alarm stays in ALARM). The 2× headroom rule
    // cannot hold for that cap (about 100 operations) and no query can split an open operation set
    // into bounded groups (WHERE has only =, != and AND), so past 100 concurrently failing
    // operations the 101st is not named in a new notification; the runbook query lists every one
    // (owner decision 2026-10-10). The `Adlytics` namespace has no
    // environment dimension, so like the per-operation alarms it counts every environment in the
    // account. ADR-148: the Presentation page operations are held by their page alarms
    // (`ApiStack`), whose OK means an observed success, so the catch-all excludes them too.
    const catchAll = new cloudwatch.Alarm(this, 'AlarmingErrorsByOperation', {
      alarmName: `adlytics-${config.envName}-alarming-errors-by-operation`,
      alarmDescription:
        'An operation without its own alarm emitted an invariant or dependency failure through the '
        + 'shared Errors metric (ADR-009, ADR-133). The contributor names the Operation; its log '
        + 'record carries the ErrorCode. Domain and validation refusals remain product signals. '
        + 'If the state reason reads "100+", more operations are failing than it names: run the '
        + `query in ${CATCH_ALL_RUNBOOK} to list every one.`
        + ' Presentation page operations are held by their page alarms instead (ADR-148).',
      metric: new cloudwatch.MathExpression({
        expression: alarmingErrorsByOperationQuery([
          ...INGESTION_ERROR_OPERATIONS,
          ...PAGE_OPERATIONS,
          ...(config.operationalStatus.provisioned ? [METRIC_RECONCILER_OPERATION] : []),
        ]),
        period: cdk.Duration.minutes(5),
      }),
      threshold: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      evaluationPeriods: 1,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    catchAll.addAlarmAction(action);

    // `Action=superseded` (every bucket older than CURRENT) is a product signal and is deliberately
    // not alarmed, so a bulk load of older data never holds this alarm in ALARM.
    const blocked = new cloudwatch.Alarm(this, 'BlockedPartitions', {
      alarmName: `adlytics-${config.envName}-ingestion-blocked`,
      alarmDescription:
        'A Semantic V2 delivery was blocked. Its data is absent and visibly so; it is not ' +
        'dead-lettered because the refusal is a terminal recorded decision.',
      metric: new cloudwatch.Metric({
        namespace: 'Adlytics',
        metricName: 'IngestPartitions',
        dimensionsMap: { Action: 'blocked' },
        period: cdk.Duration.minutes(15),
        statistic: cloudwatch.Stats.SUM,
      }),
      threshold: 0,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      evaluationPeriods: 1,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    blocked.addAlarmAction(action);

    // Not an alarm and deliberately so: a duration approaching the timeout is the signal that would
    // force Step Functions, and it is worth watching before it is worth paging on. The metric exists
    // because `ingest.delivery` records it; naming it here is how the eventual alarm gets written
    // against something rather than invented.
    new cdk.CfnOutput(this, 'OutIngestionFunctionName', {
      value: fn.functionName,
      description:
        'Load duration is on the ingest.delivery span and this function log group. The ceiling ' +
        `is ${TIMEOUT_SECONDS}s and the load path allows 600s per statement.`,
    });
  }

  private buildProducerAlarms(config: AppConfig, producer: ProducerResources): void {
    const action = new actions.SnsAction(this.staffedTopic);
    arrivalAlarm(this, 'DeadLetteredRegistrations', {
      alarmName: `adlytics-${config.envName}-registration-dead-lettered`,
      alarmDescription:
        'A registration could not be decided after retries. The message binds its exact S3 version.',
      queue: producer.registrationDeadLetterQueue,
      action,
    });

    const rejected = new cloudwatch.Alarm(this, 'RejectedRegistrations', {
      alarmName: `adlytics-${config.envName}-registration-rejected`,
      alarmDescription:
        'A source registration was understood and rejected before a canonical manifest existed.',
      metric: new cloudwatch.Metric({
        namespace: 'Adlytics',
        metricName: 'ProducerRegistrations',
        dimensionsMap: { Action: 'rejected' },
        period: cdk.Duration.minutes(15),
        statistic: cloudwatch.Stats.SUM,
      }),
      threshold: 0,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      evaluationPeriods: 1,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    rejected.addAlarmAction(action);

    new cdk.CfnOutput(this, 'OutProducerFinalizerFunctionName', {
      value: producer.finalizer.functionName,
      description: 'Function that converts exact incoming registrations into canonical manifests.',
    });
  }

  /**
   * The core alarm topic ARN, so a customer can subscribe without editing this stack.
   *
   * This is the only value the disabled core publishes. Nothing reads the queue or function by
   * name, because nothing downstream of a load reacts to one — curated tables are the interface.
   */
  private publishCoreParameters(config: AppConfig): void {
    new ssm.StringParameter(this, 'ParamAlarmTopicArn', {
      parameterName: paramPath(config.envName, 'ingestionAlarmTopicArn'),
      stringValue: this.alarmTopic.topicArn,
      description:
        'Subscribe to this to be told when a delivery is dead-lettered or blocked. ' +
        'Unsubscribed by default: a destination baked into the template would be ours, not yours.',
    });
  }

  private publishProducerParameters(config: AppConfig, producer: ProducerResources): void {
    new ssm.StringParameter(this, 'ParamLandingRoleArn', {
      parameterName: paramPath(config.envName, 'landingRoleArn'),
      stringValue: producer.landingRole.roleArn,
    });
    new ssm.StringParameter(this, 'ParamLandingIncomingPrefix', {
      parameterName: paramPath(config.envName, 'landingIncomingPrefix'),
      stringValue: INCOMING_PREFIX,
    });
    new ssm.StringParameter(this, 'ParamRegistrationStatusTable', {
      parameterName: paramPath(config.envName, 'registrationStatusTableName'),
      stringValue: producer.registrationStatusTable.tableName,
    });

    new cdk.CfnOutput(this, 'OutLandingRoleArn', { value: producer.landingRole.roleArn });
    new cdk.CfnOutput(this, 'OutLandingBucketName', {
      value: dataBucketName(config, 'semantic-v2-raw'),
    });
    new cdk.CfnOutput(this, 'OutLandingIncomingPrefix', { value: INCOMING_PREFIX });
    new cdk.CfnOutput(this, 'OutRegistrationStatusTableName', {
      value: producer.registrationStatusTable.tableName,
    });
  }
}
