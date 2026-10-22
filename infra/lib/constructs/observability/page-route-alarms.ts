// SPDX-License-Identifier: MIT-0
import * as cdk from 'aws-cdk-lib';
import * as apigwv2 from 'aws-cdk-lib/aws-apigatewayv2';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import { Construct } from 'constructs';
import { API_PREFIX, AppConfig } from '../../config';
import { alarmingErrorsQuery } from './error-alarms';
/**
 * ADR-148: every Presentation page route has one alarm that only an observed success clears.
 *
 * The alarm reads the route's built-in `AWS/ApiGateway` `Count`, `4xx` and `5xx` (detailed route
 * metrics, enabled on exactly these routes by `ApiStack`). Its value is the route's `5xx` Sum,
 * reported only for a period in which the route answered at least one request it did not refuse
 * with a 4xx. A period with no requests, or only refused ones, yields no data point, and
 * `TreatMissingData.IGNORE` holds the state, so a broken page with no callers stays in `ALARM`.
 * The operation catch-all excludes `PAGE_OPERATIONS` (`ingestion-stack.ts`), so a page failure
 * pages once, through its page alarm.
 */

/** One Presentation page route: its path, its `TELEMETRY_OPERATIONS` value and its page name. */
export type PageRoute = {
  readonly path: string;
  readonly operation: string;
  readonly page: string;
};

/**
 * The 17 page routes, in route-declaration order. Each `operation` is the route's value in
 * `TELEMETRY_OPERATIONS` (`src/adapters/inbound/presentation_handlers/handler.py`), which
 * `alarm-coverage.test.ts` check 8 compares against this list.
 */
export const PAGE_ROUTES: readonly PageRoute[] = [
  { path: `${API_PREFIX}/presentation/overview`, operation: 'presentation.overview', page: 'overview' },
  {
    path: `${API_PREFIX}/presentation/amazon-channel-performance`,
    operation: 'presentation.channel_performance',
    page: 'amazon-channel-performance',
  },
  { path: `${API_PREFIX}/presentation/campaigns`, operation: 'presentation.campaigns', page: 'campaigns' },
  {
    path: `${API_PREFIX}/presentation/campaign-detail`,
    operation: 'presentation.campaign_detail',
    page: 'campaign-detail',
  },
  {
    path: `${API_PREFIX}/presentation/campaign-metric-focus`,
    operation: 'presentation.campaign_metric_focus',
    page: 'campaign-metric-focus',
  },
  {
    path: `${API_PREFIX}/presentation/portfolio-metric-focus`,
    operation: 'presentation.portfolio_metric_focus',
    page: 'portfolio-metric-focus',
  },
  {
    path: `${API_PREFIX}/presentation/retail-metric-focus`,
    operation: 'presentation.retail_metric_focus',
    page: 'retail-metric-focus',
  },
  {
    path: `${API_PREFIX}/presentation/insight-deep-dive`,
    operation: 'presentation.insight_deep_dive',
    page: 'insight-deep-dive',
  },
  {
    path: `${API_PREFIX}/presentation/marketplace-comparison`,
    operation: 'presentation.marketplace_comparison',
    page: 'marketplace-comparison',
  },
  { path: `${API_PREFIX}/presentation/data-health`, operation: 'presentation.data_health', page: 'data-health' },
  {
    path: `${API_PREFIX}/presentation/retail-health`,
    operation: 'presentation.retail_health',
    page: 'retail-health',
  },
  // ADR-125: Retail health product detail, a baked child of retail-health. It replaces the
  // retired request-time retail-product route, so there is never a second path.
  {
    path: `${API_PREFIX}/presentation/retail-health-product`,
    operation: 'presentation.retail_health_product',
    page: 'retail-health-product',
  },
  // UI slice 9a: the Scorecard, owner-approved for dev 2026-10-10. Its metric focus route
  // moved to slice 9b with the focus itself.
  { path: `${API_PREFIX}/presentation/scorecard`, operation: 'presentation.scorecard', page: 'scorecard' },
  // UI slice 9, New customers sub-slice: the Customers hub's first tab, owner-approved for
  // dev 2026-10-09 (S-9). It reads only its own baked bodies and pointers.
  {
    path: `${API_PREFIX}/presentation/new-customers`,
    operation: 'presentation.new_customers',
    page: 'new-customers',
  },
  // Reach and funnel is a parent-only calendar page (D3, O3). Its metric focus route is
  // separate work, so it has none yet.
  { path: `${API_PREFIX}/presentation/reach`, operation: 'presentation.reach', page: 'reach' },
  // Retention is a parent-only vintage page. It deliberately has no metric-focus route.
  { path: `${API_PREFIX}/presentation/retention`, operation: 'presentation.retention', page: 'retention' },
  // Audiences is a parent-only single-month page. It deliberately has no metric-focus route.
  { path: `${API_PREFIX}/presentation/audiences`, operation: 'presentation.audiences', page: 'audiences' },
];

