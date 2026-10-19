# SPDX-License-Identifier: MIT-0
"""Authorized catalog projection from governed mappings and successful dimensions."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest

from adapters.inbound.tool_handlers.dimension_labels import DimensionLabelResolver
from adapters.jobs.contract_validation.loader import load_contracts
from adapters.outbound.commerce_identity import load_commerce_identity_bridge
from adapters.outbound.entity_catalog import CanonicalEntityCatalog, EntityCatalogTooling
from adapters.outbound.media_taxonomy import load_media_taxonomy
from adapters.outbound.publication_selection import (
    PublicationScopeRequest,
    publication_scopes_for,
)
from adapters.outbound.source_pack import load_source_packs
from domain.data_contracts.contract import DataContract
from domain.semantics.analytical_resolution import (
    AuthorizedAnalyticalScope,
    EntityAlias,
    EntityContext,
    EntityKind,
    EntityReference,
    FieldOrigin,
    ResolutionOutcome,
    ResolveEntityRequest,
    resolve_entity,
)
from domain.semantics.canonical_compiler import CanonicalRow, canonical_scalar
from domain.semantics.commerce_identity import CommerceIdentityBridge
from domain.semantics.identity import (
    CampaignKey,
    ChannelKey,
    CommerceScopeKey,
    MarketplaceKey,
    PlatformKey,
    SourceIdentifier,
    SourceSystemKey,
)
from domain.semantics.media_taxonomy import MediaTaxonomy
from domain.semantics.publication import (
    AttemptId,
    CanonicalPublisherKey,
    PartitionIdentity,
    PublicationId,
    PublicationScope,
    SourcePublicationRef,
    SuccessfulPublication,
)
from domain.semantics.revision import RevisionSchemeKey, SourceRevision
from domain.semantics.source_pack import SourcePack
from domain.semantics.time import MeasurementBucket, TimeGrain
from domain.semantics.validation import LegacyPublicationRules, PublicationEvidence
from domain.shared_kernel.values import DateRange
from ports.outbound.entity_catalog import (
    EntityCatalogBrowseRequest,
    EntityCatalogLookupRequest,
    PublicEntityLabelItem,
    PublicEntityLabelOutcome,
    PublicEntityLabelRequest,
    PublicEntityLabelResolution,
    PublicEntityLabelResult,
)

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-052", "ADR-053", "ADR-054")]

ROOT = Path(__file__).resolve().parents[2]
SCOPE = CommerceScopeKey("amazon_retail_us")
OTHER_SCOPE = CommerceScopeKey("other_scope")
PLATFORM = PlatformKey("amazon_ads")
SOURCE_SYSTEM = SourceSystemKey("amc")
SOURCE_SCOPE = SourceIdentifier("amc.entity", "fictional-fixture-amc-entity")
AUTHORIZED = AuthorizedAnalyticalScope(frozenset({SCOPE}))
WINDOW = DateRange(date(2026, 8, 10), date(2026, 8, 14))
OPEN_END = datetime(2999, 12, 31, tzinfo=UTC)
PUBLISHED_AT = datetime(2026, 8, 24, 12, tzinfo=UTC)


@dataclass
class MemoryCurrent:
    """Read-only CURRENT implementation recording scope reads."""

    by_scope: dict[str, tuple[SuccessfulPublication, ...]] = field(
        default_factory=dict[str, tuple[SuccessfulPublication, ...]]
    )
    scope_calls: list[PublicationScope] = field(default_factory=list[PublicationScope])

    def current(self, partition: PartitionIdentity) -> SuccessfulPublication | None:
        return next(
            (
                item
                for item in self.by_scope.get(partition.scope.identity, ())
                if item.partition == partition
            ),
            None,
        )

    def current_exact(
        self, partitions: tuple[PartitionIdentity, ...]
    ) -> tuple[SuccessfulPublication, ...] | None:
        found = tuple(self.current(partition) for partition in partitions)
        if any(item is None for item in found):
            return None
        return tuple(item for item in found if item is not None)

    def current_for_scope(
        self, scope: PublicationScope, horizon: object = None
    ) -> tuple[SuccessfulPublication, ...]:
        del horizon  # every publication: the reader still reduces to the newest
        self.scope_calls.append(scope)
        return self.by_scope.get(scope.identity, ())


@dataclass
class MemoryCanonical:
    """Exact immutable successful-row implementation recording publication reads."""

    rows_by_publication: dict[str, tuple[CanonicalRow, ...]] = field(
        default_factory=dict[str, tuple[CanonicalRow, ...]]
    )
    calls: list[SuccessfulPublication] = field(default_factory=list[SuccessfulPublication])

    def rows(self, publication: SuccessfulPublication) -> tuple[CanonicalRow, ...]:
        self.calls.append(publication)
        return self.rows_by_publication[publication.publication_id.value]


@dataclass(frozen=True, slots=True)
class Environment:
    contracts: tuple[DataContract, ...]
    by_contract: dict[str, DataContract]
    packs: tuple[SourcePack, ...]
    by_pack: dict[str, SourcePack]
    bridge: CommerceIdentityBridge


@dataclass(frozen=True, slots=True)
class _PublishRequest:
    contract_id: str
    rows: tuple[dict[str, object], ...]
    token: str
    on: date = date(2026, 8, 14)


@dataclass(frozen=True, slots=True)
class _DspOptions:
    source_id: str
    valid_from: datetime = datetime(2026, 1, 1, tzinfo=UTC)
    valid_to: datetime = OPEN_END
    is_current: bool = True


@dataclass(frozen=True, slots=True)
class _LookupOptions:
    kinds: frozenset[EntityKind] = frozenset({EntityKind.CAMPAIGN})
    scope: AuthorizedAnalyticalScope = AUTHORIZED
    max_candidates: int = 25
    window: DateRange | None = WINDOW
    current_on: date | None = None


@dataclass
class Harness:
    environment: Environment
    current: MemoryCurrent
    canonical: MemoryCanonical
    catalog: CanonicalEntityCatalog

    def publish(self, request: _PublishRequest) -> SuccessfulPublication:
        scope = _scope(self.environment, request.contract_id)
        publication = _success(scope, request.token, request.on)
        self.current.by_scope[scope.identity] = (
            *self.current.by_scope.get(scope.identity, ()),
            publication,
        )
        self.canonical.rows_by_publication[publication.publication_id.value] = tuple(
            tuple(
                sorted(
                    ((name, canonical_scalar(value, column=name)) for name, value in row.items())
                )
            )
            for row in request.rows
        )
        return publication


@pytest.fixture(scope="module")
def environment() -> Environment:
    contracts = tuple(contract for _, contract in load_contracts(ROOT))
    packs = load_source_packs(ROOT)
    return Environment(
        contracts,
        {contract.contract_id: contract for contract in contracts},
        packs,
        {pack.contract_id: pack for pack in packs},
        load_commerce_identity_bridge(ROOT),
    )


def _harness(environment: Environment, taxonomy: MediaTaxonomy | None = None) -> Harness:
    current = MemoryCurrent()
    canonical = MemoryCanonical()
    catalog = CanonicalEntityCatalog(
        EntityCatalogTooling(
            current=current,
            canonical=canonical,
            packs=environment.packs,
            contracts=environment.contracts,
            bridge=environment.bridge,
            taxonomy=taxonomy or load_media_taxonomy(ROOT),
        )
    )
    return Harness(environment, current, canonical, catalog)


def _scope(environment: Environment, contract_id: str) -> PublicationScope:
    return publication_scopes_for(
        PublicationScopeRequest(
            pack=environment.by_pack[contract_id],
            contract=environment.by_contract[contract_id],
            bridge=environment.bridge,
            filters={},
        )
    )[0]


def _success(scope: PublicationScope, token: str, on: date) -> SuccessfulPublication:
    partition = PartitionIdentity(
        scope,
        MeasurementBucket(
            DateRange(on, on),
            TimeGrain.DAY,
            scope.business_calendar,
            scope.source_timezone,
            scope.observation_basis,
        ),
    )
    source = SourcePublicationRef(f"source-{token}")
    return SuccessfulPublication(
        publication_id=PublicationId(f"publication-{token}"),
        publisher=CanonicalPublisherKey("entity-catalog-unit"),
        partition=partition,
        attempt_id=AttemptId(f"attempt-{token}"),
        source_inputs=(source,),
        source_revision=SourceRevision.instant(
            scheme=RevisionSchemeKey("unit.entity_catalog"),
            source_id=source.value,
            observed_at=PUBLISHED_AT,
        ),
        content_checksum=f"sha256:{token}",
        evidence=PublicationEvidence(
            "entity-catalog-unit",
            legacy=LegacyPublicationRules(passed_rules=("historical_success",)),
        ),
        fencing_token=1,
        published_at=PUBLISHED_AT,
    )


def _dsp_row(
    key: str,
    name: str | None,
    options: _DspOptions,
) -> dict[str, object]:
    return {
        "source_observed_on": date(2026, 8, 14),
        "source_campaign_id": options.source_id,
        "campaign_name": name,
        "source_advertiser_id": "910000000000001",
        "advertiser_name": "Northstar Home Goods Media",
        "marketplace_key": "amazon_us",
        "source_campaign_namespace": "amc.dsp.campaign_id_string",
        "source_campaign_identifier_kind": "campaign_id_string",
        "campaign_key": key,
        "advertiser_key": "advertiser:000000000000000000000001",
        "commerce_scope_key": SCOPE.value,
        "source_system_key": SOURCE_SYSTEM.value,
        "source_scope_namespace": SOURCE_SCOPE.namespace,
        "source_scope_value": SOURCE_SCOPE.value,
        "ecosystem_key": "amazon",
        "platform_key": "amazon_ads",
        "channel_group_key": "display",
        "channel_key": "amazon_dsp",
        "ad_product_key": "amazon_dsp",
        "valid_from": options.valid_from,
        "valid_to": options.valid_to,
        "is_current": options.is_current,
        "rows_suppressed": False,
    }


def _sponsored_row(key: str, name: str, source_id: str) -> dict[str, object]:
    return {
        "source_observed_on": date(2026, 8, 14),
        "source_campaign_id": source_id,
        "campaign_name": name,
        "advertiser_name": "Northstar Home Goods Media",
        "marketplace_key": "amazon_us",
        "source_ad_product_type": "sponsored_brands",
        "source_campaign_namespace": "amc.sponsored.campaign_id_string",
        "source_campaign_identifier_kind": "campaign_id_string",
        "campaign_key": key,
        "advertiser_key": None,
        "commerce_scope_key": SCOPE.value,
        "source_system_key": SOURCE_SYSTEM.value,
        "source_scope_namespace": SOURCE_SCOPE.namespace,
        "source_scope_value": SOURCE_SCOPE.value,
        "ecosystem_key": "amazon",
        "platform_key": "amazon_ads",
        "channel_group_key": "search",
        "channel_key": "amazon_sponsored_brands",
        "ad_product_key": "sponsored_brands",
        "valid_from": datetime(2026, 1, 1, tzinfo=UTC),
        "valid_to": OPEN_END,
        "is_current": True,
        "rows_suppressed": False,
    }


def _lookup(
    reference: EntityReference,
    options: _LookupOptions | None = None,
) -> EntityCatalogLookupRequest:
    selected = options or _LookupOptions()
    return EntityCatalogLookupRequest(
        scope=selected.scope,
        reference=reference,
        kinds=selected.kinds,
        window=selected.window,
        current_on=selected.current_on,
        context=EntityContext(),
        max_candidates=selected.max_candidates,
    )


def _public_labels(
    *dimensions: tuple[tuple[str, str], ...],
    scope: AuthorizedAnalyticalScope = AUTHORIZED,
    context: EntityContext | None = None,
) -> PublicEntityLabelRequest:
    return PublicEntityLabelRequest(
        scope=scope,
        items=tuple(PublicEntityLabelItem(item) for item in dimensions),
        context=context or EntityContext(),
        window=WINDOW,
    )


def _resolve(result_aliases: tuple[EntityAlias, ...], request: EntityCatalogLookupRequest):
    return resolve_entity(
        ResolveEntityRequest(
            scope=request.scope,
            reference=request.reference,
            origin=FieldOrigin.EXPLICIT_CUSTOMER_TEXT,
            aliases=result_aliases,
            kinds=request.kinds,
            window=request.window,
            current_on=request.current_on,
            context=request.context,
        )
    )


def test_unauthorized_scope_returns_before_current_or_row_reads(
    environment: Environment,
) -> None:
    harness = _harness(environment)
    hidden_key = CampaignKey("campaign:hidden", SCOPE, PLATFORM)

    result = harness.catalog.lookup(
        _lookup(
            EntityReference(canonical_key=hidden_key),
            _LookupOptions(scope=AuthorizedAnalyticalScope(frozenset({OTHER_SCOPE}))),
        )
    )

    assert result.aliases == ()
    assert result.hidden_direct_match
    assert harness.current.scope_calls == []
    assert harness.canonical.calls == []


def test_duplicate_names_from_both_campaign_lenses_require_clarification(
    environment: Environment,
) -> None:
    harness = _harness(environment)
    harness.publish(
        _PublishRequest(
            "amc-campaign-dsp",
            (
                _dsp_row(
                    "campaign:dsp",
                    "Northstar Home | Brand",
                    _DspOptions("A0DSP0001"),
                ),
            ),
            "dsp",
        )
    )
    harness.publish(
        _PublishRequest(
            "amc-campaign-sponsored",
            (
                _sponsored_row(
                    "campaign:sponsored",
                    "Northstar Home | Brand",
                    "A0SPONSORED0001",
                ),
            ),
            "sponsored",
        )
    )
    request = _lookup(EntityReference(text="northstar home brand"))

    result = harness.catalog.lookup(request)
    record = _resolve(result.aliases, request)

    assert record.outcome is ResolutionOutcome.AMBIGUOUS
    assert {candidate.channel for candidate in record.candidates} == {
        ChannelKey("amazon_dsp"),
        ChannelKey("amazon_sponsored_brands"),
    }
    assert {call.partition.scope.lens for call in harness.canonical.calls}
    assert len(harness.canonical.calls) == 2


def test_renamed_campaign_expands_complete_alias_history_for_one_key(
    environment: Environment,
) -> None:
    harness = _harness(environment)
    harness.publish(
        _PublishRequest(
            "amc-campaign-dsp",
            (
                _dsp_row(
                    "campaign:stable",
                    "Northstar Home | Prospecting",
                    _DspOptions(
                        "A0STABLE",
                        valid_to=datetime(2026, 8, 12, tzinfo=UTC),
                        is_current=False,
                    ),
                ),
                _dsp_row(
                    "campaign:stable",
                    "Northstar Home | Growth",
                    _DspOptions(
                        "A0STABLE",
                        valid_from=datetime(2026, 8, 12, tzinfo=UTC),
                    ),
                ),
            ),
            "renamed",
        )
    )
    request = _lookup(EntityReference(text="Northstar Home Prospecting"))

    result = harness.catalog.lookup(request)
    record = _resolve(result.aliases, request)

    assert record.outcome is ResolutionOutcome.RESOLVED
    assert str(record.value) == "campaign:stable"
    assert {label.value for label in record.candidates[0].labels} == {
        "Northstar Home | Prospecting",
        "Northstar Home | Growth",
    }


def test_source_identifier_resolves_campaign_without_a_readable_name(
    environment: Environment,
) -> None:
    harness = _harness(environment)
    source = SourceIdentifier("amc.dsp.campaign_id_string", "A0UNNAMED")
    harness.publish(
        _PublishRequest(
            "amc-campaign-dsp",
            (
                _dsp_row(
                    "campaign:unnamed",
                    None,
                    _DspOptions(source.value),
                ),
            ),
            "unnamed",
        )
    )
    request = _lookup(
        EntityReference(
            source_identifier=source,
            source_system=SOURCE_SYSTEM,
            source_scope=SOURCE_SCOPE,
        )
    )

    unqualified = harness.catalog.lookup(_lookup(EntityReference(source_identifier=source)))
    result = harness.catalog.lookup(request)
    record = _resolve(result.aliases, request)

    assert unqualified.aliases == ()
    assert record.outcome is ResolutionOutcome.RESOLVED
    assert str(record.value) == "campaign:unnamed"


def test_taxonomy_preserves_names_and_kind_ambiguity(
    environment: Environment,
) -> None:
    harness = _harness(environment)
    request = _lookup(
        EntityReference(text="Amazon DSP"),
        _LookupOptions(kinds=frozenset({EntityKind.CHANNEL, EntityKind.AD_PRODUCT})),
    )

    result = harness.catalog.lookup(request)
    record = _resolve(result.aliases, request)

    assert record.outcome is ResolutionOutcome.AMBIGUOUS
    assert {candidate.kind for candidate in record.candidates} == {
        EntityKind.CHANNEL,
        EntityKind.AD_PRODUCT,
    }
    assert {label.value for alias in result.aliases for label in alias.labels} == {"Amazon DSP"}


def test_catalog_bounds_canonical_candidates_and_marks_truncation(
    environment: Environment,
) -> None:
    harness = _harness(environment)
    rows = tuple(
        _dsp_row(
            f"campaign:bounded{index}",
            "Shared Campaign",
            _DspOptions(f"A0BOUND{index}"),
        )
        for index in range(3)
    )
    harness.publish(_PublishRequest("amc-campaign-dsp", rows, "bounded"))

    result = harness.catalog.lookup(
        _lookup(
            EntityReference(text="Shared Campaign"),
            _LookupOptions(max_candidates=2),
        )
    )

    assert result.truncated
    assert len({alias.key for alias in result.aliases}) == 2


def test_catalog_rejects_malformed_successful_dimension_rows(
    environment: Environment,
) -> None:
    harness = _harness(environment)
    malformed = _dsp_row(
        "campaign:malformed",
        "Malformed",
        _DspOptions("A0MALFORMED"),
    )
    malformed["unexpected"] = "field"
    harness.publish(_PublishRequest("amc-campaign-dsp", (malformed,), "malformed"))

    with pytest.raises(ValueError, match="unexpected"):
        harness.catalog.lookup(_lookup(EntityReference(text="Malformed")))


def test_catalog_lookup_requires_one_explicit_effective_selector() -> None:
    with pytest.raises(ValueError, match="exactly one window or current day"):
        _lookup(
            EntityReference(text="Campaign"),
            _LookupOptions(window=None),
        )
    with pytest.raises(ValueError, match="exactly one window or current day"):
        _lookup(
            EntityReference(text="Campaign"),
            _LookupOptions(current_on=date(2026, 8, 24)),
        )


def _product_row() -> dict[str, object]:
    return {
        "amazon_asin": "amazon_asin:b0fixture01",
        "amazon_parent_asin": None,
        "marketplace_key": "amazon_us",
        "product_title": "Northstar Home Air Purifier",
        "brand": "Northstar Home",
        "brand_code": "NORTHSTAR_HOME",
        "valid_from": datetime(2026, 1, 1, tzinfo=UTC),
        "valid_to": OPEN_END,
        "is_current": True,
    }


def test_product_brand_and_marketplace_aliases_resolve_with_governed_context(
    environment: Environment,
) -> None:
    harness = _harness(environment)
    harness.publish(
        _PublishRequest(
            "vendor-product-hierarchy",
            (_product_row(),),
            "product",
        )
    )

    product_request = _lookup(
        EntityReference(text="Northstar Home Air Purifier"),
        _LookupOptions(kinds=frozenset({EntityKind.PRODUCT})),
    )
    product_result = harness.catalog.lookup(product_request)
    product_record = _resolve(product_result.aliases, product_request)
    brand_request = _lookup(
        EntityReference(text="Northstar Home"),
        _LookupOptions(kinds=frozenset({EntityKind.BRAND})),
    )
    brand_result = harness.catalog.lookup(brand_request)
    brand_record = _resolve(brand_result.aliases, brand_request)
    marketplace_request = _lookup(
        EntityReference(text="US"),
        _LookupOptions(kinds=frozenset({EntityKind.MARKETPLACE})),
    )
    marketplace_result = harness.catalog.lookup(marketplace_request)
    marketplace_record = _resolve(marketplace_result.aliases, marketplace_request)

    assert product_record.outcome is ResolutionOutcome.RESOLVED
    assert str(product_record.value) == "amazon_asin:b0fixture01"
    assert product_record.candidates[0].platform == PlatformKey("amazon_retail")
    assert brand_record.outcome is ResolutionOutcome.RESOLVED
    assert str(brand_record.value).startswith("brand:")
    assert marketplace_record.outcome is ResolutionOutcome.RESOLVED
    assert str(marketplace_record.value) == "amazon_us"


def test_asi_segment_marketplace_value_is_a_qualified_alias_never_a_text_label(
    environment: Environment,
) -> None:
    """The ASI integer `1` fills an AMC parameter; as text it would turn "1" into a marketplace."""
    harness = _harness(environment)
    marketplace = _LookupOptions(kinds=frozenset({EntityKind.MARKETPLACE}))

    by_text = harness.catalog.lookup(_lookup(EntityReference(text="1"), marketplace))
    qualified_request = _lookup(
        EntityReference(source_identifier=SourceIdentifier("amc.segment_marketplace_id", "1")),
        marketplace,
    )
    qualified = harness.catalog.lookup(qualified_request)
    record = _resolve(qualified.aliases, qualified_request)

    assert by_text.aliases == ()
    assert record.outcome is ResolutionOutcome.RESOLVED
    assert str(record.value) == "amazon_us"
    segment_aliases = tuple(
        alias
        for alias in qualified.aliases
        if SourceIdentifier("amc.segment_marketplace_id", "1") in alias.source_identifiers
    )
    assert segment_aliases
    assert all(
        tuple(label.value for label in alias.labels) == ("United States",)
        for alias in segment_aliases
    )


def test_catalog_reads_only_the_latest_complete_history_publication(
    environment: Environment,
) -> None:
    harness = _harness(environment)
    older = harness.publish(
        _PublishRequest(
            "amc-campaign-dsp",
            (
                _dsp_row(
                    "campaign:older",
                    "Older Snapshot Only",
                    _DspOptions("A0OLDER"),
                ),
            ),
            "older",
            on=date(2026, 8, 13),
        )
    )
    latest = harness.publish(
        _PublishRequest(
            "amc-campaign-dsp",
            (
                _dsp_row(
                    "campaign:latest",
                    "Latest Snapshot",
                    _DspOptions("A0LATEST"),
                ),
            ),
            "latest",
            on=date(2026, 8, 14),
        )
    )

    result = harness.catalog.lookup(_lookup(EntityReference(text="Older Snapshot Only")))

    assert result.aliases == ()
    assert harness.canonical.calls == [latest]
    assert older not in harness.canonical.calls


def _browse(
    scope: AuthorizedAnalyticalScope = AUTHORIZED,
    max_candidates: int = 25,
) -> EntityCatalogBrowseRequest:
    return EntityCatalogBrowseRequest(
        scope=scope,
        kinds=frozenset({EntityKind.CAMPAIGN}),
        window=WINDOW,
        current_on=None,
        context=EntityContext(),
        max_candidates=max_candidates,
    )


def test_unauthorized_browse_returns_before_current_or_row_reads(
    environment: Environment,
) -> None:
    harness = _harness(environment)

    result = harness.catalog.browse(_browse(AuthorizedAnalyticalScope(frozenset({OTHER_SCOPE}))))

    assert result.aliases == ()
    assert not result.truncated
    assert harness.current.scope_calls == []
    assert harness.canonical.calls == []


def test_browse_bounds_campaign_keys_and_returns_complete_rename_history(
    environment: Environment,
) -> None:
    harness = _harness(environment)
    harness.publish(
        _PublishRequest(
            "amc-campaign-dsp",
            (
                _dsp_row(
                    "campaign:stable",
                    "Northstar Home | Prospecting",
                    _DspOptions(
                        "A0STABLE",
                        valid_to=datetime(2026, 8, 12, tzinfo=UTC),
                        is_current=False,
                    ),
                ),
                _dsp_row(
                    "campaign:stable",
                    "Northstar Home | Growth",
                    _DspOptions(
                        "A0STABLE",
                        valid_from=datetime(2026, 8, 12, tzinfo=UTC),
                    ),
                ),
                _dsp_row(
                    "campaign:zzz-second",
                    None,
                    _DspOptions("A0SECOND"),
                ),
            ),
            "browse",
        )
    )

    result = harness.catalog.browse(_browse(max_candidates=1))

    assert result.truncated
    assert result.authorized_count == 2
    assert {alias.key for alias in result.aliases} == {
        CampaignKey("campaign:stable", SCOPE, PLATFORM)
    }
    assert {label.value for alias in result.aliases for label in alias.labels} == {
        "Northstar Home | Prospecting",
        "Northstar Home | Growth",
    }


def test_public_campaign_label_uses_unique_name_effective_at_window_end(
    environment: Environment,
) -> None:
    harness = _harness(environment)
    harness.publish(
        _PublishRequest(
            "amc-campaign-dsp",
            (
                _dsp_row(
                    "campaign:stable",
                    "Northstar Prospecting",
                    _DspOptions(
                        "A0STABLE",
                        valid_to=datetime(2026, 8, 12, tzinfo=UTC),
                        is_current=False,
                    ),
                ),
                _dsp_row(
                    "campaign:stable",
                    "Northstar Growth",
                    _DspOptions(
                        "A0STABLE",
                        valid_from=datetime(2026, 8, 12, tzinfo=UTC),
                    ),
                ),
            ),
            "public-campaign-rename",
        )
    )

    result = harness.catalog.public_labels(
        _public_labels(
            (("campaign_key", "campaign:stable"),),
            context=EntityContext(platform=PLATFORM),
        )
    )

    assert result.resolutions[0].outcome is PublicEntityLabelOutcome.RESOLVED
    assert result.resolutions[0].display_label == "Northstar Growth"


def test_public_product_label_requires_unique_title_and_exact_marketplace(
    environment: Environment,
) -> None:
    harness = _harness(environment)
    missing_title = {
        **_product_row(),
        "amazon_asin": "amazon_asin:b0fixture02",
        "product_title": None,
    }
    unique_title = {
        **_product_row(),
        "amazon_asin": "amazon_asin:b0fixture03",
        "product_title": "Northstar Compact Purifier",
    }
    ambiguous_title = {**_product_row(), "product_title": "Alternative governed title"}
    harness.publish(
        _PublishRequest(
            "vendor-product-hierarchy",
            (_product_row(), missing_title, unique_title, ambiguous_title),
            "public-products",
        )
    )

    resolved = harness.catalog.public_labels(
        _public_labels(
            (("amazon_asin", "b0fixture03"),),
            context=EntityContext(marketplace=MarketplaceKey("amazon_us")),
        )
    )
    missing = harness.catalog.public_labels(
        _public_labels(
            (("amazon_asin", "b0fixture02"),),
            context=EntityContext(marketplace=MarketplaceKey("amazon_us")),
        )
    )
    ambiguous = harness.catalog.public_labels(
        _public_labels(
            (("amazon_asin", "b0fixture01"),),
            context=EntityContext(marketplace=MarketplaceKey("amazon_us")),
        )
    )
    wrong_marketplace = harness.catalog.public_labels(
        _public_labels(
            (("amazon_asin", "b0fixture01"),),
            context=EntityContext(marketplace=MarketplaceKey("amazon_ca")),
        )
    )

    assert resolved.resolutions[0].outcome is PublicEntityLabelOutcome.RESOLVED
    assert resolved.resolutions[0].display_label == "Northstar Compact Purifier"
    assert missing.resolutions[0].outcome is PublicEntityLabelOutcome.NOT_FOUND
    assert missing.resolutions[0].display_label is None
    assert ambiguous.resolutions[0].outcome is PublicEntityLabelOutcome.AMBIGUOUS
    assert ambiguous.resolutions[0].display_label is None
    assert wrong_marketplace.resolutions[0].outcome is PublicEntityLabelOutcome.INVALID_CONTEXT
    assert wrong_marketplace.resolutions[0].display_label is None


def test_public_marketplace_and_taxonomy_labels_use_governed_names_in_position(
    environment: Environment,
) -> None:
    harness = _harness(environment)

    result = harness.catalog.public_labels(
        _public_labels(
            (("marketplace_key", "amazon_us"),),
            (("platform_key", "amazon_ads"),),
            (("channel_key", "amazon_dsp"),),
        )
    )

    assert tuple(item.display_label for item in result.resolutions) == (
        "United States",
        "Amazon Ads",
        "Amazon DSP",
    )
    assert all(item.outcome is PublicEntityLabelOutcome.RESOLVED for item in result.resolutions)
    assert "AMAZON.COM" not in repr(result)


def test_public_labels_unauthorized_scope_reads_nothing(
    environment: Environment,
) -> None:
    harness = _harness(environment)

    result = harness.catalog.public_labels(
        _public_labels(
            (("campaign_key", "campaign:hidden"),),
            scope=AuthorizedAnalyticalScope(frozenset({OTHER_SCOPE})),
            context=EntityContext(platform=PLATFORM),
        )
    )

    assert result.resolutions == (
        PublicEntityLabelResolution(PublicEntityLabelOutcome.UNAUTHORIZED_OR_HIDDEN),
    )
    assert harness.current.scope_calls == []
    assert harness.canonical.calls == []


def test_duplicate_public_labels_are_refused_without_ordinal_disambiguation(
    environment: Environment,
) -> None:
    harness = _harness(environment)
    harness.publish(
        _PublishRequest(
            "amc-campaign-dsp",
            (
                _dsp_row("campaign:first", "Shared governed name", _DspOptions("A0FIRST")),
                _dsp_row("campaign:second", "Shared governed name", _DspOptions("A0SECOND")),
            ),
            "duplicate-public-labels",
        )
    )

    result = harness.catalog.public_labels(
        _public_labels(
            (("campaign_key", "campaign:first"),),
            (("campaign_key", "campaign:second"),),
            context=EntityContext(platform=PLATFORM),
        )
    )

    assert tuple(item.outcome for item in result.resolutions) == (
        PublicEntityLabelOutcome.AMBIGUOUS,
        PublicEntityLabelOutcome.AMBIGUOUS,
    )
    assert all(item.display_label is None for item in result.resolutions)


_CANONICAL_KEY_TOKEN = re.compile(r"[a-z0-9]+(_[a-z0-9]+)+")
"""A canonical media key such as ``amazon_sponsored_brands`` (W14 case 2; deterministic text)."""
_SHARED_NAME = "Shared governed name"


def _renamed_channel(name: str) -> MediaTaxonomy:
    """Return the governed taxonomy with the Sponsored Brands channel's name replaced."""
    taxonomy = load_media_taxonomy(ROOT)
    target = ChannelKey("amazon_sponsored_brands")
    return replace(
        taxonomy,
        terms=tuple(
            replace(term, name=name) if term.key == target else term for term in taxonomy.terms
        ),
    )


