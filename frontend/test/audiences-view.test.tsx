// SPDX-License-Identifier: MIT-0
import assert from 'node:assert/strict';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, test } from 'vitest';

import { AUD_CAMPAIGN_GONE, AUD_PAGE_FETCH_FAILED, AUD_PAGE_REVISION_CHANGED, clearAudiencesResume } from '@/components/AudiencesParts';
import { AUDIENCES_ACTIVATE_LINE, AUDIENCES_CAMPAIGNS_SUMMARY, AudiencesView } from '@/components/AudiencesView';
import { parseAudienceProfileArtifact, parseAudiencesPart } from '@/lib/audiences';
import fixture from './fixtures/audiences-complete-v2.json';
import campaignPage from './fixtures/audiences-campaign-page-v1.json';
import inRosterUnavailableFixture from './fixtures/audiences-in-roster-unavailable-v2.json';
import notObservedFixture from './fixtures/audiences-not-observed-v2.json';
import { audiencesFixture } from './fixtures/audiences-v1-fixtures';

afterEach(() => {
  cleanup();
  clearAudiencesResume();
  document.cookie = 'adl_csrf=; Max-Age=0; path=/';
});
type Json = Record<string, unknown>;
const clone = <T,>(value: T): T => structuredClone(value);
const object = (value: unknown): Json => value as Json;
const objects = (value: unknown): Json[] => value as Json[];
const SESSION = 'conversation-fictional';
const REVISION = fixture.revision;
const NEWER = 'b'.repeat(64);
const NEWEST = 'c'.repeat(64);
const campaignsOf = (value: Json): Json => object(objects(value.markets)[0]!.campaign_audiences);
/** One served campaign page: the fixture's page 0, overridden. */
const page = (overrides: Json = {}): Json => ({ ...clone(campaignPage), ...overrides });
const firstRow = (part: Json): Json => objects(part.rows)[0]!;

/**
 * Serve every part request from `answer` and record each request body, so a test counts the
 * browser's fetches. The response wraps the part as the server's handler does.
 */
function serveParts(answer: (body: Json) => Json) {
  const calls: Json[] = [];
  document.cookie = 'adl_csrf=csrf; path=/';
  Object.defineProperty(globalThis, 'fetch', { configurable: true, value: async (_input: unknown, init?: RequestInit) => {
    const body = JSON.parse(String(init?.body)) as Json;
    calls.push(body);
    const part = answer(body);
    return new Response(JSON.stringify({ kind: 'audiences_part', parent_revision: part.parent_revision, part, session_id: SESSION }), { status: 200 });
  } });
  return calls;
}
const view = (value: unknown = fixture, reload: () => void = () => undefined) => render(<AudiencesView artifact={parseAudienceProfileArtifact(value)} reload={reload} session={SESSION} />);
function chooseCampaign(label: string) {
  fireEvent.focus(screen.getByRole('combobox', { name: 'Campaign' }));
  fireEvent.mouseDown(screen.getByRole('option', { name: label }));
}
/** A parent listing `labels` as its campaigns, each with `segments` segments, under `revision`. */
function withCampaigns(labels: readonly string[], revision = REVISION, segments = 1): Json {
  const value = clone(fixture) as Json;
  value.revision = revision;
  const campaigns = campaignsOf(value);
  campaigns.campaign_count = labels.length;
  campaigns.index = labels.map((label, index) => ({ campaign_label: label, campaign_ref: `campaign-${index + 1}`, page_count: Math.ceil(segments / 50), segment_count: segments, state: 'available', text: null }));
  return value;
}

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

