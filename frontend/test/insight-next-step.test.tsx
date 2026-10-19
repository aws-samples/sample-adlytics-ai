// SPDX-License-Identifier: MIT-0
/** FR-11 card next steps, `NextStep.v2` (ADR-122): the strict decoder and the served control. */
import assert from 'node:assert/strict';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState, type ReactNode } from 'react';
import { test, vi } from 'vitest';
import { closeModalForAssistant, Dialog } from '../components/Dialog';
import { InsightCard, InsightNextStepControl, type InsightSlotView } from '../components/InsightCard';
import { PresetStepContext, VisibleTabsContext } from '../components/NextStepContext';
import { type InsightNextStep, NEXT_STEP_KINDS, openPageHref, parseNextStep, slotNextStep } from '../lib/insight-next-step';
import type { ManifestTab } from '../lib/route-manifest';

const QUESTION = 'Why did ad spend fall from Sep 2–8 to Sep 9–15?';
const step: InsightNextStep = {
  kind: 'open_preset',
  label: 'Show 14 days',
  reason: 'The 14-day view shows whether the drop persists.',
  target: { page_id: 'campaigns', preset_id: '14d' },
};

/** One fictional served target per kind, in the v2 grammar. */
const TARGETS: { readonly [K in InsightNextStep['kind']]: unknown } = {
  open_page: { page_id: 'retail-health', preset_id: '14d' },
  open_preset: { page_id: 'campaigns', preset_id: '14d' },
  open_campaign_detail: { page_id: 'campaigns', campaign_ref: 'capability-fictional' },
  open_product_detail: { selection_reference: 'product-fictional' },
  open_metric_focus: { page_id: 'campaigns', metric: 'spend' },
  open_evidence_status: null,
  ask_assistant: { question: QUESTION },
};

const TABS: readonly ManifestTab[] = [
  { description: 'Every campaign', entitlement: null, kind: 'page', label: 'Campaigns', order: 2, route: '/campaigns/', tab_id: 'campaigns' },
  { description: 'Retail actuals', entitlement: null, kind: 'page', label: 'Retail health', order: 1, route: '/retail-health/', tab_id: 'retail-health' },
];

function filled(nextStep: InsightNextStep | null): InsightSlotView {
  return {
    absence_class: null,
    absence_reason: null,
    evidence_line: 'Read from the values this page already shows.',
    next_step: nextStep,
    output_class: 'perspective',
    state: 'filled',
    text: 'A fictional reading.',
  };
}

function served(kind: InsightNextStep['kind'], label = `Label ${kind}`): InsightNextStep {
  return parseNextStep({ kind, label, reason: 'A fictional reason.', target: TARGETS[kind] });
}

test('every kind decodes its own closed target, and a missing or extra key fails', () => {
  for (const kind of NEXT_STEP_KINDS) {
    const value: Record<string, unknown> = { ...step, kind, target: TARGETS[kind] };
    assert.deepEqual(parseNextStep(value), value);
  }
  const missing = Object.fromEntries(Object.entries(step).filter(([key]) => key !== 'label'));
  for (const broken of [missing, { ...step, extra: 1 }, { ...step, kind: 'change_budget' }]) {
    assert.throws(() => parseNextStep(broken));
  }
  // v1's bare-string target, a target of another kind, an extra target key, and a null target.
  for (const target of ['14d', { question: QUESTION }, { page_id: 'campaigns', preset_id: '14d', metric: 'spend' }, null, {}]) {
    assert.throws(() => parseNextStep({ ...step, target }));
  }
  assert.throws(() => parseNextStep({ ...step, kind: 'open_evidence_status' }));
  // A campaign step carries the minted capability, never the server-only key.
  assert.throws(() => parseNextStep({ ...step, kind: 'open_campaign_detail', target: { page_id: 'campaigns', campaign_key: 'k' } }));
  assert.throws(() => parseNextStep({ ...step, reason: 'x'.repeat(241) }));
  assert.throws(() => parseNextStep({ ...step, kind: 'ask_assistant', target: { question: 'x'.repeat(241) } }));
  assert.throws(() => parseNextStep({ ...step, target: { page_id: 'campaigns', preset_id: '14 days' } }));
  assert.throws(() => parseNextStep({ ...step, kind: 'open_page', target: { page_id: 'campaigns', filter_id: null } }));
});

test('a slot reads a step only when filled, and a slot without the key has none', () => {
  assert.equal(slotNextStep({}, true), null);
  assert.deepEqual(slotNextStep({ next_step: step }, true), step);
  assert.throws(() => slotNextStep({ next_step: step }, false));
});

test('a served step renders a chip named by its label and described by its reason', async () => {
  const onPreset = vi.fn();
  render(<InsightCard nextStepHandlers={{ open_preset: onPreset }} pageId="campaigns" slot={filled(step)} title="Contribution" />);
  const button = screen.getByRole('button', { name: 'Show 14 days' });
  assert.equal(
    document.getElementById(button.getAttribute('aria-describedby') ?? '')?.textContent,
    step.reason,
  );
  await userEvent.click(button);
  assert.deepEqual(onPreset.mock.calls, [[step.target, button]]);
});

