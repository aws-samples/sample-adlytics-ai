// SPDX-License-Identifier: MIT-0
import * as cdk from 'aws-cdk-lib';
import * as logs from 'aws-cdk-lib/aws-logs';

import {
  agentQualityAggregateTableName,
  agentQualityAuditTableName,
  agentQualityFindingsTableName,
  paramPath,
  reviewerAccessLogGroupName,
  reviewerAccessLogRetention,
} from '../lib/config';
import { AgentQualityStack } from '../lib/stacks/agent-quality-stack';
import { configWith } from './support/config';
import { everyTemplate, resourcesOfType } from './support/templates';
import { expectBoundedStreamConsumer } from './support/async-failure';

const auditTemplate = everyTemplate().AgentQualityStack as Record<string, unknown>;
const processorTemplate = everyTemplate().AgentQualityProcessorStack as Record<string, unknown>;

describe('AgentQualityStack', () => {
  it('retains encrypted PITR receipt, findings, and aggregate tables', () => {
    const tables = resourcesOfType(auditTemplate, 'AWS::DynamoDB::Table');
    expect(tables).toEqual(expect.arrayContaining([
      expect.objectContaining({
        TableName: agentQualityAuditTableName('dev'),
        BillingMode: 'PAY_PER_REQUEST',
        SSESpecification: { SSEEnabled: true },
        PointInTimeRecoverySpecification: { PointInTimeRecoveryEnabled: true },
        StreamSpecification: { StreamViewType: 'KEYS_ONLY' },
        KeySchema: [
          { AttributeName: 'audit_id', KeyType: 'HASH' },
          { AttributeName: 'item_key', KeyType: 'RANGE' },
        ],
      }),
      expect.objectContaining({
        TableName: agentQualityFindingsTableName('dev'),
        BillingMode: 'PAY_PER_REQUEST',
        SSESpecification: { SSEEnabled: true },
        PointInTimeRecoverySpecification: { PointInTimeRecoveryEnabled: true },
        DeletionProtectionEnabled: true,
        StreamSpecification: { StreamViewType: 'KEYS_ONLY' },
      }),
      expect.objectContaining({
        TableName: agentQualityAggregateTableName('dev'),
        BillingMode: 'PAY_PER_REQUEST',
        SSESpecification: { SSEEnabled: true },
        PointInTimeRecoverySpecification: { PointInTimeRecoveryEnabled: true },
        KeySchema: [{ AttributeName: 'metric_id', KeyType: 'HASH' }],
      }),
    ]));
    // ADR-081 §5: the receipts and findings tables expire records on `expires_at`, in epoch
    // seconds. The aggregate table holds derived measurements, not a turn's records, and keeps
    // no expiry.
    const ttl = Object.fromEntries(
      tables.map((table) => [String(table.TableName), table.TimeToLiveSpecification]),
    );
    expect(ttl).toStrictEqual({
      [agentQualityAuditTableName('dev')]: { AttributeName: 'expires_at', Enabled: true },
      [agentQualityFindingsTableName('dev')]: { AttributeName: 'expires_at', Enabled: true },
      [agentQualityAggregateTableName('dev')]: undefined,
    });
    const resources = Object.values((auditTemplate.Resources ?? {}) as Record<string, Record<string, unknown>>);
    for (const table of resources.filter((resource) => resource.Type === 'AWS::DynamoDB::Table')) {
      expect(table.DeletionPolicy).toBe('Retain');
      expect(table.UpdateReplacePolicy).toBe('Retain');
    }
  });

  it('declares only the correlation and turns-by-day indexes and typed parameters', () => {
    const [audit] = resourcesOfType(auditTemplate, 'AWS::DynamoDB::Table')
      .filter((table) => table.TableName === agentQualityAuditTableName('dev'));
    expect(audit?.GlobalSecondaryIndexes).toStrictEqual([
      {
        IndexName: 'correlation-audit',
        KeySchema: [
          { AttributeName: 'correlation_id', KeyType: 'HASH' },
          { AttributeName: 'correlation_audit_id', KeyType: 'RANGE' },
        ],
        Projection: { ProjectionType: 'KEYS_ONLY' },
      },
      // ADR-078: the `turns` page. Exactly a row's fields; the principal is never projected.
      {
        IndexName: 'turns-by-day',
        KeySchema: [
          { AttributeName: 'turns_day', KeyType: 'HASH' },
          { AttributeName: 'turns_created_at', KeyType: 'RANGE' },
        ],
        Projection: {
          ProjectionType: 'INCLUDE',
          NonKeyAttributes: ['correlation_id', 'terminal_outcome', 'expires_at'],
        },
      },
    ]);
    expect(JSON.stringify(audit?.GlobalSecondaryIndexes)).not.toMatch(/principal|subject/);
    const parameters = resourcesOfType(auditTemplate, 'AWS::SSM::Parameter');
    expect(parameters).toEqual(expect.arrayContaining([
      expect.objectContaining({ Name: paramPath('dev', 'agentQualityAuditTableName') }),
      expect.objectContaining({ Name: paramPath('dev', 'agentQualityAuditStreamArn') }),
      expect.objectContaining({ Name: paramPath('dev', 'agentQualityFindingsTableName') }),
      expect.objectContaining({ Name: paramPath('dev', 'agentQualityFindingsTableArn') }),
      expect.objectContaining({ Name: paramPath('dev', 'agentQualityAggregateTableName') }),
    ]));
  });
});

