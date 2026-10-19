// SPDX-License-Identifier: MIT-0
import assert from 'node:assert/strict';
import { afterEach, test } from 'vitest';

import { reachRouteAdapter, reachUrlState } from '@/components/ReachRouteAdapter';
import { PresentationRequestError, PresentationResponseError } from '@/lib/presentation';
import {
  fetchReachPresentationV2,
  parsePresentationResultCalendarV2,
  reachPresentationV2,
} from '@/lib/presentation-v2';
import {
  cloneReachFixture,
  reachCompleteFineFixture,
  reachEmptyFixture,
  reachInsufficientFixture,
  reachNoArtifactMissingEnvelope,
  reachNoArtifactPendingEnvelope,
} from './fixtures/reach-v1-fixtures';

type Json = Record<string, unknown>;
const session = 'conversation-fictional';
const expiry = '2099-10-16T12:00:00+00:00';

function servedArtifact(fixture: unknown = reachCompleteFineFixture): Json {
  const artifact = cloneReachFixture(fixture) as Json;
  artifact.page_context_expires_at = expiry;
  return artifact;
}

function temporal(selected = false): Json {
  return {
    comparison: {
      policy: 'prior_calendar_period',
      prior_period_label: 'August 2026',
      status: 'available',
    },
    custom: { availability: 'unavailable', bounds: null, reason_code: 'custom_disabled' },
    mode: 'selectable',
    presets: [
      { availability: 'available', id: 'last_complete_week', label: 'Latest measured week', period_label: 'Week of 14–20 Sep 2026', reason_code: null, reason_text: null, window: { end: '2026-09-20', start: '2026-09-14' } },
      { availability: 'available', id: 'last_complete_month', label: 'Latest measured month', period_label: 'September 2026', reason_code: null, reason_text: null, window: { end: '2026-09-30', start: '2026-09-01' } },
      { availability: 'available', id: 'last_complete_quarter', label: 'Latest measured quarter', period_label: 'Q3 2026', reason_code: null, reason_text: null, window: { end: '2026-09-30', start: '2026-07-01' } },
    ],
    resolved: {
      as_of_month: null,
      display_grain: 'month',
      period_label: 'September 2026',
      reporting_window_status: selected ? 'user_selected' : 'preferred_default',
      selection: selected
        ? { kind: 'preset', preset_id: 'last_complete_month' }
        : { kind: 'default', preset_id: null },
      window: { end: '2026-09-30', start: '2026-09-01' },
    },
    version: 2,
  };
}

function envelope(selected = false, fixture: unknown = reachCompleteFineFixture): Json {
  const artifact = servedArtifact(fixture);
  return {
    envelope_version: 2,
    kind: 'page',
    page: {
      actions: [],
      artifact,
      artifact_id: 'reach',
      artifact_revision: 'r'.repeat(43),
      artifact_type: 'ReachAndFunnelArtifact',
      artifact_version: 1,
      kind: 'reach',
      page_context_expires_at: expiry,
      page_context_label: artifact.page_context_label,
      page_id: 'reach',
      route: '/reach/',
      starter_questions: artifact.starter_questions,
    },
    session_id: session,
    temporal: temporal(selected),
  };
}

function decode(value: unknown, selection: Parameters<typeof reachPresentationV2>[1] = undefined) {
  return reachPresentationV2(parsePresentationResultCalendarV2(value, 'reach'), selection);
}

afterEach(() => {
  document.cookie = 'adl_csrf=; Max-Age=0; path=/';
});

