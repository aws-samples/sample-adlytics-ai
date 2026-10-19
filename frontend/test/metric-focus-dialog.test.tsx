// SPDX-License-Identifier: MIT-0
/** Shared metric-focus dialog: composition blocks, switcher, and designed states. */
import assert from 'node:assert/strict';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, test, vi } from 'vitest';

import { MetricFocusDialog, type MetricFocusSurface } from '../components/MetricFocusDialog';
import type { MetricFocusV4 } from '../lib/metric-focus';
import { changeBlock } from './fixtures/change-fixture';

afterEach(cleanup);

const period = { end: '2026-08-16', start: '2026-08-10' };
const prior = { end: '2026-08-09', start: '2026-08-03' };
const usd = { currency_code: 'USD', kind: 'currency' as const };
const count = { currency_code: null, kind: 'count' as const };

function fixture(overrides: Partial<MetricFocusV4> = {}): MetricFocusV4 {
  const value = (window: typeof period, figure: string, display: string) => ({
    basis: 'ad_attributed' as const,
    display_value: display,
    grain: ['period', 'marketplace'],
    period: window,
    units: { currency_code: 'USD', kind: 'currency' as const },
    value: figure,
  });
  return {
    boundary: null,
    change: changeBlock('100', '+11.11%'),
    current: value(period, '1000', '1000.00'),
    drivers: {
      entries: [
        { change: '54', delta: changeBlock('54', '+12.40%'), display_change: '54.00', name: 'Fictional Growth', share_of_move: '54%', units: usd, chip: null, moves_against_basis: false },
        { change: '-12', delta: changeBlock('-12', '-21.90%'), display_change: '-12.00', name: 'Fictional Awareness', share_of_move: null, units: usd, chip: 'Moved against the overall change', moves_against_basis: true },
      ],
      grain: 'campaign',
      scope: { count: 2, label: 'Largest changes among all 2 campaigns in the portfolio', population: 2, share_suffix: 'of the overall change' },
      share_basis: 'portfolio',
    },
    drivers_summary: {
      fell: 1,
      flat: 0,
      lines: [
        'Largest changes among all 2 campaigns in the portfolio',
        'The 2 listed campaigns: 42.00 USD',
        'Among these 2: 1 rose, 1 fell, 0 were flat',
      ],
      listed_change: { display: '42.00 USD', value: '42' },
      other_in_scope_change: null,
      outside_scope_change: null,
      rose: 1,
    },
    facts: [
      { id: 'peak_day', label: 'Peak day', qualifier: null, value: '2026-08-16 · 17.6 USD' },
      { id: 'window_total', label: 'Window total', qualifier: null, value: '1000 USD' },
    ],
    focus_version: 4,
    insight: {
      absence_class: null,
      absence_reason: null,
      artifact_revision: 'revision-fictional',
      evidence_line: 'Spend rose from 900 to 1000.',
      id: 'metric-focus',
      output_class: 'perspective',
      next_step: null,
      prompt_version: 'v3',
      state: 'filled',
      text: 'The spend increase is concentrated in one campaign.',
    },
    metric: {
      basis: 'ad_attributed',
      definition: 'Fee-inclusive advertising spend across DSP and sponsored ads, as AMC reports it.',
      grain: ['period', 'marketplace'],
      id: 'spend',
      label: 'Ad spend',
      unit: { currency_code: 'USD', kind: 'currency' },
    },
    neighbors: ['spend', 'purchases'],
    next_check: null,
    period,
    prior: value(prior, '900', '900.00'),
    prior_period: prior,
    series: {
      display_grain: 'day',
      points: [
        { current: '450', date: '2026-08-10', prior: '440' },
        { current: '550', date: '2026-08-16', prior: '460' },
      ],
    },
    ...overrides,
  };
}

function surfaceFor(focus: MetricFocusV4): MetricFocusSurface {
  return {
    cache: new Map<string, MetricFocusV4>(),
    cacheScope: 'revision-fictional',
    pageId: 'campaigns',
    askSeed: () => 'Ask about ad spend for the portfolio.',
    fetch: () => Promise.resolve(focus),
    deepFor: () => null,
    labelFor: (metricId) => metricId === 'spend' ? 'Ad spend' : 'Attributed purchases',
    neighbors: ['spend', 'purchases'],
    window: period,
  };
}

