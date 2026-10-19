// SPDX-License-Identifier: MIT-0
/**
 * ADR-123 decision 2 (ia §6.3 #6): the not-found page is static and needs no session. Signed in it
 * has the full shell and the docked assistant with no context line. A signed-out visitor to an
 * unknown path is sent to sign-in, as frontend-auth-session requires: `callApi` begins login when
 * the session check is refused (test/session.test.ts). So the signed-out 404 shell (no assistant,
 * Assistant toggle or Sign out; Sign in offered) shows only briefly before the redirect, or when
 * session status is unavailable. These tests render that shell directly.
 */
import assert from 'node:assert/strict';
import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ReactNode } from 'react';
import { hydrateRoot } from 'react-dom/client';
import { renderToString } from 'react-dom/server';
import { beforeEach, test, vi } from 'vitest';

const navigation = vi.hoisted(() => ({ pathname: '/route-that-does-not-exist/' }));
const session = vi.hoisted(() => {
  class SessionExpiredError extends Error {}
  return {
    SessionExpiredError,
    beginLogin: vi.fn(),
    fetchSession: vi.fn(),
    signOut: vi.fn(),
  };
});
vi.mock('next/navigation', () => ({ usePathname: () => navigation.pathname, useRouter: () => ({ push: vi.fn() }) }));
vi.mock('next/link', () => ({
  default: ({ children, href, ...props }: { children: ReactNode; href: string }) => (
    <a href={href} {...props}>{children}</a>
  ),
}));
vi.mock('@/lib/session', () => session);
vi.mock('@/components/ChatPanel', () => ({
  ChatPanel: ({ pageLabel }: { pageLabel: string | null }) => (
    <aside aria-label="Assistant" data-page-label={pageLabel ?? 'none'} />
  ),
  toggleAssistant: vi.fn(),
}));
import { AppShell } from '@/components/AppShell';
import { NotFoundPage } from '@/components/NotFoundPage';
import { ApplicationController } from '@/components/PresentationController';

function installMediaQuery({ narrow = false }: { narrow?: boolean } = {}) {
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    value: (media: string) => ({
      addEventListener: vi.fn(),
      addListener: vi.fn(),
      matches: media === '(max-width: 880px)' ? narrow : false,
      media,
      removeEventListener: vi.fn(),
      removeListener: vi.fn(),
    }) as unknown as MediaQueryList,
  });
}

const PATH = '/route-that-does-not-exist/';

function signedOut(pathname = PATH) {
  return <AppShell pathname={pathname} sessionId={null}><NotFoundPage /></AppShell>;
}

beforeEach(() => {
  navigation.pathname = PATH;
  session.beginLogin.mockReset();
  session.fetchSession.mockReset();
  installMediaQuery();
});

test('the page: h1, one sentence, "Go to {root tab}" and the customer hubs only', () => {
  render(<AppShell pathname={PATH} sessionId="conversation-fictional" entitlements={{ can_manage_prompts: true }}><NotFoundPage /></AppShell>);
  const heading = screen.getByRole('heading', { level: 1, name: 'Page not found' });
  assert.equal(document.activeElement, heading);
  assert.equal(screen.getAllByRole('heading', { level: 1 }).length, 1);
  assert.ok(screen.getByText("This page doesn't exist or has moved."));
  // The root tab's own label: Campaigns until Overview ships at "/".
  const root = screen.getByRole('link', { name: 'Go to Campaigns' });
  assert.equal(root.getAttribute('href'), '/');
  assert.equal(root.className, 'btn primary');
  const hubs = screen.getByRole('heading', { level: 2, name: 'Hubs' }).nextElementSibling as HTMLElement;
  assert.deepEqual(
    within(hubs).getAllByRole('link').map((link) => [link.textContent, link.getAttribute('href')]),
    [
      ['Performance', '/scorecard/'],
      ['Reach and audiences', '/reach/'],
      ['Customers', '/new-customers/'],
      ['Retail health', '/retail-health/'],
    ],
  );
  // The operator hub stays in the session's navigation, never in the page's hub list.
  assert.ok(screen.getByRole('list', { name: 'Operators' }));
});

