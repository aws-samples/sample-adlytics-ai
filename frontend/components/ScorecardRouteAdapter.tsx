// SPDX-License-Identifier: MIT-0
'use client';
import { ScorecardControlsSkeleton, ScorecardSkeleton, ScorecardView } from './ScorecardView';
import { bindingByRoute, type TemporalPresetId } from '@/lib/page-bindings';
import { PresentationRequestError, PresentationResponseError, type PresentationErrorClass } from '@/lib/presentation';
import {
  fetchNewCustomersPresentationV2,
  fetchScorecardPresentationV2,
  type NewCustomersPresentation,
  type ScorecardPresentation,
  type TemporalSelectionV1,
} from '@/lib/presentation-v2';
import type { OverviewSource } from '@/lib/overview';
import type { V2RouteAdapter, V2RouteUrlState } from './PresentationV2RouteController';

/** The channel row a campaign's step asks the Scorecard to outline (ADR-122 `highlight_channel`). */
export interface ScorecardParentIntent {
  readonly highlight?: string;
}

/**
 * A served Scorecard with the highlight its URL validated, plus the served New customers
 * presentation for the same preset (plan 2.3), settled on its own so it never blocks the page.
 */
export type ScorecardRouteParent = ScorecardPresentation & {
  readonly highlight: string | null;
  readonly newCustomers: OverviewSource<NewCustomersPresentation>;
  readonly preset: TemporalPresetId;
};

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
  loadParent: async (session, selection, signal, intent) => {
    // The same preset for both: the Scorecard's default when the URL names none, because the
    // two pages declare different defaults.
    const preset = selection?.kind === 'preset' ? selection.preset_id : policy.default_preset;
    const sameSelection: TemporalSelectionV1 = { kind: 'preset', preset_id: preset };
    const [scorecard, newCustomers] = await Promise.allSettled([
      fetchScorecardPresentationV2(session, selection, signal),
      fetchNewCustomersPresentationV2(session, sameSelection, signal),
    ]);
    if (scorecard.status === 'rejected') throw scorecard.reason;
    return {
      ...scorecard.value,
      highlight: intent.highlight ?? null,
      newCustomers: newCustomers.status === 'fulfilled'
        ? { kind: 'served', presentation: newCustomers.value }
        : { issue: pageIssueClass(newCustomers.reason), kind: 'rejected' },
      preset,
    };
  },
  pageIssueClass,
  // A page-level absence (no complete period, an empty body) offers no context.
  pageContext: (parent) => parent.kind === 'parent' && parent.artifact.outcome === 'complete' ? parent.page_context : null,
  render: (state) => {
    if (state.kind !== 'parent_ready' || state.parent.kind !== 'parent') return null;
    return <ScorecardView
      artifact={state.parent.artifact}
      highlight={state.parent.highlight}
      newCustomers={state.parent.newCustomers}
      preset={state.parent.preset === bindingByRoute['new-customers'].temporal_policy.default_preset ? null : state.parent.preset}
    />;
  },
  renderLoading: () => <ScorecardSkeleton />,
  renderLoadingControls: () => <ScorecardControlsSkeleton />,
  route: 'scorecard',
  temporal: (parent) => parent.temporal,
  url: scorecardUrlState,
  windowUnavailable: (parent): parent is ScorecardAbsence => parent.kind === 'window_unavailable',
};
