# SPDX-License-Identifier: MIT-0
"""Authorized entity catalog over governed mappings and exact successful dimensions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from domain.data_contracts.contract import DataContract
from domain.data_contracts.generated_rows import (
    AmcCampaignDspRow,
    AmcCampaignSponsoredRow,
    VendorProductHierarchyRow,
)
from domain.semantics.analytical_resolution import (
    EffectiveLabel,
    EntityAlias,
    EntityKind,
    MappingEvidence,
    normalize_exact_text,
)
from domain.semantics.canonical_compiler import CanonicalRow
from domain.semantics.commerce_identity import (
    QUERY_PARAMETER_MARKETPLACE_NAMESPACES,
    CommerceIdentityBridge,
)
from domain.semantics.identity import (
    AdvertiserKey,
    CampaignKey,
    CommerceScopeKey,
    MarketplaceKey,
    ProductKey,
    SourceIdentifier,
    SourceSystemKey,
    canonical_brand_key,
)
from domain.semantics.media_taxonomy import (
    MediaTaxonomy,
    MediaTaxonomyKey,
    MediaTaxonomyResolution,
)
from domain.semantics.publication import SuccessfulPublication
from domain.semantics.source_pack import PackReadiness, SourcePack, SourcePackRole
from ports.outbound.canonical import CanonicalDatasetReadPort
from ports.outbound.entity_catalog import (
    AnalyticalEntityCatalogPort,
    EntityCatalogBrowseRequest,
    EntityCatalogBrowseResult,
    EntityCatalogLookupRequest,
    EntityCatalogLookupResult,
    PublicEntityLabelItem,
    PublicEntityLabelOutcome,
    PublicEntityLabelRequest,
    PublicEntityLabelResolution,
    PublicEntityLabelResult,
)
from ports.outbound.semantic_state import CurrentPublicationPort, ScopeHorizon

from .publication_selection import PublicationScopeRequest, publication_scopes_for

LATEST_ONLY = ScopeHorizon(newest=1)
"""The one publication a newest-wins reader reduces to (`publication_order`; A5.1 I9)."""

_SUPPORTED_DIMENSIONS = frozenset({"dim_campaign", "dim_product"})

_CatalogRequest = EntityCatalogLookupRequest | EntityCatalogBrowseRequest


@dataclass(frozen=True, slots=True)
class _ProjectionRequest:
    rows: tuple[CanonicalRow, ...]
    contract: DataContract
    publication: SuccessfulPublication
    lookup: _CatalogRequest
    bridge: CommerceIdentityBridge
    pack: SourcePack
    taxonomy: MediaTaxonomy


@dataclass(frozen=True, slots=True)
class _PublicProjection:
    request: PublicEntityLabelRequest
    aliases: tuple[EntityAlias, ...]
    bridge: CommerceIdentityBridge


@dataclass(frozen=True, slots=True)
class EntityCatalogTooling:
    """Dependencies and declarations required for one deterministic catalog."""

    current: CurrentPublicationPort
    canonical: CanonicalDatasetReadPort
    packs: tuple[SourcePack, ...]
    contracts: tuple[DataContract, ...]
    bridge: CommerceIdentityBridge
    taxonomy: MediaTaxonomy

    def __post_init__(self) -> None:
        contract_ids = tuple(contract.contract_id for contract in self.contracts)
        if len(set(contract_ids)) != len(contract_ids):
            raise ValueError("entity catalog contracts repeat a contract id.")
        available = set(contract_ids)
        selected = tuple(
            pack
            for pack in self.packs
            if pack.role is SourcePackRole.DIMENSION
            and pack.readiness is PackReadiness.READY
            and pack.dataset.value in _SUPPORTED_DIMENSIONS
        )
        if not selected or any(pack.contract_id not in available for pack in selected):
            raise ValueError("entity catalog requires contracts for every ready dimension pack.")


class CanonicalEntityCatalog(AnalyticalEntityCatalogPort):
    """Search authorized aliases without exposing SQL or reading unauthorized scopes."""

    def __init__(self, tooling: EntityCatalogTooling) -> None:
        self._tooling = tooling
        self._contracts = {contract.contract_id: contract for contract in tooling.contracts}

    def browse(self, request: EntityCatalogBrowseRequest) -> EntityCatalogBrowseResult:
        """Return bounded authorized entity histories effective for the requested view."""
        bridge = self._tooling.bridge
        if not request.scope.allows(bridge.commerce_scope):
            return EntityCatalogBrowseResult(())

        aliases = [*_marketplace_aliases(self._tooling, request)]
        aliases.extend(_taxonomy_aliases(self._tooling, request))
        aliases.extend(self._published_aliases(request))
        ordered = tuple(sorted(set(aliases), key=_alias_order))
        matching = tuple(alias for alias in ordered if _effective_for_browse(alias, request))
        matched_keys = tuple(
            sorted(
                {(alias.kind, alias.key) for alias in matching},
                key=lambda item: (item[0].value, item[1].value),
            )
        )
        selected_keys = frozenset(matched_keys[: request.max_candidates])
        selected = tuple(alias for alias in ordered if (alias.kind, alias.key) in selected_keys)
        return EntityCatalogBrowseResult(
            selected,
            truncated=len(matched_keys) > request.max_candidates,
            authorized_count=len(matched_keys),
        )

    def lookup(self, request: EntityCatalogLookupRequest) -> EntityCatalogLookupResult:
        """Return exact matching authorized alias histories in deterministic order."""
        bridge = self._tooling.bridge
        if not request.scope.allows(bridge.commerce_scope):
            return EntityCatalogLookupResult(
                (),
                hidden_direct_match=_hidden_direct_reference(request, self._tooling),
            )

        aliases = [*_marketplace_aliases(self._tooling, request)]
        aliases.extend(_taxonomy_aliases(self._tooling, request))
        aliases.extend(self._published_aliases(request))
        ordered = tuple(sorted(set(aliases), key=_alias_order))
        matching = tuple(
            alias
            for alias in ordered
            if _matches_reference(alias, request) and _effective_for_lookup(alias, request)
        )
        matched_keys = tuple(
            sorted(
                {(alias.kind, alias.key) for alias in matching},
                key=lambda item: (item[0].value, item[1].value),
            )
        )
        selected_keys = frozenset(matched_keys[: request.max_candidates])
        selected = tuple(alias for alias in ordered if (alias.kind, alias.key) in selected_keys)
        return EntityCatalogLookupResult(
            selected,
            hidden_direct_match=(not selected and _hidden_direct_reference(request, self._tooling)),
            truncated=len(matched_keys) > request.max_candidates,
        )

    def public_labels(self, request: PublicEntityLabelRequest) -> PublicEntityLabelResult:
        """Project exact protected tuples to positional governed public labels."""
        bridge = self._tooling.bridge
        if not request.scope.allows(bridge.commerce_scope):
            return PublicEntityLabelResult(
                tuple(
                    PublicEntityLabelResolution(PublicEntityLabelOutcome.UNAUTHORIZED_OR_HIDDEN)
                    for _ in request.items
                )
            )
        kinds = frozenset(
            _PUBLIC_DIMENSION_KINDS[name]
            for item in request.items
            for name, _ in item.dimensions
            if name != "marketplace_key"
        )
        projection_request = EntityCatalogBrowseRequest(
            scope=request.scope,
            kinds=kinds,
            window=request.window,
            current_on=None,
            context=request.context,
            max_candidates=25,
        )
        aliases = [*_taxonomy_aliases(self._tooling, projection_request)]
        aliases.extend(self._published_aliases(projection_request))
        projection = _PublicProjection(request, tuple(aliases), bridge)
        resolutions = _channel_qualified(
            projection,
            self._channel_projection(request),
            tuple(_public_item_resolution(item, projection) for item in request.items),
        )
        duplicate_labels = {
            resolution.display_label
            for resolution in resolutions
            if resolution.display_label is not None
            and sum(
                candidate.display_label == resolution.display_label for candidate in resolutions
            )
            > 1
        }
        return PublicEntityLabelResult(
            tuple(
                PublicEntityLabelResolution(PublicEntityLabelOutcome.AMBIGUOUS)
                if resolution.display_label in duplicate_labels
                else resolution
                for resolution in resolutions
            )
        )

    def _channel_projection(self, request: PublicEntityLabelRequest) -> _PublicProjection:
        """W14: a second projection over the channel column only (ADR-067 item 3)."""
        channel_request = EntityCatalogBrowseRequest(
            scope=request.scope,
            kinds=frozenset({EntityKind.CHANNEL}),
            window=request.window,
            current_on=None,
            context=request.context,
            max_candidates=25,
        )
        return _PublicProjection(
            request,
            _taxonomy_aliases(self._tooling, channel_request),
            self._tooling.bridge,
        )

    def _published_aliases(self, request: _CatalogRequest) -> tuple[EntityAlias, ...]:
        found: list[EntityAlias] = []
        for pack in self._tooling.packs:
            if not _pack_requested(pack, request):
                continue
            contract = self._contracts[pack.contract_id]
            scopes = publication_scopes_for(
                PublicationScopeRequest(
                    pack=pack,
                    contract=contract,
                    bridge=self._tooling.bridge,
                    filters={},
                )
            )
            for scope in scopes:
                if not request.scope.allows(scope.commerce_scope):
                    continue
                current = self._tooling.current.current_for_scope(scope, horizon=LATEST_ONLY)
                if not current:
                    continue
                publication = max(current, key=publication_order)
                rows = self._tooling.canonical.rows(publication)
                projection = _ProjectionRequest(
                    rows,
                    contract,
                    publication,
                    request,
                    self._tooling.bridge,
                    pack,
                    self._tooling.taxonomy,
                )
                if pack.dataset.value == "dim_campaign":
                    found.extend(_campaign_aliases(projection))
                elif pack.dataset.value == "dim_product":
                    found.extend(_product_aliases(projection))
        return tuple(found)


_PUBLIC_DIMENSION_KINDS = {
    "campaign_key": EntityKind.CAMPAIGN,
    "amazon_asin": EntityKind.PRODUCT,
    "platform_key": EntityKind.PLATFORM,
    "ecosystem_key": EntityKind.ECOSYSTEM,
    "channel_group_key": EntityKind.CHANNEL_GROUP,
    "channel_key": EntityKind.CHANNEL,
    "ad_product_key": EntityKind.AD_PRODUCT,
}


def _public_item_resolution(
    item: PublicEntityLabelItem,
    projection: _PublicProjection,
) -> PublicEntityLabelResolution:
    labels: list[str] = []
    for dimension, value in item.dimensions:
        if dimension == "marketplace_key":
            resolution = _public_marketplace_label(value, projection.request, projection.bridge)
        else:
            resolution = _public_alias_label(dimension, value, projection)
        if resolution.outcome is not PublicEntityLabelOutcome.RESOLVED:
            return resolution
        if resolution.display_label is None:  # pragma: no cover - DTO invariant
            raise AssertionError("resolved public label has no display text.")
        labels.append(resolution.display_label)
    return PublicEntityLabelResolution(
        PublicEntityLabelOutcome.RESOLVED,
        " · ".join(labels),
    )


def _channel_qualified(
    projection: _PublicProjection,
    channel_projection: _PublicProjection,
    resolutions: tuple[PublicEntityLabelResolution, ...],
) -> tuple[PublicEntityLabelResolution, ...]:
    """W14 (ADR-067 amendment): qualify a repeated campaign label with its channel's label.

    Only a single-dimension campaign item whose resolved label repeats in the batch is
    qualified, as "<campaign label> (<channel label>)". The channel comes from the campaign
    alias and its label from the governed taxonomy, never from the canonical key. A repeated
    campaign whose channel does not resolve to one public label becomes ambiguous; a
    qualified label that still repeats is downgraded by the caller's unchanged rule.
    """
    repeated = {
        resolution.display_label
        for resolution in resolutions
        if resolution.display_label is not None
        and sum(other.display_label == resolution.display_label for other in resolutions) > 1
    }
    qualified: list[PublicEntityLabelResolution] = []
    for item, resolution in zip(projection.request.items, resolutions, strict=True):
        if (
            resolution.display_label not in repeated
            or len(item.dimensions) != 1
            or item.dimensions[0][0] != "campaign_key"
        ):
            qualified.append(resolution)
            continue
        channel_label = _campaign_channel_label(
            item.dimensions[0][1], projection, channel_projection
        )
        qualified.append(
            PublicEntityLabelResolution(PublicEntityLabelOutcome.AMBIGUOUS)
            if channel_label is None
            else PublicEntityLabelResolution(
                PublicEntityLabelOutcome.RESOLVED,
                f"{resolution.display_label} ({channel_label})",
            )
        )
    return tuple(qualified)


def _campaign_channel_label(
    campaign_key: str,
    projection: _PublicProjection,
    channel_projection: _PublicProjection,
) -> str | None:
    """Return a campaign's one public channel label at the window end, else None."""
    request = projection.request
    channels = {
        alias.channel
        for alias in projection.aliases
        if alias.kind is EntityKind.CAMPAIGN
        and alias.key.value == campaign_key
        and alias.valid_from <= request.window.end
        and (alias.valid_to is None or request.window.end < alias.valid_to)
        and request.context.matches(alias)
    }
    if len(channels) != 1:
        return None
    channel = channels.pop()
    if channel is None:
        return None
    resolution = _public_alias_label("channel_key", channel.value, channel_projection)
    label = resolution.display_label
    # The public-text rule: a resolved, nonblank label that is not the canonical key itself.
    if (
        resolution.outcome is not PublicEntityLabelOutcome.RESOLVED
        or label is None
        or not label.strip()
        or label == channel.value
    ):
        return None
    return label


