// SPDX-License-Identifier: MIT-0
'use client';
/**
 * The Scorecard (Performance › Scorecard, ia §8.15): return on ad spend per network and channel.
 *
 * Every text is a server field and every figure a served value: the model selector switches
 * which served headline shows, with no request; nothing here adds, ranks or converts a figure.
 * Attributed returns appear only on a channel row of one network card (ia §8.5).
 */
import { AnswerSentence } from './AnswerSentence';
import { metricLabel } from '@/lib/glossary';
import { Fragment, useEffect, useId, useRef, useState, type ReactNode } from 'react';

import { CoverageProvider, FigureChange, TileAbsence, TileChange } from './Coverage';
import { DataTable, DtBody, DtCell, DtColumn, DtHead, DtRow, DtRowHeader } from './DataTable';
import { InsightFocusDialog, type InsightDialogSlot, type InsightFocusSurface } from './InsightFocusDialog';
import { InsightNextStepControl } from './InsightCard';
import { useVisibleTabs } from './NextStepContext';
import { SCORECARD_CHANNEL_SECTION } from '@/lib/insight-next-step';
import { formatCurrency, formatRatePercent, formatRatio } from '@/lib/exact-format';
import { adProductChannels, bindingByRoute, isCalendarPreset } from '@/lib/page-bindings';
import type { NewCustomersPresentation } from '@/lib/presentation-v2';
import type { StepOneRow } from '@/lib/new-customers';
import type { OverviewSource } from '@/lib/overview';
import { ChangeChip } from './ChartPrimitives';
import type {
  NetworkScorecardArtifact,
  ScorecardCard,
  ScorecardFigure,
  ScorecardInsightCard,
} from '@/lib/scorecard';
import { scorecardAnswer } from '@/lib/scorecard';

/**
 * A served figure's value text: money and multipliers from their two-place served display value
 * (ADR-084, plan S26), a rate through the shared percent formatter. The browser never rounds.
 */
export function figureText(figure: ScorecardFigure, prior = false): string | null {
  const value = prior ? figure.prior_value : figure.value;
  const display = prior ? figure.prior_display_value : figure.display_value;
  if (value === null) return null;
  if (figure.format === 'percent') return formatRatePercent(value, 1);
  if (display === null) return null;
  if (figure.format === 'currency' && figure.currency_code !== null) return formatCurrency(display, figure.currency_code);
  return formatRatio(display);
}

/**
 * A tile: label, value or "Not available" with its server reason, prior, basis, and the change
 * chip (or its withheld text, from the coverage `slot` the server wrote). The metric focus
 * openers ("Details ›") wait for the Scorecard metric focus path, so none renders yet.
 */
export function ScorecardTile({ figure, notAvailable, slot, market = null, children }: {
  readonly figure: ScorecardFigure;
  /** The served marketplace a retail tile measures, when the page shows more than one. */
  readonly market?: string | null;
  /** The server's word for an absent figure. */
  readonly notAvailable: string;
  /** The served coverage slot that gates this figure's change (ADR-119). */
  readonly slot: string;
  /** Anything the tile's foot carries beside the change, such as its link. */
  readonly children?: ReactNode;
}) {
  const value = figureText(figure);
  const prior = figureText(figure, true);
  return <div className={value === null ? 'tile na' : 'tile'}>
    <span className="k">{metricLabel(figure.metric, figure.label)}</span>
    {market === null ? null : <span className="market">{market}</span>}
    <span className="v num">{value ?? notAvailable}</span>
    {value === null ? <span className="reason">{figure.absence_text}</span> : null}
    {prior === null || figure.prior_label === null ? null : <span className="prior">{figure.prior_label}: {prior}</span>}
    <span className="basis">{figure.basis}</span>
    {value === null ? null : <TileAbsence slot={slot} figure={metricLabel(figure.metric, figure.label)} />}
    <div className="foot">
      {/* The chip, or nothing while coverage withholds it: TileAbsence then says why, once. */}
      {figure.change === null ? <span /> : <TileChange change={figure.change} slot={slot} />}
      {children}
    </div>
  </div>;
}

