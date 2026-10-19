// SPDX-License-Identifier: MIT-0
/**
 * Strict immutable mirror of the closed public conversation protocols: the ConversationStream.v4
 * streamed Markdown answer (ADR-083) and, until the route switches, the ConversationStream.v3
 * block stream.
 */

export type PublicConversationBlock =
  | { readonly kind: 'preface'; readonly text: string }
  | { readonly kind: 'paragraph'; readonly text: string }
  | { readonly kind: 'grouped_list'; readonly heading: string; readonly items: readonly string[] }
  | { readonly kind: 'governed_table'; readonly caption: string; readonly columns: readonly string[]; readonly rows: readonly (readonly string[])[] }
  | { readonly kind: 'notice'; readonly notice_kind: 'notice' | 'limitation'; readonly title: string; readonly text: string };

export type ConversationActivityStage = 'understanding_request' | 'retrieving_governed_data' | 'composing_response' | 'refining_response' | 'assessing_publication' | 'preparing_public_response';

export type ConversationStreamV3Event =
  | { readonly schema_version: 'ConversationStream.v3'; readonly kind: 'activity'; readonly stage: ConversationActivityStage }
  | { readonly schema_version: 'ConversationStream.v3'; readonly kind: 'tool'; readonly tool_name: string; readonly status: 'requested' | 'completed' }
  | { readonly schema_version: 'ConversationStream.v3'; readonly kind: 'block'; readonly sequence: number; readonly block: PublicConversationBlock }
  | { readonly schema_version: 'ConversationStream.v3'; readonly kind: 'done'; readonly audit_id?: string }
  | { readonly schema_version: 'ConversationStream.v3'; readonly kind: 'refused'; readonly reason_code: 'output_intervened' | 'dependency_unavailable' | 'publication_refused' | 'unsupported_operation'; readonly reason: string; readonly audit_id?: string };

export type ConversationActivityStageV4 = 'understanding_request' | 'retrieving_governed_data';
export type ConversationRefusalReasonCodeV4 = 'dependency_unavailable' | 'no_answer';

/** One ConversationStream.v4 event (ADR-083): progress, a Markdown text delta, or the terminal. */
export type ConversationStreamV4Event =
  | { readonly schema_version: 'ConversationStream.v4'; readonly kind: 'activity'; readonly stage: ConversationActivityStageV4 }
  | { readonly schema_version: 'ConversationStream.v4'; readonly kind: 'tool'; readonly tool_name: string; readonly status: 'requested' | 'completed' }
  | { readonly schema_version: 'ConversationStream.v4'; readonly kind: 'text'; readonly sequence: number; readonly text: string }
  | ConversationContextEventV4
  | { readonly schema_version: 'ConversationStream.v4'; readonly kind: 'done'; readonly audit_id?: string }
  | { readonly schema_version: 'ConversationStream.v4'; readonly kind: 'refused'; readonly reason_code: ConversationRefusalReasonCodeV4; readonly reason: string; readonly audit_id?: string };

/**
 * Whether the turn used the page context it was sent (ADR-120 decision 5). It comes once, before
 * any text, and only when the request carried a reference. When context wasn't used, both server
 * lines come with it, and the browser shows one of them after it refreshes the page.
 */
export type ConversationContextEventV4 =
  | { readonly schema_version: 'ConversationStream.v4'; readonly kind: 'context'; readonly used: true }
  | {
    readonly schema_version: 'ConversationStream.v4';
    readonly kind: 'context';
    readonly used: false;
    readonly reason: 'expired' | 'invalid';
    readonly display_text: string;
    readonly display_text_refresh_failed: string;
  };

export type ConversationStreamEvent = ConversationStreamV3Event | ConversationStreamV4Event;

export interface ConversationTerminal {
  readonly schema_version: 'ConversationTerminal.v2';
  readonly kind: 'refused';
  readonly reason_code: 'input_intervened' | 'context_custody_refused' | 'invalid_request';
  readonly reason: string;
  /** The turn's support reference (ADR-081 §2), present only when its receipt was recorded. */
  readonly audit_id?: string;
}

