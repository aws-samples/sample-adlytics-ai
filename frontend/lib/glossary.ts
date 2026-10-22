// SPDX-License-Identifier: MIT-0
/**
 * Customer labels for governed metrics and attribution models (UI refresh D5, cross-cutting
 * §2.1). A served label is baked into the page artifact, so it can't change before the next
 * bake; this one table relabels it in the browser by governed id. Only the words change: the
 * metric, its value and its basis stay as served. `docs/glossary.md` owns the customer label.
 */
const METRIC_LABELS: Readonly<Record<string, string>> = {
  ad_spend: 'Ad spend',
  amazon_margin_proxy: "Amazon's margin on your products (not yours)",
  glance_views: 'Product page views',
  portfolio_roas_attributed_14d: 'Return on ad spend',
  purchaser_rate_index_vs_sponsored_only: 'Purchase rate compared with sponsored-only shoppers',
  roas_attributed_14d: 'Return on ad spend',
  roas_attributed_incl_halo_14d: 'Return incl. other products of your brand',
  roas_deduplicated_incl_halo_14d: 'Return on ad spend, each sale counted once',
  tacos: 'Ad spend as a share of shipped sales',
};

/** Every customer label the glossary writes: browser-owned copy, for the copy-walk allowlists. */
export const CUSTOMER_METRIC_LABELS: readonly string[] = Object.values(METRIC_LABELS);

/** The customer label for a governed metric, or the served label when the glossary has none. */
export function metricLabel(metric: string, served: string): string {
  return METRIC_LABELS[metric] ?? served;
}

/** Insight output classes in customer words (cross-cutting §2.1). */
export const INSIGHT_TAG_LABELS = {
  hypothesis: 'Idea to check',
  observed: 'Fact',
  perspective: 'Reading',
} as const;
