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
import { DataTable, DtBody, DtCell, DtColumn, DtHead, DtRow, DtRowHeader } from './DataTable';
import { InsightFocusDialog, type InsightDialogSlot, type InsightFocusSurface } from './InsightFocusDialog';

const NOT_AVAILABLE = 'Not available';
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

function ChangeFigure({ change }: { readonly change: ReachChange }) {
  return <span className={change.absence === null ? 'reach-change' : 'reach-change absent'}>
    {change.text ?? change.absence?.text}
  </span>;
}

function Caveat({ lines }: { readonly lines: readonly string[] }) {
  return <div className="reach-caveat" role="note">{lines.map((line) => <p key={line}>{line}</p>)}</div>;
}

function SectionState({ text }: { readonly text: string }) {
  return <div className="callout reach-state"><h3>{NOT_AVAILABLE}</h3><p>{text}</p></div>;
}

function OverlapSection({ market }: { readonly market: ReachMarket }) {
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
          {tile.prior === null ? null : <Figure figure={tile.prior} />}
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
      <DataTable title="Amazon account IDs">
        <DtHead><DtRow>
          <DtColumn><span className="sr-only">Overlap between DSP and sponsored ads</span></DtColumn>
          <DtColumn id={accountHeadingId}>Amazon account IDs</DtColumn>
          <DtColumn id={allIdsHeadingId}>{overlap.all_ids_label}</DtColumn>
        </DtRow></DtHead>
        <DtBody>{overlap.groups.map((group) => {
          const rowHeadingId = `${allIdsHeadingId}-${group.exposure_group}`;
          return <DtRow key={group.exposure_group}>
            <DtRowHeader id={rowHeadingId}>{group.label}</DtRowHeader>
            <DtCell headers={`${rowHeadingId} ${accountHeadingId}`} label="Amazon account IDs">
              <span className="reach-cell-stack"><Figure figure={group.account} />{group.prior_account === null ? null : <Figure figure={group.prior_account} />}<ChangeFigure change={group.change} /></span>
            </DtCell>
            <DtCell headers={`${rowHeadingId} ${allIdsHeadingId}`} label={overlap.all_ids_label}><Figure figure={group.all_ids} /></DtCell>
          </DtRow>;
        })}</DtBody>
      </DataTable>
      <div className="reach-notes">{overlap.window_notes.map((line) => <p key={line}>{line}</p>)}</div>
    </>}
  </section>;
}

