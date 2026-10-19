# SPDX-License-Identifier: MIT-0
"""GENERATED FROM contracts/*/*.yaml BY make generate-schemas. Do not edit.

One model per data contract: the canonical row a reader must produce for that contract.
`make check-schemas` regenerates this and fails on any diff, so an edit here is reverted
rather than kept. Change the contract instead.

Validates the curated row, not the delivered document — see
adapters/jobs/schema_generation/models.py for why those are different jobs, and
docs/capabilities/data-layer/low-level-design.md §6 for what else the contracts generate.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "AmcAcquisitionCohortsArpRow",
    "AmcAcquisitionCohortsRow",
    "AmcAttributedConversionRow",
    "AmcAudiencePerformanceRow",
    "AmcCampaignDspRow",
    "AmcCampaignSponsoredRow",
    "AmcDeduplicatedTemporalAttributionRow",
    "AmcDspClicksRow",
    "AmcDspImpressionsRow",
    "AmcDspReachRow",
    "AmcDspSegmentNamesRow",
    "AmcDspViewsRow",
    "AmcExposureFrequencyChannelRow",
    "AmcExposureFrequencyPlatformRow",
    "AmcExposureGroupsRow",
    "AmcNewToBrandBuyersCampaignRow",
    "AmcNewToBrandBuyersChannelRow",
    "AmcNewToBrandBuyersPlatformRow",
    "AmcReachFrequencyRow",
    "AmcRetailPurchasesRow",
    "AmcSegmentBuyerProfileDspExposedRow",
    "AmcSegmentBuyerProfileInmarketRow",
    "AmcSegmentBuyerProfileLifestyleRow",
    "AmcSegmentMetadataRow",
    "VendorProductHierarchyRow",
    "VendorSalesByAsinRow",
    "VendorSalesReportedTotalRow",
    "VendorTrafficByAsinRow",
    "VendorTrafficReportedTotalRow",
]


class AmcAcquisitionCohortsRow(BaseModel):
    """One curated row produced by `amc-acquisition-cohorts`.

    Target table: fct_shopper_acquisition.
    Grain: period_start, period_end, time_grain, as_of_month, grain_set, marketplace_key,
    cohort_path, cohort_grain, cohort_month, months_since_acquisition, acquisition_channel_group.
    Measurement basis: ad_attributed.
    Source: amc · amazon_attributed_events_by_conversion_time.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    period_start: date = Field(description="assigned by the load")
    period_end: date = Field(description="assigned by the load")
    time_grain: str = Field(description="assigned by the load")
    as_of_month: date = Field(description="assigned by the load")
    grain_set: str = Field(description="literal 'platform'")
    marketplace_key: str = Field(description="marketplace_name")
    cohort_path: str = Field(description="literal 'standard'")
    cohort_grain: str = Field(description="derived from conversion_event_date_utc")
    cohort_month: date = Field(description="derived from conversion_event_date_utc")
    months_since_acquisition: int = Field(
        description=(
            "derived from conversion_event_date_utc, event_date_utc"
        ),
    )
    acquisition_source_set: str = Field(description="derived from ad_product_type")
    acquisition_channel_group: str = Field(description="assigned by the load")
    channel_group_key: str | None = Field(
        default=None,
        description=(
            "assigned by the load · null means: not_applicable_under_options"
        ),
    )
    channel_key: str | None = Field(
        default=None,
        description=(
            "assigned by the load · null means: not_applicable_under_options"
        ),
    )
    ad_product_key: str | None = Field(
        default=None,
        description=(
            "assigned by the load · null means: not_applicable_under_options"
        ),
    )
    campaign_key: str | None = Field(
        default=None,
        description=(
            "assigned by the load · null means: not_applicable_under_options"
        ),
    )
    identity_unit: str = Field(description="literal 'amazon_user_id'")
    cell_state: str = Field(description="assigned by the load")
    cohort_buyers_new_to_brand_12mo: int | None = Field(
        default=None,
        description=(
            "derived from user_id, new_to_brand · null means: not_returned"
        ),
    )
    cohort_buyers_account_ids: int | None = Field(
        default=None,
        description=(
            "derived from user_id, user_id_type, new_to_brand · null means: not_returned"
        ),
    )
    repeat_buyers: int | None = Field(
        default=None,
        description=(
            "derived from user_id, event_category · null means: not_returned"
        ),
    )
    repeat_buyers_account_ids: int | None = Field(
        default=None,
        description=(
            "derived from user_id, user_id_type, event_category · null means: not_returned"
        ),
    )
    first_repeat_buyers: int | None = Field(
        default=None,
        description=(
            "derived from user_id, event_category · null means: not_returned"
        ),
    )
    cumulative_repeat_buyers: int | None = Field(
        default=None,
        description=(
            "derived from user_id, event_category · null means: not_returned"
        ),
    )
    cumulative_repeat_buyers_account_ids: int | None = Field(
        default=None,
        description=(
            "derived from user_id, user_id_type, event_category · null means: not_returned"
        ),
    )
    repeat_purchases: int | None = Field(
        default=None,
        description=(
            "derived from event_category · null means: not_returned"
        ),
    )
    repeat_revenue: Decimal | None = Field(
        default=None,
        description=(
            "derived from conversions.total_product_sales · null means: not_returned"
        ),
        max_digits=38,
        decimal_places=4,
    )
    currency_code: str | None = Field(
        default=None,
        description=(
            "purchase_currency · null means: not_returned"
        ),
    )
    basis_key: str = Field(description="literal 'ad_attributed'")
    source_timezone: str = Field(description="literal 'UTC'")
    ecosystem_key: str = Field(description="governed ecosystem 'amazon'")
    platform_key: str = Field(description="governed platform 'amazon_ads'")


