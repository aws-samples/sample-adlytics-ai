// SPDX-License-Identifier: MIT-0
/** Retail Health view: insight card placement outside the tile grid, states, and the dialog. */
import assert from 'node:assert/strict';
import { render, screen, within } from '@testing-library/react';
import { test } from 'vitest';

import { CoverageProvider, TileAbsence, TileChange } from '../components/Coverage';
import { VisibleTabsContext } from '../components/NextStepContext';
import { RetailHealthBandTitle, RetailHealthView } from '../components/RetailHealthView';
import { provenanceLine } from '../lib/provenance';
import type { ManifestTab } from '../lib/route-manifest';
import { parseRetailHealthArtifact, type RetailHealthArtifact } from '../lib/retail-health';
import { changeBlock } from './fixtures/change-fixture';
import { completeRetailCoverage } from './fixtures/retail-fixture';
const period = { end: '2026-08-21', start: '2026-08-15' };
const prior = { end: '2026-08-14', start: '2026-08-08' };
const TREND_METRICS = ['retail_revenue_shipped', 'units_shipped', 'customer_returns', 'glance_views'] as const;
const TOTAL_METRICS = [...TREND_METRICS, 'revenue_ordered', 'cogs_shipped', 'conversion_rate_retail', 'return_rate', 'returns_value_estimate_retail', 'fulfilment_rate', 'amazon_margin_proxy'] as const;
const ROW_METRICS = [...TREND_METRICS, 'return_rate', 'amazon_margin_proxy'] as const;
const CURRENCY_METRICS = new Set(['retail_revenue_shipped', 'revenue_ordered', 'cogs_shipped', 'returns_value_estimate_retail', 'amazon_margin_proxy', 'asp_shipped', 'revenue_per_glance_view']);
const RATIO_METRICS = new Set(['conversion_rate_retail', 'return_rate', 'fulfilment_rate']);

function metricValue(metric: string, window: { end: string; start: string }, value: string): Record<string, unknown> {
  const currency = CURRENCY_METRICS.has(metric);
  return { basis: 'retail_actual', grain: ['date', 'marketplace'], period: window, units: { currency_code: currency ? 'USD' : null, kind: currency ? 'currency' : RATIO_METRICS.has(metric) ? 'ratio' : 'count' }, value };
}

/** A displayed comparison value: whole fixture amounts carry the server's two-place display for currency (ADR-084). */
function comparisonValue(metric: string, window: { end: string; start: string }, value: string): Record<string, unknown> {
  return { ...metricValue(metric, window, value), display_value: CURRENCY_METRICS.has(metric) ? `${value}.00` : null };
}
function comparisonFixture(metric: string): Record<string, unknown> {
  return { absolute_change: '2', change: changeBlock('2', '+100.0%', 'favourable'), current: comparisonValue(metric, period, '4'), metric, prior: comparisonValue(metric, prior, '2') };
}

function completeArtifact(revenue?: Record<string, unknown>): RetailHealthArtifact {
  return parseRetailHealthArtifact(completePayload(revenue));
}

