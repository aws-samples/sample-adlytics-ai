// SPDX-License-Identifier: MIT-0
import assert from 'node:assert/strict';
import { test } from 'vitest';
import { parsePresentationResultCalendarV2, reachPresentationV2 } from '@/lib/presentation-v2';
import { cloneReachFixture } from './fixtures/reach-v1-fixtures';
// The Reach handler's own response over a real bake; tests/unit/test_reach_presentation.py pins it.
import served from './fixtures/reach-served-week-v2.json';

const week = { kind: 'preset', preset_id: 'last_complete_week' } as const;

test('the strict Reach decoder accepts the server handler\'s own served week envelope', () => {
  const result = parsePresentationResultCalendarV2(cloneReachFixture(served), 'reach');
  const presentation = reachPresentationV2(result, week);
  assert.equal(presentation.kind, 'parent');
  if (presentation.kind !== 'parent') return;
  assert.equal(presentation.artifact.preset, 'last_complete_week');
  assert.equal(presentation.temporal.resolved.period_label, presentation.artifact.period.label);
  assert.equal(presentation.page_context.label, presentation.artifact.page_context_label);
  assert.ok(presentation.artifact.markets.every((market) =>
    market.insights.every((slot) => slot.artifact_revision === presentation.artifact_revision)));
});

test('the served Reach envelope is bound to the preset it answers', () => {
  // Paired control: the same server bytes refuse to stand in for the default month request.
  const result = parsePresentationResultCalendarV2(cloneReachFixture(served), 'reach');
  assert.throws(() => reachPresentationV2(result, undefined));
});
