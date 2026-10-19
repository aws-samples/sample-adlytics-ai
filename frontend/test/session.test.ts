// SPDX-License-Identifier: MIT-0
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { afterEach, test, vi } from 'vitest';

import { PresentationRequestError } from '../lib/presentation';
import { fetchPresentationParentV2 } from '../lib/presentation-v2';
import {
  callApi,
  fetchSession,
  SessionExpiredError,
  SessionUnavailableError,
} from '../lib/session';

const originalFetch = globalThis.fetch;
const originalDocument = Object.getOwnPropertyDescriptor(globalThis, 'document');

function restoreGlobal(name: 'document', descriptor: PropertyDescriptor | undefined): void {
  if (descriptor === undefined) {
    Reflect.deleteProperty(globalThis, name);
  } else {
    Object.defineProperty(globalThis, name, descriptor);
  }
}

afterEach(() => {
  vi.unstubAllGlobals();
  globalThis.fetch = originalFetch;
  restoreGlobal('document', originalDocument);
});

test('sends JSON with cookies, CSRF, tracing, correlation, and the exact body hash', async () => {
  const calls: Array<{ readonly init: RequestInit; readonly path: string }> = [];
  Object.defineProperty(globalThis, 'document', {
    configurable: true,
    value: { cookie: 'adl_csrf=csrf-value' },
  });
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    calls.push({ init: init ?? {}, path: String(input) });
    return new Response('{}', { status: 200 });
  }) as typeof fetch;

  const controller = new AbortController();
  const response = await callApi('/api/presentation/campaigns', 'POST', {
    session_id: 'conversation-fictional',
  }, controller.signal);

  assert.equal(response.status, 200);
  assert.equal(calls.length, 1);
  const call = calls[0];
  assert.ok(call);
  const headers = new Headers(call.init.headers);
  const body = JSON.stringify({ session_id: 'conversation-fictional' });
  assert.equal(call.path, '/api/presentation/campaigns');
  assert.equal(call.init.body, body);
  assert.equal(call.init.credentials, 'include');
  assert.equal(call.init.cache, 'no-store');
  assert.equal(call.init.signal, controller.signal);
  assert.equal(headers.get('content-type'), 'application/json');
  assert.equal(headers.get('x-adl-csrf'), 'csrf-value');
  assert.equal(headers.get('x-amz-content-sha256'), createHash('sha256').update(body).digest('hex'));
  // ADR-081 §2: the server mints the correlation ID; the browser never proposes one.
  assert.equal(headers.has('x-correlation-id'), false);
  assert.match(headers.get('traceparent') ?? '', /^00-[0-9a-f]{32}-[0-9a-f]{16}-01$/);
  assert.equal(headers.has('authorization'), false);
});

test('refreshes once, retries the same body, and re-reads the rotated CSRF cookie', async () => {
  let cookie = 'adl_csrf=old-value';
  const calls: Array<{ readonly headers: Headers; readonly path: string; readonly body: BodyInit | null }> =
    [];
  Object.defineProperty(globalThis, 'document', {
    configurable: true,
    value: {
      get cookie() {
        return cookie;
      },
    },
  });
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    calls.push({
      body: init?.body ?? null,
      headers: new Headers(init?.headers),
      path,
    });
    if (path === '/api/auth/refresh') {
      cookie = 'adl_csrf=new-value';
      return new Response(null, { status: 200 });
    }
    return new Response(null, { status: calls.filter((call) => call.path !== '/api/auth/refresh').length === 1 ? 403 : 200 });
  }) as typeof fetch;

  const response = await callApi('/api/presentation/campaigns', 'POST', { value: 'fictional' });

  assert.equal(response.status, 200);
  assert.deepEqual(
    calls.map((call) => call.path),
    ['/api/presentation/campaigns', '/api/auth/refresh', '/api/presentation/campaigns'],
  );
  const first = calls[0];
  const retry = calls[2];
  assert.ok(first);
  assert.ok(retry);
  assert.equal(first.body, retry.body);
  assert.equal(first.headers.get('x-adl-csrf'), 'old-value');
  assert.equal(retry.headers.get('x-adl-csrf'), 'new-value');
  assert.equal(first.headers.get('x-amz-content-sha256'), retry.headers.get('x-amz-content-sha256'));
});


