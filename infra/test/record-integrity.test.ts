// SPDX-License-Identifier: MIT-0
/**
 * ADR-080 §2 and §4: every record table's one-writer resource policy, and the tamper detection
 * that alarms on what the policy cannot prevent.
 */
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';

import {
  ENV_NAME_MAX_LENGTH,
  EVALUATION_FAMILIES,
  evaluationExecutionRoleName,
  evaluationGateProviderRoleName,
  evaluationReleaseParameterName,
  evaluationResultLogGroupName,
  paramPath,
  qualityReviewFunctionName,
  qualityReviewFunctionRoleName,
} from '../lib/config';
import {
  ACCOUNT_LOGS_EVENT_NAMES,
  assertEventPatternFits,
  BACKUP_JOB_DETAIL_TYPE,
  backupDetail,
  backupJobStateDetail,
  CLOUDTRAIL_DETAIL_TYPE,
  controlPlaneDetail,
  DATA_COPY_ACTIONS,
  DEPLOYMENT_ONLY_ACTIONS,
  DETECTION_SERVICES,
  detectionChangeDetail,
  familySlug,
  FUNCTION_CHANGE_EVENT_NAMES,
  FUNCTION_CHANGE_RULES,
  FUNCTION_CODE_EVENT_NAMES,
  FUNCTION_MAPPING_EVENT_VERBS,
  FUNCTION_POLICY_EVENT_NAMES,
  functionChangeDetail,
  IMMUTABLE_ACTIONS,
  ITEM_CHANGE_FILTERS,
  RECEIPT_READ_ACTIONS,
  RECEIPT_READERS,
  receiptReaderRoleArns,
  RECORD_FAMILIES,
  recordTable,
  recordTables,
  RELEASE_RECORD_EVENT_NAMES,
  releaseRecordDetail,
  RESULT_GROUP_EVENT_NAMES,
  resultGroupDetail,
  ROLE_CHANGE_EVENT_NAMES,
  roleChangeDetail,
  tableArnBase,
  watchedEvaluationRoles,
  watchedFunctions,
  WRITER_ONLY_ACTIONS,
} from '../lib/constructs/agent-quality/record-integrity';
import { RecordIntegrityStack } from '../lib/stacks/record-integrity-stack';
import { configWith, TEST_CONFIG } from './support/config';
import { matchesEventPattern, passesPipeFilters } from './support/event-pattern';
import { roleLogicalId, rolesThatReadFrom } from './support/iam';
import { everyTemplate, resourcesOfType } from './support/templates';

const templates = everyTemplate();
const template = (name: string): Record<string, unknown> =>
  templates[name] as Record<string, unknown>;
type Resource = { Type: string; Properties: Record<string, unknown>; DependsOn?: unknown };
const resources = (stack: string): Record<string, Resource> =>
  (template(stack).Resources ?? {}) as Record<string, Resource>;

const { account, region } = TEST_CONFIG;
const ENV = 'dev';

/** Render the partition-only `Fn::Join` tokens these ARNs carry as their `${AWS::Partition}` text. */
function render(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(render);
  if (value === null || typeof value !== 'object') return value;
  const record = value as Record<string, unknown>;
  if ('Ref' in record && record.Ref === 'AWS::Partition') return '${AWS::Partition}';
  if ('Fn::Join' in record) {
    const [separator, parts] = record['Fn::Join'] as [string, unknown[]];
    return parts.map((part) => String(render(part))).join(separator);
  }
  return Object.fromEntries(Object.entries(record).map(([key, nested]) => [key, render(nested)]));
}

const TABLE_STACK = {
  receipts: 'AgentQualityStack',
  findings: 'AgentQualityStack',
  deliveries: 'EvaluationEvidenceStack',
  writerReceipts: 'EvaluationEvidenceStack',
  verdicts: 'EvaluationEvidenceStack',
} as const;

const CFN_EXEC_ROLE_NAME = `cdk-hnb659fds-cfn-exec-role-${account}-${region}`;
const CFN_EXEC_ROLE = {
  'Fn::Sub': `arn:\${AWS::Partition}:iam::${account}:role/${CFN_EXEC_ROLE_NAME}`,
};

describe('record table resource policies (ADR-080 §2)', () => {
  const policyOf = (family: (typeof RECORD_FAMILIES)[number]): Record<string, unknown> => {
    const { tableName } = recordTable(ENV, family);
    const table = Object.values(resources(TABLE_STACK[family])).find(
      (resource) => resource.Type === 'AWS::DynamoDB::Table' && resource.Properties.TableName === tableName,
    );
    expect(table).toBeDefined();
    return render((table?.Properties.ResourcePolicy as { PolicyDocument: unknown }).PolicyDocument) as Record<string, unknown>;
  };

  it.each([
    ['receipts', 'adlytics-dev-agent-quality-audit', 'adlytics-dev-conversation-function'],
    ['findings', 'adlytics-dev-agent-quality-findings', 'adlytics-dev-agent-quality-processor'],
    ['deliveries', 'adlytics-dev-evaluation-deliveries', 'adlytics-dev-evaluation-outbox-dispatcher'],
    ['writerReceipts', 'adlytics-dev-evaluation-writer-receipts', 'adlytics-dev-evaluation-writer'],
    ['verdicts', 'adlytics-dev-evaluation-verdicts', 'adlytics-dev-evaluation-result-linker'],
  ] as const)('%s (%s) denies exactly the four statement sets, admitting only %s to put', (family, tableName, writer) => {
    const tableArn = `arn:\${AWS::Partition}:dynamodb:${region}:${account}:table/${tableName}`;
    const Resource = [tableArn, `${tableArn}/index/*`];
    const policy = policyOf(family) as { Statement: unknown[] };
    // Only the receipts table adds the read deny, asserted in its own describe below.
    const statements = family === 'receipts' ? policy.Statement.slice(0, 4) : policy.Statement;
    expect({ ...policy, Statement: statements }).toStrictEqual({
      Version: '2012-10-17',
      Statement: [
        {
          Sid: 'OnlyTheWriterPuts',
          Effect: 'Deny',
          Principal: '*',
          Action: ['dynamodb:PutItem', 'dynamodb:PartiQLInsert'],
          Resource,
          Condition: {
            ArnNotEquals: { 'aws:PrincipalArn': `arn:\${AWS::Partition}:iam::${account}:role/${writer}` },
          },
        },
        {
          Sid: 'RecordsAreImmutable',
          Effect: 'Deny',
          Principal: '*',
          Action: [
            'dynamodb:BatchWriteItem',
            'dynamodb:UpdateItem',
            'dynamodb:DeleteItem',
            'dynamodb:PartiQLUpdate',
            'dynamodb:PartiQLDelete',
          ],
          Resource,
        },
        {
          Sid: 'NoDataLeavesTheTable',
          Effect: 'Deny',
          Principal: '*',
          Action: [
            'dynamodb:CreateBackup',
            'dynamodb:RestoreTableToPointInTime',
            'dynamodb:ExportTableToPointInTime',
            'dynamodb:EnableKinesisStreamingDestination',
            'dynamodb:DisableKinesisStreamingDestination',
            'dynamodb:UpdateKinesisStreamingDestination',
          ],
          Resource,
        },
        {
          Sid: 'OnlyTheDeploymentChangesTheTable',
          Effect: 'Deny',
          Principal: '*',
          Action: [
            'dynamodb:PutResourcePolicy',
            'dynamodb:DeleteResourcePolicy',
            'dynamodb:UpdateTable',
            'dynamodb:DeleteTable',
            'dynamodb:UpdateTimeToLive',
            'dynamodb:UpdateContinuousBackups',
          ],
          Resource,
          // The deploying role itself, so DynamoDB's self-lockout check accepts the policy.
          Condition: { ArnNotEquals: { 'aws:PrincipalArn': CFN_EXEC_ROLE } },
        },
      ],
    });
  });

  it.each(RECORD_FAMILIES)('%s allows nothing and exempts no principal from mutation or data copy', (family) => {
    const statements = policyOf(family).Statement as Record<string, unknown>[];
    expect(statements.filter((statement) => statement.Effect !== 'Deny')).toStrictEqual([]);
    for (const actions of [IMMUTABLE_ACTIONS, DATA_COPY_ACTIONS]) {
      const statement = statements.find(
        (candidate) => JSON.stringify(candidate.Action) === JSON.stringify(actions),
      );
      expect(statement).toBeDefined();
      expect(statement).not.toHaveProperty('Condition');
      expect(statement).not.toHaveProperty('NotPrincipal');
      expect(statement?.Principal).toBe('*');
    }
  });

  it('the four statement sets are disjoint', () => {
    const all = [...WRITER_ONLY_ACTIONS, ...IMMUTABLE_ACTIONS, ...DATA_COPY_ACTIONS, ...DEPLOYMENT_ONLY_ACTIONS];
    expect(new Set(all).size).toBe(all.length);
  });

  it('the aggregate and outbox tables carry no record-table policy', () => {
    const tables = [
      ...resourcesOfType(template('AgentQualityStack'), 'AWS::DynamoDB::Table'),
      ...resourcesOfType(template('EvaluationEvidenceStack'), 'AWS::DynamoDB::Table'),
    ];
    const withPolicy = tables.filter((table) => table.ResourcePolicy !== undefined).map((table) => table.TableName);
    expect(withPolicy.sort()).toStrictEqual(recordTables(ENV, true).map((table) => table.tableName).sort());
  });
});

