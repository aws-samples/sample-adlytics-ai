// SPDX-License-Identifier: MIT-0
import * as cdk from 'aws-cdk-lib';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import { Construct } from 'constructs';
import { AppConfig } from '../../config';
import { BAKE_TASKS } from '../../generated/execution-graph-protocol';

/**
 * ADR-139 layer 4: a presentation grant-serving refusal that outlasts the settle bound pages.
 *
 * `report_serving_refusal` (`src/adapters/outbound/telemetry/reporting.py`) emits
 * `PresentationServingRefused` in the `Adlytics` namespace with exactly the closed dimensions
 * `Reason` (`ungranted_generation`, `serving_refresh_pending`) and `Settle` (`within_settle`,
 * `past_settle`). Presentation classifies each refusal against `SERVING_REFUSAL_SETTLE`
 * (`domain/presentation/graph_serving.py`, = `FINALIZER_BLOCKED_AFTER`, 270 s), so infra never
 * recomputes the window: the alarm counts only `past_settle`. A `within_settle` refusal is a defect
 * signal too, recorded by ops check O3, not paged.
 *
 * The WHERE clause uses `=` only, the form CloudWatch accepts (`error-alarms.ts`).
 */
export const SERVING_REFUSAL_METRIC = 'PresentationServingRefused';
export const SERVING_REFUSAL_QUERY =
  `SELECT SUM(${SERVING_REFUSAL_METRIC}) FROM SCHEMA("Adlytics", Reason, Settle) `
  + "WHERE Settle = 'past_settle'";
/** One bake worker schedule: the period every bake-side alarm evaluates on. */
export const SERVING_REFUSAL_PERIOD_SECONDS = BAKE_TASKS.worker_schedule_seconds;

export function buildServingRefusalAlarm(
  scope: Construct,
  config: AppConfig,
  action: cloudwatch.IAlarmAction,
): cloudwatch.Alarm {
  const alarm = new cloudwatch.Alarm(scope, 'PresentationServingRefused', {
    alarmName: `adlytics-${config.envName}-presentation-serving-refused`,
    alarmDescription:
      'A Presentation page refused a grant-bearing generation (ungranted_generation or '
      + 'serving_refresh_pending) more than the settle bound after its bind. The finalizer moves '
      + 'CURRENT only under a binding presentation accepts, so this is a write-side defect. Read the '
      + 'Presentation WARNING operation refused lines (operation, error_context.reason, '
      + 'error_context.refusal_age_seconds), then follow the PresentationServingRefused entry in '
      + 'operator-tools.md.',
    metric: new cloudwatch.MathExpression({
      expression: SERVING_REFUSAL_QUERY,
      period: cdk.Duration.seconds(SERVING_REFUSAL_PERIOD_SECONDS),
    }),
    threshold: 1,
    comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
    evaluationPeriods: 1,
    datapointsToAlarm: 1,
    treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
  });
  alarm.addAlarmAction(action);
  alarm.addOkAction(action);
  return alarm;
}
