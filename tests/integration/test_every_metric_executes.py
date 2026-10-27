# SPDX-License-Identifier: MIT-0
"""Every declared metric, executed against real Iceberg through the tool.

**Planning proves the model; only executing proves the SQL.** The planner refuses what it cannot
express, so a metric that plans has a valid grain, accounted-for filters and a resolvable
definition — and none of that says the composed statement runs. A wrong alias, a measure Athena
cannot sum, a ratio whose two sides join on a column one of them spells differently: each passes
planning and fails on execution.

Parametrized from the metric dictionary rather than from a list, so a metric added tomorrow is
covered without anyone remembering to add it here. That is the same reason the generated fixtures
derive their shape from the schema.

The assertions are deliberately weak on values and strict on shape. What each figure *is* belongs to
the tests that pin a known number — `test_measurement_end_to_end` for retail, `test_roas_end_to_end`
and `test_suppression_end_to_end` for AMC. What this file establishes is that all fifty are
*reachable*: the statement composes, Athena accepts it, and the envelope comes back qualified. A
figure this test cannot check is still worth executing, because the failure it catches is a raised
exception rather than a wrong number.

One grain — `date` — for every metric, because `date_key` is in every fact's grain and the point is
coverage of the metric set rather than of the grain set. A per-grain sweep would be fifty times
larger and would mostly re-prove the planner.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass
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
from adapters.outbound.athena.query import (
    INTERACTIVE_WAIT,
    AthenaQueryAdapter,
    AthenaQueryTarget,
    ResultTransport,
)
from adapters.outbound.commerce_identity import load_commerce_identity_bridge
from adapters.outbound.dynamodb.semantic_state import DynamoCurrentPublications
from adapters.outbound.metric_dictionary import load_catalog
from adapters.outbound.publication_selection import PublicationSelector
from adapters.outbound.source_pack import load_source_packs
from domain.catalog.capability import Catalog, MetricKind
from domain.data_contracts.contract import DataContract
from domain.measurement.planning import FactShape
from domain.narrative.shaped_response import ShapedResponse, UnitKind
from domain.shared_kernel.errors import DomainError
from domain.shared_kernel.values import DateRange
from ports.outbound.publication_selection import (
    PublicationSelection,
    PublicationSelectionRequest,
)

pytestmark = [pytest.mark.integration, pytest.mark.covers("ADR-028", "ADR-035")]

ROOT = pathlib.Path(__file__).resolve().parents[2]

WEEK = DateRange(start=date(2026, 8, 11), end=date(2026, 8, 11))
"""One exact unsuppressed V2 CURRENT day shared by every fact family."""

FILTER_POOL = {
    "marketplace_key": "ATVPDKIKX0DER",
    "channel_key": "sponsored_brands",
    "ad_product_key": "sponsored_brands",
    "campaign_key": "campaign_key-001",
    "amazon_asin": "B0FIXTUR01",
    "currency_code": "USD",
    "distributor_view": "MANUFACTURING",
    "selling_program": "RETAIL",
}
"""Every value a question might pin, applied per fact rather than wholesale.

**A flat mapping cannot be passed to every metric, and trying is itself a refusal.** Naming a column
the fact does not declare is `ADLY-PLAN-0001`, and the facts genuinely differ — `fct_retail_sales`
has a distributor view and no campaign, `fct_ad_exposure` has a campaign and no ASIN. So each metric
receives the intersection of this pool with the columns its own facts declare.