test('the dialog renders the contract blocks: definition line, facts, drivers with shares', async () => {
  render(
    <MetricFocusDialog
      metricId="spend"
      onClose={() => undefined}
      onMetricChange={() => undefined}
      surface={surfaceFor(fixture())}
      surfaceKey="campaign-portfolio"
    />,
  );

  const dialog = await screen.findByRole('dialog');
  await waitFor(() => {
    assert.ok(dialog.textContent?.includes('Fee-inclusive advertising spend'));
  });
  assert.ok(dialog.querySelector('.defline'));
  assert.ok(dialog.textContent?.includes('Peak day'));
  // The share carries its basis's served suffix (MetricFocus.v3, revised 2026-10-05).
  assert.ok(dialog.textContent?.includes('54% of the overall change'));
  assert.ok(dialog.textContent?.includes('The spend increase is concentrated in one campaign.'));
  // The identity head renders the exact returned values with their currency.
  assert.ok(dialog.querySelector('.big')?.textContent?.includes('1,000'));
});

test('the switcher hops metrics without closing and Escape closes', async () => {
  const user = userEvent.setup();
  const onClose = vi.fn();
  const onMetricChange = vi.fn();
  render(
    <MetricFocusDialog
      metricId="spend"
      onClose={onClose}
      onMetricChange={onMetricChange}
      surface={surfaceFor(fixture())}
      surfaceKey="campaign-portfolio"
    />,
  );

  await screen.findByRole('dialog');
  await user.click(screen.getByRole('button', { name: 'Next metric' }));
  assert.deepEqual(onMetricChange.mock.calls, [['purchases']]);
  assert.equal(onClose.mock.calls.length, 0);
  await user.keyboard('{Escape}');
  assert.equal(onClose.mock.calls.length, 1);
});

test('an absent decomposition renders its plain-words reason, never an empty block', async () => {
  const focus = fixture({
    drivers: { absent_reason: 'This page does not carry per-campaign values for this metric.' },
    series: null,
  });
  render(
    <MetricFocusDialog
      metricId="spend"
      onClose={() => undefined}
      onMetricChange={() => undefined}
      surface={surfaceFor(focus)}
      surfaceKey="campaign-portfolio"
    />,
  );

  const dialog = await screen.findByRole('dialog');
  await waitFor(() => {
    assert.ok(dialog.textContent?.includes('does not carry per-campaign values'));
  });
  // The absent series renders the designed state, never a blank panel.
  assert.ok(dialog.textContent?.includes('A returned time series is not available'));
});

test('a focus for another resolved window is refused as unavailable', async () => {
  const focus = fixture({
    current: { ...fixture().current, period: { end: '2026-08-09', start: '2026-08-03' } },
    period: { end: '2026-08-09', start: '2026-08-03' },
    prior: { ...fixture().prior, period: { end: '2026-08-02', start: '2026-07-27' } },
    prior_period: { end: '2026-08-02', start: '2026-07-27' },
  });
  render(
    <MetricFocusDialog
      metricId="spend"
      onClose={() => undefined}
      onMetricChange={() => undefined}
      surface={surfaceFor(focus)}
      surfaceKey="campaign-portfolio"
    />,
  );

  const dialog = await screen.findByRole('dialog');
  await waitFor(() => {
    assert.ok(dialog.textContent?.includes('could not be served'));
  });
});

test('the rendered dialog is axe-clean, including heading order', async () => {
  const axe = await import('axe-core');
  render(
    <MetricFocusDialog
      metricId="spend"
      onClose={() => undefined}
      onMetricChange={() => undefined}
      surface={surfaceFor(fixture())}
      surfaceKey="campaign-portfolio"
    />,
  );
  const dialog = await screen.findByRole('dialog');
  await waitFor(() => {
    assert.ok(dialog.textContent?.includes('54% of the overall change'));
  });
  const results = await axe.default.run(dialog, {
    rules: { 'color-contrast': { enabled: false }, region: { enabled: false } },
  });
  assert.deepEqual(results.violations.map((violation) => violation.id), []);
});

