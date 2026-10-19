// SPDX-License-Identifier: MIT-0
/** The browser's check of the server's two-place display value (ADR-084). */
import assert from 'node:assert/strict';
import { test } from 'vitest';

import { isDisplayValue } from '../lib/display-value';

test('a currency value must carry a canonical two-place display string', () => {
  // USD amounts as the server serves them: ungrouped, exactly two places, no "+".
  for (const display of ['75459.64', '-12.30', '0.00', '1234.00']) {
    assert.equal(isDisplayValue(display, 'currency'), true, display);
  }
  for (const display of [null, undefined, '5', '5.000', '5.0', '-0.00', '+1.00', '1,234.50', '01.00', 12.3]) {
    assert.equal(isDisplayValue(display, 'currency'), false, String(display));
  }
});

test('a count or ratio value, including DSP quality impressions and the viewable rate, carries null', () => {
  for (const kind of ['count', 'ratio']) {
    assert.equal(isDisplayValue(null, kind), true, kind);
    assert.equal(isDisplayValue('10.00', kind), false, kind);
    assert.equal(isDisplayValue(undefined, kind), false, kind);
  }
});
