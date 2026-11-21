// SPDX-License-Identifier: MIT-0
'use client';
import { useCallback, useEffect, useId, useRef, useState, type FormEvent, type RefObject } from 'react';
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
  itemCountOf,
  filterTargetCopy,
  INFO,
  listAnswer,
  lowerFirst,
  measureCopy,
  modelCopy,
  modelNames,
  moneyTargets,
  plural,
  skipReasonCopy,
  skipReasons,
  weekStartSentenceCopy,
  type DemandForecastDetail,
  type DemandForecastList,
  type DemandForecastStatus,
  type ExampleAsin,
  type FetchResult,
  type ListEntry,
  type Target,
  type ViewKey,
} from '@/lib/demand-forecast';
import { useOpenEntity } from './AppShell';
import { AnswerSentence } from './AnswerSentence';
import { seedAssistantQuestion } from './ChatPanel';
import { DataTable, DtBody, DtCell, DtColumn, DtHead, DtRow, DtRowHeader } from './DataTable';
import { DemandForecastAsinDetail } from './DemandForecastAsinDetail';
import { PAGE_HEADING_ID } from './Dialog';
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
import { InfoTip } from './InfoTip';
import { PageAbsence } from './PageAbsence';
import { PageError, StateCallout } from './StatePanels';

export type { Loaded } from './DemandForecastScope';
export { DEMAND_FORECAST_NO_FORECAST_TITLE, DEMAND_FORECAST_READ_FAILED_TEXT, noForecastYetText } from './DemandForecastScope';

/**
 * The Demand forecast tab (ADR-163; forecasting LLD "Demand forecast page"). The shell draws the
 * one h1. The list and status requests start together and each renders when ready: the list
 * carries its own freshness, so only the pill's schedule lines wait for status. A publish that
 * changes the page count returns the list to page 1 once; a service failure offers Try again; an
 * aborted request is not an error. The list, status and detail each name the generation they
 * read; when a publish lands between requests and they disagree, none of them renders, and Show
 * the latest forecast refetches all three together. Every figure is a served display string.
 */

interface Keyed<T> { readonly key: string; readonly loaded: Loaded<T> }

const MIXED_TEXT = 'A newer forecast was published while this page loaded.';
const PAGE1_TEXT = 'The forecast was updated, so the list is back on page 1.';
const ASIN = /^[A-Za-z0-9]{10}$/;

function isAbort(error: unknown): boolean {
  return error instanceof DOMException && error.name === 'AbortError';
}

/** The generation each ready result read, `null` for none published; mixed results disagree. */
function mixedGenerations(
  list: Loaded<DemandForecastList>,
  status: Loaded<DemandForecastStatus>,
  detail: Loaded<DemandForecastDetail | null> | null,
): boolean {
  const read = new Set<string | null>();
  if (list.state === 'ready') read.add(list.value.provenance?.generation_id ?? null);
  if (status.state === 'ready') read.add(status.value.generation?.generation_id ?? null);
  if (detail?.state === 'ready' && detail.value !== null) read.add(detail.value.provenance?.generation_id ?? null);
  return read.size > 1;
}

function settle<T>(result: FetchResult<T>): Loaded<T> {
  return result.kind === 'ok' ? { state: 'ready', value: result.value } : { state: 'failed' };
}

