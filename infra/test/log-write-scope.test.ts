// SPDX-License-Identifier: MIT-0
/**
 * Every Lambda role writes only its own log group, and nothing reaches the evaluation result
 * group outside the evaluator's pinned grant (ADR-085, M-IAM-DISPATCH-LOGS-001,
 * H-EVALUATION-RESULT-001).
 *
 * Each predicate in `support/log-write-scope` is tested on a throwaway `Probe` stack (fictional
 * account, `eu-west-1`, environment `test`) against its exact output, with a positive control per
 * rule, and then run over every synthesised stack.
 */
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { evaluationResultLogGroupName, paramPath } from '../lib/config';
import { RESULT_GROUP_PARAMETER } from '../lib/constructs/agent-quality/evaluation-gate';
import { grantOwnLogWrites, lambdaRole } from '../lib/mixins/logging';
import {
  LOG_WRITE_PINS,
  type LogWritePin,
  type ScopeContext,
  ownLogGroupControl,
  r1ManagedPolicies,
  r2LogWrites,
  r3Custody,
  r3Read,
  r3Unscoped,
  r3Write,
  r4ResourcePolicies,
  unusedPins,
} from './support/log-write-scope';
import { everyTemplate } from './support/templates';

type Json = Record<string, unknown>;

const PROBE: ScopeContext = { envName: 'test', region: 'eu-west-1', account: '111122223333' };
const OTHER_GROUP = 'arn:aws:logs:eu-west-1:111122223333:log-group:/fictional/other';
const HANDLER_ASSET = __dirname;

function probe(build: (stack: cdk.Stack) => void): Json {
  const stack = new cdk.Stack(new cdk.App(), 'Probe', {
    env: { account: PROBE.account, region: PROBE.region },
  });
  build(stack);
  return Template.fromStack(stack).toJSON() as Json;
}

/** The one logical id of a resource type in a probe template. */
function onlyId(template: Json, type: string): string {
  const ids = Object.entries((template.Resources ?? {}) as Record<string, { Type?: unknown }>)
    .filter(([, resource]) => resource.Type === type)
    .map(([id]) => id);
  expect(ids).toHaveLength(1);
  return ids[0] as string;
}

