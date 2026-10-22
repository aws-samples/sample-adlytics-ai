// SPDX-License-Identifier: MIT-0
/**
 * Sign one synthetic persona in through Cognito managed login, as Amazon Quick's browser would,
 * and hand the authorization code back to `probe_external_mcp.py` (ADR-150).
 *
 * Input, on stdin only, as JSON: `authorizeUrl` (PKCE S256 and `resource` already in it),
 * `redirectUri`, `username`, `password`. The callback request to `redirectUri` is intercepted and
 * aborted, so the code never reaches Quick; stdout is `{"code", "state"}` and nothing else. No
 * credential, code or page content is ever printed. The managed login v2 field labels are
 * [unverified] against the live page; a selector change is a fix to this file, never to the flow.
 */
import { createRequire } from 'node:module';

const require = createRequire(new URL('../frontend/package.json', import.meta.url));
const { chromium } = require('@playwright/test');

async function readStdin() {
  const chunks = [];
  for await (const chunk of process.stdin) chunks.push(chunk);
  return JSON.parse(Buffer.concat(chunks).toString('utf-8'));
}

const request = await readStdin();
for (const key of ['authorizeUrl', 'redirectUri', 'username', 'password']) {
  if (typeof request[key] !== 'string' || request[key] === '') {
    process.stderr.write(`missing ${key}\n`);
    process.exit(2);
  }
}

const browser = await chromium.launch({ headless: true });
try {
  const page = await (await browser.newContext()).newPage();
  let captured;
  // Abort the callback so the code is read here and never delivered to Quick.
  await page.route(`${request.redirectUri}**`, async (route) => {
    captured = new URL(route.request().url());
    await route.abort();
  });
  await page.goto(request.authorizeUrl, { waitUntil: 'domcontentloaded' }).catch(() => undefined);
  await page.getByLabel(/email|username/i).first().fill(request.username);
  const next = page.getByRole('button', { name: /^next$/i });
  if (await next.count()) await next.first().click();
  await page.getByLabel(/password/i).first().fill(request.password);
  await page.getByRole('button', { name: /sign in|continue|log in/i }).first().click();
  const deadline = Date.now() + 60_000;
  while (captured === undefined && Date.now() < deadline) {
    await page.waitForTimeout(250);
  }
  if (captured === undefined) {
    process.stderr.write('no callback within 60 s\n');
    process.exit(3);
  }
  const code = captured.searchParams.get('code');
  const state = captured.searchParams.get('state');
  if (!code || !state) {
    process.stderr.write(`callback carried no code: ${captured.searchParams.get('error') ?? 'unknown'}\n`);
    process.exit(4);
  }
  process.stdout.write(JSON.stringify({ code, state }));
} finally {
  await browser.close();
}
