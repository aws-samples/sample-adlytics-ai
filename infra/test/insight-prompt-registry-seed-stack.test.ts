// SPDX-License-Identifier: MIT-0
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';

import { insightContractSetDigest } from '../lib/stacks/insight-prompt-registry-seed-stack';
import { everyTemplate } from './support/templates';

type Props = Record<string, unknown>;

const seed = everyTemplate().InsightPromptRegistrySeedStack as Props;
const INSIGHT_CONTRACTS = path.join(__dirname, '..', '..', 'contracts', 'insights');

describe('the seed custom resource', () => {
  it('carries the synth-computed digest of the exact insight contract set', () => {
    const digest = insightContractSetDigest(INSIGHT_CONTRACTS);
    expect(digest).toMatch(/^[a-f0-9]{64}$/);
    const customResources = Object.values((seed.Resources as Record<string, Props>) ?? {}).filter(
      (resource) => {
        const properties = resource.Properties;
        return typeof properties === 'object' && properties !== null
          && (properties as Props).contractSet === 'insight-v1';
      },
    );
    expect(customResources).toHaveLength(1);
    expect(((customResources[0] as Props).Properties as Props).contractDigest).toBe(digest);
  });
});

describe('insightContractSetDigest', () => {
  let fixture: string;

  beforeEach(() => {
    fixture = mkdtempSync(path.join(os.tmpdir(), 'insight-contracts-'));
    writeFileSync(path.join(fixture, 'a.yaml'), 'id: a\nversion: 1\n');
    mkdirSync(path.join(fixture, 'nested'));
    writeFileSync(path.join(fixture, 'nested', 'b.yaml'), 'id: b\nversion: 1\n');
  });

  afterEach(() => {
    rmSync(fixture, { recursive: true, force: true });
  });

  it('is deterministic over unchanged directory content', () => {
    expect(insightContractSetDigest(fixture)).toBe(insightContractSetDigest(fixture));
  });

  it('changes when one contract\'s bytes change', () => {
    const before = insightContractSetDigest(fixture);
    writeFileSync(path.join(fixture, 'nested', 'b.yaml'), 'id: b\nversion: 2\n');
    expect(insightContractSetDigest(fixture)).not.toBe(before);
  });

  it('changes when a contract is added or removed', () => {
    const before = insightContractSetDigest(fixture);
    writeFileSync(path.join(fixture, 'c.yaml'), 'id: c\nversion: 1\n');
    const added = insightContractSetDigest(fixture);
    expect(added).not.toBe(before);
    rmSync(path.join(fixture, 'c.yaml'));
    expect(insightContractSetDigest(fixture)).toBe(before);
  });

  it('distinguishes the same bytes under a different relative path', () => {
    const before = insightContractSetDigest(fixture);
    rmSync(path.join(fixture, 'nested', 'b.yaml'));
    writeFileSync(path.join(fixture, 'b.yaml'), 'id: b\nversion: 1\n');
    expect(insightContractSetDigest(fixture)).not.toBe(before);
  });
});
