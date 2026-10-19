// SPDX-License-Identifier: MIT-0
import assert from 'node:assert/strict';
import { test } from 'vitest';
import { parseRetailHealthArtifact } from '../lib/retail-health';
import { changeBlock } from './fixtures/change-fixture';
import { completeRetailCoverage } from './fixtures/retail-fixture';
const period = { start: '2026-08-15', end: '2026-08-21' };
const prior = { start: '2026-08-08', end: '2026-08-14' };
/** The two server-written statuses every body carries from render format 8 (fictional wording). */
const FIXED_STATUSES = {
  amc_retail_purchases: { status: 'paid_feature', text: 'Fictional paid-feature status.' },
  return_reasons: { reason: 'Fictional return-reasons absence.' },
} as const;
function artifact(): Record<string, unknown> {
  return { kind: 'retail_health', outcome: 'empty', period, prior_period: prior, distributor_view: 'MANUFACTURING', totals: [], absences: [], insights: [['demand-shape', 'perspective', 'not-generated'], ['returns-pressure', 'perspective', 'not-generated'], ['ordered-vs-shipped', 'perspective', 'not-generated'], ['retail-movers', 'observed', 'deterministic-v1']].map(([id, output_class, prompt_version]) => ({ id, output_class, prompt_version, artifact_revision: 'unpublished', state: 'unavailable', text: null, evidence_line: null })), trend: [], rows: [], total_count: 0, truncation: null, reconciliation: null, boundary: 'Retail actuals do not establish advertising attribution.', evidence_status: null, ...FIXED_STATUSES };
}
test('Retail Health decoder accepts a typed closed state and rejects altered fields', () => {
  assert.equal(parseRetailHealthArtifact(artifact()).outcome, 'empty');
  const hostile = structuredClone(artifact());
  hostile.unexpected = true;
  assert.throws(() => parseRetailHealthArtifact(hostile));
});
test('Retail Health decoder rejects a nonpreceding comparison window', () => {
  const hostile = structuredClone(artifact());
  hostile.prior_period = { start: '2026-08-07', end: '2026-08-13' };
  assert.throws(() => parseRetailHealthArtifact(hostile));
});
test('Retail Health decoder accepts exactly the base and absence-declared slot shapes', () => {
  // A slot with no declared absence carries the seven base fields only.
  const undeclared = parseRetailHealthArtifact(artifact());
  assert.equal(undeclared.insights[0]?.absence_class, null);
  assert.equal(undeclared.insights[0]?.absence_reason, null);
  const declared = artifact();
  for (const slot of declared.insights as Record<string, unknown>[]) { slot.absence_class = 'generation-pending'; slot.absence_reason = 'This insight could not be generated just now.'; }
  const decoded = parseRetailHealthArtifact(declared);
  assert.equal(decoded.insights[0]?.absence_class, 'generation-pending');
  assert.equal(decoded.insights[0]?.absence_reason, 'This insight could not be generated just now.');
  const hostilePayloads: ReadonlyArray<(slot: Record<string, unknown>) => void> = [
    // A class without its sibling reason key is neither legal shape.
    (slot) => { slot.absence_class = 'generation-pending'; },
    (slot) => { slot.absence_class = 'mystery-class'; slot.absence_reason = null; },
    // A reason requires its class.
    (slot) => { slot.absence_class = null; slot.absence_reason = 'A reason without a class.'; },
  ];
  for (const mutate of hostilePayloads) {
    const hostile = structuredClone(artifact());
    mutate((hostile.insights as Record<string, unknown>[])[0]!);
    assert.throws(() => parseRetailHealthArtifact(hostile));
  }
});

