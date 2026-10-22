// SPDX-License-Identifier: MIT-0
/** Insight-focus dialog: clickable cards, per-state body, provenance, hops, focus return. */
import assert from 'node:assert/strict';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { expect, test, vi } from 'vitest';

import { InsightFocusDialog, type InsightFocusSurface } from '../components/InsightFocusDialog';

const period = { end: '2026-08-21', start: '2026-08-15' };

const slots = [
  {
    artifact_revision: 'revision-fictional',
    absence_class: null,
    absence_reason: null,
    evidence_line: 'Spend changed while attributed purchases were stable.',
    id: 'contribution',
    output_class: 'perspective' as const,
    prompt_version: 'v4',
    state: 'filled' as const,
    text: 'Spend was the clearest contributor to the portfolio movement.',
  },
  {
    artifact_revision: 'revision-fictional',
    absence_class: 'evaluated-quiet' as const,
    absence_reason: 'No campaign direction materially diverges from the portfolio direction.',
    evidence_line: null,
    id: 'divergence',
    output_class: 'perspective' as const,
    prompt_version: 'deterministic-gate1',
    state: 'silent' as const,
    text: null,
  },
  {
    artifact_revision: 'revision-fictional',
    absence_class: 'ineligible' as const,
    absence_reason: 'This insight requires a governed delivery follow-up read, which is not available for this view and window.',
    evidence_line: null,
    id: 'exposure',
    output_class: 'perspective' as const,
    // The real wire pairing: ineligible slots are stamped by the deterministic gate.
    prompt_version: 'deterministic-gate1',
    state: 'silent' as const,
    text: null,
  },
  {
    artifact_revision: 'revision-fictional',
    absence_class: 'generation-pending' as const,
    absence_reason: 'This insight could not be generated just now; it retries automatically and appears on a later view.',
    evidence_line: null,
    id: 'movers',
    output_class: 'observed' as const,
    // The real wire stamp for an unavailable movers slot.
    prompt_version: 'deterministic-v1',
    state: 'unavailable' as const,
    text: null,
  },
  {
    artifact_revision: 'revision-fictional',
    absence_class: 'generation-pending' as const,
    absence_reason: 'This insight could not be generated just now; it retries automatically and appears on a later view.',
    evidence_line: null,
    id: 'trend-shape',
    output_class: 'perspective' as const,
    // The real wire stamp for an unavailable non-movers slot.
    prompt_version: 'not-generated',
    state: 'unavailable' as const,
    text: null,
  },
];

function surface(): InsightFocusSurface {
  return {
    labelFor: (slotId) => ({ contribution: 'Contribution', divergence: 'Divergence', exposure: 'Exposure', movers: 'Largest changes', 'trend-shape': 'Trend shape' })[slotId] ?? slotId,
    slots,
    window: period,
  };
}

test('a quiet slot dialog states the served reason and its deterministic provenance', () => {
  render(
    <InsightFocusDialog
      onClose={() => undefined}
      onSlotChange={() => undefined}
      slotId="divergence"
      surface={surface()}
      surfaceKey="campaign-portfolio"
    />,
  );

  const dialog = screen.getByRole('dialog');
  assert.ok(screen.getByRole('heading', { level: 2, name: 'Divergence' }));
  assert.ok(screen.getByText('Nothing noteworthy'));
  assert.ok(screen.getByText('Why this slot is quiet'));
  assert.ok(dialog.textContent?.includes('No campaign direction materially diverges from the portfolio direction.'));
  // Provenance: the contract's reading substitutes for the gate-1 wire token.
  assert.ok(dialog.textContent?.includes('Decided by a fixed rule from the values on this page'));
  assert.equal(dialog.textContent?.includes('cached against'), false);
  // No internal class or wire token reaches the dialog.
  assert.equal(dialog.textContent?.includes('deterministic-gate1'), false);
  assert.equal(dialog.textContent?.includes('evaluated-quiet'), false);
});

test('a filled slot dialog carries the narration, evidence line, and output class', () => {
  render(
    <InsightFocusDialog
      onClose={() => undefined}
      onSlotChange={() => undefined}
      slotId="contribution"
      surface={surface()}
      surfaceKey="campaign-portfolio"
    />,
  );

  const dialog = screen.getByRole('dialog');
  assert.ok(dialog.textContent?.includes('Spend was the clearest contributor to the portfolio movement.'));
  assert.ok(dialog.textContent?.includes('Spend changed while attributed purchases were stable.'));
  assert.ok(screen.getAllByText('Reading').length >= 1);
  // Customer words only (design revision 2026-09-25.1): no prompt version, no
  // internal vocabulary — the strip carries the generic governed-reading line.
  assert.equal(dialog.textContent?.includes('v4'), false);
  assert.ok(dialog.textContent?.includes('One governed reading of this view'));
  assert.equal(dialog.textContent?.includes('no model involved'), false);
  // The ask-row matches the mockup's wording, window qualifier included.
  assert.ok(dialog.textContent?.includes("Ask the assistant about this window's"));
});

