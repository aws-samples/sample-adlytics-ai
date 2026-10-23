// SPDX-License-Identifier: MIT-0
'use client';

/**
 * The shared hardened metric-focus dialog (docs/ui/metric-focus-contract.md).
 *
 * One composition on every focusable surface: identity head with the dictionary-served
 * definition line, visual evidence with exact values, the server-computed facts strip,
 * deterministic drivers, one prompted interpretation slot, the next-step chips, and the
 * basis boundary. Every figure is a returned server value; the browser derives nothing.
 */

import type { ReactNode, RefObject } from 'react';
import { useEffect, useState } from 'react';

import { formatCurrency, formatIsoDate, formatIsoDateRange } from '@/lib/exact-format';
import type { InsightNextStepHandlers } from '@/lib/insight-next-step';
import type { MetricFocusDriverEntry, MetricFocusV4, MetricFocusValue } from '@/lib/metric-focus';
import { provenanceLine } from '@/lib/provenance';
import { ChangeChip, ChartLegend, displayValueText, numberText, SeriesChart, type ValueKind } from './ChartPrimitives';
import { seedAssistantQuestion } from './ChatPanel';
import { Dialog } from './Dialog';
import { InsightDeepSection, type InsightDeepSurface } from './InsightDeepSection';
import { InsightCardBody, InsightNextStepControl, insightTreatment, useRendersNextStep } from './InsightCard';

export interface MetricFocusSurface {
  /** Parent-owned cache: survives dialog unmount, dies when the owning page does. */
  readonly cache: Map<string, MetricFocusV4>;
  /** Exact parent identity: artifact revision plus capability where one exists. */
  readonly cacheScope: string;
  /** Fetch one focused metric's v4 result for the page's resolved window. */
  readonly fetch: (metricId: string, signal: AbortSignal) => Promise<MetricFocusV4>;
  /** Resolve an on-request deep section after the card-side focus is filled. */
  readonly deepFor: (focus: MetricFocusV4) => InsightDeepSurface | null;
  /** Display label per focusable metric, for the switcher and next-check chips. */
  readonly labelFor: (metricId: string) => string;
  /** The page-declared focusable roster, in switcher order. */
  readonly neighbors: readonly string[];
  /** Optional served caveat copy that must lead this metric's dialog body. */
  readonly preambleFor?: (metricId: string) => ReactNode;
  /** Server-composed assistant seed naming the focused scope in words. */
  readonly askSeed: (focus: MetricFocusV4) => string;
  /** The page's resolved window; a result for another window is refused. */
  readonly window: { readonly end: string; readonly start: string };
  /** The tab the dialog opens on; a served same-tab step names it (ADR-122). */
  readonly pageId: string;
}

type FocusState =
  | { readonly kind: 'loading' }
  | { readonly kind: 'ready'; readonly focus: MetricFocusV4 }
  | { readonly kind: 'unavailable' };

function valueKind(focus: MetricFocusV4): ValueKind {
  return focus.metric.unit.kind;
}

function focusValueText(focus: MetricFocusV4, value: MetricFocusValue): string {
  return displayValueText(value, valueKind(focus));
}

function driverGrainCopy(grain: 'campaign' | 'day' | 'ratio_component' | 'product'): string {
  return {
    campaign: 'per campaign',
    day: 'day steps',
    product: 'per product',
    ratio_component: 'ratio components',
  }[grain];
}

/**
 * One sign rule for a served currency change: no sign on exactly "0.00", the served "-" on a
 * negative, and "+" otherwise. Count and ratio entries keep their exact signed change.
 */
function driverChangeText(entry: MetricFocusDriverEntry): string {
  if (entry.units.kind === 'currency' && entry.display_change !== null && entry.units.currency_code !== null) {
    const text = formatCurrency(entry.display_change, entry.units.currency_code);
    return entry.display_change === '0.00' || entry.display_change.startsWith('-') ? text : `+${text}`;
  }
  const change = typeof entry.change === 'number' ? String(entry.change) : entry.change;
  return change.startsWith('-') ? change : `+${change}`;
}
/**
 * A served chip replaces the share when the share is not meaningful; otherwise the share
 * carries the basis's served suffix, so the dialog never composes what it is a share of.
 */
