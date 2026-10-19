// SPDX-License-Identifier: MIT-0
'use client';
/**
 * New customers (Customers › New customers, docs/ui/new-customers): new-to-brand purchases per
 * channel, new buyers for complete weeks and months, and gateway products one channel at a time.
 *
 * Every text is a server field and every figure a served value: nothing here adds, divides,
 * converts or ranks. No figure spans channels, so no table has a total or subtotal row: one
 * purchase or shopper can be credited to more than one channel (ADR-106). With two marketplaces
 * every section repeats once per marketplace, never one table across two.
 */
import { Fragment, useEffect, useId, useRef, useState } from 'react';

import { ChangeChip } from './ChartPrimitives';
import { CoverageProvider, SeeWhy } from './Coverage';
import { DataTable, DtBody, DtCell, DtColumn, DtHead, DtRow, DtRowHeader } from './DataTable';
import { InsightFocusDialog, type InsightDialogSlot, type InsightFocusSurface } from './InsightFocusDialog';
import { usePresetStep, useVisibleTabs } from './NextStepContext';
import { coverageEntry, type EvidenceStatusV2 } from '@/lib/evidence-status';
import { formatCount, formatCurrency } from '@/lib/exact-format';
import type {
  GatewayEntry,
  NewCustomersArtifact,
  NewCustomersFigure,
  NewCustomersInsightCard,
  NewCustomersMarket,
  NewCustomersPageText,
  PerChannel,
  StepOneRow,
} from '@/lib/new-customers';
import { bindingByRoute, isCalendarPreset } from '@/lib/page-bindings';

/** A served figure's value text: a count grouped, money from its two-place display, a share as served. */
export function newCustomersFigureText(figure: NewCustomersFigure): string | null {
  if (figure.value === null) return null;
  if (figure.kind === 'count') return formatCount(figure.value);
  if (figure.kind === 'currency' && figure.display_value !== null && figure.currency_code !== null) {
    return formatCurrency(figure.display_value, figure.currency_code);
  }
  return figure.display_text;
}

/** A cell or tile value: the figure, or "Not available" with the server's reason under it. */
function FigureValue({ figure, notAvailable }: { readonly figure: NewCustomersFigure; readonly notAvailable: string }) {
  const value = newCustomersFigureText(figure);
  if (value !== null) return <>{value}</>;
  // "Not available over hiding": the word and the server's reason, both visible.
  return <span className="cell-na">{notAvailable}<span className="reason">{figure.absence?.text}</span></span>;
}

function marketName(market: NewCustomersMarket, label: string): string {
  return market.marketplace_text === null ? label : `${label} · ${market.marketplace_text}`;
}

/**
 * With two marketplaces, each market's block opens with its served marketplace line, which also
 * leads the name of every table in it, so no two regions share a name (axe `landmark-unique`).
 */
function MarketHead({ id, market }: { readonly id: string; readonly market: NewCustomersMarket }) {
  return market.marketplace_text === null ? null : <h3 id={id} className="market-head">{market.marketplace_text}</h3>;
}

function leadFor(market: NewCustomersMarket, id: string): string | undefined {
  return market.marketplace_text === null ? undefined : id;
}

function StepOneTable({ market, text }: { readonly market: NewCustomersMarket; readonly text: NewCustomersPageText }) {
  const lead = useId();
  const table = market.new_to_brand;
  // A calendar period has no comparison, so the change column isn't drawn (LLD §6, N4).
  const compared = table.rows.some((row) => row.ntb_revenue_share_change.absence?.reason !== 'calendar_period_selected');
  const columns = table.columns.filter((column) => compared || column.column !== 'ntb_revenue_share_change');
  return <>
    <MarketHead id={lead} market={market} />
    <DataTable title={table.caption} note={table.caption_detail} className="fixed-first" nameLeadId={leadFor(market, lead)}>
      <DtHead>
        <DtRow>
          <DtColumn className="l">{text.channel_header}</DtColumn>
          {columns.map((column) => <DtColumn key={column.column} basis={column.column === 'ntb_revenue_share' ? table.share_basis : null}>{column.label}</DtColumn>)}
        </DtRow>
      </DtHead>
      <DtBody>
        {table.rows.map((row: StepOneRow) => <DtRow key={row.ad_product_key}>
          <DtRowHeader>{row.ad_product_label}<span className="sub">{row.channel_group_label}</span></DtRowHeader>
          {columns.map((column) => {
            if (column.column === 'ntb_revenue_share_change') {
              const change = row.ntb_revenue_share_change;
              return <DtCell key={column.column} label={column.label} absent={change.change === null}>
                {change.change === null
                  ? <span className="cell-na">{text.not_available_text}<span className="reason">{change.absence?.text}</span></span>
                  : <ChangeChip change={change.change} />}
              </DtCell>;
            }
            const figure = row[column.column];
            const share = column.column === 'ntb_revenue_share';
            return <DtCell
              key={column.column}
              label={column.label}
              absent={figure.value === null}
              {...(share ? { basis: { coverage: null, text: table.share_basis } } : {})}
            >
              <FigureValue figure={figure} notAvailable={text.not_available_text} />
            </DtCell>;
          })}
        </DtRow>)}
      </DtBody>
    </DataTable>
    <p className="dt-foot">{table.foot}</p>
  </>;
}

