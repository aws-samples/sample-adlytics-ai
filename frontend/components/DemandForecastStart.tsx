// SPDX-License-Identifier: MIT-0
'use client';
import { useId, type FormEvent, type MouseEvent, type ReactNode, type RefObject } from 'react';
import { formatCount } from '@/lib/exact-format';
import {
  INFO,
  plural,
  shortlistEntries,
  skipReasonCopy,
  skipReasons,
  startCopy,
  type DemandForecastList,
  type SkipReason,
  type ViewKey,
} from '@/lib/demand-forecast';
import { DataTable, DtBody, DtCell, DtColumn, DtHead, DtRow, DtRowHeader } from './DataTable';
import { InfoTip } from './InfoTip';

/**
 * The Demand forecast opening view (hybrid design H1, H5, H6): "Check these first", the one search
 * box, the reasons line and Browse all. Every row, count and label reads the start list: the
 * unfiltered page 1 of the generation on screen (O-01, G-08).
 */

/** A plain click opens the view in place; a modified click keeps the browser's own link handling. */
function inPlace(event: MouseEvent<HTMLAnchorElement>): boolean {
  if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return false;
  event.preventDefault();
  return true;
}

/**
 * The one search box (OD-H1): an ASIN today. It is one component so live suggestions can join it
 * later without a layout change. Its outcome line is drawn by the card, full width (B-08).
 */
export function SearchBox({ busy, describedBy, id, input, invalid, onChange, onSubmit, value }: {
  readonly busy: boolean;
  readonly describedBy: string | null;
  readonly id: string;
  readonly input: RefObject<HTMLInputElement | null>;
  readonly invalid: boolean;
  readonly onChange: (value: string) => void;
  readonly onSubmit: (event: FormEvent) => void;
  readonly value: string;
}) {
  const hint = `${id}-hint`;
  return (
    <form role="search" className="df-search" onSubmit={onSubmit} noValidate aria-busy={busy}>
      <label htmlFor={id}>{startCopy.searchLabel}</label>
      <span className="df-search-row">
        <input
          id={id}
          ref={input}
          className="text-input"
          value={value}
          onChange={(event) => onChange(event.target.value)}
          aria-invalid={invalid}
          aria-describedby={describedBy === null ? hint : `${hint} ${describedBy}`}
          autoComplete="off"
          spellCheck={false}
        />
        <button type="submit" className="btn" aria-disabled={busy ? 'true' : undefined}>{busy ? startCopy.finding : 'Find'}</button>
      </span>
      <span id={hint} className="df-hint">10 letters or digits</span>
    </form>
  );
}

/** The card's frame: the title and basis lines left, the search right, then the outcome line (B-08). */
function StartCard({ basis, children, outcome, search }: {
  readonly basis: ReactNode;
  readonly children: ReactNode;
  readonly outcome: ReactNode;
  readonly search: ReactNode;
}) {
  const title = useId();
  return (
    <section className="card df-start" aria-labelledby={title}>
      <div className="df-start-head">
        <div className="df-start-title">
          <h2 id={title}>{startCopy.title}</h2>
          {basis}
        </div>
        <div className="df-start-search">{search}</div>
      </div>
      {outcome}
      {children}
    </section>
  );
}

