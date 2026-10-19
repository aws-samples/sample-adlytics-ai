# SPDX-License-Identifier: MIT-0
"""Durable page artifacts and owner/session-bound selection capabilities."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import cast

from domain.presentation.campaign_attribution import SafeCampaignDisplaySignature
from domain.presentation.retail_product_detail import SafeProductDisplaySignature
from domain.presentation.temporal import (
    TemporalComparison,
    TemporalComparisonPolicy,
    TemporalComparisonStatus,
    TemporalControl,
    TemporalResolved,
    VintageControl,
)
from domain.semantics.analytical_resolution import ResolutionOutcome
from domain.semantics.identity import (
    CampaignKey,
    ChannelKey,
    CommerceScopeKey,
    MarketplaceKey,
    PlatformKey,
    ProductKey,
)
from domain.semantics.page_context import PageContextReference, ReportingWindowStatus
from domain.shared_kernel.provenance import MeasurementBasis
from domain.shared_kernel.values import DateRange

_ARTIFACT_TYPE = re.compile(r"^[A-Z][A-Za-z0-9]{2,80}$")
_MODEL_ID = re.compile(r"^[a-z][a-z0-9_]{1,80}$")
_VISUAL_ID = re.compile(r"^[a-z][a-z0-9-]{1,80}$")
MAX_ARTIFACT_BYTES = 256_000
MAX_SAFE_SCOPE_LABEL_LENGTH = 256


def canonical_artifact_json(value: dict[str, object]) -> str:
    """Render exact stable JSON for immutable artifact custody."""
    if not value:
        raise ValueError("page artifact payload cannot be empty.")
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


@dataclass(frozen=True, slots=True)
class PageArtifact:
    """One closed page artifact identity and canonical serialized payload."""

    artifact_type: str
    artifact_version: int
    payload_json: str

    def __post_init__(self) -> None:
        if not _ARTIFACT_TYPE.fullmatch(self.artifact_type) or self.artifact_version < 1:
            raise ValueError("page artifact type/version is invalid.")
        if not self.payload_json or len(self.payload_json.encode()) > MAX_ARTIFACT_BYTES:
            raise ValueError("page artifact payload is empty or too large.")
        try:
            raw: object = json.loads(self.payload_json)
        except ValueError:
            raise ValueError("page artifact payload is not JSON.") from None
        if (
            not isinstance(raw, dict)
            or canonical_artifact_json(cast("dict[str, object]", raw)) != self.payload_json
        ):
            raise ValueError("page artifact payload must be a canonical JSON object.")

    @property
    def payload(self) -> dict[str, object]:
        """Decode the already-attested canonical durable payload."""
        return cast("dict[str, object]", json.loads(self.payload_json))


@dataclass(frozen=True, slots=True)
class PageArtifactRecord:
    """Artifact bytes bound to owner/session/expiry and an optional parent revision."""

    reference: PageContextReference
    artifact: PageArtifact
    verified_owner_id: str
    conversation_session_id: str
    expires_at: datetime
    parent_reference: PageContextReference | None = None
    temporal_control: TemporalControl | VintageControl | None = None

    def __post_init__(self) -> None:
        if not isinstance(cast("object", self.reference), PageContextReference):
            raise TypeError("page artifact record requires an opaque page reference.")
        if not isinstance(cast("object", self.artifact), PageArtifact):
            raise TypeError("page artifact record requires a PageArtifact.")
        if not self.verified_owner_id.strip() or not self.conversation_session_id.strip():
            raise ValueError("page artifact owner and conversation session are required.")
        if not _is_aware(self.expires_at):
            raise ValueError("page artifact expiry must be timezone-aware.")
        if self.parent_reference is not None:
            if not isinstance(cast("object", self.parent_reference), PageContextReference):
                raise TypeError("page artifact parent must be an opaque page reference.")
            if self.parent_reference == self.reference:
                raise ValueError("page artifact cannot name itself as its parent.")
        if self.temporal_control is not None and type(self.temporal_control) not in {
            TemporalControl,
            VintageControl,
        }:
            raise TypeError("page artifact temporal control must be a supported temporal control.")


@dataclass(frozen=True, slots=True)
class PageArtifactReadRequest:
    """Exact custody values used to read one durable page artifact."""

    reference: PageContextReference
    verified_owner_id: str
    conversation_session_id: str
    at: datetime

    def __post_init__(self) -> None:
        if not isinstance(cast("object", self.reference), PageContextReference):
            raise TypeError("page artifact read requires an opaque page reference.")
        if not self.verified_owner_id.strip() or not self.conversation_session_id.strip():
            raise ValueError("page artifact read owner and session are required.")
        if not _is_aware(self.at):
            raise ValueError("page artifact read instant must be timezone-aware.")


@dataclass(frozen=True, slots=True, init=False)
class ServerValidatedPageArtifact:
    """Artifact whose reference, owner, session, parent, and expiry were validated."""

    reference: PageContextReference
    artifact: PageArtifact
    expires_at: datetime
    parent_reference: PageContextReference | None
    temporal_control: TemporalControl | VintageControl | None

    def __init__(self) -> None:
        raise TypeError("ServerValidatedPageArtifact can only be created by validation.")


@dataclass(frozen=True, slots=True)
class PageArtifactValidationResult:
    """Resolved artifact or one nondisclosing invalid-context outcome."""

    outcome: ResolutionOutcome
    artifact: ServerValidatedPageArtifact | None = None

    def __post_init__(self) -> None:
        if self.outcome not in {ResolutionOutcome.RESOLVED, ResolutionOutcome.INVALID_CONTEXT}:
            raise ValueError("page artifact validation outcome is unsupported.")
        if (self.artifact is not None) != (self.outcome is ResolutionOutcome.RESOLVED):
            raise ValueError("only resolved page artifact validation carries an artifact.")


def validate_page_artifact(
    record: PageArtifactRecord | None,
    request: PageArtifactReadRequest,
) -> PageArtifactValidationResult:
    """Validate artifact custody without disclosing which exact check failed."""
    if (
        record is None
        or record.reference != request.reference
        or record.verified_owner_id != request.verified_owner_id
        or record.conversation_session_id != request.conversation_session_id
        or record.expires_at <= request.at
    ):
        return PageArtifactValidationResult(ResolutionOutcome.INVALID_CONTEXT)
    validated = object.__new__(ServerValidatedPageArtifact)
    for name in (
        "reference",
        "artifact",
        "expires_at",
        "parent_reference",
        "temporal_control",
    ):
        object.__setattr__(validated, name, getattr(record, name))
    return PageArtifactValidationResult(ResolutionOutcome.RESOLVED, validated)


def page_artifact_parent_is_valid(child: PageArtifactRecord, parent: PageArtifactRecord) -> bool:
    """Return whether one immutable child is in the parent's custody and lifetime."""
    return (
        child.parent_reference == parent.reference
        and child.verified_owner_id == parent.verified_owner_id
        and child.conversation_session_id == parent.conversation_session_id
        and child.expires_at <= parent.expires_at
    )


