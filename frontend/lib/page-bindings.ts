// SPDX-License-Identifier: MIT-0
import generatedDocument from '../generated/page-bindings.json';

/**
 * Every page the backend presentation contract declares, in its generated order.
 *
 * The generated binding document is a projection of the backend contract and lists all of
 * these. The frontend renders only the subset in `routeNames`; the rest remain declared
 * (their APIs stay deployed) but have no route, no navigation entry, and no view.
 */
export const declaredPageIds = [
  'campaigns',
  'channel-performance',
  'workspace',
  'data-health',
  'media-efficiency',
  'retail-health',
  'scorecard',
  'new-customers',
  'reach',
  'retention',
  'audiences',
] as const;

export type DeclaredPageId = typeof declaredPageIds[number];

/** The pages this frontend renders. The command center is the whole workspace for now. */
export const routeNames = ['campaigns', 'retail-health', 'scorecard', 'new-customers', 'reach', 'retention', 'audiences'] as const;

export type RouteName = typeof routeNames[number];
export type CanonicalRoute =
  | '/campaigns/'
  | '/channel-performance/'
  | '/workspace/'
  | '/data-health/'
  | '/media-efficiency/'
  | '/retail-health/'
  | '/scorecard/'
  | '/new-customers/'
  | '/reach/'
  | '/retention/'
  | '/audiences/';
export type RenderedRoute = '/campaigns/' | '/retail-health/' | '/scorecard/' | '/new-customers/' | '/reach/' | '/retention/' | '/audiences/';
export type CompatibilityRoute = '/';
export type PageMode = 'selectable' | 'selectable_parent_child';
export type Cardinality = 'exactly_one' | 'zero_or_one' | 'one_or_more' | 'zero_or_more';
export type SectionRole =
  | 'availability'
  | 'boundary'
  | 'evidence'
  | 'selection'
  | 'scope'
  | 'summary'
  | 'table'
  | 'trend';
/** The rolling presets, the only ones a version-1 control carries. */
export type RollingPresetId = '1d' | '7d' | '14d' | '30d';
/** Calendar presets (ADR-100 point 4); a page declaring one needs temporal control version 2. */
export type CalendarPresetId = 'last_complete_week' | 'last_complete_month' | 'last_complete_quarter';
export type TemporalPresetId = RollingPresetId | CalendarPresetId;
export type TemporalComparisonPolicy =
  | 'required_equal_prior'
  | 'optional_equal_prior'
  | 'prior_calendar_period'
  | 'none';
export type DisplayGrain = 'period' | 'day' | 'declared_period';

export interface PageBindingBase {
  readonly actions: readonly {
    readonly action_id: string;
    readonly target_route: CanonicalRoute;
  }[];
  readonly artifacts: readonly {
    readonly artifact_id: string;
    readonly outcomes: readonly string[];
    readonly type: string;
    readonly version: number;
  }[];
  readonly boundary_required: boolean;
  readonly compatibility_routes: readonly CompatibilityRoute[];
  readonly display_name: string;
  readonly mode: PageMode;
  readonly page_id: DeclaredPageId;
  readonly primary_job: string;
  readonly purpose: string;
  readonly route: CanonicalRoute;
  readonly sections: readonly {
    readonly cardinality: Cardinality;
    readonly role: SectionRole;
    readonly section_id: string;
  }[];
  readonly visuals: readonly {
    readonly metric_cohort: readonly string[];
    readonly narration_eligible: boolean;
    readonly visual_id: string;
  }[];
}

export interface PresetPolicyBinding {
  readonly candidate_presets: readonly TemporalPresetId[];
  readonly comparison_intent: 'compare_previous' | null;
  readonly comparison_policy: TemporalComparisonPolicy;
  readonly custom: {
    readonly enabled: boolean;
    readonly max_duration_days: number;
    readonly min_duration_days: number;
  };
  readonly default_preset: TemporalPresetId;
  readonly display_grains: readonly DisplayGrain[];
  readonly preset_exceptions: readonly {
    readonly availability: 'unavailable';
    readonly preset_id: TemporalPresetId;
    readonly reason_code: string;
  }[];
}

export interface VintagePolicyBinding {
  readonly comparison: 'none';
  readonly default: 'latest_with_current_publication';
  readonly selector: 'as_of_month';
}

