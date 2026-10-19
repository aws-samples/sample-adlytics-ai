// SPDX-License-Identifier: MIT-0
/**
 * ADR-080 §2 and §4: the one-writer resource policy every record table carries, and the event
 * patterns tamper detection alarms on.
 *
 * This module is the only home of which role writes which record table, which actions the policy
 * denies, which role may send to a writer's input queue, and which functions hold a role a policy
 * names. The table stacks and the tamper stack both read it, so a table cannot gain a policy the
 * alarms do not watch, or the reverse.
 *
 * The pattern builders take plain names so tests can match them against sample events offline;
 * every value they render is a literal, never a token.
 */
import * as cdk from 'aws-cdk-lib';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as sqs from 'aws-cdk-lib/aws-sqs';

import {
  agentQualityAuditTableName,
  agentQualityFindingsTableName,
  agentQualityProcessorFunctionName,
  agentQualityProcessorRoleName,
  conversationFunctionName,
  conversationFunctionRoleName,
  evaluationDeliveriesTableName,
  EVALUATION_FAMILIES,
  EvaluationFamily,
  evaluationExecutionRoleName,
  evaluationGateProviderFunctionName,
  evaluationGateProviderRoleName,
  evaluationReleaseParameterName,
  evaluationResultLogGroupName,
  evaluationOutboxDispatcherFunctionName,
  evaluationOutboxDispatcherRoleName,
  evaluationResultIngressRoleName,
  evaluationResultIngressV3FunctionName,
  evaluationResultLinkerRoleName,
  evaluationResultLinkerV3FunctionName,
  evaluationVerdictsTableName,
  evaluationWriterFunctionName,
  evaluationWriterReceiptsTableName,
  evaluationWriterRoleName,
  fixedRoleArn,
  ParamKey,
  qualityReviewFunctionName,
  qualityReviewFunctionRoleName,
} from '../../config';

/** Statement 1: denied to every principal but the table's one writer role. */
export const WRITER_ONLY_ACTIONS = ['dynamodb:PutItem', 'dynamodb:PartiQLInsert'] as const;

/** Statement 2: denied to every principal, the writer included. Records are immutable. */
export const IMMUTABLE_ACTIONS = [
  'dynamodb:BatchWriteItem',
  'dynamodb:UpdateItem',
  'dynamodb:DeleteItem',
  'dynamodb:PartiQLUpdate',
  'dynamodb:PartiQLDelete',
] as const;

/** Statement 3: every action that copies the table's data out, denied to every principal. */
export const DATA_COPY_ACTIONS = [
  'dynamodb:CreateBackup',
  'dynamodb:RestoreTableToPointInTime',
  'dynamodb:ExportTableToPointInTime',
  'dynamodb:EnableKinesisStreamingDestination',
  'dynamodb:DisableKinesisStreamingDestination',
  'dynamodb:UpdateKinesisStreamingDestination',
] as const;

/**
 * Statement 4: control-plane changes, denied to every principal but the stack's CloudFormation
 * execution role. The exception is also what keeps the policy deployable: DynamoDB refuses a
 * policy that would stop its caller from changing the policy again.
 */
export const DEPLOYMENT_ONLY_ACTIONS = [
  'dynamodb:PutResourcePolicy',
  'dynamodb:DeleteResourcePolicy',
  'dynamodb:UpdateTable',
  'dynamodb:DeleteTable',
  'dynamodb:UpdateTimeToLive',
  'dynamodb:UpdateContinuousBackups',
] as const;

/**
 * The receipts table's read deny (ADR-078, ADR-080 §2), statement `OnlyNamedReadersRead`: every
 * action that reads an item, denied to every principal but the named reader roles. Only the
 * receipts table holds the principal, so only it carries this statement. Stream reads are not item
 * reads and are not covered: the statement's resources are the table and its indexes.
 */
export const RECEIPT_READ_ACTIONS = [
  'dynamodb:GetItem',
  'dynamodb:BatchGetItem',
  'dynamodb:Query',
  'dynamodb:Scan',
  'dynamodb:ConditionCheckItem',
  'dynamodb:PartiQLSelect',
] as const;
/** The only roles the receipts read deny exempts: the writer and the four readers. */
export const RECEIPT_READERS: readonly ((envName: string) => string)[] = [
  conversationFunctionRoleName,
  agentQualityProcessorRoleName,
  evaluationOutboxDispatcherRoleName,
  evaluationResultIngressRoleName,
  qualityReviewFunctionRoleName,
];
/** The receipt readers' fixed role ARNs, buildable before any of their stacks deploys. */
export function receiptReaderRoleArns(partition: string, account: string, envName: string): string[] {
  return RECEIPT_READERS.map((roleName) => fixedRoleArn(partition, account, roleName(envName)));
}
export type RecordFamily = 'receipts' | 'findings' | 'deliveries' | 'writerReceipts' | 'verdicts';

