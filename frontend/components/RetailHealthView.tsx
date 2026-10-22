// SPDX-License-Identifier: MIT-0
'use client';

import { useRef, useState } from 'react';

import { formatCanonicalDecimal, formatIsoDate, formatIsoDateRange } from '@/lib/exact-format';
import type { InsightNextStepHandlers } from '@/lib/insight-next-step';
import type { MetricFocusV4 } from '@/lib/metric-focus';
import { fetchInsightDeepDive, fetchRetailMetricFocus, type TemporalSelectionV1 } from '@/lib/presentation-v2';
import type { Metric, RetailComparison, RetailHealthArtifact } from '@/lib/retail-health';
import { ChartLegend, SeriesChart, type ValueKind } from './ChartPrimitives';
import { CoverageProvider, FigureChange, useCoverageText } from './Coverage';
import { DataTable, DtBody, DtCell, DtColumn, DtHead, DtRow, DtRowHeader } from './DataTable';
import { InsightCard } from './InsightCard';
import { InsightFocusDialog, type InsightFocusSurface } from './InsightFocusDialog';
import { MetricFocusDialog, type MetricFocusSurface } from './MetricFocusDialog';
import { useVisibleTabs } from './NextStepContext';
import { RetailTileGroups, retailMetricLabel, retailValue } from './RetailTiles';

type TotalMetric = Metric;
type InsightSlot = RetailHealthArtifact['insights'][number];
type ProductRow = RetailHealthArtifact['rows'][number];

/** Metric identifiers whose values chart in the trend panel, in the mockup's switcher order. */
const TREND_SWITCHER = ['retail_revenue_shipped', 'units_shipped', 'glance_views', 'customer_returns'] as const;
/** Position of each chartable metric inside a trend point's value arrays (artifact column order). */
const TREND_COLUMNS = ['retail_revenue_shipped', 'units_shipped', 'customer_returns', 'glance_views'] as const;
/** The insight cards in the mockup's order: the movers first, then the shipping, demand and returns reads. */
const INSIGHT_ORDER: readonly InsightSlot['id'][] = ['retail-movers', 'ordered-vs-shipped', 'demand-shape', 'returns-pressure'];

function metricLabel(metric: TotalMetric): string {
  return retailMetricLabel(metric);
}

function switcherLabel(metric: typeof TREND_SWITCHER[number]): string {
  return metric === 'customer_returns' ? 'Returns' : metricLabel(metric);
}

export function insightTitle(id: InsightSlot['id']): string {
  return {
    'demand-shape': 'Demand',
    'ordered-vs-shipped': 'Ordered vs shipped',
    'retail-movers': 'Largest movers',
    'returns-pressure': 'Returns pressure',
  }[id];
}

/** A link to another tab only when this session's manifest lists it, never a dead link. */
export function ManifestPointer({ lead, tabId, text }: { readonly lead: string; readonly tabId: string; readonly text: string }) {
  const tab = useVisibleTabs().find((candidate) => candidate.tab_id === tabId);
  if (tab === undefined) return null;
  return <p className="pointer">{lead} <a href={tab.route}>{text}</a></p>;
}

/** The paid-feature section both retail pages show, in the server's words (ia §8.4). */
export function AmcRetailPurchases({ headingId, text }: { readonly headingId: string; readonly text: string }) {
  return <section className="metric-section" aria-labelledby={headingId}>
    <div className="sec-head"><h2 id={headingId}>Retail purchases from Amazon Marketing Cloud</h2></div>
    <div className="state-note paid">
      <span className="badge">Paid feature</span>
      <strong>Available with the paid feature</strong>
      <span>{text}</span>
    </div>
  </section>;
}

/* ---------- title band ---------- */

