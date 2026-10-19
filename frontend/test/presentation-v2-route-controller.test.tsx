// SPDX-License-Identifier: MIT-0
import assert from 'node:assert/strict';
import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, test, vi } from 'vitest';

import {
  PI_DOMAIN_FALLBACK,
  PresentationV2RouteController,
  type V2RouteAdapter,
} from '@/components/PresentationV2RouteController';
import { audiencesRouteAdapter } from '@/components/AudiencesRouteAdapter';
import { campaignRouteAdapter } from '@/components/CampaignRouteAdapter';
import { InsightNextStepControl } from '@/components/InsightCard';
import { newCustomersRouteAdapter } from '@/components/NewCustomersRouteAdapter';
import { reachRouteAdapter } from '@/components/ReachRouteAdapter';
import { retailHealthRouteAdapter } from '@/components/RetailHealthRouteAdapter';
import { retentionRouteAdapter } from '@/components/RetentionRouteAdapter';
import { scorecardRouteAdapter } from '@/components/ScorecardRouteAdapter';
import { bindingByRoute } from '@/lib/page-bindings';
import { PresentationRequestError } from '@/lib/presentation';
import { fetchReachPresentationV2 } from '@/lib/presentation-v2';
import type { InsightNextStep } from '@/lib/insight-next-step';
import { stateParent, type PresentationRouteState } from '@/lib/presentation-route-state';
import type { PageGenerationV1, TemporalControlV1, TemporalSelectionV1 } from '@/lib/presentation-v2';

type Parent = {
  readonly id: string;
  readonly temporal: TemporalControlV1;
  readonly unavailable?: boolean;
};

type Child = { readonly id: string };

function deferred<Value>() {
  let resolve: (value: Value) => void = () => undefined;
  let reject: (reason?: unknown) => void = () => undefined;
  const promise = new Promise<Value>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, reject, resolve };
}

function temporal(): TemporalControlV1 {
  return {
    comparison: { policy: 'required_equal_prior', reason_code: null, status: 'available' },
    custom: {
      availability: 'available',
      bounds: {
        earliest_start: '2026-08-01',
        latest_end: '2026-08-21',
        max_duration_days: 90,
        min_duration_days: 1,
      },
      reason_code: null,
    },
    mode: 'selectable',
    presets: [
      { availability: 'available', id: '1d', label: '1D', reason_code: null },
      { availability: 'available', id: '7d', label: '7D', reason_code: null },
      { availability: 'available', id: '14d', label: '14D', reason_code: null },
      { availability: 'available', id: '30d', label: '30D', reason_code: null },
    ],
    resolved: {
      display_grain: 'period',
      reporting_window_status: 'preferred_default',
      selection: { kind: 'default', preset_id: null },
      window: { end: '2026-08-21', start: '2026-08-15' },
    },
    version: 1,
  };
}

function parent(id: string, unavailable = false): Parent {
  return { id, temporal: temporal(), ...(unavailable ? { unavailable: true } : {}) };
}

function renderedParent(state: PresentationRouteState<Parent, Child, 'load'>): Parent | null {
  return stateParent(state);
}

afterEach(() => cleanup());

const band = {
  absence: () => ({ message: 'A fictional absence message.', title: 'Fictional page' }),
  bandTitle: () => null,
  bandTitleLoading: () => null,
  pageContext: () => null,
} as const;

/** The fixtures' one state predicate, written as the type guard the adapter contract requires. */
function unavailableParent<Value extends Parent>(value: Value): value is Value {
  return value.unavailable === true;
}

test('V2 route controller clears parent output before a returned temporal selection replaces it', async () => {
  const first = deferred<Parent>();
  const second = deferred<Parent>();
  const loadParent = vi.fn()
    .mockReturnValueOnce(first.promise)
    .mockReturnValueOnce(second.promise);
  const adapter: V2RouteAdapter<Parent> = {
    ...band,
    loadParent,
    pageIssueClass: () => 'invariant',
    render: (state) => state.kind === 'parent_ready'
      ? <output aria-label="parent">{state.parent.id}</output>
      : null,
    route: 'campaigns',
    temporal: (value) => value.temporal,
    windowUnavailable: unavailableParent,
  };
  const user = userEvent.setup();
  render(<PresentationV2RouteController adapter={adapter} sessionId="session-a" />);

  await act(async () => first.resolve(parent('initial')));
  assert.equal(await screen.findByLabelText('parent').then((node) => node.textContent), 'initial');

  await user.click(screen.getByRole('radio', { name: '7D' }));
  await waitFor(() => assert.equal(loadParent.mock.calls.length, 2));
  assert.deepEqual(loadParent.mock.calls[1]?.slice(0, 2), [
    'session-a',
    { kind: 'preset', preset_id: '7d' },
  ]);
  assert.equal(screen.queryByLabelText('parent'), null);
  // The loading note announces itself beside the persistent (empty) page status region.
  assert.ok(screen.getAllByRole('status').some((node) => node.classList.contains('loading-note')));

  await act(async () => second.resolve(parent('preset')));
  assert.equal(await screen.findByLabelText('parent').then((node) => node.textContent), 'preset');
});

