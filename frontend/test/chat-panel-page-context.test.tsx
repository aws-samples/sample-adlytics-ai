// SPDX-License-Identifier: MIT-0
import assert from 'node:assert/strict';
import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, test, vi } from 'vitest';
const streamChat = vi.hoisted(() => vi.fn());
vi.mock('@/lib/chat', () => ({
  ChatStreamError: class ChatStreamError extends Error {},
  streamChat,
}));
import { ChatPanel } from '@/components/ChatPanel';
import type { AssistantPage } from '@/components/AssistantPageContext';
import type { PageContextV1 } from '@/lib/page-context';

const LABEL = 'Campaigns · Amazon DSP · Sep 9\u201315, 2026';
const STARTERS: readonly [string, string] = [
  'Which campaign changed spend most between Sep 2\u20138, 2026 and Sep 9\u201315, 2026?',
  'How is spend split across campaigns in Sep 9\u201315, 2026?',
];
const LIVE: PageContextV1 = {
  expires_at: '2099-09-16T12:00:00+00:00',
  label: LABEL,
  reference: 'L'.repeat(43),
  starter_questions: STARTERS,
};
const FRESH: PageContextV1 = { ...LIVE, reference: 'F'.repeat(43) };
const EXPIRED: PageContextV1 = { ...LIVE, expires_at: '2020-01-01T00:00:00+00:00', reference: 'E'.repeat(43) };
const REFRESHED_LINE = "Answered without this page's context, because it had expired. The page context is refreshed now, so ask again to use it.";
const FAILED_LINE = "Answered without this page's context. Reload the page to use it.";

afterEach(() => {
  cleanup();
  streamChat.mockReset();
  localStorage.clear();
});

function page(context: PageContextV1, refresh: () => Promise<PageContextV1 | null> = async () => FRESH): AssistantPage {
  return { kind: 'page', context, refresh };
}

function panel(value: AssistantPage | null) {
  return <ChatPanel narrow={false} page={value} sessionId="conversation-test" toggleId="assistant-toggle" />;
}

function answered() {
  streamChat.mockImplementation(async (_q: string, _s: string, handlers: { onText: (event: object) => void }) => {
    handlers.onText({ schema_version: 'ConversationStream.v4', kind: 'text', sequence: 1, text: 'Spend rose most on two campaigns.' });
    return { kind: 'done' };
  });
}

async function ask(text: string) {
  const user = userEvent.setup();
  await user.type(screen.getByRole('textbox', { name: 'Ask the workspace' }), text);
  await user.click(screen.getByRole('button', { name: 'Submit' }));
}

test('a current page shows the server label and its starter questions (ADR-120)', () => {
  const view = render(panel(page(LIVE)));

  const chip = view.container.querySelector('.ctx');
  assert.equal(chip?.textContent, `Using this page: ${LABEL}`);
  assert.equal(chip?.getAttribute('title'), `Using this page: ${LABEL}`);
  const group = screen.getByRole('group', { name: 'Starter questions' });
  assert.deepEqual([...group.querySelectorAll('button')].map((button) => button.textContent), STARTERS);
  assert.ok(screen.getByText("Ask about what's on this page. Answers name their own window."));
});

test('a starter question fills the composer, moves focus there and sends nothing', async () => {
  render(panel(page(LIVE)));
  const user = userEvent.setup();

  await user.click(screen.getByRole('button', { name: STARTERS[1] }));

  const composer = screen.getByRole('textbox', { name: 'Ask the workspace' });
  await waitFor(() => assert.ok(document.activeElement === composer, 'focus is on the composer'));
  assert.equal((composer as HTMLTextAreaElement).value, STARTERS[1]);
  assert.equal(streamChat.mock.calls.length, 0);
});

test('loading, absence and the not-found page each show their own chip and no starters', () => {
  const view = render(panel({ kind: 'loading' }));
  assert.equal(view.container.querySelector('.ctx')?.textContent, 'Loading page context…');
  view.rerender(panel({ kind: 'none' }));
  assert.equal(view.container.querySelector('.ctx')?.textContent, 'No page context on this page');
  assert.equal(screen.queryByRole('group', { name: 'Starter questions' }), null);
  assert.ok(screen.getByText('Ask a question. Answers name their own window.'));
  view.rerender(panel(null));
  assert.equal(view.container.querySelector('.ctx'), null);
  assert.equal(screen.queryByRole('group', { name: 'Starter questions' }), null);
});

test('a turn sends the current page reference', async () => {
  answered();
  render(panel(page(LIVE)));

  await ask('Which campaigns drove the change?');

  await waitFor(() => assert.equal(streamChat.mock.calls.length, 1));
  assert.equal(streamChat.mock.calls[0]?.[4], LIVE.reference);
});