@dataclass(frozen=True, slots=True)
class ChannelRowCapabilityRecord:
    """Opaque authority for one exact channel row and parent artifact revision."""

    reference: PageContextReference
    parent_artifact_reference: PageContextReference
    parent_artifact_type: str
    parent_artifact_version: int
    verified_owner_id: str
    conversation_session_id: str
    expires_at: datetime
    channel: ChannelKey
    marketplace: MarketplaceKey
    current_period: DateRange
    model_ids: tuple[str, ...]
    reporting_window_status: ReportingWindowStatus = ReportingWindowStatus.USER_SELECTED

    def __post_init__(self) -> None:
        _validate_capability_custody(
            self.reference,
            self.parent_artifact_reference,
            self.parent_artifact_type,
            self.parent_artifact_version,
            self.verified_owner_id,
            self.conversation_session_id,
            self.expires_at,
            "channel",
        )
        if not isinstance(cast("object", self.channel), ChannelKey):
            raise TypeError("channel capability requires a canonical ChannelKey.")
        if not isinstance(cast("object", self.marketplace), MarketplaceKey):
            raise TypeError("channel capability requires a canonical MarketplaceKey.")
        if not isinstance(cast("object", self.current_period), DateRange):
            raise TypeError("channel capability current period must be a DateRange.")
        if not isinstance(cast("object", self.reporting_window_status), ReportingWindowStatus):
            raise TypeError("channel capability reporting-window status is invalid.")
        if (
            not isinstance(cast("object", self.model_ids), tuple)
            or not self.model_ids
            or len(set(self.model_ids)) != len(self.model_ids)
            or any(not _MODEL_ID.fullmatch(model_id) for model_id in self.model_ids)
        ):
            raise ValueError("channel capability model set is invalid.")


@dataclass(frozen=True, slots=True)
class ChannelRowCapabilityReadRequest:
    """Opaque channel capability reference plus verified custody and observation time."""

    reference: PageContextReference
    verified_owner_id: str
    conversation_session_id: str
    at: datetime

    def __post_init__(self) -> None:
        _validate_capability_read(
            self.reference,
            self.verified_owner_id,
            self.conversation_session_id,
            self.at,
            "channel",
        )


@dataclass(frozen=True, slots=True, init=False)
class ServerValidatedChannelRowCapability:
    """Channel row authority validated with its exact parent artifact revision."""

    reference: PageContextReference
    parent_artifact_reference: PageContextReference
    parent_artifact_type: str
    parent_artifact_version: int
    expires_at: datetime
    channel: ChannelKey
    marketplace: MarketplaceKey
    current_period: DateRange
    model_ids: tuple[str, ...]
    reporting_window_status: ReportingWindowStatus

    def __init__(self) -> None:
        raise TypeError("ServerValidatedChannelRowCapability can only be created by validation.")


@dataclass(frozen=True, slots=True)
class ChannelRowCapabilityValidationResult:
    """Resolved channel authority or one nondisclosing invalid-context outcome."""

    outcome: ResolutionOutcome
    capability: ServerValidatedChannelRowCapability | None = None

    def __post_init__(self) -> None:
        _validate_capability_result(self.outcome, self.capability, "channel")


