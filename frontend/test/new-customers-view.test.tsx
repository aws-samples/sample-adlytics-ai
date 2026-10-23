// SPDX-License-Identifier: MIT-0
/**
 * The New customers view (slice 9 New customers, item B7) on the server's own fixtures: section
 * order, no total or subtotal row, the ratified `no_new_buyers` copy and `not_returned` at both
 * levels, the per-channel states, gateway products one channel at a time, pointer lines, the
 * insight dialog, the "Show Latest measured week" action, loading, absence states, the copy walk
 * and axe. Each passing case sits beside its failing control.
 */
import { newCustomersAnswer } from '../lib/new-customers';
import assert from 'node:assert/strict';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { test, vi } from 'vitest';
import { newCustomersRouteAdapter } from '@/components/NewCustomersRouteAdapter';
import { NewCustomersSkeleton, NewCustomersView, newCustomersFigureText } from '@/components/NewCustomersView';
import { PresetStepContext, VisibleTabsContext } from '@/components/NextStepContext';
import { PageAbsence } from '@/components/PageAbsence';
import { PresentationV2RouteController } from '@/components/PresentationV2RouteController';
import { formatCount } from '@/lib/exact-format';
import type { NewCustomersArtifact, NewCustomersFigure } from '@/lib/new-customers';
import { bindingByRoute } from '@/lib/page-bindings';
import { newCustomersPresentationV2, parsePresentationResultCalendarV2 } from '@/lib/presentation-v2';
import { visibleHubs, type ManifestTab } from '@/lib/route-manifest';
import errorResponse from './fixtures/new-customers-error-v2.json';
import rolling from './fixtures/new-customers-7d-v2.json';
import thirty from './fixtures/new-customers-30d-v2.json';
import week from './fixtures/new-customers-week-v2.json';
import month from './fixtures/new-customers-month-v2.json';
import tilesNotReturned from './fixtures/new-customers-tiles-not-returned-v2.json';
import tileNoNewBuyers from './fixtures/new-customers-tile-no-new-buyers-v2.json';
import rowNoNewBuyers from './fixtures/new-customers-row-no-new-buyers-v2.json';
import noAdSpend from './fixtures/new-customers-no-ad-spend-v2.json';
import noChannels from './fixtures/new-customers-no-channels-v2.json';
import perChannelPending from './fixtures/new-customers-per-channel-pending-v2.json';
import perChannelMissing from './fixtures/new-customers-per-channel-missing-v2.json';
import tilesPending from './fixtures/new-customers-tiles-pending-v2.json';
import tilesMissing from './fixtures/new-customers-tiles-missing-v2.json';
import stepOneAbsences from './fixtures/new-customers-step-1-absences-v2.json';
import gatewayNotCovered from './fixtures/new-customers-gateway-not-covered-v2.json';
import gatewayNotMeasured from './fixtures/new-customers-gateway-not-measured-v2.json';
import twoMarketplaces from './fixtures/new-customers-two-marketplaces-v2.json';
import rollingQuiet from './fixtures/new-customers-rolling-quiet-v2.json';
import noPeriod from './fixtures/new-customers-no-period-v2.json';
import pending from './fixtures/new-customers-pending-v2.json';
import empty from './fixtures/new-customers-empty-v2.json';

const AXE_RULES = { rules: { 'color-contrast': { enabled: false }, region: { enabled: false } } };

function artifactOf(fixture: unknown): NewCustomersArtifact {
  const presentation = newCustomersPresentationV2(parsePresentationResultCalendarV2(fixture, 'new-customers'));
  if (presentation.kind !== 'parent') throw new Error('expected a parent');
  return presentation.artifact;
}

const tab = (tab_id: string, label: string, route: string): ManifestTab => ({
  description: label, entitlement: null, kind: 'page', label, order: 1, route, tab_id,
});
const AUDIENCES = tab('audiences', 'Audiences', '/audiences/');
const RETENTION = tab('retention', 'Retention', '/retention/');

