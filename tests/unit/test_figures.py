# SPDX-License-Identifier: MIT-0
"""Reading query rows into figures that carry their own units.

SQL does the summing. What is tested here is everything that decides whether a returned
number is safe to present: that it exists, that it is an exact quantity, that a monetary
amount states its currency, and that a set of amounts does not quietly span two.

Every assertion states units and grain alongside the number, because a correct figure at the
wrong grain — or in the wrong currency — is the failure mode this product must not have.

Rows are built as strings for the decimal columns, which is what a query engine actually
hands back over an API. Accepting them is the point; accepting them through `float` would
lose the value.
"""

from __future__ import annotations

import pathlib
from dataclasses import FrozenInstanceError, replace
from datetime import date
from decimal import Decimal

import pytest

from adapters.jobs.contract_validation.loader import load_contracts
from adapters.outbound.athena.query import (
    _suppression_from,  # pyright: ignore[reportPrivateUsage]
)
from adapters.outbound.metric_dictionary import load_catalog
from domain.catalog.capability import Catalog
from domain.measurement.figures import (
    MEASURE_ABSENT,
    MEASURE_NOT_A_QUANTITY,
    MIXED_CURRENCY,
    Figure,
    Measurement,
    Withholding,
    measure,
    withholding_in,
)
from domain.measurement.planning import (
    CURRENCY_COLUMN,
    FactShape,
    FactShapes,
    PlanRequest,
    QueryPlan,
    measure_alias,
    plan,
    withheld_alias,
    withheld_rows_alias,
)
from domain.narrative.shaped_response import UnitKind
from domain.shared_kernel.errors import InvariantError
from domain.shared_kernel.provenance import Suppression, WithheldAmount
from domain.shared_kernel.values import DateRange, Money

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-004", "ADR-010", "ADR-021")]

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
WEEK = DateRange(date(2026, 8, 10), date(2026, 8, 16))
ONE_POPULATION = {
    "marketplace_key": "ATVPDKIKX0DER",
    "distributor_view": "MANUFACTURING",
    "selling_program": "RETAIL",
}


@pytest.fixture(scope="module")
def revenue_plan() -> QueryPlan:
    """Plan the real target question against the committed dictionary and contracts."""
    catalog = load_catalog(ROOT)
    contracts = {c.contract_id: c for _, c in load_contracts(ROOT)}
    sales = contracts["vendor-sales-by-asin"]
    return plan(
        catalog,
        PlanRequest(
            metric_name="retail_revenue_shipped",
            grains=("product",),
            window=WEEK,
            filters=ONE_POPULATION,
        ),
        FactShape.of(sales),
    )


# Spelled out rather than borrowed from `ports.outbound.query.QueryRow`: the domain cannot
# import from ports, so `measure` declares this shape itself and a test must match it.
#
# A plain assignment, not a `type` statement: check-coverage-matrix runs under system
# python and ast.parses every test file, so 3.12+ syntax breaks that gate.
Row = dict[str, str | int | Decimal | None]


@pytest.fixture(scope="module")
def catalog() -> Catalog:
    return load_catalog(ROOT)


@pytest.fixture(scope="module")
def conversion() -> FactShape:
    """Read the real shape of `fct_attributed_conversion`, which carries the halo columns."""
    contracts = {c.contract_id: c for _, c in load_contracts(ROOT)}
    return FactShape.of(contracts["amc-attributed-conversion"])


def _row(asin: str, amount: str | None, currency: str | None = "USD") -> Row:
    """Build one row in the shape a query actually returns it.

    Each aggregate arrives under its own numbered alias, not under the contract column it was
    aggregated from — the statement is `SUM(revenue_shipped) AS value_0`. This fixture used
    `revenue_shipped` and so did `measure()`, and both were self-consistently wrong: the first
    real result put through this path failed on a column the query never returned.

    `MEASURE_ALIAS` is the *output* name a combined figure is emitted under, which is why it is
    not the key here.
    """
    row: Row = {"amazon_asin": asin, measure_alias(0): amount}
    if currency is not None:
        row[CURRENCY_COLUMN] = currency
    return row


class TestThePlanAsksForTheCurrency:
    def test_a_monetary_metric_carries_the_currency_column(self, revenue_plan: QueryPlan) -> None:
        """`currency_code` is on the fact and in the grain of none of them.

        So a plan has to ask for it deliberately. One that did not would leave a tool
        labelling an amount by assuming which currency a marketplace uses.
        """
        assert revenue_plan.carried_columns == (CURRENCY_COLUMN,)

    def test_it_is_carried_rather_than_grouped(self, revenue_plan: QueryPlan) -> None:
        """It labels the figure; it does not slice it.

        Putting it in the reported grain would describe a breakdown nobody asked for.
        """
        assert CURRENCY_COLUMN not in revenue_plan.group_by
        assert str(revenue_plan.grain) == "amazon_asin"
        assert revenue_plan.selected_columns == ("amazon_asin", CURRENCY_COLUMN)


