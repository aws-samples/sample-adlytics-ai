// SPDX-License-Identifier: MIT-0
/**
 * Strict browser decoder for `RetailProductDetail` v1, the baked child of Retail health
 * (ADR-125). Every figure, sentence and label is the server's; the browser never totals the
 * ad-attributed channels, never adds them to shipped revenue and never derives a ratio.
 */
import { type Change, parseChange } from './change';
import { coverageEntry, EVIDENCE_STATUS_KEY, type EvidenceStatusV2, isWithheld, parseEvidenceStatus } from './evidence-status';
import { isDisplayValue } from './display-value';
import type { DateWindow } from './presentation';
import {
  AMC_RETAIL_PURCHASES_KEY,
  parseRetailFixedStatuses,
  parseRetailInsightSlot,
  RETAIL_COMPARISON_DECODER,
  RETAIL_HEALTH_TOTAL_METRICS,
  RETURN_REASONS_KEY,
  type RetailComparison,
  type RetailFixedStatuses,
  type RetailInsightSlot,
  type RetailPointValue,
} from './retail-health';

type AdMetric = 'purchases' | 'attributed_revenue_14d';
const SOURCING_WITHHELD = ['revenue_ordered', 'conversion_rate_retail', 'fulfilment_rate', 'units_ordered', 'revenue_per_glance_view'];
const NUMBER = /^[+-]?(?:0|[1-9]\d*)(?:\.\d+)?$/;
const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

export interface RetailDailyPoint {
  readonly date: string;
  readonly current: RetailPointValue;
  readonly prior: RetailPointValue;
}

export interface RetailAdValue {
  readonly display_value: string | null;
  readonly period: DateWindow;
  readonly units: { readonly kind: 'count' | 'currency'; readonly currency_code: string | null };
  readonly value: number | string;
}

export interface RetailAdComparison {
  readonly metric: AdMetric;
  readonly current: RetailAdValue;
  readonly prior: RetailAdValue;
  readonly change: Change;
}

export interface RetailAdChannelRow {
  readonly channel: string;
  readonly label: string;
  readonly purchases: RetailAdComparison;
  readonly revenue: RetailAdComparison;
}

export interface RetailProductInsightStat {
  readonly change: Change | null;
  readonly label: string;
  readonly text: string;
}

export interface RetailProductInsight {
  readonly provenance: string;
  readonly slot: RetailInsightSlot;
  readonly stats: readonly RetailProductInsightStat[];
}

export interface RetailProductDetailArtifact {
  readonly kind: 'retail_product_detail';
  readonly outcome: 'selected' | 'insufficient';
  readonly period: DateWindow;
  readonly prior_period: DateWindow;
  readonly product: { readonly title: string; readonly child_asin: string; readonly parent_asin: string };
  readonly distributor_view: 'MANUFACTURING' | 'SOURCING';
  readonly totals: readonly RetailComparison[];
  readonly absences: readonly { readonly metric: string; readonly reason: string }[];
  readonly sales_daily: readonly RetailDailyPoint[];
  readonly traffic_daily: readonly RetailDailyPoint[];
  /** Per channel, never totalled; a typed absence carries the server's reason instead. */
  readonly ad_attributed: {
    readonly basis: string;
    readonly rows: readonly RetailAdChannelRow[];
    readonly reason_code: string | null;
    readonly reason: string | null;
  } | null;
  readonly insights: readonly RetailProductInsight[];
  readonly no_insight_line: string | null;
  readonly boundaries: readonly string[];
  readonly evidence_status: EvidenceStatusV2 | null;
  readonly fixed_statuses: RetailFixedStatuses;
}

function fail(): never {
  throw new TypeError('Retail product detail is malformed.');
}

function record(value: unknown, fields: readonly string[]): Record<string, unknown> {
  if (
    typeof value !== 'object' || value === null || Array.isArray(value) ||
    Object.keys(value).length !== fields.length || fields.some((field) => !(field in value))
  ) return fail();
  return value as Record<string, unknown>;
}

function text(value: unknown): string {
  if (typeof value !== 'string' || !value.trim()) return fail();
  return value;
}

function array(value: unknown): unknown[] {
  if (!Array.isArray(value)) return fail();
  return value;
}

function nullableText(value: unknown): string | null {
  return value === null ? null : text(value);
}

/** Decode one ad-attributed value served for `period`; shared with the Products and ads part. */
export function adValue(value: unknown, period: DateWindow): RetailAdValue {
  const item = record(value, ['basis', 'display_value', 'period', 'units', 'value']);
  if (item.basis !== 'ad_attributed') return fail();
  const units = record(item.units, ['kind', 'currency_code']);
  if (units.kind !== 'count' && units.kind !== 'currency') return fail();
  if (units.currency_code !== null && (typeof units.currency_code !== 'string' || !/^[A-Z]{3}$/.test(units.currency_code))) return fail();
  const raw = item.value;
  if ((typeof raw !== 'number' && typeof raw !== 'string') || (typeof raw === 'string' && !NUMBER.test(raw)) || Number(raw) < 0) return fail();
  const valuePeriod = RETAIL_COMPARISON_DECODER.window(item.period);
  if (!RETAIL_COMPARISON_DECODER.sameWindow(valuePeriod, period)) return fail();
  if (!isDisplayValue(item.display_value, units.kind)) return fail();
  return {
    display_value: item.display_value as string | null,
    period: valuePeriod,
    units: { kind: units.kind, currency_code: units.currency_code as string | null },
    value: raw,
  };
}

