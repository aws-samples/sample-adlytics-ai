// SPDX-License-Identifier: MIT-0
'use client';
import { useCallback, useEffect, useId, useRef, useState, useSyncExternalStore, type FormEvent } from 'react';
import { formatCanonicalDecimal, formatCount, formatCurrency, formatIsoDate, formatIsoInstantShort } from '@/lib/exact-format';
import {
  asinErrorText,
  askQuestions,
  currentExampleAsin,
  displayTargets,
  exampleAsinFrom,
  fetchDemandForecastDetail,
  fetchDemandForecastList,
  fetchDemandForecastStatus,
  FORECAST_START,
  forecastHistoryState,
  forecastSearch,
  hasDigit,
  isAsin,
  itemCountOf,
  filterTargetCopy,
  INFO,
  listAnswer,
  looksLikeName,
  lowerFirst,
  measureCopy,
  modelCopy,
  modelNames,
  moneyTargets,
  nameErrorText,
  normalizedAsin,
  notInForecastInlineText,
  plural,
  readForecastEntry,
  readForecastLink,
  skipReasonCopy,
  skipReasons,
  startCopy,
  weekStartSentenceCopy,
  type DemandForecastDetail,
  type DemandForecastList,
  type DemandForecastStatus,
  type ExampleAsin,
  type FetchResult,
  type ForecastEntry,
  type ListEntry,
  type ListQuery,
  type SkipReason,
  type Target,
  type ViewKey,
} from '@/lib/demand-forecast';
import { useOpenEntity } from './AppShell';
import { AnswerSentence } from './AnswerSentence';
import { seedAssistantQuestion } from './ChatPanel';
import { DataTable, DtBody, DtCell, DtColumn, DtHead, DtRow, DtRowHeader } from './DataTable';
import { DemandForecastAsinDetail } from './DemandForecastAsinDetail';
import {
  DEMAND_FORECAST_NO_FORECAST_TITLE,
  DEMAND_FORECAST_READ_FAILED_TEXT,
  FreshnessPill,
  noForecastYetText,
  ScopeCard,
  StaleBanner,
  WeekControl,
  readyStatus,
  type Loaded,
} from './DemandForecastScope';
import { SearchBox, StartSkeleton, StartView } from './DemandForecastStart';
import { PAGE_HEADING_ID } from './Dialog';
import { InfoTip } from './InfoTip';
import { PageAbsence } from './PageAbsence';
import { PageError, StateCallout } from './StatePanels';

export type { Loaded } from './DemandForecastScope';
export { DEMAND_FORECAST_NO_FORECAST_TITLE, DEMAND_FORECAST_READ_FAILED_TEXT, noForecastYetText } from './DemandForecastScope';

/**
 * The Demand forecast tab (ADR-163; forecasting LLD "Demand forecast page"; hybrid design). The
 * shell draws the one h1. The page opens on the answer, "Check these first" and the one search
 * box (H1); the full list is Browse all (H4); a product shows alone (H3). The start list is always
 * the unfiltered page 1 and feeds the opening view; Browse all's filter and page never replace it
 * (O-01). The lists, status and detail each name the generation they read; when a publish lands
 * between requests and they disagree, none of them renders, and Show the latest forecast refetches
 * them together. An aborted request is not an error. Every figure is a served display string.
 */

interface Keyed<T> { readonly key: string; readonly loaded: Loaded<T> }

/** What opened a view, so closing it returns focus there (G-09, B-09). */
type Opener =
  | { readonly kind: 'row'; readonly asin: string }
  | { readonly kind: 'search' }
  | { readonly kind: 'browse' }
  | { readonly kind: 'reason'; readonly reason: SkipReason };
/**
 * Where focus goes once the view it names has its list (or none to wait for). Only a closing
 * goal may take focus from the Back control, which leaves with the closed view.
 */
interface FocusGoal { readonly to: Opener | null; readonly wait: 'start' | 'browse' | null; readonly closing: boolean }
/** The search box's inline outcome (H2), drawn on its own line under the card header (B-08). */
type SearchOutcome =
  | { readonly kind: 'asin' }
  | { readonly kind: 'name' }
  | { readonly kind: 'notin'; readonly asin: string; readonly noDigit: boolean }
  | { readonly kind: 'unavailable' }
  | { readonly kind: 'failed'; readonly asin: string }
  | { readonly kind: 'link' };
interface PendingSearch { readonly asin: string; readonly id: number; readonly controller: AbortController }

const MIXED_TEXT = 'A newer forecast was published while this page loaded.';
const PAGE1_TEXT = 'The forecast was updated, so the list is back on page 1.';
const ALL: ForecastEntry = { view: 'all', asin: null, from: 'start', depth: 1 };
const START_QUERY: ListQuery = { status: null, page: 1 };

function isAbort(error: unknown): boolean {
  return error instanceof DOMException && error.name === 'AbortError';
}
function productEntry(asin: string, from: 'start' | 'all'): ForecastEntry {
  return { view: 'product', asin, from, depth: from === 'all' ? 2 : 1 };
}

