// SPDX-License-Identifier: MIT-0
/**
 * MemoryStack: conversation state, and the two properties of it that cannot be fixed afterwards.
 *
 * Retention and the strategy set. A memory deployed without a strategy is blind to every
 * conversation that happened before the strategy was added — long-term memory processes only events
 * created after one reaches `ACTIVE` — and the event expiry is applied per event at write time, so
 * raising it later does not extend anything already written. Both are asserted here because both
 * fail as data quietly missing rather than as an error.
 *
 * Doubles as the synth-time check for the stack, like every other suite here.
 */

import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';

import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';

import { MemoryStack } from '../lib/stacks/memory-stack';
import { memoryName, paramPath } from '../lib/config';
import type { AppConfig } from '../lib/config';
import { TEST_CONFIG, configWith } from './support/config';
import { MEMORY_PROMPTS } from './support/templates';

/** The name pattern AWS documents for `MemoryName`: underscores only, up to 48 characters. */
const MEMORY_NAME_PATTERN = /^[a-zA-Z][a-zA-Z0-9_]{0,47}$/;

function synth(
  config: AppConfig = TEST_CONFIG,
  promptPaths: { extraction: string; consolidation: string } = MEMORY_PROMPTS,
): Template {
  const app = new cdk.App();
  const stack = new MemoryStack(app, 'Adlytics-Memory-test', {
    config,
    env: { account: config.account, region: config.region },
    promptPaths,
  });
  return Template.fromStack(stack);
}

const EXTRACTION = fs.readFileSync(MEMORY_PROMPTS.extraction, 'utf-8').trim();
const CONSOLIDATION = fs.readFileSync(MEMORY_PROMPTS.consolidation, 'utf-8').trim();
const CONTEXT_DESCRIPTION =
  'Durable context about this advertiser: preferences, goals, business context and vocabulary. ' +
  'Never measured results.';
const PROFILE_CONFIG = configWith({
  agent: { ...TEST_CONFIG.agent, memoryModelId: 'us.test.memory-model-v1:0' },
});

type Props = Record<string, any>;

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

function only<T>(items: readonly T[], what: string): T {
  const [first] = items;
  if (items.length !== 1 || first === undefined) {
    throw new Error(`expected exactly one ${what}, found ${items.length}`);
  }
  return first;
}

function memory(template: Template): Props {
  const found = Object.values(template.findResources('AWS::BedrockAgentCore::Memory'));
  return only(found, 'memory').Properties as Props;
}

/** The strategies, keyed by the wrapper name CloudFormation uses for each type. */
function strategies(template: Template): Record<string, Props> {
  const rendered = memory(template).MemoryStrategies as Props[];
  return Object.fromEntries(
    rendered.map((entry) => {
      const [type] = Object.keys(entry);
      if (type === undefined) throw new Error('a strategy rendered with no type wrapper');
      return [type, entry[type] as Props];
    }),
  );
}

