// SPDX-License-Identifier: MIT-0
import { type Answer } from './answer';
import { displayFigure } from './exact-format';
/** Strict browser decoder for server-owned AudienceProfileArtifact v2 and its parts (ADR-141). */

export type AudiencesOutcome = 'complete' | 'empty' | 'insufficient';
export type AudienceViewId = 'asi_all_exposed' | 'dsp_exposed_only';
export type ProfilePath = 'asi_in_market' | 'asi_lifestyle' | 'dsp_exposed_only';

export interface AudienceShare { readonly value: string; readonly text: string }
export interface AudiencePaidState {
  readonly feature_observed: boolean;
  readonly period_published: boolean;
  readonly expected_after: string;
  readonly latest_available_period: { readonly start: string; readonly end: string } | null;
}
export interface AudienceInsight {
  readonly absence_class: string | null;
  readonly absence_reason: string | null;
  readonly artifact_revision: string;
  readonly evidence_line: string | null;
  readonly id: 'segment-over-index';
  readonly output_class: 'perspective';
  readonly prompt_version: string;
  readonly state: 'filled' | 'silent';
  readonly text: string | null;
}
export interface AudienceDelivery {
  readonly profile_path: ProfilePath;
  readonly label: string;
  readonly returned: boolean;
  readonly total_returned: boolean;
  readonly total_text: string | null;
  readonly profiled_buyers_total: number | null;
  readonly profiled_buyers_total_text: string | null;
  readonly profiled_exposed_total: number | null;
  readonly profiled_exposed_total_text: string | null;
  readonly first_month_note: string | null;
  readonly roster_note: string | null;
  readonly paid_state: AudiencePaidState | null;
  readonly period_text: string | null;
  readonly insights: readonly AudienceInsight[];
  /** The path's 50-row pages; page 0 rides in the parent, later pages are parts. */
  readonly page_count: number;
  /** The server's "Segments 1–50 of 150" for page 0, or null when one page holds them all. */
  readonly paging_text: string | null;
}
export interface AudienceSegmentNames {
  readonly segment_id: string;
  readonly name: string | null;
  readonly display_name: string;
  readonly provider_label: 'Amazon' | 'Third party' | null;
  readonly category_level_1: string | null;
  readonly category_path: string | null;
}
export interface AudienceRow extends AudienceSegmentNames {
  readonly profile_path: ProfilePath;
  readonly state: 'value' | 'below_minimum' | 'not_available';
  readonly text: string | null;
  readonly segment_buyers: number;
  readonly segment_buyers_text: string;
  readonly segment_exposed_shoppers: number;
  readonly segment_buyers_new_to_brand: number;
  readonly segment_buyers_new_to_brand_text: string;
  readonly segment_buyer_share: AudienceShare | null;
  readonly segment_exposed_share: AudienceShare | null;
  readonly segment_index: string | null;
  readonly segment_index_text: string | null;
}
export interface AudienceUnavailableRow extends AudienceSegmentNames {
  readonly profile_path: ProfilePath;
  readonly state: 'not_available';
  readonly text: string;
}
export interface AudienceView {
  readonly view: AudienceViewId;
  readonly label: string;
  readonly paid_state: 'not_observed' | 'observed' | 'standard';
  readonly callout: string | null;
  readonly caption: string;
  readonly segment_ids_sentence: string;
  readonly buyer_label: string;
  readonly display_minimum_text: string;
  readonly deliveries: readonly AudienceDelivery[];
  readonly rows: readonly AudienceRow[];
  readonly not_available: readonly AudienceUnavailableRow[];
}
export interface CampaignAudienceRow extends AudienceSegmentNames {
  readonly segment_impressions: number | null;
  readonly segment_impressions_text: string | null;
  readonly segment_cpm: { readonly value: string; readonly currency: string; readonly text: string } | null;
  readonly cpm_text: string | null;
  readonly segment_roas_attributed: string | null;
  readonly segment_roas_attributed_text: string | null;
  readonly roas_text: string | null;
}
export type CampaignEntryState = 'available' | 'withdrawn' | 'refused';
export interface CampaignIndexEntry {
  /** An opaque per-artifact reference (`campaign-N`, by rank), never the campaign's key. */
  readonly campaign_ref: string;
  readonly campaign_label: string;
  readonly segment_count: number;
  readonly page_count: number;
  readonly state: CampaignEntryState;
  /** The withdrawal note or the refusal sentence; null when available. */
  readonly text: string | null;
}
export interface AudienceCampaigns {
  readonly limit_text: string;
  readonly pending_text: string | null;
  /** The section's own sentence when the month's campaign figures aren't complete. */
  readonly refused_text: string | null;
  readonly segment_ids_sentence: string;
  readonly basis_label: string;
  readonly campaign_count: number;
  readonly index_shards: number;
  /** Index shard 0; later shards are parts. */
  readonly index: readonly CampaignIndexEntry[];
}
export interface AudienceMarket {
  readonly outcome: AudiencesOutcome;
  readonly default_view: AudienceViewId;
  readonly views: readonly [AudienceView, AudienceView];
  readonly campaign_audiences: AudienceCampaigns;
}
export interface AudienceEvidenceSlot {
  readonly slot_label: string;
  readonly role_label: string;
  readonly missing_dates: readonly { readonly start: string; readonly end: string }[];
  readonly reason_text: string;
  readonly recovery_text: string | null;
}
export interface AudienceProfileArtifact {
  readonly type: 'AudienceProfileArtifact';
  readonly version: 2;
  /** The publication's revision; every part names it as its `parent_revision`. */
  readonly revision: string;
  readonly outcome: AudiencesOutcome;
  readonly month: { readonly start: string; readonly end: string; readonly label: string };
  readonly display_minimum: number;
  readonly markets: readonly [AudienceMarket];
  readonly boundary: readonly [string, string];
  readonly evidence_status: { readonly version: 2; readonly slots: readonly AudienceEvidenceSlot[] };
  readonly page_context_label: string;
  readonly page_context_expires_at: string;
  readonly starter_questions: readonly string[];
}

