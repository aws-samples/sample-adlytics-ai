// SPDX-License-Identifier: MIT-0
import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';

import { AppConfig, insightPromptRegistryTableName, paramPath } from '../config';
import { applyRemovalPolicy, applyStandardTags } from '../mixins/tagging';

export interface InsightPromptRegistryStackProps extends cdk.StackProps {
  readonly config: AppConfig;
}

/** Retained immutable prompt version, active-pointer, and activation-event authority. */
export class InsightPromptRegistryStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: InsightPromptRegistryStackProps) {
    super(scope, id, props);
    const { config } = props;
    const table = new dynamodb.Table(this, 'PromptRegistry', {
      tableName: insightPromptRegistryTableName(config.envName),
      partitionKey: { name: 'slot_key', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'record_key', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });
    for (const [id, key, value] of [
      ['ParamPromptRegistryTableName', 'insightPromptRegistryTableName', table.tableName],
      ['ParamPromptRegistryTableArn', 'insightPromptRegistryTableArn', table.tableArn],
    ] as const) {
      new ssm.StringParameter(this, id, {
        parameterName: paramPath(config.envName, key),
        stringValue: value,
        description: 'Retained insight prompt registry contract.',
      });
    }
    applyStandardTags(this, config);
    applyRemovalPolicy(this, cdk.RemovalPolicy.RETAIN);
  }
}