class TestReadingFigures:
    def test_rows_become_figures_with_units_and_grain(self, revenue_plan: QueryPlan) -> None:
        result = measure(revenue_plan, (_row("B0TRAP00001", "100.0000"),))

        assert result.row_count == 1
        assert result.units is not None
        assert result.units.kind is UnitKind.CURRENCY
        assert result.units.currency_code == "USD"
        assert str(result.grain) == "amazon_asin"

    def test_the_total_states_its_currency(self, revenue_plan: QueryPlan) -> None:
        """A bare number is never enough: the scale and the currency have to travel with it."""
        result = measure(
            revenue_plan,
            (_row("B0TRAP00001", "100.0000"), _row("B0TRAP00002", "25.5000")),
        )

        assert result.total() == Money(Decimal("125.5000"), "USD")

    def test_a_decimal_string_is_read_exactly_and_never_through_a_float(
        self, revenue_plan: QueryPlan
    ) -> None:
        """0.1 + 0.2 is 0.3 here. Through binary floats it is not."""
        result = measure(revenue_plan, (_row("A", "0.1000"), _row("B", "0.2000")))
        total = result.total()

        assert isinstance(total, Money)
        assert total.amount == Decimal("0.3000")

    def test_a_negative_amount_survives_with_its_sign(self, revenue_plan: QueryPlan) -> None:
        """Vendor cancellations make revenue genuinely negative; there is no abs() anywhere."""
        result = measure(revenue_plan, (_row("B0TRAP00001", "-45.5000"),))

        assert result.total() == Money(Decimal("-45.5000"), "USD")

    def test_a_figure_flattens_into_the_row_shape_the_envelope_carries(
        self, revenue_plan: QueryPlan
    ) -> None:
        result = measure(revenue_plan, (_row("B0TRAP00001", "100.0000"),))

        assert result.figures[0].labelled() == {
            "amazon_asin": "B0TRAP00001",
            "value": Decimal("100.0000"),
        }


class TestNullIsNeverZero:
    def test_a_null_measure_is_refused_rather_than_read_as_zero(
        self, revenue_plan: QueryPlan
    ) -> None:
        """`revenue_shipped` is non-nullable in its contract.

        So a null means the query or the load is wrong, and a zero would report a product
        that sold as having sold nothing — plausible, and false.
        """
        with pytest.raises(InvariantError) as raised:
            measure(revenue_plan, (_row("B0TRAP00001", None),))

        assert raised.value.code == MEASURE_ABSENT
        assert raised.value.context["column"] == "revenue_shipped"

    def test_a_missing_dimension_is_refused_naming_the_column(
        self, revenue_plan: QueryPlan
    ) -> None:
        with pytest.raises(InvariantError) as raised:
            bad: Row = {"revenue_shipped": "1.0000", CURRENCY_COLUMN: "USD"}
            measure(revenue_plan, (bad,))

        assert raised.value.context["column"] == "amazon_asin"

    def test_no_rows_at_all_is_a_legitimate_answer(self, revenue_plan: QueryPlan) -> None:
        """ADR-010 makes "we looked and there was nothing there" a valid response."""
        result = measure(revenue_plan, ())

        assert result.is_empty is True
        assert result.row_count == 0

    def test_a_result_with_figures_does_not_claim_to_be_empty(
        self, revenue_plan: QueryPlan
    ) -> None:
        """Asserted by identity, because the failure mode is a bound method and those are truthy.

        `answers.py` feeds `is_empty` into the narrative, so an always-true one would tell every
        caller the window held nothing. Mutation review found the truthiness assertion above
        passing on exactly that.
        """
        result = measure(revenue_plan, (_row("B0TRAP00001", "100.0000"),))

        assert result.is_empty is False
        assert result.row_count == 1

    def test_an_empty_result_has_no_units_because_there_is_nothing_to_denominate(
        self, revenue_plan: QueryPlan
    ) -> None:
        """The currency of an answer comes from the rows, since that is where it is stated.

        With no rows there is no amount, and inventing a currency — from
        `dim_marketplace.default_currency_code` or from whatever seemed likely — would be the
        invented figure this platform refuses.
        """
        assert measure(revenue_plan, ()).units is None

    def test_totalling_nothing_raises_rather_than_returning_zero(
        self, revenue_plan: QueryPlan
    ) -> None:
        with pytest.raises(InvariantError, match="Nothing to total"):
            measure(revenue_plan, ()).total()


class TestCurrencyMustBeUnambiguous:
    def test_two_currencies_in_one_result_are_refused(self, revenue_plan: QueryPlan) -> None:
        """No single total is correct, so none is produced.

        Labelling the sum `USD` when it silently includes euros is exactly the plausible
        wrong number this platform exists to avoid. Conversion is not available either: the
        only rate at hand is today's, and ADR-004 requires the rate as of the fact's date.
        """
        with pytest.raises(InvariantError) as raised:
            measure(
                revenue_plan,
                (_row("A", "100.0000", "USD"), _row("B", "25.5000", "EUR")),
            )

        assert raised.value.code == MIXED_CURRENCY
        assert raised.value.context["currencies"] == ["EUR", "USD"]

    def test_a_monetary_row_without_a_currency_is_refused(self, revenue_plan: QueryPlan) -> None:
        """Which is what happens if a plan forgets to carry the column."""
        with pytest.raises(InvariantError) as raised:
            measure(revenue_plan, (_row("A", "100.0000", currency=None),))

        assert raised.value.code == MEASURE_ABSENT
        assert raised.value.context["column"] == CURRENCY_COLUMN

    def test_excess_precision_is_classified_rather_than_escaping_as_a_value_error(
        self, revenue_plan: QueryPlan
    ) -> None:
        """`Money` raises ValueError because it is a value object; that must not escape here.

        An inbound adapter would map a bare ValueError to an unclassified failure and lose
        the reason. Translating it at read time also means `total()` cannot raise, and the
        error can name the row that was wrong.
        """
        with pytest.raises(InvariantError) as raised:
            measure(revenue_plan, (_row("B0TRAP00001", "1.234567"),))

        assert raised.value.code == MEASURE_NOT_A_QUANTITY
        assert raised.value.context["dimensions"] == {"amazon_asin": "B0TRAP00001"}
        assert raised.value.context["cause"] == "invalid_money"
        assert "1.234567" not in str(raised.value.context)

    def test_units_cannot_be_absent_while_figures_exist(self, revenue_plan: QueryPlan) -> None:
        """The invariant that keeps the empty case from becoming an unlabelled figure."""
        with pytest.raises(InvariantError, match="no units to state them in"):
            Measurement(
                figures=(Figure(dimensions=(("amazon_asin", "A"),), value=Decimal(1)),),
                units=None,
                grain=revenue_plan.grain,
            )


