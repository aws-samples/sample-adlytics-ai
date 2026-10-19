# SPDX-License-Identifier: MIT-0
"""Suppression folds over deployed AMC rows instead of refusing suppressible facts."""

from __future__ import annotations

import pathlib
from datetime import UTC, date, datetime

import pytest

from adapters.config import Settings
from adapters.jobs.contract_validation.loader import load_contracts
from adapters.outbound.athena.query import AthenaQueryAdapter, AthenaQueryTarget
from adapters.outbound.commerce_identity import load_commerce_identity_bridge
from adapters.outbound.dynamodb.semantic_state import DynamoCurrentPublications
from adapters.outbound.metric_dictionary import load_catalog
from adapters.outbound.publication_selection import PublicationSelector
from adapters.outbound.source_pack import load_source_packs
from domain.measurement.planning import FactShape, PlanRequest, QueryPlan, plan
from domain.measurement.publications import BoundQueryPlan
from domain.shared_kernel.values import DateRange
from ports.outbound.publication_selection import PublicationSelectionRequest
from ports.outbound.query import QueryRequest, QueryResult

pytestmark = [pytest.mark.integration, pytest.mark.covers("ADR-035", "G-30")]

ROOT = pathlib.Path(__file__).resolve().parents[2]
DAY = DateRange(start=date(2026, 8, 11), end=date(2026, 8, 11))
FULL_GRAIN = ("channel", "campaign", "ad_product", "product", "marketplace")


def _amc_plan(**extra: str) -> QueryPlan:
    contracts = {contract.contract_id: contract for _, contract in load_contracts(ROOT)}
    return plan(
        load_catalog(ROOT),
        PlanRequest(
            metric_name="attributed_revenue_14d",
            grains=FULL_GRAIN,
            window=DAY,
            filters=extra,
        ),
        FactShape.of(contracts["amc-attributed-conversion"]),
    )


def _bind(selector: PublicationSelector, logical: QueryPlan) -> BoundQueryPlan:
    contracts = tuple(contract for _, contract in load_contracts(ROOT))
    return selector.select(
        PublicationSelectionRequest(
            plans=(logical,),
            contracts=contracts,
            now=datetime.now(UTC),
        )
    ).plans[0]


@pytest.fixture(scope="module")
def selector(settings: Settings) -> PublicationSelector:
    return PublicationSelector(
        DynamoCurrentPublications(settings.semantic_publications_table),
        load_source_packs(ROOT),
        load_commerce_identity_bridge(ROOT),
    )


@pytest.fixture
def adapter(settings: Settings) -> AthenaQueryAdapter:
    return AthenaQueryAdapter(
        AthenaQueryTarget(
            workgroup=settings.athena_workgroup,
            database=settings.semantic_v2_current_database,
            reuse_minutes=settings.athena_result_reuse_max_age_minutes,
        )
    )


@pytest.fixture
def unpinned_result(
    adapter: AthenaQueryAdapter,
    selector: PublicationSelector,
) -> tuple[QueryPlan, QueryResult]:
    logical = _amc_plan()
    bound = _bind(selector, logical)
    return logical, adapter.run(QueryRequest(plan=bound, max_rows=50))


def test_suppressible_amc_fact_executes_without_discriminator_filter(
    unpinned_result: tuple[QueryPlan, QueryResult],
) -> None:
    logical, result = unpinned_result
    assert logical.suppression_columns
    assert "rows_suppressed" not in logical.filters
    assert result.rows
    assert result.suppression.rows_suppressed is False
    assert result.suppression.reason is None


@pytest.mark.parametrize("suppressed", ["false", "true"])
def test_explicit_discriminator_populations_remain_queryable(
    adapter: AthenaQueryAdapter,
    selector: PublicationSelector,
    suppressed: str,
) -> None:
    logical = _amc_plan(rows_suppressed=suppressed)
    bound = _bind(selector, logical)
    result = adapter.run(QueryRequest(plan=bound, max_rows=50))

    assert bound.primary_ids
    assert result.suppression.rows_suppressed is bool(
        sum(int(row["withheld_rows_0"] or 0) for row in result.rows)
    )
    assert bool(result.suppression.reason) is result.suppression.rows_suppressed


def test_vendor_fact_reports_a_provable_suppression_absence(
    adapter: AthenaQueryAdapter,
    selector: PublicationSelector,
) -> None:
    contracts = {contract.contract_id: contract for _, contract in load_contracts(ROOT)}
    logical = plan(
        load_catalog(ROOT),
        PlanRequest(
            metric_name="retail_revenue_shipped",
            grains=("product",),
            window=DAY,
            filters={
                "marketplace_key": "ATVPDKIKX0DER",
                "distributor_view": "MANUFACTURING",
                "selling_program": "RETAIL",
            },
        ),
        FactShape.of(contracts["vendor-sales-by-asin"]),
    )
    assert logical.suppression_columns == ()

    result = adapter.run(QueryRequest(plan=_bind(selector, logical), max_rows=50))

    assert result.suppression.rows_suppressed is False
    assert result.suppression.reason is None