class AmcAcquisitionCohortsArpRow(BaseModel):
    """One curated row produced by `amc-acquisition-cohorts-arp`.

    Target table: fct_shopper_acquisition.
    Grain: period_start, period_end, time_grain, as_of_month, grain_set, marketplace_key,
    cohort_path, cohort_grain, cohort_month, months_since_acquisition, acquisition_channel_group.
    Measurement basis: retail_actual.
    Source: amc · amazon_attributed_events_by_conversion_time.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    period_start: date = Field(description="assigned by the load")
    period_end: date = Field(description="assigned by the load")
    time_grain: str = Field(description="assigned by the load")
    as_of_month: date = Field(description="assigned by the load")
    grain_set: str = Field(description="literal 'platform'")
    marketplace_key: str = Field(description="marketplace_name")
    cohort_path: str = Field(description="literal 'arp'")
    cohort_grain: str = Field(description="derived from conversion_event_date_utc")
    cohort_month: date = Field(description="derived from conversion_event_date_utc")
    months_since_acquisition: int = Field(
        description=(
            "derived from conversion_event_date_utc, purchase_date_utc"
        ),
    )
    acquisition_source_set: str = Field(description="derived from ad_product_type")
    acquisition_channel_group: str = Field(description="assigned by the load")
    channel_group_key: str | None = Field(
        default=None,
        description=(
            "assigned by the load · null means: not_applicable_under_options"
        ),
    )
    channel_key: str | None = Field(
        default=None,
        description=(
            "assigned by the load · null means: not_applicable_under_options"
        ),
    )
    ad_product_key: str | None = Field(
        default=None,
        description=(
            "assigned by the load · null means: not_applicable_under_options"
        ),
    )
    campaign_key: str | None = Field(
        default=None,
        description=(
            "assigned by the load · null means: not_applicable_under_options"
        ),
    )
    identity_unit: str = Field(description="literal 'amazon_user_id'")
    cell_state: str = Field(description="assigned by the load")
    cohort_buyers_account_ids_check: int | None = Field(
        default=None,
        description=(
            "derived from user_id, user_id_type, new_to_brand · null means: not_returned"
        ),
    )
    repeat_buyers: int | None = Field(
        default=None,
        description=(
            "derived from user_id, purchase_id · null means: not_returned"
        ),
    )
    first_repeat_buyers: int | None = Field(
        default=None,
        description=(
            "derived from user_id, purchase_id · null means: not_returned"
        ),
    )
    cumulative_repeat_buyers: int | None = Field(
        default=None,
        description=(
            "derived from user_id, purchase_id · null means: not_returned"
        ),
    )
    repeat_purchases: int | None = Field(
        default=None,
        description=(
            "derived from purchase_id · null means: not_returned"
        ),
    )
    repeat_revenue: Decimal | None = Field(
        default=None,
        description=(
            "derived from unit_price, purchase_units_sold · null means: not_returned"
        ),
        max_digits=38,
        decimal_places=4,
    )
    currency_code: str | None = Field(
        default=None,
        description=(
            "derived from currency_code · null means: not_returned"
        ),
    )
    basis_key: str = Field(description="literal 'retail_actual'")
    source_timezone: str = Field(description="literal 'UTC'")
    ecosystem_key: str = Field(description="governed ecosystem 'amazon'")
    platform_key: str = Field(description="governed platform 'amazon_ads'")


class AmcAttributedConversionRow(BaseModel):
    """One curated row produced by `amc-attributed-conversion`.

    Target table: fct_attributed_conversion.
    Grain: date_key, channel_key, campaign_key, amazon_asin, marketplace_key, rows_suppressed.
    Measurement basis: ad_attributed.
    Source: amc · amazon_attributed_events_by_conversion_time.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    date_key: date = Field(description="conversion_event_date_utc")
    channel_key: str = Field(description="ad_product_type")
    ad_product_key: str = Field(description="ad_product_type")
    channel_group_key: str = Field(description="derived from ad_product_type")
    campaign_key: str = Field(description="campaign_id_string")
    amazon_asin: str = Field(description="tracked_asin")
    marketplace_key: str = Field(description="conversion_event_marketplace_name")
    conversions: int = Field(description="conversions")
    purchases: int = Field(description="purchases")
    purchases_incl_halo: int = Field(description="total_purchases")
    revenue_attributed: Decimal = Field(
        description=(
            "product_sales"
        ),
        max_digits=38,
        decimal_places=4,
    )
    revenue_attributed_incl_halo: Decimal = Field(
        description=(
            "total_product_sales"
        ),
        max_digits=38,
        decimal_places=4,
    )
    revenue_new_to_brand_12mo: Decimal = Field(
        description=(
            "new_to_brand_product_sales"
        ),
        max_digits=38,
        decimal_places=4,
    )
    currency_code: str = Field(description="purchase_currency")
    units_attributed: int = Field(description="units_sold")
    units_attributed_incl_halo: int = Field(description="total_units_sold")
    purchases_new_to_brand_12mo: int = Field(description="new_to_brand_purchases")
    purchases_new_to_brand_incl_halo_12mo: int | None = Field(
        default=None,
        description=(
            "new_to_brand_total_purchases · null means: not_applicable_under_options"
        ),
    )
    rows_suppressed: bool = Field(description="extraction option, delivered as values_removed")
    suppression_reason: str | None = Field(
        default=None,
        description=(
            "extraction option, delivered as filtered_reason · null means: suppressed"
        ),
    )
    distinct_user_count: int | None = Field(
        default=None,
        description=(
            "extraction option, delivered as distinct_user_count · null means: suppressed"
        ),
    )
    basis_key: str = Field(description="literal 'ad_attributed'")
    source_timezone: str = Field(description="literal 'UTC'")
    ecosystem_key: str = Field(description="governed ecosystem 'amazon'")
    platform_key: str = Field(description="governed platform 'amazon_ads'")


class AmcAudiencePerformanceRow(BaseModel):
    """One curated row produced by `amc-audience-performance`.

    Target table: fct_segment_delivery.
    Grain: period_start, period_end, time_grain, grain_set, marketplace_key, campaign_key,
    segment_namespace, segment_id.
    Measurement basis: ad_attributed.
    Source: amc · dsp_impressions_by_matched_segments.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    period_start: date = Field(description="assigned by the load")
    period_end: date = Field(description="assigned by the load")
    time_grain: str = Field(description="assigned by the load")
    grain_set: str = Field(description="literal 'campaign'")
    marketplace_key: str = Field(description="campaign_country")
    campaign_key: str = Field(description="campaign_id_string")
    segment_namespace: str = Field(description="literal 'amc_dsp_behavior'")
    segment_id: str = Field(description="behavior_segment_id")
    channel_group_key: str = Field(description="assigned by the load")
    channel_key: str = Field(description="assigned by the load")
    ad_product_key: str = Field(description="assigned by the load")
    identity_unit: str = Field(description="literal 'amazon_user_id'")
    cell_state: str = Field(description="assigned by the load")
    segment_reach: int | None = Field(
        default=None,
        description=(
            "derived from user_id, behavior_segment_id · null means: not_returned"
        ),
    )
    segment_purchasers: int | None = Field(
        default=None,
        description=(
            "derived from user_id, purchases · null means: not_returned"
        ),
    )
    impressions: int | None = Field(
        default=None,
        description=(
            "derived from impressions · null means: not_returned"
        ),
    )
    dsp_spend: Decimal | None = Field(
        default=None,
        description=(
            "derived from total_cost · null means: not_returned"
        ),
        max_digits=38,
        decimal_places=4,
    )
    segment_purchases: int | None = Field(
        default=None,
        description=(
            "derived from purchases · null means: not_returned"
        ),
    )
    segment_attributed_revenue: Decimal | None = Field(
        default=None,
        description=(
            "derived from product_sales · null means: not_returned"
        ),
        max_digits=38,
        decimal_places=4,
    )
    dsp_currency: str | None = Field(
        default=None,
        description=(
            "derived from currency_iso_code · null means: not_returned"
        ),
    )
    purchase_currency: str | None = Field(
        default=None,
        description=(
            "derived from purchase_currency · null means: not_returned"
        ),
    )
    basis_key: str = Field(description="literal 'ad_attributed'")
    source_timezone: str = Field(description="literal 'UTC'")
    ecosystem_key: str = Field(description="governed ecosystem 'amazon'")
    platform_key: str = Field(description="governed platform 'amazon_ads'")


class AmcCampaignDspRow(BaseModel):
    """One curated row produced by `amc-campaign-dsp`.

    Target table: dim_campaign.
    Grain: campaign_key, valid_from.
    Measurement basis: ad_attributed.
    Source: amc · dsp_impressions.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_observed_on: date = Field(description="derived from impression_date_utc")
    source_campaign_id: str = Field(description="campaign_id_string")
    campaign_name: str | None = Field(
        default=None,
        description=(
            "campaign · null means: not_yet_available"
        ),
    )
    source_advertiser_id: str = Field(
        description=(
            "advertiser_id · null means: unsupported_by_channel"
        ),
    )
    advertiser_name: str = Field(description="advertiser")
    marketplace_key: str = Field(description="campaign_country")
    source_campaign_namespace: str = Field(description="literal 'amc.dsp.campaign_id_string'")
    source_campaign_identifier_kind: str = Field(description="literal 'campaign_id_string'")
    campaign_key: str = Field(description="assigned by the load")
    advertiser_key: str = Field(description="assigned by the load")
    commerce_scope_key: str = Field(description="assigned by the load")
    source_system_key: str = Field(description="assigned by the load")
    source_scope_namespace: str = Field(description="assigned by the load")
    source_scope_value: str = Field(description="assigned by the load")
    ecosystem_key: str = Field(description="assigned by the load")
    platform_key: str = Field(description="assigned by the load")
    channel_group_key: str = Field(description="assigned by the load")
    channel_key: str = Field(description="assigned by the load")
    ad_product_key: str = Field(description="assigned by the load")
    valid_from: datetime = Field(description="assigned by the load")
    valid_to: datetime = Field(description="assigned by the load")
    is_current: bool = Field(description="literal 'true'")
    rows_suppressed: bool = Field(description="extraction option, delivered as rows_suppressed")