def _public_marketplace_label(
    value: str,
    request: PublicEntityLabelRequest,
    bridge: CommerceIdentityBridge,
) -> PublicEntityLabelResolution:
    matching_mappings = tuple(
        mapping
        for mapping in bridge.marketplace_mappings
        if mapping.target.value == value
        and mapping.valid_from <= request.window.end
        and (mapping.valid_to is None or request.window.end < mapping.valid_to)
    )
    if not matching_mappings:
        return PublicEntityLabelResolution(PublicEntityLabelOutcome.NOT_FOUND)
    if request.context.marketplace is not None and request.context.marketplace.value != value:
        return PublicEntityLabelResolution(PublicEntityLabelOutcome.INVALID_CONTEXT)
    labels = {
        display.value
        for display in bridge.marketplace_display_names
        if display.marketplace.value == value
    }
    if len(labels) != 1:
        return PublicEntityLabelResolution(
            PublicEntityLabelOutcome.AMBIGUOUS if labels else PublicEntityLabelOutcome.NOT_FOUND
        )
    return PublicEntityLabelResolution(PublicEntityLabelOutcome.RESOLVED, labels.pop())


def _public_alias_label(
    dimension: str,
    value: str,
    projection: _PublicProjection,
) -> PublicEntityLabelResolution:
    request = projection.request
    # A campaign is matched by its exact canonical key, which is unique within the authorized
    # commerce scope, so no platform differentiator is required: an impossible cross-platform
    # collision would surface as AMBIGUOUS rather than as a wrong label. An ASIN's label is a
    # per-marketplace title for the same key value, so its marketplace context stays mandatory.
    if dimension == "amazon_asin" and request.context.marketplace is None:
        return PublicEntityLabelResolution(PublicEntityLabelOutcome.INVALID_CONTEXT)
    kind = _PUBLIC_DIMENSION_KINDS[dimension]
    exact = tuple(
        alias
        for alias in projection.aliases
        if alias.kind is kind
        and _public_key_matches(dimension, value, alias)
        and alias.valid_from <= request.window.end
        and (alias.valid_to is None or request.window.end < alias.valid_to)
    )
    contextual = tuple(alias for alias in exact if request.context.matches(alias))
    if exact and not contextual:
        return PublicEntityLabelResolution(PublicEntityLabelOutcome.INVALID_CONTEXT)
    labels = {
        label.value
        for alias in contextual
        for label in alias.labels
        if label.contains(request.window.end) and _is_public_label(dimension, label, alias)
    }
    if len(labels) == 1:
        return PublicEntityLabelResolution(PublicEntityLabelOutcome.RESOLVED, labels.pop())
    outcome = (
        PublicEntityLabelOutcome.AMBIGUOUS
        if len(labels) > 1
        else PublicEntityLabelOutcome.NOT_FOUND
    )
    return PublicEntityLabelResolution(outcome)