export interface RecordTable {
  readonly family: RecordFamily;
  readonly tableName: string;
  readonly writerRoleName: string;
  /** The KEYS_ONLY stream ARN its owning stack publishes, which its item-change pipe reads. */
  readonly streamArnKey: ParamKey;
  /** Owned by `EvaluationEvidenceStack`, which exists only when evaluation input is provisioned. */
  readonly evaluation: boolean;
  /** Set only on a table whose item reads are denied to all but these roles: the receipts. */
  readonly readerRoleNames?: readonly string[];
}

/** ADR-080 §1: every record table and its only writer. */
export function recordTable(envName: string, family: RecordFamily): RecordTable {
  switch (family) {
    case 'receipts':
      return {
        family,
        tableName: agentQualityAuditTableName(envName),
        writerRoleName: conversationFunctionRoleName(envName),
        streamArnKey: 'agentQualityAuditStreamArn',
        evaluation: false,
        readerRoleNames: RECEIPT_READERS.map((roleName) => roleName(envName)),
      };
    case 'findings':
      return {
        family,
        tableName: agentQualityFindingsTableName(envName),
        writerRoleName: agentQualityProcessorRoleName(envName),
        streamArnKey: 'agentQualityFindingsStreamArn',
        evaluation: false,
      };
    case 'deliveries':
      return {
        family,
        tableName: evaluationDeliveriesTableName(envName),
        writerRoleName: evaluationOutboxDispatcherRoleName(envName),
        streamArnKey: 'evaluationDeliveriesStreamArn',
        evaluation: true,
      };
    case 'writerReceipts':
      return {
        family,
        tableName: evaluationWriterReceiptsTableName(envName),
        writerRoleName: evaluationWriterRoleName(envName),
        streamArnKey: 'evaluationWriterReceiptsStreamArn',
        evaluation: true,
      };
    case 'verdicts':
      return {
        family,
        tableName: evaluationVerdictsTableName(envName),
        writerRoleName: evaluationResultLinkerRoleName(envName),
        streamArnKey: 'evaluationVerdictsStreamArn',
        evaluation: true,
      };
  }
}

export const RECORD_FAMILIES: readonly RecordFamily[] = [
  'receipts',
  'findings',
  'deliveries',
  'writerReceipts',
  'verdicts',
];

/** A family as a name segment: `writerReceipts` becomes `writer-receipts`. */
export function familySlug(family: RecordFamily): string {
  return family.replace(/[A-Z]/g, (c) => `-${c.toLowerCase()}`);
}

/** The record tables a configuration composes. */
export function recordTables(envName: string, evaluationProvisioned: boolean): RecordTable[] {
  return RECORD_FAMILIES.map((family) => recordTable(envName, family)).filter(
    (table) => evaluationProvisioned || !table.evaluation,
  );
}

/**
 * The exact CloudFormation execution role a stack deploys with, from its own synthesizer. The
 * synthesizer returns the ARN with pseudo-parameter placeholders, so it is wrapped in `Fn::Sub`.
 * Any other synthesizer fails synth: the one principal allowed to change the table would then be
 * unknown.
 */
export function cloudFormationExecutionRoleArn(stack: cdk.Stack): string {
  return cdk.Fn.sub(defaultSynthesizer(stack).cloudFormationExecutionRoleArn);
}

function defaultSynthesizer(stack: cdk.Stack): cdk.DefaultStackSynthesizer {
  const synthesizer = stack.synthesizer;
  if (!(synthesizer instanceof cdk.DefaultStackSynthesizer)) {
    throw new Error(
      `${stack.stackName} needs the DefaultStackSynthesizer to name the one CloudFormation `
      + 'execution role that may change its record tables.',
    );
  }
  return synthesizer;
}

/**
 * The CloudFormation execution role's name, as a literal. Every stack in the app uses the same
 * default synthesizer, so the role the tamper stack names is the one the table stacks exempt. The
 * synthesizer renders the account and Region literally when the stack's environment is explicit;
 * a placeholder left in the name would make the IAM rule match nothing, so it fails synth.
 */
export function cloudFormationExecutionRoleName(stack: cdk.Stack): string {
  const arn = defaultSynthesizer(stack).cloudFormationExecutionRoleArn;
  const name = arn.slice(arn.indexOf(':role/') + ':role/'.length);
  if (!arn.includes(':role/') || name.includes('${')) {
    throw new Error(
      `${stack.stackName} needs an explicit account and Region to name the CloudFormation `
      + `execution role literally; the synthesizer rendered ${arn}.`,
    );
  }
  return name;
}

/**
 * ADR-080 §3: a writer's input queue admits `SendMessage` only from its one producer role. The
 * table policy fixes who writes a record; this fixes who can tell that writer what to write.
 * Deny-only, so the producer's identity policy still grants the send.
 */
