// SPDX-License-Identifier: MIT-0
import assert from 'node:assert/strict';
import { test } from 'vitest';

import {
  decodeConversationStreamEvent,
  decodeConversationTerminal,
} from '@/lib/assistant-response';

function event(value: object) {
  return decodeConversationStreamEvent(JSON.stringify(value));
}

test('strictly decodes and deeply freezes only V3 public activity, block, and terminal events', () => {
  const activity = event({ schema_version: 'ConversationStream.v3', kind: 'activity', stage: 'retrieving_governed_data' });
  const preface = event({ schema_version: 'ConversationStream.v3', kind: 'block', sequence: 1, block: { kind: 'preface', text: 'I will use governed information.' } });
  const list = event({ schema_version: 'ConversationStream.v3', kind: 'block', sequence: 2, block: { kind: 'grouped_list', heading: 'Findings', items: ['First', 'Second'] } });
  const table = event({ schema_version: 'ConversationStream.v3', kind: 'block', sequence: 3, block: { kind: 'governed_table', caption: 'Results', columns: ['Campaign', 'Sales'], rows: [['A', '10']] } });
  const notice = event({ schema_version: 'ConversationStream.v3', kind: 'block', sequence: 4, block: { kind: 'notice', notice_kind: 'limitation', title: 'Coverage', text: 'One source is partial.' } });
  const done = event({ schema_version: 'ConversationStream.v3', kind: 'done' });

  assert.equal(activity.kind, 'activity');
  assert.equal(preface.kind, 'block');
  assert.equal(list.kind, 'block');
  assert.equal(table.kind, 'block');
  assert.equal(notice.kind, 'block');
  assert.equal(done.kind, 'done');
  assert.equal(Object.isFrozen(table), true);
  if (table.kind !== 'block' || table.block.kind !== 'governed_table') throw new Error('expected table block');
  assert.equal(Object.isFrozen(table.block), true);
  assert.equal(Object.isFrozen(table.block.columns), true);
  assert.equal(Object.isFrozen(table.block.rows), true);
  assert.equal(Object.isFrozen(table.block.rows[0]), true);
});

test('rejects legacy, unknown, duplicate, unsafe, and structurally invalid public events', () => {
  const rejected = [
    { schema_version: 'ConversationStream.v2', kind: 'progress', stage: 'planning' },
    { schema_version: 'ConversationStream.v3', kind: 'response', response: {} },
    { schema_version: 'ConversationStream.v3', kind: 'activity', stage: 'planning' },
    { schema_version: 'ConversationStream.v3', kind: 'done', extra: true },
    { schema_version: 'ConversationStream.v3', kind: 'block', sequence: 1, block: { kind: 'governed_table', caption: 'T', columns: ['A'], rows: [['1']], extra: true } },
  ];
  for (const value of rejected) {
    assert.throws(() => event(value));
  }
  assert.throws(() => decodeConversationStreamEvent('{"schema_version":"ConversationStream.v3","kind":"done","kind":"done"}'));
  assert.throws(() => decodeConversationStreamEvent('{"schema_version":"ConversationStream.v3","kind":"block","sequence":1.0,"block":{"kind":"preface","text":"Safe"}}'));
  assert.throws(() => decodeConversationStreamEvent('{"schema_version":"ConversationStream.v3","kind":"block","sequence":1e0,"block":{"kind":"preface","text":"Safe"}}'));
});

test('accepts released text verbatim, markup and links included, and renders none of it (ADR-082)', () => {
  // The server releases any bounded text; the page renders it as a text node, so a campaign named
  // like a URL or a sentence quoting a tag must decode unchanged rather than fail the stream.
  for (const text of ['www.brand.com Launch', 'Q4 <Holiday> push', 'See https://example.com', 'Use `ROAS` here', '[a](b)']) {
    const decoded = event({ schema_version: 'ConversationStream.v3', kind: 'block', sequence: 1, block: { kind: 'paragraph', text } });
    assert.equal(decoded.kind, 'block');
    assert.equal(decoded.kind === 'block' && decoded.block.kind === 'paragraph' ? decoded.block.text : null, text);
  }
});
test('retains the separate strict pre-open refusal boundary', () => {
  assert.deepEqual(
    decodeConversationTerminal('{"schema_version":"ConversationTerminal.v2","kind":"refused","reason_code":"invalid_request","reason":"The request is invalid."}'),
    { schema_version: 'ConversationTerminal.v2', kind: 'refused', reason_code: 'invalid_request', reason: 'The request is invalid.' },
  );
  assert.throws(() => decodeConversationTerminal('{"schema_version":"ConversationTerminal.v2","kind":"refused","reason_code":"invalid_request","reason":"Safe","extra":true}'));
});

