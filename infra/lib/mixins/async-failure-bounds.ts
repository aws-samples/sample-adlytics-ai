// SPDX-License-Identifier: MIT-0
/**
 * T1: no asynchronous consumer fails silently (ADR-133 clauses 1–3).
 *
 * Applied once to the app (`bin/app.ts`) and to every cross-stack test app
 * (`test/support/templates.ts`). On each stack it registers a validation, so a violating stack
 * fails `cdk synth` and every test that synthesizes it, naming the construct path:
 *
 * 1. A stream `CfnEventSourceMapping` (one with a starting position) sets a retry bound, a
 *    record-age bound (neither `-1`, the unbounded default), bisect-on-error and an on-failure
 *    destination that is an in-stack queue.
 * 2. An SQS `CfnEventSourceMapping` reads an in-stack queue that has a redrive policy, and every
 *    redrive policy's dead-letter target is an in-stack queue. A target this stack cannot see
 *    (a literal ARN, an imported queue) is refused, because its alarm cannot be checked here.
 * 3. Every queue that is a redrive target or an on-failure destination has an in-stack alarm in
 *    exactly the `arrivalAlarm` shape: `ARRIVALS_EXPRESSION` over that queue's
 *    `NumberOfMessagesSent` (Sum) and `ApproximateNumberOfMessagesVisible` (Maximum) at
 *    `ARRIVALS_PERIOD`, `> 0` on one of one datapoint, missing data not breaching, actions
 *    enabled and at least one action. An alarm that reads the queue's arrivals in any other
 *    shape is named with each departure, since it could stay `OK` through an arrival.
 *
 * It reads the L1 constructs' typed properties through `stack.resolve`, so it judges exactly what
 * the template will say, whichever helper or L2 produced it.
 */
import { CfnResource, IAspect, Stack } from 'aws-cdk-lib';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import { IConstruct } from 'constructs';

import {
  ARRIVALS_EXPRESSION,
  ARRIVALS_PERIOD,
  ARRIVALS_SENT_ID,
  ARRIVALS_SENT_STATISTIC,
  ARRIVALS_VISIBLE_ID,
  ARRIVALS_VISIBLE_STATISTIC,
} from '../constructs/observability/async-failure';

type Json = Record<string, unknown>;

/** A resolved property in either casing (an L2 may hand its L1 a lazily rendered value). */
function field(value: unknown, key: string): unknown {
  if (value === null || typeof value !== 'object') return undefined;
  const record = value as Json;
  return key in record ? record[key] : record[key.charAt(0).toUpperCase() + key.slice(1)];
}

/** The logical ID an in-stack `Fn::GetAtt [id, attribute]` names, or undefined. */
function getAttTarget(value: unknown, attribute: string): string | undefined {
  const getAtt = field(value, 'Fn::GetAtt');
  if (!Array.isArray(getAtt) || getAtt.length !== 2 || getAtt[1] !== attribute) return undefined;
  return typeof getAtt[0] === 'string' ? getAtt[0] : undefined;
}

function isUnbounded(value: unknown): boolean {
  return typeof value !== 'number' || value < 0;
}

/** Whether a metric query reads `metricName` on exactly the queue `queueId`. */
function readsQueueMetric(query: unknown, metricName: string, queueId: string): boolean {
  const metric = field(field(query, 'metricStat'), 'metric');
  if (field(metric, 'namespace') !== 'AWS/SQS' || field(metric, 'metricName') !== metricName) {
    return false;
  }
  const dimensions = field(metric, 'dimensions');
  return Array.isArray(dimensions)
    && dimensions.length === 1
    && field(dimensions[0], 'name') === 'QueueName'
    && getAttTarget(field(dimensions[0], 'value'), 'QueueName') === queueId;
}

/** `name is <actual>, not <expected>` when they differ. */
function departure(name: string, actual: unknown, expected: unknown): string[] {
  return actual === expected ? [] : [`${name} is ${String(actual)}, not ${String(expected)}`];
}

/**
 * How `alarm` departs from the arrival-alarm shape on queue `queueId`: undefined when it does not
 * read that queue's arrivals at all, an empty list when it is exactly the shape.
 */
