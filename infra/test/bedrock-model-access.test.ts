// SPDX-License-Identifier: MIT-0
/**
 * A-IAM-MODEL-001: every Bedrock model grant is bounded to its own inference profile, pinned to
 * the stack's Region, or, for a bare model ID, to the foundation model in that Region.
 */
import * as path from 'node:path';

import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';

import { invocableModelArn, modelInvokeStatements } from '../lib/bedrock-model-access';
import {
  AppConfig,
  BEDROCK_PROVIDER_NAMESPACES,
  bedrockBaseModelId,
  isInferenceProfileId,
  PROFILE_GEOGRAPHIES,
} from '../lib/config';
import { ApiStack } from '../lib/stacks/api-stack';
import { EvaluationV4Stack } from '../lib/stacks/evaluation-v4-stack';
import { HarnessStack } from '../lib/stacks/harness-proof-stack';
import { InsightContinuationStack } from '../lib/stacks/insight-continuation-stack';
import { InsightPrecomputeStack } from '../lib/stacks/insight-precompute-stack';
import { InsightPromptCalibrationStack } from '../lib/stacks/insight-prompt-calibration-stack';
import { MemoryStack } from '../lib/stacks/memory-stack';
import { configWith, TEST_CONFIG } from './support/config';
import {
  EVALUATION_CORPORA,
  EVALUATOR_V4_PROMPTS,
  everyTemplate,
  MEMORY_PROMPTS,
  resourcesOfType,
} from './support/templates';

type Json = Record<string, unknown>;

/** The complete message `action` throws; fails the test when it throws nothing or a non-Error. */
function thrownMessage(action: () => unknown): string {
  try {
    action();
  } catch (error) {
    if (error instanceof Error) return error.message;
    throw new Error(`expected an Error, caught ${String(error)}`);
  }
  throw new Error('expected the action to throw');
}

const ASSET = __dirname;
const CONTRACTS = path.join(__dirname, '..', '..', 'contracts');

function statements(template: unknown): Json[] {
  return [...resourcesOfType(template, 'AWS::IAM::Policy'), ...resourcesOfType(template, 'AWS::IAM::ManagedPolicy')]
    .flatMap((policy) => (((policy.PolicyDocument as Json).Statement ?? []) as Json[]));
}

const isBedrock = (statement: Json): boolean =>
  [statement.Action].flat().some((action) => String(action).startsWith('bedrock:'));

/** A resource rendered as text: a literal, or an `Fn::Join` with each token shown as `{Ref}`. */
function rendered(resource: unknown): string | undefined {
  if (typeof resource === 'string') return resource;
  const join = (resource as Json)['Fn::Join'] as [string, unknown[]] | undefined;
  if (join === undefined) return undefined;
  return join[1].map((part) => (typeof part === 'string' ? part : `{${String((part as Json).Ref)}}`)).join(join[0]);
}

