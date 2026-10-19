# SPDX-License-Identifier: MIT-0
"""Persistence authorities for materialized source-aligned publications.

Write and read are separate protocols so a canonical publisher may resolve immutable source inputs
without gaining authority to reinterpret or replace them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from domain.semantics.publication import SourcePublicationRef
from domain.semantics.source_aligned import SourceAlignedPublication

__all__ = [
    "MaterializeSourceRowsetResult",
    "SourceAlignedReadPort",
    "SourceAlignedWritePort",
]


@dataclass(frozen=True, slots=True)
class MaterializeSourceRowsetResult:
    """Whether immutable source evidence was created or already existed exactly."""

    accepted: bool
    created: bool
    reason: str = ""

    def __post_init__(self) -> None:
        if not self.accepted and not self.reason:
            raise ValueError("a refused source-aligned materialization must state why.")
        if self.created and not self.accepted:
            raise ValueError("a refused source-aligned materialization cannot be created.")


@runtime_checkable
class SourceAlignedWritePort(Protocol):
    """Authority to materialize an immutable interpreted source rowset."""

    def materialize(
        self,
        publication: SourceAlignedPublication,
    ) -> MaterializeSourceRowsetResult:
        """Create one immutable publication or accept an exact replay."""
        ...


@runtime_checkable
class SourceAlignedReadPort(Protocol):
    """Authority to resolve source evidence without changing its interpretation."""

    def publication(self, reference: SourcePublicationRef) -> SourceAlignedPublication | None:
        """Return one immutable source publication by its stable reference."""
        ...
