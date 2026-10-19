// SPDX-License-Identifier: MIT-0
import {
  decodeConversationStreamEvent,
  decodeConversationTerminal,
  type ConversationStreamEvent,
  type ConversationStreamV3Event,
  type ConversationStreamV4Event,
} from './assistant-response';
import { CHAT_PATH } from './config';
import { type JsonValue, callApi } from './session';

const SSE_CONTENT_TYPE = 'text/event-stream';
const MAX_PUBLIC_RESPONSE_BYTES = 64 * 1024;
const SSE_FRAME_ENVELOPE_HEADROOM_BYTES = 8 * 1024;
const MAX_BLOCKS = 24;
export const MAX_PENDING_SSE_FRAME_BYTES = MAX_PUBLIC_RESPONSE_BYTES + SSE_FRAME_ENVELOPE_HEADROOM_BYTES;

export type ChatActivityEvent = Extract<ConversationStreamEvent, { readonly kind: 'activity' }>;
export type ChatToolEvent = Extract<ConversationStreamEvent, { readonly kind: 'tool' }>;
export type ChatBlockEvent = Extract<ConversationStreamEvent, { readonly kind: 'block' }>;
/** One V4 Markdown answer delta (ADR-083); the caller appends `text` to the answer so far. */
export type ChatTextEvent = Extract<ConversationStreamEvent, { readonly kind: 'text' }>;
/** Whether the turn used its page context (ADR-120); sent only when a reference was. */
export type ChatContextEvent = Extract<ConversationStreamEvent, { readonly kind: 'context' }>;
/** `reference` is the turn's support reference (ADR-081 §2), absent when none was recorded. */
export type ChatResult =
  | { readonly kind: 'done'; readonly reference?: string }
  | { readonly kind: 'refused'; readonly reason: string; readonly reference?: string };
function withReference(auditId: string | undefined): { readonly reference?: string } {
  return auditId === undefined ? {} : { reference: auditId };
}
export interface ChatStreamHandlers {
  readonly onActivity: (event: ChatActivityEvent) => void;
  readonly onTool: (event: ChatToolEvent) => void;
  /** A V3 public block. */
  readonly onBlock: (event: ChatBlockEvent) => void;
  /** A V4 answer text delta, delivered in sequence order. */
  readonly onText: (event: ChatTextEvent) => void;
  /** The turn's page-context status, once, before any text (ADR-120 decision 5). */
  readonly onContext?: (event: ChatContextEvent) => void;
}

const PAGE_CONTEXT_REFERENCE = /^[A-Za-z0-9_-]{43}$/;

export const CHAT_STREAM_FAILURE_REASONS = [
  'unsupported_media_type',
  'missing_body',
  'invalid_utf8',
  'invalid_sse_frame',
  'frame_limit_exceeded',
  'unterminated_frame',
  'invalid_stream_contract',
  'invalid_event_order',
  'missing_terminal',
] as const;
export type ChatStreamFailureReason = (typeof CHAT_STREAM_FAILURE_REASONS)[number];

export class ChatStreamError extends Error {
  constructor(readonly reason: ChatStreamFailureReason) {
    super('The response stream was not valid.');
    this.name = 'ChatStreamError';
  }
}

export class ChatRequestError extends Error {
  constructor() {
    super('The generative AI assistant is unavailable.');
    this.name = 'ChatRequestError';
  }
}

function parseBlock(block: string): ConversationStreamEvent | null {
  const data: string[] = [];
  for (const line of block.split(/\r\n|\r|\n/)) {
    if (line.startsWith(':')) continue;
    const separator = line.indexOf(':');
    const field = separator === -1 ? line : line.slice(0, separator);
    if (field !== 'data') throw new ChatStreamError('invalid_sse_frame');
    const rawValue = separator === -1 ? '' : line.slice(separator + 1);
    data.push(rawValue.startsWith(' ') ? rawValue.slice(1) : rawValue);
  }
  if (data.length === 0) return null;
  try {
    return decodeConversationStreamEvent(data.join('\n'));
  } catch {
    throw new ChatStreamError('invalid_stream_contract');
  }
}

/** Incremental parser: network chunks are unrelated to SSE event boundaries. */
export class ChatStreamParser {
  private pending = '';
  private pendingBytes = 0;
  private readonly encoder = new TextEncoder();

