// SPDX-License-Identifier: MIT-0
'use client';
/**
 * Products and ads (UI refresh 3.7, readout A.10): each product's served shipped sales and page
 * views beside what one chosen ad channel credits to it, from the Products and ads part baked
 * with the page (render format 12). The two systems sit side by side and are never added,
 * divided or ranked; a row without a served ad section shows its served absence instead.
 */
import { useState } from 'react';

import { AnswerSentence } from './AnswerSentence';
import { ChangeChip } from './ChartPrimitives';
import { CoverageProvider, FigureChange } from './Coverage';
import { DataTable, DtBody, DtCell, DtColumn, DtHead, DtRow, DtRowHeader } from './DataTable';
import { useVisibleTabs } from './NextStepContext';
import { RETAIL_HEALTH_EMPTY_TEXT, RETAIL_HEALTH_EMPTY_TITLE, RETAIL_HEALTH_INSUFFICIENT_TEXT, RETAIL_HEALTH_INSUFFICIENT_TITLE } from './RetailHealthRouteAdapter';
import { retailValue } from './RetailTiles';
import { windowText } from '@/lib/answer';
import { adChannelOptions, adValueText, productAdCell, productsAndAdsAnswer, PRODUCTS_AND_ADS_COPY } from '@/lib/products-and-ads';
import type { RetailHealthArtifact } from '@/lib/retail-health';
import type { RetailProductAdsArtifact, RetailProductAdsRow } from '@/lib/retail-health-product';

/** The tab's browser-owned framing lines (`docs/ui/products-and-ads/README.md` "Copy"). */
export const PRODUCTS_AND_ADS_LINES = {
  channel: 'Ad channel',
  noProductReturn: "There's no return on ad spend per product, because ad spend isn't recorded per product.",
  retailBasis: 'Vendor Central · shipped basis',
  retailLink: 'Retail health ›',
  tableTitle: 'Products, top by shipped sales',
  twoSystems: 'Two systems side by side. They never add up or divide into each other.',
} as const;

type ProductRow = RetailHealthArtifact['rows'][number];

function rowValue(row: ProductRow, metric: 'retail_revenue_shipped' | 'glance_views'): string {
  const comparison = row.comparisons.find((entry) => entry.metric === metric);
  return comparison === undefined ? 'Not available' : retailValue(metric, comparison.current);
}

/** Units per 1,000 page views from the product's served conversion, or null when not served. */
function conversion(ads: RetailProductAdsRow | undefined): string | null {
  return ads?.conversion == null ? null : retailValue('conversion_rate_retail', ads.conversion);
}

