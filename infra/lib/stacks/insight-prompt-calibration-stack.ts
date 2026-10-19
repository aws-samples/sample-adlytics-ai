// SPDX-License-Identifier: MIT-0
import * as cdk from 'aws-cdk-lib';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as eventSources from 'aws-cdk-lib/aws-lambda-event-sources';
import * as events from 'aws-cdk-lib/aws-events';
import * as eventTargets from 'aws-cdk-lib/aws-events-targets';
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

export interface InsightPromptCalibrationStackProps extends cdk.StackProps {
  readonly config: AppConfig;
  readonly sourceAssetPath: string;
}

const MAX_RECEIVE_COUNT = 3;
const PROVIDER_TIMEOUT_SECONDS = 45;
const LEASE_SECONDS = 85;
const WORKER_TIMEOUT_SECONDS = 90;
const VISIBILITY_TIMEOUT_SECONDS = 95;

/** Disposable queued worker for calibrating retained immutable prompt versions. */
export class InsightPromptCalibrationStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: InsightPromptCalibrationStackProps) {
    super(scope, id, props);
    const { config } = props;
    const read = (key: Parameters<typeof paramPath>[1]): string =>
      ssm.StringParameter.valueForStringParameter(this, paramPath(config.envName, key));
    const registryName = read('insightPromptRegistryTableName');
    const registryArn = read('insightPromptRegistryTableArn');
    const dependencies = lambda.LayerVersion.fromLayerVersionArn(
      this, 'PythonDependencies', read('dependenciesLayerArn'),
    );

    const deadLetterQueue = new sqs.Queue(this, 'CalibrationDeadLetter', {
      queueName: `adlytics-${config.envName}-InsightPromptCalibrationDlq`,
      encryption: sqs.QueueEncryption.SQS_MANAGED,
      enforceSSL: true,
      retentionPeriod: cdk.Duration.days(14),
    });
    const queue = new sqs.Queue(this, 'CalibrationQueue', {
      queueName: `adlytics-${config.envName}-InsightPromptCalibration`,
      encryption: sqs.QueueEncryption.SQS_MANAGED,
      enforceSSL: true,
      retentionPeriod: cdk.Duration.days(1),
      visibilityTimeout: cdk.Duration.seconds(VISIBILITY_TIMEOUT_SECONDS),
      deadLetterQueue: { queue: deadLetterQueue, maxReceiveCount: MAX_RECEIVE_COUNT },
    });

    const staffedAction = staffedAlarmAction(this, config);
    arrivalAlarm(this, 'DeadLetteredCalibrations', {
      alarmName: `adlytics-${config.envName}-insight-prompt-calibration-dead-lettered`,
      alarmDescription:
        'A prompt calibration exhausted three receives. Its retained version stays pending; ' +
        'inspect the worker failure before redrive.',
      queue: deadLetterQueue,
      action: staffedAction,
    });
    const old = new cloudwatch.Alarm(this, 'OldestCalibration', {
      alarmName: `adlytics-${config.envName}-insight-prompt-calibration-oldest`,
      alarmDescription:
        'A prompt calibration has remained queued beyond two worker envelopes.',
      metric: queue.metricApproximateAgeOfOldestMessage({
        period: cdk.Duration.minutes(1),
        statistic: cloudwatch.Stats.MAXIMUM,
      }),
      threshold: WORKER_TIMEOUT_SECONDS * 2,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      evaluationPeriods: 1,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    old.addAlarmAction(staffedAction);

    const functionName = `adlytics-${config.envName}-InsightPromptCalibration`;
    const logGroup = new logs.LogGroup(this, 'CalibrationLogs', {
      logGroupName: `/aws/lambda/${functionName}`,
      retention: config.logRetentionDays,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    const role = lambdaRole(this, 'CalibrationWorkerServiceRole');
    grantOwnLogWrites(role, logGroup);
    const worker = new lambda.Function(this, 'CalibrationWorker', {
      functionName,
      role,
      runtime: new lambda.Runtime(config.pythonRuntime, lambda.RuntimeFamily.PYTHON),
      architecture: { arm64: lambda.Architecture.ARM_64 }[config.lambdaArchitecture],
      code: lambda.Code.fromAsset(props.sourceAssetPath),
      handler: 'adapters.jobs.insight_prompt_calibration.handler.handler',
      layers: [dependencies],
      environment: {
        ...tracingEnvironment(config),
        ADL_INSIGHT_PROMPT_REGISTRY_TABLE: registryName,
        ADL_INSIGHT_PROMPT_CALIBRATION_QUEUE_URL: queue.queueUrl,
        ADL_INSIGHT_QUICK_MODEL_ID: config.insight.quickModelId,
        ADL_INSIGHT_DEEP_MODEL_ID: config.insight.deepModelId,
        ADL_INSIGHT_GENERATION_TIMEOUT_SECONDS: String(PROVIDER_TIMEOUT_SECONDS),
        ADL_INSIGHT_PROMPT_CALIBRATION_LEASE_SECONDS: String(LEASE_SECONDS),
      },
      // Two generation calls and the independent judge calls run in bounded waves; 512 MB
      // gives the orchestration CPU without changing the downstream concurrency bound.
      memorySize: 512,
      timeout: cdk.Duration.seconds(WORKER_TIMEOUT_SECONDS),
      reservedConcurrentExecutions: 2,
      logGroup,
    });
    grantSpanExport(worker);
    worker.addEventSource(new eventSources.SqsEventSource(queue, {
      batchSize: 1,
      maxConcurrency: 2,
      reportBatchItemFailures: true,
    }));
    new events.Rule(this, 'CalibrationOutboxSchedule', {
      schedule: events.Schedule.rate(cdk.Duration.minutes(1)),
      targets: [new eventTargets.LambdaFunction(worker)],
    });
    worker.addToRolePolicy(new iam.PolicyStatement({
      actions: [
        'dynamodb:GetItem',
        'dynamodb:Query',
        'dynamodb:UpdateItem',
        'dynamodb:PutItem',
        'dynamodb:DeleteItem',
        'dynamodb:ConditionCheckItem',
      ],
      resources: [registryArn],
    }));
    worker.addToRolePolicy(new iam.PolicyStatement({
      actions: ['sqs:SendMessage'], resources: [queue.queueArn],
    }));
    for (const statement of modelInvokeStatements(
      this,
      'InvokeInsightModels',
      [config.insight.quickModelId, config.insight.deepModelId],
      ['bedrock:InvokeModel'],
    )) worker.addToRolePolicy(statement);

    for (const [id, key, value, description] of [
      ['ParamCalibrationQueueUrl', 'insightPromptCalibrationQueueUrl', queue.queueUrl, 'Prompt calibration work queue URL.'],
      ['ParamCalibrationQueueArn', 'insightPromptCalibrationQueueArn', queue.queueArn, 'Prompt calibration work queue ARN.'],
    ] as const) {
      new ssm.StringParameter(this, id, {
        parameterName: paramPath(config.envName, key), stringValue: value, description,
      });
    }
    applyFunctionErrorAlarms(this, config, staffedAction);
    applyRemovalPolicy(this, cdk.RemovalPolicy.DESTROY);
    applyStandardTags(this, config);
  }
}
