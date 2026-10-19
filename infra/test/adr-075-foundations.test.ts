// SPDX-License-Identifier: MIT-0
import * as fs from 'node:fs';
import * as path from 'node:path';

import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';

import {
  PROJECTION_HANDOFF_STATES,
  ProjectionHandoffState,
  defaultConfig,
  paramPath,
  resolveProjectionHandoffState,
} from '../lib/config';
import type { AppConfig } from '../lib/config';
import { IngestionStack } from '../lib/stacks/ingestion-stack';
import { expectArrivalAlarm } from './support/async-failure';
import { TEST_CONFIG, configWith } from './support/config';

type Props = Record<string, any>;

function configFor(state: ProjectionHandoffState, recovery?: boolean): AppConfig {
  return configWith({
    landing: { enabled: false },
    ingestion: {
      ...TEST_CONFIG.ingestion,
      projectionHandoffState: state,
      // Every QUEUED_* state requires the recovery triggers enabled; INLINE_* defaults to the
      // pre-switch-fence deployment unless a test asks for the post-verification redeploy.
      stage1RecoveryEnabled: recovery ?? state.startsWith('QUEUED'),
    },
  });
}

function synth(state: ProjectionHandoffState = 'INLINE_ACTIVE', recovery?: boolean): Template {
  const config = configFor(state, recovery);
  const app = new cdk.App();
  const suffix = recovery === undefined ? '' : recovery ? '-recovery-on' : '-recovery-off';
  return Template.fromStack(new IngestionStack(app, `Ingestion-${state.replace(/_/g, '-')}${suffix}`, {
    config,
    env: { account: config.account, region: config.region },
    sourceAssetPath: __dirname,
  }));
}

function resources(template: Template, type: string): Props[] {
  return Object.values(template.findResources(type)).map(
    (resource) => (resource as Props).Properties as Props,
  );
}

function mappings(template: Template): { delivery: Props; semantic: Props } {
  const all = resources(template, 'AWS::Lambda::EventSourceMapping');
  const delivery = all.find((mapping) => JSON.stringify(mapping.EventSourceArn).includes('DeliveryQueue'));
  const semantic = all.find((mapping) => JSON.stringify(mapping.EventSourceArn).includes('SemanticWriteQueue'));
  if (delivery === undefined || semantic === undefined || all.length !== 2) {
    throw new Error('expected exactly the delivery and semantic-writer mappings');
  }
  return { delivery, semantic };
}

function allowStatements(template: Template): Props[] {
  return resources(template, 'AWS::IAM::Policy')
    .flatMap((policy) => ((policy.PolicyDocument as Props).Statement ?? []) as Props[])
    .filter((statement) => statement.Effect === 'Allow');
}

