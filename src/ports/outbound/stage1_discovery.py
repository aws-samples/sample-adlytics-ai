# SPDX-License-Identifier: MIT-0
"""Outbound boundary for ADR-075 Stage-1 sparse-index discovery (write discipline §§5, 5.3).

Three bounded, cursorless, eventually consistent queries over the two DataV2-owned sparse GSIs
on the publications table: due pending fences and canonical blocked fences on
``table-attempt-reconcile``, and due pending plan intents on ``stage1-plan-intent-ready``. Each
returns only projected base keys; every consumer then strongly reads the base items and decides
from those bytes, never from the index projection.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from domain.operations.table_attempt import is_canonical_instant

__all__ = [
    "BlockedFenceQuery",
    "DiscoveredKey",
    "DueQuery",
    "Stage1DiscoveryPort",
]

_MAX_LIMIT = 100


@dataclass(frozen=True, slots=True)
class DiscoveredKey:
    """One projected publications-table base key returned by a sparse-index query."""

    partition_key: str
    record_key: str
    sort_value: str
    """The index sort key value (``reconcile_after_or_block`` or ``ready_order``)."""

    def __post_init__(self) -> None:
        for name in ("partition_key", "record_key", "sort_value"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"a discovered key carries a nonblank {name}.")


@dataclass(frozen=True, slots=True)
class DueQuery:
    """One due query: everything whose 20-digit due instant is at or before ``now``."""

    now_instant: str
    limit: int

    def __post_init__(self) -> None:
        if not is_canonical_instant(self.now_instant):
            raise ValueError("the due query instant must be a 20-digit epoch-microseconds instant.")
        if isinstance(self.limit, bool) or not 1 <= self.limit <= _MAX_LIMIT:
            raise ValueError("the due query limit must be an integer in 1..100.")

    @property
    def upper_bound(self) -> str:
        """Render ``<now>#~``: every key due at or before ``now`` sorts at or below it."""
        return f"{self.now_instant}#~"


@dataclass(frozen=True, slots=True)
class BlockedFenceQuery:
    """One blocked-partition page: ``limit`` keys lexically after the optional cursor ref."""

    limit: int
    after_recovery_ref: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.limit, bool) or not 1 <= self.limit <= _MAX_LIMIT:
            raise ValueError("the blocked query limit must be an integer in 1..100.")
        if self.after_recovery_ref is not None and not self.after_recovery_ref.strip():
            raise ValueError("a blocked query cursor must be a nonblank recovery reference.")


@runtime_checkable
class Stage1DiscoveryPort(Protocol):
    """Cursorless bounded discovery over the two Stage-1 sparse indexes."""

    def due_pending_fences(self, query: DueQuery) -> tuple[DiscoveredKey, ...]:
        """Query ``reconcile_status="pending"`` with ``reconcile_after_or_block <= now#~``."""
        ...

    def blocked_fences(self, query: BlockedFenceQuery) -> tuple[DiscoveredKey, ...]:
        """Query ``reconcile_status="blocked"``, optionally after ``BLOCK#<cursor>``."""
        ...

    def ready_intents(self, query: DueQuery) -> tuple[DiscoveredKey, ...]:
        """Query ``ready_status="ready"`` with ``ready_order <= now#~``."""
        ...
