// SPDX-License-Identifier: MIT-0
import * as cdk from 'aws-cdk-lib';
import * as athena from 'aws-cdk-lib/aws-athena';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as glue from 'aws-cdk-lib/aws-glue';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';

import {
  AppConfig,
  ATHENA_ENGINE_VERSION,
  AthenaWorkgroupKind,
  paramPath,
  semanticV2AthenaWorkgroupName,
  semanticV2DatabaseName,
  semanticV2StagingDatabaseName,
  SemanticStateTableKind,
  semanticV2StateTableName,
} from '../config';
import {
  GeneratedTable,
  GeneratedV2Storage,
  loadGeneratedV2Storage,
} from '../constructs/data-lake/generated-tables';
import { cfnTableProps } from '../constructs/data-lake/iceberg-table';
import { stagingCfnTableProps, stagingTables } from '../constructs/data-lake/staging-table';
import {
  athenaResultsLifecycle,
  createDataBucket,
  dataBucketName,
  rawZoneLifecycle,
  stagingLifecycle,
} from '../mixins/storage';
import { applyRemovalPolicy, applyStandardTags } from '../mixins/tagging';

export interface DataV2StackProps extends cdk.StackProps {
  readonly config: AppConfig;
  readonly tablesPath: string;
}

const MIN_BYTES_SCANNED_CUTOFF = 10 * 1024 * 1024;
const BATCH_SCAN_MULTIPLE = 10;

/**
 * SEMANTIC-M1: one workgroup per execution class, each with its own result prefix
 * (`s3://<v2-results>/<kind>/`). Every non-interactive class takes the batch scan multiple.
 * `batch` is retained so pre-cutover executions stay readable.
 */
const WORKGROUPS: Readonly<Record<AthenaWorkgroupKind, { logicalId: string; description: string }>> = {
  interactive: {
    logicalId: 'SemanticV2InteractiveWorkgroup',
    description: 'Semantic V2 interactive current-model reads.',
  },
  batch: {
    logicalId: 'SemanticV2BatchWorkgroup',
    description: 'Semantic V2 immutable materialization and exact readback.',
  },
  bake: {
    logicalId: 'SemanticV2BakeWorkgroup',
    description: 'Semantic V2 bake and load DML: Baker, ingestion and operator writes.',
  },
  'insight-precompute': {
    logicalId: 'SemanticV2InsightPrecomputeWorkgroup',
    description: 'Semantic V2 insight precompute reads.',
  },
};

/** Retained, isolated physical storage and publication state for Semantic V2. */
export class DataV2Stack extends cdk.Stack {
  public readonly rawBucket: s3.Bucket;
  public readonly bucket: s3.Bucket;
  public readonly resultsBucket: s3.Bucket;
  /** ADR-149: staged table units, a derived transport copy that expires (ING-RETENTION). */
  public readonly stagingBucket: s3.Bucket;
  public readonly interactiveWorkgroup: athena.CfnWorkGroup;
  public readonly batchWorkgroup: athena.CfnWorkGroup;
  public readonly bakeWorkgroup: athena.CfnWorkGroup;
  public readonly insightPrecomputeWorkgroup: athena.CfnWorkGroup;
  public readonly sourceDatabase: glue.CfnDatabase;
  public readonly candidateDatabase: glue.CfnDatabase;
  public readonly currentDatabase: glue.CfnDatabase;
  public readonly stagingDatabase: glue.CfnDatabase;
  public readonly sourceTables: readonly glue.CfnTable[];
  public readonly candidateTables: readonly glue.CfnTable[];
  public readonly currentTables: readonly glue.CfnTable[];
  public readonly stagingTables: readonly glue.CfnTable[];
  public readonly attemptsTable: dynamodb.Table;
  public readonly leasesTable: dynamodb.Table;
  public readonly publicationsTable: dynamodb.Table;