/** The generation each ready result read, `null` for none published; mixed results disagree. */
function mixedGenerations(
  lists: readonly Loaded<DemandForecastList>[],
  status: Loaded<DemandForecastStatus>,
  detail: Loaded<DemandForecastDetail | null> | null,
): boolean {
  const read = new Set<string | null>();
  for (const list of lists) if (list.state === 'ready') read.add(list.value.provenance?.generation_id ?? null);
  if (status.state === 'ready') read.add(status.value.generation?.generation_id ?? null);
  if (detail?.state === 'ready' && detail.value !== null) read.add(detail.value.provenance?.generation_id ?? null);
  return read.size > 1;
}

function settle<T>(result: FetchResult<T>): Loaded<T> {
  return result.kind === 'ok' ? { state: 'ready', value: result.value } : { state: 'failed' };
}
/** A list the page can show: complete, with provenance and a horizon. */
function completeList(list: Loaded<DemandForecastList>): DemandForecastList | null {
  return list.state === 'ready' && list.value.outcome === 'complete' && list.value.provenance !== null && list.value.horizon !== null
    ? list.value
    : null;
}

/** Focus fell to the document because the control that held it left with the view. */
function focusLost(): boolean {
  return document.activeElement === null || document.activeElement === document.body;
}
/**
 * Focus is on the shell's Back control, which leaves with the view: in a browser the frame after
 * the view closes can run before the shell drops it, so focus would fall to the body (891bf4df).
 */
function focusOnBack(): boolean {
  return document.activeElement?.closest('.hub-back') != null;
}
/** Focus may move: it was lost, or is on the h1, the crumb or tab link, or the Back control of a closing view. */
function focusMovable(closing: boolean): boolean {
  const active = document.activeElement;
  return focusLost() || (closing && focusOnBack()) || active?.id === PAGE_HEADING_ID || active?.closest('.crumb, .tabs') != null;
}
function openerElement(opener: Opener, search: HTMLInputElement | null): HTMLElement | null {
  switch (opener.kind) {
    case 'row': return document.querySelector<HTMLElement>(`button[data-forecast-row="${opener.asin}"]`);
    case 'search': return search;
    case 'browse': return document.querySelector<HTMLElement>('a[data-forecast-browse]');
    case 'reason': return document.querySelector<HTMLElement>(`button[data-forecast-reason="${opener.reason}"]`)
      ?? document.querySelector<HTMLElement>('a[data-forecast-browse]');
  }
}

/** What a page load shows: the view its URL names, and whether history needs writes (B-04). */
interface Boot { readonly entry: ForecastEntry; readonly seed: boolean; readonly invalid: boolean }
function bootRoute(): Boot {
  const link = readForecastLink(window.location.search);
  if (link === 'invalid') return { entry: FORECAST_START, seed: false, invalid: true };
  const saved = readForecastEntry(window.history.state);
  // A reload keeps its entry, so `from` and `depth` still give the Back label and target.
  if (saved !== null && saved.view === link.view && saved.asin === link.asin) return { entry: saved, seed: false, invalid: false };
  return { entry: link, seed: link.view !== 'start', invalid: false };
}
const subscribeNever = () => () => undefined;
/** False while the static export hydrates, so the first render matches the prerendered H1. */
function useHydrated(): boolean {
  return useSyncExternalStore(subscribeNever, () => true, () => false);
}

