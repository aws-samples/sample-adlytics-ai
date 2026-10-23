// SPDX-License-Identifier: MIT-0
'use client';

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';

import { AssistantMarkdown } from '@/components/assistant/AssistantMarkdown';
import { PublicBlockStream } from '@/components/assistant/PublicBlockStream';
import type { PublicConversationBlock } from '@/lib/assistant-response';
import { pageContextExpired, type PageContextV1 } from '@/lib/page-context';
import {
  ChatStreamError,
  streamChat,
  type ChatActivityEvent,
  type ChatToolEvent,
} from '@/lib/chat';
import type { AssistantPage } from './AssistantPageContext';
import { BrandMark } from './BrandMark';
import { closeModalForAssistant } from './Dialog';
import { Icon } from './Icon';
import { useMediaQuery } from './useMediaQuery';

const OPEN_KEY = 'adlytics.chat-open';
const WIDTH_KEY = 'adlytics.chat-width';
const MIN = 280;
const MAX = 560;
const DEFAULT_WIDTH = 360;
/** Below 881 px the docked panel is fixed at this width, with no resize (ia §3.4). */
export const NARROW_WIDTH = 320;
/** The main column never drops below this beside the docked panel (ADR-118 decision 2). */
export const MAIN_MIN = 448;
/** One "Narrower" or "Wider" press (WCAG 2.2 SC 2.5.7). */
export const WIDTH_STEP = 40;
/** The sidebar from 1420 px; the labelled rail from 881 to 1419 px (ADR-115 decision 9). */
const WIDE_QUERY = '(min-width: 1420px)';
const SIDEBAR_WIDTH = 232;
const RAIL_WIDTH = 88;
const OPAQUE = /\b[A-Za-z0-9_-]{43}\b/g;

type Message =
  | {
      readonly kind: 'user';
      readonly text: string;
    }
  | {
      readonly kind: 'assistant-blocks';
      readonly blocks: readonly PublicConversationBlock[];
      /** The turn's support reference (ADR-081 §2), shown once the turn has ended. */
      readonly reference?: string;
      /** The governed tools the turn called, in order (OD-UX-8 trace). */
      readonly trace?: readonly string[];
    }
  | {
      /** The V4 streamed Markdown answer (ADR-083), accumulated from its text deltas. */
      readonly kind: 'assistant-markdown';
      readonly text: string;
      readonly reference?: string;
      readonly trace?: readonly string[];
    }
  | {
      readonly kind: 'assistant-text';
      readonly text: string;
      readonly tone: 'failure' | 'refusal';
      readonly reference?: string;
    };

const PROGRESS_LABELS: Readonly<Record<ChatActivityEvent['stage'], string>> = {
  understanding_request: 'Understanding the question',
  retrieving_governed_data: 'Checking governed information',
  composing_response: 'Composing the response',
  refining_response: 'Refining the response',
  assessing_publication: 'Assessing safe publication',
  preparing_public_response: 'Preparing the response',
};

export function friendlyChatProgress(stage: ChatActivityEvent['stage']): string {
  return PROGRESS_LABELS[stage];
}

/**
 * OD-UX-8 (views/chat.md fix 3): each governed tool's plain step, keyed by its declared name in
 * `contracts/generated/tools.json` (a test pins the keys); an undeclared name reads the generic step.
 */
export const TOOL_STEP_LABELS: Readonly<Record<string, string>> = {
  catalog_describe_metric: 'Looked up how a metric is defined',
  catalog_list_metrics: 'Listed the metrics this workspace defines',
  measurement_compare_windows: 'Compared a metric across two windows',
  measurement_metric_by_grain: "Read a metric's breakdown or trend",
  measurement_top_n: 'Ranked a metric',
};
export function toolStepLabel(toolName: string): string {
  return TOOL_STEP_LABELS[toolName] ?? 'Read governed data';
}

/** "How this was answered": the turn's tool steps, collapsed, each with its governed tool name. */
function AnswerTrace({ trace }: { readonly trace: readonly string[] }) {
  if (trace.length === 0) return null;
  return <details className="msg-trace">
    <summary>How this was answered</summary>
    <ol>{trace.map((name, index) => <li key={`${index}:${name}`}>{toolStepLabel(name)} <code>{name}</code></li>)}</ol>
  </details>;
}