test('Reach transport sends only session and an optional declared calendar preset, negotiates v2 and forwards cancellation', async () => {
  const calls: Array<{ body: unknown; headers: Headers; path: string; signal: AbortSignal | null }> = [];
  document.cookie = 'adl_csrf=csrf; path=/';
  Object.defineProperty(globalThis, 'fetch', {
    configurable: true,
    value: async (input: string | URL | Request, init?: RequestInit) => {
      const body = JSON.parse(String(init?.body)) as Json;
      calls.push({ body, headers: new Headers(init?.headers), path: String(input), signal: init?.signal ?? null });
      return new Response(JSON.stringify(envelope('temporal_selection' in body)), { status: 200 });
    },
  });
  const controller = new AbortController();

  const initial = await fetchReachPresentationV2(session, undefined, controller.signal);
  const selected = await fetchReachPresentationV2(session, {
    kind: 'preset',
    preset_id: 'last_complete_month',
  });

  assert.equal(initial.kind, 'parent');
  assert.equal(selected.kind, 'parent');
  assert.deepEqual(calls.map((call) => call.body), [
    { session_id: session },
    { session_id: session, temporal_selection: { kind: 'preset', preset_id: 'last_complete_month' } },
  ]);
  assert.ok(calls.every((call) => call.path === '/api/presentation/reach'));
  assert.ok(calls.every((call) => call.headers.get('accept') === 'application/vnd.adlytics.presentation-v2+json'));
  assert.equal(calls[0]?.signal, controller.signal);
});

test('Reach transport rejects Custom, rolling presets and a mismatched session before publishing a page', async () => {
  let calls = 0;
  Object.defineProperty(globalThis, 'fetch', {
    configurable: true,
    value: async () => {
      calls += 1;
      return new Response(JSON.stringify({ ...envelope(), session_id: 'another-session' }), { status: 200 });
    },
  });

  await assert.rejects(fetchReachPresentationV2(session, { kind: 'custom', start: '2026-09-01', end: '2026-09-30' }), PresentationResponseError);
  await assert.rejects(fetchReachPresentationV2(session, { kind: 'preset', preset_id: '7d' }), PresentationResponseError);
  assert.equal(calls, 0);
  await assert.rejects(fetchReachPresentationV2(session), PresentationResponseError);
  assert.equal(calls, 1);
});

test('Reach projection rejects response identity, period, preset, page-context, market and unknown-field drift', () => {
  const edits: Array<(candidate: Json) => void> = [
    (candidate) => { ((candidate.page as Json).artifact as Json).unexpected = true; },
    (candidate) => { (candidate.page as Json).artifact_id = 'another-artifact'; },
    (candidate) => { (candidate.page as Json).page_id = 'scorecard'; },
    (candidate) => { (candidate.page as Json).kind = 'reach_and_funnel'; },
    (candidate) => { (((candidate.page as Json).artifact as Json).period as Json).start = '2026-09-02'; },
    (candidate) => { (((candidate.page as Json).artifact as Json).period as Json).grain = 'quarter'; },
    (candidate) => { (candidate.page as Json).starter_questions = []; },
    (candidate) => {
      const artifact = (candidate.page as Json).artifact as Json;
      artifact.markets = [...artifact.markets as Json[], cloneReachFixture((artifact.markets as Json[])[0]!)];
    },
    (candidate) => { candidate.unexpected = true; },
    (candidate) => {
      const comparison = ((candidate.temporal as Json).comparison as Json);
      comparison.reason_code = 'calendar_period_selected';
    },
  ];
  assert.equal(decode(envelope()).kind, 'parent');
  for (const edit of edits) {
    const candidate = cloneReachFixture(envelope());
    edit(candidate);
    assert.throws(() => decode(candidate), PresentationResponseError);
  }
});

test('Reach projection binds page responses to the requested preset and selected preset entry', () => {
  const selectedMonth = { kind: 'preset', preset_id: 'last_complete_month' } as const;
  assert.equal(decode(envelope(true), selectedMonth).kind, 'parent');

  assert.throws(
    () => decode(envelope(true), { kind: 'preset', preset_id: 'last_complete_week' }),
    PresentationResponseError,
  );

  for (const edit of [
    (candidate: Json) => { ((((candidate.temporal as Json).presets as Json[])[1] as Json).window as Json).start = '2026-09-02'; },
    (candidate: Json) => { (((candidate.temporal as Json).presets as Json[])[1] as Json).period_label = 'August 2026'; },
  ]) {
    const inconsistent = cloneReachFixture(envelope());
    edit(inconsistent);
    assert.throws(() => decode(inconsistent), PresentationResponseError);
  }
});

test('Reach projection binds every insight slot revision to the page artifact revision', () => {
  assert.equal(decode(envelope()).kind, 'parent');
  const mismatched = cloneReachFixture(envelope());
  const market = ((((mismatched.page as Json).artifact as Json).markets as Json[])[0] as Json);
  for (const slot of market.insights as Json[]) slot.artifact_revision = 's'.repeat(43);
  assert.throws(() => decode(mismatched), PresentationResponseError);
});

