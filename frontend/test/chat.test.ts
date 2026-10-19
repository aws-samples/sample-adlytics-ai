// SPDX-License-Identifier: MIT-0
import assert from 'node:assert/strict';
import { test, vi } from 'vitest';

const callApi = vi.hoisted(() => vi.fn());
vi.mock('@/lib/session', () => ({ callApi }));

import { ChatStreamError, ChatStreamParser, streamChat } from '@/lib/chat';

const encoder = new TextEncoder();

function streamResponse(events: readonly object[], chunkAt?: number): Response {
  const text = events.map((event) => `data:${JSON.stringify(event)}\n\n`).join('');
  const chunks = chunkAt === undefined ? [text] : [text.slice(0, chunkAt), text.slice(chunkAt)];
  return new Response(new ReadableStream<Uint8Array>({
    start(controller) {
      chunks.filter(Boolean).forEach((chunk) => controller.enqueue(encoder.encode(chunk)));
      controller.close();
    },
  }), { headers: { 'content-type': 'text/event-stream; charset=utf-8' } });
}

const preface = { schema_version: 'ConversationStream.v3', kind: 'block', sequence: 1, block: { kind: 'preface', text: 'I will look into that.' } } as const;
const activity = { schema_version: 'ConversationStream.v3', kind: 'activity', stage: 'retrieving_governed_data' } as const;
const composing = { schema_version: 'ConversationStream.v3', kind: 'activity', stage: 'composing_response' } as const;
const toolCall = { schema_version: 'ConversationStream.v3', kind: 'tool', tool_name: 'catalog_describe_metric', status: 'requested' } as const;
const paragraph = { schema_version: 'ConversationStream.v3', kind: 'block', sequence: 1, block: { kind: 'paragraph', text: 'The governed result is available.' } } as const;
const done = { schema_version: 'ConversationStream.v3', kind: 'done' } as const;
const refusal = { schema_version: 'ConversationStream.v3', kind: 'refused', reason_code: 'dependency_unavailable', reason: 'The assistant is temporarily unavailable.' } as const;

test('parses split activity-first V3 frames and publishes immutable blocks progressively', async () => {
  callApi.mockResolvedValueOnce(streamResponse([activity, toolCall, composing, paragraph, done], 37));
  const received: string[] = [];
  const result = await streamChat('Question', 'conversation-test', {
    onActivity: (event) => received.push(`activity:${event.stage}`),
    onTool: (event) => received.push(`tool:${event.tool_name}:${event.status}`),
    onBlock: (event) => {
      received.push(`block:${event.sequence}`);
      assert.equal(Object.isFrozen(event), true);
      assert.equal(Object.isFrozen(event.block), true);
    },
    onText: () => assert.fail('a V3 stream carries no text events'),
  });

  assert.deepEqual(received, ['activity:retrieving_governed_data', 'tool:catalog_describe_metric:requested', 'activity:composing_response', 'block:1']);
  assert.deepEqual(result, { kind: 'done' });
});

test('accepts a legacy preface but permits a refusal without any public block', async () => {
  callApi.mockResolvedValueOnce(streamResponse([preface, { ...paragraph, sequence: 2 }, done]));
  const legacy = await streamChat('Question', 'conversation-test', { onActivity: vi.fn(), onTool: vi.fn(), onBlock: vi.fn(), onText: vi.fn() });
  assert.deepEqual(legacy, { kind: 'done' });

  callApi.mockResolvedValueOnce(streamResponse([activity, refusal]));
  const refused = await streamChat('Question', 'conversation-test', { onActivity: vi.fn(), onTool: vi.fn(), onBlock: vi.fn(), onText: vi.fn() });
  assert.deepEqual(refused, { kind: 'refused', reason: refusal.reason });
});

