// SPDX-License-Identifier: MIT-0
import * as fs from 'node:fs';
import * as path from 'node:path';

import * as cdk from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';

import {
  ATHENA_ENGINE_VERSION,
  paramPath,
  semanticV2AthenaWorkgroupName,
  semanticV2DatabaseName,
  semanticV2StagingDatabaseName,
  semanticV2StateTableName,
} from '../lib/config';
import { loadGeneratedV2Storage } from '../lib/constructs/data-lake/generated-tables';
import { STAGING_TABLE_NAMES, stagingTables } from '../lib/constructs/data-lake/staging-table';
import { dataBucketName } from '../lib/mixins/storage';
import { DataV2Stack } from '../lib/stacks/data-v2-stack';
import { TEST_CONFIG } from './support/config';

const TABLES = path.join(__dirname, '..', '..', 'contracts', 'generated', 'v2-tables.json');
type Props = Record<string, any>;
type GeneratedColumn = { fieldId: number; name: string; nullable: boolean };
type GeneratedTable = {
  name: string;
  grain: string[];
  partitionKeys: string[];
  columns: GeneratedColumn[];
};
const generated = JSON.parse(fs.readFileSync(TABLES, 'utf-8')) as {
  sourceAlignedTables: GeneratedTable[];
  canonicalCandidateTables: GeneratedTable[];
  canonicalCurrentTables: GeneratedTable[];
};

/** Every workgroup kind with its scan-cutoff multiple: interactive 1, every DML class 10. */
const WORKGROUP_KINDS = [
  ['interactive', 1],
  ['batch', 10],
  ['bake', 10],
  ['insight-precompute', 10],
] as const;

/** Key-order-independent JSON text, so a comparison sees values and never property order. */
function canonical(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
  if (value !== null && typeof value === 'object') {
    const entries = Object.entries(value as Props).sort(([a], [b]) => a.localeCompare(b));
    return `{${entries.map(([key, item]) => `${JSON.stringify(key)}:${canonical(item)}`).join(',')}}`;
  }
  return JSON.stringify(value);
}

function workgroupsByName(template: Props): Record<string, Props> {
  return Object.fromEntries(
    Object.values(template.Resources as Record<string, Props>)
      .filter((resource) => resource.Type === 'AWS::Athena::WorkGroup')
      .map((resource) => [resource.Properties.Name as string, resource.Properties as Props]),
  );
}

/** Each kind's exact expected configuration; returns `<kind>: <field>` for every difference. */
function workgroupMismatches(template: Props): string[] {
  const byName = workgroupsByName(template);
  const mismatches: string[] = [];
  for (const [kind, multiple] of WORKGROUP_KINDS) {
    const actual = byName[semanticV2AthenaWorkgroupName(TEST_CONFIG.envName, kind)];
    if (actual === undefined) {
      mismatches.push(`${kind}: missing`);
      continue;
    }
    const expected: Props = {
      State: 'ENABLED',
      EnforceWorkGroupConfiguration: true,
      BytesScannedCutoffPerQuery: TEST_CONFIG.athenaBytesScannedCutoff * multiple,
      PublishCloudWatchMetricsEnabled: true,
      RequesterPaysEnabled: false,
      EngineVersion: { SelectedEngineVersion: ATHENA_ENGINE_VERSION },
      ResultConfiguration: {
        OutputLocation: `s3://${dataBucketName(TEST_CONFIG, 'v2-results')}/${kind}/`,
        EncryptionConfiguration: { EncryptionOption: 'SSE_S3' },
      },
    };
    const observed: Props = { State: actual.State, ...actual.WorkGroupConfiguration };
    for (const field of Object.keys(expected)) {
      if (canonical(observed[field]) !== canonical(expected[field])) {
        mismatches.push(`${kind}: ${field}`);
      }
    }
  }
  return mismatches;
}

function synth(): Template {
  const app = new cdk.App();
  return Template.fromStack(
    new DataV2Stack(app, 'DataV2', {
      config: TEST_CONFIG,
      env: { account: TEST_CONFIG.account, region: TEST_CONFIG.region },
      tablesPath: TABLES,
    }),
  );
}

