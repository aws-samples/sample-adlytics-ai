// SPDX-License-Identifier: MIT-0
/**
 * Strict browser decoder for `ReachAndFunnelArtifact` v1.
 *
 * Every visible value and explanation remains the server's `display_text` or paired absence
 * text. This module validates structure only: it never derives a share, rate, change, total,
 * or formatted value from an exact decimal.
 */

export class ReachDecodeError extends Error {
  constructor() {
    super('The Reach and funnel artifact is invalid.');
    this.name = 'ReachDecodeError';
  }
}

const fail = (): never => {
  throw new ReachDecodeError();
};

type Item = Record<string, unknown>;

function record(value: unknown, keys: readonly string[]): Item {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return fail();
  const item = value as Item;
  const actual = Object.keys(item);
  if (actual.length !== keys.length || keys.some((key) => !actual.includes(key))) return fail();
  return item;
}

function list(value: unknown): readonly unknown[] {
  return Array.isArray(value) ? value : fail();
}

function text(value: unknown): string {
  return typeof value === 'string' && value.trim() ? value : fail();
}

function nullableText(value: unknown): string | null {
  return value === null ? null : text(value);
}

function exactText<const Expected extends string>(value: unknown, expected: Expected): Expected {
  return text(value) === expected ? expected : fail();
}

function enumValue<const Values extends readonly string[]>(value: unknown, values: Values): Values[number] {
  const candidate = text(value);
  return values.includes(candidate) ? candidate as Values[number] : fail();
}

const DECIMAL = /^-?\d+(\.\d+)?$/;
const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;
const ISO_INSTANT = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{6})?\+00:00$/;

function decimal(value: unknown): string {
  return typeof value === 'string' && DECIMAL.test(value) ? value : fail();
}

function isoDate(value: unknown): string {
  const candidate = text(value);
  if (!ISO_DATE.test(candidate)) return fail();
  const parsed = new Date(`${candidate}T00:00:00Z`);
  return !Number.isNaN(parsed.valueOf()) && parsed.toISOString().slice(0, 10) === candidate
    ? candidate
    : fail();
}

const DISPLAY_MONTHS: Readonly<Record<string, string>> = {
  Apr: '04', Aug: '08', Dec: '12', Feb: '02', Jan: '01', Jul: '07', Jun: '06',
  Mar: '03', May: '05', Nov: '11', Oct: '10', Sep: '09',
};

function expectedAfterInText(value: unknown, prefix: string): string {
  const line = text(value);
  if (!line.startsWith(prefix) || !line.endsWith('.')) return fail();
  const match = /^(\d{1,2}) ([A-Z][a-z]{2}) (\d{4})$/.exec(line.slice(prefix.length, -1));
  if (match === null) return fail();
  const [, day, monthName, year] = match;
  const month = DISPLAY_MONTHS[monthName ?? ''];
  if (day === undefined || month === undefined || year === undefined) return fail();
  return isoDate(`${year}-${month}-${day.padStart(2, '0')}`);
}

function nullableInstant(value: unknown): string | null {
  if (value === null) return null;
  const candidate = text(value);
  return ISO_INSTANT.test(candidate) && !Number.isNaN(Date.parse(candidate)) ? candidate : fail();
}

export type ReachPreset = 'last_complete_month' | 'last_complete_quarter' | 'last_complete_week';
export type ReachGrain = 'month' | 'quarter' | 'week';
export type ReachOutcome = 'complete' | 'empty' | 'insufficient';
export type ReachAbsenceReason =
  | 'change_unavailable'
  | 'currency_mismatch'
  | 'no_sales'
  | 'no_shoppers'
  | 'no_spend'
  | 'not_available_per_channel'
  | 'not_returned'
  | 'pack_variant_mismatch'
  | 'prior_unavailable';
export type ReachUnit = 'count' | 'currency' | 'multiplier' | 'share';

export interface ReachPeriod {
  readonly end: string;
  readonly grain: ReachGrain;
  readonly label: string;
  readonly start: string;
}

export interface ReachAbsence {
  readonly reason: ReachAbsenceReason;
  readonly text: string;
}

