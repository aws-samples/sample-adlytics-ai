// SPDX-License-Identifier: MIT-0
/** Answer-sentence builders over the served fixtures (UI refresh 1.5): answer and fallback. */
import assert from 'node:assert/strict';
import { test } from 'vitest';
import { reachFigureText } from '../components/ReachAndFunnelView';
import { audiencesAnswer, parseAudienceProfileArtifact } from '../lib/audiences';
import { campaignsAnswer } from '../lib/campaign-attribution';
import { newCustomersAnswer } from '../lib/new-customers';
import { campaignPresentationV2, newCustomersPresentationV2, parsePresentationResultCalendarV2, parsePresentationResultV2, scorecardPresentationV2 } from '../lib/presentation-v2';
import { parseReachAndFunnelArtifact, reachAnswer } from '../lib/reach';
import { parseRetailHealthArtifact, retailHealthAnswer } from '../lib/retail-health';
import { parseRetentionCohortArtifact, retentionAnswer } from '../lib/retention';
import { scorecardAnswer } from '../lib/scorecard';
import audiencesServed from './fixtures/audiences-served-v2.json';
import campaignsAll from './fixtures/campaigns-all-v2.json';
import campaignsDsp from './fixtures/campaigns-dsp-v2.json';
import ncWeek from './fixtures/new-customers-week-v2.json';
import ncPending from './fixtures/new-customers-per-channel-pending-v2.json';
import ncRolling from './fixtures/new-customers-7d-v2.json';
import ncEmpty from './fixtures/new-customers-empty-v2.json';
import { reachCompleteFineFixture, reachNeedsDspFixture, reachQuarterFixture, reachEmptyFixture } from './fixtures/reach-v1-fixtures';
import retailHealth from './fixtures/retail-health-v2.json';
import { retentionFixture } from './fixtures/retention-v1-fixtures';
import scorecardWeekly from './fixtures/scorecard-7d-v2.json';
import scorecardNoCredit from './fixtures/scorecard-no-deduplicated-credit-v2.json';
import scorecardMonth from './fixtures/scorecard-month-v2.json';

const sc = (fixture: unknown) => { const p = scorecardPresentationV2(parsePresentationResultCalendarV2(fixture, 'scorecard')); if (p.kind !== 'parent') throw new Error('expected a parent'); return p.artifact; };
const nc = (fixture: unknown) => { const p = newCustomersPresentationV2(parsePresentationResultCalendarV2(fixture, 'new-customers')); if (p.kind !== 'parent') throw new Error('expected a parent'); return p.artifact; };
const cp = (fixture: unknown) => { const p = campaignPresentationV2(parsePresentationResultV2(fixture, 'campaigns')); if (p.kind !== 'parent') throw new Error('expected a parent'); return p.artifact; };
const served = (fixture: unknown) => (fixture as { page: { artifact: unknown } }).page.artifact;

test('Scorecard: the selected model\'s served return, with its prior only when one is served', () => {
  const weekly = sc(scorecardWeekly);
  const card = weekly.cards[0]!;
  assert.deepEqual(scorecardAnswer(weekly, card, card.headline.default_model), {
    kind: 'answer',
    text: 'Amazon Ads returned 3.96 USD in sales for every 1 USD spent in Sep 9–15, 2026, compared with 4.40 USD in Sep 2–8, 2026.',
  });
  // Another model reads its own served figure, never a recomputed one.
  const other = card.headline.models.find((model) => model.model !== card.headline.default_model)!;
  assert.match(scorecardAnswer(weekly, card, other.model).text, new RegExp(`returned ${other.figure.display_value!.replace('.', '\\.')} USD`));
  const month = sc(scorecardMonth);
  assert.equal(scorecardAnswer(month, month.cards[0]!, month.cards[0]!.headline.default_model).text, 'Amazon Ads returned 3.96 USD in sales for every 1 USD spent in Aug 1–31, 2026.');
  const noCredit = sc(scorecardNoCredit);
  assert.equal(scorecardAnswer(noCredit, noCredit.cards[0]!, noCredit.cards[0]!.headline.default_model).kind, 'answer');
  // Fallback: an absent headline reads its served absence text.
  const absent = { ...card, headline: { ...card.headline, absence_text: 'Fictional headline reason.', models: [] } };
  assert.deepEqual(scorecardAnswer(weekly, absent, card.headline.default_model), { kind: 'fallback', text: 'Fictional headline reason.' });
});

test('Campaigns: count and spend with its served change, or one channel\'s served return', () => {
  assert.deepEqual(campaignsAnswer(cp(campaignsAll), 'Absent.'), { kind: 'answer', text: '4 campaigns spent 2,870.00 USD in Aug 15–21, 2026, down 2.4% on Aug 8–14, 2026.' });
  assert.deepEqual(campaignsAnswer(cp(campaignsDsp), 'Absent.'), { kind: 'answer', text: 'Amazon DSP returned 2.67× on 2,100.00 USD in Aug 15–21, 2026.' });
  const empty = { ...cp(campaignsAll), outcome: 'empty' as const };
  assert.deepEqual(campaignsAnswer(empty, 'Fictional empty copy.'), { kind: 'fallback', text: 'Fictional empty copy.' });
});

