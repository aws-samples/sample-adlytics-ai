// SPDX-License-Identifier: MIT-0
/**
 * S6 part 2 (G2-12, design §4 "Call inventory"): every new AWS call the GRAPH chain's code makes
 * from slices 2 to 6, per Lambda, and the §2.11 row that grants it.
 *
 * `graph-call-inventory.test.ts` synthesizes the stacks with the graph authority provisioned and
 * checks each entry against the function role's policy statements: an entry naming a §2.11 row
 * is allowed by exactly one row statement, the one `ROW_STATEMENTS` maps to that row, and an
 * entry marked `existing` is allowed by a grant that predates S6 part 2 and by no row statement.
 * Nothing on the role may deny it. Every row statement must be used by at least one entry, so a
 * row grants every call the chosen paths make and nothing more (§2.11).
 *
 * Keys are prefixes (`RUN#`, `EXEC_CLAIM#`, a dataset key's `<dataset>|`), checked against the
 * statement's `dynamodb:LeadingKeys` with a sample key; the mode item is its exact key. An entry
 * with `enclosing` is a member of that transaction (TransactWriteItems authorizes each member's
 * own action, with `dynamodb:EnclosingOperation` set); one without is a direct call.
 */
import { MODE_KEY } from '../../lib/generated/execution-graph-protocol';
import { CANONICAL_DATASET_PREFIXES } from '../../lib/stacks/ingestion-stage1';

export type GraphCallStack =
  | 'IngestionStack'
  | 'PublisherStack'
  | 'InsightPrecomputeStack'
  | 'ApiStack'
  | 'OperationalStatusProcessorStack';

/** A §2.11 row, or a grant that predates S6 part 2 (rows 4 and 11 add nothing by design). */
export type GraphRow = '1' | '2' | '2b' | '3' | '5' | '6' | '7' | '8' | 'existing';

export type GraphCallTarget =
  | { readonly table: 'graph' | 'publications' | 'status' }
  | { readonly queue: string };

export interface GraphCall {
  readonly stack: GraphCallStack;
  /** The function's physical name after `adlytics-<env>-`. */
  readonly lambda: string;
  readonly action: string;
  readonly target: GraphCallTarget;
  /** The key prefix (or the mode item's exact key) the call addresses; absent for SQS. */
  readonly key?: string;
  readonly enclosing?: 'TransactWriteItems' | 'TransactGetItems';
  readonly row: GraphRow;
  /** The slice whose code makes the call, and the code path. */
  readonly slice: '2' | '3' | '4a' | '4b' | '5i' | '5ii' | '6' | '7' | 'bake-v3';
  readonly operation: string;
}

const MODE = MODE_KEY.pk;
const GRAPH = { table: 'graph' } as const;
const PUBLICATIONS = { table: 'publications' } as const;
const STATUS = { table: 'status' } as const;
const TWI = 'TransactWriteItems' as const;
const TGI = 'TransactGetItems' as const;
/** One canonical dataset key prefix stands for them all (`datasetKeys()`, row 2b). */
const DATASET_KEY = `${CANONICAL_DATASET_PREFIXES[0]}|`;

