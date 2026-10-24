// SPDX-License-Identifier: MIT-0
'use client';

import { campaignsAnswer } from '@/lib/campaign-attribution';
import { AnswerSentence } from './AnswerSentence';
import { useEffect, useMemo, useRef, useState, type MouseEvent, type RefObject } from 'react';

import type { Change } from '@/lib/change';
import { campaignDeliverySlots } from '@/lib/campaign-attribution';
import type {
  CampaignAttributionDetailV2,
  CampaignChannelFilter,
  CampaignChannelView,
  CampaignDeliverySlot,
  CampaignInsightSlot,
  CampaignMetricValue,
  CampaignPortfolioArtifact,
  CampaignPortfolioComparison,
  CampaignPortfolioMetricValue,
  CampaignPortfolioRow,
  CampaignSelectionCapability,
} from '@/lib/campaign-attribution';
import { formatCount, formatIsoDate, formatIsoDateRange } from '@/lib/exact-format';
import type { InsightNextStep, InsightNextStepHandlers } from '@/lib/insight-next-step';
import type { MetricFocusV4 } from '@/lib/metric-focus';
import { bindingByRoute } from '@/lib/page-bindings';
import { isCapabilityExpired, type PresentationErrorClass } from '@/lib/presentation';
import {
  campaignFocusMetrics,
  fetchCampaignMetricFocus,
  fetchCampaignPresentationV2,
  fetchInsightDeepDive,
  fetchPortfolioMetricFocus,
  multiChannelFocusMetrics,
  portfolioFocusMetrics,
  type CampaignFocusMetric,
  type CampaignPortfolioSort,
  type PortfolioFocusMetric,
  type TemporalSelectionV1,
} from '@/lib/presentation-v2';
import { useOpenEntity } from './AppShell';
import { DataTable, DtBody, DtCell, DtColumn, DtHead, DtRow, DtRowHeader } from './DataTable';
import { PAGE_HEADING_ID } from './Dialog';
import { ChangeChip, ChartLegend, displayValueText, numberText, SeriesChart, type ValueKind } from './ChartPrimitives';
import { seedAssistantQuestion } from './ChatPanel';
import { CoverageBasis, CoverageProvider, FigureChange, SeeWhy, TileAbsence, TileChange, useCoverageText } from './Coverage';
import { EvidenceStatusSheet } from './EvidenceStatusSheet';
import { InsightCard, InsightNextStepControl } from './InsightCard';
import { InsightFocusDialog, type InsightFocusSurface } from './InsightFocusDialog';
import { MetricFocusDialog, type MetricFocusSurface } from './MetricFocusDialog';

// Re-exported so existing imports of the shared primitives keep resolving.
export { ChangeChip, ChartLegend, numberText, SeriesChart, Spark } from './ChartPrimitives';
export type { ValueKind } from './ChartPrimitives';

export type CampaignSelectionState =
  | { readonly kind: 'idle'; readonly choice: null }
  | { readonly kind: 'loading'; readonly choice: CampaignSelectionCapability }
  | { readonly kind: 'unavailable'; readonly choice: CampaignSelectionCapability | null; readonly issueClass: PresentationErrorClass };

const deliveryMetrics = campaignDeliverySlots;

function isDeliveryMetric(metric: string): metric is CampaignDeliverySlot {
  return campaignDeliverySlots.includes(metric as CampaignDeliverySlot);
}

/** The served label of the option this body renders (the decoder checks it is offered). */
function selectedLabel(filter: CampaignChannelFilter): string {
  return filter.options.find((option) => option.filter_id === filter.selected)?.label ?? filter.selected;
}

function campaignName(row: Pick<CampaignPortfolioRow, 'signature'>): string {
  return row.signature.effective_name ?? 'Name unavailable';
}

/** A campaign slot's governed metric id, for its dictionary display rule (plan D4). */
function governedMetric(slot: string): string {
  if (slot === 'spend') return 'ad_spend';
  if (slot === 'attributed_roas') return 'roas_attributed_14d';
  return slot;
}
function metric(value: CampaignMetricValue | null, kind: ValueKind, slot?: string): string {
  return value === null
    ? 'Not available for this reporting window'
    : displayValueText(value, kind, slot === undefined ? undefined : governedMetric(slot));
}

/** Why a portfolio ratio is typed absent: its denominator is zero in the window (ADR-087). */
const undefinedRatioReason: Partial<Record<CampaignPortfolioComparison['metric'] | 'cpc' | 'cpm' | 'clicks' | 'viewable_rate', string>> = {
  attributed_roas: 'no spend',
  cpc: 'no clicks',
  cpm: 'no impressions',
  ctr: 'no impressions',
  portfolio_roas_attributed_14d: 'no spend',
};

/** A served portfolio value; a typed-absent ratio renders an em dash with its accessible reason. */
export function PortfolioValue({ metric: metricName, value }: {
  readonly metric: CampaignPortfolioComparison['metric'];
  readonly value: CampaignPortfolioMetricValue;
}) {
  if (value.value === null) {
    const reason = undefinedRatioReason[metricName] ?? 'no denominator';
    const description = `Not applicable: ${reason} in this window`;
    return <span className="not-applicable" role="img" aria-label={description} title={description}>—</span>;
  }
  return <>{displayValueText({ ...value, value: value.value }, value.units.kind, governedMetric(metricName))}</>;
}

/** UX sweep fix 6: the one line under All channels in place of the "Not added" tiles. */
export const CAMPAIGNS_PICK_CHANNEL = 'Pick a channel to see its sales and return.';
/** UI refresh 3.2: the delivery-per-channel disclosure's name. */
export const DELIVERY_SUMMARY = 'Delivery per channel';
/** UX sweep fix 6: the table's decision hint; the change itself happens in Amazon Ads. */
export const CAMPAIGNS_SORT_HINT = 'Sort by Attributed ROAS to find campaigns to fix; you make the change in Amazon Ads.';

/**
 * One freshness line for the campaign table (cp-page-fresh, UX sweep fix 6), not a badge per row:
 * "Latest data {date}" when no row carries a served staleness qualifier, else each distinct served
 * qualifier once, with "See why" opening the page's one coverage sheet.
 */
function PageFreshness({ end, qualifiers }: { readonly end: string; readonly qualifiers: readonly string[] }) {
  const distinct = [...new Set(qualifiers)];
  if (distinct.length === 0) return <p className="footnote" data-slot="cp-page-fresh"><span className="pill good">Latest data {formatIsoDate(end)}</span></p>;
  return <p className="footnote" data-slot="cp-page-fresh"><span className="pill warn">Late data</span> {distinct.join(' · ')} <SeeWhy figure="campaign freshness" /></p>;
}
/** Whether both sides are defined; an undefined comparison has no change (ADR-087). */
function comparable(comparison: CampaignPortfolioComparison): boolean {
  return comparison.current.value !== null && comparison.prior.value !== null;
}
/**
 * The change chip; an undefined comparison has no change, so nothing renders (ADR-087). A
 * withheld change reads the coverage block's absence text with "See why: {figure}".
 */
function PortfolioChange({ comparison, figure }: { readonly comparison: CampaignPortfolioComparison; readonly figure?: string }) {
  if (!comparable(comparison)) return null;
  return <FigureChange change={comparison.change} slot={comparison.metric} {...(figure === undefined ? {} : { figure })} />;
}

function label(metricName: CampaignFocusMetric | CampaignPortfolioComparison['metric'] | CampaignDeliverySlot): string {
  return {
    ad_spend: 'Ad spend',
    attributed_roas: 'Attributed ROAS',
    clicks: 'Clicks',
    cpc: 'Cost per click',
    cpm: 'Cost per thousand impressions',
    ctr: 'Click-through rate',
    impressions: 'Impressions',
    portfolio_roas_attributed_14d: 'Portfolio ROAS',
    purchases: 'Attributed purchases',
    spend: 'Ad spend',
  }[metricName];
}

function valueKind(metricName: CampaignFocusMetric | CampaignDeliverySlot): ValueKind {
  if (metricName === 'purchases' || metricName === 'impressions' || metricName === 'clicks') return 'count';
  if (metricName === 'attributed_roas' || metricName === 'ctr') return 'ratio';
  return 'currency';
}

function issueCopy(issue: PresentationErrorClass): string {
  if (issue === 'validation') return 'This campaign choice has expired. The campaign list is refreshing.';
  if (issue === 'invariant') return 'This campaign could not be verified safely.';
  return 'Campaign detail is temporarily unavailable. Choose another campaign or try again shortly.';
}

function portfolioOutcomeCopy(outcome: CampaignPortfolioArtifact['outcome']): { readonly description: string; readonly status: string; readonly title: string } {
  if (outcome === 'complete') return {
    description: 'Select a server-authorized campaign to open its command center.',
    status: 'Portfolio ready',
    title: 'Campaign portfolio',
  };
  if (outcome === 'empty') return {
    description: 'No authorized campaigns are available for this reporting window. Choose another available window to review campaign activity.',
    status: 'No authorized campaigns',
    title: 'No campaigns for this reporting window',
  };
  return {
    description: 'Campaign evidence could not be verified for this reporting window. Choose another available window and try again.',
    status: 'Evidence unavailable',
    title: 'Campaign evidence needs another window',
  };
}

function insightTitle(id: CampaignInsightSlot['id']): string {
  return {
    contribution: 'Contribution',
    divergence: 'Divergence',
    exposure: 'Exposure',
    movers: 'Largest changes',
    'trend-shape': 'Trend shape',
    'efficiency-drift': 'Efficiency drift',
    funnel: 'Delivery and outcome',
    'next-check': 'Next check',
    'metric-focus': 'Why this moved',
  }[id];
}

