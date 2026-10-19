// SPDX-License-Identifier: MIT-0
/**
 * The exact rendering of one `boundedStreamMapping` consumer, asserted per stack.
 *
 * Not named `*.test.ts`, so jest does not collect it as a suite.
 */
import { paramPath } from '../../lib/config';
import { ARRIVALS_EXPRESSION } from '../../lib/constructs/observability/async-failure';
import { TEST_CONFIG } from './config';

type Properties = Record<string, unknown>;
type Raw = Record<string, { Type: string; Properties: Properties }>;

/** The arm of the staffed switch an alarm's stack resolves to, and so its exact action. */
export type StaffedDestination =
  /** The ARN IngestionStack publishes at SSM `ingestionAlarmTopicArn` (the email arm). */
  | { readonly arm: 'published' }
  /** IngestionStack's own topic, by its physical name (the email arm, in that stack). */
  | { readonly arm: 'inStackTopic'; readonly topicName: string }
  /** The configured topic arm's exact ARN. */
  | { readonly arm: 'configuredTopic'; readonly topicArn: string };

/** The one rendered alarm action `destination` resolves to in `template`. */
export function staffedActionIn(template: Properties, destination: StaffedDestination): unknown {
  if (destination.arm === 'configuredTopic') return destination.topicArn;
  if (destination.arm === 'inStackTopic') {
    const topics = ofType(template, 'AWS::SNS::Topic').filter(
      ([, properties]) => properties.TopicName === destination.topicName,
    );
    expect(topics).toHaveLength(1);
    return { Ref: topics[0]?.[0] };
  }
  const parameters = Object.entries(
    (template.Parameters ?? {}) as Record<string, { Type?: unknown; Default?: unknown }>,
  ).filter(([, parameter]) =>
    parameter.Type === 'AWS::SSM::Parameter::Value<String>'
    && parameter.Default === paramPath(TEST_CONFIG.envName, 'ingestionAlarmTopicArn'),
  );
  expect(parameters).toHaveLength(1);
  return { Ref: parameters[0]?.[0] };
}

const TAGS = [
  { Key: 'Application', Value: 'adlytics-ai' },
  { Key: 'Environment', Value: 'dev' },
  { Key: 'ManagedBy', Value: 'cdk' },
];

export interface BoundedStreamConsumer {
  /** The mapping's logical ID prefix (the id it is created with, plus its scope's). */
  readonly mappingId: RegExp;
  readonly queueName: string;
  readonly alarmName: string;
  readonly alarmDescription: string;
  /** The function role's logical ID. */
  readonly roleId: RegExp;
  /** Every mapping property beyond the four bounds and the tags. */
  readonly mapping: Properties;
}

function ofType(template: Properties, type: string): [string, Properties][] {
  return Object.entries(template.Resources as Raw)
    .filter(([, resource]) => resource.Type === type)
    .map(([id, resource]) => [id, resource.Properties]);
}

/**
 * Plan 6.2 (DB26): the names of every alarm whose metrics read the queue named `queueName`, so a
 * test can prove a dead-letter queue has exactly one alarm (its arrival alarm), never a duplicate.
 */
export function alarmsOnQueue(template: Properties, queueName: string): string[] {
  const queues = ofType(template, 'AWS::SQS::Queue').filter(
    ([, properties]) => properties.QueueName === queueName,
  );
  expect(queues).toHaveLength(1);
  const reference = JSON.stringify({ 'Fn::GetAtt': [queues[0]?.[0], 'QueueName'] });
  return ofType(template, 'AWS::CloudWatch::Alarm')
    .filter(([, properties]) => JSON.stringify(properties).includes(reference))
    .map(([, properties]) => String(properties.AlarmName));
}

/**
 * The failure queue, the bounded mapping, the arrival alarm on the staffed destination, the one
 * send grant on the function role, and the queue policy, each exactly.
 */
