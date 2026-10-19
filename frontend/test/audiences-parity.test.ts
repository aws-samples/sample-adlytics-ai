// SPDX-License-Identifier: MIT-0
// Decoder parity: the table tests/unit/test_audiences_parity.py builds from server-encoded output
// and judges with the server's served validator; the browser must give every case the same verdict.
import assert from 'node:assert/strict';
import { test } from 'vitest';

import { parseAudienceProfileArtifact } from '@/lib/audiences';
import complete from './fixtures/audiences-complete-v2.json';
import inRosterUnavailable from './fixtures/audiences-in-roster-unavailable-v2.json';
import cases from './fixtures/audiences-parity-cases.json';

interface ParityCase {
  readonly name: string;
  readonly base: 'complete' | 'in_roster_unavailable';
  readonly accept: boolean;
  readonly control: string | null;
  readonly set: readonly { readonly path: readonly (string | number)[]; readonly value: unknown }[];
}
const table = cases as readonly ParityCase[];
const bases = { complete, in_roster_unavailable: inRosterUnavailable } as const;

function apply(item: ParityCase): unknown {
  const candidate = structuredClone(bases[item.base]) as unknown;
  for (const change of item.set) {
    let target = candidate as Record<string | number, unknown>;
    for (const key of change.path.slice(0, -1)) target = target[key] as Record<string | number, unknown>;
    target[change.path.at(-1)!] = structuredClone(change.value);
  }
  return candidate;
}

test('every accepting parity case has a refusing control and every refusal names one', () => {
  const accepting = new Set(table.filter((item) => item.accept).map((item) => item.name));
  const controls = new Set(table.filter((item) => !item.accept).map((item) => item.control));
  assert.deepEqual([...controls].sort(), [...accepting].sort());
  assert.ok(accepting.size >= 2 && table.length > accepting.size);
});

test.each(table.map((item) => [item.name, item] as const))('browser decoder gives the server verdict: %s', (_name, item) => {
  const candidate = apply(item);
  if (item.accept) parseAudienceProfileArtifact(candidate);
  else assert.throws(() => parseAudienceProfileArtifact(candidate), TypeError);
});