  constructor(scope: Construct, id: string, props: DataV2StackProps) {
    super(scope, id, props);
    const { config } = props;
    const generated = loadGeneratedV2Storage(props.tablesPath);
    const semanticBucketName = dataBucketName(config, 'semantic-v2');

    this.rawBucket = createDataBucket(this, 'SemanticV2RawBucket', {
      config,
      purpose: 'semantic-v2-raw',
      versioned: true,
      lifecycleRules: rawZoneLifecycle(config),
      eventBridgeEnabled: true,
    });
    this.bucket = createDataBucket(this, 'SemanticV2Bucket', {
      config,
      purpose: 'semantic-v2',
      versioned: false,
      lifecycleRules: [],
    });
    this.resultsBucket = createDataBucket(this, 'SemanticV2AthenaResultsBucket', {
      config,
      purpose: 'v2-results',
      versioned: false,
      lifecycleRules: athenaResultsLifecycle(config),
    });
    // ADR-149 decision 4: a dedicated bucket, not a prefix of the Semantic V2 bucket, so no
    // lifecycle rule sits one prefix filter away from authority and no reader's IAM resource set
    // overlaps it. Unversioned: a staged object is re-rendered byte for byte from authority.
    this.stagingBucket = createDataBucket(this, 'SemanticV2StagingBucket', {
      config,
      purpose: 'v2-staging',
      versioned: false,
      lifecycleRules: stagingLifecycle(config),
    });
    this.interactiveWorkgroup = this.workgroup(config, 'interactive');
    this.batchWorkgroup = this.workgroup(config, 'batch');
    this.bakeWorkgroup = this.workgroup(config, 'bake');
    this.insightPrecomputeWorkgroup = this.workgroup(config, 'insight-precompute');
    this.sourceDatabase = this.database(
      config,
      semanticBucketName,
      'source-aligned',
      'V2SourceAlignedDatabase',
    );
    this.candidateDatabase = this.database(
      config,
      semanticBucketName,
      'candidate',
      'V2CanonicalCandidateDatabase',
    );
    this.currentDatabase = this.database(
      config,
      semanticBucketName,
      'current',
      'V2CanonicalCurrentDatabase',
    );

    const stagingBucketName = dataBucketName(config, 'v2-staging');
    this.stagingDatabase = new glue.CfnDatabase(this, 'V2StagingDatabase', {
      catalogId: this.account,
      databaseInput: {
        name: semanticV2StagingDatabaseName(config.envName),
        description: 'ADR-149 staged table units: generated all-string external tables.',
        locationUri: `s3://${stagingBucketName}/`,
      },
    });
    this.stagingTables = stagingTables(generated).map((table) => {
      const created = new glue.CfnTable(
        this,
        `V2StagingTable${pascal(table.name)}`,
        stagingCfnTableProps({
          table,
          catalogId: this.account,
          databaseName: semanticV2StagingDatabaseName(config.envName),
          bucketName: stagingBucketName,
        }),
      );
      created.addResourceDependency(this.stagingDatabase);
      return created;
    });

    this.sourceTables = this.tables(
      generated.sourceAlignedTables,
      this.sourceDatabase,
      semanticV2DatabaseName(config.envName, 'source-aligned'),
      semanticBucketName,
      generated.sourceAlignedPrefix,
      'V2SourceAlignedTable',
    );
    this.candidateTables = this.tables(
      generated.canonicalCandidateTables,
      this.candidateDatabase,
      semanticV2DatabaseName(config.envName, 'candidate'),
      semanticBucketName,
      generated.canonicalCandidatePrefix,
      'V2CanonicalCandidateTable',
    );
    this.currentTables = this.tables(
      generated.canonicalCurrentTables,
      this.currentDatabase,
      semanticV2DatabaseName(config.envName, 'current'),
      semanticBucketName,
      generated.canonicalCurrentPrefix,
      'V2CanonicalCurrentTable',
    );

    this.attemptsTable = this.stateTable(config, 'attempts');
    this.leasesTable = this.stateTable(config, 'leases');
    this.publicationsTable = this.stateTable(config, 'publications');
    // DynamoDB accepts one GSI creation or deletion per table update, and this table is
    // RETAIN, so on an existing deployment each new index below must land in its own
    // deploy (append one entry, deploy, read the index back ACTIVE, repeat). A first
    // deployment creates every index at once. Verified against the service 2026-09-21.
    for (const index of [
      {
        indexName: 'scope-current',
        partitionKey: 'coverage_scope',
        sortKey: 'bucket_start',
      },
      {
        indexName: 'table-attempt-reconcile',
        partitionKey: 'reconcile_status',
        sortKey: 'reconcile_after_or_block',
      },
      {
        indexName: 'stage1-plan-intent-ready',
        partitionKey: 'ready_status',
        sortKey: 'ready_order',
      },
      {
        indexName: 'stage1-outbox-pending',
        partitionKey: 'outbox_status',
        sortKey: 'outbox_order',
      },
    ] as const) {
      this.publicationsTable.addGlobalSecondaryIndex({
        indexName: index.indexName,
        partitionKey: { name: index.partitionKey, type: dynamodb.AttributeType.STRING },
        sortKey: { name: index.sortKey, type: dynamodb.AttributeType.STRING },
        projectionType: dynamodb.ProjectionType.KEYS_ONLY,
      });
    }

    applyStandardTags(this, config);
    applyRemovalPolicy(this, cdk.RemovalPolicy.RETAIN);
    this.publish(config, generated);
  }

