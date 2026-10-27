// SPDX-License-Identifier: MIT-0
/**
 * Products and ads (UI refresh 3.7, readout A.10): served Retail health rows beside the baked
 * Products and ads part (render format 12) for one chosen channel, from one request. Nothing is
 * added, divided or ranked.
 */
import assert from 'node:assert/strict';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, test } from 'vitest';

import { productsAndAdsRouteAdapter } from '../components/ProductsAndAdsRouteAdapter';
import { ProductsAndAdsView, PRODUCTS_AND_ADS_LINES } from '../components/ProductsAndAdsView';
import { parsePresentationResultV2, productsAndAdsPresentationV2, retailHealthPresentationV2, type ProductsAndAdsPresentation } from '../lib/presentation-v2';
import { PRODUCTS_AND_ADS_COPY, productsAndAdsAnswer } from '../lib/products-and-ads';
import tabResponse from './fixtures/products-and-ads-v2.json';
import parentResponse from './fixtures/retail-health-v2.json';

const clone = <Value,>(value: Value): Value => JSON.parse(JSON.stringify(value)) as Value;
function tab(raw: unknown = tabResponse): Extract<ProductsAndAdsPresentation, { readonly kind: 'parent' }> {
  const served = productsAndAdsPresentationV2(parsePresentationResultV2(clone(raw), 'retail-health'));
  if (served.kind !== 'parent') throw new Error('expected a parent');
  return served;
}
const originalFetch = globalThis.fetch;
afterEach(() => { Object.defineProperty(globalThis, 'fetch', { configurable: true, value: originalFetch }); });

test('a load makes one Retail health request that names the part, and no product-detail request', async () => {
  const calls: { body: unknown; path: string }[] = [];
  document.cookie = 'adl_csrf=csrf; path=/';
  Object.defineProperty(globalThis, 'fetch', {
    configurable: true,
    value: async (input: string | URL | Request, init?: RequestInit) => {
      calls.push({ body: JSON.parse(String(init?.body)), path: String(input) });
      return new Response(JSON.stringify(tabResponse), { status: 200 });
    },
  });
  const loaded = await productsAndAdsRouteAdapter.loadParent('conversation-session', { kind: 'preset', preset_id: '7d' }, new AbortController().signal, undefined);
  assert.equal(loaded.kind, 'parent');
  assert.deepEqual(calls, [{
    body: { product_ads: true, session_id: 'conversation-session', temporal_selection: { kind: 'preset', preset_id: '7d' } },
    path: '/api/presentation/retail-health',
  }]);
});

test('the part is decoded only beside its own parent: same window, same rows in the same order', () => {
  const served = tab();
  assert.deepEqual(served.product_ads?.rows.map((row) => row.child_asin), served.artifact.rows.map((row) => row.child_asin));
  const reordered = clone(tabResponse);
  reordered.page.product_ads.rows.reverse();
  const totalled = clone(tabResponse) as { page: { product_ads: { rows: Record<string, unknown>[] } } };
  totalled.page.product_ads.rows[0]!.total = totalled.page.product_ads.rows[0]!.channels;
  const missing = clone(tabResponse) as { page: Record<string, unknown> };
  delete missing.page.product_ads;
  for (const raw of [reordered, totalled, missing]) assert.throws(() => tab(raw));
  // Retail health's own decoder never accepts the part, so its page never carries one.
  assert.throws(() => retailHealthPresentationV2(parsePresentationResultV2(clone(tabResponse), 'retail-health')));
  assert.equal(retailHealthPresentationV2(parsePresentationResultV2(clone(parentResponse), 'retail-health')).kind, 'parent');
});

test('each row shows its served sales beside the chosen channel credit; another channel switches only the ad columns', async () => {
  const user = userEvent.setup();
  const served = tab();
  const view = render(<ProductsAndAdsView artifact={served.artifact} productAds={served.product_ads} />);
  const table = screen.getByRole('region', { name: new RegExp(PRODUCTS_AND_ADS_LINES.tableTitle) });
  const rows = within(table).getAllByRole('row').slice(1);
  assert.equal(rows.length, served.artifact.rows.length);
  // Amazon DSP credits FICTA 40.00 USD and 4 purchases, with its served change.
  assert.match(rows[0]!.textContent ?? '', /40\.00 USD/);
  assert.match(rows[0]!.textContent ?? '', /\+33\.3%/);
  assert.match(rows[0]!.textContent ?? '', new RegExp(PRODUCTS_AND_ADS_COPY.notMeasured));
  // FICTB's ad section is absent: the row reads the part's own served reason, not a figure.
  const reason = served.product_ads!.rows[1]!.reason!;
  assert.ok(within(rows[1]!).getAllByText(reason).length > 0);
  await user.click(screen.getByRole('button', { name: 'Sponsored Products' }));
  // Sponsored Products credits FICTA 50.00 USD.
  assert.match(within(table).getAllByRole('row')[1]!.textContent ?? '', /50\.00 USD/);
  // No product return on ad spend, and the two systems never add up.
  assert.ok(screen.getByText(PRODUCTS_AND_ADS_LINES.noProductReturn));
  assert.equal(/ROAS|×/.test(table.textContent ?? ''), false);
  view.unmount();
});

test('the answer names the served count and the first served row; an empty body reads its fallback', () => {
  const served = tab();
  const answer = productsAndAdsAnswer(served.artifact, 'absent');
  assert.equal(answer.kind, 'answer');
  assert.match(answer.text, new RegExp(`^${served.artifact.total_count} products? shipped in Aug 13–19, 2026; ${served.artifact.rows[0]!.title} shipped most, `));
  const empty = { ...served.artifact, outcome: 'empty' as const, rows: [] };
  assert.deepEqual(productsAndAdsAnswer(empty, 'absent'), { kind: 'fallback', text: 'absent' });
});

test('the page is axe-clean', async () => {
  const served = tab();
  const view = render(<ProductsAndAdsView artifact={served.artifact} productAds={served.product_ads} />);
  const axe = await import('axe-core');
  const results = await axe.default.run(view.container, { rules: { 'color-contrast': { enabled: false }, region: { enabled: false } } });
  assert.deepEqual(results.violations, []);
});
