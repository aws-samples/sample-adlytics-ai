// SPDX-License-Identifier: MIT-0
import assert from 'node:assert/strict';
import { StrictMode } from 'react';
import { act, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, test, vi } from 'vitest';

const { callApi } = vi.hoisted(() => ({ callApi: vi.fn() }));
let canManagePrompts = false;

vi.mock('../components/PresentationController', () => ({
  useApplicationState: () => ({ sessionId: 'conversation-fictional', canManagePrompts }),
}));
vi.mock('../lib/session', () => ({ callApi }));

import { VisibleTabsContext } from '../components/NextStepContext';
import { PROMPT_BODY_MAX_BYTES, PromptAdminPage } from '../components/PromptAdminPage';
import { visibleHubs, type ManifestTab } from '../lib/route-manifest';

const roster = {
  slots: [{
    slot_id: 'contribution',
    display_name: 'Contribution (fictional)',
    purpose: 'Explain a server-owned contribution.',
    output_class: 'perspective',
    silence_condition: 'Remain silent when no material finding exists.',
    active_version: 1,
    newest_version: 2,
    newest_calibration: 'failed',
    retired: false,
  }],
};

const history = {
  slot_id: 'contribution',
  active_version: 1,
  retired: false,
  versions: [{
    version: 2,
    prompt: 'A fictional proposed prompt.',
    model_route: 'quick',
    created_by: { kind: 'operator_email', email: 'operator@example.com' },
    created_at: '2026-08-29T10:30:00+00:00',
    calibration: 'failed',
    calibration_cases: [
      { kind: 'known-correct', outcome: 'passed' },
      { kind: 'contradiction', outcome: 'failed' },
      { kind: 'fluent-unsupported', outcome: 'passed' },
    ],
  }, {
    version: 1,
    prompt: 'A fictional active prompt.',
    model_route: 'quick',
    created_by: { kind: 'system', name: 'contract-seed' },
    created_at: '2026-08-28T10:30:00+00:00',
    calibration: 'passed',
    calibration_cases: [
      { kind: 'known-correct', outcome: 'passed' },
      { kind: 'contradiction', outcome: 'passed' },
      { kind: 'fluent-unsupported', outcome: 'passed' },
    ],
  }],
};

const activationHistory = {
  ...history,
  versions: [{
    ...history.versions[0]!,
    calibration: 'passed',
    calibration_cases: history.versions[1]!.calibration_cases,
  }, history.versions[1]!],
};

function json(value: object, status = 200): Response {
  return new Response(JSON.stringify(value), { status, headers: { 'content-type': 'application/json' } });
}

async function renderReady(historyResponse = history) {
  canManagePrompts = true;
  callApi.mockResolvedValueOnce(json(roster)).mockResolvedValueOnce(json(historyResponse));
  const result = render(<PromptAdminPage />);
  await screen.findByRole('heading', { name: 'Contribution (fictional)' });
  return result;
}

afterEach(() => {
  canManagePrompts = false;
  vi.clearAllMocks();
  vi.useRealTimers();
});

test('renders a plain-language unauthorized prompt administration state without registry calls', () => {
  render(<PromptAdminPage />);

  assert.ok(screen.getByRole('heading', { name: 'Not authorized' }));
  assert.ok(screen.getByText('You do not have permission to manage governed prompts.'));
  assert.equal(callApi.mock.calls.length, 0);
});

test('renders roster output class and version author and creation time without enum leakage', async () => {
  await renderReady();

  assert.ok(screen.getByText(/Output: Perspective/));
  assert.ok(screen.getByText('Version 2: calibration needs changes'));
  assert.ok(screen.getByText('Needs changes'));
  assert.ok(screen.getByText('1 of 3: Contradiction'));
  assert.ok(screen.getByText(/Created by operator@example.com on August 29, 2026 at 10:30 AM UTC\./));
  assert.ok(screen.getByText(/Created by the deployment seed on August 28, 2026 at 10:30 AM UTC\./));
  assert.ok(screen.getAllByText('Model: Quick model.').length > 0);
  assert.equal(screen.queryByText('quick'), null);
  // No wire enum reaches the page: neither the attribution kinds nor the system name.
  const text = document.body.textContent ?? '';
  for (const wire of ['not-recorded', 'not_recorded', 'contract-seed', 'operator_email', 'operator_unavailable']) {
    assert.equal(text.includes(wire), false, wire);
  }
  assert.deepEqual(callApi.mock.calls.slice(0, 2), [
    ['/api/prompt-registry/slots', 'POST', {}],
    ['/api/prompt-registry/versions', 'POST', { slot_id: 'contribution' }],
  ]);
});

