// SPDX-License-Identifier: MIT-0
import { type Answer, changeClause, windowText } from './answer';
import { displayFigure, formatCount, formatCurrency } from './exact-format';
import { type Change, CHANGE_KEY, comparisonFields, PERCENTAGE_KEY, parseChange } from './change';
import { isDisplayValue } from './display-value';
import { coverageEntry, EVIDENCE_STATUS_KEY, type EvidenceStatusV2, isWithheld, parseEvidenceStatus } from './evidence-status';
import { hasNextStep, type InsightNextStep, slotNextStep } from './insight-next-step';
import type { DateWindow, IsoInstant } from './presentation';

const OPAQUE_REFERENCE = /^[A-Za-z0-9_-]{43}$/;
const ISO_DATE = /^(\d{4})-(\d{2})-(\d{2})$/;
const ISO_INSTANT =
  /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{6}))?\+00:00$/;
const DECIMAL = /^([+-]?)(0|[1-9][0-9]*)(?:\.([0-9]+))?$/;
/** Every metric value: the exact value, its semantics, and the server's display value. */
const METRIC_VALUE_FIELDS = ['basis', 'display_value', 'grain', 'period', 'units', 'value'] as const;
const PERCENTAGE = /^[+-]?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?%$/;

export const CAMPAIGN_SUMMARY_GRAIN = ['period', 'campaign', 'marketplace'] as const;
export const CAMPAIGN_PURCHASE_SERIES_GRAIN = ['date', 'campaign', 'marketplace'] as const;
export const CAMPAIGN_ATTRIBUTION_BOUNDARIES = [
  'ROAS is the governed ad-attributed metric for advertised products over a 14-day attribution window.',
  'Campaign-attributed purchases and ROAS do not establish Vendor retail impact or product-level ROAS.',
  'Campaign-attributed evidence is descriptive and does not establish incrementality or causality.',
] as const;

export const campaignListReasons = [
  'empty_covered',
  'safe_signature_collision',
  'missing_catalog',
  'missing_current_publication',
  'coverage_hole',
  'suppressed',
  'truncated',
  'incompatible_scope',
] as const;
export type CampaignListReason = typeof campaignListReasons[number];

export const campaignRecoveryActions = [
  'change_period',
  'change_campaign',
  'compare_previous_period',
  'inspect_channel_context',
  'contact_workspace_data_owner',
] as const;
export type CampaignRecoveryAction = typeof campaignRecoveryActions[number];

export const campaignEvidenceSlots = [
  'purchases',
  'attributed_roas',
  'spend',
  'purchase_series',
  'prior_purchases',
  'prior_attributed_roas',
  'prior_spend',
] as const;
export type CampaignEvidenceSlot = typeof campaignEvidenceSlots[number];

export const campaignEvidenceConditions = [
  'available',
  'empty_covered',
  'missing_current_publication',
  'missing_comparator_publication',
  'suppressed',
  'truncated',
  'coverage_hole',
  'incompatible_scope',
] as const;
export type CampaignEvidenceCondition = typeof campaignEvidenceConditions[number];

export const campaignEvidenceStatusConditions = [
  'available',
  'empty_covered',
  'missing_current_publication',
  'missing_comparator_publication',
  'suppressed',
  'truncated',
  'coverage_hole',
  'incomplete_daily_series',
  'series_total_divergence',
  'incompatible_scope',
] as const;
export type CampaignEvidenceStatusCondition = typeof campaignEvidenceStatusConditions[number];

export const campaignEvidenceStatusRoles = ['current', 'prior', 'daily', 'prior_daily'] as const;
export type CampaignEvidenceStatusRole = typeof campaignEvidenceStatusRoles[number];

/** One (slot, role)'s served status: accepted, or a named refusal with served words (ADR-072). */
export interface CampaignEvidenceStatus {
  readonly condition: CampaignEvidenceStatusCondition;
  readonly days_expected: number | null;
  readonly days_present: number | null;
  readonly explanation: string;
  readonly recovery_reading: string | null;
  readonly role: CampaignEvidenceStatusRole;
  readonly slot: string;
}

export const campaignSummarySlots = ['purchases', 'attributed_roas', 'spend'] as const;
export type CampaignSummarySlot = typeof campaignSummarySlots[number];

export const campaignInsufficiencyReasons = [
  'missing_current_publication',
  'missing_comparator_publication',
  'empty_covered',
  'suppressed',
  'truncated',
  'coverage_hole',
  'incompatible_scope',
  'too_short_series',
  'one_sided_comparison',
] as const;
export type CampaignInsufficiencyReason = typeof campaignInsufficiencyReasons[number];

export type CampaignListOutcome = 'choices' | 'empty' | 'ambiguous' | 'insufficient';
export type CampaignDetailOutcome = 'selected' | 'insufficient';
export type CampaignNumber = number | string;
export type CampaignUnitKind = 'count' | 'currency' | 'ratio';
export type CampaignComparisonReason = 'one_sided' | 'prior_zero_percentage_undefined';

export interface SafeCampaignDisplaySignature {
  readonly channel: string;
  readonly effective_name: string | null;
  readonly effective_name_status: 'available' | 'missing' | 'unavailable_for_window';
  readonly effective_name_valid_from: string | null;
  readonly effective_name_valid_to: string | null;
  readonly marketplace: string;
  readonly platform: string;
}

export interface CampaignSelectionCapability {
  readonly expires_at: IsoInstant;
  readonly selection_reference: string;
}

export interface CampaignSelectionChoice {
  readonly capability: CampaignSelectionCapability;
  readonly signature: SafeCampaignDisplaySignature;
}

export interface CampaignListEvidence {
  readonly actual_coverage: readonly DateWindow[];
  readonly coverage_holes: readonly DateWindow[];
  readonly freshness_qualifiers: readonly string[];
  readonly requested_period: DateWindow;
  readonly source: string;
}

export interface CampaignSelectionListPage {
  readonly choices: readonly CampaignSelectionChoice[];
  readonly evidence: readonly CampaignListEvidence[];
  readonly kind: 'campaign_list';
  readonly outcome: CampaignListOutcome;
  readonly period: DateWindow;
  readonly reason_codes: readonly CampaignListReason[];
  readonly recovery_actions: readonly CampaignRecoveryAction[];
  readonly truncated: boolean;
}

export interface CampaignUnits {
  readonly currency_code: string | null;
  readonly kind: CampaignUnitKind;
}

export interface CampaignMetricValue {
  readonly basis: 'ad_attributed';
  /** The server's two-place display for currency, null otherwise (ADR-084). */
  readonly display_value: string | null;
  readonly grain: typeof CAMPAIGN_SUMMARY_GRAIN;
  readonly period: DateWindow;
  readonly units: CampaignUnits;
  readonly value: CampaignNumber;
}

export interface CampaignSummary {
  readonly attributed_roas: CampaignMetricValue | null;
  readonly period: DateWindow;
  readonly purchases: CampaignMetricValue | null;
  readonly spend: CampaignMetricValue | null;
}

export interface CampaignPurchasePoint {
  readonly date: string;
  readonly purchases: CampaignNumber;
}

export interface CampaignPurchaseSeries {
  readonly basis: 'ad_attributed';
  readonly grain: typeof CAMPAIGN_PURCHASE_SERIES_GRAIN;
  readonly period: DateWindow;
  readonly points: readonly CampaignPurchasePoint[];
  readonly units: CampaignUnits;
}

export interface CampaignSuppression {
  readonly reason: string | null;
  readonly rows_suppressed: boolean;
}

export interface CampaignSlotEvidence {
  readonly actual_coverage: readonly DateWindow[];
  readonly basis: 'ad_attributed' | null;
  readonly condition: CampaignEvidenceCondition;
  readonly coverage_holes: readonly DateWindow[];
  readonly freshness_qualifiers: readonly string[];
  readonly grain: typeof CAMPAIGN_SUMMARY_GRAIN | typeof CAMPAIGN_PURCHASE_SERIES_GRAIN;
  readonly requested_period: DateWindow;
  readonly row_count: number;
  readonly slot: CampaignEvidenceSlot;
  readonly suppression: CampaignSuppression;
  readonly truncated: boolean;
  readonly units: CampaignUnits | null;
}

export interface CampaignMetricComparison {
  readonly absolute_change: CampaignNumber | null;
  /** The server's change (ADR-121); null only when one side is absent. */
  readonly change: Change | null;
  readonly current: CampaignMetricValue | null;
  readonly prior: CampaignMetricValue | null;
  readonly reason: CampaignComparisonReason | null;
  readonly slot: CampaignSummarySlot;
}

export interface CampaignPriorComparison {
  readonly current_period: DateWindow;
  readonly metrics: readonly CampaignMetricComparison[];
  readonly prior_period: DateWindow;
}

export interface CampaignAttributionDetailPage {
  readonly boundaries: typeof CAMPAIGN_ATTRIBUTION_BOUNDARIES;
  readonly comparison: CampaignPriorComparison | null;
  readonly evidence_by_slot: readonly CampaignSlotEvidence[];
  readonly insufficiency_reasons: readonly CampaignInsufficiencyReason[];
  readonly kind: 'campaign_detail';
  readonly outcome: CampaignDetailOutcome;
  readonly period: DateWindow;
  readonly prior_period: DateWindow | null;
  readonly purchase_series: CampaignPurchaseSeries | null;
  readonly selection: SafeCampaignDisplaySignature;
  readonly summary: CampaignSummary;
}

export class CampaignAttributionContractError extends Error {
  constructor() {
    super('The campaign-attribution response was not valid.');
    this.name = 'CampaignAttributionContractError';
  }
}

const fail = (): never => {
  throw new CampaignAttributionContractError();
};

type UnknownRecord = Record<string, unknown>;

function exact(value: unknown, keys: readonly string[]): UnknownRecord {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return fail();
  const item = value as UnknownRecord;
  const actual = Object.keys(item);
  if (actual.length !== keys.length || !actual.every((key) => keys.includes(key))) return fail();
  return item;
}

function nonblank(value: unknown): string {
  if (typeof value !== 'string' || !value.trim()) return fail();
  return value;
}

function nullableText(value: unknown): string | null {
  return value === null ? null : nonblank(value);
}

function member<const Values extends readonly string[]>(
  value: unknown,
  values: Values,
): Values[number] {
  if (typeof value !== 'string' || !values.includes(value)) return fail();
  return value as Values[number];
}

function unique<T>(values: readonly T[]): boolean {
  return new Set(values).size === values.length;
}

function isoDate(value: unknown): string {
  if (typeof value !== 'string') return fail();
  const match = ISO_DATE.exec(value);
  if (match === null || match[1] === '0000') return fail();
  const parsed = new Date(`${value}T00:00:00Z`);
  if (Number.isNaN(parsed.valueOf()) || parsed.toISOString().slice(0, 10) !== value) return fail();
  return value;
}

function isoInstant(value: unknown): IsoInstant {
  if (typeof value !== 'string') return fail();
  const match = ISO_INSTANT.exec(value);
  if (match === null || match[1] === '0000') return fail();
  const parts = match.slice(1, 7).map(Number);
  const [year, month, day, hour, minute, second] = parts as [
    number,
    number,
    number,
    number,
    number,
    number,
  ];
  const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  const maximumDay = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][
    month - 1
  ];
  if (
    maximumDay === undefined ||
    day < 1 ||
    day > maximumDay ||
    hour > 23 ||
    minute > 59 ||
    second > 59
  ) return fail();
  return value as IsoInstant;
}

