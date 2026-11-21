// SPDX-License-Identifier: MIT-0
import type { Answer } from './answer';
import {
  formatCount, formatCurrency, formatIsoDate, formatIsoDateRangeShort, formatIsoInstantDate, formatIsoInstantShort,
} from './exact-format';
import { callApi } from './session';

/**
 * The demand forecast read API's browser side (ADR-163): three fetchers, strict decoders and the
 * plain-words copy for every closed code. The server owns every figure and its display string
 * (ADR-084); this module never rounds, sums or derives one. An unknown enum value, a money figure
 * without two places or a unit figure with them fails decoding, so it can never render as text.
 */

export const targets = ['units_shipped', 'units_ordered', 'revenue_shipped', 'cogs_shipped'] as const;
export type Target = typeof targets[number];
export const moneyTargets: readonly Target[] = ['revenue_shipped', 'cogs_shipped'];
export const skipReasons = [
  'ASIN_INACTIVE', 'TARGET_VALUE_MISSING', 'INSUFFICIENT_CONTEXT', 'HOLDOUT_UNKNOWN', 'HOLDOUT_ALL_ZERO',
  'BACKTEST_BAND_UNORDERED', 'SEASONAL_LAG_UNKNOWN', 'SEASONAL_LAG_UNKNOWN_AT_ORIGIN',
  'SEASONAL_RESIDUALS_TOO_FEW', 'HISTORY_TOO_SHORT_FOR_SEASON', 'ETS_INSUFFICIENT_KNOWN', 'ETS_FIT_FAILED',
  'ACCURACY_NOT_TESTABLE', 'FORECAST_NOT_PUBLISHABLE', 'FORECAST_SHAPE_INVALID', 'FORECAST_NOT_FINITE',
  'FORECAST_BAND_UNORDERED', 'FORECAST_NEGATIVE', 'PRODUCT_KEY_INVALID', 'DISPLAY_ASIN_COLLISION',
  'CURRENCY_MIXED',
] as const;
export type SkipReason = typeof skipReasons[number];
export const modelNames = ['chronos2', 'seasonal_naive', 'ets'] as const;
export type ModelName = typeof modelNames[number];
const runOutcomes = ['published', 'held_quality', 'failed', 'rolled_back'] as const;
export type RunOutcome = typeof runOutcomes[number];
const failureFamilies = ['quality_check', 'input_not_ready', 'forecast_job', 'input_build', 'retry_exhausted', 'other'] as const;
export type FailureFamily = typeof failureFamilies[number];
const freshnessCauses = ['age_over_limit', 'last_run_failed', 'last_run_held'] as const;
export type FreshnessCause = typeof freshnessCauses[number];
const accuracyClasses = ['beat_naive', 'not_beat_naive', 'history_unchanging', 'scale_unknown'] as const;
export type AccuracyClass = typeof accuracyClasses[number];
const holds = ['none', 'quality'] as const;
export type Hold = typeof holds[number];
const refusals = [
  'ASIN_INVALID', 'STATUS_INVALID', 'PAGE_INVALID', 'QUERY_INVALID', 'FORECAST_ARGUMENT_INVALID',
  'CURRENCY_MISMATCH', 'FORECAST_NOT_PUBLISHED', 'FORECAST_STORE_INCOMPLETE', 'FORECAST_STORE_UNAVAILABLE',
  'ROUTE_UNKNOWN', 'FORECAST_READ_FAILED', 'IDENTITY_REQUIRED',
] as const;
export type Refused = typeof refusals[number];
export const weekStates = ['observed', 'inferred_zero', 'unknown'] as const;
export type WeekState = typeof weekStates[number];
export const maseGaps = ['HISTORY_UNCHANGING', 'MASE_SCALE_UNKNOWN'] as const;
export type MaseGap = typeof maseGaps[number];
const views = ['4', '8', '13'] as const;
export type ViewKey = typeof views[number];

export interface Figure { readonly value: string; readonly display: string }
export interface Provenance {
  readonly generation_id: string; readonly published_at: string; readonly input_end: string; readonly first_forecast_week: string;
}
export interface Freshness {
  readonly state: 'fresh' | 'stale' | 'unavailable'; readonly age_days: number | null;
  readonly stale_after_days: number; readonly causes: readonly FreshnessCause[];
}
export interface Horizon {
  readonly grain: 'week'; readonly week_starts_on: 'sunday'; readonly weeks: readonly string[];
  readonly views: readonly number[]; readonly default_view_weeks: number;
}
export interface ListTarget {
  readonly chosen_model: ModelName | null; readonly withheld_reason: SkipReason | null;
  readonly currency_code: string | null; readonly p50_totals: Readonly<Record<ViewKey, Figure | null>>;
}
export interface ListEntry {
  readonly rank: number; readonly asin: string; readonly title: string | null; readonly title_truncated: boolean;
  readonly targets: Readonly<Record<Target, ListTarget>>; readonly stockout_weeks_last_13: number;
}
export interface DemandForecastList {
  readonly outcome: 'complete' | 'unavailable'; readonly provenance: Provenance | null; readonly freshness: Freshness;
  readonly horizon: Horizon | null; readonly query: { readonly status: string | null; readonly page: number };
  readonly page: number; readonly page_count: number; readonly match_count: number; readonly page_rows: number;
  readonly entries: readonly ListEntry[]; readonly item_count: number | null;
  readonly counts_by_reason: Readonly<Record<Target, Readonly<Partial<Record<SkipReason, number>>>>> | null;
  readonly counts_by_model: Readonly<Record<Target, Readonly<Partial<Record<ModelName, number>>>>> | null;
  readonly currency_codes: readonly string[]; readonly status_target: Target;
}
export interface Accuracy {
  readonly wql: string; readonly mase: string | null; readonly mase_gap: MaseGap | null;
  readonly weeks_in_range: number; readonly weeks_scored: number; readonly accuracy_class: AccuracyClass;
}
export interface TargetDetail extends ListTarget {
  readonly bands: { readonly p10: readonly Figure[]; readonly p50: readonly Figure[]; readonly p90: readonly Figure[] } | null;
  readonly accuracy: Accuracy | null;
}
export interface RecentWeek {
  readonly week_start: string;
  readonly targets: Readonly<Record<Target, { readonly state: WeekState; readonly value: Figure | null }>>;
  readonly stockout: boolean | null;
}
export interface DemandForecastDetail {
  readonly outcome: 'selected' | 'not_forecast' | 'not_in_forecast' | 'unavailable'; readonly asin: string;
  readonly provenance: Provenance | null; readonly freshness: Freshness; readonly horizon: Horizon | null;
  readonly title: string | null; readonly title_truncated: boolean;
  readonly targets: Readonly<Record<Target, TargetDetail>> | null; readonly recent_weeks: readonly RecentWeek[] | null;
  readonly stockout_weeks_last_13: number | null;
  readonly week_state_counts: { readonly observed: number; readonly inferred_zero: number; readonly unknown: number } | null;
}
export interface LastRun {
  readonly outcome: RunOutcome; readonly failure_category: string | null; readonly failure_family: FailureFamily | null;
  readonly retry_disposition: 'suppress' | 'retry'; readonly transient_retries: number; readonly at: string;
}
export interface DemandForecastStatus {
  readonly generation: { readonly generation_id: string; readonly published_at: string; readonly input_end: string } | null;
  readonly freshness: Freshness; readonly last_run: LastRun | null; readonly hold: Hold;
  readonly rollback_active: boolean; readonly rolled_back_from: string | null;
  readonly next_check_at: string; readonly next_new_input_check_at: string;
}
export interface DemandForecastRefusal { readonly refused: Refused; readonly code: string; readonly detail: string }

