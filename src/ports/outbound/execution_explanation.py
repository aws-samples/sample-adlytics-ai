# SPDX-License-Identifier: MIT-0
"""Outbound ports the execution explanation and the graph projection use (S2).

Every port here is read-only against the execution-graph store: the explanation reads graph
authority and the status projection, and the projector writes only the projection. No port
offers a graph write, so a cursor or an explanation can never mutate graph authority (BF-5).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Protocol

from domain.operations.execution_graph import ModeState
from domain.operations.graph_projection import GraphProjectionRow

__all__ = [
    "GraphAuthorityReader",
    "GraphProjectionReader",
    "GraphProjectionWriter",
    "ManifestVersionReader",
]

type AuthorityItem = Mapping[str, object]


class GraphAuthorityReader(Protocol):
    """Strongly consistent reads of the execution-graph store; never a write."""

    def read_mode(self) -> ModeState:
        """Read the authority mode item."""
        ...

    def graph(self, graph_id: str) -> AuthorityItem | None:
        """Read one graph record, decoded."""
        ...

    def epoch1_graph_id(self, fingerprint: str) -> str | None:
        """Return the graph of the epoch-1 run a fingerprint names, if one exists."""
        ...

    def items(self, keys: Sequence[tuple[str, str]]) -> Mapping[tuple[str, str], AuthorityItem]:
        """Read exact items by (pk, sk), decoded; absent keys are omitted."""
        ...


class GraphProjectionReader(Protocol):
    """Reads of the status v2 rows under one root flow."""

    def graphs(self, root_flow_id: str) -> tuple[GraphProjectionRow, ...]:
        """Return every projected graph row of the root."""
        ...

    def receipts(
        self, root_flow_id: str, after: str | None, limit: int
    ) -> tuple[tuple[GraphProjectionRow, ...], bool]:
        """Return up to `limit` receipt rows after `after`, and whether more exist."""
        ...

    def executions(self, root_flow_id: str) -> tuple[tuple[GraphProjectionRow, ...], bool]:
        """Return the root's NODE, CLAIM and SERVING rows, and whether any collection was cut."""
        ...


class GraphProjectionWriter(Protocol):
    """The projector's only write: one immutable row, idempotently."""

    def apply(self, row: GraphProjectionRow) -> bool:
        """Put the row once; return False for an exact replay and raise on a conflict."""
        ...


class ManifestVersionReader(Protocol):
    """Exact-version reads of the raw manifest zone."""

    def version_ids(self, key: str) -> tuple[str, ...]:
        """Return the version IDs of exactly this key (longer keys under the prefix excluded)."""
        ...

    def last_modified(self, key: str, version_id: str) -> datetime | None:
        """Return the exact version's `LastModified`, or None if the version does not exist."""
        ...
