// SPDX-License-Identifier: MIT-0
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { paramPath } from '../lib/config';
import { ApiStack, OPERATIONAL_STATUS_RESERVED_CONCURRENCY } from '../lib/stacks/api-stack';
import { TEST_CONFIG } from './support/config';
import { everyTemplate, resourcesOfType } from './support/templates';

const api = everyTemplate().ApiStack as Record<string, unknown>;
type Resource = Record<string, unknown>;

function parameterDefault(reference: unknown): unknown {
  const logicalId = (reference as { Ref?: unknown } | undefined)?.Ref;
  if (typeof logicalId !== 'string') return undefined;
  return ((api.Parameters as Record<string, Resource> | undefined)?.[logicalId])?.Default;
}

function operationalFunction(): Resource {
  const functions = resourcesOfType(api, 'AWS::Lambda::Function').filter(
    (fn) => fn.Handler === 'adapters.inbound.operational_status.handler.handler',
  );
  expect(functions).toHaveLength(1);
  const [fn] = functions;
  if (fn === undefined) throw new Error('operational status function is absent');
  return fn;
}

function operationalRole(): string {
  const role = (operationalFunction().Role as { 'Fn::GetAtt'?: unknown[] } | undefined)
    ?.["Fn::GetAtt"]?.[0];
  if (typeof role !== 'string') throw new Error('operational status role is absent');
  return role;
}

function operationalStatements(): Resource[] {
  const role = operationalRole();
  return resourcesOfType(api, 'AWS::IAM::Policy')
    .filter((policy) =>
      ((policy.Roles as { Ref?: string }[] | undefined) ?? []).some(
        (candidate) => candidate.Ref === role,
      ),
    )
    .flatMap(
      (policy) =>
        ((policy.PolicyDocument as { Statement?: Resource[] } | undefined)?.Statement ?? []),
    );
}

function actions(): string[] {
  return operationalStatements().flatMap((statement) => {
    const action = statement.Action;
    return (Array.isArray(action) ? action : [action]).filter(
      (candidate): candidate is string => typeof candidate === 'string',
    );
  });
}

function authProvisionedFlag(template: Record<string, unknown>): unknown {
  const functions = resourcesOfType(template, 'AWS::Lambda::Function').filter(
    (fn) => fn.Handler === 'adapters.inbound.auth_handlers.handlers.handler',
  );
  expect(functions).toHaveLength(1);
  return ((functions[0]?.Environment as { Variables?: Resource } | undefined)?.Variables ?? {})
    .ADL_OPERATIONAL_STATUS_PROVISIONED;
}

describe('the auth function knows whether the operational status API is provisioned (ADR-115 decision 8)', () => {
  it('is "true" where the routes exist', () => {
    expect(resourcesOfType(api, 'AWS::ApiGatewayV2::Route').some(
      (route) => route.RouteKey === 'GET /api/operations/data-flows',
    )).toBe(true);
    expect(authProvisionedFlag(api)).toBe('true');
  });

  it('is "false" where they do not', () => {
    const unprovisioned = Template.fromStack(new ApiStack(new cdk.App(), 'ApiWithoutOperationalStatus', {
      config: TEST_CONFIG,
      env: { account: TEST_CONFIG.account, region: TEST_CONFIG.region },
      sourceAssetPath: __dirname,
    })).toJSON() as Record<string, unknown>;
    expect(TEST_CONFIG.operationalStatus.provisioned).toBe(false);
    expect(resourcesOfType(unprovisioned, 'AWS::ApiGatewayV2::Route').some(
      (route) => String(route.RouteKey).includes('/api/operations/'),
    )).toBe(false);
    expect(authProvisionedFlag(unprovisioned)).toBe('false');
  });
});

