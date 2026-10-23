// SPDX-License-Identifier: MIT-0
import { type Answer } from './answer';
import { displayFigure } from './exact-format';
/** Strict browser decoder for server-owned AudienceProfileArtifact v1. */

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
  /** An opaque per-artifact reference (`campaign-N`), never the campaign's canonical key. */
  readonly campaign_ref: string;
  readonly campaign_label: string;
  readonly segment_impressions: number | null;
  readonly segment_impressions_text: string | null;
  readonly segment_cpm: { readonly value: string; readonly currency: string; readonly text: string } | null;
  readonly cpm_text: string | null;
  readonly segment_roas_attributed: string | null;
  readonly segment_roas_attributed_text: string | null;
  readonly roas_text: string | null;
}
export interface AudienceMarket {
  readonly outcome: AudiencesOutcome;
  readonly default_view: AudienceViewId;
  readonly views: readonly [AudienceView, AudienceView];
  readonly campaign_audiences: {
    readonly limit_text: string;
    readonly pending_text: string | null;
    /** The server's "Showing N of M campaigns" when the page shows fewer than pack 8 listed. */
    readonly shown_text: string | null;
    readonly segment_ids_sentence: string;
    readonly basis_label: string;
    readonly rows: readonly CampaignAudienceRow[];
  };
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
  readonly version: 1;
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
  const item = exact(value, ['first_month_note', 'insights', 'label', 'paid_state', 'period_text', 'profile_path', 'profiled_buyers_total', 'profiled_buyers_total_text', 'profiled_exposed_total', 'profiled_exposed_total_text', 'returned', 'roster_note', 'total_returned', 'total_text']);
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
  return {
    first_month_note: nullableText(item.first_month_note), insights, label: text(item.label), paid_state: parsedPaid,
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
  return { buyer_label: text(item.buyer_label), callout, caption: text(item.caption), deliveries, display_minimum_text: text(item.display_minimum_text), label: text(item.label), not_available: missing, paid_state, rows, segment_ids_sentence: SEGMENT_IDS_SENTENCE, view: expected };
}
/** References number campaigns `campaign-1`, `campaign-2`, … in row order, one label each, rows grouped. */
function campaignRefsGrouped(rows: readonly CampaignAudienceRow[]): boolean {
  const labels = new Map<string, string>();
  let last: string | undefined;
  for (const row of rows) {
    if (!labels.has(row.campaign_ref) && row.campaign_ref !== `campaign-${labels.size + 1}`) return false;
    if ((labels.get(row.campaign_ref) ?? row.campaign_label) !== row.campaign_label) return false;
    if (labels.has(row.campaign_ref) && last !== row.campaign_ref) return false;
    labels.set(row.campaign_ref, row.campaign_label);
    last = row.campaign_ref;
  }
  return true;
}
function campaignRow(value: unknown): CampaignAudienceRow {
  const item = exact(value, ['campaign_label', 'campaign_ref', 'category_level_1', 'category_path', 'cpm_text', 'display_name', 'name', 'provider_label', 'roas_text', 'segment_cpm', 'segment_id', 'segment_impressions', 'segment_impressions_text', 'segment_roas_attributed', 'segment_roas_attributed_text']);
  const impressions = countPair(item.segment_impressions, item.segment_impressions_text);
  const cpm = item.segment_cpm === null ? null : exact(item.segment_cpm, ['currency', 'text', 'value']);
  const segment_cpm = cpm === null ? null : { currency: text(cpm.currency), text: text(cpm.text), value: canonical(cpm.value, MONEY) };
  const cpm_text = nullableText(item.cpm_text);
  if ((segment_cpm === null) !== (cpm_text !== null) || (segment_cpm !== null && segment_cpm.text !== `${segment_cpm.value} ${segment_cpm.currency}`)) return fail();
  const segment_roas_attributed = item.segment_roas_attributed === null ? null : canonical(item.segment_roas_attributed, MONEY);
  const segment_roas_attributed_text = nullableText(item.segment_roas_attributed_text);
  const roas_text = nullableText(item.roas_text);
  if ((segment_roas_attributed === null) !== (segment_roas_attributed_text === null) || (segment_roas_attributed === null) !== (roas_text !== null) || (segment_roas_attributed !== null && segment_roas_attributed_text !== `${segment_roas_attributed}x`)) return fail();
  return { ...names(item), campaign_label: text(item.campaign_label), campaign_ref: text(item.campaign_ref), cpm_text, roas_text, segment_cpm, segment_impressions: impressions.value, segment_impressions_text: impressions.display, segment_roas_attributed, segment_roas_attributed_text };
}
const SHOWN = /^Showing ([1-9][0-9]*) of ([1-9][0-9]*) campaigns$/;
/** The shown note, judged as the server judges it: exact copy, fewer shown than listed, never pending. */
function shownNote(value: unknown, campaigns: number, pending: boolean): string | null {
  const note = nullableText(value);
  if (note === null) return null;
  const match = SHOWN.exec(note);
  if (match === null || pending) return fail();
  const shown = Number(match[1]);
  if (!(campaigns <= shown && shown < Number(match[2]))) return fail();
  return note;
}
function market(value: unknown, monthLabel: string): AudienceMarket {
  const item = exact(value, ['campaign_audiences', 'default_view', 'outcome', 'views']);
  const viewsValue = array(item.views);
  if (viewsValue.length !== 2) return fail();
  const paid = view(viewsValue[0], 'asi_all_exposed', PAID_PATHS, monthLabel);
  const dsp = view(viewsValue[1], 'dsp_exposed_only', DSP_PATHS, monthLabel);
  const campaign = exact(item.campaign_audiences, ['basis_label', 'limit_text', 'pending_text', 'rows', 'segment_ids_sentence', 'shown_text']);
  const outcome = oneOf(item.outcome, ['complete', 'empty', 'insufficient'] as const);
  const default_view = oneOf(item.default_view, ['asi_all_exposed', 'dsp_exposed_only'] as const);
  if (default_view !== (paid.paid_state === 'observed' && paid.deliveries.some((entry) => entry.returned) ? 'asi_all_exposed' : 'dsp_exposed_only')) return fail();
  const campaignRows = array(campaign.rows).map(campaignRow);
  const pending_text = nullableText(campaign.pending_text);
  if (!campaignRefsGrouped(campaignRows)) return fail();
  const shown_text = shownNote(campaign.shown_text, new Set(campaignRows.map((row) => row.campaign_ref)).size, pending_text !== null);
  if (campaign.segment_ids_sentence !== SEGMENT_IDS_SENTENCE || (pending_text !== null && campaignRows.length > 0)) return fail();
  if (outcome !== 'complete' && ![paid, dsp].flatMap((entry) => entry.deliveries).some((entry) => entry.period_text !== null || entry.total_text !== null || (entry.insights[0]?.absence_reason ?? null) !== null)) return fail();
  return {
    campaign_audiences: { basis_label: text(campaign.basis_label), limit_text: text(campaign.limit_text), pending_text, rows: campaignRows, segment_ids_sentence: SEGMENT_IDS_SENTENCE, shown_text },
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
  const item = exact(value, ['boundary', 'display_minimum', 'evidence_status', 'markets', 'month', 'outcome', 'page_context_expires_at', 'page_context_label', 'starter_questions', 'type', 'version']);
  if (item.type !== 'AudienceProfileArtifact' || item.version !== 1) return fail();
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
  return { boundary: [BOUNDARY_TEXT, SEGMENT_IDS_SENTENCE], display_minimum: positiveInteger(item.display_minimum), evidence_status: evidence(item.evidence_status), markets, month, outcome, page_context_expires_at: text(item.page_context_expires_at), page_context_label: text(item.page_context_label), starter_questions, type: 'AudienceProfileArtifact', version: 1 };
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