function InsightRow({ campaignSegment, campaignSort, filterId, line, nextStepHandlers, period, selectionReference, sessionId, slots, surfaceKey, temporalSelection, title }: {
  /** The served (sort, segment) the portfolio cards were filled from (W11). */
  readonly campaignSegment?: number;
  /** The channel filter sibling the cards were filled from (ADR-127). */
  readonly filterId?: string;
  /** The server's one line about what these insights cover, under a multi-channel option. */
  readonly line?: string | null;
  readonly campaignSort?: CampaignPortfolioSort;
  /** What this surface's served next steps open (ADR-122); Campaigns is one tab. */
  readonly nextStepHandlers?: InsightNextStepHandlers;
  readonly period: { readonly end: string; readonly start: string };
  readonly selectionReference?: string | null;
  readonly sessionId: string;
  readonly slots: readonly CampaignInsightSlot[];
  readonly surfaceKey: string;
  readonly temporalSelection?: TemporalSelectionV1 | undefined;
  readonly title: string;
}) {
  const [focusId, setFocusId] = useState<CampaignInsightSlot['id'] | null>(null);
  const origin = useRef<HTMLButtonElement | null>(null);
  const closeFocus = () => setFocusId(null);
  const surface: InsightFocusSurface = {
    labelFor: (slotId) => insightTitle(slotId as CampaignInsightSlot['id']),
    deepFor: (slot) => {
      if (slot.id === 'movers' || (surfaceKey === 'campaign-detail' && !selectionReference)) return null;
      const cacheKey = `${surfaceKey}:${filterId ?? 'all'}:${slot.id}:${slot.artifact_revision}:${period.start}:${period.end}:${selectionReference ?? ''}`;
      return {
        cacheKey,
        expectedArtifactRevision: slot.artifact_revision,
        fetch: (signal) => fetchInsightDeepDive(
          selectionReference === undefined || selectionReference === null
            ? {
              session_id: sessionId,
              slot_id: slot.id,
              ...(temporalSelection === undefined ? {} : { temporal_selection: temporalSelection }),
              ...(campaignSort === undefined ? {} : { sort: campaignSort }),
              ...(campaignSegment === undefined ? {} : { segment: campaignSegment }),
              ...(filterId === undefined ? {} : { filter_id: filterId }),
            }
            : { session_id: sessionId, slot_id: slot.id, selection_reference: selectionReference },
          signal,
        ),
        window: period,
      };
    },
    slots,
    window: period,
  };
  const headingId = `${title.toLowerCase().replaceAll(/[^a-z]+/g, '-')}-heading`;
  return <section aria-labelledby={headingId}>
    <h2 className={line == null ? 'sr-only' : 'section-label'} id={headingId}>{title}</h2>
    {line == null ? null : <p className="section-line">{line}</p>}
    <div className="narr-row">
      {slots.map((slot) => <InsightCard
        key={slot.id}
        nextStepHandlers={nextStepHandlers}
        onOpen={(source) => { origin.current = source; setFocusId(slot.id); }}
        pageId="campaigns"
        slot={slot}
        title={insightTitle(slot.id)}
      />)}
    </div>
    {focusId !== null ? <InsightFocusDialog
      onClose={closeFocus}
      opener={origin}
      onSlotChange={(slotId) => setFocusId(slotId as CampaignInsightSlot['id'])}
      slotId={focusId}
      surface={surface}
      surfaceKey={surfaceKey}
    /> : null}
  </section>;
}

/* ---------- portfolio sections ---------- */

/**
 * Portfolio outcome tiles. Every tile opens the shared focus dialog; the assistant seed
 * lives inside the dialog's next-step block, never as a tile's primary action.
 */
/** The portfolio's one metric focus dialog, opened by a tile or by a served next step. */
interface PortfolioFocus {
  readonly metric: PortfolioFocusMetric | null;
  readonly open: (metric: PortfolioFocusMetric, origin: HTMLButtonElement) => void;
  readonly origin: RefObject<HTMLButtonElement | null>;
  readonly set: (metric: PortfolioFocusMetric | null) => void;
}

function usePortfolioFocus(): PortfolioFocus {
  const [metric, set] = useState<PortfolioFocusMetric | null>(null);
  const origin = useRef<HTMLButtonElement | null>(null);
  return { metric, open: (next, source) => { origin.current = source; set(next); }, origin, set };
}

function PortfolioTiles({ artifact, filterId, focus, sessionId, temporalSelection }: {
  readonly artifact: CampaignPortfolioArtifact;
  readonly filterId: string;
  readonly focus: PortfolioFocus;
  readonly sessionId: string;
  readonly temporalSelection: TemporalSelectionV1 | undefined;
}) {
  const focusMetric = focus.metric;
  const setFocusMetric = focus.set;
  const [focusCache] = useState(() => new Map<string, MetricFocusV4>());
  const origin = focus.origin;
  const closeFocus = () => setFocusMetric(null);
  const windowText = formatIsoDateRange(artifact.period.start, artifact.period.end);
  const surface: MetricFocusSurface = {
    cache: focusCache,
    pageId: 'campaigns',
    cacheScope: artifact.insights[0]?.artifact_revision ?? `window:${windowText}`,
    askSeed: (focus) => `Ask about ${focus.metric.label} for the portfolio from ${windowText}.`,
    fetch: (metricId, signal) => fetchPortfolioMetricFocus(sessionId, metricId as PortfolioFocusMetric, temporalSelection, signal, filterId),
    deepFor: (focus) => ({
      cacheKey: `campaign-portfolio:${filterId}:metric-focus:${focus.metric.id}:${focus.insight.artifact_revision}:${focus.period.start}:${focus.period.end}`,
      expectedArtifactRevision: focus.insight.artifact_revision,
      fetch: (signal) => fetchInsightDeepDive({
        session_id: sessionId,
        slot_id: 'metric-focus',
        metric: focus.metric.id,
        ...(temporalSelection === undefined ? {} : { temporal_selection: temporalSelection }),
        ...(filterId === 'all' ? {} : { filter_id: filterId }),
      }, signal),
      window: focus.period,
    }),
    labelFor: (metricId) => label(metricId as PortfolioFocusMetric),
    // Under a multi-channel option purchases and ROAS aren't focusable (ADR-127).
    neighbors: artifact.channel_view === null ? portfolioFocusMetrics : multiChannelFocusMetrics,
    window: artifact.period,
  };
  return <>
    <div className="tiles">
      {artifact.totals.map((comparison) => <article className="tile" key={comparison.metric}>
        <p className="k">{label(comparison.metric)}</p>
        <div className="row">
          <div>
            <p className="v"><PortfolioValue metric={comparison.metric} value={comparison.current} /></p>
            <p className="prior">{formatIsoDateRange(artifact.prior_period.start, artifact.prior_period.end)}: <PortfolioValue metric={comparison.metric} value={comparison.prior} /></p>
          </div>
          {comparable(comparison) ? <TileChange change={comparison.change} slot={comparison.metric} /> : null}
        </div>
        <CoverageBasis slot={comparison.metric} />
        {comparable(comparison) ? <TileAbsence figure={label(comparison.metric)} slot={comparison.metric} /> : null}
        <button
          type="button"
          className="ask"
          aria-haspopup="dialog"
          onClick={(event) => focus.open(comparison.metric as PortfolioFocusMetric, event.currentTarget)}
        >
          Open the focus view for {label(comparison.metric).toLowerCase()}
        </button>
      </article>)}
    </div>
    {/* UX sweep fix 6 (readout A.4): an attributed total is never added across channels, so one
        line replaces the served "Not added across channels" tiles. */}
    {(artifact.channel_view?.not_added ?? []).length === 0 ? null : <p className="section-line" data-slot="cp-pick-channel">{CAMPAIGNS_PICK_CHANNEL}</p>}
    {focusMetric !== null ? <MetricFocusDialog
      key={`portfolio-${windowText}`}
      metricId={focusMetric}
      onClose={closeFocus}
      opener={origin}
      onMetricChange={(metricId) => setFocusMetric(metricId as PortfolioFocusMetric)}
      surface={surface}
      surfaceKey="campaign-portfolio"
    /> : null}
  </>;
}

const trendMetrics = ['spend', 'portfolio_roas_attributed_14d', 'purchases'] as const;
/** Why a trend day's ROAS is "—": the day has no spend, so the ratio is undefined (ADR-087). */
const NO_SPEND_DAY = 'Not applicable: no spend that day';

function PortfolioTrend({ artifact }: { readonly artifact: CampaignPortfolioArtifact }) {
  const [chosen, setSelected] = useState<typeof trendMetrics[number]>('spend');
  const trend = artifact.trend;
  if (trend === null) return <section className="panel"><p className="state-note"><span className="badge">No trend</span>This reporting window does not include a portfolio trend. Choose another available window to review returned trend evidence.</p></section>;
  // A spend-only trend offers spend alone. ROAS on a day with no spend is typed absent
  // (ADR-087): the line breaks there and the exact values show "—" with its reason.
  const offered: readonly (typeof trendMetrics)[number][] = trend.spend_only ? ['spend'] : trendMetrics;
  const selected = offered.includes(chosen) ? chosen : 'spend';
  const values = trend.points.map((point) => point[selected]);
  const absent = values.some((value) => value === null);
  const kind: ValueKind = selected === 'spend' ? 'currency' : selected === 'purchases' ? 'count' : 'ratio';
  const currencyCode = trend.units[selected]?.currency_code ?? null;
  return <section className="panel" aria-label="Portfolio trend">
    <div className="panel-head">
      <div>
        <h2 id="portfolio-trend-heading">{label(selected)}</h2>
        <p className="footnote">{formatIsoDateRange(trend.period.start, trend.period.end)}</p>
      </div>
      <div className="controls">
        {offered.length < 2 ? null : <div className="seg" role="group" aria-label="Portfolio trend metric">
          {offered.map((name) => <button key={name} type="button" aria-pressed={selected === name} onClick={() => setSelected(name)}>{label(name)}</button>)}
        </div>}
        <div className="seg" role="group" aria-label="Available chart grain">
          <button type="button" aria-pressed="true">{trend.grain[0]}</button>
        </div>
      </div>
    </div>
    <SeriesChart
      ariaLabel={`${label(selected)} across the returned ${trend.grain[0]} reporting values`}
      currencyCode={currencyCode}
      current={values}
      dates={trend.points.map((point) => point.date)}
      kind={kind}
      zeroBased={kind !== 'ratio'}
    />
    <ChartLegend prior={false} />
    {absent ? <div className="dt-legend"><span>— {NO_SPEND_DAY}.</span></div> : null}
    {artifact.channel_view === null ? null : <p className="section-line">{artifact.channel_view.trend_line}</p>}
    <details className="exact">
      <summary>Show exact values</summary>
      <div className="scroll">
        <table>
          <caption className="sr-only">Exact returned {label(selected)} values for {formatIsoDateRange(trend.period.start, trend.period.end)}</caption>
          <thead><tr><th scope="col">Series</th>{trend.points.map((point) => <th scope="col" key={point.date}>{point.date}</th>)}</tr></thead>
          <tbody><tr><th scope="row">Current</th>{trend.points.map((point, index) => {
            const value = values[index] ?? null;
            return <td key={point.date}>{value === null
              ? <span className="not-applicable" role="img" aria-label={NO_SPEND_DAY}>—</span>
              : numberText(value, kind, currencyCode)}</td>;
          })}</tr></tbody>
        </table>
      </div>
    </details>
  </section>;
}

