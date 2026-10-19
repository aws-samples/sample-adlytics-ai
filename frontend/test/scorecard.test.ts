// SPDX-License-Identifier: MIT-0
/**
 * The strict Scorecard decoder (UI slice 9a item A9). The fixtures are the server's own
 * responses (tests/unit/test_scorecard_presentation.py keeps them current); each negative
 * control is a hand-edit of an accepted fixture and is refused beside the original.
 */
import assert from 'node:assert/strict';
import { test } from 'vitest';

import { parsePresentationResultCalendarV2, parsePresentationResultV2, scorecardPresentationV2 } from '../lib/presentation-v2';
import { parseNetworkScorecardArtifact } from '../lib/scorecard';
import weekly from './fixtures/scorecard-7d-v2.json';
import absentCell from './fixtures/scorecard-absent-cell-v2.json';
import emptyBody from './fixtures/scorecard-empty-v2.json';
import monthly from './fixtures/scorecard-month-v2.json';
import noPeriod from './fixtures/scorecard-no-period-v2.json';
import pending from './fixtures/scorecard-pending-v2.json';
import twoMarketplaces from './fixtures/scorecard-two-marketplaces-v2.json';
import defaultBody from './fixtures/scorecard-default-v2.json';
import modelSensitive from './fixtures/scorecard-model-sensitive-v2.json';
import noDsp from './fixtures/scorecard-no-dsp-v2.json';
import needsVendor from './fixtures/scorecard-tacos-needs-vendor-v2.json';
import twoAlsoChanged from './fixtures/scorecard-two-marketplaces-also-changed-v2.json';

type Json = Record<string, unknown>;

function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

function artifactOf(fixture: unknown): Json {
  return clone((fixture as { page: { artifact: Json } }).page.artifact);
}

function cardOf(artifact: Json): Json {
  return (artifact.cards as Json[])[0] as Json;
}

function rowsOf(artifact: Json): Json[] {
  return (cardOf(artifact).channel_table as Json).rows as Json[];
}

test('the served 7-day and month responses decode and bind to their own control', () => {
  for (const fixture of [weekly, monthly]) {
    const presentation = scorecardPresentationV2(parsePresentationResultCalendarV2(fixture, 'scorecard'));
    assert.equal(presentation.kind, 'parent');
    if (presentation.kind !== 'parent') return;
    assert.equal(presentation.artifact.cards.length, 1);
    assert.equal(presentation.artifact.outcome, 'complete');
  }
  const week = scorecardPresentationV2(parsePresentationResultCalendarV2(weekly, 'scorecard'));
  assert.ok(week.kind === 'parent' && week.artifact.prior_period !== null);
  const month = scorecardPresentationV2(parsePresentationResultCalendarV2(monthly, 'scorecard'));
  assert.ok(month.kind === 'parent' && month.artifact.period_label === 'August 2026');
  // Paired: the version-1 parser refuses a page that declares calendar presets.
  assert.throws(() => parsePresentationResultV2(weekly, 'scorecard'));
});

test('an attributed total row or a channel-group subtotal row is refused', () => {
  const accepted = artifactOf(weekly);
  assert.doesNotThrow(() => parseNetworkScorecardArtifact(accepted));
  // SC-N1: an attributed total row beside the channel rows (spend = the network's spend).
  const total = artifactOf(weekly);
  const rows = rowsOf(total);
  const totalRow = clone(rows[0] as Json);
  totalRow.channel = 'all_channels';
  totalRow.channel_label = 'All channels';
  ((totalRow.figures as Json[])[0] as Json).value = ((cardOf(total).spend as Json).value as string);
  rows.push(totalRow);
  assert.throws(() => parseNetworkScorecardArtifact(total));
  // SC-N2: a channel-group subtotal row, planted in both tables so the join alone can't catch it.
  const subtotal = artifactOf(weekly);
  const group = clone(rowsOf(subtotal)[1] as Json);
  group.channel = 'sponsored_ads';
  group.channel_label = 'Sponsored ads';
  rowsOf(subtotal).push(group);
  const ranking = cardOf(subtotal).ranking as Json;
  const rankingRow = clone((ranking.rows as Json[])[1] as Json);
  rankingRow.channel = 'sponsored_ads';
  (ranking.rows as Json[]).push(rankingRow);
  assert.throws(() => parseNetworkScorecardArtifact(subtotal));
});

