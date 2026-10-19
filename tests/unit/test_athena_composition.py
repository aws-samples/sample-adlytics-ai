# SPDX-License-Identifier: MIT-0
"""The SQL one validated plan produces.

A pure string function, so the statement is assertable exactly — which is the reason
composition is separated from execution at all. The executing half needs a real workgroup and
is covered in the integration tier, per ADR-014.

The rule under test throughout: **interpolate identifiers, parameterise values.** SQL has no
placeholder for a table or column name, so identifiers are formatted into the text and that is
safe only because a `QueryPlan` took each one from a data contract. Everything a caller
supplied is a `?`.
"""

from __future__ import annotations

import pathlib
import re
from datetime import UTC, date, datetime

import pytest
from publication_support import bound_plan

from adapters.jobs.contract_validation.loader import load_contracts
from adapters.outbound.athena.composition import compose
from adapters.outbound.metric_dictionary import load_catalog
from domain.measurement.planning import FactShape, FactShapes, PlanRequest, plan
from domain.measurement.publications import BoundQueryPlan
from domain.shared_kernel.values import DateRange

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-024")]

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
WEEK = DateRange(date(2026, 8, 10), date(2026, 8, 16))
PUBLISHED_AT = datetime(2026, 8, 19, tzinfo=UTC)
ONE_POPULATION = {
    "marketplace_key": "ATVPDKIKX0DER",
    "distributor_view": "MANUFACTURING",
    "selling_program": "RETAIL",
}


@pytest.fixture(scope="module")
def revenue_plan() -> BoundQueryPlan:
    """Plan and bind the real target question from committed declarations."""
    contracts = {c.contract_id: c for _, c in load_contracts(ROOT)}
    logical = plan(
        load_catalog(ROOT),
        PlanRequest(
            metric_name="retail_revenue_shipped",
            grains=("product",),
            window=WEEK,
            filters=ONE_POPULATION,
        ),
        FactShape.of(contracts["vendor-sales-by-asin"]),
    )
    return bound_plan(logical, published_at=PUBLISHED_AT)


class TestTheStatementForTheTargetQuestion:
    def test_it_is_exactly_this(self, revenue_plan: BoundQueryPlan) -> None:
        """Asserted whole rather than in fragments.

        A statement assembled from correct-looking pieces can still be wrong overall — a
        missing GROUP BY column, a predicate on the wrong side of an AND — and only the whole
        string shows that.
        """
        composed = compose(revenue_plan, max_rows=100)

        assert composed.sql == (
            'SELECT "amazon_asin", "currency_code", SUM("revenue_shipped") AS value_0 '
            'FROM "fct_retail_sales" '
            'WHERE "date_key" BETWEEN CAST(? AS DATE) AND CAST(? AS DATE) '
            'AND "publication_id" IN (?, ?, ?, ?, ?, ?, ?) '
            'AND "distributor_view" = ? AND "marketplace_key" = ? '
            'AND "selling_program" = ? '
            'GROUP BY "amazon_asin", "currency_code" '
            'ORDER BY "amazon_asin" '
            "LIMIT 101"
        )

    def test_the_bound_values_are_in_statement_order(self, revenue_plan: BoundQueryPlan) -> None:
        """The nth `?` is the nth parameter, so order is correctness rather than tidiness."""
        composed = compose(revenue_plan, max_rows=100)

        assert composed.parameters == (
            "2026-08-10",
            "2026-08-16",
            *revenue_plan.primary_ids,
            "MANUFACTURING",
            "ATVPDKIKX0DER",
            "RETAIL",
        )

    def test_there_is_one_placeholder_per_parameter(self, revenue_plan: BoundQueryPlan) -> None:
        composed = compose(revenue_plan, max_rows=100)

        assert composed.sql.count("?") == len(composed.parameters)