test('a quiet interpretation renders the served reason verbatim under its contract tag', async () => {
  const focus = fixture({
    insight: {
      ...fixture().insight,
      absence_class: 'evaluated-quiet',
      absence_reason: 'No supplied finding crosses the materiality threshold for this window.',
      evidence_line: null,
      prompt_version: 'deterministic-gate1',
      state: 'silent',
      text: null,
    },
  });
  render(
    <MetricFocusDialog
      metricId="spend"
      onClose={() => undefined}
      onMetricChange={() => undefined}
      surface={surfaceFor(focus)}
      surfaceKey="campaign-portfolio"
    />,
  );

  const dialog = await screen.findByRole('dialog');
  await waitFor(() => {
    assert.ok(dialog.textContent?.includes('No supplied finding crosses the materiality threshold for this window.'));
  });
  assert.ok(screen.getByText('Nothing noteworthy'));
  assert.ok(dialog.querySelector('.narr-card.quiet'));
  assert.equal(dialog.textContent?.includes('evaluated-quiet'), false);
});

test('a non-filled interpretation served without the absence pair renders the contract fallback line', async () => {
  const focus = fixture({
    insight: {
      ...fixture().insight,
      absence_class: null,
      absence_reason: null,
      evidence_line: null,
      state: 'unavailable',
      text: null,
    },
  });
  render(
    <MetricFocusDialog
      metricId="spend"
      onClose={() => undefined}
      onMetricChange={() => undefined}
      surface={surfaceFor(focus)}
      surfaceKey="campaign-portfolio"
    />,
  );

  const dialog = await screen.findByRole('dialog');
  await waitFor(() => {
    assert.ok(dialog.textContent?.includes('Nothing noteworthy this window.'));
  });
  // A non-filled interpretation claims no governed reading: no provenance strip.
  assert.equal(dialog.querySelector('.prov-strip'), null);
});


test('a filled interpretation renders the customer-word provenance strip', async () => {
  render(
    <MetricFocusDialog
      metricId="spend"
      onClose={() => undefined}
      onMetricChange={() => undefined}
      surface={surfaceFor(fixture())}
      surfaceKey="campaign-portfolio"
    />,
  );

  const dialog = await screen.findByRole('dialog');
  await waitFor(() => {
    assert.ok(dialog.textContent?.includes('One governed reading of this view'));
  });
  // Customer words only: no prompt version or cache vocabulary anywhere.
  assert.equal(dialog.textContent?.includes('cached against'), false);
});


test('parent cache scope change invalidates a same-window metric focus', async () => {
  const cache = new Map<string, MetricFocusV4>();
  const first = fixture();
  const second = fixture({ current: { ...first.current, display_value: '999.00', value: '999' } });
  const firstFetch = vi.fn(() => Promise.resolve(first));
  const secondFetch = vi.fn(() => Promise.resolve(second));
  const firstSurface = { ...surfaceFor(first), cache, cacheScope: 'revision-one', fetch: firstFetch };
  const rendered = render(
    <MetricFocusDialog metricId="spend" onClose={() => undefined} onMetricChange={() => undefined} surface={firstSurface} surfaceKey="campaign-portfolio" />,
  );
  const dialog = await screen.findByRole('dialog');
  await waitFor(() => {
    assert.ok(dialog.querySelector('.big')?.textContent?.includes('1,000'));
  });

  rendered.rerender(
    <MetricFocusDialog metricId="spend" onClose={() => undefined} onMetricChange={() => undefined} surface={{ ...surfaceFor(second), cache, cacheScope: 'revision-two', fetch: secondFetch }} surfaceKey="campaign-portfolio" />,
  );

  await waitFor(() => {
    assert.ok(dialog.querySelector('.big')?.textContent?.includes('999'));
  });
  assert.equal(firstFetch.mock.calls.length, 1);
  assert.equal(secondFetch.mock.calls.length, 1);
});

function renderFocus(focus: MetricFocusV4) {
  render(
    <MetricFocusDialog
      metricId="spend"
      onClose={() => undefined}
      onMetricChange={() => undefined}
      surface={surfaceFor(focus)}
      surfaceKey="campaign-portfolio"
    />,
  );
}

test('the head shows the two-place display value while the exact table keeps four places', async () => {
  const base = fixture();
  renderFocus(fixture({
    current: { ...base.current, display_value: '75459.64', value: '75459.6400' },
    series: {
      display_grain: 'day',
      points: [
        { current: '75459.6400', date: '2026-08-10', prior: null },
        { current: '0.0000', date: '2026-08-16', prior: null },
      ],
    },
  }));
  const dialog = await screen.findByRole('dialog');
  await waitFor(() => {
    assert.equal(dialog.querySelector('.big')?.textContent, '75,459.64 USD');
  });
  assert.equal(dialog.querySelector('.prior')?.textContent, 'Aligned prior: 900.00 USD');
  // The exact-values disclosure is unchanged: the served value at its own scale.
  assert.ok(dialog.querySelector('details.exact')?.textContent?.includes('75,459.6400 USD'));
});

