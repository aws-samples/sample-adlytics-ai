// SPDX-License-Identifier: MIT-0
/**
 * T1 (ADR-133 clauses 1–3): synth fails on any async consumer that can fail silently.
 *
 * Each positive control is a fixture stack that breaks exactly one rule and must fail synth
 * with that rule's message and the construct path; the compliant fixture and the whole app must
 * synthesize.
 */
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as sqs from 'aws-cdk-lib/aws-sqs';

import {
  ARRIVALS_EXPRESSION,
  arrivalAlarm,
  boundedStreamMapping,
  staffedAlarmAction,
} from '../lib/constructs/observability/async-failure';
import * as actions from 'aws-cdk-lib/aws-cloudwatch-actions';
import * as sns from 'aws-cdk-lib/aws-sns';

import type { AppConfig } from '../lib/config';
import { AsyncFailureBoundsAspect } from '../lib/mixins/async-failure-bounds';
import { expectArrivalAlarm, type StaffedDestination } from './support/async-failure';
import { TEST_CONFIG, configWith } from './support/config';
import { everyTemplate } from './support/templates';

const STREAM_ARN = 'arn:aws:dynamodb:eu-west-1:111122223333:table/fixture/stream/2026-10-09T00:00:00.000';

interface Fixture {
  readonly stack: cdk.Stack;
  readonly fn: lambda.Function;
  readonly action: cloudwatch.IAlarmAction;
}

function fixture(config: AppConfig = TEST_CONFIG): Fixture {
  const app = new cdk.App();
  cdk.Aspects.of(app).add(new AsyncFailureBoundsAspect());
  const stack = new cdk.Stack(app, 'Fixture', {
    env: { account: TEST_CONFIG.account, region: TEST_CONFIG.region },
  });
  const fn = new lambda.Function(stack, 'Consumer', {
    runtime: lambda.Runtime.PYTHON_3_13,
    handler: 'index.handler',
    code: lambda.Code.fromInline('def handler(event, context):\n    return {}\n'),
  });
  return { stack, fn, action: staffedAlarmAction(stack, config) };
}

/** A failure queue with its arrival alarm, as `boundedStreamMapping` builds it. */
function alarmedQueue(f: Fixture, id: string): sqs.Queue {
  const queue = new sqs.Queue(f.stack, id, { enforceSSL: true });
  arrivalAlarm(f.stack, `${id}Alarm`, {
    alarmName: `fixture-${id}`,
    alarmDescription: 'fixture',
    queue,
    action: f.action,
  });
  return queue;
}

/** A stream mapping with every bound, then exactly the given properties replaced or removed. */
function streamMapping(f: Fixture, overrides: Readonly<Record<string, unknown>>): void {
  const failures = alarmedQueue(f, 'Failures');
  const props: Record<string, unknown> = {
    functionName: f.fn.functionName,
    eventSourceArn: STREAM_ARN,
    startingPosition: 'LATEST',
    maximumRetryAttempts: 100,
    maximumRecordAgeInSeconds: 3600,
    bisectBatchOnFunctionError: true,
    destinationConfig: { onFailure: { destination: failures.queueArn } },
  };
  for (const [key, value] of Object.entries(overrides)) {
    if (value === undefined) delete props[key];
    else props[key] = value;
  }
  new lambda.CfnEventSourceMapping(
    f.stack,
    'Stream',
    props as unknown as lambda.CfnEventSourceMappingProps,
  );
}

function synthErrors(f: Fixture): string {
  try {
    Template.fromStack(f.stack);
  } catch (error) {
    return String(error);
  }
  return '';
}

/** Each validation message the synth error carries for the fixture stack, in order. */
function messages(errors: string): string[] {
  return errors.split('\n').flatMap((line) => {
    const start = line.indexOf('Fixture/');
    return start < 0 ? [] : [line.slice(start)];
  });
}

/** The arrival-alarm properties a mutation fixture may change from `arrivalAlarm`'s shape. */
interface ArrivalShape {
  readonly sentStatistic?: string;
  readonly visibleStatistic?: string;
  readonly period?: cdk.Duration;
  readonly threshold?: number;
  readonly comparisonOperator?: cloudwatch.ComparisonOperator;
  readonly evaluationPeriods?: number;
  readonly datapointsToAlarm?: number;
  readonly treatMissingData?: cloudwatch.TreatMissingData;
  readonly actionsEnabled?: boolean;
  /** Leave the alarm with no action at all. */
  readonly withoutAction?: boolean;
}