function completePayload(revenue?: Record<string, unknown>): Record<string, unknown> {
  return {
    absences: [],
    boundary: 'Retail actuals do not establish advertising attribution.',
    distributor_view: 'MANUFACTURING',
    insights: [
      { artifact_revision: 'revision', evidence_line: 'Shipped revenue rose while glance views were flat.', id: 'demand-shape', output_class: 'perspective', prompt_version: 'v2', state: 'filled', text: 'The gain came from conversion of existing traffic.' },
      { absence_class: 'evaluated-quiet', absence_reason: 'Returns moved with shipped units this window.', artifact_revision: 'revision', evidence_line: null, id: 'returns-pressure', output_class: 'perspective', prompt_version: 'deterministic-gate1', state: 'silent', text: null },
      { absence_class: 'ineligible', absence_reason: 'This insight requires ordered-revenue evidence, which the Sourcing view withholds.', artifact_revision: 'revision', evidence_line: null, id: 'ordered-vs-shipped', output_class: 'perspective', prompt_version: 'deterministic-gate1', state: 'silent', text: null },
      { absence_class: 'generation-pending', absence_reason: 'This insight could not be generated just now; it retries automatically.', artifact_revision: 'revision', evidence_line: null, id: 'retail-movers', output_class: 'observed', prompt_version: 'deterministic-v1', state: 'unavailable', text: null },
    ],
    kind: 'retail_health',
    outcome: 'complete',
    period,
    prior_period: prior,
    reconciliation: { as_of: '2026-08-21', outcome: 'reconciled', product_revenue: null, product_units: null, reported_revenue: null, reported_units: null },
    rows: [{
      child_asin: 'b0fictional',
      comparisons: ROW_METRICS.map((metric) => comparisonFixture(metric)),
      freshness_qualifiers: [],
      parent_asin: 'b0fictionp',
      shipped_revenue_spark: [metricValue('retail_revenue_shipped', { end: '2026-08-15', start: '2026-08-15' }, '1')],
      title: 'Fictional product',
    }],
    total_count: 1,
    totals: FORMAT_8_TOTALS.map((metric) => metric === 'retail_revenue_shipped' && revenue !== undefined ? revenue : comparisonFixture(metric)),
    trend: [{
      current: TREND_METRICS.map((metric) => metricValue(metric, { end: '2026-08-15', start: '2026-08-15' }, '1')),
      date: '2026-08-15',
      prior: TREND_METRICS.map((metric) => metricValue(metric, { end: '2026-08-08', start: '2026-08-08' }, '1')),
    }],
    truncation: null,
    evidence_status: completeRetailCoverage(FORMAT_8_TOTALS, period),
    ...FIXED,
  };
}
test('Retail insight cards render in their own Insights section, never inside a tile grid', () => {
  const view = render(<RetailHealthView artifact={completeArtifact()} />);

  // No insight card shares the tile grid (uneven-card owner feedback, 2026-09-23).
  assert.equal(view.container.querySelectorAll('.tiles .narr-card').length, 0);
  const insights = screen.getByRole('region', { name: 'Insights' });
  assert.equal(insights.querySelectorAll('.narr-card').length, 4);
  // Each state carries its served words and treatment.
  assert.ok(screen.getByText('The gain came from conversion of existing traffic.'));
  assert.ok(screen.getByText('Returns moved with shipped units this window.'));
  assert.ok(screen.getByText(/requires ordered-revenue evidence/));
  assert.ok(view.container.querySelector('.narr-card.quiet'));
  assert.ok(view.container.querySelector('.narr-card.callout'));
  // The movers card leads the section with its pending treatment (mockup order).
  assert.ok(insights.querySelector('.narr-card.pending'));
  assert.equal(insights.querySelector('.narr-card h3')?.textContent, 'Largest movers');
});