function NetworkCardSection({ artifact, card, highlight, notAvailable, several }: {
  /** The page body, whose served windows the answer sentence names. */
  readonly artifact: NetworkScorecardArtifact;
  readonly card: ScorecardCard;
  readonly highlight: string | null;
  readonly notAvailable: string;
  /** Whether the page shows more than one card, so each table names its own card. */
  readonly several: boolean;
}) {
  const headingId = useId();
  const selectId = useId();
  const [model, setModel] = useState(card.headline.default_model);
  const selected = card.headline.models.find((item) => item.model === model) ?? card.headline.models[0];
  if (selected === undefined) return null;
  return <section className="card section scorecard-card" aria-labelledby={headingId}>
    <div className="section-head">
      <h2 id={headingId}>{card.title}</h2>
      {card.networks_text === null ? null : <span className="note">{card.networks_text}</span>}
    </div>
    {card.dsp_absence_text === null ? null : <p className="small dim">{card.dsp_absence_text}</p>}
    <AnswerSentence answer={scorecardAnswer(artifact, card, selected.model)} />
    {/* Readout A.2: the selected model's served basis under the answer (sc-basis). */}
    {several ? null : <p className="answer-basis" data-slot="sc-basis">{selected.figure.basis}</p>}
    {/* UX sweep fix 3: the model explainer folds into one "How credit is shared" disclosure. */}
    <details className="about sc-model">
      <summary>{SCORECARD_CARD_COPY.modelSummary} {selected.option_label}</summary>
      <div className="body">
        <div className="select-wrap">
          <label htmlFor={selectId}>{card.headline.selector_label}</label>
          <select id={selectId} value={model} onChange={(event) => setModel(event.currentTarget.value)}>
            {card.headline.models.map((item) => <option key={item.model} value={item.model}>{item.option_label}</option>)}
          </select>
        </div>
        <p>{card.headline.model_note}</p>
      </div>
    </details>
    {several ? <>
      <div className="tiles two">
        <ScorecardTile figure={selected.figure} notAvailable={notAvailable} slot={`headline:${card.card_id}`} />
        <ScorecardTile figure={card.spend} notAvailable={notAvailable} slot={`spend:${card.card_id}`} />
      </div>
      <RankingDisclosure card={card} highlight={highlight} notAvailable={notAvailable} several={several} headingId={headingId} />
    </> : <div className="tiles three" data-slot="sc-tiles">
      {/* UI refresh 3.1 (readout A.2): the answer states the return, so the tiles are spend, its
          share of retail sales and the networks loaded, each from its served row. */}
      <ScorecardTile figure={card.spend} notAvailable={notAvailable} slot={`spend:${card.card_id}`} />
      <RetailTiles artifact={artifact} notAvailable={notAvailable} />
    </div>}
  </section>;
}

/** The ranking in a closed disclosure under its served heading (UX sweep fix 3, readout A.2). */
function RankingDisclosure({ card, headingId, highlight, notAvailable, several }: {
  readonly card: ScorecardCard;
  readonly headingId?: string;
  readonly highlight: string | null;
  readonly notAvailable: string;
  readonly several: boolean;
}) {
  const ranking = card.ranking;
  return <details className="about rank-block">
      <summary>{ranking.heading}</summary>
      <p className="rank-outcome">{ranking.outcome_text}</p>
      {ranking.outcome === 'insufficient' ? null : <DataTable title={ranking.caption} note={ranking.caption_detail} nameLeadId={several ? headingId : undefined}>
        <DtHead>
          <DtRow>
            <DtColumn className="l">{ranking.row_header_label}</DtColumn>
            {ranking.models.map((item) => <DtColumn key={item.model}>{item.label}</DtColumn>)}
          </DtRow>
        </DtHead>
        <DtBody>
          {ranking.rows.map((row) => <DtRow key={row.channel} className={row.channel === highlight ? 'highlight-row' : undefined}>
            <DtRowHeader>{row.channel_label}</DtRowHeader>
            {row.values.map((value, index) => <DtCell key={value.model} label={ranking.models[index]?.label ?? value.model}>
              {value.credit_text ?? (value.display_value === null ? notAvailable : formatRatio(value.display_value))}{' '}
              {value.rank_text === null ? null : <span className="rank">{value.rank_text}</span>}
            </DtCell>)}
          </DtRow>)}
        </DtBody>
      </DataTable>}
      <p className="dt-foot">{ranking.foot}</p>
    </details>;
}

