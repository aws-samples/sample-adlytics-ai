// SPDX-License-Identifier: MIT-0
import * as cdk from 'aws-cdk-lib';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';

import {
  agentQualityProcessorFunctionName,
  agentQualityProcessorRoleName,
  AppConfig,
  paramPath,
} from '../config';
import { boundedStreamMapping, staffedAlarmAction } from '../constructs/observability/async-failure';
import { grantOwnLogWrites, lambdaRole } from '../mixins/logging';
import { applyFunctionErrorAlarms } from '../mixins/function-error-alarms';
import { applyRemovalPolicy, applyStandardTags } from '../mixins/tagging';
import { grantSpanExport } from '../telemetry';

export interface AgentQualityProcessorStackProps extends cdk.StackProps {
  readonly config: AppConfig;
  readonly sourceAssetPath: string;
}

/** Destroyable consumer of retained audit records; it receives only DynamoDB KEYS_ONLY events. */
export class AgentQualityProcessorStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: AgentQualityProcessorStackProps) {
    super(scope, id, props);
    const { config } = props;
    const read = (key: Parameters<typeof paramPath>[1]): string =>
      ssm.StringParameter.valueForStringParameter(this, paramPath(config.envName, key));
    const auditTableArn = read('agentQualityAuditTableArn');
    const findingsTableArn = read('agentQualityFindingsTableArn');
    const aggregateTableArn = read('agentQualityAggregateTableArn');
    const streamArn = read('agentQualityAuditStreamArn');
    const dependencies = lambda.LayerVersion.fromLayerVersionArn(
      this,
      'PythonDependencies',
      read('dependenciesLayerArn'),
    );
    const functionLogs = new logs.LogGroup(this, 'ProcessorLogs', {
      logGroupName: `/aws/lambda/${agentQualityProcessorFunctionName(config.envName)}`,
      retention: config.logRetentionDays,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    // ADR-080 §1: the findings table's resource policy will name this role as its only writer.
    const role = lambdaRole(this, 'ProcessorRole', {
      roleName: agentQualityProcessorRoleName(config.envName),
    });
    grantOwnLogWrites(role, functionLogs);
    const processor = new lambda.Function(this, 'Processor', {
      functionName: agentQualityProcessorFunctionName(config.envName),
      role,
      runtime: new lambda.Runtime(config.pythonRuntime, lambda.RuntimeFamily.PYTHON),
      architecture: this.lambdaArchitecture(config),
      code: lambda.Code.fromAsset(props.sourceAssetPath),
      handler: 'adapters.jobs.agent_quality_processor.handler.handler',
      layers: [dependencies],
      environment: {
        ADL_ENVIRONMENT: config.envName,
        ADL_AWS_REGION: config.region,
        ADL_TRACING_ENABLED: String(config.transactionSearchEnabled),
        ADL_TRACE_SAMPLE_RATIO: String(config.traceSampleRatio),
        ADL_AGENT_QUALITY_AUDIT_TABLE: read('agentQualityAuditTableName'),
        ADL_AGENT_QUALITY_FINDINGS_TABLE: read('agentQualityFindingsTableName'),
        ADL_AGENT_QUALITY_AGGREGATE_TABLE: read('agentQualityAggregateTableName'),
        // Its findings expire with the receipt they derive from (ADR-081 §5).
        ADL_RECEIPT_RETENTION_DAYS: String(config.receiptRetentionDays),
      },
      // Record attestation and conditional writes are short; 512 MiB protects cold-start latency.
      memorySize: 512,
      timeout: cdk.Duration.seconds(30),
      reservedConcurrentExecutions: 2,
      logGroup: functionLogs,
    });
    grantSpanExport(processor);
    processor.addToRolePolicy(new iam.PolicyStatement({
      sid: 'ReadAuditReceiptAndStream',
      actions: ['dynamodb:GetItem', 'dynamodb:DescribeStream', 'dynamodb:GetRecords', 'dynamodb:GetShardIterator', 'dynamodb:ListStreams'],
      resources: [auditTableArn, streamArn],
    }));
    // GetItem reads back a replayed conditional write so it is classified as exact (ADR-080 §3).
    processor.addToRolePolicy(new iam.PolicyStatement({
      sid: 'WriteImmutableAuditFindings',
      actions: ['dynamodb:PutItem', 'dynamodb:GetItem'],
      resources: [findingsTableArn],
    }));
    processor.addToRolePolicy(new iam.PolicyStatement({
      sid: 'WriteImmutableAggregateMeasurements',
      actions: ['dynamodb:PutItem', 'dynamodb:GetItem'],
      resources: [aggregateTableArn],
    }));
    const staffedAction = staffedAlarmAction(this, config);
    boundedStreamMapping(this, 'AuditKeysOnlyMapping', {
      fn: processor,
      eventSourceArn: streamArn,
      startingPosition: 'LATEST',
      batchSize: 10,
      parallelizationFactor: 1,
      functionResponseTypes: ['ReportBatchItemFailures'],
      // ADR-080 §1 step 2: only a new item is work. The legacy backfill's updates and TTL's
      // deletions reach the stream too, and a receipt read for one of them finds nothing and
      // retries. Filtered-out events are dropped before invocation and never retried.
      filterCriteria: {
        filters: [{ pattern: JSON.stringify({ eventName: ['INSERT'] }) }],
      },
      bounds: config.asyncFailureBounds,
      failureQueue: {
        id: 'AuditStreamFailureQueue',
        queueName: `adlytics-${config.envName}-agent-quality-audit-stream-failures`,
      },
      alarm: {
        id: 'AuditStreamFailureAlarm',
        alarmName: `adlytics-${config.envName}-agent-quality-audit-stream-exhausted`,
        alarmDescription:
          'An audit-stream batch exhausted its bounded processor retries; its receipts have no '
          + 'findings or aggregate measurements. Disposition: attempts_failures status '
          + '--consumer agent-quality-audit, then attempts_failures drain --consumer '
          + 'agent-quality-audit (operator-tools.md).',
        action: staffedAction,
      },
    });
    applyFunctionErrorAlarms(this, config, staffedAction);
    applyRemovalPolicy(this, cdk.RemovalPolicy.DESTROY);
    applyStandardTags(this, config);
  }

  private lambdaArchitecture(config: AppConfig): lambda.Architecture {
    return { arm64: lambda.Architecture.ARM_64 }[config.lambdaArchitecture];
  }
}