/**
 * ADR-007 amendment (2026-10-05): a 403 with an HTML body is the edge's block page. `callApi`
 * returns it, with no refresh and no login; every other 403 keeps the refresh path.
 */
type Answer = { readonly status: number; readonly contentType?: string };
function scripted(answers: readonly Answer[], refreshOk = true) {
  const paths: string[] = [];
  const assign = vi.fn();
  vi.stubGlobal('window', { location: { assign } });
  Object.defineProperty(globalThis, 'document', { configurable: true, value: { cookie: 'adl_csrf=c' } });
  let next = 0;
  globalThis.fetch = (async (input: RequestInfo | URL) => {
    const path = String(input);
    paths.push(path);
    if (path === '/api/auth/refresh') return new Response(null, { status: refreshOk ? 200 : 403 });
    const answer = answers[next++];
    assert.ok(answer, `unexpected request ${next} to ${path}`);
    const headers = new Headers(answer.contentType === undefined ? {} : { 'content-type': answer.contentType });
    // A byte body sets no content type of its own, so "no content type" stays absent.
    return new Response(new TextEncoder().encode('<html>blocked</html>'), { status: answer.status, headers });
  }) as typeof fetch;
  return { assign, paths };
}

test('returns an HTML 403 to the caller with no refresh and no login', async () => {
  for (const contentType of ['text/html', 'text/html; charset=utf-8', 'TEXT/HTML']) {
    const { assign, paths } = scripted([{ status: 403, contentType }]);
    const response = await callApi('/api/prompt-registry/create-version', 'POST', { slot_id: 'x' });
    assert.equal(response.status, 403);
    assert.equal(await response.text(), '<html>blocked</html>');
    assert.deepEqual(paths, ['/api/prompt-registry/create-version']);
    assert.equal(assign.mock.calls.length, 0);
    vi.unstubAllGlobals();
  }
});

test('still refreshes and retries once on a JSON 403', async () => {
  const { assign, paths } = scripted([
    { status: 403, contentType: 'application/json' },
    { status: 200, contentType: 'application/json' },
  ]);
  const response = await callApi('/api/presentation/campaigns', 'POST', { value: 'fictional' });
  assert.equal(response.status, 200);
  assert.deepEqual(paths, ['/api/presentation/campaigns', '/api/auth/refresh', '/api/presentation/campaigns']);
  assert.equal(assign.mock.calls.length, 0);
});

test('still refreshes on a 403 with no content type, and logs in when the refresh fails', async () => {
  const { assign, paths } = scripted([{ status: 403 }], false);
  await assert.rejects(callApi('/api/presentation/campaigns', 'POST', { value: 'fictional' }), SessionExpiredError);
  assert.deepEqual(paths, ['/api/presentation/campaigns', '/api/auth/refresh']);
  // Positive control: the login spy observes `beginLogin`.
  assert.deepEqual(assign.mock.calls, [['/api/auth/login']]);
});

test('returns an HTML 403 on the retried request with exactly one refresh and no login', async () => {
  const { assign, paths } = scripted([
    { status: 403, contentType: 'application/json' },
    { status: 403, contentType: 'text/html' },
  ]);
  const response = await callApi('/api/prompt-registry/create-version', 'POST', { slot_id: 'x' });
  assert.equal(response.status, 403);
  assert.equal(response.headers.get('content-type'), 'text/html');
  assert.deepEqual(paths, [
    '/api/prompt-registry/create-version',
    '/api/auth/refresh',
    '/api/prompt-registry/create-version',
  ]);
  assert.equal(assign.mock.calls.length, 0);
});

test('a presentation fetch answered with an HTML 403 throws its own error and never logs in', async () => {
  const { assign, paths } = scripted([{ status: 403, contentType: 'text/html' }]);
  // The presentation client's existing error for a non-JSON refusal.
  await assert.rejects(fetchPresentationParentV2('campaigns', 'conversation-fictional'), PresentationRequestError);
  assert.equal(paths.filter((path) => path === '/api/auth/refresh').length, 0);
  assert.equal(paths.length, 1);
  assert.equal(assign.mock.calls.length, 0);
});

test('reports session endpoint failures as unavailable rather than expired', async () => {
  Object.defineProperty(globalThis, 'document', {
    configurable: true,
    value: { cookie: '' },
  });
  globalThis.fetch = (async () => new Response('{}', { status: 503 })) as typeof fetch;

  await assert.rejects(fetchSession(), SessionUnavailableError);
});

