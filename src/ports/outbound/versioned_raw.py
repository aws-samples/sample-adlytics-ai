# SPDX-License-Identifier: MIT-0
"""Read-only outbound boundary for exact retained raw object versions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

__all__ = ["RawVersion", "VersionedBody", "VersionedRawReadPort"]


@dataclass(frozen=True, slots=True)
class RawVersion:
    """One listed object or delete-marker version under an explicit source prefix."""

    key: str
    version_id: str
    byte_count: int
    modified_at: datetime
    delete_marker: bool

    def __post_init__(self) -> None:
        if not self.key.strip() or not self.version_id.strip():
            raise ValueError("raw version requires key and VersionId.")
        if self.byte_count < 0:
            raise ValueError("raw version byte count cannot be negative.")
        if self.modified_at.tzinfo is None:
            raise ValueError("raw version modification instant must be timezone-aware.")


@dataclass(frozen=True, slots=True)
class VersionedBody:
    """Bytes returned for one explicitly selected object VersionId."""

    key: str
    version_id: str
    body: bytes

    def __post_init__(self) -> None:
        if not self.key.strip() or not self.version_id.strip():
            raise ValueError("versioned body requires key and VersionId.")


@runtime_checkable
class VersionedRawReadPort(Protocol):
    """Read exact versions only; never discover buckets or mutate raw evidence."""

    def versions(self, prefix: str) -> tuple[RawVersion, ...]:
        """List every retained version under one validated non-empty dataset prefix."""
        ...

    def body(self, key: str, version_id: str) -> VersionedBody:
        """Read exactly the selected key and VersionId."""
        ...