describe('ADR-075 six-state projection handoff', () => {
  it('resolves every state to the only approved control row', () => {
    expect(PROJECTION_HANDOFF_STATES).toEqual([
      'INLINE_ACTIVE',
      'INLINE_QUIESCED',
      'QUEUED_QUIESCED',
      'QUEUED_ACCUMULATE',
      'QUEUED_ACTIVE',
      'QUEUED_DRAIN',
    ]);
    expect(PROJECTION_HANDOFF_STATES.map((state) => [state, resolveProjectionHandoffState(state)])).toEqual([
      ['INLINE_ACTIVE', { projectionComposition: 'inline', deliveryMappingEnabled: true, semanticWriterMappingEnabled: false }],
      ['INLINE_QUIESCED', { projectionComposition: 'inline', deliveryMappingEnabled: false, semanticWriterMappingEnabled: false }],
      ['QUEUED_QUIESCED', { projectionComposition: 'queued', deliveryMappingEnabled: false, semanticWriterMappingEnabled: false }],
      ['QUEUED_ACCUMULATE', { projectionComposition: 'queued', deliveryMappingEnabled: true, semanticWriterMappingEnabled: false }],
      ['QUEUED_ACTIVE', { projectionComposition: 'queued', deliveryMappingEnabled: true, semanticWriterMappingEnabled: true }],
      ['QUEUED_DRAIN', { projectionComposition: 'queued', deliveryMappingEnabled: false, semanticWriterMappingEnabled: true }],
    ]);
  });

  it.each([
    ['INLINE_ACTIVE', true, false],
    ['INLINE_QUIESCED', false, false],
    ['QUEUED_QUIESCED', false, false],
    ['QUEUED_ACCUMULATE', true, false],
    ['QUEUED_ACTIVE', true, true],
    ['QUEUED_DRAIN', false, true],
  ] as const)('renders %s with delivery=%s and semantic-writer=%s', (state, delivery, semantic) => {
    const rendered = mappings(synth(state));
    expect(rendered.delivery.Enabled).toBe(delivery);
    expect(rendered.semantic.Enabled).toBe(semantic);
  });

  it('prepares the shipped default handoff state, the one a new environment bootstraps to', () => {
    // Fresh-env run 1 (2026-10-19): every first deploy prepares the SHIPPED ingestion config against
    // an ABSENT handoff row. This suite's support config is INLINE_ACTIVE, so nothing tied the shipped
    // value to the state machine. tests/unit/test_projection_handoff.py pins that the same literal is
    // a permitted ABSENT bootstrap; this test pins that it is what the template requests.
    // A customer environment name, not dev: the shipped value must hold for every new environment.
    const shipped = defaultConfig('pubcheck').ingestion;
    const config = configWith({ landing: { enabled: false }, ingestion: shipped });
    const template = Template.fromStack(new IngestionStack(new cdk.App(), 'Ingestion-shipped-default', {
      config,
      env: { account: config.account, region: config.region },
      sourceAssetPath: __dirname,
    }));
    const prepare = resources(template, 'Custom::ProjectionHandoffPrepare');
    expect(prepare).toHaveLength(1);
    expect(shipped.projectionHandoffState).toBe('QUEUED_ACTIVE');
    expect(prepare[0]?.requestedState).toBe('QUEUED_ACTIVE');
    expect(prepare[0]?.projectionComposition).toBe('queued');
    expect(prepare[0]?.deliveryMappingEnabled).toBe(true);
    expect(prepare[0]?.semanticWriterMappingEnabled).toBe(true);
  });

  it('ships the pre-switch state with delivery active and every queued automatic path disabled', () => {
    const template = synth();
    const rendered = mappings(template);
    expect(rendered.delivery.Enabled).toBe(true);
    expect(rendered.semantic.Enabled).toBe(false);
    const schedules = resources(template, 'AWS::Events::Rule').filter(
      (rule) => typeof rule.ScheduleExpression === 'string',
    );
    expect(schedules).toHaveLength(2);
    expect(schedules.every((rule) => rule.State === 'DISABLED')).toBe(true);
  });

  it('rejects missing, unknown, boolean-valued, and independent legacy controls', () => {
    expect(() => resolveProjectionHandoffState(undefined)).toThrow(/must be exactly one of/);
    expect(() => resolveProjectionHandoffState('ACTIVE')).toThrow(/must be exactly one of/);
    const invalid = {
      ...TEST_CONFIG.ingestion,
      projectionHandoffState: 'INLINE_ACTIVE',
      tableWriterConcurrency: true,
    } as unknown as AppConfig['ingestion'];
    expect(() => synthInvalid(configWith({ landing: { enabled: false }, ingestion: invalid }))).toThrow(
      /tableWriterConcurrency must be the reviewed positive integer 8/,
    );
    expect(() => synthInvalid({
      ...configFor('INLINE_ACTIVE'),
      projectionMode: 'queued',
    } as AppConfig)).toThrow(/Legacy independent projectionMode input is forbidden/);
  });
});

