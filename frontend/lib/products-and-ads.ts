// SPDX-License-Identifier: MIT-0
/**
 * Retail health › Products and ads (UI refresh 3.7, readout A.10, ADR-151): a composed tab over
 * the served Retail health rows and each row's baked product detail (ADR-125). The browser reads
 * every figure as served: it never adds the two systems, never divides one by the other, and
 * never ranks a product. There is no product return on ad spend, because ad spend has no product
 * grain.
 */
import { changeClause, type Answer, windowText } from './answer';
import { formatCount, formatCurrency } from './exact-format';
import type { RetailHealthPresentation, RetailProductPresentation, TemporalSelectionV1 } from './presentation-v2';
import type { RetailHealthArtifact } from './retail-health';
import type { RetailAdChannelRow, RetailAdValue, RetailProductDetailArtifact } from './retail-health-product';

/** The served detail of one product row, or why the row shows no ad figures. */
export type ProductAds =
  | { readonly kind: 'served'; readonly detail: RetailProductDetailArtifact }
  /** The row serves no product-detail capability. */
  | { readonly kind: 'none' }
  /** The detail didn't load, or answered for another bake, window or product. */
  | { readonly kind: 'rejected' };

export type ProductsAndAdsParent = RetailHealthPresentation & {
  /** Keyed by the row's child ASIN; empty unless the parent is a complete body. */
  readonly products: ReadonlyMap<string, ProductAds>;
};

/** Plan 3.7: at most four product details in flight at once. */
export const PRODUCT_DETAIL_CONCURRENCY = 4;

type ProductRow = RetailHealthArtifact['rows'][number];
type DetailFetcher = (selectionReference: string, selection: TemporalSelectionV1, signal: AbortSignal) => Promise<RetailProductPresentation>;

function sameWindow(left: { readonly start: string; readonly end: string }, right: { readonly start: string; readonly end: string }): boolean {
  return left.start === right.start && left.end === right.end;
}

/**
 * Each row's served detail, at most `PRODUCT_DETAIL_CONCURRENCY` in flight. A detail is used only
 * when it answers for this parent's bake, window and product; anything else reads as rejected.
 */
export async function loadProductAds(
  parent: Extract<RetailHealthPresentation, { readonly kind: 'parent' }>,
  selection: TemporalSelectionV1,
  signal: AbortSignal,
  fetchDetail: DetailFetcher,
): Promise<ReadonlyMap<string, ProductAds>> {
  const result = new Map<string, ProductAds>();
  const rows = parent.artifact.outcome === 'complete' ? parent.artifact.rows : [];
  const queue: ProductRow[] = [];
  for (const row of rows) {
    if (row.selection_reference === null) result.set(row.child_asin, { kind: 'none' });
    else queue.push(row);
  }
  const worker = async () => {
    for (let row = queue.shift(); row !== undefined; row = queue.shift()) {
      try {
        const child = await fetchDetail(row.selection_reference as string, selection, signal);
        const detail = child.artifact;
        const matches = child.parent_revision === parent.artifact_revision &&
          sameWindow(detail.period, parent.artifact.period) &&
          detail.product.child_asin === row.child_asin;
        result.set(row.child_asin, matches ? { detail, kind: 'served' } : { kind: 'rejected' });
      } catch (error) {
        if (signal.aborted) throw error;
        result.set(row.child_asin, { kind: 'rejected' });
      }
    }
  };
  await Promise.all(Array.from({ length: Math.min(PRODUCT_DETAIL_CONCURRENCY, queue.length) }, worker));
  return result;
}

export interface AdChannelOption {
  readonly channel: string;
  readonly label: string;
}

/** The channels the served details credit, in the order they're first served; never merged. */
export function adChannelOptions(products: ReadonlyMap<string, ProductAds>, rows: readonly ProductRow[]): readonly AdChannelOption[] {
  const options = new Map<string, string>();
  for (const row of rows) {
    const ads = products.get(row.child_asin);
    if (ads?.kind !== 'served') continue;
    for (const entry of ads.detail.ad_attributed?.rows ?? []) {
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
  | { readonly kind: 'row'; readonly row: RetailAdChannelRow }
  /** The detail serves its own absence reason for the ad section. */
  | { readonly kind: 'absent'; readonly text: string };

/** Browser-owned words of the tab (`docs/ui/products-and-ads/README.md` "Copy"). */
export const PRODUCTS_AND_ADS_COPY = {
  detailUnavailable: "This product's ad figures can't be shown right now.",
  noCapability: 'Ad figures open once this window is baked again.',
  notCredited: 'No credit served for this channel.',
  notMeasured: 'Not measured yet',
} as const;

export function productAdCell(ads: ProductAds | undefined, channel: string | null): ProductAdCell {
  if (ads === undefined || ads.kind === 'rejected') return { kind: 'absent', text: PRODUCTS_AND_ADS_COPY.detailUnavailable };
  if (ads.kind === 'none') return { kind: 'absent', text: PRODUCTS_AND_ADS_COPY.noCapability };
  const section = ads.detail.ad_attributed;
  if (section === null) return { kind: 'absent', text: PRODUCTS_AND_ADS_COPY.detailUnavailable };
  if (section.reason !== null) return { kind: 'absent', text: section.reason };
  const row = section.rows.find((entry) => entry.channel === channel);
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
