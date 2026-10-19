// SPDX-License-Identifier: MIT-0
/**
 * The one shape every asynchronous consumer's failure path takes (ADR-133 clauses 1–3).
 *
 * A stream mapping bounds its retries and record age, bisects a failing batch and sends an
 * exhausted batch's metadata to an on-failure queue. An SQS consumer redrives to a dead-letter
 * queue. Every such queue alarms on arrival, never on standing depth, and the alarm acts on the
 * staffed destination. `AsyncFailureBoundsAspect` (`lib/mixins/async-failure-bounds.ts`) fails
 * synth on any consumer that departs from it.
 *
 * Functions, not a construct: a construct would nest the mapping and change its logical ID,
 * which replaces it, and a replaced `TRIM_HORIZON` mapping re-reads its stream while a replaced
 * `LATEST` one drops the gap. Each helper creates its resources in the caller's scope with the
 * caller's ids.
 */
import * as cdk from 'aws-cdk-lib';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as actions from 'aws-cdk-lib/aws-cloudwatch-actions';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';

import { AppConfig, AsyncFailureBoundsConfig, paramPath } from '../../config';

/**
 * One arrival signal per 5-minute period: messages sent, or any rise in visible depth.
 *
 * `NumberOfMessagesSent` does not count a message SQS moves to a dead-letter queue after failed
 * receives, so the rise in visible depth is the arrival signal there. A queue left holding
 * earlier messages reads 0 on both, so it never holds the alarm in `ALARM` and hides the next
 * arrival (the `ingestion-dead-lettered` blind spot, 2026-10-06 to 2026-10-09).
 */
export const ARRIVALS_EXPRESSION = 'MAX([FILL(sent, 0), FILL(DIFF(visible), 0)])';
/** The ids the arrival expression reads; the aspect matches them. */
export const ARRIVALS_SENT_ID = 'sent';
export const ARRIVALS_VISIBLE_ID = 'visible';
/** The statistic each arrival metric reads and their shared period; the aspect matches them. */
export const ARRIVALS_SENT_STATISTIC = 'Sum';
export const ARRIVALS_VISIBLE_STATISTIC = 'Maximum';
export const ARRIVALS_PERIOD = cdk.Duration.minutes(5);
/** A failure queue keeps a message for SQS's maximum, so its disposition has two weeks. */
const FAILURE_QUEUE_RETENTION = cdk.Duration.days(14);

/**
 * FR-10: the one topic every generated alarm acts on. The email arm (and an environment with no
 * selection) is the existing in-account ingestion topic; the topic arm imports its exact ARN.
 */
export function staffedAlarmTopic(
  scope: Construct,
  config: AppConfig,
  ingestionTopic: sns.ITopic,
): sns.ITopic {
  const destination = config.staffedAlarmDestination;
  if (destination === undefined || destination.arm === 'ExistingInAccountTopicEmail') {
    return ingestionTopic;
  }
  return sns.Topic.fromTopicArn(scope, 'StaffedAlarmTopic', destination.topicArn);
}

/**
 * The staffed destination as an alarm action, for a stack that does not own the ingestion
 * topic: the email arm's topic is read from the ARN IngestionStack publishes
 * (`ingestionAlarmTopicArn`), never written here. Call once per scope.
 */
export function staffedAlarmAction(scope: Construct, config: AppConfig): actions.SnsAction {
  const destination = config.staffedAlarmDestination;
  if (destination?.arm === 'ExistingConfirmedSnsTopic') {
    return new actions.SnsAction(
      sns.Topic.fromTopicArn(scope, 'StaffedAlarmTopic', destination.topicArn),
    );
  }
  const published = ssm.StringParameter.valueForStringParameter(
    scope,
    paramPath(config.envName, 'ingestionAlarmTopicArn'),
  );
  return new actions.SnsAction(sns.Topic.fromTopicArn(scope, 'IngestionAlarmTopic', published));
}

export interface ArrivalAlarmProps {
  readonly alarmName: string;
  readonly alarmDescription: string;
  readonly queue: sqs.IQueue;
  readonly action: cloudwatch.IAlarmAction;
}

