// SPDX-License-Identifier: MIT-0
/**
 * Customer-word provenance lines (design revision 2026-09-25.1), shared by both
 * dialogs. The wire's non-model prompt_version stamps select contract-owned customer
 * lines; every other stamp (a real registry version) reads as the generic governed
 * line — internal tokens and prompt versions never render.
 */

export const PROVENANCE_READINGS: Readonly<Record<string, string>> = {
  'deterministic-gate1': 'Decided by a fixed rule from the values on this page · no model involved',
  'deterministic-v1': 'Computed directly from the values on this page · no model involved',
  'not-generated': 'No reading was generated for this view',
};

export const GENERIC_PROVENANCE =
  "One governed reading of this view · updates when this page's data does";

/** The strip line for one stamp: a mapped contract line or the generic governed line. */
export function provenanceLine(promptVersion: string): string {
  return PROVENANCE_READINGS[promptVersion] ?? GENERIC_PROVENANCE;
}
