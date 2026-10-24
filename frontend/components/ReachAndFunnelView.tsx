// SPDX-License-Identifier: MIT-0
'use client';

/** Reach and funnel visual surface. Visible figures are served display text or absence text. */
import { useId, useRef, useState, type ReactNode } from 'react';

import { reachAnswer } from '@/lib/reach';
import { AnswerSentence } from './AnswerSentence';
import type {
  ReachAndFunnelArtifact,
  ReachChange,
  ReachFigure,
  ReachFrequency,
  ReachInsightId,
  ReachInsightSlot,
  ReachMarket,
} from '@/lib/reach';
import { formatCanonicalDecimal, formatCount } from '@/lib/exact-format';
import { metricLabel } from '@/lib/glossary';
import { EvidenceDisclosure } from './PrecisionPrimitives';
import { DataTable, DtBody, DtCell, DtColumn, DtHead, DtRow, DtRowHeader } from './DataTable';
import { InsightFocusDialog, type InsightDialogSlot, type InsightFocusSurface } from './InsightFocusDialog';

const NOT_AVAILABLE = 'Not available';
/** Customer words for the identity spaces and the platform return (cross-cutting §2.1). */
const ACCOUNT_IDS_LABEL = 'Signed-in shoppers';
/** The decoder pins the served `all_ids_label` to "All ID types", so this relabels exactly that one string. */
const ALL_IDS_LABEL = 'All shoppers incl. devices';
const PLATFORM_RETURN_LABEL = 'Return on ad spend, all Amazon ads';
/** UI refresh 3.3 (readout A.5): the reframed section names and lines, all browser-owned. */
export const SHOW_COUNTS = 'Show shopper counts';
const SHOPPERS = 'shoppers';
export const FUNNEL_HEADING = 'What they did next';
export const FREQUENCY_HEADING = 'When more ads stop paying';
const AXIS_NOTE = 'Bars: share who bought. Axis starts at 0%.';

/**
 * "Sales per ad dollar, all Amazon ads: {first} at {first bucket}, {last} at {last bucket}." from
 * the first and last served buckets (UX sweep fix 8). It names both and compares nothing.
 */
export function frequencyLead(frequency: ReachFrequency): string | null {
  const first = frequency.buckets[0];
  const last = frequency.buckets[frequency.buckets.length - 1];
  if (first === undefined || last === undefined || first === last) return null;
  if (first.platform_return.display_text === null || last.platform_return.display_text === null) return null;
  return `Sales per ad dollar, all Amazon ads: ${reachFigureText(first.platform_return)} at ${first.label}, ${reachFigureText(last.platform_return)} at ${last.label}.`;
}
function FrequencyLead({ frequency }: { readonly frequency: ReachFrequency }) {
  const text = frequencyLead(frequency);
  return text === null ? null : <p className="reach-lead" data-slot="rf-freq-lead">{text}</p>;
}
const INSIGHT_LABELS: Readonly<Record<ReachInsightId, string>> = {
  'frequency-saturation': 'Frequency',
  'full-funnel-association': 'Association, not cause',
  'reach-overlap': 'Overlap between DSP and sponsored ads',
};

const SERVED_COUNT = /^(0|[1-9][0-9]*)$/;
const SERVED_MULTIPLIER = /^(0|[1-9][0-9]*)\.([0-9]+)x$/;
/**
 * The served display text in the shared figure style (plan D4): a count's digits grouped and a
 * multiplier's "x" read as "×". It regroups the server's own string and never reads `value`, so
 * the server's rounding stands.
 */