/** One channel's served New customers row, or the served reason the band shows none (plan 2.3). */
export type ChannelNewCustomers =
  | { readonly kind: 'rows'; readonly rows: ReadonlyMap<string, StepOneRow>; readonly share_basis: string; readonly not_available: string }
  | { readonly kind: 'absent'; readonly text: string };

/** The channel cards' and disclosures' browser-owned words (Scorecard LLD "Browser-owned copy"). */
export const SCORECARD_CARD_COPY = {
  campaignsOn: 'Campaigns on',
  heading: 'What each channel does for you',
  modelSummary: 'How credit is shared:',
  newCustomers: 'New customers ›',
  ntbBasis: 'First-time-buyer share:',
  ntbCost: 'Ad cost per first-time-buyer purchase',
  ntbShare: 'Share of sales from first-time buyers',
  paysBack: 'Pays back',
  returnBasis: 'Return:',
  table: 'Show the channel table',
  whatChanged: 'What changed',
  why: "Each channel counts the sales Amazon credits to it, so one sale can count in two channels. That's why channel figures aren't added up, and why the headline, which counts each sale once, can read lower than a channel's own return.",
  winsNew: 'Wins new customers',
} as const;

/** ABS-SC-CH (`docs/ui/page-absence-copy.md`): no served New customers row for this channel and window. */
export const CHANNEL_NEW_CUSTOMERS_ABSENT = "New-customer figures aren't available for this window.";

/**
 * Join the served New customers rows to the Scorecard's channels on the governed taxonomy keys
 * (`ad_product_channels`), never by name; only for the same marketplace and the same window.
 */
export function channelNewCustomers(
  artifact: NetworkScorecardArtifact,
  source: OverviewSource<NewCustomersPresentation> | null,
): ChannelNewCustomers {
  if (source === null || source.kind === 'rejected') return { kind: 'absent', text: CHANNEL_NEW_CUSTOMERS_ABSENT };
  const presentation = source.presentation;
  if (presentation.kind === 'window_unavailable') return { kind: 'absent', text: presentation.message };
  const body = presentation.artifact;
  const market = body.outcome === 'complete' && body.markets.length === 1 && artifact.cards.length === 1 ? body.markets[0] : undefined;
  const sameWindow = body.period.start === artifact.period.start && body.period.end === artifact.period.end;
  if (market === undefined || !sameWindow || market.currency_code !== artifact.cards[0]?.currency_code) {
    return { kind: 'absent', text: body.outcome_text ?? CHANNEL_NEW_CUSTOMERS_ABSENT };
  }
  const rows = new Map<string, StepOneRow>();
  for (const row of market.new_to_brand.rows) {
    const channel = adProductChannels.get(row.ad_product_key);
    if (channel !== undefined) rows.set(channel, row);
  }
  return { kind: 'rows', not_available: body.page_text.not_available_text, rows, share_basis: market.new_to_brand.share_basis };
}

function newCustomerFigure(figure: StepOneRow['ntb_revenue_share'], notAvailable: string): string {
  if (figure.display_text !== null) return figure.display_text;
  if (figure.display_value !== null && figure.currency_code !== null) return formatCurrency(figure.display_value, figure.currency_code);
  return figure.absence?.text ?? notAvailable;
}

