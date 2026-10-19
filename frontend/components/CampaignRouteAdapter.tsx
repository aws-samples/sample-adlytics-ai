// SPDX-License-Identifier: MIT-0
'use client';

import {
  CampaignCommandCenterSkeleton,
  CampaignPortfolioV2View,
  DetailBandTitle,
  PortfolioBandTitle,
  PortfolioBandTitleSkeleton,
  type CampaignSelectionState,
} from './CampaignPortfolioV2View';
import type { V2RouteAdapter, V2RouteUrlState } from './PresentationV2RouteController';
import { bindingByRoute } from '@/lib/page-bindings';
import {
  anyPresetAvailable,
  fetchCampaignPresentationChildV2,
  fetchCampaignPresentationV2,
  focusTemporalSelection,
  type CampaignParentPresentation,
  type CampaignPortfolioSort,
  type CampaignPresentationV2,
  type TemporalSelectionV1,
} from '@/lib/presentation-v2';
import {
  PresentationRequestError,
  PresentationResponseError,
  type PresentationErrorClass,
} from '@/lib/presentation';
import { sameCampaignSignature, type CampaignSelectionCapability } from '@/lib/campaign-attribution';

type CampaignChildPresentation = Extract<CampaignPresentationV2, { readonly kind: 'child' }>;

function pageIssueClass(error: unknown): PresentationErrorClass {
  if (error instanceof PresentationRequestError) return error.errorClass;
  if (error instanceof PresentationResponseError) return 'invariant';
  return 'dependency';
}

function selectionState(
  state: Parameters<typeof campaignRouteAdapter.render>[0],
): CampaignSelectionState {
  if (state.kind === 'child_loading') return { choice: state.command, kind: 'loading' };
  if (state.kind === 'child_unavailable') {
    return { choice: state.command, issueClass: state.issueClass, kind: 'unavailable' };
  }
  return { choice: null, kind: 'idle' };
}

export interface CampaignParentIntent {
  /** The channel filter (ADR-127); absent is All channels. */
  readonly filter?: string;
  readonly segment?: number;
  readonly sort?: CampaignPortfolioSort;
}

const GOVERNED_ID = /^[a-z0-9][a-z0-9_-]{0,63}$/;
const campaignsPolicy = bindingByRoute.campaigns.temporal_policy;

/**
 * Campaigns' URL (ia §8.1): `?preset={preset_id}&channel={filter_id}`, governed ids only. A value
 * the grammar refuses is read as omitted; a channel the server doesn't offer is refused there,
 * and the page then shows All channels with the fallback sentence.
 */
export const campaignUrlState: V2RouteUrlState<CampaignParentIntent> = {
  read: (params) => {
    const preset = params.get('preset');
    const channel = params.get('channel');
    const presetId = campaignsPolicy.candidate_presets.find((candidate) => candidate === preset);
    return {
      intent: channel !== null && GOVERNED_ID.test(channel) && channel !== 'all' ? { filter: channel } : {},
      selection: presetId === undefined || presetId === campaignsPolicy.default_preset
        ? undefined
        : { kind: 'preset', preset_id: presetId },
    };
  },
  write: (selection, intent) => {
    const params = new URLSearchParams();
    if (selection?.kind === 'preset' && selection.preset_id !== campaignsPolicy.default_preset) {
      params.set('preset', selection.preset_id);
    }
    if (intent.filter !== undefined && intent.filter !== 'all') params.set('channel', intent.filter);
    return params;
  },
  // A channel refused and shown as All channels is dropped from the intent, so the URL loses
  // `?channel` and a later preset change doesn't ask for it again (ia §8.1 "treated as omitted").
  served: (parent, intent) => {
    if (intent.filter === undefined) return intent;
    const served = parent as CampaignParentPresentation;
    const selected = served.kind === 'parent' ? served.artifact.channel_filter.selected : intent.filter;
    if (selected === intent.filter) return intent;
    return {
      ...(intent.segment === undefined ? {} : { segment: intent.segment }),
      ...(intent.sort === undefined ? {} : { sort: intent.sort }),
    };
  },
};

/** The refusal of a well-formed channel the window doesn't offer (ADR-127), and only that one. */
export const FILTER_NOT_OFFERED = 'ADLY-PRESENTATION-0005';

/** Keep the served channel filter on a sort or segment request, which re-serves the same option. */
function withFilter(parent: CampaignParentPresentation, intent: CampaignParentIntent): CampaignParentIntent {
  const filter = parent.kind === 'parent' ? parent.artifact.channel_filter.selected : undefined;
  return filter === undefined || filter === 'all' ? intent : { ...intent, filter };
}

/**
 * Why the last parent load showed All channels in place of a channel, or null: `link` when the
 * channel came from the URL, `window` when it was on screen and the new window doesn't offer it.
 */
export type ChannelFallback = 'link' | 'window' | null;
let channelFallback: ChannelFallback = null;
/** The channel the last served parent showed; a refusal of it means the window changed. */
let lastServedFilter: string | undefined;

