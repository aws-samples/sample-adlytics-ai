// SPDX-License-Identifier: MIT-0
'use client';
import type { ReactNode } from 'react';
import { formatIsoDate } from '@/lib/exact-format';
import {
  freshnessPillCopy,
  staleCauseLine,
  staleShownLine,
  statusLines,
  weekStartCopy,
  type DemandForecastStatus,
  type Freshness,
  type Horizon,
  type Provenance,
} from '@/lib/demand-forecast';
import { InfoTip } from './InfoTip';
import { StateCallout } from './StatePanels';

/**
 * The scope pieces the demand forecast list and product views share (design S1 to S3, S8): the
 * scope card, the freshness pill with its status pop-up, the week control and the out-of-date
 * banner. Every date is a served one; nothing here computes a figure.
 */

export type Loaded<T> = { readonly state: 'loading' } | { readonly state: 'ready'; readonly value: T } | { readonly state: 'failed' };

export function ScopeCard({ children }: { readonly children: ReactNode }) {
  return <section className="band df-scope" aria-label="Forecast scope">{children}</section>;
}

/** The status the schedule lines read: null while it is loading or failed. */
export function readyStatus(status: Loaded<DemandForecastStatus>): DemandForecastStatus | null {
  return status.state === 'ready' ? status.value : null;
}

/** The freshness pill (L-FRESH-*) and its pop-up (I-FRESH), which carries every status field (D4). */
export function FreshnessPill({ freshness, onRetryStatus, provenance, status }: {
  readonly freshness: Freshness;
  readonly onRetryStatus: () => void;
  readonly provenance: Provenance;
  readonly status: Loaded<DemandForecastStatus>;
}) {
  if (freshness.state === 'unavailable') return null;
  const lines = statusLines({ freshness, provenance }, readyStatus(status));
  return (
    <span className="df-fresh">
      <span className={freshness.state === 'fresh' ? 'pill good' : 'pill warn'}>{freshnessPillCopy[freshness.state](provenance.input_end)}</span>
      <InfoTip title="About this forecast" label="About this forecast">
        <span className="line">{lines.made}</span>
        {status.state === 'loading' ? <span className="skel h-line" aria-hidden="true"/> : null}
        {status.state === 'failed' ? (
          <span className="line">
            The update schedule can&apos;t be loaded right now.{' '}
            <button type="button" className="btn link" onClick={onRetryStatus}>Try again</button>
          </span>
        ) : null}
        {lines.schedule?.map((line) => <span className="line" key={line}>{line}</span>)}
        <span className="line">{lines.rule}</span>
      </InfoTip>
    </span>
  );
}

/** The week control (L-WEEKS) and the served window it shows (L-WINDOW). */
export function WeekControl({ horizon, onViewWeeks, viewWeeks }: {
  readonly horizon: Horizon;
  readonly onViewWeeks: (weeks: number) => void;
  readonly viewWeeks: number;
}) {
  const first = horizon.weeks[0];
  return (
    <div className="df-weeks">
      <div className="seg" role="group" aria-label="Weeks to show">
        {horizon.views.map((view) => (
          <button key={view} type="button" aria-pressed={view === viewWeeks} onClick={() => onViewWeeks(view)}>{view} weeks</button>
        ))}
      </div>
      {first === undefined ? null : (
        <span className="df-window">{viewWeeks} weeks starting {formatIsoDate(first)} · {weekStartCopy[horizon.week_starts_on]}</span>
      )}
    </div>
  );
}

/** The out-of-date banner (E-STALE): one line per served cause, then what the page shows. */
export function StaleBanner({ freshness, provenance, status }: {
  readonly freshness: Freshness;
  readonly provenance: Provenance;
  readonly status: DemandForecastStatus | null;
}) {
  return (
    <StateCallout tone="warn" title="This forecast is out of date">
      {freshness.causes.map((cause) => (
        <span className="df-line" data-cause={cause} key={cause}>{staleCauseLine(cause, freshness, status)}</span>
      ))}
      <span className="df-line">{staleShownLine(provenance, status)}</span>
    </StateCallout>
  );
}
