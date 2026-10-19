// SPDX-License-Identifier: MIT-0
'use client';
/**
 * Operations › Data pipeline status (ADR-115 decision 8, ia §6.5, `mockups/pipeline-status.html`).
 *
 * Dark: the route isn't in `presentation/navigation.yaml` and nothing is exported at
 * `/admin/status/`, so no session can reach this view. It is listed, and gets its route file and
 * feed read, once the platform operations serving fixes it reads from are designed and approved:
 * the summary semantics fix, the currency read and the flow index route
 * (`docs/ui/platform-operations/README.md`, open decision 2).
 *
 * Every value in the feed table is server-written text. The browser judges no timeliness and
 * computes no duration (platform operations, "Health is declared, never browser-inferred").
 */
import Link from 'next/link';

import { rootTab } from '@/lib/route-manifest';

import { DataTable, DtBody, DtCell, DtColumn, DtHead, DtRow, DtRowHeader } from './DataTable';
import { StateCallout } from './StatePanels';

/** Which of the page's three states a session is in, from its `/me` fields alone. */
export type PipelineStatusAccess = 'not-provisioned' | 'no-access' | 'granted';

export function pipelineStatusAccess(session: {
  readonly canReadOperationalStatus: boolean;
  readonly operationalStatusProvisioned: boolean;
}): PipelineStatusAccess {
  if (!session.operationalStatusProvisioned) return 'not-provisioned';
  return session.canReadOperationalStatus ? 'granted' : 'no-access';
}

/** A server-written status pill: its words and whether it calls for attention. */
export interface FeedPill {
  readonly label: string;
  readonly tone: 'neutral' | 'warn';
}

/**
 * One feed and query cadence (mockup M3-3). The render input for the feed read's serving, which
 * has no wire contract yet; its decoder ships with that read.
 */
export interface FeedStatusRow {
  readonly feed: string;
  readonly data_through: string;
  readonly last_landed: string;
  readonly timeliness: FeedPill;
  /** A gap in a figure: its pill ("1 gap") and the sentence naming the day, figure and pages. */
  readonly gap: { readonly pill: string; readonly detail: string } | null;
  readonly pages: string;
}

export interface FeedStatusTable {
  /** "Checked {date, time} UTC", server-written. */
  readonly checked: string;
  readonly rows: readonly FeedStatusRow[];
}

const COLUMNS = ['Feed', 'Data through', 'Last landed', 'Status', 'Pages it feeds'] as const;

function GoToRoot() {
  return <Link className="btn primary" href="/">Go to {rootTab().label}</Link>;
}

function FeedTable({ table }: { readonly table: FeedStatusTable }) {
  return (
    <div className="pipeline-status">
      <DataTable className="text" title="Data feeds" note={table.checked}>
        <DtHead>
          <DtRow>{COLUMNS.map((column) => <DtColumn key={column}>{column}</DtColumn>)}</DtRow>
        </DtHead>
        <DtBody>
          {table.rows.map((row) => (
            <DtRow key={row.feed}>
              <DtRowHeader>{row.feed}</DtRowHeader>
              <DtCell label="Data through" className="nw">{row.data_through}</DtCell>
              <DtCell label="Last landed" className="nw">{row.last_landed}</DtCell>
              <DtCell label="Status">
                <span className="cv-stack">
                  <span className="pills">
                    <span className={row.timeliness.tone === 'warn' ? 'pill warn' : 'pill'}>{row.timeliness.label}</span>
                    {row.gap === null ? null : <span className="pill warn">{row.gap.pill}</span>}
                  </span>
                  {row.gap === null ? null : <span className="gap">{row.gap.detail}</span>}
                </span>
              </DtCell>
              <DtCell label="Pages it feeds">{row.pages}</DtCell>
            </DtRow>
          ))}
        </DtBody>
      </DataTable>
      <p className="later">More detail per feed (events, retries) arrives in later slices of the platform operations design.</p>
    </div>
  );
}

/** The page body under the shell's hub header, for one of the three states. */
export function DataPipelineStatusView(
  props: { readonly access: 'not-provisioned' | 'no-access' } | { readonly access: 'granted'; readonly table: FeedStatusTable },
) {
  if (props.access === 'granted') return <FeedTable table={props.table} />;
  if (props.access === 'not-provisioned') {
    return (
      <StateCallout title="Data pipeline status isn't set up in this deployment." actions={<GoToRoot />}>
        This page reads an operations API that this deployment doesn&apos;t include.
      </StateCallout>
    );
  }
  return (
    <StateCallout title="You don't have access to this page" actions={<GoToRoot />}>
      Data pipeline status is for operators in the operations group.
    </StateCallout>
  );
}