class TestCombiningAggregates:
    """The arithmetic a plan's aggregates go through, and what it refuses to invent.

    This is where a derived figure becomes correct or plausible-but-wrong, so each case pins a
    specific claim: that division happens on the sums rather than on per-row quotients, that a
    scalar in a metric's name is in its declaration, and that a zero denominator produces no
    figure at all rather than a zero.
    """

    @staticmethod
    def _plan(catalog: Catalog, shape: FactShape, metric_name: str) -> QueryPlan:
        return plan(
            catalog,
            PlanRequest(
                metric_name=metric_name,
                grains=("product",),
                window=WEEK,
                filters={
                    "marketplace_key": "ATVPDKIKX0DER",
                    "channel_key": "DSP",
                    "campaign_key": "C1",
                    "rows_suppressed": "false",
                },
            ),
            shape,
        )

    @staticmethod
    def _pair(asin: str, operands: tuple[str, str]) -> Row:
        """Build a row carrying two aggregates, under the aliases the statement gives them."""
        left, right = operands
        return {
            "amazon_asin": asin,
            measure_alias(0): Decimal(left),
            measure_alias(1): Decimal(right),
        }

    def test_a_ratio_divides_the_sums(self, catalog: Catalog, conversion: FactShape) -> None:
        """The ratio of sums, not the mean of ratios.

        They diverge most where the denominators are least even, which is exactly where
        somebody is most likely to be looking.
        """
        built = self._plan(catalog, conversion, "ntb_rate_12mo")
        result = measure(built, (self._pair("B01", ("25", "100")),))
        assert result.figures[0].value == Decimal("0.2500")

    def test_a_quotient_is_rounded_to_the_canonical_scale(
        self, catalog: Catalog, conversion: FactShape
    ) -> None:
        """Four places, so a tool never hands a model a repeating decimal to shorten itself."""
        built = self._plan(catalog, conversion, "ntb_rate_12mo")
        result = measure(built, (self._pair("B01", ("1", "3")),))
        assert result.figures[0].value == Decimal("0.3333")

    def test_a_zero_denominator_produces_no_figure(
        self, catalog: Catalog, conversion: FactShape
    ) -> None:
        """A campaign with no purchases did not achieve a rate of zero.

        Zero would be a claim about performance; infinity is not a number a figure can carry.
        """
        built = self._plan(catalog, conversion, "ntb_rate_12mo")
        result = measure(built, (self._pair("B01", ("5", "0")),))
        assert result.figures == ()
        assert result.undefined_rows == 1

    def test_an_undefined_row_does_not_hide_among_the_defined_ones(
        self, catalog: Catalog, conversion: FactShape
    ) -> None:
        """Counted, because a breakdown that silently drops rows does not add up to its total."""
        built = self._plan(catalog, conversion, "ntb_rate_12mo")
        result = measure(built, (self._pair("B01", ("25", "100")), self._pair("B02", ("5", "0"))))
        assert result.row_count == 1
        assert result.undefined_rows == 1

    def test_a_difference_of_sums_subtracts_in_declaration_order(
        self, catalog: Catalog, conversion: FactShape
    ) -> None:
        """Halo is the total minus the advertised-only portion, so order is the whole figure."""
        built = self._plan(catalog, conversion, "attributed_revenue_halo_14d")
        row = {**self._pair("B01", ("500.0000", "380.0000")), CURRENCY_COLUMN: "USD"}
        assert measure(built, (row,)).figures[0].value == Decimal("120.0000")

    def test_a_multiplier_scales_the_quotient(self, catalog: Catalog) -> None:
        """`cpm` is cost per thousand, and 140.5 over 48210 is 2.9143 rather than 0.0029."""
        contracts = {c.contract_id: c for _, c in load_contracts(ROOT)}
        exposure = FactShape.of(contracts["amc-dsp-impressions"])
        built = plan(
            catalog,
            PlanRequest(
                metric_name="cpm",
                grains=("campaign",),
                window=WEEK,
                filters={
                    "marketplace_key": "ATVPDKIKX0DER",
                    "channel_key": "DSP",
                    "ad_product_key": "DSP",
                    "rows_suppressed": "false",
                },
            ),
            exposure,
        )
        row: Row = {
            "campaign_key": "C1",
            CURRENCY_COLUMN: "USD",
            measure_alias(0): Decimal("140.5"),
            measure_alias(1): Decimal("48210"),
        }
        assert measure(built, (row,)).figures[0].value == Decimal("2.9143")


class TestAWhollyWithheldSingleFactGroup:
    @staticmethod
    def _plan(catalog: Catalog) -> QueryPlan:
        contracts = [contract for _, contract in load_contracts(ROOT)]
        return plan(
            catalog,
            PlanRequest(
                metric_name="ad_spend",
                grains=("campaign",),
                window=WEEK,
                filters={
                    "marketplace_key": "ATVPDKIKX0DER",
                    "channel_key": "DSP",
                    "ad_product_key": "DSP",
                    "rows_suppressed": "false",
                },
            ),
            FactShape.for_fact("fct_ad_exposure", contracts),
        )

    @staticmethod
    def _row(value: Decimal | None) -> Row:
        return {
            "campaign_key": "C1",
            CURRENCY_COLUMN: "USD",
            measure_alias(0): value,
            withheld_rows_alias(0): 1,
        }

    def test_null_attributable_sum_is_unknown_instead_of_zero(self, catalog: Catalog) -> None:
        result = measure(self._plan(catalog), (self._row(None),))

        assert result.figures == ()
        assert result.units is None
        assert str(result.grain) == "campaign_key"
        assert result.undefined_rows == 1

    def test_known_zero_remains_reportable_when_another_row_was_withheld(
        self, catalog: Catalog
    ) -> None:
        result = measure(self._plan(catalog), (self._row(Decimal(0)),))

        assert result.figures[0].value == Decimal(0)
        assert result.units is not None
        assert result.units.kind is UnitKind.CURRENCY
        assert result.units.currency_code == "USD"
        assert str(result.grain) == "campaign_key"


