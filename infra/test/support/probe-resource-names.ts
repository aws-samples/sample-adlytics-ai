// SPDX-License-Identifier: MIT-0
/**
 * Collect the resource names validation probes read and the names the app synthesizes.
 *
 * Used by `probe-resource-names.test.ts`. A probe that reads a runtime resource by a name no stack
 * synthesizes (S6B-1: expected-alarm names that did not exist) treats a real alarm as unexpected,
 * or waits on a queue that is never there, and the probe still reports a verdict. The names here
 * are compared as suffixes after `adlytics-<env>-`.
 *
 * A candidate is:
 * - (a) a Python string literal (f-string or plain, single-line or multiline: triple-quoted, or an
 *   f-string whose replacement fields span lines) holding `adlytics-{<expr>}-<suffix>` or
 *   `adlytics-dev-<suffix>` (the `{<expr>}` field read with balanced braces, so it may hold a
 *   nested dict, subscript or string) whose suffix is fully literal and does not end in `-`, at the
 *   literal's start or embedded where a name starts (an ARN's `role/` or `:`, a `/aws/lambda/` log
 *   group path, the `aws-waf-logs-` prefix), reported on the line the name starts. Literals are
 *   read by a lexer that skips comments, those inside a replacement field included; a literal it
 *   cannot follow throws rather than hiding a name;
 * - (b) a string entry of a tuple or list constant whose name ends in `_ALARMS`, `_ALARM_NAMES`,
 *   `_ALARM_SUFFIXES`, `_QUEUES` or `_FUNCTIONS`, read as a suffix.
 *
 * The name ends at the first character outside a placeholder that a resource name cannot contain,
 * so `adlytics-{env}-ingestion-dlq:visible`, a counter key, is read as `ingestion-dlq`.
 *
 * A literal with a placeholder in its suffix, or a constant entry that is not one string literal
 * (a name, a starred generator), is dynamic: it is counted and reported, never silently dropped. A
 * suffix ending in `-` is a prefix used for matching, and is counted the same way. A candidate on
 * a line carrying, or directly below, `# probe-owned-resource: <reason>` names a resource the
 * probe creates itself; it is listed with its reason rather than checked. A marker without a
 * reason throws, and a marker that exempts no candidate is reported as stale.
 */

export interface Candidate {
  readonly path: string;
  readonly line: number;
  readonly suffix: string;
}

export interface ExemptCandidate extends Candidate {
  readonly reason: string;
}

export interface Collected {
  /** Fully literal suffixes, checked against the synthesized universe. */
  readonly candidates: readonly Candidate[];
  /** Suffixes with a placeholder, a trailing `-`, or a non-literal constant entry. */
  readonly dynamic: readonly Candidate[];
  /** Candidates marked `# probe-owned-resource: <reason>`. */
  readonly exempt: readonly ExemptCandidate[];
  /** `path:line` of each marker that exempts no candidate: a stale exemption hides nothing. */
  readonly staleMarkers: readonly string[];
}

export const CONSTANT_NAME_ENDINGS = [
  '_ALARMS',
  '_ALARM_NAMES',
  '_ALARM_SUFFIXES',
  '_QUEUES',
  '_FUNCTIONS',
] as const;

const EXEMPTION = /#\s*probe-owned-resource:\s*(\S.*)$/;
const EXEMPTION_WITHOUT_REASON = /#\s*probe-owned-resource:\s*$/;
const ANY_EXEMPTION = /#\s*probe-owned-resource:/;
const STRING_PREFIX = /[rRbBfFuU]/;
const WORD = /[A-Za-z0-9_]/;
/** `adlytics-` where a name starts: at the literal's start or after `/`, `:`, `-` or another
 * non-word character (an ARN, a log group path, a WAF log group name). `nameStarts` reads what
 * follows: `dev-`, or one balanced `{expr}` placeholder then `-`. */