describe('record integrity stack (ADR-080 §4)', () => {
  const stack = resources('RecordIntegrityStack');
  const ofType = (type: string): [string, Resource][] =>
    Object.entries(stack).filter(([, resource]) => resource.Type === type);
  const ssmDefault = (ref: unknown): unknown => {
    const parameters = (template('RecordIntegrityStack').Parameters ?? {}) as Record<string, { Default?: unknown }>;
    return parameters[(ref as { Ref: string }).Ref]?.Default;
  };
  const logArn = (slug: string): string =>
    `arn:\${AWS::Partition}:logs:${region}:${account}:log-group:/adlytics/dev/record-integrity/${slug}`;

  it('is destroyable', () => {
    for (const resource of Object.values(stack) as (Resource & { DeletionPolicy?: string })[]) {
      expect(resource.DeletionPolicy ?? 'Delete').toBe('Delete');
    }
  });

  it('declares three tamper log groups with the configured retention', () => {
    expect(ofType('AWS::Logs::LogGroup').map(([, group]) => group.Properties).sort(
      (a, b) => String(a.LogGroupName).localeCompare(String(b.LogGroupName)),
    )).toStrictEqual(['control-plane-changes', 'function-changes', 'item-changes'].map((slug) => ({
      LogGroupName: `/adlytics/dev/record-integrity/${slug}`,
      RetentionInDays: 14,
      Tags: expect.any(Array),
    })));
  });

  it('pipes each record table stream, from LATEST, through the non-TTL filter, into the item-change group', () => {
    const pipes = ofType('AWS::Pipes::Pipe');
    expect(pipes).toHaveLength(RECORD_FAMILIES.length);
    const sources = pipes.map(([, pipe]) => ssmDefault(pipe.Properties.Source)).sort();
    expect(sources).toStrictEqual(
      recordTables(ENV, true).map((table) => paramPath(ENV, table.streamArnKey)).sort(),
    );
    expect(pipes.map(([, pipe]) => pipe.Properties.Name).sort()).toStrictEqual(
      RECORD_FAMILIES.map((family) => `adlytics-dev-record-${familySlug(family)}`).sort(),
    );
    for (const [, pipe] of pipes) {
      expect(pipe.Properties.SourceParameters).toStrictEqual({
        DynamoDBStreamParameters: { StartingPosition: 'LATEST' },
        FilterCriteria: { Filters: ITEM_CHANGE_FILTERS.map((filter) => ({ Pattern: JSON.stringify(filter) })) },
      });
      expect(render(pipe.Properties.Target)).toBe(logArn('item-changes'));
      expect(pipe.Properties).not.toHaveProperty('Enrichment');
    }
  });

  it('gives each pipe a role that reads only its stream and writes only the item-change group', () => {
    const logGroup = ofType('AWS::Logs::LogGroup').find(
      ([, group]) => group.Properties.LogGroupName === '/adlytics/dev/record-integrity/item-changes',
    )?.[0];
    for (const [, pipe] of ofType('AWS::Pipes::Pipe')) {
      const roleId = (pipe.Properties.RoleArn as { 'Fn::GetAtt': [string, string] })['Fn::GetAtt'][0];
      const role = stack[roleId];
      expect(role?.Properties.AssumeRolePolicyDocument).toStrictEqual({
        Version: '2012-10-17',
        Statement: [{
          Action: 'sts:AssumeRole',
          Effect: 'Allow',
          Principal: { Service: 'pipes.amazonaws.com' },
          Condition: { StringEquals: { 'aws:SourceAccount': account } },
        }],
      });
      const [policyId, policy] = ofType('AWS::IAM::Policy').find(
        ([, candidate]) => JSON.stringify(candidate.Properties.Roles) === JSON.stringify([{ Ref: roleId }]),
      ) ?? [];
      expect(policy?.Properties.PolicyDocument).toStrictEqual({
        Version: '2012-10-17',
        Statement: [
          {
            Action: ['dynamodb:DescribeStream', 'dynamodb:GetRecords', 'dynamodb:GetShardIterator', 'dynamodb:ListStreams'],
            Effect: 'Allow',
            Resource: pipe.Properties.Source,
          },
          {
            Action: ['logs:CreateLogStream', 'logs:PutLogEvents'],
            Effect: 'Allow',
            Resource: { 'Fn::GetAtt': [logGroup, 'Arn'] },
          },
        ],
      });
      // The pipe validates its role's access at creation, so the policy must exist first.
      expect(pipe.DependsOn).toEqual(expect.arrayContaining([roleId, policyId]));
    }
  });

  it('counts every record in each group and alarms on the first, to the published alarm topic', () => {
    const filters = ofType('AWS::Logs::MetricFilter').map(([, filter]) => filter.Properties);
    expect(filters.map((filter) => filter.FilterPattern)).toStrictEqual(['', '', '']);
    const metrics = filters.map((filter) => filter.MetricTransformations).sort(
      (a, b) => JSON.stringify(a).localeCompare(JSON.stringify(b)),
    );
    expect(metrics).toStrictEqual(
      ['RecordControlPlaneChanges', 'RecordFunctionChanges', 'RecordItemChanges'].map((MetricName) => [
        { MetricName, MetricNamespace: 'Adlytics', MetricValue: '1' },
      ]),
    );
    const alarms = ofType('AWS::CloudWatch::Alarm')
      .map(([, alarm]) => alarm.Properties)
      .filter((alarm) => alarm.Namespace === 'Adlytics');
    expect(alarms.map((alarm) => alarm.AlarmName).sort()).toStrictEqual([
      'adlytics-dev-record-control-plane-changes',
      'adlytics-dev-record-function-changes',
      'adlytics-dev-record-item-changes',
    ]);
    for (const alarm of alarms) {
      expect(alarm).toMatchObject({
        Namespace: 'Adlytics',
        Statistic: 'Sum',
        Period: 60,
        Threshold: 1,
        ComparisonOperator: 'GreaterThanOrEqualToThreshold',
        EvaluationPeriods: 1,
        TreatMissingData: 'notBreaching',
      });
      const actions = alarm.AlarmActions as unknown[];
      expect(actions).toHaveLength(1);
      expect(ssmDefault(actions[0])).toBe(paramPath(ENV, 'ingestionAlarmTopicArn'));
    }
  });

  it('alarms on each pipe\'s execution failures, by pipe name, to the published alarm topic', () => {
    const alarms = ofType('AWS::CloudWatch::Alarm')
      .map(([, alarm]) => alarm.Properties)
      .filter((alarm) => alarm.Namespace === 'AWS/EventBridge/Pipes');
    expect(alarms.map((alarm) => alarm.AlarmName).sort()).toStrictEqual(
      RECORD_FAMILIES.map((family) => `adlytics-dev-record-pipe-failures-${familySlug(family)}`).sort(),
    );
    for (const alarm of alarms) {
      const family = String(alarm.AlarmName).replace('adlytics-dev-record-pipe-failures-', '');
      expect(alarm).toMatchObject({
        Namespace: 'AWS/EventBridge/Pipes',
        MetricName: 'ExecutionFailed',
        Dimensions: [{ Name: 'PipeName', Value: `adlytics-dev-record-${family}` }],
        Statistic: 'Sum',
        Period: 300,
        Threshold: 1,
        ComparisonOperator: 'GreaterThanOrEqualToThreshold',
        EvaluationPeriods: 1,
        TreatMissingData: 'notBreaching',
      });
      const actions = alarm.AlarmActions as unknown[];
      expect(actions).toHaveLength(1);
      expect(ssmDefault(actions[0])).toBe(paramPath(ENV, 'ingestionAlarmTopicArn'));
    }
  });

  it('alarms on each rule\'s failed deliveries, by rule name, to the published alarm topic', () => {
    const ruleNames = ofType('AWS::Events::Rule').map(([, rule]) => String(rule.Properties.Name)).sort();
    const alarms = ofType('AWS::CloudWatch::Alarm')
      .map(([, alarm]) => alarm.Properties)
      .filter((alarm) => alarm.Namespace === 'AWS/Events');
    expect(alarms.map((alarm) => (alarm.Dimensions as { Value: string }[])[0]?.Value).sort()).toStrictEqual(ruleNames);
    for (const alarm of alarms) {
      const ruleName = (alarm.Dimensions as { Value: string }[])[0]!.Value;
      expect(alarm).toMatchObject({
        AlarmName: ruleName.replace('adlytics-dev-record-', 'adlytics-dev-record-delivery-failures-'),
        Namespace: 'AWS/Events',
        MetricName: 'FailedInvocations',
        Dimensions: [{ Name: 'RuleName', Value: ruleName }],
        Statistic: 'Sum',
        Period: 300,
        Threshold: 1,
        ComparisonOperator: 'GreaterThanOrEqualToThreshold',
        EvaluationPeriods: 1,
        TreatMissingData: 'notBreaching',
      });
      const actions = alarm.AlarmActions as unknown[];
      expect(actions).toHaveLength(1);
      expect(ssmDefault(actions[0])).toBe(paramPath(ENV, 'ingestionAlarmTopicArn'));
    }
  });

  it('names every rule, pipe, and alarm under the prefix the self-watch rules match', () => {
    const names = [
      ...ofType('AWS::Events::Rule').map(([, rule]) => rule.Properties.Name),
      ...ofType('AWS::Pipes::Pipe').map(([, pipe]) => pipe.Properties.Name),
      ...ofType('AWS::CloudWatch::Alarm').map(([, alarm]) => alarm.Properties.AlarmName),
    ];
    for (const name of names) expect(String(name)).toMatch(/^adlytics-dev-record-/);
    for (const [, group] of ofType('AWS::Logs::LogGroup')) {
      expect(String(group.Properties.LogGroupName)).toMatch(/^\/adlytics\/dev\/record-integrity\//);
    }
  });

  it('renders each CloudTrail rule with exactly the module pattern, targeting its tamper group', () => {
    const tableNames = recordTables(ENV, true).map((table) => table.tableName);
    const perTable = Object.fromEntries(recordTables(ENV, true).map((table) => [
      `adlytics-dev-record-tables-${familySlug(table.family)}`,
      [
        { source: ['aws.dynamodb'], 'detail-type': [CLOUDTRAIL_DETAIL_TYPE], detail: controlPlaneDetail([table.tableName], tableArnBase(region, account)) },
        'control-plane-changes',
      ],
    ])) as Record<string, [Record<string, unknown>, string]>;
    const expected: Record<string, [Record<string, unknown>, string]> = {
      ...perTable,
      'adlytics-dev-record-backup-jobs': [
        { source: ['aws.backup'], 'detail-type': [CLOUDTRAIL_DETAIL_TYPE], detail: backupDetail(tableNames) },
        'control-plane-changes',
      ],
      'adlytics-dev-record-functions-by-name': [
        { source: ['aws.lambda'], 'detail-type': [CLOUDTRAIL_DETAIL_TYPE], detail: functionChangeDetail(watchedFunctions(ENV), 'name') },
        'function-changes',
      ],
      'adlytics-dev-record-functions-by-role': [
        { source: ['aws.lambda'], 'detail-type': [CLOUDTRAIL_DETAIL_TYPE], detail: functionChangeDetail(watchedFunctions(ENV), 'role') },
        'function-changes',
      ],
      'adlytics-dev-record-functions-by-permission': [
        { source: ['aws.lambda'], 'detail-type': [CLOUDTRAIL_DETAIL_TYPE], detail: functionChangeDetail(watchedFunctions(ENV), 'permission') },
        'function-changes',
      ],
      'adlytics-dev-record-functions-by-policy': [
        { source: ['aws.lambda'], 'detail-type': [CLOUDTRAIL_DETAIL_TYPE], detail: functionChangeDetail(watchedFunctions(ENV), 'policy') },
        'function-changes',
      ],
      'adlytics-dev-record-backup-job-states': [
        { source: ['aws.backup'], 'detail-type': [BACKUP_JOB_DETAIL_TYPE], detail: backupJobStateDetail(tableNames) },
        'control-plane-changes',
      ],
      'adlytics-dev-record-release-records': [
        { source: ['aws.ssm'], 'detail-type': [CLOUDTRAIL_DETAIL_TYPE], detail: releaseRecordDetail(ENV, region, account) },
        'control-plane-changes',
      ],
      'adlytics-dev-record-result-groups': [
        { source: ['aws.logs'], 'detail-type': [CLOUDTRAIL_DETAIL_TYPE], detail: resultGroupDetail(ENV, region, account) },
        'control-plane-changes',
      ],
      'adlytics-dev-record-role-changes': [
        { source: ['aws.iam'], 'detail-type': [CLOUDTRAIL_DETAIL_TYPE], detail: roleChangeDetail(watchedFunctions(ENV), watchedEvaluationRoles(ENV), CFN_EXEC_ROLE_NAME) },
        'function-changes',
      ],
      ...Object.fromEntries(DETECTION_SERVICES.map((service) => {
        const { source, detail } = detectionChangeDetail(ENV, service);
        return [`adlytics-dev-record-self-${service}`, [
          { ...(source === undefined ? {} : { source: [source] }), 'detail-type': [CLOUDTRAIL_DETAIL_TYPE], detail },
          'control-plane-changes',
        ]];
      })) as Record<string, [Record<string, unknown>, string]>,
    };
    const rules = ofType('AWS::Events::Rule').map(([, rule]) => rule.Properties);
    expect(rules.map((rule) => rule.Name).sort()).toStrictEqual(Object.keys(expected).sort());
    for (const rule of rules) {
      const [pattern, slug] = expected[String(rule.Name)] ?? [];
      expect(rule.EventPattern).toStrictEqual(pattern);
      expect(rule.State).toBe('ENABLED');
      expect(render(rule.Targets)).toStrictEqual([{ Arn: logArn(slug ?? ''), Id: 'Target0' }]);
      expect(rule).not.toHaveProperty('EventBusName');
    }
  });

  it('admits only EventBridge to each rule group, and only on behalf of the rules that target it', () => {
    const [policy, ...rest] = ofType('AWS::Logs::ResourcePolicy').map(([, resource]) => resource.Properties);
    expect(rest).toHaveLength(0);
    const groupId = (slug: string): string | undefined => ofType('AWS::Logs::LogGroup').find(
      ([, group]) => group.Properties.LogGroupName === `/adlytics/dev/record-integrity/${slug}`,
    )?.[0];
    const rulesTargeting = (slug: string): string[] => ofType('AWS::Events::Rule')
      .filter(([, rule]) => JSON.stringify(render(rule.Properties.Targets)).includes(`/record-integrity/${slug}"`))
      .map(([id]) => id);
    expect(policy?.PolicyName).toBe('adlytics-dev-record-integrity-rules');
    const document = policy?.PolicyDocument as { 'Fn::Join': [string, unknown[]] };
    const parts = document['Fn::Join'][1];
    const text = parts.map((part) => (typeof part === 'string' ? part : `<${JSON.stringify(part)}>`)).join('');
    const statement = (slug: string): string => {
      const arns = rulesTargeting(slug).map((id) => `"<{"Fn::GetAtt":["${id}","Arn"]}>"`).join(',');
      return '{"Action":["logs:CreateLogStream","logs:PutLogEvents"],'
        + `"Condition":{"ArnEquals":{"aws:SourceArn":[${arns}]}},"Effect":"Allow",`
        + '"Principal":{"Service":"events.amazonaws.com"},'
        + `"Resource":"<{"Fn::GetAtt":["${groupId(slug)}","Arn"]}>"}`;
    };
    expect(rulesTargeting('control-plane-changes')).toHaveLength(recordTables(ENV, true).length + 4 + DETECTION_SERVICES.length);
    expect(rulesTargeting('function-changes')).toHaveLength(FUNCTION_CHANGE_RULES.length + 1);
    expect(text).toBe(
      `{"Statement":[${statement('control-plane-changes')},${statement('function-changes')}],"Version":"2012-10-17"}`,
    );
  });

  it('watches only the receipts and findings tables when evaluation input is not provisioned', () => {
    const app = new cdk.App();
    const synthesized = Template.fromStack(new RecordIntegrityStack(app, 'RecordIntegrityCore', {
      config: TEST_CONFIG,
      env: { account, region },
    })).toJSON() as Record<string, unknown>;
    const pipes = resourcesOfType(synthesized, 'AWS::Pipes::Pipe');
    expect(pipes).toHaveLength(2);
    const controlPlaneRules = resourcesOfType(synthesized, 'AWS::Events::Rule')
      .filter((candidate) => String(candidate.Name).startsWith('adlytics-dev-record-tables-'))
      .map((candidate) => (candidate.EventPattern as { detail: unknown }).detail);
    expect(controlPlaneRules).toStrictEqual(
      ['adlytics-dev-agent-quality-audit', 'adlytics-dev-agent-quality-findings'].map(
        (name) => controlPlaneDetail([name], tableArnBase(region, account)),
      ),
    );
  });

  it('fits EventBridge pattern and name limits at the longest environment name', () => {
    // Synthesized, not only constructed: rule and pipe name lengths are checked at synth.
    const envName = `e${'x'.repeat(ENV_NAME_MAX_LENGTH - 1)}`;
    const synthesized = Template.fromStack(new RecordIntegrityStack(new cdk.App(), 'RecordIntegrityLongName', {
      config: configWith({ envName, evaluationInput: { provisioned: true } }),
      env: { account, region },
    })).toJSON() as Record<string, unknown>;
    const names = [
      ...resourcesOfType(synthesized, 'AWS::Events::Rule').map((rule) => String(rule.Name)),
      ...resourcesOfType(synthesized, 'AWS::Pipes::Pipe').map((pipe) => String(pipe.Name)),
    ];
    expect(names.length).toBeGreaterThan(0);
    for (const name of names) expect(name.length).toBeLessThanOrEqual(64);
    // The ADR-079 rules grow with the environment name too; synth refuses one past the limit.
    for (const [ruleId, detail] of [
      ['ReleaseRecordRule', releaseRecordDetail(envName, region, account)],
      ['ResultGroupRule', resultGroupDetail(envName, region, account)],
      ...FUNCTION_CHANGE_RULES.map((by) => [`Function-${by}`, functionChangeDetail(watchedFunctions(envName), by)] as const),
      ['SelfLogs', detectionChangeDetail(envName, 'logs').detail],
      ['RoleChangeRule', roleChangeDetail(watchedFunctions(envName), watchedEvaluationRoles(envName), CFN_EXEC_ROLE_NAME)],
    ] as const) {
      expect(() => assertEventPatternFits(ruleId, { source: ['x'], 'detail-type': [CLOUDTRAIL_DETAIL_TYPE], detail })).not.toThrow();
    }
  });
});

describe('every watched function holds the role the alarm names', () => {
  it.each(watchedFunctions(ENV).map((fn) => [fn.functionName, fn.roleName]))('%s runs as %s', (functionName, roleName) => {
    const holders = Object.entries(templates).flatMap(([name, synthesized]) => {
      const role = roleLogicalId(synthesized as never, roleName);
      return resourcesOfType(synthesized, 'AWS::Lambda::Function')
        .filter((fn) => fn.FunctionName === functionName)
        .map((fn) => ({ name, matches: JSON.stringify(fn.Role) === JSON.stringify({ 'Fn::GetAtt': [role, 'Arn'] }) }));
    });
    expect(holders).toHaveLength(1);
    expect(holders[0]?.matches).toBe(true);
  });

  it('watches every role a record-table policy names as its writer', () => {
    const watched = new Set(watchedFunctions(ENV).map((fn) => fn.roleName));
    for (const table of recordTables(ENV, true)) expect(watched).toContain(table.writerRoleName);
  });

  it('watches both gate providers, whose roles the release-record rule exempts (ADR-079)', () => {
    const watched = new Set(watchedFunctions(ENV).map((fn) => fn.roleName));
    for (const family of EVALUATION_FAMILIES) expect(watched).toContain(evaluationGateProviderRoleName(ENV, family));
  });

  it('watches the one review function and none of the retired per-level ones (ADR-078)', () => {
    const review = watchedFunctions(ENV).filter((fn) => fn.functionName.includes('QualityReview'));
    expect(review).toStrictEqual([
      { functionName: qualityReviewFunctionName(ENV), roleName: qualityReviewFunctionRoleName(ENV) },
    ]);
    expect(review[0]).toStrictEqual({
      functionName: 'adlytics-dev-QualityReview',
      roleName: 'adlytics-dev-quality-review',
    });
  });

  it('watches every receipt reader the read deny exempts', () => {
    const watched = new Set(watchedFunctions(ENV).map((fn) => fn.roleName));
    for (const reader of RECEIPT_READERS) expect(watched).toContain(reader(ENV));
  });
});

describe('the receipts read deny (ADR-078, ADR-080 §2)', () => {
  const receipts = recordTable(ENV, 'receipts').tableName;
  const readsReceipts = (table: string): boolean =>
    table === '*'
    || table === paramPath(ENV, 'agentQualityAuditTableArn')
    || table.startsWith(`${paramPath(ENV, 'agentQualityAuditTableArn')}/index/`)
    || table === `table/${receipts}`
    || table.startsWith(`table/${receipts}/index/`);

  it('denies exactly the item-read actions', () => {
    expect(RECEIPT_READ_ACTIONS).toStrictEqual([
      'dynamodb:GetItem',
      'dynamodb:BatchGetItem',
      'dynamodb:Query',
      'dynamodb:Scan',
      'dynamodb:ConditionCheckItem',
      'dynamodb:PartiQLSelect',
    ]);
    const others = new Set<string>([
      ...WRITER_ONLY_ACTIONS, ...IMMUTABLE_ACTIONS, ...DATA_COPY_ACTIONS, ...DEPLOYMENT_ONLY_ACTIONS,
    ]);
    expect(RECEIPT_READ_ACTIONS.filter((action) => others.has(action))).toStrictEqual([]);
  });

  it('exempts exactly the writer, processor, dispatcher, ingress, and review roles', () => {
    expect(RECEIPT_READERS.map((reader) => reader(ENV))).toStrictEqual([
      'adlytics-dev-conversation-function',
      'adlytics-dev-agent-quality-processor',
      'adlytics-dev-evaluation-outbox-dispatcher',
      'adlytics-dev-evaluation-result-ingress',
      'adlytics-dev-quality-review',
    ]);
    expect(receiptReaderRoleArns('aws', account, ENV)).toStrictEqual(
      RECEIPT_READERS.map((reader) => `arn:aws:iam::${account}:role/${reader(ENV)}`),
    );
  });

  it('names every role that holds a receipts read today, so applying it locks out no reader', () => {
    const readers = rolesThatReadFrom(templates, readsReceipts);
    expect(readers).toStrictEqual(new Set(RECEIPT_READERS.map((reader) => reader(ENV))));
  });

  const statementsOf = (stack: string, tableName: string): Record<string, unknown>[] => {
    const table = Object.values(resources(stack)).find(
      (resource) => resource.Type === 'AWS::DynamoDB::Table' && resource.Properties.TableName === tableName,
    );
    expect(table).toBeDefined();
    return (render((table?.Properties.ResourcePolicy as { PolicyDocument: unknown }).PolicyDocument) as {
      Statement: Record<string, unknown>[];
    }).Statement;
  };

  it('is the receipts table policy\'s fifth statement, exempting exactly the named readers', () => {
    const tableArn = `arn:\${AWS::Partition}:dynamodb:${region}:${account}:table/${receipts}`;
    const statements = statementsOf('AgentQualityStack', receipts);
    expect(statements).toHaveLength(5);
    expect(statements[4]).toStrictEqual({
      Sid: 'OnlyNamedReadersRead',
      Effect: 'Deny',
      Principal: '*',
      Action: [...RECEIPT_READ_ACTIONS],
      Resource: [tableArn, `${tableArn}/index/*`],
      Condition: {
        ArnNotEquals: {
          'aws:PrincipalArn': RECEIPT_READERS.map(
            (reader) => `arn:\${AWS::Partition}:iam::${account}:role/${reader(ENV)}`,
          ),
        },
      },
    });
  });

  it.each(RECORD_FAMILIES.filter((family) => family !== 'receipts'))(
    'is on no other record table: %s',
    (family) => {
      const { tableName, evaluation } = recordTable(ENV, family);
      const sids = statementsOf(evaluation ? 'EvaluationEvidenceStack' : 'AgentQualityStack', tableName)
        .map((statement) => statement.Sid);
      expect(sids).not.toContain('OnlyNamedReadersRead');
    },
  );
});

/* -------------------------------------------------------------------------
 * Offline pattern matching against sample events
 * ---------------------------------------------------------------------- */

const streamRecord = (eventName: string, userIdentity?: Record<string, string>): Record<string, unknown> => ({
  eventID: '1',
  eventVersion: '1.1',
  eventSource: 'aws:dynamodb',
  awsRegion: region,
  eventName,
  eventSourceARN: `arn:aws:dynamodb:${region}:${account}:table/adlytics-dev-agent-quality-audit/stream/2026-09-27T00:00:00.000`,
  dynamodb: {
    Keys: { audit_id: { S: 'fictional-audit' }, item_key: { S: 'RECEIPT' } },
    SequenceNumber: '111',
    SizeBytes: 26,
    StreamViewType: 'KEYS_ONLY',
  },
  ...(userIdentity === undefined ? {} : { userIdentity }),
});

describe('the item-change pipe filter', () => {
  it.each([
    ['a MODIFY', streamRecord('MODIFY'), true],
    ['a REMOVE', streamRecord('REMOVE'), true],
    ['a TTL REMOVE', streamRecord('REMOVE', { type: 'Service', principalId: 'dynamodb.amazonaws.com' }), false],
    ['an INSERT', streamRecord('INSERT'), false],
    ['a REMOVE by another service', streamRecord('REMOVE', { type: 'Service', principalId: 'replication.example' }), true],
    ['a REMOVE claiming the DynamoDB principal but not the Service type', streamRecord('REMOVE', { type: 'AssumedRole', principalId: 'dynamodb.amazonaws.com' }), true],
    ['a REMOVE with only a principal', streamRecord('REMOVE', { principalId: 'dynamodb.amazonaws.com' }), true],
  ] as const)('%s passes: %s', (_label, record, expected) => {
    expect(passesPipeFilters(ITEM_CHANGE_FILTERS, record)).toBe(expected);
  });

  it('fits the pipe limit of five filters', () => {
    expect(ITEM_CHANGE_FILTERS.length).toBeLessThanOrEqual(5);
  });
});

const TABLES = recordTables(ENV, true).map((table) => table.tableName);
const AUDIT = 'adlytics-dev-agent-quality-audit';
const AUDIT_ARN = `arn:aws:dynamodb:${region}:${account}:table/${AUDIT}`;

const cloudTrail = (
  source: string,
  eventSource: string,
  eventName: string,
  requestParameters: Record<string, unknown> | null,
): Record<string, unknown> => ({
  version: '0',
  id: '00000000-0000-4000-8000-000000000000',
  'detail-type': CLOUDTRAIL_DETAIL_TYPE,
  source,
  account,
  time: '2026-09-27T00:00:00Z',
  region,
  resources: [],
  detail: {
    eventVersion: '1.09',
    userIdentity: {
      type: 'AssumedRole',
      arn: `arn:aws:sts::${account}:assumed-role/fictional-operator/session`,
    },
    eventSource,
    eventName,
    awsRegion: region,
    requestParameters,
    responseElements: null,
    eventType: 'AwsApiCall',
    managementEvent: true,
  },
});

const controlPlanePattern = {
  source: ['aws.dynamodb'],
  'detail-type': [CLOUDTRAIL_DETAIL_TYPE],
  detail: controlPlaneDetail(TABLES, tableArnBase(region, account)),
};
const dynamo = (eventName: string, requestParameters: Record<string, unknown> | null) =>
  cloudTrail('aws.dynamodb', 'dynamodb.amazonaws.com', eventName, requestParameters);

describe('the control-plane rule pattern', () => {
  it.each([
    ['CreateBackup', { tableName: AUDIT, backupName: 'fictional' }],
    ['CreateBackup', { tableName: AUDIT_ARN, backupName: 'fictional' }],
    ['RestoreTableToPointInTime', { sourceTableName: AUDIT, targetTableName: 'fictional-copy' }],
    ['RestoreTableToPointInTime', { sourceTableArn: AUDIT_ARN, targetTableName: 'fictional-copy' }],
    ['ExportTableToPointInTime', { tableArn: AUDIT_ARN, s3Bucket: 'fictional-bucket' }],
    ['EnableKinesisStreamingDestination', { tableName: AUDIT, streamArn: 'arn:aws:kinesis:::stream/fictional' }],
    ['DisableKinesisStreamingDestination', { tableName: AUDIT, streamArn: 'arn:aws:kinesis:::stream/fictional' }],
    ['UpdateKinesisStreamingDestination', { tableName: AUDIT_ARN, streamArn: 'arn:aws:kinesis:::stream/fictional' }],
    ['PutResourcePolicy', { resourceArn: AUDIT_ARN, policy: '{}' }],
    ['PutResourcePolicy', { resourceArn: `${AUDIT_ARN}/stream/2026-09-27T00:00:00.000`, policy: '{}' }],
    ['DeleteResourcePolicy', { resourceArn: AUDIT_ARN }],
    ['UpdateTable', { tableName: AUDIT, billingMode: 'PROVISIONED' }],
    ['DeleteTable', { tableName: AUDIT }],
    ['UpdateTimeToLive', { tableName: AUDIT, timeToLiveSpecification: { enabled: false, attributeName: 'expires_at' } }],
    ['UpdateContinuousBackups', { tableName: AUDIT_ARN, pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: false } }],
  ])('matches %s naming the receipts table by %j', (eventName, parameters) => {
    expect(matchesEventPattern(controlPlanePattern, dynamo(eventName, parameters))).toBe(true);
  });

  it.each(TABLES)('matches UpdateTimeToLive on %s', (tableName) => {
    expect(matchesEventPattern(controlPlanePattern, dynamo('UpdateTimeToLive', { tableName }))).toBe(true);
  });

  it.each([
    ['another table', dynamo('UpdateTimeToLive', { tableName: 'adlytics-dev-agent-quality-aggregate' })],
    ['a table sharing a name prefix', dynamo('DeleteTable', { tableName: `${AUDIT}-copy` })],
    ['the outbox table by ARN', dynamo('PutResourcePolicy', { resourceArn: `arn:aws:dynamodb:${region}:${account}:table/adlytics-dev-evaluation-outbox` })],
    ['a restore into a record table name from another source', dynamo('RestoreTableToPointInTime', { sourceTableName: 'fictional', targetTableName: AUDIT })],
    ['an item write', dynamo('PutItem', { tableName: AUDIT })],
    ['a describe', dynamo('DescribeTable', { tableName: AUDIT })],
    ['a missing request', dynamo('UpdateTable', null)],
    ['another source', cloudTrail('aws.s3', 's3.amazonaws.com', 'UpdateTable', { tableName: AUDIT })],
  ])('does not match %s', (_label, event) => {
    expect(matchesEventPattern(controlPlanePattern, event)).toBe(false);
  });

  it('names every statement 3 and statement 4 action', () => {
    expect((controlPlanePattern.detail.eventName as string[]).map((name) => `dynamodb:${name}`))
      .toStrictEqual([...DATA_COPY_ACTIONS, ...DEPLOYMENT_ONLY_ACTIONS]);
  });
});

