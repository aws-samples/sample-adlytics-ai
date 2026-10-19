// SPDX-License-Identifier: MIT-0
import {
  evaluationInputLogGroupName,
  evaluationOutboxTableName,
  evaluationResultLogGroupName,
  paramPath,
} from '../lib/config';
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { EvaluationEvidenceStack } from '../lib/stacks/evaluation-evidence-stack';
import { configWith } from './support/config';
import { everyTemplate, resourcesOfType } from './support/templates';

const template = everyTemplate().EvaluationEvidenceStack as Record<string, unknown>;

function resources(): Record<string, Record<string, unknown>> {
  return (template.Resources ?? {}) as Record<string, Record<string, unknown>>;
}

describe('EvaluationEvidenceStack', () => {
  it('retains default-encrypted released-response evidence, durable outbox state, and record tables', () => {
    const logGroups = resourcesOfType(template, 'AWS::Logs::LogGroup');
    const tables = resourcesOfType(template, 'AWS::DynamoDB::Table');
    expect(resourcesOfType(template, 'AWS::KMS::Key')).toEqual([]);
    expect(logGroups).toEqual([
      expect.objectContaining({
        LogGroupName: evaluationInputLogGroupName('dev'),
        RetentionInDays: 14,
      }),
      // The current family's owned result group (V4; V3 was retired, ADR-079).
      expect.objectContaining({ LogGroupName: evaluationResultLogGroupName('dev', 'v4') }),
    ]);
    // The three ADR-080 record tables are asserted in record-family-storage.test.ts.
    expect(tables).toHaveLength(4);
    expect(tables.filter((table) => table.TableName === evaluationOutboxTableName('dev'))).toEqual([
      expect.objectContaining({
        BillingMode: 'PAY_PER_REQUEST',
        KeySchema: [{ AttributeName: 'admission_ref', KeyType: 'HASH' }],
        PointInTimeRecoverySpecification: { PointInTimeRecoveryEnabled: true },
        SSESpecification: { SSEEnabled: true },
        TimeToLiveSpecification: { AttributeName: 'expires_at', Enabled: true },
        GlobalSecondaryIndexes: [
          expect.objectContaining({
            IndexName: 'dispatch-ready',
            Projection: { ProjectionType: 'KEYS_ONLY' },
          }),
        ],
      }),
    ]);

    for (const resource of Object.values(resources()).filter((item) =>
      ['AWS::Logs::LogGroup', 'AWS::DynamoDB::Table'].includes(String(item.Type)),
    )) {
      expect(resource.DeletionPolicy).toBe('Retain');
      expect(resource.UpdateReplacePolicy).toBe('Retain');
    }
  });

  it('publishes retained evidence contracts without disposable compute or queues', () => {
    expect(resourcesOfType(template, 'AWS::SSM::Parameter').map((item) => item.Name).sort())
      .toEqual([
        paramPath('dev', 'evaluationInputLogGroupName'),
        paramPath('dev', 'evaluationOutboxTableArn'),
        paramPath('dev', 'evaluationOutboxTableName'),
        paramPath('dev', 'evaluationDeliveriesTableName'),
        paramPath('dev', 'evaluationDeliveriesTableArn'),
        paramPath('dev', 'evaluationWriterReceiptsTableName'),
        paramPath('dev', 'evaluationWriterReceiptsTableArn'),
        paramPath('dev', 'evaluationVerdictsTableName'),
        paramPath('dev', 'evaluationVerdictsTableArn'),
        paramPath('dev', 'evaluationDeliveriesStreamArn'),
        paramPath('dev', 'evaluationWriterReceiptsStreamArn'),
        paramPath('dev', 'evaluationVerdictsStreamArn'),
        paramPath('dev', 'evaluationV4ResultLogGroupName'),
      ].sort());
    expect(resourcesOfType(template, 'AWS::Lambda::Function')).toEqual([]);
    expect(resourcesOfType(template, 'AWS::SQS::Queue')).toEqual([]);
  });
  it('owns one result group for the current family, protected, retained, and with no key (ADR-079)', () => {
    const groups = Object.values(resources()).filter((item) => item.Type === 'AWS::Logs::LogGroup'
      && String((item.Properties as Record<string, unknown>).LogGroupName).includes('/evaluation/results-'));
    expect(groups.map((group) => group.Properties)).toEqual((['v4'] as const).map((family) =>
      expect.objectContaining({
        LogGroupName: evaluationResultLogGroupName('dev', family),
        RetentionInDays: 14,
        DeletionProtectionEnabled: true,
      })));
    for (const group of groups) {
      expect(group.Properties).not.toHaveProperty('KmsKeyId');
      expect(group.DeletionPolicy).toBe('Retain');
      expect(String((group.Properties as Record<string, unknown>).LogGroupName)).not.toMatch(
        /^\/aws\/bedrock-agentcore\/evaluations\//,
      );
    }
    const published = Object.fromEntries(resourcesOfType(template, 'AWS::SSM::Parameter')
      .map((parameter) => [parameter.Name, parameter.Value]));
    expect(published[paramPath('dev', 'evaluationV4ResultLogGroupName')]).toEqual({ Ref: expect.stringMatching(/^EvaluationResultsV4/) });
  });
  it('owns no result group unless online evaluation is provisioned', () => {
    const synthesize = (onlineEvaluationProvisioned: boolean): string[] => {
      const config = configWith({
        transactionSearchEnabled: true,
        evaluationInput: { provisioned: true },
        onlineEvaluation: { provisioned: onlineEvaluationProvisioned, samplingPercentage: 10, sessionTimeoutMinutes: 1 },
      });
      const stack = new EvaluationEvidenceStack(new cdk.App(), 'EvaluationEvidenceFamilies', {
        config,
        env: { account: config.account, region: config.region },
      });
      return resourcesOfType(Template.fromStack(stack).toJSON(), 'AWS::Logs::LogGroup')
        .map((group) => String(group.LogGroupName))
        .filter((name) => name.includes('/evaluation/results-'));
    };
    expect(synthesize(false)).toEqual([]);
    expect(synthesize(true)).toEqual([evaluationResultLogGroupName('dev', 'v4')]);
  });
});