test('an ineligible slot dialog is the loud callout with the served missing-evidence sentence', () => {
  render(
    <InsightFocusDialog
      onClose={() => undefined}
      onSlotChange={() => undefined}
      slotId="exposure"
      surface={surface()}
      surfaceKey="campaign-portfolio"
    />,
  );

  assert.ok(screen.getByText('Needs data'));
  assert.ok(screen.getByText('What this slot needs'));
  assert.ok(screen.getByText(/requires a governed delivery follow-up read/));
  assert.ok(document.querySelector('.focus-dialog .narr-card.callout'));
  // The gate stamp on an ineligible slot reads as its contract-owned line.
  const dialog = screen.getByRole('dialog');
  assert.ok(dialog.textContent?.includes('Decided by a fixed rule from the values on this page'));
  assert.equal(dialog.textContent?.includes('deterministic-gate1'), false);
});

test('a pending movers dialog reads the deterministic-narrator stamp as its contract line', () => {
  render(
    <InsightFocusDialog
      onClose={() => undefined}
      onSlotChange={() => undefined}
      slotId="movers"
      surface={surface()}
      surfaceKey="campaign-portfolio"
    />,
  );

  const dialog = screen.getByRole('dialog');
  assert.ok(screen.getByText('Retrying'));
  assert.ok(screen.getByText('What happens next'));
  assert.ok(dialog.textContent?.includes('retries automatically and appears on a later view'));
  // The reading names the composer without asserting composition (contract §3).
  assert.ok(dialog.textContent?.includes('Computed directly from the values on this page'));
  assert.equal(dialog.textContent?.includes('deterministic-v1'), false);
});

test('a pending non-movers dialog reads the not-generated stamp as its contract line', () => {
  render(
    <InsightFocusDialog
      onClose={() => undefined}
      onSlotChange={() => undefined}
      slotId="trend-shape"
      surface={surface()}
      surfaceKey="campaign-detail"
    />,
  );

  const dialog = screen.getByRole('dialog');
  assert.ok(dialog.textContent?.includes('No reading was generated for this view'));
  assert.equal(dialog.textContent?.includes('not-generated'), false);
});

test('the switcher hops the page roster without closing and Escape closes', async () => {
  const user = userEvent.setup();
  let closed = 0;
  const hops: string[] = [];
  render(
    <InsightFocusDialog
      onClose={() => { closed += 1; }}
      onSlotChange={(slotId) => { hops.push(slotId); }}
      slotId="divergence"
      surface={surface()}
      surfaceKey="campaign-portfolio"
    />,
  );

  assert.ok(screen.getByText('2 of 5'));
  await user.click(screen.getByRole('button', { name: 'Next insight' }));
  await user.click(screen.getByRole('button', { name: 'Previous insight' }));
  assert.deepEqual(hops, ['exposure', 'contribution']);
  assert.equal(closed, 0);
  await user.keyboard('{Escape}');
  assert.equal(closed, 1);
});

test('the rendered dialog is axe-clean, including heading order', async () => {
  const axe = await import('axe-core');
  render(
    <InsightFocusDialog
      onClose={() => undefined}
      onSlotChange={() => undefined}
      slotId="divergence"
      surface={surface()}
      surfaceKey="campaign-portfolio"
    />,
  );

  const dialog = screen.getByRole('dialog');
  const results = await axe.default.run(dialog, {
    rules: { 'color-contrast': { enabled: false }, region: { enabled: false } },
  });
  assert.deepEqual(results.violations.map((violation) => violation.id), []);
});


test('a filled deep-bearing insight starts the deep request while movers never do', async () => {
  const fetch = vi.fn(() => Promise.resolve({
    absence_reason: null,
    artifact_revision: 'revision-fictional',
    deep_prompt_version: 'v9',
    period,
    slot_id: 'contribution',
    state: 'filled' as const,
    deep: {
      what_stands_out: 'The deep contribution stands out.',
      candidate_readings: ['Reading one: it may be temporary.'],
      discriminating_evidence: 'The next window would discriminate.',
      cited_figures: ['5'],
    },
  }));
  const base = surface();
  const deepSurface: InsightFocusSurface = {
    ...base,
    deepFor: (slot) => slot.id === 'movers' ? null : {
      cacheKey: `test:${slot.id}`,
      expectedArtifactRevision: slot.artifact_revision,
      fetch,
      window: period,
    },
  };
  const rendered = render(
    <InsightFocusDialog onClose={() => undefined} onSlotChange={() => undefined} slotId="contribution" surface={deepSurface} surfaceKey="campaign-portfolio" />,
  );
  assert.ok(await screen.findByText('The deep contribution stands out.'));
  expect(fetch).toHaveBeenCalledTimes(1);
  rendered.unmount();

  render(
    <InsightFocusDialog onClose={() => undefined} onSlotChange={() => undefined} slotId="movers" surface={deepSurface} surfaceKey="campaign-portfolio" />,
  );
  expect(fetch).toHaveBeenCalledTimes(1);
  expect(screen.queryByRole('heading', { name: 'Deep analysis' })).toBeNull();
});

test('an optional served preamble is the first dialog body content', () => {
  const withPreamble: InsightFocusSurface = {
    ...surface(),
    preambleFor: (slot) => <div role="note"><p>Served caveat for {slot.id}.</p></div>,
  };
  render(
    <InsightFocusDialog
      onClose={() => undefined}
      onSlotChange={() => undefined}
      slotId="contribution"
      surface={withPreamble}
      surfaceKey="campaign-portfolio"
    />,
  );

  const body = screen.getByRole('dialog').querySelector('.dialog-body');
  assert.equal(body?.firstElementChild?.getAttribute('role'), 'note');
  assert.equal(body?.firstElementChild?.textContent, 'Served caveat for contribution.');
});
