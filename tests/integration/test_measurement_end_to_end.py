# SPDX-License-Identifier: MIT-0
"""The target question, end to end, through the tool against real AWS.

*"What was my shipped revenue by ASIN last week?"* — build-order Phase 2's target and the reason
Wave 3 exists. This is the only test that exercises every layer at once: the metric dictionary,
the contract, the planner, the composed SQL, real Athena over real Iceberg, the freshness registry
in real DynamoDB, and the envelope.

**It is worth having even though every layer is already covered, because the seams are not.** The
composition tests assert SQL text; the query integration tests read rows straight out of the
adapter; the figures tests built their own rows. Each half was self-consistent, and the aggregate
was aliased `value` by the adapter and read back as `revenue_shipped` by the domain — a defect that
survived all of them and surfaced the first time one call went through the whole path. A test that
spans the seams is the only kind that could have caught it.

The figures come from the **load path** — `land_fixtures` then `ingest_delivery` — so the
assertions are on values rather than on shapes. `B0TRAP00001` is 88.5000: the restatement is
resolved by the load, because the later delivery's watermark is newer and its partition overwrite
replaces the earlier figure. It used to be 188.5000, which was both halves summed by a seed that
could not resolve one.
"""

from __future__ import annotations

import pathlib
from datetime import UTC, date, datetime
from decimal import Decimal

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
from domain.shared_kernel.errors import DomainError
from domain.shared_kernel.provenance import MeasurementBasis
from domain.shared_kernel.values import MONETARY_DECIMAL_PLACES, DateRange
from ports.outbound.publication_selection import (
    PublicationSelection,
    PublicationSelectionRequest,
)

pytestmark = [pytest.mark.integration, pytest.mark.covers("ADR-006", "ADR-024", "ADR-027")]

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
CONTRACT_ID = "vendor-sales-by-asin"
US = "ATVPDKIKX0DER"

WEEK = DateRange(date(2025, 6, 11), date(2025, 6, 11))
"""The restatement trap's day, kept before the generated fixture span (2025-07-01 onward)
so full-span generated landings can never supersede the hand-built trap partition."""

LOADED = {"amazon_asin:b0trap0001": Decimal("88.5000")}
"""The restated MANUFACTURING/RETAIL figure selected for the exact serving day."""


type Contracts = tuple[DataContract, ...]
"""Every contract in the repository. A fact is described by all of those writing it."""


@pytest.fixture(scope="module")
def contracts() -> tuple[DataContract, ...]:
    """Every contract, because a fact is described by all of those writing it."""
    return tuple(c for _, c in load_contracts(ROOT))


class RecordedSelector:
    """Delegate to deployed CURRENT selection and retain its immutable evidence for assertions."""

    def __init__(self, selector: PublicationSelector) -> None:
        self._selector = selector
        self.selection: PublicationSelection | None = None

    def select(self, request: PublicationSelectionRequest) -> PublicationSelection:
        self.selection = self._selector.select(request)
        return self.selection


@pytest.fixture
def publications(settings: Settings) -> RecordedSelector:
    return RecordedSelector(
        PublicationSelector(
            DynamoCurrentPublications(settings.semantic_publications_table),
            load_source_packs(ROOT),
            load_commerce_identity_bridge(ROOT),
        )
    )


@pytest.fixture
def tooling(settings: Settings, publications: RecordedSelector) -> MeasurementTooling:
    return MeasurementTooling(
        queries=AthenaQueryAdapter(
            AthenaQueryTarget(
                workgroup=settings.athena_workgroup,
                database=settings.semantic_v2_current_database,
                reuse_minutes=settings.athena_result_reuse_max_age_minutes,
            )
        ),
        publications=publications,
    )


@pytest.fixture
def answer(contracts: Contracts, tooling: MeasurementTooling) -> ShapedResponse:
    """Ask the target question, once, and let every assertion read the same answer.

    One Athena round trip for the whole module rather than one per assertion: the query costs real
    money and scans real bytes, and asserting twenty things about one answer is the same coverage
    as twenty answers.
    """
    return handle(
        MeasurementToolRequest(
            tool=MeasurementTool.METRIC_BY_GRAIN,
            catalog=load_catalog(ROOT),
            contracts=contracts,
            metric_name="retail_revenue_shipped",
            grains=("product",),
            window=WEEK,
            computed_at=datetime.now(UTC),
            correlation_id="integration-target-question",
            filters={"marketplace_key": US},
        ),
        tooling,
    )


