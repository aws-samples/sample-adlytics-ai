// SPDX-License-Identifier: MIT-0
/**
 * The server's two-place `display_value` beside an exact metric value (ADR-084).
 *
 * The server owns the rounding; the browser only checks the served shape and never re-derives
 * it, which would be a second implementation of the rule. A currency value carries a canonical
 * decimal with exactly two fraction digits; every other value carries null.
 */
const DISPLAY_VALUE = /^-?(?:0|[1-9]\d*)\.\d{2}$/;

/** Whether `value` is the display value a metric of `unitKind` must carry. */
export function isDisplayValue(value: unknown, unitKind: string): value is string | null {
  if (unitKind !== 'currency') return value === null;
  return typeof value === 'string' && DISPLAY_VALUE.test(value) && value !== '-0.00';
}
