// SPDX-License-Identifier: MIT-0
import assert from 'node:assert/strict';
import { render, screen, waitFor, within } from '@testing-library/react';
import { test } from 'vitest';

import { CampaignPortfolioV2View, DetailBandTitle } from '../components/CampaignPortfolioV2View';
import { PageAbsence } from '@/components/PageAbsence';
import { campaignRouteAdapter } from '../components/CampaignRouteAdapter';
import {
  campaignDeliverySlots,
  parseCampaignPortfolioArtifact,
  type CampaignAttributionDetailV2,
  type CampaignPortfolioArtifact,
} from '../lib/campaign-attribution';
import type { EvidenceStatusV2 } from '../lib/evidence-status';
import { changeBlock } from './fixtures/change-fixture';
import { asServedPortfolio } from './fixtures/portfolio-fixture';

const period = { end: '2026-08-21', start: '2026-08-15' };
const prior = { end: '2026-08-14', start: '2026-08-08' };
const days = [
  '2026-08-15', '2026-08-16', '2026-08-17', '2026-08-18',
  '2026-08-19', '2026-08-20', '2026-08-21',
];

function units(kind: 'count' | 'currency' | 'ratio') {
  return { currency_code: kind === 'currency' ? 'USD' : null, kind };
}
/**
 * The server's two-place display a fixture amount carries for currency (ADR-084), null
 * otherwise. Fixture amounts are whole or already at four places with trailing zeros, so
 * this only rewrites the scale; it never stands in for the server's rounding.
 */
function displayValue(value: number | string, kind: 'count' | 'currency' | 'ratio'): string | null {
  if (kind !== 'currency') return null;
  const [whole = '', fraction = ''] = String(value).split('.');
  if (/[1-9]/.test(fraction.slice(2))) throw new Error(`fixture amount ${String(value)} needs rounding`);
  return `${whole}.${fraction.padEnd(2, '0').slice(0, 2)}`;
}

const evidenceStatusRoster: readonly (readonly [string, string])[] = [
  ['purchases', 'current'], ['attributed_roas', 'current'], ['spend', 'current'],
  ['purchase_series', 'daily'],
  ['prior_purchases', 'prior'], ['prior_attributed_roas', 'prior'], ['prior_spend', 'prior'],
  ...campaignDeliverySlots.flatMap((slot) =>
    (['current', 'prior', 'daily', 'prior_daily'] as const).map((role) => [slot, role] as const),
  ),
];

function acceptedEvidenceStatuses(): Record<string, unknown>[] {
  return evidenceStatusRoster.map(([slot, role]) => ({
    condition: 'available',
    days_expected: null,
    days_present: null,
    explanation: `Complete: fictional ${slot} is fully served for ${role}.`,
    recovery_reading: null,
    role,
    slot,
  }));
}

function refusedEvidenceStatuses(): Record<string, unknown>[] {
  return evidenceStatusRoster.map(([slot, role]) => {
    if (slot === 'impressions' && role === 'daily') {
      return {
        condition: 'incomplete_daily_series',
        days_expected: 7,
        days_present: 3,
        explanation: '3 of 7 days have an impressions row in this window.',
        recovery_reading: 'This fills when the source delivers the missing days.',
        role,
        slot,
      };
    }
    return {
      condition: 'available',
      days_expected: null,
      days_present: null,
      explanation: `Complete: fictional ${slot} is fully served for ${role}.`,
      recovery_reading: null,
      role,
      slot,
    };
  });
}

/**
 * Campaign detail's coverage block as the server writes it from these statuses (fictional
 * words): a refused slot is unavailable, with its served reason, recovery and missing dates.
 */
function detailCoverage(statuses: readonly Record<string, unknown>[]): EvidenceStatusV2 {
  return {
    entries: statuses.map((status) => {
      const refused = status.condition !== 'available';
      return {
        absence_text: null,
        coverage_text: null,
        label: `Fictional ${String(status.slot)}`,
        missing_dates: refused ? ['2026-08-19', '2026-08-20', '2026-08-21'] : [],
        missing_text: refused ? 'Aug 19\u201321' : 'None',
        reason: refused ? String(status.explanation) : 'Every day of this window has arrived.',
        recovery: refused ? String(status.recovery_reading) : 'No action needed.',
        role: String(status.role),
        role_label: `Fictional role ${String(status.role)}`,
        slot: String(status.slot),
        state: refused ? 'unavailable' : 'complete',
      };
    }),
    summary: statuses.some((status) => status.condition !== 'available')
      ? "Fictional impressions can't be shown for this window; each row says why. Every other figure is complete."
      : 'Every figure is complete for this window.',
    version: 2,
    window: period,
  };
}
function comparison(
  metric: string,
  currentValue: number | string,
  priorValue: number | string,
  absolute_change: number | string,
  percentage_change: string | null,
  grain: readonly string[],
  kind: 'count' | 'currency' | 'ratio',
) {
  return {
    absolute_change,
    change: changeBlock(absolute_change, percentage_change),
    current: { basis: 'ad_attributed', display_value: displayValue(currentValue, kind), grain, period, units: units(kind), value: currentValue },
    metric,
    prior: { basis: 'ad_attributed', display_value: displayValue(priorValue, kind), grain, period: prior, units: units(kind), value: priorValue },
  };
}

function unavailableInsights(): Record<string, unknown>[] {
  return [
    ['contribution', 'perspective', 'not-generated'],
    ['divergence', 'perspective', 'not-generated'],
    ['exposure', 'perspective', 'not-generated'],
    ['movers', 'observed', 'deterministic-v1'],
  ].map(([id, output_class, prompt_version]) => ({
    artifact_revision: 'unpublished',
    evidence_line: null,
    id,
    output_class,
    prompt_version,
    state: 'unavailable',
    text: null,
  }));
}

function closedArtifact(outcome: 'empty' | 'insufficient'): CampaignPortfolioArtifact {
  return parseCampaignPortfolioArtifact(asServedPortfolio({
    evidence: [{
      actual_coverage: [period],
      coverage_holes: [],
      freshness_qualifiers: [],
      requested_period: period,
      source: 'Governed campaign catalog',
    }],
    insights: unavailableInsights(),
    kind: 'campaign_portfolio',
    outcome,
    period,
    prior_period: prior,
    reason_codes: outcome === 'insufficient' ? ['incompatible_scope'] : [],
    pagination: null,
    roster: { channels: [], entries: [], marketplaces: [] },
    rows: [],
    sort: { direction: 'desc', field: 'spend' },
    total_count: 0,
    totals: [],
    trend: null,
  }));
}
function completeArtifact(
  field: CampaignPortfolioArtifact['sort']['field'],
  direction: CampaignPortfolioArtifact['sort']['direction'],
): CampaignPortfolioArtifact {
  return parseCampaignPortfolioArtifact(completePayload(field, direction));
}