describe('ADR-075 queues, functions, and closed handoff resources', () => {
  const template = synth();

  it('creates the exact FIFO semantic-write queue pair and disabled one-message mapping', () => {
    const queues = resources(template, 'AWS::SQS::Queue').filter(
      (queue) => String(queue.QueueName).includes('semantic-write'),
    );
    expect(queues).toHaveLength(2);
    expect(queues).toEqual(expect.arrayContaining([
      expect.objectContaining({
        QueueName: 'adlytics-dev-semantic-write.fifo',
        FifoQueue: true,
        ContentBasedDeduplication: false,
        VisibilityTimeout: 960,
        RedrivePolicy: expect.objectContaining({ maxReceiveCount: 3 }),
      }),
      expect.objectContaining({ QueueName: 'adlytics-dev-semantic-write-dlq.fifo', FifoQueue: true }),
    ]));
    const writer = resources(template, 'AWS::Lambda::Function').find(
      (fn) => fn.Handler === 'adapters.inbound.ingestion_handlers.table_writer.handler',
    );
    expect(writer).toEqual(expect.objectContaining({
      Timeout: 900,
      ReservedConcurrentExecutions: 8,
      Architectures: ['arm64'],
      Runtime: TEST_CONFIG.pythonRuntime,
    }));
    const environment = (writer?.Environment as Props).Variables as Props;
    expect(environment).toEqual(expect.objectContaining({
      ADL_PROJECTION_HANDOFF_STATE: 'INLINE_ACTIVE',
      ADL_STAGE1_MAX_UNITS: '15',
      ADL_TABLE_WRITER_TIMEOUT_SECONDS: '900',
      ADL_TABLE_ATTEMPT_RECONCILE_BATCH_SIZE: '10',
      ADL_PLAN_INTENT_RECONCILE_BATCH_SIZE: '50',
      ADL_PROJECTION_HANDOFF_DRAIN_OBSERVATION_SECONDS: '300',
      ADL_SEMANTIC_V2_SOURCE_DATABASE: expect.any(String),
      ADL_SEMANTIC_V2_CANDIDATE_DATABASE: expect.any(String),
      ADL_SEMANTIC_V2_CURRENT_DATABASE: expect.any(String),
    }));
    expect(mappings(template).semantic).toEqual(expect.objectContaining({
      Enabled: false,
      BatchSize: 1,
      ScalingConfig: { MaximumConcurrency: 8 },
    }));
  });

  it('reports per-item failures on the semantic-writer mapping, so a retryable unit is redelivered', () => {
    // The writer returns a retryable unit's messageId in batchItemFailures. Without this
    // response type Lambda deletes it as a success: on 2026-10-06 a seal's three units
    // classified admission_incomplete were deleted and never retried (O-SEMANTIC-WRITE-ACK-001).
    const reportsPerItem = (mapping: Props): boolean =>
      JSON.stringify(mapping.FunctionResponseTypes) === JSON.stringify(['ReportBatchItemFailures']);
    for (const state of PROJECTION_HANDOFF_STATES) {
      const rendered = mappings(synth(state));
      expect(rendered.semantic.FunctionResponseTypes).toEqual(['ReportBatchItemFailures']);
      expect(reportsPerItem(rendered.semantic)).toBe(true);
      // Positive control: the same mapping without the property is refused by the same rule.
      const { FunctionResponseTypes: _removed, ...withoutIt } = rendered.semantic;
      expect(reportsPerItem(withoutIt)).toBe(false);
      // S6 (V-19): the delivery mapping reports per item too, so a delivery the drain leaves
      // waiting stays unacknowledged without failing the invocation (batch size one).
      expect(rendered.delivery.FunctionResponseTypes).toEqual(['ReportBatchItemFailures']);
    }
  });

  it('creates one provider and the three exact closed custom resources', () => {
    const provider = resources(template, 'AWS::Lambda::Function').filter(
      (fn) => fn.Handler === 'adapters.jobs.projection_handoff.handler',
    );
    expect(provider).toHaveLength(1);
    const prepare = resources(template, 'Custom::ProjectionHandoffPrepare');
    const commit = resources(template, 'Custom::ProjectionHandoffCommit');
    const guard = resources(template, 'Custom::ProjectionHandoffDeletionGuard');
    expect(prepare).toHaveLength(1);
    expect(commit).toHaveLength(1);
    expect(guard).toHaveLength(1);
    expect(Object.keys(prepare[0] ?? {}).sort()).toEqual([
      'ServiceToken',
      'deliveryMappingEnabled',
      'environment',
      'projectionComposition',
      'protocolVersion',
      'requestedState',
      'resourceKind',
      'semanticWriterMappingEnabled',
      'stackIdentity',
    ].sort());
    expect(Object.keys(commit[0] ?? {}).sort()).toEqual([
      ...Object.keys(prepare[0] ?? {}),
      'expectedRevision',
      'fromDisposition',
      'fromState',
      'mode',
      'transitionToken',
    ].sort());
    expect(Object.keys(guard[0] ?? {}).sort()).toEqual([
      'ServiceToken',
      'environment',
      'protocolVersion',
      'resourceKind',
      'stackIdentity',
    ].sort());
    for (const attribute of ['mode', 'transitionToken', 'fromDisposition', 'fromState', 'expectedRevision']) {
      expect(JSON.stringify(commit[0]?.[attribute])).toContain('ProjectionHandoffPrepare');
    }
  });

  it('orders every destroyable application resource between prepare and commit/guard', () => {
    const rendered = template.toJSON() as {
      Resources: Record<string, { Type: string; DependsOn?: string | string[] }>;
    };
    const entries = Object.entries(rendered.Resources);
    const prepareId = entries.find(([, resource]) => resource.Type === 'Custom::ProjectionHandoffPrepare')?.[0];
    const commitEntry = entries.find(([, resource]) => resource.Type === 'Custom::ProjectionHandoffCommit');
    const guardEntry = entries.find(([, resource]) => resource.Type === 'Custom::ProjectionHandoffDeletionGuard');
    if (prepareId === undefined || commitEntry === undefined || guardEntry === undefined) {
      throw new Error('handoff custom resource graph is incomplete');
    }
    const dependencies = (resource: { DependsOn?: string | string[] }): string[] =>
      resource.DependsOn === undefined
        ? []
        : Array.isArray(resource.DependsOn)
          ? resource.DependsOn
          : [resource.DependsOn];
    const applicationIds = entries
      .filter(([logicalId, resource]) =>
        !resource.Type.startsWith('Custom::ProjectionHandoff') &&
        !logicalId.includes('ProjectionHandoffProviderResources'),
      )
      .map(([logicalId, resource]) => {
        expect(dependencies(resource)).toContain(prepareId);
        return logicalId;
      });
    const commitDependencies = dependencies(commitEntry[1]);
    const guardDependencies = dependencies(guardEntry[1]);
    expect(commitDependencies).toEqual(expect.arrayContaining(applicationIds));
    expect(guardDependencies).toEqual(expect.arrayContaining([...applicationIds, commitEntry[0]]));
  });

  it('publishes the three exact Ingestion-owned operator/recovery parameters', () => {
    const names = resources(template, 'AWS::SSM::Parameter').map((parameter) => parameter.Name);
    for (const key of [
      'providerFenceRecoveryOperatorRoleArn',
      'providerFenceRecoveryFunctionArn',
      'semanticProjectionOperatorRoleArn',
    ] as const) {
      expect(names).toContain(paramPath('dev', key));
    }
  });
});

