// SPDX-License-Identifier: MIT-0
import * as cdk from 'aws-cdk-lib';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';

import {
  API_PREFIX,
  AppConfig,
  EDGE_DEFAULT_MAX_BODY_BYTES,
  EDGE_SECURITY_REGION,
  PROMPT_CREATE_VERSION_MAX_BODY_BYTES,
  paramPath,
} from '../config';
import { ExactRoute, GuardedWebAcl, WebAclRule } from '../constructs/edge/guarded-web-acl';
import { applyRemovalPolicy, applyStandardTags } from '../mixins/tagging';

export interface EdgeSecurityStackProps extends cdk.StackProps {
  readonly config: AppConfig;
}

/** The one request shape exempt from the 8 KB body refusal: prompt create-version, no query string. */
export const PROMPT_CREATE_VERSION_ROUTE: ExactRoute = {
  method: 'POST',
  path: `${API_PREFIX}/prompt-registry/create-version`,
};

/** The distribution ACL's body inspection limit; it must cover the create-version ceiling. */
export const EDGE_BODY_INSPECTION_LIMIT = 'KB_32';

/**
 * The distribution's rules, in evaluation order (ADR-078, authentication.md edge entry).
 *
 * Exported so the stack test and the live acceptance read one statement of each rule's mode.
 * Every rule blocks since 2026-09-29, after a full dev probe run through the distribution in
 * count mode matched no legitimate request (validation/RESULTS.md, edge slice).
 *
 * The managed `SizeRestrictions_BODY` rule counts, and two label-free size rules take over its
 * refusal: 8,192 bytes everywhere except exact create-version, and 32,768 bytes on that route
 * (ADR-078, "Refined 2026-10-05: path-scoped body-size exclusion"). Both block from the first
 * deploy, because they only re-impose refusals the ACL already applied in block mode.
 */
export function edgeWebAclRules(config: AppConfig): readonly WebAclRule[] {
  return [
    { kind: 'managed', name: 'AwsIpReputation', managedGroup: 'AWSManagedRulesAmazonIpReputationList', mode: 'block' },
    { kind: 'managed', name: 'AwsKnownBadInputs', managedGroup: 'AWSManagedRulesKnownBadInputsRuleSet', mode: 'block' },
    {
      kind: 'managed',
      name: 'AwsCommonRuleSet',
      managedGroup: 'AWSManagedRulesCommonRuleSet',
      mode: 'block',
      countRules: ['SizeRestrictions_BODY'],
    },
    {
      kind: 'body-size',
      name: 'PromptCreateBodyCeiling',
      maxBodyBytes: PROMPT_CREATE_VERSION_MAX_BODY_BYTES,
      route: { only: PROMPT_CREATE_VERSION_ROUTE },
      mode: 'block',
    },
    {
      kind: 'body-size',
      name: 'BodySizeOutsidePromptCreate',
      maxBodyBytes: EDGE_DEFAULT_MAX_BODY_BYTES,
      route: { except: PROMPT_CREATE_VERSION_ROUTE },
      mode: 'block',
    },
    {
      kind: 'rate',
      name: 'RateLimitPerSourceIp',
      limitPerFiveMinutes: config.edge.wafRateLimitPerFiveMinutes,
      mode: 'block',
    },
  ];
}

/**
 * The `CLOUDFRONT`-scope web ACL in front of every customer-facing request (ADR-078).
 *
 * Its own stack because such an ACL can be created only in us-east-1, whatever the application
 * region; `bin/app.ts` pins this stack there. It publishes the ACL ARN as an SSM parameter in
 * us-east-1, which `FrontendStack` reads — directly in us-east-1, and through one SDK-call custom
 * resource pinned to us-east-1 anywhere else. Ordered before `FrontendStack` in DEPLOY_ORDER.
 *
 * Destroyable: it holds rules and a log of matched requests, nothing a customer would restore.
 */
export class EdgeSecurityStack extends cdk.Stack {
  public readonly webAcl: GuardedWebAcl;

  constructor(scope: Construct, id: string, props: EdgeSecurityStackProps) {
    super(scope, id, props);
    applyStandardTags(this, props.config);
    const { config } = props;
    if (!cdk.Token.isUnresolved(this.region) && this.region !== EDGE_SECURITY_REGION) {
      throw new Error(`EdgeSecurityStack must be deployed to ${EDGE_SECURITY_REGION}.`);
    }

    this.webAcl = new GuardedWebAcl(this, 'DistributionAcl', {
      name: `adlytics-${config.envName}-edge`,
      scope: 'CLOUDFRONT',
      rules: edgeWebAclRules(config),
      bodyInspectionLimit: EDGE_BODY_INSPECTION_LIMIT,
      logRetention: config.logRetentionDays,
    });

    new ssm.StringParameter(this, 'ParamEdgeWebAclArn', {
      parameterName: paramPath(config.envName, 'edgeWebAclArn'),
      stringValue: this.webAcl.acl.attrArn,
      description: 'The distribution web ACL (ADR-078); read by FrontendStack.',
    });

    applyRemovalPolicy(this, cdk.RemovalPolicy.DESTROY);
  }
}
