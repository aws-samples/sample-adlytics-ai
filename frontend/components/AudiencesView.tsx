// SPDX-License-Identifier: MIT-0
'use client';

import { audiencesAnswer } from '@/lib/audiences';
import { AnswerSentence } from './AnswerSentence';
import { PAID_FEATURE_NEXT_STEP } from './PageAbsence';
import { displayFigure, formatCount } from '@/lib/exact-format';
import { useId, useMemo, useState } from 'react';

import type { AudienceShare } from '@/lib/audiences';
import type { AudienceDelivery, AudienceMarket, AudienceProfileArtifact, AudienceSegmentNames, AudienceView, AudienceViewId } from '@/lib/audiences';
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

function SegmentTable({ buyerMode, delivery, view }: {
  readonly buyerMode: 'all' | 'new';
  readonly delivery: AudienceDelivery;
  readonly view: AudienceView;
}) {
  const rows = view.rows.filter((row) => row.profile_path === delivery.profile_path);
  const unavailable = view.not_available.filter((row) => row.profile_path === delivery.profile_path);
  if (!delivery.returned) return <div className="callout audience-state" role="status"><p>{delivery.period_text ?? delivery.insights[0]?.absence_reason}</p></div>;
  return <div className="audience-delivery">
    <h3>{delivery.label}</h3>
    {delivery.period_text === null ? null : <p className="state-note">{delivery.period_text}</p>}
    {delivery.total_text === null ? null : <p className="state-note">{delivery.total_text}</p>}
    {delivery.first_month_note === null ? null : <p className="sec-line">{delivery.first_month_note}</p>}
    {delivery.roster_note === null ? null : <p className="sec-line">{delivery.roster_note}</p>}
    {buyerMode === 'all' ? <p className="au-legend" aria-hidden="true"><span className="au-bar buyers" /> Share of buyers <span className="au-bar reached" /> Share of reached shoppers</p> : null}
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

function ProfileSection({ market }: { readonly market: AudienceMarket }) {
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
      {selected.paid_state === 'not_observed' ? <div className="callout audience-state" role="status"><p>{selected.callout}</p><p className="small">{PAID_FEATURE_NEXT_STEP}</p></div> : selected.deliveries.map((delivery) => <SegmentTable buyerMode={buyerMode} delivery={delivery} key={delivery.profile_path} view={selected} />)}
      <p className="footnote">{selected.segment_ids_sentence}</p>
      <p className="footnote">{selected.buyer_label}</p>
    </section>
    <Insights deliveries={selected.deliveries} />
  </>;
}

function CampaignSection({ market }: { readonly market: AudienceMarket }) {
  const campaigns = market.campaign_audiences;
  const options = useMemo(() => Array.from(new Map(campaigns.rows.map((row) => [row.campaign_ref, row.campaign_label])).entries()), [campaigns.rows]);
  const [chosen, setSelected] = useState(options[0]?.[0] ?? null);
  // A refreshed artifact can drop the campaign chosen before it; the reader then sees its first campaign.
  const selected = options.some(([key]) => key === chosen) ? chosen : options[0]?.[0] ?? null;
  const rows = selected === null ? [] : campaigns.rows.filter((row) => row.campaign_ref === selected);
  const title = options.find(([key]) => key === selected)?.[1];
  return <details className="about audience-campaigns" data-slot="au-campaigns">
    <summary>{AUDIENCES_CAMPAIGNS_SUMMARY}</summary>
    <section className="card section" aria-labelledby="audience-campaign-heading">
    <h2 id="audience-campaign-heading">DSP campaigns by segment</h2>
    <p className="sec-line">{campaigns.limit_text}</p>
    {campaigns.pending_text === null ? null : <p className="state-note" role="status">{campaigns.pending_text}</p>}
    {campaigns.shown_text === null ? null : <p className="sec-line">{campaigns.shown_text}</p>}
    {options.length < 2 ? null : <label className="select-wrap">Campaign<select value={selected ?? ''} onChange={(event) => setSelected(event.currentTarget.value)}>{options.map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label>}
    {title === undefined ? null : <DataTable title={`${title}, by segment`} note={market.campaign_audiences.basis_label} nameWithNote>
      <DtHead><DtRow><DtColumn className="l">Segment</DtColumn><DtColumn>Impressions</DtColumn><DtColumn>CPM</DtColumn><DtColumn>ROAS</DtColumn></DtRow></DtHead>
      <DtBody>{rows.map((row) => <DtRow key={`${row.campaign_ref}:${row.segment_id}`}>
        <DtRowHeader><SegmentName row={row} /></DtRowHeader>
        <DtCell label="Impressions">{groupedCount(row.segment_impressions_text)}</DtCell>
        <DtCell label="CPM">{row.segment_cpm?.text ?? row.cpm_text}</DtCell>
        <DtCell label="ROAS">{row.segment_roas_attributed === null ? row.roas_text : displayFigure('segment_roas_attributed', row.segment_roas_attributed, 'ratio', null).text}</DtCell>
      </DtRow>)}</DtBody>
    </DataTable>}
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

function Outcome({ artifact }: { readonly artifact: AudienceProfileArtifact }) {
  const market = artifact.markets[0];
  if (market.outcome !== 'complete') {
    const absence = market.views.flatMap((view) => view.deliveries).flatMap((delivery) => [delivery.period_text, delivery.total_text, delivery.insights[0]?.absence_reason]).find((value): value is string => value !== null && value !== undefined);
    // Campaign evidence is published on its own: a profile absence never hides campaign rows.
    return <><section className="callout" role="status"><h2>Audiences</h2><p>{absence}</p></section>{market.campaign_audiences.rows.length === 0 ? null : <><MonthScope artifact={artifact} /><CampaignSection market={market} /></>}</>;
  }
  return <>
    <MonthScope artifact={artifact} />
    <AnswerSentence answer={audiencesAnswer(artifact)} />
    <p className="callout au-activate" data-slot="au-activate">{AUDIENCES_ACTIVATE_LINE}</p>
    <ProfileSection market={market} />
    <CampaignSection market={market} />
  </>;
}

export function AudiencesView({ artifact }: { readonly artifact: AudienceProfileArtifact }) {
  // The page declares exactly one boundary (pages.yaml), so every outcome carries it.
  return <div className="audiences-view">
    <Outcome artifact={artifact} />
    <Evidence artifact={artifact} />
    <details className="about"><summary>About these figures</summary><div className="body">{artifact.boundary.map((line) => <p key={line}>{line}</p>)}</div></details>
  </div>;
}
