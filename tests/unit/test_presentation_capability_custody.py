# SPDX-License-Identifier: MIT-0
"""Parent-bound Product, Media, and visual-narration capability custody."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import cast

import pytest

from adapters.presentation_capability_codec import (
    media_scope_capability_record_from_json,
    media_scope_capability_record_to_json,
    media_window_capability_record_from_json,
    media_window_capability_record_to_json,
    product_selection_capability_record_from_json,
    product_selection_capability_record_to_json,
    visual_narration_reference_record_from_json,
    visual_narration_reference_record_to_json,
)
from domain.presentation.artifacts import (
    MediaScope,
    MediaScopeCapabilityReadRequest,
    MediaScopeCapabilityRecord,
    MediaWindowCapabilityReadRequest,
    MediaWindowCapabilityRecord,
    PageArtifact,
    PageArtifactReadRequest,
    PageArtifactRecord,
    ParentCapabilityIssuanceRequest,
    ParentCapabilityRecord,
    ProductSelectionCapabilityReadRequest,
    ProductSelectionCapabilityRecord,
    SafeMediaScopeDisplaySignature,
    VisualNarrationReferenceReadRequest,
    VisualNarrationReferenceRecord,
    canonical_artifact_json,
    media_scope_capability_parent_is_valid,
    media_window_capability_parent_is_valid,
    product_capability_parent_is_valid,
    validate_media_scope_capability,
    validate_media_window_capability,
    validate_page_artifact,
    validate_product_selection_capability,
    validate_visual_narration_reference,
    visual_narration_reference_parent_is_valid,
)
from domain.presentation.campaigns import EffectiveNameStatus
from domain.presentation.page_schemas import TemporalPresetId
from domain.presentation.retail_product_detail import SafeProductDisplaySignature
from domain.presentation.temporal import (
    TemporalComparison,
    TemporalComparisonPolicy,
    TemporalComparisonStatus,
    TemporalDisplayGrain,
    TemporalReportingWindowStatus,
    TemporalResolved,
    TemporalResolvedSelection,
    TemporalResolvedSelectionKind,
)
from domain.semantics.analytical_resolution import ResolutionOutcome
from domain.semantics.identity import (
    ChannelKey,
    CommerceScopeKey,
    MarketplaceKey,
    PlatformKey,
    ProductKey,
)
from domain.semantics.page_context import PageContextReference
from domain.shared_kernel.provenance import MeasurementBasis
from domain.shared_kernel.values import DateRange

pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 7, 12, tzinfo=UTC)
EXPIRY = NOW + timedelta(hours=1)
PARENT_REFERENCE = PageContextReference("P" * 43)
PRODUCT_REFERENCE = PageContextReference("R" * 43)
MEDIA_SCOPE_REFERENCE = PageContextReference("S" * 43)
MEDIA_WINDOW_REFERENCE = PageContextReference("W" * 43)
NARRATION_REFERENCE = PageContextReference("N" * 43)
PARENT = PageArtifactRecord(
    reference=PARENT_REFERENCE,
    artifact=PageArtifact(
        "MediaEfficiencyScopeList", 1, canonical_artifact_json({"outcome": "choices"})
    ),
    verified_owner_id="owner",
    conversation_session_id="session",
    expires_at=EXPIRY,
)
RESOLVED = TemporalResolved(
    window=DateRange(date(2026, 9, 1), date(2026, 9, 7)),
    reporting_window_status=TemporalReportingWindowStatus.USER_SELECTED,
    selection=TemporalResolvedSelection(
        kind=TemporalResolvedSelectionKind.PRESET,
        preset_id=TemporalPresetId.SEVEN_DAYS,
    ),
    display_grain=TemporalDisplayGrain.DAY,
)
COMPARISON = TemporalComparison(
    policy=TemporalComparisonPolicy.OPTIONAL_EQUAL_PRIOR,
    status=TemporalComparisonStatus.AVAILABLE,
)


def _product_record() -> ProductSelectionCapabilityRecord:
    return ProductSelectionCapabilityRecord(
        reference=PRODUCT_REFERENCE,
        parent_artifact_reference=PARENT.reference,
        parent_artifact_type=PARENT.artifact.artifact_type,
        parent_artifact_version=PARENT.artifact.artifact_version,
        verified_owner_id=PARENT.verified_owner_id,
        conversation_session_id=PARENT.conversation_session_id,
        expires_at=EXPIRY,
        product=ProductKey(
            "fictional-product",
            CommerceScopeKey("fictional-scope"),
            MarketplaceKey("fictional-marketplace"),
        ),
        safe_signature=SafeProductDisplaySignature(
            effective_name="Fictional product",
            effective_name_status=EffectiveNameStatus.AVAILABLE,
            marketplace="Fictional marketplace",
        ),
        catalog_identity="catalog-v1",
    )


def _media_scope_record() -> MediaScopeCapabilityRecord:
    return MediaScopeCapabilityRecord(
        reference=MEDIA_SCOPE_REFERENCE,
        parent_artifact_reference=PARENT.reference,
        parent_artifact_type=PARENT.artifact.artifact_type,
        parent_artifact_version=PARENT.artifact.artifact_version,
        verified_owner_id=PARENT.verified_owner_id,
        conversation_session_id=PARENT.conversation_session_id,
        expires_at=EXPIRY,
        scope=MediaScope(
            commerce_scope=CommerceScopeKey("fictional-scope"),
            platform=PlatformKey("fictional-platform"),
            channel=ChannelKey("fictional-channel"),
            marketplace=MarketplaceKey("fictional-marketplace"),
        ),
        safe_signature=SafeMediaScopeDisplaySignature("Fictional media scope"),
        catalog_identity="catalog-v1",
    )


def _media_window_record() -> MediaWindowCapabilityRecord:
    return MediaWindowCapabilityRecord(
        reference=MEDIA_WINDOW_REFERENCE,
        parent_artifact_reference=PARENT.reference,
        parent_artifact_type=PARENT.artifact.artifact_type,
        parent_artifact_version=PARENT.artifact.artifact_version,
        verified_owner_id=PARENT.verified_owner_id,
        conversation_session_id=PARENT.conversation_session_id,
        expires_at=EXPIRY,
        resolved=RESOLVED,
        compare_previous=True,
        comparison=COMPARISON,
    )


def _narration_reference() -> VisualNarrationReferenceRecord:
    return VisualNarrationReferenceRecord(
        reference=NARRATION_REFERENCE,
        parent_artifact_reference=PARENT.reference,
        parent_artifact_type=PARENT.artifact.artifact_type,
        parent_artifact_version=PARENT.artifact.artifact_version,
        verified_owner_id=PARENT.verified_owner_id,
        conversation_session_id=PARENT.conversation_session_id,
        expires_at=EXPIRY,
        visual_id="delivery-trend",
        metric_cohort=("spend", "impressions"),
        safe_scope_label="Fictional media scope",
        resolved=RESOLVED,
        basis=MeasurementBasis.AD_ATTRIBUTED,
        qualification=("qualified",),
        allowed_action_ids=("open-exact-data",),
    )


def test_product_and_media_capabilities_resolve_only_under_exact_parent_custody() -> None:
    parent = validate_page_artifact(
        PARENT,
        PageArtifactReadRequest(PARENT.reference, "owner", "session", NOW),
    ).artifact
    assert parent is not None

    product = validate_product_selection_capability(
        _product_record(),
        ProductSelectionCapabilityReadRequest(PRODUCT_REFERENCE, "owner", "session", NOW),
        parent,
    )
    scope = validate_media_scope_capability(
        _media_scope_record(),
        MediaScopeCapabilityReadRequest(MEDIA_SCOPE_REFERENCE, "owner", "session", NOW),
        parent,
    )

    assert product.outcome is ResolutionOutcome.RESOLVED
    assert product.capability is not None
    assert product.capability.product.value == "fictional-product"
    assert scope.outcome is ResolutionOutcome.RESOLVED
    assert scope.capability is not None
    assert scope.capability.safe_signature.label == "Fictional media scope"
    assert (
        validate_product_selection_capability(
            _product_record(),
            ProductSelectionCapabilityReadRequest(PRODUCT_REFERENCE, "owner", "other-session", NOW),
            parent,
        ).outcome
        is ResolutionOutcome.INVALID_CONTEXT
    )


def test_media_window_requires_matching_optional_comparison_intent_and_status() -> None:
    record = _media_window_record()

    assert record.comparison.status is TemporalComparisonStatus.AVAILABLE
    with pytest.raises(ValueError, match="intent and status disagree"):
        replace(record, compare_previous=False)


def test_narration_reference_cannot_outlive_its_parent() -> None:
    reference = _narration_reference()

    assert visual_narration_reference_parent_is_valid(reference, PARENT) is True
    assert (
        visual_narration_reference_parent_is_valid(
            replace(reference, expires_at=EXPIRY + timedelta(seconds=1)), PARENT
        )
        is False
    )


@pytest.mark.parametrize(
    ("record", "to_json", "from_json"),
    [
        (
            _product_record(),
            product_selection_capability_record_to_json,
            product_selection_capability_record_from_json,
        ),
        (
            _media_scope_record(),
            media_scope_capability_record_to_json,
            media_scope_capability_record_from_json,
        ),
        (
            _media_window_record(),
            media_window_capability_record_to_json,
            media_window_capability_record_from_json,
        ),
        (
            _narration_reference(),
            visual_narration_reference_record_to_json,
            visual_narration_reference_record_from_json,
        ),
    ],
)
def test_capability_codecs_round_trip_only_canonical_exact_fields(
    record: object,
    to_json: object,
    from_json: object,
) -> None:
    serialized = cast("str", to_json(record))  # type: ignore[operator]

    assert from_json(serialized) == record  # type: ignore[operator]
    payload = cast("dict[str, object]", json.loads(serialized))
    assert isinstance(payload, dict)
    assert serialized == json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    reordered = json.dumps(
        dict(reversed(tuple(payload.items()))),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    with pytest.raises(ValueError, match="not canonical JSON"):
        from_json(reordered)  # type: ignore[operator]
    malformed = json.dumps({**payload, "unknown": "value"}, sort_keys=True, separators=(",", ":"))
    with pytest.raises(ValueError, match="fields are not exact"):
        from_json(malformed)  # type: ignore[operator]


@pytest.mark.parametrize(
    "capability",
    (
        _product_record(),
        _media_scope_record(),
        _media_window_record(),
        _narration_reference(),
    ),
)
def test_parent_capability_issuance_accepts_every_parent_bound_capability_kind(
    capability: ParentCapabilityRecord,
) -> None:
    request = ParentCapabilityIssuanceRequest(PARENT, (capability,))

    assert request.parent is PARENT
    assert request.capabilities == (capability,)


def test_parent_capability_issuance_rejects_parent_mismatch_and_duplicate_references() -> None:
    with pytest.raises(ValueError, match="exact parent custody"):
        ParentCapabilityIssuanceRequest(
            PARENT,
            (replace(_product_record(), parent_artifact_reference=PageContextReference("Q" * 43)),),
        )
    with pytest.raises(ValueError, match="references must be distinct"):
        ParentCapabilityIssuanceRequest(
            PARENT,
            (_product_record(), replace(_media_scope_record(), reference=PRODUCT_REFERENCE)),
        )


def _validated_parent(record: PageArtifactRecord = PARENT):
    parent = validate_page_artifact(
        record,
        PageArtifactReadRequest(record.reference, "owner", "session", NOW),
    ).artifact
    assert parent is not None
    return parent


def test_each_new_capability_kind_nondisclosingly_rejects_wrong_custody_and_expiry() -> None:
    parent = _validated_parent()
    invalid_outcomes = (
        validate_product_selection_capability(
            _product_record(),
            ProductSelectionCapabilityReadRequest(PRODUCT_REFERENCE, "other-owner", "session", NOW),
            parent,
        ).outcome,
        validate_product_selection_capability(
            _product_record(),
            ProductSelectionCapabilityReadRequest(PRODUCT_REFERENCE, "owner", "other-session", NOW),
            parent,
        ).outcome,
        validate_product_selection_capability(
            _product_record(),
            ProductSelectionCapabilityReadRequest(PRODUCT_REFERENCE, "owner", "session", EXPIRY),
            parent,
        ).outcome,
        validate_media_scope_capability(
            _media_scope_record(),
            MediaScopeCapabilityReadRequest(MEDIA_SCOPE_REFERENCE, "other-owner", "session", NOW),
            parent,
        ).outcome,
        validate_media_scope_capability(
            _media_scope_record(),
            MediaScopeCapabilityReadRequest(MEDIA_SCOPE_REFERENCE, "owner", "other-session", NOW),
            parent,
        ).outcome,
        validate_media_scope_capability(
            _media_scope_record(),
            MediaScopeCapabilityReadRequest(MEDIA_SCOPE_REFERENCE, "owner", "session", EXPIRY),
            parent,
        ).outcome,
        validate_media_window_capability(
            _media_window_record(),
            MediaWindowCapabilityReadRequest(MEDIA_WINDOW_REFERENCE, "other-owner", "session", NOW),
            parent,
        ).outcome,
        validate_media_window_capability(
            _media_window_record(),
            MediaWindowCapabilityReadRequest(MEDIA_WINDOW_REFERENCE, "owner", "other-session", NOW),
            parent,
        ).outcome,
        validate_media_window_capability(
            _media_window_record(),
            MediaWindowCapabilityReadRequest(MEDIA_WINDOW_REFERENCE, "owner", "session", EXPIRY),
            parent,
        ).outcome,
        validate_visual_narration_reference(
            _narration_reference(),
            VisualNarrationReferenceReadRequest(NARRATION_REFERENCE, "other-owner", "session", NOW),
            parent,
        ).outcome,
        validate_visual_narration_reference(
            _narration_reference(),
            VisualNarrationReferenceReadRequest(NARRATION_REFERENCE, "owner", "other-session", NOW),
            parent,
        ).outcome,
        validate_visual_narration_reference(
            _narration_reference(),
            VisualNarrationReferenceReadRequest(NARRATION_REFERENCE, "owner", "session", EXPIRY),
            parent,
        ).outcome,
    )

    assert invalid_outcomes == (ResolutionOutcome.INVALID_CONTEXT,) * 12


def test_new_capabilities_require_matching_parent_identity_and_lifetime() -> None:
    mismatched_parent = PageArtifactRecord(
        reference=PARENT.reference,
        artifact=PageArtifact(
            "DifferentArtifact",
            1,
            canonical_artifact_json({"outcome": "choices"}),
        ),
        verified_owner_id=PARENT.verified_owner_id,
        conversation_session_id=PARENT.conversation_session_id,
        expires_at=PARENT.expires_at,
    )
    parent = _validated_parent(mismatched_parent)
    invalid_outcomes = (
        validate_product_selection_capability(
            _product_record(),
            ProductSelectionCapabilityReadRequest(PRODUCT_REFERENCE, "owner", "session", NOW),
            parent,
        ).outcome,
        validate_media_scope_capability(
            _media_scope_record(),
            MediaScopeCapabilityReadRequest(MEDIA_SCOPE_REFERENCE, "owner", "session", NOW),
            parent,
        ).outcome,
        validate_media_window_capability(
            _media_window_record(),
            MediaWindowCapabilityReadRequest(MEDIA_WINDOW_REFERENCE, "owner", "session", NOW),
            parent,
        ).outcome,
        validate_visual_narration_reference(
            _narration_reference(),
            VisualNarrationReferenceReadRequest(NARRATION_REFERENCE, "owner", "session", NOW),
            parent,
        ).outcome,
    )

    assert invalid_outcomes == (ResolutionOutcome.INVALID_CONTEXT,) * 4
    assert product_capability_parent_is_valid(_product_record(), PARENT) is True
    assert media_scope_capability_parent_is_valid(_media_scope_record(), PARENT) is True
    assert media_window_capability_parent_is_valid(_media_window_record(), PARENT) is True
    assert visual_narration_reference_parent_is_valid(_narration_reference(), PARENT) is True
    assert (
        product_capability_parent_is_valid(
            replace(_product_record(), expires_at=EXPIRY + timedelta(seconds=1)), PARENT
        )
        is False
    )
    assert (
        media_scope_capability_parent_is_valid(
            replace(_media_scope_record(), expires_at=EXPIRY + timedelta(seconds=1)), PARENT
        )
        is False
    )
    assert (
        media_window_capability_parent_is_valid(
            replace(_media_window_record(), expires_at=EXPIRY + timedelta(seconds=1)), PARENT
        )
        is False
    )
    assert (
        visual_narration_reference_parent_is_valid(
            replace(_narration_reference(), expires_at=EXPIRY + timedelta(seconds=1)), PARENT
        )
        is False
    )
