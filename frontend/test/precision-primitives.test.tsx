// SPDX-License-Identifier: MIT-0
import assert from 'node:assert/strict';
import axeCore from 'axe-core';
import { fireEvent, render, screen, within } from '@testing-library/react';
import { test, vi } from 'vitest';

import { ExactChart, type ExactChartSeries } from '@/components/ExactChart';
import {
  ActionBar,
  CoveredEmptyState,
  EpistemicNarrativeBlocks,
  EvidenceDisclosure,
  InsufficientState,
  LoadingSkeleton,
  OutcomeBanner,
  PageIssue,
  PeriodComparisonStrip,
  SelectedEntityDetail,
  SemanticDataTable,
  SemanticMatrix,
} from '@/components/PrecisionPrimitives';
import {
  formatCanonicalDecimal,
  formatCount,
  formatCurrency,
  formatIsoDate,
  formatIsoDateRange,
  formatIsoInstant,
  formatPercent,
  formatRatePercent,
  formatRatio,
} from '@/lib/exact-format';
import type * as ExactFormat from '@/lib/exact-format';

async function expectNoAxeViolations(): Promise<void> {
  const results = await axeCore.run(document.body, {
    rules: { 'color-contrast': { enabled: false } },
  });
  assert.deepEqual(results.violations, []);
}

test('exact formatters preserve every analytical digit and reject noncanonical input', () => {
  assert.equal(
    formatCanonicalDecimal('123456789012345678901234567890.1200'),
    '123,456,789,012,345,678,901,234,567,890.1200',
  );
  assert.equal(formatCurrency('-1234567.5000', 'USD'), '-1,234,567.5000 USD');
  assert.equal(formatCount('+12345678901234567890'), '+12,345,678,901,234,567,890');
  assert.equal(formatRatio('0.0000'), '0.0000×');
  assert.equal(formatPercent('+12.3400'), '+12.3400%');

  assert.equal(formatIsoDate('2028-02-29'), 'Feb 29, 2028');
  assert.equal(formatIsoDateRange('2026-08-11', '2026-08-17'), 'Aug 11–17, 2026');
  assert.equal(formatIsoDateRange('2025-12-29', '2026-01-04'), 'Dec 29, 2025–Jan 4, 2026');

  assert.throws(() => formatCanonicalDecimal('01.5000'), RangeError);
  assert.throws(() => formatCanonicalDecimal('1e3'), RangeError);
  assert.throws(() => formatCount('3.0'), RangeError);
  assert.throws(() => formatCurrency('1.00', 'usd'), RangeError);
  assert.throws(() => formatIsoDate('2025-02-29'), RangeError);
  assert.throws(() => formatIsoDateRange('2026-08-17', '2026-08-11'), RangeError);
});

/**
 * exact-format's month table and input patterns are module constants, built once at import. The
 * tests that pin them read a fresh import, so they check the module as it loads in that test
 * rather than a copy evaluated before the test began; a change switched on at test start (as the
 * mutation runner does) would otherwise never reach them.
 */
