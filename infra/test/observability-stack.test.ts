// SPDX-License-Identifier: MIT-0
/**
 * ObservabilityStack — account-wide span ingestion, and the reversibility of turning it off.
 *
 * The behaviour worth pinning is not that a resource appears. It is that the flag genuinely
 * controls it in both directions, because the whole point of the split between instrumentation
 * and ingestion is that a customer's cost decision stays reversible by configuration.
 */

import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { ObservabilityStack } from '../lib/stacks/observability-stack';
import { configWith, TEST_CONFIG } from './support/config';

const TRANSACTION_SEARCH = 'AWS::XRay::TransactionSearchConfig';

function synth(config = TEST_CONFIG): Template {
  const app = new cdk.App();
  const stack = new ObservabilityStack(app, 'Adlytics-Observability-test', { config });
  return Template.fromStack(stack);
}

/**
 * The rendered resource policy as text, tokens and all.
 *
 * The document interpolates partition, region and account, so CDK renders it as an `Fn::Join`
 * rather than a string — which is why the assertions below read the serialised template instead
 * of matching a property against a pattern.
 */
function policyText(): string {
  const resources = synth().toJSON().Resources as Record<string, { Type: string }>;
  const policy = Object.values(resources).find((r) => r.Type === 'AWS::Logs::ResourcePolicy');
  return JSON.stringify(policy);
}

describe('ObservabilityStack', () => {
  it('is never empty, because CloudFormation rejects a stack with no resources', () => {
    // The reason the flag is read in `bin/app.ts` rather than here. This stack existing *is*
    // the decision that ingestion is on, so it always carries the setting — and a version that
    // rendered nothing when the flag was off would ship an undeployable stack as the default.
    const resources = synth().toJSON().Resources as Record<string, unknown>;

    expect(Object.keys(resources).length).toBeGreaterThan(0);
  });

  it('creates exactly one Transaction Search configuration', () => {
    // Exactly one, because the resource is a singleton per account and region. Two definitions
    // would contend for the same setting and the last deploy would silently win.
    const template = synth(configWith({ transactionSearchIndexingPercentage: 5 }));

    template.resourceCountIs(TRANSACTION_SEARCH, 1);
    template.hasResourceProperties(TRANSACTION_SEARCH, { IndexingPercentage: 5 });
  });

  it('carries the configured indexing percentage rather than a hardcoded one', () => {
    // Indexing is the expensive half and the dial a customer should reach for before turning
    // ingestion off, because lowering it preserves the spans themselves.
    synth(configWith({ transactionSearchIndexingPercentage: 25 })).hasResourceProperties(
      TRANSACTION_SEARCH,
      { IndexingPercentage: 25 },
    );
  });

  it('states the indexing percentage, so the cheaper dial is visible before the drastic one', () => {
    const template = synth(configWith({ transactionSearchIndexingPercentage: 25 }));
    const output = template.findOutputs('OutSpanIndexingPercentage').OutSpanIndexingPercentage as
      | { Value: string }
      | undefined;

    expect(output?.Value).toBe('25');
  });

  it('grants X-Ray delivery before enabling ingestion, because the reverse order fails', () => {
    // Not a stylistic ordering. Creating the configuration first returns
    // "XRay does not have permission to call PutLogEvents on the aws/spans Log Group" and rolls
    // back — observed on a real deploy. Neither resource references the other, so
    // CloudFormation infers nothing and the dependency has to be stated.
    const resources = synth().toJSON().Resources as Record<
      string,
      { Type: string; DependsOn?: string[] }
    >;
    const policy = Object.entries(resources).find(([, r]) => r.Type === 'AWS::Logs::ResourcePolicy');
    const config = Object.entries(resources).find(([, r]) => r.Type === TRANSACTION_SEARCH);

    expect(policy).toBeDefined();
    expect(config?.[1].DependsOn).toContain(policy?.[0]);
  });

  it('grants delivery to both span log groups, not only aws/spans', () => {
    // Application Signals writes to its own log group and X-Ray needs the same grant there.
    // Read from the rendered template rather than matched as a string: the document interpolates
    // partition, region and account, so CDK renders it as an `Fn::Join` and not as text.
    expect(policyText()).toContain('application-signals');
    expect(policyText()).toContain('aws/spans');
  });

  it('pins the delivery grant to this account, so another cannot induce the write', () => {
    // Confused-deputy protection. Without the source conditions the grant names a service
    // principal and nothing else, which any account's X-Ray would satisfy.
    expect(policyText()).toContain('aws:SourceAccount');
    expect(policyText()).toContain('aws:SourceArn');
    expect(policyText()).toContain('xray.amazonaws.com');
  });

  it('holds nothing that retains on delete', () => {
    // A setting, not a store. Retaining an account-wide toggle after the stack that owned it is
    // gone leaves a customer billed for a decision nobody can find.
    const resources = synth().toJSON().Resources as Record<string, { DeletionPolicy?: string }>;

    for (const [name, resource] of Object.entries(resources)) {
      expect(resource.DeletionPolicy ?? 'Delete').not.toBe('Retain');
      expect(name).toBeTruthy();
    }
  });
});
