# SPDX-License-Identifier: MIT-0
"""Generate the curated table definitions from the contracts.

`make generate-schemas` writes them; `make check-schemas` regenerates and fails on any
diff. That pairing is the point: it makes it impossible for the curated schema to exist in
two places, which is what `docs/capabilities/data-layer/low-level-design.md` §6 requires. A
hand-written Iceberg definition alongside a contract is a second source of truth, and the two
drift silently because both look internally correct.

Output is JSON because the generated artefacts are committed interfaces consumed by checks and
Semantic V2 storage generation. Emitting SQL would make TypeScript parse SQL, which is a parser
nobody should write.

Stdout is the interface here, the same as the contract gate: this is a command-line tool
run by a make target, not a service.
"""

from __future__ import annotations

import json
import pathlib
import sys

from adapters.jobs.contract_validation.loader import load_contracts, load_registry
from adapters.jobs.schema_generation.dq import rulesets_for
from adapters.jobs.schema_generation.merge import (
    AbsentFill,
    MergedTable,
    merge_tables,
    withheld_tables,
)
from adapters.jobs.schema_generation.models import render_models
from adapters.jobs.schema_generation.physical_schema import (
    load_physical_schema_compatibility,
)
from adapters.jobs.schema_generation.publisher_graph import load_publication_graph
from adapters.jobs.schema_generation.v2_storage import V2StorageRequest, v2_storage_json
from adapters.outbound.source_pack import load_source_packs
from domain.semantics.publication import CanonicalDatasetKey, PublicationGraph

OUTPUT = pathlib.Path("contracts/generated/tables.json")

MODELS_OUTPUT = pathlib.Path("src/domain/data_contracts/generated_rows.py")
"""The Pydantic row models, one per contract.

Python rather than JSON because the consumer is Python and the point is static typing: a
reader assigning a `str` to a `Decimal` column should be a pyright error, not a runtime
validation failure. Committed alongside the table definitions and guarded by the same
staleness check.
"""

DQ_OUTPUT = pathlib.Path("contracts/generated/dq-rulesets.json")
V2_OUTPUT = pathlib.Path("contracts/generated/v2-tables.json")
"""The DQDL ruleset per curated table, for build-order 1.8's DQ gate.

JSON for the same reason as the table definitions: the consumer is CDK, which passes the
`ruleset` text to `glue.CfnDataQualityRuleset` verbatim.
"""

HEADER = {
    "$comment": (
        "GENERATED FROM contracts/*/*.yaml BY make generate-schemas. Do not edit. "
        "make check-schemas regenerates this and fails on any diff, so an edit here is "
        "reverted rather than kept. Change the contract instead."
    )
}


def _as_json(tables: tuple[MergedTable, ...], graph: PublicationGraph) -> dict[str, object]:
    return {
        **HEADER,
        "tables": [
            {
                "name": table.name,
                "kind": "dimension" if table.is_dimension else "fact",
                # The generated canonical declaration records the contract's semantic load shape.
                # V2 storage generation consumes the same contracts without reviving the retired
                # mutable writer.
                "load": "scd2_merge" if table.is_dimension else "partition_overwrite",
                "grain": list(table.grain),
                "partitionKeys": list(table.partition_keys),
                "measurementBases": list(table.measurement_bases),
                "writtenBy": list(table.written_by),
                "canonicalPublisher": graph.owner_for(CanonicalDatasetKey(table.name)).value,
                "columns": [
                    {
                        "name": column.name,
                        "type": column.type.value,
                        "nullable": column.nullable,
                        "comment": " · ".join(column.provenance),
                        **({"nullMeaning": column.null_meaning} if column.null_meaning else {}),
                        # Only the ADR-109 exception is rendered: zero-fill is every other
                        # partially written column's default, so naming it would add nothing.
                        **(
                            {"absentFill": column.absent_fill.value}
                            if column.absent_fill is AbsentFill.NULL
                            else {}
                        ),
                    }
                    for column in table.columns
                ],
            }
            for table in tables
        ],
    }


def main(argv: list[str]) -> int:
    """Generate the table definitions, or with `--check` verify they are not stale."""
    check_only = "--check" in argv
    root = pathlib.Path.cwd()

    registry = load_registry(root)
    contracts = [contract for _, contract in load_contracts(root)]
    tables = merge_tables(contracts, registry)
    # Residual tables are derived from the facts, then treated identically from here on — they are
    # ordinary Iceberg tables that CDK creates and a load writes. ADR-034.
    tables = tuple(sorted((*tables, *withheld_tables(tables, contracts)), key=lambda t: t.name))
    graph = load_publication_graph(
        root,
        frozenset(CanonicalDatasetKey(table.name) for table in tables),
    )
    packs = load_source_packs(root)
    compatibility = load_physical_schema_compatibility(root)

    artefacts = {
        OUTPUT: json.dumps(_as_json(tables, graph), indent=2) + "\n",
        MODELS_OUTPUT: render_models(contracts, registry),
        DQ_OUTPUT: json.dumps({**HEADER, "rulesets": rulesets_for(tables, contracts)}, indent=2)
        + "\n",
        V2_OUTPUT: json.dumps(
            {
                **HEADER,
                **v2_storage_json(V2StorageRequest(tables, packs, graph, compatibility)),
            },
            indent=2,
        )
        + "\n",
    }

    print("CURATED SCHEMA GENERATION")
    print("=" * 60)
    print(f"  contracts: {len(contracts)}")
    print(f"  tables:    {len(tables)}")
    for table in tables:
        nullable = sum(1 for c in table.columns if c.nullable)
        print(
            f"    {table.name:38s} {len(table.columns):3d} cols "
            f"({nullable} nullable)  <- {len(table.written_by)} contract(s)"
        )

    if check_only:
        stale = [
            path
            for path, content in artefacts.items()
            if not (root / path).exists() or (root / path).read_text() != content
        ]
        if stale:
            print("\nStale or missing, so a contract changed without regeneration:")
            for path in stale:
                print(f"  {path}")
            print("\nRun `make generate-schemas` and commit the result.")
            return 1
        print(f"\n{len(artefacts)} generated artefacts match the contracts.")
        return 0

    for path, content in artefacts.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        print(f"\nWrote {path}.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
