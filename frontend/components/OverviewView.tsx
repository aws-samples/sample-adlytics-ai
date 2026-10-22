// SPDX-License-Identifier: MIT-0
'use client';
/**
 * Overview › Growth check (ADR-151, `docs/ui/overview/README.md`): one screen over the served
 * 7-day Scorecard, Retail health, New customers and Campaigns presentations. Each band reads one
 * source and shows that source's own absence when it isn't served, so no band waits on another.
 * Every figure is the served value its drill-down tab shows; nothing here adds or recomputes one.
 */
import { useId, type ReactNode } from 'react';

import { AnswerSentence } from './AnswerSentence';
import { ChangeChip, ChartLegend, SeriesChart } from './ChartPrimitives';
import { CoverageProvider, TileAbsence, TileChange } from './Coverage';
import { useVisibleTabs } from './NextStepContext';
import { CAMPAIGN_ABSENCE_TITLE } from './CampaignRouteAdapter';
import {
  RETAIL_HEALTH_ABSENCE_TITLE,
  RETAIL_HEALTH_EMPTY_TEXT,
  RETAIL_HEALTH_INSUFFICIENT_TEXT,
} from './RetailHealthRouteAdapter';
import { trendSeries } from './RetailHealthView';
import { retailMetricLabel, retailValue } from './RetailTiles';
import { ScorecardTile } from './ScorecardView';
import { formatCurrency, formatIsoDate } from '@/lib/exact-format';
import { windowText } from '@/lib/answer';
import { overviewAnswer, overviewCard, OVERVIEW_PRESET, throughLines, type OverviewSource } from '@/lib/overview';
import type {
  CampaignParentPresentation,
  NewCustomersPresentation,
  RetailHealthPresentation,
  ScorecardPresentation,
} from '@/lib/presentation-v2';
import type { NewCustomersFigure } from '@/lib/new-customers';
import type { RetailHealthArtifact } from '@/lib/retail-health';
import type { NetworkScorecardArtifact } from '@/lib/scorecard';

/** The four served sources, each settled on its own (plan 2.2). */
export interface OverviewSources {
  readonly campaigns: OverviewSource<CampaignParentPresentation>;
  readonly newCustomers: OverviewSource<NewCustomersPresentation>;
  readonly retail: OverviewSource<RetailHealthPresentation>;
  readonly scorecard: OverviewSource<ScorecardPresentation>;
}

/** ABS-OV-1 (`docs/ui/page-absence-copy.md`): a source that didn't load, named by its tab. */
export function sourceIssueText(tab: string): string {
  return `This part comes from ${tab}, which can't be shown right now. Try again later.`;
}

/** ABS-OV-2: a served Campaigns body with no portfolio trend. */
export const OVERVIEW_NO_SPEND_TREND = 'This window has no daily ad spend to draw.';

/** A band's absence: the source's served or catalog words, and the tab that owns it. */
function BandAbsence({ children, link }: { readonly children: ReactNode; readonly link: ReactNode }) {
  return <div className="insight quiet ov-absence" role="status"><p>{children}</p>{link}</div>;
}

/** A link to a tab this session's manifest lists, with an optional query; never a dead link. */
function TabLink({ children, query = '', tabId }: { readonly children: ReactNode; readonly query?: string; readonly tabId: string }) {
  const tab = useVisibleTabs().find((candidate) => candidate.tab_id === tabId);
  return tab === undefined ? null : <a className="ov-link" href={`${tab.route}${query}`}>{children}</a>;
}

/** The served 7-day query the Overview's links carry, so each drill-down shows the same window. */
const PRESET_QUERY = `?preset=${OVERVIEW_PRESET}`;

type ServedScorecard = Extract<ScorecardPresentation, { readonly kind: 'parent' }>;

function scorecardBody(source: OverviewSource<ScorecardPresentation>): NetworkScorecardArtifact | null {
  return source.kind === 'served' && source.presentation.kind === 'parent' && source.presentation.artifact.outcome === 'complete'
    ? source.presentation.artifact
    : null;
}

function retailBody(source: OverviewSource<RetailHealthPresentation>): RetailHealthArtifact | null {
  return source.kind === 'served' && source.presentation.kind === 'parent' && source.presentation.artifact.outcome === 'complete'
    ? source.presentation.artifact
    : null;
}

/** Why the Scorecard band is empty: the served window message, the served outcome, or ABS-OV-1. */
function scorecardAbsence(source: OverviewSource<ScorecardPresentation>): string {
  if (source.kind === 'rejected') return sourceIssueText('the Scorecard');
  const presentation = source.presentation;
  if (presentation.kind === 'window_unavailable') return presentation.message;
  return presentation.artifact.outcome_text ?? presentation.artifact.not_available_text;
}

