// SPDX-License-Identifier: MIT-0
'use client';

/**
 * Shared exact-value rendering primitives: change chips, series charts, sparklines.
 *
 * Every plotted or printed number is a returned server value; nothing here derives a
 * figure. Extracted from the campaign portfolio view when the shared metric-focus dialog
 * arrived, so the three focusable surfaces render one visual language without an import
 * cycle.
 */

import type { Change } from '@/lib/change';
import { displayFigure, formatCanonicalDecimal, formatCount, formatCurrency, formatIsoDate } from '@/lib/exact-format';

import { Icon } from './Icon';

export type ValueKind = 'count' | 'currency' | 'ratio';

export function numberText(value: number | string, kind: ValueKind, currencyCode: string | null = null): string {
  const source = typeof value === 'number' ? String(value) : value;
  if (kind === 'count') return source.includes('.') ? formatCanonicalDecimal(source) : formatCount(source);
  if (kind === 'ratio') return `${formatCanonicalDecimal(source)}×`;
  return currencyCode === null ? formatCanonicalDecimal(source) : formatCurrency(source, currencyCode);
}

/** A served metric value's display: the value carried beside it, before any formatting. */
export interface DisplayedValue {
  readonly display_value: string | null;
  readonly units: { readonly currency_code: string | null };
  readonly value: number | string;
}

/**
 * Display a served metric value: money shows the server's two-place `display_value` with its
 * code (ADR-084); counts and ratios show the exact value. Exact-values tables use `numberText`.
 */
export function displayValueText(value: DisplayedValue, kind: ValueKind, metric?: string): string {
  if (kind === 'currency' && value.display_value !== null && value.units.currency_code !== null) {
    return formatCurrency(value.display_value, value.units.currency_code);
  }
  // A governed ratio reads by its dictionary scale: a rate as 2-place %, a multiplier as 2-place ×
  // (plan D4). The exact value stays in the "Show exact values" disclosure, via `numberText`.
  if (kind === 'ratio' && metric !== undefined) {
    return displayFigure(metric, String(value.value), 'ratio', null).text;
  }
  return numberText(value.value, kind, value.units.currency_code);
}

const DIRECTION_WORDS = { down: 'Down', flat: 'No change', up: 'Up' } as const;

/**
 * The change chip (ADR-121, ia §8.6): the server's direction icon, its tone from `sentiment`,
 * and its `display_text`. Nothing here reads a sign or derives a figure. A word state ("Up
 * from a loss", "New") carries its own direction, so only its tone is announced beside it.
 * No change block (one side absent, or a body baked before the block) reads as unavailable.
 */
export function ChangeChip({ change }: { readonly change: Change | null }) {
  if (change === null) return <span className="chg-absent">Change unavailable</span>;
  // A negative or zero prior reads as a word; a near-zero prior reads as its absolute change.
  const word = change.not_meaningful === 'negative_prior' || change.not_meaningful === 'no_prior';
  const tone = change.sentiment === 'neutral' ? '' : change.sentiment;
  const spoken = change.direction === 'flat'
    ? null
    : word
      ? tone === '' ? null : `${tone}: `
      : `${DIRECTION_WORDS[change.direction]}${tone === '' ? '' : `, ${tone}`}: `;
  return <span className={`chg${word ? ' word' : ''} ${change.sentiment}`}>
    <Icon name={change.direction} />
    {spoken === null ? null : <span className="sr-only">{spoken}</span>}
    {change.display_text}
  </span>;
}

/* ---------- charts: every plotted number is a returned server value ---------- */

/** A null value is a typed-absent day (ADR-087): the line breaks there instead of reading 0. */
type ChartValue = number | string | null;

interface ChartSeries {
  readonly kind: 'current' | 'prior';
  readonly values: readonly ChartValue[];
}

/** The plot's own coordinates; axis labels sit outside it as HTML so text stays 12 px at any width. */
const CHART = { height: 200, left: 6, right: 994, bottom: 194, top: 6 } as const;

/** A plotted index's x in the plot's own coordinates, as `chartLayout` places points. */
function chartX(index: number, count: number): number {
  return count <= 1 ? (CHART.left + CHART.right) / 2 : CHART.left + (index * (CHART.right - CHART.left)) / (count - 1);
}

