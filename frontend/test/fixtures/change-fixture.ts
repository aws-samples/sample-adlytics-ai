// SPDX-License-Identifier: MIT-0
import type { Change, ChangeSentiment } from '../../lib/change';

/**
 * A server change block for a fixture comparison (ADR-121). The browser never derives one, so
 * fixtures state it: the direction follows the fixture's signed absolute change, the tone is
 * whatever the test needs (neutral unless given), and the text is the fixture's percentage, or
 * the "New" word state when the prior is zero.
 */
export function changeBlock(
  absolute: number | string,
  text: string | null,
  sentiment: ChangeSentiment = 'neutral',
): Change {
  const source = String(absolute);
  const flat = /^[+-]?0+(?:\.0+)?$/.test(source);
  const direction = flat ? 'flat' : source.startsWith('-') ? 'down' : 'up';
  if (direction === 'flat') return { direction, display_text: 'No change', not_meaningful: null, sentiment: 'neutral', unit: 'percent' };
  if (text === null) return { direction, display_text: 'New', not_meaningful: 'no_prior', sentiment, unit: 'absolute' };
  return { direction, display_text: text, not_meaningful: null, sentiment, unit: 'percent' };
}