/** H1 and H5: the shortlist (or why there is none), the reasons line and Browse all. */
export function StartView({ list, onBrowse, onOpen, onReason, outcome, search, viewWeeks }: {
  readonly list: DemandForecastList;
  readonly onBrowse: () => void;
  readonly onOpen: (asin: string) => void;
  readonly onReason: (reason: SkipReason) => void;
  readonly outcome: ReactNode;
  readonly search: ReactNode;
  readonly viewWeeks: number;
}) {
  const horizon = list.horizon;
  const first = horizon?.weeks[0];
  if (horizon === null || first === undefined) return null;
  const entries = shortlistEntries(list);
  const basis = <>
    <p className="df-basis">
      {startCopy.basis(horizon.default_view_weeks)}
      {viewWeeks === horizon.default_view_weeks ? null : <>. {startCopy.keep(horizon.default_view_weeks)}</>}
    </p>
    <p className="df-short-hint">{startCopy.hint}</p>
  </>;
  const rank = INFO.rank(horizon.default_view_weeks, first);
  const key = String(viewWeeks) as ViewKey;
  return (
    <StartCard basis={entries.length === 0 ? null : basis} outcome={outcome} search={search}>
      {entries.length === 0 ? <p className="df-short-empty">{startCopy.empty(list.status_target)}</p> : (
        <div className="df-shortlist">
          {/* The stacked form's copies of the header pop-ups, in column order (G-04). */}
          <p className="df-short-tips">
            <span>Rank<InfoTip title={rank.title}>{rank.text}</InfoTip></span>
            <span>Weeks shipped less than ordered<InfoTip title={INFO.short.title}>{INFO.short.text}</InfoTip></span>
          </p>
          <DataTable className="df-short" captionHidden title={startCopy.caption(viewWeeks, first)}>
            <DtHead>
              <DtRow>
                <DtColumn>Rank<InfoTip title={rank.title}>{rank.text}</InfoTip></DtColumn>
                <DtColumn className="l">Product</DtColumn>
                <DtColumn basis={`${viewWeeks} weeks`}>Likely units ordered</DtColumn>
                <DtColumn basis="of the last 13">Weeks shipped less than ordered<InfoTip title={INFO.short.title} className="end">{INFO.short.text}</InfoTip></DtColumn>
              </DtRow>
            </DtHead>
            <DtBody>
              {entries.map((entry) => {
                const total = entry.targets.units_ordered.p50_totals[key];
                return (
                  <DtRow key={entry.asin}>
                    <DtCell label="Rank" className="l">{entry.rank}</DtCell>
                    <DtRowHeader className="row-pick">
                      <button type="button" data-forecast-row={entry.asin} onClick={() => onOpen(entry.asin)}>
                        {entry.title ?? 'Untitled product'}
                        {entry.title_truncated ? <span className="sr-only"> (name shortened)</span> : null}
                      </button>
                      <span className="sub">{entry.asin}</span>
                    </DtRowHeader>
                    <DtCell label={`Likely units ordered, ${viewWeeks} weeks`}>{total === null ? 'No forecast' : formatCount(total.display)}</DtCell>
                    <DtCell label="Weeks shipped less than ordered, of the last 13">{entry.stockout_weeks_last_13}</DtCell>
                  </DtRow>
                );
              })}
            </DtBody>
          </DataTable>
        </div>
      )}
      <ReasonsLine list={list} onReason={onReason}/>
      <p className="df-browse">
        <a href="?view=all" data-forecast-browse="" onClick={(event) => { if (inPlace(event)) onBrowse(); }}>
          {startCopy.browse(list.item_count)}<span aria-hidden="true"> ›</span>
        </a>
      </p>
    </StartCard>
  );
}

/** L-REASONS (O-07): one item per reason with products; each opens Browse all on that filter (G-05). */
function ReasonsLine({ list, onReason }: { readonly list: DemandForecastList; readonly onReason: (reason: SkipReason) => void }) {
  const byReason = list.counts_by_reason?.[list.status_target] ?? {};
  const present = skipReasons.filter((reason) => (byReason[reason] ?? 0) > 0);
  if (present.length === 0) return null;
  return (
    <p className="df-reasons">
      {startCopy.reasonsLead(list.status_target)}{' '}
      {present.map((reason, index) => {
        const count = byReason[reason] ?? 0;
        return (
          <span key={reason}>
            {index === 0 ? null : ' · '}
            {skipReasonCopy[reason]}: {plural(count, 'product', 'products')}{' '}
            <button type="button" className="btn link" data-forecast-reason={reason} aria-label={startCopy.reasonShow(count, reason)} onClick={() => onReason(reason)}>Show them</button>
          </span>
        );
      })}
    </p>
  );
}

/**
 * H6: the scope card, answer line and ten shortlist rows in `.skel` shapes; the search box works
 * at once, because it needs no data.
 */
export function StartSkeleton({ outcome, search }: { readonly outcome: ReactNode; readonly search: ReactNode }) {
  return (<>
    <section className="df-skeleton" aria-busy="true" aria-label="Loading the forecast">
      <div className="band df-scope">
        <div className="df-pills"><span className="skel h-label"/><span className="skel h-label"/><span className="skel h-label"/></div>
        <span className="skel df-skel-seg"/>
      </div>
      <span className="skel h-line"/>
    </section>
    <StartCard basis={<span className="skel h-line" aria-hidden="true"/>} outcome={outcome} search={search}>
      <div className="df-skeleton df-short-skel" aria-busy="true" aria-label="Loading Check these first">
        {Array.from({ length: 10 }, (_unused, index) => <span key={index} className="skel h-row"/>)}
      </div>
    </StartCard>
  </>);
}
