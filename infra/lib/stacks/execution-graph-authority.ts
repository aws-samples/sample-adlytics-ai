// SPDX-License-Identifier: MIT-0
import * as cdk from 'aws-cdk-lib';
import * as iam from 'aws-cdk-lib/aws-iam';
import { AppConfig, executionGraphTableName } from '../config';
import {
  DRAIN_SEQ_KEY,
  LIVE_TABLE_SUFFIX,
  MODE_KEY,
  OPERATOR_ONLY_DENIED_ACTIONS,
  OPERATOR_ONLY_PREFIXES,
  WRITER_ACTIONS,
} from '../generated/execution-graph-protocol';

/**
 * The retained execution-graph table, as the ingestion functions see it (S3).
 *
 * It exists only where the operational plane is provisioned, and its name is a pure function of
 * the environment (`executionGraphTableName`, the same call the store stack names it with), so it
 * is derived at synth like every other ingestion target and read from no SSM parameter.
 */
export interface ExecutionGraphAuthority {
  readonly tableName: string;
  readonly tableArn: string;
}

export function executionGraphAuthority(
  config: AppConfig,
  stack: cdk.Stack,
): ExecutionGraphAuthority | undefined {
  if (!config.operationalStatus.provisioned) {
    return undefined;
  }
  const tableName = executionGraphTableName(config.envName, LIVE_TABLE_SUFFIX);
  return {
    tableName,
    tableArn: `arn:${stack.partition}:dynamodb:${stack.region}:${stack.account}:table/${tableName}`,
  };
}

/**
 * S3 item 7 (`design.md`, "Mode-item IAM" and "Mode-read and `DRAIN_SEQ` IAM"): a strong read of
 * the mode item only, scoped by `dynamodb:LeadingKeys` to its partition key, and, for the
 * principals whose transactions condition on it (new-root intent submission, legacy finalize),
 * `dynamodb:ConditionCheckItem` on the same item. Both are read-class actions.
 */
export function grantModeItemRead(
  principal: iam.IGrantable,
  authority: ExecutionGraphAuthority,
  conditionCheck: boolean,
): void {
  principal.grantPrincipal.addToPrincipalPolicy(new iam.PolicyStatement({
    sid: conditionCheck ? 'ReadAndConditionCheckGraphModeItem' : 'ReadGraphModeItem',
    actions: conditionCheck
      ? ['dynamodb:GetItem', 'dynamodb:ConditionCheckItem']
      : ['dynamodb:GetItem'],
    resources: [authority.tableArn],
    conditions: {
      'ForAllValues:StringEquals': { 'dynamodb:LeadingKeys': [MODE_KEY.pk] },
    },
  }));
}

/**
 * S6 (V-17b, `design.md`, "Mode-read and `DRAIN_SEQ` IAM"): `dynamodb:UpdateItem` on the one
 * `DRAIN_SEQ` item in the coordinator (publications) table, scoped by `LeadingKeys` to its
 * partition key, for each legacy family-changing writer whose policy does not already cover it.
 */
export function grantDrainSeqWrite(principal: iam.IGrantable, publicationsArn: string): void {
  principal.grantPrincipal.addToPrincipalPolicy(new iam.PolicyStatement({
    sid: 'CountDrainSequence',
    actions: ['dynamodb:UpdateItem'],
    resources: [publicationsArn],
    conditions: {
      'ForAllValues:StringEquals': { 'dynamodb:LeadingKeys': [DRAIN_SEQ_KEY.partition_key] },
    },
  }));
}

/** Where an automatic requester's admission reads and writes (S6, V-19). */
export interface AdmissionRequesterTargets {
  readonly rawBucketArn: string;
  readonly publicationsArn: string;
}

/**
 * S6 (V-19, S1A-7): the workload requesters (the SQS delivery handler's `delivery_event` and the
 * registration finalizer's `schedule`) admit through `admit_run_request`, exactly as the
 * run-request API does: the writer's item actions on the live graph table with the explicit Deny
 * of the operator-only prefixes, the exact manifest version and its listings in the raw zone, and
 * the legacy authority rows 9 and 13 read in epoch 1 (`WRITE_PLAN#` owner and plan `META`).
 */
