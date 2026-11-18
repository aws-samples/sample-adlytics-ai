// SPDX-License-Identifier: MIT-0
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import * as path from 'node:path';
import * as cdk from 'aws-cdk-lib';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import { DockerImageAsset, Platform } from 'aws-cdk-lib/aws-ecr-assets';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as scheduler from 'aws-cdk-lib/aws-scheduler';
import * as schedulerTargets from 'aws-cdk-lib/aws-scheduler-targets';
import * as sfn from 'aws-cdk-lib/aws-stepfunctions';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';
import {
  AppConfig,
  paramPath,
  semanticV2AthenaWorkgroupName,
  semanticV2DatabaseName,
} from '../config';
import { DEMAND_FORECAST_INPUT_RESERVED_CONCURRENCY } from '../config/athena-start-budget';
import { staffedAlarmAction } from '../constructs/observability/async-failure';
import { OPERATIONAL_METRICS } from '../generated/operational-metrics';
import { applyFunctionErrorAlarms } from '../mixins/function-error-alarms';
import { applyRemovalPolicy, applyStandardTags } from '../mixins/tagging';
import { grantSpanExport, tracingEnvironment } from '../telemetry';

export interface DemandForecastStackProps extends cdk.StackProps {
  readonly config: AppConfig;
  readonly sourceAssetPath: string;
}

const REPOSITORY_ROOT = path.join(__dirname, '..', '..', '..');
const JOB_DIRECTORY = 'src/adapters/jobs/demand_forecast';
const INPUT_HANDLER = 'adapters.jobs.demand_forecast_input.handler.handler';
const INPUT_MEMORY_MIB = 1024;
const INPUT_TIMEOUT_SECONDS = 900;
const PUBLISHER_HANDLER = 'adapters.jobs.demand_forecast_publisher.handler.handler';
/** Interim by design (plan bounds table): moves at P2-2 along the §10 measured-value ladder. */
const PUBLISHER_MEMORY_MIB = 2048;
/** The Lambda maximum. */
const PUBLISHER_TIMEOUT_SECONDS = 900;
/** Interim, removal item RB-1: the store's write lanes, re-derived from P2-2's throttle counts. */
const PUBLISH_MAX_CONCURRENCY = 4;
/** The daily check (design §6d "Trigger"); `SelectInput` decides, the schedule only requests. */
export const FORECAST_SCHEDULE = 'cron(0 13 * * ? *)';
/**
 * `serving.stale_after_days` in contracts/forecasting/demand-forecast.yaml; infra has no YAML
 * parser, so the stack test reads the contract and fails on drift.
 */
export const STALE_AFTER_DAYS = 16;
/** `DemandForecastInferredZeroShare` alarms above this percentage (design §3b, *policy*). */
export const INFERRED_ZERO_SHARE_PERCENT = 5;
/** What the publisher reads; also the `s3:prefix` scope of its `ListBucket`, so a missing key is
 * `NoSuchKey` rather than `AccessDenied` (F-A7-1). */
export const PUBLISHER_READ_KEYS = [
  'runs/*/shards/*/attempts/*/candidate/*',
  'runs/*/shards/*/attempts/*/roster-part.jsonl.gz',
  'runs/*/input/*/manifest.json',
  'runs/*/input/*/titles.json',
] as const;
/** Typed errors the publisher raises that a retry can clear (design §6d; A7 handoff). */
const STORE_RETRY = {
  ErrorEquals: ['ForecastStoreThrottled', 'ObjectStoreError'],
  MaxAttempts: 3,
  IntervalSeconds: 30,
  BackoffRate: 2,
};
const VOLUME_GIB = 30;
const STATE_MACHINE_TIMEOUT_SECONDS = 86_400;
const SAGEMAKER_EVENTS_RULE = 'StepFunctionsGetEventsForSageMakerProcessingJobsRule';
const FORECAST_TABLES = ['fct_retail_sales', 'fct_product_page_views', 'dim_product'] as const;

/** The image's closed path list (design §4b): `**` excluded, then each listed path re-included. */
export function forecastImageExcludes(root: string = REPOSITORY_ROOT): string[] {
  const listed = readFileSync(path.join(root, JOB_DIRECTORY, 'image-paths.txt'), 'utf8')
    .split('\n')
    .map((line) => line.trim())
    .filter((line) => line !== '' && !line.startsWith('#'));
  return ['**', ...listed.map((line) => `!${line}`)];
}

/** SHA-256 of the exported job lock, the lock digest every run report and fingerprint carries. */
export function forecastLockDigest(root: string = REPOSITORY_ROOT): string {
  return createHash('sha256')
    .update(readFileSync(path.join(root, JOB_DIRECTORY, 'requirements.lock')))
    .digest('hex');
}

/** The bucket and key prefix of an `s3://bucket/prefix/` URI, refused unless it has both. */
export function s3Prefix(uri: string): { bucket: string; prefix: string } {
  const match = /^s3:\/\/([a-z0-9][a-z0-9.-]{1,61}[a-z0-9])\/(.+\/)$/.exec(uri);
  if (match === null) {
    throw new Error(`demandForecast.modelArtifactUri must be an s3:// prefix ending in /: ${uri}`);
  }
  return { bucket: match[1] as string, prefix: match[2] as string };
}

/** The Processing job name prefix the IAM conditions and the job names share. */
export function processingJobPrefix(envName: string): string {
  return `adlytics-${envName}-demand-forecast-`;
}

/**
 * The weekly Vendor demand forecast's engine (approval I-2): the job image, the network-isolated
 * Processing role, the input builder, the publisher, the daily schedule, the alarms and the
 * Standard state machine (`SelectInput` -> `BuildInput` -> `Forecast` map of
 * `createProcessingJob.sync` -> `Publish` map -> `Commit`, every other end in `RecordOutcome`),
 * with a dev-only `ProbeStart` entry (design §6d).
 */
