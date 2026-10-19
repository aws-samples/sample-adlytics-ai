// SPDX-License-Identifier: MIT-0
/**
 * S6 part 2 (G2-12, design §4 "Call inventory"): every new AWS call of the GRAPH chain is granted
 * by exactly one §2.11 row (or by a grant that predates it), and no row grants a call the code
 * does not make. The paired controls remove one row grant, and duplicate one, and show the check
 * then fails on exactly that call.
 */
import * as path from 'node:path';

import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';

import { paramPath } from '../lib/config';
import { ApiStack } from '../lib/stacks/api-stack';
import { IngestionStack } from '../lib/stacks/ingestion-stack';
import { InsightPrecomputeStack } from '../lib/stacks/insight-precompute-stack';
import { OperationalStatusProcessorStack } from '../lib/stacks/operational-status-processor-stack';
import { PublisherStack } from '../lib/stacks/publisher-stack';
import { configWith } from './support/config';
import {
  GRAPH_CALLS,
  type GraphCall,
  type GraphCallStack,
  type GraphCallTarget,
  type GraphRow,
  ROW_STATEMENTS,
} from './support/graph-call-inventory';

type Json = Record<string, any>;
type Statement = Json;

const ASSET = __dirname;
const PUBLISHER_INPUT = path.join(__dirname, '..', '..', 'presentation', 'pages', 'v2', 'pages.yaml');
const CONFIG = configWith({
  transactionSearchEnabled: true,
  operationalStatus: {
    provisioned: true,
    stage1LifecycleReconcileBatchSize: 10,
    stage1OutboxMaxConsecutiveFailedDeliveryAttempts: 3,
  },
});

function synthesize(): Record<GraphCallStack, Json> {
  const app = new cdk.App();
  const env = { account: CONFIG.account, region: CONFIG.region };
  const stacks = {
    IngestionStack: new IngestionStack(app, 'Ingestion', { config: CONFIG, env, sourceAssetPath: ASSET }),
    PublisherStack: new PublisherStack(app, 'Publisher', {
      config: CONFIG,
      env,
      sourceAssetPath: ASSET,
      publisherInputPath: PUBLISHER_INPUT,
    }),
    InsightPrecomputeStack: new InsightPrecomputeStack(app, 'InsightPrecompute', {
      config: CONFIG,
      env,
      sourceAssetPath: ASSET,
    }),
    ApiStack: new ApiStack(app, 'Api', { config: CONFIG, env, sourceAssetPath: ASSET }),
    OperationalStatusProcessorStack: new OperationalStatusProcessorStack(
      app,
      'OperationalStatusProcessor',
      { config: CONFIG, env, sourceAssetPath: ASSET },
    ),
  };
  return Object.fromEntries(
    Object.entries(stacks).map(([name, stack]) => [name, Template.fromStack(stack).toJSON()]),
  ) as Record<GraphCallStack, Json>;
}

const list = <T>(value: T | T[] | undefined): T[] =>
  value === undefined ? [] : Array.isArray(value) ? value : [value];

/** Render a resource to text: parameter references to their default path, joins joined. */
function resolve(value: unknown, template: Json): string {
  if (typeof value === 'string') return value;
  if (value !== null && typeof value === 'object') {
    const record = value as Json;
    if (typeof record.Ref === 'string') {
      return template.Parameters?.[record.Ref]?.Default ?? `{${record.Ref}}`;
    }
    const join = record['Fn::Join'];
    if (Array.isArray(join)) return (join[1] as unknown[]).map((part) => resolve(part, template)).join(join[0]);
    const getAtt = record['Fn::GetAtt'];
    if (Array.isArray(getAtt)) {
      const queue = template.Resources?.[getAtt[0]];
      if (queue?.Type === 'AWS::SQS::Queue' && getAtt[1] === 'Arn') return `queue:${queue.Properties.QueueName}`;
      return `{${getAtt.join('.')}}`;
    }
  }
  return JSON.stringify(value);
}

