// SPDX-License-Identifier: MIT-0
/**
 * T2: every failure reaches an alarm on the staffed destination (ADR-133 clause 4).
 *
 * Over every stack the app builds (`support/templates.ts`):
 *
 * 1. Every `AWS::Lambda::Function`, except a function a closed rule in
 *    `lib/mixins/function-error-alarms.ts` exempts, has exactly one `AWS/Lambda` `Errors` alarm on
 *    its `FunctionName`, in the mixin's shape, with actions enabled, acting on the staffed switch.
 * 2. Exactly one `alarming-errors-by-operation` alarm exists, and the operations it excludes are
 *    exactly the operations that have their own page-capable `Errors` alarm (the exact
 *    `alarmingErrorsQuery` shape on the staffed switch): one more pages twice, one fewer is silent.
 *    Each owned operation has exactly one such alarm; a second pages twice. An alarm that only
 *    filters on the same Operation owns nothing.
 * 3. Every alarm has an action and does not disable its actions, and no action is a literal ARN.
 *    No alarm is exempt: the P5 probe alarm (`execution-graph-alarms.ts`) acts on the staffed
 *    destination by design.
 * 4. The query's two non-alarming classes are exactly the `ErrorClass` members whose errors do not
 *    page (`src/domain/shared_kernel/errors.py`).
 * 5. Every exemption rule matches at least one function, so no rule is stale.
 * 6. Every exempt function is reachable only through its custom resource in the synthesized
 *    templates: nothing references it but its class's own edges (an onEvent handler's framework
 *    function and invoke policy; a framework function's or singleton helper's custom resources by
 *    `ServiceToken` alone), no other template names it, and no identity policy anywhere allows
 *    invoking an arbitrary function.
 * 7. The catch-all names its runbook entry, which tells the operator to run an uncapped query over
 *    the same `Errors` source, one series per Operation, whenever the state reason shows the
 *    100-contributor cap.
 * 8. `PAGE_ROUTES` is exactly the routed Presentation operations: the `{path, operation}` pairs of
 *    `TELEMETRY_OPERATIONS` (`src/adapters/inbound/presentation_handlers/handler.py`), each `*_PATH`
 *    name resolved to its value, minus `UNROUTED_PRESENTATION_PATHS`.
 * 9. Every `PAGE_ROUTES` entry has exactly one page alarm (ADR-148).
 *
 * In check 2, a page alarm (ADR-148) owns its route's operation only in its exact shape: the route's
 * `AWS/ApiGateway` `Count`, `4xx` and `5xx` under `PAGE_ALARM_EXPRESSION`, missing data `ignore`,
 * and the staffed switch on both `ALARM` and `OK`. A `Resource` that is not a page path owns nothing.
 *
 * Each check is a function returning its violations, and a positive control proves each one fails
 * on the departure it exists to catch and passes once corrected.
 */
import * as fs from 'node:fs';
import * as path from 'node:path';

import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as cr from 'aws-cdk-lib/custom-resources';

import { AppConfig, paramPath } from '../lib/config';
import { staffedAlarmAction } from '../lib/constructs/observability/async-failure';
import {
  CATCH_ALL_RUNBOOK,
  NON_ALARMING_ERROR_CLASSES,
  alarmingErrorsByOperationQuery,
  alarmingErrorsQuery,
} from '../lib/constructs/observability/error-alarms';
import {
  PAGE_ALARM_EXPRESSION,
  PAGE_ROUTES,
  PageRoute,
  UNROUTED_PRESENTATION_PATHS,
} from '../lib/constructs/observability/page-route-alarms';
import {
  FUNCTION_ERROR_EXEMPTIONS,
  FunctionErrorExemption,
  applyFunctionErrorAlarms,
  exemptFunctionInvokers,
  frameworkInvokePolicy,
  functionAlarmSuffix,
  functionErrorExemption,
  functionLogicalId,
  onEventProvider,
  providerFramework,
  stackFunctions,
} from '../lib/mixins/function-error-alarms';
import { TEST_CONFIG } from './support/config';
import { everyStack } from './support/templates';

type Json = Record<string, any>;

const ERRORS_PY = path.join(__dirname, '..', '..', 'src', 'domain', 'shared_kernel', 'errors.py');
const HANDLER_PY = path.join(
  __dirname, '..', '..', 'src', 'adapters', 'inbound', 'presentation_handlers', 'handler.py',
);
const RUNBOOK = path.join(__dirname, '..', '..', String(CATCH_ALL_RUNBOOK.split('#')[0]));
const CATCH_ALL_SUFFIX = 'alarming-errors-by-operation';
const SINGLETON_ID = `AWS${cr.AwsCustomResource.PROVIDER_FUNCTION_UUID.replace(/-/g, '')}`;

function resourcesOf(template: Json, type: string): [string, Json][] {
  return Object.entries((template.Resources ?? {}) as Json)
    .filter(([, resource]) => resource.Type === type)
    .map(([id, resource]) => [id, (resource.Properties ?? {}) as Json]);
}

/** Whether one rendered alarm action is the staffed switch `staffedAlarmAction` selects. */
function isStaffedAction(template: Json, action: unknown, config: AppConfig): boolean {
  const destination = config.staffedAlarmDestination;
  if (destination?.arm === 'ExistingConfirmedSnsTopic') return action === destination.topicArn;
  const ref = (action as Json | null)?.Ref;
  if (typeof ref !== 'string') return false;
  const parameter = (template.Parameters ?? {})[ref] as Json | undefined;
  if (parameter !== undefined) {
    return parameter.Type === 'AWS::SSM::Parameter::Value<String>'
      && parameter.Default === paramPath(config.envName, 'ingestionAlarmTopicArn');
  }
  const topic = (template.Resources ?? {})[ref] as Json | undefined;
  return topic?.Type === 'AWS::SNS::Topic'
    && topic.Properties?.TopicName === `adlytics-${config.envName}-ingestion-alarms`;
}

/** Whether CloudWatch runs the alarm's actions: unset or literally true (anything else fails closed). */
function actionsEnabled(alarm: Json): boolean {
  return alarm.ActionsEnabled === undefined || alarm.ActionsEnabled === true;
}

interface FunctionCoverage {
  readonly violations: string[];
  readonly exempted: [FunctionErrorExemption, string][];
}

/** Check 1 for one stack: its construct tree says which functions exist, its template the alarms. */
function functionCoverage(stack: cdk.Stack, template: Json, config: AppConfig): FunctionCoverage {
  const violations: string[] = [];
  const exempted: [FunctionErrorExemption, string][] = [];
  const constructs = new Map(stackFunctions(stack).map((fn) => [functionLogicalId(stack, fn), fn]));
  const alarms = resourcesOf(template, 'AWS::CloudWatch::Alarm');
  for (const [id] of resourcesOf(template, 'AWS::Lambda::Function')) {
    const fn = constructs.get(id);
    if (fn === undefined) {
      violations.push(`${stack.stackName}/${id}: not a lambda.Function, so no rule can judge it`);
      continue;
    }
    const exemption = functionErrorExemption(stack, fn);
    if (exemption !== undefined) {
      exempted.push([exemption, fn.node.path]);
      continue;
    }
    const watching = alarms.filter(([, alarm]) =>
      alarm.Namespace === 'AWS/Lambda'
      && alarm.MetricName === 'Errors'
      && JSON.stringify(alarm.Dimensions) === JSON.stringify([{ Name: 'FunctionName', Value: { Ref: id } }]),
    );
    if (watching.length !== 1) {
      violations.push(`${fn.node.path}: ${watching.length} AWS/Lambda Errors alarms, not 1`);
      continue;
    }
    const [, alarm] = watching[0] as [string, Json];
    const expected = {
      AlarmName: `adlytics-${config.envName}-${functionAlarmSuffix(stack, config, fn)}-function-errors`,
      Namespace: 'AWS/Lambda',
      MetricName: 'Errors',
      Statistic: 'Sum',
      Period: 300,
      EvaluationPeriods: 1,
      Threshold: 1,
      ComparisonOperator: 'GreaterThanOrEqualToThreshold',
      TreatMissingData: 'notBreaching',
    };
    for (const [key, value] of Object.entries(expected)) {
      if (alarm[key] !== value) violations.push(`${fn.node.path}: ${key} is ${alarm[key]}, not ${value}`);
    }
    if (!actionsEnabled(alarm)) {
      violations.push(`${fn.node.path}: its Errors alarm sets ActionsEnabled ${JSON.stringify(alarm.ActionsEnabled)}`);
    }
    const actions = (alarm.AlarmActions ?? []) as unknown[];
    if (actions.length !== 1 || !isStaffedAction(template, actions[0], config)) {
      violations.push(`${fn.node.path}: its Errors alarm does not act on the staffed switch`);
    }
  }
  return { violations, exempted };
}

