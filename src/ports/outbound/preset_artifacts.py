# SPDX-License-Identifier: MIT-0
"""Outbound persistence boundaries for capability-free preset artifacts."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from domain.presentation.preset_artifacts import (
    ArtifactBodyWriteResult,
    CustodyManifestWriteResult,
    PresetArtifactBody,
    PresetArtifactCustodyManifest,
    PresetArtifactPage,
    PresetArtifactPointer,
    PresetArtifactPointerAdvanceRequest,
    PresetArtifactPointerAdvanceResult,
    PresetArtifactPointerRosterRequest,
    PresetArtifactPointerRosterResult,
    PresetArtifactPublicationRequest,
    PresetArtifactPublicationResult,
    PresetArtifactSiblingBody,
    PresetArtifactSiblingCustody,
    PresetArtifactSiblingCustodyWriteResult,
    PresetArtifactSiblingReadRequest,
    PresetArtifactSiblingWriteResult,
    PresetArtifactStageResult,
    PresetArtifactVariant,
)

__all__ = [
    "LegacyPointerAfterCutoverError",
    "PresetArtifactBodyStorePort",
    "PresetArtifactCustodyStorePort",
    "PresetArtifactPersistencePort",
    "PresetArtifactPointerStorePort",
    "PresetArtifactStagePort",
]


class LegacyPointerAfterCutoverError(Exception):
    """S6 §2.9 (AGG3-6): a grant-less pointer write refused by the graph mode `ConditionCheck`.

    The mode moved to `GRAPH` after the baker's batch read it. Nothing committed: the bake is
    acknowledged with zero effect and counted `LEGACY_BAKE_AFTER_CUTOVER`.
    """


@runtime_checkable
class PresetArtifactBodyStorePort(Protocol):
    """Persist and retrieve immutable revision-keyed preset JSON bodies."""

    def write(self, body: PresetArtifactBody) -> ArtifactBodyWriteResult:
        """Create one immutable body, or attest that exact bytes already exist."""
        ...

    def read(self, pointer: PresetArtifactPointer) -> PresetArtifactBody:
        """Read and attest the immutable JSON body named by one pointer."""
        ...

    def write_sibling(self, body: PresetArtifactSiblingBody) -> PresetArtifactSiblingWriteResult:
        """Create one immutable sibling body, or attest exact existing bytes."""
        ...

    def read_sibling(self, request: PresetArtifactSiblingReadRequest) -> PresetArtifactSiblingBody:
        """Read exactly one parent-indexed public sibling body."""
        ...


@runtime_checkable
class PresetArtifactCustodyStorePort(Protocol):
    """Persist and retrieve immutable private custody manifests beside public bodies."""

    def write(self, manifest: PresetArtifactCustodyManifest) -> CustodyManifestWriteResult:
        """Create one immutable custody manifest, or attest that exact bytes already exist."""
        ...

    def read(self, pointer: PresetArtifactPointer) -> PresetArtifactCustodyManifest:
        """Read and attest the custody manifest a custody-bearing pointer names."""
        ...

    def write_sibling(
        self, manifest: PresetArtifactSiblingCustody
    ) -> PresetArtifactSiblingCustodyWriteResult:
        """Create one immutable sibling custody body, or attest exact existing bytes."""
        ...

    def read_sibling(
        self, request: PresetArtifactSiblingReadRequest
    ) -> PresetArtifactSiblingCustody:
        """Read exactly one parent-indexed sibling custody body."""
        ...


@runtime_checkable
class PresetArtifactPointerStorePort(Protocol):
    """Read and conditionally advance shared page/variant artifact pointers."""

    def current(
        self, page: PresetArtifactPage, variant: PresetArtifactVariant
    ) -> PresetArtifactPointer | None:
        """Return one strongly read current pointer, or no pointer for precompute lag."""
        ...

    def roster(
        self, request: PresetArtifactPointerRosterRequest
    ) -> PresetArtifactPointerRosterResult:
        """Strongly read all declared CURRENT pointers in one ordered transaction."""
        ...

    def advance(
        self, request: PresetArtifactPointerAdvanceRequest
    ) -> PresetArtifactPointerAdvanceResult:
        """Advance one pointer only if its predecessor and forward token still match."""
        ...


@runtime_checkable
class PresetArtifactPersistencePort(Protocol):
    """Persist a rendered body before attempting its forward-only pointer transition."""

    def publish(self, request: PresetArtifactPublicationRequest) -> PresetArtifactPublicationResult:
        """Write immutable body evidence first, then conditionally advance its pointer."""
        ...


@runtime_checkable
class PresetArtifactStagePort(Protocol):
    """Plan 4.5: write and attest one revision's immutable objects with no pointer capability.

    A GRAPH bake task holds only this port; the finalizer alone constructs pointer members.
    """

    def stage(self, request: PresetArtifactPublicationRequest) -> PresetArtifactStageResult:
        """Write siblings (bounded body->custody pairs), then the parent body and custody."""
        ...
