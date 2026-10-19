// SPDX-License-Identifier: MIT-0
'use client';

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from 'react';

import type { PageContextV1 } from '@/lib/page-context';
import { PresentationRequestError, type PresentationErrorClass } from '@/lib/presentation';
import { bindingByRoute, type RouteName, type TemporalPresetId } from '@/lib/page-bindings';
import type { AnyTemporalControl, PageGenerationV1, TemporalSelectionV1 } from '@/lib/presentation-v2';
import {
  childLoading,
  childReady,
  childUnavailable,
  pageIssue,
  parentLoading,
  parentReady,
  selectionUnavailableRefreshing,
  stateParent,
  temporalUnavailable,
  type PresentationRouteState,
} from '@/lib/presentation-route-state';
import { usePublishAssistantPage, type AssistantPage } from './AssistantPageContext';
import { PresetStepContext, type PresetStep } from './NextStepContext';
import { PageAbsence } from './PageAbsence';
import { PageError } from './StatePanels';
import { TemporalControl } from './TemporalControl';

export interface V2ChildRouteAdapter<Parent, Child, Command> {
  readonly load: (
    parent: Parent,
    command: Command,
    sessionId: string,
    signal: AbortSignal,
  ) => Promise<Child>;
  readonly selectionUnavailable: (error: unknown) => boolean;
  /**
   * ADR-120 decision 8: in a re-served parent, the one row that is the open entity, matched by
   * its safe display signature. Null when no row or more than one row matches: a failed refresh.
   */
  readonly rematch: (parent: Parent, child: Child) => Command | null;
  /**
   * ADR-125 decision 1: detail opens in place with one history entry, so the browser's Back
   * closes it. Moving between entities inside detail adds no entry.
   */
  readonly historyMarker?: boolean;
  /**
   * ADR-125 consequences: a window change re-serves the parent and re-opens the entity when
   * exactly one row matches it (`rematch`); otherwise detail closes and the page is told which
   * entity closed, so it can say why.
   */
  readonly reopenAfterParentChange?: boolean;
}

/**
 * A tab's URL state (ia §8.1): governed ids only, never a selection or reference. An absent
 * parameter is the default, so a default is never written.
 */
export interface V2RouteUrlState<ParentIntent> {
  readonly read: (params: URLSearchParams) => {
    readonly intent: ParentIntent;
    readonly selection: TemporalSelectionV1 | undefined;
  };
  readonly write: (selection: TemporalSelectionV1 | undefined, intent: ParentIntent) => URLSearchParams;
  /**
   * The intent the served parent stands for. A parent served in place of a refused value (a
   * channel the window doesn't offer, shown as All channels) drops it, so the URL and every
   * later request read as omitted (ia §8.1). Returns the same object when nothing changed.
   */
  readonly served?: (parent: unknown, intent: ParentIntent) => ParentIntent;
}

/** Whether a path is this tab's own route, so a URL write never lands on another tab's entry. */
export function isRoutePath(pathname: string, route: RouteName): boolean {
  const binding = bindingByRoute[route];
  const trim = (path: string) => (path.length > 1 ? path.replace(/\/+$/, '') : path);
  return [binding.route, ...binding.compatibility_routes].some((path) => trim(path) === trim(pathname));
}

/**
 * A page-level absence (ADR-139): the served title and message `PageAbsence` renders. Required
 * of every adapter and typed on the `window_unavailable` parent its predicate narrows to, so a
 * page in that state can never render an empty main region.
 */
export interface PageAbsenceCopy {
  readonly title: string;
  readonly message: string;
}

export interface V2RouteAdapter<
  Parent,
  Child = never,
  Command = never,
  ParentIntent = undefined,
  Absent extends Parent = Parent,
