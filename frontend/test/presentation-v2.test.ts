// SPDX-License-Identifier: MIT-0
import assert from 'node:assert/strict';
import { afterEach, test } from 'vitest';

import {
  campaignPresentationV2,
  fetchCampaignMetricFocus,
  fetchInsightDeepDive,
  fetchPresentationParentV2,
  parsePresentationResultV2,
} from '../lib/presentation-v2';
import { parseMetricFocusV4 } from '../lib/metric-focus';
import { changeBlock } from './fixtures/change-fixture';
import { ONE_CHANNEL_FILTER } from './fixtures/portfolio-fixture';
import { PresentationRequestError, PresentationResponseError } from '../lib/presentation';

const session = 'conversation-fictional';
const window = { end: '2026-08-21', start: '2026-08-15' };

function temporal() {
  return {
    comparison: {
      policy: 'required_equal_prior',
      reason_code: null,
      status: 'available',
    },
    custom: {
      availability: 'available',
      bounds: {
        earliest_start: '2026-08-01',
        latest_end: '2026-08-21',
        max_duration_days: 90,
        min_duration_days: 2,
      },
      reason_code: null,
    },
    mode: 'selectable',
    presets: [
      {
        availability: 'unavailable',
        id: '1d',
        label: '1D',
        reason_code: 'single_period_non_trend_detail_unavailable',
      },
      { availability: 'available', id: '7d', label: '7D', reason_code: null },
      { availability: 'available', id: '14d', label: '14D', reason_code: null },
      { availability: 'unavailable', id: '30d', label: '30D', reason_code: 'preset_unavailable' },
    ],
    resolved: {
      display_grain: 'day',
      reporting_window_status: 'user_selected',
      selection: { kind: 'custom', preset_id: null },
      window,
    },
    version: 1,
  };
}

function campaignPage() {
  return {
    actions: [] as { action_id: string; target_route: string }[],
    artifact: {
      outcome: 'empty',
      period: window,
    },
    artifact_id: 'campaign_catalog',
    artifact_revision: 'a'.repeat(43),
    artifact_type: 'CampaignPortfolioArtifact',
    artifact_version: 1,
    kind: 'campaign_portfolio',
    page_context_expires_at: '2099-08-21T13:00:00+00:00',
    page_context_label: 'Campaigns · Aug 15\u201321, 2026',
    page_id: 'campaigns',
    route: '/campaigns/',
    scorecard_step: null,
    starter_questions: ['How is spend split across campaigns in Aug 15\u201321, 2026?'] as unknown[],
  };
}

function pageResult() {
  return {
    envelope_version: 2,
    kind: 'page',
    page: campaignPage(),
    session_id: session,
    temporal: temporal(),
  };
}

afterEach(() => {
  document.cookie = 'adl_csrf=; Max-Age=0; path=/';
});

test('V2 decoder accepts a declaration-bound page and exact temporal control', () => {
  const result = parsePresentationResultV2(pageResult(), 'campaigns');

  assert.equal(result.kind, 'page');
  if (result.kind !== 'page') throw new Error('expected V2 page');
  assert.equal(result.page.page_id, 'campaigns');
  assert.equal(result.page.artifact_revision, 'a'.repeat(43));
  assert.deepEqual(result.temporal.resolved.window, window);
  // ADR-120: the page's own revision is the reference; the chip text is the server's.
  assert.deepEqual(result.page.page_context, {
    expires_at: '2099-08-21T13:00:00+00:00',
    label: 'Campaigns · Aug 15\u201321, 2026',
    reference: 'a'.repeat(43),
    starter_questions: ['How is spend split across campaigns in Aug 15\u201321, 2026?'],
  });
});

test('V2 decoder refuses a page without its page context or with an unbounded starter question', () => {
  for (const edit of [
    (page: ReturnType<typeof campaignPage>) => { delete (page as Partial<typeof page>).page_context_label; },
    (page: ReturnType<typeof campaignPage>) => { page.page_context_expires_at = '2099-08-21T13:00:00Z'; },
    (page: ReturnType<typeof campaignPage>) => { page.starter_questions = ['a', 'b', 'c', 'd']; },
    (page: ReturnType<typeof campaignPage>) => { page.starter_questions = ['x'.repeat(121)]; },
    (page: ReturnType<typeof campaignPage>) => { page.starter_questions = [7]; },
    (page: ReturnType<typeof campaignPage>) => { page.page_context_label = ' '; },
  ]) {
    const result = pageResult();
    edit(result.page);
    assert.throws(() => parsePresentationResultV2(result, 'campaigns'), PresentationResponseError);
  }
  // The bound counts code points, as the server does: 120 emoji are one question.
  const emoji = pageResult();
  emoji.page.starter_questions = ['\u{1F600}'.repeat(120)];
  assert.doesNotThrow(() => parsePresentationResultV2(emoji, 'campaigns'));
});

