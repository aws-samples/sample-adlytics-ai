// SPDX-License-Identifier: MIT-0
import {
  campaignPortfolioSortFields,
  parseCampaignAttributionDetailV2,
  parseCampaignPortfolioArtifact,
  parseCampaignRosterShard,
  type CampaignAttributionDetailV2,
  type CampaignPortfolioArtifact,
  type CampaignPortfolioRosterEntry,
  type CampaignSelectionCapability,
} from './campaign-attribution';
import {
  CAMPAIGN_DETAIL_PATH,
  CAMPAIGN_LIST_PATH,
  CAMPAIGN_METRIC_FOCUS_PATH,
  INSIGHT_DEEP_DIVE_PATH,
  PORTFOLIO_METRIC_FOCUS_PATH,
  RETAIL_HEALTH_PATH,
  RETAIL_HEALTH_PRODUCT_PATH,
  NEW_CUSTOMERS_PATH,
  REACH_PATH,
  RETENTION_PATH,
  AUDIENCES_PATH,
  RETAIL_METRIC_FOCUS_PATH,
  SCORECARD_PATH,
} from './config';
import { parseMetricFocusV4, type MetricFocusV4 } from './metric-focus';
import { parseInsightDeepDiveV1, type InsightDeepDiveV1 } from './insight-deep-dive';
import { parseNextStep, type InsightNextStep } from './insight-next-step';
import {
  bindingByRoute,
  isCalendarPreset,
  needsTemporalV2,
  presetIds,
  type CanonicalRoute,
  type DeclaredPageId,
  type DisplayGrain,
  type PageBinding,
  type PresetPolicyBinding,
  type RollingPresetId,
  type RouteName,
  type TemporalComparisonPolicy,
  type TemporalPresetId,
} from './page-bindings';
import { PAGE_CONTEXT_KEYS, parsePageContext, type PageContextV1 } from './page-context';
import { parseRetailHealthArtifact, type RetailHealthArtifact } from './retail-health';
import { parseNetworkScorecardArtifact, type NetworkScorecardArtifact } from './scorecard';
import { parseNewCustomersArtifact, type NewCustomersArtifact } from './new-customers';
import { parseReachAndFunnelArtifact, type ReachAndFunnelArtifact } from './reach';
import { parseAudienceProfileArtifact, parseAudiencesPart, type AudienceProfileArtifact, type AudiencesPart, type AudiencesPartRequest } from './audiences';
import { parseRetentionCohortArtifact, type RetentionCohortArtifact } from './retention';
import { parseRetailProductAds, parseRetailProductDetail, type RetailProductAdsArtifact, type RetailProductDetailArtifact } from './retail-health-product';
import {
  PresentationRequestError,
  PresentationResponseError,
} from './presentation';
import { callApi, type JsonValue } from './session';

export const PRESENTATION_V2_MEDIA_TYPE = 'application/vnd.adlytics.presentation-v2+json';

const DATE = /^\d{4}-\d{2}-\d{2}$/;
const MONTH = /^\d{4}-(0[1-9]|1[0-2])$/;
const REF = /^[A-Za-z0-9_-]{43}$/;
const FORBIDDEN = new Set([
  'amazon_asin',
  'campaign_key',
  'channel_key',
  'marketplace_key',
  'platform_key',
]);
const PRESETS = ['1d', '7d', '14d', '30d'] as const satisfies readonly RollingPresetId[];

export interface TemporalWindow {
  readonly end: string;
  readonly start: string;
}

export type TemporalSelectionV1 =
  | { readonly kind: 'preset'; readonly preset_id: TemporalPresetId }
  | { readonly end: string; readonly kind: 'custom'; readonly start: string };


export interface TemporalControlV1 {
  readonly comparison: {
    readonly policy: TemporalComparisonPolicy;
    readonly reason_code: string | null;
    readonly status: 'available' | 'not_applicable' | 'not_requested' | 'unavailable';
  };
  readonly custom: {
    readonly availability: 'available' | 'unavailable';
    readonly bounds: {
      readonly earliest_start: string;
      readonly latest_end: string;
      readonly max_duration_days: number;
      readonly min_duration_days: number;
    } | null;
    readonly reason_code: string | null;
  };
  readonly mode: 'fixed' | 'selectable';
  readonly presets: readonly {
    readonly availability: 'available' | 'unavailable';
    readonly id: RollingPresetId;
    readonly label: '1D' | '7D' | '14D' | '30D';
    readonly reason_code: string | null;
  }[];
  readonly resolved: {
    readonly display_grain: DisplayGrain;
    readonly reporting_window_status: 'partial_default' | 'preferred_default' | 'user_selected';
    readonly selection: {
      readonly kind: 'custom' | 'default' | 'preset';
      readonly preset_id: RollingPresetId | null;
    };
    readonly window: TemporalWindow;
  };
  readonly version: 1;
}

/** One declared preset in a version-2 control, with the window it serves now (ia §8.1). */
export interface TemporalPresetV2 {
  readonly availability: 'available' | 'unavailable';
  readonly id: TemporalPresetId;
  readonly label: string;
  /** A calendar preset's served period ("August 2026"); null on a rolling preset. */
  readonly period_label: string | null;
  readonly reason_code: string | null;
  /** The server text an unavailable preset shows inside its button; null when available. */
  readonly reason_text: string | null;
  /** The window the preset serves now; null when its pointer names none. */
  readonly window: TemporalWindow | null;
}

export type TemporalComparisonV2 =
  | {
    readonly policy: Exclude<TemporalComparisonPolicy, 'prior_calendar_period'>;
    readonly reason_code: string | null;
    readonly status: 'available' | 'not_applicable' | 'not_requested' | 'unavailable';
  }
  | {
    readonly policy: 'prior_calendar_period';
    readonly prior_period_label: string;
    readonly status: 'available' | 'unavailable';
  };

/** `TemporalControl.v2` (ADR-100 point 8): the page's declared candidates, rolling then calendar. */
export interface TemporalControlV2 {
  readonly comparison: TemporalComparisonV2;
  readonly custom: TemporalControlV1['custom'];
  readonly mode: 'fixed' | 'selectable';
  readonly presets: readonly TemporalPresetV2[];
  readonly resolved: {
    readonly as_of_month: null;
    readonly display_grain: 'day' | 'month' | 'period' | 'quarter' | 'week';
    /** The resolved calendar period's server label; null on a rolling preset. */
    readonly period_label: string | null;
    readonly reporting_window_status: 'partial_default' | 'preferred_default' | 'user_selected';
    readonly selection: {
      readonly kind: 'custom' | 'default' | 'preset';
      readonly preset_id: TemporalPresetId | null;
    };
    readonly window: TemporalWindow;
  };
  readonly version: 2;
}

/** Retention's latest-only vintage: fixed, no roster and no selection (owner decision 2026-10-14). */
export interface VintageControlV2 {
  readonly mode: 'fixed';
  readonly resolved: {
    readonly as_of_month: string;
    /** The server's static label, "Cohorts through {period_label}", rendered as served. */
    readonly label_text: string;
    readonly period_label: string;
    readonly window: TemporalWindow;
  } | null;
  readonly version: 2;
}

export type AnyTemporalControl = TemporalControlV1 | TemporalControlV2 | VintageControlV2;

const CALENDAR_GRAINS = ['month', 'quarter', 'week'] as const;

export interface PresentationPageV2 {
  readonly actions: readonly { readonly action_id: string; readonly target_route: CanonicalRoute }[];
  readonly artifact: unknown;
  readonly artifact_id: string;
  readonly artifact_revision: string;
  readonly artifact_type: string;
  readonly artifact_version: number;
  readonly kind: string;
  readonly page_context: PageContextV1;
  readonly page_id: DeclaredPageId;
  readonly parent_revision?: string;
  readonly generation?: PageGenerationV1;
  readonly route: CanonicalRoute;
  /** Campaigns' served `open_page` step to the Scorecard (ADR-127), or null; absent elsewhere. */
  readonly scorecard_step?: InsightNextStep | null;
  /** Retail health's Products and ads part, only when Products and ads asks for it; undecoded here. */
  readonly product_ads?: unknown;
}

/** The served generation's build time (S6 §2.9): exactly one RFC 3339 whole-second instant. */
export type PageGenerationV1 =
  | { readonly built_at: string }
  | { readonly built_before: string };
export type PresentationResultV2<Control extends AnyTemporalControl = TemporalControlV1> =
  | {
    readonly envelope_version: 2;
    readonly kind: 'page';
    readonly page: PresentationPageV2;
    readonly session_id: string;
    readonly temporal: Control;
  }
  | {
    readonly envelope_version: 2;
    /** Version 2, `no_complete_period_measured` only: when the first period is expected. */
    readonly expected_after?: string | null;
    readonly kind: 'window_unavailable';
    /** Version 2, every reason: the server text the page shows (temporal contract rule 3). */
    readonly message?: string | null;
    readonly reason_code: string;
    readonly recovery: {
      readonly action_id: string | null;
      readonly no_self_service_reason: string | null;
    };
    readonly session_id: string;
    readonly temporal: Control;
  };