def _public_key_matches(dimension: str, value: str, alias: EntityAlias) -> bool:
    if dimension != "amazon_asin":
        return alias.key.value == value
    canonical_value = alias.key.value.removeprefix("amazon_asin:")
    return value.removeprefix("amazon_asin:") == canonical_value


def _is_public_label(
    dimension: str,
    label: EffectiveLabel,
    alias: EntityAlias,
) -> bool:
    if dimension != "amazon_asin":
        return True
    protected_values = {
        alias.key.value,
        alias.key.value.removeprefix("amazon_asin:"),
        *(source.value for source in alias.source_identifiers),
    }
    return label.value not in protected_values


def _pack_requested(pack: SourcePack, request: _CatalogRequest) -> bool:
    if (
        pack.role is not SourcePackRole.DIMENSION
        or pack.readiness is not PackReadiness.READY
        or pack.dataset.value not in _SUPPORTED_DIMENSIONS
    ):
        return False
    supported = (
        {EntityKind.CAMPAIGN, EntityKind.ADVERTISER}
        if pack.dataset.value == "dim_campaign"
        else {EntityKind.PRODUCT, EntityKind.BRAND}
    )
    return not request.kinds or bool(request.kinds & supported)


def _campaign_aliases(projection: _ProjectionRequest) -> tuple[EntityAlias, ...]:
    contract = projection.contract
    request = projection.lookup
    model = (
        AmcCampaignDspRow if contract.contract_id == "amc-campaign-dsp" else AmcCampaignSponsoredRow
    )
    found: list[EntityAlias] = []
    for raw in projection.rows:
        row = model.model_validate(dict(raw))
        if row.rows_suppressed:
            raise ValueError("successful campaign dimension contains a suppressed row.")
        valid_from, valid_to = _validity(row.valid_from, row.valid_to, row.is_current)
        commerce_scope = CommerceScopeKey(row.commerce_scope_key)
        if commerce_scope != projection.bridge.commerce_scope or not request.scope.allows(
            commerce_scope
        ):
            raise ValueError("campaign dimension row contradicts its authorized publication scope.")
        marketplace = MarketplaceKey(row.marketplace_key)
        source_system = SourceSystemKey(row.source_system_key)
        source_scope = SourceIdentifier(
            row.source_scope_namespace,
            row.source_scope_value,
        )
        media = _media_resolution(
            projection,
            (row.source_ad_product_type if isinstance(row, AmcCampaignSponsoredRow) else None),
            row.source_observed_on,
        )
        observed_media = (
            row.ecosystem_key,
            row.platform_key,
            row.channel_group_key,
            row.channel_key,
            row.ad_product_key,
        )
        resolved_media = (
            media.ecosystem.value,
            media.platform.value,
            None if media.channel_group is None else media.channel_group.value,
            None if media.channel is None else media.channel.value,
            None if media.ad_product is None else media.ad_product.value,
        )
        if observed_media != resolved_media:
            raise ValueError("campaign dimension media axes contradict governed taxonomy.")
        platform = media.platform
        evidence = _publication_evidence(contract, projection.publication)
        if not request.kinds or EntityKind.CAMPAIGN in request.kinds:
            labels = (
                ()
                if not row.campaign_name
                else (EffectiveLabel(row.campaign_name, valid_from, valid_to),)
            )
            found.append(
                EntityAlias(
                    kind=EntityKind.CAMPAIGN,
                    key=CampaignKey(row.campaign_key, commerce_scope, platform),
                    commerce_scope=commerce_scope,
                    labels=labels,
                    source_identifiers=(
                        SourceIdentifier(
                            row.source_campaign_namespace,
                            row.source_campaign_id,
                        ),
                    ),
                    evidence=evidence,
                    valid_from=valid_from,
                    valid_to=valid_to,
                    source_system=source_system,
                    source_scope=source_scope,
                    ecosystem=media.ecosystem,
                    platform=platform,
                    channel_group=media.channel_group,
                    channel=media.channel,
                    ad_product=media.ad_product,
                    marketplace=marketplace,
                )
            )
        if (
            not request.kinds or EntityKind.ADVERTISER in request.kinds
        ) and row.advertiser_key is not None:
            source_id = (
                (SourceIdentifier("amc.dsp.advertiser_id", row.source_advertiser_id),)
                if isinstance(row, AmcCampaignDspRow)
                else ()
            )
            labels = (
                ()
                if not row.advertiser_name
                else (EffectiveLabel(row.advertiser_name, valid_from, valid_to),)
            )
            found.append(
                EntityAlias(
                    kind=EntityKind.ADVERTISER,
                    key=AdvertiserKey(row.advertiser_key, commerce_scope, platform),
                    commerce_scope=commerce_scope,
                    labels=labels,
                    source_identifiers=source_id,
                    evidence=evidence,
                    valid_from=valid_from,
                    valid_to=valid_to,
                    source_system=source_system,
                    source_scope=source_scope,
                    ecosystem=media.ecosystem,
                    platform=platform,
                    channel_group=media.channel_group,
                    channel=media.channel,
                    ad_product=media.ad_product,
                    marketplace=marketplace,
                )
            )
    return tuple(found)


