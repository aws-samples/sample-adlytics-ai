// SPDX-License-Identifier: MIT-0
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import * as path from 'node:path';

import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { OPERATOR_ONLY_DENIED_ACTIONS, PROTOCOL_VERSION, ROUTES } from '../lib/generated/execution-graph-protocol';
import { dataBucketName } from '../lib/mixins/storage';
import {
  PublisherStack,
  publisherDeclaredInputGeneration,
  publisherDeploymentGeneration,
} from '../lib/stacks/publisher-stack';
import { paramPath, semanticV2AthenaWorkgroupName, semanticV2DatabaseName } from '../lib/config';
import { BAKE_ALARMS, BAKE_ALARM_PERIOD_SECONDS } from '../lib/stacks/execution-graph-alarms';
import { everyTemplate, resourcesOfType } from './support/templates';
import { TEST_CONFIG, configWith } from './support/config';
import { grantsOfRoleLogicalId, listBucketPrefixes, resultPrefixGrants } from './support/iam';
import {
  alarmsOnQueue,
  expectArrivalAlarm,
  expectBoundedStreamConsumer,
  staffedActionIn,
} from './support/async-failure';

type Props = Record<string, unknown>;
const store = everyTemplate().PublisherArtifactStoreStack as Record<string, unknown>;
const publisher = everyTemplate().PublisherStack as Record<string, unknown>;
const PUBLISHER_INPUT = path.join(__dirname, '..', '..', 'presentation', 'pages', 'v2', 'pages.yaml');

function only<T>(items: readonly T[], label: string): T {
  if (items.length !== 1 || items[0] === undefined) {
    throw new Error(`expected one ${label}, found ${items.length}`);
  }
  return items[0];
}

function lambda(handler: string): Props {
  return only(
    resourcesOfType(publisher, 'AWS::Lambda::Function').filter((item) => item.Handler === handler),
    handler,
  );
}

function actions(value: unknown): string[] {
  if (!Array.isArray(value)) return typeof value === 'string' ? [value] : [];
  return value.filter((item): item is string => typeof item === 'string');
}

/** Every policy statement attached to the baker's execution role, and only that role. */
function bakerStatements(): Props[] {
  const baker = lambda('adapters.jobs.preset_artifact_publisher.baker.handler');
  const role = (baker.Role as { 'Fn::GetAtt'?: unknown[] } | undefined)?.['Fn::GetAtt']?.[0];
  if (typeof role !== 'string') throw new Error('baker has no role logical id');
  return resourcesOfType(publisher, 'AWS::IAM::Policy')
    .filter((policy) =>
      ((policy.Roles ?? []) as Props[]).some((candidate) => candidate.Ref === role),
    )
    .flatMap((policy) => (((policy.PolicyDocument as Props).Statement ?? []) as Props[]));
}

describe('retained preset artifact store', () => {
  it('retains encrypted versioned bodies and an on-demand PITR pointer table', () => {
    const bucket = only(resourcesOfType(store, 'AWS::S3::Bucket'), 'artifact bucket');
    expect(bucket.BucketName).toBe(dataBucketName(TEST_CONFIG, 'preset-artifacts'));
    expect(bucket.VersioningConfiguration).toEqual({ Status: 'Enabled' });
    expect(JSON.stringify(bucket)).toContain('AES256');
    expect(JSON.stringify(resourcesOfType(store, 'AWS::S3::BucketPolicy'))).toContain(
      'aws:SecureTransport',
    );

    const table = only(resourcesOfType(store, 'AWS::DynamoDB::Table'), 'pointer table');
    expect(table).toEqual(
      expect.objectContaining({
        TableName: 'adlytics-dev-preset-artifact-pointers',
        BillingMode: 'PAY_PER_REQUEST',
        PointInTimeRecoverySpecification: { PointInTimeRecoveryEnabled: true },
        TimeToLiveSpecification: { AttributeName: 'expires_at', Enabled: true },
      }),
    );
    expect(table.KeySchema).toEqual([
      { AttributeName: 'page_variant_key', KeyType: 'HASH' },
      { AttributeName: 'record_key', KeyType: 'RANGE' },
    ]);
    expect(JSON.stringify(table)).toContain('SSEEnabled');
  });

  it('publishes only the retained store seams through typed SSM paths', () => {
    const names = resourcesOfType(store, 'AWS::SSM::Parameter').map((item) => item.Name);
    expect(names).toEqual(
      expect.arrayContaining([
        paramPath('dev', 'publisherArtifactBucketName'),
        paramPath('dev', 'publisherArtifactPointerTableName'),
        paramPath('dev', 'publisherArtifactPointerTableArn'),
      ]),
    );
  });
});