const expressionOf = (alarm: Json): string =>
  ((alarm.Metrics ?? []) as Json[]).map((query) => String(query.Expression ?? '')).join(' ');

const PAGE_ALARM_METRICS = [['m_count', 'Count'], ['m_4xx', '4xx'], ['m_5xx', '5xx']] as const;

/**
 * The operation a page alarm owns (ADR-148): only in its exact shape. That is the route's
 * `AWS/ApiGateway` `Count`, `4xx` and `5xx` Sums over 300 s with one set of dimensions (`ApiId` a
 * `Ref` to the template's only HTTP API, `Method` POST, `Stage` `$default`, a string `Resource`),
 * `expr_1` exactly `PAGE_ALARM_EXPRESSION`, 1 of 1 at ≥ 1, missing data `ignore`, actions enabled,
 * and one staffed action on both `ALARM` and `OK`. The owned operation is the `PAGE_ROUTES` entry
 * whose path is the `Resource`; any other `Resource` owns nothing.
 */
function pageAlarmOperation(template: Json, alarm: Json, config: AppConfig): string | undefined {
  const apis = resourcesOf(template, 'AWS::ApiGatewayV2::Api');
  const metrics = (alarm.Metrics ?? []) as Json[];
  if (apis.length !== 1 || metrics.length !== 4) return undefined;
  const [[apiId]] = apis as [[string, Json]];
  const byId = new Map(metrics.map((query) => [String(query.Id), query]));
  const expression = byId.get('expr_1');
  if (expression === undefined || Object.keys(expression).length !== 3
    || expression.Expression !== PAGE_ALARM_EXPRESSION || expression.ReturnData !== true) {
    return undefined;
  }
  const dimensionSets = new Set<string>();
  let resource: unknown;
  for (const [id, metricName] of PAGE_ALARM_METRICS) {
    const query = byId.get(id);
    const stat = query?.MetricStat as Json | undefined;
    const metric = stat?.Metric as Json | undefined;
    if (query === undefined || Object.keys(query).length !== 3 || query.ReturnData !== false
      || stat === undefined || stat.Stat !== 'Sum' || stat.Period !== 300
      || metric?.Namespace !== 'AWS/ApiGateway' || metric.MetricName !== metricName) {
      return undefined;
    }
    const dimensions = Object.fromEntries(
      ((metric.Dimensions ?? []) as Json[]).map((dimension) => [String(dimension.Name), dimension.Value]),
    );
    if (JSON.stringify(Object.keys(dimensions).sort()) !== JSON.stringify(['ApiId', 'Method', 'Resource', 'Stage'])
      || JSON.stringify(dimensions.ApiId) !== JSON.stringify({ Ref: apiId })
      || dimensions.Method !== 'POST' || dimensions.Stage !== '$default'
      || typeof dimensions.Resource !== 'string') {
      return undefined;
    }
    dimensionSets.add(JSON.stringify(Object.entries(dimensions).sort()));
    resource = dimensions.Resource;
  }
  const shape = {
    EvaluationPeriods: 1,
    DatapointsToAlarm: 1,
    Threshold: 1,
    ComparisonOperator: 'GreaterThanOrEqualToThreshold',
    TreatMissingData: 'ignore',
    ActionsEnabled: undefined,
  };
  const matches = dimensionSets.size === 1 && Object.entries(shape).every(
    ([key, value]) => JSON.stringify(alarm[key]) === JSON.stringify(value),
  );
  const staffedOn = (key: string): boolean => {
    const actions = (alarm[key] ?? []) as unknown[];
    return actions.length === 1 && isStaffedAction(template, actions[0], config);
  };
  return matches && staffedOn('AlarmActions') && staffedOn('OKActions')
    ? PAGE_ROUTES.find((route) => route.path === resource)?.operation
    : undefined;
}

/**
 * Check 8: `routes` is exactly the routed `{path, operation}` pairs of `handler.py`'s
 * `TELEMETRY_OPERATIONS`, with each `*_PATH` name resolved to its assigned value, minus `unrouted`.
 */
function pageRosterViolations(
  source: string,
  routes: readonly PageRoute[] = PAGE_ROUTES,
  unrouted: readonly string[] = UNROUTED_PRESENTATION_PATHS,
): string[] {
  const paths = new Map(
    [...source.matchAll(/^([A-Z_]+_PATH) = "(\/api\/[^"]+)"$/gm)].map(
      (match) => [String(match[1]), String(match[2])] as const,
    ),
  );
  const block = /^TELEMETRY_OPERATIONS = \{\n([\s\S]*?)^\}$/m.exec(source)?.[1];
  if (block === undefined || paths.size === 0) {
    throw new Error('handler.py TELEMETRY_OPERATIONS or its *_PATH constants not found');
  }
  const telemetry = [...block.matchAll(/^ {4}([A-Z_]+): "([^"]+)",$/gm)].map((match) => {
    const resolved = paths.get(String(match[1]));
    if (resolved === undefined) throw new Error(`handler.py ${match[1]} has no *_PATH assignment`);
    return `${resolved} -> ${match[2]}`;
  });
  if (telemetry.length === 0) throw new Error('handler.py TELEMETRY_OPERATIONS did not parse');
  const routed = new Set(telemetry.filter((entry) => !unrouted.includes(entry.split(' -> ')[0] as string)));
  const declared = new Set(routes.map((route) => `${route.path} -> ${route.operation}`));
  return [
    ...[...routed].filter((entry) => !declared.has(entry)).sort()
      .map((entry) => `handler.py routes ${entry}, which PAGE_ROUTES does not declare`),
    ...[...declared].filter((entry) => !routed.has(entry)).sort()
      .map((entry) => `PAGE_ROUTES declares ${entry}, which handler.py does not route`),
  ];
}

/** Check 9 over every template: each `PAGE_ROUTES` operation has exactly one page alarm. */
function pageAlarmViolations(templates: Json[], config: AppConfig): string[] {
  const counts = new Map(PAGE_ROUTES.map((route) => [route.operation, 0]));
  for (const template of templates) {
    for (const [, alarm] of resourcesOf(template, 'AWS::CloudWatch::Alarm')) {
      const operation = pageAlarmOperation(template, alarm, config);
      if (operation !== undefined) counts.set(operation, (counts.get(operation) ?? 0) + 1);
    }
  }
  return [...counts].filter(([, count]) => count !== 1)
    .map(([operation, count]) => `${operation} has ${count} page alarms, not 1`);
}

/**
 * The operation one alarm owns: only when it is exactly the page-capable shape of
 * `alarmingErrorsQuery(operation)` (≥ 1 paging error in one 5-minute period, actions enabled,
 * acting only on the staffed switch). An alarm that merely filters on `Operation = '<op>'` (another
 * metric, other classes, a threshold that cannot breach, another action) owns nothing.
 */
function ownedOperation(template: Json, alarm: Json, config: AppConfig): string | undefined {
  const operation = /Operation = '([^']+)'$/.exec(expressionOf(alarm))?.[1];
  if (operation === undefined) return pageAlarmOperation(template, alarm, config);
  const shape = {
    Metrics: [{ Expression: alarmingErrorsQuery(operation), Id: 'expr_1', Period: 300, ReturnData: true }],
    EvaluationPeriods: 1,
    DatapointsToAlarm: undefined,
    Threshold: 1,
    ComparisonOperator: 'GreaterThanOrEqualToThreshold',
    TreatMissingData: 'notBreaching',
    ActionsEnabled: undefined,
  };
  const matches = Object.entries(shape).every(
    ([key, value]) => JSON.stringify(alarm[key]) === JSON.stringify(value),
  );
  const actions = (alarm.AlarmActions ?? []) as unknown[];
  return matches && actions.length === 1 && isStaffedAction(template, actions[0], config)
    ? operation
    : undefined;
}