test('V2 route controller aborts and discards a superseded parent result', async () => {
  const first = deferred<Parent>();
  const second = deferred<Parent>();
  const signals: AbortSignal[] = [];
  const loadParent = vi.fn((_session: string, _selection: TemporalSelectionV1 | undefined, signal: AbortSignal) => {
    signals.push(signal);
    return signals.length === 1 ? first.promise : second.promise;
  });
  const adapter: V2RouteAdapter<Parent> = {
    ...band,
    loadParent,
    pageIssueClass: () => 'invariant',
    render: (state) => state.kind === 'parent_ready'
      ? <output aria-label="parent">{state.parent.id}</output>
      : null,
    route: 'campaigns',
    temporal: (value) => value.temporal,
    windowUnavailable: unavailableParent,
  };
  const view = render(<PresentationV2RouteController adapter={adapter} sessionId="session-a" />);
  view.rerender(<PresentationV2RouteController adapter={adapter} sessionId="session-b" />);
  await waitFor(() => assert.equal(loadParent.mock.calls.length, 2));
  assert.equal(signals[0]?.aborted, true);

  await act(async () => second.resolve(parent('current')));
  assert.equal(await screen.findByLabelText('parent').then((node) => node.textContent), 'current');
  await act(async () => first.resolve(parent('stale')));
  assert.equal(screen.getByLabelText('parent').textContent, 'current');
});

test('V2 route controller aborts child work on temporal replacement without carrying child output forward', async () => {
  const parents = [deferred<Parent>(), deferred<Parent>()];
  const child = deferred<Child>();
  let parentIndex = 0;
  const childSignals: AbortSignal[] = [];
  const adapter: V2RouteAdapter<Parent, Child, 'load'> = {
    ...band,
    child: {
      load: (_parent, _command, _session, signal) => {
        childSignals.push(signal);
        return child.promise;
      },
      selectionUnavailable: () => false,
      rematch: () => null,
    },
    loadParent: () => parents[parentIndex++]!.promise,
    pageIssueClass: () => 'invariant',
    render: (state, requestChild) => {
      const currentParent = renderedParent(state);
      if (currentParent === null) return null;
      return (
        <>
          <output aria-label="parent">{currentParent.id}</output>
          {state.kind === 'child_ready' ? <output aria-label="child">{state.child.id}</output> : null}
          <button type="button" onClick={() => requestChild('load')}>Load child</button>
        </>
      );
    },
    route: 'campaigns',
    temporal: (value) => value.temporal,
    windowUnavailable: unavailableParent,
  };
  const user = userEvent.setup();
  render(<PresentationV2RouteController adapter={adapter} sessionId="session-a" />);
  await act(async () => parents[0]!.resolve(parent('initial')));
  await screen.findByLabelText('parent');

  await user.click(screen.getByRole('button', { name: 'Load child' }));
  await waitFor(() => assert.equal(childSignals.length, 1));
  await user.click(screen.getByRole('radio', { name: '14D' }));
  assert.equal(childSignals[0]?.aborted, true);
  assert.equal(screen.queryByLabelText('parent'), null);

  await act(async () => child.resolve({ id: 'stale-child' }));
  assert.equal(screen.queryByLabelText('child'), null);
  await act(async () => parents[1]!.resolve(parent('replacement')));
  assert.equal(await screen.findByLabelText('parent').then((node) => node.textContent), 'replacement');
  assert.equal(screen.queryByLabelText('child'), null);
});

