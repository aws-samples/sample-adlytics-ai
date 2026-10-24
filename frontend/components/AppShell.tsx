// SPDX-License-Identifier: MIT-0
'use client';

import Link from 'next/link';
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
  type CSSProperties,
  type KeyboardEvent,
  type MouseEvent,
  type ReactNode,
  type RefObject,
} from 'react';

import {
  documentTitle,
  locateRoute,
  notFoundLabel,
  rootTab,
  visibleHubs,
  type ManifestHub,
  type RouteLocation,
  type SessionEntitlements,
} from '@/lib/route-manifest';
import { beginLogin, signOut } from '@/lib/session';
import { BrandMark } from './BrandMark';
import { AssistantPagePublisher, type AssistantPage } from './AssistantPageContext';
import { ChatPanel, toggleAssistant, type AssistantPlacement } from './ChatPanel';
import { APP_ROOT_ID, Dialog, PAGE_HEADING_ID } from './Dialog';
import { Icon } from './Icon';
import { useMediaQuery } from './useMediaQuery';
import { VisibleTabsContext } from './NextStepContext';

const NO_PAGE_CONTEXT: AssistantPage = { kind: 'none' };
const THEME_KEY = 'adlytics.theme';
const THEME_MENU_ID = 'theme-preference-menu';
const THEME_OPTIONS = ['system', 'light', 'dark'] as const;
/** The owner's slide-out menu breakpoint (owner UX decision 1): the sheet below 881 px. */
const NARROW_QUERY = '(max-width: 880px)';
const MENU_SHEET_ID = 'menu-sheet';
const ASSISTANT_TOGGLE_ID = 'assistant-toggle';

type ThemePreference = typeof THEME_OPTIONS[number];
type EffectiveTheme = 'dark' | 'light';

const subscribeNever = () => () => {};
/**
 * False on the server and while hydrating a prerendered shell, true after. The not-found page is
 * the one shell that's prerendered (ADR-123), and its stored theme is only known in the browser,
 * so the theme label reads the stored value only once hydration is done.
 */
function useHydrated(): boolean {
  return useSyncExternalStore(subscribeNever, () => true, () => false);
}

function storedTheme(): ThemePreference {
  if (typeof window === 'undefined') return 'system';
  try {
    const value = localStorage.getItem(THEME_KEY);
    return THEME_OPTIONS.includes(value as ThemePreference)
      ? value as ThemePreference
      : 'system';
  } catch {
    return 'system';
  }
}

