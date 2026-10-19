// SPDX-License-Identifier: MIT-0
import assert from 'node:assert/strict';
import { test } from 'vitest';

import manifest from '../generated/route-manifest.json';
import { exportRouteFindings, routeIndexPath } from '../scripts/check-export-routes.mjs';

// The export listing of 2026-10-07, before app/not-found.tsx ships (Slice 2).
const listing = [
  '404.html',
  '404/index.html',
  '_not-found/index.html',
  'admin/prompts/index.html',
  'audiences/index.html',
  'campaigns/index.html',
  'index.html',
  // Slice 9 New customers: the Customers hub's first tab.
  'new-customers/index.html',
  'reach/index.html',
  'retention/index.html',
  'retail-health/index.html',
  'scorecard/index.html',
];

test('the committed manifest matches the real export listing in both directions', () => {
  assert.deepEqual(exportRouteFindings(listing, manifest), []);
});

test('an exported page without a manifest entry fails the build', () => {
  assert.deepEqual(
    exportRouteFindings([...listing, 'channel-performance/index.html'], manifest),
    ['exported channel-performance/index.html has no route manifest entry'],
  );
});

test('a manifest route without an exported page fails the build', () => {
  assert.deepEqual(
    exportRouteFindings(listing.filter((file) => file !== 'retail-health/index.html'), manifest),
    ['manifest route /retail-health/ has no exported retail-health/index.html'],
  );
});

test('the export must keep its 404 page', () => {
  assert.deepEqual(
    exportRouteFindings(listing.filter((file) => file !== '404.html'), manifest),
    ['the export has no 404.html'],
  );
});

test('the root route is served from the top-level index', () => {
  assert.equal(routeIndexPath('/'), 'index.html');
  assert.equal(routeIndexPath('/admin/prompts/'), 'admin/prompts/index.html');
});