test('rejects activity after content, done without content, and unrecognized SSE payloads', async () => {
  callApi.mockResolvedValueOnce(streamResponse([paragraph, activity, done]));
  await assert.rejects(
    streamChat('Question', 'conversation-test', { onActivity: vi.fn(), onTool: vi.fn(), onBlock: vi.fn(), onText: vi.fn() }),
    (error: unknown) => error instanceof ChatStreamError && error.reason === 'invalid_event_order',
  );

  callApi.mockResolvedValueOnce(streamResponse([activity, done]));
  await assert.rejects(
    streamChat('Question', 'conversation-test', { onActivity: vi.fn(), onTool: vi.fn(), onBlock: vi.fn(), onText: vi.fn() }),
    (error: unknown) => error instanceof ChatStreamError && error.reason === 'invalid_event_order',
  );

  const unknown = new ChatStreamParser();
  assert.throws(
    () => unknown.push('data:{"schema_version":"ConversationStream.v3","kind":"answer"}\n\n'),
    (error: unknown) => error instanceof ChatStreamError && error.reason === 'invalid_stream_contract',
  );
});

test('enforces contiguous immutable public block sequences', async () => {
  callApi.mockResolvedValueOnce(streamResponse([
    activity,
    { ...paragraph, sequence: 3 },
    done,
  ]));
  await assert.rejects(
    streamChat('Question', 'conversation-test', { onActivity: vi.fn(), onTool: vi.fn(), onBlock: vi.fn(), onText: vi.fn() }),
    (error: unknown) => error instanceof ChatStreamError && error.reason === 'invalid_event_order',
  );
});

test('rejects a named tool event after public content began', async () => {
  callApi.mockResolvedValueOnce(streamResponse([paragraph, toolCall, done]));
  await assert.rejects(
    streamChat('Question', 'conversation-test', { onActivity: vi.fn(), onTool: vi.fn(), onBlock: vi.fn(), onText: vi.fn() }),
    (error: unknown) => error instanceof ChatStreamError && error.reason === 'invalid_event_order',
  );
});
test('returns the support reference from the done, stream refusal, and pre-open refusal terminals (ADR-081)', async () => {
  // pragma: allowlist nextline secret (A-ENTROPY-IDENTIFIER-001)
  const reference = '01JABCDEFGHJKMNPQRSTVWXYZ0';
  const handlers = { onActivity: vi.fn(), onTool: vi.fn(), onBlock: vi.fn(), onText: vi.fn() };
  callApi.mockResolvedValueOnce(streamResponse([paragraph, { ...done, audit_id: reference }]));
  assert.deepEqual(await streamChat('Question', 'conversation-test', handlers), { kind: 'done', reference });
  callApi.mockResolvedValueOnce(streamResponse([activity, { ...refusal, audit_id: reference }]));
  assert.deepEqual(await streamChat('Question', 'conversation-test', handlers), { kind: 'refused', reason: refusal.reason, reference });
  callApi.mockResolvedValueOnce(streamResponse([activity, { ...refusal, audit_id: null }]));
  assert.deepEqual(await streamChat('Question', 'conversation-test', handlers), { kind: 'refused', reason: refusal.reason });
  callApi.mockResolvedValueOnce(new Response(JSON.stringify({
    schema_version: 'ConversationTerminal.v2',
    kind: 'refused',
    reason_code: 'input_intervened',
    reason: 'Rephrase the question.',
    audit_id: reference,
  }), { status: 400, headers: { 'content-type': 'application/json' } }));
  assert.deepEqual(await streamChat('Question', 'conversation-test', handlers), { kind: 'refused', reason: 'Rephrase the question.', reference });
});

