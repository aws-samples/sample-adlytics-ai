# SPDX-License-Identifier: MIT-0
"""Resolve source-aligned inputs into one owner-authorized canonical publication plan.

This is the pure seam between source interpretation and canonical mutation. It deliberately does not
carry SQL or source-specific mapping logic: adapters materialize and retrieve source publications,
the canonical owner later transforms them, and a write adapter applies an authorized plan.
"""

from __future__ import annotations

from dataclasses import dataclass

from domain.semantics.publication import (
    CanonicalPublisherAuthority,
    CanonicalPublisherKey,
    PartitionIdentity,
    PublicationGraph,
    SourcePublicationRef,
)
from domain.semantics.source_aligned import (
    SourceAlignedPublication,
    resolve_source_inputs,
)

__all__ = [
    "CanonicalCurationPlan",
    "CanonicalCurationRequest",
    "plan_curation",
]


@dataclass(frozen=True, slots=True)
class CanonicalCurationRequest:
    """Requested canonical partition, owner, source refs, and resolvable source evidence."""

    partition: PartitionIdentity
    publisher: CanonicalPublisherKey
    source_references: tuple[SourcePublicationRef, ...]
    available_sources: tuple[SourceAlignedPublication, ...]
    graph: PublicationGraph


@dataclass(frozen=True, slots=True)
class CanonicalCurationPlan:
    """Exactly one canonical apply command backed by resolved immutable source publications."""

    partition: PartitionIdentity
    authority: CanonicalPublisherAuthority
    source_publications: tuple[SourceAlignedPublication, ...]

    def __post_init__(self) -> None:
        if self.authority.dataset != self.partition.scope.dataset:
            raise ValueError("canonical publisher authority is for another dataset.")
        if not self.source_publications:
            raise ValueError("canonical curation plan requires resolved source publications.")

    @property
    def publisher(self) -> CanonicalPublisherKey:
        """Canonical component authorized by the validated graph."""
        return self.authority.publisher

    @property
    def source_references(self) -> tuple[SourcePublicationRef, ...]:
        """Stable references in the owner-requested source order."""
        return tuple(publication.reference for publication in self.source_publications)


def plan_curation(request: CanonicalCurationRequest) -> CanonicalCurationPlan:
    """Authorize the sole owner and resolve every source input before canonical work begins."""
    authority = request.graph.authorize(request.partition.scope.dataset, request.publisher)
    resolved = resolve_source_inputs(request.source_references, request.available_sources)
    return CanonicalCurationPlan(
        partition=request.partition,
        authority=authority,
        source_publications=resolved,
    )