describe('isolated retained Semantic V2 storage', () => {
  it('creates three disjoint generated databases', () => {
    for (const layer of ['source-aligned', 'candidate', 'current'] as const) {
      synth().hasResourceProperties('AWS::Glue::Database', {
        DatabaseInput: { Name: semanticV2DatabaseName('dev', layer) },
      });
    }
  });

  it('creates exactly every generated source, candidate and current table, and their staging twins', () => {
    const staging = new Set([
      ...generated.sourceAlignedTables.map((table) => table.name),
      ...generated.canonicalCandidateTables.map((table) => table.name),
    ]).size;
    expect(staging).toBe(54);
    const expected =
      generated.sourceAlignedTables.length +
      generated.canonicalCandidateTables.length +
      generated.canonicalCurrentTables.length +
      staging;
    synth().resourceCountIs('AWS::Glue::Table', expected);
  });

  it('renders governed field, identifier, and partition ids for both canonical layers', () => {
    const resources = synth().toJSON().Resources as Record<string, any>;
    for (const [database, tables] of [
      [semanticV2DatabaseName('dev', 'candidate'), generated.canonicalCandidateTables],
      [semanticV2DatabaseName('dev', 'current'), generated.canonicalCurrentTables],
    ] as const) {
      for (const table of tables) {
        const resource = Object.values(resources).find(
          (candidate) =>
            candidate.Type === 'AWS::Glue::Table' &&
            candidate.Properties.Name === table.name &&
            candidate.Properties.DatabaseName === database,
        );
        expect(resource).toBeDefined();
        const input = resource.Properties.OpenTableFormatInput.IcebergInput.IcebergTableInput;
        expect(
          input.Schema.Fields.map((field: { Id: number; Name: string }) => ({
            fieldId: field.Id,
            name: field.Name,
          })),
        ).toEqual(
          table.columns.map((column) => ({
            fieldId: column.fieldId,
            name: column.name,
          })),
        );
        const byName = new Map(table.columns.map((column) => [column.name, column]));
        const identifiable = table.grain.every((name) => byName.get(name)?.nullable === false);
        expect(input.Schema.IdentifierFieldIds).toEqual(
          identifiable ? table.grain.map((name) => byName.get(name)?.fieldId) : undefined,
        );
        expect(input.PartitionSpec.Fields).toEqual(
          table.partitionKeys.map((name) => ({
            Name: name,
            SourceId: byName.get(name)?.fieldId,
            Transform: 'identity',
          })),
        );
      }
    }
  });

  it('uses isolated raw, semantic, Athena-results and staging buckets', () => {
    synth().resourceCountIs('AWS::S3::Bucket', 4);
    const staging = Object.values(synth().findResources('AWS::S3::Bucket', {
      Properties: { BucketName: `adlytics-dev-v2-staging-${TEST_CONFIG.account}` },
    })) as Record<string, any>[];
    expect(staging).toHaveLength(1);
    expect(staging[0]?.Properties.VersioningConfiguration).toBeUndefined();
    synth().hasResourceProperties('AWS::S3::Bucket', {
      BucketName: `adlytics-dev-semantic-v2-raw-${TEST_CONFIG.account}`,
      VersioningConfiguration: { Status: 'Enabled' },
      NotificationConfiguration: { EventBridgeConfiguration: { EventBridgeEnabled: true } },
      BucketEncryption: {
        ServerSideEncryptionConfiguration: [
          { ServerSideEncryptionByDefault: { SSEAlgorithm: 'AES256' } },
        ],
      },
    });
    synth().hasResourceProperties('AWS::S3::Bucket', {
      BucketName: `adlytics-dev-semantic-v2-${TEST_CONFIG.account}`,
      BucketEncryption: {
        ServerSideEncryptionConfiguration: [
          { ServerSideEncryptionByDefault: { SSEAlgorithm: 'AES256' } },
        ],
      },
      PublicAccessBlockConfiguration: {
        BlockPublicAcls: true,
        BlockPublicPolicy: true,
        IgnorePublicAcls: true,
        RestrictPublicBuckets: true,
      },
    });
    synth().hasResourceProperties('AWS::S3::Bucket', {
      BucketName: dataBucketName(TEST_CONFIG, 'v2-results'),
      LifecycleConfiguration: {
        Rules: Match.arrayWith([
          Match.objectLike({ Id: 'expire-query-results', Status: 'Enabled' }),
        ]),
      },
    });
    expect(JSON.stringify(synth().toJSON())).not.toContain('adlytics_dev"');
  });

  it('creates four separate enforced engine-v3 workgroups, each on its own result prefix', () => {
    const template = synth().toJSON();
    expect(workgroupMismatches(template)).toEqual([]);
    expect(Object.keys(workgroupsByName(template)).sort()).toEqual(
      WORKGROUP_KINDS.map(([kind]) => semanticV2AthenaWorkgroupName(TEST_CONFIG.envName, kind)).sort(),
    );
    // Paired control: the same helper reports a bake workgroup writing into the batch prefix,
    // and the retained batch workgroup is still present in that mutated template.
    const mutated = JSON.parse(JSON.stringify(template)) as Props;
    const bake = workgroupsByName(mutated)[semanticV2AthenaWorkgroupName(TEST_CONFIG.envName, 'bake')];
    if (bake === undefined) throw new Error('the bake workgroup is missing');
    bake.WorkGroupConfiguration.ResultConfiguration.OutputLocation =
      `s3://${dataBucketName(TEST_CONFIG, 'v2-results')}/batch/`;
    expect(workgroupMismatches(mutated)).toEqual(['bake: ResultConfiguration']);
    expect(workgroupsByName(mutated)[semanticV2AthenaWorkgroupName(TEST_CONFIG.envName, 'batch')]).toBeDefined();
  });

  it('expires query results bucket-wide with no prefix filter', () => {
    const results = Object.values(synth().toJSON().Resources as Record<string, Props>).find(
      (resource) =>
        resource.Type === 'AWS::S3::Bucket' &&
        (resource.Properties as Props).BucketName === dataBucketName(TEST_CONFIG, 'v2-results'),
    );
    const rules = ((results?.Properties as Props).LifecycleConfiguration.Rules ?? []) as Props[];
    const expiry = rules.find((rule) => rule.Id === 'expire-query-results');
    expect(expiry).toBeDefined();
    expect(expiry?.Prefix).toBeUndefined();
    expect(expiry?.Filter).toBeUndefined();
  });

  it('creates isolated point-in-time recoverable publication authorities', () => {
    synth().resourceCountIs('AWS::DynamoDB::Table', 3);
    for (const kind of ['attempts', 'leases', 'publications'] as const) {
      synth().hasResourceProperties('AWS::DynamoDB::Table', {
        TableName: semanticV2StateTableName(TEST_CONFIG.envName, kind),
        BillingMode: 'PAY_PER_REQUEST',
        PointInTimeRecoverySpecification: { PointInTimeRecoveryEnabled: true },
      });
    }
    synth().hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: semanticV2StateTableName(TEST_CONFIG.envName, 'attempts'),
      StreamSpecification: { StreamViewType: 'NEW_IMAGE' },
    });
    const publications = Object.values(
      synth().toJSON().Resources as Record<string, Props>,
    ).find(
      (resource) =>
        resource.Type === 'AWS::DynamoDB::Table' &&
        (resource.Properties as Props).TableName ===
          semanticV2StateTableName(TEST_CONFIG.envName, 'publications'),
    );
    const indexes = ((publications?.Properties as Props).GlobalSecondaryIndexes ?? []) as Props[];
    expect(indexes).toEqual([
      {
        IndexName: 'scope-current',
        KeySchema: [
          { AttributeName: 'coverage_scope', KeyType: 'HASH' },
          { AttributeName: 'bucket_start', KeyType: 'RANGE' },
        ],
        Projection: { ProjectionType: 'KEYS_ONLY' },
      },
      {
        IndexName: 'table-attempt-reconcile',
        KeySchema: [
          { AttributeName: 'reconcile_status', KeyType: 'HASH' },
          { AttributeName: 'reconcile_after_or_block', KeyType: 'RANGE' },
        ],
        Projection: { ProjectionType: 'KEYS_ONLY' },
      },
      {
        IndexName: 'stage1-plan-intent-ready',
        KeySchema: [
          { AttributeName: 'ready_status', KeyType: 'HASH' },
          { AttributeName: 'ready_order', KeyType: 'RANGE' },
        ],
        Projection: { ProjectionType: 'KEYS_ONLY' },
      },
      {
        IndexName: 'stage1-outbox-pending',
        KeySchema: [
          { AttributeName: 'outbox_status', KeyType: 'HASH' },
          { AttributeName: 'outbox_order', KeyType: 'RANGE' },
        ],
        Projection: { ProjectionType: 'KEYS_ONLY' },
      },
    ]);
  });

  it('publishes every clean V2 storage, execution, and state contract', () => {
    const keys = [
      'semanticV2RawBucketName',
      'semanticV2BucketName',
      'semanticV2AthenaResultsBucketName',
      'semanticV2AthenaWorkgroupInteractive',
      'semanticV2AthenaWorkgroupBatch',
      'semanticV2AthenaWorkgroupBake',
      'semanticV2AthenaWorkgroupInsightPrecompute',
      'semanticV2AttemptsTableName',
      'semanticV2AttemptsStreamArn',
      'semanticV2LeasesTableName',
      'semanticV2PublicationsTableName',
      'semanticV2PublicationsStreamArn',
      'semanticV2SourceDatabase',
      'semanticV2CandidateDatabase',
      'semanticV2CurrentDatabase',
      'semanticV2StagingBucketName',
      'semanticV2StagingDatabase',
    ] as const;
    synth().resourceCountIs('AWS::SSM::Parameter', keys.length);
    for (const key of keys) {
      synth().hasResourceProperties('AWS::SSM::Parameter', {
        Name: paramPath(TEST_CONFIG.envName, key),
        Type: 'String',
      });
    }
    for (const [kind, key] of [
      ['interactive', 'semanticV2AthenaWorkgroupInteractive'],
      ['batch', 'semanticV2AthenaWorkgroupBatch'],
      ['bake', 'semanticV2AthenaWorkgroupBake'],
      ['insight-precompute', 'semanticV2AthenaWorkgroupInsightPrecompute'],
    ] as const) {
      synth().hasResourceProperties('AWS::SSM::Parameter', {
        Name: paramPath(TEST_CONFIG.envName, key),
        Type: 'String',
        Value: semanticV2AthenaWorkgroupName(TEST_CONFIG.envName, kind),
      });
    }
  });

  it('retains every data-bearing resource on replacement or delete', () => {
    const resources = synth().toJSON().Resources as Record<
      string,
      { Type: string; DeletionPolicy?: string; UpdateReplacePolicy?: string }
    >;
    for (const resource of Object.values(resources)) {
      if (['AWS::S3::Bucket', 'AWS::Glue::Database', 'AWS::Glue::Table'].includes(resource.Type)) {
        expect(resource.DeletionPolicy).toBe('Retain');
        expect(resource.UpdateReplacePolicy).toBe('Retain');
      }
    }
  });
});

