# SPDX-License-Identifier: MIT-0
"""Generate the isolated Semantic V2 source, candidate, and successful table catalog."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import NotRequired, TypedDict

from adapters.jobs.schema_generation.merge import MergedColumn, MergedTable
from adapters.jobs.schema_generation.physical_schema import (
    PhysicalColumn,
    PhysicalSchemaCompatibility,
    PhysicalSchemaError,
    compatible_column_order,
    require_append_only_physical_columns,
)
from domain.semantics.publication import CanonicalDatasetKey, PublicationGraph
from domain.semantics.source_pack import SourcePack, SourcePackRole


class ColumnBody(TypedDict):
    """One unnumbered physical column body."""

    name: str
    type: str
    nullable: bool
    comment: str
    nullMeaning: NotRequired[str]


class ColumnJson(ColumnBody):
    """One generated Glue column with explicit physical identity."""

    fieldId: int


def _column(name: str, column_type: str, comment: str) -> ColumnBody:
    return {
        "name": name,
        "type": column_type,
        "nullable": False,
        "comment": comment,
    }


def _nullable_column(name: str, column_type: str, comment: str) -> ColumnBody:
    return {
        "name": name,
        "type": column_type,
        "nullable": True,
        "comment": comment,
        "nullMeaning": "not_applicable_to_publication_envelope",
    }


def _canonical_column(column: MergedColumn) -> ColumnBody:
    projected: ColumnBody = {
        "name": column.name,
        "type": column.type.value,
        "nullable": column.nullable,
        "comment": " · ".join(column.provenance),
    }
    if column.null_meaning is not None:
        projected["nullMeaning"] = column.null_meaning
    return projected


def _numbered(columns: tuple[ColumnBody, ...] | list[ColumnBody]) -> list[ColumnJson]:
    return [ColumnJson(**column, fieldId=index) for index, column in enumerate(columns, start=1)]


# One physical source table contains one envelope record at ordinal -1 and zero or more row
# records at ordinals starting at zero. The envelope record makes a valid empty publication
# materializable without inventing a source row. Repeating immutable publication evidence on row
# records makes each record independently attributable while exact replay still compares the whole
# publication partition.
_SOURCE_COLUMNS: tuple[ColumnBody, ...] = (
    _column("source_publication_id", "string", "immutable source publication reference"),
    _column("record_kind", "string", "publication envelope or interpreted source row"),
    _column("source_row_ordinal", "bigint", "-1 for the envelope; otherwise stable row position"),
    _column("contract_id", "string", "source-pack contract identity"),
    _column("content_checksum", "string", "deterministic interpreted-rowset checksum"),
    _column("delivery_scope_json", "string", "complete qualified source delivery scope"),
    _column("extraction_start", "date", "source extraction coverage start"),
    _column("extraction_end", "date", "source extraction coverage end"),
    _column("interpreter_key", "string", "versioned source interpretation key"),
    _column("interpreter_version", "bigint", "source interpretation version"),
    _column("source_options_json", "string", "complete ordered source extraction options"),
    _nullable_column(
        "source_payload_json",
        "string",
        "typed fields, qualified identifiers, and measurement bucket in source vocabulary",
    ),
    _nullable_column("measurement_start", "date", "exact row measurement start"),
    _nullable_column("measurement_end", "date", "exact row measurement end"),
    _nullable_column("measurement_grain", "string", "declared row time grain"),
    _nullable_column("measurement_calendar", "string", "declared row business calendar"),
    _nullable_column(
        "measurement_week_starts_on",
        "bigint",
        "declared row business week start",
    ),
    _nullable_column("source_timezone", "string", "source-owned business-day timezone"),
    _column("manifest_key", "string", "immutable landed manifest key"),
    _column("manifest_version", "string", "S3 manifest VersionId"),
    _column("manifest_checksum", "string", "landed manifest checksum"),
    _column("object_key", "string", "immutable landed object key"),
    _column("object_version", "string", "S3 object VersionId"),
    _column("object_checksum", "string", "landed object checksum"),
    _column("source_revision_json", "string", "complete qualified source-owned revision"),
    _column("published_at", "timestamp", "source interpretation instant"),
)

_CANONICAL_ENVELOPE: tuple[ColumnBody, ...] = (
    _column("publication_id", "string", "immutable canonical publication identity"),
    _column("attempt_id", "string", "semantic attempt identity"),
    _column("partition_key", "string", "exact semantic partition identity"),
    _column("publisher", "string", "sole graph-authorized canonical publisher"),
    _column("fencing_token", "bigint", "lease fence authorizing this physical version"),
    _column(
        "source_publications_json",
        "string",
        "ordered immutable source publication references",
    ),
    _column("source_revision", "string", "qualified source-owned revision"),
    _column("content_checksum", "string", "canonical row-content checksum"),
    _column("rule_set_version", "string", "prepublication rule-set version"),
    _column("created_at", "timestamp", "immutable physical version creation instant"),
)


def _source_name(contract_id: str) -> str:
    return f"sa_{contract_id.replace('-', '_')}"


def _canonical_columns(
    table: MergedTable,
    compatibility: PhysicalSchemaCompatibility,
) -> list[ColumnJson]:
    envelope_names = {column["name"] for column in _CANONICAL_ENVELOPE}
    collisions = envelope_names.intersection(column.name for column in table.columns)
    if collisions:
        raise ValueError(
            f"{table.name}: canonical columns collide with V2 envelope: {sorted(collisions)}"
        )
    compatible = compatible_column_order(
        table.name,
        table.columns,
        compatibility,
    )
    return _numbered(
        [
            *_CANONICAL_ENVELOPE,
            *(_canonical_column(column) for column in compatible),
        ]
    )


def _validate_envelopes(compatibility: PhysicalSchemaCompatibility) -> None:
    canonical = tuple(
        PhysicalColumn(column["name"], column["type"], column["nullable"])
        for column in _CANONICAL_ENVELOPE
    )
    if canonical != compatibility.envelope:
        raise PhysicalSchemaError("the fixed V2 canonical envelope differs from its baseline.")
    source = tuple(
        PhysicalColumn(column["name"], column["type"], column["nullable"])
        for column in _SOURCE_COLUMNS
    )
    require_append_only_physical_columns("sourceEnvelope", source, compatibility.source_envelope)


def _require_unique_tables(tables: tuple[MergedTable, ...]) -> None:
    names = tuple(table.name for table in tables)
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ValueError(f"canonical V2 table names repeat: {duplicates}")


def _require_unique_source_names(packs: tuple[SourcePack, ...]) -> None:
    names = tuple(_source_name(pack.contract_id) for pack in packs)
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ValueError(f"source-pack names collide after physical normalization: {duplicates}")


@dataclass(frozen=True, slots=True)
class V2StorageRequest:
    """Logical declarations plus the physical compatibility baseline."""

    tables: tuple[MergedTable, ...]
    packs: tuple[SourcePack, ...]
    graph: PublicationGraph
    compatibility: PhysicalSchemaCompatibility


def v2_storage_json(request: V2StorageRequest) -> dict[str, object]:
    """Build the complete append-only Semantic V2 storage projection."""
    tables = request.tables
    packs = request.packs
    graph = request.graph
    compatibility = request.compatibility
    _require_unique_tables(tables)
    _require_unique_source_names(packs)
    _validate_envelopes(compatibility)
    source = [
        {
            "name": _source_name(pack.contract_id),
            "kind": "dimension" if pack.role is SourcePackRole.DIMENSION else "fact",
            "load": "partition_overwrite",
            "measurementBases": [],
            "writtenBy": [pack.contract_id],
            "canonicalPublisher": "source-interpreter",
            "contractId": pack.contract_id,
            "dataset": pack.dataset.value,
            "role": pack.role.value,
            "readiness": pack.readiness.value,
            "grain": ["source_publication_id", "record_kind", "source_row_ordinal"],
            "partitionKeys": ["source_publication_id"],
            "columns": _numbered(list(_SOURCE_COLUMNS)),
        }
        for pack in sorted(packs, key=lambda item: item.contract_id)
    ]
    canonical = [
        {
            "name": table.name,
            "kind": "dimension" if table.is_dimension else "fact",
            "load": "partition_overwrite",
            "measurementBases": list(table.measurement_bases),
            "writtenBy": list(table.written_by),
            "grain": ["publication_id", *table.grain],
            "partitionKeys": ["publication_id"],
            "canonicalPublisher": graph.owner_for(CanonicalDatasetKey(table.name)).value,
            "columns": _canonical_columns(table, compatibility),
        }
        for table in sorted(tables, key=lambda item: item.name)
    ]
    return {
        "version": 1,
        "storage": {
            "sourceAlignedPrefix": "source-aligned",
            "canonicalCandidatePrefix": "canonical/candidate",
            "canonicalCurrentPrefix": "canonical/current",
        },
        "sourceAlignedTables": source,
        "canonicalCandidateTables": canonical,
        "canonicalCurrentTables": copy.deepcopy(canonical),
    }