function driverWhat(entry: MetricFocusDriverEntry, shareSuffix: string | null): string {
  const signed = driverChangeText(entry);
  if (entry.chip !== null) return `${signed} · ${entry.chip}`;
  if (entry.share_of_move === null) return signed;
  return shareSuffix === null ? `${signed} · ${entry.share_of_move} of the move` : `${signed} · ${entry.share_of_move} ${shareSuffix}`;
}

function basisCopy(focus: MetricFocusV4): string {
  return focus.metric.basis === 'retail_actual' ? 'Retail actuals' : 'Ad-attributed';
}

function FocusEvidence({ focus }: { readonly focus: MetricFocusV4 }) {
  const series = focus.series;
  if (series === null) {
    return <p className="state-note"><span className="badge">No trend</span>A returned time series is not available for this metric. Review the server-computed facts and drivers beside it.</p>;
  }
  const hasPrior = series.points.some((point) => point.prior !== null);
  const kind = valueKind(focus);
  const currency = focus.metric.unit.currency_code;
  return <>
    <SeriesChart
      ariaLabel={`${focus.metric.label} across the returned ${series.display_grain} reporting values`}
      currencyCode={currency}
      current={series.points.map((point) => point.current)}
      dates={series.points.map((point) => point.date)}
      kind={kind}
      {...(hasPrior ? { prior: series.points.map((point) => point.prior ?? '') } : {})}
      zeroBased={kind !== 'ratio'}
    />
    <ChartLegend prior={hasPrior} />
    <details className="exact">
      <summary>Show exact values</summary>
      <div className="scroll" tabIndex={0}>
        <table>
          <caption className="sr-only">Exact returned {focus.metric.label} values from {formatIsoDateRange(focus.period.start, focus.period.end)}</caption>
          <thead><tr><th scope="col">Series</th>{series.points.map((point) => <th scope="col" key={point.date}>{formatIsoDate(point.date)}</th>)}</tr></thead>
          <tbody>
            <tr><th scope="row">Current</th>{series.points.map((point) => <td key={point.date}>{numberText(point.current, kind, currency)}</td>)}</tr>
            {hasPrior ? <tr><th scope="row">Aligned prior</th>{series.points.map((point) => <td key={point.date}>{point.prior === null ? '—' : numberText(point.prior, kind, currency)}</td>)}</tr> : null}
          </tbody>
        </table>
      </div>
    </details>
  </>;
}

function FocusFacts({ focus }: { readonly focus: MetricFocusV4 }) {
  return <div className="facts" aria-label="Server-computed facts about this metric in this window">
    {focus.facts.map((fact) => <span className="fact" key={fact.id}>
      <span className="f-k">{fact.label}</span>
      <span className="f-v">{fact.value}{fact.qualifier === null ? null : <> <span className="sub-note">{fact.qualifier}</span></>}</span>
    </span>)}
  </div>;
}

function FocusDrivers({ focus }: { readonly focus: MetricFocusV4 }) {
  const drivers = focus.drivers;
  if ('absent_reason' in drivers) {
    return <div className="drivers">
      <h3 className="drivers-heading">Drivers</h3>
      <p className="quiet">{drivers.absent_reason}</p>
    </div>;
  }
  const shareSuffix = drivers.scope?.share_suffix ?? null;
  const summary = focus.drivers_summary;
  return <div className="drivers">
    <h3 className="drivers-heading">Drivers · {driverGrainCopy(drivers.grain)}, server-computed</h3>
    {drivers.scope === null ? null : <p className="sub-note">{drivers.scope.label}</p>}
    <div className="movers-stack" aria-label={`Top contributors to the window-over-window ${focus.metric.label} change`}>
      {drivers.entries.map((entry) => <div className="mover" key={entry.name}>
        <span>
          <span className="who">{entry.name}</span>
          <span className="what">{driverWhat(entry, shareSuffix)}</span>
        </span>
        <ChangeChip change={entry.delta} />
      </div>)}
    </div>
    {summary === null ? null : <div className="drivers-summary" aria-label="How these changes add up">
      {summary.lines.filter((line) => line !== drivers.scope?.label).map((line) => <p className="quiet" key={line}>{line}</p>)}
    </div>}
  </div>;
}