const fail = (): never => {
  throw new PresentationResponseError();
};

function presetPolicy(binding: PageBinding): PresetPolicyBinding {
  return binding.temporal_policy ?? fail();
}

function record(value: unknown, keys: readonly string[]): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return fail();
  const item = value as Record<string, unknown>;
  const actual = Object.keys(item);
  if (actual.length !== keys.length || !actual.every((key) => keys.includes(key))) return fail();
  return item;
}

function optionalRecord(
  value: unknown,
  required: readonly string[],
  optional: readonly string[],
): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return fail();
  const item = value as Record<string, unknown>;
  const actual = Object.keys(item);
  if (!required.every((key) => actual.includes(key)) || actual.some((key) => ![...required, ...optional].includes(key))) {
    return fail();
  }
  return item;
}

function text(value: unknown): string {
  if (typeof value !== 'string' || !value.trim()) return fail();
  return value;
}

function date(value: unknown): string {
  const result = text(value);
  if (!DATE.test(result)) return fail();
  const parsed = new Date(`${result}T00:00:00Z`);
  if (Number.isNaN(parsed.valueOf()) || parsed.toISOString().slice(0, 10) !== result) return fail();
  return result;
}

function month(value: unknown): string {
  const result = text(value);
  if (!MONTH.test(result)) return fail();
  return result;
}

function window(value: unknown): TemporalWindow {
  const item = record(value, ['end', 'start']);
  const start = date(item.start);
  const end = date(item.end);
  if (end < start) return fail();
  return { end, start };
}

function enumValue<const Values extends readonly string[]>(value: unknown, values: Values): Values[number] {
  const candidate = text(value);
  if (!values.includes(candidate)) return fail();
  return candidate as Values[number];
}

function positiveInteger(value: unknown): number {
  if (!Number.isSafeInteger(value) || (value as number) < 1) return fail();
  return value as number;
}

function jsonSafe(value: unknown): JsonValue {
  if (
    value === null ||
    typeof value === 'boolean' ||
    typeof value === 'string' ||
    (typeof value === 'number' && Number.isFinite(value))
  ) return value;
  if (Array.isArray(value)) return value.map(jsonSafe);
  if (typeof value !== 'object' || value === null) return fail();
  const item = value as Record<string, unknown>;
  if (Object.keys(item).some((key) => FORBIDDEN.has(key))) return fail();
  return Object.fromEntries(Object.entries(item).map(([key, nested]) => [key, jsonSafe(nested)]));
}

function parseTemporalControl(value: unknown): TemporalControlV1 {
  const item = record(value, ['comparison', 'custom', 'mode', 'presets', 'resolved', 'version']);
  if (item.version !== 1) return fail();
  const mode = enumValue(item.mode, ['fixed', 'selectable'] as const);
  const resolvedItem = record(item.resolved, ['display_grain', 'reporting_window_status', 'selection', 'window']);
  const status = enumValue(
    resolvedItem.reporting_window_status,
    ['partial_default', 'preferred_default', 'user_selected'] as const,
  );
  const selectionItem = record(resolvedItem.selection, ['kind', 'preset_id']);
  const selectionKind = enumValue(selectionItem.kind, ['custom', 'default', 'preset'] as const);
  const preset = selectionItem.preset_id === null ? null : enumValue(selectionItem.preset_id, PRESETS);
  if ((selectionKind === 'preset') !== (preset !== null)) return fail();
  if ((status === 'user_selected') !== (selectionKind !== 'default')) return fail();
  const display_grain = enumValue(resolvedItem.display_grain, ['day', 'period'] as const);
  const comparisonItem = record(item.comparison, ['policy', 'reason_code', 'status']);
  const policy = enumValue(comparisonItem.policy, ['none', 'optional_equal_prior', 'required_equal_prior'] as const);
  const comparisonStatus = enumValue(
    comparisonItem.status,
    ['available', 'not_applicable', 'not_requested', 'unavailable'] as const,
  );
  const comparisonReason = comparisonItem.reason_code === null ? null : text(comparisonItem.reason_code);
  if ((comparisonStatus === 'unavailable') !== (comparisonReason !== null)) return fail();
  if (policy === 'none' && (comparisonStatus !== 'not_applicable' || comparisonReason !== null)) return fail();
  if (policy === 'required_equal_prior' && comparisonStatus === 'not_requested') return fail();
  const presetsValue = item.presets;
  if (!Array.isArray(presetsValue) || presetsValue.length !== PRESETS.length) return fail();
  const presets = presetsValue.map((value, index) => {
    const presetItem = record(value, ['availability', 'id', 'label', 'reason_code']);
    const id = enumValue(presetItem.id, PRESETS);
    if (id !== PRESETS[index]) return fail();
    const availability = enumValue(presetItem.availability, ['available', 'unavailable'] as const);
    const reason_code = presetItem.reason_code === null ? null : text(presetItem.reason_code);
    if ((availability === 'unavailable') !== (reason_code !== null)) return fail();
    const label = enumValue(presetItem.label, ['1D', '7D', '14D', '30D'] as const);
    if (label !== id.toUpperCase()) return fail();
    return { availability, id, label, reason_code };
  });
  const customItem = record(item.custom, ['availability', 'bounds', 'reason_code']);
  const customAvailability = enumValue(customItem.availability, ['available', 'unavailable'] as const);
  const customReason = customItem.reason_code === null ? null : text(customItem.reason_code);
  const bounds = customItem.bounds === null ? null : record(
    customItem.bounds,
    ['earliest_start', 'latest_end', 'max_duration_days', 'min_duration_days'],
  );
  if ((customAvailability === 'available') !== (bounds !== null) || (customAvailability === 'unavailable') !== (customReason !== null)) return fail();
  const parsedBounds = bounds === null ? null : {
    earliest_start: date(bounds.earliest_start),
    latest_end: date(bounds.latest_end),
    max_duration_days: positiveInteger(bounds.max_duration_days),
    min_duration_days: positiveInteger(bounds.min_duration_days),
  };
  if (parsedBounds !== null && (parsedBounds.latest_end < parsedBounds.earliest_start || parsedBounds.max_duration_days < parsedBounds.min_duration_days)) return fail();
  if (mode === 'fixed' && (presets.some((preset) => preset.availability === 'available') || customAvailability === 'available')) return fail();
  return {
    comparison: { policy, reason_code: comparisonReason, status: comparisonStatus },
    custom: { availability: customAvailability, bounds: parsedBounds, reason_code: customReason },
    mode,
    presets,
    resolved: {
      display_grain,
      reporting_window_status: status,
      selection: { kind: selectionKind, preset_id: preset },
      window: window(resolvedItem.window),
    },
    version: 1,
  };
}

function nullableText(value: unknown): string | null {
  return value === null ? null : text(value);
}

/**
 * Decode one `TemporalControl.v2` for a page that declares calendar presets. Its presets are
 * exactly the page's declared candidates in canonical order; a calendar period carries its
 * server label; the "no comparison for calendar periods" state belongs to calendar periods only.
 */