function rowHeaders(region: HTMLElement): string[] {
  return within(region).getAllByRole('rowheader').map((cell) => cell.firstChild?.textContent ?? '');
}

test('the sections render in the LLD order with the server scope band and texts', () => {
  const artifact = artifactOf(rolling);
  const view = render(<NewCustomersView artifact={artifact} />);
  const headings = within(view.container).getAllByRole('heading', { level: 2 }).map((node) => node.textContent);
  assert.deepEqual(headings, ['New-to-brand purchases', 'Insights', 'New buyers', 'Gateway products']);
  const scope = view.container.querySelector('.scope-band');
  assert.equal(scope?.textContent, artifact.scope_text);
  assert.equal(scope?.getAttribute('aria-live'), 'polite');
  assert.ok(screen.getByText('About these figures'));
  // The step-1 table's columns are the served ones, change in share included on a rolling preset.
  const table = screen.getByRole('region', { name: 'New-to-brand purchases per channel' });
  assert.deepEqual(
    within(table).getAllByRole('columnheader').map((cell) => cell.firstChild?.textContent),
    ['Channel', 'New-to-brand purchases', 'New-to-brand revenue', 'New-to-brand revenue share', 'Change in share', 'Cost per new-to-brand purchase'],
  );
});

test('no table has a total or subtotal row: each lists exactly its served ad products (NCH §5)', () => {
  const artifact = artifactOf(month);
  render(<NewCustomersView artifact={artifact} />);
  const market = artifact.markets[0];
  const step = screen.getByRole('region', { name: 'New-to-brand purchases per channel' });
  assert.deepEqual(rowHeaders(step), market?.new_to_brand.rows.map((row) => row.ad_product_label));
  const perChannel = screen.getByRole('region', { name: 'New buyers per channel' });
  assert.deepEqual(rowHeaders(perChannel), market?.new_buyers.per_channel?.rows.map((row) => row.ad_product_label));
  for (const region of [step, perChannel]) {
    assert.equal(within(region).queryByText(/^(Total|All channels)$/), null);
  }
  // Paired control: a total row injected into the decoded body would be counted here.
  const forged = structuredClone(artifact) as unknown as { markets: { new_buyers: { per_channel: { rows: unknown[] } } }[] };
  const rows = forged.markets[0]?.new_buyers.per_channel.rows ?? [];
  rows.push({ ...(rows[0] as object), ad_product_key: 'all_channels', ad_product_label: 'All channels' });
  const forgedView = render(<NewCustomersView artifact={forged as unknown as NewCustomersArtifact} />);
  const forgedTable = within(forgedView.container).getByRole('region', { name: 'New buyers per channel' });
  assert.ok(rowHeaders(forgedTable).includes('All channels'));
});

test('a calendar preset draws no change column and shows the tiles with their basis', () => {
  const artifact = artifactOf(week);
  render(<NewCustomersView artifact={artifact} />);
  const step = screen.getByRole('region', { name: 'New-to-brand purchases per channel' });
  assert.equal(within(step).queryByRole('columnheader', { name: /Change in share/ }), null);
  const tiles = artifact.markets[0]?.new_buyers;
  assert.ok(screen.getByText(formatCount(tiles?.new_buyers?.value ?? '')));
  assert.ok(screen.getByText(tiles?.cost_basis ?? ''));
  // Paired control: the rolling preset draws the column.
  const view = render(<NewCustomersView artifact={artifactOf(rolling)} />);
  assert.ok(within(view.container).getAllByRole('columnheader', { name: /Change in share/ }).length > 0);
});