export function onlyProducerSends(queue: sqs.Queue, producerRoleName: string): void {
  const stack = cdk.Stack.of(queue);
  queue.addToResourcePolicy(new iam.PolicyStatement({
    sid: 'OnlyTheProducerSends',
    effect: iam.Effect.DENY,
    principals: [new iam.StarPrincipal()],
    actions: ['sqs:SendMessage'],
    resources: [queue.queueArn],
    conditions: {
      ArnNotEquals: { 'aws:PrincipalArn': fixedRoleArn(stack.partition, stack.account, producerRoleName) },
    },
  }));
}

/**
 * ADR-080 §2: four Deny statements on the table and its indexes, and nothing allowed, plus the
 * read deny on the receipts table. Readers and the writer are granted by their identity policies;
 * the policy only takes away.
 */
export function recordTablePolicy(stack: cdk.Stack, table: RecordTable): iam.PolicyDocument {
  const tableArn = `arn:${stack.partition}:dynamodb:${stack.region}:${stack.account}:table/${table.tableName}`;
  const resources = [tableArn, `${tableArn}/index/*`];
  const deny = (
    sid: string,
    actions: readonly string[],
    exceptPrincipalArn?: string | readonly string[],
  ): iam.PolicyStatement => new iam.PolicyStatement({
    sid,
    effect: iam.Effect.DENY,
    principals: [new iam.StarPrincipal()],
    actions: [...actions],
    resources,
    ...(exceptPrincipalArn === undefined
      ? {}
      : {
          conditions: {
            ArnNotEquals: {
              'aws:PrincipalArn': typeof exceptPrincipalArn === 'string'
                ? exceptPrincipalArn
                : [...exceptPrincipalArn],
            },
          },
        }),
  });
  const readers = table.readerRoleNames;
  return new iam.PolicyDocument({
    statements: [
      deny(
        'OnlyTheWriterPuts',
        WRITER_ONLY_ACTIONS,
        fixedRoleArn(stack.partition, stack.account, table.writerRoleName),
      ),
      deny('RecordsAreImmutable', IMMUTABLE_ACTIONS),
      deny('NoDataLeavesTheTable', DATA_COPY_ACTIONS),
      deny(
        'OnlyTheDeploymentChangesTheTable',
        DEPLOYMENT_ONLY_ACTIONS,
        cloudFormationExecutionRoleArn(stack),
      ),
      ...(readers === undefined
        ? []
        : [deny(
            'OnlyNamedReadersRead',
            RECEIPT_READ_ACTIONS,
            readers.map((roleName) => fixedRoleArn(stack.partition, stack.account, roleName)),
          )]),
    ],
  });
}

/** A function that runs under a role some record-table policy or a release record names. */
export interface WatchedFunction {
  readonly functionName: string;
  readonly roleName: string;
}

/**
 * Every function whose role a record-table policy or a release record names, whether or not its
 * stack is composed. The gate providers are here because the release-record rule exempts their
 * roles: whoever runs code as one writes a release record without an alarm (ADR-079).
 */
export function watchedFunctions(envName: string): WatchedFunction[] {
  const gate = (family: EvaluationFamily): WatchedFunction => ({
    functionName: evaluationGateProviderFunctionName(envName, family),
    roleName: evaluationGateProviderRoleName(envName, family),
  });
  return [
    { functionName: conversationFunctionName(envName), roleName: conversationFunctionRoleName(envName) },
    { functionName: agentQualityProcessorFunctionName(envName), roleName: agentQualityProcessorRoleName(envName) },
    { functionName: evaluationOutboxDispatcherFunctionName(envName), roleName: evaluationOutboxDispatcherRoleName(envName) },
    { functionName: evaluationWriterFunctionName(envName), roleName: evaluationWriterRoleName(envName) },
    { functionName: evaluationResultIngressV3FunctionName(envName), roleName: evaluationResultIngressRoleName(envName) },
    { functionName: evaluationResultLinkerV3FunctionName(envName), roleName: evaluationResultLinkerRoleName(envName) },
    { functionName: qualityReviewFunctionName(envName), roleName: qualityReviewFunctionRoleName(envName) },
    ...EVALUATION_FAMILIES.map(gate),
  ];
}

/* -------------------------------------------------------------------------
 * Event patterns
 * ---------------------------------------------------------------------- */

export const CLOUDTRAIL_DETAIL_TYPE = 'AWS API Call via CloudTrail';

const ITEM_MUTATIONS = ['MODIFY', 'REMOVE'];

/**
 * The item-change pipe's filters, any one of which admits a record: a `MODIFY` or `REMOVE` whose
 * user identity is not the TTL service's. A TTL deletion carries `userIdentity.type` "Service"
 * and `userIdentity.principalId` "dynamodb.amazonaws.com"; every other record carries no user
 * identity. The four filters are "not (type is Service and principal is DynamoDB)" written as
 * alternatives, each absence spelled out, because `anything-but` does not match an absent field.
 */
