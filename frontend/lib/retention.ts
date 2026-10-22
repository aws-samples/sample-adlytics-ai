// SPDX-License-Identifier: MIT-0
import { type Answer } from './answer';
/** Strict browser decoder for server-owned RetentionCohortArtifact v1. */

export type RetentionOutcome = 'complete' | 'empty' | 'insufficient';
export type CohortGrain = 'month' | 'quarter';
export type RetentionCellState = 'value' | 'not_returned' | 'withheld' | 'not_yet';

export interface RetentionFigure { readonly value: string; readonly text: string }
export interface RetentionCell {
  readonly offset: number;
  readonly column_label: string;
  readonly state: RetentionCellState;
  readonly rate: RetentionFigure | null;
  readonly cohort_size: RetentionFigure | null;
  readonly text: string | null;
}
export interface RetentionRow {
  readonly cohort: string;
  readonly label: string;
  readonly absent_text: string | null;
  readonly cells: readonly RetentionCell[];
}
export interface RetentionGrid {
  readonly group: RetentionGroup;
  readonly label: string;
  readonly grid_label: string;
  readonly rows: readonly RetentionRow[];
}
export type RetentionGroup = 'dsp_only' | 'sponsored_only' | 'dsp_and_sponsored';
export interface RetentionCurvePoint {
  readonly offset: number;
  readonly label: string;
  readonly rate: RetentionFigure | null;
  readonly text: string | null;
}
export interface RetentionCurveLine {
  readonly group: RetentionGroup;
  readonly label: string;
  readonly points: readonly RetentionCurvePoint[];
}
export interface RetentionInsight {
  readonly absence_class: string | null;
  readonly absence_reason: string | null;
  readonly artifact_revision: string;
  readonly evidence_line: string | null;
  readonly id: 'retention-shift';
  readonly output_class: 'perspective';
  readonly prompt_version: string;
  readonly state: 'filled' | 'silent';
  readonly text: string | null;
}
export interface RetentionMarket {
  readonly marketplace_key: string;
  /** Which marketplace the market's sections measure, served only with two or more shown. */
  readonly marketplace_text: string | null;
  readonly outcome: RetentionOutcome;
  readonly absence_text: string | null;
  readonly cohort_grain: CohortGrain | null;
  readonly scope_band: string | null;
  readonly grain_note: string | null;
  readonly path: {
    readonly standard: { readonly available: boolean };
    readonly paid: { readonly state: 'observed' | 'not_observed'; readonly callout: string | null };
  };
  readonly triangle: {
    readonly legend: string;
    readonly identity_caveat: string;
    readonly standard: readonly RetentionGrid[];
    readonly paid: readonly RetentionGrid[];
  };
  readonly curve: {
    readonly label: string;
    readonly metric: 'cumulative_repeat_rate_account_ids';
    readonly lines: readonly RetentionCurveLine[];
  };
  readonly insights: readonly RetentionInsight[];
}
export interface RetentionEvidenceStatusSlot {
  readonly marketplace_key: string;
  readonly missing_dates: readonly { readonly end: string; readonly start: string }[];
  readonly reason_text: string;
  readonly recovery_text: string | null;
  readonly role_label: string;
  readonly slot_label: string;
}
export interface RetentionCohortArtifact {
  readonly type: 'RetentionCohortArtifact';
  readonly version: 1;
  readonly outcome: RetentionOutcome;
  readonly vintage: { readonly as_of_month: string; readonly label: string; readonly start: string; readonly end: string };
  readonly markets: readonly RetentionMarket[];
  readonly boundary: readonly string[];
  readonly evidence_status: { readonly version: 2; readonly slots: readonly RetentionEvidenceStatusSlot[] };
  readonly page_context_label: string;
  readonly page_context_expires_at: string;
  readonly starter_questions: readonly string[];
}