test('the tiles read the ratified no-new-buyers copy: 0 new buyers and the cost not available', () => {
  const view = render(<NewCustomersView artifact={artifactOf(tileNoNewBuyers)} />);
  const tiles = view.container.querySelectorAll('.tiles.two .tile');
  assert.equal(tiles[0]?.querySelector('.v')?.textContent, '0');
  assert.equal(tiles[1]?.querySelector('.v')?.textContent, 'Not availableNo new buyers in Week of 14–20 Sep 2026.');
  // The per-channel table still renders beneath the tiles.
  assert.ok(screen.getByRole('region', { name: 'New buyers per channel' }));
  // Paired control: returned new buyers show a figure for the cost.
  const returned = render(<NewCustomersView artifact={artifactOf(week)} />);
  assert.notEqual(returned.container.querySelectorAll('.tiles.two .tile')[1]?.querySelector('.reason'), undefined);
  assert.equal(returned.container.querySelectorAll('.tiles.two .tile')[1]?.querySelector('.reason'), null);
});

test('a per-channel row reads the ratified copy beside a numeric row and a row not returned', () => {
  render(<NewCustomersView artifact={artifactOf(rowNoNewBuyers)} />);
  const table = screen.getByRole('region', { name: 'New buyers per channel' });
  const row = (name: string) => within(table).getByRole('rowheader', { name: new RegExp(`^${name}`) }).closest('tr') as HTMLElement;
  const cells = (name: string) => within(row(name)).getAllByRole('cell').map((cell) => cell.textContent);
  assert.deepEqual(cells('Sponsored Brands'), ['0', 'Not availableNo new buyers for Sponsored Brands in Week of 14–20 Sep 2026.']);
  assert.deepEqual(cells('Sponsored Display'), ['Not availabletoo few shoppers, or none', 'Not availabletoo few shoppers, or none']);
  assert.ok(!(cells('Amazon DSP')[1] ?? '').startsWith('Not available'));
  assert.ok(screen.getByText("Some breakdowns had too few shoppers and aren't shown."));
});

test('tiles not returned read "too few shoppers, or none" in both, never "withheld"', () => {
  const view = render(<NewCustomersView artifact={artifactOf(tilesNotReturned)} />);
  const tiles = [...view.container.querySelectorAll('.tiles.two .tile')];
  assert.deepEqual(tiles.map((tile) => tile.querySelector('.v')?.textContent), [
    'Not availabletoo few shoppers, or none',
    'Not availabletoo few shoppers, or none',
  ]);
  assert.equal(view.container.textContent?.includes('withheld'), false);
});

test('no ad spend, no channels and the per-channel delivery states read their server lines', () => {
  const spend = render(<NewCustomersView artifact={artifactOf(noAdSpend)} />);
  assert.ok(within(spend.container).getByText('No ad spend for Sponsored Display in Week of 14–20 Sep 2026.'));
  spend.unmount();
  const none = render(<NewCustomersView artifact={artifactOf(noChannels)} />);
  assert.ok(within(none.container).getByText('No channel had ad spend in Week of 14–20 Sep 2026.'));
  assert.equal(within(none.container).queryByRole('region', { name: 'New buyers per channel' }), null);
  none.unmount();
  for (const [fixture, line] of [
    [perChannelPending, 'Figures per channel for Week of 14–20 Sep 2026 are expected after 22 Sep 2026.'],
    [perChannelMissing, "Figures per channel for Week of 14–20 Sep 2026 didn't arrive."],
  ] as const) {
    const view = render(<NewCustomersView artifact={artifactOf(fixture)} />);
    assert.ok(within(view.container).getByText(line));
    // The tiles render while the per-channel delivery is late.
    assert.equal(view.container.querySelectorAll('.tiles.two .tile').length, 2);
    // NC-GATE-105: its "See why" names the coverage entry it explains, never the tiles'.
    assert.ok(within(view.container).getByRole('button', { name: 'See why: New buyers per channel' }));
    assert.equal(within(view.container).queryByRole('button', { name: 'See why: New buyers' }), null);
    view.unmount();
  }
});