test('V2 route controller retains returned temporal controls for a temporal refusal', async () => {
  const render_ = vi.fn(() => <output aria-label="temporal-result">Rendered</output>);
  const adapter: V2RouteAdapter<Parent> = {
    ...band,
    absence: (value) => ({ message: `No body for ${value.id}.`, title: 'Fictional page' }),
    loadParent: async () => parent('unavailable', true),
    pageIssueClass: () => 'invariant',
    render: render_,
    route: 'campaigns',
    temporal: (value) => value.temporal,
    temporalRefusal: () => ({ reason: 'required_comparison_unavailable', recovery: 'Choose another returned period.' }),
    windowUnavailable: unavailableParent,
  };
  render(<PresentationV2RouteController adapter={adapter} sessionId="session-a" />);

  // ADR-139: the absence renders through PageAbsence; the adapter's render never runs here.
  assert.equal((await screen.findByRole('heading', { name: 'Fictional page' })).textContent, 'Fictional page');
  assert.ok(screen.getByText('No body for unavailable.'));
  assert.equal(screen.queryByLabelText('temporal-result'), null);
  assert.equal(render_.mock.calls.length, 0);
  assert.ok(screen.getByRole('radiogroup', { name: 'Available reporting periods' }));
  assert.match(screen.getAllByRole('status').find((item) => item.textContent?.includes('cannot be compared safely'))?.textContent ?? '', /cannot be compared safely/);
});


test('V2 route controller retains the parent when a child is unavailable and clears a selected child', async () => {
  const child = deferred<Child>();
  const adapter: V2RouteAdapter<Parent, Child, 'load'> = {
    ...band,
    child: {
      load: () => child.promise,
      selectionUnavailable: () => false,
      rematch: () => null,
    },
    loadParent: async () => parent('parent'),
    pageIssueClass: () => 'dependency',
    render: (state, requestChild, clearChild) => {
      const currentParent = renderedParent(state);
      if (currentParent === null) return null;
      return (
        <>
          <output aria-label="parent">{currentParent.id}</output>
          {state.kind === 'child_loading' ? <output aria-label="loading">Loading</output> : null}
          {state.kind === 'child_unavailable' ? <output aria-label="child-error">{state.issueClass}</output> : null}
          {state.kind === 'child_ready' ? <output aria-label="child">{state.child.id}</output> : null}
          <button type="button" onClick={() => requestChild('load')}>Load child</button>
          <button type="button" onClick={clearChild}>Clear child</button>
        </>
      );
    },
    route: 'campaigns',
    temporal: (value) => value.temporal,
    windowUnavailable: unavailableParent,
  };
  const user = userEvent.setup();
  render(<PresentationV2RouteController adapter={adapter} sessionId="session-a" />);
  await screen.findByLabelText('parent');

  await user.click(screen.getByRole('button', { name: 'Load child' }));
  assert.ok(screen.getByLabelText('loading'));
  await act(async () => child.reject(new Error('unavailable')));
  assert.equal(screen.getByLabelText('parent').textContent, 'parent');
  assert.equal(screen.getByLabelText('child-error').textContent, 'dependency');

  const selectedAdapter: V2RouteAdapter<Parent, Child, 'load'> = {
    ...adapter,
    child: { load: async () => ({ id: 'selected' }), rematch: () => null, selectionUnavailable: () => false },
  };
  cleanup();
  render(<PresentationV2RouteController adapter={selectedAdapter} sessionId="session-a" />);
  await screen.findByLabelText('parent');
  await user.click(screen.getByRole('button', { name: 'Load child' }));
  await screen.findByLabelText('child');
  await user.click(screen.getByRole('button', { name: 'Clear child' }));
  assert.equal(screen.queryByLabelText('child'), null);
  assert.equal(screen.getByLabelText('parent').textContent, 'parent');
});

test('V2 route controller refreshes the parent only for a declared child refresh condition', async () => {
  const parents = [parent('initial'), parent('refreshed')];
  const refresh = new Error('refresh');
  let parentLoads = 0;
  const adapter: V2RouteAdapter<Parent, Child, 'load'> = {
    ...band,
    child: {
      load: async () => { throw refresh; },
      selectionUnavailable: (error) => error === refresh,
      rematch: () => null,
    },
    loadParent: async () => parents[parentLoads++]!,
    pageIssueClass: () => 'dependency',
    render: (state, requestChild) => {
      const currentParent = renderedParent(state);
      return currentParent === null ? null : (
        <>
          <output aria-label="parent">{currentParent.id}</output>
          <button type="button" onClick={() => requestChild('load')}>Load child</button>
        </>
      );
    },
    route: 'campaigns',
    temporal: (value) => value.temporal,
    windowUnavailable: unavailableParent,
  };
  const user = userEvent.setup();
  render(<PresentationV2RouteController adapter={adapter} sessionId="session-a" />);
  assert.equal(await screen.findByLabelText('parent').then((node) => node.textContent), 'initial');
  await user.click(screen.getByRole('button', { name: 'Load child' }));
  assert.equal(await screen.findByLabelText('parent').then((node) => node.textContent), 'refreshed');
  assert.equal(parentLoads, 2);
});

