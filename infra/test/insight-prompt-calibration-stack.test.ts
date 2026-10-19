// SPDX-License-Identifier: MIT-0
import { TEST_CONFIG } from './support/config';
import { expectArrivalAlarm } from './support/async-failure';
import { everyTemplate, resourcesOfType } from './support/templates';

const template = everyTemplate().InsightPromptCalibrationStack as Record<string, unknown>;

describe('the queued insight prompt calibration worker', () => {
  it('buffers work in an encrypted queue with bounded redrive and partial batch retry', () => {
    const queues = resourcesOfType(template, 'AWS::SQS::Queue');
    expect(queues).toHaveLength(2);
    expect(JSON.stringify(queues)).toContain('SqsManagedSseEnabled');
    const work = queues.find((queue) => queue.RedrivePolicy !== undefined) as {
      RedrivePolicy: { maxReceiveCount: number };
      VisibilityTimeout: number;
    };
    expect(work.RedrivePolicy.maxReceiveCount).toBe(3);
    const mappings = resourcesOfType(template, 'AWS::Lambda::EventSourceMapping');
    expect(mappings).toHaveLength(1);
    expect(mappings[0]).toEqual(expect.objectContaining({
      BatchSize: 1,
      FunctionResponseTypes: ['ReportBatchItemFailures'],
      ScalingConfig: { MaximumConcurrency: 2 },
    }));
  });

  it('keeps patient model transport inside worker timeout and queue visibility', () => {
    const worker = resourcesOfType(template, 'AWS::Lambda::Function')[0] as {
      Timeout: number;
      Environment: { Variables: Record<string, string> };
    };
    const transport = Number(worker.Environment.Variables.ADL_INSIGHT_GENERATION_TIMEOUT_SECONDS);
    const lease = Number(
      worker.Environment.Variables.ADL_INSIGHT_PROMPT_CALIBRATION_LEASE_SECONDS,
    );
    const work = resourcesOfType(template, 'AWS::SQS::Queue').find(
      (queue) => queue.RedrivePolicy !== undefined,
    ) as { VisibilityTimeout: number };
    expect(transport).toBe(45);
    expect(worker.Timeout).toBe(90);
    expect(lease).toBe(85);
    expect(transport).toBeLessThan(lease);
    expect(lease).toBeLessThan(worker.Timeout);
    expect(work.VisibilityTimeout).toBeGreaterThan(worker.Timeout);
  });

  it('can invoke only the configured insight models and mutate only the prompt registry', () => {
    const rendered = JSON.stringify(template);
    expect(rendered).toContain(`:eu-west-1::foundation-model/${TEST_CONFIG.insight.quickModelId}`);
    expect(rendered).toContain(`:eu-west-1::foundation-model/${TEST_CONFIG.insight.deepModelId}`);
    expect(rendered).not.toContain('inference-profile');
    expect(rendered).toContain('dynamodb:UpdateItem');
    expect(rendered).toContain('dynamodb:ConditionCheckItem');
    expect(rendered).not.toContain('athena:StartQueryExecution');
    expect(rendered).not.toContain('s3:GetObject');
  });

  it('runs the retained outbox dispatcher every minute with queue-send authority', () => {
    const rules = resourcesOfType(template, 'AWS::Events::Rule');
    expect(rules).toHaveLength(1);
    expect(rules[0]!.ScheduleExpression).toBe('rate(1 minute)');
    expect(JSON.stringify(template)).toContain('sqs:SendMessage');
    expect(JSON.stringify(template)).toContain('dynamodb:DeleteItem');
    expect(JSON.stringify(template)).toContain('dynamodb:Query');
  });

  it('alarms on stale work and on each dead-lettered calibration arrival', () => {
    const all = resourcesOfType(template, 'AWS::CloudWatch::Alarm');
    // The worker's function-errors alarm is asserted by alarm-coverage.test.ts.
    expect(all.map((alarm) => alarm.AlarmName)).toContain(
      'adlytics-dev-InsightPromptCalibration-function-errors',
    );
    const alarms = all.filter(
      (alarm) => alarm.AlarmName !== 'adlytics-dev-InsightPromptCalibration-function-errors',
    );
    expect(alarms).toHaveLength(2);
    expect(all).toHaveLength(3);
    expect(alarms.map((alarm) => alarm.MetricName)).toEqual(
      expect.arrayContaining(['ApproximateAgeOfOldestMessage']),
    );
    expectArrivalAlarm(template, {
      alarmName: 'adlytics-dev-insight-prompt-calibration-dead-lettered',
      queueName: 'adlytics-dev-InsightPromptCalibrationDlq',
      staffed: { arm: 'published' },
    });
    for (const alarm of alarms) {
      // Both act on the staffed destination: the published ingestion topic for the email arm.
      expect(JSON.stringify(alarm.AlarmActions)).toContain('ingestionalarmtopicarn');
      expect(alarm).toEqual(expect.objectContaining({
        ComparisonOperator: 'GreaterThanThreshold',
        EvaluationPeriods: 1,
        TreatMissingData: 'notBreaching',
      }));
      expect(JSON.stringify(alarm)).toContain('AlarmActions');
    }
  });
});
