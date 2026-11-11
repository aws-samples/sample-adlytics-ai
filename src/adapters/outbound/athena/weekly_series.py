# SPDX-License-Identifier: MIT-0
"""The demand-forecast weekly aggregates: roster, target and traffic statements (design §3c).

`compose_unload(plan)` writes the three statements from a validated plan. Every read is fenced
to the plan's pinned publications with a bound `publication_id IN (?, ...)` predicate, as T's
Athena composition fences every read; selector values travel as parameters too. The `UNLOAD`
destinations are fresh per build attempt (`r<RedriveCount>a<RetryCount>`) because Athena needs
an empty `TO` location; they are validated S3 prefixes, never caller text. Each statement must
pass `start_envelope.envelope_fits` against the 200,000-byte envelope, else
`statement_too_large`. `AthenaWeeklySeriesSource` runs them one after another on the batch
workgroup, polling `GetQueryExecution` every 2 s and stopping a statement with
`StopQueryExecution` (`athena_timeout`) when less than 60 s of the invocation remain.
`parquet_row_count()` reads an object's row count from its Parquet footer (the Lambda layer has
no pyarrow).

A-SQL-001 (docs/security-dispositions.md): identifiers are literals in this file, the shard size
is an int, the destinations pass an anchored `fullmatch` admitting no quote, and every value
(publication IDs, selectors) binds as `?` through `execution.start`, the one place values are
encoded; no caller text reaches a statement.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from adapters.outbound.athena.execution import (
    StatementOutcome,
    StatementRequest,
    start,
    stream_result_rows,
)
from adapters.outbound.athena.literals import quoted
from adapters.outbound.athena.start_envelope import envelope_fits
from domain.forecasting.policy import MONEY_TARGETS, TARGETS
from domain.operations.statement_protocol import MAX_STATEMENT_BYTES
from domain.shared_kernel.errors import DependencyError, InvariantError
from ports.outbound.weekly_series_source import (
    RosterResult,
    StatementStat,
    UnloadResult,
    WeeklySeriesError,
    WeeklySeriesPlan,
)

if TYPE_CHECKING:
    from types_boto3_athena.client import AthenaClient
    from types_boto3_s3.client import S3Client

POLL_SECONDS: Final = 2.0
STOP_MARGIN_SECONDS: Final = 60.0
SALES_FACT: Final = "fct_retail_sales"
TRAFFIC_FACT: Final = "fct_product_page_views"
_DESTINATION: Final = re.compile(r"^s3://[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]/[A-Za-z0-9/_=.-]+/$")
_PUBLICATION_ID: Final = re.compile(r"^[A-Za-z0-9:._/-]{1,256}$")


@dataclass(frozen=True, slots=True)
class ComposedStatement:
    """One statement and its parameters, in order."""

    kind: str
    sql: str
    parameters: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ComposedStatements:
    """The roster, target and traffic statements of one plan."""

    roster: ComposedStatement
    target: ComposedStatement
    traffic: ComposedStatement


def _ids(values: tuple[str, ...]) -> tuple[str, tuple[str, ...]]:
    for value in values:
        if _PUBLICATION_ID.fullmatch(value) is None:
            raise ValueError("a pinned publication ID has an unexpected shape.")
    return ", ".join("?" for _ in values), values


def _roster_cte(plan: WeeklySeriesPlan) -> tuple[str, tuple[str, ...]]:
    marks, ids = _ids(plan.sales_publication_ids)
    size = int(plan.shard_max_asins)
    sql = (
        f"roster AS (SELECT canonical_asin, "  # noqa: S608  # nosec B608
        f"(dense_rank() OVER (ORDER BY canonical_asin) - 1) / {size} AS shard "
        f"FROM (SELECT DISTINCT {quoted('amazon_asin')} AS canonical_asin "
        f"FROM {quoted(SALES_FACT)} WHERE {quoted('publication_id')} IN ({marks})))"
    )
    return sql, ids


def _week_start(column: str) -> str:
    # Sunday-to-Saturday weeks: day_of_week is 1 (Monday) to 7 (Sunday).
    return f"date_add('day', -(day_of_week({column}) % 7), {column})"


def _destination(prefix: str) -> str:
    if _DESTINATION.fullmatch(prefix) is None:
        raise ValueError("an UNLOAD destination must be a plain s3:// prefix.")
    return prefix


def _unload(select: str, destination: str) -> str:
    return (
        f"UNLOAD ({select}) TO '{_destination(destination)}' "
        "WITH (format = 'PARQUET', compression = 'SNAPPY', partitioned_by = ARRAY['shard'])"
    )


def compose_unload(plan: WeeklySeriesPlan) -> ComposedStatements:
    """Compose the roster SELECT and the two partitioned UNLOADs of one build attempt."""
    roster_cte, roster_ids = _roster_cte(plan)
    roster = ComposedStatement(
        "roster",
        f"WITH {roster_cte} SELECT canonical_asin, shard FROM roster ORDER BY canonical_asin",  # noqa: S608  # nosec B608
        roster_ids,
    )
    sales_marks, sales_ids = _ids(plan.sales_publication_ids)
    names = ", ".join(f"'{target}'" for target in TARGETS)
    values = ", ".join(f"CAST(d.{quoted(target)} AS DECIMAL(38, 4))" for target in TARGETS)
    money = ", ".join(f"'{target}'" for target in sorted(MONEY_TARGETS))
    target_select = (
        f"WITH {roster_cte}, days AS (SELECT {quoted('amazon_asin')} AS canonical_asin, "  # noqa: S608  # nosec B608
        f"{_week_start(quoted('date_key'))} AS week_start, "
        + ", ".join(quoted(target) for target in TARGETS)
        + f", {quoted('currency_code')} FROM {quoted(SALES_FACT)} "  # noqa: S608  # nosec B608
        f"WHERE {quoted('publication_id')} IN ({sales_marks}) "
        f"AND {quoted('distributor_view')} = ? AND {quoted('selling_program')} = ?) "
        "SELECT CAST(1 AS TINYINT) AS schema_version, d.canonical_asin, d.week_start, "
        "t.target, CAST(sum(t.value) AS DECIMAL(38, 4)) AS value_sum, "
        "CAST(count(*) AS TINYINT) AS days_with_row, "
        "CAST(count_if(t.value IS NULL) AS TINYINT) AS days_with_null, "
        f"min(CASE WHEN t.target IN ({money}) THEN d.{quoted('currency_code')} END) "
        "AS min_currency, "
        f"max(CASE WHEN t.target IN ({money}) THEN d.{quoted('currency_code')} END) "
        "AS max_currency, r.shard "
        "FROM days d JOIN roster r ON r.canonical_asin = d.canonical_asin "
        f"CROSS JOIN UNNEST(ARRAY[{names}], ARRAY[{values}]) AS t(target, value) "
        "GROUP BY d.canonical_asin, d.week_start, t.target, r.shard"
    )
    target = ComposedStatement(
        "target",
        _unload(target_select, plan.target_destination),
        (*roster_ids, *sales_ids, plan.distributor_view, plan.selling_program),
    )
    traffic_marks, traffic_ids = _ids(plan.traffic_publication_ids or ("none",))
    traffic_select = (
        f"WITH {roster_cte}, days AS (SELECT {quoted('amazon_asin')} AS canonical_asin, "  # noqa: S608  # nosec B608
        f"{_week_start(quoted('date_key'))} AS week_start, {quoted('glance_views')} "
        f"FROM {quoted(TRAFFIC_FACT)} WHERE {quoted('publication_id')} IN ({traffic_marks})) "
        "SELECT CAST(1 AS TINYINT) AS schema_version, d.canonical_asin, d.week_start, "
        f"CAST(sum(d.{quoted('glance_views')}) AS BIGINT) AS glance_views_sum, "
        "CAST(count(*) AS TINYINT) AS days_with_traffic_row, r.shard "
        "FROM days d JOIN roster r ON r.canonical_asin = d.canonical_asin "
        "GROUP BY d.canonical_asin, d.week_start, r.shard"
    )
    traffic = ComposedStatement(
        "traffic", _unload(traffic_select, plan.traffic_destination), (*roster_ids, *traffic_ids)
    )
    return ComposedStatements(roster, target, traffic)


@dataclass(frozen=True, slots=True)
class AthenaWeeklySeriesTarget:
    """Where the statements run (the batch workgroup and the current database) and the clients.

    The roster result is streamed from its result object (ADR-143), so the builder needs no
    `GetQueryResults` grant, only its own `v2-results/batch/*` objects.
    """

    workgroup: str
    database: str
    athena: AthenaClient
    s3: S3Client


class AthenaWeeklySeriesSource:
    """`WeeklySeriesSourcePort` on Athena, bounded by the invocation's remaining time."""

    def __init__(
        self, target: AthenaWeeklySeriesTarget, remaining_seconds: Callable[[], float]
    ) -> None:
        self._client = target.athena
        self._target = target
        self._remaining = remaining_seconds
        self._fits = envelope_fits(target.workgroup, target.database, MAX_STATEMENT_BYTES)

    def _start(self, statement: ComposedStatement) -> str:
        if not self._fits(statement.sql, statement.parameters):
            raise WeeklySeriesError("statement_too_large")
        request = StatementRequest(
            sql=statement.sql,
            parameters=statement.parameters,
            workgroup=self._target.workgroup,
            database=self._target.database,
            # Result reuse off: every build attempt UNLOADs into its own fresh prefix.
            reuse_minutes=0,
        )
        try:
            return start(self._client, request)
        except DependencyError as error:
            raise WeeklySeriesError(f"athena_start:{error.context.get('aws_code', '')}") from None

    def _await(self, kind: str, query_id: str) -> tuple[StatementStat, str | None]:
        while True:
            try:
                execution = self._client.get_query_execution(QueryExecutionId=query_id)[
                    "QueryExecution"
                ]
            except Exception as error:
                raise WeeklySeriesError(f"athena_status:{type(error).__name__}", query_id) from None
            state = execution.get("Status", {}).get("State", "UNKNOWN")
            statistics = execution.get("Statistics", {})
            if state == "SUCCEEDED":
                stat = StatementStat(
                    kind,
                    query_id,
                    statistics.get("DataScannedInBytes", 0),
                    statistics.get("EngineExecutionTimeInMillis", 0),
                    statistics.get("TotalExecutionTimeInMillis", 0),
                )
                return stat, execution.get("ResultConfiguration", {}).get("OutputLocation")
            if state in {"FAILED", "CANCELLED"}:
                raise WeeklySeriesError(f"athena_{state.lower()}", query_id)
            if self._remaining() < STOP_MARGIN_SECONDS + POLL_SECONDS:
                stopped = True
                try:
                    self._client.stop_query_execution(QueryExecutionId=query_id)
                # error-propagation: recorded as stop_requested=False on the timeout below
                except Exception:
                    stopped = False
                raise WeeklySeriesError("athena_timeout", query_id, stop_requested=stopped)
            time.sleep(POLL_SECONDS)

    def _run(self, statement: ComposedStatement) -> tuple[StatementStat, str | None]:
        return self._await(statement.kind, self._start(statement))

    def roster(self, plan: WeeklySeriesPlan) -> RosterResult:
        """Run the roster statement and stream its result object."""
        stat, location = self._run(compose_unload(plan).roster)
        outcome = StatementOutcome(output_location=location)
        asins: list[str] = []
        shards: list[int] = []
        try:
            for row in stream_result_rows(
                self._target.s3, outcome, execution_id=stat.query_id, cells=2
            ):
                asins.append(str(row[0]))
                shards.append(int(str(row[1])))
        except DependencyError, InvariantError:
            raise WeeklySeriesError("athena_result_unreadable", stat.query_id) from None
        return RosterResult(tuple(asins), tuple(shards), stat)

    def unload(self, plan: WeeklySeriesPlan) -> UnloadResult:
        """Run the target UNLOAD, then the traffic UNLOAD."""
        statements = compose_unload(plan)
        return UnloadResult((self._run(statements.target)[0], self._run(statements.traffic)[0]))


