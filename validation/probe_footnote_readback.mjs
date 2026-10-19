// SPDX-License-Identifier: MIT-0
// HIST-8, read-only: render one presented V2 page as a persona and record its footnote.
// Driven by `validation/probe_footnote_readback.py`, which judges the result.
// Inputs (env): ADL_BASE_URL, ADL_SESSION (persona access token, never printed), ADL_PATH,
// ADL_OUT (the readback document), ADL_TEXTS (a temporary file for the full page and asset
// texts, which the driver deletes). Prints nothing but the footnote line(s).
import { createHash, randomBytes } from 'node:crypto';
import { writeFile } from 'node:fs/promises';
import { createRequire } from 'node:module';

const require = createRequire(new URL('../frontend/package.json', import.meta.url));
const { chromium } = require('@playwright/test');

const BASE = process.env.ADL_BASE_URL.replace(/\/$/, '');
const SESSION = process.env.ADL_SESSION;
const PATH = process.env.ADL_PATH;
const sha = (s) => createHash('sha256').update(s).digest('hex');

// The first object at any depth carrying `key`, breadth-first; the page envelope's own field.
function find(value, key) {
  const queue = [value];
  while (queue.length) {
    const item = queue.shift();
    if (item === null || typeof item !== 'object') continue;
    if (!Array.isArray(item) && Object.hasOwn(item, key)) return item[key];
    queue.push(...Object.values(item));
  }
  return undefined;
}

const browser = await chromium.launch();
try {
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const csrf = randomBytes(16).toString('hex');
  await context.addCookies([
    { name: 'adl_session', value: SESSION, url: BASE, httpOnly: true, secure: true, sameSite: 'Lax' },
    { name: 'adl_csrf', value: csrf, url: BASE, httpOnly: false, secure: true, sameSite: 'Lax' },
  ]);
  const page = await context.newPage();
  const assets = [];
  const presentations = [];
  // A presentation response >= 400 (a governed refusal or a failure) renders a PageIssue with no
  // footnote, so the footnote wait races it and the readback records the error with the
  // handler's `error_class` instead of timing out; `presentation_error_reason` names it.
  let reportError;
  const errored = new Promise((resolve) => { reportError = resolve; });
  page.on('response', async (response) => {
    const url = new URL(response.url());
    if (url.origin !== BASE) return;
    try {
      if (url.pathname.startsWith('/api/presentation/')) {
        const status = response.status();
        let body = null;
        try { body = await response.json(); } catch { /* an error may carry no JSON body */ }
        presentations.push({ path: url.pathname, status, body });
        if (status >= 400) {
          reportError({
            path: url.pathname,
            status,
            code: typeof body?.code === 'string' ? body.code : null,
            error_class: typeof body?.error_class === 'string' ? body.error_class : null,
          });
        }
      } else if (!url.pathname.startsWith('/api/')) {
        assets.push({ path: url.pathname, text: await response.text() });
      }
    } catch { /* a non-text or aborted body is not an asset this reads */ }
  });
  const nav = await page.goto(`${BASE}${PATH}`, { waitUntil: 'networkidle', timeout: 60_000 });
  const presentationError = await Promise.race([
    page.getByText(/Data as of /).first().waitFor({ timeout: 60_000 }).then(() => null),
    errored,
  ]);
  await page.waitForLoadState('networkidle');
  const pageText = await page.evaluate(() => document.body.innerText);
  const readAt = new Date().toISOString();
  const footnotes = pageText.split('\n').filter((line) => line.includes('Data as of'));
  const served = presentations.filter((p) => p.status === 200 && find(p.body, 'generation') !== undefined);
  const parent = served[0] ?? presentations.find((p) => p.status === 200);
  const document_ = {
    url_path: PATH,
    final_path: new URL(page.url()).pathname,
    navigation_status: nav?.status() ?? null,
    read_at: readAt,
    footnote_lines: footnotes,
    page_text_sha256: sha(pageText),
    presentation_error: presentationError,
    presentation_path: parent?.path ?? null,
    generation: parent === undefined ? null : (find(parent.body, 'generation') ?? null),
    window_end_iso: parent === undefined ? null : (find(find(parent.body, 'resolved'), 'window')?.end ?? null),
    presentation_responses: presentations.map((p) => ({
      path: p.path,
      status: p.status,
      has_generation: find(p.body, 'generation') !== undefined,
    })),
    assets: assets.map((a) => ({ path: a.path, bytes: a.text.length, sha256: sha(a.text) })),
  };
  await writeFile(process.env.ADL_OUT, `${JSON.stringify(document_, null, 2)}\n`);
  await writeFile(process.env.ADL_TEXTS, JSON.stringify({ page_text: pageText, asset_texts: assets.map((a) => a.text) }));
  console.log(JSON.stringify({ footnote_lines: footnotes }));
} finally {
  await browser.close();
}
