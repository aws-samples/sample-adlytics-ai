// SPDX-License-Identifier: MIT-0
import * as cdk from 'aws-cdk-lib';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import { Template } from 'aws-cdk-lib/assertions';

import { grantSpanExport } from '../lib/telemetry';
import { TEST_CONFIG } from './support/config';

type Statement = {
  readonly Action?: string | string[];
  readonly Resource?: string | string[];
};

function spanExportStatement(): Statement {
  const stack = new cdk.Stack();
  const fn = new lambda.Function(stack, 'Function', {
    runtime: new lambda.Runtime(TEST_CONFIG.pythonRuntime, lambda.RuntimeFamily.PYTHON),
    architecture: lambda.Architecture.ARM_64,
    code: lambda.Code.fromAsset(__dirname),
    handler: 'index.handler',
  });
  grantSpanExport(fn);

  const policies = Object.values(Template.fromStack(stack).findResources('AWS::IAM::Policy'));
  const statements = policies.flatMap((policy) => {
    const document = policy.Properties.PolicyDocument as { Statement?: Statement[] };
    return document.Statement ?? [];
  });
  const matching = statements.filter((statement) => {
    const actions = Array.isArray(statement.Action) ? statement.Action : [statement.Action];
    return actions.includes('xray:PutTraceSegments');
  });
  if (matching.length !== 1 || matching[0] === undefined) {
    throw new Error(`expected one Lambda span-export statement, found ${matching.length}`);
  }
  return matching[0];
}

describe('grantSpanExport', () => {
  it('grants exactly the two scopeless X-Ray span-export actions', () => {
    const statement = spanExportStatement();
    expect((statement.Action as string[]).toSorted()).toEqual([
      'xray:PutTelemetryRecords',
      'xray:PutTraceSegments',
    ]);
    expect(statement.Resource).toBe('*');
  });

  it('does not grant centralised sampling reads to Lambda', () => {
    const actions = spanExportStatement().Action as string[];
    expect(actions).not.toContain('xray:GetSamplingRules');
    expect(actions).not.toContain('xray:GetSamplingTargets');
    expect(actions).not.toContain('xray:GetSamplingStatisticSummaries');
  });
});
