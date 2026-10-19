// SPDX-License-Identifier: MIT-0
/**
 * The retained execution-graph store (S1) and its probe isolation (V-15, CDK half).
 *
 * The live table is RETAIN, encrypted and point-in-time recoverable. The probe pair is built by
 * the same construct from the same schema. Only the probe role has actions on the probe tables,
 * exactly the generated set, and nothing on the live table; no other role in any stack names a
 * probe table, and only the run-request admission role writes the live table.
 */
import { everyTemplate, resourcesOfType } from './support/templates';
import {
  OPERATOR_ONLY_DENIED_ACTIONS,
  OPERATOR_ONLY_PREFIXES,
  PROBE_ROLE_ACTIONS,
  PROBE_TABLE_SUFFIXES,
  WRITER_ACTIONS,
} from '../lib/generated/execution-graph-protocol';

type Resource = Record<string, unknown>;
const templates = everyTemplate();
const store = templates.ControlPlaneAuthorityStoreStack as Record<string, unknown>;

function resources(template: unknown): Record<string, Resource> {
  return ((template as { Resources?: Record<string, Resource> }).Resources ?? {});
}

function tables(): Record<string, Resource> {
  return Object.fromEntries(
    Object.entries(resources(store)).filter(([, item]) => item.Type === 'AWS::DynamoDB::Table'),
  );
}

function statementsReferencing(template: unknown, needle: string): Resource[] {
  return Object.values(resources(template))
    // A role whose inline policy outgrows its size limit overflows into a ManagedPolicy (CDK), as
    // TableWriter's does once §2.11 row 2 lands, so a grant there is counted too.
    .filter((item) => ['AWS::IAM::Policy', 'AWS::IAM::ManagedPolicy', 'AWS::IAM::Role']
      .includes(String(item.Type)))
    .flatMap((item) => {
      const properties = item.Properties as Resource;
      const documents = [
        properties.PolicyDocument,
        ...(((properties.Policies as Resource[] | undefined) ?? []).map((p) => p.PolicyDocument)),
      ];
      return documents.flatMap(
        (document) => ((document as { Statement?: Resource[] } | undefined)?.Statement ?? []),
      );
    })
    .filter((statement) => {
      const rendered = JSON.stringify(statement.Resource ?? '');
      return rendered.includes(needle) || rendered.includes(needle.replace(/-/g, ''));
    });
}