function adComparison(value: unknown, metric: AdMetric, period: DateWindow, prior: DateWindow): RetailAdComparison {
  const item = record(value, ['metric', 'current', 'prior', 'change']);
  if (item.metric !== metric) return fail();
  const change = parseChange(item.change);
  if (change === undefined || change === null) return fail();
  return { metric, current: adValue(item.current, period), prior: adValue(item.prior, prior), change };
}

function daily(value: unknown, period: DateWindow, prior: DateWindow): RetailDailyPoint[] {
  const points = array(value).map((entry) => {
    const item = record(entry, ['date', 'current', 'prior']);
    const day = text(item.date);
    if (!ISO_DATE.test(day)) return fail();
    return {
      current: RETAIL_COMPARISON_DECODER.valueMetric(item.current),
      date: day,
      prior: RETAIL_COMPARISON_DECODER.valueMetric(item.prior),
    };
  });
  const days = (window: DateWindow) => {
    const start = Date.parse(`${window.start}T00:00:00Z`);
    const end = Date.parse(`${window.end}T00:00:00Z`);
    return Array.from({ length: (end - start) / 86_400_000 + 1 }, (_, index) => new Date(start + index * 86_400_000).toISOString().slice(0, 10));
  };
  const current = days(period);
  const earlier = days(prior);
  if (
    points.length !== current.length ||
    points.some((point, index) => point.date !== current[index] || point.current.period.start !== current[index] || point.prior.period.start !== earlier[index])
  ) return fail();
  return points;
}

function stat(value: unknown): RetailProductInsightStat {
  const item = record(value, ['label', 'text', 'change']);
  const change = item.change === null ? null : parseChange(item.change);
  if (change === undefined) return fail();
  return { change, label: text(item.label), text: text(item.text) };
}

const FIELDS = [
  'kind', 'outcome', 'period', 'prior_period', 'product', 'distributor_view', 'totals', 'absences',
  'sales_daily', 'traffic_daily', 'ad_attributed', 'insights', 'no_insight_line', 'boundaries',
  EVIDENCE_STATUS_KEY, RETURN_REASONS_KEY, AMC_RETAIL_PURCHASES_KEY,
] as const;

export function parseRetailProductDetail(raw: unknown): RetailProductDetailArtifact {
  const item = record(raw, FIELDS);
  if (item.kind !== 'retail_product_detail' || (item.outcome !== 'selected' && item.outcome !== 'insufficient')) return fail();
  if (item.distributor_view !== 'MANUFACTURING' && item.distributor_view !== 'SOURCING') return fail();
  const period = RETAIL_COMPARISON_DECODER.window(item.period);
  const prior_period = RETAIL_COMPARISON_DECODER.window(item.prior_period);
  if (!RETAIL_COMPARISON_DECODER.previous(period, prior_period)) return fail();
  const product = record(item.product, ['title', 'child_asin', 'parent_asin']);
  const totals = array(item.totals).map((entry) => RETAIL_COMPARISON_DECODER.comparison(entry, period, prior_period));
  const absences = array(item.absences).map((entry) => {
    const absence = record(entry, ['metric', 'reason']);
    return { metric: text(absence.metric), reason: text(absence.reason) };
  });
  const evidence_status = item.evidence_status === null ? null : parseEvidenceStatus(item.evidence_status, period);
  const selected = item.outcome === 'selected';
  if (selected) {
    const absent = new Set(absences.map((entry) => entry.metric));
    if (item.distributor_view === 'SOURCING') for (const metric of SOURCING_WITHHELD) absent.add(metric);
    const expected = RETAIL_HEALTH_TOTAL_METRICS.filter((metric) => !absent.has(metric));
    if (totals.map((entry) => entry.metric).join('|') !== expected.join('|') || evidence_status === null) return fail();
    if (totals.some((entry) => entry.withheld !== isWithheld(coverageEntry(evidence_status, entry.metric)))) return fail();
  } else if (totals.length > 0 || evidence_status !== null) return fail();
  const ads = item.ad_attributed === null ? null : (() => {
    const section = record(item.ad_attributed, ['basis', 'rows', 'reason_code', 'reason']);
    const rows = array(section.rows).map((entry) => {
      const row = record(entry, ['channel', 'label', 'purchases', 'revenue']);
      return {
        channel: text(row.channel),
        label: text(row.label),
        purchases: adComparison(row.purchases, 'purchases', period, prior_period),
        revenue: adComparison(row.revenue, 'attributed_revenue_14d', period, prior_period),
      };
    });
    const reason_code = nullableText(section.reason_code);
    const reason = nullableText(section.reason);
    if ((reason_code === null) !== (reason === null) || (reason !== null && rows.length > 0) || (reason === null && rows.length === 0)) return fail();
    return { basis: text(section.basis), reason, reason_code, rows };
  })();
  if (selected && ads === null) return fail();
  const insights = array(item.insights).map((entry) => {
    const insight = record(entry, ['slot', 'stats', 'provenance']);
    const slot = parseRetailInsightSlot(insight.slot, 'retail-movers');
    if (slot.state !== 'filled' || slot.next_step !== null) return fail();
    return { provenance: text(insight.provenance), slot, stats: array(insight.stats).map(stat) };
  });
  const no_insight_line = nullableText(item.no_insight_line);
  if (selected && (insights.length > 0) === (no_insight_line !== null)) return fail();
  return {
    absences,
    ad_attributed: ads,
    boundaries: array(item.boundaries).map(text),
    distributor_view: item.distributor_view,
    evidence_status,
    fixed_statuses: parseRetailFixedStatuses(item),
    insights,
    kind: 'retail_product_detail',
    no_insight_line,
    outcome: item.outcome,
    period,
    prior_period,
    product: { child_asin: text(product.child_asin), parent_asin: text(product.parent_asin), title: text(product.title) },
    sales_daily: daily(item.sales_daily, period, prior_period),
    totals,
    traffic_daily: daily(item.traffic_daily, period, prior_period),
  };
}

