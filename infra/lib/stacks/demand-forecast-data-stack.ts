// SPDX-License-Identifier: MIT-0
import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';
import { type AppConfig, demandForecastTableName, paramPath } from '../config';
import { createDataBucket, demandForecastLifecycle } from '../mixins/storage';
import { applyRemovalPolicy, applyStandardTags } from '../mixins/tagging';

export interface DemandForecastDataStackProps extends cdk.StackProps {
  readonly config: AppConfig;
}

/**
 * The retained demand-forecast processing bucket and forecast table (approval I-1).
 *
 * The bucket holds each run's staged weekly input under `runs/<run_id>/input/<attempt>/` and each
 * shard attempt's candidate and roster part under `runs/<run_id>/shards/<k>/attempts/<attempt>/`.
 * Private, TLS-only, SSE-S3 and versioned, with every `runs/` object expiring (design §6a).
 *
 * The table holds the set's fenced `CURRENT` pointer, the immutable `RUN#` receipts, the weekly
 * slot markers and every generation's items (design §6b). On-demand billing, AWS-owned
 * encryption, point-in-time recovery, deletion protection and RETAIN are each declared, never left
 * to a service default (infra-composition-stacks). Both names are published to SSM for
 * `DemandForecastStack`.
 */
export class DemandForecastDataStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: DemandForecastDataStackProps) {
    super(scope, id, props);
    const { config } = props;
    const bucket = createDataBucket(this, 'ProcessingBucket', {
      config,
      purpose: 'demand-forecast',
      versioned: true,
      lifecycleRules: demandForecastLifecycle(config),
    });
    const table = new dynamodb.Table(this, 'ForecastTable', {
      tableName: demandForecastTableName(config.envName),
      partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'SK', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      encryption: dynamodb.TableEncryption.DEFAULT,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      deletionProtection: true,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });
    for (const [paramId, key, value, description] of [
      [
        'ParamDemandForecastTableName',
        'demandForecastTableName',
        table.tableName,
        'Retained demand-forecast table name.',
      ],
      [
        'ParamDemandForecastProcessingBucketName',
        'demandForecastProcessingBucketName',
        bucket.bucketName,
        'Retained demand-forecast processing bucket name.',
      ],
    ] as const) {
      new ssm.StringParameter(this, paramId, {
        parameterName: paramPath(config.envName, key),
        stringValue: value,
        description,
      });
    }
    applyRemovalPolicy(this, cdk.RemovalPolicy.RETAIN);
    applyStandardTags(this, config);
  }
}
