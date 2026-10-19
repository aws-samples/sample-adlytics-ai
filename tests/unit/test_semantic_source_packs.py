# SPDX-License-Identifier: MIT-0
"""All Amazon contracts have explicit V2 identity/time source-pack declarations."""

from pathlib import Path

import pytest

from adapters.jobs.contract_validation.loader import load_contracts
from adapters.outbound.source_pack import load_source_packs
from domain.semantics.identity import (
    CanonicalCampaignKeyRequest,
    ChannelKey,
    CommerceScopeKey,
    PlatformKey,
    SourceIdentifier,
    SourceSystemKey,
    canonical_campaign_key,
)
from domain.semantics.source_pack import (
    PackReadiness,
    SourcePackControlRole,
    SourcePackRole,
)
from domain.semantics.time import ObservationTimeBasis

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-044", "ADR-045", "ADR-046")]
ROOT = Path(__file__).resolve().parents[2]


def test_every_legacy_contract_has_one_v2_source_pack() -> None:
    contracts = {contract.contract_id for _, contract in load_contracts(ROOT)}
    packs = load_source_packs(ROOT)
    assert {pack.contract_id for pack in packs} == contracts
    assert len(packs) == 29
    assert {pack.registry_version for pack in packs} == {2}


@pytest.mark.covers("ADR-053")
def test_every_campaign_pack_declares_its_source_native_identifier_kind() -> None:
    campaigns = {
        pack.contract_id: pack.campaign
        for pack in load_source_packs(ROOT)
        if pack.campaign is not None
    }

    assert set(campaigns) == {
        "amc-attributed-conversion",
        "amc-audience-performance",
        "amc-campaign-dsp",
        "amc-campaign-sponsored",
        "amc-dsp-clicks",
        "amc-dsp-impressions",
        "amc-dsp-reach",
        "amc-dsp-views",
        "amc-new-to-brand-buyers-campaign",
        "amc-reach-frequency",
    }
    assert {campaign.identifier_kind for campaign in campaigns.values()} == {"campaign_id_string"}


@pytest.mark.covers("ADR-053")
@pytest.mark.parametrize(
    ("dimension_id", "fact_id", "channel"),
    (
        ("amc-campaign-dsp", "amc-dsp-impressions", "amazon_dsp"),
        (
            "amc-campaign-sponsored",
            "amc-reach-frequency",
            "amazon_sponsored_products",
        ),
    ),
)
def test_matching_fact_and_dimension_campaign_aliases_share_a_key(
    dimension_id: str,
    fact_id: str,
    channel: str,
) -> None:
    packs = {pack.contract_id: pack for pack in load_source_packs(ROOT)}
    dimension = packs[dimension_id].campaign
    fact = packs[fact_id].campaign
    assert dimension is not None and fact is not None
    assert dimension.namespace != fact.namespace
    assert dimension.identifier_kind == fact.identifier_kind
    source_scope = SourceIdentifier("amc.entity", "fictional-account-a")

    def key(identifier_kind: str) -> object:
        return canonical_campaign_key(
            CanonicalCampaignKeyRequest(
                commerce_scope=CommerceScopeKey("amazon_us"),
                platform=PlatformKey("amazon_ads"),
                channel=ChannelKey(channel),
                source_system=SourceSystemKey("amc"),
                source_scope=source_scope,
                identifier_kind=identifier_kind,
                source_value="fictional-campaign-001",
            )
        )

    assert key(dimension.identifier_kind) == key(fact.identifier_kind)


@pytest.mark.covers("ADR-053")
def test_source_pack_v2_loader_rejects_v1_and_missing_campaign_kind(tmp_path: Path) -> None:
    registry = (ROOT / "contracts/v2/source-packs.json").read_text()
    path = tmp_path / "contracts/v2/source-packs.json"
    path.parent.mkdir(parents=True)
    path.write_text(registry.replace('"version": 2', '"version": 1', 1))
    with pytest.raises(ValueError, match="exactly version 2"):
        load_source_packs(tmp_path)

    path.write_text(registry.replace(',"identifierKind":"campaign_id_string"', "", 1))
    with pytest.raises(ValueError, match="identifierKind"):
        load_source_packs(tmp_path)


def test_control_total_roles_match_the_canonical_model() -> None:
    totals = [pack for pack in load_source_packs(ROOT) if pack.role is SourcePackRole.CONTROL_TOTAL]
    assert {pack.contract_id for pack in totals} == {
        "vendor-sales-reported-total",
        "vendor-traffic-reported-total",
    }
    assert all(pack.product is None for pack in totals if pack.contract_id.startswith("vendor-"))


