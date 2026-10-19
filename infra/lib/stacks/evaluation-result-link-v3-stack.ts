// SPDX-License-Identifier: MIT-0
import * as cdk from 'aws-cdk-lib';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';

import {
  AppConfig,
  EvaluationFamily,
  evaluationResultIngressRoleName,
  evaluationResultIngressV3FunctionName,
  evaluationResultLinkerRoleName,
  evaluationResultLinkerV3FunctionName,
  evaluationResultProjectionV3DeadLetterQueueName,
  evaluationResultProjectionV3QueueName,
  evaluationResultSubscriptionName,
  paramPath,
} from '../config';
import { onlyProducerSends } from '../constructs/agent-quality/record-integrity';
import { arrivalAlarm, staffedAlarmAction } from '../constructs/observability/async-failure';
import { grantOwnLogWrites, lambdaRole } from '../mixins/logging';
import { applyFunctionErrorAlarms } from '../mixins/function-error-alarms';
import { applyRemovalPolicy, applyStandardTags } from '../mixins/tagging';
import { grantSpanExport } from '../telemetry';

export interface EvaluationResultLinkV3StackProps extends cdk.StackProps {
  readonly config: AppConfig;
  readonly sourceAssetPath: string;
}

/** Event-driven protected V3 result ingress with a content-free durable projection boundary. */
export class EvaluationResultLinkV3Stack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: EvaluationResultLinkV3StackProps) {
    super(scope, id, props);
    const { config } = props;
    if (!config.onlineEvaluation.provisioned) {
      throw new Error('EvaluationResultLinkV3Stack requires onlineEvaluation.provisioned.');
    }
    const read = (key: Parameters<typeof paramPath>[1]): string =>
      ssm.StringParameter.valueForStringParameter(this, paramPath(config.envName, key));
    const auditTableArn = read('agentQualityAuditTableArn');
    const writerReceiptsTableArn = read('evaluationWriterReceiptsTableArn');
    const verdictsTableArn = read('evaluationVerdictsTableArn');
    const deliveriesTableArn = read('evaluationDeliveriesTableArn');
    // One source per current family: its stack-owned result group (ADR-079). A successor family
    // writes to its own group, so admitting it is another subscription to the same ingress rather
    // than a second ingress. Which configuration and evaluator a result may come from is decided
    // per turn by its delivery receipt, never by a deploy-time list. The stack and its functions
    // keep V3 in their names from before the V3 family's retirement; they serve every family.
    const sources: { readonly family: EvaluationFamily; readonly logGroupName: string }[] = [
      { family: 'v4', logGroupName: read('evaluationV4ResultLogGroupName') },
    ];
    const logGroupArn = (logGroupName: string): string =>
      cdk.Stack.of(this).formatArn({
        service: 'logs',
        resource: 'log-group',
        resourceName: `${logGroupName}:*`,
        arnFormat: cdk.ArnFormat.COLON_RESOURCE_NAME,
      });
    const dependencies = lambda.LayerVersion.fromLayerVersionArn(
      this,
      'PythonDependencies',
      read('dependenciesLayerArn'),
    );