describe('the backup rule pattern', () => {
  const pattern = { source: ['aws.backup'], 'detail-type': [CLOUDTRAIL_DETAIL_TYPE], detail: backupDetail(TABLES) };
  const backup = (resourceArn: string) =>
    cloudTrail('aws.backup', 'backup.amazonaws.com', 'StartBackupJob', { resourceArn, backupVaultName: 'fictional' });

  it('matches an on-demand AWS Backup job of a record table', () => {
    expect(matchesEventPattern(pattern, backup(AUDIT_ARN))).toBe(true);
  });

  it('does not match a backup job of another table', () => {
    expect(matchesEventPattern(pattern, backup(`${AUDIT_ARN}-copy`))).toBe(false);
  });
});

describe('the function-change rule pattern', () => {
  const pattern = {
    source: ['aws.lambda'],
    'detail-type': [CLOUDTRAIL_DETAIL_TYPE],
    detail: functionChangeDetail(watchedFunctions(ENV), 'name'),
  };
  const rolePattern = {
    source: ['aws.lambda'],
    'detail-type': [CLOUDTRAIL_DETAIL_TYPE],
    detail: functionChangeDetail(watchedFunctions(ENV), 'role'),
  };
  const policyPatterns = (['permission', 'policy'] as const).map((by) => ({
    source: ['aws.lambda'],
    'detail-type': [CLOUDTRAIL_DETAIL_TYPE],
    detail: functionChangeDetail(watchedFunctions(ENV), by),
  }));
  const matchesPolicyRules = (event: unknown): boolean =>
    policyPatterns.some((candidate) => matchesEventPattern(candidate, event));
  const matchesFunctionRules = (event: unknown): boolean =>
    [pattern, rolePattern].some((candidate) => matchesEventPattern(candidate, event)) || matchesPolicyRules(event);
  /** Add/RemovePermission name the function in `functionName`, the resource-policy calls in `resourceArn`. */
  const byResourceArn = (eventName: string): boolean => /ResourcePolicy/.test(eventName);
  const lambda = (eventName: string, requestParameters: Record<string, unknown>) =>
    cloudTrail('aws.lambda', 'lambda.amazonaws.com', eventName, requestParameters);
  const functionArn = (name: string): string => `arn:aws:lambda:${region}:${account}:function:${name}`;
  const roleArn = (name: string): string => `arn:aws:iam::${account}:role/${name}`;

  it('watches every code, configuration, permission, and resource-policy call', () => {
    expect(FUNCTION_CHANGE_EVENT_NAMES).toStrictEqual([
      ...FUNCTION_CODE_EVENT_NAMES,
      ...FUNCTION_MAPPING_EVENT_VERBS,
      ...FUNCTION_POLICY_EVENT_NAMES,
    ]);
    expect(FUNCTION_MAPPING_EVENT_VERBS).toStrictEqual(['CreateEventSourceMapping', 'UpdateEventSourceMapping']);
    expect(FUNCTION_POLICY_EVENT_NAMES).toStrictEqual([
      'AddPermission20150331v2',
      'RemovePermission20150331v2',
      'AddPermission',
      'RemovePermission',
      'PutResourcePolicy',
      'DeleteResourcePolicy',
    ]);
  });

  describe.each(watchedFunctions(ENV))('$functionName', ({ functionName, roleName }) => {
    it.each(FUNCTION_CODE_EVENT_NAMES)('matches %s by name', (eventName) => {
      expect(matchesFunctionRules(lambda(eventName, { functionName }))).toBe(true);
    });

    it.each(FUNCTION_CODE_EVENT_NAMES)('matches %s by full and partial ARN', (eventName) => {
      expect(matchesFunctionRules(lambda(eventName, { functionName: functionArn(functionName) }))).toBe(true);
      expect(matchesFunctionRules(lambda(eventName, { functionName: `${account}:function:${functionName}` }))).toBe(true);
    });

    it.each(FUNCTION_POLICY_EVENT_NAMES)('matches %s naming it as its API does', (eventName) => {
      const parameters = byResourceArn(eventName)
        ? { resourceArn: functionArn(functionName) }
        : { functionName, statementId: 'fictional', action: 'lambda:InvokeFunction', principal: '*' };
      expect(matchesPolicyRules(lambda(eventName, parameters))).toBe(true);
    });

    it.each(FUNCTION_POLICY_EVENT_NAMES.filter((name) => !byResourceArn(name)))(
      'matches %s by full and partial ARN',
      (eventName) => {
        expect(matchesPolicyRules(lambda(eventName, { functionName: functionArn(functionName) }))).toBe(true);
        expect(matchesPolicyRules(lambda(eventName, { functionName: `${account}:function:${functionName}` }))).toBe(true);
      },
    );

    it.each(['CreateEventSourceMapping20150331', 'UpdateEventSourceMapping20150331', 'CreateEventSourceMapping'])(
      'matches %s attaching an event source to it, by name and by ARN',
      (eventName) => {
        expect(matchesFunctionRules(lambda(eventName, { functionName, eventSourceArn: 'arn:aws:sqs:x' }))).toBe(true);
        expect(matchesFunctionRules(lambda(eventName, { functionName: functionArn(functionName) }))).toBe(true);
      },
    );

    it('does not match an event source mapping update that names no function', () => {
      expect(matchesFunctionRules(lambda('UpdateEventSourceMapping20150331', { uUID: 'fictional', batchSize: 5 }))).toBe(false);
    });

    it('matches a date-versioned resource-policy event name', () => {
      expect(matchesPolicyRules(lambda('PutResourcePolicy20260709', { resourceArn: functionArn(functionName) }))).toBe(true);
      expect(matchesPolicyRules(lambda('DeleteResourcePolicy20260709', { resourceArn: functionArn(functionName) }))).toBe(true);
    });

    it('matches a new function created with its role', () => {
      expect(matchesFunctionRules(lambda('CreateFunction20150331', {
        functionName: 'fictional-impostor',
        role: roleArn(roleName),
      }))).toBe(true);
    });

    it('matches another function reconfigured to its role', () => {
      expect(matchesFunctionRules(lambda('UpdateFunctionConfiguration20150331v2', {
        functionName: 'fictional-impostor',
        role: roleArn(roleName),
      }))).toBe(true);
    });
  });

  it.each([
    ['an unwatched function', lambda('UpdateFunctionCode20150331v2', { functionName: 'adlytics-dev-InsightContinuation' })],
    ['an unwatched role', lambda('CreateFunction20150331', { functionName: 'fictional', role: roleArn('fictional-role') })],
    ['a watched name used as a suffix of another', lambda('UpdateFunctionCode20150331v2', { functionName: `x${watchedFunctions(ENV)[0]?.functionName}` })],
    ['an invoke', lambda('Invoke', { functionName: watchedFunctions(ENV)[0]?.functionName })],
    ['an unversioned event name', lambda('UpdateFunctionCode', { functionName: watchedFunctions(ENV)[0]?.functionName })],
    ['a permission on an unwatched function', lambda('AddPermission20150331v2', { functionName: 'adlytics-dev-InsightContinuation' })],
    ['a resource policy on an unwatched function', lambda('PutResourcePolicy', { resourceArn: functionArn('adlytics-dev-InsightContinuation') })],
    ['a resource-policy read', lambda('GetResourcePolicy', { resourceArn: functionArn(watchedFunctions(ENV)[0]?.functionName ?? '') })],
    ['a policy read', lambda('GetPolicy20150331v2', { functionName: watchedFunctions(ENV)[0]?.functionName })],
    ['a layer permission', lambda('AddLayerVersionPermission20181031', { layerName: watchedFunctions(ENV)[0]?.functionName })],
  ])('does not match %s', (_label, event) => {
    expect(matchesFunctionRules(event)).toBe(false);
  });
});

