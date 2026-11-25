// SPDX-License-Identifier: MIT-0
/**
 * InfoTip, the demand forecast page's info pop-up (design "Info pop-up behaviour"). A pop-up that
 * opens on hover breaks WCAG 1.4.13; one that moves focus on Escape strands a keyboard user; two
 * open at once cover each other's terms.
 */
import assert from 'node:assert/strict';
import { act, fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { test, vi } from 'vitest';
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
  assert.ok(document.activeElement === button);
});

test('Escape from a control inside the open panel closes it and puts focus on the button', async () => {
  render(<p>Status <InfoTip title="About this forecast" label="About this forecast">
    The update schedule can&apos;t be loaded right now. <button type="button">Try again</button>
  </InfoTip></p>);
  const button = screen.getByRole('button', { name: 'About this forecast' });
  await userEvent.click(button);
  const retry = screen.getByRole('button', { name: 'Try again' });
  retry.focus();
  await userEvent.keyboard('{Escape}');
  assert.equal(button.getAttribute('aria-expanded'), 'false');
  assert.equal(panelOf(button).hidden, true);
  assert.ok(document.activeElement === button);
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
/** A DOMRect with only the horizontal edges a test sets. */
function edges(left: number, right: number): DOMRect {
  return { bottom: 0, height: 0, left, right, top: 0, width: right - left, x: left, y: 0, toJSON: () => ({}) } as DOMRect;
}
test('an open panel shifts inside the page column, so the column never scrolls sideways', async () => {
  render(<div className="main">
    <InfoTip title="Near the right edge">A</InfoTip>
    <InfoTip title="Near the left edge" className="end">B</InfoTip>
    <InfoTip title="Inside">C</InfoTip>
  </div>);
  const column = document.querySelector('.main');
  assert.ok(column instanceof HTMLElement);
  column.getBoundingClientRect = () => edges(0, 400);
  const place = (name: string, left: number, right: number) => {
    const button = screen.getByRole('button', { name });
    panelOf(button).getBoundingClientRect = () => edges(left, right);
    return button;
  };
  const right = place('About near the right edge', 300, 620);
  await userEvent.click(right);
  assert.equal(panelOf(right).style.transform, 'translateX(-228px)');
  const left = place('About near the left edge', -50, 270);
  await userEvent.click(left);
  assert.equal(panelOf(left).style.transform, 'translateX(58px)');
  const inside = place('About inside', 40, 360);
  await userEvent.click(inside);
  assert.equal(panelOf(inside).style.transform, '');
});
test('an open panel whose own content changes its size is placed again (the freshness pop-up)', async () => {
  // jsdom has no ResizeObserver: this one reports a size change only to observers of that node.
  const observers: { callback: ResizeObserverCallback; targets: Element[] }[] = [];
  class FakeResizeObserver {
    private readonly entry: { callback: ResizeObserverCallback; targets: Element[] };
    constructor(callback: ResizeObserverCallback) {
      this.entry = { callback, targets: [] };
      observers.push(this.entry);
    }
    observe(target: Element) { this.entry.targets.push(target); }
    unobserve() { /* not used */ }
    disconnect() { this.entry.targets = []; }
  }
  const resized = (target: Element) => {
    for (const { callback, targets } of observers) if (targets.includes(target)) callback([], {} as ResizeObserver);
  };
  vi.stubGlobal('ResizeObserver', FakeResizeObserver);
  try {
    render(<div className="main"><InfoTip title="About this forecast" label="About this forecast">Made Nov 8.</InfoTip></div>);
    const column = document.querySelector('.main');
    assert.ok(column instanceof HTMLElement);
    column.getBoundingClientRect = () => edges(0, 400);
    const button = screen.getByRole('button', { name: 'About this forecast' });
    const panel = panelOf(button);
    panel.getBoundingClientRect = () => edges(200, 380);
    await userEvent.click(button);
    assert.equal(panel.style.transform, '');
    // The schedule lines land and the panel grows past the column's right edge.
    panel.getBoundingClientRect = () => edges(200, 520);
    act(() => resized(panel));
    assert.equal(panel.style.transform, 'translateX(-128px)');
  } finally {
    vi.unstubAllGlobals();
  }
});
test('an open panel is placed again when a render moves its button without resizing anything', async () => {
  const view = (page: number) => <div className="main"><InfoTip title="Rank">Page {page}</InfoTip></div>;
  const { rerender } = render(view(1));
  const column = document.querySelector('.main');
  assert.ok(column instanceof HTMLElement);
  column.getBoundingClientRect = () => edges(0, 400);
  const button = screen.getByRole('button', { name: 'About rank' });
  const panel = panelOf(button);
  panel.getBoundingClientRect = () => edges(40, 360);
  await userEvent.click(button);
  assert.equal(panel.style.transform, '');
  // A later page lands: the table re-measures its columns and the button moves right.
  panel.getBoundingClientRect = () => edges(150, 470);
  rerender(view(2));
  assert.equal(panel.style.transform, 'translateX(-78px)');
});