/** Check 2 over every template. */
function catchAllViolations(templates: Json[], config: AppConfig): string[] {
  const alarms = templates.flatMap((template) => resourcesOf(template, 'AWS::CloudWatch::Alarm'));
  // Counted, not deduplicated: two owners of one operation page twice for one failure.
  const owners = new Map<string, number>();
  for (const template of templates) {
    for (const [, alarm] of resourcesOf(template, 'AWS::CloudWatch::Alarm')) {
      const operation = ownedOperation(template, alarm, config);
      if (operation !== undefined) owners.set(operation, (owners.get(operation) ?? 0) + 1);
    }
  }
  const owned = new Set(owners.keys());
  const catchAlls = alarms.filter(([, alarm]) => expressionOf(alarm).includes('GROUP BY Operation'));
  if (catchAlls.length !== 1) return [`${catchAlls.length} GROUP BY Operation alarms, not 1`];
  const [, alarm] = catchAlls[0] as [string, Json];
  const found: string[] = [];
  if (alarm.AlarmName !== `adlytics-${config.envName}-${CATCH_ALL_SUFFIX}`) {
    found.push(`the catch-all is named ${alarm.AlarmName}`);
  }
  const excluded = [...expressionOf(alarm).matchAll(/ Operation != '([^']+)'/g)].map((m) =>
    String(m[1]),
  );
  if (expressionOf(alarm) !== alarmingErrorsByOperationQuery(excluded)) {
    found.push('the catch-all expression is not alarmingErrorsByOperationQuery of its exclusions');
  }
  for (const [operation, count] of [...owners].filter(([, n]) => n > 1).sort()) {
    found.push(`${operation} has ${count} alarms of its own, not 1 (pages ${count} times)`);
  }
  for (const operation of [...owned].filter((op) => !excluded.includes(op)).sort()) {
    found.push(`${operation} has its own alarm and the catch-all also counts it (pages twice)`);
  }
  for (const operation of excluded.filter((op) => !owned.has(op)).sort()) {
    found.push(`the catch-all excludes ${operation}, which has no alarm of its own (silent)`);
  }
  return found;
}

/** Whether a template value references `logicalId` by Ref, Fn::GetAtt or a Fn::Sub variable. */
function mentions(value: unknown, logicalId: string): boolean {
  if (Array.isArray(value)) return value.some((item) => mentions(item, logicalId));
  if (value === null || typeof value !== 'object') return false;
  const record = value as Json;
  if (record.Ref === logicalId) return true;
  const getAtt = record['Fn::GetAtt'];
  if (Array.isArray(getAtt) && getAtt[0] === logicalId) return true;
  if (typeof getAtt === 'string' && getAtt.split('.')[0] === logicalId) return true;
  const sub = record['Fn::Sub'];
  const text = Array.isArray(sub) ? sub[0] : sub;
  if (typeof text === 'string' && (text.includes(`\${${logicalId}}`) || text.includes(`\${${logicalId}.`))) {
    return true;
  }
  return Object.values(record).some((item) => mentions(item, logicalId));
}

const INVOKE = ['lambda:InvokeFunction', 'lambda:InvokeAsync', 'lambda:InvokeFunctionUrl'];
const IDENTITY_TYPES = ['AWS::IAM::Role', 'AWS::IAM::Policy', 'AWS::IAM::ManagedPolicy'];

/** Every statement in a template's identity policies, with the resource that holds it. */
function identityStatements(template: Json): [string, Json][] {
  return Object.entries((template.Resources ?? {}) as Json).flatMap(([id, resource]) => {
    const properties = (resource.Properties ?? {}) as Json;
    const documents: unknown[] = resource.Type === 'AWS::IAM::Role'
      ? ((properties.Policies ?? []) as Json[]).map((policy) => policy.PolicyDocument)
      : IDENTITY_TYPES.includes(String(resource.Type))
        ? [properties.PolicyDocument]
        : [];
    return documents.flatMap((document) =>
      [((document ?? {}) as Json).Statement ?? []].flat().map((s): [string, Json] => [id, s as Json]),
    );
  });
}

/** Whether an Allow statement's actions can include a lambda invoke (a NotAction fails closed). */
function allowsInvoke(statement: Json): boolean {
  if (statement.Effect !== 'Allow') return false;
  return statement.NotAction !== undefined || [statement.Action].flat().some((action) => {
    if (typeof action !== 'string') return true;
    const pattern = action.replace(/[.+^${}()|[\]\\]/g, '\\$&').replace(/\*/g, '.*').replace(/\?/g, '.');
    return INVOKE.some((invoke) => new RegExp(`^${pattern}$`, 'i').test(invoke));
  });
}

/**
 * Whether a statement allows invoking an arbitrary function: an invoke action on a resource that
 * is not one resource's own ARN or `${arn}:*` (a wildcard, or NotResource).
 */
function wildcardInvoke(statement: Json): boolean {
  if (!allowsInvoke(statement)) return false;
  if (statement.NotResource !== undefined) return true;
  return [statement.Resource].flat().some((resource) => {
    const getAtt = (resource as Json | null)?.['Fn::GetAtt'];
    if (Array.isArray(getAtt) && getAtt[1] === 'Arn') return false;
    const parts = (resource as Json | null)?.['Fn::Join']?.[1];
    if (Array.isArray(parts) && parts.length === 2 && parts[1] === ':*'
      && Array.isArray(parts[0]?.['Fn::GetAtt'])) {
      return false;
    }
    return JSON.stringify(resource).includes('*');
  });
}

const EXEMPTION_LABEL: Record<FunctionErrorExemption, string> = {
  'provider-framework': 'a provider framework function',
  'provider-on-event-handler': 'a provider onEvent handler',
  'singleton-helper': 'a singleton helper',
};

/**
 * A custom resource's properties without its `ServiceToken`, when that token is exactly `id`'s
 * ARN: the one edge by which CloudFormation invokes a framework function or singleton helper.
 * Any other property that names the function is still a reference.
 */
function withoutServiceToken(resource: Json, id: string): unknown {
  const custom = resource.Type === 'AWS::CloudFormation::CustomResource'
    || String(resource.Type).startsWith('Custom::');
  const properties = (resource.Properties ?? {}) as Json;
  if (!custom || JSON.stringify(properties.ServiceToken) !== JSON.stringify({ 'Fn::GetAtt': [id, 'Arn'] })) {
    return properties;
  }
  const { ServiceToken: _token, ...rest } = properties;
  return rest;
}

/**
 * Check 6: every exempt function is reachable only through its custom resource, over every
 * synthesized template. In its own template nothing references it but its class's own edges (an
 * onEvent handler's framework function and the framework role's invoke policy; a framework
 * function's or singleton helper's custom resources, by `ServiceToken` alone) and a log group; no
 * other template names it literally; and no identity policy anywhere allows invoking an arbitrary
 * function.
 */