/** The served wire form of a complete one-campaign portfolio (fictional figures). */
function completePayload(
  field: CampaignPortfolioArtifact['sort']['field'],
  direction: CampaignPortfolioArtifact['sort']['direction'],
): Record<string, unknown> {
  const rowGrain = ['period', 'campaign', 'marketplace'];
  const totalGrain = ['period', 'marketplace'];
  return asServedPortfolio({
    evidence: [{
      actual_coverage: [period],
      coverage_holes: [],
      freshness_qualifiers: [],
      requested_period: period,
      source: 'Governed campaign catalog',
    }],
    insights: unavailableInsights(),
    kind: 'campaign_portfolio',
    outcome: 'complete',
    period,
    prior_period: prior,
    reason_codes: [],
    rows: [{
      capability: { expires_at: '2026-08-22T00:00:00+00:00', selection_reference: 'a'.repeat(43) },
      comparisons: [
        comparison('spend', '5', '10', '-5', '-50.00%', rowGrain, 'currency'),
        comparison('attributed_roas', '2.00', '1.00', '1.00', '+100.00%', rowGrain, 'ratio'),
        comparison('purchases', 2, 1, 1, '+100.00%', rowGrain, 'count'),
        comparison('ctr', '0.10', '0.20', '-0.10', '-50.00%', rowGrain, 'ratio'),
      ],
      freshness_qualifiers: [],
      signature: {
        channel: 'Fictional channel',
        effective_name: 'Fictional campaign',
        effective_name_status: 'available',
        effective_name_valid_from: '2026-01-01',
        effective_name_valid_to: null,
        marketplace: 'United States',
        platform: 'Amazon Ads',
      },
    }],
    sort: { direction, field },
    total_count: 1,
    totals: [
      comparison('spend', '5', '10', '-5', '-50.00%', totalGrain, 'currency'),
      comparison('purchases', 2, 1, 1, '+100.00%', totalGrain, 'count'),
      comparison('impressions', 100, 200, -100, '-50.00%', totalGrain, 'count'),
      comparison('portfolio_roas_attributed_14d', '2.00', '1.00', '1.00', '+100.00%', totalGrain, 'ratio'),
    ],
    trend: {
      basis: 'ad_attributed',
      grain: ['date', 'marketplace'],
      period,
      points: days.map((date) => ({
        date,
        portfolio_roas_attributed_14d: '2.00',
        purchases: 1,
        spend: 1,
      })),
      units: {
        portfolio_roas_attributed_14d: units('ratio'),
        purchases: units('count'),
        spend: units('currency'),
      },
    },
    pagination: { segment_count: 1, segment_index: 0, segment_size: 50, total_count: 1 },
    roster: {
      channels: ['Fictional channel'],
      entries: [['Fictional campaign', 0, 0, 0]],
      marketplaces: ['United States'],
    },
  });
}
function portfolioView(artifact: CampaignPortfolioArtifact) {
  return render(
    <CampaignPortfolioV2View
      child={null}
      parent={artifact}
      onClear={() => undefined}
      onSelect={() => undefined}
      selection={{ choice: null, kind: 'idle' }}
    />,
  );
}

test('Campaign Portfolio view uses plain language for no authorized campaigns', () => {
  const view = portfolioView(closedArtifact('empty'));

  assert.ok(screen.getAllByText(/No authorized campaigns are available for this reporting window/).length >= 1);
  assert.equal(view.container.textContent?.includes('empty'), false);
  assert.equal(view.container.textContent?.includes('insufficient'), false);
  assert.equal(view.container.textContent?.includes('complete'), false);
});

test('Campaign Portfolio view hides insufficient reason codes and offers recovery', () => {
  const view = portfolioView(closedArtifact('insufficient'));

  assert.ok(screen.getAllByText(/Campaign evidence could not be verified/).length >= 1);
  assert.ok(screen.getAllByText(/Choose another available window and try again/).length >= 1);
  assert.equal(view.container.textContent?.includes('incompatible_scope'), false);
  assert.equal(view.container.textContent?.includes('insufficient'), false);
});

test('Campaign Portfolio exposes server-owned order through accessible sortable headers', () => {
  const cases: ReadonlyArray<[
    Exclude<CampaignPortfolioArtifact['sort']['field'], 'name'>,
    CampaignPortfolioArtifact['sort']['direction'],
    'ascending' | 'descending',
  ]> = [
    ['spend', 'asc', 'ascending'], ['spend', 'desc', 'descending'],
    ['attributed_roas', 'asc', 'ascending'], ['attributed_roas', 'desc', 'descending'],
    ['purchases', 'asc', 'ascending'], ['purchases', 'desc', 'descending'],
    ['ctr', 'asc', 'ascending'], ['ctr', 'desc', 'descending'],
  ];
  const headerName = { attributed_roas: 'Attributed ROAS', ctr: 'Click-through rate', purchases: 'Attributed purchases', spend: 'Ad spend' } as const;
  for (const [field, direction, expected] of cases) {
    const view = portfolioView(completeArtifact(field, direction));
    assert.equal(screen.getByRole('columnheader', { name: headerName[field] }).getAttribute('aria-sort'), expected);
    view.unmount();
  }
});

test('Campaign window recovery keeps closed outcome tokens internal', () => {
  // ADR-139: the controller renders the adapter's absence through PageAbsence.
  // One preset is still available, so the copy is ABS-CAMP-1 (choose another period).
  const parent = {
    kind: 'window_unavailable',
    temporal: { presets: [{ availability: 'available', id: '7d', label: '7D', reason_code: null }] },
  } as unknown as Parameters<typeof campaignRouteAdapter.absence>[0];
  const view = render(<PageAbsence {...campaignRouteAdapter.absence(parent)} />);
  const rendered = view.container.textContent?.toLowerCase() ?? '';

  assert.ok(screen.getByText(/No campaign results are published for this period\./));
  assert.equal(rendered.includes('complete'), false);
  assert.equal(rendered.includes('empty'), false);
  assert.equal(rendered.includes('insufficient'), false);
});

/** The four portfolio slots, one per designed state (insight-presentation contract §2). */
function statefulInsights(): CampaignPortfolioArtifact['insights'] {
  return [
    {
      absence_class: null,
      absence_reason: null,
      artifact_revision: 'revision',
      evidence_line: 'Spend changed while attributed purchases were stable.',
      id: 'contribution',
      output_class: 'perspective',
      prompt_version: 'v1',
      next_step: null,
      state: 'filled',
      text: 'Spend was the clearest contributor to the portfolio movement.',
    },
    {
      absence_class: 'evaluated-quiet',
      absence_reason: 'No campaign direction materially diverges from the portfolio direction.',
      artifact_revision: 'revision',
      evidence_line: null,
      id: 'divergence',
      output_class: 'perspective',
      prompt_version: 'deterministic-gate1',
      next_step: null,
      state: 'silent',
      text: null,
    },
    {
      absence_class: 'ineligible',
      absence_reason: 'This insight requires a governed delivery follow-up read, which is not available for this view and window.',
      artifact_revision: 'revision',
      evidence_line: null,
      id: 'exposure',
      output_class: 'perspective',
      // The real wire pairing: ineligible slots are stamped by the deterministic gate.
      prompt_version: 'deterministic-gate1',
      next_step: null,
      state: 'silent',
      text: null,
    },
    {
      absence_class: 'generation-pending',
      absence_reason: 'This insight could not be generated just now; it retries automatically and appears on a later view.',
      artifact_revision: 'revision',
      evidence_line: null,
      id: 'movers',
      output_class: 'observed',
      // The real wire stamp for an unavailable movers slot.
      prompt_version: 'deterministic-v1',
      next_step: null,
      state: 'unavailable',
      text: null,
    },
  ] as CampaignPortfolioArtifact['insights'];
}