/** The §2.11 row each new statement grants, by function and statement Sid. */
export const ROW_STATEMENTS: Readonly<Record<string, Readonly<Record<string, GraphRow>>>> = {
  ExecutionGraphDispatcher: { SendGraphRouteEnvelopes: '1' },
  ExecutionGraphPromoter: {
    ReadServingGraphFamilies: '2',
    ConditionPromoterServingAuthority: '2',
    PutPromoterServingAuthority: '2',
    UpdatePromoterServingAuthority: '2',
  },
  ExecutionGraphRelease: {
    ReadServingGraphFamilies: '2',
    ConditionReleaseServingAuthority: '2',
    PutReleaseServingAuthority: '2',
    UpdateReleaseServingAuthority: '2',
  },
  TableWriter: {
    ReadGraphFamilies: '2',
    ReadServingGraphFamilies: '2',
    TransactGraphFamilies: '2',
    PutServingGraphFamilies: '2',
    UpdateServingGraphFamilies: '2',
    UpdateGraphClaims: '2',
    StageAndFoldGrantPublications: '2b',
    FlipGenerationPointers: '2b',
    ReadGenerationPointers: '2b',
  },
  IngestionFunction: { QueryGraphRunNodes: '3' },
  PresetArtifactEnqueue: { ReadGraphModeItem: '6' },
  PresetArtifactBackfill: { ReadGraphModeItem: '6' },
  // Plan 4.6 F1 (A3): the Baker-only BakeCoordinator family; no generation grant.
  PresetArtifactBaker: {
    ReadAndConditionCheckGraphModeItem: '6',
    ReadGraphFamilies: '5',
    ReadServingGraphFamilies: '5',
    TransactGraphFamilies: '5',
    ConditionBakeServingAuthority: '5',
    UpdateBakeFinalizationAuthority: '5',
    UpdateGraphClaims: '5',
  },
  InsightPrecompute: {
    ReadGraphModeItem: '6',
    ReadGraphFamilies: '7',
    TransactGraphFamilies: '7',
    UpdateGraphClaims: '7',
  },
  PresentationFunction: { ReadGraphServingBindings: '8' },
};

function writes(
  base: Omit<GraphCall, 'action' | 'key' | 'enclosing' | 'operation'>,
  operation: string,
  members: readonly (readonly [string, string])[],
): GraphCall[] {
  return members.map(([action, key]) => ({ ...base, action, key, enclosing: TWI, operation }));
}

const DISPATCHER = { stack: 'IngestionStack', lambda: 'ExecutionGraphDispatcher' } as const;
const PROMOTER = { stack: 'IngestionStack', lambda: 'ExecutionGraphPromoter' } as const;
const FENCE_SETTLER = { stack: 'IngestionStack', lambda: 'ExecutionGraphFenceSettler' } as const;
const NODE_VERIFIER = { stack: 'IngestionStack', lambda: 'ExecutionGraphNodeVerifier' } as const;
const RELEASE = { stack: 'IngestionStack', lambda: 'ExecutionGraphRelease' } as const;
const DELIVERY = { stack: 'IngestionStack', lambda: 'IngestionFunction' } as const;
const TABLE_WRITER = { stack: 'IngestionStack', lambda: 'TableWriter' } as const;
const BAKER = { stack: 'PublisherStack', lambda: 'PresetArtifactBaker' } as const;
const PRECOMPUTE = { stack: 'InsightPrecomputeStack', lambda: 'InsightPrecompute' } as const;
const PRESENTATION = { stack: 'ApiStack', lambda: 'PresentationFunction' } as const;

/** The POST_GRANT consumer's calls (§2.8), the same for the baker and precompute. */
function consumerCalls(
  base: { readonly stack: GraphCallStack; readonly lambda: string },
  row: GraphRow,
): GraphCall[] {
  const on = { ...base, target: GRAPH, row, slice: '5i' as const };
  return [
    { ...on, action: 'dynamodb:GetItem', key: 'RUN#', operation: 'consume_post_grant read_run' },
    { ...on, action: 'dynamodb:GetItem', key: 'EXEC_CLAIM#', operation: 'read_claim' },
    { ...on, action: 'dynamodb:GetItem', key: 'SERVING#', operation: 'read_serving_binding' },
    { ...on, action: 'dynamodb:Query', key: 'RUN#', operation: 'read_nodes' },
    { ...on, action: 'dynamodb:Query', key: 'COHORT#', operation: 'read_contributions' },
    ...writes(on, 'claim (CLAIM or RECLAIM)', [
      ['dynamodb:PutItem', 'EXEC_CLAIM#'],
      ['dynamodb:UpdateItem', 'EXEC_CLAIM#'],
      ['dynamodb:UpdateItem', 'RUN#'],
      ['dynamodb:ConditionCheckItem', 'RUN#'],
    ]),
    ...writes(on, 'settle_node (node, token and lock release, lease bump)', [
      ['dynamodb:DeleteItem', 'TOKEN#'],
      ['dynamodb:DeleteItem', 'LOCK#'],
    ]),
    ...writes(on, 'record_contribution and bind_serving', [
      ['dynamodb:PutItem', 'COHORT#'],
      ['dynamodb:PutItem', 'SERVING#'],
    ]),
    { ...on, action: 'dynamodb:UpdateItem', key: 'EXEC_CLAIM#', operation: 'release_claim_lease' },
  ];
}