export type ReachFigure =
  | {
    readonly absence: null;
    readonly currency_code: string | null;
    readonly display_text: string;
    readonly unit: ReachUnit;
    readonly value: string;
  }
  | {
    readonly absence: ReachAbsence;
    readonly currency_code: null;
    readonly display_text: null;
    readonly unit: null;
    readonly value: null;
  };

export type ReachChange =
  | { readonly absence: null; readonly text: string; readonly value: string }
  | { readonly absence: ReachAbsence; readonly text: null; readonly value: null };

const ABSENCE_TEXT: Readonly<Record<Exclude<ReachAbsenceReason, 'prior_unavailable'>, string>> = {
  change_unavailable: "No change is shown, because a figure isn't available.",
  currency_mismatch: "Spend and sales are in different currencies, so return isn't shown.",
  no_sales: 'No sales were recorded for this group.',
  no_shoppers: 'No shoppers were reached in this group.',
  no_spend: 'No spend was recorded for these shoppers.',
  not_available_per_channel: 'Not available per channel',
  not_returned: 'too few shoppers, or none',
  pack_variant_mismatch: "No change is shown, because a figure isn't available.",
};

function absence(
  value: unknown,
  allowed: readonly ReachAbsenceReason[],
  grain: ReachGrain,
): ReachAbsence {
  const item = record(value, ['reason', 'text']);
  const reason = enumValue(item.reason, allowed);
  const expected = reason === 'prior_unavailable'
    ? `The previous ${grain} isn't available.`
    : ABSENCE_TEXT[reason];
  return { reason, text: exactText(item.text, expected) };
}

function figure(
  value: unknown,
  allowed: readonly ReachAbsenceReason[],
  units: readonly ReachUnit[],
  grain: ReachGrain,
): ReachFigure {
  if (typeof value === 'object' && value !== null && 'absence' in value) {
    const item = record(value, ['absence']);
    return {
      absence: absence(item.absence, allowed, grain),
      currency_code: null,
      display_text: null,
      unit: null,
      value: null,
    };
  }
  const item = record(value, ['currency_code', 'display_text', 'unit', 'value']);
  const unit = enumValue(item.unit, units);
  const currency = nullableText(item.currency_code);
  if ((unit === 'currency') !== (currency !== null)) return fail();
  return {
    absence: null,
    currency_code: currency,
    display_text: text(item.display_text),
    unit,
    value: decimal(item.value),
  };
}

function nullableFigure(
  value: unknown,
  allowed: readonly ReachAbsenceReason[],
  units: readonly ReachUnit[],
  grain: ReachGrain,
): ReachFigure | null {
  return value === null ? null : figure(value, allowed, units, grain);
}

const CHANGE_REASONS = ['change_unavailable', 'pack_variant_mismatch', 'prior_unavailable'] as const;

function change(value: unknown, grain: ReachGrain): ReachChange {
  if (typeof value === 'object' && value !== null && 'absence' in value) {
    const item = record(value, ['absence']);
    return { absence: absence(item.absence, CHANGE_REASONS, grain), text: null, value: null };
  }
  const item = record(value, ['text', 'value']);
  return { absence: null, text: text(item.text), value: decimal(item.value) };
}

function validateChange(current: ReachFigure, prior: ReachFigure | null, compared: ReachChange): void {
  if (prior === null) {
    if (compared.absence?.reason !== 'prior_unavailable' && compared.absence?.reason !== 'pack_variant_mismatch') fail();
    return;
  }
  const available = current.value !== null && prior.value !== null;
  if (available !== (compared.value !== null)) fail();
  if (!available && compared.absence?.reason !== 'change_unavailable') fail();
}