test('Campaign Portfolio view gives each insight state its designed treatment and served words', () => {
  const artifact = {
    ...completeArtifact('spend', 'desc'),
    insights: statefulInsights(),
  } as CampaignPortfolioArtifact;
  const view = portfolioView(artifact);
  const rendered = view.container.textContent ?? '';

  // Filled: narration, evidence line, output-class tag.
  assert.ok(screen.getByText('Spend was the clearest contributor to the portfolio movement.'));
  assert.ok(document.querySelector('.narr-card .ev'));
  assert.ok(screen.getByText('Reading'));
  // Each absence states its served reason verbatim under its contract-owned tag.
  assert.ok(screen.getByText('No campaign direction materially diverges from the portfolio direction.'));
  assert.ok(screen.getByText('Nothing noteworthy'));
  assert.ok(screen.getByText(/requires a governed delivery follow-up read/));
  assert.ok(screen.getByText('Needs data'));
  assert.ok(screen.getByText(/retries automatically and appears on a later view/));
  assert.ok(screen.getByText('Retrying'));
  // Treatment is carried by the card class, never by graying the served words.
  assert.ok(view.container.querySelector('.narr-card.quiet'));
  assert.ok(view.container.querySelector('.narr-card.callout'));
  assert.ok(view.container.querySelector('.narr-card.pending'));
  // No internal state or class token reaches the page.
  assert.equal(rendered.includes('filled'), false);
  assert.equal(rendered.includes('silent'), false);
  assert.equal(rendered.includes('unavailable'), false);
  assert.equal(rendered.includes('evaluated-quiet'), false);
  assert.equal(rendered.includes('ineligible'), false);
  assert.equal(rendered.includes('generation-pending'), false);
});

test('A non-filled slot served without the absence pair renders the contract fallback line', () => {
  const artifact = {
    ...completeArtifact('spend', 'desc'),
    insights: statefulInsights().map((slot) => ({
      ...slot,
      absence_class: null,
      absence_reason: null,
      evidence_line: null,
      state: 'silent',
      text: null,
    })),
  } as CampaignPortfolioArtifact;
  portfolioView(artifact);

  assert.equal(screen.getAllByText('Nothing noteworthy this window.').length, 4);
  assert.equal(screen.getAllByText('Nothing noteworthy').length, 4);
});

function commandCenterDetail(
  currentValues: readonly (number | string)[] = days.map(() => 2),
  priorValues: readonly (number | string)[] = days.map(() => 1),
): CampaignAttributionDetailV2 {
  const metricValue = (value: number | string, kind: 'count' | 'currency' | 'ratio', metricPeriod = period) => ({
    basis: 'ad_attributed' as const,
    display_value: displayValue(value, kind),
    grain: ['period', 'campaign', 'marketplace'],
    period: metricPeriod,
    units: units(kind),
    value,
  });
  const deliveryValues = (metricPeriod: typeof period) => campaignDeliverySlots.map((slot) => ({
    slot,
    value: metricValue(
      slot === 'impressions' || slot === 'clicks' ? 10 : '1.0000',
      slot === 'impressions' || slot === 'clicks' ? 'count' : slot === 'ctr' ? 'ratio' : 'currency',
      metricPeriod,
    ),
  }));
  const seriesValues = (value: number | string) => Object.fromEntries(
    campaignDeliverySlots.map((slot) => [slot, value]),
  );
  const seriesRows = days.map((date, index) => ({
    current: seriesValues(currentValues[index]!),
    date,
    prior: seriesValues(priorValues[index]!),
  }));
  return {
    boundaries: [],
    comparison: {
      metrics: [
        { slot: 'purchases', prior: metricValue(5, 'count', prior) },
        { slot: 'attributed_roas', prior: metricValue('1.0000', 'ratio', prior) },
        { slot: 'spend', prior: metricValue('10.0000', 'currency', prior) },
      ],
    },
    delivery: {
      comparison: { period: prior, slots: deliveryValues(prior) },
      dsp_quality: {
        measurable_impressions: metricValue(10, 'count'),
        outcome: 'available',
        reason_code: null,
        viewable_impressions: metricValue(10, 'count'),
        viewable_impressions_estimated: metricValue(10, 'count'),
        viewable_rate: metricValue('1.0000', 'ratio'),
      },
      evidence: acceptedEvidenceStatuses(),
      narration_reference: null,
      outcome: 'complete',
      recovery: 'change_period',
      series: {
        available_grains: ['day'],
        exact_rows: seriesRows,
        grain: 'day',
        metrics: campaignDeliverySlots,
        points: seriesRows,
        visual_id: 'campaign_delivery_daily',
      },
      summary: deliveryValues(period),
    },
    evidence_by_slot: [],
    evidence_status: detailCoverage(acceptedEvidenceStatuses()),
    insights: [
      'trend-shape', 'efficiency-drift', 'funnel', 'next-check',
    ].map((id) => ({
      artifact_revision: 'revision',
      evidence_line: null,
      id,
      output_class: id === 'next-check' ? 'hypothesis' : 'perspective',
      // The real wire stamp for an unavailable non-movers slot.
      prompt_version: 'not-generated',
      state: 'unavailable',
      text: null,
    })),
    insufficiency_reasons: [],
    kind: 'campaign_attribution_detail',
    outcome: 'selected',
    period,
    prior_period: prior,
    purchase_series: null,
    selection: {
      channel: 'Fictional channel',
      effective_name: 'Fictional campaign',
      marketplace: 'United States',
      platform: 'Amazon Ads',
    },
    summary: {
      attributed_roas: metricValue('2.0000', 'ratio'),
      purchases: metricValue(10, 'count'),
      spend: metricValue('20.0000', 'currency'),
    },
  } as unknown as CampaignAttributionDetailV2;
}

test('Campaign command center renders only server-declared paired daily delivery series', () => {
  const view = render(
    <CampaignPortfolioV2View
      child={commandCenterDetail()}
      parent={completeArtifact('spend', 'desc')}
      onClear={() => undefined}
      onSelect={() => undefined}
      selection={{ choice: null, kind: 'idle' }}
    />,
  );

  assert.ok(screen.getAllByRole('button', { name: 'Ad spend' }).length >= 1);
  assert.ok(screen.getByRole('button', { name: 'day' }));
  assert.ok(screen.getByText('Aligned prior values are returned by the server.'));
  assert.ok(screen.getAllByRole('rowheader', { name: 'Current' }).length >= 1);
  assert.ok(screen.getByRole('rowheader', { name: 'Aligned prior' }));
  assert.equal(view.container.textContent?.includes('Comparison trend values have not been published'), false);
  // Owner grid direction 2026-09-24: the eight summary metrics render inside the
  // uniform two-row grid wrapper (the 2×4 wrap itself is the CSS's job).
  const grid = view.container.querySelector('.tiles.detail-tiles');
  assert.ok(grid);
  assert.equal(grid?.querySelectorAll('.tile').length, 8);
});

