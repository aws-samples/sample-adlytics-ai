// SPDX-License-Identifier: MIT-0
import {
  conversationFunctionTimeoutSeconds,
  evaluationOutboxDispatcherRoleName,
  evaluationReleaseParameterName,
  paramPath,
} from '../lib/config';
import { TEST_CONFIG } from './support/config';
import { roleGrants } from './support/iam';
import { resourcesOfType, everyTemplate } from './support/templates';

const stack = everyTemplate().EvaluationOutboxDispatchStack as Record<string, unknown>;

function statements(): Record<string, unknown>[] {
  return resourcesOfType(stack, 'AWS::IAM::Policy').flatMap((policy) => {
    const document = policy.PolicyDocument as { Statement?: Record<string, unknown>[] };
    return document.Statement ?? [];
  });
}

describe('EvaluationOutboxDispatchStack', () => {
  it('gives the dispatcher no managed policy and stream writes on its own log group only', () => {
    const roles = Object.entries((stack.Resources ?? {}) as Record<string, { Type?: unknown; Properties?: Record<string, unknown> }>)
      .filter(([, resource]) => resource.Type === 'AWS::IAM::Role');
    expect(roles).toHaveLength(1);
    expect(roles[0]?.[1].Properties?.ManagedPolicyArns).toBeUndefined();
    const logging = statements().filter((statement) => JSON.stringify(statement.Action).includes('logs:'));
    expect(logging).toHaveLength(1);
    expect(logging[0]?.Action).toEqual(['logs:CreateLogStream', 'logs:PutLogEvents']);
    expect(JSON.stringify(logging[0]?.Resource)).toMatch(/^\{"Fn::Join":\["",\[\{"Fn::GetAtt":\["DispatcherLogs[0-9A-F]{8}","Arn"\]\},":\*"\]\]\}$/);
  });
  it('owns only disposable scheduled dispatch compute', () => {
    expect(resourcesOfType(stack, 'AWS::Lambda::Function')).toHaveLength(1);
    expect(resourcesOfType(stack, 'AWS::Events::Rule')).toHaveLength(1);
    expect(resourcesOfType(stack, 'AWS::DynamoDB::Table')).toEqual([]);
    const fn = resourcesOfType(stack, 'AWS::Lambda::Function')[0];
    expect(fn?.Handler).toBe('adapters.jobs.evaluation_outbox_dispatcher.handler.handler');
    expect(fn?.Architectures).toEqual(['arm64']);
    expect(fn?.Timeout).toBe(30);
  });

  it('grants only ready-index query, exact table transitions, and queue send', () => {
    const policy = JSON.stringify(statements());
    expect(policy).toContain('dynamodb:Query');
    expect(policy).toContain('dispatch-ready');
    expect(policy).toContain('dynamodb:GetItem');
    expect(policy).toContain('dynamodb:PutItem');
    expect(policy).toContain('dynamodb:UpdateItem');
    expect(policy).toContain('dynamodb:TransactWriteItems');
    expect(policy).toContain('sqs:SendMessage');
    expect(policy).not.toContain('dynamodb:Scan');
    expect(policy).not.toContain('sqs:ReceiveMessage');
  });
  it('reads the current family\'s release record and the delivery companion on every run (ADR-079)', () => {
    const fn = resourcesOfType(stack, 'AWS::Lambda::Function')[0] as {
      Environment: { Variables: Record<string, unknown> };
    };
    const variables = fn.Environment.Variables;
    expect(variables.ADL_EVALUATION_RELEASE_PARAMETERS).toBe(evaluationReleaseParameterName('dev', 'v4'));
    expect(variables.ADL_AGENT_QUALITY_AUDIT_TABLE).toEqual({ Ref: expect.stringContaining('agentqualityaudittable') });
    expect(variables.ADL_EVALUATION_DELIVERY_WAIT_SECONDS).toBe(
      String(conversationFunctionTimeoutSeconds(TEST_CONFIG) + 120),
    );
    const grants = roleGrants(stack, evaluationOutboxDispatcherRoleName('dev'));
    const ssm = [...grants].filter((grant) => grant.startsWith('ssm:')).sort();
    expect(ssm).toEqual((['v4'] as const).map((family) =>
      `ssm:GetParameters arn:{AWS::Partition}:ssm:eu-west-1:111122223333:parameter${evaluationReleaseParameterName('dev', family)}`));
    expect(grants).toContain(`dynamodb:GetItem ${paramPath('dev', 'agentQualityAuditTableArn')}`);
  });
});