export function parseTemporalControlV2(value: unknown, binding: PageBinding): TemporalControlV2 {
  const item = record(value, ['comparison', 'custom', 'mode', 'presets', 'resolved', 'version']);
  if (item.version !== 2) return fail();
  const mode = enumValue(item.mode, ['fixed', 'selectable'] as const);
  const resolvedItem = record(
    item.resolved,
    ['as_of_month', 'display_grain', 'period_label', 'reporting_window_status', 'selection', 'window'],
  );
  if (resolvedItem.as_of_month !== null) return fail();
  const status = enumValue(
    resolvedItem.reporting_window_status,
    ['partial_default', 'preferred_default', 'user_selected'] as const,
  );
  const declaredPolicy = presetPolicy(binding);
  const candidates = declaredPolicy.candidate_presets;
  const selectionItem = record(resolvedItem.selection, ['kind', 'preset_id']);
  const selectionKind = enumValue(selectionItem.kind, ['custom', 'default', 'preset'] as const);
  const preset = selectionItem.preset_id === null ? null : enumValue(selectionItem.preset_id, candidates);
  if ((selectionKind === 'preset') !== (preset !== null)) return fail();
  if ((status === 'user_selected') !== (selectionKind !== 'default')) return fail();
  const display_grain = enumValue(resolvedItem.display_grain, ['day', 'month', 'period', 'quarter', 'week'] as const);
  const calendar = (CALENDAR_GRAINS as readonly string[]).includes(display_grain);
  const period_label = nullableText(resolvedItem.period_label);
  const resolvedWindow = window(resolvedItem.window);
  if (calendar !== (period_label !== null)) return fail();
  if (preset !== null && isCalendarPreset(preset) !== calendar) return fail();
  const comparisonProbe = optionalRecord(item.comparison, ['policy', 'status'], ['prior_period_label', 'reason_code']);
  const policy = enumValue(comparisonProbe.policy, ['none', 'optional_equal_prior', 'prior_calendar_period', 'required_equal_prior'] as const);
  if (policy !== declaredPolicy.comparison_policy) return fail();
  let comparison: TemporalComparisonV2;
  if (policy === 'prior_calendar_period') {
    const comparisonItem = record(item.comparison, ['policy', 'prior_period_label', 'status']);
    const status = enumValue(comparisonItem.status, ['available', 'unavailable'] as const);
    if (!calendar) return fail();
    comparison = {
      policy,
      prior_period_label: text(comparisonItem.prior_period_label),
      status,
    };
  } else {
    const comparisonItem = record(item.comparison, ['policy', 'reason_code', 'status']);
    const comparisonStatus = enumValue(
      comparisonItem.status,
      ['available', 'not_applicable', 'not_requested', 'unavailable'] as const,
    );
    const comparisonReason = nullableText(comparisonItem.reason_code);
    const calendarComparison = comparisonStatus === 'not_applicable' && comparisonReason === 'calendar_period_selected';
    if (calendar !== calendarComparison) return fail();
    if (calendar && policy !== 'optional_equal_prior') return fail();
    if (!calendar) {
      if ((comparisonStatus === 'unavailable') !== (comparisonReason !== null)) return fail();
      if (policy === 'none' && comparisonStatus !== 'not_applicable') return fail();
      if (policy !== 'none' && comparisonStatus === 'not_applicable') return fail();
      if (policy === 'required_equal_prior' && comparisonStatus === 'not_requested') return fail();
    }
    comparison = { policy, reason_code: comparisonReason, status: comparisonStatus };
  }
  const presetsValue = item.presets;
  if (!Array.isArray(presetsValue) || presetsValue.length !== candidates.length) return fail();
  const declared = presetIds.filter((id) => candidates.includes(id));
  const presets = presetsValue.map((entry, index): TemporalPresetV2 => {
    const presetItem = record(entry, ['availability', 'id', 'label', 'period_label', 'reason_code', 'reason_text', 'window']);
    const id = enumValue(presetItem.id, candidates);
    // The label is the server's (temporal contract: `id, label`), rendered as served (G9A-8).
    if (id !== declared[index]) return fail();
    const label = text(presetItem.label);
    const availability = enumValue(presetItem.availability, ['available', 'unavailable'] as const);
    const reason_code = nullableText(presetItem.reason_code);
    const reason_text = nullableText(presetItem.reason_text);
    if ((availability === 'unavailable') !== (reason_code !== null) || (reason_code === null) !== (reason_text === null)) return fail();
    const presetWindow = presetItem.window === null ? null : window(presetItem.window);
    const presetPeriod = nullableText(presetItem.period_label);
    if (presetPeriod !== null && (presetWindow === null || !isCalendarPreset(id))) return fail();
    if (presetWindow !== null && isCalendarPreset(id) && presetPeriod === null) return fail();
    return { availability, id, label, period_label: presetPeriod, reason_code, reason_text, window: presetWindow };
  });
  if (selectionKind === 'custom') return fail();
  const resolvedPresetId = preset ?? declaredPolicy.default_preset;
  const resolvedPreset = presets.find((entry) => entry.id === resolvedPresetId);
  if (
    resolvedPreset === undefined || resolvedPreset.window === null ||
    resolvedPreset.period_label !== period_label || !sameWindow(resolvedPreset.window, resolvedWindow)
  ) return fail();
  const customItem = record(item.custom, ['availability', 'bounds', 'reason_code']);
  // Every version-2 page is served from preset artifacts: Custom is never offered.
  if (customItem.availability !== 'unavailable' || customItem.bounds !== null) return fail();
  const customReason = text(customItem.reason_code);
  if (mode === 'fixed' && presets.some((entry) => entry.availability === 'available')) return fail();
  return {
    comparison,
    custom: { availability: 'unavailable', bounds: null, reason_code: customReason },
    mode,
    presets,
    resolved: {
      as_of_month: null,
      display_grain,
      period_label,
      reporting_window_status: status,
      selection: { kind: selectionKind, preset_id: preset },
      window: resolvedWindow,
    },
    version: 2,
  };
}

export function parseVintageControlV2(value: unknown): VintageControlV2 {
  const item = record(value, ['mode', 'resolved', 'version']);
  if (item.version !== 2 || item.mode !== 'fixed') return fail();
  if (item.resolved === null) return { mode: 'fixed', resolved: null, version: 2 };
  const resolvedItem = record(item.resolved, ['as_of_month', 'label_text', 'period_label', 'window']);
  const as_of_month = month(resolvedItem.as_of_month);
  const period_label = text(resolvedItem.period_label);
  const label_text = text(resolvedItem.label_text);
  const resolvedWindow = window(resolvedItem.window);
  if (!resolvedWindow.end.startsWith(as_of_month) || label_text !== `Cohorts through ${period_label}`) return fail();
  return { mode: 'fixed', resolved: { as_of_month, label_text, period_label, window: resolvedWindow }, version: 2 };
}
function pageContext(item: Record<string, unknown>): PageContextV1 {
  try {
    return parsePageContext(item, text(item.artifact_revision));
  } catch {
    return fail();
  }
}

function parsePage(value: unknown, route: RouteName): PresentationPageV2 {
  const item = optionalRecord(
    value,
    ['actions', 'artifact', 'artifact_id', 'artifact_revision', 'artifact_type', 'artifact_version', 'kind', 'page_id', 'route', ...PAGE_CONTEXT_KEYS],
    ['generation', 'parent_revision', 'product_ads', 'scorecard_step'],
  );
  const binding = bindingByRoute[route];
  const page_id = enumValue(item.page_id, [binding.page_id] as const);
  const pageRoute = enumValue(item.route, [binding.route] as const);
  const artifact_id = text(item.artifact_id);
  const artifact = binding.artifacts.find((candidate) => candidate.artifact_id === artifact_id);
  if (
    artifact === undefined ||
    text(item.artifact_type) !== artifact.type ||
    item.artifact_version !== artifact.version ||
    !REF.test(text(item.artifact_revision))
  ) return fail();
  const actionsValue = item.actions;
  if (!Array.isArray(actionsValue) || actionsValue.length !== binding.actions.length) return fail();
  const actions = actionsValue.map((value, index) => {
    const action = record(value, ['action_id', 'target_route']);
    const expected = binding.actions[index];
    if (expected === undefined || text(action.action_id) !== expected.action_id || action.target_route !== expected.target_route) return fail();
    return { action_id: expected.action_id, target_route: expected.target_route };
  });
  const parent_revision = item.parent_revision === undefined ? undefined : text(item.parent_revision);
  if (parent_revision !== undefined && !REF.test(parent_revision)) return fail();
  const generation = item.generation === undefined ? undefined : parseGeneration(item.generation);
  if ('scorecard_step' in item && route !== 'campaigns') return fail();
  if ('product_ads' in item && route !== 'retail-health') return fail();
  let scorecard_step: InsightNextStep | null | undefined;
  try {
    scorecard_step = item.scorecard_step === undefined || item.scorecard_step === null
      ? item.scorecard_step as null | undefined
      : parseNextStep(item.scorecard_step);
  } catch {
    return fail();
  }
  if (scorecard_step !== undefined && scorecard_step !== null && scorecard_step.kind !== 'open_page') return fail();
  return {
    actions,
    // Reach's strict artifact owns its machine-only marketplace key and refuses multiple markets;
    // every other page, Audiences included, keeps the generic forbidden-key walk at this boundary.
    artifact: route === 'reach' || route === 'retention' ? item.artifact : jsonSafe(item.artifact),
    artifact_id,
    artifact_revision: text(item.artifact_revision),
    artifact_type: artifact.type,
    artifact_version: artifact.version,
    ...(generation === undefined ? {} : { generation }),
    kind: text(item.kind),
    // ADR-120: the page's own revision is the reference the assistant sends back.
    page_context: pageContext(item),
    page_id,
    ...(parent_revision === undefined ? {} : { parent_revision }),
    ...('product_ads' in item ? { product_ads: item.product_ads } : {}),
    route: pageRoute,
    ...(scorecard_step === undefined ? {} : { scorecard_step }),
  };
}

const INSTANT = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/;

/** A closed object with exactly one of `built_at` or `built_before`; anything else is malformed. */
function parseGeneration(value: unknown): PageGenerationV1 {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return fail();
  const keys = Object.keys(value);
  if (keys.length !== 1) return fail();
  const [key] = keys;
  if (key !== 'built_at' && key !== 'built_before') return fail();
  const instant = (value as Record<string, unknown>)[key];
  if (typeof instant !== 'string' || !INSTANT.test(instant)) return fail();
  return key === 'built_at' ? { built_at: instant } : { built_before: instant };
}

/** Decode a version-1 page's result; a page that declares calendar presets is refused here. */
export function parsePresentationResultV2(value: unknown, route: RouteName): PresentationResultV2 {
  if (needsTemporalV2(bindingByRoute[route])) return fail();
  return parseEnvelope(value, route, parseTemporalControl, false);
}