export function reachDisplayText(text: string, unit: ReachFigure['unit']): string {
  if (unit === 'count' && SERVED_COUNT.test(text)) return formatCount(text);
  if (unit === 'multiplier' && SERVED_MULTIPLIER.test(text)) return `${formatCanonicalDecimal(text.slice(0, -1))}×`;
  return text;
}
/** A served figure's visible text: its regrouped display text, or "Not available". */
export function reachFigureText(figure: ReachFigure): string {
  return figure.display_text === null ? NOT_AVAILABLE : reachDisplayText(figure.display_text, figure.unit);
}
function Figure({ figure }: { readonly figure: ReachFigure }) {
  return <span className={figure.absence === null ? 'reach-figure' : 'reach-figure absent'}>
    <span className="reach-value">{reachFigureText(figure)}</span>
    {figure.absence === null ? null : <span className="reach-absence">{figure.absence.text}</span>}
  </span>;
}

/**
 * A served prior figure, named by the served prior period ("August 2026:") so it can't be read as
 * the current one (C6). Without a served label it reads "Previous {grain}:".
 */
function PriorFigure({ figure, label }: { readonly figure: ReachFigure; readonly label: string }) {
  return <span className="reach-prior"><span className="reach-prior-label">{`${label}:`}</span> <Figure figure={figure} /></span>;
}

const SERVED_SIGNED_COUNT = /^([+\u2212-]?)(0|[1-9][0-9]*)$/;
/** A served count change ("+155963") with its digits grouped ("+155,963"); any other text as served (T1L-4). */
export function reachChangeText(text: string): string {
  const match = SERVED_SIGNED_COUNT.exec(text);
  return match === null ? text : `${match[1]}${formatCount(match[2] ?? '')}`;
}

function ChangeFigure({ change }: { readonly change: ReachChange }) {
  return <span className={change.absence === null ? 'reach-change' : 'reach-change absent'}>
    {change.text === null ? change.absence?.text : reachChangeText(change.text)}
  </span>;
}

/**
 * OD-UX-7 (ADR-102 amendment): one visible caveat line per visual, the served first line, and the
 * full served caveat, every line, in one disclosure beneath it.
 */
function Caveat({ lines }: { readonly lines: readonly string[] }) {
  const [first] = lines;
  if (first === undefined) return null;
  return <div className="reach-caveat" role="note">
    <p>{first}</p>
    <EvidenceDisclosure summary="Why this is association, not cause">
      {lines.map((line) => <p key={line}>{line}</p>)}
    </EvidenceDisclosure>
  </div>;
}

/** The dialog's preamble keeps the full served caveat open: it is the dialog's only context. */
function CaveatLines({ lines }: { readonly lines: readonly string[] }) {
  return <div className="reach-caveat" role="note">{lines.map((line) => <p key={line}>{line}</p>)}</div>;
}

function SectionState({ text }: { readonly text: string }) {
  return <div className="callout reach-state"><h3>{NOT_AVAILABLE}</h3><p>{text}</p></div>;
}