test('renders DSP profile, campaign figures and server text without browser formatting', async () => {
  const calls = serveParts(() => page());
  const { container } = view();
  assert.ok(screen.getByText('Showing September 2026 (Latest measured month).'));
  const profile = screen.getByRole('region', { name: /Segments your buyers belong to.*DSP-exposed buyers only/ });
  assert.ok(within(profile).getByText('40.0%'));
  // Plan D4: the segment index reads as a governed multiplier.
  assert.ok(within(profile).getByText('1.33×'));
  assert.equal(container.textContent?.includes('0.4000'), false);
  // A campaign's figures are fetched only when it is chosen.
  assert.equal(calls.length, 0);
  assert.equal(screen.queryByText('7.00 USD'), null);
  chooseCampaign('Fall prospecting');
  assert.ok(await screen.findByText('7.00 USD'));
  assert.ok(screen.getByText('0.59×'));
  // Plan D4: the served count's own digits, grouped; the ungrouped string is gone.
  assert.ok(screen.getByText('30,000'));
  assert.equal(container.textContent?.includes('30000'), false);
  // One page, so one fetch and no prefetch.
  assert.deepEqual(calls, [{ campaign: { page: 0, ref: 'campaign-1' }, session_id: SESSION }]);
});
test('the activation line shows, share bars pair each served row, and campaigns fold into one disclosure (UI refresh 3.4)', () => {
  const { container } = view();
  assert.ok(screen.getByText(AUDIENCES_ACTIVATE_LINE));
  const rows = container.querySelectorAll('.audience-delivery tbody tr:not(.na-row)');
  assert.ok(rows.length > 0);
  for (const row of rows) assert.equal(row.querySelectorAll('.au-bars .au-bar').length, 2);
  const campaigns = container.querySelector('[data-slot="au-campaigns"]') as HTMLDetailsElement;
  assert.equal(campaigns.open, false);
  assert.equal(campaigns.querySelector('summary')?.textContent, AUDIENCES_CAMPAIGNS_SUMMARY);
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
  const calls = serveParts(() => page());
  view();
  fireEvent.click(screen.getByRole('radio', { name: 'New-to-brand buyers' }));
  assert.ok(screen.getByText('19'));
  fireEvent.click(screen.getByRole('radio', { name: 'Amazon audience segments (paid)' }));
  assert.equal(screen.getAllByText('Figures for September 2026 are expected after 16 Oct 2026.').length, 2);
  assert.ok(document.body.textContent?.includes('In-market'));
  assert.ok(document.body.textContent?.includes('Lifestyle'));
  assert.equal(calls.length, 0);
});

test('refuses stale campaign display text after raw values change', () => {
  const changed = page();
  const row = firstRow(changed);
  row.segment_impressions = 9;
  object(row.segment_cpm).value = '999.99';
  row.segment_roas_attributed = '88.88';
  assert.throws(() => parseAudiencesPart(changed, { kind: 'campaign', page: 0, ref: 'campaign-1' }), TypeError);
  // Control: the served page decodes.
  assert.equal(parseAudiencesPart(page(), { kind: 'campaign', page: 0, ref: 'campaign-1' }).part.kind, 'campaign');
});

test('renders campaign absence reasons', async () => {
  const absent = page();
  const row = firstRow(absent);
  row.segment_cpm = null; row.cpm_text = 'Not available: no impressions came back for this segment.';
  row.segment_roas_attributed = null; row.segment_roas_attributed_text = null; row.roas_text = "Not available: attributed sales didn't come back for this segment.";
  serveParts(() => absent);
  view();
  chooseCampaign('Fall prospecting');
  assert.ok(await screen.findByText('Not available: no impressions came back for this segment.'));
  assert.ok(screen.getByText("Not available: attributed sales didn't come back for this segment."));
});

test('a refused or withdrawn campaign shows the server sentence in place of its table, with no fetch for a refusal', async () => {
  const refused = withCampaigns(['Fall prospecting']);
  Object.assign(objects(campaignsOf(refused).index)[0]!, { page_count: 0, state: 'refused', text: 'This campaign is not shown for September 2026.' });
  const calls = serveParts(() => page());
  const rendered = view(refused);
  chooseCampaign('Fall prospecting');
  assert.ok(screen.getByText('This campaign is not shown for September 2026.'));
  assert.equal(calls.length, 0);
  rendered.unmount();
  // A withdrawn page is fetched, and its sentence replaces the table.
  serveParts(() => page({ paging_text: null, rows: [], state: 'withdrawn', text: 'This campaign was withdrawn.' }));
  view();
  chooseCampaign('Fall prospecting');
  assert.ok(await screen.findByText('This campaign was withdrawn.'));
  assert.equal(screen.queryByRole('region', { name: /Fall prospecting, by segment/ }), null);
});

test('the campaign selector is a combobox over the index that filters by name on the client', () => {
  const calls = serveParts(() => page());
  view(withCampaigns(['Fall prospecting', 'Spring launch', 'Holiday retargeting']));
  const box = screen.getByRole('combobox', { name: 'Campaign' });
  assert.equal(box.getAttribute('aria-expanded'), 'false');
  fireEvent.focus(box);
  assert.equal(box.getAttribute('aria-expanded'), 'true');
  const list = screen.getByRole('listbox', { name: 'Campaigns' });
  assert.equal(box.getAttribute('aria-controls'), list.id);
  assert.equal(within(list).getAllByRole('option').length, 3);
  fireEvent.change(box, { target: { value: 'spring' } });
  assert.deepEqual(within(list).getAllByRole('option').map((option) => option.textContent), ['Spring launch']);
  fireEvent.keyDown(box, { key: 'ArrowDown' });
  assert.equal(box.getAttribute('aria-activedescendant'), within(list).getByRole('option').id);
  // Typing and moving through the list fetch nothing; only a choice does.
  assert.equal(calls.length, 0);
});