/** The V3 bake's own calls (plan 4.6 under A3): fan-out, roster tasks and the finalizer. */
function bakeV3Calls(): GraphCall[] {
  const on = { ...BAKER, target: GRAPH, row: '5' as const, slice: 'bake-v3' as const };
  return [
    { ...on, action: 'dynamodb:UpdateItem', key: 'EXEC_CLAIM#', operation: 'mark_bake_selection' },
    { ...on, action: 'dynamodb:GetItem', key: 'DUE#', operation: 'finalizer COALESCE# cohort read' },
    ...(['SERVING_DOMAIN#', 'SERVING_FRONTIER#'] as const).map((key) => ({
      ...on,
      action: 'dynamodb:GetItem',
      key,
      operation: 'read_serving_domain (live head and every frontier shard, strong)',
    })),
    {
      ...on,
      action: 'dynamodb:GetItem',
      key: 'SERVING_PUBLICATION#',
      operation: 'final image marker read',
    },
    ...writes(on, 'pin_durable_bake_sources (BAKE_SOURCE#, BAKE_PIN)', [
      ['dynamodb:PutItem', 'RUN#'],
      ['dynamodb:ConditionCheckItem', 'EXEC_CLAIM#'],
      ['dynamodb:ConditionCheckItem', 'SERVING_DOMAIN#'],
    ]),
    ...writes(on, 'fan_out_bake_tasks (task nodes, tokens, locks, lane outboxes, ARM#, winner head)', [
      ['dynamodb:PutItem', 'TOKEN#'],
      ['dynamodb:PutItem', 'LOCK#'],
      ['dynamodb:PutItem', 'DUE#'],
      ['dynamodb:ConditionCheckItem', 'SERVING_DOMAIN#'],
    ]),
    ...writes(on, 'finalize_bake (immutable-cohort final transaction, no GENERATION# member)', [
      ['dynamodb:UpdateItem', 'SERVING_DOMAIN#'],
      ['dynamodb:UpdateItem', 'SERVING_PUBLICATION#'],
      ['dynamodb:PutItem', 'SERVING#'],
      ['dynamodb:ConditionCheckItem', 'COHORT#'],
      ['dynamodb:UpdateItem', 'DUE#'],
      ['dynamodb:DeleteItem', 'DUE#'],
    ]),
  ];
}