    const deadLetterQueue = new sqs.Queue(this, 'ProjectionDeadLetterQueueV3', {
      queueName: evaluationResultProjectionV3DeadLetterQueueName(config.envName),
      encryption: sqs.QueueEncryption.SQS_MANAGED,
      enforceSSL: true,
      retentionPeriod: cdk.Duration.days(14),
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    const projectionQueue = new sqs.Queue(this, 'ProjectionQueueV3', {
      queueName: evaluationResultProjectionV3QueueName(config.envName),
      encryption: sqs.QueueEncryption.SQS_MANAGED,
      enforceSSL: true,
      retentionPeriod: cdk.Duration.days(4),
      visibilityTimeout: cdk.Duration.seconds(120),
      deadLetterQueue: { queue: deadLetterQueue, maxReceiveCount: 8 },
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    // ADR-080 §3: only the ingress tells the linker what verdict to write.
    onlyProducerSends(projectionQueue, evaluationResultIngressRoleName(config.envName));
    const staffedAction = staffedAlarmAction(this, config);
    arrivalAlarm(this, 'ProjectionDeadLetterQueueAlarmV3', {
      alarmName: `adlytics-${config.envName}-evaluation-result-projection-dead-lettered`,
      alarmDescription:
        'An evaluation result exhausted its eight linker receives; its verdict was not written. '
        + 'Inspect the linker failure before redrive.',
      queue: deadLetterQueue,
      action: staffedAction,
    });
    const ingressLogs = new logs.LogGroup(this, 'IngressLogsV3', {
      logGroupName: `/aws/lambda/${evaluationResultIngressV3FunctionName(config.envName)}`,
      retention: config.logRetentionDays,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    const persisterLogs = new logs.LogGroup(this, 'LinkerLogsV3', {
      logGroupName: `/aws/lambda/${evaluationResultLinkerV3FunctionName(config.envName)}`,
      retention: config.logRetentionDays,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    // ADR-080 §1: the record tables' resource policies will name these roles, the ingress as a
    // receipts reader and the linker as the verdicts table's only writer.
    const ingressRole = lambdaRole(this, 'IngressRoleV3', {
      roleName: evaluationResultIngressRoleName(config.envName),
    });
    grantOwnLogWrites(ingressRole, ingressLogs);
    const persisterRole = lambdaRole(this, 'LinkerRoleV3', {
      roleName: evaluationResultLinkerRoleName(config.envName),
    });
    grantOwnLogWrites(persisterRole, persisterLogs);
    const ingress = new lambda.Function(this, 'IngressV3', {
      functionName: evaluationResultIngressV3FunctionName(config.envName),
      role: ingressRole,
      description: 'Released evaluator ingress with a content-free result projection.',
      runtime: new lambda.Runtime(config.pythonRuntime, lambda.RuntimeFamily.PYTHON),
      architecture: this.lambdaArchitecture(config),
      code: lambda.Code.fromAsset(props.sourceAssetPath),
      handler: 'adapters.jobs.evaluation_result_ingress.handler.handler',
      layers: [dependencies],
      environment: {
        ADL_ENVIRONMENT: config.envName,
        ADL_AWS_REGION: config.region,
        ADL_TRACING_ENABLED: String(config.transactionSearchEnabled),
        ADL_TRACE_SAMPLE_RATIO: String(config.traceSampleRatio),
        ADL_AGENT_QUALITY_AUDIT_TABLE: read('agentQualityAuditTableName'),
        ADL_EVALUATION_WRITER_RECEIPTS_TABLE: read('evaluationWriterReceiptsTableName'),
        // ADR-079: a result is admitted only when the turn's delivery receipt names its
        // configuration and evaluator, and its provenance is stamped from that receipt.
        ADL_EVALUATION_DELIVERIES_TABLE: read('evaluationDeliveriesTableName'),
        ADL_EVALUATION_RESULT_PROJECTION_QUEUE_URL: projectionQueue.queueUrl,
        ADL_RELEASED_EVALUATION_RESULT_LOG_GROUPS: cdk.Fn.join(
          ',',
          sources.map((source) => source.logGroupName),
        ),
      },
      // The decoder handles one bounded compressed subscription batch and only safe SQS handoff.
      memorySize: 512,
      timeout: cdk.Duration.seconds(30),
      reservedConcurrentExecutions: 2,
      logGroup: ingressLogs,
    });
    const persister = new lambda.Function(this, 'LinkerV3', {
      functionName: evaluationResultLinkerV3FunctionName(config.envName),
      role: persisterRole,
      runtime: new lambda.Runtime(config.pythonRuntime, lambda.RuntimeFamily.PYTHON),
      architecture: this.lambdaArchitecture(config),
      code: lambda.Code.fromAsset(props.sourceAssetPath),
      handler: 'adapters.jobs.evaluation_result_linker.handler.handler',
      layers: [dependencies],
      environment: {
        ADL_ENVIRONMENT: config.envName,
        ADL_AWS_REGION: config.region,
        ADL_TRACING_ENABLED: String(config.transactionSearchEnabled),
        ADL_TRACE_SAMPLE_RATIO: String(config.traceSampleRatio),
        ADL_EVALUATION_VERDICTS_TABLE: read('evaluationVerdictsTableName'),
        // The verdict expires with its turn's receipt (ADR-081 §5).
        ADL_RECEIPT_RETENTION_DAYS: String(config.receiptRetentionDays),
      },
      // One small canonical EvaluationResult.v3 write completes well within SQS visibility.
      memorySize: 512,
      timeout: cdk.Duration.seconds(30),
      reservedConcurrentExecutions: 2,
      logGroup: persisterLogs,
    });
    grantSpanExport(ingress);
    grantSpanExport(persister);

    ingress.addToRolePolicy(new iam.PolicyStatement({
      sid: 'ResolveExactEvaluationResultLink',
      actions: ['dynamodb:GetItem'],
      resources: [auditTableArn, writerReceiptsTableArn],
    }));
    // A strongly consistent read of the one delivery receipt that admits the result.
    ingress.addToRolePolicy(new iam.PolicyStatement({
      sid: 'ReadTheAdmittingDeliveryReceipt',
      actions: ['dynamodb:GetItem'],
      resources: [deliveriesTableArn],
    }));
    ingress.addToRolePolicy(new iam.PolicyStatement({
      sid: 'DiscoverOnlyOneCorrelationAudit',
      actions: ['dynamodb:Query'],
      resources: [`${auditTableArn}/index/correlation-audit`],
    }));
    ingress.addToRolePolicy(new iam.PolicyStatement({
      sid: 'EnqueueOnlyAllowlistedEvaluationResult',
      actions: ['sqs:SendMessage'],
      resources: [projectionQueue.queueArn],
    }));
    persister.addToRolePolicy(new iam.PolicyStatement({
      sid: 'ConsumeOnlyAllowlistedEvaluationResult',
      actions: ['sqs:DeleteMessage', 'sqs:GetQueueAttributes', 'sqs:ReceiveMessage'],
      resources: [projectionQueue.queueArn],
    }));
    // GetItem reads back a replayed conditional write so it is classified as exact (ADR-080 §3).
    persister.addToRolePolicy(new iam.PolicyStatement({
      sid: 'WriteExactEvaluationResult',
      actions: ['dynamodb:PutItem', 'dynamodb:GetItem'],
      resources: [verdictsTableArn],
    }));

    // ADR-079: only CloudWatch Logs, delivering from an owned result group, invokes the ingress;
    // every other principal is denied, which closes the directed-invocation path ADR-080 §3 left
    // open. One full resource policy rather than `AWS::Lambda::Permission`, which cannot express a
    // Deny; the two are not mixed on one function. aws-cdk-lib has no L1 class for the type.
    // `ResourceArn`, not `FunctionResourceArn`: the CloudFormation reference's property table
    // names `FunctionResourceArn` while its own example writes `ResourceArn`, and the registry
    // schema CloudFormation validates against lists only `ResourceArn` (required, create-only)
    // [verified 2026-09-27, DescribeType in us-east-1], which is also what TokenServiceStack's
    // deployed route-exchange policy uses.
    const ownedGroupArns = sources.map((source) => logGroupArn(source.logGroupName));
    const resourcePolicy = new cdk.CfnResource(this, 'IngressResourcePolicy', {
      type: 'AWS::Lambda::ResourcePolicy',
      properties: {
        ResourceArn: ingress.functionArn,
        PolicyDocument: {
          Version: '2012-10-17',
          Statement: [
            {
              Sid: 'AllowResultSubscriptions',
              Effect: 'Allow',
              Principal: { Service: 'logs.amazonaws.com' },
              Action: 'lambda:InvokeFunction',
              Resource: ingress.functionArn,
              Condition: {
                StringEquals: { 'aws:SourceAccount': cdk.Stack.of(this).account },
                ArnLike: { 'aws:SourceArn': ownedGroupArns },
              },
            },
            {
              // Keyed on the source rather than on who calls: a direct invocation carries no
              // `aws:SourceArn`, and a negated operator is true when its key is absent
              // [verified 2026-09-27, IAM User Guide, condition operators], so it is denied.
              // `InvokeAsync` is deprecated but still an action, as TokenServiceStack treats it.
              Sid: 'DenyAllButOwnedResultGroups',
              Effect: 'Deny',
              Principal: '*',
              Action: ['lambda:InvokeFunction', 'lambda:InvokeAsync'],
              Resource: ingress.functionArn,
              // Logs delivers asynchronously: its invoke is admitted by the Allow, then Lambda's
              // own service principal runs the queued event and is evaluated again, with no
              // SourceArn. Exempting only that principal admits delivery; a direct invoke,
              // sync or async, is still refused at the API [verified 2026-09-27, dev probe].
              Condition: {
                ArnNotLike: { 'aws:SourceArn': ownedGroupArns },
                StringNotEquals: { 'aws:PrincipalServiceName': 'lambda.amazonaws.com' },
              },
            },
          ],
        },
      },
    });
    for (const source of sources) {
      const subscription = new logs.CfnSubscriptionFilter(
        this,
        `ResultGroupSubscription${source.family.toUpperCase()}`,
        {
          destinationArn: ingress.functionArn,
          filterName: evaluationResultSubscriptionName(config.envName, source.family),
          // CloudWatch filter syntax cannot address dotted field names; ingress validates identity.
          filterPattern: '{ $.traceId = "*" }',
          logGroupName: source.logGroupName,
        },
      );
      // PutSubscriptionFilter checks the destination's permission, so the policy lands first.
      subscription.addResourceDependency(resourcePolicy);
    }

    new lambda.CfnEventSourceMapping(this, 'ResultLinkQueueMappingV3', {
      functionName: persister.functionName,
      eventSourceArn: projectionQueue.queueArn,
      batchSize: 1,
      functionResponseTypes: ['ReportBatchItemFailures'],
    });
    applyFunctionErrorAlarms(this, config, staffedAction);
    applyRemovalPolicy(this, cdk.RemovalPolicy.DESTROY);
    applyStandardTags(this, config);
  }

  private lambdaArchitecture(config: AppConfig): lambda.Architecture {
    return { arm64: lambda.Architecture.ARM_64 }[config.lambdaArchitecture];
  }
}
