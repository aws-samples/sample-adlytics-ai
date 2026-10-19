// SPDX-License-Identifier: MIT-0
/**
 * Every function alarms on an unhandled failure (ADR-133 clause 4).
 *
 * Called once at the end of each function-bearing stack constructor, after every function exists.
 * For each `lambda.Function` in the stack it creates `adlytics-<env>-<suffix>-function-errors` on
 * the function's `AWS/Lambda` `Errors` (Sum over 5 minutes, at least 1 in one period, missing data
 * not breaching) and acts on `action`, the staffed switch. A handler that reports through
 * `report_error` and then raises, times out or runs out of memory is counted here; the operation
 * catch-all over the `Errors` EMF metric covers the failures a handler reports and absorbs.
 *
 * An explicit per-stack call rather than an app-wide Aspect: the staffed action reads the
 * published topic parameter in each stack, and an explicit call keeps that dependency visible.
 * `test/alarm-coverage.test.ts` fails on any function without the alarm, so a forgotten call fails.
 */
import * as cdk from 'aws-cdk-lib';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import * as cr from 'aws-cdk-lib/custom-resources';
import { IConstruct } from 'constructs';

import type { AppConfig } from '../config';

export const FUNCTION_ERRORS_ALARM_ID = 'FunctionErrorsAlarm';
export const FUNCTION_ERRORS_PERIOD = cdk.Duration.minutes(5);

/**
 * The closed set of functions that get no alarm, each a custom-resource handler. A custom
 * resource's failure fails the deploy visibly, so it is not silent. A function that backs a
 * `cdk.CustomResource` directly (its ARN as the service token) is not exempt: no rule here names
 * it, so it alarms like any other.
 *
 * Every class holds only while nothing else in the stack can invoke the function
 * (`exemptFunctionInvokers`): no event source mapping, resource-based permission or function URL
 * names it; no identity policy grants a lambda invoke action on it or on a wildcard, except, for an
 * onEvent handler, the policy of its framework function's own role; and no output or SSM parameter
 * publishes it. `test/alarm-coverage.test.ts` proves the same over every synthesized template,
 * including any other reference and other stacks.
 */
export type FunctionErrorExemption =
  /** A `cr.Provider` framework function (`framework.onEvent` and its siblings). */
  | 'provider-framework'
  /** A `cr.Provider`'s `onEventHandler`, invoked only by its framework function. */
  | 'provider-on-event-handler'
  /** The stack-level function a `lambda.SingletonFunction` (an `AwsCustomResource`) creates. */
  | 'singleton-helper';

export const FUNCTION_ERROR_EXEMPTIONS: readonly FunctionErrorExemption[] = [
  'provider-framework',
  'provider-on-event-handler',
  'singleton-helper',
];

/** Whether a resolved template value contains a `Ref` or `Fn::GetAtt` to `logicalId`. */
function references(value: unknown, logicalId: string): boolean {
  if (Array.isArray(value)) return value.some((item) => references(item, logicalId));
  if (value === null || typeof value !== 'object') return false;
  const record = value as Record<string, unknown>;
  if (record.Ref === logicalId) return true;
  const getAtt = record['Fn::GetAtt'];
  if (Array.isArray(getAtt) && getAtt[0] === logicalId) return true;
  return Object.values(record).some((item) => references(item, logicalId));
}

function ownedBy<T extends IConstruct>(
  stack: cdk.Stack,
  guard: (construct: IConstruct) => construct is T,
): T[] {
  return stack.node.findAll().filter(
    (construct): construct is T => guard(construct) && cdk.Stack.of(construct) === stack,
  );
}

/** The functions in `stack` (not in a nested stage or stack). */
export function stackFunctions(stack: cdk.Stack): lambda.Function[] {
  return ownedBy(stack, (c): c is lambda.Function => c instanceof lambda.Function);
}

/** The logical ID of a function's `AWS::Lambda::Function`. */
export function functionLogicalId(stack: cdk.Stack, fn: lambda.Function): string {
  return stack.getLogicalId(fn.node.defaultChild as lambda.CfnFunction);
}

/** The invoke actions an identity policy can authorize on a function. */
const INVOKE_ACTIONS = ['lambda:InvokeFunction', 'lambda:InvokeAsync', 'lambda:InvokeFunctionUrl'];

/** Whether one IAM action pattern (`*` and `?` wildcards, case-insensitive) covers an invoke. */
function coversInvoke(action: unknown): boolean {
  // A token or non-string action cannot be judged, so it is taken as covering (fail closed).
  if (typeof action !== 'string' || cdk.Token.isUnresolved(action)) return true;
  const pattern = action.replace(/[.+^${}()|[\]\\]/g, '\\$&').replace(/\*/g, '.*').replace(/\?/g, '.');
  return INVOKE_ACTIONS.some((invoke) => new RegExp(`^${pattern}$`, 'i').test(invoke));
}

/**
 * The strings by which an unresolved construct value names `fn`: its ARN and name tokens and, when
 * literal, its name. Matched as substrings, so `${arn}:*` and a joined ARN match too.
 */
