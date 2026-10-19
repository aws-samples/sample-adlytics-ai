// SPDX-License-Identifier: MIT-0
'use client';

/**
 * What a served next step can reach from where it renders (ADR-122 decision 5): the tabs in this
 * session's manifest, for a cross-tab `open_page` step, and the page's own preset control, for an
 * `open_preset` step. Outside their providers neither exists, so those chips don't render.
 */

import { createContext, useContext } from 'react';

import type { ManifestTab } from '@/lib/route-manifest';

/** The tabs this session's manifest lists, provided by the shell. */
export const VisibleTabsContext = createContext<readonly ManifestTab[]>([]);

export function useVisibleTabs(): readonly ManifestTab[] {
  return useContext(VisibleTabsContext);
}

/** Select one of the page's presets, as its date control does; null on an unavailable preset. */
export type PresetStep = (presetId: string) => (() => void) | null;

/** Provided by the page's route controller from its served temporal control. */
export const PresetStepContext = createContext<PresetStep | null>(null);

export function usePresetStep(): PresetStep | null {
  return useContext(PresetStepContext);
}
