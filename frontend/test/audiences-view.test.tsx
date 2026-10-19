// SPDX-License-Identifier: MIT-0
import assert from 'node:assert/strict';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, test } from 'vitest';

import { AudiencesView } from '@/components/AudiencesView';
import { parseAudienceProfileArtifact } from '@/lib/audiences';
import fixture from './fixtures/audiences-complete-v2.json';
import inRosterUnavailableFixture from './fixtures/audiences-in-roster-unavailable-v2.json';
import notObservedFixture from './fixtures/audiences-not-observed-v2.json';
import { audiencesFixture } from './fixtures/audiences-v1-fixtures';

afterEach(cleanup);
type Json = Record<string, unknown>;
const clone = <T,>(value: T): T => structuredClone(value);
const object = (value: unknown): Json => value as Json;
const objects = (value: unknown): Json[] => value as Json[];
const campaignRow = (value: unknown): Json => {
  const root = object(value);
  const market = objects(root.markets)[0]!;
  const campaigns = object(market.campaign_audiences);
  return objects(campaigns.rows)[0]!;
};
const view = (value: unknown = fixture) => render(<AudiencesView artifact={parseAudienceProfileArtifact(value)} />);
const TOTAL_REASON = "Not available: this month's profiled total didn't come back, so no share or index is computed.";
const GAP_REASON = 'Amazon returned too few shoppers for this segment this month. It was listed last month.';
/** The one unavailable-row contract: the reason once in the first metric cell, described hidden markers elsewhere. */
function assertUnavailableRow(row: HTMLElement, reason: string) {
  assert.equal(within(row).getAllByText(reason).length, 1);
  const [first, ...rest] = within(row).getAllByRole('cell');
  assert.equal(first?.textContent, reason);
  const reasonElement = within(first!).getByText(reason);
  assert.ok(reasonElement.id);
  assert.ok(rest.length > 0);
  for (const cell of rest) {
    assert.equal(cell.getAttribute('aria-describedby'), reasonElement.id);
    assert.equal(cell.textContent, '—');
    assert.equal(within(cell).getByText('—').getAttribute('aria-hidden'), 'true');
  }
}

test('renders DSP profile, campaign figures and server text without browser formatting', () => {
  const { container } = view();
  assert.ok(screen.getByText('Showing September 2026 (Latest measured month).'));
  const profile = screen.getByRole('region', { name: /Segments your buyers belong to.*DSP-exposed buyers only/ });
  assert.ok(within(profile).getByText('40.0%'));
  assert.ok(within(profile).getByText('1.33x'));
  assert.ok(screen.getByText('7.00 USD'));
  assert.ok(screen.getByText('0.59x'));
  assert.equal(container.textContent?.includes('0.4000'), false);
  assert.equal(container.textContent?.includes('30000'), true);
});

test('renders a roster gap with one reason and described visual-only markers in both buyer modes', () => {
  view();
  assert.equal(screen.getAllByText(GAP_REASON).length, 1);
  assertUnavailableRow(screen.getByRole('row', { name: /Segment 41003/ }), GAP_REASON);
  fireEvent.click(screen.getByRole('radio', { name: 'New-to-brand buyers' }));
  assertUnavailableRow(screen.getByRole('row', { name: /Segment 41003/ }), GAP_REASON);
});

test('renders an in-roster not-available row through the same renderer in both buyer modes, with no count', () => {
  view(inRosterUnavailableFixture);
  const inMarketRow = () => within(screen.getByRole('table', { name: /In-market/ })).getByRole('row', { name: /Segment 41001/ });
  assert.equal((screen.getByRole('radio', { name: 'Amazon audience segments (paid)' }) as HTMLInputElement).checked, true);
  assertUnavailableRow(inMarketRow(), TOTAL_REASON);
  fireEvent.click(screen.getByRole('radio', { name: 'New-to-brand buyers' }));
  assertUnavailableRow(inMarketRow(), TOTAL_REASON);
  assert.equal(inMarketRow().textContent?.includes('16'), false);
  // Control: the Lifestyle row of the same segment still shows its measured new-to-brand count.
  assert.ok(within(screen.getByRole('table', { name: /Lifestyle/ })).getByText('21'));
});