describe('disposable preset artifact publisher', () => {
  it('uses a FIFO bake queue and FIFO DLQ with one-message partial-batch bake execution', () => {
    // The bake FIFO and its DLQ; the third is the enqueue mapping's standard failure queue.
    const queues = resourcesOfType(publisher, 'AWS::SQS::Queue').filter(
      (item) => item.QueueName !== 'adlytics-dev-publisher-enqueue-stream-failures',
    );
    expect(resourcesOfType(publisher, 'AWS::SQS::Queue')).toHaveLength(3);
    expect(queues).toHaveLength(2);
    for (const queue of queues) {
      expect(queue.FifoQueue).toBe(true);
      expect(queue.ContentBasedDeduplication).toBe(true);
      expect(JSON.stringify(queue)).toContain('SqsManagedSseEnabled');
    }
    const queue = only(queues.filter((item) => item.QueueName === 'adlytics-dev-preset-bake.fifo'), 'bake queue');
    // The generated `routes.bake` is the one source, and revision 2 kept its value (design §2.1).
    expect(ROUTES.bake.max_receive_count).toBe(3);
    expect((queue.RedrivePolicy as Props).maxReceiveCount).toBe(ROUTES.bake.max_receive_count);

    const mapping = only(
      resourcesOfType(publisher, 'AWS::Lambda::EventSourceMapping').filter((item) =>
        JSON.stringify(item.EventSourceArn).includes('BakeQueue'),
      ),
      'bake queue mapping',
    );
    expect(mapping).toEqual(
      expect.objectContaining({
        BatchSize: 1,
        FunctionResponseTypes: ['ReportBatchItemFailures'],
        // D11: two lanes, the Baker's reserved concurrency (the S1 `baker` row).
        ScalingConfig: { MaximumConcurrency: 2 },
      }),
    );
  });

  it('binds the staged handlers to a stream enqueue mapping and narrowly scoped artifact policies', () => {
    const enqueue = lambda('adapters.jobs.preset_artifact_publisher.enqueue.handler');
    const baker = lambda('adapters.jobs.preset_artifact_publisher.baker.handler');
    // S6: Enqueue owns its eight; S1: the Baker is the 2 x Q row of the start budget.
    expect(enqueue.ReservedConcurrentExecutions).toBe(8);
    expect(baker.ReservedConcurrentExecutions).toBe(2);
    const streamMapping = only(
      resourcesOfType(publisher, 'AWS::Lambda::EventSourceMapping').filter(
        (item) => item.StartingPosition === 'LATEST',
      ),
      'publication stream mapping',
    );
    expect(streamMapping).toEqual(
      expect.objectContaining({
        StartingPosition: 'LATEST',
        BatchSize: 10,
        FunctionResponseTypes: ['ReportBatchItemFailures'],
      }),
    );

    const policyActions = resourcesOfType(publisher, 'AWS::IAM::Policy').flatMap((policy) => {
      const document = policy.PolicyDocument as Props;
      return ((document.Statement as Props[] | undefined) ?? []).flatMap((statement) =>
        actions(statement.Action),
      );
    });
    expect(policyActions).toEqual(
      expect.arrayContaining([
        'sqs:SendMessage',
        'dynamodb:TransactGetItems',
        'dynamodb:TransactWriteItems',
        'dynamodb:PutItem',
        'dynamodb:UpdateItem',
        's3:GetObject',
        's3:PutObject',
      ]),
    );
    expect(policyActions).not.toContain('s3:DeleteObject');
    expect(policyActions).not.toContain('dynamodb:DeleteItem');
  });

  it('wires the baker for retained measurement exactly as the precompute worker resolves it', () => {
    const baker = lambda('adapters.jobs.preset_artifact_publisher.baker.handler');
    expect(baker.MemorySize).toBe(1024);
    const variables = (baker.Environment as Props).Variables as Props;
    expect(variables.ADL_SEMANTIC_V2_ATHENA_BAKE_WORKGROUP).toBe(
      semanticV2AthenaWorkgroupName(TEST_CONFIG.envName, 'bake'),
    );
    expect(variables.ADL_SEMANTIC_V2_ATHENA_BATCH_WORKGROUP).toBeUndefined();
    expect(variables.ADL_ATHENA_RESULT_REUSE_MAX_AGE_MINUTES).toBe(
      String(TEST_CONFIG.athenaResultReuseMaxAgeMinutes),
    );
    // Q = 10 from the start budget, not the CHAT fan-out width.
    expect(variables.ADL_ATHENA_QUERY_BATCH_MAX_CONCURRENCY).toBe('10');
    expect(variables.ADL_ATHENA_QUERY_BATCH_MAX_CONCURRENCY).not.toBe(
      String(TEST_CONFIG.athenaQueryBatchMaxConcurrency),
    );
    expect(JSON.stringify(variables.ADL_SEMANTIC_PUBLICATIONS_TABLE)).toContain(
      'SsmParameterValueadlyticsdevdatav2semanticpublicationstable',
    );
    expect(JSON.stringify(variables.ADL_SEMANTIC_V2_BUCKET)).toContain(
      'SsmParameterValueadlyticsdevdatav2bucket',
    );
    expect(JSON.stringify(variables.ADL_SEMANTIC_V2_CURRENT_DATABASE)).toContain(
      'SsmParameterValueadlyticsdevdatav2canonicalcurrentgluedatabase',
    );

    const statements = bakerStatements();
    const granted = statements.flatMap((statement) => actions(statement.Action));
    expect(granted).toEqual(
      expect.arrayContaining([
        'dynamodb:GetItem',
        'dynamodb:TransactGetItems',
        'dynamodb:Query',
        'athena:StartQueryExecution',
        'athena:GetQueryExecution',
        'athena:StopQueryExecution',
        'glue:GetDatabase',
        'glue:GetTable',
        'glue:GetTables',
        'glue:GetPartitions',
        's3:GetObject',
        's3:PutObject',
        's3:AbortMultipartUpload',
        's3:ListBucket',
        's3:GetBucketLocation',
      ]),
    );
    // ADR-143 (A1): the baker streams its result objects and never pages GetQueryResults.
    expect(granted).not.toContain('athena:GetQueryResults');
    const rendered = JSON.stringify(statements);
    expect(rendered).toContain('/index/scope-current');
    expect(rendered).toContain(
      `workgroup/${semanticV2AthenaWorkgroupName(TEST_CONFIG.envName, 'bake')}`,
    );
    expect(rendered).not.toContain(semanticV2AthenaWorkgroupName(TEST_CONFIG.envName, 'batch'));
    expect(rendered).toContain(
      `database/${semanticV2DatabaseName(TEST_CONFIG.envName, 'current')}`,
    );
    expect(rendered).toContain('/bake/*');
    expect(rendered).not.toContain('batch/*');
    expect(rendered).toContain('/pages/*');
    expect(rendered).toContain('/custody/*');
    expect(rendered).toContain('canonical/current/*');
    expect(rendered).toContain('immutable/canonical/successful/*');
    expect(granted).not.toContain('s3:DeleteObject');
    expect(granted).not.toContain('dynamodb:DeleteItem');
    expect(granted).not.toContain('dynamodb:Scan');
  });

  it('gives the baker exactly its plan 2.4 results-prefix set on BAKE and nothing on batch', () => {
    const baker = lambda('adapters.jobs.preset_artifact_publisher.baker.handler');
    const role = (baker.Role as { 'Fn::GetAtt': string[] })['Fn::GetAtt'][0] as string;
    const results = paramPath(TEST_CONFIG.envName, 'semanticV2AthenaResultsBucketName');
    const expected = ['s3:AbortMultipartUpload bake/*', 's3:GetObject bake/*', 's3:PutObject bake/*'];
    expect(resultPrefixGrants(publisher, role, results)).toEqual(expected);
    expect(listBucketPrefixes(publisher, role, results)).toEqual(['bake/*']);
    const athena = [...grantsOfRoleLogicalId(publisher, role)].filter((grant) => grant.startsWith('athena:'));
    expect(athena.every((grant) => grant.endsWith(`:workgroup/${semanticV2AthenaWorkgroupName(TEST_CONFIG.envName, 'bake')}`))).toBe(true);
    expect(athena).toHaveLength(3);
    // Paired control: the same equality fails once DeleteObject joins the bake prefix.
    const mutated = JSON.parse(JSON.stringify(publisher)) as Record<string, unknown>;
    for (const policy of resourcesOfType(mutated, 'AWS::IAM::Policy')) {
      for (const statement of ((policy.PolicyDocument as Props).Statement ?? []) as Props[]) {
        if (JSON.stringify(statement.Resource).includes('/bake/*')) {
          statement.Action = [...actions(statement.Action), 's3:DeleteObject'];
        }
      }
    }
    expect(resultPrefixGrants(mutated, role, results)).not.toEqual(expected);
  });

  it('wires the optional insight-fill child to the continuation queue and prompt registry', () => {
    const baker = lambda('adapters.jobs.preset_artifact_publisher.baker.handler');
    const variables = (baker.Environment as Props).Variables as Props;
    expect(JSON.stringify(variables.ADL_INSIGHT_CONTINUATION_QUEUE_URL)).toContain(
      'SsmParameterValueadlyticsdevinsightcontinuationqueueurl',
    );
    expect(JSON.stringify(variables.ADL_INSIGHT_PROMPT_REGISTRY_TABLE)).toContain(
      'SsmParameterValueadlyticsdevinsightpromptregistrytable',
    );
    const statements = bakerStatements();
    const granted = statements.flatMap((statement) => actions(statement.Action));
    expect(granted).toContain('sqs:SendMessage');
    const rendered = JSON.stringify(statements);
    expect(rendered).toContain('insightcontinuationqueuearn');
  });

  it('uses a deterministic packaged-input generation for deploy backfill', () => {
    const generation = publisherDeploymentGeneration(PUBLISHER_INPUT);
    expect(generation).toMatch(/^[a-f0-9]{64}$/);
    for (const handler of [
      'adapters.jobs.preset_artifact_publisher.enqueue.handler',
      'adapters.jobs.preset_artifact_publisher.baker.handler',
    ]) {
      const environment = lambda(handler).Environment as Props;
      expect((environment.Variables as Props).ADL_PUBLISHER_DEPLOYMENT_GENERATION).toBe(generation);
    }
    const declared = publisherDeclaredInputGeneration(PUBLISHER_INPUT);
    const customResources = Object.values((publisher.Resources as Record<string, Props>) ?? {}).filter(
      (resource) => {
        const properties = resource.Properties;
        return typeof properties === 'object' && properties !== null
          && (properties as Props).generation === declared;
      },
    );
    expect(customResources).toHaveLength(1);
    expect(JSON.stringify(customResources[0])).toContain(declared);
  });

  // R5-1: the custom resource's only property is the declared-input generation, so a code-only
  // redeploy leaves it byte-identical and CloudFormation sends DeployBackfill no event.
  it('binds DeployBackfill to the declared input, not to the packaged runtime digest', () => {
    const directory = mkdtempSync(path.join(tmpdir(), 'publisher-input-'));
    try {
      const declarations = readFileSync(PUBLISHER_INPUT, 'utf8');
      const staged = (runtime: string): string => {
        const file = path.join(directory, `${runtime}.yaml`);
        writeFileSync(file, `${declarations}\n# publisher-runtime-generation: ${runtime}\n`);
        return file;
      };
      const before = staged('a'.repeat(64));
      const codeOnly = staged('b'.repeat(64));
      expect(publisherDeploymentGeneration(before)).not.toBe(publisherDeploymentGeneration(codeOnly));
      expect(publisherDeclaredInputGeneration(before)).toBe(publisherDeclaredInputGeneration(codeOnly));
      expect(publisherDeclaredInputGeneration(before)).toBe(
        publisherDeclaredInputGeneration(PUBLISHER_INPUT),
      );

      const changed = path.join(directory, 'changed.yaml');
      writeFileSync(changed, `${declarations}# a changed declaration\n`);
      expect(publisherDeclaredInputGeneration(changed)).not.toBe(
        publisherDeclaredInputGeneration(PUBLISHER_INPUT),
      );
    } finally {
      rmSync(directory, { recursive: true, force: true });
    }

    const backfills = Object.values((publisher.Resources as Record<string, Props>) ?? {}).filter(
      (resource) => resource.Type === 'AWS::CloudFormation::CustomResource',
    );
    expect(backfills).toHaveLength(1);
    const properties = only(backfills, 'DeployBackfill').Properties as Props;
    expect(Object.keys(properties).sort()).toEqual(['ServiceToken', 'generation']);
    expect(properties.generation).toBe(publisherDeclaredInputGeneration(PUBLISHER_INPUT));
  });
});

