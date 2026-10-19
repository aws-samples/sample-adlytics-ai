# SPDX-License-Identifier: MIT-0
"""Render a Pydantic model per contract: the row shape a reader must produce.

The second projection of `TargetColumn`, alongside the Iceberg table definitions in
`merge.py`. Both read the same list, so a curated column cannot exist in one and not the
other (docs/capabilities/data-layer/low-level-design.md §6).

**These validate the curated row, not the delivered document.** A reader's job is to turn a
source file into canonical rows, and this is the assertion that it did. Source-level checks
are a different thing and live elsewhere: the load assertions in
docs/capabilities/load-path/low-level-design.md §6 cover header presence, currency agreement
and control totals, and the `data-contract-definition` steering requires source parsing to be
*additive-tolerant* — so a strict model pointed at a delivered document would reject exactly
the field-added-without-notice case both sources are known for.

**One model per contract, never per table.** `fct_ad_exposure` is written by four contracts
and 10 of its 17 columns are nullable *because* only some contracts write them. A per-table
model would demand columns a single contract has no way to produce.

**Coercion is the point, not a side effect.** A CSV reader hands back strings for everything.
Pydantic in its default lax mode turns `"137"` into `int`, `"2026-08-13"` into `date` and
`"11.3700"` into `Decimal` — and for the money case it does it *without* a float
intermediary, so no cent is lost to binary representation. The model is doing the typing
work, not merely checking it afterwards.
"""

from __future__ import annotations

import textwrap

from domain.data_contracts.contract import DataContract
from domain.data_contracts.schema import CanonicalType, ColumnRegistry, TargetColumn, target_schema

# Canonical type to the Python annotation that validates it. `decimal(38,4)` maps to a bare
# `Decimal` with the precision expressed as field constraints rather than in the annotation,
# because `condecimal` is deprecated in Pydantic v2 in favour of `Field`.
LINE_LIMIT = 100
"""Matches ruff's configured limit. Generated code is linted like everything else."""

PYTHON_TYPES: dict[CanonicalType, str] = {
    CanonicalType.BOOLEAN: "bool",
    CanonicalType.INT: "int",
    CanonicalType.BIGINT: "int",
    CanonicalType.DOUBLE: "float",
    CanonicalType.DECIMAL: "Decimal",
    CanonicalType.DATE: "date",
    CanonicalType.TIMESTAMP: "datetime",
    CanonicalType.STRING: "str",
}

# What each annotation needs imported. Emitted only when used, so the generated file has no
# unused import for ruff to reject.
IMPORTS: dict[str, tuple[str, str]] = {
    "Decimal": ("decimal", "Decimal"),
    "date": ("datetime", "date"),
    "datetime": ("datetime", "datetime"),
}

HEADER = '''# SPDX-License-Identifier: MIT-0
"""GENERATED FROM contracts/*/*.yaml BY make generate-schemas. Do not edit.

One model per data contract: the canonical row a reader must produce for that contract.
`make check-schemas` regenerates this and fails on any diff, so an edit here is reverted
rather than kept. Change the contract instead.

Validates the curated row, not the delivered document — see
adapters/jobs/schema_generation/models.py for why those are different jobs, and
docs/capabilities/data-layer/low-level-design.md §6 for what else the contracts generate.
"""'''


def class_name_for(contract_id: str) -> str:
    """Turn `vendor-sales-by-asin` into `VendorSalesByAsinRow`.

    Derived from the contract id rather than the target table, because a table written by
    four contracts would otherwise give four classes the same name.
    """
    parts = [part for part in contract_id.replace("_", "-").split("-") if part]
    return "".join(part[:1].upper() + part[1:] for part in parts) + "Row"