  private drain(final: boolean): readonly ConversationStreamEvent[] {
    const events: ConversationStreamEvent[] = [];
    const lineEnding = final
      ? String.raw`(?:\r\n|\r(?!\n)|(?<!\r)\n)`
      : String.raw`(?:\r\n|\r(?!\n)(?=[\s\S])|(?<!\r)\n)`;
    const delimiter = new RegExp(`${lineEnding}${lineEnding}`);
    let boundary = delimiter.exec(this.pending);
    while (boundary !== null) {
      const block = this.pending.slice(0, boundary.index);
      this.pending = this.pending.slice(boundary.index + boundary[0].length);
      this.pendingBytes = this.encoder.encode(this.pending).byteLength;
      const event = parseBlock(block);
      if (event !== null) events.push(event);
      boundary = delimiter.exec(this.pending);
    }
    return events;
  }

  private fittingEnd(chunk: string, start: number, available: number): number {
    let low = start;
    let high = chunk.length;
    while (low < high) {
      const candidate = Math.ceil((low + high) / 2);
      if (this.encoder.encode(chunk.slice(start, candidate)).byteLength <= available) low = candidate;
      else high = candidate - 1;
    }
    return low;
  }

  push(chunk: string): readonly ConversationStreamEvent[] {
    const events: ConversationStreamEvent[] = [];
    let offset = 0;
    while (offset < chunk.length) {
      const available = MAX_PENDING_SSE_FRAME_BYTES - this.pendingBytes;
      const end = this.fittingEnd(chunk, offset, available);
      if (end === offset) {
        this.pending = '';
        this.pendingBytes = 0;
        throw new ChatStreamError('frame_limit_exceeded');
      }
      const piece = chunk.slice(offset, end);
      this.pending += piece;
      this.pendingBytes += this.encoder.encode(piece).byteLength;
      offset = end;
      events.push(...this.drain(false));
    }
    return events;
  }

  finish(): readonly ConversationStreamEvent[] {
    const events = this.drain(true);
    if (!this.pending.trim()) {
      this.pending = '';
      this.pendingBytes = 0;
      return events;
    }
    this.pending = '';
    this.pendingBytes = 0;
    throw new ChatStreamError('unterminated_frame');
  }
}

/**
 * `ConversationRequest.v3` (ADR-120 decision 4), the only version the server accepts: the question,
 * the session and, optionally, the page's `artifact_revision` as an
 * opaque reference the server redeems. The browser sends no page metrics, filters or labels.
 */
export function chatRequestBody(question: string, sessionId: string, pageContextReference?: string): JsonValue {
  if (!question.trim() || !sessionId.trim()) throw new ChatRequestError();
  if (pageContextReference !== undefined && !PAGE_CONTEXT_REFERENCE.test(pageContextReference)) throw new ChatRequestError();
  return {
    schema_version: 'ConversationRequest.v3',
    question,
    session_id: sessionId,
    ...(pageContextReference === undefined ? {} : { page_context_reference: pageContextReference }),
  };
}

function mediaType(response: Response): string | null {
  return response.headers.get('content-type')?.split(';', 1)[0]?.trim().toLowerCase() ?? null;
}

function publicBlockBytes(event: ChatBlockEvent): number {
  return new TextEncoder().encode(JSON.stringify(event.block)).byteLength;
}