/** "What each channel does for you" (readout A.3, OD-UX-4): one card per served channel row. */
function ChannelCards({ artifact, card, highlight, newCustomers, notAvailable, preset }: {
  readonly artifact: NetworkScorecardArtifact;
  readonly card: ScorecardCard;
  readonly highlight: string | null;
  readonly newCustomers: ChannelNewCustomers;
  readonly notAvailable: string;
  readonly preset: string | null;
}) {
  const table = card.channel_table;
  const query = preset === null ? '' : `?preset=${preset}`;
  const shareCard = (channel: string) => artifact.insight_cards.find((entry) => entry.card_id === card.card_id && entry.channel === channel && entry.finding_id === 'channel-share-of-network-spend');
  const returnBasis = table.columns.find((column) => column.column === 'roas_attributed_14d')?.basis ?? null;
  const tabs = useVisibleTabs();
  const newCustomersTab = tabs.find((tab) => tab.tab_id === 'new-customers');
  return <>
    <div className="ch-cards">
      {table.rows.map((row) => {
        const [spend, , roas, halo] = row.figures;
        const share = shareCard(row.channel);
        const ntb = newCustomers.kind === 'rows' ? newCustomers.rows.get(row.channel) ?? null : null;
        const highlighted = row.channel === highlight;
        return <article key={row.channel} className={highlighted ? 'ch-card highlight-card' : 'ch-card'} data-channel={row.channel} data-slot={`ch-${row.channel}`}>
          <h3>{row.channel_label}</h3>
          {highlighted ? <span className="ch-tag">{table.highlight_tag}</span> : null}
          <p className="ch-spend">
            <span className="v num" data-slot={`ch-${row.channel}-spend`}>{figureText(spend) ?? notAvailable}</span>
            {spend.change === null ? null : <FigureChange change={spend.change} slot={`channels:${card.card_id}`} figure={`${row.channel_label} ${metricLabel(spend.metric, spend.label)}`} />}
          </p>
          {share === undefined ? null : <p className="small" data-slot={`ch-${row.channel}-share`}>{share.headline}. {share.body}</p>}
          <h4>{SCORECARD_CARD_COPY.paysBack}</h4>
          <dl className="ch-figures">
            {[roas, halo].map((figure) => <div key={figure.metric} data-slot={`ch-${row.channel}-${figure.metric === 'roas_attributed_14d' ? 'roas' : 'roas-halo'}`}>
              <dt>{metricLabel(figure.metric, figure.label)}</dt>
              <dd>
                {figureText(figure) ?? <span className="cell-na">{notAvailable}<span className="reason">{figure.absence_text}</span></span>}
                {figure.change === null ? null : <FigureChange change={figure.change} slot={`channels:${card.card_id}`} figure={`${row.channel_label} ${metricLabel(figure.metric, figure.label)}`} />}
              </dd>
            </div>)}
          </dl>
          <h4>{SCORECARD_CARD_COPY.winsNew}</h4>
          {ntb === null
            ? <p className="small dim" data-slot={`ch-${row.channel}-ntb-absent`}>{newCustomers.kind === 'absent' ? newCustomers.text : CHANNEL_NEW_CUSTOMERS_ABSENT}</p>
            : <dl className="ch-figures">
              <div data-slot={`ch-${row.channel}-ntb-share`}>
                <dt>{SCORECARD_CARD_COPY.ntbShare}</dt>
                <dd>
                  {newCustomerFigure(ntb.ntb_revenue_share, newCustomers.kind === 'rows' ? newCustomers.not_available : notAvailable)}
                  {ntb.ntb_revenue_share_change.change === null ? null : <ChangeChip change={ntb.ntb_revenue_share_change.change} />}
                </dd>
              </div>
              <div data-slot={`ch-${row.channel}-cost-ntb`}>
                <dt>{SCORECARD_CARD_COPY.ntbCost}</dt>
                <dd>{newCustomerFigure(ntb.cost_per_ntb_purchase, newCustomers.kind === 'rows' ? newCustomers.not_available : notAvailable)}</dd>
              </div>
            </dl>}
          <p className="ch-links">
            {row.link === null ? null : <a href={row.link.href} aria-label={row.link.label}>{SCORECARD_CARD_COPY.campaignsOn} {row.channel_label} ›</a>}
            {newCustomersTab === undefined ? null : <a href={`${newCustomersTab.route}${query}`}>{SCORECARD_CARD_COPY.newCustomers}</a>}
          </p>
        </article>;
      })}
    </div>
    <p className="small dim" data-slot="ch-why">{SCORECARD_CARD_COPY.why}</p>
    <p className="small dim" data-slot="ch-basis">
      {returnBasis === null ? null : <>{SCORECARD_CARD_COPY.returnBasis} {returnBasis}.</>}
      {newCustomers.kind === 'rows' ? <> {SCORECARD_CARD_COPY.ntbBasis} {newCustomers.share_basis}.</> : null}
    </p>
  </>;
}

