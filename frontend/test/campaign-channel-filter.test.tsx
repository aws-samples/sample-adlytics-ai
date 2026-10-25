// SPDX-License-Identifier: MIT-0
// Campaigns channel filter (ADR-127): the server's own All channels and Amazon DSP pages, decoded
// and rendered — the control, the multi-channel tiles and tables, and the URL state.
import assert from 'node:assert/strict';

import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { test } from 'vitest';

import { CAMPAIGNS_PICK_CHANNEL, CampaignPortfolioV2View, PortfolioBandTitle } from '../components/CampaignPortfolioV2View';
import { campaignUrlState } from '../components/CampaignRouteAdapter';
import { CampaignAttributionContractError, parseCampaignPortfolioArtifact } from '../lib/campaign-attribution';
import { PresentationResponseError } from '../lib/presentation';
import { campaignPresentationV2, parsePresentationResultV2, type CampaignParentPresentation } from '../lib/presentation-v2';
import everything from './fixtures/campaigns-all-v2.json';
import dsp from './fixtures/campaigns-dsp-v2.json';

function parent(raw: unknown): Extract<CampaignParentPresentation, { readonly kind: 'parent' }> {
  const decoded = campaignPresentationV2(parsePresentationResultV2(raw, 'campaigns'));
  if (decoded.kind !== 'parent') throw new Error('expected the Campaigns parent');
  return decoded;
}

/** The baked body's own roster shard, as the server would serve it: its rows' names in order. */
function rosterSource(page: Extract<CampaignParentPresentation, { readonly kind: 'parent' }>) {
  const revision = page.artifact.roster_shards?.revision ?? '';
  const entries = page.artifact.rows.map((row, position) => ({
    channel: row.signature.channel,
    marketplace: row.signature.marketplace,
    name: row.signature.effective_name,
    position,
  }));
  return { load: async () => ({ entries, parent_revision: revision }), onStale: () => undefined };
}
function view(raw: unknown, onFilter: (filterId: string) => void = () => undefined) {
  const page = parent(raw);
  return render(<CampaignPortfolioV2View
    child={null}
    onClear={() => undefined}
    onFilter={onFilter}
    onSelect={() => undefined}
    parent={page.artifact}
    roster={rosterSource(page)}
    scorecardStep={page.scorecard_step}
    selection={{ choice: null, kind: 'idle' }}
  />);
}

test('All channels totals spend and impressions only and shows purchases and ROAS as not added', () => {
  const page = parent(everything);
  assert.deepEqual(page.artifact.totals.map((item) => item.metric), ['spend', 'impressions']);
  assert.equal(page.artifact.trend?.spend_only, true);
  view(everything);
  // UX sweep fix 6: one line replaces the two served "Not added across channels" tiles.
  assert.equal(screen.queryByText('Not added across channels'), null);
  assert.equal(screen.getAllByText(CAMPAIGNS_PICK_CHANNEL).length, 1);
  assert.ok(screen.getByText('Choose a channel to see purchases and ROAS over time.'));
  // Divergence reads a portfolio ROAS, so it isn't scheduled; the server line says what runs.
  assert.ok(screen.getByText(/Under All channels, insights cover spend and single-campaign changes/));
  assert.equal(screen.queryByText('Divergence'), null);
  // The spend-only trend offers no metric switch.
  assert.equal(screen.queryByRole('group', { name: 'Portfolio trend metric' }), null);
});

test('By channel no longer repeats the Scorecard spend table; delivery and the Scorecard line stay (fix 6)', () => {
  view(everything);
  assert.equal(screen.queryByRole('region', { name: 'Spend and impressions per channel' }), null);
  assert.ok(screen.getByRole('region', { name: 'Delivery per channel' }));
  assert.ok(screen.getByText('Return on ad spend per channel is on the Scorecard.'));
});