test('V2 route controller retains the served generation beside the window while a new window loads', async () => {
  type BuiltParent = Parent & { readonly generation?: PageGenerationV1 };
  const first = deferred<BuiltParent>();
  const second = deferred<BuiltParent>();
  const loadParent = vi.fn()
    .mockReturnValueOnce(first.promise)
    .mockReturnValueOnce(second.promise);
  const adapter: V2RouteAdapter<BuiltParent> = {
    ...band,
    generation: (value) => value.generation,
    loadParent,
    pageIssueClass: () => 'invariant',
    render: (state) => state.kind === 'parent_ready'
      ? <output aria-label="parent">{state.parent.id}</output>
      : null,
    route: 'campaigns',
    temporal: (value) => value.temporal,
    windowUnavailable: unavailableParent,
  };
  const footnote = () => document.querySelector('.footnote.win-note')?.textContent ?? '';
  const user = userEvent.setup();
  render(<PresentationV2RouteController adapter={adapter} sessionId="session-a" />);

  await act(async () => first.resolve({ ...parent('initial'), generation: { built_at: '2026-08-22T06:12:04Z' } }));
  await screen.findByLabelText('parent');
  assert.match(footnote(), /built 2026-08-22 06:12:04 UTC\./);

  await user.click(screen.getByRole('radio', { name: '7D' }));
  await waitFor(() => assert.equal(loadParent.mock.calls.length, 2));
  assert.equal(screen.queryByLabelText('parent'), null);
  assert.match(footnote(), /built 2026-08-22 06:12:04 UTC\./);

  // Control: the new parent's own generation replaces the retained one once it resolves.
  await act(async () => second.resolve({ ...parent('preset'), generation: { built_before: '2026-10-09T21:06:31Z' } }));
  await screen.findByLabelText('parent');
  assert.match(footnote(), /built before 2026-10-09 21:06:31 UTC\./);
});

test('a card preset step selects the preset like the date control and leaves focus on it', async () => {
  const first = deferred<Parent>();
  const second = deferred<Parent>();
  const loadParent = vi.fn()
    .mockReturnValueOnce(first.promise)
    .mockReturnValueOnce(second.promise);
  const step: InsightNextStep = {
    kind: 'open_preset',
    label: 'Show 14 days',
    reason: 'A fictional reason.',
    target: { page_id: 'campaigns', preset_id: '14d' },
  };
  const adapter: V2RouteAdapter<Parent> = {
    ...band,
    loadParent,
    pageIssueClass: () => 'invariant',
    render: (state) => state.kind === 'parent_ready'
      ? <InsightNextStepControl pageId="campaigns" step={step} />
      : null,
    route: 'campaigns',
    temporal: (value) => value.temporal,
    windowUnavailable: unavailableParent,
  };
  const user = userEvent.setup();
  render(<PresentationV2RouteController adapter={adapter} sessionId="session-a" />);
  await act(async () => first.resolve(parent('initial')));
  await user.click(await screen.findByRole('button', { name: 'Show 14 days' }));
  await waitFor(() => assert.equal(loadParent.mock.calls.length, 2));
  assert.deepEqual(loadParent.mock.calls[1]?.slice(0, 2), ['session-a', { kind: 'preset', preset_id: '14d' }]);
  const fourteen = temporal();
  await act(async () => second.resolve({
    id: 'preset',
    temporal: { ...fourteen, resolved: { ...fourteen.resolved, selection: { kind: 'preset', preset_id: '14d' }, window: { end: '2026-08-21', start: '2026-08-08' } } },
  }));
  // ia §2: a preset change isn't a route change, so focus is on the date control's preset.
  const fourteenControl = screen.getByRole('radio', { name: '14D' });
  // Compare identities only: printing two DOM nodes in a failure message never finishes.
  await waitFor(() => assert.ok(document.activeElement === fourteenControl, 'focus is not on the 14D preset'));
});

/* ---------- ADR-125: detail in place with one history entry, re-opened on a window change ---------- */

function detailAdapter(rematch: (parent: Parent, child: Child) => 'load' | null, parents: Parent[]): V2RouteAdapter<Parent, Child, 'load'> {
  let index = 0;
  return {
    ...band,
    child: {
      historyMarker: true,
      load: async (current) => ({ id: `child-of-${current.id}` }),
      rematch,
      reopenAfterParentChange: true,
      selectionUnavailable: () => false,
    },
    loadParent: async () => parents[Math.min(index++, parents.length - 1)]!,
    pageIssueClass: () => 'invariant',
    render: (state, requestChild, clearChild, _intent, _session, closedChild) => {
      const currentParent = renderedParent(state);
      if (currentParent === null) return null;
      return (
        <>
          <output aria-label="parent">{currentParent.id}</output>
          {state.kind === 'child_ready' ? <output aria-label="child">{state.child.id}</output> : null}
          {closedChild == null ? null : <output aria-label="closed">{closedChild.id}</output>}
          <button type="button" onClick={() => requestChild('load')}>Open detail</button>
          <button type="button" onClick={clearChild}>Back to the list</button>
        </>
      );
    },
    route: 'retail-health',
    temporal: (value) => value.temporal,
    windowUnavailable: unavailableParent,
  };
}