test('V2 decoder rejects wrapper/binding and temporal invariant drift', () => {
  const original = pageResult();
  const wrongAction = {
    ...original,
    page: {
      ...original.page,
      actions: [{ action_id: 'inspect-channel-context', target_route: '/workspace/' }],
    },
  };
  assert.throws(
    () => parsePresentationResultV2(wrongAction, 'campaigns'),
    PresentationResponseError,
  );

  const wrongRevision = pageResult();
  wrongRevision.page.artifact_revision = 'not-opaque';
  assert.throws(
    () => parsePresentationResultV2(wrongRevision, 'campaigns'),
    PresentationResponseError,
  );

  const inconsistentTemporal = pageResult();
  inconsistentTemporal.temporal.resolved.selection = { kind: 'default', preset_id: null };
  assert.throws(
    () => parsePresentationResultV2(inconsistentTemporal, 'campaigns'),
    PresentationResponseError,
  );

  const unavailableWithPage = pageResult();
  unavailableWithPage.kind = 'window_unavailable';
  assert.throws(
    () => parsePresentationResultV2(unavailableWithPage, 'campaigns'),
    PresentationResponseError,
  );
});

test('V2 decoder accepts an absent generation or exactly one whole-second build instant', () => {
  const absent = parsePresentationResultV2(pageResult(), 'campaigns');
  if (absent.kind !== 'page') throw new Error('expected V2 page');
  assert.equal('generation' in absent.page, false);
  for (const generation of [{ built_at: '2026-08-22T06:12:04Z' }, { built_before: '2026-10-09T21:06:31Z' }]) {
    const result = parsePresentationResultV2({ ...pageResult(), page: { ...campaignPage(), generation } }, 'campaigns');
    if (result.kind !== 'page') throw new Error('expected V2 page');
    assert.deepEqual(result.page.generation, generation);
  }
});

test('V2 decoder refuses a generation with both keys, neither key, another key or a non-instant', () => {
  for (const generation of [
    { built_at: '2026-08-22T06:12:04Z', built_before: '2026-08-22T06:12:04Z' },
    {},
    { built_on: '2026-08-22T06:12:04Z' },
    { built_at: '2026-08-22' },
    { built_at: '2026-08-22T06:12:04.5Z' },
    { built_before: '2026-08-22T06:12:04+00:00' },
    { built_at: 1760044000 },
    '2026-08-22T06:12:04Z',
    null,
    [],
  ]) {
    assert.throws(
      () => parsePresentationResultV2({ ...pageResult(), page: { ...campaignPage(), generation } }, 'campaigns'),
      PresentationResponseError,
      JSON.stringify(generation),
    );
  }
});

test('V2 parent transport negotiates Accept and sends only server-declared temporal intent', async () => {
  const calls: Array<{ body: unknown; headers: Headers; path: string }> = [];
  document.cookie = 'adl_csrf=csrf; path=/';
  Object.defineProperty(globalThis, 'fetch', {
    configurable: true,
    value: async (input: string | URL | Request, init?: RequestInit) => {
      calls.push({
        body: JSON.parse(String(init?.body)),
        headers: new Headers(init?.headers),
        path: String(input),
      });
      return new Response(JSON.stringify(pageResult()), { status: 200 });
    },
  });

  const result = await fetchPresentationParentV2('campaigns', session, {
    end: '2026-08-21',
    kind: 'custom',
    start: '2026-08-15',
  });

  assert.equal(result.kind, 'page');
  assert.equal(calls.length, 1);
  assert.deepEqual(calls[0]?.body, {
    session_id: session,
    temporal_selection: { end: '2026-08-21', kind: 'custom', start: '2026-08-15' },
  });
  assert.equal(
    calls[0]?.headers.get('accept'),
    'application/vnd.adlytics.presentation-v2+json',
  );
  assert.equal(calls[0]?.headers.has('x-amz-content-sha256'), true);
});

