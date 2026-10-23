// SPDX-License-Identifier: MIT-0
'use client';
/**
 * Retail health › Products and ads (UI refresh 3.7, ADR-151): a composed tab over the served
 * Retail health parent and each row's baked product detail. It keeps Retail health's own window
 * control and URL, because both read the same preset.
 */
import { ProductsAndAdsView } from './ProductsAndAdsView';
import { RetailHealthBandTitle, RetailHealthBandTitleSkeleton, RetailHealthSkeleton } from './RetailHealthView';
import { parentPreset, RETAIL_HEALTH_ABSENCE_NO_PERIOD, RETAIL_HEALTH_ABSENCE_OTHER_PERIODS, RETAIL_HEALTH_ABSENCE_TITLE } from './RetailHealthRouteAdapter';
import { PresentationRequestError, PresentationResponseError, type PresentationErrorClass } from '@/lib/presentation';
import { anyPresetAvailable, fetchRetailHealthPresentationV2, fetchRetailHealthProductV2, type TemporalSelectionV1 } from '@/lib/presentation-v2';
import { loadProductAds, type ProductsAndAdsParent } from '@/lib/products-and-ads';
import type { V2RouteAdapter } from './PresentationV2RouteController';

function pageIssueClass(error: unknown): PresentationErrorClass {
  if (error instanceof PresentationRequestError) return error.errorClass;
  if (error instanceof PresentationResponseError) return 'invariant';
  return 'dependency';
}

type Absence = Extract<ProductsAndAdsParent, { readonly kind: 'window_unavailable' }>;

export const productsAndAdsRouteAdapter: V2RouteAdapter<ProductsAndAdsParent, never, never, undefined, Absence> = {
  // The same catalog rows as Retail health (ABS-RH-1, `docs/ui/page-absence-copy.md`): one source.
  absence: (parent) => ({
    message: anyPresetAvailable(parent.temporal) ? RETAIL_HEALTH_ABSENCE_OTHER_PERIODS : RETAIL_HEALTH_ABSENCE_NO_PERIOD,
    title: RETAIL_HEALTH_ABSENCE_TITLE,
  }),
  bandTitle: (state) => <RetailHealthBandTitle artifact={'parent' in state && state.parent.kind === 'parent' ? state.parent.artifact : null} />,
  bandTitleLoading: () => <RetailHealthBandTitleSkeleton />,
  loadParent: async (session: string, selection: TemporalSelectionV1 | undefined, signal: AbortSignal) => {
    const parent = await fetchRetailHealthPresentationV2(session, selection, signal);
    if (parent.kind !== 'parent') return { ...parent, products: new Map() };
    const products = await loadProductAds(parent, parentPreset(parent.temporal), signal, (reference, preset, abort) => fetchRetailHealthProductV2(session, reference, preset, abort));
    return { ...parent, products };
  },
  pageIssueClass,
  pageContext: (parent) => (parent.kind === 'parent' && parent.artifact.outcome === 'complete' ? parent.page_context : null),
  render: (state) => (state.kind === 'parent_ready' && state.parent.kind === 'parent'
    ? <ProductsAndAdsView artifact={state.parent.artifact} products={state.parent.products} />
    : null),
  renderLoading: () => <RetailHealthSkeleton subject="products and ads" />,
  generation: (presentation) => (presentation.kind === 'parent' ? presentation.generation : undefined),
  // The window control and URL are Retail health's: both tabs read the same preset.
  route: 'retail-health',
  temporal: (presentation) => presentation.temporal,
  windowUnavailable: (presentation): presentation is Absence => presentation.kind === 'window_unavailable',
};