const TILE_ROSTER = [
  ['overlap_share', "Saw both within 14 days of their first ad · Amazon account IDs · a lower bound: DSP impressions Amazon couldn't link to an account aren't matched"],
  ['overlap_share_all_ids', 'Saw both within 14 days of their first ad · all ID types · overlap is understated'],
  ['dsp_only_reach_share', 'Saw DSP only within 14 days of their first ad · Amazon account IDs'],
] as const;
const GROUP_ROSTER = [
  ['dsp_only', 'Saw DSP only'],
  ['sponsored_only', 'Saw sponsored ads only'],
  ['dsp_and_sponsored', 'Saw both'],
] as const;
const FUNNEL_ROSTER = [
  ['search_rate', 'Search rate', 'share'],
  ['purchaser_rate', 'Purchaser rate', 'share'],
  ['revenue_per_exposed_shopper', 'Sales per shopper reached', 'currency'],
  ['purchaser_rate_index_vs_sponsored_only', 'Purchaser rate index vs sponsored ads only', 'multiplier'],
] as const;
const FUNNEL_CAVEAT = [
  'This shows association, not cause. Shoppers were not randomly assigned to groups.',
  'Shoppers can see an ad after they buy, so groups that include DSP retargeting may look stronger.',
  'Sponsored ads appear when shoppers search, so sponsored groups start closer to buying.',
] as const;
const FREQUENCY_CAVEAT = [
  ...FUNNEL_CAVEAT,
  'Shoppers can see more ads after they buy, so higher buckets may look stronger.',
] as const;
const FINE_BUCKETS = [
  ['1', '1 ad'], ['2', '2 ads'], ['3', '3 ads'], ['4', '4 ads'], ['5', '5 ads'],
  ['6', '6 ads'], ['7', '7 ads'], ['8', '8 ads'], ['9', '9 ads'], ['10_plus', '10 or more'],
] as const;
const COARSE_BUCKETS = [
  ['1', '1 ad'], ['2', '2 ads'], ['3', '3 ads'], ['4_6', '4 to 6'],
  ['7_9', '7 to 9'], ['10_plus', '10 or more'],
] as const;
const AXIS_LABELS = {
  coarse: 'Ads seen within 14 days of the first ad, in coarse buckets: 1, 2, 3, 4 to 6, 7 to 9, then 10 or more',
  fine: 'Ads seen within 14 days of the first ad: 1 to 9, then 10 or more',
} as const;

function exactTexts(value: unknown, expected: readonly string[]): readonly string[] {
  const values = list(value).map(text);
  if (values.length !== expected.length || values.some((entry, index) => entry !== expected[index])) return fail();
  return values;
}

export interface ReachOverlapTile {
  readonly change: ReachChange;
  readonly current: ReachFigure;
  readonly label: string;
  readonly metric: typeof TILE_ROSTER[number][0];
  readonly prior: ReachFigure | null;
}

export interface ReachGroup {
  readonly account: ReachFigure;
  readonly all_ids: ReachFigure;
  readonly change: ReachChange;
  readonly exposure_group: typeof GROUP_ROSTER[number][0];
  readonly label: string;
  readonly prior_account: ReachFigure | null;
}

export interface ReachOverlap {
  readonly all_ids_label: 'All ID types';
  readonly groups: readonly ReachGroup[];
  readonly state: 'figures' | 'needs_dsp' | 'outside_declared_periods';
  readonly subtitle: string | null;
  readonly text: string | null;
  readonly tiles: readonly ReachOverlapTile[];
  readonly window_notes: readonly string[];
}