test('reports session network failures as unavailable', async () => {
  Object.defineProperty(globalThis, 'document', {
    configurable: true,
    value: { cookie: '' },
  });
  globalThis.fetch = (async () => {
    throw new TypeError('network unavailable');
  }) as typeof fetch;

  await assert.rejects(fetchSession(), SessionUnavailableError);
});

test('reports malformed session identity as unavailable', async () => {
  Object.defineProperty(globalThis, 'document', {
    configurable: true,
    value: { cookie: '' },
  });
  globalThis.fetch = (async () =>
    new Response(JSON.stringify({ subject: '', unexpected: 'fictional' }), {
      status: 200,
    })) as typeof fetch;

  await assert.rejects(fetchSession(), SessionUnavailableError);
});


function serveSession(body: unknown): void {
  Object.defineProperty(globalThis, 'document', { configurable: true, value: { cookie: '' } });
  globalThis.fetch = (async () => new Response(JSON.stringify(body), { status: 200 })) as typeof fetch;
}

const SESSION_BODY = {
  subject: 'subject-fictional',
  can_manage_prompts: false,
  can_read_operational_status: false,
  operational_status_provisioned: false,
};

test('accepts only the server-derived prompt-administration entitlement projection', async () => {
  serveSession({ ...SESSION_BODY, can_manage_prompts: true });
  assert.deepEqual(await fetchSession(), {
    subject: 'subject-fictional',
    canManagePrompts: true,
    canReadOperationalStatus: false,
    operationalStatusProvisioned: false,
  });
});

// ADR-115 decision 8: the three Data pipeline status states, each beside the state that differs
// from it by one field, so no assertion passes for a reason other than the field it names.
test('not provisioned: neither field is set', async () => {
  serveSession(SESSION_BODY);
  assert.deepEqual(await fetchSession(), {
    subject: 'subject-fictional', canManagePrompts: false, canReadOperationalStatus: false, operationalStatusProvisioned: false,
  });
  serveSession({ ...SESSION_BODY, operational_status_provisioned: true });
  assert.equal((await fetchSession()).operationalStatusProvisioned, true);
});

test('provisioned without access: provisioned, not granted', async () => {
  serveSession({ ...SESSION_BODY, operational_status_provisioned: true });
  assert.deepEqual(await fetchSession(), {
    subject: 'subject-fictional', canManagePrompts: false, canReadOperationalStatus: false, operationalStatusProvisioned: true,
  });
  serveSession({ ...SESSION_BODY, operational_status_provisioned: true, can_read_operational_status: true });
  assert.equal((await fetchSession()).canReadOperationalStatus, true);
});

test('provisioned with access: provisioned and granted', async () => {
  serveSession({ ...SESSION_BODY, operational_status_provisioned: true, can_read_operational_status: true });
  assert.deepEqual(await fetchSession(), {
    subject: 'subject-fictional', canManagePrompts: false, canReadOperationalStatus: true, operationalStatusProvisioned: true,
  });
  serveSession({ ...SESSION_BODY, operational_status_provisioned: true });
  assert.equal((await fetchSession()).canReadOperationalStatus, false);
});

test('refuses the retired two- and three-key bodies and every other shape as unavailable', async () => {
  // Positive control: the full body decodes.
  serveSession(SESSION_BODY);
  assert.equal((await fetchSession()).subject, 'subject-fictional');
  for (const body of [
    { subject: 'subject-fictional', can_manage_prompts: false },
    { subject: 'subject-fictional', can_manage_prompts: false, can_read_operational_status: false },
    { subject: 'subject-fictional', can_manage_prompts: false, operational_status_provisioned: false },
    { ...SESSION_BODY, unexpected: false },
    { ...SESSION_BODY, can_read_operational_status: 'true' },
    { ...SESSION_BODY, operational_status_provisioned: null },
    { ...SESSION_BODY, can_manage_prompts: 'false' },
    { ...SESSION_BODY, can_read_operational_status: true },
    { ...SESSION_BODY, subject: ' ' },
    [],
  ]) {
    serveSession(body);
    await assert.rejects(fetchSession(), SessionUnavailableError);
  }
});