const PAID_PATHS = ['asi_in_market', 'asi_lifestyle'] as const;
const DSP_PATHS = ['dsp_exposed_only'] as const;
// Year 0000 is outside the server's `date` domain (year 1 onward), so both sides refuse it.
const DATE = /^(?!0000)\d{4}-\d{2}-\d{2}$/;
// Canonical fixed-point text exactly as the server's `_decimal` writes it (audiences.py `_decimal_text`).
// At most 28 digits (26 before the point), the server's `_MAX_DECIMAL_DIGITS` bound.
const INDEX = /^(0|[1-9]\d{0,25})\.\d{2}$/;
const MONEY = /^-?(0|[1-9]\d{0,25})\.\d{2}$/;
const SHARE = /^(0|1)\.(\d{4})$/;
const REF = /^[A-Za-z0-9_-]{43}$/;
// A publication's revision as the server's `PresetArtifactRevision` writes it: a lowercase SHA-256.
const PUBLICATION_REVISION = /^[0-9a-f]{64}$/;
// Server-pinned sentences (domain.semantics.audience_segments and domain.presentation.audiences).
const PAID_CALLOUT = 'Needs the Amazon Insights paid feature (Audience Segment Insights). No data has arrived for this workspace.';
const SEGMENT_IDS_SENTENCE = "DSP behaviour segments and Amazon audience segments (paid) use different IDs, so they're never linked or compared as one list.";
const BOUNDARY_TEXT = "This describes an audience you can build in your own tools. Adlytics doesn't create or activate audiences.";
const TOTAL_NOT_AVAILABLE_TEXT = "Not available: this month's profiled total didn't come back, so no share or index is computed.";
const ROSTER_GAP_TEXTS = [
  'Amazon returned too few shoppers for this segment this month. It was listed last month.',
  "Not available: Amazon didn't return every figure for this segment this month.",
] as const;
const MONTH_NAMES = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'] as const;
const fail = (): never => { throw new TypeError('The Audiences artifact is malformed.'); };
const exact = (value: unknown, keys: readonly string[]): Record<string, unknown> => {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return fail();
  const found = Object.keys(value);
  if (found.length !== keys.length || found.some((key) => !keys.includes(key))) return fail();
  return value as Record<string, unknown>;
};
const array = (value: unknown): readonly unknown[] => Array.isArray(value) ? value : fail();
// Blank is JavaScript whitespace or Python's extra separators (U+001C–U+001F, U+0085): the union
// the server's `_BLANK` also refuses, so neither runtime accepts text the other calls blank.
const BLANK = /^[\s\x1c-\x1f\x85]*$/;
const text = (value: unknown): string => typeof value === 'string' && !BLANK.test(value) ? value : fail();
const nullableText = (value: unknown): string | null => value === null ? null : text(value);
const isoDate = (value: unknown): string => {
  const candidate = text(value);
  const parsed = new Date(`${candidate}T00:00:00Z`);
  return DATE.test(candidate) && !Number.isNaN(parsed.valueOf()) && parsed.toISOString().slice(0, 10) === candidate ? candidate : fail();
};
const oneOf = <const T extends readonly string[]>(value: unknown, values: T): T[number] => {
  const candidate = text(value);
  return values.includes(candidate) ? candidate as T[number] : fail();
};
const nonnegativeInteger = (value: unknown): number => Number.isSafeInteger(value) && (value as number) >= 0 ? value as number : fail();
const positiveInteger = (value: unknown): number => Number.isSafeInteger(value) && (value as number) > 0 ? value as number : fail();
const canonical = (value: unknown, pattern: RegExp): string => pattern.test(text(value)) ? value as string : fail();
const nullableCount = (value: unknown): number | null => value === null ? null : nonnegativeInteger(value);
const countPair = (value: unknown, display: unknown): { readonly value: number | null; readonly display: string | null } => {
  const parsed = nullableCount(value);
  const parsedDisplay = nullableText(display);
  if (parsed === null ? parsedDisplay !== null : parsedDisplay !== String(parsed)) return fail();
  return { display: parsedDisplay, value: parsed };
};

