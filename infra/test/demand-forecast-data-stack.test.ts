// SPDX-License-Identifier: MIT-0
/**
 * The retained demand-forecast data stack (design §6a, §6b; approval I-1).
 *
 * The table holds the pointer planners read and every published generation: a table left to a
 * service default for billing, encryption, recovery or retention, or one a stack delete could
 * remove, could lose what planners see. The SSM names are how DemandForecastStack finds both.
 */
import * as cdk from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import { paramPath } from '../lib/config';
import { DemandForecastDataStack } from '../lib/stacks/demand-forecast-data-stack';
import { TEST_CONFIG } from './support/config';

function synth(): Template {
  const app = new cdk.App();
  const stack = new DemandForecastDataStack(app, 'DemandForecastData', {
    config: TEST_CONFIG,
    env: { account: TEST_CONFIG.account, region: TEST_CONFIG.region },
  });
  return Template.fromStack(stack);
}

describe('DemandForecastDataStack', () => {
  const template = synth();

  test('the forecast table declares on-demand, AWS-owned encryption, PITR, deletion protection and RETAIN', () => {
    const tables = template.findResources('AWS::DynamoDB::Table');
    expect(Object.keys(tables)).toHaveLength(1);
    const [table] = Object.values(tables);
    expect(table?.DeletionPolicy).toBe('Retain');
    expect(table?.UpdateReplacePolicy).toBe('Retain');
    expect(table?.Properties).toEqual({
      TableName: 'adlytics-dev-demand-forecast',
      KeySchema: [
        { AttributeName: 'PK', KeyType: 'HASH' },
        { AttributeName: 'SK', KeyType: 'RANGE' },
      ],
      AttributeDefinitions: [
        { AttributeName: 'PK', AttributeType: 'S' },
        { AttributeName: 'SK', AttributeType: 'S' },
      ],
      BillingMode: 'PAY_PER_REQUEST',
      // AWS-owned encryption, declared (`TableEncryption.DEFAULT`), never left to the default.
      SSESpecification: { SSEEnabled: false },
      PointInTimeRecoverySpecification: { PointInTimeRecoveryEnabled: true },
      DeletionProtectionEnabled: true,
      Tags: expect.any(Array),
    });
  });

  test('the table and bucket names are published under their PARAMS paths', () => {
    template.hasResourceProperties('AWS::SSM::Parameter', {
      Name: paramPath('dev', 'demandForecastTableName'),
      Value: { Ref: Match.stringLikeRegexp('^ForecastTable') },
    });
    template.hasResourceProperties('AWS::SSM::Parameter', {
      Name: paramPath('dev', 'demandForecastProcessingBucketName'),
      Value: { Ref: Match.stringLikeRegexp('^ProcessingBucket') },
    });
  });

  test('the processing bucket is retained, versioned, private and expires runs/', () => {
    template.hasResource('AWS::S3::Bucket', {
      DeletionPolicy: 'Retain',
      Properties: Match.objectLike({
        BucketName: `adlytics-dev-demand-forecast-${TEST_CONFIG.account}`,
        VersioningConfiguration: { Status: 'Enabled' },
        PublicAccessBlockConfiguration: Match.objectLike({ BlockPublicAcls: true }),
        LifecycleConfiguration: {
          Rules: Match.arrayWith([
            Match.objectLike({
              Id: 'expire-forecast-runs',
              Prefix: 'runs/',
              ExpirationInDays: 90,
              NoncurrentVersionExpiration: { NoncurrentDays: 90 },
            }),
          ]),
        },
      }),
    });
  });
});
