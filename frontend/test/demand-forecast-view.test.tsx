// SPDX-License-Identifier: MIT-0
/**
 * The Demand forecast tab over the shared wire fixtures (ADR-163; hybrid design H1 to H6): the
 * scope card and freshness pop-up, the out-of-date banner, the answer sentence, "Check these
 * first", the one search box and its inline outcomes, Browse all's chips, table and pager, the
 * PAGE_INVALID reset, Try again, the no-generation state and a publish between requests. A wrong
 * state shows a planner a stale or partial list as current, a ranked list labelled as flagged, a
 * product that doesn't match the typed ASIN, another generation's ASIN as an example, or an
 * outage where the page only cancelled a request.
 */
import assert from 'node:assert/strict';
import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, test } from 'vitest';
import { DemandForecastView } from '@/components/DemandForecastView';
import detail from './fixtures/demand-forecast-detail.json';
import listed from './fixtures/demand-forecast-list.json';
import emptyFilter from './fixtures/demand-forecast-list-empty-filter.json';
import refusal from './fixtures/demand-forecast-refusal.json';
import noGeneration from './fixtures/demand-forecast-status-no-generation.json';
import stale from './fixtures/demand-forecast-status-stale.json';

const originalFetch = globalThis.fetch;
beforeEach(() => { window.history.replaceState(null, '', '/demand-forecast/'); });
afterEach(() => { globalThis.fetch = originalFetch; });

type Answer = [number, unknown];
type Route = (url: string, signal: AbortSignal | undefined) => Answer | Promise<Answer>;

function serve(route: Route): string[] {
  const seen: string[] = [];
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    seen.push(url);
    const [status, body] = await route(url, init?.signal ?? undefined);
    return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
  }) as typeof fetch;
  return seen;
}
/**
 * An answer the test releases later. It rejects with an AbortError when its request is aborted,
 * unless `late` is set: then it answers anyway, as a server that never saw the abort.
 */
function deferred(signal: AbortSignal | undefined, late = false): { promise: Promise<Answer>; resolve: (answer: Answer) => void } {
  let resolve: (answer: Answer) => void = () => undefined;
  const promise = new Promise<Answer>((settle, reject) => {
    resolve = settle;
    if (!late) signal?.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')));
  });
  return { promise, resolve };
}

const storeDown = { schema_version: 1, refused: 'FORECAST_STORE_UNAVAILABLE', code: 'ADLY-FORECAST_READ-0009', detail: 'x' };
const unavailableList = {
  ...structuredClone(emptyFilter),
  outcome: 'unavailable', provenance: null, horizon: null, page_count: 0, item_count: null,
  counts_by_reason: null, counts_by_model: null, currency_codes: [],
  freshness: { state: 'unavailable', age_days: null, stale_after_days: 16, causes: [] },
  query: { status: null, page: 1 },
};
const absent = (asin: string) => ({
  schema_version: 1, outcome: 'not_in_forecast', asin,
  provenance: structuredClone(detail).provenance, freshness: structuredClone(detail).freshness, horizon: structuredClone(detail).horizon,
  title: null, title_truncated: false, targets: null, recent_weeks: null, stockout_weeks_last_13: null, week_state_counts: null,
});
const noneYet = { ...absent('B0FIXTUR01'), outcome: 'unavailable', provenance: null, horizon: null, freshness: unavailableList.freshness };
const SHORT = /^Check these first: largest forecast demand, 13 weeks starting Nov 8, 2026/;
const LIST_NAME = /^Likely totals, 13 weeks starting Nov 8, 2026/;
const searchBox = () => screen.getByRole('textbox', { name: 'Find any product by ASIN' });
const forecastRoutes = (product: unknown = detail): Route => (url) => {
  if (url.endsWith('/status')) return [200, stale];
  return url.includes('asin=') ? [200, product] : [200, listed];
};
/** Where focus is, by name: comparing DOM nodes in an assertion message is expensive. */
function focused(): string {
  const active = document.activeElement;
  return active === null ? 'none' : `${active.tagName.toLowerCase()}:${active.getAttribute('aria-label') ?? active.textContent ?? ''}`;
}