describe('ADR-149 staging storage (ING-INFRA, T-14)', () => {
  const bucket = `adlytics-dev-v2-staging-${TEST_CONFIG.account}`;
  const database = semanticV2StagingDatabaseName('dev');
  const stagingTablesOf = (template: Template): Props[] =>
    Object.values(template.findResources('AWS::Glue::Table'))
      .map((resource) => (resource as Props).Properties as Props)
      .filter((properties) => properties.DatabaseName === database);

  it('creates one all-string external table per distinct physical table name', () => {
    const template = synth();
    template.hasResourceProperties('AWS::Glue::Database', {
      DatabaseInput: { Name: database, LocationUri: `s3://${bucket}/` },
    });
    const tables = stagingTablesOf(template);
    expect(tables.map((table) => table.TableInput.Name).sort()).toEqual([...STAGING_TABLE_NAMES]);
    const byName = new Map(
      [...generated.sourceAlignedTables, ...generated.canonicalCandidateTables].map((table) => [
        table.name,
        table,
      ]),
    );
    for (const table of tables) {
      const input = table.TableInput as Props;
      const source = byName.get(input.Name as string)!;
      expect(input.TableType).toBe('EXTERNAL_TABLE');
      expect(input.PartitionKeys).toEqual([{ Name: 'staging_key', Type: 'string' }]);
      expect(input.Parameters).toEqual({
        classification: 'json',
        'projection.enabled': 'true',
        'projection.staging_key.type': 'injected',
        'storage.location.template': `s3://${bucket}/${input.Name}/\${staging_key}/`,
      });
      const descriptor = input.StorageDescriptor as Props;
      expect(descriptor.Columns).toEqual([
        ...source.columns.map((column) => ({ Name: column.name, Type: 'string' })),
        { Name: 'stg_ordinal', Type: 'string' },
      ]);
      expect(descriptor.Location).toBe(`s3://${bucket}/${input.Name}/`);
      expect(descriptor.InputFormat).toBe('org.apache.hadoop.mapred.TextInputFormat');
      expect(descriptor.OutputFormat).toBe('org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat');
      expect(descriptor.SerdeInfo).toEqual({
        SerializationLibrary: 'org.openx.data.jsonserde.JsonSerDe',
        Parameters: { 'ignore.malformed.json': 'false' },
      });
    }
  });

  it('keeps staged units in a private, encrypted, TLS-only bucket that expires them', () => {
    const template = synth();
    template.hasResourceProperties('AWS::S3::Bucket', {
      BucketName: bucket,
      BucketEncryption: {
        ServerSideEncryptionConfiguration: [
          { ServerSideEncryptionByDefault: { SSEAlgorithm: 'AES256' } },
        ],
      },
      PublicAccessBlockConfiguration: {
        BlockPublicAcls: true,
        BlockPublicPolicy: true,
        IgnorePublicAcls: true,
        RestrictPublicBuckets: true,
      },
      LifecycleConfiguration: {
        Rules: [
          {
            Id: 'expire-staged-units',
            Status: 'Enabled',
            ExpirationInDays: TEST_CONFIG.data.stagingExpireAfterDays,
          },
          {
            Id: 'abort-incomplete-multipart-uploads',
            Status: 'Enabled',
            AbortIncompleteMultipartUpload: {
              DaysAfterInitiation: TEST_CONFIG.data.abortIncompleteUploadsAfterDays,
            },
          },
        ],
      },
    });
    expect(TEST_CONFIG.data.stagingExpireAfterDays).toBe(7);
    const policies = Object.values(template.findResources('AWS::S3::BucketPolicy')).filter((policy) =>
      JSON.stringify(policy).includes(bucket) || JSON.stringify(policy).includes('SemanticV2StagingBucket'),
    );
    expect(policies).toHaveLength(1);
    expect(JSON.stringify(policies[0])).toContain('aws:SecureTransport');
    expect(JSON.stringify(policies[0])).toContain('s3:TlsVersion');
  });

  it('refuses at synth a candidate/current pair whose columns differ', () => {
    const drifted = JSON.parse(fs.readFileSync(TABLES, 'utf-8')) as Props;
    const current = drifted.canonicalCurrentTables as Props[];
    current[0]!.columns = [...(current[0]!.columns as Props[]), { ...(current[0]!.columns as Props[])[0], fieldId: 999, name: 'drifted' }];
    const loaded = {
      ...loadGeneratedV2Storage(TABLES),
      canonicalCurrentTables: current as never,
    };
    expect(() => stagingTables(loaded)).toThrow(/candidate and current columns differ/);
    // Control: the committed catalog stages cleanly.
    expect(stagingTables(loadGeneratedV2Storage(TABLES))).toHaveLength(54);
  });

  it('refuses at synth a catalog column named like a staging column', () => {
    const storage = loadGeneratedV2Storage(TABLES);
    const [first, ...rest] = storage.sourceAlignedTables;
    const reserved = {
      ...storage,
      sourceAlignedTables: [
        { ...first!, columns: [...first!.columns, { ...first!.columns[0]!, fieldId: 999, name: 'stg_ordinal' }] },
        ...rest,
      ],
    };
    expect(() => stagingTables(reserved)).toThrow(/reserved staging column name/);
  });
});
