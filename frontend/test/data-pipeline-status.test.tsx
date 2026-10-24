// SPDX-License-Identifier: MIT-0
/**
 * Operations › Data pipeline status (ADR-115 decision 8). The view's three states, each beside the
 * state that differs from it by one field, and the page's darkness: it is listed and routed only
 * once `presentation/navigation.yaml` carries its entry, which it doesn't yet.
 */
import assert from 'node:assert/strict';
import { render, screen, within } from '@testing-library/react';
import { afterEach, test } from 'vitest';
import { cleanup } from '@testing-library/react';

import generated from '../generated/route-manifest.json';
import {
  DataPipelineStatusView,
  pipelineStatusAccess,
  type FeedStatusTable,
} from '../components/DataPipelineStatusView';
import { locateRoute, parseRouteManifest, routeManifest, visibleHubs } from '../lib/route-manifest';

afterEach(cleanup);

const ROUTE = '/admin/status/';
const NOT_SET_UP = "Data pipeline status isn't set up in this deployment.";
const NO_ACCESS = "You don't have access to this page";

/** Fictional, from the mockup's snapshot. */
const TABLE: FeedStatusTable = {
  checked: 'Checked Sep 16, 2026, 09:05 UTC. Fictional.',
  rows: [
    {
      feed: 'Amazon Ads reports',
      data_through: 'Sep 15, 2026',
      last_landed: 'Sep 16, 06:10 UTC',
      timeliness: { label: 'On time', tone: 'neutral' },
      gap: { pill: '1 gap', detail: 'Sep 13 viewability not yet sent for 1 Amazon DSP campaign (Campaigns, campaign detail).' },
      pages: 'Overview, Scorecard, Campaigns, New customers, Budget simulator',
    },
    {
      feed: 'Vendor Central sales, traffic and returns',
      data_through: 'Sep 11, 2026',
      last_landed: 'Sep 12, 07:40 UTC',
      timeliness: { label: '4 days late', tone: 'warn' },
      gap: null,
      pages: 'Retail health, Overview, Scorecard (TACoS)',
    },
  ],
};

function headings(): string[] {
  return screen.queryAllByRole('heading').map((heading) => heading.textContent ?? '');
}

test('access: not provisioned, whatever the entitlement says', () => {
  assert.equal(pipelineStatusAccess({ operationalStatusProvisioned: false, canReadOperationalStatus: false }), 'not-provisioned');
  // Positive control: provisioning alone moves it.
  assert.equal(pipelineStatusAccess({ operationalStatusProvisioned: true, canReadOperationalStatus: false }), 'no-access');
});

test('access: provisioned without the entitlement has no access', () => {
  assert.equal(pipelineStatusAccess({ operationalStatusProvisioned: true, canReadOperationalStatus: false }), 'no-access');
  // Positive control: the entitlement alone moves it.
  assert.equal(pipelineStatusAccess({ operationalStatusProvisioned: true, canReadOperationalStatus: true }), 'granted');
});

test('access: provisioned with the entitlement is granted', () => {
  assert.equal(pipelineStatusAccess({ operationalStatusProvisioned: true, canReadOperationalStatus: true }), 'granted');
  // Positive control: without provisioning the same entitlement grants nothing.
  assert.equal(pipelineStatusAccess({ operationalStatusProvisioned: false, canReadOperationalStatus: true }), 'not-provisioned');
});

test('not provisioned: the set-up absence and Go to the root tab, nothing else', () => {
  render(<DataPipelineStatusView access="not-provisioned" />);
  assert.deepEqual(headings(), [NOT_SET_UP]);
  assert.ok(screen.getByText("This page reads an operations API that this deployment doesn't include."));
  assert.equal(screen.getByRole('link', { name: 'Go to Growth check' }).getAttribute('href'), '/');
  assert.equal(screen.queryByRole('table'), null);
  cleanup();
  // Positive control: the no-access state shows its own heading in the same place.
  render(<DataPipelineStatusView access="no-access" />);
  assert.deepEqual(headings(), [NO_ACCESS]);
});

