// SPDX-License-Identifier: MIT-0
import * as cdk from 'aws-cdk-lib';
import * as eventSources from 'aws-cdk-lib/aws-lambda-event-sources';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';

import { modelInvokeStatements } from '../bedrock-model-access';
import { AppConfig, paramPath } from '../config';
import { arrivalAlarm, staffedAlarmAction } from '../constructs/observability/async-failure';
import { grantOwnLogWrites, lambdaRole } from '../mixins/logging';
import { applyFunctionErrorAlarms } from '../mixins/function-error-alarms';
import { applyRemovalPolicy, applyStandardTags } from '../mixins/tagging';
import { grantSpanExport, tracingEnvironment } from '../telemetry';

export interface InsightContinuationStackProps extends cdk.StackProps {
  readonly config: AppConfig;
  readonly sourceAssetPath: string;
}

/** One value drives the queue redrive policy and the worker's final-receive degrade. */
const MAX_RECEIVE_COUNT = 3;

/** Disposable queue/worker pair completing durable exact cache-miss continuations. */
export class InsightContinuationStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: InsightContinuationStackProps) {
    super(scope, id, props);
    const { config } = props;
    const read = (key: Parameters<typeof paramPath>[1]): string =>
      ssm.StringParameter.valueForStringParameter(this, paramPath(config.envName, key));
    const cacheName = read('insightCacheTableName');
    const cacheArn = read('insightCacheTableArn');
    const registryName = read('insightPromptRegistryTableName');
    const registryArn = read('insightPromptRegistryTableArn');
    const dependencies = lambda.LayerVersion.fromLayerVersionArn(
      this, 'PythonDependencies', read('dependenciesLayerArn'),
    );
    const deadLetterQueue = new sqs.Queue(this, 'ContinuationDeadLetter', {
      queueName: `adlytics-${config.envName}-InsightContinuationDlq`,
      encryption: sqs.QueueEncryption.SQS_MANAGED,
      enforceSSL: true,
      retentionPeriod: cdk.Duration.days(14),
    });
    const queue = new sqs.Queue(this, 'ContinuationQueue', {
      queueName: `adlytics-${config.envName}-InsightContinuation`,
      encryption: sqs.QueueEncryption.SQS_MANAGED,
      enforceSSL: true,
      retentionPeriod: cdk.Duration.days(1),
      // Above the worker's 90s timeout so an in-flight fill is never redelivered
      // concurrently (the event-source mapping requires it at synth).
      visibilityTimeout: cdk.Duration.seconds(95),
      deadLetterQueue: { queue: deadLetterQueue, maxReceiveCount: MAX_RECEIVE_COUNT },
    });
    // Mirrors the ingestion dead-letter alarm, on arrival. The worker degrades an exhausted
    // generation to a durable unavailable cache row on its final receive, so anything that
    // still reaches this queue is an unexpected failure class.
    const staffedAction = staffedAlarmAction(this, config);
    arrivalAlarm(this, 'DeadLetteredContinuations', {
      alarmName: `adlytics-${config.envName}-insight-continuation-dead-lettered`,
      alarmDescription:
        'An insight continuation was never completed or degraded. The retry-exhausted path ' +
        'writes a durable unavailable cache row, so a dead-lettered message is a live defect.',
      queue: deadLetterQueue,
      action: staffedAction,
    });
    const functionName = `adlytics-${config.envName}-InsightContinuation`;
    const logGroup = new logs.LogGroup(this, 'ContinuationLogs', {
      logGroupName: `/aws/lambda/${functionName}`,
      retention: config.logRetentionDays,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    const role = lambdaRole(this, 'ContinuationWorkerServiceRole');
    grantOwnLogWrites(role, logGroup);
    const worker = new lambda.Function(this, 'ContinuationWorker', {
      functionName,
      role,
      runtime: new lambda.Runtime(config.pythonRuntime, lambda.RuntimeFamily.PYTHON),
      architecture: this.lambdaArchitecture(config),
      code: lambda.Code.fromAsset(props.sourceAssetPath),
      handler: 'adapters.jobs.insight_continuation.handler.handler',
      layers: [dependencies],
      environment: {
        ...tracingEnvironment(config),
        ADL_INSIGHT_CACHE_TABLE: cacheName,
        ADL_INSIGHT_PROMPT_REGISTRY_TABLE: registryName,
        ADL_INSIGHT_QUICK_MODEL_ID: config.insight.quickModelId,
        ADL_INSIGHT_DEEP_MODEL_ID: config.insight.deepModelId,
        // Dependency-failure recovery gets a patient transport boundary independent of the
        // request path. Opus deep generations measured 14-17s live (2026-09-24) and exceed
        // the request path's 20s on full evidence rosters; 60s lets the continuation
        // converge on its first receive instead of grinding retry cycles. It remains below
        // the worker timeout; no request deadline is implied.
        ADL_INSIGHT_GENERATION_TIMEOUT_SECONDS: '60',
        // The queue's own redrive policy, restated to the worker so its final receive
        // degrades to a durable unavailable cache row instead of dead-lettering silently.
        ADL_INSIGHT_CONTINUATION_MAX_RECEIVE_COUNT: String(MAX_RECEIVE_COUNT),
      },
      // Cache fill can make one bounded Bedrock call; the envelope absorbs one full 60s
      // deep transport plus registry/cache I/O and stays below queue visibility.
      memorySize: 512,
      timeout: cdk.Duration.seconds(90),
      reservedConcurrentExecutions: 2,
      logGroup,
    });
    grantSpanExport(worker);
    worker.addEventSource(new eventSources.SqsEventSource(queue, {
      batchSize: 1,
      maxConcurrency: 2,
      reportBatchItemFailures: true,
    }));
    worker.addToRolePolicy(new iam.PolicyStatement({
      actions: ['dynamodb:GetItem'], resources: [registryArn, cacheArn],
    }));
    worker.addToRolePolicy(new iam.PolicyStatement({
      actions: ['dynamodb:PutItem', 'dynamodb:Query', 'dynamodb:UpdateItem'], resources: [cacheArn],
    }));
    for (const statement of modelInvokeStatements(
      this,
      'InvokeInsightModels',
      [config.insight.quickModelId, config.insight.deepModelId],
      ['bedrock:InvokeModel'],
    )) worker.addToRolePolicy(statement);
    for (const [id, key, value, description] of [
      ['ParamContinuationQueueUrl', 'insightContinuationQueueUrl', queue.queueUrl, 'Durable insight continuation queue URL.'],
      ['ParamContinuationQueueArn', 'insightContinuationQueueArn', queue.queueArn, 'Durable insight continuation queue ARN.'],
    ] as const) {
      new ssm.StringParameter(this, id, { parameterName: paramPath(config.envName, key), stringValue: value, description });
    }
    applyFunctionErrorAlarms(this, config, staffedAction);
    applyRemovalPolicy(this, cdk.RemovalPolicy.DESTROY);
    applyStandardTags(this, config);
  }

  private lambdaArchitecture(config: AppConfig): lambda.Architecture {
    return { arm64: lambda.Architecture.ARM_64 }[config.lambdaArchitecture];
  }
}