def test_vendor_control_relationships_are_explicit_and_reciprocal() -> None:
    packs = {pack.contract_id: pack for pack in load_source_packs(ROOT)}
    controlled = {key: pack for key, pack in packs.items() if pack.control is not None}
    assert set(controlled) == {
        "vendor-sales-by-asin",
        "vendor-sales-reported-total",
        "vendor-traffic-by-asin",
        "vendor-traffic-reported-total",
    }
    for contract_id, pack in controlled.items():
        control = pack.control
        assert control is not None
        peer = packs[control.peer_contract_id]
        assert peer.control is not None
        assert peer.control.peer_contract_id == contract_id
        assert peer.control.group == control.group
        assert peer.control.role is not control.role
    assert {pack.control.role for pack in controlled.values() if pack.control is not None} == {
        SourcePackControlRole.DETAIL,
        SourcePackControlRole.REPORTED,
    }


def test_source_business_date_packs_have_explicit_row_bounds() -> None:
    packs = load_source_packs(ROOT)
    source_dated = tuple(
        pack
        for pack in packs
        if pack.time.observation_basis is ObservationTimeBasis.SOURCE_BUSINESS_DATE
    )
    assert source_dated
    assert all(pack.time.start_path and pack.time.end_path for pack in source_dated)


def test_hierarchy_uses_explicit_system_first_observed_snapshot_time() -> None:
    hierarchy = next(
        pack for pack in load_source_packs(ROOT) if pack.contract_id == "vendor-product-hierarchy"
    )
    assert hierarchy.readiness is PackReadiness.READY
    assert hierarchy.blocker is None
    assert hierarchy.time.observation_basis is ObservationTimeBasis.SYSTEM_FIRST_OBSERVED
    assert hierarchy.time.start_path is None and hierarchy.time.end_path is None


def test_all_source_evidence_blockers_are_resolved() -> None:
    assert all(pack.readiness is PackReadiness.READY for pack in load_source_packs(ROOT))


