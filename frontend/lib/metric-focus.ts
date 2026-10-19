// SPDX-License-Identifier: MIT-0
/** Strict browser decoder for the shared MetricFocus.v4 result (metric-focus contract §3). */

import { type Change, parseChange } from './change';
import { isDisplayValue } from './display-value';
import { hasNextStep, type InsightNextStep, slotNextStep } from './insight-next-step';
import type { DateWindow } from './presentation';

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;
const NUMBER = /^(?:0|[1-9]\d*)(?:\.\d+)?$/;
const SIGNED_NUMBER = /^[+-]?(?:0|[1-9]\d*)(?:\.\d+)?$/;
const DRIVER_GRAINS = ['campaign', 'day', 'ratio_component', 'product'] as const;
const FOCUS_SLOTS = ['metric-focus', 'retail-metric-focus'] as const;
/** The one metric whose values may be legitimately negative: Amazon's margin proxy. */
const SIGNED_METRICS = new Set(['amazon_margin_proxy']);

type Numeric = number | string;

type FocusUnits = { readonly kind: 'count' | 'currency' | 'ratio'; readonly currency_code: string | null };

export interface MetricFocusValue {
  readonly basis: 'ad_attributed' | 'retail_actual';
  /** The server's two-place display for currency, null otherwise (ADR-084). */
  readonly display_value: string | null;
  readonly grain: readonly string[];
  readonly period: DateWindow;
  readonly units: FocusUnits;
  readonly value: Numeric;
}

const ABSENCE_CLASSES = ['evaluated-quiet', 'ineligible', 'generation-pending', 'out-of-slice'] as const;
/** One closed shape family: the absence pair is present together (a declared absence) or absent together. */
const INSIGHT_BASE_FIELDS = ['artifact_revision', 'evidence_line', 'id', 'output_class', 'prompt_version', 'state', 'text'] as const;
const INSIGHT_ABSENCE_FIELDS = [...INSIGHT_BASE_FIELDS, 'absence_class', 'absence_reason'] as const;
const INSIGHT_STEP_FIELDS = [...INSIGHT_BASE_FIELDS, 'next_step'] as const;

export interface MetricFocusInsight {
  readonly absence_class: typeof ABSENCE_CLASSES[number] | null;
  readonly absence_reason: string | null;
  readonly artifact_revision: string;
  readonly evidence_line: string | null;
  readonly id: typeof FOCUS_SLOTS[number];
  readonly output_class: 'observed' | 'perspective';
  readonly prompt_version: string;
  readonly state: 'filled' | 'silent' | 'unavailable';
  readonly text: string | null;
  /** FR-11: the interpretation's served next step, when its prompt offered one. */
  readonly next_step: InsightNextStep | null;
}

export interface MetricFocusDriverEntry {
  readonly change: Numeric;
  /** The served text that replaces a share that is not meaningful (design §4.6). */
  readonly chip: string | null;
  /** The entry's server change (ADR-121): direction, tone and text for its chip. */
  readonly delta: Change;
  /** The entry's own two-place change for a currency entry, null otherwise (ADR-084). */
  readonly display_change: string | null;
  readonly moves_against_basis: boolean;
  readonly name: string;
  readonly share_of_move: string | null;
  /** The entry's own units, which may differ from the focused metric's (a ratio component). */
  readonly units: FocusUnits;
}

/** Which N of M entities the drivers rank among; present exactly on additive grains. */
export interface MetricFocusDriverScope {
  readonly count: number;
  readonly label: string;
  readonly population: number;
  readonly share_suffix: string | null;
}

const SHARE_BASES = ['portfolio', 'scope', 'none'] as const;
const ADDITIVE_GRAINS = new Set<string>(['campaign', 'product']);

export type MetricFocusDrivers =
  | {
      readonly entries: readonly MetricFocusDriverEntry[];
      readonly grain: typeof DRIVER_GRAINS[number];
      readonly scope: MetricFocusDriverScope | null;
      readonly share_basis: typeof SHARE_BASES[number] | null;
    }
  | { readonly absent_reason: string };

type SummaryChange = { readonly display: string; readonly value: string } | null;