/**
 * One typed result for every fetcher (DF-S3-PLAN-011). `refused` is a coded refusal body on a
 * 400, 403, 404, 500 or 503; `invalid` is any body that failed decoding, logged without the
 * body. A session failure is `callApi`'s own throw, and an aborted request rejects with the
 * fetch's AbortError unchanged, so neither is ever reported as an outage.
 */
export type FetchResult<T> =
  | { readonly kind: 'ok'; readonly value: T }
  | { readonly kind: 'refused'; readonly status: number; readonly refusal: DemandForecastRefusal }
  | { readonly kind: 'invalid'; readonly status: number };

// --- Decoding -------------------------------------------------------------------------------

type Json = Record<string, unknown>;
const INSTANT = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/;
const DATE = /^\d{4}-\d{2}-\d{2}$/;
const DECIMAL = /^-?[0-9]+\.[0-9]{4}$/;
const MONEY = /^-?[0-9]+\.[0-9]{2}$/;
const WHOLE = /^-?[0-9]+$/;

function fail(where: string): never {
  throw new TypeError(`Demand forecast response: ${where} is not valid.`);
}
function record(value: unknown, keys: readonly string[], where: string): Json {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) fail(where);
  const found = Object.keys(value).sort();
  if (found.join('|') !== [...keys].sort().join('|')) fail(where);
  return value as Json;
}
function oneOf<T extends string>(value: unknown, allowed: readonly T[], where: string): T {
  if (typeof value !== 'string' || !(allowed as readonly string[]).includes(value)) fail(where);
  return value as T;
}
function text(value: unknown, where: string, pattern?: RegExp): string {
  if (typeof value !== 'string' || value === '' || (pattern !== undefined && !pattern.test(value))) fail(where);
  return value;
}
function count(value: unknown, where: string): number {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < 0) fail(where);
  return value;
}
function bool(value: unknown, where: string): boolean {
  if (typeof value !== 'boolean') fail(where);
  return value;
}
function nullable<T>(value: unknown, read: (inner: unknown) => T): T | null {
  return value === null ? null : read(value);
}
function list<T>(value: unknown, where: string, read: (inner: unknown, at: string) => T): T[] {
  if (!Array.isArray(value)) fail(where);
  return value.map((inner, index) => read(inner, `${where}[${index}]`));
}
function byKey<K extends string, T>(value: unknown, keys: readonly K[], where: string, read: (inner: unknown, key: K) => T): Record<K, T> {
  const raw = record(value, keys, where);
  return Object.fromEntries(keys.map((key) => [key, read(raw[key], key)])) as Record<K, T>;
}

function figure(value: unknown, target: Target, where: string): Figure {
  const raw = record(value, ['display', 'value'], where);
  const display = text(raw.display, `${where}.display`, moneyTargets.includes(target) ? MONEY : WHOLE);
  return { value: text(raw.value, `${where}.value`, DECIMAL), display };
}
function provenance(value: unknown): Provenance {
  const raw = record(value, ['first_forecast_week', 'generation_id', 'input_end', 'published_at'], 'provenance');
  return {
    generation_id: text(raw.generation_id, 'generation_id'),
    published_at: text(raw.published_at, 'published_at', INSTANT),
    input_end: text(raw.input_end, 'input_end', DATE),
    first_forecast_week: text(raw.first_forecast_week, 'first_forecast_week', DATE),
  };
}
function freshness(value: unknown): Freshness {
  const raw = record(value, ['age_days', 'causes', 'stale_after_days', 'state'], 'freshness');
  return {
    state: oneOf(raw.state, ['fresh', 'stale', 'unavailable'] as const, 'freshness.state'),
    age_days: nullable(raw.age_days, (inner) => count(inner, 'age_days')),
    stale_after_days: count(raw.stale_after_days, 'stale_after_days'),
    causes: list(raw.causes, 'causes', (inner, at) => oneOf(inner, freshnessCauses, at)),
  };
}
function horizon(value: unknown): Horizon {
  const raw = record(value, ['default_view_weeks', 'grain', 'views', 'week_starts_on', 'weeks'], 'horizon');
  const parsed = {
    grain: oneOf(raw.grain, ['week'] as const, 'grain'),
    week_starts_on: oneOf(raw.week_starts_on, ['sunday'] as const, 'week_starts_on'),
    weeks: list(raw.weeks, 'weeks', (inner, at) => text(inner, at, DATE)),
    views: list(raw.views, 'views', (inner, at) => count(inner, at)),
    default_view_weeks: count(raw.default_view_weeks, 'default_view_weeks'),
  };
  if (!parsed.views.includes(parsed.default_view_weeks)) fail('horizon.default_view_weeks');
  return parsed;
}
const LIST_TARGET_KEYS = ['chosen_model', 'currency_code', 'p50_totals', 'withheld_reason'] as const;
function listTarget(raw: Json, target: Target, where: string): ListTarget {
  return {
    chosen_model: nullable(raw.chosen_model, (inner) => oneOf(inner, modelNames, `${where}.chosen_model`)),
    withheld_reason: nullable(raw.withheld_reason, (inner) => oneOf(inner, skipReasons, `${where}.withheld_reason`)),
    currency_code: nullable(raw.currency_code, (inner) => text(inner, `${where}.currency_code`, /^[A-Z]{3}$/)),
    p50_totals: byKey(raw.p50_totals, views, `${where}.p50_totals`, (inner, key) =>
      nullable(inner, (shown) => figure(shown, target, `${where}.p50_totals.${key}`))),
  };
}
function entry(value: unknown, where: string): ListEntry {
  const raw = record(value, ['asin', 'rank', 'stockout_weeks_last_13', 'targets', 'title', 'title_truncated'], where);
  return {
    rank: count(raw.rank, `${where}.rank`),
    asin: text(raw.asin, `${where}.asin`, /^[A-Z0-9]{10}$/),
    title: nullable(raw.title, (inner) => text(inner, `${where}.title`)),
    title_truncated: bool(raw.title_truncated, `${where}.title_truncated`),
    targets: byKey(raw.targets, targets, `${where}.targets`, (inner, key) =>
      listTarget(record(inner, LIST_TARGET_KEYS, `${where}.${key}`), key, `${where}.${key}`)),
    stockout_weeks_last_13: count(raw.stockout_weeks_last_13, `${where}.stockout_weeks_last_13`),
  };
}
function countsBy<K extends string>(value: unknown, keys: readonly K[], where: string): Record<Target, Partial<Record<K, number>>> {
  const raw = record(value, targets, where);
  return Object.fromEntries(targets.map((target) => {
    const inner = raw[target];
    if (typeof inner !== 'object' || inner === null || Array.isArray(inner)) fail(where);
    const shaped: Partial<Record<K, number>> = {};
    for (const [key, shown] of Object.entries(inner)) shaped[oneOf(key, keys, where)] = count(shown, where);
    return [target, shaped];
  })) as Record<Target, Partial<Record<K, number>>>;
}