/** Arrival focus after "Show Latest measured week": the New buyers heading (plan D16). */
let focusNewBuyersOnArrival = false;

/**
 * Settle a load for the arrival focus: one that failed, or that brought no calendar body, drops
 * it, so a later preset selection never moves focus unasked (NC-GATE-106). A calendar body keeps
 * it for its view to take.
 */
export function settleNewBuyersArrival(calendarBody: boolean): void {
  if (!calendarBody) focusNewBuyersOnArrival = false;
}

function NewBuyersSection({ market, status, text }: {
  readonly market: NewCustomersMarket;
  readonly status: EvidenceStatusV2 | null;
  readonly text: NewCustomersPageText;
}) {
  const lead = useId();
  const section = market.new_buyers;
  const presetStep = usePresetStep();
  const select = section.action === null ? null : presetStep?.(section.action.preset) ?? null;
  const title = marketName(market, text.new_buyers_heading);
  if (section.state === 'outside_declared_periods') {
    return <div className="callout">
      <MarketHead id={lead} market={market} />
      <p>{section.text}</p>
      {section.action === null || select === null ? null : <div className="acts">
        <button
          type="button"
          className="btn"
          onClick={() => {
            focusNewBuyersOnArrival = true;
            select();
          }}
        >{section.action.label}</button>
      </div>}
    </div>;
  }
  if (section.state === 'pending' || section.state === 'missing') {
    // The tiles' delivery hasn't landed: its line, no table (plan D15), and the coverage sheet.
    return <>
      <MarketHead id={lead} market={market} />
      <p className="section-line">{section.text} <SeeWhy figure={title} /></p>
    </>;
  }
  const buyers = section.new_buyers;
  const cost = section.cost_per_new_buyer;
  if (buyers === null || cost === null) return null;
  return <div>
    <MarketHead id={lead} market={market} />
    <div className="tiles two">
      <div className={buyers.value === null ? 'tile na' : 'tile'}>
        <span className="k">{text.new_buyers_label}</span>
        <span className="v num"><FigureValue figure={buyers} notAvailable={text.not_available_text} /></span>
        <span className="basis">{section.new_buyers_basis}</span>
      </div>
      <div className={cost.value === null ? 'tile na' : 'tile'}>
        <span className="k">{text.cost_per_new_buyer_label}</span>
        <span className="v num"><FigureValue figure={cost} notAvailable={text.not_available_text} /></span>
        <span className="basis">{section.cost_basis}</span>
      </div>
    </div>
    {section.per_channel === null ? null : <PerChannelTable
      table={section.per_channel}
      text={text}
      leadId={leadFor(market, lead)}
      why={coverageEntry(status, `new-buyers-per-channel:${market.marketplace}`)?.label ?? marketName(market, text.new_buyers_label)}
    />}
  </div>;
}

function PerChannelTable({ leadId, table, text, why }: {
  readonly leadId: string | undefined;
  readonly table: PerChannel;
  readonly text: NewCustomersPageText;
  /** "See why" names the coverage entry it explains, "New buyers per channel" (NC-GATE-105). */
  readonly why: string;
}) {
  if (table.state !== 'rows' || table.caption === null) {
    const line = <p className="section-line">{table.text}</p>;
    // Pending or missing has a coverage entry the sheet explains; no channels doesn't.
    return table.state === 'no_channels'
      ? line
      : <p className="section-line">{table.text} <SeeWhy figure={why} /></p>;
  }
  return <>
    <DataTable title={table.caption} note={table.caption_detail} className="fixed-first" nameLeadId={leadId}>
      <DtHead>
        <DtRow>
          <DtColumn className="l">{text.channel_header}</DtColumn>
          <DtColumn>{text.new_buyers_label}</DtColumn>
          <DtColumn basis={table.cost_basis}>{table.cost_header}</DtColumn>
        </DtRow>
      </DtHead>
      <DtBody>
        {table.rows.map((row) => <DtRow key={row.ad_product_key}>
          <DtRowHeader>{row.ad_product_label}<span className="sub">{row.channel_group_label}</span></DtRowHeader>
          <DtCell label={text.new_buyers_label} absent={row.new_buyers.value === null}>
            <FigureValue figure={row.new_buyers} notAvailable={text.not_available_text} />
          </DtCell>
          <DtCell label={table.cost_header ?? ''} absent={row.cost_per_new_buyer.value === null}>
            <FigureValue figure={row.cost_per_new_buyer} notAvailable={text.not_available_text} />
          </DtCell>
        </DtRow>)}
      </DtBody>
    </DataTable>
    <p className="dt-foot">{table.foot}</p>
    {table.note === null ? null : <p className="dt-foot">{table.note}</p>}
  </>;
}

