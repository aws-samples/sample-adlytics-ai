// SPDX-License-Identifier: MIT-0
/**
 * Campaigns "Highest return" and "Lowest return" (UI refresh 3.2, render format 14): the lists
 * the server bakes into the parent body, rendered in served order. A page load makes exactly one
 * Campaigns request, so the lists cost no second round trip and no ROAS-sorted serve.
 */
import assert from 'node:assert/strict';
import { cleanup, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, test, vi } from 'vitest';

import { parentReady } from '@/lib/presentation-route-state';
import { campaignPresentationV2, parsePresentationResultV2, type CampaignParentPresentation } from '@/lib/presentation-v2';
import everything from './fixtures/campaigns-all-v2.json';

const fetchParent = vi.hoisted(() => vi.fn());
vi.mock('@/lib/presentation-v2', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/presentation-v2')>()),
  fetchCampaignPresentationV2: fetchParent,
}));

const { campaignRouteAdapter } = await import('@/components/CampaignRouteAdapter');
const { CampaignPortfolioV2View, DELIVERY_SUMMARY, RETURN_LISTS_COPY } = await import('@/components/CampaignPortfolioV2View');

function served(raw: unknown = everything): Extract<CampaignParentPresentation, { readonly kind: 'parent' }> {
  const presentation = campaignPresentationV2(parsePresentationResultV2(JSON.parse(JSON.stringify(raw)), 'campaigns')) as CampaignParentPresentation;
  if (presentation.kind !== 'parent') throw new Error('expected a parent');
  return presentation;
}
const signal = new AbortController().signal;
beforeEach(() => fetchParent.mockReset());
afterEach(() => cleanup());

function names(region: HTMLElement): (string | null | undefined)[] {
  return within(region).getAllByRole('listitem').map((item) => item.querySelector('.name')?.firstChild?.textContent);
}

test('a page load makes one Campaigns request and shows both served lists in served order', async () => {
  const parent = served();
  fetchParent.mockResolvedValue(parent);
  const loaded = await campaignRouteAdapter.loadParent('conversation-session', undefined, signal, {});
  render(<>{campaignRouteAdapter.render(parentReady(loaded), () => undefined, () => undefined, () => undefined, 'conversation-session')}</>);
  const high = await screen.findByRole('region', { name: RETURN_LISTS_COPY.high });
  const low = screen.getByRole('region', { name: RETURN_LISTS_COPY.low });
  await new Promise((resolve) => { setTimeout(resolve, 0); });
  // One request, the canonical parent: no sort, no segment, no filter.
  assert.equal(fetchParent.mock.calls.length, 1);
  assert.deepEqual(fetchParent.mock.calls[0]?.slice(3), [undefined, undefined, undefined]);
  // The server's orderings: ROAS 3, 3, 2.5, 2 highest first; a tie keeps the server's order.
  assert.deepEqual(names(high), ['Fictional DSP Prospecting', 'Fictional SP Exact', 'Fictional DSP Retargeting', 'Fictional SB Launch']);
  assert.deepEqual(names(low), ['Fictional SB Launch', 'Fictional DSP Retargeting', 'Fictional DSP Prospecting', 'Fictional SP Exact']);
  // Each entry shows its served ROAS and spend.
  const cells = [...within(high).getAllByRole('listitem')[0]!.querySelectorAll('.num')].map((cell) => cell.textContent);
  assert.deepEqual(cells, ['3.00×', '700.00 USD']);
});

test('a body whose lists are empty renders no lists and requests nothing', () => {
  const parent = served();
  const empty = { ...parent.artifact, return_lists: { highest: [], lowest: [] } };
  const { container } = render(<CampaignPortfolioV2View child={null} focusSessionId="session-a" onClear={() => undefined} onSelect={() => undefined} parent={empty} selection={{ choice: null, kind: 'idle' }} />);
  assert.equal(screen.queryByRole('region', { name: RETURN_LISTS_COPY.high }), null);
  assert.equal(fetchParent.mock.calls.length, 0);
  const delivery = container.querySelector('[data-slot="cp-delivery"]') as HTMLDetailsElement | null;
  if (parent.artifact.channel_view !== null) {
    assert.equal(delivery?.open, false);
    assert.equal(delivery?.querySelector('summary')?.textContent, DELIVERY_SUMMARY);
  }
});

test('a complete body without return lists, or with unequal lists, is refused by the decoder', () => {
  const missing = JSON.parse(JSON.stringify(everything)) as { page: { artifact: Record<string, unknown> } };
  delete missing.page.artifact.return_lists;
  assert.throws(() => served(missing));
  const unequal = JSON.parse(JSON.stringify(everything)) as { page: { artifact: { return_lists: { lowest: unknown[] } } } };
  unequal.page.artifact.return_lists.lowest.pop();
  assert.throws(() => served(unequal));
  // Positive control: the served fixture decodes.
  assert.ok(served().artifact.return_lists !== null);
});