export function parseDemandForecastList(value: unknown): DemandForecastList {
  const raw = record(value, [
    'counts_by_model', 'counts_by_reason', 'currency_codes', 'entries', 'freshness', 'horizon', 'item_count',
    'match_count', 'outcome', 'page', 'page_count', 'page_rows', 'provenance', 'query', 'schema_version', 'status_target',
  ], 'list');
  if (raw.schema_version !== 1) fail('schema_version');
  const query = record(raw.query, ['page', 'status'], 'query');
  return {
    outcome: oneOf(raw.outcome, ['complete', 'unavailable'] as const, 'outcome'),
    provenance: nullable(raw.provenance, provenance),
    freshness: freshness(raw.freshness),
    horizon: nullable(raw.horizon, horizon),
    query: {
      status: nullable(query.status, (inner) => oneOf(inner, ['shown', ...skipReasons] as const, 'query.status')),
      page: count(query.page, 'query.page'),
    },
    page: count(raw.page, 'page'),
    page_count: count(raw.page_count, 'page_count'),
    match_count: count(raw.match_count, 'match_count'),
    page_rows: count(raw.page_rows, 'page_rows'),
    entries: list(raw.entries, 'entries', entry),
    item_count: nullable(raw.item_count, (inner) => count(inner, 'item_count')),
    counts_by_reason: nullable(raw.counts_by_reason, (inner) => countsBy(inner, skipReasons, 'counts_by_reason')),
    counts_by_model: nullable(raw.counts_by_model, (inner) => countsBy(inner, modelNames, 'counts_by_model')),
    currency_codes: list(raw.currency_codes, 'currency_codes', (inner, at) => text(inner, at, /^[A-Z]{3}$/)),
    status_target: oneOf(raw.status_target, targets, 'status_target'),
  };
}

function accuracy(value: unknown): Accuracy {
  const raw = record(value, ['accuracy_class', 'mase', 'mase_gap', 'weeks_in_range', 'weeks_scored', 'wql'], 'accuracy');
  return {
    wql: text(raw.wql, 'wql', DECIMAL),
    mase: nullable(raw.mase, (inner) => text(inner, 'mase', DECIMAL)),
    mase_gap: nullable(raw.mase_gap, (inner) => oneOf(inner, maseGaps, 'mase_gap')),
    weeks_in_range: count(raw.weeks_in_range, 'weeks_in_range'),
    weeks_scored: count(raw.weeks_scored, 'weeks_scored'),
    accuracy_class: oneOf(raw.accuracy_class, accuracyClasses, 'accuracy_class'),
  };
}
function targetDetail(value: unknown, target: Target): TargetDetail {
  const raw = record(value, [...LIST_TARGET_KEYS, 'accuracy', 'bands'], `targets.${target}`);
  const band = (inner: unknown, at: string) => list(inner, at, (shown, where) => figure(shown, target, where));
  return {
    ...listTarget(raw, target, `targets.${target}`),
    bands: nullable(raw.bands, (inner) => {
      const bands = record(inner, ['p10', 'p50', 'p90'], 'bands');
      return { p10: band(bands.p10, 'p10'), p50: band(bands.p50, 'p50'), p90: band(bands.p90, 'p90') };
    }),
    accuracy: nullable(raw.accuracy, accuracy),
  };
}
function recentWeek(value: unknown, where: string): RecentWeek {
  const raw = record(value, ['stockout', 'targets', 'week_start'], where);
  return {
    week_start: text(raw.week_start, `${where}.week_start`, DATE),
    targets: byKey(raw.targets, targets, `${where}.targets`, (inner, key) => {
      const shown = record(inner, ['state', 'value'], `${where}.${key}`);
      return {
        state: oneOf(shown.state, weekStates, `${where}.${key}.state`),
        value: nullable(shown.value, (figureValue) => figure(figureValue, key, `${where}.${key}.value`)),
      };
    }),
    stockout: nullable(raw.stockout, (inner) => bool(inner, `${where}.stockout`)),
  };
}