function retailAbsence(source: OverviewSource<RetailHealthPresentation>): string {
  if (source.kind === 'rejected') return sourceIssueText('Retail health');
  const presentation: RetailHealthPresentation = source.presentation;
  if (presentation.kind === 'window_unavailable') return `${RETAIL_HEALTH_ABSENCE_TITLE}.`;
  return presentation.artifact.outcome === 'empty' ? RETAIL_HEALTH_EMPTY_TEXT : RETAIL_HEALTH_INSUFFICIENT_TEXT;
}

/** The shipped-revenue tile: the served total, its prior under the prior window, its change. */
function RetailRevenueTile({ artifact }: { readonly artifact: RetailHealthArtifact }) {
  const revenue = artifact.totals.find((entry) => entry.metric === 'retail_revenue_shipped');
  const label = retailMetricLabel('retail_revenue_shipped');
  // A complete body always serves shipped revenue (it is never a declared absence).
  if (revenue === undefined) return null;
  return <div className="tile" data-slot="ov-tile-retail">
    <span className="k">{label}</span>
    <span className="v num">{retailValue('retail_revenue_shipped', revenue.current)}</span>
    <span className="prior">{windowText(artifact.prior_period)}: {retailValue('retail_revenue_shipped', revenue.prior)}</span>
    <span className="basis">Vendor Central · shipped revenue</span>
    <TileAbsence figure={label} slot="retail_revenue_shipped" />
    <div className="foot"><TileChange change={revenue.change} slot="retail_revenue_shipped" /></div>
  </div>;
}

function Tiles({ retail, scorecard }: { readonly retail: OverviewSources['retail']; readonly scorecard: OverviewSources['scorecard'] }) {
  const sc = scorecardBody(scorecard);
  const card = sc === null ? null : overviewCard(sc);
  const rh = retailBody(retail);
  const model = card?.headline.models.find((entry) => entry.model === card.headline.default_model) ?? null;
  const tacos = sc === null || card === null ? null : sc.tacos.find((row) => row.card_id === card.card_id) ?? null;
  return <div className="tiles ov-tiles">
    {sc === null || card === null
      ? <BandAbsence link={<TabLink tabId="scorecard" query={PRESET_QUERY}>Open Scorecard ›</TabLink>}>{sc === null ? scorecardAbsence(scorecard) : sc.scope_text}</BandAbsence>
      : <CoverageProvider status={sc.evidence_status} subject="Scorecard">
        <ScorecardTile figure={card.spend} notAvailable={sc.not_available_text} slot={`spend:${card.card_id}`} />
        {model === null ? null : <ScorecardTile figure={model.figure} notAvailable={sc.not_available_text} slot={`headline:${card.card_id}`} />}
      </CoverageProvider>}
    {rh === null
      ? <BandAbsence link={<TabLink tabId="retail-health">Open Retail health ›</TabLink>}>{retailAbsence(retail)}</BandAbsence>
      : <CoverageProvider status={rh.evidence_status} subject="Retail health"><RetailRevenueTile artifact={rh} /></CoverageProvider>}
    {sc === null || tacos === null ? null : <CoverageProvider status={sc.evidence_status} subject="Scorecard">
      <ScorecardTile figure={tacos.figure} notAvailable={sc.not_available_text} slot={tacos.coverage_slot} />
    </CoverageProvider>}
  </div>;
}

function SpendChart({ source }: { readonly source: OverviewSources['campaigns'] }) {
  const headingId = useId();
  const served = source.kind === 'served' && source.presentation.kind === 'parent' ? source.presentation.artifact : null;
  const trend = served?.trend ?? null;
  return <section className="panel ov-chart" aria-labelledby={headingId} data-slot="ov-chart-spend">
    <h3 id={headingId}>Daily ad spend</h3>
    {trend === null
      ? <p className="small dim">{source.kind === 'rejected'
        ? sourceIssueText('Campaigns')
        : served === null ? `${CAMPAIGN_ABSENCE_TITLE}.` : OVERVIEW_NO_SPEND_TREND}</p>
      : <>
        <p className="footnote">{windowText(trend.period)} · all channels</p>
        <SeriesChart
          ariaLabel="Daily ad spend, all channels"
          currencyCode={trend.units.spend.currency_code}
          current={trend.points.map((point) => point.spend)}
          dates={trend.points.map((point) => point.date)}
          kind="currency"
          zeroBased
        />
        <ChartLegend prior={false} />
      </>}
  </section>;
}