test('Campaign command center plots paired series with one shared visual domain', () => {
  const view = render(
    <CampaignPortfolioV2View
      child={commandCenterDetail([100, 200, 100, 200, 100, 200, 100], [1, 2, 1, 2, 1, 2, 1])}
      parent={completeArtifact('spend', 'desc')}
      onClear={() => undefined}
      onSelect={() => undefined}
      selection={{ choice: null, kind: 'idle' }}
    />,
  );

  const chart = view.container.querySelector('.campaign-command-center svg.chart');
  assert.ok(chart);
  const currentLine = chart.querySelector('polyline.series:not(.prior)');
  const priorLine = chart.querySelector('polyline.series.prior');
  assert.ok(currentLine);
  assert.ok(priorLine);
  assert.notEqual(currentLine.getAttribute('points'), priorLine.getAttribute('points'));
});

function insufficientDeliveryDetail(): CampaignAttributionDetailV2 {
  const detail = commandCenterDetail() as unknown as Record<string, unknown>;
  const delivery = detail.delivery as Record<string, unknown>;
  return {
    ...detail,
    delivery: {
      ...delivery,
      comparison: { period: prior, slots: campaignDeliverySlots.map((slot) => ({ slot, value: null })) },
      dsp_quality: {
        measurable_impressions: null,
        outcome: 'unavailable',
        reason_code: 'dsp_quality_evidence_unavailable',
        viewable_impressions: null,
        viewable_impressions_estimated: null,
        viewable_rate: null,
      },
      evidence: refusedEvidenceStatuses(),
      outcome: 'insufficient',
      series: { ...(delivery.series as Record<string, unknown>), exact_rows: [], points: [] },
      summary: campaignDeliverySlots.map((slot) => ({ slot, value: null })),
    },
    evidence_status: detailCoverage(refusedEvidenceStatuses()),
  } as unknown as CampaignAttributionDetailV2;
}

test('An insufficient delivery renders one opener card and the served-words status sheet', async () => {
  const user = (await import('@testing-library/user-event')).default.setup();
  render(
    <CampaignPortfolioV2View
      child={insufficientDeliveryDetail()}
      parent={completeArtifact('spend', 'desc')}
      onClear={() => undefined}
      onSelect={() => undefined}
      selection={{ choice: null, kind: 'idle' }}
    />,
  );

  // One spanning card replaces five dead delivery tiles; the card is an opener.
  assert.equal(screen.queryAllByText('Not measured for this reporting window').length, 0);
  assert.ok(screen.getByText('Not enough data for this window'));
  // The grid narrows to three columns so the outcome row stays full and the ADR-072
  // card forms the second row — never a hole (owner grid direction 2026-09-24).
  assert.ok(document.querySelector('.tiles.detail-tiles.delivery-short'));
  const opener = screen.getByRole('button', { name: 'See why delivery evidence is insufficient' });
  await user.click(opener);

  const dialog = screen.getByRole('dialog', { name: 'Coverage for this window' });
  // The sheet renders the coverage block verbatim: labels, missing dates, reason, recovery.
  assert.ok(dialog.textContent?.includes('3 of 7 days have an impressions row in this window.'));
  assert.ok(dialog.textContent?.includes('This fills when the source delivers the missing days.'));
  assert.ok(dialog.textContent?.includes('Aug 19\u201321'));
  assert.ok(dialog.textContent?.includes('Fictional role daily'));
  assert.ok(dialog.textContent?.includes("Fictional impressions can't be shown for this window"));
  // No raw condition code, no browser label, and no "written by the server" line (F32).
  assert.equal(dialog.textContent?.includes('incomplete_daily_series'), false);
  assert.equal(dialog.textContent?.includes('Every line below is written by the server'), false);
  assert.equal(dialog.textContent?.includes('Needs data'), false);
  const rows = within(dialog).getAllByRole('row');
  assert.equal(rows.length, 31 + 1);
  assert.ok(within(dialog).getByRole('region', { name: 'What each figure needs' }));

  const axe = await import('axe-core');
  const results = await axe.default.run(dialog, {
    rules: { 'color-contrast': { enabled: false }, region: { enabled: false } },
  });
  assert.deepEqual(results.violations, []);

  await user.keyboard('{Escape}');
  assert.equal(screen.queryByRole('dialog'), null);
  await new Promise((resolve) => { requestAnimationFrame(resolve); });
  assert.equal(document.activeElement, opener);
});

test('An empty attribution tile is an opener into the same status sheet', async () => {
  const user = (await import('@testing-library/user-event')).default.setup();
  const detail = commandCenterDetail() as unknown as Record<string, unknown>;
  const withMissingPurchases = {
    ...detail,
    summary: { ...(detail.summary as Record<string, unknown>), purchases: null },
    comparison: null,
  } as unknown as CampaignAttributionDetailV2;
  render(
    <CampaignPortfolioV2View
      child={withMissingPurchases}
      parent={completeArtifact('spend', 'desc')}
      onClear={() => undefined}
      onSelect={() => undefined}
      selection={{ choice: null, kind: 'idle' }}
    />,
  );

  const opener = screen.getAllByRole('button', { name: 'See why this value is missing' })[0]!;
  await user.click(opener);
  assert.ok(screen.getByRole('dialog', { name: 'Coverage for this window' }));
});

test('Estate reach renders served pagination state and refuses to page past the population', async () => {
  const user = (await import('@testing-library/user-event')).default.setup();
  const segments: number[] = [];
  const view = render(
    <CampaignPortfolioV2View
      child={null}
      parent={completeArtifact('spend', 'desc')}
      onClear={() => undefined}
      onSegment={(_sort, segment) => { segments.push(segment); }}
      onSelect={() => undefined}
      selection={{ choice: null, kind: 'idle' }}
    />,
  );

  assert.ok(screen.getByText(/Rows 1–1 of 1 · sorted by ad spend, descending/));
  assert.ok(screen.getByText('Page 1 of 1'));
  const previous = screen.getByRole('button', { name: 'Previous 10 rows' });
  const next = screen.getByRole('button', { name: 'Next 10 rows' });
  assert.equal(previous.hasAttribute('disabled'), true);
  assert.equal(next.hasAttribute('disabled'), true);
  await user.click(next);
  assert.deepEqual(segments, []);
  view.unmount();
});

/** A populated baked segment: `rowCount` rows inside segment `segmentIndex` of a 62-campaign estate. */
function pagedArtifact(rowCount: number, segmentIndex: number): CampaignPortfolioArtifact {
  const base = completeArtifact('spend', 'desc');
  const template = base.rows[0]!;
  const segmentStart = segmentIndex * 50;
  const rows = Array.from({ length: rowCount }, (_ignored, index) => ({
    ...template,
    capability: {
      expires_at: template.capability.expires_at,
      selection_reference: `ref-${String(segmentStart + index).padStart(39, '0')}`,
    },
    signature: {
      ...template.signature,
      effective_name: `Fictional campaign ${segmentStart + index + 1}`,
    },
  }));
  return {
    ...base,
    pagination: { segment_count: 2, segment_index: segmentIndex, segment_size: 50, total_count: 62 },
    roster: Array.from({ length: 62 }, (_ignored, position) => ({
      channel: 'Fictional channel',
      marketplace: 'United States',
      name: `Fictional campaign ${position + 1}`,
      position,
    })),
    rows,
    total_count: 62,
  } as CampaignPortfolioArtifact;
}

