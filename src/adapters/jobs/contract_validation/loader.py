# SPDX-License-Identifier: MIT-0
"""Read contracts and the AMC column registry off disk into domain objects.

The adapter half of contract validation: this module owns YAML, paths and file layout,
and the rules that judge what it produces live in `domain/data_contracts`. That split is
what lets every rule be unit tested without a fixture file.

YAML is parsed only through `adapters.outbound.repository_yaml`, the safe C loader, never the
full loader: that constructs arbitrary Python objects and `contracts/` is read by a Lambda.

Every value is narrowed from `object` before use. A parsed document is untyped, so without
narrowing a malformed contract reaches a dataclass as the wrong type and fails somewhere
unrelated — the `_as_*` helpers turn that into one message naming the file and the key.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

from adapters.outbound import repository_yaml
from domain.data_contracts.contract import (
    CohortGrain,
    CohortTriangle,
    ContractError,
    DataContract,
    ExpectedAxis,
    ExpectedCells,
    ExpectedCellSet,
    FieldMapping,
    MarketplaceFilterCheck,
    NullMeaning,
    SourceFormat,
    SuppressionColumns,
)
from domain.data_contracts.validation import ColumnRegistry, RegisteredColumn
from domain.shared_kernel.provenance import MeasurementBasis

CONTRACTS_DIR = "contracts"
REGISTRY_FILE = "shared/amc-columns.yaml"

# Files under contracts/shared/ are reference data, not contracts. Listing them rather
# than pattern-matching keeps a new shared file from silently being read as a contract.
SHARED_FILES = frozenset(
    {
        "amc-columns.yaml",
        "amc-instance.yaml",
        "attribution-models.yaml",
        "audience-segment-providers.yaml",
        "canonical-publishers.yaml",
        "commerce-identities.yaml",
        "fixture-provenance.yaml",
        "iceberg-schema-compatibility.yaml",
        "media-taxonomy.yaml",
        "metric-dictionary.yaml",
        "synthetic-entity-catalog.yaml",
    }
)

type Mapping = dict[str, object]


def _load_yaml_mapping(path: Path) -> Mapping:
    """Parse a YAML file, requiring a mapping at the top level."""
    parsed = repository_yaml.safe_load(path.read_text())
    if not isinstance(parsed, dict):
        raise ContractError(f"{path}: expected a mapping at the top level.")
    return cast("Mapping", parsed)


def _require(mapping: Mapping, key: str, *, where: str) -> object:
    """Fetch a required key, naming the file when it is absent."""
    if key not in mapping:
        raise ContractError(f"{where}: required key {key!r} is missing.")
    return mapping[key]


def _as_str(value: object, *, key: str, where: str) -> str:
    if not isinstance(value, str):
        raise ContractError(f"{where}: {key} must be a string, got {type(value).__name__}.")
    return value


def _as_int(value: object, *, key: str, where: str) -> int:
    # bool is a subclass of int, and `nullable: 1` should not read as an integer field.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractError(f"{where}: {key} must be an integer, got {type(value).__name__}.")
    return value


def _as_str_tuple(value: object, *, key: str, where: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ContractError(f"{where}: {key} must be a list, got {type(value).__name__}.")
    return tuple(_as_str(item, key=f"{key}[]", where=where) for item in cast("list[object]", value))


def _as_mapping(value: object, *, key: str, where: str) -> Mapping:
    if not isinstance(value, dict):
        raise ContractError(f"{where}: {key} must be a mapping, got {type(value).__name__}.")
    return cast("Mapping", value)


def _optional_int(mapping: Mapping, key: str, *, where: str) -> int | None:
    value = mapping.get(key)
    return None if value is None else _as_int(value, key=key, where=where)


def _optional_str(mapping: Mapping, key: str, *, where: str) -> str | None:
    value = mapping.get(key)
    return None if value is None else _as_str(value, key=key, where=where)


def _field_from(raw: Mapping, *, where: str) -> FieldMapping:
    """Build one field mapping, converting the null meaning to its enum."""
    null_meaning_raw = _optional_str(raw, "null_meaning", where=where)
    try:
        null_meaning = NullMeaning(null_meaning_raw) if null_meaning_raw else None
    except ValueError as error:
        raise ContractError(
            f"{where}: null_meaning {null_meaning_raw!r} is not one of "
            f"{[m.value for m in NullMeaning]}. Absent is not one of the null meanings."
        ) from error

    return FieldMapping(
        source_path=_optional_str(raw, "source_path", where=where) or "",
        target_column=_as_str(
            _require(raw, "target_column", where=where), key="target_column", where=where
        ),
        scale_divisor=_optional_int(raw, "scale_divisor", where=where),
        currency_column=_optional_str(raw, "currency_column", where=where),
        null_meaning=null_meaning,
        nullable=bool(raw.get("nullable", False)),
        non_additive=bool(raw.get("non_additive", False)),
        derived_from=_as_str_tuple(raw.get("derived_from", []), key="derived_from", where=where),
        from_extraction_option=bool(raw.get("from_extraction_option", False)),
        literal_value=_optional_str(raw, "literal_value", where=where),
        value_domain=(
            _as_str_tuple(raw["value_domain"], key="value_domain", where=where)
            if "value_domain" in raw
            else ()
        ),
        assigned_by_load=bool(raw.get("assigned_by_load", False)),
        target_type=_optional_str(raw, "target_type", where=where),
        present_from_pack_version=_optional_int(raw, "present_from_pack_version", where=where),
        served_where_published=bool(raw.get("served_where_published", False)),
    )


def contract_from_mapping(raw: Mapping, *, where: str) -> DataContract:
    """Build a contract from a parsed YAML mapping.

    Every enum conversion reports the legal values on failure, because the most common
    authoring error is a plausible-looking value that is not in the set — `retail` for
    `retail_actual`, or `utc` for `UTC`.
    """
    raw_format = _require(raw, "source_format", where=where)
    try:
        source_format = SourceFormat(raw_format)
    except ValueError as error:
        raise ContractError(
            f"{where}: source_format {raw_format!r} is not one of "
            f"{[f.value for f in SourceFormat]}."
        ) from error

    raw_basis = _require(raw, "measurement_basis", where=where)
    try:
        basis = MeasurementBasis(raw_basis)
    except ValueError as error:
        raise ContractError(
            f"{where}: measurement_basis {raw_basis!r} is not one of "
            f"{[b.value for b in MeasurementBasis]}. It is stamped as a literal and never "
            "read from a source."
        ) from error

    suppression_raw = raw.get("suppression")
    suppression: SuppressionColumns | None = None
    if suppression_raw is not None:
        block = _as_mapping(suppression_raw, key="suppression", where=where)
        suppression = SuppressionColumns(
            discriminator_column=_as_str(
                _require(block, "discriminator_column", where=where),
                key="suppression.discriminator_column",
                where=where,
            ),
            reason_column=_as_str(
                _require(block, "reason_column", where=where),
                key="suppression.reason_column",
                where=where,
            ),
            distinct_user_count_column=_as_str(
                _require(block, "distinct_user_count_column", where=where),
                key="suppression.distinct_user_count_column",
                where=where,
            ),
        )

    fields_raw = _require(raw, "fields", where=where)
    if not isinstance(fields_raw, list):
        raise ContractError(f"{where}: fields must be a list.")
    fields = tuple(
        _field_from(_as_mapping(item, key="fields[]", where=where), where=where)
        for item in cast("list[object]", fields_raw)
    )

    return DataContract(
        contract_id=_as_str(
            _require(raw, "contract_id", where=where), key="contract_id", where=where
        ),
        contract_version=_as_int(
            _require(raw, "contract_version", where=where), key="contract_version", where=where
        ),
        source_system=_as_str(
            _require(raw, "source_system", where=where), key="source_system", where=where
        ),
        source_dataset=_as_str(
            _require(raw, "source_dataset", where=where), key="source_dataset", where=where
        ),
        source_format=source_format,
        target_table=_as_str(
            _require(raw, "target_table", where=where), key="target_table", where=where
        ),
        grain=_as_str_tuple(_require(raw, "grain", where=where), key="grain", where=where),
        partition_keys=_as_str_tuple(
            _require(raw, "partition_keys", where=where), key="partition_keys", where=where
        ),
        measurement_basis=basis,
        source_timezone=_as_str(
            _require(raw, "source_timezone", where=where), key="source_timezone", where=where
        ),
        ecosystem_key=_as_str(
            _require(raw, "ecosystem_key", where=where), key="ecosystem_key", where=where
        ),
        platform_key=_as_str(
            _require(raw, "platform_key", where=where), key="platform_key", where=where
        ),
        fields=fields,
        restatement_window_days=_as_int(
            _require(raw, "restatement_window_days", where=where),
            key="restatement_window_days",
            where=where,
        ),
        staleness_threshold_hours=_as_int(
            _require(raw, "staleness_threshold_hours", where=where),
            key="staleness_threshold_hours",
            where=where,
        ),
        source_tables=_as_str_tuple(raw.get("source_tables", []), key="source_tables", where=where),
        suppression=suppression,
        grain_feasibility_note=str(raw.get("grain_feasibility_note", "")),
        key_unique_among_unsuppressed_only=bool(
            raw.get("key_unique_among_unsuppressed_only", False)
        ),
        currency_agreement=_as_str_tuple(
            raw.get("currency_agreement", []), key="currency_agreement", where=where
        ),
        tags=_as_str_tuple(raw.get("tags", []), key="tags", where=where),
        expected_cells=_expected_cells(raw.get("expected_cells"), where=where),
        currency_minimums=_currency_minimums(raw.get("currency_minimums"), where=where),
        marketplace_filter_check=_marketplace_filter_check(
            raw.get("marketplace_filter_check"), where=where
        ),
        repeated_totals=_repeated_totals(raw.get("repeated_totals"), where=where),
        variant_follows=(
            None
            if raw.get("variant_follows") is None
            else _as_str(raw["variant_follows"], key="variant_follows", where=where)
        ),
    )


_EXPECTED_SET_KEYS = frozenset({"slices", "cells", "open_slices", "measures", "serves", "cohorts"})
_COHORT_KEYS = frozenset({"column", "offset", "grain", "follow_ups"})


def _cohort_triangle(raw: object, *, where: str) -> CohortTriangle | None:
    """Read a cohort vintage's triangle (packs 14-15): its columns, grain and follow-ups."""
    if raw is None:
        return None
    block = _as_mapping(raw, key="expected_cells.cohorts", where=where)
    if frozenset(block) != _COHORT_KEYS:
        raise ContractError(f"{where}: expected_cells.cohorts takes {sorted(_COHORT_KEYS)}.")
    grain = _as_str(block["grain"], key="cohorts.grain", where=where)
    try:
        cohort_grain = CohortGrain(grain)
    except ValueError as error:
        raise ContractError(
            f"{where}: cohorts.grain is month or quarter, not {grain!r}."
        ) from error
    return CohortTriangle(
        column=_as_str(block["column"], key="cohorts.column", where=where),
        offset=_as_str(block["offset"], key="cohorts.offset", where=where),
        grain=cohort_grain,
        follow_ups=_as_int(block["follow_ups"], key="cohorts.follow_ups", where=where),
    )


