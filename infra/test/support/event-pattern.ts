// SPDX-License-Identifier: MIT-0
/**
 * An offline matcher for the EventBridge event-pattern subset the record-integrity patterns use.
 *
 * `TestEventPattern` is an AWS call, so the unit loop cannot use it. This follows the documented
 * semantics instead (EventBridge comparison operators; aws/event-ruler README):
 *
 * - every key in a pattern must match; a `$or` array matches when any alternative does;
 * - a leaf rule is an array that matches when any of its entries matches the event value, and an
 *   array-valued event field matches when any element does;
 * - `exists: false` matches when the leaf is absent, including when a parent object is absent or
 *   the node is an object rather than a leaf; every other operator needs the leaf present;
 * - `anything-but`, `prefix`, `suffix`, and `wildcard` compare strings.
 *
 * An operator it does not implement throws, so a pattern it cannot judge never passes silently.
 */

type Leaf = string | number | boolean | null;

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value);

const isLeaf = (value: unknown): value is Leaf =>
  value === null || ['string', 'number', 'boolean'].includes(typeof value);

/** The leaf values at a path, flattening arrays; empty when the leaf is absent. */
const leaves = (value: unknown): Leaf[] => {
  if (Array.isArray(value)) return value.flatMap(leaves);
  return isLeaf(value) ? [value] : [];
};

const wildcardRegex = (pattern: string): RegExp =>
  new RegExp(`^${pattern.split('*').map((part) => part.replace(/[.+?^${}()|[\]\\]/g, '\\$&')).join('.*')}$`);

function matchesEntry(entry: unknown, present: Leaf[]): boolean {
  if (!isRecord(entry)) return present.some((value) => value === entry);
  const [operator, ...rest] = Object.keys(entry);
  if (operator === undefined || rest.length > 0) throw new Error(`Unsupported rule ${JSON.stringify(entry)}`);
  const operand = entry[operator];
  const strings = present.filter((value): value is string => typeof value === 'string');
  switch (operator) {
    case 'exists':
      return operand === true ? present.length > 0 : present.length === 0;
    case 'prefix':
      return strings.some((value) => value.startsWith(String(operand)));
    case 'suffix':
      return strings.some((value) => value.endsWith(String(operand)));
    case 'wildcard':
      return strings.some((value) => wildcardRegex(String(operand)).test(value));
    case 'anything-but': {
      if (!isLeaf(operand) && !Array.isArray(operand)) {
        throw new Error(`Unsupported anything-but ${JSON.stringify(operand)}`);
      }
      const excluded = Array.isArray(operand) ? operand : [operand];
      return present.length > 0 && present.every((value) => !excluded.includes(value));
    }
    default:
      throw new Error(`Unsupported operator ${operator}`);
  }
}

function matchesAt(rule: unknown, value: unknown): boolean {
  if (Array.isArray(rule)) return rule.some((entry) => matchesEntry(entry, leaves(value)));
  if (!isRecord(rule)) throw new Error(`Unsupported rule ${JSON.stringify(rule)}`);
  // An array of objects in the event matches when any element matches the nested rule.
  if (Array.isArray(value)) return value.some((element) => matchesAt(rule, element));
  return Object.entries(rule).every(([key, nested]) => {
    if (key === '$or') {
      if (!Array.isArray(nested)) throw new Error('$or must be an array');
      return nested.some((alternative) => matchesAt(alternative, value));
    }
    return matchesAt(nested, isRecord(value) ? value[key] : undefined);
  });
}

/** Whether an event matches an event pattern. */
export function matchesEventPattern(pattern: Record<string, unknown>, event: unknown): boolean {
  return matchesAt(pattern, event);
}

/** Whether a record passes a pipe's filter criteria: any one filter admits it. */
export function passesPipeFilters(filters: readonly Record<string, unknown>[], record: unknown): boolean {
  return filters.some((filter) => matchesEventPattern(filter, record));
}
