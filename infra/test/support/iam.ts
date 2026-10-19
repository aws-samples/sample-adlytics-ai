// SPDX-License-Identifier: MIT-0
/**
 * Read a synthesised template's grants per role, with every resource resolved to the parameter
 * path or physical table it names.
 *
 * A grant's resource is usually `{ Ref: <SSM parameter> }` whose default is the published PARAMS
 * path, or a `Fn::Join` over one. Resolving both to a readable string lets a test assert the exact
 * `(action, table)` pairs a role holds instead of searching the rendered JSON for a substring.
 */
import { grants } from './invoke-sources';

type Resource = { Type?: unknown; Properties?: Record<string, unknown> };
type Template = Record<string, unknown>;

function resourcesOf(template: Template): Record<string, Resource> {
  return (template.Resources ?? {}) as Record<string, Resource>;
}

function parameterDefaults(template: Template): Record<string, string> {
  const parameters = (template.Parameters ?? {}) as Record<string, { Default?: unknown }>;
  return Object.fromEntries(
    Object.entries(parameters)
      .filter(([, parameter]) => typeof parameter.Default === 'string')
      .map(([logicalId, parameter]) => [logicalId, parameter.Default as string]),
  );
}

function resolve(value: unknown, defaults: Record<string, string>): string {
  if (typeof value === 'string') return value;
  if (value !== null && typeof value === 'object') {
    const record = value as Record<string, unknown>;
    if (typeof record.Ref === 'string') return defaults[record.Ref] ?? `{${record.Ref}}`;
    const join = record['Fn::Join'];
    if (Array.isArray(join) && typeof join[0] === 'string' && Array.isArray(join[1])) {
      return (join[1] as unknown[]).map((part) => resolve(part, defaults)).join(join[0]);
    }
  }
  return JSON.stringify(value);
}

/** A physical ARN reduces to its `table/...` suffix; a parameter path is kept whole. */
function tableOf(resolved: string): string {
  const at = resolved.indexOf(':table/');
  return at >= 0 && !resolved.startsWith('/adlytics/') ? resolved.slice(at + 1) : resolved;
}

const list = (value: unknown): unknown[] => (Array.isArray(value) ? value : [value]);

type PolicyDocument = { Statement?: Record<string, unknown>[] };

/** Every policy document attached to one role: its policies, its managed policies, and inline. */
function documentsOf(template: Template, role: string): PolicyDocument[] {
  const documents: PolicyDocument[] = [];
  for (const [logicalId, resource] of Object.entries(resourcesOf(template))) {
    if (resource.Type === 'AWS::IAM::Policy' || resource.Type === 'AWS::IAM::ManagedPolicy') {
      const roles = (resource.Properties?.Roles ?? []) as { Ref?: unknown }[];
      if (roles.some((candidate) => candidate.Ref === role)) {
        documents.push(resource.Properties?.PolicyDocument as PolicyDocument);
      }
    }
    if (resource.Type === 'AWS::IAM::Role' && logicalId === role) {
      const inline = (resource.Properties?.Policies ?? []) as { PolicyDocument?: PolicyDocument }[];
      documents.push(...inline.flatMap((policy) => (policy.PolicyDocument ? [policy.PolicyDocument] : [])));
    }
  }
  return documents;
}

/** Every allowed `<action> <resource>` pair attached to one role logical id. */
function grantsOf(template: Template, role: string): Set<string> {
  const defaults = parameterDefaults(template);
  const grants = new Set<string>();
  for (const document of documentsOf(template, role)) {
    for (const statement of document.Statement ?? []) {
      if (statement.Effect !== 'Allow') continue;
      for (const action of list(statement.Action)) {
        if (typeof action !== 'string') continue;
        for (const target of list(statement.Resource)) {
          grants.add(`${action} ${tableOf(resolve(target, defaults))}`);
        }
      }
    }
  }
  return grants;
}

/** The logical id of the role with this physical name, or undefined when the template has none. */
export function roleLogicalId(template: Template, roleName: string): string | undefined {
  return Object.entries(resourcesOf(template)).find(
    ([, resource]) => resource.Type === 'AWS::IAM::Role' && resource.Properties?.RoleName === roleName,
  )?.[0];
}

/** Every allowed `dynamodb:<Action> <table>` pair the role with this physical name holds. */
export function dynamoGrants(template: Template, roleName: string): Set<string> {
  const role = roleLogicalId(template, roleName);
  if (role === undefined) throw new Error(`no role named ${roleName}`);
  return new Set([...grantsOf(template, role)].filter((grant) => grant.startsWith('dynamodb:')));
}

/** Every role, across every template, allowed to put an item into a table `names` matches. */
export function rolesThatPutInto(
  templates: Record<string, unknown>,
  names: (table: string) => boolean,
): Set<string> {
  const puts = new Set(['dynamodb:PutItem', 'dynamodb:BatchWriteItem', 'dynamodb:*', '*']);
  const writers = new Set<string>();
  for (const template of Object.values(templates) as Template[]) {
    for (const [logicalId, resource] of Object.entries(resourcesOf(template))) {
      if (resource.Type !== 'AWS::IAM::Role') continue;
      for (const grant of grantsOf(template, logicalId)) {
        const [action, table] = grant.split(' ', 2) as [string, string];
        if (puts.has(action) && names(table)) {
          writers.add(String(resource.Properties?.RoleName ?? logicalId));
        }
      }
    }
  }
  return writers;
}

