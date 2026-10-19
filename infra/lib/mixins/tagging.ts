// SPDX-License-Identifier: MIT-0
/**
 * Cross-cutting concerns applied once at the app or stack level.
 *
 * Per ADR-005 and the CDK steering: tagging, encryption, log retention and
 * removal policy are mixins, never set per construct. Setting them per
 * construct is how one resource ends up untagged or unencrypted.
 */

import { Aspects, IAspect, RemovalPolicy, Tags } from 'aws-cdk-lib';
import { CfnResource } from 'aws-cdk-lib';
import { Construct, IConstruct } from 'constructs';
import type { AppConfig } from '../config';

/**
 * The standard tag set. Exported for a resource the Tags aspect cannot reach, such as one a
 * custom resource creates through an API; it passes the same set explicitly.
 */
export function standardTags(config: AppConfig): Record<string, string> {
  return { Application: 'adlytics-ai', Environment: config.envName, ManagedBy: 'cdk' };
}

/**
 * Request-tag condition: the create must carry exactly the standard tag set, no more and no
 * other value. `StringEquals` on each key requires it present with that value; `ForAllValues` on
 * `aws:TagKeys` refuses any extra key (H-IAM-AGENTCORE-CREATE-001).
 */
export function requestTagCondition(config: AppConfig): Record<string, Record<string, unknown>> {
  const tags = standardTags(config);
  return {
    StringEquals: Object.fromEntries(Object.entries(tags).map(([key, value]) => [`aws:RequestTag/${key}`, value])),
    'ForAllValues:StringEquals': { 'aws:TagKeys': Object.keys(tags) },
  };
}

/**
 * TagResource: the request carries exactly the standard set, and the target carries no other
 * application's or environment's tags. IfExists admits tag-on-create, when the target has no tags.
 */
export function tagOwnResourceCondition(config: AppConfig): Record<string, Record<string, unknown>> {
  const tags = standardTags(config);
  return {
    ...requestTagCondition(config),
    StringEqualsIfExists: {
      'aws:ResourceTag/Application': tags.Application,
      'aws:ResourceTag/Environment': tags.Environment,
    },
  };
}

/** Applied to every stack so cost allocation and ownership are never optional. */
export function applyStandardTags(scope: Construct, config: AppConfig): void {
  for (const [key, value] of Object.entries(standardTags(config))) Tags.of(scope).add(key, value);
}

/**
 * Forces a removal policy across a scope.
 *
 * Stateful stacks (Foundation, Data) use RETAIN so a stack delete cannot destroy
 * customer data. Stateless stacks use DESTROY. See repo-structure.md section 3.
 */
/**
 * CloudFormation types a RETAIN sweep must not touch.
 *
 * A retained pointer is not a saved asset, it is a blocked redeploy: an SSM
 * parameter left behind by a deleted stack makes the next deploy fail with "already
 * exists", on a resource holding no data worth keeping. Retaining the bucket is the
 * point of RETAIN; retaining the parameter naming the bucket defeats recovery.
 *
 * An Athena workgroup is the same shape of thing. It is configuration — a scan cutoff, a
 * result location, an engine version — and the query output it points at lives in the
 * results bucket, which is retained separately. Its query history is discarded with it, and
 * that is acceptable because the run ledger is the audit trail
 * (docs/capabilities/publication-state/low-level-design.md §6),
 * not Athena's 45-day list.
 *
 * A table optimizer is configuration attached to a table, and the table is retained. A retained
 * optimizer whose table was deleted is a schedule pointing at nothing, and it blocks the next
 * deploy with "already exists" — while the files it would have compacted sit safely in the
 * retained curated bucket either way.
 */
const NEVER_RETAINED = [
  'AWS::SSM::Parameter',
  'AWS::Athena::WorkGroup',
  'AWS::Glue::TableOptimizer',
];

export class RemovalPolicyAspect implements IAspect {
  constructor(private readonly policy: RemovalPolicy) {}

  visit(node: IConstruct): void {
    if (!(node instanceof CfnResource)) {
      return;
    }
    if (this.policy === RemovalPolicy.RETAIN && NEVER_RETAINED.includes(node.cfnResourceType)) {
      return;
    }
    node.applyRemovalPolicy(this.policy);
  }
}

export function applyRemovalPolicy(scope: Construct, policy: RemovalPolicy): void {
  Aspects.of(scope).add(new RemovalPolicyAspect(policy));
}