test('opening detail adds one history entry and the browser Back closes it without a second pop', async () => {
  const back = vi.spyOn(window.history, 'back').mockImplementation(() => undefined);
  const push = vi.spyOn(window.history, 'pushState');
  const user = userEvent.setup();
  render(<PresentationV2RouteController adapter={detailAdapter(() => null, [parent('list')])} sessionId="session-a" />);
  await screen.findByLabelText('parent');
  await user.click(screen.getByRole('button', { name: 'Open detail' }));
  assert.equal((await screen.findByLabelText('child')).textContent, 'child-of-list');
  assert.equal(push.mock.calls.length, 1);
  await act(async () => { window.dispatchEvent(new PopStateEvent('popstate')); });
  await waitFor(() => assert.equal(screen.queryByLabelText('child'), null));
  assert.equal(back.mock.calls.length, 0);
  back.mockRestore();
  push.mockRestore();
});

test('"Back to the list" closes detail and consumes its one history entry', async () => {
  const back = vi.spyOn(window.history, 'back').mockImplementation(() => undefined);
  const user = userEvent.setup();
  render(<PresentationV2RouteController adapter={detailAdapter(() => null, [parent('list')])} sessionId="session-a" />);
  await screen.findByLabelText('parent');
  await user.click(screen.getByRole('button', { name: 'Open detail' }));
  await screen.findByLabelText('child');
  await user.click(screen.getByRole('button', { name: 'Back to the list' }));
  await waitFor(() => assert.equal(screen.queryByLabelText('child'), null));
  assert.equal(back.mock.calls.length, 1);
  back.mockRestore();
});

test('a window change re-opens the entity when exactly one row matches, and otherwise says which closed', async () => {
  const back = vi.spyOn(window.history, 'back').mockImplementation(() => undefined);
  const user = userEvent.setup();
  const reopened = render(<PresentationV2RouteController adapter={detailAdapter(() => 'load', [parent('7d'), parent('14d')])} sessionId="session-a" />);
  await screen.findByLabelText('parent');
  await user.click(screen.getByRole('button', { name: 'Open detail' }));
  await screen.findByLabelText('child');
  await user.click(screen.getByRole('radio', { name: '14D' }));
  assert.equal((await screen.findByLabelText('child')).textContent, 'child-of-14d');
  assert.equal(back.mock.calls.length, 0);
  reopened.unmount();

  render(<PresentationV2RouteController adapter={detailAdapter(() => null, [parent('7d'), parent('14d')])} sessionId="session-b" />);
  await screen.findByLabelText('parent');
  await user.click(screen.getByRole('button', { name: 'Open detail' }));
  await screen.findByLabelText('child');
  await user.click(screen.getByRole('radio', { name: '14D' }));
  assert.equal((await screen.findByLabelText('closed')).textContent, 'child-of-7d');
  assert.equal(screen.queryByLabelText('child'), null);
  assert.equal(back.mock.calls.length, 1);
  back.mockRestore();
});

// ia §8.1 URL state: the tab writes only its own entry, and a refused value reads as omitted.
type FilterIntent = { readonly filter?: string };
const filterUrl = {
  read: (params: URLSearchParams) => ({
    intent: params.get('channel') === null ? {} : { filter: params.get('channel') as string },
    selection: undefined,
  }),
  write: (_selection: TemporalSelectionV1 | undefined, intent: FilterIntent) => {
    const params = new URLSearchParams();
    if (intent.filter !== undefined) params.set('channel', intent.filter);
    return params;
  },
  served: (served: unknown, intent: FilterIntent): FilterIntent => (
    intent.filter !== undefined && (served as Parent).id === 'all' ? {} : intent
  ),
};

function filterAdapter(loadParent: V2RouteAdapter<Parent, Child, 'load', FilterIntent>['loadParent']): V2RouteAdapter<Parent, Child, 'load', FilterIntent> {
  return {
    ...band,
    child: {
      historyMarker: true,
      load: async () => ({ id: 'child' }),
      rematch: () => null,
      selectionUnavailable: () => false,
    },
    initialParentIntent: {},
    loadParent,
    pageIssueClass: () => 'invariant',
    render: (state) => (state.kind === 'parent_ready' ? <p>{renderedParent(state)?.id}</p> : null),
    route: 'campaigns',
    temporal: (value) => value.temporal,
    url: filterUrl,
    windowUnavailable: unavailableParent,
  };
}