describe('ADR-075 IAM boundaries', () => {
  const template = synth();
  const statements = allowStatements(template);

  it('uses no managed policies, wildcard resources, or transaction API actions', () => {
    for (const role of resources(template, 'AWS::IAM::Role')) {
      expect(role.ManagedPolicyArns).toBeUndefined();
    }
    for (const statement of statements.filter((candidate) => {
      const actions = Array.isArray(candidate.Action) ? candidate.Action : [candidate.Action];
      return actions.some((action) =>
        typeof action === 'string' &&
        (action.startsWith('dynamodb:') || action.startsWith('sqs:') || action.startsWith('lambda:')),
      );
    })) {
      expect(statement.Resource).not.toBe('*');
      expect(Array.isArray(statement.Resource) ? statement.Resource : [statement.Resource]).not.toContain('*');
    }
    const actions = statements.flatMap((statement) =>
      Array.isArray(statement.Action) ? statement.Action : [statement.Action],
    );
    expect(actions).not.toContain('dynamodb:TransactGetItems');
    expect(actions).not.toContain('dynamodb:TransactWriteItems');
  });

  it('keeps direct, transaction-read, and transaction-write permissions separated', () => {
    const dynamo = statements.filter((statement) =>
      JSON.stringify(statement.Action).includes('dynamodb:'),
    );
    const enclosedReads = dynamo.filter(
      (statement) => statement.Condition?.StringEquals?.['dynamodb:EnclosingOperation'] === 'TransactGetItems',
    );
    const enclosedWrites = dynamo.filter(
      (statement) => statement.Condition?.StringEquals?.['dynamodb:EnclosingOperation'] === 'TransactWriteItems',
    );
    expect(enclosedReads.length).toBeGreaterThan(0);
    expect(enclosedWrites.length).toBeGreaterThan(0);
    expect(enclosedReads.every((statement) => JSON.stringify(statement.Action) === '"dynamodb:GetItem"')).toBe(true);
    expect(enclosedWrites.some((statement) => JSON.stringify(statement.Action).includes('dynamodb:UpdateItem'))).toBe(true);
    expect(enclosedWrites.every((statement) => {
      const actions = Array.isArray(statement.Action) ? statement.Action : [statement.Action];
      return actions.every((action) => [
        'dynamodb:PutItem',
        'dynamodb:UpdateItem',
        'dynamodb:ConditionCheckItem',
      ].includes(action));
    })).toBe(true);
    expect(dynamo.filter((statement) => statement.Condition?.StringEquals?.['dynamodb:EnclosingOperation'] !== undefined).every(
      (statement) =>
        statement.Condition?.['ForAllValues:StringLike']?.['dynamodb:LeadingKeys'] !== undefined,
    )).toBe(true);
  });

  it('denies item creation to the provider-fence recovery broker', () => {
    const recovery = resources(template, 'AWS::Lambda::Function').find(
      (fn) => fn.Handler === 'adapters.jobs.provider_fence_recovery.handler',
    );
    const roleId = (recovery?.Role as Props | undefined)?.['Fn::GetAtt']?.[0];
    if (typeof roleId !== 'string') throw new Error('provider-fence recovery role is missing');
    const roleStatements = resources(template, 'AWS::IAM::Policy')
      .filter((policy) => JSON.stringify(policy.Roles).includes(`\"Ref\":\"${roleId}\"`))
      .flatMap((policy) => ((policy.PolicyDocument as Props).Statement ?? []) as Props[]);
    const actions = roleStatements.flatMap((statement) =>
      Array.isArray(statement.Action) ? statement.Action : [statement.Action],
    );
    expect(actions).not.toContain('dynamodb:PutItem');
    const enclosed = roleStatements.filter(
      (statement) => statement.Condition?.StringEquals?.['dynamodb:EnclosingOperation'] === 'TransactWriteItems',
    );
    expect(enclosed).toHaveLength(1);
    expect(enclosed[0]?.Action).toEqual(['dynamodb:UpdateItem', 'dynamodb:ConditionCheckItem']);
  });

  it('confines handoff state access to the exact environment partition', () => {
    const provider = statements.filter((statement) =>
      (JSON.stringify(statement.Condition) ?? '').includes('PROJECTION_HANDOFF#dev'),
    );
    expect(provider).toHaveLength(1);
    expect(provider[0]?.Action).toEqual([
      'dynamodb:GetItem',
      'dynamodb:UpdateItem',
      'dynamodb:DeleteItem',
    ]);
    expect(provider[0]?.Condition).toEqual({
      'ForAllValues:StringEquals': {
        'dynamodb:LeadingKeys': ['PROJECTION_HANDOFF#dev'],
      },
    });
  });

  it('separates pending, blocked, and ready discovery grants by exact index and leading key', () => {
    const queries = statements.filter((statement) =>
      (Array.isArray(statement.Action) ? statement.Action : [statement.Action]).includes('dynamodb:Query'),
    );
    for (const [index, key] of [
      ['table-attempt-reconcile', 'pending'],
      ['table-attempt-reconcile', 'blocked'],
      ['stage1-plan-intent-ready', 'ready'],
    ] as const) {
      const matches = queries.filter(
        (statement) =>
          JSON.stringify(statement.Resource).includes(`/index/${index}`) &&
          statement.Condition?.['ForAllValues:StringLike']?.['dynamodb:LeadingKeys']?.[0] === key,
      );
      expect(matches).toHaveLength(1);
    }
  });
});