describe('the reviewer access log group (ADR-081 §4)', () => {
  const synth = (receiptRetentionDays: number, receiptTtlLagMarginDays = 7) => {
    const config = configWith({ receiptRetentionDays, receiptTtlLagMarginDays });
    return new AgentQualityStack(new cdk.App(), 'AgentQualityRetention', {
      config,
      env: { account: config.account, region: config.region },
    });
  };

  it('is retained, deletion protected, and outlives receipts plus the TTL lag', () => {
    const resources = Object.values(
      (auditTemplate.Resources ?? {}) as Record<string, Record<string, unknown>>,
    ).filter((resource) => resource.Type === 'AWS::Logs::LogGroup');
    expect(resources).toHaveLength(1);
    // 90 receipt days plus a 7-day margin is 97, and CloudWatch's next value up is 120.
    expect(resources[0]).toEqual(expect.objectContaining({
      DeletionPolicy: 'Retain',
      UpdateReplacePolicy: 'Retain',
      Properties: expect.objectContaining({
        LogGroupName: reviewerAccessLogGroupName('dev'),
        RetentionInDays: 120,
        DeletionProtectionEnabled: true,
      }),
    }));
    expect(resourcesOfType(auditTemplate, 'AWS::SSM::Parameter')).toContainEqual(
      expect.objectContaining({ Name: paramPath('dev', 'agentQualityReviewerAccessLogGroupArn') }),
    );
  });

  it('rounds up to the smallest CloudWatch value that covers the floor, exact values included', () => {
    expect(reviewerAccessLogRetention({ receiptRetentionDays: 83, receiptTtlLagMarginDays: 7 }))
      .toBe(logs.RetentionDays.THREE_MONTHS);
    expect(reviewerAccessLogRetention({ receiptRetentionDays: 84, receiptTtlLagMarginDays: 7 }))
      .toBe(logs.RetentionDays.FOUR_MONTHS);
    expect(reviewerAccessLogRetention({ receiptRetentionDays: 3646, receiptTtlLagMarginDays: 7 }))
      .toBe(logs.RetentionDays.TEN_YEARS);
  });

  it('fails synth when no CloudWatch retention covers receipts plus the TTL lag', () => {
    // 3647 + 7 = 3654, one past ten years, the longest finite retention. Never `INFINITE`.
    expect(() => synth(3647)).toThrow(/No CloudWatch retention covers 3654 days/);
    expect(() => synth(90, -1)).toThrow(/whole-day receipt retention and TTL-lag margin/);
    expect(() => synth(90, 0.5)).toThrow(/whole-day receipt retention and TTL-lag margin/);
  });
});

describe('AgentQualityProcessorStack', () => {
  it('uses a partial-failure KEYS_ONLY stream mapping and least-privilege table access', () => {
    const mappings = resourcesOfType(processorTemplate, 'AWS::Lambda::EventSourceMapping');
    expect(mappings).toContainEqual(expect.objectContaining({
      StartingPosition: 'LATEST',
      BatchSize: 10,
      FunctionResponseTypes: ['ReportBatchItemFailures'],
    }));
    const functions = resourcesOfType(processorTemplate, 'AWS::Lambda::Function');
    expect(functions).toContainEqual(expect.objectContaining({
      Handler: 'adapters.jobs.agent_quality_processor.handler.handler',
      MemorySize: 512,
      Timeout: 30,
      Architectures: ['arm64'],
    }));
    expect(functions[0]?.Environment).toEqual({
      Variables: expect.objectContaining({ ADL_RECEIPT_RETENTION_DAYS: '90' }),
    });
    const policy = JSON.stringify(processorTemplate);
    expect(policy).toContain('dynamodb:GetItem');
    expect(policy).toContain('dynamodb:PutItem');
    expect(policy).not.toContain('dynamodb:Scan');
  });

  it('admits only INSERT events from the receipts stream (ADR-080 §1 step 2)', () => {
    // The backfill's updates and TTL's deletions reach the same stream. Unfiltered, each one
    // invokes the processor, which reads a receipt for it and retries when it finds none.
    const mappings = resourcesOfType(processorTemplate, 'AWS::Lambda::EventSourceMapping');
    expect(mappings).toHaveLength(1);
    expect(mappings[0]?.FilterCriteria).toStrictEqual({
      Filters: [{ Pattern: JSON.stringify({ eventName: ['INSERT'] }) }],
    });
  });
});

describe('AgentQualityProcessorStack failure path (ADR-133)', () => {
  it('bounds audit-stream retries and sends an exhausted batch to an alarmed failure queue', () => {
    expectBoundedStreamConsumer(processorTemplate, {
      mappingId: /^AuditKeysOnlyMapping$/,
      queueName: 'adlytics-dev-agent-quality-audit-stream-failures',
      alarmName: 'adlytics-dev-agent-quality-audit-stream-exhausted',
      alarmDescription:
        'An audit-stream batch exhausted its bounded processor retries; its receipts have no '
        + 'findings or aggregate measurements. Disposition: attempts_failures status '
        + '--consumer agent-quality-audit, then attempts_failures drain --consumer '
        + 'agent-quality-audit (operator-tools.md).',
      roleId: /^ProcessorRole[0-9A-F]{8}$/,
      mapping: {
        BatchSize: 10,
        EventSourceArn: {
          Ref: expect.stringMatching(/^SsmParameterValueadlyticsdevagentqualityauditstreamarn/),
        },
        FilterCriteria: { Filters: [{ Pattern: JSON.stringify({ eventName: ['INSERT'] }) }] },
        FunctionName: { Ref: expect.stringMatching(/^Processor[0-9A-F]{8}$/) },
        FunctionResponseTypes: ['ReportBatchItemFailures'],
        ParallelizationFactor: 1,
        StartingPosition: 'LATEST',
      },
    });
  });
});