export function DemandForecastView() {
  // The URL is read once, on the client's first render; nothing it names renders until hydrated.
  const [boot] = useState<Boot | null>(() => (typeof window === 'undefined' ? null : bootRoute()));
  const hydrated = useHydrated();
  const [entry, setEntry] = useState<ForecastEntry>(boot?.entry ?? FORECAST_START);
  const [browseQuery, setBrowseQuery] = useState<ListQuery>(START_QUERY);
  const [startResult, setStartResult] = useState<Keyed<DemandForecastList> | null>(null);
  const [browseResult, setBrowseResult] = useState<Keyed<DemandForecastList> | null>(null);
  const [statusResult, setStatusResult] = useState<Keyed<DemandForecastStatus> | null>(null);
  const [detailResult, setDetailResult] = useState<Keyed<DemandForecastDetail | null> | null>(null);
  const [search, setSearch] = useState('');
  const [outcome, setOutcome] = useState<SearchOutcome | null>(boot?.invalid === true ? { kind: 'link' } : null);
  const [pendingAsin, setPendingAsin] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [viewWeeks, setViewWeeks] = useState<number | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [statusAttempt, setStatusAttempt] = useState(0);
  const [example, setExample] = useState<ExampleAsin | null>(null);
  const [focusTick, setFocusTick] = useState(0);
  const searchInput = useRef<HTMLInputElement>(null);
  const pending = useRef<PendingSearch | null>(null);
  const requestId = useRef(0);
  // A link lands focus on its view's h1; an invalid one in the search box (B-04).
  const focusGoal = useRef<FocusGoal | null>(boot === null || (!boot.invalid && boot.entry.view === 'start')
    ? null
    : { to: boot.invalid ? { kind: 'search' } : null, wait: null, closing: false });
  const openers = useRef<{ start?: Opener; all?: Opener }>({});
  const tabJump = useRef(false);
  const focusFrame = useRef<number | null>(null);
  const searchId = useId();
  const outcomeId = `${searchId}-outcome`;
  // Each result carries the request it answers, so a newer request reads as loading until its
  // own result lands, and no effect sets state before its fetch settles.
  const statusKey = `${attempt}|${statusAttempt}`;
  const startKey = `|1|${attempt}`;
  const browseIsStart = browseQuery.status === null && browseQuery.page === 1;
  const browseKey = `${browseQuery.status ?? ''}|${browseQuery.page}|${attempt}`;
  const detailKey = entry.view === 'product' && entry.asin !== null ? `${entry.asin}|${attempt}` : null;
  const browseWanted = entry.view === 'all' && !browseIsStart;
  const haveBrowse = browseResult?.key === browseKey;
  const haveDetail = detailKey !== null && detailResult?.key === detailKey;

  useEffect(() => {
    const controller = new AbortController();
    fetchDemandForecastStatus(controller.signal)
      .then((result) => setStatusResult({ key: statusKey, loaded: settle(result) }))
      .catch((error: unknown) => { if (!isAbort(error)) setStatusResult({ key: statusKey, loaded: { state: 'failed' } }); });
    return () => controller.abort();
  }, [statusKey]);

  useEffect(() => {
    const controller = new AbortController();
    fetchDemandForecastList(START_QUERY, controller.signal)
      .then((result) => setStartResult({ key: startKey, loaded: settle(result) }))
      .catch((error: unknown) => { if (!isAbort(error)) setStartResult({ key: startKey, loaded: { state: 'failed' } }); });
    return () => controller.abort();
  }, [startKey]);

  useEffect(() => {
    // Browse all asks only for a filter or a later page, and only once per key: unfiltered page 1
    // is the start list, and a stored result serves a return from a product (O-01).
    if (!browseWanted || haveBrowse) return undefined;
    const controller = new AbortController();
    const query = browseQuery;
    const load = async () => {
      const result = await fetchDemandForecastList(query, controller.signal);
      if (result.kind === 'refused' && result.refusal.refused === 'PAGE_INVALID' && query.page !== 1) {
        // A publish changed the page count since the last list: back to page 1, which refetches
        // once. Page 1 always exists, so a second PAGE_INVALID shows the error panel.
        setNotice(PAGE1_TEXT);
        setBrowseQuery({ status: query.status, page: 1 });
        return;
      }
      setBrowseResult({ key: browseKey, loaded: settle(result) });
    };
    load().catch((error: unknown) => { if (!isAbort(error)) setBrowseResult({ key: browseKey, loaded: { state: 'failed' } }); });
    return () => controller.abort();
  }, [browseKey, browseQuery, browseWanted, haveBrowse]);

  const start: Loaded<DemandForecastList> = startResult?.key === startKey ? startResult.loaded : { state: 'loading' };
  const browse: Loaded<DemandForecastList> = browseIsStart
    ? start
    : browseResult?.key === browseKey ? browseResult.loaded : { state: 'loading' };
  const status: Loaded<DemandForecastStatus> = statusResult?.key === statusKey ? statusResult.loaded : { state: 'loading' };
  const detail: Loaded<DemandForecastDetail | null> | null = detailKey === null
    ? null
    : detailResult?.key === detailKey ? detailResult.loaded : { state: 'loading' };

  /** Abort the pending search; its late answer, if any, is ignored (B-03). */
  const abortSearch = useCallback(() => {
    if (pending.current === null) return;
    pending.current.controller.abort();
    pending.current = null;
    requestId.current += 1;
    setPendingAsin(null);
  }, []);
  useEffect(() => () => pending.current?.controller.abort(), []);

  const moveFocus = useCallback((goal: FocusGoal) => {
    focusGoal.current = goal;
    setFocusTick((value) => value + 1);
  }, []);
  /**
   * Open a view from the one on screen as a pushed history entry with its own URL (O-03), so the
   * browser's Back closes it. The opener is where focus returns when it closes.
   */
  const navigate = useCallback((next: ForecastEntry, opener: Opener) => {
    abortSearch();
    if (entry.view === 'start') openers.current.start = opener;
    if (entry.view === 'all') openers.current.all = opener;
    window.history.pushState(forecastHistoryState(window.history.state, next), '', `${window.location.pathname}${forecastSearch(next)}`);
    // An invalid link's notice is about the load; once the planner moves on it no longer applies.
    setOutcome((current) => (current?.kind === 'link' ? null : current));
    setEntry(next);
    moveFocus({ to: null, wait: null, closing: false });
  }, [abortSearch, entry.view, moveFocus]);
  /** Closing is always the browser's Back: the entry under every view is this page's (seeded). */
  const close = useCallback(() => window.history.back(), []);
  /** The crumb's and the active tab's "Demand forecast" open H1 from any depth (O-02, B-01). */
  const onTab = useCallback(() => {
    if (entry.depth === 0) return;
    abortSearch();
    tabJump.current = true;
    window.history.go(-entry.depth);
  }, [abortSearch, entry.depth]);

  useEffect(() => {
    // popstate renders the view the entry names; a pending search is aborted. Going back focuses
    // the opener of the view returned to, or the row of the product closed; going forward, or
    // landing after the tab link, focuses the h1.
    const onPopState = (event: PopStateEvent) => {
      const link = readForecastLink(window.location.search);
      const next = readForecastEntry(event.state) ?? (link === 'invalid' ? FORECAST_START : link);
      abortSearch();
      let goal: FocusGoal = { to: null, wait: null, closing: next.depth < entry.depth };
      if (tabJump.current) {
        tabJump.current = false;
        openers.current = {};
      } else if (next.depth < entry.depth) {
        const key = next.view === 'all' ? 'all' : 'start';
        const fallback: Opener | null = entry.view === 'product' && entry.asin !== null
          ? { kind: 'row', asin: entry.asin }
          : entry.view === 'all' ? { kind: 'browse' } : null;
        goal = { to: openers.current[key] ?? fallback, wait: next.view === 'all' ? 'browse' : 'start', closing: true };
        delete openers.current[key];
      }
      setEntry(next);
      moveFocus(goal);
    };
    window.addEventListener('popstate', onPopState);
    return () => window.removeEventListener('popstate', onPopState);
  }, [abortSearch, entry, moveFocus]);

  useEffect(() => {
    // The history writes the load's URL needs (bootRoute): an invalid link is replaced by the bare
    // URL; a fresh link gets an H1 entry seeded under it, so closing never leaves the app. Run
    // twice (a development remount), it finds its own entry and writes nothing.
    if (boot === null) return;
    const bare = window.location.pathname;
    if (boot.invalid) {
      if (window.location.search !== '') window.history.replaceState(forecastHistoryState(window.history.state, FORECAST_START), '', bare);
      return;
    }
    const current = readForecastEntry(window.history.state);
    if (!boot.seed || (current?.view === boot.entry.view && current.asin === boot.entry.asin)) return;
    window.history.replaceState(forecastHistoryState(window.history.state, FORECAST_START), '', bare);
    window.history.pushState(forecastHistoryState(window.history.state, boot.entry), '', `${bare}${forecastSearch(boot.entry)}`);
  }, [boot]);

  useEffect(() => {
    // The detail a search found is stored under its key, so opening it sends no second request.
    if (detailKey === null || entry.asin === null || haveDetail) return undefined;
    const asin = entry.asin;
    const controller = new AbortController();
    fetchDemandForecastDetail(asin, controller.signal)
      .then((result) => {
        if (result.kind === 'refused' && result.refusal.refused === 'ASIN_INVALID') {
          // The server's format check agrees with the browser's: close and show the inline error.
          setSearch(asin);
          setOutcome({ kind: 'asin' });
          close();
          return;
        }
        setDetailResult({ key: detailKey, loaded: settle(result) });
      })
      .catch((error: unknown) => { if (!isAbort(error)) setDetailResult({ key: detailKey, loaded: { state: 'failed' } }); });
    return () => controller.abort();
  }, [close, detailKey, entry.asin, haveDetail]);

  const startSettled = start.state !== 'loading';
  const browseSettled = browse.state !== 'loading';
  useEffect(() => {
    // G-09: once the view the goal names has its list, focus its opener, else the h1. Focus that
    // is still on a control other than the closing Back, the crumb or the h1 never moves.
    const goal = focusGoal.current;
    if (goal === null || (goal.wait === 'start' && !startSettled) || (goal.wait === 'browse' && !browseSettled)) return;
    focusGoal.current = null;
    focusFrame.current = window.requestAnimationFrame(() => {
      focusFrame.current = null;
      if (!focusMovable(goal.closing)) return;
      const target = goal.to === null ? null : openerElement(goal.to, searchInput.current);
      (target ?? document.getElementById(PAGE_HEADING_ID))?.focus();
    });
  }, [browseSettled, focusTick, startSettled]);
  // A frame still pending when the page leaves must not move focus on whatever replaced it.
  useEffect(() => () => { if (focusFrame.current !== null) window.cancelAnimationFrame(focusFrame.current); }, []);

  const startReady = start.state === 'ready' ? start.value : null;
  // The search example is the start list's first ASIN, kept with its generation (G-08).
  const offered = startReady === null ? null : exampleAsinFrom(startReady);
  if (offered !== null && (offered.asin !== example?.asin || offered.generationId !== example.generationId)) setExample(offered);

  useEffect(() => {
    // The page-level error or absence replaces the view, so a pending search goes with it.
    if (start.state === 'failed' || (startReady !== null && startReady.outcome === 'unavailable')) abortSearch();
  }, [abortSearch, start.state, startReady]);

  const retry = useCallback(() => {
    abortSearch();
    setOutcome(null);
    setExample(null);
    setAttempt((value) => value + 1);
  }, [abortSearch]);
  const retryStatus = useCallback(() => setStatusAttempt((value) => value + 1), []);
  const mixed = mixedGenerations(entry.view === 'all' && !browseIsStart ? [start, browse] : [start], status, detail);

  const runSearch = (asin: string) => {
    abortSearch();
    const id = requestId.current + 1;
    requestId.current = id;
    const controller = new AbortController();
    pending.current = { asin, id, controller };
    setPendingAsin(asin);
    setOutcome(null);
    const key = `${asin}|${attempt}`;
    const done = () => {
      pending.current = null;
      setPendingAsin(null);
    };
    fetchDemandForecastDetail(asin, controller.signal)
      .then((result) => {
        if (requestId.current !== id) return;
        done();
        if (result.kind === 'refused' && result.refusal.refused === 'ASIN_INVALID') setOutcome({ kind: 'asin' });
        else if (result.kind !== 'ok') setOutcome({ kind: 'failed', asin });
        else if (result.value.outcome === 'not_in_forecast') setOutcome({ kind: 'notin', asin, noDigit: !hasDigit(asin) });
        else if (result.value.outcome === 'unavailable') setOutcome({ kind: 'unavailable' });
        else {
          setDetailResult({ key, loaded: { state: 'ready', value: result.value } });
          navigate(productEntry(asin, 'start'), { kind: 'search' });
        }
      })
      .catch((error: unknown) => {
        if (isAbort(error) || requestId.current !== id) return;
        done();
        setOutcome({ kind: 'failed', asin });
      });
  };
  const onSearch = (event: FormEvent) => {
    event.preventDefault();
    const typed = search.trim();
    if (!isAsin(typed)) {
      abortSearch();
      setOutcome({ kind: typed !== '' && looksLikeName(typed) ? 'name' : 'asin' });
      return;
    }
    const asin = typed.toUpperCase();
    // A repeat of the pending ASIN is ignored; a different one replaces it (O-05).
    if (pending.current?.asin === asin) return;
    runSearch(asin);
  };
  const onSearchChange = (value: string) => {
    setSearch(value);
    // B-03: an edit that changes the ASIN cancels the pending search and a stale Try again.
    const asin = normalizedAsin(value);
    if (pending.current !== null && pending.current.asin !== asin) abortSearch();
    if (outcome?.kind === 'failed' && outcome.asin !== asin) setOutcome(null);
  };
  const retrySearch = () => {
    if (outcome?.kind !== 'failed' || normalizedAsin(search) !== outcome.asin) return;
    runSearch(outcome.asin);
    searchInput.current?.focus();
  };
  const onBrowseQuery = (next: ListQuery) => {
    setNotice(null);
    setBrowseQuery(next);
  };

  const horizon = startReady?.horizon ?? null;
  const weeks = viewWeeks ?? horizon?.default_view_weeks ?? null;
  const startComplete = weeks === null ? null : completeList(start);
  const shownExample = currentExampleAsin(example, startComplete?.provenance?.generation_id ?? null);

  const outcomeLine = (() => {
    if (outcome === null || !hydrated) return null;
    switch (outcome.kind) {
      case 'asin': return <p id={outcomeId} className="df-outcome df-error" role="alert">{asinErrorText(shownExample)}</p>;
      case 'name': return <p id={outcomeId} className="df-outcome df-error" role="alert">{nameErrorText(shownExample)}</p>;
      case 'notin': return <p id={outcomeId} className="df-outcome" role="status">{notInForecastInlineText(outcome.asin, itemCountOf(startComplete), outcome.noDigit)}</p>;
      case 'unavailable': {
        // B-05: only while the start list loads; a ranked list never sits beside "no forecast".
        if (start.state !== 'loading') return null;
        const next = readyStatus(status)?.next_new_input_check_at ?? null;
        return <p id={outcomeId} className="df-outcome" role="status">{noForecastYetText(next === null ? null : formatIsoInstantShort(next))}</p>;
      }
      case 'failed': return (
        <p id={outcomeId} className="df-outcome" role="status">
          {startCopy.readFailed}{' '}
          <button type="button" className="btn link" onClick={retrySearch}>Try again</button>
        </p>
      );
      case 'link': return <p id={outcomeId} className="df-outcome" role="status">{startCopy.linkInvalid}</p>;
    }
  })();
  const searchBox = <SearchBox
    busy={pendingAsin !== null}
    describedBy={outcomeLine === null ? null : outcomeId}
    id={searchId}
    input={searchInput}
    invalid={outcome?.kind === 'asin' || outcome?.kind === 'name'}
    onChange={onSearchChange}
    onSubmit={onSearch}
    value={search}
  />;

  // Until hydrated the view is H1, as the export prerendered it.
  const route = hydrated ? entry : FORECAST_START;
  let body;
  if (mixed) {
    body = <StateCallout live="alert" title="Demand forecast" actions={<button type="button" className="btn" onClick={retry}>Show the latest forecast</button>}>{MIXED_TEXT}</StateCallout>;
  } else if (route.view === 'product' && route.asin !== null && (start.state === 'loading' || startComplete !== null)) {
    body = <ProductRegion
      asin={route.asin}
      backLabel={route.from === 'all' ? startCopy.backToAll : startCopy.backToStart}
      detail={detail ?? { state: 'loading' }}
      key={route.asin}
      list={startComplete}
      onBack={close}
      onRetry={retry}
      onRetryStatus={retryStatus}
      onTab={onTab}
      onViewWeeks={setViewWeeks}
      status={status}
      viewWeeks={weeks}
    />;
  } else if (start.state === 'failed') {
    body = <PageError message={DEMAND_FORECAST_READ_FAILED_TEXT} onRetry={retry}/>;
  } else if (start.state === 'ready' && startComplete === null) {
    const next = readyStatus(status)?.next_new_input_check_at ?? null;
    body = <PageAbsence title={DEMAND_FORECAST_NO_FORECAST_TITLE} message={noForecastYetText(next === null ? null : formatIsoInstantShort(next))}/>;
  } else if (route.view === 'all') {
    body = <BrowseAll
      list={browse}
      notice={notice}
      onBack={close}
      onOpen={(asin) => navigate(productEntry(asin, 'all'), { kind: 'row', asin })}
      onQuery={onBrowseQuery}
      onRetry={retry}
      onRetryStatus={retryStatus}
      onTab={onTab}
      onViewWeeks={setViewWeeks}
      query={browseQuery}
      status={status}
      viewWeeks={weeks}
    />;
  } else if (startComplete === null || weeks === null) {
    body = <StartSkeleton outcome={outcomeLine} search={searchBox}/>;
  } else {
    body = <>
      <ScopeBlock list={startComplete} onRetryStatus={retryStatus} onViewWeeks={setViewWeeks} status={status} viewWeeks={weeks}/>
      <AnswerSentence answer={listAnswer(startComplete, weeks)}/>
      <p className="df-ask">
        <button type="button" className="ask-chip" onClick={() => { if (startComplete.horizon !== null) seedAssistantQuestion(askQuestions.list(startComplete.horizon)); }}>Ask about this forecast</button>
      </p>
      <StartView
        list={startComplete}
        onBrowse={() => navigate(ALL, { kind: 'browse' })}
        onOpen={(asin) => navigate(productEntry(asin, 'start'), { kind: 'row', asin })}
        onReason={(reason) => {
          onBrowseQuery({ status: reason, page: 1 });
          navigate(ALL, { kind: 'reason', reason });
        }}
        outcome={outcomeLine}
        search={searchBox}
        viewWeeks={weeks}
      />
      <ListMethod list={startComplete}/>
    </>;
  }
  return <div className="page demand-forecast">{body}</div>;
}

