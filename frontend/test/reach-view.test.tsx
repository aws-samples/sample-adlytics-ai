// SPDX-License-Identifier: MIT-0
/** Reach visual states: served text only, caveat placement, semantics, focus, layouts, and axe. */
import assert from 'node:assert/strict';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, test } from 'vitest';

import { ReachAndFunnelView } from '../components/ReachAndFunnelView';
import { parseReachAndFunnelArtifact } from '../lib/reach';
import {
  cloneReachFixture,
  reachChangeUnavailableFixture,
  reachCompleteCoarseFixture,
  reachCompleteFineFixture,
  reachCurrencyMismatchFixture,
  reachEmptyFixture,
  reachInsufficientFixture,
  reachNeedsCampaignsFixture,
  reachNeedsDspFixture,
  reachNoPriorFixture,
  reachNoSalesFixture,
  reachNoShoppersFixture,
  reachNoSpendFixture,
  reachNotReturnedFixture,
  reachPackVariantMismatchFixture,
  reachPendingFixture,
  reachQuarterFixture,
  reachWeekFixture,
} from './fixtures/reach-v1-fixtures';

afterEach(cleanup);

const view = (fixture: unknown = reachCompleteFineFixture) => render(<ReachAndFunnelView artifact={parseReachAndFunnelArtifact(fixture)} />);

const FUNNEL_CAVEAT = [
  'This shows association, not cause. Shoppers were not randomly assigned to groups.',
  'Shoppers can see an ad after they buy, so groups that include DSP retargeting may look stronger.',
  'Sponsored ads appear when shoppers search, so sponsored groups start closer to buying.',
] as const;
const FREQUENCY_CAVEAT = [
  ...FUNNEL_CAVEAT,
  'Shoppers can see more ads after they buy, so higher buckets may look stronger.',
] as const;

function assertInOrder(text: string, values: readonly string[]) {
  let previous = -1;
  for (const value of values) {
    const index = text.indexOf(value);
    assert.ok(index > previous, `${value} should follow the preceding text`);
    previous = index;
  }
}

test('renders the complete surface in section order using only served display text', () => {
  const { container } = view();
  const headings = Array.from(container.querySelectorAll('.reach-view > .reach-section > h2')).map((node) => node.textContent);
  assert.deepEqual(headings, ['Overlap between DSP and sponsored ads', 'Full funnel by exposure group', 'Insights', 'Frequency']);
  assert.ok(container.textContent?.includes('30.00%'));
  assert.ok(container.textContent?.includes('2000 served'));
  assert.ok(container.textContent?.includes('2.50 USD'));
  assert.ok(container.textContent?.includes('2.50x'));
  assert.ok(container.textContent?.includes('4.00%'));
  assert.equal(container.textContent?.includes('0.9876'), false);
  assert.equal(container.textContent?.includes('9.8765'), false);
  assert.equal(container.textContent?.includes('$'), false);
  assert.equal(container.textContent?.includes('×'), false);

  const notes = container.querySelectorAll('.reach-caveat');
  assert.equal(notes.length, 2);
  assertInOrder(notes[0]?.textContent ?? '', FUNNEL_CAVEAT);
  assertInOrder(notes[1]?.textContent ?? '', FREQUENCY_CAVEAT);
  assert.ok(notes[0]?.nextElementSibling?.classList.contains('dt-wrap'));
  assert.ok(notes[1]?.nextElementSibling?.textContent?.includes('Ads seen within 14 days'));
});

test('the overlap table gives every value its group and server-owned identity-space heading', () => {
  view();
  const region = screen.getByRole('region', { name: 'Amazon account IDs' });
  assert.ok(within(region).getByRole('columnheader', { name: 'All ID types' }));
  assert.ok(screen.getByText('Saw both within 14 days of their first ad · all ID types · overlap is understated'));
  assert.equal(screen.getAllByText('Shoppers reached in September 2026, grouped by the ads they saw within 14 days of their first ad in that month.').length, 1);

  for (const row of within(region).getAllByRole('row').slice(1)) {
    const rowHeader = within(row).getByRole('rowheader');
    const cells = within(row).getAllByRole('cell');
    for (const [index, cell] of cells.entries()) {
      const headings = (cell.getAttribute('headers') ?? '')
        .split(' ')
        .map((id) => document.getElementById(id)?.textContent);
      assert.deepEqual(headings, [rowHeader.textContent, index === 0 ? 'Amazon account IDs' : 'All ID types']);
    }
  }
});