/** Decode the result of a page that declares calendar presets: its control is version 2. */
export function parsePresentationResultCalendarV2(value: unknown, route: RouteName): PresentationResultV2<TemporalControlV2> {
  const binding = bindingByRoute[route];
  if (!needsTemporalV2(binding)) return fail();
  return parseEnvelope(value, route, (control) => parseTemporalControlV2(control, binding), true);
}

/** Decode the result of a page that declares cohort vintages. */
export function parsePresentationResultVintageV2(
  value: unknown,
  route: 'retention',
): PresentationResultV2<VintageControlV2> {
  const item = optionalRecord(
    value,
    ['envelope_version', 'kind', 'session_id', 'temporal'],
    ['expected_after', 'message', 'page', 'reason_code', 'recovery'],
  );
  if (item.envelope_version !== 2) return fail();
  const session_id = text(item.session_id);
  const temporal = parseVintageControlV2(item.temporal);
  if (item.kind === 'page') {
    if (item.reason_code !== undefined || item.recovery !== undefined || item.message !== undefined || item.expected_after !== undefined) return fail();
    return { envelope_version: 2, kind: 'page', page: parsePage(item.page, route), session_id, temporal };
  }
  if (item.kind !== 'window_unavailable' || item.page !== undefined) return fail();
  const recovery = record(item.recovery, ['action_id', 'no_self_service_reason']);
  const action_id = recovery.action_id === null ? null : text(recovery.action_id);
  const no_self_service_reason = recovery.no_self_service_reason === null ? null : text(recovery.no_self_service_reason);
  if ((action_id === null) === (no_self_service_reason === null)) return fail();
  // Version-2 refusals always carry both keys. Only no_complete_period_measured carries a date.
  if (!("message" in item) || !("expected_after" in item)) return fail();
  const reason_code = text(item.reason_code);
  const message = text(item.message);
  const expected_after = item.expected_after === null ? null : date(item.expected_after);
  if ((reason_code === 'no_complete_period_measured') !== (expected_after !== null)) return fail();
  return { envelope_version: 2, expected_after, kind: 'window_unavailable', message, reason_code, recovery: { action_id, no_self_service_reason }, session_id, temporal };
}

function parseEnvelope<Control extends AnyTemporalControl>(
  value: unknown,
  route: RouteName,
  parseControl: (value: unknown) => Control,
  version2: boolean,
): PresentationResultV2<Control> {
  const item = optionalRecord(
    value,
    ['envelope_version', 'kind', 'session_id', 'temporal'],
    version2
      ? ['expected_after', 'message', 'page', 'reason_code', 'recovery', 'row_capabilities']
      : ['page', 'reason_code', 'recovery', 'row_capabilities'],
  );
  if (item.envelope_version !== 2) return fail();
  const session_id = text(item.session_id);
  const temporal = parseControl(item.temporal);
  if (item.kind === 'page') {
    if (version2) {
      const temporalV2 = temporal as TemporalControlV2;
      const selectedPresetId = temporalV2.resolved.selection.preset_id ?? presetPolicy(bindingByRoute[route]).default_preset;
      if (temporalV2.presets.find((preset) => preset.id === selectedPresetId)?.availability !== 'available') return fail();
    }
    if (item.reason_code !== undefined || item.recovery !== undefined) return fail();
    if (item.message !== undefined || item.expected_after !== undefined) return fail();
    const page = parsePage(item.page, route);
    if (item.row_capabilities !== undefined) return fail();
    return {
      envelope_version: 2,
      kind: 'page',
      page,
      session_id,
      temporal,
    };
  }
  if (item.kind !== 'window_unavailable' || item.page !== undefined || item.row_capabilities !== undefined) return fail();
  const recovery = record(item.recovery, ['action_id', 'no_self_service_reason']);
  const action_id = recovery.action_id === null ? null : text(recovery.action_id);
  const no_self_service_reason = recovery.no_self_service_reason === null ? null : text(recovery.no_self_service_reason);
  if ((action_id === null) === (no_self_service_reason === null)) return fail();
  const reason_code = text(item.reason_code);
  if (!version2) {
    return { envelope_version: 2, kind: 'window_unavailable', reason_code, recovery: { action_id, no_self_service_reason }, session_id, temporal };
  }
  // Temporal contract rules 3 and 4: every version-2 refusal carries the server's text, so the
  // browser never maps a reason code; only "no complete period measured" carries a date.
  if (!('message' in item) || !('expected_after' in item)) return fail();
  const noPeriod = reason_code === 'no_complete_period_measured';
  const message = text(item.message);
  const expected_after = item.expected_after === null ? null : date(item.expected_after);
  if (noPeriod !== (expected_after !== null)) return fail();
  return {
    envelope_version: 2,
    expected_after,
    kind: 'window_unavailable',
    message,
    reason_code,
    recovery: { action_id, no_self_service_reason },
    session_id,
    temporal,
  };
}

export interface CampaignPortfolioSort {
  readonly direction: 'asc' | 'desc';
  readonly field: typeof campaignPortfolioSortFields[number];
}

/** A governed channel filter id (ia §8.1); `all` is the default and is never sent. */
const FILTER_ID = /^[a-z0-9][a-z0-9_-]{0,63}$/;

function filterField(filterId: string | undefined): Record<string, JsonValue> {
  if (filterId === undefined || filterId === 'all') return {};
  return FILTER_ID.test(filterId) ? { filter_id: filterId } : fail();
}

function requestBody(
  session: string,
  selection?: TemporalSelectionV1,
  sort?: CampaignPortfolioSort,
  segment?: number,
  filterId?: string,
): JsonValue {
  if (!text(session) || session.length > 200) return fail();
  if (selection !== undefined) {
    if (selection.kind === 'preset') {
      enumValue(selection.preset_id, PRESETS);
    } else {
      const start = date(selection.start);
      const end = date(selection.end);
      if (end < start) return fail();
    }
  }
  if (sort !== undefined) {
    enumValue(sort.field, campaignPortfolioSortFields);
    enumValue(sort.direction, ['asc', 'desc'] as const);
  }
  if (segment !== undefined && (!Number.isInteger(segment) || segment < 0)) return fail();
  const body: Record<string, JsonValue> = { session_id: session };
  if (selection !== undefined) body.temporal_selection = selection;
  if (sort !== undefined) body.sort = { direction: sort.direction, field: sort.field };
  if (segment !== undefined && segment > 0) body.segment = segment;
  return { ...body, ...filterField(filterId) };
}

function parentPath(route: RouteName): string {
  if (route === 'campaigns') return CAMPAIGN_LIST_PATH;
  if (route === 'retail-health') return RETAIL_HEALTH_PATH;
  return fail();
}

async function responseJson(response: Response): Promise<unknown> {
  if (response.ok) {
    try {
      return await response.json();
    } catch {
      return fail();
    }
  }
  try {
    const item = record(await response.json(), ['code', 'error', 'error_class']);
    const code = text(item.code);
    const errorClass = enumValue(item.error_class, ['dependency', 'domain', 'invariant', 'validation'] as const);
    if (typeof item.error !== 'string' || !item.error.trim()) return fail();
    throw new PresentationRequestError(errorClass, code, item.error.trim());
  } catch (error) {
    if (error instanceof PresentationRequestError) throw error;
    throw new PresentationRequestError();
  }
}

export async function fetchPresentationParentV2(
  route: RouteName,
  session: string,
  selection?: TemporalSelectionV1,
  signal?: AbortSignal,
): Promise<PresentationResultV2> {
  const response = await callApi(
    parentPath(route),
    'POST',
    requestBody(session, selection),
    signal,
    { accept: PRESENTATION_V2_MEDIA_TYPE },
  );
  const result = parsePresentationResultV2(await responseJson(response), route);
  if (result.session_id !== session) return fail();
  return result;
}

function sameWindow(left: TemporalWindow, right: TemporalWindow): boolean {
  return left.start === right.start && left.end === right.end;
}

function childRequestBody(session: string, selectionReference: string): JsonValue {
  if (!text(session) || session.length > 200 || !REF.test(text(selectionReference))) return fail();
  return { selection_reference: selectionReference, session_id: session };
}

export type CampaignParentPresentation =
  | {
    readonly artifact: CampaignPortfolioArtifact;
    readonly artifact_revision: string;
    readonly generation?: PageGenerationV1;
    readonly kind: 'parent';
    readonly page_context: PageContextV1;
    /** The served step to the Scorecard under a multi-channel option; null until it ships. */
    readonly scorecard_step: InsightNextStep | null;
    readonly temporal: TemporalControlV1;
  }
  | {
    readonly kind: 'window_unavailable';
    readonly temporal: TemporalControlV1;
  };

export type CampaignPresentationV2 =
  | CampaignParentPresentation
  | {
    readonly artifact: CampaignAttributionDetailV2;
    readonly artifact_revision: string;
    readonly kind: 'child';
    readonly page_context: PageContextV1;
    readonly parent_revision: string;
    /** The campaign's channel step to the Scorecard row (ia §7.2 item 13); null until it ships. */
    readonly scorecard_step: InsightNextStep | null;
    readonly temporal: TemporalControlV1;
  };