describe('the backup job state rule pattern', () => {
  const pattern = { source: ['aws.backup'], 'detail-type': [BACKUP_JOB_DETAIL_TYPE], detail: backupJobStateDetail(TABLES) };
  const jobState = (resourceArn: string | undefined, state: string): Record<string, unknown> => ({
    version: '0',
    id: '00000000-0000-4000-8000-000000000001',
    'detail-type': BACKUP_JOB_DETAIL_TYPE,
    source: 'aws.backup',
    account,
    time: '2026-09-27T00:00:00Z',
    region,
    resources: [],
    detail: { backupJobId: 'fictional-job', state, ...(resourceArn === undefined ? {} : { resourceArn, resourceType: 'DynamoDB' }) },
  });

  it.each(['PENDING', 'RUNNING', 'COMPLETED', 'FAILED'])('matches a plan-driven %s job on a record table', (state) => {
    expect(matchesEventPattern(pattern, jobState(AUDIT_ARN, state))).toBe(true);
  });

  it.each([
    ['a job on another table', jobState(`${AUDIT_ARN}-copy`, 'RUNNING')],
    ['a CREATED event, which carries no resource', jobState(undefined, 'CREATED')],
    ['a CloudTrail call', cloudTrail('aws.backup', 'backup.amazonaws.com', 'StartBackupJob', { resourceArn: AUDIT_ARN })],
  ])('does not match %s', (_label, event) => {
    expect(matchesEventPattern(pattern, event)).toBe(false);
  });
});