// --- S6 slice 5i (§2.8): the 900 s bake and the graph table ------------------------------------

describe('graph bake timeout and mode table', () => {
  it('runs the baker for 900 s behind a 960 s bake queue visibility', () => {
    const baker = lambda('adapters.jobs.preset_artifact_publisher.baker.handler');
    expect(baker.Timeout).toBe(900);
    const queue = only(
      resourcesOfType(publisher, 'AWS::SQS::Queue').filter(
        (item) => item.QueueName === 'adlytics-dev-preset-bake.fifo',
      ),
      'bake queue',
    );
    expect(queue.VisibilityTimeout).toBe(960);
  });

  it('keeps the Baker at its start-budget cap whatever the general query-batch admission is (D7)', () => {
    const app = new cdk.App();
    const template = Template.fromStack(
      new PublisherStack(app, 'PublisherNonEightAdmission', {
        config: configWith({ athenaQueryBatchMaxConcurrency: 3 }),
        env: { account: TEST_CONFIG.account, region: TEST_CONFIG.region },
        sourceAssetPath: __dirname,
        publisherInputPath: PUBLISHER_INPUT,
      }),
    ).toJSON() as Record<string, unknown>;
    const baker = only(
      resourcesOfType(template, 'AWS::Lambda::Function').filter(
        (item) => item.Handler === 'adapters.jobs.preset_artifact_publisher.baker.handler',
      ),
      'baker',
    );
    const variables = (baker.Environment as Props).Variables as Props;
    expect(variables.ADL_ATHENA_QUERY_BATCH_MAX_CONCURRENCY).toBe('10');
  });

  it('control: the provisioned publisher workers each receive the graph table they read the mode from', () => {
    const app = new cdk.App();
    const template = Template.fromStack(
      new PublisherStack(app, 'PublisherProvisioned', {
        config: configWith({
          operationalStatus: {
            provisioned: true,
            stage1LifecycleReconcileBatchSize: 10,
            stage1OutboxMaxConsecutiveFailedDeliveryAttempts: 3,
          },
        }),
        env: { account: TEST_CONFIG.account, region: TEST_CONFIG.region },
        sourceAssetPath: __dirname,
        publisherInputPath: PUBLISHER_INPUT,
      }),
    ).toJSON() as Record<string, unknown>;
    for (const handler of [
      'adapters.jobs.preset_artifact_publisher.enqueue.handler',
      'adapters.jobs.preset_artifact_publisher.baker.handler',
      'adapters.jobs.preset_artifact_publisher.backfill.handler',
    ]) {
      const fn = only(
        resourcesOfType(template, 'AWS::Lambda::Function').filter((item) => item.Handler === handler),
        handler,
      );
      const variables = ((fn.Environment as Props).Variables ?? {}) as Props;
      expect(variables.ADL_EXECUTION_GRAPH_TABLE).toBe('adlytics-dev-execution-graph');
    }
    // Unprovisioned (the shared fixture): no graph table is wired.
    expect(JSON.stringify(lambda('adapters.jobs.preset_artifact_publisher.baker.handler'))).not.toContain(
      'ADL_EXECUTION_GRAPH_TABLE',
    );
  });
});

