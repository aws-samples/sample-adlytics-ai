// SPDX-License-Identifier: MIT-0
'use client';

/**
 * The four tile groups Retail health and its product detail share, under the same headings in
 * the same order (ADR-125 decision 4; ia §7.3 item 8, §7.3a item 3). Every figure is the
 * server's; the browser only shifts a decimal point to show a rate as a percentage or per 1,000.
 */

import { formatCanonicalDecimal, formatCount, formatCurrency } from '@/lib/exact-format';
import type { Metric, RetailComparison, RetailMetricValue } from '@/lib/retail-health';
import { TileAbsence, TileChange, useWithheld } from './Coverage';

export type RetailTileScope = 'page' | 'product';

/** The shared headings, with each group's tiles in display order. */
export const RETAIL_TILE_GROUPS: readonly {
  readonly id: string;
  readonly metrics: readonly Metric[];
  readonly returnReasons?: true;
  readonly title: string;
}[] = [
  { id: 'demand', metrics: ['glance_views', 'conversion_rate_retail', 'revenue_ordered', 'units_ordered'], title: 'Demand and conversion' },
  { id: 'sales', metrics: ['retail_revenue_shipped', 'units_shipped', 'fulfilment_rate', 'asp_shipped'], title: 'Sales and fulfilment' },
  { id: 'margin', metrics: ['revenue_per_glance_view', 'cogs_shipped', 'amazon_margin_proxy'], title: 'Traffic value and margin' },
  { id: 'returns', metrics: ['customer_returns', 'return_rate', 'returns_value_estimate_retail'], returnReasons: true, title: 'Returns' },
];

const LABELS: Record<Metric, string> = {
  amazon_margin_proxy: 'Amazon margin proxy',
  asp_shipped: 'Average selling price',
  cogs_shipped: 'Cost of goods shipped',
  conversion_rate_retail: 'Units per 1,000 glance views',
  customer_returns: 'Customer returns',
  fulfilment_rate: 'Fulfilment rate',
  glance_views: 'Glance views',
  retail_revenue_shipped: 'Shipped revenue',
  return_rate: 'Return rate',
  returns_value_estimate_retail: 'Estimated revenue withheld by returns',
  revenue_ordered: 'Ordered revenue',
  revenue_per_glance_view: 'Revenue per glance view',
  units_ordered: 'Ordered units',
  units_shipped: 'Shipped units',
};

export function retailMetricLabel(metric: Metric): string {
  return LABELS[metric];
}

/** Each tile's basis line: how the figure is defined, in the dictionary's terms. */
function basisLine(metric: Metric, scope: RetailTileScope): string | null {
  switch (metric) {
    case 'glance_views': return 'Vendor Central · glance views';
    case 'conversion_rate_retail': return 'Ordered units ÷ glance views × 1,000';
    case 'retail_revenue_shipped': return 'Vendor Central · shipped revenue';
    case 'fulfilment_rate': return 'Shipped units ÷ ordered units';
    case 'asp_shipped': return 'Shipped revenue ÷ shipped units';
    case 'revenue_per_glance_view': return 'Shipped revenue ÷ glance views';
    case 'cogs_shipped': return "Amazon's wholesale cost for shipped units: what Amazon paid you";
    case 'amazon_margin_proxy': return "Shipped revenue − cost of goods shipped · Amazon's margin, not yours";
    case 'return_rate': return 'Customer returns ÷ shipped units';
    case 'returns_value_estimate_retail':
      return scope === 'product'
        ? "Returned units × this product's average selling price"
        : "Returned units × the window's average selling price";
    default: return null;
  }
}

/** Shift an exact decimal's point to the right: a lossless rendering, never arithmetic. */
export function shiftDecimal(source: string, places: number): string {
  const negative = source.startsWith('-');
  const digits = negative ? source.slice(1) : source;
  const [whole = '0', fraction = ''] = digits.split('.');
  const shiftedWhole = `${whole}${fraction.slice(0, places).padEnd(places, '0')}`.replace(/^0+(?=\d)/, '');
  const shiftedFraction = fraction.slice(places);
  return `${negative ? '-' : ''}${shiftedWhole}${shiftedFraction === '' ? '' : `.${shiftedFraction}`}`;
}

const PERCENT_RATES: readonly Metric[] = ['return_rate', 'fulfilment_rate'];