test('drift from the server contract is refused, never repaired', () => {
  const edits: ((artifact: Json) => void)[] = [
    // SC-N3: an unknown key.
    (artifact) => { artifact.attributed_total = null; },
    // SC-N4: a figure without its basis label.
    (artifact) => { ((rowsOf(artifact)[0] as Json).figures as Json[])[2] = { ...((rowsOf(artifact)[0] as Json).figures as Json[])[2] as Json, basis: '' }; },
    // SC-N5: a card figure in another currency.
    (artifact) => { (((rowsOf(artifact)[0] as Json).figures as Json[])[0] as Json).units = { currency_code: 'EUR', kind: 'currency' }; },
    // SC-N6: a ranking row whose spend differs from its channel row.
    (artifact) => { (((cardOf(artifact).ranking as Json).rows as Json[])[0] as Json).spend_display_value = '1.00'; },
    // A canonical selector key the browser must never receive.
    (artifact) => { (rowsOf(artifact)[0] as Json).channel_key = 'amazon_dsp'; },
    // A change chip with no prior to compare with.
    (artifact) => { (cardOf(artifact).spend as Json).prior_value = null; },
    // An empty Also changed section (it is skipped when empty, never served empty).
    (artifact) => { artifact.also_changed = { heading: 'Also changed', intro: 'x', rows: [] }; },
    // A calendar period with a comparison window.
    (artifact) => { artifact.period_label = 'September 2026'; },
    // An insight card naming a marketplace on a page whose retail rows name none.
    (artifact) => { ((artifact.insight_cards as Json[])[0] as Json).marketplace_text = 'US marketplace'; },
  ];
  for (const edit of edits) {
    const artifact = artifactOf(weekly);
    edit(artifact);
    assert.throws(() => parseNetworkScorecardArtifact(artifact), /invalid/, edit.toString());
  }
});

test('a body for another window than the control is refused', () => {
  const tampered = clone(weekly) as unknown as { page: { artifact: Json } };
  (tampered.page.artifact.period as Json).start = '2026-09-08';
  assert.throws(() => scorecardPresentationV2(parsePresentationResultCalendarV2(tampered, 'scorecard')));
});

test('every served absence state decodes: absent cell, empty, pending, no complete period', () => {
  const cell = scorecardPresentationV2(parsePresentationResultCalendarV2(absentCell, 'scorecard'));
  assert.ok(cell.kind === 'parent');
  const dsp = cell.artifact.cards[0]?.channel_table.rows.find((row) => row.channel === 'amazon_dsp');
  assert.deepEqual(dsp?.figures.map((figure) => figure.value === null), [false, false, true, true]);
  assert.equal(cell.artifact.not_available_text, 'Not available');
  const served = scorecardPresentationV2(parsePresentationResultCalendarV2(emptyBody, 'scorecard'));
  assert.ok(served.kind === 'parent' && served.artifact.outcome === 'empty' && served.artifact.cards.length === 0);
  // Paired (SC9A-GATE-R5-001): the page declares no `insufficient` outcome, because no input
  // bakes one, so the same served body under that outcome is refused.
  const undeclared = artifactOf(emptyBody);
  undeclared.outcome = 'insufficient';
  assert.throws(() => parseNetworkScorecardArtifact(undeclared), /invalid/);
  const waiting = scorecardPresentationV2(parsePresentationResultCalendarV2(pending, 'scorecard'));
  assert.ok(waiting.kind === 'window_unavailable');
  // Catalog ABS-SC-2 with ABS-ALT (`docs/ui/page-absence-copy.md`), exactly as the server served it.
  assert.equal(waiting.message, "The Scorecard for July 2026 isn't ready to show yet. It's built from your Amazon Ads data, plus Vendor Central sales where you have them. When it's ready, you'll see the return on ad spend by network and channel your data supports for July 2026. Other windows above are ready to view.");
  assert.equal(waiting.expected_after, null);
  const none = scorecardPresentationV2(parsePresentationResultCalendarV2(noPeriod, 'scorecard'));
  assert.ok(none.kind === 'window_unavailable' && none.expected_after === '2026-08-02');
  // Paired (temporal contract rules 3 and 4): a version-2 refusal with no server text, and a
  // date on a reason other than "no complete period", are refused, so no browser copy is needed.
  assert.throws(() => parsePresentationResultCalendarV2({ ...clone(pending), message: null }, 'scorecard'));
  assert.throws(() => parsePresentationResultCalendarV2({ ...clone(pending), expected_after: '2026-08-02' }, 'scorecard'));
  assert.throws(() => parsePresentationResultCalendarV2({ ...clone(noPeriod), expected_after: null }, 'scorecard'));
});

