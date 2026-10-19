// SPDX-License-Identifier: MIT-0
/**
 * `EvidenceStatus.v2`, the one server-written coverage block (ADR-119).
 *
 * Every string here is the server's: figure and role labels, missing dates, reason, recovery,
 * and the typed-absence text that takes a withheld change's place (ADR-118 decision 5). The
 * browser keeps no code-to-label map and writes no coverage copy. A body baked before the block
 * carries no `evidence_status` key and decodes to `null`.
 */

export const EVIDENCE_STATUS_KEY = 'evidence_status';

export type CoverageState = 'complete' | 'missing_days' | 'partial_days' | 'unavailable';

export interface EvidenceStatusEntry {
  readonly slot: string;
  readonly role: string;
  readonly label: string;
  readonly role_label: string;
  readonly state: CoverageState;
  readonly missing_dates: readonly string[];
  readonly missing_text: string;
  readonly reason: string;
  readonly recovery: string;
  /** "1 day not yet sent." or "1 partial day.", in the change's place; null unless withheld. */
  readonly absence_text: string | null;
  /** The basis's coverage part ("6 of 7 days"); null unless withheld. */
  readonly coverage_text: string | null;
}

export interface EvidenceStatusV2 {
  readonly version: 2;
  readonly window: { readonly start: string; readonly end: string };
  readonly summary: string;
  readonly entries: readonly EvidenceStatusEntry[];
}

const STATES: readonly CoverageState[] = ['complete', 'missing_days', 'partial_days', 'unavailable'];
const ENTRY_KEYS = [
  'absence_text', 'coverage_text', 'label', 'missing_dates', 'missing_text', 'reason', 'recovery',
  'role', 'role_label', 'slot', 'state',
] as const;
const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

function fail(): never {
  throw new TypeError('The coverage block is malformed.');
}

function exact(value: unknown, keys: readonly string[]): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return fail();
  const found = Object.keys(value);
  if (found.length !== keys.length || keys.some((key) => !found.includes(key))) return fail();
  return value as Record<string, unknown>;
}

function text(value: unknown): string {
  return typeof value === 'string' && value.trim() ? value : fail();
}

function optionalText(value: unknown): string | null {
  return value === null ? null : text(value);
}

function isoDate(value: unknown): string {
  const result = text(value);
  return ISO_DATE.test(result) && !Number.isNaN(Date.parse(`${result}T00:00:00Z`)) ? result : fail();
}

/** True when a figure's change is withheld for a missing or partial day. */
export function isWithheld(entry: EvidenceStatusEntry | null | undefined): entry is EvidenceStatusEntry {
  return entry !== null && entry !== undefined && (entry.state === 'missing_days' || entry.state === 'partial_days');
}

function entry(value: unknown): EvidenceStatusEntry {
  const item = exact(value, ENTRY_KEYS);
  const state = STATES.find((candidate) => candidate === item.state) ?? fail();
  if (!Array.isArray(item.missing_dates)) return fail();
  const dates = item.missing_dates.map(isoDate);
  if (dates.some((day, index) => index > 0 && dates[index - 1]! >= day)) return fail();
  const parsed: EvidenceStatusEntry = {
    absence_text: optionalText(item.absence_text),
    coverage_text: optionalText(item.coverage_text),
    label: text(item.label),
    missing_dates: dates,
    missing_text: text(item.missing_text),
    reason: text(item.reason),
    recovery: text(item.recovery),
    role: text(item.role),
    role_label: text(item.role_label),
    slot: text(item.slot),
    state,
  };
  const withheld = isWithheld(parsed);
  if (withheld !== (parsed.absence_text !== null) || withheld !== (parsed.coverage_text !== null)) return fail();
  if (withheld && dates.length === 0) return fail();
  if (state === 'complete' && dates.length > 0) return fail();
  return parsed;
}

/**
 * Decode the block for one window; `null` stays `null`. Refuses a block for another window, a
 * duplicate figure, or an entry whose absence text disagrees with its state.
 */
export function parseEvidenceStatus(
  value: unknown,
  window: { readonly start: string; readonly end: string },
): EvidenceStatusV2 | null {
  if (value === null) return null;
  const item = exact(value, ['entries', 'summary', 'version', 'window']);
  if (item.version !== 2 || !Array.isArray(item.entries) || item.entries.length === 0) return fail();
  const blockWindow = exact(item.window, ['end', 'start']);
  if (isoDate(blockWindow.start) !== window.start || isoDate(blockWindow.end) !== window.end) return fail();
  const entries = item.entries.map(entry);
  const keys = new Set(entries.map((parsed) => `${parsed.slot}\u0000${parsed.role}`));
  if (keys.size !== entries.length) return fail();
  return { entries, summary: text(item.summary), version: 2, window: { end: window.end, start: window.start } };
}

/** The entry for one figure in this window, or `undefined` when the block doesn't list it. */
export function coverageEntry(
  status: EvidenceStatusV2 | null,
  slot: string,
  role = 'current',
): EvidenceStatusEntry | undefined {
  return status?.entries.find((item) => item.slot === slot && item.role === role);
}