class AmcCampaignSponsoredRow(BaseModel):
    """One curated row produced by `amc-campaign-sponsored`.

    Target table: dim_campaign.
    Grain: campaign_key, valid_from.
    Measurement basis: ad_attributed.
    Source: amc · sponsored_ads_traffic.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_observed_on: date = Field(description="derived from event_date_utc")
    source_campaign_id: str = Field(description="campaign_id_string")
    campaign_name: str | None = Field(
        default=None,
        description=(
            "campaign · null means: not_yet_available"
        ),
    )
    advertiser_name: str = Field(description="advertiser")
    marketplace_key: str = Field(description="marketplace_name")
    source_ad_product_type: str = Field(
        description=(
            "ad_product_type · null means: unsupported_by_channel"
        ),
    )
    source_campaign_namespace: str = Field(description="literal 'amc.sponsored.campaign_id_string'")
    source_campaign_identifier_kind: str = Field(description="literal 'campaign_id_string'")
    campaign_key: str = Field(description="assigned by the load")
    advertiser_key: str | None = Field(
        default=None,
        description=(
            "assigned by the load · null means: not_yet_available"
        ),
    )
    commerce_scope_key: str = Field(description="assigned by the load")
    source_system_key: str = Field(description="assigned by the load")
    source_scope_namespace: str = Field(description="assigned by the load")
    source_scope_value: str = Field(description="assigned by the load")
    ecosystem_key: str = Field(description="assigned by the load")
    platform_key: str = Field(description="assigned by the load")
    channel_group_key: str = Field(description="assigned by the load")
    channel_key: str = Field(description="assigned by the load")
    ad_product_key: str = Field(description="assigned by the load")
    valid_from: datetime = Field(description="assigned by the load")
    valid_to: datetime = Field(description="assigned by the load")
    is_current: bool = Field(description="literal 'true'")
    rows_suppressed: bool = Field(description="extraction option, delivered as rows_suppressed")


class AmcDeduplicatedTemporalAttributionRow(BaseModel):
    """One curated row produced by `amc-deduplicated-temporal-attribution`.

    Target table: fct_deduplicated_temporal_attribution.
    Grain: date_key, channel_key, marketplace_key, currency_code, attribution_model_key,
    rows_suppressed.
    Measurement basis: ad_attributed.
    Source: amc · deduplicated_temporal_attribution.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    date_key: date = Field(description="event_date_utc")
    channel_key: str = Field(description="ad_product_type")
    ad_product_key: str = Field(description="ad_product_type")
    channel_group_key: str = Field(description="derived from ad_product_type")
    marketplace_key: str = Field(description="marketplace_name")
    attribution_model_key: str = Field(
        description=(
            "derived from conversion_id, user_id, event_dt_utc, impression_dt_utc, "
            "click_dt_utc, campaign_id_string"
        ),
    )
    matched_purchase_credit: Decimal = Field(
        description=(
            "derived from conversion_id, user_id, campaign_id_string"
        ),
        max_digits=38,
        decimal_places=4,
    )
    revenue_attributed_deduplicated_incl_halo: Decimal = Field(
        description=(
            "derived from conversion_id, user_id, total_product_sales, campaign_id_string"
        ),
        max_digits=38,
        decimal_places=4,
    )
    currency_code: str = Field(description="purchase_currency")
    rows_suppressed: bool = Field(description="extraction option, delivered as values_removed")
    suppression_reason: str | None = Field(
        default=None,
        description=(
            "extraction option, delivered as filtered_reason · null means: suppressed"
        ),
    )
    distinct_user_count: int | None = Field(
        default=None,
        description=(
            "extraction option, delivered as distinct_user_count · null means: suppressed"
        ),
    )
    basis_key: str = Field(description="literal 'ad_attributed'")
    source_timezone: str = Field(description="literal 'UTC'")
    ecosystem_key: str = Field(description="governed ecosystem 'amazon'")
    platform_key: str = Field(description="governed platform 'amazon_ads'")


class AmcDspClicksRow(BaseModel):
    """One curated row produced by `amc-dsp-clicks`.

    Target table: fct_ad_exposure.
    Grain: date_key, channel_key, campaign_key, ad_product_key, marketplace_key, rows_suppressed.
    Measurement basis: ad_attributed.
    Source: amc · dsp_clicks.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    date_key: date = Field(description="impression_date_utc")
    channel_key: str = Field(description="literal 'amazon_dsp'")
    ad_product_key: str = Field(description="literal 'amazon_dsp'")
    channel_group_key: str = Field(description="literal 'display'")
    campaign_key: str = Field(description="campaign_id_string")
    marketplace_key: str = Field(description="campaign_country")
    clicks: int = Field(description="clicks")
    rows_suppressed: bool = Field(description="extraction option, delivered as values_removed")
    basis_key: str = Field(description="literal 'ad_attributed'")
    source_timezone: str = Field(description="literal 'UTC'")
    ecosystem_key: str = Field(description="governed ecosystem 'amazon'")
    platform_key: str = Field(description="governed platform 'amazon_ads'")


class AmcDspImpressionsRow(BaseModel):
    """One curated row produced by `amc-dsp-impressions`.

    Target table: fct_ad_exposure.
    Grain: date_key, channel_key, campaign_key, ad_product_key, marketplace_key, rows_suppressed.
    Measurement basis: ad_attributed.
    Source: amc · dsp_impressions.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    date_key: date = Field(description="impression_date_utc")
    channel_key: str = Field(description="literal 'amazon_dsp'")
    ad_product_key: str = Field(description="literal 'amazon_dsp'")
    channel_group_key: str = Field(description="literal 'display'")
    campaign_key: str = Field(description="campaign_id_string")
    marketplace_key: str = Field(description="campaign_country")
    impressions: int = Field(description="impressions")
    spend: Decimal = Field(description="total_cost", max_digits=38, decimal_places=4)
    currency_code: str = Field(description="currency_iso_code")
    rows_suppressed: bool = Field(description="extraction option, delivered as values_removed")
    basis_key: str = Field(description="literal 'ad_attributed'")
    source_timezone: str = Field(description="literal 'UTC'")
    ecosystem_key: str = Field(description="governed ecosystem 'amazon'")
    platform_key: str = Field(description="governed platform 'amazon_ads'")


class AmcDspReachRow(BaseModel):
    """One curated row produced by `amc-dsp-reach`.

    Target table: fct_ad_exposure.
    Grain: date_key, channel_key, campaign_key, ad_product_key, marketplace_key, rows_suppressed.
    Measurement basis: ad_attributed.
    Source: amc · dsp_impressions.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    date_key: date = Field(description="impression_date_utc")
    channel_key: str = Field(description="literal 'amazon_dsp'")
    ad_product_key: str = Field(description="literal 'amazon_dsp'")
    channel_group_key: str = Field(description="literal 'display'")
    campaign_key: str = Field(description="campaign_id_string")
    marketplace_key: str = Field(description="campaign_country")
    reach_users: int = Field(description="derived from user_id")
    rows_suppressed: bool = Field(description="extraction option, delivered as values_removed")
    basis_key: str = Field(description="literal 'ad_attributed'")
    source_timezone: str = Field(description="literal 'UTC'")
    ecosystem_key: str = Field(description="governed ecosystem 'amazon'")
    platform_key: str = Field(description="governed platform 'amazon_ads'")


class AmcDspSegmentNamesRow(BaseModel):
    """One curated row produced by `amc-dsp-segment-names`.

    Target table: dim_audience_segment.
    Grain: platform_key, segment_namespace, segment_id, segment_marketplace_key, valid_from.
    Measurement basis: ad_attributed.
    Source: amc · dsp_impressions_by_matched_segments.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    platform_key: str = Field(description="assigned by the load")
    ecosystem_key: str = Field(description="assigned by the load")
    segment_namespace: str = Field(description="literal 'amc_dsp_behavior'")
    segment_id: str = Field(description="behavior_segment_id")
    segment_marketplace_key: str = Field(description="campaign_country")
    segment_name: str | None = Field(
        default=None,
        description=(
            "derived from behavior_segment_name · null means: not_yet_available"
        ),
    )
    segment_description: str | None = Field(
        default=None,
        description=(
            "derived from behavior_segment_description · null means: not_yet_available"
        ),
    )
    category_level_1: str | None = Field(
        default=None,
        description=(
            "assigned by the load · null means: unsupported_by_channel"
        ),
    )
    category_level_2: str | None = Field(
        default=None,
        description=(
            "assigned by the load · null means: unsupported_by_channel"
        ),
    )
    category_path: str | None = Field(
        default=None,
        description=(
            "assigned by the load · null means: unsupported_by_channel"
        ),
    )
    provider: str | None = Field(
        default=None,
        description=(
            "assigned by the load · null means: not_yet_available"
        ),
    )
    valid_from: datetime = Field(description="assigned by the load")
    valid_to: datetime = Field(description="assigned by the load")
    is_current: bool = Field(description="literal 'true'")