/** A row's focus key: its safe display signature, never a capability. */
export function rowKey(signature: CampaignPortfolioRow['signature']): string {
  return [signature.effective_name ?? '', signature.channel, signature.marketplace, signature.platform].join('|');
}

function rosterEntryLabel(entry: CampaignPortfolioArtifact['roster'][number]): string {
  return entry.name ?? `Unnamed campaign ${entry.position + 1}`;
}

/** Ten rows per display page — a browser slice over the baked 50-row segments (owner mandate 2026-09-23). */
const DISPLAY_ROWS = 10;

/**
 * Navigation hints survive the loading-skeleton remount, never an unrelated arrival:
 * each names the exact segment and ordering it was armed for, is consumed at most once,
 * and expires quickly so a failed request cannot leave it live for a later load.
 */
const PENDING_NAVIGATION_TTL_MS = 30_000;

/**
 * Landing slice for the next arriving segment: paging backward across a boundary lands
 * on the previous segment's last slice, and a roster jump lands on the slice containing
 * the match. Module-scoped because a segment request remounts the table through the
 * route controller's loading skeleton.
 */
let pendingDisplaySlice: { readonly at: number; readonly periodKey: string; readonly segment: number; readonly slice: number; readonly sortKey: string } | null = null;

function periodKeyOf(period: { readonly end: string; readonly start: string }): string {
  return `${period.start}:${period.end}`;
}

/** Arm a landing slice for one specific segment arrival (event-handler time, never render). */
function scheduleDisplaySlice(slice: number, sort: CampaignPortfolioSort, segment: number, period: { readonly end: string; readonly start: string }): void {
  pendingDisplaySlice = { at: Date.now(), periodKey: periodKeyOf(period), segment, slice, sortKey: sortKeyOf(sort) };
}

/** Read the landing hint without draining it (pure: safe in a render-time initializer). */
function peekDisplaySlice(artifact: CampaignPortfolioArtifact): number {
  const hint = pendingDisplaySlice;
  if (hint === null || Date.now() - hint.at > PENDING_NAVIGATION_TTL_MS) return 0;
  if (hint.periodKey !== periodKeyOf(artifact.period)) return 0;
  if (hint.segment !== (artifact.pagination?.segment_index ?? 0) || hint.sortKey !== sortKeyOf(artifact.sort)) return 0;
  return hint.slice;
}

/** Drain the landing hint after arrival — matching or not, an arrival consumes it. */
function clearDisplaySlice(): void {
  pendingDisplaySlice = null;
}

function sortLabel(sort: CampaignPortfolioSort): string {
  const field = sort.field === 'name' ? 'campaign name' : label(sort.field).toLowerCase();
  return `${field}, ${sort.direction === 'asc' ? 'ascending' : 'descending'}`;
}

/** A query "Search All channels" carries across the filter change that remounts the table. */
let pendingFindQuery: string | null = null;

/** Open one roster entry: in hand, request its detail; otherwise fetch its canonical segment first. */
function openRosterEntry(
  artifact: CampaignPortfolioArtifact,
  entry: CampaignPortfolioArtifact['roster'][number],
  onSelect: (capability: CampaignSelectionCapability) => void,
  onSegment: ((sort: CampaignPortfolioSort, segment: number) => void) | undefined,
): void {
  const canonical: CampaignPortfolioSort = { direction: 'desc', field: 'spend' };
  const segmentSize = artifact.pagination?.segment_size ?? 50;
  const segmentStart = artifact.pagination === null ? 0 : artifact.pagination.segment_index * segmentSize;
  if (sortKeyOf(artifact.sort) === sortKeyOf(canonical)) {
    const row = artifact.rows[entry.position - segmentStart];
    if (row !== undefined) { onSelect(row.capability); return; }
  }
  scheduleOpenAtPosition(entry.position, canonical, artifact.period);
  onSegment?.(canonical, Math.floor(entry.position / segmentSize));
}

/**
 * "Find a campaign" (ia §7.2 item 9a): a combobox over the current option's baked roster. Arrow
 * keys move through at most eight matches, Enter opens one, Escape clears the list. Matching stays
 * in the browser over the roster (ADR-071); a request never carries the query.
 */
export function FindCampaign({ artifact, filter, hinted = false, id, onFilter, onOpen }: {
  readonly artifact: CampaignPortfolioArtifact;
  readonly filter: CampaignChannelFilter;
  /** The portfolio's field names its scope and Enter under it (mockup campaigns.html). */
  readonly hinted?: boolean;
  readonly id: string;
  readonly onFilter?: (filterId: string) => void;
  readonly onOpen: (entry: CampaignPortfolioArtifact['roster'][number]) => void;
}) {
  const [query, setQuery] = useState(() => {
    const pending = pendingFindQuery;
    return pending ?? '';
  });
  useEffect(() => { pendingFindQuery = null; }, []);
  const [active, setActive] = useState(0);
  const trimmed = query.trim().toLowerCase();
  const matches = trimmed.length === 0
    ? []
    : artifact.roster.filter((entry) => rosterEntryLabel(entry).toLowerCase().includes(trimmed)).slice(0, 8);
  const listId = `${id}-list`;
  const hintId = `${id}-hint`;
  const allChannels = filter.selected === 'all';
  // The mockup's wording per option; a one-channel option is named by its served label.
  const scope = allChannels
    ? 'every channel'
    : filter.selected === 'sponsored' ? 'sponsored channels only' : `${selectedLabel(filter)} only`;
  const none = trimmed.length > 0 && matches.length === 0;
  const open = (entry: CampaignPortfolioArtifact['roster'][number]) => { setQuery(''); onOpen(entry); };
  return <div className="find-campaign">
    <label htmlFor={id}>Find a campaign</label>
    <input
      aria-activedescendant={matches.length === 0 ? undefined : `${listId}-${Math.min(active, matches.length - 1)}`}
      aria-autocomplete="list"
      aria-controls={listId}
      aria-describedby={hinted ? hintId : undefined}
      aria-expanded={matches.length > 0}
      autoComplete="off"
      id={id}
      onChange={(event) => { setQuery(event.target.value); setActive(0); }}
      onKeyDown={(event) => {
        if (event.key === 'ArrowDown' && matches.length > 0) { event.preventDefault(); setActive((index) => (index + 1) % matches.length); }
        if (event.key === 'ArrowUp' && matches.length > 0) { event.preventDefault(); setActive((index) => (index - 1 + matches.length) % matches.length); }
        if (event.key === 'Escape') { setQuery(''); setActive(0); }
        if (event.key === 'Enter') {
          const entry = matches[Math.min(active, matches.length - 1)];
          if (entry !== undefined) { event.preventDefault(); open(entry); }
        }
      }}
      role="combobox"
      type="text"
      value={query}
    />
    <ul className="estate-matches" id={listId} role="listbox" aria-label="Matching campaigns" hidden={matches.length === 0}>
      {matches.map((entry, index) => <li
        aria-selected={index === Math.min(active, matches.length - 1)}
        id={`${listId}-${index}`}
        key={entry.position}
        onClick={() => open(entry)}
        onMouseDown={(event) => event.preventDefault()}
        role="option"
      >
        {rosterEntryLabel(entry)}{' '}
        <span className="sub">{entry.channel} · {entry.marketplace}</span>
      </li>)}
    </ul>
    {/* The roster this field searches: a one-channel option's is that channel's alone. */}
    {hinted ? <p className="find-hint" id={hintId}>Enter on a match opens that campaign&apos;s detail. Matches come from {scope}.</p> : null}
    {/* Mounted empty, so the no-match line is announced when it fills. */}
    <p className="find-none" role="status">
      {!none ? null : allChannels
        ? <>No campaign matches &ldquo;{query.trim()}&rdquo;.</>
        : <>No {selectedLabel(filter)} campaign matches &ldquo;{query.trim()}&rdquo;.{' '}
          {onFilter === undefined ? null : <button type="button" className="btn link" onClick={() => { pendingFindQuery = query; onFilter('all'); }}>Search All channels</button>}</>}
    </p>
  </div>;
}

