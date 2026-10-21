// SPDX-License-Identifier: MIT-0
/**
 * ADR-148: every Presentation page route has one alarm that only an observed success clears, read
 * back from the synthesized ApiStack.
 *
 * The oracle is literal. The expression, the 17 `{path, operation, page}` triples and the runbook
 * pointer are written out here, independently of the module that builds the alarms, so a change to
 * that module that also changed what it exports could not pass by agreeing with itself.
 */
import {
  PAGE_ALARM_EXPRESSION,
  PAGE_ALARM_RUNBOOK,
  PAGE_ROUTES,
  PageRoute,
  pageRouteKey,
  validatePageRoutes,
} from '../lib/constructs/observability/page-route-alarms';
import { staffedActionIn } from './support/async-failure';
import { everyTemplate } from './support/templates';

type Props = Record<string, unknown>;
type Resource = { Type: string; Properties: Props; DependsOn?: string[] };

/** A served period with no 5xx is `0`; no requests, or only refused ones, is no data point. */
const EXPECTED_EXPRESSION = 'IF((m_count - FILL(m_4xx, 0)) > 0, FILL(m_5xx, 0))';
const EXPECTED_RUNBOOK =
  'docs/capabilities/operational-data-flow-status/operator-tools.md#page-alarms-page-page-server-errors';

/** The 17 page routes, in route-declaration order, each with its `TELEMETRY_OPERATIONS` value. */
const EXPECTED_PAGES: ReadonlyArray<{ path: string; operation: string; page: string }> = [
  ['overview', 'presentation.overview'],
  ['amazon-channel-performance', 'presentation.channel_performance'],
  ['campaigns', 'presentation.campaigns'],
  ['campaign-detail', 'presentation.campaign_detail'],
  ['campaign-metric-focus', 'presentation.campaign_metric_focus'],
  ['portfolio-metric-focus', 'presentation.portfolio_metric_focus'],
  ['retail-metric-focus', 'presentation.retail_metric_focus'],
  ['insight-deep-dive', 'presentation.insight_deep_dive'],
  ['marketplace-comparison', 'presentation.marketplace_comparison'],
  ['data-health', 'presentation.data_health'],
  ['retail-health', 'presentation.retail_health'],
  ['retail-health-product', 'presentation.retail_health_product'],
  ['scorecard', 'presentation.scorecard'],
  ['new-customers', 'presentation.new_customers'],
  ['reach', 'presentation.reach'],
  ['retention', 'presentation.retention'],
  ['audiences', 'presentation.audiences'],
].map(([page, operation]) => ({
  path: `/api/presentation/${page}`,
  operation: operation as string,
  page: page as string,
}));

const api = everyTemplate().ApiStack as Props;
const stackResources = api.Resources as Record<string, Resource>;
const staffed = staffedActionIn(api, { arm: 'published' });

function ofType(type: string): [string, Resource][] {
  return Object.entries(stackResources).filter(([, resource]) => resource.Type === type);
}

function only<T>(items: T[], what: string): T {
  expect(items).toHaveLength(1);
  const [first] = items;
  if (first === undefined) throw new Error(`expected exactly one ${what}`);
  return first;
}

const apiId = only(ofType('AWS::ApiGatewayV2::Api'), 'HTTP API')[0];
const alarms = ofType('AWS::CloudWatch::Alarm').map(([, resource]) => resource.Properties);
const routes = ofType('AWS::ApiGatewayV2::Route');

function alarmNamed(name: string): Props {
  return only(alarms.filter((alarm) => alarm.AlarmName === name), `alarm ${name}`);
}

function routeIdFor(path: string): string {
  return only(
    routes.filter(([, route]) => route.Properties.RouteKey === `POST ${path}`),
    `route POST ${path}`,
  )[0];
}

function byName(dimensions: unknown): Props[] {
  return [...(dimensions as Props[])].sort((a, b) =>
    String(a.Name).localeCompare(String(b.Name)),
  );
}