function period(value: unknown): DateWindow {
  const item = exact(value, ['end', 'start']);
  const start = isoDate(item.start);
  const end = isoDate(item.end);
  if (end < start) return fail();
  return { end, start };
}

export function sameCampaignPeriod(left: DateWindow, right: DateWindow): boolean {
  return left.start === right.start && left.end === right.end;
}

function periodDay(value: string): number {
  const parsed = Date.parse(`${value}T00:00:00Z`);
  if (!Number.isFinite(parsed)) return fail();
  return parsed / 86_400_000;
}

function previousEqualPeriod(current: DateWindow, prior: DateWindow): boolean {
  const currentStart = periodDay(current.start);
  const currentEnd = periodDay(current.end);
  const priorStart = periodDay(prior.start);
  const priorEnd = periodDay(prior.end);
  return priorEnd + 1 === currentStart && priorEnd - priorStart === currentEnd - currentStart;
}

function orderedPeriods(value: unknown): readonly DateWindow[] {
  if (!Array.isArray(value)) return fail();
  const ranges = value.map(period);
  for (let index = 1; index < ranges.length; index += 1) {
    const prior = ranges[index - 1];
    const current = ranges[index];
    if (
      prior === undefined ||
      current === undefined ||
      prior.start > current.start ||
      (prior.start === current.start && prior.end > current.end) ||
      prior.end >= current.start
    ) return fail();
  }
  return ranges;
}

function isCoveredBy(outer: DateWindow, inner: DateWindow): boolean {
  return outer.start <= inner.start && outer.end >= inner.end;
}

function orderedStrings(value: unknown): readonly string[] {
  if (!Array.isArray(value)) return fail();
  const values = value.map(nonblank);
  return unique(values) ? values : fail();
}

function uniqueEnumArray<const Values extends readonly string[]>(
  value: unknown,
  values: Values,
): readonly Values[number][] {
  if (!Array.isArray(value)) return fail();
  const parsed = value.map((item) => member(item, values));
  return unique(parsed) ? parsed : fail();
}

function canonicalEnumArray<const Values extends readonly string[]>(
  value: unknown,
  order: Values,
): readonly Values[number][] {
  if (!Array.isArray(value)) return fail();
  const values = value.map((item) => member(item, order));
  const canonical = order.filter((item) => values.includes(item));
  if (
    !unique(values) ||
    canonical.length !== values.length ||
    values.some((item, index) => item !== canonical[index])
  ) return fail();
  return values;
}

function signature(value: unknown): SafeCampaignDisplaySignature {
  const item = exact(value, [
    'channel',
    'effective_name',
    'effective_name_status',
    'effective_name_valid_from',
    'effective_name_valid_to',
    'marketplace',
    'platform',
  ]);
  const effective_name = nullableText(item.effective_name);
  const effective_name_status = member(
    item.effective_name_status,
    ['available', 'missing', 'unavailable_for_window'] as const,
  );
  const effective_name_valid_from = item.effective_name_valid_from === null
    ? null
    : isoDate(item.effective_name_valid_from);
  const effective_name_valid_to = item.effective_name_valid_to === null
    ? null
    : isoDate(item.effective_name_valid_to);
  if (
    (effective_name !== null && effective_name_valid_from === null) ||
    (effective_name === null &&
      (effective_name_valid_from !== null || effective_name_valid_to !== null)) ||
    (effective_name_valid_from !== null &&
      effective_name_valid_to !== null &&
      effective_name_valid_to <= effective_name_valid_from) ||
    (effective_name_status === 'available' && effective_name === null) ||
    (effective_name_status === 'missing' &&
      (effective_name !== null ||
        effective_name_valid_from !== null ||
        effective_name_valid_to !== null))
  ) return fail();
  return {
    channel: nonblank(item.channel),
    effective_name,
    effective_name_status,
    effective_name_valid_from,
    effective_name_valid_to,
    marketplace: nonblank(item.marketplace),
    platform: nonblank(item.platform),
  };
}

function signatureSortKey(value: SafeCampaignDisplaySignature): readonly string[] {
  return [
    value.platform,
    value.marketplace,
    value.channel,
    value.effective_name ?? '',
    value.effective_name_status,
    value.effective_name_valid_from ?? '',
    value.effective_name_valid_to ?? '',
  ];
}

function compareStrings(left: readonly string[], right: readonly string[]): number {
  for (let index = 0; index < left.length; index += 1) {
    const leftValue = left[index];
    const rightValue = right[index];
    if (leftValue === undefined || rightValue === undefined) return fail();
    if (leftValue < rightValue) return -1;
    if (leftValue > rightValue) return 1;
  }
  return left.length - right.length;
}

function signatureKey(value: SafeCampaignDisplaySignature): string {
  return JSON.stringify(signatureSortKey(value));
}

export function sameCampaignSignature(
  left: SafeCampaignDisplaySignature,
  right: SafeCampaignDisplaySignature,
): boolean {
  return signatureKey(left) === signatureKey(right);
}

function capability(value: unknown): CampaignSelectionCapability {
  const item = exact(value, ['expires_at', 'selection_reference']);
  const selection_reference = nonblank(item.selection_reference);
  if (!OPAQUE_REFERENCE.test(selection_reference)) return fail();
  return { expires_at: isoInstant(item.expires_at), selection_reference };
}

function choice(value: unknown): CampaignSelectionChoice {
  const item = exact(value, ['capability', 'signature']);
  return { capability: capability(item.capability), signature: signature(item.signature) };
}

function listEvidence(value: unknown, requested: DateWindow): CampaignListEvidence {
  const item = exact(value, [
    'actual_coverage',
    'coverage_holes',
    'freshness_qualifiers',
    'requested_period',
    'source',
  ]);
  const requested_period = period(item.requested_period);
  const actual_coverage = orderedPeriods(item.actual_coverage);
  const coverage_holes = orderedPeriods(item.coverage_holes);
  const freshness_qualifiers = orderedStrings(item.freshness_qualifiers);
  if (
    !sameCampaignPeriod(requested_period, requested) ||
    coverage_holes.some((hole) => !isCoveredBy(requested_period, hole))
  ) return fail();
  return {
    actual_coverage,
    coverage_holes,
    freshness_qualifiers,
    requested_period,
    source: nonblank(item.source),
  };
}

function booleanValue(value: unknown): boolean {
  return typeof value === 'boolean' ? value : fail();
}

function listOutcome(value: unknown): CampaignListOutcome {
  return member(value, ['choices', 'empty', 'ambiguous', 'insufficient'] as const);
}

export function parseCampaignSelectionListPage(value: unknown): CampaignSelectionListPage {
  const item = exact(value, [
    'choices',
    'evidence',
    'kind',
    'outcome',
    'period',
    'reason_codes',
    'recovery_actions',
    'truncated',
  ]);
  if (item.kind !== 'campaign_list' || !Array.isArray(item.choices) || !Array.isArray(item.evidence)) {
    return fail();
  }
  const selectedPeriod = period(item.period);
  const choices = item.choices.map(choice);
  const evidence = item.evidence.map((entry) => listEvidence(entry, selectedPeriod));
  const outcome = listOutcome(item.outcome);
  const reason_codes = canonicalEnumArray(item.reason_codes, campaignListReasons);
  const recovery_actions = uniqueEnumArray(item.recovery_actions, campaignRecoveryActions);
  const signatureKeys = choices.map((entry) => signatureKey(entry.signature));
  const capabilities = choices.map((entry) => entry.capability.selection_reference);
  if (
    evidence.length === 0 ||
    !unique(signatureKeys) ||
    !unique(capabilities) ||
    choices.some((entry, index) => {
      const prior = choices[index - 1];
      return prior !== undefined &&
        compareStrings(signatureSortKey(prior.signature), signatureSortKey(entry.signature)) >= 0;
    })
  ) return fail();

  const insufficiencyReasons = new Set<CampaignListReason>([
    'missing_catalog',
    'missing_current_publication',
    'coverage_hole',
    'suppressed',
    'truncated',
    'incompatible_scope',
  ]);
  const valid = outcome === 'choices'
    ? choices.length > 0 && reason_codes.length === 0 && recovery_actions.length === 0
    : outcome === 'empty'
      ? choices.length === 0 &&
        reason_codes.length === 1 &&
        reason_codes[0] === 'empty_covered'
      : outcome === 'ambiguous'
        ? choices.length === 0 &&
          reason_codes.length === 1 &&
          reason_codes[0] === 'safe_signature_collision' &&
          recovery_actions.length === 0
        : choices.length === 0 &&
          reason_codes.length > 0 &&
          reason_codes.every((reason) => insufficiencyReasons.has(reason));
  if (!valid) return fail();

  return {
    choices,
    evidence,
    kind: 'campaign_list',
    outcome,
    period: selectedPeriod,
    reason_codes,
    recovery_actions,
    truncated: booleanValue(item.truncated),
  };
}

interface DecimalValue {
  readonly coefficient: bigint;
  readonly scale: number;
}

function exactNumber(value: unknown, nonnegative: boolean = true): CampaignNumber {
  if (typeof value === 'number') {
    if (!Number.isSafeInteger(value) || (nonnegative && value < 0)) return fail();
    return value;
  }
  if (typeof value !== 'string') return fail();
  const match = DECIMAL.exec(value);
  if (match === null || (nonnegative && match[1] === '-')) return fail();
  return value;
}

function decimalValue(value: CampaignNumber): DecimalValue {
  if (typeof value === 'number') return { coefficient: BigInt(value), scale: 0 };
  const match = DECIMAL.exec(value);
  if (match === null) return fail();
  const sign = match[1] === '-' ? -1n : 1n;
  const integer = match[2];
  const fraction = match[3] ?? '';
  if (integer === undefined) return fail();
  return {
    coefficient: sign * BigInt(`${integer}${fraction}`),
    scale: fraction.length,
  };
}

function scaledCoefficient(value: DecimalValue, scale: number): bigint {
  return value.coefficient * (10n ** BigInt(scale - value.scale));
}

function numbersEqual(left: CampaignNumber, right: CampaignNumber): boolean {
  const leftValue = decimalValue(left);
  const rightValue = decimalValue(right);
  const scale = Math.max(leftValue.scale, rightValue.scale);
  return scaledCoefficient(leftValue, scale) === scaledCoefficient(rightValue, scale);
}

function exactDifference(
  current: CampaignNumber,
  prior: CampaignNumber,
  supplied: CampaignNumber,
): boolean {
  const currentValue = decimalValue(current);
  const priorValue = decimalValue(prior);
  const suppliedValue = decimalValue(supplied);
  const scale = Math.max(currentValue.scale, priorValue.scale, suppliedValue.scale);
  return scaledCoefficient(currentValue, scale) - scaledCoefficient(priorValue, scale) ===
    scaledCoefficient(suppliedValue, scale);
}

function exactSum(values: readonly CampaignNumber[], supplied: CampaignNumber): boolean {
  const parsed = values.map(decimalValue);
  const suppliedValue = decimalValue(supplied);
  const scale = Math.max(suppliedValue.scale, ...parsed.map((value) => value.scale));
  const sum = parsed.reduce((total, value) => total + scaledCoefficient(value, scale), 0n);
  return sum === scaledCoefficient(suppliedValue, scale);
}

