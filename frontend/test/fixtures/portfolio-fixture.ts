// SPDX-License-Identifier: MIT-0
/**
 * Campaigns render format 11 on the wire (ADR-127, ADR-119): every body names its channel filter
 * and carries its coverage key. The decoder refuses the retired format-6, format-7 and format-10
 * forms, so hand-built fixtures pass through `asServedPortfolio` (fictional figures).
 */

/** A one-channel preset's sibling for that channel: it keeps the attributed totals (ADR-127). */
export const ONE_CHANNEL_FILTER = {
  options: [
    { filter_id: 'all', label: 'All channels' },
    { filter_id: 'sponsored_products', label: 'Sponsored Products' },
  ],
  selected: 'sponsored_products',
} as const;

const PORTFOLIO_SLOTS: readonly (readonly [string, string, string])[] = [
  ['spend', 'Ad spend', 'Headline and campaign rows'],
  ['purchases', 'Attributed purchases', 'Headline and campaign rows'],
  ['impressions', 'Impressions', 'Headline'],
  ['portfolio_roas_attributed_14d', 'Portfolio ROAS (attributed, 14 day)', 'Headline'],
  ['attributed_roas', 'ROAS (attributed, 14 day)', 'Campaign rows'],
  ['ctr', 'Click-through rate', 'Campaign rows'],
];

/** The coverage block of a complete one-channel portfolio whose every figure has arrived. */
export function completePortfolioCoverage(window: { readonly end: string; readonly start: string }): Record<string, unknown> {
  return {
    entries: PORTFOLIO_SLOTS.map(([slot, label, role_label]) => ({
      absence_text: null, coverage_text: null, label, missing_dates: [], missing_text: 'None',
      reason: 'Every day of this window has arrived.', recovery: 'No action needed.',
      role: 'current', role_label, slot, state: 'complete',
    })),
    summary: 'Every figure is complete for this window.',
    version: 2,
    window,
  };
}

/**
 * Return a hand-built portfolio body in its served form: the one-channel filter, no channel
 * view, the coverage block and the return lists (complete only; `null` on an empty or
 * insufficient body). A complete body's lists are empty, which renders none; a body that
 * already states its coverage or lists keeps them.
 */
export function asServedPortfolio(payload: Record<string, unknown>): Record<string, unknown> {
  const complete = payload.outcome === 'complete';
  return {
    channel_filter: ONE_CHANNEL_FILTER,
    channel_view: null,
    evidence_status: complete ? completePortfolioCoverage(payload.period as { end: string; start: string }) : null,
    return_lists: complete ? { highest: [], lowest: [] } : null,
    ...payload,
  };
}
