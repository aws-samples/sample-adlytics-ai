# SPDX-License-Identifier: MIT-0
"""Strict loading and effective resolution of the governed commerce identity bridge."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from adapters.outbound.commerce_identity import load_commerce_identity_bridge
from domain.semantics.commerce_identity import (
    MarketplaceDisplayName,
    ResolveCommerceIdentityRequest,
    resolve_commerce_identity,
)
from domain.semantics.identity import IdentityResolutionError, MarketplaceKey, SourceIdentifier

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-045")]
ROOT = Path(__file__).resolve().parents[2]


def test_real_bridge_resolves_marketplace_id_and_amc_name_to_same_marketplace() -> None:
    bridge = load_commerce_identity_bridge(ROOT)
    by_id = resolve_commerce_identity(
        bridge,
        ResolveCommerceIdentityRequest(
            SourceIdentifier("amazon.marketplace_id", "ATVPDKIKX0DER"),
            date(2026, 8, 25),
        ),
    )
    by_name = resolve_commerce_identity(
        bridge,
        ResolveCommerceIdentityRequest(
            SourceIdentifier("amc.marketplace_name", "AMAZON.COM"),
            date(2026, 8, 25),
        ),
    )
    by_country = resolve_commerce_identity(
        bridge,
        ResolveCommerceIdentityRequest(
            SourceIdentifier("amc.campaign_country", "US"),
            date(2026, 8, 25),
        ),
    )
    assert by_id.marketplace.target == MarketplaceKey("amazon_us")
    assert by_name.marketplace.target == by_id.marketplace.target
    assert by_country.marketplace.target == by_id.marketplace.target
    assert by_name.marketplace.source != by_id.marketplace.source
    assert bridge.marketplace_display_names == (
        MarketplaceDisplayName(MarketplaceKey("amazon_us"), "United States"),
    )


def test_observed_out_of_scope_campaign_country_remains_fail_closed() -> None:
    bridge = load_commerce_identity_bridge(ROOT)
    with pytest.raises(IdentityResolutionError, match="no canonical mapping"):
        resolve_commerce_identity(
            bridge,
            ResolveCommerceIdentityRequest(
                SourceIdentifier("amc.campaign_country", "CA"),
                date(2026, 8, 25),
            ),
        )


def test_real_bridge_mints_marketplace_and_scope_qualified_product() -> None:
    bridge = load_commerce_identity_bridge(ROOT)
    resolved = resolve_commerce_identity(
        bridge,
        ResolveCommerceIdentityRequest(
            marketplace=SourceIdentifier("amazon.marketplace_id", "ATVPDKIKX0DER"),
            product=SourceIdentifier("vendor.asin", "B000TEST12"),
            business_date=date(2026, 8, 25),
        ),
    )
    assert resolved.product is not None
    assert resolved.product.target.value == "amazon_asin:b000test12"
    assert resolved.product.target.marketplace == MarketplaceKey("amazon_us")
    assert resolved.product.target.commerce_scope == bridge.commerce_scope
