// SPDX-License-Identifier: MIT-0
import assert from 'node:assert/strict';
import { test } from 'vitest';

import { changeBlock } from './fixtures/change-fixture';
import { asServedPortfolio } from './fixtures/portfolio-fixture';
import { CampaignAttributionContractError, parseCampaignPortfolioArtifact, parseCampaignRosterShard } from '../lib/campaign-attribution';
const current = { end: '2026-08-21', start: '2026-08-15' };
const prior = { end: '2026-08-14', start: '2026-08-08' };
const rowGrain = ['period', 'campaign', 'marketplace'];
const totalGrain = ['period', 'marketplace'];
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

function metricValue(
  value: number | string,
  period: typeof current,
  grain: readonly string[],
  kind: 'count' | 'currency' | 'ratio',
) {
  return { basis: 'ad_attributed', display_value: displayValue(value, kind), grain, period, units: units(kind), value };
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
    current: metricValue(currentValue, current, grain, kind),
    metric,
    prior: metricValue(priorValue, prior, grain, kind),
  };
}

function insights(): Record<string, unknown>[] {
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

function artifact(): Record<string, unknown> {
  const rowComparisons = [
    comparison('spend', '5', '10', '-5', '-50.00%', rowGrain, 'currency'),
    comparison('attributed_roas', '2.00', '1.00', '1.00', '+100.00%', rowGrain, 'ratio'),
    comparison('purchases', 2, 1, 1, '+100.00%', rowGrain, 'count'),
    comparison('ctr', '0.10', '0.20', '-0.10', '-50.00%', rowGrain, 'ratio'),
  ];
  return asServedPortfolio({
    evidence: [{
      actual_coverage: [current],
      coverage_holes: [],
      freshness_qualifiers: [],
      requested_period: current,
      source: 'Governed campaign catalog',
    }],
    insights: insights(),
    kind: 'campaign_portfolio',
    outcome: 'complete',
    period: current,
    prior_period: prior,
    reason_codes: [],
    rows: [{
      capability: { expires_at: '2026-08-22T00:00:00+00:00', selection_reference: 'a'.repeat(43) },
      comparisons: rowComparisons,
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
    sort: { direction: 'desc', field: 'spend' },
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
      period: current,
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

function record(value: unknown): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) throw new Error('expected record');
  return value as Record<string, unknown>;
}

function list(value: unknown): unknown[] {
  if (!Array.isArray(value)) throw new Error('expected list');
  return value;
}

function firstComparison(value: Record<string, unknown>): Record<string, unknown> {
  const row = record(list(value.rows)[0]);
  return record(list(row.comparisons)[0]);
}

function firstTotal(value: Record<string, unknown>): Record<string, unknown> {
  return record(list(value.totals)[0]);
}

test('Campaign Portfolio decoder accepts server-signed decreasing deltas', () => {
  const result = parseCampaignPortfolioArtifact(artifact());

  assert.equal(result.rows[0]?.comparisons[0]?.absolute_change, '-5');
  assert.equal(result.totals[0]?.change?.display_text, '-50.00%');
});

test('Campaign Portfolio decoder rejects forged arithmetic and semantic drift', () => {
  const hostilePayloads: ReadonlyArray<(value: Record<string, unknown>) => void> = [
    (value) => { firstComparison(value).absolute_change = '-4'; },
    (value) => { firstComparison(value).percentage_change = '-40.00%'; },
    (value) => { record(firstComparison(value).current).value = '-5'; },
    (value) => { record(firstComparison(value).current).grain = totalGrain; },
    (value) => { record(firstTotal(value).current).units = units('count'); },
    (value) => { record(firstComparison(value).prior).period = { end: '2026-08-13', start: '2026-08-07' }; },
    (value) => { record(record(value.trend).units).spend = units('count'); },
  ];

  for (const mutate of hostilePayloads) {
    const value = structuredClone(artifact());
    mutate(value);
    assert.throws(() => parseCampaignPortfolioArtifact(value));
  }
});


test('Campaign Portfolio decoder rejects rows, totals, trend, count, and truncation for closed outcomes', () => {
  const hostilePayloads: ReadonlyArray<(value: Record<string, unknown>) => void> = [
    (value) => { value.outcome = 'empty'; },
    (value) => {
      value.outcome = 'insufficient';
      value.reason_codes = ['incompatible_scope'];
    },
  ];

  for (const mutate of hostilePayloads) {
    const value = structuredClone(artifact());
    mutate(value);
    assert.throws(() => parseCampaignPortfolioArtifact(value));
  }
});


function insufficientArtifact(): Record<string, unknown> {
  return {
    ...artifact(),
    evidence_status: null,
    outcome: 'insufficient',
    pagination: null,
    reason_codes: ['missing_catalog'],
    return_lists: null,
    roster: { channels: [], entries: [], marketplaces: [] },
    rows: [],
    total_count: 0,
    totals: [],
    trend: null,
  };
}

test('Campaign Portfolio decoder rejects unknown, out-of-order, and duplicate insufficiency reasons', () => {
  const hostileReasonCodes = [
    ['unknown_reason'],
    ['incompatible_scope', 'missing_catalog'],
    ['missing_catalog', 'missing_catalog'],
  ];

  for (const reason_codes of hostileReasonCodes) {
    const value = insufficientArtifact();
    value.reason_codes = reason_codes;
    assert.throws(() => parseCampaignPortfolioArtifact(value));
  }
});


test('Campaign Portfolio decoder requires the declared insight roster and slot state', () => {
  const valid = parseCampaignPortfolioArtifact(artifact());
  assert.equal(valid.insights[3]?.id, 'movers');
  assert.equal(valid.insights[3]?.output_class, 'observed');

  const hostilePayloads: ReadonlyArray<(value: Record<string, unknown>) => void> = [
    (value) => { delete value.insights; },
    (value) => { list(value.insights).reverse(); },
    (value) => { record(list(value.insights)[0]).output_class = 'observed'; },
    (value) => { record(list(value.insights)[0]).state = 'filled'; },
    (value) => { record(list(value.insights)[0]).artifact_revision = ' '; },
  ];
  for (const mutate of hostilePayloads) {
    const value = structuredClone(artifact());
    mutate(value);
    assert.throws(() => parseCampaignPortfolioArtifact(value));
  }
});


test('Campaign Portfolio decoder accepts exactly the base and absence-declared slot shapes', () => {
  // A slot with no declared absence carries the seven base fields only (every
  // filled slot, and slots persisted before the taxonomy).
  const undeclared = parseCampaignPortfolioArtifact(artifact());
  assert.equal(undeclared.insights[0]?.absence_class, null);
  assert.equal(undeclared.insights[0]?.absence_reason, null);

  const declared = artifact();
  for (const slot of list(declared.insights)) {
    record(slot).absence_class = 'generation-pending';
    record(slot).absence_reason = 'This insight could not be generated just now.';
  }
  const decoded = parseCampaignPortfolioArtifact(declared);
  assert.equal(decoded.insights[0]?.absence_class, 'generation-pending');
  assert.equal(decoded.insights[0]?.absence_reason, 'This insight could not be generated just now.');

  const hostilePayloads: ReadonlyArray<(value: Record<string, unknown>) => void> = [
    // A class without its sibling reason key is neither legal shape.
    (value) => { record(list(value.insights)[0]).absence_class = 'generation-pending'; },
    (value) => {
      record(list(value.insights)[0]).absence_class = 'mystery-class';
      record(list(value.insights)[0]).absence_reason = null;
    },
    // A reason requires its class.
    (value) => {
      record(list(value.insights)[0]).absence_class = null;
      record(list(value.insights)[0]).absence_reason = 'A reason without a class.';
    },
  ];
  for (const mutate of hostilePayloads) {
    const value = structuredClone(artifact());
    mutate(value);
    assert.throws(() => parseCampaignPortfolioArtifact(value));
  }
});


test('Campaign Portfolio decoder reaches the tail of a 1,000-campaign compact roster', () => {
  const value = artifact();
  const template = record(list(value.rows)[0]);
  value.rows = Array.from({ length: 50 }, (_, index) => {
    const row = structuredClone(template);
    record(row.capability).selection_reference = String(index).padStart(43, '0');
    record(row.signature).effective_name = `Fictional Campaign ${String(index + 1).padStart(4, '0')}`;
    return row;
  });
  value.total_count = 1_000;
  value.pagination = {
    segment_count: 20,
    segment_index: 0,
    segment_size: 50,
    total_count: 1_000,
  };
  value.roster = {
    channels: ['Fictional channel'],
    entries: Array.from({ length: 1_000 }, (_, index) => [
      `Fictional Campaign ${String(index + 1).padStart(4, '0')}`,
      0,
      0,
      index,
    ]),
    marketplaces: ['United States'],
  };

  const result = parseCampaignPortfolioArtifact(value);

  assert.equal(result.rows.length, 50);
  assert.equal(result.roster.length, 1_000);
  assert.equal(result.roster[999]?.position, 999);
  assert.equal(result.pagination?.segment_count, 20);
});


test('Campaign Portfolio decoder carries the served two-place display beside the exact value', () => {
  const result = parseCampaignPortfolioArtifact(artifact());
  // Spend of 5 USD displays as 5.00; purchases (a count of 2) carry no display value.
  assert.equal(result.totals[0]?.current.display_value, '5.00');
  assert.equal(result.totals[0]?.current.value, '5');
  assert.equal(result.totals[1]?.current.display_value, null);
});

test('Campaign Portfolio decoder refuses a missing, three-place, or count display value', () => {
  const hostilePayloads: ReadonlyArray<(value: Record<string, unknown>) => void> = [
    (value) => { delete record(firstTotal(value).current).display_value; },
    (value) => { record(firstTotal(value).current).display_value = '5.000'; },
    (value) => { record(firstTotal(value).current).display_value = '5'; },
    (value) => { record(firstTotal(value).current).display_value = null; },
    (value) => { record(record(list(value.totals)[1]).current).display_value = '2.00'; },
  ];
  for (const mutate of hostilePayloads) {
    const value = structuredClone(artifact());
    mutate(value);
    assert.throws(() => parseCampaignPortfolioArtifact(value));
  }
});

function roasComparison(value: Record<string, unknown>): Record<string, unknown> {
  const row = record(list(value.rows)[0]);
  return record(list(row.comparisons)[1]);
}

function typeAbsent(item: Record<string, unknown>, side: 'current' | 'prior'): void {
  const sideValue = record(item[side]);
  sideValue.value = null;
  sideValue.display_value = null;
  item.absolute_change = null;
  item.percentage_change = null;
  item.change = null;
}

test('Campaign Portfolio decoder accepts a typed-absent ratio with no change (ADR-087)', () => {
  const value = structuredClone(artifact());
  typeAbsent(roasComparison(value), 'current');
  const result = parseCampaignPortfolioArtifact(value);
  const roas = result.rows[0]?.comparisons[1];
  assert.equal(roas?.metric, 'attributed_roas');
  assert.equal(roas?.current.value, null);
  assert.equal(roas?.absolute_change, null);
  assert.equal(roas?.change, null);
  // Positive control: the other ratio in the row keeps its served value.
  assert.notEqual(result.rows[0]?.comparisons[3]?.current.value, null);
});

test('Campaign Portfolio decoder refuses a null additive value and a change beside a null ratio', () => {
  const hostilePayloads: ReadonlyArray<(value: Record<string, unknown>) => void> = [
    (value) => { typeAbsent(firstComparison(value), 'current'); },
    (value) => { typeAbsent(roasComparison(value), 'prior'); roasComparison(value).absolute_change = '0'; },
    (value) => { typeAbsent(roasComparison(value), 'current'); roasComparison(value).percentage_change = '+0.00%'; },
  ];
  for (const mutate of hostilePayloads) {
    const value = structuredClone(artifact());
    mutate(value);
    assert.throws(() => parseCampaignPortfolioArtifact(value));
  }
});
/** Rewrite one current comparison in the form a body baked before change blocks carries. */
function asRetained(item: Record<string, unknown>, percentage: string | null): void {
  delete item.change;
  item.percentage_change = percentage;
}
test('Campaign Portfolio decoder reads each comparison\'s server change block (ADR-121)', () => {
  const result = parseCampaignPortfolioArtifact(artifact());
  assert.deepEqual(result.rows[0]?.comparisons[0]?.change, {
    direction: 'down', display_text: '-50.00%', not_meaningful: null, sentiment: 'neutral', unit: 'percent',
  });
  assert.ok(result.totals.every((total) => total.change !== null));
});
test('Campaign Portfolio decoder refuses a format-6 body or sibling with no change keys, and reads the current one', () => {
  for (const sort of [{ direction: 'desc', field: 'spend' }, { direction: 'asc', field: 'name' }]) {
    const body: Record<string, unknown> = { ...structuredClone(artifact()), sort };
    // Paired positive control: the current body decodes with a block on every comparison.
    const decoded = parseCampaignPortfolioArtifact(structuredClone(body));
    assert.deepEqual(decoded.sort, sort);
    assert.ok(decoded.totals.every((total) => total.change !== null));
    const comparisons = [...list(body.totals), ...list(record(list(body.rows)[0]).comparisons)].map(record);
    // Each fixture block's text is the percentage the pre-block body carried.
    for (const entry of comparisons) asRetained(entry, record(entry.change).display_text as string);
    assert.throws(() => parseCampaignPortfolioArtifact(body), CampaignAttributionContractError);
    // The decoded-and-re-encoded form (`change: null` beside the percentage) is refused too.
    for (const entry of comparisons) entry.change = null;
    assert.throws(() => parseCampaignPortfolioArtifact(body), CampaignAttributionContractError);
  }
});
test('Campaign Portfolio decoder refuses a format-7 body without the coverage key, and a complete body with a null block', () => {
  // Paired positive control: the current body decodes with its coverage block.
  assert.notEqual(parseCampaignPortfolioArtifact(artifact()).evidence_status, null);
  const blockless = structuredClone(artifact());
  delete blockless.evidence_status;
  assert.throws(() => parseCampaignPortfolioArtifact(blockless), CampaignAttributionContractError);
  assert.throws(() => parseCampaignPortfolioArtifact({ ...artifact(), evidence_status: null }), CampaignAttributionContractError);
  // An insufficient body carries `null` and never a block (ADR-119 decision 4).
  assert.equal(parseCampaignPortfolioArtifact(insufficientArtifact()).evidence_status, null);
});
test('Campaign Portfolio decoder refuses a block beside a percentage, a missing block, and a hostile tone', () => {
  const hostilePayloads: ReadonlyArray<(value: Record<string, unknown>) => void> = [
    (value) => { firstComparison(value).percentage_change = '-50.00%'; },
    (value) => { firstComparison(value).change = null; },
    (value) => { record(firstComparison(value).change).sentiment = 'good'; },
    (value) => { record(firstComparison(value).change).tone = 'bad'; },
    (value) => { record(firstComparison(value).change).display_text = ' '; },
    (value) => { firstComparison(value).change = { direction: 'flat', display_text: 'No change', not_meaningful: null, sentiment: 'favourable', unit: 'percent' }; },
  ];
  for (const mutate of hostilePayloads) {
    const value = structuredClone(artifact());
    mutate(value);
    assert.throws(() => parseCampaignPortfolioArtifact(value));
  }
});


test('A baked body names its roster shards and the bake revision instead of the roster (ADR-145)', () => {
  const value = artifact();
  value.roster = { revision: 'rev-fictional', shards: 1 };
  const result = parseCampaignPortfolioArtifact(value);
  assert.deepEqual(result.roster_shards, { revision: 'rev-fictional', shards: 1 });
  assert.deepEqual(result.roster, []);
  // Paired: a shard count that doesn't cover the population is refused.
  value.roster = { revision: 'rev-fictional', shards: 2 };
  assert.throws(() => parseCampaignPortfolioArtifact(value), CampaignAttributionContractError);
});

test('A roster shard is exactly the shard asked for, each entry at its own position', () => {
  const shard = {
    channels: ['Fictional channel'],
    entries: [['Fictional Campaign 0501', 0, 0, 500], ['Fictional Campaign 0502', 0, 0, 501]],
    kind: 'campaign_roster_shard',
    marketplaces: ['United States'],
    parent_revision: 'rev-fictional',
    shard: 1,
  };
  const parsed = parseCampaignRosterShard(shard, 1);
  assert.equal(parsed.parent_revision, 'rev-fictional');
  assert.deepEqual(parsed.entries.map((entry) => entry.position), [500, 501]);
  // Paired: another shard's index, or a position outside the shard, is refused.
  assert.throws(() => parseCampaignRosterShard(shard, 0), CampaignAttributionContractError);
  const misplaced = { ...shard, entries: [['Fictional Campaign 0001', 0, 0, 0]] };
  assert.throws(() => parseCampaignRosterShard(misplaced, 1), CampaignAttributionContractError);
});