test('The distributor view and provenance render in customer words, never as wire values', () => {
  // Page chrome maps server enums to words; the browser probe no longer scans rendered text (ADR-082),
  // so the mapping is asserted here.
  const view = render(<><RetailHealthBandTitle artifact={completeArtifact()} /><RetailHealthView artifact={completeArtifact()} /></>);
  assert.ok(screen.getByText('Manufacturing view'));
  assert.equal(/\bMANUFACTURING\b|\bSOURCING\b/.test(view.container.textContent ?? ''), false);
  for (const stamp of ['deterministic-gate1', 'deterministic-v1', 'not-generated']) {
    assert.notEqual(provenanceLine(stamp), stamp);
  }
});
test('A retail insight card opens the insight dialog with the served reason', async () => {
  const user = (await import('@testing-library/user-event')).default.setup();
  render(<RetailHealthView artifact={completeArtifact()} />);

  await user.click(screen.getByRole('button', { name: 'Open the insight view for returns pressure' }));
  const dialog = screen.getByRole('dialog');
  assert.ok(screen.getByRole('heading', { level: 2, name: 'Returns pressure' }));
  assert.ok(dialog.textContent?.includes('Returns moved with shipped units this window.'));
  assert.ok(dialog.textContent?.includes('Decided by a fixed rule from the values on this page'));
  // The dialog hops across all four page slots, including the table-lead movers card.
  assert.ok(screen.getByText(/of 4$/));
});
test('A retail money tile shows the served two-place display, never the exact four-place value', () => {
  // Shipped revenue of 75459.6400 USD (prior 2.0000 USD) is served with its 75459.64 display.
  const revenue = {
    ...comparisonFixture('retail_revenue_shipped'),
    absolute_change: '75457.6400',
    current: { ...comparisonValue('retail_revenue_shipped', period, '4'), display_value: '75459.64', value: '75459.6400' },
    // A near-zero prior: the server writes the absolute change, never "+3772882.0%" (ADR-121).
    change: { direction: 'up', display_text: '+75,457.64 USD', not_meaningful: 'near_zero_prior', sentiment: 'favourable', unit: 'absolute' },
    prior: { ...comparisonValue('retail_revenue_shipped', prior, '2'), display_value: '2.00', value: '2.0000' },
  };
  const view = render(<RetailHealthView artifact={completeArtifact(revenue)} />);
  assert.ok(screen.getAllByText('75,459.64 USD').length >= 1);
  assert.ok([...view.container.querySelectorAll('.chg.favourable')].some((chip) => chip.textContent?.includes('+75,457.64 USD')));
  assert.ok(screen.getAllByText('Prior: 2.00 USD').length >= 1);
  assert.equal(view.container.textContent?.includes('75,459.6400'), false);
});

/* ---------- Slice 4: a partly reported day (ADR-119, ADR-118 decision 5) ---------- */

const PARTIAL = new Set(['customer_returns', 'return_rate', 'returns_value_estimate_retail']);

/**
 * Fixture: the source sent Aug 21 customer returns for some products only, so the returns
 * family is partly reported and its changes are withheld (unit-proven; no bake produces it).
 */
function partialReturnsPayload(): Record<string, unknown> {
  return withheldPayload(PARTIAL, (metric) => {
    const ratio = metric === 'return_rate';
    return {
      absence_text: '1 partial day.',
      coverage_text: ratio ? 'Aug 15\u201320, 6 of 7 days' : 'Aug 21 counts the products that reported',
      label: metric === 'customer_returns' ? 'Customer returns (units)' : `Fictional ${metric}`,
      missing_dates: ['2026-08-21'],
      missing_text: 'Aug 21 (Fictional pack)',
      reason: ratio ? 'Covers Aug 15\u201320 (6 of 7 days): a partly reported day is left out of both parts.' : 'Shows the products that reported on Aug 21.',
      recovery: 'The figure and its change update by themselves when the rest of the day arrives.',
      role: 'current', role_label: 'Returns', slot: metric, state: 'partial_days',
    };
  });
}

/** Fixture: the source has not sent Aug 21 shipped revenue at all (unit-proven; no bake produces it). */
function missingRevenueDayPayload(): Record<string, unknown> {
  return withheldPayload(new Set(['retail_revenue_shipped']), (metric) => ({
    absence_text: '1 day not yet sent.',
    coverage_text: '6 of 7 days',
    label: 'Shipped revenue',
    missing_dates: ['2026-08-21'],
    missing_text: 'Aug 21',
    reason: 'Covers Aug 15\u201320 (6 of 7 days).',
    recovery: 'The figure and its change update by themselves when the day arrives.',
    role: 'current', role_label: 'Sales', slot: metric, state: 'missing_days',
  }));
}