function overlap(value: unknown, period: ReachPeriod, preset: ReachPreset): ReachOverlap {
  const item = record(value, ['all_ids_label', 'groups', 'state', 'subtitle', 'text', 'tiles', 'window_notes']);
  const allIdsLabel = exactText(item.all_ids_label, 'All ID types');
  const state = enumValue(item.state, ['figures', 'needs_dsp', 'outside_declared_periods'] as const);
  if ((preset === 'last_complete_quarter') !== (state === 'outside_declared_periods')) return fail();
  if (state === 'outside_declared_periods') {
    exactText(item.text, 'Overlap is measured for complete weeks and months. Choose Latest measured month to see it.');
  } else if (state === 'needs_dsp') {
    exactText(item.text, 'Needs DSP and sponsored ads on one AMC instance');
  } else if (item.text !== null) return fail();
  if (state !== 'figures') {
    if (item.subtitle !== null || list(item.window_notes).length || list(item.tiles).length || list(item.groups).length) return fail();
    return { all_ids_label: allIdsLabel, groups: [], state, subtitle: null, text: text(item.text), tiles: [], window_notes: [] };
  }
  const subtitle = exactText(
    item.subtitle,
    `Shoppers reached in ${period.label}, grouped by the ads they saw within 14 days of their first ad in that ${period.grain}.`,
  );
  const notes = exactTexts(item.window_notes, [
    "These groups use each shopper's 14-day window, so they don't match Amazon's own period reach reports.",
    `Ads seen before the ${period.grain} began aren't counted.`,
  ]);
  const tiles = list(item.tiles).map((entry, index): ReachOverlapTile => {
    const expected = TILE_ROSTER[index] ?? fail();
    const tile = record(entry, ['change', 'current', 'label', 'metric', 'prior']);
    if (tile.metric !== expected[0] || tile.label !== expected[1]) return fail();
    const current = figure(tile.current, ['not_returned', 'no_shoppers'], ['share'], period.grain);
    const prior = nullableFigure(tile.prior, ['not_returned', 'no_shoppers'], ['share'], period.grain);
    const compared = change(tile.change, period.grain);
    validateChange(current, prior, compared);
    return { change: compared, current, label: expected[1], metric: expected[0], prior };
  });
  if (tiles.length !== TILE_ROSTER.length) return fail();
  const groups = list(item.groups).map((entry, index): ReachGroup => {
    const expected = GROUP_ROSTER[index] ?? fail();
    const group = record(entry, ['account', 'all_ids', 'change', 'exposure_group', 'label', 'prior_account']);
    if (group.exposure_group !== expected[0] || group.label !== expected[1]) return fail();
    const account = figure(group.account, ['not_returned'], ['count'], period.grain);
    const allIds = figure(group.all_ids, ['not_returned'], ['count'], period.grain);
    const prior = nullableFigure(group.prior_account, ['not_returned'], ['count'], period.grain);
    const compared = change(group.change, period.grain);
    validateChange(account, prior, compared);
    return {
      account,
      all_ids: allIds,
      change: compared,
      exposure_group: expected[0],
      label: expected[1],
      prior_account: prior,
    };
  });
  if (groups.length !== GROUP_ROSTER.length) return fail();
  return { all_ids_label: allIdsLabel, groups, state, subtitle, text: null, tiles, window_notes: notes };
}

export interface ReachFunnelColumn {
  readonly label: string;
  readonly metric: typeof FUNNEL_ROSTER[number][0];
}

export interface ReachFunnelRow {
  readonly exposure_group: typeof GROUP_ROSTER[number][0];
  readonly figures: Readonly<Record<typeof FUNNEL_ROSTER[number][0], ReachFigure>>;
  readonly label: string;
}

export interface ReachFunnel {
  readonly caveat: readonly string[];
  readonly columns: readonly ReachFunnelColumn[];
  readonly index_note: string | null;
  readonly rows: readonly ReachFunnelRow[];
  readonly state: 'figures' | 'needs_dsp' | 'outside_declared_periods';
  readonly text: string | null;
}

