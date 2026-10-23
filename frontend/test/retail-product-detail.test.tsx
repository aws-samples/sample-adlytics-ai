// SPDX-License-Identifier: MIT-0
/**
 * Retail health › product detail (ADR-125) over the server's own responses: the decoder, the
 * detail view (per-channel ads with no total, Back), and the route adapter (a row opens detail
 * in place, Back closes it and returns focus to the row, a child from another bake is refused).
 * The fixtures are written by the presentation handler; a Python unit test keeps them current.
 */
import assert from 'node:assert/strict';
import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, test, vi } from 'vitest';

vi.mock('../lib/presentation-v2', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../lib/presentation-v2')>();
  return { ...actual, fetchRetailHealthPresentationV2: vi.fn(), fetchRetailHealthProductV2: vi.fn() };
});

import { PAGE_HEADING_ID } from '../components/Dialog';
import { VisibleTabsContext } from '../components/NextStepContext';
import { PresentationV2RouteController } from '../components/PresentationV2RouteController';
import { retailHealthRouteAdapter } from '../components/RetailHealthRouteAdapter';
import { RetailProductDetailView } from '../components/RetailProductDetailView';
import {
  fetchRetailHealthPresentationV2,
  fetchRetailHealthProductV2,
  parsePresentationResultV2,
  retailHealthPresentationV2,
  retailProductPresentationV2,
  type RetailProductPresentation,
} from '../lib/presentation-v2';
import { parseRetailProductDetail } from '../lib/retail-health-product';
import { visibleHubs, type ManifestTab } from '../lib/route-manifest';
import parentResponse from './fixtures/retail-health-v2.json';
import childResponse from './fixtures/retail-product-v2.json';

const SESSION = parentResponse.session_id;
const parentFetch = vi.mocked(fetchRetailHealthPresentationV2);
const childFetch = vi.mocked(fetchRetailHealthProductV2);
const clone = <Value,>(value: Value): Value => JSON.parse(JSON.stringify(value)) as Value;
const served = () => retailHealthPresentationV2(parsePresentationResultV2(clone(parentResponse), 'retail-health'));
const child = (): RetailProductPresentation => retailProductPresentationV2(parsePresentationResultV2(clone(childResponse), 'retail-health'));

beforeEach(() => {
  parentFetch.mockReset();
  childFetch.mockReset();
  parentFetch.mockImplementation(async () => served());
  childFetch.mockImplementation(async () => child());
});

afterEach(() => { vi.restoreAllMocks(); });

test('the browser decodes the served child and refuses one that totals its ad channels', () => {
  const detail = child();
  assert.equal(detail.parent_revision, parentResponse.page.artifact_revision);
  assert.equal(detail.artifact.product.title, 'Fictional Product FICTA');
  assert.equal(detail.artifact.totals.length, 14);
  assert.deepEqual(detail.artifact.ad_attributed?.rows.map((row) => row.label), ['Amazon DSP', 'Sponsored Products']);
  const artifact = clone(childResponse.page.artifact) as Record<string, unknown>;
  assert.doesNotThrow(() => parseRetailProductDetail(clone(artifact)));
  // A total row, an unknown key, a dropped tile or a series off the parent's window is malformed.
  const ads = artifact.ad_attributed as { rows: Record<string, unknown>[] };
  assert.throws(() => parseRetailProductDetail({ ...artifact, ad_attributed: { ...ads, total: ads.rows[0] } }), TypeError);
  assert.throws(() => parseRetailProductDetail({ ...artifact, totals: (artifact.totals as unknown[]).slice(1) }), TypeError);
  assert.throws(() => parseRetailProductDetail({ ...artifact, sales_daily: (artifact.sales_daily as unknown[]).slice(1) }), TypeError);
  assert.throws(() => parseRetailProductDetail({ ...artifact, product_ref: 'a'.repeat(32) }), TypeError);
  // A child on another window than its temporal control is refused before it renders.
  const moved = clone(childResponse);
  moved.temporal.resolved.window = { end: '2026-08-20', start: '2026-08-14' };
  assert.throws(() => retailProductPresentationV2(parsePresentationResultV2(moved, 'retail-health')));
});