def channel_capability_parent_is_valid(
    capability: ChannelRowCapabilityRecord,
    parent: PageArtifactRecord,
) -> bool:
    """Return whether a row capability names and cannot outlive its parent artifact."""
    return _capability_parent_is_valid(capability, parent)


def validate_channel_row_capability(
    record: ChannelRowCapabilityRecord | None,
    request: ChannelRowCapabilityReadRequest,
    parent: ServerValidatedPageArtifact | None,
) -> ChannelRowCapabilityValidationResult:
    """Validate row and parent custody without disclosing the failed check."""
    if not _capability_is_valid(record, request, parent):
        return ChannelRowCapabilityValidationResult(ResolutionOutcome.INVALID_CONTEXT)
    validated = object.__new__(ServerValidatedChannelRowCapability)
    for name in (
        "reference",
        "parent_artifact_reference",
        "parent_artifact_type",
        "parent_artifact_version",
        "expires_at",
        "channel",
        "marketplace",
        "current_period",
        "model_ids",
        "reporting_window_status",
    ):
        object.__setattr__(validated, name, getattr(record, name))
    return ChannelRowCapabilityValidationResult(ResolutionOutcome.RESOLVED, validated)


@dataclass(frozen=True, slots=True)
class CampaignSelectionCapabilityRecord:
    """Opaque authority for one campaign choice and exact parent artifact revision."""

    reference: PageContextReference
    parent_artifact_reference: PageContextReference
    parent_artifact_type: str
    parent_artifact_version: int
    verified_owner_id: str
    conversation_session_id: str
    expires_at: datetime
    campaign: CampaignKey
    channel: ChannelKey
    marketplace: MarketplaceKey
    safe_signature: SafeCampaignDisplaySignature
    reporting_window_status: ReportingWindowStatus = ReportingWindowStatus.USER_SELECTED

    def __post_init__(self) -> None:
        _validate_capability_custody(
            self.reference,
            self.parent_artifact_reference,
            self.parent_artifact_type,
            self.parent_artifact_version,
            self.verified_owner_id,
            self.conversation_session_id,
            self.expires_at,
            "campaign",
        )
        if not isinstance(cast("object", self.campaign), CampaignKey):
            raise TypeError("campaign capability requires a canonical CampaignKey.")
        if not isinstance(cast("object", self.channel), ChannelKey):
            raise TypeError("campaign capability requires a canonical ChannelKey.")
        if not isinstance(cast("object", self.marketplace), MarketplaceKey):
            raise TypeError("campaign capability requires a canonical MarketplaceKey.")
        if not isinstance(cast("object", self.safe_signature), SafeCampaignDisplaySignature):
            raise TypeError("campaign capability requires one exact safe signature.")
        if not isinstance(cast("object", self.reporting_window_status), ReportingWindowStatus):
            raise TypeError("campaign capability reporting-window status is invalid.")


@dataclass(frozen=True, slots=True)
class CampaignSelectionCapabilityReadRequest:
    """Opaque campaign capability plus verified custody and observation instant."""

    reference: PageContextReference
    verified_owner_id: str
    conversation_session_id: str
    at: datetime

    def __post_init__(self) -> None:
        _validate_capability_read(
            self.reference,
            self.verified_owner_id,
            self.conversation_session_id,
            self.at,
            "campaign",
        )


@dataclass(frozen=True, slots=True, init=False)
class ServerValidatedCampaignSelectionCapability:
    """Campaign authority validated with its exact parent artifact revision."""

    reference: PageContextReference
    parent_artifact_reference: PageContextReference
    parent_artifact_type: str
    parent_artifact_version: int
    expires_at: datetime
    campaign: CampaignKey
    channel: ChannelKey
    marketplace: MarketplaceKey
    safe_signature: SafeCampaignDisplaySignature
    reporting_window_status: ReportingWindowStatus

    def __init__(self) -> None:
        raise TypeError(
            "ServerValidatedCampaignSelectionCapability can only be created by validation."
        )


@dataclass(frozen=True, slots=True)
class CampaignSelectionCapabilityValidationResult:
    """Resolved campaign authority or one nondisclosing invalid-context outcome."""

    outcome: ResolutionOutcome
    capability: ServerValidatedCampaignSelectionCapability | None = None

    def __post_init__(self) -> None:
        _validate_capability_result(self.outcome, self.capability, "campaign")


def campaign_capability_parent_is_valid(
    capability: CampaignSelectionCapabilityRecord,
    parent: PageArtifactRecord,
) -> bool:
    """Return whether a campaign capability names and cannot outlive its parent."""
    return _capability_parent_is_valid(capability, parent)