describe('the retained execution-graph store', () => {
  it('creates the live table and the probe pair, all retained, encrypted and recoverable', () => {
    const all = tables();
    const names = Object.values(all).map((item) => (item.Properties as Resource).TableName).sort();
    // Owner-gate Item 20 retired S5's staffed-destination table (its acknowledgement path).
    expect(names).toEqual([
      'adlytics-dev-execution-graph',
      ...PROBE_TABLE_SUFFIXES.map((suffix) => `adlytics-dev-${suffix}`),
    ].sort());
    for (const table of Object.values(all)) {
      const properties = table.Properties as Resource;
      expect(table.DeletionPolicy).toBe('Retain');
      expect(table.UpdateReplacePolicy).toBe('Retain');
      expect(properties.SSESpecification).toEqual({ SSEEnabled: true });
      expect(properties.PointInTimeRecoverySpecification).toEqual({
        PointInTimeRecoveryEnabled: true,
      });
      expect(properties.DeletionProtectionEnabled).toBe(true);
      expect(properties.KeySchema).toEqual([
        { AttributeName: 'pk', KeyType: 'HASH' },
        { AttributeName: 'sk', KeyType: 'RANGE' },
      ]);
      expect(properties.TimeToLiveSpecification).toBeUndefined();
    }
  });

  it('publishes the live table name and ARN through SSM only (ADR-012)', () => {
    const parameters = resourcesOfType(store, 'AWS::SSM::Parameter')
      .map((item) => item.Name)
      .sort();
    expect(parameters).toEqual([
      '/adlytics/dev/operations/execution-graph-table-arn',
      '/adlytics/dev/operations/execution-graph-table-name',
      '/adlytics/dev/operations/execution-graph-table-stream-arn',
    ]);
  });

  it('S2: only the live table streams new images; the probe tables have no stream', () => {
    for (const table of Object.values(tables())) {
      const properties = table.Properties as Resource;
      if (properties.TableName === 'adlytics-dev-execution-graph') {
        expect(properties.StreamSpecification).toEqual({ StreamViewType: 'NEW_IMAGE' });
      } else {
        expect(properties.StreamSpecification).toBeUndefined();
      }
    }
  });

  it('S2 (BF-5): no cursor table exists and no read role holds a graph write action', () => {
    for (const [name, template] of Object.entries(templates)) {
      const names = Object.values((template as { Resources?: Record<string, Resource> }).Resources ?? {})
        .filter((item) => item.Type === 'AWS::DynamoDB::Table')
        .map((item) => String((item.Properties as Resource).TableName ?? ''));
      expect([name, names.filter((table) => table.includes('cursor'))]).toEqual([name, []]);
    }
    const writes = ['dynamodb:PutItem', 'dynamodb:UpdateItem', 'dynamodb:DeleteItem',
      'dynamodb:BatchWriteItem', 'dynamodb:ConditionCheckItem'];
    const api = templates.ApiStack as Record<string, unknown>;
    const reads = statementsReferencing(api, 'execution-graph-table-arn').filter(
      (statement) => statement.Sid === 'ReadExecutionGraphAuthority',
    );
    expect(reads).toHaveLength(1);
    expect(reads[0]!.Action).toEqual(['dynamodb:GetItem', 'dynamodb:BatchGetItem']);
    for (const action of writes) expect(reads[0]!.Action).not.toContain(action);
  });

  it('gives the probe role exactly the generated action set on the probe tables only', () => {
    const roles = Object.entries(resources(store)).filter(([, item]) => item.Type === 'AWS::IAM::Role');
    expect(roles).toHaveLength(1);
    const policies = Object.values(resources(store)).filter((item) => item.Type === 'AWS::IAM::Policy');
    expect(policies).toHaveLength(1);
    const statements = ((policies[0]!.Properties as Resource).PolicyDocument as {
      Statement: Resource[];
    }).Statement;
    // S5: the probe-scope gauge put (namespace-conditioned) and readback, and nothing else.
    expect(statements).toHaveLength(3);
    const [statement, put, readback] = statements;
    expect(statement!.Action).toEqual([...PROBE_ROLE_ACTIONS]);
    expect(put).toEqual({
      Sid: 'ProbeScopeGauges',
      Effect: 'Allow',
      Action: 'cloudwatch:PutMetricData',
      Resource: '*',
      Condition: { StringEquals: { 'cloudwatch:namespace': 'Adlytics' } },
    });
    expect(readback).toEqual({
      Sid: 'ProbeScopeGaugeReadback',
      Effect: 'Allow',
      Action: 'cloudwatch:GetMetricData',
      Resource: '*',
    });
    const probeIds = Object.entries(tables())
      .filter(([, item]) => (item.Properties as Resource).TableName !== 'adlytics-dev-execution-graph')
      .map(([id]) => id)
      .sort();
    const named = (statement!.Resource as { 'Fn::GetAtt': string[] }[])
      .map((reference) => reference['Fn::GetAtt'][0])
      .sort();
    expect(named).toEqual(probeIds);
  });

  it('V-15 negative: no role outside the store stack names a probe table', () => {
    for (const [name, template] of Object.entries(templates)) {
      if (name === 'ControlPlaneAuthorityStoreStack') continue;
      for (const suffix of PROBE_TABLE_SUFFIXES) {
        expect([name, statementsReferencing(template, suffix)]).toEqual([name, []]);
      }
    }
  });

  it('V-15 positive: only the admission roles, the five graph workers and TableWriter write the live table', () => {
    const api = templates.ApiStack as Record<string, unknown>;
    const writeActions = new Set(['dynamodb:PutItem', 'dynamodb:UpdateItem', 'dynamodb:DeleteItem',
      'dynamodb:BatchWriteItem', 'dynamodb:ConditionCheckItem']);
    // The live table is named through its SSM ARN parameter (Api) or derived at synth from the
    // environment (Ingestion, S3). The closing quote keeps the probe tables out of the match.
    const live = (template: unknown): Resource[] => [
      ...statementsReferencing(template, 'execution-graph-table-arn'),
      ...statementsReferencing(template, 'table/adlytics-dev-execution-graph"'),
    ];
    const modeItemOnly = (statement: Resource): boolean => JSON.stringify(statement.Condition ?? {})
      === JSON.stringify({ 'ForAllValues:StringEquals': { 'dynamodb:LeadingKeys': ['MODE#ingestion'] } });
    const writers = Object.entries(templates).flatMap(([name, template]) =>
      live(template)
        .filter((statement) => statement.Effect !== 'Deny')
        // V-13/R2-26: ConditionCheckItem on the mode item alone is a read-class grant.
        .filter((statement) => !modeItemOnly(statement))
        .filter((statement) => (Array.isArray(statement.Action) ? statement.Action : [statement.Action])
          .some((action) => writeActions.has(String(action))))
        .map((statement) => [name, statement.Sid ?? null, statement.Action]),
    );
    const transactional = ['dynamodb:PutItem', 'dynamodb:UpdateItem', 'dynamodb:DeleteItem',
      'dynamodb:ConditionCheckItem'];
    expect(writers).toEqual([
      ['ApiStack', null, [...WRITER_ACTIONS]],
      // S6 (V-19, S1A-7): the two workload requesters admit exactly as the API does: the SQS
      // delivery handler (`delivery_event`) and the registration finalizer (`schedule`).
      ...Array.from({ length: 2 }, () => ['IngestionStack', 'AdmitRunRequests', [...WRITER_ACTIONS]]),
      // S6 part 2 (§2.11 row 2): TableWriter's family transactions and its one direct UpdateItem
      // on EXEC_CLAIM# (the lease release), outside any transaction.
      ['IngestionStack', 'TransactGraphFamilies', transactional],
      ['IngestionStack', 'PutServingGraphFamilies', 'dynamodb:PutItem'],
      ['IngestionStack', 'UpdateServingGraphFamilies', 'dynamodb:UpdateItem'],
      ['IngestionStack', 'UpdateGraphClaims', 'dynamodb:UpdateItem'],
      // S3's four workers, then S4's release issuer with its scoped serving-authority writes.
      ...Array.from({ length: 2 }, () => ['IngestionStack', 'TransactGraphFamilies', transactional]),
      ['IngestionStack', 'TransactGraphFamilies', transactional],
      ['IngestionStack', 'ConditionPromoterServingAuthority', 'dynamodb:ConditionCheckItem'],
      // The promoter's rolling election, drain and transition writes (`GRAPH_WORKER_FAMILIES.Promoter`).
      ['IngestionStack', 'PutPromoterServingAuthority', 'dynamodb:PutItem'],
      ['IngestionStack', 'UpdatePromoterServingAuthority', 'dynamodb:UpdateItem'],
      ['IngestionStack', 'TransactGraphFamilies', transactional],
      ['IngestionStack', 'TransactGraphFamilies', transactional],
      ['IngestionStack', 'ConditionReleaseServingAuthority', 'dynamodb:ConditionCheckItem'],
      ['IngestionStack', 'PutReleaseServingAuthority', 'dynamodb:PutItem'],
      ['IngestionStack', 'UpdateReleaseServingAuthority', 'dynamodb:UpdateItem'],
    ]);
    // The admission Allow, its Deny, and (S2) the explanation's read-only statement.
    expect(statementsReferencing(api, 'execution-graph-table-arn')).toHaveLength(3);
  });

  it('owner-gate item 6: the admission role is denied every write to the mode and command items', () => {
    const api = templates.ApiStack as Record<string, unknown>;
    const denies = statementsReferencing(api, 'execution-graph-table-arn').filter(
      (statement) => statement.Effect === 'Deny',
    );
    expect(denies).toHaveLength(1);
    const [deny] = denies;
    expect(deny!.Action).toEqual([...OPERATOR_ONLY_DENIED_ACTIONS]);
    expect(deny!.Action).toEqual(
      expect.arrayContaining(['dynamodb:PutItem', 'dynamodb:UpdateItem', 'dynamodb:DeleteItem']),
    );
    expect(deny!.Action).not.toContain('dynamodb:GetItem');
    expect(deny!.Condition).toEqual({
      'ForAnyValue:StringLike': { 'dynamodb:LeadingKeys': ['MODE#*', 'COMMAND#*'] },
    });
    expect(OPERATOR_ONLY_PREFIXES).toEqual(['MODE#', 'COMMAND#']);
    // The Allow keeps GetItem, so the function can still read the mode item.
    const allow = statementsReferencing(api, 'execution-graph-table-arn').find(
      (statement) => statement.Effect !== 'Deny',
    );
    expect(allow!.Action).toContain('dynamodb:GetItem');
    expect(allow!.Condition).toBeUndefined();
  });
});