def _publish_same_named_pair(harness: Harness, *, missing_name: bool = False) -> None:
    """One DSP and one Sponsored Brands campaign under one name (plus an unnamed DSP one)."""
    dsp = [_dsp_row("campaign:dsp", _SHARED_NAME, _DspOptions("A0DSP"))]
    if missing_name:
        dsp.append(_dsp_row("campaign:unnamed", None, _DspOptions("A0UNNAMED")))
    harness.publish(_PublishRequest("amc-campaign-dsp", tuple(dsp), "w14-dsp"))
    harness.publish(
        _PublishRequest(
            "amc-campaign-sponsored",
            (_sponsored_row("campaign:brands", _SHARED_NAME, "B0BRANDS"),),
            "w14-sponsored",
        )
    )


def _pair_labels(harness: Harness, *extra: str) -> PublicEntityLabelResult:
    return harness.catalog.public_labels(
        _public_labels(
            (("campaign_key", "campaign:dsp"),),
            (("campaign_key", "campaign:brands"),),
            *((("campaign_key", key),) for key in extra),
        )
    )


@pytest.mark.covers("ADR-067")
def test_w14_same_named_campaigns_on_two_channels_carry_the_channel_label(
    environment: Environment,
) -> None:
    harness = _harness(environment)
    _publish_same_named_pair(harness)

    result = _pair_labels(harness)

    assert tuple(item.outcome for item in result.resolutions) == (
        PublicEntityLabelOutcome.RESOLVED,
        PublicEntityLabelOutcome.RESOLVED,
    )
    labels = tuple(item.display_label for item in result.resolutions)
    assert labels == (f"{_SHARED_NAME} (Amazon DSP)", f"{_SHARED_NAME} (Sponsored Brands)")
    # Case 2: the qualifier is the governed channel name, never its canonical key.
    assert not any(_CANONICAL_KEY_TOKEN.search(cast("str", label)) for label in labels)