export function grantAdmissionRequester(
  principal: iam.IGrantable,
  authority: ExecutionGraphAuthority,
  targets: AdmissionRequesterTargets,
): void {
  const policy = principal.grantPrincipal;
  policy.addToPrincipalPolicy(new iam.PolicyStatement({
    sid: 'AdmitRunRequests',
    actions: [...WRITER_ACTIONS],
    resources: [authority.tableArn],
  }));
  policy.addToPrincipalPolicy(new iam.PolicyStatement({
    sid: 'DenyOperatorOnlyGraphItems',
    effect: iam.Effect.DENY,
    actions: [...OPERATOR_ONLY_DENIED_ACTIONS],
    resources: [authority.tableArn],
    conditions: {
      'ForAnyValue:StringLike': {
        'dynamodb:LeadingKeys': OPERATOR_ONLY_PREFIXES.map((prefix) => `${prefix}*`),
      },
    },
  }));
  policy.addToPrincipalPolicy(new iam.PolicyStatement({
    sid: 'ReadAdmittedManifestVersions',
    actions: ['s3:GetObjectVersion'],
    resources: [`${targets.rawBucketArn}/*`],
  }));
  policy.addToPrincipalPolicy(new iam.PolicyStatement({
    sid: 'ListAdmittedManifestRetrievals',
    actions: ['s3:ListBucket', 's3:ListBucketVersions'],
    resources: [targets.rawBucketArn],
  }));
  policy.addToPrincipalPolicy(new iam.PolicyStatement({
    sid: 'ReadLegacyPlanAuthority',
    actions: ['dynamodb:GetItem'],
    resources: [targets.publicationsArn],
    conditions: {
      'ForAllValues:StringLike': { 'dynamodb:LeadingKeys': ['WRITE_PLAN#*'] },
    },
  }));
}