> {
  /**
   * The page-level absence for a `window_unavailable` parent. The controller calls it only in
   * that state, guarded by `windowUnavailable`, and renders `PageAbsence`; `render` never runs
   * there.
   */
  readonly absence: (parent: Absent) => PageAbsenceCopy;
  /** The tab's URL state; a tab without one keeps its state out of the URL. */
  readonly url?: V2RouteUrlState<ParentIntent>;
  /**
   * Title-band content (eyebrow, heading, scope pills) rendered beside the window
   * controls. Receives the same child/intent actions as render, so a band can carry
   * in-place navigation (the campaign switcher) without a second action channel.
   */
  readonly bandTitle: (
    state: PresentationRouteState<Parent, Child, Command>,
    requestChild: (command: Command) => void,
    replaceParentIntent: (intent: ParentIntent) => void,
  ) => ReactNode;
  /** Title-band skeleton reserving the heading position while the parent resolves. */
  readonly bandTitleLoading: () => ReactNode;
  readonly child?: V2ChildRouteAdapter<Parent, Child, Command>;
  readonly initialParentIntent?: ParentIntent;
  /** Clears module state a previous visit to this tab left; runs before the first parent load. */
  readonly onMount?: () => void;
  readonly loadParent: (
    sessionId: string,
    selection: TemporalSelectionV1 | undefined,
    signal: AbortSignal,
    intent: ParentIntent,
  ) => Promise<Parent>;
  readonly pageIssueClass: (error: unknown) => PresentationErrorClass;
  /**
   * The served page context the assistant uses (ADR-120), or null for a page-level absence
   * state (ia §8.1), which offers no context and no starter questions.
   */
  readonly pageContext: (parent: Parent, child: Child | null) => PageContextV1 | null;
  readonly render: (
    state: PresentationRouteState<Parent, Child, Command>,
    requestChild: (command: Command) => void,
    clearChild: () => void,
    replaceParentIntent: (intent: ParentIntent) => void,
    sessionId?: string,
    /** The entity a window change closed because no single row matched it, or null. */
    closedChild?: Child | null,
  ) => ReactNode;
  readonly renderLoading?: () => ReactNode;
  /**
   * The window control's loading shape before the first response supplies a control, so the
   * band keeps its place instead of shifting when the control arrives (mockup-loading.html).
   */
  readonly renderLoadingControls?: () => ReactNode;
  /** False for a single-preset page whose scope band owns the served month. */
  readonly renderTemporalControl?: boolean;
  readonly route: RouteName;
  /** The parent's served generation, printed in the as-of footnote (S6 §2.9). */
  readonly generation?: (parent: Parent) => PageGenerationV1 | undefined;
  readonly temporal: (parent: Parent) => AnyTemporalControl;
  readonly temporalRefusal?: (
    parent: Parent,
  ) => { readonly reason: string; readonly recovery: string } | null;
  /** The only state predicate: a parent the server served as a page-level absence. */
  readonly windowUnavailable: (parent: Parent) => parent is Absent;
}

/**
 * Catalog PI-DOMAIN (`docs/ui/page-absence-copy.md`): the `domain` page text when a refusal
 * carries no server `error` text. No current path takes it; every `domain` refusal today shows
 * its own validated server text.
 */
export const PI_DOMAIN_FALLBACK = "This page's latest data is still being checked before it's shown. Try again later.";

/** The served `error` text of a `domain` refusal (temporal contract §8 rule 4), else null. */
function domainMessage(error: unknown): string | null {
  return error instanceof PresentationRequestError && error.errorClass === 'domain' ? error.serverMessage : null;
}

/** A page error keeps the hub header above it and offers Try again (ia §7.9). */
function PageIssue({ issueClass, onRetry, serverMessage }: {
  readonly issueClass: PresentationErrorClass;
  readonly onRetry: () => void;
  readonly serverMessage: string | null;
}) {
  const message = issueClass === 'dependency'
    ? 'This page is temporarily unavailable. Try again when its source is available.'
    : issueClass === 'validation'
      ? 'This page request is no longer available. Refresh the page before selecting again.'
      : issueClass === 'domain'
        ? serverMessage ?? PI_DOMAIN_FALLBACK
        : 'This page cannot be shown safely right now.';
  return <PageError message={message} onRetry={onRetry} />;
}

