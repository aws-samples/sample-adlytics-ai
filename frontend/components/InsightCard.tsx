// SPDX-License-Identifier: MIT-0
'use client';

import { INSIGHT_TAG_LABELS } from '@/lib/glossary';
import Link from 'next/link';
import { useId } from 'react';

import { type InsightNextStep, type InsightNextStepHandlers, openPageHref } from '../lib/insight-next-step';
import type { ManifestTab } from '../lib/route-manifest';
import { seedAssistantQuestion } from './ChatPanel';
import { Icon } from './Icon';
import { useCoverageOpen } from './Coverage';
import { usePresetStep, useVisibleTabs } from './NextStepContext';

/**
 * The shared insight-slot card grammar (docs/ui/insight-presentation-contract.md).
 *
 * Five designed states, one treatment table: filled narrates; evaluated-quiet (and the
 * never-expected out-of-slice) recedes behind a dashed border; ineligible calls out its
 * missing evidence; generation-pending is honest and calm. The body words are the served
 * narration or the served absence_reason verbatim — the browser composes nothing, and no
 * card is ever an unexplained blank.
 */

export type InsightAbsenceClass = 'evaluated-quiet' | 'generation-pending' | 'ineligible' | 'out-of-slice';

/** The slot fields every insight surface shares (campaign, retail, metric focus). */
export interface InsightSlotView {
  readonly absence_class: InsightAbsenceClass | null;
  readonly absence_reason: string | null;
  readonly evidence_line: string | null;
  readonly output_class: 'hypothesis' | 'observed' | 'perspective';
  readonly state: 'filled' | 'silent' | 'unavailable';
  readonly text: string | null;
  readonly next_step?: InsightNextStep | null | undefined;
}

/** The contract's one owned fallback line: a non-filled slot served without the absence pair. */
export const INSIGHT_FALLBACK_REASON = 'Nothing noteworthy this window.';

const OUTPUT_TAGS = {
  hypothesis: { className: 'tag hypothesis', label: INSIGHT_TAG_LABELS.hypothesis },
  observed: { className: 'tag observed', label: INSIGHT_TAG_LABELS.observed },
  perspective: { className: 'tag perspective', label: INSIGHT_TAG_LABELS.perspective },
} as const;

export interface InsightTreatment {
  /** Card modifier: '' (filled), 'quiet', 'callout', or 'pending'. */
  readonly cardClass: '' | 'callout' | 'pending' | 'quiet';
  /** The served reason a non-filled card states verbatim; null for filled. */
  readonly reason: string | null;
  /** The contract-owned tag, carried in every state. */
  readonly tag: { readonly className: string; readonly label: string };
}

/** Resolve a slot's designed treatment; absence_class selects it, never the browser's judgment. */
export function insightTreatment(slot: InsightSlotView): InsightTreatment {
  if (slot.state === 'filled') return { cardClass: '', reason: null, tag: OUTPUT_TAGS[slot.output_class] };
  const reason = slot.absence_reason ?? INSIGHT_FALLBACK_REASON;
  if (slot.absence_class === 'ineligible') return { cardClass: 'callout', reason, tag: { className: 'tag needs-data', label: 'Not enough data yet' } };
  if (slot.absence_class === 'generation-pending') return { cardClass: 'pending', reason, tag: { className: 'tag retrying', label: 'Loading' } };
  // evaluated-quiet, the never-rendered out-of-slice, and the pair-less retained shape all recede.
  return { cardClass: 'quiet', reason, tag: { className: 'tag quiet', label: 'Nothing noteworthy' } };
}

/** The card's inner grammar: purpose row with the state's tag, then narration or served reason. */
export function InsightCardBody({ slot, title }: { readonly slot: InsightSlotView; readonly title: string }) {
  const treatment = insightTreatment(slot);
  return <>
    <div className="purpose">
      <h3>{title}</h3>
      <span className={treatment.tag.className}>{treatment.tag.label}</span>
    </div>
    {treatment.reason === null
      ? <><p>{slot.text}</p><span className="ev">{slot.evidence_line}</span></>
      : <p>{treatment.reason}</p>}
  </>;
}

/**
 * The action that serves a step where it renders, bound to its target, or undefined when it
 * can't be reached there, so no dead chip renders. Every surface can ask the assistant ("Asking
 * from the page", ia §8.8, which never sends). The page's preset control and coverage sheet
 * serve their kinds where they exist; every other same-tab kind needs the page's own handler,
 * and a same-tab target on another tab is unreachable.
 */