/** The complete payload with `withheld` figures' changes withheld and named in the coverage block. */
function withheldPayload(withheld: ReadonlySet<string>, entryFor: (metric: string) => Record<string, unknown>): Record<string, unknown> {
  const base = completePayload();
  const hold = (entry: Record<string, unknown>) => (withheld.has(String(entry.metric))
    ? { ...entry, absolute_change: null, change: null, percentage_change: null }
    : entry);
  return {
    ...base,
    evidence_status: {
      entries: FORMAT_8_TOTALS.map((metric) => (withheld.has(metric)
        ? entryFor(metric)
        : { absence_text: null, coverage_text: null, label: `Fictional ${metric}`, missing_dates: [], missing_text: 'None', reason: 'Every day of this window has arrived.', recovery: 'No action needed.', role: 'current', role_label: 'Headline', slot: metric, state: 'complete' })),
      summary: `Fictional: ${withheld.size} of the figures are partly reported for this window. They show what arrived, without a change, until the rest arrives. Every other figure is complete.`,
      version: 2,
      window: period,
    },
    rows: (base.rows as Record<string, unknown>[]).map((row) => ({ ...row, comparisons: (row.comparisons as Record<string, unknown>[]).map(hold) })),
    totals: (base.totals as Record<string, unknown>[]).map(hold),
  };
}

test('A partial day reads "1 partial day." in the change place, names what the count covers and opens the sheet', async () => {
  const user = (await import('@testing-library/user-event')).default.setup();
  const view = render(<RetailHealthView artifact={parseRetailHealthArtifact(partialReturnsPayload())} />);
  const tile = (name: string) => [...view.container.querySelectorAll('.tiles .tile')].find((item) => item.querySelector('.k')?.textContent === name)!;
  const returns = tile('Customer returns');
  assert.ok(returns.textContent?.includes('1 partial day.'));
  assert.equal(returns.querySelector('.basis')?.textContent, 'Aug 21 counts the products that reported');
  assert.equal(returns.querySelector('.chg'), null);
  const rate = tile('Return rate');
  // The definition, then the covered days, on one basis line (as the mockups write it).
  assert.equal(rate.querySelector('.basis')?.textContent, 'Customer returns \u00f7 shipped units \u00b7 Aug 15\u201320, 6 of 7 days');
  assert.ok(tile('Shipped revenue').querySelector('.chg'));
  await user.click(screen.getByRole('button', { name: 'See why: Customer returns' }));
  const dialog = screen.getByRole('dialog', { name: 'Coverage for this window' });
  assert.ok(within(dialog).getByRole('rowheader', { name: 'Customer returns (units)' }));
  assert.ok(dialog.textContent?.includes('Aug 21 (Fictional pack)'));
  assert.ok(dialog.textContent?.includes('Retail health'));
});

/** True when `first` comes before `second` in document order. */
function precedes(first: Element | null, second: Element | null): boolean {
  assert.ok(first !== null && second !== null);
  return (first.compareDocumentPosition(second) & Node.DOCUMENT_POSITION_FOLLOWING) !== 0;
}

test('A tile reads its value, prior, basis and then the absence line, as the mockup does', () => {
  const view = render(<RetailHealthView artifact={parseRetailHealthArtifact(partialReturnsPayload())} />);
  const tile = (name: string) => [...view.container.querySelectorAll('.tiles .tile')].find((item) => item.querySelector('.k')?.textContent === name)!;
  for (const name of ['Customer returns', 'Return rate', 'Estimated revenue withheld by returns']) {
    const absence = tile(name).querySelector('.tile-absence');
    assert.ok(absence?.textContent?.startsWith('1 partial day.'));
    assert.ok(precedes(tile(name).querySelector('.basis'), absence));
    // The withheld line sits outside the chip slot, so nothing in the value row moves.
    assert.equal(tile(name).querySelector('.row .chg-absent'), null);
  }
  // The prior reads before the basis line, as the mockup does.
  const rate = tile('Return rate');
  assert.ok(precedes([...rate.querySelectorAll('p.prior')].at(-1) ?? null, rate.querySelector('.basis')));
});

test('A retained or one-sided tile keeps "Change unavailable" in its chip slot', () => {
  const view = render(<CoverageProvider status={null} subject="Retail health">
    <div className="tile"><div className="row"><div /><TileChange change={null} slot="customer_returns" /></div>
      <TileAbsence slot="customer_returns" /></div>
  </CoverageProvider>);
  assert.equal(view.container.querySelector('.row > .chg-absent')?.textContent, 'Change unavailable');
  assert.equal(view.container.querySelector('.tile-absence'), null);
});

