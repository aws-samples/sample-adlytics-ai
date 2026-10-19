// SPDX-License-Identifier: MIT-0
import * as cdk from 'aws-cdk-lib';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as events from 'aws-cdk-lib/aws-events';
import * as targets from 'aws-cdk-lib/aws-events-targets';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';

import {
  AppConfig,
  operationalStatusFunctionName,
  paramPath,
  validateIngestionConfig,
} from '../config';
import { grantSpanExport, tracingEnvironment } from '../telemetry';
import { CANONICAL_DATASET_KEYS } from './ingestion-stage1';
import { boundedStreamMapping } from '../constructs/observability/async-failure';

const OUTBOX_PENDING_INDEX = 'stage1-outbox-pending';
// Every partition that can hold a Stage-1 outbox authority: plan/unit items, table-attempt USAGE#
// settlements, the STATUS_FLOW# lifecycle EVENT# items of plan, unit, and inline-run flows, and
// the dataset-prefixed publication partitions that hold headless pointer EVENT# items.
export const PUBLICATION_AUTHORITY_KEYS: readonly string[] = [
  'WRITE_PLAN#*',
  'TABLE_ATTEMPT#*',
  'STATUS_FLOW#*',
  // Every canonical dataset, from the one contract-pinned list (ingestion-stage1.ts).
  ...CANONICAL_DATASET_KEYS,
];

export interface OperationalStatusStage1Props {
  readonly config: AppConfig;
  readonly sourceAssetPath: string;
  readonly dependencies: lambda.ILayerVersion;
  readonly statusQueue: sqs.Queue;
  readonly projectionTableName: string;
  readonly projectionTableArn: string;
  readonly publicationsTableName: string;
  readonly publicationsTableArn: string;
  readonly publicationsStreamArn: string;
  /** The staffed destination the stream failure alarm acts on. */
  readonly alarmAction: cloudwatch.IAlarmAction;
}

/** Stage-1 lifecycle delivery and protected outbox recovery foundations. */
export class OperationalStatusStage1 extends Construct {
  private readonly stack: cdk.Stack;

