// SPDX-License-Identifier: MIT-0
/**
 * ADR-078: one review function behind two exact POST routes. Its exact DynamoDB grants are
 * asserted by record-family-storage.test.ts; this file owns the routes, the function, and what the
 * role must never hold.
 */
import { paramPath, qualityReviewFunctionName, qualityReviewFunctionRoleName } from '../lib/config';
import { roleGrants } from './support/iam';
import { everyTemplate, resourcesOfType } from './support/templates';

const api = everyTemplate().ApiStack as Record<string, unknown>;
const routes = () => resourcesOfType(api, 'AWS::ApiGatewayV2::Route');
const reviewFunction = (): Record<string, unknown> => {
  const matches = resourcesOfType(api, 'AWS::Lambda::Function').filter(
    (fn) => fn.FunctionName === qualityReviewFunctionName('dev'),
  );
  expect(matches).toHaveLength(1);
  return matches[0] as Record<string, unknown>;
};
const variables = (): Record<string, unknown> =>
  (reviewFunction().Environment as { Variables: Record<string, unknown> }).Variables;

describe('the protected quality review routes', () => {
  it('writes the stage access log with the reviewer keys and never the subject (ADR-081 §4)', () => {
    const stages = resourcesOfType(api, 'AWS::ApiGatewayV2::Stage');
    expect(stages).toHaveLength(1);
    const settings = stages[0]?.AccessLogSettings as { DestinationArn: unknown; Format: string };
    // The destination is the deploy-time SSM value AgentQualityStack publishes, and nothing else.
    const ref = (settings.DestinationArn as { Ref?: string }).Ref ?? '';
    const parameters = (api.Parameters ?? {}) as Record<string, { Type?: string; Default?: string }>;
    expect(parameters[ref]).toStrictEqual({
      Type: 'AWS::SSM::Parameter::Value<String>',
      Default: paramPath('dev', 'agentQualityReviewerAccessLogGroupArn'),
    });
    const format = JSON.parse(settings.Format) as Record<string, string>;
    expect(format).toStrictEqual({
      requestId: '$context.requestId',
      routeKey: '$context.routeKey',
      status: '$context.status',
      reviewer: '$context.authorizer.reviewer',
      attribution: '$context.authorizer.attribution',
      authorizerError: '$context.authorizer.error',
      responseLatency: '$context.responseLatency',
      integrationLatency: '$context.integrationLatency',
    });
    // Never the general subject, a selector or correlation, content, a header, or a path.
    expect(settings.Format).not.toMatch(/subject|correlation|audit_id|request\.|header|path|identity|body/i);
  });
  it('declares exactly the turn and turns routes, both behind the session authorizer', () => {
    const review = routes().filter((route) => String(route.RouteKey).includes('/quality-review/'));
    expect(review.map((route) => route.RouteKey).sort()).toEqual([
      'POST /api/quality-review/turn',
      'POST /api/quality-review/turns',
    ]);
    for (const route of review) {
      expect(route).toEqual(expect.objectContaining({ AuthorizationType: 'CUSTOM' }));
    }
    // One integration serves both: the function routes by route key.
    expect(new Set(review.map((route) => JSON.stringify(route.Target))).size).toBe(1);
  });

  it('runs one review function with the consolidated handler and no route selector', () => {
    const fn = reviewFunction();
    expect(fn.Handler).toBe('adapters.inbound.quality_review.handler.handler');
    expect(variables()).not.toHaveProperty('ADL_QUALITY_REVIEW_ROUTE');
    expect(variables().ADL_EVALUATION_RESULT_DEADLINE_SECONDS).toBe('1800');
    const retired = resourcesOfType(api, 'AWS::Lambda::Function').filter((candidate) =>
      /QualityReview(Audit|Findings|Evaluation)Function/.test(String(candidate.FunctionName)));
    expect(retired).toEqual([]);
  });

  it('gives the review role no scan, write, log-read, or queue permission', () => {
    // Its inline grants are keyed reads, span export and stream writes on its own log group
    // (ADR-085; log-write-scope.test.ts pins the shape); nothing else, in any service.
    const actions = new Set(
      [...roleGrants(api, qualityReviewFunctionRoleName('dev'))].map((grant) => grant.split(' ', 1)[0]),
    );
    expect(actions).toStrictEqual(new Set([
      'dynamodb:GetItem',
      'dynamodb:Query',
      'logs:CreateLogStream',
      'logs:PutLogEvents',
      'xray:PutTraceSegments',
      'xray:PutTelemetryRecords',
    ]));
  });
});