export const ITEM_CHANGE_FILTERS: readonly Record<string, unknown>[] = [
  { eventName: ITEM_MUTATIONS, userIdentity: { principalId: [{ exists: false }] } },
  { eventName: ITEM_MUTATIONS, userIdentity: { principalId: [{ 'anything-but': ['dynamodb.amazonaws.com'] }] } },
  { eventName: ITEM_MUTATIONS, userIdentity: { type: [{ exists: false }] } },
  { eventName: ITEM_MUTATIONS, userIdentity: { type: [{ 'anything-but': ['Service'] }] } },
];

const withoutPrefix = (action: string): string => action.replace(/^dynamodb:/, '');

/**
 * The CloudTrail event names of statement 3 and 4 actions. AWS Backup's permission-only actions
 * cannot appear in a table policy (DynamoDB rejects them), so the backup rule below watches the
 * AWS Backup call itself.
 */
export const CONTROL_PLANE_EVENT_NAMES: readonly string[] = [
  ...DATA_COPY_ACTIONS,
  ...DEPLOYMENT_ONLY_ACTIONS,
].map(withoutPrefix);

/**
 * A table named directly or by ARN. The `TableName` parameters accept either, and matching an ARN
 * by its `:table/<name>` suffix keeps the pattern free of the account and Region.
 */
const tableReference = (tableNames: readonly string[]): unknown[] => [
  ...tableNames,
  ...tableNames.map((name) => ({ suffix: `:table/${name}` })),
];

/**
 * CloudTrail management events for statement 3 and 4 actions against the record tables, from any
 * principal. Each action names its table in one of these request parameters:
 * `tableName` (CreateBackup, UpdateTable, DeleteTable, UpdateTimeToLive, UpdateContinuousBackups,
 * and the Kinesis streaming-destination actions), `sourceTableName` or `sourceTableArn`
 * (RestoreTableToPointInTime), `tableArn` (ExportTableToPointInTime), and `resourceArn`
 * (PutResourcePolicy, DeleteResourcePolicy, for the table or its stream).
 */
export function controlPlaneDetail(
  tableNames: readonly string[],
  tableArnBase: string,
): Record<string, unknown> {
  const nameOrArn = tableReference(tableNames);
  const arn = tableNames.map((name) => ({ suffix: `:table/${name}` }));
  return {
    eventSource: ['dynamodb.amazonaws.com'],
    eventName: [...CONTROL_PLANE_EVENT_NAMES],
    $or: [
      { requestParameters: { tableName: nameOrArn } },
      { requestParameters: { tableArn: arn } },
      { requestParameters: { sourceTableName: nameOrArn } },
      { requestParameters: { sourceTableArn: arn } },
      {
        requestParameters: {
          resourceArn: [
            ...arn,
            ...tableNames.map((name) => ({ prefix: `${tableArnBase}${name}/stream/` })),
          ],
        },
      },
    ],
  };
}

/**
 * `arn:<partition>:dynamodb:<region>:<account>:table/` for literal account and Region values. A
 * stream ARN ends in a service-assigned label, so its policy events are matched by prefix: a
 * two-wildcard pattern per table was refused at deploy as "too complex" [verified 2026-09-27].
 */
export function tableArnBase(region: string, account: string): string {
  return `arn:${partitionOf(region)}:dynamodb:${region}:${account}:table/`;
}

/** The literal partition of a Region, for patterns that must carry no token. */
export function partitionOf(region: string): string {
  return region.startsWith('cn-') ? 'aws-cn' : region.startsWith('us-gov-') ? 'aws-us-gov' : 'aws';
}

/** `PutRule` refuses an event pattern longer than this many characters [verified 2026-09-27, a refused deploy]. */
export const EVENT_PATTERN_MAX_LENGTH = 2048;

/**
 * Refuse at synth a pattern `PutRule` would refuse at deploy. The patterns grow with the
 * environment name, which appears in every table and function name they list.
 */
export function assertEventPatternFits(ruleId: string, pattern: Record<string, unknown>): void {
  const length = JSON.stringify(pattern).length;
  if (length > EVENT_PATTERN_MAX_LENGTH) {
    throw new Error(
      `${ruleId} event pattern is ${length} characters; EventBridge accepts at most `
      + `${EVENT_PATTERN_MAX_LENGTH}. Shorten the environment name or split the rule.`,
    );
  }
}

/** AWS Backup's on-demand backup of a record table, which no table policy statement can deny. */
export function backupDetail(tableNames: readonly string[]): Record<string, unknown> {
  return {
    eventSource: ['backup.amazonaws.com'],
    eventName: ['StartBackupJob'],
    requestParameters: { resourceArn: tableNames.map((name) => ({ suffix: `:table/${name}` })) },
  };
}

/**
 * AWS Backup's own job events, which a backup plan's selection produces without any
 * `StartBackupJob` call [verified 2026-09-27, AWS Backup Developer Guide, "Monitoring AWS Backup
 * events using Amazon EventBridge"]. The `CREATED` event carries no `resourceArn`; every later state
 * of the same job does.
 */
export const BACKUP_JOB_DETAIL_TYPE = 'Backup Job State Change';

