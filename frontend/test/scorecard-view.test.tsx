// SPDX-License-Identifier: MIT-0
/**
 * The Scorecard view (UI slice 9a item A10) on the server's own fixtures: section order, the
 * model selector, the channel table and its links, highlight, pointer lines, dialogs, loading
 * and absence states, and axe. Each passing case sits beside its failing control.
 */
import { CUSTOMER_METRIC_LABELS } from '../lib/glossary';
import assert from 'node:assert/strict';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { test } from 'vitest';

import { InsightFocusDialog, type InsightDialogSlot, type InsightFocusSurface } from '@/components/InsightFocusDialog';
import { VisibleTabsContext } from '@/components/NextStepContext';
import { PageAbsence } from '@/components/PageAbsence';
import { scorecardRouteAdapter } from '@/components/ScorecardRouteAdapter';
import { ScorecardControlsSkeleton, ScorecardSkeleton, ScorecardView } from '@/components/ScorecardView';
import { formatCurrency, formatRatio } from '@/lib/exact-format';
import { bindingByRoute } from '@/lib/page-bindings';
import { parsePresentationResultCalendarV2, scorecardPresentationV2 } from '@/lib/presentation-v2';
import { PROVENANCE_READINGS } from '@/lib/provenance';
import { visibleHubs, type ManifestTab } from '@/lib/route-manifest';
import type { NetworkScorecardArtifact, ScorecardFigure } from '@/lib/scorecard';
import absentCell from './fixtures/scorecard-absent-cell-v2.json';
import alsoChanged from './fixtures/scorecard-also-changed-v2.json';
import weekly from './fixtures/scorecard-7d-v2.json';
import emptyBody from './fixtures/scorecard-empty-v2.json';
import monthly from './fixtures/scorecard-month-v2.json';
import noPeriod from './fixtures/scorecard-no-period-v2.json';
import pending from './fixtures/scorecard-pending-v2.json';
import twoMarketplaces from './fixtures/scorecard-two-marketplaces-v2.json';
import defaultBody from './fixtures/scorecard-default-v2.json';
import modelSensitive from './fixtures/scorecard-model-sensitive-v2.json';
import noDsp from './fixtures/scorecard-no-dsp-v2.json';
import needsVendor from './fixtures/scorecard-tacos-needs-vendor-v2.json';
import twoAlsoChanged from './fixtures/scorecard-two-marketplaces-also-changed-v2.json';
import noCredit from './fixtures/scorecard-no-deduplicated-credit-v2.json';

function artifactOf(fixture: unknown): NetworkScorecardArtifact {
  const presentation = scorecardPresentationV2(parsePresentationResultCalendarV2(fixture, 'scorecard'));
  if (presentation.kind !== 'parent') throw new Error('expected a parent');
  return presentation.artifact;
}

const NEW_CUSTOMERS: ManifestTab = {
  description: 'Shoppers buying from your brand for the first time in 12 months',
  entitlement: null,
  kind: 'page',
  label: 'New customers',
  order: 1,
  route: '/new-customers/',
  tab_id: 'new-customers',
};

