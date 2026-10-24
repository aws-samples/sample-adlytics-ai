// SPDX-License-Identifier: MIT-0
/**
 * Campaigns "Highest return" and "Lowest return" (UI refresh 3.2, readout A.4, D10): the first
 * served rows of the server's own Attributed ROAS orderings. The browser never sorts, and when
 * either ordering isn't served for the same window and option, both lists are omitted.
 */
import assert from 'node:assert/strict';
import { render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, test, vi } from 'vitest';
vi.mock('../lib/presentation-v2', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../lib/presentation-v2')>();
  return { ...actual, fetchCampaignPresentationV2: vi.fn() };
});
import { CampaignPortfolioV2View, DELIVERY_SUMMARY, RETURN_LISTS_COPY } from '../components/CampaignPortfolioV2View';
import { campaignPresentationV2, fetchCampaignPresentationV2, parsePresentationResultV2, type CampaignParentPresentation, type CampaignPortfolioSort } from '../lib/presentation-v2';
import everything from './fixtures/campaigns-all-v2.json';

const fetchMock = vi.mocked(fetchCampaignPresentationV2);
function served(): Extract<CampaignParentPresentation, { readonly kind: 'parent' }> {
  const presentation = campaignPresentationV2(parsePresentationResultV2(JSON.parse(JSON.stringify(everything)), 'campaigns')) as CampaignParentPresentation;
  if (presentation.kind !== 'parent') throw new Error('expected a parent');
  return presentation;
}
/** The served body as the server would answer one ordering: its rows in reverse for "asc". */
function ordered(sort: CampaignPortfolioSort): CampaignParentPresentation {
  const base = served();
  const rows = sort.direction === 'asc' ? [...base.artifact.rows].reverse() : base.artifact.rows;
  return { ...base, artifact: { ...base.artifact, rows, sort } };
}
beforeEach(() => { fetchMock.mockReset(); });
afterEach(() => { vi.restoreAllMocks(); });

function view(sessionId = 'session-a') {
  const parent = served();
  return render(<CampaignPortfolioV2View
    child={null}
    focusSessionId={sessionId}
    onClear={() => undefined}
    onSelect={() => undefined}
    parent={parent.artifact}
    selection={{ choice: null, kind: 'idle' }}
  />);
}

test('the lists show the first five served rows of each server ordering, in served order', async () => {
  fetchMock.mockImplementation(async (_session, _selection, _signal, sort) => ordered(sort!));
  view();
  const high = await screen.findByRole('region', { name: RETURN_LISTS_COPY.high });
  const low = screen.getByRole('region', { name: RETURN_LISTS_COPY.low });
  // Two requests, the server's own orderings, segment 0, the same channel option.
  assert.deepEqual(fetchMock.mock.calls.map((call) => [call[3], call[4], call[5]]), [
    [{ direction: 'desc', field: 'attributed_roas' }, 0, undefined],
    [{ direction: 'asc', field: 'attributed_roas' }, 0, undefined],
  ]);
  const names = (region: HTMLElement) => within(region).getAllByRole('listitem').map((item) => item.querySelector('.name')?.firstChild?.textContent);
  const rows = served().artifact.rows.filter((row) => row.comparisons.some((entry) => entry.metric === 'attributed_roas' && entry.current.value !== null));
  assert.equal(names(high).length, Math.min(5, rows.length));
  assert.equal(names(high)[0], rows[0]?.signature.effective_name ?? names(high)[0]);
  assert.equal(names(low)[0], [...rows].reverse()[0]?.signature.effective_name ?? names(low)[0]);
});

test('an ordering that answers for another sort, or fails, omits both lists', async () => {
  fetchMock.mockImplementation(async () => served());
  const first = view();
  await waitFor(() => { assert.equal(fetchMock.mock.calls.length, 2); });
  await new Promise((resolve) => { setTimeout(resolve, 0); });
  assert.equal(screen.queryByRole('region', { name: RETURN_LISTS_COPY.high }), null);
  first.unmount();
  fetchMock.mockReset();
  fetchMock.mockImplementation(async () => { throw new Error('down'); });
  view();
  await waitFor(() => { assert.equal(fetchMock.mock.calls.length, 2); });
  await new Promise((resolve) => { setTimeout(resolve, 0); });
  assert.equal(screen.queryByRole('region', { name: RETURN_LISTS_COPY.low }), null);
});

test('without a session nothing is requested, and delivery per channel folds below the table', () => {
  const { container } = view('');
  assert.equal(fetchMock.mock.calls.length, 0);
  const delivery = container.querySelector('[data-slot="cp-delivery"]') as HTMLDetailsElement | null;
  if (served().artifact.channel_view !== null) {
    assert.equal(delivery?.open, false);
    assert.equal(delivery?.querySelector('summary')?.textContent, DELIVERY_SUMMARY);
  }
});