describe('the role-change rule pattern', () => {
  const pattern = {
    source: ['aws.iam'],
    'detail-type': [CLOUDTRAIL_DETAIL_TYPE],
    detail: roleChangeDetail(watchedFunctions(ENV), watchedEvaluationRoles(ENV), CFN_EXEC_ROLE_NAME),
  };
  const iamCall = (eventName: string, roleName: string): Record<string, unknown> =>
    cloudTrail('aws.iam', 'iam.amazonaws.com', eventName, { roleName });
  const roles = [
    ...new Set(watchedFunctions(ENV).map((fn) => fn.roleName)),
    ...watchedEvaluationRoles(ENV),
    CFN_EXEC_ROLE_NAME,
  ];

  it('names every writer, reader, conversation, and review role, and the execution role', () => {
    expect((pattern.detail.requestParameters as { roleName: string[] }).roleName.sort()).toStrictEqual([...roles].sort());
    for (const table of recordTables(ENV, true)) expect(roles).toContain(table.writerRoleName);
  });

  it('names both evaluation execution roles, each its result group\'s only writer (ADR-079)', () => {
    for (const family of EVALUATION_FAMILIES) {
      expect(roles).toContain(evaluationExecutionRoleName(ENV, family));
      expect(watchedFunctions(ENV).map((fn) => fn.roleName)).not.toContain(evaluationExecutionRoleName(ENV, family));
    }
  });

  describe.each(roles)('%s', (roleName) => {
    it.each(ROLE_CHANGE_EVENT_NAMES)('matches %s', (eventName) => {
      expect(matchesEventPattern(pattern, iamCall(eventName, roleName))).toBe(true);
    });
  });

  it.each([
    ['another role', iamCall('UpdateAssumeRolePolicy', 'fictional-role')],
    ['a role sharing a name prefix', iamCall('CreateRole', `${roles[0]}-copy`)],
    ['a read', iamCall('GetRole', roles[0] ?? '')],
    ['a tag change', iamCall('TagRole', roles[0] ?? '')],
  ])('does not match %s', (_label, event) => {
    expect(matchesEventPattern(pattern, event)).toBe(false);
  });
});