function numberIsZero(value: CampaignNumber): boolean {
  return decimalValue(value).coefficient === 0n;
}

function campaignUnits(value: unknown, expectedKind: CampaignUnitKind): CampaignUnits {
  const item = exact(value, ['currency_code', 'kind']);
  if (item.kind !== expectedKind) return fail();
  const currency_code = item.currency_code === null ? null : nonblank(item.currency_code);
  if (
    (expectedKind === 'currency') !== (currency_code !== null) ||
    (currency_code !== null && !/^[A-Z]{3}$/.test(currency_code))
  ) return fail();
  return { currency_code, kind: expectedKind };
}

function exactGrain<const Grain extends readonly string[]>(value: unknown, expected: Grain): Grain {
  if (
    !Array.isArray(value) ||
    value.length !== expected.length ||
    value.some((entry, index) => entry !== expected[index])
  ) return fail();
  return expected;
}

function unitKindForSlot(slot: CampaignSummarySlot | CampaignEvidenceSlot): CampaignUnitKind {
  if (slot === 'attributed_roas' || slot === 'prior_attributed_roas') return 'ratio';
  if (slot === 'spend' || slot === 'prior_spend') return 'currency';
  return 'count';
}

function metricValue(
  value: unknown,
  slot: CampaignSummarySlot,
  expectedPeriod: DateWindow,
): CampaignMetricValue {
  const item = exact(value, METRIC_VALUE_FIELDS);
  const metricPeriod = period(item.period);
  if (item.basis !== 'ad_attributed' || !sameCampaignPeriod(metricPeriod, expectedPeriod)) {
    return fail();
  }
  const units = campaignUnits(item.units, unitKindForSlot(slot));
  if (!isDisplayValue(item.display_value, units.kind)) return fail();
  return {
    basis: 'ad_attributed',
    display_value: item.display_value,
    grain: exactGrain(item.grain, CAMPAIGN_SUMMARY_GRAIN),
    period: metricPeriod,
    units,
    value: exactNumber(item.value),
  };
}

function nullableMetricValue(
  value: unknown,
  slot: CampaignSummarySlot,
  expectedPeriod: DateWindow,
): CampaignMetricValue | null {
  return value === null ? null : metricValue(value, slot, expectedPeriod);
}

function summary(value: unknown, selectedPeriod: DateWindow): CampaignSummary {
  const item = exact(value, ['attributed_roas', 'period', 'purchases', 'spend']);
  const summaryPeriod = period(item.period);
  if (!sameCampaignPeriod(summaryPeriod, selectedPeriod)) return fail();
  return {
    attributed_roas: nullableMetricValue(item.attributed_roas, 'attributed_roas', summaryPeriod),
    period: summaryPeriod,
    purchases: nullableMetricValue(item.purchases, 'purchases', summaryPeriod),
    spend: nullableMetricValue(item.spend, 'spend', summaryPeriod),
  };
}

function purchaseSeries(value: unknown, selectedPeriod: DateWindow): CampaignPurchaseSeries {
  const item = exact(value, ['basis', 'grain', 'period', 'points', 'units']);
  const seriesPeriod = period(item.period);
  if (
    item.basis !== 'ad_attributed' ||
    !sameCampaignPeriod(seriesPeriod, selectedPeriod) ||
    !Array.isArray(item.points)
  ) return fail();
  const points = item.points.map((entry): CampaignPurchasePoint => {
    const point = exact(entry, ['date', 'purchases']);
    return { date: isoDate(point.date), purchases: exactNumber(point.purchases) };
  });
  if (
    points.some((point, index) => {
      const prior = points[index - 1];
      return point.date < seriesPeriod.start ||
        point.date > seriesPeriod.end ||
        (prior !== undefined && prior.date >= point.date);
    })
  ) return fail();
  return {
    basis: 'ad_attributed',
    grain: exactGrain(item.grain, CAMPAIGN_PURCHASE_SERIES_GRAIN),
    period: seriesPeriod,
    points,
    units: campaignUnits(item.units, 'count'),
  };
}

function suppression(value: unknown): CampaignSuppression {
  const item = exact(value, ['reason', 'rows_suppressed']);
  if (typeof item.rows_suppressed !== 'boolean') return fail();
  const reason = nullableText(item.reason);
  if ((reason !== null) !== item.rows_suppressed) return fail();
  return { reason, rows_suppressed: item.rows_suppressed };
}

function evidenceGrain(slot: CampaignEvidenceSlot):
  typeof CAMPAIGN_SUMMARY_GRAIN | typeof CAMPAIGN_PURCHASE_SERIES_GRAIN {
  return slot === 'purchase_series' ? CAMPAIGN_PURCHASE_SERIES_GRAIN : CAMPAIGN_SUMMARY_GRAIN;
}

function slotEvidence(value: unknown): CampaignSlotEvidence {
  const item = exact(value, [
    'actual_coverage',
    'basis',
    'condition',
    'coverage_holes',
    'freshness_qualifiers',
    'grain',
    'requested_period',
    'row_count',
    'slot',
    'suppression',
    'truncated',
    'units',
  ]);
  const slot = member(item.slot, campaignEvidenceSlots);
  const condition = member(item.condition, campaignEvidenceConditions);
  const requested_period = period(item.requested_period);
  const actual_coverage = orderedPeriods(item.actual_coverage);
  const coverage_holes = orderedPeriods(item.coverage_holes);
  const parsedSuppression = suppression(item.suppression);
  const row_count = exactNumber(item.row_count);
  if (typeof row_count !== 'number') return fail();
  const truncated = booleanValue(item.truncated);
  const units = item.units === null ? null : campaignUnits(item.units, unitKindForSlot(slot));
  const basis = item.basis === null
    ? null
    : item.basis === 'ad_attributed'
      ? 'ad_attributed' as const
      : fail();
  const grain = exactGrain(item.grain, evidenceGrain(slot));
  const exactCoverage = actual_coverage.length === 1 &&
    actual_coverage[0] !== undefined &&
    sameCampaignPeriod(actual_coverage[0], requested_period);
  if (
    coverage_holes.some((hole) => !isCoveredBy(requested_period, hole)) ||
    (condition === 'available' &&
      (!exactCoverage ||
        coverage_holes.length > 0 ||
        parsedSuppression.rows_suppressed ||
        truncated ||
        row_count === 0 ||
        units === null ||
        basis === null)) ||
    (condition === 'empty_covered' &&
      (!exactCoverage ||
        coverage_holes.length > 0 ||
        parsedSuppression.rows_suppressed ||
        truncated ||
        row_count !== 0)) ||
    (condition === 'suppressed' && !parsedSuppression.rows_suppressed) ||
    (condition === 'truncated' && !truncated) ||
    (condition === 'coverage_hole' && coverage_holes.length === 0) ||
    ((condition === 'missing_current_publication' ||
      condition === 'missing_comparator_publication') &&
      (actual_coverage.length > 0 || row_count !== 0)) ||
    (condition === 'missing_comparator_publication' && !slot.startsWith('prior_')) ||
    (slot.startsWith('prior_') && condition === 'missing_current_publication')
  ) return fail();
  return {
    actual_coverage,
    basis,
    condition,
    coverage_holes,
    freshness_qualifiers: orderedStrings(item.freshness_qualifiers),
    grain,
    requested_period,
    row_count,
    slot,
    suppression: parsedSuppression,
    truncated,
    units,
  };
}

const evidenceStatusSlots = new Set<string>([
  ...campaignEvidenceSlots,
  'ad_spend', 'impressions', 'clicks', 'ctr', 'cpc', 'cpm',
]);

function evidenceStatus(value: unknown): CampaignEvidenceStatus {
  const item = exact(value, [
    'condition', 'days_expected', 'days_present', 'explanation', 'recovery_reading', 'role', 'slot',
  ]);
  const condition = member(item.condition, campaignEvidenceStatusConditions);
  const role = member(item.role, campaignEvidenceStatusRoles);
  const slot = nonblank(item.slot);
  if (!evidenceStatusSlots.has(slot)) return fail();
  const explanation = nonblank(item.explanation);
  const recovery_reading = item.recovery_reading === null ? null : nonblank(item.recovery_reading);
  if ((condition === 'available') !== (recovery_reading === null)) return fail();
  const parseDays = (value: unknown): number | null => {
    if (value === null) return null;
    const parsed = exactNumber(value);
    if (typeof parsed !== 'number' || !Number.isInteger(parsed) || parsed < 0) return fail();
    return parsed;
  };
  const days_present = parseDays(item.days_present);
  const days_expected = parseDays(item.days_expected);
  if (condition === 'incomplete_daily_series') {
    if (days_present === null || days_expected === null || days_present >= days_expected) {
      return fail();
    }
  } else if (days_present !== null || days_expected !== null) return fail();
  return { condition, days_expected, days_present, explanation, recovery_reading, role, slot };
}

function sameUnits(left: CampaignUnits, right: CampaignUnits): boolean {
  return left.kind === right.kind && left.currency_code === right.currency_code;
}

function sameMetricValue(
  left: CampaignMetricValue | null,
  right: CampaignMetricValue | null,
): boolean {
  if (left === null || right === null) return left === right;
  return left.basis === right.basis &&
    sameCampaignPeriod(left.period, right.period) &&
    left.grain.every((entry, index) => entry === right.grain[index]) &&
    sameUnits(left.units, right.units) &&
    numbersEqual(left.value, right.value);
}

function comparisonMetric(
  value: unknown,
  expectedSlot: CampaignSummarySlot,
  currentPeriod: DateWindow,
  priorPeriod: DateWindow,
): CampaignMetricComparison {
  const item = exact(value, comparisonFields(value, ['absolute_change', 'current', 'prior', 'reason', 'slot']));
  const slot = member(item.slot, campaignSummarySlots);
  if (slot !== expectedSlot) return fail();
  const current = nullableMetricValue(item.current, slot, currentPeriod);
  const prior = nullableMetricValue(item.prior, slot, priorPeriod);
  const absolute_change = item.absolute_change === null
    ? null
    : exactNumber(item.absolute_change, false);
  const percentage_change = !(PERCENTAGE_KEY in item) || item.percentage_change === null
    ? null
    : nonblank(item.percentage_change);
  if (percentage_change !== null && !PERCENTAGE.test(percentage_change)) return fail();
  const change = parseChange(item.change);
  if (change === undefined) return fail();
  const reason = item.reason === null
    ? null
    : member(item.reason, ['one_sided', 'prior_zero_percentage_undefined'] as const);
  if (current === null && prior === null) return fail();
  if (current === null || prior === null) {
    if (absolute_change !== null || percentage_change !== null || change !== null || reason !== 'one_sided') {
      return fail();
    }
  } else {
    // Two defined sides always carry a block (ADR-121).
    if (change === null) return fail();
    if (
      !sameUnits(current.units, prior.units) ||
      current.basis !== prior.basis ||
      current.grain.some((entry, index) => entry !== prior.grain[index]) ||
      absolute_change === null ||
      !exactDifference(current.value, prior.value, absolute_change)
    ) return fail();
    if (numberIsZero(prior.value)) {
      if (percentage_change !== null || reason !== 'prior_zero_percentage_undefined') {
        return fail();
      }
    } else if (reason !== null) {
      return fail();
    }
  }
  return { absolute_change, change, current, prior, reason, slot };
}