function targets(resource: string, target: GraphCallTarget): boolean {
  if (resource === '*') return true;
  if ('queue' in target) {
    return resource === `queue:${target.queue}` || resource.endsWith(`:${target.queue}`);
  }
  switch (target.table) {
    case 'graph':
      return resource.endsWith(':table/adlytics-dev-execution-graph');
    case 'publications':
      return (
        resource.endsWith(':table/adlytics-dev-v2-semantic-publications')
        || resource.endsWith(`:table/${paramPath('dev', 'semanticV2PublicationsTableName')}`)
      );
    case 'status':
      return resource === paramPath('dev', 'operationalStatusProjectionTableArn');
  }
}

/** An IAM string pattern (`*`, `?`) as a whole-string, case-sensitive regular expression. */
function glob(pattern: string): RegExp {
  return new RegExp(`^${pattern.replace(/[.+^${}()|[\]\\]/g, '\\$&').replace(/\*/g, '.*').replace(/\?/g, '.')}$`);
}

/** Does every condition of the statement hold for this call? An unknown key never holds. */
function conditionsHold(statement: Statement, call: GraphCall): boolean {
  const sample = call.key === undefined ? undefined : call.key.endsWith('#') || call.key.endsWith('|')
    ? `${call.key}sample`
    : call.key;
  for (const [operator, entries] of Object.entries((statement.Condition ?? {}) as Json)) {
    for (const [name, raw] of Object.entries(entries as Json)) {
      const values = list(raw as string | string[]);
      if (name === 'dynamodb:LeadingKeys') {
        if (sample === undefined) return false;
        const like = operator.endsWith('StringLike');
        if (!values.some((value) => (like ? glob(value).test(sample) : value === sample))) return false;
      } else if (name === 'dynamodb:EnclosingOperation' && operator === 'StringEquals') {
        if (!values.includes(call.enclosing ?? '')) return false;
      } else {
        return false;
      }
    }
  }
  return true;
}

function matches(statement: Statement, call: GraphCall, template: Json): boolean {
  if (statement.NotAction !== undefined || statement.NotResource !== undefined) return false;
  if (!list(statement.Action as string | string[]).some((action) => glob(action).test(call.action))) return false;
  if (!list(statement.Resource).some((resource) => targets(resolve(resource, template), call.target))) return false;
  return conditionsHold(statement, call);
}

/** Every statement on the role of the function named `adlytics-dev-<lambda>`. */
function roleStatements(template: Json, lambda: string): Statement[] {
  const functions = Object.values(template.Resources as Json).filter(
    (resource) =>
      resource.Type === 'AWS::Lambda::Function'
      && resource.Properties.FunctionName === `adlytics-dev-${lambda}`,
  );
  if (functions.length !== 1) throw new Error(`expected one function adlytics-dev-${lambda}`);
  const role = functions[0].Properties.Role['Fn::GetAtt'][0] as string;
  return Object.values(template.Resources as Json)
    .filter((resource) => resource.Type === 'AWS::IAM::Policy' || resource.Type === 'AWS::IAM::ManagedPolicy')
    .filter((resource) => list(resource.Properties.Roles).some((candidate: Json) => candidate.Ref === role))
    .flatMap((resource) => list(resource.Properties.PolicyDocument.Statement as Statement[]));
}

const describeCall = (call: GraphCall): string =>
  `${call.lambda} ${call.action} ${JSON.stringify(call.target)} ${call.key ?? ''} ${call.enclosing ?? 'direct'}`;