test('offers a plain-language conflict recovery state when the active pointer moves', async () => {
  canManagePrompts = true;
  callApi.mockResolvedValueOnce(json({ error: 'conflict' }, 409));

  render(<PromptAdminPage />);

  assert.ok(await screen.findByRole('heading', { name: 'Prompt state changed' }));
  assert.ok(screen.getByRole('button', { name: 'Reload prompt state' }));
});

test('creates an immutable version through the canonical route and safe default routing hint', async () => {
  await renderReady();
  callApi
    .mockResolvedValueOnce(json({ slot_id: 'contribution', version: 3, outcome: 'pending' }, 202))
    .mockResolvedValueOnce(json(roster))
    .mockResolvedValueOnce(json(history));

  fireEvent.click(screen.getByRole('button', { name: 'New version' }));
  fireEvent.change(screen.getByLabelText('Prompt template'), { target: { value: 'A new fictional prompt.' } });
  fireEvent.click(screen.getByRole('button', { name: 'Create version and run calibration' }));

  await vi.waitFor(() => assert.equal(callApi.mock.calls.length, 5));
  assert.deepEqual(callApi.mock.calls[2], [
    '/api/prompt-registry/create-version',
    'POST',
    { slot_id: 'contribution', prompt_template: 'A new fictional prompt.' },
  ]);
});

test('the roster names each slot by its served display name and leads with the active version', async () => {
  await renderReady(activationHistory);

  assert.ok(screen.getByRole('region', { name: 'Insight prompts' }));
  assert.ok(screen.getByRole('rowheader', { name: 'Contribution (fictional)' }));
  assert.equal(screen.queryByText('contribution'), null);
  const versions = within(screen.getByRole('region', { name: 'Versions, active first' })).getAllByRole('rowheader');
  assert.match(versions[0]?.textContent ?? '', /^Version 1/);
  assert.ok(within(screen.getByRole('region', { name: 'Versions, active first' })).getByText('Active'));
  // Activate is secondary in the roster; the primary action lives only in the confirmation.
  assert.equal(screen.getByRole('button', { name: 'Activate version 2' }).className, 'btn');
  fireEvent.click(screen.getByRole('button', { name: 'Activate version 2' }));
  const dialog = screen.getByRole('dialog', { name: 'Activate version 2?' });
  assert.equal(within(dialog).getByRole('button', { name: 'Activate version 2' }).className, 'btn primary');
  fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
  assert.equal(screen.queryByRole('dialog'), null);
});

test('portfolio-acquisition is listed only while the New customers tab is in the session manifest (NC LLD §10)', async () => {
  // The served roster lists every contract, the New customers one among them.
  const acquisition = {
    ...roster.slots[0]!,
    slot_id: 'portfolio-acquisition',
    display_name: 'New-to-brand share mover (New customers)',
    active_version: null,
    newest_version: null,
    newest_calibration: null,
  };
  const served = { slots: [roster.slots[0]!, acquisition] };
  // The committed route manifest is the session's: its Customers hub lists New customers.
  const manifest = visibleHubs({}).flatMap((hub) => hub.tabs);
  assert.ok(manifest.some((tab) => tab.tab_id === 'new-customers'));
  const page = (tabs: readonly ManifestTab[]) => <VisibleTabsContext.Provider value={tabs}><PromptAdminPage /></VisibleTabsContext.Provider>;
  canManagePrompts = true;
  callApi.mockResolvedValueOnce(json(served)).mockResolvedValueOnce(json(history));
  const listed = render(page(manifest));
  await screen.findByRole('heading', { name: 'Contribution (fictional)' });
  assert.deepEqual(
    within(screen.getByRole('region', { name: 'Insight prompts' })).getAllByRole('rowheader').map((cell) => cell.textContent),
    ['Contribution (fictional)', 'New-to-brand share mover (New customers)'],
  );
  listed.unmount();
  // Paired control: the same roster with no New customers tab doesn't list its contract.
  callApi.mockResolvedValueOnce(json(served)).mockResolvedValueOnce(json(history));
  render(page(manifest.filter((tab) => tab.tab_id !== 'new-customers')));
  await screen.findByRole('heading', { name: 'Contribution (fictional)' });
  assert.deepEqual(
    within(screen.getByRole('region', { name: 'Insight prompts' })).getAllByRole('rowheader').map((cell) => cell.textContent),
    ['Contribution (fictional)'],
  );
});

