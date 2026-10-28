// SPDX-License-Identifier: MIT-0
/**
 * Retail health › Products and ads (UI refresh 3.7, readout A.10, ADR-151): a composed tab over
 * the served Retail health rows and the `RetailProductAds` part baked with them (render format
 * 12), each row's ad credit projected from its product detail (ADR-125). One request serves both.
 * The browser reads every figure as served: it never adds the two systems, never divides one by
 * the other, and never ranks a product. There is no product return on ad spend, because ad
 * spend has no product grain.
 */
import { changeClause, type Answer, windowText } from './answer';
import { formatCount, formatCurrency } from './exact-format';
import type { ProductsAndAdsPresentation } from './presentation-v2';
import type { RetailHealthArtifact } from './retail-health';
import type { RetailAdValue, RetailProductAdCell, RetailProductAdsArtifact, RetailProductAdsRow } from './retail-health-product';

export type ProductsAndAdsParent = ProductsAndAdsPresentation;

export interface AdChannelOption {
  readonly channel: string;
  readonly label: string;
}

/** The channels the served part credits, in the order they're first served; never merged. */
export function adChannelOptions(ads: RetailProductAdsArtifact | null): readonly AdChannelOption[] {
  const options = new Map<string, string>();
  for (const row of ads?.rows ?? []) {
    for (const entry of row.channels) {
      if (!options.has(entry.channel)) options.set(entry.channel, entry.label);
    }
  }
  return [...options].map(([channel, label]) => ({ channel, label }));
}

/** A served ad value's text: currency from its two-place display value, a count grouped. */
export function adValueText(value: RetailAdValue): string {
  const source = typeof value.value === 'number' ? String(value.value) : value.value;
  if (value.units.kind === 'currency' && value.units.currency_code !== null && value.display_value !== null) {
    return formatCurrency(value.display_value, value.units.currency_code);
  }
  return value.units.kind === 'count' && !source.includes('.') ? formatCount(source) : source;
}

/** One row's ad cell state for the chosen channel. */
export type ProductAdCell =
  | { readonly kind: 'row'; readonly row: RetailProductAdCell }
  /** The part serves the product's own absence reason for its ad section. */
  | { readonly kind: 'absent'; readonly text: string };

/** Browser-owned words of the tab (`docs/ui/products-and-ads/README.md` "Copy"). */
export const PRODUCTS_AND_ADS_COPY = {
  notCredited: 'No credit served for this channel.',
  notMeasured: 'Not measured yet',
} as const;

/** One row's ad cell: the served absence, the chosen channel's credit, or no credit for it. */
export function productAdCell(ads: RetailProductAdsRow, channel: string | null): ProductAdCell {
  if (ads.reason !== null) return { kind: 'absent', text: ads.reason };
  const row = ads.channels.find((entry) => entry.channel === channel);
  return row === undefined ? { kind: 'absent', text: PRODUCTS_AND_ADS_COPY.notCredited } : { kind: 'row', row };
}

/**
 * The tab's answer (plan template): "{N} products shipped in {window}; {top title} shipped most,
 * {rev}." from the served count and the first served row, which the server orders by shipped
 * revenue. Its served change follows when one is comparable. Absent: `absenceText`.
 */
export function productsAndAdsAnswer(artifact: RetailHealthArtifact, absenceText: string): Answer {
  const first = artifact.rows[0];
  const revenue = first?.comparisons.find((entry) => entry.metric === 'retail_revenue_shipped') ?? null;
  const current = revenue?.current ?? null;
  if (artifact.outcome !== 'complete' || first === undefined || current === null || !('display_value' in current) || current.display_value === null || current.units.currency_code === null) {
    return { kind: 'fallback', text: absenceText };
  }
  const count = `${formatCount(String(artifact.total_count))} ${artifact.total_count === 1 ? 'product' : 'products'}`;
  const moved = revenue!.withheld ? null : changeClause(revenue!.change);
  const tail = moved === null ? '' : `, ${moved} on ${windowText(artifact.prior_period)}`;
  return {
    kind: 'answer',
    text: `${count} shipped in ${windowText(artifact.period)}; ${first.title} shipped most, ${formatCurrency(current.display_value, current.units.currency_code)}${tail}.`,
  };
}
