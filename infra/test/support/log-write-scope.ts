// SPDX-License-Identifier: MIT-0
/**
 * Log writes stay inside the writer's own group, and nothing reaches the evaluation result group
 * except the evaluator's own pinned grant (M-IAM-DISPATCH-LOGS-001, H-EVALUATION-RESULT-001,
 * ADR-085).
 *
 * Each predicate is a pure function of one synthesised template and returns the exact findings,
 * so a test asserts one rule's whole output:
 *
 * - R1 `r1ManagedPolicies`: no role, user or group attaches a managed policy the gate cannot
 *   read (an AWS-managed policy is refused by class, not by name).
 * - R2 `r2LogWrites`: every log write is on a same-template log group, or is a pinned grant.
 * - R3 `r3Write`, `r3Read`, `r3Custody`, `r3Unscoped`: what may reach the result group, judged
 *   by what each statement's actions can do.
 * - R4 `r4ResourcePolicies`: no Logs resource policy lets a service write or read it.
 * - `ownLogGroupControl`: the positive control, every function can write its own group.
 * - `unusedPins`: a pin no template uses is stale and fails.
 *
 * The probe's custody acceptance (validation/probe_evaluation_custody.py) checks the same action
 * classes live; its parity test reads READ, CUSTODY_SCOPED and CUSTODY_UNSCOPED from here.
 */
import {
  type EvaluationFamily,
  EVALUATION_FAMILIES,
  evaluationResultLogGroupName,
  harnessName,
  paramPath,
} from '../../lib/config';
import { RESULT_GROUP_PARAMETER } from '../../lib/constructs/agent-quality/evaluation-gate';
import { TEST_CONFIG } from './config';

type CfnTemplate = Record<string, unknown>;
type Json = Record<string, unknown>;
type CfnResource = { Type?: unknown; Properties?: Json };

/** The environment the result group's name and ARN are computed for. */
export interface ScopeContext {
  readonly envName: string;
  readonly region: string;
  readonly account: string;
}

export const DEFAULT_CONTEXT: ScopeContext = {
  envName: TEST_CONFIG.envName,
  region: TEST_CONFIG.region,
  account: TEST_CONFIG.account,
};

/** The three actions that write log events or create the group they land in. */
export const WRITE = ['logs:CreateLogGroup', 'logs:CreateLogStream', 'logs:PutLogEvents'] as const;

/**
 * The seven non-write CloudWatch Logs actions that return a group's event content, per the AWS
 * service authorization reference for CloudWatch Logs [verified 2026-10-02:
 * https://servicereference.us-east-1.amazonaws.com/v1/logs/logs.json].
 */
export const READ = [
  'logs:GetLogEvents',
  'logs:FilterLogEvents',
  'logs:StartQuery',
  'logs:GetQueryResults',
  'logs:StartLiveTail',
  'logs:GetLogRecord',
  'logs:Unmask',
] as const;

/** The 13 log-group-scoped actions that change who receives, keeps or deletes a group's events. */
export const CUSTODY_SCOPED = [
  'logs:PutSubscriptionFilter',
  'logs:DeleteSubscriptionFilter',
  'logs:CreateExportTask',
  'logs:AssociateKmsKey',
  'logs:DisassociateKmsKey',
  'logs:PutRetentionPolicy',
  'logs:DeleteRetentionPolicy',
  'logs:DeleteLogGroup',
  'logs:PutLogGroupDeletionProtection',
  'logs:PutDataProtectionPolicy',
  'logs:DeleteDataProtectionPolicy',
  'logs:PutTransformer',
  'logs:DeleteTransformer',
] as const;

/**
 * Custody actions that take no resource type able to exclude the result group, so any grant of
 * them can open any log group to a delivering service.
 */
export const CUSTODY_UNSCOPED = [
  'logs:PutResourcePolicy',
  'logs:PutAccountPolicy',
  'logs:DeleteAccountPolicy',
  'logs:PutDeliveryDestination',
] as const;

/**
 * Managed policies a role may attach although the gate cannot read them. Each entry is a reviewed
 * exception with its reason; there are none (ADR-085 removed the last).
 */
export const MANAGED_POLICY_PINS: readonly { arnText: string; reason: string }[] = [];

/** A reviewed log write that is not on the writer's own group: one role, exact resources. */
export interface LogWritePin {
  readonly name: string;
  readonly stack: string;
  readonly role: RegExp;
  readonly resources: readonly string[];
}

