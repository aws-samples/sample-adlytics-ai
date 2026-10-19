// SPDX-License-Identifier: MIT-0
'use client';

/**
 * The "Coverage for this window" sheet (ADR-119, ia §7.6): the one sheet every tab's "See why"
 * opens. It renders only the page's `EvidenceStatus.v2` block, verbatim: the figure and role
 * labels, the missing dates, the reason and the recovery are all the server's. The browser keeps
 * no code-to-label map and writes no coverage copy.
 */

import type { RefObject } from 'react';
import type { EvidenceStatusV2 } from '@/lib/evidence-status';
import { formatIsoDateRange } from '@/lib/exact-format';
import { DataTable, DtBody, DtCell, DtColumn, DtHead, DtRow, DtRowHeader } from './DataTable';
import { Dialog } from './Dialog';

export function EvidenceStatusSheet({ onClose, opener, status, subject }: {
  readonly onClose: () => void;
  /** The "See why" control that opened the sheet; focus returns to it on close. */
  readonly opener?: RefObject<HTMLElement | null> | undefined;
  readonly status: EvidenceStatusV2;
  /** What the window covers: the tab, or the campaign on campaign detail. */
  readonly subject: string;
}) {
  const headingId = 'coverage-sheet-heading';
  return <Dialog
    className="focus-dialog evidence-status"
    closeLabel="Close coverage"
    labelledBy={headingId}
    onClose={onClose}
    opener={opener}
    surface="evidence-status"
    header={<>
      <h2 id={headingId}>Coverage for this window</h2>
      <p className="defline">{subject} · {formatIsoDateRange(status.window.start, status.window.end)}</p>
    </>}
  >
    <p className="coverage-summary">{status.summary}</p>
    <DataTable className="coverage-table" title="What each figure needs">
      <DtHead><DtRow>
        <DtColumn>Figure</DtColumn>
        <DtColumn className="l">Role</DtColumn>
        <DtColumn className="l">Missing dates</DtColumn>
        <DtColumn className="l">What happens next</DtColumn>
      </DtRow></DtHead>
      <DtBody>{status.entries.map((entry) => <DtRow key={`${entry.slot}:${entry.role}`}>
        <DtRowHeader>{entry.label}</DtRowHeader>
        <DtCell className="l" label="Role">{entry.role_label}</DtCell>
        <DtCell className="l" label="Missing dates">{entry.missing_text}</DtCell>
        <DtCell className="l" label="What happens next">
          <span>{entry.reason} {entry.recovery}</span>
        </DtCell>
      </DtRow>)}</DtBody>
    </DataTable>
  </Dialog>;
}
