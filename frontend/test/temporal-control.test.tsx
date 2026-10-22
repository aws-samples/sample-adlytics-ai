// SPDX-License-Identifier: MIT-0
import assert from 'node:assert/strict';
import { fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { test } from 'vitest';

import { BuiltLine, PageGenerationContext } from '@/components/EvidenceStatusSheet';
import { TemporalControl } from '@/components/TemporalControl';
import type { TemporalControlV1, VintageControlV2 } from '@/lib/presentation-v2';

const control: TemporalControlV1 = {
  comparison: { policy: 'required_equal_prior', reason_code: null, status: 'available' },
  custom: { availability: 'available', bounds: { earliest_start: '2026-08-01', latest_end: '2026-08-21', max_duration_days: 90, min_duration_days: 1 }, reason_code: null },
  mode: 'selectable',
  presets: [
    { availability: 'available', id: '1d', label: '1D', reason_code: null },
    { availability: 'available', id: '7d', label: '7D', reason_code: null },
    { availability: 'unavailable', id: '14d', label: '14D', reason_code: 'preset_unavailable' },
    { availability: 'available', id: '30d', label: '30D', reason_code: null },
  ],
  resolved: { display_grain: 'period', reporting_window_status: 'preferred_default', selection: { kind: 'default', preset_id: null }, window: { end: '2026-08-21', start: '2026-08-15' } },
  version: 1,
};

test('a vintage control renders the server static label and no selector', () => {
  const vintage: VintageControlV2 = {
    mode: 'fixed',
    resolved: { as_of_month: '2026-09', label_text: 'Cohorts through September 2026', period_label: 'September 2026', window: { end: '2026-09-30', start: '2025-09-01' } },
    version: 2,
  };
  const view = render(<TemporalControl control={vintage} onSelect={() => undefined} />);
  assert.ok(screen.getByText('Cohorts through September 2026'));
  assert.equal(screen.queryByRole('combobox'), null);
  view.unmount();
  // Control: an unavailable vintage renders nothing.
  const empty = render(<TemporalControl control={{ ...vintage, resolved: null }} onSelect={() => undefined} />);
  assert.equal(empty.container.textContent, '');
});

test('returned temporal control submits only returned preset and Custom intent without exposing reason codes', async () => {
  const selections: unknown[] = []; const user = userEvent.setup();
  const view = render(<TemporalControl control={control} onSelect={(selection) => selections.push(selection)} />);
  assert.equal(screen.getByRole('radiogroup').getAttribute('aria-label'), 'Available reporting periods');
  assert.equal(screen.getByRole('radio', { name: '14 days' }).hasAttribute('disabled'), true);
  // The reason stays as the disabled option's accessible description, not visible prose (D5).
  const reason = document.getElementById(screen.getByRole('radio', { name: '14 days' }).getAttribute('aria-describedby') ?? '');
  assert.match(reason?.textContent ?? '', /14 days is not available for the currently published coverage\. Choose another available reporting window/);
  assert.equal(reason?.className, 'sr-only');
  assert.equal(view.container.textContent?.includes('preset_unavailable'), false);
  await user.click(screen.getByRole('radio', { name: '7 days' }));
  await user.click(screen.getByRole('button', { name: 'Custom period' }));
  fireEvent.change(screen.getByLabelText('Start'), { target: { value: '2026-08-16' } });
  fireEvent.change(screen.getByLabelText('End'), { target: { value: '2026-08-21' } });
  await user.click(screen.getByRole('button', { name: 'Apply' }));
  assert.deepEqual(selections, [{ kind: 'preset', preset_id: '7d' }, { end: '2026-08-21', kind: 'custom', start: '2026-08-16' }]);
  assert.equal(view.container.textContent?.includes('Server-resolved reporting scope'), false);
});

test('returned temporal control keeps a comparison refusal in plain language', () => {
  const view = render(<TemporalControl control={control} onSelect={() => undefined} refusal={{ reason: 'required_comparison_unavailable', recovery: 'Choose another returned period.' }} />);
  assert.match(screen.getByRole('status').textContent ?? '', /cannot be compared safely/);
  assert.equal(view.container.textContent?.includes('required_comparison_unavailable'), false);
});

test('returned Custom bounds enforce dates and inclusive duration before sending intent', async () => {
  const selections: unknown[] = []; const user = userEvent.setup();
  const bounded: TemporalControlV1 = { ...control, custom: { availability: 'available', bounds: { earliest_start: '2026-08-10', latest_end: '2026-08-21', max_duration_days: 5, min_duration_days: 3 }, reason_code: null } };
  render(<TemporalControl control={bounded} onSelect={(selection) => selections.push(selection)} />);
  await user.click(screen.getByRole('button', { name: 'Custom period' }));
  fireEvent.change(screen.getByLabelText('Start'), { target: { value: '2026-08-10' } });
  fireEvent.change(screen.getByLabelText('End'), { target: { value: '2026-08-11' } });
  await user.click(screen.getByRole('button', { name: 'Apply' }));
  assert.equal(selections.length, 0);
  assert.match(screen.getByRole('alert').textContent ?? '', /published bounds and allowed duration/);
  fireEvent.change(screen.getByLabelText('End'), { target: { value: '2026-08-12' } });
  await user.click(screen.getByRole('button', { name: 'Apply' }));
  assert.deepEqual(selections, [{ end: '2026-08-12', kind: 'custom', start: '2026-08-10' }]);
});

test('returned temporal control restores focus to the server-resolved selection', () => {
  let restored = 0;
  const selected: TemporalControlV1 = { ...control, resolved: { ...control.resolved, reporting_window_status: 'user_selected', selection: { kind: 'preset', preset_id: '7d' } } };
  render(<TemporalControl control={selected} onFocusRestored={() => { restored += 1; }} onSelect={() => undefined} restoreFocus />);
  assert.equal(document.activeElement, screen.getByRole('radio', { name: '7 days' }));
  assert.equal(restored, 1);
});

test('fixed returned controls offer a plain-language unavailable state without a Custom form', () => {
  const fixed: TemporalControlV1 = { ...control, custom: { availability: 'unavailable', bounds: null, reason_code: 'fixed_period' }, mode: 'fixed', presets: control.presets.map((preset) => ({ ...preset, availability: 'unavailable' as const, reason_code: 'fixed_period' })) };
  const view = render(<TemporalControl control={fixed} onSelect={() => undefined} />);
  assert.equal(screen.queryByRole('button', { name: 'Custom period' }), null);
  assert.equal(view.container.textContent?.includes('Custom reporting windows'), false);
  assert.equal(view.container.textContent?.includes('fixed_period'), false);
  assert.ok(screen.getAllByRole('radio').every((radio) => radio.hasAttribute('disabled')));
});

function footnote(view: ReturnType<typeof render>): string {
  return view.container.querySelector('.footnote.win-note')?.textContent ?? '';
}

test('the as-of footnote prints the window end only; the build time reads in the Coverage sheet (T1L-8)', () => {
  const view = render(<TemporalControl control={control} onSelect={() => undefined} />);
  assert.match(footnote(view), /^Aug 15–21, 2026 · 7 days · Data through Aug 21, 2026\.$/);
  assert.doesNotMatch(footnote(view), /built/);
  assert.equal(view.container.textContent?.includes('latest published coverage'), false);
  view.unmount();
});
test('the Coverage sheet prints the served build time from the page generation, in each form', () => {
  for (const [generation, text] of [
    [{ built_at: '2026-08-22T06:12:04Z' }, 'Built 2026-08-22 06:12:04 UTC.'],
    [{ built_before: '2026-10-09T21:06:31Z' }, 'Built before 2026-10-09 21:06:31 UTC.'],
  ] as const) {
    const view = render(<PageGenerationContext.Provider value={generation}><BuiltLine /></PageGenerationContext.Provider>);
    assert.equal(view.container.textContent, text);
    view.unmount();
  }
  const none = render(<BuiltLine />);
  assert.equal(none.container.textContent, '');
});