type CampaignChildPresentationV2 = Extract<CampaignPresentationV2, { readonly kind: 'child' }>;

export function campaignPresentationV2(result: PresentationResultV2): CampaignPresentationV2 {
  if (result.kind === 'window_unavailable') {
    return { kind: 'window_unavailable', temporal: result.temporal };
  }
  const { page, temporal } = result;
  // ADR-127 retired the `campaigns` actions block; the Scorecard step is a served step instead.
  if (page.page_id !== 'campaigns' || page.route !== '/campaigns/' || page.actions.length !== 0) return fail();
  if (page.scorecard_step === undefined) return fail();
  const scorecard_step = page.scorecard_step;
  if (page.parent_revision === undefined) {
    if (
      page.kind !== 'campaign_portfolio' || page.artifact_id !== 'campaign_catalog' ||
      page.artifact_type !== 'CampaignPortfolioArtifact' || page.artifact_version !== 1
    ) return fail();
    let artifact: CampaignPortfolioArtifact;
    try {
      artifact = parseCampaignPortfolioArtifact(page.artifact);
    } catch {
      return fail();
    }
    if (!sameWindow(artifact.period, temporal.resolved.window)) return fail();
    if (scorecard_step !== null && artifact.channel_view === null) return fail();
    return {
      artifact,
      artifact_revision: page.artifact_revision,
      ...(page.generation === undefined ? {} : { generation: page.generation }),
      kind: 'parent',
      page_context: page.page_context,
      scorecard_step,
      temporal,
    };
  }
  if (
    page.kind !== 'campaign_detail' || page.artifact_id !== 'campaign_detail' ||
    page.artifact_type !== 'CampaignAttributionDetail' || page.artifact_version !== 2
  ) return fail();
  let artifact: CampaignAttributionDetailV2;
  try {
    artifact = parseCampaignAttributionDetailV2(page.artifact);
  } catch {
    return fail();
  }
  if (!sameWindow(artifact.period, temporal.resolved.window)) return fail();
  return {
    artifact,
    artifact_revision: page.artifact_revision,
    kind: 'child',
    page_context: page.page_context,
    parent_revision: page.parent_revision,
    scorecard_step,
    temporal,
  };
}

export async function fetchCampaignPresentationV2(
  session: string,
  selection?: TemporalSelectionV1,
  signal?: AbortSignal,
  sort?: CampaignPortfolioSort,
  segment?: number,
  filterId?: string,
): Promise<CampaignParentPresentation> {
  const response = await callApi(
    CAMPAIGN_LIST_PATH,
    'POST',
    requestBody(session, selection, sort, segment, filterId),
    signal,
    { accept: PRESENTATION_V2_MEDIA_TYPE },
  );
  const result = parsePresentationResultV2(await responseJson(response), 'campaigns');
  if (result.session_id !== session) return fail();
  const presentation = campaignPresentationV2(result);
  if (presentation.kind === 'child') return fail();
  return presentation;
}

/**
 * Fetch one baked shard of a filter's name roster (ADR-145). The server serves it from the
 * parent's index, never measuring; a shard of another revision comes back as it is, for the
 * caller's revision guard to compare with the body it holds.
 */
export async function fetchCampaignRosterShardV2(
  session: string,
  selection: TemporalSelectionV1 | undefined,
  filterId: string,
  shard: number,
  signal?: AbortSignal,
): Promise<{ readonly entries: readonly CampaignPortfolioRosterEntry[]; readonly parent_revision: string }> {
  if (!Number.isInteger(shard) || shard < 0) return fail();
  const body = { ...(requestBody(session, selection, undefined, undefined, filterId) as Record<string, JsonValue>), roster_shard: shard };
  const response = await callApi(CAMPAIGN_LIST_PATH, 'POST', body, signal, { accept: PRESENTATION_V2_MEDIA_TYPE });
  const value = await responseJson(response);
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return fail();
  const item = value as Record<string, unknown>;
  const keys = Object.keys(item).sort();
  if (keys.join(',') !== 'kind,parent_revision,session_id,shard' || item.kind !== 'campaign_roster_shard' || item.session_id !== session) return fail();
  let parsed: { readonly entries: readonly CampaignPortfolioRosterEntry[]; readonly parent_revision: string };
  try { parsed = parseCampaignRosterShard(item.shard, shard); } catch { return fail(); }
  if (parsed.parent_revision !== item.parent_revision) return fail();
  return parsed;
}

export async function fetchCampaignPresentationChildV2(
  session: string,
  choice: CampaignSelectionCapability,
  signal?: AbortSignal,
): Promise<CampaignChildPresentationV2> {
  const response = await callApi(
    CAMPAIGN_DETAIL_PATH,
    'POST',
    childRequestBody(session, choice.selection_reference),
    signal,
    { accept: PRESENTATION_V2_MEDIA_TYPE },
  );
  const result = parsePresentationResultV2(await responseJson(response), 'campaigns');
  if (result.session_id !== session) return fail();
  const presentation = campaignPresentationV2(result);
  if (presentation.kind !== 'child') return fail();
  return presentation;
}

export const campaignFocusMetrics = [
  'purchases', 'attributed_roas', 'spend', 'impressions', 'clicks', 'ctr', 'cpc', 'cpm',
] as const;
export type CampaignFocusMetric = typeof campaignFocusMetrics[number];

export const portfolioFocusMetrics = [
  'spend', 'purchases', 'impressions', 'portfolio_roas_attributed_14d',
] as const;
export type PortfolioFocusMetric = typeof portfolioFocusMetrics[number];
/** A multi-channel option's focusable tiles: purchases and ROAS aren't added (ADR-127). */
export const multiChannelFocusMetrics = ['spend', 'impressions'] as const satisfies readonly PortfolioFocusMetric[];

function campaignMetricFocusRequestBody(
  session: string,
  selectionReference: string,
  metric: CampaignFocusMetric,
): JsonValue {
  if (!text(session) || session.length > 200 || !REF.test(text(selectionReference))) return fail();
  if (!campaignFocusMetrics.includes(metric)) return fail();
  return { metric, selection_reference: selectionReference, session_id: session };
}

/** Whether the control offers at least one preset the user can choose instead. */
export function anyPresetAvailable(temporal: TemporalControlV1): boolean {
  return temporal.presets.some((preset) => preset.availability === 'available');
}

/** The preset the page is actually on, so a focus request addresses the same baked body. */
export function focusTemporalSelection(control: TemporalControlV1): TemporalSelectionV1 | undefined {
  const selection = control.resolved.selection;
  return selection.kind === 'preset' && selection.preset_id !== null
    ? { kind: 'preset', preset_id: selection.preset_id }
    : undefined;
}

function parentFocusRequestBody(
  session: string,
  metric: string,
  selection: TemporalSelectionV1 | undefined,
  filterId?: string,
): JsonValue {
  if (!text(session) || session.length > 200 || !text(metric)) return fail();
  if (selection === undefined) return { metric, session_id: session, ...filterField(filterId) };
  if (selection.kind !== 'preset') return fail();
  return {
    metric,
    session_id: session,
    temporal_selection: { kind: 'preset', preset_id: selection.preset_id },
    ...filterField(filterId),
  };
}

function decodedMetricFocus(raw: unknown, metric: string): MetricFocusV4 {
  let focus: MetricFocusV4;
  try {
    focus = parseMetricFocusV4(raw);
  } catch {
    return fail();
  }
  if (focus.metric.id !== metric) return fail();
  return focus;
}

/** The capability-bound detail focus; the response is the versioned MetricFocus.v3 result. */
export async function fetchCampaignMetricFocus(
  session: string,
  selectionReference: string,
  metric: CampaignFocusMetric,
  signal?: AbortSignal,
): Promise<MetricFocusV4> {
  const response = await callApi(
    CAMPAIGN_METRIC_FOCUS_PATH,
    'POST',
    campaignMetricFocusRequestBody(session, selectionReference, metric),
    signal,
    { accept: PRESENTATION_V2_MEDIA_TYPE },
  );
  return decodedMetricFocus(await responseJson(response), metric);
}

/** The parent-scope portfolio focus derived from the same baked artifact the page shows. */
export async function fetchPortfolioMetricFocus(
  session: string,
  metric: PortfolioFocusMetric,
  selection: TemporalSelectionV1 | undefined,
  signal?: AbortSignal,
  filterId?: string,
): Promise<MetricFocusV4> {
  if (!portfolioFocusMetrics.includes(metric)) return fail();
  const response = await callApi(
    PORTFOLIO_METRIC_FOCUS_PATH,
    'POST',
    parentFocusRequestBody(session, metric, selection, filterId),
    signal,
    { accept: PRESENTATION_V2_MEDIA_TYPE },
  );
  return decodedMetricFocus(await responseJson(response), metric);
}

