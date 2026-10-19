# SPDX-License-Identifier: MIT-0
"""The GRAPH gates on the legacy publication workers (S6 §2.9; D6, AGG3-6).

After the `GRAPH` CAS, the publication-stream workers (preset enqueue and insight precompute),
the baker's legacy intents and the deploy-time backfill must have zero effect: the first granted
run's consumers re-evaluate every registered page against post-flip `CURRENT`, so nothing a gate
drops is lost. Each worker reads the mode once per batch, before it parses a record, and counts
what it dropped on `LegacyWorkAfterCutover` under its closed signal.

Where the graph authority is not provisioned the gate reads `LEGACY`, exactly as an absent mode
item reads, and never closes.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import cast

import boto3

from adapters.config import graph_consumer_settings
from adapters.outbound.dynamodb.execution_graph_dispatch import DynamoGraphDispatchStore
from adapters.outbound.telemetry.graph_metrics import emit_cutover_drop
from domain.operations.execution_graph import AuthorityMode

__all__ = ["CutoverGate", "provisioned_cutover_gate", "record_count"]


def _legacy() -> AuthorityMode:
    return AuthorityMode.LEGACY


@dataclass(frozen=True, slots=True)
class CutoverGate:
    """One worker's mode read and its drop counter."""

    mode: Callable[[], AuthorityMode] = _legacy
    count: Callable[[str, int], None] = field(default=emit_cutover_drop)

    def closed(self, signal: str, dropped: int) -> bool:
        """Read the mode once; in `GRAPH` count `dropped` under `signal` and return True."""
        if self.mode() is not AuthorityMode.GRAPH:
            return False
        self.count(signal, dropped)
        return True


def record_count(event: object) -> int:
    """Return how many records a Lambda event carries, without parsing any of them."""
    if not isinstance(event, dict):
        return 0
    records = cast("dict[str, object]", event).get("Records")
    return len(cast("list[object]", records)) if isinstance(records, list) else 0


def provisioned_cutover_gate() -> CutoverGate:
    """Compose the gate over the graph table's mode item, or a `LEGACY` gate without one."""
    table = graph_consumer_settings().execution_graph_table
    if not table:
        return CutoverGate()
    store = DynamoGraphDispatchStore(table, boto3.client("dynamodb"))
    return CutoverGate(mode=lambda: store.read_mode().mode)