/** Every inventory failure, as text; empty means every call is granted exactly as declared. */
function coverage(
  templates: Record<GraphCallStack, Json>,
  rowStatements: Readonly<Record<string, Readonly<Record<string, GraphRow>>>> = ROW_STATEMENTS,
): string[] {
  const failures: string[] = [];
  const used = new Set<string>();
  for (const call of GRAPH_CALLS) {
    const template = templates[call.stack];
    const statements = roleStatements(template, call.lambda);
    const rows = rowStatements[call.lambda] ?? {};
    const allowing = statements.filter((s) => s.Effect === 'Allow' && matches(s, call, template));
    const denying = statements.filter((s) => s.Effect === 'Deny' && matches(s, call, template));
    const byRow = allowing.filter((s) => typeof s.Sid === 'string' && s.Sid in rows);
    if (denying.length > 0) failures.push(`denied: ${describeCall(call)}`);
    if (call.row === 'existing') {
      if (allowing.length === 0) failures.push(`ungranted: ${describeCall(call)}`);
      if (byRow.length > 0) failures.push(`a row also grants an existing call: ${describeCall(call)}`);
      continue;
    }
    // Two statements of one row may both allow a call (a direct-call grant with no
    // EnclosingOperation condition also covers that action inside a transaction); two rows may not.
    const granting = [...new Set(byRow.map((statement) => rows[statement.Sid as string]))].toSorted();
    if (granting.length !== 1 || granting[0] !== call.row) {
      failures.push(`granted by rows [${granting.join(', ')}], not exactly row ${call.row}: ${describeCall(call)}`);
    }
    for (const statement of byRow) used.add(`${call.lambda}/${statement.Sid as string}`);
  }
  for (const [lambda, sids] of Object.entries(rowStatements)) {
    for (const sid of Object.keys(sids)) {
      if (!used.has(`${lambda}/${sid}`)) failures.push(`row statement grants no inventoried call: ${lambda}/${sid}`);
    }
  }
  return failures;
}

function withoutStatement(templates: Record<GraphCallStack, Json>, lambda: string, sid: string): Record<GraphCallStack, Json> {
  const copy = JSON.parse(JSON.stringify(templates)) as Record<GraphCallStack, Json>;
  for (const template of Object.values(copy)) {
    for (const statements of roleStatementLists(template, lambda)) {
      const index = statements.findIndex((statement) => statement.Sid === sid);
      if (index >= 0) statements.splice(index, 1);
    }
  }
  return copy;
}

function roleStatementLists(template: Json, lambda: string): Statement[][] {
  const fn = Object.values(template.Resources as Json).find(
    (resource) =>
      resource.Type === 'AWS::Lambda::Function'
      && resource.Properties.FunctionName === `adlytics-dev-${lambda}`,
  );
  if (fn === undefined) return [];
  const role = fn.Properties.Role['Fn::GetAtt'][0] as string;
  return Object.values(template.Resources as Json)
    .filter((resource) => resource.Type === 'AWS::IAM::Policy' || resource.Type === 'AWS::IAM::ManagedPolicy')
    .filter((resource) => list(resource.Properties.Roles).some((candidate: Json) => candidate.Ref === role))
    .map((resource) => resource.Properties.PolicyDocument.Statement as Statement[]);
}

/** The stage-1 fence and the retired day/wait/telemetry prefixes (plan item 4, D7). */
const RETIRED_KEYS = ['MIGRATION#', 'SERVING_DAY#', 'CAPACITY_WAIT#', 'PUBLISH_WAIT#', 'SERVING_TELEMETRY#'];

/** Every `stack/Sid` of any synthesized policy statement that names a retired prefix. */
function retiredGrants(templates: Record<GraphCallStack, Json>): string[] {
  const found: string[] = [];
  for (const [stack, template] of Object.entries(templates)) {
    for (const resource of Object.values(template.Resources as Json)) {
      if (resource.Type !== 'AWS::IAM::Policy' && resource.Type !== 'AWS::IAM::ManagedPolicy') continue;
      for (const statement of list(resource.Properties.PolicyDocument.Statement as Statement[])) {
        const text = JSON.stringify(statement);
        if (RETIRED_KEYS.some((prefix) => text.includes(prefix))) found.push(`${stack}/${String(statement.Sid)}`);
      }
    }
  }
  return found;
}

