# SPDX-License-Identifier: MIT-0
"""The Athena adapter against the real deployed workgroup.

What a fake could not establish, and what these exist for: that the statement `compose`
builds is one Athena actually **parses and executes**. A unit test can assert the SQL text
character by character and still be asserting a statement no engine accepts — which is
precisely how the three Glue rejections behind this deployment were found.

`fct_retail_sales` and `dim_product` hold rows put there by the **load path** — `land_fixtures`
then `ingest_delivery` — so the assertions here are on values rather than on "it returned".
**That change is what exposed the parameter defect.** Every
one of these tests passed against empty tables while the date bounds were arriving as the integer
2008, because a cast over zero rows is never evaluated. A test that only asserts a query ran is
worth much less than it looks.

The empty case is still covered, deliberately and separately: Athena returns a **header row**
for a `SELECT` even when nothing matches, and read naively that becomes a phantom row whose
every value is a column name.

What is deliberately not here: a scan-cutoff trip, which needs a table large enough to exceed
the budget.
"""

from __future__ import annotations

import pathlib
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from decimal import Decimal

import boto3
import pytest

from adapters.config import Settings
from adapters.jobs.contract_validation.loader import load_contracts
from adapters.outbound.athena.composition import compose
from adapters.outbound.athena.execution import (
    MAX_WAIT_SECONDS,
    QUERY_FAILED,
    StatementRequest,
    await_completion,
    start,
)
from adapters.outbound.athena.query import (
    AGGREGATE_COLUMN,
    INTERACTIVE_WAIT,
    AthenaQueryAdapter,
    AthenaQueryTarget,
)
from adapters.outbound.commerce_identity import load_commerce_identity_bridge
from adapters.outbound.dynamodb.semantic_state import DynamoCurrentPublications
from adapters.outbound.metric_dictionary import load_catalog
from adapters.outbound.publication_selection import PublicationSelector
from adapters.outbound.source_pack import load_source_packs
from domain.measurement.planning import FactShape, PlanRequest, QueryPlan, plan
from domain.measurement.publications import BoundQueryPlan
from domain.shared_kernel.errors import DependencyError, DomainError
from domain.shared_kernel.values import DateRange
from ports.outbound.publication_selection import PublicationSelectionRequest
from ports.outbound.query import QueryPort, QueryRequest

pytestmark = [pytest.mark.integration, pytest.mark.covers("ADR-014", "ADR-024")]

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
WEEK = DateRange(date(2026, 8, 11), date(2026, 8, 11))
# The restatement trap's day, kept before the generated fixture span (2025-07-01 onward)
# so full-span generated landings can never supersede the hand-built trap partition.
TRAP_DAY = DateRange(date(2025, 6, 11), date(2025, 6, 11))
# One filter set per fact, each accounting for that fact's own grain (ADR-024).
RETAIL_SALES_FILTERS = {
    "marketplace_key": "ATVPDKIKX0DER",
    "distributor_view": "MANUFACTURING",
    "selling_program": "RETAIL",
}
# fct_product_page_views is date x product x marketplace, with no option columns.
PAGE_VIEW_FILTERS = {"marketplace_key": "ATVPDKIKX0DER"}
# An AMC fact adds channel, campaign and the suppression discriminator to its grain.
AMC_CONVERSION_FILTERS = {
    "marketplace_key": "ATVPDKIKX0DER",
    "channel_key": "amazon_dsp",
    "campaign_key": "camp-1",
    "rows_suppressed": "false",
}


def _plan(metric: str, contract_id: str, filters: dict[str, str]) -> QueryPlan:
    """Plan one question. Filters are per fact, because each has its own grain to account for.

    There is no shared set: `fct_retail_sales` needs its two option columns pinned,
    `fct_product_page_views` has none, and an AMC fact needs its channel, campaign and
    suppression discriminator. ADR-024 makes that explicit rather than letting a
    one-size-fits-all filter dict quietly under-account for one of them.
    """
    contracts = {c.contract_id: c for _, c in load_contracts(ROOT)}
    window = TRAP_DAY if contract_id == "vendor-sales-by-asin" else WEEK
    return plan(
        load_catalog(ROOT),
        PlanRequest(metric_name=metric, grains=("product",), window=window, filters=filters),
        FactShape.of(contracts[contract_id]),
    )


