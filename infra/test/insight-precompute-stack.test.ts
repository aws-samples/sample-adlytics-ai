// SPDX-License-Identifier: MIT-0
import * as cdk from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';

import { AppConfig, paramPath } from '../lib/config';
import { BAKE_TASKS, ROUTES } from '../lib/generated/execution-graph-protocol';
import { InsightPrecomputeStack } from '../lib/stacks/insight-precompute-stack';
import { TEST_CONFIG, configWith } from './support/config';
import { grantsOfRoleLogicalId, listBucketPrefixes, resultPrefixGrants } from './support/iam';
import { alarmsOnQueue, expectArrivalAlarm, expectBoundedStreamConsumer } from './support/async-failure';

function synth(config: AppConfig = TEST_CONFIG): Template {
  const app = new cdk.App();
  return Template.fromStack(
    new InsightPrecomputeStack(app, 'InsightPrecompute', {
      config,
      env: { account: config.account, region: config.region },
      sourceAssetPath: __dirname,
    }),
  );
}

const PROVISIONED: AppConfig = configWith({
  operationalStatus: {
    provisioned: true,
    stage1LifecycleReconcileBatchSize: 10,
    stage1OutboxMaxConsecutiveFailedDeliveryAttempts: 3,
  },
});

describe('retained insight precompute worker', () => {
  it('receives the retained Semantic V2 bucket and cache parameter required for materialization', () => {
    const template = synth();
    template.hasResourceProperties('AWS::Lambda::Function', {
      Environment: {
        Variables: Match.objectLike({
          // The INSIGHT model seam, never the chat agent's (ADR-076 Phase 1): the
          // fixture values differ exactly so a rewire to the agent models fails here.
          ADL_INSIGHT_QUICK_MODEL_ID: TEST_CONFIG.insight.quickModelId,
          ADL_INSIGHT_CACHE_TABLE: Match.anyValue(),
          ADL_INSIGHT_DEEP_MODEL_ID: TEST_CONFIG.insight.deepModelId,
          ADL_PRESET_ARTIFACT_BUCKET: Match.anyValue(),
          ADL_SEMANTIC_V2_ATHENA_INSIGHT_PRECOMPUTE_WORKGROUP: 'adlytics-dev-v2-insight-precompute',
          ADL_SEMANTIC_V2_CURRENT_DATABASE: Match.anyValue(),
          ADL_ATHENA_RESULT_REUSE_MAX_AGE_MINUTES: '60',
          // S2: the 2 x 4 `insight_precompute` row; model calls keep their own eight.
          ADL_ATHENA_QUERY_BATCH_MAX_CONCURRENCY: '4',
          ADL_INSIGHT_GENERATION_MAX_CONCURRENCY: '8',
        }),
      },
      ReservedConcurrentExecutions: 2,
    });
    expect(JSON.stringify(template.toJSON())).toContain(
      'SsmParameterValueadlyticsdevinsightcachetable',
    );
  });

  it('keeps the insight 2 x 4 bound whatever the general query-batch admission is (D7)', () => {
    synth(configWith({ athenaQueryBatchMaxConcurrency: 3 })).hasResourceProperties(
      'AWS::Lambda::Function',
      {
        Environment: {
          Variables: Match.objectLike({
            ADL_ATHENA_QUERY_BATCH_MAX_CONCURRENCY: '4',
            ADL_INSIGHT_GENERATION_MAX_CONCURRENCY: '8',
          }),
        },
        ReservedConcurrentExecutions: 2,
      },
    );
  });
  it('invokes only the configured insight models, never the chat agent seam', () => {
    const policy = JSON.stringify(synth().toJSON());
    expect(policy).toContain(`:eu-west-1::foundation-model/${TEST_CONFIG.insight.quickModelId}`);
    expect(policy).toContain(`:eu-west-1::foundation-model/${TEST_CONFIG.insight.deepModelId}`);
    expect(policy).not.toContain('inference-profile');
    expect(policy).not.toContain(TEST_CONFIG.agent.quickChat.modelId);
    expect(policy).not.toContain(TEST_CONFIG.agent.deepDive.modelId);
  });

  it('gives the worker exactly its plan 2.4 results-prefix set and starts only on its workgroup', () => {
    const template = synth().toJSON() as Record<string, any>;
    const worker = Object.values(template.Resources as Record<string, any>).find(
      (resource) =>
        resource.Type === 'AWS::Lambda::Function' &&
        resource.Properties.Handler === 'adapters.jobs.insight_precompute.handler.handler',
    );
    const role = worker.Properties.Role['Fn::GetAtt'][0] as string;
    const results = paramPath(TEST_CONFIG.envName, 'semanticV2AthenaResultsBucketName');
    const expected = [
      's3:AbortMultipartUpload insight-precompute/*',
      's3:GetObject insight-precompute/*',
      's3:PutObject insight-precompute/*',
    ];
    expect(resultPrefixGrants(template, role, results)).toEqual(expected);
    expect(listBucketPrefixes(template, role, results)).toEqual(['insight-precompute/*']);
    const starts = [...grantsOfRoleLogicalId(template, role)].filter((grant) =>
      grant.startsWith('athena:StartQueryExecution '),
    );
    expect(starts).toHaveLength(1);
    expect(starts[0]).toMatch(/:workgroup\/adlytics-dev-v2-insight-precompute$/);
    // Paired control: a DeleteObject grant on the prefix breaks the same equality.
    const mutated = JSON.parse(JSON.stringify(template)) as Record<string, any>;
    for (const resource of Object.values(mutated.Resources as Record<string, any>)) {
      if (resource.Type !== 'AWS::IAM::Policy') continue;
      for (const statement of resource.Properties.PolicyDocument.Statement as Record<string, any>[]) {
        if (JSON.stringify(statement.Resource).includes('/insight-precompute/*')) {
          statement.Action = [...[statement.Action].flat(), 's3:DeleteObject'];
        }
      }
    }
    expect(resultPrefixGrants(mutated, role, results)).not.toEqual(expected);
  });

  it('grants GetObject on exactly the two approved artifact prefixes', () => {
    const template = synth().toJSON() as Record<string, any>;
    const worker = Object.values(template.Resources as Record<string, any>).find(
      (resource) =>
        resource.Type === 'AWS::Lambda::Function' &&
        resource.Properties.Handler === 'adapters.jobs.insight_precompute.handler.handler',
    );
    const role = worker.Properties.Role['Fn::GetAtt'][0] as string;
    const bucket = paramPath(TEST_CONFIG.envName, 'publisherArtifactBucketName');
    const expected = ['s3:GetObject custody/*', 's3:GetObject pages/*'];
    const assertExact = (candidate: Record<string, any>): void => {
      expect(resultPrefixGrants(candidate, role, bucket)).toEqual(expected);
      const artifactReads = [...grantsOfRoleLogicalId(candidate, role)].filter(
        (grant) => grant.startsWith('s3:GetObject ') && /\/(pages|custody|other)\/\*$/.test(grant),
      );
      expect(artifactReads).toHaveLength(2);
      expect(artifactReads.every((grant) => grant.includes(`:s3:::${bucket}/`))).toBe(true);
      expect([...grantsOfRoleLogicalId(candidate, role)]).not.toContain('s3:ListBucket *');
    };

    assertExact(template);
    const grants = [...grantsOfRoleLogicalId(template, role)];
    expect(grants.some((grant) => grant.endsWith(`:s3:::${bucket}/pages/*`))).toBe(true);
    expect(grants.some((grant) => grant.endsWith(`:s3:::${bucket}/custody/*`))).toBe(true);
    expect(grants.some((grant) => grant.includes(`:s3:::${bucket}/other/*`))).toBe(false);
    expect(grants.some((grant) => grant.includes(':s3:::another-bucket/'))).toBe(false);

    const withOtherPrefix = structuredClone(template);
    const artifactPolicy = Object.values(withOtherPrefix.Resources as Record<string, any>)
      .find((resource) =>
        resource.Type === 'AWS::IAM::Policy' &&
        JSON.stringify(resource.Properties.PolicyDocument).includes('/pages/*'),
      );
    artifactPolicy.Properties.PolicyDocument.Statement
      .find((statement: Record<string, any>) => statement.Sid === 'ReadPresetArtifacts')
      .Resource.push(`arn:aws:s3:::${bucket}/other/*`);
    expect(() => assertExact(withOtherPrefix)).toThrow();

    const withOtherBucket = structuredClone(template);
    const otherPolicy = Object.values(withOtherBucket.Resources as Record<string, any>)
      .find((resource) =>
        resource.Type === 'AWS::IAM::Policy' &&
        JSON.stringify(resource.Properties.PolicyDocument).includes('/pages/*'),
      );
    otherPolicy.Properties.PolicyDocument.Statement
      .find((statement: Record<string, any>) => statement.Sid === 'ReadPresetArtifacts')
      .Resource.push('arn:aws:s3:::another-bucket/pages/*');
    expect(() => assertExact(withOtherBucket)).toThrow();
  });

  it('uses only the insight-precompute Athena workgroup and result prefix', () => {
    const policy = JSON.stringify(synth().toJSON());

    expect(policy).toContain('adlytics-dev-v2-insight-precompute');
    expect(policy).toContain('/insight-precompute/*');
    expect(policy).not.toContain('adlytics-dev-v2-batch');
    expect(policy).not.toContain('/batch/*');
    expect(policy).not.toContain('adlytics-dev-v2-bake');
    expect(policy).not.toContain('adlytics-dev-v2-interactive');
    expect(policy).not.toContain('/interactive/*');
  });

  it('grants only exact cache reads, writes, updates, and primary-key Query access plus retained publication index and canonical-current reads', () => {
    const template = synth();
    expect(JSON.stringify(template.toJSON())).toContain(
      'SsmParameterValueadlyticsdevinsightcachetablearn',
    );
    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: ['dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:Query', 'dynamodb:UpdateItem'],
            Resource: Match.anyValue(),
          }),
        ]),
      },
    });
    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: 'dynamodb:TransactGetItems',
            Resource: Match.anyValue(),
          }),
          Match.objectLike({
            Action: 'dynamodb:Query',
            Resource: Match.anyValue(),
          }),
          Match.objectLike({
            Action: 's3:GetObject',
            Resource: Match.anyValue(),
          }),
        ]),
      },
    });
  });
});