function names(value: Record<string, unknown>): AudienceSegmentNames {
  const provider = value.provider_label === null ? null : oneOf(value.provider_label, ['Amazon', 'Third party'] as const);
  return {
    category_level_1: nullableText(value.category_level_1), category_path: nullableText(value.category_path),
    display_name: text(value.display_name), name: nullableText(value.name), provider_label: provider,
    segment_id: text(value.segment_id),
  };
}
function share(value: unknown): AudienceShare | null {
  if (value === null) return null;
  const item = exact(value, ['text', 'value']);
  const parsedValue = text(item.value);
  const parsedText = text(item.text);
  const match = SHARE.exec(parsedValue);
  if (match === null || !/^\d{1,3}\.\d%$/.test(parsedText)) return fail();
  const fixedPoint = BigInt(`${match[1]}${match[2]}`);
  if (fixedPoint > 10000n) return fail();
  let percentageTenths = fixedPoint / 10n;
  const discardedDigit = fixedPoint % 10n;
  if (discardedDigit > 5n || (discardedDigit === 5n && percentageTenths % 2n === 1n)) percentageTenths += 1n;
  const canonicalText = `${percentageTenths / 10n}.${percentageTenths % 10n}%`;
  if (parsedText !== canonicalText) return fail();
  return { text: parsedText, value: parsedValue };
}
function paidState(value: unknown): AudiencePaidState {
  const item = exact(value, ['expected_after', 'feature_observed', 'latest_available_period', 'period_published']);
  if (typeof item.feature_observed !== 'boolean' || typeof item.period_published !== 'boolean') return fail();
  const latest = item.latest_available_period === null ? null : exact(item.latest_available_period, ['end', 'start']);
  const parsedLatest = latest === null ? null : { end: isoDate(latest.end), start: isoDate(latest.start) };
  if (parsedLatest === null ? item.period_published : parsedLatest.end < parsedLatest.start) return fail();
  return { expected_after: isoDate(item.expected_after), feature_observed: item.feature_observed, latest_available_period: parsedLatest, period_published: item.period_published };
}
function insight(value: unknown): AudienceInsight {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return fail();
  const raw = value as Record<string, unknown>;
  const state = oneOf(raw.state, ['filled', 'silent'] as const);
  const item = exact(value, state === 'silent'
    ? ['absence_class', 'absence_reason', 'artifact_revision', 'evidence_line', 'id', 'output_class', 'prompt_version', 'state', 'text']
    : ['artifact_revision', 'evidence_line', 'id', 'output_class', 'prompt_version', 'state', 'text']);
  const parsed = {
    absence_class: item.absence_class === undefined ? null : nullableText(item.absence_class),
    absence_reason: item.absence_reason === undefined ? null : nullableText(item.absence_reason),
    artifact_revision: text(item.artifact_revision), evidence_line: nullableText(item.evidence_line),
    id: oneOf(item.id, ['segment-over-index'] as const), output_class: oneOf(item.output_class, ['perspective'] as const),
    prompt_version: text(item.prompt_version), state, text: nullableText(item.text),
  };
  if (!REF.test(parsed.artifact_revision)) return fail();
  if (state === 'filled' ? parsed.text === null || parsed.evidence_line === null : parsed.text !== null || parsed.evidence_line !== null || parsed.absence_class === null || parsed.absence_reason === null) return fail();
  return parsed;
}