/**
 * The prompted interpretation slot renders its absences under the insight-presentation
 * contract: the served reason verbatim with the treatment its served class selects,
 * superseding this dialog's earlier generic fallback line for served slots.
 */
function FocusInterpretation({ focus, loading }: { readonly focus: MetricFocusV4 | null; readonly loading: boolean }) {
  const slot = focus?.insight ?? null;
  const treatment = loading || slot === null ? null : insightTreatment(slot);
  const cardClass = treatment === null || treatment.cardClass === '' ? 'narr-card' : `narr-card ${treatment.cardClass}`;
  return <article className={cardClass} aria-live="polite" aria-busy={loading}>
    {loading || slot === null
      ? <div className="purpose"><h3>Interpretation</h3></div>
      : <InsightCardBody slot={slot} title="Interpretation" />}
    {loading ? <><span className="skel h-line" /><span className="skel h-line" /><span className="skel h-line short" /><span className="sr-only">Preparing the metric explanation</span></> : null}
    {!loading && slot === null ? <p className="quiet">An interpretation is not available for these results yet. Review the delivered values or choose another reporting window.</p> : null}
  </article>;
}

export function MetricFocusDialog({ metricId, onClose, onMetricChange, opener, surface, surfaceKey }: {
  readonly metricId: string;
  readonly onClose: () => void;
  /** The tile that opened the dialog; focus returns to it on close. */
  readonly opener?: RefObject<HTMLElement | null> | undefined;
  readonly onMetricChange: (metricId: string) => void;
  readonly surface: MetricFocusSurface;
  readonly surfaceKey: string;
}) {
  // The fetched state carries the identity it was resolved for; a key mismatch
  // derives 'loading' during render, so the effect never needs a synchronous reset.
  const [fetched, setState] = useState<{ readonly key: string; readonly state: FocusState }>({ key: '', state: { kind: 'loading' } });
  const headingId = 'metric-focus-heading';
  const neighbors = surface.neighbors;
  const index = neighbors.indexOf(metricId);
  const windowText = formatIsoDateRange(surface.window.start, surface.window.end);
  // The parent-owned cache is read during render so a retained focus serves without
  // a state round-trip; the effect only fetches identities the cache does not hold.
  const cacheKey = `${surfaceKey}:${surface.cacheScope}:${metricId}:${surface.window.start}:${surface.window.end}`;
  const cached = surface.cache.get(cacheKey);
  useEffect(() => {
    if (surface.cache.get(cacheKey) !== undefined) return undefined;
    const controller = new AbortController();
    const publish = (next: FocusState) => setState({ key: cacheKey, state: next });
    void surface.fetch(metricId, controller.signal).then((focus) => {
      // Window-scope integrity: a focus for another resolved window never renders here.
      if (controller.signal.aborted || focus.period.start !== surface.window.start || focus.period.end !== surface.window.end) {
        if (!controller.signal.aborted) publish({ kind: 'unavailable' });
        return;
      }
      surface.cache.set(cacheKey, focus);
      publish({ focus, kind: 'ready' });
    }).catch(() => { if (!controller.signal.aborted) publish({ kind: 'unavailable' }); });
    return () => controller.abort();
  }, [cacheKey, metricId, surface, surfaceKey]);
  const hop = (direction: -1 | 1) => {
    const next = neighbors[(index + direction + neighbors.length) % neighbors.length];
    if (next !== undefined) onMetricChange(next);
  };
  const state: FocusState = fetched.key === cacheKey ? fetched.state : { kind: 'loading' };
  const focus = cached ?? (state.kind === 'ready' ? state.focus : null);
  const loading = focus === null && state.kind === 'loading';
  const unavailable = focus === null && state.kind === 'unavailable';
  const deepSurface = focus?.insight.state === 'filled' ? surface.deepFor(focus) : null;
  const label = focus?.metric.label ?? surface.labelFor(metricId);
  const stepHandlers: InsightNextStepHandlers = { open_metric_focus: (target) => onMetricChange(target.metric), focusableMetrics: neighbors };
  const servedStep = focus?.insight.state === 'filled' ? focus.insight.next_step : null;
  const rendersStep = useRendersNextStep(servedStep, stepHandlers, surface.pageId);
  return <Dialog
    className="focus-dialog wide"
    closeLabel="Close focus view"
    labelledBy={headingId}
    onClose={onClose}
    opener={opener}
    surface={surfaceKey}
    headerEnd={<nav className="metric-switch" aria-label="Other focusable metrics">
      <button type="button" onClick={() => hop(-1)} aria-label="Previous metric"><span aria-hidden="true">‹</span></button>
      <span className="current">{index + 1} of {neighbors.length}</span>
      <button type="button" onClick={() => hop(1)} aria-label="Next metric"><span aria-hidden="true">›</span></button>
    </nav>}
    header={<>
          <p className="eyebrow">Metric focus · {windowText}{focus === null ? '' : ` · ${basisCopy(focus)}`}</p>
          <h2 id={headingId}>{label}</h2>
          {focus === null ? null : <p className="defline">{focus.metric.definition}</p>}
          {focus === null
            ? loading
              ? <div className="row"><span className="skel h-value" /><span className="sr-only">Loading the focused metric</span></div>
              : <p className="quiet">This metric focus is not available right now. Review the delivered values or choose another reporting window.</p>
            : <div className="row">
              <span className="big">{focusValueText(focus, focus.current)}</span>
              <ChangeChip change={focus.change} />
              <span className="prior">Aligned prior: {focusValueText(focus, focus.prior)}</span>
            </div>}
    </>}
  >
      {surface.preambleFor?.(metricId) ?? null}
      <div className="focus-grid">
        <div>
          {loading ? <><span className="skel h-chart" /><span className="skel h-line" /></> : null}
          {unavailable ? <p className="state-note"><span className="badge">Unavailable</span>The focus evidence could not be served. The values on the page behind remain exact.</p> : null}
          {focus === null ? null : <><FocusEvidence focus={focus} /><FocusFacts focus={focus} /></>}
        </div>
        <div className="related">
          {focus === null ? null : <FocusDrivers focus={focus} />}
          <FocusInterpretation focus={focus} loading={loading} />
        </div>
      </div>
      {/* The deep band spans full width beneath the evidence columns (design revision
          2026-09-25.1): its variable length extends the dialog evenly. */}
      {deepSurface === null ? null : <InsightDeepSection surface={deepSurface} />}
      {/* The strip claims a governed reading only when the interpretation is filled,
          keyed by its stamp; a designed absence carries its own served words above. */}
      {focus?.insight.state === 'filled'
        ? <p className="prov-strip">{provenanceLine(focus.insight.prompt_version)}</p>
        : null}
      <div className="ask-row">
        {/* At most two controls (metric-focus contract §2 item 6): the interpretation's
            served step replaces the deterministic next-check chip when it has one. */}
        {rendersStep
          ? <InsightNextStepControl handlers={stepHandlers} pageId={surface.pageId} step={servedStep} />
          : focus?.next_check == null ? null : <button
          type="button"
          className="ask-chip"
          onClick={() => onMetricChange(focus.next_check?.metric_id ?? metricId)}
        >
          Open the focus view for {focus.next_check.label.toLowerCase()}
        </button>}
        {focus === null ? null : <button type="button" className="ask-chip" onClick={() => seedAssistantQuestion(surface.askSeed(focus))}>
          Ask the assistant about {focus.metric.label.toLowerCase()}
        </button>}
      </div>
      {focus?.boundary == null ? null : <p className="footnote focus-boundary">{focus.boundary}</p>}
  </Dialog>;
}