test('all Reach prompt slots are listed only while the Reach tab is in the session manifest', async () => {
  const reachSlots = [
    ['reach-overlap', 'Reach overlap'],
    ['full-funnel-association', 'Full funnel association'],
    ['frequency-saturation', 'Frequency saturation'],
  ].map(([slot_id, display_name]) => ({
    ...roster.slots[0]!,
    slot_id,
    display_name,
    active_version: null,
    newest_version: null,
    newest_calibration: null,
  }));
  const served = { slots: [roster.slots[0]!, ...reachSlots] };
  const manifest = visibleHubs({}).flatMap((hub) => hub.tabs);
  assert.ok(manifest.some((tab) => tab.tab_id === 'reach'));
  const page = (tabs: readonly ManifestTab[]) => <VisibleTabsContext.Provider value={tabs}><PromptAdminPage /></VisibleTabsContext.Provider>;
  canManagePrompts = true;

  callApi.mockResolvedValueOnce(json(served)).mockResolvedValueOnce(json(history));
  const listed = render(page(manifest));
  await screen.findByRole('heading', { name: 'Contribution (fictional)' });
  assert.deepEqual(
    within(screen.getByRole('region', { name: 'Insight prompts' })).getAllByRole('rowheader').map((cell) => cell.textContent),
    ['Contribution (fictional)', 'Reach overlap', 'Full funnel association', 'Frequency saturation'],
  );
  listed.unmount();

  callApi.mockResolvedValueOnce(json(served)).mockResolvedValueOnce(json(history));
  render(page(manifest.filter((tab) => tab.tab_id !== 'reach')));
  await screen.findByRole('heading', { name: 'Contribution (fictional)' });
  assert.deepEqual(
    within(screen.getByRole('region', { name: 'Insight prompts' })).getAllByRole('rowheader').map((cell) => cell.textContent),
    ['Contribution (fictional)'],
  );
});

test('the deterministic Audiences slot is paired to its routed tab only', async () => {
  const audienceSlot = { ...roster.slots[0]!, slot_id: 'segment-over-index', display_name: 'Segment over-index' };
  const served = { slots: [roster.slots[0]!, audienceSlot] };
  const manifest = visibleHubs({}).flatMap((hub) => hub.tabs);
  assert.ok(manifest.some((tab) => tab.tab_id === 'audiences'));
  const page = (tabs: readonly ManifestTab[]) => <VisibleTabsContext.Provider value={tabs}><PromptAdminPage /></VisibleTabsContext.Provider>;
  canManagePrompts = true;

  callApi.mockResolvedValueOnce(json(served)).mockResolvedValueOnce(json(history));
  const listed = render(page(manifest));
  await screen.findByRole('heading', { name: 'Contribution (fictional)' });
  assert.deepEqual(within(screen.getByRole('region', { name: 'Insight prompts' })).getAllByRole('rowheader').map((cell) => cell.textContent), ['Contribution (fictional)', 'Segment over-index']);
  listed.unmount();

  callApi.mockResolvedValueOnce(json(served)).mockResolvedValueOnce(json(history));
  render(page(manifest.filter((tab) => tab.tab_id !== 'audiences')));
  await screen.findByRole('heading', { name: 'Contribution (fictional)' });
  assert.deepEqual(within(screen.getByRole('region', { name: 'Insight prompts' })).getAllByRole('rowheader').map((cell) => cell.textContent), ['Contribution (fictional)']);
});