def validate_campaign_selection_capability(
    record: CampaignSelectionCapabilityRecord | None,
    request: CampaignSelectionCapabilityReadRequest,
    parent: ServerValidatedPageArtifact | None,
) -> CampaignSelectionCapabilityValidationResult:
    """Validate campaign choice and parent custody as one nondisclosing operation."""
    if not _capability_is_valid(record, request, parent):
        return CampaignSelectionCapabilityValidationResult(ResolutionOutcome.INVALID_CONTEXT)
    validated = object.__new__(ServerValidatedCampaignSelectionCapability)
    for name in (
        "reference",
        "parent_artifact_reference",
        "parent_artifact_type",
        "parent_artifact_version",
        "expires_at",
        "campaign",
        "channel",
        "marketplace",
        "safe_signature",
        "reporting_window_status",
    ):
        object.__setattr__(validated, name, getattr(record, name))
    return CampaignSelectionCapabilityValidationResult(ResolutionOutcome.RESOLVED, validated)


def _validate_capability_custody(  # noqa: PLR0913, PLR0917
    reference: PageContextReference,
    parent_reference: PageContextReference,
    parent_type: str,
    parent_version: int,
    owner: str,
    session: str,
    expires_at: datetime,
    kind: str,
) -> None:
    if not isinstance(cast("object", reference), PageContextReference):
        raise TypeError(f"{kind} capability requires an opaque reference.")
    if not isinstance(cast("object", parent_reference), PageContextReference):
        raise TypeError(f"{kind} capability requires an opaque parent artifact reference.")
    if reference == parent_reference:
        raise ValueError(f"{kind} capability and parent artifact references must differ.")
    if not _ARTIFACT_TYPE.fullmatch(parent_type) or parent_version < 1:
        raise ValueError(f"{kind} capability parent artifact identity is invalid.")
    if not owner.strip() or not session.strip():
        raise ValueError(f"{kind} capability owner and session are required.")
    if not _is_aware(expires_at):
        raise ValueError(f"{kind} capability expiry must be timezone-aware.")


def _validate_capability_read(  # noqa: PLR0913, PLR0917
    reference: PageContextReference,
    owner: str,
    session: str,
    at: datetime,
    kind: str,
) -> None:
    if not isinstance(cast("object", reference), PageContextReference):
        raise TypeError(f"{kind} capability read requires an opaque reference.")
    if not owner.strip() or not session.strip():
        raise ValueError(f"{kind} capability read owner and session are required.")
    if not _is_aware(at):
        raise ValueError(f"{kind} capability read instant must be timezone-aware.")


def _validate_capability_result(
    outcome: ResolutionOutcome,
    capability: object | None,
    kind: str,
) -> None:
    if outcome not in {ResolutionOutcome.RESOLVED, ResolutionOutcome.INVALID_CONTEXT}:
        raise ValueError(f"{kind} capability validation outcome is unsupported.")
    if (capability is not None) != (outcome is ResolutionOutcome.RESOLVED):
        raise ValueError(f"only resolved {kind} capability validation carries authority.")


def _capability_parent_is_valid(
    capability: ChannelRowCapabilityRecord | CampaignSelectionCapabilityRecord,
    parent: PageArtifactRecord,
) -> bool:
    return (
        capability.parent_artifact_reference == parent.reference
        and capability.parent_artifact_type == parent.artifact.artifact_type
        and capability.parent_artifact_version == parent.artifact.artifact_version
        and capability.verified_owner_id == parent.verified_owner_id
        and capability.conversation_session_id == parent.conversation_session_id
        and capability.expires_at <= parent.expires_at
    )


def _capability_is_valid(
    record: ChannelRowCapabilityRecord | CampaignSelectionCapabilityRecord | None,
    request: ChannelRowCapabilityReadRequest | CampaignSelectionCapabilityReadRequest,
    parent: ServerValidatedPageArtifact | None,
) -> bool:
    return not (
        record is None
        or parent is None
        or record.reference != request.reference
        or record.verified_owner_id != request.verified_owner_id
        or record.conversation_session_id != request.conversation_session_id
        or record.expires_at <= request.at
        or record.parent_artifact_reference != parent.reference
        or record.parent_artifact_type != parent.artifact.artifact_type
        or record.parent_artifact_version != parent.artifact.artifact_version
        or record.expires_at > parent.expires_at
    )


def _is_aware(value: datetime) -> bool:
    return (
        isinstance(cast("object", value), datetime)
        and value.tzinfo is not None
        and value.utcoffset() is not None
    )


@dataclass(frozen=True, slots=True)
class MediaScope:
    """Canonical, server-only selector for one governed media scope."""

    commerce_scope: CommerceScopeKey
    platform: PlatformKey
    channel: ChannelKey
    marketplace: MarketplaceKey

    def __post_init__(self) -> None:
        for value, expected, label in (
            (self.commerce_scope, CommerceScopeKey, "commerce scope"),
            (self.platform, PlatformKey, "platform"),
            (self.channel, ChannelKey, "channel"),
            (self.marketplace, MarketplaceKey, "marketplace"),
        ):
            if not isinstance(cast("object", value), expected):
                raise TypeError(f"media scope requires a canonical {label}.")


