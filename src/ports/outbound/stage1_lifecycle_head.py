# SPDX-License-Identifier: MIT-0
"""Outbound port for ADR-075 Stage-1 lifecycle head and event-authority persistence.

The store's primary consumption is embedding into larger coordinator transactions, so the port
exposes the conditioned event puts and head write as opaque transaction fragments. The fragment
mappings are DynamoDB ``TransactWriteItem``-shaped; the port deliberately types them as plain
mappings so no AWS SDK name crosses this boundary.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from domain.operations.stage1_lifecycle_head import Stage1LifecycleProof


class Stage1LifecycleApplyKind(StrEnum):
    """Closed standalone-apply outcomes; a conflict raises instead."""

    APPLIED = "applied"
    REPLAYED_BYTE_IDENTICAL = "replayed_byte_identical"
    REPLAYED_AFTER_ADVANCE = "replayed_after_advance"


@dataclass(frozen=True, slots=True)
class Stage1LifecycleApplyResult:
    """One standalone transaction's typed outcome."""

    kind: Stage1LifecycleApplyKind


@dataclass(frozen=True, slots=True)
class Stage1LifecycleTransactionItems:
    """Typed transaction fragments for one proof: event puts plus at most one head write."""

    event_puts: tuple[Mapping[str, object], ...]
    head_write: Mapping[str, object] | None

    def __post_init__(self) -> None:
        if not self.event_puts:
            raise ValueError("a lifecycle proof carries at least one event authority")


@runtime_checkable
class Stage1LifecycleHeadStorePort(Protocol):
    """Conditional Stage-1 lifecycle head and retained event-authority writes."""

    def transaction_items(self, proof: Stage1LifecycleProof) -> Stage1LifecycleTransactionItems:
        """Return the conditioned fragments a coordinator embeds in its own transaction."""
        ...

    def apply(self, proof: Stage1LifecycleProof) -> Stage1LifecycleApplyResult:
        """Run exactly the proof's fragments as one transaction with replay classification."""
        ...
