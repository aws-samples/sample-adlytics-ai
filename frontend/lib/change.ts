// SPDX-License-Identifier: MIT-0
/**
 * The server-declared change block every comparison carries (ADR-121).
 *
 * The server states direction, sentiment, unit and the chip's text; the browser renders them
 * and never infers a direction or a tone from a sign. A comparison with a block no longer
 * carries `percentage_change`. Every comparison carries the `change` key: a body without it, the
 * form baked before the block, is refused. That retained read retired in the integration change,
 * because the API, a full re-bake and this bundle deploy together (owner revision, 2026-10-08).
 */

export type ChangeDirection = 'up' | 'down' | 'flat';
export type ChangeSentiment = 'favourable' | 'unfavourable' | 'neutral';
export type ChangeUnit = 'percent' | 'percentage_points' | 'absolute';
export type ChangeNotMeaningful = 'near_zero_prior' | 'negative_prior' | 'no_prior';

export interface Change {
  readonly direction: ChangeDirection;
  readonly sentiment: ChangeSentiment;
  readonly unit: ChangeUnit;
  readonly display_text: string;
  readonly not_meaningful: ChangeNotMeaningful | null;
}

export const CHANGE_KEY = 'change';
export const PERCENTAGE_KEY = 'percentage_change';

const DIRECTIONS: readonly ChangeDirection[] = ['up', 'down', 'flat'];
const SENTIMENTS: readonly ChangeSentiment[] = ['favourable', 'unfavourable', 'neutral'];
const UNITS: readonly ChangeUnit[] = ['percent', 'percentage_points', 'absolute'];
const NOT_MEANINGFUL: readonly ChangeNotMeaningful[] = ['near_zero_prior', 'negative_prior', 'no_prior'];
const FIELDS = ['direction', 'display_text', 'not_meaningful', 'sentiment', 'unit'] as const;

function member<T extends string>(value: unknown, allowed: readonly T[]): T | null {
  return typeof value === 'string' && (allowed as readonly string[]).includes(value) ? value as T : null;
}

/**
 * Decode one block, or `null` for an absent change. Returns `undefined` when the value is
 * malformed, so each artifact decoder fails with its own contract error.
 */
export function parseChange(value: unknown): Change | null | undefined {
  if (value === null) return null;
  if (typeof value !== 'object' || Array.isArray(value)) return undefined;
  const item = value as Record<string, unknown>;
  const keys = Object.keys(item);
  if (keys.length !== FIELDS.length || !FIELDS.every((key) => keys.includes(key))) return undefined;
  const direction = member(item.direction, DIRECTIONS);
  const sentiment = member(item.sentiment, SENTIMENTS);
  const unit = member(item.unit, UNITS);
  const notMeaningful = item.not_meaningful === null ? null : member(item.not_meaningful, NOT_MEANINGFUL);
  const text = item.display_text;
  if (direction === null || sentiment === null || unit === null || notMeaningful === undefined) return undefined;
  if (item.not_meaningful !== null && notMeaningful === null) return undefined;
  if (typeof text !== 'string' || !text.trim()) return undefined;
  // An unchanged value is neutral and meaningful; the server never writes otherwise.
  if (direction === 'flat' && (sentiment !== 'neutral' || notMeaningful !== null)) return undefined;
  return { direction, display_text: text, not_meaningful: notMeaningful, sentiment, unit };
}

/**
 * The exact keys one comparison carries, given its keys without the change fields: the block
 * alone, or the percentage beside `change: null`. A comparison without the key fails `exact`.
 */
export function comparisonFields(value: unknown, base: readonly string[]): readonly string[] {
  return typeof value === 'object' && value !== null && (value as Record<string, unknown>)[CHANGE_KEY] === null
    ? [...base, PERCENTAGE_KEY, CHANGE_KEY]
    : [...base, CHANGE_KEY];
}