export function DemandForecastView() {
  const [query, setQuery] = useState<{ status: string | null; page: number }>({ status: null, page: 1 });
  const [listResult, setListResult] = useState<Keyed<DemandForecastList> | null>(null);
  const [statusResult, setStatusResult] = useState<Keyed<DemandForecastStatus> | null>(null);
  const [detailResult, setDetailResult] = useState<Keyed<DemandForecastDetail | null> | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [search, setSearch] = useState('');
  const [searchInvalid, setSearchInvalid] = useState(false);
  const [refocusSearch, setRefocusSearch] = useState(0);
  const [notice, setNotice] = useState<string | null>(null);
  const [viewWeeks, setViewWeeks] = useState<number | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [statusAttempt, setStatusAttempt] = useState(0);
  const [example, setExample] = useState<ExampleAsin | null>(null);
  const searchInput = useRef<HTMLInputElement>(null);
  // Each result carries the request it answers, so a newer request reads as loading until its
  // own result lands, and no effect sets state before its fetch settles.
  const statusKey = `${attempt}|${statusAttempt}`;
  const listKey = `${query.status ?? ''}|${query.page}|${attempt}`;
  const detailKey = selected === null ? null : `${selected}|${attempt}`;

  useEffect(() => {
    const controller = new AbortController();
    fetchDemandForecastStatus(controller.signal)
      .then((result) => setStatusResult({ key: statusKey, loaded: settle(result) }))
      .catch((error: unknown) => { if (!isAbort(error)) setStatusResult({ key: statusKey, loaded: { state: 'failed' } }); });
    return () => controller.abort();
  }, [statusKey]);

  useEffect(() => {
    const controller = new AbortController();
    const load = async () => {
      const result = await fetchDemandForecastList(query, controller.signal);
      if (result.kind === 'refused' && result.refusal.refused === 'PAGE_INVALID' && query.page !== 1) {
        // A publish changed the page count since the last list: back to page 1, which refetches
        // once. Page 1 always exists, so a second PAGE_INVALID shows the error panel.
        setNotice(PAGE1_TEXT);
        setQuery({ status: query.status, page: 1 });
        return;
      }
      setListResult({ key: listKey, loaded: settle(result) });
    };
    load().catch((error: unknown) => { if (!isAbort(error)) setListResult({ key: listKey, loaded: { state: 'failed' } }); });
    return () => controller.abort();
  }, [query, listKey]);

  useEffect(() => {
    if (selected === null || detailKey === null) return undefined;
    const controller = new AbortController();
    fetchDemandForecastDetail(selected, controller.signal)
      .then((result) => {
        if (result.kind === 'refused' && result.refusal.refused === 'ASIN_INVALID') {
          // The server's format check agrees with the browser's: show the same inline error.
          setSelected(null);
          setSearchInvalid(true);
          setRefocusSearch((value) => value + 1);
          return;
        }
        setDetailResult({ key: detailKey, loaded: settle(result) });
      })
      .catch((error: unknown) => { if (!isAbort(error)) setDetailResult({ key: detailKey, loaded: { state: 'failed' } }); });
    return () => controller.abort();
  }, [selected, detailKey]);

  useEffect(() => {
    if (refocusSearch > 0) searchInput.current?.focus();
  }, [refocusSearch]);

  const list: Loaded<DemandForecastList> = listResult?.key === listKey ? listResult.loaded : { state: 'loading' };
  const status: Loaded<DemandForecastStatus> = statusResult?.key === statusKey ? statusResult.loaded : { state: 'loading' };
  const detail: Loaded<DemandForecastDetail | null> | null = detailKey === null
    ? null
    : detailResult?.key === detailKey ? detailResult.loaded : { state: 'loading' };

  // The search example is the first ASIN of an unfiltered page 1, kept with its generation (G-08).
  const offered = list.state === 'ready' ? exampleAsinFrom(list.value) : null;
  if (offered !== null && (offered.asin !== example?.asin || offered.generationId !== example.generationId)) setExample(offered);

  const retry = useCallback(() => {
    setExample(null);
    setAttempt((value) => value + 1);
  }, []);
  const retryStatus = useCallback(() => setStatusAttempt((value) => value + 1), []);
  const mixed = mixedGenerations(list, status, detail);

  const onSearch = (event: FormEvent) => {
    event.preventDefault();
    const asin = search.trim();
    if (!ASIN.test(asin)) {
      setSearchInvalid(true);
      return;
    }
    setSearchInvalid(false);
    setSelected(asin.toUpperCase());
  };
  const onQuery = (next: { status: string | null; page: number }) => {
    setNotice(null);
    setQuery(next);
  };

  const ready = list.state === 'ready' ? list.value : null;
  const horizon = ready?.horizon ?? null;
  const weeks = viewWeeks ?? horizon?.default_view_weeks ?? null;

  const onBack = useCallback(() => {
    setSelected(null);
    setSearchInvalid(false);
  }, []);
  const complete = ready !== null && ready.outcome === 'complete' && ready.provenance !== null && horizon !== null && weeks !== null
    ? ready
    : null;

  let body;
  if (mixed) {
    body = <StateCallout live="alert" title="Demand forecast" actions={<button type="button" className="btn" onClick={retry}>Show the latest forecast</button>}>{MIXED_TEXT}</StateCallout>;
  } else if (selected !== null && (list.state === 'loading' || complete !== null)) {
    body = <ProductRegion
      asin={selected}
      detail={detail ?? { state: 'loading' }}
      key={selected}
      list={complete}
      onBack={onBack}
      onRetry={retry}
      onRetryStatus={retryStatus}
      onViewWeeks={setViewWeeks}
      status={status}
      viewWeeks={weeks}
    />;
  } else if (list.state === 'loading') {
    body = <ListSkeleton/>;
  } else if (list.state === 'failed') {
    body = <PageError message={DEMAND_FORECAST_READ_FAILED_TEXT} onRetry={retry}/>;
  } else if (ready === null || ready.outcome === 'unavailable' || ready.provenance === null || horizon === null || weeks === null) {
    const next = readyStatus(status)?.next_new_input_check_at ?? null;
    body = <PageAbsence title={DEMAND_FORECAST_NO_FORECAST_TITLE} message={noForecastYetText(next === null ? null : formatIsoInstantShort(next))}/>;
  } else {
    const provenance = ready.provenance;
    body = <>
      <ScopeCard>
        <div className="df-pills">
          {ready.item_count === null ? null : <span className="pill">{plural(ready.item_count, 'product', 'products')}</span>}
          <span className="pill">Vendor Central · ordered and shipped</span>
          <FreshnessPill freshness={ready.freshness} provenance={provenance} status={status} onRetryStatus={retryStatus}/>
        </div>
        <WeekControl horizon={horizon} viewWeeks={weeks} onViewWeeks={setViewWeeks}/>
      </ScopeCard>
      {ready.freshness.state === 'stale' ? <StaleBanner freshness={ready.freshness} provenance={provenance} status={readyStatus(status)}/> : null}
      <AnswerSentence answer={listAnswer(ready, weeks)}/>
      <p className="df-ask">
        <button type="button" className="ask-chip" onClick={() => seedAssistantQuestion(askQuestions.list(horizon))}>Ask about this forecast</button>
      </p>
      <div className="df-tools">
        <FilterChips list={ready} status={query.status} onChoose={(value) => onQuery({ status: value, page: 1 })}/>
        <SearchForm
          example={currentExampleAsin(example, provenance.generation_id)}
          input={searchInput}
          invalid={searchInvalid}
          onChange={(value) => { setSearch(value); }}
          onSubmit={onSearch}
          value={search}
        />
      </div>
      {notice ? <p role="status" className="df-notice">{notice}</p> : null}
      <ForecastTable list={ready} viewWeeks={weeks} onSelect={setSelected} onShowAll={() => onQuery({ status: null, page: 1 })}/>
      <Pager list={ready} onPage={(page) => onQuery({ status: query.status, page })}/>
      <ListMethod list={ready}/>
    </>;
  }
  return <div className="page demand-forecast">{body}</div>;
}

