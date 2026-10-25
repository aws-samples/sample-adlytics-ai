// SPDX-License-Identifier: MIT-0
import assert from 'node:assert/strict';
import { test } from 'vitest';

import { parseAudienceProfileArtifact, parseAudiencesPart } from '@/lib/audiences';
import fixture from './fixtures/audiences-complete-v2.json';
import campaignPage from './fixtures/audiences-campaign-page-v1.json';
import { audiencesFixture } from './fixtures/audiences-v1-fixtures';

type Json = Record<string, unknown>;
const clone = <T>(value: T): T => structuredClone(value);
const object = (value: unknown): Json => value as Json;
const objects = (value: unknown): Json[] => value as Json[];
const market = (value: Json): Json => objects(value.markets)[0]!;
const views = (value: Json): Json[] => objects(market(value).views);

test('strict Audiences decoder accepts the exact server fixture', () => {
  const artifact = parseAudienceProfileArtifact(fixture);
  assert.equal(artifact.markets[0].views[0].deliveries.length, 2);
  assert.equal(artifact.markets[0].views[1].deliveries[0]?.profile_path, 'dsp_exposed_only');
  assert.deepEqual(artifact.markets[0].campaign_audiences.index.map((entry) => [entry.campaign_ref, entry.campaign_label]), [['campaign-1', 'Fall prospecting']]);
  // The campaign's figures are its own part, fetched on selection, under the parent's revision.
  const served = parseAudiencesPart(campaignPage, { kind: 'campaign', page: 0, ref: 'campaign-1' });
  assert.equal(served.parent_revision, artifact.revision);
  assert.equal(served.part.kind === 'campaign' ? served.part.rows[0]?.segment_cpm?.text : null, '7.00 USD');
  // Paired: the same part asked for as another campaign or page is refused.
  assert.throws(() => parseAudiencesPart(campaignPage, { kind: 'campaign', page: 0, ref: 'campaign-2' }), TypeError);
  assert.throws(() => parseAudiencesPart(campaignPage, { kind: 'campaign', page: 1, ref: 'campaign-1' }), TypeError);
});

test('Audiences refuses unknown fields, multiple markets and reordered semantic rosters', () => {
  const cases: Json[] = [];
  const extra = clone(fixture) as Json; extra.unexpected = true; cases.push(extra);
  const multiple = clone(fixture) as Json; objects(multiple.markets).push(clone(objects(multiple.markets)[0]!)); cases.push(multiple);
  const reorderedViews = clone(fixture) as Json; views(reorderedViews).reverse(); cases.push(reorderedViews);
  const deliveries = clone(fixture) as Json; objects(views(deliveries)[0]!.deliveries).reverse(); cases.push(deliveries);
  const rows = clone(fixture) as Json; objects(views(rows)[1]!.rows)[0]!.profile_path = 'asi_in_market'; cases.push(rows);
  for (const candidate of cases) assert.throws(() => parseAudienceProfileArtifact(candidate), TypeError);
});

