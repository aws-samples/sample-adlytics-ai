// SPDX-License-Identifier: MIT-0
/**
 * The strict browser decoder of `NetworkScorecardArtifact` v1 (Scorecard LLD §6, §9).
 *
 * Every text is a server field and every figure carries its basis label. The decoder refuses
 * what the server can't have written: an unknown key, a figure without its basis, a currency
 * figure in another currency than its card, a ranking row whose spend differs from its channel
 * row, and any attributed total or channel-group subtotal row, because attributed returns are
 * never added across channels (ia §8.5).
 */
import { parseChange, type Change } from './change';
import { isDisplayValue } from './display-value';
import { parseEvidenceStatus, type EvidenceStatusV2 } from './evidence-status';
import { parseNextStep, type InsightNextStep } from './insight-next-step';

export class ScorecardDecodeError extends Error {
  constructor() {
    super('The Scorecard artifact is invalid.');
    this.name = 'ScorecardDecodeError';
  }
}

const fail = (): never => {
  throw new ScorecardDecodeError();
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
function decimal(value: unknown): string {
  return typeof value === 'string' && DECIMAL.test(value) ? value : fail();
}

function nullableDecimal(value: unknown): string | null {
  return value === null ? null : decimal(value);
}

function scaled(value: string, places: number): bigint {
  const negative = value.startsWith('-');
  const [whole = '0', fraction = ''] = (negative ? value.slice(1) : value).split('.');
  const digits = BigInt(`${whole}${fraction.padEnd(places, '0')}`);
  return negative ? -digits : digits;
}

function places(value: string): number {
  return value.split('.')[1]?.length ?? 0;
}

/** Exact decimal sum of served decimal strings, for the decoder's integrity checks only. */
function exactSum(values: readonly string[]): string {
  const scale = Math.max(0, ...values.map(places));
  const total = values.reduce((sum, value) => sum + scaled(value, scale), 0n);
  return scale === 0 ? total.toString() : `${total < 0n ? '-' : ''}${(total < 0n ? -total : total).toString().padStart(scale + 1, '0').replace(new RegExp(`(\\d{${scale}})$`), '.$1')}`;
}

function sameDecimal(left: string, right: string): boolean {
  const scale = Math.max(places(left), places(right));
  return scaled(left, scale) === scaled(right, scale);
}

const DATE = /^\d{4}-\d{2}-\d{2}$/;
function window(value: unknown): { readonly end: string; readonly start: string } {
  const item = record(value, ['end', 'start']);
  const start = text(item.start);
  const end = text(item.end);
  if (!DATE.test(start) || !DATE.test(end) || end < start) return fail();
  return { end, start };
}

export type ScorecardFormat = 'currency' | 'multiplier' | 'percent';

/** One served figure: exact value, server display value, prior, change, basis and absence. */
export interface ScorecardFigure {
  readonly absence_text: string | null;
  readonly basis: string;
  readonly change: Change | null;
  readonly currency_code: string | null;
  readonly display_value: string | null;
  readonly format: ScorecardFormat | null;
  readonly label: string;
  readonly metric: string;
  readonly prior_display_value: string | null;
  readonly prior_label: string | null;
  readonly prior_value: string | null;
  readonly value: string | null;
}

const FIGURE_KEYS = [
  'absence_text', 'basis', 'change', 'display_value', 'format', 'label', 'metric', 'prior_display_value',
  'prior_label', 'prior_value', 'units', 'value',
] as const;

/** A served two-place display value: the shape ADR-084 gives money, here also a multiplier's. */
function isTwoPlace(value: unknown): value is string {
  return isDisplayValue(value, 'currency') && value !== null;
}

function figure(value: unknown, currency?: string): ScorecardFigure {
  const item = record(value, FIGURE_KEYS);
  const metric = text(item.metric);
  const basis = text(item.basis);
  const absence = nullableText(item.absence_text);
  const current = nullableDecimal(item.value);
  // A figure carries a value or its typed absence, never both and never neither.
  if ((current === null) === (absence === null)) return fail();
  const prior = nullableDecimal(item.prior_value);
  if (current === null && prior !== null) return fail();
  let unitKind: string | null = null;
  let currencyCode: string | null = null;
  if (item.units === null) {
    if (current !== null) return fail();
  } else {
    const units = record(item.units, ['currency_code', 'kind']);
    unitKind = text(units.kind);
    currencyCode = nullableText(units.currency_code);
    if ((unitKind === 'currency') !== (currencyCode !== null)) return fail();
    // A card is never converted across currencies.
    if (currency !== undefined && currencyCode !== null && currencyCode !== currency) return fail();
  }
  const format = item.format === null ? null : (['currency', 'multiplier', 'percent'] as const).find((name) => name === item.format) ?? fail();
  if ((format === null) !== (unitKind === null) || (format === 'currency') !== (unitKind === 'currency')) return fail();
  // Each served side carries the server's two-place display value for money (ADR-084) and for a
  // multiplier (plan S26, G9A-4); a percent and an absent side carry none. The browser never rounds.
  const shown = (side: string | null, display: unknown) => side === null || format === 'percent'
    ? display === null
    : isTwoPlace(display);
  if (!shown(current, item.display_value) || !shown(prior, item.prior_display_value)) return fail();
  const change = parseChange(item.change);
  if (change === undefined || (change !== null && prior === null)) return fail();
  const priorLabel = nullableText(item.prior_label);
  if ((priorLabel === null) !== (prior === null)) return fail();
  return {
    absence_text: absence,
    basis,
    change,
    currency_code: currencyCode,
    display_value: item.display_value as string | null,
    format,
    label: text(item.label),
    metric,
    prior_display_value: item.prior_display_value as string | null,
    prior_label: priorLabel,
    prior_value: prior,
    value: current,
  };
}

const ATTRIBUTED_METRICS = new Set([
  'attributed_revenue_14d', 'roas_attributed_14d', 'roas_attributed_incl_halo_14d',
]);

export interface ScorecardLink {
  readonly href: string;
  readonly label: string;
  readonly text: string;
}

function link(value: unknown, route: string): ScorecardLink | null {
  if (value === null) return null;
  const item = record(value, ['href', 'label', 'text']);
  const href = text(item.href);
  if (href !== route && !href.startsWith(`${route}?`)) return fail();
  return { href, label: text(item.label), text: text(item.text) };
}

export interface ScorecardModel {
  readonly figure: ScorecardFigure;
  readonly label: string;
  readonly model: string;
  readonly option_label: string;
}

export interface ScorecardRankingRow {
  readonly channel: string;
  readonly channel_label: string;
  readonly spend_display_value: string;
  readonly values: readonly {
    /** The server's two-place multiplier, the only form a ranking value renders in. */
    readonly display_value: string | null;
    readonly model: string;
    readonly rank: number | null;
    readonly rank_text: string | null;
    readonly value: string | null;
  }[];
}

export interface ScorecardChannelRow {
  readonly channel: string;
  readonly channel_label: string;
  readonly figures: readonly [ScorecardFigure, ScorecardFigure, ScorecardFigure, ScorecardFigure];
  readonly link: ScorecardLink | null;
}

export interface ScorecardCard {
  readonly card_id: string;
  readonly channel_table: {
    readonly absent_lines: readonly string[];
    readonly caption: string;
    readonly caption_detail: string;
    readonly columns: readonly { readonly basis: string | null; readonly column: string; readonly label: string }[];
    readonly foot: string;
    readonly fresh_text: string;
    readonly heading: string;
    readonly highlight_tag: string;
    readonly link_column: string;
    /** Which marketplace the table measures, served only when the page shows two; the arrival tag names it. */
    readonly marketplace_text: string | null;
    readonly row_header_label: string;
    readonly rows: readonly ScorecardChannelRow[];
  };
  readonly currency_code: string;
  readonly dsp_absence_text: string | null;
  readonly headline: {
    readonly absence_text: string | null;
    readonly default_model: string;
    readonly label: string;
    readonly model_note: string;
    readonly models: readonly ScorecardModel[];
    readonly selector_label: string;
  };
  readonly networks_text: string | null;
  readonly ranking: {
    readonly by_day_label: string;
    readonly caption: string;
    readonly caption_detail: string;
    readonly foot: string;
    readonly heading: string;
    readonly models: readonly { readonly label: string; readonly model: string }[];
    readonly outcome: 'insufficient' | 'model_sensitive' | 'robust_leader';
    readonly outcome_text: string;
    readonly row_header_label: string;
    readonly rows: readonly ScorecardRankingRow[];
  };
  readonly spend: ScorecardFigure;
  readonly title: string;
}

const CHANNEL_COLUMNS = ['ad_spend', 'attributed_revenue_14d', 'roas_attributed_14d', 'roas_attributed_incl_halo_14d'] as const;

function card(value: unknown): ScorecardCard {
  const item = record(value, [
    'card_id', 'channel_table', 'currency_code', 'dsp_absence_text', 'headline', 'networks_text', 'ranking', 'spend', 'title',
  ]);
  const currency = text(item.currency_code);
  const headlineItem = record(item.headline, ['absence_text', 'default_model', 'label', 'model_note', 'models', 'selector_label']);
  const models = list(headlineItem.models).map((entry): ScorecardModel => {
    const model = record(entry, ['figure', 'label', 'model', 'option_label']);
    const parsed = figure(model.figure, currency);
    if (parsed.metric !== 'roas_deduplicated_incl_halo_14d') return fail();
    return { figure: parsed, label: text(model.label), model: text(model.model), option_label: text(model.option_label) };
  });
  const defaultModel = text(headlineItem.default_model);
  if (models.length === 0 || !models.some((model) => model.model === defaultModel)) return fail();
  if (new Set(models.map((model) => model.model)).size !== models.length) return fail();
  const spend = figure(item.spend, currency);
  if (spend.metric !== 'ad_spend' || spend.format !== 'currency') return fail();
  const tableItem = record(item.channel_table, [
    'absent_lines', 'caption', 'caption_detail', 'columns', 'foot', 'fresh_text', 'heading', 'highlight_tag', 'link_column',
    'marketplace_text', 'row_header_label', 'rows',
  ]);
  const columns = list(tableItem.columns).map((entry) => {
    const column = record(entry, ['basis', 'column', 'label']);
    return { basis: nullableText(column.basis), column: text(column.column), label: text(column.label) };
  });
  if (columns.map((column) => column.column).join() !== CHANNEL_COLUMNS.join()) return fail();
  // Every return column names its basis in the header (ia §8.5); spend needs none.
  if (columns.slice(1).some((column) => column.basis === null)) return fail();
  const rows = list(tableItem.rows).map((entry): ScorecardChannelRow => {
    const row = record(entry, ['channel', 'channel_label', 'figures', 'link']);
    const figures = list(row.figures).map((raw) => figure(raw, currency));
    if (figures.length !== 4 || figures.map((parsed) => parsed.metric).join() !== CHANNEL_COLUMNS.join()) return fail();
    return {
      channel: text(row.channel),
      channel_label: text(row.channel_label),
      figures: figures as unknown as ScorecardChannelRow['figures'],
      link: link(row.link, '/campaigns/'),
    };
  });
  if (rows.length === 0 || new Set(rows.map((row) => row.channel)).size !== rows.length) return fail();
  // Integrity, not display: the card's spend is exactly its channel rows' spend (the server's
  // invariant), so a total or channel-group subtotal row slipped in among them is refused.
  if (spend.value === null || !sameDecimal(exactSum(rows.map((row) => row.figures[0].value ?? fail())), spend.value)) return fail();
  const rankingItem = record(item.ranking, [
    'by_day_label', 'caption', 'caption_detail', 'foot', 'heading', 'models', 'outcome', 'outcome_text', 'row_header_label', 'rows',
  ]);
  const outcome = (['insufficient', 'model_sensitive', 'robust_leader'] as const).find((name) => name === rankingItem.outcome) ?? fail();
  const rankingModels = list(rankingItem.models).map((entry) => {
    const model = record(entry, ['label', 'model']);
    return { label: text(model.label), model: text(model.model) };
  });
  if (rankingModels.map((model) => model.model).join() !== models.map((model) => model.model).join()) return fail();
  const spendByChannel = new Map(rows.map((row) => [row.channel, row.figures[0].display_value]));
  const rankingRows = list(rankingItem.rows).map((entry): ScorecardRankingRow => {
    const row = record(entry, ['channel', 'channel_label', 'spend_display_value', 'values']);
    const channel = text(row.channel);
    const spendDisplay = text(row.spend_display_value);
    // A ranking row carries its own channel row's spend, so the two tables never disagree.
    if (spendByChannel.get(channel) !== spendDisplay) return fail();
    const values = list(row.values).map((raw) => {
      const value = record(raw, ['display_value', 'model', 'rank', 'rank_text', 'value']);
      const rank = value.rank === null ? null : Number.isSafeInteger(value.rank) && (value.rank as number) > 0 ? value.rank as number : fail();
      const rankText = nullableText(value.rank_text);
      if ((rank === null) !== (rankText === null)) return fail();
      const exact = nullableDecimal(value.value);
      if (exact === null ? value.display_value !== null : !isTwoPlace(value.display_value)) return fail();
      return { display_value: exact === null ? null : value.display_value as string, model: text(value.model), rank, rank_text: rankText, value: exact };
    });
    if (values.map((value) => value.model).join() !== models.map((model) => model.model).join()) return fail();
    // A stated ranking ranks every channel on a value; an insufficient one ranks none (G9A-10),
    // so a bare "Not available" with no reason, or a rank beside no value, can't be decoded.
    if (outcome === 'insufficient'
      ? values.some((value) => value.rank !== null)
      : values.some((value) => value.rank === null || value.value === null)) return fail();
    return { channel, channel_label: text(row.channel_label), spend_display_value: spendDisplay, values };
  });
  if (rankingRows.map((row) => row.channel).join() !== rows.map((row) => row.channel).join()) return fail();
  return {
    card_id: text(item.card_id),
    channel_table: {
      absent_lines: list(tableItem.absent_lines).map(text),
      caption: text(tableItem.caption),
      caption_detail: text(tableItem.caption_detail),
      columns,
      foot: text(tableItem.foot),
      fresh_text: text(tableItem.fresh_text),
      heading: text(tableItem.heading),
      highlight_tag: text(tableItem.highlight_tag),
      link_column: text(tableItem.link_column),
      marketplace_text: nullableText(tableItem.marketplace_text),
      row_header_label: text(tableItem.row_header_label),
      rows,
    },
    currency_code: currency,
    dsp_absence_text: nullableText(item.dsp_absence_text),
    headline: {
      absence_text: nullableText(headlineItem.absence_text),
      default_model: defaultModel,
      label: text(headlineItem.label),
      model_note: text(headlineItem.model_note),
      models,
      selector_label: text(headlineItem.selector_label),
    },
    networks_text: nullableText(item.networks_text),
    ranking: {
      by_day_label: text(rankingItem.by_day_label),
      caption: text(rankingItem.caption),
      caption_detail: text(rankingItem.caption_detail),
      foot: text(rankingItem.foot),
      heading: text(rankingItem.heading),
      models: rankingModels,
      outcome,
      outcome_text: text(rankingItem.outcome_text),
      row_header_label: text(rankingItem.row_header_label),
      rows: rankingRows,
    },
    spend,
    title: text(item.title),
  };
}

export interface ScorecardInsightSlot {
  /** The served artifact stamp the dialog carries, never a browser-made one (SC9A-GATE-R5-004). */
  readonly artifact_revision: string;
  readonly evidence_line: string | null;
  /** The served prompt stamp; it picks the dialog's provenance line. */
  readonly prompt_version: string;
  readonly state: 'filled' | 'silent' | 'unavailable';
  readonly text: string | null;
  readonly absence_reason: string | null;
}

function slot(value: unknown): ScorecardInsightSlot {
  const withAbsence = typeof value === 'object' && value !== null && 'absence_class' in value;
  const item = record(value, withAbsence
    ? ['absence_class', 'absence_reason', 'artifact_revision', 'evidence_line', 'id', 'output_class', 'prompt_version', 'state', 'text']
    : ['artifact_revision', 'evidence_line', 'id', 'output_class', 'prompt_version', 'state', 'text']);
  if (item.id !== 'network-scorecard' || item.output_class !== 'observed') return fail();
  const state = (['filled', 'silent', 'unavailable'] as const).find((name) => name === item.state) ?? fail();
  const filled = state === 'filled';
  if (filled !== (typeof item.text === 'string')) return fail();
  const reason = withAbsence ? nullableText(item.absence_reason) : null;
  // A slot that isn't filled says why, in the server's words, so its absence never renders
  // blank (SC9A-GATE-R4-003); a filled slot carries no absence.
  if (filled !== (reason === null) || (reason !== null && reason.trim() === '')) return fail();
  return {
    absence_reason: reason,
    artifact_revision: text(item.artifact_revision),
    evidence_line: nullableText(item.evidence_line),
    prompt_version: text(item.prompt_version),
    state,
    text: nullableText(item.text),
  };
}

export interface ScorecardInsightCard {
  readonly body: string;
  readonly card_id: string;
  readonly channel: string;
  readonly finding_id: 'channel-roas-mover' | 'channel-share-of-network-spend';
  readonly headline: string;
  readonly kind_label: string;
  /** Which marketplace the card narrates, served only when the page shows two. */
  readonly marketplace_text: string | null;
  readonly next_step: InsightNextStep | null;
  /** The card's served name (its control's and dialog's): the headline, plus its marketplace. */
  readonly title: string;
}

export interface ScorecardAlsoChangedRow {
  readonly card_id: string;
  readonly channel: string;
  /** The row's insight dialog narration, the server's sentence. */
  readonly dialog_text: string;
  readonly direction_text: string;
  readonly finding_id: string;
  readonly label: string;
  /** Which marketplace the row measures, served only when the page shows two. */
  readonly marketplace_text: string | null;
  readonly rule_id: string;
  readonly scope_text: string;
  /** The row's served name (its control's and dialog's): the label, plus its marketplace. */
  readonly title: string;
  readonly window_text: string;
}

export interface ScorecardPointerLine {
  readonly href: string;
  readonly lead: string;
  readonly link_text: string;
  readonly tab_id: string;
}

export interface NetworkScorecardArtifact {
  readonly also_changed: { readonly heading: string; readonly intro: string; readonly rows: readonly ScorecardAlsoChangedRow[] } | null;
  readonly boundary: readonly string[];
  readonly boundary_heading: string;
  readonly cards: readonly ScorecardCard[];
  readonly cross_network_spend: readonly {
    readonly currency_code: string;
    readonly figure: ScorecardFigure | null;
    readonly label: string;
    /** Which marketplace and currency the row measures, served only when the page shows two. */
    readonly marketplace_text: string | null;
    readonly networks_loaded: readonly string[];
    readonly reason: string | null;
    /** The server's identity for the row (marketplace and currency), its render key. */
    readonly row_id: string;
    readonly text: string | null;
  }[];
  readonly evidence_status: EvidenceStatusV2 | null;
  readonly insight_cards: readonly ScorecardInsightCard[];
  readonly insights: ScorecardInsightSlot;
  readonly insights_heading: string;
  /** The server's word for an absent figure ("Not available"), shown beside its reason. */
  readonly not_available_text: string;
  /** The page declares only the outcomes the source bakes: no input yields `insufficient` (SC9A-GATE-R5-001). */
  readonly outcome: 'complete' | 'empty';
  readonly outcome_text: string | null;
  readonly period: { readonly end: string; readonly start: string };
  readonly period_label: string | null;
  readonly pointers: readonly ScorecardPointerLine[];
  readonly preset_label: string;
  readonly prior_period: { readonly end: string; readonly start: string } | null;
  readonly retail_sales_heading: string;
  readonly scope_text: string;
  readonly tacos: readonly {
    readonly card_id: string;
    readonly coverage_slot: string;
    readonly figure: ScorecardFigure;
    readonly link: ScorecardLink | null;
    /** Which marketplace the TACoS measures, served only when the page shows two. */
    readonly marketplace_text: string | null;
  }[];
}

const ARTIFACT_KEYS = [
  'also_changed', 'boundary', 'boundary_heading', 'cards', 'cross_network_spend', 'evidence_status', 'findings', 'insight_cards',
  'insights', 'insights_heading', 'not_available_text', 'outcome', 'outcome_text', 'period', 'period_label', 'pointers', 'preset_label',
  'prior_period',
  'retail_sales_heading', 'scope_text', 'tacos', 'type', 'version',
] as const;

/** Decode one served body; any drift from the server contract is refused, never repaired. */
export function parseNetworkScorecardArtifact(value: unknown): NetworkScorecardArtifact {
  const item = record(value, ARTIFACT_KEYS);
  if (item.type !== 'NetworkScorecardArtifact' || item.version !== 1) return fail();
  const outcome = (['complete', 'empty'] as const).find((name) => name === item.outcome) ?? fail();
  const outcomeText = nullableText(item.outcome_text);
  if ((outcome === 'complete') !== (outcomeText === null)) return fail();
  const period = window(item.period);
  const prior = item.prior_period === null ? null : window(item.prior_period);
  if (prior !== null && prior.end >= period.start) return fail();
  const periodLabel = nullableText(item.period_label);
  if (periodLabel !== null && prior !== null) return fail();
  const cards = list(item.cards).map(card);
  if ((outcome === 'complete') !== (cards.length > 0)) return fail();
  if (new Set(cards.map((entry) => entry.card_id)).size !== cards.length) return fail();
  const ids = new Set(cards.map((entry) => entry.card_id));
  const tacos = list(item.tacos).map((entry) => {
    const row = record(entry, ['card_id', 'coverage_slot', 'figure', 'link', 'marketplace_text']);
    const cardId = text(row.card_id);
    if (!ids.has(cardId)) return fail();
    // The coverage entry that gates the TACoS change (ADR-119), named by the server.
    const coverageSlot = text(row.coverage_slot);
    if (!coverageSlot.startsWith('tacos:')) return fail();
    const parsed = figure(row.figure);
    if (parsed.metric !== 'tacos') return fail();
    return { card_id: cardId, coverage_slot: coverageSlot, figure: parsed, link: link(row.link, '/retail-health/'), marketplace_text: nullableText(row.marketplace_text) };
  });
  const crossNetwork = list(item.cross_network_spend).map((entry) => {
    const row = record(entry, ['currency_code', 'figure', 'label', 'marketplace_text', 'networks_loaded', 'reason', 'row_id', 'text']);
    const currency = text(row.currency_code);
    const parsed = row.figure === null ? null : figure(row.figure, currency);
    // Spend is the only figure that totals across networks, and only with two or more loaded;
    // a served total always has its value (a typed absence would render as a blank, G9A-10).
    if (parsed !== null && (parsed.metric !== 'ad_spend' || parsed.value === null)) return fail();
    const networks = list(row.networks_loaded).map(text);
    if ((parsed !== null) !== (networks.length >= 2)) return fail();
    const reason = nullableText(row.reason);
    const line = nullableText(row.text);
    if ((parsed === null) !== (line !== null) || (parsed === null) !== (reason !== null)) return fail();
    return {
      currency_code: currency, figure: parsed, label: text(row.label), marketplace_text: nullableText(row.marketplace_text), networks_loaded: networks,
      reason, row_id: text(row.row_id), text: line,
    };
  });
  // No two spend rows share one identity, and no two TACoS rows share one coverage slot: the
  // served keys, never the rendered marketplace sentence (SC9A-GATE-004).
  if (new Set(crossNetwork.map((row) => row.row_id)).size !== crossNetwork.length) return fail();
  if (new Set(tacos.map((row) => row.coverage_slot)).size !== tacos.length) return fail();
  const channelsByCard = new Map(cards.map((entry) => [entry.card_id, new Set(entry.channel_table.rows.map((row) => row.channel))]));
  const known = (cardId: string, channel: string) => channelsByCard.get(cardId)?.has(channel) === true;
  const insightCards = list(item.insight_cards).map((entry): ScorecardInsightCard => {
    const row = record(entry, ['body', 'card_id', 'channel', 'finding_id', 'headline', 'kind_label', 'marketplace_text', 'next_step', 'title']);
    const findingId = (['channel-roas-mover', 'channel-share-of-network-spend'] as const).find((name) => name === row.finding_id) ?? fail();
    const cardId = text(row.card_id);
    const channel = text(row.channel);
    if (!known(cardId, channel)) return fail();
    const marketplace = nullableText(row.marketplace_text);
    let step: InsightNextStep | null = null;
    if (row.next_step !== null) {
      try { step = parseNextStep(row.next_step); } catch { return fail(); }
      // Campaigns can't be scoped to a marketplace, so a card that names one offers no step.
      if (step.kind !== 'open_page' || findingId !== 'channel-roas-mover' || marketplace !== null) return fail();
    }
    return {
      body: text(row.body), card_id: cardId, channel, finding_id: findingId, headline: text(row.headline), kind_label: text(row.kind_label),
      marketplace_text: marketplace, next_step: step, title: text(row.title),
    };
  });
  if (insightCards.length > 3) return fail();
  // The cards are the slot's narrated findings: a filled slot has cards, any other has none.
  const insights = slot(list(item.insights).length === 1 ? (item.insights as unknown[])[0] : fail());
  if ((insights.state === 'filled') !== (insightCards.length > 0)) return fail();
  const alsoChanged = item.also_changed === null ? null : (() => {
    const section = record(item.also_changed, ['heading', 'intro', 'rows']);
    const rows = list(section.rows).map((entry): ScorecardAlsoChangedRow => {
      // Direction only: a row carries no figure, so it can't state a cross-channel total.
      const row = record(entry, ['card_id', 'channel', 'dialog_text', 'direction_text', 'finding_id', 'label', 'marketplace_text', 'rule_id', 'scope_text', 'title', 'window_text']);
      const cardId = text(row.card_id);
      const channel = text(row.channel);
      if (!known(cardId, channel)) return fail();
      return {
        card_id: cardId, channel, dialog_text: text(row.dialog_text), direction_text: text(row.direction_text), finding_id: text(row.finding_id), label: text(row.label),
        marketplace_text: nullableText(row.marketplace_text), rule_id: text(row.rule_id), scope_text: text(row.scope_text), title: text(row.title),
        window_text: text(row.window_text),
      };
    });
    // The section is skipped when empty, never served empty; at most five rows.
    if (rows.length === 0 || rows.length > 5) return fail();
    return { heading: text(section.heading), intro: text(section.intro), rows };
  })();
  // Marketplace identity is all or nothing across the retail rows, channel tables (their arrival
  // tag), insight cards and Also changed rows: with two marketplaces each names its own, with one none does (G9A-6,
  // SC9A-GATE-001). No two cards, and no two rows, share a name, so none reads as another.
  const named = [...tacos, ...crossNetwork, ...cards.map((entry) => entry.channel_table), ...insightCards, ...(alsoChanged?.rows ?? [])];
  if (named.some((row) => row.marketplace_text === null) && named.some((row) => row.marketplace_text !== null)) return fail();
  const titles = [...insightCards, ...(alsoChanged?.rows ?? [])].map((row) => row.title);
  if (new Set(titles).size !== titles.length) return fail();
  const pointers = list(item.pointers).map((entry) => {
    const row = record(entry, ['href', 'lead', 'link_text', 'tab_id']);
    return { href: text(row.href), lead: text(row.lead), link_text: text(row.link_text), tab_id: text(row.tab_id) };
  });
  list(item.findings);
  const evidence = parseEvidenceStatus(item.evidence_status, period);
  if ((outcome === 'complete') !== (evidence !== null)) return fail();
  return {
    also_changed: alsoChanged,
    boundary: list(item.boundary).map(text),
    boundary_heading: text(item.boundary_heading),
    cards,
    cross_network_spend: crossNetwork,
    evidence_status: evidence,
    insight_cards: insightCards,
    insights,
    insights_heading: text(item.insights_heading),
    not_available_text: text(item.not_available_text),
    outcome,
    outcome_text: outcomeText,
    period,
    period_label: periodLabel,
    pointers,
    preset_label: text(item.preset_label),
    prior_period: prior,
    retail_sales_heading: text(item.retail_sales_heading),
    scope_text: text(item.scope_text),
    tacos,
  };
}

/** Whether a figure measures an attributed return, which is only ever served on a channel row. */
export function isAttributedMetric(metric: string): boolean {
  return ATTRIBUTED_METRICS.has(metric);
}