/** A page 1 of `count` forecast products, ranked 1 to `count`, built from the fixture's first row. */
function rankedList(count: number) {
  const list = structuredClone(listed);
  const [first] = list.entries;
  assert.ok(first);
  list.entries = Array.from({ length: count }, (_unused, index) => ({
    ...structuredClone(first), rank: index + 1, asin: `B0RANK${String(index + 1).padStart(4, '0')}`, title: `Fictional product ${index + 1}`,
  }));
  list.item_count = count;
  list.match_count = count;
  list.counts_by_reason = { units_shipped: {}, units_ordered: {}, revenue_shipped: {}, cogs_shipped: {} } as never;
  return list;
}

/** The visible text of a subtree: hidden pop-up panels are skipped, as a sighted reader sees it. */
function visibleText(root: Element): string {
  const parts: string[] = [];
  const walk = (node: Node) => {
    if (node instanceof Element && (node.hasAttribute('hidden') || node.classList.contains('sr-only'))) return;
    if (node.nodeType === Node.TEXT_NODE) parts.push(node.textContent ?? '');
    node.childNodes.forEach(walk);
  };
  walk(root);
  return parts.join('');
}

test('the opening view draws no h1; the scope card, answer and Check these first read only served values', async () => {
  serve(forecastRoutes());
  const { container } = render(<DemandForecastView/>);
  const table = await screen.findByRole('table', { name: SHORT });
  assert.equal(container.querySelector('h1'), null);
  assert.ok(screen.getByText('3 products'));
  assert.ok(screen.getByText('Vendor Central · ordered and shipped'));
  assert.ok(screen.getByText('Up to date · sales through Nov 7, 2026'));
  assert.ok(screen.getByText('13 weeks starting Nov 8, 2026 · Sunday to Saturday weeks'));
  assert.ok(screen.getByText('Fictional product 2 leads the 13-week forecast: customers are likely to order 260 units in the 13 weeks starting Nov 8, 2026.'));
  assert.ok(screen.getByRole('heading', { level: 2, name: 'Check these first' }));
  assert.ok(screen.getByText('Largest forecast demand, ordered by 13-week likely units ordered'));
  assert.doesNotMatch(visibleText(container), /flagged/i);
  assert.deepEqual(within(table).getAllByRole('columnheader').map((cell) => visibleText(cell)),
    ['Rank', 'Product', 'Likely units ordered13 weeks', 'Weeks shipped less than orderedof the last 13']);
  // Rank 3 has no ordered-units forecast, so it isn't listed and nothing replaces it (OD-H2 A).
  const rows = within(table).getAllByRole('row').slice(1);
  assert.deepEqual(rows.map((row) => within(row).getByRole('rowheader').textContent), ['Fictional product 2B0FIXTUR02', 'Fictional product 1B0FIXTUR01']);
  assert.deepEqual(within(rows[0] as HTMLElement).getAllByRole('cell').map((cell) => cell.textContent), ['1', '260', '1']);
  assert.equal(within(table).queryByText(/USD|16,048/), null);
  const reasons = container.querySelector('.df-reasons');
  assert.ok(reasons);
  assert.equal(visibleText(reasons), 'Products without an ordered-units forecast: Too little history: 1 product Show them');
  assert.ok(screen.getByRole('button', { name: 'Show the 1 product with no forecast: too little history' }));
  assert.ok(screen.getByRole('link', { name: 'Browse all 3 products' }));
  await userEvent.click(screen.getByRole('button', { name: '4 weeks' }));
  const four = screen.getByRole('table', { name: /^Check these first: largest forecast demand, 4 weeks/ });
  assert.equal(within(four).getAllByRole('row')[1]?.textContent, '1Fictional product 2B0FIXTUR02801');
  assert.ok(screen.getByText(/The order stays on 13-week likely units ordered\./));
  assert.ok(screen.getByText(/customers are likely to order 80 units in the 4 weeks starting Nov 8, 2026\./));
});