/** The scope card and, when the list is out of date, its banner (S1, S6). */
function ScopeBlock({ list, onRetryStatus, onViewWeeks, status, viewWeeks }: {
  readonly list: DemandForecastList;
  readonly onRetryStatus: () => void;
  readonly onViewWeeks: (weeks: number) => void;
  readonly status: Loaded<DemandForecastStatus>;
  readonly viewWeeks: number;
}) {
  const { horizon, provenance } = list;
  if (horizon === null || provenance === null) return null;
  return <>
    <ScopeCard>
      <div className="df-pills">
        {list.item_count === null ? null : <span className="pill">{plural(list.item_count, 'product', 'products')}</span>}
        <span className="pill">Vendor Central · ordered and shipped</span>
        <FreshnessPill freshness={list.freshness} provenance={provenance} status={status} onRetryStatus={onRetryStatus}/>
      </div>
      <WeekControl horizon={horizon} viewWeeks={viewWeeks} onViewWeeks={onViewWeeks}/>
    </ScopeCard>
    {list.freshness.state === 'stale' ? <StaleBanner freshness={list.freshness} provenance={provenance} status={readyStatus(status)}/> : null}
  </>;
}

/**
 * H4: phase 1's full list, one click from the opening view. It publishes "All products" as the
 * open entity with Back, and unmounts while a product opened from it is shown (O-02).
 */
