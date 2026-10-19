// SPDX-License-Identifier: MIT-0
import * as fs from 'node:fs';
import * as path from 'node:path';
import {
  operationalStatusFunctionName,
  operationalStatusProjectionTableName,
  operationalStatusQueueName,
  paramPath,
} from '../lib/config';
import { ARRIVALS_EXPRESSION } from '../lib/constructs/observability/async-failure';
import { GRAPH_PROJECTED_ITEM_KINDS, GRAPH_PROJECTOR_STALL_MS } from '../lib/stacks/operational-status-processor-stack';
import { TEST_CONFIG } from './support/config';
import { everyTemplate, resourcesOfType } from './support/templates';
import { expectArrivalAlarm, expectBoundedStreamConsumer } from './support/async-failure';

const templates = everyTemplate();
const store = templates.OperationalStatusStoreStack as Record<string, unknown>;
const processor = templates.OperationalStatusProcessorStack as Record<string, unknown>;
type Resource = Record<string, unknown>;

describe('OperationalStatusStoreStack', () => {
  it('contains one retained on-demand encrypted PITR projection with TTL and bounded indexes', () => {
    const tables = resourcesOfType(store, 'AWS::DynamoDB::Table');
    expect(tables).toHaveLength(1);
    expect(tables[0]).toEqual(expect.objectContaining({
      TableName: operationalStatusProjectionTableName('dev'),
      BillingMode: 'PAY_PER_REQUEST',
      SSESpecification: { SSEEnabled: true },
      PointInTimeRecoverySpecification: { PointInTimeRecoveryEnabled: true },
      TimeToLiveSpecification: { AttributeName: 'expires_at', Enabled: true },
      KeySchema: [
        { AttributeName: 'partition_key', KeyType: 'HASH' },
        { AttributeName: 'sort_key', KeyType: 'RANGE' },
      ],
    }));
    expect((tables[0]?.GlobalSecondaryIndexes as Resource[]).map((index) => index.IndexName)).toEqual([
      'applied-event',
      'pending',
      'surface-sample-baseline',
    ]);
    const resources = Object.values(
      (store.Resources ?? {}) as Record<string, { Type: string; DeletionPolicy?: string; UpdateReplacePolicy?: string }>,
    );
    const table = resources.find((resource) => resource.Type === 'AWS::DynamoDB::Table');
    expect(table?.DeletionPolicy).toBe('Retain');
    expect(table?.UpdateReplacePolicy).toBe('Retain');
    expect(resourcesOfType(store, 'AWS::Lambda::Function')).toHaveLength(0);
    expect(resourcesOfType(store, 'AWS::SQS::Queue')).toHaveLength(0);
  });

  it('publishes only the typed projection name and ARN contracts', () => {
    const parameters = resourcesOfType(store, 'AWS::SSM::Parameter');
    expect(parameters).toHaveLength(2);
    expect(parameters).toEqual(expect.arrayContaining([
      expect.objectContaining({ Name: paramPath('dev', 'operationalStatusProjectionTableName') }),
      expect.objectContaining({ Name: paramPath('dev', 'operationalStatusProjectionTableArn') }),
    ]));
  });
});