function exemptFunctionViolations(
  stacks: Record<string, cdk.Stack>,
  templates: Record<string, Json>,
): string[] {
  const found: string[] = [];
  for (const [stackName, stack] of Object.entries(stacks)) {
    for (const fn of stackFunctions(stack)) {
      const rule = functionErrorExemption(stack, fn);
      if (rule === undefined) continue;
      const id = functionLogicalId(stack, fn);
      const allowed = new Set([id]);
      if (rule === 'provider-on-event-handler') {
        const provider = onEventProvider(stack, fn) as cr.Provider;
        const framework = providerFramework(provider);
        const policy = frameworkInvokePolicy(stack, provider);
        if (framework !== undefined) allowed.add(functionLogicalId(stack, framework));
        if (policy !== undefined) allowed.add(stack.getLogicalId(policy as cdk.CfnElement));
      }
      // Only a framework function or singleton helper is a custom resource's service token.
      const properties = (resource: Json): unknown =>
        rule === 'provider-on-event-handler' ? resource.Properties : withoutServiceToken(resource, id);
      const where = `${fn.node.path}: exempt as ${EXEMPTION_LABEL[rule]}, but`;
      const name = (fn.node.defaultChild as lambda.CfnFunction).functionName;
      const literal = typeof name === 'string' && !cdk.Token.isUnresolved(name)
        ? new RegExp(`${name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}(?![A-Za-z0-9_-])`)
        : undefined;
      // Whether a value in `otherStack` denotes the function: a reference in its own template, or
      // its literal name in any template.
      const denotes = (otherStack: string, value: unknown): boolean =>
        (otherStack === stackName && mentions(value, id))
        || (literal !== undefined && literal.test(JSON.stringify(value ?? {})));
      for (const [otherStack, other] of Object.entries(templates)) {
        for (const [otherId, resource] of Object.entries((other.Resources ?? {}) as Json)) {
          const own = otherStack === stackName && allowed.has(otherId);
          // An identity policy matters only where it can invoke; its other grants (the function's
          // own role writing its logs) are not a path to the function.
          if (own || IDENTITY_TYPES.includes(String(resource.Type))
            || resource.Type === 'AWS::Logs::LogGroup') {
            continue;
          }
          if (denotes(otherStack, otherStack === stackName ? properties(resource) : resource.Properties)) {
            found.push(`${where} ${otherStack}/${otherId} (${resource.Type}) references it`);
          }
        }
        for (const [output, value] of Object.entries((other.Outputs ?? {}) as Json)) {
          if (denotes(otherStack, value)) found.push(`${where} output ${otherStack}/${output} publishes it`);
        }
        for (const [otherId, statement] of identityStatements(other)) {
          if (wildcardInvoke(statement)) {
            found.push(`${where} ${otherStack}/${otherId} allows invoking any function`);
          } else if (!(otherStack === stackName && allowed.has(otherId)) && allowsInvoke(statement)
            && denotes(otherStack, statement.Resource)) {
            found.push(`${where} ${otherStack}/${otherId} allows invoking it`);
          }
        }
      }
    }
  }
  return found;
}

/**
 * Check 7: the catch-all's runbook entry exists where its description says, tells the operator to
 * run its query when the reason shows the cap, and that query reads the alarm's `Errors` source
 * (the same SCHEMA and paging-class filters) with no Operation exclusion, grouped by Operation
 * alone, so its 500-series limit is spent on operations, not on their error codes.
 */