def _target(settings: Settings) -> AthenaQueryTarget:
    return AthenaQueryTarget(
        workgroup=settings.athena_workgroup,
        database=settings.semantic_v2_current_database,
        reuse_minutes=settings.athena_result_reuse_max_age_minutes,
        wait=INTERACTIVE_WAIT,
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
    return AthenaQueryAdapter(_target(settings))


@pytest.fixture(scope="module")
def revenue_plan(selector: PublicationSelector) -> BoundQueryPlan:
    return _bind(
        selector,
        _plan("retail_revenue_shipped", "vendor-sales-by-asin", RETAIL_SALES_FILTERS),
    )


class TestPortConformance:
    def test_the_adapter_satisfies_the_port(self, adapter: AthenaQueryAdapter) -> None:
        assert isinstance(adapter, QueryPort)


class TestAthenaAcceptsWhatWeCompose:
    def test_the_target_question_returns_the_loaded_figures(
        self, adapter: AthenaQueryAdapter, revenue_plan: BoundQueryPlan
    ) -> None:
        """The target question, end to end, against real Iceberg.

        Shipped revenue by ASIN for last week.

        The figures are asserted, not just the shape. Two loaded rows are excluded by the
        ADR-024 option filters — one is `SOURCING` and one is `FRESH` — and `B0TRAP00001` is
        **88.5000**, the restated figure alone.

        **That number is the change this assertion was written to capture.** It used to be
        `188.5000`, which is `100.0000 + 88.5000` — both halves of a restatement summed, because
        the table was filled by a seed that could not resolve one. The load path resolves it: the
        later delivery's watermark is newer, so its partition overwrite replaces the earlier
        figure rather than adding to it. The old assertion recorded a double-count as expected,
        and said in as many words that it existed so the merge landing would be visible. It has
        landed.
        """
        assert revenue_plan.primary_ids
        result = adapter.run(QueryRequest(plan=revenue_plan, max_rows=100))

        assert {row["amazon_asin"]: row[AGGREGATE_COLUMN] for row in result.rows} == {
            "amazon_asin:b0trap0001": Decimal("88.5000"),
        }

    def test_one_adapter_safely_runs_independent_requests_in_parallel(
        self, adapter: AthenaQueryAdapter, revenue_plan: BoundQueryPlan
    ) -> None:
        """The presentation executor shares one adapter across its bounded worker threads."""
        request = QueryRequest(plan=revenue_plan, max_rows=100)
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = (executor.submit(adapter.run, request), executor.submit(adapter.run, request))
            results = tuple(future.result() for future in futures)

        assert all(result.rows for result in results)
        assert results[0].rows == results[1].rows

    def test_a_monetary_row_carries_its_currency(
        self, adapter: AthenaQueryAdapter, revenue_plan: BoundQueryPlan
    ) -> None:
        # A monetary figure without its currency is a number, not an amount. `currency_code` is
        # in the grain of no fact, so it is carried rather than grouped — and this is where that
        # distinction has to hold against a real result set.
        result = adapter.run(QueryRequest(plan=revenue_plan, max_rows=100))

        assert result.rows
        for row in result.rows:
            assert row["currency_code"] == "USD"

    def test_a_decimal_measure_arrives_as_a_decimal_not_a_float(
        self, adapter: AthenaQueryAdapter, revenue_plan: BoundQueryPlan
    ) -> None:
        """Athena returns every value as a string, so the type is the adapter's to restore.

        Through `float`, `88.5000` is where a fourth decimal place and then a cent goes missing.
        """
        result = adapter.run(QueryRequest(plan=revenue_plan, max_rows=100))

        for row in result.rows:
            assert type(row[AGGREGATE_COLUMN]) is Decimal

    def test_an_empty_result_drops_athenas_header_row(
        self, adapter: AthenaQueryAdapter, selector: PublicationSelector
    ) -> None:
        """Athena returns column headers as the first row of a SELECT, even with no matches.

        Read naively that becomes a data row in which `value` is the string "value", and it
        would surface much later as a measurement failure rather than here.

        Reached by asking about a window nothing was seeded into, rather than by relying on an
        empty table — the tables have rows now, and an assertion that depends on them not having
        any would stop testing this the moment more data lands.
        """
        empty = plan(
            load_catalog(ROOT),
            PlanRequest(
                metric_name="retail_revenue_shipped",
                grains=("product",),
                window=WEEK,
                filters={**RETAIL_SALES_FILTERS, "amazon_asin": "B0EMPTY001"},
            ),
            FactShape.of(
                {c.contract_id: c for _, c in load_contracts(ROOT)}["vendor-sales-by-asin"]
            ),
        )

        result = adapter.run(QueryRequest(plan=_bind(selector, empty), max_rows=100))

        assert result.rows == ()
        assert result.truncated is False

    def test_it_reports_what_the_query_cost(
        self, adapter: AthenaQueryAdapter, revenue_plan: BoundQueryPlan
    ) -> None:
        """A tool that cannot say what a question scanned cannot explain a bill.

        Now that rows exist the figure is positive, which is a real assertion — `>= 0` held over
        an empty table no matter what the adapter did with the field.
        """
        result = adapter.run(QueryRequest(plan=revenue_plan, max_rows=100))

        assert result.bytes_scanned > 0

    def test_bound_parameters_reach_the_engine_as_values(
        self, adapter: AthenaQueryAdapter, revenue_plan: BoundQueryPlan
    ) -> None:
        """A `?` count mismatch is an Athena error, not a Python one.

        Running the composed statement is the only way to know the placeholders and the
        `ExecutionParameters` list agree — and the CAST around the date bounds is needed
        precisely because Athena parses a parameter as an expression rather than binding it,
        so `execution.start` has to send it as a quoted literal for the cast to have a string
        to work on.
        """
        composed = compose(revenue_plan, max_rows=5)
        assert composed.sql.count("?") == len(composed.parameters)

        # If they disagreed, this raises DependencyError rather than returning.
        adapter.run(QueryRequest(plan=revenue_plan, max_rows=5))

    def test_a_wider_grain_also_runs(
        self, adapter: AthenaQueryAdapter, selector: PublicationSelector
    ) -> None:
        """Two group-by columns, so the GROUP BY and ORDER BY lists are exercised."""
        wider = plan(
            load_catalog(ROOT),
            PlanRequest(
                metric_name="retail_revenue_shipped",
                grains=("product", "marketplace"),
                window=WEEK,
                filters={"distributor_view": "MANUFACTURING", "selling_program": "RETAIL"},
            ),
            FactShape.of(
                {c.contract_id: c for _, c in load_contracts(ROOT)}["vendor-sales-by-asin"]
            ),
        )

        result = adapter.run(QueryRequest(plan=_bind(selector, wider), max_rows=10))

        assert result.rows
        for row in result.rows:
            # Both grain columns present in every row: a GROUP BY that dropped one would still
            # return rows, with each figure silently aggregated across the missing dimension.
            assert row["amazon_asin"]
            assert row["marketplace_key"] == "amazon_us"

    def test_a_count_metric_runs_without_asking_for_a_currency(
        self, adapter: AthenaQueryAdapter, selector: PublicationSelector
    ) -> None:
        """`fct_product_page_views` has no `currency_code`, so carrying one would fail here.

        This is the assertion that `carried_columns` is conditional on the metric's units
        rather than always populated — a column that does not exist is a parse error.
        """
        views = _plan("glance_views", "vendor-traffic-by-asin", PAGE_VIEW_FILTERS)
        assert views.carried_columns == ()

        result = adapter.run(QueryRequest(plan=_bind(selector, views), max_rows=10))
        assert result.rows
        assert {row[AGGREGATE_COLUMN] for row in result.rows} == {Decimal("170")}


class TestTheWindowPredicateIsPushedDown:
    """The one thing only a real engine can answer about the composed statement.

    `date_key` is cast nowhere and the parameter is cast twice-removed, which raised a fair
    worry: a predicate the engine cannot fold is a predicate applied *after* reading, and every
    windowed question becomes a full scan. `EXPLAIN` cannot settle it — Athena rejects
    `ExecutionParameters` on an `EXPLAIN` outright — so the scan figure is the only evidence.
    """

    def test_a_window_without_current_publications_fails_before_athena(
        self, selector: PublicationSelector
    ) -> None:
        """Missing exact CURRENT coverage is a refusal, never an unfenced table scan."""
        outside = plan(
            load_catalog(ROOT),
            PlanRequest(
                metric_name="retail_revenue_shipped",
                grains=("product",),
                window=DateRange(date(2020, 1, 1), date(2020, 1, 7)),
                filters=RETAIL_SALES_FILTERS,
            ),
            FactShape.of(
                {c.contract_id: c for _, c in load_contracts(ROOT)}["vendor-sales-by-asin"]
            ),
        )

        with pytest.raises(DomainError, match="No published data covers the requested window"):
            _bind(selector, outside)

    def test_a_window_containing_the_data_does_read_it(
        self, adapter: AthenaQueryAdapter, revenue_plan: BoundQueryPlan
    ) -> None:
        # The other half. Without it, an adapter that reported zero unconditionally would pass
        # the test above.
        assert adapter.run(QueryRequest(plan=revenue_plan, max_rows=100)).bytes_scanned > 0


class TestSuppressionIsStatedNeverAssumed:
    def test_a_vendor_fact_reports_no_withholding_as_a_provable_absence(
        self, adapter: AthenaQueryAdapter, revenue_plan: BoundQueryPlan
    ) -> None:
        """Vendor Central has no threshold mechanism, so nothing could have been withheld.

        That is why `rows_suppressed=False` is honest here and would not be on an AMC fact.
        """
        assert revenue_plan.logical.suppression_columns == ()

        result = adapter.run(QueryRequest(plan=revenue_plan, max_rows=10))

        assert result.suppression.rows_suppressed is False
        assert result.suppression.reason is None

    def test_an_amc_fact_reports_what_it_measured_rather_than_refusing(
        self, adapter: AthenaQueryAdapter, selector: PublicationSelector
    ) -> None:
        """The claim this platform must never make by accident, now established rather than dodged.

        `fct_attributed_conversion` can withhold rows. Answering `rows_suppressed=False` without
        looking would be a claim rather than an absence, so this used to refuse before spending a
        query. Since ADR-035 the flag is folded over each group, so `False` is *earned* — and a true
        flag always carries a reason, because `Suppression` refuses one without.
        """
        amc = _plan("attributed_revenue_14d", "amc-attributed-conversion", AMC_CONVERSION_FILTERS)
        assert amc.suppression_columns != ()

        result = adapter.run(QueryRequest(plan=_bind(selector, amc), max_rows=10))

        assert result.suppression.rows_suppressed is (result.suppression.reason is not None)


class TestFailuresBecomeDependencyErrors:
    def test_a_query_against_a_missing_table_translates(
        self, settings: Settings, revenue_plan: BoundQueryPlan
    ) -> None:
        """No SDK exception may cross into the domain (ADR-010).

        Pointed at a database that does not exist, so Athena rejects the statement itself.
        """
        wrong = settings.model_copy(
            update={"semantic_v2_current_database": "adlytics_no_such_database"}
        )

        with pytest.raises(DependencyError) as raised:
            AthenaQueryAdapter(_target(wrong)).run(QueryRequest(plan=revenue_plan, max_rows=10))

        assert raised.value.code == QUERY_FAILED

    def test_the_failure_names_no_infrastructure_in_its_description(
        self, settings: Settings, revenue_plan: BoundQueryPlan
    ) -> None:
        """A description can reach a caller; a query id and a database name cannot.

        Athena's own message names the database, so it has to stay in context.
        """
        wrong = settings.model_copy(
            update={"semantic_v2_current_database": "adlytics_no_such_database"}
        )

        with pytest.raises(DependencyError) as raised:
            AthenaQueryAdapter(_target(wrong)).run(QueryRequest(plan=revenue_plan, max_rows=10))

        assert "adlytics_no_such_database" not in raised.value.description
        assert "arn:" not in raised.value.description
        assert raised.value.context

    def test_an_unknown_workgroup_translates_at_submission(
        self, settings: Settings, revenue_plan: BoundQueryPlan
    ) -> None:
        """The submission path, which fails differently from the polling path."""
        wrong = settings.model_copy(update={"athena_workgroup": "no-such-workgroup"})

        with pytest.raises(DependencyError) as raised:
            AthenaQueryAdapter(_target(wrong)).run(QueryRequest(plan=revenue_plan, max_rows=10))

        assert raised.value.code == QUERY_FAILED
        assert "no-such-workgroup" not in raised.value.description


class TestResultReuse:
    def test_a_reuse_age_is_accepted_by_the_service(
        self, settings: Settings, revenue_plan: BoundQueryPlan
    ) -> None:
        """F-14's one item that CDK cannot express, so it is only ever verifiable here.

        `AWS::Athena::WorkGroup` has no result-reuse property, which makes this a per-query
        argument — and an argument the service could reject for a value out of range.
        """
        reusing = settings.model_copy(update={"athena_result_reuse_max_age_minutes": 60})

        result = AthenaQueryAdapter(_target(reusing)).run(
            QueryRequest(plan=revenue_plan, max_rows=10)
        )

        assert result.rows

    def test_an_identical_statement_is_answered_from_the_reused_result(
        self, settings: Settings, revenue_plan: BoundQueryPlan
    ) -> None:
        """Whether reuse actually *hits*, which nothing could previously observe.

        The test above proves only that Athena accepts the argument. Reuse was requested on every
        statement this platform has ever run, and until `ResultReuseInformation` was read there was
        no way to tell whether a single one was served from cache — so the setting was a hope.

        Asserted on the second run rather than by comparing the two. A previous run inside the
        60-minute window would make the first a hit as well, which is correct behaviour and would
        make a first-miss-then-hit assertion fail for the wrong reason.
        """
        reusing = settings.model_copy(update={"athena_result_reuse_max_age_minutes": 60})
        composed = compose(revenue_plan, 10)
        request = StatementRequest(
            sql=composed.sql,
            parameters=composed.parameters,
            workgroup=reusing.athena_workgroup,
            database=reusing.semantic_v2_current_database,
            reuse_minutes=60,
        )
        client = boto3.client("athena")

        first = await_completion(client, start(client, request), limit=MAX_WAIT_SECONDS)
        second = await_completion(client, start(client, request), limit=MAX_WAIT_SECONDS)

        assert second.reused_previous_result is True
        # A reused result is not re-scanned, which is the whole reason the setting exists.
        assert second.bytes_scanned == 0
        assert first.engine_millis >= 0
