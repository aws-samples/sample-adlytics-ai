// SPDX-License-Identifier: MIT-0
/**
 * The static Athena start budget (plan S1, F2, F4): each batch caller's maximum concurrent Athena
 * executions is fixed by construction as reserved concurrency x in-process start cap, and the sum
 * must fit the contract budget, which must leave the CHAT reserve under the account quota.
 *
 * `validateAthenaStartBudgetRequest` is the one rule, pure and dependency-free; the Python twin
 * `src/domain/operations/athena_start_budget.py` evaluates the same rule over deployed values, and
 * both suites run the shared `tests/fixtures/athena-start-budget/cases.json`. The two limits live
 * only in the contract block `athena_start_budget`; `validateAthenaStartBudget(config)` refuses a
 * config that differs from it before evaluating anything, so no config can loosen the rule.
 */
import type { AppConfig } from './index';
import { ATHENA_START_BUDGET, BAKE_TASKS } from '../generated/execution-graph-protocol';

/** Baker: one reserved execution per fixed bake lane (D11), also the bake ESM `maxConcurrency`. */
export const BAKER_RESERVED_CONCURRENCY: number = BAKE_TASKS.lanes;
/** The ingestion load function (also the ProducerFinalizer and its ESM width). */
export const INGESTION_LOAD_RESERVED_CONCURRENCY = 2;
export const PROVIDER_ATTEMPT_RECONCILER_RESERVED_CONCURRENCY = 1;
export const INSIGHT_PRECOMPUTE_RESERVED_CONCURRENCY = 2;
/** Each operator row is one process at a time per environment, by the runbook rule (F5). */
export const OPERATOR_PROCESSES_PER_ROW = 1;
/** A sequential caller drives one execution to terminal before starting the next (S3 census). */
export const SEQUENTIAL_START_CAP = 1;

/** The closed row ids, in the order every report lists them. */
export const ATHENA_START_BUDGET_ROW_IDS = [
  'baker',
  'table_writer',
  'inline_ingestion',
  'provider_attempt_reconciler',
  'ingest_operator',
  'reprocess_operator',
  'insight_precompute',
] as const;
export type AthenaStartBudgetRowId = (typeof ATHENA_START_BUDGET_ROW_IDS)[number];

/** The closed refusal reasons, checked in this order; the first failure wins. */
export const ATHENA_START_BUDGET_REFUSALS = [
  'UNKNOWN_ROW',
  'MISSING_ROW',
  'DUPLICATE_ROW',
  'NON_INTEGER',
  'NEGATIVE_RESERVED_CONCURRENCY',
  'UNRESERVED',
  'NON_POSITIVE_CAP',
  'BELOW_CERTIFIED_QUERY_CAP',
  'NON_POSITIVE_CHAT_RESERVE',
  'SUM_OVER_BUDGET',
  'BUDGET_OVER_CHAT_HEADROOM',
] as const;
export type AthenaStartBudgetRefusal = (typeof ATHENA_START_BUDGET_REFUSALS)[number];

export interface AthenaStartBudgetRow {
  readonly rowId: string;
  /** `null` means unreserved (unbounded) and is refused; 0 is a quiesced row. */
  readonly reservedConcurrency: number | null;
  readonly startCap: number;
}

export interface AthenaStartBudgetRequest {
  readonly rows: readonly AthenaStartBudgetRow[];
  readonly batchAthenaBudget: number;
  readonly chatReserveActiveDml: number;
  readonly activeDmlQuota: number;
  readonly certifiedQueryCap: number;
}

export type AthenaStartBudgetVerdict =
  | { readonly verdict: 'accepted'; readonly total: number }
  | {
      readonly verdict: 'refused';
      readonly reason: AthenaStartBudgetRefusal;
      readonly observed: number | string | null;
      readonly limit: number | string | null;
    };

/** The seven S1 rows at their configured maximum (the load row is its active-state value). */
export function athenaStartBudgetRows(config: AppConfig): readonly AthenaStartBudgetRow[] {
  const sequential = (rowId: AthenaStartBudgetRowId, reservedConcurrency: number) => ({
    rowId,
    reservedConcurrency,
    startCap: SEQUENTIAL_START_CAP,
  });
  return [
    {
      rowId: 'baker',
      reservedConcurrency: BAKER_RESERVED_CONCURRENCY,
      startCap: config.athenaStartBudget.bakerStartCap,
    },
    sequential('table_writer', config.ingestion.tableWriterConcurrency),
    sequential('inline_ingestion', INGESTION_LOAD_RESERVED_CONCURRENCY),
    sequential('provider_attempt_reconciler', PROVIDER_ATTEMPT_RECONCILER_RESERVED_CONCURRENCY),
    sequential('ingest_operator', OPERATOR_PROCESSES_PER_ROW),
    sequential('reprocess_operator', OPERATOR_PROCESSES_PER_ROW),
    {
      rowId: 'insight_precompute',
      reservedConcurrency: INSIGHT_PRECOMPUTE_RESERVED_CONCURRENCY,
      startCap: config.athenaStartBudget.insightStartCap,
    },
  ];
}