test('the unavailable-row contract refuses a triple-repeated reason and a count beside the reason', () => {
  const repeated = render(<table><tbody><tr><th scope="row">Segment 41001</th>{['buyers', 'reached', 'index'].map((key) => <td key={key}>{TOTAL_REASON}</td>)}</tr></tbody></table>);
  assert.throws(() => assertUnavailableRow(within(repeated.container).getByRole('row'), TOTAL_REASON));
  repeated.unmount();
  const counted = render(<table><tbody><tr><th scope="row">Segment 41001</th><td>16</td><td><span id="reason">{TOTAL_REASON}</span></td><td aria-describedby="reason"><span aria-hidden="true">—</span></td></tr></tbody></table>);
  assert.throws(() => assertUnavailableRow(within(counted.container).getByRole('row'), TOTAL_REASON));
});

test('switches to new-to-brand and both paid deliveries without a request', () => {
  view();
  fireEvent.click(screen.getByRole('radio', { name: 'New-to-brand buyers' }));
  assert.ok(screen.getByText('19'));
  fireEvent.click(screen.getByRole('radio', { name: 'Amazon audience segments (paid)' }));
  assert.equal(screen.getAllByText('Figures for September 2026 are expected after 16 Oct 2026.').length, 2);
  assert.ok(document.body.textContent?.includes('In-market'));
  assert.ok(document.body.textContent?.includes('Lifestyle'));
});

test('refuses stale campaign display text after raw values change', () => {
  const changed = clone(fixture);
  const campaign = campaignRow(changed);
  campaign.segment_impressions = 9;
  object(campaign.segment_cpm).value = '999.99';
  campaign.segment_roas_attributed = '88.88';
  assert.throws(() => parseAudienceProfileArtifact(changed), TypeError);
});

test('renders campaign absence reasons', () => {
  const absent = clone(fixture);
  const row = campaignRow(absent);
  row.segment_cpm = null; row.cpm_text = 'Not available: no impressions came back for this segment.';
  row.segment_roas_attributed = null; row.segment_roas_attributed_text = null; row.roas_text = "Not available: attributed sales didn't come back for this segment.";
  view(absent);
  assert.ok(screen.getByText('Not available: no impressions came back for this segment.'));
  assert.ok(screen.getByText("Not available: attributed sales didn't come back for this segment."));
});

test('renders both paid-feature-unavailable callouts', () => {
  view(notObservedFixture);
  assert.ok(screen.getByRole('note').textContent?.includes('Available with the paid feature'));
  fireEvent.click(screen.getByRole('radio', { name: 'Amazon audience segments (paid)' }));
  assert.ok(screen.getByRole('status').textContent?.includes('Needs the Amazon Insights paid feature'));
});

test('every outcome renders exactly one collapsed boundary with both server lines', () => {
  for (const outcome of ['complete', 'empty', 'insufficient'] as const) {
    const artifact = parseAudienceProfileArtifact(audiencesFixture(outcome));
    const rendered = render(<AudiencesView artifact={artifact} />);
    const summaries = within(rendered.container).getAllByText('About these figures');
    assert.equal(summaries.length, 1, outcome);
    const details = summaries[0]!.closest('details')!;
    assert.equal(details.open, false, outcome);
    for (const line of artifact.boundary) assert.ok(within(details).getByText(line), `${outcome}: ${line}`);
    rendered.unmount();
  }
  // Control: an artifact without its boundary is refused before it can render.
  for (const boundary of [undefined, []]) {
    const missing = audiencesFixture('empty');
    if (boundary === undefined) delete missing.boundary; else missing.boundary = boundary;
    assert.throws(() => parseAudienceProfileArtifact(missing), TypeError);
  }
});

test('renders a non-empty server status for a non-complete outcome', () => {
  view(audiencesFixture('insufficient'));
  const status = screen.getByRole('status');
  assert.ok(status.textContent?.includes('Figures for September 2026 are expected after 16 Oct 2026.'));
});

