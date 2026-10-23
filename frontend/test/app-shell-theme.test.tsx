// SPDX-License-Identifier: MIT-0
import assert from 'node:assert/strict';
import { act, fireEvent, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState, type ReactNode } from 'react';
import { afterEach, test, vi } from 'vitest';
vi.mock('next/navigation', () => ({ usePathname: () => '/campaigns/', useRouter: () => ({ push: vi.fn() }) }));
vi.mock('next/link', () => ({
  default: ({ children, href, ...props }: { children: ReactNode; href: string }) => (
    <a href={href} {...props}>{children}</a>
  ),
}));
vi.mock('@/lib/session', () => ({ signOut: vi.fn() }));
vi.mock('@/components/ChatPanel', () => ({
  ChatPanel: () => <aside aria-label="Assistant" />,
  toggleAssistant: vi.fn(),
}));
import { AppShell, useOpenEntity } from '@/components/AppShell';

/** matchMedia per query: `dark` answers the color scheme, `narrow` the 881 px breakpoint. */
function installMediaQuery({ dark = false, narrow = false }: { dark?: boolean; narrow?: boolean } = {}) {
  let listener: ((event: MediaQueryListEvent) => void) | null = null;
  // Only the color-scheme listeners are counted; the breakpoint query has its own.
  const addEventListener = vi.fn();
  const removeEventListener = vi.fn();
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    value: (media: string) => {
      return {
        addEventListener: (type: string, callback: (event: MediaQueryListEvent) => void) => {
          if (media === '(prefers-color-scheme: dark)') {
            listener = callback;
            addEventListener(type, callback);
          }
        },
        addListener: vi.fn(),
        matches: media === '(prefers-color-scheme: dark)' ? dark : media === '(max-width: 880px)' ? narrow : false,
        media,
        removeEventListener: (type: string, callback: (event: MediaQueryListEvent) => void) => {
          if (media === '(prefers-color-scheme: dark)') removeEventListener(type, callback);
        },
        removeListener: vi.fn(),
      } as unknown as MediaQueryList;
    },
  });
  return {
    addEventListener,
    change: (next: boolean) => listener?.({ matches: next } as MediaQueryListEvent),
    removeEventListener,
  };
}

function shell(pathname = '/campaigns/', entitlements = {}, children: ReactNode = <p>Fictional evidence</p>) {
  return <AppShell pathname={pathname} sessionId="conversation-fictional" entitlements={entitlements}>{children}</AppShell>;
}

afterEach(() => {
  localStorage.clear();
  document.documentElement.removeAttribute('data-theme');
});

test('theme chooser persists preferences, restores focus, and never calls application APIs', async () => {
  const media = installMediaQuery({ dark: true });
  const user = userEvent.setup();
  render(shell());
  const trigger = screen.getByRole('button', { name: 'Theme: System' });
  assert.equal(document.documentElement.dataset.theme, 'system');
  assert.equal(media.addEventListener.mock.calls.length, 1);
  trigger.focus();
  await user.click(trigger);
  assert.equal(screen.getByRole('menuitemradio', { name: 'System' }).getAttribute('aria-checked'), 'true');
  await user.click(screen.getByRole('menuitemradio', { name: 'Dark' }));
  assert.equal(document.documentElement.dataset.theme, 'dark');
  assert.equal(localStorage.getItem('adlytics.theme'), 'dark');
  assert.equal(document.activeElement, trigger);
  assert.match(screen.getByRole('status').textContent ?? '', /Theme set to Dark, using dark mode/);
  assert.equal(media.removeEventListener.mock.calls.length, 1);
  await user.click(trigger);
  await user.click(screen.getByRole('menuitemradio', { name: 'System' }));
  assert.equal(document.documentElement.dataset.theme, 'system');
  assert.equal(localStorage.getItem('adlytics.theme'), 'system');
  assert.equal(media.addEventListener.mock.calls.length, 2);
});

test('theme chooser supports roving keyboard focus and Escape restoration', async () => {
  installMediaQuery();
  const user = userEvent.setup();
  render(shell());
  const trigger = screen.getByRole('button', { name: 'Theme: System' });
  trigger.focus();
  await user.click(trigger);
  assert.equal(document.activeElement, screen.getByRole('menuitemradio', { name: 'System' }));
  await user.keyboard('{ArrowDown}');
  assert.equal(document.activeElement, screen.getByRole('menuitemradio', { name: 'Light' }));
  await user.keyboard('{End}');
  assert.equal(document.activeElement, screen.getByRole('menuitemradio', { name: 'Dark' }));
  await user.keyboard('{Escape}');
  assert.equal(screen.queryByRole('menu'), null);
  assert.equal(document.activeElement, trigger);
});