test('Back landing on another tab never takes this tab\'s query', async () => {
  window.history.replaceState(null, '', '/campaigns/?channel=amazon_dsp');
  const loadParent = vi.fn(async () => parent('amazon_dsp'));
  render(<PresentationV2RouteController adapter={filterAdapter(loadParent)} sessionId="session" />);
  await screen.findByText('amazon_dsp');
  assert.equal(window.location.search, '?channel=amazon_dsp');
  // The browser lands on an earlier Retail health entry before Campaigns unmounts.
  act(() => {
    window.history.pushState(null, '', '/retail-health/?preset=14d');
    window.dispatchEvent(new PopStateEvent('popstate'));
  });
  assert.equal(`${window.location.pathname}${window.location.search}`, '/retail-health/?preset=14d');
  window.history.replaceState(null, '', '/');
});

test('A mount clears the adapter\'s module state before the first parent load', async () => {
  const order: string[] = [];
  const loadParent = vi.fn(async () => { order.push('load'); return parent('all'); });
  render(<PresentationV2RouteController adapter={{ ...filterAdapter(loadParent), onMount: () => { order.push('mount'); } }} sessionId="session" />);
  await screen.findByText('all');
  assert.equal(order[0], 'mount');
  assert.ok(order.includes('load'));
});

test('A channel served as All channels leaves the URL and the next request without being asked again', async () => {
  window.history.replaceState(null, '', '/campaigns/?channel=amazon_sponsored_television');
  const loadParent = vi.fn<V2RouteAdapter<Parent, Child, 'load', FilterIntent>['loadParent']>(async () => parent('all'));
  render(<PresentationV2RouteController adapter={filterAdapter(loadParent)} sessionId="session" />);
  await screen.findByText('all');
  assert.deepEqual(loadParent.mock.calls[0]?.[3], { filter: 'amazon_sponsored_television' });
  await waitFor(() => assert.equal(window.location.search, ''));
  // Adopting the served intent doesn't re-request the parent already on screen.
  assert.equal(loadParent.mock.calls.length, 1);
  window.history.replaceState(null, '', '/');
});

/* ---------- ADR-139: page-level absence, one persistent status region, server refusal text ---------- */

const STATUS_REGION = 'p.sr-only[role="status"]';

test('the status region is one instance from loading to an absence, and holds the served title and message', async () => {
  const pending = deferred<Parent>();
  const adapter: V2RouteAdapter<Parent> = {
    ...band,
    absence: () => ({ message: 'The served absence message.', title: 'Fictional page' }),
    loadParent: () => pending.promise,
    pageIssueClass: () => 'invariant',
    render: () => null,
    route: 'campaigns',
    temporal: (value) => value.temporal,
    windowUnavailable: unavailableParent,
  };
  const { container } = render(<PresentationV2RouteController adapter={adapter} sessionId="session-a" />);
  // The region exists, empty, before the message arrives (ARIA22).
  const region = container.querySelector(STATUS_REGION);
  assert.ok(region !== null);
  assert.equal(region.textContent, '');
  assert.equal(region.getAttribute('aria-atomic'), 'true');
  // The loading note keeps its own status role during loading, unchanged.
  assert.ok(container.querySelector('p.loading-note[role="status"]') !== null);

  await act(async () => pending.resolve(parent('absent', true)));
  await screen.findByRole('heading', { name: 'Fictional page' });
  assert.ok(container.querySelector(STATUS_REGION) === region, 'the status region was remounted');
  assert.deepEqual(Array.from(region.childNodes, (node) => node.textContent), ['Fictional page', '. ', 'The served absence message.']);
  assert.equal(container.querySelector('p.loading-note[role="status"]'), null);
  // The callout itself carries no live role: the persistent region announces it.
  const callout = screen.getByRole('heading', { name: 'Fictional page' }).closest('section');
  assert.ok(callout !== null);
  assert.equal(callout.getAttribute('role'), null);
});

