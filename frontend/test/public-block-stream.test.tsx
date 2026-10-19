// SPDX-License-Identifier: MIT-0
import assert from 'node:assert/strict';
import axeCore from 'axe-core';
import { render, screen, within } from '@testing-library/react';
import { test } from 'vitest';

import { PublicBlockStream } from '@/components/assistant/PublicBlockStream';

const blocks = [
  { kind: 'preface', text: 'I will use governed information.' },
  { kind: 'paragraph', text: 'Sales improved over the selected period.' },
  { kind: 'grouped_list', heading: 'Findings', items: ['Sales increased', 'Coverage is complete'] },
  { kind: 'governed_table', caption: 'Campaign results', columns: ['Campaign', 'Sales'], rows: [['Spring', '10'], ['Summer', '12']] },
  { kind: 'notice', notice_kind: 'notice', title: 'Method', text: 'Values use the selected reporting window.' },
  { kind: 'notice', notice_kind: 'limitation', title: 'Limitation', text: 'One comparison source is partial.' },
] as const;

test('renders only safe native public blocks without a visible generic Answer heading', async () => {
  const view = render(<PublicBlockStream blocks={blocks} />);

  assert.ok(screen.getByRole('article', { name: 'Assistant response' }));
  assert.equal(screen.queryByRole('heading', { name: 'Answer' }), null);
  assert.ok(screen.getByText('Sales improved over the selected period.'));
  assert.ok(screen.getByRole('heading', { level: 3, name: 'Findings' }));
  assert.deepEqual(screen.getAllByRole('listitem').map((item) => item.textContent), ['Sales increased', 'Coverage is complete']);

  const table = screen.getByRole('table', { name: 'Campaign results' });
  assert.equal(within(table).getByRole('columnheader', { name: 'Campaign' }).getAttribute('scope'), 'col');
  assert.equal(within(table).getByRole('rowheader', { name: 'Spring' }).getAttribute('scope'), 'row');
  assert.equal(table.closest('[role="region"]')?.getAttribute('tabindex'), '0');
  assert.ok(screen.getByRole('region', { name: 'Method' }));
  assert.ok(screen.getByRole('region', { name: 'Limitation' }));
  assert.equal(view.container.querySelectorAll('a, button, details, script').length, 0);

  try {
    const results = await axeCore.run(view.container, { rules: { 'color-contrast': { enabled: false } } });
    assert.deepEqual(results.violations, []);
  } finally {
    view.unmount();
    axeCore.reset();
  }
});