test('the sections render in the tab order with the server scope band and texts', () => {
  const artifact = artifactOf(weekly);
  const view = render(<ScorecardView artifact={artifact} />);
  const headings = within(view.container).getAllByRole('heading', { level: 2 }).map((node) => node.textContent);
  assert.deepEqual(headings, [
    'Amazon · US marketplace · USD',
    'Insights',
    'By channel',
    'Ad spend and retail sales',
  ]);
  assert.equal(view.container.querySelector('.scope-band')?.textContent, artifact.scope_text);
  assert.equal(view.container.querySelector('.scope-band')?.getAttribute('aria-live'), 'polite');
  assert.ok(screen.getByText('Sponsored Products leads under all three models.'));
  assert.ok(screen.getByText(/Attributed sales aren't added across channels/));
  // No total or subtotal row: the channel table lists exactly the served channels.
  const table = screen.getByRole('region', { name: 'Spend and attributed return per channel' });
  assert.deepEqual(
    within(table).getAllByRole('rowheader').map((cell) => cell.textContent),
    artifact.cards[0]?.channel_table.rows.map((row) => row.channel_label),
  );
  // Empty "Also changed" is absent; the boundary reads in About these figures.
  assert.equal(screen.queryByRole('heading', { name: 'Also changed' }), null);
  assert.ok(screen.getByText('About these figures'));
});

test('switching the model changes only the headline, never the ranking or the channel rows', async () => {
  const user = userEvent.setup();
  const artifact = artifactOf(weekly);
  const view = render(<ScorecardView artifact={artifact} />);
  const tables = () => [...view.container.querySelectorAll('table')].map((table) => table.textContent);
  const before = tables();
  const headline = () => view.container.querySelector('.tile .basis')?.textContent;
  assert.match(headline() ?? '', /First touch/);
  await user.selectOptions(screen.getByLabelText('Attribution model for deduplicated ROAS'), 'linear_v1');
  assert.match(headline() ?? '', /Linear/);
  assert.deepEqual(tables(), before);
});

test('a channel row links to Campaigns on that channel with the served dates', () => {
  render(<ScorecardView artifact={artifactOf(weekly)} />);
  const link = screen.getByRole('link', { name: /Open Campaigns for Sponsored Products/ });
  assert.equal(link.getAttribute('href'), '/campaigns/?channel=amazon_sponsored_products');
  assert.equal(link.textContent, 'Open · Sep 9–15');
  assert.ok(screen.getByRole('link', { name: /Open Retail health · Sep 5–11/ }));
});

test('the highlighted channel row is outlined and tagged; an unknown channel highlights nothing', () => {
  const artifact = artifactOf(weekly);
  const view = render(<ScorecardView artifact={artifact} highlight="amazon_dsp" />);
  const tagged = view.container.querySelectorAll('.tag-channel');
  assert.equal(tagged.length, 1);
  assert.equal(tagged[0]?.closest('tr')?.getAttribute('data-channel'), 'amazon_dsp');
  view.unmount();
  const none = render(<ScorecardView artifact={artifact} highlight="amazon_radio" />);
  assert.equal(none.container.querySelectorAll('.tag-channel').length, 0);
  assert.equal(none.container.querySelectorAll('.highlight-row').length, 0);
});

test('a pointer line renders only when its tab is in the session manifest', () => {
  const artifact = artifactOf(weekly);
  const without = render(<ScorecardView artifact={artifact} />);
  assert.equal(without.container.querySelector('.pointer'), null);
  without.unmount();
  const withTab = render(<VisibleTabsContext.Provider value={[NEW_CUSTOMERS]}>
    <ScorecardView artifact={artifact} />
  </VisibleTabsContext.Provider>);
  const link = within(withTab.container.querySelector('.pointer') as HTMLElement).getByRole('link');
  assert.equal(link.getAttribute('href'), '/new-customers/');
  assert.equal(link.textContent, 'Customers › New customers');
  // The simulator line stays hidden without its tab.
  assert.equal(withTab.container.querySelectorAll('.pointer').length, 1);
  withTab.unmount();
  // N30: the committed route manifest now lists New customers, so its line renders from it.
  const committed = visibleHubs({}).flatMap((hub) => hub.tabs);
  const shipped = render(<VisibleTabsContext.Provider value={committed}><ScorecardView artifact={artifact} /></VisibleTabsContext.Provider>);
  const shippedLinks = [...shipped.container.querySelectorAll('.pointer a')];
  assert.deepEqual(shippedLinks.map((node) => node.getAttribute('href')), ['/new-customers/']);
});

test('an insight card opens its dialog with its own figures and returns focus on close', async () => {
  const user = userEvent.setup();
  render(<ScorecardView artifact={artifactOf(weekly)} />);
  const opener = screen.getByRole('button', { name: /Read more: Sponsored Products ROAS rose/ });
  await user.click(opener);
  const dialog = screen.getByRole('dialog');
  // The shared dialog, exactly per the insight contract §3 (slice 9a decision F11a): the
  // narration, no browser-composed stats, and the contract's deterministic provenance line.
  assert.match(dialog.textContent ?? '', /ROAS/);
  assert.equal(dialog.querySelector('.prov-strip')?.textContent, 'Computed directly from the values on this page · no model involved');
  assert.equal(dialog.querySelector('.stat-list'), null);
  const axe = await import('axe-core');
  const results = await axe.default.run(dialog, { rules: { 'color-contrast': { enabled: false }, region: { enabled: false } } });
  assert.deepEqual(results.violations, []);
  await user.keyboard('{Escape}');
  assert.equal(screen.queryByRole('dialog'), null);
  await new Promise((resolve) => { requestAnimationFrame(resolve); });
  assert.equal(document.activeElement, opener);
});

test('a calendar month shows no change chips and names its period', () => {
  const artifact = artifactOf(monthly);
  const view = render(<ScorecardView artifact={artifact} />);
  assert.equal(view.container.querySelectorAll('.chg').length, 0);
  assert.match(view.container.querySelector('.scope-band')?.textContent ?? '', /No comparison for calendar periods/);
  // Paired: the 7-day body shows its served change chips.
  view.unmount();
  const rolling = render(<ScorecardView artifact={artifactOf(weekly)} />);
  assert.ok(rolling.container.querySelectorAll('.chg').length > 0);
});

test('the page has no axe violations and the loading skeleton announces itself', async () => {
  const view = render(<ScorecardView artifact={artifactOf(weekly)} />);
  const axe = await import('axe-core');
  const results = await axe.default.run(view.container, { rules: { 'color-contrast': { enabled: false }, region: { enabled: false } } });
  assert.deepEqual(results.violations, []);
  view.unmount();
  render(<ScorecardSkeleton />);
  assert.equal(screen.getByRole('status').getAttribute('aria-label'), 'Loading Scorecard');
});

test('the served empty body shows its server outcome text and no figures', () => {
  // The server's own response, baked with no delivering channel (the page declares no other
  // page-level absence, SC9A-GATE-R5-001).
  const artifact = artifactOf(emptyBody);
  assert.equal(artifact.outcome, 'empty');
  const view = render(<ScorecardView artifact={artifact} />);
  const status = within(view.container).getByRole('status');
  assert.equal(status.querySelector('p')?.textContent, artifact.outcome_text);
  assert.equal(view.container.querySelector('table'), null);
  assert.equal(view.container.querySelector('.scope-band'), null);
  view.unmount();
  // Paired: the complete body shows its scope band and tables, and no outcome callout.
  const complete = render(<ScorecardView artifact={artifactOf(weekly)} />);
  assert.ok(complete.container.querySelector('.scope-band'));
  assert.ok(complete.container.querySelector('table'));
  assert.equal(complete.container.querySelector('.callout'), null);
});

test('an absent channel figure shows "Not available" and the server reason; its siblings stay', async () => {
  const artifact = artifactOf(absentCell);
  const view = render(<ScorecardView artifact={artifact} />);
  const row = view.container.querySelector('tr[data-channel="amazon_dsp"]') as HTMLElement;
  const absent = [...row.querySelectorAll('.cell-na')];
  assert.equal(absent.length, 2);
  for (const cell of absent) {
    // Visible text, so a sighted reader and a screen reader get the same words (gate F1).
    assert.equal(cell.firstChild?.textContent, 'Not available');
    assert.equal(cell.querySelector('.reason')?.textContent, "No ad spend on this channel in this window, so there's no spend to divide its return by.");
    assert.equal(cell.getAttribute('aria-label'), null);
  }
  // The row's spend and attributed revenue, and the other channel's row, still render.
  assert.match(row.textContent ?? '', /1,200\.00 USD/);
  assert.equal(view.container.querySelectorAll('tr[data-channel="amazon_sponsored_products"] .cell-na').length, 0);
  assert.equal(row.textContent?.includes('—'), false);
  // The ranking states no ratio for the unspent channel, so it ranks nobody (G9A-1): its
  // served reason, no table, and no 0x or ordinal anywhere in the block.
  const rank = view.container.querySelector('.rank-block') as HTMLElement;
  assert.equal(rank.querySelector('.rank-outcome')?.textContent, artifact.cards[0]?.ranking.outcome_text);
  assert.match(artifact.cards[0]?.ranking.outcome_text ?? '', /^Not available: Amazon DSP had no ad spend in this window/);
  assert.equal(rank.querySelector('table'), null);
  assert.equal(/\d×|\b\d+(st|nd|rd|th)\b/.test(rank.textContent ?? ''), false);
  const axe = await import('axe-core');
  const results = await axe.default.run(view.container, { rules: { 'color-contrast': { enabled: false }, region: { enabled: false } } });
  assert.deepEqual(results.violations, []);
  view.unmount();
  // Paired: the complete 7-day body has no absent cell, and its ranking ranks DSP second.
  const complete = render(<ScorecardView artifact={artifactOf(weekly)} />);
  assert.equal(complete.container.querySelectorAll('.cell-na').length, 0);
  assert.match(complete.container.querySelector('.rank-block')?.textContent ?? '', /3\.75× 2nd/);
});

/** Multipliers shown at more than two places: a browser that showed the exact value, not the served one. */
function longRatios(container: HTMLElement): string[] {
  return (container.textContent ?? '').match(/\d+\.\d{3,}×/g) ?? [];
}

test('every multiplier shows its served two-place display value, never the exact value (S26)', () => {
  const artifact = artifactOf(weekly);
  const view = render(<ScorecardView artifact={artifact} />);
  const row = view.container.querySelector('tr[data-channel="amazon_sponsored_products"]') as HTMLElement;
  assert.match(row.textContent ?? '', /4\.50×/);
  assert.match(view.container.querySelector('.scorecard-card .tile .v')?.textContent ?? '', /^3\.96×$/);
  assert.deepEqual(longRatios(view.container), []);
  view.unmount();
  // Paired: the same page fed an exact value as its display value shows the long form, which
  // the check catches (the decoder refuses such a body; scorecard.test.ts).
  const card = artifact.cards[0];
  if (card === undefined) throw new Error('expected a card');
  const exact: NetworkScorecardArtifact = {
    ...artifact,
    cards: [{ ...card, headline: { ...card.headline, models: card.headline.models.map((model) => ({ ...model, figure: { ...model.figure, display_value: model.figure.value } })) } }],
  };
  const long = render(<ScorecardView artifact={exact} />);
  assert.deepEqual(longRatios(long.container), ['3.9600×']);
});

/** Whether every served marketplace shows as its own card and its own named retail rows. */
function marketplacesShown(container: HTMLElement, artifact: NetworkScorecardArtifact): boolean {
  const titles = [...container.querySelectorAll('.scorecard-card h2')].map((node) => node.textContent);
  const markets = [...container.querySelectorAll('.tile .market')].map((node) => node.textContent);
  const servedMarkets = [...artifact.tacos.map((row) => row.marketplace_text), ...artifact.cross_network_spend.map((row) => row.marketplace_text)];
  return artifact.cards.length >= 2
    && JSON.stringify(titles) === JSON.stringify(artifact.cards.map((card) => card.title))
    && new Set(titles).size === titles.length
    && JSON.stringify(markets) === JSON.stringify(servedMarkets)
    && new Set(artifact.tacos.map((row) => row.marketplace_text)).size === artifact.tacos.length
    && container.querySelectorAll('#channel-table .dt-wrap').length === artifact.cards.length;
}

test('two marketplaces render two cards, two tables and retail rows that name each (S4, S14)', async () => {
  const artifact = artifactOf(twoMarketplaces);
  const view = render(<ScorecardView artifact={artifact} />);
  assert.ok(marketplacesShown(view.container, artifact));
  assert.deepEqual(within(view.container).getAllByRole('heading', { level: 2 }).map((node) => node.textContent).slice(0, 2), [
    'Amazon · United Kingdom marketplace · GBP',
    'Amazon · US marketplace · USD',
  ]);
  assert.deepEqual([...view.container.querySelectorAll('.tile .market')].map((node) => node.textContent), [
    'United Kingdom marketplace', 'US marketplace', 'United Kingdom marketplace · GBP', 'US marketplace · USD',
  ]);
  // Never converted: the GBP card's money reads GBP.
  const tables = view.container.querySelectorAll('#channel-table .dt-wrap');
  assert.match(tables[0]?.textContent ?? '', /400\.00 GBP/);
  assert.equal(/USD/.test(tables[0]?.textContent ?? ''), false);
  // Each repeated table names its own card, so no two regions share a name (axe landmark-unique).
  const names = within(view.container).getAllByRole('region').map((node) => node.getAttribute('aria-labelledby')?.split(' ').map((id) => document.getElementById(id)?.textContent).join(' '));
  assert.equal(new Set(names).size, names.length);
  assert.ok(screen.getByRole('region', { name: 'Spend and attributed return per channel Amazon · United Kingdom · Sep 9–15, 2026' }));
  assert.ok(screen.getByRole('region', { name: /^Amazon · US marketplace · USD Deduplicated ROAS and its rank/ }));
  const axe = await import('axe-core');
  const results = await axe.default.run(view.container, { rules: { 'color-contrast': { enabled: false }, region: { enabled: false } } });
  assert.deepEqual(results.violations, []);
  view.unmount();
  // Paired: a dropped marketplace, and retail rows merged into one marketplace, both fail.
  const dropped: NetworkScorecardArtifact = { ...artifact, cards: artifact.cards.slice(1) };
  const droppedView = render(<ScorecardView artifact={dropped} />);
  assert.equal(marketplacesShown(droppedView.container, artifact), false);
  droppedView.unmount();
  const merged: NetworkScorecardArtifact = { ...artifact, tacos: artifact.tacos.map((row) => ({ ...row, marketplace_text: 'US marketplace' })) };
  const mergedView = render(<ScorecardView artifact={merged} />);
  assert.equal(marketplacesShown(mergedView.container, merged), false);
});

/** Whether every insight card shows its served marketplace and no two share a visible or accessible name. */
function insightNamesDistinct(container: HTMLElement, artifact: NetworkScorecardArtifact): boolean {
  const cards = [...container.querySelectorAll('.insights .insight')];
  const visible = cards.map((node) => `${node.querySelector('.kind')?.textContent ?? ''} | ${node.querySelector('h3')?.textContent ?? ''}`);
  const accessible = cards.map((node) => node.querySelector('button.view-details')?.getAttribute('aria-label') ?? '');
  const markets = cards.map((node) => node.querySelector('.kind .market')?.textContent ?? null);
  return cards.length === artifact.insight_cards.length && cards.length > 1
    && JSON.stringify(markets) === JSON.stringify(artifact.insight_cards.map((card) => card.marketplace_text))
    && new Set(visible).size === cards.length && new Set(accessible).size === cards.length;
}

/** How many channel rows carry the campaign-arrival tag, across every table on the page. */
function arrivalTags(container: HTMLElement): number {
  return container.querySelectorAll('#channel-table .tag-channel').length;
}

test('two marketplaces: each insight card names its marketplace, and arrival tags no wrong row (SC9A-GATE-001)', () => {
  const artifact = artifactOf(twoMarketplaces);
  const view = render(<ScorecardView artifact={artifact} highlight="amazon_dsp" />);
  assert.ok(insightNamesDistinct(view.container, artifact));
  assert.deepEqual([...view.container.querySelectorAll('.insights .kind')].map((node) => node.textContent), [
    'Channel mover · United Kingdom marketplace', 'Channel mover · US marketplace', 'Spend share · United Kingdom marketplace',
  ]);
  assert.ok(screen.getByRole('button', { name: 'Read more: Sponsored Products ROAS rose 0.50× · US marketplace' }));
  // Campaigns can't be scoped to a marketplace, so no card offers a step.
  assert.equal(view.container.querySelectorAll('.insights .next').length, 0);
  // DSP is in both marketplaces' cards and the step names no marketplace: no row is tagged.
  assert.equal(arrivalTags(view.container), 0);
  assert.equal(view.container.querySelectorAll('.highlight-row').length, 0);
  // Paired: the round-3 defect, both DSP rows tagged, is caught.
  for (const row of view.container.querySelectorAll('#channel-table tr[data-channel="amazon_dsp"] th')) row.insertAdjacentHTML('beforeend', '<span class="tag-channel">x</span>');
  assert.equal(arrivalTags(view.container) <= 1, false);
  view.unmount();
  // Paired: with the marketplace line removed (the round-3 body), two cards share a name.
  const unnamed: NetworkScorecardArtifact = { ...artifact, insight_cards: artifact.insight_cards.map((card) => ({ ...card, marketplace_text: null, title: card.headline })) };
  const unnamedView = render(<ScorecardView artifact={unnamed} />);
  assert.equal(insightNamesDistinct(unnamedView.container, artifact), false);
  assert.equal(insightNamesDistinct(unnamedView.container, unnamed), false);
  unnamedView.unmount();
  // With DSP in the US card alone, the arrival tags that one row and names its marketplace.
  const [uk, us] = artifact.cards;
  if (uk === undefined || us === undefined) throw new Error('expected two cards');
  const usOnly: NetworkScorecardArtifact = {
    ...artifact,
    cards: [{
      ...uk,
      channel_table: { ...uk.channel_table, rows: uk.channel_table.rows.filter((row) => row.channel !== 'amazon_dsp') },
      ranking: { ...uk.ranking, rows: uk.ranking.rows.filter((row) => row.channel !== 'amazon_dsp') },
    }, us],
  };
  const usView = render(<ScorecardView artifact={usOnly} highlight="amazon_dsp" />);
  const usTags = [...usView.container.querySelectorAll('#channel-table .tag-channel')];
  assert.deepEqual(usTags.map((node) => node.textContent), ["This campaign's channel · US marketplace"]);
  assert.equal(usTags[0]?.querySelector('.market')?.textContent, us.channel_table.marketplace_text);
  assert.equal(usTags[0]?.closest('.dt-wrap'), usView.container.querySelectorAll('#channel-table .dt-wrap')[1]);
  usView.unmount();
  // Paired: one marketplace still tags its one DSP row, naming no marketplace.
  const single = render(<ScorecardView artifact={artifactOf(weekly)} highlight="amazon_dsp" />);
  assert.deepEqual([...single.container.querySelectorAll('#channel-table .tag-channel')].map((node) => node.textContent), ["This campaign's channel"]);
});

test('the TACoS change goes through its served coverage entry, like every other figure', () => {
  const artifact = artifactOf(weekly);
  const tacos = artifact.tacos[0];
  assert.equal(tacos?.coverage_slot, 'tacos:amazon:amazon_us');
  const tile = (view: ReturnType<typeof render>) => [...view.container.querySelectorAll('.tile')].find((node) => node.querySelector('.k')?.textContent === 'Ad spend as a share of shipped sales') as HTMLElement;
  const complete = render(<ScorecardView artifact={artifact} />);
  assert.equal(tile(complete).querySelectorAll('.chg').length, 1);
  assert.equal(tile(complete).querySelector('.tile-absence'), null);
  complete.unmount();
  // The same body with the TACoS entry withheld (a missing day, as ADR-119 serves it): the
  // chip gives way to the served absence text and "See why", once.
  const status = artifact.evidence_status;
  if (status === null) throw new Error('expected coverage');
  const withheld: NetworkScorecardArtifact = {
    ...artifact,
    evidence_status: {
      ...status,
      entries: status.entries.map((entry) => entry.slot === tacos?.coverage_slot
        ? { ...entry, absence_text: 'Change withheld: Vendor Central sales for Sep 12 didn\u2019t arrive.', coverage_text: '6 of 7 days', missing_dates: ['2026-09-12'], state: 'missing_days' as const }
        : entry),
    },
  };
  const gated = render(<ScorecardView artifact={withheld} />);
  assert.equal(tile(gated).querySelectorAll('.chg').length, 0);
  assert.equal(tile(gated).querySelectorAll('.chg-absent').length, 1);
  assert.match(tile(gated).querySelector('.tile-absence')?.textContent ?? '', /Sep 12/);
  assert.ok(within(tile(gated)).getByRole('button', { name: /Ad spend as a share of shipped sales/ }));
});

/** Every string one served response carries, from that response alone (never another state's). */
function servedStrings(fixture: unknown): Set<string> {
  const served = new Set<string>();
  const collect = (value: unknown): void => {
    if (typeof value === 'string') served.add(value);
    else if (Array.isArray(value)) value.forEach(collect);
    else if (typeof value === 'object' && value !== null) Object.values(value).forEach(collect);
  };
  collect(fixture);
  return served;
}

/**
 * The percent text each served rate in these fixtures must show: the rate times 100 at one
 * place (ADR-121). Literals, never `formatRatePercent` itself, so a wrong scaling or rounding in
 * the formatter shows up as stray text instead of being accepted as its own expectation.
 */
const RATE_PERCENT_TEXT: Readonly<Record<string, string>> = {
  '0.0300': '3.0%',
  '0.0450': '4.5%',
  '0.0500': '5.0%',
  '0.0550': '5.5%',
  '0.0650': '6.5%',
  '0.0750': '7.5%',
};
function ratePercentText(value: string): string {
  const text = RATE_PERCENT_TEXT[value];
  if (text === undefined) throw new Error(`no literal percent text for the served rate ${value}`);
  return text;
}
/** The figure texts one decoded body may show, each from a served display value (or a served rate). */
function servedFigures(artifact: NetworkScorecardArtifact | null): Set<string> {
  const shown = new Set<string>();
  const add = (figure: ScorecardFigure) => {
    for (const [value, display] of [[figure.value, figure.display_value], [figure.prior_value, figure.prior_display_value]] as const) {
      if (value === null) continue;
      if (figure.format === 'percent') shown.add(ratePercentText(value));
      else if (display !== null && figure.format === 'currency' && figure.currency_code !== null) shown.add(formatCurrency(display, figure.currency_code));
      else if (display !== null) shown.add(formatRatio(display));
    }
  };
  for (const card of artifact?.cards ?? []) {
    add(card.spend);
    card.headline.models.forEach((model) => add(model.figure));
    card.channel_table.rows.forEach((row) => row.figures.forEach(add));
    card.ranking.rows.forEach((row) => row.values.forEach((value) => { if (value.display_value !== null) shown.add(formatRatio(value.display_value)); }));
  }
  artifact?.tacos.forEach((row) => add(row.figure));
  artifact?.cross_network_spend.forEach((row) => { if (row.figure !== null) add(row.figure); });
  return shown;
}

/**
 * Text nodes that are not, whole and exact, a string this response served, one of its served
 * figures, the governed page name, or on the LLD browser-owned allowlist (gate G9A-3).
 */
function strayTexts(container: HTMLElement, served: ReadonlySet<string>, allowlist: ReadonlySet<string>): string[] {
  const stray: string[] = [];
  const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node !== null; node = walker.nextNode()) {
    const value = node.textContent?.trim() ?? '';
    // Separators between served parts: a middle dot, a colon or a full stop, never a word.
    if (value === '' || /^[·:.]$/.test(value)) continue;
    // The shared change chip's spoken direction and tone (ADR-121 shared pattern, from the
    // served `direction` and `sentiment`), the same words on every page.
    if (/^(Up|Down)(, (favourable|unfavourable))?:$/.test(value)) continue;
    if (allowlist.has(value) || served.has(value)) continue;
    stray.push(value);
  }
  return stray;
}

/** Render a served response the way the route does: through the Scorecard route adapter. */
function renderRoute(fixture: unknown) {
  const presentation = scorecardPresentationV2(parsePresentationResultCalendarV2(fixture, 'scorecard'));
  const parent = { ...presentation, highlight: 'amazon_dsp' };
  const noop = () => undefined;
  // ADR-139: the controller renders a window_unavailable parent through PageAbsence.
  const node = parent.kind === 'window_unavailable'
    ? <PageAbsence {...scorecardRouteAdapter.absence(parent)} />
    : scorecardRouteAdapter.render({ kind: 'parent_ready', parent }, noop, noop, noop);
  const view = render(<VisibleTabsContext.Provider value={[NEW_CUSTOMERS]}>{node}</VisibleTabsContext.Provider>);
  return { artifact: presentation.kind === 'parent' ? presentation.artifact : null, view };
}

// Scorecard LLD "Browser-owned copy": the only words the browser writes on a served page.
// Plus the glossary's customer labels for served metric labels (UI refresh D5).
const ALLOWLIST = new Set(['Read more ›', ...CUSTOMER_METRIC_LABELS]);
const WALKED = { absentCell, alsoChanged, defaultBody, emptyBody, modelSensitive, monthly, needsVendor, noCredit, noDsp, noPeriod, pending, twoAlsoChanged, twoMarketplaces, weekly };

test('every visible text is exactly a string or figure its own response served, or allowlisted', () => {
  for (const [name, fixture] of Object.entries(WALKED)) {
    const { artifact, view } = renderRoute(fixture);
    const served = new Set([...servedStrings(fixture), ...servedFigures(artifact), bindingByRoute.scorecard.display_name]);
    assert.ok((view.container.textContent ?? '').length > 0, name);
    assert.deepEqual(strayTexts(view.container, served, ALLOWLIST), [], `browser-written text in ${name}`);
    view.unmount();
  }
  // Paired controls in the same walk. A browser-written sentence is caught.
  const own = new Set([...servedStrings(weekly), ...servedFigures(artifactOf(weekly)), bindingByRoute.scorecard.display_name]);
  const planted = render(<div><ScorecardView artifact={artifactOf(weekly)} /><p>This window is not ready.</p></div>);
  assert.deepEqual(strayTexts(planted.container, own, ALLOWLIST), ['This window is not ready.']);
  planted.unmount();
  // A word that is only part of a served string is caught: matching is whole-string.
  const partial = render(<div><ScorecardView artifact={artifactOf(weekly)} /><span>Not</span></div>);
  assert.deepEqual(strayTexts(partial.container, own, ALLOWLIST), ['Not']);
  partial.unmount();
  // Server text from another state is caught: the absent-cell reason, never served by the
  // 7-day body. A walk over every fixture's strings together would have accepted it.
  const borrowed = artifactOf(absentCell).cards[0]?.channel_table.rows[0]?.figures[2]?.absence_text ?? '';
  assert.ok(servedStrings(absentCell).has(borrowed) && !own.has(borrowed));
  const crossState = render(<div><ScorecardView artifact={artifactOf(weekly)} /><p>{borrowed}</p></div>);
  assert.deepEqual(strayTexts(crossState.container, own, ALLOWLIST), [borrowed]);
});

test('an Also changed row opens its dialog on the server-written narration', async () => {
  const user = userEvent.setup();
  const artifact = artifactOf(alsoChanged);
  const row = artifact.also_changed?.rows[0];
  if (row === undefined) throw new Error('expected an Also changed row');
  render(<ScorecardView artifact={artifact} />);
  const item = screen.getByRole('button', { name: row.label }).closest('li') as HTMLElement;
  assert.equal(item.textContent, `${row.label} · ${row.direction_text}${row.window_text} · ${row.scope_text}`);
  await user.click(screen.getByRole('button', { name: row.label }));
  const dialog = screen.getByRole('dialog');
  assert.ok([...dialog.querySelectorAll('p')].some((node) => node.textContent === row.dialog_text));
});

/** The dialog's evidence line, or null when it shows none. */
function evidenceLine(dialog: HTMLElement): string | null {
  return dialog.querySelector('.narr-card .ev')?.textContent ?? null;
}

/** Whether the dialog's assistant chip names the served title verbatim, case included. */
function chipKeepsTitle(dialog: HTMLElement, title: string): boolean {
  return dialog.querySelector('.ask-chip')?.textContent === `Ask the assistant about this window's ${title}`;
}

test('an Also changed dialog claims no card basis, and every dialog keeps the served name and stamps (SC9A-GATE-R5-002, -004, -005)', async () => {
  const user = userEvent.setup();
  const artifact = artifactOf(twoAlsoChanged);
  const row = artifact.also_changed?.rows.find((item) => item.marketplace_text === 'US marketplace');
  const card = artifact.insight_cards[0];
  if (row === undefined || card === undefined) throw new Error('expected an Also changed row and a card');
  const view = render(<ScorecardView artifact={artifact} />);
  await user.click(screen.getByRole('button', { name: row.title }));
  const rowDialog = screen.getByRole('dialog');
  // The row's own narration and no evidence line: the slot's line names the cards' shares and
  // ROAS moves, a basis this spend mover doesn't have.
  assert.ok([...rowDialog.querySelectorAll('p')].some((node) => node.textContent === row.dialog_text));
  assert.equal(evidenceLine(rowDialog), null);
  // The chip keeps the served title: "Amazon DSP ad spend · US marketplace", never lowercased.
  assert.ok(chipKeepsTitle(rowDialog, row.title));
  await user.keyboard('{Escape}');
  // Paired: an insight card's dialog still shows the served line its card was written from.
  await user.click(screen.getByRole('button', { name: `Read more: ${card.title}` }));
  const cardDialog = screen.getByRole('dialog');
  assert.notEqual(artifact.insights.evidence_line, null);
  assert.equal(evidenceLine(cardDialog), artifact.insights.evidence_line);
  assert.ok(chipKeepsTitle(cardDialog, card.title));
  await user.keyboard('{Escape}');
  view.unmount();
  // Paired: the shared dialog over the same served title, without the served-label flag, lowercases
  // it ("amazon dsp ad spend · us marketplace"), and the chip check fails.
  const slot: InsightDialogSlot = {
    absence_class: null, absence_reason: null, artifact_revision: artifact.insights.artifact_revision, evidence_line: null, id: 'row',
    next_step: null, output_class: 'observed', prompt_version: artifact.insights.prompt_version, state: 'filled', text: row.dialog_text,
  };
  const shared: InsightFocusSurface = { labelFor: () => row.title, slots: [slot], window: artifact.period };
  for (const [surface, kept] of [[{ ...shared, servedLabels: true }, true], [shared, false]] as const) {
    const alone = render(<InsightFocusDialog onClose={() => undefined} onSlotChange={() => undefined} slotId="row" surface={surface} surfaceKey="row" />);
    assert.equal(chipKeepsTitle(screen.getByRole('dialog'), row.title), kept);
    alone.unmount();
  }
  // The provenance line follows the served prompt stamp. Paired: a browser literal would show the
  // first stamp's line for the second too.
  for (const stamp of ['deterministic-v1', 'deterministic-gate1']) {
    const restamped: NetworkScorecardArtifact = { ...artifact, insights: { ...artifact.insights, prompt_version: stamp } };
    const page = render(<ScorecardView artifact={restamped} />);
    await user.click(screen.getByRole('button', { name: row.title }));
    assert.equal(screen.getByRole('dialog').querySelector('.prov-strip')?.textContent, PROVENANCE_READINGS[stamp]);
    await user.keyboard('{Escape}');
    page.unmount();
  }
});

test('the loading state keeps the window control and scope line in place, as shapes only', () => {
  const view = render(<><ScorecardControlsSkeleton /><ScorecardSkeleton /></>);
  // One shape per declared preset, the divider before the first calendar preset, no text.
  const options = view.container.querySelectorAll('.controls-skel .dc-wide .seg-opt');
  assert.equal(options.length, 4);
  const seg = view.container.querySelector('.controls-skel .dc-wide .seg');
  assert.deepEqual([...(seg?.children ?? [])].map((node) => node.className), ['seg-opt', 'seg-opt', 'seg-div', 'seg-opt', 'seg-opt']);
  assert.equal(view.container.querySelector('.controls-skel')?.textContent, '');
  assert.equal(view.container.querySelector('.controls-skel')?.getAttribute('aria-hidden'), 'true');
  assert.ok(view.container.querySelector('.scope-skel'));
  assert.equal(screen.getByRole('status').getAttribute('aria-label'), 'Loading Scorecard');
});

/** A control's accessible name as the browser computes it here: its aria-label, else its text. */
function nameOf(node: Element | null): string {
  return node === null ? '' : node.getAttribute('aria-label') ?? node.textContent ?? '';
}

/**
 * Whether every Also changed row shows its served marketplace, and no two rows share a visible
 * line or an accessible name (SC9A-GATE-R4-001), with each name the row's served title.
 */
function alsoChangedNamesDistinct(container: HTMLElement, artifact: NetworkScorecardArtifact): boolean {
  const rows = artifact.also_changed?.rows ?? [];
  const items = [...container.querySelectorAll('.also-changed li')];
  const markets = items.map((item) => item.querySelector('.wc-sub .market')?.textContent ?? null);
  const names = items.map((item) => nameOf(item.querySelector('button.view-details')));
  const lines = items.map((item) => item.textContent ?? '');
  return rows.length > 1 && items.length === rows.length
    && JSON.stringify(markets) === JSON.stringify(rows.map((row) => row.marketplace_text))
    && JSON.stringify(names) === JSON.stringify(rows.map((row) => row.title))
    && new Set(lines).size === items.length && new Set(names).size === items.length;
}

test('two marketplaces: each Also changed row names its marketplace and has its own name (SC9A-GATE-R4-001)', async () => {
  const user = userEvent.setup();
  const artifact = artifactOf(twoAlsoChanged);
  const view = render(<ScorecardView artifact={artifact} />);
  assert.ok(alsoChangedNamesDistinct(view.container, artifact));
  assert.deepEqual([...view.container.querySelectorAll('.also-changed li')].map((item) => item.textContent), [
    'Amazon DSP ad spend · UpSep 9–15 vs Sep 2–8 · inside Amazon DSP only · United Kingdom marketplace',
    'Amazon DSP ad spend · UpSep 9–15 vs Sep 2–8 · inside Amazon DSP only · US marketplace',
  ]);
  // The name is the visible label plus its marketplace (label in name), and opens that row.
  const us = screen.getByRole('button', { name: 'Amazon DSP ad spend · US marketplace' });
  assert.equal(us.textContent, 'Amazon DSP ad spend');
  await user.click(us);
  assert.ok(screen.getByRole('dialog', { name: /Amazon DSP ad spend · US marketplace/ }));
  await user.keyboard('{Escape}');
  const axe = await import('axe-core');
  const results = await axe.default.run(view.container, { rules: { 'color-contrast': { enabled: false }, region: { enabled: false } } });
  assert.deepEqual(results.violations, []);
  // Each card's figures are its own marketplace's (SC9A-GATE-R4-005): the UK returns 80%.
  const tables = [...view.container.querySelectorAll('#channel-table .dt-wrap')];
  assert.match(tables[0]?.querySelector('tr[data-channel="amazon_sponsored_products"]')?.textContent ?? '', /2,160\.00 GBP.*3\.60×/);
  assert.match(tables[1]?.querySelector('tr[data-channel="amazon_sponsored_products"]')?.textContent ?? '', /2,700\.00 USD.*4\.50×/);
  // Paired: the row's marketplace line dropped, or its served name dropped, is caught.
  const unlined = render(<ScorecardView artifact={artifact} />);
  unlined.container.querySelectorAll('.also-changed .market').forEach((node) => node.remove());
  assert.equal(alsoChangedNamesDistinct(unlined.container, artifact), false);
  unlined.unmount();
  const unnamed = render(<ScorecardView artifact={artifact} />);
  unnamed.container.querySelectorAll('.also-changed button.view-details').forEach((node) => node.removeAttribute('aria-label'));
  assert.equal(alsoChangedNamesDistinct(unnamed.container, artifact), false);
  unnamed.unmount();
  view.unmount();
  // Paired: one marketplace names none, and the control's name is its visible label.
  const single = artifactOf(alsoChanged);
  const one = render(<ScorecardView artifact={single} />);
  assert.equal(one.container.querySelectorAll('.also-changed .market').length, 0);
  assert.equal(one.container.querySelector('.also-changed button.view-details')?.getAttribute('aria-label'), null);
});

/** The channel rows of every table on the page, by channel label, in rendered order. */
function rowHeaders(container: HTMLElement): string[][] {
  return [...container.querySelectorAll('table')].map((table) => [...table.querySelectorAll('tbody th[scope="row"]')].map((cell) => cell.firstChild?.textContent ?? ''));
}

test('SC-1 renders four channel rows in the mockup order in both tables (SC9A-GATE-R4-002)', () => {
  const artifact = artifactOf(defaultBody);
  const view = render(<ScorecardView artifact={artifact} />);
  const governed = ['Amazon DSP', 'Sponsored Products', 'Sponsored Brands', 'Sponsored Display'];
  assert.deepEqual(rowHeaders(view.container), [governed, governed]);
  assert.match(view.container.querySelector('#channel-table .dt-foot')?.textContent ?? '', /No Sponsored Television delivery in this window\./);
  assert.ok(screen.getByRole('heading', { name: 'Also changed' }));
  assert.equal(view.container.querySelectorAll('.insights .insight').length, 3);
  view.unmount();
  // Paired: the round-4 default (two channels) fails the SC-1 row population.
  const two = render(<ScorecardView artifact={artifactOf(weekly)} />);
  assert.notDeepEqual(rowHeaders(two.container), [governed, governed]);
});

test('the A.3 states render their served ranking, card line and TACoS absence', () => {
  const sensitive = artifactOf(modelSensitive);
  const ranked = render(<ScorecardView artifact={sensitive} />);
  assert.equal(ranked.container.querySelector('.rank-outcome')?.textContent, 'The leading channel changes with the attribution model.');
  // The first-ranked row differs by model column: DSP under first touch, SP under the others.
  const leaders = [0, 1, 2].map((column) => [...ranked.container.querySelectorAll('.rank-block tbody tr')].find((row) => row.querySelectorAll('td')[column]?.textContent?.includes('1st'))?.querySelector('th')?.textContent);
  assert.deepEqual(leaders, ['Amazon DSP', 'Sponsored Products', 'Sponsored Products']);
  ranked.unmount();
  const undelivered = render(<ScorecardView artifact={artifactOf(noDsp)} />);
  assert.equal(undelivered.container.querySelector('.scorecard-card p.small.dim')?.textContent, 'No DSP delivery in this window');
  assert.equal(undelivered.container.querySelectorAll('tr[data-channel="amazon_dsp"]').length, 0);
  undelivered.unmount();
  const tacosTile = (container: HTMLElement) => [...container.querySelectorAll('.tile')].find((node) => node.querySelector('.k')?.textContent === 'Ad spend as a share of shipped sales');
  const vendor = render(<ScorecardView artifact={artifactOf(needsVendor)} />);
  assert.equal(tacosTile(vendor.container)?.querySelector('.v')?.textContent, 'Not available');
  assert.equal(tacosTile(vendor.container)?.querySelector('.reason')?.textContent, 'Needs Vendor Central sales');
  assert.equal(tacosTile(vendor.container)?.querySelectorAll('.chg').length, 0);
  vendor.unmount();
  // Paired: SC-1 shows a TACoS figure with its change, and a robust ranking, and DSP.
  const sc1 = render(<ScorecardView artifact={artifactOf(defaultBody)} />);
  assert.equal(tacosTile(sc1.container)?.querySelector('.reason'), null);
  assert.equal(tacosTile(sc1.container)?.querySelectorAll('.chg').length, 1);
  assert.equal(sc1.container.querySelector('.rank-outcome')?.textContent, 'Sponsored Products leads under all three models.');
  assert.equal(sc1.container.querySelector('.scorecard-card p.small.dim'), null);
});

test('a channel a model credits nothing reads "No deduplicated credit" with no rank, not "0.00× 2nd"', () => {
  const cellsOf = (container: HTMLElement, channel: string) => [...[...container.querySelectorAll('.rank-block tbody tr')]
    .find((row) => row.querySelector('th')?.textContent === channel)?.querySelectorAll('td') ?? []];
  const view = render(<ScorecardView artifact={artifactOf(noCredit)} />);
  const [first, last] = cellsOf(view.container, 'Sponsored Brands');
  assert.equal(first?.textContent?.trim(), 'No deduplicated credit');
  assert.equal(first?.querySelector('.rank'), null);
  // Paired: the same channel under last touch shows its served ratio and rank.
  assert.equal(last?.textContent?.trim(), `${formatRatio('4.00')} 2nd`);
  assert.equal(last?.querySelector('.rank')?.textContent, '2nd');
  view.unmount();
  // Paired: SC-1, where every channel has credit, names no cell.
  const sc1 = render(<ScorecardView artifact={artifactOf(defaultBody)} />);
  assert.equal(sc1.container.textContent?.includes('No deduplicated credit'), false);
  assert.equal(cellsOf(sc1.container, 'Sponsored Brands')[0]?.querySelector('.rank')?.textContent, '2nd');
  sc1.unmount();
});
/** The quiet insights section's sentence, or null when the section shows none. */
function quietReason(container: HTMLElement): string | null {
  const text = container.querySelector('.insight.quiet p')?.textContent ?? null;
  return text === null || text.trim() === '' ? null : text;
}

test('a quiet insights section shows the server reason, never a blank (SC9A-GATE-R4-003)', () => {
  const served = artifactOf(weekly);
  const silent = artifactOf(emptyBody).insights;
  const quiet: NetworkScorecardArtifact = { ...served, insight_cards: [], insights: silent };
  const view = render(<ScorecardView artifact={quiet} />);
  assert.equal(quietReason(view.container), silent.absence_reason);
  view.unmount();
  // Paired: a silent slot with no reason renders blank, which is why the decoder refuses it.
  const blank = render(<ScorecardView artifact={{ ...quiet, insights: { ...silent, absence_reason: null } }} />);
  assert.equal(quietReason(blank.container), null);
});
