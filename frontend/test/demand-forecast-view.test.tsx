// SPDX-License-Identifier: MIT-0
/**
 * The Demand forecast tab over the shared wire fixtures (ADR-163, revised design S1 and S5 to S9):
 * the scope card and freshness pop-up, the out-of-date banner, the answer sentence, chips that ask
 * the server for their rows, the ranked list, the pager, the PAGE_INVALID reset, Try again, the
 * no-generation state and a publish between requests. A wrong state shows a planner a stale or
 * partial list as current, a served 1 as a measured fact for a product with no forecast, another
 * generation's ASIN as an example, or an outage where the page only cancelled a request.
 */
import assert from 'node:assert/strict';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, test } from 'vitest';
import { DemandForecastView } from '@/components/DemandForecastView';
import detail from './fixtures/demand-forecast-detail.json';
import listed from './fixtures/demand-forecast-list.json';
import emptyFilter from './fixtures/demand-forecast-list-empty-filter.json';
import refusal from './fixtures/demand-forecast-refusal.json';
import noGeneration from './fixtures/demand-forecast-status-no-generation.json';
import stale from './fixtures/demand-forecast-status-stale.json';

const originalFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = originalFetch; });

type Route = (url: string) => [number, unknown];

function serve(route: Route): string[] {
  const seen: string[] = [];
  globalThis.fetch = (async (input: RequestInfo | URL) => {
    const url = String(input);
    seen.push(url);
    const [status, body] = route(url);
    return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
  }) as typeof fetch;
  return seen;
}

const storeDown = { schema_version: 1, refused: 'FORECAST_STORE_UNAVAILABLE', code: 'ADLY-FORECAST_READ-0009', detail: 'x' };
const unavailableList = {
  ...structuredClone(emptyFilter),
  outcome: 'unavailable', provenance: null, horizon: null, page_count: 0, item_count: null,
  counts_by_reason: null, counts_by_model: null, currency_codes: [],
  freshness: { state: 'unavailable', age_days: null, stale_after_days: 16, causes: [] },
  query: { status: null, page: 1 },
};
const LIST_NAME = /^Likely totals, 13 weeks starting Nov 8, 2026/;

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

test('the view draws no h1; the scope card, answer and ranked list read only served values', async () => {
  serve((url) => (url.endsWith('/status') ? [200, stale] : [200, listed]));
  const { container } = render(<DemandForecastView/>);
  const table = await screen.findByRole('table', { name: LIST_NAME });
  assert.equal(container.querySelector('h1'), null);
  assert.ok(screen.getByText('3 products'));
  assert.ok(screen.getByText('Vendor Central · ordered and shipped'));
  assert.ok(screen.getByText('Up to date · sales through Nov 7, 2026'));
  assert.ok(screen.getByText('13 weeks starting Nov 8, 2026 · Sunday to Saturday weeks'));
  assert.ok(screen.getByText('Fictional product 2 leads the 13-week forecast: customers are likely to order 260 units in the 13 weeks starting Nov 8, 2026.'));
  const rank = within(table).getByRole('columnheader', { name: /^Rank/ });
  assert.match(rank.textContent ?? '', /13-week likely demand/);
  assert.equal(within(table).queryByText(/Rows stay ordered/), null);
  const row = within(table).getByRole('row', { name: /Fictional product 2/ });
  assert.deepEqual(within(row).getAllByRole('cell').map((cell) => cell.textContent), ['1', '260', '260', '16,048.50', '16,048.50', '1']);
  // No model column (D2).
  assert.equal(within(table).queryByText(/Chronos|model/), null);
  await userEvent.click(screen.getByRole('button', { name: '4 weeks' }));
  const four = screen.getByRole('table', { name: /^Likely totals, 4 weeks starting Nov 8, 2026/ });
  assert.ok(within(four).getByText('Rows stay ordered by 13-week likely demand.'));
  assert.ok(screen.getByText(/customers are likely to order 80 units in the 4 weeks starting Nov 8, 2026\./));
});

test('a product with no forecast shows one merged cell and a dash, never its served short-shipped count', async () => {
  serve((url) => (url.endsWith('/status') ? [200, stale] : [200, listed]));
  render(<DemandForecastView/>);
  const table = await screen.findByRole('table', { name: LIST_NAME });
  const row = within(table).getByRole('row', { name: /Fictional product 3/ });
  const cells = within(row).getAllByRole('cell').map((cell) => cell.textContent);
  assert.deepEqual(cells, ['3', 'No forecast: too little history', '—Not shown for a product without a forecast']);
});