export function backupJobStateDetail(tableNames: readonly string[]): Record<string, unknown> {
  return { resourceArn: tableNames.map((name) => ({ suffix: `:table/${name}` })) };
}

/**
 * IAM changes that would let another principal satisfy a policy's `aws:PrincipalArn` condition:
 * recreating a fixed-name role, changing who may assume it, or changing what it may do.
 */
export const ROLE_CHANGE_EVENT_NAMES: readonly string[] = [
  'CreateRole',
  'DeleteRole',
  'UpdateAssumeRolePolicy',
  'AttachRolePolicy',
  'DetachRolePolicy',
  'PutRolePolicy',
  'DeleteRolePolicy',
];

/**
 * The roles the evaluation service assumes, one per family (ADR-079). Each is its owned result
 * group's only writer, so a changed trust or grant forges verdict content without touching a
 * function; they have no function, so they are watched here rather than in `watchedFunctions`.
 */
export function watchedEvaluationRoles(envName: string): string[] {
  return EVALUATION_FAMILIES.map((family) => evaluationExecutionRoleName(envName, family));
}

/**
 * Every fixed role a record-table policy names, the evaluation execution roles, and the
 * CloudFormation execution role statement 4 exempts.
 */
export function roleChangeDetail(
  functions: readonly WatchedFunction[],
  evaluationRoleNames: readonly string[],
  executionRoleName: string,
): Record<string, unknown> {
  return {
    eventSource: ['iam.amazonaws.com'],
    eventName: [...ROLE_CHANGE_EVENT_NAMES],
    requestParameters: {
      roleName: [...new Set([
        ...functions.map((fn) => fn.roleName),
        ...evaluationRoleNames,
        executionRoleName,
      ])],
    },
  };
}

/**
 * Every rule, pipe, and alarm the tamper stack names starts with this, so the self-watch patterns
 * match all of them, and any later one, by prefix.
 */
export function recordIntegrityNamePrefix(envName: string): string {
  return `adlytics-${envName}-record-`;
}

/** Every tamper log group's name starts with this. */
export function recordIntegrityLogGroupPrefix(envName: string): string {
  return `/adlytics/${envName}/record-integrity/`;
}

/** The account-level Logs resource policy every CloudTrail tamper rule delivers through. */
export function recordIntegrityRulesPolicyName(envName: string): string {
  return `${recordIntegrityPolicyPrefix(envName)}-rules`;
}

export type DetectionService = 'events' | 'pipes' | 'cloudwatch' | 'logs';

export interface DetectionChange {
  /**
   * The EventBridge `source`, where EventBridge's event reference documents it for CloudTrail
   * delivery. CloudWatch's page lists only its direct alarm events, so that rule matches on
   * `eventSource` alone rather than on an undocumented `source`.
   */
  readonly source: string | undefined;
  readonly detail: Record<string, unknown>;
}

/**
 * Changes to the detection itself: disabling, retargeting, or deleting a tamper rule; stopping,
 * changing, or deleting a pipe; silencing, deleting, or redefining an alarm; deleting or
 * overwriting a metric filter, deleting a tamper log group, or shortening its retention; and
 * deleting the rules' Logs resource policy. Request parameter names follow each API's CloudTrail
 * record: `name` (PutRule, DisableRule, DeleteRule, and the pipe calls), `rule` (PutTargets,
 * RemoveTargets), `alarmName` (PutMetricAlarm), `alarmNames` (DeleteAlarms, DisableAlarmActions),
 * `logGroupName` (the four group calls), and `policyName` (DeleteResourcePolicy; its CloudTrail
 * spelling is [unverified] until the control-plane alarm's live proof).
 */
export function detectionChangeDetail(envName: string, service: DetectionService): DetectionChange {
  const name = [{ prefix: recordIntegrityNamePrefix(envName) }];
  switch (service) {
    case 'events':
      return {
        source: 'aws.events',
        detail: {
          eventSource: ['events.amazonaws.com'],
          eventName: ['DisableRule', 'DeleteRule', 'RemoveTargets', 'PutTargets', 'PutRule'],
          $or: [{ requestParameters: { name } }, { requestParameters: { rule: name } }],
        },
      };
    case 'pipes':
      return {
        source: 'aws.pipes',
        detail: {
          eventSource: ['pipes.amazonaws.com'],
          eventName: ['StopPipe', 'UpdatePipe', 'DeletePipe'],
          requestParameters: { name },
        },
      };
    case 'cloudwatch':
      return {
        source: undefined,
        detail: {
          eventSource: ['monitoring.amazonaws.com'],
          eventName: ['DisableAlarmActions', 'DeleteAlarms', 'PutMetricAlarm'],
          $or: [{ requestParameters: { alarmName: name } }, { requestParameters: { alarmNames: name } }],
        },
      };
    case 'logs':
      return {
        source: 'aws.logs',
        detail: {
          eventSource: ['logs.amazonaws.com'],
          eventName: [...SELF_WATCH_GROUP_EVENT_NAMES, 'DeleteResourcePolicy'],
          $or: [
            {
              eventName: [...SELF_WATCH_GROUP_EVENT_NAMES],
              requestParameters: { logGroupName: [{ prefix: recordIntegrityLogGroupPrefix(envName) }] },
            },
            {
              eventName: ['DeleteResourcePolicy'],
              requestParameters: { policyName: [{ prefix: recordIntegrityPolicyPrefix(envName) }] },
            },
          ],
        },
      };
  }
}

