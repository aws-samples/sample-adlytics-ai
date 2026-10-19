// SPDX-License-Identifier: MIT-0
/**
 * The one data table (ADR-118 decision 8, ia §8.12). Rows stack below an 800 px main column
 * (a CSS container query, no dependency), so no table scrolls sideways at a verified width.
 * Because the stacked form sets `display: block` on table parts, every part carries its
 * explicit table role, and the scroll region is named by the caption's title alone.
 *
 * Usage: DataTable > DtHead > DtRow > DtColumn, then DtBody > DtRow > DtRowHeader + DtCell.
 */
import { useId, type ReactNode, type ThHTMLAttributes, type TdHTMLAttributes, type HTMLAttributes } from 'react';

export function DataTable({
  captionHidden = false,
  children,
  className,
  nameLeadId,
  nameWithNote = false,
  note,
  title,
}: {
  /** Visually hide the caption when a visible section heading already names the table. */
  readonly captionHidden?: boolean;
  readonly children: ReactNode;
  readonly className?: string;
  /**
   * The id of a visible element whose text leads the region's name, for a page that shows the
   * same table once per card, so each region's name is its own (axe `landmark-unique`).
   */
  readonly nameLeadId?: string | undefined;
  /** Name the region by its title and its note, for the same reason. */
  readonly nameWithNote?: boolean;
  readonly note?: ReactNode;
  readonly title: string;
}) {
  const titleId = useId();
  const noteId = useId();
  const parts = [nameLeadId, titleId, nameWithNote && note !== undefined ? noteId : undefined].filter((part) => part !== undefined);
  // One table on the page keeps the plain title as its name; repeated ones name their own card.
  const name = nameLeadId === undefined && !nameWithNote ? { 'aria-label': title } : { 'aria-labelledby': parts.join(' ') };
  return (
    <div className="dt-wrap stackable" role="region" {...name} tabIndex={0}>
      <table className={className === undefined ? 'dt' : `dt ${className}`} role="table">
        <caption className={captionHidden ? 'sr-only' : undefined}>
          <strong id={titleId}>{title}</strong>
          {note === undefined ? null : <span className="cap-note" id={noteId}>{note}</span>}
        </caption>
        {children}
      </table>
    </div>
  );
}

export function DtHead({ children }: { readonly children: ReactNode }) {
  return <thead role="rowgroup">{children}</thead>;
}

export function DtBody({ children }: { readonly children: ReactNode }) {
  return <tbody role="rowgroup">{children}</tbody>;
}

export function DtRow({ children, ...props }: HTMLAttributes<HTMLTableRowElement>) {
  return <tr role="row" {...props}>{children}</tr>;
}

/** A column header. `basis` is its visible basis line ("6 of 7 days"), under the name. */
export function DtColumn({ basis, children, ...props }: ThHTMLAttributes<HTMLTableCellElement> & { readonly basis?: string | null }) {
  return <th scope="col" role="columnheader" {...props}>
    {children}
    {basis === undefined || basis === null ? null : <span className="basis">{basis}</span>}
  </th>;
}

export function DtRowHeader({ children, ...props }: ThHTMLAttributes<HTMLTableCellElement>) {
  return <th scope="row" role="rowheader" {...props}>{children}</th>;
}

/**
 * The basis line a stacked cell shows under its column name: the basis text and its coverage
 * part. A typed-absence cell ("—") drops the coverage part, so "6 of 7 days" never sits beside
 * "—" (ADR-118 decision 5); the basis text stays.
 */
export function stackedBasis(
  basis: { readonly text: string | null; readonly coverage: string | null },
  absent: boolean,
): string | null {
  const parts = [basis.text, absent ? null : basis.coverage].filter((part): part is string => part !== null);
  return parts.length === 0 ? null : parts.join(' · ');
}

/**
 * A body cell. `label` is its column name, shown beside the value when rows stack. `basis` is
 * the column's basis, shown under the name when rows stack; `absent` marks a typed absence.
 */
export function DtCell({ absent = false, basis, children, className, label, ...props }: TdHTMLAttributes<HTMLTableCellElement> & {
  readonly absent?: boolean;
  readonly basis?: { readonly text: string | null; readonly coverage: string | null };
  readonly label: string;
}) {
  const line = basis === undefined ? null : stackedBasis(basis, absent);
  if (line === null) return <td role="cell" data-label={label} className={className} {...props}>{children}</td>;
  return <td role="cell" data-label={label} className={className === undefined ? 'has-basis' : `${className} has-basis`} {...props}>
    <span className="basis-inline">{line}</span>
    <span className="cv">{children}</span>
  </td>;
}
