// SPDX-License-Identifier: MIT-0
/** Permanent Semantic V2 ingestion wiring, least privilege, and shared operational resources. */
import { readFileSync } from 'node:fs';
import * as path from 'node:path';

import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';

import {
  paramPath,
  semanticV2AthenaWorkgroupName,
  semanticV2DatabaseName,
  semanticV2StateTableName,
  staffedAlarmDestinationConfig,
} from '../lib/config';
import { SELECTED_ARMS, STAFFING_POLICY } from '../lib/generated/alarm-destinations';
import {
  BAKE_TASKS,
  LEGACY_WRITERS,
  ROUTES,
  T_MAX_SECONDS,
} from '../lib/generated/execution-graph-protocol';
import {
  FINALIZER_BLOCKED_AFTER_SECONDS,
  GRAPH_ALARMS,
  POST_GRANT_INVARIANT_SIGNALS,
  PRECOMPUTE_BLOCKED_AFTER_SECONDS,
  PROMOTE_AGE_ALARM_SECONDS,
} from '../lib/stacks/execution-graph-alarms';
import type { AppConfig } from '../lib/config';
import { dataBucketName } from '../lib/mixins/storage';
import { IngestionStack } from '../lib/stacks/ingestion-stack';
import {
  CANONICAL_DATASET_KEYS,
  INGESTION_ERROR_OPERATIONS,
  stage1LimitsEnvironment,
} from '../lib/stacks/ingestion-stage1';
import {
  alarmingErrorsByOperationQuery,
  alarmingErrorsQuery,
} from '../lib/constructs/observability/error-alarms';
import { PAGE_OPERATIONS } from '../lib/constructs/observability/page-route-alarms';
import { METRIC_RECONCILER_OPERATION } from '../lib/stacks/operational-status-processor-stack';

/** One typed-error alarm per fixed ingestion operation (Metrics Insights has no IN or OR). */
const ERROR_ALARM_NAMES = INGESTION_ERROR_OPERATIONS.map(
  (operation) => `adlytics-dev-ingestion-alarming-errors-${operation.replace(/[._]/g, '-')}`,
);
import { TEST_CONFIG, configWith } from './support/config';
import { grantsOfRoleLogicalId, listBucketPrefixes, resultPrefixGrants } from './support/iam';
import { everyTemplate } from './support/templates';

const ASSET = __dirname;
const INGESTION_HANDLER = 'adapters.inbound.ingestion_handlers.load.handler';
type Handler = typeof INGESTION_HANDLER;
type Props = Record<string, any>;

const DISABLED_CONFIG: AppConfig = configWith({ landing: { enabled: false } });
/**
 * ADR-148: the 17 Presentation page operations, pinned independently of the implementation. Each
 * is held by its own page alarm, so the catch-all excludes it.
 */
const PAGE_OPERATIONS_PINNED = [
  'presentation.overview',
  'presentation.channel_performance',
  'presentation.campaigns',
  'presentation.campaign_detail',
  'presentation.campaign_metric_focus',
  'presentation.portfolio_metric_focus',
  'presentation.retail_metric_focus',
  'presentation.insight_deep_dive',
  'presentation.marketplace_comparison',
  'presentation.data_health',
  'presentation.retail_health',
  'presentation.retail_health_product',
  'presentation.scorecard',
  'presentation.new_customers',
  'presentation.reach',
  'presentation.retention',
  'presentation.audiences',
] as const;

function synth(config: AppConfig = DISABLED_CONFIG): Template {
  const app = new cdk.App();
  const stack = new IngestionStack(app, 'Adlytics-Ingestion-test', {
    config,
    env: { account: config.account, region: config.region },
    sourceAssetPath: ASSET,
  });
  return Template.fromStack(stack);
}

function resources(template: Template, type: string): Props[] {
  return Object.values(template.findResources(type)).map((resource) =>
    (resource as Props).Properties as Props,
  );
}

function only<T>(items: readonly T[], what: string): T {
  const [first] = items;
  if (items.length !== 1 || first === undefined) {
    throw new Error(`expected exactly one ${what}, found ${items.length}`);
  }
  return first;
}

function functionForHandler(template: Template, handler: Handler): Props {
  return only(
    resources(template, 'AWS::Lambda::Function').filter((fn) => fn.Handler === handler),
    `function for ${handler}`,
  );
}

function ingestionFunction(template: Template): Props {
  return functionForHandler(template, INGESTION_HANDLER);
}

function roleLogicalId(template: Template, handler: Handler): string {
  const getAtt = (functionForHandler(template, handler).Role as Props)['Fn::GetAtt'];
  if (
    !Array.isArray(getAtt) ||
    getAtt.length !== 2 ||
    typeof getAtt[0] !== 'string' ||
    getAtt[1] !== 'Arn'
  ) {
    throw new Error(`function for ${handler} role is not a role ARN reference`);
  }
  return getAtt[0];
}

function statements(
  template: Template,
  handler: Handler = INGESTION_HANDLER,
): Props[] {
  const functionRoleLogicalId = roleLogicalId(template, handler);
  return [
    ...resources(template, 'AWS::IAM::Policy'),
    ...resources(template, 'AWS::IAM::ManagedPolicy'),
  ]
    .filter((policy) =>
      ((policy.Roles ?? []) as Props[]).some(
        (role) => role.Ref === functionRoleLogicalId,
      ),
    )
    .flatMap(
      (policy) => ((policy.PolicyDocument as Props).Statement ?? []) as Props[],
    );
}

function actionsOf(statement: Props): string[] {
  const action = statement.Action;
  return (Array.isArray(action) ? action : [action]).filter(
    (value): value is string => typeof value === 'string',
  );
}

function hasExactActions(statement: Props, actions: readonly string[]): boolean {
  return (
    JSON.stringify(actionsOf(statement).toSorted()) ===
    JSON.stringify(actions.toSorted())
  );
}

function statementResourceTexts(statement: Props): string[] {
  const resource = statement.Resource;
  return (Array.isArray(resource) ? resource : [resource]).map(
    (value) => JSON.stringify(value) ?? '',
  );
}

function grantedActions(
  template: Template,
  handler: Handler = INGESTION_HANDLER,
): string[] {
  return statements(template, handler).flatMap(actionsOf);
}

function grantedResourceText(
  template: Template,
  handler: Handler = INGESTION_HANDLER,
): string {
  return JSON.stringify(
    statements(template, handler).map((statement) => statement.Resource),
  );
}

function actionsOnResource(
  template: Template,
  fragment: string,
  handler: Handler = INGESTION_HANDLER,
): string[] {
  return [
    ...new Set(
      statements(template, handler)
        .filter((statement) => JSON.stringify(statement.Resource).includes(fragment))
        .flatMap(actionsOf),
    ),
  ].sort();
}

function functionEnvironment(template: Template): Props {
  return (ingestionFunction(template).Environment as Props).Variables as Props;
}

describe('general Athena query-batch admission (D7)', () => {
  it('propagates the configured value to the ingestion and stage-1 functions', () => {
    const template = synth(configWith({ landing: { enabled: false }, athenaQueryBatchMaxConcurrency: 3 }));
    const carriers = resources(template, 'AWS::Lambda::Function').filter(
      (fn) =>
        ((fn.Environment as Props | undefined)?.Variables as Props | undefined)
          ?.ADL_ATHENA_QUERY_BATCH_MAX_CONCURRENCY !== undefined,
    );
    expect(carriers.length).toBeGreaterThanOrEqual(2);
    for (const fn of carriers) {
      expect(((fn.Environment as Props).Variables as Props).ADL_ATHENA_QUERY_BATCH_MAX_CONCURRENCY).toBe('3');
    }
    expect(functionEnvironment(template).ADL_ATHENA_QUERY_BATCH_MAX_CONCURRENCY).toBe('3');
  });
});

describe('shared ingestion delivery controls', () => {
  it('matches queue concurrency to execution slots and preserves retry visibility', () => {
    const template = synth();
    const fn = ingestionFunction(template);
    expect(fn.ReservedConcurrentExecutions).toBe(2);
    const mappings = resources(template, 'AWS::Lambda::EventSourceMapping');
    expect(mappings).toHaveLength(2);
    const deliveryMapping = only(
      mappings.filter((candidate) =>
        JSON.stringify(candidate.EventSourceArn).includes('DeliveryQueue'),
      ),
      'delivery event source mapping',
    );
    expect(deliveryMapping.Enabled).toBe(true);
    expect(deliveryMapping.BatchSize).toBe(1);
    expect(deliveryMapping.ScalingConfig).toEqual({ MaximumConcurrency: 2 });
    const semanticMapping = only(
      mappings.filter((candidate) =>
        JSON.stringify(candidate.EventSourceArn).includes('SemanticWriteQueue'),
      ),
      'semantic-writer event source mapping',
    );
    expect(semanticMapping.Enabled).toBe(false);
    expect(semanticMapping.BatchSize).toBe(1);
    expect(semanticMapping.ScalingConfig).toEqual({ MaximumConcurrency: 8 });

    const work = only(
      resources(template, 'AWS::SQS::Queue').filter(
        (queue) => queue.QueueName === `adlytics-${TEST_CONFIG.envName}-ingestion`,
      ),
      'ingestion queue',
    );
    expect(work.VisibilityTimeout).toBeGreaterThan(fn.Timeout as number);
    expect((work.RedrivePolicy as Props).maxReceiveCount).toBe(ROUTES.delivery.max_receive_count);
  });
  it('reads the delivery and semantic-write maxReceiveCount from the generated routes', () => {
    const template = synth();
    // Revision 2 moved the values into `routes` without changing them (design §2.1).
    expect([ROUTES.delivery.max_receive_count, ROUTES.semantic_write.max_receive_count]).toEqual([3, 3]);
    const queue = (name: string): Props =>
      only(
        resources(template, 'AWS::SQS::Queue').filter((item) => item.QueueName === name),
        name,
      );
    const env = TEST_CONFIG.envName;
    expect((queue(`adlytics-${env}-ingestion`).RedrivePolicy as Props).maxReceiveCount).toBe(
      ROUTES.delivery.max_receive_count,
    );
    expect(
      (queue(`adlytics-${env}-semantic-write.fifo`).RedrivePolicy as Props).maxReceiveCount,
    ).toBe(ROUTES.semantic_write.max_receive_count);
    const writer = soleFunction(template, 'adapters.inbound.ingestion_handlers.table_writer.handler');
    expect((writer.Environment as Props).Variables.ADL_TABLE_WRITER_MAX_RECEIVE_COUNT).toBe(
      String(ROUTES.semantic_write.max_receive_count),
    );
  });

  it('retains dead letters for fourteen days', () => {
    const dead = only(
      resources(synth(), 'AWS::SQS::Queue').filter(
        (queue) => queue.QueueName === `adlytics-${TEST_CONFIG.envName}-ingestion-dlq`,
      ),
      'ingestion dead-letter queue',
    );
    expect(dead.MessageRetentionPeriod).toBe(1209600);
  });

  it('delivers manifest events through the queue', () => {
    const rule = only(
      resources(synth(), 'AWS::Events::Rule').filter(
        (candidate) =>
          candidate.Name === `adlytics-${TEST_CONFIG.envName}-manifest-landed`,
      ),
      'manifest rule',
    );
    const pattern = rule.EventPattern as Props;
    expect(pattern.source).toEqual(['aws.s3']);
    expect(pattern['detail-type']).toEqual(['Object Created']);
    expect((pattern.detail as Props).object.key).toEqual([{ suffix: 'manifest.json' }]);
    expect((pattern.detail as Props).bucket.name).toEqual([
      dataBucketName(TEST_CONFIG, 'semantic-v2-raw'),
    ]);

    const target = only(rule.Targets as Props[], 'rule target');
    expect(JSON.stringify(target.Arn)).toContain('DeliveryQueue');
  });
});

