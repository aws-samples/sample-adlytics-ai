// SPDX-License-Identifier: MIT-0
/**
 * Temporal transport v2 in the browser (ADR-100 point 8; UI slice 9a item 0.2): the strict
 * version-2 control decoder and the date control that renders it. Each accepted case sits
 * beside the refusal that must fail on the guarded defect.
 */
import assert from 'node:assert/strict';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { test } from 'vitest';

import { DateControlV2, presetDetail, TemporalControl } from '@/components/TemporalControl';
import { bindingByRoute, pageBindings, parsePageBindings, type PageBinding } from '@/lib/page-bindings';
import { parsePresentationResultCalendarV2, parsePresentationResultV2, parseTemporalControlV2, type TemporalControlV2 } from '@/lib/presentation-v2';
import generated from '../generated/page-bindings.json';
import pending from './fixtures/scorecard-pending-v2.json';

/** A synthetic page binding that declares the Scorecard's four presets. */
const calendarBinding: PageBinding = {
  ...bindingByRoute.campaigns,
  temporal_policy: {
    ...bindingByRoute.campaigns.temporal_policy,
    candidate_presets: ['7d', '30d', 'last_complete_week', 'last_complete_month'],
    comparison_intent: null,
    comparison_policy: 'optional_equal_prior',
    default_preset: '7d',
    preset_exceptions: [],
  },
};

function wire(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    comparison: { policy: 'optional_equal_prior', reason_code: null, status: 'available' },
    custom: { availability: 'unavailable', bounds: null, reason_code: 'custom_disabled' },
    mode: 'selectable',
    presets: [
      { availability: 'available', id: '7d', label: '7 days', period_label: null, reason_code: null, reason_text: null, window: { end: '2026-09-15', start: '2026-09-09' } },
      { availability: 'available', id: '30d', label: '30 days', period_label: null, reason_code: null, reason_text: null, window: { end: '2026-09-15', start: '2026-08-17' } },
      { availability: 'available', id: 'last_complete_week', label: 'Latest measured week', period_label: 'Week of 8–14 Sep 2026', reason_code: null, reason_text: null, window: { end: '2026-09-14', start: '2026-09-08' } },
      { availability: 'unavailable', id: 'last_complete_month', label: 'Latest measured month', period_label: null, reason_code: 'preset_unavailable', reason_text: 'Not available yet', window: null },
    ],
    resolved: {
      as_of_month: null,
      display_grain: 'period',
      period_label: null,
      reporting_window_status: 'preferred_default',
      selection: { kind: 'default', preset_id: null },
      window: { end: '2026-09-15', start: '2026-09-09' },
    },
    version: 2,
    ...overrides,
  };
}

function monthWire(): Record<string, unknown> {
  const base = wire();
  const presets = (base.presets as Record<string, unknown>[]).map((preset) => preset.id === 'last_complete_month'
    ? { ...preset, availability: 'available', period_label: 'August 2026', reason_code: null, reason_text: null, window: { end: '2026-08-31', start: '2026-08-01' } }
    : preset);
  return {
    ...base,
    comparison: { policy: 'optional_equal_prior', reason_code: 'calendar_period_selected', status: 'not_applicable' },
    presets,
    resolved: {
      as_of_month: null,
      display_grain: 'month',
      period_label: 'August 2026',
      reporting_window_status: 'user_selected',
      selection: { kind: 'preset', preset_id: 'last_complete_month' },
      window: { end: '2026-08-31', start: '2026-08-01' },
    },
  };
}

test('the version-2 decoder accepts the declared candidates in canonical order', () => {
  const control = parseTemporalControlV2(wire(), calendarBinding);
  assert.deepEqual(control.presets.map((preset) => preset.id), ['7d', '30d', 'last_complete_week', 'last_complete_month']);
  assert.equal(control.presets[3]?.reason_text, 'Not available yet');
  const month = parseTemporalControlV2(monthWire(), calendarBinding);
  assert.equal(month.resolved.period_label, 'August 2026');
  assert.equal(month.comparison.policy, 'optional_equal_prior');
  if (month.comparison.policy !== 'optional_equal_prior') throw new Error('expected optional equal-prior comparison');
  assert.equal(month.comparison.reason_code, 'calendar_period_selected');
});

