// SPDX-License-Identifier: MIT-0
'use client';
/**
 * Overview › Growth check (ADR-151): a composed tab. Its parent is the four sources' served
 * 7-day presentations, each settled on its own, so one source that fails never blocks the rest.
 * The window is fixed (the one preset all three window-bound sources declare), so the tab shows
 * no date control and keeps nothing in its URL.
 */
import { OverviewBandTitle, OverviewSkeleton, OverviewView, type OverviewSources } from './OverviewView';
import { OVERVIEW_PRESET, type OverviewSource } from '@/lib/overview';
import { PresentationRequestError, PresentationResponseError, type PresentationErrorClass } from '@/lib/presentation';
import {
  fetchCampaignPresentationV2,
  fetchNewCustomersPresentationV2,
  fetchRetailHealthPresentationV2,
  fetchScorecardPresentationV2,
  type TemporalSelectionV1,
  type VintageControlV2,
} from '@/lib/presentation-v2';
import type { V2RouteAdapter } from './PresentationV2RouteController';

function issueClass(error: unknown): PresentationErrorClass {
  if (error instanceof PresentationRequestError) return error.errorClass;
  if (error instanceof PresentationResponseError) return 'invariant';
  return 'dependency';
}

function settled<Presentation>(result: PromiseSettledResult<Presentation>): OverviewSource<Presentation> {
  return result.status === 'fulfilled'
    ? { kind: 'served', presentation: result.value }
    : { issue: issueClass(result.reason), kind: 'rejected' };
}

/** No date control: the tab's window is the fixed 7-day preset its bands each request. */
const NO_CONTROL: VintageControlV2 = { mode: 'fixed', resolved: null, version: 2 };

const PRESET: TemporalSelectionV1 = { kind: 'preset', preset_id: OVERVIEW_PRESET };

/** Every band carries its own absence, so the tab itself is never a page-level absence. */
function neverAbsent(parent: OverviewSources): parent is never {
  void parent;
  return false;
}

export const overviewRouteAdapter: V2RouteAdapter<OverviewSources, never, never, undefined, never> = {
  // Never called: `neverAbsent` never admits a page-level absence.
  absence: () => ({ message: '', title: 'Overview' }),
  bandTitle: (state) => {
    const parent = 'parent' in state ? state.parent : null;
    const scorecard = parent?.scorecard.kind === 'served' && parent.scorecard.presentation.kind === 'parent'
      ? parent.scorecard.presentation
      : null;
    return <OverviewBandTitle scorecard={scorecard} />;
  },
  bandTitleLoading: () => null,
  loadParent: async (session, _selection, signal) => {
    const [scorecard, retail, newCustomers, campaigns] = await Promise.allSettled([
      fetchScorecardPresentationV2(session, PRESET, signal),
      fetchRetailHealthPresentationV2(session, PRESET, signal),
      fetchNewCustomersPresentationV2(session, PRESET, signal),
      // All channels: the served spend-only trend, the one daily spend series across channels.
      fetchCampaignPresentationV2(session, PRESET, signal),
    ]);
    return {
      campaigns: settled(campaigns),
      newCustomers: settled(newCustomers),
      retail: settled(retail),
      scorecard: settled(scorecard),
    };
  },
  pageIssueClass: issueClass,
  // The assistant reads the Scorecard's served page context, the tab's headline source.
  pageContext: (parent) => (
    parent.scorecard.kind === 'served' && parent.scorecard.presentation.kind === 'parent' &&
    parent.scorecard.presentation.artifact.outcome === 'complete'
      ? parent.scorecard.presentation.page_context
      : null
  ),
  render: (state) => (state.kind === 'parent_ready' ? <OverviewView sources={state.parent} /> : null),
  renderLoading: () => <OverviewSkeleton />,
  renderTemporalControl: false,
  // The Scorecard is the tab's headline source; the route only names the binding the controller
  // reads for a date control and URL writes, and this tab has neither.
  route: 'scorecard',
  temporal: () => NO_CONTROL,
  windowUnavailable: neverAbsent,
};
