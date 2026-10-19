# SPDX-License-Identifier: MIT-0
"""Canonical legacy-compatible artifact and channel-capability storage codecs."""

import json
from dataclasses import replace
from datetime import UTC, date, datetime
from typing import cast

import pytest

from adapters.page_artifact_codec import (
    campaign_selection_capability_record_from_json,
    campaign_selection_capability_record_to_json,
    channel_row_capability_record_from_json,
    channel_row_capability_record_to_json,
    page_artifact_record_from_json,
    page_artifact_record_to_json,
)
from domain.presentation.artifacts import (
    CampaignSelectionCapabilityRecord,
    ChannelRowCapabilityRecord,
    PageArtifact,
    PageArtifactReadRequest,
    PageArtifactRecord,
    canonical_artifact_json,
    validate_page_artifact,
)
from domain.presentation.campaign_attribution import SafeCampaignDisplaySignature
from domain.presentation.campaigns import EffectiveNameStatus
from domain.presentation.page_schemas import ROLLING_PRESET_IDS, TemporalPresetId
from domain.presentation.temporal import (
    TemporalAvailability,
    TemporalComparison,
    TemporalComparisonPolicy,
    TemporalComparisonStatus,
    TemporalControl,
    TemporalCustomBounds,
    TemporalCustomControl,
    TemporalDisplayGrain,
    TemporalMode,
    TemporalPreset,
    TemporalReportingWindowStatus,
    TemporalResolved,
    TemporalResolvedSelection,
    TemporalResolvedSelectionKind,
    VintageControl,
    VintageResolved,
)
from domain.semantics.identity import (
    CampaignKey,
    ChannelKey,
    CommerceScopeKey,
    MarketplaceKey,
    PlatformKey,
)
from domain.semantics.page_context import PageContextReference, ReportingWindowStatus
from domain.shared_kernel.values import DateRange

pytestmark = pytest.mark.unit

REFERENCE = PageContextReference("A" * 43)
PARENT = PageContextReference("P" * 43)
EXPIRY = datetime(2026, 8, 19, 13, 30, tzinfo=UTC)
ARTIFACT = PageArtifact("FictionalArtifact", 1, canonical_artifact_json({"outcome": "ready"}))
ROOT = PageArtifactRecord(REFERENCE, ARTIFACT, "owner", "session", EXPIRY)
TEMPORAL_CONTROL = TemporalControl(
    version=1,
    mode=TemporalMode.SELECTABLE,
    resolved=TemporalResolved(
        window=DateRange(date(2026, 8, 13), date(2026, 8, 19)),
        reporting_window_status=TemporalReportingWindowStatus.USER_SELECTED,
        selection=TemporalResolvedSelection(
            kind=TemporalResolvedSelectionKind.PRESET,
            preset_id=TemporalPresetId.SEVEN_DAYS,
        ),
        display_grain=TemporalDisplayGrain.DAY,
    ),
    comparison=TemporalComparison(
        policy=TemporalComparisonPolicy.OPTIONAL_EQUAL_PRIOR,
        status=TemporalComparisonStatus.AVAILABLE,
    ),
    presets=tuple(
        TemporalPreset(
            preset_id=preset_id,
            label={
                TemporalPresetId.ONE_DAY: "1D",
                TemporalPresetId.SEVEN_DAYS: "7D",
                TemporalPresetId.FOURTEEN_DAYS: "14D",
                TemporalPresetId.THIRTY_DAYS: "30D",
            }[preset_id],
            availability=TemporalAvailability.AVAILABLE,
        )
        for preset_id in ROLLING_PRESET_IDS
    ),
    custom=TemporalCustomControl(
        availability=TemporalAvailability.AVAILABLE,
        bounds=TemporalCustomBounds(
            earliest_start=date(2026, 5, 22),
            latest_end=date(2026, 8, 19),
            min_duration_days=1,
            max_duration_days=90,
        ),
    ),
)
VINTAGE_CONTROL = VintageControl(
    resolved=VintageResolved(
        as_of_month="2026-08",
        period_label="August 2026",
        window=DateRange(date(2025, 8, 1), date(2026, 8, 31)),
    ),
)


def test_root_artifact_preserves_the_exact_legacy_shape_and_round_trip() -> None:
    serialized = page_artifact_record_to_json(ROOT)

    assert "parent_reference" not in serialized
    assert page_artifact_record_from_json(serialized) == ROOT
    assert serialized == page_artifact_record_to_json(page_artifact_record_from_json(serialized))


def test_parent_aware_artifact_round_trips_without_weakening_legacy_decode() -> None:
    child = replace(ROOT, parent_reference=PARENT)

    serialized = page_artifact_record_to_json(child)

    assert f'"parent_reference":"{PARENT.value}"' in serialized
    assert page_artifact_record_from_json(serialized) == child
    assert page_artifact_record_from_json(page_artifact_record_to_json(ROOT)) == ROOT


