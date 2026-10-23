// SPDX-License-Identifier: MIT-0
import generatedManifest from '../generated/route-manifest.json';
import { bindingByRoute, routeNames, type RouteName } from './page-bindings';

/**
 * The shell's navigation, decoded strictly from the generated route manifest (ADR-115). One
 * governed file drives the sidebar, the labelled rail, the slide-out sheet and the tab strips,
 * so no route can appear in navigation without a page behind it.
 */

/** The hubs the icon set draws. A hub outside this set fails decoding until it gets an icon. */
export const hubIds = ['overview', 'performance', 'reach', 'customers', 'retail', 'plan', 'operations'] as const;
export type HubId = typeof hubIds[number];

/** ADR-115 decision 7: the session fields an entitlement can map to. */
export const sessionFields = ['can_manage_prompts', 'can_read_operational_status'] as const;
export type SessionField = typeof sessionFields[number];
/** What the session grants. A field the session doesn't carry yet reads as false. */
export type SessionEntitlements = Readonly<Partial<Record<SessionField, boolean>>>;

/** ADR-151: the composed tabs this frontend renders, each over its sources' served presentations. */
export const composedTabIds = ['overview', 'products-and-ads'] as const;
export type ComposedTabId = typeof composedTabIds[number];

export interface ManifestTab {
  readonly description: string;
  readonly entitlement: { readonly group: string; readonly session_field: SessionField } | null;
  readonly kind: 'composed' | 'page' | 'static';
  readonly label: string;
  readonly order: number;
  /** UI refresh 3.8: the customer question the tab answers, or null. */
  readonly question: string | null;
  readonly route: string;
  readonly tab_id: string;
}

export interface ManifestHub {
  readonly audience: 'customer' | 'operator';
  readonly hub_id: HubId;
  readonly label: string;
  readonly short_label: string;
  readonly tabs: readonly ManifestTab[];
}

export interface RouteManifest {
  readonly hubs: readonly ManifestHub[];
  readonly not_found_outputs: readonly string[];
  readonly root: { readonly route: '/'; readonly tab_id: string };
  readonly version: 1;
}

type UnknownRecord = Record<string, unknown>;

function record(value: unknown, keys: readonly string[], context: string): UnknownRecord {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new TypeError(`${context} must be an object.`);
  }
  const item = value as UnknownRecord;
  const actual = Object.keys(item);
  if (actual.length !== keys.length || !actual.every((key) => keys.includes(key))) {
    throw new TypeError(`${context} has invalid fields.`);
  }
  return item;
}

function text(value: unknown, context: string): string {
  if (typeof value !== 'string' || !value.trim()) throw new TypeError(`${context} must be nonblank text.`);
  return value;
}

function oneOf<const Values extends readonly string[]>(value: unknown, values: Values, context: string): Values[number] {
  const candidate = text(value, context);
  if (!values.includes(candidate)) throw new TypeError(`${context} has an unknown value.`);
  return candidate as Values[number];
}

function route(value: unknown, context: string): string {
  const candidate = text(value, context);
  if (!candidate.startsWith('/') || !candidate.endsWith('/')) {
    throw new TypeError(`${context} must be a canonical slash-delimited path.`);
  }
  return candidate;
}

function parseTab(value: unknown, audience: ManifestHub['audience']): ManifestTab {
  const tab = record(value, ['description', 'entitlement', 'kind', 'label', 'order', 'question', 'route', 'tab_id'], 'manifest tab');
  const kind = oneOf(tab.kind, ['composed', 'page', 'static'] as const, 'manifest tab kind');
  let entitlement: ManifestTab['entitlement'] = null;
  if (tab.entitlement !== null) {
    const raw = record(tab.entitlement, ['group', 'session_field'], 'manifest tab entitlement');
    entitlement = {
      group: text(raw.group, 'manifest entitlement group'),
      session_field: oneOf(raw.session_field, sessionFields, 'manifest entitlement session field'),
    };
  }
  // An operator surface is always entitled; a customer tab is a presentation page this
  // frontend renders, at the route its binding declares.
  if ((audience === 'operator') !== (entitlement !== null)) {
    throw new TypeError('Manifest tab entitlement does not match its hub audience.');
  }
  const tabId = text(tab.tab_id, 'manifest tab id');
  const tabRoute = route(tab.route, 'manifest tab route');
  if (kind === 'page') {
    if (!(routeNames as readonly string[]).includes(tabId)) {
      throw new TypeError('Manifest page tab is not a rendered page.');
    }
    if (bindingByRoute[tabId as RouteName].route !== tabRoute) {
      throw new TypeError('Manifest page tab route does not match its page binding.');
    }
  }
  if (kind === 'composed' && !(composedTabIds as readonly string[]).includes(tabId)) {
    throw new TypeError('Manifest composed tab is not a rendered view.');
  }
  if (!Number.isSafeInteger(tab.order) || (tab.order as number) < 1) {
    throw new TypeError('Manifest tab order must be a positive integer.');
  }
  return {
    description: text(tab.description, 'manifest tab description'),
    entitlement,
    kind,
    label: text(tab.label, 'manifest tab label'),
    order: tab.order as number,
    question: tab.question === null ? null : text(tab.question, 'manifest tab question'),
    route: tabRoute,
    tab_id: tabId,
  };
}