export type PresetPageBinding = PageBindingBase & {
  readonly temporal_policy: PresetPolicyBinding;
  readonly vintage_policy?: never;
};
export type VintagePageBinding = PageBindingBase & {
  readonly temporal_policy?: never;
  readonly vintage_policy: VintagePolicyBinding;
};
export type PageBinding = PresetPageBinding | VintagePageBinding;

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

function array(value: unknown, context: string): readonly unknown[] {
  if (!Array.isArray(value)) throw new TypeError(`${context} must be an array.`);
  return value;
}

function nonblank(value: unknown, context: string): string {
  if (typeof value !== 'string' || !value.trim()) {
    throw new TypeError(`${context} must be nonblank text.`);
  }
  return value;
}

function enumValue<const Values extends readonly string[]>(
  value: unknown,
  values: Values,
  context: string,
): Values[number] {
  const candidate = nonblank(value, context);
  if (!values.some((allowed) => allowed === candidate)) {
    throw new TypeError(`${context} has an unknown value.`);
  }
  return candidate as Values[number];
}

function positiveInteger(value: unknown, context: string): number {
  if (!Number.isSafeInteger(value) || (value as number) < 1) {
    throw new TypeError(`${context} must be a positive integer.`);
  }
  return value as number;
}

function unique(values: readonly string[], context: string): void {
  if (new Set(values).size !== values.length) throw new TypeError(`${context} must be unique.`);
}

const canonicalRoutes = [
  '/campaigns/',
  '/channel-performance/',
  '/workspace/',
  '/data-health/',
  '/media-efficiency/',
  '/retail-health/',
  '/scorecard/',
  '/new-customers/',
  '/reach/',
  '/retention/',
  '/audiences/',
] as const;
const compatibilityRoutes = ['/'] as const;
const pageModes = ['selectable', 'selectable_parent_child'] as const;
const cardinalities = ['exactly_one', 'zero_or_one', 'one_or_more', 'zero_or_more'] as const;
const sectionRoles = [
  'availability',
  'boundary',
  'evidence',
  'selection',
  'scope',
  'summary',
  'table',
  'trend',
] as const;
/** Canonical preset order: rolling, then calendar (ADR-100 point 8). */
export const presetIds = [
  '1d',
  '7d',
  '14d',
  '30d',
  'last_complete_week',
  'last_complete_month',
  'last_complete_quarter',
] as const satisfies readonly TemporalPresetId[];
const calendarPresetIds: readonly string[] = ['last_complete_week', 'last_complete_month', 'last_complete_quarter'];

/** Whether a preset resolves to a complete calendar period rather than rolling days. */
export function isCalendarPreset(preset: TemporalPresetId): preset is CalendarPresetId {
  return calendarPresetIds.includes(preset);
}
const comparisonPolicies = [
  'required_equal_prior',
  'optional_equal_prior',
  'prior_calendar_period',
  'none',
] as const;
const displayGrains = ['period', 'day', 'declared_period'] as const;

const declaredPaths: Record<DeclaredPageId, CanonicalRoute> = {
  campaigns: '/campaigns/',
  'channel-performance': '/channel-performance/',
  workspace: '/workspace/',
  'data-health': '/data-health/',
  'media-efficiency': '/media-efficiency/',
  'retail-health': '/retail-health/',
  scorecard: '/scorecard/',
  'new-customers': '/new-customers/',
  reach: '/reach/',
  retention: '/retention/',
  audiences: '/audiences/',
};