class AmcDspViewsRow(BaseModel):
    """One curated row produced by `amc-dsp-views`.

    Target table: fct_ad_exposure.
    Grain: date_key, channel_key, campaign_key, ad_product_key, marketplace_key, rows_suppressed.
    Measurement basis: ad_attributed.
    Source: amc · dsp_views.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    date_key: date = Field(description="event_date_utc")
    channel_key: str = Field(description="literal 'amazon_dsp'")
    ad_product_key: str = Field(description="literal 'amazon_dsp'")
    channel_group_key: str = Field(description="literal 'display'")
    campaign_key: str = Field(description="campaign_id_string")
    marketplace_key: str = Field(description="campaign_country")
    viewable_impressions: int = Field(description="viewable_impressions")
    measurable_impressions: int = Field(description="measurable_impressions")
    viewable_impressions_estimated: int = Field(description="unmeasurable_viewable_impressions")
    rows_suppressed: bool = Field(description="extraction option, delivered as values_removed")
    basis_key: str = Field(description="literal 'ad_attributed'")
    source_timezone: str = Field(description="literal 'UTC'")
    ecosystem_key: str = Field(description="governed ecosystem 'amazon'")
    platform_key: str = Field(description="governed platform 'amazon_ads'")


class AmcExposureFrequencyChannelRow(BaseModel):
    """One curated row produced by `amc-exposure-frequency-channel`.

    Target table: fct_exposure_frequency.
    Grain: period_start, period_end, time_grain, grain_set, marketplace_key, ad_product_key,
    frequency_bucket, identity_space, bucket_scheme, row_kind.
    Measurement basis: ad_attributed.
    Source: amc · dsp_impressions.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    period_start: date = Field(description="assigned by the load")
    period_end: date = Field(description="assigned by the load")
    time_grain: str = Field(description="assigned by the load")
    grain_set: str = Field(description="literal 'channel'")
    marketplace_key: str = Field(description="marketplace_name")
    ad_product_key: str = Field(description="ad_product_type")
    frequency_bucket: str = Field(description="derived from impressions")
    identity_space: str = Field(description="derived from user_id_type")
    bucket_scheme: str = Field(description="derived from impressions")
    row_kind: str = Field(description="derived from impressions")
    attribution_model_key: str | None = Field(
        default=None,
        description=(
            "assigned by the load · null means: not_applicable_under_options"
        ),
    )
    channel_group_key: str = Field(description="assigned by the load")
    channel_key: str = Field(description="assigned by the load")
    campaign_key: str | None = Field(
        default=None,
        description=(
            "assigned by the load · null means: not_applicable_under_options"
        ),
    )
    identity_unit: str = Field(description="literal 'amazon_user_id'")
    cell_state: str = Field(description="assigned by the load")
    shoppers: int | None = Field(
        default=None,
        description=(
            "derived from user_id, impressions · null means: not_returned"
        ),
    )
    purchasers: int | None = Field(
        default=None,
        description=(
            "derived from user_id, event_category · null means: not_returned"
        ),
    )
    ntb_purchasers: int | None = Field(
        default=None,
        description=(
            "derived from user_id, event_category, new_to_brand · null means: not_returned"
        ),
    )
    impressions: int | None = Field(
        default=None,
        description=(
            "derived from impressions · null means: not_returned"
        ),
    )
    dsp_window_spend: Decimal | None = Field(
        default=None,
        description=(
            "derived from total_cost · null means: not_returned"
        ),
        max_digits=38,
        decimal_places=4,
    )
    sponsored_window_spend: Decimal | None = Field(
        default=None,
        description=(
            "derived from spend · null means: not_returned"
        ),
        max_digits=38,
        decimal_places=4,
    )
    purchases: int | None = Field(
        default=None,
        description=(
            "derived from event_category · null means: not_returned"
        ),
    )
    revenue_incl_halo: Decimal | None = Field(
        default=None,
        description=(
            "derived from total_product_sales · null means: not_returned"
        ),
        max_digits=38,
        decimal_places=4,
    )
    dsp_currency: str | None = Field(
        default=None,
        description=(
            "derived from currency_iso_code · null means: not_returned"
        ),
    )
    sponsored_currency: str | None = Field(
        default=None,
        description=(
            "derived from currency_iso_code · null means: not_returned"
        ),
    )
    currency_code: str | None = Field(
        default=None,
        description=(
            "derived from purchase_currency · null means: not_returned"
        ),
    )
    basis_key: str = Field(description="literal 'ad_attributed'")
    source_timezone: str = Field(description="literal 'UTC'")
    ecosystem_key: str = Field(description="governed ecosystem 'amazon'")
    platform_key: str = Field(description="governed platform 'amazon_ads'")


