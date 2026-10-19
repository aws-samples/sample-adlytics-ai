// SPDX-License-Identifier: MIT-0
'use client';

import { createContext, useContext, useLayoutEffect } from 'react';

import type { PageContextV1 } from '@/lib/page-context';

/**
 * What the page on screen tells the assistant (ADR-120, ia §8.8). A presentation tab publishes
 * one of these; a route that publishes nothing (Prompt administration) has no page context.
 *
 * - `loading`: the page or its detail is loading, so a turn sends no reference.
 * - `none`: a page error or a page-level absence state; no reference and no starter questions.
 * - `page`: the served context, and how to refresh it (decision 8). `refresh` resolves to the
 *   fresh context, or null when the refresh failed and the page should be reloaded.
 */
export type AssistantPage =
  | { readonly kind: 'loading' }
  | { readonly kind: 'none' }
  | {
    readonly kind: 'page';
    readonly context: PageContextV1;
    readonly refresh: () => Promise<PageContextV1 | null>;
  };

export const AssistantPagePublisher = createContext<(page: AssistantPage | null) => void>(() => undefined);

/** Publish the page's context to the assistant while the page is mounted. */
export function usePublishAssistantPage(page: AssistantPage): void {
  const publish = useContext(AssistantPagePublisher);
  // Before paint, so the chip never shows another page's text for a frame.
  useLayoutEffect(() => {
    publish(page);
  }, [page, publish]);
  useLayoutEffect(() => () => publish(null), [publish]);
}