test('View prompt shows that version in the wrapped viewer', async () => {
  await renderReady(activationHistory);

  assert.ok(screen.getByRole('region', { name: 'Version 1 prompt text' }));
  fireEvent.click(screen.getByRole('button', { name: 'View prompt: version 2' }));
  assert.equal(screen.getByRole('region', { name: 'Version 2 prompt text' }).textContent, 'A fictional proposed prompt.');
});

test('a roster without served display names is treated as an unavailable registry', async () => {
  canManagePrompts = true;
  const withoutName = Object.fromEntries(Object.entries(roster.slots[0]!).filter(([key]) => key !== 'display_name'));
  callApi.mockResolvedValueOnce(json({ slots: [withoutName] }));
  render(<PromptAdminPage />);
  assert.ok(await screen.findByRole('heading', { name: 'Prompt administration is unavailable' }));
});

test('activates a passed version through the canonical route after cache-consequence confirmation', async () => {
  await renderReady(activationHistory);
  fireEvent.click(screen.getByRole('button', { name: 'Activate version 2' }));
  assert.ok(screen.getByText(/regenerates only this slot’s cached insights/));
  callApi
    .mockResolvedValueOnce(json({ event: {} }))
    .mockResolvedValueOnce(json(roster))
    .mockResolvedValueOnce(json(activationHistory));

  fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Activate version 2' }));

  await vi.waitFor(() => assert.equal(callApi.mock.calls.length, 5));
  assert.deepEqual(callApi.mock.calls[2], [
    '/api/prompt-registry/activate',
    'POST',
    { slot_id: 'contribution', version: 2, expected_active_version: 1 },
  ]);
});

test('retires the active slot through the canonical route after confirmation', async () => {
  await renderReady();
  fireEvent.click(screen.getByRole('button', { name: 'Retire slot' }));
  callApi
    .mockResolvedValueOnce(json({ event: {} }))
    .mockResolvedValueOnce(json(roster))
    .mockResolvedValueOnce(json(history));

  fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Retire slot' }));

  await vi.waitFor(() => assert.equal(callApi.mock.calls.length, 5));
  assert.deepEqual(callApi.mock.calls[2], [
    '/api/prompt-registry/retire',
    'POST',
    { slot_id: 'contribution', expected_active_version: 1 },
  ]);
});


test('renders a plain-language calibration recovery state without exposing the API error token', async () => {
  await renderReady(activationHistory);
  fireEvent.click(screen.getByRole('button', { name: 'Activate version 2' }));
  callApi.mockResolvedValueOnce(json({ error: 'calibration_required' }, 409));

  fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Activate version 2' }));

  assert.ok(await screen.findByRole('heading', { name: 'Activation is not available' }));
  assert.ok(screen.getByText(/must complete and pass calibration/));
  assert.equal(screen.queryByText('calibration_required'), null);
});


test('polls the version projection after queued creation and stops on a terminal result', async () => {
  await renderReady();
  vi.useFakeTimers();
  const pendingHistory = {
    ...history,
    versions: [{
      ...history.versions[0]!,
      version: 3,
      calibration: 'pending' as const,
      calibration_cases: [],
    }, ...history.versions],
  };
  const passedHistory = {
    ...pendingHistory,
    versions: [{
      ...pendingHistory.versions[0]!,
      calibration: 'passed' as const,
      calibration_cases: history.versions[1]!.calibration_cases,
    }, ...history.versions],
  };
  callApi
    .mockResolvedValueOnce(json({ slot_id: 'contribution', version: 3, outcome: 'pending' }, 202))
    .mockResolvedValueOnce(json(roster))
    .mockResolvedValueOnce(json(pendingHistory));

  fireEvent.click(screen.getByRole('button', { name: 'New version' }));
  fireEvent.change(screen.getByLabelText('Prompt template'), {
    target: { value: 'A queued fictional prompt.' },
  });
  fireEvent.click(screen.getByRole('button', { name: 'Create version and run calibration' }));
  await vi.waitFor(() => assert.equal(callApi.mock.calls.length, 5));

  callApi.mockResolvedValueOnce(json(passedHistory)).mockResolvedValueOnce(json(roster));
  await act(async () => { await vi.advanceTimersByTimeAsync(2_000); });
  await vi.waitFor(() => assert.equal(callApi.mock.calls.length, 7));

  assert.deepEqual(callApi.mock.calls[5], [
    '/api/prompt-registry/versions',
    'POST',
    { slot_id: 'contribution' },
  ]);
  assert.equal(
    callApi.mock.calls.some(([path]) => path === '/api/prompt-registry/extensions/calibrate'),
    false,
  );
  await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
  assert.equal(callApi.mock.calls.length, 7);
});


