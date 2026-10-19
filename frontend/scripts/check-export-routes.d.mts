// SPDX-License-Identifier: MIT-0
export interface ExportRouteManifest {
  readonly hubs: readonly { readonly tabs: readonly { readonly route: string }[] }[];
  readonly not_found_outputs: readonly string[];
  readonly root: { readonly route: string };
}
export function routeIndexPath(route: string): string;
export function manifestRoutes(manifest: ExportRouteManifest): string[];
export function exportRouteFindings(
  htmlFiles: readonly string[],
  manifest: ExportRouteManifest,
): string[];