function funnel(value: unknown, period: ReachPeriod, preset: ReachPreset): ReachFunnel {
  const item = record(value, ['caveat', 'columns', 'index_note', 'rows', 'state', 'text']);
  const state = enumValue(item.state, ['figures', 'needs_dsp', 'outside_declared_periods'] as const);
  if ((preset === 'last_complete_quarter') !== (state === 'outside_declared_periods')) return fail();
  if (state === 'outside_declared_periods') {
    exactText(item.text, 'Full funnel is measured for complete weeks and months. Choose Latest measured month to see it.');
  } else if (state === 'needs_dsp') {
    exactText(item.text, 'Needs DSP and sponsored ads on one AMC instance');
  } else if (item.text !== null) return fail();
  if (state !== 'figures') {
    if (list(item.caveat).length || list(item.columns).length || list(item.rows).length || item.index_note !== null) return fail();
    return { caveat: [], columns: [], index_note: null, rows: [], state, text: text(item.text) };
  }
  const caveat = exactTexts(item.caveat, FUNNEL_CAVEAT);
  const columns = list(item.columns).map((entry, index): ReachFunnelColumn => {
    const expected = FUNNEL_ROSTER[index] ?? fail();
    const column = record(entry, ['label', 'metric']);
    if (column.metric !== expected[0] || column.label !== expected[1]) return fail();
    return { label: expected[1], metric: expected[0] };
  });
  if (columns.length !== FUNNEL_ROSTER.length) return fail();
  const rows = list(item.rows).map((entry, index): ReachFunnelRow => {
    const expectedGroup = GROUP_ROSTER[index] ?? fail();
    const row = record(entry, ['exposure_group', 'figures', 'label']);
    if (row.exposure_group !== expectedGroup[0] || row.label !== expectedGroup[1]) return fail();
    const rawFigures = record(row.figures, FUNNEL_ROSTER.map(([metric]) => metric));
    const figures = Object.fromEntries(FUNNEL_ROSTER.map(([metric, , unit]) => [
      metric,
      figure(
        rawFigures[metric],
        unit === 'currency' ? ['no_sales', 'no_shoppers', 'not_returned'] : ['no_shoppers', 'not_returned'],
        [unit],
        period.grain,
      ),
    ])) as unknown as ReachFunnelRow['figures'];
    return { exposure_group: expectedGroup[0], figures, label: expectedGroup[1] };
  });
  if (rows.length !== GROUP_ROSTER.length) return fail();
  return {
    caveat,
    columns,
    index_note: exactText(item.index_note, 'may understate the difference between groups'),
    rows,
    state,
    text: null,
  };
}

export interface ReachFrequencyBucket {
  readonly bucket: string;
  readonly label: string;
  readonly platform_return: ReachFigure;
  readonly purchaser_rate: ReachFigure;
}

export interface ReachFrequencyChannel {
  readonly ad_product_key: string;
  readonly cells: Readonly<Record<string, ReachFigure>>;
  readonly label: string;
}

export interface ReachPerChannel {
  readonly foot: string;
  readonly label: string;
  readonly platform_label: string;
  readonly rows: readonly ReachFrequencyChannel[];
  readonly unavailable_reason: string;
}

export interface ReachFrequency {
  readonly axis_label: string | null;
  readonly bucket_scheme: 'coarse' | 'fine' | null;
  readonly buckets: readonly ReachFrequencyBucket[];
  readonly caveat: readonly string[];
  readonly expected_after: string | null;
  readonly per_channel: ReachPerChannel | null;
  readonly state: 'figures' | 'needs_campaigns' | 'outside_declared_periods' | 'pending';
  readonly text: string | null;
}

function perChannel(value: unknown, buckets: readonly (readonly [string, string])[], period: ReachPeriod): ReachPerChannel {
  const item = record(value, ['foot', 'label', 'platform_label', 'rows', 'unavailable_reason']);
  const rows = list(item.rows).map((entry): ReachFrequencyChannel => {
    const row = record(entry, ['ad_product_key', 'cells', 'label']);
    const key = text(row.ad_product_key);
    const label = text(row.label);
    if (/(^|[_-])(all|subtotal|summary|total)([_-]|$)/i.test(key) || /\b(all channels|subtotal|total)\b/i.test(label)) return fail();
    const expectedKeys = buckets.map(([bucket]) => bucket);
    const rawCells = record(row.cells, expectedKeys);
    const cells = Object.fromEntries(expectedKeys.map((bucket) => {
      const parsed = figure(rawCells[bucket], ['not_available_per_channel'], [], period.grain);
      if (parsed.absence?.reason !== 'not_available_per_channel') return fail();
      return [bucket, parsed];
    }));
    return { ad_product_key: key, cells, label };
  });
  if (new Set(rows.map((row) => row.ad_product_key)).size !== rows.length) return fail();
  return {
    foot: exactText(item.foot, "Attributed sales aren't added across channels, because one purchase can be credited to more than one channel."),
    label: exactText(item.label, `Credited revenue ÷ spend · linear model · 14-day lookback from each shopper's first ad · includes brand halo · ${period.label}`),
    platform_label: text(item.platform_label),
    rows,
    unavailable_reason: exactText(item.unavailable_reason, "one purchase can follow ads on several channels, and we can't split its credit fairly here. See the platform view."),
  };
}

