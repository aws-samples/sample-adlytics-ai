// SPDX-License-Identifier: MIT-0
import {
  DEPLOY_ORDER,
  insightCacheTableName,
  PARAMS,
  paramPath,
} from '../lib/config';
import { everyTemplate, resourcesOfType } from './support/templates';

const cacheTemplate = everyTemplate().InsightCacheStack as Record<string, unknown>;

function resources(): Record<string, Record<string, unknown>> {
  return cacheTemplate.Resources as Record<string, Record<string, unknown>>;
}

describe('InsightCacheStack', () => {
  it('retains the encrypted on-demand PITR table with superseded-entry TTL only', () => {
    expect(resourcesOfType(cacheTemplate, 'AWS::DynamoDB::Table')).toEqual([
      expect.objectContaining({
        TableName: insightCacheTableName('dev'),
        BillingMode: 'PAY_PER_REQUEST',
        SSESpecification: { SSEEnabled: true },
        PointInTimeRecoverySpecification: { PointInTimeRecoveryEnabled: true },
        TimeToLiveSpecification: { AttributeName: 'superseded_at', Enabled: true },
        KeySchema: [
          { AttributeName: 'ownership_key', KeyType: 'HASH' },
          { AttributeName: 'content_key', KeyType: 'RANGE' },
        ],
      }),
    ]);
    const [table] = Object.values(resources()).filter((resource) => resource.Type === 'AWS::DynamoDB::Table');
    expect(table?.DeletionPolicy).toBe('Retain');
    expect(table?.UpdateReplacePolicy).toBe('Retain');
  });

  it('publishes the preserved typed paths before API and precompute consumers', () => {
    expect(PARAMS.insightCacheTableName.producedBy).toBe('InsightCacheStack');
    expect(PARAMS.insightCacheTableArn.producedBy).toBe('InsightCacheStack');
    expect(DEPLOY_ORDER.indexOf('InsightCacheStack')).toBeLessThan(DEPLOY_ORDER.indexOf('ApiStack'));
    expect(DEPLOY_ORDER.indexOf('InsightCacheStack')).toBeLessThan(DEPLOY_ORDER.indexOf('InsightPrecomputeStack'));
    expect(resourcesOfType(cacheTemplate, 'AWS::SSM::Parameter')).toEqual(expect.arrayContaining([
      expect.objectContaining({ Name: paramPath('dev', 'insightCacheTableName') }),
      expect.objectContaining({ Name: paramPath('dev', 'insightCacheTableArn') }),
    ]));
  });
});
