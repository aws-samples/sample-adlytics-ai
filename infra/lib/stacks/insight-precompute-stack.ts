// SPDX-License-Identifier: MIT-0
import * as cdk from 'aws-cdk-lib';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as sources from 'aws-cdk-lib/aws-lambda-event-sources';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';

import { modelInvokeStatements } from '../bedrock-model-access';
import { AppConfig, paramPath, semanticV2AthenaWorkgroupName, semanticV2DatabaseName } from '../config';
import {
  INSIGHT_PRECOMPUTE_RESERVED_CONCURRENCY,
  validateAthenaStartBudget,
} from '../config/athena-start-budget';
import { ROUTES } from '../generated/execution-graph-protocol';
import { executionGraphAuthority, grantGraphWorker } from './execution-graph-authority';
import {
  arrivalAlarm,
  boundedStreamMapping,
  staffedAlarmAction,
} from '../constructs/observability/async-failure';
import { grantOwnLogWrites, lambdaRole } from '../mixins/logging';
import { applyFunctionErrorAlarms } from '../mixins/function-error-alarms';
import { applyRemovalPolicy, applyStandardTags } from '../mixins/tagging';
import { grantSpanExport, tracingEnvironment } from '../telemetry';

/** The worker's timeout: one precompute node does what one stream record does today (§2.8). */
const PRECOMPUTE_TIMEOUT_SECONDS = 30;
const PRECOMPUTE_UNION_MAX_REQUESTS = 40;
const PRECOMPUTE_GENERATION_CONCURRENCY = 8;
const PRECOMPUTE_GENERATION_TIMEOUT_SECONDS = 4;
/** The FIFO's visibility margin; timeout + margin (60 s) is the precompute claim's lease. */
const PRECOMPUTE_VISIBILITY_MARGIN_SECONDS = 30;

export interface InsightPrecomputeStackProps extends cdk.StackProps {
  readonly config: AppConfig;
  readonly sourceAssetPath: string;
}