test('a chosen campaign fetches page 0, prefetches at most its next page, and pages with the server text verbatim', async () => {
  const pages = ['Segments 1–50 of 120', 'Segments 51–100 of 120', 'Segments 101–120 of 120'];
  const calls = serveParts((body) => {
    const index = object(body.campaign).page as number;
    return page({ page: index, page_count: 3, paging_text: pages[index] });
  });
  view(withCampaigns(['Fall prospecting', 'Spring launch'], REVISION, 120));
  chooseCampaign('Fall prospecting');
  assert.ok(await screen.findByText(pages[0]!, { selector: '.pager span' }));
  await waitFor(() => assert.equal(calls.length, 2));
  // Page 0 on selection, then exactly one prefetch: the open campaign's next page.
  assert.deepEqual(calls.map((call) => call.campaign), [{ page: 0, ref: 'campaign-1' }, { page: 1, ref: 'campaign-1' }]);
  assert.equal(screen.getByRole('button', { name: 'Previous segments, Fall prospecting' }).hasAttribute('disabled'), true);
  fireEvent.click(screen.getByRole('button', { name: 'Next segments, Fall prospecting' }));
  // The prefetched page shows without another fetch for it; its own next page is prefetched.
  assert.ok(await screen.findByText(pages[1]!, { selector: '.pager span' }));
  await waitFor(() => assert.equal(calls.length, 3));
  assert.deepEqual(calls.map((call) => call.campaign), [{ page: 0, ref: 'campaign-1' }, { page: 1, ref: 'campaign-1' }, { page: 2, ref: 'campaign-1' }]);
  // After a page change, focus moves to the table and the status region says which rows show.
  assert.equal(document.activeElement?.getAttribute('aria-label'), 'Fall prospecting, by segment');
  assert.ok(screen.getByText(pages[1]!, { selector: '.sr-only' }));
  // No other campaign was fetched.
  assert.equal(calls.some((call) => object(call.campaign).ref !== 'campaign-1'), false);
});

test('a profile page after the first is fetched on paging, under the parent revision', async () => {
  const paged = clone(fixture) as Json;
  const delivery = objects(objects(objects(paged.markets)[0]!.views)[1]!.deliveries)[0]!;
  delivery.page_count = 2;
  delivery.paging_text = 'Segments 1–50 of 60';
  const second = { ...clone(objects(objects(objects(fixture.markets)[0]!.views)[1]!.rows)[0]!), display_name: 'Demo - Age 35-44 · Amazon', name: 'Demo - Age 35-44', segment_id: '41009' };
  const calls = serveParts(() => ({ not_available: [], page: 1, page_count: 2, paging_text: 'Segments 51–60 of 60', parent_revision: REVISION, profile_path: 'dsp_exposed_only', rows: [second], total_returned: true, type: 'AudienceProfilePage', version: 1 }));
  view(paged);
  assert.ok(screen.getByText('Segments 1–50 of 60'));
  assert.equal(calls.length, 0);
  fireEvent.click(screen.getByRole('button', { name: 'Next segments, DSP behaviour segments' }));
  assert.ok(await screen.findByText('Segments 51–60 of 60', { selector: '.pager span' }));
  assert.ok(screen.getByRole('row', { name: /Demo - Age 35-44/ }));
  assert.deepEqual(calls, [{ profile: { page: 1, path: 'dsp_exposed_only' }, session_id: SESSION }]);
  // Page 0 is the parent's own rows: going back fetches nothing.
  fireEvent.click(screen.getByRole('button', { name: 'Previous segments, DSP behaviour segments' }));
  assert.ok(screen.getByText('Segments 1–50 of 60', { selector: '.pager span' }));
  assert.equal(calls.length, 1);
});