function comparison(
  value: unknown,
  selectedPeriod: DateWindow,
  prior: DateWindow,
): CampaignPriorComparison {
  const item = exact(value, ['current_period', 'metrics', 'prior_period']);
  const current_period = period(item.current_period);
  const prior_period = period(item.prior_period);
  const metricsSource = item.metrics;
  if (
    !sameCampaignPeriod(current_period, selectedPeriod) ||
    !sameCampaignPeriod(prior_period, prior) ||
    !previousEqualPeriod(current_period, prior_period) ||
    !Array.isArray(metricsSource) ||
    metricsSource.length !== campaignSummarySlots.length
  ) return fail();
  const metrics = campaignSummarySlots.map((slot, index) =>
    comparisonMetric(metricsSource[index], slot, current_period, prior_period)
  );
  return { current_period, metrics, prior_period };
}

function metricForSummary(
  campaignSummary: CampaignSummary,
  slot: CampaignSummarySlot,
): CampaignMetricValue | null {
  return slot === 'purchases'
    ? campaignSummary.purchases
    : slot === 'attributed_roas'
      ? campaignSummary.attributed_roas
      : campaignSummary.spend;
}

const reasonForCondition: Readonly<Partial<Record<
  CampaignEvidenceCondition,
  CampaignInsufficiencyReason
>>> = {
  coverage_hole: 'coverage_hole',
  empty_covered: 'empty_covered',
  incompatible_scope: 'incompatible_scope',
  missing_comparator_publication: 'missing_comparator_publication',
  missing_current_publication: 'missing_current_publication',
  suppressed: 'suppressed',
  truncated: 'truncated',
};

function derivedDetailReasons(
  campaignSummary: CampaignSummary,
  series: CampaignPurchaseSeries | null,
  priorPeriod: DateWindow | null,
  priorComparison: CampaignPriorComparison | null,
  evidence: readonly CampaignSlotEvidence[],
): readonly CampaignInsufficiencyReason[] {
  const reasons = new Set<CampaignInsufficiencyReason>();
  for (const item of evidence) {
    const reason = reasonForCondition[item.condition];
    if (reason !== undefined) reasons.add(reason);
  }
  const evidenceBySlot = new Map(evidence.map((item) => [item.slot, item]));
  for (const slot of campaignSummarySlots) {
    const slotEvidence = evidenceBySlot.get(slot);
    if (slotEvidence === undefined) return fail();
    const represented = metricForSummary(campaignSummary, slot) !== null;
    const available = slotEvidence.condition === 'available';
    if (available !== represented || (available && slotEvidence.row_count !== 1)) {
      reasons.add('incompatible_scope');
    }
  }
  const seriesEvidence = evidenceBySlot.get('purchase_series');
  if (seriesEvidence === undefined) return fail();
  const seriesAvailable = seriesEvidence.condition === 'available';
  if (seriesAvailable !== (series !== null)) reasons.add('incompatible_scope');
  if (series !== null) {
    if (seriesEvidence.row_count !== series.points.length) reasons.add('incompatible_scope');
    if (series.points.length < 2) reasons.add('too_short_series');
  }
  if (priorPeriod !== null) {
    if (priorComparison === null) {
      if (['prior_purchases', 'prior_attributed_roas', 'prior_spend'].every(
        (slot) => evidenceBySlot.get(slot as CampaignEvidenceSlot)?.condition === 'available',
      )) reasons.add('incompatible_scope');
    } else {
      if (priorComparison.metrics.some((metric) => metric.reason === 'one_sided')) {
        reasons.add('one_sided_comparison');
      }
      if (priorComparison.metrics.some(
        (metric) => !sameMetricValue(metric.current, metricForSummary(campaignSummary, metric.slot)),
      )) reasons.add('incompatible_scope');
    }
  }
  return campaignInsufficiencyReasons.filter((reason) => reasons.has(reason));
}

function detailOutcome(value: unknown): CampaignDetailOutcome {
  return member(value, ['selected', 'insufficient'] as const);
}

export function parseCampaignAttributionDetailPage(value: unknown): CampaignAttributionDetailPage {
  const item = exact(value, [
    'boundaries',
    'comparison',
    'evidence_by_slot',
    'insufficiency_reasons',
    'kind',
    'outcome',
    'period',
    'prior_period',
    'purchase_series',
    'selection',
    'summary',
  ]);
  if (item.kind !== 'campaign_detail' || !Array.isArray(item.evidence_by_slot)) return fail();
  const selectedPeriod = period(item.period);
  const prior_period = item.prior_period === null ? null : period(item.prior_period);
  if (prior_period !== null && !previousEqualPeriod(selectedPeriod, prior_period)) return fail();
  const parsedSummary = summary(item.summary, selectedPeriod);
  const series = item.purchase_series === null
    ? null
    : purchaseSeries(item.purchase_series, selectedPeriod);
  const priorComparison = item.comparison === null
    ? null
    : prior_period === null
      ? fail()
      : comparison(item.comparison, selectedPeriod, prior_period);
  const evidence_by_slot = item.evidence_by_slot.map(slotEvidence);
  const expectedSlots = prior_period === null
    ? campaignEvidenceSlots.slice(0, 4)
    : campaignEvidenceSlots;
  if (
    evidence_by_slot.length !== expectedSlots.length ||
    evidence_by_slot.some((entry, index) =>
      entry.slot !== expectedSlots[index] ||
      !sameCampaignPeriod(
        entry.requested_period,
        entry.slot.startsWith('prior_') ? prior_period ?? selectedPeriod : selectedPeriod,
      ),
    )
  ) return fail();

  if (
    parsedSummary.purchases !== null &&
    series !== null &&
    evidence_by_slot[0]?.condition === 'available' &&
    evidence_by_slot[3]?.condition === 'available' &&
    !exactSum(series.points.map((point) => point.purchases), parsedSummary.purchases.value)
  ) return fail();

  if (
    !Array.isArray(item.boundaries) ||
    item.boundaries.length !== CAMPAIGN_ATTRIBUTION_BOUNDARIES.length ||
    item.boundaries.some((boundary, index) => boundary !== CAMPAIGN_ATTRIBUTION_BOUNDARIES[index])
  ) return fail();
  const insufficiency_reasons = canonicalEnumArray(
    item.insufficiency_reasons,
    campaignInsufficiencyReasons,
  );
  const derivedReasons = derivedDetailReasons(
    parsedSummary,
    series,
    prior_period,
    priorComparison,
    evidence_by_slot,
  );
  if (
    derivedReasons.length !== insufficiency_reasons.length ||
    derivedReasons.some((reason, index) => reason !== insufficiency_reasons[index])
  ) return fail();

  const outcome = detailOutcome(item.outcome);
  const completeSummary = campaignSummarySlots.every(
    (slot) => metricForSummary(parsedSummary, slot) !== null,
  );
  const valid = outcome === 'selected'
    ? insufficiency_reasons.length === 0 &&
      completeSummary &&
      series !== null &&
      series.points.length >= 2 &&
      evidence_by_slot.every((entry) => entry.condition === 'available') &&
      (prior_period === null || priorComparison !== null)
    : insufficiency_reasons.length > 0;
  if (!valid) return fail();

  return {
    boundaries: CAMPAIGN_ATTRIBUTION_BOUNDARIES,
    comparison: priorComparison,
    evidence_by_slot,
    insufficiency_reasons,
    kind: 'campaign_detail',
    outcome,
    period: selectedPeriod,
    prior_period,
    purchase_series: series,
    selection: signature(item.selection),
    summary: parsedSummary,
  };
}


export const campaignDeliverySlots = ['ad_spend', 'impressions', 'clicks', 'ctr', 'cpc', 'cpm'] as const;
export type CampaignDeliverySlot = typeof campaignDeliverySlots[number];

type CampaignDeliverySeriesValues = Record<CampaignDeliverySlot, CampaignNumber>;

export interface CampaignDeliveryValue {
  readonly slot: CampaignDeliverySlot;
  readonly value: CampaignMetricValue | null;
  /**
   * On a comparison entry: the server's change from this prior value to the summary's current
   * one (ADR-121). Null on a summary entry or an absent side.
   */
  readonly change: Change | null;
}

export interface CampaignDeliverySeriesRow {
  readonly current: CampaignDeliverySeriesValues;
  readonly date: string;
  readonly prior: CampaignDeliverySeriesValues;
}

export interface CampaignAttributionDelivery {
  readonly outcome: 'complete' | 'qualified' | 'insufficient';
  readonly summary: readonly CampaignDeliveryValue[];
  readonly comparison: { readonly period: DateWindow; readonly slots: readonly CampaignDeliveryValue[] };
  readonly series: {
    readonly available_grains: readonly ['day'];
    readonly exact_rows: readonly CampaignDeliverySeriesRow[];
    readonly grain: 'day';
    readonly metrics: readonly CampaignDeliverySlot[];
    readonly points: readonly CampaignDeliverySeriesRow[];
    readonly visual_id: 'campaign_delivery_daily';
  };
  readonly dsp_quality: {
    readonly outcome: 'available' | 'unavailable';
    readonly measurable_impressions: CampaignMetricValue | null;
    readonly viewable_impressions: CampaignMetricValue | null;
    readonly viewable_impressions_estimated: CampaignMetricValue | null;
    readonly viewable_rate: CampaignMetricValue | null;
    readonly reason_code: string | null;
  };
  readonly evidence: readonly CampaignEvidenceStatus[];
  readonly recovery: string;
  readonly narration_reference: null;
}

export const campaignInsightSlotIds = [
  'contribution',
  'divergence',
  'exposure',
  'movers',
  'trend-shape',
  'efficiency-drift',
  'funnel',
  'next-check',
  'metric-focus',
] as const;
export type CampaignInsightSlotId = typeof campaignInsightSlotIds[number];

const campaignInsightOutputClasses = ['observed', 'perspective', 'hypothesis'] as const;
export type CampaignInsightOutputClass = typeof campaignInsightOutputClasses[number];
const campaignInsightStates = ['filled', 'silent', 'unavailable'] as const;
export type CampaignInsightState = typeof campaignInsightStates[number];
const campaignInsightAbsenceClasses = [
  'evaluated-quiet', 'ineligible', 'generation-pending', 'out-of-slice',
] as const;
export type CampaignInsightAbsenceClass = typeof campaignInsightAbsenceClasses[number];
const campaignInsightClassById: Record<CampaignInsightSlotId, CampaignInsightOutputClass> = {
  contribution: 'perspective',
  divergence: 'perspective',
  exposure: 'perspective',
  movers: 'observed',
  'trend-shape': 'perspective',
  'efficiency-drift': 'perspective',
  funnel: 'perspective',
  'next-check': 'hypothesis',
  'metric-focus': 'perspective',
};
const portfolioInsightRoster = ['contribution', 'divergence', 'exposure', 'movers'] as const;
const detailInsightRoster = ['trend-shape', 'efficiency-drift', 'funnel', 'next-check'] as const;

export interface CampaignInsightSlot {
  readonly absence_class: CampaignInsightAbsenceClass | null;
  readonly absence_reason: string | null;
  readonly artifact_revision: string;
  readonly evidence_line: string | null;
  readonly id: CampaignInsightSlotId;
  readonly output_class: CampaignInsightOutputClass;
  readonly prompt_version: string;
  readonly state: CampaignInsightState;
  readonly text: string | null;
  /** FR-11: present only on a filled card whose served prompt offered a step. */
  readonly next_step: InsightNextStep | null;
}

