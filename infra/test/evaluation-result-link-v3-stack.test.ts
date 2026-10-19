// SPDX-License-Identifier: MIT-0
import {
  evaluationResultIngressV3FunctionName,
  evaluationResultLinkerV3FunctionName,
  evaluationResultProjectionV3QueueName,
  evaluationResultSubscriptionName,
  paramPath,
} from '../lib/config';
import { TEST_CONFIG } from './support/config';
import { roleGrants } from './support/iam';
import { everyTemplate, resourcesOfType } from './support/templates';
import { expectArrivalAlarm } from './support/async-failure';
const template = everyTemplate().EvaluationResultLinkV3Stack as Record<string, unknown>;
type Resource = { Type: string; Properties: Record<string, unknown>; DependsOn?: string[] };
const resources = Object.entries(template.Resources as Record<string, Resource>);
const ingressId = resources.find(([, resource]) =>
  resource.Type === 'AWS::Lambda::Function'
  && resource.Properties.FunctionName === evaluationResultIngressV3FunctionName(TEST_CONFIG.envName))![0];
const ingress = resources.find(([id]) => id === ingressId)![1].Properties as {
  Environment: { Variables: Record<string, unknown> };
};
describe('EvaluationResultLinkV3Stack', () => {
  it('subscribes to the current family\'s owned result group only and uses distinct identities', () => {
    const rendered = JSON.stringify(template);
    expect(rendered).toContain(paramPath(TEST_CONFIG.envName, 'evaluationV4ResultLogGroupName'));
    // The V3 family is retired (ADR-079): no V3 group is read or subscribed.
    expect(rendered).not.toContain('result-log-group-v3');
    expect(rendered).not.toContain('results-v3');
    // Configuration and evaluator identity come from each turn's delivery receipt (ADR-079).
    expect(rendered).not.toContain('released-evaluator-');
    expect(rendered).not.toContain('/evaluation/released-');
    const functions = resourcesOfType(template, 'AWS::Lambda::Function');
    expect(JSON.stringify(functions)).toContain(evaluationResultLinkerV3FunctionName(TEST_CONFIG.envName));
    expect(JSON.stringify(functions)).not.toContain(
      '"FunctionName":"adlytics-dev-EvaluationResultIngress"',
    );
    const queues = resourcesOfType(template, 'AWS::SQS::Queue');
    expect(JSON.stringify(queues)).toContain(evaluationResultProjectionV3QueueName(TEST_CONFIG.envName));
    expect(resourcesOfType(template, 'AWS::Logs::SubscriptionFilter')).toEqual([
      expect.objectContaining({
        FilterName: evaluationResultSubscriptionName(TEST_CONFIG.envName, 'v4'),
        FilterPattern: '{ $.traceId = "*" }',
      }),
    ]);
    expect(resourcesOfType(template, 'AWS::Lambda::EventSourceMapping')).toEqual([
      expect.objectContaining({ BatchSize: 1, FunctionResponseTypes: ['ReportBatchItemFailures'] }),
    ]);
  });

  it('admits results by delivery receipt: owned groups and the deliveries table, no configuration list', () => {
    const variables = Object.keys(ingress.Environment.Variables);
    expect(variables).toEqual(expect.arrayContaining([
      'ADL_RELEASED_EVALUATION_RESULT_LOG_GROUPS',
      'ADL_EVALUATION_DELIVERIES_TABLE',
    ]));
    expect(variables).not.toContain('ADL_RELEASED_ONLINE_EVALUATION_CONFIG_IDS');
    expect(variables).not.toContain('ADL_RELEASED_EVALUATOR_ID');
    const grants = roleGrants(template, `adlytics-${TEST_CONFIG.envName}-evaluation-result-ingress`);
    expect(grants).toContain(
      `dynamodb:GetItem ${paramPath(TEST_CONFIG.envName, 'evaluationDeliveriesTableArn')}`,
    );
  });

  it('lets only CloudWatch Logs, from an owned group, invoke the ingress', () => {
    expect(resourcesOfType(template, 'AWS::Lambda::Permission')).toEqual([]);
    const policies = resources.filter(([, resource]) => resource.Type === 'AWS::Lambda::ResourcePolicy');
    expect(policies).toHaveLength(1);
    const [policyId, policy] = policies[0]!;
    const functionArn = { 'Fn::GetAtt': [ingressId, 'Arn'] };
    // The registry schema's property, which the deployed route-exchange policy also uses.
    expect(policy.Properties).toStrictEqual({ ResourceArn: functionArn, PolicyDocument: expect.anything() });
    const groupArn = (key: string): unknown => ({
      'Fn::Join': ['', [
        'arn:', { Ref: 'AWS::Partition' }, ':logs:eu-west-1:111122223333:log-group:',
        { Ref: expect.stringContaining(key) }, ':*',
      ]],
    });
    expect(policy.Properties.PolicyDocument).toEqual({
      Version: '2012-10-17',
      Statement: [
        {
          Sid: 'AllowResultSubscriptions',
          Effect: 'Allow',
          Principal: { Service: 'logs.amazonaws.com' },
          Action: 'lambda:InvokeFunction',
          Resource: functionArn,
          Condition: {
            StringEquals: { 'aws:SourceAccount': '111122223333' },
            ArnLike: { 'aws:SourceArn': [groupArn('resultloggroupv4')] },
          },
        },
        {
          // Keyed on the source: a direct invoke carries no SourceArn, so the negated match denies it.
          Sid: 'DenyAllButOwnedResultGroups',
          Effect: 'Deny',
          Principal: '*',
          Action: ['lambda:InvokeFunction', 'lambda:InvokeAsync'],
          Resource: functionArn,
          Condition: {
            ArnNotLike: { 'aws:SourceArn': [groupArn('resultloggroupv4')] },
            StringNotEquals: { 'aws:PrincipalServiceName': 'lambda.amazonaws.com' },
          },
        },
      ],
    });
    // The one service exemption is Lambda's own async execution, never CloudWatch Logs by name.
    expect(JSON.stringify(policy)).not.toContain('"aws:PrincipalServiceName":"logs.amazonaws.com"');
    // PutSubscriptionFilter checks the destination's permission, so each filter waits for it.
    for (const [, resource] of resources.filter(([, r]) => r.Type === 'AWS::Logs::SubscriptionFilter')) {
      expect(resource.DependsOn).toContain(policyId);
    }
  });
});

describe('EvaluationResultLinkV3Stack failure path (ADR-133)', () => {
  it('alarms on each linker dead-letter arrival', () => {
    expectArrivalAlarm(template, {
      alarmName: 'adlytics-dev-evaluation-result-projection-dead-lettered',
      queueName: 'adlytics-dev-evaluation-result-projection-v3-dlq',
      staffed: { arm: 'published' },
    });
  });
});
