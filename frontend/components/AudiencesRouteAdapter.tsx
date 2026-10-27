// SPDX-License-Identifier: MIT-0
'use client';

import { bindingByRoute } from '@/lib/page-bindings';
import { PresentationRequestError, PresentationResponseError, type PresentationErrorClass } from '@/lib/presentation';
import { fetchAudiencesPresentationV2, type AudiencesPresentation } from '@/lib/presentation-v2';
import { clearAudiencesResume } from './AudiencesParts';
import { AudiencesView } from './AudiencesView';
import type { V2RouteAdapter, V2RouteUrlState } from './PresentationV2RouteController';

type AudiencesIntent = string | null;

/** Audiences has one fixed month and rejects every query string before any fetch. */
export const audiencesUrlState: V2RouteUrlState<AudiencesIntent> = {
  read: (params) => ({ intent: params.size === 0 ? null : params.toString(), selection: undefined }),
  write: (_selection, invalid) => new URLSearchParams(invalid ?? ''),
};

function pageIssueClass(error: unknown): PresentationErrorClass {
  if (error instanceof PresentationRequestError) return error.errorClass;
  if (error instanceof PresentationResponseError) return 'invariant';
  return 'dependency';
}

/** The parent a page-level absence carries (ADR-139). */
type AudiencesAbsence = Extract<AudiencesPresentation, { readonly kind: 'window_unavailable' }>;
export const audiencesRouteAdapter: V2RouteAdapter<AudiencesPresentation, never, never, AudiencesIntent, AudiencesAbsence> = {
  // ia §8.1 page-level absence: no scope band; the server's text (temporal contract rule 3).
  absence: (parent) => ({ message: parent.message, title: bindingByRoute.audiences.display_name }),
  bandTitle: () => null,
  bandTitleLoading: () => null,
  initialParentIntent: null,
  loadParent: (session, selection, signal, invalid) => invalid === null
    ? fetchAudiencesPresentationV2(session, selection, signal)
    : Promise.reject(new PresentationRequestError('validation')),
  onMount: clearAudiencesResume,
  pageIssueClass,
  pageContext: (parent) => parent.kind === 'parent' && parent.artifact.outcome === 'complete' ? parent.page_context : null,
  render: (state, _requestChild, _clearChild, replaceParentIntent, sessionId = '') => {
    if (state.kind !== 'parent_ready' || state.parent.kind !== 'parent') return null;
    // A part from a newer publication reloads the parent once (design 01 §7).
    return <AudiencesView artifact={state.parent.artifact} reload={() => replaceParentIntent(null)} session={sessionId} />;
  },
  renderLoading: () => <div className="audiences-view" role="status" aria-label="Loading Audiences"><div className="skel card" /><div className="skel card" /></div>,
  renderTemporalControl: false,
  route: 'audiences',
  temporal: (parent) => parent.temporal,
  url: audiencesUrlState,
  windowUnavailable: (parent): parent is AudiencesAbsence => parent.kind === 'window_unavailable',
};
