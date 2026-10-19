// SPDX-License-Identifier: MIT-0
'use client';

/**
 * Retail health › product detail (ADR-125; ia §7.3a), in place on /retail-health/ with the
 * parent's window and freshness. The tiles use the same four groups and headings as Retail
 * health; the ad-attributed table is per channel with no total, never added to shipped revenue
 * and with no product ROAS. Every figure and sentence is the server's.
 */

import { useRef, useState } from 'react';

import { formatCount, formatCurrency, formatIsoDate, formatIsoDateRange } from '@/lib/exact-format';
import type { MetricFocusV4 } from '@/lib/metric-focus';
import { fetchRetailProductMetricFocus, type TemporalSelectionV1 } from '@/lib/presentation-v2';
import type { Metric } from '@/lib/retail-health';
import type { RetailAdValue, RetailDailyPoint, RetailProductDetailArtifact } from '@/lib/retail-health-product';
import { ChangeChip, ChartLegend, numberText, SeriesChart } from './ChartPrimitives';
import { CoverageProvider } from './Coverage';
import { DataTable, DtBody, DtCell, DtColumn, DtHead, DtRow, DtRowHeader } from './DataTable';
import { Icon } from './Icon';
import { InsightCard } from './InsightCard';
import { InsightFocusDialog, type InsightDialogSlot, type InsightFocusSurface } from './InsightFocusDialog';
import { MetricFocusDialog, type MetricFocusSurface } from './MetricFocusDialog';
import { AmcRetailPurchases, insightTitle, ManifestPointer } from './RetailHealthView';
import { RetailTileGroups, retailMetricLabel } from './RetailTiles';

/** The product's place in the list, for previous and next (ia §7.3a item 1). */
export interface ProductNeighbours {
  readonly index: number;
  readonly next: (() => void) | null;
  readonly previous: (() => void) | null;
  readonly total: number;
}

function adValueText(value: RetailAdValue): string {
  const source = typeof value.value === 'number' ? String(value.value) : value.value;
  if (value.units.kind === 'currency' && value.units.currency_code !== null && value.display_value !== null) {
    return formatCurrency(value.display_value, value.units.currency_code);
  }
  return formatCount(source);
}

/**
 * One daily chart, zero-based because it plots a currency or a count, with its exact values
 * (ADR-118 decision 7). The table prints the served values; nothing here totals them.
 */
function DailyChart({ label, points, priorText, title, windowText }: {
  readonly label: string;
  readonly points: readonly RetailDailyPoint[];
  readonly priorText: string;
  readonly title: string;
  readonly windowText: string;
}) {
  const first = points[0];
  const kind = first?.current.units.kind === 'currency' ? 'currency' : 'count';
  const currencyCode = first?.current.units.currency_code ?? null;
  // A negative day would put 0 inside the axis rather than at its foot, so the note would be wrong.
  const startsAtZero = points.every((point) => Number(point.current.value) >= 0 && Number(point.prior.value) >= 0);
  return <div className="panel product-daily">
    <h3>{title}</h3>
    <SeriesChart
      ariaLabel={label}
      currencyCode={currencyCode}
      current={points.map((point) => point.current.value)}
      dates={points.map((point) => point.date)}
      kind={kind}
      prior={points.map((point) => point.prior.value)}
      zeroBased
    />
    <ChartLegend prior />
    <details className="exact">
      <summary>Show values</summary>
      <div className="scroll" tabIndex={0}>
        <table>
          <caption className="sr-only">{title}, exact values</caption>
          <thead><tr><th scope="col">Day</th><th scope="col">{windowText}</th><th scope="col">{priorText}</th></tr></thead>
          <tbody>{points.map((point) => <tr key={point.date}>
            <th scope="row">{formatIsoDate(point.date)}</th>
            <td>{numberText(point.current.value, kind, currencyCode)}</td>
            <td>{numberText(point.prior.value, kind, currencyCode)}</td>
          </tr>)}</tbody>
        </table>
      </div>
    </details>
    {startsAtZero ? <p className="footnote">Axis starts at 0, because this is a {kind}.</p> : null}
  </div>;
}

