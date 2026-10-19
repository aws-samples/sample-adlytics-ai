// SPDX-License-Identifier: MIT-0
/**
 * The Athena start roster (plan S3, 3.6, F3b): the synthesized principals allowed
 * `athena:StartQueryExecution` on the BAKE, INSIGHT_PRECOMPUTE and retained `batch` workgroups map
 * one-to-one onto the seven static start-budget rows. IAM is the enforcement; this proves it. The
 * three BAKE probes run under the operator's own profile, outside every synthesized role (F5).
 */
import { semanticV2AthenaWorkgroupName } from '../lib/config';
import { athenaStartBudgetRows } from '../lib/config/athena-start-budget';
import { TEST_CONFIG } from './support/config';
import { athenaStartRoles } from './support/iam';
import { everyTemplate, resourcesOfType } from './support/templates';

type Template = Record<string, unknown>;
type Props = Record<string, unknown>;

const templates = everyTemplate();
const ENV = TEST_CONFIG.envName;
const BAKE = semanticV2AthenaWorkgroupName(ENV, 'bake');
const INSIGHT = semanticV2AthenaWorkgroupName(ENV, 'insight-precompute');
const BATCH = semanticV2AthenaWorkgroupName(ENV, 'batch');

/** `<template key>/<role logical id>` of the one function with this handler. */
function roleOfHandler(stack: string, handler: string): string {
  const functions = resourcesOfType(templates[stack], 'AWS::Lambda::Function').filter(
    (fn) => fn.Handler === handler,
  );
  const role = (functions[0]?.Role as { 'Fn::GetAtt'?: unknown[] } | undefined)?.['Fn::GetAtt']?.[0];
  if (functions.length !== 1 || typeof role !== 'string') throw new Error(`no single role for ${handler}`);
  return `${stack}/${role}`;
}

function roleNamed(stack: string, name: string): string {
  const resources = ((templates[stack] as Template).Resources ?? {}) as Record<string, Props>;
  const ids = Object.keys(resources).filter(
    (id) => id.includes(name) && resources[id]?.Type === 'AWS::IAM::Role',
  );
  if (ids.length !== 1) throw new Error(`expected one ${name} role, found ${ids.length}`);
  return `${stack}/${ids[0]}`;
}

const BAKER = roleOfHandler('PublisherStack', 'adapters.jobs.preset_artifact_publisher.baker.handler');
const LOAD = roleOfHandler('IngestionStack', 'adapters.inbound.ingestion_handlers.load.handler');
const WRITER = roleOfHandler('IngestionStack', 'adapters.inbound.ingestion_handlers.table_writer.handler');
const RECONCILER = roleOfHandler('IngestionStack', 'adapters.jobs.provider_attempt_reconciler.handler');
const OPERATOR = roleNamed('IngestionStack', 'SemanticProjectionOperatorRole');
const PRECOMPUTE = roleOfHandler('InsightPrecomputeStack', 'adapters.jobs.insight_precompute.handler.handler');

/** The closed role -> budget-row map per workgroup. */
const ROSTER: Record<string, Record<string, readonly string[]>> = {
  [BAKE]: {
    [BAKER]: ['baker'],
    [LOAD]: ['inline_ingestion'],
    [WRITER]: ['table_writer'],
    [RECONCILER]: ['provider_attempt_reconciler'],
    [OPERATOR]: ['ingest_operator', 'reprocess_operator'],
  },
  [INSIGHT]: { [PRECOMPUTE]: ['insight_precompute'] },
  // AGG-6 residual: only the reconciler, whose row also covers its starts here.
  [BATCH]: { [RECONCILER]: ['provider_attempt_reconciler'] },
};

const sorted = (values: Iterable<string>): string[] => [...values].sort();

function mappedRows(roster: Record<string, Record<string, readonly string[]>>): string[] {
  return sorted(new Set(Object.values(roster).flatMap((roles) => Object.values(roles).flat())));
}

/** A copy of the templates with one extra role granted `action` on `resource`. */
function withExtraRole(action: string, resource: string): Record<string, unknown> {
  const copy = JSON.parse(JSON.stringify(templates)) as Record<string, Template>;
  const resources = (copy.PublisherStack?.Resources ?? {}) as Record<string, Props>;
  resources.RogueRole = { Type: 'AWS::IAM::Role', Properties: {} };
  resources.RoguePolicy = {
    Type: 'AWS::IAM::Policy',
    Properties: {
      Roles: [{ Ref: 'RogueRole' }],
      PolicyDocument: { Statement: [{ Effect: 'Allow', Action: action, Resource: resource }] },
    },
  };
  return copy;
}

describe('Athena start principals equal the static start-budget roster', () => {
  it.each([BAKE, INSIGHT, BATCH])('the roles allowed to start on %s are exactly the roster', (workgroup) => {
    expect(sorted(athenaStartRoles(templates, workgroup))).toEqual(sorted(Object.keys(ROSTER[workgroup] ?? {})));
  });

  it('maps the roster onto exactly the seven budget rows', () => {
    expect(mappedRows(ROSTER)).toEqual(sorted(athenaStartBudgetRows(TEST_CONFIG).map((row) => row.rowId)));
  });

  it('control: one extra role granted Start on BAKE breaks the roster equality', () => {
    const rogue = withExtraRole('athena:StartQueryExecution', `arn:aws:athena:eu-west-1:111122223333:workgroup/${BAKE}`);
    const found = athenaStartRoles(rogue, BAKE);
    expect(found.has('PublisherStack/RogueRole')).toBe(true);
    expect(sorted(found)).not.toEqual(sorted(Object.keys(ROSTER[BAKE] ?? {})));
  });

  it('control: a role granted athena:* on workgroup/* is found by the helper', () => {
    const broad = withExtraRole('athena:*', 'arn:aws:athena:eu-west-1:111122223333:workgroup/*');
    for (const workgroup of [BAKE, INSIGHT, BATCH]) {
      expect(athenaStartRoles(broad, workgroup).has('PublisherStack/RogueRole')).toBe(true);
    }
    // Paired negative: a grant on another workgroup is not a start on BAKE.
    const other = withExtraRole('athena:StartQueryExecution', 'arn:aws:athena:eu-west-1:111122223333:workgroup/other');
    expect(athenaStartRoles(other, BAKE).has('PublisherStack/RogueRole')).toBe(false);
  });

  it('control: a roster missing one row id fails the union assertion', () => {
    const missing = { ...ROSTER, [BAKE]: { ...ROSTER[BAKE], [OPERATOR]: ['ingest_operator'] } };
    expect(mappedRows(missing)).not.toEqual(sorted(athenaStartBudgetRows(TEST_CONFIG).map((row) => row.rowId)));
  });
});