test('Delivery per channel shows the DSP viewable rate over its covered days and a reason for sponsored rows', () => {
  view(everything);
  const table = screen.getByRole('region', { name: 'Delivery per channel' });
  const cells = within(table).getAllByRole('columnheader');
  const headers = cells.map((cell) => cell.textContent ?? '');
  // The column name is the header's own text; its basis line is a child span.
  assert.deepEqual(cells.map((cell) => cell.firstChild?.textContent), [
    'Channel', 'Clicks', 'CTR', 'CPC', 'CPM', 'Viewable rate',
  ]);
  assert.ok(headers[3]!.includes('Ad spend ÷ clicks'));
  assert.ok(headers[2]!.includes('Clicks ÷ impressions'));
  assert.ok(headers[5]!.includes('Amazon DSP only'));
  const sponsored = within(table).getAllByRole('img', { name: "Not available: sponsored ads don't report viewability" });
  assert.equal(sponsored.length, 2);
  assert.ok(screen.getByText("— Not available: sponsored ads don't report viewability."));
});

test('A one-channel sibling keeps all four tiles and no per-channel tables', () => {
  const page = parent(dsp);
  assert.equal(page.artifact.channel_filter?.selected, 'amazon_dsp');
  assert.equal(page.artifact.channel_view, null);
  assert.deepEqual(page.artifact.totals.map((item) => item.metric), [
    'spend', 'purchases', 'impressions', 'portfolio_roas_attributed_14d',
  ]);
  view(dsp);
  assert.equal(screen.queryByText('Not added across channels'), null);
  assert.equal(screen.queryByRole('region', { name: 'Delivery per channel' }), null);
  assert.ok(screen.getByRole('group', { name: 'Portfolio trend metric' }));
});

