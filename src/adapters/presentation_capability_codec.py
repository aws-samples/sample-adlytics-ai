# SPDX-License-Identifier: MIT-0
"""Strict canonical JSON codecs for parent-bound presentation capabilities."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import TypedDict, cast

from domain.presentation.artifacts import (
    MediaScope,
    MediaScopeCapabilityRecord,
    MediaWindowCapabilityRecord,
    ProductSelectionCapabilityRecord,
    SafeMediaScopeDisplaySignature,
    VisualNarrationReferenceRecord,
)
from domain.presentation.campaigns import EffectiveNameStatus
from domain.presentation.retail_product_detail import SafeProductDisplaySignature
from domain.presentation.temporal import (
    ComparisonUnavailableReason,
    TemporalComparison,
    TemporalComparisonPolicy,
    TemporalComparisonStatus,
    TemporalDisplayGrain,
    TemporalPresetId,
    TemporalReportingWindowStatus,
    TemporalResolved,
    TemporalResolvedSelection,
    TemporalResolvedSelectionKind,
)
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

__all__ = [
    "media_scope_capability_record_from_json",
    "media_scope_capability_record_to_json",
    "media_window_capability_record_from_json",
    "media_window_capability_record_to_json",
    "product_selection_capability_record_from_json",
    "product_selection_capability_record_to_json",
    "visual_narration_reference_record_from_json",
    "visual_narration_reference_record_to_json",
]


class _CustodyKwargs(TypedDict):
    reference: PageContextReference
    parent_artifact_reference: PageContextReference
    parent_artifact_type: str
    parent_artifact_version: int
    verified_owner_id: str
    conversation_session_id: str
    expires_at: datetime


_PARENT_FIELDS = frozenset({"reference", "type", "version"})
_PRODUCT_FIELDS = frozenset({"commerce_scope", "marketplace", "value"})
_SCOPE_FIELDS = frozenset({"channel", "commerce_scope", "marketplace", "platform"})
_SAFE_PRODUCT_FIELDS = frozenset({"effective_name", "effective_name_status", "marketplace"})
_SAFE_MEDIA_SCOPE_FIELDS = frozenset({"label"})
_PERIOD_FIELDS = frozenset({"end", "start"})
_RESOLVED_SELECTION_FIELDS = frozenset({"kind", "preset_id"})
_RESOLVED_FIELDS = frozenset({"display_grain", "reporting_window_status", "selection", "window"})
_COMPARISON_FIELDS = frozenset({"policy", "reason_code", "status"})
_PRODUCT_CAPABILITY_FIELDS = frozenset(
    {
        "catalog_identity",
        "conversation_session_id",
        "expires_at",
        "parent_artifact",
        "product",
        "reference",
        "safe_signature",
        "verified_owner_id",
    }
)
_MEDIA_SCOPE_CAPABILITY_FIELDS = frozenset(
    {
        "catalog_identity",
        "conversation_session_id",
        "expires_at",
        "parent_artifact",
        "reference",
        "safe_signature",
        "scope",
        "verified_owner_id",
    }
)
_MEDIA_WINDOW_CAPABILITY_FIELDS = frozenset(
    {
        "compare_previous",
        "comparison",
        "conversation_session_id",
        "expires_at",
        "parent_artifact",
        "reference",
        "resolved",
        "verified_owner_id",
    }
)
_VISUAL_NARRATION_REFERENCE_FIELDS = frozenset(
    {
        "allowed_action_ids",
        "basis",
        "conversation_session_id",
        "expires_at",
        "metric_cohort",
        "parent_artifact",
        "qualification",
        "reference",
        "resolved",
        "safe_scope_label",
        "verified_owner_id",
        "visual_id",
    }
)


def product_selection_capability_record_to_json(
    record: ProductSelectionCapabilityRecord,
) -> str:
    """Serialize one parent-bound Product selection capability."""
    return _canonical(
        {
            "catalog_identity": record.catalog_identity,
            "conversation_session_id": record.conversation_session_id,
            "expires_at": _moment(record.expires_at),
            "parent_artifact": _parent_payload(record),
            "product": {
                "commerce_scope": record.product.commerce_scope.value,
                "marketplace": record.product.marketplace.value,
                "value": record.product.value,
            },
            "reference": record.reference.value,
            "safe_signature": _safe_product_payload(record.safe_signature),
            "verified_owner_id": record.verified_owner_id,
        }
    )


def product_selection_capability_record_from_json(
    serialized: str,
) -> ProductSelectionCapabilityRecord:
    """Decode and re-attest only the exact canonical Product capability shape."""
    item = _object(
        _load(serialized, "product capability"), _PRODUCT_CAPABILITY_FIELDS, "product capability"
    )
    parent = _parent(item["parent_artifact"], "product capability parent")
    product = _object(item["product"], _PRODUCT_FIELDS, "product capability product")
    record = ProductSelectionCapabilityRecord(
        **_custody_kwargs(item, parent, "product capability"),
        product=ProductKey(
            _text(product["value"], "product capability product value"),
            CommerceScopeKey(_text(product["commerce_scope"], "product capability commerce scope")),
            MarketplaceKey(_text(product["marketplace"], "product capability marketplace")),
        ),
        safe_signature=_safe_product_from_payload(item["safe_signature"]),
        catalog_identity=_text(item["catalog_identity"], "product capability catalog identity"),
    )
    _attest(product_selection_capability_record_to_json(record), serialized, "product capability")
    return record


def media_scope_capability_record_to_json(record: MediaScopeCapabilityRecord) -> str:
    """Serialize one parent-bound Media scope capability."""
    return _canonical(
        {
            "catalog_identity": record.catalog_identity,
            "conversation_session_id": record.conversation_session_id,
            "expires_at": _moment(record.expires_at),
            "parent_artifact": _parent_payload(record),
            "reference": record.reference.value,
            "safe_signature": {"label": record.safe_signature.label},
            "scope": {
                "channel": record.scope.channel.value,
                "commerce_scope": record.scope.commerce_scope.value,
                "marketplace": record.scope.marketplace.value,
                "platform": record.scope.platform.value,
            },
            "verified_owner_id": record.verified_owner_id,
        }
    )


def media_scope_capability_record_from_json(serialized: str) -> MediaScopeCapabilityRecord:
    """Decode and re-attest only the exact canonical Media scope capability shape."""
    item = _object(
        _load(serialized, "media scope capability"),
        _MEDIA_SCOPE_CAPABILITY_FIELDS,
        "media scope capability",
    )
    parent = _parent(item["parent_artifact"], "media scope capability parent")
    scope = _object(item["scope"], _SCOPE_FIELDS, "media scope capability scope")
    signature = _object(
        item["safe_signature"], _SAFE_MEDIA_SCOPE_FIELDS, "media scope capability signature"
    )
    record = MediaScopeCapabilityRecord(
        **_custody_kwargs(item, parent, "media scope capability"),
        scope=MediaScope(
            commerce_scope=CommerceScopeKey(
                _text(scope["commerce_scope"], "media scope capability commerce scope")
            ),
            platform=PlatformKey(_text(scope["platform"], "media scope capability platform")),
            channel=ChannelKey(_text(scope["channel"], "media scope capability channel")),
            marketplace=MarketplaceKey(
                _text(scope["marketplace"], "media scope capability marketplace")
            ),
        ),
        safe_signature=SafeMediaScopeDisplaySignature(
            _text(signature["label"], "media scope capability signature label")
        ),
        catalog_identity=_text(item["catalog_identity"], "media scope capability catalog identity"),
    )
    _attest(media_scope_capability_record_to_json(record), serialized, "media scope capability")
    return record


def media_window_capability_record_to_json(record: MediaWindowCapabilityRecord) -> str:
    """Serialize one parent-bound Media temporal-resolution capability."""
    return _canonical(
        {
            "compare_previous": record.compare_previous,
            "comparison": _comparison_payload(record.comparison),
            "conversation_session_id": record.conversation_session_id,
            "expires_at": _moment(record.expires_at),
            "parent_artifact": _parent_payload(record),
            "reference": record.reference.value,
            "resolved": _resolved_payload(record.resolved),
            "verified_owner_id": record.verified_owner_id,
        }
    )


def media_window_capability_record_from_json(serialized: str) -> MediaWindowCapabilityRecord:
    """Decode and re-attest only the exact canonical Media window capability shape."""
    item = _object(
        _load(serialized, "media window capability"),
        _MEDIA_WINDOW_CAPABILITY_FIELDS,
        "media window capability",
    )
    parent = _parent(item["parent_artifact"], "media window capability parent")
    record = MediaWindowCapabilityRecord(
        **_custody_kwargs(item, parent, "media window capability"),
        resolved=_resolved_from_payload(item["resolved"], "media window capability resolved"),
        compare_previous=_bool(item["compare_previous"], "media window compare_previous"),
        comparison=_comparison_from_payload(item["comparison"], "media window comparison"),
    )
    _attest(media_window_capability_record_to_json(record), serialized, "media window capability")
    return record


def visual_narration_reference_record_to_json(
    record: VisualNarrationReferenceRecord,
) -> str:
    """Serialize one parent-bound visual narration reference without canonical selectors."""
    return _canonical(
        {
            "allowed_action_ids": list(record.allowed_action_ids),
            "basis": record.basis.value,
            "conversation_session_id": record.conversation_session_id,
            "expires_at": _moment(record.expires_at),
            "metric_cohort": list(record.metric_cohort),
            "parent_artifact": _parent_payload(record),
            "qualification": list(record.qualification),
            "reference": record.reference.value,
            "resolved": _resolved_payload(record.resolved),
            "safe_scope_label": record.safe_scope_label,
            "verified_owner_id": record.verified_owner_id,
            "visual_id": record.visual_id,
        }
    )


def visual_narration_reference_record_from_json(
    serialized: str,
) -> VisualNarrationReferenceRecord:
    """Decode and re-attest only the exact canonical narration-reference shape."""
    item = _object(
        _load(serialized, "visual narration reference"),
        _VISUAL_NARRATION_REFERENCE_FIELDS,
        "visual narration reference",
    )
    parent = _parent(item["parent_artifact"], "visual narration parent")
    record = VisualNarrationReferenceRecord(
        **_custody_kwargs(item, parent, "visual narration reference"),
        visual_id=_text(item["visual_id"], "visual narration visual id"),
        metric_cohort=_texts(item["metric_cohort"], "visual narration metric cohort"),
        safe_scope_label=_text(item["safe_scope_label"], "visual narration safe scope label"),
        resolved=_resolved_from_payload(item["resolved"], "visual narration resolved"),
        basis=MeasurementBasis(_text(item["basis"], "visual narration basis")),
        qualification=_texts(item["qualification"], "visual narration qualification"),
        allowed_action_ids=_texts(item["allowed_action_ids"], "visual narration action ids"),
    )
    _attest(
        visual_narration_reference_record_to_json(record),
        serialized,
        "visual narration reference",
    )
    return record


def _parent_payload(
    record: ProductSelectionCapabilityRecord
    | MediaScopeCapabilityRecord
    | MediaWindowCapabilityRecord
    | VisualNarrationReferenceRecord,
) -> dict[str, object]:
    return {
        "reference": record.parent_artifact_reference.value,
        "type": record.parent_artifact_type,
        "version": record.parent_artifact_version,
    }


def _parent(value: object, label: str) -> dict[str, object]:
    return _object(value, _PARENT_FIELDS, label)


def _custody_kwargs(
    item: dict[str, object], parent: dict[str, object], label: str
) -> _CustodyKwargs:
    return {
        "reference": PageContextReference(_text(item["reference"], f"{label} reference")),
        "parent_artifact_reference": PageContextReference(
            _text(parent["reference"], f"{label} parent reference")
        ),
        "parent_artifact_type": _text(parent["type"], f"{label} parent type"),
        "parent_artifact_version": _integer(parent["version"], f"{label} parent version"),
        "verified_owner_id": _text(item["verified_owner_id"], f"{label} owner"),
        "conversation_session_id": _text(
            item["conversation_session_id"], f"{label} conversation session"
        ),
        "expires_at": _datetime(item["expires_at"], f"{label} expiry"),
    }


def _safe_product_payload(signature: SafeProductDisplaySignature) -> dict[str, object]:
    return {
        "effective_name": signature.effective_name,
        "effective_name_status": signature.effective_name_status.value,
        "marketplace": signature.marketplace,
    }


def _safe_product_from_payload(value: object) -> SafeProductDisplaySignature:
    item = _object(value, _SAFE_PRODUCT_FIELDS, "product capability signature")
    name = item["effective_name"]
    if name is not None and not isinstance(name, str):
        raise ValueError("product capability effective name is invalid.")
    return SafeProductDisplaySignature(
        effective_name=name,
        effective_name_status=EffectiveNameStatus(
            _text(item["effective_name_status"], "product capability effective name status")
        ),
        marketplace=_text(item["marketplace"], "product capability signature marketplace"),
    )


def _resolved_payload(resolved: TemporalResolved) -> dict[str, object]:
    return {
        "display_grain": resolved.display_grain.value,
        "reporting_window_status": resolved.reporting_window_status.value,
        "selection": {
            "kind": resolved.selection.kind.value,
            "preset_id": None
            if resolved.selection.preset_id is None
            else resolved.selection.preset_id.value,
        },
        "window": {
            "end": resolved.window.end.isoformat(),
            "start": resolved.window.start.isoformat(),
        },
    }


def _resolved_from_payload(value: object, label: str) -> TemporalResolved:
    item = _object(value, _RESOLVED_FIELDS, label)
    selection = _object(item["selection"], _RESOLVED_SELECTION_FIELDS, f"{label} selection")
    period = _object(item["window"], _PERIOD_FIELDS, f"{label} window")
    preset = selection["preset_id"]
    if preset is not None and not isinstance(preset, str):
        raise ValueError(f"{label} selection preset is invalid.")
    return TemporalResolved(
        window=DateRange(
            _date(period["start"], f"{label} window start"),
            _date(period["end"], f"{label} window end"),
        ),
        reporting_window_status=TemporalReportingWindowStatus(
            _text(item["reporting_window_status"], f"{label} reporting window status")
        ),
        selection=TemporalResolvedSelection(
            kind=TemporalResolvedSelectionKind(_text(selection["kind"], f"{label} selection kind")),
            preset_id=None if preset is None else TemporalPresetId(preset),
        ),
        display_grain=TemporalDisplayGrain(_text(item["display_grain"], f"{label} display grain")),
    )


def _comparison_payload(comparison: TemporalComparison) -> dict[str, object]:
    return {
        "policy": comparison.policy.value,
        "reason_code": None if comparison.reason_code is None else comparison.reason_code.value,
        "status": comparison.status.value,
    }


def _comparison_from_payload(value: object, label: str) -> TemporalComparison:
    item = _object(value, _COMPARISON_FIELDS, label)
    reason = item["reason_code"]
    if reason is not None and not isinstance(reason, str):
        raise ValueError(f"{label} reason code is invalid.")
    return TemporalComparison(
        policy=TemporalComparisonPolicy(_text(item["policy"], f"{label} policy")),
        status=TemporalComparisonStatus(_text(item["status"], f"{label} status")),
        reason_code=None if reason is None else ComparisonUnavailableReason(reason),
    )


def _canonical(value: dict[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _pairs(values: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in values:
        if key in result:
            raise ValueError(f"presentation capability JSON repeats field {key!r}.")
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


def _texts(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list.")
    items = cast("list[object]", value)
    return tuple(_text(item, label) for item in items)


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be nonblank text.")
    return value


def _bool(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{label} must be a bool.")
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


def _attest(actual: str, expected: str, label: str) -> None:
    if actual != expected:
        raise ValueError(f"{label} is not canonical JSON.")