function ChannelTable({ card, highlight, notAvailable, several }: {
  readonly card: ScorecardCard;
  readonly highlight: string | null;
  readonly notAvailable: string;
  /** Whether the page shows more than one card, so the table is named with its served caption detail. */
  readonly several: boolean;
}) {
  const table = card.channel_table;
  return <>
    <p className="fresh">{table.fresh_text}</p>
    {/* One card shows the channel cards above, so the table is a closed disclosure; two cards keep it open. */}
    <details className="exact ch-table" open={several}>
      <summary>{SCORECARD_CARD_COPY.table}</summary>
    <DataTable title={table.caption} note={table.caption_detail} nameWithNote={several}>
      <DtHead>
        <DtRow>
          <DtColumn className="l">{table.row_header_label}</DtColumn>
          {table.columns.map((column) => <DtColumn key={column.column} basis={column.basis}>{metricLabel(column.column, column.label)}</DtColumn>)}
          <DtColumn className="l">{table.link_column}</DtColumn>
        </DtRow>
      </DtHead>
      <DtBody>
        {table.rows.map((row) => {
          const highlighted = row.channel === highlight;
          return <DtRow key={row.channel} className={highlighted ? 'highlight-row' : undefined} data-channel={row.channel}>
            <DtRowHeader>
              {row.channel_label}
              {/* With two marketplaces the tag names the one its row measures (SC9A-GATE-001). */}
              {highlighted ? <span className="tag-channel">{table.highlight_tag}{table.marketplace_text === null ? null : <> · <span className="market">{table.marketplace_text}</span></>}</span> : null}
            </DtRowHeader>
            {row.figures.map((figure, index) => {
              const value = figureText(figure);
              return <DtCell
                key={figure.metric}
                label={metricLabel(figure.metric, table.columns[index]?.label ?? figure.label)}
                absent={value === null}
                basis={{ coverage: null, text: table.columns[index]?.basis ?? null }}
              >
                {/* "Not available over hiding": the word and the server's reason, both visible. */}
                {value ?? <span className="cell-na">{notAvailable}<span className="reason">{figure.absence_text}</span></span>}
                {figure.change === null ? null : <FigureChange change={figure.change} slot={`channels:${card.card_id}`} figure={`${row.channel_label} ${metricLabel(figure.metric, figure.label)}`} />}
              </DtCell>;
            })}
            <DtCell label={table.link_column} className="cell-link nw">
              {row.link === null ? null : <a href={row.link.href} aria-label={row.link.label}>{row.link.text}</a>}
            </DtCell>
          </DtRow>;
        })}
      </DtBody>
    </DataTable>
    </details>
    {/* One card: the cards' ch-why line already says the table's foot (T2L-3), so only the
        served absent lines stay, in the basis block. */}
    <p className="dt-foot">{several ? table.foot : null}{table.absent_lines.map((line) => <span key={line}> {line}</span>)}</p>
  </>;
}

function Pointers({ artifact }: { readonly artifact: NetworkScorecardArtifact }) {
  const tabs = useVisibleTabs();
  return <>
    {artifact.pointers.map((pointer) => {
      // A pointer line renders only when the session's manifest lists its tab (LLD §9).
      const tab = tabs.find((candidate) => candidate.tab_id === pointer.tab_id);
      return tab === undefined ? null : <p key={pointer.tab_id} className="pointer">{pointer.lead} <a href={tab.route}>{pointer.link_text}</a>.</p>;
    })}
  </>;
}

function InsightCards({ artifact, cards = artifact.insight_cards, onRead }: {
  readonly artifact: NetworkScorecardArtifact;
  readonly cards?: readonly ScorecardInsightCard[];
  readonly onRead: (id: string, source: HTMLButtonElement) => void;
}) {
  if (cards.length === 0) {
    return <div className="insight quiet"><p>{artifact.insights.absence_reason ?? artifact.insights.text}</p></div>;
  }
  return <div className="insights">
    {cards.map((card) => <article key={cardKey(card)} className="insight">
      {/* With two marketplaces the eyebrow says whose figures the card narrates (SC9A-GATE-001). */}
      <span className="kind">{card.kind_label}{card.marketplace_text === null ? null : <> · <span className="market">{card.marketplace_text}</span></>}</span>
      <h3>{card.headline}</h3>
      <p>{card.body}</p>
      <div className="acts">
        {card.next_step === null ? null : <span className="next"><InsightNextStepControl step={card.next_step} /></span>}
        <div className="act-row">
          <button
            className="view-details"
            type="button"
            aria-label={`Read more: ${card.title}`}
            onClick={(event) => onRead(cardKey(card), event.currentTarget)}
          >Read more ›</button>
        </div>
      </div>
    </article>)}
  </div>;
}