const GROUPS = ['dsp_only', 'sponsored_only', 'dsp_and_sponsored'] as const;
const EMPTY_MARKET_TEXT = 'No cohorts were measured for this vintage.';
const INSUFFICIENT_MARKET_TEXT = 'There is not enough cohort evidence to show Retention safely.';
const DATE = /^\d{4}-\d{2}-\d{2}$/;
const MONTH = /^\d{4}-(0[1-9]|1[0-2])$/;
const DECIMAL = /^-?(0|[1-9]\d*)(\.\d+)?$/;
const REF = /^[A-Za-z0-9_-]{43}$/;
const fail = (): never => { throw new TypeError('The Retention artifact is malformed.'); };
const exact = (value: unknown, keys: readonly string[]): Record<string, unknown> => {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return fail();
  const item = value as Record<string, unknown>;
  const found = Object.keys(item);
  if (found.length !== keys.length || found.some((key) => !keys.includes(key))) return fail();
  return item;
};
const text = (value: unknown): string => typeof value === 'string' && value.trim() ? value : fail();
const nullableText = (value: unknown): string | null => value === null ? null : text(value);
const isoDate = (value: unknown): string => {
  const result = text(value);
  const parsed = new Date(`${result}T00:00:00Z`);
  return DATE.test(result) && !Number.isNaN(parsed.valueOf()) && parsed.toISOString().slice(0, 10) === result ? result : fail();
};
const month = (value: unknown): string => MONTH.test(text(value)) ? value as string : fail();
const oneOf = <const T extends readonly string[]>(value: unknown, values: T): T[number] => {
  const result = text(value);
  return values.includes(result) ? result as T[number] : fail();
};
const array = (value: unknown): readonly unknown[] => Array.isArray(value) ? value : fail();
const positiveInteger = (value: unknown): number => Number.isSafeInteger(value) && (value as number) > 0 ? value as number : fail();