/**
 * Calls on a tamper log group: deleting or overwriting its metric filter (a rewritten filter that
 * never matches silences the alarm as surely as a deleted one), deleting the group, or shortening
 * its retention. Each names the group in `logGroupName`.
 */
const SELF_WATCH_GROUP_EVENT_NAMES = [
  'DeleteMetricFilter',
  'PutMetricFilter',
  'DeleteLogGroup',
  'PutRetentionPolicy',
] as const;

/**
 * The Logs resource policy every CloudTrail rule delivers through starts with this. Its deletion
 * is named by `policyName`; an account-scoped overwrite is already matched unconditionally by the
 * result-group rule. A deleted policy also blocks delivery of the event of its own deletion, which
 * is why each rule's `FailedInvocations` alarm, not this pattern, is the check that holds.
 */
export function recordIntegrityPolicyPrefix(envName: string): string {
  return `adlytics-${envName}-record-integrity`;
}

/**
 * EventBridge's per-rule failed-delivery metric, alarmed per tamper rule so a target the rule can
 * no longer write (a deleted Logs resource policy, a deleted group) is itself an alarm. The metric,
 * namespace, and `RuleName` dimension are [verified 2026-09-27, EventBridge User Guide, "Monitoring
 * Amazon EventBridge"]; it counts permanent failures and is sent only when non-zero, so missing
 * data is not breaching. That a refused log-group delivery counts as one is [unverified] until a
 * live proof.
 */
export const RULE_FAILURE_METRIC = {
  namespace: 'AWS/Events',
  metricName: 'FailedInvocations',
  dimension: 'RuleName',
} as const;

export const DETECTION_SERVICES: readonly DetectionService[] = ['events', 'pipes', 'cloudwatch', 'logs'];

/**
 * EventBridge Pipes' per-pipe failure metric. The metric name and its `PipeName` dimension are
 * [verified 2026-09-27, EventBridge User Guide, "Logging and monitoring Amazon EventBridge Pipes"];
 * that page does not name the namespace, so `AWS/EventBridge/Pipes` is [unverified].
 */
export const PIPE_FAILURE_METRIC = {
  namespace: 'AWS/EventBridge/Pipes',
  metricName: 'ExecutionFailed',
  dimension: 'PipeName',
} as const;

/**
 * The versioned CloudTrail event names Lambda records for function creation, code changes, and
 * configuration changes (the last carries a role change). Each names its function in
 * `functionName`, and the create and configuration calls carry `role`.
 */
export const FUNCTION_CODE_EVENT_NAMES: readonly string[] = [
  'CreateFunction20150331',
  'UpdateFunctionCode20150331v2',
  'UpdateFunctionConfiguration20150331v2',
];

/**
 * The calls that change who may invoke a function: `AddPermission` and `RemovePermission`, which
 * name it in `functionName`, and `PutResourcePolicy` and `DeleteResourcePolicy` (the API behind
 * `AWS::Lambda::ResourcePolicy`), which name it in `resourceArn`. Removing the ingress Deny or
 * opening a gate provider is one of these.
 *
 * The CloudTrail spellings are [unverified]: Lambda versions most event names by API date, the
 * permission calls are recorded as `...20150331v2` elsewhere, and the resource-policy API's
 * request path carries the date `2026-07-09` [verified 2026-09-27, Lambda API reference,
 * PutResourcePolicy], so its event may carry a suffix too. The pattern therefore matches each verb
 * by prefix, which covers every name listed here and any versioned form.
 */
export const FUNCTION_POLICY_EVENT_NAMES: readonly string[] = [
  'AddPermission20150331v2',
  'RemovePermission20150331v2',
  'AddPermission',
  'RemovePermission',
  'PutResourcePolicy',
  'DeleteResourcePolicy',
];

/** The verbs the policy rule matches by prefix; every `FUNCTION_POLICY_EVENT_NAMES` entry starts with one. */
export const FUNCTION_POLICY_EVENT_VERBS: readonly string[] = [
  'AddPermission',
  'RemovePermission',
  'PutResourcePolicy',
  'DeleteResourcePolicy',
];

/**
 * The calls that attach an event source to a function or repoint one at it. Lambda runs a mapping's
 * invocations itself, so they are one path the gate provider's and the ingress's Lambda-principal
 * exemption could admit (ADR-079). Both name the function in `functionName`; an update that does
 * not change the function omits it and so cannot repoint a mapping. Matched by prefix because the
 * CloudTrail spelling carries an API date: CloudTrail records `CreateEventSourceMapping20150331`
 * and `UpdateEventSourceMapping20150331` [verified 2026-10-02, CloudTrail LookupEvents in dev].
 */