// --- S6 part 2 (§2.11 rows 5, 5b and 6): the publisher's graph grants and the bake DLQ alarm ----

const GRAPH_TABLE_SUFFIX = 'table/adlytics-dev-execution-graph"';
const MODE_ONLY = { 'ForAllValues:StringEquals': { 'dynamodb:LeadingKeys': ['MODE#ingestion'] } };
const TRANSACT = 'TransactWriteItems';
const BAKE_FAMILIES = ['COHORT#*', 'DUE#*', 'EXEC_CLAIM#*', 'LOCK#*', 'RUN#*', 'SERVING#*', 'TOKEN#*'];
type GraphShape = [sid: string, actions: string[], leadingKeys: string[], enclosing: string | null];
/** Plan 4.6 F1 under A3: the exact `BakeCoordinator` expansion plus the mode check, by Sid. */
const BAKE_COORDINATOR_SHAPE: GraphShape[] = [
  ['ConditionBakeServingAuthority', ['dynamodb:ConditionCheckItem'], ['SERVING_DOMAIN#*'], TRANSACT],
  ['DenyOperatorOnlyGraphWrites', [...OPERATOR_ONLY_DENIED_ACTIONS].toSorted(), ['COMMAND#*', 'MODE#*'], null],
  ['ReadAndConditionCheckGraphModeItem', ['dynamodb:ConditionCheckItem', 'dynamodb:GetItem'], ['MODE#ingestion'], null],
  ['ReadGraphFamilies', ['dynamodb:GetItem', 'dynamodb:Query'], BAKE_FAMILIES, null],
  ['ReadServingGraphFamilies', ['dynamodb:GetItem'],
    ['SERVING_DOMAIN#*', 'SERVING_FRONTIER#*', 'SERVING_PUBLICATION#*'], null],
  ['TransactGraphFamilies',
    ['dynamodb:ConditionCheckItem', 'dynamodb:DeleteItem', 'dynamodb:PutItem', 'dynamodb:UpdateItem'],
    BAKE_FAMILIES, TRANSACT],
  ['UpdateBakeFinalizationAuthority', ['dynamodb:UpdateItem'],
    ['SERVING_DOMAIN#*', 'SERVING_PUBLICATION#*'], TRANSACT],
  ['UpdateGraphClaims', ['dynamodb:UpdateItem'], ['EXEC_CLAIM#*'], null],
];