/** What the listed drivers, the rest of the scope, and everything outside it moved. */
export interface MetricFocusDriversSummary {
  readonly fell: number;
  readonly flat: number;
  /** The served customer sentences, one per line, in display order. */
  readonly lines: readonly string[];
  readonly listed_change: SummaryChange;
  readonly other_in_scope_change: SummaryChange;
  readonly outside_scope_change: SummaryChange;
  readonly rose: number;
}

export interface MetricFocusV4 {
  readonly boundary: string | null;
  /** The focused metric's server change (ADR-121). */
  readonly change: Change;
  readonly current: MetricFocusValue;
  readonly drivers: MetricFocusDrivers;
  readonly drivers_summary: MetricFocusDriversSummary | null;
  readonly facts: readonly { readonly id: string; readonly label: string; readonly qualifier: string | null; readonly value: string }[];
  readonly focus_version: 4;
  readonly insight: MetricFocusInsight;
  readonly metric: {
    readonly basis: 'ad_attributed' | 'retail_actual';
    readonly definition: string;
    readonly grain: readonly string[];
    readonly id: string;
    readonly label: string;
    readonly unit: { readonly kind: 'count' | 'currency' | 'ratio'; readonly currency_code: string | null };
  };
  readonly neighbors: readonly string[];
  readonly next_check: { readonly label: string; readonly metric_id: string } | null;
  readonly period: DateWindow;
  readonly prior: MetricFocusValue;
  readonly prior_period: DateWindow;
  readonly series: {
    readonly display_grain: string;
    readonly points: readonly { readonly current: Numeric; readonly date: string; readonly prior: Numeric | null }[];
  } | null;
}

