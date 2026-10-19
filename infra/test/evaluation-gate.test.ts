// SPDX-License-Identifier: MIT-0
/**
 * ADR-079: the evaluation enablement gate the evaluation stack declares (V4, the only family).
 *
 * The GateSpec member sets are read from `src/domain/evaluation/gate.py` itself rather than
 * restated, so a member added on either side fails here instead of at the provider's first run.
 */
import * as crypto from 'node:crypto';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';

import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';

import {
  AppConfig,
  evaluationExecutionRoleName,
  evaluationGateProviderFunctionName,
  evaluationGateProviderRoleName,
  evaluationReleaseParameterName,
  evaluationResultIngressV3FunctionName,
  evaluationResultSubscriptionName,
  onlineEvaluationGateName,
  paramPath,
} from '../lib/config';
import {
  EVALUATION_GATE_RESOURCE_TYPE,
  EVALUATOR_NAME_PATTERN,
  evaluatorIdentity,
  GATE_SPEC_SCHEMA,
  loadCalibration,
} from '../lib/constructs/agent-quality/evaluation-gate';
import { EvaluationV4Stack } from '../lib/stacks/evaluation-v4-stack';
import { configWith, TEST_CONFIG } from './support/config';
import { roleGrants, roleLogicalId } from './support/iam';
import { EVALUATION_CORPORA } from './support/templates';

const REPO = path.join(__dirname, '..', '..');
const PROMPTS = path.join(REPO, 'contracts', 'prompts', 'v1');
const V4_PROMPTS = {
  groundedCorrectness: path.join(PROMPTS, 'grounded-response-correctness.md'),
  internalConsistency: path.join(PROMPTS, 'response-internal-consistency.md'),
  helpfulness: path.join(PROMPTS, 'released-response-helpfulness-v4.md'),
};
const ENV = TEST_CONFIG.envName;
const QUICK_MODEL = 'test.quick-chat-model-v1:0';
const DEEP_MODEL = 'test.deep-dive-model-v1:0';
/** The revision recomputed here from its definition: sha256(instructions + "\n" + model), 8 hex. */
const revisionOf = (promptFile: string, modelId: string): string =>
  crypto.createHash('sha256').update(`${fs.readFileSync(promptFile, 'utf-8')}\n${modelId}`, 'utf-8').digest('hex').slice(0, 8);
/** Each V4 evaluator: its corpus dimension, name segment, prompt, and model. */
const V4_EVALUATORS = [
  ['grounded_correctness', 'grounded_correctness_v4', V4_PROMPTS.groundedCorrectness, DEEP_MODEL],
  ['internal_consistency', 'internal_consistency_v4', V4_PROMPTS.internalConsistency, DEEP_MODEL],
  ['helpfulness', 'helpfulness_v4', V4_PROMPTS.helpfulness, QUICK_MODEL],
] as const;
const v4Name = (segment: string, prompt: string, model: string): string =>
  `adlytics_dev_${segment}_${revisionOf(prompt, model)}`;
/** The SSM parameter the family's result group name is read from, as the template renders its Ref. */
const GROUP_PARAMETER = paramPath(ENV, 'evaluationV4ResultLogGroupName');

/** A `frozenset({...})` literal of `gate.py`, by its assignment name. */
function gateMembers(name: string): string[] {
  const source = fs.readFileSync(path.join(REPO, 'src', 'domain', 'evaluation', 'gate.py'), 'utf-8');
  const match = new RegExp(`^${name}: Final = frozenset\\(\\s*\\{([^}]*)\\}`, 'm').exec(source);
  if (match === null) throw new Error(`gate.py has no ${name} literal`);
  return [...match[1]!.matchAll(/"([a-z_]+)"/g)].map((member) => member[1]!).sort();
}

function gateConfig(options: { v4Requested?: boolean } = {}): AppConfig {
  return configWith({
    transactionSearchEnabled: true,
    evaluationInput: { provisioned: true },
    onlineEvaluation: { provisioned: true, samplingPercentage: 100, sessionTimeoutMinutes: 1 },
    onlineEvaluationV4: { executionRequested: options.v4Requested ?? false },
  });
}