function BrowseAll({ list, notice, onBack, onOpen, onQuery, onRetry, onRetryStatus, onTab, onViewWeeks, query, status, viewWeeks }: {
  readonly list: Loaded<DemandForecastList>;
  readonly notice: string | null;
  readonly onBack: () => void;
  readonly onOpen: (asin: string) => void;
  readonly onQuery: (query: ListQuery) => void;
  readonly onRetry: () => void;
  readonly onRetryStatus: () => void;
  readonly onTab: () => void;
  readonly onViewWeeks: (weeks: number) => void;
  readonly query: ListQuery;
  readonly status: Loaded<DemandForecastStatus>;
  readonly viewWeeks: number | null;
}) {
  useOpenEntity(startCopy.browseTitle, onBack, startCopy.backToStart, onTab);
  if (list.state === 'loading') return <ListSkeleton/>;
  if (list.state === 'failed') return <PageError message={DEMAND_FORECAST_READ_FAILED_TEXT} onRetry={onRetry}/>;
  const ready = completeList(list);
  if (ready === null || viewWeeks === null) {
    const next = readyStatus(status)?.next_new_input_check_at ?? null;
    return <PageAbsence title={DEMAND_FORECAST_NO_FORECAST_TITLE} message={noForecastYetText(next === null ? null : formatIsoInstantShort(next))}/>;
  }
  return <>
    <ScopeBlock list={ready} onRetryStatus={onRetryStatus} onViewWeeks={onViewWeeks} status={status} viewWeeks={viewWeeks}/>
    <div className="df-tools">
      <FilterChips list={ready} status={query.status} onChoose={(value) => onQuery({ status: value, page: 1 })}/>
    </div>
    {notice ? <p role="status" className="df-notice">{notice}</p> : null}
    <ForecastTable list={ready} viewWeeks={viewWeeks} onSelect={onOpen} onShowAll={() => onQuery(START_QUERY)}/>
    <Pager list={ready} onPage={(page) => onQuery({ status: query.status, page })}/>
    <ListMethod list={ready}/>
  </>;
}

