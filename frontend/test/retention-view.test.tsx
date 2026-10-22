// SPDX-License-Identifier: MIT-0
import assert from 'node:assert/strict';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { test } from 'vitest';

import { RetentionView } from '@/components/RetentionView';
import { parseRetentionCohortArtifact } from '@/lib/retention';
import { retentionFixture, retentionTwoMarketFixture } from './fixtures/retention-v1-fixtures';

test('renders the group selector, keyboard-reachable triangle and opaque status spans', () => {
  const view = render(<RetentionView artifact={parseRetentionCohortArtifact(retentionFixture())} />);
  assert.ok(screen.getByRole('group', { name: 'Acquired through' }));
  const region = screen.getByRole('region', { name: /Share of each cohort/ });
  assert.equal(region.getAttribute('tabindex'), '0');
  assert.ok(view.container.querySelector('.retention-cell.not_returned > .retention-status'));
  assert.ok(view.container.querySelector('.retention-cell.not_yet > .retention-status'));
  assert.equal(view.container.querySelector('.retention-status')?.classList.contains('dim'), false);
  assert.equal(screen.queryByText(/month hasn't ended/i), null);

  fireEvent.click(screen.getByRole('radio', { name: 'Won by DSP only' }));
  assert.ok(within(region).getByText(/Won by DSP only · ad-touched repeat only/));
});

test('an all-absent selected grid renders the governed Status column and keeps metric headings out', () => {
  const fixture = retentionFixture();
  const market = (fixture.markets as Record<string, unknown>[])[0]!;
  const standard = ((market.triangle as Record<string, unknown>).standard as Record<string, unknown>[]);
  const selected = standard[1]!;
  for (const row of selected.rows as Record<string, unknown>[]) {
    row.cells = [];
    row.absent_text = 'No cohort was measured for this acquisition group.';
  }
  render(<RetentionView artifact={parseRetentionCohortArtifact(fixture)} />);
  const table = screen.getByRole('table', { name: /Won by sponsored ads only/ });
  assert.deepEqual(within(table).getAllByRole('columnheader').map((item) => item.textContent), ['Cohort', 'Status']);
  assert.ok(within(table).getAllByRole('cell').every((cell) => cell.textContent === 'No cohort was measured for this acquisition group.'));
  // Control: the normal selected grid retains its declared follow-up headings.
  cleanup();
  render(<RetentionView artifact={parseRetentionCohortArtifact(retentionFixture())} />);
  assert.ok(screen.getByRole('columnheader', { name: 'Month 1' }));
  assert.equal(screen.queryByRole('columnheader', { name: 'Status' }), null);
});

test('renders server-owned marketplace absence and its one evidence trace', () => {
  for (const outcome of ['empty', 'insufficient'] as const) {
    const artifact = parseRetentionCohortArtifact(retentionFixture(outcome));
    const view = render(<RetentionView artifact={artifact} />);
    assert.ok(screen.getAllByText(artifact.markets[0]!.absence_text!).length > 0);
    if (outcome === 'empty') {
      assert.equal(view.container.querySelectorAll('.retention-evidence').length, 1);
      assert.ok(screen.getByRole('heading', { name: artifact.evidence_status.slots[0]!.slot_label }));
      assert.ok(screen.getByText(artifact.evidence_status.slots[0]!.role_label));
      assert.ok(screen.getAllByText(artifact.evidence_status.slots[0]!.reason_text).length > 0);
    } else {
      assert.equal(view.container.querySelectorAll('.retention-evidence').length, 0);
    }
    view.unmount();
  }
});

test('renders the first available server cohort-size phrase and never invents one', () => {
  const later = retentionFixture();
  const laterGrid = ((((later.markets as Record<string, unknown>[])[0]!.triangle as Record<string, unknown>).standard as Record<string, unknown>[])[1]!);
  const laterCells = ((laterGrid.rows as Record<string, unknown>[])[0]!.cells as Record<string, unknown>[]);
  laterCells[0] = { cohort_size: null, column_label: 'Month 1', offset: 1, rate: null, state: 'not_returned', text: 'Not available' };
  laterCells[1] = { cohort_size: { value: '1200', text: '1,200 new customers' }, column_label: 'Month 2', offset: 2, rate: { value: '0.1250', text: '12.5%' }, state: 'value', text: null };
  const laterView = render(<RetentionView artifact={parseRetentionCohortArtifact(later)} />);
  assert.ok(screen.getByText('1,200 new customers'));
  laterView.unmount();

  const absent = retentionFixture();
  const absentGrid = ((((absent.markets as Record<string, unknown>[])[0]!.triangle as Record<string, unknown>).standard as Record<string, unknown>[])[1]!);
  const absentCells = ((absentGrid.rows as Record<string, unknown>[])[0]!.cells as Record<string, unknown>[]);
  absentCells[0] = { cohort_size: null, column_label: 'Month 1', offset: 1, rate: null, state: 'not_returned', text: 'Not available' };
  render(<RetentionView artifact={parseRetentionCohortArtifact(absent)} />);
  assert.equal(screen.queryByText('1,200 new customers'), null);
});


test('renders the quarterly grain note and server-owned curve values without formatting them', () => {
  const fixture = retentionFixture();
  const market = (fixture.markets as Record<string, unknown>[])[0]!;
  market.cohort_grain = 'quarter';
  market.grain_note = 'Cohorts are grouped by quarter from Q4 2026, because too many monthly cells had too few shoppers.';
  const triangle = market.triangle as Record<string, unknown>;
  for (const grid of triangle.standard as Record<string, unknown>[]) {
    for (const row of grid.rows as Record<string, unknown>[]) {
      row.label = 'Q2 2026';
      const cells = row.cells as Record<string, unknown>[];
      row.cells = cells.slice(0, 3);
      for (const cell of row.cells as Record<string, unknown>[]) cell.column_label = `Quarter ${cell.offset}`;
    }
  }
  const curve = market.curve as Record<string, unknown>;
  for (const line of curve.lines as Record<string, unknown>[]) {
    for (const point of line.points as Record<string, unknown>[]) point.label = `Quarter ${point.offset}`;
    line.points = (line.points as Record<string, unknown>[]).slice(0, 3);
  }
  render(<RetentionView artifact={parseRetentionCohortArtifact(fixture)} />);
  assert.ok(screen.getByText(/grouped by quarter from Q4 2026/));
  assert.ok(screen.getAllByText('Quarter 1').length > 0);
  assert.ok(screen.getAllByText('20.0%').length === 9);
});
test('a refreshed artifact without paid grids shows the standard path instead of the removed one', () => {
  const observed = retentionFixture();
  const market = (observed.markets as Record<string, unknown>[])[0]!;
  const triangle = market.triangle as Record<string, unknown>;
  market.path = { paid: { callout: null, state: 'observed' }, standard: { available: true } };
  triangle.paid = structuredClone(triangle.standard);
  for (const grid of triangle.paid as Record<string, unknown>[]) grid.grid_label = `${String(grid.label)} · all Amazon purchases`;
  const view = render(<RetentionView artifact={parseRetentionCohortArtifact(observed)} />);
  fireEvent.click(screen.getByRole('radio', { name: 'Any repeat purchase' }));
  const region = () => screen.getByRole('region', { name: /Share of each cohort/ });
  assert.ok(within(region()).getByText('Won by sponsored ads only · all Amazon purchases'));
  // The page context refresh replaces the artifact in place, so this component's choice survives it.
  view.rerender(<RetentionView artifact={parseRetentionCohortArtifact(retentionFixture())} />);
  assert.ok(within(region()).getByText(/Won by sponsored ads only · ad-touched repeat only/));
  assert.ok(screen.getByText('Amazon account IDs only.'));
});
test('two markets each lead with their marketplace heading and distinct landmark names', () => {
  const view = render(<RetentionView artifact={parseRetentionCohortArtifact(retentionTwoMarketFixture())} />);
  assert.deepEqual(screen.getAllByRole('heading', { level: 2 }).map((heading) => heading.textContent).filter((text) => text?.endsWith('marketplace')), ['US marketplace', 'UK marketplace']);
  const regions = screen.getAllByRole('region').map((region) => region.getAttribute('aria-label') ?? '');
  assert.equal(new Set(regions.filter((name) => name.startsWith('Share of each cohort'))).size, 2);
  for (const market of ['US', 'UK'] as const) {
    for (const section of ['Repeat purchase by cohort', 'Insights']) assert.ok(screen.getByRole('region', { name: `${market} marketplace ${section}` }));
    assert.ok(screen.getByRole('region', { name: `Share of each cohort that bought again, by period after acquisition · ${market} marketplace` }));
  }
  view.unmount();
  // Control: one market renders no marketplace heading and keeps the mockup's names.
  render(<RetentionView artifact={parseRetentionCohortArtifact(retentionFixture())} />);
  assert.equal(screen.queryByRole('heading', { name: /marketplace$/ }), null);
  assert.ok(screen.getByRole('region', { name: 'Share of each cohort that bought again, by period after acquisition' }));
  assert.ok(screen.getByRole('region', { name: 'Repeat purchase by cohort' }));
});
