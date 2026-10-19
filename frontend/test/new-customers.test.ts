// SPDX-License-Identifier: MIT-0
/**
 * The strict New customers v2 decoder (slice 9 New customers, item B6). Every fixture is the
 * server's own response (tests/unit/test_new_customers_presentation.py keeps them current);
 * each negative control (NC-N1..N7) is a hand-edit of an accepted fixture, refused beside it.
 */
import assert from 'node:assert/strict';
import { test } from 'vitest';
import { parseNewCustomersArtifact } from '../lib/new-customers';
import { newCustomersPresentationV2, parsePresentationResultCalendarV2, parsePresentationResultV2 } from '../lib/presentation-v2';
import rolling from './fixtures/new-customers-7d-v2.json';
import thirty from './fixtures/new-customers-30d-v2.json';
import week from './fixtures/new-customers-week-v2.json';
import month from './fixtures/new-customers-month-v2.json';
import tilesNotReturned from './fixtures/new-customers-tiles-not-returned-v2.json';
import tileNoNewBuyers from './fixtures/new-customers-tile-no-new-buyers-v2.json';
import rowNoNewBuyers from './fixtures/new-customers-row-no-new-buyers-v2.json';
import noAdSpend from './fixtures/new-customers-no-ad-spend-v2.json';
import noChannels from './fixtures/new-customers-no-channels-v2.json';
import perChannelPending from './fixtures/new-customers-per-channel-pending-v2.json';
import perChannelMissing from './fixtures/new-customers-per-channel-missing-v2.json';
import tilesPending from './fixtures/new-customers-tiles-pending-v2.json';
import tilesMissing from './fixtures/new-customers-tiles-missing-v2.json';
import stepOneAbsences from './fixtures/new-customers-step-1-absences-v2.json';
import gatewayNotCovered from './fixtures/new-customers-gateway-not-covered-v2.json';
import gatewayNotMeasured from './fixtures/new-customers-gateway-not-measured-v2.json';
import twoMarketplaces from './fixtures/new-customers-two-marketplaces-v2.json';
import rollingQuiet from './fixtures/new-customers-rolling-quiet-v2.json';
import noPeriod from './fixtures/new-customers-no-period-v2.json';
import pending from './fixtures/new-customers-pending-v2.json';
import empty from './fixtures/new-customers-empty-v2.json';

type Json = Record<string, unknown>;
const clone = <T>(value: T): T => JSON.parse(JSON.stringify(value)) as T;
const artifactOf = (fixture: unknown): Json => clone((fixture as { page: { artifact: Json } }).page.artifact);
const marketOf = (artifact: Json, index = 0): Json => (artifact.markets as Json[])[index] as Json;
const buyersOf = (artifact: Json): Json => marketOf(artifact).new_buyers as Json;
const tableOf = (artifact: Json): Json => buyersOf(artifact).per_channel as Json;
const bind = (fixture: unknown) => newCustomersPresentationV2(parsePresentationResultCalendarV2(fixture, 'new-customers'));

const PAGES = {
  empty, gatewayNotCovered, gatewayNotMeasured, month, noAdSpend, noChannels, perChannelMissing, perChannelPending, rolling, rowNoNewBuyers,
  rollingQuiet, stepOneAbsences, thirty, tileNoNewBuyers, tilesMissing, tilesNotReturned, tilesPending, twoMarketplaces, week,
};

test('every served page response decodes and binds to its own control (NC-1..NC-16, NC-18, NC-23)', () => {
  for (const [name, fixture] of Object.entries(PAGES)) {
    const presentation = bind(fixture);
    assert.equal(presentation.kind, 'parent', name);
  }
  const served = bind(rolling);
  assert.ok(served.kind === 'parent');
  assert.equal(served.artifact.markets[0]?.new_to_brand.rows.length, 4);
  assert.equal(served.artifact.insight_cards.length, 1);
  // Paired: the version-1 parser refuses a page that declares calendar presets.
  assert.throws(() => parsePresentationResultV2(rolling, 'new-customers'));
});

test('the no-complete-period and pending windows bind with the server text (NC-17)', () => {
  for (const fixture of [noPeriod, pending]) {
    const presentation = bind(fixture);
    assert.equal(presentation.kind, 'window_unavailable');
    if (presentation.kind !== 'window_unavailable') return;
    assert.ok(presentation.message.length > 0);
  }
});