def test_every_pack_matches_the_reviewed_semantic_crosswalk() -> None:
    expected = {
        "amc-campaign-dsp": (
            "dim_campaign",
            "dimension",
            "amc.campaign_country",
            "campaign_country",
            None,
            None,
            "source_observed_on",
        ),
        "amc-campaign-sponsored": (
            "dim_campaign",
            "dimension",
            "amc.marketplace_name",
            "marketplace_name",
            None,
            None,
            "source_observed_on",
        ),
        "amc-attributed-conversion": (
            "fct_attributed_conversion",
            "fact",
            "amc.marketplace_name",
            "conversion_event_marketplace_name",
            "amc.tracked_asin",
            "tracked_asin",
            "conversion_event_date_utc",
        ),
        "amc-deduplicated-temporal-attribution": (
            "fct_deduplicated_temporal_attribution",
            "fact",
            "amc.marketplace_name",
            "marketplace_name",
            None,
            None,
            "event_date_utc",
        ),
        "amc-retail-purchases": (
            "fct_retail_purchase_amc",
            "fact",
            "amc.marketplace_name",
            "marketplace_name",
            "amc.retail_purchase_asin",
            "asin",
            "purchase_date_utc",
        ),
        "amc-audience-performance": (
            "fct_segment_delivery",
            "fact",
            "amc.campaign_country",
            "campaign_country",
            None,
            None,
            "time_window_start",
        ),
        **{
            f"amc-segment-buyer-profile-{name}": (
                "fct_segment_profile",
                "fact",
                "amc.marketplace_name",
                "marketplace_name",
                None,
                None,
                "time_window_start",
            )
            for name in ("inmarket", "lifestyle", "dsp-exposed")
        },
        "amc-dsp-segment-names": (
            "dim_audience_segment",
            "dimension",
            "amc.campaign_country",
            "campaign_country",
            None,
            None,
            "time_window_start",
        ),
        "amc-segment-metadata": (
            "dim_audience_segment",
            "dimension",
            "amc.segment_marketplace_id",
            "segment_marketplace_id",
            None,
            None,
            "time_window_start",
        ),
        "amc-new-to-brand-buyers-campaign": (
            "fct_new_to_brand_buyers",
            "fact",
            "amc.marketplace_name",
            "conversion_event_marketplace_name",
            None,
            None,
            "time_window_start",
        ),
        "amc-acquisition-cohorts": (
            "fct_shopper_acquisition",
            "fact",
            "amc.marketplace_name",
            "marketplace_name",
            None,
            None,
            "time_window_start",
        ),
        "amc-acquisition-cohorts-arp": (
            "fct_shopper_acquisition",
            "fact",
            "amc.marketplace_name",
            "marketplace_name",
            None,
            None,
            "time_window_start",
        ),
        "amc-exposure-groups": (
            "fct_exposure_group_outcome",
            "fact",
            "amc.marketplace_name",
            "marketplace_name",
            None,
            None,
            "time_window_start",
        ),
        "amc-exposure-frequency-platform": (
            "fct_exposure_frequency",
            "fact",
            "amc.marketplace_name",
            "marketplace_name",
            None,
            None,
            "time_window_start",
        ),
        "amc-exposure-frequency-channel": (
            "fct_exposure_frequency",
            "fact",
            "amc.marketplace_name",
            "marketplace_name",
            None,
            None,
            "time_window_start",
        ),
        "amc-new-to-brand-buyers-channel": (
            "fct_new_to_brand_buyers",
            "fact",
            "amc.marketplace_name",
            "conversion_event_marketplace_name",
            None,
            None,
            "time_window_start",
        ),
        "amc-new-to-brand-buyers-platform": (
            "fct_new_to_brand_buyers",
            "fact",
            "amc.marketplace_name",
            "conversion_event_marketplace_name",
            None,
            None,
            "time_window_start",
        ),
        "amc-reach-frequency": (
            "fct_ad_exposure",
            "fact",
            "amc.marketplace_name",
            "marketplace_name",
            None,
            None,
            "event_date_utc",
        ),
        "amc-dsp-clicks": (
            "fct_ad_exposure",
            "fact",
            "amc.campaign_country",
            "campaign_country",
            None,
            None,
            "impression_date_utc",
        ),
        "amc-dsp-impressions": (
            "fct_ad_exposure",
            "fact",
            "amc.campaign_country",
            "campaign_country",
            None,
            None,
            "impression_date_utc",
        ),
        "amc-dsp-reach": (
            "fct_ad_exposure",
            "fact",
            "amc.campaign_country",
            "campaign_country",
            None,
            None,
            "impression_date_utc",
        ),
        "amc-dsp-views": (
            "fct_ad_exposure",
            "fact",
            "amc.campaign_country",
            "campaign_country",
            None,
            None,
            "event_date_utc",
        ),
        "vendor-product-hierarchy": (
            "dim_product",
            "dimension",
            "amazon.marketplace_id",
            "marketplaceId",
            "vendor_data_kiosk.asin",
            "asin",
            None,
        ),
        "vendor-sales-by-asin": (
            "fct_retail_sales",
            "fact",
            "amazon.marketplace_id",
            "reportSpecification.marketplaceIds[0]",
            "vendor.asin",
            "salesByAsin[].asin",
            "salesByAsin[].startDate",
        ),
        "vendor-sales-reported-total": (
            "fct_retail_sales_reported_total",
            "control_total",
            "amazon.marketplace_id",
            "reportSpecification.marketplaceIds[0]",
            None,
            None,
            "salesAggregate[].startDate",
        ),
        "vendor-traffic-by-asin": (
            "fct_product_page_views",
            "fact",
            "amazon.marketplace_id",
            "reportSpecification.marketplaceIds[0]",
            "vendor.asin",
            "trafficByAsin[].asin",
            "trafficByAsin[].startDate",
        ),
        "vendor-traffic-reported-total": (
            "fct_product_page_views_reported_total",
            "control_total",
            "amazon.marketplace_id",
            "reportSpecification.marketplaceIds[0]",
            None,
            None,
            "trafficAggregate[].startDate",
        ),
    }
    actual = {
        pack.contract_id: (
            pack.dataset.value,
            pack.role.value,
            pack.marketplace.namespace,
            pack.marketplace.source_path,
            None if pack.product is None else pack.product.namespace,
            None if pack.product is None else pack.product.source_path,
            pack.time.start_path,
        )
        for pack in load_source_packs(ROOT)
    }
    assert actual == expected

    expected_time = {
        "amc-campaign-dsp": (
            "source_observed_on",
            "source_observed_on",
            "snapshot",
            "day",
        ),
        "amc-campaign-sponsored": (
            "source_observed_on",
            "source_observed_on",
            "snapshot",
            "day",
        ),
        "amc-attributed-conversion": (
            "conversion_event_date_utc",
            "conversion_event_date_utc",
            "daily_additive",
            "day",
        ),
        "amc-deduplicated-temporal-attribution": (
            "event_date_utc",
            "event_date_utc",
            "daily_additive",
            "day",
        ),
        "amc-retail-purchases": ("purchase_date_utc", "purchase_date_utc", "daily_additive", "day"),
        # A periodic pack's grain is its finest declared period; each row's bucket grain is the
        # declared period its half-open AMC window is (ADR-100).
        "amc-new-to-brand-buyers-campaign": (
            "time_window_start",
            "time_window_end",
            "periodic",
            "month",
        ),
        # Packs 8, 9 and 13 (FEAT-008): calendar months; the dimensions' month is the SCD2
        # boundary a name is valid from.
        "amc-audience-performance": ("time_window_start", "time_window_end", "periodic", "month"),
        "amc-dsp-segment-names": ("time_window_start", "time_window_end", "periodic", "month"),
        "amc-segment-metadata": ("time_window_start", "time_window_end", "periodic", "month"),
        # Packs 10-12 (FEAT-009): calendar months, one snapshot per month on the paid path.
        **{
            f"amc-segment-buyer-profile-{name}": (
                "time_window_start",
                "time_window_end",
                "periodic",
                "month",
            )
            for name in ("inmarket", "lifestyle", "dsp-exposed")
        },
        # Packs 14 and 15 (FEAT-010): one cohort vintage, 13 calendar months (ADR-104).
        "amc-acquisition-cohorts": ("time_window_start", "time_window_end", "periodic", "custom"),
        "amc-acquisition-cohorts-arp": (
            "time_window_start",
            "time_window_end",
            "periodic",
            "custom",
        ),
        "amc-exposure-groups": (
            "time_window_start",
            "time_window_end",
            "periodic",
            "week",
        ),
        "amc-exposure-frequency-platform": (
            "time_window_start",
            "time_window_end",
            "periodic",
            "month",
        ),
        "amc-exposure-frequency-channel": (
            "time_window_start",
            "time_window_end",
            "periodic",
            "month",
        ),
        "amc-new-to-brand-buyers-channel": (
            "time_window_start",
            "time_window_end",
            "periodic",
            "week",
        ),
        "amc-new-to-brand-buyers-platform": (
            "time_window_start",
            "time_window_end",
            "periodic",
            "week",
        ),
        "amc-reach-frequency": ("event_date_utc", "event_date_utc", "daily_additive", "day"),
        "amc-dsp-clicks": ("impression_date_utc", "impression_date_utc", "daily_additive", "day"),
        "amc-dsp-impressions": (
            "impression_date_utc",
            "impression_date_utc",
            "daily_additive",
            "day",
        ),
        "amc-dsp-reach": (
            "impression_date_utc",
            "impression_date_utc",
            "daily_additive",
            "day",
        ),
        "amc-dsp-views": ("event_date_utc", "event_date_utc", "daily_additive", "day"),
        "vendor-product-hierarchy": (None, None, "snapshot", "day"),
        "vendor-sales-by-asin": (
            "salesByAsin[].startDate",
            "salesByAsin[].startDate",
            "daily_additive",
            "day",
        ),
        "vendor-sales-reported-total": (
            "salesAggregate[].startDate",
            "salesAggregate[].startDate",
            "daily_additive",
            "day",
        ),
        "vendor-traffic-by-asin": (
            "trafficByAsin[].startDate",
            "trafficByAsin[].startDate",
            "daily_additive",
            "day",
        ),
        "vendor-traffic-reported-total": (
            "trafficAggregate[].startDate",
            "trafficAggregate[].startDate",
            "daily_additive",
            "day",
        ),
    }
    actual_time = {
        pack.contract_id: (
            pack.time.start_path,
            pack.time.end_path,
            pack.time.time_class.value,
            pack.time.grain.value,
        )
        for pack in load_source_packs(ROOT)
    }
    assert actual_time == expected_time


def test_observation_basis_is_explicit_for_every_source_pack() -> None:
    actual = {pack.contract_id: pack.time.observation_basis for pack in load_source_packs(ROOT)}
    assert actual["vendor-product-hierarchy"] is ObservationTimeBasis.SYSTEM_FIRST_OBSERVED
    assert {
        basis for contract_id, basis in actual.items() if contract_id != "vendor-product-hierarchy"
    } == {ObservationTimeBasis.SOURCE_BUSINESS_DATE}
