# SPDX-License-Identifier: MIT-0
"""Source-derived delivery assessment for Semantic V2 ingestion.

Control-total failures remain symmetric across every contract reading the delivery, and manifest,
header, and currency assertions are decided only from immutable landed evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from domain.curation.assertions import AssertionName
from domain.curation.ingestion import (
    AssessmentRequest,
    ContractLoad,
    assess_source_delivery,
)
from domain.curation.landing import LandedObject, Manifest, partition_for
from domain.curation.mapping import SourceDocument, options_hash
from domain.data_contracts.contract import DataContract, FieldMapping, SourceFormat
from domain.data_contracts.validation import ColumnRegistry
from domain.semantics.identity import SourceIdentifier
from domain.shared_kernel.provenance import MeasurementBasis

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-030", "ADR-031")]

OPTIONS: dict[str, object] = {"distributorView": "MANUFACTURING", "sellingProgram": "RETAIL"}
OTHER_OPTIONS: dict[str, object] = {"distributorView": "SOURCING", "sellingProgram": "RETAIL"}
WINDOW = date(2026, 8, 10)
EMPTY = ColumnRegistry(columns={}, known_tables=frozenset(), inherits={})


def _manifest() -> Manifest:
    return Manifest(
        source_system="vendor_central",
        source_dataset="GET_VENDOR_SALES_REPORT",
        source_scope=SourceIdentifier("vendor_central.vendor_group", "fictional-vendor-group"),
        option_set=OPTIONS,
        option_set_hash=options_hash(OPTIONS),
        marketplace="ATVPDKIKX0DER",
        period_start=WINDOW,
        period_end=WINDOW,
        source_watermark=date(2026, 8, 12),
        retrieved_at=datetime(2026, 8, 19, 6, 0, tzinfo=UTC),
        declared_columns=("asin", "shippedRevenue"),
        objects=(LandedObject(name="data-0001.json", byte_count=64, checksum="sha256:a"),),
    )


@dataclass(frozen=True, slots=True)
class _Shape:
    """What varies between the contracts these tests need."""

    contract_id: str
    table: str
    grain: tuple[str, ...]
    measure: str = "revenue_shipped"


def _contract(shape: _Shape) -> DataContract:
    return DataContract(
        contract_id=shape.contract_id,
        contract_version=1,
        source_system="vendor_central",
        source_dataset="GET_VENDOR_SALES_REPORT",
        source_format=SourceFormat.JSON_DUAL_ARRAY,
        target_table=shape.table,
        grain=shape.grain,
        partition_keys=("marketplace_key", "date_key"),
        measurement_basis=MeasurementBasis.RETAIL_ACTUAL,
        source_timezone="America/Los_Angeles",
        ecosystem_key="amazon",
        platform_key="amazon_retail",
        fields=(
            FieldMapping(
                source_path="salesByAsin[].asin",
                target_column="amazon_asin",
                target_type="string",
            ),
            FieldMapping(
                source_path=f"salesByAsin[].{shape.measure}",
                target_column=shape.measure,
                target_type="decimal(38,4)",
            ),
        ),
        restatement_window_days=45,
        staleness_threshold_hours=96,
    )


DETAIL = _Shape(
    contract_id="vendor-sales-by-asin",
    table="fct_retail_sales",
    grain=("amazon_asin", "date_key", "marketplace_key"),
)
TOTAL = _Shape(
    contract_id="vendor-sales-reported-total",
    table="fct_retail_sales_reported_total",
    grain=("date_key", "marketplace_key"),
)


def _load(shape: _Shape, rows: tuple[dict[str, object], ...]) -> ContractLoad:
    manifest = _manifest()
    contract = _contract(shape)
    return ContractLoad(contract=contract, partition=partition_for(manifest, contract), rows=rows)


def _request(loads: tuple[ContractLoad, ...]) -> AssessmentRequest:
    return AssessmentRequest(
        manifest=_manifest(),
        document=SourceDocument(document={"salesByAsin": []}),
        loads=loads,
        registry=EMPTY,
    )


def _detail_rows(amount: str) -> tuple[dict[str, object], ...]:
    row: dict[str, object] = {
        "amazon_asin": "B0TRAP00007",
        "marketplace_key": "ATVPDKIKX0DER",
        "revenue_shipped": Decimal(amount),
    }
    return (row,)


def _total_rows(amount: str) -> tuple[dict[str, object], ...]:
    row: dict[str, object] = {
        "marketplace_key": "ATVPDKIKX0DER",
        "revenue_shipped": Decimal(amount),
    }
    return (row,)


# ---------------------------------------------------------------------------
# ADR-031 — a delivery-level failure lands on every partition
# ---------------------------------------------------------------------------


def test_a_control_total_mismatch_quarantines_both_grains() -> None:
    """Withholding one and not the other publishes a figure its own counterpart contradicts."""
    assessment = assess_source_delivery(
        _request(
            (_load(DETAIL, _detail_rows("40.0")), _load(TOTAL, _total_rows("100.0"))),
        )
    )

    assert [item.contract_id for item in assessment.assessments if item.quarantines] == [
        "vendor-sales-by-asin",
        "vendor-sales-reported-total",
    ]
    assert assessment.quarantines


def test_a_control_total_mismatch_is_reported_once_not_once_per_partition() -> None:
    """One defect in one document. Reporting it twice would overstate how much is wrong."""
    assessment = assess_source_delivery(
        _request(
            (_load(DETAIL, _detail_rows("40.0")), _load(TOTAL, _total_rows("100.0"))),
        )
    )
    control = [v for v in assessment.violations if v.assertion is AssertionName.CONTROL_TOTAL]
    assert len(control) == 1


def test_agreeing_totals_leave_both_grains_writable() -> None:
    assessment = assess_source_delivery(
        _request(
            (_load(DETAIL, _detail_rows("40.0")), _load(TOTAL, _total_rows("40.0"))),
        )
    )
    assert not assessment.quarantines
    assert all(item.writes for item in assessment.assessments)


def test_a_delivery_read_by_one_contract_runs_no_control_total() -> None:
    """The assertion is about two contracts. With one there is nothing to compare."""
    assessment = assess_source_delivery(_request((_load(DETAIL, _detail_rows("40.0")),)))
    assert not assessment.quarantines


# ---------------------------------------------------------------------------
# The manifest and the document must agree about which pull this is
# ---------------------------------------------------------------------------


def test_rows_carrying_a_different_option_hash_than_the_manifest_quarantine() -> None:
    """Two lenses on the same units merged would double-count every one of them."""
    row: dict[str, object] = {
        "amazon_asin": "B0TRAP00007",
        "marketplace_key": "ATVPDKIKX0DER",
        "revenue_shipped": Decimal("40.0"),
        "report_options_hash": options_hash(OTHER_OPTIONS),
    }
    rows = (row,)
    assessment = assess_source_delivery(_request((_load(DETAIL, rows),)))
    assert assessment.quarantines
    assert any(v.assertion is AssertionName.MANIFEST_AGREEMENT for v in assessment.violations)


def test_rows_carrying_the_manifests_own_option_hash_agree() -> None:
    row: dict[str, object] = {
        "amazon_asin": "B0TRAP00007",
        "marketplace_key": "ATVPDKIKX0DER",
        "revenue_shipped": Decimal("40.0"),
        "report_options_hash": options_hash(OPTIONS),
    }
    rows = (row,)
    assessment = assess_source_delivery(_request((_load(DETAIL, rows),)))
    assert not assessment.quarantines


def test_a_delivery_with_no_option_hash_column_has_nothing_to_disagree_about() -> None:
    """A Data Kiosk delivery has no report options at all, so an empty outcome is honest."""
    assessment = assess_source_delivery(_request((_load(DETAIL, _detail_rows("40.0")),)))
    assert not any(v.assertion is AssertionName.MANIFEST_AGREEMENT for v in assessment.violations)


# ---------------------------------------------------------------------------
# Partitions
# ---------------------------------------------------------------------------


def test_two_contracts_reading_one_delivery_get_two_distinct_partitions() -> None:
    """One ledger entry each is what lets one grain quarantine while the other loads."""
    detail = _load(DETAIL, _detail_rows("40.0"))
    total = _load(TOTAL, _total_rows("40.0"))
    assert detail.partition != total.partition
    assert detail.partition.report_options_hash == total.partition.report_options_hash


def test_a_partition_takes_its_option_hash_from_the_manifest_not_the_rows() -> None:
    """So the ledger's idea of which pull this is agrees with the landing's by construction."""
    assert _load(DETAIL, ()).partition.report_options_hash == options_hash(OPTIONS)