function RetailChart({ source }: { readonly source: OverviewSources['retail'] }) {
  const headingId = useId();
  const rh = retailBody(source);
  return <section className="panel ov-chart" aria-labelledby={headingId} data-slot="ov-chart-retail">
    <h3 id={headingId}>Daily shipped sales</h3>
    {rh === null || rh.trend.length === 0
      ? <p className="small dim">{retailAbsence(source)}</p>
      : <>
        <p className="footnote">{windowText(rh.period)} · prior {windowText(rh.prior_period)} dashed</p>
        <SeriesChart
          ariaLabel="Daily shipped sales, with the prior window"
          currencyCode={rh.trend[0]?.current[0]?.units.currency_code ?? null}
          current={trendSeries(rh, 'retail_revenue_shipped', 'current')}
          dates={rh.trend.map((point) => point.date)}
          kind="currency"
          prior={trendSeries(rh, 'retail_revenue_shipped', 'prior')}
          zeroBased
        />
        <ChartLegend prior />
      </>}
  </section>;
}

/** "16.62 USD per first-time purchase" from the served cost, or its served absence. */
export function costText(cost: NewCustomersFigure, notAvailable: string): string {
  if (cost.display_value !== null && cost.currency_code !== null) return `${formatCurrency(cost.display_value, cost.currency_code)} per first-time purchase`;
  return cost.absence?.text ?? notAvailable;
}

function NewCustomersBand({ source }: { readonly source: OverviewSources['newCustomers'] }) {
  const headingId = useId();
  const served = source.kind === 'served' && source.presentation.kind === 'parent' ? source.presentation.artifact : null;
  const market = served !== null && served.outcome === 'complete' && served.markets.length === 1 ? served.markets[0] ?? null : null;
  let absence: string | null = null;
  if (source.kind === 'rejected') absence = sourceIssueText('New customers');
  else if (source.presentation.kind === 'window_unavailable') absence = source.presentation.message;
  else if (market === null) absence = source.presentation.artifact.outcome_text ?? source.presentation.artifact.scope_text;
  return <section className="card section" aria-labelledby={headingId}>
    <h2 id={headingId}>New customers by channel</h2>
    {market === null
      ? <p className="small dim" role="status">{absence}</p>
      : <>
        <p className="small dim">Share of ad-credited sales from first-time buyers ({market.new_to_brand.share_basis}). Not added across channels.</p>
        <ul className="ov-ntb">
          {market.new_to_brand.rows.map((row) => <li key={row.ad_product_key} data-slot={`ov-ntb-${row.ad_product_key}`}>
            <span>{row.ad_product_label}</span>
            <span className="ov-ntb-v">
              <strong className="num">{row.ntb_revenue_share.display_text ?? row.ntb_revenue_share.absence?.text ?? served?.page_text.not_available_text}</strong>
              {row.ntb_revenue_share_change.change === null ? null : <ChangeChip change={row.ntb_revenue_share_change.change} />}
              <span className="ov-ntb-cost" data-slot={`ov-ntb-${row.ad_product_key}-cost`}>{costText(row.cost_per_ntb_purchase, served?.page_text.not_available_text ?? '')}</span>
            </span>
          </li>)}
        </ul>
      </>}
    <p className="ov-links">
      <TabLink tabId="new-customers" query={PRESET_QUERY}>See new customers ›</TabLink>
      <TabLink tabId="scorecard" query={PRESET_QUERY}>See each channel ›</TabLink>
    </p>
  </section>;
}

function WhatChanged({ retail, scorecard }: { readonly retail: OverviewSources['retail']; readonly scorecard: OverviewSources['scorecard'] }) {
  const headingId = useId();
  const sc = scorecardBody(scorecard);
  const rh = retailBody(retail);
  // Served findings only, in served order: up to two Scorecard cards and the retail movers card.
  const cards = (sc?.insight_cards ?? []).slice(0, 2);
  const movers = rh?.insights.find((slot) => slot.id === 'retail-movers' && slot.state === 'filled') ?? null;
  if (cards.length === 0 && movers === null) return null;
  return <section className="card section" aria-labelledby={headingId}>
    <h2 id={headingId}>What changed</h2>
    <div className="insights">
      {cards.map((card) => <article key={`${card.finding_id}:${card.channel}`} className="insight" data-slot="ov-changed">
        <span className="kind">{card.kind_label} · Scorecard</span>
        <h3>{card.headline}</h3>
        <TabLink tabId="scorecard" query={PRESET_QUERY}>Open Scorecard ›</TabLink>
      </article>)}
      {movers === null ? null : <article className="insight" data-slot="ov-changed">
        <span className="kind">Observed · Retail health</span>
        <h3>{movers.text}</h3>
        <TabLink tabId="retail-health">Open Retail health ›</TabLink>
      </article>}
    </div>
  </section>;
}