describe('permanent Semantic V2 load authority', () => {
  it('uses only scoped inline logging permissions on its explicit role', () => {
    const template = synth();
    const role = (
      template.findResources('AWS::IAM::Role')[roleLogicalId(template, INGESTION_HANDLER)] as Props
    ).Properties as Props;
    expect(role.ManagedPolicyArns).toBeUndefined();

    const logging = statements(template).filter((statement) =>
      hasExactActions(statement, ['logs:CreateLogStream', 'logs:PutLogEvents']),
    );
    expect(logging).toHaveLength(1);
    expect(statementResourceTexts(logging[0] ?? {})).toEqual([
      expect.stringContaining('IngestionFunctionLogs'),
    ]);
    expect(statementResourceTexts(logging[0] ?? {})).not.toContain('"*"');
  });

  it('reads only governed source-system object versions from the V2 landing bucket', () => {
    const template = synth();
    const raw = dataBucketName(TEST_CONFIG, 'semantic-v2-raw');
    const rawStatements = statements(template).filter((statement) =>
      JSON.stringify(statement.Resource).includes(raw),
    );
    expect(rawStatements).toHaveLength(1);
    expect(actionsOf(rawStatements[0] ?? {})).toEqual(['s3:GetObjectVersion']);
    expect(statementResourceTexts(rawStatements[0] ?? {})).toEqual([
      expect.stringContaining(`${raw}/amc/*`),
      expect.stringContaining(`${raw}/vendor_central/*`),
      expect.stringContaining(`${raw}/vendor_data_kiosk/*`),
    ]);
    expect(rawStatements[0]?.Condition).toBeUndefined();
    expect(JSON.stringify(rawStatements)).not.toContain(`${raw}/incoming/*`);
    expect(JSON.stringify(rawStatements)).not.toContain(`${raw}/*`);
  });

  it('grants exact V2 storage, query, catalog, and state authority', () => {
    const template = synth();
    const load = statements(template);
    const semantic = dataBucketName(TEST_CONFIG, 'semantic-v2');
    const results = dataBucketName(TEST_CONFIG, 'v2-results');
    const batch = semanticV2AthenaWorkgroupName(TEST_CONFIG.envName, 'batch');
    const bake = semanticV2AthenaWorkgroupName(TEST_CONFIG.envName, 'bake');
    const attempts = semanticV2StateTableName(TEST_CONFIG.envName, 'attempts');
    const leases = semanticV2StateTableName(TEST_CONFIG.envName, 'leases');
    const publications = semanticV2StateTableName(TEST_CONFIG.envName, 'publications');
    const glueActions = [
      'glue:GetDatabase',
      'glue:GetTable',
      'glue:GetTables',
      'glue:GetPartitions',
      'glue:UpdateTable',
      'glue:BatchCreatePartition',
      'glue:CreatePartition',
      'glue:UpdatePartition',
      'glue:DeletePartition',
      'glue:BatchDeletePartition',
    ].sort();

    const immutable = load.filter(
      (statement) =>
        hasExactActions(statement, ['s3:GetObject', 's3:PutObject']) &&
        JSON.stringify(statement.Resource).includes(`${semantic}/immutable/source-aligned/*`),
    );
    expect(immutable).toHaveLength(1);
    expect(statementResourceTexts(immutable[0] ?? {})).toEqual([
      expect.stringContaining(`${semantic}/immutable/source-aligned/*`),
      expect.stringContaining(`${semantic}/immutable/canonical/candidate/*`),
      expect.stringContaining(`${semantic}/immutable/canonical/successful/*`),
    ]);
    expect(immutable[0]?.Condition).toBeUndefined();
    // The claim-check grant is its own customer-managed policy, and the path's S3, Athena and Glue
    // grants are another (the inline role-policy aggregate reached IAM's 10,240-character limit;
    // iam-policy-size.test.ts holds the headroom). Both attach to the ingestion role and nothing
    // else, and the function waits for both (R041-1).
    // TableWriter's own storage policy (Stage1TableWriterStoragePolicy) attaches to its role only.
    const managedById = template.findResources('AWS::IAM::ManagedPolicy');
    const ingestionRole = { Ref: roleLogicalId(template, INGESTION_HANDLER) };
    const managedIds = Object.keys(managedById)
      .filter((id) => !id.startsWith('Stage1TableWriterStoragePolicy'))
      .sort();
    expect(managedIds.map((id) => id.replace(/[0-9A-F]{8}$/, ''))).toEqual([
      'IdentityEvidencePolicy',
      'SemanticPublicationKeysPolicy',
      'SemanticV2StoragePolicy',
    ]);
    for (const id of managedIds) {
      const properties = (managedById[id] as Props).Properties as Props;
      expect(properties.Roles).toEqual([ingestionRole]);
      expect(properties.Groups).toBeUndefined();
      expect(properties.Users).toBeUndefined();
    }
    const functionDependsOn = (only(
      Object.values(template.findResources('AWS::Lambda::Function', {
        Properties: { Handler: INGESTION_HANDLER },
      })),
      'ingestion function',
    ) as Props).DependsOn as string[];
    expect(managedIds.filter((id) => !functionDependsOn.includes(id))).toEqual([]);
    // Moved statements exist in their destination policies before the default policy drops them.
    const ingestionPolicies = template.findResources('AWS::IAM::Policy');
    const policyId = (prefix: string): string =>
      only(
        Object.keys(ingestionPolicies).filter(
          (id) =>
            id.startsWith(prefix) &&
            ((ingestionPolicies[id] as Props).Properties.Roles as Props[]).some(
              (role) => role.Ref === ingestionRole.Ref,
            ),
        ),
        prefix,
      ) as string;
    const defaultDependsOn = (ingestionPolicies[policyId('IngestionExecutionRoleDefaultPolicy')] as Props)
      .DependsOn as string[];
    expect(defaultDependsOn).toEqual(
      expect.arrayContaining([
        policyId('RetainedPublicationPathPolicy'),
        managedIds.find((id) => id.startsWith('SemanticPublicationKeysPolicy')),
      ]),
    );
    const managed = managedIds
      .filter((id) => id.startsWith('IdentityEvidencePolicy'))
      .map((id) => (managedById[id] as Props).Properties as Props);
    expect(managed).toHaveLength(1);
    expect(managed[0]?.PolicyDocument).toEqual({
      Statement: [
        {
          Action: ['s3:GetObject', 's3:PutObject'],
          Effect: 'Allow',
          Resource: {
            'Fn::Join': [
              '',
              ['arn:', { Ref: 'AWS::Partition' }, `:s3:::${semantic}/immutable/identity-evidence/*`],
            ],
          },
        },
      ],
      Version: '2012-10-17',
    });
    // Write-discipline §2.6: the claim-check prefix carries exactly GetObject and PutObject,
    // only on the ingestion role; no other role this stack defines may touch it.
    expect(actionsOnResource(template, `${semantic}/immutable/identity-evidence/*`)).toEqual(
      ['s3:GetObject', 's3:PutObject'].sort(),
    );
    const otherPolicies = resources(template, 'AWS::IAM::Policy').filter(
      (policy) =>
        !((policy.Roles ?? []) as Props[]).some(
          (role) => role.Ref === roleLogicalId(template, INGESTION_HANDLER),
        ),
    );
    expect(JSON.stringify(otherPolicies)).not.toContain('immutable/identity-evidence');

    const semanticObjects = load.filter(
      (statement) =>
        hasExactActions(statement, [
          's3:GetObject',
          's3:PutObject',
          's3:DeleteObject',
          's3:AbortMultipartUpload',
          's3:ListMultipartUploadParts',
        ]) && JSON.stringify(statement.Resource).includes(`${semantic}/source-aligned/*`),
    );
    expect(semanticObjects).toHaveLength(1);
    expect(statementResourceTexts(semanticObjects[0] ?? {})).toEqual([
      expect.stringContaining(`${semantic}/source-aligned/*`),
      expect.stringContaining(`${semantic}/canonical/candidate/*`),
      expect.stringContaining(`${semantic}/canonical/current/*`),
    ]);
    expect(semanticObjects[0]?.Condition).toBeUndefined();

    const semanticList = load.filter(
      (statement) =>
        hasExactActions(statement, ['s3:ListBucket']) &&
        JSON.stringify(statement.Condition).includes('source-aligned/*'),
    );
    expect(semanticList).toHaveLength(1);
    expect(statementResourceTexts(semanticList[0] ?? {})).toEqual([
      expect.stringContaining(semantic),
    ]);
    expect(statementResourceTexts(semanticList[0] ?? {})[0]).not.toContain(`${semantic}/`);
    expect(semanticList[0]?.Condition).toEqual({
      StringLike: {
        's3:prefix': [
          'source-aligned/*',
          'canonical/candidate/*',
          'canonical/current/*',
        ],
      },
    });

    expect(load.flatMap(actionsOf)).not.toContain('s3:ListBucketMultipartUploads');

    const semanticLocations = load.filter(
      (statement) =>
        hasExactActions(statement, ['s3:GetBucketLocation']) &&
        JSON.stringify(statement.Resource).includes(semantic),
    );
    expect(semanticLocations).toHaveLength(1);
    expect(statementResourceTexts(semanticLocations[0] ?? {})).toEqual([
      expect.stringContaining(semantic),
    ]);
    expect(statementResourceTexts(semanticLocations[0] ?? {})[0]).not.toContain(`${semantic}/`);
    expect(semanticLocations[0]?.Condition).toBeUndefined();

    const resultObjects = load.filter(
      (statement) =>
        hasExactActions(statement, [
          's3:GetObject',
          's3:PutObject',
          's3:AbortMultipartUpload',
        ]) && JSON.stringify(statement.Resource).includes(`${results}/bake/*`),
    );
    expect(resultObjects).toHaveLength(1);
    expect(statementResourceTexts(resultObjects[0] ?? {})).toEqual([
      expect.stringContaining(`${results}/bake/*`),
    ]);
    expect(resultObjects[0]?.Condition).toBeUndefined();
    // Pre-cutover `batch` results stay readable; nothing else is granted on that prefix.
    const batchResults = load.filter((statement) =>
      JSON.stringify(statement.Resource).includes(`${results}/batch/*`),
    );
    expect(batchResults.flatMap(actionsOf)).toEqual(['s3:GetObject']);

    const resultList = load.filter(
      (statement) =>
        hasExactActions(statement, ['s3:ListBucket']) &&
        JSON.stringify(statement.Condition).includes('bake/*'),
    );
    expect(resultList).toHaveLength(1);
    expect(statementResourceTexts(resultList[0] ?? {})).toEqual([
      expect.stringContaining(results),
    ]);
    expect(statementResourceTexts(resultList[0] ?? {})[0]).not.toContain(`${results}/`);
    expect(resultList[0]?.Condition).toEqual({
      StringLike: { 's3:prefix': ['bake/*', 'batch/*'] },
    });

    const resultLocations = load.filter(
      (statement) =>
        hasExactActions(statement, ['s3:GetBucketLocation']) &&
        JSON.stringify(statement.Resource).includes(results),
    );
    expect(resultLocations).toHaveLength(1);
    expect(statementResourceTexts(resultLocations[0] ?? {})).toEqual([
      expect.stringContaining(results),
    ]);
    expect(statementResourceTexts(resultLocations[0] ?? {})[0]).not.toContain(`${results}/`);
    expect(resultLocations[0]?.Condition).toBeUndefined();

    expect(actionsOnResource(template, `workgroup/${bake}`)).toEqual(
      [
        'athena:StartQueryExecution',
        'athena:GetQueryExecution',
        'athena:GetQueryResults',
        'athena:StopQueryExecution',
      ].sort(),
    );
    // Terminal reads only on the retained `batch` workgroup: load never starts there.
    expect(actionsOnResource(template, `workgroup/${batch}`)).toEqual(
      ['athena:GetQueryExecution', 'athena:GetQueryResults', 'athena:StopQueryExecution'].sort(),
    );

    for (const layer of ['source-aligned', 'candidate', 'current'] as const) {
      expect(
        actionsOnResource(
          template,
          `database/${semanticV2DatabaseName(TEST_CONFIG.envName, layer)}`,
        ),
      ).toEqual(glueActions);
    }
    expect(actionsOnResource(template, attempts)).toEqual(
      ['dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:Query'].sort(),
    );
    expect(actionsOnResource(template, leases)).toEqual(
      ['dynamodb:GetItem', 'dynamodb:UpdateItem', 'dynamodb:ConditionCheckItem'].sort(),
    );
    expect(actionsOnResource(template, publications)).toEqual(
      [
        'dynamodb:GetItem',
        'dynamodb:PutItem',
        'dynamodb:UpdateItem',
        'dynamodb:ConditionCheckItem',
        'dynamodb:Query',
      ].sort(),
    );
    expect(grantedActions(template)).not.toEqual(
      expect.arrayContaining(['dynamodb:TransactGetItems', 'dynamodb:TransactWriteItems']),
    );

    const granted = grantedResourceText(template);
    for (const suffix of [
      '/immutable/source-aligned/*',
      '/immutable/canonical/candidate/*',
      '/immutable/canonical/successful/*',
      '/immutable/identity-evidence/*',
      '/source-aligned/*',
      '/canonical/candidate/*',
      '/canonical/current/*',
    ]) {
      expect(granted).toContain(`${semantic}${suffix}`);
    }
    for (const forbidden of [
      `${semantic}/immutable/*`,
      `${results}/interactive/*`,
      `${results}/*`,
    ]) {
      expect(granted).not.toContain(forbidden);
    }
    expect(granted).toContain(`${publications}/index/scope-current`);
    expect(granted).not.toContain(
      semanticV2AthenaWorkgroupName(TEST_CONFIG.envName, 'interactive'),
    );
  });

  it('write-discipline §2.6: across every stack, only the ingestion role reaches the claim-check prefix', () => {
    const semantic = dataBucketName(TEST_CONFIG, 'semantic-v2');
    // A resource covers the prefix when it names it or any wildcard above it in the bucket.
    const covering = [
      `${semantic}/immutable/identity-evidence/`,
      `${semantic}/immutable/*`,
      `${semantic}/*`,
      'arn:aws:s3:::*',
    ];
    const objectAction = (action: string): boolean =>
      action === '*' || action === 's3:*' || /^s3:(Get|Put|Delete)Object/.test(action);
    const reaching: string[] = [];
    for (const [stack, template] of Object.entries(everyTemplate())) {
      const raw = (template as { Resources: Record<string, { Type: string; Properties: Props }> })
        .Resources;
      for (const [id, resource] of Object.entries(raw)) {
        const documents: Props[] =
          resource.Type === 'AWS::IAM::Policy' || resource.Type === 'AWS::IAM::ManagedPolicy'
            ? [resource.Properties.PolicyDocument as Props]
            : resource.Type === 'AWS::IAM::Role'
              ? ((resource.Properties.Policies ?? []) as Props[]).map(
                  (policy) => policy.PolicyDocument as Props,
                )
              : [];
        for (const document of documents) {
          for (const statement of (document.Statement ?? []) as Props[]) {
            if (statement.Effect !== 'Allow' || !actionsOf(statement).some(objectAction)) continue;
            const texts = statementResourceTexts(statement);
            const covers = texts.some(
              (text) => text === '"*"' || covering.some((fragment) => text.includes(fragment)),
            );
            if (covers) reaching.push(`${stack}/${id}`);
          }
        }
      }
    }
    expect(reaching).toEqual([
      expect.stringMatching(/^IngestionStack\/IdentityEvidencePolicy[0-9A-F]{8}$/),
    ]);
  });

  it('keeps the retained publication path authorized beside the Stage-1 grants', () => {
    // Write discipline §2.4: phase 1 still publishes authority exactly as before. The sidecar
    // acquires and releases partition leases with direct conditional updates keyed by the
    // canonical partition identity, replays attempt history by attempt id, and the transitional
    // zero-row servable advance updates a CURRENT item directly. Narrowing any of these to the
    // Stage-1 prefixes alone would fail every live delivery with AccessDenied.
    const template = synth();
    const load = statements(template);
    const leases = semanticV2StateTableName(TEST_CONFIG.envName, 'leases');
    const publications = semanticV2StateTableName(TEST_CONFIG.envName, 'publications');
    const canonical = 'fct_retail_purchase_amc|*';
    const leadingKeys = (statement: Props): string[] =>
      statement.Condition?.['ForAllValues:StringLike']?.['dynamodb:LeadingKeys'] ?? [];
    const enclosing = (statement: Props): string | undefined =>
      statement.Condition?.StringEquals?.['dynamodb:EnclosingOperation'];
    const on = (table: string) =>
      load.filter((statement) => JSON.stringify(statement.Resource).includes(`table/${table}`));

    const directLeaseUpdate = on(leases).filter(
      (statement) =>
        hasExactActions(statement, ['dynamodb:UpdateItem']) && enclosing(statement) === undefined,
    );
    expect(directLeaseUpdate).toHaveLength(1);
    expect(leadingKeys(directLeaseUpdate[0] ?? {})).toContain(canonical);
    expect(leadingKeys(directLeaseUpdate[0] ?? {})).not.toContain('TABLE#*');

    const transactionalLease = on(leases).filter(
      (statement) => enclosing(statement) === 'TransactWriteItems',
    );
    expect(transactionalLease).toHaveLength(1);
    expect(leadingKeys(transactionalLease[0] ?? {})).toEqual(
      expect.arrayContaining(['TABLE#*', canonical]),
    );

    const directPublicationUpdate = on(publications).filter(
      (statement) =>
        hasExactActions(statement, ['dynamodb:UpdateItem']) && enclosing(statement) === undefined,
    );
    expect(directPublicationUpdate).toHaveLength(1);
    expect(leadingKeys(directPublicationUpdate[0] ?? {})).toEqual(
      expect.arrayContaining(['TABLE_ATTEMPT#*', canonical]),
    );
    expect(leadingKeys(directPublicationUpdate[0] ?? {})).not.toContain('WRITE_PLAN#*');
    // D2 live acceptance (2026-10-16): every canonical dataset is keyed on each dataset-scoped
    // lease and publication statement, so a D2 delivery no longer fails its lease with
    // AccessDenied (the refused keys were `dim_audience_segment|` and `fct_exposure_group_outcome|`).
    const datasetScoped = [...on(leases), ...on(publications)].filter((statement) =>
      leadingKeys(statement).some((key) => key.endsWith('|*')),
    );
    expect(datasetScoped.length).toBeGreaterThanOrEqual(6);
    for (const statement of datasetScoped) {
      expect(leadingKeys(statement)).toEqual(expect.arrayContaining([...CANONICAL_DATASET_KEYS]));
    }
  });

  it('cannot see parameter store or wildcard resources outside tracing', () => {
    const template = synth();
    expect(grantedActions(template)).not.toContain('ssm:GetParameter');

    const offenders = statements(template).filter((statement) => {
      if (statement.Effect !== 'Allow') return false;
      if (!JSON.stringify(statement.Resource ?? '').includes('"*"')) return false;
      return !actionsOf(statement).every((action) => action.startsWith('xray:'));
    });
    expect(offenders).toEqual([]);
  });
});