function PortfolioTable({ artifact, onFilter, onSegment, onSegmentApproach, onSelect, onSort, selection }: {
  readonly artifact: CampaignPortfolioArtifact;
  readonly onFilter?: (filterId: string) => void;
  readonly onSegment?: (sort: CampaignPortfolioSort, segment: number) => void;
  readonly onSegmentApproach?: (sort: CampaignPortfolioSort, segment: number) => void;
  readonly onSelect: (capability: CampaignSelectionCapability) => void;
  readonly onSort?: (sort: CampaignPortfolioSort) => void;
  readonly selection: CampaignSelectionState;
}) {
  // The route controller remounts this table for every segment arrival, so mount time
  // is arrival time: the initializer reads the armed landing slice (a pure peek —
  // StrictMode's double initialization reads the same value), and the mount effect
  // drains the hint so it can never apply to a later, unrelated arrival.
  const [slice, setSlice] = useState(() => peekDisplaySlice(artifact));
  useEffect(() => { clearDisplaySlice(); }, []);
  const [clock, setClock] = useState(() => Date.now());
  useEffect(() => {
    const expiry = artifact.rows.map((row) => Date.parse(row.capability.expires_at)).filter((value) => Number.isFinite(value) && value > clock).sort((left, right) => left - right)[0];
    if (expiry === undefined) return;
    const timeout = window.setTimeout(() => setClock(Date.now()), Math.max(0, expiry - Date.now()));
    return () => window.clearTimeout(timeout);
  }, [artifact.rows, clock]);
  // A partly reported column names its covered days; a stacked "—" cell drops them (ADR-118 d5).
  const coverageText = useCoverageText();
  const requestSort = (field: CampaignPortfolioSort['field']) => onSort?.({ field, direction: artifact.sort.field === field && artifact.sort.direction === 'desc' ? 'asc' : 'desc' });
  const segmentSize = artifact.pagination?.segment_size ?? 50;
  // Display paging: ten rows per page, sliced from the loaded baked segment. Four of
  // five page turns need no request; only a boundary crossing requests the sibling.
  const lastLocalSlice = Math.max(0, Math.ceil(artifact.rows.length / DISPLAY_ROWS) - 1);
  const displaySlice = Math.min(slice, lastLocalSlice);
  const displayRows = artifact.rows.slice(displaySlice * DISPLAY_ROWS, (displaySlice + 1) * DISPLAY_ROWS);
  const pagination = artifact.pagination;
  const slicesPerSegment = Math.ceil(segmentSize / DISPLAY_ROWS);
  const segmentStart = pagination === null ? 0 : pagination.segment_index * pagination.segment_size;
  const totalCount = pagination?.total_count ?? artifact.rows.length;
  const firstRow = segmentStart + displaySlice * DISPLAY_ROWS + 1;
  const lastRow = segmentStart + displaySlice * DISPLAY_ROWS + displayRows.length;
  const pageNumber = (pagination === null ? 0 : pagination.segment_index * slicesPerSegment) + displaySlice + 1;
  const pageCount = Math.max(1, Math.ceil(totalCount / DISPLAY_ROWS));
  const atFirstPage = displaySlice === 0 && (pagination === null || pagination.segment_index === 0);
  const atLastPage = displaySlice === lastLocalSlice && (pagination === null || pagination.segment_index + 1 >= pagination.segment_count);
  const turnPage = (direction: -1 | 1) => {
    const target = displaySlice + direction;
    if (target >= 0 && target <= lastLocalSlice) { setSlice(target); return; }
    if (pagination === null) return;
    if (direction === 1 && pagination.segment_index + 1 < pagination.segment_count) {
      onSegment?.(artifact.sort, pagination.segment_index + 1);
    }
    if (direction === -1 && pagination.segment_index > 0) {
      // The previous sibling is a full segment; land on its last slice.
      scheduleDisplaySlice(slicesPerSegment - 1, artifact.sort, pagination.segment_index - 1, artifact.period);
      onSegment?.(artifact.sort, pagination.segment_index - 1);
    }
  };
  // Reaching a segment's last slice prefetches the next baked sibling, so the
  // boundary crossing needs no visible request.
  useEffect(() => {
    if (pagination === null) return;
    if (displaySlice === lastLocalSlice && pagination.segment_index + 1 < pagination.segment_count) {
      onSegmentApproach?.(artifact.sort, pagination.segment_index + 1);
    }
  }, [artifact.sort, displaySlice, lastLocalSlice, onSegmentApproach, pagination]);
  return <section className="panel" aria-labelledby="campaign-table-heading">
    <div className="panel-head">
      <div>
        <h2 id="campaign-table-heading">Campaigns</h2>
        <p className="footnote">Sorted by {sortLabel(artifact.sort)}. Select a column header to sort by it.</p>
        {artifact.channel_view === null ? <p className="footnote" data-slot="cp-sort-hint">{CAMPAIGNS_SORT_HINT}</p> : null}
        <PageFreshness end={artifact.period.end} qualifiers={artifact.rows.flatMap((row) => row.freshness_qualifiers)} />
      </div>
    </div>
    <div className="table-toolbar">
      <FindCampaign
        artifact={artifact}
        filter={artifact.channel_filter}
        hinted
        id="find-campaign"
        onOpen={(entry) => openRosterEntry(artifact, entry, onSelect, onSegment)}
        {...(onFilter === undefined ? {} : { onFilter })}
      />
    </div>
    <DataTable className="camp-table" title="Campaigns in this window" captionHidden>
        <DtHead><DtRow>
          <DtColumn aria-sort={artifact.sort.field === 'name' ? artifact.sort.direction === 'asc' ? 'ascending' : 'descending' : 'none'}>
            <button type="button" onClick={() => requestSort('name')}>Campaign</button>
          </DtColumn>
          {(['spend', 'attributed_roas', 'purchases', 'ctr'] as const).map((field) => (
            <DtColumn className="num" key={field} basis={coverageText(field)} aria-sort={artifact.sort.field === field ? artifact.sort.direction === 'asc' ? 'ascending' : 'descending' : 'none'}>
              <button type="button" onClick={() => requestSort(field)}>{label(field)}</button>
            </DtColumn>
          ))}
        </DtRow></DtHead>
        <DtBody>{displayRows.map((row) => {
          const expired = isCapabilityExpired(row.capability.expires_at, () => clock);
          const loading = selection.kind === 'loading' && selection.choice.selection_reference === row.capability.selection_reference;
          return <DtRow key={row.capability.selection_reference}>
            <DtRowHeader className="name">
              <button type="button" data-campaign-row={rowKey(row.signature)} disabled={expired || loading} onClick={() => onSelect(row.capability)}>
                <span className="link-text">{campaignName(row)}</span>
                <span className="sub">{row.signature.channel}</span>
              </button>
            </DtRowHeader>
            {row.comparisons.map((comparison) => <DtCell
              absent={comparison.current.value === null}
              basis={{ coverage: coverageText(comparison.metric), text: null }}
              className="num"
              key={comparison.metric}
              label={label(comparison.metric)}
            >
              <span><strong><PortfolioValue metric={comparison.metric} value={comparison.current} /></strong>{' '}
              <PortfolioChange comparison={comparison} figure={`${label(comparison.metric)}, ${campaignName(row)}`} /></span>
            </DtCell>)}
          </DtRow>;
        })}</DtBody>
    </DataTable>
    {pagination === null && totalCount <= DISPLAY_ROWS ? null : <nav className="pager" aria-label="Campaign table position and paging">
      <span className="pager-pos">
        Rows {firstRow}–{lastRow} of {formatCount(String(totalCount))} · sorted by {sortLabel(artifact.sort)}
      </span>
      <span className="pager-controls">
        <button
          type="button"
          disabled={atFirstPage}
          aria-label={`Previous ${DISPLAY_ROWS} rows`}
          onClick={() => turnPage(-1)}
        >‹ Prev</button>
        <span className="pager-page">Page {pageNumber} of {pageCount}</span>
        <button
          type="button"
          disabled={atLastPage}
          aria-label={`Next ${DISPLAY_ROWS} rows`}
          onClick={() => turnPage(1)}
        >Next ›</button>
      </span>
    </nav>}
  </section>;
}

/* ---------- highest and lowest return (UI refresh 3.2, readout A.4, D10) ---------- */

/** The lists' browser-owned words (Campaigns LLD "Highest and lowest return"). */
export const RETURN_LISTS_COPY = {
  high: 'Highest return',
  low: 'Lowest return',
  order: 'Attributed ROAS · in the order the server returns',
  size: 5,
} as const;

type ReturnOrder =
  | { readonly kind: 'pending' }
  | { readonly kind: 'served'; readonly high: readonly CampaignPortfolioRow[]; readonly low: readonly CampaignPortfolioRow[] }
  | { readonly kind: 'omitted' };

function definedReturn(row: CampaignPortfolioRow): boolean {
  return row.comparisons.some((comparison) => comparison.metric === 'attributed_roas' && comparison.current.value !== null);
}

/**
 * The first served rows of the server's own Attributed ROAS orderings, descending and ascending
 * (segment 0 of each, ADR-071), for the same window and channel option. The browser never sorts:
 * if either ordering isn't served for this window and option, both lists are omitted (D10).
 * Rows whose ROAS is undefined sort last on the server in both directions, so they're skipped.
 */