test('V2 parent transport retains the closed refresh-required error code only', async () => {
  document.cookie = 'adl_csrf=csrf; path=/';
  Object.defineProperty(globalThis, 'fetch', {
    configurable: true,
    value: async () => new Response(JSON.stringify({
      code: 'ADLY-PRESENTATION-0004',
      error: 'Refresh the parent page before selecting again.',
      error_class: 'domain',
    }), { status: 409 }),
  });

  await assert.rejects(
    fetchPresentationParentV2('campaigns', session),
    (error: unknown) => {
      assert.ok(error instanceof PresentationRequestError);
      assert.equal(error.errorClass, 'domain');
      assert.equal(error.code, 'ADLY-PRESENTATION-0004');
      assert.doesNotMatch(error.message, /Refresh the parent page/);
      return true;
    },
  );
});
test('V2 parent transport carries a refusal\'s validated server text, trimmed, apart from its message', async () => {
  document.cookie = 'adl_csrf=csrf; path=/';
  const served = "This page's latest data is still being checked before it's shown. Try again later.";
  Object.defineProperty(globalThis, 'fetch', {
    configurable: true,
    value: async () => new Response(JSON.stringify({
      code: 'ADLY-PRESENTATION-0002',
      error: `\n  ${served}  `,
      error_class: 'domain',
    }), { status: 409 }),
  });
  await assert.rejects(
    fetchPresentationParentV2('campaigns', session),
    (error: unknown) => {
      assert.ok(error instanceof PresentationRequestError);
      assert.equal(error.serverMessage, served);
      // The generic Error.message never carries the server text (it may reach a log).
      assert.equal(error.message, 'Presentation data is unavailable.');
      return true;
    },
  );
  // Control: the transport's bare fallback (an unreadable error body) carries no server text.
  Object.defineProperty(globalThis, 'fetch', { configurable: true, value: async () => new Response('not json', { status: 500 }) });
  await assert.rejects(
    fetchPresentationParentV2('campaigns', session),
    (error: unknown) => error instanceof PresentationRequestError && error.serverMessage === null && error.errorClass === 'invariant',
  );
});


test('V2 parent transport forwards cancellation to the authenticated request boundary', async () => {
  document.cookie = 'adl_csrf=csrf; path=/';
  const controller = new AbortController();
  let observed: AbortSignal | null = null;
  Object.defineProperty(globalThis, 'fetch', {
    configurable: true,
    value: async (_input: string | URL | Request, init?: RequestInit) => {
      observed = init?.signal ?? null;
      return new Response(JSON.stringify(pageResult()), { status: 200 });
    },
  });

  await fetchPresentationParentV2('campaigns', session, undefined, controller.signal);

  assert.equal(observed, controller.signal);
});

test('V2 Campaign projection accepts only a period-bound campaign parent', () => {
  const artifact = {
    channel_filter: ONE_CHANNEL_FILTER,
    channel_view: null,
    evidence_status: null,
    evidence: [{
      actual_coverage: [window],
      coverage_holes: [],
      freshness_qualifiers: [],
      requested_period: window,
      source: 'Governed campaign catalog',
    }],
    insights: [
      {
        artifact_revision: 'unpublished', evidence_line: null, id: 'contribution',
        output_class: 'perspective', prompt_version: 'not-generated', state: 'unavailable', text: null,
      },
      {
        artifact_revision: 'unpublished', evidence_line: null, id: 'divergence',
        output_class: 'perspective', prompt_version: 'not-generated', state: 'unavailable', text: null,
      },
      {
        artifact_revision: 'unpublished', evidence_line: null, id: 'exposure',
        output_class: 'perspective', prompt_version: 'not-generated', state: 'unavailable', text: null,
      },
      {
        artifact_revision: 'unpublished', evidence_line: null, id: 'movers',
        output_class: 'observed', prompt_version: 'deterministic-v1', state: 'unavailable', text: null,
      },
    ],
    kind: 'campaign_portfolio',
    outcome: 'empty',
    pagination: null,
    period: window,
    prior_period: { end: '2026-08-14', start: '2026-08-08' },
    reason_codes: [],
    return_lists: null,
    roster: { channels: [], entries: [], marketplaces: [] },
    rows: [],
    sort: { direction: 'desc', field: 'spend' },
    total_count: 0,
    totals: [],
    trend: null,
  };
  const initial = pageResult();
  const result = { ...initial, page: { ...initial.page, artifact } };
  const parent = campaignPresentationV2(parsePresentationResultV2(result, 'campaigns'));

  assert.equal(parent.kind, 'parent');
  assert.deepEqual(parent.artifact.period, window);
  assert.equal('generation' in parent, false);
  const built = { ...result, page: { ...result.page, generation: { built_at: '2026-08-22T06:12:04Z' } } };
  const builtParent = campaignPresentationV2(parsePresentationResultV2(built, 'campaigns'));
  if (builtParent.kind !== 'parent') throw new Error('expected a campaign parent');
  assert.deepEqual(builtParent.generation, { built_at: '2026-08-22T06:12:04Z' });

  const mismatch = {
    ...initial,
    page: {
      ...initial.page,
      artifact: { ...artifact, period: { end: '2026-08-20', start: '2026-08-15' } },
    },
  };
  assert.throws(
    () => campaignPresentationV2(parsePresentationResultV2(mismatch, 'campaigns')),
    PresentationResponseError,
  );
});


