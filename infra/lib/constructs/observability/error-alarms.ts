// SPDX-License-Identifier: MIT-0
/**
 * The shared ADLY `Errors` metric as alarm queries (ADR-009, ADR-133 clause 4).
 *
 * `report_error` emits `Errors` in the `Adlytics` namespace with exactly the dimensions
 * ErrorClass, ErrorCode and Operation (`src/adapters/outbound/telemetry/reporting.py`). Two of the
 * four closed ErrorClass values page (`dependency`, `invariant`); the other two are product
 * signals, so every query here excludes them and counts exactly the paging classes.
 *
 * A Metrics Insights WHERE clause accepts only `=`, `!=` and `AND`: a query with `IN (...)` lists
 * was refused by CloudWatch at alarm creation ("Invalid syntax near 'IN'"), live on dev
 * 2026-10-07, which rolled the ingestion deploy back.
 */

/** The two product-signal classes; the closed ErrorClass vocabulary's other two page. */
export const NON_ALARMING_ERROR_CLASSES = ['domain', 'validation'] as const;

const ERRORS_SCHEMA = 'SELECT SUM(Errors) FROM SCHEMA("Adlytics", ErrorClass, ErrorCode, Operation)';

/** An operation is a fixed vocabulary value, never a quote that could end the literal. */
function literal(operation: string): string {
  if (operation.trim() === '' || operation.includes("'")) {
    throw new Error(`Operation ${JSON.stringify(operation)} cannot be a Metrics Insights literal.`);
  }
  return `'${operation}'`;
}

function alarmingClasses(): string {
  return NON_ALARMING_ERROR_CLASSES.map((name) => `ErrorClass != '${name}'`).join(' AND ');
}

/**
 * The paging errors of one fixed operation, aggregated over every ErrorCode, as one time series.
 */
export function alarmingErrorsQuery(operation: string): string {
  return `${ERRORS_SCHEMA} WHERE ${alarmingClasses()} AND Operation = ${literal(operation)}`;
}

/**
 * The paging errors of every operation except `excluded`, one time series per Operation.
 *
 * `GROUP BY` makes the alarm evaluate each operation as its own contributor, so an operation that
 * starts failing while another is already in `ALARM` still notifies. `ORDER BY ... DESC` is
 * required with `GROUP BY` in an alarm, and keeps the failing operations when the query is capped.
 * `excluded` is exactly the operations that have their own alarm, so one failure pages once.
 */
export function alarmingErrorsByOperationQuery(excluded: readonly string[]): string {
  const filters = [
    alarmingClasses(),
    ...excluded.map((operation) => `Operation != ${literal(operation)}`),
  ];
  return `${ERRORS_SCHEMA} WHERE ${filters.join(' AND ')} GROUP BY Operation ORDER BY SUM() DESC`;
}

/**
 * The runbook entry for the catch-all, named in its description: its query lists every failing
 * Operation without the alarm's 100-contributor cap.
 */
export const CATCH_ALL_RUNBOOK =
  'docs/capabilities/operational-data-flow-status/operator-tools.md'
  + '#operation-catch-all-alarm-alarming-errors-by-operation';

/** The alarm-name and construct-id suffix for one operation: `ingest.handler` -> `ingest-handler`. */
export function errorAlarmSuffix(operation: string): string {
  return operation.replace(/[._]/g, '-');
}