class TestADrillAcrossRowResolvesOrRefuses:
    """What an absent side of a joined ratio means, which is the decision option C settles.

    Two cases look identical in the data under a naive join and demand opposite answers. A
    campaign with spend and **no conversion rows at all** genuinely earned no attributed revenue,
    so ROAS zero is true and is the row somebody asking about wasted budget most needs. A campaign
    whose conversion rows were **all suppressed** has an unknown numerator, and reporting zero
    there would read as a wasted budget when it may have been the best performer.

    The full join keeps both rather than dropping either, and the withheld-row count is the only
    thing in the row that tells them apart. A count rather than a flag since ADR-035, because a
    withheld measure can sum to zero and an amount of zero cannot distinguish the two.
    """

    @staticmethod
    def _plan(catalog: Catalog) -> QueryPlan:
        contracts = [c for _, c in load_contracts(ROOT)]
        return plan(
            catalog,
            PlanRequest(
                metric_name="roas_attributed_14d",
                grains=("campaign",),
                window=WEEK,
                filters={
                    "marketplace_key": "ATVPDKIKX0DER",
                    "channel_key": "DSP",
                    "ad_product_key": "DSP",
                    "rows_suppressed": "false",
                },
            ),
            FactShapes(
                numerator=FactShape.for_fact("fct_attributed_conversion", contracts),
                denominator=FactShape.for_fact("fct_ad_exposure", contracts),
            ),
        )

    @staticmethod
    def _row(revenue: tuple[str | None, int | None], spend: tuple[str | None, int | None]) -> Row:
        return {
            "campaign_key": "C1",
            measure_alias(0): None if revenue[0] is None else Decimal(revenue[0]),
            withheld_rows_alias(0): revenue[1],
            measure_alias(1): None if spend[0] is None else Decimal(spend[0]),
            withheld_rows_alias(1): spend[1],
        }

    def test_both_sides_present_divides_the_sums(self, catalog: Catalog) -> None:
        result = measure(self._plan(catalog), (self._row(("400", 0), ("100", 0)),))
        assert result.figures[0].value == Decimal("4.0000")

    def test_no_rows_on_the_numerator_side_is_a_genuine_zero(self, catalog: Catalog) -> None:
        """Spent and earned nothing attributed. The wasted-spend answer, and it is true."""
        result = measure(self._plan(catalog), (self._row((None, None), ("100", 0)),))
        assert result.figures[0].value == Decimal("0.0000")

    def test_a_suppressed_numerator_produces_no_figure(self, catalog: Catalog) -> None:
        """A null attributable aggregate with withheld rows is unknown, never zero."""
        result = measure(self._plan(catalog), (self._row((None, 1), ("100", 0)),))
        assert result.figures == ()
        assert result.undefined_rows == 1

    def test_a_partially_suppressed_known_zero_remains_a_figure(self, catalog: Catalog) -> None:
        """A real attributable zero survives when other rows in the group were withheld."""
        result = measure(self._plan(catalog), (self._row(("0", 1), ("100", 0)),))
        assert result.figures[0].value == Decimal("0.0000")
        assert result.units is not None
        assert result.units.kind is UnitKind.RATIO
        assert str(result.grain) == "campaign_key"

    def test_a_zero_denominator_produces_no_figure(self, catalog: Catalog) -> None:
        result = measure(self._plan(catalog), (self._row(("400", 0), ("0", 0)),))
        assert result.figures == ()

    def test_no_rows_on_the_denominator_side_produces_no_figure(self, catalog: Catalog) -> None:
        """Revenue attributed with no spend at all is undefined, not infinite."""
        result = measure(self._plan(catalog), (self._row(("400", 0), (None, None)),))
        assert result.figures == ()

    def test_a_suppressed_denominator_produces_no_figure(self, catalog: Catalog) -> None:
        result = measure(self._plan(catalog), (self._row(("400", 0), ("0", 1)),))
        assert result.figures == ()

    def test_the_undefined_rows_are_counted_beside_the_defined_ones(self, catalog: Catalog) -> None:
        """Counted so the narrative can say so. A breakdown that drops rows silently misleads."""
        result = measure(
            self._plan(catalog),
            (
                self._row(("400", 0), ("100", 0)),
                self._row((None, None), ("100", 0)),
                self._row((None, 1), ("100", 0)),
                self._row(("400", 0), ("0", 0)),
            ),
        )
        assert (result.row_count, result.undefined_rows) == (2, 2)

    def test_a_cross_fact_quotient_keeps_its_fraction(self, catalog: Catalog) -> None:
        """Every real ROAS divides unevenly, so a floored quotient would truncate all of them.

        The existing cases all divide exactly, which cannot tell true division from floor
        division. Mutation review (ADR-013) found that.
        """
        result = measure(self._plan(catalog), (self._row(("400", 0), ("3", 0)),))

        assert result.figures[0].value == Decimal("133.3333")
        assert result.units is not None
        assert result.units.kind is UnitKind.RATIO
        assert str(result.grain) == "campaign_key"

    def test_a_multiplier_scales_a_cross_fact_quotient(self, catalog: Catalog) -> None:
        """Pinned before a scaled cross-fact metric exists, not after one reports a wrong figure.

        No cross-fact metric declares a multiplier yet, but a ratio is permitted one and this is
        the arithmetic it would go through. Unlike the single-aggregate path, where the catalog
        refuses a scaled simple metric outright, nothing here forbids it.
        """
        scaled = replace(self._plan(catalog), multiplier=1000)
        result = measure(scaled, (self._row(("400", 0), ("100", 0)),))

        assert result.figures[0].value == Decimal("4000.0000")
        assert result.units is not None
        assert result.units.kind is UnitKind.RATIO