/** Every role, across every template, allowed to read an item from a table or index `names` matches. */
export function rolesThatReadFrom(
  templates: Record<string, unknown>,
  names: (table: string) => boolean,
): Set<string> {
  const reads = [
    'dynamodb:GetItem',
    'dynamodb:BatchGetItem',
    'dynamodb:Query',
    'dynamodb:Scan',
    'dynamodb:ConditionCheckItem',
    'dynamodb:PartiQLSelect',
  ];
  // An IAM action pattern matches case-insensitively, with `*` and `?` as wildcards.
  const grantsRead = (action: string): boolean => {
    const pattern = new RegExp(
      `^${action.replace(/[.+^${}()|[\]\\]/g, '\\$&').replace(/\*/g, '.*').replace(/\?/g, '.')}$`,
      'i',
    );
    return reads.some((read) => pattern.test(read));
  };
  const readers = new Set<string>();
  for (const template of Object.values(templates) as Template[]) {
    for (const [logicalId, resource] of Object.entries(resourcesOf(template))) {
      if (resource.Type !== 'AWS::IAM::Role') continue;
      for (const grant of grantsOf(template, logicalId)) {
        const [action, table] = grant.split(' ', 2) as [string, string];
        if (grantsRead(action) && names(table)) {
          readers.add(String(resource.Properties?.RoleName ?? logicalId));
        }
      }
    }
  }
  return readers;
}
/** Every allowed `<action> <resource>` pair the role with this physical name holds, any service. */
export function roleGrants(template: Template, roleName: string): Set<string> {
  const role = roleLogicalId(template, roleName);
  if (role === undefined) throw new Error(`no role named ${roleName}`);
  return grantsOf(template, role);
}

/**
 * Every allowed `<action> <resource>` pair one role logical id holds, any service.
 *
 * For roles CDK names itself (a Lambda's service role, `SemanticProjectionOperatorRole`): they
 * carry no physical `RoleName`, so `roleGrants` cannot find them.
 */
export function grantsOfRoleLogicalId(template: Template, logicalId: string): Set<string> {
  return grantsOf(template, logicalId);
}

/**
 * The role's S3 object grants inside one results bucket, as sorted `<action> <key prefix>`.
 *
 * `bucket` is the resolved bucket text: a physical name, or the SSM parameter path a stack reads
 * it from. `s3:GetObject bake/*` reads "GetObject on `<bucket>/bake/*`".
 */
export function resultPrefixGrants(template: Template, logicalId: string, bucket: string): string[] {
  const marker = `:s3:::${bucket}/`;
  return [...grantsOf(template, logicalId)]
    .filter((grant) => grant.startsWith('s3:') && grant.includes(marker))
    .map((grant) => {
      const [action, resource] = grant.split(' ', 2) as [string, string];
      return `${action} ${resource.slice(resource.indexOf(marker) + marker.length)}`;
    })
    .sort();
}

/** Whether a granted resource covers one Athena workgroup, wildcards resolved as IAM does. */
function coversWorkgroup(resource: string, workgroupName: string): boolean {
  if (resource === '*') return true;
  const at = resource.indexOf(':workgroup/');
  if (at >= 0) return grants(resource.slice(at + ':workgroup/'.length), workgroupName);
  // An Athena ARN whose resource part is a bare wildcard (`arn:...:athena:<region>:<account>:*`).
  return /:athena:[^:]*:[^:]*:\*$/.test(resource);
}

/**
 * Every role, across every template, allowed `athena:StartQueryExecution` on one workgroup, as
 * `<template key>/<role logical id>`. Action and resource wildcards (`athena:*`, `workgroup/*`)
 * are resolved with the shared IAM glob matcher, so a broad grant is never missed.
 */
export function athenaStartRoles(templates: Record<string, unknown>, workgroupName: string): Set<string> {
  const roles = new Set<string>();
  for (const [stack, template] of Object.entries(templates) as [string, Template][]) {
    for (const [logicalId, resource] of Object.entries(resourcesOf(template))) {
      if (resource.Type !== 'AWS::IAM::Role') continue;
      for (const grant of grantsOf(template, logicalId)) {
        const [action, target] = grant.split(' ', 2) as [string, string];
        if (grants(action, 'athena:StartQueryExecution') && coversWorkgroup(target, workgroupName)) {
          roles.add(`${stack}/${logicalId}`);
        }
      }
    }
  }
  return roles;
}

/** The `s3:prefix` values every `s3:ListBucket` the role holds on one bucket is conditioned on. */
export function listBucketPrefixes(template: Template, logicalId: string, bucket: string): string[] {
  const defaults = parameterDefaults(template);
  const prefixes = new Set<string>();
  for (const document of documentsOf(template, logicalId)) {
    for (const statement of document.Statement ?? []) {
      if (statement.Effect !== 'Allow' || !list(statement.Action).includes('s3:ListBucket')) continue;
      const onBucket = list(statement.Resource).some((target) =>
        resolve(target, defaults).endsWith(`:s3:::${bucket}`),
      );
      if (!onBucket) continue;
      const like = (statement.Condition as Record<string, Record<string, unknown>> | undefined)
        ?.StringLike?.['s3:prefix'];
      for (const prefix of list(like ?? '<unconditioned>')) prefixes.add(String(prefix));
    }
  }
  return [...prefixes].sort();
}
