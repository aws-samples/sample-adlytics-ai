// SPDX-License-Identifier: MIT-0

type Json = Record<string, unknown>;
export const RETENTION_REVISION = 'r'.repeat(43);
const groups = [
  ['dsp_only', 'Won by DSP only'],
  ['sponsored_only', 'Won by sponsored ads only'],
  ['dsp_and_sponsored', 'Won by both'],
] as const;
const figure = (value: string, text: string) => ({ value, text });
const cohortSize = (value: string, text: string) => figure(value, `${text} new customers`);
const status = (offset: number, state: 'not_returned' | 'not_yet') => ({
  cohort_size: null, column_label: `Month ${offset}`, offset, rate: null, state,
  text: state === 'not_returned' ? 'Not available' : 'Not yet',
});
const grids = (state: 'complete' | 'insufficient') => groups.map(([group, label]) => ({
  grid_label: `${label} · ad-touched repeat only. Understates true repeat. Month 0 repeats are excluded.`,
  group, label,
  rows: [{
    absent_text: null,
    cells: state === 'complete' ? [
      { cohort_size: cohortSize('1200', '1,200'), column_label: 'Month 1', offset: 1, rate: figure('0.1250', '12.5%'), state: 'value', text: null },
      status(2, 'not_returned'),
      ...Array.from({ length: 10 }, (_, index) => status(index + 3, 'not_yet')),
    ] : [status(1, 'not_returned'), ...Array.from({ length: 11 }, (_, index) => status(index + 2, 'not_yet'))],
    cohort: '2026-06-01', label: 'June 2026',
  }],
}));
const lines = (state: 'complete' | 'insufficient') => groups.map(([group, label]) => ({
  group, label,
  points: Array.from({ length: 12 }, (_, index) => ({
    label: `Month ${index + 1}`, offset: index + 1,
    rate: state === 'complete' ? figure('0.2000', '20.0%') : null,
    text: state === 'complete' ? null : 'Not available',
  })),
}));
const insight = (state: 'complete' | 'insufficient' | 'empty') => ({
  ...(state === 'complete' ? {} : {
    absence_class: 'evaluated-quiet',
    absence_reason: 'No fictional shift cleared the threshold.',
  }),
  artifact_revision: RETENTION_REVISION,
  evidence_line: state === 'complete' ? 'Found by a fixed rule from the figures above. Not written by an AI model.' : null,
  id: 'retention-shift', output_class: 'perspective', prompt_version: 'deterministic-v1',
  state: state === 'complete' ? 'filled' : 'silent',
  text: state === 'complete' ? 'For shoppers won by DSP only, the month-1 repeat rate rose 2.0 points.' : null,
});

export function retentionFixture(outcome: 'complete' | 'empty' | 'insufficient' = 'complete'): Json {
  const available = outcome !== 'empty';
  return {
    boundary: [
      'A cohort is the shoppers first won in one calendar month.',
      'The standard path counts ad-touched repeat purchases only.',
      'Repeats are from this advertiser.',
    ],
    evidence_status: { slots: outcome === 'empty' ? [{ marketplace_key: 'US', missing_dates: [{ end: '2026-09-30', start: '2025-09-01' }], reason_text: 'No cohorts were measured for this vintage.', recovery_text: null, role_label: 'This vintage', slot_label: 'Cohorts' }] : [], version: 2 },
    markets: [{
      absence_text: outcome === 'complete' ? null : outcome === 'empty'
        ? 'No cohorts were measured for this vintage.'
        : 'There is not enough cohort evidence to show Retention safely.',
      cohort_grain: available ? 'month' : null,
      curve: { label: 'Cumulative repeat rate · Amazon account IDs', lines: available ? lines(outcome as 'complete' | 'insufficient') : [], metric: 'cumulative_repeat_rate_account_ids' },
      grain_note: null,
      insights: [insight(outcome)],
      marketplace_key: 'US', marketplace_text: null, outcome,
      path: { paid: { callout: 'All Amazon purchases by these cohorts, not only ad-touched ones, need the Amazon Retail Purchases paid feature.', state: 'not_observed' }, standard: { available } },
      scope_band: available ? 'Cohorts as of September 2026, grouped by month: acquired September 2025 to August 2026, with repeat months through September 2026.' : null,
      triangle: { identity_caveat: 'Amazon account IDs only.', legend: 'Not available: too few shoppers bought again, or none', paid: [], standard: available ? grids(outcome as 'complete' | 'insufficient') : [] },
    }],
    outcome,
    page_context_expires_at: '2099-10-16T12:00:00+00:00',
    page_context_label: 'Retention · cohorts as of September 2026',
    starter_questions: outcome === 'complete' ? ["Which acquisition group's cohorts bought again most by month 3, as of September 2026?"] : [],
    type: 'RetentionCohortArtifact', version: 1,
    vintage: { as_of_month: '2026-09', end: '2026-09-30', label: 'September 2026', start: '2025-09-01' },
  };
}

export function cloneRetention<T>(value: T): T { return structuredClone(value); }
/** Two complete marketplaces, each named by its server-owned marketplace text. */
export function retentionTwoMarketFixture(): Json {
  const fixture = retentionFixture();
  const [us] = fixture.markets as Json[];
  const uk = { ...structuredClone(us!), marketplace_key: 'UK', marketplace_text: 'UK marketplace' };
  us!.marketplace_text = 'US marketplace';
  fixture.markets = [us!, uk];
  return fixture;
}