const monthName = (iso: string): string => MONTH_NAMES[Number(iso.slice(5, 7)) - 1]!;
/** The period texts the server's `resolve_audiences_payload` can serve for one baked paid state. */
function servedPeriodTexts(state: AudiencePaidState, monthLabel: string): readonly (string | null)[] {
  if (state.period_published) return [null];
  if (!state.feature_observed) return [PAID_CALLOUT];
  const day = state.expected_after;
  const latest = state.latest_available_period;
  return [
    `Figures for ${monthLabel} are expected after ${Number(day.slice(8))} ${monthName(day).slice(0, 3)} ${day.slice(0, 4)}.`,
    `Figures for ${monthLabel} didn't arrive.${latest === null ? '' : ` The latest available is ${monthName(latest.start)} ${latest.start.slice(0, 4)}.`}`,
  ];
}
function delivery(value: unknown, expectedPath: ProfilePath, paid: boolean, monthLabel: string): AudienceDelivery {
  const item = exact(value, ['first_month_note', 'insights', 'label', 'page_count', 'paging_text', 'paid_state', 'period_text', 'profile_path', 'profiled_buyers_total', 'profiled_buyers_total_text', 'profiled_exposed_total', 'profiled_exposed_total_text', 'returned', 'roster_note', 'total_returned', 'total_text']);
  if (item.profile_path !== expectedPath || typeof item.returned !== 'boolean' || typeof item.total_returned !== 'boolean') return fail();
  const buyersTotal = countPair(item.profiled_buyers_total, item.profiled_buyers_total_text);
  const exposedTotal = countPair(item.profiled_exposed_total, item.profiled_exposed_total_text);
  const parsedPaid = item.paid_state === null ? null : paidState(item.paid_state);
  const period_text = nullableText(item.period_text);
  if (!paid) {
    if (parsedPaid !== null || period_text !== null) return fail();
  } else if (parsedPaid === null || !servedPeriodTexts(parsedPaid, monthLabel).includes(period_text)) return fail();
  const insights = array(item.insights).map(insight);
  if (insights.length !== 1) return fail();
  const total_text = nullableText(item.total_text);
  if (item.total_returned && !item.returned) return fail();
  if ((buyersTotal.value !== null) !== item.total_returned || (exposedTotal.value !== null) !== item.total_returned) return fail();
  if (total_text !== (item.returned && !item.total_returned ? TOTAL_NOT_AVAILABLE_TEXT : null)) return fail();
  if (!item.total_returned && insights[0]!.state !== 'silent') return fail();
  if (item.returned && !item.total_returned && insights[0]!.absence_reason !== TOTAL_NOT_AVAILABLE_TEXT) return fail();
  const page_count = nonnegativeInteger(item.page_count);
  const paging_text = nullableText(item.paging_text);
  if ((page_count >= 1) !== item.returned || (paging_text !== null) !== (page_count > 1)) return fail();
  return {
    first_month_note: nullableText(item.first_month_note), insights, label: text(item.label), page_count, paging_text, paid_state: parsedPaid,
    period_text, profile_path: expectedPath, profiled_buyers_total: buyersTotal.value, profiled_buyers_total_text: buyersTotal.display,
    profiled_exposed_total: exposedTotal.value, profiled_exposed_total_text: exposedTotal.display, returned: item.returned, roster_note: nullableText(item.roster_note),
    total_returned: item.total_returned, total_text,
  };
}
function deliveryOf(value: unknown, deliveries: readonly AudienceDelivery[]): AudienceDelivery {
  const found = deliveries.find((entry) => entry.profile_path === value);
  return found !== undefined && found.returned ? found : fail();
}
function row(value: unknown, deliveries: readonly AudienceDelivery[]): AudienceRow {
  const item = exact(value, ['category_level_1', 'category_path', 'display_name', 'name', 'profile_path', 'provider_label', 'segment_buyer_share', 'segment_buyers', 'segment_buyers_new_to_brand', 'segment_buyers_new_to_brand_text', 'segment_buyers_text', 'segment_exposed_share', 'segment_exposed_shoppers', 'segment_id', 'segment_index', 'segment_index_text', 'state', 'text']);
  const owner = deliveryOf(item.profile_path, deliveries);
  const profile_path = owner.profile_path;
  const buyers = countPair(item.segment_buyers, item.segment_buyers_text);
  const newToBrand = countPair(item.segment_buyers_new_to_brand, item.segment_buyers_new_to_brand_text);
  if (buyers.value === null || newToBrand.value === null) return fail();
  const segment_index = item.segment_index === null ? null : canonical(item.segment_index, INDEX);
  const segment_index_text = nullableText(item.segment_index_text);
  if ((segment_index === null) !== (segment_index_text === null) || (segment_index !== null && segment_index_text !== `${segment_index}x`)) return fail();
  const buyerShare = share(item.segment_buyer_share);
  const exposedShare = share(item.segment_exposed_share);
  const state = oneOf(item.state, ['value', 'below_minimum', 'not_available'] as const);
  const rowText = nullableText(item.text);
  const sharesPresent = buyerShare !== null && exposedShare !== null;
  if (state === 'value' ? !sharesPresent || segment_index === null || rowText !== null
    : state === 'below_minimum' ? !sharesPresent || segment_index !== null || rowText === null
      : buyerShare !== null || exposedShare !== null || segment_index !== null || rowText !== TOTAL_NOT_AVAILABLE_TEXT) return fail();
  if ((state === 'not_available') === owner.total_returned) return fail();
  return {
    ...names(item), profile_path, segment_buyer_share: buyerShare, segment_buyers: buyers.value,
    segment_buyers_new_to_brand: newToBrand.value, segment_buyers_new_to_brand_text: newToBrand.display!,
    segment_buyers_text: buyers.display!, segment_exposed_share: exposedShare,
    segment_exposed_shoppers: nonnegativeInteger(item.segment_exposed_shoppers), segment_index, segment_index_text,
    state, text: rowText,
  };
}
function unavailable(value: unknown, deliveries: readonly AudienceDelivery[]): AudienceUnavailableRow {
  const item = exact(value, ['category_level_1', 'category_path', 'display_name', 'name', 'profile_path', 'provider_label', 'segment_id', 'state', 'text']);
  if (item.state !== 'not_available') return fail();
  return { ...names(item), profile_path: deliveryOf(item.profile_path, deliveries).profile_path, state: 'not_available', text: oneOf(item.text, ROSTER_GAP_TEXTS) };
}
function view(value: unknown, expected: AudienceViewId, paths: readonly ProfilePath[], monthLabel: string): AudienceView {
  const item = exact(value, ['buyer_label', 'callout', 'caption', 'deliveries', 'display_minimum_text', 'label', 'not_available', 'paid_state', 'rows', 'segment_ids_sentence', 'view']);
  if (item.view !== expected) return fail();
  const paid_state = oneOf(item.paid_state, expected === 'dsp_exposed_only' ? ['standard'] as const : ['not_observed', 'observed'] as const);
  const callout = nullableText(item.callout);
  if (callout !== (paid_state === 'not_observed' ? PAID_CALLOUT : null) || item.segment_ids_sentence !== SEGMENT_IDS_SENTENCE) return fail();
  const deliveriesValue = array(item.deliveries);
  if (deliveriesValue.length !== paths.length) return fail();
  const deliveries = deliveriesValue.map((entry, index) => delivery(entry, paths[index]!, expected === 'asi_all_exposed', monthLabel));
  if (expected === 'asi_all_exposed' && deliveries.some((entry) => entry.paid_state?.feature_observed !== (paid_state === 'observed'))) return fail();
  const rows = array(item.rows).map((entry) => row(entry, deliveries));
  const missing = array(item.not_available).map((entry) => unavailable(entry, deliveries));
  for (const roster of [rows, missing]) {
    if (roster.some((entry, index) => index > 0 && paths.indexOf(roster[index - 1]!.profile_path) > paths.indexOf(entry.profile_path))) return fail();
  }
  if (paths.some((path) => [...rows, ...missing].filter((entry) => entry.profile_path === path).length > PAGE_ROWS)) return fail();
  return { buyer_label: text(item.buyer_label), callout, caption: text(item.caption), deliveries, display_minimum_text: text(item.display_minimum_text), label: text(item.label), not_available: missing, paid_state, rows, segment_ids_sentence: SEGMENT_IDS_SENTENCE, view: expected };
}
function campaignRow(value: unknown): CampaignAudienceRow {
  const item = exact(value, ['category_level_1', 'category_path', 'cpm_text', 'display_name', 'name', 'provider_label', 'roas_text', 'segment_cpm', 'segment_id', 'segment_impressions', 'segment_impressions_text', 'segment_roas_attributed', 'segment_roas_attributed_text']);
  const impressions = countPair(item.segment_impressions, item.segment_impressions_text);
  const cpm = item.segment_cpm === null ? null : exact(item.segment_cpm, ['currency', 'text', 'value']);
  const segment_cpm = cpm === null ? null : { currency: text(cpm.currency), text: text(cpm.text), value: canonical(cpm.value, MONEY) };
  const cpm_text = nullableText(item.cpm_text);
  if ((segment_cpm === null) !== (cpm_text !== null) || (segment_cpm !== null && segment_cpm.text !== `${segment_cpm.value} ${segment_cpm.currency}`)) return fail();
  const segment_roas_attributed = item.segment_roas_attributed === null ? null : canonical(item.segment_roas_attributed, MONEY);
  const segment_roas_attributed_text = nullableText(item.segment_roas_attributed_text);
  const roas_text = nullableText(item.roas_text);
  if ((segment_roas_attributed === null) !== (segment_roas_attributed_text === null) || (segment_roas_attributed === null) !== (roas_text !== null) || (segment_roas_attributed !== null && segment_roas_attributed_text !== `${segment_roas_attributed}x`)) return fail();
  return { ...names(item), cpm_text, roas_text, segment_cpm, segment_impressions: impressions.value, segment_impressions_text: impressions.display, segment_roas_attributed, segment_roas_attributed_text };
}
const PAGE_ROWS = 50;
const INDEX_SHARD_ENTRIES = 100;
const CAMPAIGN_REF = /^campaign-[1-9]\d{0,5}$/;
const pagesFor = (rows: number): number => Math.max(1, Math.ceil(rows / PAGE_ROWS));
/** Index entries numbered `campaign-N` by rank from `firstRank`, judged as the server judges them. */
function indexEntries(value: unknown, firstRank: number): readonly CampaignIndexEntry[] {
  return array(value).map((raw, offset) => {
    const item = exact(raw, ['campaign_label', 'campaign_ref', 'page_count', 'segment_count', 'state', 'text']);
    if (item.campaign_ref !== `campaign-${firstRank + offset}`) return fail();
    const state = oneOf(item.state, ['available', 'withdrawn', 'refused'] as const);
    const entryText = nullableText(item.text);
    const segment_count = nonnegativeInteger(item.segment_count);
    const page_count = nonnegativeInteger(item.page_count);
    if ((entryText === null) !== (state === 'available') || page_count !== (state === 'refused' ? 0 : pagesFor(segment_count))) return fail();
    return { campaign_label: text(item.campaign_label), campaign_ref: item.campaign_ref as string, page_count, segment_count, state, text: entryText };
  });
}
function campaigns(value: unknown): AudienceCampaigns {
  const item = exact(value, ['basis_label', 'campaign_count', 'index', 'index_shards', 'limit_text', 'pending_text', 'refused_text', 'segment_ids_sentence']);
  const campaign_count = nonnegativeInteger(item.campaign_count);
  const index_shards = positiveInteger(item.index_shards);
  const pending_text = nullableText(item.pending_text);
  const refused_text = nullableText(item.refused_text);
  const index = indexEntries(item.index, 1);
  if (item.segment_ids_sentence !== SEGMENT_IDS_SENTENCE) return fail();
  if ((pending_text !== null || refused_text !== null) && campaign_count > 0) return fail();
  if (pending_text !== null && refused_text !== null) return fail();
  if (index_shards !== Math.max(1, Math.ceil(campaign_count / INDEX_SHARD_ENTRIES)) || index.length !== Math.min(campaign_count, INDEX_SHARD_ENTRIES)) return fail();
  return { basis_label: text(item.basis_label), campaign_count, index, index_shards, limit_text: text(item.limit_text), pending_text, refused_text, segment_ids_sentence: SEGMENT_IDS_SENTENCE };
}
function market(value: unknown, monthLabel: string): AudienceMarket {
  const item = exact(value, ['campaign_audiences', 'default_view', 'outcome', 'views']);
  const viewsValue = array(item.views);
  if (viewsValue.length !== 2) return fail();
  const paid = view(viewsValue[0], 'asi_all_exposed', PAID_PATHS, monthLabel);
  const dsp = view(viewsValue[1], 'dsp_exposed_only', DSP_PATHS, monthLabel);
  const campaign_audiences = campaigns(item.campaign_audiences);
  const outcome = oneOf(item.outcome, ['complete', 'empty', 'insufficient'] as const);
  const default_view = oneOf(item.default_view, ['asi_all_exposed', 'dsp_exposed_only'] as const);
  if (default_view !== (paid.paid_state === 'observed' && paid.deliveries.some((entry) => entry.returned) ? 'asi_all_exposed' : 'dsp_exposed_only')) return fail();
  if (outcome !== 'complete' && ![paid, dsp].flatMap((entry) => entry.deliveries).some((entry) => entry.period_text !== null || entry.total_text !== null || (entry.insights[0]?.absence_reason ?? null) !== null)) return fail();
  return {
    campaign_audiences,
    default_view,
    outcome, views: [paid, dsp],
  };
}
function evidence(value: unknown): AudienceProfileArtifact['evidence_status'] {
  const item = exact(value, ['slots', 'version']);
  if (item.version !== 2) return fail();
  const slots = array(item.slots).map((value) => {
    const slot = exact(value, ['missing_dates', 'reason_text', 'recovery_text', 'role_label', 'slot_label']);
    return { missing_dates: array(slot.missing_dates).map((value) => { const period = exact(value, ['end', 'start']); const parsed = { end: isoDate(period.end), start: isoDate(period.start) }; return parsed.end < parsed.start ? fail() : parsed; }), reason_text: text(slot.reason_text), recovery_text: nullableText(slot.recovery_text), role_label: text(slot.role_label), slot_label: text(slot.slot_label) };
  });
  return { slots, version: 2 };
}

