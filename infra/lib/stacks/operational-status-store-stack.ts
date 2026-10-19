// SPDX-License-Identifier: MIT-0
import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';

import {
  AppConfig,
  operationalStatusProjectionTableName,
  paramPath,
} from '../config';
import { applyRemovalPolicy, applyStandardTags } from '../mixins/tagging';

export interface OperationalStatusStoreStackProps extends cdk.StackProps {
  readonly config: AppConfig;
}

/** Retained content-free operational projection, isolated from disposable processors. */
export class OperationalStatusStoreStack extends cdk.Stack {
  public readonly projectionTable: dynamodb.Table;

  constructor(scope: Construct, id: string, props: OperationalStatusStoreStackProps) {
    super(scope, id, props);
    const { config } = props;
    if (!config.operationalStatus.provisioned) {
      throw new Error('Operational status store requires operational status provisioning.');
    }

    this.projectionTable = new dynamodb.Table(this, 'Projection', {
      tableName: operationalStatusProjectionTableName(config.envName),
      partitionKey: { name: 'partition_key', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'sort_key', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      timeToLiveAttribute: 'expires_at',
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });
    this.projectionTable.addGlobalSecondaryIndex({
      indexName: 'applied-event',
      partitionKey: { name: 'applied_flow_id', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'applied_sequence', type: dynamodb.AttributeType.NUMBER },
      projectionType: dynamodb.ProjectionType.ALL,
    });
    this.projectionTable.addGlobalSecondaryIndex({
      indexName: 'pending',
      partitionKey: { name: 'pending_state', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'pending_order', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });
    this.projectionTable.addGlobalSecondaryIndex({
      indexName: 'surface-sample-baseline',
      partitionKey: { name: 'surface_partition', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'surface_sort', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    for (const [logicalId, key, value, description] of [
      [
        'ProjectionTableNameParameter',
        'operationalStatusProjectionTableName',
        this.projectionTable.tableName,
        'Retained operational data-flow status projection table name.',
      ],
      [
        'ProjectionTableArnParameter',
        'operationalStatusProjectionTableArn',
        this.projectionTable.tableArn,
        'Retained operational data-flow status projection table ARN.',
      ],
    ] as const) {
      new ssm.StringParameter(this, logicalId, {
        parameterName: paramPath(config.envName, key),
        stringValue: value,
        description,
      });
    }

    applyRemovalPolicy(this, cdk.RemovalPolicy.RETAIN);
    applyStandardTags(this, config);
  }
}