const insightSlotBaseFields = [
  'artifact_revision', 'evidence_line', 'id', 'output_class', 'prompt_version', 'state', 'text',
] as const;
const insightSlotAbsenceFields = [...insightSlotBaseFields, 'absence_class', 'absence_reason'] as const;
const insightSlotStepFields = [...insightSlotBaseFields, 'next_step'] as const;

function parseCampaignInsightSlots(
  value: unknown,
  expectedRoster: readonly CampaignInsightSlotId[],
): readonly CampaignInsightSlot[] {
  if (!Array.isArray(value) || value.length !== expectedRoster.length) return fail();
  const slots = value.map((entry) => {
    // One closed shape family: the absence pair is present together (a declared
    // absence, class never null) or absent together (no declared absence).
    const withAbsence =
      typeof entry === 'object' && entry !== null && 'absence_class' in entry;
    const item = exact(
      entry,
      withAbsence ? insightSlotAbsenceFields : hasNextStep(entry) ? insightSlotStepFields : insightSlotBaseFields,
    );
    const id = member(item.id, campaignInsightSlotIds);
    const output_class = member(item.output_class, campaignInsightOutputClasses);
    const state = member(item.state, campaignInsightStates);
    const text = nullableText(item.text);
    const evidence_line = nullableText(item.evidence_line);
    const absence_class = withAbsence
      ? member(item.absence_class, campaignInsightAbsenceClasses)
      : null;
    const absence_reason = withAbsence ? nullableText(item.absence_reason) : null;
    if (
      output_class !== campaignInsightClassById[id] ||
      (state === 'filled' && (text === null || evidence_line === null)) ||
      (state !== 'filled' && (text !== null || evidence_line !== null)) ||
      (state === 'filled' && absence_class !== null)
    ) return fail();
    return {
      absence_class,
      absence_reason,
      artifact_revision: nonblank(item.artifact_revision),
      evidence_line,
      id,
      next_step: slotNextStep(item, state === 'filled'),
      output_class,
      prompt_version: nonblank(item.prompt_version),
      state,
      text,
    };
  });
  if (slots.some((slot, index) => slot.id !== expectedRoster[index])) return fail();
  return slots;
}

export function parseCampaignMetricFocusInsight(value: unknown): CampaignInsightSlot {
  const [slot] = parseCampaignInsightSlots([value], ['metric-focus']);
  if (slot === undefined) return fail();
  return slot;
}

export interface CampaignAttributionDetailV2 extends CampaignAttributionDetailPage {
  readonly delivery: CampaignAttributionDelivery;
  /** The coverage block the "Coverage for this window" sheet renders (ADR-119). */
  readonly evidence_status: EvidenceStatusV2;
  readonly insights: readonly CampaignInsightSlot[];
}

/** Decode a coverage block, failing with this module's contract error. */
function coverageBlock(value: unknown, window: DateWindow): EvidenceStatusV2 | null {
  try {
    return parseEvidenceStatus(value, window);
  } catch {
    return fail();
  }
}

function deliveryKind(slot: CampaignDeliverySlot): CampaignUnitKind {
  return slot === 'impressions' || slot === 'clicks'
    ? 'count'
    : slot === 'ctr'
      ? 'ratio'
      : 'currency';
}

function deliveryValue(
  value: unknown,
  slot: CampaignDeliverySlot,
  expectedPeriod: DateWindow,
): CampaignMetricValue | null {
  if (value === null) return null;
  const item = exact(value, METRIC_VALUE_FIELDS);
  const metricPeriod = period(item.period);
  if (item.basis !== 'ad_attributed' || !sameCampaignPeriod(metricPeriod, expectedPeriod)) return fail();
  const units = campaignUnits(item.units, deliveryKind(slot));
  if (!isDisplayValue(item.display_value, units.kind)) return fail();
  return {
    basis: 'ad_attributed',
    display_value: item.display_value,
    grain: exactGrain(item.grain, CAMPAIGN_SUMMARY_GRAIN),
    period: metricPeriod,
    units,
    value: exactNumber(item.value),
  };
}

function parseDeliveryValues(
  value: unknown,
  periodValue: DateWindow,
  current: readonly CampaignDeliveryValue[] | null = null,
): readonly CampaignDeliveryValue[] {
  if (!Array.isArray(value) || value.length !== campaignDeliverySlots.length) return fail();
  const parsed = campaignDeliverySlots.map((slot, index) => {
    const raw: unknown = value[index];
    // A comparison entry always carries its change key; a summary entry never does.
    const carries = current !== null;
    const item = exact(raw, carries ? ['slot', 'value', CHANGE_KEY] : ['slot', 'value']);
    if (item.slot !== slot) return fail();
    const entry = deliveryValue(item.value, slot, periodValue);
    const change = carries ? parseChange(item.change) : null;
    if (change === undefined) return fail();
    // A block exists exactly when both windows served the slot (ADR-121).
    if (change !== null && (entry === null || current?.[index]?.value === null)) return fail();
    if (carries && change === null && entry !== null && current?.[index]?.value !== null) return fail();
    return { change, slot, value: entry };
  });
  return parsed;
}

function parseDeliverySeriesValues(value: unknown): CampaignDeliverySeriesValues {
  const values = exact(value, campaignDeliverySlots);
  return Object.fromEntries(
    campaignDeliverySlots.map((slot) => [slot, exactNumber(values[slot])]),
  ) as CampaignDeliverySeriesValues;
}

export function parseCampaignAttributionDetailV2(value: unknown): CampaignAttributionDetailV2 {
  const item = exact(value, [
    'boundaries', 'comparison', 'delivery', EVIDENCE_STATUS_KEY, 'evidence_by_slot', 'insights', 'insufficiency_reasons', 'kind',
    'outcome', 'period', 'prior_period', 'purchase_series', 'selection', 'summary',
  ]);
  const legacy = parseCampaignAttributionDetailPage({
    boundaries: item.boundaries,
    comparison: item.comparison,
    evidence_by_slot: item.evidence_by_slot,
    insufficiency_reasons: item.insufficiency_reasons,
    kind: item.kind,
    outcome: item.outcome,
    period: item.period,
    prior_period: item.prior_period,
    purchase_series: item.purchase_series,
    selection: item.selection,
    summary: item.summary,
  });
  if (legacy.prior_period === null) return fail();
  const insights = parseCampaignInsightSlots(item.insights, detailInsightRoster);
  const delivery = exact(item.delivery, [
    'comparison', 'dsp_quality', 'evidence', 'narration_reference', 'outcome', 'recovery', 'series', 'summary',
  ]);
  const outcome = member(delivery.outcome, ['complete', 'qualified', 'insufficient'] as const);
  const comparisonValue = exact(delivery.comparison, ['period', 'slots']);
  const comparisonPeriod = period(comparisonValue.period);
  if (!sameCampaignPeriod(comparisonPeriod, legacy.prior_period)) return fail();
  const summary = parseDeliveryValues(delivery.summary, legacy.period);
  const comparison = parseDeliveryValues(comparisonValue.slots, comparisonPeriod, summary);
  const series = exact(delivery.series, [
    'available_grains', 'exact_rows', 'grain', 'metrics', 'points', 'visual_id',
  ]);
  if (
    series.grain !== 'day' || series.visual_id !== 'campaign_delivery_daily' ||
    !Array.isArray(series.available_grains) || !Array.isArray(series.metrics) ||
    !Array.isArray(series.points) || !Array.isArray(series.exact_rows) ||
    series.points.length !== series.exact_rows.length ||
    JSON.stringify(series.available_grains) !== JSON.stringify(['day']) ||
    JSON.stringify(series.metrics) !== JSON.stringify(campaignDeliverySlots)
  ) return fail();
  const deliveryRows = (rows: readonly unknown[]): readonly CampaignDeliverySeriesRow[] => {
    const parsed = rows.map((entry) => {
      const row = exact(entry, ['current', 'date', 'prior']);
      return {
        current: parseDeliverySeriesValues(row.current),
        date: isoDate(row.date),
        prior: parseDeliverySeriesValues(row.prior),
      };
    });
    if (parsed.some((row, index) => (
      row.date < legacy.period.start || row.date > legacy.period.end ||
      (index > 0 && parsed[index - 1]!.date >= row.date)
    ))) return fail();
    return parsed;
  };
  const seriesPoints = deliveryRows(series.points);
  const exactRows = deliveryRows(series.exact_rows);
  if (JSON.stringify(seriesPoints) !== JSON.stringify(exactRows)) return fail();
  const expectedDays = Math.floor(
    (Date.parse(`${legacy.period.end}T00:00:00Z`) - Date.parse(`${legacy.period.start}T00:00:00Z`)) / 86_400_000,
  ) + 1;
  if (seriesPoints.length > 0 && seriesPoints.length !== expectedDays) return fail();
  const quality = exact(delivery.dsp_quality, [
    'measurable_impressions', 'outcome', 'reason_code', 'viewable_impressions',
    'viewable_impressions_estimated', 'viewable_rate',
  ]);
  const qualityOutcome = member(quality.outcome, ['available', 'unavailable'] as const);
  const measurable_impressions = deliveryValue(
    quality.measurable_impressions, 'impressions', legacy.period,
  );
  const viewable_impressions = deliveryValue(
    quality.viewable_impressions, 'impressions', legacy.period,
  );
  const viewable_impressions_estimated = deliveryValue(
    quality.viewable_impressions_estimated, 'impressions', legacy.period,
  );
  const viewable_rate = deliveryValue(quality.viewable_rate, 'ctr', legacy.period);
  const qualityValues = [
    measurable_impressions,
    viewable_impressions,
    viewable_impressions_estimated,
    viewable_rate,
  ];
  const reason_code = quality.reason_code === null ? null : nonblank(quality.reason_code);
  if (
    (qualityOutcome === 'available' && (qualityValues.some((entry) => entry === null) || reason_code !== null)) ||
    (qualityOutcome === 'unavailable' && (qualityValues.some((entry) => entry !== null) || reason_code === null))
  ) return fail();
  if (
    (outcome === 'complete' && (
      summary.some((entry) => entry.value === null) || comparison.some((entry) => entry.value === null) ||
      seriesPoints.length === 0 || qualityOutcome !== 'available'
    )) ||
    (outcome === 'qualified' && (
      summary.some((entry) => entry.value === null) || comparison.some((entry) => entry.value === null) ||
      seriesPoints.length === 0
    )) ||
    (outcome === 'insufficient' && (summary.some((entry) => entry.value !== null) || seriesPoints.length !== 0))
  ) return fail();
  if (delivery.narration_reference !== null) return fail();
  const evidence = Array.isArray(delivery.evidence)
    ? delivery.evidence.map(evidenceStatus)
    : fail();
  const expectedStatusRoster: readonly (readonly [string, CampaignEvidenceStatusRole])[] = [
    ['purchases', 'current'], ['attributed_roas', 'current'], ['spend', 'current'],
    ['purchase_series', 'daily'],
    ['prior_purchases', 'prior'], ['prior_attributed_roas', 'prior'], ['prior_spend', 'prior'],
    ...campaignDeliverySlots.flatMap((slot) =>
      campaignEvidenceStatusRoles.map((role) => [slot, role] as const),
    ),
  ];
  if (
    evidence.length !== expectedStatusRoster.length ||
    evidence.some((entry, index) => (
      entry.slot !== expectedStatusRoster[index]![0] || entry.role !== expectedStatusRoster[index]![1]
    ))
  ) return fail();
  const deliveryStatuses = evidence.slice(-campaignDeliverySlots.length * campaignEvidenceStatusRoles.length);
  const deliveryAccepted = deliveryStatuses.every((entry) => entry.condition === 'available');
  if ((outcome === 'insufficient') === deliveryAccepted) return fail();
  const evidence_status = coverageBlock(item.evidence_status, legacy.period) ?? fail();
  // One coverage entry per served status, in order; a refused status is never shown complete.
  if (
    evidence_status.entries.length !== evidence.length ||
    evidence_status.entries.some((entry, index) => (
      entry.slot !== evidence[index]!.slot || entry.role !== evidence[index]!.role ||
      (entry.state === 'complete') !== (evidence[index]!.condition === 'available')
    ))
  ) return fail();
  return {
    ...legacy,
    evidence_status,
    insights,
    delivery: {
      comparison: { period: comparisonPeriod, slots: comparison },
      dsp_quality: {
        measurable_impressions,
        outcome: qualityOutcome,
        reason_code,
        viewable_impressions,
        viewable_impressions_estimated,
        viewable_rate,
      },
      evidence,
      narration_reference: null,
      outcome,
      recovery: nonblank(delivery.recovery),
      series: {
        available_grains: ['day'],
        exact_rows: exactRows,
        grain: 'day',
        metrics: campaignDeliverySlots,
        points: seriesPoints,
        visual_id: 'campaign_delivery_daily',
      },
      summary,
    },
  };
}