export function parseAudienceProfileArtifact(value: unknown): AudienceProfileArtifact {
  const item = exact(value, ['boundary', 'display_minimum', 'evidence_status', 'markets', 'month', 'outcome', 'page_context_expires_at', 'page_context_label', 'revision', 'starter_questions', 'type', 'version']);
  if (item.type !== 'AudienceProfileArtifact' || item.version !== 2) return fail();
  const revision = canonical(item.revision, PUBLICATION_REVISION);
  const monthValue = exact(item.month, ['end', 'label', 'start']);
  const month = { end: isoDate(monthValue.end), label: text(monthValue.label), start: isoDate(monthValue.start) };
  if (month.end < month.start) return fail();
  const marketsValue = array(item.markets);
  if (marketsValue.length !== 1) return fail();
  const markets: [AudienceMarket] = [market(marketsValue[0], month.label)];
  const outcome = oneOf(item.outcome, ['complete', 'empty', 'insufficient'] as const);
  if (markets[0].outcome !== outcome) return fail();
  const boundaryValue = array(item.boundary);
  if (boundaryValue.length !== 2 || boundaryValue[0] !== BOUNDARY_TEXT || boundaryValue[1] !== SEGMENT_IDS_SENTENCE) return fail();
  const starter_questions = array(item.starter_questions).map(text);
  if (starter_questions.length !== (outcome === 'complete' ? 2 : 0)) return fail();
  return { boundary: [BOUNDARY_TEXT, SEGMENT_IDS_SENTENCE], display_minimum: positiveInteger(item.display_minimum), evidence_status: evidence(item.evidence_status), markets, month, outcome, page_context_expires_at: text(item.page_context_expires_at), page_context_label: text(item.page_context_label), revision, starter_questions, type: 'AudienceProfileArtifact', version: 2 };
}

