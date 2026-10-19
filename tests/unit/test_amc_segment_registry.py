# SPDX-License-Identifier: MIT-0
"""The five AMC segment tables are transcribed, and the ASI segment marketplace is pinned.

Business-outcomes slice S0.6. The registry is an allowlist, so until these tables were
transcribed the contract gate refused any contract that read them (`amc-table-transcribed`).
These tests read the real `contracts/shared/amc-columns.yaml` and the real
`contracts/shared/commerce-identities.yaml`, because the facts under test are the governed
entries themselves, not the gate's logic (which `test_data_contracts.py` owns).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import cast

import pytest
import yaml

from adapters.jobs.contract_validation.loader import load_registry
from adapters.outbound.commerce_identity import load_commerce_identity_bridge
from domain.data_contracts.contract import DataContract, FieldMapping, SourceFormat
from domain.data_contracts.validation import Severity, validate
from domain.semantics.commerce_identity import (
    ResolveCommerceIdentityRequest,
    resolve_commerce_identity,
)
from domain.semantics.identity import IdentityResolutionError, MarketplaceKey, SourceIdentifier
from domain.shared_kernel.provenance import MeasurementBasis

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-103")]

ROOT = Path(__file__).resolve().parents[2]
REGISTRY_PATH = ROOT / "contracts" / "shared" / "amc-columns.yaml"

ASI_TABLES = tuple(
    f"audience_segments_{region}_{category}{view}"
    for region in ("amer", "apac", "eu")
    for category in ("inmarket", "lifestyle")
    for view in ("", "_snapshot")
)
DSP_SEGMENT_TABLES = ("dsp_impressions_by_matched_segments", "dsp_impressions_by_user_segments")
ASI_COLUMNS = {
    "no_3p_trackers": ("BOOLEAN", "NONE"),
    "segment_id": ("INTEGER", "LOW"),
    "segment_marketplace_id": ("LONG", "LOW"),
    "segment_name": ("STRING", "LOW"),
    "snapshot_datetime": ("DATE", "LOW"),
    "user_id": ("STRING", "VERY_HIGH"),
    "user_id_type": ("STRING", "LOW"),
}


SEGMENT_METADATA_COLUMNS = {
    "category_level_1": ("STRING", "LOW"),
    "category_level_2": ("STRING", "LOW"),
    "category_path": ("STRING", "LOW"),
    "no_3p_trackers": ("BOOLEAN", "NONE"),
    "segment_description": ("STRING", "LOW"),
    "segment_id": ("INTEGER", "LOW"),
    "segment_marketplace_id": ("LONG", "LOW"),
    "segment_name": ("STRING", "LOW"),
}
BEHAVIOR_SEGMENT_COLUMNS = {
    "behavior_segment_description": ("STRING", "LOW", "DIMENSION"),
    "behavior_segment_id": ("INTEGER", "LOW", "DIMENSION"),
    "behavior_segment_matched": ("LONG", "NONE", "METRIC"),
    "behavior_segment_name": ("STRING", "LOW", "DIMENSION"),
}


def _raw_registry() -> dict[str, object]:
    return cast("dict[str, object]", yaml.safe_load(REGISTRY_PATH.read_text()))


def _raw_tables() -> dict[str, dict[str, object]]:
    return cast("dict[str, dict[str, object]]", _raw_registry()["tables"])


def _segment_contract(table: str, *fields: FieldMapping) -> DataContract:
    """Build a minimal AMC contract over one segment table, so a test states only what it reads."""
    return DataContract(
        contract_id="test-segment-contract",
        contract_version=1,
        source_system="amc",
        source_dataset=table,
        source_format=SourceFormat.CSV,
        target_table="dim_test_segment",
        grain=("segment_id",),
        partition_keys=("segment_id",),
        measurement_basis=MeasurementBasis.AD_ATTRIBUTED,
        source_timezone="UTC",
        ecosystem_key="amazon",
        platform_key="amazon_ads",
        fields=fields,
        restatement_window_days=30,
        staleness_threshold_hours=72,
        source_tables=(table,),
        grain_feasibility_note="Every output column is NONE or LOW.",
    )


def _errors(contract: DataContract) -> set[tuple[str, str]]:
    return {
        (finding.rule, finding.message)
        for finding in validate(contract, load_registry(ROOT))
        if finding.severity is Severity.ERROR
    }


def test_every_segment_table_is_transcribed_and_no_longer_listed_as_pending() -> None:
    raw = _raw_registry()
    tables = _raw_tables()
    pending = cast("list[str]", raw["not_yet_transcribed"])

    for table in (*ASI_TABLES, *DSP_SEGMENT_TABLES, "segment_metadata"):
        assert table in tables, table
        assert table not in pending, table
    # The unqualified names the registry used to list don't exist in AMC, so they must not
    # survive as table entries either (design §5.3, "fix the names on the way in").
    assert "audience_segments_inmarket" not in tables
    assert "audience_segments_lifestyle" not in tables
    # The two brand-store tables were never in scope for this slice and stay pending.
    assert pending == ["amazon_brand_store_page_views", "amazon_brand_store_engagement_events"]


def test_asi_tables_and_segment_metadata_are_paid_and_the_dsp_segment_tables_are_not() -> None:
    tables = _raw_tables()

    for table in (*ASI_TABLES, "segment_metadata"):
        assert tables[table].get("paid_feature") is True, table
        assert tables[table]["source_page"] == "datasources/audience_segment_membership_paid.md"
    for table in DSP_SEGMENT_TABLES:
        assert "paid_feature" not in tables[table], table
        assert tables[table]["source_page"] == "datasources/dsp_impressions_by_segments.md"


def test_every_asi_membership_table_carries_the_published_types_and_thresholds() -> None:
    registry = load_registry(ROOT)

    for table in ASI_TABLES:
        for column, (source_type, threshold) in ASI_COLUMNS.items():
            found = registry.lookup(table=table, column=column)
            assert found is not None, (table, column)
            assert (found.source_type, found.threshold) == (source_type, threshold)


def test_segment_metadata_carries_exactly_the_published_eight_columns() -> None:
    """Review A-4: every `segment_metadata` column, not only the shared membership ones."""
    registry = load_registry(ROOT)
    tables = _raw_tables()

    assert set(cast("dict[str, object]", tables["segment_metadata"]["columns"])) == set(
        SEGMENT_METADATA_COLUMNS
    )
    for column, (source_type, threshold) in SEGMENT_METADATA_COLUMNS.items():
        found = registry.lookup(table="segment_metadata", column=column)
        assert found is not None, column
        assert (found.table, found.source_type, found.threshold, found.kind) == (
            "segment_metadata",
            source_type,
            threshold,
            "DIMENSION",
        ), column


def test_dsp_segment_tables_add_four_columns_and_inherit_the_dsp_impressions_surface() -> None:
    registry = load_registry(ROOT)
    tables = _raw_tables()

    for table in DSP_SEGMENT_TABLES:
        # Review A-4: all four own columns, with type, threshold and kind.
        assert set(cast("dict[str, object]", tables[table]["columns"])) == set(
            BEHAVIOR_SEGMENT_COLUMNS
        ), table
        for column, (source_type, threshold, kind) in BEHAVIOR_SEGMENT_COLUMNS.items():
            found = registry.lookup(table=table, column=column)
            assert found is not None, (table, column)
            assert (found.table, found.source_type, found.threshold, found.kind) == (
                table,
                source_type,
                threshold,
                kind,
            ), (table, column)
        inherited = registry.lookup(table=table, column="total_cost")
        # Inherited through `inherits_columns_from`, with the money scale that comes with it.
        assert inherited is not None
        assert inherited.table == "dsp_impressions"
        assert inherited.scale_divisor == 100_000


def test_a_contract_reading_an_asi_column_passes_the_allowlist_gate() -> None:
    contract = _segment_contract(
        "audience_segments_amer_inmarket",
        FieldMapping(source_path="segment_id", target_column="segment_id"),
        FieldMapping(source_path="segment_marketplace_id", target_column="segment_marketplace_id"),
    )

    assert _errors(contract) == set()


def test_a_contract_reading_an_untranscribed_asi_column_is_refused_by_the_registry_gate() -> None:
    contract = _segment_contract(
        "audience_segments_amer_inmarket",
        FieldMapping(source_path="segment_id", target_column="segment_id"),
        FieldMapping(source_path="segment_category", target_column="segment_category"),
    )

    errors = _errors(contract)

    assert {rule for rule, _ in errors} == {"amc-column-verified", "target-type-resolves"}
    assert any("'segment_category' is not in the registry" in message for _, message in errors)


def test_an_asi_user_id_in_final_output_is_refused_as_not_outputtable() -> None:
    contract = _segment_contract(
        "audience_segments_eu_lifestyle_snapshot",
        FieldMapping(source_path="segment_id", target_column="segment_id"),
        FieldMapping(source_path="user_id", target_column="user_id"),
    )

    assert {rule for rule, _ in _errors(contract)} == {"amc-column-outputtable"}


def test_the_old_unqualified_table_name_is_refused_as_untranscribed() -> None:
    contract = _segment_contract(
        "audience_segments_inmarket",
        FieldMapping(source_path="segment_id", target_column="segment_id"),
    )

    assert "amc-table-transcribed" in {rule for rule, _ in _errors(contract)}


def test_the_asi_segment_marketplace_namespace_resolves_the_us_value_only() -> None:
    bridge = load_commerce_identity_bridge(ROOT)

    resolved = resolve_commerce_identity(
        bridge,
        ResolveCommerceIdentityRequest(
            SourceIdentifier("amc.segment_marketplace_id", "1"), date(2026, 10, 8)
        ),
    )

    assert resolved.marketplace.target == MarketplaceKey("amazon_us")
    assert resolved.marketplace.mapping_version == 4
    with pytest.raises(IdentityResolutionError, match="no canonical mapping"):
        resolve_commerce_identity(
            bridge,
            ResolveCommerceIdentityRequest(
                SourceIdentifier("amc.segment_marketplace_id", "2"), date(2026, 10, 8)
            ),
        )