/** Focus fell to the document because the control that held it left with the view. */
function focusLost(): boolean {
  return document.activeElement === null || document.activeElement === document.body;
}
/**
 * G-09: opening a product focuses the h1 (the product's name) once the product region replaces
 * the list; closing it returns focus to the row that opened it, or to the h1 when that row isn't
 * on the page. Neither moves focus that is still on a control.
 */
function useForecastDetailFocus(asin: string): void {
  useEffect(() => {
    if (focusLost()) document.getElementById(PAGE_HEADING_ID)?.focus();
    return () => {
      window.requestAnimationFrame(() => {
        if (!focusLost() && document.activeElement?.id !== PAGE_HEADING_ID) return;
        const row = [...document.querySelectorAll<HTMLButtonElement>('button[data-forecast-row]')]
          .find((button) => button.dataset.forecastRow === asin);
        (row ?? document.getElementById(PAGE_HEADING_ID))?.focus();
      });
    };
  }, [asin]);
}

/**
 * The product in place of the list (S2 to S4). It publishes the product as the open entity, so the
 * shell's h1 names it with Back, and shows its skeleton until both the detail and the current
 * list are ready; the list supplies the measure default and the product count (same generation).
 */
function ProductRegion({ asin, detail, list, onBack, onRetry, onRetryStatus, onViewWeeks, status, viewWeeks }: {
  readonly asin: string;
  readonly detail: Loaded<DemandForecastDetail | null>;
  readonly list: DemandForecastList | null;
  readonly onBack: () => void;
  readonly onRetry: () => void;
  readonly onRetryStatus: () => void;
  readonly onViewWeeks: (weeks: number) => void;
  readonly status: Loaded<DemandForecastStatus>;
  readonly viewWeeks: number | null;
}) {
  const shown = detail.state === 'ready' ? detail.value : null;
  const listed = list?.entries.find((entry) => entry.asin === asin);
  const name = shown === null
    ? (listed === undefined ? asin : listed.title ?? 'Untitled product')
    : shown.outcome === 'not_in_forecast' ? null : shown.title ?? 'Untitled product';
  useOpenEntity(name, onBack, 'Back to Demand forecast');
  useForecastDetailFocus(asin);
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
    <div className="df-skeleton" aria-busy="true" aria-label="Loading the product forecast">
      <div className="tiles detail-tiles"><span className="skel card"/><span className="skel card"/><span className="skel card"/><span className="skel card"/></div>
      <span className="skel h-chart"/>
      <span className="skel table"/>
    </div>
  );
}