/**
 * The Audiences answer (plan template). The server ranks each delivery's rows by index, so the
 * first shown row of a delivery whose `segment-over-index` slot is filled names the segment; the
 * figures are its served shares and index. A silent slot reads the rule's own line, an
 * unavailable one its served reason.
 */
export function audiencesAnswer(artifact: AudienceProfileArtifact): Answer {
  const market = artifact.markets[0];
  const view = market.views.find((entry) => entry.view === market.default_view) ?? market.views[0];
  for (const delivery of view.deliveries) {
    const slot = delivery.insights.find((entry) => entry.id === 'segment-over-index') ?? null;
    if (slot?.state !== 'filled') continue;
    const row = view.rows.find((entry) => entry.profile_path === delivery.profile_path && entry.state === 'value') ?? null;
    if (row === null || row.segment_buyer_share === null || row.segment_exposed_share === null || row.segment_index === null) continue;
    const index = displayFigure('segment_index', row.segment_index, 'ratio', null).text;
    return {
      kind: 'answer',
      // UX sweep fix 10 (T1L-6): the served segment name leads, without the provider suffix the
      // display name adds; a segment the server can't name keeps its served display name.
      text: `Shoppers in ${row.name ?? row.display_name} are ${row.segment_buyer_share.text} of your buyers but ${row.segment_exposed_share.text} of shoppers reached: ${index} as common among your buyers.`,
    };
  }
  const reason = view.deliveries.flatMap((delivery) => delivery.insights).find((slot) => slot.absence_reason !== null && slot.absence_class !== 'evaluated-quiet')?.absence_reason ?? null;
  return { kind: 'fallback', text: reason ?? `No segment met the over-index rule in ${artifact.month.label}.` };
}

