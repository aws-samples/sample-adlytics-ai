// SPDX-License-Identifier: MIT-0
import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';

import { AppConfig, insightCacheTableName, paramPath } from '../config';
import { applyRemovalPolicy, applyStandardTags } from '../mixins/tagging';

export interface InsightCacheStackProps extends cdk.StackProps {
  readonly config: AppConfig;
}

/** Retained derived-insight cache; obsolete exact entries may expire only after supersession. */
export class InsightCacheStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: InsightCacheStackProps) {
    super(scope, id, props);
    const { config } = props;
    const table = new dynamodb.Table(this, 'InsightCache', {
      tableName: insightCacheTableName(config.envName),
      partitionKey: { name: 'ownership_key', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'content_key', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      timeToLiveAttribute: 'superseded_at',
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });
    for (const [id, key, value, description] of [
      ['ParamInsightCacheTableName', 'insightCacheTableName', table.tableName, 'Retained insight cache table name.'],
      ['ParamInsightCacheTableArn', 'insightCacheTableArn', table.tableArn, 'Retained insight cache table ARN for exact-key access.'],
    ] as const) {
      new ssm.StringParameter(this, id, {
        parameterName: paramPath(config.envName, key),
        stringValue: value,
        description,
      });
    }
    applyStandardTags(this, config);
    applyRemovalPolicy(this, cdk.RemovalPolicy.RETAIN);
  }
}