test('navigation comes from the route manifest and lists only hubs with a shipped tab', () => {
  installMediaQuery();
  render(shell());
  const hubs = screen.getByRole('navigation', { name: 'Hubs' });
  const links = within(hubs).getAllByRole('link');
  assert.deepEqual(links.map((link) => [link.getAttribute('aria-label'), link.getAttribute('href')]), [
    ['Adlytics AI, go to Growth check', '/'],
    // ADR-151: the composed Overview owns the root and is the first hub.
    ['Overview', '/'],
    // UI slice 9a: the Scorecard is the Performance hub's first tab.
    ['Performance', '/scorecard/'],
    ['Reach and audiences', '/reach/'],
    ['Customers', '/new-customers/'],
    ['Retail health', '/retail-health/'],
  ]);
  // Two visible tabs (Scorecard and Campaigns): the hub item is the current hub, not a page.
  assert.equal(within(hubs).getByRole('link', { name: 'Performance' }).getAttribute('aria-current'), 'true');
  assert.equal(within(hubs).getByRole('link', { name: 'Retail health' }).getAttribute('aria-current'), null);
  // The rail's visible short label sits inside the accessible name.
  assert.equal(within(hubs).getByRole('link', { name: 'Retail health' }).querySelector('.short')?.textContent, 'Retail');
  assert.equal(screen.queryByText(/server-declared/), null);
});

test('the Operators group is listed only for the prompt-admin session entitlement', () => {
  installMediaQuery();
  const { rerender } = render(shell());
  assert.equal(screen.queryByRole('link', { name: 'Operations' }), null);
  rerender(shell('/campaigns/', { can_manage_prompts: true }));
  const operators = screen.getByRole('list', { name: 'Operators' });
  assert.equal(within(operators).getByRole('link', { name: 'Operations' }).getAttribute('href'), '/admin/prompts/');
});

test('landmarks come in order and the h1 is the tab name, focused on a route change', () => {
  installMediaQuery();
  const { rerender } = render(shell());
  const order = Array.from(document.querySelectorAll('header, nav[aria-label="Hubs"], main, aside')).map((node) => node.tagName.toLowerCase());
  assert.deepEqual(order, ['header', 'nav', 'main', 'aside']);
  assert.equal(screen.getByRole('link', { name: 'Skip to main content' }).getAttribute('href'), '#main-content');
  assert.equal(screen.getByRole('link', { name: 'Skip to assistant' }).getAttribute('href'), '#chat-question');
  const heading = screen.getByRole('heading', { level: 1, name: 'Campaigns' });
  assert.equal(document.activeElement, heading);
  // UI refresh 3.8: the tab's question is its lead line, in place of the description.
  assert.equal(document.querySelector('.hubhead .desc.lead-q')?.textContent, 'Which campaigns should get more or less?');
  assert.equal(document.title, 'Campaigns · Performance · Adlytics AI');
  assert.deepEqual(
    within(screen.getByRole('navigation', { name: 'Breadcrumb' })).getAllByRole('listitem').map((item) => item.textContent),
    ['Performance', 'Campaigns'],
  );

  rerender(shell('/retail-health/'));
  assert.equal(document.activeElement, screen.getByRole('heading', { level: 1, name: 'Retail health' }));
  assert.equal(document.title, 'Retail health · Adlytics AI');
  // A tab that carries its hub's name has a one-item crumb; Products and ads (UI refresh 3.7)
  // gives the hub a tab strip, each tab with its question under its label (3.8).
  assert.deepEqual(
    within(screen.getByRole('navigation', { name: 'Breadcrumb' })).getAllByRole('listitem').map((item) => item.textContent),
    ['Retail health'],
  );
  const strip = screen.getByRole('navigation', { name: 'Sections of Retail health' });
  assert.deepEqual(within(strip).getAllByRole('link').map((link) => link.textContent), ['Retail healthAre my products selling?', 'Products and ads']);
  assert.ok(within(strip).getByRole('link', { name: 'Retail health' }));
});

test('the root route is the composed Overview tab (ADR-151)', () => {
  installMediaQuery();
  render(shell('/'));
  // UI refresh 3.8: the Overview hub's one tab is the Growth check, led by its question.
  assert.ok(screen.getByRole('heading', { level: 1, name: 'Growth check' }));
  assert.ok(screen.getByText('Is your advertising growing your business?'));
  assert.equal(document.title, 'Growth check · Overview · Adlytics AI');
});