function OverlapSection({ market, priorLabel }: { readonly market: ReachMarket; readonly priorLabel: string }) {
  const accountHeadingId = useId();
  const allIdsHeadingId = useId();
  const overlap = market.overlap;
  if (overlap === null) return null;
  return <section className="section reach-section">
    <h2>Overlap between DSP and sponsored ads</h2>
    {overlap.state !== 'figures' ? <SectionState text={overlap.text ?? ''} /> : <>
      <p className="reach-section-line">{overlap.subtitle}</p>
      <div className="reach-tiles">
        {overlap.tiles.map((tile) => <article className={tile.current.absence === null ? 'reach-tile' : 'reach-tile absent'} key={tile.metric}>
          <h3>{tile.label}</h3>
          <Figure figure={tile.current} />
          {tile.prior === null ? null : <PriorFigure figure={tile.prior} label={priorLabel} />}
          <ChangeFigure change={tile.change} />
        </article>)}
      </div>
      <div className="reach-overlap-bar" role="img" aria-label="Overlap between DSP and sponsored ads">
        {overlap.groups.map((group) => <span
          aria-hidden="true"
          className={`reach-overlap-segment ${group.exposure_group}`}
          key={group.exposure_group}
          style={{ flexGrow: group.account.value === null ? 0 : Number(group.account.value) }}
        />)}
      </div>
      {/* UX sweep fix 8 (readout A.5 rf-bar-*): each segment is named with its served count, not by colour alone. */}
      <ul className="reach-overlap-legend" data-slot="rf-bar-legend">
        {overlap.groups.map((group) => <li key={group.exposure_group}>
          <span aria-hidden="true" className={`reach-overlap-swatch ${group.exposure_group}`} />
          {group.label} <strong>{reachFigureText(group.account)}</strong> {SHOPPERS}
        </li>)}
      </ul>
      <details className="reach-values reach-counts" data-slot="rf-counts">
      <summary>{SHOW_COUNTS}</summary>
      <DataTable title={ACCOUNT_IDS_LABEL}>
        <DtHead><DtRow>
          <DtColumn><span className="sr-only">Overlap between DSP and sponsored ads</span></DtColumn>
          <DtColumn id={accountHeadingId}>{ACCOUNT_IDS_LABEL}</DtColumn>
          <DtColumn id={allIdsHeadingId}>{ALL_IDS_LABEL}</DtColumn>
        </DtRow></DtHead>
        <DtBody>{overlap.groups.map((group) => {
          const rowHeadingId = `${allIdsHeadingId}-${group.exposure_group}`;
          return <DtRow key={group.exposure_group}>
            <DtRowHeader id={rowHeadingId}>{group.label}</DtRowHeader>
            <DtCell headers={`${rowHeadingId} ${accountHeadingId}`} label={ACCOUNT_IDS_LABEL}>
              <span className="reach-cell-stack"><Figure figure={group.account} />{group.prior_account === null ? null : <PriorFigure figure={group.prior_account} label={priorLabel} />}<ChangeFigure change={group.change} /></span>
            </DtCell>
            <DtCell headers={`${rowHeadingId} ${allIdsHeadingId}`} label={ALL_IDS_LABEL}><Figure figure={group.all_ids} /></DtCell>
          </DtRow>;
        })}</DtBody>
      </DataTable>
      </details>
      <div className="reach-notes">{overlap.window_notes.map((line) => <p key={line}>{line}</p>)}</div>
    </>}
  </section>;
}

function FunnelSection({ market }: { readonly market: ReachMarket }) {
  const funnel = market.funnel;
  if (funnel === null) return null;
  return <section className="section reach-section">
    <h2>{FUNNEL_HEADING}</h2>
    {funnel.state !== 'figures' ? <SectionState text={funnel.text ?? ''} /> : <>
      <Caveat lines={funnel.caveat} />
      <DataTable title="Full funnel by exposure group">
        <DtHead><DtRow>
          <DtColumn><span className="sr-only">Full funnel by exposure group</span></DtColumn>
          {/* T2L-5: the served index note is the index column's basis, as the page design draws it. */}
          {funnel.columns.map((column, index) => <DtColumn key={column.metric} basis={index === funnel.columns.length - 1 ? funnel.index_note : null}>{metricLabel(column.metric, column.label)}</DtColumn>)}
        </DtRow></DtHead>
        <DtBody>{funnel.rows.map((row) => <DtRow key={row.exposure_group}>
          <DtRowHeader>{row.label}</DtRowHeader>
          {funnel.columns.map((column) => <DtCell key={column.metric} label={metricLabel(column.metric, column.label)}><Figure figure={row.figures[column.metric]} /></DtCell>)}
        </DtRow>)}</DtBody>
      </DataTable>
    </>}
  </section>;
}

function insightDialogSlot(slot: ReachInsightSlot): InsightDialogSlot {
  return { ...slot, next_step: null };
}