def _product_aliases(projection: _ProjectionRequest) -> tuple[EntityAlias, ...]:
    contract = projection.contract
    request = projection.lookup
    bridge = projection.bridge
    found: list[EntityAlias] = []
    evidence = _publication_evidence(contract, projection.publication)
    for raw in projection.rows:
        row = VendorProductHierarchyRow.model_validate(dict(raw))
        valid_from, valid_to = _validity(row.valid_from, row.valid_to, row.is_current)
        media = _media_resolution(projection, None, valid_from)
        marketplace = MarketplaceKey(row.marketplace_key)
        asin = _canonical_identifier_value(row.amazon_asin, "amazon_asin")
        if not request.kinds or EntityKind.PRODUCT in request.kinds:
            labels = [EffectiveLabel(asin, valid_from, valid_to)]
            if row.product_title:
                labels.append(EffectiveLabel(row.product_title, valid_from, valid_to))
            found.append(
                EntityAlias(
                    kind=EntityKind.PRODUCT,
                    key=ProductKey(row.amazon_asin, bridge.commerce_scope, marketplace),
                    commerce_scope=bridge.commerce_scope,
                    labels=tuple(labels),
                    source_identifiers=(SourceIdentifier("amazon_asin", asin),),
                    evidence=evidence,
                    valid_from=valid_from,
                    valid_to=valid_to,
                    ecosystem=media.ecosystem,
                    platform=media.platform,
                    marketplace=marketplace,
                )
            )
        if (not request.kinds or EntityKind.BRAND in request.kinds) and row.brand_code is not None:
            source = SourceIdentifier("vendor_data_kiosk.brand_code", row.brand_code)
            labels = () if not row.brand else (EffectiveLabel(row.brand, valid_from, valid_to),)
            found.append(
                EntityAlias(
                    kind=EntityKind.BRAND,
                    key=canonical_brand_key(bridge.commerce_scope, source),
                    commerce_scope=bridge.commerce_scope,
                    labels=labels,
                    source_identifiers=(source,),
                    evidence=evidence,
                    valid_from=valid_from,
                    valid_to=valid_to,
                    ecosystem=media.ecosystem,
                    platform=media.platform,
                    marketplace=marketplace,
                )
            )
    return tuple(found)