function parseTemporalPolicy(value: unknown): PageBinding['temporal_policy'] {
  const policy = record(
    value,
    [
      'candidate_presets',
      'comparison_intent',
      'comparison_policy',
      'custom',
      'default_preset',
      'display_grains',
      'preset_exceptions',
    ],
    'temporal policy',
  );
  const candidate_presets = array(policy.candidate_presets, 'temporal candidate presets').map((preset) =>
    enumValue(preset, presetIds, 'temporal candidate preset')
  );
  unique(candidate_presets, 'Temporal candidate presets');
  if (candidate_presets.length === 0) throw new TypeError('Temporal candidate presets must not be empty.');
  const default_preset = enumValue(policy.default_preset, presetIds, 'temporal default preset');
  if (!candidate_presets.includes(default_preset)) {
    throw new TypeError('Temporal default preset must be a candidate preset.');
  }
  const comparison_policy = enumValue(
    policy.comparison_policy,
    comparisonPolicies,
    'temporal comparison policy',
  );
  const comparison_intent = policy.comparison_intent === null
    ? null
    : enumValue(policy.comparison_intent, ['compare_previous'] as const, 'temporal comparison intent');
  if (comparison_intent !== null && comparison_policy !== 'optional_equal_prior') {
    throw new TypeError('Temporal comparison intent requires optional equal-prior comparison.');
  }
  const custom = record(
    policy.custom,
    ['enabled', 'max_duration_days', 'min_duration_days'],
    'temporal Custom policy',
  );
  if (typeof custom.enabled !== 'boolean') throw new TypeError('Temporal Custom policy enabled must be boolean.');
  const min_duration_days = positiveInteger(custom.min_duration_days, 'temporal Custom minimum duration');
  const max_duration_days = positiveInteger(custom.max_duration_days, 'temporal Custom maximum duration');
  if (max_duration_days < min_duration_days || max_duration_days > 90) {
    throw new TypeError('Temporal Custom duration bounds are invalid.');
  }
  const parsedDisplayGrains = array(policy.display_grains, 'temporal display grains').map((grain) =>
    enumValue(grain, displayGrains, 'temporal display grain')
  );
  unique(parsedDisplayGrains, 'Temporal display grains');
  if (parsedDisplayGrains.length === 0) throw new TypeError('Temporal display grains must not be empty.');
  const preset_exceptions = array(policy.preset_exceptions, 'temporal preset exceptions').map((value) => {
    const exception = record(value, ['availability', 'preset_id', 'reason_code'], 'temporal preset exception');
    if (exception.availability !== 'unavailable') {
      throw new TypeError('Temporal preset exception availability must be unavailable.');
    }
    return {
      availability: 'unavailable' as const,
      preset_id: enumValue(exception.preset_id, presetIds, 'temporal exception preset'),
      reason_code: nonblank(exception.reason_code, 'temporal exception reason'),
    };
  });
  unique(preset_exceptions.map((exception) => exception.preset_id), 'Temporal exception presets');
  if (preset_exceptions.some((exception) => !candidate_presets.includes(exception.preset_id))) {
    throw new TypeError('Temporal preset exceptions must name candidate presets.');
  }
  return {
    candidate_presets,
    comparison_intent,
    comparison_policy,
    custom: { enabled: custom.enabled, max_duration_days, min_duration_days },
    default_preset,
    display_grains: parsedDisplayGrains,
    preset_exceptions,
  };
}

function parseVintagePolicy(value: unknown): VintagePolicyBinding {
  const policy = record(value, ['comparison', 'default', 'selector'], 'vintage policy');
  if (
    policy.comparison !== 'none'
    || policy.default !== 'latest_with_current_publication'
    || policy.selector !== 'as_of_month'
  ) throw new TypeError('Vintage policy has an unknown value.');
  return {
    comparison: 'none',
    default: 'latest_with_current_publication',
    selector: 'as_of_month',
  };
}