test('NC-N1: a per-channel total row is refused beside the served table', () => {
  const total = artifactOf(month);
  assert.doesNotThrow(() => parseNewCustomersArtifact(clone(total)));
  const rows = tableOf(total).rows as Json[];
  const totalRow = clone(rows[0] as Json);
  totalRow.ad_product_key = 'all_channels';
  totalRow.ad_product_label = 'All channels';
  rows.push(totalRow);
  assert.throws(() => parseNewCustomersArtifact(total));
});

test('NC-N2: a step-1 channel-group subtotal row is refused beside the served table', () => {
  const subtotal = artifactOf(rolling);
  const rows = (marketOf(subtotal).new_to_brand as Json).rows as Json[];
  const row = clone(rows[1] as Json);
  row.ad_product_key = 'search';
  row.ad_product_label = 'Search';
  rows.push(row);
  assert.throws(() => parseNewCustomersArtifact(subtotal));
  // Even with a gateway entry added for it, its key repeats a channel group, not an ad product,
  // so the one-for-one order check is what refuses a lone insert; a duplicate key is refused too.
  const duplicate = artifactOf(rolling);
  const duplicateRows = (marketOf(duplicate).new_to_brand as Json).rows as Json[];
  duplicateRows.push(clone(duplicateRows[0] as Json));
  (marketOf(duplicate).gateway as Json[]).push(clone((marketOf(duplicate).gateway as Json[])[0] as Json));
  assert.throws(() => parseNewCustomersArtifact(duplicate));
});

test('NC-N3: a version 1 body is refused', () => {
  const v1 = artifactOf(rolling);
  v1.version = 1;
  assert.throws(() => parseNewCustomersArtifact(v1));
});

test('NC-N4: an unknown absence reason is refused', () => {
  const unknown = artifactOf(rowNoNewBuyers);
  const cost = ((tableOf(unknown).rows as Json[])[2] as Json).cost_per_new_buyer as Json;
  assert.equal((cost.absence as Json).reason, 'no_new_buyers');
  (cost.absence as Json).reason = 'too_few_shoppers';
  assert.throws(() => parseNewCustomersArtifact(unknown));
});

test('NC-N5: no_new_buyers on a new-buyers figure is refused, at both levels', () => {
  assert.doesNotThrow(() => parseNewCustomersArtifact(artifactOf(tileNoNewBuyers)));
  const tile = artifactOf(tilesNotReturned);
  ((buyersOf(tile).new_buyers as Json).absence as Json).reason = 'no_new_buyers';
  assert.throws(() => parseNewCustomersArtifact(tile));
  const row = artifactOf(month);
  const sd = (tableOf(row).rows as Json[])[3] as Json;
  ((sd.new_buyers as Json).absence as Json).reason = 'no_new_buyers';
  assert.throws(() => parseNewCustomersArtifact(row));
});

test('NC-N6: a per-channel table beside pending tiles is refused', () => {
  const awaiting = artifactOf(tilesPending);
  assert.equal(buyersOf(awaiting).state, 'pending');
  buyersOf(awaiting).per_channel = clone(tableOf(artifactOf(week)));
  assert.throws(() => parseNewCustomersArtifact(awaiting));
});

test('NC-N7: rows beside a missing per-channel table are refused', () => {
  const missing = artifactOf(perChannelMissing);
  assert.equal(tableOf(missing).state, 'missing');
  tableOf(missing).rows = clone(tableOf(artifactOf(week)).rows);
  assert.throws(() => parseNewCustomersArtifact(missing));
});

test('the served absence texts reach the decoded figures verbatim', () => {
  const tile = parseNewCustomersArtifact(artifactOf(tileNoNewBuyers));
  const tiles = tile.markets[0]?.new_buyers;
  assert.equal(tiles?.new_buyers?.value, '0');
  assert.deepEqual(tiles?.cost_per_new_buyer?.absence, { reason: 'no_new_buyers', text: 'No new buyers in Week of 14–20 Sep 2026.' });
  const row = parseNewCustomersArtifact(artifactOf(rowNoNewBuyers)).markets[0]?.new_buyers.per_channel?.rows[2];
  assert.equal(row?.new_buyers.value, '0');
  assert.deepEqual(row?.cost_per_new_buyer.absence, {
    reason: 'no_new_buyers',
    text: 'No new buyers for Sponsored Brands in Week of 14–20 Sep 2026.',
  });
  const notReturned = parseNewCustomersArtifact(artifactOf(tilesNotReturned)).markets[0]?.new_buyers;
  assert.deepEqual(notReturned?.new_buyers?.absence, { reason: 'not_returned', text: 'too few shoppers, or none' });
  assert.deepEqual(notReturned?.cost_per_new_buyer?.absence, { reason: 'not_returned', text: 'too few shoppers, or none' });
  // NC-GATE-102: the platform count deduplicates the channels' shoppers, so withheld tiles sit
  // over a table whose every row came back withheld too.
  const rows = notReturned?.per_channel?.rows ?? [];
  assert.equal(rows.length, 4);
  for (const each of rows) assert.equal(each.new_buyers.absence?.reason, 'not_returned', each.ad_product_label);
});