export function RetailHealthBandTitle({ artifact, product = null }: {
  readonly artifact: RetailHealthArtifact | null;
  /** The product open in place; detail shares the parent's window and freshness (ADR-125). */
  readonly product?: string | null;
}) {
  return <>
    <div className="scopes">
      {product === null ? null : <span className="pill accent wrap">{product}</span>}
      {artifact === null || artifact.outcome !== 'complete'
        ? <>
          <span className="pill">Retail actuals · shipped basis</span>
          <span className="pill">Reporting window unavailable</span>
        </>
        : <>
          <span className="pill">{artifact.total_count} authorized {artifact.total_count === 1 ? 'product' : 'products'}</span>
          <span className="pill">Retail actuals · shipped basis</span>
          <span className="pill">{artifact.distributor_view === 'MANUFACTURING' ? 'Manufacturing view' : 'Sourcing view'}</span>
          {artifact.reconciliation === null ? null : artifact.reconciliation.outcome === 'reconciled'
            ? <span className="pill good">Reconciled to reported totals · loaded {formatIsoDate(artifact.reconciliation.as_of)}</span>
            : <span className="pill warn">Totals differ from reported · loaded {formatIsoDate(artifact.reconciliation.as_of)}</span>}
        </>}
    </div>
  </>;
}

export function RetailHealthBandTitleSkeleton() {
  return <span className="skel h-line short" />;
}

/** The page's skeleton; a loading product names itself, as its h1 and crumb already do. */
export function RetailHealthSkeleton({ subject = 'retail health' }: { readonly subject?: string }) {
  return <div role="status" aria-label={`Loading ${subject}`}>
    {[0, 1].map((section) => <div className="metric-section" aria-hidden="true" key={section}>
      <span className="skel h-label" />
      <div className="tiles">
        {[0, 1, 2, 3].map((item) => <div className="tile static" key={item}><span className="skel h-label" /><span className="skel h-value" /><span className="skel h-line short" /></div>)}
      </div>
    </div>)}
    <div className="panel" aria-hidden="true">
      <span className="skel h-label" />
      <span className="skel h-chart" />
    </div>
    <p className="loading-note"><span className="spin" aria-hidden="true" />Loading {subject}…</p>
  </div>;
}

/* ---------- trend panel ---------- */

function trendSeries(artifact: RetailHealthArtifact, metric: typeof TREND_SWITCHER[number], series: 'current' | 'prior'): readonly (number | string)[] {
  const column = TREND_COLUMNS.indexOf(metric);
  return artifact.trend.map((point) => point[series][column]?.value ?? '');
}

function RetailTrend({ artifact }: { readonly artifact: RetailHealthArtifact }) {
  const [selected, setSelected] = useState<typeof TREND_SWITCHER[number]>('retail_revenue_shipped');
  const kind: ValueKind = selected === 'retail_revenue_shipped' ? 'currency' : 'count';
  const currencyCode = artifact.trend[0]?.current[TREND_COLUMNS.indexOf(selected)]?.units.currency_code ?? null;
  return <section className="panel" aria-label="Retail trend">
    <div className="panel-head">
      <div>
        <p className="eyebrow">Visual evidence</p>
        <h2>{metricLabel(selected)}</h2>
        <p className="footnote">{formatIsoDateRange(artifact.period.start, artifact.period.end)} · daily</p>
      </div>
      <div className="controls">
        <div className="seg" role="group" aria-label="Retail trend metric">
          {TREND_SWITCHER.map((name) => <button key={name} type="button" aria-pressed={selected === name} onClick={() => setSelected(name)}>{switcherLabel(name)}</button>)}
        </div>
        <div className="seg" role="group" aria-label="Available chart grain">
          <button type="button" aria-pressed="true">day</button>
        </div>
      </div>
    </div>
    <SeriesChart
      ariaLabel={`${metricLabel(selected)} across the returned daily reporting values`}
      currencyCode={currencyCode}
      current={trendSeries(artifact, selected, 'current')}
      dates={artifact.trend.map((point) => point.date)}
      kind={kind}
      prior={trendSeries(artifact, selected, 'prior')}
      zeroBased
    />
    <ChartLegend prior />
  </section>;
}

/* ---------- reconciliation ---------- */

