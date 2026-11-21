// SPDX-License-Identifier: MIT-0
'use client';
import { useEffect, useId, useRef, useState, type ReactNode } from 'react';
import { Icon } from './Icon';

/**
 * An info pop-up (demand forecast design, "Info pop-up behaviour"): a disclosure button and a
 * text-only panel right after it in the DOM, so reading order follows. Click, Enter or Space
 * toggles it; hover never opens it (WCAG 1.4.13). Escape closes it with focus kept on the button,
 * a pointer press outside closes it, and opening one closes any other. Built from spans, so it is
 * valid inside a `th`, `caption` or `p`.
 */
const OPEN_EVENT = 'infotip-open';

export function InfoTip({ children, className, label, title }: {
  readonly children: ReactNode;
  readonly className?: string;
  /** The button's accessible name; defaults to "About {title}" with a lower-case first letter. */
  readonly label?: string;
  readonly title: string;
}) {
  const id = useId();
  const [open, setOpen] = useState(false);
  const wrap = useRef<HTMLSpanElement>(null);
  const button = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    const onOther = (event: Event) => {
      if (event instanceof CustomEvent && event.detail !== id) setOpen(false);
    };
    window.addEventListener(OPEN_EVENT, onOther);
    return () => window.removeEventListener(OPEN_EVENT, onOther);
  }, [id]);
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
    <span className={className === undefined ? 'infotip' : `infotip ${className}`} ref={wrap}>
      <button
        type="button"
        className="infotip-btn"
        ref={button}
        aria-label={name}
        aria-expanded={open}
        aria-controls={id}
        onClick={toggle}
        onKeyDown={(event) => {
          if (event.key !== 'Escape' || !open) return;
          event.stopPropagation();
          setOpen(false);
          button.current?.focus();
        }}
      >
        <Icon name="info" small/>
      </button>
      <span id={id} className="infotip-panel" hidden={!open}>
        <strong>{title}</strong>
        {children}
      </span>
    </span>
  );
}