test('late tiles read their line with no table, and See why opens the coverage sheet', async () => {
  const user = userEvent.setup();
  for (const [fixture, line] of [
    [tilesPending, 'Figures for Week of 14–20 Sep 2026 are expected after 22 Sep 2026.'],
    [tilesMissing, "Figures for Week of 14–20 Sep 2026 didn't arrive."],
  ] as const) {
    const view = render(<NewCustomersView artifact={artifactOf(fixture)} />);
    // Once on the page outside the answer sentence, which may lead with the same served line.
    assert.equal(within(view.container).getAllByText(line, { exact: false }).filter((node) => node.closest('[data-slot="answer"]') === null).length, 1);
    assert.equal(view.container.querySelector('.tiles.two'), null);
    assert.equal(within(view.container).queryByRole('region', { name: 'New buyers per channel' }), null);
    const why = within(view.container).getByRole('button', { name: 'See why: New buyers' });
    await user.click(why);
    const sheet = screen.getByRole('dialog', { name: 'Coverage for this window' });
    assert.ok(within(sheet).getByText(`${line} It shows by itself when the data arrives.`));
    await user.keyboard('{Escape}');
    assert.equal(document.activeElement, why);
    view.unmount();
  }
});

test('gateway products show one channel at a time and switch with no request', async () => {
  const user = userEvent.setup();
  const fetchSpy = vi.spyOn(globalThis, 'fetch');
  const artifact = artifactOf(rolling);
  render(<NewCustomersView artifact={artifact} />);
  const group = screen.getByRole('group', { name: 'Channel' });
  const options = within(group).getAllByRole('radio');
  assert.deepEqual(options.map((option) => (option as HTMLInputElement).checked), [true, false, false, false]);
  assert.ok(screen.getByRole('region', { name: 'Products new-to-brand shoppers bought first, Amazon DSP' }));
  await user.click(within(group).getByRole('radio', { name: 'Sponsored Products' }));
  const region = screen.getByRole('region', { name: 'Products new-to-brand shoppers bought first, Sponsored Products' });
  assert.deepEqual(rowHeaders(region), artifact.markets[0]?.gateway[1]?.rows.map((row) => row.title));
  assert.equal(screen.queryByRole('region', { name: /Amazon DSP$/ }), null);
  // Arrow keys move the selection, as a radio group does.
  await user.keyboard('{ArrowRight}');
  assert.ok(screen.getByRole('region', { name: 'Products new-to-brand shoppers bought first, Sponsored Brands' }));
  assert.equal(fetchSpy.mock.calls.length, 0);
  fetchSpy.mockRestore();
});

test('gateway coverage and no-purchase texts render in place of the list', () => {
  for (const [fixture, line] of [
    [gatewayNotCovered, 'Gateway products are measured from 15 Sep 2026.'],
    [gatewayNotMeasured, "Gateway products aren't measured yet."],
  ] as const) {
    const view = render(<NewCustomersView artifact={artifactOf(fixture)} />);
    assert.ok(within(view.container).getByText(line, { exact: false }));
    assert.equal(within(view.container).queryByRole('group', { name: 'Channel' }), null);
    view.unmount();
  }
  const absent = render(<NewCustomersView artifact={artifactOf(stepOneAbsences)} />);
  assert.ok(within(absent.container).getByText('No new-to-brand revenue share for Sep 14–20, 2026, so there\'s no change.'));
  assert.ok(within(absent.container).getByText("No attributed revenue in this window, so there's no share."));
});