export interface AudienceCampaignPage {
  readonly kind: 'campaign';
  readonly campaign_ref: string;
  readonly campaign_label: string;
  readonly page: number;
  readonly page_count: number;
  readonly paging_text: string | null;
  readonly state: 'available' | 'withdrawn';
  readonly text: string | null;
  readonly rows: readonly CampaignAudienceRow[];
}
export interface AudienceProfilePage {
  readonly kind: 'profile';
  readonly profile_path: ProfilePath;
  readonly page: number;
  readonly page_count: number;
  readonly paging_text: string;
  readonly rows: readonly AudienceRow[];
  readonly not_available: readonly AudienceUnavailableRow[];
}
export interface AudienceIndexShard {
  readonly kind: 'campaign_index';
  readonly shard: number;
  readonly entries: readonly CampaignIndexEntry[];
}
export type AudiencesPart = AudienceCampaignPage | AudienceProfilePage | AudienceIndexShard;
/** What the browser asks the server for: one campaign page, one later profile page or one later index shard. */
export type AudiencesPartRequest =
  | { readonly kind: 'campaign'; readonly ref: string; readonly page: number }
  | { readonly kind: 'profile'; readonly path: ProfilePath; readonly page: number }
  | { readonly kind: 'campaign_index'; readonly shard: number };