@dataclass(frozen=True, slots=True)
class SafeMediaScopeDisplaySignature:
    """The one browser-safe label for a media scope capability."""

    label: str

    def __post_init__(self) -> None:
        if not self.label.strip() or len(self.label) > MAX_SAFE_SCOPE_LABEL_LENGTH:
            raise ValueError("media scope display label is invalid.")


@dataclass(frozen=True, slots=True)
class ProductSelectionCapabilityRecord:
    """Opaque authority for one product choice and exact parent artifact revision."""

    reference: PageContextReference
    parent_artifact_reference: PageContextReference
    parent_artifact_type: str
    parent_artifact_version: int
    verified_owner_id: str
    conversation_session_id: str
    expires_at: datetime
    product: ProductKey
    safe_signature: SafeProductDisplaySignature
    catalog_identity: str

    def __post_init__(self) -> None:
        _validate_capability_custody(
            self.reference,
            self.parent_artifact_reference,
            self.parent_artifact_type,
            self.parent_artifact_version,
            self.verified_owner_id,
            self.conversation_session_id,
            self.expires_at,
            "product",
        )
        if not isinstance(cast("object", self.product), ProductKey):
            raise TypeError("product capability requires a canonical ProductKey.")
        if not isinstance(cast("object", self.safe_signature), SafeProductDisplaySignature):
            raise TypeError("product capability requires one exact safe signature.")
        if not self.catalog_identity.strip():
            raise ValueError("product capability catalog identity is required.")


@dataclass(frozen=True, slots=True)
class ProductSelectionCapabilityReadRequest:
    """Opaque product capability reference plus exact custody and observation time."""

    reference: PageContextReference
    verified_owner_id: str
    conversation_session_id: str
    at: datetime

    def __post_init__(self) -> None:
        _validate_capability_read(
            self.reference,
            self.verified_owner_id,
            self.conversation_session_id,
            self.at,
            "product",
        )


@dataclass(frozen=True, slots=True, init=False)
class ServerValidatedProductSelectionCapability:
    """Product authority validated with its exact parent artifact revision."""

    reference: PageContextReference
    parent_artifact_reference: PageContextReference
    parent_artifact_type: str
    parent_artifact_version: int
    expires_at: datetime
    product: ProductKey
    safe_signature: SafeProductDisplaySignature
    catalog_identity: str

    def __init__(self) -> None:
        raise TypeError(
            "ServerValidatedProductSelectionCapability can only be created by validation."
        )


@dataclass(frozen=True, slots=True)
class ProductSelectionCapabilityValidationResult:
    """Resolved product authority or one nondisclosing invalid-context outcome."""

    outcome: ResolutionOutcome
    capability: ServerValidatedProductSelectionCapability | None = None

    def __post_init__(self) -> None:
        _validate_capability_result(self.outcome, self.capability, "product")


@dataclass(frozen=True, slots=True)
class MediaScopeCapabilityRecord:
    """Opaque authority for one safe media scope and exact parent revision."""

    reference: PageContextReference
    parent_artifact_reference: PageContextReference
    parent_artifact_type: str
    parent_artifact_version: int
    verified_owner_id: str
    conversation_session_id: str
    expires_at: datetime
    scope: MediaScope
    safe_signature: SafeMediaScopeDisplaySignature
    catalog_identity: str

    def __post_init__(self) -> None:
        _validate_capability_custody(
            self.reference,
            self.parent_artifact_reference,
            self.parent_artifact_type,
            self.parent_artifact_version,
            self.verified_owner_id,
            self.conversation_session_id,
            self.expires_at,
            "media scope",
        )
        if not isinstance(cast("object", self.scope), MediaScope):
            raise TypeError("media scope capability requires a canonical MediaScope.")
        if not isinstance(cast("object", self.safe_signature), SafeMediaScopeDisplaySignature):
            raise TypeError("media scope capability requires one exact safe signature.")
        if not self.catalog_identity.strip():
            raise ValueError("media scope capability catalog identity is required.")


@dataclass(frozen=True, slots=True)
class MediaScopeCapabilityReadRequest:
    """Opaque media-scope capability plus verified custody and observation instant."""

    reference: PageContextReference
    verified_owner_id: str
    conversation_session_id: str
    at: datetime

    def __post_init__(self) -> None:
        _validate_capability_read(
            self.reference,
            self.verified_owner_id,
            self.conversation_session_id,
            self.at,
            "media scope",
        )


@dataclass(frozen=True, slots=True, init=False)
class ServerValidatedMediaScopeCapability:
    """Media scope authority validated with its exact parent artifact revision."""

    reference: PageContextReference
    parent_artifact_reference: PageContextReference
    parent_artifact_type: str
    parent_artifact_version: int
    expires_at: datetime
    scope: MediaScope
    safe_signature: SafeMediaScopeDisplaySignature
    catalog_identity: str

    def __init__(self) -> None:
        raise TypeError("ServerValidatedMediaScopeCapability can only be created by validation.")


@dataclass(frozen=True, slots=True)
class MediaScopeCapabilityValidationResult:
    """Resolved media-scope authority or one nondisclosing invalid-context outcome."""

    outcome: ResolutionOutcome
    capability: ServerValidatedMediaScopeCapability | None = None

    def __post_init__(self) -> None:
        _validate_capability_result(self.outcome, self.capability, "media scope")