const PSEUDO = (context: ScopeContext): Record<string, string> => ({
  'AWS::Partition': 'aws',
  'AWS::Region': context.region,
  'AWS::AccountId': context.account,
  'AWS::URLSuffix': 'amazonaws.com',
});

function logsArn(context: ScopeContext, groupName: string): string {
  return `arn:aws:logs:${context.region}:${context.account}:log-group:${groupName}`;
}

/** The four cross-template or pattern log grants, computed from the helpers the stacks use. */
export function logWritePins(context: ScopeContext = DEFAULT_CONTEXT): readonly LogWritePin[] {
  const ssm = (path: string): string => `{ssm:${path}}`;
  const result = logsArn(context, ssm(paramPath(context.envName, RESULT_GROUP_PARAMETER.v4)));
  const input = logsArn(context, ssm(paramPath(context.envName, 'evaluationInputLogGroupName')));
  const runtimes = logsArn(context, '/aws/bedrock-agentcore/runtimes/*');
  return [
    {
      name: 'GateV4ExecutionRole',
      stack: 'EvaluationV4Stack',
      role: /^GateV4ExecutionRole[0-9A-F]{8}$/,
      resources: [result, `${result}:*`],
    },
    {
      name: 'WriterRole',
      stack: 'EvaluationInputStack',
      role: /^WriterRole[0-9A-F]{8}$/,
      resources: [input, `${input}:log-stream:*`],
    },
    {
      name: 'HarnessExecutionRole',
      stack: 'HarnessStack',
      role: /^ExecutionRole[0-9A-F]{8}$/,
      resources: [runtimes, `${runtimes}:log-stream:*`],
    },
    {
      name: 'HarnessProviderRole',
      stack: 'HarnessStack',
      role: /^HarnessProviderRole[0-9A-F]{8}$/,
      resources: [
        logsArn(context, `/aws/bedrock-agentcore/runtimes/harness_${harnessName(context.envName)}-*`),
      ],
    },
  ];
}

export const LOG_WRITE_PINS = logWritePins();

// --- Rendering ---------------------------------------------------------------------------------

const list = (value: unknown): unknown[] => (Array.isArray(value) ? value : [value]);
const isObject = (value: unknown): value is Json =>
  value !== null && typeof value === 'object' && !Array.isArray(value);

function resourcesOf(template: CfnTemplate): Record<string, CfnResource> {
  return (template.Resources ?? {}) as Record<string, CfnResource>;
}

function ssmDefaults(template: CfnTemplate): Record<string, string> {
  const parameters = (template.Parameters ?? {}) as Record<string, Json>;
  return Object.fromEntries(
    Object.entries(parameters)
      .filter(([, p]) => p.Type === 'AWS::SSM::Parameter::Value<String>' && typeof p.Default === 'string')
      .map(([id, p]) => [id, p.Default as string]),
  );
}

/**
 * Render a template value as text. Pseudo parameters resolve (pseudo-parameter flattening); an
 * SSM parameter becomes `{ssm:<path>}`, a GetAtt `{GetAtt:<id>.<attr>}`, any other Ref
 * `{Ref:<id>}`. A value with no `{` left is a flat string an IAM glob can be matched against.
 */
function render(value: unknown, template: CfnTemplate, context: ScopeContext): string {
  if (typeof value === 'string') return value;
  if (!isObject(value)) return JSON.stringify(value);
  const pseudo = PSEUDO(context);
  if (typeof value.Ref === 'string') {
    const ref = value.Ref;
    const ssm = ssmDefaults(template)[ref];
    return pseudo[ref] ?? (ssm === undefined ? `{Ref:${ref}}` : `{ssm:${ssm}}`);
  }
  const getAtt = value['Fn::GetAtt'];
  if (Array.isArray(getAtt)) return `{GetAtt:${String(getAtt[0])}.${String(getAtt[1])}}`;
  const join = value['Fn::Join'];
  if (Array.isArray(join) && typeof join[0] === 'string' && Array.isArray(join[1])) {
    return (join[1] as unknown[]).map((part) => render(part, template, context)).join(join[0]);
  }
  const sub = value['Fn::Sub'];
  if (typeof sub === 'string') {
    return sub.replace(/\$\{([^}]+)\}/g, (_match, name: string) => pseudo[name] ?? `{Ref:${name}}`);
  }
  return JSON.stringify(value);
}