test('Check these first shows ranks 1 to 10, no 11th, and an untitled product by its ASIN', async () => {
  const twelve = rankedList(12);
  (twelve.entries[3] as { title: string | null }).title = null;
  serve((url) => (url.endsWith('/status') ? [200, stale] : [200, twelve]));
  render(<DemandForecastView/>);
  const table = await screen.findByRole('table', { name: SHORT });
  const rows = within(table).getAllByRole('row').slice(1);
  assert.equal(rows.length, 10);
  assert.deepEqual(rows.map((row) => within(row).getAllByRole('cell')[0]?.textContent), ['1', '2', '3', '4', '5', '6', '7', '8', '9', '10']);
  assert.equal(within(rows[3] as HTMLElement).getByRole('rowheader').textContent, 'Untitled productB0RANK0004');
  assert.equal(within(table).queryByText('Fictional product 11'), null);
  assert.ok(screen.getByRole('link', { name: 'Browse all 12 products' }));
  assert.equal(document.querySelector('.df-reasons'), null);
});

test('with no forecast product to rank the card says so and keeps the reasons and Browse all (H5)', async () => {
  const withheld = structuredClone(listed);
  for (const entry of withheld.entries) {
    entry.targets.units_ordered = { ...structuredClone(listed.entries[2]?.targets.units_ordered) } as never;
  }
  serve((url) => (url.endsWith('/status') ? [200, stale] : [200, withheld]));
  render(<DemandForecastView/>);
  assert.ok(await screen.findByText("No product has an ordered-units forecast in this update, so there's no demand to rank."));
  assert.ok(screen.getByText('3 products in this forecast for the 13 weeks starting Nov 8, 2026.'));
  assert.equal(screen.queryByRole('table'), null);
  assert.ok(screen.getByRole('link', { name: 'Browse all 3 products' }));
});

test('Browse all shows the full list: every measure, one merged cell for no forecast, no answer or search', async () => {
  serve(forecastRoutes());
  render(<DemandForecastView/>);
  await userEvent.click(await screen.findByRole('link', { name: 'Browse all 3 products' }));
  const table = await screen.findByRole('table', { name: LIST_NAME });
  assert.equal(screen.queryByRole('table', { name: SHORT }), null);
  assert.equal(screen.queryByRole('search'), null);
  assert.equal(document.querySelector('[data-slot="answer"]'), null);
  const rank = within(table).getByRole('columnheader', { name: /^Rank/ });
  assert.match(rank.textContent ?? '', /13-week likely demand/);
  const first = within(table).getByRole('row', { name: /Fictional product 2/ });
  assert.deepEqual(within(first).getAllByRole('cell').map((cell) => cell.textContent), ['1', '260', '260', '16,048.50', '16,048.50', '1']);
  assert.equal(within(table).queryByText(/Chronos|model/), null);
  const third = within(table).getByRole('row', { name: /Fictional product 3/ });
  assert.deepEqual(within(third).getAllByRole('cell').map((cell) => cell.textContent), ['3', 'No forecast: too little history', '—Not shown for a product without a forecast']);
  await userEvent.click(screen.getByRole('button', { name: '4 weeks' }));
  assert.ok(within(screen.getByRole('table', { name: /^Likely totals, 4 weeks/ })).getByText('Rows stay ordered by 13-week likely demand.'));
});

