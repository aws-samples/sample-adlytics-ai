// SPDX-License-Identifier: MIT-0
/** Strict Reach v1 decoder contract tests. Every fixture value is fictional. */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { test } from 'vitest';

import { parseReachAndFunnelArtifact, ReachDecodeError } from '../lib/reach';
import {
  cloneReachFixture,
  reachChangeUnavailableFixture,
  reachCompleteCoarseFixture,
  reachCompleteFineFixture,
  reachCurrencyMismatchFixture,
  reachEmptyFixture,
  reachInsufficientFixture,
  reachNeedsCampaignsFixture,
  reachNeedsDspFixture,
  reachNoArtifactMissingEnvelope,
  reachNoArtifactPendingEnvelope,
  reachNoPriorFixture,
  reachNoSalesFixture,
  reachNoShoppersFixture,
  reachNoSpendFixture,
  reachNotReturnedFixture,
  reachPackVariantMismatchFixture,
  reachPendingFixture,
  reachQuarterFixture,
  reachWeekFixture,
} from './fixtures/reach-v1-fixtures';

type Json = Record<string, unknown>;

const marketOf = (artifact: Json): Json => (artifact.markets as Json[])[0] as Json;
const overlapOf = (artifact: Json): Json => marketOf(artifact).overlap as Json;
const funnelOf = (artifact: Json): Json => marketOf(artifact).funnel as Json;
const frequencyOf = (artifact: Json): Json => marketOf(artifact).frequency as Json;
const firstBucketOf = (artifact: Json): Json => (frequencyOf(artifact).buckets as Json[])[0] as Json;
const firstChannelOf = (artifact: Json): Json => ((frequencyOf(artifact).per_channel as Json).rows as Json[])[0] as Json;

interface EncodedNoPriorFixture {
  readonly grain: 'month' | 'week';
  readonly group_changes: Json[];
  readonly tile_changes: Json[];
}

const ENCODED_NO_PRIOR = JSON.parse(
  readFileSync(join(process.cwd(), 'test/fixtures/reach-no-prior-encoder-fixtures.json'), 'utf8'),
) as Record<'month' | 'week', EncodedNoPriorFixture>;

function noPriorArtifact(
  source: Json,
  encoded: EncodedNoPriorFixture,
): Json {
  const artifact = cloneReachFixture(source);
  const overlap = overlapOf(artifact);
  for (const [index, tile] of (overlap.tiles as Json[]).entries()) {
    tile.prior = null;
    tile.change = cloneReachFixture(encoded.tile_changes[index]);
  }
  for (const [index, group] of (overlap.groups as Json[]).entries()) {
    group.prior_account = null;
    group.change = cloneReachFixture(encoded.group_changes[index]);
  }
  return artifact;
}

const VALID_ARTIFACTS = {
  changeUnavailable: reachChangeUnavailableFixture,
  coarse: reachCompleteCoarseFixture,
  currencyMismatch: reachCurrencyMismatchFixture,
  empty: reachEmptyFixture,
  fine: reachCompleteFineFixture,
  insufficient: reachInsufficientFixture,
  needsCampaigns: reachNeedsCampaignsFixture,
  needsDsp: reachNeedsDspFixture,
  noPrior: reachNoPriorFixture,
  noSales: reachNoSalesFixture,
  noShoppers: reachNoShoppersFixture,
  noSpend: reachNoSpendFixture,
  notReturned: reachNotReturnedFixture,
  packVariantMismatch: reachPackVariantMismatchFixture,
  pending: reachPendingFixture,
  quarter: reachQuarterFixture,
  week: reachWeekFixture,
};

