// SPDX-License-Identifier: MIT-0
import assert from 'node:assert/strict';
import { act, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { test, vi } from 'vitest';

const streamChat = vi.hoisted(() => vi.fn());

vi.mock('@/lib/chat', () => ({
  ChatStreamError: class ChatStreamError extends Error {},
  streamChat,
}));

import {
  ChatPanel,
  friendlyToolProgress,
  nextProgressFeed,
  renderedAssistantWidth,
  seedAssistantQuestion,
  toggleAssistant,
} from '@/components/ChatPanel';
import type { AssistantPage } from '@/components/AssistantPageContext';

const NO_PAGE: AssistantPage = { kind: 'none' };

const preface = {
  schema_version: 'ConversationStream.v3',
  kind: 'block',
  sequence: 1,
  block: Object.freeze({ kind: 'preface' as const, text: 'I will use governed information.' }),
};
const paragraph = {
  schema_version: 'ConversationStream.v3',
  kind: 'block',
  sequence: 2,
  block: Object.freeze({ kind: 'paragraph' as const, text: 'The governed result is available.' }),
};

test('appends validated public blocks as they arrive and exposes friendly activity without an Answer heading', async () => {
  let releaseSecondBlock: (() => void) | undefined;
  streamChat.mockImplementationOnce(async (
    _question: string,
    _sessionId: string,
    handlers: { onActivity: (event: { stage: 'retrieving_governed_data' }) => void; onTool: (event: { tool_name: string; status: 'requested' | 'completed' }) => void; onBlock: (event: typeof preface | typeof paragraph) => void },
  ) => {
    handlers.onActivity({ stage: 'retrieving_governed_data' });
    handlers.onBlock(preface);
    await new Promise<void>((resolve) => { releaseSecondBlock = resolve; });
    handlers.onBlock(paragraph);
    return { kind: 'done' };
  });

  const user = userEvent.setup();
  render(<ChatPanel narrow={false} page={NO_PAGE} sessionId="conversation-test" toggleId="assistant-toggle" />);
  await user.type(screen.getByRole('textbox', { name: 'Ask the workspace' }), 'What changed?');
  await user.click(screen.getByRole('button', { name: 'Submit' }));

  assert.ok(await screen.findByText('I will use governed information.'));
  assert.equal(screen.queryByText('The governed result is available.'), null);
  assert.match(screen.getByRole('status').textContent ?? '', /Checking governed information/);
  assert.equal(screen.queryByRole('heading', { name: 'Answer' }), null);
  assert.ok(screen.getByRole('article', { name: 'Assistant response' }));

  if (releaseSecondBlock === undefined) throw new Error('stream did not await the second block');
  releaseSecondBlock();
  assert.ok(await screen.findByText('The governed result is available.'));
});

test('a completed tool call replaces its own requested line and keeps the feed ordered', () => {
  let feed = nextProgressFeed([], 'Understanding the question');
  feed = nextProgressFeed(feed, friendlyToolProgress({ tool_name: 'catalog_describe_metric', status: 'requested' }));
  feed = nextProgressFeed(
    feed,
    friendlyToolProgress({ tool_name: 'catalog_describe_metric', status: 'completed' }),
    friendlyToolProgress({ tool_name: 'catalog_describe_metric', status: 'requested' }),
  );
  assert.deepEqual(feed, ['Understanding the question', 'catalog_describe_metric returned']);
});


test('page Ask seeding dispatches only the supplied scope text', () => {
  let received: unknown;
  const listener = (event: Event) => { received = (event as CustomEvent<unknown>).detail; };
  window.addEventListener('adlytics:assistant-seed', listener);
  const callsBeforeSeed = streamChat.mock.calls.length;
  seedAssistantQuestion('Ask about Ad spend for the visible campaign.');
  window.removeEventListener('adlytics:assistant-seed', listener);
  assert.equal((received as { readonly question: string }).question, 'Ask about Ad spend for the visible campaign.');
  assert.equal(streamChat.mock.calls.length, callsBeforeSeed);
});
test('shows the support reference as plain text once a turn ends (ADR-081)', async () => {
  streamChat.mockImplementationOnce(async (
    _question: string,
    _sessionId: string,
    handlers: { onBlock: (event: typeof paragraph) => void },
  ) => {
    handlers.onBlock(paragraph);
    // pragma: allowlist nextline secret (A-ENTROPY-IDENTIFIER-001)
    return { kind: 'done', reference: '01JABCDEFGHJKMNPQRSTVWXYZ0' };
  });
  // pragma: allowlist nextline secret (A-ENTROPY-IDENTIFIER-001)
  streamChat.mockImplementationOnce(async () => ({ kind: 'refused', reason: 'Rephrase the question.', reference: '01JZYXWVTSRQPNMKJHGFEDCBA9' }));
  streamChat.mockImplementationOnce(async () => ({ kind: 'refused', reason: 'The assistant is temporarily unavailable.' }));
  const user = userEvent.setup();
  render(<ChatPanel narrow={false} page={NO_PAGE} sessionId="conversation-test" toggleId="assistant-toggle" />);
  const box = screen.getByRole('textbox', { name: 'Ask the workspace' });
  await user.type(box, 'What changed?');
  await user.click(screen.getByRole('button', { name: 'Submit' }));
  assert.ok(await screen.findByText('Reference: 01JABCDEFGHJKMNPQRSTVWXYZ0'));
  assert.ok(await screen.findByText('The governed result is available.'));
  await user.type(box, 'And the week before?');
  await user.click(screen.getByRole('button', { name: 'Submit' }));
  assert.ok(await screen.findByText('Reference: 01JZYXWVTSRQPNMKJHGFEDCBA9'));
  await user.type(box, 'And the month?');
  await user.click(screen.getByRole('button', { name: 'Submit' }));
  assert.ok(await screen.findByText('The assistant is temporarily unavailable.'));
  assert.equal(screen.getAllByText(/^Reference: /).length, 2);
});

type TextHandlers = {
  onActivity: (event: { stage: 'understanding_request' | 'retrieving_governed_data' }) => void;
  onTool: (event: { tool_name: string; status: 'requested' | 'completed' }) => void;
  onText: (event: { sequence: number; text: string }) => void;
};
const nextFrame = () => new Promise<void>((resolve) => { requestAnimationFrame(() => resolve()); });

test('renders a streamed V4 Markdown answer incrementally and safely, then the reference (ADR-083)', async () => {
  const deltas = [
    '## Summary\n\nSales rose. See [the report](https://example.com/report) ',
    'or [this](javascript:alert(1)).\n\n![chart](https://example.com/chart.png)\n\n',
    '- First finding\n- Second <b>bold</b> finding\n\n',
    '| Campaign | Sales |\n| --- | ---: |\n| Spring | 10 |\n| Summer | 12 |\n',
  ];
  const gates = deltas.map(() => {
    let release!: () => void;
    const gate = new Promise<void>((resolve) => { release = resolve; });
    return { gate, release };
  });
  streamChat.mockImplementationOnce(async (_question: string, _sessionId: string, handlers: TextHandlers) => {
    handlers.onActivity({ stage: 'understanding_request' });
    for (const [index, text] of deltas.entries()) {
      await gates[index]!.gate;
      if (index === 1) handlers.onTool({ tool_name: 'campaign_performance', status: 'requested' });
      handlers.onText({ sequence: index + 1, text });
    }
    await nextFrame();
    // pragma: allowlist nextline secret (A-ENTROPY-IDENTIFIER-001)
    return { kind: 'done', reference: '01JABCDEFGHJKMNPQRSTVWXYZ0' };
  });
  const user = userEvent.setup();
  const view = render(<ChatPanel narrow={false} page={NO_PAGE} sessionId="conversation-test" toggleId="assistant-toggle" />);
  await user.type(screen.getByRole('textbox', { name: 'Ask the workspace' }), 'What changed?');
  await user.click(screen.getByRole('button', { name: 'Submit' }));

  gates[0]!.release();
  const answer = await screen.findByRole('article', { name: 'Assistant response' });
  assert.ok(await within(answer).findByRole('heading', { name: 'Summary' }));
  assert.equal(within(answer).queryByRole('table'), null);
  assert.match(screen.getByRole('status', { name: 'Assistant progress' }).textContent ?? '', /Understanding the question/);
  assert.equal(screen.queryByText(/^Reference: /), null);

  gates[1]!.release();
  assert.ok(await within(answer).findByText('chart'));
  const link = within(answer).getByRole('link', { name: /the report/ });
  assert.equal(link.getAttribute('href'), 'https://example.com/report');
  assert.equal(link.getAttribute('target'), '_blank');
  assert.equal(link.getAttribute('rel'), 'noopener noreferrer nofollow');
  assert.match(screen.getByRole('status', { name: 'Assistant progress' }).textContent ?? '', /Calling campaign_performance/);
  assert.equal(within(answer).queryByRole('link', { name: /this/ }), null);
  assert.ok(within(answer).getByText('this'));
  assert.equal(view.container.querySelector('img'), null);

  gates[2]!.release();
  assert.deepEqual(
    (await within(answer).findAllByRole('listitem')).map((item) => item.textContent),
    ['First finding', 'Second <b>bold</b> finding'],
  );
  assert.equal(answer.querySelector('b, script, iframe'), null);

  gates[3]!.release();
  const region = await within(answer).findByRole('region', { name: 'Table: Campaign, Sales' });
  assert.equal(region.getAttribute('tabindex'), '0');
  const table = within(region).getByRole('table');
  assert.deepEqual(within(table).getAllByRole('columnheader').map((cell) => [cell.tagName, cell.textContent, cell.getAttribute('scope')]), [
    ['TH', 'Campaign', 'col'],
    ['TH', 'Sales', 'col'],
  ]);
  assert.deepEqual(within(table).getAllByRole('cell').map((cell) => cell.textContent), ['Spring', '10', 'Summer', '12']);

  assert.ok(await screen.findByText('Reference: 01JABCDEFGHJKMNPQRSTVWXYZ0'));
  assert.equal(screen.getByText('Reference: 01JABCDEFGHJKMNPQRSTVWXYZ0').className, 'msg-reference');
  assert.equal(screen.queryByRole('status', { name: 'Assistant progress' }), null);
  assert.ok(screen.getByText('Assistant response ready.'));
  assert.equal(view.container.querySelectorAll('[aria-live]').length, 1);
});

test('renders the final V4 text exactly when the turn ends, even inside one frame', async () => {
  streamChat.mockImplementationOnce(async (_question: string, _sessionId: string, handlers: TextHandlers) => {
    handlers.onText({ sequence: 1, text: 'Sales ' });
    handlers.onText({ sequence: 2, text: 'rose **12%**.' });
    return { kind: 'done' };
  });
  const user = userEvent.setup();
  render(<ChatPanel narrow={false} page={NO_PAGE} sessionId="conversation-test" toggleId="assistant-toggle" />);
  await user.type(screen.getByRole('textbox', { name: 'Ask the workspace' }), 'What changed?');
  await user.click(screen.getByRole('button', { name: 'Submit' }));
  const answer = await screen.findByRole('article', { name: 'Assistant response' });
  assert.equal(answer.textContent, 'Sales rose 12%.');
  assert.equal(answer.querySelector('strong')?.textContent, '12%');
  assert.equal(screen.queryByText(/^Reference: /), null);
});

test('keeps streamed V4 text on screen when the turn is refused after it, with the reference', async () => {
  streamChat.mockImplementationOnce(async (_question: string, _sessionId: string, handlers: TextHandlers) => {
    handlers.onText({ sequence: 1, text: 'Partial answer so far.' });
    await nextFrame();
    // pragma: allowlist nextline secret (A-ENTROPY-IDENTIFIER-001)
    return { kind: 'refused', reason: 'The data service is unavailable.', reference: '01JZYXWVTSRQPNMKJHGFEDCBA9' };
  });
  const user = userEvent.setup();
  const view = render(<ChatPanel narrow={false} page={NO_PAGE} sessionId="conversation-test" toggleId="assistant-toggle" />);
  await user.type(screen.getByRole('textbox', { name: 'Ask the workspace' }), 'What changed?');
  await user.click(screen.getByRole('button', { name: 'Submit' }));
  const refusal = await screen.findByText('The data service is unavailable.');
  assert.equal(refusal.closest('.msg')?.getAttribute('data-assistant-tone'), 'refusal');
  assert.ok(within(refusal.closest('.msg') as HTMLElement).getByText('Reference: 01JZYXWVTSRQPNMKJHGFEDCBA9'));
  const answer = screen.getByRole('article', { name: 'Assistant response' });
  assert.equal(answer.textContent, 'Partial answer so far.');
  assert.equal(answer.closest('.msg')?.querySelector('.msg-reference'), null);
  assert.ok(view.container.querySelector('.msg.agent + .msg.agent'));
  assert.ok(screen.getByText('Assistant response refused.'));
});
test('keeps streamed V4 text on screen when the stream breaks after it (ADR-083)', async () => {
  streamChat.mockImplementationOnce(async (_question: string, _sessionId: string, handlers: TextHandlers) => {
    handlers.onText({ sequence: 1, text: 'Partial answer before the break.' });
    await nextFrame();
    throw new Error('stream reset');
  });
  const user = userEvent.setup();
  render(<ChatPanel narrow={false} page={NO_PAGE} sessionId="conversation-test" toggleId="assistant-toggle" />);
  await user.type(screen.getByRole('textbox', { name: 'Ask the workspace' }), 'What changed?');
  await user.click(screen.getByRole('button', { name: 'Submit' }));
  const failure = await screen.findByText('The assistant could not complete this response. Submit the request again.');
  assert.equal(failure.closest('.msg')?.getAttribute('data-assistant-tone'), 'failure');
  assert.equal(screen.getByRole('article', { name: 'Assistant response' }).textContent, 'Partial answer before the break.');
});

function setViewport(width: number): void {
  Object.defineProperty(window, 'innerWidth', { configurable: true, value: width });
}

test('the docked width is clamped so the main column keeps 448 px, and the clamp never stores', () => {
  // 881 px with the rail: 881 − 88 − 448 = 345, so the default 360 renders at 345.
  assert.equal(renderedAssistantWidth(360, 881, 88, false), 345);
  assert.equal(renderedAssistantWidth(360, 1280, 88, false), 360);
  assert.equal(renderedAssistantWidth(560, 1440, 232, false), 560);
  assert.equal(renderedAssistantWidth(560, 1280, 88, false), 560);
  assert.equal(renderedAssistantWidth(480, 1008, 88, false), 472);
  // Below 881 px the panel is fixed at 320 px.
  assert.equal(renderedAssistantWidth(480, 768, 0, true), 320);

  setViewport(1024);
  localStorage.setItem('adlytics.chat-width', '560');
  render(<ChatPanel narrow={false} page={NO_PAGE} sessionId="conversation-test" toggleId="assistant-toggle" />);
  assert.equal(screen.getByRole('separator', { name: 'Resize assistant' }).getAttribute('aria-valuenow'), '488');
  assert.equal(localStorage.getItem('adlytics.chat-width'), '560');
});

test('Narrower and Wider step 40 px, disable at their bound and hand focus to the other', async () => {
  setViewport(1024);
  const user = userEvent.setup();
  render(<ChatPanel narrow={false} page={NO_PAGE} sessionId="conversation-test" toggleId="assistant-toggle" />);
  const group = screen.getByRole('group', { name: 'Assistant width' });
  const wider = within(group).getByRole('button', { name: 'Wider' });
  const separator = screen.getByRole('separator', { name: 'Resize assistant' });
  await user.click(wider);
  assert.equal(separator.getAttribute('aria-valuenow'), '400');
  assert.equal(localStorage.getItem('adlytics.chat-width'), '400');
  await user.click(wider);
  await user.click(wider);
  await user.click(wider);
  assert.equal(separator.getAttribute('aria-valuenow'), '488');
  assert.equal((wider as HTMLButtonElement).disabled, true);
  assert.equal(document.activeElement, within(group).getByRole('button', { name: 'Narrower' }));
});

test('below 881 px the docked panel has no separator and no width buttons', () => {
  setViewport(768);
  render(<ChatPanel narrow page={NO_PAGE} sessionId="conversation-test" toggleId="assistant-toggle" />);
  assert.ok(screen.getByRole('complementary', { name: 'Assistant' }));
  assert.equal(screen.queryByRole('separator'), null);
  assert.equal(screen.queryByRole('group', { name: 'Assistant width' }), null);
});

test('the not-found page shows no chip, and a route with no page context says so (ia §8.8)', () => {
  setViewport(1440);
  const view = render(<ChatPanel narrow={false} page={NO_PAGE} sessionId="conversation-test" toggleId="assistant-toggle" />);
  assert.equal(view.container.querySelector('.ctx')?.textContent, 'No page context on this page');
  view.rerender(<ChatPanel narrow={false} page={null} sessionId="conversation-test" toggleId="assistant-toggle" />);
  assert.equal(view.container.querySelector('.ctx'), null);
  assert.ok(screen.getByRole('textbox', { name: 'Ask the workspace' }));
});

test('the assistant is docked and open by default, its thread is a labelled log, and closing leaves no launcher', async () => {
  setViewport(1440);
  render(<>
    <button type="button" id="assistant-toggle">Assistant</button>
    <ChatPanel narrow={false} page={NO_PAGE} sessionId="conversation-test" toggleId="assistant-toggle" />
  </>);
  assert.ok(screen.getByRole('complementary', { name: 'Assistant' }));
  assert.ok(screen.getByRole('log', { name: 'Conversation' }));
  screen.getByRole('textbox', { name: 'Ask the workspace' }).focus();
  await act(async () => { toggleAssistant(); });
  assert.equal(screen.queryByRole('complementary', { name: 'Assistant' }), null);
  assert.equal(screen.getAllByRole('button').length, 1);
  assert.equal(localStorage.getItem('adlytics.chat-open'), 'false');
});