function leadingKeys(statement: Props): string[] {
  const conditions = Object.values((statement.Condition ?? {}) as Record<string, Props>);
  return conditions.flatMap((operator) => actions(operator['dynamodb:LeadingKeys'])).toSorted();
}

/** One statement set as exact (Sid, actions, LeadingKeys, EnclosingOperation) rows, by Sid. */
function graphShape(statements: Props[]): GraphShape[] {
  return statements
    .map((statement): GraphShape => {
      const equals = ((statement.Condition ?? {}) as Record<string, Props>).StringEquals ?? {};
      const enclosing = equals['dynamodb:EnclosingOperation'];
      return [
        String(statement.Sid),
        actions(statement.Action).toSorted(),
        leadingKeys(statement),
        typeof enclosing === 'string' ? enclosing : null,
      ];
    })
    .toSorted((left, right) => left[0].localeCompare(right[0]));
}

function withLeadingKeys(statement: Props, change: (keys: string[]) => string[]): Props {
  const condition = structuredClone((statement.Condition ?? {}) as Record<string, Props>);
  for (const operator of Object.values(condition)) {
    if ('dynamodb:LeadingKeys' in operator) {
      operator['dynamodb:LeadingKeys'] = change(actions(operator['dynamodb:LeadingKeys']));
    }
  }
  return { ...statement, Condition: condition };
}