function Reconciliation({ reconciliation }: { readonly reconciliation: NonNullable<RetailHealthArtifact['reconciliation']> }) {
  if (reconciliation.outcome === 'reconciled') {
    return <div className="state-note good" role="status">
      <span className="badge">Reconciled</span>
      <strong>By-product rows reconcile to the vendor&apos;s reported totals for this window.</strong>
      <span>Shipped revenue and units sum exactly to the separately delivered control totals; a mismatch renders here as a dated discrepancy, never a silent blend.</span>
    </div>;
  }
  const figure = (value: number | string | null): string => value === null ? 'not reported' : formatCanonicalDecimal(typeof value === 'number' ? String(value) : value);
  return <div className="state-note bad" role="status">
    <span className="badge">Discrepancy</span>
    <strong>By-product rows differ from the vendor&apos;s reported totals for this window.</strong>
    <span>
      Reported on {formatIsoDate(reconciliation.as_of)}: shipped revenue {figure(reconciliation.reported_revenue)} against {figure(reconciliation.product_revenue)} from product rows; units {figure(reconciliation.reported_units)} against {figure(reconciliation.product_units)}.
    </span>
  </div>;
}

/* ---------- product table ---------- */

function rowComparison(row: ProductRow, metric: TotalMetric): RetailComparison | null {
  return row.comparisons.find((comparison) => comparison.metric === metric) ?? null;
}

function rowCell(row: ProductRow, metric: TotalMetric): string {
  const comparison = rowComparison(row, metric);
  return comparison === null ? 'Not available' : retailValue(metric, comparison.current);
}

function ProductTable({ artifact, loadingReference, onOpenProduct }: {
  readonly artifact: RetailHealthArtifact;
  readonly loadingReference: string | null;
  readonly onOpenProduct: ((row: ProductRow) => void) | undefined;
}) {
  // A partly reported column names its covered days on the header and on each stacked cell
  // (ADR-118 decision 5, contract §2.4), as on Campaigns.
  const coverageText = useCoverageText();
  const cellBasis = (metric: TotalMetric) => ({ coverage: coverageText(metric), text: null });
  const windowText = formatIsoDateRange(artifact.period.start, artifact.period.end);
  const priorText = formatIsoDateRange(artifact.prior_period.start, artifact.prior_period.end);
  return <section className="metric-section" aria-labelledby="retail-products-heading">
    <div className="sec-head"><h2 id="retail-products-heading">Products</h2></div>
    <DataTable
      className="camp-table retail-products"
      title="Shipped revenue per product"
      note={`Vendor Central · shipped revenue · ${windowText} compared with ${priorText}`}
    >
      <DtHead>
        <DtRow>
          <DtColumn>Product</DtColumn>
          <DtColumn className="num" basis={coverageText('retail_revenue_shipped')}>Shipped revenue</DtColumn>
          <DtColumn className="num">Change</DtColumn>
          <DtColumn className="num" basis={coverageText('units_shipped')}>Shipped units</DtColumn>
          <DtColumn className="num" basis={coverageText('return_rate')}>Return rate</DtColumn>
          <DtColumn className="l">Status</DtColumn>
        </DtRow>
      </DtHead>
      <DtBody>
        {artifact.rows.map((row) => {
          const revenue = rowComparison(row, 'retail_revenue_shipped');
          const reference = row.selection_reference;
          return <DtRow key={row.child_asin}>
            <DtRowHeader className="name">
              {reference === null || onOpenProduct === undefined
                ? <><strong>{row.title}</strong><span className="sub">{row.child_asin}</span></>
                : <button
                  type="button"
                  data-product-row={row.child_asin}
                  aria-busy={loadingReference === reference ? true : undefined}
                  disabled={loadingReference !== null}
                  onClick={() => onOpenProduct(row)}
                >
                  <span className="link-text">{row.title}</span>
                  <span className="sub">{row.child_asin}</span>
                </button>}
            </DtRowHeader>
            <DtCell className="num" label="Shipped revenue" basis={cellBasis('retail_revenue_shipped')}><strong>{revenue === null ? 'Not available' : retailValue('retail_revenue_shipped', revenue.current)}</strong></DtCell>
            <DtCell className="num" label="Change"><FigureChange change={revenue?.change ?? null} figure={`Shipped revenue, ${row.title}`} slot="retail_revenue_shipped" /></DtCell>
            <DtCell className="num" label="Shipped units" basis={cellBasis('units_shipped')}>{rowCell(row, 'units_shipped')}</DtCell>
            <DtCell className="num" label="Return rate" basis={cellBasis('return_rate')}>{rowCell(row, 'return_rate')}</DtCell>
            <DtCell className="l" label="Status">
              {row.freshness_qualifiers.length === 0
                ? <span className="pill">Current</span>
                : <span className="pill warn">{row.freshness_qualifiers.join(' · ')}</span>}
            </DtCell>
          </DtRow>;
        })}
      </DtBody>
    </DataTable>
    <p className="footnote">
      {artifact.truncation === null ? '' : `Showing the first ${artifact.truncation} of ${artifact.total_count} products, by shipped revenue. `}
      {onOpenProduct === undefined || artifact.rows.every((row) => row.selection_reference === null)
        ? 'Product detail opens once this window is baked again.'
        : 'Choose a product to see its detail for this window.'}
    </p>
  </section>;
}

