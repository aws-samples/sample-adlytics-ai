// SPDX-License-Identifier: MIT-0

import * as crypto from 'node:crypto';
import * as fs from 'node:fs';

import { paramPath } from '../lib/config';
import {
  GATEWAY_TOKEN_SCOPE,
  HARNESS_PROVIDER_AGENTCORE_ACTIONS,
  HARNESS_RESOURCE_TYPE,
} from '../lib/stacks/harness-proof-stack';
import { TEST_CONFIG } from './support/config';
import { MANAGED_CONVERSATION_PROMPT, everyTemplate, resourcesOfType } from './support/templates';

type Json = Record<string, unknown>;

/** The complete recalled-memory rule of the managed-conversation prompt, word for word (ADR-147). */
const RECALLED_MEMORY_RULE =
  'Context recalled from earlier conversations, such as remembered facts, preferences, and ' +
  'conversation summaries, tells you who the user is, what they work on, and how they like ' +
  'answers. It is context, never evidence. Never state, repeat, or round a figure that appears ' +
  'only in recalled context, or only in a tool result or answer from an earlier turn. A number ' +
  'the user typed in this conversation stays a premise; a number that appears only in recalled ' +
  'context is never a premise. Never present recalled context as the user\'s records, tracking, ' +
  'or a previous result. When the question needs such a figure, read it with a governed tool in ' +
  'this turn. When a governed tool refuses, for example because a period has not been measured ' +
  'yet, tell the user what could not be established and why, and offer the nearest measured ' +
  'alternative the refusal names as a fresh read they can ask for; never fill the gap with a ' +
  'remembered figure.';
/** The digest of the complete canonical prompt; change it only together with the prompt. */
const CANONICAL_PROMPT_DIGEST =
  'sha256:686630c614baf2d81714a57a1f500ad90903dad332407e6fafa95a449f51ab16';

const template = everyTemplate().HarnessStack as Json;
const resources = (template.Resources ?? {}) as Record<string, Json>;

function only<T>(items: T[]): T {
  expect(items).toHaveLength(1);
  const [item] = items;
  if (item === undefined) throw new Error('expected exactly one item');
  return item;
}

function logicalIdOf(predicate: (resource: Json) => boolean): string {
  return only(Object.entries(resources).filter(([, resource]) => predicate(resource)))[0];
}

function policyStatements(source: Json): Json[] {
  return resourcesOfType(source, 'AWS::IAM::Policy').flatMap((policy) =>
    (((policy.PolicyDocument as Json).Statement ?? []) as Json[]),
  );
}

function statementsOfRole(roleLogicalId: string): Json[] {
  return resourcesOfType(template, 'AWS::IAM::Policy')
    .filter((policy) => ((policy.Roles ?? []) as Json[]).some((role) => role.Ref === roleLogicalId))
    .flatMap((policy) => (((policy.PolicyDocument as Json).Statement ?? []) as Json[]));
}

/** The logical id of the SSM-parameter template parameter that reads one published path. */
function parameterFor(path: string): string {
  const parameters = (template.Parameters ?? {}) as Record<string, Json>;
  return only(Object.entries(parameters)
    .filter(([, parameter]) =>
      parameter.Type === 'AWS::SSM::Parameter::Value<String>' && parameter.Default === path)
    .map(([logicalId]) => logicalId));
}

const managedHarnessId = logicalIdOf((resource) => resource.Type === HARNESS_RESOURCE_TYPE);
const managedHarness = resources[managedHarnessId] as Json;
const providerFunctionId = logicalIdOf((resource) =>
  resource.Type === 'AWS::Lambda::Function'
  && (resource.Properties as Json).Handler === 'adapters.jobs.harness_resource.handler.handler');
const providerFunction = (resources[providerFunctionId] as Json).Properties as Json;
const executionRoleId = logicalIdOf((resource) =>
  resource.Type === 'AWS::IAM::Role'
  && JSON.stringify((resource.Properties as Json).AssumeRolePolicyDocument)
    .includes('bedrock-agentcore.amazonaws.com'));
const providerRoleId = logicalIdOf((resource) =>
  resource.Type === 'AWS::IAM::Role'
  && (resource.Properties as Json).RoleName === 'adlytics-dev-harness-provider');

/**
 * Resolve the rendered `HarnessSpec` (an `Fn::Join` of JSON text around tokens) back into the
 * structure the provider receives, with each token left in place as its template expression. A
 * string that mixes text and tokens becomes the equivalent `Fn::Join`, so every field compares
 * exactly against the intrinsic CloudFormation will substitute.
 */