/** S7: the list's final layout in `.skel` shapes, under one busy region. */
function ListSkeleton() {
  return (
    <div className="df-skeleton" aria-busy="true" aria-label="Loading the forecast">
      <div className="band df-scope">
        <div className="df-pills"><span className="skel h-label"/><span className="skel h-label"/><span className="skel h-label"/></div>
        <span className="skel df-skel-seg"/>
      </div>
      <span className="skel h-line"/>
      <span className="skel table"/>
    </div>
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

/** P1-7: ASIN search with its hint and an inline error the input names (E-ASIN). */
function SearchForm({ example, input, invalid, onChange, onSubmit, value }: {
  readonly example: string | null;
  readonly input: RefObject<HTMLInputElement | null>;
  readonly invalid: boolean;
  readonly onChange: (value: string) => void;
  readonly onSubmit: (event: FormEvent) => void;
  readonly value: string;
}) {
  const id = useId();
  const hint = `${id}-hint`;
  const error = `${id}-error`;
  return (
    <form role="search" className="df-search" onSubmit={onSubmit} noValidate>
      <label htmlFor={id}>Find a product by ASIN</label>
      <span className="df-search-row">
        <input
          id={id}
          ref={input}
          className="text-input"
          value={value}
          onChange={(event) => onChange(event.target.value)}
          aria-invalid={invalid}
          aria-describedby={invalid ? `${hint} ${error}` : hint}
          autoComplete="off"
          spellCheck={false}
        />
        <button type="submit" className="btn">Find</button>
      </span>
      <span id={hint} className="df-hint">10 letters or digits</span>
      {invalid ? <span id={error} className="df-error" role="alert">{asinErrorText(example)}</span> : null}
    </form>
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
  return (
    <DataTable
      className="df-list"
      title={`Likely totals, ${viewWeeks} weeks starting ${formatIsoDate(first)}`}
      note={<>
        <InfoTip title={INFO.total.title}>{INFO.total.text}</InfoTip>
        {viewWeeks === horizon.default_view_weeks ? null : <span className="df-line">Rows stay ordered by {horizon.default_view_weeks}-week likely demand.</span>}
        <span className="stack-tips">
          <span>Rank<InfoTip title={rank.title}>{rank.text}</InfoTip></span>
          <span>Amazon wholesale cost{cogsTip()}</span>
          <span>Weeks shipped less than ordered<InfoTip title={INFO.short.title}>{INFO.short.text}</InfoTip></span>
        </span>
      </>}
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
  );
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
