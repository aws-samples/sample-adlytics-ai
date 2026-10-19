// SPDX-License-Identifier: MIT-0
import * as cdk from 'aws-cdk-lib';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as actions from 'aws-cdk-lib/aws-cloudwatch-actions';
import * as events from 'aws-cdk-lib/aws-events';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as pipes from 'aws-cdk-lib/aws-pipes';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';

import { AppConfig, paramPath, ParamKey } from '../config';
import {
  assertEventPatternFits,
  BACKUP_JOB_DETAIL_TYPE,
  backupDetail,
  backupJobStateDetail,
  CLOUDTRAIL_DETAIL_TYPE,
  cloudFormationExecutionRoleName,
  controlPlaneDetail,
  DETECTION_SERVICES,
  detectionChangeDetail,
  familySlug,
  FUNCTION_CHANGE_RULES,
  functionChangeDetail,
  FunctionChangeRule,
  ITEM_CHANGE_FILTERS,
  PIPE_FAILURE_METRIC,
  recordIntegrityLogGroupPrefix,
  recordIntegrityNamePrefix,
  recordIntegrityRulesPolicyName,
  recordTables,
  releaseRecordDetail,
  resultGroupDetail,
  roleChangeDetail,
  RULE_FAILURE_METRIC,
  tableArnBase,
  watchedEvaluationRoles,
  watchedFunctions,
} from '../constructs/agent-quality/record-integrity';
import { applyRemovalPolicy, applyStandardTags } from '../mixins/tagging';

export interface RecordIntegrityStackProps extends cdk.StackProps {
  readonly config: AppConfig;
}

/** One log group, its any-record metric filter, the alarm on it, and the rules that feed it. */
interface TamperLog {
  readonly group: logs.LogGroup;
  /** The group ARN without the `:*` stream suffix, the form a pipe or rule target takes. */
  readonly targetArn: string;
  readonly rules: events.Rule[];
}

/**
 * ADR-080 §4: tamper detection for the record tables the resource policies cannot fully protect.
 *
 * - Item changes: one pipe per table stream admits `MODIFY` and `REMOVE` records that are not TTL
 *   deletions into a log group.
 * - Control-plane changes: CloudTrail management events for the policy's data-copy and
 *   control-plane actions on those tables, whoever calls them, AWS Backup jobs on them, release
 *   record writes by anyone but a gate provider and evaluation result-group actions (ADR-079), and
 *   changes to this stack's own rules, pipes, alarms, and log groups, into a second log group.
 * - Function and role changes: creation, code, and configuration changes of every function holding
 *   a role a policy names, or passing one of those roles, and IAM changes to those roles or to the
 *   CloudFormation execution role, into a third.
 *
 * Every record in any of the three groups raises its alarm, and each pipe and each rule has a
 * failure alarm.
 * Destroyable: the tables it watches live in retained stacks, and destroying this stack loses the
 * detection, never a record.
 */
export class RecordIntegrityStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: RecordIntegrityStackProps) {
    super(scope, id, props);
    const { config } = props;
    const read = (key: ParamKey): string =>
      ssm.StringParameter.valueForStringParameter(this, paramPath(config.envName, key));
    const tables = recordTables(config.envName, config.evaluationInput.provisioned);
    // Rule and pipe names allow 64 characters and the prefix takes 41 at the longest environment
    // name, so every slug stays within 23; the long-name synth test is the gate.
    const named = (slug: string): string => `${recordIntegrityNamePrefix(config.envName)}${slug}`;
    // The published, unsubscribed ingestion alarm topic every alarm in this app notifies.
    const alarmAction = new actions.SnsAction(
      sns.Topic.fromTopicArn(this, 'AlarmTopic', read('ingestionAlarmTopicArn')),
    );
    const alarmOn = (alarmId: string, props: cloudwatch.AlarmProps): void => {
      new cloudwatch.Alarm(this, alarmId, props).addAlarmAction(alarmAction);
    };