export class DemandForecastStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: DemandForecastStackProps) {
    super(scope, id, props);
    const { config } = props;
    const forecast = config.demandForecast;
    const read = (key: Parameters<typeof paramPath>[1]): string =>
      ssm.StringParameter.valueForStringParameter(this, paramPath(config.envName, key));
    const arn = (service: string, resource: string): string =>
      `arn:${this.partition}:${service}:${this.region}:${this.account}:${resource}`;
    const s3Arn = (bucket: string, key = ''): string =>
      `arn:${this.partition}:s3:::${bucket}${key === '' ? '' : `/${key}`}`;

    const processingBucket = read('demandForecastProcessingBucketName');
    const forecastTable = read('demandForecastTableName');
    const forecastTableArn = arn('dynamodb', `table/${forecastTable}`);
    const namespace = OPERATIONAL_METRICS.ForecastRunOutcome.namespace;
    const artifact = s3Prefix(forecast.modelArtifactUri);
    const jobPrefix = processingJobPrefix(config.envName);
    const jobArn = arn('sagemaker', `processing-job/${jobPrefix}*`);
    // Read by scripts/deploy_preflight.py before any diff or deploy. The Forecast map runs up to
    // maxParallelJobs single-instance jobs at once, and a lower quota fails a run.
    this.templateOptions.metadata = {
      'Adlytics::ServiceQuotaRequirements': [
        {
          serviceCode: 'sagemaker',
          quotaName: `${forecast.instanceType} for processing job usage`,
          minimum: forecast.maxParallelJobs,
        },
      ],
    };

    // ---- ForecastJobImage (I-9): the bootstrap repository, the asset hash over the runtime set.
    const image = new DockerImageAsset(this, 'ForecastJobImage', {
      directory: REPOSITORY_ROOT,
      file: `${JOB_DIRECTORY}/Dockerfile`,
      platform: Platform.LINUX_AMD64,
      ignoreMode: cdk.IgnoreMode.DOCKER,
      exclude: forecastImageExcludes(),
    });

