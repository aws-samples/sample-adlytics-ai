# SPDX-License-Identifier: MIT-0
"""The shape of each composed `MERGE`: its row count and distinct partitions (slice 7, M4).

A `graph_unit_set_executed` record carries `rows` and `max_partitions_per_merge` so P6(e) can
see a multi-partition statement slower than anything probe 2 measured (design §2.5 step 4,
"Re-derivation"). The composer keeps only each statement's SQL and parameters, and its module
is one of the four digested allowance inputs, so its result type does not change. This module
re-runs the composer's own packing over the same inputs, read-only: `ordered_records` per
object, `envelope_fits` at the same bound, then `pack_object_batches`. Each batch it returns is
the exact record batch the composer rendered into one `MERGE`.
"""

from __future__ import annotations

from dataclasses import dataclass

from adapters.outbound.athena.semantic_storage import (
    PhysicalRecord,
    ordered_records,
    pack_object_batches,
)
from adapters.outbound.athena.start_envelope import envelope_fits
from adapters.outbound.athena.table_attempt_statements import ComposeUnitStatementsRequest

__all__ = ["MergeShape", "merge_batches_of", "merge_shapes"]


@dataclass(frozen=True, slots=True)
class MergeShape:
    """One composed `MERGE`: how many records and how many distinct partitions it writes."""

    rows: int
    partitions: int


def merge_batches_of(
    request: ComposeUnitStatementsRequest,
) -> tuple[tuple[PhysicalRecord, ...], ...]:
    """Return the record batches `compose_unit_statements` renders, one per `MERGE`."""
    workload = request.workload
    table = workload.table
    objects = tuple(ordered_records(table, records) for _, records in workload.records_by_identity)
    fits = envelope_fits(request.workgroup, workload.database, request.max_statement_bytes)
    return tuple(pack_object_batches(table, objects, fits))


def merge_shapes(request: ComposeUnitStatementsRequest) -> tuple[MergeShape, ...]:
    """Return each composed `MERGE`'s rows and distinct partitions, in statement order.

    A partition is the table's first partition key, the column the verification reads
    (`multi_select_statement`); for a canonical table that is `publication_id`. The
    verification statement is not a `MERGE` and is never counted.
    """
    partition = request.workload.table.partition_keys[0]
    return tuple(
        MergeShape(rows=len(batch), partitions=len({record[partition] for record in batch}))
        for batch in merge_batches_of(request)
    )