describe('the GRAPH chain call inventory (G2-12)', () => {
  const templates = synthesize();

  it('lists calls from every slice that adds one, each naming its code path', () => {
    expect([...new Set(GRAPH_CALLS.map((call) => call.slice))].toSorted()).toEqual(
      ['2', '3', '4a', '4b', '5i', '5ii', '6', '7', 'bake-v3'],
    );
    for (const call of GRAPH_CALLS) expect(call.operation.length).toBeGreaterThan(0);
    // Every §2.11 row that adds a grant is inventoried (rows 4 and 11 add none; 5b, 9 and 10
    // are alarms, schedules and the operator profile, not Lambda grants).
    expect([...new Set(GRAPH_CALLS.map((call) => call.row))].toSorted()).toEqual(
      ['1', '2', '2b', '3', '5', '6', '7', '8', 'existing'],
    );
  });

  it('grants every call by exactly its declared row, and no row grants an uninventoried call', () => {
    expect(coverage(templates)).toEqual([]);
  });

  it('control: removing one row grant fails on exactly the calls it carried', () => {
    const failures = coverage(withoutStatement(templates, 'TableWriter', 'UpdateGraphClaims'));
    expect(failures).toEqual([
      'granted by rows [], not exactly row 2: TableWriter dynamodb:UpdateItem {"table":"graph"} EXEC_CLAIM# direct',
      'row statement grants no inventoried call: TableWriter/UpdateGraphClaims',
    ]);
  });

  it('control: removing the delivery handler Query (row 3) fails its one call', () => {
    const failures = coverage(withoutStatement(templates, 'IngestionFunction', 'QueryGraphRunNodes'));
    expect(failures).toEqual([
      'granted by rows [], not exactly row 3: IngestionFunction dynamodb:Query {"table":"graph"} RUN# direct',
      'row statement grants no inventoried call: IngestionFunction/QueryGraphRunNodes',
    ]);
  });

  // Slice 7 (D38): the fence settler and node verifier now release a counted `LOCK#` share
  // (UpdateItem, not the holder delete). Both map to their existing family transaction grant;
  // removing it fails exactly that path's calls, so the inventory proves each one.
  it('control: removing the fence settler family grant fails its slice-7 fence_unsent calls', () => {
    const failures = coverage(withoutStatement(templates, 'ExecutionGraphFenceSettler', 'TransactGraphFamilies'));
    expect(failures).toEqual([
      'ungranted: ExecutionGraphFenceSettler dynamodb:UpdateItem {"table":"graph"} RUN# TransactWriteItems',
      'ungranted: ExecutionGraphFenceSettler dynamodb:DeleteItem {"table":"graph"} TOKEN# TransactWriteItems',
      'ungranted: ExecutionGraphFenceSettler dynamodb:UpdateItem {"table":"graph"} LOCK# TransactWriteItems',
      'ungranted: ExecutionGraphFenceSettler dynamodb:ConditionCheckItem {"table":"graph"} RUN# TransactWriteItems',
    ]);
  });

  it('control: removing the node verifier family grant fails its slice-7 settle_unit_set calls', () => {
    const failures = coverage(withoutStatement(templates, 'ExecutionGraphNodeVerifier', 'TransactGraphFamilies'));
    expect(failures).toEqual([
      'ungranted: ExecutionGraphNodeVerifier dynamodb:UpdateItem {"table":"graph"} RUN# TransactWriteItems',
      'ungranted: ExecutionGraphNodeVerifier dynamodb:DeleteItem {"table":"graph"} TOKEN# TransactWriteItems',
      'ungranted: ExecutionGraphNodeVerifier dynamodb:UpdateItem {"table":"graph"} LOCK# TransactWriteItems',
      'ungranted: ExecutionGraphNodeVerifier dynamodb:DeleteItem {"table":"graph"} DUE# TransactWriteItems',
    ]);
  });

  it('lists the slice-7 counted-release calls of every principal that releases a TABLE# share', () => {
    const principals = new Set(
      GRAPH_CALLS.filter(
        (call) => call.slice === '7' && call.key === 'LOCK#' && call.action === 'dynamodb:UpdateItem',
      ).map((call) => call.lambda),
    );
    expect([...principals].toSorted()).toEqual([
      'ExecutionGraphDispatcher',
      'ExecutionGraphFenceSettler',
      'ExecutionGraphNodeVerifier',
      'TableWriter',
    ]);
  });

  it('control: a second row granting the same call is refused as an overlap', () => {
    const copy = JSON.parse(JSON.stringify(templates)) as Record<GraphCallStack, Json>;
    const [statements] = roleStatementLists(copy.ApiStack, 'PresentationFunction')
      .filter((found) => found.some((statement) => statement.Sid === 'ReadGraphServingBindings'));
    const original = statements!.find((statement) => statement.Sid === 'ReadGraphServingBindings')!;
    // The same reads granted again under another row (6, the mode-read row).
    statements!.push({ ...original, Sid: 'ReadGraphServingBindingsCopy' });
    const rows = {
      ...ROW_STATEMENTS,
      PresentationFunction: { ...ROW_STATEMENTS.PresentationFunction, ReadGraphServingBindingsCopy: '6' as const },
    };
    expect(coverage(copy, rows)).toEqual([
      'granted by rows [6, 8], not exactly row 8: PresentationFunction dynamodb:GetItem {"table":"graph"} MODE#ingestion direct',
      'granted by rows [6, 8], not exactly row 8: PresentationFunction dynamodb:GetItem {"table":"graph"} SERVING# direct',
    ]);
  });

  // Plan item 4 (D7 final tip): the inert migration fence is historical data only. No inventory
  // call addresses it or a retired day/wait/telemetry key, and no synthesized policy in any
  // GRAPH stack grants one; the promoter keeps exactly its transition read and create-once put.
  it('final tip: no call or policy names the fence or a retired day/wait prefix', () => {
    expect(GRAPH_CALLS.filter((call) => call.key !== undefined && RETIRED_KEYS.some(
      (prefix) => call.key!.startsWith(prefix),
    ))).toEqual([]);
    expect(retiredGrants(templates)).toEqual([]);
    const transition = GRAPH_CALLS.filter((call) => call.key === 'SERVING_TRANSITION#')
      .map((call) => `${call.lambda} ${call.action}`).toSorted();
    expect(transition).toEqual([
      'ExecutionGraphPromoter dynamodb:GetItem',
      'ExecutionGraphPromoter dynamodb:PutItem',
    ]);
  });

  it('control: a reinstated fence or legacy-day grant is reported by stack and Sid', () => {
    const copy = JSON.parse(JSON.stringify(templates)) as Record<GraphCallStack, Json>;
    const [statements] = roleStatementLists(copy.IngestionStack, 'TableWriter');
    statements!.push({
      Sid: 'ConditionRollingFreshnessMigrationFence',
      Effect: 'Allow',
      Action: 'dynamodb:ConditionCheckItem',
      Resource: '*',
      Condition: { 'ForAllValues:StringLike': { 'dynamodb:LeadingKeys': ['MIGRATION#*'] } },
    });
    const [promoter] = roleStatementLists(copy.IngestionStack, 'ExecutionGraphPromoter');
    promoter!.push({
      Sid: 'ReserveServingDay',
      Effect: 'Allow',
      Action: 'dynamodb:UpdateItem',
      Resource: '*',
      Condition: { 'ForAllValues:StringLike': { 'dynamodb:LeadingKeys': ['SERVING_DAY#*'] } },
    });
    expect(retiredGrants(copy).toSorted()).toEqual([
      'IngestionStack/ConditionRollingFreshnessMigrationFence',
      'IngestionStack/ReserveServingDay',
    ]);
  });

  it('row 8 grants no REVISION# read (D7 hold) and row 3 adds no second GetItem', () => {
    const presentation = roleStatements(templates.ApiStack, 'PresentationFunction');
    expect(JSON.stringify(presentation)).not.toContain('REVISION#');
    const delivery = roleStatements(templates.IngestionStack, 'IngestionFunction').filter(
      (statement) => list(statement.Action as string | string[]).includes('dynamodb:GetItem')
        && list(statement.Resource).some((resource) => targets(resolve(resource, templates.IngestionStack), { table: 'graph' })),
    );
    expect(delivery.map((statement) => statement.Sid).toSorted()).toEqual([
      'AdmitRunRequests',
      'ReadAndConditionCheckGraphModeItem',
    ]);
  });
});