function ReturnLists({ parent, sessionId, temporalSelection }: {
  readonly parent: CampaignPortfolioArtifact;
  readonly sessionId: string;
  readonly temporalSelection: TemporalSelectionV1 | undefined;
}) {
  const option = parent.channel_filter.selected;
  const selectionKey = JSON.stringify(temporalSelection ?? null);
  const { end, start } = parent.period;
  // Each answer is kept with the request it answers, so a changed window or option reads as pending.
  const requestKey = JSON.stringify([sessionId, selectionKey, option, start, end]);
  const [answered, setAnswered] = useState<{ readonly key: string; readonly order: ReturnOrder } | null>(null);
  const order: ReturnOrder = answered?.key === requestKey ? answered.order : { kind: 'pending' };
  useEffect(() => {
    if (sessionId === '') return;
    const setOrder = (next: ReturnOrder) => setAnswered({ key: requestKey, order: next });
    const controller = new AbortController();
    const selection = (JSON.parse(selectionKey) as TemporalSelectionV1 | null) ?? undefined;
    const filter = option === 'all' ? undefined : option;
    const ordering = (direction: CampaignPortfolioSort['direction']) => fetchCampaignPresentationV2(sessionId, selection, controller.signal, { direction, field: 'attributed_roas' }, 0, filter);
    Promise.all([ordering('desc'), ordering('asc')]).then(([high, low]) => {
      const rows = (served: typeof high, direction: CampaignPortfolioSort['direction']) => (
        served.kind === 'parent' && served.artifact.outcome === 'complete' &&
        served.artifact.sort.field === 'attributed_roas' && served.artifact.sort.direction === direction &&
        served.artifact.period.start === start && served.artifact.period.end === end &&
        served.artifact.channel_filter.selected === option
          ? served.artifact.rows.filter(definedReturn).slice(0, RETURN_LISTS_COPY.size)
          : null
      );
      const highRows = rows(high, 'desc');
      const lowRows = rows(low, 'asc');
      setOrder(highRows === null || lowRows === null || highRows.length === 0 ? { kind: 'omitted' } : { high: highRows, kind: 'served', low: lowRows });
    }).catch(() => { if (!controller.signal.aborted) setOrder({ kind: 'omitted' }); });
    return () => controller.abort();
  }, [end, option, requestKey, selectionKey, sessionId, start]);
  if (order.kind !== 'served') return null;
  const list = (title: string, rows: readonly CampaignPortfolioRow[], slot: string) => <section className="card section cp-return-list" aria-labelledby={`${slot}-heading`} data-slot={slot}>
    <h3 id={`${slot}-heading`}>{title}</h3>
    <p className="footnote">{RETURN_LISTS_COPY.order}</p>
    <ol>{rows.map((row) => {
      const roas = row.comparisons.find((comparison) => comparison.metric === 'attributed_roas');
      const spend = row.comparisons.find((comparison) => comparison.metric === 'spend');
      return <li key={row.capability.selection_reference}>
        <span className="name">{campaignName(row)}<span className="sub">{row.signature.channel}</span></span>
        <span className="num"><strong>{roas === undefined ? null : <PortfolioValue metric="attributed_roas" value={roas.current} />}</strong></span>
        <span className="num">{spend === undefined ? null : <PortfolioValue metric="spend" value={spend.current} />}</span>
      </li>;
    })}</ol>
  </section>;
  return <div className="cp-return-lists">
    {list(RETURN_LISTS_COPY.high, order.high, 'cp-top')}
    {list(RETURN_LISTS_COPY.low, order.low, 'cp-low')}
  </div>;
}

/* ---------- channel filter (ADR-127) ---------- */

/**
 * The server-declared channel filter: a segmented control at two or three options and a
 * labelled "Channel" select at four or more; below a 560 px container it's always the select.
 * A change keeps focus on the control and replaces the history entry (ia §8.1).
 */
export function ChannelFilterControl({ filter, onChange }: {
  readonly filter: CampaignChannelFilter;
  readonly onChange: (filterId: string) => void;
}) {
  if (filter.options.length < 2) return null;
  const select = <label className={filter.options.length <= 3 ? 'channel-select narrow-only' : 'channel-select'}>
    <span className="channel-label">Channel</span>
    <select value={filter.selected} onChange={(event) => onChange(event.target.value)}>
      {filter.options.map((option) => <option key={option.filter_id} value={option.filter_id}>{option.label}</option>)}
    </select>
  </label>;
  if (filter.options.length > 3) return select;
  return <>
    <div className="seg channel-seg wide-only" role="group" aria-label="Channel">
      {filter.options.map((option) => <button
        aria-pressed={filter.selected === option.filter_id}
        key={option.filter_id}
        onClick={() => onChange(option.filter_id)}
        type="button"
      >{option.label}</button>)}
    </div>
    {select}
  </>;
}

function ChannelCell({ comparison, figure, roles, slot }: {
  readonly comparison: CampaignPortfolioComparison<'spend' | 'impressions' | 'clicks' | 'ctr' | 'cpc' | 'cpm' | 'viewable_rate'>;
  readonly figure: string;
  readonly roles?: readonly string[];
  readonly slot: string;
}) {
  const value = comparison.current;
  return <span>
    <strong>{value.value === null
      // The same words as the delivery legend's line for this "—" (server-served).
      ? <span className="not-applicable" role="img" aria-label={`Not applicable: ${undefinedRatioReason[comparison.metric] ?? 'no denominator'} in this window`}>—</span>
      : displayValueText({ ...value, value: value.value }, value.units.kind, governedMetric(comparison.metric))}</strong>{' '}
    {comparison.current.value !== null && comparison.prior.value !== null
      ? <FigureChange change={comparison.change} figure={figure} slot={slot} {...(roles === undefined ? {} : { roles })} />
      : null}
  </span>;
}

/** The viewable rate's change is withheld by a gap in either window; each has its entry. */
const VIEWABLE_ROLES = ['current', 'prior'] as const;

/**
 * "By channel" (ia §7.2 item 9): Delivery per channel from each channel's own operands with the
 * DSP viewable rate. No attributed figure and, since UX sweep fix 6, no spend table: spend and
 * return per channel have their one home on the Scorecard.
 */
function ByChannel({ comparedText, scorecardStep, view }: {
  /** "{option} · {window} compared with {prior}", each table's caption note. */
  readonly comparedText: string;
  readonly scorecardStep: InsightNextStep | null;
  readonly view: CampaignChannelView;
}) {
  const coverageText = useCoverageText();
  const basis = (...parts: (string | null | undefined)[]) => parts.filter(Boolean).join(' · ') || null;
  return <section className="panel channel-section" aria-labelledby="by-channel-heading">
    <div className="panel-head"><div>
      <h2 id="by-channel-heading">By channel</h2>
    </div></div>
    {/* UX sweep fix 6 (readout A.4): the spend-per-channel table repeated the Scorecard, so only
        delivery per channel renders here; spend and return per channel live on the Scorecard. */}
    <DataTable className="channel-table" title="Delivery per channel" note={comparedText}>
      <DtHead><DtRow>
        <DtColumn>Channel</DtColumn>
        <DtColumn className="num" basis={basis(coverageText('channel_clicks'))}>Clicks</DtColumn>
        <DtColumn className="num" basis={basis(view.delivery_bases.ctr, coverageText('channel_ctr'))}>CTR</DtColumn>
        <DtColumn className="num" basis={basis(view.delivery_bases.cpc, coverageText('channel_cpc'))}>CPC</DtColumn>
        <DtColumn className="num" basis={basis(view.delivery_bases.cpm, coverageText('channel_cpm'))}>CPM</DtColumn>
        {view.viewability ? <DtColumn className="num" basis={basis(view.viewability_heading, coverageText('channel_viewable_rate'))}>Viewable rate</DtColumn> : null}
      </DtRow></DtHead>
      <DtBody>{view.delivery.map((row) => <DtRow key={row.channel}>
        <DtRowHeader className="name">{row.label}</DtRowHeader>
        <DtCell className="num" label="Clicks"><ChannelCell comparison={row.clicks} figure={`Clicks, ${row.label}`} slot="channel_clicks" /></DtCell>
        <DtCell className="num" label="CTR"><ChannelCell comparison={row.ctr} figure={`CTR, ${row.label}`} slot="channel_ctr" /></DtCell>
        <DtCell className="num" label="CPC"><ChannelCell comparison={row.cpc} figure={`CPC, ${row.label}`} slot="channel_cpc" /></DtCell>
        <DtCell className="num" label="CPM"><ChannelCell comparison={row.cpm} figure={`CPM, ${row.label}`} slot="channel_cpm" /></DtCell>
        {row.viewable_rate === null ? null : <DtCell absent={row.viewable_rate.comparison === null} className="num" label="Viewable rate">
          {row.viewable_rate.comparison === null
            ? <span className="not-applicable" role="img" aria-label={row.viewable_rate.absence_reason ?? undefined}>—</span>
            : <ChannelCell comparison={row.viewable_rate.comparison} figure={`Viewable rate, ${row.label}`} roles={VIEWABLE_ROLES} slot="channel_viewable_rate" />}
        </DtCell>}
      </DtRow>)}</DtBody>
    </DataTable>
    {view.delivery_legend.length === 0 ? null : <div className="dt-legend">
      {view.delivery_legend.map((line) => <span key={line}>{line}</span>)}
    </div>}
    <p className="section-line">{view.scorecard_line}</p>
    <InsightNextStepControl pageId="campaigns" step={scorecardStep} />
  </section>;
}

/* ---------- detail sections ---------- */

function CoverageGaps({ detail }: { readonly detail: CampaignAttributionDetailV2 }) {
  const gaps = detail.evidence_by_slot.flatMap((evidence) => evidence.coverage_holes);
  if (gaps.length === 0) return <p className="quality" role="status"><span className="pill good">Published coverage spans {formatIsoDateRange(detail.period.start, detail.period.end)}</span></p>;
  return <div className="state-note" role="status">
    <span className="badge">Coverage gap</span>
    <strong>Some returned evidence has a coverage gap.</strong>
    <ul>{gaps.map((gap) => <li key={`${gap.start}-${gap.end}`}>No published data from {formatIsoDateRange(gap.start, gap.end)}. Covered dates remain visible in the chart and table.</li>)}</ul>
  </div>;
}

function focusValue(detail: CampaignAttributionDetailV2, metricName: CampaignFocusMetric, prior = false): CampaignMetricValue | null {
  if (metricName === 'purchases' || metricName === 'attributed_roas' || metricName === 'spend') {
    const comparison = detail.comparison?.metrics.find((entry) => entry.slot === metricName);
    return prior ? comparison?.prior ?? null : detail.summary[metricName];
  }
  const values = prior ? detail.delivery.comparison.slots : detail.delivery.summary;
  return values.find((entry) => entry.slot === metricName)?.value ?? null;
}

/** The tile's server change: a summary slot's, or a delivery slot's from its comparison entry. */
function focusChange(detail: CampaignAttributionDetailV2, metricName: CampaignFocusMetric): Change | null {
  if (metricName === 'purchases' || metricName === 'attributed_roas' || metricName === 'spend') {
    return detail.comparison?.metrics.find((entry) => entry.slot === metricName)?.change ?? null;
  }
  return detail.delivery.comparison.slots.find((entry) => entry.slot === metricName)?.change ?? null;
}

function deliveryMetricValue(
  detail: CampaignAttributionDetailV2,
  metricName: CampaignDeliverySlot,
  prior = false,
): CampaignMetricValue | null {
  const values = prior ? detail.delivery.comparison.slots : detail.delivery.summary;
  return values.find((entry) => entry.slot === metricName)?.value ?? null;
}

