// SPDX-License-Identifier: MIT-0
'use client';

import { retentionAnswer } from '@/lib/retention';
import { metricLabel } from '@/lib/glossary';
import { AnswerSentence } from './AnswerSentence';
import { PAID_FEATURE_NEXT_STEP } from './PageAbsence';
import { useState } from 'react';

import type { RetentionCohortArtifact, RetentionEvidenceStatusSlot, RetentionGrid, RetentionGroup, RetentionMarket } from '@/lib/retention';

const GROUPS: readonly RetentionGroup[] = ['dsp_only', 'sponsored_only', 'dsp_and_sponsored'];
const TRIANGLE_LABEL = 'Share of each cohort that bought again, by period after acquisition';

function Triangle({ grid, legend, marketplaceText }: { readonly grid: RetentionGrid; readonly legend: string; readonly marketplaceText: string | null }) {
  const columns = grid.rows.find((row) => row.cells.length > 0)?.cells ?? [];
  const allAbsent = columns.length === 0;
  return <>
    <div className="retention-triangle" tabIndex={0} role="region" aria-label={marketplaceText === null ? TRIANGLE_LABEL : `${TRIANGLE_LABEL} · ${marketplaceText}`}>
      <table className="retention-heat">
        <caption><strong>{TRIANGLE_LABEL}</strong><span>{grid.grid_label}</span></caption>
        <thead><tr><th scope="col">Cohort</th>{allAbsent ? <th scope="col">Status</th> : columns.map((cell) => <th scope="col" key={cell.offset}>{cell.column_label}</th>)}</tr></thead>
        <tbody>{grid.rows.map((row) => {
          const cohortSize = row.cells.find((cell) => cell.state === 'value')?.cohort_size?.text;
          return <tr key={row.cohort}>
          <th scope="row">{row.label}{cohortSize === undefined ? null : <span className="retention-cohort-size">{cohortSize}</span>}</th>
          {row.absent_text !== null ? <td colSpan={Math.max(1, columns.length)}>{row.absent_text}</td> : row.cells.map((cell) => <td className={`retention-cell ${cell.state}`} data-label={cell.column_label} key={cell.offset}>
            {cell.state === 'value'
              ? <span className="retention-value" title={cell.cohort_size?.text}>{cell.rate?.text}</span>
              : <span className="retention-status">{cell.text}</span>}
          </td>)}
        </tr>;
        })}</tbody>
      </table>
    </div>
    <p className="retention-legend"><span className="retention-hatch" aria-hidden="true" />{legend}</p>
  </>;
}

function Curve({ labelledBy, market }: { readonly labelledBy: string; readonly market: RetentionMarket }) {
  return <section className="section retention-curve" aria-labelledby={labelledBy}>
    <h2 id={`curve-${market.marketplace_key}`}>{metricLabel(market.curve.metric, market.curve.label)}</h2>
    <div className="retention-curve-lines">
      {market.curve.lines.map((line) => <article key={line.group}>
        <h3>{line.label}</h3>
        <ol>{line.points.map((point) => <li key={point.offset}><span>{point.label}</span><strong>{point.rate?.text ?? point.text}</strong></li>)}</ol>
      </article>)}
    </div>
  </section>;
}