async function freshExactFormat(): Promise<typeof ExactFormat> {
  vi.resetModules();
  return import('@/lib/exact-format');
}
// Gregorian months under their English three-letter labels, read from the calendar rather than
// from exact-format's own table: first day, last day, and the day after the last (refused).
const CALENDAR_2023 = [
  ['2023-01-01', 'Jan 1, 2023', '2023-01-31', 'Jan 31, 2023', '2023-01-32'],
  ['2023-02-01', 'Feb 1, 2023', '2023-02-28', 'Feb 28, 2023', '2023-02-29'],
  ['2023-03-01', 'Mar 1, 2023', '2023-03-31', 'Mar 31, 2023', '2023-03-32'],
  ['2023-04-01', 'Apr 1, 2023', '2023-04-30', 'Apr 30, 2023', '2023-04-31'],
  ['2023-05-01', 'May 1, 2023', '2023-05-31', 'May 31, 2023', '2023-05-32'],
  ['2023-06-01', 'Jun 1, 2023', '2023-06-30', 'Jun 30, 2023', '2023-06-31'],
  ['2023-07-01', 'Jul 1, 2023', '2023-07-31', 'Jul 31, 2023', '2023-07-32'],
  ['2023-08-01', 'Aug 1, 2023', '2023-08-31', 'Aug 31, 2023', '2023-08-32'],
  ['2023-09-01', 'Sep 1, 2023', '2023-09-30', 'Sep 30, 2023', '2023-09-31'],
  ['2023-10-01', 'Oct 1, 2023', '2023-10-31', 'Oct 31, 2023', '2023-10-32'],
  ['2023-11-01', 'Nov 1, 2023', '2023-11-30', 'Nov 30, 2023', '2023-11-31'],
  ['2023-12-01', 'Dec 1, 2023', '2023-12-31', 'Dec 31, 2023', '2023-12-32'],
] as const;
test('every month renders under its own three-letter label', async () => {
  const fresh = await freshExactFormat();
  for (const [first, label] of CALENDAR_2023) assert.equal(fresh.formatIsoDate(first), label, first);
});
test('every month accepts its last day and refuses the day after it', async () => {
  const fresh = await freshExactFormat();
  for (const [, , last, label, after] of CALENDAR_2023) {
    assert.equal(fresh.formatIsoDate(last), label, last);
    assert.throws(() => fresh.formatIsoDate(after), RangeError, after);
  }
});
test('29 February exists only in Gregorian leap years', () => {
  assert.equal(formatIsoDate('2024-02-29'), 'Feb 29, 2024'); // divisible by 4
  assert.equal(formatIsoDate('2000-02-29'), 'Feb 29, 2000'); // divisible by 400
  assert.throws(() => formatIsoDate('2100-02-29'), RangeError); // by 100 and not by 400
  assert.throws(() => formatIsoDate('2023-02-29'), RangeError); // not divisible by 4
});
test('formatRatePercent shows a served rate times 100 at the asked places', () => {
  // A served rate is a fraction: 0.0450 is 4.5%, never 0.0450%.
  assert.equal(formatRatePercent('0.0450', 1), '4.5%');
  assert.equal(formatRatePercent('1', 0), '100%');
  assert.equal(formatRatePercent('0.5', 2), '50.00%'); // more places shown than served
  assert.equal(formatRatePercent('0', 2), '0.00%');
});
test('formatRatePercent rounds half up for display only', () => {
  // 0.0725% at three places: exactly half rounds up, just under half rounds down.
  assert.equal(formatRatePercent('0.000725', 3), '0.073%');
  assert.equal(formatRatePercent('0.000724', 3), '0.072%');
  assert.equal(formatRatePercent('12.3456', 0), '1,235%'); // 1,234.56% to a grouped whole percent
});
test('formatRatePercent keeps a served sign unless the shown figure rounds to zero', () => {
  assert.equal(formatRatePercent('-0.0450', 1), '-4.5%');
  assert.equal(formatRatePercent('+0.0450', 1), '+4.5%');
  assert.equal(formatRatePercent('-0.0000004', 1), '0.0%');
});
test('decimal, date and instant inputs must match whole: nothing before or after is ignored', async () => {
  const fresh = await freshExactFormat();
  // Controls: the same values without the extra characters are accepted.
  assert.equal(fresh.formatCanonicalDecimal('1234'), '1,234');
  assert.equal(fresh.formatCanonicalDecimal('-10.25'), '-10.25');
  assert.equal(fresh.formatIsoDate('2024-01-05'), 'Jan 5, 2024');
  assert.equal(fresh.formatIsoInstant('2026-10-09T21:06:31Z'), '2026-10-09 21:06:31 UTC');
  for (const [format, value] of [
    [fresh.formatCanonicalDecimal, 'x1234'],
    [fresh.formatCanonicalDecimal, '1234x'],
    [fresh.formatIsoDate, 'x2024-01-05'],
    [fresh.formatIsoDate, '2024-01-05x'],
    [fresh.formatIsoInstant, 'x2026-10-09T21:06:31Z'],
    [fresh.formatIsoInstant, '2026-10-09T21:06:31Zx'],
  ] as const) {
    assert.throws(() => format(value), RangeError, value);
  }
});
test('formatIsoInstant prints a whole-second UTC instant and refuses anything else', () => {
  assert.equal(formatIsoInstant('2026-10-09T21:06:31Z'), '2026-10-09 21:06:31 UTC');
  assert.equal(formatIsoInstant('2028-02-29T00:00:00Z'), '2028-02-29 00:00:00 UTC');
  // Control: a non-instant is refused, never reformatted.
  for (const value of [
    '2026-10-09',
    '2026-10-09T21:06:31',
    '2026-10-09T21:06:31.5Z',
    '2026-10-09T21:06:31+00:00',
    '2026-10-09 21:06:31Z',
    '2026-10-09T24:00:00Z',
    '2026-10-09T21:60:00Z',
    '2025-02-29T00:00:00Z',
    '',
  ]) {
    assert.throws(() => formatIsoInstant(value), RangeError, value);
  }
});

