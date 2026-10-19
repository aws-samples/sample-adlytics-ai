// SPDX-License-Identifier: MIT-0
import * as cdk from 'aws-cdk-lib';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as events from 'aws-cdk-lib/aws-events';
import * as targets from 'aws-cdk-lib/aws-events-targets';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as sources from 'aws-cdk-lib/aws-lambda-event-sources';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';

import {
  AppConfig,
  operationalStatusFunctionName,
  operationalStatusQueueName,
  paramPath,
} from '../config';
import {
  arrivalAlarm,
  boundedStreamMapping,
  staffedAlarmAction,
} from '../constructs/observability/async-failure';
import { alarmingErrorsQuery } from '../constructs/observability/error-alarms';
import { OperationalStatusStage1 } from './operational-status-stage1';
import { applyFunctionErrorAlarms } from '../mixins/function-error-alarms';
import { applyRemovalPolicy, applyStandardTags } from '../mixins/tagging';
import { grantSpanExport, tracingEnvironment } from '../telemetry';

export interface OperationalStatusProcessorStackProps extends cdk.StackProps {
  readonly config: AppConfig;
  readonly sourceAssetPath: string;
}

const PROJECTOR_TIMEOUT_SECONDS = 30;
const QUEUE_VISIBILITY_SECONDS = 90;
const MAX_RECEIVE_COUNT = 3;
const PROCESSOR_CONCURRENCY = 2;
/** S5 (FR-3): the partition families the measurement protocol owns in the status table. */
export const MEASUREMENT_CONTRIBUTION_FAMILIES = [
  'MEASUREMENT#*',
  'METRIC_ACCEPT#*',
  'METRIC_DUE#*',
] as const;
export const MEASUREMENT_FAMILIES = [
  ...MEASUREMENT_CONTRIBUTION_FAMILIES,
  'AGGREGATE#*',
  'AGGREGATE_MEMBER#*',
  'METRIC_SNAPSHOT#*',
  'SINK_CONFIRMATION#*',
] as const;
/** S5 (FR-3): the reconciler's fixed operation, as `metric_reconciler.py` reports it. */
export const METRIC_RECONCILER_OPERATION = 'operational_status.metric_reconciler';
export const METRIC_RECONCILER_ALARM_SUFFIX = 'operational-status-metric-reconciler-alarming-errors';
/** The graph-store `item_kind`s the projector maps to explanation rows (`project_graph_item`):
 * S2's graph and receipt rows, and S6 part 2's node, claim and serving rows, whose commits carry
 * the `FlowTransitionLatency` and `ActivityAdmissionElapsed` contributions (design §2.10). The
 * same five kinds `graph_projection_rebuild` scans. */
export const GRAPH_PROJECTED_ITEM_KINDS = ['graph', 'receipt', 'node', 'claim', 'serving_binding'] as const;
/** Fifteen minutes behind the graph stream: far past a healthy batch, far before 24 h retention. */
export const GRAPH_PROJECTOR_STALL_MS = 15 * 60 * 1000;

/** Disposable direct-FIFO producers and processors for the retained operational projection. */
export class OperationalStatusProcessorStack extends cdk.Stack {
  private readonly config: AppConfig;

