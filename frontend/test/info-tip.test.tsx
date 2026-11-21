// SPDX-License-Identifier: MIT-0
/**
 * InfoTip, the demand forecast page's info pop-up (design "Info pop-up behaviour"). A pop-up that
 * opens on hover breaks WCAG 1.4.13; one that moves focus on Escape strands a keyboard user; two
 * open at once cover each other's terms.
 */
import assert from 'node:assert/strict';
import { fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { test } from 'vitest';
import { InfoTip } from '@/components/InfoTip';

function panelOf(button: HTMLElement): HTMLElement {
  const panel = document.getElementById(button.getAttribute('aria-controls') ?? '');
  assert.ok(panel);
  return panel;
}

test('Enter and Space toggle the panel and set aria-expanded; Escape closes it and keeps focus', async () => {
  render(<p>Likely total <InfoTip title="Likely total">The likely weekly forecasts, added up.</InfoTip></p>);
  const button = screen.getByRole('button', { name: 'About likely total' });
  assert.equal(button.getAttribute('aria-expanded'), 'false');
  assert.equal(panelOf(button).hidden, true);
  button.focus();
  await userEvent.keyboard('{Enter}');
  assert.equal(button.getAttribute('aria-expanded'), 'true');
  assert.equal(panelOf(button).hidden, false);
  await userEvent.keyboard(' ');
  assert.equal(button.getAttribute('aria-expanded'), 'false');
  await userEvent.keyboard(' ');
  assert.equal(button.getAttribute('aria-expanded'), 'true');
  await userEvent.keyboard('{Escape}');
  assert.equal(button.getAttribute('aria-expanded'), 'false');
  assert.equal(document.activeElement, button);
});

test('hover alone never opens it', async () => {
  render(<InfoTip title="Rank">Ranked by likely units ordered.</InfoTip>);
  const button = screen.getByRole('button', { name: 'About rank' });
  await userEvent.hover(button);
  fireEvent.mouseEnter(button);
  assert.equal(button.getAttribute('aria-expanded'), 'false');
});

test('opening one closes the other, and a press outside closes it', async () => {
  render(<div>
    <InfoTip title="Rank">A</InfoTip>
    <InfoTip title="Likely total">B</InfoTip>
    <span>outside</span>
  </div>);
  const rank = screen.getByRole('button', { name: 'About rank' });
  const total = screen.getByRole('button', { name: 'About likely total' });
  await userEvent.click(rank);
  assert.equal(rank.getAttribute('aria-expanded'), 'true');
  await userEvent.click(total);
  assert.equal(rank.getAttribute('aria-expanded'), 'false');
  assert.equal(total.getAttribute('aria-expanded'), 'true');
  assert.notEqual(rank.getAttribute('aria-controls'), total.getAttribute('aria-controls'));
  fireEvent.pointerDown(screen.getByText('outside'));
  assert.equal(total.getAttribute('aria-expanded'), 'false');
});

test('a given label names the button as written', () => {
  render(<InfoTip title="Amazon wholesale cost" label="About Amazon wholesale cost">C</InfoTip>);
  assert.ok(screen.getByRole('button', { name: 'About Amazon wholesale cost' }));
});
