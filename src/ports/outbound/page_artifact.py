# SPDX-License-Identifier: MIT-0
"""Immutable typed artifact and selection-capability custody boundaries."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from domain.presentation.artifacts import (
    CampaignSelectionCapabilityReadRequest,
    CampaignSelectionCapabilityRecord,
    CampaignSelectionCapabilityValidationResult,
    ChannelRowCapabilityReadRequest,
    ChannelRowCapabilityRecord,
    ChannelRowCapabilityValidationResult,
    MediaScopeCapabilityReadRequest,
    MediaScopeCapabilityRecord,
    MediaScopeCapabilityValidationResult,
    MediaWindowCapabilityReadRequest,
    MediaWindowCapabilityRecord,
    MediaWindowCapabilityValidationResult,
    PageArtifactReadRequest,
    PageArtifactRecord,
    PageArtifactValidationResult,
    ParentCapabilityIssuanceRequest,
    ParentCapabilityIssuanceResult,
    ProductSelectionCapabilityReadRequest,
    ProductSelectionCapabilityRecord,
    ProductSelectionCapabilityValidationResult,
    VisualNarrationReferenceReadRequest,
    VisualNarrationReferenceRecord,
    VisualNarrationReferenceValidationResult,
)

__all__ = [
    "ArtifactCapabilityStorePort",
    "PageArtifactStorePort",
    "ParentCapabilityIssuancePort",
    "SelectionCapabilityStorePort",
    "VisualNarrationReferenceStorePort",
]


@runtime_checkable
class PageArtifactStorePort(Protocol):
    """Store and strongly read exact typed artifacts behind opaque references."""

    def put_artifact(self, record: PageArtifactRecord) -> None:
        """Create one immutable owner/session-bound artifact record."""
        ...

    def read_artifact(self, request: PageArtifactReadRequest) -> PageArtifactValidationResult:
        """Return exact validated artifact custody or invalid context."""
        ...


@runtime_checkable
class ParentCapabilityIssuancePort(Protocol):
    """Atomically create one parent artifact and its complete capability set."""

    def issue_parent_capabilities(
        self, request: ParentCapabilityIssuanceRequest
    ) -> ParentCapabilityIssuanceResult:
        """Commit all supplied immutable records, or refuse before a capacity overflow."""
        ...


@runtime_checkable
class SelectionCapabilityStorePort(Protocol):
    """Issue and redeem immutable parent-bound selection capabilities."""

    def put_campaign_capability(self, record: CampaignSelectionCapabilityRecord) -> None:
        """Create one immutable parent-bound campaign selection capability."""
        ...

    def read_campaign_capability(
        self, request: CampaignSelectionCapabilityReadRequest
    ) -> CampaignSelectionCapabilityValidationResult:
        """Redeem opaque campaign authority under exact owner/session/parent custody."""
        ...

    def put_channel_capability(self, record: ChannelRowCapabilityRecord) -> None:
        """Create one immutable parent-bound channel row capability."""
        ...

    def read_channel_capability(
        self, request: ChannelRowCapabilityReadRequest
    ) -> ChannelRowCapabilityValidationResult:
        """Redeem opaque row authority under exact owner/session/parent custody."""
        ...

    def put_product_capability(self, record: ProductSelectionCapabilityRecord) -> None:
        """Create one immutable parent-bound product selection capability."""
        ...

    def read_product_capability(
        self, request: ProductSelectionCapabilityReadRequest
    ) -> ProductSelectionCapabilityValidationResult:
        """Redeem opaque product authority under exact owner/session/parent custody."""
        ...

    def put_media_scope_capability(self, record: MediaScopeCapabilityRecord) -> None:
        """Create one immutable parent-bound media scope capability."""
        ...

    def read_media_scope_capability(
        self, request: MediaScopeCapabilityReadRequest
    ) -> MediaScopeCapabilityValidationResult:
        """Redeem opaque media scope authority under exact owner/session/parent custody."""
        ...

    def put_media_window_capability(self, record: MediaWindowCapabilityRecord) -> None:
        """Create one immutable parent-bound media window capability."""
        ...

    def read_media_window_capability(
        self, request: MediaWindowCapabilityReadRequest
    ) -> MediaWindowCapabilityValidationResult:
        """Redeem opaque media window authority under exact owner/session/parent custody."""
        ...


@runtime_checkable
class ArtifactCapabilityStorePort(PageArtifactStorePort, SelectionCapabilityStorePort, Protocol):
    """Store artifacts and their parent-bound selection capabilities."""


@runtime_checkable
class VisualNarrationReferenceStorePort(Protocol):
    """Issue and redeem one-visual narration context capabilities."""

    def put_visual_narration_reference(self, record: VisualNarrationReferenceRecord) -> None:
        """Create one immutable parent-bound visual narration reference."""
        ...

    def read_visual_narration_reference(
        self, request: VisualNarrationReferenceReadRequest
    ) -> VisualNarrationReferenceValidationResult:
        """Redeem one visual narration authority under exact custody."""
        ...
