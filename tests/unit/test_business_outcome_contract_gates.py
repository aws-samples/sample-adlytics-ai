# SPDX-License-Identifier: MIT-0
"""Contract gates AC-BR.9 and AC-BR.10 (business-outcomes slice S0.10).

Three rules in `domain.data_contracts.validation`, which `make validate-contracts` runs per
contract:

- `no-canonical-taxonomy-key-delivered`: a pack emits source labels, never a canonical key.
- `attributed-additive-needs-campaign-grain`: no additive sum from the attributed-events
  tables at a grain without `campaign_id_string`.
- `no-source-reported-ratio`: a ratio or share the source computed never maps to a curated
  column.

Each refusal asserts the exact rule, severity and contract, with a paired control that passes.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from adapters.jobs.contract_validation.loader import load_contracts, load_registry
from domain.data_contracts.contract import DataContract, FieldMapping, SourceFormat
from domain.data_contracts.validation import (
    EXISTING_DELIVERED_TAXONOMY_KEYS,
    ColumnRegistry,
    Finding,
    RegisteredColumn,
    Severity,
    validate,
)
from domain.shared_kernel.provenance import MeasurementBasis

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-099", "ADR-105")]

ROOT = Path(__file__).resolve().parents[2]
ATTRIBUTED = "amazon_attributed_events_by_conversion_time"
TAXONOMY_RULE = "no-canonical-taxonomy-key-delivered"
ATTRIBUTED_RULE = "attributed-additive-needs-campaign-grain"
RATIO_RULE = "no-source-reported-ratio"


_DIMENSION = RegisteredColumn(
    name="", table="", threshold="LOW", source_type="STRING", kind="DIMENSION"
)
_METRIC = replace(_DIMENSION, source_type="LONG", kind="METRIC")


REGISTRY = ColumnRegistry(
    columns={
        (column.table, column.name): column
        for column in (
            replace(_DIMENSION, table=ATTRIBUTED, name="ad_product_type"),
            replace(_DIMENSION, table=ATTRIBUTED, name="campaign_id_string"),
            replace(_DIMENSION, table=ATTRIBUTED, name="user_id", threshold="VERY_HIGH"),
            replace(_METRIC, table=ATTRIBUTED, name="total_purchases"),
            replace(_METRIC, table=ATTRIBUTED, name="total_product_sales", source_type="DECIMAL"),
            replace(
                _METRIC, table="conversions", name="total_product_sales", source_type="DECIMAL"
            ),
            replace(_DIMENSION, table="conversions", name="user_id", threshold="VERY_HIGH"),
            replace(
                _DIMENSION,
                table="dsp_impressions",
                name="placement_view_rate",
                source_type="DECIMAL",
            ),
        )
    },
    known_tables=frozenset({ATTRIBUTED, "conversions", "dsp_impressions"}),
    inherits={},
)


def _amc(
    *fields: FieldMapping, grain: tuple[str, ...] = ("grain_set",), **overrides: object
) -> DataContract:
    """Build a minimal AMC contract with a literal grain, so each test adds only what it tests."""
    defaults: dict[str, object] = {
        "contract_id": "test-shopper-pack",
        "contract_version": 1,
        "source_system": "amc",
        "source_dataset": ATTRIBUTED,
        "source_format": SourceFormat.CSV,
        "target_table": "fct_test_outcome",
        "grain": grain,
        "partition_keys": ("grain_set",),
        "measurement_basis": MeasurementBasis.AD_ATTRIBUTED,
        "source_timezone": "UTC",
        "ecosystem_key": "amazon",
        "platform_key": "amazon_ads",
        "fields": (
            FieldMapping(target_column="grain_set", target_type="string", literal_value="platform"),
            *fields,
        ),
        "restatement_window_days": 30,
        "staleness_threshold_hours": 72,
        "source_tables": (ATTRIBUTED,),
        "grain_feasibility_note": "Every output column is NONE or LOW.",
    }
    return DataContract(**(defaults | overrides))  # type: ignore[arg-type]


def _findings(
    contract: DataContract, rule: str, registry: ColumnRegistry = REGISTRY
) -> list[Finding]:
    return [finding for finding in validate(contract, registry) if finding.rule == rule]


def _assert_refused(findings: list[Finding], message_fragment: str) -> None:
    assert len(findings) == 1
    finding = findings[0]
    assert finding.severity is Severity.ERROR
    assert finding.contract_id == "test-shopper-pack"
    assert message_fragment in finding.message


class TestNoCanonicalTaxonomyKeyIsDelivered:
    def test_a_pack_deriving_channel_group_key_in_sql_is_refused(self) -> None:
        contract = _amc(
            FieldMapping(
                source_path="CASE WHEN ad_product_type = 'sponsored_products' THEN 'search' END",
                target_column="channel_group_key",
                target_type="string",
                derived_from=("ad_product_type",),
            )
        )

        _assert_refused(_findings(contract, TAXONOMY_RULE), "carries 'channel_group_key'")

    def test_a_pack_emitting_an_exposure_group_column_is_refused(self) -> None:
        contract = _amc(
            FieldMapping(
                source_path="exposure_group", target_column="exposure_group", target_type="string"
            )
        )

        _assert_refused(_findings(contract, TAXONOMY_RULE), "carries 'exposure_group'")

    def test_a_source_label_or_a_stamped_literal_is_not_refused(self) -> None:
        """Paired control: the governed convention maps `ad_product_type` and stamps literals."""
        contract = _amc(
            FieldMapping(source_path="ad_product_type", target_column="ad_product_key"),
            FieldMapping(
                target_column="channel_key", target_type="string", literal_value="amazon_dsp"
            ),
            FieldMapping(
                source_path="exposure_source_set",
                target_column="exposure_source_set",
                target_type="string",
            ),
        )

        assert _findings(contract, TAXONOMY_RULE) == []

    def test_the_named_existing_derivations_are_exactly_the_shipped_ones(self) -> None:
        """The exception list is load-bearing and complete: no stale entry, no hidden one."""
        registry = load_registry(ROOT)
        contracts = {contract.contract_id: contract for _, contract in load_contracts(ROOT)}
        delivered = {
            (contract.contract_id, contract.contract_version, mapping.delivered_name)
            for contract in contracts.values()
            for mapping in contract.fields
            if not mapping.has_no_delivered_value and mapping.delivered_name == "channel_group_key"
        }

        assert delivered == EXISTING_DELIVERED_TAXONOMY_KEYS
        for contract in contracts.values():
            assert _findings(contract, TAXONOMY_RULE, registry) == []

    def test_a_new_version_of_an_excepted_contract_loses_the_exception(self) -> None:
        contracts = {contract.contract_id: contract for _, contract in load_contracts(ROOT)}
        next_version = replace(contracts["amc-attributed-conversion"], contract_version=2)

        findings = _findings(next_version, TAXONOMY_RULE, load_registry(ROOT))

        assert [(f.rule, f.severity, f.contract_id) for f in findings] == [
            (TAXONOMY_RULE, Severity.ERROR, "amc-attributed-conversion")
        ]


class TestAttributedSumsNeedCampaignGrain:
    def test_summing_attributed_revenue_at_platform_grain_is_refused(self) -> None:
        contract = _amc(
            FieldMapping(
                source_path="total_product_sales",
                target_column="revenue",
                target_type="decimal(38,4)",
            )
        )

        _assert_refused(
            _findings(contract, ATTRIBUTED_RULE),
            f"sums {ATTRIBUTED}.total_product_sales at a grain without campaign_id_string",
        )

    def test_a_derived_sum_of_an_attributed_metric_is_refused(self) -> None:
        contract = _amc(
            FieldMapping(
                source_path="SUM(total_purchases)",
                target_column="purchases",
                target_type="bigint",
                derived_from=("total_purchases",),
            )
        )

        _assert_refused(_findings(contract, ATTRIBUTED_RULE), f"{ATTRIBUTED}.total_purchases")

    def test_the_same_sum_with_campaign_in_the_grain_is_not_refused(self) -> None:
        """Paired control: within one campaign the credited rows don't overlap."""
        contract = _amc(
            FieldMapping(source_path="campaign_id_string", target_column="campaign_key"),
            FieldMapping(
                source_path="total_product_sales",
                target_column="revenue",
                target_type="decimal(38,4)",
            ),
            grain=("grain_set", "campaign_key"),
        )

        assert _findings(contract, ATTRIBUTED_RULE) == []

    def test_a_distinct_count_at_platform_grain_is_not_refused(self) -> None:
        """Packs 1-3 count new-to-brand buyers from this table at platform grain, by design."""
        contract = _amc(
            FieldMapping(
                source_path="COUNT(DISTINCT CASE WHEN total_purchases > 0 THEN user_id END)",
                target_column="buyers_attributed",
                target_type="bigint",
                derived_from=("user_id", "total_purchases"),
                non_additive=True,
            )
        )

        assert _findings(contract, ATTRIBUTED_RULE) == []

    def test_a_shared_column_qualified_to_conversions_is_not_refused(self) -> None:
        """Pack 14 shape: the cohort anchor is attributed, repeat revenue comes from conversions."""
        two_tables = (ATTRIBUTED, "conversions")
        qualified = _amc(
            FieldMapping(
                source_path="SUM(c.total_product_sales)",
                target_column="repeat_revenue",
                target_type="decimal(38,4)",
                derived_from=("conversions.total_product_sales",),
            ),
            source_tables=two_tables,
        )
        unqualified = _amc(
            FieldMapping(
                source_path="SUM(c.total_product_sales)",
                target_column="repeat_revenue",
                target_type="decimal(38,4)",
                derived_from=("total_product_sales",),
            ),
            source_tables=two_tables,
        )

        assert _findings(qualified, ATTRIBUTED_RULE) == []
        assert [
            f.rule for f in validate(qualified, REGISTRY) if f.rule == "amc-column-verified"
        ] == []
        _assert_refused(_findings(unqualified, ATTRIBUTED_RULE), "Qualify a shared column")

    def test_a_qualified_input_on_a_table_the_contract_does_not_read_is_unverified(self) -> None:
        contract = _amc(
            FieldMapping(
                source_path="SUM(total_product_sales)",
                target_column="repeat_revenue",
                target_type="decimal(38,4)",
                derived_from=("conversions.total_product_sales",),
            )
        )

        assert "amc-column-verified" in {
            f.rule for f in validate(contract, REGISTRY) if f.severity is Severity.ERROR
        }


