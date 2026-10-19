# SPDX-License-Identifier: MIT-0
"""Govern append-only physical Iceberg identity for existing canonical tables."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

import yaml

from adapters.jobs.schema_generation.merge import MergedColumn

COMPATIBILITY_FILE = Path("contracts/shared/iceberg-schema-compatibility.yaml")
MIGRATION_NULL_MEANING = "not_yet_available"


class PhysicalSchemaError(ValueError):
    """A generated physical schema would break an existing Iceberg identity."""


@dataclass(frozen=True, slots=True)
class PhysicalColumn:
    """One immutable historical physical column identity."""

    name: str
    type: str
    nullable: bool


@dataclass(frozen=True, slots=True)
class PhysicalSchemaCompatibility:
    """Fixed envelopes and per-table historical column order."""

    envelope: tuple[PhysicalColumn, ...]
    source_envelope: tuple[PhysicalColumn, ...]
    tables: dict[str, tuple[PhysicalColumn, ...]]


def _column(value: object, *, where: str) -> PhysicalColumn:
    if not isinstance(value, dict):
        raise PhysicalSchemaError(f"{where} must be a mapping.")
    raw = cast("dict[object, object]", value)
    name = raw.get("name")
    column_type = raw.get("type")
    nullable = raw.get("nullable")
    if not isinstance(name, str) or not name:
        raise PhysicalSchemaError(f"{where}.name must be non-empty text.")
    if not isinstance(column_type, str) or not column_type:
        raise PhysicalSchemaError(f"{where}.{name}.type must be non-empty text.")
    if not isinstance(nullable, bool):
        raise PhysicalSchemaError(f"{where}.{name}.nullable must be boolean.")
    return PhysicalColumn(name, column_type, nullable)


def _columns(value: object, *, where: str) -> tuple[PhysicalColumn, ...]:
    if not isinstance(value, list):
        raise PhysicalSchemaError(f"{where} must be a list.")
    raw_items = cast("list[object]", value)
    columns = tuple(
        _column(item, where=f"{where}[{index}]") for index, item in enumerate(raw_items)
    )
    names = tuple(column.name for column in columns)
    if not columns or len(set(names)) != len(names):
        raise PhysicalSchemaError(f"{where} must contain unique columns.")
    return columns


def load_physical_schema_compatibility(root: Path) -> PhysicalSchemaCompatibility:
    """Load the committed historical baseline without accepting partial shapes."""
    path = root / COMPATIBILITY_FILE
    raw: object = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict):
        raise PhysicalSchemaError(f"{path} must contain a mapping.")
    document = cast("dict[object, object]", raw)
    if document.get("version") != 1:
        raise PhysicalSchemaError(f"{path} requires version 1.")
    canonical_envelope = _columns(document.get("canonicalEnvelope"), where="canonicalEnvelope")
    source_envelope = _columns(document.get("sourceEnvelope"), where="sourceEnvelope")
    table_value = document.get("tables")
    if not isinstance(table_value, dict):
        raise PhysicalSchemaError("tables must be a mapping.")
    tables = {
        str(name): _columns(value, where=f"tables.{name}")
        for name, value in cast("dict[object, object]", table_value).items()
    }
    if not tables:
        raise PhysicalSchemaError("tables must not be empty.")
    return PhysicalSchemaCompatibility(canonical_envelope, source_envelope, tables)


def require_append_only_physical_columns(
    schema_name: str,
    current: tuple[PhysicalColumn, ...],
    baseline: tuple[PhysicalColumn, ...],
) -> None:
    """Require historical ordinals to remain a prefix and additions to be nullable."""
    if len(current) < len(baseline):
        raise PhysicalSchemaError(f"{schema_name}: an existing physical column was removed.")
    for field_id, historical in enumerate(baseline, start=1):
        column = current[field_id - 1]
        if column.name != historical.name:
            raise PhysicalSchemaError(
                f"{schema_name} field {field_id}: {historical.name} was removed, renamed, "
                f"or reordered as {column.name}."
            )
        if column.type != historical.type:
            raise PhysicalSchemaError(
                f"{schema_name}.{historical.name}: physical type changed from "
                f"{historical.type} to {column.type}."
            )
        if historical.nullable and not column.nullable:
            raise PhysicalSchemaError(
                f"{schema_name}.{historical.name}: physical nullability cannot be tightened."
            )
    required_additions = [column.name for column in current[len(baseline) :] if not column.nullable]
    if required_additions:
        raise PhysicalSchemaError(
            f"{schema_name}: new physical columns must append nullable: {required_additions}."
        )


def compatible_column_order(
    table_name: str,
    current: tuple[MergedColumn, ...],
    compatibility: PhysicalSchemaCompatibility,
) -> tuple[MergedColumn, ...]:
    """Preserve historical identity and append migration-nullable new fields."""
    baseline = compatibility.tables.get(table_name)
    if baseline is None:
        return current
    by_name = {column.name: column for column in current}
    if len(by_name) != len(current):
        raise PhysicalSchemaError(f"{table_name}: current schema repeats a column name.")
    ordered: list[MergedColumn] = []
    for historical in baseline:
        column = by_name.pop(historical.name, None)
        if column is None:
            raise PhysicalSchemaError(
                f"{table_name}.{historical.name}: an existing physical column was "
                "removed or renamed."
            )
        if column.type.value != historical.type:
            raise PhysicalSchemaError(
                f"{table_name}.{historical.name}: physical type changed from "
                f"{historical.type} to {column.type.value}."
            )
        if historical.nullable and not column.nullable:
            raise PhysicalSchemaError(
                f"{table_name}.{historical.name}: physical nullability cannot be tightened."
            )
        ordered.append(replace(column, nullable=historical.nullable or column.nullable))
    for column in current:
        if column.name in by_name:
            ordered.append(
                replace(
                    column,
                    nullable=True,
                    null_meaning=column.null_meaning or MIGRATION_NULL_MEANING,
                )
            )
    return tuple(ordered)