test('V2 Campaign projection retains unavailable temporal controls without exposing its reason code', () => {
  const unavailable = {
    envelope_version: 2,
    kind: 'window_unavailable',
    reason_code: 'single_period_non_trend_detail_unavailable',
    recovery: { action_id: 'choose_covered_period', no_self_service_reason: null },
    session_id: session,
    temporal: temporal(),
  };

  const presentation = campaignPresentationV2(parsePresentationResultV2(unavailable, 'campaigns'));

  assert.equal(presentation.kind, 'window_unavailable');
  assert.deepEqual(presentation.temporal.resolved.window, window);
  assert.equal('reason_code' in presentation, false);
});

function metricFocusResponse(metric = 'purchases') {
  const value = (period: { end: string; start: string }, figure: string | number) => ({
    basis: 'ad_attributed',
    // A count carries no display value (ADR-084).
    display_value: null,
    grain: ['period', 'campaign', 'marketplace'],
    period,
    units: { currency_code: null, kind: 'count' },
    value: figure,
  });
  const period = { end: '2026-08-16', start: '2026-08-10' };
  const prior = { end: '2026-08-09', start: '2026-08-03' };
  return {
    boundary: null,
    change: changeBlock(6, '+50.00%'),
    current: value(period, 18),
    drivers: {
      entries: [
        { change: 6, chip: null, delta: changeBlock(6, '+50.00%'), display_change: null, moves_against_basis: false, name: '2026-08-12 → 2026-08-13', share_of_move: null, units: { currency_code: null, kind: 'count' } },
      ],
      grain: 'day',
      scope: null,
      share_basis: null,
    },
    drivers_summary: null,
    facts: [
      { id: 'peak_day', label: 'Peak day', qualifier: null, value: '2026-08-13 · 5' },
      { id: 'window_total', label: 'Window total', qualifier: null, value: '18' },
    ],
    focus_version: 4,
    insight: {
      artifact_revision: 'b'.repeat(43),
      evidence_line: 'Attributed purchases increased from 12 to 18.',
      id: 'metric-focus',
      output_class: 'perspective',
      prompt_version: 'metric-focus-v1',
      state: 'filled',
      text: 'Attributed purchases improved for this selected campaign.',
    },
    metric: {
      basis: 'ad_attributed',
      definition: 'Ad-attributed purchases within the 14-day attribution window.',
      grain: ['period', 'campaign', 'marketplace'],
      id: metric,
      label: 'Attributed purchases',
      unit: { currency_code: null, kind: 'count' },
    },
    neighbors: ['purchases', 'attributed_roas', 'spend', 'impressions', 'clicks', 'ctr', 'cpc', 'cpm'],
    next_check: null,
    period,
    prior: value(prior, 12),
    prior_period: prior,
    series: {
      display_grain: 'day',
      points: [
        { current: 2, date: '2026-08-10', prior: 1 },
        { current: 5, date: '2026-08-13', prior: 2 },
      ],
    },
  };
}

test('metric focus transport sends only the session, opaque selection reference, and declared metric', async () => {
  const calls: Array<{ body: unknown; path: string }> = [];
  document.cookie = 'adl_csrf=csrf; path=/';
  Object.defineProperty(globalThis, 'fetch', {
    configurable: true,
    value: async (input: string | URL | Request, init?: RequestInit) => {
      calls.push({ body: JSON.parse(String(init?.body)), path: String(input) });
      return new Response(JSON.stringify(metricFocusResponse()), { status: 200 });
    },
  });

  const result = await fetchCampaignMetricFocus(session, 'a'.repeat(43), 'purchases');

  assert.equal(result.insight.text, 'Attributed purchases improved for this selected campaign.');
  assert.deepEqual(calls, [{
    body: { metric: 'purchases', selection_reference: 'a'.repeat(43), session_id: session },
    path: '/api/presentation/campaign-metric-focus',
  }]);
});