function resolvedSpec(): Json {
  const rendered = (managedHarness.Properties as Json).HarnessSpec as
    | string
    | { 'Fn::Join': [string, unknown[]] };
  if (typeof rendered === 'string') return JSON.parse(rendered) as Json;
  const tokens: unknown[] = [];
  const text = rendered['Fn::Join'][1]
    .map((part) => {
      if (typeof part === 'string') return part;
      tokens.push(part);
      return `@@TOKEN${tokens.length - 1}@@`;
    })
    .join('');
  const restore = (value: unknown): unknown => {
    if (Array.isArray(value)) return value.map(restore);
    if (value !== null && typeof value === 'object') {
      return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, restore(item)]));
    }
    if (typeof value !== 'string' || !value.includes('@@TOKEN')) return value;
    const parts = value.split(/(@@TOKEN\d+@@)/u).filter((part) => part !== '')
      .map((part) => {
        const match = /^@@TOKEN(\d+)@@$/u.exec(part);
        return match ? tokens[Number(match[1])] : part;
      });
    return parts.length === 1 ? parts[0] : { 'Fn::Join': ['', parts] };
  };
  return restore(JSON.parse(text)) as Json;
}

const spec = resolvedSpec();

const agentcoreArn = (resource: string): unknown => ({
  'Fn::Join': ['', [
    'arn:',
    { Ref: 'AWS::Partition' },
    `:bedrock-agentcore:${TEST_CONFIG.region}:${TEST_CONFIG.account}:${resource}`,
  ]],
});
/** The Gateway and Memory the HarnessSpec references, as the template renders them. */
const gatewayToken = {
  'Fn::Join': ['', [
    'arn:',
    { Ref: 'AWS::Partition' },
    `:bedrock-agentcore:${TEST_CONFIG.region}:${TEST_CONFIG.account}:gateway/`,
    { Ref: parameterFor(paramPath('dev', 'gatewayId')) },
  ]],
};
const memoryToken = { Ref: parameterFor(paramPath('dev', 'memoryArn')) };
/** H-IAM-AGENTCORE-CREATE-001: exactly the standard tag set on the request. */
const REQUEST_TAGS = {
  StringEquals: {
    'aws:RequestTag/Application': 'adlytics-ai',
    'aws:RequestTag/Environment': 'dev',
    'aws:RequestTag/ManagedBy': 'cdk',
  },
  'ForAllValues:StringEquals': { 'aws:TagKeys': ['Application', 'Environment', 'ManagedBy'] },
};
const IDENTITIES = [
  agentcoreArn('workload-identity-directory/default'),
  agentcoreArn('workload-identity-directory/default/workload-identity/harness_adlytics_dev_agent-*'),
];
const CREATED_IDENTITIES = [
  agentcoreArn('workload-identity-directory/default'),
  agentcoreArn('workload-identity-directory/default/workload-identity/*'),
];
/** The ten AgentCore statements of the provider role, in order. */
const HARNESS_OWNERSHIP = [
  {
    Sid: 'CreateTheHarnessTagged',
    Effect: 'Allow',
    Action: 'bedrock-agentcore:CreateHarness',
    Resource: '*',
    Condition: REQUEST_TAGS,
  },
  {
    Sid: 'TagWithTheStandardSet',
    Effect: 'Allow',
    Action: 'bedrock-agentcore:TagResource',
    Resource: '*',
    Condition: {
      ...REQUEST_TAGS,
      StringEqualsIfExists: {
        'aws:ResourceTag/Application': 'adlytics-ai',
        'aws:ResourceTag/Environment': 'dev',
      },
    },
  },
  {
    Sid: 'CreateTheHarnessRuntimeTagged',
    Effect: 'Allow',
    Action: 'bedrock-agentcore:CreateAgentRuntime',
    Resource: '*',
    Condition: REQUEST_TAGS,
  },
  {
    Sid: 'CreateTheHarnessIdentityTagged',
    Effect: 'Allow',
    Action: 'bedrock-agentcore:CreateWorkloadIdentity',
    Resource: CREATED_IDENTITIES,
    Condition: REQUEST_TAGS,
  },
  {
    Sid: 'OwnTheHarness',
    Effect: 'Allow',
    Action: [
      'bedrock-agentcore:GetHarness',
      'bedrock-agentcore:UpdateHarness',
      'bedrock-agentcore:DeleteHarness',
      'bedrock-agentcore:ListTagsForResource',
      'bedrock-agentcore:UntagResource',
    ],
    Resource: agentcoreArn('harness/adlytics_dev_agent-*'),
  },
  {
    Sid: 'OwnTheHarnessReferences',
    Effect: 'Allow',
    Action: ['bedrock-agentcore:GetHarness', 'bedrock-agentcore:UpdateHarness', 'bedrock-agentcore:DeleteHarness'],
    Resource: [gatewayToken, memoryToken],
  },
  {
    Sid: 'CreateTheHarnessRuntimeEndpoint',
    Effect: 'Allow',
    Action: 'bedrock-agentcore:CreateAgentRuntimeEndpoint',
    Resource: agentcoreArn('runtime/*'),
  },
  {
    Sid: 'OwnTheHarnessRuntime',
    Effect: 'Allow',
    Action: [
      'bedrock-agentcore:GetAgentRuntime',
      'bedrock-agentcore:UpdateAgentRuntime',
      'bedrock-agentcore:DeleteAgentRuntime',
      'bedrock-agentcore:GetAgentRuntimeEndpoint',
      'bedrock-agentcore:UpdateAgentRuntimeEndpoint',
      'bedrock-agentcore:DeleteAgentRuntimeEndpoint',
    ],
    Resource: [
      agentcoreArn('runtime/harness_adlytics_dev_agent-*'),
      agentcoreArn('runtime/harness_adlytics_dev_agent-*/runtime-endpoint/*'),
    ],
  },
  {
    Sid: 'OwnTheHarnessIdentity',
    Effect: 'Allow',
    Action: 'bedrock-agentcore:DeleteWorkloadIdentity',
    Resource: IDENTITIES,
  },
  {
    Sid: 'ReadTheConversationMemory',
    Effect: 'Allow',
    Action: 'bedrock-agentcore:GetMemory',
    Resource: memoryToken,
  },
];