@dataclass(frozen=True, slots=True)
class MediaWindowCapabilityRecord:
    """Opaque authority for one exact resolved media window and comparison state."""

    reference: PageContextReference
    parent_artifact_reference: PageContextReference
    parent_artifact_type: str
    parent_artifact_version: int
    verified_owner_id: str
    conversation_session_id: str
    expires_at: datetime
    resolved: TemporalResolved
    compare_previous: bool
    comparison: TemporalComparison

    def __post_init__(self) -> None:
        _validate_capability_custody(
            self.reference,
            self.parent_artifact_reference,
            self.parent_artifact_type,
            self.parent_artifact_version,
            self.verified_owner_id,
            self.conversation_session_id,
            self.expires_at,
            "media window",
        )
        if type(self.resolved) is not TemporalResolved:
            raise TypeError("media window capability requires an exact TemporalResolved value.")
        if type(self.compare_previous) is not bool:
            raise TypeError("media window compare_previous must be a bool.")
        if type(self.comparison) is not TemporalComparison:
            raise TypeError("media window capability requires a TemporalComparison.")
        if self.comparison.policy is not TemporalComparisonPolicy.OPTIONAL_EQUAL_PRIOR:
            raise ValueError("media window comparison policy must be optional equal prior.")
        if self.compare_previous != (
            self.comparison.status is not TemporalComparisonStatus.NOT_REQUESTED
        ):
            raise ValueError("media window comparison intent and status disagree.")


@dataclass(frozen=True, slots=True)
class MediaWindowCapabilityReadRequest:
    """Opaque media-window capability plus verified custody and observation instant."""

    reference: PageContextReference
    verified_owner_id: str
    conversation_session_id: str
    at: datetime

    def __post_init__(self) -> None:
        _validate_capability_read(
            self.reference,
            self.verified_owner_id,
            self.conversation_session_id,
            self.at,
            "media window",
        )


@dataclass(frozen=True, slots=True, init=False)
class ServerValidatedMediaWindowCapability:
    """Media window authority validated with its parent revision and exact resolution."""

    reference: PageContextReference
    parent_artifact_reference: PageContextReference
    parent_artifact_type: str
    parent_artifact_version: int
    expires_at: datetime
    resolved: TemporalResolved
    compare_previous: bool
    comparison: TemporalComparison

    def __init__(self) -> None:
        raise TypeError("ServerValidatedMediaWindowCapability can only be created by validation.")


@dataclass(frozen=True, slots=True)
class MediaWindowCapabilityValidationResult:
    """Resolved media-window authority or one nondisclosing invalid-context outcome."""

    outcome: ResolutionOutcome
    capability: ServerValidatedMediaWindowCapability | None = None

    def __post_init__(self) -> None:
        _validate_capability_result(self.outcome, self.capability, "media window")


@dataclass(frozen=True, slots=True)
class VisualNarrationReferenceRecord:
    """Opaque one-visual narration authority bound to immutable rendered evidence."""

    reference: PageContextReference
    parent_artifact_reference: PageContextReference
    parent_artifact_type: str
    parent_artifact_version: int
    verified_owner_id: str
    conversation_session_id: str
    expires_at: datetime
    visual_id: str
    metric_cohort: tuple[str, ...]
    safe_scope_label: str
    resolved: TemporalResolved
    basis: MeasurementBasis
    qualification: tuple[str, ...]
    allowed_action_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _validate_capability_custody(
            self.reference,
            self.parent_artifact_reference,
            self.parent_artifact_type,
            self.parent_artifact_version,
            self.verified_owner_id,
            self.conversation_session_id,
            self.expires_at,
            "visual narration",
        )
        if not _VISUAL_ID.fullmatch(self.visual_id):
            raise ValueError("visual narration visual id is invalid.")
        _validate_identifiers(self.metric_cohort, "visual narration metric cohort", required=True)
        if (
            not self.safe_scope_label.strip()
            or len(self.safe_scope_label) > MAX_SAFE_SCOPE_LABEL_LENGTH
        ):
            raise ValueError("visual narration safe scope label is invalid.")
        if type(self.resolved) is not TemporalResolved:
            raise TypeError("visual narration reference requires an exact TemporalResolved value.")
        if not isinstance(cast("object", self.basis), MeasurementBasis):
            raise TypeError("visual narration basis is invalid.")
        _validate_identifiers(self.qualification, "visual narration qualification", required=False)
        _validate_identifiers(
            self.allowed_action_ids, "visual narration action ids", required=False
        )


@dataclass(frozen=True, slots=True)
class VisualNarrationReferenceReadRequest:
    """Opaque visual-narration reference plus verified custody and observation instant."""

    reference: PageContextReference
    verified_owner_id: str
    conversation_session_id: str
    at: datetime

    def __post_init__(self) -> None:
        _validate_capability_read(
            self.reference,
            self.verified_owner_id,
            self.conversation_session_id,
            self.at,
            "visual narration",
        )


