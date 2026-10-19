# SPDX-License-Identifier: MIT-0
"""Read-only binding of logical measurement plans to exact Semantic V2 CURRENT publications."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

from domain.catalog.freshness import Coverage
from domain.data_contracts.contract import DataContract
from domain.measurement.planning import QueryPlan
from domain.measurement.publications import BoundQueryPlan
from domain.semantics.publication import SuccessfulPublication

__all__ = [
    "PinnedPublication",
    "PresentationEvidenceInspectionPlan",
    "PresentationEvidenceInspectionPort",
    "PresentationEvidenceInspectionRequest",
    "PresentationEvidenceInspectionResult",
    "PresentationEvidenceInspectionSlot",
    "PresentationEvidenceRole",
    "PublicationSelection",
    "PublicationSelectionExactPort",
    "PublicationSelectionExactRequest",
    "PublicationSelectionPort",
    "PublicationSelectionRequest",
]


class PresentationEvidenceRole(StrEnum):
    """A plan's exact responsibility within a presentation comparison."""

    CURRENT = "current"
    COMPARATOR = "comparator"


@dataclass(frozen=True, slots=True)
class PresentationEvidenceInspectionPlan:
    """One governed logical plan and its presentation-period role."""

    role: PresentationEvidenceRole
    plan: QueryPlan


@dataclass(frozen=True, slots=True)
class PresentationEvidenceInspectionRequest:
    """Ordered governed plans whose exact CURRENT evidence may be independently absent."""

    plans: tuple[PresentationEvidenceInspectionPlan, ...]
    contracts: tuple[DataContract, ...]
    now: datetime

    def __post_init__(self) -> None:
        if not self.plans or not self.contracts:
            raise ValueError(
                "presentation evidence inspection requires plans and contributing contracts."
            )
        if self.now.tzinfo is None:
            raise ValueError("presentation evidence inspection instant must be timezone-aware.")


@dataclass(frozen=True, slots=True)
class PresentationEvidenceInspectionSlot:
    """One request-aligned role that is either wholly attested or stably absent."""

    role: PresentationEvidenceRole
    bound: BoundQueryPlan | None
    coverage: Coverage | None = None
    qualified_dq: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        present = self.bound is not None
        if present != (self.coverage is not None):
            raise ValueError("present inspection evidence requires its exact coverage.")
        if not present and self.qualified_dq:
            raise ValueError("absent inspection evidence cannot carry publication caveats.")

    @property
    def present(self) -> bool:
        """Whether every required publication and its provenance were present."""
        return self.bound is not None


@dataclass(frozen=True, slots=True)
class PresentationEvidenceInspectionResult:
    """Ordered present/absent bound-plan slots aligned with the requested plans."""

    slots: tuple[PresentationEvidenceInspectionSlot, ...]

    def __post_init__(self) -> None:
        if not self.slots:
            raise ValueError("presentation evidence inspection requires at least one result slot.")


@runtime_checkable
class PresentationEvidenceInspectionPort(Protocol):
    """Inspects immutable CURRENT evidence without collapsing stable absence across plans."""

    def inspect(
        self,
        request: PresentationEvidenceInspectionRequest,
    ) -> PresentationEvidenceInspectionResult:
        """Resolve once and return one ordered whole-plan evidence slot per request plan."""
        ...


@dataclass(frozen=True, slots=True)
class PublicationSelectionRequest:
    """Every logical plan and contract required by one all-or-none measurement operation."""

    plans: tuple[QueryPlan, ...]
    contracts: tuple[DataContract, ...]
    now: datetime

    def __post_init__(self) -> None:
        if not self.plans or not self.contracts:
            raise ValueError("publication selection requires plans and contributing contracts.")
        if self.now.tzinfo is None:
            raise ValueError("publication selection instant must be timezone-aware.")


@dataclass(frozen=True, slots=True)
class PublicationSelection:
    """Bound plans plus coverage and caveats derived from those exact same successes."""

    plans: tuple[BoundQueryPlan, ...]
    coverage: Coverage
    qualified_dq: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.plans:
            raise ValueError("publication selection must bind at least one plan.")


@runtime_checkable
class PublicationSelectionPort(Protocol):
    """Selects immutable CURRENT publications without exposing query or mutation authority."""

    def select(self, request: PublicationSelectionRequest) -> PublicationSelection:
        """Bind all plans transactionally or fail before any analytical query executes."""
        ...


@dataclass(frozen=True, slots=True)
class PinnedPublication:
    """One immutable publication a durable bake source manifest names (plan 4.3, DB21)."""

    partition: str
    """The partition identity (the publications table partition key)."""
    publication_id: str
    fencing_token: int
    digest: str
    """The canonical digest of the attested `SuccessfulPublication`."""

    def __post_init__(self) -> None:
        if not self.partition.strip() or not self.publication_id.strip():
            raise ValueError("a pinned publication names its partition and publication.")
        if self.fencing_token < 1 or len(self.digest) != 64:  # noqa: PLR2004 -- sha-256 hex
            raise ValueError("a pinned publication carries a positive token and its digest.")


@dataclass(frozen=True, slots=True)
class PublicationSelectionExactRequest:
    """Resolve exactly the named immutable records; never CURRENT (plan 4.3, DB21)."""

    pins: tuple[PinnedPublication, ...]

    def __post_init__(self) -> None:
        if len({(pin.partition, pin.publication_id) for pin in self.pins}) != len(self.pins):
            raise ValueError("an exact selection names each publication once.")


@runtime_checkable
class PublicationSelectionExactPort(Protocol):
    """`select_exact`: the only selection a pinned bake task or retry may call (S5, DB21)."""

    def select_exact(
        self, request: PublicationSelectionExactRequest
    ) -> tuple[SuccessfulPublication, ...]:
        """Return each pinned publication, attested by digest, in request order."""
        ...