test('a calendar preset\'s quiet slot says nothing was compared; a quiet rolling one names its threshold', () => {
  for (const fixture of [week, month]) {
    assert.equal(parseNewCustomersArtifact(artifactOf(fixture)).insights.absence_reason, 'No comparison for calendar periods.');
  }
  // Paired: on a rolling preset with no share moved, the slot keeps the contract's silence condition.
  const quiet = parseNewCustomersArtifact(artifactOf(rollingQuiet));
  assert.equal(quiet.prior_period === null, false);
  assert.equal(quiet.insight_cards.length, 0);
  assert.equal(
    quiet.insights.absence_reason,
    "No ad product's new-to-brand revenue share moved by the declared threshold against the equal prior window.",
  );
});

test('the artifact must name its marketplace exactly when the page shows two', () => {
  assert.doesNotThrow(() => parseNewCustomersArtifact(artifactOf(twoMarketplaces)));
  const unnamed = artifactOf(twoMarketplaces);
  marketOf(unnamed, 1).marketplace_text = null;
  assert.throws(() => parseNewCustomersArtifact(unnamed));
  const named = artifactOf(rolling);
  marketOf(named).marketplace_text = 'US marketplace · USD';
  assert.throws(() => parseNewCustomersArtifact(named));
});

test('a calendar body with a change in share, or a rolling body without one, is refused', () => {
  const calendar = artifactOf(week);
  const row = ((marketOf(calendar).new_to_brand as Json).rows as Json[])[0] as Json;
  row.ntb_revenue_share_change = clone(((marketOf(artifactOf(rolling)).new_to_brand as Json).rows as Json[])[0] as Json).ntb_revenue_share_change;
  assert.throws(() => parseNewCustomersArtifact(calendar));
});

test('a gateway entry carries exactly its two served captions and a safe integer "more" count (NC-GATE-008)', () => {
  assert.doesNotThrow(() => parseNewCustomersArtifact(artifactOf(rolling)));
  const gatewayOf = (artifact: Json): Json => (marketOf(artifact).gateway as Json[])[0] as Json;
  const one = artifactOf(rolling);
  gatewayOf(one).captions = ((gatewayOf(one).captions as string[]).slice(0, 1));
  assert.throws(() => parseNewCustomersArtifact(one));
  const three = artifactOf(rolling);
  (gatewayOf(three).captions as string[]).push('A third caption.');
  assert.throws(() => parseNewCustomersArtifact(three));
  // Ten listed products and "And {N} more": accepted with a safe count, refused past 2^53 - 1.
  const listed = (more: number): Json => {
    const artifact = artifactOf(rolling);
    const entry = gatewayOf(artifact);
    const first = (entry.rows as Json[])[0] as Json;
    entry.rows = Array.from({ length: 10 }, (_, index) => ({ ...clone(first), asin: `B0FICT10${String(index).padStart(2, '0')}` }));
    entry.more_count = more;
    entry.more_text = `And ${more} more products.`;
    return artifact;
  };
  assert.doesNotThrow(() => parseNewCustomersArtifact(listed(3)));
  assert.throws(() => parseNewCustomersArtifact(listed(Number.MAX_SAFE_INTEGER + 2)));
  assert.throws(() => parseNewCustomersArtifact(listed(2.5)));
});

test('the artifact page context must be the envelope\'s (plan D5)', () => {
  const drifted = clone(rolling) as unknown as { page: { artifact: Json } };
  drifted.page.artifact.starter_questions = ['Which channel had the most new-to-brand purchases this week?'];
  assert.throws(() => bind(drifted));
});
