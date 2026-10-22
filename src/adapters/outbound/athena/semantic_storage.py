# SPDX-License-Identifier: MIT-0
"""Append-only Athena/Iceberg projection for immutable Semantic V2 publications."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from time import monotonic
from typing import TYPE_CHECKING, Final, cast

import boto3

from adapters.outbound.athena.execution import (
    BATCH_MAX_WAIT_SECONDS,
    StatementRequest,
    await_completion,
    fetch_rows,
    start,
)
from adapters.outbound.athena.literals import quoted
from adapters.outbound.athena.parameters import sql_literal
from adapters.outbound.athena.start_envelope import StatementFits, envelope_fits
from adapters.outbound.s3.semantic_storage import source_publication_payload
from adapters.outbound.telemetry.tracing import ATHENA_STATEMENT, span, trace_boundary
from adapters.outbound.v2_storage_catalog import V2Column, V2StorageCatalog, V2Table
from domain.operations.statement_protocol import MAX_STATEMENT_BYTES
from domain.semantics.publication import PublicationCandidate, SuccessfulPublication
from domain.semantics.source_aligned import SourceAlignedPublication
from ports.outbound.canonical import (
    CanonicalDestination,
    CanonicalMaterializeRequest,
    CanonicalWriteResult,
)
from ports.outbound.source_aligned import MaterializeSourceRowsetResult

if TYPE_CHECKING:  # pragma: no cover
    from types_boto3_athena.client import AthenaClient

    from adapters.outbound.s3.semantic_storage import ProjectionUnitSource
    from ports.outbound.unit_staging import StagedUnit

__all__ = [
    "AthenaCanonicalMaterializer",
    "AthenaSemanticTarget",
    "AthenaSourceMaterializer",
    "AthenaTableUnitProjector",
    "PhysicalRecord",
    "StagedSource",
    "UnitProjectionResult",
    "UnitWorkload",
    "canonical_records",
    "canonical_unit_records",
    "merge_batches",
    "merge_statement",
    "multi_select_statement",
    "ordered_records",
    "pack_object_batches",
    "select_statement",
    "source_records",
    "staged_merge_statement",
    "verification_matches",
    "verification_statement",
]

_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")
_MAX_PARAMETER_LENGTH = 1024
type PhysicalValue = str | int | float | bool | Decimal | date | datetime | None
type PhysicalRecord = dict[str, PhysicalValue]


@dataclass(frozen=True, slots=True)
class AthenaSemanticTarget:
    """The BAKE DML workgroup and generated databases for all V2 physical projections.

    The field keeps its historical name; it carries the BAKE DML workgroup
    (`adlytics-<env>-v2-bake`, SEMANTIC-M1), never the retained `batch` one.
    """

    batch_workgroup: str
    source_database: str
    candidate_database: str
    current_database: str
    staging_database: str
    """The staging database whose external tables read staged units (ADR-149)."""

    def __post_init__(self) -> None:
        if any(
            not value.strip()
            for value in (
                self.batch_workgroup,
                self.source_database,
                self.candidate_database,
                self.current_database,
                self.staging_database,
            )
        ):
            raise ValueError("Athena Semantic V2 target requires every workgroup/database name.")
        if not _IDENTIFIER.fullmatch(self.staging_database):
            raise ValueError("Athena Semantic V2 staging database must be a generated identifier.")


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _revision(publication: PublicationCandidate | SuccessfulPublication) -> str:
    revision = publication.source_revision
    return "|".join(
        (
            revision.scheme.value,
            revision.precision.value,
            revision.observed_at.isoformat(),
            revision.source_id,
        )
    )


def source_records(
    publication: SourceAlignedPublication,
    table: V2Table,
) -> tuple[PhysicalRecord, ...]:
    """Project one source publication into its generated envelope-plus-row records."""
    payload = source_publication_payload(publication)
    landed = publication.landed_delivery
    rowset = publication.rowset
    common: PhysicalRecord = {
        "source_publication_id": publication.reference.value,
        "contract_id": table.contract_id or "",
        "content_checksum": publication.content_checksum,
        "delivery_scope_json": _json(payload["delivery_scope"]),
        "extraction_start": rowset.extraction_coverage.window.start,
        "extraction_end": rowset.extraction_coverage.window.end,
        "interpreter_key": rowset.interpreter.value,
        "interpreter_version": rowset.interpreter.version,
        "source_options_json": _json(payload["source_options"]),
        "manifest_key": landed.manifest_key,
        "manifest_version": landed.manifest_version,
        "manifest_checksum": landed.manifest_checksum,
        "object_key": landed.object_key,
        "object_version": landed.object_version,
        "object_checksum": landed.object_checksum,
        "source_revision_json": _json(payload["source_revision"]),
        "published_at": publication.published_at,
    }
    records: list[PhysicalRecord] = [
        {
            **common,
            "record_kind": "envelope",
            "source_row_ordinal": -1,
            "source_payload_json": None,
            "measurement_start": None,
            "measurement_end": None,
            "measurement_grain": None,
            "measurement_calendar": None,
            "measurement_week_starts_on": None,
            "source_timezone": None,
        }
    ]
    encoded_rows = payload["rows"]
    if not isinstance(encoded_rows, list):  # pragma: no cover - encoder owns the shape
        raise ValueError("source publication encoder returned no row array.")
    encoded_items = cast("list[object]", encoded_rows)
    for ordinal, (row, encoded) in enumerate(zip(rowset.rows, encoded_items, strict=True)):
        records.append(
            {
                **common,
                "record_kind": "row",
                "source_row_ordinal": ordinal,
                "source_payload_json": _json(encoded),
                "measurement_start": row.measurement.window.start,
                "measurement_end": row.measurement.window.end,
                "measurement_grain": row.measurement.grain.value,
                "measurement_calendar": row.measurement.calendar.value,
                "measurement_week_starts_on": row.measurement.calendar.week_starts_on,
                "source_timezone": row.measurement.source_timezone,
            }
        )
    return tuple(records)


def canonical_records(
    request: CanonicalMaterializeRequest,
    table: V2Table,
) -> tuple[PhysicalRecord, ...]:
    """Project typed canonical rows into generated candidate/successful-history records."""
    publication = request.publication
    envelope: PhysicalRecord = {
        "publication_id": publication.publication_id.value,
        "attempt_id": publication.attempt_id.value,
        "partition_key": publication.partition.identity,
        "publisher": publication.publisher.value,
        "fencing_token": publication.fencing_token,
        "source_publications_json": _json([item.value for item in publication.source_inputs]),
        "source_revision": _revision(publication),
        "content_checksum": publication.content_checksum,
        "rule_set_version": request.rule_set_version,
        "created_at": publication.published_at,
    }
    envelope_names = set(envelope)
    canonical_names = tuple(
        column.name for column in table.columns if column.name not in envelope_names
    )
    records: list[PhysicalRecord] = []
    for row in request.rows:
        mapped = dict(row)
        unknown = set(mapped) - set(canonical_names)
        if unknown:
            raise ValueError(
                f"{table.name}: canonical row contains unknown columns {sorted(unknown)}."
            )
        records.append(
            {
                **envelope,
                **{name: mapped.get(name) for name in canonical_names},
            }
        )
    return tuple(records)


def _athena_type(column_type: str) -> str:
    if column_type == "string":
        return "varchar"
    if column_type == "int":
        return "integer"
    if column_type in {"long", "bigint"}:
        return "bigint"
    if column_type in {"float", "double"}:
        return "double"
    if column_type in {"boolean", "date", "timestamp"}:
        return column_type
    if re.fullmatch(r"decimal\(\d+,\s*\d+\)", column_type):
        return column_type
    raise ValueError(f"unsupported generated V2 Athena type {column_type!r}.")


def _value(value: PhysicalValue, column: V2Column) -> tuple[str, tuple[str, ...]]:
    sql_type = _athena_type(column.type)
    if value is None:
        if not column.nullable:
            raise ValueError(f"required V2 column {column.name!r} is null.")
        return f"CAST(NULL AS {sql_type})", ()
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError(f"V2 timestamp {column.name!r} must be timezone-aware.")
        rendered = value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")
    elif isinstance(value, date):
        rendered = value.isoformat()
    elif isinstance(value, bool):
        rendered = "true" if value else "false"
    else:
        rendered = str(value)
    literal = sql_literal(rendered)
    if len(literal.encode()) > _MAX_PARAMETER_LENGTH:
        return f"CAST({literal} AS {sql_type})", ()
    return f"CAST(? AS {sql_type})", (rendered,)


def _ordered_record(table: V2Table, record: PhysicalRecord) -> tuple[PhysicalValue, ...]:
    names = tuple(column.name for column in table.columns)
    missing = set(names) - set(record)
    if missing:
        raise ValueError(f"{table.name}: physical record is missing {sorted(missing)}.")
    return tuple(record[name] for name in names)


def merge_statement(
    table: V2Table,
    records: tuple[PhysicalRecord, ...],
) -> tuple[str, tuple[str, ...]]:
    """Compose one insert-only immutable-partition MERGE from generated identifiers."""
    if not records:
        raise ValueError(f"{table.name}: immutable materialization requires at least one record.")
    columns = table.columns
    names = tuple(column.name for column in columns)
    for name in names:
        if not _IDENTIFIER.fullmatch(name):
            raise ValueError(f"unsafe generated V2 column identifier {name!r}.")
    parameters: list[str] = []
    rows: list[str] = []
    for record in records:
        expressions: list[str] = []
        for value, column in zip(_ordered_record(table, record), columns, strict=True):
            expression, bound = _value(value, column)
            expressions.append(expression)
            parameters.extend(bound)
        rows.append("(" + ", ".join(expressions) + ")")
    column_list = ", ".join(quoted(name) for name in names)
    source_columns = ", ".join(quoted(name) for name in names)
    match = " AND ".join(f"target.{quoted(name)} = incoming.{quoted(name)}" for name in table.grain)
    inserted = ", ".join(f"incoming.{quoted(name)}" for name in names)
    sql = (
        f"MERGE INTO {quoted(table.name)} AS target "  # noqa: S608 - identifiers come from generated catalog
        f"USING (VALUES {', '.join(rows)}) AS incoming ({source_columns}) "
        f"ON {match} "
        f"WHEN NOT MATCHED THEN INSERT ({column_list}) VALUES ({inserted})"
    )
    return sql, tuple(parameters)


def _grain_sort_value(value: PhysicalValue) -> tuple[int, Decimal | str]:
    """Order one grain value the way Athena's ORDER BY orders the typed column.

    Numeric columns sort numerically; the previous rendered-string key put ordinal
    10 before 2, so any partition past nine rows diverged from the SELECT's order
    and an exact projection was refused. Rendered ISO dates, timestamps, booleans,
    and strings already sort like their typed columns. NULL sorts last, matching
    Athena's ascending default.
    """
    if value is None:
        return (1, "")
    if isinstance(value, bool):
        return (0, "false" if not value else "true")
    if isinstance(value, int | float | Decimal):
        return (0, Decimal(str(value)))
    return (0, _expected(value) or "")


def ordered_records(
    table: V2Table,
    records: tuple[PhysicalRecord, ...],
) -> tuple[PhysicalRecord, ...]:
    """Order immutable records exactly as the exact-partition SELECT returns them."""

    def grain(record: PhysicalRecord) -> tuple[tuple[int, Decimal | str], ...]:
        return tuple(_grain_sort_value(record[name]) for name in table.grain)

    def rendered(record: PhysicalRecord) -> tuple[str, ...]:
        return tuple(_expected(record[name]) or "" for name in table.grain)

    ordered = tuple(sorted(records, key=grain))
    keys = tuple(rendered(record) for record in ordered)
    if len(set(keys)) != len(keys):
        raise ValueError(f"{table.name}: immutable records repeat generated grain values.")
    return ordered


def _expected_records(
    table: V2Table,
    records: tuple[PhysicalRecord, ...],
) -> tuple[tuple[str | None, ...], ...]:
    return tuple(
        tuple(_expected(value) for value in _ordered_record(table, record)) for record in records
    )


def _missing_records(
    table: V2Table,
    records: tuple[PhysicalRecord, ...],
    actual: tuple[tuple[str | None, ...], ...],
) -> tuple[PhysicalRecord, ...] | None:
    expected = _expected_records(table, records)
    column_indexes = {column.name: index for index, column in enumerate(table.columns)}
    grain_indexes = tuple(column_indexes[name] for name in table.grain)
    expected_by_grain = {
        tuple(row[index] for index in grain_indexes): (record, row)
        for record, row in zip(records, expected, strict=True)
    }
    found: set[tuple[str | None, ...]] = set()
    for row in actual:
        key = tuple(row[index] for index in grain_indexes)
        match = expected_by_grain.get(key)
        if key in found or match is None or match[1] != row:
            return None
        found.add(key)
    return tuple(record for key, (record, _) in expected_by_grain.items() if key not in found)


def merge_batches(
    table: V2Table,
    records: tuple[PhysicalRecord, ...],
    fits: StatementFits,
) -> tuple[tuple[PhysicalRecord, ...], ...]:
    """Split immutable records into contiguous merges, each within the statement byte bound.

    ``fits`` is the one §11 byte measure (``start_envelope.envelope_fits``); there is no
    parameter-count bound. A record whose own merge does not fit raises ``ValueError``.
    """
    batches: list[tuple[PhysicalRecord, ...]] = []
    pending: list[PhysicalRecord] = []
    for record in records:
        proposed = (*pending, record)
        within = fits(*merge_statement(table, proposed))
        if not within and pending:
            batches.append(tuple(pending))
            pending = [record]
            within = fits(*merge_statement(table, tuple(pending)))
        else:
            pending.append(record)
        if not within:
            raise ValueError(
                f"{table.name}: one immutable record exceeds the statement byte bound."
            )
    if pending:
        batches.append(tuple(pending))
    return tuple(batches)


def select_statement(table: V2Table) -> str:
    """Compose the exact-partition readback query for an immutable projection."""
    names = ", ".join(quoted(column.name) for column in table.columns)
    partition = table.partition_keys[0]
    ordering = ", ".join(quoted(name) for name in table.grain)
    return (
        f"SELECT {names} FROM {quoted(table.name)} "  # noqa: S608 - generated identifiers only  # nosec B608 # A-SQL-001
        f"WHERE {quoted(partition)} = CAST(? AS varchar) ORDER BY {ordering}"
    )


def _expected(value: PhysicalValue) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        rendered = value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")
        return rendered[:-3] + "000"
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _readback_reason(
    table: V2Table,
    expected: tuple[tuple[str | None, ...], ...],
    actual: tuple[tuple[str | None, ...], ...],
) -> str:
    if len(actual) != len(expected):
        return f"Iceberg readback row count differs for {table.name}."
    for row_index, (wanted, found) in enumerate(zip(expected, actual, strict=True)):
        if len(found) != len(wanted):
            return f"Iceberg readback column count differs at row {row_index} for {table.name}."
        for column_index, (wanted_value, found_value) in enumerate(zip(wanted, found, strict=True)):
            if found_value != wanted_value:
                return (
                    f"Iceberg readback differs at row {row_index}, column "
                    f"{table.columns[column_index].name}."
                )
    return "Iceberg readback differs from immutable records."


@dataclass(frozen=True, slots=True)
class _ProjectionRequest:
    table: V2Table
    database: str
    records: tuple[PhysicalRecord, ...]
    identity: str


@dataclass(frozen=True, slots=True)
class _StatementExecutionRequest:
    table: V2Table
    operation_kind: str
    statement: StatementRequest


@trace_boundary(ATHENA_STATEMENT)
def _execute_statement(client: AthenaClient, request: _StatementExecutionRequest) -> str:
    """Run one traced semantic-storage statement to completion and return its execution id."""
    with span(
        ATHENA_STATEMENT,
        operation_kind=request.operation_kind,
        logical_table=request.table.name,
    ) as statement_span:
        execution = start(client, request.statement)
        statement_span.record(query_id=execution)
        outcome = await_completion(
            client,
            execution,
            limit=BATCH_MAX_WAIT_SECONDS,
        )
        statement_span.record(
            bytes_scanned=outcome.bytes_scanned,
            queue_millis=outcome.queue_millis,
            planning_millis=outcome.planning_millis,
            engine_millis=outcome.engine_millis,
            total_millis=outcome.total_millis,
            reuse=outcome.reused_previous_result,
        )
    return execution


class _AthenaProjection:
    def __init__(
        self,
        target: AthenaSemanticTarget,
        catalog: V2StorageCatalog,
        client: AthenaClient | None,
    ) -> None:
        self._target = target
        self._catalog = catalog
        self._client = client or boto3.client("athena")

    def attest(self, request: _ProjectionRequest) -> bool:
        """Compare one exact identity projection through SELECT only."""
        records = ordered_records(request.table, request.records)
        expected = _expected_records(request.table, records)
        return self._read(request.table, request.database, request.identity) == expected

    def _statement(self, request: _StatementExecutionRequest) -> str:
        return _execute_statement(self._client, request)

    def materialize(self, request: _ProjectionRequest) -> tuple[bool, bool, str]:
        table = request.table
        database = request.database
        records = ordered_records(table, request.records)
        identity = request.identity
        expected = _expected_records(table, records)
        before = self._read(table, database, identity)
        if before == expected:
            return True, False, ""
        missing = _missing_records(table, records, before)
        if missing is None:
            return False, False, "identity has different Iceberg records"
        created = False
        fits = envelope_fits(self._target.batch_workgroup, database, MAX_STATEMENT_BYTES)
        for batch in merge_batches(table, missing, fits):
            sql, parameters = merge_statement(table, batch)
            self._statement(
                _StatementExecutionRequest(
                    table=table,
                    operation_kind="merge",
                    statement=StatementRequest(
                        sql=sql,
                        parameters=parameters,
                        workgroup=self._target.batch_workgroup,
                        database=database,
                        reuse_minutes=0,
                    ),
                )
            )
            created = True
        after = self._read(table, database, identity)
        accepted = after == expected
        return (
            accepted,
            accepted and created,
            "" if accepted else _readback_reason(table, expected, after),
        )

    def _read(
        self,
        table: V2Table,
        database: str,
        identity: str,
    ) -> tuple[tuple[str | None, ...], ...]:
        execution = self._statement(
            _StatementExecutionRequest(
                table=table,
                operation_kind="select",
                statement=StatementRequest(
                    sql=select_statement(table),
                    parameters=(identity,),
                    workgroup=self._target.batch_workgroup,
                    database=database,
                    reuse_minutes=0,
                ),
            )
        )
        return tuple(tuple(row) for row in fetch_rows(self._client, execution))


def canonical_unit_records(
    source: ProjectionUnitSource,
    table: V2Table,
) -> tuple[PhysicalRecord, ...]:
    """Project one decoded immutable canonical object into merge records.

    Mirrors `canonical_records` from exactly the flat envelope the object stores, so the
    table writer never reconstructs a domain publication or reads publication state.
    """
    envelope: PhysicalRecord = {
        "publication_id": source.publication_id,
        "attempt_id": source.attempt_id,
        "partition_key": source.partition_key,
        "publisher": source.publisher,
        "fencing_token": source.fencing_token,
        "source_publications_json": _json(list(source.source_publications)),
        "source_revision": source.source_revision,
        "content_checksum": source.content_checksum,
        "rule_set_version": source.rule_set_version,
        "created_at": source.created_at,
    }
    envelope_names = set(envelope)
    canonical_names = tuple(
        column.name for column in table.columns if column.name not in envelope_names
    )
    records: list[PhysicalRecord] = []
    for row in source.rows:
        mapped = dict(row)
        unknown = set(mapped) - set(canonical_names)
        if unknown:
            raise ValueError(
                f"{table.name}: canonical row contains unknown columns {sorted(unknown)}."
            )
        records.append({**envelope, **{name: mapped.get(name) for name in canonical_names}})
    return tuple(records)


def pack_object_batches(
    table: V2Table,
    objects: tuple[tuple[PhysicalRecord, ...], ...],
    fits: StatementFits,
) -> tuple[tuple[PhysicalRecord, ...], ...]:
    """Pack whole immutable objects greedily under the one statement byte bound.

    A failed statement then leaves whole objects either fully merged or untouched; only
    an object that alone exceeds the bound is split, exactly as `merge_batches` splits.
    """
    batches: list[tuple[PhysicalRecord, ...]] = []
    pending: tuple[PhysicalRecord, ...] = ()
    for records in objects:
        if not records:
            continue
        proposed = (*pending, *records)
        if fits(*merge_statement(table, proposed)):
            pending = proposed
            continue
        if pending:
            batches.append(pending)
            pending = ()
        if fits(*merge_statement(table, records)):
            pending = records
        else:
            batches.extend(merge_batches(table, records, fits))
    if pending:
        batches.append(pending)
    return tuple(batches)


def multi_select_statement(table: V2Table, identity_count: int) -> str:
    """Compose one readback over every identity a unit projected."""
    if identity_count < 1:
        raise ValueError("unit readback requires at least one identity.")
    names = ", ".join(quoted(column.name) for column in table.columns)
    partition = quoted(table.partition_keys[0])
    placeholders = ", ".join("CAST(? AS varchar)" for _ in range(identity_count))
    ordering = ", ".join((partition, *(quoted(name) for name in table.grain)))
    return (
        f"SELECT {names} FROM {quoted(table.name)} "  # noqa: S608 - generated identifiers only  # nosec B608 # A-SQL-001
        f"WHERE {partition} IN ({placeholders}) ORDER BY {ordering}"
    )


_STAGING_KEY = re.compile(r"^[0-9a-f]{64}$")
_VERIFICATION_SIDES: Final = ("target", "staged", "matched_target", "matched_staged")
_COUNT = re.compile(r"^(0|[1-9][0-9]*)$")


def _equal(left: str, right: str, column: V2Column) -> str:
    """Compare one column: ``=``, or "equal, or both NULL" when the column is nullable.

    Never ``IS NOT DISTINCT FROM``: on Athena it dropped correct rows when every compared row
    held NULL in a nullable varchar column (ADR-149 decision 2).
    """
    if column.nullable:
        return f"({left} = {right} OR ({left} IS NULL AND {right} IS NULL))"
    return f"{left} = {right}"


@dataclass(frozen=True, slots=True)
class StagedSource:
    """Where one unit's staged rows are read: the staging database and the unit's key."""

    staging_database: str
    staging_key: str

    def __post_init__(self) -> None:
        if not _IDENTIFIER.fullmatch(self.staging_database):
            raise ValueError(f"unsafe staging database identifier {self.staging_database!r}.")
        if not _STAGING_KEY.fullmatch(self.staging_key):
            raise ValueError("a staging key is 64 lowercase hex characters.")


def _staged_identifiers(table: V2Table) -> None:
    for name in (table.name, *(column.name for column in table.columns)):
        if not _IDENTIFIER.fullmatch(name):
            raise ValueError(f"unsafe generated V2 identifier {name!r}.")


def _staged_casts(table: V2Table) -> str:
    return ", ".join(
        f"CAST({quoted(column.name)} AS {_athena_type(column.type)}) AS {quoted(column.name)}"
        for column in table.columns
    )


def _staged_rows(table: V2Table, source: StagedSource) -> str:
    return (
        f"FROM {quoted(source.staging_database)}.{quoted(table.name)} "
        f"WHERE {quoted('staging_key')} = '{source.staging_key}'"
    )


def _grain_match(table: V2Table, left: str, right: str) -> str:
    columns = {column.name: column for column in table.columns}
    return " AND ".join(
        _equal(f"{left}.{quoted(name)}", f"{right}.{quoted(name)}", columns[name])
        for name in table.grain
    )


def staged_merge_statement(table: V2Table, source: StagedSource) -> str:
    """Compose the unit's one insert-only MERGE, reading its staged rows (ADR-149 decision 1).

    The source casts each all-``string`` staging column exactly as the deleted renderer cast
    the same text; the ``ON`` clause matches a nullable grain column when both sides are NULL,
    so a re-run inserts nothing. The key is a validated 64-hex literal, so the MERGE has no
    parameters.
    """
    _staged_identifiers(table)
    names = ", ".join(quoted(column.name) for column in table.columns)
    inserted = ", ".join(f"incoming.{quoted(column.name)}" for column in table.columns)
    rows = f"SELECT {_staged_casts(table)} {_staged_rows(table, source)}"
    return (
        f"MERGE INTO {quoted(table.name)} AS target "  # noqa: S608 - generated identifiers and a validated hex key  # nosec B608 # A-SQL-001
        f"USING ({rows}) "
        f"AS incoming ON {_grain_match(table, 'target', 'incoming')} "
        f"WHEN NOT MATCHED THEN INSERT ({names}) VALUES ({inserted})"
    )


def verification_statement(table: V2Table, source: StagedSource, identity_count: int) -> str:
    """Compose the unit's one set-based verification: four counts per identity, never rows.

    ``target`` and ``staged`` count each side; ``matched_target`` counts target rows equal in
    every column to a staged row, and ``matched_staged`` the distinct staged rows matched. The
    identities are bound as ``CAST(? AS varchar)`` parameters.
    """
    _staged_identifiers(table)
    if identity_count < 1:
        raise ValueError("a unit verification requires at least one identity.")
    partition = quoted(table.partition_keys[0])
    ordinal = quoted("stg_ordinal")
    names = ", ".join(quoted(column.name) for column in table.columns)
    placeholders = ", ".join("CAST(? AS varchar)" for _ in range(identity_count))
    compared = " AND ".join(
        _equal(f"t.{quoted(column.name)}", f"s.{quoted(column.name)}", column)
        for column in table.columns
        if column.name not in table.grain
    )
    where = f" WHERE {compared}" if compared else ""
    return (
        f"WITH s AS (SELECT CAST({ordinal} AS bigint) AS {ordinal}, {_staged_casts(table)} "  # noqa: S608 - generated identifiers and a validated hex key  # nosec B608 # A-SQL-001
        f"{_staged_rows(table, source)}), "
        f"t AS (SELECT {names} FROM {quoted(table.name)} WHERE {partition} IN ({placeholders})), "
        f"m AS (SELECT t.{partition} AS identity, s.{ordinal} AS ordinal FROM t JOIN s "
        f"ON {_grain_match(table, 't', 's')}{where}) "
        f"SELECT 'target' AS side, {partition} AS identity, count(*) AS n FROM t GROUP BY 2 "
        f"UNION ALL SELECT 'staged', {partition}, count(*) FROM s GROUP BY 2 "
        "UNION ALL SELECT 'matched_target', identity, count(*) FROM m GROUP BY 2 "
        "UNION ALL SELECT 'matched_staged', identity, count(DISTINCT ordinal) FROM m GROUP BY 2"
    )


def _verification_counts(
    staged: StagedUnit, rows: tuple[tuple[str | None, ...], ...]
) -> dict[tuple[str, str], int] | None:
    """Return each (side, identity) count, or ``None`` for any row of another shape."""
    identities = {identity for identity, _ in staged.row_counts}
    counts: dict[tuple[str, str], int] = {}
    for row in rows:
        if len(row) != 3:  # noqa: PLR2004 - side, identity, count
            return None
        side, identity, count = row
        if (
            side not in _VERIFICATION_SIDES
            or identity not in identities
            or count is None
            or not _COUNT.fullmatch(count)
            or (side, identity) in counts
        ):
            return None
        counts[(side, identity)] = int(count)
    return counts


def verification_matches(staged: StagedUnit, rows: tuple[tuple[str | None, ...], ...]) -> bool:
    """Accept a unit only when all four sides count exactly its staged rows per identity.

    Staged grains are unique and the partition key is in every grain, so equal target and
    staged counts plus both matched counts mean every target row matches one staged row with
    identical values: a missing, extra, duplicated, altered or misplaced row, and an empty or
    missing staged object, each leave some side short or over.
    """
    counts = _verification_counts(staged, rows)
    expected = {
        (side, identity): count
        for side in _VERIFICATION_SIDES
        for identity, count in staged.row_counts
    }
    return counts == expected


@dataclass(frozen=True, slots=True)
class UnitWorkload:
    """One physical table's complete projection input, keyed by identity."""

    table: V2Table
    database: str
    records_by_identity: tuple[tuple[str, tuple[PhysicalRecord, ...]], ...]

    def __post_init__(self) -> None:
        if not self.records_by_identity:
            raise ValueError("a unit workload requires at least one identity.")
        identities = tuple(identity for identity, _ in self.records_by_identity)
        if len(identities) != len(set(identities)):
            raise ValueError("a unit workload cannot repeat an identity.")