function cardKey(card: { readonly card_id: string; readonly channel: string; readonly finding_id: string }): string {
  return `${card.finding_id}:${card.card_id}:${card.channel}`;
}

/** The served TACoS and cross-network spend rows as tiles (one card: in the card's tile row). */
function RetailTiles({ artifact, notAvailable }: { readonly artifact: NetworkScorecardArtifact; readonly notAvailable: string }) {
  return <>
      {artifact.tacos.map((row) => <ScorecardTile key={row.coverage_slot} figure={row.figure} notAvailable={notAvailable} slot={row.coverage_slot} market={row.marketplace_text}>
        {row.link === null ? null : <a className="chip-link" href={row.link.href} aria-label={row.link.label}>{row.link.text}</a>}
      </ScorecardTile>)}
      {artifact.cross_network_spend.map((row) => <div key={row.row_id} className={row.figure === null ? 'tile na' : 'tile'}>
        <span className="k">{row.label}</span>
        {row.marketplace_text === null ? null : <span className="market">{row.marketplace_text}</span>}
        <span className="v num">{row.figure === null ? row.text : figureText(row.figure)}</span>
        {row.reason === null ? null : <span className="reason">{row.reason}</span>}
        {row.figure === null ? null : <span className="basis">{row.figure.basis}</span>}
      </div>)}
  </>;
}

function RetailSales({ artifact, notAvailable }: { readonly artifact: NetworkScorecardArtifact; readonly notAvailable: string }) {
  const headingId = useId();
  if (artifact.tacos.length === 0 && artifact.cross_network_spend.length === 0) return null;
  return <section className="section" aria-labelledby={headingId}>
    <h2 id={headingId}>{artifact.retail_sales_heading}</h2>
    <div className="tiles two"><RetailTiles artifact={artifact} notAvailable={notAvailable} /></div>
  </section>;
}

/** The served finding the channel cards don't already narrate (T2L-3, readout A.2 sc-changed). */
const SHARE_FINDING = 'channel-share-of-network-spend';
/**
 * "What changed" with one card: every served finding other than the spend-share sentences, which
 * each channel card already reads, then the first served spend-share finding; at most three, in
 * served order. Nothing is ranked or rewritten.
 */
export function whatChangedCards(cards: readonly ScorecardInsightCard[]): readonly ScorecardInsightCard[] {
  const others = cards.filter((card) => card.finding_id !== SHARE_FINDING);
  const share = cards.find((card) => card.finding_id === SHARE_FINDING);
  return [...others, ...(share === undefined ? [] : [share])].slice(0, 3);
}

/**
 * On arrival at `#channel-table` (the one section a cross-tab step may name on this page, ADR-122
 * decision 5), move focus to that focusable section so the arrival is announced.
 */
function useSectionArrival(ready: boolean) {
  useEffect(() => {
    if (!ready || typeof window === 'undefined') return;
    if (decodeURIComponent(window.location.hash.slice(1)) !== SCORECARD_CHANNEL_SECTION) return;
    const section = document.getElementById(SCORECARD_CHANNEL_SECTION);
    section?.scrollIntoView?.({ block: 'start' });
    section?.focus({ preventScroll: true });
  }, [ready]);
}