class AmcExposureFrequencyPlatformRow(BaseModel):
    """One curated row produced by `amc-exposure-frequency-platform`.

    Target table: fct_exposure_frequency.
    Grain: period_start, period_end, time_grain, grain_set, marketplace_key, frequency_bucket,
    identity_space, bucket_scheme, row_kind.
    Measurement basis: ad_attributed.
    Source: amc · dsp_impressions.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    period_start: date = Field(description="assigned by the load")
    period_end: date = Field(description="assigned by the load")
    time_grain: str = Field(description="assigned by the load")
    grain_set: str = Field(description="literal 'platform'")
    marketplace_key: str = Field(description="marketplace_name")
    frequency_bucket: str = Field(description="derived from impressions")
    identity_space: str = Field(description="derived from user_id_type")
    bucket_scheme: str = Field(description="derived from impressions")
    row_kind: str = Field(description="derived from impressions")
    attribution_model_key: str | None = Field(
        default=None,
        description=(
            "assigned by the load · null means: not_applicable_under_options"
        ),
    )
    channel_group_key: str | None = Field(
        default=None,
        description=(
            "assigned by the load · null means: not_applicable_under_options"
        ),
    )
    channel_key: str | None = Field(
        default=None,
        description=(
            "assigned by the load · null means: not_applicable_under_options"
        ),
    )
    ad_product_key: str | None = Field(
        default=None,
        description=(
            "assigned by the load · null means: not_applicable_under_options"
        ),
    )
    campaign_key: str | None = Field(
        default=None,
        description=(
            "assigned by the load · null means: not_applicable_under_options"
        ),
    )
    identity_unit: str = Field(description="literal 'amazon_user_id'")
    cell_state: str = Field(description="assigned by the load")
    shoppers: int | None = Field(
        default=None,
        description=(
            "derived from user_id, impressions · null means: not_returned"
        ),
    )
    purchasers: int | None = Field(
        default=None,
        description=(
            "derived from user_id, event_category · null means: not_returned"
        ),
    )
    ntb_purchasers: int | None = Field(
        default=None,
        description=(
            "derived from user_id, event_category, new_to_brand · null means: not_returned"
        ),
    )
    impressions: int | None = Field(
        default=None,
        description=(
            "derived from impressions · null means: not_returned"
        ),
    )
    dsp_window_spend: Decimal | None = Field(
        default=None,
        description=(
            "derived from total_cost · null means: not_returned"
        ),
        max_digits=38,
        decimal_places=4,
    )
    sponsored_window_spend: Decimal | None = Field(
        default=None,
        description=(
            "derived from spend · null means: not_returned"
        ),
        max_digits=38,
        decimal_places=4,
    )
    purchases: int | None = Field(
        default=None,
        description=(
            "derived from event_category · null means: not_returned"
        ),
    )
    revenue_incl_halo: Decimal | None = Field(
        default=None,
        description=(
            "derived from total_product_sales · null means: not_returned"
        ),
        max_digits=38,
        decimal_places=4,
    )
    dsp_currency: str | None = Field(
        default=None,
        description=(
            "derived from currency_iso_code · null means: not_returned"
        ),
    )
    sponsored_currency: str | None = Field(
        default=None,
        description=(
            "derived from currency_iso_code · null means: not_returned"
        ),
    )
    currency_code: str | None = Field(
        default=None,
        description=(
            "derived from purchase_currency · null means: not_returned"
        ),
    )
    basis_key: str = Field(description="literal 'ad_attributed'")
    source_timezone: str = Field(description="literal 'UTC'")
    ecosystem_key: str = Field(description="governed ecosystem 'amazon'")
    platform_key: str = Field(description="governed platform 'amazon_ads'")


class AmcExposureGroupsRow(BaseModel):
    """One curated row produced by `amc-exposure-groups`.

    Target table: fct_exposure_group_outcome.
    Grain: period_start, period_end, time_grain, grain_set, marketplace_key, exposure_group,
    identity_space, row_kind.
    Measurement basis: ad_attributed.
    Source: amc · dsp_impressions.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    period_start: date = Field(description="assigned by the load")
    period_end: date = Field(description="assigned by the load")
    time_grain: str = Field(description="assigned by the load")
    grain_set: str = Field(description="literal 'platform'")
    marketplace_key: str = Field(description="marketplace_name")
    exposure_group: str = Field(description="assigned by the load")
    exposure_source_set: str = Field(description="derived from impressions")
    identity_space: str = Field(description="derived from user_id_type")
    row_kind: str = Field(description="derived from impressions, event_category")
    channel_group_key: str | None = Field(
        default=None,
        description=(
            "assigned by the load · null means: not_applicable_under_options"
        ),
    )
    channel_key: str | None = Field(
        default=None,
        description=(
            "assigned by the load · null means: not_applicable_under_options"
        ),
    )
    ad_product_key: str | None = Field(
        default=None,
        description=(
            "assigned by the load · null means: not_applicable_under_options"
        ),
    )
    campaign_key: str | None = Field(
        default=None,
        description=(
            "assigned by the load · null means: not_applicable_under_options"
        ),
    )
    identity_unit: str = Field(description="literal 'amazon_user_id'")
    cell_state: str = Field(description="assigned by the load")
    shoppers_reached: int | None = Field(
        default=None,
        description=(
            "derived from user_id, impressions · null means: not_returned"
        ),
    )
    search_shoppers: int | None = Field(
        default=None,
        description=(
            "derived from user_id, event_subtype, event_dt_hour_utc · null means: not_returned"
        ),
    )
    detail_page_view_shoppers: int | None = Field(
        default=None,
        description=(
            "derived from user_id, event_subtype, event_dt_hour_utc · null means: not_returned"
        ),
    )
    purchasers: int | None = Field(
        default=None,
        description=(
            "derived from user_id, event_category · null means: not_returned"
        ),
    )
    ntb_purchasers: int | None = Field(
        default=None,
        description=(
            "derived from user_id, event_category, new_to_brand · null means: not_returned"
        ),
    )
    impressions: int | None = Field(
        default=None,
        description=(
            "derived from impressions · null means: not_returned"
        ),
    )
    dsp_window_spend: Decimal | None = Field(
        default=None,
        description=(
            "derived from total_cost · null means: not_returned"
        ),
        max_digits=38,
        decimal_places=4,
    )
    sponsored_window_spend: Decimal | None = Field(
        default=None,
        description=(
            "derived from spend · null means: not_returned"
        ),
        max_digits=38,
        decimal_places=4,
    )
    purchases: int | None = Field(
        default=None,
        description=(
            "derived from event_category · null means: not_returned"
        ),
    )
    revenue_incl_halo: Decimal | None = Field(
        default=None,
        description=(
            "derived from total_product_sales · null means: not_returned"
        ),
        max_digits=38,
        decimal_places=4,
    )
    dsp_currency: str | None = Field(
        default=None,
        description=(
            "derived from currency_iso_code · null means: not_returned"
        ),
    )
    sponsored_currency: str | None = Field(
        default=None,
        description=(
            "derived from currency_iso_code · null means: not_returned"
        ),
    )
    currency_code: str | None = Field(
        default=None,
        description=(
            "derived from purchase_currency · null means: not_returned"
        ),
    )
    basis_key: str = Field(description="literal 'ad_attributed'")
    source_timezone: str = Field(description="literal 'UTC'")
    ecosystem_key: str = Field(description="governed ecosystem 'amazon'")
    platform_key: str = Field(description="governed platform 'amazon_ads'")


class AmcNewToBrandBuyersCampaignRow(BaseModel):
    """One curated row produced by `amc-new-to-brand-buyers-campaign`.

    Target table: fct_new_to_brand_buyers.
    Grain: period_start, period_end, time_grain, grain_set, marketplace_key, campaign_key.
    Measurement basis: ad_attributed.
    Source: amc · amazon_attributed_events_by_conversion_time.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    period_start: date = Field(description="assigned by the load")
    period_end: date = Field(description="assigned by the load")
    time_grain: str = Field(description="assigned by the load")
    grain_set: str = Field(description="literal 'campaign'")
    marketplace_key: str = Field(description="conversion_event_marketplace_name")
    campaign_key: str = Field(description="campaign_id_string")
    ad_product_key: str = Field(description="ad_product_type")
    channel_group_key: str = Field(description="assigned by the load")
    channel_key: str = Field(description="assigned by the load")
    identity_unit: str = Field(description="literal 'amazon_user_id'")
    cell_state: str = Field(description="assigned by the load")
    buyers_new_to_brand_12mo: int | None = Field(
        default=None,
        description=(
            "derived from new_to_brand, user_id · null means: not_returned"
        ),
    )
    buyers_attributed: int | None = Field(
        default=None,
        description=(
            "derived from user_id · null means: not_returned"
        ),
    )
    basis_key: str = Field(description="literal 'ad_attributed'")
    source_timezone: str = Field(description="literal 'UTC'")
    ecosystem_key: str = Field(description="governed ecosystem 'amazon'")
    platform_key: str = Field(description="governed platform 'amazon_ads'")


class AmcNewToBrandBuyersChannelRow(BaseModel):
    """One curated row produced by `amc-new-to-brand-buyers-channel`.

    Target table: fct_new_to_brand_buyers.
    Grain: period_start, period_end, time_grain, grain_set, marketplace_key, ad_product_key.
    Measurement basis: ad_attributed.
    Source: amc · amazon_attributed_events_by_conversion_time.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    period_start: date = Field(description="assigned by the load")
    period_end: date = Field(description="assigned by the load")
    time_grain: str = Field(description="assigned by the load")
    grain_set: str = Field(description="literal 'channel'")
    marketplace_key: str = Field(description="conversion_event_marketplace_name")
    ad_product_key: str = Field(description="ad_product_type")
    channel_group_key: str = Field(description="assigned by the load")
    channel_key: str = Field(description="assigned by the load")
    campaign_key: str | None = Field(
        default=None,
        description=(
            "assigned by the load · null means: not_applicable_under_options"
        ),
    )
    identity_unit: str = Field(description="literal 'amazon_user_id'")
    cell_state: str = Field(description="assigned by the load")
    buyers_new_to_brand_12mo: int | None = Field(
        default=None,
        description=(
            "derived from new_to_brand, user_id · null means: not_returned"
        ),
    )
    buyers_attributed: int | None = Field(
        default=None,
        description=(
            "derived from user_id · null means: not_returned"
        ),
    )
    basis_key: str = Field(description="literal 'ad_attributed'")
    source_timezone: str = Field(description="literal 'UTC'")
    ecosystem_key: str = Field(description="governed ecosystem 'amazon'")
    platform_key: str = Field(description="governed platform 'amazon_ads'")


