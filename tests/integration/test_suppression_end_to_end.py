# SPDX-License-Identifier: MIT-0
"""AMC suppression folding reaching a caller through the deployed tool envelope."""

from __future__ import annotations

import pathlib
from datetime import UTC, date, datetime

import pytest

from adapters.config import Settings
from adapters.inbound.tool_handlers.measurement import (
    MeasurementTool,
    MeasurementTooling,
    MeasurementToolRequest,
    handle,
)
from adapters.jobs.contract_validation.loader import load_contracts
from adapters.outbound.athena.query import AthenaQueryAdapter, AthenaQueryTarget
from adapters.outbound.commerce_identity import load_commerce_identity_bridge
from adapters.outbound.dynamodb.semantic_state import DynamoCurrentPublications
from adapters.outbound.metric_dictionary import load_catalog
from adapters.outbound.publication_selection import PublicationSelector
from adapters.outbound.source_pack import load_source_packs
from domain.data_contracts.contract import DataContract
from domain.narrative.shaped_response import ShapedResponse, UnitKind
from domain.shared_kernel.values import MONETARY_DECIMAL_PLACES, DateRange

pytestmark = [pytest.mark.integration, pytest.mark.covers("ADR-035", "G-30")]

ROOT = pathlib.Path(__file__).resolve().parents[2]
DAY = DateRange(start=date(2026, 8, 11), end=date(2026, 8, 11))
FULL_GRAIN = ("date", "channel", "campaign", "ad_product", "product", "marketplace")


@pytest.fixture(scope="module")
def contracts() -> tuple[DataContract, ...]:
    return tuple(contract for _, contract in load_contracts(ROOT))


@pytest.fixture(scope="module")
def answer(
    contracts: tuple[DataContract, ...],
    settings: Settings,
) -> ShapedResponse:
    tooling = MeasurementTooling(
        queries=AthenaQueryAdapter(
            AthenaQueryTarget(
                workgroup=settings.athena_workgroup,
                database=settings.semantic_v2_current_database,
                reuse_minutes=settings.athena_result_reuse_max_age_minutes,
            )
        ),
        publications=PublicationSelector(
            DynamoCurrentPublications(settings.semantic_publications_table),
            load_source_packs(ROOT),
            load_commerce_identity_bridge(ROOT),
        ),
    )
    return handle(
        MeasurementToolRequest(
            tool=MeasurementTool.METRIC_BY_GRAIN,
            catalog=load_catalog(ROOT),
            contracts=contracts,
            metric_name="attributed_revenue_14d",
            grains=FULL_GRAIN,
            window=DAY,
            computed_at=datetime.now(UTC),
            correlation_id="integration-suppression-envelope",
            filters={},
        ),
        tooling,
    )


def test_unsuppressed_amc_rows_reach_the_envelope_with_money_and_full_grain(
    answer: ShapedResponse,
) -> None:
    assert answer.data.values
    assert answer.metadata.row_count == len(answer.data.values)
    assert answer.data.grain.dimensions == (
        "date_key",
        "channel_key",
        "campaign_key",
        "ad_product_key",
        "amazon_asin",
        "marketplace_key",
    )
    assert answer.data.units is not None
    assert answer.data.units.kind is UnitKind.CURRENCY
    assert answer.data.units.currency_code == "USD"
    for row in answer.data.values:
        value = str(row["value"])
        assert len(value.partition(".")[2]) == MONETARY_DECIMAL_PLACES


def test_suppression_absence_is_measured_and_coverage_remains_attached(
    answer: ShapedResponse,
) -> None:
    assert answer.metadata.suppression.rows_suppressed is False
    assert answer.metadata.suppression.reason is None
    assert answer.metadata.requested_coverage_intervals == (DAY,)
    assert answer.metadata.actual_coverage_intervals == (DAY,)
    assert answer.metadata.coverage_holes == ()
    assert answer.metadata.loaded_at is not None
    assert answer.metadata.loaded_at.tzinfo is not None
    assert answer.narrative