describe('HarnessStack', () => {
  it('owns the Harness through one custom resource and no CfnHarness', () => {
    expect(resourcesOfType(template, 'AWS::BedrockAgentCore::Harness')).toStrictEqual([]);
    expect(Object.values(resources).filter((resource) => resource.Type === HARNESS_RESOURCE_TYPE))
      .toHaveLength(1);
    expect(managedHarness.DeletionPolicy).toBe('Delete');
    expect(managedHarness.UpdateReplacePolicy).toBe('Delete');
    // Every property is one of the five the provider accepts; the spec travels as one string.
    expect(Object.keys(managedHarness.Properties as Json).sort()).toStrictEqual([
      'HarnessSpec',
      'MemoryStrategyRevision',
      'RuntimeLogRetentionDays',
      'ServiceTimeout',
      'ServiceToken',
    ]);
    // A changed strategy digest sends an Update, so UpdateHarness re-derives retrieval (ADR-147).
    expect((managedHarness.Properties as Json).MemoryStrategyRevision)
      .toStrictEqual({ Ref: parameterFor(paramPath('dev', 'memoryStrategyRevision')) });
    expect(JSON.stringify(spec)).not.toContain('memoryStrategyRevision');
    expect(JSON.stringify(spec)).not.toContain('MemoryStrategyRevision');
    // The runtime log group's retention comes from typed config, as text.
    expect((managedHarness.Properties as Json).RuntimeLogRetentionDays)
      .toBe(String(TEST_CONFIG.harnessRuntimeLogRetentionDays));
    expect((managedHarness.Properties as Json).ServiceToken)
      .toStrictEqual({ 'Fn::GetAtt': [providerFunctionId, 'Arn'] });
    // Above the provider's 900 s timeout, below CloudFormation's default hour.
    expect((managedHarness.Properties as Json).ServiceTimeout).toBe('1200');
    // The execution role and its grants exist before the Harness and outlive it.
    expect(managedHarness.DependsOn).toEqual(expect.arrayContaining([executionRoleId]));
  });

  it('declares every CfnHarness field in the API shape, under the new name', () => {
    expect(Object.keys(spec).sort()).toStrictEqual([
      'allowedTools',
      'authorizerConfiguration',
      'executionRoleArn',
      'harnessName',
      'maxIterations',
      'maxTokens',
      'memory',
      'model',
      'systemPrompt',
      'tags',
      'timeoutSeconds',
      'tools',
    ]);
    // Not the retired CfnHarness's `adlytics_dev_harness`, which is deleted in the same deploy.
    expect(spec.harnessName).toBe('adlytics_dev_agent');
    expect(spec.executionRoleArn).toStrictEqual({ 'Fn::GetAtt': [executionRoleId, 'Arn'] });
    expect(spec.model).toStrictEqual({
      bedrockModelConfig: {
        modelId: 'test.deep-dive-model-v1:0',
        maxTokens: 16384,
        temperature: 0,
        apiFormat: 'converse_stream',
      },
    });
    expect(spec.maxIterations).toBe(24);
    expect(spec.maxTokens).toBe(16384);
    expect(spec.timeoutSeconds).toBe(TEST_CONFIG.agent.deepDive.timeoutSeconds);
    expect(spec.tags).toStrictEqual({
      Application: 'adlytics-ai',
      Environment: 'dev',
      ManagedBy: 'cdk',
    });
  });

  it('closes the built-in tools by allowing exactly the generated Gateway tools', () => {
    expect(spec.allowedTools).toStrictEqual([
      '@adlytics/catalog___catalog_list_metrics',
      '@adlytics/catalog___catalog_describe_metric',
      '@adlytics/measurement___measurement_metric_by_grain',
      '@adlytics/measurement___measurement_compare_windows',
      '@adlytics/measurement___measurement_top_n',
    ]);
    // ADR-083: the answer streams as text, so the retired finalizer is no tool the Harness allows.
    expect(JSON.stringify(spec.allowedTools)).not.toContain('presentation');
  });

  it('admits only a token the token service minted for the route (ADR-077 §2, amended)', () => {
    /**
     * SigV4 callers are refused only while this authorizer is declared, and a signed-in user's
     * Cognito token is refused only because the discovery URL names the token service and the
     * audience is the Harness audience. Compared exactly: a Cognito discovery URL, a dropped
     * audience, or an added client each fails here.
     */
    expect(spec.authorizerConfiguration).toStrictEqual({
      customJWTAuthorizer: {
        discoveryUrl: {
          'Fn::Join': ['', [
            { Ref: parameterFor(paramPath('dev', 'tokenIssuerUrl')) },
            '/.well-known/openid-configuration',
          ]],
        },
        allowedAudience: ['adlytics-dev-harness'],
        allowedClients: ['adlytics-dev-conversation-route'],
      },
    });
  });

  it('reaches the Gateway only with the token it exchanges at the token service (ADR-077 §4)', () => {
    expect(GATEWAY_TOKEN_SCOPE).toBe('adlytics-gateway');
    expect(spec.tools).toStrictEqual([{
      type: 'agentcore_gateway',
      name: 'adlytics',
      config: {
        agentCoreGateway: {
          gatewayArn: {
            'Fn::Join': ['', [
              'arn:',
              { Ref: 'AWS::Partition' },
              `:bedrock-agentcore:${TEST_CONFIG.region}:${TEST_CONFIG.account}:gateway/`,
              { Ref: parameterFor(paramPath('dev', 'gatewayId')) },
            ]],
          },
          outboundAuth: {
            oauth: {
              providerArn: { Ref: parameterFor(paramPath('dev', 'tokenCredentialProviderArn')) },
              scopes: ['adlytics-gateway'],
              grantType: 'TOKEN_EXCHANGE',
            },
          },
        },
      },
    }]);
  });

  it('uses the retained Memory by ARN and never creates managed memory', () => {
    expect(spec.memory).toStrictEqual({
      agentCoreMemoryConfiguration: { arn: { Ref: parameterFor(paramPath('dev', 'memoryArn')) } },
    });
    expect(JSON.stringify(managedHarness)).not.toContain('managedMemoryConfiguration');
  });

  it('no longer reads the Cognito pool, so a Cognito token has no path to the authorizer', () => {
    const defaults = Object.values((template.Parameters ?? {}) as Record<string, Json>)
      .map((parameter) => parameter.Default);
    expect(defaults).not.toContain(paramPath('dev', 'userPoolId'));
    expect(defaults).not.toContain(paramPath('dev', 'userPoolClientId'));
    expect(JSON.stringify(template)).not.toContain('cognito-idp');
  });

  it('renders the canonical prompt exactly, carrying the recalled-memory rule once (ADR-147)', () => {
    const canonical = fs.readFileSync(MANAGED_CONVERSATION_PROMPT, 'utf-8').trim();
    // The Harness carries the contract unchanged: one block, byte for byte.
    expect(spec.systemPrompt).toStrictEqual([{ text: canonical }]);
    // The complete rule appears exactly once: a changed or duplicated rule is red here.
    expect(canonical.split('\n\n').filter((paragraph) => paragraph === RECALLED_MEMORY_RULE))
      .toHaveLength(1);
    // A rule added anywhere else, contradictory or not, is red here until this pin is updated
    // deliberately with the prompt.
    expect(`sha256:${crypto.createHash('sha256').update(canonical).digest('hex')}`)
      .toBe(CANONICAL_PROMPT_DIGEST);
  });

  it('requires governed tools then streams an agent-authored Markdown answer (ADR-083)', () => {
    const prompt = JSON.stringify(spec.systemPrompt);

    expect((spec.systemPrompt as Json[]).map((block) => Object.keys(block)))
      .toStrictEqual([['text']]);
    expect(prompt).toContain('The user reads your answer as you write it');
    expect(prompt).toContain(
      'Use the governed catalog or measurement tools for every claim about this platform',
    );
    expect(prompt).toContain('write a concise natural-language answer');
    expect(prompt).toContain('You own the wording, explanation, transitions, and presentation');
    expect(prompt).toContain('Use customer-readable labels rather than raw internal identifiers');
    expect(prompt).toContain(
      'never show rendering IDs, collection IDs, grounding IDs, or correlation IDs',
    );
    expect(prompt).toContain('Do not narrate between tool calls, and do not think out loud');
    expect(prompt).toContain('Format the answer in Markdown');
    expect(prompt).toContain('Do not use HTML, images, or code blocks for anything but code');
    expect(prompt).toContain('End the turn when the answer is complete');
    expect(prompt).toContain(
      'Every rendering listed in mandatory_rendering_ids states something the answer must tell the user',
    );
    expect(prompt).toContain(
      'Never state a figure from outside this turn\'s tool results, from general knowledge, or from memory',
    );
    expect(prompt).toContain(
      'figures you validly derive from them: differences, percentage changes, shares, comparisons, rankings, and projections labelled as projections',
    );
    expect(prompt).toContain(
      'never combines figures of incompatible basis, grain, window, or currency',
    );
    expect(prompt).not.toContain('numeric literal');
    // The retired finalizer and its plan vocabulary are gone from the prompt entirely.
    for (const retired of [
      'presentation_publish',
      'PresentationPlan',
      'free_paragraph',
      'grounded_paragraph',
      'qualifier_rendering_ids',
    ]) {
      expect(prompt).not.toContain(retired);
    }
    expect(prompt).not.toContain('receipt_token');
    expect(prompt).not.toContain('evidence_receipts');
    expect(prompt).not.toContain('exact grounded renderings');
    expect(prompt).not.toContain('custody digest');
  });

  it('names the execution role, because the agent client secret admits it by ARN', () => {
    expect(((resources[executionRoleId] as Json).Properties as Json).RoleName)
      .toBe('adlytics-dev-harness-execution');
  });

  it('grants the execution role exact model and Memory access, no Gateway IAM, and denies commands', () => {
    const statements = statementsOfRole(executionRoleId);
    const allow = statements.filter((statement) => statement.Effect === 'Allow');
    // ADR-077 §4: the Gateway is CUSTOM_JWT, so an IAM invoke grant would be unused reach.
    expect(JSON.stringify(statements)).not.toContain('InvokeGateway');

    // A bare model ID (the test fixture's form) is the foundation model in this Region only.
    const models = only(allow.filter((statement) => statement.Sid === 'InvokeConfiguredModelsModel0'));
    expect(models.Action).toEqual(['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream']);
    expect(JSON.stringify(models.Resource)).not.toContain('test.quick-chat-model-v1:0');
    expect(JSON.stringify(models.Resource))
      .toContain(`:${TEST_CONFIG.region}::foundation-model/test.deep-dive-model-v1:0`);
    expect(JSON.stringify(models.Resource)).not.toContain('inference-profile');
    expect(allow.filter((statement) => String(statement.Sid).startsWith('InvokeConfiguredModels')))
      .toHaveLength(1);

    const memory = only(allow.filter((statement) => statement.Sid === 'UseConversationMemory'));
    expect(memory.Resource).toStrictEqual({ Ref: parameterFor(paramPath('dev', 'memoryArn')) });
    expect(memory.Action).toEqual([
      'bedrock-agentcore:CreateEvent',
      'bedrock-agentcore:DeleteEvent',
      'bedrock-agentcore:GetEvent',
      'bedrock-agentcore:ListEvents',
      'bedrock-agentcore:RetrieveMemoryRecords',
    ]);

    const deny = statements.filter((statement) => statement.Effect === 'Deny');
    expect(JSON.stringify(deny)).toContain('bedrock-agentcore:InvokeAgentRuntimeCommand');
    expect(JSON.stringify(deny)).toContain('bedrock-agentcore:InvokeAgentRuntimeCommandShell');
  });

  it('may exchange for a Gateway token through exactly the token-service provider (ADR-077 S4)', () => {
    /**
     * Compared exactly: another credential provider, a wildcard over the vault, a workload
     * identity outside this Harness, or the secret read widened past its one ARN each fails here.
     */
    const arn = (resource: string): unknown => ({
      'Fn::Join': ['', [
        'arn:',
        { Ref: 'AWS::Partition' },
        `:bedrock-agentcore:${TEST_CONFIG.region}:${TEST_CONFIG.account}:${resource}`,
      ]],
    });
    const statements = statementsOfRole(executionRoleId);

    expect(only(statements.filter((statement) => statement.Sid === 'ExchangeForGatewayToken')))
      .toStrictEqual({
        Sid: 'ExchangeForGatewayToken',
        Effect: 'Allow',
        Action: 'bedrock-agentcore:GetResourceOauth2Token',
        Resource: [
          arn('token-vault/default'),
          arn('token-vault/default/oauth2credentialprovider/adlytics-dev-token-service'),
          arn('workload-identity-directory/default'),
          arn('workload-identity-directory/default/workload-identity/harness_adlytics_dev_agent-*'),
        ],
      });
    expect(only(statements.filter((statement) => statement.Sid === 'HarnessWorkloadIdentity'))
      .Resource).toStrictEqual([
      arn('workload-identity-directory/default'),
      arn('workload-identity-directory/default/workload-identity/harness_adlytics_dev_agent-*'),
    ]);
    expect(only(statements.filter((statement) => statement.Sid === 'ReadAgentClientSecret')))
      .toStrictEqual({
        Sid: 'ReadAgentClientSecret',
        Effect: 'Allow',
        Action: 'secretsmanager:GetSecretValue',
        Resource: { Ref: parameterFor(paramPath('dev', 'agentClientSecretArn')) },
      });
    // Nothing else in the stack touches the vault or Secrets Manager.
    const touching = (needle: string): unknown[] => policyStatements(template)
      .filter((statement) => JSON.stringify(statement.Action).includes(needle))
      .map((statement) => statement.Sid);
    expect(touching('GetResourceOauth2Token')).toStrictEqual(['ExchangeForGatewayToken']);
    expect(touching('secretsmanager:')).toStrictEqual(['ReadAgentClientSecret']);
  });

  it('gives the provider exactly the CloudFormation Harness handler permissions, scoped', () => {
    /**
     * Compared exactly: an added action, a wildcard resource, a PassRole widened past the one
     * execution role or to another service, or an unconditioned service-linked role each fails.
     */
    const logGroupId = logicalIdOf((resource) =>
      resource.Type === 'AWS::Logs::LogGroup'
      && (resource.Properties as Json).LogGroupName === '/aws/lambda/adlytics-dev-HarnessProvider');
    expect(statementsOfRole(providerRoleId)).toStrictEqual([
      {
        Sid: 'WriteProviderLogs',
        Effect: 'Allow',
        Action: ['logs:CreateLogStream', 'logs:PutLogEvents'],
        Resource: { 'Fn::Join': ['', [{ 'Fn::GetAtt': [logGroupId, 'Arn'] }, ':*']] },
      },
      {
        Sid: 'RetainHarnessRuntimeLogs',
        Effect: 'Allow',
        Action: ['logs:CreateLogGroup', 'logs:PutRetentionPolicy', 'logs:TagResource'],
        Resource: {
          'Fn::Join': ['', [
            'arn:',
            { Ref: 'AWS::Partition' },
            `:logs:${TEST_CONFIG.region}:${TEST_CONFIG.account}:log-group:/aws/bedrock-agentcore/runtimes/harness_adlytics_dev_agent-*`,
          ]],
        },
      },
      {
        Sid: 'ReadBackHarnessRuntimeLogs',
        Effect: 'Allow',
        Action: 'logs:DescribeLogGroups',
        Resource: {
          'Fn::Join': ['', [
            'arn:',
            { Ref: 'AWS::Partition' },
            `:logs:${TEST_CONFIG.region}:${TEST_CONFIG.account}:log-group:*`,
          ]],
        },
      },
      ...HARNESS_OWNERSHIP,
      {
        Sid: 'PassHarnessExecutionRole',
        Effect: 'Allow',
        Action: 'iam:PassRole',
        Resource: { 'Fn::GetAtt': [executionRoleId, 'Arn'] },
        Condition: { StringEquals: { 'iam:PassedToService': 'bedrock-agentcore.amazonaws.com' } },
      },
      {
        Sid: 'CreateRuntimeIdentityServiceLinkedRole',
        Effect: 'Allow',
        Action: 'iam:CreateServiceLinkedRole',
        Resource: {
          'Fn::Join': ['', [
            'arn:',
            { Ref: 'AWS::Partition' },
            `:iam::${TEST_CONFIG.account}:role/aws-service-role/runtime-identity.bedrock-agentcore.amazonaws.com/*`,
          ]],
        },
        Condition: {
          StringEquals: { 'iam:AWSServiceName': 'runtime-identity.bedrock-agentcore.amazonaws.com' },
        },
      },
      {
        Effect: 'Allow',
        Action: ['xray:PutTraceSegments', 'xray:PutTelemetryRecords'],
        Resource: '*',
      },
    ]);
    // The union of the ten statements' actions is the declared set. Each action appears once,
    // except GetHarness, UpdateHarness and DeleteHarness, which appear exactly in OwnTheHarness
    // and OwnTheHarnessReferences.
    const agentcore = statementsOfRole(providerRoleId).filter((statement) =>
      [statement.Action].flat().every((entry) => String(entry).startsWith('bedrock-agentcore:')));
    const actions = agentcore.flatMap((statement) => [statement.Action].flat() as string[]);
    expect([...new Set(actions)].sort()).toStrictEqual([...HARNESS_PROVIDER_AGENTCORE_ACTIONS].sort());
    const holders = (name: string): unknown[] => agentcore
      .filter((statement) => ([statement.Action].flat() as string[]).includes(name))
      .map((statement) => statement.Sid);
    for (const name of HARNESS_PROVIDER_AGENTCORE_ACTIONS) {
      expect(holders(name)).toStrictEqual(
        ['bedrock-agentcore:GetHarness', 'bedrock-agentcore:UpdateHarness', 'bedrock-agentcore:DeleteHarness']
          .includes(name) ? ['OwnTheHarness', 'OwnTheHarnessReferences'] : [expect.any(String)],
      );
    }
    expect(HARNESS_PROVIDER_AGENTCORE_ACTIONS).toHaveLength(18);
    // Managed memory is refused by this resource, so the role cannot create or change one.
    for (const refused of ['CreateMemory', 'UpdateMemory', 'DeleteMemory']) {
      expect(actions).not.toContain(`bedrock-agentcore:${refused}`);
    }
    const role = (resources[providerRoleId] as Json).Properties as Json;
    expect(role.ManagedPolicyArns).toBeUndefined();
    expect(role.Policies).toBeUndefined();
    expect(JSON.stringify(role.AssumeRolePolicyDocument)).toContain('lambda.amazonaws.com');
    expect(JSON.stringify(role.AssumeRolePolicyDocument)).not.toContain('bedrock-agentcore');
  });

  it('bounds every AgentCore create on * by the exact tag set, and names nothing account-wide', () => {
    /**
     * H-IAM-AGENTCORE-CREATE-001. Refused: a fourth `*` statement, an unconditioned `*`, or any
     * AgentCore resource that ends in `:*` (the account-wide grant this replaces). The X-Ray
     * statement grantSpanExport adds is outside the AgentCore set and keeps its `*`.
     */
    const agentcore = statementsOfRole(providerRoleId).filter((statement) =>
      [statement.Action].flat().every((entry) => String(entry).startsWith('bedrock-agentcore:')));
    const onStar = agentcore.filter((statement) => statement.Resource === '*');
    expect(onStar.map((statement) => [statement.Sid, statement.Condition])).toStrictEqual([
      ['CreateTheHarnessTagged', REQUEST_TAGS],
      ['TagWithTheStandardSet', {
        ...REQUEST_TAGS,
        StringEqualsIfExists: {
          'aws:ResourceTag/Application': 'adlytics-ai',
          'aws:ResourceTag/Environment': 'dev',
        },
      }],
      ['CreateTheHarnessRuntimeTagged', REQUEST_TAGS],
    ]);
    // The identity create names exactly the directory and the unnamed identity ARN it presents.
    const identity = only(agentcore.filter((statement) => statement.Sid === 'CreateTheHarnessIdentityTagged'));
    expect(identity.Resource).toStrictEqual(CREATED_IDENTITIES);
    expect(identity.Condition).toStrictEqual(REQUEST_TAGS);
    // The only unnamed-ARN grant without a tag condition is the runtime's endpoint create.
    const untagged = agentcore.filter((statement) => statement.Condition === undefined
      && JSON.stringify(statement.Resource).match(/runtime\/\*"|workload-identity\/\*"/));
    expect(untagged.map((statement) => [statement.Sid, statement.Action]))
      .toStrictEqual([['CreateTheHarnessRuntimeEndpoint', 'bedrock-agentcore:CreateAgentRuntimeEndpoint']]);
    const references = only(agentcore.filter((statement) => statement.Sid === 'OwnTheHarnessReferences'));
    // Exactly the two ARNs the HarnessSpec references, and only the three Harness actions.
    expect(references.Resource).toStrictEqual([
      ((((spec.tools as Json[])[0] as Json).config as Json).agentCoreGateway as Json).gatewayArn,
      ((spec.memory as Json).agentCoreMemoryConfiguration as Json).arn,
    ]);
    expect(references.Action).toStrictEqual([
      'bedrock-agentcore:GetHarness',
      'bedrock-agentcore:UpdateHarness',
      'bedrock-agentcore:DeleteHarness',
    ]);
    for (const statement of agentcore) {
      for (const resource of [statement.Resource].flat()) {
        expect(JSON.stringify(resource)).not.toMatch(/:bedrock-agentcore:[^"]*:\d{12}:\*"/);
      }
    }
  });

  it('lets only the deployment invoke the provider, before the Harness first runs', () => {
    const policies = resourcesOfType(template, 'AWS::Lambda::ResourcePolicy');
    expect(policies).toHaveLength(1);
    const functionArn = { 'Fn::GetAtt': [providerFunctionId, 'Arn'] };
    expect(policies[0]).toStrictEqual({
      ResourceArn: functionArn,
      PolicyDocument: {
        Version: '2012-10-17',
        Statement: [{
          Sid: 'OnlyTheDeployment',
          Effect: 'Deny',
          Principal: '*',
          Action: ['lambda:InvokeFunction', 'lambda:InvokeAsync'],
          Resource: functionArn,
          Condition: {
            ArnNotEquals: {
              'aws:PrincipalArn': {
                'Fn::Sub': `arn:\${AWS::Partition}:iam::${TEST_CONFIG.account}:role/cdk-hnb659fds-cfn-exec-role-${TEST_CONFIG.account}-${TEST_CONFIG.region}`,
              },
            },
            StringNotEquals: { 'aws:PrincipalServiceName': 'lambda.amazonaws.com' },
          },
        }],
      },
    });
    // No second writer of the same policy.
    expect(resourcesOfType(template, 'AWS::Lambda::Permission')).toStrictEqual([]);
    const policyId = logicalIdOf((resource) => resource.Type === 'AWS::Lambda::ResourcePolicy');
    expect(managedHarness.DependsOn).toEqual(expect.arrayContaining([policyId]));
  });

  it('runs the provider as an arm64 Python function with a 900 s timeout and its own log group', () => {
    expect(providerFunction.Architectures).toStrictEqual(['arm64']);
    expect(providerFunction.Timeout).toBe(900);
    expect(providerFunction.MemorySize).toBe(256);
    expect(providerFunction.Runtime).toBe(TEST_CONFIG.pythonRuntime);
    expect(providerFunction.Role).toStrictEqual({ 'Fn::GetAtt': [providerRoleId, 'Arn'] });
    expect(providerFunction.FunctionName).toBe('adlytics-dev-HarnessProvider');
    expect(providerFunction.Layers).toStrictEqual([
      { Ref: parameterFor(paramPath('dev', 'dependenciesLayerArn')) },
    ]);
    expect(JSON.stringify(providerFunction.LoggingConfig)).toContain('HarnessProviderLogs');
    // Nothing in the stack is an x86 function (no cr.Provider framework, no logRetention singleton).
    for (const fn of resourcesOfType(template, 'AWS::Lambda::Function')) {
      expect(fn.Architectures).toStrictEqual(['arm64']);
    }
    expect(resourcesOfType(template, 'AWS::Lambda::Function')).toHaveLength(1);
  });

  it('publishes the Harness identity from the custom resource and the prompt digest', () => {
    const parameters = resourcesOfType(template, 'AWS::SSM::Parameter');
    const parameter = only(parameters.filter(
      (candidate) => candidate.Name === paramPath('dev', 'harnessArn'),
    ));

    expect(parameter.Value).toStrictEqual({ 'Fn::GetAtt': [managedHarnessId, 'Arn'] });
    const digest = only(parameters.filter(
      (candidate) => candidate.Name === paramPath('dev', 'harnessPromptDigest'),
    ));
    expect(digest.Value).toMatch(/^sha256:[0-9a-f]{64}$/u);
  });
});

/**
 * H-EVALUATION-RESULT-001: no application role may open a log group to X-Ray. The Harness
 * execution role held `logs:PutResourcePolicy` on `*`, so it could have let X-Ray write the
 * evaluation result group. The stack makes no Logs resource policy either: with
 * ADLYTICS_TRANSACTION_SEARCH on, the runtime's spans reach `aws/spans` through
 * ObservabilityStack's XRaySpanDelivery, so a grant here would be unused account-wide surface.
 */
function unexpectedActions(actual: readonly string[], pinned: readonly string[]): string[] {
  const extra = [...new Set(actual)].filter((action) => !pinned.includes(action)).sort();
  const missing = pinned.filter((action) => !actual.includes(action)).map((a) => `missing ${a}`);
  return [...extra, ...missing];
}

/** The execution role's complete Allow set. Any change reopens the analysis above. */
const HARNESS_EXECUTION_ACTIONS = [
  'bedrock:InvokeModel',
  'bedrock:InvokeModelWithResponseStream',
  'bedrock-agentcore:CreateEvent',
  'bedrock-agentcore:DeleteEvent',
  'bedrock-agentcore:GetEvent',
  'bedrock-agentcore:ListEvents',
  'bedrock-agentcore:RetrieveMemoryRecords',
  'bedrock-agentcore:GetWorkloadAccessToken',
  'bedrock-agentcore:GetWorkloadAccessTokenForJWT',
  'bedrock-agentcore:GetResourceOauth2Token',
  'ecr-public:GetAuthorizationToken',
  'sts:GetServiceBearerToken',
  'xray:GetSamplingRules',
  'xray:GetSamplingTargets',
  'xray:PutTelemetryRecords',
  'xray:PutTraceSegments',
  'logs:CreateLogGroup',
  'logs:DescribeLogStreams',
  'logs:CreateLogStream',
  'logs:PutLogEvents',
  'logs:DescribeLogGroups',
  'cloudwatch:PutMetricData',
  'secretsmanager:GetSecretValue',
] as const;

/** Return the logical ID of every Logs resource policy a template declares. */
function logsResourcePolicies(rendered: Json): string[] {
  const declared = (rendered.Resources ?? {}) as Record<string, Json>;
  return Object.entries(declared)
    .filter(([, resource]) => resource.Type === 'AWS::Logs::ResourcePolicy')
    .map(([logicalId]) => logicalId);
}

describe('HarnessStack log delivery (H-EVALUATION-RESULT-001)', () => {
  const allowedActions = statementsOfRole(executionRoleId)
    .filter((statement) => statement.Effect === 'Allow')
    .flatMap((statement) => (Array.isArray(statement.Action) ? statement.Action : [statement.Action]))
    .filter((action): action is string => typeof action === 'string');

  it('refuses an execution role that can open a log group to X-Ray', () => {
    expect(unexpectedActions(
      [...HARNESS_EXECUTION_ACTIONS, 'logs:PutResourcePolicy'],
      HARNESS_EXECUTION_ACTIONS,
    )).toEqual(['logs:PutResourcePolicy']);
  });

  it('accepts the pinned action set against itself', () => {
    expect(unexpectedActions(HARNESS_EXECUTION_ACTIONS, HARNESS_EXECUTION_ACTIONS)).toEqual([]);
  });

  it('grants the rendered execution role no action beyond the pinned set', () => {
    expect(unexpectedActions(allowedActions, HARNESS_EXECUTION_ACTIONS)).toEqual([]);
  });

  it('refuses a template that declares a Logs resource policy', () => {
    const withPolicy: Json = {
      Resources: {
        FictionalDelivery: { Type: 'AWS::Logs::ResourcePolicy', Properties: {} },
        FictionalGroup: { Type: 'AWS::Logs::LogGroup', Properties: {} },
      },
    };
    expect(logsResourcePolicies(withPolicy)).toEqual(['FictionalDelivery']);
  });

  it('declares no Logs resource policy, so X-Ray gets no grant on the runtime groups', () => {
    expect(logsResourcePolicies(template)).toEqual([]);
  });
});