test('signed in: the full shell, the docked assistant with no context line, and Sign out', () => {
  render(<AppShell pathname={PATH} sessionId="conversation-fictional"><NotFoundPage /></AppShell>);
  assert.equal(screen.getByRole('complementary', { name: 'Assistant' }).dataset.pageLabel, 'none');
  assert.ok(screen.getByRole('button', { name: 'Assistant' }));
  assert.ok(screen.getByRole('button', { name: 'Sign out' }));
  assert.ok(screen.getByRole('link', { name: 'Skip to assistant' }));
  assert.equal(screen.queryByRole('button', { name: 'Sign in' }), null);
  assert.deepEqual(
    within(screen.getByRole('navigation', { name: 'Breadcrumb' })).getAllByRole('listitem').map((item) => item.textContent),
    ['Page not found'],
  );
  assert.equal(document.title, 'Page not found · Adlytics AI');
});

test('the brief signed-out shell: no assistant, Assistant toggle or Sign out, no inline style, and Sign in', async () => {
  const user = userEvent.setup();
  render(signedOut());
  assert.equal(screen.queryByRole('complementary', { name: 'Assistant' }), null);
  assert.equal(screen.queryByRole('button', { name: 'Assistant' }), null);
  assert.equal(screen.queryByRole('button', { name: 'Sign out' }), null);
  assert.equal(screen.queryByRole('link', { name: 'Skip to assistant' }), null);
  assert.ok(screen.getByRole('link', { name: 'Skip to main content' }));
  assert.ok(screen.getByRole('button', { name: 'Theme: System' }));
  const app = document.getElementById('app-root');
  assert.equal(app?.dataset.assistant, 'absent');
  assert.equal(app?.hasAttribute('style'), false);
  assert.equal(document.querySelectorAll('[style]').length, 0);
  // Customer hubs only: a signed-out visit has no entitlements.
  const hubs = screen.getByRole('navigation', { name: 'Hubs' });
  assert.deepEqual(within(hubs).getAllByRole('link').map((link) => link.getAttribute('href')), ['/', '/scorecard/', '/reach/', '/new-customers/', '/retail-health/']);
  assert.equal(document.querySelectorAll('nav[aria-label="Hubs"] [aria-current]').length, 0);
  assert.deepEqual(
    within(screen.getByRole('navigation', { name: 'Breadcrumb' })).getAllByRole('listitem').map((item) => item.textContent),
    ['Page not found'],
  );
  assert.equal(document.title, 'Page not found · Adlytics AI');
  await user.click(screen.getByRole('button', { name: 'Sign in' }));
  assert.equal(session.beginLogin.mock.calls.length, 1);
});

test('the brief signed-out shell below 881 px: the menu sheet offers Sign in instead of Sign out', async () => {
  installMediaQuery({ narrow: true });
  const user = userEvent.setup();
  render(signedOut());
  await user.click(screen.getByRole('button', { name: 'Menu' }));
  const sheet = screen.getByRole('dialog', { name: 'Menu' });
  assert.equal(within(sheet).queryByRole('button', { name: 'Sign out' }), null);
  assert.deepEqual(within(within(sheet).getByRole('navigation', { name: 'Hubs' })).getAllByRole('link').map((link) => link.textContent), ['Performance', 'Scorecard', 'Campaigns', 'Reach and audiences', 'Reach and funnel', 'Audiences', 'Customers', 'New customers', 'Retention', 'Retail health']);
  // No path heads the sheet: the page isn't in any hub.
  assert.equal(sheet.querySelector('.path'), null);
  await user.click(within(sheet).getByRole('button', { name: 'Sign in' }));
  assert.equal(session.beginLogin.mock.calls.length, 1);
});