/**
 * The product in place of the list (S2 to S4). It publishes the product as the open entity, so the
 * shell's h1 names it with Back, and shows its skeleton until both the detail and the start list
 * are ready; the start list supplies the measure default and the product count (same generation).
 */
function ProductRegion({ asin, backLabel, detail, list, onBack, onRetry, onRetryStatus, onTab, onViewWeeks, status, viewWeeks }: {
  readonly asin: string;
  readonly backLabel: string;
  readonly detail: Loaded<DemandForecastDetail | null>;
  readonly list: DemandForecastList | null;
  readonly onBack: () => void;
  readonly onRetry: () => void;
  readonly onRetryStatus: () => void;
  readonly onTab: () => void;
  readonly onViewWeeks: (weeks: number) => void;
  readonly status: Loaded<DemandForecastStatus>;
  readonly viewWeeks: number | null;
}) {
  const shown = detail.state === 'ready' ? detail.value : null;
  const listed = list?.entries.find((entry) => entry.asin === asin);
  const name = shown === null
    ? (listed === undefined ? asin : listed.title ?? 'Untitled product')
    : shown.outcome === 'not_in_forecast' ? null : shown.title ?? 'Untitled product';
  useOpenEntity(name, onBack, backLabel, onTab);
  if (detail.state === 'failed') return <PageError message={DEMAND_FORECAST_READ_FAILED_TEXT} onRetry={onRetry}/>;
  if (shown === null || list === null || viewWeeks === null) return <ProductSkeleton/>;
  return <DemandForecastAsinDetail
    detail={shown}
    itemCount={itemCountOf(list)}
    onBack={onBack}
    onRetryStatus={onRetryStatus}
    onViewWeeks={onViewWeeks}
    status={status}
    statusTarget={list.status_target}
    viewWeeks={viewWeeks}
  />;
}

