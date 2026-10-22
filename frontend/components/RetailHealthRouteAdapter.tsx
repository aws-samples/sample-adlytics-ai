// SPDX-License-Identifier: MIT-0
'use client';

import { useEffect } from 'react';

import { useOpenEntity } from './AppShell';
import { PAGE_HEADING_ID } from './Dialog';
import { Icon } from './Icon';
import { RetailHealthBandTitle, RetailHealthBandTitleSkeleton, RetailHealthSkeleton, RetailHealthView } from './RetailHealthView';
import { RetailProductDetailView, type ProductNeighbours } from './RetailProductDetailView';
import { PageError } from './StatePanels';
import { PresentationRequestError, PresentationResponseError } from '@/lib/presentation';
import { bindingByRoute } from '@/lib/page-bindings';
import {
  anyPresetAvailable,
  fetchRetailHealthPresentationV2,
  fetchRetailHealthProductV2,
  focusTemporalSelection,
  type RetailHealthPresentation,
  type RetailProductPresentation,
  type TemporalControlV1,
  type TemporalSelectionV1,
} from '@/lib/presentation-v2';
import type { PresentationErrorClass } from '@/lib/presentation';
import type { V2RouteAdapter } from './PresentationV2RouteController';

/** One product row's opaque capability: the only thing the browser sends to open detail. */
export interface RetailProductCommand {
  readonly selection_reference: string;
}

type ParentPresentation = Extract<RetailHealthPresentation, { readonly kind: 'parent' }>;

function pageIssueClass(error: unknown): PresentationErrorClass {
  if (error instanceof PresentationRequestError) return error.errorClass;
  if (error instanceof PresentationResponseError) return 'invariant';
  return 'dependency';
}

/** The preset the parent was served on; product detail always names the parent's own window. */
export function parentPreset(temporal: TemporalControlV1): TemporalSelectionV1 {
  return focusTemporalSelection(temporal)
    ?? { kind: 'preset', preset_id: bindingByRoute['retail-health'].temporal_policy.default_preset };
}

function openableRows(parent: RetailHealthPresentation) {
  return parent.kind === 'parent' ? parent.artifact.rows.filter((row) => row.selection_reference !== null) : [];
}

function neighboursFor(
  parent: RetailHealthPresentation,
  reference: string,
  requestChild: (command: RetailProductCommand) => void,
): ProductNeighbours | null {
  const rows = openableRows(parent);
  const index = rows.findIndex((row) => row.selection_reference === reference);
  if (index < 0) return null;
  const open = (position: number) => {
    const row = rows[position];
    return row?.selection_reference == null ? null : () => requestChild({ selection_reference: row.selection_reference as string });
  };
  return { index, next: open(index + 1), previous: open(index - 1), total: rows.length };
}

/** Focus fell to the document because the control that held it left with the view. */
function focusLost(): boolean {
  return document.activeElement === null || document.activeElement === document.body;
}

/** Focus is where detail left it: lost, or still on the h1 that opening detail focused. */
function focusLeftByDetail(): boolean {
  return focusLost() || document.activeElement?.id === PAGE_HEADING_ID;
}

/**
 * Opening detail focuses the h1; closing it returns focus to the product's row (ia §7.3a item 1,
 * as campaign detail does in §7.2 item 11a). Neither moves focus that is still on a control, such
 * as the date control after a window change re-opens the product.
 */
function useDetailFocus(childAsin: string | null, focusHeading: boolean): void {
  useEffect(() => {
    // Detail replaces the list in place, so the h1 is scrolled into view with its focus.
    if (focusHeading && focusLost()) document.getElementById(PAGE_HEADING_ID)?.focus();
    if (childAsin === null) return;
    return () => {
      window.requestAnimationFrame(() => {
        // Browser Back leaves focus on the h1; the close returns it to the row all the same.
        if (!focusLeftByDetail()) return;
        const rows = document.querySelectorAll<HTMLButtonElement>('button[data-product-row]');
        [...rows].find((row) => row.dataset.productRow === childAsin)?.focus();
      });
    };
  }, [childAsin, focusHeading]);
}