/** A failure queue a compliant stream mapping sends to, with no alarm yet. */
function failureQueueFor(f: Fixture): sqs.Queue {
  const failures = new sqs.Queue(f.stack, 'Failures');
  new lambda.CfnEventSourceMapping(f.stack, 'Stream', {
    functionName: f.fn.functionName,
    eventSourceArn: STREAM_ARN,
    startingPosition: 'LATEST',
    maximumRetryAttempts: 100,
    maximumRecordAgeInSeconds: 3600,
    bisectBatchOnFunctionError: true,
    destinationConfig: { onFailure: { destination: failures.queueArn } },
  });
  return failures;
}

/** `arrivalAlarm`'s exact shape on `queue`, with only `shape`'s properties changed. */
function arrivalShapedAlarm(f: Fixture, queue: sqs.Queue, shape: ArrivalShape): void {
  const alarm = new cloudwatch.Alarm(f.stack, 'Arrivals', {
    metric: new cloudwatch.MathExpression({
      expression: ARRIVALS_EXPRESSION,
      usingMetrics: {
        sent: queue.metricNumberOfMessagesSent({ statistic: shape.sentStatistic ?? 'Sum' }),
        visible: queue.metricApproximateNumberOfMessagesVisible({
          statistic: shape.visibleStatistic ?? 'Maximum',
        }),
      },
      period: shape.period ?? cdk.Duration.minutes(5),
    }),
    threshold: shape.threshold ?? 0,
    comparisonOperator: shape.comparisonOperator
      ?? cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
    evaluationPeriods: shape.evaluationPeriods ?? 1,
    datapointsToAlarm: shape.datapointsToAlarm ?? 1,
    treatMissingData: shape.treatMissingData ?? cloudwatch.TreatMissingData.NOT_BREACHING,
    actionsEnabled: shape.actionsEnabled ?? true,
  });
  if (shape.withoutAction !== true) alarm.addAlarmAction(f.action);
}

/**
 * `arrivalAlarm`'s rendered shape on `queue` as a raw `CfnAlarm`, with the expression's
 * `ReturnData` set to `expressionReturnData`, or omitted when that is undefined.
 */
function rawArrivalAlarm(f: Fixture, queue: sqs.Queue, expressionReturnData: boolean | undefined): void {
  const queueMetric = (metricName: string, stat: string): cloudwatch.CfnAlarm.MetricStatProperty => ({
    metric: {
      namespace: 'AWS/SQS',
      metricName,
      dimensions: [{ name: 'QueueName', value: queue.queueName }],
    },
    period: 300,
    stat,
  });
  new cloudwatch.CfnAlarm(f.stack, 'Arrivals', {
    metrics: [
      {
        id: 'expr_1',
        expression: ARRIVALS_EXPRESSION,
        ...(expressionReturnData === undefined ? {} : { returnData: expressionReturnData }),
      },
      { id: 'sent', metricStat: queueMetric('NumberOfMessagesSent', 'Sum'), returnData: false },
      {
        id: 'visible',
        metricStat: queueMetric('ApproximateNumberOfMessagesVisible', 'Maximum'),
        returnData: false,
      },
    ],
    threshold: 0,
    comparisonOperator: 'GreaterThanThreshold',
    evaluationPeriods: 1,
    datapointsToAlarm: 1,
    treatMissingData: 'notBreaching',
    alarmActions: ['arn:aws:sns:eu-west-1:111122223333:fixture-staffed'],
  });
}

