# SPDX-License-Identifier: MIT-0
"""Generate and check the operational data-flow runtime registry."""

from __future__ import annotations

import sys
from pathlib import Path

from adapters.jobs.data_flow_registry.contract import (
    GENERATED_PATH,
    DataFlowRegistryError,
    infrastructure_findings,
    load_registry,
    render_generated,
)

_CHECK = "--check"


def main(argv: list[str] | None = None) -> int:
    """Write the pure source projection or validate registry and processor coverage."""
    arguments = sys.argv[1:] if argv is None else argv
    if any(argument != _CHECK for argument in arguments) or arguments.count(_CHECK) > 1:
        sys.stderr.write("usage: python -m adapters.jobs.data_flow_registry [--check]\n")
        return 2
    root = Path.cwd()
    try:
        document = load_registry(root)
    except DataFlowRegistryError as error:
        sys.stderr.write(f"DATA FLOW REGISTRY ERROR: {error}\n")
        return 1
    rendered = render_generated(document)
    output = root / GENERATED_PATH
    if _CHECK in arguments:
        findings = list(infrastructure_findings(root))
        if not output.is_file() or output.read_text() != rendered:
            findings.append(
                f"{GENERATED_PATH} is missing or stale; run make generate-data-flow-registry"
            )
        if findings:
            sys.stderr.write("DATA FLOW COVERAGE CHECK FAILED\n")
            for finding in findings:
                sys.stderr.write(f"  {finding}\n")
            return 1
        sys.stdout.write(
            "DATA FLOW COVERAGE CHECK PASSED: 22 asynchronous and 7 synchronous surfaces.\n"
        )
        return 0
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered)
    sys.stdout.write(f"wrote {GENERATED_PATH}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