test('carries the support reference on every terminal, tolerating an absent or null value (ADR-081)', () => {
  // pragma: allowlist nextline secret (A-ENTROPY-IDENTIFIER-001)
  const reference = '01JABCDEFGHJKMNPQRSTVWXYZ0';
  assert.deepEqual(event({ schema_version: 'ConversationStream.v3', kind: 'done', audit_id: reference }), {
    schema_version: 'ConversationStream.v3',
    kind: 'done',
    audit_id: reference,
  });
  assert.deepEqual(event({ schema_version: 'ConversationStream.v3', kind: 'done', audit_id: null }), {
    schema_version: 'ConversationStream.v3',
    kind: 'done',
  });
  const refused = event({
    schema_version: 'ConversationStream.v3',
    kind: 'refused',
    reason_code: 'output_intervened',
    reason: 'Rephrase the question.',
    audit_id: reference,
  });
  assert.equal(refused.kind === 'refused' ? refused.audit_id : undefined, reference);
  const terminal = decodeConversationTerminal(JSON.stringify({
    schema_version: 'ConversationTerminal.v2',
    kind: 'refused',
    reason_code: 'input_intervened',
    reason: 'Rephrase the question.',
    audit_id: reference,
  }));
  assert.equal(terminal.audit_id, reference);
  assert.equal('audit_id' in decodeConversationTerminal(JSON.stringify({
    schema_version: 'ConversationTerminal.v2',
    kind: 'refused',
    reason_code: 'input_intervened',
    reason: 'Rephrase the question.',
    audit_id: null,
  })), false);
  for (const invalid of ['', ' ', 'x'.repeat(129), 7, true, ['a'], { id: 'a' }]) {
    assert.throws(() => event({ schema_version: 'ConversationStream.v3', kind: 'done', audit_id: invalid }));
  }
  assert.throws(() => event({ schema_version: 'ConversationStream.v3', kind: 'done', audit_id: reference, extra: true }));
});

const V4 = 'ConversationStream.v4';

test('strictly decodes and freezes every V4 event variant (ADR-083)', () => {
  const activity = event({ schema_version: V4, kind: 'activity', stage: 'understanding_request' });
  const tool = event({ schema_version: V4, kind: 'tool', tool_name: 'catalog_describe_metric', status: 'completed' });
  const text = event({ schema_version: V4, kind: 'text', sequence: 1, text: '| A | B |\n' });
  const done = event({ audit_id: null, kind: 'done', schema_version: V4 });
  const refused = event({ audit_id: null, kind: 'refused', reason: 'No answer was produced.', reason_code: 'no_answer', schema_version: V4 });
  assert.deepEqual(activity, { schema_version: V4, kind: 'activity', stage: 'understanding_request' });
  assert.deepEqual(tool, { schema_version: V4, kind: 'tool', tool_name: 'catalog_describe_metric', status: 'completed' });
  assert.deepEqual(text, { schema_version: V4, kind: 'text', sequence: 1, text: '| A | B |\n' });
  assert.deepEqual(done, { schema_version: V4, kind: 'done' });
  assert.deepEqual(refused, { schema_version: V4, kind: 'refused', reason_code: 'no_answer', reason: 'No answer was produced.' });
  for (const decoded of [activity, tool, text, done, refused]) assert.equal(Object.isFrozen(decoded), true);
  // Canonical sorted-key wire text decodes the same as any key order.
  assert.deepEqual(
    decodeConversationStreamEvent('{"kind":"text","schema_version":"ConversationStream.v4","sequence":2,"text":"Sales rose."}'),
    { schema_version: V4, kind: 'text', sequence: 2, text: 'Sales rose.' },
  );
});