/** The turn's support reference behind one button (views/chat.md fix 2), never printed raw. */
function CopyReference({ reference }: { readonly reference: string }) {
  const [copied, setCopied] = useState(false);
  return <p className="msg-reference">
    <button
      type="button"
      className="btn link"
      onClick={() => {
        void navigator.clipboard?.writeText(reference).then(() => setCopied(true), () => setCopied(false));
      }}
    >Copy reference</button>
    <span className="sr-only" role="status">{copied ? 'Reference copied.' : ''}</span>
  </p>;
}

/** Deliberate transparency: the declared governed tool is named to the user (ADR-065). */
export function friendlyToolProgress(event: Pick<ChatToolEvent, 'tool_name' | 'status'>): string {
  return event.status === 'requested'
    ? `Calling ${event.tool_name}`
    : `${event.tool_name} returned`;
}

/** A completed call replaces its own requested line; everything else appends. */
export function nextProgressFeed(
  feed: readonly string[],
  entry: string,
  replaces?: string,
): readonly string[] {
  const base = replaces !== undefined && feed[feed.length - 1] === replaces
    ? feed.slice(0, -1)
    : [...feed];
  return [...base, entry];
}

export const safeChatContent = (value: string) => value.replaceAll(OPAQUE, '[context omitted]');
export const nextChatOpen = (open: boolean) => !open;

export const ASSISTANT_SEED_EVENT = 'adlytics:assistant-seed';
export const ASSISTANT_TOGGLE_EVENT = 'adlytics:assistant-toggle';

/** Where the assistant sits: docked beside the page at every width, open or closed, and how wide. */
export interface AssistantPlacement {
  readonly open: boolean;
  readonly width: number;
}

/** The rendered width's bounds at 881 px and wider: the main column keeps 448 px. */
export function assistantBounds(viewport: number, navigation: number): { readonly max: number; readonly min: number } {
  return { max: Math.max(MIN, Math.min(MAX, viewport - navigation - MAIN_MIN)), min: MIN };
}

/**
 * clamp(280, stored ?? 360, viewport − nav − 448) at 881 px and wider; 320 px below. The
 * stored width is clamped for rendering only and never overwritten by the clamp.
 */
export function renderedAssistantWidth(stored: number, viewport: number, navigation: number, narrow: boolean): number {
  if (narrow) return NARROW_WIDTH;
  const { max, min } = assistantBounds(viewport, navigation);
  return Math.min(max, Math.max(min, stored));
}

function useViewportWidth(): number {
  const [width, setWidth] = useState(() => (typeof window === 'undefined' ? 1440 : window.innerWidth));
  useEffect(() => {
    const update = () => setWidth(window.innerWidth);
    window.addEventListener('resize', update);
    return () => window.removeEventListener('resize', update);
  }, []);
  return width;
}

/** Shell chrome (the top bar button) toggles the panel through this event. */
export function toggleAssistant(): void {
  window.dispatchEvent(new Event(ASSISTANT_TOGGLE_EVENT));
}

/** A seeded question and where focus returns when the assistant later closes. */
interface AssistantSeed {
  readonly question: string;
  readonly returnTarget: HTMLElement | null;
}

/**
 * "Asking from the page" (ia §8.8), one rule for every ask control: a modal it sits in closes,
 * a closed assistant opens, the question fills the composer verbatim and focus moves there.
 * Nothing sends; the user presses Ask. A closed dialog's opener is the return target.
 */
export function seedAssistantQuestion(value: string): void {
  const opener = closeModalForAssistant();
  const returnTarget = opener ?? (document.activeElement instanceof HTMLElement ? document.activeElement : null);
  window.dispatchEvent(new CustomEvent<AssistantSeed>(ASSISTANT_SEED_EVENT, { detail: { question: value, returnTarget } }));
}

export function focusChatInput(
  resolve: () => HTMLTextAreaElement | null,
  schedule: (callback: FrameRequestCallback) => number = requestAnimationFrame,
  attempts = 4,
): void {
  schedule(() => {
    const element = resolve();
    // A closed assistant's column shows once the shell has read the open placement, a commit
    // after the panel mounts, and a hidden composer can't take focus: try again next frame.
    if ((element === null || element.getClientRects().length === 0) && attempts > 1) {
      focusChatInput(resolve, schedule, attempts - 1);
      return;
    }
    element?.focus();
  });
}