// --- S6 slice 5i (§2.8, plan B2): the graph precompute FIFO ------------------------------------

describe('graph precompute FIFO', () => {
  it('adds the FIFO and its DLQ with maxReceiveCount from the generated route, batch size 1', () => {
    const template = synth(PROVISIONED);
    expect(ROUTES.precompute.max_receive_count).toBe(3);
    template.hasResourceProperties('AWS::SQS::Queue', {
      QueueName: 'adlytics-dev-insight-precompute.fifo',
      FifoQueue: true,
      ContentBasedDeduplication: true,
      // TD-279: the precompute claim lease the detector and the armed finalizer derive from.
      VisibilityTimeout: BAKE_TASKS.precompute_claim_lease_seconds,
      RedrivePolicy: Match.objectLike({ maxReceiveCount: ROUTES.precompute.max_receive_count }),
    });
    template.hasResourceProperties('AWS::SQS::Queue', {
      QueueName: 'adlytics-dev-insight-precompute-dlq.fifo',
      FifoQueue: true,
    });
    template.hasResourceProperties('AWS::Lambda::EventSourceMapping', {
      BatchSize: 1,
      FunctionResponseTypes: ['ReportBatchItemFailures'],
      EventSourceArn: Match.objectLike({ 'Fn::GetAtt': Match.arrayWith([Match.stringLikeRegexp('GraphPrecomputeQueue')]) }),
    });
    template.hasResourceProperties('AWS::Lambda::Function', {
      Timeout: 30,
      Environment: { Variables: Match.objectLike({ ADL_EXECUTION_GRAPH_TABLE: 'adlytics-dev-execution-graph' }) },
    });
  });

  it('control: without the graph authority there is no FIFO and no graph table', () => {
    const template = synth();
    // The stream failure queue (ADR-133) is not graph-gated; no FIFO is.
    template.resourcePropertiesCountIs('AWS::SQS::Queue', { FifoQueue: true }, 0);
    const json = JSON.stringify(template.toJSON());
    expect(json).not.toContain('ADL_EXECUTION_GRAPH_TABLE');
    expect(json).not.toContain('insight-precompute.fifo');
  });
});