test('metric focus transport rejects a response that drifts from the requested metric', async () => {
  document.cookie = 'adl_csrf=csrf; path=/';
  Object.defineProperty(globalThis, 'fetch', {
    configurable: true,
    value: async () => new Response(JSON.stringify(metricFocusResponse('spend')), { status: 200 }),
  });

  await assert.rejects(
    fetchCampaignMetricFocus(session, 'a'.repeat(43), 'purchases'),
    PresentationResponseError,
  );
});

test('metric focus decoder accepts the absence-taxonomy insight shape', async () => {
  const response = metricFocusResponse();
  Object.assign(response.insight, {
    absence_class: 'evaluated-quiet',
    absence_reason: 'No supplied finding is material.',
    evidence_line: null,
    state: 'silent',
    text: null,
  });
  document.cookie = 'adl_csrf=csrf; path=/';
  Object.defineProperty(globalThis, 'fetch', {
    configurable: true,
    value: async () => new Response(JSON.stringify(response), { status: 200 }),
  });

  const result = await fetchCampaignMetricFocus(session, 'a'.repeat(43), 'purchases');

  assert.equal(result.insight.state, 'silent');
  assert.equal(result.insight.absence_class, 'evaluated-quiet');
  assert.equal(result.insight.absence_reason, 'No supplied finding is material.');
});

test('metric focus decoder refuses a present absence pair with a null or missing class', async () => {
  const hostileInsights: ReadonlyArray<Record<string, unknown>> = [
    // The pair present with a null class is neither legal shape.
    { absence_class: null, absence_reason: null, evidence_line: null, state: 'silent', text: null },
    // A class key without its sibling reason key is neither legal shape.
    { absence_class: 'evaluated-quiet', evidence_line: null, state: 'silent', text: null },
  ];
  document.cookie = 'adl_csrf=csrf; path=/';
  for (const overrides of hostileInsights) {
    const response = metricFocusResponse();
    Object.assign(response.insight, overrides);
    Object.defineProperty(globalThis, 'fetch', {
      configurable: true,
      value: async () => new Response(JSON.stringify(response), { status: 200 }),
    });
    await assert.rejects(fetchCampaignMetricFocus(session, 'a'.repeat(43), 'purchases'));
  }
});
function serveFocus(response: unknown): void {
  document.cookie = 'adl_csrf=csrf; path=/';
  Object.defineProperty(globalThis, 'fetch', {
    configurable: true,
    value: async () => new Response(JSON.stringify(response), { status: 200 }),
  });
}
test('metric focus v4 decoder accepts a currency entry with its own units and two-place change', async () => {
  const response = metricFocusResponse() as unknown as { drivers: { entries: Record<string, unknown>[] } };
  // A USD entry under a count focus: its change of 6 displays as 6.00.
  response.drivers.entries[0] = { ...response.drivers.entries[0], display_change: '6.00', units: { currency_code: 'USD', kind: 'currency' } };
  serveFocus(response);
  const result = await fetchCampaignMetricFocus(session, 'a'.repeat(43), 'purchases');
  assert.equal(result.focus_version, 4);
  assert.equal(result.current.display_value, null);
  assert.ok('entries' in result.drivers);
  assert.equal(result.drivers.entries[0]?.display_change, '6.00');
  assert.equal(result.drivers.entries[0]?.units.currency_code, 'USD');
});
test('metric focus v4 decoder refuses v3, a missing display value, a display change on a count, and a v3 change', async () => {
  const hostile: ReadonlyArray<(response: Record<string, unknown> & { current: Record<string, unknown>; drivers: { entries: Record<string, unknown>[] } }) => void> = [
    (response) => { response.focus_version = 3; },
    (response) => { (response as Record<string, unknown>).change = { absolute: '6', percentage: '+50.00%' }; },
    (response) => { delete response.drivers.entries[0]!.delta; },
    (response) => { response.drivers.entries[0]!.delta = { ...(response.drivers.entries[0]!.delta as object), sentiment: 'good' }; },
    (response) => { delete response.current.display_value; },
    (response) => { response.current.display_value = '18.00'; },
    (response) => { delete response.drivers.entries[0]!.units; },
    (response) => { response.drivers.entries[0]!.display_change = '6.00'; },
    (response) => {
      response.drivers.entries[0]!.units = { currency_code: 'USD', kind: 'currency' };
      response.drivers.entries[0]!.display_change = '6.000';
    },
  ];
  for (const mutate of hostile) {
    const response = structuredClone(metricFocusResponse()) as unknown as Parameters<typeof mutate>[0];
    mutate(response);
    serveFocus(response);
    await assert.rejects(fetchCampaignMetricFocus(session, 'a'.repeat(43), 'purchases'));
  }
});

