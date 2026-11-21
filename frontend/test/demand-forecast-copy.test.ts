// SPDX-License-Identifier: MIT-0
/**
 * The demand forecast page's pure builders (forecasting LLD "Demand forecast page"): answer
 * sentences, week runs, the generation-keyed search example, the freshness pop-up and the reason
 * copy. Each quotes served strings exactly and computes no figure. A wrong builder names a
 * product as leading on a filtered page, quotes another generation's ASIN, dates a next step
 * from a status that never loaded, or claims dates the inactivity rule doesn't guarantee.
 */
import assert from 'node:assert/strict';
import { test } from 'vitest';
import {
  asinErrorText,
  askQuestions,
  currentExampleAsin,
  detailAnswer,
  exampleAsinFrom,
  INFO,
  itemCountOf,
  listAnswer,
  parseDemandForecastDetail,
  parseDemandForecastList,
  parseDemandForecastStatus,
  reasonNextStep,
  reasonNextStepCopy,
  reasonSentence,
  skipReasons,
  staleCauseLine,
  staleShownLine,
  statusLines,
  weekRuns,
  weekStateCopy,
} from '../lib/demand-forecast';
import detailFixture from './fixtures/demand-forecast-detail.json';
import notForecastFixture from './fixtures/demand-forecast-detail-not-forecast.json';
import listFixture from './fixtures/demand-forecast-list.json';
import staleFixture from './fixtures/demand-forecast-status-stale.json';

const list = parseDemandForecastList(listFixture);
const detail = parseDemandForecastDetail(detailFixture);
const stale = parseDemandForecastStatus(staleFixture);

test('listAnswer names the rank-1 product on an unfiltered page 1 and quotes the chosen view', () => {
  assert.deepEqual(listAnswer(list, 13), {
    kind: 'answer',
    text: 'Fictional product 2 leads the 13-week forecast: customers are likely to order 260 units in the 13 weeks starting Nov 8, 2026.',
  });
  assert.equal(listAnswer(list, 4)?.text,
    'Fictional product 2 leads the 13-week forecast: customers are likely to order 80 units in the 4 weeks starting Nov 8, 2026.');
});

test('listAnswer hides on page 2 and when filtered, and falls back without a rank-1 total', () => {
  assert.equal(listAnswer(parseDemandForecastList({ ...structuredClone(listFixture), page: 2, page_count: 2, query: { status: null, page: 2 } }), 13), null);
  assert.equal(listAnswer(parseDemandForecastList({ ...structuredClone(listFixture), query: { status: 'shown', page: 1 } }), 13), null);
  const withheld = structuredClone(listFixture);
  withheld.entries = withheld.entries.slice(2);
  assert.deepEqual(listAnswer(parseDemandForecastList(withheld), 13), {
    kind: 'fallback', text: '3 products in this forecast for the 13 weeks starting Nov 8, 2026.',
  });
  const uncounted = { ...structuredClone(withheld), item_count: null };
  assert.equal(listAnswer(parseDemandForecastList(uncounted), 8)?.text, 'This forecast covers the 8 weeks starting Nov 8, 2026.');
});

test('a null title reads as its ASIN in the answer sentences (G-12)', () => {
  const untitled = structuredClone(listFixture);
  const first = untitled.entries[0];
  assert.ok(first);
  (first as { title: string | null }).title = null;
  assert.match(listAnswer(parseDemandForecastList(untitled), 13)?.text ?? '', /^B0FIXTUR02 leads the 13-week forecast/);
  const product = parseDemandForecastDetail({ ...structuredClone(detailFixture), title: null });
  assert.match(detailAnswer(product, 'units_ordered', 13)?.text ?? '', /units of B0FIXTUR01 in/);
});