const NAME_PREFIX = /(?<![A-Za-z0-9_])adlytics-/g;
const CONSTANT_START = new RegExp(
  `^(_?[A-Z][A-Z0-9_]*(?:${CONSTANT_NAME_ENDINGS.join('|')}))\\s*(?::[^=\\n]*)?=\\s*([(\\[])`,
  'gm',
);
const SINGLE_LITERAL = /^([rRbBfFuU]{0,2})(["'])((?:\\.|(?!\2)[^\\\n])*)\2$/;

function lineOf(source: string, index: number): number {
  let line = 1;
  for (let i = 0; i < index; i += 1) {
    if (source.charCodeAt(i) === 10) line += 1;
  }
  return line;
}

/** The exemption reason on `line` or the line above it, `''` for a bare marker, else undefined. */
function exemptionFor(lines: readonly string[], line: number): string | undefined {
  for (const text of [lines[line - 1], lines[line - 2]]) {
    if (text === undefined) continue;
    const match = EXEMPTION.exec(text);
    if (match?.[1] !== undefined) return match[1].trim();
    if (EXEMPTION_WITHOUT_REASON.test(text)) return '';
  }
  return undefined;
}

/** Cut a literal suffix at the first character, outside a `{...}` placeholder, no name can hold. */
function resourceName(suffix: string): string {
  let depth = 0;
  for (let i = 0; i < suffix.length; i += 1) {
    const ch = suffix[i] as string;
    if (ch === '{') depth += 1;
    else if (ch === '}') depth -= 1;
    else if (depth === 0 && !/[A-Za-z0-9_.-]/.test(ch)) return suffix.slice(0, i);
  }
  return suffix;
}

/** Offset of the first character of `text` that is neither whitespace nor inside a comment. */
function firstCodeOffset(text: string): number {
  let i = 0;
  while (i < text.length) {
    if (/\s/.test(text[i] as string)) {
      i += 1;
    } else if (text[i] === '#') {
      while (i < text.length && text[i] !== '\n') i += 1;
    } else {
      return i;
    }
  }
  return i;
}

/** One Python string literal's body, as written in the source, and the offset it starts at. */
interface Literal {
  readonly body: string;
  readonly bodyStart: number;
  readonly fString: boolean;
}

/**
 * Skip one string literal whose opening quote is at `open`; return its body span and its end.
 *
 * Follows Python's lexical rules for the forms probes use: single- and triple-quoted, a backslash
 * escaping the next character (a line continuation included), and, in an f-string, replacement
 * fields (read by `fieldEnd`) that may span lines and hold nested strings and comments (PEP 701),
 * with `{{`/`}}` as literal braces.
 * Anything else, a single-quoted literal running into a newline or a literal the file never
 * closes, is refused by throwing, so a name in syntax this lexer does not follow is never
 * silently skipped.
 */
function skipLiteral(
  path: string,
  source: string,
  open: number,
  fString: boolean,
): { bodyStart: number; bodyEnd: number; end: number } {
  const mark = source[open] as string;
  const triple = source.startsWith(mark.repeat(3), open);
  const quote = triple ? mark.repeat(3) : mark;
  const bodyStart = open + quote.length;
  let i = bodyStart;
  while (i < source.length) {
    const ch = source[i] as string;
    if (source.startsWith(quote, i)) {
      return { bodyStart, bodyEnd: i, end: i + quote.length };
    }
    if (ch === '\\') {
      i += 2;
      continue;
    }
    if (ch === '\n' && !triple) break;
    if (fString && ch === '{') {
      if (source[i + 1] === '{') {
        i += 2;
        continue;
      }
      const end = fieldEnd(path, source, i);
      if (end === -1) break;
      i = end;
      continue;
    }
    if (fString && ch === '}') {
      i += source[i + 1] === '}' ? 2 : 1;
      continue;
    }
    i += 1;
  }
  throw new Error(
    `${path}:${lineOf(source, open)} unsupported or unterminated string literal; ` +
      'the probe-name collector cannot read it',
  );
}

/**
 * End (exclusive) of the f-string replacement field whose `{` is at `open` in `text`, or -1 if
 * `text` ends first.
 *
 * Follows PEP 701 inside the field. In the expression, brackets nest, a backslash escapes the next
 * character, a nested string is skipped whole (with `skipLiteral`), and `#` starts a comment that
 * runs to the end of the physical line, so a brace or quote in the comment is not code. A `:`
 * outside any bracket starts the format spec, where `#` is text (`{n:#x}`) and `{` opens a nested
 * field read the same way.
 */
function fieldEnd(path: string, text: string, open: number): number {
  const closers: string[] = [];
  let spec = false;
  let i = open + 1;
  while (i < text.length) {
    const ch = text[i] as string;
    if (ch === '\\') {
      i += 2;
      continue;
    }
    if (spec) {
      if (ch === '}') return i + 1;
      if (ch === '{') {
        const end = fieldEnd(path, text, i);
        if (end === -1) return -1;
        i = end;
        continue;
      }
    } else if (ch === '#') {
      while (i < text.length && text[i] !== '\n') i += 1;
      continue;
    } else if (ch === '"' || ch === "'") {
      i = skipLiteral(path, text, i, prefixAt(text, i).includes('f')).end;
      continue;
    } else if (ch === '(' || ch === '[' || ch === '{') {
      closers.push(ch === '(' ? ')' : ch === '[' ? ']' : '}');
    } else if (closers.length > 0) {
      if (ch === closers[closers.length - 1]) closers.pop();
    } else if (ch === '}') {
      return i + 1;
    } else if (ch === ':') {
      spec = true;
    }
    i += 1;
  }
  return -1;
}

/** The lowercased string prefix (`f`, `rb`, ...) directly before the quote at `quote`, or `''`. */
function prefixAt(source: string, quote: number): string {
  let start = quote;
  while (start > 0 && quote - start < 2 && STRING_PREFIX.test(source[start - 1] as string)) {
    start -= 1;
  }
  // Letters that end an identifier are not a prefix (Python has no `x"..."` form anyway).
  if (start > 0 && WORD.test(source[start - 1] as string)) return '';
  return source.slice(start, quote).toLowerCase();
}

/** Every string literal in a Python source, single-line or multiline, outside comments. */
function pythonLiterals(path: string, source: string): Literal[] {
  const literals: Literal[] = [];
  let i = 0;
  while (i < source.length) {
    const ch = source[i] as string;
    if (ch === '#') {
      while (i < source.length && source[i] !== '\n') i += 1;
    } else if (ch === '"' || ch === "'") {
      const fString = prefixAt(source, i).includes('f');
      const span = skipLiteral(path, source, i, fString);
      literals.push({
        body: source.slice(span.bodyStart, span.bodyEnd),
        bodyStart: span.bodyStart,
        fString,
      });
      i = span.end;
    } else {
      i += 1;
    }
  }
  return literals;
}

/**
 * End (exclusive) of the `{...}` placeholder opening at `open` in a literal body, or -1 if it
 * never closes.
 *
 * In an f-string this is a replacement field, read by `fieldEnd`, the reader that already found
 * this literal's end: so `{ {'}': env}['}'] }` closes at its last brace and `{env # }\n}` at the
 * brace after the comment. In a plain string the braces are text and only nest.
 */
function placeholderEnd(path: string, literal: Literal, open: number): number {
  const { body } = literal;
  if (literal.fString) return fieldEnd(path, body, open);
  let depth = 0;
  for (let i = open; i < body.length; i += 1) {
    if (body[i] === '{') {
      depth += 1;
    } else if (body[i] === '}') {
      depth -= 1;
      if (depth === 0) return i + 1;
    }
  }
  return -1;
}

/**
 * Every name in one literal: its body offset and the suffix after `adlytics-<env>-`.
 *
 * After `adlytics-` comes `dev-`, or a placeholder read by `placeholderEnd` then `-`. A placeholder
 * that never closes yields its unread rest, which holds `{` and so counts as dynamic: a name in
 * syntax this reader cannot follow is reported, never dropped. An f-string's `{{`, a literal brace,
 * starts no placeholder.
 */
function nameStarts(path: string, literal: Literal): { at: number; suffix: string }[] {
  const { body } = literal;
  const names: { at: number; suffix: string }[] = [];
  for (const match of body.matchAll(NAME_PREFIX)) {
    const at = match.index ?? 0;
    const after = at + match[0].length;
    if (body.startsWith('dev-', after)) {
      names.push({ at, suffix: resourceName(body.slice(after + 'dev-'.length)) });
    } else if (body[after] === '{' && !(literal.fString && body[after + 1] === '{')) {
      const end = placeholderEnd(path, literal, after);
      if (end === -1) names.push({ at, suffix: body.slice(after) });
      else if (body[end] === '-') names.push({ at, suffix: resourceName(body.slice(end + 1)) });
    }
  }
  return names;
}

function isDynamic(suffix: string): boolean {
  return suffix === '' || suffix.includes('{') || suffix.endsWith('-');
}

/**
 * Split a bracketed Python constant body into its top-level entries, with each entry's start
 * offset. Strings and comments are skipped while tracking depth, so a comma or bracket inside
 * them does not split an entry.
 */
function topLevelEntries(source: string, open: number): { text: string; start: number }[] {
  const closeFor: Record<string, string> = { '(': ')', '[': ']', '{': '}' };
  const stack: string[] = [];
  const entries: { text: string; start: number }[] = [];
  let entryStart = open + 1;
  let i = open;
  while (i < source.length) {
    const ch = source[i] as string;
    if (ch === '#') {
      while (i < source.length && source[i] !== '\n') i += 1;
      continue;
    }
    if (ch === '"' || ch === "'") {
      const triple = source.startsWith(ch.repeat(3), i);
      const quote = triple ? ch.repeat(3) : ch;
      i += quote.length;
      while (i < source.length && !source.startsWith(quote, i)) {
        i += source[i] === '\\' ? 2 : 1;
      }
      i += quote.length;
      continue;
    }
    if (ch in closeFor) {
      stack.push(closeFor[ch] as string);
    } else if (ch === stack[stack.length - 1]) {
      stack.pop();
      if (stack.length === 0) {
        entries.push({ text: source.slice(entryStart, i), start: entryStart });
        return entries;
      }
    } else if (ch === ',' && stack.length === 1) {
      entries.push({ text: source.slice(entryStart, i), start: entryStart });
      entryStart = i + 1;
    }
    i += 1;
  }
  throw new Error(`unterminated constant starting at offset ${open}`);
}

function stripComments(entry: string): string {
  return entry
    .split('\n')
    .map((line) => line.replace(/#.*$/, ''))
    .join('\n')
    .trim();
}

/** Collect every candidate in one probe source file. */
export function collectCandidates(path: string, source: string): Collected {
  const lines = source.split('\n');
  const candidates: Candidate[] = [];
  const dynamic: Candidate[] = [];
  const exempt: ExemptCandidate[] = [];

  const classify = (line: number, suffix: string, literal: boolean): void => {
    const candidate = { path, line, suffix };
    const reason = exemptionFor(lines, line);
    if (reason === '') {
      throw new Error(`${path}:${line} probe-owned-resource marker without a reason`);
    }
    if (reason !== undefined) {
      exempt.push({ ...candidate, reason });
    } else if (!literal || isDynamic(suffix)) {
      dynamic.push(candidate);
    } else {
      candidates.push(candidate);
    }
  };

  for (const literal of pythonLiterals(path, source)) {
    for (const name of nameStarts(path, literal)) {
      classify(lineOf(source, literal.bodyStart + name.at), name.suffix, true);
    }
  }

  for (const match of source.matchAll(CONSTANT_START)) {
    const open = (match.index ?? 0) + match[0].length - 1;
    for (const entry of topLevelEntries(source, open)) {
      const body = stripComments(entry.text);
      if (body === '') continue; // a trailing comma
      const line = lineOf(source, entry.start + firstCodeOffset(entry.text));
      const literal = SINGLE_LITERAL.exec(body);
      if (literal === null) {
        classify(line, body.replace(/\s+/g, ' '), false);
        continue;
      }
      const value = String(literal[3]);
      if (value.startsWith('adlytics-')) continue; // read by rule (a) on its own line
      classify(line, resourceName(value), true);
    }
  }
  const used = new Set(exempt.map((candidate) => candidate.line));
  const staleMarkers = lines.flatMap((text, index) =>
    ANY_EXEMPTION.test(text) && !used.has(index + 1) && !used.has(index + 2)
      ? [`${path}:${index + 1}`]
      : [],
  );
  const byLine = (a: Candidate, b: Candidate): number => a.line - b.line;
  return {
    candidates: candidates.sort(byLine),
    dynamic: dynamic.sort(byLine),
    exempt: exempt.sort(byLine),
    staleMarkers,
  };
}

/** Keys whose string values are token identifiers (an audience or a client id), not names. */
const IDENTIFIER_KEYS = new Set(['AllowedAudience', 'AllowedClients', 'ClientId']);
/** Lambda environment variables that carry a token identifier to the workload. */
const IDENTIFIER_VARIABLE = /(_AUDIENCE|_CLIENT_ID)$/;

/**
 * Every `adlytics-<env>-` suffix the synthesized templates define:
 * - each string property, at any depth of a resource's `Properties`, whose key ends in `Name`, read
 *   wherever a name starts inside it (so a `/aws/lambda/adlytics-dev-X` log group yields `X`); a
 *   FIFO queue is also known by its `.fifo` name, which SQS requires;
 * - each token identifier a probe presents or expects: the string values, at any depth, of
 *   `AllowedAudience`, `AllowedClients` and `ClientId`, and of every Lambda environment variable
 *   ending in `_AUDIENCE` or `_CLIENT_ID`. These are synthesized from the same config helpers
 *   (`harnessTokenAudience` and its siblings) and are what the token service and authorizers check.
 */
export function synthesizedNameSuffixes(
  templates: Record<string, unknown>,
  envName: string,
): Set<string> {
  const prefix = `adlytics-${envName}-`;
  const suffixes = new Set<string>();
  // A name may sit inside a path-shaped name (`/aws/lambda/adlytics-dev-X`, `aws-waf-logs-…`), so
  // every occurrence where a name starts counts, as the collector reads probe literals.
  const escaped = prefix.replace(/[.*+?^$()|[\]{}\\]/g, '\\$&');
  const start = new RegExp('(?<![A-Za-z0-9_])' + escaped, 'g');
  const addEmbedded = (text: string): void => {
    for (const match of text.matchAll(start)) {
      const name = resourceName(text.slice((match.index ?? 0) + match[0].length));
      if (name !== '') suffixes.add(name);
    }
  };
  const visit = (value: unknown): void => {
    if (Array.isArray(value)) {
      value.forEach(visit);
      return;
    }
    if (value === null || typeof value !== 'object') return;
    for (const [key, child] of Object.entries(value as Record<string, unknown>)) {
      if (key.endsWith('Name') && typeof child === 'string') addEmbedded(child);
      if (IDENTIFIER_KEYS.has(key)) {
        for (const identifier of [child].flat()) {
          if (typeof identifier === 'string' && identifier.startsWith(prefix)) {
            suffixes.add(identifier.slice(prefix.length));
          }
        }
      }
      visit(child);
    }
  };
  const addVariables = (properties: Record<string, unknown>): void => {
    const environment = properties.Environment as { Variables?: Record<string, unknown> } | undefined;
    for (const [name, value] of Object.entries(environment?.Variables ?? {})) {
      if (IDENTIFIER_VARIABLE.test(name) && typeof value === 'string' && value.startsWith(prefix)) {
        suffixes.add(value.slice(prefix.length));
      }
    }
  };
  for (const template of Object.values(templates)) {
    const resources = ((template as Record<string, unknown>).Resources ?? {}) as Record<
      string,
      { Type?: string; Properties?: Record<string, unknown> }
    >;
    for (const resource of Object.values(resources)) {
      const properties = resource.Properties ?? {};
      visit(properties);
      if (resource.Type === 'AWS::Lambda::Function') addVariables(properties);
      const queueName = properties.QueueName;
      if (
        resource.Type === 'AWS::SQS::Queue' &&
        properties.FifoQueue === true &&
        typeof queueName === 'string' &&
        queueName.startsWith(prefix) &&
        !queueName.endsWith('.fifo')
      ) {
        suffixes.add(`${queueName.slice(prefix.length)}.fifo`);
      }
    }
  }
  return suffixes;
}

/** One inventory entry: the probe file and the suffix as collected (no line, so edits do not move it). */
export type InventoryEntry = readonly [path: string, suffix: string];

/**
 * Which probe names the check reads (checked) and which it cannot (dynamic), as a multiset.
 *
 * Committed in `probe-resource-inventory.ts`, so coverage cannot shrink unseen: a name that moves
 * from checked to dynamic, or that the collector stops finding at all, is drift until the
 * inventory is edited to say so.
 */
export interface Inventory {
  readonly checked: readonly InventoryEntry[];
  readonly dynamic: readonly InventoryEntry[];
}

function sortedEntries(candidates: readonly Candidate[]): InventoryEntry[] {
  return candidates
    .map((candidate): InventoryEntry => [candidate.path, candidate.suffix])
    .sort(([pa, sa], [pb, sb]) => (pa === pb ? (sa < sb ? -1 : sa > sb ? 1 : 0) : pa < pb ? -1 : 1));
}

/** The inventory the collected files hold, sorted by path then suffix. */
export function inventoryOf(collected: readonly Collected[]): Inventory {
  return {
    checked: sortedEntries(collected.flatMap((file) => file.candidates)),
    dynamic: sortedEntries(collected.flatMap((file) => file.dynamic)),
  };
}

function entryCounts(entries: readonly InventoryEntry[]): Map<string, number> {
  const counts = new Map<string, number>();
  for (const [path, suffix] of entries) {
    const key = `${path} ${JSON.stringify(suffix)}`;
    counts.set(key, (counts.get(key) ?? 0) + 1);
  }
  return counts;
}

function bucketDrift(
  bucket: 'checked' | 'dynamic',
  expected: readonly InventoryEntry[],
  actual: readonly InventoryEntry[],
): string[] {
  const want = entryCounts(expected);
  const have = entryCounts(actual);
  const drift: string[] = [];
  for (const key of [...new Set([...want.keys(), ...have.keys()])].sort()) {
    const delta = (have.get(key) ?? 0) - (want.get(key) ?? 0);
    for (let i = 0; i < Math.abs(delta); i += 1) {
      drift.push(`${delta > 0 ? 'newly' : 'no longer'} ${bucket}: ${key}`);
    }
  }
  return drift;
}

/**
 * Every difference between the committed inventory and the collected one, one line per entry:
 * `no longer checked: <path> "<suffix>"`, `newly dynamic: ...` and so on. A checked-to-dynamic
 * transition shows as one `no longer checked` and one `newly dynamic` line.
 */
export function inventoryDrift(expected: Inventory, actual: Inventory): string[] {
  return [
    ...bucketDrift('checked', expected.checked, actual.checked),
    ...bucketDrift('dynamic', expected.dynamic, actual.dynamic),
  ];
}

/** `path:line suffix` for every candidate the universe does not contain. */
export function missingNames(collected: Collected, universe: ReadonlySet<string>): string[] {
  return collected.candidates
    .filter((candidate) => !universe.has(candidate.suffix))
    .map((candidate) => `${candidate.path}:${candidate.line} ${candidate.suffix}`);
}