test('detail shows the shared tile groups and per-channel ad rows with no total row', async () => {
  const user = userEvent.setup();
  const back = vi.fn();
  const detail = child();
  render(<RetailProductDetailView
    artifact={detail.artifact}
    neighbours={{ index: 0, next: () => undefined, previous: null, total: 2 }}
    onBack={back}
    selectionReference={parentResponse.page.artifact.rows[0]!.selection_reference}
    temporalSelection={{ kind: 'preset', preset_id: '7d' }}
  />);
  const headings = screen.getAllByRole('heading', { level: 2 }).map((heading) => heading.textContent);
  assert.deepEqual(headings.slice(0, 4), ['Demand and conversion', 'Sales and fulfilment', 'Traffic value and margin', 'Returns']);
  const table = screen.getByRole('table', { name: /Sales Amazon credits to ads for this product, per channel/ });
  const rows = within(table).getAllByRole('row').slice(1);
  assert.deepEqual(rows.map((row) => within(row).getByRole('rowheader').textContent), ['Amazon DSP', 'Sponsored Products']);
  assert.equal(within(table).queryByText(/total/i), null);
  assert.ok(screen.getByText(/aren.t added across channels/));
  assert.ok(screen.getByText('1 of 2 products'));
  assert.equal((screen.getByRole('button', { name: 'Previous product' }) as HTMLButtonElement).disabled, true);
  // Each daily chart is zero-based and carries its exact served values (ADR-118 decision 7).
  for (const [name, points, unit] of [
    ['Shipped revenue by day', detail.artifact.sales_daily, 'currency'],
    ['Glance views by day', detail.artifact.traffic_daily, 'count'],
  ] as const) {
    const panel = screen.getByRole('heading', { level: 3, name: new RegExp(`^${name}`) }).closest('.product-daily') as HTMLElement;
    const values = within(panel).getByRole('table', { name: new RegExp(`^${name}.*exact values$`) });
    assert.equal(within(panel).getByText('Show values').tagName, 'SUMMARY');
    assert.equal(within(values).getAllByRole('row').length, points.length + 1);
    assert.equal(within(values).queryByText(/total/i), null);
    assert.ok(within(panel).getByText(`Axis starts at 0, because this is a ${unit}.`));
    const axis = panel.querySelectorAll('.y-axis .axis-label');
    assert.match(axis[axis.length - 1]?.textContent ?? '', /^0( USD)?$/);
  }
  // The product named by the movers card shows that card with its own stats' provenance.
  assert.ok(screen.getByRole('region', { name: 'Insights that name this product' }).querySelector('.narr-card'));
  await user.click(screen.getByRole('button', { name: 'Back to Retail health' }));
  assert.equal(back.mock.calls.length, 1);
});

const repeatPurchaseTab = (tabId: string): ManifestTab => ({ description: 'Repeat purchases per product', entitlement: null, kind: 'page', label: 'Repeat purchase by product', order: 1, question: null, route: '/repeat-purchase/', tab_id: tabId });
test('the Repeat purchase pointer renders from the manifest tab repeat-purchase-by-product, and the old tab id renders none', () => {
  const view = (tabId: string) => <VisibleTabsContext.Provider value={[repeatPurchaseTab(tabId)]}>
    <RetailProductDetailView
      artifact={child().artifact}
      neighbours={{ index: 0, next: () => undefined, previous: null, total: 2 }}
      onBack={() => undefined}
      selectionReference={parentResponse.page.artifact.rows[0]!.selection_reference}
      temporalSelection={{ kind: 'preset', preset_id: '7d' }}
    />
  </VisibleTabsContext.Provider>;
  const shown = render(view('repeat-purchase-by-product'));
  assert.equal(screen.getByRole('link', { name: 'Repeat purchase by product' }).getAttribute('href'), '/repeat-purchase/');
  shown.unmount();
  // Paired control: a manifest listing only the retired id leaves the pointer unrendered.
  render(view('repeat-purchase'));
  assert.equal(screen.queryByRole('link', { name: 'Repeat purchase by product' }), null);
});
test('the Gateway products pointer renders now that the committed manifest lists New customers (N30)', () => {
  const committed = visibleHubs({}).flatMap((hub) => hub.tabs);
  const view = (tabs: readonly ManifestTab[]) => <VisibleTabsContext.Provider value={tabs}>
    <RetailProductDetailView
      artifact={child().artifact}
      neighbours={{ index: 0, next: () => undefined, previous: null, total: 2 }}
      onBack={() => undefined}
      selectionReference={parentResponse.page.artifact.rows[0]!.selection_reference}
      temporalSelection={{ kind: 'preset', preset_id: '7d' }}
    />
  </VisibleTabsContext.Provider>;
  const shown = render(view(committed));
  const link = screen.getByRole('link', { name: 'Customers › New customers › Gateway products' });
  assert.equal(link.getAttribute('href'), '/new-customers/');
  // The link names the page's own hub, tab and section headings.
  assert.equal(committed.find((tab) => tab.tab_id === 'new-customers')?.label, 'New customers');
  shown.unmount();
  // Paired control: the same manifest without the New customers tab renders no pointer.
  render(view(committed.filter((tab) => tab.tab_id !== 'new-customers')));
  assert.equal(screen.queryByRole('link', { name: 'Customers › New customers › Gateway products' }), null);
});
test('a product without ad purchases says why, in the server words, instead of a table', () => {
  const artifact = clone(childResponse.page.artifact) as Record<string, unknown>;
  const reason = "Amazon Marketing Cloud hasn't sent purchases for this product for Aug 13\u201319, 2026.";
  const absent = parseRetailProductDetail({
    ...artifact,
    ad_attributed: { ...(artifact.ad_attributed as object), reason, reason_code: 'missing_current_publication', rows: [] },
  });
  render(<RetailProductDetailView artifact={absent} neighbours={null} onBack={() => undefined} selectionReference="v2_AAAA" temporalSelection={{ kind: 'preset', preset_id: '7d' }} />);
  assert.equal(screen.queryByRole('table', { name: /credits to ads/ }), null);
  assert.ok(screen.getByText(reason));
});