function FunnelSection({ market }: { readonly market: ReachMarket }) {
  const funnel = market.funnel;
  if (funnel === null) return null;
  return <section className="section reach-section">
    <h2>Full funnel by exposure group</h2>
    {funnel.state !== 'figures' ? <SectionState text={funnel.text ?? ''} /> : <>
      <Caveat lines={funnel.caveat} />
      <DataTable title="Full funnel by exposure group">
        <DtHead><DtRow>
          <DtColumn><span className="sr-only">Full funnel by exposure group</span></DtColumn>
          {funnel.columns.map((column) => <DtColumn key={column.metric}>{column.label}</DtColumn>)}
        </DtRow></DtHead>
        <DtBody>{funnel.rows.map((row) => <DtRow key={row.exposure_group}>
          <DtRowHeader>{row.label}</DtRowHeader>
          {funnel.columns.map((column) => <DtCell key={column.metric} label={column.label}><Figure figure={row.figures[column.metric]} /></DtCell>)}
        </DtRow>)}</DtBody>
      </DataTable>
      {funnel.index_note === null ? null : <p className="reach-notes">{funnel.index_note}</p>}
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
    if (slot.id === 'full-funnel-association' && market.funnel?.state === 'figures') return <Caveat lines={market.funnel.caveat} />;
    if (slot.id === 'frequency-saturation' && market.frequency.state === 'figures') return <Caveat lines={market.frequency.caveat} />;
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
  return <>
    <div className="reach-chart" role="img" aria-label="Purchaser rate by number of ads seen" tabIndex={0}>
      <div className="reach-chart-bars">
        {frequency.buckets.map((bucket) => <div className={bucket.purchaser_rate.absence === null ? 'reach-chart-item' : 'reach-chart-item gap'} key={bucket.bucket}>
          <div className="reach-bar-space" aria-hidden="true">
            {bucket.purchaser_rate.value === null ? null : <span className="reach-bar" style={{ height: `${Math.max(0, Math.min(1, Number(bucket.purchaser_rate.value))) * 100}%` }} />}
          </div>
          <span className="reach-chart-value">{bucket.purchaser_rate.display_text ?? NOT_AVAILABLE}</span>
          {bucket.purchaser_rate.absence === null ? null : <span className="reach-absence">{bucket.purchaser_rate.absence.text}</span>}
          <span className="reach-bucket-label">{bucket.label}</span>
        </div>)}
      </div>
    </div>
    <details className="reach-values">
      <summary>Show values</summary>
      <DataTable title="Purchaser rate by number of ads seen">
        <DtHead><DtRow><DtColumn>Ads seen</DtColumn><DtColumn>Purchaser rate</DtColumn></DtRow></DtHead>
        <DtBody>{frequency.buckets.map((bucket) => <DtRow key={bucket.bucket}>
          <DtRowHeader>{bucket.label}</DtRowHeader>
          <DtCell label="Purchaser rate"><Figure figure={bucket.purchaser_rate} /></DtCell>
        </DtRow>)}</DtBody>
      </DataTable>
    </details>
  </>;
}

function FrequencyTables({ frequency }: { readonly frequency: ReachFrequency }) {
  const table = frequency.per_channel;
  if (table === null) return null;
  return <div className="reach-frequency-tables">
    <div>
      <DataTable title="Return per channel by frequency" note={table.label} nameWithNote>
        <DtHead><DtRow><DtColumn>Channel</DtColumn>{frequency.buckets.map((bucket) => <DtColumn key={bucket.bucket}>{bucket.label}</DtColumn>)}</DtRow></DtHead>
        <DtBody>{table.rows.map((row) => <DtRow key={row.ad_product_key}>
          <DtRowHeader>{row.label}</DtRowHeader>
          {frequency.buckets.map((bucket) => <DtCell key={bucket.bucket} label={bucket.label}><Figure figure={row.cells[bucket.bucket]!} /></DtCell>)}
        </DtRow>)}</DtBody>
      </DataTable>
      <p className="reach-notes">{table.unavailable_reason}</p>
      <p className="reach-notes">{table.foot}</p>
    </div>
    <DataTable title="Platform return" note={table.platform_label} nameWithNote>
      <DtHead><DtRow><DtColumn>Ads seen</DtColumn><DtColumn>Platform return</DtColumn></DtRow></DtHead>
      <DtBody>{frequency.buckets.map((bucket) => <DtRow key={bucket.bucket}>
        <DtRowHeader>{bucket.label}</DtRowHeader>
        <DtCell label="Platform return"><Figure figure={bucket.platform_return} /></DtCell>
      </DtRow>)}</DtBody>
    </DataTable>
  </div>;
}

function FrequencySection({ market }: { readonly market: ReachMarket }) {
  const frequency = market.frequency;
  return <section className="section reach-section">
    <h2>Frequency</h2>
    {frequency.state !== 'figures' ? <SectionState text={frequency.text ?? ''} /> : <>
      <Caveat lines={frequency.caveat} />
      <p className="reach-section-line">{frequency.axis_label}</p>
      <FrequencyChart frequency={frequency} />
      <FrequencyTables frequency={frequency} />
    </>}
  </section>;
}

export function ReachAndFunnelView({ artifact }: { readonly artifact: ReachAndFunnelArtifact }) {
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
    <OverlapSection market={market} />
    <FunnelSection market={market} />
    <InsightsSection artifact={artifact} market={market} />
    <FrequencySection market={market} />
    <details className="reach-about">
      <summary>About these figures</summary>
      <div>{artifact.boundary.map((line) => <p key={line}>{line}</p>)}</div>
    </details>
  </div>;
}