class AmcNewToBrandBuyersPlatformRow(BaseModel):
    """One curated row produced by `amc-new-to-brand-buyers-platform`.

    Target table: fct_new_to_brand_buyers.
    Grain: period_start, period_end, time_grain, grain_set, marketplace_key.
    Measurement basis: ad_attributed.
    Source: amc · amazon_attributed_events_by_conversion_time.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    period_start: date = Field(description="assigned by the load")
    period_end: date = Field(description="assigned by the load")
    time_grain: str = Field(description="assigned by the load")
    grain_set: str = Field(description="literal 'platform'")
    marketplace_key: str = Field(description="conversion_event_marketplace_name")
    channel_group_key: str | None = Field(
        default=None,
        description=(
            "assigned by the load · null means: not_applicable_under_options"
        ),
    )
    channel_key: str | None = Field(
        default=None,
        description=(
            "assigned by the load · null means: not_applicable_under_options"
        ),
    )
    ad_product_key: str | None = Field(
        default=None,
        description=(
            "assigned by the load · null means: not_applicable_under_options"
        ),
    )
    campaign_key: str | None = Field(
        default=None,
        description=(
            "assigned by the load · null means: not_applicable_under_options"
        ),
    )
    identity_unit: str = Field(description="literal 'amazon_user_id'")
    cell_state: str = Field(description="assigned by the load")
    buyers_new_to_brand_12mo: int | None = Field(
        default=None,
        description=(
            "derived from new_to_brand, user_id · null means: not_returned"
        ),
    )
    buyers_attributed: int | None = Field(
        default=None,
        description=(
            "derived from user_id · null means: not_returned"
        ),
    )
    basis_key: str = Field(description="literal 'ad_attributed'")
    source_timezone: str = Field(description="literal 'UTC'")
    ecosystem_key: str = Field(description="governed ecosystem 'amazon'")
    platform_key: str = Field(description="governed platform 'amazon_ads'")


class AmcReachFrequencyRow(BaseModel):
    """One curated row produced by `amc-reach-frequency`.

    Target table: fct_ad_exposure.
    Grain: date_key, channel_key, campaign_key, ad_product_key, marketplace_key, rows_suppressed.
    Measurement basis: ad_attributed.
    Source: amc · sponsored_ads_traffic.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    date_key: date = Field(description="event_date_utc")
    ad_product_key: str = Field(description="ad_product_type")
    channel_key: str = Field(description="ad_product_type")
    channel_group_key: str = Field(description="derived from ad_product_type")
    campaign_key: str = Field(description="campaign_id_string")
    marketplace_key: str = Field(description="marketplace_name")
    impressions: int = Field(description="impressions")
    clicks: int = Field(description="clicks")
    spend: Decimal = Field(description="spend", max_digits=38, decimal_places=4)
    currency_code: str = Field(description="currency_iso_code")
    reach_users: int = Field(description="derived from user_id")
    rows_suppressed: bool = Field(description="extraction option, delivered as values_removed")
    basis_key: str = Field(description="literal 'ad_attributed'")
    source_timezone: str = Field(description="literal 'UTC'")
    ecosystem_key: str = Field(description="governed ecosystem 'amazon'")
    platform_key: str = Field(description="governed platform 'amazon_ads'")


class AmcRetailPurchasesRow(BaseModel):
    """One curated row produced by `amc-retail-purchases`.

    Target table: fct_retail_purchase_amc.
    Grain: date_key, amazon_asin, marketplace_key, rows_suppressed.
    Measurement basis: retail_actual.
    Source: amc · amazon_retail_purchases.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    date_key: date = Field(description="purchase_date_utc")
    amazon_asin: str = Field(description="asin")
    marketplace_key: str = Field(description="marketplace_name")
    units_purchased: int = Field(description="purchase_units_sold")
    currency_code: str = Field(description="currency_code")
    revenue_purchased: Decimal = Field(
        description=(
            "derived from unit_price, purchase_units_sold"
        ),
        max_digits=38,
        decimal_places=4,
    )
    rows_suppressed: bool = Field(description="extraction option, delivered as values_removed")
    suppression_reason: str | None = Field(
        default=None,
        description=(
            "extraction option, delivered as filtered_reason · null means: suppressed"
        ),
    )
    distinct_user_count: int | None = Field(
        default=None,
        description=(
            "extraction option, delivered as distinct_user_count · null means: suppressed"
        ),
    )
    basis_key: str = Field(description="literal 'retail_actual'")
    source_timezone: str = Field(description="literal 'UTC'")
    ecosystem_key: str = Field(description="governed ecosystem 'amazon'")
    platform_key: str = Field(description="governed platform 'amazon_retail'")


class AmcSegmentBuyerProfileDspExposedRow(BaseModel):
    """One curated row produced by `amc-segment-buyer-profile-dsp-exposed`.

    Target table: fct_segment_profile.
    Grain: period_start, period_end, time_grain, grain_set, marketplace_key, profile_path,
    segment_namespace, segment_id, row_kind.
    Measurement basis: ad_attributed.
    Source: amc · dsp_impressions_by_user_segments.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    period_start: date = Field(description="assigned by the load")
    period_end: date = Field(description="assigned by the load")
    time_grain: str = Field(description="assigned by the load")
    grain_set: str = Field(description="literal 'platform'")
    marketplace_key: str = Field(description="marketplace_name")
    profile_path: str = Field(description="literal 'dsp_exposed_only'")
    segment_namespace: str = Field(description="literal 'amc_dsp_behavior'")
    segment_id: str | None = Field(
        default=None,
        description=(
            "behavior_segment_id · null means: not_applicable_under_options"
        ),
    )
    row_kind: str = Field(description="derived from user_id")
    channel_group_key: str | None = Field(
        default=None,
        description=(
            "assigned by the load · null means: not_applicable_under_options"
        ),
    )
    channel_key: str | None = Field(
        default=None,
        description=(
            "assigned by the load · null means: not_applicable_under_options"
        ),
    )
    ad_product_key: str | None = Field(
        default=None,
        description=(
            "assigned by the load · null means: not_applicable_under_options"
        ),
    )
    campaign_key: str | None = Field(
        default=None,
        description=(
            "assigned by the load · null means: not_applicable_under_options"
        ),
    )
    identity_unit: str = Field(description="literal 'amazon_user_id'")
    cell_state: str = Field(description="assigned by the load")
    segment_exposed_shoppers: int | None = Field(
        default=None,
        description=(
            "derived from user_id, behavior_segment_id · null means: not_returned"
        ),
    )
    segment_buyers: int | None = Field(
        default=None,
        description=(
            "derived from user_id, event_category · null means: not_returned"
        ),
    )
    segment_buyers_new_to_brand: int | None = Field(
        default=None,
        description=(
            "derived from user_id, new_to_brand · null means: not_returned"
        ),
    )
    profiled_exposed_total: int | None = Field(
        default=None,
        description=(
            "derived from user_id · null means: not_returned"
        ),
    )
    profiled_buyers_total: int | None = Field(
        default=None,
        description=(
            "derived from user_id, event_category · null means: not_returned"
        ),
    )
    basis_key: str = Field(description="literal 'ad_attributed'")
    source_timezone: str = Field(description="literal 'UTC'")
    ecosystem_key: str = Field(description="governed ecosystem 'amazon'")
    platform_key: str = Field(description="governed platform 'amazon_ads'")