test('a pointer line renders only when its tab is in the session manifest', () => {
  const artifact = artifactOf(rolling);
  const without = render(<NewCustomersView artifact={artifact} />);
  assert.equal(without.container.querySelector('.pointer'), null);
  without.unmount();
  const withTabs = render(<VisibleTabsContext.Provider value={[AUDIENCES, RETENTION]}><NewCustomersView artifact={artifact} /></VisibleTabsContext.Provider>);
  const lines = [...withTabs.container.querySelectorAll('.pointer')];
  assert.deepEqual(lines.map((line) => line.textContent), [
    'Which audience segments your buyers belong to is on Reach and audiences › Audiences.',
    'Whether new customers buy again is on Customers › Retention.',
  ]);
  assert.deepEqual(lines.map((line) => line.querySelector('a')?.getAttribute('href')), ['/audiences/', '/retention/']);
  withTabs.unmount();
  const one = render(<VisibleTabsContext.Provider value={[RETENTION]}><NewCustomersView artifact={artifact} /></VisibleTabsContext.Provider>);
  assert.equal(one.container.querySelectorAll('.pointer').length, 1);
  one.unmount();
  // NC-GATE-104: the link is the served href, never rewritten from the manifest's route.
  const moved = render(<VisibleTabsContext.Provider value={[tab('retention', 'Retention', '/elsewhere/')]}><NewCustomersView artifact={artifact} /></VisibleTabsContext.Provider>);
  assert.equal(moved.container.querySelector('.pointer a')?.getAttribute('href'), '/retention/');
  moved.unmount();
  // NC-19: the committed route manifest serves both adjacent customer-question destinations.
  const committed = visibleHubs({}).flatMap((hub) => hub.tabs);
  assert.ok(committed.some((candidate) => candidate.tab_id === 'new-customers'));
  assert.ok(committed.some((candidate) => candidate.tab_id === 'retention'));
  assert.ok(committed.some((candidate) => candidate.tab_id === 'audiences'));
  const shipped = render(<VisibleTabsContext.Provider value={committed}><NewCustomersView artifact={artifact} /></VisibleTabsContext.Provider>);
  assert.equal(shipped.container.querySelectorAll('.pointer').length, 2);
  assert.deepEqual([...shipped.container.querySelectorAll('.pointer a')].map((link) => link.getAttribute('href')), ['/audiences/', '/retention/']);
});

test('the page context is the served envelope\'s on a complete body and absent otherwise (N24)', () => {
  const presentation = newCustomersPresentationV2(parsePresentationResultCalendarV2(rolling, 'new-customers'));
  assert.ok(presentation.kind === 'parent');
  const context = newCustomersRouteAdapter.pageContext(presentation, null);
  assert.equal(context?.label, 'New customers · Sep 14–20, 2026');
  assert.deepEqual(context?.starter_questions, presentation.artifact.starter_questions);
  // Paired: an empty body and a page-level window state offer no context.
  for (const fixture of [empty, noPeriod]) {
    const other = newCustomersPresentationV2(parsePresentationResultCalendarV2(fixture, 'new-customers'));
    assert.equal(newCustomersRouteAdapter.pageContext(other, null), null);
  }
});

test('a served error shows the page error with Try again, and Try again asks once more (NC-22)', async () => {
  const user = userEvent.setup();
  document.cookie = 'adl_csrf=csrf; path=/';
  const session = rolling.session_id;
  const paths: string[] = [];
  let failing = true;
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
    paths.push(String(input));
    return failing
      ? new Response(JSON.stringify(errorResponse.body), { status: errorResponse.status })
      : new Response(JSON.stringify(rolling), { status: 200 });
  });
  render(<PresentationV2RouteController adapter={newCustomersRouteAdapter} sessionId={session} />);
  const alert = await screen.findByRole('alert');
  assert.ok(within(alert).getByText('This page cannot be shown safely right now.'));
  // No figure, scope band or section renders beside the error.
  assert.equal(document.querySelector('.scope-band'), null);
  assert.equal(document.querySelector('table'), null);
  assert.deepEqual(paths, ['/api/presentation/new-customers']);
  // Paired: once the source answers, Try again renders the page from the served body.
  failing = false;
  await user.click(within(alert).getByRole('button', { name: 'Try again' }));
  assert.ok(await screen.findByRole('heading', { level: 2, name: 'New-to-brand purchases' }));
  assert.equal(screen.queryByRole('alert'), null);
  assert.equal(paths.length, 2);
});