test('A missing day names the covered days on the product table and withholds the row change', () => {
  render(<RetailHealthView artifact={parseRetailHealthArtifact(missingRevenueDayPayload())} />);
  const table = screen.getByRole('table', { name: /Shipped revenue per product/ });
  const header = (name: string) => within(table).getAllByRole('columnheader').find((th) => th.firstChild?.textContent === name)!;
  assert.equal(header('Shipped revenue').querySelector('.basis')?.textContent, '6 of 7 days');
  assert.equal(header('Shipped units').querySelector('.basis'), null);
  const revenue = table.querySelector('td[data-label="Shipped revenue"]')!;
  assert.equal(revenue.querySelector('.basis-inline')?.textContent, '6 of 7 days');
  const change = table.querySelector('td[data-label="Change"]')!;
  assert.ok(change.textContent?.includes('1 day not yet sent.'));
  assert.equal(change.querySelector('.chg'), null);
  assert.ok(within(table).getByRole('button', { name: 'See why: Shipped revenue, Fictional product' }));
  assert.equal(table.querySelector('td[data-label="Shipped units"] .basis-inline'), null);
});

test('A partial day says what the product rows count on the header and on each stacked cell', () => {
  render(<RetailHealthView artifact={parseRetailHealthArtifact(partialReturnsPayload())} />);
  const table = screen.getByRole('table', { name: /Shipped revenue per product/ });
  const header = (name: string) => within(table).getAllByRole('columnheader').find((th) => th.firstChild?.textContent === name)!;
  assert.equal(header('Return rate').querySelector('.basis')?.textContent, 'Aug 15\u201320, 6 of 7 days');
  assert.equal(table.querySelector('td[data-label="Return rate"] .basis-inline')?.textContent, 'Aug 15\u201320, 6 of 7 days');
  // A complete column carries no coverage text.
  assert.equal(table.querySelector('td[data-label="Shipped revenue"] .basis-inline'), null);
});

test('The Retail health decoder holds withheld changes to the coverage block and refuses a body without it', () => {
  // Paired positive control: a complete current body carries its block.
  assert.notEqual(parseRetailHealthArtifact(completePayload()).evidence_status, null);
  const decoded = parseRetailHealthArtifact(partialReturnsPayload());
  assert.equal(decoded.totals.find((entry) => entry.metric === 'customer_returns')?.withheld, true);
  assert.equal(decoded.rows[0]?.comparisons.find((entry) => entry.metric === 'return_rate')?.withheld, true);
  const { evidence_status: _omitted, ...withoutBlock } = partialReturnsPayload();
  void _omitted;
  const block = partialReturnsPayload().evidence_status as Record<string, unknown>;
  for (const forged of [
    // The retired format-5 form (no key) and a complete body with a null block.
    withoutBlock,
    { ...completePayload(), evidence_status: null },
    { ...completePayload(), evidence_status: block },
    { ...partialReturnsPayload(), evidence_status: { ...block, entries: (block.entries as unknown[]).slice(1) } },
  ]) {
    assert.throws(() => parseRetailHealthArtifact(forged), TypeError);
  }
});
test('A retail card\'s metric step opens that tile\'s focus dialog; a campaign step renders nothing here (ADR-122)', async () => {
  const user = (await import('@testing-library/user-event')).default.setup();
  const stepped = (nextStep: Record<string, unknown>) => {
    const payload = completePayload();
    const insights = (payload.insights as Record<string, unknown>[]).map((slot) => ({ ...slot }));
    insights[0] = { ...insights[0], next_step: nextStep };
    return parseRetailHealthArtifact({ ...payload, insights });
  };
  const view = render(<RetailHealthView artifact={stepped({
    kind: 'open_metric_focus',
    label: 'Open glance views by day',
    reason: 'Traffic stayed flat while revenue rose.',
    target: { metric: 'glance_views', page_id: 'retail-health' },
  })} />);
  const chip = screen.getByRole('button', { name: 'Open glance views by day' });
  assert.equal(document.getElementById(chip.getAttribute('aria-describedby') ?? '')?.textContent, 'Traffic stayed flat while revenue rose.');
  await user.click(chip);
  assert.ok(screen.getByRole('dialog', { name: 'Product page views' }));
  await user.keyboard('{Escape}');
  view.unmount();
  const campaign = render(<RetailHealthView artifact={stepped({
    kind: 'open_campaign_detail',
    label: 'Open Fictional campaign',
    reason: 'A fictional reason.',
    target: { campaign_ref: 'a'.repeat(43), page_id: 'retail-health' },
  })} />);
  assert.equal(campaign.container.querySelector('.next-step'), null);
});


