// SPDX-License-Identifier: MIT-0
'use client';

import { bindingByRoute } from '@/lib/page-bindings';
import { PresentationRequestError, PresentationResponseError, type PresentationErrorClass } from '@/lib/presentation';
import { fetchRetentionPresentationV2, type RetentionPresentation } from '@/lib/presentation-v2';
import { RetentionView } from './RetentionView';
import type { V2RouteAdapter, V2RouteUrlState } from './PresentationV2RouteController';

type RetentionIntent = string | null;
/** Retention serves only its latest vintage and rejects every query string before any fetch. */
export const retentionUrlState: V2RouteUrlState<RetentionIntent> = {
  read: (params) => ({ intent: params.size === 0 ? null : params.toString(), selection: undefined }),
  write: (_selection, invalid) => new URLSearchParams(invalid ?? ''),
};
function pageIssueClass(error: unknown): PresentationErrorClass {
  if (error instanceof PresentationRequestError) return error.errorClass;
  if (error instanceof PresentationResponseError) return 'invariant';
  return 'dependency';
}

/** The parent a page-level absence carries (ADR-139). */
type RetentionAbsence = Extract<RetentionPresentation, { readonly kind: 'window_unavailable' }>;
export const retentionRouteAdapter: V2RouteAdapter<RetentionPresentation, never, never, RetentionIntent, RetentionAbsence> = {
  // ia §8.1 page-level absence: no scope band; the server's text (temporal contract rule 3).
  absence: (parent) => ({ message: parent.message, title: bindingByRoute.retention.display_name }),
  bandTitle: () => null,
  bandTitleLoading: () => null,
  initialParentIntent: null,
  loadParent: (session, _selection, signal, invalid) => invalid === null
    ? fetchRetentionPresentationV2(session, signal)
    : Promise.reject(new PresentationRequestError('validation')),
  pageIssueClass,
  pageContext: (parent) => parent.kind === 'parent' && parent.artifact.outcome === 'complete' ? parent.page_context : null,
  render: (state) => {
    if (state.kind !== 'parent_ready' || state.parent.kind !== 'parent') return null;
    return <RetentionView artifact={state.parent.artifact} />;
  },
  renderLoading: () => <div className="retention-view" role="status" aria-label="Loading Retention"><div className="section"><span className="skel h-chart" /></div></div>,
  route: 'retention',
  temporal: (parent) => parent.temporal,
  url: retentionUrlState,
  windowUnavailable: (parent): parent is RetentionAbsence => parent.kind === 'window_unavailable',
};