test('the insight card opens its dialog with the served provenance and returns focus on close', async () => {
  const user = userEvent.setup();
  const artifact = artifactOf(rolling);
  render(<NewCustomersView artifact={artifact} />);
  const card = artifact.insight_cards[0];
  const open = screen.getByRole('button', { name: `Read more: ${card?.title ?? ''}` });
  await user.click(open);
  const dialog = screen.getByRole('dialog');
  assert.ok(within(dialog).getByText(card?.body ?? ''));
  assert.ok(within(dialog).getByText('Found by a fixed rule from the figures above. Not written by an AI model.'));
  const axe = await import('axe-core');
  const results = await axe.default.run(dialog, AXE_RULES);
  assert.deepEqual(results.violations.map((violation) => violation.id), []);
  await user.keyboard('{Escape}');
  assert.equal(document.activeElement, open);
  // Paired control: a calendar preset's slot is silent, so no card and no dialog opener.
  const quiet = render(<NewCustomersView artifact={artifactOf(week)} />);
  assert.equal(within(quiet.container).queryByRole('button', { name: /^Read more/ }), null);
  assert.ok(within(quiet.container).getByText(artifactOf(week).insights.absence_reason ?? ''));
});

test('"Show Latest measured week" selects that preset through the date control path', async () => {
  const user = userEvent.setup();
  const selected: string[] = [];
  const step = (presetId: string) => () => { selected.push(presetId); };
  render(<PresetStepContext.Provider value={step}><NewCustomersView artifact={artifactOf(thirty)} /></PresetStepContext.Provider>);
  await user.click(screen.getByRole('button', { name: 'Show Latest measured week' }));
  assert.deepEqual(selected, ['last_complete_week']);
  // The week then arrives: the New buyers heading takes focus (plan D16).
  const arrived = render(<NewCustomersView artifact={artifactOf(week)} />);
  assert.equal(document.activeElement, within(arrived.container).getByRole('heading', { name: 'New buyers' }));
  // Paired control: with no available preset to select, the button doesn't render.
  const none = render(<PresetStepContext.Provider value={() => null}><NewCustomersView artifact={artifactOf(rolling)} /></PresetStepContext.Provider>);
  assert.equal(within(none.container).queryByRole('button', { name: 'Show Latest measured week' }), null);
});

test('a load that fails after "Show Latest measured week" drops the arrival focus (NC-GATE-106)', async () => {
  const user = userEvent.setup();
  document.cookie = 'adl_csrf=csrf; path=/';
  const selection = { kind: 'preset', preset_id: 'last_complete_week' } as const;
  const load = (served: { readonly status: number; readonly body: unknown }) => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(new Response(JSON.stringify(served.body), { status: served.status }));
    return newCustomersRouteAdapter.loadParent(rolling.session_id, selection, new AbortController().signal, undefined);
  };
  const press = async () => {
    const view = render(<PresetStepContext.Provider value={() => () => undefined}><NewCustomersView artifact={artifactOf(thirty)} /></PresetStepContext.Provider>);
    await user.click(within(view.container).getByRole('button', { name: 'Show Latest measured week' }));
    view.unmount();
  };
  await press();
  await assert.rejects(load(errorResponse));
  const later = render(<NewCustomersView artifact={artifactOf(week)} />);
  assert.notEqual(document.activeElement, within(later.container).getByRole('heading', { name: 'New buyers' }));
  later.unmount();
  // Paired: when the week itself arrives, its New buyers heading takes focus.
  await press();
  await load({ status: 200, body: week });
  const arrived = render(<NewCustomersView artifact={artifactOf(week)} />);
  assert.equal(document.activeElement, within(arrived.container).getByRole('heading', { name: 'New buyers' }));
});

