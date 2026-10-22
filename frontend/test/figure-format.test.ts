// SPDX-License-Identifier: MIT-0
/** Display formatting by the dictionary's unit and ratio scale (plan D4). */
import assert from 'node:assert/strict';
import { test } from 'vitest';
import { displayFigure, formatMultiplier } from '../lib/exact-format';
import { metricDisplay } from '../lib/page-bindings';

test('a rate reads as a 2-place percentage, with the exact percentage kept for the disclosure', () => {
  assert.deepEqual(displayFigure('ctr', '0.078262885787', 'ratio', null), { exact: '7.8262885787%', text: '7.83%' });
  assert.deepEqual(displayFigure('viewable_rate', '0.636363636364', 'ratio', null), { exact: '63.6363636364%', text: '63.64%' });
  assert.deepEqual(displayFigure('ctr', '0.0004', 'ratio', null), { exact: '0.04%', text: '0.04%' });
  assert.deepEqual(displayFigure('ctr', '1', 'ratio', null), { exact: '100%', text: '100.00%' });
});

test('a multiplier reads as a 2-place times sign, rounded half up for display only', () => {
  assert.deepEqual(displayFigure('roas_attributed_14d', '2.2437', 'ratio', null), { exact: '2.2437×', text: '2.24×' });
  assert.equal(formatMultiplier('1.755', 2), '1.76×');
  assert.equal(formatMultiplier('3', 2), '3.00×');
  assert.equal(formatMultiplier('-0.004', 2), '0.00×');
  assert.equal(formatMultiplier('1234.5', 2), '1,234.50×');
});

test('a count is grouped and money shows the served display value with its ISO code', () => {
  assert.deepEqual(displayFigure('impressions', '438444', 'count', null), { exact: '438,444', text: '438,444' });
  assert.deepEqual(displayFigure('ad_spend', '240337.5612', 'currency', 'USD', '240337.56'), { exact: '240,337.5612 USD', text: '240,337.56 USD' });
});

test('an unknown metric, a unit mismatch or money without a code is refused, never guessed', () => {
  assert.throws(() => displayFigure('not_a_metric', '1', 'count', null), /No governed display rule/);
  assert.throws(() => displayFigure('ctr', '1', 'count', null), /served as count/);
  assert.throws(() => displayFigure('ad_spend', '1.00', 'currency', null), /without a currency code/);
});

test('the generated projection carries every dictionary ratio with its scale', () => {
  assert.deepEqual(metricDisplay.get('ctr'), { ratio_scale: 'rate', units: 'ratio' });
  assert.deepEqual(metricDisplay.get('segment_index'), { ratio_scale: 'multiplier', units: 'ratio' });
  assert.deepEqual(metricDisplay.get('ad_spend'), { ratio_scale: null, units: 'currency' });
});