class AmcSegmentBuyerProfileInmarketRow(BaseModel):
    """One curated row produced by `amc-segment-buyer-profile-inmarket`.

    Target table: fct_segment_profile.
    Grain: period_start, period_end, time_grain, grain_set, marketplace_key, profile_path,
    segment_namespace, segment_id, row_kind.
    Measurement basis: ad_attributed.
    Source: amc · audience_segments_amer_inmarket_snapshot.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    period_start: date = Field(description="assigned by the load")
    period_end: date = Field(description="assigned by the load")
    time_grain: str = Field(description="assigned by the load")
    grain_set: str = Field(description="literal 'platform'")
    marketplace_key: str = Field(description="marketplace_name")
    profile_path: str = Field(description="literal 'asi_in_market'")
    segment_namespace: str = Field(description="literal 'amc_asi'")
    segment_id: str | None = Field(
        default=None,
        description=(
            "segment_id · null means: not_applicable_under_options"
        ),
    )
    row_kind: str = Field(description="derived from user_id")
    channel_group_key: str | None = Field(
        default=None,
        description=(
            "assigned by the load · null means: not_applicable_under_options"
        ),
    )
    channel_key: str | None = Field(
        default=None,
        description=(
            "assigned by the load · null means: not_applicable_under_options"
        ),
    )
    ad_product_key: str | None = Field(
        default=None,
        description=(
            "assigned by the load · null means: not_applicable_under_options"
        ),
    )
    campaign_key: str | None = Field(
        default=None,
        description=(
            "assigned by the load · null means: not_applicable_under_options"
        ),
    )
    identity_unit: str = Field(description="literal 'amazon_user_id'")
    cell_state: str = Field(description="assigned by the load")
    segment_exposed_shoppers: int | None = Field(
        default=None,
        description=(
            "derived from user_id, segment_id · null means: not_returned"
        ),
    )
    segment_buyers: int | None = Field(
        default=None,
        description=(
            "derived from user_id, event_category · null means: not_returned"
        ),
    )
    segment_buyers_new_to_brand: int | None = Field(
        default=None,
        description=(
            "derived from user_id, new_to_brand · null means: not_returned"
        ),
    )
    profiled_exposed_total: int | None = Field(
        default=None,
        description=(
            "derived from user_id · null means: not_returned"
        ),
    )
    profiled_buyers_total: int | None = Field(
        default=None,
        description=(
            "derived from user_id, event_category · null means: not_returned"
        ),
    )
    basis_key: str = Field(description="literal 'ad_attributed'")
    source_timezone: str = Field(description="literal 'UTC'")
    ecosystem_key: str = Field(description="governed ecosystem 'amazon'")
    platform_key: str = Field(description="governed platform 'amazon_ads'")


class AmcSegmentBuyerProfileLifestyleRow(BaseModel):
    """One curated row produced by `amc-segment-buyer-profile-lifestyle`.

    Target table: fct_segment_profile.
    Grain: period_start, period_end, time_grain, grain_set, marketplace_key, profile_path,
    segment_namespace, segment_id, row_kind.
    Measurement basis: ad_attributed.
    Source: amc · audience_segments_amer_lifestyle_snapshot.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    period_start: date = Field(description="assigned by the load")
    period_end: date = Field(description="assigned by the load")
    time_grain: str = Field(description="assigned by the load")
    grain_set: str = Field(description="literal 'platform'")
    marketplace_key: str = Field(description="marketplace_name")
    profile_path: str = Field(description="literal 'asi_lifestyle'")
    segment_namespace: str = Field(description="literal 'amc_asi'")
    segment_id: str | None = Field(
        default=None,
        description=(
            "segment_id · null means: not_applicable_under_options"
        ),
    )
    row_kind: str = Field(description="derived from user_id")
    channel_group_key: str | None = Field(
        default=None,
        description=(
            "assigned by the load · null means: not_applicable_under_options"
        ),
    )
    channel_key: str | None = Field(
        default=None,
        description=(
            "assigned by the load · null means: not_applicable_under_options"
        ),
    )
    ad_product_key: str | None = Field(
        default=None,
        description=(
            "assigned by the load · null means: not_applicable_under_options"
        ),
    )
    campaign_key: str | None = Field(
        default=None,
        description=(
            "assigned by the load · null means: not_applicable_under_options"
        ),
    )
    identity_unit: str = Field(description="literal 'amazon_user_id'")
    cell_state: str = Field(description="assigned by the load")
    segment_exposed_shoppers: int | None = Field(
        default=None,
        description=(
            "derived from user_id, segment_id · null means: not_returned"
        ),
    )
    segment_buyers: int | None = Field(
        default=None,
        description=(
            "derived from user_id, event_category · null means: not_returned"
        ),
    )
    segment_buyers_new_to_brand: int | None = Field(
        default=None,
        description=(
            "derived from user_id, new_to_brand · null means: not_returned"
        ),
    )
    profiled_exposed_total: int | None = Field(
        default=None,
        description=(
            "derived from user_id · null means: not_returned"
        ),
    )
    profiled_buyers_total: int | None = Field(
        default=None,
        description=(
            "derived from user_id, event_category · null means: not_returned"
        ),
    )
    basis_key: str = Field(description="literal 'ad_attributed'")
    source_timezone: str = Field(description="literal 'UTC'")
    ecosystem_key: str = Field(description="governed ecosystem 'amazon'")
    platform_key: str = Field(description="governed platform 'amazon_ads'")


class AmcSegmentMetadataRow(BaseModel):
    """One curated row produced by `amc-segment-metadata`.

    Target table: dim_audience_segment.
    Grain: platform_key, segment_namespace, segment_id, segment_marketplace_key, valid_from.
    Measurement basis: ad_attributed.
    Source: amc · segment_metadata.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    platform_key: str = Field(description="assigned by the load")
    ecosystem_key: str = Field(description="assigned by the load")
    segment_namespace: str = Field(description="literal 'amc_asi'")
    segment_id: str = Field(description="segment_id")
    segment_marketplace_key: str = Field(description="segment_marketplace_id")
    segment_name: str | None = Field(
        default=None,
        description=(
            "derived from segment_name · null means: not_yet_available"
        ),
    )
    segment_description: str | None = Field(
        default=None,
        description=(
            "derived from segment_description · null means: not_yet_available"
        ),
    )
    category_level_1: str | None = Field(
        default=None,
        description=(
            "derived from category_level_1 · null means: not_yet_available"
        ),
    )
    category_level_2: str | None = Field(
        default=None,
        description=(
            "derived from category_level_2 · null means: not_yet_available"
        ),
    )
    category_path: str | None = Field(
        default=None,
        description=(
            "derived from category_path · null means: not_yet_available"
        ),
    )
    provider: str | None = Field(
        default=None,
        description=(
            "assigned by the load · null means: not_yet_available"
        ),
    )
    valid_from: datetime = Field(description="assigned by the load")
    valid_to: datetime = Field(description="assigned by the load")
    is_current: bool = Field(description="literal 'true'")


class VendorProductHierarchyRow(BaseModel):
    """One curated row produced by `vendor-product-hierarchy`.

    Target table: dim_product.
    Grain: amazon_asin, marketplace_key, valid_from.
    Measurement basis: retail_actual.
    Source: vendor_data_kiosk · analytics_vendorAnalytics_2024_09_30.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    amazon_asin: str = Field(description="asin")
    amazon_parent_asin: str | None = Field(
        default=None,
        description=(
            "parentAsin · null means: unsupported_by_channel"
        ),
    )
    marketplace_key: str = Field(description="marketplaceId")
    product_title: str | None = Field(
        default=None,
        description=(
            "productTitle · null means: not_yet_available"
        ),
    )
    brand: str | None = Field(default=None, description="brand · null means: not_yet_available")
    brand_code: str | None = Field(
        default=None,
        description=(
            "brandCode · null means: not_yet_available"
        ),
    )
    valid_from: datetime = Field(description="assigned by the load")
    valid_to: datetime = Field(description="assigned by the load")
    is_current: bool = Field(description="literal 'true'")


