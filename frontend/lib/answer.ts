// SPDX-License-Identifier: MIT-0
/**
 * The answer sentence above a page's figures (UI refresh 1.5, plan "Answer-sentence templates").
 * Each page's builder fills one fixed template with served display strings, and its fallback when
 * a slot is absent. A builder does no arithmetic and reads no model text: a change reads only the
 * served `direction` and the served `display_text`.
 */
import type { Change } from './change';
import { formatIsoDateRange } from './exact-format';

export interface Answer {
  readonly kind: 'answer' | 'fallback';
  readonly text: string;
}

/** A served window in the page's date style ("Sep 30–Oct 6, 2026"). */
export function windowText(window: { readonly end: string; readonly start: string }): string {
  return formatIsoDateRange(window.start, window.end);
}

/** The served change text without its leading sign, because the clause's verb carries it. */
function unsigned(text: string): string {
  return text.replace(/^[+\u2212-]/, '');
}

/**
 * "up 5.6%" / "down 2.4%" / "unchanged" from a served change; null when there is no comparable
 * change (withheld, absent, or a word state such as "New", which reads only in its chip).
 */
export function changeClause(change: Change | null): string | null {
  if (change === null || change.not_meaningful !== null) return null;
  if (change.direction === 'flat') return 'unchanged';
  return `${change.direction === 'up' ? 'up' : 'down'} ${unsigned(change.display_text)}`;
}

/** "rose 5.6%" / "fell 2.4%" / "didn't change", for a sentence whose subject is the figure. */
export function changeVerb(change: Change | null): string | null {
  if (change === null || change.not_meaningful !== null) return null;
  if (change.direction === 'flat') return "didn't change";
  return `${change.direction === 'up' ? 'rose' : 'fell'} ${unsigned(change.display_text)}`;
}