/** A role holding one statement; the template's only IAM policy carries it. */
function roleWith(statement: iam.PolicyStatement): Json {
  return probe((stack) => {
    const role = new iam.Role(stack, 'Role', { assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com') });
    role.addToPolicy(statement);
  });
}

function resultGroup(stack: cdk.Stack): logs.LogGroup {
  return new logs.LogGroup(stack, 'ResultGroup', {
    logGroupName: evaluationResultLogGroupName(PROBE.envName, 'v4'),
  });
}

function roleOnResultGroup(actions: string[]): Json {
  return probe((stack) => {
    const group = resultGroup(stack);
    const role = new iam.Role(stack, 'Role', { assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com') });
    role.addToPolicy(new iam.PolicyStatement({ actions, resources: [group.logGroupArn] }));
  });
}

function resourcePolicy(statement: Json): Json {
  return probe((stack) => {
    new logs.CfnResourcePolicy(stack, 'P', {
      policyName: 'fictional',
      policyDocument: JSON.stringify({ Version: '2012-10-17', Statement: [statement] }),
    });
  });
}

const policyId = (template: Json): string => onlyId(template, 'AWS::IAM::Policy');
const READ_LINE = 'log read reaches the evaluation result group';
const CUSTODY_LINE = 'custody action reaches the evaluation result group';
const WRITE_LINE = 'log write reaches the evaluation result group outside the execution role pin';

describe('R1: no managed policy the gate cannot read', () => {
  it('refuses the default role a function gets without one', () => {
    const template = probe((stack) => {
      new lambda.Function(stack, 'Fn', {
        runtime: lambda.Runtime.PYTHON_3_13,
        handler: 'index.handler',
        code: lambda.Code.fromInline('def handler(event, context): pass'),
      });
    });
    const found = r1ManagedPolicies('Probe', template, PROBE);
    expect(found).toHaveLength(1);
    expect(found[0]).toMatch(
      /^Probe\/FnServiceRole[0-9A-F]{8}: attaches AWS-managed policy service-role\/AWSLambdaBasicExecutionRole$/,
    );
    expect(ownLogGroupControl('Probe', template)).toEqual([
      `Probe/${onlyId(template, 'AWS::Lambda::Function')}: cannot write its own log group`,
    ]);
  });

  it('refuses any AWS-managed policy by class', () => {
    const template = probe((stack) => {
      new iam.Role(stack, 'Role', {
        assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
        managedPolicies: [iam.ManagedPolicy.fromAwsManagedPolicyName('CloudWatchLogsFullAccess')],
      });
    });
    expect(r1ManagedPolicies('Probe', template, PROBE)).toEqual([
      `Probe/${onlyId(template, 'AWS::IAM::Role')}: attaches AWS-managed policy CloudWatchLogsFullAccess`,
    ]);
  });

  it('refuses a customer-managed policy attached by ARN', () => {
    const template = probe((stack) => {
      new iam.Role(stack, 'Role', {
        assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
        managedPolicies: [iam.ManagedPolicy.fromManagedPolicyArn(
          stack, 'Fictional', 'arn:aws:iam::111122223333:policy/Fictional',
        )],
      });
    });
    expect(r1ManagedPolicies('Probe', template, PROBE)).toEqual([
      `Probe/${onlyId(template, 'AWS::IAM::Role')}: attaches a managed policy the gate cannot read`,
    ]);
  });

  it('accepts an in-template managed policy, whose document R2 then reads', () => {
    const template = probe((stack) => {
      const group = new logs.LogGroup(stack, 'Group');
      const policy = new iam.ManagedPolicy(stack, 'Managed', {
        statements: [new iam.PolicyStatement({
          actions: ['logs:CreateLogStream', 'logs:PutLogEvents'],
          resources: [`${group.logGroupArn}:*`],
        })],
      });
      new iam.Role(stack, 'Role', {
        assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
        managedPolicies: [policy],
      });
    });
    expect(r1ManagedPolicies('Probe', template, PROBE)).toEqual([]);
    expect(r2LogWrites('Probe', template, PROBE, [])).toEqual([]);
  });
});

describe('R2 and R3-write: log writes', () => {
  it('refuses a write on a glob that reaches the result group', () => {
    const template = roleWith(new iam.PolicyStatement({
      actions: ['logs:PutLogEvents'],
      resources: ['arn:aws:logs:*:*:log-group:/adlytics/*'],
    }));
    expect(r2LogWrites('Probe', template, PROBE, [])).toEqual([
      `Probe/${policyId(template)}: log write on "arn:aws:logs:*:*:log-group:/adlytics/*" is not the writer's own group or a pinned grant`,
    ]);
    expect(r3Write('Probe', template, PROBE, [])).toEqual([`Probe/${policyId(template)}: ${WRITE_LINE}`]);
  });

  it('refuses a write on the parameter-named result group outside the pin', () => {
    const template = probe((stack) => {
      const name = ssm.StringParameter.valueForStringParameter(
        stack, paramPath(PROBE.envName, RESULT_GROUP_PARAMETER.v4),
      );
      const role = new iam.Role(stack, 'Role', { assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com') });
      role.addToPolicy(new iam.PolicyStatement({
        actions: ['logs:PutLogEvents'],
        resources: [`arn:${cdk.Aws.PARTITION}:logs:eu-west-1:111122223333:log-group:${name}`],
      }));
    });
    expect(r3Write('Probe', template, PROBE, [])).toEqual([`Probe/${policyId(template)}: ${WRITE_LINE}`]);
  });

  it('refuses an Allow whose NotAction leaves log writes in', () => {
    const template = roleWith(new iam.PolicyStatement({ notActions: ['s3:*'], resources: ['*'] }));
    expect(r2LogWrites('Probe', template, PROBE, [])).toEqual([
      `Probe/${policyId(template)}: log write on NotAction is not the writer's own group or a pinned grant`,
    ]);
  });

  it('accepts an Allow whose NotAction excludes every log action', () => {
    const template = roleWith(new iam.PolicyStatement({ notActions: ['logs:*'], resources: ['*'] }));
    expect(r2LogWrites('Probe', template, PROBE, [])).toEqual([]);
  });
});

describe('R3-read and R3-custody: what reaches the result group', () => {
  it('refuses a read on a same-template group named as the result group', () => {
    const template = roleOnResultGroup(['logs:FilterLogEvents']);
    expect(r3Read('Probe', template, PROBE)).toEqual([`Probe/${policyId(template)}: ${READ_LINE}`]);
  });

  it.each([['logs:Start*'], ['logs:GetLogRecord']])('refuses %s on every group', (action) => {
    const template = roleWith(new iam.PolicyStatement({ actions: [action], resources: ['*'] }));
    expect(r3Read('Probe', template, PROBE)).toEqual([`Probe/${policyId(template)}: ${READ_LINE}`]);
  });

  it('accepts a read on a group that is not a result group', () => {
    const template = roleWith(new iam.PolicyStatement({ actions: ['logs:GetQueryResults'], resources: [OTHER_GROUP] }));
    expect(r3Read('Probe', template, PROBE)).toEqual([]);
  });

  it('judges a NotAction that excludes only the writes by what it still covers', () => {
    const template = roleWith(new iam.PolicyStatement({
      notActions: ['logs:CreateLogGroup', 'logs:CreateLogStream', 'logs:PutLogEvents'],
      resources: ['*'],
    }));
    const id = policyId(template);
    expect(r3Write('Probe', template, PROBE, [])).toEqual([]);
    expect(r3Read('Probe', template, PROBE)).toEqual([`Probe/${id}: ${READ_LINE}`]);
    expect(r3Custody('Probe', template, PROBE)).toEqual([`Probe/${id}: ${CUSTODY_LINE}`]);
    expect(r2LogWrites('Probe', template, PROBE, [])).toEqual([]);
  });

  it('treats a NotResource as reaching the result group', () => {
    const template = roleWith(new iam.PolicyStatement({ actions: ['logs:GetLogEvents'], notResources: [OTHER_GROUP] }));
    expect(r3Read('Probe', template, PROBE)).toEqual([`Probe/${policyId(template)}: ${READ_LINE}`]);
  });

  it('accepts a NotAction that excludes every log action', () => {
    const template = roleWith(new iam.PolicyStatement({ notActions: ['logs:*'], resources: ['*'] }));
    expect(r3Write('Probe', template, PROBE, [])).toEqual([]);
    expect(r3Read('Probe', template, PROBE)).toEqual([]);
    expect(r3Custody('Probe', template, PROBE)).toEqual([]);
  });

  it('refuses an export task on every group', () => {
    const template = roleWith(new iam.PolicyStatement({ actions: ['logs:CreateExportTask'], resources: ['*'] }));
    expect(r3Custody('Probe', template, PROBE)).toEqual([`Probe/${policyId(template)}: ${CUSTODY_LINE}`]);
  });

  it('refuses a subscription filter on the result group', () => {
    const template = roleOnResultGroup(['logs:PutSubscriptionFilter']);
    expect(r3Custody('Probe', template, PROBE)).toEqual([`Probe/${policyId(template)}: ${CUSTODY_LINE}`]);
  });

  it('accepts the Harness provider shape, whose pattern cannot match a result group', () => {
    const template = roleWith(new iam.PolicyStatement({
      actions: ['logs:CreateLogGroup', 'logs:PutRetentionPolicy'],
      resources: ['arn:aws:logs:eu-west-1:111122223333:log-group:/aws/bedrock-agentcore/runtimes/harness_fictional-*'],
    }));
    expect(r3Custody('Probe', template, PROBE)).toEqual([]);
  });

  it('accepts the gate readback on the parameter-named group', () => {
    const template = probe((stack) => {
      const name = ssm.StringParameter.valueForStringParameter(
        stack, paramPath(PROBE.envName, RESULT_GROUP_PARAMETER.v4),
      );
      const role = new iam.Role(stack, 'Role', { assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com') });
      role.addToPolicy(new iam.PolicyStatement({
        actions: ['logs:DescribeSubscriptionFilters', 'logs:GetDataProtectionPolicy', 'logs:GetTransformer'],
        resources: [`arn:${cdk.Aws.PARTITION}:logs:eu-west-1:111122223333:log-group:${name}`],
      }));
    });
    expect(r3Write('Probe', template, PROBE, [])).toEqual([]);
    expect(r3Read('Probe', template, PROBE)).toEqual([]);
    expect(r3Custody('Probe', template, PROBE)).toEqual([]);
  });
});

describe('R3-unscoped: actions that can open any group to a delivering service', () => {
  it('refuses logs:PutResourcePolicy, which scoped custody does not cover', () => {
    const template = roleWith(new iam.PolicyStatement({
      actions: ['logs:PutResourcePolicy', 'logs:DescribeLogGroups'],
      resources: ['*'],
    }));
    expect(r3Custody('Probe', template, PROBE)).toEqual([]);
    expect(r3Unscoped('Probe', template)).toEqual([
      `Probe/${policyId(template)}: grants logs:PutResourcePolicy, which can open any log group to a delivering service`,
    ]);
  });

  it('refuses every unscoped action a glob covers, whatever the resource', () => {
    const template = roleWith(new iam.PolicyStatement({ actions: ['logs:Put*'], resources: [OTHER_GROUP] }));
    const id = policyId(template);
    expect(r3Unscoped('Probe', template)).toEqual([
      `Probe/${id}: grants logs:PutResourcePolicy, which can open any log group to a delivering service`,
      `Probe/${id}: grants logs:PutAccountPolicy, which can open any log group to a delivering service`,
      `Probe/${id}: grants logs:PutDeliveryDestination, which can open any log group to a delivering service`,
    ]);
  });

  it('accepts DescribeLogGroups alone', () => {
    const template = roleWith(new iam.PolicyStatement({ actions: ['logs:DescribeLogGroups'], resources: ['*'] }));
    expect(r3Unscoped('Probe', template)).toEqual([]);
  });
});

describe('R4: Logs resource policies', () => {
  it('refuses a NotAction resource policy on every group', () => {
    const template = resourcePolicy({
      Effect: 'Allow',
      Principal: { Service: 'xray.amazonaws.com' },
      NotAction: ['logs:DeleteLogGroup'],
      Resource: '*',
    });
    expect(r4ResourcePolicies('Probe', template, PROBE)).toEqual([
      'Probe/P: Logs resource policy reaches the evaluation result group',
    ]);
  });

  it('refuses a delivery grant on the evaluation groups', () => {
    const template = resourcePolicy({
      Effect: 'Allow',
      Principal: { Service: 'xray.amazonaws.com' },
      Action: 'logs:PutLogEvents',
      Resource: 'arn:aws:logs:*:*:log-group:/adlytics/test/evaluation/*',
    });
    expect(r4ResourcePolicies('Probe', template, PROBE)).toEqual([
      'Probe/P: Logs resource policy reaches the evaluation result group',
    ]);
  });

  it('accepts the span-delivery shape', () => {
    const template = resourcePolicy({
      Effect: 'Allow',
      Principal: { Service: 'xray.amazonaws.com' },
      Action: 'logs:PutLogEvents',
      Resource: [
        'arn:aws:logs:eu-west-1:111122223333:log-group:aws/spans:*',
        'arn:aws:logs:eu-west-1:111122223333:log-group:/aws/application-signals/data:*',
      ],
    });
    expect(r4ResourcePolicies('Probe', template, PROBE)).toEqual([]);
  });
});

describe('pins and the positive control', () => {
  const ownGroupFunction = (): Json => probe((stack) => {
    const group = new logs.LogGroup(stack, 'FnLogs');
    const role = lambdaRole(stack, 'FnRole');
    grantOwnLogWrites(role, group);
    new lambda.Function(stack, 'Fn', {
      role,
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: 'index.handler',
      code: lambda.Code.fromAsset(HANDLER_ASSET),
      logGroup: group,
    });
  });

  it('reports a pin no template uses', () => {
    const fictional: LogWritePin = {
      name: 'FictionalPin',
      stack: 'Probe',
      role: /^FictionalRole[0-9A-F]{8}$/,
      resources: [OTHER_GROUP],
    };
    expect(unusedPins({ Probe: ownGroupFunction() }, [fictional], PROBE)).toEqual(['unused pin FictionalPin']);
    expect(unusedPins({ Probe: ownGroupFunction() }, [], PROBE)).toEqual([]);
  });

  it('passes every rule for a function built with lambdaRole and grantOwnLogWrites', () => {
    const template = ownGroupFunction();
    expect(r1ManagedPolicies('Probe', template, PROBE)).toEqual([]);
    expect(r2LogWrites('Probe', template, PROBE, [])).toEqual([]);
    expect(r3Write('Probe', template, PROBE, [])).toEqual([]);
    expect(r3Read('Probe', template, PROBE)).toEqual([]);
    expect(r3Custody('Probe', template, PROBE)).toEqual([]);
    expect(r3Unscoped('Probe', template)).toEqual([]);
    expect(r4ResourcePolicies('Probe', template, PROBE)).toEqual([]);
    expect(ownLogGroupControl('Probe', template)).toEqual([]);
  });
});

describe('every synthesised stack', () => {
  const templates = everyTemplate() as Record<string, Json>;
  const each = (predicate: (name: string, template: Json) => string[]): string[] =>
    Object.entries(templates).flatMap(([name, template]) => predicate(name, template));

  it.each([
    ['R1', (n: string, t: Json) => r1ManagedPolicies(n, t)],
    ['R2', (n: string, t: Json) => r2LogWrites(n, t)],
    ['R3-write', (n: string, t: Json) => r3Write(n, t)],
    ['R3-read', (n: string, t: Json) => r3Read(n, t)],
    ['R3-custody', (n: string, t: Json) => r3Custody(n, t)],
    ['R3-unscoped', (n: string, t: Json) => r3Unscoped(n, t)],
    ['R4', (n: string, t: Json) => r4ResourcePolicies(n, t)],
    ['every function can write its own log group', (n: string, t: Json) => ownLogGroupControl(n, t)],
  ])('%s holds', (_rule, predicate) => {
    expect(each(predicate)).toEqual([]);
  });

  it('uses every log-write pin', () => {
    expect(unusedPins(templates, LOG_WRITE_PINS)).toEqual([]);
  });

  it('gives the evaluator and the Harness no grant that reaches the result group', () => {
    for (const name of ['EvaluationV4Stack', 'HarnessStack']) {
      const template = templates[name] as Json;
      expect(r3Write(name, template)).toEqual([]);
      expect(r3Read(name, template)).toEqual([]);
      expect(r3Custody(name, template)).toEqual([]);
      expect(r3Unscoped(name, template)).toEqual([]);
    }
    expect(r4ResourcePolicies('HarnessStack', templates.HarnessStack as Json)).toEqual([]);
  });

  it('builds the Frontend with no auto-delete helper and no managed policy', () => {
    const resources = Object.values((templates.FrontendStack as Json).Resources as Record<string, Json>);
    expect(resources.filter((r) => r.Type === 'Custom::S3AutoDeleteObjects')).toEqual([]);
    expect(resources.filter((r) => r.Type === 'AWS::IAM::Role'
      && (r.Properties as Json | undefined)?.ManagedPolicyArns !== undefined)).toEqual([]);
  });
});
