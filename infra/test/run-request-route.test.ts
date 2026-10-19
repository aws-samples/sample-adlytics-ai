// SPDX-License-Identifier: MIT-0
/**
 * The run-request routes (S1, owner-gate item 5): two routes behind the existing session
 * authorizer, one admission function with exactly the IAM its path needs, and the requester group.
 */
import { everyTemplate, resourcesOfType } from './support/templates';
import { REQUESTER_GROUP, WRITER_ACTIONS } from '../lib/generated/execution-graph-protocol';
import { everyTemplate as allTemplates } from './support/templates';
import { tracingEnvironment } from '../lib/telemetry';
import { TEST_CONFIG } from './support/config';

const TRACING = tracingEnvironment(TEST_CONFIG);

type Resource = Record<string, unknown>;
const api = everyTemplate().ApiStack as Record<string, unknown>;
const HANDLER = 'adapters.inbound.run_requests.handler.handler';

function runRequestFunction(): Resource {
  const functions = resourcesOfType(api, 'AWS::Lambda::Function').filter(
    (fn) => fn.Handler === HANDLER,
  );
  expect(functions).toHaveLength(1);
  return functions[0]!;
}

function statements(): Resource[] {
  const role = (runRequestFunction().Role as { 'Fn::GetAtt': string[] })['Fn::GetAtt'][0];
  return resourcesOfType(api, 'AWS::IAM::Policy')
    .filter((policy) =>
      ((policy.Roles as { Ref?: string }[] | undefined) ?? []).some((item) => item.Ref === role),
    )
    .flatMap(
      (policy) => ((policy.PolicyDocument as { Statement?: Resource[] }).Statement ?? []),
    );
}

function actionsOf(statement: Resource): string[] {
  const action = statement.Action;
  return (Array.isArray(action) ? action : [action]) as string[];
}

describe('the run-request routes', () => {
  it('creates one arm64 admission function with only its own environment', () => {
    const fn = runRequestFunction();
    expect(fn.FunctionName).toBe('adlytics-dev-RunRequestsFunction');
    expect(fn.Architectures).toEqual(['arm64']);
    const variables = (fn.Environment as { Variables: Record<string, unknown> }).Variables;
    const own = Object.keys(variables).filter((name) => !(name in TRACING));
    expect(own.sort()).toEqual(['ADL_EXECUTION_GRAPH_TABLE', 'ADL_RAW_BUCKET']);
  });

  it('routes exactly POST and GET run-requests through the session authorizer', () => {
    const routes = resourcesOfType(api, 'AWS::ApiGatewayV2::Route').filter((route) =>
      String(route.RouteKey).includes('/operations/run-requests'),
    );
    expect(routes.map((route) => route.RouteKey).sort()).toEqual([
      'GET /api/operations/run-requests/{request_id}',
      'POST /api/operations/run-requests',
    ]);
    for (const route of routes) {
      expect(route.AuthorizationType).toBe('CUSTOM');
      expect(route.AuthorizerId).toBeDefined();
    }
  });

  it('grants the writer actions on the live table and exact-version raw reads only', () => {
    const granted = statements().map((statement) => actionsOf(statement).sort());
    expect(granted).toEqual(
      expect.arrayContaining([
        [...WRITER_ACTIONS].sort(),
        ['s3:GetObject', 's3:GetObjectVersion'],
        ['s3:ListBucket', 's3:ListBucketVersions'],
      ]),
    );
    const all = statements().flatMap(actionsOf);
    for (const forbidden of ['dynamodb:*', 's3:PutObject', 's3:DeleteObject', 'dynamodb:Scan']) {
      expect(all).not.toContain(forbidden);
    }
    expect(all.filter((action) => action.startsWith('cognito-idp:'))).toEqual([]);
    // Span export is the one account-wide grant every function in the stack carries.
    const wildcard = statements().filter((statement) => JSON.stringify(statement.Resource) === '"*"');
    expect(wildcard.flatMap(actionsOf).every((action) => action.startsWith('xray:'))).toBe(true);
  });

  it('creates the requester group in the user pool, and none in the API stack', () => {
    const auth = allTemplates().AuthStack as Record<string, unknown>;
    const groups = resourcesOfType(auth, 'AWS::Cognito::UserPoolGroup').map((group) => group.GroupName);
    expect(groups).toContain(REQUESTER_GROUP);
    expect(resourcesOfType(api, 'AWS::Cognito::UserPoolGroup')).toEqual([]);
  });
});
