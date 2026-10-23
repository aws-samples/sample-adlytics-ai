// SPDX-License-Identifier: MIT-0
/**
 * Products and ads (UI refresh 3.7, readout A.10): served Retail health rows beside each row's
 * served product-detail ad section for one chosen channel. Nothing is added, divided or ranked.
 */
import assert from 'node:assert/strict';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { test } from 'vitest';

import { ProductsAndAdsView, PRODUCTS_AND_ADS_LINES } from '../components/ProductsAndAdsView';
import { parsePresentationResultV2, retailHealthPresentationV2, retailProductPresentationV2, type RetailProductPresentation } from '../lib/presentation-v2';
import { loadProductAds, PRODUCT_DETAIL_CONCURRENCY, PRODUCTS_AND_ADS_COPY, productsAndAdsAnswer, type ProductAds } from '../lib/products-and-ads';
import parentResponse from './fixtures/retail-health-v2.json';
import childResponse from './fixtures/retail-product-v2.json';

const clone = <Value,>(value: Value): Value => JSON.parse(JSON.stringify(value)) as Value;
function parent() {
  const served = retailHealthPresentationV2(parsePresentationResultV2(clone(parentResponse), 'retail-health'));
  if (served.kind !== 'parent') throw new Error('expected a parent');
  return served;
}
const child = (): RetailProductPresentation => retailProductPresentationV2(parsePresentationResultV2(clone(childResponse), 'retail-health'));
const preset = { kind: 'preset', preset_id: '7d' } as const;

test('details load at most four at a time, and only a detail for this bake, window and product is used', async () => {
  const served = parent();
  let inFlight = 0;
  let peak = 0;
  const products = await loadProductAds(served, preset, new AbortController().signal, async () => {
    inFlight += 1;
    peak = Math.max(peak, inFlight);
    await new Promise((resolve) => { setTimeout(resolve, 5); });
    inFlight -= 1;
    return child();
  });
  assert.ok(peak <= PRODUCT_DETAIL_CONCURRENCY);
  const [first, second] = served.artifact.rows;
  // The fixture child is FICTA's: the FICTB row gets a detail for another product, so it's refused.
  assert.equal(products.get(first!.child_asin)?.kind, 'served');
  assert.equal(products.get(second!.child_asin)?.kind, 'rejected');
  // Another bake's detail is refused too.
  const stale = await loadProductAds(served, preset, new AbortController().signal, async () => ({ ...child(), parent_revision: 'B'.repeat(43) }));
  assert.equal(stale.get(first!.child_asin)?.kind, 'rejected');
  // A failed request reads as rejected for its row only.
  const failed = await loadProductAds(served, preset, new AbortController().signal, async () => { throw new Error('down'); });
  assert.deepEqual([...failed.values()].map((entry) => entry.kind), served.artifact.rows.map(() => 'rejected'));
});

test('each row shows its served sales beside the chosen channel credit; another channel switches only the ad columns', async () => {
  const user = userEvent.setup();
  const served = parent();
  const detail = child().artifact;
  const products = new Map<string, ProductAds>([
    [served.artifact.rows[0]!.child_asin, { detail, kind: 'served' }],
    [served.artifact.rows[1]!.child_asin, { kind: 'rejected' }],
  ]);
  const view = render(<ProductsAndAdsView artifact={served.artifact} products={products} />);
  const table = screen.getByRole('region', { name: new RegExp(PRODUCTS_AND_ADS_LINES.tableTitle) });
  const rows = within(table).getAllByRole('row').slice(1);
  assert.equal(rows.length, served.artifact.rows.length);
  const dsp = detail.ad_attributed!.rows[0]!;
  assert.match(rows[0]!.textContent ?? '', new RegExp(dsp.revenue.current.display_value!.replace('.', '\\.')));
  assert.match(rows[0]!.textContent ?? '', new RegExp(PRODUCTS_AND_ADS_COPY.notMeasured));
  // The refused detail shows its catalogued absence, not a figure.
  assert.match(rows[1]!.textContent ?? '', new RegExp(PRODUCTS_AND_ADS_COPY.detailUnavailable.replace(/[.']/g, '.')));
  await user.click(screen.getByRole('button', { name: 'Sponsored Products' }));
  const sp = detail.ad_attributed!.rows[1]!;
  assert.match(within(table).getAllByRole('row')[1]!.textContent ?? '', new RegExp(sp.revenue.current.display_value!.replace('.', '\\.')));
  // No product return on ad spend, and the two systems never add up.
  assert.ok(screen.getByText(PRODUCTS_AND_ADS_LINES.noProductReturn));
  assert.equal(/ROAS|×/.test(table.textContent ?? ''), false);
  view.unmount();
});

test('the answer names the served count and the first served row; an empty body reads its fallback', () => {
  const served = parent();
  const answer = productsAndAdsAnswer(served.artifact, 'absent');
  assert.equal(answer.kind, 'answer');
  assert.match(answer.text, new RegExp(`^${served.artifact.total_count} products? shipped in Aug 13–19, 2026; ${served.artifact.rows[0]!.title} shipped most, `));
  const empty = { ...served.artifact, outcome: 'empty' as const, rows: [] };
  assert.deepEqual(productsAndAdsAnswer(empty, 'absent'), { kind: 'fallback', text: 'absent' });
});

test('the page is axe-clean', async () => {
  const served = parent();
  const products = new Map<string, ProductAds>([[served.artifact.rows[0]!.child_asin, { detail: child().artifact, kind: 'served' }]]);
  const view = render(<ProductsAndAdsView artifact={served.artifact} products={products} />);
  const axe = await import('axe-core');
  const results = await axe.default.run(view.container, { rules: { 'color-contrast': { enabled: false }, region: { enabled: false } } });
  assert.deepEqual(results.violations, []);
});