export const campaignPortfolioInsufficiencyReasons = [
  'missing_catalog',
  'incompatible_scope',
] as const;
export type CampaignPortfolioInsufficiencyReason =
  typeof campaignPortfolioInsufficiencyReasons[number];

export const campaignPortfolioSortFields = [
  'spend',
  'attributed_roas',
  'purchases',
  'ctr',
  'name',
] as const;
export type CampaignPortfolioSortField = typeof campaignPortfolioSortFields[number];
export type CampaignPortfolioMetric =
  | Exclude<CampaignPortfolioSortField, 'name'>
  | 'impressions'
  | 'portfolio_roas_attributed_14d';
/** Delivery per channel's columns (ADR-127 decision 4), each from that channel's own operands. */
export type CampaignDeliveryMetric = 'clicks' | 'ctr' | 'cpc' | 'cpm';
export type CampaignChannelMetric = CampaignPortfolioMetric | CampaignDeliveryMetric | 'viewable_rate';

export interface CampaignPortfolioMetricValue {
  readonly basis: 'ad_attributed';
  /** The server's two-place display for currency, null otherwise (ADR-084). */
  readonly display_value: string | null;
  readonly grain: readonly string[];
  readonly period: DateWindow;
  readonly units: CampaignUnits;
  /** Null only for a ratio whose denominator is zero in its window (ADR-087). */
  readonly value: CampaignNumber | null;
}

export interface CampaignPortfolioComparison<Metric extends CampaignChannelMetric = CampaignPortfolioMetric> {
  /** Null exactly when either side's ratio is typed absent (ADR-087). */
  readonly absolute_change: CampaignNumber | null;
  /** The server's change (ADR-121); null when a side is typed absent or the change is withheld. */
  readonly change: Change | null;
  readonly current: CampaignPortfolioMetricValue;
  readonly metric: Metric;
  readonly prior: CampaignPortfolioMetricValue;
  /** A missing or partial day withholds this change; the coverage block says why (ADR-118 d5). */
  readonly withheld: boolean;
}

export interface CampaignPortfolioRow {
  readonly capability: CampaignSelectionCapability;
  readonly comparisons: readonly CampaignPortfolioComparison[];
  readonly freshness_qualifiers: readonly string[];
  readonly signature: SafeCampaignDisplaySignature;
}

/** One server-declared channel filter option (ADR-127 decision 1). */
export interface CampaignChannelFilterOption {
  readonly filter_id: string;
  readonly label: string;
}

/** The preset's closed filter set and the option this body renders. */
export interface CampaignChannelFilter {
  readonly options: readonly CampaignChannelFilterOption[];
  readonly selected: string;
}

export interface CampaignViewabilityCell {
  readonly absence_reason: string | null;
  readonly comparison: CampaignPortfolioComparison<'viewable_rate'> | null;
  readonly coverage_text: string | null;
}

/** What a multi-channel option carries beyond its rows (ADR-127 decisions 3 and 4). */
export interface CampaignChannelView {
  readonly by_channel: readonly {
    readonly channel: string;
    readonly impressions: CampaignPortfolioComparison<'impressions'>;
    readonly label: string;
    readonly spend: CampaignPortfolioComparison<'spend'>;
  }[];
  readonly delivery: readonly {
    readonly channel: string;
    readonly clicks: CampaignPortfolioComparison<'clicks'>;
    readonly cpc: CampaignPortfolioComparison<'cpc'>;
    readonly cpm: CampaignPortfolioComparison<'cpm'>;
    readonly ctr: CampaignPortfolioComparison<'ctr'>;
    readonly label: string;
    readonly viewable_rate: CampaignViewabilityCell | null;
  }[];
  readonly delivery_bases: { readonly cpc: string; readonly cpm: string; readonly ctr: string };
  readonly delivery_legend: readonly string[];
  readonly insights_line: string;
  readonly not_added: readonly {
    readonly label: string;
    readonly metric: 'purchases' | 'portfolio_roas_attributed_14d';
    readonly reason: string;
    readonly value_text: string;
  }[];
  readonly scorecard_line: string;
  readonly trend_line: string;
  readonly viewability: boolean;
  readonly viewability_heading: string | null;
}

export interface CampaignPortfolioArtifact {
  /** The channel filter set (ADR-127); every served body names it (render format 11). */
  readonly channel_filter: CampaignChannelFilter;
  /** A complete multi-channel option's per-channel content; null otherwise. */
  readonly channel_view: CampaignChannelView | null;
  readonly evidence: readonly CampaignListEvidence[];
  /** The window's coverage block (ADR-119); null only on an empty or insufficient portfolio. */
  readonly evidence_status: EvidenceStatusV2 | null;
  readonly insights: readonly CampaignInsightSlot[];
  readonly kind: 'campaign_portfolio';
  readonly outcome: 'complete' | 'empty' | 'insufficient';
  readonly period: DateWindow;
  readonly prior_period: DateWindow;
  readonly reason_codes: readonly CampaignPortfolioInsufficiencyReason[];
  readonly rows: readonly CampaignPortfolioRow[];
  readonly sort: { readonly direction: 'asc' | 'desc'; readonly field: CampaignPortfolioSortField };
  readonly total_count: number;
  readonly totals: readonly CampaignPortfolioComparison[];
  readonly trend: {
    readonly basis: 'ad_attributed';
    readonly grain: readonly ['date', 'marketplace'];
    readonly period: DateWindow;
    readonly points: readonly {
      readonly date: string;
      /** Null on a day with no spend (ADR-087), and on every day of a spend-only trend. */
      readonly portfolio_roas_attributed_14d: CampaignNumber | null;
      readonly purchases: CampaignNumber | null;
      readonly spend: CampaignNumber;
    }[];
    /** A multi-channel option serves spend alone: the other series would total across channels. */
    readonly spend_only: boolean;
    readonly units: {
      readonly portfolio_roas_attributed_14d: CampaignUnits | null;
      readonly purchases: CampaignUnits | null;
      readonly spend: CampaignUnits;
    };
  } | null;
  readonly pagination: {
    readonly segment_count: number;
    readonly segment_index: number;
    readonly segment_size: 50;
    readonly total_count: number;
  } | null;
  /** The name roster in the body: a request-time list's, or a body baked before render format 13. */
  readonly roster: readonly CampaignPortfolioRosterEntry[];
  /** A baked body's roster (ADR-145): its shards, fetched on the name field's first focus. */
  readonly roster_shards: CampaignPortfolioRosterShards | null;
  /** The population's highest and lowest Attributed ROAS campaigns (render format 14); null unless complete. */
  readonly return_lists: CampaignReturnLists | null;
}

/** One served return-list campaign: its display identity, Attributed ROAS and spend. */
export interface CampaignReturnEntry {
  readonly attributed_roas: CampaignPortfolioMetricValue;
  readonly signature: SafeCampaignDisplaySignature;
  readonly spend: CampaignPortfolioMetricValue;
}

/** The server's highest- and lowest-return campaigns, in served order; the browser never ranks. */
export interface CampaignReturnLists {
  readonly highest: readonly CampaignReturnEntry[];
  readonly lowest: readonly CampaignReturnEntry[];
}

/** What a baked body says about its name roster: its shard count and the bake's revision. */
export interface CampaignPortfolioRosterShards {
  readonly revision: string;
  readonly shards: number;
}

/** Roster entries per shard, as the bake writes them (ADR-145). */
export const CAMPAIGN_ROSTER_SHARD_ENTRIES = 500;

/** One display-only estate roster entry (ADR-071): labels and canonical position, never a key. */
export interface CampaignPortfolioRosterEntry {
  readonly channel: string;
  readonly marketplace: string;
  readonly name: string | null;
  readonly position: number;
}

/** Decode a dictionary-encoded roster whose positions start at `offset` (the server's encoding). */
function rosterEntries(value: unknown, offset: number): CampaignPortfolioRosterEntry[] {
  const rosterRaw = exact(value, ['channels', 'entries', 'marketplaces']);
  if (
    !Array.isArray(rosterRaw.channels) ||
    !Array.isArray(rosterRaw.marketplaces) ||
    !Array.isArray(rosterRaw.entries)
  ) return fail();
  const channels = rosterRaw.channels.map(nonblank);
  const marketplaces = rosterRaw.marketplaces.map(nonblank);
  return rosterRaw.entries.map((entry, index) => {
    if (!Array.isArray(entry) || entry.length !== 4) return fail();
    const [name, channelIndexRaw, marketplaceIndexRaw, positionRaw] = entry;
    const channelIndex = portfolioCount(channelIndexRaw);
    const marketplaceIndex = portfolioCount(marketplaceIndexRaw);
    const position = portfolioCount(positionRaw);
    const channel = channels[channelIndex];
    const marketplace = marketplaces[marketplaceIndex];
    if (position !== offset + index || channel === undefined || marketplace === undefined) return fail();
    return {
      channel,
      marketplace,
      name: name === null ? null : nonblank(name),
      position,
    };
  });
}

/**
 * Decode one baked roster shard (ADR-145): exactly the shard asked for, naming the revision its
 * entries were baked under, each entry at its own canonical position.
 */
export function parseCampaignRosterShard(value: unknown, shard: number): {
  readonly entries: readonly CampaignPortfolioRosterEntry[];
  readonly parent_revision: string;
} {
  const item = exact(value, ['channels', 'entries', 'kind', 'marketplaces', 'parent_revision', 'shard']);
  if (item.kind !== 'campaign_roster_shard' || item.shard !== shard) return fail();
  const entries = rosterEntries(
    { channels: item.channels, entries: item.entries, marketplaces: item.marketplaces },
    shard * CAMPAIGN_ROSTER_SHARD_ENTRIES,
  );
  if (entries.length === 0 || entries.length > CAMPAIGN_ROSTER_SHARD_ENTRIES) return fail();
  return { entries, parent_revision: nonblank(item.parent_revision) };
}