describe('AsyncFailureBoundsAspect', () => {
  it('passes a compliant stream consumer and SQS consumer', () => {
    const f = fixture();
    boundedStreamMapping(f.stack, 'Stream', {
      fn: f.fn,
      eventSourceArn: STREAM_ARN,
      startingPosition: 'LATEST',
      batchSize: 10,
      parallelizationFactor: 1,
      bounds: TEST_CONFIG.asyncFailureBounds,
      failureQueue: { id: 'Failures', queueName: 'fixture-failures' },
      alarm: { id: 'FailuresAlarm', alarmName: 'fixture-exhausted', alarmDescription: 'fixture', action: f.action },
    });
    const dlq = alarmedQueue(f, 'DeadLetters');
    const work = new sqs.Queue(f.stack, 'Work', { deadLetterQueue: { queue: dlq, maxReceiveCount: 3 } });
    new lambda.CfnEventSourceMapping(f.stack, 'Sqs', {
      functionName: f.fn.functionName,
      eventSourceArn: work.queueArn,
    });
    expect(synthErrors(f)).toBe('');
  });

  it.each([
    ['an unbounded retry count (-1)', { maximumRetryAttempts: -1 }, 'stream mapping sets no maximumRetryAttempts bound'],
    ['no retry count', { maximumRetryAttempts: undefined }, 'stream mapping sets no maximumRetryAttempts bound'],
    ['an unbounded record age (-1)', { maximumRecordAgeInSeconds: -1 }, 'stream mapping sets no maximumRecordAgeInSeconds bound'],
    ['no record age', { maximumRecordAgeInSeconds: undefined }, 'stream mapping sets no maximumRecordAgeInSeconds bound'],
    ['no bisect', { bisectBatchOnFunctionError: undefined }, 'stream mapping does not set bisectBatchOnFunctionError'],
    ['no on-failure destination', { destinationConfig: undefined }, 'stream mapping has no destinationConfig.onFailure'],
    [
      'an on-failure destination that is not an in-stack queue',
      { destinationConfig: { onFailure: { destination: 'arn:aws:sqs:eu-west-1:111122223333:elsewhere' } } },
      "stream mapping's on-failure destination is not an in-stack queue",
    ],
  ] as [string, Record<string, unknown>, string][])(
    'refuses a stream mapping with %s, paired with the compliant mapping',
    (_name, overrides, message) => {
      const compliant = fixture();
      streamMapping(compliant, {});
      expect(synthErrors(compliant)).toBe('');

      const f = fixture();
      streamMapping(f, overrides);
      expect(messages(synthErrors(f))).toStrictEqual([`Fixture/Stream: ${message}`]);
    },
  );

  it('refuses an SQS consumer queue with no redrive, paired with a redriven queue', () => {
    const compliant = fixture();
    const dlq = alarmedQueue(compliant, 'DeadLetters');
    const redriven = new sqs.Queue(compliant.stack, 'Work', {
      deadLetterQueue: { queue: dlq, maxReceiveCount: 3 },
    });
    new lambda.CfnEventSourceMapping(compliant.stack, 'Sqs', {
      functionName: compliant.fn.functionName,
      eventSourceArn: redriven.queueArn,
    });
    expect(synthErrors(compliant)).toBe('');

    const f = fixture();
    const work = new sqs.Queue(f.stack, 'Work');
    new lambda.CfnEventSourceMapping(f.stack, 'Sqs', {
      functionName: f.fn.functionName,
      eventSourceArn: work.queueArn,
    });
    expect(messages(synthErrors(f))).toStrictEqual([
      'Fixture/Sqs: SQS consumer queue Fixture/Work/Resource has no redrive dead-letter queue',
    ]);
  });

  it.each([
    ['a literal ARN', (): string => 'arn:aws:sqs:eu-west-1:111122223333:elsewhere'],
    ["another stack's queue", (f: Fixture): string => {
      const other = new cdk.Stack(f.stack.node.root as cdk.App, 'Other', {
        env: { account: TEST_CONFIG.account, region: TEST_CONFIG.region },
      });
      return new sqs.Queue(other, 'DeadLetters').queueArn;
    }],
  ] as [string, (f: Fixture) => string][])(
    'refuses a redrive dead-letter target that is %s, paired with an in-stack target',
    (_name, target) => {
      const compliant = fixture();
      const dlq = alarmedQueue(compliant, 'DeadLetters');
      const ok = new sqs.Queue(compliant.stack, 'Work', { deadLetterQueue: { queue: dlq, maxReceiveCount: 3 } });
      new lambda.CfnEventSourceMapping(compliant.stack, 'Sqs', {
        functionName: compliant.fn.functionName,
        eventSourceArn: ok.queueArn,
      });
      expect(synthErrors(compliant)).toBe('');

      const f = fixture();
      const work = new sqs.CfnQueue(f.stack, 'Work', {
        redrivePolicy: { deadLetterTargetArn: target(f), maxReceiveCount: 3 },
      });
      new lambda.CfnEventSourceMapping(f.stack, 'Sqs', {
        functionName: f.fn.functionName,
        eventSourceArn: work.attrArn,
      });
      const errors = synthErrors(f);
      expect(messages(errors)).toStrictEqual([
        'Fixture/Work: redrive dead-letter target is not an in-stack queue',
      ]);
    },
  );

  it.each([
    ['a Minimum sent statistic', { sentStatistic: 'Minimum' }, 'sent statistic is Minimum, not Sum'],
    ['an Average visible statistic', { visibleStatistic: 'Average' }, 'visible statistic is Average, not Maximum'],
    ['a 1-minute period', { period: cdk.Duration.minutes(1) }, 'sent period is 60, not 300; visible period is 60, not 300'],
    ['a threshold of 1000', { threshold: 1000 }, 'threshold is 1000, not 0'],
    [
      'a less-than comparison',
      { comparisonOperator: cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD },
      'comparisonOperator is LessThanThreshold, not GreaterThanThreshold',
    ],
    ['three evaluation periods', { evaluationPeriods: 3, datapointsToAlarm: 1 }, 'evaluationPeriods is 3, not 1'],
    ['two datapoints to alarm', { evaluationPeriods: 1, datapointsToAlarm: 2 }, 'datapointsToAlarm is 2, not 1'],
    [
      'missing data breaching',
      { treatMissingData: cloudwatch.TreatMissingData.BREACHING },
      'treatMissingData is breaching, not notBreaching',
    ],
    ['actions disabled', { actionsEnabled: false }, 'actionsEnabled is false, not true'],
    ['no action', { withoutAction: true }, 'it has no alarm action'],
  ] as [string, ArrivalShape, string][])('refuses an arrival alarm with %s', (_name, shape, departures) => {
    const compliant = fixture();
    arrivalShapedAlarm(compliant, failureQueueFor(compliant), {});
    expect(synthErrors(compliant)).toBe('');

    const f = fixture();
    arrivalShapedAlarm(f, failureQueueFor(f), shape);
    expect(messages(synthErrors(f))).toStrictEqual([
      'Fixture/Failures/Resource: failure queue (from Fixture/Stream) has no arrival alarm with an action',
      'Fixture/Arrivals/Resource: arrival alarm on Fixture/Failures/Resource departs from the '
        + `arrival shape: ${departures}`,
    ]);
  });

  it.each([
    ['omitted', undefined, 'undefined'],
    ['false', false, 'false'],
  ] as [string, boolean | undefined, string][])(
    'refuses an arrival alarm whose expression ReturnData is %s, paired with ReturnData true',
    (_name, returnData, rendered) => {
      const compliant = fixture();
      rawArrivalAlarm(compliant, failureQueueFor(compliant), true);
      expect(synthErrors(compliant)).toBe('');

      const f = fixture();
      rawArrivalAlarm(f, failureQueueFor(f), returnData);
      expect(messages(synthErrors(f))).toStrictEqual([
        'Fixture/Failures/Resource: failure queue (from Fixture/Stream) has no arrival alarm with an action',
        'Fixture/Arrivals: arrival alarm on Fixture/Failures/Resource departs from the arrival shape: '
          + `arrival expression returnData is ${rendered}, not true`,
      ]);
    },
  );

  it('refuses a dead-letter queue with only a standing-depth alarm, paired with an arrival alarm', () => {
    const compliant = fixture();
    const compliantDlq = new sqs.Queue(compliant.stack, 'DeadLetters');
    new sqs.Queue(compliant.stack, 'Work', { deadLetterQueue: { queue: compliantDlq, maxReceiveCount: 3 } });
    arrivalShapedAlarm(compliant, compliantDlq, {});
    expect(synthErrors(compliant)).toBe('');

    const f = fixture();
    const dlq = new sqs.Queue(f.stack, 'DeadLetters');
    new sqs.Queue(f.stack, 'Work', { deadLetterQueue: { queue: dlq, maxReceiveCount: 3 } });
    new cloudwatch.Alarm(f.stack, 'Depth', {
      metric: dlq.metricApproximateNumberOfMessagesVisible(),
      threshold: 0,
      evaluationPeriods: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
    }).addAlarmAction(f.action);
    expect(messages(synthErrors(f))).toStrictEqual([
      'Fixture/DeadLetters/Resource: failure queue (from Fixture/Work/Resource) has no arrival alarm with an action',
    ]);
  });

  it('refuses an arrival alarm that watches a different queue, paired with one on the failure queue', () => {
    const compliant = fixture();
    arrivalShapedAlarm(compliant, failureQueueFor(compliant), {});
    expect(synthErrors(compliant)).toBe('');

    const f = fixture();
    failureQueueFor(f);
    arrivalShapedAlarm(f, new sqs.Queue(f.stack, 'Other'), {});
    expect(messages(synthErrors(f))).toStrictEqual([
      'Fixture/Failures/Resource: failure queue (from Fixture/Stream) has no arrival alarm with an action',
    ]);
  });

  it('passes every stack the app builds', () => {
    expect(() => everyTemplate()).not.toThrow();
  });
});

