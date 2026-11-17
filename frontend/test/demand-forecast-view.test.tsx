// SPDX-License-Identifier: MIT-0
/**
 * The Demand forecast tab over the shared wire fixtures (ADR-163): the ready list with its view
 * control from the server horizon, the filter, the pager, in-place detail, the PAGE_INVALID reset,
 * Try again on a service failure and the no-generation state. A wrong state shows a planner a
 * stale or partial list as current, or an outage where the page only cancelled a request.
 */
import assert from 'node:assert/strict';
import { render, screen } from '@testing-library/react';
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

const unavailableList = {
  ...structuredClone(emptyFilter),
  outcome: 'unavailable', provenance: null, horizon: null, page_count: 0, item_count: null,
  counts_by_reason: null, counts_by_model: null, currency_codes: [],
  freshness: { state: 'unavailable', age_days: null, stale_after_days: 16, causes: [] },
  query: { status: null, page: 1 },
};

test('the ready list shows the stored totals, the server views and the status panel', async () => {
  serve((url) => (url.endsWith('/status') ? [200, stale] : [200, listed]));
  render(<DemandForecastView/>);
  assert.ok(await screen.findByRole('table', { name: /Forecast totals \(p50\) for the next 13 weeks/ }));
  assert.ok(screen.getAllByText('16,048.50 USD').length > 0);
  assert.deepEqual(screen.getAllByRole('radio').map((radio) => radio.parentElement?.textContent), ['4 weeks', '8 weeks', '13 weeks']);
  assert.ok(await screen.findByText('Last forecast run'));
  assert.ok(screen.getByText(/Held by a quality check, 2026-11-08 14:05:09 UTC/));
  await userEvent.click(screen.getByRole('radio', { name: '4 weeks' }));
  assert.ok(screen.getByRole('table', { name: /next 4 weeks/ }));
  assert.ok(screen.getAllByText('80 units ordered').length > 0);
});

test('a filter refetches page 1 of that status and an empty one says so', async () => {
  const seen = serve((url) => {
    if (url.endsWith('/status')) return [200, stale];
    return url.includes('status=ASIN_INACTIVE') ? [200, emptyFilter] : [200, listed];
  });
  render(<DemandForecastView/>);
  await screen.findByRole('table', { name: /Forecast totals/ });
  await userEvent.selectOptions(screen.getByLabelText('Show'), 'ASIN_INACTIVE');
  assert.ok(await screen.findByText(/No product matches this filter/));
  assert.ok(seen.includes('/api/demand-forecast?status=ASIN_INACTIVE'));
});

test('selecting a product loads its detail in place', async () => {
  serve((url) => {
    if (url.endsWith('/status')) return [200, stale];
    return url.includes('asin=') ? [200, detail] : [200, listed];
  });
  render(<DemandForecastView/>);
  await userEvent.click(await screen.findByRole('button', { name: 'B0FIXTUR01' }));
  assert.ok(await screen.findByRole('heading', { name: /B0FIXTUR01: Fictional product 1/ }));
  assert.ok(screen.getAllByText(/Beat repeating last week's value/).length > 0);
});

test('a malformed ASIN search is refused inline before any request', async () => {
  const seen = serve((url) => (url.endsWith('/status') ? [200, stale] : [200, listed]));
  render(<DemandForecastView/>);
  await screen.findByRole('table', { name: /Forecast totals/ });
  await userEvent.type(screen.getByLabelText('Find a product by ASIN'), 'B0FIX');
  await userEvent.click(screen.getByRole('button', { name: 'Find' }));
  assert.ok(screen.getByText('Enter a 10-character ASIN.'));
  assert.equal(seen.filter((url) => url.includes('asin=')).length, 0);
});

test('a PAGE_INVALID on a later page returns to page 1 once and says why', async () => {
  const twoPages = { ...structuredClone(listed), page_count: 2 };
  const seen = serve((url) => {
    if (url.endsWith('/status')) return [200, stale];
    return url.includes('page=2') ? [400, refusal] : [200, twoPages];
  });
  render(<DemandForecastView/>);
  await userEvent.click(await screen.findByRole('button', { name: 'Next' }));
  assert.ok(await screen.findByText('The forecast was updated, so the list is back on page 1.'));
  assert.deepEqual(seen.filter((url) => !url.endsWith('/status')), [
    '/api/demand-forecast',
    '/api/demand-forecast?page=2',
    '/api/demand-forecast',
  ]);
});

test('a store failure offers Try again, which refetches', async () => {
  let failing = true;
  serve((url) => {
    if (url.endsWith('/status')) return [200, stale];
    return failing
      ? [503, { schema_version: 1, refused: 'FORECAST_STORE_UNAVAILABLE', code: 'ADLY-FORECAST_READ-0009', detail: 'x' }]
      : [200, listed];
  });
  render(<DemandForecastView/>);
  assert.ok(await screen.findByText("The forecast can't be read right now."));
  failing = false;
  await userEvent.click(screen.getByRole('button', { name: 'Try again' }));
  assert.ok(await screen.findByRole('table', { name: /Forecast totals/ }));
});

test('with no generation the page says so and names the next new-data check', async () => {
  serve((url) => (url.endsWith('/status') ? [200, noGeneration] : [200, unavailableList]));
  render(<DemandForecastView/>);
  assert.ok(await screen.findByText(/No forecast has been published yet/));
  assert.ok(await screen.findByText(/next check that can take new data is 2026-11-19 13:00:00 UTC/));
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
