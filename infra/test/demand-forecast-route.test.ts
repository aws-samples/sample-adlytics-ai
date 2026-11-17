// SPDX-License-Identifier: MIT-0
/**
 * The demand forecast read routes (ADR-163): two GET routes behind the existing session
 * authorizer, one read function whose only data grant is GetItem on the forecast table's SET# and
 * GEN# keys, its Errors alarm, and route keys equal to the generated API contract. A drift here
 * would serve a route the Python handler refuses, or a role that can write or read S3.
 */
import * as fs from 'node:fs';
import * as path from 'node:path';

import { DEMAND_FORECAST_ROUTES } from '../lib/stacks/api-stack';
import { everyTemplate, resourcesOfType } from './support/templates';

type Resource = Record<string, unknown>;

const api = everyTemplate().ApiStack as Record<string, unknown>;
const HANDLER = 'adapters.inbound.forecast_read.handler.handler';
const GENERATED = path.join(__dirname, '..', '..', 'contracts', 'generated', 'demand-forecast-api.json');

function readFunction(): Resource {
  const functions = resourcesOfType(api, 'AWS::Lambda::Function').filter((fn) => fn.Handler === HANDLER);
  expect(functions).toHaveLength(1);
  return functions[0]!;
}

function statements(): Resource[] {
  const role = (readFunction().Role as { 'Fn::GetAtt': string[] })['Fn::GetAtt'][0];
  return resourcesOfType(api, 'AWS::IAM::Policy')
    .filter((policy) => ((policy.Roles as { Ref?: string }[] | undefined) ?? []).some((item) => item.Ref === role))
    .flatMap((policy) => (policy.PolicyDocument as { Statement?: Resource[] }).Statement ?? []);
}

function actionsOf(statement: Resource): string[] {
  const action = statement.Action;
  return (Array.isArray(action) ? action : [action]) as string[];
}

describe('the demand forecast read routes', () => {
  it('declares the route keys the generated API contract names', () => {
    const generated = JSON.parse(fs.readFileSync(GENERATED, 'utf8')) as { routes: string[] };
    expect([...DEMAND_FORECAST_ROUTES]).toEqual(generated.routes);
  });

  it('routes both keys as GET through the session authorizer', () => {
    const routes = resourcesOfType(api, 'AWS::ApiGatewayV2::Route').filter((route) =>
      String(route.RouteKey).includes('/demand-forecast'),
    );
    expect(routes.map((route) => route.RouteKey).sort()).toEqual([
      'GET /api/demand-forecast',
      'GET /api/demand-forecast/status',
    ]);
    for (const route of routes) {
      expect(route.AuthorizationType).toBe('CUSTOM');
      expect(route.AuthorizerId).toBeDefined();
    }
  });

  it('runs one 10 s, 1,024 MiB function with the reader settings', () => {
    const fn = readFunction();
    expect(fn.FunctionName).toBe('adlytics-dev-DemandForecastReadFunction');
    expect([fn.Timeout, fn.MemorySize]).toEqual([10, 1024]);
    const variables = (fn.Environment as { Variables: Record<string, unknown> }).Variables;
    expect(variables.ADL_DEMAND_FORECAST_CHECK_SCHEDULE).toBe('cron(0 13 * * ? *)');
    expect(variables.ADL_DEMAND_FORECAST_TABLE).toBeDefined();
    expect(variables.ADL_DEMAND_FORECAST_MARKETPLACE_KEY).toBeDefined();
  });

  it('grants only GetItem on the SET# and GEN# keys, with no S3 or write action', () => {
    const data = statements().filter((statement) =>
      actionsOf(statement).some((action) => action.startsWith('dynamodb:') || action.startsWith('s3:')),
    );
    expect(data).toHaveLength(1);
    expect(actionsOf(data[0]!)).toEqual(['dynamodb:GetItem']);
    expect(data[0]!.Condition).toEqual({
      'ForAllValues:StringLike': { 'dynamodb:LeadingKeys': ['SET#*', 'GEN#*'] },
    });
  });

  it('alarms on the function errors', () => {
    const alarms = resourcesOfType(api, 'AWS::CloudWatch::Alarm').filter(
      (alarm) => alarm.AlarmName === 'adlytics-dev-DemandForecastReadFunction-function-errors',
    );
    expect(alarms).toHaveLength(1);
  });
});