@pytest.mark.covers("ADR-037")
class TestAMeasureNoWriterSupplied:
    """`fct_ad_exposure` is written by four contracts, so a row carries only its writer's columns.

    Every one of the fifty metrics plans, and six of them raised `InvariantError` the first time
    they were executed against real data: `SUM` over a group whose every contributing row came from
    a different writer returns NULL, and a NULL measure was treated as the model and the data
    disagreeing. It is not — it is a delivery that was never made. ADR-037.

    The distinction cannot be detected from the rows, because data loss and a column this writer
    never supplied look identical. So it is declared from the contracts, which is what
    `partially_written_columns` carries.
    """

    @staticmethod
    def _exposure() -> FactShape:
        """Read the real four-contract shape, rather than constructing one.

        Built with `for_fact` deliberately: which columns are partially written is computed from
        how many contracts write the fact, so a hand-made shape would be asserting against this
        test's own idea of the answer rather than against the contracts.
        """
        return FactShape.for_fact("fct_ad_exposure", [c for _, c in load_contracts(ROOT)])

    @staticmethod
    def _plan(catalog: Catalog, shape: FactShape, metric_name: str) -> QueryPlan:
        return plan(
            catalog,
            PlanRequest(
                metric_name=metric_name,
                grains=("date",),
                window=WEEK,
                filters={
                    "marketplace_key": "ATVPDKIKX0DER",
                    "channel_key": "DSP",
                    "campaign_key": "C1",
                    "ad_product_key": "DSP",
                },
            ),
            shape,
        )

    def test_the_viewability_columns_are_declared_partially_written(self) -> None:
        """Only `amc-dsp-views` writes them, so three of the four writers leave them null."""
        partial = self._exposure().partially_written_columns

        assert "viewable_impressions" in partial
        assert "measurable_impressions" in partial
        # The grain is written by every contract, so it is never partial. A grain column going
        # missing is still a genuine invariant breach and must keep raising.
        assert "date_key" not in partial
        assert "marketplace_key" not in partial

    def test_a_single_writer_fact_has_none(self, conversion: FactShape) -> None:
        """The invariant is preserved everywhere it was already right.

        Seven of the eight curated facts have one writer. For those, a null measure really is the
        model and the data disagreeing, and this fix must not soften that.
        """
        assert conversion.partially_written_columns == ()

    def test_a_group_no_writer_reported_yields_no_figure_rather_than_raising(
        self, catalog: Catalog
    ) -> None:
        """The six failing metrics, reduced to one assertion.

        A day on which nobody delivered viewability has no viewable-impression figure. Not zero —
        that would claim the impressions were measured and none were viewable.
        """
        built = self._plan(catalog, self._exposure(), "viewable_impressions")
        result = measure(built, ({"date_key": "2026-08-10", measure_alias(0): None},))

        assert result.figures == ()
        assert result.undefined_rows == 1
        assert result.units is None

    def test_a_reported_group_beside_an_unreported_one_still_produces_its_figure(
        self, catalog: Catalog
    ) -> None:
        """Dropping the silent group must not discard the measured one."""
        built = self._plan(catalog, self._exposure(), "viewable_impressions")
        result = measure(
            built,
            (
                {"date_key": "2026-08-10", measure_alias(0): 8533},
                {"date_key": "2026-08-11", measure_alias(0): None},
            ),
        )

        assert result.row_count == 1
        assert result.undefined_rows == 1
        assert result.figures[0].value == 8533
        assert result.grain.dimensions == ("date_key",)

    def test_a_null_measure_on_a_single_writer_fact_still_raises(
        self, catalog: Catalog, conversion: FactShape
    ) -> None:
        """The half of the old behaviour that was correct, pinned so it cannot be lost.

        One contract writes `fct_attributed_conversion`, and it declares its measures
        non-nullable. A null there is a real disagreement between the model and the data, and
        reporting it as a missing delivery would hide a broken load.
        """
        built = plan(
            catalog,
            PlanRequest(
                metric_name="units_attributed",
                grains=("date",),
                window=WEEK,
                # `amazon_asin` is in this fact's grain and not in `fct_ad_exposure`'s, so it is
                # pinned here rather than in the shared helper.
                filters={
                    "marketplace_key": "ATVPDKIKX0DER",
                    "channel_key": "DSP",
                    "campaign_key": "C1",
                    "amazon_asin": "B01",
                },
            ),
            conversion,
        )
        with pytest.raises(InvariantError) as raised:
            measure(built, ({"date_key": "2026-08-10", measure_alias(0): None},))

        assert raised.value.code == MEASURE_ABSENT