function frequency(value: unknown, period: ReachPeriod, preset: ReachPreset): ReachFrequency {
  const item = record(value, [
    'axis_label', 'bucket_scheme', 'buckets', 'caveat', 'expected_after', 'per_channel', 'state', 'text',
  ]);
  const state = enumValue(item.state, ['figures', 'needs_campaigns', 'outside_declared_periods', 'pending'] as const);
  const expectedAfter = item.expected_after === null ? null : isoDate(item.expected_after);
  if ((state === 'pending') !== (expectedAfter !== null)) return fail();
  if ((preset === 'last_complete_week') !== (state === 'outside_declared_periods')) return fail();
  if (state === 'outside_declared_periods') {
    exactText(item.text, 'Frequency is measured for complete months and quarters. Choose Latest measured month to see it.');
  } else if (state === 'needs_campaigns') {
    exactText(item.text, 'Needs at least 4 active campaigns');
  } else if (state === 'pending') {
    const prefix = `Figures for ${period.label} are expected after `;
    if (expectedAfterInText(item.text, prefix) !== expectedAfter) return fail();
  } else if (item.text !== null) return fail();
  if (state !== 'figures') {
    if (
      item.bucket_scheme !== null || item.axis_label !== null || item.per_channel !== null ||
      list(item.caveat).length || list(item.buckets).length
    ) return fail();
    return {
      axis_label: null,
      bucket_scheme: null,
      buckets: [],
      caveat: [],
      expected_after: expectedAfter,
      per_channel: null,
      state,
      text: text(item.text),
    };
  }
  const scheme = enumValue(item.bucket_scheme, ['coarse', 'fine'] as const);
  const roster = scheme === 'fine' ? FINE_BUCKETS : COARSE_BUCKETS;
  const caveat = exactTexts(item.caveat, FREQUENCY_CAVEAT);
  const buckets = list(item.buckets).map((entry, index): ReachFrequencyBucket => {
    const expected = roster[index] ?? fail();
    const bucket = record(entry, ['bucket', 'label', 'platform_return', 'purchaser_rate']);
    if (bucket.bucket !== expected[0] || bucket.label !== expected[1]) return fail();
    return {
      bucket: expected[0],
      label: expected[1],
      platform_return: figure(bucket.platform_return, ['currency_mismatch', 'no_spend', 'not_returned'], ['multiplier'], period.grain),
      purchaser_rate: figure(bucket.purchaser_rate, ['no_shoppers', 'not_returned'], ['share'], period.grain),
    };
  });
  if (buckets.length !== roster.length) return fail();
  return {
    axis_label: exactText(item.axis_label, AXIS_LABELS[scheme]),
    bucket_scheme: scheme,
    buckets,
    caveat,
    expected_after: null,
    per_channel: perChannel(item.per_channel, roster, period),
    state,
    text: null,
  };
}

export type ReachInsightId = 'frequency-saturation' | 'full-funnel-association' | 'reach-overlap';
export interface ReachInsightSlot {
  readonly absence_class: 'evaluated-quiet' | 'generation-pending' | 'ineligible' | 'out-of-slice' | null;
  readonly absence_reason: string | null;
  readonly artifact_revision: string;
  readonly evidence_line: string | null;
  readonly id: ReachInsightId;
  readonly output_class: 'perspective';
  readonly prompt_version: string;
  readonly state: 'filled' | 'silent' | 'unavailable';
  readonly text: string | null;
}

const INSIGHT_ROSTER = ['reach-overlap', 'full-funnel-association', 'frequency-saturation'] as const;