function fail(): never { throw new TypeError('The metric focus result is malformed.'); }
function record(value: unknown, fields: readonly string[]): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return fail();
  const item = value as Record<string, unknown>;
  const actual = Object.keys(item);
  if (actual.length !== fields.length || fields.some((field) => !(field in item))) return fail();
  return item;
}
function text(value: unknown): string { if (typeof value !== 'string' || !value.trim()) return fail(); return value; }
function optionalText(value: unknown): string | null { return value === null ? null : text(value); }
function member<T extends string>(value: unknown, allowed: readonly T[]): T {
  if (typeof value !== 'string' || !allowed.includes(value as T)) return fail();
  return value as T;
}
function number(value: unknown, signed = false): Numeric {
  if (typeof value === 'number') {
    if (!Number.isFinite(value) || (!signed && value < 0)) return fail();
    return value;
  }
  if (typeof value !== 'string' || !(signed ? SIGNED_NUMBER : NUMBER).test(value)) return fail();
  return value;
}
function isoDate(value: unknown): string {
  const result = text(value);
  if (!ISO_DATE.test(result) || Number.isNaN(Date.parse(`${result}T00:00:00Z`))) return fail();
  return result;
}
function window(value: unknown): DateWindow {
  const item = record(value, ['start', 'end']);
  const result = { end: isoDate(item.end), start: isoDate(item.start) };
  if (result.start > result.end) return fail();
  return result;
}
function sameWindow(left: DateWindow, right: DateWindow): boolean {
  return left.start === right.start && left.end === right.end;
}
function previous(period: DateWindow, prior: DateWindow): boolean {
  const day = 86_400_000;
  const currentStart = Date.parse(`${period.start}T00:00:00Z`);
  const currentEnd = Date.parse(`${period.end}T00:00:00Z`);
  const priorStart = Date.parse(`${prior.start}T00:00:00Z`);
  const priorEnd = Date.parse(`${prior.end}T00:00:00Z`);
  return priorEnd + day === currentStart && currentEnd - currentStart === priorEnd - priorStart;
}
function units(value: unknown): FocusUnits {
  const item = record(value, ['kind', 'currency_code']);
  const kind = member(item.kind, ['count', 'currency', 'ratio'] as const);
  const code = item.currency_code;
  if (code !== null && (typeof code !== 'string' || !/^[A-Z]{3}$/.test(code))) return fail();
  return { currency_code: code as string | null, kind };
}
function focusValue(value: unknown, expectedPeriod: DateWindow, signed: boolean): MetricFocusValue {
  const item = record(value, ['basis', 'display_value', 'grain', 'period', 'units', 'value']);
  const basis = member(item.basis, ['ad_attributed', 'retail_actual'] as const);
  const grain = Array.isArray(item.grain) ? item.grain.map(text) : fail();
  const period = window(item.period);
  if (!sameWindow(period, expectedPeriod)) return fail();
  const valueUnits = units(item.units);
  if (!isDisplayValue(item.display_value, valueUnits.kind)) return fail();
  return {
    basis,
    display_value: item.display_value,
    grain,
    period,
    units: valueUnits,
    value: number(item.value, signed),
  };
}
function insight(value: unknown): MetricFocusInsight {
  // One closed shape family: the absence pair is present together (a declared
  // absence, class never null) or absent together (no declared absence).
  const withAbsence = typeof value === 'object' && value !== null && 'absence_class' in value;
  const item = record(value, withAbsence ? INSIGHT_ABSENCE_FIELDS : hasNextStep(value) ? INSIGHT_STEP_FIELDS : INSIGHT_BASE_FIELDS);
  const id = member(item.id, FOCUS_SLOTS);
  const outputClass = member(item.output_class, ['observed', 'perspective'] as const);
  const state = member(item.state, ['filled', 'silent', 'unavailable'] as const);
  const filled = state === 'filled';
  if (filled !== (typeof item.text === 'string' && typeof item.evidence_line === 'string')) return fail();
  if (!filled && (item.text !== null || item.evidence_line !== null)) return fail();
  const absenceClass = withAbsence ? member(item.absence_class, ABSENCE_CLASSES) : null;
  const absenceReason = withAbsence && item.absence_reason !== null ? text(item.absence_reason) : null;
  if (filled && absenceClass !== null) return fail();
  return {
    absence_class: absenceClass,
    absence_reason: absenceReason,
    artifact_revision: text(item.artifact_revision),
    evidence_line: item.evidence_line as string | null,
    id,
    next_step: slotNextStep(item, filled),
    output_class: outputClass,
    prompt_version: text(item.prompt_version),
    state,
    text: item.text as string | null,
  };
}
function drivers(value: unknown): MetricFocusDrivers {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return fail();
  const keys = Object.keys(value as Record<string, unknown>);
  if (keys.length === 1 && keys[0] === 'absent_reason') {
    return { absent_reason: text((value as Record<string, unknown>).absent_reason) };
  }
  const item = record(value, ['grain', 'scope', 'share_basis', 'entries']);
  const grain = member(item.grain, DRIVER_GRAINS);
  const entries = Array.isArray(item.entries) ? item.entries : fail();
  if (entries.length === 0) return fail();
  // Null by grain (MetricFocus.v3, revised 2026-10-05): a scope and share basis exist exactly
  // for the additive grains, and are refused on the others.
  const additive = ADDITIVE_GRAINS.has(grain);
  if (additive === (item.scope === null) || additive === (item.share_basis === null)) return fail();
  const scope = item.scope === null ? null : driverScope(item.scope);
  return {
    entries: entries.map((raw) => {
      const entry = record(raw, ['name', 'change', 'units', 'display_change', 'share_of_move', 'delta', 'moves_against_basis', 'chip']);
      const share = entry.share_of_move;
      if (share !== null && (typeof share !== 'string' || !/^\d+(?:\.\d+)?%$/.test(share))) return fail();
      if (typeof entry.moves_against_basis !== 'boolean') return fail();
      const chip = optionalText(entry.chip);
      // A chip replaces the share; the two never arrive together.
      if (chip !== null && share !== null) return fail();
      const entryUnits = units(entry.units);
      if (!isDisplayValue(entry.display_change, entryUnits.kind)) return fail();
      return {
        change: number(entry.change, true),
        chip,
        delta: parseChange(entry.delta) ?? fail(),
        display_change: entry.display_change,
        moves_against_basis: entry.moves_against_basis,
        name: text(entry.name),
        share_of_move: share as string | null,
        units: entryUnits,
      };
    }),
    grain,
    scope,
    share_basis: item.share_basis === null ? null : member(item.share_basis, SHARE_BASES),
  };
}

function count(value: unknown): number {
  if (typeof value !== 'number' || !Number.isInteger(value) || value < 0) return fail();
  return value;
}

function driverScope(value: unknown): MetricFocusDriverScope {
  const item = record(value, ['count', 'population', 'label', 'share_suffix']);
  const scoped = count(item.count);
  const population = count(item.population);
  if (population < scoped) return fail();
  return { count: scoped, label: text(item.label), population, share_suffix: optionalText(item.share_suffix) };
}

