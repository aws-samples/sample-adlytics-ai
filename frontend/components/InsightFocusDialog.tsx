// SPDX-License-Identifier: MIT-0
'use client';

/**
 * The insight-focus dialog (docs/ui/insight-presentation-contract.md §3).
 *
 * Opened from any insight card; reuses the metric-focus visual language: scrim, inert
 * page, focus trap, Escape returns focus to the opening card, ‹ › hops between the
 * page's insight slots without closing. Single-column (design revision 2026-09-25.1):
 * the served narration full width, the deep band full width beneath it, and one
 * customer-word provenance strip — nothing internal ever renders here.
 */

import type { ReactNode, RefObject } from 'react';
import type { Change } from '@/lib/change';
import { formatIsoDateRange } from '@/lib/exact-format';
import type { InsightNextStepHandlers } from '@/lib/insight-next-step';
import { provenanceLine } from '@/lib/provenance';
import { seedAssistantQuestion } from './ChatPanel';
import { Dialog } from './Dialog';
import { InsightDeepSection, type InsightDeepSurface } from './InsightDeepSection';
import { ChangeChip } from './ChartPrimitives';
import { InsightNextStepControl, insightTreatment, type InsightSlotView } from './InsightCard';

/** A slot as the dialog needs it: the shared card fields plus identity and provenance. */
export interface InsightDialogSlot extends InsightSlotView {
  readonly artifact_revision: string;
  readonly id: string;
  readonly prompt_version: string;
}

export interface InsightFocusSurface {
  /** Display title per slot purpose, for the head and the switcher. */
  readonly labelFor: (slotId: string) => string;
  /** Resolve an on-request deep section for one filled deep-bearing slot. */
  readonly deepFor?: (slot: InsightDialogSlot) => InsightDeepSurface | null;
  /** The page's declared insight roster, in display order; the dialog hops across it. */
  readonly slots: readonly InsightDialogSlot[];
  /** The page's resolved window; the slots are baked against the same artifact. */
  readonly window: { readonly end: string; readonly start: string };
  /** The page's step handlers, so a card's served step also renders in its dialog. */
  readonly nextStepHandlers?: InsightNextStepHandlers;
  readonly pageId?: string;
  /**
   * Stats the server wrote for the page's own entity (ADR-125: a product page's dialog shows
   * that product's figures, each label naming it) and the provenance line that says what the
   * card's text was written from. Both are served text; null keeps the defaults.
   */
  readonly statsFor?: (slot: InsightDialogSlot) => readonly { readonly change: Change | null; readonly label: string; readonly text: string }[] | null;
  /** Optional served caveat copy that must lead this slot's dialog body. */
  readonly preambleFor?: (slot: InsightDialogSlot) => ReactNode;
  readonly provenanceFor?: (slot: InsightDialogSlot) => string | null;
  /**
   * Whether `labelFor` returns served names (the Scorecard's card and row titles, with proper
   * nouns, acronyms and marketplaces), which the assistant chip then keeps verbatim. Unset, the
   * labels are the browser's purpose words and the chip lowercases them mid-sentence.
   */
  readonly servedLabels?: boolean;
}

/** Browser-owned section headings per treatment — chrome, never served words. */
const STATE_HEADINGS = {
  '': 'Narration',
  callout: 'What this slot needs',
  pending: 'What happens next',
  quiet: 'Why this slot is quiet',
} as const;



export function InsightFocusDialog({ onClose, onSlotChange, opener, slotId, surface, surfaceKey }: {
  readonly onClose: () => void;
  /** The card that opened the dialog; focus returns to it on close. */
  readonly opener?: RefObject<HTMLElement | null> | undefined;
  readonly onSlotChange: (slotId: string) => void;
  readonly slotId: string;
  readonly surface: InsightFocusSurface;
  readonly surfaceKey: string;
}) {
  const index = surface.slots.findIndex((candidate) => candidate.id === slotId);
  const slot = surface.slots[index];
  if (slot === undefined) return null;
  const treatment = insightTreatment(slot);
  const deep = slot.state === 'filled' ? surface.deepFor?.(slot) ?? null : null;
  const windowText = formatIsoDateRange(surface.window.start, surface.window.end);
  // A served name keeps its case ("Amazon DSP … US marketplace", SC9A-GATE-R5-005).
  const subject = surface.servedLabels === true ? surface.labelFor(slot.id) : surface.labelFor(slot.id).toLowerCase();
  const hop = (direction: -1 | 1) => {
    const next = surface.slots[(index + direction + surface.slots.length) % surface.slots.length];
    if (next !== undefined) onSlotChange(next.id);
  };
  return <Dialog
    className="focus-dialog"
    closeLabel="Close insight view"
    labelledBy="insight-focus-heading"
    onClose={onClose}
    opener={opener}
    surface={surfaceKey}
    header={<>
      <p className="eyebrow">Insight focus · {windowText}</p>
      <h2 id="insight-focus-heading">{surface.labelFor(slot.id)}</h2>
      <div className="row"><span className={treatment.tag.className}>{treatment.tag.label}</span></div>
    </>}
    headerEnd={<nav className="metric-switch" aria-label="Other insights on this page">
      <button type="button" onClick={() => hop(-1)} aria-label="Previous insight"><span aria-hidden="true">‹</span></button>
      <span className="current">{index + 1} of {surface.slots.length}</span>
      <button type="button" onClick={() => hop(1)} aria-label="Next insight"><span aria-hidden="true">›</span></button>
    </nav>}
  >
      {surface.preambleFor?.(slot) ?? null}
      {/* Single column (design revision 2026-09-25.1): narration full width, the deep
          band full width beneath it, one customer-word provenance strip at the foot. */}
      <article className={treatment.cardClass === '' ? 'narr-card dialog-lead' : `narr-card dialog-lead ${treatment.cardClass}`}>
        <div className="purpose"><h3>{STATE_HEADINGS[treatment.cardClass]}</h3></div>
        {treatment.reason === null
          ? <><p>{slot.text}</p>{slot.evidence_line === null ? null : <span className="ev">{slot.evidence_line}</span>}</>
          : <p>{treatment.reason}</p>}
      </article>
      {(() => {
        const stats = surface.statsFor?.(slot) ?? null;
        return stats === null || stats.length === 0 ? null : <dl className="stat-list">
          {stats.map((stat) => <div className="stat" key={stat.label}>
            <dt>{stat.label}</dt>
            <dd>{stat.text}{stat.change === null ? null : <> <ChangeChip change={stat.change} /></>}</dd>
          </div>)}
        </dl>;
      })()}
      {deep === null ? null : <InsightDeepSection surface={deep} />}
      {slot.state === 'filled' ? <InsightNextStepControl handlers={surface.nextStepHandlers} pageId={surface.pageId} step={slot.next_step} /> : null}
      <p className="prov-strip">{surface.provenanceFor?.(slot) ?? provenanceLine(slot.prompt_version)}</p>
      <div className="ask-row">
        <button type="button" className="ask-chip" onClick={() => seedAssistantQuestion(`Ask about this window's ${subject}.`)}>
          Ask the assistant about this window&apos;s {subject}
        </button>
      </div>
  </Dialog>;
}
