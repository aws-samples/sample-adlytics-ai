// SPDX-License-Identifier: MIT-0
'use client';
/**
 * The Scorecard (Performance › Scorecard, ia §8.15): return on ad spend per network and channel.
 *
 * Every text is a server field and every figure a served value: the model selector switches
 * which served headline shows, with no request; nothing here adds, ranks or converts a figure.
 * Attributed returns appear only on a channel row of one network card (ia §8.5).
 */
import { Fragment, useEffect, useId, useRef, useState, type ReactNode } from 'react';

import { CoverageProvider, FigureChange, TileAbsence, TileChange } from './Coverage';
import { DataTable, DtBody, DtCell, DtColumn, DtHead, DtRow, DtRowHeader } from './DataTable';
import { InsightFocusDialog, type InsightDialogSlot, type InsightFocusSurface } from './InsightFocusDialog';
import { InsightNextStepControl } from './InsightCard';
import { useVisibleTabs } from './NextStepContext';
import { SCORECARD_CHANNEL_SECTION } from '@/lib/insight-next-step';
import { formatCurrency, formatRatePercent, formatRatio } from '@/lib/exact-format';
import { bindingByRoute, isCalendarPreset } from '@/lib/page-bindings';
import type {
  NetworkScorecardArtifact,
  ScorecardCard,
  ScorecardFigure,
  ScorecardInsightCard,
} from '@/lib/scorecard';

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
function Tile({ figure, notAvailable, slot, market = null, children }: {
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
    <span className="k">{figure.label}</span>
    {market === null ? null : <span className="market">{market}</span>}
    <span className="v num">{value ?? notAvailable}</span>
    {value === null ? <span className="reason">{figure.absence_text}</span> : null}
    {prior === null || figure.prior_label === null ? null : <span className="prior">{figure.prior_label}: {prior}</span>}
    <span className="basis">{figure.basis}</span>
    {value === null ? null : <TileAbsence slot={slot} figure={figure.label} />}
    <div className="foot">
      {/* The chip, or nothing while coverage withholds it: TileAbsence then says why, once. */}
      {figure.change === null ? <span /> : <TileChange change={figure.change} slot={slot} />}
      {children}
    </div>
  </div>;
}

function NetworkCardSection({ card, highlight, notAvailable, several }: {
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
  const ranking = card.ranking;
  return <section className="card section scorecard-card" aria-labelledby={headingId}>
    <div className="section-head">
      <h2 id={headingId}>{card.title}</h2>
      {card.networks_text === null ? null : <span className="note">{card.networks_text}</span>}
    </div>
    {card.dsp_absence_text === null ? null : <p className="small dim">{card.dsp_absence_text}</p>}
    <div className="tiles three">
      <Tile figure={selected.figure} notAvailable={notAvailable} slot={`headline:${card.card_id}`} />
      <Tile figure={card.spend} notAvailable={notAvailable} slot={`spend:${card.card_id}`} />
      <div className="tile">
        <div className="select-wrap">
          <label htmlFor={selectId}>{card.headline.selector_label}</label>
          <select id={selectId} value={model} onChange={(event) => setModel(event.currentTarget.value)}>
            {card.headline.models.map((item) => <option key={item.model} value={item.model}>{item.option_label}</option>)}
          </select>
        </div>
        <span className="basis">{card.headline.model_note}</span>
      </div>
    </div>
    <div className="rank-block">
      <h3>{ranking.heading}</h3>
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
              {value.display_value === null ? notAvailable : formatRatio(value.display_value)}{' '}
              {value.rank_text === null ? null : <span className="rank">{value.rank_text}</span>}
            </DtCell>)}
          </DtRow>)}
        </DtBody>
      </DataTable>}
      <p className="dt-foot">{ranking.foot}</p>
    </div>
  </section>;
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
    <DataTable title={table.caption} note={table.caption_detail} nameWithNote={several}>
      <DtHead>
        <DtRow>
          <DtColumn className="l">{table.row_header_label}</DtColumn>
          {table.columns.map((column) => <DtColumn key={column.column} basis={column.basis}>{column.label}</DtColumn>)}
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
                label={table.columns[index]?.label ?? figure.label}
                absent={value === null}
                basis={{ coverage: null, text: table.columns[index]?.basis ?? null }}
              >
                {/* "Not available over hiding": the word and the server's reason, both visible. */}
                {value ?? <span className="cell-na">{notAvailable}<span className="reason">{figure.absence_text}</span></span>}
                {figure.change === null ? null : <FigureChange change={figure.change} slot={`channels:${card.card_id}`} figure={`${row.channel_label} ${figure.label}`} />}
              </DtCell>;
            })}
            <DtCell label={table.link_column} className="cell-link nw">
              {row.link === null ? null : <a href={row.link.href} aria-label={row.link.label}>{row.link.text}</a>}
            </DtCell>
          </DtRow>;
        })}
      </DtBody>
    </DataTable>
    <p className="dt-foot">{table.foot}{table.absent_lines.map((line) => <span key={line}> {line}</span>)}</p>
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