@pytest.mark.covers("ADR-067")
def test_w14_a_unique_campaign_label_is_never_qualified(environment: Environment) -> None:
    """Positive control: qualification only answers a repeat in the batch."""
    harness = _harness(environment)
    _publish_same_named_pair(harness)

    result = harness.catalog.public_labels(_public_labels((("campaign_key", "campaign:brands"),)))

    assert result.resolutions[0].display_label == _SHARED_NAME


@pytest.mark.covers("ADR-067")
def test_w14_equal_channel_labels_stay_ambiguous(environment: Environment) -> None:
    harness = _harness(environment, _renamed_channel("Amazon DSP"))
    _publish_same_named_pair(harness)

    result = _pair_labels(harness)

    assert all(
        item.outcome is PublicEntityLabelOutcome.AMBIGUOUS and item.display_label is None
        for item in result.resolutions
    )


@pytest.mark.covers("ADR-067")
def test_w14_a_channel_label_that_fails_the_public_text_rule_keeps_the_placeholder(
    environment: Environment,
) -> None:
    """The channel's governed name equal to its canonical key never reaches public text."""
    harness = _harness(environment, _renamed_channel("amazon_sponsored_brands"))
    _publish_same_named_pair(harness)

    result = _pair_labels(harness)

    assert result.resolutions[0].display_label == f"{_SHARED_NAME} (Amazon DSP)"
    assert result.resolutions[1] == PublicEntityLabelResolution(PublicEntityLabelOutcome.AMBIGUOUS)