test('an absent figure must carry its reason, and a TACoS row its coverage slot', () => {
  const missingReason = artifactOf(absentCell);
  (((rowsOf(missingReason)[0] as Json).figures as Json[])[2] as Json).absence_text = null;
  assert.throws(() => parseNetworkScorecardArtifact(missingReason), /invalid/);
  const noSlot = artifactOf(weekly);
  delete ((noSlot.tacos as Json[])[0] as Json).coverage_slot;
  assert.throws(() => parseNetworkScorecardArtifact(noSlot), /invalid/);
  const otherSlot = artifactOf(weekly);
  ((otherSlot.tacos as Json[])[0] as Json).coverage_slot = 'headline:amazon-amazon_ads-amazon_us';
  assert.throws(() => parseNetworkScorecardArtifact(otherSlot), /invalid/);
  // Paired: both served bodies decode.
  assert.doesNotThrow(() => parseNetworkScorecardArtifact(artifactOf(absentCell)));
  assert.doesNotThrow(() => parseNetworkScorecardArtifact(artifactOf(weekly)));
});

test('a multiplier carries its two-place served display value, and a rate none (S26, G9A-4)', () => {
  const served = parseNetworkScorecardArtifact(artifactOf(weekly));
  const roas = served.cards[0]?.channel_table.rows[0]?.figures[2];
  assert.deepEqual([roas?.value, roas?.display_value], ['3.0000', '3.00']);
  assert.equal(served.cards[0]?.ranking.rows[0]?.values[0]?.display_value, '3.75');
  assert.equal(served.tacos[0]?.figure.display_value, null);
  const edits: ((artifact: Json) => void)[] = [
    // The exact value where the two-place display value belongs.
    (artifact) => { (((rowsOf(artifact)[0] as Json).figures as Json[])[2] as Json).display_value = '3.0000'; },
    // A multiplier with no display value, which the browser would then have to round.
    (artifact) => { (((rowsOf(artifact)[0] as Json).figures as Json[])[2] as Json).display_value = null; },
    // A ranking value with no display value, or one at four places.
    (artifact) => { ((((cardOf(artifact).ranking as Json).rows as Json[])[0] as Json).values as Json[])[0] = { ...((((cardOf(artifact).ranking as Json).rows as Json[])[0] as Json).values as Json[])[0] as Json, display_value: null }; },
    (artifact) => { ((((cardOf(artifact).ranking as Json).rows as Json[])[0] as Json).values as Json[])[0] = { ...((((cardOf(artifact).ranking as Json).rows as Json[])[0] as Json).values as Json[])[0] as Json, display_value: '3.7500' }; },
    // A rate with a display value: TACoS goes through the shared percent formatter.
    (artifact) => { (((artifact.tacos as Json[])[0] as Json).figure as Json).display_value = '0.05'; },
  ];
  for (const edit of edits) {
    const artifact = artifactOf(weekly);
    edit(artifact);
    assert.throws(() => parseNetworkScorecardArtifact(artifact), /invalid/, edit.toString());
  }
});

