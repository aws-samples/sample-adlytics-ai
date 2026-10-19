# SPDX-License-Identifier: MIT-0
"""Strict canonical JSON for immutable page-context records."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import cast

from domain.semantics.identity import (
    CampaignKey,
    ChannelKey,
    CommerceScopeKey,
    MarketplaceKey,
    PlatformKey,
    ProductKey,
)
from domain.semantics.page_context import (
    PageContextDisplayLabels,
    PageContextLabels,
    PageContextRecord,
    PageContextReference,
    PageContextSnapshot,
    ReportingWindowStatus,
)
from domain.shared_kernel.values import DateRange

__all__ = ["page_context_record_from_json", "page_context_record_to_json"]

_PAGE_FIELDS = frozenset(
    {
        "conversation_session_id",
        "expires_at",
        "reference",
        "snapshot",
        "verified_owner_id",
    }
)
_SNAPSHOT_FIELDS = frozenset(
    {
        "campaign",
        "channel",
        "comparison_window",
        "marketplace",
        "product",
        "response_identity",
        "reporting_window_status",
        "visible_window",
    }
)
_LABELS_FIELDS = frozenset({"display_labels", "page_context_label", "page_id", "preset_id"})
_DISPLAY_FIELDS = frozenset({"campaign", "channel", "marketplace", "page", "product", "window"})
_WINDOW_FIELDS = frozenset({"end", "start"})
_CAMPAIGN_FIELDS = frozenset({"commerce_scope", "platform", "value"})
_PRODUCT_FIELDS = frozenset({"commerce_scope", "marketplace", "value"})
_KEY_FIELDS = frozenset({"value"})


def _canonical(value: Mapping[str, object]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _moment(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _window(value: DateRange) -> dict[str, object]:
    return {"end": value.end.isoformat(), "start": value.start.isoformat()}


def _key(value: ChannelKey | MarketplaceKey) -> dict[str, object]:
    return {"value": value.value}


def _campaign(value: CampaignKey) -> dict[str, object]:
    return {
        "commerce_scope": value.commerce_scope.value,
        "platform": value.platform.value,
        "value": value.value,
    }


def _product(value: ProductKey) -> dict[str, object]:
    return {
        "commerce_scope": value.commerce_scope.value,
        "marketplace": value.marketplace.value,
        "value": value.value,
    }


def _labels(value: PageContextLabels) -> dict[str, object]:
    display = value.display_labels
    return {
        "display_labels": {
            "campaign": display.campaign,
            "channel": display.channel,
            "marketplace": display.marketplace,
            "page": display.page,
            "product": display.product,
            "window": display.window,
        },
        "page_context_label": value.page_context_label,
        "page_id": value.page_id,
        "preset_id": value.preset_id,
    }


def _snapshot(value: PageContextSnapshot) -> dict[str, object]:
    # A v1 snapshot has no `labels` key, so records written before ADR-120 stay canonical.
    labels = {} if value.labels is None else {"labels": _labels(value.labels)}
    return {
        **labels,
        "campaign": None if value.campaign is None else _campaign(value.campaign),
        "channel": None if value.channel is None else _key(value.channel),
        "comparison_window": (
            None if value.comparison_window is None else _window(value.comparison_window)
        ),
        "marketplace": None if value.marketplace is None else _key(value.marketplace),
        "product": None if value.product is None else _product(value.product),
        "response_identity": value.response_identity,
        "reporting_window_status": value.reporting_window_status.value,
        "visible_window": _window(value.visible_window),
    }


def page_context_record_to_json(record: PageContextRecord) -> str:
    """Serialize one complete page record into canonical bytes."""
    return _canonical(
        {
            "conversation_session_id": record.conversation_session_id,
            "expires_at": _moment(record.expires_at),
            "reference": record.reference.value,
            "snapshot": _snapshot(record.snapshot),
            "verified_owner_id": record.verified_owner_id,
        }
    )


def _pairs(values: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in values:
        if key in result:
            raise ValueError(f"page context JSON repeats field {key!r}.")
        result[key] = value
    return result


def _load(serialized: str) -> dict[str, object]:
    try:
        raw: object = json.loads(serialized, object_pairs_hook=_pairs)
    except TypeError, ValueError:
        raise ValueError("page context record is not valid canonical JSON.") from None
    if not isinstance(raw, dict):
        raise ValueError("page context record must be an object.")
    return cast("dict[str, object]", raw)


def _object(value: object, label: str, expected: frozenset[str]) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object.")
    item = cast("dict[str, object]", value)
    actual = frozenset(item)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        raise ValueError(f"{label} fields are not exact; missing={missing}, unknown={unknown}.")
    return item


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty text.")
    return value


def _date(value: object, label: str) -> date:
    text = _text(value, label)
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        raise ValueError(f"{label} must be an ISO date.") from None
    if parsed.isoformat() != text:
        raise ValueError(f"{label} must use canonical ISO date form.")
    return parsed


def _datetime(value: object, label: str) -> datetime:
    text = _text(value, label)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"{label} must be an ISO instant.") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must include a timezone offset.")
    result = parsed.astimezone(UTC)
    if result.isoformat() != text:
        raise ValueError(f"{label} must use canonical UTC ISO form.")
    return result


def _read_window(value: object, label: str) -> DateRange:
    item = _object(value, label, _WINDOW_FIELDS)
    return DateRange(
        start=_date(item["start"], f"{label} start"),
        end=_date(item["end"], f"{label} end"),
    )


def _read_optional_window(value: object, label: str) -> DateRange | None:
    return None if value is None else _read_window(value, label)


def _read_campaign(value: object) -> CampaignKey | None:
    if value is None:
        return None
    item = _object(value, "page context campaign", _CAMPAIGN_FIELDS)
    return CampaignKey(
        _text(item["value"], "campaign value"),
        CommerceScopeKey(_text(item["commerce_scope"], "campaign commerce scope")),
        PlatformKey(_text(item["platform"], "campaign platform")),
    )


def _read_product(value: object) -> ProductKey | None:
    if value is None:
        return None
    item = _object(value, "page context product", _PRODUCT_FIELDS)
    return ProductKey(
        _text(item["value"], "product value"),
        CommerceScopeKey(_text(item["commerce_scope"], "product commerce scope")),
        MarketplaceKey(_text(item["marketplace"], "product marketplace")),
    )


def _read_key(value: object, label: str, key_type: type[ChannelKey]) -> ChannelKey | None:
    if value is None:
        return None
    item = _object(value, label, _KEY_FIELDS)
    return key_type(_text(item["value"], f"{label} value"))


def _read_marketplace(value: object) -> MarketplaceKey | None:
    if value is None:
        return None
    item = _object(value, "page context marketplace", _KEY_FIELDS)
    return MarketplaceKey(_text(item["value"], "page context marketplace value"))


def _optional_text(value: object, label: str) -> str | None:
    return None if value is None else _text(value, label)


def _read_labels(value: object) -> PageContextLabels:
    item = _object(value, "page context labels", _LABELS_FIELDS)
    display = _object(item["display_labels"], "page context display labels", _DISPLAY_FIELDS)
    return PageContextLabels(
        page_id=_text(item["page_id"], "page context page id"),
        preset_id=_optional_text(item["preset_id"], "page context preset id"),
        page_context_label=_text(item["page_context_label"], "page context label"),
        display_labels=PageContextDisplayLabels(
            page=_text(display["page"], "page display label"),
            window=_text(display["window"], "window display label"),
            channel=_optional_text(display["channel"], "channel display label"),
            campaign=_optional_text(display["campaign"], "campaign display label"),
            product=_optional_text(display["product"], "product display label"),
            marketplace=_optional_text(display["marketplace"], "marketplace display label"),
        ),
    )


def _read_snapshot(value: object) -> PageContextSnapshot:
    if not isinstance(value, dict):
        raise ValueError("page context snapshot must be an object.")
    raw = cast("dict[str, object]", value)
    versioned = "labels" in raw
    item = _object(
        raw,
        "page context snapshot",
        _SNAPSHOT_FIELDS | {"labels"} if versioned else _SNAPSHOT_FIELDS,
    )
    return PageContextSnapshot(
        labels=_read_labels(item["labels"]) if versioned else None,
        response_identity=_text(item["response_identity"], "page response identity"),
        visible_window=_read_window(item["visible_window"], "page visible window"),
        reporting_window_status=ReportingWindowStatus(
            _text(item["reporting_window_status"], "page reporting window status")
        ),
        comparison_window=_read_optional_window(
            item["comparison_window"], "page comparison window"
        ),
        campaign=_read_campaign(item["campaign"]),
        product=_read_product(item["product"]),
        channel=_read_key(item["channel"], "page context channel", ChannelKey),
        marketplace=_read_marketplace(item["marketplace"]),
    )


def page_context_record_from_json(serialized: str) -> PageContextRecord:
    """Decode only the exact canonical page-record shape."""
    raw = _object(_load(serialized), "page context record", _PAGE_FIELDS)
    record = PageContextRecord(
        reference=PageContextReference(_text(raw["reference"], "page context reference")),
        snapshot=_read_snapshot(raw["snapshot"]),
        verified_owner_id=_text(raw["verified_owner_id"], "page context owner"),
        conversation_session_id=_text(
            raw["conversation_session_id"], "page context conversation session"
        ),
        expires_at=_datetime(raw["expires_at"], "page context expiry"),
    )
    if page_context_record_to_json(record) != serialized:
        raise ValueError("page context record is not canonical JSON.")
    return record