export async function streamChat(
  question: string,
  sessionId: string,
  handlers: ChatStreamHandlers,
  signal?: AbortSignal,
  pageContextReference?: string,
): Promise<ChatResult> {
  const response = await callApi(CHAT_PATH, 'POST', chatRequestBody(question, sessionId, pageContextReference), signal);
  if (!response.ok) {
    if (mediaType(response) !== 'application/json') throw new ChatRequestError();
    try {
      const terminal = decodeConversationTerminal(await response.text());
      return { kind: 'refused', reason: terminal.reason, ...withReference(terminal.audit_id) };
    } catch {
      throw new ChatRequestError();
    }
  }
  if (mediaType(response) !== SSE_CONTENT_TYPE) throw new ChatStreamError('unsupported_media_type');
  if (response.body === null) throw new ChatStreamError('missing_body');

  const reader = response.body.getReader();
  let readerCancelled = false;
  const cancelReader = async (): Promise<void> => {
    if (readerCancelled) return;
    readerCancelled = true;
    await reader.cancel().catch(() => undefined);
  };
  const abortReader = () => { void cancelReader(); };
  signal?.addEventListener('abort', abortReader, { once: true });
  const decoder = new TextDecoder('utf-8', { fatal: true });
  const decodeBytes = (value?: Uint8Array, stream = false): string => {
    try {
      return decoder.decode(value, { stream });
    } catch {
      throw new ChatStreamError('invalid_utf8');
    }
  };
  const parser = new ChatStreamParser();
  const activityIndexes: Readonly<Record<ChatActivityEvent['stage'], number>> = {
    understanding_request: 0,
    retrieving_governed_data: 1,
    composing_response: 2,
    refining_response: 3,
    assessing_publication: 4,
    preparing_public_response: 5,
  };
  let result: ChatResult | null = null;
  let blockCount = 0;
  let contentBlockCount = 0;
  let nextSequence = 1;
  let lastActivityIndex = -1;
  let encodedBlockBytes = 0;

  let nextTextSequence = 1;
  let releasedTextBytes = 0;
  let contextSeen = false;
  let version: ConversationStreamEvent['schema_version'] | null = null;
  const encoder = new TextEncoder();

  const acceptV3 = (event: ConversationStreamV3Event): void => {
    switch (event.kind) {
      case 'activity': {
        if (contentBlockCount > 0 || activityIndexes[event.stage] <= lastActivityIndex) {
          throw new ChatStreamError('invalid_event_order');
        }
        lastActivityIndex = activityIndexes[event.stage];
        handlers.onActivity(event);
        break;
      }
      case 'tool': {
        if (contentBlockCount > 0) throw new ChatStreamError('invalid_event_order');
        handlers.onTool(event);
        break;
      }
      case 'block': {
        if (event.sequence !== nextSequence || blockCount >= MAX_BLOCKS) throw new ChatStreamError('invalid_event_order');
        if (blockCount > 0 && event.block.kind === 'preface') {
          throw new ChatStreamError('invalid_event_order');
        }
        encodedBlockBytes += publicBlockBytes(event);
        if (encodedBlockBytes > MAX_PUBLIC_RESPONSE_BYTES) throw new ChatStreamError('invalid_stream_contract');
        blockCount += 1;
        if (event.block.kind !== 'preface') contentBlockCount += 1;
        nextSequence += 1;
        handlers.onBlock(event);
        break;
      }
      case 'done':
        if (contentBlockCount === 0) throw new ChatStreamError('invalid_event_order');
        result = { kind: 'done', ...withReference(event.audit_id) };
        break;
      case 'refused':
        result = { kind: 'refused', reason: event.reason, ...withReference(event.audit_id) };
        break;
    }
  };

  // V4 (ADR-083): progress and text interleave freely; text is contiguous from 1 and bounded in
  // total; `done` requires released text; a refusal may follow text, which stays on screen.
  const acceptV4 = (event: ConversationStreamV4Event): void => {
    switch (event.kind) {
      case 'activity':
        handlers.onActivity(event);
        break;
      case 'tool':
        handlers.onTool(event);
        break;
      case 'context':
        // Once, before any text, and only for a turn that sent a reference.
        if (contextSeen || nextTextSequence !== 1 || pageContextReference === undefined) {
          throw new ChatStreamError('invalid_event_order');
        }
        contextSeen = true;
        handlers.onContext?.(event);
        break;
      case 'text': {
        if (event.sequence !== nextTextSequence) throw new ChatStreamError('invalid_event_order');
        releasedTextBytes += encoder.encode(event.text).byteLength;
        if (releasedTextBytes > MAX_PUBLIC_RESPONSE_BYTES) throw new ChatStreamError('invalid_stream_contract');
        nextTextSequence += 1;
        handlers.onText(event);
        break;
      }
      case 'done':
        if (nextTextSequence === 1) throw new ChatStreamError('invalid_event_order');
        result = { kind: 'done', ...withReference(event.audit_id) };
        break;
      case 'refused':
        result = { kind: 'refused', reason: event.reason, ...withReference(event.audit_id) };
        break;
    }
  };

  const accept = (events: readonly ConversationStreamEvent[]): void => {
    for (const event of events) {
      if (result !== null) throw new ChatStreamError('invalid_event_order');
      // One stream speaks one protocol version; a mixed stream is not a legal V3 or V4 stream.
      version ??= event.schema_version;
      if (event.schema_version !== version) throw new ChatStreamError('invalid_stream_contract');
      if (event.schema_version === 'ConversationStream.v4') acceptV4(event);
      else acceptV3(event);
    }
  };

  try {
    if (signal?.aborted) throw signal.reason;
    for (;;) {
      const read = await reader.read();
      if (read.done) break;
      accept(parser.push(decodeBytes(read.value, true)));
    }
    accept(parser.push(decodeBytes()));
    accept(parser.finish());
    if (result === null) throw new ChatStreamError('missing_terminal');
    return result;
  } catch (error: unknown) {
    await cancelReader();
    throw error;
  } finally {
    signal?.removeEventListener('abort', abortReader);
  }
}
