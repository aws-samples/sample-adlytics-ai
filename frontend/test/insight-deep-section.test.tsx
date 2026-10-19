// SPDX-License-Identifier: MIT-0
/** ADR-076 deep section progressive fill, bounded polling, and repeat-open cache behavior. */

import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';

import { InsightDeepSection, type InsightDeepSurface } from '@/components/InsightDeepSection';
import type { InsightDeepDiveV1 } from '@/lib/insight-deep-dive';

const period = { start: '2026-09-01', end: '2026-09-07' } as const;

function filled(version = 'v9'): InsightDeepDiveV1 {
  return {
    absence_reason: null,
    artifact_revision: 'revision-fictional',
    deep_prompt_version: version,
    period,
    slot_id: 'contribution',
    state: 'filled',
    deep: {
      what_stands_out: 'The fictional movement stands out.',
      candidate_readings: ['Reading one: the supplied movement may be temporary.'],
      discriminating_evidence: 'The next delivered window would distinguish the reading.',
      cited_figures: ['5'],
    },
  };
}

function surface(key: string, fetch: InsightDeepSurface['fetch']): InsightDeepSurface {
  return { cacheKey: key, expectedArtifactRevision: 'revision-fictional', fetch, window: period };
}

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

test('filled deep analysis renders its structured cells in customer words only', async () => {
  render(<InsightDeepSection surface={surface('filled-identity', () => Promise.resolve(filled()))} />);

  expect(await screen.findByText('The fictional movement stands out.')).toBeTruthy();
  expect(screen.getByText('What stands out')).toBeTruthy();
  expect(screen.getByText('Reading one')).toBeTruthy();
  expect(screen.getByText(/What would tell these apart/)).toBeTruthy();
  // Customer-facing only: no citations, no prompt versions, no cache vocabulary
  // (design revision 2026-09-25.1) — grounding verification stays server-side.
  expect(screen.queryByText(/Cited figures/)).toBeNull();
  expect(screen.queryByText(/v9|prompt|cached/i)).toBeNull();
});

test('the maximum three readings render as four labeled cells', async () => {
  const maxed: InsightDeepDiveV1 = {
    ...filled(),
    deep: {
      ...filled().deep!,
      candidate_readings: [
        'Reading one: the supplied movement may be temporary.',
        'Reading two: the movement may reflect a planned shift.',
        'Reading three: the movement may be broad-based.',
      ],
    },
  };
  render(<InsightDeepSection surface={surface('maxed-identity', () => Promise.resolve(maxed))} />);

  expect(await screen.findByText('What stands out')).toBeTruthy();
  expect(screen.getByText('Reading one')).toBeTruthy();
  expect(screen.getByText('Reading two')).toBeTruthy();
  expect(screen.getByText('Reading three')).toBeTruthy();
  expect(document.querySelectorAll('.deep-cell')).toHaveLength(4);
});

test('a completed identity is retained across dialog unmount without another fetch', async () => {
  const fetch = vi.fn(() => Promise.resolve(filled('v10')));
  const first = render(<InsightDeepSection surface={surface('repeat-identity', fetch)} />);
  await screen.findByText('The fictional movement stands out.');
  first.unmount();

  render(<InsightDeepSection surface={surface('repeat-identity', fetch)} />);

  expect(screen.getByText('The fictional movement stands out.')).toBeTruthy();
  expect(fetch).toHaveBeenCalledTimes(1);
});

test('a pending section polls on the declared schedule and then fills', async () => {
  vi.useFakeTimers();
  const pending: InsightDeepDiveV1 = {
    ...filled(),
    state: 'pending',
    deep: null,
    absence_reason: 'The deep analysis is still being prepared.',
  };
  const fetch = vi.fn()
    .mockResolvedValueOnce(pending)
    .mockResolvedValueOnce(filled('v11'));
  render(<InsightDeepSection surface={surface('pending-identity', fetch)} />);
  await Promise.resolve();
  expect(screen.getByText('Gathering the delivered evidence for this view…')).toBeTruthy();

  await act(async () => {
    await vi.advanceTimersByTimeAsync(5_000);
    await Promise.resolve();
  });
  expect(screen.getByText('The fictional movement stands out.')).toBeTruthy();
  expect(fetch).toHaveBeenCalledTimes(2);
});