function provisionedPublisher(): Record<string, unknown> {
  const app = new cdk.App();
  return Template.fromStack(
    new PublisherStack(app, 'PublisherGraphRows', {
      config: configWith({
        operationalStatus: {
          provisioned: true,
          stage1LifecycleReconcileBatchSize: 10,
          stage1OutboxMaxConsecutiveFailedDeliveryAttempts: 3,
        },
      }),
      env: { account: TEST_CONFIG.account, region: TEST_CONFIG.region },
      sourceAssetPath: __dirname,
      publisherInputPath: PUBLISHER_INPUT,
    }),
  ).toJSON() as Record<string, unknown>;
}

/** Every statement on one function's role, its default and overflow policies alike. */
function roleStatementsOf(template: Record<string, unknown>, handler: string): Props[] {
  const fn = only(
    resourcesOfType(template, 'AWS::Lambda::Function').filter((item) => item.Handler === handler),
    handler,
  );
  const role = (fn.Role as { 'Fn::GetAtt'?: unknown[] })['Fn::GetAtt']?.[0];
  return [
    ...resourcesOfType(template, 'AWS::IAM::Policy'),
    ...resourcesOfType(template, 'AWS::IAM::ManagedPolicy'),
  ]
    .filter((policy) => ((policy.Roles ?? []) as Props[]).some((candidate) => candidate.Ref === role))
    .flatMap((policy) => (((policy.PolicyDocument as Props).Statement ?? []) as Props[]));
}

/** Every graph-table statement on one function's role. */
function graphStatementsOf(template: Record<string, unknown>, handler: string): Props[] {
  return roleStatementsOf(template, handler).filter((statement) =>
    JSON.stringify(statement.Resource).includes(GRAPH_TABLE_SUFFIX),
  );
}

