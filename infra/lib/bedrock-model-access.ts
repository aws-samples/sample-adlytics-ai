// SPDX-License-Identifier: MIT-0
/**
 * The one shape every Bedrock model grant takes (A-IAM-MODEL-001).
 *
 * AWS requires a profile's grant to name the foundation model in each Region the profile routes
 * to, which is why that resource carries a `*` Region. What bounds it is the condition: the
 * foundation model is reachable only through this stack's own profile, never directly and never
 * through another profile (`bedrock:InferenceProfileArn`, an ARN-typed condition key of
 * InvokeModel and InvokeModelWithResponseStream; AWS service authorization reference, bedrock
 * v1.4, read 2026-10-04).
 */
import * as cdk from 'aws-cdk-lib';
import * as iam from 'aws-cdk-lib/aws-iam';

import {
  BEDROCK_PROVIDER_NAMESPACES,
  bedrockBaseModelId,
  isInferenceProfileId,
  PROFILE_GEOGRAPHIES,
} from './config';

/**
 * Refuse an ID shaped like a profile whose geography is not listed: `<a>.<b>.<…>` with `<a>` not
 * a listed geography and `<b>` a provider namespace. Granted as a bare model, it would name a
 * foundation model that does not exist and fail on every call.
 */
function refuseUnlistedGeography(modelId: string): void {
  const [first, second, ...rest] = modelId.split('.');
  if (
    rest.length > 0
    && first !== undefined
    && second !== undefined
    && !(PROFILE_GEOGRAPHIES as readonly string[]).includes(first)
    && (BEDROCK_PROVIDER_NAMESPACES as readonly string[]).includes(second)
  ) {
    throw new Error(
      `Model ID ${modelId} looks like an inference profile with geography "${first}", which `
      + 'PROFILE_GEOGRAPHIES does not list. Confirm the prefix with "aws bedrock '
      + 'list-inference-profiles --type-equals SYSTEM_DEFINED", then add it to '
      + 'PROFILE_GEOGRAPHIES in infra/lib/config/index.ts.',
    );
  }
}

/**
 * The ARN an L2 construct's `IModel.modelArn` should name for `modelId`: the profile in this
 * stack's Region for a profile ID, else the foundation model in this Region. The same first
 * resource `modelInvokeStatements` grants.
 */
export function invocableModelArn(stack: cdk.Stack, modelId: string): string {
  refuseUnlistedGeography(modelId);
  const partition = stack.partition;
  if (isInferenceProfileId(modelId)) {
    return `arn:${partition}:bedrock:${stack.region}:${stack.account}:inference-profile/${modelId}`;
  }
  return `arn:${partition}:bedrock:${stack.region}::foundation-model/${modelId}`;
}

/**
 * Allow statements to invoke `modelIds`. A profile ID: the profile in this stack's Region, and its
 * foundation model in any Region only through that profile. Any other ID: the foundation model in
 * this stack's Region. One statement per model, so model A's foundation model never pairs with
 * model B's profile; Sids keep minimizePolicies from merging them.
 */
export function modelInvokeStatements(
  stack: cdk.Stack,
  sid: string,
  modelIds: readonly string[],
  actions: readonly string[],
): iam.PolicyStatement[] {
  const partition = stack.partition;
  return [...new Set(modelIds)].flatMap((modelId, n) => {
    refuseUnlistedGeography(modelId);
    if (!isInferenceProfileId(modelId)) {
      return [new iam.PolicyStatement({
        sid: `${sid}Model${n}`,
        actions: [...actions],
        resources: [`arn:${partition}:bedrock:${stack.region}::foundation-model/${modelId}`],
      })];
    }
    const profileArn = `arn:${partition}:bedrock:${stack.region}:${stack.account}:inference-profile/${modelId}`;
    return [
      new iam.PolicyStatement({
        sid: `${sid}Profile${n}`,
        actions: [...actions],
        resources: [profileArn],
      }),
      new iam.PolicyStatement({
        sid: `${sid}Model${n}`,
        actions: [...actions],
        resources: [`arn:${partition}:bedrock:*::foundation-model/${bedrockBaseModelId(modelId)}`],
        conditions: { ArnEquals: { 'bedrock:InferenceProfileArn': profileArn } },
      }),
    ];
  });
}