function GatewayProducts({ market, text }: { readonly market: NewCustomersMarket; readonly text: NewCustomersPageText }) {
  const name = useId();
  const lead = useId();
  const [selected, setSelected] = useState(market.gateway[0]?.ad_product_key ?? '');
  const entry: GatewayEntry | undefined = market.gateway.find((item) => item.ad_product_key === selected) ?? market.gateway[0];
  if (entry === undefined) return null;
  // Every channel reads the same coverage text when the window isn't covered, so no selector.
  if (entry.state === 'not_covered') {
    return <>
      <MarketHead id={lead} market={market} />
      <p className="section-line">{entry.text} <SeeWhy figure={marketName(market, text.gateway_heading)} /></p>
    </>;
  }
  return <>
    <MarketHead id={lead} market={market} />
    {/* One channel at a time, switched in place with no request: never a cross-channel list. */}
    <fieldset className="datectl">
      <legend>{text.gateway_selector_label}</legend>
      <div className="seg compact">
        {market.gateway.map((item) => <label key={item.ad_product_key} className="seg-opt">
          <input
            type="radio"
            name={name}
            value={item.ad_product_key}
            checked={item.ad_product_key === entry.ad_product_key}
            onChange={() => setSelected(item.ad_product_key)}
          />
          <span className="l">{item.channel_label}</span>
        </label>)}
      </div>
    </fieldset>
    {entry.state === 'rows'
      ? <>
        <DataTable
          title={entry.caption_title}
          nameLeadId={leadFor(market, lead)}
          note={entry.captions.map((caption, index) => <Fragment key={caption}>{index === 0 ? null : ' '}<span>{caption}</span></Fragment>)}
        >
          <DtHead>
            <DtRow>
              <DtColumn className="l">{text.gateway_product_header}</DtColumn>
              <DtColumn>{entry.column_header}</DtColumn>
              <DtColumn>{text.gateway_share_header}</DtColumn>
            </DtRow>
          </DtHead>
          <DtBody>
            {entry.rows.map((row) => <DtRow key={row.asin}>
              <DtRowHeader>{row.title}{row.title === row.asin ? null : <span className="sub">{row.asin}</span>}</DtRowHeader>
              <DtCell label={entry.column_header}>{formatCount(String(row.ntb_purchases))}</DtCell>
              <DtCell label={text.gateway_share_header}>{row.share_text}</DtCell>
            </DtRow>)}
          </DtBody>
        </DataTable>
        {entry.more_text === null ? null : <p className="dt-foot">{entry.more_text}</p>}
      </>
      : <p className="section-line">{entry.text}</p>}
  </>;
}

function Pointers({ artifact }: { readonly artifact: NewCustomersArtifact }) {
  const tabs = useVisibleTabs();
  return <>
    {[...artifact.pointers].sort((left, right) => left.order - right.order).map((pointer) => {
      // A pointer line renders only when the session's manifest lists its tab (LLD §9).
      const tab = tabs.find((candidate) => candidate.tab_id === pointer.tab_id);
      // The link is the served href (LLD §9 names it); the manifest decides only visibility.
      return tab === undefined ? null : <p key={pointer.tab_id} className="pointer">{pointer.lead} <a href={pointer.href}>{pointer.link_text}</a>.</p>;
    })}
  </>;
}

function cardKey(card: NewCustomersInsightCard): string {
  return `${card.finding_id}:${card.marketplace}:${card.ad_product_key}`;
}