describe('the detection self-watch rule patterns', () => {
  const patternOf = (service: (typeof DETECTION_SERVICES)[number]): Record<string, unknown> => {
    const { source, detail } = detectionChangeDetail(ENV, service);
    return { ...(source === undefined ? {} : { source: [source] }), 'detail-type': [CLOUDTRAIL_DETAIL_TYPE], detail };
  };
  const RULE = 'adlytics-dev-record-tables-receipts';
  const PIPE = 'adlytics-dev-record-receipts';
  const ALARM = 'adlytics-dev-record-item-changes';
  const GROUP = '/adlytics/dev/record-integrity/item-changes';

  it.each([
    ['events', cloudTrail('aws.events', 'events.amazonaws.com', 'DisableRule', { name: RULE })],
    ['events', cloudTrail('aws.events', 'events.amazonaws.com', 'DeleteRule', { name: RULE })],
    ['events', cloudTrail('aws.events', 'events.amazonaws.com', 'PutRule', { name: RULE, eventPattern: '{}' })],
    ['events', cloudTrail('aws.events', 'events.amazonaws.com', 'RemoveTargets', { rule: RULE, ids: ['Target0'] })],
    ['events', cloudTrail('aws.events', 'events.amazonaws.com', 'PutTargets', { rule: RULE, targets: [] })],
    ['pipes', cloudTrail('aws.pipes', 'pipes.amazonaws.com', 'StopPipe', { name: PIPE })],
    ['pipes', cloudTrail('aws.pipes', 'pipes.amazonaws.com', 'UpdatePipe', { name: PIPE })],
    ['pipes', cloudTrail('aws.pipes', 'pipes.amazonaws.com', 'DeletePipe', { name: PIPE })],
    ['cloudwatch', cloudTrail('aws.cloudwatch', 'monitoring.amazonaws.com', 'DisableAlarmActions', { alarmNames: ['fictional', ALARM] })],
    ['cloudwatch', cloudTrail('aws.cloudwatch', 'monitoring.amazonaws.com', 'DeleteAlarms', { alarmNames: [ALARM] })],
    ['cloudwatch', cloudTrail('aws.cloudwatch', 'monitoring.amazonaws.com', 'PutMetricAlarm', { alarmName: ALARM })],
    ['logs', cloudTrail('aws.logs', 'logs.amazonaws.com', 'DeleteMetricFilter', { logGroupName: GROUP, filterName: 'fictional' })],
    ['logs', cloudTrail('aws.logs', 'logs.amazonaws.com', 'DeleteLogGroup', { logGroupName: GROUP })],
    ['logs', cloudTrail('aws.logs', 'logs.amazonaws.com', 'PutRetentionPolicy', { logGroupName: GROUP, retentionInDays: 1 })],
    ['logs', cloudTrail('aws.logs', 'logs.amazonaws.com', 'PutMetricFilter', { logGroupName: GROUP, filterName: 'x', filterPattern: 'never', metricTransformations: [] })],
    ['logs', cloudTrail('aws.logs', 'logs.amazonaws.com', 'DeleteResourcePolicy', { policyName: 'adlytics-dev-record-integrity-rules' })],
  ] as const)('the %s rule matches %j', (service, event) => {
    expect(matchesEventPattern(patternOf(service), event)).toBe(true);
  });

  it.each([
    ['events', cloudTrail('aws.events', 'events.amazonaws.com', 'DisableRule', { name: 'adlytics-dev-other' })],
    ['events', cloudTrail('aws.events', 'events.amazonaws.com', 'DescribeRule', { name: RULE })],
    ['pipes', cloudTrail('aws.pipes', 'pipes.amazonaws.com', 'StopPipe', { name: 'adlytics-prod-record-receipts' })],
    ['pipes', cloudTrail('aws.pipes', 'pipes.amazonaws.com', 'StartPipe', { name: PIPE })],
    ['cloudwatch', cloudTrail('aws.cloudwatch', 'monitoring.amazonaws.com', 'DeleteAlarms', { alarmNames: ['adlytics-dev-other'] })],
    ['cloudwatch', cloudTrail('aws.cloudwatch', 'monitoring.amazonaws.com', 'EnableAlarmActions', { alarmNames: [ALARM] })],
    ['logs', cloudTrail('aws.logs', 'logs.amazonaws.com', 'DeleteLogGroup', { logGroupName: '/aws/lambda/fictional' })],
    ['logs', cloudTrail('aws.logs', 'logs.amazonaws.com', 'CreateLogStream', { logGroupName: GROUP })],
    ['logs', cloudTrail('aws.logs', 'logs.amazonaws.com', 'PutMetricFilter', { logGroupName: '/aws/lambda/fictional', filterName: 'x' })],
    ['logs', cloudTrail('aws.logs', 'logs.amazonaws.com', 'DeleteResourcePolicy', { policyName: 'adlytics-dev-other' })],
    ['logs', cloudTrail('aws.logs', 'logs.amazonaws.com', 'DeleteResourcePolicy', { policyName: 'adlytics-prod-record-integrity-rules' })],
    ['logs', cloudTrail('aws.logs', 'logs.amazonaws.com', 'DescribeResourcePolicies', { policyName: 'adlytics-dev-record-integrity-rules' })],
    // A policy name is matched only on its own call: a group call does not match by policy name.
    ['logs', cloudTrail('aws.logs', 'logs.amazonaws.com', 'DeleteLogGroup', { policyName: 'adlytics-dev-record-integrity-rules' })],
  ] as const)('the %s rule does not match %j', (service, event) => {
    expect(matchesEventPattern(patternOf(service), event)).toBe(false);
  });
});