function runbookViolations(markdown: string, alarm: Json): string[] {
  const [file, anchor] = CATCH_ALL_RUNBOOK.split('#') as [string, string];
  const found: string[] = [];
  if (!String(alarm.AlarmDescription).includes(CATCH_ALL_RUNBOOK)) {
    found.push(`the catch-all description does not name ${CATCH_ALL_RUNBOOK}`);
  }
  const heading = [...markdown.matchAll(/^## (.+)$/gm)].find(
    (match) => String(match[1]).toLowerCase().replace(/[^a-z0-9 -]/g, '').replace(/ /g, '-') === anchor,
  );
  if (heading === undefined) return [...found, `${file} has no section #${anchor}`];
  const start = (heading.index ?? 0) + heading[0].length;
  const next = markdown.slice(start).search(/^## /m);
  const section = markdown.slice(start, next === -1 ? undefined : start + next);
  if (!section.includes('Whenever the state reason reads `100+`, run the query below')) {
    found.push(`#${anchor} does not tell the operator to run the query when the reason shows 100+`);
  }
  const block = /```bash\n([\s\S]*?)```/.exec(section)?.[1]
    ?.replace(/'"'"'/g, "'")
    .replace(/\\"/g, '"');
  const query = block === undefined ? undefined : /"Expression":"(.*?)"\}\]'/.exec(block)?.[1];
  if (block === undefined || !block.includes('aws cloudwatch get-metric-data') || query === undefined) {
    return [...found, `#${anchor} has no get-metric-data query`];
  }
  const expression = expressionOf(alarm);
  const source = expression.slice(0, expression.indexOf(' WHERE '));
  const classes = expression
    .slice(expression.indexOf(' WHERE ') + ' WHERE '.length, expression.indexOf(' GROUP BY'))
    .split(' AND ')
    .filter((filter) => filter.startsWith('ErrorClass '));
  const expected = `${source} WHERE ${classes.join(' AND ')} `
    + 'GROUP BY Operation ORDER BY SUM() DESC LIMIT 500';
  if (query !== expected) found.push(`#${anchor} query is not the alarm's Errors source: ${query}`);
  return found;
}

/** Check 3 over every template. */
function actionViolations(templates: Record<string, Json>, config: AppConfig): string[] {
  const configured = config.staffedAlarmDestination?.arm === 'ExistingConfirmedSnsTopic'
    ? config.staffedAlarmDestination.topicArn
    : undefined;
  return Object.entries(templates).flatMap(([stackName, template]) =>
    resourcesOf(template, 'AWS::CloudWatch::Alarm').flatMap(([id, alarm]) => {
      const actions = (alarm.AlarmActions ?? []) as unknown[];
      if (actions.length === 0) return [`${stackName}/${id} (${alarm.AlarmName}): no alarm action`];
      return [
        ...(actionsEnabled(alarm)
          ? []
          : [`${stackName}/${id} (${alarm.AlarmName}): ActionsEnabled ${JSON.stringify(alarm.ActionsEnabled)}`]),
        ...actions
          .filter((action) => typeof action === 'string' && action !== configured)
          .map((action) => `${stackName}/${id} (${alarm.AlarmName}): literal action ${String(action)}`),
      ];
    }),
  );
}

/** Check 4: the ErrorClass members whose `alarms` property is false, read from the source. */
function nonAlarmingErrorClasses(source: string): string[] {
  const body = /class ErrorClass\(StrEnum\):([\s\S]*?)\n\n\n/.exec(source)?.[1];
  const alarming = /def alarms\(self\) -> bool:[\s\S]*?return self\.error_class in \(([^)]*)\)/
    .exec(source)?.[1];
  if (body === undefined || alarming === undefined) {
    throw new Error('ErrorClass or AdlyticsError.alarms not found in errors.py');
  }
  const members = new Map(
    [...body.matchAll(/^ {4}([A-Z_]+) = "([a-z_]+)"$/gm)].map(
      (match) => [String(match[1]), String(match[2])] as const,
    ),
  );
  const paging = new Set(
    [...alarming.matchAll(/ErrorClass\.([A-Z_]+)/g)].map((match) => String(match[1])),
  );
  if (members.size === 0 || [...paging].some((name) => !members.has(name))) {
    throw new Error('errors.py ErrorClass members did not parse');
  }
  return [...members].filter(([name]) => !paging.has(name)).map(([, value]) => value).sort();
}

function templatesOf(stacks: Record<string, cdk.Stack>): Record<string, Json> {
  return Object.fromEntries(
    Object.entries(stacks).map(([name, stack]) => [name, Template.fromStack(stack).toJSON() as Json]),
  );
}

function fixtureStack(id: string): cdk.Stack {
  return new cdk.Stack(new cdk.App(), id, {
    env: { account: TEST_CONFIG.account, region: TEST_CONFIG.region },
  });
}

function fixtureFunction(stack: cdk.Stack, id: string): lambda.Function {
  return new lambda.Function(stack, id, {
    functionName: `adlytics-${TEST_CONFIG.envName}-${id}`,
    runtime: new lambda.Runtime(TEST_CONFIG.pythonRuntime, lambda.RuntimeFamily.PYTHON),
    handler: 'index.handler',
    // Any directory will do, as in support/templates.ts.
    code: lambda.Code.fromAsset(__dirname),
  });
}

describe('T2 alarm coverage (ADR-133 clause 4)', () => {
  const stacks = everyStack();
  const templates = templatesOf(stacks);
  const coverage = Object.entries(stacks).map(([name, stack]) =>
    functionCoverage(stack, templates[name] ?? {}, TEST_CONFIG),
  );

  it('alarms on every function that no closed rule exempts, on the staffed switch', () => {
    expect(coverage.flatMap((item) => item.violations)).toStrictEqual([]);
    const functions = Object.values(templates).flatMap((template) =>
      resourcesOf(template, 'AWS::Lambda::Function'),
    );
    const alarmed = Object.values(templates).flatMap((template) =>
      resourcesOf(template, 'AWS::CloudWatch::Alarm').filter(([, alarm]) =>
        String(alarm.AlarmName).endsWith('-function-errors'),
      ),
    );
    // Not vacuous: 57 functions, 6 exempt, 51 alarmed (feat-3-verify.md lists them).
    expect(functions).toHaveLength(57);
    expect(alarmed).toHaveLength(functions.length - coverage.flatMap((item) => item.exempted).length);
    expect(alarmed).toHaveLength(51);
  });

  it('names the functions RCA R7 and the brief require', () => {
    const names = Object.values(templates).flatMap((template) =>
      resourcesOf(template, 'AWS::CloudWatch::Alarm').map(([, alarm]) => String(alarm.AlarmName)),
    );
    for (const suffix of [
      'IngestionFunction',
      'ExecutionGraphDispatcher',
      'ExecutionGraphFenceSettler',
      'ExecutionGraphPromoter',
      'ExecutionGraphNodeVerifier',
      'ExecutionGraphRelease',
      'ExecutionGraphStuckFlowDetector',
      'AuthFunction',
      'AuthorizerFunction',
      'PresentationFunction',
      'RunRequestsFunction',
      'PromptAdminCreateFunction',
      'QualityReview',
      'OperationalStatusFunction',
      'MeasurementTool',
      'CatalogTool',
      'GatewayInterceptor',
      'TokenRouteExchange',
      'TokenIssuer',
    ]) {
      expect(names).toContain(`adlytics-dev-${suffix}-function-errors`);
    }
  });

  it('covers every other operation with exactly one catch-all excluding exactly the owned ones', () => {
    expect(catchAllViolations(Object.values(templates), TEST_CONFIG)).toStrictEqual([]);
  });

  it('gives every alarm an action and none a literal ARN', () => {
    expect(actionViolations(templates, TEST_CONFIG)).toStrictEqual([]);
  });

  it('excludes exactly the ErrorClass members that do not page', () => {
    expect([...NON_ALARMING_ERROR_CLASSES].sort()).toStrictEqual(
      nonAlarmingErrorClasses(fs.readFileSync(ERRORS_PY, 'utf8')),
    );
  });

  it('matches each exemption rule at least once, and only the custom-resource kinds', () => {
    const exempted = coverage.flatMap((item) => item.exempted);
    for (const rule of FUNCTION_ERROR_EXEMPTIONS) {
      expect(exempted.filter(([matched]) => matched === rule).length).toBeGreaterThan(0);
    }
    expect(exempted.map(([rule, where]) => `${rule} ${where}`).sort()).toStrictEqual([
      'provider-framework InsightPromptRegistrySeed/SeedProvider/framework-onEvent',
      'provider-framework Publisher/BackfillProvider/framework-onEvent',
      'provider-on-event-handler InsightPromptRegistrySeed/Seed',
      'provider-on-event-handler Publisher/Backfill',
      `singleton-helper Auth/${SINGLETON_ID}`,
      `singleton-helper Frontend/${SINGLETON_ID}`,
    ]);
  });

  it('reaches each exempt function only through its custom resource', () => {
    expect(exemptFunctionViolations(stacks, templates)).toStrictEqual([]);
  });

  const catchAllAlarm = (): Json => {
    const found = Object.values(templates)
      .flatMap((template) => resourcesOf(template, 'AWS::CloudWatch::Alarm'))
      .find(([, alarm]) => alarm.AlarmName === `adlytics-dev-${CATCH_ALL_SUFFIX}`);
    return JSON.parse(JSON.stringify((found as [string, Json])[1])) as Json;
  };

  it('names a runbook query over the same Errors source for when the catch-all is capped', () => {
    expect(runbookViolations(fs.readFileSync(RUNBOOK, 'utf8'), catchAllAlarm())).toStrictEqual([]);
  });

  it('declares exactly the routed Presentation operations as page routes', () => {
    expect(pageRosterViolations(fs.readFileSync(HANDLER_PY, 'utf8'))).toStrictEqual([]);
    // Not vacuous: 17 routed pages, each owned by its page alarm in check 2.
    expect(PAGE_ROUTES).toHaveLength(17);
  });

  it('gives every page route exactly one page alarm', () => {
    expect(pageAlarmViolations(Object.values(templates), TEST_CONFIG)).toStrictEqual([]);
  });

  describe('positive controls', () => {
    it('check 1 fails on a function without the mixin and passes once it is applied', () => {
      const bare = fixtureStack('Bare');
      fixtureFunction(bare, 'Worker');
      expect(functionCoverage(bare, Template.fromStack(bare).toJSON(), TEST_CONFIG).violations)
        .toStrictEqual(['Bare/Worker: 0 AWS/Lambda Errors alarms, not 1']);

      const covered = fixtureStack('Covered');
      fixtureFunction(covered, 'Worker');
      applyFunctionErrorAlarms(covered, TEST_CONFIG, staffedAlarmAction(covered, TEST_CONFIG));
      expect(functionCoverage(covered, Template.fromStack(covered).toJSON(), TEST_CONFIG).violations)
        .toStrictEqual([]);
    });

    it('checks 1 and 3 fail on a function alarm whose actions are disabled', () => {
      const render = (disable: boolean): Json => {
        const stack = fixtureStack('Disabled');
        fixtureFunction(stack, 'Worker');
        const [alarm] = applyFunctionErrorAlarms(stack, TEST_CONFIG, staffedAlarmAction(stack, TEST_CONFIG));
        if (disable) (alarm?.node.defaultChild as cloudwatch.CfnAlarm).actionsEnabled = false;
        return { stack, template: Template.fromStack(stack).toJSON() };
      };
      // Paired control: the mixin's alarm leaves its actions enabled.
      const enabled = render(false);
      expect(functionCoverage(enabled.stack, enabled.template, TEST_CONFIG).violations).toStrictEqual([]);
      expect(actionViolations({ Disabled: enabled.template }, TEST_CONFIG)).toStrictEqual([]);

      const disabled = render(true);
      expect(functionCoverage(disabled.stack, disabled.template, TEST_CONFIG).violations).toStrictEqual([
        'Disabled/Worker: its Errors alarm sets ActionsEnabled false',
      ]);
      const [id] = resourcesOf(disabled.template, 'AWS::CloudWatch::Alarm').map(([key]) => key);
      expect(actionViolations({ Disabled: disabled.template }, TEST_CONFIG)).toStrictEqual([
        `Disabled/${id} (adlytics-dev-Worker-function-errors): ActionsEnabled false`,
      ]);
    });

    it('check 1 fails on an alarm acting elsewhere than the staffed switch', () => {
      const stack = fixtureStack('Elsewhere');
      fixtureFunction(stack, 'Worker');
      applyFunctionErrorAlarms(stack, TEST_CONFIG, {
        bind: () => ({ alarmActionArn: 'arn:aws:sns:us-east-1:111111111111:somewhere-else' }),
      });
      expect(functionCoverage(stack, Template.fromStack(stack).toJSON(), TEST_CONFIG).violations)
        .toStrictEqual(['Elsewhere/Worker: its Errors alarm does not act on the staffed switch']);
    });

    it('an onEvent handler that something else also invokes is not exempt', () => {
      const stack = fixtureStack('Invoked');
      const handler = fixtureFunction(stack, 'Handler');
      new cr.Provider(stack, 'Provider', { onEventHandler: handler });
      expect(functionErrorExemption(stack, handler)).toBe('provider-on-event-handler');
      handler.addPermission('Scheduled', {
        principal: new cdk.aws_iam.ServicePrincipal('events.amazonaws.com'),
      });
      expect(functionErrorExemption(stack, handler)).toBeUndefined();
    });

    it('an onEvent handler an identity policy can invoke is not exempt and then requires an alarm', () => {
      const exempt = (
        grant: (stack: cdk.Stack, handler: lambda.Function) => void,
        withMixin = false,
      ): {
        exemption: FunctionErrorExemption | undefined;
        invokers: string[];
        violations: string[];
      } => {
        const stack = fixtureStack('Granted');
        const handler = fixtureFunction(stack, 'Handler');
        const provider = new cr.Provider(stack, 'Provider', { onEventHandler: handler });
        grant(stack, handler);
        if (withMixin) {
          applyFunctionErrorAlarms(stack, TEST_CONFIG, staffedAlarmAction(stack, TEST_CONFIG));
        }
        return {
          exemption: functionErrorExemption(stack, handler),
          invokers: exemptFunctionInvokers(stack, handler, frameworkInvokePolicy(stack, provider)),
          violations: functionCoverage(stack, Template.fromStack(stack).toJSON(), TEST_CONFIG).violations,
        };
      };
      const caller = (stack: cdk.Stack): cdk.aws_iam.Role => new cdk.aws_iam.Role(stack, 'Caller', {
        assumedBy: new cdk.aws_iam.ServicePrincipal('states.amazonaws.com'),
      });

      // Paired control: only the provider's own path, and another function's versions, are fine.
      const alone = exempt((stack) => {
        caller(stack).addToPolicy(new cdk.aws_iam.PolicyStatement({
          actions: ['lambda:InvokeFunction'],
          resources: [`${fixtureFunction(stack, 'Other').functionArn}:*`],
        }));
      });
      expect(alone.invokers).toStrictEqual([]);
      expect(alone.exemption).toBe('provider-on-event-handler');

      const grantInvoke = (stack: cdk.Stack, handler: lambda.Function): void => {
        handler.grantInvoke(caller(stack));
      };
      const granted = exempt(grantInvoke);
      expect(granted.invokers).toStrictEqual(['identity policy Granted/Caller/DefaultPolicy/Resource']);
      expect(granted.exemption).toBeUndefined();
      expect(granted.violations).toStrictEqual(['Granted/Handler: 0 AWS/Lambda Errors alarms, not 1']);
      expect(exempt(grantInvoke, true).violations).toStrictEqual([]);

      const wildcard = exempt((stack) => {
        caller(stack).addToPolicy(new cdk.aws_iam.PolicyStatement({
          actions: ['lambda:Invoke*'],
          resources: ['*'],
        }));
      });
      expect(wildcard.invokers).toStrictEqual(['identity policy Granted/Caller/DefaultPolicy/Resource']);
      expect(wildcard.exemption).toBeUndefined();

      const published = exempt((stack, handler) => {
        new cdk.aws_ssm.StringParameter(stack, 'HandlerArn', { stringValue: handler.functionArn });
      });
      expect(published.invokers).toStrictEqual(['SSM parameter Granted/HandlerArn/Resource']);
      expect(published.exemption).toBeUndefined();
    });

    it('an onEvent handler whose framework role another function shares is not exempt', () => {
      const stack = fixtureStack('Shared');
      const handler = fixtureFunction(stack, 'Handler');
      const role = new cdk.aws_iam.Role(stack, 'FrameworkRole', {
        assumedBy: new cdk.aws_iam.ServicePrincipal('lambda.amazonaws.com'),
      });
      new cr.Provider(stack, 'Provider', { onEventHandler: handler, frameworkOnEventRole: role });
      expect(functionErrorExemption(stack, handler)).toBe('provider-on-event-handler');
      new lambda.Function(stack, 'Sharer', {
        runtime: new lambda.Runtime(TEST_CONFIG.pythonRuntime, lambda.RuntimeFamily.PYTHON),
        handler: 'index.handler',
        code: lambda.Code.fromAsset(__dirname),
        role,
      });
      expect(functionErrorExemption(stack, handler)).toBeUndefined();
    });

    it('check 6 fails on another path to an exempt handler in any template and passes once removed', () => {
      const copy = JSON.parse(JSON.stringify(templates)) as Record<string, Json>;
      const publisher = copy.PublisherStack as Json;
      const backfill = functionLogicalId(
        stacks.PublisherStack as cdk.Stack,
        (stacks.PublisherStack as cdk.Stack).node.findChild('Backfill') as lambda.Function,
      );
      const seed = (stacks.InsightPromptRegistrySeedStack as cdk.Stack).node
        .findChild('Seed') as lambda.Function;
      const seedName = (seed.node.defaultChild as lambda.CfnFunction).functionName as string;

      publisher.Resources.Caller = {
        Type: 'AWS::StepFunctions::StateMachine',
        Properties: { DefinitionString: { 'Fn::Join': ['', ['{"Resource":"', { 'Fn::GetAtt': [backfill, 'Arn'] }, '"}']] } },
      };
      expect(exemptFunctionViolations(stacks, copy)).toStrictEqual([
        'Publisher/Backfill: exempt as a provider onEvent handler, but '
        + 'PublisherStack/Caller (AWS::StepFunctions::StateMachine) references it',
      ]);
      delete publisher.Resources.Caller;

      (copy.ApiStack as Json).Resources.Caller = {
        Type: 'AWS::IAM::Policy',
        Properties: {
          PolicyDocument: {
            Statement: [{
              Effect: 'Allow',
              Action: 'lambda:InvokeFunction',
              Resource: `arn:aws:lambda:${TEST_CONFIG.region}:${TEST_CONFIG.account}:function:${seedName}`,
            }],
          },
        },
      };
      expect(exemptFunctionViolations(stacks, copy)).toStrictEqual([
        'InsightPromptRegistrySeed/Seed: exempt as a provider onEvent handler, but '
        + 'ApiStack/Caller allows invoking it',
      ]);
      (copy.ApiStack as Json).Resources.Caller.Properties.PolicyDocument.Statement[0].Resource = '*';
      const anyFunction = ' but ApiStack/Caller allows invoking any function';
      expect(exemptFunctionViolations(stacks, copy)).toStrictEqual([
        `Publisher/Backfill: exempt as a provider onEvent handler,${anyFunction}`,
        `Publisher/BackfillProvider/framework-onEvent: exempt as a provider framework function,${anyFunction}`,
        `Frontend/${SINGLETON_ID}: exempt as a singleton helper,${anyFunction}`,
        `Auth/${SINGLETON_ID}: exempt as a singleton helper,${anyFunction}`,
        `InsightPromptRegistrySeed/Seed: exempt as a provider onEvent handler,${anyFunction}`,
        'InsightPromptRegistrySeed/SeedProvider/framework-onEvent: exempt as a provider framework '
        + `function,${anyFunction}`,
      ]);
      delete (copy.ApiStack as Json).Resources.Caller;
      expect(exemptFunctionViolations(stacks, copy)).toStrictEqual([]);
    });

    it('check 6 allows a framework function or singleton helper only its custom resources', () => {
      const copy = JSON.parse(JSON.stringify(templates)) as Record<string, Json>;
      const publisher = stacks.PublisherStack as cdk.Stack;
      const auth = stacks.AuthStack as cdk.Stack;
      const backfill = functionLogicalId(publisher, publisher.node.findChild('Backfill') as lambda.Function);
      const framework = functionLogicalId(
        publisher,
        providerFramework(publisher.node.findChild('BackfillProvider') as cr.Provider) as lambda.Function,
      );
      const singleton = functionLogicalId(auth, auth.node.findChild(SINGLETON_ID) as lambda.Function);
      const arn = (id: string): Json => ({ 'Fn::GetAtt': [id, 'Arn'] });
      const resources = (stack: string): Json => (copy[stack] as Json).Resources as Json;

      // Paired control: another custom resource on the framework's service token is its own edge.
      resources('PublisherStack').Extra = { Type: 'Custom::Extra', Properties: { ServiceToken: arn(framework) } };
      expect(exemptFunctionViolations(stacks, copy)).toStrictEqual([]);
      // The same custom resource also carrying the framework's ARN is another reference.
      resources('PublisherStack').Extra.Properties.Target = arn(framework);
      expect(exemptFunctionViolations(stacks, copy)).toStrictEqual([
        'Publisher/BackfillProvider/framework-onEvent: exempt as a provider framework function, but '
        + 'PublisherStack/Extra (Custom::Extra) references it',
      ]);
      // An onEvent handler is never a service token: that is a second path to it.
      resources('PublisherStack').Extra = { Type: 'Custom::Extra', Properties: { ServiceToken: arn(backfill) } };
      expect(exemptFunctionViolations(stacks, copy)).toStrictEqual([
        'Publisher/Backfill: exempt as a provider onEvent handler, but '
        + 'PublisherStack/Extra (Custom::Extra) references it',
      ]);
      delete resources('PublisherStack').Extra;

      resources('AuthStack').Url = {
        Type: 'AWS::Lambda::Url',
        Properties: { AuthType: 'AWS_IAM', TargetFunctionArn: arn(singleton) },
      };
      expect(exemptFunctionViolations(stacks, copy)).toStrictEqual([
        `Auth/${SINGLETON_ID}: exempt as a singleton helper, but AuthStack/Url (AWS::Lambda::Url) references it`,
      ]);
      delete resources('AuthStack').Url;
      expect(exemptFunctionViolations(stacks, copy)).toStrictEqual([]);
    });

    it('a framework function or singleton helper with another invoke path is not exempt and then alarms', () => {
      const render = (
        attach: (framework: lambda.Function, singleton: lambda.Function) => void,
        withMixin: boolean,
      ): { exemptions: (FunctionErrorExemption | undefined)[]; violations: string[] } => {
        const stack = fixtureStack('Reused');
        const provider = new cr.Provider(stack, 'Provider', { onEventHandler: fixtureFunction(stack, 'Handler') });
        new cr.AwsCustomResource(stack, 'Read', {
          onCreate: {
            service: 'SSM',
            action: 'getParameter',
            parameters: { Name: 'fixture' },
            physicalResourceId: cr.PhysicalResourceId.of('fixture'),
          },
          policy: cr.AwsCustomResourcePolicy.fromSdkCalls({
            resources: cr.AwsCustomResourcePolicy.ANY_RESOURCE,
          }),
          installLatestAwsSdk: false,
        });
        const framework = providerFramework(provider) as lambda.Function;
        const singleton = stack.node.findChild(SINGLETON_ID) as lambda.Function;
        attach(framework, singleton);
        if (withMixin) applyFunctionErrorAlarms(stack, TEST_CONFIG, staffedAlarmAction(stack, TEST_CONFIG));
        return {
          exemptions: [functionErrorExemption(stack, framework), functionErrorExemption(stack, singleton)],
          violations: functionCoverage(stack, Template.fromStack(stack).toJSON(), TEST_CONFIG).violations,
        };
      };
      // Paired control: reached only through their custom resources, both stay exempt.
      expect(render(() => undefined, false)).toStrictEqual({
        exemptions: ['provider-framework', 'singleton-helper'],
        violations: [],
      });
      const reuse = (framework: lambda.Function, singleton: lambda.Function): void => {
        framework.addFunctionUrl();
        singleton.addPermission('Scheduled', {
          principal: new cdk.aws_iam.ServicePrincipal('events.amazonaws.com'),
        });
      };
      expect(render(reuse, false)).toStrictEqual({
        exemptions: [undefined, undefined],
        violations: [
          'Reused/Provider/framework-onEvent: 0 AWS/Lambda Errors alarms, not 1',
          `Reused/${SINGLETON_ID}: 0 AWS/Lambda Errors alarms, not 1`,
        ],
      });
      expect(render(reuse, true).violations).toStrictEqual([]);
    });

    it('check 2 fails on a missing or an extra exclusion and passes once corrected', () => {
      const copy = JSON.parse(JSON.stringify(Object.values(templates))) as Json[];
      const [, catchAll] = copy
        .flatMap((template) => resourcesOf(template, 'AWS::CloudWatch::Alarm'))
        .find(([, alarm]) => alarm.AlarmName === `adlytics-dev-${CATCH_ALL_SUFFIX}`) as [string, Json];
      const original = String(catchAll.Metrics[0].Expression);

      catchAll.Metrics[0].Expression = original.replace(" AND Operation != 'ingest.handler'", '');
      expect(catchAllViolations(copy, TEST_CONFIG)).toStrictEqual([
        'ingest.handler has its own alarm and the catch-all also counts it (pages twice)',
      ]);

      catchAll.Metrics[0].Expression = original.replace(
        ' GROUP BY',
        " AND Operation != 'conversation.turn' GROUP BY",
      );
      expect(catchAllViolations(copy, TEST_CONFIG)).toStrictEqual([
        'the catch-all excludes conversation.turn, which has no alarm of its own (silent)',
      ]);

      catchAll.Metrics[0].Expression = original;
      expect(catchAllViolations(copy, TEST_CONFIG)).toStrictEqual([]);
    });

    it('check 2 fails on a second exact alarm for an owned operation and passes once removed', () => {
      const copy = JSON.parse(JSON.stringify(Object.values(templates))) as Json[];
      const name = 'adlytics-dev-ingestion-alarming-errors-ingest-handler';
      const owner = copy.find((template) =>
        resourcesOf(template, 'AWS::CloudWatch::Alarm').some(([, alarm]) => alarm.AlarmName === name),
      ) as Json;
      const [id, real] = resourcesOf(owner, 'AWS::CloudWatch::Alarm').find(
        ([, alarm]) => alarm.AlarmName === name,
      ) as [string, Json];
      const clone = JSON.parse(JSON.stringify(owner.Resources[id])) as Json;
      clone.Properties.AlarmName = `${String(real.AlarmName)}-copy`;
      owner.Resources.DuplicateOwner = clone;
      expect(catchAllViolations(copy, TEST_CONFIG)).toStrictEqual([
        'ingest.handler has 2 alarms of its own, not 1 (pages 2 times)',
      ]);
      delete owner.Resources.DuplicateOwner;
      expect(catchAllViolations(copy, TEST_CONFIG)).toStrictEqual([]);
    });

    it('check 2 lets only an exact page-capable Errors alarm own an excluded operation', () => {
      const copy = JSON.parse(JSON.stringify(Object.values(templates))) as Json[];
      const alarms = copy.flatMap((template) => resourcesOf(template, 'AWS::CloudWatch::Alarm'));
      const [, catchAll] = alarms.find(
        ([, alarm]) => alarm.AlarmName === `adlytics-dev-${CATCH_ALL_SUFFIX}`,
      ) as [string, Json];
      catchAll.Metrics[0].Expression = String(catchAll.Metrics[0].Expression).replace(
        ' GROUP BY',
        " AND Operation != 'conversation.turn' GROUP BY",
      );
      const owner = copy.find((template) =>
        resourcesOf(template, 'AWS::CloudWatch::Alarm').some(
          ([, alarm]) => alarm.AlarmName === 'adlytics-dev-ingestion-alarming-errors-ingest-handler',
        ),
      ) as Json;
      const [, real] = resourcesOf(owner, 'AWS::CloudWatch::Alarm').find(
        ([, alarm]) => alarm.AlarmName === 'adlytics-dev-ingestion-alarming-errors-ingest-handler',
      ) as [string, Json];
      const silent = ['the catch-all excludes conversation.turn, which has no alarm of its own (silent)'];
      const withOwner = (change: (properties: Json) => void): string[] => {
        const properties = JSON.parse(JSON.stringify(real)) as Json;
        properties.AlarmName = 'adlytics-dev-conversation-turn-errors';
        properties.Metrics[0].Expression = alarmingErrorsQuery('conversation.turn');
        change(properties);
        owner.Resources.ConversationTurnErrors = { Type: 'AWS::CloudWatch::Alarm', Properties: properties };
        return catchAllViolations(copy, TEST_CONFIG);
      };

      // Paired control: the exact shape owns the operation, so the exclusion is not silent.
      expect(withOwner(() => undefined)).toStrictEqual([]);
      // Another metric filtered on the same Operation.
      expect(withOwner((p) => {
        p.Metrics[0].Expression = 'SELECT AVG(Latency) FROM SCHEMA("Adlytics", Operation) '
          + "WHERE Operation = 'conversation.turn'";
      })).toStrictEqual(silent);
      // The Errors metric, but the product-signal classes rather than the paging ones.
      expect(withOwner((p) => {
        p.Metrics[0].Expression = 'SELECT SUM(Errors) FROM SCHEMA("Adlytics", ErrorClass, ErrorCode, '
          + "Operation) WHERE ErrorClass = 'domain' AND Operation = 'conversation.turn'";
      })).toStrictEqual(silent);
      // A threshold or comparison that one paging error cannot breach.
      expect(withOwner((p) => { p.Threshold = 1000000; })).toStrictEqual(silent);
      expect(withOwner((p) => { p.ComparisonOperator = 'LessThanThreshold'; })).toStrictEqual(silent);
      // Not acting, or acting elsewhere than the staffed switch.
      expect(withOwner((p) => { p.ActionsEnabled = false; })).toStrictEqual(silent);
      expect(withOwner((p) => {
        p.AlarmActions = ['arn:aws:sns:us-east-1:111111111111:somewhere-else'];
      })).toStrictEqual(silent);
    });

    const reachPageAlarm = (copy: Json[]): [Json, string, Json] => {
      const name = 'adlytics-dev-page-reach-server-errors';
      const owner = copy.find((template) =>
        resourcesOf(template, 'AWS::CloudWatch::Alarm').some(([, alarm]) => alarm.AlarmName === name),
      ) as Json;
      const [id, alarm] = resourcesOf(owner, 'AWS::CloudWatch::Alarm').find(
        ([, candidate]) => candidate.AlarmName === name,
      ) as [string, Json];
      return [owner, id, alarm];
    };

    it('check 2 lets only an exact page alarm on a page path own a page operation', () => {
      const silent = ['the catch-all excludes presentation.reach, which has no alarm of its own (silent)'];
      const withReach = (change: (alarm: Json) => void): string[] => {
        const copy = JSON.parse(JSON.stringify(Object.values(templates))) as Json[];
        change(reachPageAlarm(copy)[2]);
        return catchAllViolations(copy, TEST_CONFIG);
      };
      // Paired control: the unmodified page alarm owns presentation.reach.
      expect(withReach(() => undefined)).toStrictEqual([]);
      // Negative control: the exact shape on a Resource that is not a page path owns nothing.
      expect(withReach((alarm) => {
        for (const query of alarm.Metrics as Json[]) {
          for (const dimension of (query.MetricStat?.Metric?.Dimensions ?? []) as Json[]) {
            if (dimension.Name === 'Resource') dimension.Value = '/api/presentation/not-a-page';
          }
        }
      })).toStrictEqual(silent);
      // Silence read as good, so a broken page with no callers would clear.
      expect(withReach((alarm) => { alarm.TreatMissingData = 'notBreaching'; })).toStrictEqual(silent);
      // No OK action, so a recovery is never announced.
      expect(withReach((alarm) => { delete alarm.OKActions; })).toStrictEqual(silent);
      // The 5xx alone, which a refusal-only period clears.
      expect(withReach((alarm) => {
        (alarm.Metrics as Json[]).find((query) => query.Id === 'expr_1')!.Expression = 'FILL(m_5xx, 0)';
      })).toStrictEqual(silent);
    });

    it('check 8 fails on a renamed page path or a media path no longer unrouted', () => {
      const source = fs.readFileSync(HANDLER_PY, 'utf8');
      expect(pageRosterViolations(source)).toStrictEqual([]);
      const renamed = source.replace(
        'REACH_PATH = "/api/presentation/reach"',
        'REACH_PATH = "/api/presentation/reach-and-funnel"',
      );
      expect(renamed).not.toBe(source);
      expect(pageRosterViolations(renamed)).toStrictEqual([
        'handler.py routes /api/presentation/reach-and-funnel -> presentation.reach, which PAGE_ROUTES '
        + 'does not declare',
        'PAGE_ROUTES declares /api/presentation/reach -> presentation.reach, which handler.py does not route',
      ]);
      expect(pageRosterViolations(source, PAGE_ROUTES, UNROUTED_PRESENTATION_PATHS.slice(1))).toStrictEqual([
        'handler.py routes /api/presentation/media-efficiency/scopes -> '
        + 'presentation.media_efficiency.scopes, which PAGE_ROUTES does not declare',
      ]);
    });

    it('check 9 fails on a deleted or a duplicated page alarm and passes once corrected', () => {
      const copy = JSON.parse(JSON.stringify(Object.values(templates))) as Json[];
      const [owner, id] = reachPageAlarm(copy);
      const original = owner.Resources[id] as Json;
      delete owner.Resources[id];
      expect(pageAlarmViolations(copy, TEST_CONFIG)).toStrictEqual([
        'presentation.reach has 0 page alarms, not 1',
      ]);
      owner.Resources[id] = original;
      owner.Resources.DuplicatePageAlarm = JSON.parse(JSON.stringify(original)) as Json;
      expect(pageAlarmViolations(copy, TEST_CONFIG)).toStrictEqual([
        'presentation.reach has 2 page alarms, not 1',
      ]);
      delete owner.Resources.DuplicatePageAlarm;
      expect(pageAlarmViolations(copy, TEST_CONFIG)).toStrictEqual([]);
    });

    it('check 7 fails on a runbook query over another source, a lost instruction or a lost pointer', () => {
      const markdown = fs.readFileSync(RUNBOOK, 'utf8');
      const anchor = String(CATCH_ALL_RUNBOOK.split('#')[1]);
      const alarm = catchAllAlarm();
      expect(runbookViolations(markdown, alarm)).toStrictEqual([]);

      const query = (text: string): string =>
        String(/"Expression":"(.*?)"\}\]'/.exec(text.replace(/'"'"'/g, "'").replace(/\\"/g, '"'))?.[1]);
      const otherNamespace = markdown.replace('SCHEMA(\\"Adlytics\\"', 'SCHEMA(\\"Other\\"');
      expect(runbookViolations(otherNamespace, alarm)).toStrictEqual([
        `#${anchor} query is not the alarm's Errors source: ${query(otherNamespace)}`,
      ]);
      const allClasses = markdown.replace(
        ` AND ErrorClass != '"'"'validation'"'"'`,
        '',
      );
      expect(runbookViolations(allClasses, alarm)).toStrictEqual([
        `#${anchor} query is not the alarm's Errors source: ${query(allClasses)}`,
      ]);
      const excluding = markdown.replace(
        ' GROUP BY Operation ORDER',
        ` AND Operation != '"'"'ingest.handler'"'"' GROUP BY Operation ORDER`,
      );
      expect(runbookViolations(excluding, alarm)).toStrictEqual([
        `#${anchor} query is not the alarm's Errors source: ${query(excluding)}`,
      ]);
      // Error-code groups would spend the 500-series limit before every operation is listed.
      const finer = markdown.replace(' GROUP BY Operation ORDER', ' GROUP BY Operation, ErrorClass, ErrorCode ORDER');
      expect(runbookViolations(finer, alarm)).toStrictEqual([
        `#${anchor} query is not the alarm's Errors source: ${query(finer)}`,
      ]);
      expect(runbookViolations(markdown.replace('Whenever the state reason reads', 'If'), alarm))
        .toStrictEqual([
          `#${anchor} does not tell the operator to run the query when the reason shows 100+`,
        ]);
      expect(runbookViolations(markdown.replace('## Operation catch-all alarm', '## Catch-all'), alarm))
        .toStrictEqual([`${CATCH_ALL_RUNBOOK.split('#')[0]} has no section #${anchor}`]);
      expect(runbookViolations(markdown, { ...alarm, AlarmDescription: 'no pointer' }))
        .toStrictEqual([`the catch-all description does not name ${CATCH_ALL_RUNBOOK}`]);
    });

    it('check 3 fails on an alarm without an action and passes once it has one', () => {
      const render = (withAction: boolean): Record<string, Json> => {
        const stack = fixtureStack('Fixture');
        const alarm = new cloudwatch.Alarm(stack, 'Silent', {
          alarmName: 'adlytics-dev-silent',
          metric: new cloudwatch.Metric({ namespace: 'Adlytics', metricName: 'Fixture' }),
          threshold: 1,
          evaluationPeriods: 1,
        });
        if (withAction) alarm.addAlarmAction(staffedAlarmAction(stack, TEST_CONFIG));
        return { Fixture: Template.fromStack(stack).toJSON() };
      };
      const without = render(false);
      const [id] = Object.keys((without.Fixture as Json).Resources);
      expect(actionViolations(without, TEST_CONFIG)).toStrictEqual([
        `Fixture/${id} (adlytics-dev-silent): no alarm action`,
      ]);
      expect(actionViolations(render(true), TEST_CONFIG)).toStrictEqual([]);
    });

    it('check 4 fails when a class stops paging and the query still counts it', () => {
      const source = fs.readFileSync(ERRORS_PY, 'utf8').replace(
        'return self.error_class in (ErrorClass.DEPENDENCY, ErrorClass.INVARIANT)',
        'return self.error_class in (ErrorClass.INVARIANT,)',
      );
      expect(nonAlarmingErrorClasses(source)).toStrictEqual(['dependency', 'domain', 'validation']);
      expect([...NON_ALARMING_ERROR_CLASSES].sort()).not.toStrictEqual(
        nonAlarmingErrorClasses(source),
      );
    });
  });
});
