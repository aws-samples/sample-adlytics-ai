// SPDX-License-Identifier: MIT-0
/**
 * The browser probe's roster-page routes (block 9, D2 seam sweep finding 11), derived from the
 * generated route allowlist. `tests/unit/test_probe_frontend_browser_routes.py` pins the
 * allowlist's page routes to the bake roster's pages and runs this derivation.
 */

/**
 * Allowlisted routes that are not roster pages: the composed tabs (ADR-151: the root and Products and
 * ads, UI refresh 3.7) and the prompt admin.
 */
export const NOT_ROSTER_PAGES = Object.freeze(['/', '/admin/prompts/', '/products-and-ads/']);

/** Roster pages the probe's earlier blocks walk in depth. */
export const WALKED_IN_DEPTH = Object.freeze(['/campaigns/', '/retail-health/']);

/**
 * Return every allowlisted roster-page route the earlier blocks don't walk in depth.
 * Throws when a hand-kept route above is no longer allowlisted (the lists are stale), or when
 * nothing is left to walk, so the block can never pass by walking no page.
 */
export function rosterRoutes(routes) {
  for (const route of [...NOT_ROSTER_PAGES, ...WALKED_IN_DEPTH]) {
    if (!routes.includes(route)) {
      throw new Error(`The route allowlist no longer carries ${route}; the probe's hand-kept route lists are stale.`);
    }
  }
  const excluded = new Set([...NOT_ROSTER_PAGES, ...WALKED_IN_DEPTH]);
  const remaining = routes.filter((route) => !excluded.has(route));
  if (remaining.length === 0) throw new Error('The route allowlist leaves no page for the roster-page walk.');
  return remaining;
}