export const GRAPH_CALLS: readonly GraphCall[] = [
  // --- Dispatcher: routing from the contract `routes` (slice 2) ---------------------------------
  ...['ingestion', 'preset-bake.fifo', 'insight-precompute.fifo'].map((suffix) => ({
    ...DISPATCHER,
    action: 'sqs:SendMessage',
    target: { queue: `adlytics-dev-${suffix}` },
    row: '1' as const,
    slice: '2' as const,
    operation: `RoutedGraphSender (${suffix})`,
  })),
  {
    ...DISPATCHER,
    action: 'sqs:SendMessage',
    target: { queue: 'adlytics-dev-semantic-write.fifo' },
    row: 'existing',
    slice: '2',
    operation: 'RoutedGraphSender (write_unit and publish)',
  },
  ...writes({ ...DISPATCHER, target: GRAPH, row: 'existing', slice: '2' }, 'POST_GRANT permit', [
    ['dynamodb:ConditionCheckItem', 'RUN#'],
    ['dynamodb:UpdateItem', 'RUN#'],
    ['dynamodb:UpdateItem', 'DUE#'],
  ]),
  // --- Promoter: the arm pass (slice 2; row 4, no change) ---------------------------------------
  {
    ...PROMOTER,
    action: 'dynamodb:Query',
    target: GRAPH,
    key: 'DUE#',
    row: 'existing',
    slice: '2',
    operation: 'arm pass ARM#/COMPLETE# reads',
  },
  ...(['dynamodb:GetItem', 'dynamodb:Query'] as const).map((action) => ({
    ...PROMOTER,
    action,
    target: GRAPH,
    key: 'RUN#',
    row: 'existing' as const,
    slice: '2' as const,
    operation: 'arm pass read_run, read_nodes',
  })),
  ...writes({ ...PROMOTER, target: GRAPH, row: 'existing', slice: '2' }, 'arming_members', [
    ['dynamodb:UpdateItem', 'RUN#'],
    ['dynamodb:PutItem', 'TOKEN#'],
    ['dynamodb:PutItem', 'LOCK#'],
    ['dynamodb:PutItem', 'DUE#'],
    ['dynamodb:DeleteItem', 'DUE#'],
  ]),
  // --- Promoter: the rolling election, alias drains and fixed-member final transition ----------
  ...(
    [
      ['SERVING_DOMAIN#', 'read_serving_domain (fresh iteration head, strong)'],
      ['SERVING_FRONTIER#', 'read_serving_domain (every live-frontier shard, strong)'],
      ['SERVING_PUBLICATION#', 'read_serving_candidate marker, successor marker coordinates'],
      ['SERVING_TRANSITION#', 'read_serving_promoter_transition (lost-response replay)'],
      ['SERVING#', 'read_serving_binding (direct and alias bindings)'],
      ['EXEC_CLAIM#', 'read_serving_candidate read_claim (no runtime state on bake/precompute)'],
    ] as const
  ).map(([key, operation]) => ({
    ...PROMOTER,
    action: 'dynamodb:GetItem',
    target: GRAPH,
    key,
    row: '2' as const,
    slice: '2' as const,
    operation,
  })),
  {
    ...PROMOTER,
    action: 'dynamodb:GetItem',
    target: GRAPH,
    key: 'DUE#',
    row: 'existing',
    slice: '2',
    operation: 'read_serving_candidate ARM#/COMPLETE# reads',
  },
  ...writes(
    { ...PROMOTER, target: GRAPH, row: '2', slice: '2' },
    'activate_serving_winner, drain_alias_binding, drain_final_alias_and_activate_next',
    [
      ['dynamodb:ConditionCheckItem', 'SERVING_FRONTIER#'],
      ['dynamodb:ConditionCheckItem', 'SERVING_PUBLICATION#'],
      ['dynamodb:ConditionCheckItem', 'SERVING_DOMAIN#'],
      ['dynamodb:ConditionCheckItem', 'SERVING#'],
      ['dynamodb:PutItem', 'SERVING#'],
      ['dynamodb:PutItem', 'SERVING_TRANSITION#'],
      ['dynamodb:UpdateItem', 'SERVING#'],
      ['dynamodb:UpdateItem', 'SERVING_DOMAIN#'],
    ],
  ),
  ...writes(
    { ...PROMOTER, target: GRAPH, row: 'existing', slice: '2' },
    'winner bake arming, loser settlement, COALESCE#, ARM#/COMPLETE# and obligation deletes',
    [
      ['dynamodb:ConditionCheckItem', 'RUN#'],
      ['dynamodb:ConditionCheckItem', 'DUE#'],
      ['dynamodb:UpdateItem', 'RUN#'],
      ['dynamodb:PutItem', 'TOKEN#'],
      ['dynamodb:PutItem', 'LOCK#'],
      ['dynamodb:PutItem', 'DUE#'],
      ['dynamodb:UpdateItem', 'DUE#'],
      ['dynamodb:DeleteItem', 'DUE#'],
    ],
  ),
  // --- Release: COMPLETE# at apply, domain bootstrap, and the direct drain at completion ------
  ...(['SERVING_DOMAIN#', 'SERVING_FRONTIER#'] as const).map((key) => ({
    ...RELEASE,
    action: 'dynamodb:GetItem',
    target: GRAPH,
    key,
    row: '2' as const,
    slice: '2' as const,
    operation: 'read_serving_domain (head and every live-frontier shard, strong)',
  })),
  ...(['SERVING_DOMAIN#', 'SERVING_FRONTIER#'] as const).map((key) => ({
    ...RELEASE,
    action: 'dynamodb:ConditionCheckItem',
    target: GRAPH,
    key,
    enclosing: TWI,
    row: '2' as const,
    slice: '2' as const,
    operation: 'bootstrap_serving_domain_and_apply_grant existing-domain conditions',
  })),
  ...(['SERVING_DOMAIN#', 'SERVING_FRONTIER#'] as const).map((key) => ({
    ...RELEASE,
    action: 'dynamodb:PutItem',
    target: GRAPH,
    key,
    enclosing: TWI,
    row: '2' as const,
    slice: '2' as const,
    operation: 'bootstrap_serving_domain_and_apply_grant first-use head and 32 shards',
  })),
  {
    ...RELEASE,
    action: 'dynamodb:UpdateItem',
    target: GRAPH,
    key: 'SERVING_DOMAIN#',
    enclosing: TWI,
    row: '2',
    slice: '2',
    operation: 'complete_granted_run direct drain of the immutable head',
  },
  ...writes({ ...RELEASE, target: GRAPH, row: 'existing', slice: '5i' }, 'complete_granted_run', [
    ['dynamodb:UpdateItem', 'SERVING#'],
    ['dynamodb:UpdateItem', 'RUN#'],
    ['dynamodb:ConditionCheckItem', 'RUN#'],
    ['dynamodb:UpdateItem', 'DUE#'],
    ['dynamodb:DeleteItem', 'DUE#'],
  ]),
  ...writes({ ...RELEASE, target: GRAPH, row: 'existing', slice: '2' }, 'apply COMPLETE#', [
    ['dynamodb:PutItem', 'DUE#'],
  ]),
  {
    ...RELEASE,
    action: 'dynamodb:GetItem',
    target: GRAPH,
    key: 'SERVING#',
    row: 'existing',
    slice: '5i',
    operation: 'read_serving_binding',
  },
  // --- Delivery handler: materialize_raw (slice 3) ----------------------------------------------
  ...(['RUN#', 'EXEC_CLAIM#', 'RECEIPT#'] as const).map((key) => ({
    ...DELIVERY,
    action: 'dynamodb:GetItem',
    target: GRAPH,
    key,
    row: 'existing' as const,
    slice: '3' as const,
    operation: 'strong reads (AGG3-8)',
  })),
  {
    ...DELIVERY,
    action: 'dynamodb:Query',
    target: GRAPH,
    key: 'RUN#',
    row: '3',
    slice: '3',
    operation: 'claim read_nodes',
  },
  ...writes(
    { ...DELIVERY, target: GRAPH, row: 'existing', slice: '3' },
    'claim, reattest_materialization, settle_materialization (+ successors or fence)',
    [
      ['dynamodb:PutItem', 'EXEC_CLAIM#'],
      ['dynamodb:UpdateItem', 'EXEC_CLAIM#'],
      ['dynamodb:UpdateItem', 'RUN#'],
      ['dynamodb:PutItem', 'RUN#'],
      ['dynamodb:DeleteItem', 'TOKEN#'],
      ['dynamodb:DeleteItem', 'LOCK#'],
      ['dynamodb:PutItem', 'DUE#'],
      ['dynamodb:UpdateItem', 'SLOT#'],
    ],
  ),
  {
    ...DELIVERY,
    action: 'dynamodb:UpdateItem',
    target: GRAPH,
    key: 'EXEC_CLAIM#',
    row: 'existing',
    slice: '3',
    operation: 'release_claim_lease',
  },
  {
    ...DELIVERY,
    action: 'sqs:ChangeMessageVisibility',
    target: { queue: 'adlytics-dev-ingestion' },
    row: 'existing',
    slice: '3',
    operation: 'hold an unacknowledged delivery',
  },
  // --- TableWriter: graph units and publish (slice 4b) ------------------------------------------
  ...(['RUN#', 'EXEC_CLAIM#'] as const).map((key) => ({
    ...TABLE_WRITER,
    action: 'dynamodb:GetItem',
    target: GRAPH,
    key,
    row: '2' as const,
    slice: '4b' as const,
    operation: 'read_run, read_claim_item',
  })),
  {
    ...TABLE_WRITER,
    action: 'dynamodb:Query',
    target: GRAPH,
    key: 'RUN#',
    row: '2',
    slice: '4b',
    operation: 'read_nodes (publish validation, claim)',
  },
  ...(['SERVING_FRONTIER#', 'SERVING_PUBLICATION#'] as const)
    .map((key) => ({
      ...TABLE_WRITER,
      action: 'dynamodb:GetItem',
      target: GRAPH,
      key,
      row: '2' as const,
      slice: '4b' as const,
      operation: 'live-shard publication authority, marker replay and frontier race reads',
    })),
  {
    ...TABLE_WRITER,
    action: 'dynamodb:GetItem',
    target: GRAPH,
    key: 'DUE#',
    row: '2',
    slice: '4b',
    operation: 'read_serving_publication_authority COMPLETE# read',
  },
  {
    ...TABLE_WRITER,
    action: 'dynamodb:ConditionCheckItem',
    target: GRAPH,
    key: 'DUE#',
    enclosing: TWI,
    row: '2',
    slice: '4b',
    operation: 'cross-table publication flip COMPLETE# identity condition',
  },
  {
    ...TABLE_WRITER,
    action: 'dynamodb:PutItem',
    target: GRAPH,
    key: 'SERVING_PUBLICATION#',
    enclosing: TWI,
    row: '2',
    slice: '4b',
    operation: 'cross-table publication VISIBLE_UNSETTLED marker put',
  },
  ...(['SERVING_FRONTIER#', 'SERVING_PUBLICATION#'] as const).map((key) => ({
    ...TABLE_WRITER,
    action: 'dynamodb:UpdateItem',
    target: GRAPH,
    key,
    enclosing: TWI,
    row: '2' as const,
    slice: '4b' as const,
    operation: 'live-shard visible/eligible counts and settled marker coordinate',
  })),
  ...writes(
    { ...TABLE_WRITER, target: GRAPH, row: '2', slice: '4b' },
    'claim, settle_node, settle_materialization (+ fence), settle_publish (ARM# serving)',
    [
      ['dynamodb:PutItem', 'EXEC_CLAIM#'],
      ['dynamodb:UpdateItem', 'EXEC_CLAIM#'],
      ['dynamodb:UpdateItem', 'RUN#'],
      ['dynamodb:ConditionCheckItem', 'RUN#'],
      ['dynamodb:DeleteItem', 'TOKEN#'],
      ['dynamodb:DeleteItem', 'LOCK#'],
      ['dynamodb:PutItem', 'DUE#'],
      ['dynamodb:UpdateItem', 'DUE#'],
      ['dynamodb:UpdateItem', 'SLOT#'],
    ],
  ),
  ...writes(
    { ...TABLE_WRITER, target: GRAPH, row: '2', slice: '4b' },
    'the flip and BeginTableAttempt cross-table fence (§2.5, §2.6)',
    [
      ['dynamodb:ConditionCheckItem', 'EXEC_CLAIM#'],
    ],
  ),
  {
    ...TABLE_WRITER,
    action: 'dynamodb:UpdateItem',
    target: GRAPH,
    key: 'EXEC_CLAIM#',
    row: '2',
    slice: '4b',
    operation: 'release_claim_lease (with publish progress)',
  },
  {
    ...TABLE_WRITER,
    action: 'dynamodb:GetItem',
    target: GRAPH,
    key: MODE,
    row: 'existing',
    slice: '4b',
    operation: 'per-batch mode read',
  },
  {
    ...TABLE_WRITER,
    action: 'dynamodb:UpdateItem',
    target: PUBLICATIONS,
    key: DATASET_KEY,
    row: '2b',
    slice: '4b',
    operation: 'stage_for_grant, fold_for_grant',
  },
  {
    ...TABLE_WRITER,
    action: 'dynamodb:UpdateItem',
    target: PUBLICATIONS,
    key: 'GENERATION#',
    enclosing: TWI,
    row: '2b',
    slice: '4b',
    operation: 'flip_generation',
  },
  {
    ...TABLE_WRITER,
    action: 'dynamodb:GetItem',
    target: PUBLICATIONS,
    key: 'GENERATION#',
    row: '2b',
    slice: '4b',
    operation: 'read_generation_pointer',
  },
  {
    ...TABLE_WRITER,
    action: 'dynamodb:GetItem',
    target: PUBLICATIONS,
    key: 'GENERATION#',
    enclosing: TGI,
    row: '2b',
    slice: '4a',
    operation: 'servable_for_scope vector read',
  },
  {
    ...TABLE_WRITER,
    action: 'dynamodb:ConditionCheckItem',
    target: PUBLICATIONS,
    key: DATASET_KEY,
    enclosing: TWI,
    row: 'existing',
    slice: '4b',
    operation: 'flip CURRENT checks',
  },
  {
    ...TABLE_WRITER,
    action: 'dynamodb:GetItem',
    target: PUBLICATIONS,
    key: DATASET_KEY,
    enclosing: TGI,
    row: 'existing',
    slice: '4b',
    operation: 'raw_current classification',
  },
  // --- Publisher: the stream gates and the baker (slices 5i, 5ii) -------------------------------
  ...(['PresetArtifactEnqueue', 'PresetArtifactBackfill', 'PresetArtifactBaker'] as const).map(
    (lambda) => ({
      stack: 'PublisherStack' as const,
      lambda,
      action: 'dynamodb:GetItem',
      target: GRAPH,
      key: MODE,
      row: '6' as const,
      slice: '5i' as const,
      operation: 'cutover gate mode read',
    }),
  ),
  {
    ...BAKER,
    action: 'dynamodb:ConditionCheckItem',
    target: GRAPH,
    key: MODE,
    enclosing: TWI,
    row: '6',
    slice: '5i',
    operation: 'legacy pointer mode check (AGG3-6)',
  },
  ...consumerCalls(BAKER, '5'),
  ...bakeV3Calls(),
  {
    ...BAKER,
    action: 'dynamodb:GetItem',
    target: PUBLICATIONS,
    key: 'GENERATION#',
    row: 'existing',
    slice: '4a',
    operation: 'generation pointer reader',
  },
  {
    ...BAKER,
    action: 'sqs:ReceiveMessage',
    target: { queue: 'adlytics-dev-preset-bake.fifo' },
    row: 'existing',
    slice: '5i',
    operation: 'graph bake envelopes',
  },
  // --- InsightPrecompute (slice 5i) -------------------------------------------------------------
  {
    ...PRECOMPUTE,
    action: 'dynamodb:GetItem',
    target: GRAPH,
    key: MODE,
    row: '6',
    slice: '5i',
    operation: 'cutover gate mode read',
  },
  ...consumerCalls(PRECOMPUTE, '7'),
  {
    ...PRECOMPUTE,
    action: 'dynamodb:GetItem',
    target: PUBLICATIONS,
    key: 'GENERATION#',
    row: 'existing',
    slice: '4a',
    operation: 'generation pointer reader',
  },
  {
    ...PRECOMPUTE,
    action: 'sqs:ReceiveMessage',
    target: { queue: 'adlytics-dev-insight-precompute.fifo' },
    row: 'existing',
    slice: '5i',
    operation: 'graph precompute envelopes (SqsEventSource)',
  },
  // --- Presentation (slice 5ii) -----------------------------------------------------------------
  ...([MODE, 'SERVING#'] as const).map((key) => ({
    ...PRESENTATION,
    action: 'dynamodb:GetItem',
    target: GRAPH,
    key,
    row: '8' as const,
    slice: '5ii' as const,
    operation: 'DynamoPresentationGraphReader',
  })),
  {
    ...PRESENTATION,
    action: 'dynamodb:GetItem',
    target: PUBLICATIONS,
    key: 'GENERATION#',
    row: 'existing',
    slice: '4a',
    operation: 'current_for_scope generation pointer',
  },
  // --- Status projection and explanation (slice 6) ----------------------------------------------
  ...(['XROOT#', 'MEASUREMENT#', 'METRIC_ACCEPT#', 'METRIC_DUE#'] as const).map((key) => ({
    stack: 'OperationalStatusProcessorStack' as const,
    lambda: 'OperationalStatusGraphProjector',
    action: 'dynamodb:PutItem',
    target: STATUS,
    key,
    enclosing: TWI,
    row: 'existing' as const,
    slice: '6' as const,
    operation: 'NODE/CLAIM row with its measurement contribution',
  })),
  {
    stack: 'ApiStack',
    lambda: 'OperationalStatusFunction',
    action: 'dynamodb:Query',
    target: STATUS,
    key: 'XROOT#',
    row: 'existing',
    slice: '6',
    operation: 'DynamoGraphProjectionStore.executions',
  },
  // --- Slice 7 (1b): per-table unit batches; every member is in an existing family (§2.11) ------
  ...writes({ ...PROMOTER, target: GRAPH, row: 'existing', slice: '7' }, 'arm_unit_batch', [
    ['dynamodb:UpdateItem', 'RUN#'],
    ['dynamodb:PutItem', 'TOKEN#'],
    ['dynamodb:PutItem', 'LOCK#'],
    ['dynamodb:PutItem', 'DUE#'],
    ['dynamodb:DeleteItem', 'DUE#'],
  ]),
  ...writes(
    { ...DISPATCHER, target: GRAPH, row: 'existing', slice: '7' },
    'acquire_batch_permit, record_batch_send, delete_batch_row',
    [
      ['dynamodb:UpdateItem', 'DUE#'],
      ['dynamodb:DeleteItem', 'DUE#'],
      ['dynamodb:UpdateItem', 'RUN#'],
      ['dynamodb:ConditionCheckItem', 'RUN#'],
    ],
  ),
  ...writes({ ...DISPATCHER, target: GRAPH, row: 'existing', slice: '7' }, 'return_unit (dispatcher)', [
    ['dynamodb:UpdateItem', 'RUN#'],
    ['dynamodb:DeleteItem', 'TOKEN#'],
    ['dynamodb:UpdateItem', 'LOCK#'],
    ['dynamodb:UpdateItem', 'DUE#'],
    ['dynamodb:ConditionCheckItem', 'RUN#'],
  ]),
  ...writes(
    { ...TABLE_WRITER, target: GRAPH, row: '2', slice: '7' },
    'return_unit, settle_unit_set, counted settle_failed, claim batch_id',
    [
      ['dynamodb:UpdateItem', 'RUN#'],
      ['dynamodb:DeleteItem', 'TOKEN#'],
      ['dynamodb:UpdateItem', 'LOCK#'],
      ['dynamodb:UpdateItem', 'EXEC_CLAIM#'],
      ['dynamodb:UpdateItem', 'DUE#'],
      ['dynamodb:ConditionCheckItem', 'RUN#'],
    ],
  ),
  // A batch member's `fence_unsent`: the node CAS, the token delete, the counted `LOCK#` share
  // release (`ADD open_members`, not the holder delete) and the FENCING run check; no outbox row.
  ...writes(
    { ...FENCE_SETTLER, target: GRAPH, row: 'existing', slice: '7' },
    'fence_unsent (counted batch-member release)',
    [
      ['dynamodb:UpdateItem', 'RUN#'],
      ['dynamodb:DeleteItem', 'TOKEN#'],
      ['dynamodb:UpdateItem', 'LOCK#'],
      ['dynamodb:ConditionCheckItem', 'RUN#'],
    ],
  ),
  // A verified unit: `settle_unit_set` (a success set of one with the `VERIFY#` delete, no
  // `EXEC_CLAIM#` write) or `settle_node` FAILED, each releasing its counted `LOCK#` share.
  ...writes(
    { ...NODE_VERIFIER, target: GRAPH, row: 'existing', slice: '7' },
    'settle_unit_set (verify_open), counted settle_node FAILED',
    [
      ['dynamodb:UpdateItem', 'RUN#'],
      ['dynamodb:DeleteItem', 'TOKEN#'],
      ['dynamodb:UpdateItem', 'LOCK#'],
      ['dynamodb:DeleteItem', 'DUE#'],
    ],
  ),
];