  private workgroup(config: AppConfig, kind: AthenaWorkgroupKind): athena.CfnWorkGroup {
    if (config.athenaBytesScannedCutoff < MIN_BYTES_SCANNED_CUTOFF) {
      throw new Error(
        `athenaBytesScannedCutoff is ${config.athenaBytesScannedCutoff} bytes, below Athena's ` +
          `minimum of ${MIN_BYTES_SCANNED_CUTOFF}.`,
      );
    }
    const { logicalId, description } = WORKGROUPS[kind];
    return new athena.CfnWorkGroup(
      this,
      logicalId,
      {
        name: semanticV2AthenaWorkgroupName(config.envName, kind),
        description,
        state: 'ENABLED',
        workGroupConfiguration: {
          enforceWorkGroupConfiguration: true,
          bytesScannedCutoffPerQuery:
            config.athenaBytesScannedCutoff * (kind === 'interactive' ? 1 : BATCH_SCAN_MULTIPLE),
          publishCloudWatchMetricsEnabled: true,
          requesterPaysEnabled: false,
          engineVersion: { selectedEngineVersion: ATHENA_ENGINE_VERSION },
          resultConfiguration: {
            outputLocation: `s3://${dataBucketName(config, 'v2-results')}/${kind}/`,
            encryptionConfiguration: { encryptionOption: 'SSE_S3' },
          },
        },
      },
    );
  }

  private database(
    config: AppConfig,
    bucketName: string,
    layer: 'source-aligned' | 'candidate' | 'current',
    logicalId: string,
  ): glue.CfnDatabase {
    return new glue.CfnDatabase(this, logicalId, {
      catalogId: this.account,
      databaseInput: {
        name: semanticV2DatabaseName(config.envName, layer),
        // Preserve deployed database metadata while the physical name remains the V2 authority.
        // A descriptive rename has no runtime value and would add noise to the retirement change.
        description: `Semantic V2 ${layer} storage. Additive and isolated from V1.`,
        locationUri: `s3://${bucketName}/`,
      },
    });
  }

  private tables(
    generated: readonly GeneratedTable[],
    database: glue.CfnDatabase,
    databaseName: string,
    bucketName: string,
    prefix: string,
    logicalPrefix: string,
  ): readonly glue.CfnTable[] {
    return generated.map((table) => {
      const created = new glue.CfnTable(this, `${logicalPrefix}${pascal(table.name)}`, {
        ...cfnTableProps({
          table,
          catalogId: this.account,
          databaseName,
          bucketName,
          locationPrefix: `${prefix}/${table.name}`,
        }),
      });
      created.addResourceDependency(database);
      return created;
    });
  }