/* ---------- Slice 7: four tile groups, the product table and product detail (ADR-125) ---------- */

const FORMAT_8_TOTALS = [...TOTAL_METRICS, 'units_ordered', 'asp_shipped', 'revenue_per_glance_view'] as const;
const FIXED = {
  amc_retail_purchases: { status: 'paid_feature', text: 'Retail purchases from Amazon Marketing Cloud need the paid feature. [fictional]' },
  return_reasons: { reason: "Vendor Central doesn't send return reasons for this account." },
};

function format8Value(metric: string, window: { end: string; start: string }, value: string): Record<string, unknown> {
  const currency = CURRENCY_METRICS.has(metric) || metric === 'asp_shipped' || metric === 'revenue_per_glance_view';
  return { basis: 'retail_actual', display_value: currency ? `${value}.00` : null, grain: ['period', 'marketplace'], period: window, units: { currency_code: currency ? 'USD' : null, kind: currency ? 'currency' : RATIO_METRICS.has(metric) ? 'ratio' : 'count' }, value };
}

function format8Payload(): Record<string, unknown> {
  const base = completePayload();
  return {
    ...base,
    ...FIXED,
    rows: (base.rows as Record<string, unknown>[]).map((row) => ({ ...row, selection_reference: 'v2_' + 'A'.repeat(40) })),
    totals: FORMAT_8_TOTALS.map((metric) => ({ absolute_change: '2', change: changeBlock('2', '+100.0%', 'favourable'), current: format8Value(metric, period, '4'), metric, prior: format8Value(metric, prior, '2') })),
    evidence_status: completeRetailCoverage(FORMAT_8_TOTALS, period),
  };
}

test('Retail health shows the four shared tile groups, the return-reasons absence and the paid feature', () => {
  const artifact = parseRetailHealthArtifact(format8Payload());
  render(<RetailHealthView artifact={artifact} />);
  const headings = screen.getAllByRole('heading', { level: 2 }).map((heading) => heading.textContent);
  assert.deepEqual(headings.slice(0, 4), ['Demand and conversion', 'Sales and fulfilment', 'Traffic value and margin', 'Returns']);
  const group = (name: string) => screen.getByRole('region', { name });
  const keys = (name: string) => [...group(name).querySelectorAll('.tile .k')].map((key) => key.textContent);
  assert.deepEqual(keys('Demand and conversion'), ['Product page views', 'Units per 1,000 product page views', 'Ordered revenue', 'Ordered units']);
  assert.deepEqual(keys('Sales and fulfilment'), ['Shipped revenue', 'Shipped units', 'Fulfilment rate', 'Average selling price']);
  assert.deepEqual(keys('Traffic value and margin'), ['Revenue per glance view', 'Cost of goods shipped', "Amazon's margin on your products (not yours)"]);
  assert.deepEqual(keys('Returns'), ['Customer returns', 'Return rate', 'Estimated revenue withheld by returns', 'Return reasons']);
  assert.ok(group('Returns').textContent?.includes("Vendor Central doesn't send return reasons for this account."));
  const paid = group('Retail purchases from Amazon Marketing Cloud');
  assert.ok(paid.textContent?.includes('Available with the paid feature'));
  assert.ok(paid.textContent?.includes('need the paid feature. [fictional]'));
  // A rate per 1,000 glance views is a decimal-point shift of the served ratio, never arithmetic.
  assert.ok(group('Demand and conversion').textContent?.includes('4,000'));
  // Repeat purchase by product isn't in this session's manifest yet, so no dead link renders.
  assert.equal(screen.queryByRole('link', { name: 'Repeat purchase by product' }), null);
});

