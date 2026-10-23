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
        const x = values.length === 1 ? (CHART.left + CHART.right) / 2 : CHART.left + (index * (CHART.right - CHART.left)) / (values.length - 1);
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

export function Spark({ ariaLabel, values }: { readonly ariaLabel: string; readonly values: readonly (number | string)[] }) {
  const layout = chartLayout([{ kind: 'current', values }]);
  if (layout === null) return <span className="footnote">No trend</span>;
  return <svg className="spark" viewBox={`0 0 1000 ${CHART.height}`} preserveAspectRatio="none" role="img" aria-label={ariaLabel}>
    <polyline className="series" points={layout.points(values).join(' ')} />
  </svg>;
}