`rows_suppressed` is deliberately absent. ADR-035 made the discriminator the query's business, and
pinning it here would both select the unsuppressed population and refuse outright on the two Vendor
Central facts that have no such column.
"""

METRIC_NAMES = tuple(sorted(metric.name for metric in load_catalog(ROOT).metrics))
"""Read at import so the parametrization is the dictionary, not a copy of it."""


class _RecordingSelector:
    """Delegate to the deployed selector while retaining the exact selection under test."""

    def __init__(self, selector: PublicationSelector) -> None:
        self._selector = selector
        self.selection: PublicationSelection | None = None

    def select(self, request: PublicationSelectionRequest) -> PublicationSelection:
        self.selection = self._selector.select(request)
        return self.selection


@dataclass(frozen=True, slots=True)
class _Harness:
    """The dictionary, the contracts and the live ports, built once for the whole sweep."""

    catalog: Catalog
    contracts: tuple[DataContract, ...]
    publications: _RecordingSelector
    tooling: MeasurementTooling

    def filters_for(self, metric_name: str) -> dict[str, str]:
        """Use shared cross-fact columns plus explicitly declared selector asymmetry."""
        metric = self.catalog.metric(metric_name)
        assert metric is not None, f"{metric_name} came from the dictionary and must resolve"
        facts = self.catalog.facts_read(metric)
        shapes = [FactShape.for_fact(fact, list(self.contracts)) for fact in facts]
        if len(shapes) > 1:
            declared: set[str] = set(shapes[0].columns)
            for shape in shapes[1:]:
                declared.intersection_update(shape.columns)
        else:
            declared = set(shapes[0].columns)
        declared.update(key for key, _value in metric.requires)
        filters = {column: value for column, value in FILTER_POOL.items() if column in declared}
        filters.update({key: values[0] for key, values in metric.required_selectors})
        return filters


@pytest.fixture(scope="module")
def harness(settings: Settings) -> _Harness:
    catalog = load_catalog(ROOT)
    contracts = tuple(c for _, c in load_contracts(ROOT))
    publications = _RecordingSelector(
        PublicationSelector(
            DynamoCurrentPublications(settings.semantic_publications_table),
            load_source_packs(ROOT),
            load_commerce_identity_bridge(ROOT),
        )
    )
    return _Harness(
        catalog=catalog,
        contracts=contracts,
        publications=publications,
        tooling=MeasurementTooling(
            queries=AthenaQueryAdapter(
                AthenaQueryTarget(
                    workgroup=settings.athena_workgroup,
                    database=settings.semantic_v2_current_database,
                    reuse_minutes=settings.athena_result_reuse_max_age_minutes,
                    wait=INTERACTIVE_WAIT,
                    results=ResultTransport.PAGED,
                )
            ),
            publications=publications,
        ),
    )


@pytest.mark.parametrize("metric_name", METRIC_NAMES)
def test_the_metric_executes_and_returns_a_qualified_envelope(
    metric_name: str, harness: _Harness
) -> None:
    """The statement composes, Athena accepts it, and the answer qualifies itself.

    An exception here is the finding. Zero rows is not a failure — the fixtures do not populate
    every fact for every lens — but a response that came back must still name its basis and carry a
    freshness status, because an unqualified figure is what this platform exists not to produce.

    A product-of-operands metric is the one declared exception: its honest figure is derived at
    publish time from measured operands, so the live planner refuses it by design and the
    refusal must offer those operands rather than a number (ADR-032 amendment).
    """
    metric = harness.catalog.metric(metric_name)
    assert metric is not None
    if metric.kind is MetricKind.PRODUCT:
        with pytest.raises(DomainError) as refused:
            handle(
                MeasurementToolRequest(
                    tool=MeasurementTool.METRIC_BY_GRAIN,
                    catalog=harness.catalog,
                    contracts=harness.contracts,
                    metric_name=metric_name,
                    grains=("date",),
                    window=WEEK,
                    computed_at=datetime.now(UTC),
                    correlation_id=f"integration-sweep-{metric_name}",
                    filters=harness.filters_for(metric_name),
                ),
                harness.tooling,
            )
        for operand in metric.operands:
            assert operand in refused.value.user_visible_description
        return
    response: ShapedResponse = handle(
        MeasurementToolRequest(
            tool=MeasurementTool.METRIC_BY_GRAIN,
            catalog=harness.catalog,
            contracts=harness.contracts,
            metric_name=metric_name,
            grains=("date",),
            window=WEEK,
            computed_at=datetime.now(UTC),
            correlation_id=f"integration-sweep-{metric_name}",
            filters=harness.filters_for(metric_name),
        ),
        harness.tooling,
    )

    assert response.metadata.measurement_basis, "an answer must name the system it measured"
    assert response.metadata.freshness_status is not None
    assert response.metadata.requested_coverage_intervals == (WEEK,)
    assert response.metadata.actual_coverage_intervals == (WEEK,)
    assert response.metadata.coverage_holes == ()
    assert response.metadata.loaded_at is not None
    assert response.metadata.loaded_at.tzinfo is not None
    selected = harness.publications.selection
    assert selected is not None
    assert all(bound.primary_ids for bound in selected.plans)
    assert all(bound.logical.join is None or bound.joined_ids for bound in selected.plans)
    assert response.narrative, "an unqualified figure is the failure mode this product avoids"
    assert response.metadata.row_count == len(response.data.values)

    if response.data.values:
        assert response.data.grain.dimensions == ("date_key",)
        units = response.data.units
        assert units is not None, "a figure with no units cannot be read safely"
        if units.kind is UnitKind.CURRENCY:
            assert units.currency_code, "money must name its currency"
    else:
        # An empty result carries no units, for the same reason an empty monetary result carries
        # no currency: there is nothing to denominate.
        assert response.data.units is None