const V4 = 'ConversationStream.v4';
const understanding4 = { kind: 'activity', schema_version: V4, stage: 'understanding_request' } as const;
const retrieving4 = { kind: 'activity', schema_version: V4, stage: 'retrieving_governed_data' } as const;
const requested4 = { kind: 'tool', schema_version: V4, status: 'requested', tool_name: 'campaign_performance' } as const;
const completed4 = { kind: 'tool', schema_version: V4, status: 'completed', tool_name: 'campaign_performance' } as const;
const text4 = (sequence: number, text: string) => ({ kind: 'text', schema_version: V4, sequence, text }) as const;
const done4 = { audit_id: '01JABCDEFGHJKMNPQRSTVWXYZ0', kind: 'done', schema_version: V4 } as const;
const refused4 = { audit_id: null, kind: 'refused', reason: 'The data service is unavailable.', reason_code: 'dependency_unavailable', schema_version: V4 } as const;
function recordingHandlers(received: string[]) {
  return {
    onActivity: (event: { stage: string }) => received.push(`activity:${event.stage}`),
    onTool: (event: { tool_name: string; status: string }) => received.push(`tool:${event.tool_name}:${event.status}`),
    onBlock: () => assert.fail('a V4 stream carries no blocks'),
    onText: (event: { sequence: number; text: string }) => {
      assert.equal(Object.isFrozen(event), true);
      received.push(`text:${event.sequence}:${event.text}`);
    },
  };
}
const rejectsWith = (reason: string) => (error: unknown) => error instanceof ChatStreamError && error.reason === reason;

test('streams V4 text interleaved with progress and tool calls, in order, across split frames', async () => {
  callApi.mockResolvedValueOnce(streamResponse([
    understanding4,
    text4(1, 'Let me check. '),
    requested4,
    retrieving4,
    completed4,
    text4(2, 'Sales rose '),
    text4(3, '**12%**.'),
    done4,
  ], 61));
  const received: string[] = [];
  const result = await streamChat('Question', 'conversation-test', recordingHandlers(received));
  assert.deepEqual(received, [
    'activity:understanding_request',
    'text:1:Let me check. ',
    'tool:campaign_performance:requested',
    'activity:retrieving_governed_data',
    'tool:campaign_performance:completed',
    'text:2:Sales rose ',
    'text:3:**12%**.',
  ]);
  assert.deepEqual(result, { kind: 'done', reference: done4.audit_id });
});

test('keeps V4 text delivered before a refusal and returns the refusal', async () => {
  callApi.mockResolvedValueOnce(streamResponse([text4(1, 'Partial answer.'), refused4]));
  const received: string[] = [];
  assert.deepEqual(
    await streamChat('Question', 'conversation-test', recordingHandlers(received)),
    { kind: 'refused', reason: refused4.reason },
  );
  assert.deepEqual(received, ['text:1:Partial answer.']);
  callApi.mockResolvedValueOnce(streamResponse([understanding4, { ...refused4, reason_code: 'no_answer', audit_id: 'ref-1' }]));
  assert.deepEqual(
    await streamChat('Question', 'conversation-test', recordingHandlers([])),
    { kind: 'refused', reason: refused4.reason, reference: 'ref-1' },
  );
});

test('rejects a V4 sequence gap, a repeated sequence, done without text, and events after the terminal', async () => {
  const cases: readonly (readonly object[])[] = [
    [text4(1, 'a'), text4(3, 'b'), done4],
    [text4(2, 'a'), done4],
    [text4(1, 'a'), text4(1, 'a'), done4],
    [understanding4, requested4, completed4, done4],
    [text4(1, 'a'), done4, text4(2, 'b')],
    [text4(1, 'a'), refused4, done4],
  ];
  for (const events of cases) {
    const received: string[] = [];
    callApi.mockResolvedValueOnce(streamResponse(events));
    await assert.rejects(streamChat('Question', 'conversation-test', recordingHandlers(received)), rejectsWith('invalid_event_order'));
  }
});