def _repeated_totals(raw: object, *, where: str) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Read `repeated_totals`: each repeated delivered total and the columns one copy shares."""
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ContractError(f"{where}: repeated_totals must be a list.")
    totals: list[tuple[str, tuple[str, ...]]] = []
    for item in cast("list[object]", raw):
        block = _as_mapping(item, key="repeated_totals[]", where=where)
        if set(block) != {"column", "keyed_by"}:
            raise ContractError(f"{where}: a repeated total takes column and keyed_by.")
        totals.append(
            (
                _as_str(block["column"], key="repeated_totals.column", where=where),
                _as_str_tuple(block["keyed_by"], key="repeated_totals.keyed_by", where=where),
            )
        )
    return tuple(totals)


def _expected_axis(raw: object, *, where: str) -> ExpectedAxis:
    block = _as_mapping(raw, key="expected_cells axis", where=where)
    if set(block) - {"column", "values", "from"}:
        raise ContractError(f"{where}: an expected-cells axis takes column and values or from.")
    source = block.get("from")
    if source is not None and source != "delivery":
        raise ContractError(f"{where}: an expected-cells axis reads `from: delivery` only.")
    return ExpectedAxis(
        column=_as_str(_require(block, "column", where=where), key="column", where=where),
        values=_as_str_tuple(block.get("values", []), key="values", where=where),
        from_delivery=source == "delivery",
    )


def _expected_set(raw: object, *, where: str) -> ExpectedCellSet:
    block = _as_mapping(raw, key="expected_cells set", where=where)
    if set(block) - _EXPECTED_SET_KEYS:
        raise ContractError(f"{where}: an expected-cells set takes {sorted(_EXPECTED_SET_KEYS)}.")

    def axes(key: str) -> tuple[ExpectedAxis, ...]:
        values = block.get(key, [])
        if not isinstance(values, list):
            raise ContractError(f"{where}: expected_cells.{key} must be a list.")
        return tuple(_expected_axis(item, where=where) for item in cast("list[object]", values))

    return ExpectedCellSet(
        cells=axes("cells"),
        slices=axes("slices"),
        open_slices=_as_str_tuple(block.get("open_slices", []), key="open_slices", where=where),
        measures=_as_str_tuple(block.get("measures", []), key="measures", where=where),
        serves=_as_str_tuple(block.get("serves", []), key="serves", where=where),
        cohorts=_cohort_triangle(block.get("cohorts"), where=where),
    )


def _expected_cells(raw: object, *, where: str) -> ExpectedCells | None:
    """Read `expected_cells`: the base pack's set, then each declared variant's sets (plan D8)."""
    if raw is None:
        return None
    block = _as_mapping(raw, key="expected_cells", where=where)
    if set(block) - _EXPECTED_SET_KEYS - {"variants", "sets"}:
        raise ContractError(f"{where}: expected_cells takes a set's keys, or sets, and variants.")
    base = {key: value for key, value in block.items() if key not in {"variants", "sets"}}
    variants_raw = _as_mapping(block.get("variants", {}), key="variants", where=where)
    variants: list[tuple[str, tuple[ExpectedCellSet, ...]]] = []
    for name, sets in variants_raw.items():
        if not isinstance(sets, list):
            raise ContractError(f"{where}: expected_cells.variants.{name} must be a list of sets.")
        variants.append(
            (
                _as_str(name, key="variant", where=where),
                tuple(_expected_set(item, where=where) for item in cast("list[object]", sets)),
            )
        )
    listed = block.get("sets")
    if listed is None:
        return ExpectedCells(sets=(_expected_set(base, where=where),), variants=tuple(variants))
    # Several base sets (packs 10-12: a required profiled_total row and the open segment rows,
    # each carrying its own measures) are listed, as a variant's are, never beside one set's keys.
    if base or not isinstance(listed, list) or not listed:
        raise ContractError(f"{where}: expected_cells.sets is a nonempty list of sets, alone.")
    return ExpectedCells(
        sets=tuple(_expected_set(item, where=where) for item in cast("list[object]", listed)),
        variants=tuple(variants),
    )


def _currency_minimums(raw: object, *, where: str) -> tuple[tuple[str, str], ...]:
    if raw is None:
        return ()
    block = _as_mapping(raw, key="currency_minimums", where=where)
    return tuple(
        (
            _as_str(name, key="currency_minimums key", where=where),
            _as_str(value, key=f"currency_minimums.{name}", where=where),
        )
        for name, value in block.items()
    )


def _marketplace_filter_check(raw: object, *, where: str) -> MarketplaceFilterCheck | None:
    if raw is None:
        return None
    block = _as_mapping(raw, key="marketplace_filter_check", where=where)
    if set(block) != {"slice_column", "slice_value", "reached", "outcome"}:
        raise ContractError(
            f"{where}: marketplace_filter_check takes slice_column, slice_value, reached, outcome."
        )
    return MarketplaceFilterCheck(
        **{key: _as_str(value, key=key, where=where) for key, value in block.items()}
    )


def load_registry(root: Path) -> ColumnRegistry:
    """Read contracts/shared/amc-columns.yaml into the domain's registry type.

    A `status: disputed` entry is carried through rather than filtered out, because the
    gate must refuse a contract that uses one — silently dropping it would let a disputed
    column through as merely unknown.
    """
    path = root / CONTRACTS_DIR / REGISTRY_FILE
    if not path.exists():
        raise ContractError(f"the AMC column registry is missing at {path}.")

    where = str(path)
    raw = _load_yaml_mapping(path)
    tables = _as_mapping(raw.get("tables") or {}, key="tables", where=where)

    columns: dict[tuple[str, str], RegisteredColumn] = {}
    inherits: dict[str, str] = {}

    for table_name, table_raw in tables.items():
        table = _as_mapping(table_raw, key=f"tables.{table_name}", where=where)
        parent = _optional_str(table, "inherits_columns_from", where=where)
        if parent:
            inherits[table_name] = parent

        table_columns = _as_mapping(
            table.get("columns") or {}, key=f"tables.{table_name}.columns", where=where
        )
        for column_name, column_raw in table_columns.items():
            column = _as_mapping(
                column_raw, key=f"tables.{table_name}.columns.{column_name}", where=where
            )
            columns[(table_name, column_name)] = RegisteredColumn(
                name=column_name,
                table=table_name,
                threshold=str(column.get("threshold", "")),
                # Amazon's published type, verbatim. The input to type resolution in
                # domain/data_contracts/schema.py.
                source_type=_optional_str(column, "type", where=where),
                disputed=column.get("status") == "disputed",
                monetary=bool(column.get("monetary", False)),
                scale_divisor=_optional_int(column, "scale_divisor", where=where),
                deprecated_for_use=bool(column.get("deprecated_for_use", False)),
                preferred_alternative=_preferred_for(column_name, table_columns),
                kind=_optional_str(column, "kind", where=where),
            )

    return ColumnRegistry(columns=columns, known_tables=frozenset(tables), inherits=inherits)


def _preferred_for(column_name: str, table_columns: Mapping) -> str | None:
    """Find the column marked `preferred` that supersedes a deprecated one.

    The registry marks the trap column `deprecated_for_use` and its replacement
    `preferred`, without linking them. Pairing them by shared prefix is enough for the
    only case that exists — `campaign_id` against `campaign_id_string` — and returns None
    rather than guessing when no single candidate matches.
    """
    candidates = [
        name
        for name, column in table_columns.items()
        if isinstance(column, dict)
        and cast("Mapping", column).get("preferred")
        and name.startswith(column_name)
    ]
    return candidates[0] if len(candidates) == 1 else None


def load_contracts(root: Path) -> list[tuple[Path, DataContract]]:
    """Read every contract under contracts/, excluding the shared reference files.

    Returns paths alongside contracts so a finding can name the file a human must open.
    """
    contracts_root = root / CONTRACTS_DIR
    if not contracts_root.exists():
        raise ContractError(f"{contracts_root} does not exist.")

    found: list[tuple[Path, DataContract]] = []
    for path in sorted(contracts_root.rglob("*.yaml")):
        if (
            path.name in SHARED_FILES
            or path.is_relative_to(contracts_root / "insights")
            or path.is_relative_to(contracts_root / "observability")
            # The operational data-flow registry is a movement declaration with its own
            # strict loader and gate, not a data contract.
            or path.is_relative_to(contracts_root / "operations")
            # The governed alert rules (ADR-108) have their own loader and validator.
            or path.is_relative_to(contracts_root / "alerts")
        ):
            continue
        raw = _load_yaml_mapping(path)
        found.append((path, contract_from_mapping(raw, where=str(path))))
    return found
