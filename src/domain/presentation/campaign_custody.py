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
from domain.presentation.preset_artifacts import (
    PresetArtifactFilterIdentity,
    PresetArtifactSiblingDescriptor,
    PresetArtifactSiblingIdentity,
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
    """Ordered custody rows matching the public artifact rows exactly, plus its body binding."""

    body_digest: str
    rows: tuple[CampaignCustodyRow, ...]
    descriptors: tuple[PresetArtifactSiblingDescriptor, ...] = ()
    filter_siblings: tuple[PresetArtifactSiblingDescriptor, ...] | None = None
    """All-channels custody's closed set of channel filter siblings (ADR-127 decision 2); empty
    on a filter sibling's own custody and on a segment's, and `None` on a retained manifest
    baked before render format 11."""
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
        identities = tuple(item.identity for item in self.descriptors)
        if len(set(identities)) != len(identities):
            raise ValueError("campaign custody sibling descriptors must be identity-unique.")
        if any(not isinstance(item, PresetArtifactSiblingIdentity) for item in identities):
            raise TypeError("campaign custody descriptors name ordered segment siblings.")
        siblings = tuple(item.identity for item in self.filter_siblings or ())
        if any(not isinstance(item, PresetArtifactFilterIdentity) for item in siblings):
            raise TypeError("campaign custody filter siblings name filter identities.")
        if len(set(siblings)) != len(siblings):
            raise ValueError("campaign custody filter siblings must be identity-unique.")
        filters = {item.filter_id for item in self.segment_identities()}
        if self.filter_siblings is not None and None in filters:
            raise ValueError("a current campaign custody names each segment's filter.")
        if len(filters) > 1:
            raise ValueError("one campaign custody binds the segments of one filter.")

    def segment_identities(self) -> tuple[PresetArtifactSiblingIdentity, ...]:
        """Return the ordered segment identities the descriptors name."""
        return tuple(
            cast("PresetArtifactSiblingIdentity", item.identity) for item in self.descriptors
        )

    def filter_sibling(self, filter_id: str) -> PresetArtifactSiblingDescriptor | None:
        """Return the descriptor binding one channel filter sibling, or none if not offered."""
        return next(
            (
                item
                for item in self.filter_siblings or ()
                if cast("PresetArtifactFilterIdentity", item.identity).filter_id == filter_id
            ),
            None,
        )


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
    """Encode the exact private custody contract; never any public display value beyond it.

    A retained manifest (no filter siblings) encodes its descriptors without a filter id.
    """
    current = manifest.filter_siblings is not None
    return {
        "body_digest": manifest.body_digest,
        "descriptors": [
            {
                "body_digest": item.body_digest,
                "custody_digest": item.custody_digest,
                **({"filter_id": identity.filter_id} if current else {}),
                "ordering": identity.ordering,
                "segment_index": identity.segment_index,
            }
            for item, identity in zip(
                manifest.descriptors, manifest.segment_identities(), strict=True
            )
        ],
        **(
            {
                "filter_siblings": [
                    {
                        "body_digest": item.body_digest,
                        "custody_digest": item.custody_digest,
                        "filter_id": cast("PresetArtifactFilterIdentity", item.identity).filter_id,
                    }
                    for item in manifest.filter_siblings or ()
                ]
            }
            if current
            else {}
        ),
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


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a nonnegative integer.")
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
    """Decode and re-attest the exact private campaign custody contract."""
    if not isinstance(value, dict):
        raise ValueError("campaign custody manifest fields are not exact.")
    raw = cast("dict[str, object]", value)
    fields = frozenset(raw)
    filtered_fields = frozenset({"body_digest", "descriptors", "filter_siblings", "kind", "rows"})
    current_fields = frozenset({"body_digest", "descriptors", "kind", "rows"})
    legacy_fields = frozenset({"body_digest", "kind", "rows"})
    if fields not in {filtered_fields, current_fields, legacy_fields}:
        raise ValueError("campaign custody manifest fields are not exact.")
    filtered = fields == filtered_fields
    descriptor_fields = (
        frozenset({"body_digest", "custody_digest", "filter_id", "ordering", "segment_index"})
        if filtered
        else frozenset({"body_digest", "custody_digest", "ordering", "segment_index"})
    )
    item = raw
    if item["kind"] != CAMPAIGN_CUSTODY_MANIFEST_KIND:
        raise ValueError("campaign custody manifest kind is unsupported.")
    rows_value = item["rows"]
    if not isinstance(rows_value, list):
        raise ValueError("campaign custody manifest rows must be a list.")
    descriptors_value = item.get("descriptors", [])
    if not isinstance(descriptors_value, list):
        raise ValueError("campaign custody descriptors must be a list.")
    descriptors = tuple(
        PresetArtifactSiblingDescriptor(
            PresetArtifactSiblingIdentity(
                _text(
                    descriptor["ordering"],
                    "campaign custody descriptor ordering",
                ),
                _integer(
                    descriptor["segment_index"],
                    "campaign custody descriptor segment index",
                ),
                _text(descriptor["filter_id"], "campaign custody descriptor filter")
                if filtered
                else None,
            ),
            _text(descriptor["body_digest"], "campaign custody descriptor body digest"),
            _text(
                descriptor["custody_digest"],
                "campaign custody descriptor custody digest",
            ),
        )
        for descriptor in (
            _object(value, descriptor_fields, "campaign custody descriptor")
            for value in cast("list[object]", descriptors_value)
        )
    )
    siblings_value = item.get("filter_siblings", [])
    if not isinstance(siblings_value, list):
        raise ValueError("campaign custody filter siblings must be a list.")
    siblings = tuple(
        PresetArtifactSiblingDescriptor(
            PresetArtifactFilterIdentity(_text(sibling["filter_id"], "campaign custody filter")),
            _text(sibling["body_digest"], "campaign custody filter body digest"),
            _text(sibling["custody_digest"], "campaign custody filter custody digest"),
        )
        for sibling in (
            _object(
                value,
                frozenset({"body_digest", "custody_digest", "filter_id"}),
                "campaign custody filter sibling",
            )
            for value in cast("list[object]", siblings_value)
        )
    )
    result = CampaignCustodyManifest(
        _text(item["body_digest"], "campaign custody manifest body digest"),
        tuple(_row_from_payload(row) for row in cast("list[object]", rows_value)),
        descriptors,
        siblings if filtered else None,
    )
    canonical = campaign_custody_manifest_payload(result)
    if fields == legacy_fields:
        canonical.pop("descriptors")
    if canonical != item:
        raise ValueError("campaign custody manifest payload is not canonical.")
    return result
