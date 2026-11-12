// SPDX-License-Identifier: MIT-0
import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';
import { AppConfig, executionGraphTableName, paramPath } from '../config';
import {
  LIVE_TABLE_SUFFIX,
  PARTITION_KEY,
  PROBE_ROLE_ACTIONS,
  PROBE_TABLE_SUFFIXES,
  SORT_KEY,
} from '../generated/execution-graph-protocol';
import { applyRemovalPolicy, applyStandardTags } from '../mixins/tagging';

export interface ControlPlaneAuthorityStoreStackProps extends cdk.StackProps {
  readonly config: AppConfig;
}

/**
 * The retained execution-graph authority store (control-plane Option B, S2 minimal v1 core).
 *
 * One live table owns run-request admission, graph identity and release permission; status stays
 * a rebuildable projection of it. The table is RETAIN, encrypted and point-in-time recoverable,
 * and is never deleted by a rollback. An absent mode item reads `LEGACY`, so the store ships in
 * the default mode with no seeding resource.
 *
 * The probe pair is built by the same construct from the same generated schema. Store-level live
 * tests write only there, through the probe role, which holds exactly the generated action set on
 * the probe tables and nothing on the live table. No worker role is granted anything on them
 * (V-15). Composed with the development-only operational plane, like the status store.
 *
 * The probe role trusts this account's root, so any principal here that may assume roles can use
 * it; that is acceptable for the development probe tables and is why it reaches no live table.
 * The probe pair and its role exist only in ENV=dev: the plane is accepted in any environment
 * (owner decision D4, 2026-11-12), and probes run only against dev.
 *
 * S2: the live table's stream (new images) carries committed graph items to the status v2
 * projector, which holds stream-read actions only. The probe tables have no stream.
 */
export class ControlPlaneAuthorityStoreStack extends cdk.Stack {
  public readonly liveTable: dynamodb.Table;
  /** The probe pair; empty outside ENV=dev. */
  public readonly probeTables: dynamodb.Table[];
  /** The probe role; absent outside ENV=dev. */
  public readonly probeRole?: iam.Role;

  constructor(scope: Construct, id: string, props: ControlPlaneAuthorityStoreStackProps) {
    super(scope, id, props);
    const { config } = props;
    if (!config.operationalStatus.provisioned) {
      throw new Error('The execution-graph store requires the operational plane.');
    }
    this.liveTable = this.graphTable('ExecutionGraph', LIVE_TABLE_SUFFIX, config, true);
    this.probeTables = [];
    if (config.envName === 'dev') {
      const probe = this.probeAccess(config);
      this.probeTables = probe.tables;
      this.probeRole = probe.role;
    }
    for (const [logicalId, key, value, description] of [
      [
        'ExecutionGraphTableNameParameter',
        'executionGraphTableName',
        this.liveTable.tableName,
        'Retained execution-graph authority table name.',
      ],
      [
        'ExecutionGraphTableArnParameter',
        'executionGraphTableArn',
        this.liveTable.tableArn,
        'Retained execution-graph authority table ARN.',
      ],
      [
        'ExecutionGraphTableStreamArnParameter',
        'executionGraphTableStreamArn',
        this.liveTable.tableStreamArn ?? '',
        'Execution-graph authority table stream ARN, read by the status v2 projector.',
      ],
    ] as const) {
      new ssm.StringParameter(this, logicalId, {
        parameterName: paramPath(config.envName, key),
        stringValue: value,
        description,
      });
    }
    applyRemovalPolicy(this, cdk.RemovalPolicy.RETAIN);
    applyStandardTags(this, config);
  }

  /** The dev probe pair and the role that alone may write it (V-15). */
  private probeAccess(config: AppConfig): { tables: dynamodb.Table[]; role: iam.Role } {
    const tables = PROBE_TABLE_SUFFIXES.map((suffix, index) =>
      this.graphTable(index === 0 ? 'ExecutionGraphProbe' : 'ExecutionGraphProbeLegacy', suffix, config),
    );
    const role = new iam.Role(this, 'ExecutionGraphProbeRole', {
      roleName: `adlytics-${config.envName}-execution-graph-probe`,
      assumedBy: new iam.AccountRootPrincipal(),
      description: 'Store-level traceability probes: the probe tables only, never the live table.',
      maxSessionDuration: cdk.Duration.hours(1),
    });
    role.addToPolicy(
      new iam.PolicyStatement({
        actions: [...PROBE_ROLE_ACTIONS],
        resources: tables.map((table) => table.tableArn),
      }),
    );
    // S5 (P5): the probe's stuck-flow and measurement controls publish `AuthorityScope=probe`
    // gauges and read them back. Neither action has resource-level scoping; the namespace
    // condition keeps the put inside `Adlytics`, and neither touches any table.
    role.addToPolicy(
      new iam.PolicyStatement({
        sid: 'ProbeScopeGauges',
        actions: ['cloudwatch:PutMetricData'],
        resources: ['*'],
        conditions: { StringEquals: { 'cloudwatch:namespace': 'Adlytics' } },
      }),
    );
    role.addToPolicy(
      new iam.PolicyStatement({
        sid: 'ProbeScopeGaugeReadback',
        actions: ['cloudwatch:GetMetricData'],
        resources: ['*'],
      }),
    );
    return { tables, role };
  }

  private graphTable(
    id: string,
    suffix: string,
    config: AppConfig,
    streamed = false,
  ): dynamodb.Table {
    return new dynamodb.Table(this, id, {
      ...(streamed ? { stream: dynamodb.StreamViewType.NEW_IMAGE } : {}),
      tableName: executionGraphTableName(config.envName, suffix),
      partitionKey: { name: PARTITION_KEY, type: dynamodb.AttributeType.STRING },
      sortKey: { name: SORT_KEY, type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      deletionProtection: true,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });
  }
}