function Trust({ sources }: { readonly sources: OverviewSources }) {
  const headingId = useId();
  const sc = sources.scorecard.kind === 'served' && sources.scorecard.presentation.kind === 'parent' ? sources.scorecard.presentation : null;
  const rh = sources.retail.kind === 'served' && sources.retail.presentation.kind === 'parent' ? sources.retail.presentation : null;
  const nc = sources.newCustomers.kind === 'served' && sources.newCustomers.presentation.kind === 'parent' ? sources.newCustomers.presentation : null;
  const cp = sources.campaigns.kind === 'served' && sources.campaigns.presentation.kind === 'parent' ? sources.campaigns.presentation : null;
  // Each source's own served window end; sources that end on the same day share one line.
  const lines = throughLines([
    ...(sc === null ? [] : [{ end: sc.artifact.period.end, name: 'ad' }]),
    ...(cp === null ? [] : [{ end: cp.artifact.period.end, name: 'campaign' }]),
    ...(rh === null ? [] : [{ end: rh.artifact.period.end, name: 'retail' }]),
    ...(nc === null ? [] : [{ end: nc.artifact.period.end, name: 'new-customer' }]),
  ]);
  const reconciliation = rh?.artifact.reconciliation ?? null;
  const boundary = [...(sc?.artifact.boundary ?? []), ...(rh === null ? [] : [rh.artifact.boundary])];
  return <section className="card section" aria-labelledby={headingId}>
    <h2 id={headingId}>Can you rely on these?</h2>
    <ul className="ov-trust">
      {reconciliation === null ? null : <li className={reconciliation.outcome === 'reconciled' ? 'ok' : undefined} data-slot="ov-trust-reconciled">{reconciliation.outcome === 'reconciled'
        ? `Retail matches Vendor Central totals · loaded ${formatIsoDate(reconciliation.as_of)}`
        : `Retail totals differ from reported · loaded ${formatIsoDate(reconciliation.as_of)}`}</li>}
      {lines.map((line) => <li key={line.end} className="ok" data-slot="ov-trust-through">{capitalised(listed(line.names))} data through {formatIsoDate(line.end)}</li>)}
    </ul>
    {boundary.length === 0 ? null : <details className="about">
      <summary>About these figures</summary>
      <div className="body">{boundary.map((line) => <p key={line}>{line}</p>)}</div>
    </details>}
  </section>;
}

function listed(names: readonly string[]): string {
  if (names.length < 2) return names.join('');
  return `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`;
}

function capitalised(text: string): string {
  return text.length === 0 ? text : `${text[0]?.toUpperCase()}${text.slice(1)}`;
}

export function OverviewView({ sources }: { readonly sources: OverviewSources }) {
  const sc = scorecardBody(sources.scorecard);
  const rh = retailBody(sources.retail);
  return <div className="scorecard overview">
    <section className="card section">
      <h2 className="ov-question">Is your advertising growing your business?</h2>
      <AnswerSentence answer={overviewAnswer(sc, rh)} />
      <Tiles retail={sources.retail} scorecard={sources.scorecard} />
      <div className="ov-charts">
        <SpendChart source={sources.campaigns} />
        <RetailChart source={sources.retail} />
      </div>
      {sc === null || rh === null ? null : <p className="small dim">Ad days are UTC and retail days are Pacific, so compare the shape, not single days.</p>}
    </section>
    <NewCustomersBand source={sources.newCustomers} />
    <WhatChanged retail={sources.retail} scorecard={sources.scorecard} />
    <nav className="ov-pointers" aria-label="Where to look next">
      <TabLink tabId="reach">Who your ads reached ›</TabLink>
      <TabLink tabId="retention">Do new customers come back? ›</TabLink>
    </nav>
    <Trust sources={sources} />
  </div>;
}

/** The fixed window label beside the h1: the Scorecard's served window and prior, or nothing. */
export function OverviewBandTitle({ scorecard }: { readonly scorecard: ServedScorecard | null }) {
  if (scorecard === null) return null;
  const prior = scorecard.artifact.prior_period;
  return <div className="scopes">
    <span className="pill" data-slot="ov-scope">7 days · {windowText(scorecard.artifact.period)}{prior === null ? '' : ` vs ${windowText(prior)}`}</span>
  </div>;
}

export function OverviewSkeleton() {
  return <div role="status" aria-label="Loading Overview">
    <div className="card section" aria-hidden="true">
      <span className="skel h-label" />
      <div className="tiles">{[0, 1, 2, 3].map((item) => <div className="tile static" key={item}><span className="skel h-label" /><span className="skel h-value" /></div>)}</div>
      <span className="skel h-chart" />
    </div>
    <p className="loading-note"><span className="spin" aria-hidden="true" />Loading Overview…</p>
  </div>;
}
