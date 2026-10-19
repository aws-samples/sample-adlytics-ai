// SPDX-License-Identifier: MIT-0
'use client';

/**
 * The one modal primitive (ADR-118 decision 6): a scrim, an inert app, a focus trap, Escape to
 * close and a sticky header with Close. One modal at a time: a dialog opened while another is
 * open replaces it, and closing the second returns focus to whatever opened the first.
 *
 * The slide-out menu sheet is the same primitive in its sheet variant, so every modal surface
 * shares one set of mechanics.
 */

import { useEffect, useLayoutEffect, useRef, type ReactNode, type RefObject } from 'react';
import { createPortal } from 'react-dom';

/** The shell root that goes inert while a modal is open; the modal itself is portalled out of it. */
export const APP_ROOT_ID = 'app-root';
/** The page h1, focused when a modal's opener no longer exists. */
export const PAGE_HEADING_ID = 'page-heading';

const FOCUSABLE = 'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

interface ModalLayer {
  readonly returnTarget: HTMLElement | null;
  replaced: boolean;
  /** False when the modal closed for the assistant: focus moves to the composer instead. */
  restore: boolean;
  readonly requestClose: () => void;
}

let currentLayer: ModalLayer | null = null;
let inertDepth = 0;

function setAppInert(inert: boolean): void {
  const root = document.getElementById(APP_ROOT_ID);
  if (root !== null) root.inert = inert;
  document.body.classList.toggle('modal-open', inert);
}

function focusableElements(container: HTMLElement): HTMLElement[] {
  return Array.from(container.querySelectorAll<HTMLElement>(FOCUSABLE));
}

/**
 * "Asking from the page" step 1 (ia §8.8): close the open modal without returning focus to its
 * opener, and hand the opener back as the assistant's return target. Null when no modal is open.
 */
export function closeModalForAssistant(): HTMLElement | null {
  const layer = currentLayer;
  if (layer === null) return null;
  layer.restore = false;
  layer.requestClose();
  return layer.returnTarget;
}

/** Where focus goes when a modal closes: its opener, or the page h1 if the opener is gone. */
function restoreFocus(target: HTMLElement | null): void {
  if (target?.isConnected) {
    target.focus();
    return;
  }
  document.getElementById(PAGE_HEADING_ID)?.focus();
}

export function Dialog({
  bodyClassName,
  children,
  className,
  closeLabel = 'Close',
  header,
  headerEnd,
  labelledBy,
  label,
  onClose,
  opener,
  surface,
  variant = 'dialog',
}: {
  readonly bodyClassName?: string;
  readonly children: ReactNode;
  readonly className?: string;
  readonly closeLabel?: string;
  /** The title block; it holds the element `labelledBy` names. */
  readonly header: ReactNode;
  /** Controls beside Close in the sticky header, such as a metric switcher. */
  readonly headerEnd?: ReactNode;
  readonly labelledBy?: string;
  readonly label?: string;
  readonly onClose: () => void;
  /**
   * The control that opened this modal. Safari doesn't focus a clicked button, so the owner
   * names it; without it the focused element at open time is the return target.
   */
  readonly opener?: RefObject<HTMLElement | null> | undefined;
  readonly surface?: string;
  readonly variant?: 'dialog' | 'sheet';
}) {
  const panel = useRef<HTMLDivElement>(null);
  const closeButton = useRef<HTMLButtonElement>(null);
  const onCloseRef = useRef(onClose);
  useLayoutEffect(() => {
    onCloseRef.current = onClose;
  }, [onClose]);

  useLayoutEffect(() => {
    const previous = currentLayer;
    const layer: ModalLayer = {
      // A dialog that replaces another inherits its return target: the first opener.
      returnTarget: previous !== null
        ? previous.returnTarget
        : opener?.current ?? (document.activeElement instanceof HTMLElement && document.activeElement !== document.body
          ? document.activeElement
          : null),
      replaced: false,
      restore: true,
      requestClose: () => onCloseRef.current(),
    };
    if (previous !== null) {
      previous.replaced = true;
      previous.requestClose();
    }
    currentLayer = layer;
    inertDepth += 1;
    setAppInert(true);
    closeButton.current?.focus();
    // Mount-only: the return target is fixed when the modal opens.
    return () => {
      if (currentLayer === layer) currentLayer = null;
      inertDepth -= 1;
      if (inertDepth === 0) setAppInert(false);
      if (!layer.replaced && layer.restore) restoreFocus(layer.returnTarget);
    };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const keydown = (event: KeyboardEvent) => {
      if (panel.current === null || currentLayer === null) return;
      if (event.key === 'Escape') {
        event.preventDefault();
        onCloseRef.current();
        return;
      }
      if (event.key !== 'Tab') return;
      const items = focusableElements(panel.current);
      const first = items[0];
      const last = items.at(-1);
      if (first === undefined || last === undefined) {
        event.preventDefault();
        panel.current.focus();
      } else if (event.shiftKey && (document.activeElement === first || !panel.current.contains(document.activeElement))) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && (document.activeElement === last || !panel.current.contains(document.activeElement))) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener('keydown', keydown);
    return () => document.removeEventListener('keydown', keydown);
  }, []);

  const panelClass = [variant === 'sheet' ? 'sheet' : 'dialog', className].filter(Boolean).join(' ');
  return createPortal(
    <div
      className={variant === 'sheet' ? 'scrim sheet-scrim' : 'scrim'}
      role="presentation"
      onPointerDown={(event) => {
        if (variant === 'sheet' && event.target === event.currentTarget) onCloseRef.current();
      }}
    >
      <div
        ref={panel}
        className={panelClass}
        role="dialog"
        aria-modal="true"
        {...(labelledBy === undefined ? {} : { 'aria-labelledby': labelledBy })}
        {...(label === undefined ? {} : { 'aria-label': label })}
        tabIndex={-1}
        {...(surface === undefined ? {} : { 'data-surface': surface })}
      >
        <div className="dialog-head">
          <div className="ttl">{header}</div>
          {headerEnd}
          <button ref={closeButton} className="tb-btn" type="button" onClick={() => onCloseRef.current()} aria-label={closeLabel}>
            <svg className="icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M6 6l12 12M18 6 6 18" /></svg>
          </button>
        </div>
        <div className={bodyClassName === undefined ? 'dialog-body' : `dialog-body ${bodyClassName}`}>{children}</div>
      </div>
    </div>,
    document.body,
  );
}
