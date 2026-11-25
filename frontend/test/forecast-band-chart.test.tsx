// SPDX-License-Identifier: MIT-0
/**
 * ForecastBandChart and its legend (demand forecast design S2, G-01, G-14). A numeric tick would be
 * a figure the browser calculated; a legend entry for a mark that isn't drawn gives a meaning with
 * nothing on screen; a not-known week drawn through would read as a reported value.
 */
import assert from 'node:assert/strict';
import { render, screen } from '@testing-library/react';
import { test } from 'vitest';
import { ForecastBandChart, ForecastBandLegend, type BandForecast, type BandRecentWeek } from '@/components/ChartPrimitives';

const recent: BandRecentWeek[] = [
  { week: '2026-10-11', state: 'observed', value: '9.0000' },
  { week: '2026-10-18', state: 'unknown', value: null },
  { week: '2026-10-25', state: 'observed', value: '12.0000' },
  { week: '2026-11-01', state: 'observed', value: '14.0000' },
];
const forecast: BandForecast = {
  weeks: ['2026-11-08', '2026-11-15', '2026-11-22', '2026-11-29'],
  low: ['10.0000', '10.0000', '9.0000', '9.0000'],
  likely: ['11.0000', '11.5000', '10.0000', '10.0000'],
  high: ['12.0000', '13.0000', '12.0000', '12.5000'],
};

function draw(weeks: readonly BandRecentWeek[] = recent) {
  return render(<ForecastBandChart
    ariaLabel="Units ordered by week for Fictional product 1"
    dividerLabel="Forecast starts Nov 8"
    forecast={forecast}
    recent={weeks}
  />);
}

test('the chart is one named image with no numeric axis text', () => {
  const { container } = draw();
  assert.ok(screen.getByRole('img', { name: 'Units ordered by week for Fictional product 1' }));
  assert.equal(container.querySelector('.y-axis'), null);
  // Only dates carry digits: the first recent week, the last forecast week and the divider's date.
  const text = (container.textContent ?? '').replace(/(Oct 11|Nov 29), 2026/g, '').replace('Nov 8', '');
  assert.doesNotMatch(text, /\d/);
  assert.ok(screen.getByText('Forecast starts Nov 8'));
  assert.ok(screen.getByText('Starts at zero. Exact weekly numbers are in the table below.'));
  assert.ok(container.querySelector('polygon.df-band'));
  assert.ok(container.querySelector('line.df-divider'));
});

test('a not-known week splits the reported line', () => {
  const { container } = draw();
  // Week 1 alone (a dot) and weeks 3 to 4 (a line); the not-known week 2 draws nothing.
  assert.equal(container.querySelectorAll('.df-reported').length, 2);
  assert.equal(container.querySelectorAll('polyline.df-reported').length, 1);
  assert.equal(container.querySelectorAll('polyline.df-likely').length, 1);
});

test('a week counted as zero is a hollow dot', () => {
  const { container } = draw([...recent.slice(0, 3), { week: '2026-11-01', state: 'inferred_zero', value: '0.0000' }]);
  assert.equal(container.querySelectorAll('circle.df-zero').length, 1);
});

test('the legend lists only the marks drawn', () => {
  const plain = render(<ForecastBandLegend reported zero={false}/>);
  assert.deepEqual([...plain.container.querySelectorAll('span')].map((item) => item.textContent), ['Reported', 'Likely', 'Low to high']);
  plain.unmount();
  const zero = render(<ForecastBandLegend reported={false} zero/>);
  assert.deepEqual([...zero.container.querySelectorAll('span')].map((item) => item.textContent),
    ['Likely', 'Low to high', 'No sales reported, counted as zero']);
});