@pytest.mark.covers("ADR-067")
def test_w14_an_unresolvable_channel_keeps_the_placeholder(environment: Environment) -> None:
    """The Sponsored Brands mapping ends before the window end, so its channel has no label."""
    taxonomy = load_media_taxonomy(ROOT)
    ended = date(2026, 8, 15)
    taxonomy = replace(
        taxonomy,
        mappings=tuple(
            replace(mapping, resolution=replace(mapping.resolution, valid_to=ended))
            if mapping.resolution.channel == ChannelKey("amazon_sponsored_brands")
            else mapping
            for mapping in taxonomy.mappings
        ),
    )
    harness = _harness(environment, taxonomy)
    _publish_same_named_pair(harness)

    result = harness.catalog.public_labels(
        PublicEntityLabelRequest(
            scope=AUTHORIZED,
            items=(
                PublicEntityLabelItem((("campaign_key", "campaign:dsp"),)),
                PublicEntityLabelItem((("campaign_key", "campaign:brands"),)),
            ),
            context=EntityContext(),
            window=DateRange(date(2026, 8, 10), date(2026, 8, 20)),
        )
    )

    assert result.resolutions[0].display_label == f"{_SHARED_NAME} (Amazon DSP)"
    assert result.resolutions[1].outcome is PublicEntityLabelOutcome.AMBIGUOUS