function figure(value: unknown): RetentionFigure {
  const item = exact(value, ['text', 'value']);
  const raw = text(item.value);
  if (!DECIMAL.test(raw)) return fail();
  return { text: text(item.text), value: raw };
}
function nullableFigure(value: unknown): RetentionFigure | null { return value === null ? null : figure(value); }
function cell(value: unknown, expectedOffset: number, grain: CohortGrain, path: 'standard' | 'paid'): RetentionCell {
  const item = exact(value, ['cohort_size', 'column_label', 'offset', 'rate', 'state', 'text']);
  const offset = positiveInteger(item.offset);
  if (offset !== expectedOffset || item.column_label !== `${grain === 'month' ? 'Month' : 'Quarter'} ${offset}`) return fail();
  const state = oneOf(item.state, ['value', 'not_returned', 'withheld', 'not_yet'] as const);
  const rate = nullableFigure(item.rate);
  const cohort_size = nullableFigure(item.cohort_size);
  const statusText = nullableText(item.text);
  if (state === 'value') {
    if (rate === null || cohort_size === null || statusText !== null) return fail();
  } else if (rate !== null || cohort_size !== null || statusText === null || (path === 'standard' && state === 'withheld')) return fail();
  return { cohort_size, column_label: text(item.column_label), offset, rate, state, text: statusText };
}
function row(value: unknown, grain: CohortGrain, path: 'standard' | 'paid'): RetentionRow {
  const item = exact(value, ['absent_text', 'cells', 'cohort', 'label']);
  const cellsValue = array(item.cells);
  const absent_text = nullableText(item.absent_text);
  if ((absent_text !== null) !== (cellsValue.length === 0)) return fail();
  const expectedCells = grain === 'month' ? 12 : 3;
  if (absent_text === null && cellsValue.length !== expectedCells) return fail();
  const cells = cellsValue.map((entry, index) => cell(entry, index + 1, grain, path));
  const cohortSizes = cells
    .filter((entry) => entry.state === 'value')
    .map((entry) => `${entry.cohort_size!.value}\u0000${entry.cohort_size!.text}`);
  if (new Set(cohortSizes).size > 1) return fail();
  return {
    absent_text,
    cells,
    cohort: isoDate(item.cohort),
    label: text(item.label),
  };
}
function grid(value: unknown, expectedGroup: RetentionGroup, grain: CohortGrain, path: 'standard' | 'paid'): RetentionGrid {
  const item = exact(value, ['grid_label', 'group', 'label', 'rows']);
  if (item.group !== expectedGroup) return fail();
  const rows = array(item.rows).map((entry) => row(entry, grain, path));
  if (rows.some((entry, index) => index > 0 && rows[index - 1]!.cohort >= entry.cohort)) return fail();
  return { grid_label: text(item.grid_label), group: expectedGroup, label: text(item.label), rows };
}
function grids(value: unknown, required: boolean, grain: CohortGrain, path: 'standard' | 'paid'): readonly RetentionGrid[] {
  const entries = array(value);
  if ((!required && entries.length !== 0) || (required && entries.length !== GROUPS.length)) return fail();
  const parsed = entries.map((entry, index) => grid(entry, GROUPS[index]!, grain, path));
  if (parsed.length > 1 && parsed.some((entry) => entry.rows.map((row) => row.cohort).join('\u0000') !== parsed[0]!.rows.map((row) => row.cohort).join('\u0000'))) return fail();
  return parsed;
}
function point(value: unknown, expectedOffset: number, grain: CohortGrain): RetentionCurvePoint {
  const item = exact(value, ['label', 'offset', 'rate', 'text']);
  const offset = positiveInteger(item.offset);
  if (offset !== expectedOffset || item.label !== `${grain === 'month' ? 'Month' : 'Quarter'} ${offset}`) return fail();
  const rate = nullableFigure(item.rate);
  const statusText = nullableText(item.text);
  if ((rate === null) !== (statusText !== null)) return fail();
  return { label: text(item.label), offset, rate, text: statusText };
}
function line(value: unknown, expectedGroup: RetentionGroup, grain: CohortGrain): RetentionCurveLine {
  const item = exact(value, ['group', 'label', 'points']);
  if (item.group !== expectedGroup) return fail();
  const points = array(item.points);
  if (points.length !== (grain === 'month' ? 12 : 3)) return fail();
  return { group: expectedGroup, label: text(item.label), points: points.map((entry, index) => point(entry, index + 1, grain)) };
}
function insight(value: unknown): RetentionInsight {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return fail();
  const raw = value as Record<string, unknown>;
  const state = oneOf(raw.state, ['filled', 'silent'] as const);
  const item = exact(value, state === 'silent'
    ? ['absence_class', 'absence_reason', 'artifact_revision', 'evidence_line', 'id', 'output_class', 'prompt_version', 'state', 'text']
    : ['artifact_revision', 'evidence_line', 'id', 'output_class', 'prompt_version', 'state', 'text']);
  const parsed = {
    absence_class: item.absence_class === undefined ? null : nullableText(item.absence_class),
    absence_reason: item.absence_reason === undefined ? null : nullableText(item.absence_reason),
    artifact_revision: text(item.artifact_revision), evidence_line: nullableText(item.evidence_line),
    id: oneOf(item.id, ['retention-shift'] as const), output_class: oneOf(item.output_class, ['perspective'] as const),
    prompt_version: text(item.prompt_version), state, text: nullableText(item.text),
  };
  if (!REF.test(parsed.artifact_revision)) return fail();
  if (state === 'filled' ? parsed.text === null || parsed.evidence_line === null : parsed.text !== null || parsed.evidence_line !== null || parsed.absence_class === null || parsed.absence_reason === null) return fail();
  return parsed;
}
function market(value: unknown): RetentionMarket {
  const item = exact(value, ['absence_text', 'cohort_grain', 'curve', 'grain_note', 'insights', 'marketplace_key', 'marketplace_text', 'outcome', 'path', 'scope_band', 'triangle']);
  const outcome = oneOf(item.outcome, ['complete', 'empty', 'insufficient'] as const);
  const absence_text = nullableText(item.absence_text);
  if (absence_text !== (outcome === 'complete' ? null : outcome === 'empty' ? EMPTY_MARKET_TEXT : INSUFFICIENT_MARKET_TEXT)) return fail();
  const cohort_grain = item.cohort_grain === null ? null : oneOf(item.cohort_grain, ['month', 'quarter'] as const);
  const scope_band = nullableText(item.scope_band);
  const grain_note = nullableText(item.grain_note);
  if ((cohort_grain === null) !== (scope_band === null) || (grain_note !== null) !== (cohort_grain === 'quarter')) return fail();
  const path = exact(item.path, ['paid', 'standard']);
  const standard = exact(path.standard, ['available']);
  if (typeof standard.available !== 'boolean' || standard.available !== (cohort_grain !== null)) return fail();
  const paid = exact(path.paid, ['callout', 'state']);
  const paidState = oneOf(paid.state, ['observed', 'not_observed'] as const);
  const callout = nullableText(paid.callout);
  if ((paidState === 'not_observed') !== (callout !== null)) return fail();
  const triangle = exact(item.triangle, ['identity_caveat', 'legend', 'paid', 'standard']);
  const grain = cohort_grain ?? 'month';
  const standardGrids = grids(triangle.standard, cohort_grain !== null, grain, 'standard');
  const paidGrids = grids(triangle.paid, paidState === 'observed' && cohort_grain !== null, grain, 'paid');
  const hasValue = standardGrids.some((grid) => grid.rows.some((row) => row.cells.some((entry) => entry.state === 'value')));
  const derived = cohort_grain === null ? 'empty' : hasValue ? 'complete' : 'insufficient';
  if (outcome !== derived) return fail();
  const curve = exact(item.curve, ['label', 'lines', 'metric']);
  if (curve.metric !== 'cumulative_repeat_rate_account_ids') return fail();
  const linesValue = array(curve.lines);
  if ((cohort_grain === null) !== (linesValue.length === 0) || (linesValue.length !== 0 && linesValue.length !== GROUPS.length)) return fail();
  const insights = array(item.insights);
  if (insights.length !== 1) return fail();
  return {
    absence_text, cohort_grain,
    curve: { label: text(curve.label), lines: linesValue.map((entry, index) => line(entry, GROUPS[index]!, grain)), metric: 'cumulative_repeat_rate_account_ids' },
    grain_note, insights: [insight(insights[0])], marketplace_key: text(item.marketplace_key), marketplace_text: nullableText(item.marketplace_text), outcome,
    path: { paid: { callout, state: paidState }, standard: { available: standard.available } }, scope_band,
    triangle: { identity_caveat: text(triangle.identity_caveat), legend: text(triangle.legend), paid: paidGrids, standard: standardGrids },
  };
}
function evidenceStatus(value: unknown, start: string, end: string, missingMarkets: readonly string[]): RetentionCohortArtifact['evidence_status'] {
  const item = exact(value, ['slots', 'version']);
  if (item.version !== 2) return fail();
  const rawSlots = array(item.slots);
  if (rawSlots.length !== missingMarkets.length) return fail();
  const slots = rawSlots.map((value, index) => {
    const slot = exact(value, ['marketplace_key', 'missing_dates', 'reason_text', 'recovery_text', 'role_label', 'slot_label']);
    const dates = array(slot.missing_dates);
    if (dates.length !== 1) return fail();
    const range = exact(dates[0], ['end', 'start']);
    if (
      isoDate(range.start) !== start || isoDate(range.end) !== end || slot.recovery_text !== null
      || slot.marketplace_key !== missingMarkets[index] || slot.reason_text !== EMPTY_MARKET_TEXT
      || slot.role_label !== 'This vintage' || slot.slot_label !== 'Cohorts'
    ) return fail();
    return {
      marketplace_key: text(slot.marketplace_key),
      missing_dates: [{ end: isoDate(range.end), start: isoDate(range.start) }],
      reason_text: text(slot.reason_text),
      recovery_text: null,
      role_label: text(slot.role_label),
      slot_label: text(slot.slot_label),
    };
  });
  return { slots, version: 2 };
}