    const tamperLog = (
      logId: string,
      slug: string,
      metricName: string,
      description: string,
    ): TamperLog => {
      const logGroupName = `${recordIntegrityLogGroupPrefix(config.envName)}${slug}`;
      const group = new logs.LogGroup(this, `${logId}Logs`, {
        logGroupName,
        retention: config.logRetentionDays,
      });
      const filter = new logs.MetricFilter(this, `${logId}Records`, {
        logGroup: group,
        filterPattern: logs.FilterPattern.allEvents(),
        metricNamespace: 'Adlytics',
        metricName,
        metricValue: '1',
      });
      alarmOn(`${logId}Alarm`, {
        alarmName: named(slug),
        alarmDescription: description,
        metric: filter.metric({ statistic: cloudwatch.Stats.SUM, period: cdk.Duration.minutes(1) }),
        threshold: 1,
        comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
        evaluationPeriods: 1,
        treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      });
      const targetArn = this.formatArn({
        service: 'logs',
        resource: 'log-group',
        arnFormat: cdk.ArnFormat.COLON_RESOURCE_NAME,
        resourceName: logGroupName,
      });
      return { group, targetArn, rules: [] };
    };

    const itemChanges = tamperLog(
      'ItemChanges',
      'item-changes',
      'RecordItemChanges',
      'A record table item was modified or removed other than by TTL expiry. The resource policy '
        + 'denies every item mutation, so this is a policy bypass or a policy change.',
    );
    for (const table of tables) {
      const streamArn = read(table.streamArnKey);
      const tableId = `${table.family.charAt(0).toUpperCase()}${table.family.slice(1)}`;
      const role = new iam.Role(this, `${tableId}PipeRole`, {
        assumedBy: new iam.ServicePrincipal('pipes.amazonaws.com', {
          conditions: { StringEquals: { 'aws:SourceAccount': this.account } },
        }),
        description: `Reads the ${table.tableName} stream into the item-change tamper log.`,
      });
      role.addToPolicy(new iam.PolicyStatement({
        actions: [
          'dynamodb:DescribeStream',
          'dynamodb:GetRecords',
          'dynamodb:GetShardIterator',
          'dynamodb:ListStreams',
        ],
        resources: [streamArn],
      }));
      role.addToPolicy(new iam.PolicyStatement({
        actions: ['logs:CreateLogStream', 'logs:PutLogEvents'],
        resources: [itemChanges.group.logGroupArn],
      }));
      // Named, so the self-watch rule and the failure alarm can address it.
      const pipeName = named(familySlug(table.family));
      const pipe = new pipes.CfnPipe(this, `${tableId}ChangePipe`, {
        name: pipeName,
        description: `Non-TTL item changes on ${table.tableName} (ADR-080 §4).`,
        roleArn: role.roleArn,
        source: streamArn,
        sourceParameters: {
          dynamoDbStreamParameters: { startingPosition: 'LATEST' },
          filterCriteria: {
            filters: ITEM_CHANGE_FILTERS.map((pattern) => ({ pattern: JSON.stringify(pattern) })),
          },
        },
        target: itemChanges.targetArn,
      });
      // The pipe validates its role's access when it is created, so the role's policy lands first.
      pipe.node.addDependency(role);
      // A pipe that cannot read its stream or write its group drops the records it exists to keep.
      alarmOn(`${tableId}PipeFailureAlarm`, {
        alarmName: named(`pipe-failures-${familySlug(table.family)}`),
        alarmDescription: `The ${table.tableName} item-change pipe failed an execution, so item `
          + 'changes on that table may not have reached the tamper log.',
        metric: new cloudwatch.Metric({
          namespace: PIPE_FAILURE_METRIC.namespace,
          metricName: PIPE_FAILURE_METRIC.metricName,
          dimensionsMap: { [PIPE_FAILURE_METRIC.dimension]: pipeName },
          statistic: cloudwatch.Stats.SUM,
          period: cdk.Duration.minutes(5),
        }),
        threshold: 1,
        comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
        evaluationPeriods: 1,
        treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      });
    }