export function parseDemandForecastDetail(value: unknown): DemandForecastDetail {
  const raw = record(value, [
    'asin', 'freshness', 'horizon', 'outcome', 'provenance', 'recent_weeks', 'schema_version',
    'stockout_weeks_last_13', 'targets', 'title', 'title_truncated', 'week_state_counts',
  ], 'detail');
  if (raw.schema_version !== 1) fail('schema_version');
  return {
    outcome: oneOf(raw.outcome, ['selected', 'not_forecast', 'not_in_forecast', 'unavailable'] as const, 'outcome'),
    asin: text(raw.asin, 'asin', /^[A-Z0-9]{10}$/),
    provenance: nullable(raw.provenance, provenance),
    freshness: freshness(raw.freshness),
    horizon: nullable(raw.horizon, horizon),
    title: nullable(raw.title, (inner) => text(inner, 'title')),
    title_truncated: bool(raw.title_truncated, 'title_truncated'),
    targets: nullable(raw.targets, (inner) => byKey(inner, targets, 'targets', targetDetail)),
    recent_weeks: nullable(raw.recent_weeks, (inner) => list(inner, 'recent_weeks', recentWeek)),
    stockout_weeks_last_13: nullable(raw.stockout_weeks_last_13, (inner) => count(inner, 'stockout_weeks_last_13')),
    week_state_counts: nullable(raw.week_state_counts, (inner) => {
      const counts = record(inner, ['inferred_zero', 'observed', 'unknown'], 'week_state_counts');
      return {
        observed: count(counts.observed, 'observed'),
        inferred_zero: count(counts.inferred_zero, 'inferred_zero'),
        unknown: count(counts.unknown, 'unknown'),
      };
    }),
  };
}

export function parseDemandForecastStatus(value: unknown): DemandForecastStatus {
  const raw = record(value, [
    'freshness', 'generation', 'hold', 'last_run', 'next_check_at', 'next_new_input_check_at',
    'rollback_active', 'rolled_back_from', 'schema_version',
  ], 'status');
  if (raw.schema_version !== 1) fail('schema_version');
  return {
    generation: nullable(raw.generation, (inner) => {
      const generation = record(inner, ['generation_id', 'input_end', 'published_at'], 'generation');
      return {
        generation_id: text(generation.generation_id, 'generation_id'),
        published_at: text(generation.published_at, 'published_at', INSTANT),
        input_end: text(generation.input_end, 'input_end', DATE),
      };
    }),
    freshness: freshness(raw.freshness),
    last_run: nullable(raw.last_run, (inner) => {
      const run = record(inner, ['at', 'failure_category', 'failure_family', 'outcome', 'retry_disposition', 'transient_retries'], 'last_run');
      return {
        outcome: oneOf(run.outcome, runOutcomes, 'last_run.outcome'),
        failure_category: nullable(run.failure_category, (shown) => text(shown, 'failure_category')),
        failure_family: nullable(run.failure_family, (shown) => oneOf(shown, failureFamilies, 'failure_family')),
        retry_disposition: oneOf(run.retry_disposition, ['suppress', 'retry'] as const, 'retry_disposition'),
        transient_retries: count(run.transient_retries, 'transient_retries'),
        at: text(run.at, 'last_run.at', INSTANT),
      };
    }),
    hold: oneOf(raw.hold, holds, 'hold'),
    rollback_active: bool(raw.rollback_active, 'rollback_active'),
    rolled_back_from: nullable(raw.rolled_back_from, (inner) => text(inner, 'rolled_back_from')),
    next_check_at: text(raw.next_check_at, 'next_check_at', INSTANT),
    next_new_input_check_at: text(raw.next_new_input_check_at, 'next_new_input_check_at', INSTANT),
  };
}

export function parseDemandForecastRefusal(value: unknown): DemandForecastRefusal {
  const raw = record(value, ['code', 'detail', 'refused', 'schema_version'], 'refusal');
  if (raw.schema_version !== 1) fail('schema_version');
  return {
    refused: oneOf(raw.refused, refusals, 'refused'),
    code: text(raw.code, 'code', /^ADLY-FORECAST_READ-[0-9]{4}$/),
    detail: text(raw.detail, 'detail'),
  };
}

// --- Fetching -------------------------------------------------------------------------------

const REFUSAL_STATUSES = new Set([400, 403, 404, 500, 503]);

async function fetchDecoded<T>(path: string, parse: (value: unknown) => T, signal?: AbortSignal): Promise<FetchResult<T>> {
  const response = await callApi(path, 'GET', undefined, signal);
  let body: unknown;
  try {
    body = await response.json();
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error;
    console.error(`Demand forecast response from ${path.split('?')[0]} was not JSON (status ${response.status}).`);
    return { kind: 'invalid', status: response.status };
  }
  try {
    if (response.ok) return { kind: 'ok', value: parse(body) };
    if (REFUSAL_STATUSES.has(response.status)) {
      return { kind: 'refused', status: response.status, refusal: parseDemandForecastRefusal(body) };
    }
  } catch {
    // The body is never logged: it may hold product names (frontend-auth-session).
  }
  console.error(`Demand forecast response from ${path.split('?')[0]} did not decode (status ${response.status}).`);
  return { kind: 'invalid', status: response.status };
}

export interface ListQuery { readonly status: string | null; readonly page: number }

export function fetchDemandForecastList(query: ListQuery, signal?: AbortSignal): Promise<FetchResult<DemandForecastList>> {
  const parameters = new URLSearchParams();
  if (query.status !== null) parameters.set('status', query.status);
  if (query.page !== 1) parameters.set('page', String(query.page));
  const suffix = parameters.toString();
  return fetchDecoded(`/api/demand-forecast${suffix ? `?${suffix}` : ''}`, parseDemandForecastList, signal);
}

export function fetchDemandForecastDetail(asin: string, signal?: AbortSignal): Promise<FetchResult<DemandForecastDetail>> {
  return fetchDecoded(`/api/demand-forecast?asin=${encodeURIComponent(asin)}`, parseDemandForecastDetail, signal);
}

export function fetchDemandForecastStatus(signal?: AbortSignal): Promise<FetchResult<DemandForecastStatus>> {
  return fetchDecoded('/api/demand-forecast/status', parseDemandForecastStatus, signal);
}

// --- Copy -----------------------------------------------------------------------------------

/** The order the page shows the measures in: demand first (design finding 6). */
export const displayTargets: readonly Target[] = ['units_ordered', 'units_shipped', 'revenue_shipped', 'cogs_shipped'];