/** A zero-based chart (ADR-118 decision 7: counts and currency) always spans 0. */
function chartLayout(series: readonly ChartSeries[], zeroBased = false): {
  readonly max: number;
  readonly mid: number;
  readonly min: number;
  readonly points: (values: readonly ChartValue[]) => string[];
} | null {
  const parsed = series.map((entry) => entry.values.filter((value) => value !== null).map((value) => Number(value)));
  if (parsed.length === 0 || parsed.some((values) => values.length === 0 || values.some((value) => !Number.isFinite(value)))) return null;
  const domain = parsed.flat();
  const min = zeroBased ? Math.min(0, ...domain) : Math.min(...domain);
  const max = zeroBased ? Math.max(0, ...domain) : Math.max(...domain);
  return {
    max,
    mid: (max + min) / 2,
    min,
    // One run of points per stretch of defined days; an absent day ends a run.
    points: (values) => {
      const runs: string[][] = [[]];
      values.forEach((value, index) => {
        if (value === null) { runs.push([]); return; }
        const x = chartX(index, values.length);
        const y = max === min ? (CHART.top + CHART.bottom) / 2 : CHART.top + ((max - Number(value)) * (CHART.bottom - CHART.top)) / (max - min);
        runs[runs.length - 1]?.push(`${x},${y}`);
      });
      return runs.filter((run) => run.length > 0).map((run) => run.join(' '));
    },
  };
}

function axisNumber(value: number, kind: ValueKind, currencyCode: string | null): string {
  const canonical = Number.isInteger(value) ? String(value) : value.toFixed(2);
  return numberText(kind === 'count' && !Number.isInteger(value) ? value.toFixed(2) : canonical, kind === 'count' && !Number.isInteger(value) ? 'currency' : kind, kind === 'currency' ? currencyCode : null);
}

/** Each run of defined days; a run of one day renders as a visible dot. */
function SeriesShape({ className, runs }: { readonly className: string; readonly runs: readonly string[] }) {
  return <>{runs.map((run) => <SeriesRun className={className} key={run} layoutPoints={run} />)}</>;
}

/** A polyline needs two points to draw; a single returned value renders as a visible dot instead. */
function SeriesRun({ className, layoutPoints }: { readonly className: string; readonly layoutPoints: string }) {
  const points = layoutPoints.split(' ');
  if (points.length > 1) return <polyline className={className} points={layoutPoints} />;
  const [x, y] = (points[0] ?? '').split(',');
  if (x === undefined || y === undefined) return null;
  // A zero-length round-capped stroke stays a round dot in a plot that stretches to its width.
  return <line className={`${className} point`} x1={x} x2={x} y1={y} y2={y} />;
}

export function SeriesChart({ ariaLabel, currencyCode, current, dates, kind, prior, zeroBased = false }: {
  readonly ariaLabel: string;
  readonly currencyCode: string | null;
  readonly current: readonly ChartValue[];
  readonly dates: readonly string[];
  readonly kind: ValueKind;
  readonly prior?: readonly ChartValue[];
  readonly zeroBased?: boolean;
}) {
  const series: ChartSeries[] = prior === undefined
    ? [{ kind: 'current', values: current }]
    : [{ kind: 'current', values: current }, { kind: 'prior', values: prior }];
  const layout = chartLayout(series, zeroBased);
  const firstDate = dates[0];
  const lastDate = dates[dates.length - 1];
  if (layout === null || firstDate === undefined || lastDate === undefined) {
    return <p className="state-note"><span className="badge">No trend</span>This reporting window does not include chartable values for this metric. Review the exact values below or choose another available window.</p>;
  }
  // The labels repeat what the image's accessible name and the exact values table carry, so
  // they're hidden from assistive technology, as they were when they were SVG text in the image.
  return <div className="series-chart">
    <div className="y-axis" aria-hidden="true">
      <span className="axis-label">{axisNumber(layout.max, kind, currencyCode)}</span>
      <span className="axis-label">{axisNumber(layout.mid, kind, currencyCode)}</span>
      <span className="axis-label">{axisNumber(layout.min, kind, currencyCode)}</span>
    </div>
    <svg className="chart" viewBox={`0 0 1000 ${CHART.height}`} preserveAspectRatio="none" role="img" aria-label={ariaLabel}>
      {[CHART.top, (CHART.top + CHART.bottom) / 2, CHART.bottom].map((y) => <line key={y} className="grid" x1={CHART.left} x2={CHART.right} y1={y} y2={y} />)}
      {prior === undefined ? null : <SeriesShape className="series prior" runs={layout.points(prior)} />}
      <SeriesShape className="series" runs={layout.points(current)} />
    </svg>
    <div className="x-axis" aria-hidden="true">
      <span className="axis-label">{formatIsoDate(firstDate)}</span>
      <span className="axis-label">{formatIsoDate(lastDate)}</span>
    </div>
  </div>;
}

export function ChartLegend({ prior }: { readonly prior: boolean }) {
  return <p className="legend">
    <span><i aria-hidden="true" />Current window</span>
    {prior ? <span><i className="prior" aria-hidden="true" />Aligned prior window</span> : null}
  </p>;
}

/** One recent week of one measure: its served state and exact value. */
export interface BandRecentWeek {
  readonly state: 'observed' | 'inferred_zero' | 'unknown';
  readonly value: string | null;
  readonly week: string;
}
/** The forecast weeks of the chosen view and their served low, likely and high values. */
export interface BandForecast {
  readonly high: readonly string[];
  readonly likely: readonly string[];
  readonly low: readonly string[];
  readonly weeks: readonly string[];
}

