# SPDX-License-Identifier: MIT-0
"""Semantic V2 identity-domain prerequisites and effective-dated source resolution.

These component tests prove source/business key separation and reproducible mapping evidence. The
cross-capability SA-03/SA-04 tests remain later: they additionally require canonical publication to
fail without changing current state.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from typing import Any, cast

import pytest

from domain.semantics.commerce_identity import (
    CommerceIdentityBridge,
    ResolveCommerceIdentityRequest,
    resolve_commerce_identity,
)
from domain.semantics.identity import (
    CanonicalAdvertiserKeyRequest,
    CanonicalCampaignKeyRequest,
    CanonicalKey,
    ChannelKey,
    CommerceScopeKey,
    DeliveryScope,
    ExtractionProfileKey,
    IdentityMapping,
    IdentityResolutionError,
    MarketplaceKey,
    PlatformKey,
    ProductKey,
    SourceDatasetKey,
    SourceIdentifier,
    SourceSystemKey,
    canonical_advertiser_key,
    canonical_campaign_key,
    resolve_identity,
)

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-045")]

AMAZON_US = CommerceScopeKey("amazon_us")
US_MARKETPLACE = MarketplaceKey("amazon_us")


def _product(value: str) -> ProductKey:
    return ProductKey(value, commerce_scope=AMAZON_US, marketplace=US_MARKETPLACE)


def test_domainless_canonical_key_cannot_be_constructed() -> None:
    with pytest.raises(TypeError, match="abstract identity boundary"):
        CanonicalKey("unscoped")


def test_business_and_source_keys_with_the_same_text_are_not_equal() -> None:
    source = SourceSystemKey("amazon")
    platform = PlatformKey("amazon")
    assert source != platform


def test_channel_is_business_vocabulary_not_a_source_dataset() -> None:
    channel = ChannelKey("amazon_sponsored_products")
    dataset = SourceDatasetKey("amazon_sponsored_products")
    assert channel != dataset


def test_product_identity_requires_its_canonical_commerce_scope() -> None:
    product = _product("product_a")
    assert product.commerce_scope == AMAZON_US
    assert product.marketplace == US_MARKETPLACE


def test_delivery_scope_keeps_profile_and_source_namespace() -> None:
    scope = DeliveryScope(
        source_system=SourceSystemKey("amc"),
        source_dataset=SourceDatasetKey("sponsored_ads_traffic"),
        extraction_profile=ExtractionProfileKey("amazon_amc_sponsored_traffic_daily", 3),
        source_scope=SourceIdentifier("amazon.marketplace_id", "ATVPDKIKX0DER"),
        # pragma: allowlist nextline secret (A-ENTROPY-IDENTIFIER-001)
        option_set_hash="44136fa355b3678a",
    )
    assert scope.extraction_profile.version == 3
    assert scope.source_scope.namespace == "amazon.marketplace_id"


def _campaign_key_request() -> CanonicalCampaignKeyRequest:
    return CanonicalCampaignKeyRequest(
        commerce_scope=AMAZON_US,
        platform=PlatformKey("amazon_ads"),
        channel=ChannelKey("amazon_dsp"),
        source_system=SourceSystemKey("amc"),
        source_scope=SourceIdentifier("amc.entity", "fictional-account-a"),
        identifier_kind="campaign_id_string",
        source_value="fictional-campaign-001",
    )


@pytest.mark.covers("ADR-053")
def test_campaign_key_separates_equal_ids_from_different_source_scopes() -> None:
    request = _campaign_key_request()
    other_scope = replace(
        request,
        source_scope=SourceIdentifier("amc.entity", "fictional-account-b"),
    )

    assert canonical_campaign_key(request) != canonical_campaign_key(other_scope)


@pytest.mark.covers("ADR-053")
def test_campaign_key_separates_channel_and_identifier_kind() -> None:
    request = _campaign_key_request()

    assert canonical_campaign_key(request) != canonical_campaign_key(
        replace(request, channel=ChannelKey("amazon_sponsored_products"))
    )
    assert canonical_campaign_key(request) != canonical_campaign_key(
        replace(request, identifier_kind="campaign_id")
    )


@pytest.mark.covers("ADR-053")
def test_advertiser_key_is_qualified_by_source_account() -> None:
    request = CanonicalAdvertiserKeyRequest(
        commerce_scope=AMAZON_US,
        platform=PlatformKey("amazon_ads"),
        source_system=SourceSystemKey("amc"),
        source_scope=SourceIdentifier("amc.entity", "fictional-account-a"),
        source=SourceIdentifier("amc.dsp.advertiser_id", "fictional-advertiser-001"),
    )
    other_scope = replace(
        request,
        source_scope=SourceIdentifier("amc.entity", "fictional-account-b"),
    )
    other_system = replace(request, source_system=SourceSystemKey("fictional_ads"))

    assert canonical_advertiser_key(request) != canonical_advertiser_key(other_scope)
    assert canonical_advertiser_key(request) != canonical_advertiser_key(other_system)


def test_marketplace_aliases_resolve_with_reproducible_mapping_evidence() -> None:
    target = MarketplaceKey("amazon_us")
    mappings = (
        IdentityMapping(
            source=SourceIdentifier("amazon.marketplace_id", "ATVPDKIKX0DER"),
            target=target,
            mapping_version=1,
            valid_from=date(2000, 1, 1),
        ),
        IdentityMapping(
            source=SourceIdentifier("amc.marketplace_name", "AMAZON.COM"),
            target=target,
            mapping_version=2,
            valid_from=date(2000, 1, 1),
        ),
    )
    resolution = resolve_identity(
        SourceIdentifier("amc.marketplace_name", "AMAZON.COM"),
        on=date(2026, 8, 25),
        mappings=mappings,
    )
    assert resolution.target == target
    assert resolution.mapping_version == 2
    assert resolution.source == SourceIdentifier("amc.marketplace_name", "AMAZON.COM")


def test_an_unresolved_identity_refuses_instead_of_becoming_a_key() -> None:
    with pytest.raises(IdentityResolutionError, match="no canonical mapping"):
        resolve_identity(
            SourceIdentifier("amc.marketplace_name", "UNKNOWN"),
            on=date(2026, 8, 25),
            mappings=(),
        )


def test_an_ambiguous_identity_refuses_instead_of_choosing_by_order() -> None:
    source = SourceIdentifier("retailer.item_id", "123")
    mappings = (
        IdentityMapping(
            source=source,
            target=_product("product_a"),
            mapping_version=1,
            valid_from=date(2026, 1, 1),
        ),
        IdentityMapping(
            source=source,
            target=_product("product_b"),
            mapping_version=2,
            valid_from=date(2026, 1, 1),
        ),
    )
    with pytest.raises(IdentityResolutionError, match="ambiguously"):
        resolve_identity(source, on=date(2026, 8, 25), mappings=mappings)


def test_overlapping_versions_are_ambiguous_even_when_the_target_matches() -> None:
    source = SourceIdentifier("retailer.item_id", "123")
    target = _product("product_a")
    mappings = (
        IdentityMapping(
            source=source,
            target=target,
            mapping_version=1,
            valid_from=date(2026, 1, 1),
        ),
        IdentityMapping(
            source=source,
            target=target,
            mapping_version=2,
            valid_from=date(2026, 7, 1),
        ),
    )
    with pytest.raises(IdentityResolutionError, match="ambiguously"):
        resolve_identity(source, on=date(2026, 8, 25), mappings=mappings)


def test_runtime_refuses_a_noncanonical_mapping_target() -> None:
    with pytest.raises(TypeError, match="canonical key"):
        IdentityMapping(
            source=SourceIdentifier("retailer.item_id", "123"),
            target=cast("Any", "not-a-canonical-key"),
            mapping_version=1,
            valid_from=date(2026, 1, 1),
        )


def test_effective_dates_select_the_mapping_valid_for_the_fact_day() -> None:
    source = SourceIdentifier("retailer.item_id", "123")
    mappings = (
        IdentityMapping(
            source=source,
            target=_product("old_product"),
            mapping_version=1,
            valid_from=date(2026, 1, 1),
            valid_to=date(2026, 7, 1),
        ),
        IdentityMapping(
            source=source,
            target=_product("new_product"),
            mapping_version=2,
            valid_from=date(2026, 7, 1),
        ),
    )
    assert resolve_identity(source, on=date(2026, 6, 30), mappings=mappings).target == _product(
        "old_product"
    )
    assert resolve_identity(source, on=date(2026, 7, 1), mappings=mappings).target == _product(
        "new_product"
    )


def test_commerce_bridge_resolves_product_with_marketplace_and_scope_evidence() -> None:
    marketplace_source = SourceIdentifier("amc.marketplace_name", "AMAZON.COM")
    product_source = SourceIdentifier("amc.tracked_asin", "B000TEST")
    product = ProductKey("amazon_asin:b000test", AMAZON_US, US_MARKETPLACE)
    bridge = CommerceIdentityBridge(
        commerce_scope=AMAZON_US,
        marketplace_mappings=(
            IdentityMapping(marketplace_source, US_MARKETPLACE, 2, date(2000, 1, 1)),
        ),
        product_mappings=(IdentityMapping(product_source, product, 4, date(2020, 1, 1)),),
    )
    resolved = resolve_commerce_identity(
        bridge,
        ResolveCommerceIdentityRequest(
            marketplace=marketplace_source,
            product=product_source,
            business_date=date(2026, 8, 25),
        ),
    )
    assert resolved.commerce_scope == AMAZON_US
    assert resolved.marketplace.mapping_version == 2
    assert resolved.product is not None
    assert resolved.product.mapping_version == 4


def test_commerce_bridge_refuses_product_from_another_marketplace() -> None:
    marketplace_source = SourceIdentifier("amazon.marketplace_id", "ATVPDKIKX0DER")
    product_source = SourceIdentifier("vendor.asin", "B000TEST")
    other = MarketplaceKey("amazon_ca")
    with pytest.raises(IdentityResolutionError, match="undeclared marketplace"):
        CommerceIdentityBridge(
            commerce_scope=AMAZON_US,
            marketplace_mappings=(
                IdentityMapping(marketplace_source, US_MARKETPLACE, 1, date(2000, 1, 1)),
            ),
            product_mappings=(
                IdentityMapping(
                    product_source,
                    ProductKey("amazon_asin:b000test", AMAZON_US, other),
                    1,
                    date(2000, 1, 1),
                ),
            ),
        )


@pytest.mark.covers("ADR-044", "ADR-045")
def test_source_dataset_preserves_source_owned_uppercase_identifiers() -> None:
    assert SourceDatasetKey("GET_VENDOR_SALES_REPORT").value == "GET_VENDOR_SALES_REPORT"


@pytest.mark.covers("ADR-044", "ADR-045")
@pytest.mark.parametrize("value", ("", "9dataset", "dataset/path", "dataset value"))
def test_source_dataset_still_refuses_unsafe_physical_identifiers(value: str) -> None:
    with pytest.raises(ValueError, match="source-owned identifier"):
        SourceDatasetKey(value)
