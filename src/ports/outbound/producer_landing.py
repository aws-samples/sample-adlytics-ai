# SPDX-License-Identifier: MIT-0
"""Outbound boundaries for exact source-registration finalization."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from domain.producer.status import (
    RegistrationAttempt,
    RegistrationStatus,
    RegistrationWriteOutcome,
)


@dataclass(frozen=True, slots=True)
class VersionedProducerObject:
    """Exact bytes returned from one selected S3 object version."""

    key: str
    version_id: str
    body: bytes

    def __post_init__(self) -> None:
        if not self.key.strip() or not self.version_id.strip():
            raise ValueError("versioned producer object requires key and VersionId.")


@dataclass(frozen=True, slots=True)
class StoredProducerObject:
    """Exact version created by one destination write."""

    key: str
    version_id: str

    def __post_init__(self) -> None:
        if not self.key.strip() or not self.version_id.strip():
            raise ValueError("stored producer object requires key and VersionId.")


@runtime_checkable
class ProducerLandingPort(Protocol):
    """Read exact incoming evidence and create version-bound governed objects."""

    def size(self, key: str, version_id: str) -> int:
        """Return exact-version ContentLength without downloading the body."""
        ...

    def body(self, key: str, version_id: str) -> VersionedProducerObject:
        """Read exactly the selected object VersionId, never the current head."""
        ...

    def put(self, key: str, body: bytes, content_type: str) -> StoredProducerObject:
        """Create once, or recover the exact version only when existing bytes are identical."""
        ...


@runtime_checkable
class RegistrationStatusPort(Protocol):
    """Persist operational finalization state without granting publication authority."""

    def get(self, attempt: RegistrationAttempt) -> RegistrationStatus | None:
        """Strongly read the exact attempt status, or None before first claim."""
        ...

    def record(self, status: RegistrationStatus) -> RegistrationWriteOutcome:
        """Conditionally create processing or transition it to one terminal decision."""
        ...
