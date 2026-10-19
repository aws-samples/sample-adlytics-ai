// SPDX-License-Identifier: MIT-0
import assert from 'node:assert/strict';
import { cleanup, render } from '@testing-library/react';
import axeCore from 'axe-core';
import { afterEach, test } from 'vitest';

import { ChangeChip } from '../components/ChartPrimitives';
import { type Change, parseChange } from '../lib/change';

afterEach(cleanup);

function chip(change: Change | null): HTMLElement {
  const view = render(<ChangeChip change={change} />);
  return view.container.firstElementChild as HTMLElement;
}
function spoken(element: HTMLElement): string | null {
  return element.querySelector('.sr-only')?.textContent ?? null;
}
function iconPath(element: HTMLElement): string | null {
  return element.querySelector('svg path')?.getAttribute('d') ?? null;
}
const UP = 'M12 19V5M6 11l6-6 6 6';
const DOWN = 'M12 5v14M6 13l6 6 6-6';
const FLAT = 'M5 12h14';

test('the tone is the server sentiment, never the sign: a CPC fall is favourable (ADR-121)', () => {
  const element = chip({ direction: 'down', display_text: '−6.9%', not_meaningful: null, sentiment: 'favourable', unit: 'percent' });
  assert.equal(element.className, 'chg favourable');
  assert.equal(iconPath(element), DOWN);
  assert.equal(spoken(element), 'Down, favourable: ');
  assert.equal(element.textContent, 'Down, favourable: −6.9%');
});

test('spend is neutral in either direction and announces only its direction', () => {
  const up = chip({ direction: 'up', display_text: '+6.2%', not_meaningful: null, sentiment: 'neutral', unit: 'percent' });
  assert.equal(up.className, 'chg neutral');
  assert.equal(spoken(up), 'Up: ');
  cleanup();
  const down = chip({ direction: 'down', display_text: '−3.0%', not_meaningful: null, sentiment: 'neutral', unit: 'percent' });
  assert.equal(down.className, 'chg neutral');
  assert.equal(spoken(down), 'Down: ');
});

test('a change below the display step renders the server text with its direction', () => {
  const element = chip({ direction: 'up', display_text: '<0.001 pts', not_meaningful: null, sentiment: 'favourable', unit: 'percentage_points' });
  assert.equal(element.textContent, 'Up, favourable: <0.001 pts');
  assert.equal(iconPath(element), UP);
  cleanup();
  const fall = chip({ direction: 'down', display_text: '−0.002 pts', not_meaningful: null, sentiment: 'unfavourable', unit: 'percentage_points' });
  assert.equal(fall.className, 'chg unfavourable');
  assert.equal(fall.textContent, 'Down, unfavourable: −0.002 pts');
});

test('word states render the word, never a percentage, and announce only a tone', () => {
  const loss = chip({ direction: 'up', display_text: 'Up from a loss', not_meaningful: 'negative_prior', sentiment: 'neutral', unit: 'absolute' });
  assert.equal(loss.className, 'chg word neutral');
  assert.equal(spoken(loss), null);
  assert.equal(loss.textContent, 'Up from a loss');
  cleanup();
  const fresh = chip({ direction: 'up', display_text: 'New', not_meaningful: 'no_prior', sentiment: 'favourable', unit: 'absolute' });
  assert.equal(fresh.className, 'chg word favourable');
  assert.equal(fresh.textContent, 'favourable: New');
  cleanup();
  // A near-zero prior is an absolute change, not a word: it keeps the direction words.
  const nearZero = chip({ direction: 'up', display_text: '+54', not_meaningful: 'near_zero_prior', sentiment: 'unfavourable', unit: 'absolute' });
  assert.equal(nearZero.className, 'chg unfavourable');
  assert.equal(nearZero.textContent, 'Up, unfavourable: +54');
});

test('no change is neutral with the flat icon, and an absent block reads as unavailable', () => {
  const flat = chip({ direction: 'flat', display_text: 'No change', not_meaningful: null, sentiment: 'neutral', unit: 'percentage_points' });
  assert.equal(flat.className, 'chg neutral');
  assert.equal(iconPath(flat), FLAT);
  assert.equal(spoken(flat), null);
  assert.equal(flat.textContent, 'No change');
  cleanup();
  const absent = chip(null);
  assert.equal(absent.className, 'chg-absent');
  assert.equal(absent.textContent, 'Change unavailable');
});

test('every chip state is axe-clean', async () => {
  const states: Change[] = [
    { direction: 'down', display_text: '−6.9%', not_meaningful: null, sentiment: 'favourable', unit: 'percent' },
    { direction: 'up', display_text: 'Up from a loss', not_meaningful: 'negative_prior', sentiment: 'neutral', unit: 'absolute' },
    { direction: 'flat', display_text: 'No change', not_meaningful: null, sentiment: 'neutral', unit: 'percent' },
  ];
  const view = render(<main>{states.map((state) => <ChangeChip change={state} key={state.display_text} />)}<ChangeChip change={null} /></main>);
  const results = await axeCore.run(view.container, { rules: { 'color-contrast': { enabled: false } } });
  assert.deepEqual(results.violations.map((violation) => violation.id), []);
  axeCore.reset();
});

test('the block decoder refuses a shape or state the server never writes', () => {
  const valid = { direction: 'up', display_text: '+6.2%', not_meaningful: null, sentiment: 'neutral', unit: 'percent' };
  assert.deepEqual(parseChange(valid), valid);
  assert.equal(parseChange(null), null);
  for (const hostile of [
    { ...valid, tone: 'good' },
    { ...valid, sentiment: 'good' },
    { ...valid, direction: 'sideways' },
    { ...valid, unit: 'times' },
    { ...valid, not_meaningful: 'tiny_prior' },
    { ...valid, display_text: '' },
    { ...valid, direction: 'flat', sentiment: 'favourable', display_text: 'No change' },
    { ...valid, direction: 'flat', not_meaningful: 'no_prior', display_text: 'No change' },
    [valid],
    'up',
  ]) {
    assert.equal(parseChange(hostile), undefined);
  }
});
