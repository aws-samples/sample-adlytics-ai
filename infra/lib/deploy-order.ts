// SPDX-License-Identifier: MIT-0
import * as cdk from 'aws-cdk-lib';
import { DEPLOY_ORDER } from './config';

/**
 * Make `DEPLOY_ORDER` the order `cdk deploy --all` follows.
 *
 * Stacks exchange values through SSM only (ADR-012), so CloudFormation sees no dependency between
 * them and the CLI would otherwise deploy in an arbitrary order. Each stack the app composes is
 * made to depend on the one before it in `DEPLOY_ORDER`; a stack the configuration does not
 * compose is skipped, so the chain runs through whichever stacks exist. The dependency is order
 * only: no template changes, and no stack reads another's output.
 *
 * Fails synth when a composed stack's class is not in `DEPLOY_ORDER`, or when two composed stacks
 * share one, because either would leave the order undefined for that stack.
 *
 * A single stack is still deployable on its own with `cdk deploy --exclusively`, which is what
 * `make deploy` passes. Without it, deploying one stack also deploys everything before it.
 */
export function orderStacksForDeployment(app: cdk.App): cdk.Stack[] {
  const order: readonly string[] = DEPLOY_ORDER;
  const stacks = app.node.children.filter((child): child is cdk.Stack => child instanceof cdk.Stack);
  const seen = new Map<string, cdk.Stack>();
  for (const stack of stacks) {
    const name = stack.constructor.name;
    if (!order.includes(name)) {
      throw new Error(
        `${stack.stackName} is a ${name}, which DEPLOY_ORDER in infra/lib/config/index.ts does ` +
          'not list. Add it there, where it must deploy.',
      );
    }
    const earlier = seen.get(name);
    if (earlier !== undefined) {
      throw new Error(
        `${earlier.stackName} and ${stack.stackName} are both a ${name}; DEPLOY_ORDER cannot ` +
          'order them.',
      );
    }
    seen.set(name, stack);
  }
  const ordered = [...stacks].sort(
    (left, right) => order.indexOf(left.constructor.name) - order.indexOf(right.constructor.name),
  );
  ordered.forEach((stack, index) => {
    if (index > 0) stack.addDependency(ordered[index - 1]!, 'DEPLOY_ORDER');
  });
  return ordered;
}