    const tableNames = tables.map((table) => table.tableName);
    const controlPlane = tamperLog(
      'ControlPlaneChanges',
      'control-plane-changes',
      'RecordControlPlaneChanges',
      'A data-copy or control-plane action was called on a record table, a release record was '
        + 'written by another principal than its gate, an evaluation result group or account-level '
        + 'Logs policy was changed, or a tamper rule, pipe, alarm, or log group was changed. A '
        + 'deploy that changes a table policy, TTL, point-in-time recovery, a result group, or this '
        + 'detection raises this as expected.',
    );
    const rule = (
      ruleId: string,
      slug: string,
      description: string,
      pattern: { source: string | undefined; detailType: string; detail: Record<string, unknown> },
      log: TamperLog,
    ): void => {
      const source = pattern.source === undefined ? {} : { source: [pattern.source] };
      assertEventPatternFits(ruleId, {
        ...source,
        'detail-type': [pattern.detailType],
        detail: pattern.detail,
      });
      const ruleName = named(slug);
      log.rules.push(new events.Rule(this, ruleId, {
        ruleName,
        description,
        eventPattern: { ...source, detailType: [pattern.detailType], detail: pattern.detail },
        // A log-group target takes no role; the Logs resource policy below admits the delivery.
        targets: [{ bind: () => ({ arn: log.targetArn }) }],
      }));
      // A rule that cannot deliver to its group (the Logs resource policy deleted, the group
      // gone) drops the event it exists to keep, including the one recording that deletion.
      alarmOn(`${ruleId}FailureAlarm`, {
        alarmName: named(`delivery-failures-${slug}`),
        alarmDescription: `The ${ruleName} tamper rule failed to deliver a matched event to its `
          + 'log group, so a change it watches may not have raised its alarm.',
        metric: new cloudwatch.Metric({
          namespace: RULE_FAILURE_METRIC.namespace,
          metricName: RULE_FAILURE_METRIC.metricName,
          dimensionsMap: { [RULE_FAILURE_METRIC.dimension]: ruleName },
          statistic: cloudwatch.Stats.SUM,
          period: cdk.Duration.minutes(5),
        }),
        threshold: 1,
        comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
        evaluationPeriods: 1,
        treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      });
    };
    const cloudTrail = (source: string | undefined, detail: Record<string, unknown>) =>
      ({ source, detailType: CLOUDTRAIL_DETAIL_TYPE, detail });
    // One rule per table: a single pattern over all five tables exceeds EventBridge's limit.
    for (const table of tables) {
      const tableId = `${table.family.charAt(0).toUpperCase()}${table.family.slice(1)}`;
      rule(
        `ControlPlaneRule${tableId}`,
        `tables-${familySlug(table.family)}`,
        `Data-copy and control-plane calls on ${table.tableName} (ADR-080).`,
        cloudTrail(
          'aws.dynamodb',
          controlPlaneDetail([table.tableName], tableArnBase(config.region, config.account)),
        ),
        controlPlane,
      );
    }
    rule(
      'BackupRule',
      'backup-jobs',
      'AWS Backup jobs started on the ADR-080 record tables.',
      cloudTrail('aws.backup', backupDetail(tableNames)),
      controlPlane,
    );
    rule(
      'BackupJobStateRule',
      'backup-job-states',
      'AWS Backup job state changes, including plan-driven jobs, on the ADR-080 record tables.',
      { source: 'aws.backup', detailType: BACKUP_JOB_DETAIL_TYPE, detail: backupJobStateDetail(tableNames) },
      controlPlane,
    );
    // ADR-079: a release record written by anyone but its gate provider, and any action on an
    // owned result group, or an account-level Logs policy, that could reach released content.
    // Unconditional, like the watched functions: the names are fixed whether or not the
    // evaluation stacks are composed, and a record or group created outside them is the event.
    rule(
      'ReleaseRecordRule',
      'release-records',
      'Release-record writes by any principal but the evaluation gate providers (ADR-079).',
      cloudTrail('aws.ssm', releaseRecordDetail(config.envName, config.region, config.account)),
      controlPlane,
    );
    rule(
      'ResultGroupRule',
      'result-groups',
      'Actions on the owned evaluation result groups and account-level Logs policies (ADR-079).',
      cloudTrail('aws.logs', resultGroupDetail(config.envName, config.region, config.account)),
      controlPlane,
    );
    // ADR-080 §4: the detection watches itself, one rule per service.
    for (const service of DETECTION_SERVICES) {
      const { source, detail } = detectionChangeDetail(config.envName, service);
      rule(
        `DetectionChangeRule${service.charAt(0).toUpperCase()}${service.slice(1)}`,
        `self-${service}`,
        `Changes to the ADR-080 tamper ${service} resources themselves.`,
        cloudTrail(source, detail),
        controlPlane,
      );
    }