function insight(value: unknown, expected: ReachInsightId): ReachInsightSlot {
  const absent = typeof value === 'object' && value !== null && 'absence_class' in value;
  const item = record(value, absent
    ? ['absence_class', 'absence_reason', 'artifact_revision', 'evidence_line', 'id', 'output_class', 'prompt_version', 'state', 'text']
    : ['artifact_revision', 'evidence_line', 'id', 'output_class', 'prompt_version', 'state', 'text']);
  if (item.id !== expected || item.output_class !== 'perspective') return fail();
  const state = enumValue(item.state, ['filled', 'silent', 'unavailable'] as const);
  const filled = state === 'filled';
  if (filled === absent) return fail();
  const slotText = nullableText(item.text);
  const evidenceLine = nullableText(item.evidence_line);
  if (filled !== (slotText !== null) || filled !== (evidenceLine !== null)) return fail();
  const absenceClass = absent
    ? enumValue(item.absence_class, ['evaluated-quiet', 'generation-pending', 'ineligible', 'out-of-slice'] as const)
    : null;
  const absenceReason = absent ? text(item.absence_reason) : null;
  return {
    absence_class: absenceClass,
    absence_reason: absenceReason,
    artifact_revision: text(item.artifact_revision),
    evidence_line: evidenceLine,
    id: expected,
    output_class: 'perspective',
    prompt_version: text(item.prompt_version),
    state,
    text: slotText,
  };
}

export interface ReachEvidenceStatusSlot {
  readonly marketplace_key: string;
  readonly missing_dates: readonly { readonly end: string; readonly start: string }[];
  readonly reason_text: string;
  readonly recovery_text: string | null;
  readonly role_label: string;
  readonly slot_label: string;
}

export interface ReachEvidenceStatus {
  readonly slots: readonly ReachEvidenceStatusSlot[];
  readonly version: 2;
}

function evidenceStatus(
  value: unknown,
  period: ReachPeriod,
  pendingMarkets: ReadonlyMap<string, string>,
): ReachEvidenceStatus {
  const item = record(value, ['slots', 'version']);
  if (item.version !== 2) return fail();
  const slots = list(item.slots).map((entry): ReachEvidenceStatusSlot => {
    const slot = record(entry, [
      'marketplace_key', 'missing_dates', 'reason_text', 'recovery_text', 'role_label', 'slot_label',
    ]);
    const marketplace = text(slot.marketplace_key);
    const pendingText = pendingMarkets.get(marketplace);
    if (pendingText === undefined) return fail();
    const dates = list(slot.missing_dates).map((raw) => {
      const window = record(raw, ['end', 'start']);
      const start = isoDate(window.start);
      const end = isoDate(window.end);
      if (start !== period.start || end !== period.end) return fail();
      return { end, start };
    });
    if (dates.length !== 1 || slot.reason_text !== pendingText || slot.recovery_text !== null) return fail();
    return {
      marketplace_key: marketplace,
      missing_dates: dates,
      reason_text: pendingText,
      recovery_text: null,
      role_label: exactText(slot.role_label, 'This period'),
      slot_label: exactText(slot.slot_label, 'Frequency'),
    };
  });
  if (slots.length !== pendingMarkets.size || new Set(slots.map((slot) => slot.marketplace_key)).size !== slots.length) return fail();
  return { slots, version: 2 };
}

export interface ReachMarket {
  readonly frequency: ReachFrequency;
  readonly funnel: ReachFunnel | null;
  readonly insights: readonly ReachInsightSlot[];
  readonly marketplace_key: string;
  readonly outcome: ReachOutcome;
  readonly overlap: ReachOverlap | null;
}

function market(value: unknown, period: ReachPeriod, preset: ReachPreset): ReachMarket {
  const item = record(value, ['frequency', 'funnel', 'insights', 'marketplace_key', 'outcome', 'overlap']);
  const outcome = enumValue(item.outcome, ['complete', 'empty', 'insufficient'] as const);
  const complete = outcome === 'complete';
  if (complete !== (item.overlap !== null && item.funnel !== null)) return fail();
  const parsedOverlap = item.overlap === null ? null : overlap(item.overlap, period, preset);
  const parsedFunnel = item.funnel === null ? null : funnel(item.funnel, period, preset);
  if (parsedOverlap !== null && parsedFunnel !== null && parsedOverlap.state !== parsedFunnel.state) return fail();
  const parsedFrequency = frequency(item.frequency, period, preset);
  const insights = list(item.insights).map((entry, index) => insight(entry, INSIGHT_ROSTER[index] ?? fail()));
  if (insights.length !== INSIGHT_ROSTER.length || new Set(insights.map((slot) => slot.artifact_revision)).size !== 1) return fail();
  if (parsedOverlap?.state !== 'figures' && insights[0]?.state === 'filled') return fail();
  if (parsedFunnel?.state !== 'figures' && insights[1]?.state === 'filled') return fail();
  if (parsedFrequency.state !== 'figures' && insights[2]?.state === 'filled') return fail();
  return {
    frequency: parsedFrequency,
    funnel: parsedFunnel,
    insights,
    marketplace_key: text(item.marketplace_key),
    outcome,
    overlap: parsedOverlap,
  };
}