test('the exhaustive fictional state matrix decodes', () => {
  for (const [name, fixture] of Object.entries(VALID_ARTIFACTS)) {
    assert.doesNotThrow(() => parseReachAndFunnelArtifact(cloneReachFixture(fixture)), name);
  }
  assert.equal(parseReachAndFunnelArtifact(reachCompleteFineFixture).markets[0]?.frequency.bucket_scheme, 'fine');
  assert.equal(parseReachAndFunnelArtifact(reachCompleteCoarseFixture).markets[0]?.frequency.bucket_scheme, 'coarse');
  assert.equal(parseReachAndFunnelArtifact(reachWeekFixture).period.grain, 'week');
  assert.equal(parseReachAndFunnelArtifact(reachQuarterFixture).period.grain, 'quarter');
});

test('server-encoded no-prior changes decode for week and month and unknown copy stays closed', () => {
  const cases = [
    [reachWeekFixture, ENCODED_NO_PRIOR.week],
    [reachCompleteFineFixture, ENCODED_NO_PRIOR.month],
  ] as const;
  for (const [source, encoded] of cases) {
    const artifact = noPriorArtifact(source, encoded);
    assert.equal((artifact.period as Json).grain, encoded.grain);
    const decoded = parseReachAndFunnelArtifact(artifact);
    assert.equal(
      decoded.markets[0]?.overlap?.tiles[0]?.change.absence?.text,
      `The previous ${encoded.grain} isn't available.`,
    );

    const unknownCopy = cloneReachFixture(artifact);
    const change = (((overlapOf(unknownCopy).tiles as Json[])[0] as Json).change as Json);
    (change.absence as Json).text = 'Unknown no-prior copy.';
    assert.throws(() => parseReachAndFunnelArtifact(unknownCopy), /invalid/);
  }
});

test('all nine absence reasons retain their exact server text', () => {
  const noShoppers = parseReachAndFunnelArtifact(reachNoShoppersFixture).markets[0];
  assert.deepEqual(noShoppers?.overlap?.tiles[0]?.current.absence, {
    reason: 'no_shoppers', text: 'No shoppers were reached in this group.',
  });
  assert.deepEqual(noShoppers?.overlap?.tiles[0]?.change.absence, {
    reason: 'change_unavailable', text: "No change is shown, because a figure isn't available.",
  });
  const noSales = parseReachAndFunnelArtifact(reachNoSalesFixture).markets[0]?.funnel?.rows[0]?.figures.revenue_per_exposed_shopper;
  assert.deepEqual(noSales?.absence, { reason: 'no_sales', text: 'No sales were recorded for this group.' });
  const noPrior = parseReachAndFunnelArtifact(reachNoPriorFixture).markets[0]?.overlap?.tiles[0]?.change;
  assert.deepEqual(noPrior?.absence, { reason: 'prior_unavailable', text: "The previous month isn't available." });
  const mismatch = parseReachAndFunnelArtifact(reachPackVariantMismatchFixture).markets[0]?.overlap?.tiles[0]?.change;
  assert.deepEqual(mismatch?.absence, {
    reason: 'pack_variant_mismatch', text: "No change is shown, because a figure isn't available.",
  });
  const bucketGap = parseReachAndFunnelArtifact(reachNotReturnedFixture).markets[0]?.frequency.buckets[3];
  assert.deepEqual(bucketGap?.purchaser_rate.absence, { reason: 'not_returned', text: 'too few shoppers, or none' });
  const noSpend = parseReachAndFunnelArtifact(reachNoSpendFixture).markets[0]?.frequency.buckets[0]?.platform_return;
  assert.deepEqual(noSpend?.absence, { reason: 'no_spend', text: 'No spend was recorded for these shoppers.' });
  const currency = parseReachAndFunnelArtifact(reachCurrencyMismatchFixture).markets[0]?.frequency.buckets[0]?.platform_return;
  assert.deepEqual(currency?.absence, {
    reason: 'currency_mismatch', text: "Spend and sales are in different currencies, so return isn't shown.",
  });
  const channel = parseReachAndFunnelArtifact(reachCompleteFineFixture).markets[0]?.frequency.per_channel?.rows[0]?.cells['1'];
  assert.deepEqual(channel?.absence, { reason: 'not_available_per_channel', text: 'Not available per channel' });
});