const TREND_METRICS = ['retail_revenue_shipped', 'units_shipped', 'customer_returns', 'glance_views'] as const;
const TOTAL_METRICS = [...TREND_METRICS, 'revenue_ordered', 'cogs_shipped', 'conversion_rate_retail', 'return_rate', 'returns_value_estimate_retail', 'fulfilment_rate', 'amazon_margin_proxy', 'units_ordered', 'asp_shipped', 'revenue_per_glance_view'] as const;
const ROW_METRICS = [...TREND_METRICS, 'return_rate', 'amazon_margin_proxy'] as const;
const CURRENCY_METRICS = new Set(['retail_revenue_shipped', 'revenue_ordered', 'cogs_shipped', 'returns_value_estimate_retail', 'amazon_margin_proxy', 'asp_shipped', 'revenue_per_glance_view']);
const RATIO_METRICS = new Set(['conversion_rate_retail', 'return_rate', 'fulfilment_rate']);
function metricValue(metric: string, window: { start: string; end: string }, value: string): Record<string, unknown> {
  const currency = CURRENCY_METRICS.has(metric);
  return { basis: 'retail_actual', grain: ['date', 'marketplace'], period: window, units: { kind: currency ? 'currency' : RATIO_METRICS.has(metric) ? 'ratio' : 'count', currency_code: currency ? 'USD' : null }, value };
}
/** A displayed comparison value: whole fixture amounts carry the server's two-place display for currency (ADR-084). */
function comparisonValue(metric: string, window: { start: string; end: string }, value: string): Record<string, unknown> {
  return { ...metricValue(metric, window, value), display_value: CURRENCY_METRICS.has(metric) ? `${value}.00` : null };
}
function comparisonFixture(metric: string): Record<string, unknown> {
  return { metric, current: comparisonValue(metric, period, '4'), prior: comparisonValue(metric, prior, '2'), absolute_change: '2', change: changeBlock('2', '+100.0%') };
}
function completeArtifact(): Record<string, unknown> {
  const base = artifact();
  return {
    ...base,
    outcome: 'complete',
    totals: TOTAL_METRICS.map((metric) => comparisonFixture(metric)),
    trend: [{
      date: '2026-08-15',
      current: TREND_METRICS.map((metric) => metricValue(metric, { start: '2026-08-15', end: '2026-08-15' }, '1')),
      prior: TREND_METRICS.map((metric) => metricValue(metric, { start: '2026-08-08', end: '2026-08-08' }, '1')),
    }],
    rows: [{
      child_asin: 'b0fictional', parent_asin: 'b0fictionp', title: 'Fictional product',
      comparisons: ROW_METRICS.map((metric) => comparisonFixture(metric)),
      shipped_revenue_spark: [metricValue('retail_revenue_shipped', { start: '2026-08-15', end: '2026-08-15' }, '1')],
      freshness_qualifiers: [],
    }],
    total_count: 1,
    reconciliation: { outcome: 'reconciled', as_of: '2026-08-21', product_revenue: null, reported_revenue: null, product_units: null, reported_units: null },
    evidence_status: completeRetailCoverage(TOTAL_METRICS, period),
  };
}
test('Retail Health decoder accepts a signed Amazon margin and rejects a signed rate', () => {
  const signed = structuredClone(completeArtifact()) as { totals: { metric: string; current: { value: string }; prior: { value: string }; absolute_change: string; change: unknown }[] };
  const margin = signed.totals.find((entry) => entry.metric === 'amazon_margin_proxy')!;
  margin.current.value = '-3';
  margin.absolute_change = '-5';
  margin.change = changeBlock('-5', '-250.0%');
  assert.equal(parseRetailHealthArtifact(signed as unknown as Record<string, unknown>).outcome, 'complete');
  const hostile = structuredClone(completeArtifact()) as typeof signed;
  const rate = hostile.totals.find((entry) => entry.metric === 'return_rate')!;
  rate.current.value = '-0.5';
  assert.throws(() => parseRetailHealthArtifact(hostile as unknown as Record<string, unknown>));
});
test('Retail Health decoder requires a typed absence to replace its compare slot exactly', () => {
  // A SOURCING view must state its withheld figures as typed absences, never zeros.
  const sourcing = structuredClone(completeArtifact()) as Record<string, unknown> & { totals: { metric: string }[] };
  sourcing.distributor_view = 'SOURCING';
  const withheld = ['revenue_ordered', 'conversion_rate_retail', 'fulfilment_rate', 'units_ordered', 'revenue_per_glance_view'];
  sourcing.totals = sourcing.totals.filter((entry) => !withheld.includes(entry.metric));
  sourcing.absences = withheld.map((metric) => ({ metric, reason: 'The source withholds this figure under the Sourcing distributor view.' }));
  // The coverage block lists the served tiles only (ADR-119).
  sourcing.evidence_status = completeRetailCoverage(sourcing.totals.map((entry) => entry.metric), period);
  assert.equal(parseRetailHealthArtifact(sourcing).outcome, 'complete');
  const hostile = structuredClone(sourcing);
  hostile.absences = [];
  assert.throws(() => parseRetailHealthArtifact(hostile));
});
test('Retail Health decoder accepts a complete artifact whose trend rows carry per-day windows', () => {
  // The deployed 2026-09-16 defect: the decoder demanded full-window trend value periods
  // while the governed artifact carries dated daily rows, refusing every complete page.
  const decoded = parseRetailHealthArtifact(completeArtifact());
  assert.equal(decoded.outcome, 'complete');
  assert.equal(decoded.trend[0]?.current[0]?.period.start, '2026-08-15');
});
test('Retail Health decoder rejects a trend row whose prior day is not aligned to the prior window', () => {
  const hostile = structuredClone(completeArtifact()) as { trend: { prior: { period: { start: string; end: string } }[] }[] };
  hostile.trend[0]!.prior = hostile.trend[0]!.prior.map((value) => ({ ...value, period: { start: '2026-08-09', end: '2026-08-09' } }));
  assert.throws(() => parseRetailHealthArtifact(hostile as unknown as Record<string, unknown>));
});
type DisplayFixture = { totals: { metric: string; current: Record<string, unknown> }[]; trend: { current: Record<string, unknown>[] }[]; rows: { shipped_revenue_spark: Record<string, unknown>[] }[] };
test('Retail Health comparisons carry the served display value; points and spark entries do not', () => {
  const decoded = parseRetailHealthArtifact(completeArtifact());
  // Shipped revenue of 4 USD displays as 4.00; shipped units (a count of 4) carry null.
  assert.equal(decoded.totals[0]?.current.display_value, '4.00');
  assert.equal(decoded.totals[1]?.current.display_value, null);
  assert.equal('display_value' in (decoded.trend[0]?.current[0] ?? {}), false);
});
test('Retail Health decoder refuses a missing or count comparison display value and a display on a point', () => {
  const hostilePayloads: ReadonlyArray<(value: DisplayFixture) => void> = [
    (value) => { delete value.totals[0]!.current.display_value; },
    (value) => { value.totals[0]!.current.display_value = '4.000'; },
    (value) => { value.totals[1]!.current.display_value = '4.00'; },
    (value) => { value.trend[0]!.current[0]!.display_value = '1.00'; },
    (value) => { value.rows[0]!.shipped_revenue_spark[0]!.display_value = '1.00'; },
  ];
  for (const mutate of hostilePayloads) {
    const hostile = structuredClone(completeArtifact()) as unknown as DisplayFixture;
    mutate(hostile);
    assert.throws(() => parseRetailHealthArtifact(hostile as unknown as Record<string, unknown>));
  }
});