def _marketplace_aliases(
    tooling: EntityCatalogTooling, request: _CatalogRequest
) -> tuple[EntityAlias, ...]:
    if request.kinds and EntityKind.MARKETPLACE not in request.kinds:
        return ()
    bridge = tooling.bridge
    return tuple(
        EntityAlias(
            kind=EntityKind.MARKETPLACE,
            key=mapping.target,
            commerce_scope=bridge.commerce_scope,
            labels=(
                *(
                    (EffectiveLabel(mapping.source.value, mapping.valid_from, mapping.valid_to),)
                    if mapping.source.namespace not in QUERY_PARAMETER_MARKETPLACE_NAMESPACES
                    else ()
                ),
                *(
                    EffectiveLabel(display.value, mapping.valid_from, mapping.valid_to)
                    for display in bridge.marketplace_display_names
                    if display.marketplace == mapping.target
                ),
            ),
            source_identifiers=(mapping.source,),
            evidence=MappingEvidence(
                "contracts/shared/commerce-identities.yaml",
                mapping.mapping_version,
            ),
            valid_from=mapping.valid_from,
            valid_to=mapping.valid_to,
            marketplace=mapping.target,
        )
        for mapping in bridge.marketplace_mappings
    )


def _taxonomy_aliases(
    tooling: EntityCatalogTooling, request: _CatalogRequest
) -> tuple[EntityAlias, ...]:
    found: list[EntityAlias] = []
    for mapping in tooling.taxonomy.mappings:
        value = mapping.resolution
        for kind, key in _resolution_axes(value):
            if request.kinds and kind not in request.kinds:
                continue
            term = tooling.taxonomy.term_for(key)
            found.append(
                EntityAlias(
                    kind=kind,
                    key=key,
                    commerce_scope=tooling.bridge.commerce_scope,
                    labels=(EffectiveLabel(term.name, value.valid_from, value.valid_to),),
                    source_identifiers=(value.source,),
                    evidence=MappingEvidence(value.evidence, value.mapping_version),
                    valid_from=value.valid_from,
                    valid_to=value.valid_to,
                    ecosystem=value.ecosystem,
                    platform=value.platform,
                    channel_group=value.channel_group,
                    channel=value.channel,
                    ad_product=value.ad_product,
                )
            )
    return tuple(found)