function synthV4(config: AppConfig = gateConfig()): Record<string, unknown> {
  const stack = new EvaluationV4Stack(new cdk.App(), 'Adlytics-EvaluationV4-test', {
    config,
    env: { account: config.account, region: config.region },
    promptPaths: V4_PROMPTS,
    calibrationCorpusPath: EVALUATION_CORPORA.v4,
    sourceAssetPath: __dirname,
  });
  return Template.fromStack(stack).toJSON();
}

type Resource = { Type: string; Properties: Record<string, unknown>; DependsOn?: string[]; DeletionPolicy?: string };
const resources = (template: Record<string, unknown>): Record<string, Resource> =>
  template.Resources as Record<string, Resource>;
const ofType = (template: Record<string, unknown>, type: string): [string, Resource][] =>
  Object.entries(resources(template)).filter(([, resource]) => resource.Type === type);

/** A deploy-time token rendered as a readable placeholder, so the spec text parses as JSON. */
function placeholder(token: unknown): string {
  const record = token as Record<string, unknown>;
  if (typeof record.Ref === 'string') return `{{Ref:${record.Ref}}}`;
  const getAtt = record['Fn::GetAtt'];
  if (Array.isArray(getAtt)) return `{{GetAtt:${getAtt.join('.')}}}`;
  throw new Error(`unexpected token ${JSON.stringify(token)}`);
}

function render(value: unknown): string {
  if (typeof value === 'string') return value;
  const join = (value as Record<string, unknown>)['Fn::Join'] as [string, unknown[]];
  return join[1].map((part) => (typeof part === 'string' ? part : placeholder(part))).join(join[0]);
}

/** The SSM path a rendered `{{Ref:<parameter>}}` placeholder reads. */
function parameterPath(template: Record<string, unknown>, rendered: string): string | undefined {
  const match = /^\{\{Ref:([A-Za-z0-9]+)\}\}$/.exec(rendered);
  const parameters = (template.Parameters ?? {}) as Record<string, { Default?: string }>;
  return match === null ? undefined : parameters[match[1]!]?.Default;
}
function gate(template: Record<string, unknown>): { id: string; resource: Resource; spec: Record<string, unknown> } {
  const found = ofType(template, EVALUATION_GATE_RESOURCE_TYPE);
  expect(found).toHaveLength(1);
  const [id, resource] = found[0]!;
  return { id, resource, spec: JSON.parse(render(resource.Properties.GateSpec)) as Record<string, unknown> };
}

const sha256 = (file: string): string =>
  crypto.createHash('sha256').update(fs.readFileSync(file, 'utf-8'), 'utf-8').digest('hex');