describe('§2.11 graph rows in the publisher', () => {
  const template = provisionedPublisher();

  it('row 6: enqueue and backfill read the mode item and nothing else in the graph table', () => {
    for (const handler of [
      'adapters.jobs.preset_artifact_publisher.enqueue.handler',
      'adapters.jobs.preset_artifact_publisher.backfill.handler',
    ]) {
      expect(graphStatementsOf(template, handler)).toEqual([
        {
          Sid: 'ReadGraphModeItem',
          Effect: 'Allow',
          Action: 'dynamodb:GetItem',
          Resource: graphStatementsOf(template, handler)[0]?.Resource,
          Condition: MODE_ONLY,
        },
      ]);
    }
  });

  it('rows 5 and 6 (plan 4.6 F1, A3): the baker holds exactly the BakeCoordinator family and the mode check', () => {
    const graph = graphStatementsOf(template, 'adapters.jobs.preset_artifact_publisher.baker.handler');
    expect(graphShape(graph)).toEqual(BAKE_COORDINATOR_SHAPE);
    const mode = graph.find((statement) => statement.Sid === 'ReadAndConditionCheckGraphModeItem')!;
    expect(mode.Condition).toEqual(MODE_ONLY);
    // No BLOCK# or LINEAGE#, and (A3) no semantic generation grant on any Baker statement.
    const everything = JSON.stringify(
      roleStatementsOf(template, 'adapters.jobs.preset_artifact_publisher.baker.handler'),
    );
    for (const family of ['BLOCK#', 'LINEAGE#', 'GENERATION#']) expect(everything).not.toContain(family);
    for (const sid of ['FinalizeGenerationPointers', 'FoldFinalizedPublications']) {
      expect(everything).not.toContain(sid);
    }
  });

  it('control (F1): a baker holding PostGrantConsumer, or a generation grant, fails the exact set', () => {
    const graph = graphStatementsOf(template, 'adapters.jobs.preset_artifact_publisher.baker.handler');
    const narrowed = graph
      .filter((statement) => !['ReadServingGraphFamilies', 'ConditionBakeServingAuthority',
        'UpdateBakeFinalizationAuthority'].includes(statement.Sid as string))
      .map((statement) => withLeadingKeys(statement, (keys) => keys.filter((key) => key !== 'DUE#*')));
    expect(graphShape(narrowed)).not.toEqual(BAKE_COORDINATOR_SHAPE);
    const flipping = [
      ...graph,
      {
        Sid: 'FinalizeGenerationPointers',
        Effect: 'Allow',
        Action: 'dynamodb:UpdateItem',
        Resource: graph[0]!.Resource,
        Condition: {
          'ForAllValues:StringLike': { 'dynamodb:LeadingKeys': ['GENERATION#*'] },
          StringEquals: { 'dynamodb:EnclosingOperation': 'TransactWriteItems' },
        },
      },
    ];
    expect(graphShape(flipping)).not.toEqual(BAKE_COORDINATOR_SHAPE);
  });

  it('row 5b: the bake DLQ alarm is an arrival alarm on the staffed destination', () => {
    expectArrivalAlarm(template, {
      alarmName: 'adlytics-dev-preset-bake-dead-lettered',
      queueName: 'adlytics-dev-preset-bake-dlq.fifo',
      staffed: { arm: 'published' },
    });
  });
  it('plan 6.2 (DB26): the bake DLQ arrival alarm is its only alarm, so a visible message pages once', () => {
    const queue = 'adlytics-dev-preset-bake-dlq.fifo';
    expect(alarmsOnQueue(template, queue)).toEqual(['adlytics-dev-preset-bake-dead-lettered']);
    // Control: a duplicate alarm on the same queue is counted.
    const resources = template.Resources as Record<string, { Type: string; Properties: Record<string, unknown> }>;
    const [id, original] = Object.entries(resources).find(
      ([, resource]) => resource.Properties?.AlarmName === 'adlytics-dev-preset-bake-dead-lettered',
    )!;
    const duplicated = {
      ...template,
      Resources: {
        ...resources,
        [`${id}Duplicate`]: { ...original, Properties: { ...original.Properties, AlarmName: 'duplicate' } },
      },
    };
    expect(alarmsOnQueue(duplicated, queue)).toHaveLength(2);
  });

  it('control: the shared unprovisioned fixture has no graph grant', () => {
    for (const handler of [
      'adapters.jobs.preset_artifact_publisher.enqueue.handler',
      'adapters.jobs.preset_artifact_publisher.baker.handler',
      'adapters.jobs.preset_artifact_publisher.backfill.handler',
    ]) {
      expect(graphStatementsOf(publisher, handler)).toEqual([]);
    }
    // The bake DLQ alarm is not graph-gated: ADR-133 alarms every dead letter, asserted below.
  });
});