test('Display paging slices ten rows per page and only a boundary crossing requests the sibling', async () => {
  const user = (await import('@testing-library/user-event')).default.setup();
  const segments: number[] = [];
  const approaches: number[] = [];
  const view = render(
    <CampaignPortfolioV2View
      child={null}
      parent={pagedArtifact(50, 0)}
      onClear={() => undefined}
      onSegment={(_sort, segment) => { segments.push(segment); }}
      onSegmentApproach={(_sort, segment) => { approaches.push(segment); }}
      onSelect={() => undefined}
      selection={{ choice: null, kind: 'idle' }}
    />,
  );

  assert.ok(screen.getByText(/Rows 1–10 of 62/));
  assert.ok(screen.getByText('Page 1 of 7'));
  assert.equal(view.container.querySelectorAll('.camp-table tbody tr').length, 10);
  const next = screen.getByRole('button', { name: 'Next 10 rows' });
  // Four page turns inside the loaded segment: request-free.
  await user.click(next);
  await user.click(next);
  await user.click(next);
  await user.click(next);
  assert.ok(screen.getByText(/Rows 41–50 of 62/));
  assert.ok(screen.getByText('Page 5 of 7'));
  assert.deepEqual(segments, []);
  // Reaching the segment's last slice prefetched the next sibling.
  assert.deepEqual(approaches, [1]);
  // The fifth turn crosses the boundary: exactly one segment request.
  await user.click(next);
  assert.deepEqual(segments, [1]);
  view.unmount();
});

test('Paging backward across a boundary lands on the previous segment\'s last slice', async () => {
  const user = (await import('@testing-library/user-event')).default.setup();
  const segments: number[] = [];
  const view = render(
    <CampaignPortfolioV2View
      child={null}
      parent={pagedArtifact(12, 1)}
      onClear={() => undefined}
      onSegment={(_sort, segment) => { segments.push(segment); }}
      onSelect={() => undefined}
      selection={{ choice: null, kind: 'idle' }}
    />,
  );

  assert.ok(screen.getByText(/Rows 51–60 of 62/));
  await user.click(screen.getByRole('button', { name: 'Previous 10 rows' }));
  assert.deepEqual(segments, [0]);
  view.unmount();
  // The sibling segment arrives (the route controller remounts the table): the
  // consumed landing hint shows its last slice, not its first.
  render(
    <CampaignPortfolioV2View
      child={null}
      parent={pagedArtifact(50, 0)}
      onClear={() => undefined}
      onSegment={() => undefined}
      onSelect={() => undefined}
      selection={{ choice: null, kind: 'idle' }}
    />,
  );
  assert.ok(screen.getByText(/Rows 41–50 of 62/));
});

test('Find a campaign is a combobox over the roster whose Enter opens the match', async () => {
  const user = (await import('@testing-library/user-event')).default.setup();
  const parent = completeArtifact('spend', 'desc');
  const opened: string[] = [];
  render(
    <CampaignPortfolioV2View
      child={null}
      parent={parent}
      onClear={() => undefined}
      onSegment={() => { throw new Error('a row in hand needs no segment request'); }}
      onSelect={(capability) => { opened.push(capability.selection_reference); }}
      selection={{ choice: null, kind: 'idle' }}
    />,
  );

  const box = screen.getByRole('combobox', { name: 'Find a campaign' });
  await user.type(box, 'fictional');
  assert.equal(box.getAttribute('aria-expanded'), 'true');
  const list = screen.getByRole('listbox', { name: 'Matching campaigns' });
  assert.equal(box.getAttribute('aria-controls'), list.id);
  assert.ok(screen.getAllByRole('option').length >= 1);
  await user.keyboard('{Enter}');
  // The first match is the canonical first row, already in hand: its detail opens.
  assert.deepEqual(opened, [parent.rows[0]!.capability.selection_reference]);
  // A query with no match says so under the served option, and lists nothing invented.
  await user.type(box, 'zzz-no-such-campaign');
  assert.equal(screen.queryAllByRole('option').length, 0);
  assert.ok(screen.getByText(/No Sponsored Products campaign matches/));
  await user.keyboard('{Escape}');
  assert.equal((box as HTMLInputElement).value, '');
});

test('The campaign name column is a declared sortable header', async () => {
  const user = (await import('@testing-library/user-event')).default.setup();
  const sorts: Array<[string, string]> = [];
  render(
    <CampaignPortfolioV2View
      child={null}
      parent={completeArtifact('spend', 'desc')}
      onClear={() => undefined}
      onSelect={() => undefined}
      onSort={(sort) => { sorts.push([sort.field, sort.direction]); }}
      selection={{ choice: null, kind: 'idle' }}
    />,
  );

  await user.click(screen.getByRole('columnheader', { name: 'Campaign' }).querySelector('button')!);
  assert.deepEqual(sorts, [['name', 'desc']]);
});

test('The campaign switcher swaps in-segment neighbors without a segment request', async () => {
  const user = (await import('@testing-library/user-event')).default.setup();
  const parent = pagedArtifact(3, 0);
  const opened: string[] = [];
  const segments: number[] = [];
  render(
    <DetailBandTitle
      detail={commandCenterDetail()}
      switcher={{
        command: parent.rows[1]!.capability,
        onSegment: (_sort, segment) => { segments.push(segment); },
        onSelect: (capability) => { opened.push(capability.selection_reference); },
        parent,
      }}
    />,
  );

  // The position readout names its ordering.
  assert.ok(screen.getByText(/Campaign command center · 2 of 62 by ad spend/));
  await user.click(screen.getByRole('button', { name: 'Next campaign by ad spend: Fictional campaign 3' }));
  await user.click(screen.getByRole('button', { name: 'Previous campaign by ad spend: Fictional campaign 1' }));
  assert.deepEqual(opened, [
    parent.rows[2]!.capability.selection_reference,
    parent.rows[0]!.capability.selection_reference,
  ]);
  assert.deepEqual(segments, []);
});

test('A switcher boundary crossing fetches the sibling segment, then opens the target campaign', async () => {
  const user = (await import('@testing-library/user-event')).default.setup();
  const parent = pagedArtifact(50, 0);
  const segments: number[] = [];
  const view = render(
    <DetailBandTitle
      detail={commandCenterDetail()}
      switcher={{
        command: parent.rows[49]!.capability,
        onSegment: (_sort, segment) => { segments.push(segment); },
        onSelect: () => undefined,
        parent,
      }}
    />,
  );

  await user.click(screen.getByRole('button', { name: 'Next campaign by ad spend' }));
  assert.deepEqual(segments, [1]);
  view.unmount();
  // The sibling arrives: the portfolio view opens the campaign at the pending position.
  const opened: string[] = [];
  render(
    <CampaignPortfolioV2View
      child={null}
      parent={pagedArtifact(12, 1)}
      onClear={() => undefined}
      onSelect={(capability) => { opened.push(capability.selection_reference); }}
      selection={{ choice: null, kind: 'idle' }}
    />,
  );
  await waitFor(() => {
    assert.deepEqual(opened, [pagedArtifact(12, 1).rows[0]!.capability.selection_reference]);
  });
});