/** S7: a product opening reserves its tiles, chart and table. */
function ProductSkeleton() {
  return (
    <section className="df-skeleton" aria-busy="true" aria-label="Loading the product forecast">
      <div className="tiles detail-tiles"><span className="skel card"/><span className="skel card"/><span className="skel card"/><span className="skel card"/></div>
      <span className="skel h-chart"/>
      <span className="skel table"/>
    </section>
  );
}

/** S7: the list's final layout in `.skel` shapes, under one busy region. */
function ListSkeleton() {
  return (
    <section className="df-skeleton" aria-busy="true" aria-label="Loading the forecast">
      <div className="band df-scope">
        <div className="df-pills"><span className="skel h-label"/><span className="skel h-label"/><span className="skel h-label"/></div>
        <span className="skel df-skel-seg"/>
      </div>
      <span className="skel h-line"/>
      <span className="skel table"/>
    </section>
  );
}

/** P1-6: one single-select chip group; each chip asks the server for its rows (G-05, D1). */
function FilterChips({ list, onChoose, status }: {
  readonly list: DemandForecastList;
  readonly onChoose: (status: string | null) => void;
  readonly status: string | null;
}) {
  const measure = filterTargetCopy[list.status_target];
  const byReason = list.counts_by_reason?.[list.status_target] ?? {};
  return (
    <div className="seg df-filter" role="group" aria-label={`Products by ${measure.label} forecast`}>
      <button type="button" aria-pressed={status === null} onClick={() => onChoose(null)}>
        All products{list.item_count === null ? null : <>{' '}<span className="n">{list.item_count}</span></>}
      </button>
      <button type="button" aria-pressed={status === 'shown'} onClick={() => onChoose('shown')}>
        With {measure.article} {measure.label} forecast
      </button>
      {skipReasons.filter((reason) => (byReason[reason] ?? 0) > 0).map((reason) => (
        <button key={reason} type="button" aria-pressed={status === reason} onClick={() => onChoose(reason)}>
          No {measure.label} forecast: {lowerFirst(skipReasonCopy[reason])}{' '}<span className="n">{byReason[reason]}</span>
        </button>
      ))}
    </div>
  );
}

/** A served total for a list cell: grouped units, or money under a one-code header or with its own code. */
function cellNumber(entry: ListEntry, target: Target, key: ViewKey, singleCode: string | null): string | null {
  const row = entry.targets[target];
  const total = row.p50_totals[key];
  if (total === null) return null;
  if (!moneyTargets.includes(target)) return formatCount(total.display);
  if (singleCode !== null) return formatCanonicalDecimal(total.display);
  if (row.currency_code === null) throw new TypeError('A money figure needs its currency.');
  return formatCurrency(total.display, row.currency_code);
}

const COLUMN: Readonly<Record<Target, string>> = {
  units_ordered: 'Units ordered',
  units_shipped: 'Units shipped',
  revenue_shipped: 'Shipped revenue',
  cogs_shipped: 'Amazon wholesale cost',
};