/** The measure switch labels (L-MEASURE). */
export const targetCopy: Readonly<Record<Target, string>> = {
  units_ordered: 'Units ordered (demand)',
  units_shipped: 'Units shipped',
  revenue_shipped: 'Shipped revenue',
  cogs_shipped: 'Amazon wholesale cost',
};
/** The measure as a chart title or column name opens with it ("Units ordered by week"). */
export const measureCopy: Readonly<Record<Target, string>> = {
  units_ordered: 'Units ordered',
  units_shipped: 'Units shipped',
  revenue_shipped: 'Shipped revenue',
  cogs_shipped: 'Amazon wholesale cost',
};
/** The measure inside a sentence ("Forecast, 13 weeks starting Nov 8, 2026, units ordered"). */
export const measureLowerCopy: Readonly<Record<Target, string>> = {
  units_ordered: 'units ordered',
  units_shipped: 'units shipped',
  revenue_shipped: 'shipped revenue',
  cogs_shipped: 'Amazon wholesale cost',
};
/** The filter chips' measure word, from the list's `status_target` (G-05). */
export const filterTargetCopy: Readonly<Record<Target, { readonly article: 'a' | 'an'; readonly label: string }>> = {
  units_ordered: { article: 'an', label: 'ordered-units' },
  units_shipped: { article: 'a', label: 'shipped-units' },
  revenue_shipped: { article: 'a', label: 'shipped-revenue' },
  cogs_shipped: { article: 'a', label: 'wholesale-cost' },
};

/** The short label for a filter chip and a list cell (design "Reason copy"). */
export const skipReasonCopy: Readonly<Record<SkipReason, string>> = {
  ASIN_INACTIVE: 'No recent sales',
  TARGET_VALUE_MISSING: 'Gaps in its sales history',
  INSUFFICIENT_CONTEXT: 'Too little history',
  HOLDOUT_UNKNOWN: 'Recent weeks not known',
  HOLDOUT_ALL_ZERO: 'No sales in the test weeks',
  BACKTEST_BAND_UNORDERED: 'The accuracy test failed a check',
  SEASONAL_LAG_UNKNOWN: "Last year's matching weeks are unknown",
  SEASONAL_LAG_UNKNOWN_AT_ORIGIN: "Last year's matching weeks are unknown for the forecast weeks",
  SEASONAL_RESIDUALS_TOO_FEW: 'Too few complete seasons',
  HISTORY_TOO_SHORT_FOR_SEASON: 'Less than a year of history',
  ETS_INSUFFICIENT_KNOWN: 'Too few known weeks for the trend method',
  ETS_FIT_FAILED: "The trend method couldn't be fitted",
  ACCURACY_NOT_TESTABLE: "Accuracy couldn't be tested",
  FORECAST_NOT_PUBLISHABLE: "Didn't meet every condition to be shown",
  FORECAST_SHAPE_INVALID: 'The forecast failed a check',
  FORECAST_NOT_FINITE: 'The forecast failed a check',
  FORECAST_BAND_UNORDERED: 'The forecast failed a check',
  FORECAST_NEGATIVE: 'The forecast failed a check',
  PRODUCT_KEY_INVALID: "The product identifier isn't valid",
  DISPLAY_ASIN_COLLISION: 'Two products share one ASIN',
  CURRENCY_MIXED: 'Sales in more than one currency',
};
/** The product callout's sentence. `{measure}` is the selected measure, lower-case. */
export const reasonSentenceCopy: Readonly<Record<SkipReason, string>> = {
  ASIN_INACTIVE: "No sales of this product have been reported recently, so there's no recent pattern to forecast. The weeks below show what is known.",
  TARGET_VALUE_MISSING: "Vendor Central left some of this product's weekly values blank, so there aren't enough complete weeks to forecast.",
  INSUFFICIENT_CONTEXT: "This product doesn't have enough weeks of sales yet to forecast.",
  HOLDOUT_UNKNOWN: "Too many of this product's most recent weeks are not known to test a forecast against them.",
  HOLDOUT_ALL_ZERO: "This product sold nothing in the recent weeks a forecast is tested on, so its accuracy can't be measured.",
  BACKTEST_BAND_UNORDERED: "The test forecast's low, likely and high came out in the wrong order, so it wasn't used.",
  SEASONAL_LAG_UNKNOWN: "The weeks a year before the test weeks are not known, so the same-weeks-last-year comparison can't run.",
  SEASONAL_LAG_UNKNOWN_AT_ORIGIN: "The weeks a year before the forecast weeks are not known, so the same-weeks-last-year comparison can't run.",
  SEASONAL_RESIDUALS_TOO_FEW: "There aren't enough complete years of sales to compare this product with the same weeks last year.",
  HISTORY_TOO_SHORT_FOR_SEASON: "This product has less than a year of sales, so it can't be compared with the same weeks last year.",
  ETS_INSUFFICIENT_KNOWN: "Too few of this product's weeks are known for the trend and seasonality method to run.",
  ETS_FIT_FAILED: "The trend and seasonality method couldn't fit this product's sales.",
  ACCURACY_NOT_TESTABLE: "No simpler method could be tested on this product, so there's nothing to check a forecast against.",
  FORECAST_NOT_PUBLISHABLE: 'No forecast met every condition required to show it for this product.',
  FORECAST_SHAPE_INVALID: "The forecast didn't cover the right weeks, so it isn't shown.",
  FORECAST_NOT_FINITE: "The forecast held a value that isn't a number, so it isn't shown.",
  FORECAST_BAND_UNORDERED: "The forecast's low, likely and high came out in the wrong order, so it isn't shown.",
  FORECAST_NEGATIVE: "The forecast went below zero, so it isn't shown.",
  PRODUCT_KEY_INVALID: "This product's identifier in Vendor Central isn't a valid ASIN.",
  DISPLAY_ASIN_COLLISION: "Two products in Vendor Central share this ASIN, so their sales can't be told apart.",
  CURRENCY_MIXED: "This product's {measure} was reported in more than one currency, so it can't be added up week to week.",
};
export function reasonSentence(reason: SkipReason, target: Target): string {
  return reasonSentenceCopy[reason].replace('{measure}', measureLowerCopy[target]);
}

