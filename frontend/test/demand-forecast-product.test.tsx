// SPDX-License-Identifier: MIT-0
/**
 * The Demand forecast product view in the real shell (revised design S2 to S4, G-06, G-07, G-09):
 * the product replaces the list and names the h1, Back restores the list and focus, one measure
 * shows at a time, and the dated forecast table, tiles, method lines and no-forecast callout read
 * only served values. A wrong view leaves a keyboard user stranded below the fold, dates the
 * forecast window as "next" when it has started, or shows a no-forecast product as a measured zero.
 */
import assert from 'node:assert/strict';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ReactNode } from 'react';
import { afterEach, beforeEach, test, vi } from 'vitest';
vi.mock('next/navigation', () => ({ usePathname: () => '/demand-forecast/', useRouter: () => ({ push: vi.fn() }) }));
vi.mock('next/link', () => ({
  default: ({ children, href, ...props }: { children: ReactNode; href: string }) => <a href={href} {...props}>{children}</a>,
}));
vi.mock('@/lib/session', async (original) => ({ ...(await original<typeof import('@/lib/session')>()), signOut: vi.fn() }));
vi.mock('@/components/ChatPanel', () => ({
  ChatPanel: () => <aside aria-label="Assistant" />,
  seedAssistantQuestion: vi.fn(),
  toggleAssistant: vi.fn(),
}));
import { AppShell } from '@/components/AppShell';
import { DemandForecastView } from '@/components/DemandForecastView';
import detail from './fixtures/demand-forecast-detail.json';
import notForecast from './fixtures/demand-forecast-detail-not-forecast.json';
import listed from './fixtures/demand-forecast-list.json';
import stale from './fixtures/demand-forecast-status-stale.json';

const originalFetch = globalThis.fetch;
beforeEach(() => {
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    value: (media: string) => ({
      addEventListener: vi.fn(), addListener: vi.fn(), matches: false, media,
      removeEventListener: vi.fn(), removeListener: vi.fn(),
    }) as unknown as MediaQueryList,
  });
});
afterEach(() => {
  globalThis.fetch = originalFetch;
  localStorage.clear();
});

type Route = (url: string) => [number, unknown] | null;
const storeDown = { schema_version: 1, refused: 'FORECAST_STORE_UNAVAILABLE', code: 'ADLY-FORECAST_READ-0009', detail: 'x' };

/** Forecast routes answer from the fixtures; anything else the shell asks for answers 404. */
function serve(route: Route): string[] {
  const seen: string[] = [];
  globalThis.fetch = (async (input: RequestInfo | URL) => {
    const url = String(input);
    seen.push(url);
    const answer = url.startsWith('/api/demand-forecast') ? route(url) : null;
    const [status, body] = answer ?? [404, {}];
    return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
  }) as typeof fetch;
  return seen;
}
function forecastRoutes(product: unknown, status: () => [number, unknown] = () => [200, stale]): Route {
  return (url) => {
    if (url.endsWith('/status')) return status();
    if (url.includes('asin=')) return [200, product];
    return [200, listed];
  };
}

function page() {
  return render(<AppShell pathname="/demand-forecast/" sessionId="conversation-fictional"><DemandForecastView/></AppShell>);
}
const LIST_NAME = /^Likely totals, 13 weeks starting Nov 8, 2026/;
const heading = () => screen.getByRole('heading', { level: 1 });
/** Where focus is, by name: comparing DOM nodes in an assertion message is expensive. */
function focused(): string {
  const active = document.activeElement;
  return active === null ? 'none' : `${active.tagName.toLowerCase()}:${active.textContent ?? ''}`;
}

test('opening a product replaces the list, names the h1 and focuses it; Back restores the list and the row', async () => {
  serve(forecastRoutes(detail));
  page();
  await screen.findByRole('table', { name: LIST_NAME });
  await userEvent.click(screen.getByRole('button', { name: '8 weeks' }));
  await userEvent.click(screen.getByRole('button', { name: 'Fictional product 1' }));
  await screen.findByText('ASIN B0FIXTUR01');
  assert.equal(screen.queryByRole('table', { name: /^Likely totals/ }), null);
  assert.equal(heading().textContent, 'Fictional product 1');
  await waitFor(() => assert.equal(focused(), `h1:${heading().textContent ?? ''}`));
  await userEvent.click(screen.getByRole('button', { name: 'Back to Demand forecast' }));
  await screen.findByRole('table', { name: /^Likely totals, 8 weeks starting Nov 8, 2026/ });
  assert.equal(heading().textContent, 'Demand forecast');
  await waitFor(() => assert.equal(focused(), 'button:Fictional product 1'));
});