test('a calendar preset\'s quiet insight says nothing was compared; a quiet rolling one names its threshold', () => {
  for (const fixture of [week, month]) {
    const view = render(<NewCustomersView artifact={artifactOf(fixture)} />);
    const quiet = view.container.querySelector('.insight.quiet p');
    assert.equal(quiet?.textContent, 'No comparison for calendar periods.');
    assert.equal(view.container.textContent?.includes('equal prior window'), false);
    view.unmount();
  }
  // Paired control: a rolling preset where no share moved serves the slot's silence condition.
  const rollingView = render(<NewCustomersView artifact={artifactOf(rollingQuiet)} />);
  assert.equal(
    rollingView.container.querySelector('.insight.quiet p')?.textContent,
    "No ad product's new-to-brand revenue share moved by the declared threshold against the equal prior window.",
  );
  assert.equal(within(rollingView.container).queryByRole('button', { name: /^Read more/ }), null);
});

test('two marketplaces repeat every section per marketplace, each named', async () => {
  const artifact = artifactOf(twoMarketplaces);
  const view = render(<NewCustomersView artifact={artifact} />);
  for (const market of artifact.markets) {
    assert.ok(screen.getByRole('region', { name: `${market.marketplace_text ?? ''} New-to-brand purchases per channel` }));
  }
  const titles = artifact.insight_cards.map((card) => card.title);
  assert.equal(new Set(titles).size, titles.length);
  for (const title of titles) assert.ok(screen.getByRole('button', { name: `Read more: ${title}` }));
  const axe = await import('axe-core');
  const results = await axe.default.run(view.container, AXE_RULES);
  assert.deepEqual(results.violations.map((violation) => violation.id), []);
});

test('the page has no axe violations and the loading skeleton announces itself', async () => {
  const axe = await import('axe-core');
  for (const fixture of [rolling, month, rowNoNewBuyers, tilesPending]) {
    const view = render(<VisibleTabsContext.Provider value={[AUDIENCES, RETENTION]}><NewCustomersView artifact={artifactOf(fixture)} /></VisibleTabsContext.Provider>);
    const results = await axe.default.run(view.container, AXE_RULES);
    assert.deepEqual(results.violations.map((violation) => violation.id), []);
    view.unmount();
  }
  render(<NewCustomersSkeleton />);
  assert.ok(screen.getByRole('status', { name: 'Loading New customers' }));
});

test('the empty body and the page-level window states show the server text and no figures', () => {
  const view = render(<NewCustomersView artifact={artifactOf(empty)} />);
  assert.ok(within(view.container).getByText(artifactOf(empty).outcome_text ?? ''));
  assert.equal(view.container.querySelector('table'), null);
  view.unmount();
  for (const fixture of [noPeriod, pending]) {
    const presentation = newCustomersPresentationV2(parsePresentationResultCalendarV2(fixture, 'new-customers'));
    assert.ok(presentation.kind === 'window_unavailable');
    // ADR-139: the controller renders the adapter's absence through PageAbsence.
    const shown = render(<PageAbsence {...newCustomersRouteAdapter.absence(presentation)} />);
    assert.ok(within(shown.container).getByText(presentation.message));
    assert.equal(shown.container.querySelector('.scope-band'), null);
    shown.unmount();
  }
});

function servedStrings(fixture: unknown): Set<string> {
  const served = new Set<string>();
  const collect = (value: unknown): void => {
    if (typeof value === 'string') served.add(value);
    else if (Array.isArray(value)) value.forEach(collect);
    else if (typeof value === 'object' && value !== null) Object.values(value).forEach(collect);
  };
  collect(fixture);
  return served;
}

