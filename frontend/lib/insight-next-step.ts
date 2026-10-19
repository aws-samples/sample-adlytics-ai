// SPDX-License-Identifier: MIT-0
/**
 * A card's served next step, `NextStep.v2` (ADR-122; docs/ui/insight-presentation-contract.md,
 * card wire grammar). Each kind has its own closed target shape. The server chose the label, the
 * target and the dated question from views this page serves; the browser renders them verbatim
 * and never composes or infers a step.
 */

export const NEXT_STEP_KINDS = [
  'open_page',
  'open_preset',
  'open_campaign_detail',
  'open_product_detail',
  'open_metric_focus',
  'open_evidence_status',
  'ask_assistant',
] as const;
export type InsightNextStepKind = (typeof NEXT_STEP_KINDS)[number];

/** Each kind's target. Optional keys are absent, never null, on the wire. */
export interface NextStepTargets {
  readonly open_page: {
    readonly page_id: string;
    readonly preset_id?: string;
    readonly filter_id?: string;
    readonly section_id?: string;
    readonly highlight_channel?: string;
    readonly focus_campaign_ref?: string;
  };
  readonly open_preset: { readonly page_id: string; readonly preset_id: string };
  readonly open_campaign_detail: { readonly page_id: string; readonly campaign_ref: string };
  readonly open_product_detail: { readonly selection_reference: string };
  readonly open_metric_focus: { readonly page_id: string; readonly metric: string; readonly filter_id?: string };
  readonly open_evidence_status: null;
  readonly ask_assistant: { readonly question: string };
}

/** One served step, narrowed by its kind. */
export type InsightNextStep = {
  readonly [K in InsightNextStepKind]: {
    readonly kind: K;
    readonly label: string;
    readonly reason: string;
    readonly target: NextStepTargets[K];
  };
}[InsightNextStepKind];

/**
 * The handlers a page can reach, per kind; a kind without one renders no control. Asking the
 * assistant and opening another tab need none: every surface can do both.
 */
export type InsightNextStepHandlers = {
  readonly [K in Exclude<InsightNextStepKind, 'ask_assistant' | 'open_page'>]?: (
    target: NextStepTargets[K],
    origin: HTMLButtonElement,
  ) => void;
} & {
  /** The metrics this page's focus dialog opens; a metric step outside them renders no control. */
  readonly focusableMetrics?: readonly string[];
};

const STEP_FIELDS = ['kind', 'label', 'reason', 'target'] as const;
const MAX_TEXT_CHARS = 240;
const GOVERNED_ID = /^[a-z0-9][a-z0-9_-]{0,63}$/;

/** [required, optional] target keys per kind; `null` is the one targetless kind. */
const TARGET_KEYS: { readonly [K in InsightNextStepKind]: readonly [readonly string[], readonly string[]] | null } = {
  open_page: [['page_id'], ['preset_id', 'filter_id', 'section_id', 'highlight_channel', 'focus_campaign_ref']],
  open_preset: [['page_id', 'preset_id'], []],
  open_campaign_detail: [['page_id', 'campaign_ref'], []],
  open_product_detail: [['selection_reference'], []],
  open_metric_focus: [['page_id', 'metric'], ['filter_id']],
  open_evidence_status: null,
  ask_assistant: [['question'], []],
};
/** URL governed ids (ia §8.1): a preset, a channel filter or a channel key. */
const GOVERNED_KEYS = new Set(['preset_id', 'filter_id', 'highlight_channel']);

function refuse(): never {
  throw new Error('The served next step is malformed.');
}

function nonblank(value: unknown): string {
  return typeof value === 'string' && value.trim() !== '' ? value : refuse();
}

/** Counted in code points, as the server counts, so an emoji in a name never refuses the body. */
function bounded(value: unknown): string {
  const text = nonblank(value);
  return [...text].length > MAX_TEXT_CHARS ? refuse() : text;
}

