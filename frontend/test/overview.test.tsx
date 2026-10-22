// SPDX-License-Identifier: MIT-0
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { cleanup, render, screen, within } from '@testing-library/react';
import { afterEach, test } from 'vitest';

import { VisibleTabsContext } from '@/components/NextStepContext';
import { overviewRouteAdapter } from '@/components/OverviewRouteAdapter';
import { OverviewView, sourceIssueText, type OverviewSources } from '@/components/OverviewView';
import { overviewAnswer, throughLines } from '@/lib/overview';
import {
  campaignPresentationV2,
  newCustomersPresentationV2,
  parsePresentationResultCalendarV2,
  parsePresentationResultV2,
  retailHealthPresentationV2,
  scorecardPresentationV2,
  type CampaignParentPresentation,
} from '@/lib/presentation-v2';
import { routeManifest } from '@/lib/route-manifest';

type Json = Record<string, unknown>;
const FIXTURES = join(__dirname, 'fixtures');
const fixture = (name: string): Json => JSON.parse(readFileSync(join(FIXTURES, name), 'utf8')) as Json;
const BODIES: Record<string, string> = {
  campaigns: 'campaigns-all-v2.json',
  'new-customers': 'new-customers-7d-v2.json',
  'retail-health': 'retail-health-v2.json',
  scorecard: 'scorecard-7d-v2.json',
};
const TABS = routeManifest.hubs.flatMap((hub) => hub.tabs);

function served(): OverviewSources {
  const scorecard = scorecardPresentationV2(parsePresentationResultCalendarV2(fixture(BODIES.scorecard!), 'scorecard'));
  const retail = retailHealthPresentationV2(parsePresentationResultV2(fixture(BODIES['retail-health']!), 'retail-health'));
  const newCustomers = newCustomersPresentationV2(parsePresentationResultCalendarV2(fixture(BODIES['new-customers']!), 'new-customers'));
  const campaigns = campaignPresentationV2(parsePresentationResultV2(fixture(BODIES.campaigns!), 'campaigns')) as CampaignParentPresentation;
  return {
    campaigns: { kind: 'served', presentation: campaigns },
    newCustomers: { kind: 'served', presentation: newCustomers },
    retail: { kind: 'served', presentation: retail },
    scorecard: { kind: 'served', presentation: scorecard },
  };
}

function view(sources: OverviewSources) {
  return render(<VisibleTabsContext.Provider value={TABS}><OverviewView sources={sources} /></VisibleTabsContext.Provider>);
}

afterEach(() => {
  cleanup();
  document.cookie = 'adl_csrf=; Max-Age=0; path=/';
});

test('every band renders its own served source, with the answer from served strings only', () => {
  const { container } = view(served());
  // Scorecard 7d fixture: 3.96 vs 4.40, spend +11.1%; Retail health fixture: +100.0% on Aug 13–19.
  assert.equal(
    container.querySelector('[data-slot="answer"]')?.textContent,
    'Amazon Ads returned 3.96 USD for every 1 USD spent in Sep 9–15, 2026, compared with 4.40 USD in Sep 2–8, 2026. '
      + 'Shipped retail sales rose 100.0% in Aug 13–19, 2026 while ad spend rose 11.1%.',
  );
  const tiles = container.querySelector('.ov-tiles') as HTMLElement;
  assert.ok(within(tiles).getByText('1,000.00 USD'));
  assert.ok(within(tiles).getByText('3.96×'));
  assert.ok(within(tiles).getByText('30.00 USD'));
  assert.ok(within(tiles).getByText('Ad spend as a share of shipped sales'));
  assert.ok(container.querySelector('[data-slot="ov-chart-spend"] svg'));
  assert.ok(container.querySelector('[data-slot="ov-chart-retail"] svg'));
  // New customers by channel: each served share, never a total row.
  const ntb = [...container.querySelectorAll('.ov-ntb li')].map((row) => row.textContent);
  assert.equal(ntb.length, 4);
  assert.ok(ntb[0]?.startsWith('Amazon DSP41.00%'));
  // The served cost per first-time purchase sits beside each share (UX sweep fix 5).
  assert.ok(ntb[0]?.endsWith('35.08 USD per first-time purchase'));
  assert.ok(ntb.every((row) => !/total/i.test(row ?? '')));
  // What changed: the served Scorecard headlines and the served retail movers text.
  assert.ok(screen.getByText('Sponsored Products ROAS rose 0.50×'));
  assert.ok(screen.getByText('Fictional Product FICTA changed by 10.00 USD.'));
  // Trust: reconciliation and one "Data through" line per served end date.
  assert.ok(screen.getByText('Retail matches Vendor Central totals · loaded Aug 19, 2026'));
  assert.ok(screen.getByText('Ad data through Sep 15, 2026'));
  assert.ok(screen.getByText('Retail data through Aug 19, 2026'));
  assert.ok(screen.getByRole('link', { name: 'See new customers ›' }).getAttribute('href')?.endsWith('?preset=7d'));
});

