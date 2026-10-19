// SPDX-License-Identifier: MIT-0
'use client';
import { ScorecardControlsSkeleton, ScorecardSkeleton, ScorecardView } from './ScorecardView';
import { bindingByRoute } from '@/lib/page-bindings';
import { PresentationRequestError, PresentationResponseError, type PresentationErrorClass } from '@/lib/presentation';
import {
  fetchScorecardPresentationV2,
  type ScorecardPresentation,
} from '@/lib/presentation-v2';
import type { V2RouteAdapter, V2RouteUrlState } from './PresentationV2RouteController';

/** The channel row a campaign's step asks the Scorecard to outline (ADR-122 `highlight_channel`). */
export interface ScorecardParentIntent {
  readonly highlight?: string;
}

/** A served Scorecard with the highlight its URL validated, so render reads one source. */
export type ScorecardRouteParent = ScorecardPresentation & { readonly highlight: string | null };

const GOVERNED_ID = /^[a-z0-9][a-z0-9_-]{0,63}$/;
const policy = bindingByRoute.scorecard.temporal_policy;

/**
 * The Scorecard's URL (ia §8.1): `?preset={preset_id}`, plus `highlight={channel}` when a campaign
 * opened it on its channel's row. Governed ids only; an unknown value reads as omitted.
 */
export const scorecardUrlState: V2RouteUrlState<ScorecardParentIntent> = {
  read: (params) => {
    const preset = params.get('preset');
    const highlight = params.get('highlight');
    const presetId = policy.candidate_presets.find((candidate) => candidate === preset);
    return {
      intent: highlight !== null && GOVERNED_ID.test(highlight) ? { highlight } : {},
      selection: presetId === undefined || presetId === policy.default_preset
        ? undefined
        : { kind: 'preset', preset_id: presetId },
    };
  },
  write: (selection, intent) => {
    const params = new URLSearchParams();
    if (selection?.kind === 'preset' && selection.preset_id !== policy.default_preset) params.set('preset', selection.preset_id);
    if (intent.highlight !== undefined) params.set('highlight', intent.highlight);
    return params;
  },
};

function pageIssueClass(error: unknown): PresentationErrorClass {
  if (error instanceof PresentationRequestError) return error.errorClass;
  if (error instanceof PresentationResponseError) return 'invariant';
  return 'dependency';
}

/** The parent a page-level absence carries (ADR-139). */
type ScorecardAbsence = Extract<ScorecardRouteParent, { readonly kind: 'window_unavailable' }>;
export const scorecardRouteAdapter: V2RouteAdapter<ScorecardRouteParent, never, never, ScorecardParentIntent, ScorecardAbsence> = {
  // ia §8.1 page-level absence: no scope band; the server's text (temporal contract rule 3).
  absence: (parent) => ({ message: parent.message, title: bindingByRoute.scorecard.display_name }),
  bandTitle: () => null,
  bandTitleLoading: () => null,
  initialParentIntent: {},
  loadParent: async (session, selection, signal, intent) => ({
    ...await fetchScorecardPresentationV2(session, selection, signal),
    highlight: intent.highlight ?? null,
  }),
  pageIssueClass,
  // A page-level absence (no complete period, an empty body) offers no context.
  pageContext: (parent) => parent.kind === 'parent' && parent.artifact.outcome === 'complete' ? parent.page_context : null,
  render: (state) => {
    if (state.kind !== 'parent_ready' || state.parent.kind !== 'parent') return null;
    return <ScorecardView artifact={state.parent.artifact} highlight={state.parent.highlight} />;
  },
  renderLoading: () => <ScorecardSkeleton />,
  renderLoadingControls: () => <ScorecardControlsSkeleton />,
  route: 'scorecard',
  temporal: (parent) => parent.temporal,
  url: scorecardUrlState,
  windowUnavailable: (parent): parent is ScorecardAbsence => parent.kind === 'window_unavailable',
};