export function parseRouteManifest(value: unknown): RouteManifest {
  const document = record(value, ['hubs', 'not_found_outputs', 'root', 'version'], 'route manifest');
  if (document.version !== 1) throw new TypeError('Route manifest version is invalid.');
  if (!Array.isArray(document.hubs) || document.hubs.length === 0) throw new TypeError('Route manifest hubs are invalid.');
  const hubs = document.hubs.map((value): ManifestHub => {
    const hub = record(value, ['audience', 'hub_id', 'label', 'short_label', 'tabs'], 'manifest hub');
    const audience = oneOf(hub.audience, ['customer', 'operator'] as const, 'manifest hub audience');
    if (!Array.isArray(hub.tabs) || hub.tabs.length === 0) throw new TypeError('Manifest hub tabs are invalid.');
    return {
      audience,
      hub_id: oneOf(hub.hub_id, hubIds, 'manifest hub id'),
      label: text(hub.label, 'manifest hub label'),
      short_label: text(hub.short_label, 'manifest hub short label'),
      tabs: hub.tabs.map((tab) => parseTab(tab, audience)),
    };
  });
  const tabs = hubs.flatMap((hub) => hub.tabs);
  if (new Set(hubs.map((hub) => hub.hub_id)).size !== hubs.length
    || new Set(tabs.map((tab) => tab.tab_id)).size !== tabs.length
    || new Set(tabs.map((tab) => tab.route)).size !== tabs.length) {
    throw new TypeError('Route manifest hubs, tabs and routes must be unique.');
  }
  const root = record(document.root, ['route', 'tab_id'], 'route manifest root');
  if (root.route !== '/' || !tabs.some((tab) => tab.tab_id === root.tab_id)) {
    throw new TypeError('Route manifest root must name a listed tab.');
  }
  if (!Array.isArray(document.not_found_outputs)) throw new TypeError('Route manifest not-found outputs are invalid.');
  return {
    hubs,
    not_found_outputs: document.not_found_outputs.map((item) => text(item, 'not-found output')),
    root: { route: '/', tab_id: root.tab_id as string },
    version: 1,
  };
}

export const routeManifest = parseRouteManifest(generatedManifest as unknown);

/** The hubs and tabs this session can see; a hub with no visible tab isn't listed. */
export function visibleHubs(
  entitlements: SessionEntitlements,
  manifest: RouteManifest = routeManifest,
): readonly ManifestHub[] {
  return manifest.hubs
    .map((hub) => ({
      ...hub,
      tabs: hub.tabs.filter((tab) => tab.entitlement === null || entitlements[tab.entitlement.session_field] === true),
    }))
    .filter((hub) => hub.tabs.length > 0);
}

export interface RouteLocation {
  readonly hub: ManifestHub;
  readonly tab: ManifestTab;
}

const INDEX_DOCUMENT = 'index.html';

/**
 * The route a pathname names. The edge passes a dotted leaf through to its object (ADR-123), so an
 * exported `<route>index.html` serves that route's page and must locate as the route itself.
 */
function normalized(pathname: string): string {
  const path = pathname.endsWith(`/${INDEX_DOCUMENT}`) ? pathname.slice(0, -INDEX_DOCUMENT.length) : pathname;
  return path.endsWith('/') ? path : `${path}/`;
}

/** The hub and tab a pathname is, from the full manifest; the root resolves to its owner tab. */
export function locateRoute(pathname: string, manifest: RouteManifest = routeManifest): RouteLocation | null {
  const path = normalized(pathname);
  const tabId = path === manifest.root.route ? manifest.root.tab_id : null;
  for (const hub of manifest.hubs) {
    const tab = hub.tabs.find((candidate) => (tabId === null ? candidate.route === path : candidate.tab_id === tabId));
    if (tab !== undefined) return { hub, tab };
  }
  return null;
}

/** The tab the root route serves, for "Go to {tab}" and the brand link's name. */
export function rootTab(manifest: RouteManifest = routeManifest): ManifestTab {
  const location = locateRoute(manifest.root.route, manifest);
  if (location === null) throw new TypeError('Route manifest root must name a listed tab.');
  return location.tab;
}

/** The crumb, h1 and title of a path that isn't in the manifest: the not-found page (ADR-123). */
export const notFoundLabel = 'Page not found';

/**
 * "{tab} · {hub} · Adlytics AI", or "{hub} · Adlytics AI" when the tab is the hub (ia §3.5); a path
 * outside the manifest is "Page not found · Adlytics AI".
 */
export function documentTitle(location: RouteLocation | null, entity: string | null = null): string {
  if (location === null) return `${notFoundLabel} · Adlytics AI`;
  const parts = [
    ...(entity === null ? [] : [entity]),
    ...(location.tab.label === location.hub.label ? [] : [location.tab.label]),
    location.hub.label,
    'Adlytics AI',
  ];
  return parts.join(' · ');
}
