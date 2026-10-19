# SPDX-License-Identifier: MIT-0
"""Whether this deployment carries the execution-graph authority (S3).

The graph store exists only where the operational plane is provisioned (dev, behind
`ADLYTICS_OPERATIONAL_STATUS_PROVISIONED`). Ingestion functions run everywhere, so they compose
the graph authority only when the stack gave them its table. Without it the authority is absent,
which reads exactly as an absent mode item does: `LEGACY` (`design.md`, "Authority modes").

The table name resolves through `adapters.config` (`run_request_api_settings`), never the
environment directly. `adapters/config.py` is pinned while the lagging stacks are accepted
(owner-gate Item 14), so the optional reading lives here until its next revision adds an
explicit optional accessor. In dev the CDK test asserts every graph consumer receives the table,
so an absent value there is a failed deploy check, not a silent default.
"""

from __future__ import annotations

from adapters.config import run_request_api_settings
from domain.shared_kernel.errors import InvariantError

__all__ = ["provisioned_graph_table"]


def provisioned_graph_table() -> str | None:
    """Return the execution-graph table, or None where the graph authority is not provisioned."""
    try:
        return run_request_api_settings().execution_graph_table
    # error-propagation: not provisioned is the declared None (LEGACY); dev's CDK test asserts it
    except InvariantError:
        return None