/** UX sweep fix 7: the grid's heading and its one line on how it differs from the cumulative curve. */
export const RETENTION_GRID_HEADING = 'By cohort: bought again in that period';
export const RETENTION_GRID_NOTE = 'Each cell counts repeat buyers in that period only; the curve above adds up every period so far.';
function Market({ market }: { readonly market: RetentionMarket }) {
  const [group, setGroup] = useState<RetentionGroup>('sponsored_only');
  const [path, setPath] = useState<'standard' | 'paid'>('standard');
  // With two or more markets the server names each one: a visible heading that also leads every
  // section's accessible name, so no two landmarks or headings read alike.
  const head = `market-${market.marketplace_key}`;
  const heading = market.marketplace_text === null ? null : <h2 id={head} className="market-head">{market.marketplace_text}</h2>;
  const named = (id: string) => market.marketplace_text === null ? id : `${head} ${id}`;
  if (market.outcome !== 'complete') return <>{heading}<section className="callout" role="status">
    <h2>Retention</h2><p>{market.absence_text}</p>
  </section></>;
  const selectedIndex = GROUPS.indexOf(group);
  // A refreshed artifact can drop the paid grids chosen before it; the reader then sees the standard path.
  const shownPath = path === 'paid' && market.triangle.paid.length > 0 ? 'paid' : 'standard';
  const grids = shownPath === 'paid' ? market.triangle.paid : market.triangle.standard;
  const selected = grids[selectedIndex]!;
  return <div className="retention-market">
    {heading}
    {/* UX sweep fix 7 (T1L-5): the served scope and grain note read as plain lines, not boxes. */}
    <p className="scope-band">{market.scope_band}{market.grain_note === null ? null : <> {market.grain_note}</>}</p>
    {/* The cumulative curve leads (readout A.8); the grid below is each period on its own. */}
    <Curve labelledBy={named(`curve-${market.marketplace_key}`)} market={market} />
    <section className="section" aria-labelledby={named(`triangle-${market.marketplace_key}`)}>
      <h2 id={`triangle-${market.marketplace_key}`}>{RETENTION_GRID_HEADING}</h2>
      <p className="footnote">{RETENTION_GRID_NOTE}</p>
      {market.path.paid.state === 'observed' ? <fieldset className="retention-switch"><legend>Purchase path</legend>
        <label><input type="radio" name={`path-${market.marketplace_key}`} checked={shownPath === 'standard'} onChange={() => setPath('standard')} />Repeat after an ad</label>
        <label><input type="radio" name={`path-${market.marketplace_key}`} checked={shownPath === 'paid'} onChange={() => setPath('paid')} />Any repeat purchase</label>
      </fieldset> : null}
      <fieldset className="datectl retention-groups"><legend>Acquired through</legend><div className="seg compact">
        {GROUPS.map((id, index) => {
          const label = market.triangle.standard[index]!.label;
          return <label className="seg-opt" key={id}><input type="radio" name={`group-${market.marketplace_key}`} checked={group === id} onChange={() => setGroup(id)} /><span className="l">{label}</span></label>;
        })}
      </div></fieldset>
      <Triangle grid={selected} legend={market.triangle.legend} marketplaceText={market.marketplace_text} />
      {shownPath === 'standard' ? <p className="footnote">{market.triangle.identity_caveat}</p> : null}
    </section>
    {market.path.paid.state === 'not_observed' ? <section className="callout paid"><h2>Available with the paid feature</h2><p>{market.path.paid.callout}</p><p className="small">{PAID_FEATURE_NEXT_STEP}</p></section> : null}
    <section className="section retention-insights" aria-labelledby={named(`insights-${market.marketplace_key}`)}><h2 id={`insights-${market.marketplace_key}`}>Insights</h2>
      {market.insights.map((slot) => <article className="insight" key={slot.id}><span className="kind">Retention shift</span><p>{slot.text ?? slot.absence_reason}</p>{slot.evidence_line === null ? null : <p className="footnote">{slot.evidence_line}</p>}</article>)}
    </section>
  </div>;
}

function EvidenceTrace({ slots }: { readonly slots: readonly RetentionEvidenceStatusSlot[] }) {
  if (slots.length === 0) return null;
  return <section className="section retention-evidence" aria-labelledby="retention-evidence-heading">
    <h2 id="retention-evidence-heading">Evidence status</h2>
    {slots.map((slot) => <article key={slot.marketplace_key}>
      <h3>{slot.slot_label}</h3>
      <p>{slot.role_label}</p>
      <p>{slot.reason_text}</p>
      {slot.recovery_text === null ? null : <p>{slot.recovery_text}</p>}
    </article>)}
  </section>;
}

export function RetentionView({ artifact }: { readonly artifact: RetentionCohortArtifact }) {
  return <div className="retention-view">
    <AnswerSentence answer={retentionAnswer(artifact)} />
    {artifact.markets.map((market) => <Market key={market.marketplace_key} market={market} />)}
    <EvidenceTrace slots={artifact.evidence_status.slots} />
    <details className="about"><summary>About these figures</summary><div className="body">{artifact.boundary.map((item) => <p key={item}>{item}</p>)}</div></details>
  </div>;
}