test('stops queued calibration polling after its finite increasing-delay budget', async () => {
  await renderReady();
  vi.useFakeTimers();
  const pendingHistory = {
    ...history,
    versions: [{
      ...history.versions[0]!,
      version: 3,
      calibration: 'pending' as const,
      calibration_cases: [],
    }, ...history.versions],
  };
  callApi
    .mockResolvedValueOnce(json({ slot_id: 'contribution', version: 3, outcome: 'pending' }, 202))
    .mockResolvedValueOnce(json(roster))
    .mockResolvedValueOnce(json(pendingHistory))
    .mockImplementation(() => Promise.resolve(json(pendingHistory)));

  fireEvent.click(screen.getByRole('button', { name: 'New version' }));
  fireEvent.change(screen.getByLabelText('Prompt template'), {
    target: { value: 'A bounded-poll fictional prompt.' },
  });
  fireEvent.click(screen.getByRole('button', { name: 'Create version and run calibration' }));
  await vi.waitFor(() => assert.equal(callApi.mock.calls.length, 5));

  for (let attempt = 0; attempt < 26; attempt += 1) {
    await act(async () => { await vi.runOnlyPendingTimersAsync(); });
  }
  assert.equal(callApi.mock.calls.length, 31);
  await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
  assert.equal(callApi.mock.calls.length, 31);
});


test('ignores a prior slot history response that resolves after a newer selection', async () => {
  canManagePrompts = true;
  const twoSlots = {
    slots: [
      ...roster.slots,
      { ...roster.slots[0]!, slot_id: 'exposure', display_name: 'Exposure (fictional)', purpose: 'Explain fictional exposure.' },
    ],
  };
  let resolveExposure: ((response: Response) => void) | undefined;
  const deferredExposure = new Promise<Response>((resolve) => { resolveExposure = resolve; });
  const exposureHistory = {
    ...history,
    slot_id: 'exposure',
    versions: history.versions.map((version) => ({
      ...version,
      prompt: 'A stale exposure prompt.',
    })),
  };
  callApi.mockResolvedValueOnce(json(twoSlots)).mockResolvedValueOnce(json(history));
  render(<PromptAdminPage />);
  await screen.findByRole('heading', { name: 'Contribution (fictional)' });
  callApi.mockReturnValueOnce(deferredExposure).mockResolvedValueOnce(json(history));

  fireEvent.click(screen.getByRole('button', { name: 'Exposure (fictional)' }));
  fireEvent.click(screen.getByRole('button', { name: 'Contribution (fictional)' }));
  await screen.findByText('A fictional active prompt.');
  resolveExposure?.(json(exposureHistory));
  await Promise.resolve();

  assert.equal(screen.queryByText('A stale exposure prompt.'), null);
  assert.ok(screen.getByText('A fictional active prompt.'));
});


test('ignores a deferred create completion after the operator selects another slot', async () => {
  canManagePrompts = true;
  const twoSlots = {
    slots: [
      ...roster.slots,
      { ...roster.slots[0]!, slot_id: 'exposure', display_name: 'Exposure (fictional)', purpose: 'Explain fictional exposure.' },
    ],
  };
  const exposureHistory = {
    ...history,
    slot_id: 'exposure',
    versions: history.versions.map((version) => ({
      ...version,
      prompt: 'A current exposure prompt.',
    })),
  };
  let resolveCreate: ((response: Response) => void) | undefined;
  const deferredCreate = new Promise<Response>((resolve) => { resolveCreate = resolve; });
  callApi.mockResolvedValueOnce(json(twoSlots)).mockResolvedValueOnce(json(history));
  render(<PromptAdminPage />);
  await screen.findByRole('heading', { name: 'Contribution (fictional)' });
  callApi.mockReturnValueOnce(deferredCreate).mockResolvedValueOnce(json(exposureHistory));

  fireEvent.click(screen.getByRole('button', { name: 'New version' }));
  fireEvent.change(screen.getByLabelText('Prompt template'), {
    target: { value: 'A deferred fictional prompt.' },
  });
  fireEvent.click(screen.getByRole('button', { name: 'Create version and run calibration' }));
  await vi.waitFor(() => assert.equal(callApi.mock.calls.length, 3));
  fireEvent.click(screen.getByRole('button', { name: 'Exposure (fictional)' }));
  await screen.findByText('A current exposure prompt.');
  resolveCreate?.(json({ slot_id: 'contribution', version: 3, outcome: 'pending' }, 202));
  await Promise.resolve();

  assert.ok(screen.getByText('A current exposure prompt.'));
  assert.equal(callApi.mock.calls.length, 4);
});