type JsonValue = null | boolean | number | string | JsonValue[] | { readonly [key: string]: JsonValue };
type ValidatedRecord = {
  readonly [key: string]: JsonValue;
  readonly block: JsonValue;
  readonly caption: JsonValue;
  readonly columns: JsonValue;
  readonly heading: JsonValue;
  readonly items: JsonValue;
  readonly kind: JsonValue;
  readonly notice_kind: JsonValue;
  readonly reason: JsonValue;
  readonly reason_code: JsonValue;
  readonly rows: JsonValue;
  readonly schema_version: JsonValue;
  readonly sequence: JsonValue;
  readonly stage: JsonValue;
  readonly status: JsonValue;
  readonly tool_name: JsonValue;
  readonly text: JsonValue;
  readonly title: JsonValue;
  readonly used: JsonValue;
  readonly display_text: JsonValue;
  readonly display_text_refresh_failed: JsonValue;
};

const MAX_EVENT_BYTES = 72 * 1024;
/** One V4 answer delta; the server bounds each delta to 8 KiB of UTF-8 (ADR-083). */
export const MAX_TEXT_DELTA_BYTES = 8 * 1024;

function fail(label: string): never {
  throw new Error(`Invalid public conversation ${label}.`);
}

class StrictJsonParser {
  private offset = 0;

  constructor(private readonly source: string) {}

  parse(): JsonValue {
    const value = this.value();
    this.space();
    if (this.offset !== this.source.length) this.invalid();
    return value;
  }

  private invalid(): never {
    return fail('JSON');
  }

  private space(): void {
    while (/^[\u0009\u000a\u000d\u0020]$/u.test(this.source[this.offset] ?? '')) {
      this.offset += 1;
    }
  }

  private value(): JsonValue {
    this.space();
    const character = this.source[this.offset];
    if (character === '{') return this.object();
    if (character === '[') return this.array();
    if (character === '"') return this.string();
    if (this.source.startsWith('true', this.offset)) {
      this.offset += 4;
      return true;
    }
    if (this.source.startsWith('false', this.offset)) {
      this.offset += 5;
      return false;
    }
    if (this.source.startsWith('null', this.offset)) {
      this.offset += 4;
      return null;
    }
    if (character === '-' || (character !== undefined && /[0-9]/u.test(character))) {
      return this.number();
    }
    return this.invalid();
  }

  private object(): { readonly [key: string]: JsonValue } {
    this.offset += 1;
    this.space();
    const result: Record<string, JsonValue> = Object.create(null) as Record<string, JsonValue>;
    if (this.source[this.offset] === '}') {
      this.offset += 1;
      return result;
    }
    for (;;) {
      if (this.source[this.offset] !== '"') this.invalid();
      const key = this.string();
      if (Object.hasOwn(result, key)) this.invalid();
      this.space();
      if (this.source[this.offset] !== ':') this.invalid();
      this.offset += 1;
      result[key] = this.value();
      this.space();
      if (this.source[this.offset] === '}') {
        this.offset += 1;
        return result;
      }
      if (this.source[this.offset] !== ',') this.invalid();
      this.offset += 1;
      this.space();
    }
  }

  private array(): JsonValue[] {
    this.offset += 1;
    this.space();
    const result: JsonValue[] = [];
    if (this.source[this.offset] === ']') {
      this.offset += 1;
      return result;
    }
    for (;;) {
      result.push(this.value());
      this.space();
      if (this.source[this.offset] === ']') {
        this.offset += 1;
        return result;
      }
      if (this.source[this.offset] !== ',') this.invalid();
      this.offset += 1;
      this.space();
    }
  }

  private string(): string {
    let value = '';
    this.offset += 1;
    while (this.offset < this.source.length) {
      const character = this.source[this.offset]!;
      if (character === '"') {
        this.offset += 1;
        return value;
      }
      if (character === '\\') {
        this.offset += 1;
        value += this.escape();
        continue;
      }
      const code = character.charCodeAt(0);
      if (code < 0x20 || (code >= 0xdc00 && code <= 0xdfff)) this.invalid();
      if (code >= 0xd800 && code <= 0xdbff) {
        const next = this.source.charCodeAt(this.offset + 1);
        if (next < 0xdc00 || next > 0xdfff) this.invalid();
        value += character + this.source[this.offset + 1];
        this.offset += 2;
        continue;
      }
      value += character;
      this.offset += 1;
    }
    return this.invalid();
  }