  private stateTable(config: AppConfig, kind: SemanticStateTableKind): dynamodb.Table {
    const keys: Record<SemanticStateTableKind, { partition: string; sort?: string }> = {
      attempts: { partition: 'attempt_id', sort: 'event_key' },
      leases: { partition: 'partition_key' },
      publications: { partition: 'partition_key', sort: 'record_key' },
    };
    const key = keys[kind];
    return new dynamodb.Table(
      this,
      `SemanticV2${kind.charAt(0).toUpperCase()}${kind.slice(1)}Table`,
      {
        tableName: semanticV2StateTableName(config.envName, kind),
        partitionKey: { name: key.partition, type: dynamodb.AttributeType.STRING },
        ...(key.sort === undefined
          ? {}
          : { sortKey: { name: key.sort, type: dynamodb.AttributeType.STRING } }),
        billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
        pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
        encryption: dynamodb.TableEncryption.AWS_MANAGED,
        ...(kind === 'attempts' || kind === 'publications'
          ? { stream: dynamodb.StreamViewType.NEW_IMAGE }
          : {}),
        removalPolicy: cdk.RemovalPolicy.RETAIN,
      },
    );
  }

  private publish(config: AppConfig, generated: GeneratedV2Storage): void {
    const attemptsStreamArn = this.attemptsTable.tableStreamArn;
    if (attemptsStreamArn === undefined) {
      throw new Error('Attempts stream must be enabled before its ARN can be published.');
    }
    const publicationsStreamArn = this.publicationsTable.tableStreamArn;
    if (publicationsStreamArn === undefined) {
      throw new Error('Publications stream must be enabled before its ARN can be published.');
    }
    const values: readonly [string, Parameters<typeof paramPath>[1], string][] = [
      ['ParamV2RawBucket', 'semanticV2RawBucketName', this.rawBucket.bucketName],
      ['ParamV2Bucket', 'semanticV2BucketName', this.bucket.bucketName],
      [
        'ParamV2AthenaResultsBucket',
        'semanticV2AthenaResultsBucketName',
        this.resultsBucket.bucketName,
      ],
      [
        'ParamV2AthenaWorkgroupInteractive',
        'semanticV2AthenaWorkgroupInteractive',
        semanticV2AthenaWorkgroupName(config.envName, 'interactive'),
      ],
      [
        'ParamV2AthenaWorkgroupBatch',
        'semanticV2AthenaWorkgroupBatch',
        semanticV2AthenaWorkgroupName(config.envName, 'batch'),
      ],
      [
        'ParamV2AthenaWorkgroupBake',
        'semanticV2AthenaWorkgroupBake',
        semanticV2AthenaWorkgroupName(config.envName, 'bake'),
      ],
      [
        'ParamV2AthenaWorkgroupInsightPrecompute',
        'semanticV2AthenaWorkgroupInsightPrecompute',
        semanticV2AthenaWorkgroupName(config.envName, 'insight-precompute'),
      ],
      [
        'ParamV2AttemptsTable',
        'semanticV2AttemptsTableName',
        this.attemptsTable.tableName,
      ],
      [
        'ParamV2AttemptsStream',
        'semanticV2AttemptsStreamArn',
        attemptsStreamArn,
      ],
      ['ParamV2LeasesTable', 'semanticV2LeasesTableName', this.leasesTable.tableName],
      [
        'ParamV2PublicationsTable',
        'semanticV2PublicationsTableName',
        this.publicationsTable.tableName,
      ],
      [
        'ParamV2PublicationsStream',
        'semanticV2PublicationsStreamArn',
        publicationsStreamArn,
      ],
      [
        'ParamV2SourceDatabase',
        'semanticV2SourceDatabase',
        semanticV2DatabaseName(config.envName, 'source-aligned'),
      ],
      [
        'ParamV2CandidateDatabase',
        'semanticV2CandidateDatabase',
        semanticV2DatabaseName(config.envName, 'candidate'),
      ],
      [
        'ParamV2CurrentDatabase',
        'semanticV2CurrentDatabase',
        semanticV2DatabaseName(config.envName, 'current'),
      ],
      ['ParamV2StagingBucket', 'semanticV2StagingBucketName', this.stagingBucket.bucketName],
      [
        'ParamV2StagingDatabase',
        'semanticV2StagingDatabase',
        semanticV2StagingDatabaseName(config.envName),
      ],
    ];
    for (const [logicalId, key, value] of values) {
      new ssm.StringParameter(this, logicalId, {
        parameterName: paramPath(config.envName, key),
        stringValue: value,
        description: `Semantic V2 generated storage (${generated.version}).`,
      });
    }
  }
}

function pascal(value: string): string {
  return value
    .split('_')
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join('');
}