    const functionChanges = tamperLog(
      'FunctionChanges',
      'function-changes',
      'RecordFunctionChanges',
      'A function holding a role a record-table policy names was created or changed, a function '
        + 'was given one of those roles, or one of those roles or the CloudFormation execution role '
        + 'was created, deleted, or had its trust or permissions changed. A deploy of that function '
        + 'or role raises this as expected.',
    );
    const functionRules: Record<FunctionChangeRule, [string, string]> = {
      name: ['FunctionChangeRule', 'Changes to functions that hold a role an ADR-080 table policy names.'],
      role: ['FunctionRoleRule', 'Functions created or reconfigured with a role an ADR-080 table policy names.'],
      permission: [
        'FunctionPermissionRule',
        'Permission changes on functions that hold a role an ADR-080 table policy names.',
      ],
      policy: [
        'FunctionPolicyRule',
        'Resource-policy changes on functions that hold a role an ADR-080 table policy names.',
      ],
    };
    for (const by of FUNCTION_CHANGE_RULES) {
      const [ruleId, description] = functionRules[by];
      rule(
        ruleId,
        `functions-by-${by}`,
        description,
        cloudTrail('aws.lambda', functionChangeDetail(watchedFunctions(config.envName), by)),
        functionChanges,
      );
    }
    // IAM delivers its CloudTrail events only to us-east-1's default bus, so outside us-east-1
    // this rule matches nothing (see the agent-quality-loop LLD).
    rule(
      'RoleChangeRule',
      'role-changes',
      'IAM changes to the roles ADR-080 table policies name, the evaluation execution roles, and '
        + 'the CloudFormation execution role.',
      cloudTrail(
        'aws.iam',
        roleChangeDetail(
          watchedFunctions(config.envName),
          watchedEvaluationRoles(config.envName),
          cloudFormationExecutionRoleName(this),
        ),
      ),
      functionChanges,
    );

    // EventBridge writes to a log group only under a Logs resource policy; a rule target takes no
    // role. Each rule-target group admits EventBridge only on behalf of the rules that target it;
    // the pipe writes with its own role.
    new logs.ResourcePolicy(this, 'RuleDelivery', {
      resourcePolicyName: recordIntegrityRulesPolicyName(config.envName),
      policyStatements: [controlPlane, functionChanges].map((log) => new iam.PolicyStatement({
        principals: [new iam.ServicePrincipal('events.amazonaws.com')],
        actions: ['logs:CreateLogStream', 'logs:PutLogEvents'],
        resources: [log.group.logGroupArn],
        conditions: { ArnEquals: { 'aws:SourceArn': log.rules.map((candidate) => candidate.ruleArn) } },
      })),
    });

    applyRemovalPolicy(this, cdk.RemovalPolicy.DESTROY);
    applyStandardTags(this, config);
  }
}
