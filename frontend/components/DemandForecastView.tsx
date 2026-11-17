// SPDX-License-Identifier: MIT-0
'use client';
import { useCallback, useEffect, useState, type FormEvent } from 'react';
import { formatIsoDate, formatIsoInstant } from '@/lib/exact-format';
import {
  failureFamilyCopy,
  fetchDemandForecastDetail,
  fetchDemandForecastList,
  fetchDemandForecastStatus,
  figureText,
  freshnessCauseCopy,
  holdCopy,
  modelCopy,
  modelNames,
  runOutcomeCopy,
  skipReasonCopy,
  skipReasons,
  targetCopy,
  targets,
  type DemandForecastDetail,
  type DemandForecastList,
  type DemandForecastStatus,
  type FetchResult,
  type ViewKey,
} from '@/lib/demand-forecast';
import { PAGE_HEADING_ID } from './Dialog';
import { DemandForecastAsinDetail } from './DemandForecastAsinDetail';

/**
 * The Demand forecast tab (ADR-163; forecasting LLD "Demand forecast page"). The list and status
 * requests start together and each renders when ready. A publish that changes the page count
 * returns the list to page 1 once; a service failure offers Try again; an aborted request is
 * not an error.
 */

interface Keyed<T> { readonly key: string; readonly loaded: Loaded<T> }
type Loaded<T> = { readonly state: 'loading' } | { readonly state: 'ready'; readonly value: T } | { readonly state: 'failed' };

const UNAVAILABLE = "The forecast can't be read right now.";
const ASIN = /^[A-Za-z0-9]{10}$/;

function isAbort(error: unknown): boolean {
  return error instanceof DOMException && error.name === 'AbortError';
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
  const [searchError, setSearchError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [viewWeeks, setViewWeeks] = useState<number | null>(null);
  const [attempt, setAttempt] = useState(0);
  // Each result carries the request it answers, so a newer request reads as loading until its
  // own result lands, and no effect sets state before its fetch settles.
  const statusKey = String(attempt);
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
        setNotice('The forecast was updated, so the list is back on page 1.');
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
          setSearchError('Enter a 10-character ASIN.');
          setDetailResult({ key: detailKey, loaded: { state: 'ready', value: null } });
          return;
        }
        setDetailResult({ key: detailKey, loaded: settle(result) });
      })
      .catch((error: unknown) => { if (!isAbort(error)) setDetailResult({ key: detailKey, loaded: { state: 'failed' } }); });
    return () => controller.abort();
  }, [selected, detailKey]);

  const list: Loaded<DemandForecastList> = listResult?.key === listKey ? listResult.loaded : { state: 'loading' };
  const status: Loaded<DemandForecastStatus> = statusResult?.key === statusKey ? statusResult.loaded : { state: 'loading' };
  const detail: Loaded<DemandForecastDetail | null> | null = detailKey === null
    ? null
    : detailResult?.key === detailKey ? detailResult.loaded : { state: 'loading' };

  const retry = useCallback(() => setAttempt((value) => value + 1), []);

  const onSearch = (event: FormEvent) => {
    event.preventDefault();
    if (!ASIN.test(search.trim())) {
      setSearchError('Enter a 10-character ASIN.');
      return;
    }
    setSearchError(null);
    setSelected(search.trim().toUpperCase());
  };

  const ready = list.state === 'ready' ? list.value : null;
  const horizon = ready?.horizon ?? null;
  const weeks = viewWeeks ?? horizon?.default_view_weeks ?? null;
  const key = (weeks === null ? null : String(weeks)) as ViewKey | null;

  return (
    <div className="page demand-forecast">
      <h1 id={PAGE_HEADING_ID} tabIndex={-1}>Demand forecast</h1>
      <StatusPanel status={status} onRetry={retry}/>
      {list.state === 'loading' ? <div className="skeleton" aria-busy="true" aria-label="Loading the forecast" style={{ minHeight: '24rem' }}/> : null}
      {list.state === 'failed' ? <Unavailable onRetry={retry}/> : null}
      {ready && ready.outcome === 'unavailable' ? <NoForecast status={status}/> : null}
      {ready && ready.outcome === 'complete' && ready.provenance && horizon && weeks !== null && key !== null ? (
        <>
          <p>Forecast from data through {formatIsoDate(ready.provenance.input_end)}, published {formatIsoInstant(ready.provenance.published_at)}.</p>
          {ready.freshness.state === 'stale' ? (
            <p role="alert">This forecast is stale: {ready.freshness.causes.map((cause) => freshnessCauseCopy[cause]).join('; ')}.</p>
          ) : null}
          <fieldset>
            <legend>Show the next</legend>
            {horizon.views.map((view) => (
              <label key={view}>
                <input type="radio" name="forecast-view" checked={view === weeks} onChange={() => setViewWeeks(view)}/>
                {view} {horizon.grain}s
              </label>
            ))}
          </fieldset>
          <ModelMix list={ready}/>
          <form role="search" onSubmit={onSearch}>
            <label htmlFor="forecast-asin">Find a product by ASIN</label>
            <input id="forecast-asin" value={search} onChange={(event) => setSearch(event.target.value)} aria-invalid={searchError !== null}/>
            <button type="submit" className="btn">Find</button>
            {searchError ? <p role="alert">{searchError}</p> : null}
          </form>
          <label htmlFor="forecast-status">Show</label>
          <select
            id="forecast-status"
            value={query.status ?? ''}
            onChange={(event) => { setNotice(null); setQuery({ status: event.target.value || null, page: 1 }); }}
          >
            <option value="">All products</option>
            <option value="shown">Products with a demand forecast</option>
            {skipReasons.map((reason) => <option key={reason} value={reason}>Not forecast: {skipReasonCopy[reason]}</option>)}
          </select>
          {notice ? <p role="status">{notice}</p> : null}
          <ForecastTable list={ready} viewKey={key} grain={horizon.grain} onSelect={setSelected}/>
          <nav aria-label="Forecast pages">
            <button type="button" className="btn" disabled={ready.page <= 1} onClick={() => setQuery({ ...query, page: ready.page - 1 })}>Previous</button>
            <span> Page {ready.page} of {ready.page_count} ({ready.match_count} products) </span>
            <button type="button" className="btn" disabled={ready.page >= ready.page_count} onClick={() => setQuery({ ...query, page: ready.page + 1 })}>Next</button>
          </nav>
          {detail?.state === 'loading' ? <div className="skeleton" aria-busy="true" aria-label="Loading the product forecast" style={{ minHeight: '16rem' }}/> : null}
          {detail?.state === 'failed' ? <Unavailable onRetry={retry}/> : null}
          {detail?.state === 'ready' && detail.value !== null ? <DemandForecastAsinDetail detail={detail.value} viewWeeks={weeks}/> : null}
        </>
      ) : null}
    </div>
  );
}