test('New customers: new buyers and their cost, else the served line and per-channel purchases, never summed', () => {
  assert.deepEqual(newCustomersAnswer(nc(ncWeek)), { kind: 'answer', text: 'Your ads won 2,180 new customers in Sep 14–20, 2026, at 21.30 USD each.' });
  assert.equal(newCustomersAnswer(nc(ncPending))?.kind, 'answer');
  assert.deepEqual(newCustomersAnswer(nc(ncRolling)), {
    kind: 'fallback',
    text: 'New buyers are measured for complete weeks and months. Choose Latest measured week to see them. First-time purchases in Sep 14–20, 2026: Amazon DSP 610, Sponsored Products 1,420, Sponsored Brands 390, Sponsored Display 95.',
  });
  assert.equal(newCustomersAnswer(nc(ncEmpty))?.kind, 'fallback');
});

test('Reach: DSP-only shoppers and share, or the served reason the overlap is not available', () => {
  assert.deepEqual(reachAnswer(parseReachAndFunnelArtifact(reachCompleteFineFixture), reachFigureText), {
    kind: 'answer',
    text: "Amazon DSP reached 2000 served shoppers your sponsored ads didn't in September 2026: 20.00% of shoppers reached.",
  });
  assert.equal(reachAnswer(parseReachAndFunnelArtifact(reachNeedsDspFixture), reachFigureText).text, "Reach for September 2026 isn't available: Needs DSP and sponsored ads on one AMC instance");
  assert.equal(reachAnswer(parseReachAndFunnelArtifact(reachQuarterFixture), reachFigureText).text, "Reach for Q3 2026 isn't available: Overlap is measured for complete weeks and months. Choose Latest measured month to see it.");
  assert.deepEqual(reachAnswer(parseReachAndFunnelArtifact(reachEmptyFixture), reachFigureText), { kind: 'fallback', text: "Reach for September 2026 isn't available." });
});

test('Audiences: the server-ranked first segment when the over-index slot is filled, else the rule line', () => {
  const artifact = parseAudienceProfileArtifact(served(audiencesServed));
  assert.deepEqual(audiencesAnswer(artifact), { kind: 'fallback', text: 'No segment met the over-index rule in September 2026.' });
  const market = artifact.markets[0];
  const view = market.views.find((entry) => entry.view === market.default_view)!;
  const delivery = view.deliveries.find((entry) => view.rows.some((row) => row.profile_path === entry.profile_path && row.state === 'value'))!;
  const filled = { ...delivery, insights: delivery.insights.map((slot) => ({ ...slot, state: 'filled' as const, text: 'Fictional narration.' })) };
  const views = market.views.map((entry) => entry === view ? { ...entry, deliveries: entry.deliveries.map((item) => item === delivery ? filled : item) } : entry) as unknown as typeof market.views;
  const answer = audiencesAnswer({ ...artifact, markets: [{ ...market, views }] });
  const row = view.rows.find((entry) => entry.profile_path === delivery.profile_path && entry.state === 'value')!;
  assert.equal(answer.kind, 'answer');
  // T1L-6: the served segment name leads ("Demo - Age 25-34"), without the " · Amazon" suffix.
  assert.ok(row.name !== null && row.display_name !== row.name);
  assert.equal(answer.text, `Shoppers in ${row.name} are ${row.segment_buyer_share!.text} of your buyers but ${row.segment_exposed_share!.text} of shoppers reached: ${row.segment_index}× as common among your buyers.`);
});

test('Retention: the last offset all three served lines carry, else the served absence', () => {
  assert.deepEqual(retentionAnswer(parseRetentionCohortArtifact(retentionFixture())), {
    kind: 'answer',
    text: 'By Month 12, 20.0% of DSP-only customers bought again, against 20.0% sponsored-only and 20.0% both.',
  });
  assert.deepEqual(retentionAnswer(parseRetentionCohortArtifact(retentionFixture('empty'))), {
    kind: 'fallback',
    text: "Repeat purchase for September 2026 isn't measured yet: No cohorts were measured for this vintage.",
  });
});

test('Retail health: shipped revenue and its served change; empty reads the served absence copy', () => {
  const artifact = parseRetailHealthArtifact(served(retailHealth));
  assert.deepEqual(retailHealthAnswer(artifact, 'Absent.'), { kind: 'answer', text: 'Amazon shipped 30.00 USD in Aug 13–19, 2026, up 100.0% on Aug 6–12, 2026.' });
  // A withheld change drops the second clause.
  const withheld = { ...artifact, totals: artifact.totals.map((entry) => entry.metric === 'retail_revenue_shipped' ? { ...entry, change: null, withheld: true } : entry) };
  assert.deepEqual(retailHealthAnswer(withheld, 'Absent.'), { kind: 'answer', text: 'Amazon shipped 30.00 USD in Aug 13–19, 2026.' });
  assert.deepEqual(retailHealthAnswer({ ...artifact, outcome: 'empty' }, 'Fictional ABS-RH-1.'), { kind: 'fallback', text: 'Fictional ABS-RH-1.' });
});