test('does not continue a deferred activation after the page unmounts', async () => {
  const page = await renderReady(activationHistory);
  let resolveActivation: ((response: Response) => void) | undefined;
  const deferredActivation = new Promise<Response>((resolve) => { resolveActivation = resolve; });
  callApi.mockReturnValueOnce(deferredActivation);
  fireEvent.click(screen.getByRole('button', { name: 'Activate version 2' }));
  fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Activate version 2' }));
  await vi.waitFor(() => assert.equal(callApi.mock.calls.length, 3));

  page.unmount();
  resolveActivation?.(json({ event: {} }));
  await Promise.resolve();

  assert.equal(callApi.mock.calls.length, 3);
});


test('applies lifecycle completions and clears pending state after Strict Mode effect replay', async () => {
  canManagePrompts = true;
  callApi.mockImplementation((path: string) => {
    if (path.endsWith('/slots')) return Promise.resolve(json(roster));
    if (path.endsWith('/versions')) return Promise.resolve(json(activationHistory));
    if (path.endsWith('/activate')) return Promise.resolve(json({ event: {} }));
    throw new Error(`unexpected route ${path}`);
  });
  render(<StrictMode><PromptAdminPage /></StrictMode>);
  await screen.findByRole('heading', { name: 'Contribution (fictional)' });

  fireEvent.click(screen.getByRole('button', { name: 'Activate version 2' }));
  fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Activate version 2' }));

  await vi.waitFor(() => {
    const activate = screen.getByRole('button', { name: 'Activate version 2' });
    assert.equal(activate.hasAttribute('disabled'), false);
  });
  assert.ok(callApi.mock.calls.some(([path]) => path === '/api/prompt-registry/activate'));
});

function withVersion(version: Record<string, unknown>) {
  return { ...history, versions: [{ ...history.versions[0]!, ...version }] };
}

test.each([
  [
    { created_by: { kind: 'operator_not_found' } },
    'Created on August 29, 2026 at 10:30 AM UTC by an operator whose account could not be found.',
  ],
  [
    { created_by: { kind: 'operator_unavailable' } },
    'Created on August 29, 2026 at 10:30 AM UTC by an operator. Their email address could not be loaded right now.',
  ],
  [
    { created_by: { kind: 'not_recorded' }, created_at: null },
    'Created before authorship was recorded.',
  ],
])('renders the authorship sentence for each attribution kind', async (version, sentence) => {
  await renderReady(withVersion(version) as typeof history);
  assert.ok(screen.getByText((content) => content.includes(sentence)));
  assert.equal((document.body.textContent ?? '').includes('not_recorded'), false);
});

test.each([
  [{ created_by: { kind: 'fictional-kind' } }],
  [{ created_by: { kind: 'operator_unavailable', email: 'operator@example.com' } }],
  [{ created_by: { kind: 'system', name: 'fictional-seed' } }],
  [{ created_by: { kind: 'operator_email' } }],
  [{ created_by: { kind: 'not_recorded' }, created_at: '2026-08-29T10:30:00+00:00' }],
  [{ created_by: { kind: 'operator_not_found' }, created_at: null }],
  [{ created_by: { kind: 'operator_not_found' }, created_at: 'not-a-time' }],
  // Date can parse these, but none is the server's UTC ISO 8601 instant.
  [{ created_by: { kind: 'operator_not_found' }, created_at: '1' }],
  [{ created_by: { kind: 'operator_not_found' }, created_at: '2026-08-29' }],
  [{ created_by: { kind: 'operator_not_found' }, created_at: '2026-08-29T10:30:00Z' }],
  [{ author: 'operator-fictional', created_by: undefined }],
])('treats a malformed version attribution as an unavailable registry', async (version) => {
  canManagePrompts = true;
  callApi.mockResolvedValueOnce(json(roster)).mockResolvedValueOnce(json(withVersion(version)));
  render(<PromptAdminPage />);
  assert.ok(await screen.findByRole('heading', { name: 'Prompt administration is unavailable' }));
});