test('the version-2 decoder refuses drift from the declaration and the calendar rules', () => {
  const presets = wire().presets as Record<string, unknown>[];
  const refusals: Record<string, unknown>[] = [
    wire({ version: 1 }),
    // Out of canonical order.
    wire({ presets: [presets[1], presets[0], presets[2], presets[3]] }),
    // A preset the page doesn't declare.
    wire({ presets: [...presets.slice(0, 3), { ...presets[3], id: 'last_complete_quarter', label: 'Latest measured quarter' }] }),
    // A preset with no label. The label itself is the server's and renders as served (G9A-8).
    wire({ presets: [{ ...presets[0], label: '' }, ...presets.slice(1)] }),
    // An unavailable preset with no server text, so the browser would have to map a code.
    wire({ presets: [...presets.slice(0, 3), { ...presets[3], reason_text: null }] }),
    // A calendar preset's window without its period label.
    wire({ presets: [...presets.slice(0, 2), { ...presets[2], period_label: null }, presets[3]] }),
    // The calendar comparison state on a rolling window.
    wire({ comparison: { policy: 'optional_equal_prior', reason_code: 'calendar_period_selected', status: 'not_applicable' } }),
    // A calendar period without its label.
    { ...monthWire(), resolved: { ...(monthWire().resolved as Record<string, unknown>), period_label: null } },
    // A comparison policy other than the declared one.
    wire({ comparison: { policy: 'required_equal_prior', reason_code: null, status: 'available' } }),
    // Custom offered on a baked page.
    wire({ custom: { availability: 'available', bounds: { earliest_start: '2026-08-01', latest_end: '2026-09-15', max_duration_days: 90, min_duration_days: 1 }, reason_code: null } }),
  ];
  for (const candidate of refusals) {
    assert.throws(() => parseTemporalControlV2(candidate, calendarBinding), /./, JSON.stringify(candidate).slice(0, 120));
  }
});

test('a version-1 page still refuses a version-2 control and any calendar preset', () => {
  const page = {
    envelope_version: 2,
    kind: 'window_unavailable',
    reason_code: 'preset_unavailable',
    recovery: { action_id: null, no_self_service_reason: 'wait_for_source_coverage' },
    session_id: 'session',
    temporal: wire(),
  };
  assert.throws(() => parsePresentationResultV2(page, 'campaigns'));
});

test('the bindings carry transport version 2 and refuse a calendar preset under version 1', () => {
  assert.equal((generated as { temporal_control_version: number }).temporal_control_version, 2);
  assert.ok(pageBindings.length > 0);
  const document = generated as unknown as Record<string, unknown>;
  const pages = document.pages as Record<string, unknown>[];
  const withCalendar = pages.map((page, index) => index === 0
    ? { ...page, temporal_policy: { ...(page.temporal_policy as Record<string, unknown>), candidate_presets: [...((page.temporal_policy as { candidate_presets: string[] }).candidate_presets), 'last_complete_month'] } }
    : page);
  assert.doesNotThrow(() => parsePageBindings({ ...document, pages: withCalendar, temporal_control_version: 2 }));
  assert.throws(() => parsePageBindings({ ...document, pages: withCalendar, temporal_control_version: 1 }), /Calendar presets need temporal control version 2/);
});

test('the date control lists rolling presets, a divider, then calendar presets with their dates or period', () => {
  const control = parseTemporalControlV2(monthWire(), calendarBinding);
  const view = render(<DateControlV2 control={control} defaultPreset="7d" onSelect={() => undefined} />);
  const group = screen.getByRole('group', { name: 'Window' });
  const options = within(group).getAllByRole('radio');
  assert.deepEqual(options.map((option) => option.getAttribute('value')), ['7d', '30d', 'last_complete_week', 'last_complete_month']);
  const labels = [...group.querySelectorAll('.seg > *')].map((node) => node.classList.contains('seg-div') ? '|' : node.querySelector('.l')?.textContent);
  assert.deepEqual(labels, ['7 days', '30 days', '|', 'Latest measured week', 'Latest measured month']);
  assert.equal(within(group).getByRole('radio', { name: /Latest measured month/ }).hasAttribute('checked') || (within(group).getByRole('radio', { name: /Latest measured month/ }) as HTMLInputElement).checked, true);
  assert.match(group.textContent ?? '', /Sep 9–15/);
  assert.match(group.textContent ?? '', /Aug 17–Sep 15/);
  assert.match(group.textContent ?? '', /Sep 8–14/);
  assert.match(group.textContent ?? '', /August 2026/);
  // The narrow select names the same choices, label, dates and period.
  const select = screen.getByLabelText('Window', { selector: 'select' });
  assert.deepEqual([...select.querySelectorAll('option')].map((option) => option.textContent), [
    '7 days · Sep 9–15', '30 days · Aug 17–Sep 15', 'Latest measured week · Sep 8–14', 'Latest measured month · August 2026',
  ]);
  assert.equal(view.container.textContent?.includes('calendar_period_selected'), false);
});