export function parseRetentionCohortArtifact(value: unknown): RetentionCohortArtifact {
  const item = exact(value, ['boundary', 'evidence_status', 'markets', 'outcome', 'page_context_expires_at', 'page_context_label', 'starter_questions', 'type', 'version', 'vintage']);
  if (item.type !== 'RetentionCohortArtifact' || item.version !== 1) return fail();
  const outcome = oneOf(item.outcome, ['complete', 'empty', 'insufficient'] as const);
  const vintage = exact(item.vintage, ['as_of_month', 'end', 'label', 'start']);
  const start = isoDate(vintage.start); const end = isoDate(vintage.end); const as_of_month = month(vintage.as_of_month);
  if (end < start || !end.startsWith(as_of_month)) return fail();
  const markets = array(item.markets).map(market);
  if (markets.length === 0 || new Set(markets.map((entry) => entry.marketplace_key)).size !== markets.length) return fail();
  // One market reads as the mockups; two or more each name a distinct marketplace.
  const texts = markets.map((entry) => entry.marketplace_text);
  if ((markets.length === 1 ? texts[0] !== null : texts.some((entry) => entry === null)) || new Set(texts).size !== texts.length) return fail();
  const derived = markets.some((entry) => entry.outcome === 'complete') ? 'complete' : markets.some((entry) => entry.outcome === 'insufficient') ? 'insufficient' : 'empty';
  if (outcome !== derived) return fail();
  const boundary = array(item.boundary).map(text);
  if (boundary.length !== 3) return fail();
  const starter_questions = array(item.starter_questions).map(text);
  if (outcome === 'complete' ? starter_questions.length !== 1 : starter_questions.length !== 0) return fail();
  return {
    boundary, evidence_status: evidenceStatus(item.evidence_status, start, end, markets.filter((market) => market.cohort_grain === null).map((market) => market.marketplace_key)), markets, outcome,
    page_context_expires_at: text(item.page_context_expires_at), page_context_label: text(item.page_context_label),
    starter_questions, type: 'RetentionCohortArtifact', version: 1,
    vintage: { as_of_month, end, label: text(vintage.label), start },
  };
}

