// SPDX-License-Identifier: MIT-0
// The Campaigns channel fallback (ADR-127, ia §8.1): only the server's "filter not offered"
// refusal shows All channels, the note names its cause, and the refused channel is dropped.
import assert from 'node:assert/strict';

import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, test, vi } from 'vitest';

import { PresentationRequestError } from '@/lib/presentation';
import { parentReady } from '@/lib/presentation-route-state';
import { campaignPresentationV2, parsePresentationResultV2, type CampaignParentPresentation } from '@/lib/presentation-v2';
import everything from './fixtures/campaigns-all-v2.json';
import dsp from './fixtures/campaigns-dsp-v2.json';

const fetchParent = vi.hoisted(() => vi.fn());
vi.mock('@/lib/presentation-v2', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/presentation-v2')>()),
  fetchCampaignPresentationV2: fetchParent,
}));

const { FILTER_NOT_OFFERED, campaignRouteAdapter, campaignUrlState } = await import('@/components/CampaignRouteAdapter');

function decoded(raw: unknown): CampaignParentPresentation {
  return campaignPresentationV2(parsePresentationResultV2(raw, 'campaigns')) as CampaignParentPresentation;
}
const all = decoded(everything);
const amazonDsp = decoded(dsp);
const signal = new AbortController().signal;

function notOffered(): PresentationRequestError {
  return new PresentationRequestError('validation', FILTER_NOT_OFFERED);
}

function show(parent: CampaignParentPresentation) {
  return render(<>{campaignRouteAdapter.render(parentReady(parent), () => undefined, () => undefined, () => undefined, 'conversation-session')}</>);
}

beforeEach(() => fetchParent.mockReset());
afterEach(() => cleanup());

test('A preset change to a window without the chosen channel shows All channels and says the window is why', async () => {
  fetchParent.mockResolvedValueOnce(amazonDsp);
  assert.equal(await campaignRouteAdapter.loadParent('s', undefined, signal, { filter: 'amazon_dsp' }), amazonDsp);
  fetchParent.mockRejectedValueOnce(notOffered()).mockResolvedValueOnce(all);
  const served = await campaignRouteAdapter.loadParent('s', { kind: 'preset', preset_id: '14d' }, signal, { filter: 'amazon_dsp' });
  assert.equal(served, all);
  // The fallback asks for All channels: no filter on the second request.
  assert.equal(fetchParent.mock.calls[2]?.[5], undefined);
  show(served);
  assert.ok(screen.getByText("All channels is shown. The channel you chose isn't offered for this window."));
  assert.equal(screen.queryByText(/The channel in the link/), null);
  // The intent drops the channel, so the URL loses it and the next preset change doesn't send it.
  const intent = campaignUrlState.served?.(served, { filter: 'amazon_dsp' });
  assert.deepEqual(intent, {});
  assert.equal(campaignUrlState.write({ kind: 'preset', preset_id: '14d' }, intent ?? {}).toString(), 'preset=14d');
});

test('A channel in the link the tab does not offer shows the link note', async () => {
  fetchParent.mockRejectedValueOnce(notOffered()).mockResolvedValueOnce(all);
  const served = await campaignRouteAdapter.loadParent('s', undefined, signal, { filter: 'amazon_sponsored_television' });
  show(served);
  assert.ok(screen.getByText("All channels is shown. The channel in the link isn't offered on this tab."));
  assert.deepEqual(campaignUrlState.served?.(served, { filter: 'amazon_sponsored_television' }), {});
  // A served channel keeps its intent object.
  const kept = { filter: 'amazon_dsp' };
  assert.equal(campaignUrlState.served?.(amazonDsp, kept), kept);
});

test('On a later visit, a link to the channel last chosen on screen reads as a link, not a window change', async () => {
  // One visit: Amazon DSP chosen on screen and served.
  fetchParent.mockResolvedValueOnce(amazonDsp);
  await campaignRouteAdapter.loadParent('s', undefined, signal, { filter: 'amazon_dsp' });
  // The tab is left and opened again from a link naming that channel, which isn't offered.
  campaignRouteAdapter.onMount?.();
  fetchParent.mockRejectedValueOnce(notOffered()).mockResolvedValueOnce(all);
  const served = await campaignRouteAdapter.loadParent('s', undefined, signal, { filter: 'amazon_dsp' });
  show(served);
  assert.ok(screen.getByText("All channels is shown. The channel in the link isn't offered on this tab."));
  assert.equal(screen.queryByText(/isn't offered for this window/), null);
});

test('Any other refusal of a filtered request stays an error and never falls back', async () => {
  const segment = new PresentationRequestError('validation', 'ADLY-PRESENTATION-0001');
  fetchParent.mockRejectedValueOnce(segment);
  await assert.rejects(
    campaignRouteAdapter.loadParent('s', undefined, signal, { filter: 'amazon_dsp', segment: 40 }),
    (error) => error === segment,
  );
  assert.equal(fetchParent.mock.calls.length, 1);
});
