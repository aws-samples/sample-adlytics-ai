# SPDX-License-Identifier: MIT-0
"""The shape of each composed `MERGE`: its row count and distinct partitions (slice 7, M4).

A `graph_unit_set_executed` record carries `rows` and `max_partitions_per_merge` so P6(e) can
see a multi-partition statement slower than anything probe 2 measured (design §2.5 step 4,
"Re-derivation"). Under ADR-149 a unit composes exactly one staged `MERGE` over every record
of every identity, so the one shape counts the workload itself: no packing to re-run.
"""

from __future__ import annotations

from dataclasses import dataclass

from adapters.outbound.athena.table_attempt_statements import ComposeUnitStatementsRequest

__all__ = ["MergeShape", "merge_shapes"]


@dataclass(frozen=True, slots=True)
class MergeShape:
    """One composed `MERGE`: how many records and how many distinct partitions it writes."""

    rows: int
    partitions: int


def merge_shapes(request: ComposeUnitStatementsRequest) -> tuple[MergeShape, ...]:
    """Return the unit's one `MERGE` shape: every record, and every declared identity.

    A partition is the table's first partition key, the column the verification groups by;
    for a canonical table that is `publication_id`. The verification statement is not a
    `MERGE` and is never counted.
    """
    objects = request.workload.records_by_identity
    return (
        MergeShape(
            rows=sum(len(records) for _, records in objects),
            partitions=len(objects),
        ),
    )