export function ScorecardView({ artifact, highlight = null, newCustomers = null, preset = null }: {
  readonly artifact: NetworkScorecardArtifact;
  /** The channel a campaign's step asks to outline; only a served channel is honoured. */
  readonly highlight?: string | null;
  /** The served New customers presentation for the same preset (plan 2.3), or null when not asked. */
  readonly newCustomers?: OverviewSource<NewCustomersPresentation> | null;
  /** The preset both were served on, carried on the New customers link. */
  readonly preset?: string | null;
}) {
  const [insight, setInsight] = useState<string | null>(null);
  const insightOrigin = useRef<HTMLButtonElement | null>(null);
  const insightsHeading = useId();
  const alsoHeading = useId();
  const channelHeading = useId();
  useSectionArrival(artifact.outcome === 'complete');
  if (artifact.outcome !== 'complete') {
    return <section className="callout" role="status">
      <h2>{bindingByRoute.scorecard.display_name}</h2>
      <p>{artifact.outcome_text}</p>
    </section>;
  }
  // Only a channel the served body names can be highlighted; any other value shows nothing.
  const notAvailable = artifact.not_available_text;
  const several = artifact.cards.length > 1;
  // The step names a channel, never a marketplace (ADR-122 `highlight_channel`), so the row is
  // the campaign's only when one card carries that channel. With the channel in several
  // marketplaces' cards no row is tagged, rather than tagging a row in the wrong marketplace
  // (SC9A-GATE-001).
  const carrying = highlight === null ? [] : artifact.cards.filter((card) => card.channel_table.rows.some((row) => row.channel === highlight));
  const arrivalCard = carrying.length === 1 ? carrying[0]?.card_id ?? null : null;
  const highlightFor = (card: ScorecardCard) => (card.card_id === arrivalCard ? highlight : null);
  // Every dialog carries the served artifact and prompt stamps (SC9A-GATE-R5-004).
  const stamps = artifact.insights;
  const dialogSlots: InsightDialogSlot[] = [
    ...artifact.insight_cards.map((card: ScorecardInsightCard): InsightDialogSlot => ({
      absence_class: null,
      absence_reason: null,
      artifact_revision: stamps.artifact_revision,
      evidence_line: stamps.evidence_line,
      id: cardKey(card),
      next_step: card.next_step,
      output_class: 'observed',
      prompt_version: stamps.prompt_version,
      state: 'filled',
      text: card.body,
    })),
    ...(artifact.also_changed?.rows ?? []).map((row): InsightDialogSlot => ({
      absence_class: null,
      absence_reason: null,
      artifact_revision: stamps.artifact_revision,
      // The slot's evidence line describes the narrated cards (shares and ROAS moves); an Also
      // changed row is a spend or ROAS mover no card narrates, so it shows none (SC9A-GATE-R5-002).
      evidence_line: null,
      id: cardKey(row),
      next_step: null,
      output_class: 'observed',
      prompt_version: stamps.prompt_version,
      state: 'filled',
      text: row.dialog_text,
    })),
  ];
  // Each dialog's heading is its card's or row's served name, so two alike never share one.
  const titles = new Map<string, string>([
    ...artifact.insight_cards.map((card) => [cardKey(card), card.title] as const),
    ...(artifact.also_changed?.rows ?? []).map((row) => [cardKey(row), row.title] as const),
  ]);
  const insightSurface: InsightFocusSurface = {
    labelFor: (id) => titles.get(id) ?? '',
    pageId: 'scorecard',
    // The titles are the server's names, so the assistant chip keeps their case.
    servedLabels: true,
    // No stats and no provenance override: the shared dialog (insight contract §3) shows the
    // narration and the contract's line for the deterministic stamp (slice 9a decision F11a).
    slots: dialogSlots,
    window: artifact.period,
  };
  const openInsight = (id: string, source: HTMLButtonElement) => {
    insightOrigin.current = source;
    setInsight(id);
  };
  return <CoverageProvider status={artifact.evidence_status} subject="Scorecard">
    <div className="scorecard">
      <p className="scope-band" aria-live="polite">{artifact.scope_text}</p>
      {artifact.cards.map((card) => <NetworkCardSection key={card.card_id} artifact={artifact} card={card} highlight={highlightFor(card)} notAvailable={notAvailable} several={several} />)}
      {several ? <section className="section" aria-labelledby={insightsHeading}>
        <h2 id={insightsHeading}>{artifact.insights_heading}</h2>
        <InsightCards artifact={artifact} onRead={openInsight} />
      </section> : null}
      {artifact.also_changed === null ? null : <section className="section" aria-labelledby={alsoHeading}>
        <h2 id={alsoHeading}>{artifact.also_changed.heading}</h2>
        <p className="small dim">{artifact.also_changed.intro}</p>
        <ul className="also-changed">
          {artifact.also_changed.rows.map((row) => <li key={cardKey(row)}>
            <span><button
              type="button"
              className="view-details"
              // With two marketplaces the served name carries the marketplace, so two alike differ.
              aria-label={row.title === row.label ? undefined : row.title}
              onClick={(event) => openInsight(cardKey(row), event.currentTarget)}
            >{row.label}</button>{' · '}{row.direction_text}</span>
            <span className="wc-sub">{row.window_text} · {row.scope_text}{row.marketplace_text === null ? null : <> · <span className="market">{row.marketplace_text}</span></>}</span>
          </li>)}
        </ul>
      </section>}
      <section className="section" id={SCORECARD_CHANNEL_SECTION} tabIndex={-1} aria-labelledby={channelHeading}>
        <h2 id={channelHeading}>{several ? artifact.cards[0]?.channel_table.heading : SCORECARD_CARD_COPY.heading}</h2>
        {artifact.cards.map((card) => <Fragment key={card.card_id}>
          {several ? null : <ChannelCards artifact={artifact} card={card} highlight={highlightFor(card)} newCustomers={channelNewCustomers(artifact, newCustomers)} notAvailable={notAvailable} preset={preset} />}
          <ChannelTable card={card} highlight={highlightFor(card)} notAvailable={notAvailable} several={several} />
        </Fragment>)}
        <Pointers artifact={artifact} />
      </section>
      {several ? <RetailSales artifact={artifact} notAvailable={notAvailable} /> : <>
        {/* UI refresh 3.1 (readout A.2): what changed after the channel cards, then the ranking. */}
        <section className="section" aria-labelledby={insightsHeading} data-slot="sc-changed">
          <h2 id={insightsHeading}>{SCORECARD_CARD_COPY.whatChanged}</h2>
          <InsightCards artifact={artifact} cards={whatChangedCards(artifact.insight_cards)} onRead={openInsight} />
        </section>
        {artifact.cards.map((card) => <RankingDisclosure key={card.card_id} card={card} highlight={highlightFor(card)} notAvailable={notAvailable} several={false} />)}
      </>}
      <details className="about">
        <summary>{artifact.boundary_heading}</summary>
        <div className="body">{artifact.boundary.map((line) => <p key={line}>{line}</p>)}</div>
      </details>
    </div>
    {insight === null ? null : <InsightFocusDialog
      onClose={() => setInsight(null)}
      onSlotChange={setInsight}
      opener={insightOrigin}
      slotId={insight}
      surface={insightSurface}
      surfaceKey={insight}
    />}
  </CoverageProvider>;
}