/** Render one served value: money from its server display value, rates by a point shift. */
export function retailValue(metric: Metric, value: Pick<RetailMetricValue, 'units' | 'value'> & { readonly display_value?: string | null }): string {
  const source = typeof value.value === 'number' ? String(value.value) : value.value;
  if (PERCENT_RATES.includes(metric)) return `${shiftDecimal(source, 2)}%`;
  if (metric === 'conversion_rate_retail') return formatCanonicalDecimal(shiftDecimal(source, 3));
  if (value.units.kind === 'currency' && value.units.currency_code !== null && value.display_value != null) {
    return formatCurrency(value.display_value, value.units.currency_code);
  }
  if (value.units.kind === 'count' && !source.includes('.')) return formatCount(source);
  return formatCanonicalDecimal(source);
}

function RetailTile({ comparison, onOpen, scope }: {
  readonly comparison: RetailComparison;
  readonly onOpen: (metric: Metric, origin: HTMLButtonElement) => void;
  readonly scope: RetailTileScope;
}) {
  const label = retailMetricLabel(comparison.metric);
  // One basis line: the definition, then the covered days while the figure is partly reported
  // ("Customer returns ÷ shipped units · Sep 5–10, 6 of 7 days", as the mockups write it).
  const coverage = useWithheld(comparison.metric)?.coverage_text ?? null;
  const parts = [basisLine(comparison.metric, scope), coverage].filter((part): part is string => part !== null);
  const basis = parts.length === 0 ? null : parts.join(' · ');
  return <article className="tile">
    <p className="k">{label}</p>
    <div className="row">
      <div>
        <p className="v">{retailValue(comparison.metric, comparison.current)}</p>
        <p className="prior">Prior: {retailValue(comparison.metric, comparison.prior)}</p>
      </div>
      <TileChange change={comparison.change} slot={comparison.metric} />
    </div>
    {basis === null ? null : <p className="basis">{basis}</p>}
    <TileAbsence figure={label} slot={comparison.metric} />
    <button
      type="button"
      className="ask"
      aria-haspopup="dialog"
      onClick={(event) => onOpen(comparison.metric, event.currentTarget)}
    >
      Open the focus view for {label.toLowerCase()}
    </button>
  </article>;
}

/** A typed absence: the server's reason, never a zero and never an opener. */
function AbsentTile({ label, reason, value }: { readonly label: string; readonly reason: string; readonly value: string }) {
  return <article className="tile empty">
    <p className="k">{label}</p>
    <p className="v">{value}</p>
    <p className="prior">{reason}</p>
  </article>;
}

export function RetailTileGroups({ absences, idPrefix, onOpen, returnReasons, scope, totals }: {
  readonly absences: readonly { readonly metric: string; readonly reason: string }[];
  /** Keeps heading ids unique when the page renders more than one set of groups. */
  readonly idPrefix: string;
  readonly onOpen: (metric: Metric, origin: HTMLButtonElement) => void;
  /** The server's return-reasons absence, or null on a body baked before it existed. */
  readonly returnReasons: string | null;
  readonly scope: RetailTileScope;
  readonly totals: readonly RetailComparison[];
}) {
  return <>{RETAIL_TILE_GROUPS.map((group) => {
    const tiles = group.metrics
      .map((metric) => totals.find((comparison) => comparison.metric === metric))
      .filter((comparison) => comparison !== undefined);
    const absent = absences.filter((absence) => (group.metrics as readonly string[]).includes(absence.metric));
    const reasons = group.returnReasons === true ? returnReasons : null;
    if (tiles.length === 0 && absent.length === 0 && reasons === null) return null;
    const headingId = `${idPrefix}-${group.id}`;
    return <section className="metric-section" aria-labelledby={headingId} key={group.id}>
      <div className="sec-head"><h2 id={headingId}>{group.title}</h2></div>
      <div className="tiles">
        {tiles.map((comparison) => <RetailTile comparison={comparison} key={comparison.metric} onOpen={onOpen} scope={scope} />)}
        {absent.map((absence) => <AbsentTile
          key={absence.metric}
          label={retailMetricLabel(absence.metric as Metric)}
          reason={absence.reason}
          value="Not measured under this view"
        />)}
        {reasons === null ? null : <AbsentTile label="Return reasons" reason={reasons} value="Not available" />}
      </div>
    </section>;
  })}</>;
}