test('bounds released V4 text at 64 KiB in total and rejects a stream that mixes V3 and V4', async () => {
  const delta = 'x'.repeat(8 * 1024);
  const full = Array.from({ length: 8 }, (_, index) => text4(index + 1, delta));
  callApi.mockResolvedValueOnce(streamResponse([...full, done4]));
  assert.deepEqual((await streamChat('Question', 'conversation-test', recordingHandlers([]))).kind, 'done');
  callApi.mockResolvedValueOnce(streamResponse([...full, text4(9, 'y'), done4]));
  await assert.rejects(streamChat('Question', 'conversation-test', recordingHandlers([])), rejectsWith('invalid_stream_contract'));
  callApi.mockResolvedValueOnce(streamResponse([activity, text4(1, 'a'), done4]));
  await assert.rejects(streamChat('Question', 'conversation-test', recordingHandlers([])), rejectsWith('invalid_stream_contract'));
  callApi.mockResolvedValueOnce(streamResponse([text4(1, 'a'), done]));
  await assert.rejects(streamChat('Question', 'conversation-test', recordingHandlers([])), rejectsWith('invalid_stream_contract'));
  callApi.mockResolvedValueOnce(streamResponse([understanding4, text4(1, 'a')]));
  await assert.rejects(streamChat('Question', 'conversation-test', recordingHandlers([])), rejectsWith('missing_terminal'));
});

const v4Text = { schema_version: 'ConversationStream.v4', kind: 'text', sequence: 1, text: 'Answer.' } as const;
const v4Done = { schema_version: 'ConversationStream.v4', kind: 'done' } as const;
const contextUnused = {
  schema_version: 'ConversationStream.v4',
  kind: 'context',
  used: false,
  reason: 'expired',
  display_text: "Answered without this page's context, because it had expired. The page context is refreshed now, so ask again to use it.",
  display_text_refresh_failed: "Answered without this page's context. Reload the page to use it.",
} as const;
const REFERENCE = 'R'.repeat(43);
const handlers = () => ({ onActivity: vi.fn(), onTool: vi.fn(), onBlock: vi.fn(), onText: vi.fn(), onContext: vi.fn() });

test('sends ConversationRequest.v3 with the page reference only when one is given (ADR-120)', async () => {
  callApi.mockResolvedValueOnce(streamResponse([v4Text, v4Done]));
  await streamChat('Question', 'conversation-test', handlers(), undefined, REFERENCE);
  callApi.mockResolvedValueOnce(streamResponse([v4Text, v4Done]));
  await streamChat('Question', 'conversation-test', handlers());

  assert.deepEqual(callApi.mock.calls.at(-2)?.[2], {
    schema_version: 'ConversationRequest.v3',
    question: 'Question',
    session_id: 'conversation-test',
    page_context_reference: REFERENCE,
  });
  assert.deepEqual(callApi.mock.calls.at(-1)?.[2], {
    schema_version: 'ConversationRequest.v3',
    question: 'Question',
    session_id: 'conversation-test',
  });
  await assert.rejects(streamChat('Question', 'conversation-test', handlers(), undefined, 'short'));
});

test('delivers the context status once, before text, and only for a turn that sent a reference', async () => {
  const received = handlers();
  callApi.mockResolvedValueOnce(streamResponse([contextUnused, v4Text, v4Done]));
  await streamChat('Question', 'conversation-test', received, undefined, REFERENCE);
  assert.deepEqual(received.onContext.mock.calls, [[contextUnused]]);

  for (const events of [[v4Text, contextUnused, v4Done], [contextUnused, contextUnused, v4Text, v4Done]]) {
    callApi.mockResolvedValueOnce(streamResponse(events));
    await assert.rejects(streamChat('Question', 'conversation-test', handlers(), undefined, REFERENCE), ChatStreamError);
  }
  callApi.mockResolvedValueOnce(streamResponse([contextUnused, v4Text, v4Done]));
  await assert.rejects(streamChat('Question', 'conversation-test', handlers()), ChatStreamError);
  callApi.mockResolvedValueOnce(streamResponse([{ ...contextUnused, used: true }, v4Text, v4Done]));
  await assert.rejects(streamChat('Question', 'conversation-test', handlers(), undefined, REFERENCE), ChatStreamError);
});