/**
 * Boundary prefetch (display-paging mandate, docs/ui/campaign-portfolio/README.md):
 * reaching a segment's last display slice prefetches the next baked sibling so the
 * crossing needs no visible request. Entries are keyed by the segment intent, carry the
 * exact request identity they were fetched under (so a window or session change can
 * never consume them), are single-use, and expire after 60 seconds — a served prefetch
 * is at most that much older than a fresh fetch, and the artifact swaps atomically so
 * no page ever mixes two data states.
 */
interface SegmentPrefetchEntry {
  readonly at: number;
  readonly identity: string;
  readonly promise: Promise<CampaignParentPresentation>;
}
const segmentPrefetch = new Map<string, SegmentPrefetchEntry>();
const SEGMENT_PREFETCH_TTL_MS = 60_000;
let lastParentIdentity: { readonly filter: string | undefined; readonly selection: TemporalSelectionV1 | undefined; readonly sessionId: string } | null = null;

function requestIdentity(sessionId: string, selection: TemporalSelectionV1 | undefined, filter?: string): string {
  return JSON.stringify([sessionId, selection ?? null, filter ?? 'all']);
}

function segmentIntentKey(sort: CampaignPortfolioSort | undefined, segment: number | undefined): string {
  return JSON.stringify([sort?.field ?? null, sort?.direction ?? null, segment ?? null]);
}

export function prefetchCampaignSegment(sort: CampaignPortfolioSort, segment: number): void {
  if (lastParentIdentity === null) return;
  const { filter, selection, sessionId } = lastParentIdentity;
  const key = segmentIntentKey(sort, segment);
  const identity = requestIdentity(sessionId, selection, filter);
  const existing = segmentPrefetch.get(key);
  const now = Date.now();
  if (existing !== undefined && existing.identity === identity && now - existing.at < SEGMENT_PREFETCH_TTL_MS) return;
  const promise = fetchCampaignPresentationV2(sessionId, selection, new AbortController().signal, sort, segment, filter);
  // A failed prefetch is evicted — only if the entry is still its own, so a slow
  // rejection never deletes a healthy successor. A crossing that already consumed it
  // falls back to a fresh fetch in loadParent: never worse than no prefetch.
  promise.catch(() => { if (segmentPrefetch.get(key)?.promise === promise) segmentPrefetch.delete(key); });
  segmentPrefetch.set(key, { at: now, identity, promise });
}

/** The parent a page-level absence carries (ADR-139). */
type CampaignAbsence = Extract<CampaignParentPresentation, { readonly kind: 'window_unavailable' }>;

/** Catalog ABS-CAMP-1 / ABS-CAMP-2 (`docs/ui/page-absence-copy.md`). */
export const CAMPAIGN_ABSENCE_TITLE = 'Campaign portfolio is unavailable for this period';
export const CAMPAIGN_ABSENCE_OTHER_PERIODS = 'Campaign reporting is unavailable for the selected period. Choose another available period to view campaign results.';
export const CAMPAIGN_ABSENCE_NO_PERIOD = "Campaign reporting isn't ready to show yet. It's built from your Amazon Ads data. When it's ready, you'll see the campaign results your data supports for each published window.";
export const campaignRouteAdapter: V2RouteAdapter<
  CampaignParentPresentation,
  CampaignChildPresentation,
  CampaignSelectionCapability,
  CampaignParentIntent,
  CampaignAbsence
