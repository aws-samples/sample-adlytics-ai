// SPDX-License-Identifier: MIT-0
'use client';

/**
 * The "Coverage for this window" sheet (ADR-119, ia §7.6): the one sheet every tab's "See why"
 * opens. It renders only the page's `EvidenceStatus.v2` block, verbatim: the figure and role
 * labels, the missing dates, the reason and the recovery are all the server's. The browser keeps
 * no code-to-label map and writes no coverage copy.
 */

import { createContext, useContext, type RefObject } from 'react';
import type { EvidenceStatusV2 } from '@/lib/evidence-status';
import { formatIsoDateRange, formatIsoInstant } from '@/lib/exact-format';
import type { PageGenerationV1 } from '@/lib/presentation-v2';

/**
 * The served generation's build time (S6 §2.9), provided by the route controller. T1L-8 / UX sweep
 * fix 4: it reads in the Coverage sheet, not on the page's "Data through" line.
 */
export const PageGenerationContext = createContext<PageGenerationV1 | undefined>(undefined);

/** "Built 2026-08-22 06:12:04 UTC." or "Built before …", or nothing when no generation is served. */
export function BuiltLine() {
  const generation = useContext(PageGenerationContext);
  if (generation === undefined) return null;
  return <p className="built-line" data-slot="pg-built">{'built_at' in generation
    ? `Built ${formatIsoInstant(generation.built_at)}.`
    : `Built before ${formatIsoInstant(generation.built_before)}.`}</p>;
}
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
    <BuiltLine />
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