/** A next step with the next new-data update's date, and the form used while status is loading or failed. */
interface NextStep { readonly dated: (next: string) => string; readonly undated: string }
const tryAgain: NextStep = { dated: (next) => `The update on ${next} tries again.`, undated: 'The next weekly update tries again.' };
const tryNewest: NextStep = {
  dated: (next) => `The update on ${next} tries again with the newest weeks.`,
  undated: 'The next weekly update tries again with the newest weeks.',
};
const moreWeeks: NextStep = {
  dated: () => 'A forecast becomes possible as more weeks of sales arrive.',
  undated: 'A forecast becomes possible as more weeks of sales arrive.',
};
function fixed(text: string): NextStep {
  return { dated: () => text, undated: text };
}
export const reasonNextStepCopy: Readonly<Record<SkipReason, NextStep>> = {
  ASIN_INACTIVE: {
    dated: (next) => `If it's still on sale, plan it by hand for now. The update on ${next} will check again after new sales data arrives; a forecast appears only once there's enough complete history.`,
    undated: "If it's still on sale, plan it by hand for now. The next weekly update will check again after new sales data arrives; a forecast appears only once there's enough complete history.",
  },
  TARGET_VALUE_MISSING: {
    dated: (next) => `The update on ${next} tries again. If the gaps stay, ask your administrator to check this product's Vendor Central sales report.`,
    undated: "The next weekly update tries again. If the gaps stay, ask your administrator to check this product's Vendor Central sales report.",
  },
  INSUFFICIENT_CONTEXT: {
    dated: (next) => `A forecast becomes possible once it has enough complete weeks of sales. The next update that can use new sales data is ${next}.`,
    undated: 'A forecast becomes possible once it has enough complete weeks of sales.',
  },
  HOLDOUT_UNKNOWN: tryNewest,
  HOLDOUT_ALL_ZERO: tryNewest,
  BACKTEST_BAND_UNORDERED: tryAgain,
  SEASONAL_LAG_UNKNOWN: tryAgain,
  SEASONAL_LAG_UNKNOWN_AT_ORIGIN: tryAgain,
  SEASONAL_RESIDUALS_TOO_FEW: moreWeeks,
  HISTORY_TOO_SHORT_FOR_SEASON: moreWeeks,
  ETS_INSUFFICIENT_KNOWN: tryAgain,
  ETS_FIT_FAILED: tryAgain,
  ACCURACY_NOT_TESTABLE: tryAgain,
  FORECAST_NOT_PUBLISHABLE: tryAgain,
  FORECAST_SHAPE_INVALID: tryAgain,
  FORECAST_NOT_FINITE: tryAgain,
  FORECAST_BAND_UNORDERED: tryAgain,
  FORECAST_NEGATIVE: tryAgain,
  PRODUCT_KEY_INVALID: fixed('Ask your administrator to check the product in Vendor Central.'),
  DISPLAY_ASIN_COLLISION: fixed('Ask your administrator to check the products in Vendor Central.'),
  CURRENCY_MIXED: fixed('Ask your administrator which marketplace this product should report in.'),
};
/** The next step for a withheld measure; `nextNewInputCheckAt` is null while status is loading or failed. */
export function reasonNextStep(reason: SkipReason, nextNewInputCheckAt: string | null): string {
  const step = reasonNextStepCopy[reason];
  return nextNewInputCheckAt === null ? step.undated : step.dated(formatIsoInstantDate(nextNewInputCheckAt));
}

/** Plain model names (L-MODEL, G-15). */
export const modelCopy: Readonly<Record<ModelName, string>> = {
  chronos2: 'the Chronos-2 forecasting model',
  seasonal_naive: 'the same weeks last year',
  ets: 'the trend and seasonality method',
};
export const runOutcomeCopy: Readonly<Record<RunOutcome, string>> = {
  published: 'Published a new forecast',
  held_quality: 'Held by a quality check',
  failed: 'Failed',
  rolled_back: 'Rolled back to an earlier forecast',
};
export const failureFamilyCopy: Readonly<Record<FailureFamily, string>> = {
  quality_check: 'A quality check held the run',
  input_not_ready: "The week's sales data wasn't ready",
  forecast_job: 'The forecasting job failed',
  input_build: 'Preparing the sales history failed',
  retry_exhausted: 'Retries ran out',
  other: 'Something else went wrong',
};
export const freshnessCauseCopy: Readonly<Record<FreshnessCause, string>> = {
  age_over_limit: 'It is older than the freshness limit',
  last_run_failed: 'The last forecast run failed',
  last_run_held: 'A quality check held the last forecast run',
};
/** How a recent week's value is known (ADR-156, L-STATE): an inferred zero is never shown as observed. */
export const weekStateCopy: Readonly<Record<WeekState, string>> = {
  observed: 'Reported',
  inferred_zero: 'No sales reported, counted as zero',
  unknown: 'Not known',
};
/** The miss line when no average weekly miss is served. */
export const maseGapCopy: Readonly<Record<MaseGap, string>> = {
  HISTORY_UNCHANGING: "Sales didn't change from week to week before the test, so there's no usual change to compare the miss with.",
  MASE_SCALE_UNKNOWN: 'Not enough history to measure the usual week-to-week change.',
};
export const holdCopy: Readonly<Record<Hold, string>> = {
  none: 'No hold',
  quality: 'Held by a quality check; the last good forecast is still shown',
};
export const weekStartCopy: Readonly<Record<Horizon['week_starts_on'], string>> = {
  sunday: 'Sunday to Saturday weeks',
};
/** The freshness pill (L-FRESH-OK, L-FRESH-STALE); an unavailable forecast has no pill. */
export const freshnessPillCopy: Readonly<Record<'fresh' | 'stale', (inputEnd: string) => string>> = {
  fresh: (inputEnd) => `Up to date · sales through ${formatIsoDate(inputEnd)}`,
  stale: (inputEnd) => `Out of date · sales through ${formatIsoDate(inputEnd)}`,
};

function lowerFirst(text: string): string {
  return text.charAt(0).toLowerCase() + text.slice(1);
}
function plural(count: number, one: string, many: string): string {
  return `${count} ${count === 1 ? one : many}`;
}

/**
 * One out-of-date banner line per served cause (E-STALE). A line that would quote the latest
 * update's date uses its no-date form while status is loading or failed.
 */
