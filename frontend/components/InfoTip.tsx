// SPDX-License-Identifier: MIT-0
'use client';
import { useEffect, useId, useLayoutEffect, useRef, useState, type ReactNode } from 'react';
import { Icon } from './Icon';

/**
 * An info pop-up (demand forecast design, "Info pop-up behaviour"): a disclosure button and a
 * text-only panel right after it in the DOM, so reading order follows. Click, Enter or Space
 * toggles it; hover never opens it (WCAG 1.4.13). Escape, on the button or on a control inside the
 * open panel, closes it and puts focus on the button; a pointer press outside closes it, and
 * opening one closes any other. Built from spans, so it is
 * valid inside a `th`, `caption` or `p`.
 */
const OPEN_EVENT = 'infotip-open';
/** The gap an open panel keeps from the page column's edge when it has to shift. */
const EDGE = 8;

function columnOf(node: HTMLElement): Element {
  return node.closest('.main') ?? document.documentElement;
}
/**
 * Under its button, shifted only as far as keeps it inside the page column, so an open pop-up
 * never makes the column scroll sideways or slides under the assistant. CSSOM, so the CSP's
 * style-src 'self' holds. An inline panel (below 560 px) already fits and never moves.
 */
function place(node: HTMLElement): void {
  node.style.transform = '';
  const column = columnOf(node).getBoundingClientRect();
  const box = node.getBoundingClientRect();
  let shift = 0;
  if (box.right > column.right) shift = column.right - EDGE - box.right;
  if (box.left + shift < column.left) shift = column.left + EDGE - box.left;
  if (shift !== 0) node.style.transform = `translateX(${Math.round(shift)}px)`;
}

export function InfoTip({ children, className, label, title }: {
  readonly children: ReactNode;
  readonly className?: string | undefined;
  /** The button's accessible name; defaults to "About {title}" with a lower-case first letter. */
  readonly label?: string;
  readonly title: string;
}) {
  const id = useId();
  const [open, setOpen] = useState(false);
  const wrap = useRef<HTMLSpanElement>(null);
  const button = useRef<HTMLButtonElement>(null);
  const panel = useRef<HTMLSpanElement>(null);
  useEffect(() => {
    const onOther = (event: Event) => {
      if (event instanceof CustomEvent && event.detail !== id) setOpen(false);
    };
    window.addEventListener(OPEN_EVENT, onOther);
    return () => window.removeEventListener(OPEN_EVENT, onOther);
  }, [id]);
  useLayoutEffect(() => {
    const node = panel.current;
    if (!open || node === null) return undefined;
    place(node);
    // A resize or rotation while it is open moves the column's edge, or makes the panel inline,
    // and the panel's own content can change its size (the freshness pop-up's schedule lines
    // landing), so it is placed again whenever the column or the panel changes size. jsdom has
    // no ResizeObserver.
    if (typeof ResizeObserver === 'undefined') return undefined;
    const observer = new ResizeObserver(() => place(node));
    observer.observe(columnOf(node));
    observer.observe(node);
    return () => observer.disconnect();
  }, [open]);
  useEffect(() => {
    if (!open) return undefined;
    const onPointer = (event: PointerEvent) => {
      if (!(event.target instanceof Node) || !wrap.current?.contains(event.target)) setOpen(false);
    };
    document.addEventListener('pointerdown', onPointer);
    return () => document.removeEventListener('pointerdown', onPointer);
  }, [open]);
  const toggle = () => {
    const next = !open;
    setOpen(next);
    if (next) window.dispatchEvent(new CustomEvent(OPEN_EVENT, { detail: id }));
  };
  const name = label ?? `About ${title.charAt(0).toLowerCase()}${title.slice(1)}`;
  return (
    <span
      className={className === undefined ? 'infotip' : `infotip ${className}`}
      ref={wrap}
      onKeyDown={(event) => {
        // On the wrapper, so Escape from a control inside the panel (Try again) closes it too.
        if (event.key !== 'Escape' || !open) return;
        event.stopPropagation();
        setOpen(false);
        button.current?.focus();
      }}
    >
      <button
        type="button"
        className="infotip-btn"
        ref={button}
        aria-label={name}
        aria-expanded={open}
        aria-controls={id}
        onClick={toggle}
      >
        <Icon name="info" small/>
      </button>
      <span id={id} className="infotip-panel" hidden={!open} ref={panel}>
        <strong>{title}</strong>
        {children}
      </span>
    </span>
  );
}