const deliveryFocusMetricSet = new Set<string>(['impressions', 'clicks', 'ctr', 'cpc', 'cpm']);

function DetailTiles({ detail, focusSelectionReference, focusSessionId }: { readonly detail: CampaignAttributionDetailV2; readonly focusSelectionReference: string | null; readonly focusSessionId: string }) {
  const [focusMetric, setFocusMetric] = useState<CampaignFocusMetric | null>(null);
  const [focusCache] = useState(() => new Map<string, MetricFocusV4>());
  const [statusOpen, setStatusOpen] = useState(false);
  const origin = useRef<HTMLButtonElement>(null);
  const statusOrigin = useRef<HTMLButtonElement | null>(null);
  const closeFocus = () => setFocusMetric(null);
  const closeStatus = () => setStatusOpen(false);
  const openStatus = (event: MouseEvent<HTMLButtonElement>) => {
    statusOrigin.current = event.currentTarget;
    setStatusOpen(true);
  };
  const deliveryInsufficient = detail.delivery.outcome === 'insufficient';
  const windowText = formatIsoDateRange(detail.period.start, detail.period.end);
  const scope = `${detail.selection.effective_name ?? 'this campaign'} from ${windowText}`;
  const surface: MetricFocusSurface = {
    cache: focusCache,
    pageId: 'campaigns',
    cacheScope: `${detail.insights[0]?.artifact_revision ?? `window:${windowText}`}:${focusSelectionReference ?? 'no-selection'}`,
    askSeed: (focus) => `Ask about ${focus.metric.label} for ${scope}.`,
    fetch: (metricId, signal) => {
      if (focusSelectionReference === null) throw new Error('The campaign selection is unavailable.');
      return fetchCampaignMetricFocus(focusSessionId, focusSelectionReference, metricId as CampaignFocusMetric, signal);
    },
    deepFor: (focus) => focusSelectionReference === null ? null : ({
      cacheKey: `campaign-detail:metric-focus:${focus.metric.id}:${focus.insight.artifact_revision}:${focus.period.start}:${focus.period.end}:${focusSelectionReference}`,
      expectedArtifactRevision: focus.insight.artifact_revision,
      fetch: (signal) => fetchInsightDeepDive({
        session_id: focusSessionId,
        slot_id: 'metric-focus',
        metric: focus.metric.id,
        selection_reference: focusSelectionReference,
      }, signal),
      window: focus.period,
    }),
    labelFor: (metricId) => label(metricId as CampaignFocusMetric),
    neighbors: campaignFocusMetrics,
    window: detail.period,
  };
  return <>
    <div className={deliveryInsufficient ? 'tiles detail-tiles delivery-short' : 'tiles detail-tiles'}>
      {campaignFocusMetrics.map((metricName) => {
        // ADR-072: an insufficient delivery renders one card that opens the status sheet,
        // never five dead per-metric tiles.
        if (deliveryInsufficient && deliveryFocusMetricSet.has(metricName)) return null;
        const value = focusValue(detail, metricName);
        if (value === null) return <article className="tile empty" key={metricName}>
          <p className="k">{label(metricName)}</p>
          <p className="v">Not measured for this reporting window</p>
          <p className="prior">The server did not publish this value.</p>
          <button type="button" className="see-why" aria-haspopup="dialog" onClick={openStatus}>
            See why this value is missing
          </button>
        </article>;
        return <article className="tile" key={metricName}>
          <p className="k">{label(metricName)}</p>
          <div className="row">
            <div>
              <p className="v">{metric(value, valueKind(metricName), metricName)}</p>
              <p className="prior">Prior: {metric(focusValue(detail, metricName, true), valueKind(metricName), metricName)}</p>
            </div>
            <ChangeChip change={focusChange(detail, metricName)} />
          </div>
          <button
            type="button"
            className="ask"
            disabled={focusSelectionReference === null}
            aria-haspopup="dialog"
            onClick={(event) => { origin.current = event.currentTarget; setFocusMetric(metricName); }}
          >
            Open the focus view for {label(metricName).toLowerCase()}
          </button>
        </article>;
      })}
      {deliveryInsufficient ? <article className="tile empty delivery-status" key="delivery-status">
        <p className="k">Delivery evidence</p>
        <p className="v">Not enough data for this window</p>
        <p className="prior">Each delivery metric's status and what would fill it are one tap away.</p>
        <button type="button" className="see-why" aria-haspopup="dialog" onClick={openStatus}>
          See why delivery evidence is insufficient
        </button>
      </article> : null}
    </div>
    {statusOpen ? <EvidenceStatusSheet
      onClose={closeStatus}
      opener={statusOrigin}
      status={detail.evidence_status}
      subject={detail.selection.effective_name ?? 'This campaign'}
    /> : null}
    {focusMetric !== null && focusSelectionReference !== null ? <MetricFocusDialog
      key={focusSelectionReference}
      metricId={focusMetric}
      onClose={closeFocus}
      opener={origin}
      onMetricChange={(metricId) => setFocusMetric(metricId as CampaignFocusMetric)}
      surface={surface}
      surfaceKey="campaign-detail"
    /> : null}
  </>;
}

function deliverySeries(
  detail: CampaignAttributionDetailV2,
  metricName: CampaignDeliverySlot,
): {
  readonly current: readonly (number | string)[];
  readonly dates: readonly string[];
  readonly prior: readonly (number | string)[];
} | null {
  const points = detail.delivery.series.points;
  if (points.length === 0 || !detail.delivery.series.metrics.includes(metricName)) return null;
  return {
    current: points.map((point) => point.current[metricName]),
    dates: points.map((point) => point.date),
    prior: points.map((point) => point.prior[metricName]),
  };
}

function ExactValues({ detail, metricName, series }: { readonly detail: CampaignAttributionDetailV2; readonly metricName: CampaignDeliverySlot; readonly series: { readonly current: readonly (number | string)[]; readonly dates: readonly string[]; readonly prior: readonly (number | string)[] } }) {
  const currencyCode = detail.delivery.summary.find((item) => item.slot === metricName)?.value?.units.currency_code ?? null;
  return <details className="exact" open>
    <summary>Show exact values</summary>
    <div className="scroll" tabIndex={0}>
      <table>
        <caption className="sr-only">Exact returned current and aligned-prior values for {label(metricName)} from {formatIsoDateRange(detail.period.start, detail.period.end)}</caption>
        <thead><tr><th scope="col">Series</th>{series.dates.map((date) => <th scope="col" key={date}>{date}</th>)}</tr></thead>
        <tbody>
          <tr><th scope="row">Current</th>{series.current.map((value, index) => <td key={series.dates[index]}>{numberText(value, valueKind(metricName), currencyCode)}</td>)}</tr>
          <tr><th scope="row">Aligned prior</th>{series.prior.map((value, index) => <td key={series.dates[index]}>{numberText(value, valueKind(metricName), currencyCode)}</td>)}</tr>
        </tbody>
      </table>
    </div>
  </details>;
}

function DetailChart({ detail }: { readonly detail: CampaignAttributionDetailV2 }) {
  const deliveryVisual = bindingByRoute.campaigns.visuals.find(
    (visual) => visual.visual_id === detail.delivery.series.visual_id,
  );
  const availableMetrics = useMemo(
    () => (deliveryVisual?.metric_cohort ?? []).filter(
      (metricName): metricName is CampaignDeliverySlot => (
        isDeliveryMetric(metricName) && detail.delivery.series.metrics.includes(metricName) &&
        deliverySeries(detail, metricName) !== null
      ),
    ),
    [deliveryVisual, detail],
  );
  const availableGrains = bindingByRoute.campaigns.temporal_policy.display_grains.filter(
    (grain): grain is 'day' => detail.delivery.series.available_grains.includes(grain as 'day'),
  );
  const [selectedMetric, setSelectedMetric] = useState<CampaignDeliverySlot>(availableMetrics[0] ?? 'ad_spend');
  const [selectedGrain, setSelectedGrain] = useState(detail.delivery.series.grain);
  const activeMetric = availableMetrics.includes(selectedMetric) ? selectedMetric : availableMetrics[0] ?? selectedMetric;
  const activeGrain = availableGrains.includes(selectedGrain) ? selectedGrain : availableGrains[0] ?? selectedGrain;
  const series = deliverySeries(detail, activeMetric);
  const currencyCode = detail.delivery.summary.find((item) => item.slot === activeMetric)?.value?.units.currency_code ?? null;
  return <section className="panel" aria-label="Campaign trend">
    <div className="panel-head">
      <div>
        <h2 id="campaign-trend-heading">{label(activeMetric)}</h2>
        <p className="footnote">{formatIsoDateRange(detail.period.start, detail.period.end)}</p>
      </div>
      <div className="controls">
        <div className="seg" role="group" aria-label="Campaign chart metric">
          {availableMetrics.map((metricName) => <button key={metricName} type="button" aria-pressed={activeMetric === metricName} onClick={() => setSelectedMetric(metricName)}>{label(metricName)}</button>)}
        </div>
        <div className="seg" role="group" aria-label="Available chart grain">
          {availableGrains.map((grain) => <button key={grain} type="button" aria-pressed={activeGrain === grain} onClick={() => setSelectedGrain(grain)}>{grain}</button>)}
        </div>
      </div>
    </div>
    {series === null
      ? <p className="state-note"><span className="badge">No trend</span>This metric does not have returned time-series values for the current reporting window.</p>
      : <>
        <SeriesChart
          ariaLabel={`${label(activeMetric)} across the returned ${activeGrain} reporting values`}
          currencyCode={currencyCode}
          current={series.current}
          dates={series.dates}
          kind={valueKind(activeMetric)}
          prior={series.prior}
          zeroBased={valueKind(activeMetric) !== 'ratio'}
        />
        <ChartLegend prior />
        <p className="footnote">Aligned prior values are returned by the server.</p>
        <ExactValues detail={detail} metricName={activeMetric} series={series} />
      </>}
  </section>;
}