test('a part from a newer publication reloads the parent once and re-asks by campaign; a second mismatch fails', async () => {
  let reloads = 0;
  let calls = serveParts(() => page({ parent_revision: NEWER }));
  view(withCampaigns(['Fall prospecting']), () => { reloads += 1; });
  chooseCampaign('Fall prospecting');
  await waitFor(() => assert.equal(reloads, 1));
  assert.equal(screen.queryByText('7.00 USD'), null);
  cleanup();
  // The reloaded parent re-asks for the same campaign by its label and says the page changed.
  calls = serveParts(() => page({ parent_revision: NEWEST }));
  view(withCampaigns(['Fall prospecting'], NEWER), () => { reloads += 1; });
  assert.ok(screen.getByText(AUD_PAGE_REVISION_CHANGED));
  assert.ok(await screen.findByText(AUD_PAGE_FETCH_FAILED));
  assert.equal(reloads, 1);
  assert.equal(calls.length, 1);
  cleanup();
  // Control: when the re-asked part matches the reloaded parent, it shows.
  view(withCampaigns(['Fall prospecting']), () => { reloads += 1; });
  serveParts(() => page({ parent_revision: NEWER }));
  chooseCampaign('Fall prospecting');
  await waitFor(() => assert.equal(reloads, 2));
  cleanup();
  serveParts(() => page({ parent_revision: NEWER }));
  view(withCampaigns(['Fall prospecting'], NEWER));
  assert.ok(await screen.findByText('7.00 USD'));
  assert.equal(screen.queryByText(AUD_PAGE_FETCH_FAILED), null);
});

test('a failed part fetch shows the retry row, and retrying fetches the same part', async () => {
  let fail = true;
  document.cookie = 'adl_csrf=csrf; path=/';
  const calls: Json[] = [];
  Object.defineProperty(globalThis, 'fetch', { configurable: true, value: async (_input: unknown, init?: RequestInit) => {
    calls.push(JSON.parse(String(init?.body)) as Json);
    if (fail) return new Response('{}', { status: 503 });
    return new Response(JSON.stringify({ kind: 'audiences_part', parent_revision: REVISION, part: page(), session_id: SESSION }), { status: 200 });
  } });
  view();
  chooseCampaign('Fall prospecting');
  assert.ok(await screen.findByText(AUD_PAGE_FETCH_FAILED));
  fail = false;
  fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
  assert.ok(await screen.findByText('7.00 USD'));
  assert.equal(calls.length, 2);
  assert.deepEqual(calls[0], calls[1]);
});

test('renders both paid-feature-unavailable callouts', () => {
  view(notObservedFixture);
  assert.ok(screen.getByRole('note').textContent?.includes('Available with the paid feature'));
  fireEvent.click(screen.getByRole('radio', { name: 'Amazon audience segments (paid)' }));
  assert.ok(screen.getAllByRole('status').some((status) => status.textContent?.includes('Needs the Amazon Insights paid feature')));
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
  assert.ok(screen.getAllByRole('status').some((status) => status.textContent?.includes('Figures for September 2026 are expected after 16 Oct 2026.')));
});

