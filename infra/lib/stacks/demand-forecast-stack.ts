// SPDX-License-Identifier: MIT-0
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import * as path from 'node:path';
import * as cdk from 'aws-cdk-lib';
import { DockerImageAsset, Platform } from 'aws-cdk-lib/aws-ecr-assets';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
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
import { applyFunctionErrorAlarms } from '../mixins/function-error-alarms';
import { dataBucketName } from '../mixins/storage';
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
 * The weekly Vendor demand forecast's slice 1 engine (approval I-2): the job image, the
 * network-isolated Processing role, the input builder and a minimal Standard state machine
 * (`SelectInput` -> `BuildInput` -> `Forecast` map of `createProcessingJob.sync`), with a dev-only
 * `ProbeStart` entry. Nothing is published to a store in slice 1; `RecordOutcome`, the publisher
 * and the commit arrive with slice 2.
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

    const processingBucket = dataBucketName(config, 'demand-forecast');
    const artifact = s3Prefix(forecast.modelArtifactUri);
    const jobPrefix = processingJobPrefix(config.envName);
    const jobArn = arn('sagemaker', `processing-job/${jobPrefix}*`);

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
    ]) {
      input.addToRolePolicy(statement);
    }

    // ---- ForecastStateMachine (design §6d, slice 1 subset).
    const machineRole = new iam.Role(this, 'ForecastStateMachineRole', {
      assumedBy: new iam.ServicePrincipal('states.amazonaws.com'),
      description: 'Demand-forecast state machine: input builder and network-isolated jobs only.',
    });
    machineRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'InvokeInputBuilder',
        actions: ['lambda:InvokeFunction'],
        resources: [input.functionArn],
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
    const toFailed = [{ ErrorEquals: ['States.ALL'], Next: 'Failed' }];
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
      End: true,
    };
    const shardItem =
      "{% {'shard': $states.context.Map.Item.Value.shard, 'run_id': $run_id, 'manifest_key': $manifest_key, 'build_attempt_id': $build_attempt_id, 'run_hash12': $run_hash12} %}";
    const states: Record<string, unknown> = {
      SelectInput: {
        Type: 'Task',
        Resource: `arn:${this.partition}:states:::lambda:invoke`,
        Arguments: {
          FunctionName: input.functionArn,
          Payload: { operation: 'select', run_id: '{% $states.context.Execution.Name %}' },
        },
        Assign: { run_id: '{% $states.context.Execution.Name %}' },
        Output: '{% $states.result.Payload %}',
        Retry: lambdaRetry,
        Catch: toFailed,
        Next: 'Decide',
      },
      Decide: {
        Type: 'Choice',
        Choices: [{ Condition: "{% $states.input.outcome = 'run' %}", Next: 'BuildInput' }],
        Default: 'Held',
      },
      Held: { Type: 'Succeed' },
      BuildInput: {
        Type: 'Task',
        Resource: `arn:${this.partition}:states:::lambda:invoke`,
        Arguments: {
          FunctionName: input.functionArn,
          Payload: {
            operation: 'build',
            run_id: '{% $states.context.Execution.Name %}',
            redrive_count: '{% $states.context.Execution.RedriveCount %}',
            retry_count: '{% $states.context.State.RetryCount %}',
            input_fingerprint: '{% $states.input.input_fingerprint %}',
          },
        },
        Assign: {
          manifest_key: '{% $states.result.Payload.manifest_key %}',
          build_attempt_id: '{% $states.result.Payload.build_attempt_id %}',
          run_hash12: '{% $states.result.Payload.run_hash12 %}',
        },
        Output: "{% {'shards': $states.result.Payload.shards} %}",
        Retry: lambdaRetry,
        Catch: toFailed,
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
          States: { RunShard: runShard },
        },
        Catch: toFailed,
        Next: 'Forecasted',
      },
      Forecasted: { Type: 'Succeed' },
      Failed: {
        Type: 'Fail',
        Error: 'DemandForecastFailed',
        Cause: 'A demand-forecast step failed; read the execution history for the failing state.',
      },
    };
    // Dev only: a staged scale input enters at Forecast (design §10, "Scale fixtures").
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
            },
            Output: "{% {'shards': $states.input.probe.shards} %}",
            Next: 'Forecast',
          },
        ],
        Default: 'SelectInput',
      };
    }
    const definition = {
      Comment: 'Weekly Vendor demand forecast, slice 1: select, build, forecast each shard.',
      QueryLanguage: 'JSONata',
      TimeoutSeconds: STATE_MACHINE_TIMEOUT_SECONDS,
      StartAt: probe ? 'ProbeStart' : 'SelectInput',
      States: states,
    };
    new sfn.StateMachine(this, 'ForecastStateMachine', {
      stateMachineName: `adlytics-${config.envName}-demand-forecast`,
      stateMachineType: sfn.StateMachineType.STANDARD,
      definitionBody: sfn.DefinitionBody.fromString(this.toJsonString(definition)),
      role: machineRole,
    });

    applyFunctionErrorAlarms(this, config, staffedAlarmAction(this, config));
    applyStandardTags(this, config);
    applyRemovalPolicy(this, cdk.RemovalPolicy.DESTROY);
  }
}