/**
 * The demand forecast product chart (design S2, D5): the served recent weeks by their state,
 * then the chosen view's likely line and low-to-high band. It starts at zero (ADR-118 decision 7)
 * and draws no numeric axis at all (G-01): the forecast table below is its exact scale, so no
 * tick value is ever calculated in the browser. A `not known` week breaks the reported line; a
 * week counted as zero plots at 0 as a hollow dot.
 */
export function ForecastBandChart({ ariaLabel, dividerLabel, forecast, recent }: {
  readonly ariaLabel: string;
  readonly dividerLabel: string;
  readonly forecast: BandForecast;
  readonly recent: readonly BandRecentWeek[];
}) {
  const count = recent.length + forecast.weeks.length;
  const before: null[] = recent.map(() => null);
  const after: null[] = forecast.weeks.map(() => null);
  const reported: ChartValue[] = [...recent.map((week) => (week.state === 'unknown' ? null : week.value)), ...after];
  const likely: ChartValue[] = [...before, ...forecast.likely];
  const low: ChartValue[] = [...before, ...forecast.low];
  const high: ChartValue[] = [...before, ...forecast.high];
  const series: ChartSeries[] = [reported, likely, low, high]
    .filter((values) => values.some((value) => value !== null))
    .map((values) => ({ kind: 'current', values }));
  const layout = chartLayout(series, true);
  const first = recent[0]?.week ?? forecast.weeks[0];
  const last = forecast.weeks[forecast.weeks.length - 1];
  if (layout === null || first === undefined || last === undefined) {
    return <p className="state-note"><span className="badge">No trend</span>There are no weekly values to draw for this measure. The exact values are in the table below.</p>;
  }
  const [highRun] = layout.points(high);
  const [lowRun] = layout.points(low);
  const band = highRun === undefined || lowRun === undefined ? null : `${highRun} ${lowRun.split(' ').reverse().join(' ')}`;
  const dividerX = chartX(recent.length, count);
  const dividerShare = dividerX / 1000;
  // Where 0 sits, as chartLayout places a value (a flat series sits mid-plot).
  const zeroY = layout.max === layout.min
    ? (CHART.top + CHART.bottom) / 2
    : CHART.top + (layout.max * (CHART.bottom - CHART.top)) / (layout.max - layout.min);
  return <div className="series-chart df-chart">
    <div className="df-plot">
      <svg className="chart" viewBox={`0 0 1000 ${CHART.height}`} preserveAspectRatio="none" role="img" aria-label={ariaLabel}>
        <line className="grid" x1={CHART.left} x2={CHART.right} y1={CHART.bottom} y2={CHART.bottom} />
        {band === null ? null : <polygon className="df-band" points={band} />}
        {recent.length === 0 ? null : <line className="df-divider" x1={dividerX} x2={dividerX} y1={CHART.top} y2={CHART.bottom} />}
        <SeriesShape className="series df-likely" runs={layout.points(likely)} />
        <SeriesShape className="series df-reported" runs={layout.points(reported)} />
        {recent.map((week, index) => (week.state === 'inferred_zero'
          ? <circle key={week.week} className="df-zero" cx={chartX(index, count)} cy={zeroY} r={5} />
          : null))}
      </svg>
      {recent.length === 0 ? null : (
        <span className={dividerShare > 0.5 ? 'df-divider-label end' : 'df-divider-label'} style={{ left: `${dividerShare * 100}%` }} aria-hidden="true">
          {dividerLabel}
        </span>
      )}
    </div>
    <div className="x-axis" aria-hidden="true">
      <span className="axis-label">{formatIsoDate(first)}</span>
      <span className="axis-label">{formatIsoDate(last)}</span>
    </div>
    <p className="df-chart-note">Starts at zero. Exact weekly numbers are in the table below.</p>
  </div>;
}

/** The chart's legend: only the marks the plotted series contain (G-14). */
export function ForecastBandLegend({ reported, zero }: { readonly reported: boolean; readonly zero: boolean }) {
  return <p className="legend df-legend">
    {reported ? <span><i className="df-reported" aria-hidden="true" />Reported</span> : null}
    <span><i className="df-likely" aria-hidden="true" />Likely</span>
    <span><i className="df-range" aria-hidden="true" />Low to high</span>
    {zero ? <span><i className="df-zero" aria-hidden="true" />No sales reported, counted as zero</span> : null}
  </p>;
}

export function Spark({ ariaLabel, values }: { readonly ariaLabel: string; readonly values: readonly (number | string)[] }) {
  const layout = chartLayout([{ kind: 'current', values }]);
  if (layout === null) return <span className="footnote">No trend</span>;
  return <svg className="spark" viewBox={`0 0 1000 ${CHART.height}`} preserveAspectRatio="none" role="img" aria-label={ariaLabel}>
    <polyline className="series" points={layout.points(values).join(' ')} />
  </svg>;
}
