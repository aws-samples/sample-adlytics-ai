// SPDX-License-Identifier: MIT-0
/**
 * Every Python Lambda that can host the in-process ADOT bootstrap is wired to export spans.
 *
 * A per-stack assertion stops covering the next function somebody adds. This gate walks every
 * synthesised template instead, so a new Python function must either receive the tracing
 * environment and the span-export grant or be named here with the reason it cannot.
 */
import { everyTemplate, resourcesOfType } from './support/templates';

type Props = Record<string, unknown>;

/**
 * The Cognito pre-token trigger is deliberately stdlib-only: it ships without the dependencies
 * layer, so it cannot import Powertools or the OTel API and has no bootstrap to configure. Adding
 * the layer would mean changing a RETAIN stack for a function that performs no AWS calls.
 */
const NO_DEPENDENCIES_LAYER = new Set(['adapters.jobs.quality_review_token.handler.handler']);

const TRACING_KEYS = [
  'ADL_ENVIRONMENT',
  'ADL_AWS_REGION',
  'ADL_TRACING_ENABLED',
  'ADL_TRACE_SAMPLE_RATIO',
] as const;

function isPythonFunction(fn: Props): boolean {
  const runtime = fn.Runtime;
  const handler = fn.Handler;
  return (
    typeof runtime === 'string' &&
    runtime.startsWith('python') &&
    typeof handler === 'string' &&
    handler.startsWith('adapters.')
  );
}

function roleLogicalId(fn: Props): string {
  const role = (fn.Role as { 'Fn::GetAtt'?: unknown[] } | undefined)?.['Fn::GetAtt']?.[0];
  if (typeof role !== 'string') throw new Error(`${String(fn.Handler)} has no role logical id`);
  return role;
}

function actionsOf(statement: Props): string[] {
  const value = statement.Action;
  if (Array.isArray(value)) return value.filter((item): item is string => typeof item === 'string');
  return typeof value === 'string' ? [value] : [];
}

function roleActions(template: Props, role: string): string[] {
  return resourcesOfType(template, 'AWS::IAM::Policy')
    .filter((policy) =>
      ((policy.Roles ?? []) as Props[]).some((candidate) => candidate.Ref === role),
    )
    .flatMap((policy) => (((policy.PolicyDocument as Props).Statement ?? []) as Props[]))
    .flatMap(actionsOf);
}

describe('span export coverage', () => {
  const functions = Object.entries(everyTemplate()).flatMap(([stackName, template]) =>
    resourcesOfType(template as Props, 'AWS::Lambda::Function')
      .filter(isPythonFunction)
      .map((fn) => ({ stackName, template: template as Props, fn })),
  );

  it('finds Python application functions to cover', () => {
    expect(functions.length).toBeGreaterThan(10);
  });

  it.each(functions.map((item) => [`${item.stackName} ${String(item.fn.Handler)}`, item]))(
    '%s carries the tracing environment and the span-export grant',
    (_label, item) => {
      const handler = String(item.fn.Handler);
      const environment = ((item.fn.Environment as Props | undefined)?.Variables ?? {}) as Props;
      const actions = roleActions(item.template, roleLogicalId(item.fn));
      if (NO_DEPENDENCIES_LAYER.has(handler)) {
        expect(item.fn.Layers).toBeUndefined();
        expect(actions).not.toContain('xray:PutTraceSegments');
        return;
      }
      for (const key of TRACING_KEYS) {
        expect(environment).toHaveProperty(key);
      }
      expect(environment.ADL_TRACING_ENABLED).toMatch(/^(true|false)$/);
      expect(actions).toContain('xray:PutTraceSegments');
      expect(actions).toContain('xray:PutTelemetryRecords');
    },
  );
});
