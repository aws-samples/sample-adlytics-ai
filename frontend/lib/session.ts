// SPDX-License-Identifier: MIT-0
/**
 * The only place the app talks to the API.
 *
 * There is no token here, and no code that looks for one. The browser holds
 * `HttpOnly` cookies it cannot read; `credentials: 'include'` is what attaches
 * them. The single readable cookie is the CSRF token, and echoing it is its only
 * purpose.
 *
 * The measured status-code decision: ADR-007 (docs/decisions/adr-007-auth-cookie-bff.md), 2026-09-25 amendment.
 */

import { clearConversationSessionId } from './conversation-session';
import { LOGIN_PATH, LOGOUT_PATH, REFRESH_PATH, SESSION_PATH } from './config';
import { isEdgeRefusal } from './edge-refusal';

const CSRF_COOKIE = 'adl_csrf';
const CSRF_HEADER = 'x-adl-csrf';

const SAFE_METHODS = new Set(['GET', 'HEAD', 'OPTIONS']);
export const PRESENTATION_V2_MEDIA_TYPE = 'application/vnd.adlytics.presentation-v2+json';

export interface ApiRequestOptions {
  readonly accept?: typeof PRESENTATION_V2_MEDIA_TYPE;
}

/**
 * Shared across concurrent callers so several denials produce one refresh.
 *
 * Without this, three requests failing at once fire three refreshes; two of them
 * rotate the session out from under the third, which then fails for a reason
 * that looks nothing like the original.
 */
let inFlightRefresh: Promise<boolean> | null = null;

export class SessionExpiredError extends Error {
  constructor() {
    super('The session is no longer valid.');
    this.name = 'SessionExpiredError';
  }
}

export class SessionUnavailableError extends Error {
  constructor() {
    super('Session status is unavailable.');
    this.name = 'SessionUnavailableError';
  }
}

/** Read the one auth cookie JavaScript is allowed to see. */
function csrfToken(): string {
  const match = document.cookie.match(new RegExp(`(?:^|;\\s*)${CSRF_COOKIE}=([^;]*)`));
  const value = match?.[1];
  // An absent cookie yields an empty string, which the backend rejects. Never
  // substitute anything for it: absent must fail, not pass.
  return value === undefined ? '' : decodeURIComponent(value);
}

/**
 * A syntactically valid W3C traceparent with a fresh trace and span ID.
 *
 * The browser emits this sampled context, but the standard-library conversation
 * route does not currently extract it. Route-to-Harness continuity starts with
 * the route's server trace and is separate from this browser parent. No correlation
 * ID is sent: the server mints one per request and returns it in `x-correlation-id`
 * (ADR-081 §2). Correlation ID and traceparent do different jobs — see
 * docs/concepts/observability.md §2.2.
 */
function traceparent(): string {
  const hex = (bytes: number): string =>
    Array.from(crypto.getRandomValues(new Uint8Array(bytes)))
      .map((byte) => byte.toString(16).padStart(2, '0'))
      .join('');
  return `00-${hex(16)}-${hex(8)}-01`;
}

export type JsonPrimitive = boolean | null | number | string;
export type JsonValue =
  | JsonPrimitive
  | readonly JsonValue[]
  | { readonly [key: string]: JsonValue };

interface JsonBody {
  readonly sha256: string;
  readonly text: string;
}

function buildHeaders(
  method: string,
  body?: JsonBody,
  options?: ApiRequestOptions,
): Headers {
  const headers = new Headers({ traceparent: traceparent() });
  if (options?.accept !== undefined) {
    headers.set('accept', options.accept);
  }
  if (body !== undefined) {
    headers.set('content-type', 'application/json');
    headers.set('x-amz-content-sha256', body.sha256);
  }
  if (!SAFE_METHODS.has(method)) {
    // Read at send time, never cached. A refresh rotates the CSRF cookie, so a
    // value captured earlier is already stale.
    headers.set(CSRF_HEADER, csrfToken());
  }
  return headers;
}

async function jsonBody(value: JsonValue | undefined): Promise<JsonBody | undefined> {
  if (value === undefined) {
    return undefined;
  }
  const text = JSON.stringify(value);
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text));
  const sha256 = Array.from(new Uint8Array(digest))
    .map((byte) => byte.toString(16).padStart(2, '0'))
    .join('');
  return { sha256, text };
}

async function send(
  path: string,
  method: string,
  body?: JsonBody,
  signal?: AbortSignal,
  options?: ApiRequestOptions,
): Promise<Response> {
  // A relative URL, because CloudFront serves every route from this same origin.
  // Its behavior sends conversation requests to the Lambda Function URL and
  // other API requests to their configured origin. Nothing here names a host.
  return fetch(path, {
    method,
    credentials: 'include',
    headers: buildHeaders(method, body, options),
    // A session response must never be served from cache.
    cache: 'no-store',
    ...(body === undefined ? {} : { body: body.text }),
    ...(signal === undefined ? {} : { signal }),
  });
}

/**
 * Attempt one refresh, shared between concurrent callers.
 *
 * Never retried on failure. A refresh that fails means re-authenticating, and
 * looping here is how a broken session becomes a request storm.
 */
async function refreshOnce(): Promise<boolean> {
  if (inFlightRefresh === null) {
    inFlightRefresh = send(REFRESH_PATH, 'POST')
      .then((response) => response.ok)
      .catch(() => false)
      .finally(() => {
        inFlightRefresh = null;
      });
  }
  return inFlightRefresh;
}

