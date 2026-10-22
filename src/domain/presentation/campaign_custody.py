# SPDX-License-Identifier: MIT-0
"""Private campaign custody manifest binding baked public rows to canonical selectors."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import cast

from domain.presentation.campaign_attribution import (
    SafeCampaignDisplaySignature,
    safe_campaign_display_signature_from_payload,
    safe_campaign_display_signature_payload,
)
from domain.semantics.identity import (
    CampaignKey,
    ChannelKey,
    CommerceScopeKey,
    MarketplaceKey,
    PlatformKey,
)

__all__ = [
    "CAMPAIGN_CUSTODY_MANIFEST_KIND",
    "CampaignCustodyManifest",
    "CampaignCustodyRow",
    "campaign_custody_manifest_from_payload",
    "campaign_custody_manifest_payload",
    "custody_rows_align",
]

CAMPAIGN_CUSTODY_MANIFEST_KIND = "campaign_custody_manifest"
_DIGEST = re.compile(r"^[a-f0-9]{64}$")


@dataclass(frozen=True, slots=True)
class CampaignCustodyRow:
    """Canonical selectors and the safe display signature for one baked public row."""

    campaign: CampaignKey
    channel: ChannelKey
    marketplace: MarketplaceKey
    safe_signature: SafeCampaignDisplaySignature

    def __post_init__(self) -> None:
        if not isinstance(cast("object", self.campaign), CampaignKey):
            raise TypeError("campaign custody row requires a CampaignKey.")
        if not isinstance(cast("object", self.channel), ChannelKey):
            raise TypeError("campaign custody row requires a ChannelKey.")
        if not isinstance(cast("object", self.marketplace), MarketplaceKey):
            raise TypeError("campaign custody row requires a MarketplaceKey.")
        if not isinstance(cast("object", self.safe_signature), SafeCampaignDisplaySignature):
            raise TypeError("campaign custody row requires a SafeCampaignDisplaySignature.")


@dataclass(frozen=True, slots=True)
class CampaignCustodyManifest:
    """Ordered custody rows matching the public artifact rows exactly, plus its body binding.

    Since render format 12 (ADR-145) a Campaigns custody names no sibling: the parent custody's
    stage-built sibling index binds every segment and channel filter sibling by digest, and a
    segment's or filter sibling's own custody carries only its rows.
    """

    body_digest: str
    rows: tuple[CampaignCustodyRow, ...]
    kind: str = CAMPAIGN_CUSTODY_MANIFEST_KIND

    def __post_init__(self) -> None:
        if self.kind != CAMPAIGN_CUSTODY_MANIFEST_KIND:
            raise ValueError("campaign custody manifest kind is unsupported.")
        if not isinstance(cast("object", self.body_digest), str) or not _DIGEST.fullmatch(
            self.body_digest
        ):
            raise ValueError("campaign custody manifest body digest is invalid.")
        if any(not isinstance(cast("object", row), CampaignCustodyRow) for row in self.rows):
            raise TypeError("campaign custody manifest rows must be custody rows.")
        campaigns = tuple(row.campaign for row in self.rows)
        signatures = tuple(row.safe_signature for row in self.rows)
        if len(set(campaigns)) != len(campaigns) or len(set(signatures)) != len(signatures):
            raise ValueError("campaign custody manifest rows must be identity-unique.")


def custody_rows_align(
    manifest: CampaignCustodyManifest,
    signatures: tuple[SafeCampaignDisplaySignature, ...],
) -> bool:
    """Whether custody rows match the public artifact rows exactly, in order."""
    return tuple(row.safe_signature for row in manifest.rows) == signatures


def _campaign_payload(campaign: CampaignKey) -> dict[str, object]:
    return {
        "commerce_scope": campaign.commerce_scope.value,
        "platform": campaign.platform.value,
        "value": campaign.value,
    }


def _row_payload(row: CampaignCustodyRow) -> dict[str, object]:
    return {
        "campaign": _campaign_payload(row.campaign),
        "channel": row.channel.value,
        "marketplace": row.marketplace.value,
        "signature": safe_campaign_display_signature_payload(row.safe_signature),
    }


def campaign_custody_manifest_payload(manifest: CampaignCustodyManifest) -> dict[str, object]:
    """Encode the exact private custody contract; never any public display value beyond it."""
    return {
        "body_digest": manifest.body_digest,
        "kind": manifest.kind,
        "rows": [_row_payload(row) for row in manifest.rows],
    }


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


def _row_from_payload(value: object) -> CampaignCustodyRow:
    item = _object(
        value,
        frozenset({"campaign", "channel", "marketplace", "signature"}),
        "campaign custody row",
    )
    campaign_item = _object(
        item["campaign"],
        frozenset({"commerce_scope", "platform", "value"}),
        "campaign custody campaign",
    )
    try:
        campaign = CampaignKey(
            _text(campaign_item["value"], "campaign custody campaign value"),
            CommerceScopeKey(_text(campaign_item["commerce_scope"], "campaign custody scope")),
            PlatformKey(_text(campaign_item["platform"], "campaign custody platform")),
        )
        channel = ChannelKey(_text(item["channel"], "campaign custody channel"))
        marketplace = MarketplaceKey(_text(item["marketplace"], "campaign custody marketplace"))
    except TypeError, ValueError:
        raise ValueError("campaign custody row selectors are invalid.") from None
    return CampaignCustodyRow(
        campaign,
        channel,
        marketplace,
        safe_campaign_display_signature_from_payload(item["signature"]),
    )


def campaign_custody_manifest_from_payload(value: object) -> CampaignCustodyManifest:
    """Decode and re-attest the exact private campaign custody contract.

    A parent custody's `sibling_index` is the stage's, split off before this decode
    (`split_parent_custody`); a custody that still lists siblings inline is refused.
    """
    item = _object(value, frozenset({"body_digest", "kind", "rows"}), "campaign custody manifest")
    if item["kind"] != CAMPAIGN_CUSTODY_MANIFEST_KIND:
        raise ValueError("campaign custody manifest kind is unsupported.")
    rows_value = item["rows"]
    if not isinstance(rows_value, list):
        raise ValueError("campaign custody manifest rows must be a list.")
    result = CampaignCustodyManifest(
        _text(item["body_digest"], "campaign custody manifest body digest"),
        tuple(_row_from_payload(row) for row in cast("list[object]", rows_value)),
    )
    if campaign_custody_manifest_payload(result) != item:
        raise ValueError("campaign custody manifest payload is not canonical.")
    return result
