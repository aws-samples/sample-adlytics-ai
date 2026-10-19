// SPDX-License-Identifier: MIT-0
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'vitest';

/**
 * C1 cross-language check (design §4.5, R3-16): the model input names the table's sort with
 * the same words the table shows. The Python literal and the table's label() are read as text,
 * so a wording edit on either side fails here.
 */
function source(relative: string): string {
  return readFileSync(fileURLToPath(new URL(relative, import.meta.url)), 'utf8');
}

function pythonSortWords(): Record<string, string> {
  const text = source('../../src/domain/presentation/campaign_portfolio.py');
  const block = /CAMPAIGN_SORT_FIELD_WORDS: Final\[Mapping\[str, str\]\] = \{([\s\S]*?)\n\}/.exec(text);
  assert.ok(block, 'CAMPAIGN_SORT_FIELD_WORDS literal not found');
  return Object.fromEntries([...block[1]!.matchAll(/"([a-z_]+)": "([^"]+)"/g)].map((match) => [match[1]!, match[2]!]));
}

function tableLabels(): Record<string, string> {
  const text = source('../components/CampaignPortfolioV2View.tsx');
  const block = /function label\([^)]*\): string \{\s*return \{([\s\S]*?)\}\[metricName\];/.exec(text);
  assert.ok(block, 'table label() not found');
  return Object.fromEntries([...block[1]!.matchAll(/([a-z_0-9]+): '([^']+)'/g)].map((match) => [match[1]!, match[2]!]));
}

test('the model-input sort words match the table sort labels for every sort field', () => {
  const python = pythonSortWords();
  const labels = tableLabels();
  assert.deepEqual(Object.keys(python).sort(), ['attributed_roas', 'ctr', 'name', 'purchases', 'spend']);
  for (const [field, words] of Object.entries(python)) {
    const expected = field === 'name' ? 'campaign name' : labels[field]?.toLowerCase();
    assert.equal(words.toLowerCase(), expected, field);
  }
});
