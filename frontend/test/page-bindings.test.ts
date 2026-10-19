// SPDX-License-Identifier: MIT-0
import assert from 'node:assert/strict';
import { test } from 'vitest';

import {
  bindingByPath,
  bindingByRoute,
  declaredPageIds,
  generatedActionById,
  pageBindings,
  parsePageBindings,
  renderedBindings,
  routeNames,
} from '@/lib/page-bindings';

function fictionalDocument(): Record<string, unknown> {
  return {
    pages: structuredClone(pageBindings),
    presentation_result_version: 2,
    temporal_control_version: 2,
    version: 2,
  };
}

function pages(document: Record<string, unknown>): Array<Record<string, unknown>> {
  const value = document.pages;
  assert.ok(Array.isArray(value));
  return value as Array<Record<string, unknown>>;
}

test('generated V2 binding parser accepts the current generated binding and canonical vocabulary', () => {
  assert.deepEqual(parsePageBindings(fictionalDocument()), pageBindings);
  // The backend contract declares ten pages; the frontend renders the routed tabs.
  assert.deepEqual(declaredPageIds, [
    'campaigns',
    'channel-performance',
    'workspace',
    'data-health',
    'media-efficiency',
    'retail-health',
    'scorecard',
    'new-customers',
    'reach',
    'retention',
    'audiences',
  ]);
  assert.deepEqual(pageBindings.map((page) => page.page_id), declaredPageIds);
  assert.deepEqual(routeNames, ['campaigns', 'retail-health', 'scorecard', 'new-customers', 'reach', 'retention', 'audiences']);
  assert.deepEqual(renderedBindings.map((page) => page.page_id), ['campaigns', 'retail-health', 'scorecard', 'new-customers', 'reach', 'retention', 'audiences']);
  assert.equal(bindingByRoute.campaigns.route, '/campaigns/');
  assert.deepEqual(bindingByRoute.campaigns.compatibility_routes, ['/']);
  assert.equal(bindingByPath['/campaigns/'].page_id, 'campaigns');
  assert.equal(bindingByPath['/retail-health/'].page_id, 'retail-health');
  assert.equal(bindingByPath['/scorecard/'].page_id, 'scorecard');
  assert.equal(bindingByPath['/new-customers/'].page_id, 'new-customers');
  assert.equal(bindingByPath['/reach/'].page_id, 'reach');
  assert.equal(bindingByPath['/retention/'].page_id, 'retention');
  assert.equal(bindingByPath['/audiences/'].page_id, 'audiences');
  assert.equal(Object.keys(bindingByRoute).length, 7);
  assert.equal(Object.keys(bindingByPath).length, 7);
});

test('generated V2 bindings expose the routed Reach declaration', () => {
  const reach = pageBindings.find((page) => page.page_id === 'reach');
  assert.ok(reach);
  assert.equal(reach.route, '/reach/');
  assert.deepEqual(reach.artifacts, [{
    artifact_id: 'reach',
    outcomes: ['complete', 'empty', 'insufficient'],
    type: 'ReachAndFunnelArtifact',
    version: 1,
  }]);
  assert.deepEqual(reach.temporal_policy, {
    candidate_presets: [
      'last_complete_week',
      'last_complete_month',
      'last_complete_quarter',
    ],
    comparison_intent: null,
    comparison_policy: 'prior_calendar_period',
    custom: { enabled: false, max_duration_days: 90, min_duration_days: 1 },
    default_preset: 'last_complete_month',
    display_grains: ['declared_period'],
    preset_exceptions: [],
  });
  assert.deepEqual(reach.sections, [
    { cardinality: 'exactly_one', role: 'scope', section_id: 'workspace-scope' },
    { cardinality: 'exactly_one', role: 'summary', section_id: 'overlap' },
    { cardinality: 'exactly_one', role: 'table', section_id: 'funnel' },
    { cardinality: 'exactly_one', role: 'evidence', section_id: 'reach-insights' },
    { cardinality: 'exactly_one', role: 'trend', section_id: 'frequency' },
    { cardinality: 'exactly_one', role: 'evidence', section_id: 'evidence-trace' },
    { cardinality: 'exactly_one', role: 'boundary', section_id: 'boundary' },
  ]);
  assert.ok(routeNames.includes('reach'));
  assert.ok(renderedBindings.some((page) => page.page_id === 'reach'));
  assert.equal(bindingByPath['/reach/'].page_id, 'reach');
});