  private escape(): string {
    const character = this.source[this.offset];
    if (character === undefined) return this.invalid();
    const simple: Readonly<Record<string, string>> = {
      '"': '"', '\\': '\\', '/': '/', b: '\b', f: '\f', n: '\n', r: '\r', t: '\t',
    };
    if (Object.hasOwn(simple, character)) {
      this.offset += 1;
      return simple[character]!;
    }
    if (character !== 'u') return this.invalid();
    const code = this.hex(this.offset + 1);
    this.offset += 5;
    if (code >= 0xd800 && code <= 0xdbff) {
      if (this.source.slice(this.offset, this.offset + 2) !== '\\u') return this.invalid();
      const low = this.hex(this.offset + 2);
      if (low < 0xdc00 || low > 0xdfff) return this.invalid();
      this.offset += 6;
      return String.fromCharCode(code, low);
    }
    if (code >= 0xdc00 && code <= 0xdfff) return this.invalid();
    return String.fromCharCode(code);
  }

  private hex(start: number): number {
    const raw = this.source.slice(start, start + 4);
    if (!/^[0-9a-f]{4}$/iu.test(raw)) this.invalid();
    return Number.parseInt(raw, 16);
  }

  private number(): number {
    const match = /-?(?:0|[1-9][0-9]*)/uy;
    match.lastIndex = this.offset;
    const raw = match.exec(this.source)?.[0];
    if (raw === undefined) return this.invalid();
    this.offset += raw.length;
    const value = Number(raw);
    if (!Number.isFinite(value)) return this.invalid();
    return value;
  }
}

function parse(serialized: string): JsonValue {
  return new StrictJsonParser(serialized).parse();
}

function record(value: JsonValue, keys: readonly string[], label: string): ValidatedRecord {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) fail(label);
  const actual = Object.keys(value).sort();
  const expected = [...keys].sort();
  if (actual.length !== expected.length || actual.some((key, index) => key !== expected[index])) fail(label);
  return value as ValidatedRecord;
}

/**
 * A terminal record whose `audit_id` key is optional: an API that predates ADR-081 §2 omits it,
 * and a turn whose receipt was not recorded sends null. Every other key stays exact.
 */
function terminalRecord(value: JsonValue, keys: readonly string[], label: string): ValidatedRecord {
  const referenced = value !== null && typeof value === 'object' && !Array.isArray(value) && 'audit_id' in value;
  return record(value, referenced ? [...keys, 'audit_id'] : keys, label);
}
function supportReference(item: ValidatedRecord): { readonly audit_id?: string } {
  const value: JsonValue | undefined = item['audit_id'];
  if (value === undefined || value === null) return {};
  return { audit_id: text(value, 'support reference', 128) };
}
function text(value: JsonValue, label: string, maximum = 4_000): string {
  if (
    typeof value !== 'string'
    || !value.trim()
    || new TextEncoder().encode(value).byteLength > maximum
  ) fail(label);
  return value;
}

/** A V4 answer delta: any non-empty bounded string. Whitespace-only deltas are legal Markdown. */
function delta(value: JsonValue): string {
  if (
    typeof value !== 'string'
    || value.length === 0
    || new TextEncoder().encode(value).byteLength > MAX_TEXT_DELTA_BYTES
  ) fail('text delta');
  return value;
}

/** Nonblank text bounded in Unicode code points, the unit the V4 server contract counts. */
function characters(value: JsonValue, label: string, maximum: number): string {
  if (typeof value !== 'string' || !value.trim() || [...value].length > maximum) fail(label);
  return value;
}

function literal<T extends string>(value: JsonValue, choices: readonly T[], label: string): T {
  if (typeof value !== 'string' || !choices.includes(value as T)) fail(label);
  return value as T;
}

function toolName(value: JsonValue): string {
  // The server's own declared logical tool names: lowercase snake_case, nothing else.
  if (typeof value !== 'string' || !/^[a-z][a-z0-9_]{0,127}$/u.test(value)) fail('tool name');
  return value;
}

function integer(value: JsonValue, label: string): number {
  if (!Number.isSafeInteger(value) || (value as number) < 1) fail(label);
  return value as number;
}

