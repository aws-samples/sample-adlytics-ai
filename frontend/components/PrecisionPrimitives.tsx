// SPDX-License-Identifier: MIT-0
import type { ReactNode } from 'react';

export type PrecisionTone = 'danger' | 'neutral' | 'success' | 'warning';

export interface PrecisionLabeledValue {
  readonly detail?: ReactNode;
  readonly id?: string;
  readonly label: string;
  readonly value: ReactNode;
}

function LabeledValues({
  className,
  items,
}: {
  readonly className: string;
  readonly items: readonly PrecisionLabeledValue[];
}) {
  if (items.length === 0) return null;
  return (
    <dl className={className}>
      {items.map((item, index) => (
        <div key={item.id ?? `${item.label}-${index}`}>
          <dt>{item.label}</dt>
          <dd>
            {item.value}
            {item.detail === undefined ? null : (
              <span className="precision-labeled-values__detail">{item.detail}</span>
            )}
          </dd>
        </div>
      ))}
    </dl>
  );
}

export interface OutcomeBannerProps {
  readonly facts?: readonly PrecisionLabeledValue[];
  readonly label?: string;
  readonly status: string;
  readonly summary: ReactNode;
  readonly title: string;
  readonly tone?: PrecisionTone;
}

export function OutcomeBanner({
  facts = [],
  label = 'Outcome',
  status,
  summary,
  title,
  tone = 'neutral',
}: OutcomeBannerProps) {
  return (
    <section className="precision-outcome" data-tone={tone} aria-label={label}>
      <p className="precision-eyebrow">{label}</p>
      <div className="precision-outcome__heading">
        <h2>{title}</h2>
        <span className="precision-status" data-tone={tone}>{status}</span>
      </div>
      <div className="precision-outcome__summary">{summary}</div>
      <LabeledValues className="precision-facts" items={facts} />
    </section>
  );
}

export interface PeriodSnapshot {
  readonly facts: readonly PrecisionLabeledValue[];
  readonly label: string;
  readonly outcome: string;
  readonly period: string;
  readonly tone?: PrecisionTone;
}

export interface PeriodRelation {
  readonly description: ReactNode;
  readonly label: string;
  readonly tone?: PrecisionTone;
}

export interface PeriodComparisonStripProps {
  readonly current: PeriodSnapshot;
  readonly heading?: string;
  readonly previous: PeriodSnapshot;
  readonly relation: PeriodRelation;
}

function PeriodSnapshotView({ snapshot }: { readonly snapshot: PeriodSnapshot }) {
  return (
    <article className="precision-period__snapshot" aria-label={snapshot.label}>
      <header className="precision-period__header">
        <div>
          <p>{snapshot.label}</p>
          <h3>{snapshot.period}</h3>
        </div>
        <span className="precision-status" data-tone={snapshot.tone ?? 'neutral'}>
          {snapshot.outcome}
        </span>
      </header>
      <LabeledValues className="precision-period__facts" items={snapshot.facts} />
    </article>
  );
}

export function PeriodComparisonStrip({
  current,
  heading = 'Period comparison',
  previous,
  relation,
}: PeriodComparisonStripProps) {
  return (
    <section className="precision-period" aria-label={heading}>
      <h2 className="sr-only">{heading}</h2>
      <div className="precision-period__grid">
        <PeriodSnapshotView snapshot={current} />
        <div
          className="precision-period__relation"
          data-tone={relation.tone ?? 'neutral'}
          role="status"
        >
          <strong>{relation.label}</strong>
          <span>{relation.description}</span>
        </div>
        <PeriodSnapshotView snapshot={previous} />
      </div>
    </section>
  );
}

export interface SemanticTableColumn<Row> {
  readonly align?: 'end' | 'start';
  readonly compactLabel?: string;
  readonly isRowHeader?: boolean;
  readonly key: string;
  readonly label: string;
  readonly render: (row: Row, rowIndex: number) => ReactNode;
}

export interface SemanticDataTableProps<Row> {
  readonly caption: string;
  readonly columns: readonly SemanticTableColumn<Row>[];
  readonly description?: string;
  readonly emptyMessage?: string;
  readonly getRowKey?: (row: Row, rowIndex: number) => string;
  readonly id: string;
  readonly matrix?: boolean;
  readonly rows: readonly Row[];
}

