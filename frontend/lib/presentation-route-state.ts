// SPDX-License-Identifier: MIT-0
import type { PresentationErrorClass } from '@/lib/presentation';

export type PresentationRouteState<Parent, Child, Command = never> =
  | { readonly kind: 'parent_loading' }
  | { readonly kind: 'window_unavailable'; readonly parent: Parent }
  | { readonly kind: 'parent_ready'; readonly parent: Parent }
  | { readonly command: Command; readonly kind: 'child_loading'; readonly parent: Parent }
  | { readonly child: Child; readonly command: Command; readonly kind: 'child_ready'; readonly parent: Parent }
  | {
    readonly command: Command;
    readonly issueClass: PresentationErrorClass;
    readonly kind: 'child_unavailable';
    readonly parent: Parent;
  }
  | { readonly kind: 'selection_unavailable_refreshing'; readonly parent: Parent }
  | {
    readonly issueClass: PresentationErrorClass;
    readonly kind: 'page_issue';
    /** A `domain` refusal's validated server text (ADR-139), else null. */
    readonly serverMessage: string | null;
  };

export function parentLoading<Parent, Child>(): PresentationRouteState<Parent, Child> {
  return { kind: 'parent_loading' };
}

export function parentReady<Parent, Child>(
  parent: Parent,
): PresentationRouteState<Parent, Child> {
  return { kind: 'parent_ready', parent };
}

export function temporalUnavailable<Parent, Child>(
  parent: Parent,
): PresentationRouteState<Parent, Child> {
  return { kind: 'window_unavailable', parent };
}

export function childLoading<Parent, Child, Command>(
  parent: Parent,
  command: Command,
): PresentationRouteState<Parent, Child, Command> {
  return { command, kind: 'child_loading', parent };
}

export function childReady<Parent, Child, Command>(
  parent: Parent,
  child: Child,
  command: Command,
): PresentationRouteState<Parent, Child, Command> {
  return { child, command, kind: 'child_ready', parent };
}

export function childUnavailable<Parent, Child, Command>(
  parent: Parent,
  command: Command,
  issueClass: PresentationErrorClass,
): PresentationRouteState<Parent, Child, Command> {
  return { command, issueClass, kind: 'child_unavailable', parent };
}

export function selectionUnavailableRefreshing<Parent, Child>(
  parent: Parent,
): PresentationRouteState<Parent, Child> {
  return { kind: 'selection_unavailable_refreshing', parent };
}

export function pageIssue<Parent, Child>(
  issueClass: PresentationErrorClass,
  serverMessage: string | null = null,
): PresentationRouteState<Parent, Child> {
  return { issueClass, kind: 'page_issue', serverMessage };
}

export function stateParent<Parent, Child, Command>(
  state: PresentationRouteState<Parent, Child, Command>,
): Parent | null {
  switch (state.kind) {
    case 'window_unavailable':
    case 'parent_ready':
    case 'child_loading':
    case 'child_ready':
    case 'child_unavailable':
    case 'selection_unavailable_refreshing':
      return state.parent;
    case 'parent_loading':
    case 'page_issue':
      return null;
  }
}