/** One channel's ad credit for one product, as Products and ads shows it (render format 12). */
export interface RetailProductAdCell {
  readonly channel: string;
  readonly label: string;
  readonly purchases: RetailAdValue;
  readonly revenue: RetailAdValue;
  readonly revenue_change: Change;
}

/** One served row's ad cells and conversion, or its ad section's served absence. */
export interface RetailProductAdsRow {
  readonly child_asin: string;
  /** Units per glance view from the product's detail; null where it serves no such tile. */
  readonly conversion: RetailComparison['current'] | null;
  readonly channels: readonly RetailProductAdCell[];
  readonly reason_code: string | null;
  readonly reason: string | null;
}

/** `RetailProductAds` v1: every served row's ad credit, baked with the parent's revision. */
export interface RetailProductAdsArtifact {
  readonly kind: 'retail_product_ads';
  readonly period: DateWindow;
  readonly prior_period: DateWindow;
  readonly basis: string;
  readonly rows: readonly RetailProductAdsRow[];
}

/**
 * Strictly decode the Products and ads part served beside `parent`: its window is the parent's
 * and its rows are the parent's rows in their baked order, by child ASIN. Nothing is totalled.
 */
export function parseRetailProductAds(raw: unknown, parent: { readonly period: DateWindow; readonly prior_period: DateWindow; readonly rows: readonly { readonly child_asin: string }[] }): RetailProductAdsArtifact {
  const item = record(raw, ['kind', 'period', 'prior_period', 'basis', 'rows']);
  if (item.kind !== 'retail_product_ads') return fail();
  const period = RETAIL_COMPARISON_DECODER.window(item.period);
  const prior_period = RETAIL_COMPARISON_DECODER.window(item.prior_period);
  if (!RETAIL_COMPARISON_DECODER.sameWindow(period, parent.period) || !RETAIL_COMPARISON_DECODER.sameWindow(prior_period, parent.prior_period)) return fail();
  const rows = array(item.rows).map((entry): RetailProductAdsRow => {
    const row = record(entry, ['child_asin', 'conversion', 'channels', 'reason_code', 'reason']);
    const conversion = row.conversion === null ? null : RETAIL_COMPARISON_DECODER.comparisonValue(row.conversion, false);
    if (conversion !== null && (conversion.units.kind !== 'ratio' || !RETAIL_COMPARISON_DECODER.sameWindow(conversion.period, period))) return fail();
    const channels = array(row.channels).map((cell) => {
      const value = record(cell, ['channel', 'label', 'purchases', 'revenue', 'revenue_change']);
      const change = parseChange(value.revenue_change);
      if (change === undefined || change === null) return fail();
      const purchases = adValue(value.purchases, period);
      const revenue = adValue(value.revenue, period);
      if (purchases.units.kind !== 'count' || revenue.units.kind !== 'currency') return fail();
      return { channel: text(value.channel), label: text(value.label), purchases, revenue, revenue_change: change };
    });
    const reason_code = nullableText(row.reason_code);
    const reason = nullableText(row.reason);
    if ((reason_code === null) !== (reason === null) || (reason !== null && channels.length > 0) || (reason === null && channels.length === 0)) return fail();
    if (new Set(channels.map((cell) => cell.channel)).size !== channels.length) return fail();
    return { channels, child_asin: text(row.child_asin), conversion, reason, reason_code };
  });
  if (rows.map((row) => row.child_asin).join('\u0000') !== parent.rows.map((row) => row.child_asin).join('\u0000')) return fail();
  return { basis: text(item.basis), kind: 'retail_product_ads', period, prior_period, rows };
}