test('Audiences complete, paid-unavailable, in-roster-unavailable and insufficient surfaces are axe-clean in wide and narrow containers under both themes', async () => {
  const axe = await import('axe-core');
  const artifacts = { complete: fixture, 'in-roster-unavailable': inRosterUnavailableFixture, insufficient: audiencesFixture('insufficient'), 'not-observed': notObservedFixture };
  for (const [name, artifact] of Object.entries(artifacts)) for (const theme of ['light', 'dark'] as const) for (const width of ['1100px', '420px'] as const) {
    document.documentElement.dataset.theme = theme;
    const rendered = render(<div style={{ width }}><AudiencesView artifact={parseAudienceProfileArtifact(artifact)} /></div>);
    const result = await axe.default.run(rendered.container, { rules: { 'color-contrast': { enabled: false } } });
    assert.deepEqual(result.violations.map((violation) => violation.id), [], `${name} ${theme} ${width}`);
    rendered.unmount();
  }
  delete document.documentElement.dataset.theme;
});
test('a refreshed artifact without the chosen campaign shows the campaign it still carries', () => {
  const two = clone(fixture) as Json;
  const campaigns = object(objects(two.markets)[0]!.campaign_audiences);
  const second = { ...objects(campaigns.rows)[0]!, campaign_label: 'Unnamed campaign 1', campaign_ref: 'campaign-2' };
  campaigns.rows = [...objects(campaigns.rows), second];
  const rendered = view(two);
  fireEvent.change(screen.getByRole('combobox', { name: 'Campaign' }), { target: { value: 'campaign-2' } });
  assert.ok(screen.getByRole('region', { name: /Unnamed campaign 1, by segment/ }));
  // The page context refresh replaces the artifact in place, so this component's choice survives it.
  rendered.rerender(<AudiencesView artifact={parseAudienceProfileArtifact(fixture)} />);
  assert.ok(screen.getByRole('region', { name: /Fall prospecting, by segment/ }));
  assert.equal(screen.queryByText(/Unnamed campaign 1/), null);
});
/** The in-roster-unavailable month, its profile declared insufficient, with the campaigns given. */
function insufficientProfile(campaigns: (value: Json) => void): Json {
  const value = clone(inRosterUnavailableFixture) as Json;
  value.outcome = 'insufficient';
  value.starter_questions = [];
  const market = objects(value.markets)[0]!;
  market.outcome = 'insufficient';
  campaigns(object(market.campaign_audiences));
  return value;
}
test('an insufficient profile keeps its callout, month scope and complete campaign rows in order', () => {
  const rendered = view(insufficientProfile(() => undefined));
  const scope = screen.getByText('Showing September 2026 (Latest measured month).');
  const campaigns = screen.getByRole('heading', { name: 'DSP campaigns by segment' }).closest('section')!;
  assert.ok(screen.getByRole('heading', { level: 2, name: 'Audiences' }));
  assert.ok(scope.compareDocumentPosition(campaigns) & Node.DOCUMENT_POSITION_FOLLOWING);
  assert.ok(screen.getByRole('region', { name: /Fall prospecting, by segment/ }));
  assert.equal(screen.queryByRole('heading', { name: 'Segment profile' }), null);
  rendered.unmount();
  // Control: with the campaigns pending, only the profile callout renders.
  view(insufficientProfile((campaigns) => {
    campaigns.rows = [];
    campaigns.pending_text = "DSP campaigns by segment for September 2026 haven't arrived yet.";
  }));
  assert.ok(screen.getByRole('heading', { level: 2, name: 'Audiences' }));
  assert.equal(screen.queryByRole('heading', { name: 'DSP campaigns by segment' }), null);
});
test('the server shown note renders as served, and only when served', () => {
  const bounded = clone(fixture) as Json;
  object(objects(bounded.markets)[0]!.campaign_audiences).shown_text = 'Showing 1 of 14 campaigns';
  const rendered = view(bounded);
  const section = screen.getByRole('heading', { name: 'DSP campaigns by segment' }).closest('section')!;
  assert.ok(within(section).getByText('Showing 1 of 14 campaigns'));
  rendered.unmount();
  // Control: every listed campaign shown, so no note.
  view();
  const complete = screen.getByRole('heading', { name: 'DSP campaigns by segment' }).closest('section')!;
  assert.equal(within(complete).queryByText(/^Showing \d+ of \d+ campaigns$/), null);
});
test('pending campaigns show their own absence state beside a complete profile', () => {
  const pending = clone(fixture) as Json;
  const campaigns = object(objects(pending.markets)[0]!.campaign_audiences);
  campaigns.rows = [];
  campaigns.pending_text = "DSP campaigns by segment for September 2026 haven't arrived yet.";
  const rendered = view(pending);
  const section = screen.getByRole('heading', { name: 'DSP campaigns by segment' }).closest('section')!;
  assert.ok(within(section).getByRole('status'));
  assert.ok(within(section).getByText(/haven't arrived yet/));
  assert.equal(within(section).queryByRole('region'), null);
  rendered.unmount();
  // Control: complete campaigns show their table and no pending state.
  view();
  const complete = screen.getByRole('heading', { name: 'DSP campaigns by segment' }).closest('section')!;
  assert.ok(within(complete).getByRole('region', { name: /Fall prospecting, by segment/ }));
  assert.equal(within(complete).queryByText(/haven't arrived yet/), null);
});