test('the chips and the reasons line ask the server for their rows (G-05)', async () => {
  const seen = serve((url) => {
    if (url.endsWith('/status')) return [200, stale];
    return url.includes('status=INSUFFICIENT_CONTEXT') ? [200, { ...structuredClone(emptyFilter), query: { status: 'INSUFFICIENT_CONTEXT', page: 1 } }] : [200, listed];
  });
  render(<DemandForecastView/>);
  await userEvent.click(await screen.findByRole('button', { name: 'Show the 1 product with no forecast: too little history' }));
  assert.ok(await screen.findByText(/No product is without a forecast for this reason/));
  assert.ok(seen.includes('/api/demand-forecast?status=INSUFFICIENT_CONTEXT'));
  const chips = screen.getByRole('group', { name: 'Products by ordered-units forecast' });
  assert.deepEqual(within(chips).getAllByRole('button').map((chip) => chip.textContent), [
    'All products 3', 'With an ordered-units forecast', 'No ordered-units forecast: too little history 1',
  ]);
  assert.equal(within(chips).getByRole('button', { name: /too little history/ }).getAttribute('aria-pressed'), 'true');
  await userEvent.click(screen.getByRole('button', { name: 'Show all products' }));
  await screen.findByRole('table', { name: LIST_NAME });
  await userEvent.click(screen.getByRole('button', { name: 'With an ordered-units forecast' }));
  await waitFor(() => assert.ok(seen.includes('/api/demand-forecast?status=shown')));
});

test('an out-of-date list shows the banner with one line per served cause', async () => {
  const outOfDate = { ...structuredClone(listed), freshness: structuredClone(stale).freshness };
  serve((url) => (url.endsWith('/status') ? [200, stale] : [200, outOfDate]));
  render(<DemandForecastView/>);
  const banner = (await screen.findByRole('heading', { name: 'This forecast is out of date' })).closest('section');
  assert.ok(banner);
  assert.equal(banner.querySelectorAll('[data-cause]').length, 2);
  assert.ok(within(banner).getByText('It was made 17 days ago; forecasts count as up to date for 16 days.'));
  assert.ok(within(banner).getByText('The weekly update on Nov 8, 2026 was held by a quality check.'));
  assert.ok(screen.getByText('Out of date · sales through Nov 7, 2026'));
});