test('accepts V4 deltas verbatim, whitespace-only and markup included, up to 8 KiB of UTF-8', () => {
  for (const value of [' ', '\n\n', '<b>raw</b>', '![x](https://example.com/x.png)', 'é'.repeat(4096)]) {
    const decoded = event({ schema_version: V4, kind: 'text', sequence: 1, text: value });
    assert.equal(decoded.kind === 'text' ? decoded.text : null, value);
  }
  for (const value of ['', 'é'.repeat(4097), 'x'.repeat(8 * 1024 + 1), 7, null, ['a']]) {
    assert.throws(() => event({ schema_version: V4, kind: 'text', sequence: 1, text: value }));
  }
});

test('rejects V4 extra or missing keys, V3-only variants, unknown enums, and out-of-bounds values', () => {
  const rejected = [
    { schema_version: V4, kind: 'text', sequence: 1, text: 'a', extra: true },
    { schema_version: V4, kind: 'text', text: 'a' },
    { schema_version: V4, kind: 'text', sequence: 0, text: 'a' },
    { schema_version: V4, kind: 'text', sequence: '1', text: 'a' },
    { schema_version: V4, kind: 'activity', stage: 'composing_response' },
    { schema_version: V4, kind: 'activity', stage: 'understanding_request', extra: 1 },
    { schema_version: V4, kind: 'tool', tool_name: 'Catalog', status: 'requested' },
    { schema_version: V4, kind: 'tool', tool_name: 'catalog', status: 'failed' },
    { schema_version: V4, kind: 'block', sequence: 1, block: { kind: 'paragraph', text: 'a' } },
    { schema_version: V4, kind: 'done', extra: true },
    { schema_version: V4, kind: 'refused', reason_code: 'output_intervened', reason: 'a' },
    { schema_version: V4, kind: 'refused', reason_code: 'no_answer', reason: ' ' },
    { schema_version: V4, kind: 'refused', reason_code: 'no_answer', reason: 'x'.repeat(4_001) },
    { schema_version: V4, kind: 'refused', reason_code: 'no_answer' },
    { schema_version: 'ConversationStream.v5', kind: 'done' },
    { kind: 'done' },
  ];
  for (const value of rejected) {
    assert.throws(() => event(value), Error, JSON.stringify(value).slice(0, 120));
  }
  // The refusal reason is bounded in code points, like the server contract, not UTF-8 bytes.
  const wide = event({ schema_version: V4, kind: 'refused', reason_code: 'dependency_unavailable', reason: 'é'.repeat(4_000) });
  assert.equal(wide.kind, 'refused');
  assert.throws(() => decodeConversationStreamEvent('{"schema_version":"ConversationStream.v4","kind":"text","sequence":1,"text":"a","text":"b"}'));
});

test('carries the V4 support reference when present and tolerates an absent or null value', () => {
  // pragma: allowlist nextline secret (A-ENTROPY-IDENTIFIER-001)
  const reference = '01JABCDEFGHJKMNPQRSTVWXYZ0';
  assert.deepEqual(event({ schema_version: V4, kind: 'done', audit_id: reference }), { schema_version: V4, kind: 'done', audit_id: reference });
  assert.deepEqual(event({ schema_version: V4, kind: 'done' }), { schema_version: V4, kind: 'done' });
  const refused = event({ schema_version: V4, kind: 'refused', reason_code: 'dependency_unavailable', reason: 'Try again.', audit_id: reference });
  assert.equal(refused.kind === 'refused' ? refused.audit_id : undefined, reference);
  for (const invalid of ['', ' ', 'x'.repeat(129), 7, true, ['a'], { id: 'a' }]) {
    assert.throws(() => event({ schema_version: V4, kind: 'done', audit_id: invalid }));
    assert.throws(() => event({ schema_version: V4, kind: 'refused', reason_code: 'no_answer', reason: 'a', audit_id: invalid }));
  }
});