@dataclass(frozen=True, slots=True, init=False)
class ServerValidatedVisualNarrationReference:
    """Visual narration authority validated with its exact parent revision."""

    reference: PageContextReference
    parent_artifact_reference: PageContextReference
    parent_artifact_type: str
    parent_artifact_version: int
    expires_at: datetime
    visual_id: str
    metric_cohort: tuple[str, ...]
    safe_scope_label: str
    resolved: TemporalResolved
    basis: MeasurementBasis
    qualification: tuple[str, ...]
    allowed_action_ids: tuple[str, ...]

    def __init__(self) -> None:
        raise TypeError(
            "ServerValidatedVisualNarrationReference can only be created by validation."
        )


@dataclass(frozen=True, slots=True)
class VisualNarrationReferenceValidationResult:
    """Resolved narration authority or one nondisclosing invalid-context outcome."""

    outcome: ResolutionOutcome
    reference: ServerValidatedVisualNarrationReference | None = None

    def __post_init__(self) -> None:
        _validate_capability_result(self.outcome, self.reference, "visual narration")


def product_capability_parent_is_valid(
    capability: ProductSelectionCapabilityRecord,
    parent: PageArtifactRecord,
) -> bool:
    """Return whether a product capability names and cannot outlive its parent."""
    return _new_capability_parent_is_valid(capability, parent)


def media_scope_capability_parent_is_valid(
    capability: MediaScopeCapabilityRecord,
    parent: PageArtifactRecord,
) -> bool:
    """Return whether a media scope capability names and cannot outlive its parent."""
    return _new_capability_parent_is_valid(capability, parent)


def media_window_capability_parent_is_valid(
    capability: MediaWindowCapabilityRecord,
    parent: PageArtifactRecord,
) -> bool:
    """Return whether a media window capability names and cannot outlive its parent."""
    return _new_capability_parent_is_valid(capability, parent)


def visual_narration_reference_parent_is_valid(
    reference: VisualNarrationReferenceRecord,
    parent: PageArtifactRecord,
) -> bool:
    """Return whether a narration reference names and cannot outlive its parent."""
    return _new_capability_parent_is_valid(reference, parent)


def validate_product_selection_capability(
    record: ProductSelectionCapabilityRecord | None,
    request: ProductSelectionCapabilityReadRequest,
    parent: ServerValidatedPageArtifact | None,
) -> ProductSelectionCapabilityValidationResult:
    """Validate product custody without disclosing which exact check failed."""
    if not _new_capability_is_valid(record, request, parent):
        return ProductSelectionCapabilityValidationResult(ResolutionOutcome.INVALID_CONTEXT)
    capability = object.__new__(ServerValidatedProductSelectionCapability)
    _copy_validated(
        capability,
        record,
        (
            "reference",
            "parent_artifact_reference",
            "parent_artifact_type",
            "parent_artifact_version",
            "expires_at",
            "product",
            "safe_signature",
            "catalog_identity",
        ),
    )
    return ProductSelectionCapabilityValidationResult(ResolutionOutcome.RESOLVED, capability)


def validate_media_scope_capability(
    record: MediaScopeCapabilityRecord | None,
    request: MediaScopeCapabilityReadRequest,
    parent: ServerValidatedPageArtifact | None,
) -> MediaScopeCapabilityValidationResult:
    """Validate media scope custody without disclosing which exact check failed."""
    if not _new_capability_is_valid(record, request, parent):
        return MediaScopeCapabilityValidationResult(ResolutionOutcome.INVALID_CONTEXT)
    capability = object.__new__(ServerValidatedMediaScopeCapability)
    _copy_validated(
        capability,
        record,
        (
            "reference",
            "parent_artifact_reference",
            "parent_artifact_type",
            "parent_artifact_version",
            "expires_at",
            "scope",
            "safe_signature",
            "catalog_identity",
        ),
    )
    return MediaScopeCapabilityValidationResult(ResolutionOutcome.RESOLVED, capability)


def validate_media_window_capability(
    record: MediaWindowCapabilityRecord | None,
    request: MediaWindowCapabilityReadRequest,
    parent: ServerValidatedPageArtifact | None,
) -> MediaWindowCapabilityValidationResult:
    """Validate media window custody without disclosing which exact check failed."""
    if not _new_capability_is_valid(record, request, parent):
        return MediaWindowCapabilityValidationResult(ResolutionOutcome.INVALID_CONTEXT)
    capability = object.__new__(ServerValidatedMediaWindowCapability)
    _copy_validated(
        capability,
        record,
        (
            "reference",
            "parent_artifact_reference",
            "parent_artifact_type",
            "parent_artifact_version",
            "expires_at",
            "resolved",
            "compare_previous",
            "comparison",
        ),
    )
    return MediaWindowCapabilityValidationResult(ResolutionOutcome.RESOLVED, capability)