test('The switcher jump field lands on a roster match in the canonical ordering', async () => {
  const user = (await import('@testing-library/user-event')).default.setup();
  const parent = pagedArtifact(50, 0);
  const opened: string[] = [];
  const segments: number[] = [];
  render(
    <DetailBandTitle
      detail={commandCenterDetail()}
      switcher={{
        command: parent.rows[0]!.capability,
        onSegment: (_sort, segment) => { segments.push(segment); },
        onSelect: (capability) => { opened.push(capability.selection_reference); },
        parent,
      }}
    />,
  );

  await user.type(screen.getByRole('combobox', { name: 'Find a campaign' }), 'campaign 7');
  await user.click(screen.getByRole('option', { name: /Fictional campaign 7\b/ }));
  // Position 6 is inside the loaded canonical segment: the capability is in hand.
  assert.deepEqual(opened, [parent.rows[6]!.capability.selection_reference]);
  assert.deepEqual(segments, []);
});

test('An insight card opens the insight dialog; Escape returns focus to the opening card', async () => {
  const user = (await import('@testing-library/user-event')).default.setup();
  const artifact = {
    ...completeArtifact('spend', 'desc'),
    insights: statefulInsights(),
  } as CampaignPortfolioArtifact;
  portfolioView(artifact);

  const opener = screen.getByRole('button', { name: 'Open the insight view for divergence' });
  await user.click(opener);
  const dialog = screen.getByRole('dialog');
  assert.ok(screen.getByRole('heading', { level: 2, name: 'Divergence' }));
  assert.ok(dialog.textContent?.includes('No campaign direction materially diverges from the portfolio direction.'));
  // Hop to the next slot without closing.
  await user.click(screen.getByRole('button', { name: 'Next insight' }));
  assert.ok(screen.getByRole('heading', { level: 2, name: 'Exposure' }));
  // Escape closes and returns focus to the opening card's cover button.
  await user.keyboard('{Escape}');
  assert.equal(screen.queryByRole('dialog'), null);
  await new Promise((resolve) => { requestAnimationFrame(resolve); });
  assert.equal(document.activeElement, opener);
});

test('The detail title band with the switcher mounted is axe-clean', async () => {
  const axe = await import('axe-core');
  const parent = pagedArtifact(3, 0);
  const view = render(
    <div className="band-title">
      <DetailBandTitle
        detail={commandCenterDetail()}
        switcher={{
          command: parent.rows[1]!.capability,
          onSegment: () => undefined,
          onSelect: () => undefined,
          parent,
        }}
      />
    </div>,
  );

  const results = await axe.default.run(view.container, {
    rules: { 'color-contrast': { enabled: false }, region: { enabled: false } },
  });
  assert.deepEqual(results.violations.map((violation) => violation.id), []);
});

test('A landing hint past its TTL is refused: the arriving table starts on its first page', async () => {
  const vitest = await import('vitest');
  const user = (await import('@testing-library/user-event')).default.setup();
  const view = render(
    <CampaignPortfolioV2View
      child={null}
      parent={pagedArtifact(12, 1)}
      onClear={() => undefined}
      onSegment={() => undefined}
      onSelect={() => undefined}
      selection={{ choice: null, kind: 'idle' }}
    />,
  );
  // Arm the backward-landing hint, then let it expire before the arrival.
  await user.click(screen.getByRole('button', { name: 'Previous 10 rows' }));
  view.unmount();
  vitest.vi.useFakeTimers({ toFake: ['Date'] });
  try {
    vitest.vi.setSystemTime(Date.now() + 31_000);
    render(
      <CampaignPortfolioV2View
        child={null}
        parent={pagedArtifact(50, 0)}
        onClear={() => undefined}
        onSegment={() => undefined}
        onSelect={() => undefined}
        selection={{ choice: null, kind: 'idle' }}
      />,
    );
    assert.ok(screen.getByText(/Rows 1–10 of 62/));
  } finally {
    vitest.vi.useRealTimers();
  }
});

test('A campaign tile shows the two-place display value while its exact table keeps four places', () => {
  const detail = commandCenterDetail(days.map(() => '75459.6400'), days.map(() => '1.0000')) as unknown as {
    summary: { spend: Record<string, unknown> };
  };
  // Spend of 75459.6400 USD is served with its 75459.64 display value.
  detail.summary.spend = { ...detail.summary.spend, display_value: '75459.64', value: '75459.6400' };
  const view = render(
    <CampaignPortfolioV2View
      child={detail as unknown as CampaignAttributionDetailV2}
      parent={completeArtifact('spend', 'desc')}
      onClear={() => undefined}
      onSelect={() => undefined}
      selection={{ choice: null, kind: 'idle' }}
    />,
  );
  const spendTile = Array.from(view.container.querySelectorAll('.detail-tiles .tile'))
    .find((tile) => tile.querySelector('.k')?.textContent === 'Ad spend');
  assert.equal(spendTile?.querySelector('.v')?.textContent, '75,459.64 USD');
  // The exact-values disclosure is unchanged: the served series at its own four places.
  assert.ok(view.container.querySelector('.campaign-command-center details.exact')?.textContent?.includes('75,459.6400 USD'));
});

test('The delivery evidence table shows two-place money and no longer claims exact values', () => {
  const view = render(
    <CampaignPortfolioV2View
      child={commandCenterDetail()}
      parent={completeArtifact('spend', 'desc')}
      onClear={() => undefined}
      onSelect={() => undefined}
      selection={{ choice: null, kind: 'idle' }}
    />,
  );
  const panel = view.container.querySelector('section[aria-labelledby="delivery-values-heading"]');
  assert.ok(panel);
  // Delivery spend of 1.0000 USD reads 1.00 USD; impressions (a count of 10) stay 10.
  const rows = Array.from(panel.querySelectorAll('tbody tr')).map((row) => Array.from(row.children).map((cell) => cell.textContent));
  assert.ok(rows.some((row) => row[1] === '1.00 USD'));
  assert.ok(rows.some((row) => row[0] === 'Impressions' && row[1] === '10'));
  assert.equal(/exact/i.test(panel.textContent ?? ''), false);
  assert.ok(panel.textContent?.includes('Monetary values show at two decimal places.'));
});

test('A typed-absent ratio renders an em dash with its reason and no change chip (ADR-087)', () => {
  const served = structuredClone(completeArtifact('spend', 'desc')) as unknown as {
    rows: { comparisons: Record<string, unknown>[] }[];
  };
  const comparisons = served.rows[0]!.comparisons;
  // Zero spend in the window: spend is a measured 0.00, ROAS over it is undefined.
  comparisons[0] = comparison('spend', '0', '10', '-10', '-100.00%', ['period', 'campaign', 'marketplace'], 'currency');
  const roas = comparisons[1] as { current: Record<string, unknown>; absolute_change: unknown; change: unknown; percentage_change: unknown };
  roas.current.value = null;
  roas.absolute_change = null;
  roas.percentage_change = null;
  roas.change = null;
  const { container } = portfolioView(served as unknown as CampaignPortfolioArtifact);
  const dash = screen.getByRole('img', { name: 'Not applicable: no spend in this window' });
  assert.equal(dash.textContent, '—');
  const cells = container.querySelectorAll('tbody tr td.num');
  assert.equal(cells[1]?.querySelector('.chg'), null);
  // Additive control: zero spend renders 0.00 with its chip.
  assert.match(cells[0]?.textContent ?? '', /0\.00/);
  assert.notEqual(cells[0]?.querySelector('.chg'), null);
  // Positive control: CTR with a real denominator still renders its value and chip, as a rate (D4).
  assert.match(cells[3]?.textContent ?? '', /10\.00%/);
  assert.notEqual(cells[3]?.querySelector('.chg'), null);
});