test('an unavailable preset shows its server reason inside its option and cannot be chosen', async () => {
  const selections: unknown[] = [];
  const user = userEvent.setup();
  const control = parseTemporalControlV2(wire(), calendarBinding);
  const view = render(<TemporalControl control={control} defaultPreset="7d" onSelect={(selection) => selections.push(selection)} />);
  const month = screen.getByRole('radio', { name: /Latest measured month/ });
  assert.equal(month.hasAttribute('disabled'), true);
  assert.equal(month.closest('label')?.querySelector('.reason')?.textContent, 'Not available yet');
  assert.equal(view.container.textContent?.includes('preset_unavailable'), false);
  // An omitted selection marks the page's declared default.
  assert.equal((screen.getByRole('radio', { name: /^7 days/ }) as HTMLInputElement).checked, true);
  await user.click(month);
  await user.click(screen.getByRole('radio', { name: /^30 days/ }));
  // Arrow keys move through the native radio group and select the next enabled option.
  await user.keyboard('{ArrowRight}');
  assert.deepEqual(selections, [{ kind: 'preset', preset_id: '30d' }, { kind: 'preset', preset_id: 'last_complete_week' }]);
  // Paired: the version-1 control on the same component keeps its own rendering.
  view.unmount();
  const v1 = render(<TemporalControl control={{
    comparison: { policy: 'required_equal_prior', reason_code: null, status: 'available' },
    custom: { availability: 'unavailable', bounds: null, reason_code: 'custom_disabled' },
    mode: 'selectable',
    presets: [
      { availability: 'available', id: '1d', label: '1D', reason_code: null },
      { availability: 'available', id: '7d', label: '7D', reason_code: null },
      { availability: 'available', id: '14d', label: '14D', reason_code: null },
      { availability: 'unavailable', id: '30d', label: '30D', reason_code: 'preset_unavailable' },
    ],
    resolved: { display_grain: 'day', reporting_window_status: 'preferred_default', selection: { kind: 'default', preset_id: null }, window: { end: '2026-09-15', start: '2026-09-09' } },
    version: 1,
  }} onSelect={() => undefined} />);
  assert.equal(v1.container.querySelector('.seg-opt'), null);
  assert.ok(screen.getByRole('radiogroup', { name: 'Available reporting periods' }));
});

test('a week reads as its dates and a month as its server period label', () => {
  const control: TemporalControlV2 = parseTemporalControlV2(monthWire(), calendarBinding);
  assert.equal(presetDetail(control.presets[2]!), 'Sep 8–14');
  assert.equal(presetDetail(control.presets[3]!), 'August 2026');
});

test('restoring focus on an unavailable resolved preset falls to a choosable option, never nowhere', () => {
  // The served pending response: the resolved month is unavailable, so its radio is disabled.
  const served = parsePresentationResultCalendarV2(pending, 'scorecard');
  const control = served.temporal;
  // Emulate the wide layout jsdom can't compute: radios laid out, the narrow select hidden.
  const descriptor = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'offsetParent');
  Object.defineProperty(HTMLElement.prototype, 'offsetParent', {
    configurable: true,
    get(this: HTMLElement) { return this.tagName === 'SELECT' ? null : document.body; },
  });
  try {
    let restored = 0;
    render(<DateControlV2 control={control} defaultPreset="7d" onFocusRestored={() => { restored += 1; }} onSelect={() => undefined} restoreFocus />);
    const month = screen.getByRole('radio', { name: /Latest measured month/ }) as HTMLInputElement;
    assert.equal(month.checked && month.disabled, true);
    // Focus lands on the first option the user can choose (7 days), not on the disabled month.
    assert.equal((document.activeElement as HTMLInputElement | null)?.value, '7d');
    assert.equal(restored, 1);
  } finally {
    if (descriptor === undefined) delete (HTMLElement.prototype as { offsetParent?: unknown }).offsetParent;
    else Object.defineProperty(HTMLElement.prototype, 'offsetParent', descriptor);
  }
});