function globRegExp(pattern: string, flags: string): RegExp {
  const body = pattern.replace(/[.+^${}()|[\]\\]/g, '\\$&').replace(/\*/g, '.*').replace(/\?/g, '.');
  return new RegExp(`^${body}$`, flags);
}

/** IAM action globs match case-insensitively. */
function actionMatches(pattern: unknown, action: string): boolean {
  return typeof pattern === 'string' && globRegExp(pattern, 'i').test(action);
}

/** The members of `members` a statement covers, through Action, NotAction, or neither. */
function covered(statement: Json, members: readonly string[]): string[] {
  if (statement.Action !== undefined) {
    return members.filter((m) => list(statement.Action).some((p) => actionMatches(p, m)));
  }
  if (statement.NotAction !== undefined) {
    return members.filter((m) => !list(statement.NotAction).some((p) => actionMatches(p, m)));
  }
  return [...members];
}

// --- Statements --------------------------------------------------------------------------------

interface Located {
  readonly policyId: string;
  readonly roles: readonly string[];
  readonly statement: Json;
}

/** Every IAM statement in the template, with its policy's logical id and the roles it reaches. */
function iamStatements(template: CfnTemplate): Located[] {
  const resources = resourcesOf(template);
  const refs = (value: unknown): string[] =>
    list(value).flatMap((entry) => (isObject(entry) && typeof entry.Ref === 'string' ? [entry.Ref] : []));
  const attachedManaged = (policyId: string): string[] =>
    Object.entries(resources)
      .filter(([, r]) => r.Type === 'AWS::IAM::Role' && refs(r.Properties?.ManagedPolicyArns).includes(policyId))
      .map(([id]) => id);
  const statementsOf = (document: unknown): Json[] =>
    isObject(document) ? list(document.Statement ?? []).filter(isObject) : [];
  const found: Located[] = [];
  for (const [id, resource] of Object.entries(resources)) {
    const props = resource.Properties ?? {};
    if (resource.Type === 'AWS::IAM::Policy' || resource.Type === 'AWS::IAM::ManagedPolicy') {
      const roles = [...refs(props.Roles), ...(resource.Type === 'AWS::IAM::ManagedPolicy' ? attachedManaged(id) : [])];
      for (const statement of statementsOf(props.PolicyDocument)) found.push({ policyId: id, roles, statement });
    } else if (resource.Type === 'AWS::IAM::RolePolicy') {
      for (const statement of statementsOf(props.PolicyDocument)) {
        found.push({ policyId: id, roles: refs(props.RoleName), statement });
      }
    } else if (resource.Type === 'AWS::IAM::Role') {
      for (const policy of list(props.Policies ?? []).filter(isObject)) {
        for (const statement of statementsOf(policy.PolicyDocument)) found.push({ policyId: id, roles: [id], statement });
      }
    }
  }
  return found;
}

const allows = (located: Located): boolean => located.statement.Effect === 'Allow';

/** Whether a rendered resource reaches the result group (the three clauses). */
function renderedReaches(rendered: string, template: CfnTemplate, context: ScopeContext): boolean {
  const resources = resourcesOf(template);
  for (const family of EVALUATION_FAMILIES as readonly EvaluationFamily[]) {
    if (rendered.includes(`{ssm:${paramPath(context.envName, RESULT_GROUP_PARAMETER[family])}}`)) return true;
    const name = evaluationResultLogGroupName(context.envName, family);
    for (const [id, resource] of Object.entries(resources)) {
      if (resource.Type === 'AWS::Logs::LogGroup' && resource.Properties?.LogGroupName === name
        && rendered.includes(`{GetAtt:${id}.Arn}`)) return true;
    }
    if (!rendered.includes('{')) {
      const arn = logsArn(context, name);
      const glob = globRegExp(rendered, '');
      if ([arn, `${arn}:*`, `${arn}:log-stream:x`].some((target) => glob.test(target))) return true;
    }
  }
  return false;
}

function statementReaches(statement: Json, template: CfnTemplate, context: ScopeContext): boolean {
  if (statement.NotResource !== undefined || statement.Resource === undefined) return true;
  return list(statement.Resource).some((r) => renderedReaches(render(r, template, context), template, context));
}

function pinMatches(
  pin: LogWritePin, stackName: string, located: Located, template: CfnTemplate, context: ScopeContext,
): boolean {
  if (pin.stack !== stackName || !located.roles.some((role) => pin.role.test(role))) return false;
  if (located.statement.Resource === undefined) return false;
  return list(located.statement.Resource).every((r) => pin.resources.includes(render(r, template, context)));
}