@pytest.mark.covers("ADR-067")
def test_w14_chat_labels_keep_the_placeholder_for_true_duplicates_and_missing_names(
    environment: Environment,
) -> None:
    """Cases 5 and 6 through the chat resolver, the only consumer of public_labels."""
    harness = _harness(environment)
    harness.publish(
        _PublishRequest(
            "amc-campaign-dsp",
            (
                _dsp_row("campaign:first", _SHARED_NAME, _DspOptions("A0FIRST")),
                _dsp_row("campaign:second", _SHARED_NAME, _DspOptions("A0SECOND")),
                _dsp_row("campaign:unnamed", None, _DspOptions("A0UNNAMED")),
            ),
            "w14-true-duplicates",
        )
    )
    rows: tuple[dict[str, str | int | Decimal | None], ...] = tuple(
        {"campaign_key": key} for key in ("campaign:first", "campaign:second", "campaign:unnamed")
    )

    labels = DimensionLabelResolver(harness.catalog, AUTHORIZED).resolve(
        rows, ("campaign_key",), WINDOW
    )

    assert {entry.value: entry.label for entry in labels.entries} == {
        "campaign:first": "Unnamed campaign 1",
        "campaign:second": "Unnamed campaign 2",
        "campaign:unnamed": "Unnamed campaign 3",
    }