  constructor(scope: Construct, id: string, props: OperationalStatusProcessorStackProps) {
    super(scope, id, props);
    this.config = props.config;
    const { config } = props;
    if (!config.operationalStatus.provisioned) {
      throw new Error('Operational status processor requires operational status provisioning.');
    }

    const read = (key: Parameters<typeof paramPath>[1]): string =>
      ssm.StringParameter.valueForStringParameter(this, paramPath(config.envName, key));
    const projectionTable = read('operationalStatusProjectionTableName');
    const projectionTableArn = read('operationalStatusProjectionTableArn');
    const attemptsTable = read('semanticV2AttemptsTableName');
    const attemptsStreamArn = read('semanticV2AttemptsStreamArn');
    const publicationsTable = read('semanticV2PublicationsTableName');
    const publicationsStreamArn = read('semanticV2PublicationsStreamArn');
    const publicationsTableArn = cdk.Stack.of(this).formatArn({
      service: 'dynamodb',
      resource: 'table',
      resourceName: publicationsTable,
    });
    const attemptsTableArn = cdk.Stack.of(this).formatArn({
      service: 'dynamodb',
      resource: 'table',
      resourceName: attemptsTable,
    });
    const dependencies = lambda.LayerVersion.fromLayerVersionArn(
      this,
      'PythonDependencies',
      read('dependenciesLayerArn'),
    );

    const deadLetterQueue = new sqs.Queue(this, 'StatusDeadLetterQueue', {
      queueName: operationalStatusQueueName(config.envName, true),
      fifo: true,
      contentBasedDeduplication: false,
      encryption: sqs.QueueEncryption.SQS_MANAGED,
      enforceSSL: true,
      retentionPeriod: cdk.Duration.days(14),
    });
    const statusQueue = new sqs.Queue(this, 'StatusQueue', {
      queueName: operationalStatusQueueName(config.envName),
      fifo: true,
      contentBasedDeduplication: false,
      encryption: sqs.QueueEncryption.SQS_MANAGED,
      enforceSSL: true,
      retentionPeriod: cdk.Duration.days(14),
      visibilityTimeout: cdk.Duration.seconds(QUEUE_VISIBILITY_SECONDS),
      deadLetterQueue: { queue: deadLetterQueue, maxReceiveCount: MAX_RECEIVE_COUNT },
    });
    // FR-10 (S5): the status FIFO DLQ and graph alarms act on the selected staffed destination.
    // The email arm's topic is the ingestion topic, read from its published parameter.
    const staffedAction = staffedAlarmAction(this, config);
    // The name predates the arrival semantics and is kept: a name change replaces the alarm.
    arrivalAlarm(this, 'StatusDeadLetterQueueAlarm', {
      alarmName: `adlytics-${config.envName}-operational-status-dlq-not-empty`,
      alarmDescription: 'Operational status records exhausted FIFO projection retries.',
      queue: deadLetterQueue,
      action: staffedAction,
    });

    const attemptsProducer = this.functionWithLogs({
      id: 'AttemptsProducer',
      functionName: operationalStatusFunctionName(config.envName, 'AttemptsProducer'),
      handler: 'adapters.jobs.operational_status.attempts.handler',
      sourceAssetPath: props.sourceAssetPath,
      dependencies,
      memorySize: 512,
      timeoutSeconds: 30,
      reservedConcurrency: PROCESSOR_CONCURRENCY,
      environment: {
        ...tracingEnvironment(config),
        ADL_OPERATIONAL_STATUS_QUEUE_URL: statusQueue.queueUrl,
        ADL_SEMANTIC_ATTEMPTS_TABLE: attemptsTable,
      },
    });
    const projector = this.functionWithLogs({
      id: 'Projector',
      functionName: operationalStatusFunctionName(config.envName, 'Projector'),
      handler: 'adapters.jobs.operational_status.projector.handler',
      sourceAssetPath: props.sourceAssetPath,
      dependencies,
      memorySize: 512,
      timeoutSeconds: PROJECTOR_TIMEOUT_SECONDS,
      reservedConcurrency: PROCESSOR_CONCURRENCY,
      environment: {
        ...tracingEnvironment(config),
        ADL_OPERATIONAL_STATUS_TABLE: projectionTable,
        // Stage-1 dispatch re-attests plan/unit/inline-run lifecycle authority from the retained
        // STATUS_FLOW# items before reducing child outcomes and plan terminals (status LLD).
        ADL_SEMANTIC_PUBLICATIONS_TABLE: publicationsTable,
      },
    });
    const reconciler = this.functionWithLogs({
      id: 'Reconciler',
      functionName: operationalStatusFunctionName(config.envName, 'Reconciler'),
      handler: 'adapters.jobs.operational_status.reconciler.handler',
      sourceAssetPath: props.sourceAssetPath,
      dependencies,
      memorySize: 512,
      timeoutSeconds: 60,
      reservedConcurrency: 1,
      environment: {
        ...tracingEnvironment(config),
        ADL_OPERATIONAL_STATUS_TABLE: projectionTable,
        ADL_OPERATIONAL_STATUS_QUEUE_URL: statusQueue.queueUrl,
        ADL_SEMANTIC_ATTEMPTS_TABLE: attemptsTable,
      },
    });
    for (const fn of [attemptsProducer, projector, reconciler]) grantSpanExport(fn);

    attemptsProducer.addToRolePolicy(new iam.PolicyStatement({
      sid: 'ReadAttemptsStream',
      actions: [
        'dynamodb:DescribeStream',
        'dynamodb:GetRecords',
        'dynamodb:GetShardIterator',
        'dynamodb:ListStreams',
      ],
      resources: [attemptsStreamArn],
    }));
    attemptsProducer.addToRolePolicy(new iam.PolicyStatement({
      sid: 'ReattestAttempt',
      // GetItem re-attests one exact event; Query reads the attempt's exact history.
      actions: ['dynamodb:GetItem', 'dynamodb:Query'],
      resources: [attemptsTableArn],
    }));
    statusQueue.grantSendMessages(attemptsProducer);

    const projectionIndexesArn = `${projectionTableArn}/index/*`;
    const projectionReadActions = ['dynamodb:GetItem', 'dynamodb:Query', 'dynamodb:BatchGetItem'];
    const projectionWriteActions = [
      'dynamodb:ConditionCheckItem',
      'dynamodb:PutItem',
      'dynamodb:UpdateItem',
      'dynamodb:DeleteItem',
    ];
    projector.addToRolePolicy(new iam.PolicyStatement({
      sid: 'ReadProjection',
      actions: projectionReadActions,
      resources: [projectionTableArn, projectionIndexesArn],
    }));
    projector.addToRolePolicy(new iam.PolicyStatement({
      sid: 'ConditionallyWriteProjection',
      actions: projectionWriteActions,
      resources: [projectionTableArn],
    }));
    projector.addToRolePolicy(new iam.PolicyStatement({
      sid: 'ReattestStage1LifecycleAuthority',
      actions: ['dynamodb:GetItem'],
      resources: [publicationsTableArn],
      conditions: {
        'ForAllValues:StringLike': { 'dynamodb:LeadingKeys': ['STATUS_FLOW#*'] },
      },
    }));
    statusQueue.grantConsumeMessages(projector);
    projector.addEventSource(new sources.SqsEventSource(statusQueue, {
      batchSize: 10,
      maxConcurrency: PROCESSOR_CONCURRENCY,
      reportBatchItemFailures: true,
    }));

    reconciler.addToRolePolicy(new iam.PolicyStatement({
      sid: 'ReadProjectionAndPendingIndexes',
      actions: projectionReadActions,
      resources: [projectionTableArn, projectionIndexesArn],
    }));
    reconciler.addToRolePolicy(new iam.PolicyStatement({
      sid: 'ReduceParkedProjectionWork',
      actions: projectionWriteActions,
      resources: [projectionTableArn],
    }));
    reconciler.addToRolePolicy(new iam.PolicyStatement({
      sid: 'ReattestAttemptAuthority',
      actions: ['dynamodb:GetItem', 'dynamodb:Query'],
      resources: [attemptsTableArn],
    }));
    statusQueue.grantSendMessages(reconciler);

    // A record the producer can never apply (for example an attempt whose predecessor attempt
    // never started) used to be retried until the 24 h stream retention dropped it, holding its
    // shard with no alarm (2026-10-09). The retry is now bounded; the exhausted batch's stream
    // metadata goes to an on-failure queue, and each new arrival pages the staffed destination.
    // The queue is drained only by the operator (`attempts_failures drain`, operator-tools.md).
    // Arrivals, not standing depth: a queue left holding earlier records must not keep the alarm
    // in ALARM and hide the next exhaustion (the ingestion-dead-lettered blind spot).
    boundedStreamMapping(this, 'AttemptsStreamMapping', {
      fn: attemptsProducer,
      eventSourceArn: attemptsStreamArn,
      startingPosition: 'LATEST',
      batchSize: 10,
      parallelizationFactor: 1,
      functionResponseTypes: ['ReportBatchItemFailures'],
      bounds: config.asyncFailureBounds,
      failureQueue: {
        id: 'AttemptsStreamFailureQueue',
        queueName: `adlytics-${config.envName}-operational-status-attempts-failures`,
      },
      alarm: {
        id: 'AttemptsStreamFailureAlarm',
        alarmName: `adlytics-${config.envName}-operational-status-attempts-exhausted`,
        alarmDescription:
          'An attempt stream batch exhausted its bounded producer retries; its status events '
          + 'were not emitted. The failure queue holds its shard and sequence range. '
          + 'Disposition: attempts_failures status --consumer attempts, then attempts_failures '
          + 'drain --consumer attempts (operator-tools.md).',
        action: staffedAction,
      },
    });
    new events.Rule(this, 'ReconcilerSchedule', {
      schedule: events.Schedule.rate(cdk.Duration.minutes(1)),
      targets: [new targets.LambdaFunction(reconciler)],
    });

    for (const [logicalId, key, value, description] of [
      [
        'StatusQueueUrlParameter',
        'operationalStatusQueueUrl',
        statusQueue.queueUrl,
        'Dedicated operational status FIFO queue URL.',
      ],
      [
        'StatusQueueArnParameter',
        'operationalStatusQueueArn',
        statusQueue.queueArn,
        'Dedicated operational status FIFO queue ARN.',
      ],
    ] as const) {
      new ssm.StringParameter(this, logicalId, {
        parameterName: paramPath(config.envName, key),
        stringValue: value,
        description,
      });
    }

    // S2 status v2: the execution-graph table's stream feeds the graph projector, which writes
    // only the projection table. It holds stream-read actions on that one stream and no action on
    // any graph-table item, so it can never issue or accept a graph command.
    const graphStreamArn = read('executionGraphTableStreamArn');
    const graphProjector = this.functionWithLogs({
      id: 'GraphProjector',
      functionName: operationalStatusFunctionName(config.envName, 'GraphProjector'),
      handler: 'adapters.jobs.operational_status.projector.graph_handler',
      sourceAssetPath: props.sourceAssetPath,
      dependencies,
      memorySize: 512,
      timeoutSeconds: PROJECTOR_TIMEOUT_SECONDS,
      reservedConcurrency: 1,
      environment: {
        ...tracingEnvironment(config),
        ADL_OPERATIONAL_STATUS_TABLE: projectionTable,
        ADL_SEMANTIC_PUBLICATIONS_TABLE: publicationsTable,
      },
    });
    grantSpanExport(graphProjector);
    graphProjector.addToRolePolicy(new iam.PolicyStatement({
      sid: 'ReadExecutionGraphStream',
      actions: [
        'dynamodb:DescribeStream',
        'dynamodb:GetRecords',
        'dynamodb:GetShardIterator',
        'dynamodb:ListStreams',
      ],
      resources: [graphStreamArn],
    }));
    graphProjector.addToRolePolicy(new iam.PolicyStatement({
      sid: 'ReplayCheckProjection',
      actions: ['dynamodb:GetItem'],
      resources: [projectionTableArn],
    }));
    graphProjector.addToRolePolicy(new iam.PolicyStatement({
      sid: 'PutImmutableProjectionRows',
      actions: ['dynamodb:PutItem'],
      resources: [projectionTableArn],
      conditions: {
        // S5 (FR-3): a receipt row's measurement contribution, its PENDING acceptance and its
        // due marker commit in the row's own transaction, all create-once.
        'ForAllValues:StringLike': {
          'dynamodb:LeadingKeys': ['XROOT#*', ...MEASUREMENT_CONTRIBUTION_FAMILIES],
        },
      },
    }));
    this.buildMetricReconciler(
      props,
      dependencies,
      projectionTable,
      projectionTableArn,
      publicationsTable,
      staffedAction,
    );
    boundedStreamMapping(this, 'ExecutionGraphStreamMapping', {
      fn: graphProjector,
      eventSourceArn: graphStreamArn,
      // The stream starts empty when it is enabled; TRIM_HORIZON then misses nothing committed
      // between enablement and this mapping. Items committed before it are rebuilt by the
      // operator job `graph_projection_rebuild`.
      startingPosition: 'TRIM_HORIZON',
      batchSize: 10,
      parallelizationFactor: 1,
      functionResponseTypes: ['ReportBatchItemFailures'],
      filterCriteria: {
        filters: [
          {
            pattern: JSON.stringify({
              eventName: ['INSERT', 'MODIFY'],
              dynamodb: { NewImage: { item_kind: { S: [...GRAPH_PROJECTED_ITEM_KINDS] } } },
            }),
          },
        ],
      },
      bounds: config.asyncFailureBounds,
      failureQueue: {
        id: 'GraphProjectorStreamFailureQueue',
        queueName: `adlytics-${config.envName}-graph-projector-stream-failures`,
      },
      alarm: {
        id: 'GraphProjectorStreamFailureAlarm',
        alarmName: `adlytics-${config.envName}-graph-projector-stream-exhausted`,
        alarmDescription:
          'A graph-stream batch exhausted its bounded projector retries; its receipts were not '
          + 'projected. Disposition: attempts_failures status --consumer graph-projector, then '
          + 'attempts_failures drain --consumer graph-projector, then replay with '
          + 'graph_projection_rebuild (operator-tools.md).',
        action: staffedAction,
      },
    });
    // S2: a persistent projector failure holds the shard (the handler stops at the first failed
    // record, so order is kept) for up to the mapping's retry bound (100 attempts or 1 h). The
    // exhausted batch then goes to the graph-projector failure queue, whose arrival alarm pages,
    // and the explanation cannot see a receipt that was never projected until it is replayed.
    // The iterator age makes a stall inside the bound visible. Disposition is
    // `attempts_failures status|drain --consumer graph-projector`, then the operator job
    // `graph_projection_rebuild`, which replays the same pure map from the graph table.
    const stalled = new cloudwatch.Alarm(this, 'GraphProjectorIteratorAgeAlarm', {
      alarmName: `adlytics-${config.envName}-operational-status-graph-projector-stalled`,
      alarmDescription:
        'The graph projector is behind the execution-graph stream; recover with graph_projection_rebuild.',
      metric: graphProjector.metric('IteratorAge', {
        period: cdk.Duration.minutes(5),
        statistic: 'Maximum',
      }),
      threshold: GRAPH_PROJECTOR_STALL_MS,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      evaluationPeriods: 1,
      datapointsToAlarm: 1,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    stalled.addAlarmAction(staffedAction);
    new OperationalStatusStage1(this, 'Stage1', {
      config,
      sourceAssetPath: props.sourceAssetPath,
      dependencies,
      statusQueue,
      projectionTableName: projectionTable,
      projectionTableArn,
      publicationsTableName: publicationsTable,
      publicationsTableArn,
      publicationsStreamArn,
      alarmAction: staffedAction,
    });

    // Precedes IngestionStack in DEPLOY_ORDER, but already reads its published topic parameter
    // for the alarms above (the pre-existing first-deploy ordering hazard), so this adds none.
    applyFunctionErrorAlarms(this, config, staffedAction);
    applyRemovalPolicy(this, cdk.RemovalPolicy.DESTROY);
    applyStandardTags(this, config);
  }

  /**
   * S5 (FR-3): the measurement reconciler. It accepts each contribution once into its aggregate,
   * emits the complete snapshot as EMF gauges, and confirms delivery only from `GetMetricData`.
   * Its item actions are scoped to the measurement families of the status table.
   */
  private buildMetricReconciler(
    props: OperationalStatusProcessorStackProps,
    dependencies: lambda.ILayerVersion,
    projectionTable: string,
    projectionTableArn: string,
    publicationsTable: string,
    staffedAction: cloudwatch.IAlarmAction,
  ): void {
    const { config } = props;
    // The reconciler reports each failed contribution or snapshot once (including gate-blocking
    // MeasurementCorruptError) and still returns success, so Lambda Errors stays 0. This alarm
    // watches its typed invariant and dependency errors on the shared Errors metric instead.
    const failed = new cloudwatch.Alarm(this, 'MetricReconcilerAlarmingErrors', {
      alarmName: `adlytics-${config.envName}-${METRIC_RECONCILER_ALARM_SUFFIX}`,
      alarmDescription:
        `Measurement operation ${METRIC_RECONCILER_OPERATION} reported an invariant or dependency `
        + 'failure (FR-3): a contribution or snapshot is not accepted or not confirmed.',
      metric: new cloudwatch.MathExpression({
        expression: alarmingErrorsQuery(METRIC_RECONCILER_OPERATION),
        period: cdk.Duration.minutes(5),
      }),
      threshold: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      evaluationPeriods: 1,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    failed.addAlarmAction(staffedAction);
    const fn = this.functionWithLogs({
      id: 'MetricReconciler',
      functionName: operationalStatusFunctionName(config.envName, 'MetricReconciler'),
      handler: 'adapters.jobs.operational_status.metric_reconciler.handler',
      sourceAssetPath: props.sourceAssetPath,
      dependencies,
      memorySize: 512,
      timeoutSeconds: 120,
      reservedConcurrency: 1,
      environment: {
        ...tracingEnvironment(config),
        ADL_OPERATIONAL_STATUS_TABLE: projectionTable,
        ADL_SEMANTIC_PUBLICATIONS_TABLE: publicationsTable,
      },
    });
    grantSpanExport(fn);
    fn.addToRolePolicy(new iam.PolicyStatement({
      sid: 'ReadMeasurementAuthority',
      actions: ['dynamodb:GetItem', 'dynamodb:Query'],
      resources: [projectionTableArn],
      conditions: {
        'ForAllValues:StringLike': { 'dynamodb:LeadingKeys': [...MEASUREMENT_FAMILIES] },
      },
    }));
    fn.addToRolePolicy(new iam.PolicyStatement({
      sid: 'TransactMeasurementAuthority',
      actions: [
        'dynamodb:PutItem',
        'dynamodb:UpdateItem',
        'dynamodb:DeleteItem',
        'dynamodb:ConditionCheckItem',
      ],
      resources: [projectionTableArn],
      conditions: {
        'ForAllValues:StringLike': { 'dynamodb:LeadingKeys': [...MEASUREMENT_FAMILIES] },
        StringEquals: { 'dynamodb:EnclosingOperation': 'TransactWriteItems' },
      },
    }));
    fn.addToRolePolicy(new iam.PolicyStatement({
      sid: 'ConfirmFromTheSink',
      // GetMetricData has no resource-level scoping; it reads metrics and writes nothing.
      actions: ['cloudwatch:GetMetricData'],
      resources: ['*'],
    }));
    new events.Rule(this, 'MetricReconcilerSchedule', {
      ruleName: `adlytics-${config.envName}-OperationalStatusMetricReconcilerSchedule`,
      schedule: events.Schedule.rate(cdk.Duration.minutes(5)),
      targets: [new targets.LambdaFunction(fn)],
    });
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
    role.addToPolicy(new iam.PolicyStatement({
      actions: ['logs:CreateLogStream', 'logs:PutLogEvents'],
      resources: [`${logGroup.logGroupArn}:*`],
    }));
    return new lambda.Function(this, options.id, {
      functionName: options.functionName,
      runtime: new lambda.Runtime(this.config.pythonRuntime, lambda.RuntimeFamily.PYTHON),
      architecture: { arm64: lambda.Architecture.ARM_64 }[this.config.lambdaArchitecture],
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
}