@pytest.mark.covers("ADR-037")
class TestACrossFactMonetaryMetricStatesItsCurrency:
    """`cost_per_purchase` and `cost_per_ntb` are the only cross-fact metrics denominated in money.

    Every other drill-across is a ratio, which needs no currency — so `carried_columns` was
    unconditionally empty on that path and nothing noticed until all fifty metrics were executed.
    Both planned cleanly and both raised on execution, because no returned row could say what
    currency it was in. ADR-037.
    """

    @staticmethod
    def _shapes() -> FactShapes:
        contracts = [c for _, c in load_contracts(ROOT)]
        return FactShapes(
            numerator=FactShape.for_fact("fct_ad_exposure", contracts),
            denominator=FactShape.for_fact("fct_attributed_conversion", contracts),
        )

    def _plan(self, catalog: Catalog, metric_name: str) -> QueryPlan:
        return plan(
            catalog,
            PlanRequest(
                metric_name=metric_name,
                grains=("campaign",),
                window=WEEK,
                filters={
                    "marketplace_key": "ATVPDKIKX0DER",
                    "channel_key": "DSP",
                    "ad_product_key": "DSP",
                },
            ),
            self._shapes(),
        )

    @pytest.mark.parametrize("metric_name", ["cost_per_purchase", "cost_per_ntb"])
    def test_the_plan_carries_currency_from_both_monetary_sides(
        self, catalog: Catalog, metric_name: str
    ) -> None:
        built = self._plan(catalog, metric_name)

        assert built.carried_columns == (CURRENCY_COLUMN,)
        assert built.primary_carried_columns == (CURRENCY_COLUMN,)
        assert built.join is not None
        assert built.join.carried_columns == (CURRENCY_COLUMN,)

    def test_the_arithmetic_is_right_once_a_currency_reaches_it(self, catalog: Catalog) -> None:
        """Separating the two faults, so fixing G-35 has something to aim at.

        The division and the units are correct; what is missing is only the column's delivery. A
        row is handed one directly here, which is what the composer will eventually produce.
        """
        built = replace(self._plan(catalog, "cost_per_purchase"), units=UnitKind.CURRENCY)
        result = measure(
            built,
            (
                {
                    "campaign_key": "C1",
                    CURRENCY_COLUMN: "USD",
                    measure_alias(0): Decimal("100.0000"),
                    measure_alias(1): 25,
                },
            ),
        )

        assert result.units is not None
        assert result.units.kind is UnitKind.CURRENCY
        assert result.units.currency_code == "USD"
        assert result.figures[0].value == Decimal("4.0000")

    def test_absent_monetary_side_needs_no_currency_for_an_undefined_row(
        self, catalog: Catalog
    ) -> None:
        contracts = [c for _, c in load_contracts(ROOT)]
        built = plan(
            catalog,
            PlanRequest(
                metric_name="revenue_per_glance_view",
                grains=("date",),
                window=WEEK,
                filters={
                    "marketplace_key": "ATVPDKIKX0DER",
                    "distributor_view": "MANUFACTURING",
                    "selling_program": "RETAIL",
                },
            ),
            FactShapes(
                numerator=FactShape.for_fact("fct_retail_sales", contracts),
                denominator=FactShape.for_fact("fct_product_page_views", contracts),
            ),
        )

        result = measure(
            built,
            (
                {
                    "date_key": "2026-08-11",
                    CURRENCY_COLUMN: None,
                    measure_alias(0): None,
                    measure_alias(1): Decimal("170"),
                },
            ),
        )

        assert result.figures == ()
        assert result.units is None
        assert result.undefined_rows == 1


@pytest.mark.covers("ADR-037")
class TestASilentCurrencyOnASingleFactQuery:
    """Two of `fct_ad_exposure`'s four writers supply `currency_code`, so a group can lack it.

    The single-fact composer does select carried columns, so such a group arrives as its own row
    with a null currency. Refusing on it discarded every other group in the answer — the whole
    question became an alarm over one row that was never going to produce a figure.
    """

    @staticmethod
    def _plan(catalog: Catalog) -> QueryPlan:
        return plan(
            catalog,
            PlanRequest(
                metric_name="cpm",
                grains=("date",),
                window=WEEK,
                filters={
                    "marketplace_key": "ATVPDKIKX0DER",
                    "channel_key": "DSP",
                    "campaign_key": "C1",
                    "ad_product_key": "DSP",
                },
            ),
            FactShape.for_fact("fct_ad_exposure", [c for _, c in load_contracts(ROOT)]),
        )

    @staticmethod
    def _row(date_key: str, currency: str | None, spend: Decimal | None) -> Row:
        return {
            "date_key": date_key,
            CURRENCY_COLUMN: currency,
            measure_alias(0): spend,
            withheld_rows_alias(0): 0,
            measure_alias(1): 1000,
            withheld_rows_alias(1): 0,
        }

    def test_the_currency_comes_from_the_rows_that_stated_one(self, catalog: Catalog) -> None:
        result = measure(
            self._plan(catalog),
            (
                self._row("2026-08-10", "USD", Decimal("40.0000")),
                self._row("2026-08-11", None, None),
            ),
        )

        assert result.units is not None
        assert result.units.currency_code == "USD"
        assert result.row_count == 1
        assert result.undefined_rows == 1

    def test_no_row_stating_a_currency_yields_no_units_rather_than_an_error(
        self, catalog: Catalog
    ) -> None:
        """Nothing to denominate, so the same absence an empty monetary result reports.

        Not an error: a row with no currency also carried no amount, so no figure was going to
        survive and there is nothing for a currency to label.
        """
        result = measure(self._plan(catalog), (self._row("2026-08-11", None, None),))

        assert result.units is None
        assert result.figures == ()