function portfolioDayCount(value: DateWindow): number {
  const start = Date.parse(`${value.start}T00:00:00Z`);
  const end = Date.parse(`${value.end}T00:00:00Z`);
  return ((end - start) / 86_400_000) + 1;
}

function portfolioCount(value: unknown): number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? value : fail();
}


const portfolioMetricSemantics = {
  attributed_roas: { grain: CAMPAIGN_SUMMARY_GRAIN, unit: 'ratio' },
  ctr: { grain: CAMPAIGN_SUMMARY_GRAIN, unit: 'ratio' },
  impressions: { grain: ['period', 'marketplace'] as const, unit: 'count' },
  portfolio_roas_attributed_14d: { grain: ['period', 'marketplace'] as const, unit: 'ratio' },
  purchases: { grain: CAMPAIGN_SUMMARY_GRAIN, unit: 'count' },
  spend: { grain: CAMPAIGN_SUMMARY_GRAIN, unit: 'currency' },
} as const;
const CHANNEL_GRAIN = ['period', 'channel', 'marketplace'] as const;
/** Quotients: over a zero denominator each is typed absent, never zero (ADR-087). */
const QUOTIENT_METRICS = new Set<string>(['attributed_roas', 'ctr', 'portfolio_roas_attributed_14d', 'cpc', 'cpm', 'viewable_rate']);

function portfolioMetricValue<const Grain extends readonly string[]>(
  value: unknown,
  expectedPeriod: DateWindow,
  expectedGrain: Grain,
  expectedUnit: CampaignUnitKind,
  quotient = expectedUnit === 'ratio',
): CampaignPortfolioMetricValue {
  const item = exact(value, METRIC_VALUE_FIELDS);
  const metricPeriod = period(item.period);
  if (item.basis !== 'ad_attributed' || !sameCampaignPeriod(metricPeriod, expectedPeriod)) return fail();
  const units = campaignUnits(item.units, expectedUnit);
  // Only a quotient may be typed absent; an additive null is a contract violation (ADR-087).
  // A typed-absent quotient has no display value, a currency one (CPC, CPM) included.
  if (item.value === null) {
    if (!quotient || item.display_value !== null) return fail();
  } else if (!isDisplayValue(item.display_value, units.kind)) return fail();
  return {
    basis: 'ad_attributed',
    display_value: item.display_value,
    grain: exactGrain(item.grain, expectedGrain),
    period: metricPeriod,
    units,
    value: item.value === null ? null : exactNumber(item.value),
  };
}

function portfolioPriorIsImmediate(current: DateWindow, prior: DateWindow): boolean {
  if (portfolioDayCount(current) !== portfolioDayCount(prior)) return false;
  const priorEnd = new Date(`${current.start}T00:00:00Z`);
  priorEnd.setUTCDate(priorEnd.getUTCDate() - 1);
  return prior.end === priorEnd.toISOString().slice(0, 10);
}


function portfolioComparison<Metric extends CampaignChannelMetric>(
  value: unknown,
  expected: Metric,
  periodValue: DateWindow,
  priorValue: DateWindow,
  expectedGrain: readonly string[],
  expectedUnit: CampaignUnitKind,
): CampaignPortfolioComparison<Metric> {
  const item = exact(value, comparisonFields(value, ['absolute_change', 'current', 'metric', 'prior']));
  if (item.metric !== expected || !portfolioPriorIsImmediate(periodValue, priorValue)) return fail();
  const quotient = QUOTIENT_METRICS.has(expected);
  const current = portfolioMetricValue(item.current, periodValue, expectedGrain, expectedUnit, quotient);
  const prior = portfolioMetricValue(item.prior, priorValue, expectedGrain, expectedUnit, quotient);
  if (!sameUnits(current.units, prior.units)) return fail();
  const change = parseChange(item.change);
  if (change === undefined) return fail();
  if (current.value === null || prior.value === null) {
    if (item.absolute_change !== null || (PERCENTAGE_KEY in item && item.percentage_change !== null) || change !== null) return fail();
    return { absolute_change: null, change: null, current, metric: expected, prior, withheld: false };
  }
  // Two sides and no delta: a withheld change. The artifact holds it to the coverage block.
  if (item.absolute_change === null) {
    if (change !== null || item.percentage_change !== null) return fail();
    return { absolute_change: null, change: null, current, metric: expected, prior, withheld: true };
  }
  const absolute_change = exactNumber(item.absolute_change, false);
  if (!exactDifference(current.value, prior.value, absolute_change)) return fail();
  if (change === null) return fail();
  return { absolute_change, change, current, metric: expected, prior, withheld: false };
}

function portfolioRow(value: unknown, periodValue: DateWindow, priorValue: DateWindow): CampaignPortfolioRow {
  // A row carries no spend spark (render format 11); the retired format-10 sparked row fails `exact`.
  const item = exact(value, ['capability', 'comparisons', 'freshness_qualifiers', 'signature']);
  if (!Array.isArray(item.comparisons) || !Array.isArray(item.freshness_qualifiers)) return fail();
  const metrics: Exclude<CampaignPortfolioSortField, 'name'>[] = ['spend', 'attributed_roas', 'purchases', 'ctr'];
  if (item.comparisons.length !== metrics.length) return fail();
  const comparisons = item.comparisons.map((entry, index) => {
    const metric = metrics[index];
    const semantics = metric === undefined ? undefined : portfolioMetricSemantics[metric];
    return metric === undefined || semantics === undefined
      ? fail()
      : portfolioComparison(entry, metric, periodValue, priorValue, semantics.grain, semantics.unit);
  });
  const freshness_qualifiers = orderedStrings(item.freshness_qualifiers);
  return {
    capability: capability(item.capability),
    comparisons,
    freshness_qualifiers,
    signature: signature(item.signature),
  };
}

const FILTER_ID = /^[a-z0-9][a-z0-9_-]{0,63}$/;
const MULTI_CHANNEL_FILTERS = new Set(['all', 'sponsored']);

function channelFilter(value: unknown): CampaignChannelFilter {
  const item = exact(value, ['options', 'selected']);
  if (!Array.isArray(item.options) || item.options.length === 0) return fail();
  const options = item.options.map((entry) => {
    const option = exact(entry, ['filter_id', 'label']);
    const filter_id = nonblank(option.filter_id);
    return FILTER_ID.test(filter_id) ? { filter_id, label: nonblank(option.label) } : fail();
  });
  const ids = options.map((option) => option.filter_id);
  const selected = nonblank(item.selected);
  if (ids[0] !== 'all' || new Set(ids).size !== ids.length || !ids.includes(selected)) return fail();
  return { options, selected };
}

function channelView(
  value: unknown,
  periodValue: DateWindow,
  priorValue: DateWindow,
  offered: ReadonlySet<string>,
): CampaignChannelView | null {
  if (value === null) return null;
  const item = exact(value, [
    'by_channel', 'delivery', 'delivery_bases', 'delivery_legend', 'insights_line', 'not_added',
    'scorecard_line', 'trend_line', 'viewability', 'viewability_heading',
  ]);
  if (
    !Array.isArray(item.by_channel) || !Array.isArray(item.delivery) ||
    !Array.isArray(item.delivery_legend) || !Array.isArray(item.not_added) ||
    typeof item.viewability !== 'boolean'
  ) return fail();
  const viewability = item.viewability;
  const compare = <Metric extends CampaignChannelMetric>(entry: unknown, metric: Metric, unit: CampaignUnitKind) =>
    portfolioComparison(entry, metric, periodValue, priorValue, CHANNEL_GRAIN, unit);
  const by_channel = item.by_channel.map((entry) => {
    const row = exact(entry, ['channel', 'impressions', 'label', 'spend']);
    const channel = nonblank(row.channel);
    if (!offered.has(channel)) return fail();
    return {
      channel,
      impressions: compare(row.impressions, 'impressions', 'count'),
      label: nonblank(row.label),
      spend: compare(row.spend, 'spend', 'currency'),
    };
  });
  const delivery = item.delivery.map((entry, index) => {
    const row = exact(entry, ['channel', 'clicks', 'cpc', 'cpm', 'ctr', 'label', 'viewable_rate']);
    const channel = nonblank(row.channel);
    if (channel !== by_channel[index]?.channel) return fail();
    let viewable_rate: CampaignViewabilityCell | null = null;
    if (row.viewable_rate !== null) {
      const cell = exact(row.viewable_rate, ['absence_reason', 'comparison', 'coverage_text']);
      const comparison = cell.comparison === null ? null : compare(cell.comparison, 'viewable_rate', 'ratio');
      const absence_reason = cell.absence_reason === null ? null : nonblank(cell.absence_reason);
      if ((comparison === null) === (absence_reason === null)) return fail();
      viewable_rate = {
        absence_reason,
        comparison,
        coverage_text: cell.coverage_text === null ? null : nonblank(cell.coverage_text),
      };
    }
    if ((viewable_rate !== null) !== viewability) return fail();
    return {
      channel,
      clicks: compare(row.clicks, 'clicks', 'count'),
      cpc: compare(row.cpc, 'cpc', 'currency'),
      cpm: compare(row.cpm, 'cpm', 'currency'),
      ctr: compare(row.ctr, 'ctr', 'ratio'),
      label: nonblank(row.label),
      viewable_rate,
    };
  });
  if (delivery.length !== by_channel.length || by_channel.length === 0) return fail();
  const bases = exact(item.delivery_bases, ['cpc', 'cpm', 'ctr']);
  const not_added = item.not_added.map((entry) => {
    const tile = exact(entry, ['label', 'metric', 'reason', 'value_text']);
    return {
      label: nonblank(tile.label),
      metric: member(tile.metric, ['purchases', 'portfolio_roas_attributed_14d'] as const),
      reason: nonblank(tile.reason),
      value_text: nonblank(tile.value_text),
    };
  });
  return {
    by_channel,
    delivery,
    delivery_bases: { cpc: nonblank(bases.cpc), cpm: nonblank(bases.cpm), ctr: nonblank(bases.ctr) },
    delivery_legend: item.delivery_legend.map(nonblank),
    insights_line: nonblank(item.insights_line),
    not_added,
    scorecard_line: nonblank(item.scorecard_line),
    trend_line: nonblank(item.trend_line),
    viewability,
    viewability_heading: item.viewability_heading === null ? null : nonblank(item.viewability_heading),
  };
}

