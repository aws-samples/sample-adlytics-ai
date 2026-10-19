# SPDX-License-Identifier: MIT-0
"""The execution-graph workers install tracing and flush spans on every handler.

Acceptance x5's trace rehearsal found zero `execution_graph.promoter_pass` and
`execution_graph.promoter_domain` spans in `aws/spans` while the Promoter ran every minute:
`workers.py` never called `configure_tracing`, so every span it opened was a no-op. Criterion 6
(trace completeness) exists to catch exactly that. This pins the wiring statically, with a
paired control that removes each half and must fail.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-074")]

_WORKERS = (
    pathlib.Path(__file__).resolve().parents[2]
    / "src"
    / "adapters"
    / "jobs"
    / "execution_graph"
    / "workers.py"
)
_HANDLERS = (
    "dispatcher_handler",
    "fence_settler_handler",
    "node_verifier_handler",
    "promoter_handler",
    "release_handler",
)


def tracing_gaps(source: str) -> list[str]:
    """Name each missing piece: module-scope `configure_tracing`, or a handler's flush."""
    tree = ast.parse(source)
    gaps: list[str] = []
    configured = any(
        isinstance(node, ast.Assign | ast.Expr)
        and isinstance(node.value, ast.Call)
        and getattr(node.value.func, "id", None) == "configure_tracing"
        for node in tree.body
    )
    if not configured:
        gaps.append("configure_tracing")
    handlers = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    for name in _HANDLERS:
        node = handlers.get(name)
        names = [getattr(item, "id", None) for item in (node.decorator_list if node else [])]
        if node is None or "flushes_traces" not in names:
            gaps.append(f"{name}:flushes_traces")
    return gaps


def test_every_worker_handler_runs_with_tracing_installed_and_flushed() -> None:
    assert tracing_gaps(_WORKERS.read_text(encoding="utf-8")) == []


def test_removing_the_tracing_setup_or_a_flush_is_caught() -> None:
    source = _WORKERS.read_text(encoding="utf-8")
    unconfigured = source.replace("_TRACING = configure_tracing(_TRACING_SETTINGS)\n", "")
    assert tracing_gaps(unconfigured) == ["configure_tracing"]
    unflushed = source.replace(
        "@flushes_traces\n@flushes_metrics\n@trace_boundary(EXECUTION_GRAPH_PROMOTER_PASS)",
        "@flushes_metrics\n@trace_boundary(EXECUTION_GRAPH_PROMOTER_PASS)",
    )
    assert tracing_gaps(unflushed) == ["promoter_handler:flushes_traces"]