test('each same-tab kind calls its own handler with its target and the chip', async () => {
  for (const kind of ['open_preset', 'open_campaign_detail', 'open_metric_focus', 'open_evidence_status', 'open_product_detail'] as const) {
    const handler = vi.fn();
    const view = render(<InsightCard
      nextStepHandlers={{ [kind]: handler, focusableMetrics: ['spend'] }}
      pageId="campaigns"
      slot={filled(served(kind))}
      title="Contribution"
    />);
    const chip = screen.getByRole('button', { name: `Label ${kind}` });
    await userEvent.click(chip);
    assert.deepEqual(handler.mock.calls, [[TARGETS[kind], chip]]);
    view.unmount();
  }
});

test('a same-tab step for another tab, or with no handler here, renders no control', () => {
  const elsewhere = render(<InsightCard
    nextStepHandlers={{ open_preset: vi.fn() }}
    pageId="retail-health"
    slot={filled(step)}
    title="Contribution"
  />);
  assert.equal(elsewhere.container.querySelector('.next-step'), null);
  elsewhere.unmount();
  for (const kind of ['open_preset', 'open_campaign_detail', 'open_metric_focus', 'open_evidence_status', 'open_product_detail'] as const) {
    const view = render(<InsightCard pageId="campaigns" slot={filled(served(kind))} title="Contribution" />);
    assert.equal(view.container.querySelector('.next-step'), null, kind);
    view.unmount();
  }
  const none = render(<InsightCard slot={filled(null)} title="Contribution" />);
  assert.equal(none.container.querySelector('.next-step'), null);
});

test('a metric step renders only for a metric the page can focus', () => {
  for (const focusableMetrics of [undefined, [], ['clicks']]) {
    const view = render(<InsightCard
      nextStepHandlers={{ open_metric_focus: vi.fn(), ...(focusableMetrics === undefined ? {} : { focusableMetrics }) }}
      pageId="campaigns"
      slot={filled(served('open_metric_focus'))}
      title="Contribution"
    />);
    assert.equal(view.container.querySelector('.next-step'), null, String(focusableMetrics));
    view.unmount();
  }
});

test('the 240 bound counts code points, as the server does', () => {
  // 240 code points, 241 UTF-16 units: the server admits it, so the browser must too.
  const question = `${'x'.repeat(239)}😀`;
  assert.equal(question.length, 241);
  const decoded = parseNextStep({ ...step, kind: 'ask_assistant', reason: question, target: { question } });
  assert.equal(decoded.reason, question);
  assert.throws(() => parseNextStep({ ...step, kind: 'ask_assistant', target: { question: `${question}x` } }));
});

test('a preset step selects only a preset the page offers as available', async () => {
  const selected: string[] = [];
  const presetStep = (presetId: string) => (presetId === '14d' ? () => { selected.push(presetId); } : null);
  const view = render(<PresetStepContext.Provider value={presetStep}>
    <InsightCard pageId="campaigns" slot={filled(step)} title="Contribution" />
  </PresetStepContext.Provider>);
  await userEvent.click(screen.getByRole('button', { name: 'Show 14 days' }));
  assert.deepEqual(selected, ['14d']);
  view.unmount();
  const unoffered = render(<PresetStepContext.Provider value={presetStep}>
    <InsightCard pageId="campaigns" slot={filled({ ...step, target: { page_id: 'campaigns', preset_id: '1d' } })} title="Contribution" />
  </PresetStepContext.Provider>);
  assert.equal(unoffered.container.querySelector('.next-step'), null);
});

test('a cross-tab step links to a tab in the session manifest, and renders nothing otherwise', () => {
  const page = served('open_page', 'Open Retail health');
  const view = render(<VisibleTabsContext.Provider value={TABS}>
    <InsightCard pageId="campaigns" slot={filled(page)} title="Contribution" />
  </VisibleTabsContext.Provider>);
  const link = screen.getByRole('link', { name: 'Open Retail health' });
  // Next's link drops the route's trailing slash outside the static export's trailingSlash config.
  assert.match(link.getAttribute('href') ?? '', /^\/retail-health\/?\?preset=14d$/);
  view.unmount();
  for (const tabs of [[], TABS.slice(0, 1)]) {
    const hidden = render(<VisibleTabsContext.Provider value={tabs}>
      <InsightCard pageId="campaigns" slot={filled(page)} title="Contribution" />
    </VisibleTabsContext.Provider>);
    assert.equal(hidden.container.querySelector('.next-step'), null);
    hidden.unmount();
  }
  assert.equal(openPageHref('/campaigns/', { page_id: 'campaigns' }), '/campaigns/');
  assert.equal(openPageHref('/campaigns/', { page_id: 'campaigns', preset_id: '14d', filter_id: 'amazon_dsp' }), '/campaigns/?preset=14d&channel=amazon_dsp');
});

