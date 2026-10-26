# SPDX-License-Identifier: MIT-0
"""Render one table unit's exact physical records as one content-addressed JSON Lines object.

ADR-149 decisions 3 and 5. Every staged value is a JSON string or ``null``: the same text the
deleted ``VALUES`` renderer sent as a parameter or an inlined literal, so the staged MERGE's
``CAST`` reads identical text and writes identical rows (``_rendered``, pinned by T-4's
golden). The key is the SHA-256 of the body, computed by streaming over the lines without
keeping them; the body itself is rendered only inside the stager, for one unit at a time.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from datetime import UTC, date, datetime
from typing import Final

from adapters.outbound.athena.semantic_storage import (
    PhysicalRecord,
    PhysicalValue,
    UnitWorkload,
    ordered_records,
)
from adapters.outbound.v2_storage_catalog import V2Column, V2Table
from domain.shared_kernel.errors import InvariantError
from ports.outbound.unit_staging import (
    STAGING_EMPTY_IDENTITY,
    STAGING_RENDER_DIVERGED,
    StagedUnit,
)

__all__ = [
    "STAGING_ORDINAL_COLUMN",
    "iter_staged_lines",
    "render_staged_body",
    "stage_unit",
]

STAGING_ORDINAL_COLUMN: Final = "stg_ordinal"
"""The 0-based line number of each staged record, so verification counts staged rows exactly."""


def _rendered(value: PhysicalValue, column: V2Column) -> str | None:
    """Return the one text a physical value is sent as, or ``None`` for SQL NULL.

    These are the deleted ``_value``'s branches without its SQL wrapping: a timezone-aware
    timestamp in UTC with microseconds, an ISO date, ``true``/``false``, and ``str`` for every
    other value.
    """
    if value is None:
        if not column.nullable:
            raise ValueError(f"required V2 column {column.name!r} is null.")
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError(f"V2 timestamp {column.name!r} must be timezone-aware.")
        return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _line(table: V2Table, record: PhysicalRecord, ordinal: int) -> bytes:
    missing = {column.name for column in table.columns} - set(record)
    if missing:
        raise ValueError(f"{table.name}: physical record is missing {sorted(missing)}.")
    cells: dict[str, str | None] = {
        column.name: _rendered(record[column.name], column) for column in table.columns
    }
    cells[STAGING_ORDINAL_COLUMN] = str(ordinal)
    text = json.dumps(cells, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return text.encode("utf-8") + b"\n"


def iter_staged_lines(workload: UnitWorkload) -> Iterator[bytes]:
    """Yield the unit's staged lines: each identity in roster order, its records in grain order.

    A record missing a column, a null in a required column, a naive timestamp, a repeated
    grain or a lone surrogate raises ``ValueError``, as the deleted renderer did.
    """
    table = workload.table
    ordinal = 0
    for _, records in workload.records_by_identity:
        for record in ordered_records(table, records):
            yield _line(table, record, ordinal)
            ordinal += 1


def stage_unit(workload: UnitWorkload, *, max_record_bytes: int) -> StagedUnit:
    """Return the unit's staged summary in one streaming pass, keeping no line.

    A line longer than ``max_record_bytes`` (its newline included) raises ``ValueError``: one
    record stays bounded by the statement byte bound, as today (ADR-149 decision 7).
    """
    table = workload.table
    digest = hashlib.sha256()
    byte_length = 0
    ordinal = 0
    row_counts: list[tuple[str, int]] = []
    for identity, records in workload.records_by_identity:
        if not records:
            raise InvariantError(
                STAGING_EMPTY_IDENTITY,
                "a staged unit identity carries no records.",
                context={"table": table.name},
            )
        for record in ordered_records(table, records):
            line = _line(table, record, ordinal)
            if len(line) > max_record_bytes:
                raise ValueError(
                    f"{table.name}: one record's staged line exceeds {max_record_bytes} bytes."
                )
            digest.update(line)
            byte_length += len(line)
            ordinal += 1
        row_counts.append((identity, len(records)))
    return StagedUnit(
        table=table.name,
        staging_key=digest.hexdigest(),
        byte_length=byte_length,
        row_counts=tuple(row_counts),
    )


def render_staged_body(workload: UnitWorkload, staged: StagedUnit) -> bytes:
    """Render the unit's body once, refusing any divergence from its staged summary."""
    body = b"".join(iter_staged_lines(workload))
    if (
        workload.table.name != staged.table
        or len(body) != staged.byte_length
        or hashlib.sha256(body).hexdigest() != staged.staging_key
    ):
        raise InvariantError(
            STAGING_RENDER_DIVERGED,
            "a re-rendered staged body differs from its staging key.",
            context={"table": workload.table.name},
        )
    return body
