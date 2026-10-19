// SPDX-License-Identifier: MIT-0
import assert from 'node:assert/strict';
import { test } from 'vitest';

import { parseRetentionCohortArtifact } from '@/lib/retention';
import { cloneRetention, retentionFixture, retentionTwoMarketFixture } from './fixtures/retention-v1-fixtures';

type Json = Record<string, unknown>;

test('decodes server-shaped complete, empty and insufficient Retention bodies directly', () => {
  for (const outcome of ['complete', 'empty', 'insufficient'] as const) {
    const parsed = parseRetentionCohortArtifact(retentionFixture(outcome));
    assert.equal(parsed.outcome, outcome);
    assert.equal(parsed.starter_questions.length, outcome === 'complete' ? 1 : 0);
  }
});

test('rejects unknown fields, malformed month, blank labels, bad roster order and mismatched state values', () => {
  const passing = parseRetentionCohortArtifact(retentionFixture());
  assert.equal(passing.vintage.as_of_month, '2026-09');
  const edits: Array<(candidate: Json) => void> = [
    (candidate) => { candidate.unexpected = true; },
    (candidate) => { (candidate.vintage as Json).as_of_month = '2026-13'; },
    (candidate) => { (candidate.vintage as Json).label = ' '; },
    (candidate) => { ((candidate.markets as Json[])[0]!.triangle as Json).standard = [...(((candidate.markets as Json[])[0]!.triangle as Json).standard as Json[])].reverse(); },
    (candidate) => { (((((candidate.markets as Json[])[0]!.triangle as Json).standard as Json[])[0]!.rows as Json[])[0]!.cells as Json[])[0]!.text = 'browser fallback'; },
  ];
  for (const edit of edits) {
    const candidate = cloneRetention(retentionFixture());
    edit(candidate);
    assert.throws(() => parseRetentionCohortArtifact(candidate), TypeError);
  }
});

test('keeps server display text opaque instead of deriving or formatting figures', () => {
  const candidate = retentionFixture();
  const first = (((((candidate.markets as Json[])[0]!.triangle as Json).standard as Json[])[0]!.rows as Json[])[0]!.cells as Json[])[0]!;
  (first.rate as Json).value = '0.9876';
  (first.rate as Json).text = '12.5%';
  const parsed = parseRetentionCohortArtifact(candidate);
  assert.equal(parsed.markets[0]!.triangle.standard[0]!.rows[0]!.cells[0]!.rate?.text, '12.5%');
  assert.notEqual(parsed.markets[0]!.triangle.standard[0]!.rows[0]!.cells[0]!.rate?.value, '0.1250');
});

test('rejects impossible path, follow-up, outcome and evidence matrices with paired valid controls', () => {
  assert.equal(parseRetentionCohortArtifact(retentionFixture()).outcome, 'complete');
  const edits: Array<(candidate: Json) => void> = [
    (candidate) => {
      const first = (((((candidate.markets as Json[])[0]!.triangle as Json).standard as Json[])[0]!.rows as Json[])[0]!.cells as Json[])[0]!;
      Object.assign(first, { cohort_size: null, rate: null, state: 'withheld', text: 'cohort sizes changed between runs' });
    },
    (candidate) => {
      const first = (((((candidate.markets as Json[])[0]!.triangle as Json).standard as Json[])[0]!.rows as Json[])[0]!.cells as Json[])[0]!;
      first.cohort_size = null;
    },
    (candidate) => {
      const row = (((((candidate.markets as Json[])[0]!.triangle as Json).standard as Json[])[0]!.rows as Json[])[0]!);
      const cells = row.cells as Json[];
      cells[1] = {
        cohort_size: { value: '1200', text: 'different customers' },
        column_label: 'Month 2', offset: 2, rate: { value: '0.1250', text: '12.5%' },
        state: 'value', text: null,
      };
    },
    (candidate) => {
      const cells = (((((candidate.markets as Json[])[0]!.triangle as Json).standard as Json[])[0]!.rows as Json[])[0]!.cells as Json[]);
      cells.pop();
    },
    (candidate) => {
      const market = (candidate.markets as Json[])[0]!;
      market.outcome = 'insufficient';
      market.absence_text = 'There is not enough cohort evidence to show Retention safely.';
    },
    (candidate) => { candidate.evidence_status = retentionFixture('empty').evidence_status; },
    (candidate) => { delete candidate.evidence_status; },
    (candidate) => { candidate.evidence_status = [retentionFixture().evidence_status, retentionFixture().evidence_status]; },
  ];
  for (const edit of edits) {
    const candidate = cloneRetention(retentionFixture());
    edit(candidate);
    assert.throws(() => parseRetentionCohortArtifact(candidate), TypeError);
  }
});
test('names every market only when two or more are shown, each with a distinct server text', () => {
  // Controls: one market without a text and two markets with distinct texts decode.
  assert.equal(parseRetentionCohortArtifact(retentionFixture()).markets[0]!.marketplace_text, null);
  assert.deepEqual(parseRetentionCohortArtifact(retentionTwoMarketFixture()).markets.map((market) => market.marketplace_text), ['US marketplace', 'UK marketplace']);
  const markets = (fixture: Record<string, unknown>) => fixture.markets as Record<string, unknown>[];
  const unnamed = retentionTwoMarketFixture(); markets(unnamed)[1]!.marketplace_text = null;
  const alike = retentionTwoMarketFixture(); markets(alike)[1]!.marketplace_text = 'US marketplace';
  const lone = retentionFixture(); markets(lone)[0]!.marketplace_text = 'US marketplace';
  const missing = retentionFixture(); delete markets(missing)[0]!.marketplace_text;
  for (const value of [unnamed, alike, lone, missing]) assert.throws(() => parseRetentionCohortArtifact(value), TypeError);
});