// ADR-078 path-scoped body-size exclusion: the page checks the request body's UTF-8 size before it
// submits, so a prompt the edge or the handler would refuse is never sent.
const ENVELOPE_BYTES = new TextEncoder().encode(
  JSON.stringify({ slot_id: 'contribution', prompt_template: '' }),
).length;

async function openEditor(promptText: string) {
  await renderReady();
  fireEvent.click(screen.getByRole('button', { name: 'New version' }));
  fireEvent.change(screen.getByLabelText('Prompt template'), { target: { value: promptText } });
}

test('allows Save at exactly 32,768 request bytes', async () => {
  assert.equal(PROMPT_BODY_MAX_BYTES, 32_768);
  await openEditor('x'.repeat(PROMPT_BODY_MAX_BYTES - ENVELOPE_BYTES));
  const save = screen.getByRole('button', { name: 'Create version and run calibration' }) as HTMLButtonElement;
  assert.equal(save.disabled, false);
  assert.equal(screen.getByRole('status').textContent, '');
  assert.equal(screen.getByLabelText('Prompt template').getAttribute('aria-describedby'), null);
});

test('disables Save one byte over the limit, names the excess, and links it to the editor', async () => {
  await openEditor('x'.repeat(PROMPT_BODY_MAX_BYTES - ENVELOPE_BYTES + 1));
  const save = screen.getByRole('button', { name: 'Create version and run calibration' }) as HTMLButtonElement;
  assert.equal(save.disabled, true);
  const status = screen.getByRole('status');
  assert.equal(
    status.textContent,
    'This prompt is too long to save. Shorten it by 1 byte (about 1 character of plain text).',
  );
  assert.equal(screen.getByLabelText('Prompt template').getAttribute('aria-describedby'), status.id);
  fireEvent.click(save);
  assert.equal(callApi.mock.calls.length, 2);
});

test('measures a multibyte prompt in UTF-8 bytes, not characters', async () => {
  // 16,384 two-byte characters fit the limit in characters but not in bytes.
  const promptText = 'é'.repeat(16_384);
  assert.ok(promptText.length + ENVELOPE_BYTES < PROMPT_BODY_MAX_BYTES);
  await openEditor(promptText);
  const save = screen.getByRole('button', { name: 'Create version and run calibration' }) as HTMLButtonElement;
  assert.equal(save.disabled, true);
  const over = 2 * 16_384 + ENVELOPE_BYTES - PROMPT_BODY_MAX_BYTES;
  assert.equal(
    screen.getByRole('status').textContent,
    `This prompt is too long to save. Shorten it by ${over} bytes (about ${over} characters of plain text).`,
  );
});

test('maps a 413 to the too-long message and keeps the editor content', async () => {
  await openEditor('A fictional prompt the handler refuses.');
  callApi.mockResolvedValueOnce(json({ error: 'request_too_large' }, 413));
  fireEvent.click(screen.getByRole('button', { name: 'Create version and run calibration' }));

  await vi.waitFor(() => assert.match(screen.getByRole('status').textContent ?? '', /^This prompt is too long to save\./));
  assert.equal((screen.getByLabelText('Prompt template') as HTMLTextAreaElement).value, 'A fictional prompt the handler refuses.');
  assert.equal(screen.getByLabelText('Prompt template').getAttribute('aria-describedby'), 'prompt-request-status');
  assert.equal(callApi.mock.calls.length, 3);
  assert.equal(screen.queryByRole('heading', { name: 'Prompt administration is unavailable' }), null);
});