describe('the release-record rule pattern (ADR-079)', () => {
  const pattern = {
    source: ['aws.ssm'],
    'detail-type': [CLOUDTRAIL_DETAIL_TYPE],
    detail: releaseRecordDetail(ENV, region, account),
  };
  const gateRole = (family: (typeof EVALUATION_FAMILIES)[number]): string =>
    `arn:aws:iam::${account}:role/${evaluationGateProviderRoleName(ENV, family)}`;
  /** A Parameter Store call by a caller whose identity is `userIdentity`. */
  const ssmCall = (
    eventName: string,
    requestParameters: Record<string, unknown>,
    userIdentity: Record<string, unknown>,
  ): Record<string, unknown> => {
    const event = cloudTrail('aws.ssm', 'ssm.amazonaws.com', eventName, requestParameters);
    return { ...event, detail: { ...(event.detail as Record<string, unknown>), userIdentity } };
  };
  const assumed = (issuer: string): Record<string, unknown> => ({
    type: 'AssumedRole',
    arn: `arn:aws:sts::${account}:assumed-role/x/session`,
    sessionContext: { sessionIssuer: { type: 'Role', arn: issuer }, attributes: { mfaAuthenticated: 'false' } },
  });
  const operator = assumed(`arn:aws:iam::${account}:role/fictional-operator`);
  // A retired family's record name still falls under the release-record prefix, so a write to it alarms.
  const releaseV3 = evaluationReleaseParameterName(ENV, 'v4').replace(/v4$/, 'v3');
  const releaseV4 = evaluationReleaseParameterName(ENV, 'v4');
  const arnOf = (name: string): string => `arn:aws:ssm:${region}:${account}:parameter${name}`;

  it.each(RELEASE_RECORD_EVENT_NAMES.filter((name) => name !== 'DeleteParameters'))(
    'matches %s of a release record by another role',
    (eventName) => {
      expect(matchesEventPattern(pattern, ssmCall(eventName, { name: releaseV3 }, operator))).toBe(true);
    },
  );

  it.each([
    ['DeleteParameters naming a release record', ssmCall('DeleteParameters', { names: ['/fictional', releaseV4] }, operator)],
    ['a write by the release record ARN', ssmCall('PutParameter', { name: arnOf(releaseV4), type: 'String' }, operator)],
    ['a write by an IAM user, which has no session', ssmCall('PutParameter', { name: releaseV3 }, { type: 'IAMUser', arn: `arn:aws:iam::${account}:user/fictional` })],
    ['a write by the root user', ssmCall('DeleteParameter', { name: releaseV3 }, { type: 'Root', arn: `arn:aws:iam::${account}:root` })],
    ['a write by the retired V3 gate role, which is no longer exempt', ssmCall('PutParameter', { name: releaseV4 }, assumed(gateRole('v4').replace(/v4$/, 'v3')))],
    ['a GetSessionToken session, which has no issuer', ssmCall('PutParameter', { name: releaseV3 }, { type: 'IAMUser', sessionContext: { attributes: { mfaAuthenticated: 'true' } } })],
  ])('matches %s', (_label, event) => {
    expect(matchesEventPattern(pattern, event)).toBe(true);
  });

  it.each([
    ['the V4 gate deleting its record', ssmCall('DeleteParameters', { names: [releaseV4] }, assumed(gateRole('v4')))],
    // A current gate role is exempt on any record: the rule names the gate roles, and each gate's
    // IAM grant is what confines it to its own record.
    ['the V4 gate writing a retired family\'s record', ssmCall('PutParameter', { name: releaseV3 }, assumed(gateRole('v4')))],
    ['another parameter', ssmCall('PutParameter', { name: paramPath(ENV, 'evaluationV4ResultLogGroupName') }, operator)],
    ['another environment\'s record', ssmCall('PutParameter', { name: evaluationReleaseParameterName('prod', 'v4') }, operator)],
    ['a read', ssmCall('GetParameters', { names: [releaseV3] }, operator)],
  ])('does not match %s', (_label, event) => {
    expect(matchesEventPattern(pattern, event)).toBe(false);
  });
});