// --- S6 part 2 (§2.11 rows 6 and 7): the precompute consumer's grants and its DLQ alarm ---------

function workerStatements(template: Template): Record<string, unknown>[] {
  const json = template.toJSON() as { Resources: Record<string, { Type: string; Properties: any }> };
  return Object.values(json.Resources)
    .filter((resource) => resource.Type === 'AWS::IAM::Policy' || resource.Type === 'AWS::IAM::ManagedPolicy')
    .filter((resource) => JSON.stringify(resource.Properties.Roles ?? []).includes('PrecomputeServiceRole'))
    .flatMap((resource) => resource.Properties.PolicyDocument.Statement as Record<string, unknown>[]);
}

type Statement = Record<string, unknown>;
/** The Baker-only V3 bake authority (plan 4.6 F1, A3): never on InsightPrecompute. */
const BAKE_ONLY_KEYS = ['DUE#', 'SERVING_DAY#', 'SERVING_DOMAIN#', 'SERVING_FRONTIER#', 'SERVING_PUBLICATION#',
  'GENERATION#'];

/** Fail unless `graph` is exactly the unchanged PostGrantConsumer family and one mode read. */
function expectPostGrantConsumerOnly(graph: Statement[]): void {
  const families = ['EXEC_CLAIM#*', 'RUN#*', 'TOKEN#*', 'LOCK#*', 'COHORT#*', 'SERVING#*'];
  const bySid = new Map(graph.map((statement) => [statement.Sid as string, statement]));
  expect([...bySid.keys()].toSorted()).toEqual([
    'DenyOperatorOnlyGraphWrites',
    'ReadGraphFamilies',
    'ReadGraphModeItem',
    'TransactGraphFamilies',
    'UpdateGraphClaims',
  ]);
  expect(bySid.get('ReadGraphModeItem')!.Action).toBe('dynamodb:GetItem');
  expect(bySid.get('ReadGraphModeItem')!.Condition).toEqual({
    'ForAllValues:StringEquals': { 'dynamodb:LeadingKeys': ['MODE#ingestion'] },
  });
  expect(bySid.get('ReadGraphFamilies')!.Condition).toEqual({
    'ForAllValues:StringLike': { 'dynamodb:LeadingKeys': families },
  });
  expect(bySid.get('TransactGraphFamilies')!.Condition).toEqual({
    'ForAllValues:StringLike': { 'dynamodb:LeadingKeys': families },
    StringEquals: { 'dynamodb:EnclosingOperation': 'TransactWriteItems' },
  });
  expect(bySid.get('UpdateGraphClaims')!.Condition).toEqual({
    'ForAllValues:StringLike': { 'dynamodb:LeadingKeys': ['EXEC_CLAIM#*'] },
  });
  const everything = JSON.stringify(graph);
  for (const key of BAKE_ONLY_KEYS) expect(everything).not.toContain(key);
}

