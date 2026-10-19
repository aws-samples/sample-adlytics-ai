# Data Layer — architecture

## 4. Internal structure

Semantic V2 keeps five distinct immutable layers: retained raw manifest-bound bytes, source-aligned interpretation, canonical candidate evidence, successful current publications, and isolated query results. Governed declarations define source interpretation and canonical meaning; they do not select authority.

### Canonical bus matrix

| Fact | Domain | Date | Channel | Marketplace | Product | Campaign | Ad product | Treatment | Basis |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `fct_ad_exposure` | exposure | ✓ | ✓ | ✓ | — | ✓ | ✓ | — | ✓ |
| `fct_ad_exposure_withheld` | exposure | ✓ | ✓ | ✓ | — | ✓ | ✓ | — | ✓ |
| `fct_ad_exposure_hourly` | exposure | ✓ | — | — | — | ✓ | — | — | ✓ |
| `fct_ad_exposure_by_product` | exposure | ✓ | — | — | ✓ | ✓ | — | — | ✓ |
| `fct_campaign_pacing` | exposure | ✓ | — | — | — | ✓ | — | — | ✓ |
| `fct_campaign_hourly_cost` | exposure | ✓ | — | — | — | ✓ | — | — | ✓ |
| `fct_campaign_change_event` | exposure | ✓ | — | — | — | ✓ | — | — | ✓ |
| `snap_campaign_budget_state` | exposure | ✓ | — | — | — | ✓ | — | — | ✓ |
| `snap_budget_pool_state` | exposure | ✓ | — | — | — | — | — | — | ✓ |
| `snap_budget_settings` | exposure | ✓ | — | ✓ | — | — | — | — | ✓ |
| `snap_budget_usage` | exposure | ✓ | — | — | — | ✓ | — | — | ✓ |
| `snap_budget_rule_association` | exposure | ✓ | — | — | — | ✓ | — | — | ✓ |
| `snap_campaign_delivery_settings` | exposure | ✓ | — | — | — | ✓ | — | — | ✓ |
| `snap_campaign_fee_line` | exposure | ✓ | — | — | — | ✓ | — | — | ✓ |
| `snap_platform_forecast_vintage` | exposure | ✓ | — | — | — | ✓ | — | — | ✓ |
| `snap_platform_forecast_curve_point` | exposure | ✓ | — | — | — | ✓ | — | — | ✓ |
| `fct_attributed_conversion` | conversion | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | — | ✓ |
| `fct_attributed_conversion_withheld` | conversion | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | — | ✓ |
| `fct_deduplicated_temporal_attribution` | conversion | ✓ | ✓ | ✓ | — | — | ✓ | — | ✓ |
| `fct_deduplicated_temporal_attribution_withheld` | conversion | ✓ | ✓ | ✓ | — | — | ✓ | — | ✓ |
| `fct_conversion_path` | conversion | ✓ | ✓ | — | — | — | — | — | ✓ |
| `fct_attributed_conversion_by_traffic_date` | conversion | ✓ | ✓ | ✓ | — | ✓ | ✓ | — | ✓ |
| `fct_product_page_views` | retail_demand | ✓ | — | ✓ | ✓ | — | — | — | ✓ |
| `fct_retail_purchase_amc_withheld` | retail_demand | ✓ | — | ✓ | ✓ | — | — | — | ✓ |
| `snap_promotion_status` | retail_demand | ✓ | — | — | — | — | — | — | ✓ |
| `snap_product_availability` | retail_availability | ✓ | — | ✓ | ✓ | — | — | — | ✓ |
| `fct_new_to_brand_buyers` | shopper_lifecycle | ✓ | — | ✓ | — | — | — | — | ✓ |
| `fct_exposure_group_outcome` | shopper_lifecycle | ✓ | — | ✓ | — | — | — | — | ✓ |
| `fct_exposure_group_outcome_withheld` | shopper_lifecycle | ✓ | — | ✓ | — | — | — | — | ✓ |
| `fct_exposure_frequency` | shopper_lifecycle | ✓ | — | ✓ | — | — | — | — | ✓ |
| `fct_exposure_frequency_withheld` | shopper_lifecycle | ✓ | — | ✓ | — | — | — | — | ✓ |
| `fct_segment_delivery` | exposure | ✓ | ✓ | ✓ | — | ✓ | ✓ | — | ✓ |
| `fct_segment_delivery_withheld` | exposure | ✓ | ✓ | ✓ | — | — | ✓ | — | ✓ |
| `fct_segment_profile` | exposure | ✓ | — | ✓ | — | — | — | — | ✓ |
| `fct_shopper_acquisition` | shopper_lifecycle | ✓ | — | ✓ | — | — | — | — | ✓ |
| `fct_shopper_acquisition_withheld` | shopper_lifecycle | ✓ | — | ✓ | — | — | — | — | ✓ |
| `fct_repeat_purchase_reported` | shopper_lifecycle | ✓ | — | ✓ | ✓ | — | — | — | ✓ |
| `fct_incremental_effect` | incrementality | ✓ | ✓ | — | ✓ | — | — | ✓ | ✓ |
| `fct_lift_test_result` | incrementality | ✓ | — | — | — | — | — | — | ✓ |
| `fct_cross_channel_performance` | cross_channel_performance | ✓ | ✓ | ✓ | ✓ | — | — | — | ✓ |

One verified parse feeds both source-aligned materialization and canonical candidate mapping. Candidate records preserve source versions/checksums, identity, grain, basis, units, exact buckets, and typed suppression, mapping, DQ, and control evidence.

## 5. Key runtime flows

1. A manifest binds every source object to an exact VersionId, byte count, and checksum.
2. One parse materializes source-native evidence and feeds canonical mapping.
3. The candidate holds the complete immutable unit of judgment for one publication scope.
4. Load Path validates and publishes immutable successful bytes.
5. Publication State alone decides which successes become readable through `CURRENT`.
6. Query Path reads only an attested selected set; a blocker may retain evidence but cannot create authority.