// --- R1 ----------------------------------------------------------------------------------------

/** R1: every attached managed policy is an in-template document or a reviewed pin. */
export function r1ManagedPolicies(
  stackName: string, template: CfnTemplate, context: ScopeContext = DEFAULT_CONTEXT,
): string[] {
  const resources = resourcesOf(template);
  const found: string[] = [];
  for (const [id, resource] of Object.entries(resources)) {
    if (!['AWS::IAM::Role', 'AWS::IAM::User', 'AWS::IAM::Group'].includes(String(resource.Type))) continue;
    for (const entry of list(resource.Properties?.ManagedPolicyArns ?? [])) {
      if (isObject(entry) && typeof entry.Ref === 'string'
        && resources[entry.Ref]?.Type === 'AWS::IAM::ManagedPolicy') continue;
      const text = render(entry, template, context);
      if (MANAGED_POLICY_PINS.some((pin) => pin.arnText === text)) continue;
      const at = text.indexOf(':iam::aws:policy/');
      found.push(at >= 0
        ? `${stackName}/${id}: attaches AWS-managed policy ${text.slice(at + ':iam::aws:policy/'.length)}`
        : `${stackName}/${id}: attaches a managed policy the gate cannot read`);
    }
  }
  return found;
}

// --- R2 ----------------------------------------------------------------------------------------

function isOwnGroupShape(resource: unknown, template: CfnTemplate): string | undefined {
  const resources = resourcesOf(template);
  const groupOf = (value: unknown): string | undefined => {
    if (!isObject(value)) return undefined;
    const getAtt = value['Fn::GetAtt'];
    if (!Array.isArray(getAtt) || getAtt[1] !== 'Arn' || typeof getAtt[0] !== 'string') return undefined;
    return resources[getAtt[0]]?.Type === 'AWS::Logs::LogGroup' ? getAtt[0] : undefined;
  };
  const direct = groupOf(resource);
  if (direct !== undefined) return direct;
  if (!isObject(resource)) return undefined;
  const join = resource['Fn::Join'];
  if (!Array.isArray(join) || join[0] !== '' || !Array.isArray(join[1]) || join[1].length !== 2) return undefined;
  const [head, tail] = join[1] as [unknown, unknown];
  return tail === ':*' || tail === ':log-stream:*' ? groupOf(head) : undefined;
}

function shapeOf(resource: unknown, template: CfnTemplate, context: ScopeContext): string {
  const rendered = render(resource, template, context);
  return rendered.includes('{') ? JSON.stringify(resource) : JSON.stringify(rendered);
}

/** R2: every log write is on a same-template log group, or is a pinned grant. */
export function r2LogWrites(
  stackName: string,
  template: CfnTemplate,
  context: ScopeContext = DEFAULT_CONTEXT,
  pins: readonly LogWritePin[] = LOG_WRITE_PINS,
): string[] {
  const found: string[] = [];
  for (const located of iamStatements(template).filter(allows)) {
    if (covered(located.statement, WRITE).length === 0) continue;
    const where = `${stackName}/${located.policyId}`;
    const message = (shape: string): string =>
      `${where}: log write on ${shape} is not the writer's own group or a pinned grant`;
    if (located.statement.NotAction !== undefined) {
      found.push(message('NotAction'));
      continue;
    }
    if (located.statement.NotResource !== undefined || located.statement.Resource === undefined) {
      found.push(message('NotResource'));
      continue;
    }
    if (pins.some((pin) => pinMatches(pin, stackName, located, template, context))) continue;
    for (const resource of list(located.statement.Resource)) {
      if (isOwnGroupShape(resource, template) === undefined) found.push(message(shapeOf(resource, template, context)));
    }
  }
  return found;
}

// --- R3 ----------------------------------------------------------------------------------------

/** R3-write: a log write that reaches the result group is the evaluator's execution-role pin. */
export function r3Write(
  stackName: string,
  template: CfnTemplate,
  context: ScopeContext = DEFAULT_CONTEXT,
  pins: readonly LogWritePin[] = LOG_WRITE_PINS,
): string[] {
  const pin = pins.find((candidate) => candidate.name === 'GateV4ExecutionRole');
  return iamStatements(template)
    .filter(allows)
    .filter((located) => covered(located.statement, WRITE).length > 0)
    .filter((located) => statementReaches(located.statement, template, context))
    .filter((located) => pin === undefined || !pinMatches(pin, stackName, located, template, context))
    .map((located) =>
      `${stackName}/${located.policyId}: log write reaches the evaluation result group outside the execution role pin`);
}