/* ---------- page ---------- */

export function RetailHealthView({ artifact, closedProduct = null, loadingReference = null, onOpenProduct, sessionId = '', temporalSelection }: {
  readonly artifact: RetailHealthArtifact;
  /** The product a window change closed because no single row matched it (ADR-125). */
  readonly closedProduct?: { readonly title: string } | null;
  readonly loadingReference?: string | null;
  readonly onOpenProduct?: (row: ProductRow, origin?: HTMLButtonElement) => void;
  readonly sessionId?: string;
  readonly temporalSelection?: TemporalSelectionV1 | undefined;
}) {
  const [focusMetric, setFocusMetric] = useState<TotalMetric | null>(null);
  const [focusCache] = useState(() => new Map<string, MetricFocusV4>());
  const [focusInsight, setFocusInsight] = useState<InsightSlot['id'] | null>(null);
  const origin = useRef<HTMLButtonElement | null>(null);
  const insightOrigin = useRef<HTMLButtonElement | null>(null);
  if (artifact.outcome !== 'complete') {
    return <section className="state-note" role="status">
      <span className="badge">Unavailable</span>
      <strong>Retail reporting is not available for this period</strong>
      <span>Choose another available reporting period when published Vendor coverage is available.</span>
    </section>;
  }
  const windowText = formatIsoDateRange(artifact.period.start, artifact.period.end);
  const neighbors = artifact.totals.map((comparison) => comparison.metric);
  const surface: MetricFocusSurface = {
    cache: focusCache,
    pageId: 'retail-health',
    cacheScope: artifact.insights[0]?.artifact_revision ?? `window:${windowText}`,
    askSeed: (focus) => `Ask about ${focus.metric.label} for retail health from ${windowText}.`,
    fetch: (metricId, signal) => fetchRetailMetricFocus(sessionId, metricId, temporalSelection, signal),
    deepFor: (focus) => ({
      cacheKey: `retail-health:metric-focus:${focus.metric.id}:${focus.insight.artifact_revision}:${focus.period.start}:${focus.period.end}`,
      expectedArtifactRevision: focus.insight.artifact_revision,
      fetch: (signal) => fetchInsightDeepDive({
        session_id: sessionId,
        slot_id: 'retail-metric-focus',
        metric: focus.metric.id,
        ...(temporalSelection === undefined ? {} : { temporal_selection: temporalSelection }),
      }, signal),
      window: focus.period,
    }),
    labelFor: (metricId) => metricLabel(metricId as TotalMetric),
    neighbors,
    window: artifact.period,
  };
  const openFocus = (metric: TotalMetric, source: HTMLButtonElement) => {
    origin.current = source;
    setFocusMetric(metric);
  };
  const closeFocus = () => setFocusMetric(null);
  const rowsByReference = new Map(artifact.rows.flatMap((row) => row.selection_reference === null ? [] : [[row.selection_reference, row] as const]));
  // A card's metric step opens the same focus dialog as that metric's tile (ADR-122), and the
  // movers card's product step opens that product in place (ADR-125 decision 5).
  const nextStepHandlers: InsightNextStepHandlers = {
    open_metric_focus: (target, source) => {
      const metric = neighbors.find((item) => item === target.metric);
      if (metric !== undefined) openFocus(metric, source);
    },
    ...(onOpenProduct === undefined ? {} : {
      open_product_detail: (target: { readonly selection_reference: string }, source: HTMLButtonElement) => {
        const row = rowsByReference.get(target.selection_reference);
        if (row !== undefined) {
          setFocusInsight(null);
          onOpenProduct(row, source);
        }
      },
    }),
    focusableMetrics: neighbors,
  };
  const openInsight = (id: InsightSlot['id'], source: HTMLButtonElement) => {
    insightOrigin.current = source;
    setFocusInsight(id);
  };
  const closeInsight = () => setFocusInsight(null);
  const insightSlots = INSIGHT_ORDER.map((id) => artifact.insights.find((slot) => slot.id === id)).filter((slot) => slot !== undefined);
  const insightSurface: InsightFocusSurface = {
    labelFor: (slotId) => insightTitle(slotId as InsightSlot['id']),
    deepFor: (slot) => slot.id === 'retail-movers' ? null : ({
      cacheKey: `retail-health:${slot.id}:${slot.artifact_revision}:${artifact.period.start}:${artifact.period.end}`,
      expectedArtifactRevision: slot.artifact_revision,
      fetch: (signal) => fetchInsightDeepDive({
        session_id: sessionId,
        slot_id: slot.id,
        ...(temporalSelection === undefined ? {} : { temporal_selection: temporalSelection }),
      }, signal),
      window: artifact.period,
    }),
    nextStepHandlers,
    pageId: 'retail-health',
    slots: insightSlots,
    window: artifact.period,
  };
  const statuses = artifact.fixed_statuses;
  return <CoverageProvider status={artifact.evidence_status} subject="Retail health"><div className="retail-health">
    {closedProduct === null ? null : <div className="state-note" role="status">
      <span className="badge">Product closed</span>
      <strong>{closedProduct.title} isn&apos;t in the product list for {windowText}.</strong>
      <span>Product detail closed. Choose a product from the list to open its detail for this window.</span>
    </div>}
    <RetailTileGroups
      absences={artifact.absences}
      idPrefix="retail-section"
      onOpen={openFocus}
      returnReasons={statuses.return_reasons.reason}
      scope="page"
      totals={artifact.totals}
    />
    <section className="metric-section" aria-labelledby="retail-insights-heading">
      <div className="sec-head"><h2 id="retail-insights-heading">Insights</h2></div>
      <div className="narr-row">
        {insightSlots.map((slot) => <InsightCard
          key={slot.id}
          nextStepHandlers={nextStepHandlers}
          onOpen={(source) => openInsight(slot.id as InsightSlot['id'], source)}
          pageId="retail-health"
          slot={slot}
          title={insightTitle(slot.id as InsightSlot['id'])}
        />)}
      </div>
    </section>
    <RetailTrend artifact={artifact} />
    <ProductTable
      artifact={artifact}
      loadingReference={loadingReference}
      onOpenProduct={onOpenProduct === undefined ? undefined : (row) => onOpenProduct(row)}
    />
    {artifact.reconciliation === null ? null : <Reconciliation reconciliation={artifact.reconciliation} />}
    <AmcRetailPurchases headingId="retail-amc-heading" text={statuses.amc_retail_purchases.text} />
    <ManifestPointer
      lead="Repeat purchases per product, from Amazon Brand Analytics:"
      tabId="repeat-purchase-by-product"
      text="Repeat purchase by product"
    />
    <div className="state-note">
      <span className="badge">About these figures</span>
      <strong>Retail actuals are not campaign outcomes.</strong>
      <span>{artifact.boundary}</span>
    </div>
    {focusMetric !== null ? <MetricFocusDialog
      key={`retail-${windowText}`}
      metricId={focusMetric}
      onClose={closeFocus}
      opener={origin}
      onMetricChange={(metricId) => setFocusMetric(metricId as TotalMetric)}
      surface={surface}
      surfaceKey="retail-health"
    /> : null}
    {focusInsight !== null ? <InsightFocusDialog
      onClose={closeInsight}
      opener={insightOrigin}
      onSlotChange={(slotId) => setFocusInsight(slotId as InsightSlot['id'])}
      slotId={focusInsight}
      surface={insightSurface}
      surfaceKey="retail-health"
    /> : null}
  </div></CoverageProvider>;
}