/* ---------- Slice 4: coverage and typed absence (ADR-119, ADR-118 decision 5) ---------- */

const PORTFOLIO_COVERAGE: readonly (readonly [string, string, string])[] = [
  ['spend', 'Ad spend', 'Headline and campaign rows'],
  ['purchases', 'Attributed purchases', 'Headline and campaign rows'],
  ['impressions', 'Impressions', 'Headline'],
  ['portfolio_roas_attributed_14d', 'Portfolio ROAS (attributed, 14 day)', 'Headline'],
  ['attributed_roas', 'ROAS (attributed, 14 day)', 'Campaign rows'],
  ['ctr', 'Click-through rate', 'Campaign rows'],
];

/**
 * The Campaigns coverage block as the server writes it (fictional window). `missing` names a
 * figure whose Aug 21 hasn't arrived; `partial` one whose Aug 21 came from some campaigns only.
 */
function portfolioCoverage({ missing, partial }: { readonly missing?: string; readonly partial?: string }): Record<string, unknown> {
  return {
    entries: PORTFOLIO_COVERAGE.map(([slot, label, role_label]) => {
      if (slot === missing) {
        return {
          absence_text: '1 day not yet sent.', coverage_text: '6 of 7 days', label,
          missing_dates: ['2026-08-21'], missing_text: 'Aug 21',
          reason: 'Covers Aug 15\u201320 (6 of 7 days).',
          recovery: 'The figure and its change update by themselves when the day arrives.',
          role: 'current', role_label, slot, state: 'missing_days',
        };
      }
      if (slot === partial) {
        return {
          absence_text: '1 partial day.', coverage_text: 'Aug 15\u201320, 6 of 7 days', label,
          missing_dates: ['2026-08-21'], missing_text: 'Aug 21',
          reason: 'Covers Aug 15\u201320 (6 of 7 days): a partly reported day is left out of both parts.',
          recovery: 'The day and the change show when the rest of the day arrives.',
          role: 'current', role_label, slot, state: 'partial_days',
        };
      }
      return {
        absence_text: null, coverage_text: null, label, missing_dates: [], missing_text: 'None',
        reason: 'Every day of this window has arrived.', recovery: 'No action needed.',
        role: 'current', role_label, slot, state: 'complete',
      };
    }),
    summary: 'Fictional summary: one figure is partly reported for this window. Every other figure is complete.',
    version: 2,
    window: period,
  };
}

/** Withhold one metric's change on every surface, as the server serves it. */
function withholdMetric(payload: Record<string, unknown>, metric: string): Record<string, unknown> {
  const hold = (entry: Record<string, unknown>) => (entry.metric === metric
    ? { ...entry, absolute_change: null, change: null, percentage_change: null }
    : entry);
  const rows = payload.rows as Record<string, unknown>[];
  return {
    ...payload,
    rows: rows.map((row) => ({ ...row, comparisons: (row.comparisons as Record<string, unknown>[]).map(hold) })),
    totals: (payload.totals as Record<string, unknown>[]).map(hold),
  };
}

function missingSpendDayPayload(): Record<string, unknown> {
  return { ...withholdMetric(completePayload('spend', 'desc'), 'spend'), evidence_status: portfolioCoverage({ missing: 'spend' }) };
}

test('A missing day withholds the change on the tile and row, names the covered days and opens the coverage sheet', async () => {
  const user = (await import('@testing-library/user-event')).default.setup();
  const view = portfolioView(parseCampaignPortfolioArtifact(missingSpendDayPayload()));
  const spendTile = [...view.container.querySelectorAll('.tiles .tile')].find((tile) => tile.querySelector('.k')?.textContent === 'Ad spend')!;
  // The figure shows over the days that arrived; its change is withheld, never zero or hidden.
  assert.ok(spendTile.textContent?.includes('1 day not yet sent.'));
  assert.equal(spendTile.querySelector('.basis')?.textContent, '6 of 7 days');
  assert.equal(spendTile.querySelector('.chg'), null);
  // The absence line reads after the basis, outside the chip slot, as the mockup does.
  const absence = spendTile.querySelector('.tile-absence')!;
  assert.ok(absence.textContent?.startsWith('1 day not yet sent.'));
  assert.ok((spendTile.querySelector('.basis')!.compareDocumentPosition(absence) & Node.DOCUMENT_POSITION_FOLLOWING) !== 0);
  assert.equal(spendTile.querySelector('.row .chg-absent'), null);
  const purchasesTile = [...view.container.querySelectorAll('.tiles .tile')].find((tile) => tile.querySelector('.k')?.textContent === 'Attributed purchases')!;
  assert.ok(purchasesTile.querySelector('.chg'));
  assert.equal(purchasesTile.querySelector('.basis'), null);
  // Each "See why" names its figure, unique on the page (M5-12).
  const names = screen.getAllByRole('button', { name: /^See why: / }).map((button) => button.getAttribute('aria-label'));
  assert.deepEqual(names, ['See why: Ad spend', 'See why: Ad spend, Fictional campaign']);
  const table = screen.getByRole('table', { name: /Campaigns in this window/ });
  assert.ok(within(table).getByRole('columnheader', { name: /Ad spend 6 of 7 days/ }));
  const opener = screen.getByRole('button', { name: 'See why: Ad spend' });
  await user.click(opener);
  const dialog = screen.getByRole('dialog', { name: 'Coverage for this window' });
  assert.ok(dialog.textContent?.includes('Campaigns'));
  assert.ok(dialog.textContent?.includes('Aug 21'));
  assert.ok(dialog.textContent?.includes('Covers Aug 15\u201320 (6 of 7 days).'));
  assert.ok(dialog.textContent?.includes('Fictional summary: one figure is partly reported'));
  const axe = await import('axe-core');
  const results = await axe.default.run(dialog, { rules: { 'color-contrast': { enabled: false }, region: { enabled: false } } });
  assert.deepEqual(results.violations, []);
  await user.keyboard('{Escape}');
  await new Promise((resolve) => { requestAnimationFrame(resolve); });
  assert.equal(document.activeElement, opener);
});