function parseTarget(kind: InsightNextStepKind, value: unknown): Record<string, string> | null {
  const keys = TARGET_KEYS[kind];
  if (keys === null) return value === null ? null : refuse();
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return refuse();
  const [required, optional] = keys;
  const item = value as Record<string, unknown>;
  const present = Object.keys(item);
  if (!required.every((key) => present.includes(key))) return refuse();
  if (present.some((key) => !required.includes(key) && !optional.includes(key))) return refuse();
  const target: Record<string, string> = {};
  for (const key of present) {
    const text = key === 'question' ? bounded(item[key]) : nonblank(item[key]);
    if (GOVERNED_KEYS.has(key) && !GOVERNED_ID.test(text)) return refuse();
    target[key] = text;
  }
  return target;
}

/** Strictly decode one served next step; a missing or extra key, here or in its target, fails. */
export function parseNextStep(value: unknown): InsightNextStep {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return refuse();
  const item = value as Record<string, unknown>;
  const keys = Object.keys(item).sort();
  if (keys.length !== STEP_FIELDS.length || keys.some((key, index) => key !== STEP_FIELDS[index])) return refuse();
  const kind = NEXT_STEP_KINDS.find((candidate) => candidate === item.kind) ?? refuse();
  return {
    kind,
    label: nonblank(item.label),
    reason: bounded(item.reason),
    target: parseTarget(kind, item.target),
  } as InsightNextStep;
}

/**
 * Read a slot's optional `next_step`: the key is present only on a filled card whose
 * served prompt offered a step (the same exact-shape rule as the absence pair).
 */
export function slotNextStep(item: Record<string, unknown>, filled: boolean): InsightNextStep | null {
  if (!('next_step' in item)) return null;
  if (!filled) return refuse();
  return parseNextStep(item.next_step);
}

/** Whether a raw slot carries a next step, selecting the step shape of its exact key set. */
export function hasNextStep(entry: unknown): boolean {
  return typeof entry === 'object' && entry !== null && 'next_step' in entry;
}

/**
 * What each tab's URL can honour beyond `preset` and `channel` (ADR-122 decision 5): the
 * sections it renders as focusable anchors and whether it outlines a highlighted channel.
 * UI slice 9a: only the Scorecard does either, for its channel table. A step naming anything
 * else renders nothing, because opening the bare tab is the fallback the decision rules out.
 */
export const SCORECARD_CHANNEL_SECTION = 'channel-table';
const HONOURED_TARGETS: Readonly<Record<string, { readonly highlight: boolean; readonly sections: readonly string[] }>> = {
  '/scorecard/': { highlight: true, sections: [SCORECARD_CHANNEL_SECTION] },
};

/**
 * The URL an `open_page` step opens (ia §8.1): the tab's route with `preset`, `channel` and
 * `highlight` as governed ids, and the section as its fragment. The server leaves out a value
 * equal to the target's default. Null when the target names a campaign, or a section or a
 * highlighted channel the target tab doesn't render (`HONOURED_TARGETS`).
 */
export function openPageHref(route: string, target: NextStepTargets['open_page']): string | null {
  if (target.focus_campaign_ref !== undefined) return null;
  const honoured = HONOURED_TARGETS[route];
  if (target.highlight_channel !== undefined && honoured?.highlight !== true) return null;
  if (target.section_id !== undefined && honoured?.sections.includes(target.section_id) !== true) return null;
  const params = new URLSearchParams();
  if (target.preset_id !== undefined) params.set('preset', target.preset_id);
  if (target.filter_id !== undefined) params.set('channel', target.filter_id);
  if (target.highlight_channel !== undefined) params.set('highlight', target.highlight_channel);
  const query = params.toString();
  const fragment = target.section_id === undefined ? '' : `#${encodeURIComponent(target.section_id)}`;
  return `${query === '' ? route : `${route}?${query}`}${fragment}`;
}
