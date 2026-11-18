// SPDX-License-Identifier: MIT-0
import { formatCount, formatCurrency } from './exact-format';
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
const weekStates = ['observed', 'inferred_zero', 'unknown'] as const;
export type WeekState = typeof weekStates[number];
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
  readonly wql: string; readonly mase: string | null; readonly mase_gap: string | null;
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
    mase_gap: nullable(raw.mase_gap, (inner) => oneOf(inner, ['HISTORY_UNCHANGING', 'MASE_SCALE_UNKNOWN'] as const, 'mase_gap')),
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

export const targetCopy: Readonly<Record<Target, string>> = {
  units_ordered: 'Demand (ordered units)',
  units_shipped: 'Shipped units',
  revenue_shipped: 'Shipped revenue',
  cogs_shipped: 'Shipped cost of goods sold',
};
const unitWords: Readonly<Record<'units_ordered' | 'units_shipped', string>> = {
  units_ordered: 'units ordered',
  units_shipped: 'units shipped',
};

export const skipReasonCopy: Readonly<Record<SkipReason, string>> = {
  ASIN_INACTIVE: 'No recent sales',
  TARGET_VALUE_MISSING: 'Missing values in its history',
  INSUFFICIENT_CONTEXT: 'Too little history',
  HOLDOUT_UNKNOWN: 'Too many unknown recent weeks to test accuracy',
  HOLDOUT_ALL_ZERO: 'No sales in the test weeks',
  BACKTEST_BAND_UNORDERED: 'The accuracy test gave an inconsistent range',
  SEASONAL_LAG_UNKNOWN: "Last year's matching weeks are unknown",
  SEASONAL_LAG_UNKNOWN_AT_ORIGIN: "Last year's matching weeks are unknown for the forecast weeks",
  SEASONAL_RESIDUALS_TOO_FEW: 'Too few complete seasons',
  HISTORY_TOO_SHORT_FOR_SEASON: 'Less than a year of history',
  ETS_INSUFFICIENT_KNOWN: 'Too few known weeks for the trend model',
  ETS_FIT_FAILED: 'The trend model could not be fitted',
  ACCURACY_NOT_TESTABLE: "Accuracy couldn't be tested",
  FORECAST_NOT_PUBLISHABLE: 'No model passed the accuracy check',
  FORECAST_SHAPE_INVALID: 'The forecast had the wrong shape',
  FORECAST_NOT_FINITE: 'The forecast was not a number',
  FORECAST_BAND_UNORDERED: 'The forecast range was inconsistent',
  FORECAST_NEGATIVE: 'The forecast was below zero',
  PRODUCT_KEY_INVALID: 'The product identifier is not valid',
  DISPLAY_ASIN_COLLISION: 'Two products share one ASIN',
  CURRENCY_MIXED: 'Sales in more than one currency',
};
export const modelCopy: Readonly<Record<ModelName, string>> = {
  chronos2: 'Chronos-2 forecast model',
  seasonal_naive: "Last year's matching weeks",
  ets: 'Trend and seasonality model',
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
export const accuracyClassCopy: Readonly<Record<AccuracyClass, string>> = {
  beat_naive: "Beat repeating last week's value",
  not_beat_naive: "Did not beat repeating last week's value",
  history_unchanging: 'History never changed, so there is no baseline to beat',
  scale_unknown: 'Too few complete weeks to measure the baseline',
};
/** How a recent week's value is known (ADR-156): an inferred zero is never shown as observed. */
export const weekStateCopy: Readonly<Record<WeekState, string>> = {
  observed: 'Reported by Vendor Central',
  inferred_zero: 'No sales reported, counted as zero',
  unknown: "Not known: the week's sales data is missing",
};
export const holdCopy: Readonly<Record<Hold, string>> = {
  none: 'No hold',
  quality: 'Held by a quality check; the last good forecast is still shown',
};

/** Render one server figure: money with its currency, units as a grouped count and unit word. */
export function figureText(shown: Figure, target: Target, currencyCode: string | null): string {
  if (moneyTargets.includes(target)) {
    if (currencyCode === null) throw new TypeError('A money figure needs its currency.');
    return formatCurrency(shown.display, currencyCode);
  }
  return `${formatCount(shown.display)} ${unitWords[target as 'units_ordered' | 'units_shipped']}`;
}