describe('every Bedrock model grant in the app (A-IAM-MODEL-001)', () => {
  const allow = Object.values(everyTemplate()).flatMap(statements)
    .filter((statement) => statement.Effect === 'Allow' && isBedrock(statement));

  it('finds the model grants it is meant to bound', () => {
    expect(allow.length).toBeGreaterThanOrEqual(7);
  });

  it('names no Region wildcard, except a foundation model that carries the profile condition', () => {
    // Only ARN-shaped resources: `bedrock:InvokeGuardrailChecks` has no resource type and is
    // granted on the bare `*` (gateway-policy.ts; A-IAM-SERVICE-001), outside this rule.
    for (const statement of allow) {
      for (const resource of [statement.Resource].flat()) {
        const text = rendered(resource);
        if (text === undefined || !text.startsWith('arn:')) continue;
        expect(text).not.toMatch(/inference-profile\/\*$/);
        expect(text).not.toMatch(/foundation-model\/\*$/);
        if (/:bedrock:\*:/.test(text)) {
          expect(text).toMatch(/:bedrock:\*::foundation-model\//);
          expect(Object.keys((statement.Condition ?? {}) as Json)).toStrictEqual(['ArnEquals']);
          expect(Object.keys(((statement.Condition as Json).ArnEquals) as Json))
            .toStrictEqual(['bedrock:InferenceProfileArn']);
        }
      }
    }
  });
});

describe('profile-configured models (A-IAM-MODEL-001)', () => {
  const PROFILES = {
    insightQuick: 'us.test.insight-quick-model-v1:0',
    insightDeep: 'us.test.insight-deep-model-v1:0',
    deepDive: 'us.test.deep-dive-model-v1:0',
    quickChat: 'us.test.quick-chat-model-v1:0',
    memory: 'us.test.memory-model-v1:0',
  };
  const config: AppConfig = configWith({
    transactionSearchEnabled: true,
    evaluationInput: { provisioned: true },
    onlineEvaluation: { provisioned: true, samplingPercentage: 10, sessionTimeoutMinutes: 1 },
    agent: {
      ...TEST_CONFIG.agent,
      quickChat: { ...TEST_CONFIG.agent.quickChat, modelId: PROFILES.quickChat },
      deepDive: { ...TEST_CONFIG.agent.deepDive, modelId: PROFILES.deepDive },
      memoryModelId: PROFILES.memory,
    },
    insight: { quickModelId: PROFILES.insightQuick, deepModelId: PROFILES.insightDeep },
  });
  const env = { account: config.account, region: config.region };
  const app = new cdk.App();
  const stacks = [
    new HarnessStack(app, 'Harness', {
      config,
      env,
      toolSchemaPath: path.join(CONTRACTS, 'generated', 'tools.json'),
      promptPath: path.join(CONTRACTS, 'prompts', 'v1', 'managed-conversation.md'),
      sourceAssetPath: ASSET,
    }),
    new ApiStack(app, 'Api', { config, env, sourceAssetPath: ASSET }),
    new InsightPrecomputeStack(app, 'InsightPrecompute', { config, env, sourceAssetPath: ASSET }),
    new InsightContinuationStack(app, 'InsightContinuation', { config, env, sourceAssetPath: ASSET }),
    new InsightPromptCalibrationStack(app, 'InsightPromptCalibration', { config, env, sourceAssetPath: ASSET }),
    new EvaluationV4Stack(app, 'EvaluationV4', {
      config,
      env,
      promptPaths: EVALUATOR_V4_PROMPTS,
      calibrationCorpusPath: EVALUATION_CORPORA.v4,
      sourceAssetPath: ASSET,
    }),
    new MemoryStack(app, 'Memory', { config, env, promptPaths: MEMORY_PROMPTS }),
  ];
  const allow = stacks.flatMap((stack) => statements(Template.fromStack(stack).toJSON()))
    .filter((statement) => statement.Effect === 'Allow' && isBedrock(statement));
  const profileArn = (id: string): string =>
    `arn:{AWS::Partition}:bedrock:${config.region}:${config.account}:inference-profile/${id}`;

  it('grants each profile in this Region, and its foundation model only through that profile', () => {
    // Harness 1, Presentation/Precompute/Continuation/Calibration 2 each, gate execution and
    // provider 2 each (the deep and quick evaluator models), Memory 1: 14 profile/model pairs.
    const profiles = allow.filter((statement) => /Profile\d+$/.test(String(statement.Sid)));
    const models = allow.filter((statement) => /Model\d+$/.test(String(statement.Sid)));
    expect(profiles).toHaveLength(14);
    expect(models).toHaveLength(14);
    // The partition is exhaustive: a model grant outside the audited shape, such as an L2's own
    // unsid'd statement, would otherwise escape every check below. The one Bedrock grant left
    // outside it is the conversation function's guardrail, which invokes no model.
    const outside = allow.filter((statement) => !profiles.includes(statement) && !models.includes(statement));
    expect(outside.map((statement) => [statement.Action].flat())).toStrictEqual([['bedrock:ApplyGuardrail']]);
    for (const statement of profiles) {
      const resource = rendered(statement.Resource);
      expect(Object.values(PROFILES).map(profileArn)).toContain(resource);
      expect(statement.Condition).toBeUndefined();
    }
    for (const statement of models) {
      const condition = (statement.Condition as Json).ArnEquals as Json;
      const profile = rendered(condition['bedrock:InferenceProfileArn']);
      const id = Object.values(PROFILES).find((candidate) => profileArn(candidate) === profile);
      expect(id).toBeDefined();
      // The foundation model is the profile's own model, never another model's.
      expect(rendered(statement.Resource)).toBe(`arn:{AWS::Partition}:bedrock:*::foundation-model/${bedrockBaseModelId(id!)}`);
      expect(Object.keys(statement.Condition as Json)).toStrictEqual(['ArnEquals']);
    }
  });

  it('pairs each foundation-model statement with the profile statement of the same index and Sid', () => {
    const bySid = new Map(allow.map((statement) => [`${String(statement.Sid)}`, statement]));
    for (const statement of allow.filter((candidate) => /Model\d+$/.test(String(candidate.Sid)))) {
      const sibling = bySid.get(String(statement.Sid).replace(/Model(\d+)$/, 'Profile$1'));
      expect(sibling).toBeDefined();
      expect(rendered(((statement.Condition as Json).ArnEquals as Json)['bedrock:InferenceProfileArn']))
        .toBe(rendered(sibling!.Resource));
    }
  });
});

describe('modelInvokeStatements and the profile classifier (A-IAM-MODEL-001)', () => {
  const stack = (): cdk.Stack =>
    new cdk.Stack(new cdk.App(), 'S', { env: { account: '111122223333', region: 'eu-west-1' } });

  it('names a profile ID as the profile in this Region, for an L2 model', () => {
    const scope = stack();
    expect(scope.resolve(invocableModelArn(scope, 'us.anthropic.test-model-v1:0'))).toStrictEqual({
      'Fn::Join': [
        '',
        ['arn:', { Ref: 'AWS::Partition' }, ':bedrock:eu-west-1:111122223333:inference-profile/us.anthropic.test-model-v1:0'],
      ],
    });
  });
  it('names a bare ID as the foundation model in this Region, for an L2 model', () => {
    const scope = stack();
    expect(scope.resolve(invocableModelArn(scope, 'anthropic.test-model-v1:0'))).toStrictEqual({
      'Fn::Join': [
        '',
        ['arn:', { Ref: 'AWS::Partition' }, ':bedrock:eu-west-1::foundation-model/anthropic.test-model-v1:0'],
      ],
    });
  });
  // The complete message, not a substring: the refusal is the operator's only diagnostic.
  const UNLISTED_GEOGRAPHY =
    'Model ID xx.anthropic.test-model-v1:0 looks like an inference profile with geography "xx", which '
    + 'PROFILE_GEOGRAPHIES does not list. Confirm the prefix with "aws bedrock '
    + 'list-inference-profiles --type-equals SYSTEM_DEFINED", then add it to '
    + 'PROFILE_GEOGRAPHIES in infra/lib/config/index.ts.';
  it('refuses an unlisted geography for an L2 model too', () => {
    expect(thrownMessage(() => invocableModelArn(stack(), 'xx.anthropic.test-model-v1:0')))
      .toBe(UNLISTED_GEOGRAPHY);
  });
  it('refuses a profile-shaped ID whose geography is not listed', () => {
    expect(thrownMessage(() => modelInvokeStatements(stack(), 'S', ['xx.anthropic.test-model-v1:0'], ['bedrock:InvokeModel'])))
      .toBe(UNLISTED_GEOGRAPHY);
  });

  it.each(['openai.gpt-5.4', 'deepseek.v3.2', 'zai.glm-4.7-flash'])(
    'grants the dotted bare ID %s as an in-Region foundation model with no condition',
    (modelId) => {
      const scope = stack();
      const [statement, ...rest] = modelInvokeStatements(scope, 'S', [modelId], ['bedrock:InvokeModel']);
      expect(rest).toStrictEqual([]);
      const json = scope.resolve(statement!.toStatementJson()) as Json;
      expect(json).toStrictEqual({
        Sid: 'SModel0',
        Effect: 'Allow',
        Action: 'bedrock:InvokeModel',
        Resource: { 'Fn::Join': ['', ['arn:', { Ref: 'AWS::Partition' }, `:bedrock:eu-west-1::foundation-model/${modelId}`]] },
      });
    },
  );

  it('grants one profile pair per distinct ID, and a duplicate ID once', () => {
    const scope = stack();
    const found = modelInvokeStatements(scope, 'S', ['jp.anthropic.m-v1:0', 'jp.anthropic.m-v1:0'], ['bedrock:InvokeModel'])
      .map((statement) => scope.resolve(statement.toStatementJson()) as Json);
    expect(found.map((statement) => statement.Sid)).toStrictEqual(['SProfile0', 'SModel0']);
  });

  it('holds the closed geography and provider lists, disjoint', () => {
    expect([...PROFILE_GEOGRAPHIES]).toStrictEqual(['us', 'eu', 'apac', 'global', 'jp', 'au', 'ca', 'in', 'us-gov']);
    expect([...BEDROCK_PROVIDER_NAMESPACES]).toStrictEqual([
      'ai21', 'amazon', 'anthropic', 'cohere', 'deepseek', 'google', 'meta', 'minimax', 'mistral',
      'moonshot', 'moonshotai', 'nvidia', 'openai', 'qwen', 'stability', 'twelvelabs', 'writer', 'xai',
      'zai',
    ]);
    const geographies: readonly string[] = PROFILE_GEOGRAPHIES;
    expect(BEDROCK_PROVIDER_NAMESPACES.filter((namespace) => geographies.includes(namespace))).toStrictEqual([]);
  });

  it('strips every listed geography, the newly listed ones included', () => {
    expect(bedrockBaseModelId('jp.test.model-v1:0')).toBe('test.model-v1:0');
    expect(bedrockBaseModelId('us-gov.test.model-v1:0')).toBe('test.model-v1:0');
    expect(bedrockBaseModelId('in.test.model-v1:0')).toBe('test.model-v1:0');
    expect(isInferenceProfileId('au.test.model-v1:0')).toBe(true);
    expect(isInferenceProfileId('test.model-v1:0')).toBe(false);
  });
});