test('Audiences complete, paid-unavailable, in-roster-unavailable, insufficient and open-campaign surfaces are axe-clean in wide and narrow containers under both themes', async () => {
  const axe = await import('axe-core');
  serveParts(() => page());
  const artifacts = { complete: fixture, 'in-roster-unavailable': inRosterUnavailableFixture, insufficient: audiencesFixture('insufficient'), 'not-observed': notObservedFixture, 'open-campaign': fixture };
  for (const [name, artifact] of Object.entries(artifacts)) for (const theme of ['light', 'dark'] as const) for (const width of ['1100px', '420px'] as const) {
    document.documentElement.dataset.theme = theme;
    const rendered = render(<div style={{ width }}><AudiencesView artifact={parseAudienceProfileArtifact(artifact)} session={SESSION} /></div>);
    if (name === 'open-campaign') {
      fireEvent.focus(within(rendered.container).getByRole('combobox', { name: 'Campaign' }));
      fireEvent.mouseDown(within(rendered.container).getByRole('option', { name: 'Fall prospecting' }));
      await within(rendered.container).findByText('7.00 USD');
    }
    const result = await axe.default.run(rendered.container, { rules: { 'color-contrast': { enabled: false } } });
    assert.deepEqual(result.violations.map((violation) => violation.id), [], `${name} ${theme} ${width}`);
    rendered.unmount();
  }
  delete document.documentElement.dataset.theme;
  // Sixteen full-page axe runs exceed vitest's 5 s default when the gates share the machine.
}, 30_000);
test('a refreshed parent keeps a chosen campaign it still lists, and clears one it no longer lists', async () => {
  const calls = serveParts((body) => {
    const ref = object(body.campaign).ref as string;
    // The first parent lists Spring launch second; the newer parent lists it first.
    const [revision, spring] = calls.length <= 1 ? [REVISION, 'campaign-2'] : [NEWER, 'campaign-1'];
    return page({ campaign_label: ref === spring ? 'Spring launch' : 'Fall prospecting', campaign_ref: ref, parent_revision: revision });
  });
  const rendered = view(withCampaigns(['Fall prospecting', 'Spring launch']));
  chooseCampaign('Spring launch');
  assert.ok(await screen.findByRole('region', { name: /Spring launch, by segment/ }));
  // A newer parent still lists it, at another reference: the same campaign is asked for by its name.
  rendered.rerender(<AudiencesView artifact={parseAudienceProfileArtifact(withCampaigns(['Spring launch', 'Fall prospecting'], NEWER))} session={SESSION} />);
  assert.ok(await screen.findByRole('region', { name: /Spring launch, by segment/ }));
  assert.deepEqual(calls.map((call) => object(call.campaign).ref), ['campaign-2', 'campaign-1']);
  // A parent that no longer lists it clears the selector and says so, with no fetch.
  rendered.rerender(<AudiencesView artifact={parseAudienceProfileArtifact(withCampaigns(['Fall prospecting'], NEWEST))} session={SESSION} />);
  await act(async () => undefined);
  assert.equal(screen.queryByRole('region', { name: /, by segment$/ }), null);
  assert.equal((screen.getByRole('combobox', { name: 'Campaign' }) as HTMLInputElement).value, '');
  assert.ok(screen.getByText(AUD_CAMPAIGN_GONE));
  assert.equal(calls.length, 2);
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
test('an insufficient profile keeps its callout, month scope and campaign selector in order', async () => {
  serveParts(() => page({ parent_revision: inRosterUnavailableFixture.revision }));
  const rendered = view(insufficientProfile(() => undefined));
  const scope = screen.getByText('Showing September 2026 (Latest measured month).');
  const campaigns = screen.getByRole('heading', { name: 'DSP campaigns by segment' }).closest('section')!;
  assert.ok(screen.getByRole('heading', { level: 2, name: 'Audiences' }));
  assert.ok(scope.compareDocumentPosition(campaigns) & Node.DOCUMENT_POSITION_FOLLOWING);
  chooseCampaign('Fall prospecting');
  assert.ok(await screen.findByRole('region', { name: /Fall prospecting, by segment/ }));
  assert.equal(screen.queryByRole('heading', { name: 'Segment profile' }), null);
  rendered.unmount();
  // Control: with the campaigns pending, only the profile callout renders.
  view(insufficientProfile((campaigns) => {
    campaigns.index = [];
    campaigns.campaign_count = 0;
    campaigns.pending_text = "DSP campaigns by segment for September 2026 haven't arrived yet.";
  }));
  assert.ok(screen.getByRole('heading', { level: 2, name: 'Audiences' }));
  assert.equal(screen.queryByRole('heading', { name: 'DSP campaigns by segment' }), null);
});
test('a refused campaign section shows the server sentence beside the profile', () => {
  const refused = clone(fixture) as Json;
  Object.assign(campaignsOf(refused), { campaign_count: 0, index: [], refused_text: "DSP campaigns by segment for September 2026 couldn't be shown." });
  view(refused);
  const section = screen.getByRole('heading', { name: 'DSP campaigns by segment' }).closest('section')!;
  assert.ok(within(section).getByText("DSP campaigns by segment for September 2026 couldn't be shown."));
  assert.equal(within(section).queryByRole('combobox'), null);
  assert.ok(screen.getByRole('heading', { name: 'Segment profile' }));
});
test('pending campaigns show their own absence state beside a complete profile', () => {
  const pending = clone(fixture) as Json;
  Object.assign(campaignsOf(pending), { campaign_count: 0, index: [], pending_text: "DSP campaigns by segment for September 2026 haven't arrived yet." });
  const rendered = view(pending);
  const section = screen.getByRole('heading', { name: 'DSP campaigns by segment' }).closest('section')!;
  assert.ok(within(section).getByRole('status'));
  assert.ok(within(section).getByText(/haven't arrived yet/));
  assert.equal(within(section).queryByRole('combobox'), null);
  rendered.unmount();
  // Control: complete campaigns show their selector and no pending state.
  view();
  const complete = screen.getByRole('heading', { name: 'DSP campaigns by segment' }).closest('section')!;
  assert.ok(within(complete).getByRole('combobox', { name: 'Campaign' }));
  assert.equal(within(complete).queryByText(/haven't arrived yet/), null);
});