function servedFigures(artifact: NewCustomersArtifact | null): Set<string> {
  const shown = new Set<string>();
  const add = (figure: NewCustomersFigure | null) => {
    const value = figure === null ? null : newCustomersFigureText(figure);
    if (value !== null) shown.add(value);
  };
  for (const market of artifact?.markets ?? []) {
    for (const row of market.new_to_brand.rows) [row.ntb_purchases, row.ntb_revenue, row.ntb_revenue_share, row.cost_per_ntb_purchase].forEach(add);
    add(market.new_buyers.new_buyers);
    add(market.new_buyers.cost_per_new_buyer);
    for (const row of market.new_buyers.per_channel?.rows ?? []) [row.new_buyers, row.cost_per_new_buyer].forEach(add);
    for (const entry of market.gateway) for (const row of entry.rows) shown.add(formatCount(String(row.ntb_purchases)));
  }
  return shown;
}

function strayTexts(container: HTMLElement, served: ReadonlySet<string>): string[] {
  const stray: string[] = [];
  const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node !== null; node = walker.nextNode()) {
    const value = node.textContent?.trim() ?? '';
    if (value === '' || /^[·:.]$/.test(value)) continue;
    // The shared change chip's spoken direction and tone, from the served `direction` and `sentiment`.
    if (/^(Up|Down)(, (favourable|unfavourable))?:$/.test(value)) continue;
    if (BROWSER_OWNED.has(value) || served.has(value)) continue;
    stray.push(value);
  }
  return stray;
}

// The words the browser writes on a served page: the shared card's opener and the shared
// "See why" control (the Scorecard LLD's browser-owned copy).
const BROWSER_OWNED = new Set(['Read more ›', 'See why']);
const WALKED = {
  empty, gatewayNotCovered, gatewayNotMeasured, month, noAdSpend, noChannels, perChannelMissing, perChannelPending, rolling, rowNoNewBuyers,
  rollingQuiet, stepOneAbsences, thirty, tileNoNewBuyers, tilesMissing, tilesNotReturned, tilesPending, twoMarketplaces, week,
};

test('every visible text is exactly a string or figure its own response served, or browser-owned', () => {
  const page = bindingByRoute['new-customers'].display_name;
  for (const [name, fixture] of Object.entries(WALKED)) {
    const artifact = artifactOf(fixture);
    const view = render(<VisibleTabsContext.Provider value={[AUDIENCES, RETENTION]}>
      <PresetStepContext.Provider value={(presetId) => () => { void presetId; }}><NewCustomersView artifact={artifact} /></PresetStepContext.Provider>
    </VisibleTabsContext.Provider>);
    // The answer sentence is a fixed template over served values (UI refresh 1.5), pinned by its builder's tests.
    const answer = newCustomersAnswer(artifact)?.text;
    const served = new Set([...servedStrings(fixture), ...servedFigures(artifact), page, ...(answer === undefined ? [] : [answer])]);
    assert.deepEqual(strayTexts(view.container, served), [], `browser-written text in ${name}`);
    view.unmount();
  }
  // Paired controls in the same walk: a browser sentence, a cut word and another state's text.
  const own = new Set([...servedStrings(month), ...servedFigures(artifactOf(month)), page, newCustomersAnswer(artifactOf(month))?.text ?? '']);
  const planted = render(<div><NewCustomersView artifact={artifactOf(month)} /><p>No buyers yet.</p></div>);
  assert.deepEqual(strayTexts(planted.container, own), ['No buyers yet.']);
  planted.unmount();
  const partial = render(<div><NewCustomersView artifact={artifactOf(month)} /><span>Not</span></div>);
  assert.deepEqual(strayTexts(partial.container, own), ['Not']);
  partial.unmount();
  const borrowed = 'No new buyers in Week of 14–20 Sep 2026.';
  assert.ok(servedStrings(tileNoNewBuyers).has(borrowed) && !own.has(borrowed));
  const crossState = render(<div><NewCustomersView artifact={artifactOf(month)} /><p>{borrowed}</p></div>);
  assert.deepEqual(strayTexts(crossState.container, own), [borrowed]);
});