/** An alarm on each arrival at a dead-letter or failure queue, acting on `action`. */
export function arrivalAlarm(scope: Construct, id: string, props: ArrivalAlarmProps): cloudwatch.Alarm {
  const alarm = new cloudwatch.Alarm(scope, id, {
    alarmName: props.alarmName,
    alarmDescription: props.alarmDescription,
    metric: new cloudwatch.MathExpression({
      expression: ARRIVALS_EXPRESSION,
      usingMetrics: {
        [ARRIVALS_SENT_ID]: props.queue.metricNumberOfMessagesSent({
          statistic: ARRIVALS_SENT_STATISTIC,
        }),
        [ARRIVALS_VISIBLE_ID]: props.queue.metricApproximateNumberOfMessagesVisible({
          statistic: ARRIVALS_VISIBLE_STATISTIC,
        }),
      },
      period: ARRIVALS_PERIOD,
    }),
    threshold: 0,
    comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
    evaluationPeriods: 1,
    datapointsToAlarm: 1,
    // An idle queue reports nothing, which is the normal state, not a signal.
    treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
  });
  alarm.addAlarmAction(props.action);
  return alarm;
}

export interface BoundedStreamMappingProps {
  readonly fn: lambda.IFunction;
  readonly eventSourceArn: string;
  readonly startingPosition: 'LATEST' | 'TRIM_HORIZON';
  readonly batchSize: number;
  readonly parallelizationFactor: number;
  readonly functionResponseTypes?: string[];
  readonly filterCriteria?: lambda.CfnEventSourceMapping.FilterCriteriaProperty;
  readonly enabled?: boolean;
  readonly bounds: AsyncFailureBoundsConfig;
  /** The on-failure queue: its id in `scope` and its explicit physical name. */
  readonly failureQueue: { readonly id: string; readonly queueName: string };
  /** The arrival alarm on that queue. */
  readonly alarm: {
    readonly id: string;
    readonly alarmName: string;
    readonly alarmDescription: string;
    readonly action: cloudwatch.IAlarmAction;
  };
}

export interface BoundedStreamMapping {
  readonly mapping: lambda.CfnEventSourceMapping;
  readonly failureQueue: sqs.Queue;
  readonly alarm: cloudwatch.Alarm;
}

/**
 * A stream mapping with a retry bound, a record-age bound, bisect-on-error and an alarmed
 * on-failure queue, created in `scope` with `id` so the mapping's logical ID is the caller's.
 * The function's role gets only the send grant on its own failure queue.
 */
export function boundedStreamMapping(
  scope: Construct,
  id: string,
  props: BoundedStreamMappingProps,
): BoundedStreamMapping {
  const failureQueue = new sqs.Queue(scope, props.failureQueue.id, {
    queueName: props.failureQueue.queueName,
    encryption: sqs.QueueEncryption.SQS_MANAGED,
    enforceSSL: true,
    retentionPeriod: FAILURE_QUEUE_RETENTION,
  });
  failureQueue.grantSendMessages(props.fn);
  const mapping = new lambda.CfnEventSourceMapping(scope, id, {
    functionName: props.fn.functionName,
    eventSourceArn: props.eventSourceArn,
    ...(props.enabled === undefined ? {} : { enabled: props.enabled }),
    startingPosition: props.startingPosition,
    batchSize: props.batchSize,
    parallelizationFactor: props.parallelizationFactor,
    ...(props.functionResponseTypes === undefined
      ? {}
      : { functionResponseTypes: props.functionResponseTypes }),
    ...(props.filterCriteria === undefined ? {} : { filterCriteria: props.filterCriteria }),
    maximumRetryAttempts: props.bounds.streamMaxRetryAttempts,
    maximumRecordAgeInSeconds: props.bounds.streamMaxRecordAgeSeconds,
    bisectBatchOnFunctionError: true,
    destinationConfig: { onFailure: { destination: failureQueue.queueArn } },
  });
  const alarm = arrivalAlarm(scope, props.alarm.id, {
    alarmName: props.alarm.alarmName,
    alarmDescription: props.alarm.alarmDescription,
    queue: failureQueue,
    action: props.alarm.action,
  });
  return { mapping, failureQueue, alarm };
}