class TestTheTargetQuestion:
    def test_it_returns_the_loaded_figures(self, answer: ShapedResponse) -> None:
        """The assertion Wave 3 exists to make true.

        Values, not shapes. Every layer between the metric dictionary and Iceberg is exercised by
        this one comparison, and a seam that lost or renamed the aggregate would show up here as a
        missing figure rather than as a passing test.
        """
        assert {str(row["amazon_asin"]): row["value"] for row in answer.data.values} == LOADED

    def test_the_figures_arrive_as_decimals_at_the_stored_scale(
        self, answer: ShapedResponse
    ) -> None:
        """Four decimal places, asserted on the exponent rather than on the text.

        Through `float`, 88.5000 is where a fourth decimal place and then a cent go missing. The
        exponent is the property that matters — a string check on trailing zeros passes or fails
        depending on the value rather than on the scale.
        """
        for row in answer.data.values:
            figure = row["value"]
            assert type(figure) is Decimal
            assert figure.as_tuple().exponent == -MONETARY_DECIMAL_PLACES

    def test_it_states_its_units_and_its_grain(self, answer: ShapedResponse) -> None:
        units = answer.data.units
        assert units is not None
        assert units.kind is UnitKind.CURRENCY
        assert units.currency_code == "USD"
        assert answer.data.grain.dimensions == ("amazon_asin",)

    def test_the_row_count_matches_what_it_carries(self, answer: ShapedResponse) -> None:
        assert answer.metadata.row_count == len(answer.data.values) == len(LOADED)

    def test_it_names_the_fact_and_basis_it_actually_read(self, answer: ShapedResponse) -> None:
        assert answer.metadata.facts_read == ("fct_retail_sales",)
        assert answer.metadata.measurement_basis == (MeasurementBasis.RETAIL_ACTUAL,)
        assert answer.metadata.mixes_basis is False

    def test_it_names_the_contract_that_delivered_these_figures(
        self, answer: ShapedResponse
    ) -> None:
        """G-37: the fact name alone cannot trace a figure back to a delivery.

        Asserted against the real contract set loaded from the repository, so a contract renamed
        without this being updated is a failure rather than a quietly emptied field.
        """
        assert answer.metadata.contract_ids == (CONTRACT_ID,)


class TestTheAnswerQualifiesItself:
    def test_it_states_the_lenses_it_pinned(self, answer: ShapedResponse) -> None:
        """The two option columns the question did not specify.

        Without them the plan is refused; with them unstated the answer is about a population the
        caller never chose.
        """
        assert any("distributor_view = MANUFACTURING" in s for s in answer.narrative)
        assert any("selling_program = RETAIL" in s for s in answer.narrative)

    def test_it_carries_the_mandatory_net_of_returns_caveat(self, answer: ShapedResponse) -> None:
        # answerable-questions §5 makes this non-optional for a recent vendor period.
        assert any("Net of returns" in sentence for sentence in answer.narrative)

    def test_it_states_the_restatement_window_with_its_duration(
        self, answer: ShapedResponse
    ) -> None:
        assert any("45" in sentence for sentence in answer.narrative)

    def test_it_names_the_sources_business_day(self, answer: ShapedResponse) -> None:
        # The dates it reports are vendor days closing in PST, not UTC days.
        assert any("PST" in sentence for sentence in answer.narrative)

    def test_the_coverage_window_is_the_one_asked_about(self, answer: ShapedResponse) -> None:
        assert answer.metadata.coverage_start.date() == WEEK.start
        assert answer.metadata.coverage_end.date() == WEEK.end


class TestCoverageComesFromSelectedV2Publications:
    def test_the_answer_uses_the_exact_current_publications(
        self,
        answer: ShapedResponse,
        publications: RecordedSelector,
    ) -> None:
        selected = publications.selection
        assert selected is not None
        assert all(bound.primary_ids for bound in selected.plans)
        assert answer.metadata.requested_coverage_intervals == (WEEK,)
        assert answer.metadata.actual_coverage_intervals == selected.coverage.intervals == (WEEK,)
        assert answer.metadata.coverage_holes == ()
        assert answer.metadata.freshness_status is selected.coverage.freshness
        assert answer.metadata.loaded_at == selected.coverage.loaded_at
        assert answer.metadata.qualified_dq == selected.qualified_dq

    def test_the_selected_coverage_has_aware_exact_bounds(self, answer: ShapedResponse) -> None:
        assert answer.metadata.actual_coverage_start is not None
        assert answer.metadata.actual_coverage_end is not None
        assert answer.metadata.actual_coverage_start.date() == WEEK.start
        assert answer.metadata.actual_coverage_end.date() == WEEK.end
        assert answer.metadata.loaded_at is not None
        assert answer.metadata.loaded_at.tzinfo is not None


class TestAWindowWithNoCurrentPublication:
    def test_it_fails_closed_before_querying(
        self, contracts: Contracts, tooling: MeasurementTooling
    ) -> None:
        """A period with no exact CURRENT publication is refused before Athena."""
        with pytest.raises(DomainError, match="No published data covers") as raised:
            handle(
                MeasurementToolRequest(
                    tool=MeasurementTool.METRIC_BY_GRAIN,
                    catalog=load_catalog(ROOT),
                    contracts=contracts,
                    metric_name="retail_revenue_shipped",
                    grains=("product",),
                    window=DateRange(date(2020, 1, 1), date(2020, 1, 7)),
                    computed_at=datetime.now(UTC),
                    correlation_id="integration-empty-window",
                    filters={"marketplace_key": US},
                ),
                tooling,
            )
        assert raised.value.alarms is False


class TestAnUnderSpecifiedQuestion:
    def test_it_is_refused_without_spending_a_query(
        self, contracts: Contracts, tooling: MeasurementTooling
    ) -> None:
        """ADR-024 reaching a caller through the real tool.

        No marketplace means no population and no currency, and the refusal arrives before Athena
        is touched — a refusal that had already queried would have cost money to say no.
        """
        with pytest.raises(DomainError) as raised:
            handle(
                MeasurementToolRequest(
                    tool=MeasurementTool.METRIC_BY_GRAIN,
                    catalog=load_catalog(ROOT),
                    contracts=contracts,
                    metric_name="retail_revenue_shipped",
                    grains=("product",),
                    window=WEEK,
                    computed_at=datetime.now(UTC),
                    correlation_id="integration-underspecified",
                    filters={},
                ),
                tooling,
            )

        assert "marketplace_key" in raised.value.user_visible_description
        assert raised.value.alarms is False