describe('OperationalStatusProcessorStack', () => {
  it('creates an explicit encrypted FIFO queue and FIFO DLQ with redrive and an alarm', () => {
    const queues = resourcesOfType(processor, 'AWS::SQS::Queue');
    // The status FIFO and its DLQ, and one failure queue per stream mapping (three).
    expect(queues).toHaveLength(5);
    expect(queues).toEqual(expect.arrayContaining([
      expect.objectContaining({
        QueueName: operationalStatusQueueName('dev'),
        FifoQueue: true,
        ContentBasedDeduplication: false,
        SqsManagedSseEnabled: true,
        MessageRetentionPeriod: 1209600,
        VisibilityTimeout: 90,
        RedrivePolicy: expect.objectContaining({ maxReceiveCount: 3 }),
      }),
      expect.objectContaining({
        QueueName: operationalStatusQueueName('dev', true),
        FifoQueue: true,
        ContentBasedDeduplication: false,
        SqsManagedSseEnabled: true,
        MessageRetentionPeriod: 1209600,
      }),
    ]));
    expect(resourcesOfType(processor, 'AWS::CloudWatch::Alarm')).toContainEqual(
      expect.objectContaining({
        ComparisonOperator: 'GreaterThanThreshold',
        EvaluationPeriods: 1,
        Threshold: 0,
        TreatMissingData: 'notBreaching',
      }),
    );
  });

  it('uses separate explicit roles and log groups for the seven fixed handlers', () => {
    const functions = resourcesOfType(processor, 'AWS::Lambda::Function');
    expect(functions).toHaveLength(7);
    const handlers = new Map(functions.map((fn) => [fn.Handler, fn]));
    for (const [handler, functionName] of [
      ['adapters.jobs.operational_status.attempts.handler', operationalStatusFunctionName('dev', 'AttemptsProducer')],
      ['adapters.jobs.operational_status.projector.handler', operationalStatusFunctionName('dev', 'Projector')],
      ['adapters.jobs.operational_status.projector.graph_handler', operationalStatusFunctionName('dev', 'GraphProjector')],
      ['adapters.jobs.operational_status.reconciler.handler', operationalStatusFunctionName('dev', 'Reconciler')],
      ['adapters.jobs.operational_status.metric_reconciler.handler', operationalStatusFunctionName('dev', 'MetricReconciler')],
      ['adapters.jobs.operational_status.stage1_lifecycle.handler', operationalStatusFunctionName('dev', 'Stage1LifecycleProducer')],
      ['adapters.jobs.operational_status.stage1_outbox_recovery.handler', operationalStatusFunctionName('dev', 'Stage1OutboxRecoveryFunction')],
    ]) {
      expect(handlers.get(handler)).toEqual(expect.objectContaining({
        FunctionName: functionName,
        Architectures: ['arm64'],
        MemorySize: 512,
      }));
    }
    const roles = functions.map((fn) => JSON.stringify(fn.Role));
    expect(new Set(roles).size).toBe(7);
    expect(resourcesOfType(processor, 'AWS::Logs::LogGroup')).toHaveLength(7);
  });

  it('S2: the graph projector reads only the graph stream and puts only XROOT# rows and contributions', () => {
    const mapping = resourcesOfType(processor, 'AWS::Lambda::EventSourceMapping').find(
      (item) => JSON.stringify(item.EventSourceArn).includes('executiongraphtablestreamarn'),
    );
    expect(mapping).toEqual(expect.objectContaining({
      StartingPosition: 'TRIM_HORIZON',
      BatchSize: 10,
      ParallelizationFactor: 1,
      BisectBatchOnFunctionError: true,
      FunctionResponseTypes: ['ReportBatchItemFailures'],
    }));
    const filters = ((mapping?.FilterCriteria as Resource | undefined)?.Filters ?? []) as Resource[];
    expect(filters).toHaveLength(1);
    // S6 part 2 (design §2.10): node, claim and serving items reach the projector too, or the
    // explanation's stages and the per-node and per-claim measurements are never written.
    expect(JSON.parse(String(filters[0]?.Pattern))).toEqual({
      eventName: ['INSERT', 'MODIFY'],
      dynamodb: { NewImage: { item_kind: { S: ['graph', 'receipt', 'node', 'claim', 'serving_binding'] } } },
    });
    // Control: the stream filter and the rebuild job cover the same kinds, so neither path can
    // drop a kind the other projects.
    const rebuild = fs.readFileSync(
      path.join(__dirname, '..', '..', 'src', 'adapters', 'jobs', 'operational_status', 'graph_projection_rebuild.py'),
      'utf8',
    );
    const scanned = [...rebuild.matchAll(/":\w+": \{"S": "(\w+)"\}/g)].map((match) => match[1]);
    expect(new Set(scanned)).toEqual(new Set(GRAPH_PROJECTED_ITEM_KINDS));
    expect(scanned).toHaveLength(GRAPH_PROJECTED_ITEM_KINDS.length);
    const policies = Object.entries(processor.Resources as Record<string, Resource>).filter(
      ([, item]) => item.Type === 'AWS::IAM::Policy'
        && JSON.stringify(item.Properties).includes('GraphProjectorExecutionRole'),
    );
    expect(policies).toHaveLength(1);
    const statements = (((policies[0]![1].Properties as Resource).PolicyDocument as Resource)
      .Statement as Resource[]);
    const bySid = new Map(statements.map((statement) => [statement.Sid, statement]));
    expect(bySid.get('ReadExecutionGraphStream')?.Action).toEqual([
      'dynamodb:DescribeStream',
      'dynamodb:GetRecords',
      'dynamodb:GetShardIterator',
      'dynamodb:ListStreams',
    ]);
    // S5 (FR-3): plus the receipt row's create-once contribution, acceptance and due marker.
    expect(bySid.get('PutImmutableProjectionRows')).toEqual(expect.objectContaining({
      Action: 'dynamodb:PutItem',
      Condition: {
        'ForAllValues:StringLike': {
          'dynamodb:LeadingKeys': ['XROOT#*', 'MEASUREMENT#*', 'METRIC_ACCEPT#*', 'METRIC_DUE#*'],
        },
      },
    }));
    // No statement names the graph table itself: stream reads only, never a graph item action.
    const rendered = JSON.stringify(statements);
    expect(rendered).not.toContain('executiongraphtablearn');
    expect(rendered).not.toContain('executiongraphtablename');
    for (const action of ['dynamodb:UpdateItem', 'dynamodb:DeleteItem', 'dynamodb:Scan']) {
      expect(rendered).not.toContain(action);
    }
    // Its one SQS grant is the send to its own stream failure queue (asserted exactly below).
    const sqsStatements = statements.filter((statement) => JSON.stringify(statement.Action).includes('sqs:'));
    expect(sqsStatements).toHaveLength(1);
    expect(JSON.stringify(sqsStatements[0]?.Resource)).toContain('GraphProjectorStreamFailureQueue');
  });

  it('S5: the metric reconciler holds only measurement-family actions and GetMetricData', () => {
    const fn = resourcesOfType(processor, 'AWS::Lambda::Function').find(
      (item) => item.Handler === 'adapters.jobs.operational_status.metric_reconciler.handler',
    );
    expect(fn).toEqual(expect.objectContaining({ ReservedConcurrentExecutions: 1, Timeout: 120 }));
    const policies = Object.values(processor.Resources as Record<string, Resource>).filter(
      (item) => item.Type === 'AWS::IAM::Policy'
        && JSON.stringify(item.Properties).includes('MetricReconcilerExecutionRole'),
    );
    expect(policies).toHaveLength(1);
    const statements = (((policies[0]!.Properties as Resource).PolicyDocument as Resource)
      .Statement as Resource[]);
    const families = [
      'MEASUREMENT#*',
      'METRIC_ACCEPT#*',
      'METRIC_DUE#*',
      'AGGREGATE#*',
      'AGGREGATE_MEMBER#*',
      'METRIC_SNAPSHOT#*',
      'SINK_CONFIRMATION#*',
    ];
    const bySid = new Map(statements.map((statement) => [statement.Sid, statement]));
    expect(bySid.get('ReadMeasurementAuthority')).toEqual(expect.objectContaining({
      Action: ['dynamodb:GetItem', 'dynamodb:Query'],
      Condition: { 'ForAllValues:StringLike': { 'dynamodb:LeadingKeys': families } },
    }));
    expect(bySid.get('TransactMeasurementAuthority')).toEqual(expect.objectContaining({
      Action: [
        'dynamodb:PutItem',
        'dynamodb:UpdateItem',
        'dynamodb:DeleteItem',
        'dynamodb:ConditionCheckItem',
      ],
      Condition: {
        'ForAllValues:StringLike': { 'dynamodb:LeadingKeys': families },
        StringEquals: { 'dynamodb:EnclosingOperation': 'TransactWriteItems' },
      },
    }));
    expect(bySid.get('ConfirmFromTheSink')).toEqual(expect.objectContaining({
      Action: 'cloudwatch:GetMetricData',
      Resource: '*',
    }));
    const rendered = JSON.stringify(statements);
    for (const action of ['dynamodb:Scan', 'cloudwatch:PutMetricData', 'sqs:SendMessage']) {
      expect(rendered).not.toContain(action);
    }
    expect(resourcesOfType(processor, 'AWS::Events::Rule')).toContainEqual(
      expect.objectContaining({
        Name: 'adlytics-dev-OperationalStatusMetricReconcilerSchedule',
        ScheduleExpression: 'rate(5 minutes)',
        State: 'ENABLED',
      }),
    );
  });
  it('S5: the status DLQ and graph projector alarms act only on the staffed destination', () => {
    const alarms = resourcesOfType(processor, 'AWS::CloudWatch::Alarm');
    for (const name of [
      'adlytics-dev-operational-status-dlq-not-empty',
      'adlytics-dev-operational-status-graph-projector-stalled',
      'adlytics-dev-operational-status-attempts-exhausted',
      'adlytics-dev-graph-projector-stream-exhausted',
      'adlytics-dev-stage1-publications-stream-exhausted',
    ]) {
      const alarm = alarms.find((item) => item.AlarmName === name);
      expect(alarm?.AlarmActions as unknown[]).toHaveLength(1);
      expect(JSON.stringify(alarm?.AlarmActions)).toContain('ingestionalarmtopicarn');
    }
  });
  it('S5: a reconciler failure, even one the handler absorbs, pages the staffed destination', () => {
    const alarm = resourcesOfType(processor, 'AWS::CloudWatch::Alarm').find(
      (item) => item.AlarmName === 'adlytics-dev-operational-status-metric-reconciler-alarming-errors',
    );
    expect(alarm).toEqual(expect.objectContaining({
      Threshold: 1,
      ComparisonOperator: 'GreaterThanOrEqualToThreshold',
      EvaluationPeriods: 1,
      TreatMissingData: 'notBreaching',
    }));
    expect(alarm?.AlarmActions as unknown[]).toHaveLength(1);
    expect(JSON.stringify(alarm?.AlarmActions)).toContain('ingestionalarmtopicarn');
    const expression = JSON.stringify(alarm?.Metrics);
    expect(expression).toContain("Operation = 'operational_status.metric_reconciler'");
    expect(expression).toContain("ErrorClass != 'domain' AND ErrorClass != 'validation'");
  });
  it('S2: alarms when the graph projector falls behind its stream', () => {
    const alarm = resourcesOfType(processor, 'AWS::CloudWatch::Alarm').find(
      (item) => item.AlarmName === 'adlytics-dev-operational-status-graph-projector-stalled',
    );
    expect(alarm).toEqual(expect.objectContaining({
      Namespace: 'AWS/Lambda',
      MetricName: 'IteratorAge',
      Statistic: 'Maximum',
      Period: 300,
      Threshold: GRAPH_PROJECTOR_STALL_MS,
      ComparisonOperator: 'GreaterThanThreshold',
      EvaluationPeriods: 1,
      TreatMissingData: 'notBreaching',
    }));
    expect(JSON.stringify(alarm?.Dimensions)).toContain('GraphProjector');
    expect(GRAPH_PROJECTOR_STALL_MS).toBeLessThan(24 * 60 * 60 * 1000);
  });

  it('bounds attempt-stream retries and sends an exhausted record to an alarmed failure queue', () => {
    const raw = processor.Resources as Record<
      string,
      { Type: string; Properties: Record<string, unknown> }
    >;
    const ofType = (type: string): [string, Record<string, unknown>][] =>
      Object.entries(raw)
        .filter(([, resource]) => resource.Type === type)
        .map(([id, resource]) => [id, resource.Properties]);
    const failureQueues = ofType('AWS::SQS::Queue').filter(
      ([, properties]) =>
        properties.QueueName === 'adlytics-dev-operational-status-attempts-failures',
    );
    expect(failureQueues).toHaveLength(1);
    const [failuresId, failureQueue] = failureQueues[0] ?? ['', {}];
    const tags = [
      { Key: 'Application', Value: 'adlytics-ai' },
      { Key: 'Environment', Value: 'dev' },
      { Key: 'ManagedBy', Value: 'cdk' },
    ];
    const failuresArn = { 'Fn::GetAtt': [failuresId, 'Arn'] };
    // A standard (not FIFO) queue, SSE, 14-day retention, nothing else.
    expect(failureQueue).toEqual({
      MessageRetentionPeriod: 1209600,
      QueueName: 'adlytics-dev-operational-status-attempts-failures',
      SqsManagedSseEnabled: true,
      Tags: tags,
    });
    const mappings = ofType('AWS::Lambda::EventSourceMapping').filter(([, properties]) =>
      JSON.stringify(properties.EventSourceArn).includes('semanticattemptsstream'),
    );
    expect(mappings).toHaveLength(1);
    const [, mapping] = mappings[0] ?? ['', {}];
    expect(mapping).toEqual({
      BatchSize: 10,
      BisectBatchOnFunctionError: true,
      DestinationConfig: { OnFailure: { Destination: failuresArn } },
      EventSourceArn: {
        Ref: expect.stringMatching(/^SsmParameterValueadlyticsdevdatav2semanticattemptsstreamarn/),
      },
      FunctionName: { Ref: expect.stringMatching(/^AttemptsProducer[0-9A-F]{8}$/) },
      FunctionResponseTypes: ['ReportBatchItemFailures'],
      MaximumRecordAgeInSeconds: TEST_CONFIG.asyncFailureBounds.streamMaxRecordAgeSeconds,
      MaximumRetryAttempts: TEST_CONFIG.asyncFailureBounds.streamMaxRetryAttempts,
      ParallelizationFactor: 1,
      StartingPosition: 'LATEST',
      Tags: tags,
    });
    const alarms = ofType('AWS::CloudWatch::Alarm').filter(
      ([, properties]) =>
        properties.AlarmName === 'adlytics-dev-operational-status-attempts-exhausted',
    );
    expect(alarms).toHaveLength(1);
    const [, alarm] = alarms[0] ?? ['', {}];
    const queueMetric = (metricName: string, stat: string): Record<string, unknown> => ({
      Metric: {
        Dimensions: [{ Name: 'QueueName', Value: { 'Fn::GetAtt': [failuresId, 'QueueName'] } }],
        MetricName: metricName,
        Namespace: 'AWS/SQS',
      },
      Period: 300,
      Stat: stat,
    });
    // Arrivals, not depth: a standing message must not hold the alarm in ALARM.
    expect(ARRIVALS_EXPRESSION).toBe('MAX([FILL(sent, 0), FILL(DIFF(visible), 0)])');
    expect(alarm).toEqual({
      AlarmActions: [
        { Ref: expect.stringMatching(/^SsmParameterValueadlyticsdevingestionalarmtopicarn/) },
      ],
      AlarmDescription:
        'An attempt stream batch exhausted its bounded producer retries; its status events '
        + 'were not emitted. The failure queue holds its shard and sequence range. '
        + 'Disposition: attempts_failures status --consumer attempts, then attempts_failures '
        + 'drain --consumer attempts (operator-tools.md).',
      AlarmName: 'adlytics-dev-operational-status-attempts-exhausted',
      ComparisonOperator: 'GreaterThanThreshold',
      DatapointsToAlarm: 1,
      EvaluationPeriods: 1,
      Metrics: [
        { Expression: ARRIVALS_EXPRESSION, Id: 'expr_1', ReturnData: true },
        { Id: 'sent', MetricStat: queueMetric('NumberOfMessagesSent', 'Sum'), ReturnData: false },
        {
          Id: 'visible',
          MetricStat: queueMetric('ApproximateNumberOfMessagesVisible', 'Maximum'),
          ReturnData: false,
        },
      ],
      Tags: tags,
      Threshold: 0,
      TreatMissingData: 'notBreaching',
    });
    // Only the producer may send to the failure queue: exactly one identity policy names it,
    // with exactly the send grant, and the queue policy only denies insecure transport.
    const naming = ofType('AWS::IAM::Policy').filter(([, properties]) =>
      JSON.stringify(properties.PolicyDocument).includes(failuresId),
    );
    expect(naming).toHaveLength(1);
    const [, senderPolicy] = naming[0] ?? ['', {}];
    expect(senderPolicy.Roles).toEqual([
      { Ref: expect.stringMatching(/^AttemptsProducerExecutionRole[0-9A-F]{8}$/) },
    ]);
    const statements = (senderPolicy.PolicyDocument as { Statement: Record<string, unknown>[] })
      .Statement.filter((statement) => JSON.stringify(statement.Resource).includes(failuresId));
    expect(statements).toEqual([
      {
        Action: ['sqs:SendMessage', 'sqs:GetQueueAttributes', 'sqs:GetQueueUrl'],
        Effect: 'Allow',
        Resource: failuresArn,
      },
    ]);
    const queuePolicies = ofType('AWS::SQS::QueuePolicy').filter(([, properties]) =>
      JSON.stringify(properties.Queues).includes(failuresId),
    );
    expect(queuePolicies).toHaveLength(1);
    expect(queuePolicies[0]?.[1].PolicyDocument).toEqual({
      Statement: [
        {
          Action: 'sqs:*',
          Condition: { Bool: { 'aws:SecureTransport': 'false' } },
          Effect: 'Deny',
          Principal: { AWS: '*' },
          Resource: failuresArn,
        },
      ],
      Version: '2012-10-17',
    });
  });

  it('bounds graph-stream retries and sends an exhausted batch to an alarmed failure queue', () => {
    expectBoundedStreamConsumer(processor, {
      mappingId: /^ExecutionGraphStreamMapping$/,
      queueName: 'adlytics-dev-graph-projector-stream-failures',
      alarmName: 'adlytics-dev-graph-projector-stream-exhausted',
      alarmDescription:
        'A graph-stream batch exhausted its bounded projector retries; its receipts were not '
        + 'projected. Disposition: attempts_failures status --consumer graph-projector, then '
        + 'attempts_failures drain --consumer graph-projector, then replay with '
        + 'graph_projection_rebuild (operator-tools.md).',
      roleId: /^GraphProjectorExecutionRole[0-9A-F]{8}$/,
      mapping: {
        BatchSize: 10,
        EventSourceArn: {
          Ref: expect.stringMatching(/^SsmParameterValueadlyticsdev.*executiongraphtablestreamarn/),
        },
        FilterCriteria: {
          Filters: [
            {
              Pattern: JSON.stringify({
                eventName: ['INSERT', 'MODIFY'],
                dynamodb: { NewImage: { item_kind: { S: ['graph', 'receipt', 'node', 'claim', 'serving_binding'] } } },
              }),
            },
          ],
        },
        FunctionName: { Ref: expect.stringMatching(/^GraphProjector[0-9A-F]{8}$/) },
        FunctionResponseTypes: ['ReportBatchItemFailures'],
        ParallelizationFactor: 1,
        StartingPosition: 'TRIM_HORIZON',
      },
    });
  });

  it('bounds Stage-1 publications-stream retries and sends an exhausted batch to an alarmed failure queue', () => {
    expectBoundedStreamConsumer(processor, {
      mappingId: /^Stage1Stage1PublicationsStreamMapping[0-9A-F]{8}$/,
      queueName: 'adlytics-dev-stage1-publications-stream-failures',
      alarmName: 'adlytics-dev-stage1-publications-stream-exhausted',
      alarmDescription:
        'A publications-stream batch exhausted its bounded Stage-1 lifecycle producer retries; '
        + 'its outbox items stay pending for the one-minute recovery schedule. Disposition: '
        + 'attempts_failures status --consumer stage1-publications, then attempts_failures '
        + 'drain --consumer stage1-publications (operator-tools.md).',
      roleId: /^Stage1Stage1LifecycleProducerExecutionRole[0-9A-F]{8}$/,
      mapping: {
        BatchSize: 10,
        Enabled: false,
        EventSourceArn: {
          Ref: expect.stringMatching(/^SsmParameterValueadlyticsdevdatav2semanticpublicationsstreamarn/),
        },
        FilterCriteria: {
          Filters: [
            {
              Pattern: JSON.stringify({
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
        FunctionName: { Ref: expect.stringMatching(/^Stage1Stage1LifecycleProducer[0-9A-F]{8}$/) },
        FunctionResponseTypes: ['ReportBatchItemFailures'],
        ParallelizationFactor: 1,
        StartingPosition: 'LATEST',
      },
    });
  });

  it('alarms on each arrival at the status DLQ, not on its standing depth', () => {
    expectArrivalAlarm(processor, {
      alarmName: 'adlytics-dev-operational-status-dlq-not-empty',
      queueName: operationalStatusQueueName('dev', true),
      staffed: { arm: 'published' },
    });
  });

  it('maps attempts and FIFO records with partial failures and schedules reconciliation', () => {
    const mappings = resourcesOfType(processor, 'AWS::Lambda::EventSourceMapping');
    expect(mappings).toHaveLength(4);
    for (const mapping of mappings) {
      expect(mapping.FunctionResponseTypes).toEqual(['ReportBatchItemFailures']);
    }
    expect(mappings).toContainEqual(expect.objectContaining({
      StartingPosition: 'LATEST',
      BatchSize: 10,
      ParallelizationFactor: 1,
    }));
    expect(resourcesOfType(processor, 'AWS::Events::Rule')).toContainEqual(
      expect.objectContaining({ ScheduleExpression: 'rate(1 minute)', State: 'ENABLED' }),
    );
    expect(resourcesOfType(processor, 'AWS::Events::EventBus')).toHaveLength(0);
    expect(resourcesOfType(processor, 'AWS::Events::EventBusPolicy')).toHaveLength(0);
  });

  it('keeps Stage-1 lifecycle delivery disabled and filtered in the pre-switch state', () => {
    const lifecycleMapping = resourcesOfType(processor, 'AWS::Lambda::EventSourceMapping').find(
      (mapping) => JSON.stringify(mapping.EventSourceArn).includes('semanticpublicationsstream'),
    );
    expect(lifecycleMapping).toEqual(expect.objectContaining({
      Enabled: false,
      StartingPosition: 'LATEST',
      BatchSize: 10,
      ParallelizationFactor: 1,
      FunctionResponseTypes: ['ReportBatchItemFailures'],
    }));
    const filters = ((lifecycleMapping?.FilterCriteria as Resource | undefined)?.Filters ?? []) as Resource[];
    expect(filters).toHaveLength(1);
    const pattern = JSON.parse(String(filters[0]?.Pattern)) as Resource;
    expect(pattern).toEqual({
      eventName: ['INSERT', 'MODIFY'],
      dynamodb: {
        NewImage: {
          outbox_status: { S: ['pending'] },
          record_key: { S: [{ prefix: 'EVENT#' }, { prefix: 'USAGE#' }] },
        },
      },
    });
    const disabledSchedules = resourcesOfType(processor, 'AWS::Events::Rule').filter(
      (rule) => rule.ScheduleExpression === 'rate(1 minute)' && rule.State === 'DISABLED',
    );
    expect(disabledSchedules).toHaveLength(1);
  });

  it('publishes queue contracts and grants no scan or broad service access', () => {
    const parameters = resourcesOfType(processor, 'AWS::SSM::Parameter');
    expect(parameters).toHaveLength(4);
    expect(parameters).toEqual(expect.arrayContaining([
      expect.objectContaining({ Name: paramPath('dev', 'operationalStatusQueueUrl') }),
      expect.objectContaining({ Name: paramPath('dev', 'operationalStatusQueueArn') }),
      expect.objectContaining({ Name: paramPath('dev', 'stage1OutboxRecoveryOperatorRoleArn') }),
      expect.objectContaining({ Name: paramPath('dev', 'stage1OutboxRecoveryFunctionArn') }),
    ]));
    const rendered = JSON.stringify(processor);
    expect(rendered).toContain('dynamodb:GetItem');
    expect(rendered).toContain('dynamodb:ConditionCheckItem');
    expect(rendered).toContain('sqs:SendMessage');
    expect(rendered).not.toContain('dynamodb:Scan');
    const allowStatements = resourcesOfType(processor, 'AWS::IAM::Policy')
      .flatMap((policy) =>
        ((policy.PolicyDocument as { Statement?: Resource[] } | undefined)?.Statement ?? []),
      )
      .filter((statement) => statement.Effect === 'Allow');
    expect(JSON.stringify(allowStatements)).not.toContain('sqs:*');
    expect(resourcesOfType(processor, 'AWS::DynamoDB::Table')).toHaveLength(0);
  });

  it('gives the projector the retained publications table it re-attests Stage-1 authority from', () => {
    // The projector resolves `operational_projector_settings()` at import; without this variable
    // every status message (Stage 0 included) failed at module import after the Stage-1 code
    // landed, and the whole status pipeline stalled in flight until the variable was rendered.
    const projector = resourcesOfType(processor, 'AWS::Lambda::Function').find(
      (fn) => fn.Handler === 'adapters.jobs.operational_status.projector.handler',
    );
    const variables = ((projector?.Environment as Resource | undefined)?.Variables ?? {}) as Resource;
    expect(variables.ADL_OPERATIONAL_STATUS_TABLE).toBeDefined();
    expect(variables.ADL_SEMANTIC_PUBLICATIONS_TABLE).toBeDefined();
    const roleId = (projector?.Role as Resource | undefined)?.['Fn::GetAtt'] as string[] | undefined;
    const statements = resourcesOfType(processor, 'AWS::IAM::Policy')
      .filter((policy) => JSON.stringify(policy.Roles).includes(`"Ref":"${roleId?.[0]}"`))
      .flatMap((policy) =>
        ((policy.PolicyDocument as { Statement?: Resource[] } | undefined)?.Statement ?? []),
      );
    const reattest = statements.find((statement) => statement.Sid === 'ReattestStage1LifecycleAuthority');
    expect(reattest?.Action).toEqual('dynamodb:GetItem');
    expect((reattest?.Condition as Resource)['ForAllValues:StringLike']).toEqual({
      'dynamodb:LeadingKeys': ['STATUS_FLOW#*'],
    });
  });

  it('lets the lifecycle producer read and mark every partition that can hold an outbox authority', () => {
    // The first live delivery after the recovery flip parked every lifecycle EVENT# with
    // AccessDenied: the producer could read TABLE_ATTEMPT# usage authorities but not the
    // STATUS_FLOW# partitions the plan, unit, and inline-run lifecycle events live under.
    const allowStatements = resourcesOfType(processor, 'AWS::IAM::Policy')
      .flatMap((policy) =>
        ((policy.PolicyDocument as { Statement?: Resource[] } | undefined)?.Statement ?? []),
      )
      .filter((statement) => statement.Effect === 'Allow');
    // The publications table ARN is built from the SSM-resolved table name, so the rendered
    // Resource is a Join over that parameter reference rather than a literal name.
    // Only the producer's own grants: the projector's narrower STATUS_FLOW#-only re-attest read
    // is asserted separately above.
    const publicationReads = allowStatements.filter((statement) => {
      const actions = Array.isArray(statement.Action) ? statement.Action : [statement.Action];
      const resource = JSON.stringify(statement.Resource);
      return (
        actions.includes('dynamodb:GetItem')
        && resource.includes('semanticpublicationstable')
        && !resource.includes('/index/')
        && statement.Sid !== 'ReattestStage1LifecycleAuthority'
      );
    });
    expect(publicationReads.length).toBeGreaterThan(0);
    for (const statement of publicationReads) {
      const keys = (statement.Condition as Resource)['ForAllValues:StringLike'] as Resource;
      expect(keys['dynamodb:LeadingKeys']).toEqual(
        expect.arrayContaining(['WRITE_PLAN#*', 'TABLE_ATTEMPT#*', 'STATUS_FLOW#*', 'fct_retail_sales|*']),
      );
    }
  });
});