def _resolution_axes(
    value: MediaTaxonomyResolution,
) -> tuple[tuple[EntityKind, MediaTaxonomyKey], ...]:
    axes: list[tuple[EntityKind, MediaTaxonomyKey]] = [
        (EntityKind.ECOSYSTEM, value.ecosystem),
        (EntityKind.PLATFORM, value.platform),
    ]
    optional = (
        (EntityKind.CHANNEL_GROUP, value.channel_group),
        (EntityKind.CHANNEL, value.channel),
        (EntityKind.AD_PRODUCT, value.ad_product),
    )
    axes.extend((kind, key) for kind, key in optional if key is not None)
    return tuple(axes)


def _media_resolution(
    projection: _ProjectionRequest,
    source_value: str | None,
    on: date,
) -> MediaTaxonomyResolution:
    field = projection.pack.media
    if field is None:
        raise ValueError("entity dimension pack has no governed media identity.")
    value = field.literal_value if field.literal_value is not None else source_value
    if value is None:
        raise ValueError("entity dimension row lacks its governed media source value.")
    return projection.taxonomy.resolve(SourceIdentifier(field.namespace, value), on)


def _validity(
    valid_from: datetime, valid_to: datetime, is_current: bool
) -> tuple[date, date | None]:
    if valid_from.tzinfo is None or valid_to.tzinfo is None:
        raise ValueError("dimension validity timestamps must be timezone-aware.")
    start = valid_from.date()
    end = None if is_current else valid_to.date()
    if end is not None and end <= start:
        raise ValueError("dimension validity interval must be increasing.")
    return start, end


