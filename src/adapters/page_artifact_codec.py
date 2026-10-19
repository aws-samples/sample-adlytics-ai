# SPDX-License-Identifier: MIT-0
"""Strict canonical JSON for immutable artifact and channel-capability custody."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import Literal, cast

from domain.presentation.artifacts import (
    CampaignSelectionCapabilityRecord,
    ChannelRowCapabilityRecord,
    PageArtifact,
    PageArtifactRecord,
)
from domain.presentation.campaign_attribution import (
    safe_campaign_display_signature_from_payload,
    safe_campaign_display_signature_payload,
)
from domain.presentation.page_schemas import TemporalPresetId
from domain.presentation.temporal import (
    ComparisonUnavailableReason,
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
    TemporalUnavailableReason,
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

__all__ = [
    "campaign_selection_capability_record_from_json",
    "campaign_selection_capability_record_to_json",
    "channel_row_capability_record_from_json",
    "channel_row_capability_record_to_json",
    "page_artifact_record_from_json",
    "page_artifact_record_to_json",
]

_LEGACY_ARTIFACT_FIELDS = frozenset(
    {
        "artifact",
        "conversation_session_id",
        "expires_at",
        "reference",
        "verified_owner_id",
    }
)
_PARENT_ARTIFACT_FIELDS = _LEGACY_ARTIFACT_FIELDS | {"parent_reference"}
_TEMPORAL_ARTIFACT_FIELDS = _LEGACY_ARTIFACT_FIELDS | {"temporal_control"}
_PARENT_TEMPORAL_ARTIFACT_FIELDS = _PARENT_ARTIFACT_FIELDS | {"temporal_control"}
_ARTIFACT_RECORD_FIELD_SETS = frozenset(
    {
        _LEGACY_ARTIFACT_FIELDS,
        _PARENT_ARTIFACT_FIELDS,
        _TEMPORAL_ARTIFACT_FIELDS,
        _PARENT_TEMPORAL_ARTIFACT_FIELDS,
    }
)
_ARTIFACT_FIELDS = frozenset({"payload", "type", "version"})
_TEMPORAL_CONTROL_FIELDS = frozenset(
    {"comparison", "custom", "mode", "presets", "resolved", "version"}
)
_TEMPORAL_RESOLVED_FIELDS = frozenset(
    {"display_grain", "reporting_window_status", "selection", "window"}
)
_TEMPORAL_V2_RESOLVED_FIELDS = _TEMPORAL_RESOLVED_FIELDS | {"period_label"}
_TEMPORAL_SELECTION_FIELDS = frozenset({"kind", "preset_id"})
_TEMPORAL_WINDOW_FIELDS = frozenset({"end", "start"})
_TEMPORAL_COMPARISON_FIELDS = frozenset({"policy", "reason_code", "status"})
_TEMPORAL_CALENDAR_COMPARISON_FIELDS = frozenset({"policy", "prior_period_label", "status"})
_TEMPORAL_PRESET_FIELDS = frozenset({"availability", "id", "label", "reason_code"})
_TEMPORAL_V2_PRESET_FIELDS = _TEMPORAL_PRESET_FIELDS | {"reason_text", "window", "period_label"}
_TEMPORAL_CUSTOM_FIELDS = frozenset({"availability", "bounds", "reason_code"})
_TEMPORAL_BOUNDS_FIELDS = frozenset(
    {"earliest_start", "latest_end", "max_duration_days", "min_duration_days"}
)
_VINTAGE_CONTROL_FIELDS = frozenset({"mode", "resolved", "version"})
_VINTAGE_RESOLVED_FIELDS = frozenset({"as_of_month", "period_label", "window"})
_VINTAGE_CONTROL_VERSION = 2
_CHANNEL_CAPABILITY_FIELDS = frozenset(
    {
        "channel",
        "conversation_session_id",
        "current_period",
        "expires_at",
        "marketplace",
        "model_ids",
        "parent_artifact",
        "reference",
        "reporting_window_status",
        "verified_owner_id",
    }
)
_CAMPAIGN_CAPABILITY_FIELDS = frozenset(
    {
        "campaign",
        "channel",
        "conversation_session_id",
        "expires_at",
        "marketplace",
        "parent_artifact",
        "reference",
        "reporting_window_status",
        "safe_signature",
        "verified_owner_id",
    }
)
_CAMPAIGN_FIELDS = frozenset({"commerce_scope", "platform", "value"})
_PARENT_FIELDS = frozenset({"reference", "type", "version"})
_PERIOD_FIELDS = frozenset({"end", "start"})


def page_artifact_record_to_json(record: PageArtifactRecord) -> str:
    """Serialize one full custody record while retaining the exact legacy root shape."""
    payload: dict[str, object] = {
        "artifact": {
            "payload": record.artifact.payload,
            "type": record.artifact.artifact_type,
            "version": record.artifact.artifact_version,
        },
        "conversation_session_id": record.conversation_session_id,
        "expires_at": _moment(record.expires_at),
        "reference": record.reference.value,
        "verified_owner_id": record.verified_owner_id,
    }
    if record.parent_reference is not None:
        payload["parent_reference"] = record.parent_reference.value
    if record.temporal_control is not None:
        payload["temporal_control"] = _temporal_control_payload(record.temporal_control)
    return _canonical(payload)


def page_artifact_record_from_json(serialized: str) -> PageArtifactRecord:
    """Decode exact legacy or temporal-custody artifact records and re-attest bytes."""
    item = _load(serialized, "page artifact record")
    fields = frozenset(item)
    if fields not in _ARTIFACT_RECORD_FIELD_SETS:
        raise ValueError("page artifact record fields are not exact.")
    artifact = _object(item["artifact"], _ARTIFACT_FIELDS, "page artifact")
    payload = artifact["payload"]
    if not isinstance(payload, dict):
        raise ValueError("page artifact payload/version is invalid.")
    version = _integer(artifact["version"], "page artifact version")
    record = PageArtifactRecord(
        reference=PageContextReference(_text(item["reference"], "page artifact reference")),
        artifact=PageArtifact(
            artifact_type=_text(artifact["type"], "page artifact type"),
            artifact_version=version,
            payload_json=_canonical(cast("dict[str, object]", payload)),
        ),
        verified_owner_id=_text(item["verified_owner_id"], "page artifact owner"),
        conversation_session_id=_text(
            item["conversation_session_id"], "page artifact conversation session"
        ),
        expires_at=_datetime(item["expires_at"], "page artifact expiry"),
        parent_reference=(
            None
            if "parent_reference" not in fields
            else PageContextReference(
                _text(item["parent_reference"], "page artifact parent reference")
            )
        ),
        temporal_control=(
            None
            if "temporal_control" not in fields
            else _temporal_control_from_payload(item["temporal_control"])
        ),
    )
    if page_artifact_record_to_json(record) != serialized:
        raise ValueError("page artifact record is not canonical JSON.")
    return record


def _temporal_control_payload(
    control: TemporalControl | VintageControl,
) -> dict[str, object]:
    if isinstance(control, VintageControl):
        return _vintage_control_payload(control)
    body = _temporal_control_v1_payload(control)
    if control.version == 1:
        return body
    # Version 2 (ADR-100 point 8) keeps every version-1 field and adds the period label and
    # each preset's served window, period label and unavailable text.
    resolved = cast("dict[str, object]", body["resolved"])
    resolved["period_label"] = control.resolved.period_label
    for raw, preset in zip(
        cast("list[dict[str, object]]", body["presets"]), control.presets, strict=True
    ):
        raw["reason_text"] = preset.reason_text
        raw["window"] = (
            None
            if preset.window is None
            else {"start": preset.window.start.isoformat(), "end": preset.window.end.isoformat()}
        )
        raw["period_label"] = preset.period_label
    return body


def _vintage_control_payload(control: VintageControl) -> dict[str, object]:
    resolved = control.resolved
    return {
        "mode": control.mode,
        "resolved": (
            None
            if resolved is None
            else {
                "as_of_month": resolved.as_of_month,
                "period_label": resolved.period_label,
                "window": {
                    "start": resolved.window.start.isoformat(),
                    "end": resolved.window.end.isoformat(),
                },
            }
        ),
        "version": control.version,
    }


def _temporal_control_v1_payload(control: TemporalControl) -> dict[str, object]:
    bounds = control.custom.bounds
    body: dict[str, object] = {
        "version": control.version,
        "mode": control.mode.value,
        "resolved": {
            "window": {
                "start": control.resolved.window.start.isoformat(),
                "end": control.resolved.window.end.isoformat(),
            },
            "reporting_window_status": control.resolved.reporting_window_status.value,
            "selection": {
                "kind": control.resolved.selection.kind.value,
                "preset_id": (
                    None
                    if control.resolved.selection.preset_id is None
                    else control.resolved.selection.preset_id.value
                ),
            },
            "display_grain": control.resolved.display_grain.value,
        },
        "comparison": {
            "policy": control.comparison.policy.value,
            "status": control.comparison.status.value,
            "reason_code": (
                None
                if control.comparison.reason_code is None
                else control.comparison.reason_code.value
            ),
        },
        "presets": [
            {
                "id": preset.preset_id.value,
                "label": preset.label,
                "availability": preset.availability.value,
                "reason_code": (None if preset.reason_code is None else preset.reason_code.value),
            }
            for preset in control.presets
        ],
        "custom": {
            "availability": control.custom.availability.value,
            "bounds": (
                None
                if bounds is None
                else {
                    "earliest_start": bounds.earliest_start.isoformat(),
                    "latest_end": bounds.latest_end.isoformat(),
                    "min_duration_days": bounds.min_duration_days,
                    "max_duration_days": bounds.max_duration_days,
                }
            ),
            "reason_code": (
                None if control.custom.reason_code is None else control.custom.reason_code.value
            ),
        },
    }
    if control.comparison.prior_period_label is not None:
        body["comparison"] = {
            "policy": control.comparison.policy.value,
            "status": control.comparison.status.value,
            "prior_period_label": control.comparison.prior_period_label,
        }
    return body


def _temporal_control_from_payload(value: object) -> TemporalControl | VintageControl:
    if isinstance(value, dict):
        mapping = cast("dict[object, object]", value)
        if all(isinstance(key, str) for key in mapping):
            keys = {cast("str", key) for key in mapping}
            if keys == set(_VINTAGE_CONTROL_FIELDS):
                return _vintage_control_from_payload(cast("object", value))
    item = _object(cast("object", value), _TEMPORAL_CONTROL_FIELDS, "temporal control")
    version = _temporal_control_version(item["version"])
    resolved = _object(
        item["resolved"],
        _TEMPORAL_V2_RESOLVED_FIELDS if version == 2 else _TEMPORAL_RESOLVED_FIELDS,  # noqa: PLR2004
        "temporal resolved",
    )
    window = _object(resolved["window"], _TEMPORAL_WINDOW_FIELDS, "temporal window")
    selection = _object(
        resolved["selection"], _TEMPORAL_SELECTION_FIELDS, "temporal resolved selection"
    )
    comparison_value = item["comparison"]
    comparison_fields = (
        _TEMPORAL_CALENDAR_COMPARISON_FIELDS
        if isinstance(comparison_value, dict) and "prior_period_label" in comparison_value
        else _TEMPORAL_COMPARISON_FIELDS
    )
    comparison = _object(cast("object", comparison_value), comparison_fields, "temporal comparison")
    custom = _object(item["custom"], _TEMPORAL_CUSTOM_FIELDS, "temporal Custom control")
    preset_values = item["presets"]
    if not isinstance(preset_values, list):
        raise ValueError("temporal presets must be a list.")
    presets = tuple(
        _temporal_preset(preset, version) for preset in cast("list[object]", preset_values)
    )
    bounds_value = custom["bounds"]
    bounds = None
    if bounds_value is not None:
        raw_bounds = _object(bounds_value, _TEMPORAL_BOUNDS_FIELDS, "temporal Custom bounds")
        bounds = TemporalCustomBounds(
            earliest_start=_date(raw_bounds["earliest_start"], "temporal Custom earliest start"),
            latest_end=_date(raw_bounds["latest_end"], "temporal Custom latest end"),
            min_duration_days=_integer(
                raw_bounds["min_duration_days"], "temporal Custom minimum duration"
            ),
            max_duration_days=_integer(
                raw_bounds["max_duration_days"], "temporal Custom maximum duration"
            ),
        )
    try:
        return TemporalControl(
            version=version,
            mode=_enum(item["mode"], TemporalMode, "temporal mode"),
            resolved=TemporalResolved(
                window=DateRange(
                    _date(window["start"], "temporal window start"),
                    _date(window["end"], "temporal window end"),
                ),
                reporting_window_status=_enum(
                    resolved["reporting_window_status"],
                    TemporalReportingWindowStatus,
                    "temporal reporting-window status",
                ),
                selection=TemporalResolvedSelection(
                    kind=_enum(
                        selection["kind"],
                        TemporalResolvedSelectionKind,
                        "temporal resolved selection kind",
                    ),
                    preset_id=_optional_enum(
                        selection["preset_id"], TemporalPresetId, "temporal resolved preset"
                    ),
                ),
                display_grain=_enum(
                    resolved["display_grain"], TemporalDisplayGrain, "temporal display grain"
                ),
                period_label=_optional_text(resolved.get("period_label"), "period label"),
            ),
            comparison=TemporalComparison(
                policy=_enum(
                    comparison["policy"], TemporalComparisonPolicy, "temporal comparison policy"
                ),
                status=_enum(
                    comparison["status"], TemporalComparisonStatus, "temporal comparison status"
                ),
                reason_code=_optional_enum(
                    comparison.get("reason_code"),
                    ComparisonUnavailableReason,
                    "temporal comparison reason",
                ),
                prior_period_label=_optional_text(
                    comparison.get("prior_period_label"), "prior calendar period label"
                ),
            ),
            presets=presets,
            custom=TemporalCustomControl(
                availability=_enum(
                    custom["availability"], TemporalAvailability, "temporal Custom availability"
                ),
                bounds=bounds,
                reason_code=_optional_enum(
                    custom["reason_code"], TemporalUnavailableReason, "temporal Custom reason"
                ),
            ),
        )
    except TypeError, ValueError:
        raise ValueError("temporal control is invalid.") from None


def _vintage_control_from_payload(value: object) -> VintageControl:
    item = _object(value, _VINTAGE_CONTROL_FIELDS, "vintage control")
    if item["version"] != _VINTAGE_CONTROL_VERSION or item["mode"] != "fixed":
        raise ValueError("vintage control version or mode is invalid.")
    resolved_value = item["resolved"]
    resolved = None
    if resolved_value is not None:
        raw_resolved = _object(resolved_value, _VINTAGE_RESOLVED_FIELDS, "vintage resolved")
        raw_window = _object(raw_resolved["window"], _TEMPORAL_WINDOW_FIELDS, "vintage window")
        resolved = VintageResolved(
            as_of_month=_text(raw_resolved["as_of_month"], "vintage resolved month"),
            period_label=_text(raw_resolved["period_label"], "vintage resolved label"),
            window=DateRange(
                _date(raw_window["start"], "vintage window start"),
                _date(raw_window["end"], "vintage window end"),
            ),
        )
    try:
        return VintageControl(resolved=resolved)
    except TypeError, ValueError:
        raise ValueError("vintage control is invalid.") from None


def _temporal_preset(value: object, version: int) -> TemporalPreset:
    fields = _TEMPORAL_V2_PRESET_FIELDS if version == 2 else _TEMPORAL_PRESET_FIELDS  # noqa: PLR2004
    item = _object(value, fields, "temporal preset")
    window_value = item.get("window")
    window = None
    if window_value is not None:
        raw_window = _object(window_value, _TEMPORAL_WINDOW_FIELDS, "temporal preset window")
        window = DateRange(
            _date(raw_window["start"], "temporal preset window start"),
            _date(raw_window["end"], "temporal preset window end"),
        )
    try:
        return TemporalPreset(
            preset_id=_enum(item["id"], TemporalPresetId, "temporal preset id"),
            label=_text(item["label"], "temporal preset label"),
            availability=_enum(
                item["availability"], TemporalAvailability, "temporal preset availability"
            ),
            reason_code=_optional_enum(
                item["reason_code"], TemporalUnavailableReason, "temporal preset reason"
            ),
            window=window,
            period_label=_optional_text(item.get("period_label"), "temporal preset period"),
            reason_text=_optional_text(item.get("reason_text"), "temporal preset text"),
        )
    except TypeError, ValueError:
        raise ValueError("temporal preset is invalid.") from None


def _optional_text(value: object, label: str) -> str | None:
    return None if value is None else _text(value, label)


def _temporal_control_version(value: object) -> Literal[1, 2]:
    version = _integer(value, "temporal control version")
    if version == 1:
        return 1
    if version == 2:  # noqa: PLR2004 -- the closed transport versions
        return 2
    raise ValueError("temporal control version is invalid.")


def _enum(value: object, enum_type: type, label: str):
    if not isinstance(value, str):
        raise ValueError(f"{label} is invalid.")
    try:
        return enum_type(value)
    except ValueError:
        raise ValueError(f"{label} is invalid.") from None


def _optional_enum(value: object, enum_type: type, label: str):
    return None if value is None else _enum(value, enum_type, label)


def channel_row_capability_record_to_json(record: ChannelRowCapabilityRecord) -> str:
    """Serialize one exact parent-bound channel row capability."""
    return _canonical(
        {
            "channel": record.channel.value,
            "conversation_session_id": record.conversation_session_id,
            "current_period": {
                "end": record.current_period.end.isoformat(),
                "start": record.current_period.start.isoformat(),
            },
            "expires_at": _moment(record.expires_at),
            "marketplace": record.marketplace.value,
            "model_ids": list(record.model_ids),
            "parent_artifact": {
                "reference": record.parent_artifact_reference.value,
                "type": record.parent_artifact_type,
                "version": record.parent_artifact_version,
            },
            "reference": record.reference.value,
            "reporting_window_status": record.reporting_window_status.value,
            "verified_owner_id": record.verified_owner_id,
        }
    )


def channel_row_capability_record_from_json(serialized: str) -> ChannelRowCapabilityRecord:
    """Decode only the exact canonical channel row capability shape."""
    item = _object(
        _load(serialized, "channel capability"),
        _CHANNEL_CAPABILITY_FIELDS,
        "channel capability",
    )
    parent = _object(item["parent_artifact"], _PARENT_FIELDS, "channel capability parent")
    period = _object(item["current_period"], _PERIOD_FIELDS, "channel capability period")
    model_values = item["model_ids"]
    if not isinstance(model_values, list):
        raise ValueError("channel capability model ids must be a list.")
    models = cast("list[object]", model_values)
    version = _integer(parent["version"], "channel capability parent version")
    record = ChannelRowCapabilityRecord(
        reference=PageContextReference(_text(item["reference"], "channel capability reference")),
        parent_artifact_reference=PageContextReference(
            _text(parent["reference"], "channel capability parent reference")
        ),
        parent_artifact_type=_text(parent["type"], "channel capability parent type"),
        parent_artifact_version=version,
        verified_owner_id=_text(item["verified_owner_id"], "channel capability owner"),
        conversation_session_id=_text(
            item["conversation_session_id"], "channel capability conversation session"
        ),
        expires_at=_datetime(item["expires_at"], "channel capability expiry"),
        channel=ChannelKey(_text(item["channel"], "channel capability channel")),
        marketplace=MarketplaceKey(_text(item["marketplace"], "channel capability marketplace")),
        current_period=DateRange(
            _date(period["start"], "channel capability period start"),
            _date(period["end"], "channel capability period end"),
        ),
        model_ids=tuple(_text(value, "channel capability model id") for value in models),
        reporting_window_status=ReportingWindowStatus(
            _text(
                item["reporting_window_status"],
                "channel capability reporting-window status",
            )
        ),
    )
    if channel_row_capability_record_to_json(record) != serialized:
        raise ValueError("channel capability is not canonical JSON.")
    return record


def campaign_selection_capability_record_to_json(
    record: CampaignSelectionCapabilityRecord,
) -> str:
    """Serialize one exact parent-bound campaign selection capability."""
    return _canonical(
        {
            "campaign": {
                "commerce_scope": record.campaign.commerce_scope.value,
                "platform": record.campaign.platform.value,
                "value": record.campaign.value,
            },
            "channel": record.channel.value,
            "conversation_session_id": record.conversation_session_id,
            "expires_at": _moment(record.expires_at),
            "marketplace": record.marketplace.value,
            "parent_artifact": {
                "reference": record.parent_artifact_reference.value,
                "type": record.parent_artifact_type,
                "version": record.parent_artifact_version,
            },
            "reference": record.reference.value,
            "reporting_window_status": record.reporting_window_status.value,
            "safe_signature": safe_campaign_display_signature_payload(record.safe_signature),
            "verified_owner_id": record.verified_owner_id,
        }
    )


def campaign_selection_capability_record_from_json(
    serialized: str,
) -> CampaignSelectionCapabilityRecord:
    """Decode only the exact canonical campaign selection capability shape."""
    item = _object(
        _load(serialized, "campaign capability"),
        _CAMPAIGN_CAPABILITY_FIELDS,
        "campaign capability",
    )
    parent = _object(item["parent_artifact"], _PARENT_FIELDS, "campaign capability parent")
    campaign = _object(item["campaign"], _CAMPAIGN_FIELDS, "campaign capability campaign")
    record = CampaignSelectionCapabilityRecord(
        reference=PageContextReference(_text(item["reference"], "campaign capability reference")),
        parent_artifact_reference=PageContextReference(
            _text(parent["reference"], "campaign capability parent reference")
        ),
        parent_artifact_type=_text(parent["type"], "campaign capability parent type"),
        parent_artifact_version=_integer(parent["version"], "campaign capability parent version"),
        verified_owner_id=_text(item["verified_owner_id"], "campaign capability owner"),
        conversation_session_id=_text(
            item["conversation_session_id"], "campaign capability conversation session"
        ),
        expires_at=_datetime(item["expires_at"], "campaign capability expiry"),
        campaign=CampaignKey(
            _text(campaign["value"], "campaign capability campaign value"),
            CommerceScopeKey(
                _text(
                    campaign["commerce_scope"],
                    "campaign capability commerce scope",
                )
            ),
            PlatformKey(_text(campaign["platform"], "campaign capability platform")),
        ),
        channel=ChannelKey(_text(item["channel"], "campaign capability channel")),
        marketplace=MarketplaceKey(_text(item["marketplace"], "campaign capability marketplace")),
        safe_signature=safe_campaign_display_signature_from_payload(item["safe_signature"]),
        reporting_window_status=ReportingWindowStatus(
            _text(
                item["reporting_window_status"],
                "campaign capability reporting-window status",
            )
        ),
    )
    if campaign_selection_capability_record_to_json(record) != serialized:
        raise ValueError("campaign capability is not canonical JSON.")
    return record


def _canonical(value: dict[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _pairs(values: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in values:
        if key in result:
            raise ValueError(f"artifact custody JSON repeats field {key!r}.")
        result[key] = value
    return result


def _load(serialized: str, label: str) -> dict[str, object]:
    try:
        raw: object = json.loads(serialized, object_pairs_hook=_pairs)
    except TypeError, ValueError:
        raise ValueError(f"{label} is not valid JSON.") from None
    if not isinstance(raw, dict):
        raise ValueError(f"{label} must be an object.")
    return cast("dict[str, object]", raw)


def _object(value: object, fields: frozenset[str], label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} fields are not exact.")
    item = cast("dict[str, object]", value)
    if frozenset(item) != fields:
        raise ValueError(f"{label} fields are not exact.")
    return item


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be nonblank text.")
    return value


def _date(value: object, label: str) -> date:
    text = _text(value, label)
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        raise ValueError(f"{label} is invalid.") from None
    if parsed.isoformat() != text:
        raise ValueError(f"{label} must use canonical ISO date form.")
    return parsed


def _datetime(value: object, label: str) -> datetime:
    text = _text(value, label)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"{label} is invalid.") from None
    if parsed.tzinfo is None or parsed.astimezone(UTC).isoformat() != text:
        raise ValueError(f"{label} must use canonical UTC form.")
    return parsed


def _moment(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _integer(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{label} must be an integer.")
    return value