function deepResponse(slotId: string) {
  return {
    absence_reason: 'The deep analysis is not available right now.',
    artifact_revision: 'sha256:fictional',
    deep: null,
    deep_prompt_version: 'v1',
    period: window,
    slot_id: slotId,
    state: 'unavailable',
  };
}

function recordDeepCalls(): Array<{ body: unknown; path: string }> {
  const calls: Array<{ body: unknown; path: string }> = [];
  document.cookie = 'adl_csrf=csrf; path=/';
  Object.defineProperty(globalThis, 'fetch', {
    configurable: true,
    value: async (input: string | URL | Request, init?: RequestInit) => {
      const body = JSON.parse(String(init?.body)) as { slot_id: string };
      calls.push({ body, path: String(input) });
      return new Response(JSON.stringify(deepResponse(body.slot_id)), { status: 200 });
    },
  });
  return calls;
}

test('deep requests carry the served sort, and the segment only past the first (W11)', async () => {
  const calls = recordDeepCalls();
  const sort = { direction: 'desc', field: 'attributed_roas' } as const;

  await fetchInsightDeepDive({ segment: 1, session_id: session, slot_id: 'contribution', sort });
  await fetchInsightDeepDive({ segment: 0, session_id: session, slot_id: 'divergence', sort });
  await fetchInsightDeepDive({ session_id: session, slot_id: 'demand-shape' });

  assert.deepEqual(calls.map((call) => call.body), [
    { segment: 1, session_id: session, slot_id: 'contribution', sort: { direction: 'desc', field: 'attributed_roas' } },
    { session_id: session, slot_id: 'divergence', sort: { direction: 'desc', field: 'attributed_roas' } },
    { session_id: session, slot_id: 'demand-shape' },
  ]);
  assert.ok(calls.every((call) => call.path === '/api/presentation/insight-deep-dive'));
});

test('deep requests refuse an invalid segment or sort before any network call', async () => {
  const calls = recordDeepCalls();

  await assert.rejects(
    fetchInsightDeepDive({ segment: -1, session_id: session, slot_id: 'contribution' }),
    PresentationResponseError,
  );
  await assert.rejects(
    fetchInsightDeepDive({
      session_id: session,
      slot_id: 'contribution',
      sort: { direction: 'down', field: 'spend' } as unknown as { direction: 'desc'; field: 'spend' },
    }),
    PresentationResponseError,
  );
  assert.equal(calls.length, 0);
});

test('the focus decoder enforces the revised driver fields null by grain', () => {
  const raw = metricFocusResponse() as unknown as Record<string, unknown>;
  // Positive control: the day-grain fixture decodes with null scope, basis and summary.
  assert.equal(parseMetricFocusV4(raw).drivers_summary, null);
  const drivers = raw.drivers as Record<string, unknown>;
  const scope = { count: 1, label: 'Largest changes', population: 1, share_suffix: null };
  // A day grain refuses a scope; a campaign grain refuses a null scope or summary.
  assert.throws(() => parseMetricFocusV4({ ...raw, drivers: { ...drivers, scope, share_basis: 'none' } }), TypeError);
  assert.throws(() => parseMetricFocusV4({ ...raw, drivers: { ...drivers, grain: 'campaign' } }), TypeError);
  assert.throws(() => parseMetricFocusV4({
    ...raw,
    drivers: { ...drivers, grain: 'campaign', scope, share_basis: 'none' },
    drivers_summary: null,
  }), TypeError);
  // A chip never arrives beside a share.
  const entries = drivers.entries as Record<string, unknown>[];
  assert.throws(() => parseMetricFocusV4({
    ...raw,
    drivers: { ...drivers, entries: [{ ...entries[0], chip: 'Moved against the overall change', share_of_move: '10%' }] },
  }), TypeError);
});