function synthInvalid(config: AppConfig): void {
  const app = new cdk.App();
  Template.fromStack(new IngestionStack(app, 'Invalid', {
    config,
    env: { account: config.account, region: config.region },
    sourceAssetPath: __dirname,
  }));
}

describe('ADR-075 Slice C conformance (Load Path §§2.5, 4, 5, 11)', () => {
  it('renders the reconciler schedules only from the typed stage1RecoveryEnabled boolean', () => {
    const disabled = resources(synth('INLINE_ACTIVE', false), 'AWS::Events::Rule').filter(
      (rule) => typeof rule.ScheduleExpression === 'string',
    );
    const enabled = resources(synth('INLINE_ACTIVE', true), 'AWS::Events::Rule').filter(
      (rule) => typeof rule.ScheduleExpression === 'string',
    );
    expect(disabled).toHaveLength(2);
    expect(disabled.every((rule) => rule.State === 'DISABLED')).toBe(true);
    expect(enabled).toHaveLength(2);
    expect(enabled.every((rule) => rule.State === 'ENABLED')).toBe(true);
  });

  it('rejects a QUEUED_* handoff state with recovery disabled and a non-boolean control', () => {
    expect(() => synthInvalid(configFor('QUEUED_ACCUMULATE', false))).toThrow(
      /stage1RecoveryEnabled must be true for every QUEUED_\* handoff state/,
    );
    const invalid = {
      ...TEST_CONFIG.ingestion,
      stage1RecoveryEnabled: 'false',
    } as unknown as AppConfig['ingestion'];
    expect(() => synthInvalid(configWith({ landing: { enabled: false }, ingestion: invalid }))).toThrow(
      /stage1RecoveryEnabled must be the typed boolean/,
    );
  });

  it('sizes the table writer at the reviewed 2048 MiB from the typed config', () => {
    const writer = resources(synth(), 'AWS::Lambda::Function').find(
      (fn) => fn.Handler === 'adapters.inbound.ingestion_handlers.table_writer.handler',
    );
    expect(writer?.MemorySize).toBe(2048);
    const invalid = { ...TEST_CONFIG.ingestion, tableWriterMemoryMiB: 1024 } as unknown as AppConfig['ingestion'];
    expect(() => synthInvalid(configWith({ landing: { enabled: false }, ingestion: invalid }))).toThrow(
      /tableWriterMemoryMiB must be the reviewed positive integer 2048/,
    );
  });

  it('alarms on each semantic-write dead-letter arrival, not on standing depth (§4, ADR-133)', () => {
    expectArrivalAlarm(synth().toJSON(), {
      alarmName: 'adlytics-dev-semantic-write-dead-lettered',
      queueName: 'adlytics-dev-semantic-write-dlq.fifo',
      staffed: { arm: 'inStackTopic', topicName: 'adlytics-dev-ingestion-alarms' },
    });
  });

  it.each([
    ['INLINE_ACTIVE', 2],
    ['INLINE_QUIESCED', 0],
    ['QUEUED_QUIESCED', 0],
    ['QUEUED_ACCUMULATE', 2],
    ['QUEUED_ACTIVE', 2],
    ['QUEUED_DRAIN', 0],
  ] as const)('renders delivery reserved concurrency %s → %s', (state, concurrency) => {
    const template = synth(state);
    const delivery = resources(template, 'AWS::Lambda::Function').find(
      (fn) => fn.Handler === 'adapters.inbound.ingestion_handlers.load.handler',
    );
    expect(delivery?.ReservedConcurrentExecutions).toBe(concurrency);
    // The service rejects a mapping whose MaximumConcurrency exceeds the function's
    // reservation, so a zero-reservation state must omit ScalingConfig entirely.
    const deliveryMapping = resources(template, 'AWS::Lambda::EventSourceMapping').find(
      (mapping) => JSON.stringify(mapping.EventSourceArn).includes('DeliveryQueue'),
    );
    if (concurrency === 0) {
      expect(deliveryMapping?.ScalingConfig).toBeUndefined();
    } else {
      expect(deliveryMapping?.ScalingConfig).toEqual({ MaximumConcurrency: concurrency });
    }
  });

  it('grants the pre-switch verifier exactly its read-only set and publishes its four paths', () => {
    const template = synth();
    const roles = template.findResources('AWS::IAM::Role');
    const roleId = Object.keys(roles).find((id) => id.includes('Stage1PreswitchVerificationRole'));
    if (roleId === undefined) throw new Error('verifier role is missing');
    const statements = resources(template, 'AWS::IAM::Policy')
      .filter((policy) => JSON.stringify(policy.Roles).includes(`"Ref":"${roleId}"`))
      .flatMap((policy) => ((policy.PolicyDocument as Props).Statement ?? []) as Props[]);
    const actions = statements.flatMap((statement) =>
      Array.isArray(statement.Action) ? statement.Action : [statement.Action],
    ).sort();
    expect(actions).toEqual([
      'dynamodb:DescribeTable',
      'dynamodb:GetItem',
      'dynamodb:Query',
      'dynamodb:Scan',
      'lambda:GetFunction',
      'lambda:GetFunctionConcurrency',
      'sqs:GetQueueAttributes',
    ]);
    const query = statements.find((statement) => JSON.stringify(statement.Action) === '"dynamodb:Query"');
    expect(JSON.stringify(query?.Resource)).toContain('/index/table-attempt-reconcile');
    expect(JSON.stringify(query?.Resource)).toContain('/index/stage1-plan-intent-ready');
    expect(JSON.stringify(query?.Resource)).toContain('/index/stage1-outbox-pending');
    for (const statement of statements) {
      expect(JSON.stringify(statement.Resource)).not.toContain('"*"');
    }
    const names = resources(template, 'AWS::SSM::Parameter').map((parameter) => parameter.Name);
    for (const key of [
      'stage1PreswitchVerificationRoleArn',
      'semanticWriteQueueArn',
      'semanticWriteDlqArn',
      'deliveryFunctionArn',
    ] as const) {
      expect(names).toContain(paramPath('dev', key));
    }
  });

  it('lets the provider-attempt reconciler replay a parked Start and read its owners', () => {
    const template = synth();
    const reconciler = resources(template, 'AWS::Lambda::Function').find(
      (fn) => fn.Handler === 'adapters.jobs.provider_attempt_reconciler.handler',
    );
    const roleId = (reconciler?.Role as Props | undefined)?.['Fn::GetAtt']?.[0];
    if (typeof roleId !== 'string') throw new Error('reconciler role is missing');
    const statements = resources(template, 'AWS::IAM::Policy')
      .filter((policy) => JSON.stringify(policy.Roles).includes(`"Ref":"${roleId}"`))
      .flatMap((policy) => ((policy.PolicyDocument as Props).Statement ?? []) as Props[]);
    const actions = statements.flatMap((statement) =>
      Array.isArray(statement.Action) ? statement.Action : [statement.Action],
    );
    expect(actions).toContain('athena:StartQueryExecution');
    expect(actions).toContain('athena:GetQueryExecution');
    expect(actions).toContain('athena:StopQueryExecution');
    const directReads = statements.filter(
      (statement) =>
        JSON.stringify(statement.Action) === '"dynamodb:GetItem"' &&
        statement.Condition?.StringEquals?.['dynamodb:EnclosingOperation'] === undefined,
    );
    const readKeys = directReads.flatMap(
      (statement) => statement.Condition?.['ForAllValues:StringLike']?.['dynamodb:LeadingKeys'] ?? [],
    );
    expect(readKeys).toEqual(expect.arrayContaining(['TABLE_ATTEMPT#*', 'WRITE_PLAN#*', 'STATUS_FLOW#*', 'TABLE#*']));
    expect(readKeys).not.toContain('PROVIDER_RECONCILE_BLOCK#*');
  });

  it('resolves every declared Lambda handler to a packaged module exposing handler', () => {
    const template = synth();
    const root = path.join(__dirname, '..', '..', 'src');
    for (const fn of resources(template, 'AWS::Lambda::Function')) {
      const handler = String(fn.Handler);
      const [modulePath, attribute] = [handler.slice(0, handler.lastIndexOf('.')), handler.slice(handler.lastIndexOf('.') + 1)];
      const file = path.join(root, `${modulePath.replace(/\./g, '/')}.py`);
      expect({ handler, exists: fs.existsSync(file) }).toEqual({ handler, exists: true });
      expect(attribute).toBe('handler');
      expect(fs.readFileSync(file, 'utf8')).toMatch(/^def handler\(/m);
    }
  });
});
