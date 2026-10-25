// SPDX-License-Identifier: MIT-0
import assert from 'node:assert/strict';
import { afterEach, test } from 'vitest';

import { audiencesRouteAdapter, audiencesUrlState } from '@/components/AudiencesRouteAdapter';
import { bindingByRoute } from '@/lib/page-bindings';
import { PresentationRequestError, PresentationResponseError } from '@/lib/presentation';
import { audiencesPresentationV2, fetchAudiencesPresentationV2, parsePresentationResultCalendarV2 } from '@/lib/presentation-v2';
import fixture from './fixtures/audiences-complete-v2.json';

const session = 'conversation-fictional';
const revision = 'r'.repeat(43);
const control = {
  comparison: { policy: 'prior_calendar_period', prior_period_label: 'August 2026', status: 'unavailable' },
  custom: { availability: 'unavailable', bounds: null, reason_code: 'custom_disabled' },
  mode: 'selectable',
  presets: [{ availability: 'available', id: 'last_complete_month', label: 'Latest measured month', period_label: 'September 2026', reason_code: null, reason_text: null, window: { end: '2026-09-30', start: '2026-09-01' } }],
  resolved: { as_of_month: null, display_grain: 'month', period_label: 'September 2026', reporting_window_status: 'preferred_default', selection: { kind: 'default', preset_id: null }, window: { end: '2026-09-30', start: '2026-09-01' } },
  version: 2,
};
function envelope() {
  return { envelope_version: 2, kind: 'page', page: { actions: [], artifact: structuredClone(fixture), artifact_id: 'audiences', artifact_revision: revision, artifact_type: 'AudienceProfileArtifact', artifact_version: 2, kind: 'audiences', page_context_expires_at: fixture.page_context_expires_at, page_context_label: fixture.page_context_label, page_id: 'audiences', route: '/audiences/', starter_questions: fixture.starter_questions }, session_id: session, temporal: control };
}
const decode = (value: unknown) => audiencesPresentationV2(parsePresentationResultCalendarV2(value, 'audiences'));
afterEach(() => { document.cookie = 'adl_csrf=; Max-Age=0; path=/'; });

test('Audiences transport sends only session, negotiates v2 and binds month, context and revisions', async () => {
  const calls: Array<{ body: unknown; accept: string | null }> = [];
  document.cookie = 'adl_csrf=csrf; path=/';
  Object.defineProperty(globalThis, 'fetch', { configurable: true, value: async (_input: unknown, init?: RequestInit) => {
    calls.push({ body: JSON.parse(String(init?.body)), accept: new Headers(init?.headers).get('accept') });
    return new Response(JSON.stringify(envelope()), { status: 200 });
  } });
  assert.equal((await fetchAudiencesPresentationV2(session)).kind, 'parent');
  assert.deepEqual(calls, [{ body: { session_id: session }, accept: 'application/vnd.adlytics.presentation-v2+json' }]);
  assert.equal(decode(envelope()).kind, 'parent');
  const wrong = envelope(); wrong.page.artifact.month.label = 'August 2026';
  assert.throws(() => decode(wrong), PresentationResponseError);
});

test('Audiences decodes its one-preset pointer-miss envelope as pending, not an invariant error', () => {
  const pending = {
    envelope_version: 2,
    expected_after: null,
    kind: 'window_unavailable',
    // Catalog ABS-AUD-1 (`docs/ui/page-absence-copy.md`): Audiences never appends ABS-ALT.
    message: "Audiences for September 2026 isn't ready to show yet. It's built from your Amazon Marketing Cloud audience segment data. When it's ready, you'll see which audience segments your buyers belong to, where there are enough shoppers to show them.",
    reason_code: 'preset_unavailable',
    recovery: { action_id: null, no_self_service_reason: 'wait_for_source_coverage' },
    session_id: session,
    temporal: {
      ...structuredClone(control),
      presets: [{ availability: 'unavailable', id: 'last_complete_month', label: 'Latest measured month', period_label: 'September 2026', reason_code: 'preset_unavailable', reason_text: 'Not available yet', window: { end: '2026-09-30', start: '2026-09-01' } }],
    },
  };
  const result = decode(pending);
  assert.equal(result.kind, 'window_unavailable');
  if (result.kind !== 'window_unavailable') throw new Error('expected pending result');
  assert.equal(result.message, pending.message);
  // ADR-139: the adapter returns the served absence for PageAbsence.
  assert.deepEqual(audiencesRouteAdapter.absence(result), { message: pending.message, title: bindingByRoute.audiences.display_name });
  // Paired pre-fix control: another page's four-preset temporal contract is refused.
  const scorecardShaped = structuredClone(pending);
  scorecardShaped.temporal.presets.push(...Array.from({ length: 3 }, (_, index) => ({ ...scorecardShaped.temporal.presets[0]!, id: `${index + 1}d` })));
  assert.throws(() => decode(scorecardShaped), PresentationResponseError);
});

test('Audiences rejects every query string with zero fetches and exposes no date control', async () => {
  assert.deepEqual(audiencesUrlState.read(new URLSearchParams()), { intent: null, selection: undefined });
  assert.equal(audiencesRouteAdapter.renderTemporalControl, false);
  for (const query of ['preset=last_complete_month', 'asof=2026-09', 'extra=1', 'a=1&a=2']) {
    const parsed = audiencesUrlState.read(new URLSearchParams(query));
    assert.notEqual(parsed.intent, null);
    let calls = 0;
    Object.defineProperty(globalThis, 'fetch', { configurable: true, value: async () => { calls += 1; return new Response(); } });
    await assert.rejects(audiencesRouteAdapter.loadParent(session, parsed.selection, new AbortController().signal, parsed.intent), (error: unknown) => error instanceof PresentationRequestError && error.errorClass === 'validation');
    assert.equal(calls, 0);
  }
});
test('the Audiences envelope walks the forbidden canonical keys with no exemption', () => {
  // Control: the served fixture names campaigns only by an opaque reference, and decodes.
  assert.equal(JSON.stringify(envelope()).includes('campaign_key'), false);
  assert.equal(decode(envelope()).kind, 'parent');
  // Under any nested position, a campaign or marketplace key fails at the envelope boundary.
  for (const key of ['campaign_key', 'marketplace_key'] as const) {
    const keyed = envelope();
    Object.assign(keyed.page.artifact.markets[0]!.campaign_audiences.index[0]!, { [key]: 'canonical-value' });
    assert.throws(() => parsePresentationResultCalendarV2(keyed, 'audiences'), PresentationResponseError);
  }
});