function values(value: JsonValue, label: string, maximum: number, itemMaximum = 500): readonly string[] {
  if (!Array.isArray(value) || value.length < 1 || value.length > maximum) fail(label);
  return value.map((item) => text(item, label, itemMaximum));
}

function block(value: JsonValue): PublicConversationBlock {
  if (value === null || typeof value !== 'object' || Array.isArray(value) || typeof value.kind !== 'string') fail('block');
  switch (value.kind) {
    case 'preface': {
      const item = record(value, ['kind', 'text'], 'preface');
      return { kind: 'preface', text: text(item.text, 'preface', 400) };
    }
    case 'paragraph': {
      const item = record(value, ['kind', 'text'], 'paragraph');
      return { kind: 'paragraph', text: text(item.text, 'paragraph') };
    }
    case 'grouped_list': {
      const item = record(value, ['kind', 'heading', 'items'], 'grouped list');
      return { kind: 'grouped_list', heading: text(item.heading, 'grouped list heading', 160), items: values(item.items, 'grouped list', 24) };
    }
    case 'governed_table': {
      const item = record(value, ['kind', 'caption', 'columns', 'rows'], 'governed table');
      const columns = values(item.columns, 'governed table columns', 8, 160);
      if (new Set(columns).size !== columns.length || !Array.isArray(item.rows) || item.rows.length < 1 || item.rows.length > 50) fail('governed table');
      const rows = item.rows.map((row) => {
        if (!Array.isArray(row) || row.length !== columns.length) fail('governed table row');
        return row.map((cell) => text(cell, 'governed table cell', 1_000));
      });
      return { kind: 'governed_table', caption: text(item.caption, 'governed table caption', 240), columns, rows };
    }
    case 'notice': {
      const item = record(value, ['kind', 'notice_kind', 'title', 'text'], 'notice');
      return {
        kind: 'notice',
        notice_kind: literal(item.notice_kind, ['notice', 'limitation'], 'notice kind'),
        title: text(item.title, 'notice title', 160),
        text: text(item.text, 'notice text'),
      };
    }
    default:
      return fail('block kind');
  }
}

function freeze<T>(value: T): T {
  if (value !== null && typeof value === 'object' && !Object.isFrozen(value)) {
    Object.values(value).forEach(freeze);
    Object.freeze(value);
  }
  return value;
}

function decodeV3(value: { readonly [key: string]: JsonValue }): ConversationStreamV3Event {
  switch (value.kind) {
    case 'activity': {
      const item = record(value, ['schema_version', 'kind', 'stage'], 'activity');
      return freeze({
        schema_version: literal(item.schema_version, ['ConversationStream.v3'], 'schema'),
        kind: 'activity',
        stage: literal(item.stage, ['understanding_request', 'retrieving_governed_data', 'composing_response', 'refining_response', 'assessing_publication', 'preparing_public_response'], 'activity stage'),
      });
    }
    case 'tool': {
      const item = record(value, ['schema_version', 'kind', 'tool_name', 'status'], 'tool event');
      return freeze({
        schema_version: literal(item.schema_version, ['ConversationStream.v3'], 'schema'),
        kind: 'tool',
        tool_name: toolName(item.tool_name),
        status: literal(item.status, ['requested', 'completed'], 'tool status'),
      });
    }
    case 'block': {
      const item = record(value, ['schema_version', 'kind', 'sequence', 'block'], 'block event');
      return freeze({
        schema_version: literal(item.schema_version, ['ConversationStream.v3'], 'schema'),
        kind: 'block',
        sequence: integer(item.sequence, 'block sequence'),
        block: block(item.block),
      });
    }
    case 'done': {
      const item = terminalRecord(value, ['schema_version', 'kind'], 'done');
      return freeze({
        schema_version: literal(item.schema_version, ['ConversationStream.v3'], 'schema'),
        kind: 'done',
        ...supportReference(item),
      });
    }
    case 'refused': {
      const item = terminalRecord(value, ['schema_version', 'kind', 'reason_code', 'reason'], 'refusal');
      return freeze({
        schema_version: literal(item.schema_version, ['ConversationStream.v3'], 'schema'),
        kind: 'refused',
        reason_code: literal(item.reason_code, ['output_intervened', 'dependency_unavailable', 'publication_refused', 'unsupported_operation'], 'refusal code'),
        reason: text(item.reason, 'refusal reason'),
        ...supportReference(item),
      });
    }
    default:
      return fail('event kind');
  }
}