describe('the protected operational status API', () => {
  it('creates one isolated reader with only its projection environment', () => {
    const fn = operationalFunction();
    expect(fn).toEqual(expect.objectContaining({
      FunctionName: 'adlytics-dev-OperationalStatusFunction',
      Architectures: ['arm64'],
      MemorySize: 512,
      Timeout: 10,
    }));
    const variables = (fn.Environment as { Variables?: Resource } | undefined)?.Variables ?? {};
    expect(parameterDefault(variables.ADL_OPERATIONAL_STATUS_TABLE)).toBe(
      paramPath('dev', 'operationalStatusProjectionTableName'),
    );
    expect(variables).not.toHaveProperty('ADL_SEMANTIC_ATTEMPTS_TABLE');
    expect(variables).not.toHaveProperty('ADL_OPERATIONAL_STATUS_QUEUE_URL');
  });

  it('S2: protects the execution-explanation POST route with the same CUSTOM authorizer', () => {
    const routes = resourcesOfType(api, 'AWS::ApiGatewayV2::Route');
    expect(
      routes.find((route) => route.RouteKey === 'POST /api/operations/execution-explanations'),
    ).toEqual(expect.objectContaining({ AuthorizationType: 'CUSTOM' }));
  });

  it('S2 (BF-5): signs cursors with one KMS HMAC key and no cursor store', () => {
    const keys = resourcesOfType(api, 'AWS::KMS::Key');
    expect(keys).toEqual([
      expect.objectContaining({ KeySpec: 'HMAC_256', KeyUsage: 'GENERATE_VERIFY_MAC' }),
    ]);
    const statement = operationalStatements().find(
      (item) => item.Sid === 'SignStatelessCursors',
    );
    expect(statement?.Action).toEqual(['kms:GenerateMac', 'kms:VerifyMac']);
    expect(resourcesOfType(api, 'AWS::DynamoDB::Table')).toHaveLength(0);
  });

  it('S2 (BF-5): bounds explanation load by reserved concurrency, leaving lifecycle on the pool', () => {
    expect(operationalFunction().ReservedConcurrentExecutions).toBe(
      OPERATIONAL_STATUS_RESERVED_CONCURRENCY,
    );
    expect(OPERATIONAL_STATUS_RESERVED_CONCURRENCY).toBeGreaterThan(0);
    // Run-request admission is a lifecycle command: no reservation caps it, and none is zero.
    const admission = resourcesOfType(api, 'AWS::Lambda::Function').filter(
      (fn) => fn.Handler === 'adapters.inbound.run_requests.handler.handler',
    );
    expect(admission).toHaveLength(1);
    expect(admission[0]).not.toHaveProperty('ReservedConcurrentExecutions');
  });

  it('S2: reads only canonical raw manifest versions, never another raw object', () => {
    const statement = operationalStatements().find((item) => item.Sid === 'ReadManifestVersion');
    expect(statement?.Action).toEqual(['s3:GetObject', 's3:GetObjectVersion']);
    const resource = JSON.stringify(statement?.Resource);
    expect(resource).toContain('/*/manifest.json');
    expect(resource).not.toContain('"/*"');
  });

  it('protects all four GET routes with the CUSTOM authorizer', () => {
    const routes = resourcesOfType(api, 'AWS::ApiGatewayV2::Route');
    for (const path of [
      '/api/operations/data-flow-definitions',
      '/api/operations/data-flows',
      '/api/operations/data-flows/{flow_id}',
      '/api/operations/data-flows/{flow_id}/events',
    ]) {
      expect(routes.find((route) => route.RouteKey === `GET ${path}`)).toEqual(
        expect.objectContaining({ AuthorizationType: 'CUSTOM' }),
      );
    }
  });

  it('grants only bounded projection reads and no source, transport, write, scan, or monitoring access', () => {
    const granted = actions();
    expect(granted).toEqual(expect.arrayContaining([
      'dynamodb:GetItem',
      'dynamodb:Query',
      'dynamodb:BatchGetItem',
    ]));
    for (const denied of [
      'dynamodb:Scan',
      'dynamodb:PutItem',
      'dynamodb:UpdateItem',
      'dynamodb:DeleteItem',
      'sqs:SendMessage',
      'cloudwatch:GetMetricData',
      'cloudwatch:GetMetricStatistics',
    ]) {
      expect(granted).not.toContain(denied);
    }
    const dynamodbStatements = operationalStatements().filter((statement) =>
      (Array.isArray(statement.Action) ? statement.Action : [statement.Action])
        .some((action) => typeof action === 'string' && action.startsWith('dynamodb:')),
    );
    // The projection read, and (S2) the explanation's read-only graph-authority read.
    expect(dynamodbStatements).toHaveLength(2);
    const graphRead = dynamodbStatements.find(
      (statement) => statement.Sid === 'ReadExecutionGraphAuthority',
    );
    expect(graphRead?.Action).toEqual(['dynamodb:GetItem', 'dynamodb:BatchGetItem']);
    expect(parameterDefault(graphRead?.Resource)).toBe(paramPath('dev', 'executionGraphTableArn'));
    const projectionRead = dynamodbStatements.find((statement) => statement !== graphRead);
    const resourceValues = projectionRead?.Resource as unknown[];
    expect(parameterDefault(resourceValues[0])).toBe(
      paramPath('dev', 'operationalStatusProjectionTableArn'),
    );
    const resources = JSON.stringify(resourceValues);
    expect(resources).toContain('/index/*');
    expect(resources).not.toContain('semantic-attempts');
  });
});