describe('preset artifact publisher failure paths (ADR-133)', () => {
  it('bounds publications-stream retries and sends an exhausted batch to an alarmed failure queue', () => {
    expectBoundedStreamConsumer(publisher, {
      mappingId: /^PublicationCurrentMapping$/,
      queueName: 'adlytics-dev-publisher-enqueue-stream-failures',
      alarmName: 'adlytics-dev-publisher-enqueue-stream-exhausted',
      alarmDescription:
        'A publications-stream batch exhausted its bounded preset-artifact enqueue retries; '
        + 'its pages were not enqueued for baking. Disposition: attempts_failures status '
        + '--consumer publisher-enqueue, then attempts_failures drain --consumer '
        + 'publisher-enqueue, then run a declarative re-bake (operator-tools.md).',
      roleId: /^EnqueueExecutionRole[0-9A-F]{8}$/,
      mapping: {
        BatchSize: 10,
        EventSourceArn: {
          Ref: expect.stringMatching(/^SsmParameterValueadlyticsdevdatav2semanticpublicationsstreamarn/),
        },
        FunctionName: { Ref: expect.stringMatching(/^Enqueue[0-9A-F]{8}$/) },
        FunctionResponseTypes: ['ReportBatchItemFailures'],
        ParallelizationFactor: 1,
        StartingPosition: 'LATEST',
      },
    });
  });

  it('alarms on each bake dead-letter arrival', () => {
    expectArrivalAlarm(publisher, {
      alarmName: 'adlytics-dev-preset-bake-dead-lettered',
      queueName: 'adlytics-dev-preset-bake-dlq.fifo',
      staffed: { arm: 'published' },
    });
  });
});

describe('ADR-142 bake alarms in the publisher stack (A1)', () => {
  type Resource = { Type: string; Properties: Record<string, unknown> };
  function alarmsNamed(template: Record<string, unknown>, name: string): Resource[] {
    const resources = template.Resources as Record<string, Resource>;
    return Object.values(resources).filter(
      (resource) => resource.Type === 'AWS::CloudWatch::Alarm' && resource.Properties.AlarmName === name,
    );
  }
  function expectBakeAlarms(template: Record<string, unknown>): void {
    for (const spec of BAKE_ALARMS) {
      const { Tags: tags, ...alarm } = only(
        alarmsNamed(template, `adlytics-dev-${spec.suffix}`),
        spec.id,
      ).Properties;
      expect(Array.isArray(tags)).toBe(true); // the standard tags, applied stack-wide
      const staffed = staffedActionIn(template, { arm: 'published' });
      expect(alarm).toEqual({
        AlarmName: `adlytics-dev-${spec.suffix}`,
        AlarmDescription: spec.description,
        Namespace: 'Adlytics',
        MetricName: spec.metricName,
        Dimensions: [
          { Name: 'AuthorityScope', Value: 'live' },
          { Name: 'Protocol', Value: PROTOCOL_VERSION },
        ],
        Statistic: 'Maximum',
        Period: BAKE_ALARM_PERIOD_SECONDS,
        Threshold: 1,
        ComparisonOperator: 'GreaterThanOrEqualToThreshold',
        EvaluationPeriods: 1,
        DatapointsToAlarm: 1,
        TreatMissingData: 'notBreaching',
        AlarmActions: [staffed],
        OKActions: [staffed],
      });
    }
  }

  it('declares exactly the two approved alarms, with the backfill and re-bake texts', () => {
    expect(BAKE_ALARMS.map((spec) => [spec.id, spec.metricName])).toEqual([
      ['BakeIsolatedRefusalLimit', 'BakeIsolatedRefusalLimit'],
      ['BakeTaskOverBudget', 'BakeTaskOverBudget'],
    ]);
    const [limit] = BAKE_ALARMS;
    expect(limit?.description).toContain('declarative re-bake');
    expect(limit?.description).toContain('quarter-manifest backfill');
  });

  it('builds both in a synth without the graph authority (an adopter deployment)', () => {
    expect(graphStatementsOf(publisher, 'adapters.jobs.preset_artifact_publisher.baker.handler')).toEqual([]);
    expectBakeAlarms(publisher);
  });

  it("builds both in dev's synth with the graph authority", () => {
    expectBakeAlarms(provisionedPublisher());
  });

  it('control: the data-class refusal metric is counted, never alarmed', () => {
    const resources = Object.values(publisher.Resources as Record<string, Resource>);
    expect(
      resources.filter(
        (resource) => resource.Type === 'AWS::CloudWatch::Alarm'
          && resource.Properties.MetricName === 'BakeIsolatedRefusalData',
      ),
    ).toEqual([]);
    // And a changed field is caught: the same alarm with another threshold fails the shape.
    const spec = BAKE_ALARMS[0]!;
    const alarm = only(alarmsNamed(publisher, `adlytics-dev-${spec.suffix}`), spec.id).Properties;
    expect({ ...alarm, Threshold: 2 }).not.toEqual(alarm);
  });
});
