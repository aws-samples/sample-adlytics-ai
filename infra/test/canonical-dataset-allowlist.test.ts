// SPDX-License-Identifier: MIT-0
/**
 * Every canonical dataset the contract declares has its partition-key grants.
 *
 * D2 live acceptance (2026-10-16): the D2 deliveries were admitted and then refused at the lease
 * (`AccessDeniedException`), because the dataset allowlist was a hand-kept copy that predated the
 * D2 tables. The list is now pinned to `contracts/generated/v2-tables.json`, and the
 * operational-status pointer-event keys derive from it.
 */
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';

import { loadGeneratedV2Storage } from '../lib/constructs/data-lake/generated-tables';
import { CANONICAL_DATASET_KEYS, CANONICAL_DATASET_PREFIXES } from '../lib/stacks/ingestion-stage1';
import { PUBLICATION_AUTHORITY_KEYS } from '../lib/stacks/operational-status-stage1';

const SOURCE = path.join(__dirname, '..', '..', 'contracts', 'generated', 'v2-tables.json');

/** The contract's canonical dataset names, sorted; candidate and current must agree. */
function contractDatasets(file: string): string[] {
  const generated = loadGeneratedV2Storage(file);
  const candidate = generated.canonicalCandidateTables.map((table) => table.name).sort();
  const current = generated.canonicalCurrentTables.map((table) => table.name).sort();
  expect(current).toEqual(candidate);
  return candidate;
}

describe('canonical dataset allowlist', () => {
  it('equals the contract canonical tables exactly, sorted', () => {
    expect([...CANONICAL_DATASET_PREFIXES]).toEqual(contractDatasets(SOURCE));
  });

  it('covers the canonical dataset of every source-aligned table', () => {
    const raw = JSON.parse(fs.readFileSync(SOURCE, 'utf-8')) as {
      sourceAlignedTables: { name: string; dataset: string }[];
    };
    const allowed = new Set<string>(CANONICAL_DATASET_PREFIXES);
    const missing = raw.sourceAlignedTables.filter((table) => !allowed.has(table.dataset));
    expect(missing.map((table) => table.name)).toStrictEqual([]);
    expect(raw.sourceAlignedTables.length).toBeGreaterThan(0);
  });

  it('includes the D2 datasets whose deliveries were refused live', () => {
    for (const dataset of ['dim_audience_segment', 'fct_exposure_group_outcome']) {
      expect(CANONICAL_DATASET_KEYS).toContain(`${dataset}|*`);
    }
  });

  it('is the one source of the operational-status pointer-event dataset keys', () => {
    expect(PUBLICATION_AUTHORITY_KEYS.filter((key) => key.endsWith('|*'))).toStrictEqual([
      ...CANONICAL_DATASET_KEYS,
    ]);
    expect(CANONICAL_DATASET_KEYS).toStrictEqual(
      CANONICAL_DATASET_PREFIXES.map((dataset) => `${dataset}|*`),
    );
  });

  it('detects a canonical table added to the contract without its grant (negative control)', () => {
    const document = JSON.parse(fs.readFileSync(SOURCE, 'utf-8')) as Record<string, any>;
    const extra = { ...document.canonicalCandidateTables[0], name: 'fct_unlisted' };
    document.canonicalCandidateTables.push(extra);
    document.canonicalCurrentTables.push({ ...extra });
    const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'adlytics-allowlist-'));
    const target = path.join(directory, 'v2-tables.json');
    fs.writeFileSync(target, JSON.stringify(document));
    expect(contractDatasets(target)).toContain('fct_unlisted');
    expect([...CANONICAL_DATASET_PREFIXES]).not.toEqual(contractDatasets(target));
  });
});