export function ProductsAndAdsView({ artifact, productAds }: {
  readonly artifact: RetailHealthArtifact;
  /** The part's rows are the artifact's rows in order (its decoder checks); null with no rows. */
  readonly productAds: RetailProductAdsArtifact | null;
}) {
  const options = adChannelOptions(productAds);
  const [chosen, setChosen] = useState<string | null>(null);
  const retailTab = useVisibleTabs().find((tab) => tab.tab_id === 'retail-health');
  if (artifact.outcome !== 'complete') {
    // The same E1 copy as Retail health: what's missing, why, and who acts.
    const empty = artifact.outcome === 'empty';
    return <section className="state-note" role="status">
      <span className="badge">{empty ? 'No movement' : 'Not enough data'}</span>
      <strong>{empty ? RETAIL_HEALTH_EMPTY_TITLE : RETAIL_HEALTH_INSUFFICIENT_TITLE}</strong>
      <span>{empty ? RETAIL_HEALTH_EMPTY_TEXT : RETAIL_HEALTH_INSUFFICIENT_TEXT}</span>
    </section>;
  }
  // A channel a refreshed window no longer credits falls back to the first one served.
  const channel = options.find((option) => option.channel === chosen) ?? options[0] ?? null;
  const showConversion = (productAds?.rows ?? []).some((row) => conversion(row) !== null);
  const adBasis = productAds !== null && productAds.rows.some((row) => row.reason === null) ? productAds.basis : null;
  const adHeading = channel === null ? 'Ad-credited sales' : `Ad-credited sales · ${channel.label}`;
  return <CoverageProvider status={artifact.evidence_status} subject="Products and ads"><div className="products-ads">
    <AnswerSentence answer={productsAndAdsAnswer(artifact, '')} />
    {options.length < 2 ? null : <div className="seg channel-seg" role="group" aria-label={PRODUCTS_AND_ADS_LINES.channel}>
      {options.map((option) => <button
        aria-pressed={channel?.channel === option.channel}
        key={option.channel}
        onClick={() => setChosen(option.channel)}
        type="button"
      >{option.label}</button>)}
    </div>}
    <p className="footnote" data-slot="pa-two-systems">{PRODUCTS_AND_ADS_LINES.twoSystems}</p>
    <DataTable
      className="camp-table pa-table"
      title={PRODUCTS_AND_ADS_LINES.tableTitle}
      note={`${PRODUCTS_AND_ADS_LINES.retailBasis} · ${windowText(artifact.period)} compared with ${windowText(artifact.prior_period)}${adBasis === null ? '' : ` | ${adBasis}`}`}
    >
      <DtHead><DtRow>
        <DtColumn>Product</DtColumn>
        <DtColumn className="num">Shipped sales</DtColumn>
        <DtColumn className="num">Change</DtColumn>
        <DtColumn className="num">Product page views</DtColumn>
        {showConversion ? <DtColumn className="num">Units per 1,000 page views</DtColumn> : null}
        <DtColumn className="num">{adHeading}</DtColumn>
        <DtColumn className="num">Ad-credited purchases</DtColumn>
        <DtColumn className="num">First-time purchases</DtColumn>
      </DtRow></DtHead>
      <DtBody>{artifact.rows.map((row, index) => {
        const ads = productAds?.rows[index];
        const cell = ads === undefined ? { kind: 'absent' as const, text: PRODUCTS_AND_ADS_COPY.notCredited } : productAdCell(ads, channel?.channel ?? null);
        const revenue = row.comparisons.find((entry) => entry.metric === 'retail_revenue_shipped') ?? null;
        return <DtRow key={row.child_asin} data-slot={`pa-${row.child_asin}`}>
          <DtRowHeader className="name"><strong>{row.title}</strong><span className="sub">{row.child_asin}</span></DtRowHeader>
          <DtCell className="num" label="Shipped sales"><strong>{rowValue(row, 'retail_revenue_shipped')}</strong></DtCell>
          <DtCell className="num" label="Change"><FigureChange change={revenue?.change ?? null} figure={`Shipped sales, ${row.title}`} slot="retail_revenue_shipped" /></DtCell>
          <DtCell className="num" label="Product page views">{rowValue(row, 'glance_views')}</DtCell>
          {showConversion ? <DtCell className="num" label="Units per 1,000 page views">{conversion(ads) ?? 'Not available'}</DtCell> : null}
          {cell.kind === 'absent'
            ? <DtCell className="num pa-absent" label={adHeading}><span className="reason">{cell.text}</span></DtCell>
            : <DtCell className="num" label={adHeading}><span>{adValueText(cell.row.revenue)} <ChangeChip change={cell.row.revenue_change} /></span></DtCell>}
          <DtCell className="num" label="Ad-credited purchases">{cell.kind === 'absent' ? <><span aria-hidden="true">—</span><span className="sr-only">{cell.text}</span></> : adValueText(cell.row.purchases)}</DtCell>
          <DtCell className="num" label="First-time purchases"><span className="reason">{PRODUCTS_AND_ADS_COPY.notMeasured}</span></DtCell>
        </DtRow>;
      })}</DtBody>
    </DataTable>
    <p className="footnote" data-slot="pa-no-roas">{PRODUCTS_AND_ADS_LINES.noProductReturn}</p>
    {retailTab === undefined ? null : <p className="pointer"><a href={retailTab.route}>{PRODUCTS_AND_ADS_LINES.retailLink}</a></p>}
  </div></CoverageProvider>;
}