function InsightsSection({ artifact, market }: { readonly artifact: ReachAndFunnelArtifact; readonly market: ReachMarket }) {
  const [open, setOpen] = useState<ReachInsightId | null>(null);
  const opener = useRef<HTMLButtonElement | null>(null);
  const slots = market.insights.map(insightDialogSlot);
  const preambleFor = (slot: InsightDialogSlot): ReactNode => {
    if (slot.id === 'full-funnel-association' && market.funnel?.state === 'figures') return <CaveatLines lines={market.funnel.caveat} />;
    if (slot.id === 'frequency-saturation' && market.frequency.state === 'figures') return <CaveatLines lines={market.frequency.caveat} />;
    return null;
  };
  const surface: InsightFocusSurface = {
    labelFor: (slotId) => INSIGHT_LABELS[slotId as ReachInsightId] ?? slotId,
    preambleFor,
    slots,
    window: artifact.period,
  };
  return <section className="section reach-section">
    <h2>Insights</h2>
    <div className="reach-insights">
      {market.insights.map((slot) => <article className={slot.state === 'filled' ? 'reach-insight' : 'reach-insight absent'} key={slot.id}>
        <h3>{INSIGHT_LABELS[slot.id]}</h3>
        <p>{slot.text ?? slot.absence_reason}</p>
        <button
          aria-label={`Read more: ${INSIGHT_LABELS[slot.id]}`}
          type="button"
          onClick={(event) => { opener.current = event.currentTarget; setOpen(slot.id); }}
        >Read more ›</button>
      </article>)}
    </div>
    {open === null ? null : <InsightFocusDialog
      onClose={() => setOpen(null)}
      onSlotChange={(slotId) => setOpen(slotId as ReachInsightId)}
      opener={opener}
      slotId={open}
      surface={surface}
      surfaceKey={`reach:${artifact.period.start}:${artifact.period.end}`}
    />}
  </section>;
}

function FrequencyChart({ frequency }: { readonly frequency: ReachFrequency }) {
  // T2L-2: a zero-based axis whose top is the largest served rate, so the bars read; display scale only.
  const served = frequency.buckets.flatMap((bucket) => bucket.purchaser_rate.value === null ? [] : [Number(bucket.purchaser_rate.value)]);
  const top = Math.max(0, ...served.filter((value) => Number.isFinite(value)));
  const height = (value: string | null) => value === null || top === 0 ? 0 : Math.max(0, Math.min(1, Number(value) / top)) * 100;
  return <>
    <div className="reach-chart" role="img" aria-label="Purchaser rate by number of ads seen" tabIndex={0}>
      <div className="reach-chart-bars">
        {frequency.buckets.map((bucket) => <div className={bucket.purchaser_rate.absence === null ? 'reach-chart-item' : 'reach-chart-item gap'} key={bucket.bucket}>
          <div className="reach-bar-space" aria-hidden="true">
            {bucket.purchaser_rate.value === null ? null : <span className="reach-bar" style={{ height: `${height(bucket.purchaser_rate.value)}%` }} />}
          </div>
          <span className="reach-chart-value">{bucket.purchaser_rate.display_text ?? NOT_AVAILABLE}</span>
          {bucket.purchaser_rate.absence === null ? null : <span className="reach-absence">{bucket.purchaser_rate.absence.text}</span>}
          <span className="reach-bucket-label">{bucket.label}</span>
        </div>)}
      </div>
    </div>
    <p className="reach-notes">{AXIS_NOTE}</p>
    {/* UX sweep fix 8 (readout A.5 rf-freq-return-*): the served platform return under each bucket. */}
    <div className="reach-return-row" role="group" aria-label={PLATFORM_RETURN_LABEL} data-slot="rf-freq-return">
      {frequency.buckets.map((bucket) => <span key={bucket.bucket}><span className="sr-only">{`${bucket.label}: `}</span><strong>{reachFigureText(bucket.platform_return)}</strong></span>)}
    </div>
    <details className="reach-values">
      <summary>Show values</summary>
      <DataTable title="Purchaser rate by number of ads seen">
        <DtHead><DtRow><DtColumn>Ads seen</DtColumn><DtColumn>Purchaser rate</DtColumn><DtColumn>{PLATFORM_RETURN_LABEL}</DtColumn></DtRow></DtHead>
        <DtBody>{frequency.buckets.map((bucket) => <DtRow key={bucket.bucket}>
          <DtRowHeader>{bucket.label}</DtRowHeader>
          <DtCell label="Purchaser rate"><Figure figure={bucket.purchaser_rate} /></DtCell>
          <DtCell label={PLATFORM_RETURN_LABEL}><Figure figure={bucket.platform_return} /></DtCell>
        </DtRow>)}</DtBody>
      </DataTable>
    </details>
  </>;
}