def test_temporal_custody_artifact_round_trips_only_in_the_canonical_shape() -> None:
    record = replace(ROOT, temporal_control=TEMPORAL_CONTROL)

    serialized = page_artifact_record_to_json(record)

    assert '"temporal_control":' in serialized
    assert page_artifact_record_from_json(serialized) == record
    validated = validate_page_artifact(
        record,
        PageArtifactReadRequest(
            record.reference,
            record.verified_owner_id,
            record.conversation_session_id,
            EXPIRY.replace(hour=12),
        ),
    )
    assert validated.artifact is not None
    assert validated.artifact.temporal_control == TEMPORAL_CONTROL
    raw = cast("dict[str, object]", json.loads(serialized))
    control = cast("dict[str, object]", raw["temporal_control"])
    malformed = json.dumps(
        {**raw, "temporal_control": {**control, "unknown": "value"}},
        sort_keys=True,
        separators=(",", ":"),
    )
    with pytest.raises(ValueError, match="fields are not exact"):
        page_artifact_record_from_json(malformed)

    record = ChannelRowCapabilityRecord(
        reference=REFERENCE,
        parent_artifact_reference=PARENT,
        parent_artifact_type=ARTIFACT.artifact_type,
        parent_artifact_version=ARTIFACT.artifact_version,
        verified_owner_id="owner",
        conversation_session_id="session",
        expires_at=EXPIRY,
        channel=ChannelKey("fictional_channel"),
        marketplace=MarketplaceKey("fictional_marketplace"),
        current_period=DateRange(date(2026, 8, 10), date(2026, 8, 16)),
        model_ids=("first_touch_v1", "last_touch_v1", "linear_v1"),
        reporting_window_status=ReportingWindowStatus.PARTIAL_DEFAULT,
    )

    serialized = channel_row_capability_record_to_json(record)

    assert channel_row_capability_record_from_json(serialized) == record
    assert '"parent_artifact":{"reference":' in serialized
    assert '"model_ids":["first_touch_v1","last_touch_v1","linear_v1"]' in serialized
    assert '"reporting_window_status":"partial_default"' in serialized


def test_vintage_custody_artifact_round_trips_in_its_closed_shape() -> None:
    record = replace(ROOT, temporal_control=VINTAGE_CONTROL)

    serialized = page_artifact_record_to_json(record)

    assert page_artifact_record_from_json(serialized) == record
    assert '"as_of_month":"2026-08"' in serialized
    assert '"mode":"fixed"' in serialized
    # Latest only: no roster and no selection are stored.
    assert '"vintages"' not in serialized
    assert '"selection"' not in serialized.split('"temporal_control"')[1]


def test_campaign_capability_round_trips_exact_parent_canonical_and_safe_signature_fields() -> None:
    signature = SafeCampaignDisplaySignature(
        effective_name="Fictional campaign",
        channel="Fictional channel",
        marketplace="Fictional marketplace",
        platform="Fictional platform",
        effective_name_status=EffectiveNameStatus.AVAILABLE,
        effective_name_valid_from=date(2026, 1, 1),
    )
    record = CampaignSelectionCapabilityRecord(
        reference=REFERENCE,
        parent_artifact_reference=PARENT,
        parent_artifact_type="CampaignAttribution",
        parent_artifact_version=1,
        verified_owner_id="owner",
        conversation_session_id="session",
        expires_at=EXPIRY,
        campaign=CampaignKey(
            "fictional-campaign",
            CommerceScopeKey("fictional-scope"),
            PlatformKey("fictional-platform"),
        ),
        channel=ChannelKey("fictional-channel"),
        marketplace=MarketplaceKey("fictional-marketplace"),
        safe_signature=signature,
        reporting_window_status=ReportingWindowStatus.PARTIAL_DEFAULT,
    )

    serialized = campaign_selection_capability_record_to_json(record)

    assert campaign_selection_capability_record_from_json(serialized) == record
    assert '"parent_artifact":{"reference":' in serialized
    assert '"safe_signature":{"channel":"Fictional channel"' in serialized
    assert '"reporting_window_status":"partial_default"' in serialized
    raw = cast("dict[str, object]", json.loads(serialized))
    malformed = json.dumps(
        {**raw, "unknown": "value"},
        sort_keys=True,
        separators=(",", ":"),
    )
    with pytest.raises(ValueError, match="fields are not exact"):
        campaign_selection_capability_record_from_json(malformed)


def test_artifact_codec_rejects_unknown_duplicate_and_noncanonical_bytes() -> None:
    serialized = page_artifact_record_to_json(ROOT)
    raw: object = json.loads(serialized)
    assert isinstance(raw, dict)
    decoded = cast("dict[str, object]", raw)
    unknown = json.dumps({**decoded, "unknown": "value"}, sort_keys=True, separators=(",", ":"))
    duplicate = serialized.replace(
        f'"reference":"{REFERENCE.value}"',
        f'"reference":"{REFERENCE.value}","reference":"{REFERENCE.value}"',
    )
    noncanonical = serialized.replace(",", ", ", 1)

    for malformed in (unknown, duplicate, noncanonical):
        with pytest.raises(ValueError):
            page_artifact_record_from_json(malformed)