describe('expectArrivalAlarm proves the staffed destination', () => {
  const CONFIGURED = 'arn:aws:sns:eu-west-1:111122223333:staffed-fixture';
  const ELSEWHERE = 'arn:aws:sns:eu-west-1:111122223333:elsewhere';
  const ALARM = { alarmName: 'fixture-dead-lettered', queueName: 'fixture-dlq' };

  /** A redriven queue whose named DLQ's arrival alarm acts on `action(f)`. */
  function dlqTemplate(
    config: AppConfig,
    action: (f: Fixture) => cloudwatch.IAlarmAction,
  ): Record<string, unknown> {
    const f = fixture(config);
    const dlq = new sqs.Queue(f.stack, 'DeadLetters', { queueName: ALARM.queueName });
    new sqs.Queue(f.stack, 'Work', { deadLetterQueue: { queue: dlq, maxReceiveCount: 3 } });
    arrivalAlarm(f.stack, 'DeadLettersAlarm', {
      alarmName: ALARM.alarmName,
      alarmDescription: 'fixture',
      queue: dlq,
      action: action(f),
    });
    return Template.fromStack(f.stack).toJSON();
  }
  const staffed = (f: Fixture): cloudwatch.IAlarmAction => f.action;
  const elsewhere = (f: Fixture): cloudwatch.IAlarmAction =>
    new actions.SnsAction(sns.Topic.fromTopicArn(f.stack, 'Elsewhere', ELSEWHERE));
  const inStack = (f: Fixture): cloudwatch.IAlarmAction =>
    new actions.SnsAction(new sns.Topic(f.stack, 'Topic', { topicName: 'fixture-alarms' }));
  const configured = configWith({
    staffedAlarmDestination: { arm: 'ExistingConfirmedSnsTopic', topicArn: CONFIGURED },
  });

  it.each([
    ['the published topic', TEST_CONFIG, staffed, { arm: 'published' }],
    ['an in-stack topic', TEST_CONFIG, inStack, { arm: 'inStackTopic', topicName: 'fixture-alarms' }],
    ['the configured topic', configured, staffed, { arm: 'configuredTopic', topicArn: CONFIGURED }],
  ] as [string, AppConfig, (f: Fixture) => cloudwatch.IAlarmAction, StaffedDestination][])(
    'accepts an alarm acting on exactly %s',
    (_name, config, action, destination) => {
      const template = dlqTemplate(config, action);
      expect(() => expectArrivalAlarm(template, { ...ALARM, staffed: destination })).not.toThrow();
    },
  );

  it.each([
    ['another topic instead of the published one', TEST_CONFIG, elsewhere, { arm: 'published' }],
    ['the published topic instead of the in-stack one', TEST_CONFIG, staffed, {
      arm: 'inStackTopic',
      topicName: 'fixture-alarms',
    }],
    ['another topic instead of the configured one', configured, elsewhere, {
      arm: 'configuredTopic',
      topicArn: CONFIGURED,
    }],
  ] as [string, AppConfig, (f: Fixture) => cloudwatch.IAlarmAction, StaffedDestination][])(
    'refuses an alarm acting on %s',
    (_name, config, action, destination) => {
      const template = dlqTemplate(config, (f) => {
        // The topic the destination names exists, so only the action can be wrong.
        if (destination.arm === 'inStackTopic') inStack(f);
        return action(f);
      });
      expect(() => expectArrivalAlarm(template, { ...ALARM, staffed: destination })).toThrow(
        /toStrictEqual/,
      );
    },
  );
});
