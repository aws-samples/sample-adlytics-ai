// SPDX-License-Identifier: MIT-0
/**
 * Lambda execution roles that write only their own declared log group (ADR-085).
 *
 * A `lambda.Function` without a `role` creates one with the AWS-managed
 * `AWSLambdaBasicExecutionRole`, which allows `logs:CreateLogGroup`, `logs:CreateLogStream` and
 * `logs:PutLogEvents` on `*`: any function could then write any log group, the evaluation result
 * group included (M-IAM-DISPATCH-LOGS-001). Every function role is built here instead, with no
 * managed policy, and is granted stream writes on the one group its function logs to.
 *
 * One grant site per function: whoever declares the function's log group calls
 * `grantOwnLogWrites`, whether the role was passed in or created by a helper.
 */
import * as iam from 'aws-cdk-lib/aws-iam';
import type * as logs from 'aws-cdk-lib/aws-logs';
import type { Construct } from 'constructs';

/** A Lambda execution role with no managed policy; grants are added where they are known. */
export function lambdaRole(
  scope: Construct,
  id: string,
  props: { roleName?: string; description?: string } = {},
): iam.Role {
  return new iam.Role(scope, id, {
    assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
    ...(props.roleName === undefined ? {} : { roleName: props.roleName }),
    ...(props.description === undefined ? {} : { description: props.description }),
  });
}

/**
 * Let a function's role write only its own declared log group.
 *
 * No `logs:CreateLogGroup`: the group is declared before the function exists, and a group deleted
 * out of band is then not silently recreated without retention; logging stops instead.
 */
export function grantOwnLogWrites(role: iam.IRole, logGroup: logs.ILogGroup): void {
  role.addToPrincipalPolicy(new iam.PolicyStatement({
    actions: ['logs:CreateLogStream', 'logs:PutLogEvents'],
    resources: [`${logGroup.logGroupArn}:*`],
  }));
}