export interface ReachAndFunnelArtifact {
  readonly boundary: readonly string[];
  readonly evidence: {
    readonly fence_check: 'structural check, not the exact pack text';
    readonly pre_exposure_exclusion: 'pending_step_b';
  };
  readonly evidence_status: ReachEvidenceStatus;
  readonly markets: readonly ReachMarket[];
  readonly outcome: ReachOutcome;
  readonly page_context_expires_at: string | null;
  readonly page_context_label: string;
  readonly period: ReachPeriod;
  readonly preset: ReachPreset;
  readonly starter_questions: readonly string[];
}

const ARTIFACT_KEYS = [
  'boundary', 'evidence', 'evidence_status', 'markets', 'outcome', 'page_context_expires_at',
  'page_context_label', 'period', 'preset', 'starter_questions', 'type', 'version',
] as const;

/** Decode one Reach body; any contract drift is refused rather than repaired in the browser. */
export function parseReachAndFunnelArtifact(value: unknown): ReachAndFunnelArtifact {
  const item = record(value, ARTIFACT_KEYS);
  if (item.type !== 'ReachAndFunnelArtifact' || item.version !== 1) return fail();
  const preset = enumValue(item.preset, ['last_complete_month', 'last_complete_quarter', 'last_complete_week'] as const);
  const periodItem = record(item.period, ['end', 'grain', 'label', 'start']);
  const period: ReachPeriod = {
    end: isoDate(periodItem.end),
    grain: enumValue(periodItem.grain, ['month', 'quarter', 'week'] as const),
    label: text(periodItem.label),
    start: isoDate(periodItem.start),
  };
  if (period.end < period.start || preset !== `last_complete_${period.grain}`) return fail();
  const outcome = enumValue(item.outcome, ['complete', 'empty', 'insufficient'] as const);
  const markets = list(item.markets).map((entry) => market(entry, period, preset));
  // marketplace_key is machine-only and has no customer label in v1, so presentation is safe
  // only for exactly one market. Multi-market support remains blocked on a server display label.
  if (markets.length !== 1 || markets[0]?.outcome !== outcome) return fail();
  const pendingMarkets = new Map(markets
    .filter((entry) => entry.frequency.state === 'pending')
    .map((entry) => [entry.marketplace_key, entry.frequency.text ?? fail()]));
  const evidence = record(item.evidence, ['fence_check', 'pre_exposure_exclusion']);
  const pageContext = exactText(item.page_context_label, `Reach and funnel · ${period.label}`);
  const contextExpiry = nullableInstant(item.page_context_expires_at);
  const starter = `Which exposure group's purchaser rate was highest in ${period.label}, and what is it associated with?`;
  const starters = exactTexts(item.starter_questions, [starter]);
  return {
    boundary: exactTexts(item.boundary, [
      'Figures are for complete weeks, months or quarters only.',
      'Rates read Amazon account IDs only.',
      'This shows association, not cause.',
    ]),
    evidence: {
      fence_check: exactText(evidence.fence_check, 'structural check, not the exact pack text'),
      pre_exposure_exclusion: exactText(evidence.pre_exposure_exclusion, 'pending_step_b'),
    },
    evidence_status: evidenceStatus(item.evidence_status, period, pendingMarkets),
    markets,
    outcome,
    page_context_expires_at: contextExpiry,
    page_context_label: pageContext,
    period,
    preset,
    starter_questions: starters,
  };
}