    // ---- ForecastProcessingRole: what the network-isolated job may read and write.
    const processingRole = new iam.Role(this, 'ForecastProcessingRole', {
      roleName: `adlytics-${config.envName}-demand-forecast-processing`,
      description: 'Demand-forecast Processing job: staged input and artifact in, candidate out.',
      assumedBy: new iam.ServicePrincipal('sagemaker.amazonaws.com', {
        conditions: {
          StringEquals: { 'aws:SourceAccount': this.account },
          ArnLike: { 'aws:SourceArn': jobArn },
        },
      }),
    });
    processingRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'ReadStagedInputAndArtifact',
        actions: ['s3:GetObject'],
        resources: [
          s3Arn(processingBucket, 'runs/*/input/*'),
          s3Arn(artifact.bucket, `${artifact.prefix}*`),
        ],
      }),
    );
    processingRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'ListStagedInput',
        actions: ['s3:ListBucket'],
        resources: [s3Arn(processingBucket)],
        conditions: { StringLike: { 's3:prefix': ['runs/*/input/*'] } },
      }),
    );
    processingRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'ListArtifact',
        actions: ['s3:ListBucket'],
        resources: [s3Arn(artifact.bucket)],
        conditions: { StringLike: { 's3:prefix': [`${artifact.prefix}*`] } },
      }),
    );
    processingRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'WriteCandidateOnly',
        actions: ['s3:PutObject'],
        resources: [s3Arn(processingBucket, 'runs/*/shards/*/attempts/*/candidate/*')],
      }),
    );
    processingRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'PullJobImage',
        actions: ['ecr:BatchGetImage', 'ecr:GetDownloadUrlForLayer', 'ecr:BatchCheckLayerAvailability'],
        resources: [image.repository.repositoryArn],
      }),
    );
    processingRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'EcrAuthorization',
        // GetAuthorizationToken has no resource-level permission.
        actions: ['ecr:GetAuthorizationToken'],
        resources: ['*'],
      }),
    );
    processingRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'JobLogs',
        actions: ['logs:CreateLogGroup', 'logs:CreateLogStream', 'logs:PutLogEvents', 'logs:DescribeLogStreams'],
        resources: [
          arn('logs', 'log-group:/aws/sagemaker/ProcessingJobs'),
          arn('logs', 'log-group:/aws/sagemaker/ProcessingJobs:*'),
        ],
      }),
    );

    // ---- ForecastInputFunction: select and build (design §3c), the baker's measurement grants
    // statement for statement on the batch workgroup and its own results prefix.
    const dependencies = lambda.LayerVersion.fromLayerVersionArn(
      this,
      'PythonDependencies',
      read('dependenciesLayerArn'),
    );
    const publicationsTable = read('semanticV2PublicationsTableName');
    const semanticV2Bucket = read('semanticV2BucketName');
    const currentDatabase = read('semanticV2CurrentDatabase');
    const resultsBucket = read('semanticV2AthenaResultsBucketName');
    const batchWorkgroup = semanticV2AthenaWorkgroupName(config.envName, 'batch');
    const functionName = `adlytics-${config.envName}-DemandForecastInput`;
    const inputLogs = new logs.LogGroup(this, 'ForecastInputFunctionLogs', {
      logGroupName: `/aws/lambda/${functionName}`,
      retention: config.logRetentionDays,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    const inputRole = new iam.Role(this, 'ForecastInputFunctionExecutionRole', {
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      description: `Least-privilege execution role for ${functionName}.`,
    });
    inputRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ['logs:CreateLogStream', 'logs:PutLogEvents'],
        resources: [`${inputLogs.logGroupArn}:*`],
      }),
    );
    const input = new lambda.Function(this, 'ForecastInputFunction', {
      functionName,
      runtime: new lambda.Runtime(config.pythonRuntime, lambda.RuntimeFamily.PYTHON),
      architecture: { arm64: lambda.Architecture.ARM_64 }[config.lambdaArchitecture],
      code: lambda.Code.fromAsset(props.sourceAssetPath),
      handler: INPUT_HANDLER,
      layers: [dependencies],
      memorySize: INPUT_MEMORY_MIB,
      timeout: cdk.Duration.seconds(INPUT_TIMEOUT_SECONDS),
      // The Athena start budget's 'demand_forecast_input' row: one invocation, sequential starts.
      reservedConcurrentExecutions: DEMAND_FORECAST_INPUT_RESERVED_CONCURRENCY,
      role: inputRole,
      logGroup: inputLogs,
      environment: {
        ...tracingEnvironment(config),
        ADL_SEMANTIC_PUBLICATIONS_TABLE: publicationsTable,
        ADL_SEMANTIC_V2_BUCKET: semanticV2Bucket,
        ADL_SEMANTIC_V2_ATHENA_BATCH_WORKGROUP: batchWorkgroup,
        ADL_SEMANTIC_V2_CURRENT_DATABASE: currentDatabase,
        ADL_DEMAND_FORECAST_PROCESSING_BUCKET: processingBucket,
        ADL_DEMAND_FORECAST_TABLE: forecastTable,
        ADL_DEMAND_FORECAST_MARKETPLACE_KEY: forecast.marketplaceKey,
        ADL_DEMAND_FORECAST_MODEL_ARTIFACT_DIGEST: forecast.modelArtifactDigest,
        ADL_DEMAND_FORECAST_PROCESSOR_CODE_IDENTITY: image.assetHash,
        ADL_DEMAND_FORECAST_LOCK_DIGEST: forecastLockDigest(),
        ADL_DEMAND_FORECAST_INSTANCE_TYPE: forecast.instanceType,
        ADL_DEMAND_FORECAST_MAX_RUNTIME_SECONDS: String(forecast.maxRuntimeSeconds),
      },
    });
    grantSpanExport(input);
    const publicationsArn = arn('dynamodb', `table/${publicationsTable}`);
    const database = semanticV2DatabaseName(config.envName, 'current');
    for (const statement of [
      new iam.PolicyStatement({
        actions: ['dynamodb:GetItem', 'dynamodb:TransactGetItems'],
        resources: [publicationsArn],
      }),
      new iam.PolicyStatement({
        actions: ['dynamodb:Query'],
        resources: [`${publicationsArn}/index/scope-current`],
      }),
      new iam.PolicyStatement({
        actions: ['s3:GetObject'],
        resources: [
          s3Arn(semanticV2Bucket, 'canonical/current/*'),
          s3Arn(semanticV2Bucket, 'immutable/canonical/successful/*'),
        ],
      }),
      new iam.PolicyStatement({
        actions: ['s3:ListBucket'],
        resources: [s3Arn(semanticV2Bucket)],
        conditions: { StringLike: { 's3:prefix': ['canonical/current/*'] } },
      }),
      new iam.PolicyStatement({
        actions: ['athena:StartQueryExecution', 'athena:GetQueryExecution', 'athena:StopQueryExecution'],
        resources: [arn('athena', `workgroup/${batchWorkgroup}`)],
      }),
      new iam.PolicyStatement({
        actions: ['glue:GetDatabase', 'glue:GetTable', 'glue:GetPartitions'],
        resources: [
          arn('glue', 'catalog'),
          arn('glue', `database/${database}`),
          ...FORECAST_TABLES.map((table) => arn('glue', `table/${database}/${table}`)),
        ],
      }),
      new iam.PolicyStatement({
        actions: ['s3:GetObject', 's3:PutObject', 's3:AbortMultipartUpload'],
        resources: [s3Arn(resultsBucket, 'batch/*'), s3Arn(processingBucket, 'runs/*/input/*')],
      }),
      new iam.PolicyStatement({
        actions: ['s3:ListBucket'],
        resources: [s3Arn(resultsBucket)],
        conditions: { StringLike: { 's3:prefix': ['batch/*'] } },
      }),
      new iam.PolicyStatement({
        actions: ['s3:ListBucket'],
        resources: [s3Arn(processingBucket)],
        conditions: { StringLike: { 's3:prefix': ['runs/*/input/*'] } },
      }),
      new iam.PolicyStatement({
        actions: ['s3:GetBucketLocation'],
        resources: [s3Arn(semanticV2Bucket), s3Arn(resultsBucket), s3Arn(processingBucket)],
      }),
      // CURRENT and the `RUN#` select receipt (same partition; IAM can't condition on the sort
      // key), and the weekly slot marker.
      new iam.PolicyStatement({
        sid: 'ReadForecastPointerAndSlot',
        actions: ['dynamodb:GetItem'],
        resources: [forecastTableArn],
        conditions: { 'ForAllValues:StringLike': { 'dynamodb:LeadingKeys': ['SET#*', 'SLOT#*'] } },
      }),
      // The week's slot marker, a conditional put at scheduled admission (F-A8 ruling). Its own
      // partition, so the builder can never write CURRENT or a receipt.
      new iam.PolicyStatement({
        sid: 'ClaimWeeklySlot',
        actions: ['dynamodb:PutItem'],
        resources: [forecastTableArn],
        conditions: { 'ForAllValues:StringLike': { 'dynamodb:LeadingKeys': ['SLOT#*'] } },
      }),
      new iam.PolicyStatement({
        sid: 'EmitForecastMetrics',
        actions: ['cloudwatch:PutMetricData'],
        resources: ['*'],
        conditions: { StringEquals: { 'cloudwatch:namespace': namespace } },
      }),
    ]) {
      input.addToRolePolicy(statement);
    }

    // ---- ForecastPublisherFunction: publish_shard, commit and record (design §6c), one role.
    const publisherName = `adlytics-${config.envName}-DemandForecastPublisher`;
    const publisherLogs = new logs.LogGroup(this, 'ForecastPublisherFunctionLogs', {
      logGroupName: `/aws/lambda/${publisherName}`,
      retention: config.logRetentionDays,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    const publisherRole = new iam.Role(this, 'ForecastPublisherFunctionExecutionRole', {
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      description: `Least-privilege execution role for ${publisherName}.`,
    });
    publisherRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ['logs:CreateLogStream', 'logs:PutLogEvents'],
        resources: [`${publisherLogs.logGroupArn}:*`],
      }),
    );
    const publisher = new lambda.Function(this, 'ForecastPublisherFunction', {
      functionName: publisherName,
      runtime: new lambda.Runtime(config.pythonRuntime, lambda.RuntimeFamily.PYTHON),
      architecture: { arm64: lambda.Architecture.ARM_64 }[config.lambdaArchitecture],
      code: lambda.Code.fromAsset(props.sourceAssetPath),
      handler: PUBLISHER_HANDLER,
      layers: [dependencies],
      memorySize: PUBLISHER_MEMORY_MIB,
      timeout: cdk.Duration.seconds(PUBLISHER_TIMEOUT_SECONDS),
      role: publisherRole,
      logGroup: publisherLogs,
      environment: {
        ...tracingEnvironment(config),
        ADL_DEMAND_FORECAST_PROCESSING_BUCKET: processingBucket,
        ADL_DEMAND_FORECAST_TABLE: forecastTable,
        ADL_DEMAND_FORECAST_MARKETPLACE_KEY: forecast.marketplaceKey,
      },
    });
    grantSpanExport(publisher);
    for (const statement of [
      new iam.PolicyStatement({
        sid: 'ReadCandidatesManifestAndTitles',
        actions: ['s3:GetObject'],
        resources: [
          s3Arn(processingBucket, 'runs/*/shards/*/attempts/*/candidate/*'),
          s3Arn(processingBucket, 'runs/*/input/*/manifest.json'),
          s3Arn(processingBucket, 'runs/*/input/*/titles.json'),
        ],
      }),
      new iam.PolicyStatement({
        sid: 'WriteRosterParts',
        actions: ['s3:PutObject', 's3:GetObject'],
        resources: [s3Arn(processingBucket, 'runs/*/shards/*/attempts/*/roster-part.jsonl.gz')],
      }),
      new iam.PolicyStatement({
        sid: 'ListReadKeys',
        actions: ['s3:ListBucket'],
        resources: [s3Arn(processingBucket)],
        conditions: { StringLike: { 's3:prefix': [...PUBLISHER_READ_KEYS] } },
      }),
      new iam.PolicyStatement({
        sid: 'ReadForecastStore',
        actions: ['dynamodb:GetItem'],
        resources: [forecastTableArn],
        conditions: { 'ForAllValues:StringLike': { 'dynamodb:LeadingKeys': ['SET#*', 'GEN#*'] } },
      }),
      new iam.PolicyStatement({
        sid: 'WriteGenerationItems',
        actions: ['dynamodb:PutItem'],
        resources: [forecastTableArn],
        conditions: { 'ForAllValues:StringLike': { 'dynamodb:LeadingKeys': ['GEN#*'] } },
      }),
      new iam.PolicyStatement({
        sid: 'CommitPointerAndReceipts',
        actions: ['dynamodb:PutItem', 'dynamodb:UpdateItem', 'dynamodb:ConditionCheckItem'],
        resources: [forecastTableArn],
        conditions: {
          'ForAllValues:StringLike': { 'dynamodb:LeadingKeys': ['SET#*'] },
          StringEquals: { 'dynamodb:EnclosingOperation': 'TransactWriteItems' },
        },
      }),
      new iam.PolicyStatement({
        sid: 'CheckGenerationMeta',
        actions: ['dynamodb:ConditionCheckItem'],
        resources: [forecastTableArn],
        conditions: {
          'ForAllValues:StringLike': { 'dynamodb:LeadingKeys': ['GEN#*'] },
          StringEquals: { 'dynamodb:EnclosingOperation': 'TransactWriteItems' },
        },
      }),
      new iam.PolicyStatement({
        sid: 'EmitForecastMetrics',
        actions: ['cloudwatch:PutMetricData'],
        resources: ['*'],
        conditions: { StringEquals: { 'cloudwatch:namespace': namespace } },
      }),
    ]) {
      publisher.addToRolePolicy(statement);
    }

    // ---- ForecastStateMachine (design §6d, slice 1 subset).
    const machineRole = new iam.Role(this, 'ForecastStateMachineRole', {
      assumedBy: new iam.ServicePrincipal('states.amazonaws.com'),
      description: 'Demand-forecast state machine: input builder and network-isolated jobs only.',
    });
    machineRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'InvokeInputBuilderAndPublisher',
        actions: ['lambda:InvokeFunction'],
        resources: [input.functionArn, publisher.functionArn],
      }),
    );
    machineRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'CreateIsolatedProcessingJob',
        actions: ['sagemaker:CreateProcessingJob'],
        resources: [jobArn],
        conditions: {
          Bool: { 'sagemaker:NetworkIsolation': 'true' },
          'ForAllValues:StringEquals': { 'sagemaker:InstanceTypes': [forecast.instanceType] },
          NumericLessThanEquals: { 'sagemaker:MaxRuntimeInSeconds': forecast.maxRuntimeSeconds },
        },
      }),
    );
    machineRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'TrackProcessingJob',
        actions: [
          'sagemaker:AddTags',
          'sagemaker:ListTags',
          'sagemaker:DescribeProcessingJob',
          'sagemaker:StopProcessingJob',
        ],
        resources: [jobArn],
      }),
    );
    machineRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'SyncIntegrationRule',
        actions: ['events:PutTargets', 'events:PutRule', 'events:DescribeRule'],
        resources: [arn('events', `rule/${SAGEMAKER_EVENTS_RULE}`)],
      }),
    );
    machineRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'PassProcessingRole',
        actions: ['iam:PassRole'],
        resources: [processingRole.roleArn],
        conditions: { StringEquals: { 'iam:PassedToService': 'sagemaker.amazonaws.com' } },
      }),
    );

    const attempt = "'r' & $string($states.context.Execution.RedriveCount) & 'a' & $string($states.context.State.RetryCount)";
    const lambdaRetry = [
      {
        ErrorEquals: ['Lambda.ServiceException', 'Lambda.TooManyRequestsException'],
        MaxAttempts: 2,
        IntervalSeconds: 2,
        BackoffRate: 2,
      },
    ];
    const lambdaTask = `arn:${this.partition}:states:::lambda:invoke`;
    const redrive = '$states.context.Execution.RedriveCount';
    // A caught error goes to RecordOutcome as `failed(<phase>:<code>)` with its own receipt step.
    const toRecord = (phase: string, step: string): unknown[] => [
      {
        ErrorEquals: ['States.ALL'],
        Output: `{% {'step': ${step}, 'outcome': 'failed', 'phase': '${phase}', 'error': $states.errorOutput} %}`,
        Next: 'RecordOutcome',
      },
    ];
    // A map iteration's failure names its shard and attempt (receipt step `s<k>-<attempt_id>`):
    // the iteration catches its own error and fails with it, wrapped, as error `ShardFailed`.
    // State names are unique across the whole machine, map iterations included, so each map's
    // Fail state has its own name (`DUPLICATE_STATE_NAME` refused the first dev deploy).
    const SHARD_FAILED = 'ShardFailed';
    const RUN_SHARD_FAILED = 'RunShardFailed';
    const PUBLISH_SHARD_FAILED = 'PublishShardFailed';
    const shardCatch = (attemptExpression: string, failState: string): unknown[] => [
      {
        ErrorEquals: ['States.ALL'],
        Output: `{% {'shard': $states.input.shard, 'attempt_id': ${attemptExpression}, 'error': $states.errorOutput} %}`,
        Next: failState,
      },
    ];
    const shardFailed = {
      Type: 'Fail',
      Error: SHARD_FAILED,
      Cause: '{% $string($states.input) %}',
    };
    const mapCatch = (phase: string): unknown[] => [
      {
        ErrorEquals: [SHARD_FAILED],
        Output: `{% ($f := $parse($states.errorOutput.Cause); {'step': 's' & $string($f.shard) & '-' & $f.attempt_id, 'attempt_id': $f.attempt_id, 'outcome': 'failed', 'phase': '${phase}', 'error': $f.error}) %}`,
        Next: 'RecordOutcome',
      },
      // Any other map failure has no shard to name; the execution fails and RunFailed alarms
      // on ExecutionsFailed (design §6d "Outcome alarm").
      { ErrorEquals: ['States.ALL'], Next: 'Failed' },
    ];
    const bucketUri = `s3://${processingBucket}`;
    const runShard = {
      Type: 'Task',
      Resource: `arn:${this.partition}:states:::sagemaker:createProcessingJob.sync`,
      Arguments: {
        ProcessingJobName: `{% '${jobPrefix}' & $states.input.run_hash12 & '-s' & $formatNumber($states.input.shard, '000') & '-' & ${attempt} %}`,
        RoleArn: processingRole.roleArn,
        AppSpecification: {
          ImageUri: image.imageUri,
          ContainerArguments: `{% ['forecast', '--run-id', $states.input.run_id, '--shard', $string($states.input.shard), '--attempt-id', ${attempt}, '--input-attempt-id', $states.input.build_attempt_id, '--processor-code-identity', '${image.assetHash}', '--input', '/opt/ml/processing/input', '--model', '/opt/ml/processing/model', '--output', '/opt/ml/processing/output'] %}`,
        },
        ProcessingResources: {
          ClusterConfig: {
            InstanceCount: 1,
            InstanceType: forecast.instanceType,
            VolumeSizeInGB: VOLUME_GIB,
          },
        },
        NetworkConfig: { EnableNetworkIsolation: true },
        StoppingCondition: { MaxRuntimeInSeconds: forecast.maxRuntimeSeconds },
        ProcessingInputs: [
          {
            InputName: 'model',
            S3Input: {
              S3Uri: forecast.modelArtifactUri,
              LocalPath: '/opt/ml/processing/model',
              S3DataType: 'S3Prefix',
              S3InputMode: 'File',
              S3DataDistributionType: 'FullyReplicated',
            },
          },
          {
            InputName: 'manifest',
            S3Input: {
              S3Uri: `{% '${bucketUri}/' & $states.input.manifest_key %}`,
              LocalPath: '/opt/ml/processing/input/manifest',
              S3DataType: 'S3Prefix',
              S3InputMode: 'File',
              S3DataDistributionType: 'FullyReplicated',
            },
          },
          ...(['target', 'traffic'] as const).map((kind) => ({
            InputName: kind,
            S3Input: {
              S3Uri: `{% '${bucketUri}/runs/' & $states.input.run_id & '/input/' & $states.input.build_attempt_id & '/${kind}/shard=' & $string($states.input.shard) & '/' %}`,
              LocalPath: `/opt/ml/processing/input/${kind}`,
              S3DataType: 'S3Prefix',
              S3InputMode: 'File',
              S3DataDistributionType: 'FullyReplicated',
            },
          })),
        ],
        ProcessingOutputConfig: {
          Outputs: [
            {
              OutputName: 'output',
              S3Output: {
                S3Uri: `{% '${bucketUri}/runs/' & $states.input.run_id & '/shards/' & $string($states.input.shard) & '/attempts/' & ${attempt} & '/candidate/' %}`,
                LocalPath: '/opt/ml/processing/output',
                S3UploadMode: 'EndOfJob',
              },
            },
          ],
        },
      },
      // Only {shard, attempt_id}, never the DescribeProcessingJob response (R5-15).
      Output: `{% {'shard': $states.input.shard, 'attempt_id': ${attempt}} %}`,
      Retry: [
        {
          ErrorEquals: ['SageMaker.AmazonSageMakerException'],
          MaxAttempts: 3,
          IntervalSeconds: 60,
          BackoffRate: 2,
          JitterStrategy: 'FULL',
        },
      ],
      Catch: shardCatch(attempt, RUN_SHARD_FAILED),
      End: true,
    };
    const shardItem =
      "{% {'shard': $states.context.Map.Item.Value.shard, 'run_id': $run_id, 'manifest_key': $manifest_key, 'build_attempt_id': $build_attempt_id, 'run_hash12': $run_hash12} %}";
    const publishShard = {
      Type: 'Task',
      Resource: lambdaTask,
      Arguments: {
        FunctionName: publisher.functionArn,
        Payload: "{% {'operation': 'publish_shard', 'run_id': $states.input.run_id, 'redrive_count': $states.input.redrive_count, 'shard': $states.input.shard, 'attempt_id': $states.input.attempt_id, 'manifest_key': $states.input.manifest_key, 'attempts': $states.input.attempts} %}",
      },
      // Only the ~300-byte shard result (R5-15).
      Output: '{% $states.result.Payload %}',
      Retry: [...lambdaRetry, STORE_RETRY],
      Catch: shardCatch('$states.input.attempt_id', PUBLISH_SHARD_FAILED),
      End: true,
    };
    const states: Record<string, unknown> = {
      SelectInput: {
        Type: 'Task',
        Resource: lambdaTask,
        Arguments: {
          FunctionName: input.functionArn,
          // A scheduled start carries only `scheduled_time`; an operator start only
          // `retry_failed` and `operator_reason`. An absent field is left out of the payload.
          Payload:
            "{% {'operation': 'select', 'execution_name': $states.context.Execution.Name, 'scheduled_time': $states.input.scheduled_time, 'retry_failed': $states.input.retry_failed, 'operator_reason': $states.input.operator_reason} %}",
        },
        // Every later state, receipt and prefix uses select's run ID, never the execution name.
        Assign: {
          run_id: '{% $states.result.Payload.run_id %}',
          run_fingerprint:
            '{% $exists($states.result.Payload.run_fingerprint) ? $states.result.Payload.run_fingerprint : null %}',
          expected_fence: '{% $states.result.Payload.expected_fence %}',
          operator_reason: '{% $states.result.Payload.operator_reason %}',
        },
        Output: '{% $states.result.Payload %}',
        Retry: lambdaRetry,
        Catch: [
          {
            ErrorEquals: ['States.ALL'],
            Assign: {
              run_id: '{% $states.context.Execution.Name %}',
              run_fingerprint: null,
              expected_fence: null,
              operator_reason: null,
            },
            Output:
              "{% {'step': 'select', 'outcome': 'failed', 'phase': 'input_build', 'error': $states.errorOutput} %}",
            Next: 'RecordOutcome',
          },
        ],
        Next: 'Decide',
      },
      Decide: {
        Type: 'Choice',
        Choices: [
          { Condition: "{% $states.input.outcome = 'run' %}", Next: 'BuildInput' },
          {
            // no_op_same_input or held_input_not_ready: a select receipt only, carrying the run
            // identity (its input fingerprint) when select computed one (R1-12).
            Condition: "{% $states.input.outcome != 'run' %}",
            Output:
              "{% {'step': 'select', 'outcome': $states.input.outcome, 'category': $states.input.category, 'run_identity': $states.input.run_identity} %}",
            Next: 'RecordOutcome',
          },
        ],
      },
      BuildInput: {
        Type: 'Task',
        Resource: lambdaTask,
        Arguments: {
          FunctionName: input.functionArn,
          Payload: {
            operation: 'build',
            run_id: '{% $run_id %}',
            redrive_count: `{% ${redrive} %}`,
            retry_count: '{% $states.context.State.RetryCount %}',
            input_fingerprint: '{% $states.input.input_fingerprint %}',
            expected_fence: '{% $expected_fence %}',
          },
        },
        Assign: {
          manifest_key: '{% $states.result.Payload.manifest_key %}',
          build_attempt_id: '{% $states.result.Payload.build_attempt_id %}',
          run_hash12: '{% $states.result.Payload.run_hash12 %}',
        },
        Output: "{% {'shards': $states.result.Payload.shards} %}",
        Retry: lambdaRetry,
        Catch: toRecord(
          'input_build',
          `'build-r' & $string(${redrive}) & 'a' & $string($states.context.State.RetryCount)`,
        ),
        Next: 'Forecast',
      },
      Forecast: {
        Type: 'Map',
        Items: '{% $states.input.shards %}',
        ItemSelector: shardItem,
        MaxConcurrency: forecast.maxParallelJobs,
        ItemProcessor: {
          ProcessorConfig: { Mode: 'INLINE' },
          StartAt: 'RunShard',
          States: { RunShard: runShard, [RUN_SHARD_FAILED]: shardFailed },
        },
        // Every shard's bound attempt, which each publish_shard and the commit receive.
        Assign: { attempts: '{% $states.result %}' },
        Catch: mapCatch('job'),
        Next: 'Publish',
      },
      Publish: {
        Type: 'Map',
        Items: '{% $attempts %}',
        ItemSelector:
          `{% {'shard': $states.context.Map.Item.Value.shard, 'attempt_id': $states.context.Map.Item.Value.attempt_id, 'run_id': $run_id, 'redrive_count': ${redrive}, 'manifest_key': $manifest_key, 'attempts': $attempts} %}`,
        MaxConcurrency: PUBLISH_MAX_CONCURRENCY,
        ItemProcessor: {
          ProcessorConfig: { Mode: 'INLINE' },
          StartAt: 'PublishShard',
          States: { PublishShard: publishShard, [PUBLISH_SHARD_FAILED]: shardFailed },
        },
        Catch: mapCatch('publish'),
        Next: 'Commit',
      },
      Commit: {
        Type: 'Task',
        Resource: lambdaTask,
        Arguments: {
          FunctionName: publisher.functionArn,
          Payload: {
            operation: 'commit',
            run_id: '{% $run_id %}',
            redrive_count: `{% ${redrive} %}`,
            manifest_key: '{% $manifest_key %}',
            shard_results: '{% $states.input %}',
            // An operator RETRY_FAILED start's REASON goes on its commit receipt (§6d step 3).
            operator_reason: '{% $operator_reason %}',
          },
        },
        Output: '{% $states.result.Payload %}',
        Retry: [...lambdaRetry, STORE_RETRY],
        Catch: toRecord('publish', "'commit'"),
        Next: 'Committed',
      },
      // published and superseded wrote their own receipt; only a hold goes on to RecordOutcome.
      Committed: {
        Type: 'Choice',
        Choices: [
          {
            Condition: "{% $states.input.outcome = 'held_quality' %}",
            Output:
              "{% {'step': $states.input.step, 'outcome': 'held_quality', 'category': $states.input.category, 'attempt_id': $states.input.attempt_id, 'shard': $states.input.shard} %}",
            Next: 'RecordOutcome',
          },
        ],
        Default: 'Published',
      },
      Published: { Type: 'Succeed' },
      RecordOutcome: {
        Type: 'Task',
        Resource: lambdaTask,
        Arguments: {
          FunctionName: publisher.functionArn,
          Payload:
            `{% {'operation': 'record', 'run_id': $run_id, 'redrive_count': ${redrive}, 'step': $states.input.step, 'outcome': $states.input.outcome, 'category': $states.input.category, 'phase': $states.input.phase, 'error': $states.input.error, 'attempt_id': $states.input.attempt_id, 'shard': $states.input.shard, 'run_identity': $states.input.run_identity, 'run_fingerprint': $run_fingerprint, 'expected_fence': $expected_fence, 'operator_reason': $operator_reason} %}`,
        },
        Output: '{% $states.result.Payload %}',
        Retry: lambdaRetry,
        Next: 'Recorded',
      },
      Recorded: { Type: 'Succeed' },
      Failed: {
        Type: 'Fail',
        Error: 'DemandForecastFailed',
        Cause: 'A demand-forecast step failed outside a shard; read the execution history.',
      },
    };
    // Dev only: a staged scale input enters at Forecast and continues into Publish and Commit
    // (design §10, "Scale fixtures"); its manifest carries the fence it read from CURRENT.
    const probe = config.envName === 'dev';
    if (probe) {
      states.ProbeStart = {
        Type: 'Choice',
        Choices: [
          {
            Condition: '{% $exists($states.input.probe) %}',
            Assign: {
              run_id: '{% $states.input.probe.run_id %}',
              manifest_key: '{% $states.input.probe.manifest_key %}',
              build_attempt_id: '{% $states.input.probe.build_attempt_id %}',
              run_hash12: '{% $states.input.probe.run_hash12 %}',
              // The staged run's identity and fence, so a probe failure is recorded against the
              // fence it read and is `superseded` after a rollback (ruling 2026-11-16 R1-1).
              run_fingerprint: '{% $states.input.probe.run_fingerprint %}',
              expected_fence: '{% $states.input.probe.expected_fence %}',
              operator_reason: null,
            },
            Output: "{% {'shards': $states.input.probe.shards} %}",
            Next: 'Forecast',
          },
        ],
        Default: 'SelectInput',
      };
    }
    const definition = {
      Comment: 'Weekly Vendor demand forecast: select, build, forecast, publish and commit.',
      QueryLanguage: 'JSONata',
      TimeoutSeconds: STATE_MACHINE_TIMEOUT_SECONDS,
      StartAt: probe ? 'ProbeStart' : 'SelectInput',
      States: states,
    };
    const machine = new sfn.StateMachine(this, 'ForecastStateMachine', {
      stateMachineName: `adlytics-${config.envName}-demand-forecast`,
      stateMachineType: sfn.StateMachineType.STANDARD,
      definitionBody: sfn.DefinitionBody.fromString(this.toJsonString(definition)),
      role: machineRole,
    });

    // ---- ForecastSchedule (design §6d "Trigger"; OQ-S2-3 decision in build-s2/state.md).
    // Scheduler names the execution with its own execution ID and passes the scheduled time,
    // from which SelectInput derives the check date and the run ID. No retry: a lost start is the
    // R7-M2 "no start" case, and the slot rule admits it on a later day.
    const scheduleRole = new iam.Role(this, 'ForecastScheduleRole', {
      assumedBy: new iam.ServicePrincipal('scheduler.amazonaws.com', {
        conditions: { StringEquals: { 'aws:SourceAccount': this.account } },
      }),
      description: 'Demand-forecast schedule: start the one state machine.',
    });
    new scheduler.Schedule(this, 'ForecastSchedule', {
      scheduleName: `adlytics-${config.envName}-demand-forecast-daily`,
      description: 'Daily demand-forecast check; SelectInput decides whether a run happens.',
      schedule: scheduler.ScheduleExpression.expression(FORECAST_SCHEDULE),
      target: new schedulerTargets.Universal({
        service: 'sfn',
        action: 'startExecution',
        role: scheduleRole,
        retryAttempts: 0,
        input: scheduler.ScheduleTargetInput.fromObject({
          StateMachineArn: machine.stateMachineArn,
          Name: scheduler.ContextAttribute.executionId,
          Input: JSON.stringify({ scheduled_time: scheduler.ContextAttribute.scheduledTime.toString() }),
        }),
        policyStatements: [
          new iam.PolicyStatement({
            sid: 'StartForecastExecution',
            actions: ['states:StartExecution'],
            resources: [machine.stateMachineArn],
          }),
        ],
      }),
    });

    // ---- Alarms (design §9): each metric from the generated operational-metrics projection.
    const action = staffedAlarmAction(this, config);
    const day = cdk.Duration.days(1);
    const metric = (
      entry: (typeof OPERATIONAL_METRICS)[keyof typeof OPERATIONAL_METRICS],
      dimensions: Record<string, string> = {},
    ): cloudwatch.Metric =>
      new cloudwatch.Metric({
        namespace: entry.namespace,
        metricName: entry.name,
        statistic: entry.statistic,
        dimensionsMap: dimensions,
        period: day,
      });
    const outcome = (value: 'failed' | 'held_quality'): cloudwatch.Metric =>
      metric(OPERATIONAL_METRICS.ForecastRunOutcome, { outcome: value });
    const alarm = (
      id: string,
      description: string,
      props: Omit<cloudwatch.AlarmProps, 'alarmName' | 'alarmDescription'>,
    ): void => {
      new cloudwatch.Alarm(this, id, {
        alarmName: `adlytics-${config.envName}-${id}`,
        alarmDescription: description,
        ...props,
      }).addAlarmAction(action);
    };
    const statesMetric = { period: day, statistic: 'Sum' };
    alarm('DemandForecastRunFailed', 'A demand-forecast run failed, inside or outside the catch.', {
      metric: new cloudwatch.MathExpression({
        expression: 'FILL(failed, 0) + FILL(executionsFailed, 0) + FILL(executionsTimedOut, 0) + FILL(executionsAborted, 0)',
        usingMetrics: {
          failed: outcome('failed'),
          executionsFailed: machine.metricFailed(statesMetric),
          executionsTimedOut: machine.metricTimedOut(statesMetric),
          executionsAborted: machine.metricAborted(statesMetric),
        },
        period: day,
      }),
      threshold: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      evaluationPeriods: 1,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    alarm('DemandForecastHeld', 'A demand-forecast run was held by a quality check.', {
      metric: outcome('held_quality'),
      threshold: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      evaluationPeriods: 1,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    // Ruling 2026-11-16 R1-4: every daily SelectInput emits UnknownSalesDays, but the age only once
    // a generation is CURRENT. A day with no check reads as just over stale_after_days, so a
    // stopped check alarms; a set never published, with its checks running, stays OK.
    alarm('DemandForecastStale', 'The forecast planners see is older than stale_after_days, or the daily check stopped.', {
      metric: new cloudwatch.MathExpression({
        expression: `IF(FILL(checks, 0) < 1, ${STALE_AFTER_DAYS + 1}, FILL(age, 0))`,
        usingMetrics: {
          checks: new cloudwatch.Metric({
            namespace: OPERATIONAL_METRICS.UnknownSalesDays.namespace,
            metricName: OPERATIONAL_METRICS.UnknownSalesDays.name,
            statistic: 'SampleCount',
            period: day,
          }),
          age: metric(OPERATIONAL_METRICS.ForecastCurrentAgeDays),
        },
        period: day,
      }),
      threshold: STALE_AFTER_DAYS,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      evaluationPeriods: 2,
      datapointsToAlarm: 2,
      treatMissingData: cloudwatch.TreatMissingData.BREACHING,
    });
    // Emitted once per published generation; between publications the alarm stays OK.
    alarm('DemandForecastInferredZeroShare', 'Over 5% of known context weeks were filled as inferred zero.', {
      metric: metric(OPERATIONAL_METRICS.InferredZeroWeekShare),
      threshold: INFERRED_ZERO_SHARE_PERCENT,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      evaluationPeriods: 1,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    // A newly unknown sales day alarms; the alarm clears when the count stops rising (R3-21).
    alarm('DemandForecastUnknownDays', 'A sales day in the forecast window became unknown.', {
      metric: new cloudwatch.MathExpression({
        expression: 'DIFF(unknownSalesDays)',
        usingMetrics: { unknownSalesDays: metric(OPERATIONAL_METRICS.UnknownSalesDays) },
        period: day,
      }),
      threshold: 0,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      evaluationPeriods: 1,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });

    applyFunctionErrorAlarms(this, config, action);
    applyStandardTags(this, config);
    applyRemovalPolicy(this, cdk.RemovalPolicy.DESTROY);
  }
}
