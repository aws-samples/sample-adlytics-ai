// SPDX-License-Identifier: MIT-0
export interface InlineScriptFile {
  readonly name: string;
  readonly code: string;
}
export function externaliseHtml(original: string): {
  readonly html: string;
  readonly files: readonly InlineScriptFile[];
  readonly flightBlocks: number;
};
export function blockedInlineScripts(html: string): string[];