class TestValuesNeverReachTheStatementText:
    def test_no_filter_value_appears_in_the_statement(self, revenue_plan: BoundQueryPlan) -> None:
        """The steering rule: never build a query string from a model-supplied value."""
        composed = compose(revenue_plan, max_rows=100)

        for supplied in ONE_POPULATION.values():
            assert supplied not in composed.sql

    def test_the_window_bounds_are_parameters_not_written_in(
        self, revenue_plan: BoundQueryPlan
    ) -> None:
        composed = compose(revenue_plan, max_rows=100)

        assert "2026-08-10" not in composed.sql
        assert "2026-08-16" not in composed.sql
        assert "2026-08-10" in composed.parameters

    def test_publication_ids_are_parameters_not_written_in(
        self, revenue_plan: BoundQueryPlan
    ) -> None:
        composed = compose(revenue_plan, max_rows=100)

        assert set(revenue_plan.primary_ids) <= set(composed.parameters)
        assert all(
            publication_id not in composed.sql for publication_id in revenue_plan.primary_ids
        )

    def test_a_hostile_filter_value_stays_a_parameter(self) -> None:
        """A value never reaches the text here, so composition cannot be subverted by one.

        The key is the half `plan()` must validate, because an identifier has to be
        interpolated. The value is carried as a parameter, and what makes *that* safe is
        `parameters.sql_literal` at the SDK boundary rather than anything in this module —
        Athena parses a parameter as an expression, so an unescaped one is not inert.
        """
        contracts = {c.contract_id: c for _, c in load_contracts(ROOT)}
        hostile = {**ONE_POPULATION, "marketplace_key": "' OR 1=1 --"}
        built = plan(
            load_catalog(ROOT),
            PlanRequest(
                metric_name="retail_revenue_shipped",
                grains=("product",),
                window=WEEK,
                filters=hostile,
            ),
            FactShape.of(contracts["vendor-sales-by-asin"]),
        )

        composed = compose(bound_plan(built, published_at=PUBLISHED_AT), max_rows=10)

        assert "OR 1=1" not in composed.sql
        assert "' OR 1=1 --" in composed.parameters
        # And the statement is byte-identical to the benign one bar nothing: the value is
        # simply absent from it.
        assert composed.sql.count("?") == len(composed.parameters)


class TestIdentifiersAreQuoted:
    def test_every_identifier_is_double_quoted(self, revenue_plan: BoundQueryPlan) -> None:
        """Not belt-and-braces over the validation — it is reserved-word protection.

        `date_key` is fine today. A future canonical column called `order` or `end` would
        parse as syntax without this.
        """
        composed = compose(revenue_plan, max_rows=100)

        for identifier in (
            "fct_retail_sales",
            "amazon_asin",
            "currency_code",
            "revenue_shipped",
            "date_key",
            "publication_id",
            *ONE_POPULATION,
        ):
            assert f'"{identifier}"' in composed.sql

    def test_the_measure_is_aggregated_and_aliased(self, revenue_plan: BoundQueryPlan) -> None:
        assert 'SUM("revenue_shipped") AS value' in compose(revenue_plan, max_rows=10).sql


class TestTruncationAndDeterminism:
    def test_it_asks_for_one_row_more_than_requested(self, revenue_plan: BoundQueryPlan) -> None:
        """The only way to tell "exactly this many" from "there were more".

        Asking for exactly `max_rows` makes a full page indistinguishable from a truncated
        one, and a partial answer presented as complete is the failure this platform exists
        to avoid.
        """
        assert compose(revenue_plan, max_rows=1).sql.endswith("LIMIT 2")
        assert compose(revenue_plan, max_rows=500).sql.endswith("LIMIT 501")

    def test_it_orders_by_the_reported_grain(self, revenue_plan: BoundQueryPlan) -> None:
        """Without ORDER BY, "the first 100 rows" is a different 100 each run."""
        assert 'ORDER BY "amazon_asin"' in compose(revenue_plan, max_rows=10).sql

    def test_the_statement_is_stable_for_one_plan(self, revenue_plan: BoundQueryPlan) -> None:
        """Result reuse cannot hit at all unless the same question produces the same text."""
        first = compose(revenue_plan, max_rows=100)
        second = compose(revenue_plan, max_rows=100)

        assert first == second

    def test_filter_order_does_not_depend_on_how_the_caller_wrote_them(self) -> None:
        contracts = {c.contract_id: c for _, c in load_contracts(ROOT)}
        shape = FactShape.of(contracts["vendor-sales-by-asin"])
        catalog = load_catalog(ROOT)
        reversed_filters = dict(reversed(list(ONE_POPULATION.items())))

        def statement(filters: dict[str, str]) -> str:
            return compose(
                bound_plan(
                    plan(
                        catalog,
                        PlanRequest(
                            metric_name="retail_revenue_shipped",
                            grains=("product",),
                            window=WEEK,
                            filters=filters,
                        ),
                        shape,
                    ),
                    published_at=PUBLISHED_AT,
                ),
                max_rows=100,
            ).sql

        assert statement(ONE_POPULATION) == statement(reversed_filters)