const PART_TYPES = { campaign: 'AudienceCampaignPage', campaign_index: 'AudienceCampaignIndex', profile: 'AudienceProfilePage' } as const;
const PART_KEYS = {
  campaign: ['campaign_label', 'campaign_ref', 'page', 'page_count', 'paging_text', 'rows', 'state', 'text'],
  campaign_index: ['entries', 'shard'],
  profile: ['not_available', 'page', 'page_count', 'paging_text', 'profile_path', 'rows', 'total_returned'],
} as const;

/**
 * Decode one served part strictly, as the server's `require_audiences_part_payload` does, and
 * check it is the part asked for under `parentRevision`. A part of another revision is refused
 * by the caller's revision guard, not here: `parent_revision` is returned for it to compare.
 */
export function parseAudiencesPart(value: unknown, request: AudiencesPartRequest): { readonly parent_revision: string; readonly part: AudiencesPart } {
  const item = exact(value, ['parent_revision', 'type', 'version', ...PART_KEYS[request.kind]]);
  if (item.type !== PART_TYPES[request.kind] || item.version !== 1) return fail();
  const parent_revision = canonical(item.parent_revision, PUBLICATION_REVISION);
  if (request.kind === 'campaign_index') {
    const shard = positiveInteger(item.shard);
    const entries = indexEntries(item.entries, shard * INDEX_SHARD_ENTRIES + 1);
    if (shard !== request.shard || entries.length < 1 || entries.length > INDEX_SHARD_ENTRIES) return fail();
    return { parent_revision, part: { entries, kind: 'campaign_index', shard } };
  }
  const page = nonnegativeInteger(item.page);
  const page_count = positiveInteger(item.page_count);
  const paging_text = nullableText(item.paging_text);
  if (page !== request.page || page >= page_count) return fail();
  if (request.kind === 'profile') {
    if (item.profile_path !== request.path || typeof item.total_returned !== 'boolean' || paging_text === null || page < 1) return fail();
    const owner: AudienceDelivery = { first_month_note: null, insights: [], label: '', page_count, paging_text, paid_state: null, period_text: null, profile_path: request.path, profiled_buyers_total: null, profiled_buyers_total_text: null, profiled_exposed_total: null, profiled_exposed_total_text: null, returned: true, roster_note: null, total_returned: item.total_returned, total_text: null };
    const rows = array(item.rows).map((entry) => row(entry, [owner]));
    const not_available = array(item.not_available).map((entry) => unavailable(entry, [owner]));
    if (rows.length + not_available.length > PAGE_ROWS) return fail();
    return { parent_revision, part: { kind: 'profile', not_available, page, page_count, paging_text, profile_path: request.path, rows } };
  }
  if (item.campaign_ref !== request.ref || !CAMPAIGN_REF.test(request.ref)) return fail();
  const state = oneOf(item.state, ['available', 'withdrawn'] as const);
  const partText = nullableText(item.text);
  const rows = array(item.rows).map(campaignRow);
  if ((partText === null) !== (state === 'available') || rows.length > PAGE_ROWS || (state === 'withdrawn' && (rows.length > 0 || paging_text !== null))) return fail();
  return { parent_revision, part: { campaign_label: text(item.campaign_label), campaign_ref: request.ref, kind: 'campaign', page, page_count, paging_text, rows, state, text: partText } };
}