function InsightCards({ artifact, onRead }: {
  readonly artifact: NetworkScorecardArtifact;
  readonly onRead: (id: string, source: HTMLButtonElement) => void;
}) {
  if (artifact.insight_cards.length === 0) {
    return <div className="insight quiet"><p>{artifact.insights.absence_reason ?? artifact.insights.text}</p></div>;
  }
  return <div className="insights">
    {artifact.insight_cards.map((card) => <article key={cardKey(card)} className="insight">
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

function RetailSales({ artifact, notAvailable }: { readonly artifact: NetworkScorecardArtifact; readonly notAvailable: string }) {
  const headingId = useId();
  if (artifact.tacos.length === 0 && artifact.cross_network_spend.length === 0) return null;
  return <section className="section" aria-labelledby={headingId}>
    <h2 id={headingId}>{artifact.retail_sales_heading}</h2>
    <div className="tiles two">
      {artifact.tacos.map((row) => <Tile key={row.coverage_slot} figure={row.figure} notAvailable={notAvailable} slot={row.coverage_slot} market={row.marketplace_text}>
        {row.link === null ? null : <a className="chip-link" href={row.link.href} aria-label={row.link.label}>{row.link.text}</a>}
      </Tile>)}
      {artifact.cross_network_spend.map((row) => <div key={row.row_id} className={row.figure === null ? 'tile na' : 'tile'}>
        <span className="k">{row.label}</span>
        {row.marketplace_text === null ? null : <span className="market">{row.marketplace_text}</span>}
        <span className="v num">{row.figure === null ? row.text : figureText(row.figure)}</span>
        {row.reason === null ? null : <span className="reason">{row.reason}</span>}
        {row.figure === null ? null : <span className="basis">{row.figure.basis}</span>}
      </div>)}
    </div>
  </section>;
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

export function ScorecardView({ artifact, highlight = null }: {
  readonly artifact: NetworkScorecardArtifact;
  /** The channel a campaign's step asks to outline; only a served channel is honoured. */
  readonly highlight?: string | null;
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
      {artifact.cards.map((card) => <NetworkCardSection key={card.card_id} card={card} highlight={highlightFor(card)} notAvailable={notAvailable} several={several} />)}
      <section className="section" aria-labelledby={insightsHeading}>
        <h2 id={insightsHeading}>{artifact.insights_heading}</h2>
        <InsightCards artifact={artifact} onRead={openInsight} />
      </section>
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
        <h2 id={channelHeading}>{artifact.cards[0]?.channel_table.heading}</h2>
        {artifact.cards.map((card) => <ChannelTable key={card.card_id} card={card} highlight={highlightFor(card)} notAvailable={notAvailable} several={several} />)}
        <Pointers artifact={artifact} />
      </section>
      <RetailSales artifact={artifact} notAvailable={notAvailable} />
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
