// SPDX-License-Identifier: MIT-0
/** ADR-139: each browser-owned catalog row equals its home in the catalog doc. */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { test } from 'vitest';
import {
  CAMPAIGN_ABSENCE_NO_PERIOD,
  CAMPAIGN_ABSENCE_OTHER_PERIODS,
  CAMPAIGN_ABSENCE_TITLE,
} from '@/components/CampaignRouteAdapter';
import { PI_DOMAIN_FALLBACK } from '@/components/PresentationV2RouteController';
import {
  RETAIL_HEALTH_ABSENCE_NO_PERIOD,
  RETAIL_HEALTH_ABSENCE_OTHER_PERIODS,
  RETAIL_HEALTH_ABSENCE_TITLE,
} from '@/components/RetailHealthRouteAdapter';

const catalog = readFileSync(join(__dirname, '..', '..', 'docs', 'ui', 'page-absence-copy.md'), 'utf8');

function row(id: string): { readonly text: string | undefined; readonly title: string | undefined } {
  const section = new RegExp(`^### ${id}\\n([\\s\\S]*?)(?=^#{2,3} |(?![\\s\\S]))`, 'm').exec(catalog)?.[1] ?? '';
  return {
    text: /^- Text: "([^"]+)"$/m.exec(section)?.[1],
    title: /^- Title: "([^"]+)"$/m.exec(section)?.[1],
  };
}

test('PI-DOMAIN in docs/ui/page-absence-copy.md is the browser fallback text', () => {
  const section = /^### PI-DOMAIN\n([\s\S]*?)(?=^#{2,3} )/m.exec(catalog)?.[1] ?? '';
  const text = /^- Text: "([^"]+)"$/m.exec(section)?.[1];
  assert.equal(text, PI_DOMAIN_FALLBACK);
});

test('ABS-CAMP-1/2 and ABS-RH-1/2 in docs/ui/page-absence-copy.md are the adapters\' absence copy', () => {
  assert.deepEqual(row('ABS-CAMP-1'), { text: CAMPAIGN_ABSENCE_OTHER_PERIODS, title: CAMPAIGN_ABSENCE_TITLE });
  assert.deepEqual(row('ABS-CAMP-2'), { text: CAMPAIGN_ABSENCE_NO_PERIOD, title: CAMPAIGN_ABSENCE_TITLE });
  assert.deepEqual(row('ABS-RH-1'), { text: RETAIL_HEALTH_ABSENCE_OTHER_PERIODS, title: RETAIL_HEALTH_ABSENCE_TITLE });
  assert.deepEqual(row('ABS-RH-2'), { text: RETAIL_HEALTH_ABSENCE_NO_PERIOD, title: RETAIL_HEALTH_ABSENCE_TITLE });
});