function DeliveryValues({ detail }: { readonly detail: CampaignAttributionDetailV2 }) {
  return <section className="panel" aria-labelledby="delivery-values-heading">
    <div className="panel-head">
      <div>
        <p className="eyebrow">Delivery values</p>
        <h2 id="delivery-values-heading">Delivery evidence</h2>
        <p className="footnote">Delivery values, current and aligned-prior rows, and qualifications are supplied by the server for this reporting window. Monetary values show at two decimal places.</p>
      </div>
    </div>
    <DataTable title={`Delivery values for ${formatIsoDateRange(detail.period.start, detail.period.end)}`} note="Monetary values at two decimal places." captionHidden>
      <DtHead><DtRow><DtColumn>Metric</DtColumn><DtColumn>Current</DtColumn><DtColumn>Aligned prior</DtColumn></DtRow></DtHead>
      <DtBody>{deliveryMetrics.map((metricName) => <DtRow key={metricName}>
        <DtRowHeader>{label(metricName)}</DtRowHeader>
        <DtCell label="Current">{metric(deliveryMetricValue(detail, metricName), valueKind(metricName), metricName)}</DtCell>
        <DtCell label="Aligned prior">{metric(deliveryMetricValue(detail, metricName, true), valueKind(metricName), metricName)}</DtCell>
      </DtRow>)}</DtBody>
    </DataTable>
    {detail.delivery.dsp_quality.outcome === 'unavailable' ? <p className="footnote">DSP quality values are not available for this campaign type in this reporting window.</p> : null}
  </section>;
}

/** Focus fell to the document, or still sits on a control that left with the view. */
function focusLeftByView(): boolean {
  const active = document.activeElement;
  return active === null || active === document.body || active.id === PAGE_HEADING_ID ||
    active instanceof HTMLButtonElement && active.dataset.campaignRow !== undefined ||
    // The hub header's Back to campaigns, which the detail it closes publishes.
    active.closest('.hub-back') !== null;
}

/**
 * Opening detail focuses the campaign h1; closing it (Back to campaigns, the crumb, browser
 * Back) returns focus to the campaign's row. Focus still on a control, such as the date
 * control after a window change, stays there (ia §7.2 item 11a).
 */
function useCampaignDetailFocus(key: string): void {
  useEffect(() => {
    if (focusLeftByView()) document.getElementById(PAGE_HEADING_ID)?.focus();
    return () => {
      window.requestAnimationFrame(() => {
        if (!focusLeftByView()) return;
        const rows = document.querySelectorAll<HTMLButtonElement>('button[data-campaign-row]');
        [...rows].find((row) => row.dataset.campaignRow === key)?.focus();
      });
    };
  }, [key]);
}

function CampaignDetail({ detail, focusSelectionReference, focusSessionId, onClear, scorecardStep }: { readonly detail: CampaignAttributionDetailV2; readonly focusSelectionReference: string | null; readonly focusSessionId: string; readonly onClear: () => void; readonly scorecardStep: InsightNextStep | null }) {
  const name = detail.selection.effective_name ?? 'Name unavailable';
  // Back to campaigns sits above the h1, before the scope band (campaign-detail.html).
  useOpenEntity(name, onClear, 'Back to campaigns');
  useCampaignDetailFocus(rowKey(detail.selection));
  const window = formatIsoDateRange(detail.period.start, detail.period.end);
  return <section className="campaign-command-center" aria-labelledby={PAGE_HEADING_ID}>
    <InsightRow
      period={detail.period}
      selectionReference={focusSelectionReference}
      sessionId={focusSessionId}
      slots={detail.insights}
      surfaceKey="campaign-detail"
      title="Campaign insights"
    />
    <DetailTiles detail={detail} focusSelectionReference={focusSelectionReference} focusSessionId={focusSessionId} />
    <DetailChart detail={detail} />
    <CoverageGaps detail={detail} />
    <DeliveryValues detail={detail} />
    {/* The campaign's channel row has its home on the Scorecard (ia §7.2 item 13). */}
    <InsightNextStepControl pageId="campaigns" step={scorecardStep} />
    <div className="ask-row">
      <button type="button" className="ask-chip" onClick={() => seedAssistantQuestion(`Ask about ${name} from ${window}.`)}>Ask about this campaign</button>
      <button type="button" className="ask-chip" onClick={() => seedAssistantQuestion(`Compare ${name} against the portfolio from ${window}.`)}>Compare against the portfolio</button>
    </div>
  </section>;
}

/* ---------- band title + skeletons ---------- */

function sortKeyOf(sort: CampaignPortfolioSort): string {
  return `${sort.field}:${sort.direction}`;
}

/** The word the position readout names its ordering with ("1 of 1,024 by spend"). */
function orderingWord(sort: CampaignPortfolioSort): string {
  return sort.field === 'name' ? 'campaign name' : label(sort.field).toLowerCase();
}

/**
 * A campaign the switcher must open in a segment not yet loaded: the boundary crossing
 * (or canonical roster jump) fetches the baked sibling first, then opens the campaign
 * at this position. Module-scoped because the segment request remounts the view.
 */
let pendingOpenPosition: { readonly at: number; readonly periodKey: string; readonly position: number; readonly sortKey: string } | null = null;

/** Schedule a campaign open for when its segment arrives (event-handler time, never render). */
function scheduleOpenAtPosition(position: number, sort: CampaignPortfolioSort, period: { readonly end: string; readonly start: string }): void {
  pendingOpenPosition = { at: Date.now(), periodKey: periodKeyOf(period), position, sortKey: sortKeyOf(sort) };
}

/** The open campaign's index in the loaded segment, or null when it is not in hand. */
function rosterIndex(parent: CampaignPortfolioArtifact, command: CampaignSelectionCapability): number | null {
  const index = parent.rows.findIndex((row) => row.capability.selection_reference === command.selection_reference);
  return index === -1 ? null : index;
}

export interface CampaignSwitcherActions {
  readonly command: CampaignSelectionCapability;
  readonly onSegment: (sort: CampaignPortfolioSort, segment: number) => void;
  readonly onSelect: (capability: CampaignSelectionCapability) => void;
  readonly parent: CampaignPortfolioArtifact;
}

/**
 * In-detail campaign switcher (owner mandate 2026-09-23): ‹ › walks the server-declared
 * ordering the campaign was opened from. A swap inside the loaded segment needs no
 * segment fetch — the neighbor's capability is already in hand — and issues the same
 * detail request a table selection does. A boundary crossing fetches the baked sibling
 * first. The jump field searches the estate-wide name roster, whose positions are
 * canonical-ordering positions (ADR-071): a jump lands in the canonical ordering.
 */
function CampaignSwitcher({ command, onSegment, onSelect, parent }: CampaignSwitcherActions) {
  const index = rosterIndex(parent, command);
  if (index === null) return null;
  const pagination = parent.pagination;
  const segmentStart = pagination === null ? 0 : pagination.segment_index * pagination.segment_size;
  const segmentSize = pagination?.segment_size ?? 50;
  const total = pagination?.total_count ?? parent.rows.length;
  const position = segmentStart + index;
  const orderWord = orderingWord(parent.sort);
  const prevRow = parent.rows[index - 1];
  const nextRow = parent.rows[index + 1];
  const openAcrossSegments = (sort: CampaignPortfolioSort, targetPosition: number) => {
    scheduleOpenAtPosition(targetPosition, sort, parent.period);
    onSegment(sort, Math.floor(targetPosition / segmentSize));
  };
  const swap = (direction: -1 | 1) => {
    const neighbor = parent.rows[index + direction];
    if (neighbor !== undefined) { onSelect(neighbor.capability); return; }
    const targetPosition = position + direction;
    if (pagination === null || targetPosition < 0 || targetPosition >= total) return;
    openAcrossSegments(parent.sort, targetPosition);
  };
  const jumpTo = (entry: CampaignPortfolioArtifact['roster'][number]) => {
    const canonical: CampaignPortfolioSort = { direction: 'desc', field: 'spend' };
    if (sortKeyOf(parent.sort) === sortKeyOf(canonical)) {
      const row = parent.rows[entry.position - segmentStart];
      if (row !== undefined) { onSelect(row.capability); return; }
    }
    openAcrossSegments(canonical, entry.position);
  };
  return <>
    <nav className="metric-switch" aria-label={`Switch campaign within the roster ordered by ${orderWord}`}>
      <button
        type="button"
        disabled={position === 0}
        aria-label={prevRow === undefined ? `Previous campaign by ${orderWord}` : `Previous campaign by ${orderWord}: ${campaignName(prevRow)}`}
        onClick={() => swap(-1)}
      ><span aria-hidden="true">‹</span></button>
      <span>
        {prevRow === undefined ? null : <>{campaignName(prevRow)} · </>}
        <span className="current">{detailRowName(parent, index)}</span>
        {nextRow === undefined ? null : <> · {campaignName(nextRow)}</>}
      </span>
      <button
        type="button"
        disabled={position + 1 >= total}
        aria-label={nextRow === undefined ? `Next campaign by ${orderWord}` : `Next campaign by ${orderWord}: ${campaignName(nextRow)}`}
        onClick={() => swap(1)}
      ><span aria-hidden="true">›</span></button>
    </nav>
    <FindCampaign artifact={parent} filter={parent.channel_filter} id="campaign-jump" onOpen={jumpTo} />
  </>;
}

function detailRowName(parent: CampaignPortfolioArtifact, index: number): string {
  const row = parent.rows[index];
  return row === undefined ? 'Name unavailable' : campaignName(row);
}