describe('permanent V2 runtime configuration', () => {
  it('emits only current V2 data settings', () => {
    const env = functionEnvironment(synth());
    expect(env.ADL_RAW_BUCKET).toBe(dataBucketName(TEST_CONFIG, 'semantic-v2-raw'));
    expect(env.ADL_ATHENA_WORKGROUP).toBe(
      semanticV2AthenaWorkgroupName(TEST_CONFIG.envName, 'interactive'),
    );
    expect(env.ADL_ATHENA_BATCH_WORKGROUP).toBeUndefined();
    expect(env.ADL_GLUE_DATABASE).toBeUndefined();
    expect(env.ADL_FRESHNESS_TABLE).toBeUndefined();
    expect(env.ADL_RUN_LEDGER_TABLE).toBeUndefined();
    expect(env.ADL_SEMANTIC_ATTEMPTS_TABLE).toBe(
      semanticV2StateTableName(TEST_CONFIG.envName, 'attempts'),
    );
    expect(env.ADL_SEMANTIC_LEASES_TABLE).toBe(
      semanticV2StateTableName(TEST_CONFIG.envName, 'leases'),
    );
    expect(env.ADL_SEMANTIC_PUBLICATIONS_TABLE).toBe(
      semanticV2StateTableName(TEST_CONFIG.envName, 'publications'),
    );
    expect(env.ADL_SEMANTIC_V2_ENABLED).toBeUndefined();
    expect(env.ADL_SEMANTIC_V2_BUCKET).toBe(dataBucketName(TEST_CONFIG, 'semantic-v2'));
    expect(env.ADL_SEMANTIC_V2_ATHENA_BAKE_WORKGROUP).toBe(
      semanticV2AthenaWorkgroupName(TEST_CONFIG.envName, 'bake'),
    );
    expect(env.ADL_SEMANTIC_V2_ATHENA_BATCH_WORKGROUP).toBeUndefined();
    expect(env.ADL_SEMANTIC_V2_SOURCE_DATABASE).toBe(
      semanticV2DatabaseName(TEST_CONFIG.envName, 'source-aligned'),
    );
    expect(env.ADL_SEMANTIC_V2_CANDIDATE_DATABASE).toBe(
      semanticV2DatabaseName(TEST_CONFIG.envName, 'candidate'),
    );
    expect(env.ADL_SEMANTIC_V2_CURRENT_DATABASE).toBe(
      semanticV2DatabaseName(TEST_CONFIG.envName, 'current'),
    );
    expect(env.ADL_ATHENA_RESULT_REUSE_MAX_AGE_MINUTES).toBe('0');
  });

  it('renders the whole §11 ingestion object and no semantic-write queue for delivery', () => {
    // The delivery accessor is strict over the full object; a partial environment fails every
    // container at import. The Lambda never sends a queue message, so no queue URL is named.
    const env = functionEnvironment(synth());
    expect(env).toEqual(expect.objectContaining(stage1LimitsEnvironment(TEST_CONFIG)));
    expect(Object.keys(stage1LimitsEnvironment(TEST_CONFIG))).toHaveLength(25);
    expect(env.ADL_PROJECTION_HANDOFF_STATE).toBe(TEST_CONFIG.ingestion.projectionHandoffState);
    expect(env.ADL_PROJECTION_COMPOSITION).toBeUndefined();
    expect(env.ADL_SEMANTIC_WRITE_QUEUE_URL).toBeUndefined();
  });

  it('never names a Cognito client secret', () => {
    const env = functionEnvironment(synth());
    expect(env.ADL_COGNITO_CLIENT_SECRET_ID).toBeUndefined();
    expect(env.ADL_COGNITO_CLIENT_SECRET).toBeUndefined();
  });
});

describe('shared observability and lifecycle', () => {
  it('uses the configured arm64 runtime and a declared log group', () => {
    const template = synth();
    const fn = ingestionFunction(template);
    expect(fn.Architectures).toEqual(['arm64']);
    expect(fn.Runtime).toBe(TEST_CONFIG.pythonRuntime);
    const functions = resources(template, 'AWS::Lambda::Function');
    expect(functions.map((candidate) => candidate.Handler).sort()).toEqual(
      [
        'adapters.inbound.ingestion_handlers.load.handler',
        'adapters.inbound.ingestion_handlers.table_writer.handler',
        'adapters.jobs.plan_intent_reconciler.handler',
        'adapters.jobs.projection_handoff.handler',
        'adapters.jobs.provider_attempt_reconciler.handler',
        'adapters.jobs.provider_fence_recovery.handler',
      ].sort(),
    );
    expect(resources(template, 'AWS::Logs::LogGroup')).toHaveLength(6);
    for (const candidate of functions) {
      expect(candidate.Architectures).toEqual(['arm64']);
      expect(candidate.Runtime).toBe(TEST_CONFIG.pythonRuntime);
    }
  });

  it('retains nothing and publishes the alarm topic parameter', () => {
    const template = synth();
    const rendered = template.toJSON() as Props;
    const retained = Object.entries(rendered.Resources as Props).filter(
      ([, resource]) => (resource as Props).DeletionPolicy === 'Retain',
    );
    expect(retained).toEqual([]);

    const params = resources(template, 'AWS::SSM::Parameter');
    expect(params.map((param) => param.Name).sort()).toEqual(
      [
        paramPath(TEST_CONFIG.envName, 'ingestionAlarmTopicArn'),
        paramPath(TEST_CONFIG.envName, 'providerFenceRecoveryOperatorRoleArn'),
        paramPath(TEST_CONFIG.envName, 'providerFenceRecoveryFunctionArn'),
        paramPath(TEST_CONFIG.envName, 'semanticProjectionOperatorRoleArn'),
        paramPath(TEST_CONFIG.envName, 'stage1PreswitchVerificationRoleArn'),
        paramPath(TEST_CONFIG.envName, 'semanticWriteQueueArn'),
        paramPath(TEST_CONFIG.envName, 'semanticWriteDlqArn'),
        paramPath(TEST_CONFIG.envName, 'deliveryFunctionArn'),
      ].sort(),
    );
  });

  it('alarms on dead letters, stuck messages, alarming errors, and blocked deliveries', () => {
    const alarms = resources(synth(), 'AWS::CloudWatch::Alarm');
    expect(alarms.map((alarm) => alarm.AlarmName).sort()).toEqual(
      [
        ...ERROR_ALARM_NAMES,
        'adlytics-dev-alarming-errors-by-operation',
        'adlytics-dev-ingestion-blocked',
        'adlytics-dev-ingestion-dead-lettered',
        'adlytics-dev-ingestion-oldest',
        'adlytics-dev-semantic-write-dead-lettered',
        'adlytics-dev-semantic-write-oldest',
        // ADR-133 clause 4: one per function, the graph workers absent while the plane is not.
        ...[
          'IngestionFunction',
          'PlanIntentReconciler',
          'ProjectionHandoffProvider',
          'ProviderAttemptReconciler',
          'ProviderFenceRecoveryFunction',
          'TableWriter',
        ].map((name) => `adlytics-dev-${name}-function-errors`),
      ].sort(),
    );
    for (const alarm of alarms) {
      expect((alarm.AlarmActions as unknown[]) ?? []).toHaveLength(1);
      expect(alarm.TreatMissingData).toBe('notBreaching');
    }
    const refusal = alarms.find((alarm) => alarm.MetricName === 'IngestPartitions');
    expect(refusal?.Dimensions).toEqual([{ Name: 'Action', Value: 'blocked' }]);
    expect(refusal?.Namespace).toBe('Adlytics');
  });
});

/**
 * Every age alarm whose threshold is not its own queue's rendered retry envelope
 * (VisibilityTimeout × maxReceiveCount). Empty means each threshold is derived, not a literal.
 */
function ageCeilingDrift(rendered: Props): string[] {
  const all = rendered.Resources as Props;
  return Object.values(all)
    .filter((resource) => resource.Type === 'AWS::CloudWatch::Alarm')
    .map((resource) => resource.Properties as Props)
    .filter((alarm) => alarm.MetricName === 'ApproximateAgeOfOldestMessage')
    .flatMap((alarm) => {
      const queueId = (alarm.Dimensions as Props[])[0]?.Value?.['Fn::GetAtt']?.[0] as string;
      const queue = all[queueId].Properties as Props;
      const ceiling = queue.VisibilityTimeout * queue.RedrivePolicy.maxReceiveCount;
      return alarm.Threshold === ceiling
        ? []
        : [`${alarm.AlarmName}: ${alarm.Threshold} != ${ceiling}`];
    });
}