def _field_line(column: TargetColumn) -> str:
    """Render one field, with the contract's own provenance as its description."""
    annotation = PYTHON_TYPES[column.type]
    if column.nullable:
        annotation = f"{annotation} | None"

    # The description carries provenance into the runtime object, so a validation error names
    # a column a reader's author can trace back to a source path without opening the contract.
    description = column.provenance.replace('"', "'")
    if column.null_meaning is not None:
        description = f"{description} · null means: {column.null_meaning}"

    arguments = [f'description="{description}"']
    if column.nullable:
        # Explicit rather than implicit. A nullable column with no default would be *required
        # and permitted to be null*, which is not what the contract says: it says the column
        # may be absent because only some contracts write it.
        arguments.insert(0, "default=None")
    if column.type is CanonicalType.DECIMAL:
        # The stored precision, asserted at the boundary. A monetary value arriving with more
        # than four decimal places means a divisor was applied wrongly, and silently rounding
        # it is how an order-of-magnitude error survives.
        arguments.extend(["max_digits=38", "decimal_places=4"])

    single = f"    {column.name}: {annotation} = Field({', '.join(arguments)})"
    if len(single) <= LINE_LIMIT:
        return single

    # Generated code is held to the same line limit as hand-written code, because a
    # per-file lint exemption would also hide a real finding in here later. A long
    # provenance string is wrapped as implicit-concatenated literals, which is what a
    # human would write.
    wrapped = [f"    {column.name}: {annotation} = Field("]
    for argument in arguments:
        if argument.startswith("description="):
            wrapped.append("        description=(")
            chunks = textwrap.wrap(description, width=LINE_LIMIT - 18)
            wrapped.extend(
                # A trailing space on every chunk but the last. `textwrap` breaks *on*
                # whitespace and discards it, so without this the literals concatenate
                # into "suppressedcolumn" and the description silently changes meaning.
                f'            "{chunk}{" " if index < len(chunks) - 1 else ""}"'
                for index, chunk in enumerate(chunks)
            )
            wrapped.append("        ),")
        else:
            wrapped.append(f"        {argument},")
    wrapped.append("    )")
    return "\n".join(wrapped)


def _model(contract: DataContract, columns: tuple[TargetColumn, ...]) -> str:
    """Render one contract's model."""
    grain = ", ".join(contract.grain)
    grain_lines = tuple(
        f"    {line}" for line in textwrap.wrap(f"Grain: {grain}.", width=LINE_LIMIT - 4)
    )
    lines = [
        f"class {class_name_for(contract.contract_id)}(BaseModel):",
        f'    """One curated row produced by `{contract.contract_id}`.',
        "",
        f"    Target table: {contract.target_table}.",
        *grain_lines,
        f"    Measurement basis: {contract.measurement_basis.value}.",
        f"    Source: {contract.source_system} · {contract.source_dataset}.",
        '    """',
        "",
        # `extra="forbid"` applies to the TARGET row, where an unexpected column is a reader
        # bug rather than an upstream surprise. The additive-tolerance rule governs parsing
        # the source document, which happens before this.
        '    model_config = ConfigDict(extra="forbid", frozen=True)',
        "",
    ]
    lines.extend(_field_line(column) for column in columns)
    return "\n".join(lines)


def render_models(contracts: list[DataContract], registry: ColumnRegistry) -> str:
    """Render the whole generated module, one model per contract in id order."""
    ordered = sorted(contracts, key=lambda contract: contract.contract_id)
    schemas = [(contract, target_schema(contract, registry)) for contract in ordered]

    needed = {
        PYTHON_TYPES[column.type]
        for _contract, columns in schemas
        for column in columns
        if PYTHON_TYPES[column.type] in IMPORTS
    }
    by_module: dict[str, list[str]] = {}
    for name in needed:
        module, symbol = IMPORTS[name]
        by_module.setdefault(module, []).append(symbol)
    import_lines = [
        f"from {module} import {', '.join(sorted(symbols))}"
        for module, symbols in sorted(by_module.items())
    ]

    # Blank lines are placed explicitly rather than by joining with "\n\n". isort wants
    # exactly one blank line between import sections and two before a top-level statement,
    # and a uniform join produced two everywhere — which ruff reports as an unsorted block.
    lines: list[str] = [HEADER, "", "from __future__ import annotations", ""]
    if import_lines:
        lines.extend([*import_lines, ""])
    lines.extend(
        [
            "from pydantic import BaseModel, ConfigDict, Field",
            # One blank, not two: isort treats `__all__` as closing the import region and
            # reports the second as an unsorted block.
            "",
            "__all__ = [",
            # Sorted by class name, as ruff's RUF022 reads it: contract order differs once one
            # contract id prefixes another (`amc-acquisition-cohorts` and its `-arp` sibling).
            *(
                f'    "{name}",'
                for name in sorted(class_name_for(contract.contract_id) for contract, _ in schemas)
            ),
            "]",
        ]
    )
    for contract, columns in schemas:
        lines.extend(["", "", _model(contract, columns)])
    return "\n".join(lines) + "\n"