/** The partition families each graph-authority worker reads and writes (least privilege). */
export const GRAPH_WORKER_FAMILIES = {
  Dispatcher: {
    reads: ['RUN#*', 'DUE#*'],
    writes: ['RUN#*', 'DUE#*', 'TOKEN#*', 'LOCK#*', 'SLOT#*'],
  },
  FenceSettler: {
    reads: ['RUN#*', 'DUE#*'],
    writes: ['RUN#*', 'DUE#*', 'TOKEN#*', 'LOCK#*'],
  },
  Promoter: {
    // S4: `BLOCK#` is read and ConditionChecked, so a waiter never becomes `A` in a blocked
    // co-release domain (S1A-8).
    reads: ['RUN#*', 'DUE#*', 'SLOT#*', 'HEAD#*', 'RECEIPT#*', 'GRAPH#*', 'BLOCK#*'],
    writes: [
      'RUN#*',
      'DUE#*',
      'TOKEN#*',
      'LOCK#*',
      'SLOT#*',
      'FINGERPRINT#*',
      'RECEIPT#*',
      'GRAPH#*',
      'BLOCK#*',
    ],
    // `EXEC_CLAIM#*` item read: the election's `read_serving_candidate` GetItems the bake and
    // precompute claims to prove a candidate holds no runtime state (GetItem only, no Query).
    itemReads: [
      'SERVING_DOMAIN#*',
      'SERVING_FRONTIER#*',
      'SERVING#*',
      'SERVING_PUBLICATION#*',
      'SERVING_TRANSITION#*',
      'EXEC_CLAIM#*',
    ],
    transactionExtras: [
      {
        sid: 'ConditionPromoterServingAuthority',
        actions: ['dynamodb:ConditionCheckItem'],
        keys: ['SERVING_DOMAIN#*', 'SERVING_FRONTIER#*', 'SERVING#*', 'SERVING_PUBLICATION#*'],
      },
      // Rolling transition: create-once loser bindings and the immutable `SERVING_TRANSITION#`
      // replay record are puts; the head and an existing binding's terminal marker are updates.
      {
        sid: 'PutPromoterServingAuthority',
        actions: ['dynamodb:PutItem'],
        keys: ['SERVING#*', 'SERVING_TRANSITION#*'],
      },
      {
        sid: 'UpdatePromoterServingAuthority',
        actions: ['dynamodb:UpdateItem'],
        keys: ['SERVING#*', 'SERVING_DOMAIN#*'],
      },
    ],
  },
  NodeVerifier: {
    reads: ['RUN#*', 'DUE#*', 'EXEC_CLAIM#*'],
    writes: ['RUN#*', 'DUE#*', 'TOKEN#*', 'LOCK#*'],
  },
  // S4: the only grant writer. Ordinal, reservations, proof, grant, lineage, head and block,
  // then apply (slot release, `PROMOTE#` wakeup, POST_GRANT nodes with token, lock and outbox),
  // and the cohort and serving binding.
  Release: {
    reads: [
      'RUN#*',
      'DUE#*',
      'SLOT#*',
      'HEAD#*',
      'BLOCK#*',
      'GRANT#*',
      'LINEAGE#*',
      'RELEASE_ORDER#*',
      'RESERVATION_SET#*',
      'TARGET#*',
      'COHORT#*',
      'SERVING#*',
    ],
    writes: [
      'RUN#*',
      'DUE#*',
      'SLOT#*',
      'HEAD#*',
      'BLOCK#*',
      'GRANT#*',
      'LINEAGE#*',
      'RELEASE_ORDER#*',
      'RESERVATION_SET#*',
      'TARGET#*',
      'TOKEN#*',
      'LOCK#*',
      'COHORT#*',
      'SERVING#*',
    ],
    itemReads: ['SERVING_DOMAIN#*', 'SERVING_FRONTIER#*'],
    transactionExtras: [
      {
        sid: 'ConditionReleaseServingAuthority',
        actions: ['dynamodb:ConditionCheckItem'],
        keys: ['SERVING_DOMAIN#*', 'SERVING_FRONTIER#*'],
      },
      {
        sid: 'PutReleaseServingAuthority',
        actions: ['dynamodb:PutItem'],
        keys: ['SERVING_DOMAIN#*', 'SERVING_FRONTIER#*'],
      },
      {
        sid: 'UpdateReleaseServingAuthority',
        actions: ['dynamodb:UpdateItem'],
        keys: ['SERVING_DOMAIN#*'],
      },
    ],
  },
  // S6 part 2, §2.11 row 2: TableWriter claims, settles and fences `write_unit` and `publish`
  // nodes (claim, settle, `ARM#`, the fence's `FENCE#`/`PROMOTE#`, `SLOT#`), and the flip's
  // cross-table `ConditionCheck` on `RUN#` and `EXEC_CLAIM#` is a transaction member too. Strong
  // reads of `RUN#` (run and nodes) and `EXEC_CLAIM#`. No `BLOCK#` or `LINEAGE#` (BF-4G dropped).
  // `DUE#*` item read: publish's `read_serving_publication_authority` GetItems the run's
  // `DUE#<n>/COMPLETE#<run_id>` obligation before the serving items (GetItem only, no Query).
  TableWriter: {
    reads: ['RUN#*', 'EXEC_CLAIM#*'],
    writes: ['EXEC_CLAIM#*', 'RUN#*', 'DUE#*', 'TOKEN#*', 'LOCK#*', 'SLOT#*'],
    itemReads: [
      'SERVING_FRONTIER#*',
      'SERVING_PUBLICATION#*',
      'DUE#*',
    ],
    // The rolling flip conditions only `COMPLETE#` (a `DUE#` family member) and its live shard
    // (an update condition); it never reads or condition-checks the domain head.
    transactionExtras: [
      {
        sid: 'PutServingGraphFamilies',
        actions: ['dynamodb:PutItem'],
        keys: ['SERVING_PUBLICATION#*'],
      },
      {
        sid: 'UpdateServingGraphFamilies',
        actions: ['dynamodb:UpdateItem'],
        keys: ['SERVING_FRONTIER#*', 'SERVING_PUBLICATION#*'],
      },
    ],
    claimUpdates: true,
  },
  // Plan 4.6 (F1, Addendum A3): the Baker only. The V3 bake fan-out, its roster tasks and the
  // finalizer: the source pin and task nodes (`RUN#`), task tokens and locks, lane outboxes,
  // `ARM#`, `BAKE_RECOVERY#` and the final `ARM#`/`COMPLETE#` deletes and `COALESCE#` update
  // (`DUE#`), the claim lease (`EXEC_CLAIM#`), the bake contribution and the precompute
  // contribution condition (`COHORT#`) and the create-once direct binding (`SERVING#`). The
  // serving extras are the strong domain read (head plus every live-frontier shard), the pin and
  // fan-out head conditions, and the final immutable-head and FINALIZED-receipt updates. No
  // generation-pointer grant: semantic visibility stays at publish.
  BakeCoordinator: {
    reads: ['EXEC_CLAIM#*', 'RUN#*', 'TOKEN#*', 'LOCK#*', 'COHORT#*', 'SERVING#*', 'DUE#*'],
    writes: ['EXEC_CLAIM#*', 'RUN#*', 'TOKEN#*', 'LOCK#*', 'COHORT#*', 'SERVING#*', 'DUE#*'],
    itemReads: [
      'SERVING_DOMAIN#*',
      'SERVING_FRONTIER#*',
      'SERVING_PUBLICATION#*',
    ],
    transactionExtras: [
      {
        sid: 'ConditionBakeServingAuthority',
        actions: ['dynamodb:ConditionCheckItem'],
        keys: ['SERVING_DOMAIN#*'],
      },
      {
        sid: 'UpdateBakeFinalizationAuthority',
        actions: ['dynamodb:UpdateItem'],
        keys: ['SERVING_DOMAIN#*', 'SERVING_PUBLICATION#*'],
      },
    ],
    claimUpdates: true,
  },
  // S6 part 2, §2.11 row 7: the precompute POST_GRANT consumer (InsightPrecompute only). Node
  // settlement only (completion is the release sweep's), the cohort contribution and the
  // serving binding. No `DUE#`, `BLOCK#` or `LINEAGE#`.
  PostGrantConsumer: {
    reads: ['EXEC_CLAIM#*', 'RUN#*', 'TOKEN#*', 'LOCK#*', 'COHORT#*', 'SERVING#*'],
    writes: ['EXEC_CLAIM#*', 'RUN#*', 'TOKEN#*', 'LOCK#*', 'COHORT#*', 'SERVING#*'],
    claimUpdates: true,
  },
} as const;