test('a direct visit without the entitlement names only the page and marks no navigation current', () => {
  installMediaQuery();
  render(shell('/admin/prompts/'));
  assert.ok(screen.getByRole('heading', { level: 1, name: 'Prompt administration' }));
  assert.equal(screen.queryByRole('link', { name: 'Operations' }), null);
  assert.equal(document.querySelectorAll('nav[aria-label="Hubs"] [aria-current]').length, 0);
  assert.deepEqual(
    within(screen.getByRole('navigation', { name: 'Breadcrumb' })).getAllByRole('listitem').map((item) => item.textContent),
    ['Prompt administration'],
  );
});

function EntityPage() {
  const [open, setOpen] = useState(true);
  useOpenEntity(open ? 'Fictional campaign Dune' : null, () => setOpen(false));
  return <p>{open ? 'Detail open' : 'List'}</p>;
}

test('an entity open in place becomes the h1 and the crumb tab link closes it', () => {
  installMediaQuery();
  render(shell('/campaigns/', {}, <EntityPage />));
  assert.ok(screen.getByRole('heading', { level: 1, name: 'Fictional campaign Dune' }));
  assert.equal(document.title, 'Fictional campaign Dune · Campaigns · Performance · Adlytics AI');
  const crumb = screen.getByRole('navigation', { name: 'Breadcrumb' });
  fireEvent.click(within(crumb).getByRole('link', { name: 'Campaigns' }));
  assert.ok(screen.getByText('List'));
  assert.ok(screen.getByRole('heading', { level: 1, name: 'Campaigns' }));
});

function EntityPageWithBack() {
  const [open, setOpen] = useState(true);
  useOpenEntity(open ? 'Fictional campaign Dune' : null, () => setOpen(false), 'Back to campaigns');
  return <p>{open ? 'Detail open' : 'List'}</p>;
}

test('an entity with a Back label draws it between the tab strip and the h1, and it closes the entity', () => {
  installMediaQuery();
  render(shell('/campaigns/', {}, <EntityPageWithBack />));
  const back = screen.getByRole('button', { name: 'Back to campaigns' });
  const heading = screen.getByRole('heading', { level: 1, name: 'Fictional campaign Dune' });
  // In the hub header, after any tab strip and right before the h1 (campaign-detail.html).
  assert.equal(back.closest('.hubhead'), heading.closest('.hubhead'));
  assert.equal(back.closest('p')?.nextElementSibling, heading);
  fireEvent.click(back);
  assert.ok(screen.getByText('List'));
  assert.equal(screen.queryByRole('button', { name: 'Back to campaigns' }), null);
});

test('the rail tooltip shows the full label and Escape dismisses it until focus leaves', () => {
  installMediaQuery();
  render(shell());
  const link = screen.getByRole('link', { name: 'Retail health' });
  assert.equal(link.querySelector('.tip')?.textContent, 'Retail health');
  fireEvent.keyDown(link, { key: 'Escape' });
  assert.ok(link.classList.contains('tip-dismissed'));
  fireEvent.blur(link);
  assert.equal(link.classList.contains('tip-dismissed'), false);
});

test('below 881 px the menu button opens a modal sheet with hubs, Theme and Sign out', async () => {
  installMediaQuery({ narrow: true });
  const user = userEvent.setup();
  render(shell('/campaigns/', { can_manage_prompts: true }));
  const menu = screen.getByRole('button', { name: 'Menu' });
  await user.click(menu);
  const sheet = screen.getByRole('dialog', { name: 'Menu' });
  assert.equal(sheet.getAttribute('aria-modal'), 'true');
  assert.equal(document.activeElement, within(sheet).getByRole('button', { name: 'Close menu' }));
  assert.ok(within(sheet).getByText('Performance / Campaigns'));
  const hubs = within(sheet).getByRole('navigation', { name: 'Hubs' });
  // The current hub lists its tabs under it in the sheet.
  assert.deepEqual(within(hubs).getAllByRole('link').map((link) => link.textContent), ['Overview', 'Performance', 'Scorecard', 'Campaigns', 'Reach and audiences', 'Reach and funnel', 'Audiences', 'Customers', 'New customers', 'Retention', 'Retail health', 'Retail health', 'Products and ads', 'Operations']);
  assert.equal(document.getElementById('app-root')?.inert, true);
  await user.click(within(sheet).getByRole('radio', { name: 'Light' }));
  assert.equal(document.documentElement.dataset.theme, 'light');
  assert.ok(within(sheet).getByRole('button', { name: 'Sign out' }));
  await act(async () => { await user.keyboard('{Escape}'); });
  assert.equal(screen.queryByRole('dialog', { name: 'Menu' }), null);
  assert.equal(document.getElementById('app-root')?.inert, false);
  assert.equal(document.activeElement, menu);
});