/** The parent-scope retail focus derived from the same baked artifact the page shows. */
export async function fetchRetailMetricFocus(
  session: string,
  metric: string,
  selection: TemporalSelectionV1 | undefined,
  signal?: AbortSignal,
): Promise<MetricFocusV4> {
  const response = await callApi(
    RETAIL_METRIC_FOCUS_PATH,
    'POST',
    parentFocusRequestBody(session, metric, selection),
    signal,
    { accept: PRESENTATION_V2_MEDIA_TYPE },
  );
  return decodedMetricFocus(await responseJson(response), metric);
}

/** Closed browser request for one ADR-076 deep section. */
export type InsightDeepDiveRequest =
  | {
      /** The channel filter sibling the card was filled from (ADR-127); omitted is All channels. */
      readonly filter_id?: string;
      readonly metric?: undefined;
      /** The served segment a portfolio page card came from (W11); page slots only. */
      readonly segment?: number;
      readonly selection_reference?: undefined;
      readonly session_id: string;
      readonly slot_id: string;
      readonly sort?: CampaignPortfolioSort;
      readonly temporal_selection?: TemporalSelectionV1;
    }
  | {
      readonly filter_id?: string;
      readonly metric: string;
      readonly selection_reference?: undefined;
      readonly session_id: string;
      readonly slot_id: 'metric-focus' | 'retail-metric-focus';
      readonly temporal_selection?: TemporalSelectionV1;
    }
  | {
      readonly metric?: string;
      readonly selection_reference: string;
      readonly session_id: string;
      readonly slot_id: string;
      readonly temporal_selection?: undefined;
    };

/** Fetch one deep section through the parent-scope or capability-bound identity. */
export async function fetchInsightDeepDive(
  request: InsightDeepDiveRequest,
  signal?: AbortSignal,
): Promise<InsightDeepDiveV1> {
  if (!text(request.session_id) || request.session_id.length > 200 || !text(request.slot_id)) return fail();
  if (request.temporal_selection?.kind === 'custom') return fail();
  if (request.selection_reference !== undefined && !REF.test(request.selection_reference)) return fail();
  const { segment, sort } = request as { readonly segment?: number; readonly sort?: CampaignPortfolioSort };
  const body: Record<string, JsonValue> = {};
  for (const [key, value] of Object.entries(request)) {
    if (key !== 'sort' && key !== 'segment' && key !== 'filter_id' && value !== undefined) body[key] = value as JsonValue;
  }
  Object.assign(body, filterField((request as { readonly filter_id?: string }).filter_id));
  if (sort !== undefined) {
    enumValue(sort.field, campaignPortfolioSortFields);
    enumValue(sort.direction, ['asc', 'desc'] as const);
    body.sort = { direction: sort.direction, field: sort.field };
  }
  if (segment !== undefined && (!Number.isInteger(segment) || segment < 0)) return fail();
  if (segment !== undefined && segment > 0) body.segment = segment;
  const response = await callApi(
    INSIGHT_DEEP_DIVE_PATH,
    'POST',
    body,
    signal,
    { accept: PRESENTATION_V2_MEDIA_TYPE },
  );
  const deep = parseInsightDeepDiveV1(await responseJson(response));
  if (deep.slot_id !== request.slot_id) return fail();
  return deep;
}

export type RetailHealthPresentation =
  | { readonly artifact: RetailHealthArtifact; readonly artifact_revision: string; readonly generation?: PageGenerationV1; readonly kind: 'parent'; readonly page_context: PageContextV1; readonly temporal: TemporalControlV1 }
  | { readonly kind: 'window_unavailable'; readonly temporal: TemporalControlV1 };

/** Retail health › product detail, a baked child on the parent's own window (ADR-125). */
export interface RetailProductPresentation {
  readonly artifact: RetailProductDetailArtifact;
  readonly artifact_revision: string;
  readonly kind: 'child';
  readonly page_context: PageContextV1;
  readonly parent_revision: string;
  readonly temporal: TemporalControlV1;
}

export function retailHealthPresentationV2(result: PresentationResultV2): RetailHealthPresentation {
  if (result.kind === 'window_unavailable') return { kind: 'window_unavailable', temporal: result.temporal };
  const { page, temporal } = result;
  if (
    page.page_id !== 'retail-health' || page.route !== '/retail-health/' || page.actions.length !== 0 ||
    page.parent_revision !== undefined || page.kind !== 'retail_health' || page.artifact_id !== 'retail_health' ||
    page.artifact_type !== 'RetailHealthArtifact' || page.artifact_version !== 2 || 'product_ads' in page
  ) return fail();
  let artifact: RetailHealthArtifact;
  try { artifact = parseRetailHealthArtifact(page.artifact); } catch { return fail(); }
  if (!sameWindow(artifact.period, temporal.resolved.window)) return fail();
  return {
    artifact,
    artifact_revision: page.artifact_revision,
    ...(page.generation === undefined ? {} : { generation: page.generation }),
    kind: 'parent',
    page_context: page.page_context,
    temporal,
  };
}

export function retailProductPresentationV2(result: PresentationResultV2): RetailProductPresentation {
  if (result.kind !== 'page') return fail();
  const { page, temporal } = result;
  if (
    page.page_id !== 'retail-health' || page.route !== '/retail-health/' || page.actions.length !== 0 ||
    page.parent_revision === undefined || page.kind !== 'retail_product_detail' ||
    page.artifact_id !== 'retail_product_detail' || page.artifact_type !== 'RetailProductDetail' || page.artifact_version !== 1
  ) return fail();
  let artifact: RetailProductDetailArtifact;
  try { artifact = parseRetailProductDetail(page.artifact); } catch { return fail(); }
  // The child always shows its parent's window, so its figures add up to the parent's tiles.
  if (!sameWindow(artifact.period, temporal.resolved.window)) return fail();
  return {
    artifact,
    artifact_revision: page.artifact_revision,
    kind: 'child',
    page_context: page.page_context,
    parent_revision: page.parent_revision,
    temporal,
  };
}

/** Retail health's served page with its Products and ads part (render format 12), for that tab. */
export type ProductsAndAdsPresentation =
  | (Extract<RetailHealthPresentation, { readonly kind: 'parent' }> & { readonly product_ads: RetailProductAdsArtifact | null })
  | Extract<RetailHealthPresentation, { readonly kind: 'window_unavailable' }>;

/** Decode the page and its part; a body with no rows serves no part, every other body one. */
export function productsAndAdsPresentationV2(result: PresentationResultV2): ProductsAndAdsPresentation {
  if (result.kind === 'window_unavailable') return { kind: 'window_unavailable', temporal: result.temporal };
  const { product_ads: raw, ...page } = result.page;
  if (raw === undefined) return fail();
  const parent = retailHealthPresentationV2({ ...result, page });
  if (parent.kind !== 'parent') return fail();
  if (raw === null) return parent.artifact.rows.length === 0 ? { ...parent, product_ads: null } : fail();
  let product_ads: RetailProductAdsArtifact;
  try { product_ads = parseRetailProductAds(raw, parent.artifact); } catch { return fail(); }
  return { ...parent, product_ads };
}

/** Products and ads: one request for Retail health's page and the part every row reads. */
export async function fetchProductsAndAdsPresentationV2(
  session: string,
  selection?: TemporalSelectionV1,
  signal?: AbortSignal,
): Promise<ProductsAndAdsPresentation> {
  if (selection?.kind === 'custom') return fail();
  const body = { ...(requestBody(session, selection) as Record<string, JsonValue>), product_ads: true };
  const response = await callApi(RETAIL_HEALTH_PATH, 'POST', body, signal, { accept: PRESENTATION_V2_MEDIA_TYPE });
  const result = parsePresentationResultV2(await responseJson(response), 'retail-health');
  if (result.session_id !== session) return fail();
  return productsAndAdsPresentationV2(result);
}

export async function fetchRetailHealthPresentationV2(
  session: string,
  selection?: TemporalSelectionV1,
  signal?: AbortSignal,
): Promise<RetailHealthPresentation> {
  if (selection?.kind === 'custom') return fail();
  const response = await callApi(
    RETAIL_HEALTH_PATH,
    'POST',
    requestBody(session, selection),
    signal,
    { accept: PRESENTATION_V2_MEDIA_TYPE },
  );
  const result = parsePresentationResultV2(await responseJson(response), 'retail-health');
  if (result.session_id !== session) return fail();
  return retailHealthPresentationV2(result);
}

function productRequestBody(
  session: string,
  selectionReference: string,
  selection: TemporalSelectionV1,
  metric?: string,
): JsonValue {
  if (!text(session) || session.length > 200 || !REF.test(text(selectionReference))) return fail();
  if (selection.kind !== 'preset') return fail();
  enumValue(selection.preset_id, PRESETS);
  return {
    ...(metric === undefined ? {} : { metric: text(metric) }),
    selection_reference: selectionReference,
    session_id: session,
    temporal_selection: { kind: 'preset', preset_id: selection.preset_id },
  };
}

