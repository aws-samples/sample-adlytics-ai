// SPDX-License-Identifier: MIT-0
import * as cdk from 'aws-cdk-lib';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as sources from 'aws-cdk-lib/aws-lambda-event-sources';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';

import {
  AppConfig,
  evaluationInputDeadLetterQueueName,
  evaluationInputQueueName,
  evaluationInputServiceName,
  evaluationOutboxDispatcherRoleName,
  evaluationWriterFunctionName,
  evaluationWriterRoleName,
  paramPath,
} from '../config';
import { onlyProducerSends } from '../constructs/agent-quality/record-integrity';
import { arrivalAlarm, staffedAlarmAction } from '../constructs/observability/async-failure';
import { grantOwnLogWrites, lambdaRole } from '../mixins/logging';
import { applyFunctionErrorAlarms } from '../mixins/function-error-alarms';
import { applyRemovalPolicy, applyStandardTags } from '../mixins/tagging';
import { grantSpanExport, tracingEnvironment } from '../telemetry';

export interface EvaluationInputStackProps extends cdk.StackProps {
  readonly config: AppConfig;
  readonly sourceAssetPath: string;
}

/** Disposable evaluation transport. Retained exact response evidence is owned by EvaluationEvidenceStack. */
export class EvaluationInputStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: EvaluationInputStackProps) {
    super(scope, id, props);
    const { config } = props;
    if (!config.evaluationInput.provisioned) throw new Error('Evaluation input is not provisioned.');

    const deadLetterQueue = new sqs.Queue(this, 'DeadLetterQueue', {
      queueName: evaluationInputDeadLetterQueueName(config.envName),
      encryption: sqs.QueueEncryption.SQS_MANAGED,
      enforceSSL: true,
      retentionPeriod: cdk.Duration.days(14),
    });
    const queue = new sqs.Queue(this, 'Queue', {
      queueName: evaluationInputQueueName(config.envName),
      encryption: sqs.QueueEncryption.SQS_MANAGED,
      enforceSSL: true,
      retentionPeriod: cdk.Duration.days(4),
      visibilityTimeout: cdk.Duration.seconds(120),
      deadLetterQueue: { queue: deadLetterQueue, maxReceiveCount: 3 },
    });
    // ADR-080 §3: only the outbox dispatcher tells the writer what to write.
    onlyProducerSends(queue, evaluationOutboxDispatcherRoleName(config.envName));
    const staffedAction = staffedAlarmAction(this, config);
    arrivalAlarm(this, 'DeadLetterQueueAlarm', {
      alarmName: `adlytics-${config.envName}-evaluation-input-dead-lettered`,
      alarmDescription:
        'A released response exhausted its three writer receives; it was not written to the '
        + 'evaluation input log, so it is not evaluated. Inspect the writer failure before redrive.',
      queue: deadLetterQueue,
      action: staffedAction,
    });

    const functionLogs = new logs.LogGroup(this, 'WriterLogs', {
      logGroupName: `/aws/lambda/${evaluationWriterFunctionName(config.envName)}`,
      retention: config.logRetentionDays,
    });
    const read = (key: Parameters<typeof paramPath>[1]): string =>
      ssm.StringParameter.valueForStringParameter(this, paramPath(config.envName, key));
    const inputLogGroupName = read('evaluationInputLogGroupName');
    const inputLogGroupArn = cdk.Stack.of(this).formatArn({
      service: 'logs',
      resource: 'log-group',
      resourceName: inputLogGroupName,
      arnFormat: cdk.ArnFormat.COLON_RESOURCE_NAME,
    });
    const dependencies = lambda.LayerVersion.fromLayerVersionArn(
      this,
      'PythonDependencies',
      read('dependenciesLayerArn'),
    );
    const writerReceiptsTableName = read('evaluationWriterReceiptsTableName');
    const writerReceiptsTableArn = read('evaluationWriterReceiptsTableArn');
    // ADR-080 §1: the writer receipts table's resource policy will name this role as its only writer.
    const role = lambdaRole(this, 'WriterRole', {
      roleName: evaluationWriterRoleName(config.envName),
    });
    grantOwnLogWrites(role, functionLogs);
    const writer = new lambda.Function(this, 'Writer', {
      functionName: evaluationWriterFunctionName(config.envName),
      role,
      runtime: new lambda.Runtime(config.pythonRuntime, lambda.RuntimeFamily.PYTHON),
      architecture: lambda.Architecture.ARM_64,
      code: lambda.Code.fromAsset(props.sourceAssetPath),
      handler: 'adapters.jobs.evaluation_writer.handler.handler',
      layers: [dependencies],
      environment: {
        ...tracingEnvironment(config),
        ADL_EVALUATION_INPUT_LOG_GROUP: inputLogGroupName,
        ADL_EVALUATION_INPUT_SERVICE_NAME: evaluationInputServiceName(config.envName),
        ADL_EVALUATION_WRITER_RECEIPTS_TABLE: writerReceiptsTableName,
        // The writer receipt expires with its turn's receipt (ADR-081 §5).
        ADL_RECEIPT_RETENTION_DAYS: String(config.receiptRetentionDays),
      },
      memorySize: 512,
      timeout: cdk.Duration.seconds(30),
      reservedConcurrentExecutions: 2,
      logGroup: functionLogs,
    });
    grantSpanExport(writer);
    writer.addEventSource(
      new sources.SqsEventSource(queue, {
        batchSize: 10,
        reportBatchItemFailures: true,
        maxConcurrency: 2,
      }),
    );
    writer.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['logs:CreateLogStream', 'logs:PutLogEvents'],
        resources: [inputLogGroupArn, `${inputLogGroupArn}:log-stream:*`],
      }),
    );
    // GetItem reads back a replayed conditional write so it is classified as exact (ADR-080 §3).
    writer.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['dynamodb:PutItem', 'dynamodb:GetItem'],
        resources: [writerReceiptsTableArn],
      }),
    );

    for (const [id, keyName, value, description] of [
      ['ParamQueueUrl', 'evaluationInputQueueUrl', queue.queueUrl, 'Released response queue URL.'],
      ['ParamQueueArn', 'evaluationInputQueueArn', queue.queueArn, 'Released response queue ARN.'],
      [
        'ParamServiceName',
        'evaluationInputServiceName',
        evaluationInputServiceName(config.envName),
        'Released response evaluation service name.',
      ],
    ] as const) {
      new ssm.StringParameter(this, id, {
        parameterName: paramPath(config.envName, keyName),
        stringValue: value,
        description,
      });
    }

    applyFunctionErrorAlarms(this, config, staffedAction);
    applyRemovalPolicy(this, cdk.RemovalPolicy.DESTROY);
    applyStandardTags(this, config);
  }
}