test('detailAnswer has one template per measure and quotes the served total', () => {
  assert.equal(detailAnswer(detail, 'units_ordered', 13)?.text,
    'Customers are likely to order 137 units of Fictional product 1 in the 13 weeks starting Nov 8, 2026.');
  assert.equal(detailAnswer(detail, 'units_shipped', 4)?.text,
    'Amazon is likely to ship 42 units of Fictional product 1 in the 4 weeks starting Nov 8, 2026.');
  assert.equal(detailAnswer(detail, 'revenue_shipped', 13)?.text,
    'Shipped revenue for Fictional product 1 is likely to be 16,048.50 USD in the 13 weeks starting Nov 8, 2026.');
  assert.equal(detailAnswer(detail, 'cogs_shipped', 8)?.text,
    "Amazon's wholesale cost for shipped units of Fictional product 1 is likely to be 9,876.00 USD in the 8 weeks starting Nov 8, 2026.");
  assert.equal(detailAnswer(parseDemandForecastDetail(notForecastFixture), 'units_ordered', 13), null);
});

test('weekRuns groups consecutive weeks by their served state, in served order', () => {
  const states = structuredClone(notForecastFixture);
  states.recent_weeks.forEach((week, index) => {
    const state = index < 6 ? 'inferred_zero' : 'unknown';
    week.targets.units_ordered = { state, value: state === 'unknown' ? null : { value: '0.0000', display: '0' } } as never;
    week.stockout = null as never;
  });
  const runs = weekRuns(parseDemandForecastDetail(states).recent_weeks ?? [], 'units_ordered');
  assert.deepEqual(runs.map((run) => [run.range, weekStateCopy[run.state], run.weeks.length]), [
    ['Aug 9–Sep 13', 'No sales reported, counted as zero', 6],
    ['Sep 20–Nov 1', 'Not known', 7],
  ]);
  // Another measure keeps its own served states.
  assert.deepEqual(weekRuns(parseDemandForecastDetail(states).recent_weeks ?? [], 'units_shipped').map((run) => run.range), ['Aug 9–Nov 1']);
});