test('the status region keeps its instance across a page issue and Try again, and is empty in the issue', async () => {
  const loads = [deferred<Parent>(), deferred<Parent>()];
  let index = 0;
  const adapter: V2RouteAdapter<Parent> = {
    ...band,
    loadParent: () => loads[index++]!.promise,
    pageIssueClass: () => 'dependency',
    render: (state) => state.kind === 'parent_ready' ? <output aria-label="parent">{state.parent.id}</output> : null,
    route: 'campaigns',
    temporal: (value) => value.temporal,
    windowUnavailable: unavailableParent,
  };
  const user = userEvent.setup();
  const { container } = render(<PresentationV2RouteController adapter={adapter} sessionId="session-a" />);
  const region = container.querySelector(STATUS_REGION);
  assert.ok(region !== null);

  await act(async () => loads[0]!.reject(new Error('fictional outage')));
  await screen.findByRole('alert');
  assert.ok(container.querySelector(STATUS_REGION) === region, 'the page issue remounted the status region');
  assert.equal(region.textContent, '');

  await user.click(screen.getByRole('button', { name: 'Try again' }));
  assert.ok(container.querySelector(STATUS_REGION) === region, 'Try again remounted the status region');
  await act(async () => loads[1]!.resolve(parent('recovered')));
  await screen.findByLabelText('parent');
  assert.ok(container.querySelector(STATUS_REGION) === region, 'the recovered page remounted the status region');
});

/** A served `window_unavailable` parent for one real adapter, with the test's temporal control. */
function absentParent(message: string): Parent & { readonly kind: 'window_unavailable'; readonly message: string } {
  return { ...parent('absent', true), kind: 'window_unavailable', message };
}

const D2_ABSENCES = [
  { adapter: reachRouteAdapter, route: 'reach', message: "Reach for September 2026 isn't ready to show yet. Other windows above are ready to view." },
  { adapter: audiencesRouteAdapter, route: 'audiences', message: "Audiences for September 2026 has more segment and campaign detail than this page can show. Ask the assistant about a DSP campaign to see its figures." },
  { adapter: retentionRouteAdapter, route: 'retention', message: "Retention isn't ready to show yet. It's built from your Amazon Marketing Cloud new-to-brand data." },
  { adapter: scorecardRouteAdapter, route: 'scorecard', message: "The Scorecard for this window isn't ready to show yet." },
  { adapter: newCustomersRouteAdapter, route: 'new-customers', message: "New customers for September 2026 isn't ready to show yet." },
] as const;

const BROWSER_ABSENCES = [
  {
    adapter: campaignRouteAdapter,
    message: 'Campaign reporting is unavailable for the selected period. Choose another available period to view campaign results.',
    title: 'Campaign portfolio is unavailable for this period',
  },
  {
    adapter: retailHealthRouteAdapter,
    message: 'Choose another published reporting period to view Retail Health.',
    title: 'Retail Health is unavailable for this period',
  },
] as const;

/** Render a real adapter's absence through the controller, the request served from a fixture. */
function renderAbsence(adapter: unknown, served: Parent) {
  // The tab's URL state is left out, so the fixture never reads or writes the test's location.
  const real = { ...(adapter as V2RouteAdapter<Parent>) };
  Reflect.deleteProperty(real, 'url');
  const fixture: V2RouteAdapter<Parent> = {
    ...real,
    loadParent: async () => served,
    temporal: () => served.temporal,
  };
  return render(<PresentationV2RouteController adapter={fixture} sessionId="session-a" />);
}

test('every D2 adapter renders its served absence through PageAbsence: title, message, no nested paragraph, no live role, axe-clean', async () => {
  const axe = await import('axe-core');
  for (const { adapter, message, route } of D2_ABSENCES) {
    const title = bindingByRoute[route].display_name;
    const { container, unmount } = renderAbsence(adapter, absentParent(message));
    const heading = await screen.findByRole('heading', { name: title });
    const callout = heading.closest('section');
    assert.ok(callout !== null, route);
    assert.equal(callout.getAttribute('role'), null, route);
    assert.equal(callout.querySelector('p')?.textContent, message, route);
    assert.equal(callout.querySelectorAll('p p').length, 0, `${route} nests a paragraph`);
    assert.equal(container.querySelector(STATUS_REGION)?.textContent, `${title}. ${message}`, route);
    const results = await axe.default.run(container, { rules: { 'color-contrast': { enabled: false } } });
    assert.deepEqual(results.violations.map((violation) => violation.id), [], route);
    unmount();
  }
}, 30_000);

test('Campaigns and Retail health render their existing browser copy unchanged through PageAbsence', async () => {
  for (const { adapter, message, title } of BROWSER_ABSENCES) {
    const { container, unmount } = renderAbsence(adapter, absentParent('unused'));
    const heading = await screen.findByRole('heading', { name: title });
    assert.equal(heading.closest('section')?.querySelector('p')?.textContent, message);
    // The retired container (an "Unavailable" badge in a mount-time status region) is gone.
    assert.equal(container.querySelector('.state-note'), null);
    unmount();
  }
});

