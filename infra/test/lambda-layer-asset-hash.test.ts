// SPDX-License-Identifier: MIT-0
/** CDK's real file-asset hash must be independent of the layer's checkout path. */
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';

import { lambdaLayerAssetHash } from '../scripts/lambda-layer-asset-hash';

function fixture(parent: string, content = 'same bytes\n'): string {
  const root = fs.mkdtempSync(path.join(parent, 'layer-'));
  fs.mkdirSync(path.join(root, 'python'));
  fs.writeFileSync(path.join(root, 'python', 'module.py'), content);
  return root;
}

describe('the Python layer CDK asset hash', () => {
  let root: string;

  beforeEach(() => {
    root = fs.mkdtempSync(path.join(os.tmpdir(), 'layer-hash-test-'));
  });

  afterEach(() => {
    fs.rmSync(root, { recursive: true, force: true });
  });

  it('is equal for byte-identical trees at different absolute roots', () => {
    const first = fixture(root);
    const longer = fs.mkdtempSync(path.join(root, 'a-deliberately-longer-parent-name-'));
    const second = fixture(longer);
    expect(lambdaLayerAssetHash(first)).toBe(lambdaLayerAssetHash(second));
  });

  it('changes when a file byte changes', () => {
    expect(lambdaLayerAssetHash(fixture(root))).not.toBe(
      lambdaLayerAssetHash(fixture(root, 'different bytes\n')),
    );
  });

  it('leaves mode sensitivity to the gate inventory because CDK normalizes it', () => {
    const first = fixture(root);
    const second = fixture(root);
    fs.chmodSync(path.join(second, 'python', 'module.py'), 0o755);
    expect(lambdaLayerAssetHash(first)).toBe(lambdaLayerAssetHash(second));
  });
});