function InsightCards({ artifact, onRead }: {
  readonly artifact: NewCustomersArtifact;
  readonly onRead: (id: string, source: HTMLButtonElement) => void;
}) {
  if (artifact.insight_cards.length === 0) {
    return <div className="insight quiet"><p>{artifact.insights.absence_reason ?? artifact.insights.text}</p></div>;
  }
  return <div className="insights">
    {artifact.insight_cards.map((card) => <article key={cardKey(card)} className="insight">
      <span className="kind">{card.kind_label}{card.marketplace_text === null ? null : <> · <span className="market">{card.marketplace_text}</span></>}</span>
      <h3>{card.headline}</h3>
      <p>{card.body}</p>
      <div className="acts">
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

export function NewCustomersView({ artifact }: { readonly artifact: NewCustomersArtifact }) {
  const [insight, setInsight] = useState<string | null>(null);
  const insightOrigin = useRef<HTMLButtonElement | null>(null);
  const stepOneHeading = useId();
  const insightsHeading = useId();
  const buyersHeading = useId();
  const gatewayHeading = useId();
  const buyersRef = useRef<HTMLHeadingElement | null>(null);
  const calendar = artifact.prior_period === null;
  useEffect(() => {
    // The button that selected Latest measured week is gone; its section heading takes focus.
    if (!focusNewBuyersOnArrival || !calendar) return;
    focusNewBuyersOnArrival = false;
    buyersRef.current?.focus();
  }, [calendar]);
  if (artifact.outcome !== 'complete') {
    return <section className="callout" role="status">
      <h2>{bindingByRoute['new-customers'].display_name}</h2>
      <p>{artifact.outcome_text}</p>
    </section>;
  }
  const text = artifact.page_text;
  const stamps = artifact.insights;
  const dialogSlots: InsightDialogSlot[] = artifact.insight_cards.map((card): InsightDialogSlot => ({
    absence_class: null,
    absence_reason: null,
    artifact_revision: stamps.artifact_revision,
    // The served line is the dialog's provenance strip (LLD dialog), so it isn't shown twice.
    evidence_line: null,
    id: cardKey(card),
    next_step: null,
    output_class: 'perspective',
    prompt_version: stamps.prompt_version,
    state: 'filled',
    text: card.body,
  }));
  const titles = new Map(artifact.insight_cards.map((card) => [cardKey(card), card.title] as const));
  const insightSurface: InsightFocusSurface = {
    labelFor: (id) => titles.get(id) ?? '',
    pageId: 'new-customers',
    provenanceFor: () => stamps.evidence_line,
    servedLabels: true,
    slots: dialogSlots,
    window: artifact.period,
  };
  return <CoverageProvider status={artifact.evidence_status} subject={bindingByRoute['new-customers'].display_name}>
    <div className="new-customers">
      <p className="scope-band" aria-live="polite">{artifact.scope_text}</p>
      <section className="section" aria-labelledby={stepOneHeading}>
        <h2 id={stepOneHeading}>{artifact.markets[0]?.new_to_brand.heading}</h2>
        {artifact.markets.map((market) => <StepOneTable key={market.marketplace} market={market} text={text} />)}
      </section>
      <section className="section" aria-labelledby={insightsHeading}>
        <h2 id={insightsHeading}>{text.insights_heading}</h2>
        <InsightCards artifact={artifact} onRead={(id, source) => { insightOrigin.current = source; setInsight(id); }} />
      </section>
      <section className="card section" aria-labelledby={buyersHeading}>
        <h2 id={buyersHeading} ref={buyersRef} tabIndex={-1}>{text.new_buyers_heading}</h2>
        {artifact.markets.map((market) => <NewBuyersSection key={market.marketplace} market={market} status={artifact.evidence_status} text={text} />)}
      </section>
      <section className="card section" aria-labelledby={gatewayHeading}>
        <h2 id={gatewayHeading}>{text.gateway_heading}</h2>
        {artifact.markets.map((market) => <GatewayProducts key={market.marketplace} market={market} text={text} />)}
      </section>
      <Pointers artifact={artifact} />
      <details className="about">
        <summary>{text.boundary_heading}</summary>
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

/** The window control's place while the first response is on its way, as the Scorecard draws it. */
export function NewCustomersControlsSkeleton() {
  const presets = bindingByRoute['new-customers'].temporal_policy.candidate_presets;
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

/** Loading: skeletons in the order of the sections, and a status for assistive technology (LLD). */
export function NewCustomersSkeleton() {
  // The page's own layout, so the skeleton sections keep the gaps the loaded sections have.
  return <div className="new-customers" role="status" aria-label="Loading New customers">
    <p className="scope-band scope-skel" aria-hidden="true"><span className="skel h-line" /></p>
    <div className="section" aria-hidden="true"><span className="skel h-label" /><span className="skel h-chart" /></div>
    <div className="section" aria-hidden="true">
      <span className="skel h-label" />
      <div className="insights"><div className="insight"><span className="skel h-line" /><span className="skel h-line short" /></div></div>
    </div>
    <div className="card section" aria-hidden="true">
      <span className="skel h-label" />
      <div className="tiles two">{[0, 1].map((item) => <div className="tile static" key={item}><span className="skel h-label" /><span className="skel h-value" /><span className="skel h-line short" /></div>)}</div>
    </div>
    <div className="card section" aria-hidden="true"><span className="skel h-label" /><span className="skel h-chart" /></div>
    <p className="loading-note"><span className="spin" aria-hidden="true" />Loading New customers…</p>
  </div>;
}