export const FUNCTION_MAPPING_EVENT_VERBS: readonly string[] = [
  'CreateEventSourceMapping',
  'UpdateEventSourceMapping',
];

/** Every Lambda event name the function rules watch. */
export const FUNCTION_CHANGE_EVENT_NAMES: readonly string[] = [
  ...FUNCTION_CODE_EVENT_NAMES,
  ...FUNCTION_MAPPING_EVENT_VERBS,
  ...FUNCTION_POLICY_EVENT_NAMES,
];

export type FunctionChangeRule = 'name' | 'role' | 'permission' | 'policy';

export const FUNCTION_CHANGE_RULES: readonly FunctionChangeRule[] = ['name', 'role', 'permission', 'policy'];

/**
 * Four rules, because a pattern over any two of them exceeds EventBridge's pattern limit (a
 * combined permission-and-policy pattern is already 2092 characters at `dev`):
 *
 * - `name`: a code or configuration change on a function that holds a named role, or an event
 *   source mapping created for or repointed at one;
 * - `role`: any function created or reconfigured with one of those roles;
 * - `permission`: `AddPermission` or `RemovePermission` on one of those functions, by
 *   `functionName`, which may be a name, a full ARN, or a partial ARN;
 * - `policy`: `PutResourcePolicy` or `DeleteResourcePolicy` on one of them, by `resourceArn`, a
 *   full ARN. A qualified ARN's policy governs only that qualifier, and these functions publish
 *   no version or alias, so the unqualified suffix is matched.
 */
export function functionChangeDetail(
  functions: readonly WatchedFunction[],
  by: FunctionChangeRule,
): Record<string, unknown> {
  const eventSource = ['lambda.amazonaws.com'];
  const byArn = functions.map((fn) => ({ suffix: `:function:${fn.functionName}` }));
  const functionName = [...functions.map((fn) => fn.functionName), ...byArn];
  const verbs = (names: readonly string[]): { prefix: string }[] =>
    FUNCTION_POLICY_EVENT_VERBS.filter((verb) => names.includes(verb)).map((verb) => ({ prefix: verb }));
  switch (by) {
    case 'name':
      return {
        eventSource,
        eventName: [
          ...FUNCTION_CODE_EVENT_NAMES,
          ...FUNCTION_MAPPING_EVENT_VERBS.map((verb) => ({ prefix: verb })),
        ],
        requestParameters: { functionName },
      };
    case 'role':
      return {
        eventSource,
        eventName: [...FUNCTION_CODE_EVENT_NAMES],
        requestParameters: { role: functions.map((fn) => ({ suffix: `:role/${fn.roleName}` })) },
      };
    case 'permission':
      return {
        eventSource,
        eventName: verbs(['AddPermission', 'RemovePermission']),
        requestParameters: { functionName },
      };
    case 'policy':
      return {
        eventSource,
        eventName: verbs(['PutResourcePolicy', 'DeleteResourcePolicy']),
        requestParameters: { resourceArn: byArn },
      };
  }
}

/* -------------------------------------------------------------------------
 * ADR-079: release records and result groups
 * ---------------------------------------------------------------------- */

/** Every Parameter Store call that writes, deletes, or relabels a parameter. */
export const RELEASE_RECORD_EVENT_NAMES: readonly string[] = [
  'PutParameter',
  'DeleteParameter',
  'DeleteParameters',
  'LabelParameterVersion',
  'UnlabelParameterVersion',
];

/** The prefix every release record's name starts with; the family follows it. */
export function releaseRecordPrefix(envName: string): string {
  const name = evaluationReleaseParameterName(envName, EVALUATION_FAMILIES[0]!);
  return name.slice(0, name.lastIndexOf('-') + 1);
}

/**
 * A write, delete, or relabel of a release record by any principal but a gate provider role.
 *
 * The parameter is named in `requestParameters.name` (PutParameter, DeleteParameter, and the two
 * label calls) or `requestParameters.names` (DeleteParameters), as the parameter path or its ARN.
 * Those names follow each API's request syntax; their CloudTrail spelling is [unverified] until
 * the control-plane alarm's live proof.
 *
 * "Not a gate provider" is two alternatives, because `anything-but` does not match an absent field
 * and `exists` works only on leaf nodes [verified 2026-09-27, EventBridge comparison operators]:
 * an assumed-role session whose issuer is another role, or a caller with no session issuer at all
 * (an IAM user, the root user, or a `GetSessionToken` session), spelled as the issuer ARN leaf
 * being absent rather than as `sessionContext` absent, which an intermediate node cannot express.
 */