test('a ranking states a value and rank for every channel, or none at all (G9A-1, G9A-10)', () => {
  // The served zero-spend body: the ranking is insufficient and ranks nobody, never a 0x.
  const cell = parseNetworkScorecardArtifact(artifactOf(absentCell));
  const ranking = cell.cards[0]?.ranking;
  assert.equal(ranking?.outcome, 'insufficient');
  assert.ok(ranking?.rows.every((row) => row.values.every((value) => value.rank === null)));
  assert.ok(ranking?.rows.find((row) => row.channel === 'amazon_dsp')?.values.every((value) => value.value === null));
  const values = (artifact: Json) => ((((cardOf(artifact).ranking as Json).rows as Json[])[0] as Json).values as Json[]);
  const edits: ((artifact: Json) => void)[] = [
    // A stated ranking with a null value and no rank: a bare "Not available" with no reason.
    (artifact) => { values(artifact)[0] = { ...values(artifact)[0] as Json, display_value: null, rank: null, rank_text: null, value: null }; },
    // A stated ranking with a rank beside no value.
    (artifact) => { values(artifact)[0] = { ...values(artifact)[0] as Json, display_value: null, value: null }; },
  ];
  for (const edit of edits) {
    const artifact = artifactOf(weekly);
    edit(artifact);
    assert.throws(() => parseNetworkScorecardArtifact(artifact), /invalid/, edit.toString());
  }
  // An insufficient ranking that still ranks a channel is refused.
  const ranked = artifactOf(absentCell);
  const sp = (((cardOf(ranked).ranking as Json).rows as Json[])[1] as Json).values as Json[];
  sp[0] = { ...sp[0] as Json, rank: 1, rank_text: '1st' };
  assert.throws(() => parseNetworkScorecardArtifact(ranked), /invalid/);
  // A spend total across networks served as a typed absence would render as a blank.
  const blank = artifactOf(weekly);
  const row = (blank.cross_network_spend as Json[])[0] as Json;
  row.networks_loaded = ['Amazon', 'Fictional Network B'];
  row.text = null;
  row.reason = null;
  row.figure = { ...((cardOf(blank).spend as Json)), absence_text: 'x', change: null, display_value: null, format: null, prior_display_value: null, prior_label: null, prior_value: null, units: null, value: null };
  assert.throws(() => parseNetworkScorecardArtifact(blank), /invalid/);
});