export function staleCauseLine(cause: FreshnessCause, freshness: Freshness, status: DemandForecastStatus | null): string {
  const run = status?.last_run ?? null;
  const when = run === null ? 'The latest weekly update' : `The weekly update on ${formatIsoInstantDate(run.at)}`;
  if (cause === 'age_over_limit') {
    const limit = `forecasts count as up to date for ${plural(freshness.stale_after_days, 'day', 'days')}.`;
    return freshness.age_days === null
      ? `It's older than the time it counts as up to date: ${limit}`
      : `It was made ${plural(freshness.age_days, 'day', 'days')} ago; ${limit}`;
  }
  if (cause === 'last_run_held') return `${when} was held by a quality check.`;
  const family = run?.failure_family ?? null;
  return family === null ? `${when} didn't finish.` : `${when} didn't finish: ${lowerFirst(failureFamilyCopy[family])}.`;
}
/** The banner's closing line: what the page shows and when new sales data can next be used. */
export function staleShownLine(provenance: Provenance, status: DemandForecastStatus | null): string {
  const shown = `You're seeing the forecast made ${formatIsoInstantDate(provenance.published_at)} from sales through ${formatIsoDate(provenance.input_end)}.`;
  return status === null
    ? shown
    : `${shown} The next update that can use new sales data is ${formatIsoInstantShort(status.next_new_input_check_at)}.`;
}

/** "today", "1 day ago", "N days ago" from the served age. */
function ageText(ageDays: number): string {
  return ageDays === 0 ? 'today' : `${plural(ageDays, 'day', 'days')} ago`;
}
/**
 * The freshness pop-up (I-FRESH, S8): every status field the old status panel showed (D4). `made`
 * and `rule` read the list or detail; `schedule` reads status, and is null while it is loading or
 * failed.
 */
export function statusLines(
  source: { readonly provenance: Provenance; readonly freshness: Freshness },
  status: DemandForecastStatus | null,
): { readonly made: string; readonly schedule: readonly string[] | null; readonly rule: string } {
  const madeOn = formatIsoInstantDate(source.provenance.published_at);
  const age = source.freshness.age_days === null ? '' : ` (${ageText(source.freshness.age_days)})`;
  const made = `Made ${madeOn}${age} from Vendor Central sales through ${formatIsoDate(source.provenance.input_end)}.`;
  const rule = `A forecast counts as up to date for ${plural(source.freshness.stale_after_days, 'day', 'days')} after it's made, unless the latest update failed or was held.`;
  if (status === null) return { made, schedule: null, rule };
  const run = status.last_run;
  const schedule: string[] = [];
  if (status.rollback_active && run?.outcome === 'rolled_back') {
    schedule.push(`The latest update was undone on ${formatIsoInstantDate(run.at)}, so you're seeing the forecast made ${madeOn}.`);
  } else if (status.rollback_active) {
    schedule.push(`An earlier forecast was restored, so you're seeing the forecast made ${madeOn}.`);
  } else if (run === null) {
    schedule.push('No update has run yet.');
  } else {
    const family = run.failure_family === null ? '' : ` ${failureFamilyCopy[run.failure_family]}.`;
    schedule.push(`Latest update, ${formatIsoInstantDate(run.at)}: ${lowerFirst(runOutcomeCopy[run.outcome])}.${family}`);
  }
  if (status.hold !== 'none') schedule.push(`${holdCopy[status.hold]}.`);
  schedule.push(`Next daily check: ${formatIsoInstantShort(status.next_check_at)}.`);
  schedule.push(`Next check that can use new sales data: ${formatIsoInstantShort(status.next_new_input_check_at)}.`);
  return { made, schedule, rule };
}

/** An info pop-up: its title and its text (design "Info pop-ups"). */
export interface InfoText { readonly title: string; readonly text: string }
export const INFO = {
  range: {
    title: 'Low, likely and high',
    text: "Low, likely and high are the forecast's 10th, 50th and 90th percentile forecasts for each week; likely is the middle one. The range from low to high is not a guarantee: actual demand can land outside it. In statistics: p10, p50 and p90.",
  },
  total: {
    title: 'Likely total',
    text: "The likely weekly forecasts for these weeks, added up when the forecast was made. Low and high don't add up across weeks, so there is no low or high total.",
  },
  rank: (defaultViewWeeks: number, firstWeek: string): InfoText => ({
    title: 'Rank',
    text: `Products are ranked by likely units ordered over the ${defaultViewWeeks} weeks starting ${formatIsoDate(firstWeek)}. The order stays the same when you pick fewer weeks.`,
  }),
  short: {
    title: 'Weeks shipped less than ordered',
    text: 'Weeks in which Amazon shipped fewer units to customers than customers ordered. It can mean stock ran short, or that orders were still waiting to ship. A week counts only when both figures were reported. A product with many such weeks needs a supply check before a promotion.',
  },
  tested: (accuracy: Accuracy | null, target: Target): InfoText => ({
    title: 'How this forecast tested',
    text: accuracy === null || accuracy.weeks_scored === 0
      ? "This forecast couldn't be tested on enough recent weeks, so how often actuals land inside the range isn't measured."
      : `Before the forecast was shown, it forecast this product's most recent weeks from the weeks before them. In ${accuracy.weeks_in_range} of those ${accuracy.weeks_scored} weeks, actual ${measureLowerCopy[target]} landed inside the range from low to high; in the other weeks it landed below or above it.`,
  }),
  miss: {
    title: 'Average weekly miss',
    text: 'How far the likely forecast was from actual sales in the test weeks, compared with how much sales usually move from one week to the next. Below 1× means the forecast missed by less than a typical weekly change. In statistics: MASE, the mean absolute scaled error.',
  },
  state: {
    title: 'How we know each week',
    text: "Reported: Vendor Central reported the week. No sales reported, counted as zero: the week's report arrived with no sales for this product. Not known: either the week's report didn't arrive, or the product had no sales for long enough that a zero can't be assumed.",
  },
  fresh: { title: 'About this forecast', text: '' },
  cogs: {
    title: 'Amazon wholesale cost',
    text: "What Amazon pays the vendor for the units it shipped. From the vendor's side it is revenue received. It is not the vendor's own cost of goods.",
  },
  measures: {
    title: 'The four measures',
    text: "Units ordered (demand): units customers ordered. Units shipped: units Amazon shipped to customers. Shipped revenue: revenue from the units Amazon shipped, net of returns, as the vendor reports it. Amazon wholesale cost: what Amazon pays the vendor for the units it shipped. It is not the vendor's own cost of goods.",
  },
} as const;

