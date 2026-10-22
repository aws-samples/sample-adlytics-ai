# SPDX-License-Identifier: MIT-0
"""The staged table unit and the port that writes it once (ADR-149 decisions 3 and 5).

A table unit's exact physical records are rendered to one content-addressed JSON Lines
object just before its `MERGE` begins. `StagedUnit` is that object's summary: its key, its
byte length and its per-identity row counts. It never holds the body, so a roster of every
unit stays small; the body exists only inside `UnitStager.ensure`.

The codes live here, not in `domain.shared_kernel.errors`, so the deep-input closure (which
imports that module) does not move when staging changes.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final, Protocol

from domain.shared_kernel.errors import DependencyError, ErrorCode, InvariantError

__all__ = [
    "STAGING_COMPOSITION_SHAPE",
    "STAGING_CONFLICT",
    "STAGING_EMPTY_IDENTITY",
    "STAGING_RENDER_DIVERGED",
    "STAGING_UNAVAILABLE",
    "StagedUnit",
    "StagingUnavailableError",
    "UnitStager",
]

STAGING_UNAVAILABLE: Final = ErrorCode("SEMANTIC_STAGING", 1)
"""The staging store could not be written or read (throttling, 5xx, timeout, 403, 409)."""
STAGING_CONFLICT: Final = ErrorCode("SEMANTIC_STAGING", 2)
"""An object already at a staging key has a different checksum or length."""
STAGING_RENDER_DIVERGED: Final = ErrorCode("SEMANTIC_STAGING", 3)
"""A re-rendered body differs from its key, or the store rejected its checksum."""
STAGING_EMPTY_IDENTITY: Final = ErrorCode("SEMANTIC_STAGING", 4)
"""A unit identity carries no records, so the verification could never count it."""
STAGING_COMPOSITION_SHAPE: Final = ErrorCode("SEMANTIC_STAGING", 5)
"""A unit composition is not exactly one staged MERGE then one verification, or its staged
summary is malformed: either is a composer defect."""

_STAGING_KEY: Final = re.compile(r"^[0-9a-f]{64}$")
_TABLE: Final = re.compile(r"^[a-z][a-z0-9_]*$")


def _positive(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


@dataclass(frozen=True, slots=True)
class StagedUnit:
    """One table unit's staged object, by content address: never its body.

    ``table`` is the physical table name the object is filed under; ``row_counts`` holds the
    record count per identity in roster order.
    """

    table: str
    staging_key: str
    byte_length: int
    row_counts: tuple[tuple[str, int], ...]

    def __post_init__(self) -> None:
        if not _TABLE.fullmatch(self.table):
            raise _malformed("a staged unit names one generated table.")
        if not _STAGING_KEY.fullmatch(self.staging_key):
            raise _malformed("a staging key is 64 lowercase hex characters.")
        if not _positive(self.byte_length):
            raise _malformed("a staged unit's byte length is a positive integer.")
        if not self.row_counts:
            raise _malformed("a staged unit counts at least one identity.")
        identities: list[str] = []
        for identity, count in self.row_counts:
            if not identity.strip() or not _positive(count):
                raise _malformed("each staged identity is nonblank with a positive row count.")
            identities.append(identity)
        if len(set(identities)) != len(identities):
            raise _malformed("a staged unit counts each identity once.")

    @property
    def rows(self) -> int:
        """Return the unit's total staged rows."""
        return sum(count for _, count in self.row_counts)


def _malformed(description: str) -> InvariantError:
    return InvariantError(STAGING_COMPOSITION_SHAPE, description)


class StagingUnavailableError(DependencyError):
    """The staging store refused or failed a call; the unit retries on a later receive."""

    def __init__(self, description: str, *, context: dict[str, Any] | None = None) -> None:
        super().__init__(STAGING_UNAVAILABLE, description, context=context)


class UnitStager(Protocol):
    """Writes one staged unit's body once, or confirms the identical body is present."""

    def ensure(self, staged: StagedUnit, render: Callable[[], bytes]) -> None:
        """Make ``staged``'s object exist with exactly the bytes ``render`` returns."""
        ...
