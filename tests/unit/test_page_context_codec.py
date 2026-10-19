# SPDX-License-Identifier: MIT-0
"""Strict canonical page-context serialization for immutable storage."""

import json
from dataclasses import replace
from datetime import UTC, date, datetime
from typing import cast

import pytest

from adapters.page_context_codec import (
    page_context_record_from_json,
    page_context_record_to_json,
)
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

pytestmark = pytest.mark.unit

SCOPE = CommerceScopeKey("amazon_retail_us")
PLATFORM = PlatformKey("amazon_ads")
REFERENCE = PageContextReference("A" * 43)
SNAPSHOT = PageContextSnapshot(
    response_identity="response-fictional",
    visible_window=DateRange(date(2026, 8, 10), date(2026, 8, 16)),
    reporting_window_status=ReportingWindowStatus.PREFERRED_DEFAULT,
    comparison_window=DateRange(date(2026, 8, 3), date(2026, 8, 9)),
    campaign=CampaignKey("campaign:fictional", SCOPE, PLATFORM),
    channel=ChannelKey("amazon_dsp"),
    marketplace=MarketplaceKey("amazon_us"),
)
EXPIRES_AT = datetime(2026, 8, 19, 13, 30, tzinfo=UTC)
PAGE = PageContextRecord(
    REFERENCE,
    SNAPSHOT,
    "verified-owner",
    "conversation-session",
    EXPIRES_AT,
)


def test_page_codec_round_trips_every_typed_selector_and_custody_field() -> None:
    serialized = page_context_record_to_json(PAGE)

    assert page_context_record_from_json(serialized) == PAGE
    assert '"commerce_scope":"amazon_retail_us"' in serialized
    assert '"platform":"amazon_ads"' in serialized
    assert '"verified_owner_id":"verified-owner"' in serialized
    assert '"conversation_session_id":"conversation-session"' in serialized
    assert serialized == page_context_record_to_json(page_context_record_from_json(serialized))


@pytest.mark.parametrize(
    ("field", "snapshot"),
    [
        ("campaign", replace(SNAPSHOT, campaign=None)),
        ("channel", replace(SNAPSHOT, channel=None)),
        ("marketplace", replace(SNAPSHOT, marketplace=None)),
        ("comparison_window", replace(SNAPSHOT, comparison_window=None)),
    ],
)
def test_optional_snapshot_fields_are_canonical_nulls(
    field: str,
    snapshot: PageContextSnapshot,
) -> None:
    serialized = page_context_record_to_json(
        PageContextRecord(REFERENCE, snapshot, "owner", "session", EXPIRES_AT)
    )

    assert f'"{field}":null' in serialized
    assert page_context_record_from_json(serialized).snapshot == snapshot


def test_unknown_missing_duplicate_and_noncanonical_fields_are_refused() -> None:
    serialized = page_context_record_to_json(PAGE)
    raw: object = json.loads(serialized)
    assert isinstance(raw, dict)
    decoded = cast("dict[str, object]", raw)

    unknown: dict[str, object] = {**decoded, "unknown": "value"}
    missing: dict[str, object] = {
        key: value for key, value in decoded.items() if key != "verified_owner_id"
    }
    duplicate = serialized.replace(
        f'"reference":"{REFERENCE.value}"',
        f'"reference":"{REFERENCE.value}","reference":"{REFERENCE.value}"',
    )
    noncanonical = serialized.replace(",", ", ", 1)

    for malformed in (
        json.dumps(unknown, sort_keys=True, separators=(",", ":")),
        json.dumps(missing, sort_keys=True, separators=(",", ":")),
        duplicate,
        noncanonical,
    ):
        with pytest.raises(ValueError):
            page_context_record_from_json(malformed)


def test_nested_unknown_fields_and_noncanonical_expiry_are_refused() -> None:
    raw: object = json.loads(page_context_record_to_json(PAGE))
    assert isinstance(raw, dict)
    decoded = cast("dict[str, object]", raw)
    snapshot_raw = decoded["snapshot"]
    assert isinstance(snapshot_raw, dict)
    snapshot = cast("dict[str, object]", snapshot_raw)
    snapshot["unknown"] = "value"
    malformed_snapshot = json.dumps(decoded, sort_keys=True, separators=(",", ":"))

    canonical = page_context_record_to_json(PAGE)
    noncanonical_expiry = canonical.replace("2026-08-19T13:30:00+00:00", "2026-08-19T13:30:00Z")

    with pytest.raises(ValueError):
        page_context_record_from_json(malformed_snapshot)
    with pytest.raises(ValueError, match="canonical UTC"):
        page_context_record_from_json(noncanonical_expiry)


def test_product_selector_round_trips_with_scope_and_marketplace_qualifiers() -> None:
    product = ProductKey("product:fictional", SCOPE, MarketplaceKey("amazon_us"))
    page = replace(PAGE, snapshot=replace(SNAPSHOT, product=product))

    serialized = page_context_record_to_json(page)
    restored = page_context_record_from_json(serialized)

    assert restored.snapshot.product == product
    assert (
        '"product":{"commerce_scope":"amazon_retail_us","marketplace":"amazon_us",'
        '"value":"product:fictional"}' in serialized
    )


LABELS = PageContextLabels(
    page_id="campaigns",
    preset_id="7d",
    page_context_label="Campaigns · Amazon DSP · Fictional campaign · Aug 10\u201316, 2026",
    display_labels=PageContextDisplayLabels(
        page="Campaigns",
        window="Aug 10\u201316, 2026",
        channel="Amazon DSP",
        campaign="Fictional campaign",
    ),
)


@pytest.mark.covers("ADR-120")
def test_a_v1_snapshot_keeps_its_bytes_and_a_v2_snapshot_round_trips_its_labels() -> None:
    v1 = page_context_record_to_json(PAGE)
    v2_record = replace(PAGE, snapshot=replace(SNAPSHOT, labels=LABELS))
    v2 = page_context_record_to_json(v2_record)

    # Records written before ADR-120 carry no `labels` key and still decode canonically.
    assert '"labels"' not in v1
    assert page_context_record_from_json(v1) == PAGE
    assert page_context_record_from_json(v2) == v2_record
    labels = json.loads(v2)["snapshot"]["labels"]
    assert labels["display_labels"]["product"] is None
    assert labels["page_context_label"].startswith("Campaigns · Amazon DSP")


@pytest.mark.covers("ADR-120")
def test_snapshot_labels_refuse_unknown_fields_and_blank_or_unbounded_text() -> None:
    document = json.loads(
        page_context_record_to_json(replace(PAGE, snapshot=replace(SNAPSHOT, labels=LABELS)))
    )
    document["snapshot"]["labels"]["display_labels"]["filter"] = "x"
    with pytest.raises(ValueError, match="not exact"):
        page_context_record_from_json(json.dumps(document, sort_keys=True, separators=(",", ":")))
    with pytest.raises(ValueError, match="page context window"):
        PageContextDisplayLabels(page="Campaigns", window=" ")
    with pytest.raises(ValueError, match="page context campaign"):
        PageContextDisplayLabels(page="Campaigns", window="Aug 10", campaign="x" * 401)
    with pytest.raises(ValueError, match="declared page identity"):
        replace(LABELS, page_id="Campaigns Page")