export function RetailProductDetailView({ artifact, neighbours, onBack, selectionReference, sessionId = '', temporalSelection }: {
  readonly artifact: RetailProductDetailArtifact;
  readonly neighbours: ProductNeighbours | null;
  readonly onBack: () => void;
  readonly selectionReference: string;
  readonly sessionId?: string;
  readonly temporalSelection: TemporalSelectionV1;
}) {
  const [focusMetric, setFocusMetric] = useState<Metric | null>(null);
  const [focusCache] = useState(() => new Map<string, MetricFocusV4>());
  const [focusInsight, setFocusInsight] = useState<string | null>(null);
  const origin = useRef<HTMLButtonElement | null>(null);
  const insightOrigin = useRef<HTMLButtonElement | null>(null);
  const title = artifact.product.title;
  const windowText = formatIsoDateRange(artifact.period.start, artifact.period.end);
  const priorText = formatIsoDateRange(artifact.prior_period.start, artifact.prior_period.end);
  const neighbors = artifact.totals.map((comparison) => comparison.metric);
  const surface: MetricFocusSurface = {
    cache: focusCache,
    pageId: 'retail-health',
    cacheScope: `product:${selectionReference}`,
    askSeed: (focus) => `Ask about ${focus.metric.label} for ${title} from ${windowText}.`,
    fetch: (metricId, signal) => fetchRetailProductMetricFocus(sessionId, selectionReference, metricId, temporalSelection, signal),
    deepFor: () => null,
    labelFor: (metricId) => retailMetricLabel(metricId as Metric),
    neighbors,
    window: artifact.period,
  };
  const openFocus = (metric: Metric, source: HTMLButtonElement) => {
    origin.current = source;
    setFocusMetric(metric);
  };
  const slots: InsightDialogSlot[] = artifact.insights.map((insight) => insight.slot);
  const insightSurface: InsightFocusSurface = {
    labelFor: (slotId) => insightTitle(slotId as Parameters<typeof insightTitle>[0]),
    provenanceFor: (slot) => artifact.insights.find((insight) => insight.slot.id === slot.id)?.provenance ?? null,
    slots,
    statsFor: (slot) => artifact.insights.find((insight) => insight.slot.id === slot.id)?.stats ?? null,
    window: artifact.period,
  };
  const ads = artifact.ad_attributed;
  return <CoverageProvider status={artifact.evidence_status} subject={title}><div className="retail-product">
    <div className="detail-nav">
      <button type="button" className="btn link" onClick={onBack}><Icon name="back" small />Back to Retail health</button>
      <span className="pill">{artifact.product.child_asin}</span>
      {neighbours === null ? null : <>
        <span className="pill">{neighbours.index + 1} of {neighbours.total} products</span>
        <button type="button" className="btn" disabled={neighbours.previous === null} onClick={() => neighbours.previous?.()} aria-label="Previous product">‹ Previous</button>
        <button type="button" className="btn" disabled={neighbours.next === null} onClick={() => neighbours.next?.()} aria-label="Next product">Next ›</button>
      </>}
    </div>
    <RetailTileGroups
      absences={artifact.absences}
      idPrefix="product-section"
      onOpen={openFocus}
      returnReasons={artifact.fixed_statuses.return_reasons.reason}
      scope="product"
      totals={artifact.totals}
    />
    <section className="metric-section" aria-labelledby="product-insights-heading">
      <div className="sec-head"><h2 id="product-insights-heading">Insights that name this product</h2></div>
      {artifact.no_insight_line !== null
        ? <p className="sec-line">{artifact.no_insight_line}</p>
        : <>
          <p className="sec-line">From Retail health&apos;s insights for {windowText}. The dialog shows this product&apos;s own figures.</p>
          <div className="narr-row">
            {artifact.insights.map((insight) => <InsightCard
              key={insight.slot.id}
              onOpen={(source) => { insightOrigin.current = source; setFocusInsight(insight.slot.id); }}
              pageId="retail-health"
              slot={insight.slot}
              title={insightTitle(insight.slot.id)}
            />)}
          </div>
        </>}
    </section>
    <section className="metric-section" aria-labelledby="product-daily-heading">
      <div className="sec-head"><h2 id="product-daily-heading">By day</h2></div>
      <div className="product-daily-grid">
        <DailyChart label={`Shipped revenue by day, ${title}, against ${priorText}`} points={artifact.sales_daily} priorText={priorText} title={`Shipped revenue by day, ${title}`} windowText={windowText} />
        <DailyChart label={`Glance views by day, ${title}, against ${priorText}`} points={artifact.traffic_daily} priorText={priorText} title={`Glance views by day, ${title}`} windowText={windowText} />
      </div>
    </section>
    <section className="metric-section" aria-labelledby="product-ads-heading">
      <div className="sec-head"><h2 id="product-ads-heading">Ad-attributed sales</h2></div>
      {ads === null || ads.reason !== null
        ? <div className="state-note" role="status">
          <span className="badge">Not available</span>
          <strong>Not available for this product yet</strong>
          <span>{ads?.reason ?? ''}</span>
        </div>
        : <DataTable
          className="camp-table"
          title="Sales Amazon credits to ads for this product, per channel"
          note={`${ads.basis} · ${windowText} compared with ${priorText}`}
        >
          <DtHead>
            <DtRow>
              <DtColumn>Channel</DtColumn>
              <DtColumn className="num">Attributed purchases</DtColumn>
              <DtColumn className="num">Change in purchases</DtColumn>
              <DtColumn className="num">Attributed revenue</DtColumn>
              <DtColumn className="num">Change in revenue</DtColumn>
            </DtRow>
          </DtHead>
          <DtBody>
            {ads.rows.map((row) => <DtRow key={row.channel}>
              <DtRowHeader className="name">{row.label}</DtRowHeader>
              <DtCell className="num" label="Attributed purchases">{adValueText(row.purchases.current)}</DtCell>
              <DtCell className="num" label="Change in purchases"><ChangeChip change={row.purchases.change} /></DtCell>
              <DtCell className="num" label="Attributed revenue">{adValueText(row.revenue.current)}</DtCell>
              <DtCell className="num" label="Change in revenue"><ChangeChip change={row.revenue.change} /></DtCell>
            </DtRow>)}
          </DtBody>
        </DataTable>}
      <p className="footnote">
        Attributed sales aren&apos;t added across channels, because one purchase can be credited to more than one channel.
        They aren&apos;t added to shipped revenue either: Amazon Ads and Vendor Central are different measurement systems.
        There&apos;s no return on ad spend for one product, because ad spend isn&apos;t reported per product.
      </p>
      <ManifestPointer
        lead="New-to-brand purchases per product, with brand halo, are on"
        tabId="new-customers"
        text="Customers › New customers › Gateway products"
      />
    </section>
    <AmcRetailPurchases headingId="product-amc-heading" text={artifact.fixed_statuses.amc_retail_purchases.text} />
    <ManifestPointer
      lead="Repeat purchases for this product, from Amazon Brand Analytics, by month:"
      tabId="repeat-purchase-by-product"
      text="Repeat purchase by product"
    />
    <div className="state-note">
      <span className="badge">About these figures</span>
      <strong>Vendor Central figures for this one product.</strong>
      <ul>{artifact.boundaries.map((line) => <li key={line}>{line}</li>)}</ul>
    </div>
    {focusMetric !== null ? <MetricFocusDialog
      key={`product-${selectionReference}`}
      metricId={focusMetric}
      onClose={() => setFocusMetric(null)}
      opener={origin}
      onMetricChange={(metricId) => setFocusMetric(metricId as Metric)}
      surface={surface}
      surfaceKey="retail-product"
    /> : null}
    {focusInsight !== null ? <InsightFocusDialog
      onClose={() => setFocusInsight(null)}
      opener={insightOrigin}
      onSlotChange={setFocusInsight}
      slotId={focusInsight}
      surface={insightSurface}
      surfaceKey="retail-product"
    /> : null}
  </div></CoverageProvider>;
}