/** The shared integer test: Python's `type(v) is int or integral float`; never a boolean. */
const integral = (value: unknown): boolean => typeof value === 'number' && Number.isInteger(value);

const refused = (
  reason: AthenaStartBudgetRefusal,
  observed: number | string | null,
  limit: number | string | null,
): AthenaStartBudgetVerdict => ({ verdict: 'refused', reason, observed, limit });

/** The one S1 rule over explicit values; no config, CDK or I/O dependency. */
export function validateAthenaStartBudgetRequest(
  request: AthenaStartBudgetRequest,
): AthenaStartBudgetVerdict {
  const known = new Set<string>(ATHENA_START_BUDGET_ROW_IDS);
  const unknown = request.rows.find((row) => !known.has(row.rowId));
  if (unknown !== undefined) return refused('UNKNOWN_ROW', unknown.rowId, null);
  const present = request.rows.map((row) => row.rowId);
  const missing = ATHENA_START_BUDGET_ROW_IDS.find((rowId) => !present.includes(rowId));
  if (missing !== undefined) return refused('MISSING_ROW', missing, null);
  const duplicate = present.find((rowId, index) => present.indexOf(rowId) !== index);
  if (duplicate !== undefined) return refused('DUPLICATE_ROW', duplicate, null);
  for (const row of request.rows) {
    if (row.reservedConcurrency !== null && !integral(row.reservedConcurrency)) {
      return refused('NON_INTEGER', row.rowId, null);
    }
    if (!integral(row.startCap)) return refused('NON_INTEGER', row.rowId, null);
  }
  const scalars = [
    ['batch_athena_budget', request.batchAthenaBudget],
    ['chat_reserve_active_dml', request.chatReserveActiveDml],
    ['active_dml_quota', request.activeDmlQuota],
    ['certified_query_cap', request.certifiedQueryCap],
  ] as const;
  for (const [name, value] of scalars) {
    if (!integral(value)) return refused('NON_INTEGER', name, null);
  }
  for (const row of request.rows) {
    if (row.reservedConcurrency !== null && row.reservedConcurrency < 0) {
      return refused('NEGATIVE_RESERVED_CONCURRENCY', row.reservedConcurrency, 0);
    }
  }
  const unreserved = request.rows.find((row) => row.reservedConcurrency === null);
  if (unreserved !== undefined) return refused('UNRESERVED', unreserved.rowId, null);
  const uncapped = request.rows.find((row) => row.startCap < 1);
  if (uncapped !== undefined) return refused('NON_POSITIVE_CAP', uncapped.startCap, 1);
  const baker = request.rows.find((row) => row.rowId === 'baker');
  if (baker !== undefined && baker.startCap < request.certifiedQueryCap) {
    return refused('BELOW_CERTIFIED_QUERY_CAP', baker.startCap, request.certifiedQueryCap);
  }
  if (request.chatReserveActiveDml < 1) {
    return refused('NON_POSITIVE_CHAT_RESERVE', request.chatReserveActiveDml, 1);
  }
  const total = request.rows.reduce(
    (sum, row) => sum + (row.reservedConcurrency ?? 0) * row.startCap,
    0,
  );
  if (total > request.batchAthenaBudget) {
    return refused('SUM_OVER_BUDGET', total, request.batchAthenaBudget);
  }
  const headroom = request.activeDmlQuota - request.chatReserveActiveDml;
  if (request.batchAthenaBudget > headroom) {
    return refused('BUDGET_OVER_CHAT_HEADROOM', request.batchAthenaBudget, headroom);
  }
  return { verdict: 'accepted', total };
}

/**
 * The synth-time wrapper the batch stacks call where the hard-coded guards were: refuse a config
 * limit that differs from the contract (F4), then throw on any rule refusal.
 */
export function validateAthenaStartBudget(config: AppConfig): number {
  const limits = [
    ['batchAthenaBudget', 'batch_athena_budget'],
    ['chatReserveActiveDml', 'chat_reserve_active_dml'],
  ] as const;
  for (const [field, contractName] of limits) {
    const configured = config.athenaStartBudget[field];
    const contract = ATHENA_START_BUDGET[contractName];
    if (configured !== contract) {
      throw new Error(
        `athenaStartBudget.${field} is ${configured}, but the contract ` +
          `athena_start_budget.${contractName} is ${contract}; change the contract, never the config.`,
      );
    }
  }
  const verdict = validateAthenaStartBudgetRequest({
    rows: athenaStartBudgetRows(config),
    batchAthenaBudget: config.athenaStartBudget.batchAthenaBudget,
    chatReserveActiveDml: config.athenaStartBudget.chatReserveActiveDml,
    activeDmlQuota: ATHENA_START_BUDGET.active_dml_quota,
    certifiedQueryCap: BAKE_TASKS.certified_query_cap,
  });
  if (verdict.verdict === 'refused') {
    throw new Error(
      `Athena start budget refused: ${verdict.reason} (observed ${verdict.observed}, ` +
        `limit ${verdict.limit}).`,
    );
  }
  return verdict.total;
}
