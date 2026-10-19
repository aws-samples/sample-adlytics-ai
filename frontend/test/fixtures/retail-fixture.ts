// SPDX-License-Identifier: MIT-0
/**
 * Retail health on the wire carries its coverage key (ADR-119): a complete body has one entry per
 * served tile in totals order, and an empty or insufficient body carries `null`. The decoder
 * refuses the retired format-5 form without the key (fictional labels).
 */

/** The coverage block of a complete body whose every tile has arrived, one entry per metric. */
export function completeRetailCoverage(metrics: readonly string[], window: { readonly end: string; readonly start: string }): Record<string, unknown> {
  return {
    entries: metrics.map((slot) => ({
      absence_text: null, coverage_text: null, label: `Fictional ${slot}`, missing_dates: [], missing_text: 'None',
      reason: 'Every day of this window has arrived.', recovery: 'No action needed.',
      role: 'current', role_label: 'Headline and product rows', slot, state: 'complete',
    })),
    summary: 'Every figure is complete for this window.',
    version: 2,
    window,
  };
}