test('a currency driver renders its served two-place change under the one sign rule', async () => {
  renderFocus(fixture({
    drivers: {
      entries: [
        { change: '12.3', delta: changeBlock('12.3', '+1.00%'), display_change: '12.30', name: 'Fictional Rise', share_of_move: null, units: usd, chip: null, moves_against_basis: false },
        { change: '-1234.5', delta: changeBlock('-1234.5', '-2.00%'), display_change: '-1234.50', name: 'Fictional Fall', share_of_move: null, units: usd, chip: null, moves_against_basis: false },
        { change: '0.004', delta: changeBlock('0.004', '+0.00%'), display_change: '0.00', name: 'Fictional Flat', share_of_move: null, units: usd, chip: null, moves_against_basis: false },
      ],
      grain: 'campaign',
      scope: { count: 3, label: 'Largest changes among all 3 campaigns in the portfolio', population: 3, share_suffix: 'of the overall change' },
      share_basis: 'portfolio',
    },
  }));
  const dialog = await screen.findByRole('dialog');
  await waitFor(() => {
    assert.ok(dialog.textContent?.includes('Fictional Rise'));
  });
  const what = (name: string) => Array.from(dialog.querySelectorAll('.mover'))
    .find((mover) => mover.querySelector('.who')?.textContent === name)?.querySelector('.what')?.textContent;
  // USD changes of +12.30, -1,234.50, and one under half a cent that rounds to 0.00 (unsigned).
  assert.equal(what('Fictional Rise'), '+12.30 USD');
  assert.equal(what('Fictional Fall'), '-1,234.50 USD');
  assert.equal(what('Fictional Flat'), '0.00 USD');
});

test('a Clicks driver under a CPC focus renders its raw count without a currency code', async () => {
  renderFocus(fixture({
    drivers: {
      entries: [
        { change: '2.0000', delta: changeBlock('2.0000', '+20.00%'), display_change: '2.00', name: 'Spend', share_of_move: null, units: usd, chip: null, moves_against_basis: false },
        { change: '10', delta: changeBlock('10', '+50.00%'), display_change: null, name: 'Clicks', share_of_move: null, units: count, chip: null, moves_against_basis: false },
      ],
      grain: 'ratio_component',
      scope: null,
      share_basis: null,
    },
    drivers_summary: null,
  }));
  const dialog = await screen.findByRole('dialog');
  await waitFor(() => {
    assert.ok(dialog.textContent?.includes('Clicks'));
  });
  const movers = Array.from(dialog.querySelectorAll('.mover .what')).map((node) => node.textContent);
  // Spend moves by 2.00 USD; clicks move by 10 (a count, no code).
  assert.deepEqual(movers, ['+2.00 USD', '+10']);
});

const usdEntry = (name: string, change: string, share: string | null, chip: string | null, against: boolean) => ({
  change,
  chip,
  delta: changeBlock('1', null),
  display_change: Number(change).toFixed(2),
  moves_against_basis: against,
  name,
  share_of_move: share,
  units: usd,
});

function scoped(basis: 'portfolio' | 'scope' | 'none', suffix: string | null, lines: readonly string[], entries: ReturnType<typeof usdEntry>[]): MetricFocusV4 {
  return fixture({
    drivers: {
      entries,
      grain: 'campaign',
      scope: { count: 4, label: 'Largest changes among the 4 campaigns with the most ad spend in this window, of 60 in the portfolio', population: 60, share_suffix: suffix },
      share_basis: basis,
    },
    drivers_summary: {
      fell: 2,
      flat: 0,
      lines,
      listed_change: { display: '1,970.00 USD', value: '1970' },
      other_in_scope_change: null,
      outside_scope_change: { display: '-6,846.00 USD', value: '-6846' },
      rose: 2,
    },
  });
}

async function moverTexts(): Promise<Record<string, string | null | undefined>> {
  const dialog = await screen.findByRole('dialog');
  await waitFor(() => {
    assert.ok(dialog.querySelector('.mover'));
  });
  return Object.fromEntries(Array.from(dialog.querySelectorAll('.mover')).map((mover) => [
    mover.querySelector('.who')?.textContent ?? '',
    mover.querySelector('.what')?.textContent,
  ]));
}