test('a same-window response for another artifact revision is refused before caching', async () => {
  const mismatched = { ...filled(), artifact_revision: 'newer-revision' };
  render(<InsightDeepSection surface={surface('revision-mismatch', () => Promise.resolve(mismatched))} />);

  expect(await screen.findByText(/another page revision or reporting window/)).toBeTruthy();
  expect(screen.queryByText('The fictional movement stands out.')).toBeNull();
});

test('pending polling stops after the declared six re-fetches', async () => {
  vi.useFakeTimers();
  const pending: InsightDeepDiveV1 = {
    ...filled(), state: 'pending', deep: null, absence_reason: 'Still preparing.',
  };
  const fetch = vi.fn(() => Promise.resolve(pending));
  render(<InsightDeepSection surface={surface('bounded-pending', fetch)} />);
  await Promise.resolve();

  await act(async () => {
    await vi.advanceTimersByTimeAsync(30_000);
    await Promise.resolve();
  });

  expect(fetch).toHaveBeenCalledTimes(7);
  expect(screen.getByText('Still preparing.')).toBeTruthy();
});

test('closing the dialog stops a pending re-fetch cycle', async () => {
  vi.useFakeTimers();
  const pending: InsightDeepDiveV1 = {
    ...filled(), state: 'pending', deep: null, absence_reason: 'Still preparing.',
  };
  const fetch = vi.fn(() => Promise.resolve(pending));
  const rendered = render(<InsightDeepSection surface={surface('closed-pending', fetch)} />);
  await Promise.resolve();
  rendered.unmount();

  await act(async () => {
    await vi.advanceTimersByTimeAsync(30_000);
  });

  expect(fetch).toHaveBeenCalledTimes(1);
});

test('a parent re-render mid-poll does not reset the six re-fetch budget', async () => {
  vi.useFakeTimers();
  const pending: InsightDeepDiveV1 = {
    ...filled(), state: 'pending', deep: null, absence_reason: 'Still preparing.',
  };
  const fetch = vi.fn(() => Promise.resolve(pending));
  const rendered = render(<InsightDeepSection surface={surface('rerender-pending', fetch)} />);
  await Promise.resolve();

  await act(async () => {
    await vi.advanceTimersByTimeAsync(10_000);
    await Promise.resolve();
  });
  // The owning page re-renders and rebuilds the surface object for the SAME
  // identity: the effect restarts, but the poll budget must carry over.
  rendered.rerender(<InsightDeepSection surface={surface('rerender-pending', fetch)} />);

  await act(async () => {
    await vi.advanceTimersByTimeAsync(60_000);
    await Promise.resolve();
  });

  // The restart fetch replaces the poll whose timer the cleanup cancelled, so the
  // identity's total stays at the same 7 the uninterrupted bound allows: a reset
  // budget would have reached 10 before exhausting.
  expect(fetch).toHaveBeenCalledTimes(7);
  expect(screen.getByText('Still preparing.')).toBeTruthy();
});

test('the preparing caption advances through its stages and holds on the last', async () => {
  vi.useFakeTimers();
  const never = vi.fn(() => new Promise<InsightDeepDiveV1>(() => undefined));
  render(<InsightDeepSection surface={surface('staged-identity', never)} />);
  await Promise.resolve();
  expect(screen.getByText('Gathering the delivered evidence for this view…')).toBeTruthy();

  await act(async () => {
    await vi.advanceTimersByTimeAsync(4_000);
  });
  expect(screen.getByText('Weighing the size and direction of each movement…')).toBeTruthy();

  await act(async () => {
    await vi.advanceTimersByTimeAsync(60_000);
  });
  // The final stage holds; the caption never cycles back or runs past the roster.
  expect(screen.getByText(/Still working — the first open of a deep analysis/)).toBeTruthy();
});