function FrequencyTables({ frequency }: { readonly frequency: ReachFrequency }) {
  const table = frequency.per_channel;
  if (table === null) return null;
  // E3 (OD-UX-9): the decoder admits only the served "not available per channel" absence in this
  // grid (lib/reach.ts), so it reads as one line per channel instead of rows of the same words.
  const reason = table.rows[0] === undefined ? null : Object.values(table.rows[0].cells)[0]?.absence?.text ?? null;
  // The return row above is the platform view; its served basis reads once beneath it.
  return <div className="reach-frequency-tables">
    <p className="reach-notes" data-slot="rf-freq-return-basis">{`${PLATFORM_RETURN_LABEL}: ${table.platform_label}`}</p>
    <div className="reach-channel-absent" role="group" aria-label="Return per channel by frequency">
      <h3>Return per channel by frequency</h3>
      <p className="reach-notes">{table.label}</p>
      <ul className="reach-notes">{table.rows.map((row) => <li key={row.ad_product_key}><strong>{row.label}</strong></li>)}</ul>
      {/* T2L-5: the served reason completes the served cell text, as the page design's legend reads. */}
      <p className="reach-notes">{reason === null ? table.unavailable_reason : `${reason}: ${table.unavailable_reason}`}</p>
      <p className="reach-notes">{table.foot}</p>
    </div>
  </div>;
}

function FrequencySection({ market }: { readonly market: ReachMarket }) {
  const frequency = market.frequency;
  return <section className="section reach-section">
    <h2>{FREQUENCY_HEADING}</h2>
    {frequency.state !== 'figures' ? <SectionState text={frequency.text ?? ''} /> : <>
      {/* UX sweep fix 8: the section opens with the served return at the first and last bucket. */}
      <FrequencyLead frequency={frequency} />
      <Caveat lines={frequency.caveat} />
      <p className="reach-section-line">{frequency.axis_label}</p>
      <FrequencyChart frequency={frequency} />
      <FrequencyTables frequency={frequency} />
    </>}
  </section>;
}

export function ReachAndFunnelView({ artifact, priorPeriodLabel = null }: {
  readonly artifact: ReachAndFunnelArtifact;
  /** The envelope's served `temporal.comparison.prior_period_label`, when it serves one. */
  readonly priorPeriodLabel?: string | null;
}) {
  if (artifact.outcome !== 'complete') return <div className="reach-view">
    <section className="callout reach-page-state" role="status">
      <h2>{NOT_AVAILABLE}</h2>
      {artifact.outcome === 'insufficient' ? <p>too few shoppers, or none</p> : null}
    </section>
  </div>;
  const market = artifact.markets[0];
  if (market === undefined) return null;
  return <div className="reach-view">
    <AnswerSentence answer={reachAnswer(artifact, reachFigureText)} />
    <OverlapSection market={market} priorLabel={priorPeriodLabel ?? `Previous ${artifact.period.grain}`} />
    <FunnelSection market={market} />
    <InsightsSection artifact={artifact} market={market} />
    <FrequencySection market={market} />
    <details className="reach-about">
      <summary>About these figures</summary>
      <div>{artifact.boundary.map((line) => <p key={line}>{line}</p>)}</div>
    </details>
  </div>;
}