class TestNoDimensionJoin:
    def test_it_reads_one_table(self, revenue_plan: BoundQueryPlan) -> None:
        """`amazon_asin` is on the fact, so joining `dim_product` would add only fan-out.

        ADR-023 keeps `dim_product` as SCD2 with a validity range, so a join without a
        validity predicate returns one row per version of every product. A question needing a
        product *attribute* — brand, parent ASIN — needs that join and the predicate with it,
        and that is a different plan than any built today.
        """
        composed = compose(revenue_plan, max_rows=10)

        assert "JOIN" not in composed.sql.upper()
        assert len(re.findall(r"\bFROM\b", composed.sql)) == 1
        assert "dim_product" not in composed.sql


class TestMonetaryDrillAcrossCarriesCurrency:
    @staticmethod
    def _bound(metric_name: str) -> BoundQueryPlan:
        contracts = [contract for _, contract in load_contracts(ROOT)]
        facts = {
            "revenue_per_glance_view": ("fct_retail_sales", "fct_product_page_views"),
            "cost_per_purchase": ("fct_ad_exposure", "fct_attributed_conversion"),
        }
        numerator, denominator = facts[metric_name]
        filters = (
            ONE_POPULATION
            if metric_name == "revenue_per_glance_view"
            else {
                "marketplace_key": "ATVPDKIKX0DER",
                "channel_key": "sponsored_brands",
                "ad_product_key": "sponsored_brands",
            }
        )
        logical = plan(
            load_catalog(ROOT),
            PlanRequest(
                metric_name=metric_name,
                grains=("date",),
                window=WEEK,
                filters=filters,
            ),
            FactShapes(
                numerator=FactShape.for_fact(numerator, contracts),
                denominator=FactShape.for_fact(denominator, contracts),
            ),
        )
        return bound_plan(logical, published_at=PUBLISHED_AT)

    def test_one_sided_currency_is_selected_before_aggregates(self) -> None:
        bound = self._bound("revenue_per_glance_view")
        composed = compose(bound, max_rows=10)

        assert 's0."currency_code" AS "currency_code", s0.value_0' in composed.sql
        assert 'GROUP BY "date_key", "currency_code"' in composed.sql
        assert bound.logical.selected_columns == ("date_key", "currency_code")

    def test_two_sided_currency_is_part_of_the_join_key(self) -> None:
        bound = self._bound("cost_per_purchase")
        composed = compose(bound, max_rows=10)

        assert 'COALESCE(s0."currency_code", s1."currency_code") AS "currency_code"' in composed.sql
        assert 's0."currency_code" = s1."currency_code"' in composed.sql
        assert composed.sql.count('GROUP BY "date_key", "currency_code"') == 2

    def test_model_selector_is_parameterized_once_on_the_numerator(self) -> None:
        contracts = [contract for _, contract in load_contracts(ROOT)]
        model_key = "first_touch_v1"
        logical = plan(
            load_catalog(ROOT),
            PlanRequest(
                metric_name="roas_deduplicated_incl_halo_14d",
                grains=("date",),
                window=WEEK,
                filters={
                    "marketplace_key": "ATVPDKIKX0DER",
                    "channel_key": "amazon_dsp",
                    "ad_product_key": "amazon_dsp",
                    "attribution_model_key": model_key,
                    "rows_suppressed": "false",
                },
            ),
            FactShapes(
                numerator=FactShape.for_fact("fct_deduplicated_temporal_attribution", contracts),
                denominator=FactShape.for_fact("fct_ad_exposure", contracts),
            ),
        )
        composed = compose(bound_plan(logical, published_at=PUBLISHED_AT), max_rows=10)

        assert composed.sql.count('"attribution_model_key" = ?') == 1
        assert composed.parameters.count(model_key) == 1
        numerator_sql, denominator_sql = composed.sql.split(" FULL JOIN ", 1)
        assert '"attribution_model_key" = ?' in numerator_sql
        assert '"attribution_model_key" = ?' not in denominator_sql

    def test_suppression_fold_preserves_null_when_no_attributable_row_exists(self) -> None:
        composed = compose(self._bound("cost_per_purchase"), max_rows=10)

        assert "ELSE 0 END) AS value_" not in composed.sql
        assert composed.sql.count("END) AS value_") == 2
        assert composed.sql.count("ELSE 0 END) AS withheld_") == 4
