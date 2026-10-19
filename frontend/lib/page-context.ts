// SPDX-License-Identifier: MIT-0
/**
 * Page context for the assistant (ADR-120): the server's chip text, its expiry and its starter
 * questions, decoded strictly from a page or detail response. The browser writes none of this
 * text; it only sends the page's `artifact_revision` back as an opaque reference.
 */

/** Decision 3: at most three starter questions, each at most 120 characters. */
export const MAX_STARTER_QUESTIONS = 3;
export const MAX_STARTER_QUESTION_CHARS = 120;
const MAX_LABEL_CHARS = 400;
const REFERENCE = /^[A-Za-z0-9_-]{43}$/;
const ISO_INSTANT = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{6})?\+00:00$/;

/** One served page's context, named by the reference the assistant sends back. */
export interface PageContextV1 {
  /** The response's `artifact_revision`: the only thing a turn sends. */
  readonly reference: string;
  /** The chip text, "Using this page: {label}". */
  readonly label: string;
  /** ISO 8601 UTC; past it, the page refreshes before a turn sends. */
  readonly expires_at: string;
  readonly starter_questions: readonly string[];
}

/** The keys a page or detail response adds (decision 3). */
export const PAGE_CONTEXT_KEYS = ['page_context_expires_at', 'page_context_label', 'starter_questions'] as const;

function fail(): never {
  throw new TypeError('The page context fields are invalid.');
}

function bounded(value: unknown, maximum: number): string {
  // Code points, as the server's `len()` counts them.
  if (typeof value !== 'string' || !value.trim() || [...value].length > maximum) return fail();
  return value;
}

/** Decode the three page-context fields of one response, bound to its reference. */
export function parsePageContext(item: Readonly<Record<string, unknown>>, reference: string): PageContextV1 {
  if (!REFERENCE.test(reference)) return fail();
  const expires = item.page_context_expires_at;
  if (typeof expires !== 'string' || !ISO_INSTANT.test(expires) || Number.isNaN(Date.parse(expires))) return fail();
  const starters = item.starter_questions;
  if (!Array.isArray(starters) || starters.length > MAX_STARTER_QUESTIONS) return fail();
  const questions = starters.map((question) => bounded(question, MAX_STARTER_QUESTION_CHARS));
  if (new Set(questions).size !== questions.length) return fail();
  return Object.freeze({
    reference,
    label: bounded(item.page_context_label, MAX_LABEL_CHARS),
    expires_at: expires,
    starter_questions: Object.freeze(questions),
  });
}

/** Whether the context is past its expiry at `now` (milliseconds since the epoch). */
export function pageContextExpired(context: PageContextV1, now: number): boolean {
  return Date.parse(context.expires_at) <= now;
}