test('generated V2 bindings expose the routed Retention vintage declaration', () => {
  const retention = bindingByRoute.retention;
  assert.equal(retention.route, '/retention/');
  assert.deepEqual(retention.artifacts, [{
    artifact_id: 'retention', outcomes: ['complete', 'empty', 'insufficient'],
    type: 'RetentionCohortArtifact', version: 1,
  }]);
  assert.deepEqual(retention.vintage_policy, {
    comparison: 'none', default: 'latest_with_current_publication', selector: 'as_of_month',
  });
  assert.equal('temporal_policy' in retention, false);
  assert.deepEqual(retention.sections.map((section) => section.section_id), [
    'workspace-scope', 'path', 'triangle', 'retention-curve', 'retention-insights', 'evidence-trace', 'boundary',
  ]);
  assert.equal(bindingByPath['/retention/'].page_id, 'retention');
});

test('generated V2 bindings expose the routed Audiences declaration', () => {
  const audiences = bindingByRoute.audiences;
  assert.equal(audiences.route, '/audiences/');
  assert.deepEqual(audiences.artifacts, [{
    artifact_id: 'audiences', outcomes: ['complete', 'empty', 'insufficient'],
    type: 'AudienceProfileArtifact', version: 1,
  }]);
  assert.deepEqual(audiences.temporal_policy.candidate_presets, ['last_complete_month']);
  assert.equal(audiences.temporal_policy.default_preset, 'last_complete_month');
  assert.deepEqual(audiences.sections.map((section) => section.section_id), [
    'workspace-scope', 'profile-path', 'segment-ranking', 'audience-insights', 'campaign-audiences', 'evidence-trace', 'boundary',
  ]);
  assert.equal(bindingByPath['/audiences/'].page_id, 'audiences');
});

test('generated V2 bindings expose temporal policy, artifacts, visuals, sections, and actions', () => {
  const campaigns = bindingByRoute.campaigns;
  assert.deepEqual(campaigns.artifacts.map((artifact) => artifact.artifact_id), [
    'campaign_catalog',
    'campaign_detail',
  ]);
  assert.deepEqual(campaigns.temporal_policy, {
    candidate_presets: ['1d', '7d', '14d', '30d'],
    comparison_intent: null,
    comparison_policy: 'required_equal_prior',
    custom: { enabled: false, max_duration_days: 90, min_duration_days: 2 },
    default_preset: '7d',
    display_grains: ['period', 'day'],
    preset_exceptions: [],
  });
  assert.deepEqual(campaigns.visuals.map((visual) => visual.visual_id), [
    'campaign_portfolio_trend',
    'campaign_delivery_daily',
  ]);
  // ADR-127 retired the `campaigns` actions block: the channel step is a served `open_page`
  // step to the Scorecard, so no page declares an action.
  assert.deepEqual(campaigns.actions, []);
  assert.deepEqual([...generatedActionById], []);
});

test('generated V2 binding parser rejects unknown fields and invalid transport versions', () => {
  const extra = fictionalDocument();
  extra.unexpected = true;
  assert.throws(() => parsePageBindings(extra), TypeError);

  const extraArtifactField = fictionalDocument();
  const artifacts = pages(extraArtifactField)[0]!.artifacts as Array<Record<string, unknown>>;
  artifacts[0]!.unexpected = true;
  assert.throws(() => parsePageBindings(extraArtifactField), TypeError);

  const wrongVersion = fictionalDocument();
  wrongVersion.presentation_result_version = 1;
  assert.throws(() => parsePageBindings(wrongVersion), TypeError);
});

test('generated V2 binding parser rejects reordered pages and mismatched page routes', () => {
  const reordered = fictionalDocument();
  const reorderedPages = pages(reordered);
  [reorderedPages[0], reorderedPages[1]] = [reorderedPages[1]!, reorderedPages[0]!];
  assert.throws(() => parsePageBindings(reordered), TypeError);

  const mismatchedRoute = fictionalDocument();
  pages(mismatchedRoute)[0]!.route = '/workspace/';
  assert.throws(() => parsePageBindings(mismatchedRoute), TypeError);
});

test('generated V2 binding parser rejects malformed browser compatibility fields', () => {
  const malformedTemporal = fictionalDocument();
  const temporal = pages(malformedTemporal)[0]!.temporal_policy as Record<string, unknown>;
  temporal.raw_visible_window = true;
  assert.throws(() => parsePageBindings(malformedTemporal), TypeError);

  const malformedArtifact = fictionalDocument();
  const artifacts = pages(malformedArtifact)[0]!.artifacts as Array<Record<string, unknown>>;
  artifacts[0]!.outcomes = ['available', 'available'];
  assert.throws(() => parsePageBindings(malformedArtifact), TypeError);

  const malformedVisual = fictionalDocument();
  const visuals = pages(malformedVisual)[0]!.visuals as Array<Record<string, unknown>>;
  visuals[0]!.metric_cohort = [];
  assert.throws(() => parsePageBindings(malformedVisual), TypeError);

  const malformedAction = fictionalDocument();
  pages(malformedAction)[0]!.actions = [{ action_id: 'fictional-action', target_route: '/fictional/' }];
  assert.throws(() => parsePageBindings(malformedAction), TypeError);
});
