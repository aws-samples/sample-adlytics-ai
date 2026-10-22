// SPDX-License-Identifier: MIT-0
/**
 * A page-level absence (ADR-139, `docs/ui/page-absence-copy.md`): the page's title and the
 * server's message in the shared heading-led callout. It carries no live role: the route
 * controller's one persistent status region announces it, because a live region mounted with its
 * content is not reliably announced (WCAG 4.1.3, ARIA22). No focus move; the window control above
 * stays the keyboard path to another window.
 */
'use client';

import { StateCallout } from './StatePanels';

/**
 * E4 (UX sweep fix 10, `docs/ui/page-absence-copy.md` PAID-NEXT): who acts on a paid-feature card,
 * one browser line after the served text on every page that shows one.
 */
export const PAID_FEATURE_NEXT_STEP = 'Ask your administrator about turning on this paid feature for your workspace.';

export function PageAbsence({ message, title }: { readonly message: string; readonly title: string }) {
  return <StateCallout title={title} tone="neutral">{message}</StateCallout>;
}
