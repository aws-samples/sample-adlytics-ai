// SPDX-License-Identifier: MIT-0
'use client';
import { NewCustomersControlsSkeleton, NewCustomersSkeleton, NewCustomersView, settleNewBuyersArrival } from './NewCustomersView';
import { bindingByRoute } from '@/lib/page-bindings';
import { PresentationRequestError, PresentationResponseError, type PresentationErrorClass } from '@/lib/presentation';
import { fetchNewCustomersPresentationV2, type NewCustomersPresentation } from '@/lib/presentation-v2';
import type { V2RouteAdapter, V2RouteUrlState } from './PresentationV2RouteController';

const policy = bindingByRoute['new-customers'].temporal_policy;

/** The tab's URL (ia §8.1): `?preset={preset_id}`, a declared preset only; anything else reads as omitted. */
export const newCustomersUrlState: V2RouteUrlState<undefined> = {
  read: (params) => {
    const preset = params.get('preset');
    const presetId = policy.candidate_presets.find((candidate) => candidate === preset);
    return {
      intent: undefined,
      selection: presetId === undefined || presetId === policy.default_preset
        ? undefined
        : { kind: 'preset', preset_id: presetId },
    };
  },
  write: (selection) => {
    const params = new URLSearchParams();
    if (selection?.kind === 'preset' && selection.preset_id !== policy.default_preset) params.set('preset', selection.preset_id);
    return params;
  },
};

function pageIssueClass(error: unknown): PresentationErrorClass {
  if (error instanceof PresentationRequestError) return error.errorClass;
  if (error instanceof PresentationResponseError) return 'invariant';
  return 'dependency';
}

/** The parent a page-level absence carries (ADR-139). */
type NewCustomersAbsence = Extract<NewCustomersPresentation, { readonly kind: 'window_unavailable' }>;
export const newCustomersRouteAdapter: V2RouteAdapter<NewCustomersPresentation, never, never, undefined, NewCustomersAbsence> = {
  // ia §8.1 page-level absence: no scope band; the server's text (temporal contract rule 3).
  absence: (parent) => ({ message: parent.message, title: bindingByRoute['new-customers'].display_name }),
  bandTitle: () => null,
  bandTitleLoading: () => null,
  initialParentIntent: undefined,
  // A previous visit's pending arrival focus never carries into this one.
  onMount: () => settleNewBuyersArrival(false),
  // Every settled load tells the arrival focus whether a calendar body came (NC-GATE-106). An
  // aborted load was superseded by a newer one, which settles it instead.
  loadParent: (session, selection, signal) => fetchNewCustomersPresentationV2(session, selection, signal).then(
    (parent) => {
      settleNewBuyersArrival(parent.kind === 'parent' && parent.artifact.prior_period === null);
      return parent;
    },
    (error: unknown) => {
      if (!signal.aborted) settleNewBuyersArrival(false);
      throw error;
    },
  ),
  pageIssueClass,
  // A page-level absence (no complete period, an empty or insufficient body) offers no context.
  pageContext: (parent) => parent.kind === 'parent' && parent.artifact.outcome === 'complete' ? parent.page_context : null,
  render: (state) => {
    if (state.kind !== 'parent_ready' || state.parent.kind !== 'parent') return null;
    return <NewCustomersView artifact={state.parent.artifact} />;
  },
  renderLoading: () => <NewCustomersSkeleton />,
  renderLoadingControls: () => <NewCustomersControlsSkeleton />,
  route: 'new-customers',
  temporal: (parent) => parent.temporal,
  url: newCustomersUrlState,
  windowUnavailable: (parent): parent is NewCustomersAbsence => parent.kind === 'window_unavailable',
};