function storedOpen(): boolean {
  if (typeof window === 'undefined') return true;
  try {
    return localStorage.getItem(OPEN_KEY) !== 'false';
  } catch {
    return true;
  }
}

function storedWidth(): number {
  if (typeof window === 'undefined') return DEFAULT_WIDTH;
  try {
    const value = Number(localStorage.getItem(WIDTH_KEY));
    return Number.isFinite(value) && value >= MIN && value <= MAX
      ? value
      : DEFAULT_WIDTH;
  } catch {
    return DEFAULT_WIDTH;
  }
}

function persist(key: string, value: string): void {
  try {
    localStorage.setItem(key, value);
  } catch {
    // In-memory interaction remains available when browser storage is unavailable.
  }
}

export function chatWidthForKey(
  width: number,
  key: string,
  bounds: { readonly max: number; readonly min: number } = { max: MAX, min: MIN },
): number | null {
  if (key === 'ArrowLeft') return Math.min(bounds.max, width + 16);
  if (key === 'ArrowRight') return Math.max(bounds.min, width - 16);
  if (key === 'Home') return bounds.min;
  if (key === 'End') return bounds.max;
  return null;
}

/** A refresh the chip is showing: none running, one running, or one that failed. */
type RefreshState = 'idle' | 'refreshing' | 'failed';

const REFRESHING_TEXT = 'Refreshing page context…';
const REFRESH_FAILED_TEXT = 'Page context expired · Reload the page to use it';
const usingPageText = (context: PageContextV1): string => `Using this page: ${context.label}`;

/**
 * The context chip (ia §8.8). Every word is the server's `page_context_label` or fixed product
 * copy; the chip never claims context the next turn won't send.
 */
function ContextChip({
  expired,
  onRefresh,
  page,
  refresh,
}: {
  readonly expired: boolean;
  readonly onRefresh: () => void;
  readonly page: AssistantPage;
  readonly refresh: RefreshState;
}) {
  if (page.kind === 'none') return <div className="ctx muted"><span className="ctx-text">No page context on this page</span></div>;
  if (page.kind === 'loading') return <div className="ctx muted"><span className="ctx-text">Loading page context…</span></div>;
  if (refresh === 'refreshing') {
    return <div className="ctx muted"><span className="ctx-text">{REFRESHING_TEXT}</span></div>;
  }
  if (refresh === 'failed') {
    return <div className="ctx muted"><span className="ctx-text">{REFRESH_FAILED_TEXT}</span></div>;
  }
  if (expired) {
    return (
      <div className="ctx muted">
        <span className="ctx-text">Page context expired ·</span>
        <button className="link-btn" type="button" onClick={onRefresh}>Refresh</button>
      </div>
    );
  }
  const text = usingPageText(page.context);
  return <div className="ctx" title={text}><span className="ctx-text">{text}</span></div>;
}