function Unavailable({ onRetry }: { readonly onRetry: () => void }) {
  return (
    <div role="alert">
      <p>{UNAVAILABLE}</p>
      <button type="button" className="btn" onClick={onRetry}>Try again</button>
    </div>
  );
}

function NoForecast({ status }: { readonly status: Loaded<DemandForecastStatus> }) {
  const next = status.state === 'ready' ? status.value.next_new_input_check_at : null;
  return (
    <p role="status">
      No forecast has been published yet. The first one appears after a weekly run that has enough Vendor history
      {next ? `; the next check that can take new data is ${formatIsoInstant(next)}` : ''}.
    </p>
  );
}

function ModelMix({ list }: { readonly list: DemandForecastList }) {
  const byModel = list.counts_by_model?.[list.status_target];
  const byReason = list.counts_by_reason?.[list.status_target];
  if (!byModel || !byReason || list.item_count === null) return null;
  return (
    <section aria-label="Summary">
      <p>
        {list.item_count} products. {targetCopy[list.status_target]}:{' '}
        {modelNames.filter((model) => byModel[model]).map((model) => `${byModel[model]} by the ${modelCopy[model]}`).join(', ') || 'none forecast'}.
      </p>
      {skipReasons.some((reason) => byReason[reason]) ? (
        <ul aria-label="Products not forecast">
          {skipReasons.filter((reason) => byReason[reason]).map((reason) => <li key={reason}>{skipReasonCopy[reason]}: {byReason[reason]}</li>)}
        </ul>
      ) : null}
    </section>
  );
}

function ForecastTable({ list, viewKey, grain, onSelect }: {
  readonly list: DemandForecastList;
  readonly viewKey: ViewKey;
  readonly grain: string;
  readonly onSelect: (asin: string) => void;
}) {
  if (list.entries.length === 0) {
    return <p role="status">No product matches this filter. Choose All products to see the whole forecast.</p>;
  }
  return (
    <table>
      <caption>Forecast totals (p50) for the next {viewKey} {grain}s</caption>
      <thead>
        <tr>
          <th scope="col">Rank</th><th scope="col">Product</th>
          {targets.map((target) => <th key={target} scope="col">{targetCopy[target]}</th>)}
          <th scope="col">Model or reason</th><th scope="col">Recent {grain}s shipped below ordered</th>
        </tr>
      </thead>
      <tbody>
        {list.entries.map((entry) => {
          const status = entry.targets[list.status_target];
          return (
            <tr key={entry.asin}>
              <td>{entry.rank}</td>
              <th scope="row">
                <button type="button" className="link" onClick={() => onSelect(entry.asin)}>{entry.asin}</button>{' '}
                {entry.title ?? 'Title not available'}
              </th>
              {targets.map((target) => {
                const row = entry.targets[target];
                const total = row.p50_totals[viewKey];
                return <td key={target}>{total ? figureText(total, target, row.currency_code) : 'Not forecast'}</td>;
              })}
              <td>{status.chosen_model ? modelCopy[status.chosen_model] : status.withheld_reason ? skipReasonCopy[status.withheld_reason] : '—'}</td>
              <td>{entry.stockout_weeks_last_13}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

function StatusPanel({ status, onRetry }: { readonly status: Loaded<DemandForecastStatus>; readonly onRetry: () => void }) {
  if (status.state === 'loading') {
    return <div className="skeleton" aria-busy="true" aria-label="Loading the forecast status" style={{ minHeight: '6rem' }}/>;
  }
  if (status.state === 'failed') return <Unavailable onRetry={onRetry}/>;
  const value = status.value;
  const run = value.last_run;
  return (
    <section aria-label="Forecast status">
      <dl>
        <dt>Last forecast run</dt>
        <dd>
          {run === null ? 'No forecast run recorded yet' : `${runOutcomeCopy[run.outcome]}, ${formatIsoInstant(run.at)}`}
          {run?.failure_family ? `. ${failureFamilyCopy[run.failure_family]}` : ''}
        </dd>
        {value.hold !== 'none' ? (<><dt>Hold</dt><dd>{holdCopy[value.hold]}</dd></>) : null}
        {value.rollback_active ? (<><dt>Rollback</dt><dd>An operator rolled back to an earlier forecast.</dd></>) : null}
        <dt>Next check</dt><dd>{formatIsoInstant(value.next_check_at)}</dd>
        <dt>Next check that can take new data</dt><dd>{formatIsoInstant(value.next_new_input_check_at)}</dd>
      </dl>
    </section>
  );
}
