# SPDX-License-Identifier: MIT-0
"""Outbound authority for immutable canonical candidate and successful physical versions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from domain.semantics.canonical_compiler import CanonicalRow, canonical_rows_checksum
from domain.semantics.publication import PublicationCandidate, SuccessfulPublication

__all__ = [
    "CanonicalDatasetReadPort",
    "CanonicalDatasetWritePort",
    "CanonicalDestination",
    "CanonicalMaterializeRequest",
    "CanonicalWriteResult",
]


class CanonicalDestination(StrEnum):
    """Physical lifecycle stage for one immutable canonical version."""

    CANDIDATE = "candidate"
    SUCCESSFUL = "successful"


@dataclass(frozen=True, slots=True)
class CanonicalMaterializeRequest:
    """Canonical rows plus the exact candidate or success envelope that owns them."""

    destination: CanonicalDestination
    publication: PublicationCandidate | SuccessfulPublication
    rows: tuple[CanonicalRow, ...]
    rule_set_version: str

    def __post_init__(self) -> None:
        evidence = self.publication.evidence
        if not self.rows and not (evidence.withheld_only or evidence.no_cell_returned):
            raise ValueError(
                "empty canonical materialization requires withheld-only or no-cell-returned "
                "publication evidence."
            )
        if self.rows and evidence.withheld_only:
            raise ValueError("withheld-only publication evidence requires empty canonical rows.")
        if self.rows and evidence.no_cell_returned:
            raise ValueError("no-cell-returned publication evidence requires empty canonical rows.")
        if not self.rule_set_version.strip():
            raise ValueError("canonical materialization requires a rule-set version.")
        if self.rule_set_version != self.publication.evidence.rule_set_version:
            raise ValueError(
                "canonical materialization rule set differs from publication evidence."
            )
        if canonical_rows_checksum(self.rows) != self.publication.content_checksum:
            raise ValueError("canonical materialization rows differ from publication checksum.")
        if self.destination is CanonicalDestination.CANDIDATE and not isinstance(
            self.publication, PublicationCandidate
        ):
            raise TypeError("candidate destination requires PublicationCandidate evidence.")
        if self.destination is CanonicalDestination.SUCCESSFUL and not isinstance(
            self.publication, SuccessfulPublication
        ):
            raise TypeError("successful destination requires SuccessfulPublication evidence.")


@dataclass(frozen=True, slots=True)
class CanonicalWriteResult:
    """Whether immutable physical content was created or already existed exactly."""

    accepted: bool
    created: bool
    row_count: int
    content_checksum: str
    reason: str = ""

    def __post_init__(self) -> None:
        if self.row_count < 0:
            raise ValueError("canonical write row count cannot be negative.")
        if not self.content_checksum.strip():
            raise ValueError("canonical write requires its content checksum.")
        if not self.accepted and not self.reason:
            raise ValueError("a refused canonical materialization must state why.")
        if self.created and not self.accepted:
            raise ValueError("a refused canonical materialization cannot be created.")


@runtime_checkable
class CanonicalDatasetReadPort(Protocol):
    """Exact immutable successful-row authority used to build complete dimension history."""

    def rows(self, publication: SuccessfulPublication) -> tuple[CanonicalRow, ...]:
        """Read and verify one exact successful publication's canonical rows."""
        ...


@runtime_checkable
class CanonicalDatasetWritePort(Protocol):
    """The sole canonical publisher's immutable physical mutation authority."""

    def materialize(self, request: CanonicalMaterializeRequest) -> CanonicalWriteResult:
        """Create one candidate/success version or accept an exact replay without SQL input."""
        ...

    def attest(self, request: CanonicalMaterializeRequest) -> CanonicalWriteResult:
        """Read and compare one exact immutable candidate/success without mutation."""
        ...