test('the freshness pop-up carries the status, and a failed status offers its own Try again', async () => {
  let statusDown = true;
  const seen = serve((url) => {
    if (url.endsWith('/status')) return statusDown ? [503, storeDown] : [200, stale];
    return [200, listed];
  });
  render(<DemandForecastView/>);
  await screen.findByRole('table', { name: SHORT });
  await userEvent.click(screen.getByRole('button', { name: 'About this forecast' }));
  assert.ok(await screen.findByText(/The update schedule can't be loaded right now\./));
  assert.ok(screen.getByText('Made Nov 8, 2026 (8 days ago) from Vendor Central sales through Nov 7, 2026.'));
  statusDown = false;
  const lists = seen.filter((url) => !url.endsWith('/status')).length;
  await userEvent.click(screen.getByRole('button', { name: 'Try again' }));
  assert.ok(await screen.findByText('Next daily check: Nov 17, 2026, 13:00 UTC.'));
  assert.ok(screen.getByText('Next check that can use new sales data: Nov 19, 2026, 13:00 UTC.'));
  assert.equal(seen.filter((url) => !url.endsWith('/status')).length, lists);
});

test('a short code gets E-ASIN and a name gets E-NAME, inline and before any request', async () => {
  const seen = serve(forecastRoutes());
  render(<DemandForecastView/>);
  await screen.findByRole('table', { name: SHORT });
  const input = searchBox();
  await userEvent.type(input, 'B0FIX');
  await userEvent.click(screen.getByRole('button', { name: 'Find' }));
  const error = screen.getByText('An ASIN is 10 letters or digits, for example B0FIXTUR02.');
  assert.equal(input.getAttribute('aria-invalid'), 'true');
  assert.ok((input.getAttribute('aria-describedby') ?? '').split(' ').includes(error.id));
  for (const name of ['Blender', 'ABC', 'Northstar Home Product 8']) {
    await userEvent.clear(input);
    await userEvent.type(input, name);
    await userEvent.click(screen.getByRole('button', { name: 'Find' }));
    assert.ok(screen.getByText("Search finds a product by its ASIN, 10 letters or digits, for example B0FIXTUR02. Searching by product name isn't available here; pick a product from Check these first or Browse all."), name);
  }
  assert.equal(seen.filter((url) => url.includes('asin=')).length, 0);
});

test('not in this forecast answers inline under the box; a 10-letter word is sent and also says names are not searched', async () => {
  const seen = serve((url) => {
    if (url.endsWith('/status')) return [200, stale];
    if (url.includes('asin=')) return [200, absent(new URL(url, 'http://x').searchParams.get('asin') ?? '')];
    return [200, listed];
  });
  render(<DemandForecastView/>);
  await screen.findByRole('table', { name: SHORT });
  await userEvent.type(searchBox(), 'B0NOTREAL0{Enter}');
  assert.ok(await screen.findByText("B0NOTREAL0 isn't in this forecast. This forecast covers 3 products with Vendor Central sales history. Check the ASIN, or pick a product below."));
  assert.ok(screen.getByRole('table', { name: SHORT }));
  assert.equal(focused(), 'input:');
  assert.equal(window.location.search, '');
  await userEvent.clear(searchBox());
  await userEvent.type(searchBox(), 'toothbrush{Enter}');
  assert.ok(await screen.findByText("TOOTHBRUSH isn't in this forecast. This forecast covers 3 products with Vendor Central sales history. Searching by product name isn't available here; pick a product from Check these first or Browse all."));
  assert.deepEqual(seen.filter((url) => url.includes('asin=')), ['/api/demand-forecast?asin=B0NOTREAL0', '/api/demand-forecast?asin=TOOTHBRUSH']);
});

test('a server ASIN_INVALID shows E-ASIN and keeps focus in the box', async () => {
  const invalid = { schema_version: 1, refused: 'ASIN_INVALID', code: 'ADLY-FORECAST_READ-0001', detail: 'x' };
  serve((url) => {
    if (url.endsWith('/status')) return [200, stale];
    return url.includes('asin=') ? [400, invalid] : [200, listed];
  });
  render(<DemandForecastView/>);
  await screen.findByRole('table', { name: SHORT });
  await userEvent.type(searchBox(), 'B0FIXTUR0X{Enter}');
  assert.ok(await screen.findByText('An ASIN is 10 letters or digits, for example B0FIXTUR02.'));
  assert.ok(document.activeElement === searchBox(), focused());
});

test('while finding, a repeat is ignored, a new ASIN aborts the first, and the found product opens with one request', async () => {
  const requests: { asin: string; signal: AbortSignal | undefined; answer: ReturnType<typeof deferred> }[] = [];
  serve((url, signal) => {
    if (url.endsWith('/status')) return [200, stale];
    if (!url.includes('asin=')) return [200, listed];
    const answer = deferred(signal);
    requests.push({ asin: new URL(url, 'http://x').searchParams.get('asin') ?? '', signal, answer });
    return answer.promise;
  });
  render(<DemandForecastView/>);
  await screen.findByRole('table', { name: SHORT });
  await userEvent.type(searchBox(), 'B0FIXTUR09{Enter}');
  const busy = screen.getByRole('button', { name: 'Finding…' });
  assert.equal(busy.getAttribute('aria-disabled'), 'true');
  assert.equal(screen.getByRole('search').getAttribute('aria-busy'), 'true');
  await userEvent.click(busy);
  assert.equal(requests.length, 1);
  await userEvent.clear(searchBox());
  // Editing to a different ASIN cancels the pending search at once (B-03).
  assert.equal(requests[0]?.signal?.aborted, true);
  await userEvent.type(searchBox(), 'B0FIXTUR01{Enter}');
  assert.equal(requests.length, 2);
  await act(async () => { requests[1]?.answer.resolve([200, detail]); });
  assert.ok(await screen.findByText('ASIN B0FIXTUR01'));
  assert.equal(requests.length, 2);
  assert.equal(window.location.search, '?asin=B0FIXTUR01');
});

test('a different ASIN submitted while finding aborts the first, and a late answer to it is ignored (B-03)', async () => {
  const requests: { signal: AbortSignal | undefined; answer: ReturnType<typeof deferred> }[] = [];
  serve((url, signal) => {
    if (url.endsWith('/status')) return [200, stale];
    if (!url.includes('asin=')) return [200, listed];
    const answer = deferred(signal, true);
    requests.push({ signal, answer });
    return answer.promise;
  });
  render(<DemandForecastView/>);
  await screen.findByRole('table', { name: SHORT });
  await userEvent.type(searchBox(), 'B0FIXTUR09{Enter}');
  await userEvent.clear(searchBox());
  assert.equal(requests[0]?.signal?.aborted, true);
  await userEvent.type(searchBox(), 'b0fixtur08{Enter}');
  // A repeat of the pending ASIN sends nothing.
  await userEvent.type(searchBox(), '{Enter}');
  assert.equal(requests.length, 2);
  await userEvent.type(searchBox(), '{Backspace}7{Enter}');
  assert.equal(requests[1]?.signal?.aborted, true);
  assert.equal(requests.length, 3);
  // The first two answers land after they were aborted: nothing opens.
  await act(async () => {
    requests[0]?.answer.resolve([200, detail]);
    requests[1]?.answer.resolve([200, detail]);
  });
  assert.ok(screen.getByRole('table', { name: SHORT }));
  await act(async () => { requests[2]?.answer.resolve([200, absent('B0FIXTUR07')]); });
  assert.ok(await screen.findByText(/^B0FIXTUR07 isn't in this forecast\./));
});

test('a failed search offers Try again for the ASIN still in the box; an edit drops it (B-03)', async () => {
  let down = true;
  const seen = serve((url) => {
    if (url.endsWith('/status')) return [200, stale];
    if (url.includes('asin=')) return down ? [503, storeDown] : [200, detail];
    return [200, listed];
  });
  render(<DemandForecastView/>);
  await screen.findByRole('table', { name: SHORT });
  await userEvent.type(searchBox(), 'B0FIXTUR01{Enter}');
  assert.ok(await screen.findByText(/^The forecast can't be loaded right now\. Try again in a minute\./));
  await userEvent.click(screen.getByRole('button', { name: 'Try again' }));
  assert.ok(await screen.findByText(/^The forecast can't be loaded right now\./));
  assert.ok(document.activeElement === searchBox(), focused());
  assert.ok(screen.getByRole('table', { name: SHORT }));
  await userEvent.type(searchBox(), 'X');
  assert.equal(screen.queryByRole('button', { name: 'Try again' }), null);
  await userEvent.type(searchBox(), '{Backspace}{Enter}');
  assert.ok(await screen.findByRole('button', { name: 'Try again' }));
  down = false;
  await userEvent.click(screen.getByRole('button', { name: 'Try again' }));
  assert.ok(await screen.findByText('ASIN B0FIXTUR01'));
  assert.deepEqual(seen.filter((url) => url.includes('asin=')).length, 4);
  assert.equal(seen.filter((url) => url === '/api/demand-forecast').length, 1);
});

test('search works while the list loads; "no forecast yet" shows only until the list arrives (B-05, H6)', async () => {
  let list: ReturnType<typeof deferred> | null = null;
  serve((url, signal) => {
    if (url.endsWith('/status')) return [200, stale];
    if (url.includes('asin=')) return [200, noneYet];
    list = deferred(signal);
    return list.promise;
  });
  const { container } = render(<DemandForecastView/>);
  assert.ok(container.querySelector('[aria-busy="true"] .skel'));
  assert.equal(container.querySelector('.skeleton'), null);
  await userEvent.type(searchBox(), 'B0FIX{Enter}');
  // No example until the start list arrives (G-08).
  assert.ok(screen.getByText('An ASIN is 10 letters or digits.'));
  await userEvent.type(searchBox(), 'TUR01{Enter}');
  assert.ok(await screen.findByText(/^No forecast has been made yet\./));
  await act(async () => { (list as ReturnType<typeof deferred> | null)?.resolve([200, listed]); });
  await screen.findByRole('table', { name: SHORT });
  assert.equal(screen.queryByText(/^No forecast has been made yet\./), null);
});

test('a list failure while a search is out aborts it and shows the page error', async () => {
  let search: AbortSignal | undefined;
  let list: ReturnType<typeof deferred> | null = null;
  serve((url, signal) => {
    if (url.endsWith('/status')) return [200, stale];
    if (url.includes('asin=')) {
      search = signal;
      return deferred(signal).promise;
    }
    list = deferred(signal);
    return list.promise;
  });
  render(<DemandForecastView/>);
  await userEvent.type(searchBox(), 'B0FIXTUR01{Enter}');
  await act(async () => { (list as ReturnType<typeof deferred> | null)?.resolve([503, storeDown]); });
  assert.ok(await screen.findByText("The forecast can't be loaded right now. Try again in a minute. If it keeps failing, tell your administrator."));
  assert.equal(search?.aborted, true);
});

test('the example ASIN is the first ASIN of the start list on screen, from the list Try again loaded (G-08)', async () => {
  let down = true;
  serve((url) => {
    if (url.endsWith('/status')) return [200, stale];
    return down ? [503, storeDown] : [200, { ...structuredClone(listed), entries: structuredClone(listed).entries.slice(1) }];
  });
  render(<DemandForecastView/>);
  assert.ok(await screen.findByText(/^The forecast can't be loaded right now/));
  down = false;
  await userEvent.click(screen.getByRole('button', { name: 'Try again' }));
  await screen.findByRole('table', { name: SHORT });
  await userEvent.type(searchBox(), 'ABC1{Enter}');
  assert.ok(screen.getByText('An ASIN is 10 letters or digits, for example B0FIXTUR01.'));
});

test('each pop-up has one header control and one stacked-form copy, all distinct', async () => {
  serve(forecastRoutes());
  const { container } = render(<DemandForecastView/>);
  const table = await screen.findByRole('table', { name: SHORT });
  const names = (root: Element) => [...root.querySelectorAll('button.infotip-btn')].map((button) => button.getAttribute('aria-label'));
  const shortTips = container.querySelector('.df-short-tips');
  assert.ok(shortTips);
  assert.deepEqual(names(table.querySelector('thead') as Element), ['About rank', 'About weeks shipped less than ordered']);
  assert.deepEqual(names(shortTips), ['About rank', 'About weeks shipped less than ordered']);
  await userEvent.click(screen.getByRole('link', { name: 'Browse all 3 products' }));
  const list = await screen.findByRole('table', { name: LIST_NAME });
  const tips = container.querySelector('.df-table-tips .stack-tips');
  assert.ok(tips);
  assert.deepEqual(names(list.querySelector('thead') as Element), ['About rank', 'About Amazon wholesale cost', 'About weeks shipped less than ordered']);
  assert.deepEqual(names(tips), ['About rank', 'About Amazon wholesale cost', 'About weeks shipped less than ordered']);
  const controls = [...container.querySelectorAll('button.infotip-btn')].map((button) => button.getAttribute('aria-controls'));
  assert.equal(new Set(controls).size, controls.length);
});

test('no statistical term shows outside a closed pop-up', async () => {
  serve(forecastRoutes());
  const { container } = render(<DemandForecastView/>);
  await screen.findByRole('table', { name: SHORT });
  await userEvent.click(screen.getByText('How this forecast is made'));
  const text = visibleText(container);
  assert.match(text, /Units ordered: 2 products forecast by the Chronos-2 forecasting model\./);
  assert.doesNotMatch(text, /p10|p50|p90|MASE/);
});

test('a PAGE_INVALID on a later page of Browse all returns to page 1 once, from the start list', async () => {
  const twoPages = { ...structuredClone(listed), page_count: 2 };
  const seen = serve((url) => {
    if (url.endsWith('/status')) return [200, stale];
    return url.includes('page=2') ? [400, refusal] : [200, twoPages];
  });
  render(<DemandForecastView/>);
  await userEvent.click(await screen.findByRole('link', { name: 'Browse all 3 products' }));
  assert.ok(await screen.findByText('Page 1 of 2 · 3 products'));
  await userEvent.click(screen.getByRole('button', { name: 'Next' }));
  assert.ok(await screen.findByText('The forecast was updated, so the list is back on page 1.'));
  assert.deepEqual(seen.filter((url) => !url.endsWith('/status')), ['/api/demand-forecast', '/api/demand-forecast?page=2']);
});

test('a store failure shows the read error with Try again, which refetches', async () => {
  let failing = true;
  serve((url) => {
    if (url.endsWith('/status')) return [200, stale];
    return failing ? [503, storeDown] : [200, listed];
  });
  render(<DemandForecastView/>);
  assert.ok(await screen.findByText("The forecast can't be loaded right now. Try again in a minute. If it keeps failing, tell your administrator."));
  failing = false;
  await userEvent.click(screen.getByRole('button', { name: 'Try again' }));
  assert.ok(await screen.findByRole('table', { name: SHORT }));
});

test('with no generation the page says so and names the next new-data update', async () => {
  serve((url) => (url.endsWith('/status') ? [200, noGeneration] : [200, unavailableList]));
  render(<DemandForecastView/>);
  assert.ok(await screen.findByText(/No forecast has been made yet\. .* The next update that can use new sales data is Nov 19, 2026, 13:00 UTC\./));
});

test('unmounting aborts the in-flight requests without an error panel', async () => {
  const aborted: string[] = [];
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    await new Promise((_resolve, reject) => {
      // As the real fetch: an already-aborted signal rejects at once.
      if (init?.signal?.aborted) {
        aborted.push(String(input));
        reject(new DOMException('aborted', 'AbortError'));
        return;
      }
      init?.signal?.addEventListener('abort', () => {
        aborted.push(String(input));
        reject(new DOMException('aborted', 'AbortError'));
      });
    });
    return new Response('{}');
  }) as typeof fetch;
  const view = render(<DemandForecastView/>);
  view.unmount();
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.deepEqual(aborted.sort(), ['/api/demand-forecast', '/api/demand-forecast/status']);
});

const republished = { generation_id: 'run-2-0123456789ab', published_at: '2026-11-15T14:05:09Z' };
const detailOfNextGeneration = {
  ...structuredClone(detail),
  provenance: { ...structuredClone(detail).provenance, ...republished },
};
const statusOfNextGeneration = {
  ...structuredClone(stale),
  generation: { ...structuredClone(stale).generation, ...republished },
};

test('a publish between the list and a found product renders neither until the latest is refetched', async () => {
  let published = false;
  const seen = serve((url) => {
    if (url.endsWith('/status')) return [200, stale];
    if (url.includes('asin=')) return [200, published ? detail : detailOfNextGeneration];
    return [200, listed];
  });
  render(<DemandForecastView/>);
  await screen.findByRole('table', { name: SHORT });
  await userEvent.type(searchBox(), 'B0FIXTUR01{Enter}');
  assert.ok(await screen.findByText('A newer forecast was published while this page loaded.'));
  assert.equal(screen.queryByRole('table', { name: SHORT }), null);
  published = true;
  const before = seen.length;
  await userEvent.click(screen.getByRole('button', { name: 'Show the latest forecast' }));
  await screen.findByText('ASIN B0FIXTUR01');
  assert.deepEqual(seen.slice(before).sort(), [
    '/api/demand-forecast',
    '/api/demand-forecast/status',
    '/api/demand-forecast?asin=B0FIXTUR01',
  ]);
});

test('a publish between the list and the status renders neither until the latest is refetched', async () => {
  let published = false;
  serve((url) => (url.endsWith('/status') ? [200, published ? stale : statusOfNextGeneration] : [200, listed]));
  render(<DemandForecastView/>);
  assert.ok(await screen.findByText('A newer forecast was published while this page loaded.'));
  assert.equal(screen.queryByRole('table', { name: SHORT }), null);
  published = true;
  await userEvent.click(screen.getByRole('button', { name: 'Show the latest forecast' }));
  assert.ok(await screen.findByRole('table', { name: SHORT }));
});