/**
 * The window control's place while the first response is on its way (mockup-loading.html):
 * one shape per declared preset, the calendar divider where it falls, and no text, because
 * every label and date is the server's. Hidden from assistive tech; the status names the wait.
 */
export function ScorecardControlsSkeleton() {
  const presets = bindingByRoute.scorecard.temporal_policy.candidate_presets;
  const firstCalendar = presets.findIndex((preset) => isCalendarPreset(preset));
  return <div className="controls controls-skel" aria-hidden="true">
    <div className="datectl dc-wide">
      <span className="skel h-label legend-skel" />
      <div className="seg">
        {presets.map((preset, index) => <Fragment key={preset}>
          {index === firstCalendar && index > 0 ? <span className="seg-div" /> : null}
          <span className="seg-opt"><span className="skel h-line" /><span className="skel h-line short" /></span>
        </Fragment>)}
      </div>
    </div>
    <div className="select-wrap dc-narrow"><span className="skel h-label legend-skel" /><span className="skel select-skel" /></div>
  </div>;
}

/** The Scorecard's loading skeleton: its scope line and sections in order, and a status. */
export function ScorecardSkeleton() {
  return <div role="status" aria-label="Loading Scorecard">
    <p className="scope-band scope-skel" aria-hidden="true"><span className="skel h-line" /></p>
    <div className="card section" aria-hidden="true">
      <span className="skel h-label" />
      <div className="tiles three">
        {[0, 1, 2].map((item) => <div className="tile static" key={item}><span className="skel h-label" /><span className="skel h-value" /><span className="skel h-line short" /></div>)}
      </div>
      <span className="skel h-chart" />
    </div>
    <div className="section" aria-hidden="true">
      <span className="skel h-label" />
      <div className="insights">{[0, 1, 2].map((item) => <div className="insight" key={item}><span className="skel h-line" /><span className="skel h-line short" /></div>)}</div>
    </div>
    <div className="section" aria-hidden="true"><span className="skel h-label" /><span className="skel h-chart" /></div>
    <p className="loading-note"><span className="spin" aria-hidden="true" />Loading Scorecard…</p>
  </div>;
}
