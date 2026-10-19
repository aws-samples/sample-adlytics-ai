// SPDX-License-Identifier: MIT-0
import { TEST_CONFIG } from './support/config';
import { expectArrivalAlarm } from './support/async-failure';
import { everyTemplate, resourcesOfType } from './support/templates';

const template = everyTemplate().InsightContinuationStack as Record<string, unknown>;

describe('the durable insight continuation worker', () => {
  it('uses an encrypted queue, DLQ, and partial-batch worker mapping', () => {
    const queues = resourcesOfType(template, 'AWS::SQS::Queue');
    expect(queues).toHaveLength(2);
    expect(JSON.stringify(queues)).toContain('SqsManagedSseEnabled');
    const mappings = resourcesOfType(template, 'AWS::Lambda::EventSourceMapping');
    expect(mappings).toHaveLength(1);
    expect(mappings[0]).toEqual(expect.objectContaining({ BatchSize: 1, FunctionResponseTypes: ['ReportBatchItemFailures'] }));
  });

  it('keeps cache fill permissions focused and excludes unrelated data-plane access', () => {
    const policies = resourcesOfType(template, 'AWS::IAM::Policy');
    const actions = JSON.stringify(policies);
    expect(actions).toContain('dynamodb:GetItem');
    expect(actions).toContain('dynamodb:PutItem');
    expect(actions).toContain('bedrock:InvokeModel');
    expect(actions).not.toContain('athena:StartQueryExecution');
    expect(actions).not.toContain('s3:GetObject');
  });

  it('generates with the configured insight models, never the chat agent seam', () => {
    // ADR-076 Phase 1: the fixture's insight values differ from the agent values exactly
    // so a rewire back to config.agent.* fails here rather than passing coincidentally.
    const rendered = JSON.stringify(template);
    expect(rendered).toContain(`:eu-west-1::foundation-model/${TEST_CONFIG.insight.quickModelId}`);
    expect(rendered).toContain(`:eu-west-1::foundation-model/${TEST_CONFIG.insight.deepModelId}`);
    expect(rendered).not.toContain('inference-profile');
    const functions = resourcesOfType(template, 'AWS::Lambda::Function');
    const worker = functions.find((fn) =>
      JSON.stringify(fn).includes('insight_continuation'),
    ) as { Environment: { Variables: Record<string, unknown> } };
    expect(worker.Environment.Variables.ADL_INSIGHT_QUICK_MODEL_ID).toBe(TEST_CONFIG.insight.quickModelId);
    expect(worker.Environment.Variables.ADL_INSIGHT_DEEP_MODEL_ID).toBe(TEST_CONFIG.insight.deepModelId);
    expect(rendered).not.toContain(TEST_CONFIG.agent.quickChat.modelId);
    expect(rendered).not.toContain(TEST_CONFIG.agent.deepDive.modelId);
  });
});

describe('the dead-letter alarm and the final-receive degrade contract', () => {
  it('alarms on each dead-lettered continuation arrival and notifies the staffed destination', () => {
    const alarms = resourcesOfType(template, 'AWS::CloudWatch::Alarm');
    // The arrival alarm and the worker's function-errors alarm (alarm-coverage.test.ts).
    expect(alarms.map((alarm) => alarm.AlarmName).sort()).toEqual([
      'adlytics-dev-InsightContinuation-function-errors',
      'adlytics-dev-insight-continuation-dead-lettered',
    ]);
    expectArrivalAlarm(template, {
      alarmName: 'adlytics-dev-insight-continuation-dead-lettered',
      queueName: 'adlytics-dev-InsightContinuationDlq',
      staffed: { arm: 'published' },
    });
    for (const alarm of alarms) {
      expect(JSON.stringify(alarm.AlarmActions)).toContain('ingestionalarmtopicarn');
    }
  });

  it('keeps the patient deep transport inside the worker timeout inside queue visibility', () => {
    // Opus deep generations measured 14-17s live (2026-09-24) and exceed the request
    // path's transport on full evidence rosters; the worker's 60s transport must fit
    // its Lambda envelope, and visibility must exceed the envelope so an in-flight
    // fill is never redelivered concurrently.
    const functions = resourcesOfType(template, 'AWS::Lambda::Function');
    const worker = functions.find((fn) =>
      JSON.stringify(fn).includes('insight_continuation'),
    ) as { Timeout: number; Environment: { Variables: Record<string, unknown> } };
    const transport = Number(worker.Environment.Variables.ADL_INSIGHT_GENERATION_TIMEOUT_SECONDS);
    expect(transport).toBe(60);
    expect(worker.Timeout).toBeGreaterThan(transport);
    const queues = resourcesOfType(template, 'AWS::SQS::Queue');
    const workQueue = queues.find(
      (queue) => (queue as { RedrivePolicy?: unknown }).RedrivePolicy !== undefined,
    ) as { VisibilityTimeout: number };
    expect(workQueue.VisibilityTimeout).toBeGreaterThan(worker.Timeout);
  });

  it('restates the queue redrive bound to the worker so its final receive can degrade', () => {
    const queues = resourcesOfType(template, 'AWS::SQS::Queue');
    const redrive = queues
      .map((queue) => (queue as { RedrivePolicy?: { maxReceiveCount?: number } }).RedrivePolicy)
      .find((policy) => policy !== undefined);
    expect(redrive?.maxReceiveCount).toBe(3);
    const functions = resourcesOfType(template, 'AWS::Lambda::Function');
    const worker = functions.find((fn) =>
      JSON.stringify(fn).includes('insight_continuation'),
    ) as { Environment: { Variables: Record<string, unknown> } };
    expect(worker.Environment.Variables.ADL_INSIGHT_CONTINUATION_MAX_RECEIVE_COUNT).toBe('3');
  });
});
