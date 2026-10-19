// SPDX-License-Identifier: MIT-0
import * as cdk from 'aws-cdk-lib';
import * as events from 'aws-cdk-lib/aws-events';
import * as targets from 'aws-cdk-lib/aws-events-targets';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';

import {
  AppConfig,
  conversationFunctionTimeoutSeconds,
  EVALUATION_FAMILIES,
  evaluationReleaseParameterName,
  evaluationOutboxDispatcherFunctionName,
  evaluationOutboxDispatcherRoleName,
  paramPath,
} from '../config';
import { grantOwnLogWrites, lambdaRole } from '../mixins/logging';
import { staffedAlarmAction } from '../constructs/observability/async-failure';
import { applyFunctionErrorAlarms } from '../mixins/function-error-alarms';
import { applyRemovalPolicy, applyStandardTags } from '../mixins/tagging';
import { grantSpanExport, tracingEnvironment } from '../telemetry';

export interface EvaluationOutboxDispatchStackProps extends cdk.StackProps {
  readonly config: AppConfig;
  readonly sourceAssetPath: string;
}

/** Disposable scheduled compute that drains the retained evaluation outbox. */
export class EvaluationOutboxDispatchStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: EvaluationOutboxDispatchStackProps) {
    super(scope, id, props);
    const { config } = props;
    if (!config.evaluationInput.provisioned) {
      throw new Error('Evaluation outbox dispatch requires evaluation input provisioning.');
    }
    const read = (key: Parameters<typeof paramPath>[1]): string =>
      ssm.StringParameter.valueForStringParameter(this, paramPath(config.envName, key));
    const outboxTable = read('evaluationOutboxTableName');
    const outboxTableArn = read('evaluationOutboxTableArn');
    const deliveriesTable = read('evaluationDeliveriesTableName');
    const deliveriesTableArn = read('evaluationDeliveriesTableArn');
    const queueUrl = read('evaluationInputQueueUrl');
    const queueArn = read('evaluationInputQueueArn');
    const auditTable = read('agentQualityAuditTableName');
    const auditTableArn = read('agentQualityAuditTableArn');
    const releaseParameterNames = EVALUATION_FAMILIES.map((family) =>
      evaluationReleaseParameterName(config.envName, family));
    const dependencies = lambda.LayerVersion.fromLayerVersionArn(
      this,
      'PythonDependencies',
      read('dependenciesLayerArn'),
    );
    const functionName = evaluationOutboxDispatcherFunctionName(config.envName);
    const functionLogs = new logs.LogGroup(this, 'DispatcherLogs', {
      logGroupName: `/aws/lambda/${functionName}`,
      retention: config.logRetentionDays,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    // ADR-080 §1: the deliveries table's resource policy will name this role as its only writer.
    const role = lambdaRole(this, 'DispatcherRole', {
      roleName: evaluationOutboxDispatcherRoleName(config.envName),
    });
    grantOwnLogWrites(role, functionLogs);
    const dispatcher = new lambda.Function(this, 'Dispatcher', {
      functionName,
      role,
      runtime: new lambda.Runtime(config.pythonRuntime, lambda.RuntimeFamily.PYTHON),
      architecture: this.lambdaArchitecture(config),
      code: lambda.Code.fromAsset(props.sourceAssetPath),
      handler: 'adapters.jobs.evaluation_outbox_dispatcher.handler.handler',
      layers: [dependencies],
      environment: {
        ...tracingEnvironment(config),
        ADL_EVALUATION_OUTBOX_TABLE: outboxTable,
        ADL_EVALUATION_DELIVERIES_TABLE: deliveriesTable,
        ADL_EVALUATION_QUEUE_URL: queueUrl,
        ADL_EVALUATION_OUTBOX_RETENTION_DAYS: String(config.logRetentionDays),
        // The delivery receipt expires with its turn's receipt (ADR-081 §5).
        ADL_RECEIPT_RETENTION_DAYS: String(config.receiptRetentionDays),
        // ADR-079: the dispatcher is the switch. Every current family always: a missing record
        // reads as not enabled, so an unprovisioned family is simply never dispatched to.
        ADL_EVALUATION_RELEASE_PARAMETERS: releaseParameterNames.join(','),
        // The ADR-063 delivery companion decides whether the user received the answer.
        ADL_AGENT_QUALITY_AUDIT_TABLE: auditTable,
        // How long an absent companion is waited for: the route's own timeout plus a margin.
        ADL_EVALUATION_DELIVERY_WAIT_SECONDS: String(conversationFunctionTimeoutSeconds(config) + 120),
      },
      // The function performs bounded DynamoDB calls and one SQS send per item.
      memorySize: 512,
      timeout: cdk.Duration.seconds(30),
      reservedConcurrentExecutions: 2,
      logGroup: functionLogs,
    });
    grantSpanExport(dispatcher);
    dispatcher.addToRolePolicy(new iam.PolicyStatement({
      actions: ['dynamodb:Query'],
      resources: [`${outboxTableArn}/index/dispatch-ready`],
    }));
    // GetItem on the deliveries table reads back a replayed delivery receipt (ADR-080 §3).
    dispatcher.addToRolePolicy(new iam.PolicyStatement({
      actions: ['dynamodb:GetItem'],
      resources: [outboxTableArn, deliveriesTableArn],
    }));
    dispatcher.addToRolePolicy(new iam.PolicyStatement({
      actions: ['dynamodb:UpdateItem'],
      resources: [outboxTableArn],
    }));
    dispatcher.addToRolePolicy(new iam.PolicyStatement({
      actions: ['dynamodb:PutItem'],
      resources: [deliveriesTableArn],
    }));
    dispatcher.addToRolePolicy(new iam.PolicyStatement({
      actions: ['dynamodb:TransactWriteItems'],
      resources: [outboxTableArn, deliveriesTableArn],
    }));
    dispatcher.addToRolePolicy(new iam.PolicyStatement({
      actions: ['sqs:SendMessage'],
      resources: [queueArn],
    }));
    // One GetParameters call per run, on the exact release records and nothing else.
    dispatcher.addToRolePolicy(new iam.PolicyStatement({
      sid: 'ReadReleaseRecords',
      actions: ['ssm:GetParameters'],
      resources: releaseParameterNames.map((name) => this.formatArn({
        service: 'ssm',
        resource: 'parameter',
        resourceName: name.slice(1),
      })),
    }));
    // The turn's ADR-063 delivery companion, read by its exact key.
    dispatcher.addToRolePolicy(new iam.PolicyStatement({
      sid: 'ReadDeliveryDisposition',
      actions: ['dynamodb:GetItem'],
      resources: [auditTableArn],
    }));
    new events.Rule(this, 'Schedule', {
      schedule: events.Schedule.rate(cdk.Duration.minutes(1)),
      targets: [new targets.LambdaFunction(dispatcher)],
    });

    applyFunctionErrorAlarms(this, config, staffedAlarmAction(this, config));
    applyRemovalPolicy(this, cdk.RemovalPolicy.DESTROY);
    applyStandardTags(this, config);
  }

  private lambdaArchitecture(config: AppConfig): lambda.Architecture {
    return { arm64: lambda.Architecture.ARM_64 }[config.lambdaArchitecture];
  }
}
