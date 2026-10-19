# SPDX-License-Identifier: MIT-0
"""Generate or check the committed static trace-contract projection."""

from __future__ import annotations

import sys
from pathlib import Path

from adapters.jobs.trace_contract.conformance import conformance_findings
from adapters.jobs.trace_contract.contract import (
    GENERATED_SPANS_PATH,
    TraceContractError,
    load_trace_contract,
    render_generated_spans,
)

_CHECK_FLAG = "--check"


def main(argv: list[str] | None = None) -> int:
    """Write generated constants, or check exactness and all AST conformance rules."""
    arguments = sys.argv[1:] if argv is None else argv
    if any(argument != _CHECK_FLAG for argument in arguments) or arguments.count(_CHECK_FLAG) > 1:
        sys.stderr.write("usage: python -m adapters.jobs.trace_contract [--check]\n")
        return 2
    checking = _CHECK_FLAG in arguments
    root = Path.cwd()
    try:
        contract = load_trace_contract(root)
    except TraceContractError as error:
        sys.stderr.write(f"TRACE CONTRACT ERROR: {error}\n")
        return 1
    output = root / GENERATED_SPANS_PATH
    rendered = render_generated_spans(contract)
    if not checking:
        # Write before conformance so a newly declared span's constant exists for the owner
        # that has yet to be written; the check mode below still fails until it is.
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered)

    findings = conformance_findings(root, contract)
    if findings:
        sys.stderr.write("TRACE CONTRACT CONFORMANCE FAILED\n")
        for finding in findings:
            sys.stderr.write(f"  {finding}\n")
        return 1

    if checking:
        if not output.is_file() or output.read_text() != rendered:
            sys.stderr.write(
                f"{GENERATED_SPANS_PATH} is missing or stale. Run `make generate-trace-contract`.\n"
            )
            return 1
        sys.stdout.write(
            f"TRACE CONTRACT CHECK PASSED: {len(contract.spans)} static spans conform.\n"
        )
        return 0

    sys.stdout.write(
        f"wrote {GENERATED_SPANS_PATH} from {SPAN_CONTRACT_LABEL} ({len(contract.spans)} spans).\n"
    )
    return 0


SPAN_CONTRACT_LABEL = "contracts/observability/spans.yaml"


if __name__ == "__main__":
    sys.exit(main())
