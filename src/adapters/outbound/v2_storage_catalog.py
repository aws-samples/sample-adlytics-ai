# SPDX-License-Identifier: MIT-0
"""Strict runtime loader for the generated Semantic V2 physical catalog."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

__all__ = [
    "STAGING_RESERVED_COLUMNS",
    "V2Column",
    "V2StorageCatalog",
    "V2Table",
    "load_v2_storage_catalog",
]

STAGING_RESERVED_COLUMNS: Final = frozenset({"stg_ordinal", "staging_key"})
"""Names the staging tables add to every catalog column (ADR-149), so no column may use one."""


@dataclass(frozen=True, slots=True)
class V2Column:
    """One generated physical column and its exact Athena type/nullability."""

    name: str
    type: str
    nullable: bool


@dataclass(frozen=True, slots=True)
class V2Table:
    """Generated physical table identity, schema, grain, and optional source contract."""

    name: str
    columns: tuple[V2Column, ...]
    grain: tuple[str, ...]
    partition_keys: tuple[str, ...]
    contract_id: str | None = None

    def __post_init__(self) -> None:
        names = tuple(column.name for column in self.columns)
        if len(set(names)) != len(names):
            raise ValueError(f"V2 table {self.name!r} repeats a column.")
        if not set(self.grain) <= set(names) or not set(self.partition_keys) <= set(names):
            raise ValueError(f"V2 table {self.name!r} grain/partition references an absent column.")


@dataclass(frozen=True, slots=True)
class V2StorageCatalog:
    """All generated source/candidate/current tables indexed by semantic identity."""

    source_by_contract: dict[str, V2Table]
    candidate_by_dataset: dict[str, V2Table]
    current_by_dataset: dict[str, V2Table]

    def source_for_interpreter(self, interpreter: str) -> V2Table:
        """Resolve the exact source table from the generated source-pack interpreter key."""
        matches = tuple(
            table
            for contract, table in self.source_by_contract.items()
            if interpreter == f"source_pack.{contract.replace('-', '_')}"
        )
        if len(matches) != 1:
            raise ValueError(f"source interpreter {interpreter!r} has no unique generated table.")
        return matches[0]

    def canonical(self, dataset: str, *, successful: bool) -> V2Table:
        """Return candidate or successful-history table for one canonical dataset."""
        selected = self.current_by_dataset if successful else self.candidate_by_dataset
        try:
            return selected[dataset]
        except KeyError:
            raise ValueError(f"canonical dataset {dataset!r} has no generated V2 table.") from None


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} is not an object.")
    return {str(key): item for key, item in cast("dict[object, object]", value).items()}


def _strings(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{label} is not a string array.")
    items = cast("list[object]", value)
    if not all(isinstance(item, str) and item for item in items):
        raise ValueError(f"{label} is not a string array.")
    return tuple(cast("list[str]", items))


def _table(value: object, label: str, *, source: bool) -> V2Table:
    item = _object(value, label)
    name = item.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError(f"{label} has no table name.")
    raw_columns = item.get("columns")
    if not isinstance(raw_columns, list) or not raw_columns:
        raise ValueError(f"{label} has no columns.")
    columns: list[V2Column] = []
    for index, raw in enumerate(cast("list[object]", raw_columns)):
        column = _object(raw, f"{label}.columns[{index}]")
        column_name = column.get("name")
        column_type = column.get("type")
        nullable = column.get("nullable")
        if not isinstance(column_name, str) or not column_name:
            raise ValueError(f"{label}.columns[{index}] has no name.")
        if column_name in STAGING_RESERVED_COLUMNS:
            raise ValueError(f"{label}.{column_name} is a name reserved for staging.")
        if not isinstance(column_type, str) or not column_type:
            raise ValueError(f"{label}.{column_name} has no type.")
        if not isinstance(nullable, bool):
            raise ValueError(f"{label}.{column_name} has no nullable flag.")
        columns.append(V2Column(column_name, column_type, nullable))
    contract = item.get("contractId")
    if source and (not isinstance(contract, str) or not contract):
        raise ValueError(f"{label} has no source contract id.")
    return V2Table(
        name=name,
        columns=tuple(columns),
        grain=_strings(item.get("grain"), f"{label}.grain"),
        partition_keys=_strings(item.get("partitionKeys"), f"{label}.partitionKeys"),
        contract_id=cast("str", contract) if source else None,
    )


def _area(root: dict[str, object], key: str, *, source: bool) -> tuple[V2Table, ...]:
    value = root.get(key)
    if not isinstance(value, list) or not value:
        raise ValueError(f"generated V2 catalog has no {key}.")
    tables = tuple(
        _table(raw, f"{key}[{index}]", source=source)
        for index, raw in enumerate(cast("list[object]", value))
    )
    names = tuple(table.name for table in tables)
    if len(set(names)) != len(names):
        raise ValueError(f"generated V2 catalog repeats a table in {key}.")
    return tables


def load_v2_storage_catalog(path: Path) -> V2StorageCatalog:
    """Load the generated V2 catalog and require candidate/current schema parity."""
    root = _object(json.loads(path.read_text()), "generated V2 catalog")
    if root.get("version") != 1:
        raise ValueError("generated V2 catalog requires version 1.")
    source = _area(root, "sourceAlignedTables", source=True)
    candidate = _area(root, "canonicalCandidateTables", source=False)
    current = _area(root, "canonicalCurrentTables", source=False)
    candidate_by_name = {table.name: table for table in candidate}
    current_by_name = {table.name: table for table in current}
    if candidate_by_name.keys() != current_by_name.keys():
        raise ValueError("candidate/current V2 table sets differ.")
    for name, candidate_table in candidate_by_name.items():
        current_table = current_by_name[name]
        if (
            candidate_table.columns != current_table.columns
            or candidate_table.grain != current_table.grain
            or candidate_table.partition_keys != current_table.partition_keys
        ):
            raise ValueError(f"candidate/current V2 schemas differ for {name!r}.")
    return V2StorageCatalog(
        source_by_contract={cast("str", table.contract_id): table for table in source},
        candidate_by_dataset=candidate_by_name,
        current_by_dataset=current_by_name,
    )
