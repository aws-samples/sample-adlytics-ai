// SPDX-License-Identifier: MIT-0
/**
 * The tile change chips (ADR-121): every tile on Campaigns and Retail health shows the server's
 * change chip, and its tone is the one the metric dictionary's `favourable_direction` gives the
 * chip's direction. The direction is read from the chip's icon, never from a sign, so a
 * favourable rise in customer returns fails here. probe_frontend_browser.mjs runs it on the
 * deployed page; the local conformance harness runs the same function against fictional
 * fixtures. It throws on the first failed surface and returns a sanitized summary.
 */
/** The three direction icons' paths in frontend/components/Icon.tsx. */
const ICON_DIRECTIONS = {
  'M12 19V5M6 11l6-6 6 6': 'up',
  'M12 5v14M6 13l6 6 6-6': 'down',
  'M5 12h14': 'flat',
};
/**
 * Tile label to [metric id, `favourable_direction`] in contracts/shared/metric-dictionary.yaml;
 * tests/unit/test_change_chip_probe_map.py holds each pair to the dictionary. A tile the map does
 * not name fails, so a new compared tile is declared here before it can pass.
 */
export const TILE_FAVOURABLE = {
  Campaigns: {
    'Ad spend': ['ad_spend', 'none'],
    'Attributed purchases': ['purchases', 'up'],
    'Impressions': ['impressions', 'none'],
    'Portfolio ROAS': ['portfolio_roas_attributed_14d', 'up'],
  },
  'Retail health': {
    'Amazon margin proxy': ['amazon_margin_proxy', 'none'],
    'COGS shipped': ['cogs_shipped', 'none'],
    'Customer returns': ['customer_returns', 'down'],
    'Est. revenue withheld by returns': ['returns_value_estimate_retail', 'down'],
    'Fulfilment rate': ['fulfilment_rate', 'up'],
    'Glance views': ['glance_views', 'up'],
    'Ordered revenue': ['revenue_ordered', 'up'],
    'Return rate': ['return_rate', 'down'],
    'Shipped revenue': ['retail_revenue_shipped', 'up'],
    'Shipped units': ['units_shipped', 'up'],
    'Units per 1K glance views': ['conversion_rate_retail', 'up'],
  },
};
/** The tone the governed direction gives a move: a flat move or an undirected metric is neutral. */
export function expectedTone(direction, favourable) {
  if (direction === 'flat' || favourable === 'none') return 'neutral';
  return direction === favourable ? 'favourable' : 'unfavourable';
}
/** Read every measured tile's chip under `scope`: label, icon direction, tone classes, text. */
async function tileChips(page, scope) {
  return page.locator(`${scope} .tiles .tile:not(.empty)`).evaluateAll((tiles, icons) => tiles.map((tile) => {
    const chip = tile.querySelector('.chg');
    const path = chip?.querySelector('svg.icon path')?.getAttribute('d') ?? null;
    return {
      absent: tile.querySelector('.chg-absent') !== null,
      direction: path === null ? null : icons[path] ?? 'unknown-icon',
      label: tile.querySelector('.k')?.textContent?.trim() ?? '',
      text: chip?.textContent?.trim() ?? null,
      tones: chip === null ? [] : ['favourable', 'unfavourable', 'neutral'].filter((tone) => chip.classList.contains(tone)),
    };
  }), ICON_DIRECTIONS);
}
/**
 * Check one surface's tiles ('Campaigns' or 'Retail health') under `scope` and return
 * `label:direction:tone` for each, joined by `|`. Labels and tones are page chrome, not data.
 */
export async function changeChipChecks(page, surface, scope) {
  const declared = TILE_FAVOURABLE[surface];
  if (declared === undefined) throw new Error(`No declared tile directions for ${surface}.`);
  const chips = await tileChips(page, scope);
  const undeclared = chips.filter((chip) => !Object.hasOwn(declared, chip.label)).map((chip) => chip.label);
  const absent = chips.filter((chip) => chip.absent || chip.text === null).map((chip) => chip.label);
  const wrong = chips.filter((chip) => !chip.absent && chip.text !== null && Object.hasOwn(declared, chip.label) && (
    chip.tones.length !== 1 ||
    !['up', 'down', 'flat'].includes(chip.direction) ||
    (chip.text.endsWith('No change') !== (chip.direction === 'flat')) ||
    chip.tones[0] !== expectedTone(chip.direction, declared[chip.label][1])
  )).map((chip) => `${chip.label}:${chip.direction}:${chip.tones.join('+') || 'none'}`);
  if (chips.length === 0 || undeclared.length > 0 || absent.length > 0 || wrong.length > 0) {
    throw new Error(
      `${surface} change chips are not server chips with the governed tone: tiles=${chips.length}, ` +
      `undeclared=${JSON.stringify(undeclared)}, absent=${JSON.stringify(absent)}, wrong=${JSON.stringify(wrong)}.`,
    );
  }
  return chips.map((chip) => `${chip.label}:${chip.direction}:${chip.tones[0]}`).join('|');
}
