// SPDX-License-Identifier: MIT-0
/** Capture one browser page-orientation chat stream without emitting its contents. */

import { createRequire } from 'node:module';

const require = createRequire(new URL('../frontend/package.json', import.meta.url));
const { chromium } = require('@playwright/test');

const input = async () => {
  let value = '';
  for await (const chunk of process.stdin) value += chunk;
  return JSON.parse(value);
};

const CHAT_STREAM_FAILURE_REASONS = new Set([
  'unsupported_media_type',
  'missing_body',
  'invalid_utf8',
  'invalid_sse_frame',
  'frame_limit_exceeded',
  'unterminated_frame',
  'invalid_stream_contract',
  'invalid_event_order',
  'missing_terminal',
]);

let stage = 'input';
try {
  const { origin, token } = await input();
  stage = 'launch';
  const browser = await chromium.launch({ headless: true });
  try {
    stage = 'context';
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    await context.addCookies([
      { name: 'adl_session', value: token, url: origin, httpOnly: true, secure: true, sameSite: 'Lax' },
      { name: 'adl_csrf', value: 'orientation-stream', url: origin, httpOnly: false, secure: true, sameSite: 'Lax' },
    ]);
    stage = 'page_load';
    const page = await context.newPage();
    // ADR-081 §2: the browser sends no correlation; the route mints one and returns it.
    let browserSentCorrelation = null;
    let streamFailureReason = null;
    const diagnosticTasks = [];
    page.on('request', (request) => {
      if (request.url().endsWith('/api/chat')) {
        browserSentCorrelation = request.headers()['x-correlation-id'] !== undefined;
      }
    });
    page.on('console', (message) => {
      if (message.type() !== 'warning' || message.text() !== 'Chat stream rejected.') return;
      const detail = message.args()[1];
      if (detail === undefined) return;
      diagnosticTasks.push(detail.jsonValue().then((value) => {
        if (
          value !== null &&
          typeof value === 'object' &&
          CHAT_STREAM_FAILURE_REASONS.has(value.reason)
        ) {
          streamFailureReason = value.reason;
        }
      }).catch(() => undefined));
    });
    await page.goto(`${origin}/campaigns/`, { waitUntil: 'domcontentloaded' });
    stage = 'page_context';
    const composer = page.getByRole('textbox', { name: 'Ask the workspace' });
    stage = 'chat_input';
    await composer.fill('I am new to this workspace. What kinds of questions and insights can this page help me explore?');
    stage = 'chat_response_wait';
    // Headers arrive before the streamed body, so the server's correlation is read from them.
    const responsePromise = page.waitForResponse(
      (response) => response.url().includes('/api/chat'),
      { timeout: 180_000 },
    );
    stage = 'chat_submit';
    await page.getByRole('button', { name: 'Submit' }).click();
    const response = await responsePromise.catch(() => null);
    stage = 'chat_terminal';
    const renderedTerminal = await page.waitForFunction(
      () => {
        if (document.querySelector('.msg-reference') !== null) return 'response';
        if (document.querySelector('[data-assistant-tone="failure"]') !== null) return 'failure';
        return null;
      },
      undefined,
      { timeout: 180_000 },
    );
    await Promise.all(diagnosticTasks);
    // The chat panel renders the reference once the turn has ended. Returned for the caller to
    // compare with the receipt and keep in ignored evidence; never printed.
    const renderedReferences = await page.locator('.msg-reference').allTextContents();
    process.stdout.write(JSON.stringify({
      http_status: response?.status() ?? null,
      browser_sent_correlation: browserSentCorrelation,
      correlation: response?.headers()['x-correlation-id'] ?? null,
      rendered_terminal: await renderedTerminal.jsonValue(),
      rendered_references: renderedReferences,
      stream_failure_reason: streamFailureReason,
    }));
    await context.close();
  } finally {
    await browser.close();
  }
} catch {
  process.stdout.write(JSON.stringify({ stage }));
  process.exitCode = 1;
}