/** The open product names the h1, the crumb's last item and the document title (ia §7.3a). */
function ProductPage({ child, neighbours, onBack, reference, sessionId }: {
  readonly child: RetailProductPresentation;
  readonly neighbours: ProductNeighbours | null;
  readonly onBack: () => void;
  readonly reference: string;
  readonly sessionId: string;
}) {
  useOpenEntity(child.artifact.product.title, onBack);
  useDetailFocus(child.artifact.product.child_asin, true);
  return <RetailProductDetailView
    artifact={child.artifact}
    neighbours={neighbours}
    onBack={onBack}
    selectionReference={reference}
    sessionId={sessionId}
    temporalSelection={parentPreset(child.temporal)}
  />;
}

function LoadingProduct({ onBack, title }: { readonly onBack: () => void; readonly title: string | null }) {
  useOpenEntity(title, onBack);
  return title === null ? <RetailHealthSkeleton /> : <RetailHealthSkeleton subject={title} />;
}

/** A product whose detail couldn't load keeps detail's one close path: Back, the crumb, browser Back. */
function UnavailableProduct({ childAsin, onBack, onRetry, title }: {
  readonly childAsin: string | null;
  readonly onBack: () => void;
  readonly onRetry: () => void;
  readonly title: string | null;
}) {
  useOpenEntity(title, onBack);
  useDetailFocus(childAsin, false);
  return <div className="retail-product">
    <div className="detail-nav">
      <button type="button" className="btn link" onClick={onBack}><Icon name="back" small />Back to Retail health</button>
    </div>
    <PageError message="This product's detail can't be shown right now." onRetry={onRetry} />
  </div>;
}

/** The parent a page-level absence carries (ADR-139). */
type RetailHealthAbsence = Extract<RetailHealthPresentation, { readonly kind: 'window_unavailable' }>;

/** Catalog ABS-RH-1 / ABS-RH-2 (`docs/ui/page-absence-copy.md`). */
export const RETAIL_HEALTH_ABSENCE_TITLE = "Retail health isn't published for this period";
export const RETAIL_HEALTH_ABSENCE_OTHER_PERIODS = 'Choose another period above to see shipped sales, traffic and returns.';
/** ABS-RH-3 and ABS-RH-4 (OD-UX-9, E1): a served empty or insufficient body, each its own words. */
export const RETAIL_HEALTH_EMPTY_TITLE = 'No retail movement in this period';
export const RETAIL_HEALTH_EMPTY_TEXT = 'Vendor Central reported no shipped sales, traffic or returns for this period. If you expected sales, ask your administrator to check the Vendor Central connection.';
export const RETAIL_HEALTH_INSUFFICIENT_TITLE = 'Not enough Vendor Central data for this period';
export const RETAIL_HEALTH_INSUFFICIENT_TEXT = "There isn't enough Vendor Central data to show this period safely. Choose another period above. If this keeps happening, ask your administrator to check the Vendor Central connection.";
export const RETAIL_HEALTH_ABSENCE_NO_PERIOD = "Retail health isn't ready to show yet. It's built from your Vendor Central data, which your administrator connects. When it's ready, you'll see the sales, traffic, conversion and returns your data supports, per product.";
export const retailHealthRouteAdapter: V2RouteAdapter<
  RetailHealthPresentation,
  RetailProductPresentation,
  RetailProductCommand,
  undefined,
  RetailHealthAbsence