/** Open one product row's baked child; the request names the parent's own preset. */
export async function fetchRetailHealthProductV2(
  session: string,
  selectionReference: string,
  selection: TemporalSelectionV1,
  signal?: AbortSignal,
): Promise<RetailProductPresentation> {
  const response = await callApi(
    RETAIL_HEALTH_PRODUCT_PATH,
    'POST',
    productRequestBody(session, selectionReference, selection),
    signal,
    { accept: PRESENTATION_V2_MEDIA_TYPE },
  );
  const result = parsePresentationResultV2(await responseJson(response), 'retail-health');
  if (result.session_id !== session) return fail();
  return retailProductPresentationV2(result);
}

/** One product tile's focus, through the same row capability and the parent's preset. */
export async function fetchRetailProductMetricFocus(
  session: string,
  selectionReference: string,
  metric: string,
  selection: TemporalSelectionV1,
  signal?: AbortSignal,
): Promise<MetricFocusV4> {
  const response = await callApi(
    RETAIL_METRIC_FOCUS_PATH,
    'POST',
    productRequestBody(session, selectionReference, selection, metric),
    signal,
    { accept: PRESENTATION_V2_MEDIA_TYPE },
  );
  return decodedMetricFocus(await responseJson(response), metric);
}

export type ScorecardPresentation =
  | {
    readonly artifact: NetworkScorecardArtifact;
    readonly artifact_revision: string;
    readonly kind: 'parent';
    readonly page_context: PageContextV1;
    readonly temporal: TemporalControlV2;
  }
  | {
    /** The server's no-complete-period date, or null for another unavailable reason. */
    readonly expected_after: string | null;
    readonly kind: 'window_unavailable';
    /** The server's text for every unavailable reason (temporal contract rule 3). */
    readonly message: string;
    readonly reason_code: string;
    readonly temporal: TemporalControlV2;
  };

/** Bind one Scorecard result to its page declaration and its own served window. */
export function scorecardPresentationV2(result: PresentationResultV2<TemporalControlV2>): ScorecardPresentation {
  if (result.kind === 'window_unavailable') {
    return {
      expected_after: result.expected_after ?? null,
      kind: 'window_unavailable',
      message: result.message ?? fail(),
      reason_code: result.reason_code,
      temporal: result.temporal,
    };
  }
  const { page, temporal } = result;
  if (
    page.page_id !== 'scorecard' || page.route !== '/scorecard/' || page.actions.length !== 0 ||
    page.parent_revision !== undefined || page.kind !== 'network_scorecard' || page.artifact_id !== 'network_scorecard' ||
    page.artifact_type !== 'NetworkScorecardArtifact' || page.artifact_version !== 1
  ) return fail();
  let artifact: NetworkScorecardArtifact;
  try { artifact = parseNetworkScorecardArtifact(page.artifact); } catch { return fail(); }
  // The body is the window the control names, and a calendar period names itself the same.
  if (!sameWindow(artifact.period, temporal.resolved.window)) return fail();
  if (artifact.period_label !== temporal.resolved.period_label) return fail();
  return { artifact, artifact_revision: page.artifact_revision, kind: 'parent', page_context: page.page_context, temporal };
}

function declaredSelection(route: RouteName, selection: TemporalSelectionV1 | undefined): JsonValue | undefined {
  if (selection === undefined) return undefined;
  // The Scorecard accepts only a declared preset (Scorecard LLD §6): no Custom range.
  if (selection.kind !== 'preset') return fail();
  enumValue(selection.preset_id, presetPolicy(bindingByRoute[route]).candidate_presets);
  return { kind: 'preset', preset_id: selection.preset_id };
}

export async function fetchScorecardPresentationV2(
  session: string,
  selection?: TemporalSelectionV1,
  signal?: AbortSignal,
): Promise<ScorecardPresentation> {
  if (!text(session) || session.length > 200) return fail();
  const temporal = declaredSelection('scorecard', selection);
  const body: Record<string, JsonValue> = { session_id: session };
  if (temporal !== undefined) body.temporal_selection = temporal;
  const response = await callApi(SCORECARD_PATH, 'POST', body, signal, { accept: PRESENTATION_V2_MEDIA_TYPE });
  const result = parsePresentationResultCalendarV2(await responseJson(response), 'scorecard');
  if (result.session_id !== session) return fail();
  return scorecardPresentationV2(result);
}

export type NewCustomersPresentation =
  | {
    readonly artifact: NewCustomersArtifact;
    readonly artifact_revision: string;
    readonly kind: 'parent';
    readonly page_context: PageContextV1;
    readonly temporal: TemporalControlV2;
  }
  | {
    /** The server's no-complete-period date, or null for another unavailable reason. */
    readonly expected_after: string | null;
    readonly kind: 'window_unavailable';
    /** The server's text for every unavailable reason (temporal contract rule 3). */
    readonly message: string;
    readonly reason_code: string;
    readonly temporal: TemporalControlV2;
  };

/** Bind one New customers result to its page declaration and its own served window. */
export function newCustomersPresentationV2(result: PresentationResultV2<TemporalControlV2>): NewCustomersPresentation {
  if (result.kind === 'window_unavailable') {
    return {
      expected_after: result.expected_after ?? null,
      kind: 'window_unavailable',
      message: result.message ?? fail(),
      reason_code: result.reason_code,
      temporal: result.temporal,
    };
  }
  const { page, temporal } = result;
  if (
    page.page_id !== 'new-customers' || page.route !== '/new-customers/' || page.actions.length !== 0 ||
    page.parent_revision !== undefined || page.kind !== 'new_customers' || page.artifact_id !== 'new_customers' ||
    page.artifact_type !== 'NewCustomersArtifact' || page.artifact_version !== 2
  ) return fail();
  let artifact: NewCustomersArtifact;
  try { artifact = parseNewCustomersArtifact(page.artifact); } catch { return fail(); }
  // The body is the window the control names; a calendar period names itself the same.
  if (!sameWindow(artifact.period, temporal.resolved.window)) return fail();
  if ((temporal.resolved.period_label ?? null) !== (artifact.prior_period === null ? artifact.period_label : null)) return fail();
  if (artifact.preset !== (temporal.resolved.selection.preset_id ?? bindingByRoute['new-customers'].temporal_policy.default_preset)) return fail();
  // The artifact's page context is the envelope's (plan D5), so the two never disagree.
  const context = page.page_context;
  if (
    artifact.page_context_label !== context.label ||
    artifact.page_context_expires_at !== context.expires_at ||
    artifact.starter_questions.join('\u0000') !== context.starter_questions.join('\u0000')
  ) return fail();
  return { artifact, artifact_revision: page.artifact_revision, kind: 'parent', page_context: context, temporal };
}

export async function fetchNewCustomersPresentationV2(
  session: string,
  selection?: TemporalSelectionV1,
  signal?: AbortSignal,
): Promise<NewCustomersPresentation> {
  if (!text(session) || session.length > 200) return fail();
  const temporal = declaredSelection('new-customers', selection);
  const body: Record<string, JsonValue> = { session_id: session };
  if (temporal !== undefined) body.temporal_selection = temporal;
  const response = await callApi(NEW_CUSTOMERS_PATH, 'POST', body, signal, { accept: PRESENTATION_V2_MEDIA_TYPE });
  const result = parsePresentationResultCalendarV2(await responseJson(response), 'new-customers');
  if (result.session_id !== session) return fail();
  return newCustomersPresentationV2(result);
}

export type ReachPresentation =
  | {
    readonly artifact: ReachAndFunnelArtifact;
    readonly artifact_revision: string;
    readonly kind: 'parent';
    readonly page_context: PageContextV1;
    readonly temporal: TemporalControlV2;
  }
  | {
    readonly expected_after: string | null;
    readonly kind: 'window_unavailable';
    readonly message: string;
    readonly reason_code: string;
    readonly temporal: TemporalControlV2;
  };

/** Bind one Reach result to its declaration, requested calendar preset, context and served period. */
export function reachPresentationV2(
  result: PresentationResultV2<TemporalControlV2>,
  requestedSelection: TemporalSelectionV1 | undefined,
): ReachPresentation {
  const returnedSelection = result.temporal.resolved.selection;
  const selectionMatches = requestedSelection === undefined
    ? returnedSelection.kind === 'default' && returnedSelection.preset_id === null
    : requestedSelection.kind === 'preset' && returnedSelection.kind === 'preset' &&
      returnedSelection.preset_id === requestedSelection.preset_id;
  if (!selectionMatches) return fail();
  if (result.kind === 'window_unavailable') {
    return {
      expected_after: result.expected_after ?? null,
      kind: 'window_unavailable',
      message: result.message ?? fail(),
      reason_code: result.reason_code,
      temporal: result.temporal,
    };
  }
  const { page, temporal } = result;
  if (
    page.page_id !== 'reach' || page.route !== '/reach/' || page.actions.length !== 0 ||
    page.parent_revision !== undefined || page.kind !== 'reach' || page.artifact_id !== 'reach' ||
    page.artifact_type !== 'ReachAndFunnelArtifact' || page.artifact_version !== 1
  ) return fail();
  let artifact: ReachAndFunnelArtifact;
  try { artifact = parseReachAndFunnelArtifact(page.artifact); } catch { return fail(); }
  if (!sameWindow(artifact.period, temporal.resolved.window)) return fail();
  if (artifact.period.label !== temporal.resolved.period_label || artifact.period.grain !== temporal.resolved.display_grain) return fail();
  const selectedPreset = temporal.resolved.selection.preset_id ?? bindingByRoute.reach.temporal_policy.default_preset;
  if (artifact.preset !== selectedPreset) return fail();
  if (artifact.markets.some((market) => market.insights.some((slot) => slot.artifact_revision !== page.artifact_revision))) return fail();
  const context = page.page_context;
  if (
    artifact.page_context_expires_at !== context.expires_at ||
    artifact.page_context_label !== context.label ||
    artifact.starter_questions.join('\u0000') !== context.starter_questions.join('\u0000')
  ) return fail();
  return { artifact, artifact_revision: page.artifact_revision, kind: 'parent', page_context: context, temporal };
}