describe('the result-group rule pattern (ADR-079)', () => {
  const pattern = {
    source: ['aws.logs'],
    'detail-type': [CLOUDTRAIL_DETAIL_TYPE],
    detail: resultGroupDetail(ENV, region, account),
  };
  const logsCall = (eventName: string, requestParameters: Record<string, unknown>): Record<string, unknown> =>
    cloudTrail('aws.logs', 'logs.amazonaws.com', eventName, requestParameters);
  // A retired family's group name still falls under the result-group prefix, so its deletion alarms.
  const v3 = evaluationResultLogGroupName(ENV, 'v4').replace(/v4$/, 'v3');
  const v4 = evaluationResultLogGroupName(ENV, 'v4');
  const arn = (name: string): string => `arn:aws:logs:${region}:${account}:log-group:${name}`;
  /** Each group action's request parameters naming a result group, per its API's request syntax. */
  const byAction: Record<string, Record<string, unknown>> = {
    PutSubscriptionFilter: { logGroupName: v3, filterName: 'x', destinationArn: 'arn:aws:lambda:x' },
    DeleteSubscriptionFilter: { logGroupName: v4, filterName: 'x' },
    CreateExportTask: { logGroupName: v3, destination: 'fictional-bucket' },
    StartLiveTail: { logGroupIdentifiers: [arn('/aws/lambda/fictional'), arn(v4)] },
    AssociateKmsKey: { logGroupName: v3, kmsKeyId: 'arn:aws:kms:x' },
    DisassociateKmsKey: { resourceIdentifier: `${arn(v4)}:*` },
    PutRetentionPolicy: { logGroupName: v3, retentionInDays: 1 },
    DeleteRetentionPolicy: { logGroupName: v4 },
    DeleteLogGroup: { logGroupName: v3 },
    PutLogGroupDeletionProtection: { logGroupIdentifier: arn(v3), deletionProtectionEnabled: false },
    PutDataProtectionPolicy: { logGroupIdentifier: v4, policyDocument: '{}' },
    DeleteDataProtectionPolicy: { logGroupIdentifier: arn(v3) },
    PutResourcePolicy: { policyName: 'x', policyDocument: '{}', resourceArn: arn(v4) },
    PutTransformer: { logGroupIdentifier: v3, transformerConfig: [{ parseJSON: {} }] },
    DeleteTransformer: { logGroupIdentifier: arn(v4) },
  };

  it('names every result-group action observability §3.7 lists, and the account-level ones', () => {
    expect(Object.keys(byAction).sort()).toStrictEqual([...RESULT_GROUP_EVENT_NAMES].sort());
    expect((pattern.detail.eventName as string[]).sort()).toStrictEqual(
      [...RESULT_GROUP_EVENT_NAMES, ...ACCOUNT_LOGS_EVENT_NAMES].sort(),
    );
  });

  it.each(RESULT_GROUP_EVENT_NAMES)('matches %s naming a result group', (eventName) => {
    expect(matchesEventPattern(pattern, logsCall(eventName, byAction[eventName]!))).toBe(true);
  });

  it.each(RESULT_GROUP_EVENT_NAMES)('does not match %s naming another group', (eventName) => {
    const other = JSON.parse(JSON.stringify(byAction[eventName]!).split('/evaluation/results-').join('/evaluation/other-')) as Record<string, unknown>;
    expect(matchesEventPattern(pattern, logsCall(eventName, other))).toBe(false);
  });

  it.each([
    ['PutAccountPolicy', { policyName: 'x', policyType: 'SUBSCRIPTION_FILTER_POLICY', policyDocument: '{}' }],
    ['DeleteAccountPolicy', { policyName: 'x', policyType: 'DATA_PROTECTION_POLICY' }],
    ['PutDeliveryDestination', { name: 'x', deliveryDestinationConfiguration: {} }],
    ['PutResourcePolicy', { policyName: 'x', policyDocument: '{}' }],
  ])('matches the account-level %s whatever it names', (eventName, requestParameters) => {
    expect(matchesEventPattern(pattern, logsCall(eventName, requestParameters))).toBe(true);
  });

  it.each([
    ['a read of a result group', logsCall('DescribeSubscriptionFilters', { logGroupName: v3 })],
    ['a write to a result group', logsCall('CreateLogStream', { logGroupName: v3, logStreamName: 'x' })],
    ['another environment\'s group', logsCall('DeleteLogGroup', { logGroupName: evaluationResultLogGroupName('prod', 'v4') })],
    ['the same name in another account', logsCall('PutLogGroupDeletionProtection', { logGroupIdentifier: `arn:aws:logs:${region}:999999999999:log-group:${v3}` })],
    ['a log-group resource policy on another group', logsCall('PutResourcePolicy', { policyName: 'x', resourceArn: arn('/aws/lambda/fictional') })],
  ])('does not match %s', (_label, event) => {
    expect(matchesEventPattern(pattern, event)).toBe(false);
  });
});

describe('writer input queues admit only their producer (ADR-080 §3)', () => {
  const producerStatement = (stack: string, queueName: string, producerRole: string): void => {
    const queues = Object.entries(resources(stack)).filter(
      ([, resource]) => resource.Type === 'AWS::SQS::Queue' && resource.Properties.QueueName === queueName,
    );
    expect(queues).toHaveLength(1);
    const [queueId] = queues[0] ?? [];
    const policies = Object.values(resources(stack)).filter(
      (resource) => resource.Type === 'AWS::SQS::QueuePolicy'
        && JSON.stringify(resource.Properties.Queues) === JSON.stringify([{ Ref: queueId }]),
    );
    expect(policies).toHaveLength(1);
    const statements = (policies[0]?.Properties.PolicyDocument as { Statement: Record<string, unknown>[] }).Statement;
    expect(render(statements.find((statement) => statement.Sid === 'OnlyTheProducerSends'))).toStrictEqual({
      Sid: 'OnlyTheProducerSends',
      Effect: 'Deny',
      Principal: '*',
      Action: 'sqs:SendMessage',
      Resource: { 'Fn::GetAtt': [queueId, 'Arn'] },
      Condition: {
        ArnNotEquals: { 'aws:PrincipalArn': `arn:\${AWS::Partition}:iam::${account}:role/${producerRole}` },
      },
    });
    // Deny-only: the queue policy grants nothing, so the producer's identity policy is the grant.
    expect(statements.filter((statement) => statement.Effect !== 'Deny')).toStrictEqual([]);
  };

  it('the evaluation input queue denies SendMessage to all but the outbox dispatcher', () => {
    producerStatement('EvaluationInputStack', 'adlytics-dev-evaluation-input', 'adlytics-dev-evaluation-outbox-dispatcher');
  });

  it('the result projection queue denies SendMessage to all but the result ingress', () => {
    producerStatement(
      'EvaluationResultLinkV3Stack',
      'adlytics-dev-evaluation-result-projection-v3',
      'adlytics-dev-evaluation-result-ingress',
    );
  });
});