export type GraphWorkerName = keyof typeof GRAPH_WORKER_FAMILIES;

/** How one graph worker's grant differs from its family's default (§2.11 rows 2, 5-7). */
export interface GraphWorkerGrantOptions {
  /**
   * `ConditionCheckItem` on the mode item beside its read: the promoter's hold check, TableWriter's
   * V-13(a) finalize check, and the baker's legacy-pointer mode check (row 6). Default: promoter.
   */
  readonly modeConditionCheck?: boolean;
}

/**
 * A graph-authority worker's grants on the live table: strong reads (`GetItem`, `Query`) of its
 * families plus the mode item, transactional writes of its families only, and the same explicit
 * Deny of every `MODE#`/`COMMAND#` write the admission writer carries (owner-gate item 6). The
 * promoter, TableWriter and the baker also hold `ConditionCheckItem` on the mode item, a
 * read-class action. A claimer family (`claimUpdates`) also holds the one direct `UpdateItem` on
 * `EXEC_CLAIM#`, outside any transaction: the lease release (§2.3) and bake's per-intent progress
 * record (§2.8 step 6).
 */
export function grantGraphWorker(
  principal: iam.IGrantable,
  authority: ExecutionGraphAuthority,
  worker: GraphWorkerName,
  options: GraphWorkerGrantOptions = {},
): void {
  const families = GRAPH_WORKER_FAMILIES[worker];
  // The promoter ConditionChecks the hold revision it read when it promotes a held waiter, so a
  // hold set again after its read refuses the commit (V-20, S3V-9).
  grantModeItemRead(principal, authority, options.modeConditionCheck ?? worker === 'Promoter');
  principal.grantPrincipal.addToPrincipalPolicy(new iam.PolicyStatement({
    sid: 'ReadGraphFamilies',
    actions: ['dynamodb:GetItem', 'dynamodb:Query'],
    resources: [authority.tableArn],
    conditions: { 'ForAllValues:StringLike': { 'dynamodb:LeadingKeys': [...families.reads] } },
  }));
  if ('itemReads' in families) {
    principal.grantPrincipal.addToPrincipalPolicy(new iam.PolicyStatement({
      sid: 'ReadServingGraphFamilies',
      actions: ['dynamodb:GetItem'],
      resources: [authority.tableArn],
      conditions: {
        'ForAllValues:StringLike': { 'dynamodb:LeadingKeys': [...families.itemReads] },
      },
    }));
  }
  principal.grantPrincipal.addToPrincipalPolicy(new iam.PolicyStatement({
    sid: 'TransactGraphFamilies',
    actions: [
      'dynamodb:PutItem',
      'dynamodb:UpdateItem',
      'dynamodb:DeleteItem',
      'dynamodb:ConditionCheckItem',
    ],
    resources: [authority.tableArn],
    conditions: {
      'ForAllValues:StringLike': { 'dynamodb:LeadingKeys': [...families.writes] },
      StringEquals: { 'dynamodb:EnclosingOperation': 'TransactWriteItems' },
    },
  }));
  if ('transactionExtras' in families) {
    for (const extra of families.transactionExtras) {
      principal.grantPrincipal.addToPrincipalPolicy(new iam.PolicyStatement({
        sid: extra.sid,
        actions: [...extra.actions],
        resources: [authority.tableArn],
        conditions: {
          'ForAllValues:StringLike': { 'dynamodb:LeadingKeys': [...extra.keys] },
          StringEquals: { 'dynamodb:EnclosingOperation': 'TransactWriteItems' },
        },
      }));
    }
  }
  if ('claimUpdates' in families && families.claimUpdates) {
    principal.grantPrincipal.addToPrincipalPolicy(new iam.PolicyStatement({
      sid: 'UpdateGraphClaims',
      actions: ['dynamodb:UpdateItem'],
      resources: [authority.tableArn],
      conditions: { 'ForAllValues:StringLike': { 'dynamodb:LeadingKeys': ['EXEC_CLAIM#*'] } },
    }));
  }
  principal.grantPrincipal.addToPrincipalPolicy(new iam.PolicyStatement({
    sid: 'DenyOperatorOnlyGraphWrites',
    effect: iam.Effect.DENY,
    actions: [...OPERATOR_ONLY_DENIED_ACTIONS],
    resources: [authority.tableArn],
    conditions: {
      'ForAnyValue:StringLike': {
        'dynamodb:LeadingKeys': OPERATOR_ONLY_PREFIXES.map((prefix) => `${prefix}*`),
      },
    },
  }));
}
