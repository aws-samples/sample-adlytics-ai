// SPDX-License-Identifier: MIT-0
import assert from 'node:assert/strict';
import { afterEach, test, vi } from 'vitest';

async function sessionModule() {
  return import('../lib/conversation-session');
}

afterEach(() => {
  localStorage.clear();
  vi.resetModules();
});

test('restores one opaque conversation selector after a browser reload without storing transcript content', async () => {
  const firstModule = await sessionModule();
  const first = firstModule.conversationSessionId();

  assert.match(first, /^conversation-[0-9a-f-]{36}$/);
  assert.equal(localStorage.getItem(firstModule.CONVERSATION_SESSION_STORAGE_KEY), first);

  vi.resetModules();
  const reloadedModule = await sessionModule();
  assert.equal(reloadedModule.conversationSessionId(), first);
  assert.deepEqual([...Array(localStorage.length).keys()].map((index) => localStorage.key(index)), [
    reloadedModule.CONVERSATION_SESSION_STORAGE_KEY,
  ]);
  assert.equal([...Array(localStorage.length).keys()].some((index) =>
    (localStorage.getItem(localStorage.key(index) ?? '') ?? '').includes('fictional message'),
  ), false);
});

test('replaces a malformed persisted selector and clears the selector on sign-out', async () => {
  const sessionApi = await sessionModule();
  localStorage.setItem(sessionApi.CONVERSATION_SESSION_STORAGE_KEY, 'malformed');

  const session = sessionApi.conversationSessionId();
  assert.match(session, /^conversation-[0-9a-f-]{36}$/);
  assert.notEqual(session, 'malformed');

  sessionApi.clearConversationSessionId();
  assert.equal(localStorage.getItem(sessionApi.CONVERSATION_SESSION_STORAGE_KEY), null);
});