test('Reach projection accepts server-shaped empty and insufficient page envelopes', () => {
  for (const [fixture, outcome] of [
    [reachEmptyFixture, 'empty'],
    [reachInsufficientFixture, 'insufficient'],
  ] as const) {
    const result = decode(envelope(false, fixture));
    assert.equal(result.kind, 'parent');
    if (result.kind !== 'parent') throw new Error('expected Reach page');
    assert.equal(result.artifact.outcome, outcome);
    assert.deepEqual(result.artifact.starter_questions, [
      "Which exposure group's purchaser rate was highest in September 2026, and what is it associated with?",
    ]);
  }
});

test('Reach unavailable responses bind the selected entry, render server text and preserve page refusal controls', () => {
  const selectedMonth = { kind: 'preset', preset_id: 'last_complete_month' } as const;

  for (const fixture of [reachNoArtifactPendingEnvelope, reachNoArtifactMissingEnvelope]) {
    const response = { ...cloneReachFixture(fixture), session_id: session };
    const result = decode(response, selectedMonth);
    assert.equal(result.kind, 'window_unavailable');
    if (result.kind !== 'window_unavailable') throw new Error('expected no-artifact result');
    // ADR-139: the adapter returns the served absence; the controller renders it through
    // PageAbsence and announces it in its persistent status region (controller tests).
    assert.deepEqual(reachRouteAdapter.absence(result), { message: result.message, title: 'Reach and funnel' });
    assert.equal(reachRouteAdapter.windowUnavailable(result), true);
    assert.equal('page_context' in result, false);

    for (const edit of [
      (resolved: Json) => { (resolved.window as Json).start = '2026-09-02'; },
      (resolved: Json) => { resolved.period_label = 'August 2026'; },
    ]) {
      const disagreeing = cloneReachFixture(response);
      edit(((disagreeing.temporal as Json).resolved as Json));
      assert.throws(() => decode(disagreeing, selectedMonth), PresentationResponseError);
    }

    assert.throws(
      () => decode(response, { kind: 'preset', preset_id: 'last_complete_week' }),
      PresentationResponseError,
    );
  }

  assert.equal(decode(envelope(true), selectedMonth).kind, 'parent');
  const unavailablePage = cloneReachFixture(envelope(true));
  const selectedEntry = (((unavailablePage.temporal as Json).presets as Json[])[1] as Json);
  selectedEntry.availability = 'unavailable';
  selectedEntry.reason_code = 'artifact_unavailable';
  selectedEntry.reason_text = "September 2026 isn't measured yet";
  assert.throws(() => decode(unavailablePage, selectedMonth), PresentationResponseError);
});

test('Reach URL state preserves unsupported presets as explicit validation errors', async () => {
  assert.deepEqual(reachUrlState.read(new URLSearchParams()), { intent: null, selection: undefined });
  assert.deepEqual(reachUrlState.read(new URLSearchParams('preset=last_complete_week')), {
    intent: null,
    selection: { kind: 'preset', preset_id: 'last_complete_week' },
  });
  assert.deepEqual(reachUrlState.read(new URLSearchParams('preset=7d')), { intent: '7d', selection: undefined });
  assert.equal(reachUrlState.write(undefined, null).toString(), '');
  assert.equal(reachUrlState.write({ kind: 'preset', preset_id: 'last_complete_month' }, null).toString(), '');
  assert.equal(reachUrlState.write({ kind: 'preset', preset_id: 'last_complete_quarter' }, null).toString(), 'preset=last_complete_quarter');
  assert.equal(reachUrlState.write(undefined, '7d').toString(), 'preset=7d');

  let calls = 0;
  Object.defineProperty(globalThis, 'fetch', {
    configurable: true,
    value: async () => { calls += 1; return new Response(); },
  });
  await assert.rejects(
    reachRouteAdapter.loadParent(session, undefined, new AbortController().signal, '7d'),
    (error: unknown) => error instanceof PresentationRequestError && error.errorClass === 'validation',
  );
  assert.equal(calls, 0);
});