test('a section and a highlighted channel ride in the URL only to the tab that renders them', () => {
  // UI slice 9a: the Scorecard outlines the channel row a campaign step names and renders its
  // channel table as a focusable anchor; it is the one tab that honours either target.
  assert.equal(
    openPageHref('/scorecard/', { highlight_channel: 'amazon_dsp', page_id: 'scorecard', section_id: 'channel-table' }),
    '/scorecard/?highlight=amazon_dsp#channel-table',
  );
  assert.equal(openPageHref('/scorecard/', { page_id: 'scorecard', section_id: 'channel-table' }), '/scorecard/#channel-table');
  // Paired: a declared Scorecard section it renders no anchor for is refused, never the top.
  assert.equal(openPageHref('/scorecard/', { page_id: 'scorecard', section_id: 'boundary' }), null);
});

test('a cross-tab step whose target the URL cannot carry renders nothing, never the bare tab', () => {
  // One refusal per target kind Retail health can't honour (ADR-122 decision 5, gate F5).
  for (const extra of [{ section_id: 'totals' }, { highlight_channel: 'amazon_dsp' }, { focus_campaign_ref: 'capability-fictional' }]) {
    const target = { page_id: 'retail-health', ...extra };
    assert.equal(openPageHref('/retail-health/', target), null);
    const view = render(<VisibleTabsContext.Provider value={TABS}>
      <InsightCard pageId="campaigns" slot={filled(parseNextStep({ kind: 'open_page', label: 'Open Retail health', reason: 'A fictional reason.', target }))} title="Contribution" />
    </VisibleTabsContext.Provider>);
    assert.equal(view.container.querySelector('.next-step'), null, Object.keys(extra)[0]);
    view.unmount();
  }
  // The same kinds on Campaigns, which renders neither a section anchor nor a highlight.
  assert.equal(openPageHref('/campaigns/', { page_id: 'campaigns', section_id: 'channel-table' }), null);
  assert.equal(openPageHref('/campaigns/', { highlight_channel: 'amazon_dsp', page_id: 'campaigns' }), null);
  assert.equal(openPageHref('/scorecard/', { focus_campaign_ref: 'capability-fictional', page_id: 'scorecard' }), null);
});

test('"Ask the assistant" seeds the dated question verbatim and sends nothing', async () => {
  const seeded: unknown[] = [];
  const listener = (event: Event) => seeded.push((event as CustomEvent<unknown>).detail);
  window.addEventListener('adlytics:assistant-seed', listener);
  const fetchSpy = vi.spyOn(globalThis, 'fetch');
  render(<InsightCard slot={filled(served('ask_assistant', 'Ask the assistant'))} title="Contribution" />);
  const chip = screen.getByRole('button', { name: 'Ask the assistant' });
  assert.equal(chip.className, 'chip-link ask');
  await userEvent.click(chip);
  window.removeEventListener('adlytics:assistant-seed', listener);
  assert.deepEqual(seeded, [{ question: QUESTION, returnTarget: chip }]);
  assert.equal(fetchSpy.mock.calls.length, 0);
  fetchSpy.mockRestore();
});

function DialogHost({ children }: { readonly children: ReactNode }) {
  const [open, setOpen] = useState(true);
  return <>
    <button type="button" id="dialog-opener">Open the insight</button>
    {open ? <Dialog header={<h2 id="h">Insight</h2>} labelledBy="h" onClose={() => setOpen(false)}>{children}</Dialog> : null}
  </>;
}

test('asking from inside a dialog closes it and keeps its opener as the return target', async () => {
  const seeded: { readonly question: string; readonly returnTarget: HTMLElement | null }[] = [];
  const listener = (event: Event) => seeded.push((event as CustomEvent<typeof seeded[number]>).detail);
  window.addEventListener('adlytics:assistant-seed', listener);
  const opener = document.createElement('button');
  opener.textContent = 'Opener';
  document.body.append(opener);
  opener.focus();
  render(<DialogHost><InsightNextStepControl step={served('ask_assistant', 'Ask the assistant')} /></DialogHost>);
  assert.notEqual(screen.queryByRole('dialog'), null);
  await userEvent.click(screen.getByRole('button', { name: 'Ask the assistant' }));
  window.removeEventListener('adlytics:assistant-seed', listener);
  assert.equal(screen.queryByRole('dialog'), null);
  assert.equal(seeded.length, 1);
  assert.equal(seeded[0]?.question, QUESTION);
  assert.equal(seeded[0]?.returnTarget, opener);
  // The dialog closed for the assistant, so it didn't hand focus back to its opener.
  assert.notEqual(document.activeElement, opener);
  assert.equal(closeModalForAssistant(), null);
  opener.remove();
});