/** Render one server figure as a bare number: a grouped count, or money with its code. */
export function figureNumber(shown: Figure, target: Target, currencyCode: string | null): string {
  if (moneyTargets.includes(target)) {
    if (currencyCode === null) throw new TypeError('A money figure needs its currency.');
    return formatCurrency(shown.display, currencyCode);
  }
  return formatCount(shown.display);
}
/** Render one server figure for a tile or sentence: money with its code, units as "{n} units". */
export function figureText(shown: Figure, target: Target, currencyCode: string | null): string {
  const number = figureNumber(shown, target, currencyCode);
  return moneyTargets.includes(target) ? number : `${number} units`;
}

function viewKey(viewWeeks: number): ViewKey {
  const key = String(viewWeeks);
  if (!(views as readonly string[]).includes(key)) throw new RangeError(`No served view of ${viewWeeks} weeks.`);
  return key as ViewKey;
}
function windowStart(horizon: Horizon): string {
  const first = horizon.weeks[0];
  if (first === undefined) throw new RangeError('The horizon has no weeks.');
  return formatIsoDate(first);
}
/** The product's name as a subject: the served title, or its ASIN when it has none (G-12). */
export function productSubject(product: { readonly title: string | null; readonly asin: string }): string {
  return product.title ?? product.asin;
}

/**
 * The list's answer sentence (S1, G-12). Only an unfiltered page 1 answers: the template when
 * its first row is rank 1 with a served ordered-units total for the chosen view, the count
 * fallback otherwise. Page 2 or a filter shows none. The rank is by the default view, so the
 * sentence names it, and it quotes the chosen view's served total.
 */
export function listAnswer(list: DemandForecastList, viewWeeks: number): Answer | null {
  const horizon = list.horizon;
  if (list.outcome !== 'complete' || horizon === null) return null;
  if (list.query.status !== null || list.page !== 1) return null;
  const start = windowStart(horizon);
  const first = list.entries[0];
  const total = first?.targets.units_ordered.p50_totals[viewKey(viewWeeks)] ?? null;
  if (first !== undefined && first.rank === 1 && total !== null) {
    return {
      kind: 'answer',
      text: `${productSubject(first)} leads the ${horizon.default_view_weeks}-week forecast: customers are likely to order ${formatCount(total.display)} units in the ${viewWeeks} weeks starting ${start}.`,
    };
  }
  return {
    kind: 'fallback',
    text: list.item_count === null
      ? `This forecast covers the ${viewWeeks} weeks starting ${start}.`
      : `${plural(list.item_count, 'product', 'products')} in this forecast for the ${viewWeeks} weeks starting ${start}.`,
  };
}

/** The product's answer sentence for one measure (L-ANSWER-DETAIL); none when that total isn't served. */
export function detailAnswer(detail: DemandForecastDetail, target: Target, viewWeeks: number): Answer | null {
  const row = detail.targets?.[target];
  const horizon = detail.horizon;
  if (row === undefined || horizon === null) return null;
  const total = row.p50_totals[viewKey(viewWeeks)];
  if (total === null) return null;
  const subject = productSubject(detail);
  const amount = figureNumber(total, target, row.currency_code);
  const window = `in the ${viewWeeks} weeks starting ${windowStart(horizon)}.`;
  const text = {
    units_ordered: `Customers are likely to order ${amount} units of ${subject} ${window}`,
    units_shipped: `Amazon is likely to ship ${amount} units of ${subject} ${window}`,
    revenue_shipped: `Shipped revenue for ${subject} is likely to be ${amount} ${window}`,
    cogs_shipped: `Amazon's wholesale cost for shipped units of ${subject} is likely to be ${amount} ${window}`,
  }[target];
  return { kind: 'answer', text };
}

/** Consecutive recent weeks with the same served state for one measure, in served order. */
export interface WeekRun {
  readonly state: WeekState;
  readonly weeks: readonly RecentWeek[];
  /** The run's dates without the year ("Aug 9–Sep 13"). */
  readonly range: string;
}
export function weekRuns(recentWeeks: readonly RecentWeek[], target: Target): WeekRun[] {
  const runs: { state: WeekState; weeks: RecentWeek[] }[] = [];
  for (const week of recentWeeks) {
    const state = week.targets[target].state;
    const last = runs[runs.length - 1];
    if (last !== undefined && last.state === state) last.weeks.push(week);
    else runs.push({ state, weeks: [week] });
  }
  return runs.map((run) => {
    const first = run.weeks[0]?.week_start ?? '';
    const end = run.weeks[run.weeks.length - 1]?.week_start ?? first;
    return { state: run.state, weeks: run.weeks, range: formatIsoDateRangeShort(first, end) };
  });
}

/** The current complete list's product count, or null before one has loaded (G-08). */
export function itemCountOf(list: DemandForecastList | null): number | null {
  return list !== null && list.outcome === 'complete' ? list.item_count : null;
}
/** The search hint's example ASIN, kept with the generation it came from (G-08). */
export interface ExampleAsin { readonly generationId: string; readonly asin: string }
/** Only an unfiltered page 1 with entries supplies the example: its first row's ASIN. */
export function exampleAsinFrom(list: DemandForecastList): ExampleAsin | null {
  const first = list.entries[0];
  if (list.outcome !== 'complete' || list.provenance === null || first === undefined) return null;
  if (list.query.status !== null || list.page !== 1) return null;
  return { generationId: list.provenance.generation_id, asin: first.asin };
}
/** The example ASIN, shown only while the page shows the generation it came from. */
export function currentExampleAsin(example: ExampleAsin | null, generationId: string | null): string | null {
  return example !== null && generationId !== null && example.generationId === generationId ? example.asin : null;
}
/** The inline search error (E-ASIN); without an example while none is current. */
export function asinErrorText(example: string | null): string {
  return example === null ? 'An ASIN is 10 letters or digits.' : `An ASIN is 10 letters or digits, for example ${example}.`;
}

/** The seeded assistant questions (S10): each names the ASIN and dates, so no page context is needed. */
export const askQuestions = {
  list: (horizon: Horizon) => `Which products have the highest forecast demand in the ${horizon.default_view_weeks} weeks starting ${windowStart(horizon)}?`,
  product: (asin: string) => `Summarize the demand forecast for ${asin} beside its recent weeks.`,
  range: (asin: string, viewWeeks: number, horizon: Horizon) => `Explain the low, likely and high demand forecast for ${asin} in the ${viewWeeks} weeks starting ${windowStart(horizon)}.`,
  why: (asin: string) => `Why does ${asin} have no demand forecast?`,
} as const;
