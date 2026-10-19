# SPDX-License-Identifier: MIT-0
"""Compile source-aligned evidence into canonical rows through governed identity bridges."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Union

from domain.data_contracts.contract import DataContract, NullMeaning
from domain.semantics.commerce_identity import (
    CommerceIdentityBridge,
    ResolveCommerceIdentityRequest,
    resolve_commerce_identity,
)
from domain.semantics.curation import CanonicalCurationPlan
from domain.semantics.identity import (
    CanonicalAdvertiserKeyRequest,
    CanonicalCampaignKeyRequest,
    SourceIdentifier,
    canonical_advertiser_key,
    canonical_campaign_key,
)
from domain.semantics.media_taxonomy import MediaTaxonomy, MediaTaxonomyResolution
from domain.semantics.source_aligned import SourceAlignedRow
from domain.semantics.source_pack import SourcePack

__all__ = [
    "CanonicalCompileRequest",
    "CanonicalCompileResult",
    "CanonicalRow",
    "CanonicalScalar",
    "IdentityMappingEvidence",
    "canonical_checksum_digest",
    "canonical_rows_checksum",
    "canonical_scalar",
    "compile_canonical_rows",
]

_CHECKSUM_PREFIX = "sha256:"
_DIGEST_HEX_LENGTH = 64
_LOWER_HEX = frozenset("0123456789abcdef")

type CanonicalScalar = Union[  # noqa: UP007 — avoids executable `|` in a runtime type alias
    str, int, float, Decimal, bool, date, datetime, None
]
type CanonicalRow = tuple[tuple[str, CanonicalScalar], ...]


@dataclass(frozen=True, slots=True)
class IdentityMappingEvidence:
    """Effective-dated source-to-canonical mapping selected for one candidate row."""

    source_namespace: str
    source_value: str
    target_value: str
    mapping_version: int
    valid_from: date
    valid_to: date | None
    source_system_key: str | None = None
    source_scope_namespace: str | None = None
    source_scope_value: str | None = None
    source_identifier_kind: str | None = None

    def __post_init__(self) -> None:
        if not self.source_namespace or not self.source_value or not self.target_value:
            raise ValueError("identity mapping evidence requires source and target identities.")
        if self.mapping_version < 1:
            raise ValueError("identity mapping evidence version starts at 1.")
        scoped = (
            self.source_system_key,
            self.source_scope_namespace,
            self.source_scope_value,
        )
        if any(value is not None for value in scoped) and not all(
            value is not None and value.strip() for value in scoped
        ):
            raise ValueError("identity mapping account evidence must be complete and nonblank.")
        if self.source_identifier_kind is not None and not self.source_identifier_kind.strip():
            raise ValueError("identity mapping identifier kind cannot be blank.")


@dataclass(frozen=True, slots=True)
class CanonicalCompileRequest:
    """Owner-authorized source evidence and legacy-mapped values awaiting identity conformance."""

    plan: CanonicalCurationPlan
    pack: SourcePack
    contract: DataContract
    mapped_rows: tuple[dict[str, object], ...]
    bridge: CommerceIdentityBridge
    included_ordinals: frozenset[int] | None = None
    taxonomy: MediaTaxonomy | None = None

    def __post_init__(self) -> None:
        if self.pack.contract_id != self.contract.contract_id:
            raise ValueError("canonical compiler pack and contract identities differ.")
        if self.pack.dataset != self.plan.partition.scope.dataset:
            raise ValueError("source pack targets another canonical dataset.")
        if len(self.plan.source_publications) != 1:
            raise ValueError("one source-pack compilation requires exactly one source publication.")
        if len(self.mapped_rows) != self.plan.source_publications[0].row_count:
            raise ValueError("mapped and source-aligned row counts differ.")
        if self.included_ordinals is not None and any(
            ordinal < 0 or ordinal >= len(self.mapped_rows) for ordinal in self.included_ordinals
        ):
            raise ValueError("canonical compiler included ordinal is outside the source rowset.")


@dataclass(frozen=True, slots=True)
class CanonicalCompileResult:
    """Immutable canonical rows and their deterministic byte-independent checksum."""

    rows: tuple[CanonicalRow, ...]
    content_checksum: str
    identity_evidence: tuple[IdentityMappingEvidence, ...]

    @property
    def row_count(self) -> int:
        """Number of canonical rows compiled."""
        return len(self.rows)


def _target_for(contract: DataContract, source_path: str) -> str:
    matches = tuple(
        field.target_column for field in contract.fields if field.source_path == source_path
    )
    if len(matches) != 1:
        raise ValueError(
            f"{contract.contract_id}: {source_path!r} does not map to exactly one target column."
        )
    return matches[0]


def _identifier(row: SourceAlignedRow, namespace: str) -> SourceIdentifier:
    identifiers = tuple(item for item in row.source_identifiers if item.namespace == namespace)
    if len(identifiers) != 1:
        raise ValueError(f"source row does not carry exactly one {namespace!r} identifier.")
    return identifiers[0]


def canonical_scalar(value: object, *, column: str) -> CanonicalScalar:
    """Narrow one validated row value to the scalar types canonical storage permits."""
    if value is None or isinstance(value, (str, int, float, Decimal, bool, date, datetime)):
        return value
    raise ValueError(f"canonical column {column!r} has an unsupported value type.")


def _json_scalar(value: CanonicalScalar) -> object:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("canonical datetime must be timezone-aware.")
        return {"type": "datetime", "value": value.astimezone(UTC).isoformat()}
    if isinstance(value, date):
        return {"type": "date", "value": value.isoformat()}
    if isinstance(value, Decimal):
        return {"type": "decimal", "value": str(value)}
    if value is None:
        return {"type": "null", "value": None}
    return {"type": type(value).__name__, "value": value}


def canonical_rows_checksum(rows: tuple[CanonicalRow, ...]) -> str:
    """Return the deterministic checksum shared by compiler and physical adapters."""
    payload = [
        [{"name": name, "value": _json_scalar(value)} for name, value in row] for row in rows
    ]
    rendered = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return f"{_CHECKSUM_PREFIX}{hashlib.sha256(rendered.encode()).hexdigest()}"


def canonical_checksum_digest(value: str) -> str:
    """Reduce a ``sha256:<hex>`` content checksum to its 64 lowercase hex digest.

    Publications render :func:`canonical_rows_checksum` with the algorithm prefix;
    the Stage-1 roster ``content_checksum`` member and every roster reader keep
    only the digest bytes, so both representations compare equal here.
    """
    digest = value.removeprefix(_CHECKSUM_PREFIX)
    if len(digest) != _DIGEST_HEX_LENGTH or not all(
        character in _LOWER_HEX for character in digest
    ):
        raise ValueError("content checksum must be sha256:<64 lowercase hex> or the bare digest.")
    return digest


def _media_evidence(
    resolution: MediaTaxonomyResolution,
) -> tuple[IdentityMappingEvidence, ...]:
    targets = (
        resolution.ecosystem,
        resolution.platform,
        resolution.channel_group,
        resolution.channel,
        resolution.ad_product,
    )
    return tuple(
        IdentityMappingEvidence(
            source_namespace=resolution.source.namespace,
            source_value=resolution.source.value,
            target_value=target.value,
            mapping_version=resolution.mapping_version,
            valid_from=resolution.valid_from,
            valid_to=resolution.valid_to,
        )
        for target in targets
        if target is not None
    )


def _apply_media(
    row: dict[str, object], contract: DataContract, resolution: MediaTaxonomyResolution
) -> None:
    values = {
        "ecosystem_key": resolution.ecosystem.value,
        "platform_key": resolution.platform.value,
        "channel_group_key": (
            None if resolution.channel_group is None else resolution.channel_group.value
        ),
        "channel_key": None if resolution.channel is None else resolution.channel.value,
        "ad_product_key": None if resolution.ad_product is None else resolution.ad_product.value,
    }
    for column, value in values.items():
        if column in contract.target_columns:
            field = contract.field_for(column)
            # A grain set with no media split declares the column NULL by option (ADR-099
            # point 2), and its media profile resolves it to nothing; any other column refuses.
            if value is None and not (
                field is not None
                and field.nullable
                and field.null_meaning is NullMeaning.NOT_APPLICABLE_UNDER_OPTIONS
            ):
                raise ValueError(f"{contract.contract_id}: media mapping supplies no {column}.")
            row[column] = value


@dataclass(frozen=True, slots=True)
class _CampaignAssignmentRequest:
    request: CanonicalCompileRequest
    source_row: SourceAlignedRow
    media: MediaTaxonomyResolution | None
    row: dict[str, object]
    evidence: set[IdentityMappingEvidence]


def _apply_campaign_identity(assignment: _CampaignAssignmentRequest) -> None:
    request = assignment.request
    source_row = assignment.source_row
    media = assignment.media
    row = assignment.row
    evidence = assignment.evidence
    campaign_field = request.pack.campaign
    if campaign_field is None:
        return
    if media is None or media.channel is None:
        raise ValueError("campaign-bearing source pack requires a resolved media channel.")
    delivery_scope = request.plan.source_publications[0].rowset.delivery_scope
    source = _identifier(source_row, campaign_field.namespace)
    target = canonical_campaign_key(
        CanonicalCampaignKeyRequest(
            commerce_scope=request.bridge.commerce_scope,
            platform=media.platform,
            channel=media.channel,
            source_system=delivery_scope.source_system,
            source_scope=delivery_scope.source_scope,
            identifier_kind=campaign_field.identifier_kind,
            source_value=source.value,
        )
    )
    row["campaign_key"] = target.value
    assigned_scope = {
        "commerce_scope_key": request.bridge.commerce_scope.value,
        "source_system_key": delivery_scope.source_system.value,
        "source_scope_namespace": delivery_scope.source_scope.namespace,
        "source_scope_value": delivery_scope.source_scope.value,
        "source_campaign_namespace": source.namespace,
        "source_campaign_identifier_kind": campaign_field.identifier_kind,
    }
    for column, value in assigned_scope.items():
        if column in request.contract.target_columns:
            row[column] = value
    evidence.add(
        IdentityMappingEvidence(
            source_namespace=source.namespace,
            source_value=source.value,
            target_value=target.value,
            mapping_version=request.pack.registry_version,
            valid_from=media.valid_from,
            valid_to=media.valid_to,
            source_system_key=delivery_scope.source_system.value,
            source_scope_namespace=delivery_scope.source_scope.namespace,
            source_scope_value=delivery_scope.source_scope.value,
            source_identifier_kind=campaign_field.identifier_kind,
        )
    )
    advertiser_field = request.pack.advertiser
    if advertiser_field is None:
        if "advertiser_key" in request.contract.target_columns:
            row["advertiser_key"] = None
        return
    advertiser_source = _identifier(source_row, advertiser_field.namespace)
    advertiser = canonical_advertiser_key(
        CanonicalAdvertiserKeyRequest(
            commerce_scope=request.bridge.commerce_scope,
            platform=media.platform,
            source_system=delivery_scope.source_system,
            source_scope=delivery_scope.source_scope,
            source=advertiser_source,
        )
    )
    row["advertiser_key"] = advertiser.value
    evidence.add(
        IdentityMappingEvidence(
            source_namespace=advertiser_source.namespace,
            source_value=advertiser_source.value,
            target_value=advertiser.value,
            mapping_version=request.pack.registry_version,
            valid_from=media.valid_from,
            valid_to=media.valid_to,
            source_system_key=delivery_scope.source_system.value,
            source_scope_namespace=delivery_scope.source_scope.namespace,
            source_scope_value=delivery_scope.source_scope.value,
        )
    )


def compile_canonical_rows(request: CanonicalCompileRequest) -> CanonicalCompileResult:
    """Replace source-local marketplace/product values with qualified canonical identities."""
    publication = request.plan.source_publications[0]
    marketplace_target = _target_for(request.contract, request.pack.marketplace.source_path)
    product_target = (
        None
        if request.pack.product is None
        else _target_for(request.contract, request.pack.product.source_path)
    )
    compiled: list[CanonicalRow] = []
    identity_evidence: set[IdentityMappingEvidence] = set()
    for ordinal, (source_row, mapped) in enumerate(
        zip(publication.rowset.rows, request.mapped_rows, strict=True)
    ):
        if request.included_ordinals is not None and ordinal not in request.included_ordinals:
            continue
        if source_row.measurement != request.plan.partition.bucket:
            continue
        marketplace = _identifier(source_row, request.pack.marketplace.namespace)
        product = (
            None
            if request.pack.product is None
            else _identifier(source_row, request.pack.product.namespace)
        )
        media_resolution: MediaTaxonomyResolution | None = None
        if request.pack.media is not None:
            if request.taxonomy is None:
                raise ValueError("media-bearing source pack requires the governed taxonomy.")
            media_source = _identifier(source_row, request.pack.media.namespace)
            media_resolution = request.taxonomy.resolve(
                media_source, source_row.measurement.window.start
            )
            identity_evidence.update(_media_evidence(media_resolution))
        resolution = resolve_commerce_identity(
            request.bridge,
            ResolveCommerceIdentityRequest(
                marketplace=marketplace,
                product=product,
                business_date=source_row.measurement.window.start,
            ),
        )
        identity_evidence.add(
            IdentityMappingEvidence(
                source_namespace=resolution.marketplace.source.namespace,
                source_value=resolution.marketplace.source.value,
                target_value=resolution.marketplace.target.value,
                mapping_version=resolution.marketplace.mapping_version,
                valid_from=resolution.marketplace.valid_from,
                valid_to=resolution.marketplace.valid_to,
            )
        )
        if resolution.product is not None:
            identity_evidence.add(
                IdentityMappingEvidence(
                    source_namespace=resolution.product.source.namespace,
                    source_value=resolution.product.source.value,
                    target_value=resolution.product.target.value,
                    mapping_version=resolution.product.mapping_version,
                    valid_from=resolution.product.valid_from,
                    valid_to=resolution.product.valid_to,
                )
            )
        conformed = dict(mapped)
        conformed[marketplace_target] = resolution.marketplace.target.value
        if media_resolution is not None:
            _apply_media(conformed, request.contract, media_resolution)
        _apply_campaign_identity(
            _CampaignAssignmentRequest(
                request=request,
                source_row=source_row,
                media=media_resolution,
                row=conformed,
                evidence=identity_evidence,
            )
        )
        if product_target is not None:
            if resolution.product is None:
                raise ValueError("product-bearing source row resolved no canonical product.")
            conformed[product_target] = resolution.product.target.value
        compiled.append(
            tuple(
                sorted(
                    (
                        (name, canonical_scalar(value, column=name))
                        for name, value in conformed.items()
                    ),
                    key=lambda item: item[0],
                )
            )
        )
    rows = tuple(compiled)
    return CanonicalCompileResult(
        rows=rows,
        content_checksum=canonical_rows_checksum(rows),
        identity_evidence=tuple(
            sorted(
                identity_evidence,
                key=lambda item: (
                    item.source_namespace,
                    item.source_value,
                    item.mapping_version,
                    item.target_value,
                ),
            )
        ),
    )