function parseBinding(value: unknown, expectedPageId: DeclaredPageId): PageBinding {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new TypeError('Page binding must be an object.');
  }
  const raw = value as UnknownRecord;
  const policyKeys = ['temporal_policy', 'vintage_policy'].filter((key) => key in raw);
  if (policyKeys.length !== 1) throw new TypeError('Page binding requires exactly one temporal policy.');
  const policyKey = policyKeys[0]!;
  const page = record(
    value,
    [
      'actions',
      'artifacts',
      'boundary_required',
      'compatibility_routes',
      'display_name',
      'mode',
      'page_id',
      'primary_job',
      'purpose',
      'route',
      'sections',
      policyKey,
      'visuals',
    ],
    'page binding',
  );
  const page_id = enumValue(page.page_id, declaredPageIds, 'page identity');
  if (page_id !== expectedPageId) throw new TypeError('Page binding identities are incomplete or reordered.');
  const route = enumValue(page.route, canonicalRoutes, 'page route');
  if (route !== declaredPaths[page_id]) throw new TypeError('Page binding route does not match its identity.');
  const compatibility_routes = array(
    page.compatibility_routes,
    'page compatibility routes',
  ).map((compatibilityRoute) =>
    enumValue(compatibilityRoute, compatibilityRoutes, 'page compatibility route')
  );
  unique(compatibility_routes, 'Page compatibility routes');
  if (compatibility_routes.includes(route as CompatibilityRoute)) {
    throw new TypeError('Page compatibility route must not be canonical.');
  }
  if (typeof page.boundary_required !== 'boolean') {
    throw new TypeError('Page boundary_required must be boolean.');
  }
  const artifacts = array(page.artifacts, 'page artifacts').map((value) => {
    const artifact = record(value, ['artifact_id', 'outcomes', 'type', 'version'], 'page artifact');
    const outcomes = array(artifact.outcomes, 'page artifact outcomes').map((outcome) =>
      nonblank(outcome, 'page artifact outcome')
    );
    unique(outcomes, 'Page artifact outcomes');
    if (outcomes.length === 0) throw new TypeError('Page artifact outcomes must not be empty.');
    return {
      artifact_id: nonblank(artifact.artifact_id, 'page artifact identity'),
      outcomes,
      type: nonblank(artifact.type, 'page artifact type'),
      version: positiveInteger(artifact.version, 'page artifact version'),
    };
  });
  unique(artifacts.map((artifact) => artifact.artifact_id), 'Page artifact identities');
  if (artifacts.length === 0) throw new TypeError('Page artifacts must not be empty.');
  const actions = array(page.actions, 'page actions').map((value) => {
    const action = record(value, ['action_id', 'target_route'], 'page action');
    return {
      action_id: nonblank(action.action_id, 'action identity'),
      target_route: enumValue(action.target_route, canonicalRoutes, 'page action target route'),
    };
  });
  unique(actions.map((action) => action.action_id), 'Page action identities');
  const sections = array(page.sections, 'page sections').map((value) => {
    const section = record(value, ['cardinality', 'role', 'section_id'], 'page section');
    return {
      cardinality: enumValue(section.cardinality, cardinalities, 'page section cardinality'),
      role: enumValue(section.role, sectionRoles, 'page section role'),
      section_id: nonblank(section.section_id, 'page section identity'),
    };
  });
  unique(sections.map((section) => section.section_id), 'Page section identities');
  if (sections.length === 0) throw new TypeError('Page sections must not be empty.');
  if (page.boundary_required !== sections.some((section) => section.section_id === 'boundary')) {
    throw new TypeError('Page boundary_required must match its boundary section.');
  }
  const visuals = array(page.visuals, 'page visuals').map((value) => {
    const visual = record(value, ['metric_cohort', 'narration_eligible', 'visual_id'], 'page visual');
    const metric_cohort = array(visual.metric_cohort, 'page visual metric cohort').map((metric) =>
      nonblank(metric, 'page visual metric')
    );
    unique(metric_cohort, 'Page visual metric cohort');
    if (metric_cohort.length === 0 || typeof visual.narration_eligible !== 'boolean') {
      throw new TypeError('Page visual fields are invalid.');
    }
    return {
      metric_cohort,
      narration_eligible: visual.narration_eligible,
      visual_id: nonblank(visual.visual_id, 'page visual identity'),
    };
  });
  unique(visuals.map((visual) => visual.visual_id), 'Page visual identities');
  const base: PageBindingBase = {
    actions,
    artifacts,
    boundary_required: page.boundary_required,
    compatibility_routes,
    display_name: nonblank(page.display_name, 'page display name'),
    mode: enumValue(page.mode, pageModes, 'page mode'),
    page_id,
    primary_job: nonblank(page.primary_job, 'page primary job'),
    purpose: nonblank(page.purpose, 'page purpose'),
    route,
    sections,
    visuals,
  };
  if (policyKey === 'temporal_policy') {
    return { ...base, temporal_policy: parseTemporalPolicy(page.temporal_policy) } as PresetPageBinding;
  }
  return { ...base, vintage_policy: parseVintagePolicy(page.vintage_policy) } as VintagePageBinding;
}