test('each band shows its own absence when its source is rejected, and the others still render', () => {
  const cases: Array<[keyof OverviewSources, string]> = [
    ['scorecard', sourceIssueText('the Scorecard')],
    ['retail', sourceIssueText('Retail health')],
    ['newCustomers', sourceIssueText('New customers')],
    ['campaigns', sourceIssueText('Campaigns')],
  ];
  for (const [key, text] of cases) {
    const sources = { ...served(), [key]: { issue: 'dependency', kind: 'rejected' } } as OverviewSources;
    const { container, unmount } = view(sources);
    assert.ok(screen.getAllByText(text).length >= 1, key);
    // Every other band still shows its served figures.
    if (key !== 'scorecard') assert.ok(within(container.querySelector('.ov-tiles') as HTMLElement).getByText('1,000.00 USD'), key);
    if (key !== 'retail') assert.ok(screen.getByText('Fictional Product FICTA changed by 10.00 USD.'), key);
    if (key !== 'newCustomers') assert.equal(container.querySelectorAll('.ov-ntb li').length, 4, key);
    if (key !== 'campaigns') assert.ok(container.querySelector('[data-slot="ov-chart-spend"] svg'), key);
    unmount();
  }
});

test('the answer drops each clause on its own and names the absent return', () => {
  const sources = served();
  const scorecard = sources.scorecard.kind === 'served' && sources.scorecard.presentation.kind === 'parent' ? sources.scorecard.presentation.artifact : null;
  assert.ok(scorecard !== null);
  // No retail source: the ROAS clause and the spend clause alone.
  assert.equal(
    overviewAnswer(scorecard, null)?.text,
    'Amazon Ads returned 3.96 USD for every 1 USD spent in Sep 9–15, 2026, compared with 4.40 USD in Sep 2–8, 2026. Ad spend rose 11.1%.',
  );
  // No Scorecard: the retail clause alone, which needs no window name beside nothing.
  const retail = sources.retail.kind === 'served' && sources.retail.presentation.kind === 'parent' ? sources.retail.presentation.artifact : null;
  assert.deepEqual(overviewAnswer(null, retail), { kind: 'fallback', text: 'Shipped retail sales rose 100.0%.' });
  assert.equal(overviewAnswer(null, null), null);
  assert.deepEqual(throughLines([{ end: '2026-10-06', name: 'a' }, { end: '2026-10-06', name: 'b' }, { end: '2026-10-04', name: 'c' }]), [
    { end: '2026-10-06', names: ['a', 'b'] },
    { end: '2026-10-04', names: ['c'] },
  ]);
});

test('the adapter asks every source for 7 days and settles each on its own', async () => {
  const bodies: Array<{ path: string; body: Json }> = [];
  document.cookie = 'adl_csrf=csrf; path=/';
  Object.defineProperty(globalThis, 'fetch', { configurable: true, value: async (input: unknown, init?: RequestInit) => {
    const path = new URL(String(input), 'http://localhost').pathname;
    const body = JSON.parse(String(init?.body)) as Json;
    bodies.push({ body, path });
    const id = path.split('/').filter(Boolean).pop() ?? '';
    if (id === 'retail-health') return new Response('{}', { status: 503 });
    return new Response(JSON.stringify({ ...fixture(BODIES[id]!), session_id: body.session_id }), { status: 200 });
  } });
  const parent = await overviewRouteAdapter.loadParent('conversation-fictional', undefined, new AbortController().signal, undefined);
  assert.deepEqual(bodies.map((entry) => entry.path.split('/').pop()).sort(), ['campaigns', 'new-customers', 'retail-health', 'scorecard']);
  assert.ok(bodies.every((entry) => JSON.stringify(entry.body.temporal_selection) === JSON.stringify({ kind: 'preset', preset_id: '7d' })));
  assert.equal(parent.retail.kind, 'rejected');
  assert.equal(parent.scorecard.kind, 'served');
  assert.equal(parent.newCustomers.kind, 'served');
  assert.equal(parent.campaigns.kind, 'served');
  // The assistant reads the Scorecard's served page context.
  assert.equal(overviewRouteAdapter.pageContext(parent, null)?.label, parent.scorecard.kind === 'served' && parent.scorecard.presentation.kind === 'parent' ? parent.scorecard.presentation.page_context.label : null);
  assert.equal(overviewRouteAdapter.renderTemporalControl, false);
});