test('Back by keyboard returns focus to the row when the frame runs before the shell drops Back', async () => {
  // A browser can run the closing frame while the focused Back control is still on the page.
  serve(forecastRoutes(detail));
  page();
  await userEvent.click(await screen.findByRole('button', { name: 'Fictional product 1' }));
  await screen.findByText('ASIN B0FIXTUR01');
  const back = screen.getByRole('button', { name: 'Back to Demand forecast' });
  back.focus();
  const frame = vi.spyOn(window, 'requestAnimationFrame').mockImplementation((callback) => { callback(0); return 0; });
  try {
    await userEvent.keyboard('{Enter}');
    await screen.findByRole('table', { name: LIST_NAME });
  } finally {
    frame.mockRestore();
  }
  await waitFor(() => assert.equal(focused(), 'button:Fictional product 1'));
});
test('a product found by search that is not on the page returns focus to the h1', async () => {
  const offPage = { ...structuredClone(detail), asin: 'B0FIXTUR09', title: null };
  serve(forecastRoutes(offPage));
  page();
  await screen.findByRole('table', { name: LIST_NAME });
  await userEvent.type(screen.getByRole('textbox', { name: 'Find a product by ASIN' }), 'b0fixtur09');
  await userEvent.click(screen.getByRole('button', { name: 'Find' }));
  await screen.findByText('ASIN B0FIXTUR09');
  assert.equal(heading().textContent, 'Untitled product');
  await userEvent.click(screen.getByRole('button', { name: 'Back to Demand forecast' }));
  await screen.findByRole('table', { name: LIST_NAME });
  await waitFor(() => assert.equal(focused(), `h1:${heading().textContent ?? ''}`));
});

