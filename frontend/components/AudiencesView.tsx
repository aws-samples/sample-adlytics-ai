// SPDX-License-Identifier: MIT-0
'use client';

import { audiencesAnswer } from '@/lib/audiences';
import { AnswerSentence } from './AnswerSentence';
import { PAID_FEATURE_NEXT_STEP } from './PageAbsence';
import { displayFigure, formatCount } from '@/lib/exact-format';
import { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react';
import { AUD_CAMPAIGN_GONE, AUD_PAGE_REVISION_CHANGED, CampaignCombobox, Pager, PartFailed, takeAudiencesResume, useIndexShards, usePart } from './AudiencesParts';

import type { AudienceShare, AudienceCampaignPage, AudienceProfilePage, AudienceRow, AudienceUnavailableRow, CampaignIndexEntry } from '@/lib/audiences';
import type { AudienceDelivery, AudienceMarket, AudienceProfileArtifact, AudienceSegmentNames, AudienceView, AudienceViewId } from '@/lib/audiences';
import type { AudiencesResume, PartContext } from './AudiencesParts';
import { DataTable, DtBody, DtCell, DtColumn, DtHead, DtRow, DtRowHeader } from './DataTable';

/** UI refresh 3.4 (readout A.6): who acts on a segment, in the mockup's words. */
export const AUDIENCES_ACTIVATE_LINE = "You can build a similar audience in your own tools. Adlytics doesn't create or activate audiences.";
/** UI refresh 3.4: campaigns by segment fold into one disclosure under this name. */
export const AUDIENCES_CAMPAIGNS_SUMMARY = "Where this campaign's sales came from";

/** A served count, digit-grouped (UI refresh D4). The decoder pins it to the count's own digits (`countPair`). */
const groupedCount = (text: string | null): string | null => text === null ? null : formatCount(text);
function SegmentName({ row }: { readonly row: AudienceSegmentNames }) {
  return <>{row.display_name}{row.provider_label === null ? null : <span className="sub">{row.provider_label}</span>}{row.category_path === null ? null : <span className="sub">{row.category_path}</span>}</>;
}

const metricLabels = (buyerMode: 'all' | 'new') => [buyerMode === 'all' ? 'Share of buyers' : 'New-to-brand buyers', 'Share of reached shoppers', 'Index'] as const;

/**
 * The one renderer for a segment the server marks not_available, in the roster or as a gap:
 * its reason once, verbatim, in the first metric cell, and in every other metric cell a
 * visual-only marker described by that reason. No count is shown beside it, in either buyer mode.
 */
function UnavailableSegmentRow({ buyerMode, reason, row }: {
  readonly buyerMode: 'all' | 'new';
  readonly reason: string;
  readonly row: AudienceSegmentNames;
}) {
  const reasonId = useId();
  const [first, ...rest] = metricLabels(buyerMode);
  return <DtRow className="na-row">
    <DtRowHeader><SegmentName row={row} /></DtRowHeader>
    <DtCell label={first}><span id={reasonId}>{reason}</span></DtCell>
    {rest.map((label) => <DtCell aria-describedby={reasonId} key={label} label={label}><span aria-hidden="true">—</span></DtCell>)}
  </DtRow>;
}

/**
 * UI refresh 3.4 (readout A.6): the served buyer share and reached share as two bars on one
 * zero-based 0–100% scale, so a segment over its reach share reads at a glance. Decorative: the
 * cells beside them carry the served text.
 */
function PairedBars({ buyer, reached }: { readonly buyer: AudienceShare | null; readonly reached: AudienceShare | null }) {
  if (buyer === null || reached === null) return null;
  const width = (share: AudienceShare) => `${Math.max(0, Math.min(1, Number(share.value))) * 100}%`;
  return <span className="au-bars" aria-hidden="true">
    <span className="au-bar buyers" style={{ width: width(buyer) }} />
    <span className="au-bar reached" style={{ width: width(reached) }} />
  </span>;
}

function SegmentTable({ announce, buyerMode, delivery, view, parts, resumePage }: {
  readonly announce: (message: string) => void;
  readonly buyerMode: 'all' | 'new';
  readonly delivery: AudienceDelivery;
  readonly view: AudienceView;
  readonly parts: PartContext;
  readonly resumePage: number;
}) {
  const [page, setPage] = useState(resumePage);
  const tableRef = useRef<HTMLDivElement>(null);
  const moved = useRef(false);
  const onMismatch = useCallback((mismatches: number): AudiencesResume => ({ kind: 'profile', mismatches, page, path: delivery.profile_path }), [delivery.profile_path, page]);
  const { request, state } = usePart<AudienceProfilePage>(parts, onMismatch);
  const path = delivery.profile_path;
  useEffect(() => {
    if (page > 0) void request({ kind: 'profile', page, path });
  }, [page, path, request]);
  const parent = { not_available: view.not_available.filter((row) => row.profile_path === path), rows: view.rows.filter((row) => row.profile_path === path) };
  const shown: { readonly rows: readonly AudienceRow[]; readonly not_available: readonly AudienceUnavailableRow[] } | null = page === 0 ? parent : state.kind === 'ready' ? state.part : null;
  const pagingText = page === 0 ? delivery.paging_text : state.kind === 'ready' ? state.part.paging_text : null;
  useEffect(() => {
    // After a page change, focus moves to the table and the status region says which rows show.
    if (!moved.current || shown === null) return;
    tableRef.current?.focus();
    if (pagingText !== null) announce(pagingText);
  }, [announce, pagingText, shown]);
  if (!delivery.returned) return <div className="callout audience-state" role="status"><p>{delivery.period_text ?? delivery.insights[0]?.absence_reason}</p></div>;
  const toPage = (next: number) => { moved.current = true; setPage(next); };
  if (shown === null) {
    return <div className="audience-delivery"><h3>{delivery.label}</h3>{state.kind === 'failed' ? <PartFailed onRetry={() => void request({ kind: 'profile', page, path })} /> : <div className="skel card" role="status" aria-label={`Loading ${delivery.label}`} />}<Pager label={delivery.label} page={page} pageCount={delivery.page_count} pagingText={pagingText} onPage={toPage} /></div>;
  }
  const { rows, not_available: unavailable } = shown;
  return <div className="audience-delivery">
    <h3>{delivery.label}</h3>
    {delivery.period_text === null ? null : <p className="state-note">{delivery.period_text}</p>}
    {delivery.total_text === null ? null : <p className="state-note">{delivery.total_text}</p>}
    {delivery.first_month_note === null ? null : <p className="sec-line">{delivery.first_month_note}</p>}
    {delivery.roster_note === null ? null : <p className="sec-line">{delivery.roster_note}</p>}
    {buyerMode === 'all' ? <p className="au-legend" aria-hidden="true"><span className="au-bar buyers" /> Share of buyers <span className="au-bar reached" /> Share of reached shoppers</p> : null}
    <div ref={tableRef} tabIndex={-1} aria-label={delivery.label}>
    <DataTable title="Segments your buyers belong to, compared with shoppers your ads reached" note={`${view.caption} · ${delivery.label}`} nameWithNote>
      <DtHead><DtRow>
        <DtColumn className="l">Segment</DtColumn>
        <DtColumn>{buyerMode === 'all' ? 'Share of buyers' : 'New-to-brand buyers'}</DtColumn>
        <DtColumn>Share of reached shoppers</DtColumn>
        <DtColumn>Index</DtColumn>
        {buyerMode === 'all' ? <DtColumn><span className="sr-only">Shares side by side</span></DtColumn> : null}
      </DtRow></DtHead>
      <DtBody>
        {/* The decoder pins every not_available row's text to TOTAL_NOT_AVAILABLE_TEXT, so the inline branch never sees one. */}
        {rows.map((row) => row.state === 'not_available' && row.text !== null
          ? <UnavailableSegmentRow buyerMode={buyerMode} key={`${row.profile_path}:${row.segment_id}`} reason={row.text} row={row} />
          : <DtRow key={`${row.profile_path}:${row.segment_id}`} className={row.state === 'value' ? undefined : 'na-row'}>
            <DtRowHeader><SegmentName row={row} /></DtRowHeader>
            <DtCell label={metricLabels(buyerMode)[0]}>{buyerMode === 'all' ? row.segment_buyer_share?.text ?? row.text : groupedCount(row.segment_buyers_new_to_brand_text)}</DtCell>
            <DtCell label="Share of reached shoppers">{row.segment_exposed_share?.text ?? row.text}</DtCell>
            <DtCell label="Index">{row.segment_index === null ? row.text : displayFigure('segment_index', row.segment_index, 'ratio', null).text}</DtCell>
            {buyerMode === 'all' ? <DtCell label="Shares side by side"><PairedBars buyer={row.segment_buyer_share} reached={row.segment_exposed_share} /></DtCell> : null}
          </DtRow>)}
        {unavailable.map((row) => <UnavailableSegmentRow buyerMode={buyerMode} key={`${row.profile_path}:${row.segment_id}`} reason={row.text} row={row} />)}
      </DtBody>
    </DataTable>
    </div>
    <Pager label={delivery.label} page={page} pageCount={delivery.page_count} pagingText={pagingText} onPage={toPage} />
    <p className="footnote">{view.display_minimum_text}</p>
  </div>;
}

function Insights({ deliveries }: { readonly deliveries: readonly AudienceDelivery[] }) {
  return <section className="section audience-insights" aria-labelledby="audience-insights-heading">
    <h2 id="audience-insights-heading">Insights</h2>
    <div className="insights">{deliveries.map((delivery) => {
      const slot = delivery.insights[0]!;
      return <article className="insight" key={delivery.profile_path}>
        <span className="kind">Over-index · {delivery.label}</span>
        <p>{slot.text ?? slot.absence_reason}</p>
        {slot.evidence_line === null ? null : <p className="footnote">{slot.evidence_line}</p>}
      </article>;
    })}</div>
  </section>;
}

function ProfileSection({ announce, market, parts, resume }: { readonly announce: (message: string) => void; readonly market: AudienceMarket; readonly parts: PartContext; readonly resume: AudiencesResume | null }) {
  const [profile, setProfile] = useState<AudienceViewId>(market.default_view);
  const [buyerMode, setBuyerMode] = useState<'all' | 'new'>('all');
  const selected = market.views.find((view) => view.view === profile)!;
  const paid = market.views[0];
  return <>
    {paid.paid_state === 'not_observed' ? <section className="callout paid" role="note"><h2>Available with the paid feature</h2><p>{paid.callout}</p><p className="small">DSP-exposed buyers are shown below instead.</p><p className="small">{PAID_FEATURE_NEXT_STEP}</p></section> : null}
    <section className="card section audience-profile" aria-labelledby="audience-profile-heading">
      <div className="section-head"><h2 id="audience-profile-heading">Segment profile</h2></div>
      <fieldset className="datectl"><legend>Profile</legend><div className="seg compact">
        {market.views.map((view) => <label className="seg-opt" key={view.view}><input type="radio" name="audience-profile" checked={profile === view.view} onChange={() => setProfile(view.view)} /><span className="l">{view.label}</span></label>)}
      </div></fieldset>
      <fieldset className="datectl"><legend>Buyers</legend><div className="seg compact">
        <label className="seg-opt"><input type="radio" name="audience-buyers" checked={buyerMode === 'all'} onChange={() => setBuyerMode('all')} /><span className="l">All buyers</span></label>
        <label className="seg-opt"><input type="radio" name="audience-buyers" checked={buyerMode === 'new'} onChange={() => setBuyerMode('new')} /><span className="l">New-to-brand buyers</span></label>
      </div></fieldset>
      {selected.paid_state === 'not_observed' ? <div className="callout audience-state" role="status"><p>{selected.callout}</p><p className="small">{PAID_FEATURE_NEXT_STEP}</p></div> : selected.deliveries.map((delivery) => <SegmentTable announce={announce} buyerMode={buyerMode} delivery={delivery} key={`${parts.revision}:${delivery.profile_path}`} parts={resume?.kind === 'profile' && resume.path === delivery.profile_path ? { ...parts, mismatches: resume.mismatches } : parts} resumePage={resume?.kind === 'profile' && resume.path === delivery.profile_path && resume.page < delivery.page_count ? resume.page : 0} view={selected} />)}
      <p className="footnote">{selected.segment_ids_sentence}</p>
      <p className="footnote">{selected.buyer_label}</p>
    </section>
    <Insights deliveries={selected.deliveries} />
  </>;
}

function CampaignTable({ market, page, focusRef }: { readonly market: AudienceMarket; readonly page: AudienceCampaignPage; readonly focusRef: React.RefObject<HTMLDivElement | null> }) {
  if (page.state === 'withdrawn') return <p className="state-note">{page.text}</p>;
  return <div ref={focusRef} tabIndex={-1} aria-label={`${page.campaign_label}, by segment`}>
    <DataTable title={`${page.campaign_label}, by segment`} note={market.campaign_audiences.basis_label} nameWithNote>
      <DtHead><DtRow><DtColumn className="l">Segment</DtColumn><DtColumn>Impressions</DtColumn><DtColumn>CPM</DtColumn><DtColumn>ROAS</DtColumn></DtRow></DtHead>
      <DtBody>{page.rows.map((row) => <DtRow key={`${page.campaign_ref}:${row.segment_id}`}>
        <DtRowHeader><SegmentName row={row} /></DtRowHeader>
        <DtCell label="Impressions">{groupedCount(row.segment_impressions_text)}</DtCell>
        <DtCell label="CPM">{row.segment_cpm?.text ?? row.cpm_text}</DtCell>
        <DtCell label="ROAS">{row.segment_roas_attributed === null ? row.roas_text : displayFigure('segment_roas_attributed', row.segment_roas_attributed, 'ratio', null).text}</DtCell>
      </DtRow>)}</DtBody>
    </DataTable>
  </div>;
}

/**
 * Every DSP campaign, reachable one at a time (ADR-141): a combobox over the server's index,
 * one fetch of a campaign's page 0 on selection, at most one prefetch of the open campaign's
 * next page, and nothing fetched in bulk, on hover or for another campaign.
 */
/** The campaign to open by label: a pending re-request, or the choice carried across a refresh. */
interface WantedCampaign {
  readonly label: string;
  readonly mismatches: number;
}

function CampaignSection({ market, parts, wanted, announce, onChoose }: {
  readonly market: AudienceMarket;
  readonly parts: PartContext;
  readonly wanted: WantedCampaign | null;
  readonly announce: (message: string) => void;
  readonly onChoose: (label: string | null) => void;
}) {
  const campaigns = market.campaign_audiences;
  const { load: loadShards, shards } = useIndexShards(parts, campaigns.index_shards);
  const entries = useMemo(() => [...campaigns.index, ...(shards ?? [])], [campaigns.index, shards]);
  const [choice, setChoice] = useState<WantedCampaign | null>(wanted);
  // A choice resolves by label against this parent's index; the later shards load first when needed.
  const selected = choice === null ? null : entries.find((entry) => entry.campaign_label === choice.label) ?? null;
  const resolving = choice !== null && selected === null && shards === null && campaigns.index_shards > 1;
  const gone = choice !== null && selected === null && !resolving;
  const [page, setPage] = useState(0);
  const focusRef = useRef<HTMLDivElement>(null);
  const moved = useRef(false);
  const context = useMemo(() => ({ ...parts, mismatches: choice?.mismatches ?? 0 }), [choice, parts]);
  const onMismatch = useCallback((mismatches: number): AudiencesResume => ({ kind: 'campaign', label: choice?.label ?? '', mismatches }), [choice]);
  const { request, state } = usePart<AudienceCampaignPage>(context, onMismatch);
  useEffect(() => {
    if (resolving) void loadShards();
  }, [loadShards, resolving]);
  useEffect(() => {
    // The chosen campaign isn't in this parent: the selector clears and the status says so.
    if (!gone) return;
    announce(AUD_CAMPAIGN_GONE);
    onChoose(null);
  }, [announce, gone, onChoose]);
  useEffect(() => {
    if (selected === null || selected.state !== 'available') return;
    void request({ kind: 'campaign', page, ref: selected.campaign_ref });
  }, [page, request, selected]);
  useEffect(() => {
    // After the open page renders, prefetch at most its next page.
    if (state.kind !== 'ready' || selected === null || state.part.campaign_ref !== selected.campaign_ref || state.part.page !== page) return;
    if (moved.current) {
      focusRef.current?.focus();
      if (state.part.paging_text !== null) announce(state.part.paging_text);
    }
    if (page + 1 < state.part.page_count) void request({ kind: 'campaign', page: page + 1, ref: selected.campaign_ref }, { prefetch: true });
  }, [announce, page, request, selected, state]);
  const choose = (entry: CampaignIndexEntry) => {
    moved.current = false;
    setPage(0);
    setChoice({ label: entry.campaign_label, mismatches: 0 });
    onChoose(entry.campaign_label);
  };
  const toPage = (next: number) => { moved.current = true; setPage(next); };
  const open = selected;
  return <details className="about audience-campaigns" data-slot="au-campaigns" open={wanted === null ? undefined : true}>
    <summary>{AUDIENCES_CAMPAIGNS_SUMMARY}</summary>
    <section className="card section" aria-labelledby="audience-campaign-heading">
    <h2 id="audience-campaign-heading">DSP campaigns by segment</h2>
    <p className="sec-line">{campaigns.limit_text}</p>
    {campaigns.pending_text === null ? null : <p className="state-note" role="status">{campaigns.pending_text}</p>}
    {campaigns.refused_text === null ? null : <p className="state-note">{campaigns.refused_text}</p>}
    {entries.length === 0 ? null : <CampaignCombobox key={resolving ? 'resolving' : 'resolved'} entries={entries} selected={selected} onFocusFirst={() => { if (campaigns.index_shards > 1) void loadShards(); }} onSelect={choose} />}
    {open === null ? null : open.state === 'refused' || open.state === 'withdrawn'
      ? <p className="state-note">{open.text}</p>
      : state.kind === 'failed'
        ? <PartFailed onRetry={() => void request({ kind: 'campaign', page, ref: open.campaign_ref })} />
        : state.kind === 'ready' && state.part.campaign_ref === open.campaign_ref && state.part.page === page
          ? <><CampaignTable focusRef={focusRef} market={market} page={state.part} /><Pager label={open.campaign_label} page={page} pageCount={state.part.page_count} pagingText={state.part.paging_text} onPage={toPage} /></>
          : <div className="skel card" role="status" aria-label={`Loading ${open.campaign_label}`} />}
    <p className="footnote">{campaigns.segment_ids_sentence}</p>
    </section>
  </details>;
}

function Evidence({ artifact }: { readonly artifact: AudienceProfileArtifact }) {
  if (artifact.evidence_status.slots.length === 0) return null;
  return <section className="section audience-evidence" aria-labelledby="audience-evidence-heading"><h2 id="audience-evidence-heading">Evidence status</h2>{artifact.evidence_status.slots.map((slot) => <article key={slot.slot_label}><h3>{slot.slot_label}</h3><p>{slot.role_label}</p><p>{slot.reason_text}</p>{slot.recovery_text === null ? null : <p>{slot.recovery_text}</p>}</article>)}</section>;
}

function MonthScope({ artifact }: { readonly artifact: AudienceProfileArtifact }) {
  return <p className="scope-band">Showing {artifact.month.label} (Latest measured month).</p>;
}

function Outcome({ artifact, parts, resume, carried, announce, onChoose }: {
  readonly artifact: AudienceProfileArtifact;
  readonly parts: PartContext;
  readonly resume: AudiencesResume | null;
  readonly carried: string | null;
  readonly announce: (message: string) => void;
  readonly onChoose: (label: string | null) => void;
}) {
  const market = artifact.markets[0];
  const campaigns = market.campaign_audiences;
  const wanted = resume?.kind === 'campaign' ? { label: resume.label, mismatches: resume.mismatches } : carried === null ? null : { label: carried, mismatches: 0 };
  // A new publication remounts the section, so its parts and shards never outlive their parent.
  const campaignSection = <CampaignSection announce={announce} key={artifact.revision} market={market} onChoose={onChoose} parts={parts} wanted={wanted} />;
  if (market.outcome !== 'complete') {
    const absence = market.views.flatMap((view) => view.deliveries).flatMap((delivery) => [delivery.period_text, delivery.total_text, delivery.insights[0]?.absence_reason]).find((value): value is string => value !== null && value !== undefined);
    // Campaign evidence is published on its own: a profile absence never hides campaign rows.
    return <><section className="callout" role="status"><h2>Audiences</h2><p>{absence}</p></section>{campaigns.campaign_count === 0 && campaigns.refused_text === null ? null : <><MonthScope artifact={artifact} />{campaignSection}</>}</>;
  }
  return <>
    <MonthScope artifact={artifact} />
    <AnswerSentence answer={audiencesAnswer(artifact)} />
    <p className="callout au-activate" data-slot="au-activate">{AUDIENCES_ACTIVATE_LINE}</p>
    <ProfileSection announce={announce} market={market} parts={parts} resume={resume} />
    {campaignSection}
  </>;
}

export function AudiencesView({ artifact, session = '', reload = () => undefined }: {
  readonly artifact: AudienceProfileArtifact;
  /** The page's session, for its part fetches. */
  readonly session?: string;
  /** Reload the parent after a part named a newer publication. */
  readonly reload?: () => void;
}) {
  // A pending re-request applies once, to this freshly loaded parent (design 01 §7).
  const [resume] = useState(takeAudiencesResume);
  const [status, setStatus] = useState(resume === null ? '' : AUD_PAGE_REVISION_CHANGED);
  const reloadRef = useRef(reload);
  useEffect(() => { reloadRef.current = reload; }, [reload]);
  const stableReload = useCallback(() => reloadRef.current(), []);
  const parts = useMemo<PartContext>(() => ({ mismatches: 0, reload: stableReload, revision: artifact.revision, session }), [artifact.revision, stableReload, session]);
  // The chosen campaign, by label, so an in-place parent refresh keeps it when it is still listed.
  const [carried, setCarried] = useState<string | null>(null);
  // The page declares exactly one boundary (pages.yaml), so every outcome carries it.
  return <div className="audiences-view">
    <p className="sr-only" role="status" aria-live="polite">{status}</p>
    <Outcome announce={setStatus} artifact={artifact} carried={carried} onChoose={setCarried} parts={parts} resume={resume} />
    <Evidence artifact={artifact} />
    <details className="about"><summary>About these figures</summary><div className="body">{artifact.boundary.map((line) => <p key={line}>{line}</p>)}</div></details>
  </div>;
}
