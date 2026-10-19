// SPDX-License-Identifier: MIT-0
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';

import * as cdk from 'aws-cdk-lib';
import * as cr from 'aws-cdk-lib/custom-resources';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import * as sources from 'aws-cdk-lib/aws-lambda-event-sources';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';

import { AppConfig, paramPath, semanticV2AthenaWorkgroupName, semanticV2DatabaseName } from '../config';
import { BAKER_RESERVED_CONCURRENCY, validateAthenaStartBudget } from '../config/athena-start-budget';
import { ROUTES } from '../generated/execution-graph-protocol';
import {
  executionGraphAuthority,
  grantGraphWorker,
  grantModeItemRead,
} from './execution-graph-authority';
import {
  arrivalAlarm,
  boundedStreamMapping,
  staffedAlarmAction,
} from '../constructs/observability/async-failure';
import { grantOwnLogWrites, lambdaRole } from '../mixins/logging';
import { applyFunctionErrorAlarms } from '../mixins/function-error-alarms';
import { applyRemovalPolicy, applyStandardTags } from '../mixins/tagging';
import { grantSpanExport, tracingEnvironment } from '../telemetry';

export interface PublisherStackProps extends cdk.StackProps {
  readonly config: AppConfig;
  /** Prebuilt Python source created by `make build-lambdas`. */
  readonly sourceAssetPath: string;
  /** Packaged declared-page input whose bytes identify a publisher deployment generation. */
  readonly publisherInputPath: string;
}

const ENQUEUE_HANDLER = 'adapters.jobs.preset_artifact_publisher.enqueue.handler';
const BAKER_HANDLER = 'adapters.jobs.preset_artifact_publisher.baker.handler';
/** The bake queue's maxReceiveCount: the generated `routes.bake` (design §2.1), one source. */
const MAX_RECEIVE_COUNT = ROUTES.bake.max_receive_count;
const ENQUEUE_TIMEOUT_SECONDS = 60;
/**
 * Plan 4.6: one V3 bake message (the fan-out, one roster task or the finalizer) per invocation,
 * up to 900 s (`bake_tasks.lambda_timeout_seconds`); the queue's visibility (timeout + margin) is
 * 960 s, the bake claim's lease.
 */
const BAKE_TIMEOUT_SECONDS = 900;
/**
 * S6: Enqueue makes no Athena call; it kept the old shared value of eight only by coincidence, so
 * it owns that value here and the bake budget no longer moves it.
 */
const ENQUEUE_RESERVED_CONCURRENCY = 8;
const VISIBILITY_MARGIN_SECONDS = 60;

/**
 * Stable deployment generation for the exact publisher input packaged with the Lambda artifact.
 *
 * This is intentionally content-derived, never clock-derived: it makes the configured publisher
 * generation reproducible and changes only when the packaged declaration changes. The future
 * backfill provider must receive this value as its custom-resource property.
 */
export function publisherDeploymentGeneration(publisherInputPath: string): string {
  return createHash('sha256').update(readFileSync(publisherInputPath)).digest('hex');
}

/**
 * The one-line trailer `scripts/build-lambdas.sh` appends to the staged declaration: a digest of
 * every packaged byte, so the functions' configured generation tracks the code they run.
 */
const RUNTIME_GENERATION_TRAILER = /\n# publisher-runtime-generation: [a-f0-9]{64}\n$/;

/**
 * Stable generation of the declared page input alone: the staged declaration without the
 * runtime trailer (binding R5-1).
 *
 * `DeployBackfill` enqueues the complete declared page/variant set, so its property is bound to
 * the declarations that set is derived from, not to every packaged byte. A deploy that changes
 * only code or contracts leaves the property unchanged, CloudFormation sends the custom resource
 * no event, and the backfill stays unfired; a changed declaration re-fires it once.
 */
export function publisherDeclaredInputGeneration(publisherInputPath: string): string {
  const declared = readFileSync(publisherInputPath, 'utf8').replace(RUNTIME_GENERATION_TRAILER, '');
  return createHash('sha256').update(declared, 'utf8').digest('hex');
}

/** Disposable publication-stream enqueue and FIFO bake transport for retained preset artifacts. */
export class PublisherStack extends cdk.Stack {
  private readonly config: AppConfig;