test('the reason copy quotes no unserved date, and every next step has a no-date form', () => {
  assert.doesNotMatch(reasonSentence('ASIN_INACTIVE', 'units_ordered'), /\d/);
  assert.equal(reasonSentence('FORECAST_NOT_PUBLISHABLE', 'units_ordered'), 'No forecast met every condition required to show it for this product.');
  assert.match(reasonSentence('CURRENCY_MIXED', 'revenue_shipped'), /This product's shipped revenue was reported/);
  for (const reason of skipReasons) {
    assert.doesNotMatch(reasonNextStepCopy[reason].undated, /\d|\{/, reason);
    assert.equal(reasonNextStep(reason, null), reasonNextStepCopy[reason].undated);
  }
  assert.equal(reasonNextStep('HOLDOUT_UNKNOWN', '2026-11-26T13:00:00Z'), 'The update on Nov 26, 2026 tries again with the newest weeks.');
  assert.equal(reasonNextStep('INSUFFICIENT_CONTEXT', null), 'A forecast becomes possible once it has enough complete weeks of sales.');
});

test('the search example comes only from an unfiltered page 1 and only for its own generation (G-08)', () => {
  const example = exampleAsinFrom(list);
  assert.deepEqual(example, { generationId: 'run-1-0123456789ab', asin: 'B0FIXTUR02' });
  assert.equal(currentExampleAsin(example, 'run-1-0123456789ab'), 'B0FIXTUR02');
  assert.equal(currentExampleAsin(example, 'run-2-0123456789ab'), null);
  assert.equal(currentExampleAsin(example, null), null);
  assert.equal(exampleAsinFrom(parseDemandForecastList({ ...structuredClone(listFixture), query: { status: 'shown', page: 1 } })), null);
  assert.equal(exampleAsinFrom(parseDemandForecastList({ ...structuredClone(listFixture), entries: [] })), null);
  assert.equal(asinErrorText('B0FIXTUR02'), 'An ASIN is 10 letters or digits, for example B0FIXTUR02.');
  assert.equal(asinErrorText(null), 'An ASIN is 10 letters or digits.');
  assert.equal(itemCountOf(list), 3);
  assert.equal(itemCountOf(null), null);
});

test('the freshness pop-up carries every status field, and its schedule waits for status', () => {
  assert.ok(list.provenance);
  const source = { provenance: list.provenance, freshness: list.freshness };
  assert.deepEqual(statusLines(source, stale), {
    made: 'Made Nov 8, 2026 (8 days ago) from Vendor Central sales through Nov 7, 2026.',
    schedule: [
      'Latest update, Nov 8, 2026: held by a quality check. A quality check held the run.',
      'Held by a quality check; the last good forecast is still shown.',
      'Next daily check: Nov 17, 2026, 13:00 UTC.',
      'Next check that can use new sales data: Nov 19, 2026, 13:00 UTC.',
    ],
    rule: "A forecast counts as up to date for 16 days after it's made, unless the latest update failed or was held.",
  });
  assert.equal(statusLines(source, null).schedule, null);
  const undone = parseDemandForecastStatus({
    ...structuredClone(staleFixture), hold: 'none', rollback_active: true,
    last_run: { ...structuredClone(staleFixture).last_run, outcome: 'rolled_back', failure_family: null, failure_category: null, at: '2026-11-19T10:00:00Z' },
  });
  assert.equal(statusLines(source, undone).schedule?.[0], "The latest update was undone on Nov 19, 2026, so you're seeing the forecast made Nov 8, 2026.");
  const today = { ...source, freshness: { ...source.freshness, age_days: 0 } };
  assert.match(statusLines(today, null).made, /\(today\)/);
  const unaged = { ...source, freshness: { ...source.freshness, age_days: null } };
  assert.equal(statusLines(unaged, null).made, 'Made Nov 8, 2026 from Vendor Central sales through Nov 7, 2026.');
});

test('the out-of-date banner has one line per served cause, with no-date forms', () => {
  const freshness = stale.freshness;
  assert.equal(staleCauseLine('age_over_limit', freshness, stale), 'It was made 17 days ago; forecasts count as up to date for 16 days.');
  assert.equal(staleCauseLine('last_run_held', freshness, stale), 'The weekly update on Nov 8, 2026 was held by a quality check.');
  assert.equal(staleCauseLine('last_run_held', freshness, null), 'The latest weekly update was held by a quality check.');
  const failed = parseDemandForecastStatus({
    ...structuredClone(staleFixture),
    last_run: { ...structuredClone(staleFixture).last_run, outcome: 'failed', failure_family: 'input_not_ready' },
  });
  assert.equal(staleCauseLine('last_run_failed', freshness, failed), "The weekly update on Nov 8, 2026 didn't finish: the week's sales data wasn't ready.");
  assert.ok(list.provenance);
  assert.equal(staleShownLine(list.provenance, stale),
    "You're seeing the forecast made Nov 8, 2026 from sales through Nov 7, 2026. The next update that can use new sales data is Nov 19, 2026, 13:00 UTC.");
  assert.equal(staleShownLine(list.provenance, null), "You're seeing the forecast made Nov 8, 2026 from sales through Nov 7, 2026.");
});

test('the range and testing pop-ups claim only percentiles and where actuals landed (G-11)', () => {
  assert.match(INFO.range.text, /10th, 50th and 90th percentile/);
  assert.match(INFO.range.text, /not a guarantee/);
  assert.doesNotMatch(INFO.range.text, /in 10|as likely/);
  const tested = INFO.tested(detail.targets?.units_ordered.accuracy ?? null, 'units_ordered').text;
  assert.match(tested, /In 9 of those 13 weeks, actual units ordered landed inside the range from low to high; in the other weeks it landed below or above it\./);
  assert.match(INFO.tested(null, 'units_ordered').text, /isn't measured/);
  assert.doesNotMatch(INFO.short.text, /safe to promote/i);
  // G-13: the supply-check framing, with no threshold and no classification of the product.
  assert.match(INFO.short.text, /before a promotion when deciding whether the product needs a supply check/);
  assert.doesNotMatch(INFO.short.text, /\d|many|more than/i);
});

test('the seeded questions name the ASIN and the served dates', () => {
  assert.ok(list.horizon);
  assert.equal(askQuestions.list(list.horizon), 'Which products have the highest forecast demand in the 13 weeks starting Nov 8, 2026?');
  assert.equal(askQuestions.range('B0FIXTUR01', 4, list.horizon), 'Explain the low, likely and high demand forecast for B0FIXTUR01 in the 4 weeks starting Nov 8, 2026.');
  assert.equal(askQuestions.why('B0FIXTUR03'), 'Why does B0FIXTUR03 have no demand forecast?');
});