@dataclass(frozen=True, slots=True)
class UnitProjectionResult:
    """The verified and measured outcome of one unit projection."""

    accepted: bool
    statement_count: int
    row_count: int
    athena_seconds: float
    reason: str = ""

    def __post_init__(self) -> None:
        if self.statement_count < 0 or self.row_count < 0 or self.athena_seconds < 0:
            raise ValueError("unit projection counters cannot be negative.")
        if not self.accepted and not self.reason:
            raise ValueError("a refused unit projection must state why.")


class AthenaTableUnitProjector:
    """Consolidated merges and one verification per physical-table write unit."""

    def __init__(
        self,
        target: AthenaSemanticTarget,
        client: AthenaClient | None = None,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._target = target
        self._client: AthenaClient = client or boto3.client("athena")
        self._clock = clock

    def project(self, workload: UnitWorkload, *, retry: bool) -> UnitProjectionResult:
        """Merge every missing object sequentially, verify once, and measure Athena time."""
        started = self._clock()
        table = workload.table
        statements = 0
        objects: tuple[tuple[PhysicalRecord, ...], ...] = tuple(
            ordered_records(table, records) for _, records in workload.records_by_identity
        )
        if retry:
            # A retried unit first excludes fully projected identities so its merges
            # re-run only interrupted work; the merge itself stays insert-only.
            actual = self._read_identities(workload)
            statements += 1
            remaining: list[tuple[PhysicalRecord, ...]] = []
            for (identity, _), records in zip(workload.records_by_identity, objects, strict=True):
                if actual.get(identity) != _expected_records(table, records):
                    remaining.append(records)
            objects = tuple(remaining)
        fits = envelope_fits(self._target.batch_workgroup, workload.database, MAX_STATEMENT_BYTES)
        for batch in pack_object_batches(table, objects, fits):
            sql, parameters = merge_statement(table, batch)
            _execute_statement(
                self._client,
                _StatementExecutionRequest(
                    table=table,
                    operation_kind="merge",
                    statement=StatementRequest(
                        sql=sql,
                        parameters=parameters,
                        workgroup=self._target.batch_workgroup,
                        database=workload.database,
                        reuse_minutes=0,
                    ),
                ),
            )
            statements += 1
        actual = self._read_identities(workload)
        statements += 1
        row_count = 0
        for identity, records in workload.records_by_identity:
            expected = _expected_records(table, ordered_records(table, records))
            row_count += len(expected)
            if actual.get(identity) != expected:
                return UnitProjectionResult(
                    accepted=False,
                    statement_count=statements,
                    row_count=row_count,
                    athena_seconds=max(0.0, self._clock() - started),
                    reason=f"unit readback differs for identity on {table.name}.",
                )
        return UnitProjectionResult(
            accepted=True,
            statement_count=statements,
            row_count=row_count,
            athena_seconds=max(0.0, self._clock() - started),
        )

    def _read_identities(
        self, workload: UnitWorkload
    ) -> dict[str, tuple[tuple[str | None, ...], ...]]:
        table = workload.table
        identities = tuple(identity for identity, _ in workload.records_by_identity)
        partition_index = next(
            index
            for index, column in enumerate(table.columns)
            if column.name == table.partition_keys[0]
        )
        execution = _execute_statement(
            self._client,
            _StatementExecutionRequest(
                table=table,
                operation_kind="select",
                statement=StatementRequest(
                    sql=multi_select_statement(table, len(identities)),
                    parameters=identities,
                    workgroup=self._target.batch_workgroup,
                    database=workload.database,
                    reuse_minutes=0,
                ),
            ),
        )
        grouped: dict[str, list[tuple[str | None, ...]]] = {}
        for row in fetch_rows(self._client, execution):
            rendered = tuple(row)
            grouped.setdefault(rendered[partition_index] or "", []).append(rendered)
        return {identity: tuple(rows) for identity, rows in grouped.items()}


class AthenaSourceMaterializer:
    """Append and exactly verify one immutable source-aligned Iceberg partition."""

    def __init__(
        self,
        target: AthenaSemanticTarget,
        catalog: V2StorageCatalog,
        client: AthenaClient | None = None,
    ) -> None:
        self._projection = _AthenaProjection(target, catalog, client)
        self._target = target
        self._catalog = catalog

    def attest(self, publication: SourceAlignedPublication) -> MaterializeSourceRowsetResult:
        """Verify one exact source projection without writing or repairing it."""
        table = self._catalog.source_for_interpreter(publication.rowset.interpreter.value)
        accepted = self._projection.attest(
            _ProjectionRequest(
                table=table,
                database=self._target.source_database,
                records=source_records(publication, table),
                identity=publication.reference.value,
            )
        )
        return MaterializeSourceRowsetResult(
            accepted=accepted,
            created=False,
            reason="" if accepted else "Iceberg source projection differs.",
        )

    def materialize(self, publication: SourceAlignedPublication) -> MaterializeSourceRowsetResult:
        """Append or exactly replay one source publication partition."""
        table = self._catalog.source_for_interpreter(publication.rowset.interpreter.value)
        accepted, created, reason = self._projection.materialize(
            _ProjectionRequest(
                table=table,
                database=self._target.source_database,
                records=source_records(publication, table),
                identity=publication.reference.value,
            )
        )
        return MaterializeSourceRowsetResult(accepted, created, reason)


class AthenaCanonicalMaterializer:
    """Append and exactly verify candidate or successful-history Iceberg partitions."""

    def __init__(
        self,
        target: AthenaSemanticTarget,
        catalog: V2StorageCatalog,
        client: AthenaClient | None = None,
    ) -> None:
        self._projection = _AthenaProjection(target, catalog, client)
        self._target = target
        self._catalog = catalog

    def attest(self, request: CanonicalMaterializeRequest) -> CanonicalWriteResult:
        """Verify one exact canonical projection without writing or repairing it."""
        successful = request.destination is CanonicalDestination.SUCCESSFUL
        dataset = request.publication.partition.scope.dataset.value
        table = self._catalog.canonical(dataset, successful=successful)
        database = self._target.current_database if successful else self._target.candidate_database
        accepted = self._projection.attest(
            _ProjectionRequest(
                table=table,
                database=database,
                records=canonical_records(request, table),
                identity=request.publication.publication_id.value,
            )
        )
        return CanonicalWriteResult(
            accepted=accepted,
            created=False,
            row_count=len(request.rows),
            content_checksum=request.publication.content_checksum,
            reason="" if accepted else "Iceberg canonical projection differs.",
        )

    def materialize(self, request: CanonicalMaterializeRequest) -> CanonicalWriteResult:
        """Append or exactly replay one candidate/successful-history partition."""
        successful = request.destination is CanonicalDestination.SUCCESSFUL
        dataset = request.publication.partition.scope.dataset.value
        table = self._catalog.canonical(dataset, successful=successful)
        database = self._target.current_database if successful else self._target.candidate_database
        records = canonical_records(request, table)
        accepted, created, reason = self._projection.materialize(
            _ProjectionRequest(
                table=table,
                database=database,
                records=records,
                identity=request.publication.publication_id.value,
            )
        )
        return CanonicalWriteResult(
            accepted=accepted,
            created=created,
            row_count=len(request.rows),
            content_checksum=request.publication.content_checksum,
            reason=reason,
        )