test('semantic matrix retains exact row and column associations', () => {
  const rows = [
    { channel: 'DSP', first: '0.0000×', last: 'Suppressed' },
    { channel: 'Sponsored Products', first: '4.6100×', last: '4.3100×' },
  ] as const;

  render(
    <SemanticMatrix
      caption="ROAS by attribution model"
      columns={[
        {
          isRowHeader: true,
          key: 'channel',
          label: 'Channel',
          render: (row) => row.channel,
        },
        {
          align: 'end',
          key: 'first',
          label: 'First touch',
          render: (row) => row.first,
        },
        {
          align: 'end',
          key: 'last',
          label: 'Last touch',
          render: (row) => row.last,
        },
      ]}
      description="Exact values; suppressed cells are not zero."
      getRowKey={(row) => row.channel}
      id="roas-matrix"
      rows={rows}
    />,
  );

  const table = screen.getByRole('table', { name: 'ROAS by attribution model' });
  const dspHeader = within(table).getByRole('rowheader', { name: 'DSP' });
  const zeroCell = within(table).getByRole('cell', { name: '0.0000×' });
  const suppressedCell = within(table).getByRole('cell', { name: 'Suppressed' });

  assert.equal(dspHeader.getAttribute('scope'), 'row');
  assert.equal(dspHeader.id, 'roas-matrix-row-0');
  assert.equal(zeroCell.getAttribute('headers'), 'roas-matrix-column-1 roas-matrix-row-0');
  assert.equal(suppressedCell.getAttribute('headers'), 'roas-matrix-column-2 roas-matrix-row-0');
  assert.equal(table.getAttribute('data-matrix'), 'true');
  assert.equal(table.closest('[role="region"]')?.getAttribute('tabindex'), '0');
});

test('ExactChart draws supplied straight segments, preserves zero, and links its exact table', async () => {
  const series: readonly ExactChartSeries[] = [
    {
      color: 'chart-1',
      id: 'first-touch',
      label: 'First touch',
      lineStyle: 'solid',
      points: [
        { key: 'd1', label: 'Aug 11', status: 'available', value: '0.0000', x: 40, y: 120 },
        { key: 'd2', label: 'Aug 12', status: 'available', value: '1.2500', x: 80, y: 90 },
        { key: 'd3', label: 'Aug 13', status: 'missing', value: null, x: 120, y: null },
        { key: 'd4', label: 'Aug 14', status: 'available', value: '2.1000', x: 160, y: 60 },
        { key: 'd5', label: 'Aug 15', status: 'available', value: '2.5000', x: 200, y: 40 },
        { key: 'd6', label: 'Aug 16', status: 'suppressed', value: '8.0000', x: 240, y: 10 },
        { key: 'd7', label: 'Aug 17', status: 'available', value: '0.0000', x: 280, y: 120 },
      ],
    },
  ];

  const view = render(
    <main>
      <h1>Selected-channel daily evidence</h1>
      <ExactChart
        description="Daily ROAS at the governed source-date grain."
        id="daily-roas-chart"
        scale={{
          height: 170,
          plot: { bottom: 130, left: 40, right: 280, top: 10 },
          width: 300,
          xTicks: [
            { key: 'start', label: 'Aug 11', position: 40 },
            { key: 'end', label: 'Aug 17', position: 280 },
          ],
          yTicks: [
            { key: 'zero', label: '0.0000×', position: 120 },
            { key: 'high', label: '3.0000×', position: 20 },
          ],
        }}
        series={series}
        tableId="daily-values"
        title="Daily ROAS"
      />
      <SemanticDataTable
        caption="Exact daily ROAS values"
        columns={[
          { isRowHeader: true, key: 'date', label: 'Date', render: (row) => row.date },
          { key: 'value', label: 'First touch', render: (row) => row.value },
        ]}
        getRowKey={(row) => row.date}
        id="daily-values"
        rows={[
          { date: '2026-08-11', value: '0.0000×' },
          { date: '2026-08-13', value: 'Missing' },
          { date: '2026-08-16', value: 'Suppressed' },
        ]}
      />
    </main>,
  );

  const svg = screen.getByRole('img', { name: 'Daily ROAS' });
  assert.equal(svg.getAttribute('aria-details'), 'daily-values');
  assert.equal(view.container.querySelector('figure')?.getAttribute('data-table-id'), 'daily-values');
  assert.equal(screen.getByRole('link', { name: 'View exact data table' }).getAttribute('href'), '#daily-values');

  const lines = [...view.container.querySelectorAll('polyline.exact-chart__line')];
  assert.equal(lines.length, 2);
  assert.deepEqual(lines.map((line) => line.getAttribute('points')), [
    '40,120 80,90',
    '160,60 200,40',
  ]);
  assert.equal(view.container.querySelectorAll('path').length, 0);
  assert.equal(view.container.querySelectorAll('circle[data-value="0.0000"]').length, 2);
  assert.equal(view.container.querySelector('circle[data-point="d6"]'), null);
  assert.ok(view.container.querySelector('g[data-gap="missing"][data-point="d3"]'));
  assert.ok(view.container.querySelector('g[data-gap="suppressed"][data-point="d6"]'));

  await expectNoAxeViolations();
});