test('FR-11: a filled slot carries its served step; a slot that is not filled refuses one', () => {
  const step = { kind: 'ask_assistant', label: 'Ask the assistant', reason: 'It can compare windows.', target: { question: 'Why did returns rise from Aug 3–9 to Aug 10–16?' } };
  const served = artifact();
  const slots = served.insights as Record<string, unknown>[];
  Object.assign(slots[0] as Record<string, unknown>, { state: 'filled', text: 'A fictional reading.', evidence_line: 'Read from the values this page already shows.', next_step: step });
  assert.deepEqual(parseRetailHealthArtifact(served).insights[0]?.next_step, step);
  assert.equal(parseRetailHealthArtifact(artifact()).insights[0]?.next_step, null);
  const quiet = artifact();
  Object.assign((quiet.insights as Record<string, unknown>[])[0] as Record<string, unknown>, { next_step: step });
  assert.throws(() => parseRetailHealthArtifact(quiet));
});

type ComparisonFixture = Record<string, unknown> & { absolute_change: string; percentage_change?: string | null; change?: unknown };
function comparisonsOf(value: Record<string, unknown>): ComparisonFixture[] {
  const rows = value.rows as { comparisons: ComparisonFixture[] }[];
  return [...(value.totals as ComparisonFixture[]), ...rows.flatMap((row) => row.comparisons)];
}
/** The fixture as served: each comparison carries its block in place of the percentage. */
function currentArtifact(): Record<string, unknown> {
  return structuredClone(completeArtifact());
}
test('Retail Health decoder refuses a format-4 body with no change keys and reads the current one', () => {
  // Paired positive control: the current body decodes with a block on every comparison.
  assert.ok(parseRetailHealthArtifact(currentArtifact()).totals.every((entry) => entry.change !== null));
  // The retired form: the percentage and no change key, on every comparison.
  const retained = currentArtifact();
  for (const entry of comparisonsOf(retained)) {
    entry.percentage_change = (entry.change as { display_text: string }).display_text;
    delete entry.change;
  }
  assert.throws(() => parseRetailHealthArtifact(retained), { message: 'Retail Health artifact is malformed.' });
});
test('Retail Health decoder refuses a format-5 body without the coverage key, and a complete body with a null block', () => {
  // Paired positive control: the current body carries its block; an empty one carries null.
  assert.notEqual(parseRetailHealthArtifact(currentArtifact()).evidence_status, null);
  assert.equal(parseRetailHealthArtifact(artifact()).evidence_status, null);
  const blockless = currentArtifact();
  delete blockless.evidence_status;
  assert.throws(() => parseRetailHealthArtifact(blockless), { message: 'Retail Health artifact is malformed.' });
  assert.throws(() => parseRetailHealthArtifact({ ...currentArtifact(), evidence_status: null }), { message: 'Retail Health artifact is malformed.' });
});
test('Retail Health decoder reads a current body\'s blocks and refuses a null block on a comparison', () => {
  const decoded = parseRetailHealthArtifact(currentArtifact());
  assert.deepEqual(decoded.totals[0]?.change, { direction: 'up', display_text: '+100.0%', not_meaningful: null, sentiment: 'neutral', unit: 'percent' });
  assert.ok(decoded.rows[0]?.comparisons.every((entry) => entry.change !== null));
  // Both retail sides always exist, so `change: null` beside a percentage is a writer bug.
  const hostile = currentArtifact();
  const first = comparisonsOf(hostile)[0]!;
  first.change = null;
  first.percentage_change = '+100.0%';
  assert.throws(() => parseRetailHealthArtifact(hostile));
});

test('Retail Health decoder refuses a format-7 body without the fixed statuses and reads the format-8 one', () => {
  // Paired positive control: the format-8 body carries both server-written statuses.
  assert.equal(parseRetailHealthArtifact(currentArtifact()).fixed_statuses.return_reasons.reason, 'Fictional return-reasons absence.');
  // The retired format-7 form: the eleven-tile roster and neither fixed status.
  const format7 = currentArtifact();
  delete format7.return_reasons;
  delete format7.amc_retail_purchases;
  const eleven = TOTAL_METRICS.slice(0, 11) as readonly string[];
  format7.totals = (format7.totals as { metric: string }[]).filter((entry) => eleven.includes(entry.metric));
  format7.evidence_status = completeRetailCoverage(eleven, period);
  assert.throws(() => parseRetailHealthArtifact(format7), { message: 'Retail Health artifact is malformed.' });
  for (const key of ['return_reasons', 'amc_retail_purchases']) {
    const missing = currentArtifact();
    delete missing[key];
    assert.throws(() => parseRetailHealthArtifact(missing), { message: 'Retail Health artifact is malformed.' });
  }
});