/** The two `TELEMETRY_OPERATIONS` entries that no HTTP API route serves. */
export const UNROUTED_PRESENTATION_PATHS = [
  `${API_PREFIX}/presentation/media-efficiency/scopes`,
  `${API_PREFIX}/presentation/media-efficiency/artifact`,
] as const;

/** The operations the page alarms own, so the catch-all excludes exactly these. */
export const PAGE_OPERATIONS: readonly string[] = PAGE_ROUTES.map((route) => route.operation);

/** The HTTP API route key of a page route, which is also its `RouteSettings` key. */
export function pageRouteKey(path: string): string {
  return `POST ${path}`;
}

/** A served period with no 5xx is `0`; no requests, or only refused ones, is no data point. */
export const PAGE_ALARM_EXPRESSION = 'IF((m_count - FILL(m_4xx, 0)) > 0, FILL(m_5xx, 0))';

/** The operator-tools entry every page alarm's description names. */
export const PAGE_ALARM_RUNBOOK =
  'docs/capabilities/operational-data-flow-status/operator-tools.md'
  + '#page-alarms-page-page-server-errors';

/**
 * Throws unless `routes` is a valid page roster: unique paths, operations and pages; every path
 * under `${API_PREFIX}/presentation/` with its page as the last segment; and every operation a
 * literal `alarmingErrorsQuery` accepts, so the catch-all can exclude it.
 */
export function validatePageRoutes(routes: readonly PageRoute[]): void {
  for (const key of ['path', 'operation', 'page'] as const) {
    const values = routes.map((route) => route[key]);
    const duplicate = values.find((value, index) => values.indexOf(value) !== index);
    if (duplicate !== undefined) throw new Error(`Page route ${key} ${duplicate} is not unique.`);
  }
  const prefix = `${API_PREFIX}/presentation/`;
  for (const route of routes) {
    if (!route.path.startsWith(prefix)) {
      throw new Error(`Page route ${route.path} is not under ${prefix}.`);
    }
    if (route.path.split('/').at(-1) !== route.page) {
      throw new Error(`Page ${route.page} is not the last segment of ${route.path}.`);
    }
    alarmingErrorsQuery(route.operation);
  }
}

validatePageRoutes(PAGE_ROUTES);

/**
 * The page alarm for one route: `ALARM` on any 5xx in a served period, `OK` only on a served
 * period with none, and the state held through silence and through refusal-only periods. It acts
 * on `action` on both `ALARM` and `OK`, so the operator learns of the recovery from an observed
 * success.
 */
export function buildPageRouteAlarm(
  scope: Construct,
  config: AppConfig,
  // Only the id: `HttpApi` does not satisfy `IHttpApi` under `exactOptionalPropertyTypes`.
  api: Pick<apigwv2.IHttpApi, 'apiId'>,
  route: PageRoute,
  action: cloudwatch.IAlarmAction,
): cloudwatch.Alarm {
  const routeMetric = (metricName: string): cloudwatch.Metric => new cloudwatch.Metric({
    namespace: 'AWS/ApiGateway',
    metricName,
    dimensionsMap: { ApiId: api.apiId, Stage: '$default', Method: 'POST', Resource: route.path },
    statistic: cloudwatch.Stats.SUM,
    period: cdk.Duration.minutes(5),
  });
  const alarm = new cloudwatch.Alarm(scope, `PageServerErrors-${route.page}`, {
    alarmName: `adlytics-${config.envName}-page-${route.page}-server-errors`,
    alarmDescription:
      `Presentation page ${route.page} (POST ${route.path}) answered a request with a 5xx `
      + '(ADR-148). The alarm returns to OK only after a 5-minute period in which the page '
      + 'answered at least one request without a 4xx refusal and returned no 5xx; with no '
      + 'requests, or only refused ones, the state holds. INSUFFICIENT_DATA means the page has '
      + 'not been served since the alarm was created. Read ErrorClass and ErrorCode with the '
      + `catch-all drill-down query for Operation = '${route.operation}'. `
      + `Runbook: ${PAGE_ALARM_RUNBOOK}.`,
    metric: new cloudwatch.MathExpression({
      expression: PAGE_ALARM_EXPRESSION,
      usingMetrics: { m_count: routeMetric('Count'), m_4xx: routeMetric('4xx'), m_5xx: routeMetric('5xx') },
      period: cdk.Duration.minutes(5),
    }),
    threshold: 1,
    comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
    evaluationPeriods: 1,
    datapointsToAlarm: 1,
    treatMissingData: cloudwatch.TreatMissingData.IGNORE,
  });
  alarm.addAlarmAction(action);
  alarm.addOkAction(action);
  return alarm;
}
