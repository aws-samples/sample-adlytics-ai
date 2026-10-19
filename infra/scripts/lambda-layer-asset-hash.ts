// SPDX-License-Identifier: MIT-0
/** Compute the CDK file-asset hash for a Python layer using Lambda's real fromAsset path. */
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import * as cdk from 'aws-cdk-lib';
import * as lambda from 'aws-cdk-lib/aws-lambda';

interface AssetManifest {
  readonly files?: Record<
    string,
    { readonly source?: { readonly packaging?: string } }
  >;
}

export function lambdaLayerAssetHash(layerPath: string): string {
  if (!path.isAbsolute(layerPath)) {
    throw new Error(`layer path must be absolute: ${layerPath}`);
  }
  if (!fs.statSync(layerPath, { throwIfNoEntry: false })?.isDirectory()) {
    throw new Error(`layer path is not a directory: ${layerPath}`);
  }

  const outdir = fs.mkdtempSync(path.join(os.tmpdir(), 'lambda-layer-asset-hash-'));
  try {
    const app = new cdk.App({ outdir });
    const stack = new cdk.Stack(app, 'LayerAssetHash');
    new lambda.LayerVersion(stack, 'Layer', {
      code: lambda.Code.fromAsset(layerPath),
    });
    const assembly = app.synth();
    const artifact = Object.values(assembly.manifest.artifacts ?? {}).find(
      (candidate) => candidate.type === 'cdk:asset-manifest',
    );
    const properties = artifact?.properties as { readonly file?: unknown } | undefined;
    const manifestFile = properties?.file;
    if (typeof manifestFile !== 'string') {
      throw new Error('CDK synthesis produced no asset manifest');
    }
    const manifest = JSON.parse(
      fs.readFileSync(path.join(outdir, manifestFile), 'utf8'),
    ) as AssetManifest;
    const hashes = Object.entries(manifest.files ?? {})
      .filter(([, asset]) => asset.source?.packaging === 'zip')
      .map(([hash]) => hash);
    if (hashes.length !== 1 || hashes[0] === undefined) {
      throw new Error(`expected exactly one zipped CDK file asset, found ${hashes.length}`);
    }
    return hashes[0];
  } finally {
    fs.rmSync(outdir, { recursive: true, force: true });
  }
}

function main(argv: readonly string[]): void {
  if (argv.length !== 1 || argv[0] === undefined) {
    process.stderr.write('usage: lambda-layer-asset-hash.ts <absolute-layer-path>\n');
    process.exit(2);
  }
  process.stdout.write(`${lambdaLayerAssetHash(argv[0])}\n`);
}

if (require.main === module) {
  main(process.argv.slice(2));
}