test('section, caveat, boundary, context and no-artifact copy stays exact', () => {
  const complete = parseReachAndFunnelArtifact(reachCompleteFineFixture);
  assert.equal(complete.markets[0]?.overlap?.tiles[0]?.label, "Saw both within 14 days of their first ad · Amazon account IDs · a lower bound: DSP impressions Amazon couldn't link to an account aren't matched");
  assert.deepEqual(complete.markets[0]?.funnel?.caveat, [
    'This shows association, not cause. Shoppers were not randomly assigned to groups.',
    'Shoppers can see an ad after they buy, so groups that include DSP retargeting may look stronger.',
    'Sponsored ads appear when shoppers search, so sponsored groups start closer to buying.',
  ]);
  assert.equal(complete.markets[0]?.frequency.caveat[3], 'Shoppers can see more ads after they buy, so higher buckets may look stronger.');
  assert.equal(complete.markets[0]?.frequency.per_channel?.foot, "Attributed sales aren't added across channels, because one purchase can be credited to more than one channel.");
  assert.deepEqual(complete.boundary, [
    'Figures are for complete weeks, months or quarters only.',
    'Rates read Amazon account IDs only.',
    'This shows association, not cause.',
  ]);
  assert.equal(complete.page_context_label, 'Reach and funnel · September 2026');
  assert.deepEqual(complete.starter_questions, [
    "Which exposure group's purchaser rate was highest in September 2026, and what is it associated with?",
  ]);
  const pendingPreset = (((reachNoArtifactPendingEnvelope.temporal as Json).presets as Json[])[1] as Json);
  assert.equal(pendingPreset.reason_text, "September 2026 isn't measured yet");
  assert.equal(reachNoArtifactPendingEnvelope.message, 'Figures for September 2026 are expected after 16 Oct 2026.');
  const missingPreset = (((reachNoArtifactMissingEnvelope.temporal as Json).presets as Json[])[1] as Json);
  assert.equal(missingPreset.reason_text, "September 2026 didn't arrive");
  assert.equal(reachNoArtifactMissingEnvelope.message, "Figures for September 2026 didn't arrive. The latest available is August 2026.");
});

test('the overlap column scope and metric tile labels decode as distinct whole values', () => {
  const decoded = parseReachAndFunnelArtifact(reachCompleteFineFixture);
  assert.equal(decoded.markets[0]?.overlap?.all_ids_label, 'All ID types');
  assert.equal(
    decoded.markets[0]?.overlap?.tiles[1]?.label,
    'Saw both within 14 days of their first ad · all ID types · overlap is understated',
  );

  const missing = cloneReachFixture(reachCompleteFineFixture);
  delete overlapOf(missing).all_ids_label;
  assert.throws(() => parseReachAndFunnelArtifact(missing), /invalid/);

  const wrongType = cloneReachFixture(reachCompleteFineFixture);
  overlapOf(wrongType).all_ids_label = { text: 'All ID types' };
  assert.throws(() => parseReachAndFunnelArtifact(wrongType), /invalid/);
});