test('a forecast measure shows the answer, tiles, chart and dated forecast table', async () => {
  serve(forecastRoutes(detail));
  page();
  await userEvent.click(await screen.findByRole('button', { name: 'Fictional product 1' }));
  assert.ok(await screen.findByText('Customers are likely to order 137 units of Fictional product 1 in the 13 weeks starting Nov 8, 2026.'));
  assert.ok(screen.getByText('Week of Jan 31, 2027'));
  assert.ok(screen.getByText('Likely 11'));
  assert.ok(screen.getByText('Low 10 · High 12 · last week of the view'));
  assert.ok(screen.getByText('9 of 13 weeks'));
  assert.ok(screen.getByText('Made by the Chronos-2 forecasting model'));
  assert.ok(screen.getByText('137 units'));
  assert.ok(screen.getByRole('img', { name: 'Units ordered by week for Fictional product 1, recent weeks Aug 9–Nov 1, 2026, forecast Nov 8, 2026–Jan 31, 2027' }));
  const table = screen.getByRole('table', { name: /^Forecast, 13 weeks starting Nov 8, 2026, units ordered/ });
  assert.deepEqual(within(table).getAllByRole('columnheader').map((cell) => cell.textContent), ['Week starting', 'Low', 'Likely', 'High']);
  assert.equal(within(table).getAllByRole('row').length, 14);
  assert.ok(screen.getByText('Made by the Chronos-2 forecasting model: in testing it beat both simpler methods.'));
  assert.ok(screen.getByText(/Average weekly miss in testing: 0\.8123× the product's usual week-to-week change\./));
  await userEvent.click(screen.getByRole('button', { name: '4 weeks' }));
  assert.ok(screen.getByRole('table', { name: /^Forecast, 4 weeks starting Nov 8, 2026, units ordered/ }));
  assert.ok(screen.getByText('Week of Nov 29, 2026'));
});

test('the measure switch shows one measure at a time, money with its code', async () => {
  serve(forecastRoutes(detail));
  page();
  await userEvent.click(await screen.findByRole('button', { name: 'Fictional product 1' }));
  const measures = await screen.findByRole('group', { name: 'Measure' });
  assert.deepEqual(within(measures).getAllByRole('button').map((button) => button.textContent),
    ['Units ordered (demand)', 'Units shipped', 'Shipped revenue', 'Amazon wholesale cost']);
  assert.equal(within(measures).getByRole('button', { name: 'Units ordered (demand)' }).getAttribute('aria-pressed'), 'true');
  await userEvent.click(within(measures).getByRole('button', { name: 'Shipped revenue' }));
  assert.ok(screen.getByText('16,048.50 USD'));
  const table = screen.getByRole('table', { name: /^Forecast, 13 weeks starting Nov 8, 2026, shipped revenue/ });
  assert.ok(within(table).getAllByText('1,234.50 USD').length > 0);
  assert.ok(screen.getByText('Shipped revenue for Fictional product 1 is likely to be 16,048.50 USD in the 13 weeks starting Nov 8, 2026.'));
});

test('a simpler model and a missing miss say only what is served', async () => {
  const simpler = structuredClone(detail);
  simpler.targets.units_ordered.chosen_model = 'ets';
  (simpler.targets.units_ordered.accuracy as { mase: string | null; mase_gap: string | null }).mase = null;
  (simpler.targets.units_ordered.accuracy as { mase: string | null; mase_gap: string | null }).mase_gap = 'HISTORY_UNCHANGING';
  serve(forecastRoutes(simpler));
  page();
  await userEvent.click(await screen.findByRole('button', { name: 'Fictional product 1' }));
  assert.ok(await screen.findByText('Made by the trend and seasonality method. The weekly run selected this forecast from the methods that passed their checks.'));
  assert.ok(screen.getByText("Sales didn't change from week to week before the test, so there's no usual change to compare the miss with."));
});

test('recent weeks name each week as reported, counted as zero or not known', async () => {
  const states = structuredClone(detail);
  const zero = (target: string) => ({ value: '0.0000', display: target.endsWith('_shipped') && !target.startsWith('units') ? '0.00' : '0' });
  states.recent_weeks.forEach((week, index) => {
    const state = (['inferred_zero', 'unknown'] as const)[index];
    if (state === undefined) return;
    for (const target of Object.keys(week.targets) as (keyof typeof week.targets)[]) {
      week.targets[target] = { state, value: state === 'unknown' ? null : zero(target) } as never;
    }
    week.stockout = null as never;
  });
  serve(forecastRoutes(states));
  page();
  await userEvent.click(await screen.findByRole('button', { name: 'Fictional product 1' }));
  await userEvent.click(await screen.findByText('Recent weeks, with reporting status'));
  const table = screen.getByRole('table', { name: /^Recent weeks, units ordered/ });
  const rows = within(table).getAllByRole('row').slice(1, 4).map((row) => Array.from(row.children, (cell) => cell.textContent));
  assert.deepEqual(rows, [
    ['Aug 9, 2026', '0', 'No sales reported, counted as zero', 'Not known'],
    ['Aug 16, 2026', 'Not known', 'Not known', 'Not known'],
    ['Aug 23, 2026', '9', 'Reported', 'No'],
  ]);
});

test('a product with no forecast shows one callout, the measure switch and the week runs', async () => {
  const runs = structuredClone(notForecast);
  runs.recent_weeks.forEach((week, index) => {
    const state = index < 6 ? 'inferred_zero' : 'unknown';
    week.targets.units_ordered = { state, value: state === 'unknown' ? null : { value: '0.0000', display: '0' } } as never;
    week.stockout = null as never;
  });
  serve(forecastRoutes(runs));
  page();
  await userEvent.click(await screen.findByRole('button', { name: 'Fictional product 3' }));
  const callout = (await screen.findByRole('heading', { name: 'No forecast for this product' })).closest('section');
  assert.ok(callout);
  assert.equal(screen.getAllByRole('heading', { name: /forecast for this product/ }).length, 1);
  assert.ok(within(callout).getByText("This product doesn't have enough weeks of sales yet to forecast."));
  assert.ok(within(callout).getByText('A forecast becomes possible once it has enough complete weeks of sales. The next update that can use new sales data is Nov 19, 2026.'));
  assert.ok(within(callout).getByRole('button', { name: 'Ask why this product has no forecast' }));
  assert.equal(screen.queryByRole('group', { name: 'Weeks to show' }), null);
  assert.equal(screen.queryByText('Weeks shipped less than ordered'), null);
  assert.ok(screen.getByRole('heading', { name: 'Recent weeks, units ordered' }));
  assert.ok(screen.getByText('Aug 9–Sep 13'));
  assert.ok(screen.getByText('Sep 20–Nov 1'));
  await userEvent.click(within(screen.getByRole('group', { name: 'Measure' })).getByRole('button', { name: 'Units shipped' }));
  assert.ok(screen.getByRole('heading', { name: 'Recent weeks, units shipped' }));
  assert.ok(screen.getByText('Aug 9–Nov 1'));
});

test('a single-currency withheld money measure shows its weekly amounts with the code', async () => {
  serve(forecastRoutes(notForecast));
  page();
  await userEvent.click(await screen.findByRole('button', { name: 'Fictional product 3' }));
  await userEvent.click(within(await screen.findByRole('group', { name: 'Measure' })).getByRole('button', { name: 'Shipped revenue' }));
  const strip = screen.getByRole('heading', { name: 'Recent weeks, shipped revenue' }).closest('section');
  assert.ok(strip);
  assert.ok(within(strip).getByText('Nov 1: 9.00 USD'));
});
test('a mixed-currency product loads and shows no money amount under one currency label', async () => {
  const mixed = structuredClone(notForecast);
  mixed.targets.revenue_shipped = { ...mixed.targets.revenue_shipped, withheld_reason: 'CURRENCY_MIXED', currency_code: null } as never;
  mixed.targets.cogs_shipped = { ...mixed.targets.cogs_shipped, withheld_reason: 'CURRENCY_MIXED', currency_code: 'USD' } as never;
  serve(forecastRoutes(mixed));
  page();
  await userEvent.click(await screen.findByRole('button', { name: 'Fictional product 3' }));
  const measures = within(await screen.findByRole('group', { name: 'Measure' }));
  for (const [button, name] of [['Shipped revenue', 'shipped revenue'], ['Amazon wholesale cost', 'Amazon wholesale cost']] as const) {
    await userEvent.click(measures.getByRole('button', { name: button }));
    assert.ok(screen.getByText(`This product's ${name} was reported in more than one currency, so it can't be added up week to week.`));
    const strip = screen.getByRole('heading', { name: `Recent weeks, ${name}` }).closest('section');
    assert.ok(strip);
    assert.ok(within(strip).getByText('Aug 9–Nov 1'));
    assert.equal(within(strip).queryByText(/USD|\d\.\d\d/), null);
  }
});
test('with status failed, the next step drops its date and the pop-up offers Try again', async () => {
  serve(forecastRoutes(notForecast, () => [503, storeDown]));
  page();
  await userEvent.click(await screen.findByRole('button', { name: 'Fictional product 3' }));
  assert.ok(await screen.findByText('A forecast becomes possible once it has enough complete weeks of sales.'));
  await userEvent.click(screen.getByRole('button', { name: 'About this forecast' }));
  assert.ok(screen.getByText(/The update schedule can't be loaded right now\./));
});

test('a product not in this forecast says so with the served count and goes back to the list', async () => {
  const absent = {
    schema_version: 1, outcome: 'not_in_forecast', asin: 'B0NOTREAL0',
    provenance: structuredClone(detail).provenance, freshness: structuredClone(detail).freshness, horizon: structuredClone(detail).horizon,
    title: null, title_truncated: false, targets: null, recent_weeks: null, stockout_weeks_last_13: null, week_state_counts: null,
  };
  serve(forecastRoutes(absent));
  page();
  await screen.findByRole('table', { name: LIST_NAME });
  await userEvent.type(screen.getByRole('textbox', { name: 'Find a product by ASIN' }), 'B0NOTREAL0');
  await userEvent.click(screen.getByRole('button', { name: 'Find' }));
  assert.ok(await screen.findByRole('heading', { name: "B0NOTREAL0 isn't in this forecast" }));
  assert.ok(screen.getByText('This forecast covers 3 products with Vendor Central sales history. Check the ASIN, or choose a product from the list.'));
  assert.equal(heading().textContent, 'Demand forecast');
  await userEvent.click(screen.getByRole('button', { name: 'Back to the list' }));
  assert.ok(await screen.findByRole('table', { name: LIST_NAME }));
});

test('a failed product read keeps the entity and offers Try again', async () => {
  let down = true;
  serve((url) => {
    if (url.endsWith('/status')) return [200, stale];
    if (url.includes('asin=')) return down ? [503, storeDown] : [200, detail];
    return [200, listed];
  });
  page();
  await userEvent.click(await screen.findByRole('button', { name: 'Fictional product 1' }));
  assert.ok(await screen.findByText("The forecast can't be loaded right now. Try again in a minute. If it keeps failing, tell your administrator."));
  assert.equal(heading().textContent, 'Fictional product 1');
  assert.ok(screen.getByRole('button', { name: 'Back to Demand forecast' }));
  down = false;
  await userEvent.click(screen.getByRole('button', { name: 'Try again' }));
  assert.ok(await screen.findByText('ASIN B0FIXTUR01'));
});