def validate_visual_narration_reference(
    record: VisualNarrationReferenceRecord | None,
    request: VisualNarrationReferenceReadRequest,
    parent: ServerValidatedPageArtifact | None,
) -> VisualNarrationReferenceValidationResult:
    """Validate narration custody without disclosing which exact check failed."""
    if not _new_capability_is_valid(record, request, parent):
        return VisualNarrationReferenceValidationResult(ResolutionOutcome.INVALID_CONTEXT)
    reference = object.__new__(ServerValidatedVisualNarrationReference)
    _copy_validated(
        reference,
        record,
        (
            "reference",
            "parent_artifact_reference",
            "parent_artifact_type",
            "parent_artifact_version",
            "expires_at",
            "visual_id",
            "metric_cohort",
            "safe_scope_label",
            "resolved",
            "basis",
            "qualification",
            "allowed_action_ids",
        ),
    )
    return VisualNarrationReferenceValidationResult(ResolutionOutcome.RESOLVED, reference)


def _new_capability_parent_is_valid(
    capability: ProductSelectionCapabilityRecord
    | MediaScopeCapabilityRecord
    | MediaWindowCapabilityRecord
    | VisualNarrationReferenceRecord,
    parent: PageArtifactRecord,
) -> bool:
    return (
        capability.parent_artifact_reference == parent.reference
        and capability.parent_artifact_type == parent.artifact.artifact_type
        and capability.parent_artifact_version == parent.artifact.artifact_version
        and capability.verified_owner_id == parent.verified_owner_id
        and capability.conversation_session_id == parent.conversation_session_id
        and capability.expires_at <= parent.expires_at
    )


def _new_capability_is_valid(
    record: ProductSelectionCapabilityRecord
    | MediaScopeCapabilityRecord
    | MediaWindowCapabilityRecord
    | VisualNarrationReferenceRecord
    | None,
    request: ProductSelectionCapabilityReadRequest
    | MediaScopeCapabilityReadRequest
    | MediaWindowCapabilityReadRequest
    | VisualNarrationReferenceReadRequest,
    parent: ServerValidatedPageArtifact | None,
) -> bool:
    return not (
        record is None
        or parent is None
        or record.reference != request.reference
        or record.verified_owner_id != request.verified_owner_id
        or record.conversation_session_id != request.conversation_session_id
        or record.expires_at <= request.at
        or record.parent_artifact_reference != parent.reference
        or record.parent_artifact_type != parent.artifact.artifact_type
        or record.parent_artifact_version != parent.artifact.artifact_version
        or record.expires_at > parent.expires_at
    )


def _copy_validated(target: object, source: object, names: tuple[str, ...]) -> None:
    for name in names:
        object.__setattr__(target, name, getattr(source, name))


def _validate_identifiers(values: tuple[str, ...], label: str, *, required: bool) -> None:
    if (
        type(values) is not tuple
        or (required and not values)
        or any(type(value) is not str or not value.strip() for value in values)
        or len(set(values)) != len(values)
    ):
        raise ValueError(f"{label} must be a distinct nonblank tuple.")


type ParentCapabilityRecord = (
    CampaignSelectionCapabilityRecord
    | ChannelRowCapabilityRecord
    | ProductSelectionCapabilityRecord
    | MediaScopeCapabilityRecord
    | MediaWindowCapabilityRecord
    | VisualNarrationReferenceRecord
)


@dataclass(frozen=True, slots=True)
class ParentCapabilityIssuanceRequest:
    """One complete immutable parent and capability set to commit all-or-nothing."""

    parent: PageArtifactRecord
    capabilities: tuple[ParentCapabilityRecord, ...]

    def __post_init__(self) -> None:
        if type(self.capabilities) is not tuple:
            raise TypeError("parent capability issuance records must be a tuple.")
        references = (self.parent.reference, *(record.reference for record in self.capabilities))
        if len(set(references)) != len(references):
            raise ValueError("parent capability issuance references must be distinct.")
        if any(
            not _issuance_capability_parent_is_valid(record, self.parent)
            for record in self.capabilities
        ):
            raise ValueError(
                "parent capability issuance records must share the exact parent custody."
            )


class ParentCapabilityIssuanceOutcome(StrEnum):
    """Closed result states for a bounded atomic parent/capability write."""

    ISSUED = "issued"
    CAPACITY_REFUSED = "capacity_refused"


@dataclass(frozen=True, slots=True)
class ParentCapabilityIssuanceResult:
    """Nondisclosing result of attempting one complete parent/capability issuance."""

    outcome: ParentCapabilityIssuanceOutcome

    @property
    def issued(self) -> bool:
        """Return whether the complete immutable set was committed."""
        return self.outcome is ParentCapabilityIssuanceOutcome.ISSUED


def _issuance_capability_parent_is_valid(
    capability: ParentCapabilityRecord, parent: PageArtifactRecord
) -> bool:
    return (
        capability.parent_artifact_reference == parent.reference
        and capability.parent_artifact_type == parent.artifact.artifact_type
        and capability.parent_artifact_version == parent.artifact.artifact_version
        and capability.verified_owner_id == parent.verified_owner_id
        and capability.conversation_session_id == parent.conversation_session_id
        and capability.expires_at <= parent.expires_at
    )