describe('tracing report I2 alarms', () => {
  it('alarms when a delivery or unit message outlives its whole retry envelope', () => {
    const template = synth();
    const ages = resources(template, 'AWS::CloudWatch::Alarm').filter(
      (alarm) => alarm.MetricName === 'ApproximateAgeOfOldestMessage',
    );
    expect(ages.map((alarm) => [alarm.AlarmName, alarm.Threshold]).sort()).toEqual([
      ['adlytics-dev-ingestion-oldest', 3 * 960],
      ['adlytics-dev-semantic-write-oldest', 3 * 960],
    ]);
    for (const alarm of ages) {
      expect(alarm).toEqual({
        AlarmName: alarm.AlarmName,
        AlarmDescription: alarm.AlarmDescription,
        AlarmActions: alarm.AlarmActions,
        Tags: alarm.Tags,
        Namespace: 'AWS/SQS',
        MetricName: 'ApproximateAgeOfOldestMessage',
        Dimensions: alarm.Dimensions,
        Statistic: 'Maximum',
        Period: 60,
        EvaluationPeriods: 1,
        Threshold: alarm.Threshold,
        ComparisonOperator: 'GreaterThanThreshold',
        TreatMissingData: 'notBreaching',
      });
    }
    expect(ageCeilingDrift(template.toJSON() as Props)).toEqual([]);
  });

  it('positive control: a queue envelope that moves without its alarm is caught', () => {
    const rendered = JSON.parse(JSON.stringify(synth().toJSON())) as Props;
    const queue = Object.values(rendered.Resources as Props).find(
      (resource) => resource.Properties?.QueueName === 'adlytics-dev-semantic-write.fifo',
    ) as Props;
    queue.Properties.RedrivePolicy.maxReceiveCount = 4;
    expect(ageCeilingDrift(rendered)).toEqual(['adlytics-dev-semantic-write-oldest: 2880 != 3840']);
  });

  it('queries the shared Errors metric once per fixed ingestion operation', () => {
    for (const config of [DISABLED_CONFIG, TEST_CONFIG]) {
      const template = synth(config);
      expect(resources(template, 'AWS::Logs::MetricFilter')).toEqual([]);
      const alarms = resources(template, 'AWS::CloudWatch::Alarm').filter((candidate) =>
        String(candidate.AlarmName).startsWith('adlytics-dev-ingestion-alarming-errors'),
      );
      expect(alarms.map((alarm) => alarm.AlarmName).sort()).toEqual([...ERROR_ALARM_NAMES].sort());
      INGESTION_ERROR_OPERATIONS.forEach((operation, index) => {
        const alarm = only(
          alarms.filter((candidate) => candidate.AlarmName === ERROR_ALARM_NAMES[index]),
          `alarming errors alarm for ${operation}`,
        );
        expect(alarm.Metrics).toEqual([
          {
            Expression: alarmingErrorsQuery(operation),
            Id: 'expr_1',
            Period: 300,
            ReturnData: true,
          },
        ]);
      });
    }
  });
  it('covers every other operation with one GROUP BY Operation catch-all on the staffed topic', () => {
    const provisioned = configWith({
      landing: { enabled: false },
      operationalStatus: { ...TEST_CONFIG.operationalStatus, provisioned: true },
    });
    // The exported page operations are exactly the pinned literal, in roster order.
    expect([...PAGE_OPERATIONS]).toEqual(PAGE_OPERATIONS_PINNED);
    for (const [config, excluded] of [
      [DISABLED_CONFIG, [...INGESTION_ERROR_OPERATIONS, ...PAGE_OPERATIONS_PINNED]],
      [
        provisioned,
        [...INGESTION_ERROR_OPERATIONS, ...PAGE_OPERATIONS_PINNED, METRIC_RECONCILER_OPERATION],
      ],
    ] as const) {
      const template = synth(config);
      const topic = only(
        Object.keys(template.findResources('AWS::SNS::Topic', {
          Properties: { TopicName: 'adlytics-dev-ingestion-alarms' },
        })),
        'ingestion alarm topic',
      );
      const alarm = only(
        resources(template, 'AWS::CloudWatch::Alarm').filter(
          (candidate) => candidate.AlarmName === 'adlytics-dev-alarming-errors-by-operation',
        ),
        'catch-all alarm',
      );
      const expression =
        'SELECT SUM(Errors) FROM SCHEMA("Adlytics", ErrorClass, ErrorCode, Operation) '
        + "WHERE ErrorClass != 'domain' AND ErrorClass != 'validation' AND "
        + excluded.map((operation) => `Operation != '${operation}'`).join(' AND ')
        + ' GROUP BY Operation ORDER BY SUM() DESC';
      expect(alarmingErrorsByOperationQuery(excluded)).toBe(expression);
      // The documented MetricDataQuery `Expression` maximum.
      expect(expression.length).toBeLessThanOrEqual(2048);
      expect(alarm).toEqual({
        AlarmName: 'adlytics-dev-alarming-errors-by-operation',
        // ADR-148: page operations are held by their page alarms, and the description says so.
        AlarmDescription: expect.stringContaining('ADR-148'),
        AlarmActions: [{ Ref: topic }],
        Tags: alarm.Tags,
        Metrics: [{ Expression: expression, Id: 'expr_1', Period: 300, ReturnData: true }],
        EvaluationPeriods: 1,
        Threshold: 1,
        ComparisonOperator: 'GreaterThanOrEqualToThreshold',
        TreatMissingData: 'notBreaching',
      });
    }
  });
  it('positive control: an operation literal that could end the quoted string is refused', () => {
    expect(() => alarmingErrorsByOperationQuery(["x' OR Operation != '"])).toThrow(
      /cannot be a Metrics Insights literal/,
    );
    expect(() => alarmingErrorsQuery('')).toThrow(/cannot be a Metrics Insights literal/);
  });
  it('pins the complete ingestion operation roster independently of the implementation', () => {
    expect([...INGESTION_ERROR_OPERATIONS]).toEqual([
      'execution_graph.stuck_flow_detector',
      'finalize_registration',
      'ingest.drain_last_receive',
      'ingest.handler',
      'ingest_delivery',
      'projection_handoff.callback',
      'semantic_table_writer',
      'stage1_plan_intent.reconcile',
      'stage1_provider_attempt.reconcile',
      'stage1_provider_fence.recovery',
    ]);
  });
  it('uses only the operators a Metrics Insights WHERE clause accepts (=, !=, AND)', () => {
    for (const operation of INGESTION_ERROR_OPERATIONS) {
      const query = alarmingErrorsQuery(operation);
      const where = query.slice(query.indexOf(' WHERE ') + 7);
      expect(where).not.toMatch(/\bIN\b|\bOR\b|\bLIKE\b|\(/);
      expect(query).toBe(
        'SELECT SUM(Errors) FROM SCHEMA("Adlytics", ErrorClass, ErrorCode, Operation) '
        + `WHERE ErrorClass != 'domain' AND ErrorClass != 'validation' AND Operation = '${operation}'`,
      );
    }
  });
  it('positive control: domain and validation outcomes stay outside every alarming query', () => {
    for (const operation of INGESTION_ERROR_OPERATIONS) {
      const query = alarmingErrorsQuery(operation);
      expect(query).toContain("ErrorClass != 'domain' AND ErrorClass != 'validation'");
      expect(query).not.toContain("ErrorClass = 'domain'");
      expect(query).not.toContain("ErrorClass = 'validation'");
      for (const forbidden of [
        'correlation',
        'trace',
        'audit',
        'identity',
        'session',
        'entity',
        'source',
        'publication',
        'request',
      ]) {
        expect(query.toLowerCase()).not.toContain(forbidden);
      }
    }
  });
  it('pages on one alarming error in five minutes for each ingestion operation', () => {
    const all = resources(synth(), 'AWS::CloudWatch::Alarm');
    INGESTION_ERROR_OPERATIONS.forEach((operation, index) => {
      const alarm = all.find((candidate) => candidate.AlarmName === ERROR_ALARM_NAMES[index]);
      expect(alarm).toEqual({
        AlarmName: ERROR_ALARM_NAMES[index],
        AlarmDescription: alarm?.AlarmDescription,
        AlarmActions: alarm?.AlarmActions,
        Tags: alarm?.Tags,
        Metrics: [
          {
            Expression: alarmingErrorsQuery(operation),
            Id: 'expr_1',
            Period: 300,
            ReturnData: true,
          },
        ],
        EvaluationPeriods: 1,
        Threshold: 1,
        ComparisonOperator: 'GreaterThanOrEqualToThreshold',
        TreatMissingData: 'notBreaching',
      });
    });
  });
});

// --- S3: graph-authority workers and mode-item grants (impl-plan S3 items 7 and 8) -------------

const PROVISIONED: AppConfig = configWith({
  operationalStatus: {
    provisioned: true,
    stage1LifecycleReconcileBatchSize: 10,
    stage1OutboxMaxConsecutiveFailedDeliveryAttempts: 3,
  },
});
const WORKER_HANDLERS = [
  'adapters.jobs.execution_graph.workers.dispatcher_handler',
  'adapters.jobs.execution_graph.workers.fence_settler_handler',
  'adapters.jobs.execution_graph.workers.promoter_handler',
  'adapters.jobs.execution_graph.workers.node_verifier_handler',
  // S4: the release issuer, the only grant writer, deployed disabled with the other workers.
  'adapters.jobs.execution_graph.workers.release_handler',
];
const GRAPH_TABLE = 'adlytics-dev-execution-graph';
const MODE_ONLY = { 'ForAllValues:StringEquals': { 'dynamodb:LeadingKeys': ['MODE#ingestion'] } };

function functionsByHandler(template: Template, handler: string): [string, Props][] {
  return Object.entries(template.findResources('AWS::Lambda::Function'))
    .map(([id, resource]) => [id, (resource as Props).Properties as Props] as [string, Props])
    .filter(([, fn]) => fn.Handler === handler);
}

function roleStatements(template: Template, fn: Props): Props[] {
  const roleId = ((fn.Role as Props)['Fn::GetAtt'] as string[])[0];
  // A role whose default policy passes IAM's inline size bound overflows into managed policies
  // CDK attaches to it (TableWriter does from S6 part 2), so both are the role's statements.
  return [
    ...resources(template, 'AWS::IAM::Policy'),
    ...resources(template, 'AWS::IAM::ManagedPolicy'),
  ]
    .filter((policy) => ((policy.Roles ?? []) as Props[]).some((role) => role.Ref === roleId))
    .flatMap((policy) => ((policy.PolicyDocument as Props).Statement ?? []) as Props[]);
}

function graphStatements(template: Template, fn: Props): Props[] {
  return roleStatements(template, fn).filter((statement) =>
    statementResourceTexts(statement).some((text) => text.includes(`table/${GRAPH_TABLE}"`)),
  );
}

/** S6 (V-19): the mode read plus the run-request writer's actions and the operator-only Deny. */
function expectAdmissionRequester(
  graph: Props[],
  conditionCheck: boolean,
  extra: readonly string[] = [],
): void {
  const bySid = new Map(graph.map((statement) => [statement.Sid as string, statement]));
  expect([...bySid.keys()].toSorted()).toEqual(
    [
      'AdmitRunRequests',
      'DenyOperatorOnlyGraphItems',
      conditionCheck ? 'ReadAndConditionCheckGraphModeItem' : 'ReadGraphModeItem',
      ...extra,
    ].toSorted(),
  );
  const mode = bySid.get(conditionCheck ? 'ReadAndConditionCheckGraphModeItem' : 'ReadGraphModeItem')!;
  expect(mode.Condition).toEqual(MODE_ONLY);
  expect(actionsOf(bySid.get('AdmitRunRequests')!).toSorted()).toEqual(
    [
      'dynamodb:ConditionCheckItem',
      'dynamodb:DeleteItem',
      'dynamodb:GetItem',
      'dynamodb:PutItem',
      'dynamodb:UpdateItem',
    ],
  );
  const deny = bySid.get('DenyOperatorOnlyGraphItems')!;
  expect(deny.Effect).toBe('Deny');
  expect(deny.Condition).toEqual({
    'ForAnyValue:StringLike': { 'dynamodb:LeadingKeys': ['MODE#*', 'COMMAND#*'] },
  });
}

function soleFunction(template: Template, handler: string): Props {
  const found = functionsByHandler(template, handler);
  expect(found).toHaveLength(1);
  return found[0]![1];
}

describe('S3 graph authority in the ingestion stack', () => {
  it('composes no worker, table or grant where the operational plane is not provisioned', () => {
    const template = synth(DISABLED_CONFIG);
    for (const handler of WORKER_HANDLERS) expect(functionsByHandler(template, handler)).toEqual([]);
    expect(JSON.stringify(template.toJSON())).not.toContain(GRAPH_TABLE);
  });

  it('deploys the five workers with their schedules enabled (§2.11 row 9; S3 deployed them disabled)', () => {
    const template = synth(PROVISIONED);
    const rules = resources(template, 'AWS::Events::Rule');
    for (const handler of WORKER_HANDLERS) {
      const [[logicalId, fn]] = functionsByHandler(template, handler) as [[string, Props]];
      expect(fn.ReservedConcurrentExecutions).toBe(1);
      expect((fn.Environment as Props).Variables.ADL_EXECUTION_GRAPH_TABLE).toBe(GRAPH_TABLE);
      // TD-279: one pass and one schedule are the contract's readiness-timeline inputs.
      expect(fn.Timeout).toBe(BAKE_TASKS.worker_pass_seconds);
      const schedule = rules.filter((rule) =>
        JSON.stringify(rule.Targets ?? []).includes(`"${logicalId}"`),
      );
      expect(schedule).toHaveLength(1);
      expect(schedule[0]!.State).toBe('ENABLED');
      expect(BAKE_TASKS.worker_schedule_seconds).toBe(60);
      expect(schedule[0]!.ScheduleExpression).toBe('rate(1 minute)');
    }
  });

  it('final runtime grants no migration-fence or retired day/wait access to graph writers', () => {
    const template = synth(PROVISIONED);
    for (const handler of [
      ...WORKER_HANDLERS,
      'adapters.inbound.ingestion_handlers.table_writer.handler',
    ]) {
      const fn = soleFunction(template, handler);
      const graph = graphStatements(template, fn);
      expect(graph.map((statement) => statement.Sid)).not.toContain(
        'ConditionRollingFreshnessMigrationFence',
      );
      const text = JSON.stringify(graph);
      for (const retired of [
        'MIGRATION#',
        'SERVING_DAY#',
        'CAPACITY_WAIT#',
        'PUBLISH_WAIT#',
        'SERVING_TELEMETRY#',
      ]) {
        expect([handler, text.includes(retired)]).toEqual([handler, false]);
      }
    }
  });

  it('gives the promoter exactly the rolling election, drain and transition authority', () => {
    const template = synth(PROVISIONED);
    const fn = soleFunction(template, 'adapters.jobs.execution_graph.workers.promoter_handler');
    const bySid = new Map(graphStatements(template, fn).map((statement) => [statement.Sid as string, statement]));
    const servingReads = bySid.get('ReadServingGraphFamilies')!;
    expect(actionsOf(servingReads)).toEqual(['dynamodb:GetItem']);
    // The election's read_serving_candidate GetItems `EXEC_CLAIM#<node>` (runtime-state check).
    const expectedServingReads = {
      'ForAllValues:StringLike': {
        'dynamodb:LeadingKeys': [
          'SERVING_DOMAIN#*',
          'SERVING_FRONTIER#*',
          'SERVING#*',
          'SERVING_PUBLICATION#*',
          'SERVING_TRANSITION#*',
          'EXEC_CLAIM#*',
        ],
      },
    };
    expect(servingReads.Condition).toEqual(expectedServingReads);
    // Control: the same statement without `EXEC_CLAIM#*` (the deployed defect) must fail.
    const withoutClaim = JSON.parse(JSON.stringify(servingReads.Condition)) as {
      'ForAllValues:StringLike': { 'dynamodb:LeadingKeys': string[] };
    };
    const claimKeys = withoutClaim['ForAllValues:StringLike']['dynamodb:LeadingKeys'];
    withoutClaim['ForAllValues:StringLike']['dynamodb:LeadingKeys'] = claimKeys.filter(
      (key) => key !== 'EXEC_CLAIM#*',
    );
    expect(() => expect(withoutClaim).toEqual(expectedServingReads)).toThrow();
    // Every serving write is a transaction member: the activation, the drains and the transition.
    const inTransaction = (keys: string[]): Props => ({
      'ForAllValues:StringLike': { 'dynamodb:LeadingKeys': keys },
      StringEquals: { 'dynamodb:EnclosingOperation': 'TransactWriteItems' },
    });
    const expected: Record<string, [string[], string[]]> = {
      ConditionPromoterServingAuthority: [
        ['dynamodb:ConditionCheckItem'],
        ['SERVING_DOMAIN#*', 'SERVING_FRONTIER#*', 'SERVING#*', 'SERVING_PUBLICATION#*'],
      ],
      PutPromoterServingAuthority: [['dynamodb:PutItem'], ['SERVING#*', 'SERVING_TRANSITION#*']],
      UpdatePromoterServingAuthority: [['dynamodb:UpdateItem'], ['SERVING#*', 'SERVING_DOMAIN#*']],
    };
    // The retired day-reservation telemetry and marker-delete grants are gone.
    expect([...bySid.keys()]).not.toContain('PutPromoterServingTelemetry');
    expect([...bySid.keys()]).not.toContain('DeletePromoterServingPublication');
    expect([...bySid.keys()]).not.toContain('PutUpdatePromoterServingAuthority');
    for (const [sid, [actions, keys]] of Object.entries(expected)) {
      const statement = bySid.get(sid);
      expect([sid, statement === undefined ? [] : actionsOf(statement)]).toEqual([sid, actions]);
      expect([sid, statement?.Condition]).toEqual([sid, inTransaction(keys)]);
      expect(statementResourceTexts(statement ?? {})).toEqual([
        expect.stringContaining(`table/${GRAPH_TABLE}`),
      ]);
    }
    // The immutable marker is only read or condition-checked by the promoter; a transition record
    // is put once and never updated.
    const writes = ['PutPromoterServingAuthority', 'UpdatePromoterServingAuthority']
      .map((sid) => bySid.get(sid));
    expect(JSON.stringify(writes)).not.toContain('SERVING_PUBLICATION#');
    expect(JSON.stringify(writes[1])).not.toContain('SERVING_TRANSITION#');
    expect(JSON.stringify(graphStatements(template, fn))).not.toContain('SERVING_TELEMETRY#');
    expect([servingReads, ...writes].flatMap((statement) => statementResourceTexts(statement ?? {}))).not.toContain('*');
  });

  it('item 7 and §2.11 row 2: TableWriter holds the mode check and the TableWriter family only', () => {
    const template = synth(PROVISIONED);
    const fn = soleFunction(template, 'adapters.inbound.ingestion_handlers.table_writer.handler');
    expect((fn.Environment as Props).Variables.ADL_EXECUTION_GRAPH_TABLE).toBe(GRAPH_TABLE);
    const bySid = new Map(graphStatements(template, fn).map((s) => [s.Sid as string, s]));
    expect([...bySid.keys()].toSorted()).toEqual([
      'DenyOperatorOnlyGraphWrites',
      'PutServingGraphFamilies',
      'ReadAndConditionCheckGraphModeItem',
      'ReadGraphFamilies',
      'ReadServingGraphFamilies',
      'TransactGraphFamilies',
      'UpdateGraphClaims',
      'UpdateServingGraphFamilies',
    ]);
    const mode = bySid.get('ReadAndConditionCheckGraphModeItem')!;
    expect(actionsOf(mode).toSorted()).toEqual(['dynamodb:ConditionCheckItem', 'dynamodb:GetItem']);
    expect(mode.Condition).toEqual(MODE_ONLY);
    const reads = bySid.get('ReadGraphFamilies')!;
    expect(actionsOf(reads)).toEqual(['dynamodb:GetItem', 'dynamodb:Query']);
    expect(reads.Condition).toEqual({
      'ForAllValues:StringLike': { 'dynamodb:LeadingKeys': ['RUN#*', 'EXEC_CLAIM#*'] },
    });
    const servingReads = bySid.get('ReadServingGraphFamilies')!;
    expect(actionsOf(servingReads)).toEqual(['dynamodb:GetItem']);
    // Publish GetItems `DUE#<n>/COMPLETE#<run_id>` (read_serving_publication_authority).
    const expectedServingReads = {
      'ForAllValues:StringLike': {
        'dynamodb:LeadingKeys': [
          'SERVING_FRONTIER#*',
          'SERVING_PUBLICATION#*',
          'DUE#*',
        ],
      },
    };
    expect(servingReads.Condition).toEqual(expectedServingReads);
    // Control: the same statement without `DUE#*` (the deployed defect) must fail this assertion.
    const withoutDue = JSON.parse(JSON.stringify(servingReads.Condition)) as {
      'ForAllValues:StringLike': { 'dynamodb:LeadingKeys': string[] };
    };
    const keys = withoutDue['ForAllValues:StringLike']['dynamodb:LeadingKeys'];
    withoutDue['ForAllValues:StringLike']['dynamodb:LeadingKeys'] = keys.filter((key) => key !== 'DUE#*');
    expect(() => expect(withoutDue).toEqual(expectedServingReads)).toThrow();
    // The rolling flip never reads or condition-checks the domain head: no serving condition
    // statement, and no SERVING_DOMAIN# key on any TableWriter statement.
    expect(bySid.has('ConditionServingGraphFamilies')).toBe(false);
    expect(JSON.stringify([...bySid.values()])).not.toContain('SERVING_DOMAIN#');
    const servingPuts = bySid.get('PutServingGraphFamilies')!;
    expect(actionsOf(servingPuts)).toEqual(['dynamodb:PutItem']);
    expect(servingPuts.Condition).toEqual({
      'ForAllValues:StringLike': { 'dynamodb:LeadingKeys': ['SERVING_PUBLICATION#*'] },
      StringEquals: { 'dynamodb:EnclosingOperation': 'TransactWriteItems' },
    });
    const servingUpdates = bySid.get('UpdateServingGraphFamilies')!;
    expect(actionsOf(servingUpdates)).toEqual(['dynamodb:UpdateItem']);
    expect(servingUpdates.Condition).toEqual({
      'ForAllValues:StringLike': {
        'dynamodb:LeadingKeys': [
          'SERVING_FRONTIER#*',
          'SERVING_PUBLICATION#*',
        ],
      },
      StringEquals: { 'dynamodb:EnclosingOperation': 'TransactWriteItems' },
    });
    const transact = bySid.get('TransactGraphFamilies')!;
    expect(actionsOf(transact)).toEqual([
      'dynamodb:PutItem',
      'dynamodb:UpdateItem',
      'dynamodb:DeleteItem',
      'dynamodb:ConditionCheckItem',
    ]);
    expect(transact.Condition).toEqual({
      'ForAllValues:StringLike': {
        'dynamodb:LeadingKeys': ['EXEC_CLAIM#*', 'RUN#*', 'DUE#*', 'TOKEN#*', 'LOCK#*', 'SLOT#*'],
      },
      StringEquals: { 'dynamodb:EnclosingOperation': 'TransactWriteItems' },
    });
    // The direct lease release (§2.3): EXEC_CLAIM# only, with no EnclosingOperation condition.
    const claims = bySid.get('UpdateGraphClaims')!;
    expect(actionsOf(claims)).toEqual(['dynamodb:UpdateItem']);
    expect(claims.Condition).toEqual({
      'ForAllValues:StringLike': { 'dynamodb:LeadingKeys': ['EXEC_CLAIM#*'] },
    });
    // No generic SERVING# binding family, COALESCE key, Query, DeleteItem, or direct write is added.
    const serving = [servingReads, servingPuts, servingUpdates];
    const servingText = JSON.stringify(serving);
    expect(servingText).not.toContain('SERVING#*');
    expect(servingText).not.toContain('COALESCE#');
    expect(serving.flatMap(actionsOf)).not.toContain('dynamodb:DeleteItem');
    expect(serving.flatMap(actionsOf)).not.toContain('dynamodb:Query');
    for (const statement of serving.slice(1)) {
      expect(statement.Condition.StringEquals).toEqual({
        'dynamodb:EnclosingOperation': 'TransactWriteItems',
      });
    }
    // No BLOCK# or LINEAGE# (BF-4G is dropped).
    const everything = JSON.stringify([...bySid.values()]);
    expect(everything).not.toContain('BLOCK#');
    expect(everything).not.toContain('LINEAGE#');
  });

  it('§2.11 row 2b: TableWriter stages, folds, flips and reads GENERATION# on publications', () => {
    const template = synth(PROVISIONED);
    const fn = soleFunction(template, 'adapters.inbound.ingestion_handlers.table_writer.handler');
    const bySid = new Map(roleStatements(template, fn).map((s) => [s.Sid as string, s]));
    const stage = bySid.get('StageAndFoldGrantPublications')!;
    expect(actionsOf(stage)).toEqual(['dynamodb:UpdateItem']);
    expect(statementResourceTexts(stage)[0]).toContain('table/adlytics-dev-v2-semantic-publications"');
    const datasetKeys = stage.Condition['ForAllValues:StringLike']['dynamodb:LeadingKeys'] as string[];
    expect(datasetKeys.length).toBeGreaterThan(0);
    expect(datasetKeys.every((key) => key.endsWith('|*'))).toBe(true);
    expect(datasetKeys).toStrictEqual([...CANONICAL_DATASET_KEYS]);
    expect(stage.Condition.StringEquals).toBeUndefined();
    const flip = bySid.get('FlipGenerationPointers')!;
    expect(actionsOf(flip)).toEqual(['dynamodb:UpdateItem']);
    expect(flip.Condition).toEqual({
      'ForAllValues:StringLike': { 'dynamodb:LeadingKeys': ['GENERATION#*'] },
      StringEquals: { 'dynamodb:EnclosingOperation': 'TransactWriteItems' },
    });
    const read = bySid.get('ReadGenerationPointers')!;
    expect(actionsOf(read)).toEqual(['dynamodb:GetItem']);
    expect(read.Condition).toEqual({
      'ForAllValues:StringLike': { 'dynamodb:LeadingKeys': ['GENERATION#*'] },
    });
    // Control: without the graph authority none of the three exists.
    const disabled = synth(DISABLED_CONFIG);
    const plain = soleFunction(disabled, 'adapters.inbound.ingestion_handlers.table_writer.handler');
    const sids = roleStatements(disabled, plain).map((s) => s.Sid);
    for (const sid of ['StageAndFoldGrantPublications', 'FlipGenerationPointers', 'ReadGenerationPointers']) {
      expect(sids).not.toContain(sid);
    }
  });

  it('plan 4.6 under A3: exactly one statement in every template may update GENERATION#, TableWriter\'s flip', () => {
    /** Every Allow statement with UpdateItem whose LeadingKeys name `GENERATION#*`, by stack/Sid. */
    const flippers = (templates: Record<string, unknown>): string[] =>
      Object.entries(templates).flatMap(([stack, template]) =>
        Object.values((template as { Resources: Record<string, { Type: string; Properties: Props }> }).Resources)
          .flatMap((resource) =>
            resource.Type === 'AWS::IAM::Policy' || resource.Type === 'AWS::IAM::ManagedPolicy'
              ? (((resource.Properties.PolicyDocument as Props).Statement ?? []) as Props[])
              : [],
          )
          .filter((statement) => statement.Effect === 'Allow')
          .filter((statement) => actionsOf(statement).includes('dynamodb:UpdateItem'))
          .filter((statement) => JSON.stringify(statement.Condition ?? {}).includes('"GENERATION#*"'))
          .map((statement) => `${stack}/${String(statement.Sid)}`),
      );
    const every = everyTemplate();
    expect(flippers(every)).toEqual(['IngestionStack/FlipGenerationPointers']);
    // Control: a template where the Baker also holds a generation flip has two flippers.
    const publisher = structuredClone(every.PublisherStack) as {
      Resources: Record<string, { Type: string; Properties: Props }>;
    };
    const policy = Object.values(publisher.Resources).find((resource) => resource.Type === 'AWS::IAM::Policy')!;
    ((policy.Properties.PolicyDocument as Props).Statement as Props[]).push({
      Sid: 'FinalizeGenerationPointers',
      Effect: 'Allow',
      Action: 'dynamodb:UpdateItem',
      Resource: '*',
      Condition: { 'ForAllValues:StringLike': { 'dynamodb:LeadingKeys': ['GENERATION#*'] } },
    });
    expect(flippers({ ...every, PublisherStack: publisher }).toSorted()).toEqual([
      'IngestionStack/FlipGenerationPointers',
      'PublisherStack/FinalizeGenerationPointers',
    ]);
  });

  it('S6 (V-19): the delivery handler reads the mode and admits as a workload requester', () => {
    const template = synth(PROVISIONED);
    const fn = soleFunction(template, 'adapters.inbound.ingestion_handlers.load.handler');
    expect((fn.Environment as Props).Variables.ADL_EXECUTION_GRAPH_TABLE).toBe(GRAPH_TABLE);
    expectAdmissionRequester(graphStatements(template, fn), true, ['QueryGraphRunNodes']);
  });

  it('§2.11 row 3: the delivery handler gains exactly one Query statement, on RUN#, and no GetItem', () => {
    const template = synth(PROVISIONED);
    const fn = soleFunction(template, 'adapters.inbound.ingestion_handlers.load.handler');
    const graph = graphStatements(template, fn);
    const queries = graph.filter((statement) => actionsOf(statement).includes('dynamodb:Query'));
    expect(queries).toHaveLength(1);
    expect(queries[0]!.Sid).toBe('QueryGraphRunNodes');
    expect(actionsOf(queries[0]!)).toEqual(['dynamodb:Query']);
    expect(queries[0]!.Condition).toEqual({
      'ForAllValues:StringLike': { 'dynamodb:LeadingKeys': ['RUN#*'] },
    });
    // WRITER_ACTIONS' table-wide GetItem and the mode read are the only GetItem statements.
    const gets = graph.filter((statement) => actionsOf(statement).includes('dynamodb:GetItem'));
    expect(gets.map((statement) => statement.Sid).toSorted()).toEqual([
      'AdmitRunRequests',
      'ReadAndConditionCheckGraphModeItem',
    ]);
  });

  it('§2.11 row 1: the dispatcher sends to the three other routes by deterministic name', () => {
    const template = synth(PROVISIONED);
    const fn = soleFunction(template, 'adapters.jobs.execution_graph.workers.dispatcher_handler');
    const routes = roleStatements(template, fn).filter((s) => s.Sid === 'SendGraphRouteEnvelopes');
    expect(routes).toHaveLength(1);
    expect(actionsOf(routes[0]!)).toEqual(['sqs:SendMessage']);
    const texts = statementResourceTexts(routes[0]!);
    expect(texts).toHaveLength(3);
    for (const [index, name] of [
      'adlytics-dev-ingestion',
      'adlytics-dev-preset-bake.fifo',
      'adlytics-dev-insight-precompute.fifo',
    ].entries()) {
      expect(texts[index]).toContain(`:sqs:eu-west-1:111122223333:${name}"`);
    }
    // No other worker may send to a graph route.
    for (const handler of WORKER_HANDLERS.filter((h) => !h.endsWith('dispatcher_handler'))) {
      const sids = roleStatements(template, soleFunction(template, handler)).map((s) => s.Sid);
      expect(sids).not.toContain('SendGraphRouteEnvelopes');
    }
  });

  it('§2.11 row 5b: one post-grant invariant alarm sums every closed signal, on the staffed topic', () => {
    const template = synth(PROVISIONED);
    const topicId = Object.keys(template.findResources('AWS::SNS::Topic'))[0]!;
    const [alarm] = resources(template, 'AWS::CloudWatch::Alarm').filter(
      (item) => item.AlarmName === 'adlytics-dev-graph-post-grant-invariant',
    );
    expect(alarm).toBeDefined();
    expect(alarm!.AlarmActions).toEqual([{ Ref: topicId }]);
    expect(alarm!.Threshold).toBe(1);
    expect(alarm!.ComparisonOperator).toBe('GreaterThanOrEqualToThreshold');
    expect(alarm!.TreatMissingData).toBe('notBreaching');
    const metrics = alarm!.Metrics as Props[];
    const expression = metrics.find((metric) => metric.Expression !== undefined)!;
    expect(expression.Expression).toBe(
      POST_GRANT_INVARIANT_SIGNALS.map((_, index) => `FILL(s${index}, 0)`).join(' + '),
    );
    const signals = metrics
      .filter((metric) => metric.MetricStat !== undefined)
      .map((metric) => metric.MetricStat.Metric);
    expect(signals).toHaveLength(POST_GRANT_INVARIANT_SIGNALS.length);
    for (const metric of signals) {
      expect(metric.Namespace).toBe('Adlytics');
      expect(metric.MetricName).toBe('GraphPostGrantInvariant');
      // One namespace, no function dimension: only the protocol and the closed signal.
      expect((metric.Dimensions as Props[]).map((d) => d.Name).toSorted()).toEqual(['Protocol', 'Signal']);
    }
    expect(
      signals.map((metric) => (metric.Dimensions as Props[]).find((d) => d.Name === 'Signal')!.Value).toSorted(),
    ).toEqual([...POST_GRANT_INVARIANT_SIGNALS].toSorted());
    // The closed set is the emitters' own constants, one per fail-closed POST_GRANT path.
    const sources = [
      'src/domain/operations/execution_graph_dispatch.py',
      'src/adapters/inbound/ingestion_handlers/graph_publish.py',
      'src/adapters/jobs/preset_artifact_publisher/graph_bake.py',
      'src/adapters/jobs/execution_graph/post_grant_consumer.py',
    ].map((file) => readFileSync(path.join(__dirname, '..', '..', file), 'utf8'));
    POST_GRANT_INVARIANT_SIGNALS.forEach((signal, index) => {
      expect(sources[index]).toMatch(new RegExp(`^[A-Z_]+: Final = "${signal}"$`, 'm'));
    });
    // Control: no graph authority, no alarm.
    const names = resources(synth(DISABLED_CONFIG), 'AWS::CloudWatch::Alarm').map((a) => a.AlarmName);
    expect(names).not.toContain('adlytics-dev-graph-post-grant-invariant');
  });

  it('item 7: the plan-intent reconciler reads the mode item and holds nothing else on the table', () => {
    const template = synth(PROVISIONED);
    const fn = soleFunction(template, 'adapters.jobs.plan_intent_reconciler.handler');
    const graph = graphStatements(template, fn);
    expect(graph).toHaveLength(1);
    expect(actionsOf(graph[0]!)).toEqual(['dynamodb:GetItem']);
    expect(graph[0]!.Condition).toEqual(MODE_ONLY);
  });

  it('S6 (V-19): the registration finalizer reads the mode and admits its schedule request', () => {
    const template = synth(configWith({ operationalStatus: PROVISIONED.operationalStatus }));
    const fn = soleFunction(template, 'adapters.inbound.producer_handlers.finalizer.handler');
    expect((fn.Environment as Props).Variables.ADL_EXECUTION_GRAPH_TABLE).toBe(GRAPH_TABLE);
    expectAdmissionRequester(graphStatements(template, fn), false);
  });

  it('each worker writes only its families inside transactions and is denied the operator items', () => {
    const template = synth(PROVISIONED);
    for (const handler of WORKER_HANDLERS) {
      const fn = soleFunction(template, handler);
      const graph = graphStatements(template, fn);
      const transact = graph.find((statement) => statement.Sid === 'TransactGraphFamilies');
      expect(transact!.Condition.StringEquals).toEqual({
        'dynamodb:EnclosingOperation': 'TransactWriteItems',
      });
      const families = transact!.Condition['ForAllValues:StringLike']['dynamodb:LeadingKeys'] as string[];
      expect(families).not.toContain('MODE#*');
      expect(families).not.toContain('COMMAND#*');
      const deny = graph.find((statement) => statement.Effect === 'Deny');
      expect(actionsOf(deny!)).toEqual(['dynamodb:PutItem', 'dynamodb:UpdateItem', 'dynamodb:DeleteItem']);
      expect(deny!.Condition).toEqual({
        'ForAnyValue:StringLike': { 'dynamodb:LeadingKeys': ['MODE#*', 'COMMAND#*'] },
      });
      const sends = roleStatements(template, fn).filter((s) => actionsOf(s).includes('sqs:SendMessage'));
      // The dispatcher: the semantic write FIFO, and the three other routes (§2.11 row 1).
      expect(sends).toHaveLength(handler.endsWith('dispatcher_handler') ? 2 : 0);
    }
  });

  it('S6 §2.1: only the dispatcher carries a queue URL per contract route, by deterministic name', () => {
    const template = synth(PROVISIONED);
    const routes: Record<string, string> = {
      ADL_GRAPH_DELIVERY_QUEUE_URL: '/adlytics-dev-ingestion"',
      ADL_GRAPH_BAKE_QUEUE_URL: '/adlytics-dev-preset-bake.fifo"',
      ADL_GRAPH_PRECOMPUTE_QUEUE_URL: '/adlytics-dev-insight-precompute.fifo"',
    };
    for (const handler of WORKER_HANDLERS) {
      const variables = (soleFunction(template, handler).Environment as Props).Variables as Props;
      for (const [name, queue] of Object.entries(routes)) {
        if (handler.endsWith('dispatcher_handler')) {
          expect(JSON.stringify(variables[name])).toContain(queue);
        } else {
          expect(variables[name]).toBeUndefined();
        }
      }
    }
    // The delivery URL names this stack's own standard queue (the bake name is the Publisher's,
    // asserted in publisher-stacks.test.ts; the precompute FIFO lands with slice 5i).
    const names = resources(template, 'AWS::SQS::Queue').map((queue) => queue.QueueName as string);
    expect(names).toContain('adlytics-dev-ingestion');
  });

  it('S4: the release issuer writes the release families and reads legacy authority read-only', () => {
    const template = synth(PROVISIONED);
    const fn = soleFunction(template, 'adapters.jobs.execution_graph.workers.release_handler');
    const transact = graphStatements(template, fn).find((s) => s.Sid === 'TransactGraphFamilies');
    const families = transact!.Condition['ForAllValues:StringLike']['dynamodb:LeadingKeys'] as string[];
    for (const family of ['GRANT#*', 'LINEAGE#*', 'RELEASE_ORDER#*', 'RESERVATION_SET#*',
      'TARGET#*', 'HEAD#*', 'BLOCK#*', 'SLOT#*', 'COHORT#*', 'SERVING#*']) {
      expect(families).toContain(family);
    }
    const statements = roleStatements(template, fn);
    const graph = graphStatements(template, fn);
    const bySid = new Map(graph.map((statement) => [statement.Sid as string, statement]));
    const servingKeys = ['SERVING_DOMAIN#*', 'SERVING_FRONTIER#*'];
    const servingReads = bySid.get('ReadServingGraphFamilies')!;
    expect(actionsOf(servingReads)).toEqual(['dynamodb:GetItem']);
    expect(servingReads.Condition).toEqual({
      'ForAllValues:StringLike': { 'dynamodb:LeadingKeys': servingKeys },
    });
    for (const [sid, action] of [
      ['ConditionReleaseServingAuthority', 'dynamodb:ConditionCheckItem'],
      ['PutReleaseServingAuthority', 'dynamodb:PutItem'],
      ['UpdateReleaseServingAuthority', 'dynamodb:UpdateItem'],
    ] as const) {
      const statement = bySid.get(sid)!;
      expect(actionsOf(statement)).toEqual([action]);
      // Only the head is updated (direct drain); first-use frontier shards are put, never updated.
      const keys = sid === 'UpdateReleaseServingAuthority' ? ['SERVING_DOMAIN#*'] : servingKeys;
      expect(statement.Condition).toEqual({
        'ForAllValues:StringLike': { 'dynamodb:LeadingKeys': keys },
        StringEquals: { 'dynamodb:EnclosingOperation': 'TransactWriteItems' },
      });
      expect(statementResourceTexts(statement)).toEqual([
        expect.stringContaining(`table/${GRAPH_TABLE}`),
      ]);
    }
    const servingGrant = [
      servingReads,
      bySid.get('ConditionReleaseServingAuthority')!,
      bySid.get('PutReleaseServingAuthority')!,
      bySid.get('UpdateReleaseServingAuthority')!,
    ];
    expect(JSON.stringify(servingGrant)).not.toContain('SERVING_PUBLICATION#');
    expect(servingGrant.flatMap(actionsOf)).not.toContain('dynamodb:DeleteItem');
    expect(servingGrant.flatMap(actionsOf)).not.toContain('dynamodb:Query');
    expect(servingGrant.flatMap(statementResourceTexts)).not.toContain('*');
    const lists = statements.filter((s) => actionsOf(s).includes('s3:ListBucketVersions'));
    expect(lists).toHaveLength(1);
    expect(actionsOf(lists[0]!).toSorted()).toEqual(['s3:ListBucket', 's3:ListBucketVersions']);
    expect(statements.some((s) => actionsOf(s).some((a) => a === 's3:PutObject'))).toBe(false);
    const plans = statements.filter((s) => JSON.stringify(s).includes('WRITE_PLAN#*'));
    expect(plans).toHaveLength(1);
    expect(actionsOf(plans[0]!)).toEqual(['dynamodb:GetItem']);
    // The promoter reads and ConditionChecks the co-release domain's block (S1A-8).
    const promoter = soleFunction(template, 'adapters.jobs.execution_graph.workers.promoter_handler');
    const promoterTransact = graphStatements(template, promoter).find(
      (s) => s.Sid === 'TransactGraphFamilies',
    );
    expect(promoterTransact!.Condition['ForAllValues:StringLike']['dynamodb:LeadingKeys']).toContain(
      'BLOCK#*',
    );
  });

  it('S3V-9: only the promoter may condition-check the mode item, the hold revision it read', () => {
    const template = synth(PROVISIONED);
    for (const handler of WORKER_HANDLERS) {
      const fn = soleFunction(template, handler);
      const mode = graphStatements(template, fn).filter(
        (statement) => JSON.stringify(statement.Condition ?? {}) === JSON.stringify(MODE_ONLY),
      );
      expect(mode).toHaveLength(1);
      expect(actionsOf(mode[0]!).toSorted()).toEqual(
        handler.endsWith('promoter_handler')
          ? ['dynamodb:ConditionCheckItem', 'dynamodb:GetItem']
          : ['dynamodb:GetItem'],
      );
    }
  });
});
// --- S5: the stuck-flow detector, the graph protocol alarms and the staffed destination --------
const DETECTOR_HANDLER = 'adapters.jobs.execution_graph.stuck_flow.handler';
const DUE_ONLY = { 'ForAllValues:StringLike': { 'dynamodb:LeadingKeys': ['DUE#*'] } };
const COHORT_ONLY = { 'ForAllValues:StringLike': { 'dynamodb:LeadingKeys': ['COHORT#*'] } };
/** Plan 6.1 (F9): the detector's graph statements are exactly these three, by Sid and shape. */
function expectDetectorStatements(graph: Props[]): void {
  expect(graph.map((statement) => statement.Sid).toSorted()).toEqual([
    'QueryDueShards',
    'ReadBakeContributions',
    'ReadGraphModeItem',
  ]);
  const bySid = new Map(graph.map((statement) => [statement.Sid as string, statement]));
  expect(actionsOf(bySid.get('ReadGraphModeItem')!)).toEqual(['dynamodb:GetItem']);
  expect(bySid.get('ReadGraphModeItem')!.Condition).toEqual(MODE_ONLY);
  expect(actionsOf(bySid.get('QueryDueShards')!)).toEqual(['dynamodb:Query']);
  expect(bySid.get('QueryDueShards')!.Condition).toEqual(DUE_ONLY);
  expect(actionsOf(bySid.get('ReadBakeContributions')!)).toEqual(['dynamodb:GetItem']);
  expect(bySid.get('ReadBakeContributions')!.Condition).toEqual(COHORT_ONLY);
}
/** Plan 6.2: one bake alarm row read back field by field from the synthesized alarm. */
function expectBakeAlarm(alarm: Props | undefined, spec: (typeof GRAPH_ALARMS)[number], topicId: string): void {
  expect(alarm).toEqual(expect.objectContaining({
    AlarmName: `adlytics-dev-${spec.suffix}`,
    Namespace: 'Adlytics',
    MetricName: spec.metricName,
    Dimensions: [
      { Name: 'AuthorityScope', Value: 'live' },
      { Name: 'Protocol', Value: 'v1' },
    ],
    Statistic: 'Maximum',
    // TD-279: the detector's derived thresholds add exactly one of these periods.
    Period: BAKE_TASKS.worker_schedule_seconds,
    Threshold: 1,
    ComparisonOperator: 'GreaterThanOrEqualToThreshold',
    EvaluationPeriods: 1,
    DatapointsToAlarm: 1,
    TreatMissingData: spec.missing,
    AlarmActions: [{ Ref: topicId }],
    OKActions: [{ Ref: topicId }],
  }));
}
const BAKE_SWEEP_ALARMS = [
  'graph-bake-task-failed',
  'graph-bake-retry-exhausted',
  'graph-bake-precompute-blocked',
  'graph-bake-finalizer-blocked',
];
const BAKE_TRACE_ALARMS = ['graph-bake-telemetry-loss', 'graph-bake-trace-incomplete'];
describe('S5 measurement and alarms in the ingestion stack', () => {
  it('composes no detector or graph alarm where the operational plane is not provisioned', () => {
    const template = synth(DISABLED_CONFIG);
    expect(functionsByHandler(template, DETECTOR_HANDLER)).toEqual([]);
    const names = resources(template, 'AWS::CloudWatch::Alarm').map((alarm) => alarm.AlarmName);
    for (const spec of GRAPH_ALARMS) expect(names).not.toContain(`adlytics-dev-${spec.suffix}`);
  });
  it('runs the detector every minute in every mode with one reader and no write action', () => {
    const template = synth(PROVISIONED);
    const [[logicalId, fn]] = functionsByHandler(template, DETECTOR_HANDLER) as [[string, Props]];
    expect(fn.ReservedConcurrentExecutions).toBe(1);
    expect((fn.Environment as Props).Variables.ADL_EXECUTION_GRAPH_TABLE).toBe(GRAPH_TABLE);
    const schedule = resources(template, 'AWS::Events::Rule').filter((rule) =>
      JSON.stringify(rule.Targets ?? []).includes(`"${logicalId}"`),
    );
    expect(schedule).toHaveLength(1);
    expect(schedule[0]!.State).toBe('ENABLED');
    expect(schedule[0]!.ScheduleExpression).toBe('rate(1 minute)');
    const graph = graphStatements(template, fn);
    expectDetectorStatements(graph);
    // Control (F9): the contribution read widened to every partition fails the exact set.
    const widened = graph.map((statement) => (statement.Sid === 'ReadBakeContributions'
      ? { ...statement, Condition: { 'ForAllValues:StringLike': { 'dynamodb:LeadingKeys': ['*'] } } }
      : statement));
    expect(() => expectDetectorStatements(widened)).toThrow();
    const everything = JSON.stringify(roleStatements(template, fn));
    for (const action of [
      'dynamodb:PutItem',
      'dynamodb:UpdateItem',
      'dynamodb:DeleteItem',
      'dynamodb:ConditionCheckItem',
      'dynamodb:Scan',
      'sqs:SendMessage',
    ]) {
      expect(everything).not.toContain(action);
    }
  });
  it('generates every graph alarm exactly, acting only on the staffed destination', () => {
    const template = synth(PROVISIONED);
    const alarms = resources(template, 'AWS::CloudWatch::Alarm');
    const topic = resources(template, 'AWS::SNS::Topic');
    expect(topic).toHaveLength(1);
    const topicId = Object.keys(template.findResources('AWS::SNS::Topic'))[0]!;
    expect(GRAPH_ALARMS.map((spec) => [spec.suffix, spec.scope])).toEqual([
      ['open-flows-past-ceiling', 'live'],
      ['open-flows-without-root-clock', 'live'],
      ['promote-obligation-age', 'live'],
      ...[...BAKE_SWEEP_ALARMS, ...BAKE_TRACE_ALARMS].map((suffix) => [suffix, 'live']),
      ['graph-serving-withheld', 'live'],
      ['open-flows-past-ceiling-probe', 'probe'],
    ]);
    for (const spec of GRAPH_ALARMS) {
      const alarm = alarms.find((item) => item.AlarmName === `adlytics-dev-${spec.suffix}`);
      expect(alarm).toEqual({
        AlarmName: `adlytics-dev-${spec.suffix}`,
        AlarmDescription: spec.description,
        Namespace: 'Adlytics',
        MetricName: spec.metricName,
        Dimensions: [
          { Name: 'AuthorityScope', Value: spec.scope },
          { Name: 'Protocol', Value: 'v1' },
        ],
        Statistic: 'Maximum',
        Period: spec.periodSeconds,
        Threshold: spec.threshold,
        ComparisonOperator: 'GreaterThanOrEqualToThreshold',
        EvaluationPeriods: 1,
        DatapointsToAlarm: 1,
        TreatMissingData: spec.missing,
        AlarmActions: [{ Ref: topicId }],
        ...(spec.notifyOk ? { OKActions: [{ Ref: topicId }] } : {}),
        Tags: alarm?.Tags,
      });
    }
    // Plan 6.2: each bake row reads back its metric, dimensions, threshold, evaluation, both
    // actions and missing-data treatment. The four sweep gauges treat silence as missing (the
    // stuck-flow alarm owns an unreadable sweep); the two A2 event counts as not breaching.
    for (const suffix of [...BAKE_SWEEP_ALARMS, ...BAKE_TRACE_ALARMS]) {
      const spec = GRAPH_ALARMS.find((item) => item.suffix === suffix)!;
      const alarm = alarms.find((item) => item.AlarmName === `adlytics-dev-${suffix}`);
      expectBakeAlarm(alarm, spec, topicId);
      expect(spec.missing).toBe(BAKE_SWEEP_ALARMS.includes(suffix) ? 'missing' : 'notBreaching');
      // Control: the same comparison fails on a spec that treats missing data as breaching.
      expect(() => expectBakeAlarm(alarm, { ...spec, missing: cdk.aws_cloudwatch.TreatMissingData.BREACHING }, topicId))
        .toThrow();
    }
    // ADR-139: the withheld-cohort gauge ignores missing data, so only a real 0 clears ALARM.
    const withheldSpec = GRAPH_ALARMS.find((item) => item.id === 'GraphServingWithheld')!;
    const withheld = alarms.find((item) => item.AlarmName === 'adlytics-dev-graph-serving-withheld');
    expectBakeAlarm(withheld, withheldSpec, topicId);
    expect(withheld).toEqual(expect.objectContaining({
      MetricName: 'GraphServingWithheld',
      Statistic: 'Maximum',
      TreatMissingData: 'ignore',
      OKActions: [{ Ref: topicId }],
    }));
    // Control: the same comparison fails on a spec that treats missing data as not breaching.
    expect(() => expectBakeAlarm(withheld, {
      ...withheldSpec, missing: cdk.aws_cloudwatch.TreatMissingData.NOT_BREACHING,
    }, topicId)).toThrow();
    // D10: no cleanup alarm.
    expect(alarms.filter((alarm) => String(alarm.AlarmName).includes('cleanup'))).toEqual([]);
    // The live stuck-flow alarm is the only one that treats silence as breaching.
    const breaching = alarms.filter((alarm) => alarm.TreatMissingData === 'breaching');
    expect(breaching.map((alarm) => alarm.AlarmName)).toEqual(['adlytics-dev-open-flows-past-ceiling']);
    expect(PROMOTE_AGE_ALARM_SECONDS).toBe(900);
    // Every alarm in the stack acts on that one topic, and on nothing else.
    for (const alarm of alarms) {
      expect(alarm.AlarmActions).toEqual([{ Ref: topicId }]);
      for (const ok of (alarm.OKActions ?? []) as unknown[]) expect(ok).toEqual({ Ref: topicId });
    }
  });
  it('ADR-140 I1: derives both blocked windows from BAKE_TASKS and names them in the alarms', () => {
    // The Python twin is `test_f9_the_age_gates_derive_from_the_contract_readiness_timeline`.
    const ready = BAKE_TASKS.worker_schedule_seconds + BAKE_TASKS.worker_pass_seconds
      + BAKE_TASKS.precompute_claim_lease_seconds;
    expect(PRECOMPUTE_BLOCKED_AFTER_SECONDS).toBe(ready + BAKE_TASKS.worker_schedule_seconds);
    expect(FINALIZER_BLOCKED_AFTER_SECONDS)
      .toBe(ready + BAKE_TASKS.control_claim_seconds + BAKE_TASKS.worker_schedule_seconds);
    expect([PRECOMPUTE_BLOCKED_AFTER_SECONDS, FINALIZER_BLOCKED_AFTER_SECONDS]).toEqual([240, 270]);
    const alarms = resources(synth(PROVISIONED), 'AWS::CloudWatch::Alarm');
    const described = (suffix: string): string => String(
      alarms.find((item) => item.AlarmName === `adlytics-dev-${suffix}`)!.AlarmDescription,
    );
    const precompute = described('graph-bake-precompute-blocked');
    const finalizer = described('graph-bake-finalizer-blocked');
    expect(precompute).toContain(`more than ${PRECOMPUTE_BLOCKED_AFTER_SECONDS} s`);
    expect(precompute).toContain('ADLY-GRAPH_PRECOMPUTE-0007');
    expect(finalizer).toContain(`more than ${FINALIZER_BLOCKED_AFTER_SECONDS} s`);
    for (const text of [precompute, finalizer]) {
      expect(text).not.toMatch(/60-second|30-second/);
      expect(text.length).toBeLessThanOrEqual(1024);
    }
  });
  it('FR-10: resolves exactly one staffed destination arm and refuses two', () => {
    expect(SELECTED_ARMS).toEqual({ dev: 'ExistingInAccountTopicEmail' });
    expect(staffedAlarmDestinationConfig('dev', undefined)).toEqual({ arm: 'ExistingInAccountTopicEmail' });
    expect(() => staffedAlarmDestinationConfig('dev', 'arn:aws:sns:us-east-1:111122223333:other'))
      .toThrow(/conflicts with the selected email arm/);
    expect(staffedAlarmDestinationConfig('stage', undefined)).toBeUndefined();
    expect(() => staffedAlarmDestinationConfig('stage', 'arn:aws:sns:us-east-1:111122223333:x'))
      .toThrow(/No staffed alarm destination arm is selected/);
    // Owner-gate Item 20: the policy names the owner only; the acknowledgement fields are gone.
    expect(Object.keys(STAFFING_POLICY).sort()).toEqual(['policy_revision', 'responsible_owner_id']);
  });
  it('FR-10: the topic arm imports its exact ARN for every alarm', () => {
    const imported = 'arn:aws:sns:eu-west-1:111122223333:staffed-fixture';
    const template = synth(configWith({
      ...PROVISIONED,
      staffedAlarmDestination: { arm: 'ExistingConfirmedSnsTopic', topicArn: imported },
    }));
    for (const alarm of resources(template, 'AWS::CloudWatch::Alarm')) {
      expect(alarm.AlarmActions).toEqual([imported]);
    }
  });
});


describe('S6 ingress, drain and legacy disablement', () => {
  const DRAIN_ONLY = {
    'ForAllValues:StringEquals': { 'dynamodb:LeadingKeys': ['CUTOVER#ingestion'] },
  };
  const drainStatements = (template: Template, handler: string): Props[] =>
    roleStatements(template, soleFunction(template, handler)).filter(
      (statement) => statement.Sid === 'CountDrainSequence',
    );

  it('V-17b: each legacy family-changing writer may add to DRAIN_SEQ and nothing else by it', () => {
    const template = synth(PROVISIONED);
    for (const handler of [
      'adapters.inbound.ingestion_handlers.load.handler',
      'adapters.inbound.ingestion_handlers.table_writer.handler',
      'adapters.jobs.plan_intent_reconciler.handler',
    ]) {
      const drain = drainStatements(template, handler);
      expect(drain).toHaveLength(1);
      expect(actionsOf(drain[0]!)).toEqual(['dynamodb:UpdateItem']);
      expect(drain[0]!.Condition).toEqual(DRAIN_ONLY);
      expect(JSON.stringify(drain[0]!.Resource)).toContain('v2-semantic-publications');
    }
  });

  it('V-17b: T_max is the largest configured legacy-writer timeout, as rendered', () => {
    const template = synth(PROVISIONED);
    for (const writer of LEGACY_WRITERS) {
      const rendered = Object.entries(template.findResources('AWS::Lambda::Function')).filter(
        ([id]) => id.includes(writer.function),
      );
      expect(rendered.length).toBeGreaterThanOrEqual(1);
      for (const [, resource] of rendered) {
        expect(((resource as Props).Properties as Props).Timeout).toBe(writer.timeout_seconds);
      }
    }
    expect(T_MAX_SECONDS).toBe(Math.max(...LEGACY_WRITERS.map((writer) => writer.timeout_seconds)));
  });

  it('V-19: the delivery handler holds a drained delivery and reports it as a batch failure', () => {
    const template = synth(PROVISIONED);
    const fn = soleFunction(template, 'adapters.inbound.ingestion_handlers.load.handler');
    const hold = roleStatements(template, fn).filter(
      (statement) => statement.Sid === 'HoldDrainedDeliveries',
    );
    expect(hold).toHaveLength(1);
    expect(actionsOf(hold[0]!)).toEqual(['sqs:ChangeMessageVisibility']);
    const mappings = resources(template, 'AWS::Lambda::EventSourceMapping').filter((mapping) =>
      JSON.stringify(mapping.EventSourceArn).includes('DeliveryQueue'),
    );
    expect(mappings).toHaveLength(1);
    expect(mappings[0]!.FunctionResponseTypes).toEqual(['ReportBatchItemFailures']);
    expect(mappings[0]!.BatchSize).toBe(1);
  });
  it('TD-61: only the delivery queue consumer may change a delivery message visibility', () => {
    // The materialize_raw transient-refusal redelivery (graph_materialize.py) calls
    // ChangeMessageVisibility on the delivery queue; exactly its consuming role holds it.
    const template = synth(PROVISIONED);
    const fn = soleFunction(template, INGESTION_HANDLER);
    const consumer = ((fn.Role as Props)['Fn::GetAtt'] as string[])[0];
    const mapping = only(
      resources(template, 'AWS::Lambda::EventSourceMapping').filter((candidate) =>
        JSON.stringify(candidate.EventSourceArn).includes('DeliveryQueue'),
      ),
      'delivery queue mapping',
    );
    const queueId = ((mapping.EventSourceArn as Props)['Fn::GetAtt'] as string[])[0];
    const changesVisibility = (statement: Props): boolean =>
      statement.Effect === 'Allow' &&
      actionsOf(statement).some((action) =>
        ['sqs:ChangeMessageVisibility', 'sqs:*', '*'].includes(action),
      ) &&
      statementResourceTexts(statement).some(
        (text) => text.includes(`"${queueId}"`) || text === '"*"',
      );
    const holders = new Set<string>();
    for (const policy of [
      ...resources(template, 'AWS::IAM::Policy'),
      ...resources(template, 'AWS::IAM::ManagedPolicy'),
    ]) {
      const granted = (((policy.PolicyDocument as Props).Statement ?? []) as Props[]).some(
        changesVisibility,
      );
      if (granted) {
        for (const role of (policy.Roles ?? []) as Props[]) holders.add(JSON.stringify(role));
      }
    }
    expect([...holders]).toEqual([JSON.stringify({ Ref: consumer })]);
    // Control: the consumer's grant names the delivery queue itself.
    const own = roleStatements(template, fn).filter(changesVisibility);
    expect(own.length).toBeGreaterThanOrEqual(1);
    expect(
      own.every((statement) =>
        statementResourceTexts(statement).some((text) => text.includes(`"${queueId}"`)),
      ),
    ).toBe(true);
  });

  for (const enabled of [true, false]) {
    it(`step 6: legacyPlanIntentReconcileEnabled=${enabled} renders exactly the one rule and its writes`, () => {
      const base = synth(PROVISIONED);
      const template = synth({
        ...PROVISIONED,
        ingestion: { ...PROVISIONED.ingestion, legacyPlanIntentReconcileEnabled: enabled },
      });
      const rules = resources(template, 'AWS::Events::Rule');
      const named = (suffix: string): Props =>
        rules.find((rule) => String(rule.Name).endsWith(suffix))!;
      const recovery = PROVISIONED.ingestion.stage1RecoveryEnabled;
      expect(named('-PlanIntentReconcilerSchedule').State).toBe(
        enabled && recovery ? 'ENABLED' : 'DISABLED',
      );
      // Kept sources (design.md, "Legacy cutover", step 6) read the same as with the default.
      const baseRules = resources(base, 'AWS::Events::Rule');
      for (const rule of rules) {
        if (String(rule.Name).endsWith('-PlanIntentReconcilerSchedule')) continue;
        const before = baseRules.find((other) => other.Name === rule.Name)!;
        expect(rule.State).toBe(before.State);
      }
      const mappings = resources(template, 'AWS::Lambda::EventSourceMapping');
      const baseMappings = resources(base, 'AWS::Lambda::EventSourceMapping');
      expect(mappings.map((mapping) => mapping.Enabled)).toEqual(
        baseMappings.map((mapping) => mapping.Enabled),
      );
      const reconciler = soleFunction(template, 'adapters.jobs.plan_intent_reconciler.handler');
      const writes = roleStatements(template, reconciler).filter((statement) =>
        actionsOf(statement).some((action) =>
          ['dynamodb:PutItem', 'dynamodb:UpdateItem', 'dynamodb:DeleteItem'].includes(action),
        ),
      );
      if (enabled) {
        expect(writes.length).toBeGreaterThan(0);
      } else {
        expect(writes).toEqual([]);
      }
      // TableWriter keeps its coordinator reads (LEGACY_REPLAY_ACKNOWLEDGED) either way.
      const writer = soleFunction(template, 'adapters.inbound.ingestion_handlers.table_writer.handler');
      const baseWriter = soleFunction(base, 'adapters.inbound.ingestion_handlers.table_writer.handler');
      expect(roleStatements(template, writer)).toEqual(roleStatements(base, baseWriter));
    });
  }
});

// --- SEMANTIC-M1 (plan 2.4): per-principal workgroup and results-prefix authority -------------

describe('SEMANTIC-M1 workgroup isolation for the ingestion principals', () => {
  const RESULTS = dataBucketName(TEST_CONFIG, 'v2-results');
  const BAKE_WRITE = ['s3:AbortMultipartUpload', 's3:GetObject', 's3:PutObject'];
  const prefixed = (actions: readonly string[], prefix: string): string[] =>
    actions.map((action) => `${action} ${prefix}`);
  /** The plan 2.4 table, exactly: results-object grants and ListBucket prefixes per principal. */
  const EXPECTED: Record<string, { objects: string[]; list: string[] }> = {
    load: {
      objects: [...prefixed(BAKE_WRITE, 'bake/*'), 's3:GetObject batch/*'].sort(),
      list: ['bake/*', 'batch/*'],
    },
    tableWriter: {
      objects: [
        ...prefixed([...BAKE_WRITE, 's3:ListMultipartUploadParts'], 'bake/*'),
        's3:GetObject batch/*',
      ].sort(),
      list: ['bake/*', 'batch/*'],
    },
    operator: {
      objects: [
        ...prefixed([...BAKE_WRITE, 's3:ListMultipartUploadParts'], 'bake/*'),
        's3:GetObject batch/*',
      ].sort(),
      list: ['bake/*', 'batch/*'],
    },
    reconciler: {
      objects: [
        ...prefixed([...BAKE_WRITE, 's3:ListMultipartUploadParts'], 'bake/*'),
        ...prefixed([...BAKE_WRITE, 's3:ListMultipartUploadParts'], 'batch/*'),
      ].sort(),
      list: ['bake/*', 'batch/*'],
    },
  };

  function roleOfHandler(template: Props, handler: string): string {
    const fns = Object.values(template.Resources as Record<string, Props>).filter(
      (resource) => resource.Type === 'AWS::Lambda::Function' && resource.Properties.Handler === handler,
    );
    const getAtt = (only(fns, `function for ${handler}`).Properties.Role as Props)['Fn::GetAtt'];
    return getAtt[0] as string;
  }

  function principals(template: Props): Record<string, string> {
    const operator = Object.entries(template.Resources as Record<string, Props>).filter(
      ([logicalId, resource]) =>
        resource.Type === 'AWS::IAM::Role' && logicalId.includes('SemanticProjectionOperatorRole'),
    );
    return {
      load: roleOfHandler(template, INGESTION_HANDLER),
      tableWriter: roleOfHandler(template, 'adapters.inbound.ingestion_handlers.table_writer.handler'),
      reconciler: roleOfHandler(template, 'adapters.jobs.provider_attempt_reconciler.handler'),
      operator: only(operator, 'SemanticProjectionOperatorRole')[0],
    };
  }

  const startsOn = (template: Props, logicalId: string, kind: 'bake' | 'batch'): boolean =>
    [...grantsOfRoleLogicalId(template, logicalId)].some(
      (grant) =>
        grant.startsWith('athena:StartQueryExecution ') &&
        grant.endsWith(`:workgroup/${semanticV2AthenaWorkgroupName(TEST_CONFIG.envName, kind)}`),
    );

  it('gives each principal exactly its plan 2.4 results-prefix set, with no DeleteObject', () => {
    const template = synth().toJSON() as Props;
    const roles = principals(template);
    for (const [name, logicalId] of Object.entries(roles)) {
      const expected = EXPECTED[name];
      if (expected === undefined) throw new Error(`no expectation for ${name}`);
      expect([name, resultPrefixGrants(template, logicalId, RESULTS)]).toEqual([name, expected.objects]);
      expect([name, listBucketPrefixes(template, logicalId, RESULTS)]).toEqual([name, expected.list]);
    }
    for (const resource of Object.keys(template.Resources as Props)) {
      if ((template.Resources as Props)[resource].Type !== 'AWS::IAM::Role') continue;
      expect(
        resultPrefixGrants(template, resource, RESULTS).filter((grant) => grant.startsWith('s3:DeleteObject')),
      ).toEqual([]);
    }
  });

  it('control: the same set equality fails on a DeleteObject grant or a dropped multipart read', () => {
    const template = synth().toJSON() as Props;
    const roles = principals(template);
    const mutate = (role: string, edit: (statement: Props) => void): Props => {
      const copy = JSON.parse(JSON.stringify(template)) as Props;
      for (const resource of Object.values(copy.Resources as Record<string, Props>)) {
        if (resource.Type !== 'AWS::IAM::Policy' && resource.Type !== 'AWS::IAM::ManagedPolicy') continue;
        if (!((resource.Properties.Roles ?? []) as Props[]).some((ref) => ref.Ref === role)) continue;
        for (const statement of resource.Properties.PolicyDocument.Statement as Props[]) edit(statement);
      }
      return copy;
    };
    const writer = roles.tableWriter;
    const reconciler = roles.reconciler;
    if (writer === undefined || reconciler === undefined) throw new Error('principals missing');
    const withDelete = mutate(writer, (statement) => {
      if (JSON.stringify(statement.Resource).includes(`${RESULTS}/bake/*`)) {
        statement.Action = [...[statement.Action].flat(), 's3:DeleteObject'];
      }
    });
    expect(resultPrefixGrants(withDelete, writer, RESULTS)).not.toEqual(EXPECTED.tableWriter?.objects);
    expect(resultPrefixGrants(withDelete, writer, RESULTS)).toContain('s3:DeleteObject bake/*');
    const withoutParts = mutate(reconciler, (statement) => {
      statement.Action = [statement.Action]
        .flat()
        .filter((action: string) => action !== 's3:ListMultipartUploadParts');
    });
    expect(resultPrefixGrants(withoutParts, reconciler, RESULTS)).not.toEqual(EXPECTED.reconciler?.objects);
  });

  it('lets only the reconciler start on batch, in its ReplayPersistedBatchEnvelopes Sid group', () => {
    const template = synth().toJSON() as Props;
    const batchStarters = Object.entries(template.Resources as Record<string, Props>)
      .filter(([, resource]) => resource.Type === 'AWS::IAM::Role')
      .map(([logicalId]) => logicalId)
      .filter((logicalId) => startsOn(template, logicalId, 'batch'));
    const roles = principals(template);
    expect(batchStarters).toEqual([roles.reconciler]);
    const replay = Object.values(template.Resources as Record<string, Props>)
      .filter((resource) => resource.Type === 'AWS::IAM::Policy')
      .flatMap((resource) => resource.Properties.PolicyDocument.Statement as Props[])
      .filter((statement) => String(statement.Sid ?? '').startsWith('ReplayPersistedBatchEnvelopes'));
    expect(replay.map((statement) => statement.Sid).sort()).toEqual([
      'ReplayPersistedBatchEnvelopes',
      'ReplayPersistedBatchEnvelopesResults',
    ]);
    const start = replay.find((statement) => statement.Sid === 'ReplayPersistedBatchEnvelopes');
    expect(start?.Action).toBe('athena:StartQueryExecution');
    // Paired control: the helper sees TableWriter's start on bake and none on batch.
    expect(startsOn(template, roles.tableWriter ?? '', 'bake')).toBe(true);
    expect(startsOn(template, roles.tableWriter ?? '', 'batch')).toBe(false);
    for (const name of ['load', 'tableWriter', 'reconciler', 'operator']) {
      expect([name, startsOn(template, roles[name] ?? '', 'bake')]).toEqual([name, true]);
    }
  });
});