function decodeV4(value: { readonly [key: string]: JsonValue }): ConversationStreamV4Event {
  switch (value.kind) {
    case 'activity': {
      const item = record(value, ['schema_version', 'kind', 'stage'], 'activity');
      return freeze({
        schema_version: 'ConversationStream.v4',
        kind: 'activity',
        stage: literal(item.stage, ['understanding_request', 'retrieving_governed_data'], 'activity stage'),
      });
    }
    case 'tool': {
      const item = record(value, ['schema_version', 'kind', 'tool_name', 'status'], 'tool event');
      return freeze({
        schema_version: 'ConversationStream.v4',
        kind: 'tool',
        tool_name: toolName(item.tool_name),
        status: literal(item.status, ['requested', 'completed'], 'tool status'),
      });
    }
    case 'text': {
      const item = record(value, ['schema_version', 'kind', 'sequence', 'text'], 'text event');
      return freeze({
        schema_version: 'ConversationStream.v4',
        kind: 'text',
        sequence: integer(item.sequence, 'text sequence'),
        text: delta(item.text),
      });
    }
    case 'context': {
      const item = record(
        value,
        ['schema_version', 'kind', 'used', 'reason', 'display_text', 'display_text_refresh_failed'],
        'context event',
      );
      if (item.used === true) {
        if (item.reason !== null || item.display_text !== null || item.display_text_refresh_failed !== null) fail('context event');
        return freeze({ schema_version: 'ConversationStream.v4', kind: 'context', used: true });
      }
      if (item.used !== false) fail('context event');
      return freeze({
        schema_version: 'ConversationStream.v4',
        kind: 'context',
        used: false,
        reason: literal(item.reason, ['expired', 'invalid'], 'context reason'),
        display_text: characters(item.display_text, 'context line', 400),
        display_text_refresh_failed: characters(item.display_text_refresh_failed, 'context line', 400),
      });
    }
    case 'done': {
      const item = terminalRecord(value, ['schema_version', 'kind'], 'done');
      return freeze({
        schema_version: 'ConversationStream.v4',
        kind: 'done',
        ...supportReference(item),
      });
    }
    case 'refused': {
      const item = terminalRecord(value, ['schema_version', 'kind', 'reason_code', 'reason'], 'refusal');
      return freeze({
        schema_version: 'ConversationStream.v4',
        kind: 'refused',
        reason_code: literal(item.reason_code, ['dependency_unavailable', 'no_answer'], 'refusal code'),
        reason: characters(item.reason, 'refusal reason', 4_000),
        ...supportReference(item),
      });
    }
    default:
      return fail('event kind');
  }
}

/** Decode one post-open event of either supported stream version, dispatched on `schema_version`. */
export function decodeConversationStreamEvent(serialized: string): ConversationStreamEvent {
  if (new TextEncoder().encode(serialized).byteLength > MAX_EVENT_BYTES) fail('event size');
  const value = parse(serialized);
  if (value === null || typeof value !== 'object' || Array.isArray(value) || typeof value.kind !== 'string') fail('event');
  return literal(value.schema_version ?? null, ['ConversationStream.v3', 'ConversationStream.v4'], 'schema') === 'ConversationStream.v4'
    ? decodeV4(value)
    : decodeV3(value);
}

export function decodeConversationTerminal(serialized: string): ConversationTerminal {
  const item = terminalRecord(parse(serialized), ['schema_version', 'kind', 'reason_code', 'reason'], 'terminal');
  return freeze({
    schema_version: literal(item.schema_version, ['ConversationTerminal.v2'], 'terminal schema'),
    kind: literal(item.kind, ['refused'], 'terminal kind'),
    reason_code: literal(item.reason_code, ['input_intervened', 'context_custody_refused', 'invalid_request'], 'terminal reason code'),
    reason: text(item.reason, 'terminal reason'),
    ...supportReference(item),
  });
}