/** Disposable bounded consumer of retained publication records; all target derivation is server-owned. */
export class InsightPrecomputeStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: InsightPrecomputeStackProps) {
    super(scope, id, props);
    const { config } = props;
    // S1/S2: insight precompute is the 2 x 4 row of the static Athena start budget.
    validateAthenaStartBudget(config);
    const read = (key: Parameters<typeof paramPath>[1]): string => ssm.StringParameter.valueForStringParameter(this, paramPath(config.envName, key));
    const dependencies = lambda.LayerVersion.fromLayerVersionArn(this, 'PythonDependencies', read('dependenciesLayerArn'));
    const publicationsTable = read('semanticV2PublicationsTableName');
    const semanticV2Bucket = read('semanticV2BucketName');
    // PublisherArtifactStoreStack is earlier in DEPLOY_ORDER and publishes this typed SSM value;
    // the read adds no reverse CloudFormation reference to PublisherStack.
    const artifactBucket = read('publisherArtifactBucketName');
    const currentDatabase = read('semanticV2CurrentDatabase');
    const resultsBucket = read('semanticV2AthenaResultsBucketName');
    const publicationsStream = read('semanticV2PublicationsStreamArn');
    const registryName = read('insightPromptRegistryTableName');
    const registryArn = read('insightPromptRegistryTableArn');
    const cacheName = read('insightCacheTableName');
    const cacheArn = read('insightCacheTableArn');
    const publicationsArn = `arn:${this.partition}:dynamodb:${this.region}:${this.account}:table/${publicationsTable}`;
    const name = `adlytics-${config.envName}-InsightPrecompute`;
    // S6 §2.8: where the graph authority is provisioned, the graph precompute node's FIFO.
    const graph = executionGraphAuthority(config, this);
    const logGroup = new logs.LogGroup(this, 'PrecomputeLogs', { logGroupName: `/aws/lambda/${name}`, retention: config.logRetentionDays, removalPolicy: cdk.RemovalPolicy.DESTROY });
    const role = lambdaRole(this, 'PrecomputeServiceRole');
    grantOwnLogWrites(role, logGroup);
    const worker = new lambda.Function(this, 'Precompute', {
      functionName: name, role, runtime: new lambda.Runtime(config.pythonRuntime, lambda.RuntimeFamily.PYTHON), architecture: lambda.Architecture.ARM_64,
      code: lambda.Code.fromAsset(props.sourceAssetPath), handler: 'adapters.jobs.insight_precompute.handler.handler', layers: [dependencies],
      environment: {
        ...tracingEnvironment(config),
        // Observation-only companion emission: present only when the development status slice
        // is provisioned; an absent URL disables the producer entirely inside the workload.
        ...(config.operationalStatus.provisioned
          ? { ADL_OPERATIONAL_STATUS_QUEUE_URL: read('operationalStatusQueueUrl') }
          : {}),
        ADL_SEMANTIC_PUBLICATIONS_TABLE: publicationsTable,
        ADL_SEMANTIC_V2_BUCKET: semanticV2Bucket,
        ADL_PRESET_ARTIFACT_BUCKET: artifactBucket,
        ADL_SEMANTIC_V2_ATHENA_INSIGHT_PRECOMPUTE_WORKGROUP: semanticV2AthenaWorkgroupName(
          config.envName,
          'insight-precompute',
        ),
        ADL_SEMANTIC_V2_CURRENT_DATABASE: currentDatabase,
        ADL_ATHENA_RESULT_REUSE_MAX_AGE_MINUTES: String(config.athenaResultReuseMaxAgeMinutes),
        ADL_ATHENA_QUERY_BATCH_MAX_CONCURRENCY: String(config.athenaStartBudget.insightStartCap),
        ADL_INSIGHT_PROMPT_REGISTRY_TABLE: registryName,
        ADL_INSIGHT_CACHE_TABLE: cacheName,
        ADL_INSIGHT_QUICK_MODEL_ID: config.insight.quickModelId,
        ADL_INSIGHT_DEEP_MODEL_ID: config.insight.deepModelId,
        ADL_INSIGHT_GENERATION_TIMEOUT_SECONDS: String(PRECOMPUTE_GENERATION_TIMEOUT_SECONDS),
        ADL_INSIGHT_GENERATION_MAX_CONCURRENCY: String(PRECOMPUTE_GENERATION_CONCURRENCY),
        ADL_INSIGHT_PRECOMPUTE_MAX_REQUESTS: String(PRECOMPUTE_UNION_MAX_REQUESTS),
        ...(graph === undefined ? {} : { ADL_EXECUTION_GRAPH_TABLE: graph.tableName }),
      },
      memorySize: 1024, timeout: cdk.Duration.seconds(PRECOMPUTE_TIMEOUT_SECONDS), reservedConcurrentExecutions: INSIGHT_PRECOMPUTE_RESERVED_CONCURRENCY, logGroup,
    });
    grantSpanExport(worker);
    worker.addToRolePolicy(new iam.PolicyStatement({ actions: ['dynamodb:DescribeStream', 'dynamodb:GetRecords', 'dynamodb:GetShardIterator', 'dynamodb:ListStreams'], resources: [publicationsStream] }));
    worker.addToRolePolicy(new iam.PolicyStatement({ actions: ['dynamodb:GetItem'], resources: [publicationsArn, registryArn] }));
    if (config.operationalStatus.provisioned) {
      worker.addToRolePolicy(new iam.PolicyStatement({
        sid: 'EmitOperationalStatusCompanions',
        actions: ['sqs:SendMessage', 'sqs:GetQueueAttributes', 'sqs:GetQueueUrl'],
        resources: [read('operationalStatusQueueArn')],
      }));
    }
    worker.addToRolePolicy(new iam.PolicyStatement({ actions: ['dynamodb:TransactGetItems'], resources: [publicationsArn] }));
    worker.addToRolePolicy(new iam.PolicyStatement({ actions: ['dynamodb:Query'], resources: [`${publicationsArn}/index/scope-current`] }));
    worker.addToRolePolicy(new iam.PolicyStatement({ actions: ['s3:GetObject'], resources: [`arn:${this.partition}:s3:::${semanticV2Bucket}/canonical/current/*`, `arn:${this.partition}:s3:::${semanticV2Bucket}/immutable/canonical/successful/*`] }));
    // Slice 5 owner-approved minimum: immutable artifact reads only. No list, write, delete,
    // wildcard bucket, or sibling prefix is granted; the bucket uses S3-managed encryption.
    worker.addToRolePolicy(new iam.PolicyStatement({
      sid: 'ReadPresetArtifacts',
      actions: ['s3:GetObject'],
      resources: [
        `arn:${this.partition}:s3:::${artifactBucket}/pages/*`,
        `arn:${this.partition}:s3:::${artifactBucket}/custody/*`,
      ],
    }));
    worker.addToRolePolicy(new iam.PolicyStatement({ actions: ['s3:ListBucket'], resources: [`arn:${this.partition}:s3:::${semanticV2Bucket}`], conditions: { StringLike: { 's3:prefix': ['canonical/current/*'] } } }));
    worker.addToRolePolicy(new iam.PolicyStatement({ actions: ['dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:Query', 'dynamodb:UpdateItem'], resources: [cacheArn] }));
    worker.addToRolePolicy(new iam.PolicyStatement({
      actions: ['athena:StartQueryExecution', 'athena:GetQueryExecution', 'athena:GetQueryResults', 'athena:StopQueryExecution'],
      resources: [`arn:${this.partition}:athena:${this.region}:${this.account}:workgroup/${semanticV2AthenaWorkgroupName(config.envName, 'insight-precompute')}`],
    }));
    const database = semanticV2DatabaseName(config.envName, 'current');
    worker.addToRolePolicy(new iam.PolicyStatement({
      actions: ['glue:GetDatabase', 'glue:GetTable', 'glue:GetTables', 'glue:GetPartitions'],
      resources: [
        `arn:${this.partition}:glue:${this.region}:${this.account}:catalog`,
        `arn:${this.partition}:glue:${this.region}:${this.account}:database/${database}`,
        `arn:${this.partition}:glue:${this.region}:${this.account}:table/${database}/*`,
      ],
    }));
    worker.addToRolePolicy(new iam.PolicyStatement({
      actions: ['s3:GetObject', 's3:PutObject', 's3:AbortMultipartUpload'],
      resources: [`arn:${this.partition}:s3:::${resultsBucket}/insight-precompute/*`],
    }));
    worker.addToRolePolicy(new iam.PolicyStatement({
      actions: ['s3:ListBucket'], resources: [`arn:${this.partition}:s3:::${resultsBucket}`],
      conditions: { StringLike: { 's3:prefix': ['insight-precompute/*'] } },
    }));
    worker.addToRolePolicy(new iam.PolicyStatement({
      actions: ['s3:GetBucketLocation'],
      resources: [`arn:${this.partition}:s3:::${semanticV2Bucket}`, `arn:${this.partition}:s3:::${resultsBucket}`],
    }));
    for (const statement of modelInvokeStatements(
      this,
      'InvokeInsightModels',
      [config.insight.quickModelId, config.insight.deepModelId],
      ['bedrock:InvokeModel'],
    )) worker.addToRolePolicy(statement);
    const staffedAction = staffedAlarmAction(this, config);
    if (graph !== undefined) {
      // S6 §2.8 (plan B2): the `precompute` route's FIFO and its DLQ, mirroring bake. The
      // DLQ's maxReceiveCount is the generated `routes.precompute`. §2.11 row 7: its alarm on
      // the staffed topic and the consumer family are below; the dispatcher's send is row 1.
      const deadLetter = new sqs.Queue(this, 'GraphPrecomputeDeadLetterQueue', {
        queueName: `adlytics-${config.envName}-insight-precompute-dlq.fifo`,
        fifo: true,
        contentBasedDeduplication: true,
        encryption: sqs.QueueEncryption.SQS_MANAGED,
        enforceSSL: true,
        retentionPeriod: cdk.Duration.days(14),
      });
      const queue = new sqs.Queue(this, 'GraphPrecomputeQueue', {
        queueName: `adlytics-${config.envName}-insight-precompute.fifo`,
        fifo: true,
        contentBasedDeduplication: true,
        encryption: sqs.QueueEncryption.SQS_MANAGED,
        enforceSSL: true,
        visibilityTimeout: cdk.Duration.seconds(PRECOMPUTE_TIMEOUT_SECONDS + PRECOMPUTE_VISIBILITY_MARGIN_SECONDS),
        retentionPeriod: cdk.Duration.days(14),
        deadLetterQueue: { queue: deadLetter, maxReceiveCount: ROUTES.precompute.max_receive_count },
      });
      worker.addEventSource(new sources.SqsEventSource(queue, { batchSize: 1, reportBatchItemFailures: true }));
      // §2.11 rows 6 and 7: the stream gate's mode read rides the consumer family's own
      // (`ReadGraphModeItem`), so the one function holds one mode statement.
      grantGraphWorker(worker, graph, 'PostGrantConsumer');
      arrivalAlarm(this, 'PrecomputeDeadLettered', {
        alarmName: `adlytics-${config.envName}-insight-precompute-dead-lettered`,
        alarmDescription:
          'A graph precompute envelope exhausted its receives. The precompute node stays CLAIMED '
          + 'and its run GRANTED; inspect it with dlq-status, then redrive --queue precompute.',
        queue: deadLetter,
        action: staffedAction,
      });
    }
    boundedStreamMapping(this, 'PublicationCurrentMapping', {
      fn: worker,
      eventSourceArn: publicationsStream,
      startingPosition: 'LATEST',
      batchSize: 10,
      parallelizationFactor: 1,
      functionResponseTypes: ['ReportBatchItemFailures'],
      bounds: config.asyncFailureBounds,
      failureQueue: {
        id: 'PrecomputeStreamFailureQueue',
        queueName: `adlytics-${config.envName}-insight-precompute-stream-failures`,
      },
      alarm: {
        id: 'PrecomputeStreamFailureAlarm',
        alarmName: `adlytics-${config.envName}-insight-precompute-stream-exhausted`,
        alarmDescription:
          'A publications-stream batch exhausted its bounded insight precompute retries; its '
          + 'insights were not precomputed. Disposition: attempts_failures status --consumer '
          + 'insight-precompute, then attempts_failures drain --consumer insight-precompute, '
          + 'then run a declarative re-bake (operator-tools.md).',
        action: staffedAction,
      },
    });
    applyFunctionErrorAlarms(this, config, staffedAction);
    applyStandardTags(this, config);
    applyRemovalPolicy(this, cdk.RemovalPolicy.DESTROY);
  }
}
