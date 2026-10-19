// SPDX-License-Identifier: MIT-0
/**
 * ADR-139 layer 4: the presentation grant-serving refusal alarm, read back from the synthesized
 * ApiStack. It counts only `past_settle` refusals (presentation classifies against
 * `SERVING_REFUSAL_SETTLE`), on the bake worker schedule's period, and acts on the staffed
 * destination on both transitions, the same action the stack's function error alarms use.
 */
import { BAKE_TASKS } from '../lib/generated/execution-graph-protocol';
import {
  SERVING_REFUSAL_METRIC,
  SERVING_REFUSAL_PERIOD_SECONDS,
  SERVING_REFUSAL_QUERY,
} from '../lib/constructs/observability/serving-refusal-alarm';
import { everyTemplate } from './support/templates';

type Props = Record<string, unknown>;

const api = everyTemplate().ApiStack as Props;
const alarms = Object.values(api.Resources as Props)
  .filter((resource): resource is Props => typeof resource === 'object' && resource !== null)
  .filter((resource) => resource.Type === 'AWS::CloudWatch::Alarm')
  .map((resource) => resource.Properties as Props);

function alarmNamed(name: string): Props {
  const found = alarms.filter((alarm) => alarm.AlarmName === name);
  expect(found).toHaveLength(1);
  return found[0]!;
}

function expectRefusalAlarm(alarm: Props, query: string, staffed: unknown): void {
  expect(alarm).toEqual(expect.objectContaining({
    Metrics: [{
      Expression: query,
      Id: expect.any(String),
      Period: SERVING_REFUSAL_PERIOD_SECONDS,
      ReturnData: true,
    }],
    Threshold: 1,
    ComparisonOperator: 'GreaterThanOrEqualToThreshold',
    EvaluationPeriods: 1,
    DatapointsToAlarm: 1,
    TreatMissingData: 'notBreaching',
    AlarmActions: staffed,
    OKActions: staffed,
  }));
}

describe('PresentationServingRefused alarm (ADR-139)', () => {
  const alarm = alarmNamed('adlytics-dev-presentation-serving-refused');
  // The staffed destination as every other ApiStack alarm acts on it.
  const staffed = alarms.find((item) => item !== alarm && item.AlarmActions !== undefined)?.AlarmActions;

  it('counts only past-settle refusals of the closed metric on the bake schedule period', () => {
    expect(SERVING_REFUSAL_QUERY).toBe(
      `SELECT SUM(${SERVING_REFUSAL_METRIC}) FROM SCHEMA("Adlytics", Reason, Settle) `
      + "WHERE Settle = 'past_settle'",
    );
    expect(SERVING_REFUSAL_METRIC).toBe('PresentationServingRefused');
    expect(SERVING_REFUSAL_PERIOD_SECONDS).toBe(BAKE_TASKS.worker_schedule_seconds);
    expect(SERVING_REFUSAL_PERIOD_SECONDS).toBe(60);
    expect(staffed).toBeDefined();
    expectRefusalAlarm(alarm, SERVING_REFUSAL_QUERY, staffed);
    // Control: a query counting every refusal, settling ones included, is not the synthesized one.
    expect(() => expectRefusalAlarm(
      alarm,
      `SELECT SUM(${SERVING_REFUSAL_METRIC}) FROM SCHEMA("Adlytics", Reason, Settle)`,
      staffed,
    )).toThrow();
  });

  it('names its runbook entry and the log fields that diagnose a refusal', () => {
    const description = String(alarm.AlarmDescription);
    for (const field of ['operation refused', 'error_context.reason', 'refusal_age_seconds']) {
      expect(description).toContain(field);
    }
    expect(description).toContain('PresentationServingRefused entry in operator-tools.md');
  });
});
