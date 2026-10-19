// SPDX-License-Identifier: MIT-0
/**
 * The one Python dependency layer, and the properties that used to be asserted in two places.
 *
 * `ApiStack` and `AgentStack` each declared a `LayerVersion` over the *same* built asset, so each
 * had its own version stream — they had drifted to v4 and v2, meaning the two halves of the platform
 * could run different dependency versions while both stacks reported success. Each also asserted its
 * own layer separately, which is why "exactly one layer" could be true twice and still be wrong
 * overall.
 *
 * These assertions moved here rather than being deleted. Architecture compatibility is the one that
 * bites hardest: a layer built for the wrong architecture fails at **function init**, not at build or
 * synth, so nothing before deploy would catch it.
 */
import { Template } from 'aws-cdk-lib/assertions';
import * as cdk from 'aws-cdk-lib';

import { paramPath } from '../lib/config';
import { DependenciesStack } from '../lib/stacks/dependencies-stack';
import { TEST_CONFIG } from './support/config';

function synth(): Template {
  const app = new cdk.App();
  const stack = new DependenciesStack(app, 'Dependencies', {
    config: TEST_CONFIG,
    env: { account: TEST_CONFIG.account, region: TEST_CONFIG.region },
    // Any directory satisfies `Code.fromAsset` at synth time.
    layerAssetPath: __dirname,
  });
  return Template.fromStack(stack);
}

describe('the shared dependency layer', () => {
  it('declares exactly one layer for the whole platform', () => {
    // The assertion the split made impossible to write. Two stacks each declaring "one" layer is
    // two layers, and neither stack could see the other's.
    synth().resourceCountIs('AWS::Lambda::LayerVersion', 1);
  });

  it('is built for arm64 only', () => {
    // Every function is arm64 per ADR-005, and the wrong variant fails at function init rather than
    // at build or synth — so this is the only place before a deploy that can catch it.
    synth().hasResourceProperties('AWS::Lambda::LayerVersion', {
      CompatibleArchitectures: ['arm64'],
    });
  });

  it('pins the runtime it was resolved for', () => {
    synth().hasResourceProperties('AWS::Lambda::LayerVersion', {
      CompatibleRuntimes: [TEST_CONFIG.pythonRuntime],
    });
  });

  it('publishes a version ARN, not a version-less one', () => {
    /**
     * The distinction is load-bearing rather than pedantic.
     *
     * A version-less layer ARN cannot be attached to a function at all, and the version is the whole
     * reason this indirection is safe: a consumer resolves the ARN at deploy time and pins it, so a
     * layer rebuilt afterwards does not reach the function until it is redeployed.
     *
     * Asserted through the CloudFormation reference rather than by string matching, because the ARN
     * is a token at synth time. `Ref` on a `LayerVersion` yields the *version* ARN; `Fn::GetAtt` on
     * `LayerVersionArn` would be how a version-less one appeared.
     */
    const parameters = synth().findResources('AWS::SSM::Parameter');
    const published = Object.values(parameters).filter(
      (resource) =>
        (resource.Properties as { Name?: string }).Name ===
        paramPath(TEST_CONFIG.envName, 'dependenciesLayerArn'),
    );
    expect(published).toHaveLength(1);

    const value = (published[0]?.Properties as { Value?: unknown }).Value;
    expect(JSON.stringify(value)).toContain('Ref');
  });

  it('does not describe its own contents', () => {
    /**
     * The two descriptions this replaced each enumerated a subset — "pyjwt, pydantic, cryptography
     * and Powertools for the auth domain" and "pydantic, PyYAML, Powertools and the OTel API for the
     * tool functions" — while both layers held the full union. A comment that confidently states
     * something untrue is worse than none, and no description can be kept in step with
     * `pyproject.toml`.
     */
    const layers = Object.values(synth().findResources('AWS::Lambda::LayerVersion'));
    const description = String((layers[0]?.Properties as { Description?: string }).Description);
    expect(description).toContain('pyproject.toml');
    for (const library of ['pyjwt', 'cryptography', 'PyYAML', 'Powertools']) {
      expect(description).not.toContain(library);
    }
  });
});