test('the chips come from counts_by_reason and each asks the server for its rows', async () => {
  const seen = serve((url) => {
    if (url.endsWith('/status')) return [200, stale];
    return url.includes('status=INSUFFICIENT_CONTEXT') ? [200, { ...structuredClone(emptyFilter), query: { status: 'INSUFFICIENT_CONTEXT', page: 1 } }] : [200, listed];
  });
  render(<DemandForecastView/>);
  await screen.findByRole('table', { name: LIST_NAME });
  const chips = screen.getByRole('group', { name: 'Products by ordered-units forecast' });
  assert.deepEqual(within(chips).getAllByRole('button').map((chip) => chip.textContent), [
    'All products 3', 'With an ordered-units forecast', 'No ordered-units forecast: too little history 1',
  ]);
  await userEvent.click(within(chips).getByRole('button', { name: /too little history/ }));
  assert.ok(await screen.findByText(/No product is without a forecast for this reason/));
  assert.ok(seen.includes('/api/demand-forecast?status=INSUFFICIENT_CONTEXT'));
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
  await screen.findByRole('table', { name: LIST_NAME });
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

test('a malformed ASIN is refused inline before any request, with the example from page 1', async () => {
  const seen = serve((url) => (url.endsWith('/status') ? [200, stale] : [200, listed]));
  render(<DemandForecastView/>);
  await screen.findByRole('table', { name: LIST_NAME });
  const input = screen.getByRole('textbox', { name: 'Find a product by ASIN' });
  await userEvent.type(input, 'ABC');
  await userEvent.click(screen.getByRole('button', { name: 'Find' }));
  const error = screen.getByText('An ASIN is 10 letters or digits, for example B0FIXTUR02.');
  assert.equal(input.getAttribute('aria-invalid'), 'true');
  assert.ok((input.getAttribute('aria-describedby') ?? '').split(' ').includes(error.id));
  assert.equal(seen.filter((url) => url.includes('asin=')).length, 0);
});

test('a server ASIN_INVALID shows the same inline error and returns focus to the input', async () => {
  const invalid = { schema_version: 1, refused: 'ASIN_INVALID', code: 'ADLY-FORECAST_READ-0001', detail: 'x' };
  serve((url) => {
    if (url.endsWith('/status')) return [200, stale];
    return url.includes('asin=') ? [400, invalid] : [200, listed];
  });
  render(<DemandForecastView/>);
  await screen.findByRole('table', { name: LIST_NAME });
  const input = screen.getByRole('textbox', { name: 'Find a product by ASIN' });
  await userEvent.type(input, 'B0FIXTUR0X');
  await userEvent.click(screen.getByRole('button', { name: 'Find' }));
  assert.ok(await screen.findByText('An ASIN is 10 letters or digits, for example B0FIXTUR02.'));
  await waitFor(() => assert.ok(document.activeElement === screen.getByRole('textbox', { name: 'Find a product by ASIN' })));
});

test('the example ASIN is dropped on Try again and shown again only from the new generation page 1 (G-08)', async () => {
  let generation = 'run-1-0123456789ab';
  let filterDown = true;
  const nextStatus = () => ({ ...structuredClone(stale), generation: { ...structuredClone(stale).generation, generation_id: generation } });
  const nextList = (status: string | null) => ({
    ...structuredClone(listed),
    provenance: { ...structuredClone(listed).provenance, generation_id: generation },
    query: { status, page: 1 },
  });
  serve((url) => {
    if (url.endsWith('/status')) return [200, nextStatus()];
    if (url.includes('status=shown')) return filterDown ? [503, storeDown] : [200, nextList('shown')];
    return [200, nextList(null)];
  });
  render(<DemandForecastView/>);
  await screen.findByRole('table', { name: LIST_NAME });
  await userEvent.type(screen.getByRole('textbox', { name: 'Find a product by ASIN' }), 'ABC');
  await userEvent.click(screen.getByRole('button', { name: 'Find' }));
  assert.ok(screen.getByText('An ASIN is 10 letters or digits, for example B0FIXTUR02.'));
  await userEvent.click(screen.getByRole('button', { name: 'With an ordered-units forecast' }));
  assert.ok(await screen.findByText("The forecast can't be loaded right now. Try again in a minute. If it keeps failing, tell your administrator."));
  generation = 'run-2-0123456789ab';
  filterDown = false;
  await userEvent.click(screen.getByRole('button', { name: 'Try again' }));
  await screen.findByRole('table', { name: LIST_NAME });
  // The filtered page of the new generation supplies no example, and the old one is gone.
  assert.ok(screen.getByText('An ASIN is 10 letters or digits.'));
  await userEvent.click(screen.getByRole('button', { name: /^All products/ }));
  assert.ok(await screen.findByText('An ASIN is 10 letters or digits, for example B0FIXTUR02.'));
});

test('the header pop-ups and the stacked-form caption pop-ups are distinct controls', async () => {
  serve((url) => (url.endsWith('/status') ? [200, stale] : [200, listed]));
  const { container } = render(<DemandForecastView/>);
  const table = await screen.findByRole('table', { name: LIST_NAME });
  const head = table.querySelector('thead');
  const tips = table.querySelector('caption .stack-tips');
  assert.ok(head && tips);
  const names = (root: Element) => [...root.querySelectorAll('button.infotip-btn')].map((button) => button.getAttribute('aria-label'));
  assert.deepEqual(names(head), ['About rank', 'About Amazon wholesale cost', 'About weeks shipped less than ordered']);
  assert.deepEqual(names(tips), ['About rank', 'About Amazon wholesale cost', 'About weeks shipped less than ordered']);
  const controls = [...container.querySelectorAll('button.infotip-btn')].map((button) => button.getAttribute('aria-controls'));
  assert.equal(new Set(controls).size, controls.length);
});

test('no statistical term shows outside a closed pop-up', async () => {
  serve((url) => (url.endsWith('/status') ? [200, stale] : [200, listed]));
  const { container } = render(<DemandForecastView/>);
  await screen.findByRole('table', { name: LIST_NAME });
  await userEvent.click(screen.getByText('How this forecast is made'));
  const text = visibleText(container);
  assert.match(text, /Units ordered: 2 products forecast by the Chronos-2 forecasting model\./);
  assert.doesNotMatch(text, /p10|p50|p90|MASE/);
});

test('loading reserves the layout with .skel shapes under a busy region', () => {
  globalThis.fetch = (() => new Promise<Response>(() => undefined)) as typeof fetch;
  const { container } = render(<DemandForecastView/>);
  assert.ok(container.querySelector('[aria-busy="true"] .skel.table'));
  assert.equal(container.querySelector('.skeleton'), null);
});

test('a PAGE_INVALID on a later page returns to page 1 once and says why', async () => {
  const twoPages = { ...structuredClone(listed), page_count: 2 };
  const seen = serve((url) => {
    if (url.endsWith('/status')) return [200, stale];
    return url.includes('page=2') ? [400, refusal] : [200, twoPages];
  });
  render(<DemandForecastView/>);
  assert.ok(await screen.findByText('Page 1 of 2 · 3 products'));
  await userEvent.click(screen.getByRole('button', { name: 'Next' }));
  assert.ok(await screen.findByText('The forecast was updated, so the list is back on page 1.'));
  assert.deepEqual(seen.filter((url) => !url.endsWith('/status')), [
    '/api/demand-forecast',
    '/api/demand-forecast?page=2',
    '/api/demand-forecast',
  ]);
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
  assert.ok(await screen.findByRole('table', { name: LIST_NAME }));
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

test('a publish between the list and the detail renders neither until the latest is refetched', async () => {
  let published = false;
  const seen = serve((url) => {
    if (url.endsWith('/status')) return [200, stale];
    if (url.includes('asin=')) return [200, published ? detail : detailOfNextGeneration];
    return [200, listed];
  });
  render(<DemandForecastView/>);
  await userEvent.click(await screen.findByRole('button', { name: 'Fictional product 1' }));
  assert.ok(await screen.findByText('A newer forecast was published while this page loaded.'));
  assert.equal(screen.queryByRole('table', { name: LIST_NAME }), null);
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
  assert.equal(screen.queryByRole('table', { name: LIST_NAME }), null);
  published = true;
  await userEvent.click(screen.getByRole('button', { name: 'Show the latest forecast' }));
  assert.ok(await screen.findByRole('table', { name: LIST_NAME }));
});