describe('§2.11 graph rows in InsightPrecompute', () => {
  const graphOf = (template: Template): Statement[] => workerStatements(template).filter((statement) =>
    JSON.stringify(statement.Resource).includes('table/adlytics-dev-execution-graph"'),
  );

  it('rows 6 and 7: one mode read and the unchanged PostGrantConsumer family, without the mode check', () => {
    expectPostGrantConsumerOnly(graphOf(synth(PROVISIONED)));
    const everything = JSON.stringify(workerStatements(synth(PROVISIONED)));
    for (const key of BAKE_ONLY_KEYS) expect(everything).not.toContain(key);
  });

  it('control (F1): a widened PostGrantConsumer fails the helper, one mutation at a time', () => {
    const graph = graphOf(synth(PROVISIONED));
    const withDue = graph.map((statement) => {
      if (statement.Sid !== 'TransactGraphFamilies') return statement;
      const condition = structuredClone(statement.Condition) as Record<string, Record<string, string[]>>;
      condition['ForAllValues:StringLike']!['dynamodb:LeadingKeys']!.push('DUE#*');
      return { ...statement, Condition: condition };
    });
    expect(() => expectPostGrantConsumerOnly(withDue)).toThrow();
    const withFlip = [...graph, {
      Sid: 'FinalizeGenerationPointers',
      Effect: 'Allow',
      Action: 'dynamodb:UpdateItem',
      Resource: graph[0]!.Resource,
      Condition: { 'ForAllValues:StringLike': { 'dynamodb:LeadingKeys': ['GENERATION#*'] } },
    }];
    expect(() => expectPostGrantConsumerOnly(withFlip)).toThrow();
  });

  it('row 7: the precompute DLQ alarm is an arrival alarm on the staffed destination', () => {
    const template = synth(PROVISIONED).toJSON();
    expectArrivalAlarm(template, {
      alarmName: 'adlytics-dev-insight-precompute-dead-lettered',
      queueName: 'adlytics-dev-insight-precompute-dlq.fifo',
      staffed: { arm: 'published' },
    });
    // Plan 6.2 (DB26): it is the queue's only alarm, so a visible message pages once.
    expect(alarmsOnQueue(template, 'adlytics-dev-insight-precompute-dlq.fifo')).toEqual([
      'adlytics-dev-insight-precompute-dead-lettered',
    ]);
  });

  it('control: without the graph authority there is no graph grant and no precompute DLQ alarm', () => {
    const template = synth();
    template.resourcePropertiesCountIs(
      'AWS::CloudWatch::Alarm',
      { AlarmName: 'adlytics-dev-insight-precompute-dead-lettered' },
      0,
    );
    expect(JSON.stringify(workerStatements(template))).not.toContain('execution-graph');
  });
});