function Page() {
  return <>
    <h1 id={PAGE_HEADING_ID} tabIndex={-1}>Retail health</h1>
    <PresentationV2RouteController adapter={retailHealthRouteAdapter} sessionId={SESSION} />
  </>;
}

test('a row opens its product in place, Back closes it, and focus returns to that row', async () => {
  const back = vi.spyOn(window.history, 'back').mockImplementation(() => {
    window.dispatchEvent(new PopStateEvent('popstate'));
  });
  const user = userEvent.setup();
  render(<Page />);
  const table = await screen.findByRole('table', { name: /Shipped revenue per product/ });
  await user.click(within(table).getByRole('button', { name: /Fictional Product FICTA/ }));
  await screen.findByRole('button', { name: 'Back to Retail health' });
  // The row's own capability and the parent's preset are all the request carries.
  assert.deepEqual(childFetch.mock.calls[0]?.slice(0, 3), [SESSION, parentResponse.page.artifact.rows[0]!.selection_reference, { kind: 'preset', preset_id: '7d' }]);
  await waitFor(() => assert.equal(document.activeElement?.id, PAGE_HEADING_ID));
  await user.click(screen.getByRole('button', { name: 'Back to Retail health' }));
  const list = await screen.findByRole('table', { name: /Shipped revenue per product/ });
  const row = within(list).getByRole('button', { name: /Fictional Product FICTA/ });
  await waitFor(() => assert.ok(document.activeElement === row, 'focus is not on the product row'));
  assert.equal(back.mock.calls.length, 1);
});

test('an openable row reads as a link, and a loading product names itself', async () => {
  let release: (value: RetailProductPresentation) => void = () => undefined;
  childFetch.mockImplementation(() => new Promise((resolve) => { release = resolve; }));
  const user = userEvent.setup();
  render(<Page />);
  const table = await screen.findByRole('table', { name: /Shipped revenue per product/ });
  const button = within(table).getByRole('button', { name: /Fictional Product FICTA/ });
  assert.equal(button.querySelector('.link-text')?.textContent, 'Fictional Product FICTA');
  await user.click(button);
  assert.ok(await screen.findByRole('status', { name: 'Loading Fictional Product FICTA' }));
  assert.equal(screen.queryByText(/Loading retail health/), null);
  await act(async () => { release(child()); });
  await screen.findByRole('button', { name: 'Back to Retail health' });
});

test('the browser\'s Back closes detail from the h1 and returns focus to the row', async () => {
  const user = userEvent.setup();
  render(<Page />);
  const table = await screen.findByRole('table', { name: /Shipped revenue per product/ });
  await user.click(within(table).getByRole('button', { name: /Fictional Product FICTA/ }));
  await screen.findByRole('button', { name: 'Back to Retail health' });
  await waitFor(() => assert.equal(document.activeElement?.id, PAGE_HEADING_ID));
  await act(async () => { window.dispatchEvent(new PopStateEvent('popstate')); });
  const list = await screen.findByRole('table', { name: /Shipped revenue per product/ });
  const row = within(list).getByRole('button', { name: /Fictional Product FICTA/ });
  await waitFor(() => assert.ok(document.activeElement === row, 'focus is not on the product row'));
});

test('the movers card opens the product it names, through that row\'s capability', async () => {
  vi.spyOn(window.history, 'back').mockImplementation(() => undefined);
  const user = userEvent.setup();
  render(<Page />);
  await user.click(await screen.findByRole('button', { name: 'Open Fictional Product FICTA' }));
  await screen.findByRole('button', { name: 'Back to Retail health' });
  assert.equal(childFetch.mock.calls[0]?.[1], parentResponse.page.artifact.rows[0]!.selection_reference);
});

test('a child from another bake is refused, and the unavailable state keeps Back to Retail health', async () => {
  vi.spyOn(window.history, 'back').mockImplementation(() => {
    window.dispatchEvent(new PopStateEvent('popstate'));
  });
  childFetch.mockImplementation(async () => ({ ...child(), parent_revision: 'v2_' + 'Z'.repeat(40) }));
  const user = userEvent.setup();
  render(<Page />);
  const table = await screen.findByRole('table', { name: /Shipped revenue per product/ });
  await user.click(within(table).getByRole('button', { name: /Fictional Product FICTB/ }));
  assert.ok(await screen.findByText("This product's detail can't be shown right now."));
  assert.equal(screen.queryByRole('table', { name: /credits to ads/ }), null);
  await act(async () => { await user.click(screen.getByRole('button', { name: 'Back to Retail health' })); });
  const row = await screen.findByRole('button', { name: /Fictional Product FICTB/ });
  await waitFor(() => assert.ok(document.activeElement === row, 'focus is not on the product row'));
});
