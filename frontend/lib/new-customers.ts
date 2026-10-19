// SPDX-License-Identifier: MIT-0
/**
 * The strict browser decoder of `NewCustomersArtifact` v2 (New customers LLD §6; FEAT-004
 * hand-off §3, with slice 9's D3/D4 alignment to ADR-119 and ADR-121).
 *
 * Every text is a server field: the browser never maps a reason code, scales a share, or
 * computes a value, change or total. The decoder refuses what the server can't have written: a
 * v1 body, an unknown key or absence reason, `no_new_buyers` on a new-buyers figure, a
 * per-channel table beside tiles that aren't shown, rows beside a table that isn't listed, and a
 * total or subtotal row, because new-to-brand figures are never added across channels (ADR-106).
 */
import { parseChange, type Change } from './change';
import { isDisplayValue } from './display-value';
import { parseEvidenceStatus, type EvidenceStatusV2 } from './evidence-status';

export class NewCustomersDecodeError extends Error {
  constructor() {
    super('The New customers artifact is invalid.');
    this.name = 'NewCustomersDecodeError';
  }
}

const fail = (): never => {
  throw new NewCustomersDecodeError();
};

type Item = Record<string, unknown>;

function record(value: unknown, keys: readonly string[]): Item {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return fail();
  const item = value as Item;
  const actual = Object.keys(item);
  if (actual.length !== keys.length || !actual.every((key) => keys.includes(key))) return fail();
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

const DECIMAL = /^-?\d+(\.\d+)?$/;
const DATE = /^\d{4}-\d{2}-\d{2}$/;

function isoDate(value: unknown): string {
  const day = text(value);
  return DATE.test(day) ? day : fail();
}

export interface DateWindow {
  readonly end: string;
  readonly start: string;
}

function window(value: unknown): DateWindow {
  const item = record(value, ['end', 'start']);
  const start = isoDate(item.start);
  const end = isoDate(item.end);
  return end < start ? fail() : { end, start };
}

function nullableWindow(value: unknown): DateWindow | null {
  return value === null ? null : window(value);
}

/** Why a figure is absent; the browser renders the paired server text, never the code. */
export type AbsenceReason =
  | 'calendar_period_selected'
  | 'no_ad_spend'
  | 'no_attributed_revenue'
  | 'no_new_buyers'
  | 'no_ntb_purchases'
  | 'no_share_to_compare'
  | 'not_returned';

export interface Absence {
  readonly reason: AbsenceReason;
  readonly text: string;
}

function absence(value: unknown, allowed: readonly AbsenceReason[]): Absence | null {
  if (value === null) return null;
  const item = record(value, ['reason', 'text']);
  const reason = allowed.find((name) => name === item.reason) ?? fail();
  return { reason, text: text(item.text) };
}

/** One figure: an exact value with its units and server display, or a typed absence. */
export interface NewCustomersFigure {
  readonly absence: Absence | null;
  readonly currency_code: string | null;
  /** Currency at two places (ADR-084); null for a count or a rate. */
  readonly display_value: string | null;
  /** A rate's percentage text ("41.00%"), so the browser never scales a share. */
  readonly display_text: string | null;
  readonly kind: 'count' | 'currency' | 'ratio' | null;
  readonly metric: string;
  readonly value: string | null;
}

function figure(value: unknown, metric: string, allowed: readonly AbsenceReason[], currency: string): NewCustomersFigure {
  const item = record(value, ['absence', 'display_text', 'display_value', 'metric', 'units', 'value']);
  if (item.metric !== metric) return fail();
  const missing = absence(item.absence, allowed);
  const current = item.value === null ? null : typeof item.value === 'string' && DECIMAL.test(item.value) ? item.value : fail();
  // A value or a typed absence, never both and never neither.
  if ((current === null) === (missing === null)) return fail();
  if (current === null) {
    if (item.units !== null || item.display_value !== null || item.display_text !== null) return fail();
    return { absence: missing, currency_code: null, display_text: null, display_value: null, kind: null, metric, value: null };
  }
  const units = record(item.units, ['currency_code', 'kind']);
  const kind = (['count', 'currency', 'ratio'] as const).find((name) => name === units.kind) ?? fail();
  const code = nullableText(units.currency_code);
  if ((kind === 'currency') !== (code !== null)) return fail();
  // A market's figures are in its one currency, never converted.
  if (code !== null && code !== currency) return fail();
  if (!isDisplayValue(item.display_value, kind)) return fail();
  const share = nullableText(item.display_text);
  if ((kind === 'ratio') !== (share !== null) || (share !== null && !/^-?\d+\.\d{2}%$/.test(share))) return fail();
  return { absence: null, currency_code: code, display_text: share, display_value: item.display_value as string | null, kind, metric, value: current };
}

export interface ShareChange {
  readonly absence: Absence | null;
  readonly change: Change | null;
}

export interface StepOneRow {
  readonly ad_product_key: string;
  readonly ad_product_label: string;
  readonly channel_group_label: string;
  readonly cost_per_ntb_purchase: NewCustomersFigure;
  readonly ntb_purchases: NewCustomersFigure;
  readonly ntb_revenue: NewCustomersFigure;
  readonly ntb_revenue_share: NewCustomersFigure;
  readonly ntb_revenue_share_change: ShareChange;
}

export type StepOneColumn = 'cost_per_ntb_purchase' | 'ntb_purchases' | 'ntb_revenue' | 'ntb_revenue_share' | 'ntb_revenue_share_change';

export interface StepOne {
  readonly caption: string;
  readonly caption_detail: string;
  readonly columns: readonly { readonly column: StepOneColumn; readonly label: string }[];
  readonly foot: string;
  readonly heading: string;
  readonly rows: readonly StepOneRow[];
  readonly share_basis: string;
}

const STEP_ONE_COLUMNS: readonly StepOneColumn[] = [
  'ntb_purchases', 'ntb_revenue', 'ntb_revenue_share', 'ntb_revenue_share_change', 'cost_per_ntb_purchase',
];

function stepOneRow(value: unknown, currency: string, rolling: boolean): StepOneRow {
  const item = record(value, [
    'ad_product_key', 'ad_product_label', 'channel_group_label', 'cost_per_ntb_purchase', 'ntb_purchases', 'ntb_revenue',
    'ntb_revenue_share', 'ntb_revenue_share_change',
  ]);
  const purchases = figure(item.ntb_purchases, 'purchases_new_to_brand_12mo', [], currency);
  const revenue = figure(item.ntb_revenue, 'revenue_new_to_brand_12mo', [], currency);
  const share = figure(item.ntb_revenue_share, 'ntb_revenue_share', ['no_attributed_revenue'], currency);
  const cost = figure(item.cost_per_ntb_purchase, 'cost_per_ntb', ['no_ntb_purchases'], currency);
  if (purchases.kind !== 'count' || revenue.kind !== 'currency' || (share.value !== null && share.kind !== 'ratio')) return fail();
  if (cost.value !== null && cost.kind !== 'currency') return fail();
  const changeItem = record(item.ntb_revenue_share_change, ['absence', 'change']);
  const changeAbsence = absence(changeItem.absence, ['calendar_period_selected', 'no_share_to_compare']);
  const change = changeItem.change === null ? null : parseChange(changeItem.change) ?? fail();
  if ((change === null) === (changeAbsence === null)) return fail();
  // The change in share is on rolling presets only, in points; a calendar preset has no column.
  if (rolling === (changeAbsence?.reason === 'calendar_period_selected')) return fail();
  if (change !== null && (change.unit !== 'percentage_points' || share.value === null)) return fail();
  return {
    ad_product_key: text(item.ad_product_key),
    ad_product_label: text(item.ad_product_label),
    channel_group_label: text(item.channel_group_label),
    cost_per_ntb_purchase: cost,
    ntb_purchases: purchases,
    ntb_revenue: revenue,
    ntb_revenue_share: share,
    ntb_revenue_share_change: { absence: changeAbsence, change },
  };
}

export interface PerChannelRow {
  readonly ad_product_key: string;
  readonly ad_product_label: string;
  readonly channel_group_label: string;
  readonly cost_per_new_buyer: NewCustomersFigure;
  readonly new_buyers: NewCustomersFigure;
}

export interface PerChannel {
  readonly caption: string | null;
  readonly caption_detail: string | null;
  readonly cost_basis: string | null;
  readonly cost_header: string | null;
  readonly expected_after: string | null;
  readonly foot: string | null;
  readonly latest_available_period: DateWindow | null;
  readonly note: string | null;
  readonly rows: readonly PerChannelRow[];
  readonly state: 'missing' | 'no_channels' | 'pending' | 'rows';
  /** The line that replaces the table: no channels, pending or missing. */
  readonly text: string | null;
}

function perChannel(value: unknown, currency: string, stepOneKeys: ReadonlySet<string>): PerChannel {
  const item = record(value, [
    'caption', 'caption_detail', 'cost_basis', 'cost_header', 'expected_after', 'foot', 'latest_available_period', 'note', 'rows',
    'state', 'text',
  ]);
  const state = (['missing', 'no_channels', 'pending', 'rows'] as const).find((name) => name === item.state) ?? fail();
  const listed = state === 'rows';
  const line = nullableText(item.text);
  const rows = list(item.rows).map((entry): PerChannelRow => {
    const row = record(entry, ['ad_product_key', 'ad_product_label', 'channel_group_label', 'cost_per_new_buyer', 'new_buyers']);
    // New buyers come back or not: never `no_new_buyers`, which only explains a cost (FEAT-004 round 3).
    const buyers = figure(row.new_buyers, 'buyers_new_to_brand_12mo', ['not_returned'], currency);
    const cost = figure(row.cost_per_new_buyer, 'cost_per_new_to_brand_buyer_12mo', ['no_ad_spend', 'no_new_buyers', 'not_returned'], currency);
    if ((buyers.value !== null && buyers.kind !== 'count') || (cost.value !== null && cost.kind !== 'currency')) return fail();
    // A row that didn't come back reads it in both cells; 0 new buyers read no_new_buyers on the cost.
    if ((buyers.absence !== null) !== (cost.absence?.reason === 'not_returned')) return fail();
    if ((cost.absence?.reason === 'no_new_buyers') !== (buyers.value !== null && Number(buyers.value) === 0 && cost.absence?.reason !== 'no_ad_spend')) return fail();
    const key = text(row.ad_product_key);
    // A row is one ad product: on the period's roster it delivered, so step 1 lists it; off the
    // roster it had no ad spend and says so. Anything else (a total or subtotal row) is refused.
    if (stepOneKeys.has(key) === (cost.absence?.reason === 'no_ad_spend')) return fail();
    return {
      ad_product_key: key,
      ad_product_label: text(row.ad_product_label),
      channel_group_label: text(row.channel_group_label),
      cost_per_new_buyer: cost,
      new_buyers: buyers,
    };
  });
  if (listed !== (rows.length > 0) || listed === (line !== null)) return fail();
  if (new Set(rows.map((row) => row.ad_product_key)).size !== rows.length) return fail();
  const caption = nullableText(item.caption);
  const detail = nullableText(item.caption_detail);
  const header = nullableText(item.cost_header);
  const basis = nullableText(item.cost_basis);
  const foot = nullableText(item.foot);
  if ([caption, detail, header, basis, foot].some((part) => (part !== null) !== listed)) return fail();
  const note = nullableText(item.note);
  if (note !== null && !listed) return fail();
  const awaiting = state === 'pending' || state === 'missing';
  const expected = item.expected_after === null ? null : isoDate(item.expected_after);
  if ((expected !== null) !== awaiting) return fail();
  const latest = nullableWindow(item.latest_available_period);
  if (latest !== null && !awaiting) return fail();
  return {
    caption, caption_detail: detail, cost_basis: basis, cost_header: header, expected_after: expected, foot,
    latest_available_period: latest, note, rows, state, text: line,
  };
}

export interface NewBuyers {
  readonly action: { readonly label: string; readonly preset: 'last_complete_week' } | null;
  readonly cost_basis: string | null;
  readonly cost_per_new_buyer: NewCustomersFigure | null;
  readonly expected_after: string | null;
  readonly latest_available_period: DateWindow | null;
  readonly new_buyers: NewCustomersFigure | null;
  readonly new_buyers_basis: string | null;
  readonly per_channel: PerChannel | null;
  readonly period: DateWindow | null;
  readonly period_label: string | null;
  readonly state: 'figures' | 'missing' | 'outside_declared_periods' | 'pending';
  readonly text: string | null;
}

function newBuyers(value: unknown, currency: string, rolling: boolean, stepOneKeys: ReadonlySet<string>): NewBuyers {
  const item = record(value, [
    'action', 'cost_basis', 'cost_per_new_buyer', 'expected_after', 'latest_available_period', 'new_buyers', 'new_buyers_basis',
    'per_channel', 'period', 'period_label', 'state', 'text',
  ]);
  const state = (['figures', 'missing', 'outside_declared_periods', 'pending'] as const).find((name) => name === item.state) ?? fail();
  // New buyers exist only for complete weeks and months (ADR-100): a rolling preset has the callout.
  if (rolling !== (state === 'outside_declared_periods')) return fail();
  const figures = state === 'figures';
  const line = nullableText(item.text);
  if (figures === (line !== null)) return fail();
  const action = item.action === null ? null : (() => {
    const served = record(item.action, ['label', 'preset']);
    return served.preset === 'last_complete_week' ? { label: text(served.label), preset: 'last_complete_week' as const } : fail();
  })();
  if ((action !== null) !== (state === 'outside_declared_periods')) return fail();
  const tile = figures ? figure(item.new_buyers, 'buyers_new_to_brand_12mo', ['not_returned'], currency) : null;
  const cost = figures ? figure(item.cost_per_new_buyer, 'cost_per_new_to_brand_buyer_12mo', ['no_new_buyers', 'not_returned'], currency) : null;
  if (!figures && (item.new_buyers !== null || item.cost_per_new_buyer !== null)) return fail();
  if (tile !== null && cost !== null) {
    if ((tile.value !== null && tile.kind !== 'count') || (cost.value !== null && cost.kind !== 'currency')) return fail();
    // The cost is absent exactly when the buyers are absent (not_returned) or came back 0 (no_new_buyers).
    if ((tile.absence !== null) !== (cost.absence?.reason === 'not_returned')) return fail();
    if ((cost.absence?.reason === 'no_new_buyers') !== (tile.value !== null && Number(tile.value) === 0)) return fail();
  }
  // The per-channel table nests under the tiles, never without them (LLD §10 proposal, plan D15).
  const table = item.per_channel === null ? null : perChannel(item.per_channel, currency, stepOneKeys);
  if ((table !== null) !== figures) return fail();
  const period = nullableWindow(item.period);
  const label = nullableText(item.period_label);
  const awaiting = state === 'pending' || state === 'missing';
  if ((period !== null) !== (figures || awaiting) || (label !== null) !== (period !== null)) return fail();
  const expected = item.expected_after === null ? null : isoDate(item.expected_after);
  if ((expected !== null) !== awaiting) return fail();
  const latest = nullableWindow(item.latest_available_period);
  if (latest !== null && !awaiting) return fail();
  const buyersBasis = nullableText(item.new_buyers_basis);
  const costBasis = nullableText(item.cost_basis);
  if ((buyersBasis !== null) !== figures || (costBasis !== null) !== figures) return fail();
  return {
    action, cost_basis: costBasis, cost_per_new_buyer: cost, expected_after: expected, latest_available_period: latest,
    new_buyers: tile, new_buyers_basis: buyersBasis, per_channel: table, period, period_label: label, state, text: line,
  };
}

export interface GatewayRow {
  readonly asin: string;
  readonly ntb_purchases: number;
  readonly share_text: string;
  readonly title: string;
}

export interface GatewayEntry {
  readonly ad_product_key: string;
  readonly caption_title: string;
  readonly captions: readonly string[];
  readonly channel_label: string;
  readonly column_header: string;
  readonly first_covered_date: string | null;
  readonly more_text: string | null;
  readonly rows: readonly GatewayRow[];
  readonly state: 'no_ntb_purchases' | 'not_covered' | 'rows';
  readonly text: string | null;
}

function gateway(value: unknown): GatewayEntry {
  const item = record(value, [
    'ad_product_key', 'caption_title', 'captions', 'channel_label', 'column_header', 'first_covered_date', 'more_count', 'more_text',
    'rows', 'state', 'text',
  ]);
  const state = (['no_ntb_purchases', 'not_covered', 'rows'] as const).find((name) => name === item.state) ?? fail();
  const rows = list(item.rows).map((entry): GatewayRow => {
    const row = record(entry, ['asin', 'ntb_purchases', 'share_of_channel', 'share_text', 'title']);
    const count = row.ntb_purchases;
    if (typeof count !== 'number' || !Number.isSafeInteger(count) || count < 1) return fail();
    if (typeof row.share_of_channel !== 'string' || !DECIMAL.test(row.share_of_channel)) return fail();
    const share = text(row.share_text);
    if (!/^\d+\.\d{2}%$/.test(share)) return fail();
    return { asin: text(row.asin), ntb_purchases: count, share_text: share, title: text(row.title) };
  });
  const listed = state === 'rows';
  const line = nullableText(item.text);
  if (listed !== (rows.length > 0) || listed === (line !== null) || rows.length > 10) return fail();
  const more = item.more_count === null ? null : item.more_count;
  const moreText = nullableText(item.more_text);
  if ((more === null) !== (moreText === null) || (more !== null && (typeof more !== 'number' || !Number.isSafeInteger(more) || more < 1 || rows.length !== 10))) return fail();
  const first = item.first_covered_date === null ? null : isoDate(item.first_covered_date);
  if (first !== null && state !== 'not_covered') return fail();
  // The two served captions (LLD N19): the halo scope, then the per-channel credit rule.
  const captions = list(item.captions).map(text);
  if (captions.length !== 2) return fail();
  return {
    ad_product_key: text(item.ad_product_key),
    caption_title: text(item.caption_title),
    captions,
    channel_label: text(item.channel_label),
    column_header: text(item.column_header),
    first_covered_date: first,
    more_text: moreText,
    rows,
    state,
    text: line,
  };
}

export interface NewCustomersMarket {
  readonly currency_code: string;
  readonly gateway: readonly GatewayEntry[];
  readonly marketplace: string;
  /** Which marketplace and currency the market's sections measure, served only with two shown. */
  readonly marketplace_text: string | null;
  readonly new_buyers: NewBuyers;
  readonly new_to_brand: StepOne;
}

function market(value: unknown, rolling: boolean): NewCustomersMarket {
  const item = record(value, ['currency_code', 'gateway', 'marketplace', 'marketplace_text', 'new_buyers', 'new_to_brand']);
  const currency = text(item.currency_code);
  const table = record(item.new_to_brand, ['caption', 'caption_detail', 'columns', 'foot', 'heading', 'rows', 'share_basis']);
  const rows = list(table.rows).map((entry) => stepOneRow(entry, currency, rolling));
  const keys = rows.map((row) => row.ad_product_key);
  if (rows.length === 0 || new Set(keys).size !== keys.length) return fail();
  const columns = list(table.columns).map((entry) => {
    const column = record(entry, ['column', 'label']);
    return { column: STEP_ONE_COLUMNS.find((name) => name === column.column) ?? fail(), label: text(column.label) };
  });
  if (columns.map((column) => column.column).join() !== STEP_ONE_COLUMNS.join()) return fail();
  const entries = list(item.gateway).map(gateway);
  // One gateway entry per step-1 ad product, in the same order: a step-1 total or subtotal row
  // has no channel of its own, so it can't be added without breaking this (ADR-106).
  if (entries.map((entry) => entry.ad_product_key).join('\u0000') !== keys.join('\u0000')) return fail();
  const states = new Set(entries.map((entry) => entry.state));
  if (states.has('not_covered') && states.size !== 1) return fail();
  return {
    currency_code: currency,
    gateway: entries,
    marketplace: text(item.marketplace),
    marketplace_text: nullableText(item.marketplace_text),
    new_buyers: newBuyers(item.new_buyers, currency, rolling, new Set(keys)),
    new_to_brand: {
      caption: text(table.caption),
      caption_detail: text(table.caption_detail),
      columns,
      foot: text(table.foot),
      heading: text(table.heading),
      rows,
      share_basis: text(table.share_basis),
    },
  };
}

export interface NewCustomersInsightSlot {
  readonly absence_reason: string | null;
  readonly artifact_revision: string;
  readonly evidence_line: string | null;
  readonly prompt_version: string;
  readonly state: 'filled' | 'silent' | 'unavailable';
  readonly text: string | null;
}

function slot(value: unknown): NewCustomersInsightSlot {
  const withAbsence = typeof value === 'object' && value !== null && 'absence_class' in value;
  const item = record(value, withAbsence
    ? ['absence_class', 'absence_reason', 'artifact_revision', 'evidence_line', 'id', 'output_class', 'prompt_version', 'state', 'text']
    : ['artifact_revision', 'evidence_line', 'id', 'output_class', 'prompt_version', 'state', 'text']);
  if (item.id !== 'portfolio-acquisition' || item.output_class !== 'perspective' || item.prompt_version !== 'deterministic-v1') return fail();
  const state = (['filled', 'silent', 'unavailable'] as const).find((name) => name === item.state) ?? fail();
  const filled = state === 'filled';
  if (filled !== (typeof item.text === 'string')) return fail();
  const reason = withAbsence ? nullableText(item.absence_reason) : null;
  // A slot that isn't filled says why in the server's words; a filled one carries no absence.
  if (filled !== (reason === null)) return fail();
  const line = nullableText(item.evidence_line);
  if (filled !== (line !== null)) return fail();
  return {
    absence_reason: reason,
    artifact_revision: text(item.artifact_revision),
    evidence_line: line,
    prompt_version: text(item.prompt_version),
    state,
    text: nullableText(item.text),
  };
}

export interface NewCustomersInsightCard {
  readonly ad_product_key: string;
  readonly body: string;
  readonly finding_id: 'ntb-share-mover';
  readonly headline: string;
  readonly kind_label: string;
  readonly marketplace: string;
  /** Which marketplace the card narrates, served only when the page shows two. */
  readonly marketplace_text: string | null;
  /** The card's served name (its control's and dialog's): the headline, plus its marketplace. */
  readonly title: string;
}

export interface NewCustomersPointer {
  readonly href: string;
  /** The served words before the link; the line reads "{lead} {link_text}." */
  readonly lead: string;
  readonly link_text: string;
  readonly order: number;
  readonly tab_id: string;
  readonly text: string;
}

export interface NewCustomersPageText {
  readonly boundary_heading: string;
  readonly channel_header: string;
  readonly cost_per_new_buyer_label: string;
  readonly gateway_heading: string;
  readonly gateway_product_header: string;
  readonly gateway_selector_label: string;
  readonly gateway_share_header: string;
  readonly insights_heading: string;
  readonly new_buyers_heading: string;
  readonly new_buyers_label: string;
  /** The server's word for an absent figure, shown above its reason. */
  readonly not_available_text: string;
}

const PAGE_TEXT_KEYS = [
  'boundary_heading', 'channel_header', 'cost_per_new_buyer_label', 'gateway_heading', 'gateway_product_header', 'gateway_selector_label',
  'gateway_share_header', 'insights_heading', 'new_buyers_heading', 'new_buyers_label', 'not_available_text',
] as const;

export interface NewCustomersArtifact {
  readonly boundary: readonly string[];
  readonly evidence_status: EvidenceStatusV2 | null;
  readonly insight_cards: readonly NewCustomersInsightCard[];
  readonly insights: NewCustomersInsightSlot;
  readonly markets: readonly NewCustomersMarket[];
  readonly outcome: 'complete' | 'empty' | 'insufficient';
  readonly outcome_text: string | null;
  readonly page_context_expires_at: string | null;
  readonly page_context_label: string;
  readonly page_text: NewCustomersPageText;
  readonly period: DateWindow;
  readonly period_label: string;
  readonly pointers: readonly NewCustomersPointer[];
  readonly preset: string;
  readonly prior_period: DateWindow | null;
  readonly scope_text: string;
  readonly starter_questions: readonly string[];
}

const ARTIFACT_KEYS = [
  'boundary', 'evidence_status', 'insight_cards', 'insights', 'markets', 'outcome', 'outcome_text', 'page_context_expires_at',
  'page_context_label', 'page_text', 'period', 'period_label', 'pointers', 'preset', 'prior_period', 'scope_text', 'starter_questions',
  'type', 'version',
] as const;

/** Decode one served body; any drift from the server contract is refused, never repaired. */
export function parseNewCustomersArtifact(value: unknown): NewCustomersArtifact {
  const item = record(value, ARTIFACT_KEYS);
  if (item.type !== 'NewCustomersArtifact' || item.version !== 2) return fail();
  const outcome = (['complete', 'empty', 'insufficient'] as const).find((name) => name === item.outcome) ?? fail();
  const complete = outcome === 'complete';
  const outcomeText = nullableText(item.outcome_text);
  if (complete !== (outcomeText === null)) return fail();
  const period = window(item.period);
  const prior = nullableWindow(item.prior_period);
  if (prior !== null && prior.end >= period.start) return fail();
  const rolling = prior !== null;
  const markets = list(item.markets).map((entry) => market(entry, rolling));
  if (complete !== (markets.length > 0)) return fail();
  if (new Set(markets.map((entry) => entry.marketplace)).size !== markets.length) return fail();
  // With two marketplaces each names its own; with one none does, as the approved mockups read.
  if (markets.some((entry) => (entry.marketplace_text !== null) !== (markets.length > 1))) return fail();
  const insights = slot(list(item.insights).length === 1 ? (item.insights as unknown[])[0] : fail());
  const byMarket = new Map(markets.map((entry) => [entry.marketplace, entry]));
  const cards = list(item.insight_cards).map((entry): NewCustomersInsightCard => {
    const card = record(entry, ['ad_product_key', 'body', 'finding_id', 'headline', 'kind_label', 'marketplace', 'marketplace_text', 'title']);
    if (card.finding_id !== 'ntb-share-mover') return fail();
    const owner = byMarket.get(text(card.marketplace)) ?? fail();
    const product = text(card.ad_product_key);
    // A card is inside one ad product of one marketplace, and names that marketplace as its market does.
    if (!owner.new_to_brand.rows.some((row) => row.ad_product_key === product)) return fail();
    const marketplace = nullableText(card.marketplace_text);
    if (marketplace !== owner.marketplace_text) return fail();
    return {
      ad_product_key: product, body: text(card.body), finding_id: 'ntb-share-mover', headline: text(card.headline), kind_label: text(card.kind_label),
      marketplace: owner.marketplace, marketplace_text: marketplace, title: text(card.title),
    };
  });
  // The cards are the filled slot's narration: at most three, each named apart.
  if ((insights.state === 'filled') !== (cards.length > 0) || cards.length > 3) return fail();
  if (new Set(cards.map((card) => card.title)).size !== cards.length) return fail();
  if (insights.state === 'filled' && insights.text !== cards.map((card) => `${card.headline}. ${card.body}`).join(' ')) return fail();
  const pointers = list(item.pointers).map((entry): NewCustomersPointer => {
    const pointer = record(entry, ['href', 'lead', 'link_text', 'order', 'tab_id', 'text']);
    const order = pointer.order;
    if (typeof order !== 'number' || !Number.isSafeInteger(order)) return fail();
    const lead = text(pointer.lead);
    const linkText = text(pointer.link_text);
    const line = text(pointer.text);
    // The lead and the link make the served sentence, word for word.
    if (line !== `${lead} ${linkText}.`) return fail();
    return { href: text(pointer.href), lead, link_text: linkText, order, tab_id: text(pointer.tab_id), text: line };
  });
  const pageText = record(item.page_text, PAGE_TEXT_KEYS);
  const evidence = parseEvidenceStatus(item.evidence_status, period);
  // ADR-119: a complete body carries its coverage block; an empty or insufficient one none.
  if (complete !== (evidence !== null)) return fail();
  const starters = list(item.starter_questions).map(text);
  return {
    boundary: list(item.boundary).map(text),
    evidence_status: evidence,
    insight_cards: cards,
    insights,
    markets,
    outcome,
    outcome_text: outcomeText,
    page_context_expires_at: nullableText(item.page_context_expires_at),
    page_context_label: text(item.page_context_label),
    page_text: Object.fromEntries(PAGE_TEXT_KEYS.map((key) => [key, text(pageText[key])])) as unknown as NewCustomersPageText,
    period,
    period_label: text(item.period_label),
    pointers,
    preset: text(item.preset),
    prior_period: prior,
    scope_text: text(item.scope_text),
    starter_questions: starters,
  };
}