test('A stacked typed-absence cell drops its coverage text, so "6 of 7 days" never sits beside a dash', () => {
  // Fictional sponsored campaign with no impressions: its CTR is typed absent ("—") while the
  // CTR column is partly reported for the window (ADR-118 decision 5 overrides the mockup).
  const base = completePayload('spend', 'desc');
  const rows = (base.rows as Record<string, unknown>[]).map((row) => ({
    ...row,
    comparisons: (row.comparisons as Record<string, unknown>[]).map((entry) => (entry.metric === 'ctr'
      ? {
        ...entry,
        absolute_change: null, change: null, percentage_change: null,
        current: { ...(entry.current as Record<string, unknown>), value: null },
      }
      : entry)),
    signature: { ...(row.signature as Record<string, unknown>), channel: 'Sponsored Products' },
  }));
  const artifact = parseCampaignPortfolioArtifact({
    ...base,
    evidence_status: portfolioCoverage({ partial: 'ctr' }),
    roster: { ...(base.roster as Record<string, unknown>), channels: ['Sponsored Products'] },
    rows,
  });
  const view = portfolioView(artifact);
  const table = screen.getByRole('table', { name: /Campaigns in this window/ });
  assert.ok(within(table).getByRole('columnheader', { name: /Click-through rate Aug 15\u201320, 6 of 7 days/ }));
  const cell = view.container.querySelector('td[data-label="Click-through rate"]')!;
  assert.ok(cell.textContent?.includes('\u2014'));
  assert.equal(cell.querySelector('.basis-inline'), null);
  assert.equal(cell.textContent?.includes('6 of 7 days'), false);
});

test('The portfolio decoder holds a withheld change to the coverage block and refuses a body without its block', () => {
  // Paired positive control: a complete current body carries its block.
  assert.notEqual(parseCampaignPortfolioArtifact(completePayload('spend', 'desc')).evidence_status, null);
  const decoded = parseCampaignPortfolioArtifact(missingSpendDayPayload());
  assert.equal(decoded.totals[0]?.withheld, true);
  assert.equal(decoded.rows[0]?.comparisons[0]?.withheld, true);
  assert.equal(decoded.totals[1]?.withheld, false);
  const { evidence_status: _omitted, ...withoutBlock } = missingSpendDayPayload();
  void _omitted;
  for (const forged of [
    // The retired format-7 form (no key) and a complete body with a null block.
    withoutBlock,
    { ...completePayload('spend', 'desc'), evidence_status: null },
    { ...completePayload('spend', 'desc'), evidence_status: portfolioCoverage({ missing: 'spend' }) },
    { ...missingSpendDayPayload(), evidence_status: portfolioCoverage({ missing: 'purchases' }) },
    { ...missingSpendDayPayload(), evidence_status: { ...portfolioCoverage({ missing: 'spend' }), window: prior } },
    { ...missingSpendDayPayload(), evidence_status: { ...portfolioCoverage({ missing: 'spend' }), version: 1 } },
  ]) {
    assert.throws(() => parseCampaignPortfolioArtifact(forged), { name: 'CampaignAttributionContractError' });
  }
});

/** A portfolio whose contribution card is filled with one served `NextStep.v2` (ADR-122). */
function steppedPayload(nextStep: Record<string, unknown>, payload = completePayload('spend', 'desc')): Record<string, unknown> {
  const insights = (payload.insights as Record<string, unknown>[]).map((slot) => ({ ...slot }));
  insights[0] = {
    artifact_revision: 'revision',
    evidence_line: 'Read from the values this page already shows.',
    id: 'contribution',
    next_step: nextStep,
    output_class: 'perspective',
    prompt_version: 'v1',
    state: 'filled',
    text: 'Spend was the clearest contributor to the portfolio movement.',
  };
  return { ...payload, insights };
}

test('A card\'s campaign step opens the row its served capability names (ADR-122)', async () => {
  const user = (await import('@testing-library/user-event')).default.setup();
  const selected: unknown[] = [];
  const artifact = parseCampaignPortfolioArtifact(steppedPayload({
    kind: 'open_campaign_detail',
    label: 'Open Fictional campaign',
    reason: 'It moved the portfolio most this window.',
    target: { campaign_ref: 'a'.repeat(43), page_id: 'campaigns' },
  }));
  render(<CampaignPortfolioV2View
    child={null}
    parent={artifact}
    onClear={() => undefined}
    onSelect={(capability) => selected.push(capability)}
    selection={{ choice: null, kind: 'idle' }}
  />);
  const chip = screen.getByRole('button', { name: 'Open Fictional campaign' });
  assert.equal(chip.closest('.narr-card') !== null, true);
  await user.click(chip);
  assert.deepEqual(selected, [artifact.rows[0]?.capability]);
});

test('A card\'s metric and coverage steps open the page\'s focus dialog and coverage sheet', async () => {
  const user = (await import('@testing-library/user-event')).default.setup();
  const focus = portfolioView(parseCampaignPortfolioArtifact(steppedPayload({
    kind: 'open_metric_focus',
    label: 'Open ad spend by day',
    reason: 'The daily values show when it moved.',
    target: { metric: 'spend', page_id: 'campaigns' },
  })));
  await user.click(screen.getByRole('button', { name: 'Open ad spend by day' }));
  assert.ok(screen.getByRole('dialog', { name: 'Ad spend' }));
  await user.keyboard('{Escape}');
  focus.unmount();
  const evidence = { kind: 'open_evidence_status', label: 'See why: this page\'s coverage', reason: 'One day of ad spend has not arrived.', target: null };
  portfolioView(parseCampaignPortfolioArtifact(steppedPayload(evidence, missingSpendDayPayload())));
  const chip = screen.getByRole('button', { name: 'See why: this page\'s coverage' });
  await user.click(chip);
  assert.ok(screen.getByRole('dialog', { name: 'Coverage for this window' }));
  await user.keyboard('{Escape}');
  await new Promise((resolve) => { requestAnimationFrame(resolve); });
  assert.equal(document.activeElement, chip);
});

test('A step to another tab\'s view renders no chip, and no complete body lacks the block a coverage step opens', () => {
  const elsewhere = portfolioView(parseCampaignPortfolioArtifact(steppedPayload({
    kind: 'open_metric_focus',
    label: 'Open shipped revenue by day',
    reason: 'A fictional reason.',
    target: { metric: 'retail_revenue_shipped', page_id: 'retail-health' },
  })));
  assert.equal(elsewhere.container.querySelector('.next-step'), null);
  elsewhere.unmount();
  // The block-less complete body a coverage step once met is refused (ADR-119 decision 4).
  assert.throws(() => parseCampaignPortfolioArtifact(steppedPayload({
    kind: 'open_evidence_status', label: 'See why', reason: 'A fictional reason.', target: null,
  }, { ...completePayload('spend', 'desc'), evidence_status: null })), { name: 'CampaignAttributionContractError' });
});

test('each campaign row carries one freshness badge instead of the staleness sentences (D5)', () => {
  const served = structuredClone(completeArtifact('spend', 'desc')) as unknown as {
    rows: { freshness_qualifiers: string[] }[];
  };
  const current = portfolioView(served as unknown as CampaignPortfolioArtifact);
  assert.match(current.container.querySelector('tbody tr td.l')?.textContent ?? '', /^Latest data [A-Z][a-z]{2} \d{1,2}, \d{4}$/);
  current.unmount();
  served.rows[0]!.freshness_qualifiers = ['Fictional source arrived late'];
  const late = portfolioView(served as unknown as CampaignPortfolioArtifact);
  // The served sentence stays as the badge's accessible description, not visible prose.
  assert.equal(late.container.querySelector('tbody tr td.l')?.textContent, 'Late data: Fictional source arrived late');
  assert.equal(late.container.querySelector('tbody tr td.l .sr-only')?.textContent, ': Fictional source arrived late');
});