/** P1-8: the list, ranked by the default view, with every measure's served total (D2: no model column). */
function ForecastTable({ list, onSelect, onShowAll, viewWeeks }: {
  readonly list: DemandForecastList;
  readonly onSelect: (asin: string) => void;
  readonly onShowAll: () => void;
  readonly viewWeeks: number;
}) {
  const horizon = list.horizon;
  const first = horizon?.weeks[0];
  if (horizon === null || first === undefined) return null;
  const key = String(viewWeeks) as ViewKey;
  const basis = `${horizon.default_view_weeks}-week likely demand`;
  const singleCode = list.currency_codes.length === 1 ? list.currency_codes[0] ?? null : null;
  const rank = INFO.rank(horizon.default_view_weeks, first);
  const cogsTip = (className?: string) => <InfoTip title={INFO.cogs.title} label="About Amazon wholesale cost" className={className}>{INFO.cogs.text}</InfoTip>;
  const label = (target: Target) => (moneyTargets.includes(target) && singleCode !== null ? `${COLUMN[target]} ${singleCode}` : COLUMN[target]);
  // The pop-ups sit just above the table, never in its caption: a table holds only rows and
  // headers (axe aria-required-children). The stacked form's copies of the header pop-ups live
  // here too, because its clipped header row holds no focusable control (G-04).
  return (<>
    <p className="df-table-tips">
      <span>Likely totals<InfoTip title={INFO.total.title}>{INFO.total.text}</InfoTip></span>
      <span className="stack-tips">
        <span>Rank<InfoTip title={rank.title}>{rank.text}</InfoTip></span>
        <span>Amazon wholesale cost{cogsTip()}</span>
        <span>Weeks shipped less than ordered<InfoTip title={INFO.short.title}>{INFO.short.text}</InfoTip></span>
      </span>
    </p>
    <DataTable
      className="df-list"
      title={`Likely totals, ${viewWeeks} weeks starting ${formatIsoDate(first)}`}
      note={viewWeeks === horizon.default_view_weeks ? undefined : `Rows stay ordered by ${horizon.default_view_weeks}-week likely demand.`}
    >
      <DtHead>
        <DtRow>
          <DtColumn basis={basis}>Rank<InfoTip title={rank.title}>{rank.text}</InfoTip></DtColumn>
          <DtColumn className="l">Product</DtColumn>
          <DtColumn basis="(demand)">Units ordered</DtColumn>
          <DtColumn>Units shipped</DtColumn>
          <DtColumn basis={singleCode}>Shipped revenue</DtColumn>
          <DtColumn basis={singleCode}>Amazon wholesale cost{cogsTip('end')}</DtColumn>
          <DtColumn basis="of the last 13">Weeks shipped less than ordered<InfoTip title={INFO.short.title} className="end">{INFO.short.text}</InfoTip></DtColumn>
        </DtRow>
      </DtHead>
      <DtBody>
        {list.entries.length === 0 ? (
          <DtRow>
            <DtCell label="" colSpan={7} className="l df-empty">
              No product is without a forecast for this reason.{' '}
              <button type="button" className="btn link" onClick={onShowAll}>Show all products</button>
            </DtCell>
          </DtRow>
        ) : list.entries.map((entry) => {
          const withheld = displayTargets.every((target) => entry.targets[target].withheld_reason !== null);
          const reason = entry.targets[list.status_target].withheld_reason;
          return (
            <DtRow key={entry.asin}>
              <DtCell label={`Rank, ${basis}`} className="l">{entry.rank}</DtCell>
              <DtRowHeader className="row-pick">
                <button type="button" data-forecast-row={entry.asin} onClick={() => onSelect(entry.asin)}>
                  {entry.title ?? 'Untitled product'}
                  {entry.title_truncated ? <span className="sr-only"> (name shortened)</span> : null}
                </button>
                <span className="sub">{entry.asin}</span>
              </DtRowHeader>
              {withheld ? (
                <DtCell label="Forecast" colSpan={4} className="l df-nofc df-merged">
                  No forecast{reason === null ? '' : `: ${lowerFirst(skipReasonCopy[reason])}`}
                </DtCell>
              ) : displayTargets.map((target) => {
                const shown = cellNumber(entry, target, key, singleCode);
                return <DtCell key={target} label={label(target)} className={shown === null ? 'df-nofc' : undefined}>{shown ?? 'No forecast'}</DtCell>;
              })}
              <DtCell label="Weeks shipped less than ordered, of the last 13">
                {withheld
                  ? <><span aria-hidden="true">—</span><span className="sr-only">Not shown for a product without a forecast</span></>
                  : entry.stockout_weeks_last_13}
              </DtCell>
            </DtRow>
          );
        })}
      </DtBody>
    </DataTable>
  </>);
}

/** P1-9 (L-PAGER). */
function Pager({ list, onPage }: { readonly list: DemandForecastList; readonly onPage: (page: number) => void }) {
  if (list.entries.length === 0) return null;
  return (
    <nav aria-label="Forecast pages" className="df-pager">
      <button type="button" className="btn" disabled={list.page <= 1} onClick={() => onPage(list.page - 1)}><span aria-hidden="true">‹ </span>Previous</button>
      <span>Page {list.page} of {list.page_count} · {plural(list.match_count, 'product', 'products')}</span>
      <button type="button" className="btn" disabled={list.page >= list.page_count} onClick={() => onPage(list.page + 1)}>Next<span aria-hidden="true"> ›</span></button>
    </nav>
  );
}

/** P1-10: how the forecast is made, with the served model counts for the status target. */
function ListMethod({ list }: { readonly list: DemandForecastList }) {
  const byModel = list.counts_by_model?.[list.status_target] ?? {};
  const used = modelNames.filter((model) => (byModel[model] ?? 0) > 0);
  const counts = used.map((model, index) => {
    const n = byModel[model] ?? 0;
    return index === 0 ? `${plural(n, 'product', 'products')} forecast by ${modelCopy[model]}` : `${n} by ${modelCopy[model]}`;
  });
  return (
    <details className="df-method">
      <summary>How this forecast is made</summary>
      <p>Each product&apos;s forecast continues its recent weekly sales and product page views. Promotions, price changes and ad plans you make aren&apos;t in it.</p>
      <p>Before a forecast is shown, each method forecasts the product&apos;s most recent weeks from the weeks before them. Chronos-2, a pretrained forecasting model, is used only where it beats both simpler methods in that test; otherwise the simpler method that tested best is used.</p>
      {counts.length === 0 ? null : <p>{measureCopy[list.status_target]}: {counts.join(', ')}.</p>}
      {list.horizon === null ? null : <p>{weekStartSentenceCopy[list.horizon.week_starts_on]}</p>}
    </details>
  );
}