test('caveat 1 under the scope basis: chips name the in-scope change, shares their suffix', async () => {
  const scope = 'the change across these 4 campaigns';
  renderFocus(scoped('scope', `of ${scope}`, [
    'Largest changes among the 4 campaigns with the most ad spend in this window, of 60 in the portfolio',
    'The 4 listed campaigns: 1,970.00 USD',
    'The 56 campaigns not among these 4: -6,846.00 USD',
    'Among these 4: 2 rose, 2 fell, 0 were flat',
  ], [
    usdEntry('Fictional A', '2500', null, `Larger than ${scope}`, false),
    usdEntry('Fictional B', '900', '46%', null, false),
    usdEntry('Fictional C', '-978', null, `Moved against ${scope}`, true),
  ]));
  const what = await moverTexts();
  assert.equal(what['Fictional A'], `+2,500.00 USD · Larger than ${scope}`);
  assert.equal(what['Fictional B'], `+900.00 USD · 46% of ${scope}`);
  assert.equal(what['Fictional C'], `-978.00 USD · Moved against ${scope}`);
  const dialog = screen.getByRole('dialog');
  // The scope label sits above the list, once; the summary block renders the other lines.
  assert.equal(dialog.querySelectorAll('.sub-note').length >= 1, true);
  const summary = Array.from(dialog.querySelectorAll('.drivers-summary p')).map((node) => node.textContent);
  assert.deepEqual(summary, [
    'The 4 listed campaigns: 1,970.00 USD',
    'The 56 campaigns not among these 4: -6,846.00 USD',
    'Among these 4: 2 rose, 2 fell, 0 were flat',
  ]);
});

test('under the portfolio basis the chips and shares name the overall change', async () => {
  renderFocus(scoped('portfolio', 'of the overall change', ['Largest changes', 'The 2 listed campaigns: 500.00 USD'], [
    usdEntry('Fictional A', '800', null, 'Larger than the overall change', false),
    usdEntry('Fictional B', '-100', null, 'Moved against the overall change', true),
    usdEntry('Fictional C', '300', '40%', null, false),
  ]));
  const what = await moverTexts();
  assert.equal(what['Fictional A'], '+800.00 USD · Larger than the overall change');
  assert.equal(what['Fictional B'], '-100.00 USD · Moved against the overall change');
  assert.equal(what['Fictional C'], '+300.00 USD · 40% of the overall change');
});

test('under the none basis no share renders and the cancel-out line explains why', async () => {
  const cancel = "Shares aren't shown because the changes among these 4 campaigns cancel out.";
  renderFocus(scoped('none', null, ['Largest changes', cancel], [
    usdEntry('Fictional A', '100', null, null, false),
    usdEntry('Fictional B', '-100', null, 'Moved against the overall change', true),
  ]));
  const what = await moverTexts();
  assert.equal(what['Fictional A'], '+100.00 USD');
  assert.ok(screen.getByRole('dialog').textContent?.includes(cancel));
});

test('an optional served preamble is the first dialog body content', async () => {
  const surface = {
    ...surfaceFor(fixture()),
    preambleFor: (metricId: string) => <div role="note"><p>Served caveat for {metricId}.</p></div>,
  };
  render(
    <MetricFocusDialog
      metricId="spend"
      onClose={() => undefined}
      onMetricChange={() => undefined}
      surface={surface}
      surfaceKey="campaign-portfolio"
    />,
  );

  const dialog = await screen.findByRole('dialog');
  const body = dialog.querySelector('.dialog-body');
  assert.equal(body?.firstElementChild?.getAttribute('role'), 'note');
  assert.equal(body?.firstElementChild?.textContent, 'Served caveat for spend.');
});

test('a ratio-component focus renders no scope label and no summary block', async () => {
  renderFocus(fixture({
    drivers: {
      entries: [usdEntry('Spend', '2', null, null, false)],
      grain: 'ratio_component',
      scope: null,
      share_basis: null,
    },
    drivers_summary: null,
  }));
  await moverTexts();
  const dialog = screen.getByRole('dialog');
  assert.equal(dialog.querySelector('.drivers-summary'), null);
  assert.equal(dialog.querySelector('.drivers .sub-note'), null);
});