test('shared Precision primitives expose truthful states and operable controls', async () => {
  const clear = vi.fn();
  const retry = vi.fn();
  const view = render(
    <main>
      <h1>Precision primitive states</h1>
      <OutcomeBanner
        facts={[{ label: 'Scope', value: 'United States' }]}
        status="Model sensitive"
        summary={<p>The supplied leaders differ by attribution model.</p>}
        title="ROAS ranking depends on attribution interpretation"
        tone="warning"
      />
      <PeriodComparisonStrip
        current={{
          facts: [{ label: 'First touch', value: 'DSP' }],
          label: 'Current period',
          outcome: 'Model sensitive',
          period: 'Aug 11–17, 2026',
          tone: 'warning',
        }}
        previous={{
          facts: [{ label: 'First touch', value: 'DSP' }],
          label: 'Previous equal period',
          outcome: 'Model sensitive',
          period: 'Aug 4–10, 2026',
          tone: 'warning',
        }}
        relation={{
          description: 'The supplied period relation is unchanged.',
          label: 'Sensitivity persisted',
          tone: 'warning',
        }}
      />
      <SelectedEntityDetail
        description={<p>Exact daily evidence for the selected channel.</p>}
        facts={[{ label: 'Channel', value: 'DSP' }]}
        heading="DSP"
        onClear={clear}
      >
        <p>Selected detail body</p>
      </SelectedEntityDetail>
      <EvidenceDisclosure summaryDetail="3 qualifications">
        <p>Coverage is partial on one source date.</p>
      </EvidenceDisclosure>
      <LoadingSkeleton label="Loading the exact artifact" lines={3} />
      <CoveredEmptyState description="Coverage is complete and the source returned no rows." />
      <InsufficientState
        description="A complete comparison cannot be made."
        reasons={['Previous evidence is suppressed.']}
      />
      <PageIssue description="The artifact request failed safely." onRetry={retry} />
      <ActionBar status="One destination is available.">
        <a href="#daily-evidence">Inspect daily evidence</a>
      </ActionBar>
      <EpistemicNarrativeBlocks
        blocks={[
          { content: <p>Every supplied model leader is preserved.</p>, id: 'evidence', label: 'Evidence' },
          { content: <p>Inspect the selected channel.</p>, id: 'next', label: 'Next step' },
        ]}
      />
      <div id="daily-evidence">Daily evidence destination</div>
    </main>,
  );

  const outcome = screen.getByRole('region', { name: 'Outcome' });
  assert.ok(outcome);
  assert.ok(screen.getByRole('region', { name: 'Period comparison' }));
  assert.equal(
    within(outcome).getByText('Model sensitive').closest('[data-tone]')?.getAttribute('data-tone'),
    'warning',
  );

  fireEvent.click(screen.getByRole('button', { name: 'Clear selection' }));
  assert.equal(clear.mock.calls.length, 1);

  const summary = screen.getByText('Evidence and qualifications');
  const disclosure = summary.closest('details');
  assert.ok(disclosure);
  assert.equal(disclosure.open, false);
  fireEvent.click(summary.closest('summary') ?? summary);
  assert.equal(disclosure.open, true);

  const loading = screen.getByRole('status', { name: 'Loading the exact artifact' });
  assert.equal(loading.getAttribute('aria-busy'), 'true');
  assert.equal(view.container.querySelectorAll('.precision-loading__line').length, 3);
  assert.ok(screen.getByRole('heading', { name: 'No values in the covered period' }));
  assert.ok(screen.getByText('Previous evidence is suppressed.'));

  fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
  assert.equal(retry.mock.calls.length, 1);
  assert.match(screen.getByRole('alert').textContent ?? '', /failed safely/);
  assert.ok(screen.getByRole('navigation', { name: 'Available actions' }));
  assert.ok(screen.getByRole('region', { name: 'Reasoning' }));

  await expectNoAxeViolations();
});
