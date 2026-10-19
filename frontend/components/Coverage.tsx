// SPDX-License-Identifier: MIT-0
'use client';

/**
 * Typed absence for a partly reported figure (ADR-118 decision 5, ADR-119), one rule for tiles
 * and table cells. Where the coverage block withholds a figure's change, its place reads the
 * server's "{n} day(s) not yet sent." or "{n} partial day(s)." with "See why: {figure}", and the
 * basis names the covered days. Every "See why" on a page opens its one coverage sheet.
 */

import { createContext, useContext, useMemo, useRef, useState, type ReactNode } from 'react';
import type { Change } from '@/lib/change';
import { coverageEntry, type EvidenceStatusEntry, type EvidenceStatusV2, isWithheld } from '@/lib/evidence-status';
import { ChangeChip } from './ChartPrimitives';
import { EvidenceStatusSheet } from './EvidenceStatusSheet';

interface CoverageControl {
  readonly open: (opener: HTMLButtonElement) => void;
  readonly status: EvidenceStatusV2 | null;
}

const CoverageContext = createContext<CoverageControl>({ open: () => undefined, status: null });

/** Serve one page's coverage block to its figures, and host the sheet they open. */
export function CoverageProvider({ children, status, subject }: {
  readonly children: ReactNode;
  readonly status: EvidenceStatusV2 | null;
  readonly subject: string;
}) {
  const [open, setOpen] = useState(false);
  const opener = useRef<HTMLElement | null>(null);
  const control = useMemo<CoverageControl>(() => ({
    open: (source) => { opener.current = source; setOpen(true); },
    status,
  }), [status]);
  return <CoverageContext.Provider value={control}>
    {children}
    {open && status !== null ? <EvidenceStatusSheet
      onClose={() => setOpen(false)}
      opener={opener}
      status={status}
      subject={subject}
    /> : null}
  </CoverageContext.Provider>;
}

/** Open this page's coverage sheet from a control, or null when the page serves no block. */
export function useCoverageOpen(): ((opener: HTMLButtonElement) => void) | null {
  const { open, status } = useContext(CoverageContext);
  return status === null ? null : open;
}

/**
 * The withheld entry for one figure, or null when its change shows. `roles` lists the entries
 * that can withhold it, this window's first; only a figure whose block lists a prior-window
 * entry (the DSP viewable rate) passes more than one.
 */
export function useWithheld(slot: string, roles: readonly string[] = ['current']): EvidenceStatusEntry | null {
  const { status } = useContext(CoverageContext);
  for (const role of roles) {
    const entry = coverageEntry(status, slot, role);
    if (isWithheld(entry)) return entry;
  }
  return null;
}

/** "See why", named for its figure (M5-12), opening the page's coverage sheet. */
export function SeeWhy({ figure }: { readonly figure: string }) {
  const { open } = useContext(CoverageContext);
  return <button
    type="button"
    className="see-why-link"
    aria-haspopup="dialog"
    aria-label={`See why: ${figure}`}
    onClick={(event) => open(event.currentTarget)}
  >See why</button>;
}

/**
 * A figure's change: the server chip, or in its place the server's absence text and "See why"
 * when a missing or partial day withholds it. `figure` names the "See why"; it defaults to the
 * server's label for the figure.
 */
export function FigureChange({ change, figure, roles, slot }: {
  readonly change: Change | null;
  readonly figure?: string;
  readonly roles?: readonly string[];
  readonly slot: string;
}) {
  const entry = useWithheld(slot, roles);
  if (entry === null) return <ChangeChip change={change} />;
  return <span className="chg-absent">{entry.absence_text} <SeeWhy figure={figure ?? entry.label} /></span>;
}

/**
 * A tile's change slot: the server chip, or nothing while a missing or partial day withholds
 * it. The tile then reads the absence text on its own line under the basis (`TileAbsence`).
 */
export function TileChange({ change, slot }: { readonly change: Change | null; readonly slot: string }) {
  return useWithheld(slot) === null ? <ChangeChip change={change} /> : null;
}

/** A tile's basis line naming the covered days, only while the figure is partly reported. */
export function CoverageBasis({ slot }: { readonly slot: string }) {
  const entry = useWithheld(slot);
  return entry === null ? null : <p className="basis">{entry.coverage_text}</p>;
}

/** A tile's withheld-change line, after its basis as the mockups show: absence text and "See why". */
export function TileAbsence({ figure, slot }: { readonly figure?: string; readonly slot: string }) {
  const entry = useWithheld(slot);
  return entry === null
    ? null
    : <p className="chg-absent tile-absence">{entry.absence_text} <SeeWhy figure={figure ?? entry.label} /></p>;
}

/** The coverage part of a table column's basis ("6 of 7 days"), or null while the figure is complete. */
export function useCoverageText(): (slot: string) => string | null {
  const { status } = useContext(CoverageContext);
  return (slot) => coverageEntry(status, slot)?.coverage_text ?? null;
}