export function parsePageBindings(value: unknown): readonly PageBinding[] {
  const document = record(
    value,
    ['metric_display', 'pages', 'presentation_result_version', 'temporal_control_version', 'version'],
    'page bindings',
  );
  if (
    document.version !== 2
    || document.presentation_result_version !== 2
    || (document.temporal_control_version !== 1 && document.temporal_control_version !== 2)
  ) {
    throw new TypeError('Page binding transport versions are invalid.');
  }
  const pages = array(document.pages, 'pages');
  if (pages.length !== declaredPageIds.length) throw new TypeError('Page binding identities are incomplete.');
  const parsed = pages.map((page, index) => {
    const pageId = declaredPageIds[index];
    if (pageId === undefined) throw new TypeError('Page binding identities are incomplete.');
    return parseBinding(page, pageId);
  });
  // A calendar preset needs version 2: the version-1 decoder can't read its control.
  if (document.temporal_control_version === 1 && parsed.some(needsTemporalV2)) {
    throw new TypeError('Calendar presets need temporal control version 2.');
  }
  unique(parsed.map((page) => page.page_id), 'Page identities');
  unique(parsed.map((page) => page.route), 'Page routes');
  const compatibilityPaths = parsed.flatMap((page) => page.compatibility_routes);
  unique(compatibilityPaths, 'Page compatibility routes');
  if (compatibilityPaths.some((path) => canonicalRoutes.includes(path as CanonicalRoute))) {
    throw new TypeError('Page compatibility routes must not be canonical bindings.');
  }
  const rootCompatibilityOwners = parsed
    .filter((page) => page.compatibility_routes.includes('/'))
    .map((page) => page.page_id);
  // ADR-151: the composed Overview tab owns the root in the route manifest, so no page binding may.
  if (rootCompatibilityOwners.length !== 0) {
    throw new TypeError('No page binding may own the root route; the Overview does.');
  }
  return parsed;
}

/** Whether a page's control is version 2: it declares a calendar preset (ADR-100 point 8). */
export function needsTemporalV2(page: PageBinding): boolean {
  return page.vintage_policy !== undefined
    || page.temporal_policy.candidate_presets.some(isCalendarPreset);
}

/** A governed metric's unit and ratio scale, projected from the metric dictionary (plan D4). */
export interface MetricDisplay {
  readonly ratio_scale: 'multiplier' | 'rate' | null;
  readonly units: 'count' | 'currency' | 'ratio';
}

/** Decode the generated `metric_display` block: one unit per metric, a scale exactly on ratios. */
export function parseMetricDisplay(value: unknown): ReadonlyMap<string, MetricDisplay> {
  const document = record(value, ['metric_display', 'pages', 'presentation_result_version', 'temporal_control_version', 'version'], 'page bindings');
  const block = document.metric_display;
  if (typeof block !== 'object' || block === null || Array.isArray(block) || Object.keys(block).length === 0) {
    throw new TypeError('Metric display must be a nonempty object.');
  }
  const displays = new Map<string, MetricDisplay>();
  for (const [metric, raw] of Object.entries(block as UnknownRecord)) {
    const entry = record(raw, ['ratio_scale', 'units'], `metric display ${metric}`);
    const units = enumValue(entry.units, ['count', 'currency', 'ratio'] as const, `metric display ${metric} units`);
    const scale = entry.ratio_scale === null
      ? null
      : enumValue(entry.ratio_scale, ['multiplier', 'rate'] as const, `metric display ${metric} ratio scale`);
    if ((units === 'ratio') !== (scale !== null)) throw new TypeError(`Metric display ${metric} must scale exactly a ratio.`);
    displays.set(metric, { ratio_scale: scale, units });
  }
  return displays;
}

/** Every governed metric's display rule, keyed by metric id. */
export const metricDisplay = parseMetricDisplay(generatedDocument as unknown);

/** Every declared page binding, whether or not this frontend renders it. */
export const pageBindings = parsePageBindings(generatedDocument as unknown);

function isRendered(page: PageBinding): page is PageBinding & { readonly page_id: RouteName } {
  return (routeNames as readonly string[]).includes(page.page_id);
}

/** Only the bindings this frontend renders; navigation and route dispatch read these. */
export const renderedBindings: readonly (PageBinding & { readonly page_id: RouteName })[] =
  pageBindings.filter(isRendered);
if (renderedBindings.length !== routeNames.length) {
  throw new TypeError('Every rendered route must have a declared page binding.');
}
type BindingByRoute = {
  readonly [Name in RouteName]: Name extends 'retention' ? VintagePageBinding : PresetPageBinding;
};
export const bindingByRoute = Object.fromEntries(
  renderedBindings.map((page) => [page.page_id, page]),
) as BindingByRoute;
export const bindingByPath = Object.fromEntries(
  renderedBindings.map((page) => [page.route, page]),
) as Record<RenderedRoute, PageBinding>;


export const generatedActionById = (() => {
  const actions = new Map<string, CanonicalRoute>();
  for (const binding of pageBindings) {
    for (const action of binding.actions) {
      if (actions.has(action.action_id)) {
        throw new TypeError('Generated action identities must be globally unique.');
      }
      actions.set(action.action_id, action.target_route);
    }
  }
  return actions;
})();