const NO_PERIOD_ABSENCES = [
  {
    adapter: campaignRouteAdapter,
    message: "Campaign reporting isn't ready to show yet. It's built from your Amazon Ads data. When it's ready, you'll see the campaign results your data supports for each published window.",
    title: 'Campaign portfolio is unavailable for this period',
  },
  {
    adapter: retailHealthRouteAdapter,
    message: "Retail Health isn't ready to show yet. It's built from your Vendor Central data. When it's ready, you'll see the sales, traffic, conversion and returns your data supports, per product.",
    title: 'Retail Health is unavailable for this period',
  },
] as const;

test('with no period published, Campaigns and Retail health never ask the user to choose another period', async () => {
  const absent = absentParent('unused');
  const none: Parent = {
    ...absent,
    temporal: {
      ...absent.temporal,
      presets: absent.temporal.presets.map((preset) => ({ ...preset, availability: 'unavailable', reason_code: 'preset_unavailable' })),
    },
  };
  for (const { adapter, message, title } of NO_PERIOD_ABSENCES) {
    const { container, unmount } = renderAbsence(adapter, none);
    const heading = await screen.findByRole('heading', { name: title });
    const text = heading.closest('section')?.querySelector('p')?.textContent ?? '';
    assert.equal(text, message);
    assert.doesNotMatch(text, /Choose another/);
    assert.equal(container.querySelector(STATUS_REGION)?.textContent, `${title}. ${message}`);
    unmount();
  }
});

test('a window_unavailable parent never renders an empty main region, for every adapter', async () => {
  const adapters = [
    ...D2_ABSENCES.map(({ adapter, message }) => ({ adapter, message })),
    ...BROWSER_ABSENCES.map(({ adapter, message }) => ({ adapter, message })),
  ];
  assert.equal(adapters.length, 7);
  for (const { adapter, message } of adapters) {
    const { container, unmount } = renderAbsence(adapter, absentParent(message));
    await screen.findByText(message);
    const band_ = container.querySelector('.band');
    let main = band_?.nextElementSibling ?? null;
    const texts: string[] = [];
    for (; main !== null; main = main.nextElementSibling) texts.push(main.textContent?.trim() ?? '');
    assert.ok(texts.some((text) => text.length > 0), 'the main region below the band is blank');
    assert.ok(texts.join(' ').includes(message));
    unmount();
  }
});

test('a domain refusal shows the server text it carries, never another refusal\'s text, and PI-DOMAIN only without one', async () => {
  const serving = "This page's latest data is still being checked before it's shown. Try again later.";
  const noPeriod = 'No reporting period is available for this page.';
  document.cookie = 'adl_csrf=csrf; path=/';
  const rendered: string[] = [];
  for (const error of [serving, noPeriod]) {
    Object.defineProperty(globalThis, 'fetch', {
      configurable: true,
      value: async () => new Response(JSON.stringify({
        code: 'ADLY-PRESENTATION-0002',
        error: `  ${error}  `,
        error_class: 'domain',
      }), { status: 409 }),
    });
    const adapter: V2RouteAdapter<Parent> = {
      ...band,
      loadParent: async (session, selection, signal) => {
        await fetchReachPresentationV2(session, selection, signal);
        return parent('never');
      },
      pageIssueClass: (failure) => failure instanceof PresentationRequestError ? failure.errorClass : 'invariant',
      render: () => null,
      route: 'reach',
      temporal: (value) => value.temporal,
      windowUnavailable: unavailableParent,
    };
    const { unmount } = render(<PresentationV2RouteController adapter={adapter} sessionId="session-a" />);
    rendered.push((await screen.findByRole('alert')).querySelector('p')?.textContent ?? '');
    unmount();
  }
  assert.deepEqual(rendered, [serving, noPeriod]);
  assert.notEqual(rendered[0], rendered[1]);
  // The serving text is API-0002, which PI-DOMAIN shares; the other refusal is never PI-DOMAIN.
  assert.notEqual(rendered[1], PI_DOMAIN_FALLBACK);

  // Control: a domain error with no server text renders the PI-DOMAIN fallback.
  const fallback: V2RouteAdapter<Parent> = {
    ...band,
    loadParent: async () => { throw new PresentationRequestError('domain', 'ADLY-PRESENTATION-0002'); },
    pageIssueClass: () => 'domain',
    render: () => null,
    route: 'reach',
    temporal: (value) => value.temporal,
    windowUnavailable: unavailableParent,
  };
  render(<PresentationV2RouteController adapter={fallback} sessionId="session-a" />);
  assert.equal((await screen.findByRole('alert')).querySelector('p')?.textContent, PI_DOMAIN_FALLBACK);
});