/** The exact page alarm for `page`: throws on any difference. */
function expectPageAlarm(alarm: Props, page: (typeof EXPECTED_PAGES)[number]): void {
  expect(alarm).toEqual(expect.objectContaining({
    AlarmName: `adlytics-dev-page-${page.page}-server-errors`,
    TreatMissingData: 'ignore',
    Threshold: 1,
    ComparisonOperator: 'GreaterThanOrEqualToThreshold',
    EvaluationPeriods: 1,
    DatapointsToAlarm: 1,
  }));
  expect(alarm.ActionsEnabled).toBeUndefined();
  expect(alarm.AlarmActions).toStrictEqual([staffed]);
  expect(alarm.OKActions).toStrictEqual([staffed]);
  for (const property of ['MetricName', 'Namespace', 'Statistic']) {
    expect(alarm).not.toHaveProperty(property);
  }

  const metrics = Object.fromEntries(
    ((alarm.Metrics ?? []) as Props[]).map((metric) => [String(metric.Id), metric]),
  );
  expect(Object.keys(metrics).sort()).toEqual(['expr_1', 'm_4xx', 'm_5xx', 'm_count']);
  expect(metrics.expr_1).toStrictEqual({
    Expression: EXPECTED_EXPRESSION,
    Id: 'expr_1',
    ReturnData: true,
  });
  const dimensions = [
    { Name: 'ApiId', Value: { Ref: apiId } },
    { Name: 'Method', Value: 'POST' },
    { Name: 'Resource', Value: page.path },
    { Name: 'Stage', Value: '$default' },
  ];
  for (const [id, metricName] of [['m_count', 'Count'], ['m_4xx', '4xx'], ['m_5xx', '5xx']]) {
    const metric = metrics[id as string] as Props;
    const stat = metric.MetricStat as Props;
    const rendered = stat.Metric as Props;
    expect(metric).toStrictEqual({
      Id: id,
      ReturnData: false,
      MetricStat: {
        Metric: { Namespace: 'AWS/ApiGateway', MetricName: metricName, Dimensions: rendered.Dimensions },
        Period: 300,
        Stat: 'Sum',
      },
    });
    expect(byName(rendered.Dimensions)).toStrictEqual(dimensions);
  }

  const description = String(alarm.AlarmDescription);
  for (const needle of ['OK', 'INSUFFICIENT_DATA', 'ADR-148', EXPECTED_RUNBOOK]) {
    expect(description).toContain(needle);
  }
}