export function SemanticDataTable<Row>({
  caption,
  columns,
  description,
  emptyMessage = 'No rows are available for this period.',
  getRowKey,
  id,
  matrix = false,
  rows,
}: SemanticDataTableProps<Row>) {
  const rowHeaderIndexes = columns
    .map((column, index) => column.isRowHeader === true ? index : -1)
    .filter((index) => index >= 0);
  if (matrix && rowHeaderIndexes.length !== 1) {
    throw new Error('A semantic matrix requires exactly one row-header column.');
  }
  const rowHeaderIndex = rowHeaderIndexes[0];
  const captionId = `${id}-caption`;

  return (
    <div
      className="precision-table-region"
      role="region"
      aria-labelledby={captionId}
      tabIndex={0}
    >
      <table
        aria-label={caption}
        className="precision-table"
        id={id}
        data-matrix={matrix ? 'true' : 'false'}
      >
        <caption className="precision-table__caption" id={captionId}>
          <strong>{caption}</strong>
          {description === undefined ? null : <span>{description}</span>}
        </caption>
        <thead>
          <tr>
            {columns.map((column, columnIndex) => (
              <th
                abbr={column.compactLabel}
                data-align={column.align ?? 'start'}
                id={`${id}-column-${columnIndex}`}
                key={column.key}
                scope="col"
              >
                {column.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.length === 0 ? (
            <tr>
              <td className="precision-table__empty" colSpan={columns.length === 0 ? 1 : columns.length}>
                {emptyMessage}
              </td>
            </tr>
          ) : rows.map((row, rowIndex) => {
            const rowId = `${id}-row-${rowIndex}`;
            const rowKey = getRowKey === undefined ? `${rowIndex}` : getRowKey(row, rowIndex);
            return (
              <tr key={rowKey}>
                {columns.map((column, columnIndex) => {
                  const columnId = `${id}-column-${columnIndex}`;
                  const compactLabel = column.compactLabel ?? column.label;
                  if (column.isRowHeader === true) {
                    return (
                      <th
                        data-align={column.align ?? 'start'}
                        data-column-label={compactLabel}
                        headers={columnId}
                        id={rowId}
                        key={column.key}
                        scope="row"
                      >
                        {column.render(row, rowIndex)}
                      </th>
                    );
                  }
                  const headers = rowHeaderIndex === undefined
                    ? columnId
                    : `${columnId} ${rowId}`;
                  return (
                    <td
                      data-align={column.align ?? 'start'}
                      data-column-label={compactLabel}
                      headers={headers}
                      key={column.key}
                    >
                      {column.render(row, rowIndex)}
                    </td>
                  );
                })}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export type SemanticMatrixProps<Row> = Omit<SemanticDataTableProps<Row>, 'matrix'>;

export function SemanticMatrix<Row>(props: SemanticMatrixProps<Row>) {
  return <SemanticDataTable {...props} matrix />;
}

export interface SelectedEntityDetailProps {
  readonly children?: ReactNode;
  readonly clearLabel?: string;
  readonly description?: ReactNode;
  readonly facts?: readonly PrecisionLabeledValue[];
  readonly heading: string;
  readonly label?: string;
  readonly onClear?: () => void;
}

export function SelectedEntityDetail({
  children,
  clearLabel = 'Clear selection',
  description,
  facts = [],
  heading,
  label = 'Selected item',
  onClear,
}: SelectedEntityDetailProps) {
  return (
    <section className="precision-selected" aria-label={label}>
      <header className="precision-selected__header">
        <div>
          <p className="precision-eyebrow">{label}</p>
          <h2>{heading}</h2>
          {description === undefined ? null : <div>{description}</div>}
        </div>
        {onClear === undefined ? null : (
          <button className="secondary" type="button" onClick={onClear}>{clearLabel}</button>
        )}
      </header>
      <LabeledValues className="precision-selected__facts" items={facts} />
      {children === undefined ? null : <div className="precision-selected__body">{children}</div>}
    </section>
  );
}

export interface EvidenceDisclosureProps {
  readonly children: ReactNode;
  readonly defaultOpen?: boolean;
  readonly summary?: ReactNode;
  readonly summaryDetail?: ReactNode;
}

export function EvidenceDisclosure({
  children,
  defaultOpen = false,
  summary = 'Evidence and qualifications',
  summaryDetail,
}: EvidenceDisclosureProps) {
  return (
    <details className="precision-evidence" open={defaultOpen}>
      <summary>
        <span>{summary}</span>
        {summaryDetail === undefined ? null : (
          <span className="precision-evidence__summary-detail">{summaryDetail}</span>
        )}
      </summary>
      <div className="precision-evidence__body">{children}</div>
    </details>
  );
}

export interface LoadingSkeletonProps {
  readonly label?: string;
  readonly lines?: 1 | 2 | 3 | 4 | 5 | 6;
}

export function LoadingSkeleton({
  label = 'Loading governed evidence',
  lines = 4,
}: LoadingSkeletonProps) {
  return (
    <div
      className="precision-loading"
      role="status"
      aria-busy="true"
      aria-label={label}
      aria-live="polite"
    >
      <span className="sr-only">{label}</span>
      <span className="precision-loading__heading" aria-hidden="true" />
      <span className="precision-loading__summary" aria-hidden="true" />
      <span className="precision-loading__panel" aria-hidden="true">
        {Array.from({ length: lines }, (_, index) => (
          <span className="precision-loading__line" key={index} />
        ))}
      </span>
    </div>
  );
}

interface StatePanelProps {
  readonly children?: ReactNode;
  readonly description: ReactNode;
  readonly role: 'alert' | 'status';
  readonly state: string;
  readonly title: string;
}

function StatePanel({ children, description, role, state, title }: StatePanelProps) {
  return (
    <section className="precision-state" data-state={state} role={role}>
      <div className="precision-state__mark" aria-hidden="true" />
      <div>
        <h2>{title}</h2>
        <div className="precision-state__description">{description}</div>
        {children === undefined ? null : <div className="precision-state__body">{children}</div>}
      </div>
    </section>
  );
}

export interface CoveredEmptyStateProps {
  readonly children?: ReactNode;
  readonly description: ReactNode;
  readonly title?: string;
}

export function CoveredEmptyState({
  children,
  description,
  title = 'No values in the covered period',
}: CoveredEmptyStateProps) {
  return (
    <StatePanel description={description} role="status" state="covered-empty" title={title}>
      {children}
    </StatePanel>
  );
}

export interface InsufficientStateProps {
  readonly children?: ReactNode;
  readonly description: ReactNode;
  readonly reasons?: readonly string[];
  readonly title?: string;
}

export function InsufficientState({
  children,
  description,
  reasons = [],
  title = 'Insufficient evidence',
}: InsufficientStateProps) {
  return (
    <StatePanel description={description} role="status" state="insufficient" title={title}>
      {reasons.length === 0 ? null : (
        <ul className="precision-state__reasons">
          {reasons.map((reason) => <li key={reason}>{reason}</li>)}
        </ul>
      )}
      {children}
    </StatePanel>
  );
}

export interface PageIssueProps {
  readonly children?: ReactNode;
  readonly description: ReactNode;
  readonly onRetry?: () => void;
  readonly retryLabel?: string;
  readonly title?: string;
}

export function PageIssue({
  children,
  description,
  onRetry,
  retryLabel = 'Try again',
  title = 'This page could not be loaded',
}: PageIssueProps) {
  return (
    <StatePanel description={description} role="alert" state="issue" title={title}>
      {children}
      {onRetry === undefined ? null : (
        <button type="button" onClick={onRetry}>{retryLabel}</button>
      )}
    </StatePanel>
  );
}

export interface ActionBarProps {
  readonly children: ReactNode;
  readonly label?: string;
  readonly status?: ReactNode;
}

export function ActionBar({
  children,
  label = 'Available actions',
  status,
}: ActionBarProps) {
  return (
    <nav className="precision-actions" aria-label={label}>
      <div className="precision-actions__controls">{children}</div>
      {status === undefined ? null : (
        <div className="precision-actions__status" aria-live="polite">{status}</div>
      )}
    </nav>
  );
}

export type EpistemicLabel = 'Evidence' | 'Hypothesis' | 'Next step' | 'Perspective';

export interface EpistemicNarrativeBlock {
  readonly content: ReactNode;
  readonly fallback?: boolean;
  readonly id: string;
  readonly label: EpistemicLabel;
}

export interface EpistemicNarrativeBlocksProps {
  readonly blocks: readonly EpistemicNarrativeBlock[];
  readonly label?: string;
}

export function EpistemicNarrativeBlocks({
  blocks,
  label = 'Reasoning',
}: EpistemicNarrativeBlocksProps) {
  if (blocks.length === 0) return null;
  return (
    <section className="precision-narrative" aria-label={label}>
      <h2 className="sr-only">{label}</h2>
      {blocks.map((block) => (
        <article
          className="precision-narrative__block"
          data-fallback={block.fallback === true ? 'true' : 'false'}
          key={block.id}
        >
          <h3>{block.label}</h3>
          <div>{block.content}</div>
        </article>
      ))}
    </section>
  );
}