test('Audiences cross-checks paid dates plus display and availability pairs', () => {
  const cases: Json[] = [];
  const wrongDate = clone(fixture) as Json;
  objects(views(wrongDate)[0]!.deliveries)[0]!.period_text = 'Figures for September 2026 are expected after 17 Oct 2026.';
  cases.push(wrongDate);
  const missingDisplay = clone(fixture) as Json;
  objects(views(missingDisplay)[1]!.rows)[0]!.segment_index_text = null;
  cases.push(missingDisplay);
  const wrongIndex = clone(fixture) as Json;
  objects(views(wrongIndex)[1]!.rows)[0]!.segment_index_text = '99.99x';
  cases.push(wrongIndex);
  const wrongCount = clone(fixture) as Json;
  objects(views(wrongCount)[1]!.rows)[0]!.segment_buyers_text = '999';
  cases.push(wrongCount);
  const wrongShare = clone(fixture) as Json;
  object(objects(views(wrongShare)[1]!.rows)[0]!.segment_buyer_share).text = '99.9%';
  cases.push(wrongShare);
  const aboveOneShare = clone(fixture) as Json;
  const aboveOne = object(objects(views(aboveOneShare)[1]!.rows)[0]!.segment_buyer_share);
  aboveOne.value = '1.9999'; aboveOne.text = '999.9%';
  cases.push(aboveOneShare);
  const wrongState = clone(fixture) as Json;
  const stateRow = objects(views(wrongState)[1]!.rows)[0]!;
  stateRow.state = 'below_minimum'; stateRow.text = 'Fewer than 30 buyers, so no index is shown.';
  cases.push(wrongState);
  const wrongPages = clone(fixture) as Json;
  objects(object(market(wrongPages).campaign_audiences).index)[0]!.page_count = 2;
  cases.push(wrongPages);
  const unavailableFilled = clone(fixture) as Json;
  const dspDelivery = objects(views(unavailableFilled)[1]!.deliveries)[0]!;
  dspDelivery.returned = false; dspDelivery.total_returned = false;
  dspDelivery.profiled_buyers_total = null; dspDelivery.profiled_buyers_total_text = null;
  dspDelivery.profiled_exposed_total = null; dspDelivery.profiled_exposed_total_text = null;
  dspDelivery.insights = [{ artifact_revision: 'r'.repeat(43), evidence_line: 'Evidence', id: 'segment-over-index', output_class: 'perspective', prompt_version: 'deterministic-v1', state: 'filled', text: 'Filled' }];
  cases.push(unavailableFilled);
  const unavailableWithoutReason = clone(fixture) as Json;
  objects(views(unavailableWithoutReason)[1]!.not_available)[0]!.text = null;
  cases.push(unavailableWithoutReason);
  for (const candidate of cases) assert.throws(() => parseAudienceProfileArtifact(candidate), TypeError);
  const parts: Json[] = [];
  const extraMoney = clone(campaignPage) as Json;
  object(objects(extraMoney.rows)[0]!.segment_cpm).unexpected = true;
  parts.push(extraMoney);
  const wrongCpm = clone(campaignPage) as Json;
  object(objects(wrongCpm.rows)[0]!.segment_cpm).text = '99.99 USD';
  parts.push(wrongCpm);
  const wrongRoas = clone(campaignPage) as Json;
  objects(wrongRoas.rows)[0]!.segment_roas_attributed_text = '88.88x';
  parts.push(wrongRoas);
  for (const candidate of parts) assert.throws(() => parseAudiencesPart(candidate, { kind: 'campaign', page: 0, ref: 'campaign-1' }), TypeError);
});

test('Audiences non-complete fixture requires a displayable status', () => {
  const nonComplete = audiencesFixture('insufficient');
  assert.equal(parseAudienceProfileArtifact(nonComplete).outcome, 'insufficient');

  const emptyDisplay = clone(nonComplete);
  for (const currentView of views(emptyDisplay)) for (const currentDelivery of objects(currentView.deliveries)) {
    currentDelivery.returned = true; currentDelivery.total_returned = true; currentDelivery.total_text = null;
    currentDelivery.profiled_buyers_total = 1; currentDelivery.profiled_buyers_total_text = '1';
    currentDelivery.profiled_exposed_total = 1; currentDelivery.profiled_exposed_total_text = '1';
    currentDelivery.period_text = null;
    if (currentDelivery.paid_state !== null) object(currentDelivery.paid_state).period_published = true;
    currentDelivery.insights = [{
      artifact_revision: 'r'.repeat(43), evidence_line: 'Found by a fixed rule.', id: 'segment-over-index',
      output_class: 'perspective', prompt_version: 'deterministic-v1', state: 'filled', text: 'Measured segment result.',
    }];
  }
  assert.throws(() => parseAudienceProfileArtifact(emptyDisplay), TypeError);
});
