// SPDX-License-Identifier: MIT-0
import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';

import {
  agentQualityAggregateTableName,
  agentQualityAuditTableName,
  agentQualityFindingsTableName,
  AppConfig,
  paramPath,
  reviewerAccessLogGroupName,
  reviewerAccessLogRetention,
} from '../config';
import { recordTable, recordTablePolicy } from '../constructs/agent-quality/record-integrity';
import { applyRemovalPolicy, applyStandardTags } from '../mixins/tagging';

export interface AgentQualityStackProps extends cdk.StackProps {
  readonly config: AppConfig;
}

/**
 * Retained minimized audit receipts, findings, aggregate-ready measurements, and the reviewer
 * access log.
 */
export class AgentQualityStack extends cdk.Stack {
  public readonly auditTable: dynamodb.Table;
  public readonly findingsTable: dynamodb.Table;
  public readonly aggregateTable: dynamodb.Table;
  public readonly reviewerAccessLog: logs.LogGroup;

  constructor(scope: Construct, id: string, props: AgentQualityStackProps) {
    super(scope, id, props);
    const { config } = props;

    this.auditTable = new dynamodb.Table(this, 'Audit', {
      tableName: agentQualityAuditTableName(config.envName),
      partitionKey: { name: 'audit_id', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'item_key', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      // ADR-080 §1: the one control that also stops the CloudFormation execution role the policy
      // exempts from its DeleteTable deny, and a principal that first deletes the policy.
      deletionProtection: true,
      // ADR-081 §5: every record expires at its receipt's creation time plus the declared
      // retention. The writer stamps whole epoch seconds; readers treat a passed expiry as absent.
      timeToLiveAttribute: 'expires_at',
      stream: dynamodb.StreamViewType.KEYS_ONLY,
      // ADR-080 §2: only the conversation function puts, nothing mutates or copies, and only this
      // stack's deployment changes the table, and only the named readers read it.
      resourcePolicy: recordTablePolicy(this, recordTable(config.envName, 'receipts')),
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });
    this.auditTable.addGlobalSecondaryIndex({
      indexName: 'correlation-audit',
      partitionKey: { name: 'correlation_id', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'correlation_audit_id', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.KEYS_ONLY,
    });
    // ADR-078: the `turns` route pages one UTC day of receipts newest first. The conversation
    // function stamps both keys on new receipts only, so an older receipt is simply not in the
    // index. The projection carries exactly a row's fields; the principal is never projected.
    // DynamoDB accepts one GSI creation per table update, so no other index change joins this.
    this.auditTable.addGlobalSecondaryIndex({
      indexName: 'turns-by-day',
      partitionKey: { name: 'turns_day', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'turns_created_at', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.INCLUDE,
      nonKeyAttributes: ['correlation_id', 'terminal_outcome', 'expires_at'],
    });

    // ADR-080 §1: findings leave the receipts table for their own, whose only writer is the
    // agent-quality processor. Same key schema, so readers change table, not query. The KEYS_ONLY
    // stream feeds RecordIntegrityStack's item-change pipe (ADR-080 §4).
    this.findingsTable = new dynamodb.Table(this, 'Findings', {
      tableName: agentQualityFindingsTableName(config.envName),
      partitionKey: { name: 'audit_id', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'item_key', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      deletionProtection: true,
      timeToLiveAttribute: 'expires_at',
      stream: dynamodb.StreamViewType.KEYS_ONLY,
      resourcePolicy: recordTablePolicy(this, recordTable(config.envName, 'findings')),
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    this.aggregateTable = new dynamodb.Table(this, 'Aggregate', {
      tableName: agentQualityAggregateTableName(config.envName),
      partitionKey: { name: 'metric_id', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // ADR-081 §4, observability §3.8: the HTTP API stage's access log, which records the reviewer on
    // the review routes. Retained here rather than beside the destroyable API, deletion protected
    // like the ADR-079 result groups, and kept past every receipt it attributes access to.
    this.reviewerAccessLog = new logs.LogGroup(this, 'ReviewerAccessLog', {
      logGroupName: reviewerAccessLogGroupName(config.envName),
      retention: reviewerAccessLogRetention(config),
      deletionProtectionEnabled: true,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    const auditStreamArn = this.auditTable.tableStreamArn;
    if (auditStreamArn === undefined) throw new Error('Agent Quality audit stream is required.');
    const findingsStreamArn = this.findingsTable.tableStreamArn;
    if (findingsStreamArn === undefined) throw new Error('Agent Quality findings stream is required.');
    const values = [
      ['ParamAuditTableName', 'agentQualityAuditTableName', this.auditTable.tableName, 'Retained content-minimized terminal audit receipts, delivery dispositions, and admissions.'],
      ['ParamAuditTableArn', 'agentQualityAuditTableArn', this.auditTable.tableArn, 'Audit receipt table ARN for least-privilege processor access.'],
      ['ParamAuditStreamArn', 'agentQualityAuditStreamArn', auditStreamArn, 'KEYS_ONLY audit stream ARN for the deterministic processor and the item-change tamper pipe.'],
      ['ParamFindingsTableName', 'agentQualityFindingsTableName', this.findingsTable.tableName, 'Retained immutable structural findings; the processor is the only writer.'],
      ['ParamFindingsTableArn', 'agentQualityFindingsTableArn', this.findingsTable.tableArn, 'Findings table ARN for the processor writer and the findings reviewer.'],
      ['ParamFindingsStreamArn', 'agentQualityFindingsStreamArn', findingsStreamArn, 'KEYS_ONLY findings stream ARN for the item-change tamper pipe.'],
      ['ParamAggregateTableName', 'agentQualityAggregateTableName', this.aggregateTable.tableName, 'Retained immutable aggregate-ready Agent Quality measurements.'],
      ['ParamAggregateTableArn', 'agentQualityAggregateTableArn', this.aggregateTable.tableArn, 'Aggregate measurement table ARN for least-privilege processor access.'],
      ['ParamReviewerAccessLogGroupArn', 'agentQualityReviewerAccessLogGroupArn', this.reviewerAccessLog.logGroupArn, 'Retained reviewer access log group the HTTP API stage writes (ADR-081).'],
    ] as const;
    for (const [id, key, value, description] of values) {
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
