// SPDX-License-Identifier: MIT-0
import { paramPath } from '../lib/config';
import { everyTemplate, resourcesOfType } from './support/templates';
import { expectArrivalAlarm } from './support/async-failure';

const stack = everyTemplate().EvaluationInputStack as Record<string, unknown>;
const [writer] = resourcesOfType(stack, 'AWS::Lambda::Function');

function policies(): Record<string, unknown>[] {
  return resourcesOfType(stack, 'AWS::IAM::Policy').flatMap((policy) => {
    const document = policy.PolicyDocument as Record<string, unknown>;
    const statements = document.Statement ?? [];
    return Array.isArray(statements)
      ? statements as Record<string, unknown>[]
      : [statements as Record<string, unknown>];
  });
}

function resources(): Record<string, Record<string, unknown>> {
  return (stack.Resources ?? {}) as Record<string, Record<string, unknown>>;
}

describe('EvaluationInputStack', () => {
  it('owns only disposable queue, writer, and writer-log transport resources', () => {
    expect(resourcesOfType(stack, 'AWS::SQS::Queue')).toHaveLength(2);
    expect(resourcesOfType(stack, 'AWS::Lambda::Function')).toHaveLength(1);
    expect(resourcesOfType(stack, 'AWS::Logs::LogGroup')).toHaveLength(1);
    expect(resourcesOfType(stack, 'AWS::KMS::Key')).toEqual([]);
    expect(JSON.stringify(stack)).not.toContain('EvaluationInputLogs');

    for (const resource of Object.values(resources())) {
      if (
        resource.Type === 'AWS::SQS::Queue'
        || resource.Type === 'AWS::Lambda::Function'
        || resource.Type === 'AWS::Logs::LogGroup'
      ) {
        expect(resource.DeletionPolicy).toBe('Delete');
        expect(resource.UpdateReplacePolicy).toBe('Delete');
      }
    }
  });

  it('reads the retained evidence source and publishes only transport contracts', () => {
    expect(writer).toBeDefined();
    const environment = writer?.Environment as Record<string, unknown>;
    const parameterToken = paramPath('dev', 'evaluationInputLogGroupName')
      .replaceAll(/[^a-zA-Z0-9]/g, '')
      .toLowerCase();
    expect(JSON.stringify(environment.Variables).toLowerCase()).toContain(parameterToken);
    // Two log writes: the function's own group (ADR-085) and the retained evidence group.
    const logWrites = policies().filter((statement) =>
      JSON.stringify(statement.Action).includes('logs:PutLogEvents'),
    );
    expect(logWrites).toHaveLength(2);
    const evidenceWrites = logWrites.filter((statement) =>
      JSON.stringify(statement.Resource).toLowerCase().includes(parameterToken));
    expect(evidenceWrites).toHaveLength(1);

    expect(resourcesOfType(stack, 'AWS::SSM::Parameter').map((parameter) => parameter.Name).sort())
      .toEqual([
        paramPath('dev', 'evaluationInputQueueArn'),
        paramPath('dev', 'evaluationInputQueueUrl'),
        paramPath('dev', 'evaluationInputServiceName'),
      ]);
  });

  it('gives the writer only its own writer receipts table, never the receipts table', () => {
    // Exact grants are asserted in record-family-storage.test.ts (ADR-080 §1, §3).
    expect(writer).toBeDefined();
    const environment = writer?.Environment as Record<string, unknown>;
    expect(environment.Variables).toEqual(expect.objectContaining({
      ADL_EVALUATION_WRITER_RECEIPTS_TABLE: expect.anything(),
    }));
    expect(environment.Variables).not.toHaveProperty('ADL_AGENT_QUALITY_AUDIT_TABLE');
    const writerPolicy = policies().find((statement) =>
      JSON.stringify(statement.Action).includes('dynamodb:PutItem'),
    );
    expect(writerPolicy).toEqual(expect.objectContaining({
      Action: ['dynamodb:PutItem', 'dynamodb:GetItem'],
    }));
    expect(JSON.stringify(writerPolicy?.Resource)).toContain('evaluationwriterreceiptstablearn');
    expect(JSON.stringify(stack)).not.toContain('agentqualityaudittablearn');
    expect(JSON.stringify(writerPolicy)).not.toContain('dynamodb:Scan');
  });
});

describe('EvaluationInputStack failure path (ADR-133)', () => {
  it('alarms on each writer dead-letter arrival', () => {
    expectArrivalAlarm(stack, {
      alarmName: 'adlytics-dev-evaluation-input-dead-lettered',
      queueName: 'adlytics-dev-evaluation-input-dlq',
      staffed: { arm: 'published' },
    });
  });
});