function functionForms(fn: lambda.Function): string[] {
  const cfn = fn.node.defaultChild as lambda.CfnFunction;
  const forms = [fn.functionArn, fn.functionName, cfn.attrArn, cfn.ref];
  if (typeof cfn.functionName === 'string' && !cdk.Token.isUnresolved(cfn.functionName)) {
    forms.push(cfn.functionName);
  }
  return forms;
}

function names(value: unknown, forms: readonly string[]): boolean {
  return typeof value === 'string' && forms.some((form) => value.includes(form));
}

/** Whether one statement resource can denote `fn`: it names it, or it is a wildcard. */
function resourceCovers(resource: unknown, fn: lambda.Function, others: readonly string[]): boolean {
  if (typeof resource !== 'string') return true;
  if (names(resource, functionForms(fn))) return true;
  if (!resource.includes('*')) return false;
  // `${otherArn}:*` is another function's versions and aliases, not a wildcard over functions.
  return !others.some((arn) => resource.startsWith(`${arn}:`));
}

/** The statements of an unrendered policy document; undefined when it cannot be read. */
function statementsOf(document: unknown): unknown[] | undefined {
  const rendered = document instanceof iam.PolicyDocument ? document.toJSON() : document;
  if (rendered === undefined) return [];
  if (rendered === null || typeof rendered !== 'object' || cdk.Token.isUnresolved(rendered)) {
    return undefined;
  }
  const statement = (rendered as Record<string, unknown>).Statement;
  return Array.isArray(statement) ? statement : undefined;
}

/** Whether one statement allows an invoke on `fn` (NotAction and NotResource fail closed). */
function grantsInvoke(statement: unknown, fn: lambda.Function, others: readonly string[]): boolean {
  if (statement === null || typeof statement !== 'object') return true;
  const record = statement as Record<string, unknown>;
  if (record.Effect === 'Deny') return false;
  if (record.NotAction !== undefined || record.NotResource !== undefined) return true;
  const actions = [record.Action].flat();
  const resources = [record.Resource].flat();
  return actions.some(coversInvoke) && resources.some((r) => resourceCovers(r, fn, others));
}

/** The `cr.Provider` in `stack` whose `onEventHandler` is `fn`, if any. */
export function onEventProvider(stack: cdk.Stack, fn: lambda.Function): cr.Provider | undefined {
  return ownedBy(stack, (c): c is cr.Provider => c instanceof cr.Provider)
    .find((provider) => provider.onEventHandler === fn);
}

/** The framework `onEvent` function of `provider`. */
export function providerFramework(provider: cr.Provider): lambda.Function | undefined {
  const framework = provider.node.tryFindChild('framework-onEvent');
  return framework instanceof lambda.Function ? framework : undefined;
}

/**
 * The `AWS::IAM::Policy` through which the provider's framework function invokes its handler: the
 * custom resource's own path, trusted only while no other function assumes the same role.
 */
export function frameworkInvokePolicy(stack: cdk.Stack, provider: cr.Provider): IConstruct | undefined {
  const role = providerFramework(provider)?.role;
  if (role === undefined) return undefined;
  if (stackFunctions(stack).filter((other) => other.role === role).length !== 1) return undefined;
  return role.node.tryFindChild('DefaultPolicy')?.node.defaultChild;
}

/**
 * What in `stack` can invoke or publish `fn`, one line each, other than `trustedPolicy` (an onEvent
 * handler's framework invoke policy; undefined for every other function). Empty means `fn` is
 * reachable only through its custom resource.
 */
export function exemptFunctionInvokers(
  stack: cdk.Stack,
  fn: lambda.Function,
  trustedPolicy: IConstruct | undefined,
): string[] {
  const id = functionLogicalId(stack, fn);
  const forms = functionForms(fn);
  const found: string[] = [];
  for (const mapping of ownedBy(stack, (c): c is lambda.CfnEventSourceMapping =>
    c instanceof lambda.CfnEventSourceMapping)) {
    if (references(stack.resolve(mapping.functionName), id)) found.push(`event source mapping ${mapping.node.path}`);
  }
  for (const permission of ownedBy(stack, (c): c is lambda.CfnPermission => c instanceof lambda.CfnPermission)) {
    if (references(stack.resolve(permission.functionName), id)) found.push(`permission ${permission.node.path}`);
  }
  for (const url of ownedBy(stack, (c): c is lambda.CfnUrl => c instanceof lambda.CfnUrl)) {
    if (references(stack.resolve(url.targetFunctionArn), id)) found.push(`function URL ${url.node.path}`);
  }
  for (const output of ownedBy(stack, (c): c is cdk.CfnOutput => c instanceof cdk.CfnOutput)) {
    if (names(output.value, forms)) found.push(`output ${output.node.path}`);
  }
  for (const parameter of ownedBy(stack, (c): c is ssm.CfnParameter => c instanceof ssm.CfnParameter)) {
    if (names(parameter.value, forms)) found.push(`SSM parameter ${parameter.node.path}`);
  }

  const others = stackFunctions(stack).filter((other) => other !== fn).map((other) => other.functionArn);
  const documents: [IConstruct, unknown][] = [
    ...ownedBy(stack, (c): c is iam.CfnPolicy => c instanceof iam.CfnPolicy)
      .filter((policy) => trustedPolicy === undefined || policy !== trustedPolicy)
      .map((policy): [IConstruct, unknown] => [policy, policy.policyDocument]),
    ...ownedBy(stack, (c): c is iam.CfnManagedPolicy => c instanceof iam.CfnManagedPolicy)
      .map((policy): [IConstruct, unknown] => [policy, policy.policyDocument]),
    ...ownedBy(stack, (c): c is iam.CfnRole => c instanceof iam.CfnRole).flatMap((role) => {
      const inline = role.policies;
      if (inline === undefined) return [];
      if (!Array.isArray(inline)) return [[role, inline] as [IConstruct, unknown]];
      return inline.map((policy): [IConstruct, unknown] => [
        role,
        (policy as { policyDocument?: unknown }).policyDocument ?? policy,
      ]);
    }),
  ];
  for (const [owner, document] of documents) {
    const statements = statementsOf(document);
    if (statements === undefined || statements.some((s) => grantsInvoke(s, fn, others))) {
      found.push(`identity policy ${owner.node.path}`);
    }
  }
  return found;
}