export function parseCampaignPortfolioArtifact(value: unknown): CampaignPortfolioArtifact {
  // Render format 11 only (ADR-127, ADR-119): every body names its channel filter and carries
  // its coverage key. The retained format-6, format-7 and format-10 reads retired in the
  // integration change (owner revision, 2026-10-08), so each fails `exact` like any other drift.
  // Render format 14 adds `return_lists`; a body without it fails `exact` (the server never
  // serves one: it shows the page's absence until the next bake).
  const item = exact(value, [
    EVIDENCE_STATUS_KEY, 'channel_filter', 'channel_view',
    'evidence', 'insights', 'kind', 'outcome', 'pagination', 'period', 'prior_period', 'reason_codes',
    'return_lists', 'roster', 'rows', 'sort', 'total_count', 'totals', 'trend',
  ]);
  if (
    item.kind !== 'campaign_portfolio' ||
    (item.outcome !== 'complete' && item.outcome !== 'empty' && item.outcome !== 'insufficient') ||
    !Array.isArray(item.evidence) || !Array.isArray(item.rows) || !Array.isArray(item.totals)
  ) return fail();
  const selectedPeriod = period(item.period);
  const prior_period = period(item.prior_period);
  const evidence = item.evidence.map((entry) => listEvidence(entry, selectedPeriod));
  const insights = parseCampaignInsightSlots(item.insights, portfolioInsightRoster);
  const sort = exact(item.sort, ['direction', 'field']);
  const field = member(sort.field, campaignPortfolioSortFields);
  const direction = member(sort.direction, ['asc', 'desc'] as const);
  const rows = item.rows.map((entry) => portfolioRow(entry, selectedPeriod, prior_period));
  const total_count = portfolioCount(item.total_count);
  const channel_filter = channelFilter(item.channel_filter);
  const multiChannel = MULTI_CHANNEL_FILTERS.has(channel_filter.selected);
  // A multi-channel option totals only what adds up across channels (ADR-127 decision 3).
  const totalMetrics: CampaignPortfolioMetric[] = multiChannel
    ? ['spend', 'impressions']
    : ['spend', 'purchases', 'impressions', 'portfolio_roas_attributed_14d'];
  const totals = item.totals.map((entry, index) => {
    const metric = totalMetrics[index];
    const semantics = metric === undefined ? undefined : portfolioMetricSemantics[metric];
    return metric === undefined || semantics === undefined
      ? fail()
      : portfolioComparison(
        entry,
        metric,
        selectedPeriod,
        prior_period,
        ['period', 'marketplace'],
        semantics.unit,
      );
  });
  const reason_codes = canonicalEnumArray(
    item.reason_codes,
    campaignPortfolioInsufficiencyReasons,
  );
  let trend: CampaignPortfolioArtifact['trend'] = null;
  if (item.trend !== null) {
    const rawTrend = exact(item.trend, ['basis', 'grain', 'period', 'points', 'units']);
    if (rawTrend.basis !== 'ad_attributed' || !Array.isArray(rawTrend.points)) return fail();
    const trendPeriod = period(rawTrend.period);
    if (!sameCampaignPeriod(trendPeriod, selectedPeriod)) return fail();
    const trendGrain = exactGrain(rawTrend.grain, ['date', 'marketplace']);
    const spendOnly = typeof rawTrend.units === 'object' && rawTrend.units !== null &&
      !('purchases' in rawTrend.units);
    const units = exact(rawTrend.units, spendOnly ? ['spend'] : ['portfolio_roas_attributed_14d', 'purchases', 'spend']);
    const points = rawTrend.points.map((entry) => {
      if (spendOnly) {
        const point = exact(entry, ['date', 'spend']);
        return { date: isoDate(point.date), portfolio_roas_attributed_14d: null, purchases: null, spend: exactNumber(point.spend) };
      }
      const point = exact(entry, ['date', 'portfolio_roas_attributed_14d', 'purchases', 'spend']);
      return {
        date: isoDate(point.date),
        portfolio_roas_attributed_14d: point.portfolio_roas_attributed_14d === null ? null : exactNumber(point.portfolio_roas_attributed_14d),
        purchases: exactNumber(point.purchases),
        spend: exactNumber(point.spend),
      };
    });
    if (spendOnly !== multiChannel) return fail();
    if (points.length !== portfolioDayCount(trendPeriod) || points.some((point, index) => {
      const prior = points[index - 1];
      return point.date < trendPeriod.start || point.date > trendPeriod.end ||
        (prior !== undefined && prior.date >= point.date);
    })) return fail();
    trend = {
      basis: 'ad_attributed',
      grain: trendGrain,
      period: trendPeriod,
      points,
      spend_only: spendOnly,
      units: {
        portfolio_roas_attributed_14d: spendOnly ? null : campaignUnits(units.portfolio_roas_attributed_14d, 'ratio'),
        purchases: spendOnly ? null : campaignUnits(units.purchases, 'count'),
        spend: campaignUnits(units.spend, 'currency'),
      },
    };
  }
  let pagination: CampaignPortfolioArtifact['pagination'] = null;
  if (item.pagination !== null) {
    const raw = exact(item.pagination, ['segment_count', 'segment_index', 'segment_size', 'total_count']);
    const segment_index = portfolioCount(raw.segment_index);
    const segment_count = portfolioCount(raw.segment_count);
    const paginationTotal = portfolioCount(raw.total_count);
    if (
      raw.segment_size !== 50 || paginationTotal < 1 ||
      segment_count !== Math.ceil(paginationTotal / 50) ||
      segment_index >= segment_count
    ) return fail();
    pagination = { segment_count, segment_index, segment_size: 50, total_count: paginationTotal };
  }
  const shardsRaw = item.roster !== null && typeof item.roster === 'object' && !Array.isArray(item.roster) && 'shards' in item.roster
    ? exact(item.roster, ['revision', 'shards'])
    : null;
  const roster_shards: CampaignPortfolioRosterShards | null = shardsRaw === null
    ? null
    : { revision: nonblank(shardsRaw.revision), shards: portfolioCount(shardsRaw.shards) };
  const roster = roster_shards === null ? rosterEntries(item.roster, 0) : [];
  const rosterCount = roster_shards === null ? roster.length : total_count;
  if (roster_shards !== null && (item.outcome !== 'complete' || roster_shards.shards !== Math.ceil(total_count / CAMPAIGN_ROSTER_SHARD_ENTRIES))) return fail();
  const expectedRows = pagination === null
    ? 0
    : Math.min(50, pagination.total_count - pagination.segment_index * 50);
  if (
    evidence.length === 0 || rows.length > 50 ||
    (pagination === null && rows.length > 0) ||
    (pagination !== null && (
      rows.length !== expectedRows || total_count !== pagination.total_count ||
      rosterCount !== total_count
    )) ||
    (rows.length > 0 && rosterCount === 0) ||
    (item.outcome === 'complete' && (rows.length === 0 || totals.length !== totalMetrics.length || trend === null || reason_codes.length > 0)) ||
    (item.outcome === 'empty' && (
      rows.length > 0 || total_count !== 0 || pagination !== null || roster.length > 0 || totals.length > 0 || trend !== null || reason_codes.length > 0
    )) ||
    (item.outcome === 'insufficient' && (
      rows.length > 0 || total_count !== 0 || pagination !== null || roster.length > 0 || totals.length > 0 || trend !== null || reason_codes.length === 0
    ))
  ) return fail();
  const evidence_status = coverageBlock(item.evidence_status, selectedPeriod);
  // ADR-119 decision 4: the block is null exactly on an empty or insufficient portfolio.
  if ((evidence_status !== null) !== (item.outcome === 'complete')) return fail();
  const channel_view = channelView(item.channel_view, selectedPeriod, prior_period, new Set(channel_filter.options.map((option) => option.filter_id)));
  if ((channel_view !== null) !== (multiChannel && item.outcome === 'complete')) return fail();
  // Delivery per channel reads its own prefixed coverage slots, withheld exactly where they say.
  if (channel_view !== null && channel_view.delivery.some((row) => {
    const viewable = row.viewable_rate?.comparison ?? null;
    return viewable !== null && viewable.current.value !== null && viewable.prior.value !== null &&
      viewable.withheld !== (
        isWithheld(coverageEntry(evidence_status, 'channel_viewable_rate')) ||
        // A prior-window gap has its own entry, listed only while it misses days (ADR-118 d5).
        isWithheld(coverageEntry(evidence_status, 'channel_viewable_rate', 'prior'))
      );
  })) return fail();
  // A change is withheld exactly where the coverage block says a day is missing or partial.
  const comparisons = [...totals, ...rows.flatMap((row) => row.comparisons)];
  if (comparisons.some((entry) => (
    // An undefined ratio (ADR-087) has no change to withhold, whatever its coverage.
    entry.current.value !== null && entry.prior.value !== null &&
    entry.withheld !== isWithheld(coverageEntry(evidence_status, entry.metric))
  ))) return fail();
  const return_lists = returnLists(item.return_lists, selectedPeriod);
  if ((return_lists !== null) !== (item.outcome === 'complete')) return fail();
  return {
    channel_filter,
    channel_view,
    evidence,
    evidence_status,
    insights,
    kind: 'campaign_portfolio',
    outcome: item.outcome,
    pagination,
    period: selectedPeriod,
    prior_period,
    reason_codes,
    return_lists,
    roster,
    roster_shards,
    rows,
    sort: { direction, field },
    total_count,
    totals,
    trend,
  };
}

/** Decode the served return lists: equal length, each ROAS defined, at the row grain. */
function returnLists(value: unknown, periodValue: DateWindow): CampaignReturnLists | null {
  if (value === null) return null;
  const item = exact(value, ['highest', 'lowest']);
  if (!Array.isArray(item.highest) || !Array.isArray(item.lowest) || item.highest.length !== item.lowest.length) return fail();
  const entry = (raw: unknown): CampaignReturnEntry => {
    const served = exact(raw, ['attributed_roas', 'signature', 'spend']);
    const attributed_roas = portfolioMetricValue(served.attributed_roas, periodValue, portfolioMetricSemantics.attributed_roas.grain, 'ratio');
    if (attributed_roas.value === null) return fail();
    return {
      attributed_roas,
      signature: signature(served.signature),
      spend: portfolioMetricValue(served.spend, periodValue, portfolioMetricSemantics.spend.grain, 'currency'),
    };
  };
  return { highest: item.highest.map(entry), lowest: item.lowest.map(entry) };
}

/**
 * The Campaigns answer (plan template): the served campaign count and total spend with its served
 * change, or, with one channel chosen, that channel's served return on its spend. Empty and
 * insufficient portfolios read their served absence copy (`absenceText`).
 */
export function campaignsAnswer(artifact: CampaignPortfolioArtifact, absenceText: string): Answer {
  if (artifact.outcome !== 'complete') return { kind: 'fallback', text: absenceText };
  const spend = artifact.totals.find((entry) => entry.metric === 'spend') ?? null;
  const money = spend === null || spend.current.display_value === null || spend.current.units.currency_code === null
    ? null
    : formatCurrency(spend.current.display_value, spend.current.units.currency_code);
  if (money === null) return { kind: 'fallback', text: absenceText };
  const window = windowText(artifact.period);
  const channel = artifact.channel_view === null
    ? artifact.channel_filter.options.find((option) => option.filter_id === artifact.channel_filter.selected)?.label ?? null
    : null;
  if (channel !== null) {
    const roas = artifact.totals.find((entry) => entry.metric === 'portfolio_roas_attributed_14d') ?? null;
    if (roas === null || roas.current.value === null) {
      return { kind: 'fallback', text: `${channel} spent ${money} in ${window}; its return isn't available for this window.` };
    }
    return { kind: 'answer', text: `${channel} returned ${formatMultiplierText(roas.current.value)} on ${money} in ${window}.` };
  }
  const count = `${formatCount(String(artifact.total_count))} ${artifact.total_count === 1 ? 'campaign' : 'campaigns'}`;
  const moved = spend?.withheld === true ? null : changeClause(spend?.change ?? null);
  const lead = `${count} spent ${money} in ${window}`;
  return { kind: 'answer', text: moved === null ? `${lead}.` : `${lead}, ${moved} on ${windowText(artifact.prior_period)}.` };
}

/** A served multiplier at two places with the times sign, as the page's tiles show it (D4). */
function formatMultiplierText(value: string | number): string {
  return displayFigure('portfolio_roas_attributed_14d', String(value), 'ratio', null).text;
}