/**
 * Call an authenticated endpoint, refreshing once if the session has expired.
 *
 * Denial semantics are owned by ADR-007 (2026-09-28 amendment):
 *
 *   401  no credential. Only the conversation route answers this today; the API
 *        authorizer has no identity source until the edge slice, so a cookieless
 *        API request arrives as 403 below instead. There is no session to refresh.
 *   403  a credential was refused, or none reached the API authorizer. This is the
 *        refresh trigger; with no cookies the refresh fails and login begins.
 *
 * An expired session lands on 403 rather than 401 because adl_refresh outlives
 * adl_session, so a Cookie header is still present. That ordering is what makes
 * refresh reachable at all, and it is asserted in the integration tier.
 *
 * 403 is only ever treated as a refresh trigger for authenticated routes.
 * `/auth/refresh` itself answers 403 when CSRF fails, and retrying that would loop
 * forever.
 *
 * 403 with an HTML body is an edge refusal, not a session refusal; it is returned to
 * the caller (ADR-007 amendment, 2026-10-05). The edge web ACL answers a block with an
 * HTML page, and refreshing would resend the blocked request and then start login,
 * losing whatever the page holds. The rule applies to the first and the retried
 * response. Every other 403, including one with no content type, keeps the refresh
 * path, so an expired session is never misread as an edge block.
 */
export async function callApi(
  path: string,
  method: string = 'GET',
  body?: JsonValue,
  signal?: AbortSignal,
  options?: ApiRequestOptions,
): Promise<Response> {
  const preparedBody = await jsonBody(body);
  const first = await send(path, method, preparedBody, signal, options);

  // No credentials were presented, so there is nothing to refresh.
  if (first.status === 401) {
    beginLogin();
    throw new SessionExpiredError();
  }

  if (first.status !== 403 || isEdgeRefusal(first)) {
    return first;
  }

  if (!(await refreshOnce())) {
    beginLogin();
    throw new SessionExpiredError();
  }

  // Exactly one retry. Headers are rebuilt, which re-reads the rotated CSRF cookie.
  const second = await send(path, method, preparedBody, signal, options);
  if (isEdgeRefusal(second)) {
    return second;
  }
  if (second.status === 401 || second.status === 403) {
    beginLogin();
    throw new SessionExpiredError();
  }
  return second;
}

export interface Session {
  readonly subject: string;
  readonly canManagePrompts: boolean;
  /** Group member AND provisioned in this deployment (ADR-115 decision 8). */
  readonly canReadOperationalStatus: boolean;
  /** Whether this deployment has the operational status API at all; grants nothing. */
  readonly operationalStatusProvisioned: boolean;
}

/**
 * Exactly these `/me` keys, all required (ADR-115 decision 8). The server ships them first, so
 * this bundle never meets a shorter body; a shorter or longer one is refused as unavailable.
 */
const SESSION_KEYS = [
  'subject',
  'can_manage_prompts',
  'can_read_operational_status',
  'operational_status_provisioned',
] as const;

/** Who the API says we are. The only source of identity in the UI. */
export async function fetchSession(): Promise<Session> {
  try {
    const response = await callApi(SESSION_PATH);
    if (!response.ok) {
      throw new SessionUnavailableError();
    }
    const value: unknown = await response.json();
    if (typeof value !== 'object' || value === null || Array.isArray(value)) {
      throw new SessionUnavailableError();
    }
    const fields = value as {
      readonly subject?: unknown;
      readonly can_manage_prompts?: unknown;
      readonly can_read_operational_status?: unknown;
      readonly operational_status_provisioned?: unknown;
    };
    const keys = Object.keys(fields);
    if (
      keys.length !== SESSION_KEYS.length ||
      !SESSION_KEYS.every((key) => Object.hasOwn(fields, key)) ||
      typeof fields.subject !== 'string' ||
      !fields.subject.trim() ||
      typeof fields.can_manage_prompts !== 'boolean' ||
      typeof fields.can_read_operational_status !== 'boolean' ||
      typeof fields.operational_status_provisioned !== 'boolean' ||
      // The server derives the entitlement from provisioning, so a grant without it is malformed.
      (fields.can_read_operational_status && !fields.operational_status_provisioned)
    ) {
      throw new SessionUnavailableError();
    }
    return {
      subject: fields.subject,
      canManagePrompts: fields.can_manage_prompts,
      canReadOperationalStatus: fields.can_read_operational_status,
      operationalStatusProvisioned: fields.operational_status_provisioned,
    };
  } catch (error: unknown) {
    if (error instanceof SessionExpiredError || error instanceof SessionUnavailableError) {
      throw error;
    }
    throw new SessionUnavailableError();
  }
}

/**
 * Begin a login by navigating to our own login endpoint.
 *
 * Never a provider URL assembled here. The app does not know that login currently
 * happens to be a redirect to Cognito, so replacing managed login with an in-app
 * form stays a server-side change.
 */
export function beginLogin(): void {
  // This endpoint begins a server-owned redirect flow, not a client-side route.
  window.location.assign(LOGIN_PATH);
}

/** Revoke at the provider and clear the cookies, then return to a clean page. */
export async function signOut(): Promise<void> {
  clearConversationSessionId();
  await send(LOGOUT_PATH, 'POST').catch(() => undefined);
  // A full reload clears all in-memory presentation and chat state after revocation.
  // eslint-disable-next-line @next/next/no-location-assign-relative-destination
  window.location.assign('/');
}