class VendorSalesByAsinRow(BaseModel):
    """One curated row produced by `vendor-sales-by-asin`.

    Target table: fct_retail_sales.
    Grain: amazon_asin, date_key, marketplace_key, distributor_view, selling_program.
    Measurement basis: retail_actual.
    Source: vendor_central · GET_VENDOR_SALES_REPORT.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    amazon_asin: str = Field(description="salesByAsin[].asin")
    date_key: date = Field(description="salesByAsin[].startDate")
    marketplace_key: str = Field(description="reportSpecification.marketplaceIds[0]")
    report_type: str = Field(description="reportSpecification.reportType")
    report_period: str = Field(description="reportSpecification.reportOptions.reportPeriod")
    period_start: date = Field(description="reportSpecification.dataStartTime")
    period_end: date = Field(description="reportSpecification.dataEndTime")
    distributor_view: str = Field(description="reportSpecification.reportOptions.distributorView")
    selling_program: str = Field(description="reportSpecification.reportOptions.sellingProgram")
    report_options_hash: str = Field(description="reportSpecification.reportOptions")
    source_watermark: date = Field(description="reportSpecification.lastUpdatedDate")
    retrieved_at: datetime = Field(description="manifest.retrieved_at")
    revenue_shipped: Decimal = Field(
        description=(
            "salesByAsin[].shippedRevenue.amount"
        ),
        max_digits=38,
        decimal_places=4,
    )
    currency_code: str = Field(description="salesByAsin[].shippedRevenue.currencyCode")
    cogs_shipped: Decimal = Field(
        description=(
            "salesByAsin[].shippedCogs.amount"
        ),
        max_digits=38,
        decimal_places=4,
    )
    revenue_ordered: Decimal | None = Field(
        default=None,
        description=(
            "salesByAsin[].orderedRevenue.amount · null means: not_applicable_under_options"
        ),
        max_digits=38,
        decimal_places=4,
    )
    units_shipped: int = Field(description="salesByAsin[].shippedUnits")
    units_ordered: int | None = Field(
        default=None,
        description=(
            "salesByAsin[].orderedUnits · null means: not_applicable_under_options"
        ),
    )
    customer_returns: int | None = Field(
        default=None,
        description=(
            "salesByAsin[].customerReturns · null means: not_applicable_under_options"
        ),
    )
    basis_key: str = Field(description="literal 'retail_actual'")
    source_timezone: str = Field(description="literal 'PST'")
    ecosystem_key: str = Field(description="governed ecosystem 'amazon'")
    platform_key: str = Field(description="governed platform 'amazon_retail'")


class VendorSalesReportedTotalRow(BaseModel):
    """One curated row produced by `vendor-sales-reported-total`.

    Target table: fct_retail_sales_reported_total.
    Grain: date_key, marketplace_key, distributor_view, selling_program.
    Measurement basis: retail_actual.
    Source: vendor_central · GET_VENDOR_SALES_REPORT.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    date_key: date = Field(description="salesAggregate[].startDate")
    marketplace_key: str = Field(description="reportSpecification.marketplaceIds[0]")
    report_type: str = Field(description="reportSpecification.reportType")
    report_period: str = Field(description="reportSpecification.reportOptions.reportPeriod")
    period_start: date = Field(description="reportSpecification.dataStartTime")
    period_end: date = Field(description="reportSpecification.dataEndTime")
    distributor_view: str = Field(description="reportSpecification.reportOptions.distributorView")
    selling_program: str = Field(description="reportSpecification.reportOptions.sellingProgram")
    report_options_hash: str = Field(description="reportSpecification.reportOptions")
    source_watermark: date = Field(description="reportSpecification.lastUpdatedDate")
    retrieved_at: datetime = Field(description="manifest.retrieved_at")
    revenue_shipped: Decimal = Field(
        description=(
            "salesAggregate[].shippedRevenue.amount"
        ),
        max_digits=38,
        decimal_places=4,
    )
    currency_code: str = Field(description="salesAggregate[].shippedRevenue.currencyCode")
    cogs_shipped: Decimal = Field(
        description=(
            "salesAggregate[].shippedCogs.amount"
        ),
        max_digits=38,
        decimal_places=4,
    )
    revenue_ordered: Decimal | None = Field(
        default=None,
        description=(
            "salesAggregate[].orderedRevenue.amount · null means: not_applicable_under_options"
        ),
        max_digits=38,
        decimal_places=4,
    )
    units_shipped: int = Field(description="salesAggregate[].shippedUnits")
    units_ordered: int | None = Field(
        default=None,
        description=(
            "salesAggregate[].orderedUnits · null means: not_applicable_under_options"
        ),
    )
    customer_returns: int | None = Field(
        default=None,
        description=(
            "salesAggregate[].customerReturns · null means: not_applicable_under_options"
        ),
    )
    basis_key: str = Field(description="literal 'retail_actual'")
    source_timezone: str = Field(description="literal 'PST'")
    ecosystem_key: str = Field(description="governed ecosystem 'amazon'")
    platform_key: str = Field(description="governed platform 'amazon_retail'")


class VendorTrafficByAsinRow(BaseModel):
    """One curated row produced by `vendor-traffic-by-asin`.

    Target table: fct_product_page_views.
    Grain: amazon_asin, date_key, marketplace_key.
    Measurement basis: retail_actual.
    Source: vendor_central · GET_VENDOR_TRAFFIC_REPORT.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    amazon_asin: str = Field(description="trafficByAsin[].asin")
    date_key: date = Field(description="trafficByAsin[].startDate")
    marketplace_key: str = Field(description="reportSpecification.marketplaceIds[0]")
    report_type: str = Field(description="reportSpecification.reportType")
    report_period: str = Field(description="reportSpecification.reportOptions.reportPeriod")
    period_start: date = Field(description="reportSpecification.dataStartTime")
    period_end: date = Field(description="reportSpecification.dataEndTime")
    report_options_hash: str = Field(description="reportSpecification.reportOptions")
    source_watermark: date = Field(description="reportSpecification.lastUpdatedDate")
    retrieved_at: datetime = Field(description="manifest.retrieved_at")
    glance_views: int = Field(description="trafficByAsin[].glanceViews")
    basis_key: str = Field(description="literal 'retail_actual'")
    source_timezone: str = Field(description="literal 'PST'")
    ecosystem_key: str = Field(description="governed ecosystem 'amazon'")
    platform_key: str = Field(description="governed platform 'amazon_retail'")


class VendorTrafficReportedTotalRow(BaseModel):
    """One curated row produced by `vendor-traffic-reported-total`.

    Target table: fct_product_page_views_reported_total.
    Grain: date_key, marketplace_key.
    Measurement basis: retail_actual.
    Source: vendor_central · GET_VENDOR_TRAFFIC_REPORT.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    date_key: date = Field(description="trafficAggregate[].startDate")
    marketplace_key: str = Field(description="reportSpecification.marketplaceIds[0]")
    report_type: str = Field(description="reportSpecification.reportType")
    report_period: str = Field(description="reportSpecification.reportOptions.reportPeriod")
    period_start: date = Field(description="reportSpecification.dataStartTime")
    period_end: date = Field(description="reportSpecification.dataEndTime")
    report_options_hash: str = Field(description="reportSpecification.reportOptions")
    source_watermark: date = Field(description="reportSpecification.lastUpdatedDate")
    retrieved_at: datetime = Field(description="manifest.retrieved_at")
    glance_views: int = Field(description="trafficAggregate[].glanceViews")
    basis_key: str = Field(description="literal 'retail_actual'")
    source_timezone: str = Field(description="literal 'PST'")
    ecosystem_key: str = Field(description="governed ecosystem 'amazon'")
    platform_key: str = Field(description="governed platform 'amazon_retail'")
