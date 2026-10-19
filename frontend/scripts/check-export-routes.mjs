// SPDX-License-Identifier: MIT-0
/**
 * The both-ways export check (ADR-115 decision 6). After the static export, every exported HTML
 * file must be a manifest route's index.html or a listed not-found output, and every manifest
 * route must have its exported index.html. A page can't ship without a manifest entry, and a
 * manifest route can't point at nothing.
 */
import { readFileSync, readdirSync } from 'node:fs';
import { dirname, join, relative, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

/** The export-relative index.html path a canonical route is served from. */
export function routeIndexPath(route) {
  return route === '/' ? 'index.html' : `${route.slice(1)}index.html`;
}

/** Every route the manifest serves: the root and each tab. */
export function manifestRoutes(manifest) {
  return [manifest.root.route, ...manifest.hubs.flatMap((hub) => hub.tabs.map((tab) => tab.route))];
}

/** The findings for one export listing; an empty list means both directions hold. */
export function exportRouteFindings(htmlFiles, manifest) {
  const exported = new Set(htmlFiles);
  const expected = new Map(manifestRoutes(manifest).map((route) => [routeIndexPath(route), route]));
  const exempt = new Set(manifest.not_found_outputs);
  const findings = [];
  for (const file of [...exported].sort()) {
    if (!expected.has(file) && !exempt.has(file)) {
      findings.push(`exported ${file} has no route manifest entry`);
    }
  }
  for (const [file, route] of expected) {
    if (!exported.has(file)) findings.push(`manifest route ${route} has no exported ${file}`);
  }
  if (!exported.has('404.html')) findings.push('the export has no 404.html');
  return findings;
}

function htmlFilesUnder(root) {
  const files = [];
  const walk = (directory) => {
    for (const entry of readdirSync(directory, { withFileTypes: true })) {
      const path = join(directory, entry.name);
      if (entry.isDirectory()) walk(path);
      else if (entry.name.endsWith('.html')) files.push(relative(root, path).split(sep).join('/'));
    }
  };
  walk(root);
  return files;
}

const invokedDirectly = process.argv[1] !== undefined
  && fileURLToPath(import.meta.url) === process.argv[1];
if (invokedDirectly) {
  const frontend = dirname(dirname(fileURLToPath(import.meta.url)));
  const manifest = JSON.parse(readFileSync(join(frontend, 'generated', 'route-manifest.json'), 'utf8'));
  const findings = exportRouteFindings(htmlFilesUnder(join(frontend, 'out')), manifest);
  for (const finding of findings) console.error(`export route check: ${finding}`);
  if (findings.length > 0) process.exit(1);
  console.log(`export route check: ${manifestRoutes(manifest).length} routes match the static export`);
}