describe('the evaluation gate spec (ADR-079)', () => {
  it('carries exactly the members gate.py parses, at every level', () => {
    const { spec } = gate(synthV4());
    expect(Object.keys(spec).sort())
      .toEqual([...gateMembers('_SPEC_MEMBERS'), ...gateMembers('_OPTIONAL_SPEC_MEMBERS')].sort());
    // The create carries exactly the standard tag set the provider role's grant requires.
    expect(spec.tags).toStrictEqual({ Application: 'adlytics-ai', Environment: ENV, ManagedBy: 'cdk' });
    for (const evaluator of spec.evaluators as Record<string, unknown>[]) {
      expect(Object.keys(evaluator).sort()).toEqual(gateMembers('_EVALUATOR_MEMBERS'));
    }
    for (const entry of spec.calibration as Record<string, unknown>[]) {
      expect(Object.keys(entry).sort()).toEqual(gateMembers('_CASE_MEMBERS'));
    }
  });

  it('renders the V4 family, its owned group, and its release record', () => {
    const template = synthV4();
    const { spec } = gate(template);
    const executionRole = roleLogicalId(template, evaluationExecutionRoleName(ENV, 'v4'));
    expect(spec).toMatchObject({
      schema_version: GATE_SPEC_SCHEMA,
      family: 'v4',
      config_name: onlineEvaluationGateName(ENV, 'v4'),
      execution_requested: false,
      execution_role_arn: `{{GetAtt:${executionRole}.Arn}}`,
      sampling_percentage: 100,
      session_timeout_minutes: 1,
      result_retention_days: 14,
      ingress_subscription_filter_name: evaluationResultSubscriptionName(ENV, 'v4'),
      ingress_function_arn:
        `arn:{{Ref:AWS::Partition}}:lambda:eu-west-1:111122223333:function:${evaluationResultIngressV3FunctionName(ENV)}`,
      release_parameter_name: evaluationReleaseParameterName(ENV, 'v4'),
    });
    // Each evaluator is referenced by the identifier of the evaluator this stack declares.
    const evaluatorIds = ofType(template, 'AWS::BedrockAgentCore::Evaluator').map(([id]) => `{{GetAtt:${id}.EvaluatorId}}`);
    expect((spec.evaluators as Record<string, string>[]).map((e) => e.evaluator_id).sort()).toEqual(evaluatorIds.sort());
    // The owned group is the one EvaluationEvidenceStack published, read rather than recomputed.
    expect(parameterPath(template, String(spec.result_log_group_name))).toBe(GROUP_PARAMETER);
    expect(spec.result_log_group_arn).toBe(
      `arn:{{Ref:AWS::Partition}}:logs:eu-west-1:111122223333:log-group:${String(spec.result_log_group_name)}`,
    );
    expect(spec.input_log_group_names).toEqual([expect.stringMatching(/^\{\{Ref:SsmParameterValue/)]);
    expect(spec.input_service_names).toEqual([expect.stringMatching(/^\{\{Ref:SsmParameterValue/)]);
    expect(String(spec.result_log_group_arn)).not.toMatch(/:\*$/);
  });

  it('carries the V4 corpus with each expectation renamed to its evaluator and its context bulleted', () => {
    const { spec } = gate(synthV4());
    const byDimension = Object.fromEntries(
      V4_EVALUATORS.map(([dimension, segment, prompt, model]) => [dimension, v4Name(segment, prompt, model)]),
    );
    const corpus = JSON.parse(fs.readFileSync(EVALUATION_CORPORA.v4, 'utf-8')) as {
      cases: {
        name: string;
        trusted_assertions: string[];
        grounded_context?: string;
        expected: Record<string, unknown>;
      }[];
    };
    const calibration = spec.calibration as { name: string; grounded_context: string; expected: Record<string, unknown> }[];
    expect(calibration.map((entry) => entry.name)).toEqual(corpus.cases.map((entry) => entry.name));
    calibration.forEach((entry, index) => {
      const source = corpus.cases[index]!;
      expect(entry.grounded_context).toBe(
        source.grounded_context ?? source.trusted_assertions.map((a) => `- ${a}`).join('\n'),
      );
      expect(entry.expected).toEqual(Object.fromEntries(
        Object.entries(source.expected).map(([dimension, labels]) => [byDimension[dimension], labels]),
      ));
    });
  });

  it('renders the three V4 evaluators, each named by its definition\'s revision and calibrated by its own dimension', () => {
    const template = synthV4();
    const { spec } = gate(template);
    expect(spec.family).toBe('v4');
    expect(spec.config_name).toBe(onlineEvaluationGateName(ENV, 'v4'));
    expect(spec.release_parameter_name).toBe(evaluationReleaseParameterName(ENV, 'v4'));
    expect(parameterPath(template, String(spec.result_log_group_name))).toBe(GROUP_PARAMETER);
    const evaluators = spec.evaluators as Record<string, string>[];
    const expected = V4_EVALUATORS.map(([, segment, prompt, model]) =>
      [v4Name(segment, prompt, model), revisionOf(prompt, model), model, sha256(prompt)]);
    expect(evaluators.map((e) => [e.evaluator_name, e.revision, e.model_id, e.prompt_digest])).toEqual(expected);
    const names = new Set(evaluators.map((e) => e.evaluator_name));
    for (const entry of spec.calibration as { expected: Record<string, unknown> }[]) {
      for (const key of Object.keys(entry.expected)) expect(names).toContain(key);
    }
  });

  it('passes the dev-only execution request through, and reruns the gate when it changes', () => {
    const off = gate(synthV4());
    const on = gate(synthV4(gateConfig({ v4Requested: true })));
    expect(off.spec.execution_requested).toBe(false);
    expect(on.spec.execution_requested).toBe(true);
    expect(off.resource.Properties.SpecDigest).toMatch(/^[0-9a-f]{64}$/);
    expect(on.resource.Properties.SpecDigest).not.toBe(off.resource.Properties.SpecDigest);
  });

  it('is a destroyable custom resource with a 20-minute service timeout that waits for both roles', () => {
    const template = synthV4();
    const { resource } = gate(template);
    expect(resource.DeletionPolicy).toBe('Delete');
    expect(resource.Properties.ServiceTimeout).toBe('1200');
    expect(Object.keys(resource.Properties).sort()).toEqual(['GateSpec', 'ServiceTimeout', 'ServiceToken', 'SpecDigest']);
    const policyOf = (roleName: string): string => {
      const role = roleLogicalId(template, roleName);
      return ofType(template, 'AWS::IAM::Policy').find(([, policy]) =>
        (policy.Properties.Roles as { Ref: string }[]).some((ref) => ref.Ref === role))![0];
    };
    expect(resource.DependsOn).toEqual(expect.arrayContaining([
      policyOf(evaluationExecutionRoleName(ENV, 'v4')),
      policyOf(evaluationGateProviderRoleName(ENV, 'v4')),
    ]));
  });

  it('declares no CloudFormation online-evaluation configuration, parameter, or output', () => {
    const template = synthV4();
    expect(ofType(template, 'AWS::BedrockAgentCore::OnlineEvaluationConfig')).toEqual([]);
    expect(ofType(template, 'AWS::SSM::Parameter')).toEqual([]);
    expect(template.Outputs).toBeUndefined();
  });
});

describe('the evaluation execution role (ADR-079, observability §3.7)', () => {
  const template = synthV4();
  const grants = roleGrants(template, evaluationExecutionRoleName(ENV, 'v4'));
  // Resolved through the published parameter, whose path stands in for the deployed name.
  const owned = `arn:{AWS::Partition}:logs:eu-west-1:111122223333:log-group:${GROUP_PARAMETER}:*`;

  it('is assumed only by AgentCore evaluation for its own configuration and evaluators', () => {
    const family = 'v4';
    const count = V4_EVALUATORS.length;
    const synthesized = template;
    const [, role] = ofType(synthesized, 'AWS::IAM::Role').find(([, r]) =>
      r.Properties.RoleName === evaluationExecutionRoleName(ENV, family))!;
    const evaluatorArns = ofType(synthesized, 'AWS::BedrockAgentCore::Evaluator')
      .map(([id]) => ({ 'Fn::GetAtt': [id, 'EvaluatorArn'] }));
    expect(evaluatorArns).toHaveLength(count);
    const statement = (role.Properties.AssumeRolePolicyDocument as { Statement: Record<string, unknown>[] }).Statement;
    expect(statement).toEqual([expect.objectContaining({
      Principal: { Service: 'bedrock-agentcore.amazonaws.com' },
      Condition: {
        StringEquals: { 'aws:SourceAccount': '111122223333', 'aws:ResourceAccount': '111122223333' },
        ArnLike: {
          'aws:SourceArn': [
            {
              'Fn::Join': ['', [
                'arn:', { Ref: 'AWS::Partition' },
                `:bedrock-agentcore:eu-west-1:111122223333:online-evaluation-config/${onlineEvaluationGateName(ENV, family)}-*`,
              ]],
            },
            ...evaluatorArns,
          ],
        },
      },
    })]);
    expect(JSON.stringify(statement)).not.toContain(':evaluator/*');
    expect(JSON.stringify(statement)).not.toContain('online-evaluation-config/*');
  });

  it('writes only its owned result group, and nothing under the reserved prefix', () => {
    const writes = [...grants].filter((grant) =>
      /^logs:(CreateLogStream|PutLogEvents|CreateLogGroup|\*)/.test(grant));
    const bare = owned.replace(/:\*$/, '');
    expect(writes.sort()).toEqual([
      `logs:CreateLogGroup ${bare}`,
      `logs:CreateLogGroup ${owned}`,
      `logs:CreateLogStream ${bare}`,
      `logs:CreateLogStream ${owned}`,
      `logs:PutLogEvents ${bare}`,
      `logs:PutLogEvents ${owned}`,
    ].sort());
    expect(JSON.stringify(template)).not.toContain('/aws/bedrock-agentcore/evaluations');
    for (const grant of grants) {
      expect(grant).not.toMatch(/^logs:(Create|Put|Delete)(?!LogStream|LogEvents|IndexPolicy|LogGroup )/);
      if (grant.startsWith('logs:CreateLogGroup')) expect(grant.split(' ')[1]!.startsWith(bare)).toBe(true);
    }
  });

  it('reads the span and input groups and invokes judge models, as the retired L2 role did', () => {
    const actions = new Set([...grants].map((grant) => grant.split(' ')[0]));
    expect([...actions].sort()).toEqual([
      'bedrock:InvokeModel',
      'bedrock:InvokeModelWithResponseStream',
      'logs:CreateLogGroup',
      'logs:CreateLogStream',
      'logs:DescribeIndexPolicies',
      'logs:DescribeLogGroups',
      'logs:GetQueryResults',
      'logs:PutIndexPolicy',
      'logs:PutLogEvents',
      'logs:StartQuery',
    ]);
    expect(grants).toContain('logs:PutIndexPolicy arn:{AWS::Partition}:logs:eu-west-1:111122223333:log-group:aws/spans');
    // A-IAM-MODEL-001: the evaluator models only, each a bare ID here, so in this Region only.
    const models = [...new Set(V4_EVALUATORS.map(([, , , model]) => model))];
    expect([...grants].filter((grant) => grant.startsWith('bedrock:')).sort()).toEqual(models.flatMap((model) => [
      `bedrock:InvokeModel arn:{AWS::Partition}:bedrock:eu-west-1::foundation-model/${model}`,
      `bedrock:InvokeModelWithResponseStream arn:{AWS::Partition}:bedrock:eu-west-1::foundation-model/${model}`,
    ]).sort());
    expect([...grants].join('\n')).not.toContain('inference-profile');
    expect([...grants].join('\n')).not.toContain('foundation-model/*');
  });
});

describe('the gate provider (ADR-079)', () => {
  const template = synthV4();
  const grants = roleGrants(template, evaluationGateProviderRoleName(ENV, 'v4'));
  const partition = '{AWS::Partition}';
  const configArn = `arn:${partition}:bedrock-agentcore:eu-west-1:111122223333:online-evaluation-config/`;
  const release = `arn:${partition}:ssm:eu-west-1:111122223333:parameter${evaluationReleaseParameterName(ENV, 'v4')}`;
  const group = `arn:${partition}:logs:eu-west-1:111122223333:log-group:${GROUP_PARAMETER}`;

  it('is a fixed-name arm64 Python function on the gate handler with Lambda\'s maximum timeout', () => {
    const [, fn] = ofType(template, 'AWS::Lambda::Function').find(([, f]) =>
      f.Properties.FunctionName === evaluationGateProviderFunctionName(ENV, 'v4'))!;
    expect(fn.Properties).toMatchObject({
      Handler: 'adapters.jobs.evaluation_gate.handler.handler',
      Runtime: TEST_CONFIG.pythonRuntime,
      Architectures: ['arm64'],
      Timeout: 900,
      MemorySize: 256,
    });
    expect(ofType(template, 'AWS::Logs::LogGroup').find(([, g]) =>
      g.Properties.LogGroupName === `/aws/lambda/${evaluationGateProviderFunctionName(ENV, 'v4')}`)![1].DeletionPolicy)
      .toBe('Delete');
  });

  it('owns only its configuration, its evaluators, its release record, and read-only Logs', () => {
    const evaluators = ofType(template, 'AWS::BedrockAgentCore::Evaluator').map(([id]) => id);
    expect(evaluators).toHaveLength(V4_EVALUATORS.length);
    const executionRole = roleLogicalId(template, evaluationExecutionRoleName(ENV, 'v4'));
    const [providerLogs] = ofType(template, 'AWS::Logs::LogGroup').find(([, g]) =>
      g.Properties.LogGroupName === `/aws/lambda/${evaluationGateProviderFunctionName(ENV, 'v4')}`)!;
    const logs = `{"Fn::GetAtt":["${providerLogs}","Arn"]}:*`;
    const models = [...new Set(V4_EVALUATORS.map(([, , , model]) => model))];
    expect([...grants].sort()).toEqual([
      'bedrock-agentcore:CreateOnlineEvaluationConfig *',
      'bedrock-agentcore:TagResource *',
      `bedrock-agentcore:DeleteOnlineEvaluationConfig ${configArn}${onlineEvaluationGateName(ENV, 'v4')}-*`,
      ...evaluators.map((evaluator) => `bedrock-agentcore:Evaluate {"Fn::GetAtt":["${evaluator}","EvaluatorArn"]}`),
      ...evaluators.map((evaluator) => `bedrock-agentcore:GetEvaluator {"Fn::GetAtt":["${evaluator}","EvaluatorArn"]}`),
      `bedrock-agentcore:GetOnlineEvaluationConfig ${configArn}${onlineEvaluationGateName(ENV, 'v4')}-*`,
      'bedrock-agentcore:ListOnlineEvaluationConfigs *',
      `bedrock-agentcore:UpdateOnlineEvaluationConfig ${configArn}${onlineEvaluationGateName(ENV, 'v4')}-*`,
      ...models.map((model) => `bedrock:InvokeModel arn:${partition}:bedrock:eu-west-1::foundation-model/${model}`),
      `iam:PassRole {"Fn::GetAtt":["${executionRole}","Arn"]}`,
      'logs:CreateLogStream ' + logs,
      'logs:DescribeAccountPolicies *',
      'logs:DescribeDeliveryDestinations *',
      'logs:DescribeExportTasks *',
      'logs:DescribeLogGroups *',
      'logs:DescribeResourcePolicies *',
      `logs:DescribeSubscriptionFilters ${group}`,
      `logs:DescribeSubscriptionFilters ${group}:*`,
      `logs:GetDataProtectionPolicy ${group}`,
      `logs:GetDataProtectionPolicy ${group}:*`,
      `logs:GetTransformer ${group}`,
      `logs:GetTransformer ${group}:*`,
      'logs:PutLogEvents ' + logs,
      `ssm:DeleteParameter ${release}`,
      `ssm:GetParameters ${release}`,
      `ssm:PutParameter ${release}`,
      'xray:PutTelemetryRecords *',
      'xray:PutTraceSegments *',
    ].sort());
  });

  it('creates and tags only with exactly the standard tag set (H-IAM-AGENTCORE-CREATE-001)', () => {
    const statements = ofType(template, 'AWS::IAM::Policy')
      .flatMap(([, policy]) => (policy.Properties.PolicyDocument as { Statement: Record<string, unknown>[] }).Statement);
    const bySid = (sid: string): Record<string, unknown> => {
      const found = statements.filter((statement) => statement.Sid === sid);
      expect(found).toHaveLength(1);
      return found[0]!;
    };
    const requestTags = {
      StringEquals: {
        'aws:RequestTag/Application': 'adlytics-ai',
        'aws:RequestTag/Environment': ENV,
        'aws:RequestTag/ManagedBy': 'cdk',
      },
      'ForAllValues:StringEquals': { 'aws:TagKeys': ['Application', 'Environment', 'ManagedBy'] },
    };
    expect(bySid('CreateTheConfiguration')).toStrictEqual({
      Sid: 'CreateTheConfiguration',
      Effect: 'Allow',
      Action: 'bedrock-agentcore:CreateOnlineEvaluationConfig',
      Resource: '*',
      Condition: requestTags,
    });
    expect(bySid('TagWithTheStandardSet')).toStrictEqual({
      Sid: 'TagWithTheStandardSet',
      Effect: 'Allow',
      Action: 'bedrock-agentcore:TagResource',
      Resource: '*',
      Condition: {
        ...requestTags,
        StringEqualsIfExists: {
          'aws:ResourceTag/Application': 'adlytics-ai',
          'aws:ResourceTag/Environment': ENV,
        },
      },
    });
  });

  it('passes the execution role only to AgentCore', () => {
    const rendered = JSON.stringify(ofType(template, 'AWS::IAM::Policy'));
    expect(rendered).toContain('"iam:PassedToService":"bedrock-agentcore.amazonaws.com"');
  });
});
describe('the gate provider invoke boundary (ADR-079)', () => {
  const CFN_EXEC_ROLE = {
    'Fn::Sub': 'arn:${AWS::Partition}:iam::111122223333:role/cdk-hnb659fds-cfn-exec-role-111122223333-eu-west-1',
  };
  it('carries one resource policy: a Deny for every principal but the CloudFormation execution role', () => {
    const family = 'v4';
    const template = synthV4();
    const [providerId] = ofType(template, 'AWS::Lambda::Function').find(([, f]) =>
      f.Properties.FunctionName === evaluationGateProviderFunctionName(ENV, family))!;
    const policies = ofType(template, 'AWS::Lambda::ResourcePolicy');
    expect(policies).toHaveLength(1);
    const [policyId, policy] = policies[0]!;
    const functionArn = { 'Fn::GetAtt': [providerId, 'Arn'] };
    expect(policy.Properties).toStrictEqual({
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
            ArnNotEquals: { 'aws:PrincipalArn': CFN_EXEC_ROLE },
            StringNotEquals: { 'aws:PrincipalServiceName': 'lambda.amazonaws.com' },
          },
        }],
      },
    });
    // No second writer of the same policy.
    expect(ofType(template, 'AWS::Lambda::Permission')).toEqual([]);
    // The gate first runs, and last runs on delete, with the boundary in place.
    expect(gate(template).resource.DependsOn).toContain(policyId);
  });
  it('is never retried by Lambda after a timed-out CloudFormation invoke', () => {
    const template = synthV4();
    const [providerId] = ofType(template, 'AWS::Lambda::Function').find(([, f]) =>
      f.Properties.FunctionName === evaluationGateProviderFunctionName(ENV, 'v4'))!;
    expect(ofType(template, 'AWS::Lambda::EventInvokeConfig').map(([, config]) => config.Properties)).toEqual([
      { FunctionName: { Ref: providerId }, MaximumRetryAttempts: 0, Qualifier: '$LATEST' },
    ]);
  });
});
describe('evaluator identity follows its definition (ADR-079)', () => {
  const prompt = fs.readFileSync(V4_PROMPTS.helpfulness, 'utf-8');
  it('changes the name and the revision when the instructions change', () => {
    const before = evaluatorIdentity(ENV, 'helpfulness_v4', prompt, QUICK_MODEL);
    const after = evaluatorIdentity(ENV, 'helpfulness_v4', `${prompt} `, QUICK_MODEL);
    expect(after.revision).not.toBe(before.revision);
    expect(after.evaluatorName).not.toBe(before.evaluatorName);
  });
  it('changes the name and the revision when the model changes', () => {
    const before = evaluatorIdentity(ENV, 'helpfulness_v4', prompt, QUICK_MODEL);
    const after = evaluatorIdentity(ENV, 'helpfulness_v4', prompt, DEEP_MODEL);
    expect(after.revision).not.toBe(before.revision);
    expect(after.evaluatorName).not.toBe(before.evaluatorName);
  });
  it('ends the name with the revision the gate carries', () => {
    const { evaluatorName, revision } = evaluatorIdentity('dev-a', 'helpfulness_v4', prompt, QUICK_MODEL);
    expect(revision).toMatch(/^[0-9a-f]{8}$/);
    expect(evaluatorName).toBe(`adlytics_dev_a_helpfulness_v4_${revision}`);
  });
  it('replaces the synthesized helpfulness evaluator when its model changes, and the gate follows it', () => {
    const before = synthV4();
    const after = synthV4(configWith({
      ...gateConfig(),
      agent: { ...TEST_CONFIG.agent, quickChat: { ...TEST_CONFIG.agent.quickChat, modelId: 'test.other-model-v1:0' } },
    }));
    const nameOf = (template: Record<string, unknown>): string =>
      String(ofType(template, 'AWS::BedrockAgentCore::Evaluator')
        .map(([, evaluator]) => evaluator.Properties.EvaluatorName)
        .find((name) => String(name).includes('_helpfulness_v4_')));
    expect(nameOf(after)).not.toBe(nameOf(before));
    expect((gate(after).spec.evaluators as Record<string, unknown>[]).map((e) => e.evaluator_name)).toContain(nameOf(after));
  });
  it('every evaluator name fits the service limit and is retained', () => {
    const found = ofType(synthV4(), 'AWS::BedrockAgentCore::Evaluator');
    expect(found.length).toBeGreaterThan(0);
    for (const [, evaluator] of found) {
      expect(String(evaluator.Properties.EvaluatorName)).toMatch(EVALUATOR_NAME_PATTERN);
      expect(String(evaluator.Properties.EvaluatorName).length).toBeLessThanOrEqual(48);
      expect(evaluator.DeletionPolicy).toBe('Retain');
    }
  });
  it('fails synth on a name past 48 characters', () => {
    expect(() => evaluatorIdentity('abcdefghijk', 'internal_consistency_v4', prompt, DEEP_MODEL))
      .toThrow(/does not match/);
    expect(() => synthV4(configWith({ ...gateConfig(), envName: 'abcdefghijk' }))).toThrow(/does not match/);
  });
});
describe('the calibration loader (ADR-079)', () => {
  const corpus = (cases: Record<string, unknown>[]): string => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'gate-corpus-'));
    const file = path.join(dir, 'cases.json');
    fs.writeFileSync(file, JSON.stringify({ cases }));
    return file;
  };
  const entry = (extra: Record<string, unknown>): Record<string, unknown> => ({
    name: 'fictional',
    question: 'q',
    trusted_assertions: ['a'],
    candidate: 'c',
    expected: { helpfulness: ['Good'] },
    ...extra,
  });
  const evaluators = [{ corpusDimension: 'helpfulness', evaluatorName: 'adlytics_dev_x' }];
  it('passes each case\'s kind through verbatim', () => {
    const file = corpus([entry({ kind: 'fictional_kind' })]);
    try {
      expect(loadCalibration(file, evaluators)).toEqual([{
        name: 'fictional',
        kind: 'fictional_kind',
        question: 'q',
        grounded_context: '- a',
        candidate: 'c',
        expected: { adlytics_dev_x: ['Good'] },
      }]);
    } finally {
      fs.rmSync(path.dirname(file), { recursive: true, force: true });
    }
  });
  it.each([
    ['absent', {}],
    ['empty', { kind: '' }],
    ['not text', { kind: 3 }],
  ])('fails synth when a case\'s kind is %s', (_label, extra) => {
    const file = corpus([entry(extra)]);
    try {
      expect(() => loadCalibration(file, evaluators)).toThrow(/has no calibration kind/);
    } finally {
      fs.rmSync(path.dirname(file), { recursive: true, force: true });
    }
  });
});