test('two marketplaces decode as two cards and retail rows that each name their marketplace (S4, S14)', () => {
  const served = parseNetworkScorecardArtifact(artifactOf(twoMarketplaces));
  assert.deepEqual(served.cards.map((card) => [card.title, card.currency_code]), [
    ['Amazon · United Kingdom marketplace · GBP', 'GBP'],
    ['Amazon · US marketplace · USD', 'USD'],
  ]);
  assert.deepEqual(served.tacos.map((row) => row.marketplace_text), ['United Kingdom marketplace', 'US marketplace']);
  assert.deepEqual(served.cross_network_spend.map((row) => [row.row_id, row.marketplace_text]), [
    ['amazon_uk-GBP', 'United Kingdom marketplace · GBP'],
    ['amazon_us-USD', 'US marketplace · USD'],
  ]);
  // One marketplace names none, as the approved mockup draws the section.
  assert.deepEqual(parseNetworkScorecardArtifact(artifactOf(weekly)).tacos.map((row) => row.marketplace_text), [null]);
  // Each insight card names its marketplace and has its own name; none offers an unscoped step.
  assert.deepEqual(served.insight_cards.map((card) => [card.card_id, card.marketplace_text, card.next_step]), [
    ['amazon-amazon_ads-amazon_uk', 'United Kingdom marketplace', null],
    ['amazon-amazon_ads-amazon_us', 'US marketplace', null],
    ['amazon-amazon_ads-amazon_uk', 'United Kingdom marketplace', null],
  ]);
  assert.equal(new Set(served.insight_cards.map((card) => card.title)).size, served.insight_cards.length);
  // TACoS identity is the served coverage slot, not the rendered sentence (SC9A-GATE-004): a
  // second ecosystem's row in the same marketplace shares the sentence and still decodes.
  const secondEcosystem = artifactOf(twoMarketplaces);
  const tacosRows = secondEcosystem.tacos as Json[];
  tacosRows.push({ ...clone(tacosRows[0] as Json), coverage_slot: 'tacos:fictional_ecosystem:amazon_uk' });
  assert.equal(parseNetworkScorecardArtifact(secondEcosystem).tacos.length, 3);
  const edits: ((artifact: Json) => void)[] = [
    // One row named and the other not: identity is all or nothing.
    (artifact) => { ((artifact.tacos as Json[])[1] as Json).marketplace_text = null; },
    // Two TACoS rows with one coverage slot (merged).
    (artifact) => { ((artifact.tacos as Json[])[1] as Json).coverage_slot = 'tacos:amazon:amazon_uk'; },
    // Two spend rows with one identity.
    (artifact) => { ((artifact.cross_network_spend as Json[])[1] as Json).row_id = 'amazon_uk-GBP'; },
    // A channel table (its arrival tag) that doesn't name its marketplace beside rows that do.
    (artifact) => { (((artifact.cards as Json[])[0] as Json).channel_table as Json).marketplace_text = null; },
    // An insight card that doesn't name its marketplace beside retail rows that do.
    (artifact) => { ((artifact.insight_cards as Json[])[1] as Json).marketplace_text = null; },
    // Two insight cards with one name (the round-3 defect, SC9A-GATE-001).
    (artifact) => { ((artifact.insight_cards as Json[])[1] as Json).title = ((artifact.insight_cards as Json[])[0] as Json).title; },
    // A card that names its marketplace with a Campaigns step that can't be scoped to it.
    (artifact) => { ((artifact.insight_cards as Json[])[0] as Json).next_step = (artifactOf(weekly).insight_cards as Json[])[0]?.next_step; },
    // A GBP card's figure converted to USD.
    (artifact) => { ((((((artifact.cards as Json[])[0] as Json).channel_table as Json).rows as Json[])[0] as Json).figures as Json[])[0] = { ...(((((((artifact.cards as Json[])[0] as Json).channel_table as Json).rows as Json[])[0] as Json).figures as Json[])[0] as Json), units: { currency_code: 'USD', kind: 'currency' } }; },
  ];
  for (const edit of edits) {
    const artifact = artifactOf(twoMarketplaces);
    edit(artifact);
    assert.throws(() => parseNetworkScorecardArtifact(artifact), /invalid/, edit.toString());
  }
});

test('a version-2 preset renders the label the server serves (G9A-8)', () => {
  const relabelled = clone(weekly) as unknown as { temporal: { presets: Json[] } };
  (relabelled.temporal.presets[3] as Json).label = 'Last complete month';
  const control = parsePresentationResultCalendarV2(relabelled, 'scorecard');
  assert.equal(control.temporal.presets[3]?.label, 'Last complete month');
  // Paired: an empty label is still refused.
  (relabelled.temporal.presets[3] as Json).label = '';
  assert.throws(() => parsePresentationResultCalendarV2(relabelled, 'scorecard'));
});

