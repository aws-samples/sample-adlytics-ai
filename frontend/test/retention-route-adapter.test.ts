// SPDX-License-Identifier: MIT-0
import assert from 'node:assert/strict';
import { afterEach, test } from 'vitest';

import { retentionRouteAdapter, retentionUrlState } from '@/components/RetentionRouteAdapter';
import { bindingByRoute } from '@/lib/page-bindings';
import { PresentationRequestError, PresentationResponseError } from '@/lib/presentation';
import { fetchRetentionPresentationV2, parsePresentationResultVintageV2, retentionPresentationV2 } from '@/lib/presentation-v2';
import { cloneRetention, retentionFixture, RETENTION_REVISION } from './fixtures/retention-v1-fixtures';

/** Catalog ABS-RET-1 (`docs/ui/page-absence-copy.md`), the served no-vintage message. */
const RETENTION_ABSENCE = "Retention isn't ready to show yet. It's built from your Amazon Marketing Cloud new-to-brand data. When it's ready, you'll see how many of the new customers your ads won come back to buy again, where there's enough evidence to show it.";

type Json = Record<string, unknown>;
const session = 'conversation-fictional';
const control = () => ({
  mode: 'fixed',
  resolved: { as_of_month: '2026-09', label_text: 'Cohorts through September 2026', period_label: 'September 2026', window: { end: '2026-09-30', start: '2025-09-01' } },
  version: 2,
});
function envelope(outcome: 'complete' | 'empty' | 'insufficient' = 'complete'): Json {
  const artifact = retentionFixture(outcome);
  return {
    envelope_version: 2, kind: 'page',
    page: {
      actions: [], artifact, artifact_id: 'retention', artifact_revision: RETENTION_REVISION,
      artifact_type: 'RetentionCohortArtifact', artifact_version: 1, kind: 'retention',
      page_context_expires_at: artifact.page_context_expires_at, page_context_label: artifact.page_context_label,
      page_id: 'retention', route: '/retention/', starter_questions: artifact.starter_questions,
    },
    session_id: session, temporal: control(),
  };
}
const decode = (value: unknown) => retentionPresentationV2(parsePresentationResultVintageV2(value, 'retention'));

afterEach(() => { document.cookie = 'adl_csrf=; Max-Age=0; path=/'; });

test('Retention transport sends only the session and negotiates v2', async () => {
  const calls: Array<{ body: unknown; accept: string | null }> = [];
  document.cookie = 'adl_csrf=csrf; path=/';
  Object.defineProperty(globalThis, 'fetch', { configurable: true, value: async (_input: unknown, init?: RequestInit) => {
    const body = JSON.parse(String(init?.body));
    calls.push({ body, accept: new Headers(init?.headers).get('accept') });
    return new Response(JSON.stringify(envelope('complete')), { status: 200 });
  } });
  assert.equal((await fetchRetentionPresentationV2(session)).kind, 'parent');
  assert.deepEqual(calls.map((entry) => entry.body), [{ session_id: session }]);
  assert.ok(calls.every((entry) => entry.accept === 'application/vnd.adlytics.presentation-v2+json'));
});

test('Retention binds the response window, labels, context and every insight revision', () => {
  assert.equal(decode(envelope()).kind, 'parent');
  const edits: Array<(candidate: Json) => void> = [
    (candidate) => { ((candidate.temporal as Json).resolved as Json).as_of_month = '2026-08'; },
    (candidate) => { ((candidate.temporal as Json).resolved as Json).period_label = 'August 2026'; },
    (candidate) => { ((candidate.temporal as Json).resolved as Json).label_text = 'Cohorts as of September 2026'; },
    (candidate) => { ((candidate.temporal as Json).resolved as Json).selection = { kind: 'default' }; },
    (candidate) => { (candidate.temporal as Json).vintages = [{ as_of_month: '2026-09', availability: 'available', label: 'September 2026' }]; },
    (candidate) => { (candidate.temporal as Json).mode = 'selectable'; },
    (candidate) => { ((((candidate.temporal as Json).resolved as Json).window as Json).start) = '2025-10-01'; },
    (candidate) => { (candidate.page as Json).starter_questions = []; },
    (candidate) => { (((((candidate.page as Json).artifact as Json).markets as Json[])[0]!.insights as Json[])[0]!.artifact_revision) = 's'.repeat(43); },
  ];
  for (const edit of edits) {
    const candidate = cloneRetention(envelope()); edit(candidate);
    assert.throws(() => decode(candidate), PresentationResponseError);
  }
});

test('Retention decodes complete, empty, insufficient and pending server envelopes without fallback', () => {
  for (const outcome of ['complete', 'empty', 'insufficient'] as const) {
    const result = decode(envelope(outcome));
    assert.equal(result.kind, 'parent');
    if (result.kind === 'parent') assert.equal(result.artifact.outcome, outcome);
  }
  const pending = {
    envelope_version: 2, expected_after: null, kind: 'window_unavailable', message: RETENTION_ABSENCE,
    reason_code: 'vintage_unavailable', recovery: { action_id: null, no_self_service_reason: 'wait_for_source_coverage' },
    session_id: session, temporal: { mode: 'fixed', resolved: null, version: 2 },
  };
  const result = decode(pending);
  assert.equal(result.kind, 'window_unavailable');
  if (result.kind !== 'window_unavailable') throw new Error('expected pending result');
  // ADR-139: the adapter returns the served absence (catalog ABS-RET-1) for PageAbsence.
  assert.deepEqual(retentionRouteAdapter.absence(result), { message: RETENTION_ABSENCE, title: bindingByRoute.retention.display_name });

  const noPeriod = {
    ...pending,
    expected_after: '2026-10-16',
    reason_code: 'no_complete_period_measured',
  };
  assert.equal(decode(noPeriod).kind, 'window_unavailable');
  for (const invalid of [
    (() => { const candidate: Json = cloneRetention(pending); delete candidate.expected_after; return candidate; })(),
    { ...pending, expected_after: '2026-10-16' },
    { ...noPeriod, expected_after: null },
    { ...pending, temporal: control() },
    { ...pending, temporal: { mode: 'selectable', resolved: null, version: 2, vintages: [] } },
  ]) assert.throws(() => decode(invalid), PresentationResponseError);
});

test('Retention URL grammar rejects every query, the vintage month included, with zero fetches', async () => {
  assert.deepEqual(retentionUrlState.read(new URLSearchParams()), { intent: null, selection: undefined });
  for (const query of ['asof=2026-09', 'as_of_month=2026-09', 'asof=2026-13', 'preset=7d', 'asof=2026-09&extra=1']) {
    const parsed = retentionUrlState.read(new URLSearchParams(query));
    assert.notEqual(parsed.intent, null);
    let calls = 0;
    Object.defineProperty(globalThis, 'fetch', { configurable: true, value: async () => { calls += 1; return new Response(); } });
    await assert.rejects(
      retentionRouteAdapter.loadParent(session, parsed.selection, new AbortController().signal, parsed.intent),
      (error: unknown) => error instanceof PresentationRequestError && error.errorClass === 'validation',
    );
    assert.equal(calls, 0);
  }
});
