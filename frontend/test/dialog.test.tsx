// SPDX-License-Identifier: MIT-0
import assert from 'node:assert/strict';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useRef, useState } from 'react';
import { test } from 'vitest';

import { APP_ROOT_ID, Dialog, PAGE_HEADING_ID } from '@/components/Dialog';

/**
 * A page with two dialogs whose owners sit outside each other, as page dialogs do: the
 * insight dialog's "See why" opens the coverage sheet, which replaces it (ADR-118 decision 6).
 */
function Page() {
  const [open, setOpen] = useState<'insight' | 'coverage' | null>(null);
  const opener = useRef<HTMLButtonElement>(null);
  return <>
    <div id={APP_ROOT_ID}>
      <h1 id={PAGE_HEADING_ID} tabIndex={-1}>Fictional tab</h1>
      <button ref={opener} type="button" onClick={() => setOpen('insight')}>Open insight</button>
      <button type="button">After</button>
    </div>
    {open === 'insight' ? <Dialog
      labelledBy="insight-title"
      onClose={() => setOpen((current) => (current === 'insight' ? null : current))}
      opener={opener}
      header={<h2 id="insight-title">Fictional insight</h2>}
    >
      <button type="button" onClick={() => setOpen('coverage')}>See why</button>
    </Dialog> : null}
    {open === 'coverage' ? <Dialog
      labelledBy="coverage-title"
      onClose={() => setOpen((current) => (current === 'coverage' ? null : current))}
      header={<h2 id="coverage-title">Coverage for this window</h2>}
    >
      <p>Fictional coverage.</p>
    </Dialog> : null}
  </>;
}

test('a dialog takes focus on its Close, makes the app inert, and Escape returns focus to its opener', async () => {
  const user = userEvent.setup();
  render(<Page />);
  const opener = screen.getByRole('button', { name: 'Open insight' });
  await user.click(opener);
  const dialog = screen.getByRole('dialog', { name: 'Fictional insight' });
  assert.equal(dialog.getAttribute('aria-modal'), 'true');
  assert.equal(document.activeElement, within(dialog).getByRole('button', { name: 'Close' }));
  assert.equal(document.getElementById(APP_ROOT_ID)?.inert, true);
  // The header is sticky and holds Close; the body follows it.
  assert.ok(dialog.querySelector('.dialog-head button[aria-label="Close"]'));
  await user.keyboard('{Escape}');
  assert.equal(screen.queryByRole('dialog'), null);
  assert.equal(document.getElementById(APP_ROOT_ID)?.inert, false);
  assert.equal(document.activeElement, opener);
});

test('Tab stays inside the open dialog', async () => {
  const user = userEvent.setup();
  render(<Page />);
  await user.click(screen.getByRole('button', { name: 'Open insight' }));
  const dialog = screen.getByRole('dialog', { name: 'Fictional insight' });
  await user.tab();
  assert.equal(document.activeElement, within(dialog).getByRole('button', { name: 'See why' }));
  await user.tab();
  assert.equal(document.activeElement, within(dialog).getByRole('button', { name: 'Close' }));
});

test('a dialog opened from another replaces it, and closing returns focus to the first opener', async () => {
  const user = userEvent.setup();
  render(<Page />);
  const opener = screen.getByRole('button', { name: 'Open insight' });
  await user.click(opener);
  await user.click(screen.getByRole('button', { name: 'See why' }));
  // One modal at a time.
  assert.deepEqual(screen.getAllByRole('dialog').map((item) => item.getAttribute('aria-labelledby')), ['coverage-title']);
  assert.equal(document.getElementById(APP_ROOT_ID)?.inert, true);
  await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Close' }));
  assert.equal(screen.queryByRole('dialog'), null);
  assert.equal(document.getElementById(APP_ROOT_ID)?.inert, false);
  assert.equal(document.activeElement, opener);
});

test('when the opener is gone, closing focuses the page h1', async () => {
  function Gone() {
    const [open, setOpen] = useState(false);
    return <>
      <div id={APP_ROOT_ID}>
        <h1 id={PAGE_HEADING_ID} tabIndex={-1}>Fictional tab</h1>
        {open ? null : <button type="button" onClick={() => setOpen(true)}>Open</button>}
      </div>
      {open ? <Dialog label="Fictional" onClose={() => setOpen(false)} header={<h2>Fictional</h2>}><p>Body</p></Dialog> : null}
    </>;
  }
  const user = userEvent.setup();
  render(<Gone />);
  await user.click(screen.getByRole('button', { name: 'Open' }));
  await user.keyboard('{Escape}');
  assert.equal(document.activeElement, screen.getByRole('heading', { level: 1 }));
});