  constructor(scope: Construct, id: string, props: OperationalStatusStage1Props) {
    super(scope, id);
    this.stack = cdk.Stack.of(this);
    const { config } = props;
    const operational = config.operationalStatus as unknown as Record<string, unknown>;
    this.requireExactPositiveInteger(
      operational.stage1LifecycleReconcileBatchSize,
      10,
      'operationalStatus.stage1LifecycleReconcileBatchSize',
    );
    this.requireExactPositiveInteger(
      operational.stage1OutboxMaxConsecutiveFailedDeliveryAttempts,
      3,
      'operationalStatus.stage1OutboxMaxConsecutiveFailedDeliveryAttempts',
    );

    // Load Path §2.5: the producer's stream mapping and recovery schedule render only the typed
    // `stage1RecoveryEnabled` boolean; the handoff composition never gates them.
    validateIngestionConfig(config.ingestion);
    const stage1Enabled = config.ingestion.stage1RecoveryEnabled;

    const producer = this.functionWithLogs(props, {
      id: 'Stage1LifecycleProducer',
      handler: 'adapters.jobs.operational_status.stage1_lifecycle.handler',
      memorySize: 512,
      timeoutSeconds: 60,
      reservedConcurrency: 1,
      environment: {
        ...tracingEnvironment(config),
        ADL_OPERATIONAL_STATUS_QUEUE_URL: props.statusQueue.queueUrl,
        ADL_SEMANTIC_PUBLICATIONS_TABLE: props.publicationsTableName,
        ADL_OPERATIONAL_STATUS_TABLE: props.projectionTableName,
        ADL_STAGE1_LIFECYCLE_RECONCILE_BATCH_SIZE: String(
          config.operationalStatus.stage1LifecycleReconcileBatchSize,
        ),
        ADL_STAGE1_OUTBOX_MAX_CONSECUTIVE_FAILED_DELIVERY_ATTEMPTS: String(
          config.operationalStatus.stage1OutboxMaxConsecutiveFailedDeliveryAttempts,
        ),
      },
    });
    producer.addToRolePolicy(new iam.PolicyStatement({
      actions: ['dynamodb:DescribeStream', 'dynamodb:GetRecords', 'dynamodb:GetShardIterator', 'dynamodb:ListStreams'],
      resources: [props.publicationsStreamArn],
    }));
    this.addDynamo(producer, ['dynamodb:GetItem'], props.publicationsTableArn, PUBLICATION_AUTHORITY_KEYS);
    this.addDynamo(producer, ['dynamodb:GetItem'], props.projectionTableArn, ['STATUS_OBSERVATION#*']);
    this.addDynamo(producer, ['dynamodb:UpdateItem'], props.publicationsTableArn, PUBLICATION_AUTHORITY_KEYS);
    this.addDynamo(producer, ['dynamodb:UpdateItem'], props.projectionTableArn, ['STATUS_OBSERVATION#*']);
    this.addDynamo(
      producer,
      ['dynamodb:UpdateItem'],
      props.publicationsTableArn,
      PUBLICATION_AUTHORITY_KEYS,
      'TransactWriteItems',
    );
    this.addDynamo(
      producer,
      ['dynamodb:UpdateItem'],
      props.projectionTableArn,
      ['STATUS_OBSERVATION#*'],
      'TransactWriteItems',
    );
    this.addDynamo(
      producer,
      ['dynamodb:PutItem'],
      props.projectionTableArn,
      ['CASE#*', 'STAGE1_OUTBOX_OPEN_CASES'],
      'TransactWriteItems',
    );
    this.addDynamo(
      producer,
      ['dynamodb:Query'],
      `${props.publicationsTableArn}/index/${OUTBOX_PENDING_INDEX}`,
      ['pending'],
    );
    producer.addToRolePolicy(new iam.PolicyStatement({
      actions: ['sqs:SendMessage'],
      resources: [props.statusQueue.queueArn],
    }));

    boundedStreamMapping(this, 'Stage1PublicationsStreamMapping', {
      fn: producer,
      eventSourceArn: props.publicationsStreamArn,
      enabled: stage1Enabled,
      startingPosition: 'LATEST',
      batchSize: 10,
      parallelizationFactor: 1,
      functionResponseTypes: ['ReportBatchItemFailures'],
      filterCriteria: {
        filters: [
          {
            pattern: JSON.stringify({
              eventName: ['INSERT', 'MODIFY'],
              dynamodb: {
                NewImage: {
                  outbox_status: { S: ['pending'] },
                  record_key: { S: [{ prefix: 'EVENT#' }, { prefix: 'USAGE#' }] },
                },
              },
            }),
          },
        ],
      },
      bounds: config.asyncFailureBounds,
      failureQueue: {
        id: 'Stage1PublicationsStreamFailureQueue',
        queueName: `adlytics-${config.envName}-stage1-publications-stream-failures`,
      },
      alarm: {
        id: 'Stage1PublicationsStreamFailureAlarm',
        alarmName: `adlytics-${config.envName}-stage1-publications-stream-exhausted`,
        alarmDescription:
          'A publications-stream batch exhausted its bounded Stage-1 lifecycle producer retries; '
          + 'its outbox items stay pending for the one-minute recovery schedule. Disposition: '
          + 'attempts_failures status --consumer stage1-publications, then attempts_failures '
          + 'drain --consumer stage1-publications (operator-tools.md).',
        action: props.alarmAction,
      },
    });
    new events.Rule(this, 'Stage1LifecycleRecoverySchedule', {
      schedule: events.Schedule.rate(cdk.Duration.minutes(1)),
      enabled: stage1Enabled,
      targets: [new targets.LambdaFunction(producer)],
    });

    const recovery = this.functionWithLogs(props, {
      id: 'Stage1OutboxRecoveryFunction',
      handler: 'adapters.jobs.operational_status.stage1_outbox_recovery.handler',
      memorySize: 512,
      timeoutSeconds: 60,
      reservedConcurrency: 1,
      environment: {
        ...tracingEnvironment(config),
        ADL_SEMANTIC_PUBLICATIONS_TABLE: props.publicationsTableName,
        ADL_OPERATIONAL_STATUS_TABLE: props.projectionTableName,
      },
    });
    this.addDynamo(recovery, ['dynamodb:GetItem'], props.publicationsTableArn, PUBLICATION_AUTHORITY_KEYS);
    this.addDynamo(
      recovery,
      ['dynamodb:GetItem'],
      props.projectionTableArn,
      ['CASE#*', 'STAGE1_OUTBOX_OPEN_CASES', 'STATUS_OBSERVATION#*'],
    );
    this.addDynamo(
      recovery,
      ['dynamodb:UpdateItem'],
      props.publicationsTableArn,
      PUBLICATION_AUTHORITY_KEYS,
      'TransactWriteItems',
    );
    this.addDynamo(
      recovery,
      ['dynamodb:UpdateItem', 'dynamodb:DeleteItem'],
      props.projectionTableArn,
      ['CASE#*', 'STAGE1_OUTBOX_OPEN_CASES', 'STATUS_OBSERVATION#*'],
      'TransactWriteItems',
    );

    const operatorRole = new iam.Role(this, 'Stage1OutboxRecoveryOperatorRole', {
      assumedBy: new iam.AccountPrincipal(this.stack.account).withConditions({
        StringEquals: { 'aws:PrincipalAccount': this.stack.account },
      }),
      description: 'Same-account invoke-only role for Stage-1 outbox recovery.',
    });
    operatorRole.addToPolicy(new iam.PolicyStatement({
      actions: ['lambda:InvokeFunction'],
      resources: [recovery.functionArn],
    }));

    for (const [logicalId, key, value] of [
      ['Stage1OutboxRecoveryOperatorRoleArnParameter', 'stage1OutboxRecoveryOperatorRoleArn', operatorRole.roleArn],
      ['Stage1OutboxRecoveryFunctionArnParameter', 'stage1OutboxRecoveryFunctionArn', recovery.functionArn],
    ] as const) {
      new ssm.StringParameter(this, logicalId, {
        parameterName: paramPath(config.envName, key),
        stringValue: value,
      });
    }
  }