@pytest.mark.covers("ADR-035", "G-30")
class TestWhatAResultHadWithheld:
    """The measurement `Suppression` is built from, and the predicate an adapter gates on.

    Untested until mutation review found it — `domain-measurement-figures.md` S-02 through S-05.
    That mattered because `Withholding.occurred` is live: the Athena adapter reads it to decide
    whether suppression is reported at all, so a wrong answer here is a suppressed result
    presented as complete. ADR-035 made suppression a measured quantity precisely so
    `rows_suppressed=False` is earned from a count rather than assumed, and this is where the
    count becomes that boolean.
    """

    @staticmethod
    def _plan(catalog: Catalog) -> QueryPlan:
        """Build a two-operand plan, since the single-sided amount rule needs a second operand."""
        contracts = [c for _, c in load_contracts(ROOT)]
        return plan(
            catalog,
            PlanRequest(
                metric_name="roas_attributed_14d",
                grains=("campaign",),
                window=WEEK,
                filters={
                    "marketplace_key": "ATVPDKIKX0DER",
                    "channel_key": "DSP",
                    "ad_product_key": "DSP",
                    "rows_suppressed": "false",
                },
            ),
            FactShapes(
                numerator=FactShape.for_fact("fct_attributed_conversion", contracts),
                denominator=FactShape.for_fact("fct_ad_exposure", contracts),
            ),
        )

    @staticmethod
    def _row(
        campaign: str,
        numerator: tuple[int, str | None],
        denominator: tuple[int, str | None],
    ) -> Row:
        """Build a row carrying each operand's withheld row count and withheld amount."""
        return {
            "campaign_key": campaign,
            withheld_rows_alias(0): numerator[0],
            withheld_alias(0): None if numerator[1] is None else Decimal(numerator[1]),
            withheld_rows_alias(1): denominator[0],
            withheld_alias(1): None if denominator[1] is None else Decimal(denominator[1]),
        }

    def test_nothing_withheld_is_earned_from_a_count_rather_than_assumed(
        self, catalog: Catalog
    ) -> None:
        """`occurred is False` is the claim an answer makes when it says nothing was withheld."""
        rows = (
            self._row("C1", (0, None), (0, None)),
            self._row("C2", (0, None), (0, None)),
        )
        withholding = withholding_in(self._plan(catalog), rows)

        assert withholding.occurred is False
        assert withholding.withheld_rows == 0
        assert withholding.amount == Decimal(0)
        assert (withholding.groups, withholding.total_groups) == (0, 2)

    def test_one_withheld_row_makes_it_occur(self, catalog: Catalog) -> None:
        rows = (self._row("C1", (1, "50.0000"), (0, None)),)
        withholding = withholding_in(self._plan(catalog), rows)

        assert withholding.occurred is True
        assert withholding.withheld_rows == 1

    def test_only_the_numerator_amount_is_totalled(self, catalog: Catalog) -> None:
        """Withheld revenue and withheld spend are different units, so their sum has none.

        The row counts do add, because "some rows were withheld" is true whichever operand
        lost them.
        """
        rows = (self._row("C1", (2, "50.0000"), (3, "70.0000")),)
        withholding = withholding_in(self._plan(catalog), rows)

        assert withholding.amount == Decimal("50.0000")
        assert withholding.withheld_rows == 5
        assert (withholding.groups, withholding.total_groups) == (1, 1)

    def test_a_group_only_the_denominator_lost_still_counts_as_withheld(
        self, catalog: Catalog
    ) -> None:
        """This is the drill-across case: spend withheld while revenue came back intact."""
        rows = (self._row("C1", (0, None), (4, "70.0000")),)
        withholding = withholding_in(self._plan(catalog), rows)

        assert withholding.occurred is True
        assert withholding.withheld_rows == 4
        assert withholding.amount == Decimal(0)
        assert withholding.groups == 1

    def test_only_the_groups_that_lost_something_are_counted(self, catalog: Catalog) -> None:
        """`groups` beside `total_groups` is what makes "one campaign of three" sayable."""
        rows = (
            self._row("C1", (2, "50.0000"), (0, None)),
            self._row("C2", (0, None), (0, None)),
            self._row("C3", (0, None), (0, None)),
        )
        withholding = withholding_in(self._plan(catalog), rows)

        assert (withholding.groups, withholding.total_groups) == (1, 3)
        assert withholding.withheld_rows == 2

    def test_an_empty_result_withheld_nothing(self, catalog: Catalog) -> None:
        """Zero rows is an answer, and it is not a suppressed one."""
        withholding = withholding_in(self._plan(catalog), ())

        assert withholding.occurred is False
        assert (withholding.groups, withholding.total_groups) == (0, 0)