/**
 * The Retention answer (plan template): at the last offset all three served curve lines carry a
 * rate, each group's served rate text; otherwise the served absence for the vintage.
 */
export function retentionAnswer(artifact: RetentionCohortArtifact): Answer {
  const market = artifact.markets[0];
  const absent = (reason: string | null): Answer => ({
    kind: 'fallback',
    text: `Repeat purchase for ${artifact.vintage.label} isn't measured yet${reason === null ? '.' : `: ${reason}`}`,
  });
  if (market === undefined) return absent(null);
  const lines = market.curve.lines;
  const line = (group: RetentionGroup) => lines.find((entry) => entry.group === group) ?? null;
  const dsp = line('dsp_only');
  const sponsored = line('sponsored_only');
  const both = line('dsp_and_sponsored');
  if (dsp === null || sponsored === null || both === null) return absent(market.absence_text);
  const at = (entry: RetentionCurveLine, offset: number) => entry.points.find((point) => point.offset === offset)?.rate ?? null;
  const offsets = dsp.points.map((point) => point.offset).filter((offset) => at(dsp, offset) !== null && at(sponsored, offset) !== null && at(both, offset) !== null);
  const last = offsets.at(-1);
  if (last === undefined) return absent(market.absence_text);
  const label = dsp.points.find((point) => point.offset === last)!.label;
  return {
    kind: 'answer',
    text: `By ${label}, ${at(dsp, last)!.text} of DSP-only customers bought again, against ${at(sponsored, last)!.text} sponsored-only and ${at(both, last)!.text} both.`,
  };
}