test('every round-5 served state decodes: SC-1, model-sensitive, no DSP, no Vendor Central, two marketplaces with Also changed', () => {
  const governed = ['amazon_dsp', 'amazon_sponsored_products', 'amazon_sponsored_brands', 'amazon_sponsored_display'];
  const sc1 = parseNetworkScorecardArtifact(artifactOf(defaultBody));
  assert.deepEqual(sc1.cards[0]?.channel_table.rows.map((row) => row.channel), governed);
  assert.deepEqual(sc1.cards[0]?.ranking.rows.map((row) => row.channel), governed);
  assert.equal(sc1.also_changed?.rows.length, 1);
  assert.equal(sc1.insight_cards.length, 3);
  assert.equal(parseNetworkScorecardArtifact(artifactOf(modelSensitive)).cards[0]?.ranking.outcome, 'model_sensitive');
  assert.equal(parseNetworkScorecardArtifact(artifactOf(noDsp)).cards[0]?.dsp_absence_text, 'No DSP delivery in this window');
  assert.equal(parseNetworkScorecardArtifact(artifactOf(needsVendor)).tacos[0]?.figure.absence_text, 'Needs Vendor Central sales');
  const two = parseNetworkScorecardArtifact(artifactOf(twoAlsoChanged));
  assert.deepEqual(two.also_changed?.rows.map((row) => [row.label, row.marketplace_text, row.title]), [
    ['Amazon DSP ad spend', 'United Kingdom marketplace', 'Amazon DSP ad spend · United Kingdom marketplace'],
    ['Amazon DSP ad spend', 'US marketplace', 'Amazon DSP ad spend · US marketplace'],
  ]);
  // Paired (SC9A-GATE-001): two rows named alike, or one row unnamed beside the other, are refused.
  const alike = artifactOf(twoAlsoChanged);
  const rows = (alike.also_changed as Json).rows as Json[];
  (rows[1] as Json).title = (rows[0] as Json).title;
  assert.throws(() => parseNetworkScorecardArtifact(alike), /invalid/);
  const unnamed = artifactOf(twoAlsoChanged);
  (((unnamed.also_changed as Json).rows as Json[])[1] as Json).marketplace_text = null;
  assert.throws(() => parseNetworkScorecardArtifact(unnamed), /invalid/);
});

test('an insight slot that is not filled says why, and the cards agree with the slot (SC9A-GATE-R4-003)', () => {
  // Accepted: the served silent slot with its reason and no cards, and the served filled slot.
  assert.doesNotThrow(() => parseNetworkScorecardArtifact(artifactOf(emptyBody)));
  assert.doesNotThrow(() => parseNetworkScorecardArtifact(artifactOf(weekly)));
  const silentSlot = (artifactOf(emptyBody).insights as Json[])[0] as Json;
  const edits: ((artifact: Json) => void)[] = [
    // A silent slot with no reason, or a blank one: the quiet section would render empty.
    (artifact) => { ((artifact.insights as Json[])[0] as Json).absence_reason = null; },
    (artifact) => { ((artifact.insights as Json[])[0] as Json).absence_reason = '  '; },
    // An unavailable slot with no reason.
    (artifact) => { Object.assign((artifact.insights as Json[])[0] as Json, { absence_reason: null, state: 'unavailable' }); },
  ];
  for (const edit of edits) {
    const artifact = artifactOf(emptyBody);
    edit(artifact);
    assert.throws(() => parseNetworkScorecardArtifact(artifact), /invalid/, edit.toString());
  }
  // A filled slot with no cards, and a silent slot beside cards, disagree.
  const noCards = artifactOf(weekly);
  noCards.insight_cards = [];
  assert.throws(() => parseNetworkScorecardArtifact(noCards), /invalid/);
  const silentWithCards = artifactOf(weekly);
  silentWithCards.insights = [clone(silentSlot)];
  assert.throws(() => parseNetworkScorecardArtifact(silentWithCards), /invalid/);
  // A filled slot that also carries an absence reason.
  const both = artifactOf(weekly);
  Object.assign((both.insights as Json[])[0] as Json, { absence_class: 'evaluated-quiet', absence_reason: 'x' });
  assert.throws(() => parseNetworkScorecardArtifact(both), /invalid/);
});

test('the insight slot keeps its served stamps, and a blank stamp is refused (SC9A-GATE-R5-004)', () => {
  const served = (artifactOf(weekly).insights as Json[])[0] as Json;
  const decoded = parseNetworkScorecardArtifact(artifactOf(weekly)).insights;
  assert.equal(decoded.artifact_revision, served.artifact_revision);
  assert.equal(decoded.prompt_version, served.prompt_version);
  // Paired: a blank or missing stamp can't be decoded, so the dialog never carries a made-up one.
  for (const key of ['artifact_revision', 'prompt_version']) {
    for (const value of ['', '  ', null]) {
      const artifact = artifactOf(weekly);
      ((artifact.insights as Json[])[0] as Json)[key] = value;
      assert.throws(() => parseNetworkScorecardArtifact(artifact), /invalid/, `${key}=${String(value)}`);
    }
  }
});
