// SPDX-License-Identifier: MIT-0
import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';

import { AppConfig, paramPath } from '../config';
import { createDataBucket, publisherArtifactLifecycle } from '../mixins/storage';
import { applyRemovalPolicy, applyStandardTags } from '../mixins/tagging';

export interface PublisherArtifactStoreStackProps extends cdk.StackProps {
  readonly config: AppConfig;
}

/** Retained immutable preset-artifact bodies and forward-only page/variant pointers. */
export class PublisherArtifactStoreStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: PublisherArtifactStoreStackProps) {
    super(scope, id, props);
    const { config } = props;

    const artifacts = createDataBucket(this, 'Artifacts', {
      config,
      purpose: 'preset-artifacts',
      versioned: true,
      lifecycleRules: publisherArtifactLifecycle(config),
    });
    const pointers = new dynamodb.Table(this, 'Pointers', {
      tableName: `adlytics-${config.envName}-preset-artifact-pointers`,
      partitionKey: { name: 'page_variant_key', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'record_key', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      timeToLiveAttribute: 'expires_at',
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    for (const [id, key, value, description] of [
      [
        'ArtifactBucketNameParameter',
        'publisherArtifactBucketName',
        artifacts.bucketName,
        'Retained S3 bucket holding immutable capability-free preset artifact bodies.',
      ],
      [
        'PointerTableNameParameter',
        'publisherArtifactPointerTableName',
        pointers.tableName,
        'Retained DynamoDB table holding forward-only preset artifact pointers.',
      ],
      [
        'PointerTableArnParameter',
        'publisherArtifactPointerTableArn',
        pointers.tableArn,
        'Retained preset artifact pointer-table ARN for least-privilege consumers.',
      ],
    ] as const) {
      new ssm.StringParameter(this, id, {
        parameterName: paramPath(config.envName, key),
        stringValue: value,
        description,
      });
    }

    applyRemovalPolicy(this, cdk.RemovalPolicy.RETAIN);
    applyStandardTags(this, config);
  }
}