function summaryChange(value: unknown): SummaryChange {
  if (value === null) return null;
  const item = record(value, ['value', 'display']);
  return { display: text(item.display), value: String(number(item.value, true)) };
}

function driversSummary(value: unknown, decoded: MetricFocusDrivers): MetricFocusDriversSummary | null {
  const additive = !('absent_reason' in decoded) && ADDITIVE_GRAINS.has(decoded.grain);
  if (!additive) return value === null ? null : fail();
  const item = record(value, ['listed_change', 'other_in_scope_change', 'outside_scope_change', 'rose', 'fell', 'flat', 'lines']);
  const lines = Array.isArray(item.lines) ? item.lines.map(text) : fail();
  const listed = summaryChange(item.listed_change);
  if (lines.length === 0 || listed === null) return fail();
  return {
    fell: count(item.fell),
    flat: count(item.flat),
    lines,
    listed_change: listed,
    other_in_scope_change: summaryChange(item.other_in_scope_change),
    outside_scope_change: summaryChange(item.outside_scope_change),
    rose: count(item.rose),
  };
}

export function parseMetricFocusV4(raw: unknown): MetricFocusV4 {
  const item = record(raw, [
    'boundary', 'change', 'current', 'drivers', 'drivers_summary', 'facts', 'focus_version', 'insight', 'metric',
    'neighbors', 'next_check', 'period', 'prior', 'prior_period', 'series',
  ]);
  if (item.focus_version !== 4) return fail();
  const metricItem = record(item.metric, ['id', 'label', 'definition', 'unit', 'basis', 'grain']);
  const metric = {
    basis: member(metricItem.basis, ['ad_attributed', 'retail_actual'] as const),
    definition: text(metricItem.definition),
    grain: Array.isArray(metricItem.grain) ? metricItem.grain.map(text) : fail(),
    id: text(metricItem.id),
    label: text(metricItem.label),
    unit: units(metricItem.unit),
  };
  const signed = SIGNED_METRICS.has(metric.id);
  const period = window(item.period);
  const prior_period = window(item.prior_period);
  if (!previous(period, prior_period)) return fail();
  const current = focusValue(item.current, period, signed);
  const prior = focusValue(item.prior, prior_period, signed);
  if (current.basis !== metric.basis || prior.basis !== metric.basis) return fail();
  const change = parseChange(item.change) ?? fail();
  const series = item.series === null ? null : (() => {
    const seriesItem = record(item.series, ['points', 'display_grain']);
    const points = Array.isArray(seriesItem.points) ? seriesItem.points : fail();
    if (points.length === 0) return fail();
    let lastDate = '';
    return {
      display_grain: text(seriesItem.display_grain),
      points: points.map((raw) => {
        const point = record(raw, ['date', 'current', 'prior']);
        const pointDate = isoDate(point.date);
        if (pointDate <= lastDate || pointDate < period.start || pointDate > period.end) return fail();
        lastDate = pointDate;
        return {
          current: number(point.current, signed),
          date: pointDate,
          prior: point.prior === null ? null : number(point.prior, signed),
        };
      }),
    };
  })();
  const facts = Array.isArray(item.facts) ? item.facts.map((raw) => {
    const fact = record(raw, ['id', 'label', 'value', 'qualifier']);
    return { id: text(fact.id), label: text(fact.label), qualifier: optionalText(fact.qualifier), value: text(fact.value) };
  }) : fail();
  if (facts.length === 0) return fail();
  const neighbors = Array.isArray(item.neighbors) ? item.neighbors.map(text) : fail();
  if (!neighbors.includes(metric.id) || new Set(neighbors).size !== neighbors.length) return fail();
  const nextCheck = item.next_check === null ? null : (() => {
    const chip = record(item.next_check, ['metric_id', 'label']);
    const metricId = text(chip.metric_id);
    if (!neighbors.includes(metricId)) return fail();
    return { label: text(chip.label), metric_id: metricId };
  })();
  const decodedDrivers = drivers(item.drivers);
  return {
    boundary: optionalText(item.boundary),
    change,
    current,
    drivers: decodedDrivers,
    drivers_summary: driversSummary(item.drivers_summary, decodedDrivers),
    facts,
    focus_version: 4,
    insight: insight(item.insight),
    metric,
    neighbors,
    next_check: nextCheck,
    period,
    prior,
    prior_period,
    series,
  };
}