export function releaseRecordDetail(
  envName: string,
  region: string,
  account: string,
): Record<string, unknown> {
  const prefix = releaseRecordPrefix(envName);
  const parameter = [
    { prefix },
    { prefix: `arn:${partitionOf(region)}:ssm:${region}:${account}:parameter${prefix}` },
  ];
  const gateRoles = EVALUATION_FAMILIES.map((family) =>
    fixedRoleArn(partitionOf(region), account, evaluationGateProviderRoleName(envName, family)));
  const callers = [
    { sessionContext: { sessionIssuer: { arn: [{ 'anything-but': gateRoles }] } } },
    { sessionContext: { sessionIssuer: { arn: [{ exists: false }] } } },
  ];
  return {
    eventSource: ['ssm.amazonaws.com'],
    eventName: [...RELEASE_RECORD_EVENT_NAMES],
    $or: (['name', 'names'] as const).flatMap((key) =>
      callers.map((userIdentity) => ({ requestParameters: { [key]: parameter }, userIdentity }))),
  };
}

/**
 * The result-group actions observability §3.7 lists, each named in its CloudTrail record by a
 * log group parameter. `PutLogGroupDeletionProtection` is included beside `DeleteLogGroup`
 * because disabling deletion protection is the step that makes a delete possible.
 */
export const RESULT_GROUP_EVENT_NAMES: readonly string[] = [
  'PutSubscriptionFilter',
  'DeleteSubscriptionFilter',
  'CreateExportTask',
  'StartLiveTail',
  'AssociateKmsKey',
  'DisassociateKmsKey',
  'PutRetentionPolicy',
  'DeleteRetentionPolicy',
  'DeleteLogGroup',
  'PutLogGroupDeletionProtection',
  'PutDataProtectionPolicy',
  'DeleteDataProtectionPolicy',
  'PutResourcePolicy',
  // A transformer rewrites events at ingestion, and subscription filters see the transformed
  // events [verified 2026-09-27, CloudWatch Logs API reference, PutTransformer], so one on a
  // result group could edit a label, score, or configuration ID without any PutLogEvents.
  'PutTransformer',
  'DeleteTransformer',
];

/**
 * Account-level calls that can reach a result group without naming it: an account subscription
 * or data-protection policy, and a delivery destination. Matched unconditionally.
 */
export const ACCOUNT_LOGS_EVENT_NAMES: readonly string[] = [
  'PutAccountPolicy',
  'DeleteAccountPolicy',
  'PutDeliveryDestination',
];

/** The prefix every result group's name starts with; the family follows it. */
export function resultGroupPrefix(envName: string): string {
  const name = evaluationResultLogGroupName(envName, EVALUATION_FAMILIES[0]!);
  return name.slice(0, name.lastIndexOf('-') + 1);
}

/**
 * A result-group action naming a result group, or an account-level Logs policy call.
 *
 * Parameter names follow each API's request syntax [verified 2026-09-27, CloudWatch Logs API
 * reference, for AssociateKmsKey, PutResourcePolicy, StartLiveTail, CreateExportTask, and
 * PutLogGroupDeletionProtection]: `logGroupName` (subscription filters, export tasks, KMS key
 * association, retention, DeleteLogGroup), `logGroupIdentifier` (data protection, deletion
 * protection, and PutTransformer and DeleteTransformer [verified 2026-09-27, CloudWatch Logs API
 * reference]; a name or an ARN), `logGroupIdentifiers` (StartLiveTail; ARNs), `resourceIdentifier`
 * (KMS key association by ARN), and `resourceArn` (a log-group-scoped PutResourcePolicy). That
 * CloudTrail records each under the same name, and records StartLiveTail at all, is [unverified]
 * until the control-plane alarm's live proof. A PutResourcePolicy without `resourceArn` is
 * account-scoped and matched unconditionally. An ARN is matched by prefix with the account and
 * Region literal, so a `:*` suffix or none both match.
 */
export function resultGroupDetail(
  envName: string,
  region: string,
  account: string,
): Record<string, unknown> {
  const namePrefix = resultGroupPrefix(envName);
  const arnPrefix = { prefix: `arn:${partitionOf(region)}:logs:${region}:${account}:log-group:${namePrefix}` };
  const nameOrArn = [{ prefix: namePrefix }, arnPrefix];
  return {
    eventSource: ['logs.amazonaws.com'],
    eventName: [...RESULT_GROUP_EVENT_NAMES, ...ACCOUNT_LOGS_EVENT_NAMES],
    $or: [
      { requestParameters: { logGroupName: [{ prefix: namePrefix }] } },
      { requestParameters: { logGroupIdentifier: nameOrArn } },
      { requestParameters: { logGroupIdentifiers: nameOrArn } },
      { requestParameters: { resourceIdentifier: [arnPrefix] } },
      { requestParameters: { resourceArn: [arnPrefix] } },
      { eventName: [...ACCOUNT_LOGS_EVENT_NAMES] },
      { eventName: ['PutResourcePolicy'], requestParameters: { resourceArn: [{ exists: false }] } },
    ],
  };
}
