# SPDX-License-Identifier: MIT-0
"""Strict declarations and static conformance for hand-authored trace spans."""

from adapters.jobs.trace_contract.conformance import conformance_findings
from adapters.jobs.trace_contract.contract import (
    GENERATED_SPANS_PATH,
    SPAN_CONTRACT_PATH,
    SpanDeclaration,
    TraceContract,
    TraceContractError,
    load_trace_contract,
    render_generated_spans,
)

__all__ = [
    "GENERATED_SPANS_PATH",
    "SPAN_CONTRACT_PATH",
    "SpanDeclaration",
    "TraceContract",
    "TraceContractError",
    "conformance_findings",
    "load_trace_contract",
    "render_generated_spans",
]