@pytest.mark.covers("ADR-035")
class TestAWithheldAmountWithNoCurrency:
    """AMC-V decision 5 (owner S-8): withheld money with no currency is never summed.

    On multi-source withheld rows AMC returned purchase-side sums and currency NULL
    [verified, 2026-10-09]. Such a group has no figure, and its withheld amount has no unit, so
    the answer reads "withheld" for it rather than adding it to the withheld total or refusing
    the whole answer over it.
    """

    @staticmethod
    def _plan(catalog: Catalog) -> QueryPlan:
        contracts = [contract for _, contract in load_contracts(ROOT)]
        return plan(
            catalog,
            PlanRequest(
                metric_name="attributed_revenue_14d",
                grains=("campaign",),
                window=WEEK,
                filters={
                    "marketplace_key": "ATVPDKIKX0DER",
                    "channel_key": "DSP",
                    "ad_product_key": "DSP",
                },
            ),
            FactShape.for_fact("fct_attributed_conversion", contracts),
        )

    @staticmethod
    def _row(campaign: str, currency: str | None, amounts: tuple[str | None, str]) -> Row:
        """One campaign group: its attributable revenue (None when all withheld), then withheld."""
        value, withheld = amounts
        return {
            "campaign_key": campaign,
            CURRENCY_COLUMN: currency,
            measure_alias(0): None if value is None else Decimal(value),
            withheld_alias(0): Decimal(withheld),
            withheld_rows_alias(0): 1,
        }

    def test_a_currency_less_withheld_group_is_typed_withheld_and_left_out(
        self, catalog: Catalog
    ) -> None:
        built = self._plan(catalog)
        assert CURRENCY_COLUMN not in built.partially_written_columns
        rows = (
            self._row("C1", "USD", ("100.0000", "20.0000")),
            self._row("C2", None, (None, "7.0000")),
        )
        result = measure(built, rows)
        assert [figure.value for figure in result.figures] == [Decimal("100.0000")]
        assert result.units is not None
        assert result.units.currency_code == "USD"
        assert result.undefined_rows == 1
        withholding = withholding_in(built, rows)
        assert withholding.amount == Decimal("20.0000")
        assert (withholding.withheld_rows, withholding.groups) == (2, 2)
        absent = withholding.withheld_amount
        assert absent is not None
        assert absent == WithheldAmount(1)
        assert absent.reason == "withheld"
        assert absent.text == ("Amazon withheld this amount, so it isn't added to the total.")
        suppression = _suppression_from(built, rows)  # pyright: ignore[reportArgumentType]
        assert suppression == Suppression(
            rows_suppressed=True,
            reason=(
                "Amazon withheld 20.0000 attributed revenue (14 day, advertised products) "
                "across 1 of 2 campaign_key value(s), because too few shoppers sat behind those "
                "rows. In 1 more campaign_key value(s), Amazon withheld this amount, so it isn't "
                "added to the total."
            ),
        )

    def test_every_withheld_group_without_its_currency_names_its_groups(
        self, catalog: Catalog
    ) -> None:
        """Review A1-2/B1-2: with no stated amount, the absence still names whose amount it is."""
        built = self._plan(catalog)
        rows = (
            self._row("C1", None, (None, "20.0000")),
            self._row("C2", None, (None, "7.0000")),
        )
        withholding = withholding_in(built, rows)
        assert withholding.amount == Decimal(0)
        assert withholding.withheld_amount == WithheldAmount(2)
        suppression = _suppression_from(built, rows)  # pyright: ignore[reportArgumentType]
        assert suppression == Suppression(
            rows_suppressed=True,
            reason=(
                "Amazon withheld 2 row(s) across 2 of 2 campaign_key value(s), because too few "
                "shoppers sat behind them. In 2 of those campaign_key value(s), Amazon withheld "
                "this amount, so it isn't added to the total."
            ),
        )

    def test_a_withheld_group_with_its_currency_is_summed_as_before(self, catalog: Catalog) -> None:
        """Positive control: the same group carrying its currency joins the withheld amount."""
        built = self._plan(catalog)
        rows = (
            self._row("C1", "USD", ("100.0000", "20.0000")),
            self._row("C2", "USD", (None, "7.0000")),
        )
        withholding = withholding_in(built, rows)
        assert withholding.amount == Decimal("27.0000")
        assert withholding.withheld_amount is None
        assert measure(built, rows).undefined_rows == 1
        suppression = _suppression_from(built, rows)  # pyright: ignore[reportArgumentType]
        assert suppression == Suppression(
            rows_suppressed=True,
            reason=(
                "Amazon withheld 27.0000 attributed revenue (14 day, advertised products) "
                "across 2 of 2 campaign_key value(s), because too few shoppers sat behind those "
                "rows."
            ),
        )

    def test_an_attributable_figure_with_no_currency_is_still_refused(
        self, catalog: Catalog
    ) -> None:
        """The skip covers a wholly withheld group only; an attributable amount needs a unit."""
        rows = (self._row("C1", None, ("100.0000", "20.0000")),)
        with pytest.raises(InvariantError) as refused:
            measure(self._plan(catalog), rows)
        assert refused.value.code == MEASURE_ABSENT


class TestTheErrorCodesAreStable:
    """The three codes this module raises, pinned as the identifiers they are.

    Nothing asserted them before, so a renumbering was invisible — and because all three share
    the MEASURE area, one number changing makes two codes identical rather than merely different.
    A code reaches logs and alarms, so a collision silently merges two distinct failures.
    """

    def test_each_code_renders_the_identifier_callers_match_on(self) -> None:
        assert str(MEASURE_ABSENT) == "ADLY-MEASURE-0001"
        assert str(MEASURE_NOT_A_QUANTITY) == "ADLY-MEASURE-0002"
        assert str(MIXED_CURRENCY) == "ADLY-MEASURE-0003"

    def test_no_two_codes_collide(self) -> None:
        codes = (MEASURE_ABSENT, MEASURE_NOT_A_QUANTITY, MIXED_CURRENCY)
        assert len({str(code) for code in codes}) == len(codes)


class TestTheseValuesAreFrozen:
    """A figure is a measurement of something that already happened, so nothing may edit one.

    `frozen=True` was unasserted on all three, which is the same gap
    `domain-strategy-objectives.md` S-04 recorded. `slots=True` is deliberately not asserted: it
    is one of ADR-013's two known equivalent classes, and a test for it would pin an
    implementation detail rather than a guarantee.
    """

    def test_a_figure_cannot_be_rewritten(self, revenue_plan: QueryPlan) -> None:
        figure = measure(revenue_plan, (_row("B0TRAP00001", "100.0000"),)).figures[0]
        with pytest.raises(FrozenInstanceError):
            figure.value = Decimal("1.0000")  # type: ignore[misc]

    def test_a_measurement_cannot_be_rewritten(self, revenue_plan: QueryPlan) -> None:
        result = measure(revenue_plan, (_row("B0TRAP00001", "100.0000"),))
        with pytest.raises(FrozenInstanceError):
            result.figures = ()  # type: ignore[misc]

    def test_a_withholding_cannot_be_rewritten(self) -> None:
        withholding = Withholding(amount=Decimal(0), withheld_rows=0, groups=0, total_groups=0)
        with pytest.raises(FrozenInstanceError):
            withholding.withheld_rows = 99  # type: ignore[misc]