const repeatPurchaseTab = (tabId: string): ManifestTab => ({ description: 'Repeat purchases per product', entitlement: null, kind: 'page', label: 'Repeat purchase by product', order: 1, route: '/repeat-purchase/', tab_id: tabId });
test('The Repeat purchase pointer renders from the manifest tab repeat-purchase-by-product, and the old tab id renders none', () => {
  const artifact = parseRetailHealthArtifact(format8Payload());
  const shown = render(<VisibleTabsContext.Provider value={[repeatPurchaseTab('repeat-purchase-by-product')]}><RetailHealthView artifact={artifact} /></VisibleTabsContext.Provider>);
  assert.equal(screen.getByRole('link', { name: 'Repeat purchase by product' }).getAttribute('href'), '/repeat-purchase/');
  shown.unmount();
  // Paired control: a manifest listing only the retired id leaves the pointer unrendered.
  render(<VisibleTabsContext.Provider value={[repeatPurchaseTab('repeat-purchase')]}><RetailHealthView artifact={artifact} /></VisibleTabsContext.Provider>);
  assert.equal(screen.queryByRole('link', { name: 'Repeat purchase by product' }), null);
});
test('The product table has six columns and a row opens that product in place', async () => {
  const user = (await import('@testing-library/user-event')).default.setup();
  const opened: string[] = [];
  render(<RetailHealthView artifact={parseRetailHealthArtifact(format8Payload())} onOpenProduct={(row) => opened.push(row.child_asin)} />);
  const table = screen.getByRole('table', { name: /Shipped revenue per product/ });
  assert.deepEqual(within(table).getAllByRole('columnheader').map((th) => th.firstChild?.textContent), ['Product', 'Shipped revenue', 'Change', 'Shipped units', 'Return rate', 'Status']);
  await user.click(within(table).getByRole('button', { name: /Fictional product/ }));
  assert.deepEqual(opened, ['b0fictional']);
});

test('Rows served with no product capability show as text, beside the paid-feature section', () => {
  render(<RetailHealthView artifact={completeArtifact()} onOpenProduct={() => assert.fail('a row without a capability must not open')} />);
  const table = screen.getByRole('table', { name: /Shipped revenue per product/ });
  assert.equal(within(table).queryByRole('button'), null);
  assert.ok(screen.queryByRole('region', { name: 'Retail purchases from Amazon Marketing Cloud' }) !== null);
  assert.ok(screen.getByText('Product detail opens once this window is baked again.'));
});

test('The browser decoder refuses a format-8 body that drops a new tile, and the retired format-7 body', () => {
  const payload = format8Payload();
  // Paired positive control: the format-8 body decodes with both fixed statuses.
  assert.equal(parseRetailHealthArtifact(payload).fixed_statuses.return_reasons.reason, "Vendor Central doesn't send return reasons for this account.");
  assert.throws(() => parseRetailHealthArtifact({ ...payload, totals: (payload.totals as unknown[]).slice(0, -1) }), TypeError);
  // The format-7 form (owner revision, 2026-10-08): the first eleven tiles and no fixed statuses.
  const { amc_retail_purchases: _amc, return_reasons: _reasons, ...format7 } = payload;
  void _amc; void _reasons;
  const eleven = TOTAL_METRICS as readonly string[];
  const retired = { ...format7, totals: (payload.totals as { metric: string }[]).filter((entry) => eleven.includes(entry.metric)), evidence_status: completeRetailCoverage(eleven, period) };
  assert.throws(() => parseRetailHealthArtifact(retired), { message: 'Retail Health artifact is malformed.' });
});

test('A window change that closed the open product says which product and the window', () => {
  render(<RetailHealthView artifact={parseRetailHealthArtifact(format8Payload())} closedProduct={{ title: 'Fictional bottle' }} />);
  assert.ok(screen.getByText("Fictional bottle isn't in the product list for Aug 15\u201321, 2026."));
});