test('A one-channel sibling with a no-spend day keeps the ROAS trend and marks that day absent', () => {
  const page = parent(dsp);
  const trend = page.artifact.trend;
  assert.ok(trend !== null && !trend.spend_only);
  // The fixture's Amazon DSP flight has days with no spend, so ROAS is typed absent on them.
  assert.ok(trend.points.some((point) => point.portfolio_roas_attributed_14d === null));
  view(dsp);
  const metrics = screen.getByRole('group', { name: 'Portfolio trend metric' });
  assert.deepEqual(within(metrics).getAllByRole('button').map((button) => button.textContent), [
    'Ad spend', 'Portfolio ROAS', 'Attributed purchases',
  ]);
  fireEvent.click(within(metrics).getByRole('button', { name: 'Portfolio ROAS' }));
  const panel = screen.getByRole('region', { name: 'Portfolio trend' });
  assert.ok(within(panel).getByRole('img', { name: /Portfolio ROAS across the returned date/ }));
  assert.ok(within(panel).getByText('— Not applicable: no spend that day.'));
  const absentDays = trend.points.filter((point) => point.portfolio_roas_attributed_14d === null).length;
  assert.equal(within(panel).getAllByRole('img', { name: 'Not applicable: no spend that day' }).length, absentDays);
});
test('An undefined delivery ratio reads "—" with the reason its served legend line gives', () => {
  const raw = structuredClone(everything) as unknown as { page: { artifact: { channel_view: { delivery: Record<string, Record<string, unknown>>[]; delivery_legend: string[] } } } };
  const served = raw.page.artifact.channel_view;
  const brands = served.delivery[2]!;
  // Sponsored Brands with no clicks in either window, as the server encodes it.
  const cpc = brands.cpc!;
  for (const side of ['current', 'prior'] as const) Object.assign(cpc[side] as object, { display_value: null, value: null });
  Object.assign(cpc, { absolute_change: null, change: null, percentage_change: null });
  served.delivery_legend.unshift('— Not applicable: no clicks in this window.');
  view(raw);
  const table = screen.getByRole('region', { name: 'Delivery per channel' });
  assert.ok(within(table).getByRole('img', { name: 'Not applicable: no clicks in this window' }));
  assert.ok(screen.getByText('— Not applicable: no clicks in this window.'));
});
test('A format-10 body is refused: no channel keys or a sparked row; the format-11 body decodes', () => {
  // Paired positive control: the server's own format-11 body names its channel filter.
  assert.equal(parent(structuredClone(dsp)).artifact.channel_filter.selected, 'amazon_dsp');
  const unfiltered = structuredClone(dsp) as unknown as { page: { artifact: Record<string, unknown> } };
  delete unfiltered.page.artifact.channel_filter;
  delete unfiltered.page.artifact.channel_view;
  assert.throws(() => parseCampaignPortfolioArtifact(unfiltered.page.artifact), CampaignAttributionContractError);
  assert.throws(() => parent(unfiltered), PresentationResponseError);
  const sparked = structuredClone(dsp) as unknown as { page: { artifact: { rows: Record<string, unknown>[] } } };
  sparked.page.artifact.rows[0]!.spend_spark = {};
  assert.throws(() => parseCampaignPortfolioArtifact(sparked.page.artifact), CampaignAttributionContractError);
  assert.throws(() => parent(sparked), PresentationResponseError);
});
test('Find a campaign names its roster scope and how Enter works, and announces no match', async () => {
  view(dsp);
  const input = screen.getByRole('combobox', { name: 'Find a campaign' });
  const hint = document.getElementById(input.getAttribute('aria-describedby') ?? '');
  assert.equal(hint?.textContent, "Enter on a match opens that campaign's detail. Matches come from Amazon DSP only.");
  // The status line is mounted empty, then fills, so a screen reader announces it.
  const status = screen.getByRole('status');
  assert.equal(status.textContent, '');
  // The baked roster loads on the field's first focus (ADR-145).
  fireEvent.focus(input);
  await waitFor(() => assert.equal(status.textContent, ''));
  fireEvent.change(input, { target: { value: 'zzz' } });
  assert.equal(screen.getByRole('status'), status);
  await waitFor(() => assert.match(status.textContent ?? '', /No Amazon DSP campaign matches “zzz”\./));
});
test('Under All channels a query with no match names no channel', async () => {
  view(everything);
  const input = screen.getByRole('combobox', { name: 'Find a campaign' });
  fireEvent.focus(input);
  fireEvent.change(input, { target: { value: 'zzz' } });
  await waitFor(() => assert.match(screen.getByRole('status').textContent ?? '', /^No campaign matches “zzz”\.$/));
});
test('The Channel control is a labelled select at four or more options and applies the chosen id', () => {
  const chosen: string[] = [];
  const page = parent(everything);
  render(<PortfolioBandTitle artifact={page.artifact} onFilter={(filterId) => { chosen.push(filterId); }} />);
  const select = screen.getByRole('combobox', { name: 'Channel' }) as HTMLSelectElement;
  assert.deepEqual([...select.options].map((option) => option.textContent), [
    'All channels', 'Sponsored ads (all sponsored channels)', 'Amazon DSP', 'Sponsored Products', 'Sponsored Brands',
  ]);
  fireEvent.change(select, { target: { value: 'amazon_dsp' } });
  assert.deepEqual(chosen, ['amazon_dsp']);
});

test('The URL holds governed preset and channel ids and never writes a default', () => {
  assert.deepEqual(campaignUrlState.read(new URLSearchParams('preset=14d&channel=amazon_sponsored_brands')), {
    intent: { filter: 'amazon_sponsored_brands' },
    selection: { kind: 'preset', preset_id: '14d' },
  });
  // A default, an unknown preset or an ungoverned value reads as omitted.
  assert.deepEqual(campaignUrlState.read(new URLSearchParams('preset=7d&channel=all')), { intent: {}, selection: undefined });
  assert.deepEqual(campaignUrlState.read(new URLSearchParams('preset=90d&channel=Amazon%20DSP')), { intent: {}, selection: undefined });
  assert.equal(campaignUrlState.write({ kind: 'preset', preset_id: '30d' }, { filter: 'amazon_dsp' }).toString(), 'preset=30d&channel=amazon_dsp');
  assert.equal(campaignUrlState.write({ kind: 'preset', preset_id: '7d' }, {}).toString(), '');
});