describe('page route alarms (ADR-148)', () => {
  it('gives every page exactly one alarm that holds until an observed success clears it', () => {
    for (const page of EXPECTED_PAGES) {
      expectPageAlarm(alarmNamed(`adlytics-dev-page-${page.page}-server-errors`), page);
    }
  });

  it('positive control: the page-alarm assertion refuses each defect the design removes', () => {
    const reach = EXPECTED_PAGES.find((page) => page.page === 'reach')!;
    const alarm = alarmNamed('adlytics-dev-page-reach-server-errors');
    // The unmodified alarm passes, so each throw below is caused by its one mutation.
    expect(() => expectPageAlarm(alarm, reach)).not.toThrow();
    const { OKActions: _ok, ...withoutOk } = alarm;
    for (const defect of [
      { ...alarm, TreatMissingData: 'notBreaching' },
      withoutOk,
      {
        ...alarm,
        Metrics: (alarm.Metrics as Props[]).map((metric) =>
          metric.Id === 'expr_1' ? { ...metric, Expression: 'FILL(m_5xx, 0)' } : metric,
        ),
      },
    ]) {
      expect(() => expectPageAlarm(defect, reach)).toThrow();
    }
  });

  it('builds exactly 17 page alarms', () => {
    const pageAlarms = alarms.filter((alarm) =>
      /^adlytics-dev-page-.+-server-errors$/.test(String(alarm.AlarmName)),
    );
    expect(pageAlarms).toHaveLength(17);
    expect(EXPECTED_PAGES).toHaveLength(17);
  });

  it('enables detailed metrics on exactly the 17 page routes, each a synthesized route key', () => {
    const [, stage] = only(ofType('AWS::ApiGatewayV2::Stage'), 'stage');
    const settings = stage.Properties.RouteSettings as Record<string, unknown> | undefined;
    const expectedKeys = EXPECTED_PAGES.map((page) => `POST ${page.path}`);
    expect(Object.keys(settings ?? {}).sort()).toEqual([...expectedKeys].sort());
    for (const key of expectedKeys) {
      expect(settings?.[key]).toStrictEqual({ DetailedMetricsEnabled: true });
    }
    const routeKeys = new Set(routes.map(([, route]) => route.Properties.RouteKey));
    for (const key of Object.keys(settings ?? {})) expect(routeKeys.has(key)).toBe(true);
    const defaults = stage.Properties.DefaultRouteSettings as Props | undefined;
    expect(defaults?.DetailedMetricsEnabled).toBeUndefined();
  });

  it('makes the stage depend on every page route, so its settings never name a missing route', () => {
    const [, stage] = only(ofType('AWS::ApiGatewayV2::Stage'), 'stage');
    const dependsOn = stage.DependsOn ?? [];
    for (const page of EXPECTED_PAGES) expect(dependsOn).toContain(routeIdFor(page.path));
  });

  it('exports exactly the literal oracle', () => {
    expect(PAGE_ALARM_EXPRESSION).toBe(EXPECTED_EXPRESSION);
    expect(PAGE_ALARM_RUNBOOK).toBe(EXPECTED_RUNBOOK);
    expect(PAGE_ROUTES).toStrictEqual(EXPECTED_PAGES);
    for (const page of EXPECTED_PAGES) {
      const key = pageRouteKey(page.path);
      expect(key).toBe(`POST ${page.path}`);
      expect(key.split(' ')[1]).toBe(page.path);
      // The key is a synthesized route's RouteKey, so it names a route that exists.
      expect(routeIdFor(page.path)).toBeDefined();
    }
  });

  describe('positive controls: the module-load roster guard', () => {
    const reach = PAGE_ROUTES.find((route) => route.page === 'reach') as PageRoute;
    const withReach = (change: Partial<PageRoute>): PageRoute[] =>
      PAGE_ROUTES.map((route) => (route === reach ? { ...route, ...change } : route));

    it('accepts the real roster', () => {
      expect(() => validatePageRoutes(PAGE_ROUTES)).not.toThrow();
    });

    it('refuses a duplicate path, operation or page', () => {
      expect(() => validatePageRoutes([...PAGE_ROUTES, reach])).toThrow(/path .* is not unique/);
      expect(() => validatePageRoutes(withReach({ operation: 'presentation.retention' })))
        .toThrow(/operation presentation.retention is not unique/);
      expect(() => validatePageRoutes([
        ...PAGE_ROUTES,
        { path: '/api/presentation/other/reach', operation: 'presentation.other', page: 'reach' },
      ])).toThrow(/page reach is not unique/);
    });

    it('refuses a path outside the presentation prefix', () => {
      expect(() => validatePageRoutes(withReach({ path: '/api/operations/reach' })))
        .toThrow(/is not under \/api\/presentation\//);
    });

    it('refuses a page that is not the last path segment', () => {
      expect(() => validatePageRoutes(withReach({ page: 'reach-and-funnel' })))
        .toThrow(/is not the last segment/);
    });

    it('refuses an operation the catch-all could not exclude as a literal', () => {
      expect(() => validatePageRoutes(withReach({ operation: "presentation.reach' OR '" })))
        .toThrow(/cannot be a Metrics Insights literal/);
    });
  });
});