function useStepAction(
  step: InsightNextStep | null | undefined,
  handlers: InsightNextStepHandlers | undefined,
  pageId: string | undefined,
): ((origin: HTMLButtonElement) => void) | undefined {
  const preset = usePresetStep();
  const coverage = useCoverageOpen();
  if (step == null) return undefined;
  if (step.target !== null && 'page_id' in step.target && step.target.page_id !== pageId) return undefined;
  switch (step.kind) {
    case 'ask_assistant': {
      const question = step.target.question;
      return () => seedAssistantQuestion(question);
    }
    case 'open_preset': {
      const select = handlers?.open_preset === undefined ? preset?.(step.target.preset_id) ?? null : null;
      const handler = handlers?.open_preset;
      const target = step.target;
      if (handler !== undefined) return (origin) => handler(target, origin);
      return select === null ? undefined : () => select();
    }
    case 'open_evidence_status': {
      const handler = handlers?.open_evidence_status;
      if (handler !== undefined) return (origin) => handler(null, origin);
      return coverage === null ? undefined : (origin) => coverage(origin);
    }
    case 'open_campaign_detail': {
      const handler = handlers?.open_campaign_detail;
      const target = step.target;
      return handler === undefined ? undefined : (origin) => handler(target, origin);
    }
    case 'open_metric_focus': {
      const handler = handlers?.open_metric_focus;
      const target = step.target;
      // A metric outside the page's focus set would open nothing, so it renders no control.
      if (handler === undefined || !(handlers?.focusableMetrics ?? []).includes(target.metric)) return undefined;
      return (origin) => handler(target, origin);
    }
    case 'open_product_detail': {
      const handler = handlers?.open_product_detail;
      const target = step.target;
      return handler === undefined ? undefined : (origin) => handler(target, origin);
    }
    case 'open_page':
      return undefined;
  }
}

/** The URL an `open_page` step opens, when this session's manifest lists its tab and the URL can carry its target. */
function pageHref(step: InsightNextStep | null | undefined, tabs: readonly ManifestTab[]): string | undefined {
  if (step?.kind !== 'open_page') return undefined;
  const tab = tabs.find((item) => item.kind === 'page' && item.tab_id === step.target.page_id);
  return tab === undefined ? undefined : openPageHref(tab.route, step.target) ?? undefined;
}

/** Whether a step renders a control here, for a host that shows a fallback when it doesn't. */
export function useRendersNextStep(
  step: InsightNextStep | null | undefined,
  handlers?: InsightNextStepHandlers,
  pageId?: string,
): boolean {
  const tabs = useVisibleTabs();
  const action = useStepAction(step, handlers, pageId);
  return pageHref(step, tabs) !== undefined || action !== undefined;
}

/**
 * A card's served next step (FR-11, ADR-122): the reason as text and one chip described by it.
 * A cross-tab step is a link to a tab in this session's manifest; every other kind is a button.
 * A step that can't be reached here renders nothing, never a dead control.
 */
export function InsightNextStepControl({ handlers, pageId, step }: {
  readonly handlers?: InsightNextStepHandlers | undefined;
  /** The tab the control is on; a same-tab step for another tab renders nothing. */
  readonly pageId?: string | undefined;
  readonly step: InsightNextStep | null | undefined;
}) {
  const reasonId = useId();
  const tabs = useVisibleTabs();
  const action = useStepAction(step, handlers, pageId);
  const href = pageHref(step, tabs);
  if (step == null) return null;
  if (step.kind === 'open_page') {
    if (href === undefined) return null;
    return <div className="next-step">
      <p className="next-step-reason" id={reasonId}>{step.reason}</p>
      <Link className="chip-link" href={href} aria-describedby={reasonId}>{step.label}</Link>
    </div>;
  }
  if (action === undefined) return null;
  const ask = step.kind === 'ask_assistant';
  return <div className="next-step">
    <p className="next-step-reason" id={reasonId}>{step.reason}</p>
    <button
      type="button"
      className={ask ? 'chip-link ask' : 'chip-link'}
      aria-describedby={reasonId}
      {...(step.kind === 'open_evidence_status' ? { 'aria-haspopup': 'dialog' as const } : {})}
      onClick={(event) => action(event.currentTarget)}
    >
      {ask ? <Icon name="assistant" small /> : null}{step.label}
    </button>
  </div>;
}

/**
 * One insight card. Clickable in every state exactly like the metric tiles: an invisible
 * full-card button whose accessible name names the purpose, opening the insight dialog.
 */
export function InsightCard({ className, nextStepHandlers, onOpen, pageId, slot, title }: {
  readonly className?: string | undefined;
  readonly nextStepHandlers?: InsightNextStepHandlers | undefined;
  /** The tab the card is on, so its same-tab steps reach this page's handlers. */
  readonly pageId?: string | undefined;
  readonly onOpen?: ((origin: HTMLButtonElement) => void) | undefined;
  readonly slot: InsightSlotView;
  readonly title: string;
}) {
  const treatment = insightTreatment(slot);
  const cardClass = ['narr-card', treatment.cardClass, className ?? ''].filter((part) => part !== '').join(' ');
  return <article className={cardClass}>
    <InsightCardBody slot={slot} title={title} />
    {onOpen === undefined ? null : <button
      type="button"
      className="open-insight"
      aria-haspopup="dialog"
      onClick={(event) => onOpen(event.currentTarget)}
    >
      Open the insight view for {title.toLowerCase()}
    </button>}
    {slot.state === 'filled' ? <InsightNextStepControl handlers={nextStepHandlers} pageId={pageId} step={slot.next_step} /> : null}
  </article>;
}