/** Title band for an open campaign: the detail fills the surface, so the band carries its identity. */
export function DetailBandTitle({ detail, switcher }: {
  readonly detail: CampaignAttributionDetailV2;
  readonly switcher?: CampaignSwitcherActions | undefined;
}) {
  const window = formatIsoDateRange(detail.period.start, detail.period.end);
  const index = switcher === undefined ? null : rosterIndex(switcher.parent, switcher.command);
  const readout = switcher === undefined || index === null
    ? ''
    : ` · ${(switcher.parent.pagination === null ? 0 : switcher.parent.pagination.segment_index * switcher.parent.pagination.segment_size) + index + 1} of ${formatCount(String(switcher.parent.pagination?.total_count ?? switcher.parent.rows.length))} by ${orderingWord(switcher.parent.sort)}`;
  return <>
    <p className="eyebrow">Campaign command center{readout}</p>
    {/* The campaign name is the shell's h1 (ia §3.5); the band keeps the switcher. */}
    {switcher === undefined ? null : <div className="title-row"><CampaignSwitcher {...switcher} /></div>}
    <div className="scopes">
      <span className="pill">{detail.selection.platform}</span>
      <span className="pill">{detail.selection.marketplace}</span>
      <span className="pill">{detail.selection.channel}</span>
      <span className="pill good">{window}</span>
    </div>
  </>;
}

export function PortfolioBandTitle({ artifact, onFilter }: {
  readonly artifact: CampaignPortfolioArtifact | null;
  readonly onFilter?: (filterId: string) => void;
}) {
  const filter = artifact?.channel_filter ?? null;
  return <>
    {filter === null || onFilter === undefined ? null : <div className="channel-control">
      <ChannelFilterControl filter={filter} onChange={onFilter} />
    </div>}
    <div className="scopes">
      {artifact === null
        ? <span className="pill">Reporting window unavailable</span>
        : <>
          {/* Text diet (D5): the channel select already names the option; readiness shows only when absent. */}
          <span className="pill">{formatCount(String(artifact.total_count))} {artifact.total_count === 1 ? 'campaign' : 'campaigns'}</span>
          <span className="pill">Ad-attributed</span>
          {artifact.outcome === 'complete' ? null : <span className="pill warn">{portfolioOutcomeCopy(artifact.outcome).status}</span>}
        </>}
    </div>
  </>;
}

export function PortfolioBandTitleSkeleton() {
  return <span className="skel h-line short" />;
}

export function CampaignCommandCenterSkeleton() {
  return <div role="status" aria-label="Loading campaign portfolio">
    <div className="narr-row" aria-hidden="true">
      {[0, 1, 2, 3].map((item) => <div className="narr-card" key={item}><span className="skel h-label" /><span className="skel h-line" /><span className="skel h-line short" /></div>)}
    </div>
    <div className="tiles" aria-hidden="true">
      {[0, 1, 2, 3].map((item) => <div className="tile static" key={item}><span className="skel h-label" /><span className="skel h-value" /><span className="skel h-line short" /></div>)}
    </div>
    <div className="panel" aria-hidden="true">
      <span className="skel h-label" />
      <span className="skel h-chart" />
    </div>
    <p className="loading-note"><span className="spin" aria-hidden="true" />Loading the campaign portfolio…</p>
  </div>;
}

function DetailSkeleton() {
  return <div role="status" aria-label="Loading campaign command center">
    <div className="tiles detail-tiles" aria-hidden="true">
      {[0, 1, 2, 3, 4, 5, 6, 7].map((item) => <div className="tile static" key={item}><span className="skel h-label" /><span className="skel h-value" /><span className="skel h-line short" /></div>)}
    </div>
    <p className="loading-note"><span className="spin" aria-hidden="true" />Loading the campaign command center…</p>
  </div>;
}

function ClosedState({ description, title }: { readonly description: string; readonly title: string }) {
  return <div className="state-note" role="status">
    <span className="badge">Unavailable</span>
    <strong>{title}</strong>
    <span>{description}</span>
  </div>;
}

export function CampaignPortfolioV2View({ child, closedCampaign = null, detailStep = null, fallback = null, focusSelectionReference = null, focusSessionId = '', focusTemporalSelection, onClear, onFilter, onSegment, onSegmentApproach, onSelect, onSort, parent, scorecardStep = null, selection }: {
  readonly child: CampaignAttributionDetailV2 | null;
  /** The campaign a window change closed because no single row matched it (ia §7.2 11a). */
  readonly closedCampaign?: string | null;
  readonly detailStep?: InsightNextStep | null;
  /**
   * All channels is shown in place of a channel: the link named one this tab doesn't offer, or
   * the window just chosen doesn't offer the one on screen.
   */
  readonly fallback?: 'link' | 'window' | null;
  readonly focusSelectionReference?: string | null; readonly focusSessionId?: string; readonly focusTemporalSelection?: TemporalSelectionV1 | undefined; readonly onClear: () => void;
  readonly onFilter?: (filterId: string) => void;
  readonly onSegment?: (sort: CampaignPortfolioSort, segment: number) => void; readonly onSegmentApproach?: (sort: CampaignPortfolioSort, segment: number) => void; readonly onSelect: (capability: CampaignSelectionCapability) => void; readonly onSort?: (sort: CampaignPortfolioSort) => void; readonly parent: CampaignPortfolioArtifact;
  readonly scorecardStep?: InsightNextStep | null;
  readonly selection: CampaignSelectionState;
}) {
  const portfolioFocus = usePortfolioFocus();
  // A card's campaign step opens the row whose capability this response minted for it, and a
  // metric step the portfolio's focus dialog (ADR-122 decision 4).
  const portfolioSteps: InsightNextStepHandlers = {
    open_campaign_detail: (target) => {
      const row = parent.rows.find((item) => item.capability.selection_reference === target.campaign_ref);
      if (row !== undefined) onSelect(row.capability);
    },
    open_metric_focus: (target, origin) => {
      const metricId = portfolioFocusMetrics.find((item) => item === target.metric);
      if (metricId !== undefined) portfolioFocus.open(metricId, origin);
    },
    focusableMetrics: portfolioFocusMetrics,
  };
  // A switcher boundary crossing (or canonical roster jump) fetched this segment to open
  // one campaign: consume the pending position exactly once when its segment arrives.
  // A parent that does not match drops the pending navigation instead of letting it fire later.
  useEffect(() => {
    if (child !== null || pendingOpenPosition === null) return;
    const target = pendingOpenPosition;
    pendingOpenPosition = null;
    if (Date.now() - target.at > PENDING_NAVIGATION_TTL_MS) return;
    if (target.periodKey !== periodKeyOf(parent.period)) return;
    if (sortKeyOf(parent.sort) !== target.sortKey) return;
    const segmentStart = parent.pagination === null ? 0 : parent.pagination.segment_index * parent.pagination.segment_size;
    const row = parent.rows[target.position - segmentStart];
    if (row !== undefined) onSelect(row.capability);
  }, [child, onSelect, parent]);
  const outcome = portfolioOutcomeCopy(parent.outcome);
  // An open campaign fills the surface alone (the mockup's two-view composition on one route);
  // the breadcrumb published by the detail returns to the portfolio.
  if (child !== null) {
    return <div className="campaign-portfolio">
      <CampaignDetail detail={child} focusSelectionReference={focusSelectionReference} focusSessionId={focusSessionId} onClear={onClear} scorecardStep={detailStep} />
    </div>;
  }
  if (selection.kind === 'loading') {
    return <div className="campaign-portfolio"><DetailSkeleton /></div>;
  }
  const filterId = parent.channel_filter.selected;
  const view = parent.channel_view;
  const windowText = formatIsoDateRange(parent.period.start, parent.period.end);
  // Under a multi-channel option divergence isn't scheduled; the server line says what runs.
  const slots = view === null ? parent.insights : parent.insights.filter((slot) => slot.id !== 'divergence');
  return <div className="campaign-portfolio">
    {fallback === 'link' ? <p className="state-note" role="status"><span className="badge">Channel not recognised</span>All channels is shown. The channel in the link isn&apos;t offered on this tab.</p> : null}
    {fallback === 'window' ? <p className="state-note" role="status"><span className="badge">Channel not offered</span>All channels is shown. The channel you chose isn&apos;t offered for this window.</p> : null}
    {closedCampaign === null ? null : <div className="state-note" role="status" aria-live="polite">
      <span className="badge">Campaign closed</span>
      <strong>{closedCampaign} isn&apos;t on this page of the list for {windowText}.</strong>
      <span>Use Find a campaign to open it.</span>
    </div>}
    {parent.outcome === 'complete'
      ? <CoverageProvider status={parent.evidence_status} subject="Campaigns">
        <AnswerSentence answer={campaignsAnswer(parent, portfolioOutcomeCopy(parent.outcome).description)} />
        <PortfolioTiles artifact={parent} filterId={filterId} focus={portfolioFocus} sessionId={focusSessionId} temporalSelection={focusTemporalSelection} />
        <ReturnLists parent={parent} sessionId={focusSessionId} temporalSelection={focusTemporalSelection} />
        <InsightRow
          campaignSegment={parent.pagination?.segment_index ?? 0}
          campaignSort={parent.sort}
          filterId={filterId}
          line={view?.insights_line ?? null}
          nextStepHandlers={portfolioSteps}
          period={parent.period}
          sessionId={focusSessionId}
          slots={slots}
          surfaceKey="campaign-portfolio"
          temporalSelection={focusTemporalSelection}
          title="Insights"
        />
        <PortfolioTrend artifact={parent} />
        <PortfolioTable artifact={parent} onSelect={onSelect} selection={selection} {...(onFilter === undefined ? {} : { onFilter })} {...(onSort === undefined ? {} : { onSort })} {...(onSegment === undefined ? {} : { onSegment })} {...(onSegmentApproach === undefined ? {} : { onSegmentApproach })} />
        {/* UI refresh 3.2 (readout A.4): delivery per channel is a media buyer's detail, so it folds below the table. */}
        {view === null ? null : <details className="about cp-delivery" data-slot="cp-delivery">
          <summary>{DELIVERY_SUMMARY}</summary>
          <ByChannel
            comparedText={`${selectedLabel(parent.channel_filter)} · ${windowText} compared with ${formatIsoDateRange(parent.prior_period.start, parent.prior_period.end)}`}
            scorecardStep={scorecardStep}
            view={view}
          />
        </details>}
      </CoverageProvider>
      : <ClosedState description={outcome.description} title={outcome.title} />}
    {selection.kind === 'unavailable' ? <p role="alert" className="state-note bad"><span className="badge">Unavailable</span>{issueCopy(selection.issueClass)}</p> : null}
  </div>;
}