  private functionWithLogs(
    props: OperationalStatusStage1Props,
    options: Readonly<{
      id: 'Stage1LifecycleProducer' | 'Stage1OutboxRecoveryFunction';
      handler: string;
      memorySize: number;
      timeoutSeconds: number;
      reservedConcurrency: number;
      environment: Record<string, string>;
    }>,
  ): lambda.Function {
    const functionName = operationalStatusFunctionName(props.config.envName, options.id);
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

  private addDynamo(
    fn: lambda.Function,
    actions: readonly string[],
    resource: string,
    leadingKeys: readonly string[],
    enclosingOperation?: 'TransactWriteItems',
  ): void {
    fn.addToRolePolicy(new iam.PolicyStatement({
      actions: [...actions],
      resources: [resource],
      conditions: {
        'ForAllValues:StringLike': { 'dynamodb:LeadingKeys': [...leadingKeys] },
        ...(enclosingOperation === undefined
          ? {}
          : { StringEquals: { 'dynamodb:EnclosingOperation': enclosingOperation } }),
      },
    }));
  }

  private requireExactPositiveInteger(value: unknown, expected: number, field: string): void {
    if (typeof value !== 'number' || !Number.isInteger(value) || value !== expected) {
      throw new Error(`${field} must be the reviewed positive integer ${expected}.`);
    }
  }
}