test('renders fine and coarse server bucket rosters without browser-composed bands or totals', () => {
  const fine = view();
  assert.equal(fine.container.querySelectorAll('.reach-chart-item').length, 10);
  assert.equal(fine.container.querySelectorAll('.reach-frequency-tables tbody tr').length, 12);
  assert.equal(screen.queryByText(/total/i), null);
  fine.unmount();

  const coarse = view(reachCompleteCoarseFixture);
  assert.equal(coarse.container.querySelectorAll('.reach-chart-item').length, 6);
  assert.ok(coarse.container.textContent?.includes('4 to 6'));
  assert.ok(coarse.container.textContent?.includes('7 to 9'));
  assert.equal(coarse.container.textContent?.includes('4 ads'), false);
  assert.equal(screen.queryByText(/total/i), null);
});

test('not-returned buckets leave a chart gap and state the exact absence in chart and tables', () => {
  const { container } = view(reachNotReturnedFixture);
  const gap = container.querySelector('.reach-chart-item.gap');
  assert.ok(gap);
  assert.equal(gap.querySelector('.reach-bar'), null);
  assert.ok(gap.textContent?.includes('Not available'));
  assert.ok(gap.textContent?.includes('too few shoppers, or none'));
  assert.ok(screen.getAllByText('too few shoppers, or none').length >= 3);
});

test('keeps no-credit channel rows separate and places each served platform return beside them', () => {
  const { container } = view();
  const regions = screen.getAllByRole('region');
  const channelRegion = regions.find((region) => region.getAttribute('aria-labelledby')?.split(' ').some((id) => document.getElementById(id)?.textContent === 'Return per channel by frequency'));
  const platformRegion = regions.find((region) => region.getAttribute('aria-labelledby')?.split(' ').some((id) => document.getElementById(id)?.textContent === 'Platform return'));
  assert.ok(channelRegion);
  assert.ok(platformRegion);
  assert.deepEqual(within(channelRegion).getAllByRole('rowheader').map((cell) => cell.textContent), ['Sponsored Products', 'Amazon DSP']);
  assert.equal(within(channelRegion).getAllByText('Not available per channel').length, 20);
  assert.equal(within(platformRegion).getAllByRole('rowheader').length, 10);
  assert.equal(container.querySelector('.reach-frequency-tables')?.children.length, 2);
  assert.equal(screen.queryByText(/total/i), null);
  assert.ok(container.textContent?.includes("Attributed sales aren't added across channels, because one purchase can be credited to more than one channel."));
});

test('renders every page and section absence without leaking a machine key', () => {
  const cases: readonly [unknown, readonly string[]][] = [
    [reachEmptyFixture, ['Not available']],
    [reachInsufficientFixture, ['Not available', 'too few shoppers, or none']],
    [reachNeedsDspFixture, ['Needs DSP and sponsored ads on one AMC instance']],
    [reachNeedsCampaignsFixture, ['Needs at least 4 active campaigns']],
    [reachPendingFixture, ['Figures for September 2026 are expected after 16 Oct 2026.']],
    [reachQuarterFixture, ['Overlap is measured for complete weeks and months. Choose Latest measured month to see it.', 'Full funnel is measured for complete weeks and months. Choose Latest measured month to see it.']],
    [reachWeekFixture, ['Frequency is measured for complete months and quarters. Choose Latest measured month to see it.']],
    [reachNoShoppersFixture, ['No shoppers were reached in this group.', "No change is shown, because a figure isn't available."]],
    [reachNoSalesFixture, ['No sales were recorded for this group.']],
    [reachNoSpendFixture, ['No spend was recorded for these shoppers.']],
    [reachCurrencyMismatchFixture, ["Spend and sales are in different currencies, so return isn't shown."]],
    [reachNoPriorFixture, ["The previous month isn't available."]],
    [reachChangeUnavailableFixture, ["No change is shown, because a figure isn't available."]],
    [reachPackVariantMismatchFixture, ["No change is shown, because a figure isn't available."]],
  ];
  for (const [fixture, lines] of cases) {
    const rendered = view(fixture);
    for (const line of lines) assert.ok(rendered.container.textContent?.includes(line), line);
    assert.equal(rendered.container.textContent?.includes('fictional_market'), false);
    rendered.unmount();
  }
});