interface ChildRequest<Parent, Command> {
  readonly command: Command;
  readonly parent: Parent;
  readonly sessionId: string;
}

export function PresentationV2RouteController<
  Parent,
  Child = never,
  Command = never,
  ParentIntent = undefined,
  Absent extends Parent = Parent,
>({
  adapter,
  sessionId,
}: {
  readonly adapter: V2RouteAdapter<Parent, Child, Command, ParentIntent, Absent>;
  readonly sessionId: string;
}) {
  // A link to a preset or channel loads it directly: the first request reads the URL (ia §8.1).
  // The export renders no window-dependent markup before the first response, so hydration
  // matches whatever the URL holds.
  const [initialUrl] = useState(() => (
    adapter.url === undefined || typeof window === 'undefined'
      ? null
      : adapter.url.read(new URLSearchParams(window.location.search))
  ));
  const [selection, setSelection] = useState<TemporalSelectionV1 | undefined>(initialUrl?.selection);
  // A layout effect runs before every passive one, so the first load never reads a past visit.
  const { onMount } = adapter;
  useLayoutEffect(() => { onMount?.(); }, [onMount]);
  const [parentIntent, setParentIntent] = useState<ParentIntent>(
    initialUrl === null ? adapter.initialParentIntent as ParentIntent : initialUrl.intent,
  );
  const [parentRequestRevision, setParentRequestRevision] = useState(0);
  const [restoreTemporalFocus, setRestoreTemporalFocus] = useState(false);
  const [childRequest, setChildRequest] = useState<ChildRequest<Parent, Command> | null>(null);
  const [state, setState] = useState<PresentationRouteState<Parent, Child, Command>>(() => parentLoading());
  const parentGeneration = useRef(0);
  const childGeneration = useRef(0);
  const activeParentRequest = useRef<AbortController | null>(null);
  const activeChildRequest = useRef<AbortController | null>(null);
  const [retainedTemporal, setRetainedTemporal] = useState<AnyTemporalControl | null>(null);
  // Retained beside the window so the footnote keeps the served build time while a new window loads.
  const [retainedGeneration, setRetainedGeneration] = useState<PageGenerationV1 | undefined>(undefined);
  // ADR-125: the entity a window change should re-open once the parent re-serves, and the one
  // it closed because no single row matched. Refs only ever change in effects and handlers.
  const [reopenChild, setReopenChild] = useState<Child | null>(null);
  const [closedChild, setClosedChild] = useState<Child | null>(null);
  const reopenRef = useRef<Child | null>(null);
  const markerActive = useRef(false);
  const markerPopped = useRef(false);
  const committedState = useRef<PresentationRouteState<Parent, Child, Command>>(parentLoading());
  const writeUrl = useCallback(() => {
    // Back may land on another tab's entry before this tab unmounts; that entry keeps its URL.
    if (adapter.url === undefined || !isRoutePath(window.location.pathname, adapter.route)) return;
    const query = adapter.url.write(selection, parentIntent).toString();
    const next = `${window.location.pathname}${query === '' ? '' : `?${query}`}`;
    if (next !== `${window.location.pathname}${window.location.search}`) {
      // A preset or filter change replaces the entry and keeps its state object (ia §8.1).
      window.history.replaceState(window.history.state, '', next);
    }
  }, [adapter.route, adapter.url, parentIntent, selection]);
  useEffect(() => { writeUrl(); }, [writeUrl]);
  // The intent a served parent dropped (a refused channel shown as All channels): adopting it
  // must not re-request the parent already on screen.
  const adoptedIntent = useRef<ParentIntent | null>(null);

  const invalidateChildren = useCallback(() => {
    childGeneration.current += 1;
    activeChildRequest.current?.abort();
    activeChildRequest.current = null;
  }, []);

  const replaceParent = useCallback((
    nextSelection: TemporalSelectionV1 | undefined,
    shouldRestoreFocus = false,
    nextIntent: ParentIntent = parentIntent,
  ) => {
    parentGeneration.current += 1;
    activeParentRequest.current?.abort();
    activeParentRequest.current = null;
    invalidateChildren();
    setChildRequest(null);
    setRestoreTemporalFocus(shouldRestoreFocus);
    setClosedChild(null);
    const current = committedState.current;
    setReopenChild(
      adapter.child?.reopenAfterParentChange === true && current.kind === 'child_ready' ? current.child : null,
    );
    setState(parentLoading());
    setSelection(nextSelection);
    setParentIntent(nextIntent);
    setParentRequestRevision((revision) => revision + 1);
  }, [adapter.child, invalidateChildren, parentIntent]);

  // Declared before the parent load, so the load's callback reads this commit's value.
  useEffect(() => {
    reopenRef.current = reopenChild;
  }, [reopenChild]);
  useLayoutEffect(() => {
    committedState.current = state;
  }, [state]);

  useEffect(() => {
    if (adoptedIntent.current !== null && adoptedIntent.current === parentIntent) {
      adoptedIntent.current = null;
      return undefined;
    }
    adoptedIntent.current = null;
    const controller = new AbortController();
    activeParentRequest.current?.abort();
    activeParentRequest.current = controller;
    invalidateChildren();
    const ticket = ++parentGeneration.current;

    void adapter.loadParent(sessionId, selection, controller.signal, parentIntent)
      .then((parent) => {
        if (controller.signal.aborted || ticket !== parentGeneration.current) return;
        const served = adapter.url?.served?.(parent, parentIntent) ?? parentIntent;
        if (served !== parentIntent) {
          adoptedIntent.current = served;
          setParentIntent(served);
        }
        const reopen = reopenRef.current;
        reopenRef.current = null;
        setReopenChild(null);
        if (adapter.windowUnavailable(parent)) {
          setState(temporalUnavailable(parent));
          return;
        }
        const command = reopen === null ? null : adapter.child?.rematch(parent, reopen) ?? null;
        if (command !== null) {
          setState(childLoading(parent, command));
          setChildRequest({ command, parent, sessionId });
          return;
        }
        if (reopen !== null) setClosedChild(reopen);
        setState(parentReady(parent));
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted || ticket !== parentGeneration.current) return;
        setState(pageIssue(adapter.pageIssueClass(error), domainMessage(error)));
      });

    return () => {
      controller.abort();
      if (activeParentRequest.current === controller) activeParentRequest.current = null;
    };
  }, [adapter, invalidateChildren, parentIntent, parentRequestRevision, selection, sessionId]);

  useEffect(() => () => {
    activeParentRequest.current?.abort();
    activeChildRequest.current?.abort();
  }, []);

  const parent = stateParent(state);
  const requestChild = useCallback((command: Command) => {
    if (adapter.child === undefined || parent === null) return;
    setClosedChild(null);
    setState(childLoading(parent, command));
    setChildRequest({ command, parent, sessionId });
  }, [adapter.child, parent, sessionId]);

  // ADR-125 decision 1: one history entry while detail is open. Opening detail pushes it; any
  // close the page makes (Back to …, a window change with no match, a refresh) consumes it; the
  // browser's Back consumes it itself, and closes detail.
  // A window change that may re-open the entity keeps the entry while the parent re-serves.
  const detailOpen = state.kind === 'child_loading' || state.kind === 'child_ready' ||
    state.kind === 'child_unavailable' || (state.kind === 'parent_loading' && reopenChild !== null);
  const usesMarker = adapter.child?.historyMarker === true;
  useEffect(() => {
    if (!usesMarker) return;
    if (detailOpen && !markerActive.current) {
      markerActive.current = true;
      window.history.pushState({ adlyticsDetail: true }, '');
    } else if (!detailOpen && markerActive.current) {
      markerActive.current = false;
      if (markerPopped.current) markerPopped.current = false;
      else window.history.back();
    }
  }, [detailOpen, usesMarker]);

  const clearChild = useCallback(() => {
    setChildRequest(null);
    setState((currentState) => {
      const currentParent = stateParent(currentState);
      return currentParent === null ? currentState : parentReady(currentParent);
    });
  }, []);

  useEffect(() => {
    if (!usesMarker) return undefined;
    const onPopState = () => {
      // The entry landed on takes the screen's preset and filter, so the URL matches it.
      writeUrl();
      if (!markerActive.current) return;
      markerPopped.current = true;
      invalidateChildren();
      clearChild();
    };
    window.addEventListener('popstate', onPopState);
    return () => window.removeEventListener('popstate', onPopState);
  }, [clearChild, invalidateChildren, usesMarker, writeUrl]);

  const requestParentIntent = useCallback((nextIntent: ParentIntent) => {
    setChildRequest(null);
    setRestoreTemporalFocus(false);
    setState(parentLoading());
    setParentIntent(nextIntent);
    setParentRequestRevision((revision) => revision + 1);
  }, []);

  useEffect(() => {
    if (childRequest === null || childRequest.sessionId !== sessionId) return;
    const child = adapter.child;
    if (child === undefined) return;

    activeChildRequest.current?.abort();
    const controller = new AbortController();
    activeChildRequest.current = controller;
    const ticket = ++childGeneration.current;
    const { command, parent: childParent } = childRequest;

    void child.load(childParent, command, sessionId, controller.signal)
      .then((result) => {
        if (controller.signal.aborted || ticket !== childGeneration.current) return;
        setState(childReady(childParent, result, command));
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted || ticket !== childGeneration.current) return;
        if (child.selectionUnavailable(error)) {
          setState(selectionUnavailableRefreshing(childParent));
          setRetainedTemporal(adapter.temporal(childParent));
          setRetainedGeneration(adapter.generation?.(childParent));
          replaceParent(selection);
          return;
        }
        setState(childUnavailable(childParent, command, adapter.pageIssueClass(error)));
      });

    return () => {
      childGeneration.current += 1;
      controller.abort();
      if (activeChildRequest.current === controller) activeChildRequest.current = null;
    };
  }, [adapter, childRequest, replaceParent, selection, sessionId]);

  // ADR-120 decision 8: re-request the response on screen and take its fresh reference. A
  // parent re-serves at the same preset, sort and segment; detail re-serves its parent, finds
  // the open entity by its signature and re-requests detail. Detail stays on screen if not.
  const stateRef = useRef(state);
  useLayoutEffect(() => {
    stateRef.current = state;
  }, [state]);
  const refreshPageContext = useCallback(async (): Promise<PageContextV1 | null> => {
    const current = stateRef.current;
    const ticket = parentGeneration.current;
    const signal = new AbortController().signal;
    try {
      const freshParent = await adapter.loadParent(sessionId, selection, signal, parentIntent);
      if (ticket !== parentGeneration.current || adapter.windowUnavailable(freshParent)) return null;
      if (current.kind !== 'child_ready') {
        if (current.kind !== 'parent_ready') return null;
        setState(parentReady(freshParent));
        return adapter.pageContext(freshParent, null);
      }
      const child = adapter.child;
      const command = child?.rematch(freshParent, current.child) ?? null;
      if (child === undefined || command === null) return null;
      const freshChild = await child.load(freshParent, command, sessionId, signal);
      if (ticket !== parentGeneration.current) return null;
      invalidateChildren();
      setState(childReady(freshParent, freshChild, command));
      return adapter.pageContext(freshParent, freshChild);
    } catch {
      return null;
    }
  }, [adapter, invalidateChildren, parentIntent, selection, sessionId]);

  const assistantPage = useMemo((): AssistantPage => {
    if (state.kind === 'page_issue' || state.kind === 'window_unavailable') return { kind: 'none' };
    if (state.kind === 'parent_ready' || state.kind === 'child_ready' || state.kind === 'child_unavailable') {
      const context = adapter.pageContext(state.parent, state.kind === 'child_ready' ? state.child : null);
      return context === null ? { kind: 'none' } : { kind: 'page', context, refresh: refreshPageContext };
    }
    return { kind: 'loading' };
  }, [adapter, refreshPageContext, state]);
  usePublishAssistantPage(assistantPage);

  const temporal = parent === null ? retainedTemporal : adapter.temporal(parent);
  const generation = parent === null ? retainedGeneration : adapter.generation?.(parent);
  const loading = state.kind === 'parent_loading';
  const windowControls = temporal === null || adapter.renderTemporalControl === false ? null : (
    <TemporalControl
      control={temporal}
      defaultPreset={bindingByRoute[adapter.route].temporal_policy?.default_preset}
      generation={generation}
      onFocusRestored={() => setRestoreTemporalFocus(false)}
      onSelect={(nextSelection) => {
        setRetainedTemporal(temporal);
        setRetainedGeneration(generation);
        replaceParent(nextSelection, true);
      }}
      pending={loading || state.kind === 'selection_unavailable_refreshing'}
      refusal={parent === null ? null : adapter.temporalRefusal?.(parent) ?? null}
      restoreFocus={restoreTemporalFocus}
    />
  );

  // ADR-139: the one place `absence` is called, guarded by the same predicate that set the state.
  const absent = state.kind === 'window_unavailable' && adapter.windowUnavailable(state.parent)
    ? adapter.absence(state.parent)
    : null;
  // One persistent status region (temporal contract §6): the same keyed element is the first
  // child of the top-level fragment in every return, so React keeps one instance and the region
  // exists, empty, before an absence message arrives (WCAG 4.1.3, ARIA22).
  const statusText = absent === null ? null : <>{absent.title}{'. '}{absent.message}</>;
  const status = (
    <p key="page-status" className="sr-only" role="status" aria-atomic="true">{statusText}</p>
  );

  if (state.kind === 'page_issue') {
    return <>{status}<PageIssue issueClass={state.issueClass} serverMessage={state.serverMessage} onRetry={() => replaceParent(selection)} /></>;
  }
  if (!loading && parent === null) {
    return <>{status}<PageIssue issueClass="invariant" serverMessage={null} onRetry={() => replaceParent(selection)} /></>;
  }

  const replaceParentIntent = (intent: ParentIntent) => {
    setRetainedTemporal(temporal);
    setRetainedGeneration(generation);
    requestParentIntent(intent);
  };
  // An `open_preset` step selects a preset exactly as the date control does, and only one the
  // served control offers as available (ADR-122 decision 5).
  const presetStep: PresetStep = (presetId) => {
    const presets: readonly { readonly availability: string; readonly id: TemporalPresetId }[] =
      temporal !== null && 'presets' in temporal ? temporal.presets : [];
    const preset = temporal?.mode === 'selectable'
      ? presets.find((item) => item.id === presetId && item.availability === 'available')
      : undefined;
    if (preset === undefined || temporal === null) return null;
    return () => {
      setRetainedTemporal(temporal);
      setRetainedGeneration(generation);
      replaceParent({ kind: 'preset', preset_id: preset.id }, true);
    };
  };
  const bandTitle = loading ? adapter.bandTitleLoading() : adapter.bandTitle(state, requestChild, replaceParentIntent);
  let content: ReactNode;
  if (loading) {
    content = adapter.renderLoading?.() ?? <p role="status" className="loading-note"><span className="spin" aria-hidden="true" />Resolving the latest available reporting window…</p>;
  } else if (state.kind === 'window_unavailable') {
    // `absent` is always set here: the state is entered only after the same predicate held.
    content = absent === null
      ? <PageIssue issueClass="invariant" serverMessage={null} onRetry={() => replaceParent(selection)} />
      : <PageAbsence {...absent} />;
  } else {
    content = adapter.render(
      state,
      requestChild,
      clearChild,
      replaceParentIntent,
      sessionId,
      closedChild,
    );
  }
  return (
    <>
      {status}
      <PresetStepContext.Provider value={presetStep}>
        <div className={bandTitle === null ? 'band band-bare' : 'band'}>
          {bandTitle === null ? null : <div className="band-title">{bandTitle}</div>}
          {windowControls ?? (loading ? adapter.renderLoadingControls?.() ?? null : null)}
        </div>
        {content}
      </PresetStepContext.Provider>
    </>
  );
}