# ---- Parquet footer row count (Thrift compact protocol, FileMetaData field 3) ----

_STOP, _TRUE, _FALSE, _BYTE, _I16, _I32, _I64, _DOUBLE, _BINARY, _LIST, _SET, _MAP, _STRUCT = (
    0,
    1,
    2,
    3,
    4,
    5,
    6,
    7,
    8,
    9,
    10,
    11,
    12,
)
_NUM_ROWS_FIELD: Final = 3


class _Reader:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self._at = 0

    def byte(self) -> int:
        value = self._data[self._at]
        self._at += 1
        return value

    def varint(self) -> int:
        shift = 0
        value = 0
        while True:
            octet = self.byte()
            value |= (octet & 0x7F) << shift
            if not octet & 0x80:
                return value
            shift += 7

    def zigzag(self) -> int:
        raw = self.varint()
        return (raw >> 1) ^ -(raw & 1)

    def skip(self, kind: int) -> None:  # noqa: PLR0912 - one branch per compact type
        if kind in {_TRUE, _FALSE}:
            return
        if kind == _BYTE:
            self._at += 1
        elif kind in {_I16, _I32, _I64}:
            self.varint()
        elif kind == _DOUBLE:
            self._at += 8
        elif kind == _BINARY:
            length = self.varint()  # read first: `self._at += self.varint()` loses its advance
            self._at += length
        elif kind in {_LIST, _SET}:
            header = self.byte()
            size = header >> 4
            if size == 15:  # noqa: PLR2004 - the compact protocol's long-size marker
                size = self.varint()
            element = header & 0x0F
            for _ in range(size):
                if element in {_TRUE, _FALSE}:
                    self._at += 1
                else:
                    self.skip(element)
        elif kind == _MAP:
            size = self.varint()
            if size:
                types = self.byte()
                for _ in range(size):
                    self.skip(types >> 4)
                    self.skip(types & 0x0F)
        elif kind == _STRUCT:
            self.struct_fields(None)
        else:
            raise ValueError(f"unknown thrift compact type {kind}")

    def struct_fields(self, wanted: int | None) -> int | None:
        """Walk one struct; return the i64 at field `wanted` when it is met, else None."""
        last = 0
        while True:
            header = self.byte()
            kind = header & 0x0F
            if kind == _STOP:
                return None
            delta = header >> 4
            field = last + delta if delta else self.zigzag()
            last = field
            if wanted is not None and field == wanted and kind == _I64:
                return self.zigzag()
            self.skip(kind)


def parquet_row_count(footer: bytes) -> int:
    """Return `num_rows` from a Parquet file's tail (at least the footer and its 8-byte trailer)."""
    if len(footer) < 12 or footer[-4:] != b"PAR1":  # noqa: PLR2004 - trailer and magic
        raise ValueError("not a Parquet file tail")
    length = int.from_bytes(footer[-8:-4], "little")
    if length + 8 > len(footer):
        raise ValueError("the tail does not hold the whole footer")
    metadata = footer[-8 - length : -8]
    rows = _Reader(metadata).struct_fields(_NUM_ROWS_FIELD)
    if rows is None:
        raise ValueError("the Parquet footer has no num_rows")
    return rows