  constructor(scope: Construct, id: string, props: PublisherStackProps) {
    super(scope, id, props);
    this.config = props.config;
    const { config } = props;
    const read = (key: Parameters<typeof paramPath>[1]): string =>
      ssm.StringParameter.valueForStringParameter(this, paramPath(config.envName, key));
    const dependencies = lambda.LayerVersion.fromLayerVersionArn(
      this,
      'PythonDependencies',
      read('dependenciesLayerArn'),
    );
    const publicationsTable = read('semanticV2PublicationsTableName');
    const publicationsStream = read('semanticV2PublicationsStreamArn');
    const semanticV2Bucket = read('semanticV2BucketName');
    const currentDatabase = read('semanticV2CurrentDatabase');
    const resultsBucket = read('semanticV2AthenaResultsBucketName');
    const artifactBucket = read('publisherArtifactBucketName');
    const pointerTable = read('publisherArtifactPointerTableName');
    const pointerTableArn = read('publisherArtifactPointerTableArn');
    const deploymentGeneration = publisherDeploymentGeneration(props.publisherInputPath);
    const declaredInputGeneration = publisherDeclaredInputGeneration(props.publisherInputPath);
    // S1: synth refuses a start budget over the contract (Baker 2 x Q among the seven rows).
    validateAthenaStartBudget(config);
    // Observation-only companion emission: present only when the development status slice is
    // provisioned; an absent URL disables the producer entirely inside the workload.
    const companionEnvironment = config.operationalStatus.provisioned
      ? { ADL_OPERATIONAL_STATUS_QUEUE_URL: read('operationalStatusQueueUrl') }
      : {};
    // S6 §2.8, §2.9: the graph table the baker consumes from and every publisher worker reads
    // its mode from, where the graph authority is provisioned. Its grants are §2.11 rows 5, 5b
    // and 6, below; the graph paths themselves stay unreachable until the mode reads GRAPH.
    const graph = executionGraphAuthority(config, this);
    const graphEnvironment = graph === undefined ? {} : { ADL_EXECUTION_GRAPH_TABLE: graph.tableName };

    const deadLetterQueue = new sqs.Queue(this, 'BakeDeadLetterQueue', {
      queueName: `adlytics-${config.envName}-preset-bake-dlq.fifo`,
      fifo: true,
      contentBasedDeduplication: true,
      encryption: sqs.QueueEncryption.SQS_MANAGED,
      enforceSSL: true,
      retentionPeriod: cdk.Duration.days(14),
    });
    const bakeQueue = new sqs.Queue(this, 'BakeQueue', {
      queueName: `adlytics-${config.envName}-preset-bake.fifo`,
      fifo: true,
      contentBasedDeduplication: true,
      encryption: sqs.QueueEncryption.SQS_MANAGED,
      enforceSSL: true,
      visibilityTimeout: cdk.Duration.seconds(BAKE_TIMEOUT_SECONDS + VISIBILITY_MARGIN_SECONDS),
      retentionPeriod: cdk.Duration.days(14),
      deadLetterQueue: { queue: deadLetterQueue, maxReceiveCount: MAX_RECEIVE_COUNT },
    });

    const enqueue = this.functionWithLogs({
      id: 'Enqueue',
      functionName: `adlytics-${config.envName}-PresetArtifactEnqueue`,
      handler: ENQUEUE_HANDLER,
      sourceAssetPath: props.sourceAssetPath,
      dependencies,
      memorySize: 512,
      timeoutSeconds: ENQUEUE_TIMEOUT_SECONDS,
      reservedConcurrency: ENQUEUE_RESERVED_CONCURRENCY,
      environment: {
        ...tracingEnvironment(config),
        ...companionEnvironment,
        ...graphEnvironment,
        ADL_SEMANTIC_PUBLICATIONS_TABLE: publicationsTable,
        ADL_PRESET_ARTIFACT_BAKE_QUEUE_URL: bakeQueue.queueUrl,
        ADL_PUBLISHER_DEPLOYMENT_GENERATION: deploymentGeneration,
      },
    });
    const baker = this.functionWithLogs({
      id: 'Baker',
      functionName: `adlytics-${config.envName}-PresetArtifactBaker`,
      handler: BAKER_HANDLER,
      sourceAssetPath: props.sourceAssetPath,
      dependencies,
      memorySize: 1024,
      timeoutSeconds: BAKE_TIMEOUT_SECONDS,
      reservedConcurrency: BAKER_RESERVED_CONCURRENCY,
      environment: {
        ...tracingEnvironment(config),
        ...companionEnvironment,
        ...graphEnvironment,
        ADL_PRESET_ARTIFACT_BUCKET: artifactBucket,
        ADL_PRESET_ARTIFACT_POINTER_TABLE: pointerTable,
        ADL_PUBLISHER_DEPLOYMENT_GENERATION: deploymentGeneration,
        // Retained campaign/retail measurement: the baker resolves publications, canonical
        // objects, and Athena the way InsightPrecomputeStack's worker does, on the BAKE workgroup.
        ADL_SEMANTIC_PUBLICATIONS_TABLE: publicationsTable,
        ADL_SEMANTIC_V2_BUCKET: semanticV2Bucket,
        ADL_SEMANTIC_V2_ATHENA_BAKE_WORKGROUP: semanticV2AthenaWorkgroupName(config.envName, 'bake'),
        ADL_SEMANTIC_V2_CURRENT_DATABASE: currentDatabase,
        ADL_ATHENA_RESULT_REUSE_MAX_AGE_MINUTES: String(config.athenaResultReuseMaxAgeMinutes),
        // Q: the invocation-shared AthenaBatchAdmission capacity and the sources' fan-out width.
        ADL_ATHENA_QUERY_BATCH_MAX_CONCURRENCY: String(config.athenaStartBudget.bakerStartCap),
        // The optional §5.3 insight-fill child: after a pointer advance the baker admits the
        // advanced revision's declared portfolio slots to the durable continuation queue.
        ADL_INSIGHT_CONTINUATION_QUEUE_URL: read('insightContinuationQueueUrl'),
        ADL_INSIGHT_PROMPT_REGISTRY_TABLE: read('insightPromptRegistryTableName'),
      },
    });

    const backfill = this.functionWithLogs({
      id: 'Backfill',
      functionName: `adlytics-${config.envName}-PresetArtifactBackfill`,
      handler: 'adapters.jobs.preset_artifact_publisher.backfill.handler',
      sourceAssetPath: props.sourceAssetPath,
      dependencies,
      memorySize: 512,
      timeoutSeconds: ENQUEUE_TIMEOUT_SECONDS,
      reservedConcurrency: 1,
      environment: {
        ...tracingEnvironment(config),
        ...graphEnvironment,
        ADL_PRESET_ARTIFACT_BAKE_QUEUE_URL: bakeQueue.queueUrl,
        ADL_PUBLISHER_DEPLOYMENT_GENERATION: deploymentGeneration,
      },
    });
    for (const fn of [enqueue, baker, backfill]) {
      grantSpanExport(fn);
    }

    const publicationsArn = `arn:${this.partition}:dynamodb:${this.region}:${this.account}:table/${publicationsTable}`;
    enqueue.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['dynamodb:DescribeStream', 'dynamodb:GetRecords', 'dynamodb:GetShardIterator', 'dynamodb:ListStreams'],
        resources: [publicationsStream],
      }),
    );
    enqueue.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['dynamodb:GetItem', 'dynamodb:TransactGetItems'],
        resources: [publicationsArn],
      }),
    );
    bakeQueue.grantSendMessages(enqueue);
    if (config.operationalStatus.provisioned) {
      const statusQueueArn = read('operationalStatusQueueArn');
      for (const fn of [enqueue, baker]) {
        fn.addToRolePolicy(new iam.PolicyStatement({
          sid: 'EmitOperationalStatusCompanions',
          actions: ['sqs:SendMessage', 'sqs:GetQueueAttributes', 'sqs:GetQueueUrl'],
          resources: [statusQueueArn],
        }));
      }
    }
    bakeQueue.grantSendMessages(backfill);
    baker.addToRolePolicy(
      new iam.PolicyStatement({
        // TransactWriteItems authorizes each item's underlying action, so the pointer
        // transaction's conditional Puts and the history-expiry Update need their direct
        // actions too — the same finding the seed and ingestion roles recorded.
        actions: ['dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:UpdateItem', 'dynamodb:TransactGetItems', 'dynamodb:TransactWriteItems'],
        resources: [pointerTableArn],
      }),
    );
    // The §5.3 insight-fill child resolves each slot's active prompt pointer and admits
    // continuation envelopes; the continuation worker owns generation and cache writes.
    baker.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['sqs:SendMessage', 'sqs:GetQueueAttributes', 'sqs:GetQueueUrl'],
        resources: [read('insightContinuationQueueArn')],
      }),
    );
    baker.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['dynamodb:GetItem'],
        resources: [read('insightPromptRegistryTableArn')],
      }),
    );
    // Bodies land under pages/* and their private custody manifests under
    // custody/<page>/<variant>/<revision>.json — both create-once immutable writes.
    baker.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['s3:GetObject', 's3:PutObject'],
        resources: [
          `arn:${this.partition}:s3:::${artifactBucket}/pages/*`,
          `arn:${this.partition}:s3:::${artifactBucket}/custody/*`,
        ],
      }),
    );
    // Retained measurement mirrors InsightPrecomputeStack's worker: publication reads,
    // canonical CURRENT objects, BAKE-workgroup Athena, current Glue metadata, and its own
    // `v2-results/bake/*` results I/O (SEMANTIC-M1).
    baker.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['dynamodb:GetItem', 'dynamodb:TransactGetItems'],
        resources: [publicationsArn],
      }),
    );
    baker.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['dynamodb:Query'],
        resources: [`${publicationsArn}/index/scope-current`],
      }),
    );
    baker.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['s3:GetObject'],
        resources: [
          `arn:${this.partition}:s3:::${semanticV2Bucket}/canonical/current/*`,
          `arn:${this.partition}:s3:::${semanticV2Bucket}/immutable/canonical/successful/*`,
        ],
      }),
    );
    baker.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['s3:ListBucket'],
        resources: [`arn:${this.partition}:s3:::${semanticV2Bucket}`],
        conditions: { StringLike: { 's3:prefix': ['canonical/current/*'] } },
      }),
    );
    baker.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['athena:StartQueryExecution', 'athena:GetQueryExecution', 'athena:GetQueryResults', 'athena:StopQueryExecution'],
        resources: [`arn:${this.partition}:athena:${this.region}:${this.account}:workgroup/${semanticV2AthenaWorkgroupName(config.envName, 'bake')}`],
      }),
    );
    const database = semanticV2DatabaseName(config.envName, 'current');
    baker.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['glue:GetDatabase', 'glue:GetTable', 'glue:GetTables', 'glue:GetPartitions'],
        resources: [
          `arn:${this.partition}:glue:${this.region}:${this.account}:catalog`,
          `arn:${this.partition}:glue:${this.region}:${this.account}:database/${database}`,
          `arn:${this.partition}:glue:${this.region}:${this.account}:table/${database}/*`,
        ],
      }),
    );
    baker.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['s3:GetObject', 's3:PutObject', 's3:AbortMultipartUpload'],
        resources: [`arn:${this.partition}:s3:::${resultsBucket}/bake/*`],
      }),
    );
    baker.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['s3:ListBucket'],
        resources: [`arn:${this.partition}:s3:::${resultsBucket}`],
        conditions: { StringLike: { 's3:prefix': ['bake/*'] } },
      }),
    );
    baker.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['s3:GetBucketLocation'],
        resources: [
          `arn:${this.partition}:s3:::${semanticV2Bucket}`,
          `arn:${this.partition}:s3:::${resultsBucket}`,
        ],
      }),
    );

    if (graph !== undefined) {
      // §2.11 row 6: the enqueue and backfill gates read the mode item; the baker also
      // ConditionChecks it in the legacy pointer transaction (AGG3-6, §2.9).
      grantModeItemRead(enqueue, graph, false);
      grantModeItemRead(backfill, graph, false);
      // Plan 4.6 (F1): the Baker-only V3 bake family, with the baker's mode check.
      grantGraphWorker(baker, graph, 'BakeCoordinator', { modeConditionCheck: true });
    }
    const staffedAction = staffedAlarmAction(this, config);
    boundedStreamMapping(this, 'PublicationCurrentMapping', {
      fn: enqueue,
      eventSourceArn: publicationsStream,
      startingPosition: 'LATEST',
      batchSize: 10,
      parallelizationFactor: 1,
      functionResponseTypes: ['ReportBatchItemFailures'],
      bounds: config.asyncFailureBounds,
      failureQueue: {
        id: 'EnqueueStreamFailureQueue',
        queueName: `adlytics-${config.envName}-publisher-enqueue-stream-failures`,
      },
      alarm: {
        id: 'EnqueueStreamFailureAlarm',
        alarmName: `adlytics-${config.envName}-publisher-enqueue-stream-exhausted`,
        alarmDescription:
          'A publications-stream batch exhausted its bounded preset-artifact enqueue retries; '
          + 'its pages were not enqueued for baking. Disposition: attempts_failures status '
          + '--consumer publisher-enqueue, then attempts_failures drain --consumer '
          + 'publisher-enqueue, then run a declarative re-bake (operator-tools.md).',
        action: staffedAction,
      },
    });
    // §2.11 row 5b (G2-7) and ADR-133: every bake dead letter, legacy or graph, is one staffed
    // arrival signal. The id is the graph alarm's, so a stack that already holds it updates it.
    arrivalAlarm(this, 'BakeDeadLettered', {
      alarmName: `adlytics-${config.envName}-preset-bake-dead-lettered`,
      alarmDescription:
        'A preset-artifact bake envelope exhausted its receives; its page variant was not baked '
        + 'and the pointer still serves the previous revision. Inspect the baker failure, then '
        + 'run a declarative re-bake. In GRAPH a bake node stays CLAIMED and its run GRANTED; '
        + 'inspect it with dlq-status, then redrive --queue bake.',
      queue: deadLetterQueue,
      action: staffedAction,
    });
    baker.addEventSource(
      new sources.SqsEventSource(bakeQueue, {
        batchSize: 1,
        // D11: two lanes; the SQS ESM minimum maxConcurrency is two.
        maxConcurrency: BAKER_RESERVED_CONCURRENCY,
        reportBatchItemFailures: true,
      }),
    );

    // The framework's onEvent function writes only its own declared group (ADR-085). No
    // providerFunctionName: naming the framework function would replace it and its ServiceToken.
    const backfillProviderLogs = new logs.LogGroup(this, 'BackfillProviderLogs', {
      retention: config.logRetentionDays,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    const backfillProviderRole = lambdaRole(this, 'BackfillProviderRole');
    grantOwnLogWrites(backfillProviderRole, backfillProviderLogs);
    const backfillProvider = new cr.Provider(this, 'BackfillProvider', {
      onEventHandler: backfill,
      frameworkOnEventRole: backfillProviderRole,
      logGroup: backfillProviderLogs,
    });
    new cdk.CustomResource(this, 'DeployBackfill', {
      serviceToken: backfillProvider.serviceToken,
      // R5-1: only a declared-input change re-fires the backfill; a code-only deploy does not.
      properties: { generation: declaredInputGeneration },
    });

    for (const [id, key, value, description] of [
      [
        'BakeQueueUrlParameter',
        'publisherBakeQueueUrl',
        bakeQueue.queueUrl,
        'FIFO preset-artifact bake queue URL for declared page-variant enqueue intents.',
      ],
      [
        'BakeQueueArnParameter',
        'publisherBakeQueueArn',
        bakeQueue.queueArn,
        'FIFO preset-artifact bake queue ARN for least-privilege enqueue access.',
      ],
    ] as const) {
      new ssm.StringParameter(this, id, {
        parameterName: paramPath(config.envName, key),
        stringValue: value,
        description,
      });
    }

    // The declared-input generation is a content-derived custom-resource property, so every
    // page declaration change enqueues the complete declared page/variant set once, and a
    // code-only redeploy sends no event (R5-1). FIFO deduplication makes duplicate recovery safe.
    applyFunctionErrorAlarms(this, config, staffedAction);
    applyStandardTags(this, config);
    applyRemovalPolicy(this, cdk.RemovalPolicy.DESTROY);
  }

  private functionWithLogs(options: {
    readonly id: string;
    readonly functionName: string;
    readonly handler: string;
    readonly sourceAssetPath: string;
    readonly dependencies: lambda.ILayerVersion;
    readonly memorySize: number;
    readonly timeoutSeconds: number;
    readonly reservedConcurrency: number;
    readonly environment: Record<string, string>;
  }): lambda.Function {
    const logGroup = new logs.LogGroup(this, `${options.id}Logs`, {
      logGroupName: `/aws/lambda/${options.functionName}`,
      retention: this.config.logRetentionDays,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    const role = new iam.Role(this, `${options.id}ExecutionRole`, {
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      description: `Least-privilege execution role for ${options.functionName}.`,
    });
    role.addToPolicy(
      new iam.PolicyStatement({
        actions: ['logs:CreateLogStream', 'logs:PutLogEvents'],
        resources: [`${logGroup.logGroupArn}:*`],
      }),
    );
    return new lambda.Function(this, options.id, {
      functionName: options.functionName,
      runtime: new lambda.Runtime(this.config.pythonRuntime, lambda.RuntimeFamily.PYTHON),
      architecture: this.lambdaArchitecture(),
      code: lambda.Code.fromAsset(options.sourceAssetPath),
      handler: options.handler,
      layers: [options.dependencies],
      environment: options.environment,
      memorySize: options.memorySize,
      timeout: cdk.Duration.seconds(options.timeoutSeconds),
      reservedConcurrentExecutions: options.reservedConcurrency,
      role,
      logGroup,
    });
  }

  private lambdaArchitecture(): lambda.Architecture {
    return { arm64: lambda.Architecture.ARM_64 }[this.config.lambdaArchitecture];
  }
}