function classReach(
  members: readonly string[], message: string,
): (stackName: string, template: CfnTemplate, context?: ScopeContext) => string[] {
  return (stackName, template, context = DEFAULT_CONTEXT) =>
    iamStatements(template)
      .filter(allows)
      .filter((located) => covered(located.statement, members).length > 0)
      .filter((located) => statementReaches(located.statement, template, context))
      .map((located) => `${stackName}/${located.policyId}: ${message}`);
}

/** R3-read: no statement may read the result group's events, pinned or not. */
export const r3Read = classReach(READ, 'log read reaches the evaluation result group');

/** R3-custody: no statement may change the result group's custody. */
export const r3Custody = classReach(CUSTODY_SCOPED, 'custody action reaches the evaluation result group');

/** R3-unscoped: no statement may grant an unscoped custody action, whatever its resource. */
export function r3Unscoped(stackName: string, template: CfnTemplate): string[] {
  return iamStatements(template)
    .filter(allows)
    .flatMap((located) => covered(located.statement, CUSTODY_UNSCOPED).map((action) =>
      `${stackName}/${located.policyId}: grants ${action}, which can open any log group to a delivering service`));
}

// --- R4 ----------------------------------------------------------------------------------------

/** R4: no Logs resource policy lets a service write or read the result group. */
export function r4ResourcePolicies(
  stackName: string, template: CfnTemplate, context: ScopeContext = DEFAULT_CONTEXT,
): string[] {
  const found: string[] = [];
  for (const [id, resource] of Object.entries(resourcesOf(template))) {
    if (resource.Type !== 'AWS::Logs::ResourcePolicy') continue;
    const document = JSON.parse(render(resource.Properties?.PolicyDocument, template, context)) as Json;
    for (const statement of list(document.Statement ?? []).filter(isObject)) {
      if (statement.Effect !== 'Allow') continue;
      if (covered(statement, [...WRITE, ...READ]).length === 0) continue;
      if (statementReaches(statement, template, context)) {
        found.push(`${stackName}/${id}: Logs resource policy reaches the evaluation result group`);
      }
    }
  }
  return found;
}

// --- Positive control and pins -----------------------------------------------------------------

/** Every function logs to a same-template group its own role may write. */
export function ownLogGroupControl(stackName: string, template: CfnTemplate): string[] {
  const resources = resourcesOf(template);
  const statements = iamStatements(template).filter(allows);
  const found: string[] = [];
  for (const [id, resource] of Object.entries(resources)) {
    if (resource.Type !== 'AWS::Lambda::Function') continue;
    const loggingConfig = resource.Properties?.LoggingConfig;
    const group = isObject(loggingConfig) && isObject(loggingConfig.LogGroup) ? loggingConfig.LogGroup.Ref : undefined;
    const roleAtt = resource.Properties?.Role;
    const role = isObject(roleAtt) && Array.isArray(roleAtt['Fn::GetAtt']) ? String(roleAtt['Fn::GetAtt'][0]) : undefined;
    const canWrite = typeof group === 'string' && resources[group]?.Type === 'AWS::Logs::LogGroup'
      && role !== undefined
      && statements.some((located) =>
        located.roles.includes(role)
        && covered(located.statement, ['logs:CreateLogStream', 'logs:PutLogEvents']).length === 2
        && located.statement.Resource !== undefined
        && list(located.statement.Resource).some((r) => isOwnGroupShape(r, template) === group));
    if (!canWrite) found.push(`${stackName}/${id}: cannot write its own log group`);
  }
  return found;
}

/** A pin no statement in any template matches is stale. */
export function unusedPins(
  templates: Record<string, unknown>,
  pins: readonly LogWritePin[],
  context: ScopeContext = DEFAULT_CONTEXT,
): string[] {
  return pins
    .filter((pin) => {
      const template = templates[pin.stack] as CfnTemplate | undefined;
      if (template === undefined) return true;
      return !iamStatements(template).filter(allows)
        .some((located) => pinMatches(pin, pin.stack, located, template, context));
    })
    .map((pin) => `unused pin ${pin.name}`);
}
