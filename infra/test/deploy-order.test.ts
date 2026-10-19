// SPDX-License-Identifier: MIT-0
import * as cdk from 'aws-cdk-lib';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';
import { DEPLOY_ORDER } from '../lib/config';
import { orderStacksForDeployment } from '../lib/deploy-order';

/** A deployable stack: CloudFormation rejects an empty `Resources` section. */
class Minimal extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);
    new ssm.StringParameter(this, 'Marker', { stringValue: id });
  }
}
// Named after DEPLOY_ORDER entries: the helper orders by class name.
class DependenciesStack extends Minimal {}
class DataV2Stack extends Minimal {}
class EdgeSecurityStack extends Minimal {}
class FrontendStack extends Minimal {}
class ApiStack extends Minimal {}
class UnlistedStack extends Minimal {}

const env = { account: '111122223333', region: 'eu-west-1' };

function dependencies(stack: cdk.Stack): string[] {
  return stack.dependencies.map((dependency) => dependency.stackName);
}

describe('orderStacksForDeployment', () => {
  it('chains the composed stacks in DEPLOY_ORDER, whatever order they were created in', () => {
    const app = new cdk.App();
    const api = new ApiStack(app, 'Api', { env });
    const frontend = new FrontendStack(app, 'Frontend', { env });
    const data = new DataV2Stack(app, 'DataV2', { env });
    const dependenciesStack = new DependenciesStack(app, 'Dependencies', { env });

    const ordered = orderStacksForDeployment(app);

    expect(ordered.map((stack) => stack.stackName)).toEqual([
      'Dependencies',
      'DataV2',
      'Frontend',
      'Api',
    ]);
    expect(dependencies(dependenciesStack)).toEqual([]);
    expect(dependencies(data)).toEqual(['Dependencies']);
    expect(dependencies(frontend)).toEqual(['DataV2']);
    expect(dependencies(api)).toEqual(['Frontend']);
  });

  it('orders across Regions, as the us-east-1 edge stack requires', () => {
    const app = new cdk.App();
    new DataV2Stack(app, 'DataV2', { env });
    const edge = new EdgeSecurityStack(app, 'EdgeSecurity', {
      env: { ...env, region: 'us-east-1' },
    });
    const frontend = new FrontendStack(app, 'Frontend', { env });

    orderStacksForDeployment(app);

    expect(dependencies(edge)).toEqual(['DataV2']);
    expect(dependencies(frontend)).toEqual(['EdgeSecurity']);
    expect(() => app.synth()).not.toThrow();
  });

  it('changes no template', () => {
    const plain = new cdk.App();
    const ordered = new cdk.App();
    for (const app of [plain, ordered]) {
      new DependenciesStack(app, 'Dependencies', { env });
      new ApiStack(app, 'Api', { env });
    }
    orderStacksForDeployment(ordered);
    const template = (app: cdk.App): unknown =>
      app.synth().getStackByName('Api').template as unknown;
    expect(template(ordered)).toEqual(template(plain));
  });

  it('refuses a stack DEPLOY_ORDER does not list', () => {
    const app = new cdk.App();
    new UnlistedStack(app, 'Unlisted', { env });
    expect(() => orderStacksForDeployment(app)).toThrow(/does not list/);
  });

  it('refuses two stacks of one class', () => {
    const app = new cdk.App();
    new ApiStack(app, 'ApiOne', { env });
    new ApiStack(app, 'ApiTwo', { env });
    expect(() => orderStacksForDeployment(app)).toThrow(/cannot order them/);
  });

  it('knows every class the test doubles stand for', () => {
    for (const name of ['DependenciesStack', 'DataV2Stack', 'EdgeSecurityStack', 'FrontendStack', 'ApiStack']) {
      expect(DEPLOY_ORDER as readonly string[]).toContain(name);
    }
  });
});
