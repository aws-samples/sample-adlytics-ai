// SPDX-License-Identifier: MIT-0
import assert from 'node:assert/strict';
import { test } from 'vitest';

import generated from '../generated/route-manifest.json';
import { documentTitle, locateRoute, parseRouteManifest, rootTab, visibleHubs } from '../lib/route-manifest';

function copy(): Record<string, unknown> & { hubs: Array<Record<string, unknown> & { tabs: Array<Record<string, unknown>> }> } {
  return structuredClone(generated) as never;
}

test('the committed route manifest decodes, and Prompt administration needs its session field', () => {
  assert.deepEqual(visibleHubs({}).map((hub) => hub.hub_id), ['overview', 'performance', 'reach', 'customers', 'retail']);
  assert.deepEqual(visibleHubs({ can_manage_prompts: true }).map((hub) => hub.hub_id), ['overview', 'performance', 'reach', 'customers', 'retail', 'operations']);
  // Data pipeline status is dark: no manifest entry, so its field lists nothing
  // (test/data-pipeline-status.test.tsx holds the positive control).
  assert.deepEqual(visibleHubs({ can_read_operational_status: true }).map((hub) => hub.hub_id), ['overview', 'performance', 'reach', 'customers', 'retail']);
  const reach = visibleHubs({}).find((hub) => hub.hub_id === 'reach');
  assert.deepEqual(reach?.tabs.map((tab) => ({ description: tab.description, label: tab.label, order: tab.order, route: tab.route, tab_id: tab.tab_id })), [{
    description: 'How many shoppers your ads reached, how often, and what they did next',
    label: 'Reach and funnel',
    order: 1,
    route: '/reach/',
    tab_id: 'reach',
  }, {
    description: 'Which Amazon audience segments your buyers belong to, compared with the shoppers your ads reached, and how DSP campaigns did per segment',
    label: 'Audiences',
    order: 2,
    route: '/audiences/',
    tab_id: 'audiences',
  }]);
});

test('routes resolve with or without the trailing slash, and the root resolves to its owner tab', () => {
  assert.equal(locateRoute('/retail-health')?.tab.tab_id, 'retail-health');
  assert.equal(locateRoute('/')?.tab.tab_id, 'overview');
  assert.equal(locateRoute('/')?.tab.kind, 'composed');
  assert.equal(locateRoute('/channel-performance/'), null);
  assert.equal(rootTab().label, 'Growth check');
  // UI refresh 3.8: a tab carries its customer question, or null.
  assert.equal(rootTab().question, 'Is your advertising growing your business?');
  assert.equal(locateRoute('/products-and-ads/')?.tab.kind, 'composed');
  assert.equal(locateRoute('/products-and-ads/')?.tab.question, null);
});

test('an exported index.html locates as its route, as the edge serves it; other dotted paths do not', () => {
  // ADR-123: the edge passes a dotted leaf through, so `<route>index.html` serves the route's page.
  assert.equal(locateRoute('/index.html')?.tab.tab_id, 'overview');
  assert.equal(locateRoute('/campaigns/index.html')?.tab.tab_id, 'campaigns');
  assert.equal(locateRoute('/retail-health/index.html')?.tab.tab_id, 'retail-health');
  assert.equal(locateRoute('/reach/index.html')?.tab.tab_id, 'reach');
  assert.equal(locateRoute('/audiences/index.html')?.tab.tab_id, 'audiences');
  assert.equal(locateRoute('/admin/prompts/index.html')?.tab.tab_id, 'prompt-admin');
  assert.equal(locateRoute('/channel-performance/index.html'), null);
  assert.equal(locateRoute('/404.html'), null);
  assert.equal(locateRoute('/campaignsindex.html'), null);
});

test('document titles follow ia §3.5', () => {
  assert.equal(documentTitle(locateRoute('/campaigns/')), 'Campaigns · Performance · Adlytics AI');
  assert.equal(documentTitle(locateRoute('/retail-health/')), 'Retail health · Adlytics AI');
  assert.equal(documentTitle(locateRoute('/new-customers/')), 'New customers · Customers · Adlytics AI');
  assert.equal(documentTitle(locateRoute('/reach/')), 'Reach and funnel · Reach and audiences · Adlytics AI');
  assert.equal(documentTitle(locateRoute('/audiences/')), 'Audiences · Reach and audiences · Adlytics AI');
  assert.equal(documentTitle(locateRoute('/admin/prompts/')), 'Prompt administration · Operations · Adlytics AI');
  assert.equal(documentTitle(locateRoute('/')), 'Growth check · Overview · Adlytics AI');
  assert.equal(documentTitle(null), 'Page not found · Adlytics AI');
});

test('the decoder refuses a hub without an icon, a mismatched entitlement and an unrendered page tab', () => {
  // Hubs are found by id, never by position: inserting a hub must not retarget a control.
  const hubOf = (manifest: ReturnType<typeof copy>, hubId: string) => manifest.hubs.find((hub) => hub.hub_id === hubId)!;
  const unknownHub = copy();
  hubOf(unknownHub, 'performance').hub_id = 'marketing';
  assert.throws(() => parseRouteManifest(unknownHub), /hub id has an unknown value/);

  const openOperator = copy();
  hubOf(openOperator, 'operations').tabs[0]!.entitlement = null;
  assert.throws(() => parseRouteManifest(openOperator), /entitlement does not match/);

  const unrendered = copy();
  hubOf(unrendered, 'performance').tabs[0]!.tab_id = 'channel-performance';
  assert.throws(() => parseRouteManifest(unrendered), /not a rendered page/);

  // ADR-151: a composed tab is one this frontend renders, never an unknown view.
  const unknownComposed = copy();
  hubOf(unknownComposed, 'overview').tabs[0]!.tab_id = 'growth-forecast';
  assert.throws(() => parseRouteManifest(unknownComposed), /composed tab is not a rendered view/);

  const extra = copy();
  extra.unexpected = true;
  assert.throws(() => parseRouteManifest(extra), /invalid fields/);
});