/**
 * The custom-resource class `fn` belongs to, by construct identity alone, with the one policy that
 * may invoke it (an onEvent handler's framework invoke policy). Undefined when it is none.
 */
export function exemptionCandidate(
  stack: cdk.Stack,
  fn: lambda.Function,
): { readonly rule: FunctionErrorExemption; readonly trustedPolicy: IConstruct | undefined } | undefined {
  if (fn.node.scopes.some((scope) => scope instanceof cr.Provider)) {
    return { rule: 'provider-framework', trustedPolicy: undefined };
  }
  const provider = onEventProvider(stack, fn);
  if (provider !== undefined) {
    return { rule: 'provider-on-event-handler', trustedPolicy: frameworkInvokePolicy(stack, provider) };
  }
  const singletons = ownedBy(
    stack,
    (c): c is lambda.SingletonFunction => c instanceof lambda.SingletonFunction,
  );
  if (fn.node.scope === stack && singletons.some((s) => s.constructName === fn.node.id)) {
    return { rule: 'singleton-helper', trustedPolicy: undefined };
  }
  return undefined;
}

/**
 * Which closed rule exempts `fn`, or undefined when it must alarm: a custom-resource class whose
 * function nothing else can invoke. A class member that gains another invoke path alarms.
 */
export function functionErrorExemption(
  stack: cdk.Stack,
  fn: lambda.Function,
): FunctionErrorExemption | undefined {
  const candidate = exemptionCandidate(stack, fn);
  if (candidate === undefined) return undefined;
  return exemptFunctionInvokers(stack, fn, candidate.trustedPolicy).length === 0
    ? candidate.rule
    : undefined;
}

/**
 * The alarm-name suffix: the literal function name without `adlytics-<env>-`, or, for a function
 * whose name is a token, its construct path below the stack in lower kebab case.
 */
export function functionAlarmSuffix(stack: cdk.Stack, config: AppConfig, fn: lambda.Function): string {
  const name = stack.resolve((fn.node.defaultChild as lambda.CfnFunction).functionName);
  if (typeof name === 'string') {
    const prefix = `adlytics-${config.envName}-`;
    return name.startsWith(prefix) ? name.slice(prefix.length) : name;
  }
  return fn.node.path
    .slice(stack.node.path.length + 1)
    .replace(/([a-z0-9])([A-Z])/g, '$1-$2')
    .replace(/[^A-Za-z0-9]+/g, '-')
    .toLowerCase();
}

/** One `function-errors` alarm per non-exempt function in `stack`, each acting on `action`. */
export function applyFunctionErrorAlarms(
  stack: cdk.Stack,
  config: AppConfig,
  action: cloudwatch.IAlarmAction,
): cloudwatch.Alarm[] {
  return stackFunctions(stack)
    .filter((fn) => functionErrorExemption(stack, fn) === undefined)
    .map((fn) => {
      const suffix = functionAlarmSuffix(stack, config, fn);
      const alarm = new cloudwatch.Alarm(fn, FUNCTION_ERRORS_ALARM_ID, {
        alarmName: `adlytics-${config.envName}-${suffix}-function-errors`,
        alarmDescription:
          `Function ${suffix} ended an invocation in error: an unhandled exception, a timeout or `
          + 'running out of memory (AWS/Lambda Errors). Its log group names the failure; a '
          + 'reported one also carries its ErrorCode.',
        metric: fn.metricErrors({
          period: FUNCTION_ERRORS_PERIOD,
          statistic: cloudwatch.Stats.SUM,
        }),
        threshold: 1,
        comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
        evaluationPeriods: 1,
        // An idle function reports nothing, which is the normal state, not a signal.
        treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      });
      alarm.addAlarmAction(action);
      return alarm;
    });
}