> = {
  url: campaignUrlState,
  // The v1 contract carries no server text, so the browser owns these catalog rows (ADR-139, N6);
  // another period is offered only when the control has one to choose.
  absence: (parent) => ({
    message: anyPresetAvailable(parent.temporal) ? CAMPAIGN_ABSENCE_OTHER_PERIODS : CAMPAIGN_ABSENCE_NO_PERIOD,
    title: CAMPAIGN_ABSENCE_TITLE,
  }),
  bandTitle: (state, requestChild, replaceParentIntent) => (
    state.kind === 'child_ready'
      ? <DetailBandTitle
        detail={state.child.artifact}
        switcher={state.parent.kind === 'parent' ? {
          command: state.command,
          onSegment: (sort, segment) => replaceParentIntent(withFilter(state.parent, { segment, sort })),
          onSelect: requestChild,
          parent: state.parent.artifact,
        } : undefined}
      />
      : <PortfolioBandTitle
        artifact={'parent' in state && state.parent.kind === 'parent' ? state.parent.artifact : null}
        // A filter change starts the list at its first segment in the default order.
        onFilter={(filter: string) => replaceParentIntent(filter === 'all' ? {} : { filter })}
      />
  ),
  bandTitleLoading: () => <PortfolioBandTitleSkeleton />,
  initialParentIntent: {},
  // A channel last chosen on an earlier visit is a link's channel on this one (ia §8.1).
  onMount: () => {
    channelFallback = null;
    lastServedFilter = undefined;
  },
  child: {
    // Detail opens in place with one history entry, so Back closes it (ia §7.2 item 11a).
    historyMarker: true,
    reopenAfterParentChange: true,
    load: async (parent, choice, sessionId, signal) => {
      if (parent.kind !== 'parent') throw new PresentationResponseError();
      const child = await fetchCampaignPresentationChildV2(sessionId, choice, signal);
      if (child.parent_revision !== parent.artifact_revision) throw new PresentationResponseError();
      return child;
    },
    selectionUnavailable: (error) => error instanceof PresentationRequestError &&
      (error.code === 'ADLY-PRESENTATION-0001' || error.code === 'ADLY-PRESENTATION-0004'),
    // ADR-120 decision 8: the open campaign is the one re-served row with its signature.
    rematch: (parent, child) => {
      if (parent.kind !== 'parent') return null;
      const matches = parent.artifact.rows.filter((row) => sameCampaignSignature(row.signature, child.artifact.selection));
      return matches.length === 1 ? matches[0]?.capability ?? null : null;
    },
  },
  // A page-level absence (an empty or insufficient portfolio or detail) offers no context.
  pageContext: (parent, child) => {
    if (child !== null) return child.artifact.outcome === 'selected' ? child.page_context : null;
    return parent.kind === 'parent' && parent.artifact.outcome === 'complete' ? parent.page_context : null;
  },
  loadParent: async (sessionId: string, selection: TemporalSelectionV1 | undefined, signal: AbortSignal, intent) => {
    lastParentIdentity = { filter: intent.filter, selection, sessionId };
    channelFallback = null;
    const entry = segmentPrefetch.get(segmentIntentKey(intent.sort, intent.segment));
    if (entry !== undefined) {
      segmentPrefetch.delete(segmentIntentKey(intent.sort, intent.segment));
      if (entry.identity === requestIdentity(sessionId, selection, intent.filter) && Date.now() - entry.at < SEGMENT_PREFETCH_TTL_MS) {
        // A prefetch whose request fails (settled or not) falls back to a fresh fetch:
        // consuming a prefetch is never worse than not having one.
        return entry.promise.catch(() => fetchCampaignPresentationV2(sessionId, selection, signal, intent.sort, intent.segment, intent.filter));
      }
    }
    try {
      const parent = await fetchCampaignPresentationV2(sessionId, selection, signal, intent.sort, intent.segment, intent.filter);
      lastServedFilter = intent.filter;
      return parent;
    } catch (error) {
      // Only a channel this window doesn't offer falls back; any other refusal stays an error.
      if (intent.filter === undefined || !(error instanceof PresentationRequestError) || error.code !== FILTER_NOT_OFFERED) throw error;
      // All channels holds every campaign of one channel, so the same sort and segment exist.
      const parent = await fetchCampaignPresentationV2(sessionId, selection, signal, intent.sort, intent.segment);
      channelFallback = lastServedFilter === intent.filter ? 'window' : 'link';
      lastServedFilter = undefined;
      return parent;
    }
  },
  pageIssueClass,
  renderLoading: () => <CampaignCommandCenterSkeleton />,
  render: (state, requestChild, clearChild, replaceParentIntent, sessionId = '', closedChild = null) => {
    if (
      state.kind !== 'parent_ready' && state.kind !== 'child_loading' &&
      state.kind !== 'child_ready' && state.kind !== 'child_unavailable' &&
      state.kind !== 'selection_unavailable_refreshing'
    ) return null;
    if (state.parent.kind !== 'parent') return null;
    return (
      <CampaignPortfolioV2View
        child={state.kind === 'child_ready' ? state.child.artifact : null}
        closedCampaign={closedChild === null ? null : closedChild.artifact.selection.effective_name ?? 'This campaign'}
        detailStep={state.kind === 'child_ready' ? state.child.scorecard_step : null}
        fallback={channelFallback}
        onFilter={(filter) => replaceParentIntent(filter === 'all' ? {} : { filter })}
        scorecardStep={state.parent.scorecard_step}
        focusSelectionReference={state.kind === 'child_ready' ? state.command.selection_reference : null}
        focusSessionId={sessionId}
        focusTemporalSelection={focusTemporalSelection(state.parent.temporal)}
        onClear={clearChild}
        onSelect={requestChild}
        onSegment={(sort, segment) => replaceParentIntent(withFilter(state.parent, { segment, sort }))}
        onSegmentApproach={prefetchCampaignSegment}
        onSort={(sort) => replaceParentIntent(withFilter(state.parent, { sort }))}
        parent={state.parent.artifact}
        selection={selectionState(state)}
      />
    );
  },
  route: 'campaigns',
  generation: (presentation) => (presentation.kind === 'parent' ? presentation.generation : undefined),
  temporal: (presentation) => presentation.temporal,
  windowUnavailable: (presentation): presentation is CampaignAbsence => presentation.kind === 'window_unavailable',
};