test('the application renders the signed-out 404 shell before the session is known and when it is unavailable', async () => {
  let reject: (error: unknown) => void = () => {};
  session.fetchSession.mockReturnValue(new Promise((_, fail) => { reject = fail; }));
  render(<ApplicationController><NotFoundPage /></ApplicationController>);
  // Before the session is known: the shell, not "Checking session…".
  assert.ok(screen.getByRole('heading', { level: 1, name: 'Page not found' }));
  assert.ok(screen.getByRole('button', { name: 'Sign in' }));
  assert.equal(screen.queryByText('Checking session…'), null);
  // A refused session check doesn't land here: callApi begins login first (frontend-auth-session),
  // so only an unavailable session status keeps the signed-out shell on screen.
  await act(async () => { reject(new Error('session status unavailable')); });
  assert.ok(screen.getByRole('heading', { level: 1, name: 'Page not found' }));
  assert.ok(screen.getByRole('button', { name: 'Sign in' }));
  assert.equal(screen.queryByRole('complementary', { name: 'Assistant' }), null);
});

test('the application renders the signed-in 404 shell once the session is verified', async () => {
  session.fetchSession.mockResolvedValue({ canManagePrompts: false, subject: 'fictional-subject' });
  render(<ApplicationController><NotFoundPage /></ApplicationController>);
  await waitFor(() => assert.ok(screen.getByRole('complementary', { name: 'Assistant' })));
  assert.ok(screen.getByRole('heading', { level: 1, name: 'Page not found' }));
  assert.ok(screen.getByRole('button', { name: 'Sign out' }));
  assert.equal(screen.queryByRole('button', { name: 'Sign in' }), null);
});

test('a manifest route still waits for the session and says when it has ended', async () => {
  navigation.pathname = '/campaigns/';
  session.fetchSession.mockRejectedValue(new session.SessionExpiredError('expired'));
  render(<ApplicationController><p>Campaigns page</p></ApplicationController>);
  await waitFor(() => assert.ok(screen.getByText('Your session has ended.')));
  assert.equal(screen.queryByText('Campaigns page'), null);
  assert.equal(screen.queryByRole('navigation', { name: 'Hubs' }), null);
});

test('a direct visit to an exported index.html is its route: it waits for the session, then loads', async () => {
  // The edge passes `/campaigns/index.html` through to the campaigns page (ADR-123), so the
  // controller must not take it for the not-found page and render the page without a session.
  navigation.pathname = '/campaigns/index.html';
  let resolve: (value: unknown) => void = () => {};
  session.fetchSession.mockReturnValue(new Promise((done) => { resolve = done; }));
  render(<ApplicationController><p>Campaigns page</p></ApplicationController>);
  assert.ok(screen.getByText('Checking session…'));
  assert.equal(screen.queryByText('Campaigns page'), null);
  await act(async () => { resolve({ canManagePrompts: false, subject: 'fictional-subject' }); });
  assert.ok(screen.getByText('Campaigns page'));
  assert.equal(document.title, 'Campaigns · Performance · Adlytics AI');
});

test('the prerendered signed-out shell hydrates without a mismatch when a theme is stored', async () => {
  localStorage.setItem('adlytics.theme', 'dark');
  const container = document.createElement('div');
  document.body.appendChild(container);
  // The static export renders without a browser: no stored theme, so "Theme: System".
  const storage = Object.getOwnPropertyDescriptor(window, 'localStorage');
  Object.defineProperty(window, 'localStorage', { configurable: true, get: () => { throw new Error('no storage at build'); } });
  container.innerHTML = renderToString(signedOut());
  Object.defineProperty(window, 'localStorage', storage!);
  assert.match(container.textContent ?? '', /Theme: System/);
  const errors: unknown[] = [];
  let root: ReturnType<typeof hydrateRoot> | null = null;
  await act(async () => {
    root = hydrateRoot(container, signedOut(), { onRecoverableError: (error) => errors.push(error) });
  });
  assert.deepEqual(errors, []);
  assert.ok(within(container).getByRole('button', { name: 'Theme: Dark' }));
  act(() => root?.unmount());
  container.remove();
});
