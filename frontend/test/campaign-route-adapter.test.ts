// SPDX-License-Identifier: MIT-0
/** Segment boundary prefetch: identity-keyed, single-use, TTL-bounded serving. */
import assert from 'node:assert/strict';
import { afterEach, test, vi } from 'vitest';

vi.mock('../lib/presentation-v2', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../lib/presentation-v2')>();
  return { ...actual, fetchCampaignPresentationV2: vi.fn() };
});

import { campaignRouteAdapter, prefetchCampaignSegment } from '../components/CampaignRouteAdapter';
import { fetchCampaignPresentationV2, type CampaignParentPresentation, type CampaignPortfolioSort } from '../lib/presentation-v2';

const fetchMock = vi.mocked(fetchCampaignPresentationV2);
const sort: CampaignPortfolioSort = { direction: 'desc', field: 'spend' };
const signal = new AbortController().signal;

afterEach(() => { vi.useRealTimers(); });

function armFetch(): void {
  fetchMock.mockReset();
  // Every call resolves a distinct object, so serving identity is observable.
  fetchMock.mockImplementation(() => Promise.resolve({} as CampaignParentPresentation));
}

test('loadParent serves a matching prefetched sibling exactly once, then fetches fresh', async () => {
  armFetch();
  // The first parent load records the request identity the prefetch inherits.
  await campaignRouteAdapter.loadParent('session-a', undefined, signal, { segment: 0, sort });
  assert.equal(fetchMock.mock.calls.length, 1);

  prefetchCampaignSegment(sort, 1);
  assert.equal(fetchMock.mock.calls.length, 2);
  const prefetched = await (fetchMock.mock.results[1]!.value as Promise<CampaignParentPresentation>);

  // The boundary crossing consumes the prefetch: same object, no new request.
  const served = await campaignRouteAdapter.loadParent('session-a', undefined, signal, { segment: 1, sort });
  assert.equal(served, prefetched);
  assert.equal(fetchMock.mock.calls.length, 2);

  // Single-use: a repeat of the same intent fetches fresh.
  const again = await campaignRouteAdapter.loadParent('session-a', undefined, signal, { segment: 1, sort });
  assert.notEqual(again, prefetched);
  assert.equal(fetchMock.mock.calls.length, 3);
});

test('a prefetch never serves across a different request identity', async () => {
  armFetch();
  await campaignRouteAdapter.loadParent('session-a', undefined, signal, { segment: 0, sort });
  prefetchCampaignSegment(sort, 1);
  const prefetched = await (fetchMock.mock.results[1]!.value as Promise<CampaignParentPresentation>);

  // A different session (or temporal selection) is a different identity: fresh fetch.
  const served = await campaignRouteAdapter.loadParent('session-b', undefined, signal, { segment: 1, sort });
  assert.notEqual(served, prefetched);
  assert.equal(fetchMock.mock.calls.length, 3);
});

test('a prefetch past its TTL is refused and the crossing fetches fresh', async () => {
  vi.useFakeTimers();
  armFetch();
  await campaignRouteAdapter.loadParent('session-a', undefined, signal, { segment: 0, sort });
  prefetchCampaignSegment(sort, 1);
  const prefetched = await (fetchMock.mock.results[1]!.value as Promise<CampaignParentPresentation>);

  vi.advanceTimersByTime(61_000);
  const served = await campaignRouteAdapter.loadParent('session-a', undefined, signal, { segment: 1, sort });
  assert.notEqual(served, prefetched);
  assert.equal(fetchMock.mock.calls.length, 3);
});

test('a failed prefetch is evicted and never poisons the crossing', async () => {
  armFetch();
  await campaignRouteAdapter.loadParent('session-a', undefined, signal, { segment: 0, sort });

  fetchMock.mockRejectedValueOnce(new Error('fictional network failure'));
  prefetchCampaignSegment(sort, 1);
  // Let the rejection settle so the eviction handler runs.
  await new Promise((resolve) => { setTimeout(resolve, 0); });

  const served = await campaignRouteAdapter.loadParent('session-a', undefined, signal, { segment: 1, sort });
  assert.ok(served);
  assert.equal(fetchMock.mock.calls.length, 3);
});

test('a crossing that consumes a not-yet-settled failing prefetch falls back to a fresh fetch', async () => {
  armFetch();
  await campaignRouteAdapter.loadParent('session-a', undefined, signal, { segment: 0, sort });

  // The prefetch will fail, but the crossing consumes it before the rejection settles.
  let reject: ((reason: Error) => void) | undefined;
  fetchMock.mockImplementationOnce(() => new Promise((_resolve, rejectPromise) => { reject = rejectPromise; }));
  prefetchCampaignSegment(sort, 1);
  const crossing = campaignRouteAdapter.loadParent('session-a', undefined, signal, { segment: 1, sort });
  reject!(new Error('fictional network failure'));

  // Consuming a prefetch is never worse than not having one: the crossing resolves.
  const served = await crossing;
  assert.ok(served);
  assert.equal(fetchMock.mock.calls.length, 3);
});