export function ChatPanel({
  narrow,
  onPlacementChange,
  page,
  sessionId,
  toggleId,
}: {
  /** Below 881 px: fixed width, no separator, no width buttons. */
  readonly narrow: boolean;
  readonly onPlacementChange?: (placement: AssistantPlacement) => void;
  /** What the page on screen offers the assistant; null on the not-found page (no chip). */
  readonly page: AssistantPage | null;
  readonly sessionId: string;
  /** The top-bar toggle, the only open and close control; focus returns to it on close. */
  readonly toggleId: string;
}) {
  const composerLabel = 'Ask the workspace';
  const wide = useMediaQuery(WIDE_QUERY);
  const viewport = useViewportWidth();
  const navigation = wide ? SIDEBAR_WIDTH : RAIL_WIDTH;
  const [open, setOpen] = useState(storedOpen);
  const [width, setWidth] = useState(storedWidth);
  const [messages, setMessages] = useState<Message[]>([]);
  const [question, setQuestion] = useState('');
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState<readonly string[]>([]);
  const [announcement, setAnnouncement] = useState('');
  const input = useRef<HTMLTextAreaElement>(null);
  const narrower = useRef<HTMLButtonElement>(null);
  const wider = useRef<HTMLButtonElement>(null);
  const messageList = useRef<HTMLDivElement>(null);
  const followLatest = useRef(true);
  const messagesRef = useRef<Message[]>([]);
  const widthRef = useRef(width);
  const drag = useRef<{ pointerId: number; x: number } | null>(null);
  const restoreTarget = useRef<HTMLElement | null>(null);
  const restoreAfterClose = useRef(false);
  const activeRequest = useRef<{ readonly controller: AbortController } | null>(null);
  const answerFrame = useRef<number | null>(null);
  // ADR-120: the chip's refresh state belongs to one served reference and resets with the next.
  const reference = page?.kind === 'page' ? page.context.reference : null;
  const [refresh, setRefresh] = useState<{ readonly reference: string | null; readonly state: RefreshState }>(
    { reference, state: 'idle' },
  );
  const refreshState: RefreshState = refresh.reference === reference ? refresh.state : 'idle';
  const [now, setNow] = useState(() => Date.now());
  const expiresAt = page?.kind === 'page' ? Date.parse(page.context.expires_at) : null;
  const expired = page?.kind === 'page' && pageContextExpired(page.context, now);
  useEffect(() => {
    // Flip the chip to "expired" the moment the server's expiry passes.
    if (expiresAt === null) return;
    const wait = Math.max(0, expiresAt - Date.now());
    const timer = window.setTimeout(() => setNow(Date.now()), Math.min(wait, 2_147_000_000));
    return () => window.clearTimeout(timer);
  }, [expiresAt]);
  const pageRef = useRef(page);
  useLayoutEffect(() => {
    pageRef.current = page;
  }, [page]);
  const chipRegion = useRef<HTMLDivElement>(null);
  /** The latest refresh's chip text, read out by a live region that stays mounted (ADR-120). */
  const [refreshNotice, setRefreshNotice] = useState<{ readonly reference: string | null; readonly text: string }>(
    { reference, text: '' },
  );
  /** Which user turns the server answered without context, and the line it chose for each. */
  const [contextLines, setContextLines] = useState<Readonly<Record<number, string>>>({});

  /** Run the page's refresh (decision 8); resolve to the fresh context, or null when it failed. */
  const runRefresh = useCallback(async (): Promise<PageContextV1 | null> => {
    const current = pageRef.current;
    if (current?.kind !== 'page') return null;
    const from = current.context.reference;
    setRefresh({ reference: from, state: 'refreshing' });
    setRefreshNotice({ reference: from, text: REFRESHING_TEXT });
    const fresh = await current.refresh();
    if (fresh === null) {
      setRefresh({ reference: from, state: 'failed' });
      setRefreshNotice({ reference: from, text: REFRESH_FAILED_TEXT });
      return null;
    }
    setNow(Date.now());
    setRefresh({ reference: fresh.reference, state: 'idle' });
    setRefreshNotice({ reference: fresh.reference, text: usingPageText(fresh) });
    return fresh;
  }, []);
  /** The chip's Refresh button leaves with its state, so focus moves to the chip's own region. */
  const refreshFromChip = () => {
    chipRegion.current?.focus();
    void runRefresh();
  };

  const cancelAnswerFrame = () => {
    if (answerFrame.current === null) return;
    cancelAnimationFrame(answerFrame.current);
    answerFrame.current = null;
  };

  useEffect(() => () => {
    activeRequest.current?.controller.abort();
    activeRequest.current = null;
    if (answerFrame.current !== null) cancelAnimationFrame(answerFrame.current);
    answerFrame.current = null;
  }, []);

  useEffect(() => {
    const seed = (event: Event) => {
      const detail = (event as CustomEvent<Partial<AssistantSeed> | null>).detail;
      const questionText = detail?.question;
      if (typeof questionText !== 'string' || !questionText.trim()) return;
      restoreTarget.current = detail?.returnTarget ?? null;
      setQuestion(questionText);
      setOpen(true);
      persist(OPEN_KEY, 'true');
      focusChatInput(() => input.current);
    };
    window.addEventListener(ASSISTANT_SEED_EVENT, seed);
    return () => window.removeEventListener(ASSISTANT_SEED_EVENT, seed);
  }, []);

  const replaceMessages = (next: Message[]) => {
    messagesRef.current = next;
    setMessages(next);
  };
  const bounds = assistantBounds(viewport, navigation);
  const rendered = renderedAssistantWidth(width, viewport, navigation, narrow);
  // A user's resize stores its own value, bounded by today's clamp; the clamp alone never stores.
  const changeWidth = (next: number) => {
    const bounded = Math.min(bounds.max, Math.max(bounds.min, next));
    widthRef.current = bounded;
    setWidth(bounded);
    persist(WIDTH_KEY, String(bounded));
    return bounded;
  };
  const stepWidth = (direction: -1 | 1) => {
    const next = changeWidth(rendered + direction * WIDTH_STEP);
    // The pressed button becomes disabled at its bound, so focus moves to the other one.
    if (direction === 1 && next >= bounds.max) narrower.current?.focus();
    if (direction === -1 && next <= bounds.min) wider.current?.focus();
  };
  const closeAndRestoreFocus = useCallback(() => {
    restoreAfterClose.current = true;
    setOpen(false);
    persist(OPEN_KEY, 'false');
  }, []);
  const openRef = useRef(open);
  useEffect(() => {
    openRef.current = open;
  }, [open]);
  const toggle = useCallback(() => {
    const next = nextChatOpen(openRef.current);
    if (next) {
      restoreTarget.current = document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
      setOpen(true);
      persist(OPEN_KEY, 'true');
      focusChatInput(() => input.current);
    } else {
      closeAndRestoreFocus();
    }
  }, [closeAndRestoreFocus]);

  useEffect(() => {
    window.addEventListener(ASSISTANT_TOGGLE_EVENT, toggle);
    return () => window.removeEventListener(ASSISTANT_TOGGLE_EVENT, toggle);
  }, [toggle]);

  useEffect(() => {
    onPlacementChange?.({ open, width: rendered });
  }, [onPlacementChange, open, rendered]);

  useLayoutEffect(() => {
    if (!open && restoreAfterClose.current) {
      const target = restoreTarget.current;
      (target?.isConnected ? target : document.getElementById(toggleId))?.focus();
      restoreAfterClose.current = false;
    }
  }, [open, toggleId]);

  useLayoutEffect(() => {
    if (followLatest.current && messageList.current !== null) {
      messageList.current.scrollTop = messageList.current.scrollHeight;
    }
  }, [messages, progress]);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (activeRequest.current !== null) return;
    const requestQuestion = question.trim();
    if (!requestQuestion) return;

    const request = { controller: new AbortController() };
    activeRequest.current = request;
    const renderedQuestion = safeChatContent(requestQuestion);
    const base = messagesRef.current;
    const withQuestion: Message[] = [
      ...base,
      { kind: 'user', text: renderedQuestion },
    ];
    let blocks: readonly PublicConversationBlock[] = Object.freeze([]);
    let answer = '';
    // A V3 stream yields blocks and a V4 stream yields Markdown text; one stream never both.
    // The governed tools this turn called, in order, for its collapsed trace (OD-UX-8).
    const trace: string[] = [];
    const answerMessages = (reference: { readonly reference?: string }): Message[] => {
      const traced = trace.length === 0 ? {} : { trace: [...trace] };
      if (blocks.length > 0) return [{ kind: 'assistant-blocks', blocks, ...reference, ...traced }];
      if (answer) return [{ kind: 'assistant-markdown', text: answer, ...reference, ...traced }];
      return [];
    };
    setQuestion('');
    setAnnouncement('');
    replaceMessages(withQuestion);
    setBusy(true);
    setProgress([]);
    const turnIndex = withQuestion.length - 1;
    try {
      // ADR-120 decision 8: a turn sends the page's reference, refreshing it first when its
      // expiry has passed; after a failed refresh, or with no page context, it sends none.
      const current = pageRef.current;
      let context: PageContextV1 | null = current?.kind === 'page' && refreshState !== 'failed' ? current.context : null;
      if (context !== null && pageContextExpired(context, Date.now())) context = await runRefresh();
      if (activeRequest.current !== request) return;
      const result = await streamChat(
        requestQuestion,
        sessionId,
        {
          onContext: (item) => {
            if (item.used || activeRequest.current !== request) return;
            // Refresh first, then show the server's line for how the refresh went.
            void runRefresh().then((fresh) => {
              setContextLines((lines) => ({
                ...lines,
                [turnIndex]: fresh === null ? item.display_text_refresh_failed : item.display_text,
              }));
            });
          },
          onActivity: (item) => {
            if (activeRequest.current === request) {
              setProgress((feed) => nextProgressFeed(feed, friendlyChatProgress(item.stage)));
            }
          },
          onTool: (item) => {
            if (activeRequest.current === request && item.status === 'requested') trace.push(item.tool_name);
            if (activeRequest.current === request) {
              setProgress((feed) => nextProgressFeed(
                feed,
                friendlyToolProgress(item),
                friendlyToolProgress({ tool_name: item.tool_name, status: 'requested' }),
              ));
            }
          },
          onBlock: (item) => {
            if (activeRequest.current !== request) return;
            blocks = Object.freeze([...blocks, item.block]);
            replaceMessages([
              ...withQuestion,
              { kind: 'assistant-blocks', blocks },
            ]);
          },
          onText: (item) => {
            if (activeRequest.current !== request) return;
            answer += item.text;
            // Deltas can arrive far faster than the screen refreshes: render at most once a frame.
            if (answerFrame.current !== null) return;
            answerFrame.current = requestAnimationFrame(() => {
              answerFrame.current = null;
              if (activeRequest.current === request) replaceMessages([...withQuestion, ...answerMessages({})]);
            });
          },
        },
        request.controller.signal,
        context?.reference,
      );
      if (activeRequest.current !== request) return;
      // The turn has ended: drop any pending frame and render the exact final answer now.
      cancelAnswerFrame();
      const reference = result.reference === undefined ? {} : { reference: result.reference };
      if (result.kind === 'done') {
        replaceMessages([...withQuestion, ...answerMessages(reference)]);
        setAnnouncement('Assistant response ready.');
      } else {
        const reason = safeChatContent(result.reason);
        replaceMessages([
          ...withQuestion,
          ...answerMessages({}),
          {
            kind: 'assistant-text',
            text: reason,
            tone: 'refusal',
            ...reference,
          },
        ]);
        setAnnouncement('Assistant response refused.');
      }
    } catch (error: unknown) {
      if (activeRequest.current !== request) return;
      cancelAnswerFrame();
      if (error instanceof ChatStreamError) {
        console.warn('Chat stream rejected.', { reason: error.reason });
      }
      const unavailable = 'The assistant could not complete this response. Submit the request again.';
      // Streamed text already on screen stays (ADR-083: nothing is retracted); the failure line
      // follows it. A V3 turn has no streamed text, so this is its failure line alone.
      replaceMessages([
        ...withQuestion,
        ...answerMessages({}),
        {
          kind: 'assistant-text',
          text: unavailable,
          tone: 'failure',
        },
      ]);
      setAnnouncement('Assistant response unavailable.');
    } finally {
      if (activeRequest.current === request) {
        activeRequest.current = null;
        setProgress([]);
        setBusy(false);
      }
    }
  };

  const resizeKey = (event: React.KeyboardEvent) => {
    const next = chatWidthForKey(rendered, event.key, bounds);
    if (next === null) return;
    changeWidth(next);
    event.preventDefault();
  };
  const stopDragging = (event: React.PointerEvent<HTMLDivElement>) => {
    if (drag.current?.pointerId === event.pointerId) {
      drag.current = null;
      if (event.currentTarget.hasPointerCapture(event.pointerId)) {
        event.currentTarget.releasePointerCapture(event.pointerId);
      }
    }
  };

  const grip = !narrow && (
    <div
      className="grip"
      role="separator"
      tabIndex={0}
      aria-label="Resize assistant"
      aria-orientation="vertical"
      aria-valuemin={bounds.min}
      aria-valuemax={bounds.max}
      aria-valuenow={rendered}
      onKeyDown={resizeKey}
      onPointerDown={(event) => {
        drag.current = { pointerId: event.pointerId, x: event.clientX };
        event.currentTarget.setPointerCapture(event.pointerId);
      }}
      onPointerMove={(event) => {
        if (drag.current?.pointerId !== event.pointerId) return;
        const delta = drag.current.x - event.clientX;
        drag.current = { pointerId: event.pointerId, x: event.clientX };
        changeWidth(Math.min(bounds.max, Math.max(bounds.min, widthRef.current)) + delta);
      }}
      onPointerCancel={stopDragging}
      onPointerUp={stopDragging}
    />
  );

  const content = (
    <>
      {grip}
      <div className="head">
        <h2 id="assistant-heading" className="who">
          <BrandMark className="mini" gradientId="assistant-gradient" />
          Assistant
        </h2>
        {narrow ? null : (
          <span className="controls" role="group" aria-label="Assistant width">
            <button
              ref={narrower}
              className="icon-btn"
              type="button"
              aria-label="Narrower"
              title="Narrower"
              disabled={rendered <= bounds.min}
              onClick={() => stepWidth(-1)}
            ><Icon name="narrower" small /></button>
            <button
              ref={wider}
              className="icon-btn"
              type="button"
              aria-label="Wider"
              title="Wider"
              disabled={rendered >= bounds.max}
              onClick={() => stepWidth(1)}
            ><Icon name="wider" small /></button>
          </span>
        )}
      </div>
      {page === null ? null : (
        <>
          <div className="ctx-region" ref={chipRegion} tabIndex={-1}>
            <ContextChip expired={expired} onRefresh={refreshFromChip} page={page} refresh={refreshState} />
          </div>
          {/* Only a served page can refresh; across its chip states this region stays mounted. */}
          {page.kind === 'page' ? <p className="sr-only" aria-live="polite">{refreshNotice.reference === reference ? refreshNotice.text : ''}</p> : null}
        </>
      )}
      <div
        ref={messageList}
        className="thread"
        id="chat-panel-content"
        role="log"
        aria-label="Conversation"
        tabIndex={0}
        onScroll={(event) => {
          const node = event.currentTarget;
          followLatest.current = node.scrollHeight - node.scrollTop - node.clientHeight < 48;
        }}
      >
        {messages.length === 0 ? (
          <div className="empty">
            <p className="empty-line">
              {page !== null && page.kind !== 'none'
                ? "Ask about what's on this page. Answers name their own window."
                : 'Ask a question. Answers name their own window.'}
            </p>
            {page?.kind === 'page' && page.context.starter_questions.length > 0 ? (
              // Server-written questions (ADR-120 decision 3): a press fills the composer and
              // moves focus there; nothing sends until the user submits.
              <div className="starters" role="group" aria-label="Starter questions">
                {page.context.starter_questions.map((starter) => (
                  <button key={starter} type="button" onClick={() => seedAssistantQuestion(starter)}>{starter}</button>
                ))}
              </div>
            ) : null}
          </div>
        ) : (
          messages.map((message, index) => (
            message.kind === 'user' ? (
              <div className="msg user" key={index}>
                <span className="sr-only">You:</span>
                {message.text}
              </div>
            ) : (
              <div
                className="msg agent"
                key={index}
                {...(message.kind === 'assistant-text' ? { 'data-assistant-tone': message.tone } : {})}
              >
                <span className="sr-only">Assistant:</span>
                {contextLines[index - 1] === undefined ? null : (
                  <p className="ctx-status">{contextLines[index - 1]}</p>
                )}
                {message.kind === 'assistant-blocks'
                  ? <PublicBlockStream blocks={message.blocks} />
                  : message.kind === 'assistant-markdown'
                    ? <AssistantMarkdown text={message.text} />
                    : message.text}
                {message.kind === 'assistant-text' || message.trace === undefined ? null : <AnswerTrace trace={message.trace} />}
                {message.reference === undefined ? null : <CopyReference reference={message.reference} />}
              </div>
            )
          ))
        )}
        {progress.length > 0 && (
          <ol aria-label="Assistant progress" className="progress" role="status">
            {progress.map((entry, index) => (
              <li data-current={index === progress.length - 1} key={index}>
                {index === progress.length - 1 ? <span aria-hidden="true" className="spin" /> : <span aria-hidden="true">✓</span>}
                {entry}
              </li>
            ))}
          </ol>
        )}
      </div>
      {announcement && (
        <p className="sr-only" role="status" aria-live="polite">
          {announcement}
        </p>
      )}
      <form className="input" onSubmit={submit}>
        <label className="sr-only" htmlFor="chat-question">
          {composerLabel}
        </label>
        <textarea
          ref={input}
          className="box"
          id="chat-question"
          value={question}
          onChange={(event) => setQuestion(event.target.value)}
          disabled={busy}
          placeholder="Ask about this page, the portfolio, or any metric…"
          rows={2}
        />
        <div className="send">
          <span aria-hidden="true" />
          <button className="btn primary" type="submit" disabled={busy || !question.trim()}>
            {busy ? 'Working' : 'Submit'}
          </button>
        </div>
      </form>
    </>
  );
  // Docked at every width (ADR-118 decision 2): no launcher, no overlay, no focus trap. Closed,
  // the panel isn't rendered and the top-bar toggle is the only way back.
  if (!open) return null;
  return (
    <aside id="assistant" className="assistant" aria-labelledby="assistant-heading">
      {content}
    </aside>
  );
}