test('provisioned without access: the access absence and Go to the root tab, nothing else', () => {
  render(<DataPipelineStatusView access="no-access" />);
  assert.deepEqual(headings(), [NO_ACCESS]);
  assert.ok(screen.getByText('Data pipeline status is for operators in the operations group.'));
  assert.equal(screen.getByRole('link', { name: 'Go to Growth check' }).getAttribute('href'), '/');
  assert.equal(screen.queryByRole('table'), null);
  cleanup();
  // Positive control: the granted state renders the table this one withholds.
  render(<DataPipelineStatusView access="granted" table={TABLE} />);
  assert.ok(screen.getByRole('table'));
});

test('provisioned with access: the feed table from server text, with no absence callout', () => {
  render(<DataPipelineStatusView access="granted" table={TABLE} />);
  assert.deepEqual(headings(), []);
  const region = screen.getByRole('region', { name: 'Data feeds' });
  assert.ok(within(region).getByText('Checked Sep 16, 2026, 09:05 UTC. Fictional.'));
  assert.deepEqual(
    within(region).getAllByRole('columnheader').map((cell) => cell.textContent),
    ['Feed', 'Data through', 'Last landed', 'Status', 'Pages it feeds'],
  );
  const rows = within(region).getAllByRole('row').slice(1);
  assert.deepEqual(rows.map((row) => within(row).getByRole('rowheader').textContent), [
    'Amazon Ads reports',
    'Vendor Central sales, traffic and returns',
  ]);
  const [ads, vendor] = rows;
  assert.deepEqual(
    within(ads!).getAllByRole('cell').map((cell) => [cell.getAttribute('data-label'), cell.textContent]),
    [
      ['Data through', 'Sep 15, 2026'],
      ['Last landed', 'Sep 16, 06:10 UTC'],
      ['Status', 'On time1 gapSep 13 viewability not yet sent for 1 Amazon DSP campaign (Campaigns, campaign detail).'],
      ['Pages it feeds', 'Overview, Scorecard, Campaigns, New customers, Budget simulator'],
    ],
  );
  assert.equal(within(ads!).getByText('On time').className, 'pill');
  assert.equal(within(vendor!).getByText('4 days late').className, 'pill warn');
  assert.equal(within(vendor!).queryByText('1 gap'), null);
  assert.ok(screen.getByText('More detail per feed (events, retries) arrives in later slices of the platform operations design.'));
  cleanup();
  // Positive control: an absence state does show its callout heading.
  render(<DataPipelineStatusView access="not-provisioned" />);
  assert.deepEqual(headings(), [NOT_SET_UP]);
});

function withPipelineStatusEntry(): unknown {
  const manifest = structuredClone(generated) as { hubs: Array<{ hub_id: string; tabs: unknown[] }> };
  manifest.hubs.find((hub) => hub.hub_id === 'operations')!.tabs.push({
    description: 'Whether each data feed is landing, and which pages are current',
    entitlement: { group: 'operations-status-read', session_field: 'can_read_operational_status' },
    kind: 'static',
    label: 'Data pipeline status',
    order: 2, question: null,
    route: ROUTE,
    tab_id: 'pipeline-status',
  });
  return manifest;
}

function routes(hubs: ReturnType<typeof visibleHubs>): string[] {
  return hubs.flatMap((hub) => hub.tabs.map((tab) => tab.route));
}

test('dark: the committed manifest lists and routes no Data pipeline status, even for a granted operator', () => {
  const everything = { can_manage_prompts: true, can_read_operational_status: true };
  assert.equal(routes(visibleHubs(everything)).includes(ROUTE), false);
  assert.equal(locateRoute(ROUTE), null);
  assert.equal(routeManifest.hubs.flatMap((hub) => hub.tabs).some((tab) => tab.tab_id === 'pipeline-status'), false);
  // Positive control: the same manifest with the entry lists it for the granted session only.
  const lit = parseRouteManifest(withPipelineStatusEntry());
  assert.equal(routes(visibleHubs(everything, lit)).includes(ROUTE), true);
  assert.equal(locateRoute(ROUTE, lit)?.tab.label, 'Data pipeline status');
  assert.equal(routes(visibleHubs({ can_manage_prompts: true, can_read_operational_status: false }, lit)).includes(ROUTE), false);
  assert.deepEqual(
    visibleHubs({ can_read_operational_status: false }, lit).map((hub) => hub.hub_id),
    ['overview', 'performance', 'reach', 'customers', 'retail'],
  );
});
