#!/usr/bin/env python3
# SPDX-License-Identifier: MIT-0
"""Verify the columns every fact must carry are declared, generated, and documented.

Three artefacts have to agree about `basis_key` and `source_timezone`:

    src/domain/data_contracts/schema.py   which columns every fact carries
    contracts/generated/tables.json       whether each table actually has them
    docs/canonical-model.md               whether a reader is told they exist

They disagreed. The canonical model's ERD joined `dim_measurement_basis` on `basis_key`,
its worked query selected it, and `dim_measurement_basis` enumerated its values — while no
contract wrote the column and no gate noticed, because `check-lineage` walks a contract's
*field mappings* and these two are projected from contract-level metadata instead. So the
one column the model insisted every fact had was the one column no fact had.

`source_timezone` was the same defect with a deadline attached: ADR-004 requires it on every
fact from the first load, because backfilling it later needs a value that may no longer be
knowable.

This gate exists because that class of defect is invisible to every other check. It runs
under `uv` rather than system python, like `check-lineage`, because it reads the column list
from the domain rather than restating it.

Usage:  uv run python scripts/check-conformed-columns.py
Exits non-zero on any finding, so it can run in CI.
"""

from __future__ import annotations

import json
import pathlib
import sys

from domain.data_contracts.contract import DIMENSION_PREFIX, FACT_PREFIX
from domain.data_contracts.schema import (
    CONFORMED_PROVENANCE_COLUMNS,
    MEASUREMENT_PROVENANCE_COLUMNS,
)

ROOT = pathlib.Path(__file__).resolve().parent.parent
TABLES = ROOT / "contracts" / "generated" / "tables.json"
CANONICAL = ROOT / "docs" / "canonical-model.md"

findings: list[str] = []


def _tables() -> list[dict[str, object]]:
    """Read the generated table list, whichever shape the artefact uses."""
    loaded = json.loads(TABLES.read_text())
    if isinstance(loaded, dict):
        inner = loaded.get("tables", loaded)
        return list(inner.values()) if isinstance(inner, dict) else list(inner)
    return list(loaded)


print("CONFORMED COLUMN CHECK")
print("=" * 60)
print("  declared: {}".format(", ".join(CONFORMED_PROVENANCE_COLUMNS)))

if not TABLES.exists():
    print("\nFINDINGS (1)\n    x {} is missing — run make generate-schemas".format(TABLES.name))
    sys.exit(1)

tables = _tables()
facts = 0
dimensions = 0

for table in sorted(tables, key=lambda t: str(t.get("name", ""))):
    name = str(table.get("name", ""))
    columns_raw = table.get("columns", [])
    columns = {str(c.get("name", "")) for c in columns_raw if isinstance(c, dict)}

    if name.startswith(FACT_PREFIX):
        facts += 1
        missing = [c for c in CONFORMED_PROVENANCE_COLUMNS if c not in columns]
        if missing:
            findings.append(
                "{}: a fact is missing {}. It is projected from contract-level "
                "metadata — see conformed_provenance in domain/data_contracts/schema.py".format(
                    name, ", ".join(missing)
                )
            )
    elif name.startswith(DIMENSION_PREFIX):
        dimensions += 1
        present = [c for c in MEASUREMENT_PROVENANCE_COLUMNS if c in columns]
        if present:
            findings.append(
                "{}: a dimension carries {}, which qualifies measurement rather than the "
                "entity it describes, so the join would mean nothing".format(
                    name, ", ".join(present)
                )
            )
    else:
        findings.append(
            "{}: neither a {} nor a {} table, so nothing decides whether the conformed "
            "columns apply".format(name, FACT_PREFIX, DIMENSION_PREFIX)
        )

# A column the code projects but no reader is told about is the same failure in the other
# direction: the model stops describing the tables it produces.
prose = CANONICAL.read_text()
for column in CONFORMED_PROVENANCE_COLUMNS:
    if f"`{column}`" not in prose:
        findings.append(
            "{} is projected onto every fact but never mentioned in {} — a column a "
            "reader cannot discover is a column they will not query".format(column, CANONICAL.name)
        )

print("  scanned:  {} fact(s), {} dimension(s)".format(facts, dimensions))
print()

if not facts:
    print("FINDINGS (1)")
    print("    x no fact tables found — the artefact is empty or wrongly shaped")
    sys.exit(1)

if findings:
    print("FINDINGS ({})".format(len(findings)))
    for finding in findings:
        print("    x {}".format(finding))
    sys.exit(1)

print(
    "Every fact carries {}; no dimension carries measurement provenance {}.".format(
        ", ".join(CONFORMED_PROVENANCE_COLUMNS),
        ", ".join(MEASUREMENT_PROVENANCE_COLUMNS),
    )
)
