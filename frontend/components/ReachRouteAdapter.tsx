// SPDX-License-Identifier: MIT-0
'use client';

import { Fragment } from 'react';

import { bindingByRoute, isCalendarPreset } from '@/lib/page-bindings';
import { PresentationRequestError, PresentationResponseError, type PresentationErrorClass } from '@/lib/presentation';
import { fetchReachPresentationV2, type ReachPresentation } from '@/lib/presentation-v2';
import { ReachAndFunnelView } from './ReachAndFunnelView';
import type { V2RouteAdapter, V2RouteUrlState } from './PresentationV2RouteController';

const policy = bindingByRoute.reach.temporal_policy;
type ReachParentIntent = string | null;

/** Reach keeps only a non-default declared calendar preset in the URL. */
export const reachUrlState: V2RouteUrlState<ReachParentIntent> = {
  read: (params) => {
    const preset = params.get('preset');
    const presetId = policy.candidate_presets.find((candidate) => candidate === preset);
    if (preset !== null && presetId === undefined) {
      return { intent: preset, selection: undefined };
    }
    return {
      intent: null,
      selection: presetId === undefined || presetId === policy.default_preset
        ? undefined
        : { kind: 'preset', preset_id: presetId },
    };
  },
  write: (selection, invalidPreset) => {
    const params = new URLSearchParams();
    if (invalidPreset !== null) params.set('preset', invalidPreset);
    else if (selection?.kind === 'preset' && selection.preset_id !== policy.default_preset) {
      params.set('preset', selection.preset_id);
    }
    return params;
  },
};

function pageIssueClass(error: unknown): PresentationErrorClass {
  if (error instanceof PresentationRequestError) return error.errorClass;
  if (error instanceof PresentationResponseError) return 'invariant';
  return 'dependency';
}

function ReachControlsSkeleton() {
  const firstCalendar = policy.candidate_presets.findIndex((preset) => isCalendarPreset(preset));
  return <div className="controls controls-skel" aria-hidden="true">
    <div className="datectl dc-wide">
      <span className="skel h-label legend-skel" />
      <div className="seg">
        {policy.candidate_presets.map((preset, index) => <Fragment key={preset}>
          {index === firstCalendar && index > 0 ? <span className="seg-div" /> : null}
          <span className="seg-opt"><span className="skel h-line" /><span className="skel h-line short" /></span>
        </Fragment>)}
      </div>
    </div>
    <div className="select-wrap dc-narrow"><span className="skel h-label legend-skel" /><span className="skel select-skel" /></div>
  </div>;
}

/** Reserve the three major analytical regions while the first calendar body resolves. */
function ReachSkeleton() {
  return <div className="reach-view" aria-hidden="true">
    {[0, 1, 2].map((section) => <div className="section reach-section" key={section}>
      <span className="skel h-label" />
      <span className="skel h-chart" />
    </div>)}
  </div>;
}

/** The parent a page-level absence carries (ADR-139). */
type ReachAbsence = Extract<ReachPresentation, { readonly kind: 'window_unavailable' }>;
export const reachRouteAdapter: V2RouteAdapter<ReachPresentation, never, never, ReachParentIntent, ReachAbsence> = {
  // ia §8.1 page-level absence: no scope band; the server's text (temporal contract rule 3).
  absence: (parent) => ({ message: parent.message, title: bindingByRoute.reach.display_name }),
  bandTitle: () => null,
  bandTitleLoading: () => null,
  initialParentIntent: null,
  loadParent: (session, selection, signal, invalidPreset) => invalidPreset === null
    ? fetchReachPresentationV2(session, selection, signal)
    : Promise.reject(new PresentationRequestError('validation')),
  pageIssueClass,
  pageContext: (parent) => parent.kind === 'parent' && parent.artifact.outcome === 'complete'
    ? parent.page_context
    : null,
  render: (state) => {
    if (state.kind !== 'parent_ready' || state.parent.kind !== 'parent') return null;
    return <ReachAndFunnelView artifact={state.parent.artifact} />;
  },
  renderLoading: () => <ReachSkeleton />,
  renderLoadingControls: () => <ReachControlsSkeleton />,
  route: 'reach',
  temporal: (parent) => parent.temporal,
  url: reachUrlState,
  windowUnavailable: (parent): parent is ReachAbsence => parent.kind === 'window_unavailable',
};