def _publication_evidence(
    contract: DataContract, publication: SuccessfulPublication
) -> MappingEvidence:
    return MappingEvidence(
        f"contract:{contract.contract_id};publication:{publication.publication_id.value}",
        contract.contract_version,
    )


def _canonical_identifier_value(value: str, namespace: str) -> str:
    prefix = f"{namespace}:"
    if not value.startswith(prefix) or len(value) == len(prefix):
        raise ValueError(f"canonical identifier is outside {namespace!r}.")
    return value.removeprefix(prefix)


def _effective_for_lookup(
    alias: EntityAlias,
    request: EntityCatalogLookupRequest,
) -> bool:
    if not request.context.matches(alias):
        return False
    if request.window is not None:
        return alias.valid_from <= request.window.end and (
            alias.valid_to is None or request.window.start < alias.valid_to
        )
    current_on = request.current_on
    return (
        current_on is not None
        and alias.valid_from <= current_on
        and (alias.valid_to is None or current_on < alias.valid_to)
    )


def _effective_for_browse(
    alias: EntityAlias,
    request: EntityCatalogBrowseRequest,
) -> bool:
    if request.kinds and alias.kind not in request.kinds:
        return False
    if not request.context.matches(alias):
        return False
    if request.window is not None:
        return alias.valid_from <= request.window.end and (
            alias.valid_to is None or request.window.start < alias.valid_to
        )
    current_on = request.current_on
    return (
        current_on is not None
        and alias.valid_from <= current_on
        and (alias.valid_to is None or current_on < alias.valid_to)
    )


