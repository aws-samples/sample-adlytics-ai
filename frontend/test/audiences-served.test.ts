// SPDX-License-Identifier: MIT-0
import assert from 'node:assert/strict';
import { test } from 'vitest';
import { audiencesPresentationV2, parsePresentationResultCalendarV2 } from '@/lib/presentation-v2';
// The Audiences handler's own response; tests/unit/test_audiences_presentation.py pins it.
import served from './fixtures/audiences-served-v2.json';

const clone = <T>(value: T): T => JSON.parse(JSON.stringify(value)) as T;

test('the strict Audiences decoder accepts the server handler\'s own served envelope', () => {
  const result = parsePresentationResultCalendarV2(clone(served), 'audiences');
  // The calendar comparison arrives in the browser's v2 shape: policy, status, prior period label.
  assert.deepEqual(result.temporal.comparison, {
    policy: 'prior_calendar_period',
    prior_period_label: 'August 2026',
    status: 'unavailable',
  });
  const presentation = audiencesPresentationV2(result);
  assert.equal(presentation.kind, 'parent');
});

test('the Audiences decoder refuses the comparison shape the server sent before the fix', () => {
  // Paired control: the earlier wire shape (reason_code, no prior_period_label) is refused.
  const legacy = clone(served) as { temporal: { comparison: Record<string, unknown> } };
  legacy.temporal.comparison = { policy: 'prior_calendar_period', reason_code: null, status: 'unavailable' };
  assert.throws(() => parsePresentationResultCalendarV2(legacy, 'audiences'));
});