test('matching insight dialogs lead with all served caveats and restore keyboard focus', async () => {
  const user = userEvent.setup();
  view();
  assert.ok(screen.getByRole('button', { name: 'Read more: Overlap between DSP and sponsored ads' }));
  const funnelOpener = screen.getByRole('button', { name: 'Read more: Association, not cause' });
  await user.click(funnelOpener);
  let dialog = screen.getByRole('dialog');
  const body = dialog.querySelector('.dialog-body');
  assert.ok(body?.firstElementChild?.classList.contains('reach-caveat'));
  assertInOrder(body?.firstElementChild?.textContent ?? '', FUNNEL_CAVEAT);
  await user.keyboard('{Escape}');
  assert.equal(document.activeElement, funnelOpener);

  const frequencyOpener = screen.getByRole('button', { name: 'Read more: Frequency' });
  await user.click(frequencyOpener);
  dialog = screen.getByRole('dialog');
  assertInOrder(dialog.querySelector('.dialog-body')?.firstElementChild?.textContent ?? '', FREQUENCY_CAVEAT);
});

test('the values disclosure and boundary remain keyboard operable', () => {
  view();
  const values = screen.getByText('Show values').closest('summary');
  const about = screen.getByText('About these figures').closest('summary');
  assert.ok(values);
  assert.ok(about);
  values.focus();
  assert.equal(document.activeElement, values);
  fireEvent.click(values);
  assert.equal(values.parentElement?.hasAttribute('open'), true);
  about.focus();
  assert.equal(document.activeElement, about);
  fireEvent.click(about);
  assert.equal(about.parentElement?.hasAttribute('open'), true);
});

function responseStrings(value: unknown, into = new Set<string>()): Set<string> {
  if (typeof value === 'string') into.add(value);
  else if (Array.isArray(value)) value.forEach((entry) => responseStrings(entry, into));
  else if (value !== null && typeof value === 'object') Object.values(value).forEach((entry) => responseStrings(entry, into));
  return into;
}

const BROWSER_COPY = new Set([
  'Overlap between DSP and sponsored ads', 'Amazon account IDs', 'Full funnel by exposure group',
  'Insights', 'Frequency', 'About these figures', 'Association, not cause', 'Not available',
  'Read more ›', 'Show values', 'Purchaser rate by number of ads seen', 'Ads seen',
  'Purchaser rate', 'Return per channel by frequency', 'Channel', 'Platform return',
]);

test('a same-response copy walk refuses invented or borrowed visible text', () => {
  const fixture = cloneReachFixture(reachCompleteFineFixture);
  const allowed = responseStrings(fixture);
  for (const line of BROWSER_COPY) allowed.add(line);
  const { container } = view(fixture);
  const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT);
  const unexpected: string[] = [];
  for (let node = walker.nextNode(); node !== null; node = walker.nextNode()) {
    const text = node.textContent?.trim() ?? '';
    if (text !== '' && !allowed.has(text)) unexpected.push(text);
  }
  assert.deepEqual(unexpected, []);
});

test('complete and absence surfaces are axe-clean in narrow and wide containers under both themes', async () => {
  const axe = await import('axe-core');
  for (const theme of ['dark', 'light'] as const) {
    for (const width of ['420px', '1100px'] as const) {
      for (const fixture of [reachCompleteFineFixture, reachNotReturnedFixture]) {
        document.documentElement.dataset.theme = theme;
        const rendered = render(<div style={{ width }}><ReachAndFunnelView artifact={parseReachAndFunnelArtifact(fixture)} /></div>);
        const results = await axe.default.run(rendered.container, { rules: { 'color-contrast': { enabled: false } } });
        assert.deepEqual(results.violations.map((violation) => violation.id), [], `${theme} ${width}`);
        rendered.unmount();
      }
    }
  }
  delete document.documentElement.dataset.theme;
  // Eight full-page axe runs exceed vitest's 5 s default when the gates share the machine.
}, 20_000);