def _matches_reference(alias: EntityAlias, request: EntityCatalogLookupRequest) -> bool:
    reference = request.reference
    if reference.text is not None:
        expected = normalize_exact_text(reference.text)
        return any(label.normalized == expected for label in alias.labels)
    if reference.canonical_key is not None:
        return alias.key == reference.canonical_key
    if reference.source_identifier not in alias.source_identifiers:
        return False
    if alias.source_system is None:
        return reference.source_system is None and reference.source_scope is None
    return (
        reference.source_system == alias.source_system
        and reference.source_scope == alias.source_scope
    )


def _hidden_direct_reference(
    request: EntityCatalogLookupRequest, tooling: EntityCatalogTooling
) -> bool:
    reference = request.reference
    if reference.text is not None:
        return False
    key = reference.canonical_key
    if key is not None:
        qualified = getattr(key, "commerce_scope", tooling.bridge.commerce_scope)
        return not request.scope.allows(qualified)
    source = reference.source_identifier
    if source is None or request.scope.allows(tooling.bridge.commerce_scope):
        return False
    return source in {
        *(mapping.source for mapping in tooling.bridge.marketplace_mappings),
        *(mapping.resolution.source for mapping in tooling.taxonomy.mappings),
    }


def publication_order(
    publication: SuccessfulPublication,
) -> tuple[date, datetime, str]:
    """Order CURRENT partitions as the catalog serves them: latest bucket end, then newest."""
    return (
        publication.partition.bucket.window.end,
        publication.published_at,
        publication.publication_id.value,
    )


def _alias_order(alias: EntityAlias) -> tuple[object, ...]:
    return (
        alias.kind.value,
        alias.key.value,
        alias.valid_from,
        date.max if alias.valid_to is None else alias.valid_to,
        tuple((item.namespace, item.value) for item in alias.source_identifiers),
        tuple(label.value for label in alias.labels),
    )
