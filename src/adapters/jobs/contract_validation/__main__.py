# SPDX-License-Identifier: MIT-0
"""`make validate-contracts` — the gate over contracts/.

Runs on a developer machine and in the merge gate, never in a Lambda: it reads the
repository, touches no AWS, and takes no configuration. So it uses `print` rather than
the Powertools Logger, which is the tool for a running service.

Exit codes follow the repository's other gates: 0 clean, 1 findings. A warning reports
and does not block, because a rule that cannot be certain should not stop a merge.
"""

from __future__ import annotations

import sys
from pathlib import Path

from domain.data_contracts.contract import ContractError
from domain.data_contracts.validation import (
    Finding,
    Severity,
    check_delivery_diagnostics_are_not_conformed,
    check_shared_measures_are_separable,
    has_errors,
    validate,
)
from domain.presentation.insights import validate_insight_contracts

from .insight_loader import load_insight_contracts
from .loader import load_contracts, load_registry


def _repo_root() -> Path:
    """Walk up from this module to the directory holding contracts/."""
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "contracts").is_dir() and (candidate / "pyproject.toml").is_file():
            return candidate
    raise ContractError("could not locate the repository root from this module.")


def main() -> int:
    """Validate every contract against the registry and report every finding."""
    print("DATA CONTRACT VALIDATION")
    print("=" * 60)

    root = _repo_root()
    try:
        registry = load_registry(root)
        contracts = load_contracts(root)
        insight_contracts = load_insight_contracts(root)
    except ContractError as error:
        # A malformed contract cannot be validated, so this is the one place the gate
        # stops early. The message names the file and the key.
        print(f"\n  {error}", file=sys.stderr)
        return 1

    print(f"  registry: {len(registry.columns)} columns across {len(registry.known_tables)} tables")
    print(f"  contracts: {len(contracts)}")
    print(f"  insight contracts: {len(insight_contracts)}")

    insight_errors = validate_insight_contracts(
        tuple(contract for _, contract in insight_contracts)
    )
    if insight_errors:
        for error in insight_errors:
            print(f"  {error}", file=sys.stderr)
        return 1

    if not contracts:
        print(
            "\n  No contracts yet. The gate is wired and will enforce on the first one.\n"
            "  Rules live in src/domain/data_contracts/validation.py."
        )
        return 0

    findings: list[Finding] = []
    for path, contract in contracts:
        contract_findings = validate(contract, registry)
        findings.extend(contract_findings)
        errors = sum(1 for f in contract_findings if f.severity is Severity.ERROR)
        warnings = len(contract_findings) - errors
        status = "OK" if not contract_findings else f"{errors} error(s), {warnings} warning(s)"
        print(f"    {contract.contract_id:36s} {status}")
        for finding in contract_findings:
            print(f"      [{finding.severity.value}] {finding.rule}: {finding.message}")
        if contract_findings:
            print(f"      in {path.relative_to(root)}")

    # Across the whole set rather than per contract, because separability is a relationship
    # between two contracts writing one fact — see ADR-033.
    print()
    print("  ACROSS CONTRACTS")
    only = [contract for _, contract in contracts]
    across = (
        *check_shared_measures_are_separable(only),
        *check_delivery_diagnostics_are_not_conformed(only),
    )
    findings.extend(across)
    if not across:
        print("    shared_measures_are_separable         OK")
        print("    delivery_diagnostics_are_not_conformed  OK")
    for finding in across:
        print(f"    [{finding.severity.value}] {finding.contract_id}: {finding.message}")

    total_errors = sum(1 for f in findings if f.severity is Severity.ERROR)
    total_warnings = len(findings) - total_errors
    print()
    if has_errors(tuple(findings)):
        print(f"  {total_errors} error(s) and {total_warnings} warning(s). Contracts not valid.")
        return 1
    if findings:
        print(f"  {total_warnings} warning(s), no errors. Contracts valid.")
        return 0
    print("  Every contract satisfies every rule.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