test('exact decimals are preserved but never turned into display text', () => {
  const decoded = parseReachAndFunnelArtifact(reachCompleteFineFixture);
  const first = decoded.markets[0]?.frequency.buckets[0];
  assert.equal(first?.purchaser_rate.value, '0.9876');
  assert.equal(first?.purchaser_rate.display_text, '4.00%');
  assert.equal(first?.platform_return.value, '9.8765');
  assert.equal(first?.platform_return.display_text, '2.50x');
  const source = readFileSync(join(process.cwd(), 'lib/reach.ts'), 'utf8');
  assert.doesNotMatch(source, /from ['"].*(?:display-value|format|formatter)/);
  assert.doesNotMatch(source, /Intl\.NumberFormat|toLocaleString|toFixed\(/);
});

test('unknown, extra and value-or-absence fields fail closed', () => {
  const edits: ((artifact: Json) => void)[] = [
    (artifact) => { artifact.total = null; },
    (artifact) => { overlapOf(artifact).percentage = '30%'; },
    (artifact) => { firstBucketOf(artifact).subtotal = null; },
    (artifact) => {
      firstBucketOf(artifact).purchaser_rate = {
        ...(firstBucketOf(artifact).purchaser_rate as Json),
        absence: { reason: 'not_returned', text: 'too few shoppers, or none' },
      };
    },
    (artifact) => { (firstBucketOf(artifact).purchaser_rate as Json).unit = 'percent'; },
    (artifact) => { (firstBucketOf(artifact).purchaser_rate as Json).currency_code = 'USD'; },
  ];
  for (const edit of edits) {
    const artifact = cloneReachFixture(reachCompleteFineFixture);
    edit(artifact);
    assert.throws(() => parseReachAndFunnelArtifact(artifact), /invalid/, edit.toString());
  }
});

test('preset, period and illegal section-state combinations fail closed', () => {
  const edits: ((artifact: Json) => void)[] = [
    (artifact) => { artifact.preset = 'last_complete_week'; },
    (artifact) => { (artifact.period as Json).grain = 'week'; },
    (artifact) => { (artifact.period as Json).end = '2026-02-30'; },
    (artifact) => { frequencyOf(artifact).state = 'outside_declared_periods'; },
    (artifact) => { frequencyOf(artifact).text = 'Frequency is measured for complete months and quarters. Choose Latest measured month to see it.'; },
    (artifact) => { overlapOf(artifact).state = 'outside_declared_periods'; },
    (artifact) => { overlapOf(artifact).text = 'Overlap is measured for complete weeks and months. Choose Latest measured month to see it.'; },
    (artifact) => { (frequencyOf(artifact).expected_after as unknown) = '2026-10-16'; },
  ];
  for (const edit of edits) {
    const artifact = cloneReachFixture(reachCompleteFineFixture);
    edit(artifact);
    assert.throws(() => parseReachAndFunnelArtifact(artifact), /invalid/, edit.toString());
  }
});

test('ordered overlap, funnel, bucket and insight rosters reject reordering', () => {
  const edits: ((artifact: Json) => void)[] = [
    (artifact) => { (overlapOf(artifact).tiles as Json[]).reverse(); },
    (artifact) => { (overlapOf(artifact).groups as Json[]).reverse(); },
    (artifact) => { (funnelOf(artifact).columns as Json[]).reverse(); },
    (artifact) => { (funnelOf(artifact).rows as Json[]).reverse(); },
    (artifact) => { (frequencyOf(artifact).buckets as Json[]).reverse(); },
    (artifact) => { (marketOf(artifact).insights as Json[]).reverse(); },
  ];
  for (const edit of edits) {
    const artifact = cloneReachFixture(reachCompleteFineFixture);
    edit(artifact);
    assert.throws(() => parseReachAndFunnelArtifact(artifact), /invalid/, edit.toString());
  }
});

test('per-channel cells exactly match the active scheme and totals are refused', () => {
  const accepted = parseReachAndFunnelArtifact(reachCompleteCoarseFixture);
  assert.deepEqual(Object.keys(accepted.markets[0]?.frequency.per_channel?.rows[0]?.cells ?? {}), [
    '1', '2', '3', '4_6', '7_9', '10_plus',
  ]);
  const edits: ((artifact: Json) => void)[] = [
    (artifact) => { (firstChannelOf(artifact).cells as Json).extra = { absence: { reason: 'not_available_per_channel', text: 'Not available per channel' } }; },
    (artifact) => { delete (firstChannelOf(artifact).cells as Json)['2']; },
    (artifact) => {
      const table = frequencyOf(artifact).per_channel as Json;
      const total = cloneReachFixture((table.rows as Json[])[0] as Json);
      total.ad_product_key = 'all_channels';
      total.label = 'All channels';
      (table.rows as Json[]).push(total);
    },
    (artifact) => {
      const table = frequencyOf(artifact).per_channel as Json;
      (table.rows as Json[]).push(cloneReachFixture((table.rows as Json[])[0] as Json));
    },
    (artifact) => {
      const cell = (firstChannelOf(artifact).cells as Json)['1'] as Json;
      (cell.absence as Json).reason = 'no_spend';
      (cell.absence as Json).text = 'No spend was recorded for these shoppers.';
    },
  ];
  for (const edit of edits) {
    const artifact = cloneReachFixture(reachCompleteFineFixture);
    edit(artifact);
    assert.throws(() => parseReachAndFunnelArtifact(artifact), /invalid/, edit.toString());
  }
});

test('change blocks agree with current and prior availability', () => {
  const edits: ((artifact: Json) => void)[] = [
    (artifact) => {
      const tile = (overlapOf(artifact).tiles as Json[])[0] as Json;
      tile.prior = null;
    },
    (artifact) => {
      const tile = (overlapOf(artifact).tiles as Json[])[0] as Json;
      tile.change = { absence: { reason: 'change_unavailable', text: "No change is shown, because a figure isn't available." } };
    },
    (artifact) => {
      const tile = (overlapOf(artifact).tiles as Json[])[0] as Json;
      tile.prior = { absence: { reason: 'not_returned', text: 'too few shoppers, or none' } };
    },
    (artifact) => {
      const tile = (overlapOf(artifact).tiles as Json[])[0] as Json;
      tile.prior = null;
      tile.change = { absence: { reason: 'prior_unavailable', text: "The previous week isn't available." } };
    },
  ];
  for (const edit of edits) {
    const artifact = cloneReachFixture(reachCompleteFineFixture);
    edit(artifact);
    assert.throws(() => parseReachAndFunnelArtifact(artifact), /invalid/, edit.toString());
  }
});

test('pending evidence slots exactly match pending markets, expected dates and the artifact period', () => {
  const pending = parseReachAndFunnelArtifact(reachPendingFixture);
  assert.deepEqual(pending.evidence_status.slots[0]?.missing_dates, [{ start: '2026-09-01', end: '2026-09-30' }]);
  const edits: ((artifact: Json) => void)[] = [
    (artifact) => { frequencyOf(artifact).text = 'Figures for September 2026 are expected after 17 Oct 2026.'; },
    (artifact) => { (artifact.evidence_status as Json).slots = []; },
    (artifact) => { (((artifact.evidence_status as Json).slots as Json[])[0] as Json).marketplace_key = 'another_market'; },
    (artifact) => { (((((artifact.evidence_status as Json).slots as Json[])[0] as Json).missing_dates as Json[])[0] as Json).start = '2026-09-02'; },
    (artifact) => { (((artifact.evidence_status as Json).slots as Json[])[0] as Json).reason_text = 'Expected later.'; },
  ];
  for (const edit of edits) {
    const artifact = cloneReachFixture(reachPendingFixture);
    edit(artifact);
    assert.throws(() => parseReachAndFunnelArtifact(artifact), /invalid/, edit.toString());
  }
});

test('multi-market and page-context drift fail closed', () => {
  const multiMarket = cloneReachFixture(reachCompleteFineFixture);
  const second = cloneReachFixture((multiMarket.markets as Json[])[0] as Json);
  second.marketplace_key = 'second_fictional_market';
  (multiMarket.markets as Json[]).push(second);
  assert.throws(() => parseReachAndFunnelArtifact(multiMarket), /invalid/);

  const edits: ((artifact: Json) => void)[] = [
    (artifact) => { artifact.page_context_label = 'Reach and funnel'; },
    (artifact) => { artifact.page_context_expires_at = '2026-10-01T00:00:00Z'; },
    (artifact) => { artifact.starter_questions = ['Did DSP drive more purchases?']; },
    (artifact) => { artifact.boundary = (artifact.boundary as string[]).slice(0, 2); },
    (artifact) => { (artifact.evidence as Json).fence_check = 'passed'; },
  ];
  for (const edit of edits) {
    const artifact = cloneReachFixture(reachCompleteFineFixture);
    edit(artifact);
    assert.throws(() => parseReachAndFunnelArtifact(artifact), /invalid/, edit.toString());
  }
});
// The bake's own sorted-key bytes (`canonical_preset_artifact_json`) for a coarse month with
// per-channel rows; `tests/unit/test_reach.py` pins this file to the serializer's output and
// decodes it with the server decoder, so both sides face the same body.
const COARSE_WIRE = JSON.parse(
  readFileSync(join(process.cwd(), 'test/fixtures/reach-month-coarse-wire.json'), 'utf8'),
) as Json;
test("the bake serializer's sorted-key coarse body decodes with cells in roster order", () => {
  const raw = cloneReachFixture(COARSE_WIRE);
  // JSON.parse lists integer keys first, so the wire order is not the roster order.
  assert.deepEqual(Object.keys(firstChannelOf(raw).cells as Json), ['1', '2', '3', '10_plus', '4_6', '7_9']);
  const decoded = parseReachAndFunnelArtifact(cloneReachFixture(raw));
  const frequency = decoded.markets[0]?.frequency;
  assert.equal(frequency?.bucket_scheme, 'coarse');
  const rows = frequency?.per_channel?.rows ?? [];
  const wireRows = (frequencyOf(raw).per_channel as Json).rows as Json[];
  assert.deepEqual(wireRows.map((row) => row.ad_product_key), ['sponsored_products', 'amazon_dsp']);
  assert.deepEqual(rows.map((row) => row.ad_product_key), ['sponsored_products', 'amazon_dsp']);
  for (const row of rows) assert.deepEqual(Object.keys(row.cells), ['1', '2', '3', '4_6', '7_9', '10_plus']);
});
test('coarse per-channel cells refuse a missing, an extra or a renamed bucket', () => {
  assert.doesNotThrow(() => parseReachAndFunnelArtifact(cloneReachFixture(COARSE_WIRE)));
  const edits: ((cells: Json) => void)[] = [
    (cells) => { delete cells['7_9']; },
    (cells) => { cells.extra = cloneReachFixture(cells['1']); },
    (cells) => { cells['4_to_6'] = cells['4_6']; delete cells['4_6']; },
  ];
  for (const edit of edits) {
    const artifact = cloneReachFixture(COARSE_WIRE);
    edit(firstChannelOf(artifact).cells as Json);
    assert.throws(
      () => parseReachAndFunnelArtifact(artifact),
      (error: unknown) => error instanceof ReachDecodeError && error.message === 'The Reach and funnel artifact is invalid.',
      edit.toString(),
    );
  }
});
// `record` checks a key set by length plus one-way containment, which equals set equality only
// while the expected keys are distinct. The per-channel cells' expected keys are the bucket
// rosters, which the decoded bucket lists mirror one for one, so pin both rosters distinct.
test('both frequency bucket rosters are distinct, so the per-channel key-set check is exact', () => {
  const rosters = {
    coarse: ['1', '2', '3', '4_6', '7_9', '10_plus'],
    fine: ['1', '2', '3', '4', '5', '6', '7', '8', '9', '10_plus'],
  } as const;
  for (const [scheme, fixture] of [['coarse', COARSE_WIRE], ['fine', reachCompleteFineFixture]] as const) {
    const buckets = (parseReachAndFunnelArtifact(cloneReachFixture(fixture)).markets[0]?.frequency.buckets ?? [])
      .map((bucket) => bucket.bucket);
    assert.deepEqual(buckets, rosters[scheme], scheme);
    assert.equal(new Set(buckets).size, buckets.length, scheme);
  }
});