> = {
  // The v1 contract carries no server text, so the browser owns these catalog rows (ADR-139, N6);
  // another period is offered only when the control has one to choose.
  absence: (parent) => ({
    message: anyPresetAvailable(parent.temporal) ? RETAIL_HEALTH_ABSENCE_OTHER_PERIODS : RETAIL_HEALTH_ABSENCE_NO_PERIOD,
    title: RETAIL_HEALTH_ABSENCE_TITLE,
  }),
  bandTitle: (state) => <RetailHealthBandTitle
    artifact={'parent' in state && state.parent.kind === 'parent' ? state.parent.artifact : null}
    product={state.kind === 'child_ready' ? state.child.artifact.product.title : null}
  />,
  bandTitleLoading: () => <RetailHealthBandTitleSkeleton />,
  child: {
    historyMarker: true,
    load: async (parent, command, sessionId, signal) => {
      if (parent.kind !== 'parent') throw new PresentationResponseError();
      const child = await fetchRetailHealthProductV2(sessionId, command.selection_reference, parentPreset(parent.temporal), signal);
      if (child.parent_revision !== parent.artifact_revision) throw new PresentationResponseError();
      return child;
    },
    // A stale capability, or a bake that moved on, re-serves the parent (ADR-125 decision 3).
    selectionUnavailable: (error) => error instanceof PresentationRequestError &&
      (error.code === 'ADLY-PRESENTATION-0001' || error.code === 'ADLY-PRESENTATION-0004'),
    // The open product is the one re-served row with its title and ASIN (its safe signature).
    rematch: (parent, child) => {
      const matches = openableRows(parent).filter((row) =>
        row.child_asin === child.artifact.product.child_asin && row.title === child.artifact.product.title);
      const reference = matches.length === 1 ? matches[0]?.selection_reference ?? null : null;
      return reference === null ? null : { selection_reference: reference };
    },
    reopenAfterParentChange: true,
  },
  loadParent: (session: string, selection: TemporalSelectionV1 | undefined, signal: AbortSignal) => fetchRetailHealthPresentationV2(session, selection, signal),
  pageIssueClass,
  // A page-level absence (an empty or insufficient body or detail) offers no context.
  pageContext: (parent, child) => {
    if (child !== null) return child.artifact.outcome === 'selected' ? child.page_context : null;
    return parent.kind === 'parent' && parent.artifact.outcome === 'complete' ? parent.page_context : null;
  },
  renderLoading: () => <RetailHealthSkeleton />,
  render: (state, requestChild, clearChild, _replaceParentIntent, sessionId = '', closedChild = null) => {
    if (
      state.kind !== 'parent_ready' && state.kind !== 'child_loading' && state.kind !== 'child_ready' &&
      state.kind !== 'child_unavailable' && state.kind !== 'selection_unavailable_refreshing'
    ) return null;
    if (state.parent.kind !== 'parent') return null;
    const parent: ParentPresentation = state.parent;
    if (state.kind === 'child_ready') {
      return <ProductPage
        child={state.child}
        key={state.command.selection_reference}
        neighbours={neighboursFor(parent, state.command.selection_reference, requestChild)}
        onBack={clearChild}
        reference={state.command.selection_reference}
        sessionId={sessionId}
      />;
    }
    if (state.kind === 'child_loading') {
      const row = parent.artifact.rows.find((candidate) => candidate.selection_reference === state.command.selection_reference);
      return <LoadingProduct onBack={clearChild} title={row?.title ?? null} />;
    }
    if (state.kind === 'child_unavailable') {
      const row = parent.artifact.rows.find((candidate) => candidate.selection_reference === state.command.selection_reference);
      return <UnavailableProduct
        childAsin={row?.child_asin ?? null}
        onBack={clearChild}
        onRetry={() => requestChild(state.command)}
        title={row?.title ?? null}
      />;
    }
    return <RetailHealthView
      artifact={parent.artifact}
      closedProduct={closedChild === null ? null : { title: closedChild.artifact.product.title }}
      onOpenProduct={(row) => {
        if (row.selection_reference !== null) requestChild({ selection_reference: row.selection_reference });
      }}
      sessionId={sessionId}
      temporalSelection={focusTemporalSelection(parent.temporal)}
    />;
  },
  generation: (presentation) => (presentation.kind === 'parent' ? presentation.generation : undefined),
  route: 'retail-health',
  temporal: (presentation) => presentation.temporal,
  windowUnavailable: (presentation): presentation is RetailHealthAbsence => presentation.kind === 'window_unavailable',
};