describe('MemoryStack', () => {
  describe('what a stack delete must not take', () => {
    it('retains the memory', () => {
      for (const resource of Object.values(
        synth().findResources('AWS::BedrockAgentCore::Memory'),
      )) {
        expect((resource as Props).DeletionPolicy).toBe('Retain');
        expect((resource as Props).UpdateReplacePolicy).toBe('Retain');
      }
    });

    /**
     * A retained memory whose execution role was deleted cannot consolidate anything: the events
     * remain and the long-term strategies stop working. So the role being retained alongside is
     * part of what makes retention mean something, not collateral.
     */
    it('retains the memory service role with it', () => {
      const roles = Object.values(synth().findResources('AWS::IAM::Role'));
      expect(roles).not.toHaveLength(0);
      for (const role of roles) {
        expect((role as Props).DeletionPolicy).toBe('Retain');
      }
    });

    /**
     * The one exclusion, and it is the difference between recovery and a blocked redeploy. A
     * parameter left behind by a deleted stack fails the next deploy with "already exists", on a
     * resource holding nothing worth keeping.
     */
    it('does not retain the parameter naming it', () => {
      for (const parameter of Object.values(synth().findResources('AWS::SSM::Parameter'))) {
        expect((parameter as Props).DeletionPolicy).toBeUndefined();
      }
    });

    it('holds nothing that is not the memory, its role and policy, or its parameters', () => {
      // The composition rule cuts by what may be destroyed together. A DESTROY resource in here
      // would make the whole stack unsafe to delete, which is the mistake this catches.
      const types = Object.values(synth().toJSON().Resources as Props).map(
        (resource) => (resource as Props).Type as string,
      );
      expect([...new Set(types)].sort()).toStrictEqual([
        'AWS::BedrockAgentCore::Memory',
        'AWS::IAM::Policy',
        'AWS::IAM::Role',
        'AWS::SSM::Parameter',
      ]);
    });

    it('retains the role policy that lets the memory consolidate', () => {
      const policies = Object.values(synth().findResources('AWS::IAM::Policy'));
      expect(policies).toHaveLength(1);
      expect((policies[0] as Props).DeletionPolicy).toBe('Retain');
    });
  });

  describe('the three strategies section 6 names', () => {
    it('declares all of them at creation, because none can be added later', () => {
      expect(Object.keys(strategies(synth())).sort()).toStrictEqual([
        'CustomMemoryStrategy',
        'SummaryMemoryStrategy',
        'UserPreferenceMemoryStrategy',
      ]);
    });

    /**
     * The override is the fix (ADR-147): extraction and consolidation run on the contract prompts,
     * which forbid storing a measured result, under a new name and namespace.
     */
    it('declares the advertiser context as built-in semantic with both overrides', () => {
      expect(strategies(synth()).CustomMemoryStrategy).toStrictEqual({
        Name: 'advertiser_context',
        Description: CONTEXT_DESCRIPTION,
        Namespaces: ['/context/{actorId}'],
        Configuration: {
          SemanticOverride: {
            Extraction: { AppendToPrompt: EXTRACTION, ModelId: 'test.memory-model-v1:0' },
            Consolidation: { AppendToPrompt: CONSOLIDATION, ModelId: 'test.memory-model-v1:0' },
          },
        },
      });
    });

    it('renders an inference-profile model ID as the override model', () => {
      const override = strategies(synth(PROFILE_CONFIG)).CustomMemoryStrategy?.Configuration
        .SemanticOverride as Props;
      expect([override.Extraction.ModelId, override.Consolidation.ModelId]).toStrictEqual([
        'us.test.memory-model-v1:0',
        'us.test.memory-model-v1:0',
      ]);
    });

    it('retires the semantic namespace whole', () => {
      expect(JSON.stringify(synth().toJSON())).not.toContain('/semantics/');
    });

    /**
     * Per-user scoping lives here rather than in the harness. `actorId` is the Cognito subject, and
     * a namespace that did not interpolate it would consolidate every user's memory into one place
     * — putting one advertiser's preferences into another's context.
     */
    it('scopes every namespace by actor', () => {
      // Collected rather than asserted in the loop so a failure reports every offender at once,
      // and so an empty namespace list cannot pass by never entering the loop.
      const unscoped = Object.entries(strategies(synth())).flatMap(([type, strategy]) => {
        const namespaces = strategy.Namespaces as string[];
        if (namespaces.length === 0) return [`${type}: no namespace declared`];
        return namespaces
          .filter((namespace) => !namespace.includes('{actorId}'))
          .map((namespace) => `${type}: ${namespace}`);
      });

      expect(unscoped).toEqual([]);
    });

    /**
     * The asymmetry is deliberate. A summary is *of* one conversation; a stated preference and a
     * learned fact are meant to outlive the conversation that produced them, and keying those by
     * session would discard them at the end of every chat.
     */
    it('keys only the summary by session', () => {
      const byType = strategies(synth());
      expect(byType.SummaryMemoryStrategy?.Namespaces).toEqual(['/summaries/{actorId}/{sessionId}']);
      expect(byType.UserPreferenceMemoryStrategy?.Namespaces).toEqual(['/preferences/{actorId}']);
      expect(byType.CustomMemoryStrategy?.Namespaces).toEqual(['/context/{actorId}']);
    });

    it('names and describes each one', () => {
      const incomplete = Object.entries(strategies(synth()))
        .filter(([, strategy]) => !strategy.Name || !strategy.Description)
        .map(([type]) => type);

      expect(incomplete).toEqual([]);
    });
  });

  describe('retention', () => {
    it('states the expiry rather than inheriting a default', () => {
      expect(memory(synth()).EventExpiryDuration).toBe(TEST_CONFIG.agent.memoryExpirationDays);
    });

    it('stays inside the range the service accepts', () => {
      const days = memory(synth()).EventExpiryDuration as number;
      expect(days).toBeGreaterThanOrEqual(7);
      expect(days).toBeLessThanOrEqual(365);
    });
  });

  describe('naming', () => {
    it('satisfies the pattern AWS enforces', () => {
      expect(memory(synth()).Name).toMatch(MEMORY_NAME_PATTERN);
    });

    it('survives an environment name containing a hyphen', () => {
      const name = memory(synth(configWith({ envName: 'acme-prod' }))).Name;
      expect(name).toBe('adlytics_acme_prod_conversations');
      expect(name).toMatch(MEMORY_NAME_PATTERN);
    });

    it('separates environments', () => {
      expect(memoryName('dev')).not.toBe(memoryName('prod'));
    });
  });

  describe('the memory role (ADR-147 decision 3)', () => {
    const role = (template: Template): Props =>
      only(Object.values(template.findResources('AWS::IAM::Role')), 'role') as Props;
    const roleId = (template: Template): string =>
      only(Object.keys(template.findResources('AWS::IAM::Role')), 'role');
    const statements = (template: Template): Props[] =>
      only(Object.values(template.findResources('AWS::IAM::Policy')), 'policy').Properties
        .PolicyDocument.Statement as Props[];
    const partition = { Ref: 'AWS::Partition' };

    it('keeps the L2 trust: the service principal, this account, and this memory name', () => {
      const t = synth();
      expect(role(t).Properties.AssumeRolePolicyDocument.Statement).toStrictEqual([
        {
          Action: 'sts:AssumeRole',
          Condition: {
            StringEquals: { 'aws:SourceAccount': TEST_CONFIG.account },
            ArnLike: {
              'aws:SourceArn': {
                'Fn::Join': [
                  '',
                  [
                    'arn:',
                    partition,
                    `:bedrock-agentcore:${TEST_CONFIG.region}:${TEST_CONFIG.account}:memory/` +
                      `${memoryName(TEST_CONFIG.envName)}*`,
                  ],
                ],
              },
            },
          },
          Effect: 'Allow',
          Principal: { Service: 'bedrock-agentcore.amazonaws.com' },
        },
      ]);
      expect(role(t).Properties.ManagedPolicyArns).toBeUndefined();
      expect(memory(t).MemoryExecutionRoleArn).toStrictEqual({ 'Fn::GetAtt': [roleId(t), 'Arn'] });
    });

    // The ordering the L2's own grant carried: a memory created before its role can invoke the
    // override model, then retained by a rolled-back create, could never consolidate.
    it('creates the memory only after the role policy that lets it invoke the override model', () => {
      for (const t of [synth(), synth(PROFILE_CONFIG)]) {
        const policyId = only(Object.keys(t.findResources('AWS::IAM::Policy')), 'policy');
        const resource = only(Object.values(t.findResources('AWS::BedrockAgentCore::Memory')), 'memory');
        expect(resource.DependsOn).toStrictEqual([policyId]);
      }
    });

    // The complete policy, not a Sid-filtered subset: a statement the L2 or a CDK upgrade adds to
    // this role turns these red (A-IAM-MODEL-001).
    it('grants a bare model ID only as the in-Region foundation model, and nothing else', () => {
      expect(statements(synth())).toStrictEqual([
        {
          Sid: 'InvokeMemoryModelModel0',
          Effect: 'Allow',
          Action: ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream'],
          Resource: {
            'Fn::Join': [
              '',
              ['arn:', partition, `:bedrock:${TEST_CONFIG.region}::foundation-model/test.memory-model-v1:0`],
            ],
          },
        },
      ]);
    });

    it('grants a profile in this Region, and its foundation model only through that profile, and nothing else', () => {
      const profileArn = {
        'Fn::Join': [
          '',
          [
            'arn:',
            partition,
            `:bedrock:${TEST_CONFIG.region}:${TEST_CONFIG.account}:inference-profile/us.test.memory-model-v1:0`,
          ],
        ],
      };
      expect(statements(synth(PROFILE_CONFIG))).toStrictEqual([
        {
          Sid: 'InvokeMemoryModelProfile0',
          Effect: 'Allow',
          Action: ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream'],
          Resource: profileArn,
        },
        {
          Sid: 'InvokeMemoryModelModel0',
          Effect: 'Allow',
          Action: ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream'],
          Resource: {
            'Fn::Join': ['', ['arn:', partition, ':bedrock:*::foundation-model/test.memory-model-v1:0']],
          },
          Condition: { ArnEquals: { 'bedrock:InferenceProfileArn': profileArn } },
        },
      ]);
    });
  });

  describe('the strategy revision the Harness carries (ADR-147 decision 4)', () => {
    const scratch = fs.mkdtempSync(path.join(os.tmpdir(), 'memory-prompts-'));
    afterAll(() => fs.rmSync(scratch, { recursive: true, force: true }));
    const write = (name: string, text: string): string => {
      const file = path.join(scratch, name);
      fs.writeFileSync(file, text, 'utf-8');
      return file;
    };
    const revision = (template: Template): string => {
      const found = Object.values(template.findResources('AWS::SSM::Parameter', {
        Properties: { Name: paramPath(TEST_CONFIG.envName, 'memoryStrategyRevision') },
      }));
      return only(found, 'revision parameter').Properties.Value as string;
    };

    it('publishes a sha256 digest, equal for identical input', () => {
      expect(revision(synth())).toMatch(/^sha256:[0-9a-f]{64}$/);
      expect(revision(synth())).toBe(revision(synth()));
    });

    it('changes with either prompt or the model', () => {
      const base = revision(synth());
      const extraction = write('extraction.md', `${EXTRACTION}.`);
      const consolidation = write('consolidation.md', `${CONSOLIDATION}.`);
      expect(revision(synth(TEST_CONFIG, { ...MEMORY_PROMPTS, extraction }))).not.toBe(base);
      expect(revision(synth(TEST_CONFIG, { ...MEMORY_PROMPTS, consolidation }))).not.toBe(base);
      expect(revision(synth(PROFILE_CONFIG))).not.toBe(base);
    });

    it('is not retained, like the ARN parameter', () => {
      const found = Object.values(synth().findResources('AWS::SSM::Parameter', {
        Properties: { Name: paramPath(TEST_CONFIG.envName, 'memoryStrategyRevision') },
      }));
      expect((only(found, 'revision parameter') as Props).DeletionPolicy).toBeUndefined();
    });

    it('refuses an empty or over-long override prompt, naming the file', () => {
      const empty = write('empty.md', '  \n');
      const long = write('long.md', 'x'.repeat(30_001));
      // Complete messages: the over-long case is red when the L2's own refusal fires instead.
      expect(thrownMessage(() => synth(TEST_CONFIG, { ...MEMORY_PROMPTS, extraction: empty })))
        .toBe(`Memory override prompt ${empty} is empty.`);
      expect(thrownMessage(() => synth(TEST_CONFIG, { ...MEMORY_PROMPTS, consolidation: long })))
        .toBe(`Memory override prompt ${long} exceeds 30,000 characters.`);
    });
  });

  it('publishes the ARN the harness reads', () => {
    synth().hasResourceProperties('AWS::SSM::Parameter', {
      Name: paramPath(TEST_CONFIG.envName, 'memoryArn'),
    });
  });
});