class TestNoSourceReportedRatioIsMapped:
    def _vendor(self, *fields: FieldMapping) -> DataContract:
        return DataContract(
            contract_id="test-shopper-pack",
            contract_version=1,
            source_system="vendor_central",
            source_dataset="GET_BRAND_ANALYTICS_REPEAT_PURCHASE_REPORT",
            source_format=SourceFormat.JSON_DUAL_ARRAY,
            target_table="fct_test_repeat",
            grain=("amazon_asin",),
            partition_keys=("amazon_asin",),
            measurement_basis=MeasurementBasis.RETAIL_ACTUAL,
            source_timezone="UTC",
            ecosystem_key="amazon",
            platform_key="amazon_retail",
            fields=(
                FieldMapping(
                    source_path="dataByAsin[].asin",
                    target_column="amazon_asin",
                    target_type="string",
                ),
                *fields,
            ),
            restatement_window_days=30,
            staleness_threshold_hours=72,
        )

    def test_a_reported_share_is_refused_even_under_a_level_sounding_name(self) -> None:
        contract = self._vendor(
            FieldMapping(
                source_path="dataByAsin[].repeatCustomersPctTotal",
                target_column="repeat_customers",
                target_type="decimal(38,4)",
            )
        )

        _assert_refused(_findings(contract, RATIO_RULE), "source-reported ratio or share (pct)")

    def test_an_amc_rate_column_is_refused(self) -> None:
        contract = _amc(
            FieldMapping(source_path="placement_view_rate", target_column="viewability"),
            source_tables=("dsp_impressions",),
            source_dataset="dsp_impressions",
        )

        _assert_refused(_findings(contract, RATIO_RULE), "(rate)")

    def test_levels_from_the_same_report_are_not_refused(self) -> None:
        """Paired control: orders and repeat revenue are levels and map to the curated fact."""
        contract = self._vendor(
            FieldMapping(
                source_path="dataByAsin[].orders", target_column="orders", target_type="bigint"
            ),
            FieldMapping(
                source_path="dataByAsin[].repeatPurchaseRevenue.amount",
                target_column="repeat_purchase_revenue",
                target_type="decimal(38,4)",
            ),
        )

        assert _findings(contract, RATIO_RULE) == []

    def test_every_shipped_contract_maps_levels_only(self) -> None:
        registry = load_registry(ROOT)

        for _, contract in load_contracts(ROOT):
            assert _findings(contract, RATIO_RULE, registry) == []