test('an expired page refreshes before the turn sends, and a failed refresh sends none', async () => {
  answered();
  const refresh = vi.fn(async (): Promise<PageContextV1 | null> => FRESH);
  const view = render(panel(page(EXPIRED, refresh)));
  await waitFor(() => assert.match(view.container.querySelector('.ctx')?.textContent ?? '', /Page context expired ·/));
  assert.ok(screen.getByRole('button', { name: 'Refresh' }));

  await ask('First question');
  await waitFor(() => assert.equal(streamChat.mock.calls.length, 1));
  assert.equal(refresh.mock.calls.length, 1);
  assert.equal(streamChat.mock.calls[0]?.[4], FRESH.reference);

  const failing = vi.fn(async (): Promise<PageContextV1 | null> => null);
  view.rerender(panel(page({ ...EXPIRED, reference: 'G'.repeat(43) }, failing)));
  await ask('Second question');
  await waitFor(() => assert.equal(streamChat.mock.calls.length, 2));
  assert.equal(streamChat.mock.calls[1]?.[4], undefined);
  await waitFor(() => assert.equal(view.container.querySelector('.ctx')?.textContent, 'Page context expired · Reload the page to use it'));
});

/** The chip's persistent live region: it stays mounted across every chip state. */
function refreshNotice(container: HTMLElement): Element | null {
  return container.querySelector('.ctx-region + p[aria-live="polite"]');
}

test('the chip Refresh button shows the refreshing state, then the fresh page, keeping focus on the chip', async () => {
  let release: (value: PageContextV1 | null) => void = () => undefined;
  const refresh = () => new Promise<PageContextV1 | null>((resolve) => { release = resolve; });
  const view = render(panel(page(EXPIRED, refresh)));
  const user = userEvent.setup();
  const region = view.container.querySelector('.ctx-region');
  const notice = refreshNotice(view.container);
  assert.ok(notice !== null);
  assert.equal(notice.textContent, '');

  const button = await screen.findByRole('button', { name: 'Refresh' });
  button.focus();
  await user.keyboard('{Enter}');

  assert.equal(view.container.querySelector('.ctx')?.textContent, 'Refreshing page context…');
  assert.ok(document.activeElement === region, 'focus moves to the chip region, not the body');
  assert.equal(notice.textContent, 'Refreshing page context…');
  await act(async () => release(FRESH));
  // The page publishes its fresh context, and the chip names it again.
  view.rerender(panel(page(FRESH)));
  assert.equal(view.container.querySelector('.ctx')?.textContent, `Using this page: ${LABEL}`);
  assert.ok(document.activeElement === region, 'focus stays on the chip region');
  assert.ok(refreshNotice(view.container) === notice, 'the live region is the same node');
  assert.equal(notice.textContent, `Using this page: ${LABEL}`);
});

test('a failed chip Refresh keeps focus on the chip and announces the failure', async () => {
  const view = render(panel(page(EXPIRED, async () => null)));
  const user = userEvent.setup();
  const region = view.container.querySelector('.ctx-region');

  const button = await screen.findByRole('button', { name: 'Refresh' });
  button.focus();
  await user.keyboard('{Enter}');

  await waitFor(() => assert.equal(view.container.querySelector('.ctx')?.textContent, 'Page context expired · Reload the page to use it'));
  assert.ok(document.activeElement === region, 'focus stays on the chip region');
  assert.equal(refreshNotice(view.container)?.textContent, 'Page context expired · Reload the page to use it');
  await user.tab();
  assert.ok(document.activeElement === screen.getByRole('log', { name: 'Conversation' }), 'Tab continues after the chip');
});

test('an unused context shows the server line that matches how the refresh went', async () => {
  const contextEvent = {
    schema_version: 'ConversationStream.v4',
    kind: 'context',
    used: false,
    reason: 'expired',
    display_text: REFRESHED_LINE,
    display_text_refresh_failed: FAILED_LINE,
  };
  streamChat.mockImplementation(async (_q: string, _s: string, handlers: { onContext: (event: object) => void; onText: (event: object) => void }) => {
    handlers.onContext(contextEvent);
    handlers.onText({ schema_version: 'ConversationStream.v4', kind: 'text', sequence: 1, text: 'Spend rose most on two campaigns.' });
    return { kind: 'done' };
  });
  const view = render(panel(page(LIVE, async () => FRESH)));

  await ask('Which campaigns drove the change?');
  assert.ok(await screen.findByText(REFRESHED_LINE));

  view.rerender(panel(page(FRESH, async () => null)));
  await ask('Again?');
  assert.ok(await screen.findByText(FAILED_LINE));
  assert.equal(view.container.querySelectorAll('.ctx-status').length, 2);
});
