// SPDX-License-Identifier: MIT-0
/**
 * Overview › Growth check (ADR-151, `docs/ui/overview/README.md`): a composed tab over the served
 * 7-day Scorecard, Retail health, New customers and Campaigns presentations. Each band reads its
 * own source. Nothing here computes a figure: the answer fills a fixed template with served
 * display strings and the served change direction, and every tile, chart and row is the same
 * served value its drill-down tab shows.
 */
import { type Answer, changeVerb, windowText } from './answer';
import type { PresentationErrorClass } from './presentation';
import type { RetailHealthArtifact } from './retail-health';
import type { NetworkScorecardArtifact, ScorecardCard } from './scorecard';

/** One source's outcome: its served presentation, or the class of the request that failed. */
export type OverviewSource<Presentation> =
  | { readonly kind: 'served'; readonly presentation: Presentation }
  | { readonly issue: PresentationErrorClass; readonly kind: 'rejected' };

/** ABS-OV-1 (`docs/ui/page-absence-copy.md`): a source that didn't load, named by its tab. */
export function sourceIssueText(tab: string): string {
  return `This part comes from ${tab}, which can't be shown right now. Try again later.`;
}

/** The one preset all three window-bound sources declare (plan 2.2). */
export const OVERVIEW_PRESET = '7d' as const;

/** The card the Overview reads: the page's only network card. With two it names none (no pick). */
export function overviewCard(artifact: NetworkScorecardArtifact): ScorecardCard | null {
  return artifact.cards.length === 1 ? artifact.cards[0] ?? null : null;
}

function sentence(text: string): string {
  return text.endsWith('.') ? text : `${text}.`;
}

function sameWindow(left: { readonly end: string; readonly start: string }, right: { readonly end: string; readonly start: string }): boolean {
  return left.start === right.start && left.end === right.end;
}
type Window = { readonly end: string; readonly start: string };
/**
 * A band's own served window, named when the page's window label (the Scorecard's) doesn't cover
 * it: a different window, or no label at all. Null when the band measures the labelled days.
 */
export function bandWindowText(period: Window, labelled: Window | null): string | null {
  return labelled !== null && sameWindow(period, labelled) ? null : windowText(period);
}

/**
 * The Growth check answer (plan template): the once-counted return and its prior, then shipped
 * retail sales and ad spend by their served change. Each clause drops on its own; a retail
 * window that isn't the ad window is named, never merged into it, and the two changes are then
 * two sentences, each naming its own window, never joined by "while".
 */
export function overviewAnswer(scorecard: NetworkScorecardArtifact | null, retail: RetailHealthArtifact | null): Answer | null {
  const complete = scorecard !== null && scorecard.outcome === 'complete' ? scorecard : null;
  const card = complete === null ? null : overviewCard(complete);
  const parts: string[] = [];
  let kind: Answer['kind'] = 'fallback';
  if (complete !== null && card !== null) {
    const model = card.headline.models.find((entry) => entry.model === card.headline.default_model) ?? null;
    const figure = model?.figure ?? null;
    const currency = card.currency_code;
    const window = windowText(complete.period);
    if (figure !== null && figure.display_value !== null) {
      kind = 'answer';
      const lead = `Amazon Ads returned ${figure.display_value} ${currency} for every 1 ${currency} spent in ${window}`;
      parts.push(figure.prior_display_value === null || complete.prior_period === null
        ? `${lead}.`
        : `${lead}, compared with ${figure.prior_display_value} ${currency} in ${windowText(complete.prior_period)}.`);
    } else {
      const reason = card.headline.absence_text ?? figure?.absence_text ?? complete.not_available_text;
      parts.push(`Return on ad spend isn't available for ${window}: ${sentence(reason)}`);
    }
  }
  const revenue = retail !== null && retail.outcome === 'complete'
    ? retail.totals.find((entry) => entry.metric === 'retail_revenue_shipped') ?? null
    : null;
  const retailMoved = revenue === null || revenue.withheld ? null : changeVerb(revenue.change);
  const spendMoved = card === null ? null : changeVerb(card.spend.change);
  // The retail clause shares the ad window only when the served windows are the same days.
  const shared = retail !== null && complete !== null && sameWindow(retail.period, complete.period);
  const retailClause = retailMoved === null || retail === null
    ? null
    : `Shipped retail sales ${retailMoved}${shared || complete === null ? '' : ` in ${windowText(retail.period)}`}`;
  // "while" pairs the two changes only over the same days; otherwise each sentence names its window.
  if (retailClause !== null && spendMoved !== null && complete !== null) {
    parts.push(shared
      ? `${retailClause} while ad spend ${spendMoved}.`
      : `${retailClause}. Ad spend ${spendMoved} in ${windowText(complete.period)}.`);
  } else if (retailClause !== null) parts.push(`${retailClause}.`);
  else if (spendMoved !== null) parts.push(`Ad spend ${spendMoved}.`);
  return parts.length === 0 ? null : { kind, text: parts.join(' ') };
}

/** One "Data through" line per served end date, naming the sources that share it. */
export function throughLines(sources: readonly { readonly end: string; readonly name: string }[]): readonly { readonly end: string; readonly names: readonly string[] }[] {
  const lines: { end: string; names: string[] }[] = [];
  for (const source of sources) {
    const line = lines.find((entry) => entry.end === source.end);
    if (line === undefined) lines.push({ end: source.end, names: [source.name] });
    else line.names.push(source.name);
  }
  return lines;
}