function systemTheme(): EffectiveTheme {
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

function themeLabel(theme: ThemePreference): string {
  return theme.charAt(0).toUpperCase() + theme.slice(1);
}

interface OpenEntity {
  /** The page's own "Back to …" control, drawn above the h1 (ia §7.2 item 10). */
  readonly backLabel: string | null;
  readonly name: string;
  readonly onBack: (() => void) | null;
}

const EntityContext = createContext<((entity: OpenEntity | null) => void) | null>(null);

/**
 * Publish the entity open in place on this tab (a campaign) while mounted. It becomes the h1
 * and the crumb's last item; the crumb's tab link calls onBack to close it (ia §3.5). With a
 * `backLabel`, the hub header also draws that Back control between the tab strip and the h1.
 */
export function useOpenEntity(name: string | null, onBack?: (() => void) | null, backLabel: string | null = null): void {
  const publish = useContext(EntityContext);
  useEffect(() => {
    if (publish === null) return;
    publish(name === null ? null : { backLabel, name, onBack: onBack ?? null });
    return () => publish(null);
  }, [backLabel, name, onBack, publish]);
}

/** The hub a nav item opens: its first tab this session can see (ADR-115 decision 2). */
function hubTarget(hub: ManifestHub): string {
  return hub.tabs[0]?.route ?? '/';
}

/** One visible tab: the nav item is the page. Several: it is the current hub, not a page. */
function hubCurrent(hub: ManifestHub, location: RouteLocation | null): 'page' | 'true' | undefined {
  if (location === null || location.hub.hub_id !== hub.hub_id) return undefined;
  return hub.tabs.length > 1 ? 'true' : 'page';
}

function HubLink({ hub, location }: { readonly hub: ManifestHub; readonly location: RouteLocation | null }) {
  // WCAG 1.4.13: the rail tooltip shows on hover and focus, stays while hovered, and Escape
  // dismisses it until the pointer or focus leaves.
  const [tipDismissed, setTipDismissed] = useState(false);
  return (
    <li>
      <Link
        className={tipDismissed ? 'hub tip-dismissed' : 'hub'}
        href={hubTarget(hub)}
        aria-current={hubCurrent(hub, location)}
        aria-label={hub.label}
        onKeyDown={(event) => { if (event.key === 'Escape') setTipDismissed(true); }}
        onBlur={() => setTipDismissed(false)}
        onMouseLeave={() => setTipDismissed(false)}
      >
        <Icon name={hub.hub_id} />
        <span className="lbl" aria-hidden="true">{hub.label}</span>
        <span className="short" aria-hidden="true">{hub.short_label}</span>
        <span className="tip" aria-hidden="true">{hub.label}</span>
      </Link>
    </li>
  );
}

function HubNavigation({ hubs, location }: { readonly hubs: readonly ManifestHub[]; readonly location: RouteLocation | null }) {
  const customer = hubs.filter((hub) => hub.audience === 'customer');
  const operator = hubs.filter((hub) => hub.audience === 'operator');
  return (
    <nav className="hubnav" aria-label="Hubs">
      <Link className="brand" href="/" aria-label={`Adlytics AI, go to ${rootTab().label}`}>
        <BrandMark className="logo" gradientId="brand-gradient" />
        <span className="word" aria-hidden="true">Adlytics AI</span>
      </Link>
      <ul>{customer.map((hub) => <HubLink hub={hub} key={hub.hub_id} location={location} />)}</ul>
      {operator.length === 0 ? null : <>
        <p className="grp-label" aria-hidden="true">Operators</p>
        <ul aria-label="Operators">{operator.map((hub) => <HubLink hub={hub} key={hub.hub_id} location={location} />)}</ul>
      </>}
    </nav>
  );
}

/**
 * The page's place in the manifest. `visible` is null when this session can't see the route
 * (a direct visit without the entitlement): then nothing in navigation is current, and the
 * crumb and h1 name only the page (ia §3.5, §6.5).
 */
interface PagePlace {
  readonly found: RouteLocation | null;
  readonly visible: RouteLocation | null;
}

function crumbItems({ found, visible }: PagePlace, entity: OpenEntity | null): ReadonlyArray<{
  readonly label: string;
  readonly href: string | null;
  readonly onClick: (() => void) | null;
}> {
  if (found === null) return [];
  if (visible === null) return [{ href: null, label: found.tab.label, onClick: null }];
  const { hub, tab } = visible;
  const items: { label: string; href: string | null; onClick: (() => void) | null }[] = [];
  if (tab.label !== hub.label) items.push({ href: hubTarget(hub), label: hub.label, onClick: null });
  items.push({ href: tab.route, label: tab.label, onClick: entity?.onBack ?? null });
  if (entity !== null) items.push({ href: null, label: entity.name, onClick: null });
  return items.map((item, index) => (index === items.length - 1 ? { ...item, href: null, onClick: null } : item));
}

function Crumb({ place, entity }: { readonly place: PagePlace; readonly entity: OpenEntity | null }) {
  const items = place.found === null
    ? [{ href: null, label: notFoundLabel, onClick: null }]
    : crumbItems(place, entity);
  return (
    <nav className="crumb" aria-label="Breadcrumb">
      <ol>
        {items.map((item) => (
          <li key={`${item.label}:${item.href ?? 'here'}`}>
            {item.href === null
              ? <span aria-current="page" title={item.label}>{item.label}</span>
              : <Link
                  href={item.href}
                  {...(item.onClick === null ? {} : {
                    onClick: (event: MouseEvent<HTMLAnchorElement>) => {
                      event.preventDefault();
                      item.onClick?.();
                    },
                  })}
                >{item.label}</Link>}
          </li>
        ))}
      </ol>
    </nav>
  );
}

/**
 * The hub header (ia §3.5): the eyebrow and tab strip when the hub shows more than one tab,
 * then the h1 (the tab name, or the entity open in place) and the tab's description.
 */
function HubHeader({ place, entity }: { readonly place: PagePlace; readonly entity: OpenEntity | null }) {
  if (place.found === null) return null;
  const { tab } = place.found;
  const hub = place.visible?.hub ?? null;
  const multi = hub !== null && hub.tabs.length > 1;
  return (
    <div className="hubhead">
      {multi && hub !== null ? <p className="hub-eyebrow">{hub.label}</p> : null}
      {multi && hub !== null ? (
        <nav className="tabs" aria-label={`Sections of ${hub.label}`}>
          <ul>{hub.tabs.map((candidate) => (
            <li key={candidate.tab_id}>
              <Link href={candidate.route} aria-current={candidate.tab_id === tab.tab_id ? 'page' : undefined}>
                {candidate.label}
                {/* UI refresh 3.8: each tab names the question it answers (readout persona framing). */}
                {/* Hidden from assistive tech so the link's name stays the tab label; the open tab reads it as its lead line. */}
                {candidate.question === null ? null : <span className="q" aria-hidden="true">{candidate.question}</span>}
              </Link>
            </li>
          ))}</ul>
        </nav>
      ) : null}
      {entity?.backLabel == null || entity.onBack === null ? null : (
        <p className="hub-back">
          <button type="button" className="btn link" onClick={entity.onBack}><Icon name="back" small />{entity.backLabel}</button>
        </p>
      )}
      <h1 id={PAGE_HEADING_ID} tabIndex={-1}>{entity?.name ?? tab.label}</h1>
      {/* The tab's question is the page's lead line when it has one; otherwise its description. */}
      {entity === null ? <p className={tab.question === null ? 'desc' : 'desc lead-q'}>{tab.question ?? tab.description}</p> : null}
    </div>
  );
}

function ThemeMenu({ theme, onSelect }: { readonly theme: ThemePreference; readonly onSelect: (theme: ThemePreference) => void }) {
  const [open, setOpen] = useState(false);
  const hydrated = useHydrated();
  const trigger = useRef<HTMLButtonElement>(null);
  const optionButtons = useRef<Array<HTMLButtonElement | null>>([]);
  useEffect(() => {
    if (!open) return;
    optionButtons.current[THEME_OPTIONS.indexOf(theme)]?.focus();
  }, [open, theme]);
  const select = (next: ThemePreference) => {
    setOpen(false);
    onSelect(next);
    trigger.current?.focus();
  };
  const keydown = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    let nextIndex: number | null = null;
    if (event.key === 'ArrowDown' || event.key === 'ArrowRight') nextIndex = (index + 1) % THEME_OPTIONS.length;
    else if (event.key === 'ArrowUp' || event.key === 'ArrowLeft') nextIndex = (index + THEME_OPTIONS.length - 1) % THEME_OPTIONS.length;
    else if (event.key === 'Home') nextIndex = 0;
    else if (event.key === 'End') nextIndex = THEME_OPTIONS.length - 1;
    else if (event.key === 'Escape') {
      event.preventDefault();
      setOpen(false);
      trigger.current?.focus();
      return;
    } else if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      select(THEME_OPTIONS[index]!);
      return;
    }
    if (nextIndex !== null) {
      event.preventDefault();
      optionButtons.current[nextIndex]?.focus();
    }
  };
  return (
    <div className="theme-control only-wide">
      <button
        ref={trigger}
        className="tb-btn"
        type="button"
        aria-controls={THEME_MENU_ID}
        aria-expanded={open}
        aria-haspopup="menu"
        onClick={() => setOpen((value) => !value)}
      >
        <Icon name="theme" />Theme: {themeLabel(hydrated ? theme : 'system')}
      </button>
      {open && (
        <div id={THEME_MENU_ID} className="theme-menu" role="menu" aria-label="Theme preference">
          {THEME_OPTIONS.map((option, index) => (
            <button
              key={option}
              ref={(element) => { optionButtons.current[index] = element; }}
              type="button"
              role="menuitemradio"
              aria-checked={theme === option}
              onClick={() => select(option)}
              onKeyDown={(event) => keydown(event, index)}
            >
              {themeLabel(option)}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

/** The slide-out menu below 881 px: hubs with their tabs, then Theme and Sign out (ia §3.4). */
function MenuSheet({
  entity,
  hubs,
  place,
  onClose,
  onSignOut,
  onTheme,
  opener,
  signingOut,
  theme,
}: {
  readonly entity: OpenEntity | null;
  readonly hubs: readonly ManifestHub[];
  readonly place: PagePlace;
  readonly onClose: () => void;
  /** Null when no session is signed in: the sheet offers Sign in instead (ADR-123 decision 2). */
  readonly onSignOut: (() => void) | null;
  readonly onTheme: (theme: ThemePreference) => void;
  readonly opener: RefObject<HTMLButtonElement | null>;
  readonly signingOut: boolean;
  readonly theme: ThemePreference;
}) {
  const path = crumbItems(place, entity).map((item) => item.label).join(' / ');
  const location = place.visible;
  return (
    <Dialog
      variant="sheet"
      label="Menu"
      closeLabel="Close menu"
      onClose={onClose}
      opener={opener}
      header={<span className="brand"><BrandMark className="logo" gradientId="brand-gradient-sheet" /><span className="word">Adlytics AI</span></span>}
    >
      <div id={MENU_SHEET_ID} className="sheet-nav">
        {path === '' ? null : <p className="path">{path}</p>}
        <nav aria-label="Hubs">
          <ul>{hubs.map((hub) => (
            <li key={hub.hub_id}>
              <Link href={hubTarget(hub)} aria-current={hubCurrent(hub, location)} onClick={onClose}>
                <Icon name={hub.hub_id} />{hub.label}
              </Link>
              {hub.tabs.length > 1 ? (
                <ul>{hub.tabs.map((tab) => (
                  <li key={tab.tab_id}>
                    <Link href={tab.route} aria-current={location?.tab.tab_id === tab.tab_id ? 'page' : undefined} onClick={onClose}>{tab.label}</Link>
                  </li>
                ))}</ul>
              ) : null}
            </li>
          ))}</ul>
        </nav>
        <div className="sheet-foot">
          <fieldset className="theme-choice">
            <legend><Icon name="theme" small />Theme</legend>
            {THEME_OPTIONS.map((option) => (
              <label key={option}>
                <input type="radio" name="sheet-theme" value={option} checked={theme === option} onChange={() => onTheme(option)} />
                {themeLabel(option)}
              </label>
            ))}
          </fieldset>
          {onSignOut === null
            ? <button type="button" className="btn" onClick={beginLogin}>Sign in</button>
            : <button type="button" className="btn" disabled={signingOut} onClick={onSignOut}>
                <Icon name="signout" />{signingOut ? 'Signing out' : 'Sign out'}
              </button>}
        </div>
      </div>
    </Dialog>
  );
}

export function AppShell({
  pathname,
  sessionId,
  entitlements = {},
  onSessionEnded,
  children,
}: {
  readonly pathname: string;
  /**
   * Null when no session is signed in, which only the not-found page renders the shell for
   * (ADR-123 decision 2), briefly before the sign-in redirect or while session status is
   * unavailable: no assistant, no Assistant toggle and no Sign out; the top bar offers Sign in,
   * and navigation lists customer hubs only.
   */
  readonly sessionId: string | null;
  readonly entitlements?: SessionEntitlements;
  readonly onSessionEnded?: () => void;
  readonly children: ReactNode;
}) {
  const [theme, setTheme] = useState<ThemePreference>(storedTheme);
  const [systemEffectiveTheme, setSystemEffectiveTheme] = useState<EffectiveTheme>(
    () => typeof window === 'undefined' ? 'light' : systemTheme(),
  );
  const [announcement, setAnnouncement] = useState('');
  const [signingOut, setSigningOut] = useState(false);
  const [assistant, setAssistant] = useState<AssistantPlacement>({ open: true, width: 360 });
  const [entity, setEntity] = useState<OpenEntity | null>(null);
  // ADR-120: what the page on screen offers the assistant; a route that publishes nothing (an
  // operator route) has no page context.
  const [assistantPage, setAssistantPage] = useState<AssistantPage | null>(null);
  const [sheetOpen, setSheetOpen] = useState(false);
  const menuButton = useRef<HTMLButtonElement>(null);
  const narrow = useMediaQuery(NARROW_QUERY);
  const hubs = useMemo(() => visibleHubs(entitlements), [entitlements]);
  const visibleTabs = useMemo(() => hubs.flatMap((hub) => hub.tabs), [hubs]);
  // The visited route as this session sees it: a route outside its visible hubs marks nothing.
  const place = useMemo((): PagePlace => {
    const found = locateRoute(pathname);
    if (found === null) return { found, visible: null };
    const hub = hubs.find((candidate) => candidate.hub_id === found.hub.hub_id);
    const tab = hub?.tabs.find((candidate) => candidate.tab_id === found.tab.tab_id);
    return { found, visible: hub === undefined || tab === undefined ? null : { hub, tab } };
  }, [hubs, pathname]);
  const location = place.visible;

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    if (theme !== 'system') return;
    const query = window.matchMedia('(prefers-color-scheme: dark)');
    const update = () => setSystemEffectiveTheme(query.matches ? 'dark' : 'light');
    if (typeof query.addEventListener === 'function') {
      query.addEventListener('change', update);
      return () => query.removeEventListener('change', update);
    }
    query.addListener(update);
    return () => query.removeListener(update);
  }, [theme]);

  // ADR-118 decision 3: a route change moves focus to the h1, the tab name.
  useLayoutEffect(() => {
    document.getElementById(PAGE_HEADING_ID)?.focus({ preventScroll: true });
  }, [pathname]);

  useEffect(() => {
    document.title = documentTitle(place.found, entity?.name ?? null);
  }, [entity, place.found]);

  // The sheet exists only below 881 px; widening the window closes it.
  const [sheetNarrow, setSheetNarrow] = useState(narrow);
  if (sheetNarrow !== narrow) {
    setSheetNarrow(narrow);
    if (!narrow) setSheetOpen(false);
  }

  const publishEntity = useCallback((next: OpenEntity | null) => setEntity(next), []);
  const publishAssistantPage = useCallback((next: AssistantPage | null) => setAssistantPage(next), []);

  const selectTheme = (next: ThemePreference) => {
    setTheme(next);
    try {
      localStorage.setItem(THEME_KEY, next);
    } catch {
      // The in-memory preference still applies when browser storage is unavailable.
    }
    const effective = next === 'system' ? systemEffectiveTheme : next;
    setAnnouncement(`Theme set to ${themeLabel(next)}, using ${effective} mode.`);
  };

  const endSession = async () => {
    setSigningOut(true);
    try {
      onSessionEnded?.();
      await signOut();
    } finally {
      setSigningOut(false);
    }
  };

  const signedIn = sessionId !== null;

  return (
    <EntityContext.Provider value={publishEntity}>
      <AssistantPagePublisher.Provider value={publishAssistantPage}>
      <div
        id={APP_ROOT_ID}
        className="app"
        // Signed out there is no assistant column and no inline style, so the prerendered
        // not-found page needs no style-src allowance (CSP in FrontendStack).
        data-assistant={signedIn ? (assistant.open ? 'open' : 'closed') : 'absent'}
        {...(signedIn
          ? { style: { '--asst-w': `${assistant.open ? assistant.width : 0}px` } as CSSProperties }
          : {})}
      >
        <header className="topbar">
          <div className="skips">
            <a href="#main-content">Skip to main content</a>
            {signedIn && assistant.open ? <a href="#chat-question">Skip to assistant</a> : null}
          </div>
          <button
            ref={menuButton}
            className="tb-btn only-narrow"
            type="button"
            aria-label="Menu"
            aria-haspopup="dialog"
            aria-expanded={sheetOpen}
            aria-controls={MENU_SHEET_ID}
            onClick={() => setSheetOpen(true)}
          >
            <Icon name="menu" />
          </button>
          <Crumb place={place} entity={entity} />
          <span className="spacer" />
          {signedIn ? (
            <button
              id={ASSISTANT_TOGGLE_ID}
              className="tb-btn"
              type="button"
              aria-controls="assistant"
              aria-expanded={assistant.open}
              aria-label="Assistant"
              onClick={toggleAssistant}
            >
              <Icon name="assistant" /><span className="btn-text" aria-hidden="true">Assistant</span>
            </button>
          ) : (
            <button className="tb-btn" type="button" onClick={beginLogin}>Sign in</button>
          )}
          <ThemeMenu theme={theme} onSelect={selectTheme} />
          {signedIn ? (
            <button className="tb-btn only-wide" type="button" disabled={signingOut} onClick={() => void endSession()}>
              <Icon name="signout" /><span className="btn-text">{signingOut ? 'Signing out' : 'Sign out'}</span>
            </button>
          ) : null}
        </header>
        <HubNavigation hubs={hubs} location={location} />
        <main id="main-content" className="main" tabIndex={-1}>
          <div className="content">
            <HubHeader place={place} entity={entity} />
            <VisibleTabsContext.Provider value={visibleTabs}>{children}</VisibleTabsContext.Provider>
          </div>
        </main>
        {signedIn ? (
          <ChatPanel
            narrow={narrow}
            onPlacementChange={setAssistant}
            // The not-found page shows no chip (ia §8.8); every other route shows its page's.
            page={place.found === null ? null : assistantPage ?? NO_PAGE_CONTEXT}
            sessionId={sessionId}
            toggleId={ASSISTANT_TOGGLE_ID}
          />
        ) : null}
      </div>
      {sheetOpen && narrow ? (
        <MenuSheet
          entity={entity}
          hubs={hubs}
          place={place}
          onClose={() => setSheetOpen(false)}
          onSignOut={signedIn ? () => void endSession() : null}
          onTheme={selectTheme}
          opener={menuButton}
          signingOut={signingOut}
          theme={theme}
        />
      ) : null}
      <span className="sr-only" role="status" aria-live="polite">{announcement}</span>
      </AssistantPagePublisher.Provider>
    </EntityContext.Provider>
  );
}