describe('insight precompute failure path (ADR-133)', () => {
  it('bounds publications-stream retries and sends an exhausted batch to an alarmed failure queue', () => {
    expectBoundedStreamConsumer(synth().toJSON(), {
      mappingId: /^PublicationCurrentMapping$/,
      queueName: 'adlytics-dev-insight-precompute-stream-failures',
      alarmName: 'adlytics-dev-insight-precompute-stream-exhausted',
      alarmDescription:
        'A publications-stream batch exhausted its bounded insight precompute retries; its '
        + 'insights were not precomputed. Disposition: attempts_failures status --consumer '
        + 'insight-precompute, then attempts_failures drain --consumer insight-precompute, '
        + 'then run a declarative re-bake (operator-tools.md).',
      roleId: /^PrecomputeServiceRole[0-9A-F]{8}$/,
      mapping: {
        BatchSize: 10,
        EventSourceArn: {
          Ref: expect.stringMatching(/^SsmParameterValueadlyticsdevdatav2semanticpublicationsstreamarn/),
        },
        FunctionName: { Ref: expect.stringMatching(/^Precompute[0-9A-F]{8}$/) },
        FunctionResponseTypes: ['ReportBatchItemFailures'],
        ParallelizationFactor: 1,
        StartingPosition: 'LATEST',
      },
    });
  });
});