/** Fetch Reach with only the session and an optional declared calendar preset. */
export async function fetchReachPresentationV2(
  session: string,
  selection?: TemporalSelectionV1,
  signal?: AbortSignal,
): Promise<ReachPresentation> {
  if (!text(session) || session.length > 200) return fail();
  const temporal = declaredSelection('reach', selection);
  const body: Record<string, JsonValue> = { session_id: session };
  if (temporal !== undefined) body.temporal_selection = temporal;
  const response = await callApi(REACH_PATH, 'POST', body, signal, { accept: PRESENTATION_V2_MEDIA_TYPE });
  const result = parsePresentationResultCalendarV2(await responseJson(response), 'reach');
  if (result.session_id !== session) return fail();
  return reachPresentationV2(result, selection);
}

export type RetentionPresentation =
  | {
    readonly artifact: RetentionCohortArtifact;
    readonly artifact_revision: string;
    readonly kind: 'parent';
    readonly page_context: PageContextV1;
    readonly temporal: VintageControlV2;
  }
  | {
    readonly expected_after: string | null;
    readonly kind: 'window_unavailable';
    readonly message: string;
    readonly reason_code: string;
    readonly temporal: VintageControlV2;
  };

/** Bind the Retention body and context to the exact resolved vintage and artifact revision. */
export function retentionPresentationV2(result: PresentationResultV2<VintageControlV2>): RetentionPresentation {
  if (result.kind === 'window_unavailable') {
    if (result.temporal.resolved !== null) return fail();
    return {
      expected_after: result.expected_after ?? null,
      kind: 'window_unavailable', message: result.message ?? fail(), reason_code: result.reason_code,
      temporal: result.temporal,
    };
  }
  const returned = result.temporal.resolved;
  if (returned === null) return fail();
  const { page, temporal } = result;
  if (
    page.page_id !== 'retention' || page.route !== '/retention/' || page.actions.length !== 0
    || page.parent_revision !== undefined || page.kind !== 'retention' || page.artifact_id !== 'retention'
    || page.artifact_type !== 'RetentionCohortArtifact' || page.artifact_version !== 1
  ) return fail();
  let artifact: RetentionCohortArtifact;
  try { artifact = parseRetentionCohortArtifact(page.artifact); } catch { return fail(); }
  if (
    artifact.vintage.as_of_month !== returned.as_of_month
    || artifact.vintage.label !== returned.period_label
    || !sameWindow(artifact.vintage, returned.window)
    || artifact.markets.some((market) => market.insights.some((slot) => slot.artifact_revision !== page.artifact_revision))
  ) return fail();
  const context = page.page_context;
  if (
    artifact.page_context_expires_at !== context.expires_at
    || artifact.page_context_label !== context.label
    || artifact.starter_questions.join('\u0000') !== context.starter_questions.join('\u0000')
  ) return fail();
  return { artifact, artifact_revision: page.artifact_revision, kind: 'parent', page_context: context, temporal };
}

/** Fetch Retention's latest vintage with only the session: the page offers no selection. */
export async function fetchRetentionPresentationV2(session: string, signal?: AbortSignal): Promise<RetentionPresentation> {
  if (!text(session) || session.length > 200) return fail();
  const body: Record<string, JsonValue> = { session_id: session };
  const response = await callApi(RETENTION_PATH, 'POST', body, signal, { accept: PRESENTATION_V2_MEDIA_TYPE });
  const result = parsePresentationResultVintageV2(await responseJson(response), 'retention');
  if (result.session_id !== session) return fail();
  return retentionPresentationV2(result);
}

export type AudiencesPresentation =
  | {
    readonly artifact: AudienceProfileArtifact;
    readonly artifact_revision: string;
    readonly kind: 'parent';
    readonly page_context: PageContextV1;
    readonly temporal: TemporalControlV2;
  }
  | {
    readonly expected_after: string | null;
    readonly kind: 'window_unavailable';
    readonly message: string;
    readonly reason_code: string;
    readonly temporal: TemporalControlV2;
  };

/** Bind Audiences to its single declared month, context, and every delivery insight revision. */
export function audiencesPresentationV2(
  result: PresentationResultV2<TemporalControlV2>,
): AudiencesPresentation {
  if (result.kind === 'window_unavailable') return {
    expected_after: result.expected_after ?? null,
    kind: 'window_unavailable', message: result.message ?? fail(), reason_code: result.reason_code,
    temporal: result.temporal,
  };
  const { page, temporal } = result;
  if (
    page.page_id !== 'audiences' || page.route !== '/audiences/' || page.actions.length !== 0
    || page.parent_revision !== undefined || page.kind !== 'audiences' || page.artifact_id !== 'audiences'
    || page.artifact_type !== 'AudienceProfileArtifact' || page.artifact_version !== 2
  ) return fail();
  let artifact: AudienceProfileArtifact;
  try { artifact = parseAudienceProfileArtifact(page.artifact); } catch { return fail(); }
  if (!sameWindow(artifact.month, temporal.resolved.window) || artifact.month.label !== temporal.resolved.period_label) return fail();
  const selected = temporal.resolved.selection.preset_id ?? bindingByRoute.audiences.temporal_policy.default_preset;
  if (selected !== 'last_complete_month') return fail();
  if (artifact.markets.some((market) => market.views.some((view) => view.deliveries.some((delivery) => delivery.insights.some((slot) => slot.artifact_revision !== page.artifact_revision))))) return fail();
  const context = page.page_context;
  if (
    artifact.page_context_expires_at !== context.expires_at
    || artifact.page_context_label !== context.label
    || artifact.starter_questions.join('\u0000') !== context.starter_questions.join('\u0000')
  ) return fail();
  return { artifact, artifact_revision: page.artifact_revision, kind: 'parent', page_context: context, temporal };
}

/**
 * Fetch one baked Audiences part (ADR-141): a campaign's page, a later profile page or a later
 * index shard. The server serves it from the parent's index, never measuring; a part of another
 * revision comes back as it is, for the caller's revision guard to compare.
 */
export async function fetchAudiencesPart(
  session: string,
  request: AudiencesPartRequest,
  signal?: AbortSignal,
): Promise<{ readonly parent_revision: string; readonly part: AudiencesPart }> {
  if (!text(session) || session.length > 200) return fail();
  const selector: JsonValue = request.kind === 'campaign'
    ? { campaign: { page: request.page, ref: request.ref } }
    : request.kind === 'profile'
      ? { profile: { page: request.page, path: request.path } }
      : { campaign_index: { shard: request.shard } };
  const body: Record<string, JsonValue> = { session_id: session, ...(selector as Record<string, JsonValue>) };
  const response = await callApi(AUDIENCES_PATH, 'POST', body, signal, { accept: PRESENTATION_V2_MEDIA_TYPE });
  const value = await responseJson(response);
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return fail();
  const item = value as Record<string, unknown>;
  const keys = Object.keys(item).sort();
  if (keys.join(',') !== 'kind,parent_revision,part,session_id' || item.kind !== 'audiences_part' || item.session_id !== session) return fail();
  let parsed: { readonly parent_revision: string; readonly part: AudiencesPart };
  try { parsed = parseAudiencesPart(item.part, request); } catch { return fail(); }
  if (parsed.parent_revision !== item.parent_revision) return fail();
  return parsed;
}
/** Fetch the single-preset Audiences page; the page exposes no browser date control. */
export async function fetchAudiencesPresentationV2(
  session: string,
  selection?: TemporalSelectionV1,
  signal?: AbortSignal,
): Promise<AudiencesPresentation> {
  if (!text(session) || session.length > 200) return fail();
  const temporal = declaredSelection('audiences', selection);
  const body: Record<string, JsonValue> = { session_id: session };
  if (temporal !== undefined) body.temporal_selection = temporal;
  const response = await callApi(AUDIENCES_PATH, 'POST', body, signal, { accept: PRESENTATION_V2_MEDIA_TYPE });
  const result = parsePresentationResultCalendarV2(await responseJson(response), 'audiences');
  if (result.session_id !== session) return fail();
  return audiencesPresentationV2(result);
}
