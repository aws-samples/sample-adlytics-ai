// SPDX-License-Identifier: MIT-0
/**
 * The prompt administration page with the real `callApi` (ADR-007 amendment, 2026-10-05): an edge
 * block page (HTML 403) on create-version is shown as a refusal, with no session refresh, no login
 * and the editor content kept. `prompt-admin-page.test.tsx` mocks the session module for the whole
 * file, so this file stubs only `globalThis.fetch`.
 */
import assert from 'node:assert/strict';
import { fireEvent, render, screen } from '@testing-library/react';
import { afterEach, test, vi } from 'vitest';

vi.mock('../components/PresentationController', () => ({
  useApplicationState: () => ({ sessionId: 'conversation-fictional', canManagePrompts: true }),
}));

import { PromptAdminPage } from '../components/PromptAdminPage';

const originalFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = originalFetch;
});

const roster = {
  slots: [{
    slot_id: 'contribution',
    display_name: 'Contribution (fictional)',
    purpose: 'Explain a server-owned contribution.',
    output_class: 'perspective',
    silence_condition: 'Remain silent when no material finding exists.',
    active_version: 1,
    newest_version: 1,
    newest_calibration: 'passed',
    retired: false,
  }],
};
const history = {
  slot_id: 'contribution',
  active_version: 1,
  retired: false,
  versions: [{
    version: 1,
    prompt: 'A fictional active prompt.',
    model_route: 'quick',
    created_by: { kind: 'system', name: 'contract-seed' },
    created_at: '2026-08-28T10:30:00+00:00',
    calibration: 'passed',
    calibration_cases: [{ kind: 'known-correct', outcome: 'passed' }],
  }],
};

function json(value: object): Response {
  return new Response(JSON.stringify(value), { status: 200, headers: { 'content-type': 'application/json' } });
}

test('shows the blocked message for an edge block on create-version and keeps the prompt', async () => {
  const paths: string[] = [];
  globalThis.fetch = (async (input: RequestInfo | URL) => {
    const path = String(input);
    paths.push(path);
    if (path === '/api/prompt-registry/slots') return json(roster);
    if (path === '/api/prompt-registry/versions') return json(history);
    if (path === '/api/prompt-registry/create-version') {
      return new Response('<html><body>Request blocked.</body></html>', {
        status: 403,
        headers: { 'content-type': 'text/html' },
      });
    }
    throw new Error(`unexpected request to ${path}`);
  }) as typeof fetch;
  const before = window.location.href;

  render(<PromptAdminPage />);
  await screen.findByRole('heading', { name: 'Contribution (fictional)' });
  fireEvent.click(screen.getByRole('button', { name: 'New version' }));
  fireEvent.change(screen.getByLabelText('Prompt template'), { target: { value: 'A typed fictional prompt.' } });
  fireEvent.click(screen.getByRole('button', { name: 'Create version and run calibration' }));

  await vi.waitFor(() => assert.equal(
    screen.getByRole('status').textContent,
    'This request was blocked before it reached the service. If the prompt is long, shorten it and try again.',
  ));
  assert.equal((screen.getByLabelText('Prompt template') as HTMLTextAreaElement).value, 'A typed fictional prompt.');
  assert.equal(paths.filter((path) => path === '/api/prompt-registry/create-version').length, 1);
  assert.equal(paths.filter((path) => path === '/api/auth/refresh').length, 0);
  assert.equal(paths.filter((path) => path === '/api/auth/login').length, 0);
  assert.equal(window.location.href, before);
});