function arrivalDepartures(
  stack: Stack,
  alarm: cloudwatch.CfnAlarm,
  queueId: string,
): string[] | undefined {
  const metrics = stack.resolve(alarm.metrics);
  if (!Array.isArray(metrics)) return undefined;
  const expressions = metrics.filter((query) => field(query, 'expression') === ARRIVALS_EXPRESSION);
  const byId = new Map(metrics.map((query) => [field(query, 'id'), query]));
  const sent = byId.get(ARRIVALS_SENT_ID);
  const visible = byId.get(ARRIVALS_VISIBLE_ID);
  if (
    expressions.length === 0
    || !readsQueueMetric(sent, 'NumberOfMessagesSent', queueId)
    || !readsQueueMetric(visible, 'ApproximateNumberOfMessagesVisible', queueId)
  ) {
    return undefined;
  }
  const period = ARRIVALS_PERIOD.toSeconds();
  const sentStat = field(sent, 'metricStat');
  const visibleStat = field(visible, 'metricStat');
  const actions = stack.resolve(alarm.alarmActions);
  return [
    ...departure('metric query count', metrics.length, 3),
    // Exactly `true`: an omitted ReturnData is a departure, not a default to assume.
    ...departure('arrival expression returnData', field(expressions[0], 'returnData'), true),
    ...departure(`${ARRIVALS_SENT_ID} returnData`, field(sent, 'returnData'), false),
    ...departure(`${ARRIVALS_VISIBLE_ID} returnData`, field(visible, 'returnData'), false),
    ...departure(`${ARRIVALS_SENT_ID} statistic`, field(sentStat, 'stat'), ARRIVALS_SENT_STATISTIC),
    ...departure(
      `${ARRIVALS_VISIBLE_ID} statistic`,
      field(visibleStat, 'stat'),
      ARRIVALS_VISIBLE_STATISTIC,
    ),
    ...departure(`${ARRIVALS_SENT_ID} period`, field(sentStat, 'period'), period),
    ...departure(`${ARRIVALS_VISIBLE_ID} period`, field(visibleStat, 'period'), period),
    ...departure('threshold', stack.resolve(alarm.threshold), 0),
    ...departure('comparisonOperator', stack.resolve(alarm.comparisonOperator), 'GreaterThanThreshold'),
    ...departure('evaluationPeriods', stack.resolve(alarm.evaluationPeriods), 1),
    ...departure('datapointsToAlarm', stack.resolve(alarm.datapointsToAlarm), 1),
    ...departure('treatMissingData', stack.resolve(alarm.treatMissingData), 'notBreaching'),
    ...departure('actionsEnabled', stack.resolve(alarm.actionsEnabled) !== false, true),
    ...(Array.isArray(actions) && actions.length > 0 ? [] : ['it has no alarm action']),
  ];
}

export class AsyncFailureBoundsAspect implements IAspect {
  private readonly registered = new WeakSet<Stack>();

  visit(node: IConstruct): void {
    if (!Stack.isStack(node) || this.registered.has(node)) return;
    this.registered.add(node);
    const stack = node;
    stack.node.addValidation({ validate: () => violations(stack) });
  }
}

/** One message per violation in `stack`, each naming the construct path. */
export function violations(stack: Stack): string[] {
  const owned = stack.node.findAll().filter(
    (construct): construct is CfnResource =>
      construct instanceof CfnResource && Stack.of(construct) === stack,
  );
  const queues = new Map<string, sqs.CfnQueue>();
  for (const queue of owned.filter((c): c is sqs.CfnQueue => c instanceof sqs.CfnQueue)) {
    queues.set(stack.getLogicalId(queue), queue);
  }
  const alarms = owned.filter((c): c is cloudwatch.CfnAlarm => c instanceof cloudwatch.CfnAlarm);
  const found: string[] = [];
  // Failure destination logical ID -> the construct path that sends to it.
  const destinations = new Map<string, string>();

  for (const queue of queues.values()) {
    const redrive = stack.resolve(queue.redrivePolicy);
    if (redrive === undefined) continue;
    const target = getAttTarget(field(redrive, 'deadLetterTargetArn'), 'Arn');
    if (target === undefined || !queues.has(target)) {
      found.push(`${queue.node.path}: redrive dead-letter target is not an in-stack queue`);
      continue;
    }
    destinations.set(target, queue.node.path);
  }

  for (const mapping of owned.filter(
    (c): c is lambda.CfnEventSourceMapping => c instanceof lambda.CfnEventSourceMapping,
  )) {
    const path = mapping.node.path;
    if (stack.resolve(mapping.startingPosition) !== undefined) {
      if (isUnbounded(stack.resolve(mapping.maximumRetryAttempts))) {
        found.push(`${path}: stream mapping sets no maximumRetryAttempts bound`);
      }
      if (isUnbounded(stack.resolve(mapping.maximumRecordAgeInSeconds))) {
        found.push(`${path}: stream mapping sets no maximumRecordAgeInSeconds bound`);
      }
      if (stack.resolve(mapping.bisectBatchOnFunctionError) !== true) {
        found.push(`${path}: stream mapping does not set bisectBatchOnFunctionError`);
      }
      const destination = field(
        field(stack.resolve(mapping.destinationConfig), 'onFailure'),
        'destination',
      );
      if (destination === undefined) {
        found.push(`${path}: stream mapping has no destinationConfig.onFailure`);
        continue;
      }
      const target = getAttTarget(destination, 'Arn');
      if (target === undefined || !queues.has(target)) {
        found.push(`${path}: stream mapping's on-failure destination is not an in-stack queue`);
        continue;
      }
      destinations.set(target, path);
      continue;
    }
    const source = getAttTarget(stack.resolve(mapping.eventSourceArn), 'Arn');
    const queue = source === undefined ? undefined : queues.get(source);
    if (queue === undefined) {
      found.push(`${path}: SQS mapping does not read an in-stack queue`);
    } else if (stack.resolve(queue.redrivePolicy) === undefined) {
      found.push(`${path}: SQS consumer queue ${queue.node.path} has no redrive dead-letter queue`);
    }
  }

  for (const [queueId, sender] of destinations) {
    const path = queues.get(queueId)?.node.path ?? queueId;
    const candidates = alarms.flatMap((alarm) => {
      const departures = arrivalDepartures(stack, alarm, queueId);
      return departures === undefined ? [] : [{ alarm, departures }];
    });
    if (candidates.some(({ departures }) => departures.length === 0)) continue;
    found.push(`${path}: failure queue (from ${sender}) has no arrival alarm with an action`);
    for (const { alarm, departures } of candidates) {
      found.push(
        `${alarm.node.path}: arrival alarm on ${path} departs from the arrival shape: `
          + departures.join('; '),
      );
    }
  }
  return found;
}