@pytest.mark.covers("ADR-067")
def test_w14_a_qualified_pair_beside_an_unnamed_campaign(environment: Environment) -> None:
    harness = _harness(environment)
    _publish_same_named_pair(harness, missing_name=True)

    result = _pair_labels(harness, "campaign:unnamed")

    assert result.resolutions[2].outcome is PublicEntityLabelOutcome.NOT_FOUND
    assert result.resolutions[0].display_label == f"{_SHARED_NAME} (Amazon DSP)"


def test_public_label_contract_rejects_nonexact_or_duplicated_material() -> None:
    item = PublicEntityLabelItem((("campaign_key", "campaign:one"),))

    with pytest.raises(ValueError, match="unsupported protected dimension"):
        PublicEntityLabelItem((("date_key", "2026-08-10"),))
    with pytest.raises(ValueError, match="nonblank"):
        PublicEntityLabelItem((("campaign_key", " "),))
    with pytest.raises(ValueError, match="repeats an exact protected dimension tuple"):
        PublicEntityLabelRequest(AUTHORIZED, (item, item), EntityContext(), WINDOW)
    with pytest.raises(ValueError, match="between 1 and 500"):
        PublicEntityLabelRequest(
            AUTHORIZED,
            tuple(
                PublicEntityLabelItem((("campaign_key", f"campaign:{index}"),))
                for index in range(501)
            ),
            EntityContext(),
            WINDOW,
        )
    with pytest.raises(ValueError, match="repeats a resolved display label"):
        PublicEntityLabelResult(
            (
                PublicEntityLabelResolution(PublicEntityLabelOutcome.RESOLVED, "Same"),
                PublicEntityLabelResolution(PublicEntityLabelOutcome.RESOLVED, "Same"),
            )
        )
