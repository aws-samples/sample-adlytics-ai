// SPDX-License-Identifier: MIT-0
/**
 * No synthesised policy may grant an action that attaches an invocation source to a function or
 * changes its code, configuration or invoke policy (H-EVALUATION-RESULT-001 residual 1).
 *
 * The result ingress and the gate providers are reachable only by the subscription and the
 * deployment their resource policies name. A role that could add an event-source mapping, a
 * permission, a URL, a destination, a version or an alias, or replace the code, could reach them
 * another way. No application role needs any of these on any function, so the list is refused
 * outright, with no resource narrowing; a future need is a reviewed change of this list.
 *
 * The structural walk and glob matcher are shared with `iam-forbidden-actions.test.ts`.
 */

export interface Statement {
  readonly Effect?: unknown;
  readonly Action?: unknown;
  readonly NotAction?: unknown;
}

/**
 * The 16 Lambda actions, in the order the probe simulates them. The first eight are the ones the
 * function-change rule watches by CloudTrail name (record-integrity.ts), minus CreateFunction; the
 * other eight close the neighbouring routes. Resource types and condition keys are from the
 * Lambda service authorization reference [verified 2026-10-02:
 * https://servicereference.us-east-1.amazonaws.com/v1/lambda/lambda.json].
 */
export const INVOKE_SOURCE_ACTIONS = [
  'lambda:CreateEventSourceMapping',
  'lambda:UpdateEventSourceMapping',
  'lambda:AddPermission',
  'lambda:RemovePermission',
  'lambda:PutResourcePolicy',
  'lambda:DeleteResourcePolicy',
  'lambda:UpdateFunctionCode',
  'lambda:UpdateFunctionConfiguration',
  'lambda:PutFunctionEventInvokeConfig',
  'lambda:UpdateFunctionEventInvokeConfig',
  'lambda:CreateFunctionUrlConfig',
  'lambda:UpdateFunctionUrlConfig',
  'lambda:DeleteFunction',
  'lambda:PublishVersion',
  'lambda:CreateAlias',
  'lambda:UpdateAlias',
] as const;

/**
 * Every policy document anywhere in a template.
 *
 * Found by structure — any object carrying a `Statement` array — rather than by looking under
 * the resource types that hold one today. Inline policies render as `AWS::IAM::Policy`, but a
 * role can carry `Policies`, a managed policy carries its own document, and a resource policy
 * is a document on something else entirely. Enumerating those types means the gate stops seeing
 * a shape nobody thought of, which is the shape a mistake arrives in.
 */
export function policyDocuments(node: unknown, found: Statement[][] = []): Statement[][] {
  if (Array.isArray(node)) {
    for (const item of node) policyDocuments(item, found);
    return found;
  }
  if (node === null || typeof node !== 'object') return found;
  const record = node as Record<string, unknown>;
  if (Array.isArray(record.Statement)) {
    found.push(record.Statement as Statement[]);
  }
  for (const value of Object.values(record)) policyDocuments(value, found);
  return found;
}

/** Action entries as strings, dropping anything unresolved rather than guessing at it. */
export function actionsOf(value: unknown): string[] {
  const raw = Array.isArray(value) ? value : [value];
  return raw.filter((entry): entry is string => typeof entry === 'string');
}

/**
 * Does a granted action pattern cover this action name?
 *
 * The pattern is the glob, not the name — that direction is the whole point. IAM wildcards are
 * `*` and `?`, and action matching is case-insensitive, so `bedrock-agentcore:invoke*` grants
 * just as much as the tidily-capitalised version.
 */
export function grants(pattern: string, actionName: string): boolean {
  const escaped = pattern.replace(/[.+^${}()|[\]\\]/g, '\\$&');
  const expanded = escaped.replace(/\*/g, '.*').replace(/\?/g, '.');
  return new RegExp(`^${expanded}$`, 'i').test(actionName);
}

/** `"<stack>/<logicalId>: grants <action>"` for each Allow that covers an invocation-source action. */
export function invokeSourceGrants(stackName: string, template: Record<string, unknown>): string[] {
  const resources = (template.Resources ?? {}) as Record<string, unknown>;
  return Object.entries(resources).flatMap(([logicalId, resource]) =>
    policyDocuments(resource)
      .flat()
      .filter((statement) => statement.Effect === 'Allow')
      .flatMap((statement) =>
        INVOKE_SOURCE_ACTIONS.filter((action) =>
          actionsOf(statement.Action).some((pattern) => grants(pattern, action)),
        ).map((action) => `${stackName}/${logicalId}: grants ${action}`),
      ),
  );
}