export function expectBoundedStreamConsumer(
  template: Properties,
  consumer: BoundedStreamConsumer,
): void {
  const queues = ofType(template, 'AWS::SQS::Queue').filter(
    ([, properties]) => properties.QueueName === consumer.queueName,
  );
  expect(queues).toHaveLength(1);
  const [queueId, queue] = queues[0] ?? ['', {}];
  const queueArn = { 'Fn::GetAtt': [queueId, 'Arn'] };
  // A standard (not FIFO) queue, SSE, 14-day retention, nothing else.
  expect(queue).toEqual({
    MessageRetentionPeriod: 1209600,
    QueueName: consumer.queueName,
    SqsManagedSseEnabled: true,
    Tags: TAGS,
  });

  const mappings = ofType(template, 'AWS::Lambda::EventSourceMapping').filter(([id]) =>
    consumer.mappingId.test(id),
  );
  expect(mappings).toHaveLength(1);
  expect(mappings[0]?.[1]).toEqual({
    ...consumer.mapping,
    BisectBatchOnFunctionError: true,
    DestinationConfig: { OnFailure: { Destination: queueArn } },
    MaximumRecordAgeInSeconds: TEST_CONFIG.asyncFailureBounds.streamMaxRecordAgeSeconds,
    MaximumRetryAttempts: TEST_CONFIG.asyncFailureBounds.streamMaxRetryAttempts,
    Tags: TAGS,
  });

  const alarms = ofType(template, 'AWS::CloudWatch::Alarm').filter(
    ([, properties]) => properties.AlarmName === consumer.alarmName,
  );
  expect(alarms).toHaveLength(1);
  const queueMetric = (metricName: string, stat: string): Properties => ({
    Metric: {
      Dimensions: [{ Name: 'QueueName', Value: { 'Fn::GetAtt': [queueId, 'QueueName'] } }],
      MetricName: metricName,
      Namespace: 'AWS/SQS',
    },
    Period: 300,
    Stat: stat,
  });
  expect(alarms[0]?.[1]).toEqual({
    AlarmActions: [staffedActionIn(template, { arm: 'published' })],
    AlarmDescription: consumer.alarmDescription,
    AlarmName: consumer.alarmName,
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
    Tags: TAGS,
    Threshold: 0,
    TreatMissingData: 'notBreaching',
  });

  // Only the consumer may send to its failure queue: exactly one identity policy names it, on
  // the consumer's role, with exactly the send grant.
  const naming = ofType(template, 'AWS::IAM::Policy').filter(([, properties]) =>
    JSON.stringify(properties.PolicyDocument).includes(`"${queueId}"`),
  );
  expect(naming).toHaveLength(1);
  const [, policy] = naming[0] ?? ['', {}];
  expect(policy.Roles).toEqual([{ Ref: expect.stringMatching(consumer.roleId) }]);
  const statements = (policy.PolicyDocument as { Statement: Properties[] }).Statement.filter(
    (statement) => JSON.stringify(statement.Resource).includes(`"${queueId}"`),
  );
  expect(statements).toEqual([
    {
      Action: ['sqs:SendMessage', 'sqs:GetQueueAttributes', 'sqs:GetQueueUrl'],
      Effect: 'Allow',
      Resource: queueArn,
    },
  ]);
  const queuePolicies = ofType(template, 'AWS::SQS::QueuePolicy').filter(([, properties]) =>
    JSON.stringify(properties.Queues).includes(`"${queueId}"`),
  );
  expect(queuePolicies).toHaveLength(1);
  expect(queuePolicies[0]?.[1].PolicyDocument).toEqual({
    Statement: [
      {
        Action: 'sqs:*',
        Condition: { Bool: { 'aws:SecureTransport': 'false' } },
        Effect: 'Deny',
        Principal: { AWS: '*' },
        Resource: queueArn,
      },
    ],
    Version: '2012-10-17',
  });
}

/** The exact arrival alarm on a dead-letter queue, acting on the staffed destination. */
export function expectArrivalAlarm(
  template: Properties,
  alarm: {
    readonly alarmName: string;
    readonly queueName: string;
    readonly staffed: StaffedDestination;
  },
): void {
  const queues = ofType(template, 'AWS::SQS::Queue').filter(
    ([, properties]) => properties.QueueName === alarm.queueName,
  );
  expect(queues).toHaveLength(1);
  const [queueId] = queues[0] ?? [''];
  const found = ofType(template, 'AWS::CloudWatch::Alarm').filter(
    ([, properties]) => properties.AlarmName === alarm.alarmName,
  );
  expect(found).toHaveLength(1);
  const queueMetric = (metricName: string, stat: string): Properties => ({
    Metric: {
      Dimensions: [{ Name: 'QueueName', Value: { 'Fn::GetAtt': [queueId, 'QueueName'] } }],
      MetricName: metricName,
      Namespace: 'AWS/SQS',
    },
    Period: 300,
    Stat: stat,
  });
  expect(found[0]?.[1]).toEqual(expect.objectContaining({
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
    Threshold: 0,
    TreatMissingData: 'notBreaching',
  }));
  const rendered = found[0]?.[1] ?? {};
  expect(rendered).not.toHaveProperty('MetricName');
  // Exactly the staffed destination this stack resolves to, and nothing else.
  expect(rendered.AlarmActions).toStrictEqual([staffedActionIn(template, alarm.staffed)]);
}
