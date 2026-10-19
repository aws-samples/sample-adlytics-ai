# SPDX-License-Identifier: MIT-0
"""Query planning: which fact, which measure, grouped by what, and what is refused.

The rule under test is the aggregation one, and it is worth stating before the tests read
it: a fact's grain declares what makes one row unique, so aggregating a grain column away is
a claim that summing across it is addition. For `marketplace_key` that is true. For
`distributor_view` it is false — a MANUFACTURING pull and a SOURCING pull describe the same
units under two lenses, so adding them double-counts.

Nothing in a contract distinguishes those two cases, so a plan must account for every grain
column rather than guess. These tests exercise that against the committed contracts and the
committed metric dictionary, not synthetic ones, because the defect being prevented is
specific to how `fct_retail_sales` is actually shaped.
"""

from __future__ import annotations

import pathlib
from dataclasses import replace
from datetime import date, timedelta

import pytest

from adapters.jobs.contract_validation.loader import load_contracts
from adapters.outbound.metric_dictionary import load_catalog
from domain.catalog.capability import (
    GRAIN_NOT_SUPPORTED_CODE,
    Catalog,
    MetricCompatibilityMode,
    MetricKind,
    references_in_definition,
)
from domain.data_contracts.contract import DataContract
from domain.measurement.pack_coverage import (
    CONTRACT_COVERAGE_REFUSED,
    PACK_COVERAGE_UNAVAILABLE,
    SPLIT_WRITER_REFUSED,
    PackDayVersions,
)
from domain.measurement.planning import (
    GRAIN_COLUMNS,
    UNFILTERABLE_COLUMN,
    Combination,
    FactShape,
    FactShapes,
    PlanRequest,
    QueryPlan,
    plan,
)
from domain.narrative.shaped_response import UnitKind
from domain.semantics.compatibility import CompatibilityField
from domain.shared_kernel.errors import DomainError, InvariantError, ValidationError
from domain.shared_kernel.provenance import MeasurementBasis
from domain.shared_kernel.values import DateRange

# ADR-023 is exercised by the grain vocabulary below. ADR-024 is tagged on the class that
# verifies it rather than here, so the matrix does not credit this whole module with it.
pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-023")]

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent

# The window the target question asks about: seven inclusive days.
WEEK = DateRange(date(2026, 8, 10), date(2026, 8, 16))

# What pins every fct_retail_sales grain column the question does not group by. All three
# are required, and marketplace is the one that surprises: a monetary sum across marketplaces
# spans currencies, which `Money` would refuse to add long after the SQL had already summed
# them. The option columns are required because summing across them double-counts.
ONE_POPULATION = {
    "marketplace_key": "ATVPDKIKX0DER",
    "distributor_view": "MANUFACTURING",
    "selling_program": "RETAIL",
}
OPTIONS_ONLY = {"distributor_view": "MANUFACTURING", "selling_program": "RETAIL"}


@pytest.fixture(scope="module")
def catalog() -> Catalog:
    return load_catalog(ROOT)


@pytest.fixture(scope="module")
def conversion() -> FactShape:
    """Read the real shape of `fct_attributed_conversion`, which carries the halo columns."""
    contracts = {c.contract_id: c for _, c in load_contracts(ROOT)}
    return FactShape.of(contracts["amc-attributed-conversion"])


@pytest.fixture(scope="module")
def exposure() -> FactShape:
    """Read `fct_ad_exposure` from every contract writing it, which is five.

    Built with `for_fact` rather than from one contract because no single one declares the whole
    table: clicks come from `amc-dsp-clicks`, impressions and spend from `amc-dsp-impressions`,
    viewability from `amc-dsp-views`, and DSP reach alone from `amc-dsp-reach`. A shape from one
    contract cannot plan `ctr`.
    """
    return FactShape.for_fact("fct_ad_exposure", [c for _, c in load_contracts(ROOT)])


@pytest.fixture(scope="module")
def retail_sales() -> FactShape:
    """Read the real shape of `fct_retail_sales` from the contract that writes it."""
    contracts = {c.contract_id: c for _, c in load_contracts(ROOT)}
    sales = contracts["vendor-sales-by-asin"]
    return FactShape.of(sales)


def _by_asin(**overrides: object) -> PlanRequest:
    fields: dict[str, object] = {
        "metric_name": "retail_revenue_shipped",
        "grains": ("product",),
        "window": WEEK,
        "filters": ONE_POPULATION,
    }
    return PlanRequest(**(fields | overrides))  # type: ignore[arg-type]


class TestTheGrainVocabulary:
    def test_product_resolves_to_the_asin_and_not_a_surrogate(self) -> None:
        """The one place a mechanical `f"{name}_key"` rule would be silently wrong.

        ADR-023 removed the `product_key` surrogate after establishing every fact had been
        mapping an ASIN straight through, so a derived mapping would emit a column no table
        has.
        """
        assert GRAIN_COLUMNS["product"] == "amazon_asin"

    def test_every_mapped_column_exists_on_a_real_fact(self) -> None:
        """Otherwise the map could name a column the generated tables do not have."""
        contracts = [c for _, c in load_contracts(ROOT)]
        real_columns = {column for contract in contracts for column in contract.target_columns}

        for dimension, column in GRAIN_COLUMNS.items():
            assert column in real_columns, f"{dimension} maps to unknown column {column}"

    def test_an_unknown_dimension_is_refused_at_request_construction(self) -> None:
        """The fixed refusal remains actionable without echoing caller-selected text."""
        with pytest.raises(ValidationError, match="requested grains are unsupported") as raised:
            _by_asin(grains=("person@example.com",))
        assert "person@example.com" not in raised.value.description
        assert raised.value.context == {
            "outcome": "unsupported_grain",
            "n": 1,
            "available": sorted(GRAIN_COLUMNS),
        }
        assert "product" in raised.value.description

    def test_the_refusal_distinguishes_dimension_names_from_result_columns(self) -> None:
        with pytest.raises(ValidationError) as raised:
            _by_asin(grains=("amazon_asin",))
        assert "dimension names rather than result column names" in raised.value.description
        assert "amazon_asin" not in raised.value.description

    def test_a_request_with_no_grain_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="At least one grain"):
            _by_asin(grains=())


class TestTheTargetQuestion:
    """Build-order Phase 2's target: shipped revenue by ASIN for last week."""

    def test_it_plans_once_the_option_columns_are_pinned(
        self, catalog: Catalog, retail_sales: FactShape
    ) -> None:
        result = plan(catalog, _by_asin(), retail_sales)

        assert result.fact == "fct_retail_sales"
        assert result.measure_column == "revenue_shipped"
        assert result.group_by == ("amazon_asin",)
        assert result.window == WEEK

    def test_the_plan_carries_publisher_leaf_source_datasets(
        self, catalog: Catalog, retail_sales: FactShape
    ) -> None:
        ordinary = plan(catalog, _by_asin(), retail_sales)
        filtered = plan(
            catalog,
            _by_asin(filters={**ONE_POPULATION, "currency_code": "USD"}),
            retail_sales,
        )

        assert ordinary.source_datasets == ("GET_VENDOR_SALES_REPORT",)
        assert filtered.source_datasets == ordinary.source_datasets

    def test_the_plan_carries_what_the_answer_must_declare(
        self, catalog: Catalog, retail_sales: FactShape
    ) -> None:
        """So a tool never invents a grain, a basis or a unit the query did not produce."""
        result = plan(catalog, _by_asin(), retail_sales)

        assert str(result.grain) == "amazon_asin"
        assert result.units is UnitKind.CURRENCY
        assert result.measurement_basis == (MeasurementBasis.RETAIL_ACTUAL,)
        assert result.facts_read == ("fct_retail_sales",)

    def test_the_measure_is_read_from_the_dictionary_not_named_in_code(
        self, catalog: Catalog, retail_sales: FactShape
    ) -> None:
        """`sum(fct_retail_sales.revenue_shipped)` is the declaration; this resolves it."""
        metric = catalog.metric("retail_revenue_shipped")
        assert metric is not None
        assert metric.definition == "sum(fct_retail_sales.revenue_shipped)"

        result = plan(catalog, _by_asin(), retail_sales)
        assert f"{result.fact}.{result.measure_column}" in metric.definition

    def test_adding_marketplace_as_a_grain_widens_the_group_by(
        self, catalog: Catalog, retail_sales: FactShape
    ) -> None:
        result = plan(
            catalog,
            _by_asin(grains=("product", "marketplace"), filters=OPTIONS_ONLY),
            retail_sales,
        )

        assert result.group_by == ("amazon_asin", "marketplace_key")
        assert str(result.grain) == "amazon_asin x marketplace_key"


@pytest.mark.covers("ADR-024")
class TestEveryGrainColumnMustBeAccountedFor:
    def test_the_naive_question_is_refused(self, catalog: Catalog, retail_sales: FactShape) -> None:
        """Grouping by ASIN alone sums across both option columns and every marketplace.

        This is the defect the rule exists for: every surviving row would be individually
        correct, the total would be roughly double, and nothing about the answer would look
        wrong.
        """
        with pytest.raises(DomainError) as raised:
            plan(catalog, _by_asin(filters={}), retail_sales)

        assert "distributor_view" in raised.value.description
        assert "selling_program" in raised.value.description
        assert "marketplace_key" in raised.value.description

    def test_the_refusal_names_only_the_unaccounted_columns(
        self, catalog: Catalog, retail_sales: FactShape
    ) -> None:
        """A refusal listing columns already pinned would send a caller in circles."""
        with pytest.raises(DomainError) as raised:
            plan(catalog, _by_asin(filters={"distributor_view": "MANUFACTURING"}), retail_sales)

        assert "selling_program" in raised.value.description
        assert "distributor_view" not in raised.value.description

    def test_marketplace_must_be_accounted_for_even_though_summing_it_is_addition(
        self, catalog: Catalog, retail_sales: FactShape
    ) -> None:
        """The currency hazard, closed for free by the same rule.

        Summing revenue across marketplaces is legitimate arithmetic on distinct
        populations — unlike the option columns — so a narrower rule would have allowed it.
        But `dim_marketplace` carries `default_currency_code` and each row's `currency_code`
        travels with its amount, so the sum can span currencies. `Money` refuses to add
        those, which is the right answer arriving too late: the SQL has already summed them
        into one number by then.
        """
        with pytest.raises(DomainError, match="marketplace_key"):
            plan(catalog, _by_asin(filters=OPTIONS_ONLY), retail_sales)

    def test_the_window_accounts_for_the_date_column(
        self, catalog: Catalog, retail_sales: FactShape
    ) -> None:
        """`date_key` is in the grain and bounded by the window, so it needs no group-by.

        Without this the target question could never plan: nobody asks for revenue broken
        down by every individual day in order to get a weekly total.
        """
        result = plan(catalog, _by_asin(), retail_sales)

        assert "date_key" not in result.group_by
        assert "date_key" in result.accounted_columns

    def test_grouping_a_column_accounts_for_it_as_well_as_filtering_does(
        self, catalog: Catalog, retail_sales: FactShape
    ) -> None:
        """Grouping is the honest alternative to pinning: the answer states the breakdown."""
        result = plan(
            catalog,
            _by_asin(grains=("product", "marketplace"), filters=OPTIONS_ONLY),
            retail_sales,
        )

        assert result.accounted_columns >= set(retail_sales.grain_columns)

    def test_an_empty_fact_grain_is_refused_rather_than_passing_every_plan(self) -> None:
        """The check's input cannot be optional, or it silently becomes a no-op."""
        with pytest.raises(ValueError, match="non-empty"):
            FactShape(name="fct_retail_sales", grain_columns=(), columns=("revenue_shipped",))


class TestAFilterKeyMustBeARealColumn:
    """A safety boundary, not a tidiness one.

    A filter key is an identifier. SQL cannot bind an identifier, so whichever adapter renders
    a plan has to interpolate the key into the statement text. Validating it here is what lets
    the adapter interpolate identifiers and bind only values.
    """

    def test_a_key_that_is_not_a_column_is_refused(
        self, catalog: Catalog, retail_sales: FactShape
    ) -> None:
        with pytest.raises(ValidationError) as raised:
            plan(catalog, _by_asin(filters={**ONE_POPULATION, "no_such_column": "x"}), retail_sales)

        assert "no_such_column" in raised.value.description
        assert raised.value.code == UNFILTERABLE_COLUMN

    def test_sql_smuggled_through_a_filter_key_is_refused(
        self, catalog: Catalog, retail_sales: FactShape
    ) -> None:
        """The concrete attack this closes.

        Before the check, `1=1 OR TRUE --` was accepted into the plan and would have reached
        an adapter that had no way to know it was not a column name.
        """
        hostile = {**ONE_POPULATION, "1=1 OR TRUE --": "x"}

        with pytest.raises(ValidationError, match="cannot be filtered"):
            plan(catalog, _by_asin(filters=hostile), retail_sales)

    def test_a_non_grain_column_of_the_fact_may_still_be_filtered(
        self, catalog: Catalog, retail_sales: FactShape
    ) -> None:
        """The rule is "a real column", not "a grain column".

        Filtering `currency_code` is legitimate and it is in no fact's grain.
        """
        result = plan(
            catalog,
            _by_asin(filters={**ONE_POPULATION, "currency_code": "USD"}),
            retail_sales,
        )

        assert result.filters["currency_code"] == "USD"

    def test_the_stored_key_comes_from_the_contract_not_the_request(
        self, catalog: Catalog, retail_sales: FactShape
    ) -> None:
        """Structural, not merely validated.

        The steering rule is "never build a query string from a model-supplied value", and a
        filter key has to be interpolated because SQL cannot bind an identifier. Rebuilding
        the mapping from the fact's own column tuple means the string an adapter puts in the
        statement provably originates in the contract, rather than being a caller's string
        that happened to compare equal to a column name.
        """
        result = plan(catalog, _by_asin(), retail_sales)
        declared = {column: column for column in retail_sales.columns}

        for key in result.filters:
            assert key is declared[key]

    def test_a_projected_provenance_column_may_be_filtered(
        self, catalog: Catalog, retail_sales: FactShape
    ) -> None:
        """`basis_key` and `source_timezone` are real columns that no field mapping declares.

        `FactShape.of` adds them back, because a shape built from `target_columns` alone would
        refuse a legitimate filter on the measurement basis.
        """
        assert "basis_key" in retail_sales.columns

        result = plan(
            catalog,
            _by_asin(filters={**ONE_POPULATION, "basis_key": "retail_actual"}),
            retail_sales,
        )

        assert result.filters["basis_key"] == "retail_actual"

    def test_a_shape_whose_grain_is_not_in_its_columns_is_refused(self) -> None:
        """Two descriptions of one fact that disagree would make the checks unreliable."""
        with pytest.raises(ValueError, match="disagree"):
            FactShape(
                name="fct_retail_sales",
                grain_columns=("amazon_asin",),
                columns=("revenue_shipped",),
            )


class TestRefusalsComeFromTheDictionary:
    def test_an_unsupported_grain_is_refused_with_the_valid_ones(
        self, catalog: Catalog, retail_sales: FactShape
    ) -> None:
        """Retail revenue carries no campaign, so the figure would be plausible and wrong."""
        with pytest.raises(DomainError) as raised:
            plan(catalog, _by_asin(grains=("campaign",)), retail_sales)

        assert "campaign" in raised.value.description
        assert "date, ecosystem, platform, product, marketplace" in raised.value.description

    def test_an_unknown_metric_is_a_validation_error_not_a_domain_error(
        self, catalog: Catalog, retail_sales: FactShape
    ) -> None:
        """A typo is a request outside its contract; an unsupported grain is understood."""
        with pytest.raises(ValidationError):
            plan(catalog, _by_asin(metric_name="retail_revenue_shiped"), retail_sales)

    def test_a_ratio_is_refused_because_it_is_two_aggregations(
        self, catalog: Catalog, retail_sales: FactShape
    ) -> None:
        """Answering it from one fact would silently return one operand.

        `roas_blended` is retail revenue over ad spend, which live in different facts with
        almost no shared dimensions. A ratio whose operands *share* a fact is now planned as one
        query; this one cannot be, and refusing explicitly is what stops a numerator being
        returned where a quotient was asked for.
        """
        with pytest.raises(DomainError, match="drill-across"):
            plan(catalog, _by_asin(metric_name="roas_blended", grains=("date",)), retail_sales)

    def test_a_refusal_is_raised_and_never_an_empty_plan(
        self, catalog: Catalog, retail_sales: FactShape
    ) -> None:
        """A refusal ran no query, so a response would have to invent basis and coverage."""
        with pytest.raises(DomainError):
            plan(catalog, _by_asin(filters={}), retail_sales)

        # And nothing about QueryPlan permits an absent metric or fact.
        assert "metric" in QueryPlan.__dataclass_fields__
        assert "fact" in QueryPlan.__dataclass_fields__


@pytest.mark.covers("ADR-019")
class TestADistinctCountIsNeverAggregated:
    """`reach_users` at a coarser grain than it was extracted at, which must refuse.

    Stricter than the ADR-024 rule the class above covers, and the difference is the date.
    That check treats `date_key` as accounted for by the window, which is right for a sum —
    a seven-day total is the seven daily totals added up. For a distinct count it is wrong:
    the same shopper appears in several days, so adding the days overstates reach by however
    much they overlapped, and AMC publishes no sketch that could be merged to find out.

    The failure this prevents looks entirely normal. A reach figure is a plausible number
    whatever grain produced it, and nothing downstream can tell it double-counted anybody.
    """

    def _reach(self, grains: tuple[str, ...], window: DateRange) -> PlanRequest:
        return PlanRequest(
            metric_name="reach_users",
            grains=grains,
            window=window,
            filters={
                "marketplace_key": "ATVPDKIKX0DER",
                "channel_key": "DSP",
                "ad_product_key": "DSP",
                "rows_suppressed": "false",
            },
        )

    def test_a_multi_day_window_without_a_date_grain_is_refused(
        self, catalog: Catalog, exposure: FactShape
    ) -> None:
        with pytest.raises(DomainError, match="counts distinct users"):
            plan(catalog, self._reach(("campaign",), WEEK), exposure)

    def test_the_refusal_says_how_many_days_it_would_have_collapsed(
        self, catalog: Catalog, exposure: FactShape
    ) -> None:
        """Naming the number is what makes the refusal actionable rather than a rule citation."""
        with pytest.raises(DomainError, match="7 days"):
            plan(catalog, self._reach(("campaign",), WEEK), exposure)

    def test_grouping_by_date_makes_the_same_window_answerable(
        self, catalog: Catalog, exposure: FactShape
    ) -> None:
        built = plan(catalog, self._reach(("date", "campaign"), WEEK), exposure)
        assert built.group_by == ("date_key", "campaign_key")

    def test_a_single_day_window_needs_no_date_grain(
        self, catalog: Catalog, exposure: FactShape
    ) -> None:
        """Nothing to aggregate across, so the stricter rule has nothing to refuse."""
        one_day = DateRange(date(2026, 8, 10), date(2026, 8, 10))
        built = plan(catalog, self._reach(("campaign",), one_day), exposure)
        assert built.group_by == ("campaign_key",)

    def test_an_additive_count_over_the_same_window_is_planned(
        self, catalog: Catalog, exposure: FactShape
    ) -> None:
        """Both are counts and only one is summable.

        Which is why non-additivity is declared rather than inferred from the unit kind.
        """
        request = PlanRequest(
            metric_name="impressions",
            grains=("campaign",),
            window=WEEK,
            filters={
                "marketplace_key": "ATVPDKIKX0DER",
                "channel_key": "DSP",
                "ad_product_key": "DSP",
                "rows_suppressed": "false",
            },
        )
        assert plan(catalog, request, exposure).measure_column == "impressions"

    def _unpinned(self, metric_name: str) -> PlanRequest:
        """Ask a reach metric with the discriminator left to the fold, as a caller now does."""
        return PlanRequest(
            metric_name=metric_name,
            grains=("campaign",),
            window=DateRange(date(2026, 8, 10), date(2026, 8, 10)),
            filters={
                "marketplace_key": "ATVPDKIKX0DER",
                "channel_key": "DSP",
                "ad_product_key": "DSP",
            },
        )

    @pytest.mark.covers("ADR-035")
    @pytest.mark.parametrize("metric_name", ["reach_users", "cost_per_reach", "avg_frequency"])
    def test_the_folded_discriminator_accounts_for_itself(
        self, catalog: Catalog, exposure: FactShape, metric_name: str
    ) -> None:
        """A distinct count need not pin `rows_suppressed` to be extracted at its own grain.

        The three non-additive metrics were the last that still required it, and requiring it
        selected a different population than the one asked about — ADR-035. The fold is sound
        here for the same reason filtering always was: a grain holds at most one unsuppressed
        row, so summing the attributable side sums exactly one distinct count.

        `cost_per_reach` and `avg_frequency` are included because they inherit non-additivity
        through their denominator without declaring it, so a fix that reached only the metric
        naming it would leave two of the three refusing.
        """
        assert plan(catalog, self._unpinned(metric_name), exposure).group_by == ("campaign_key",)

    @pytest.mark.covers("ADR-035")
    def test_the_fold_does_not_also_excuse_the_date(
        self, catalog: Catalog, exposure: FactShape
    ) -> None:
        """Accounting for the discriminator says nothing about summing days.

        The narrow risk in accepting the fold is that it is read as accounting for the whole
        grain. It accounts for one column. A multi-day window must still group by date.
        """
        spanning = replace(self._unpinned("reach_users"), window=WEEK)
        with pytest.raises(DomainError, match="7 days"):
            plan(catalog, spanning, exposure)


def _distinct_count_refusal(problems: str) -> str:
    """Build the planner's exact distinct-count refusal for `reach_users` across `problems`."""
    return (
        f"reach_users counts distinct users, so it cannot be aggregated across {problems}. Two "
        "periods' distinct shoppers are not the sum of each period's, because the same shopper "
        "may appear in both, and the source publishes no sketch that could be merged to find "
        "out. A coarser figure needs a new extraction at that grain rather than an aggregation "
        "of this one. Available instead: date, ecosystem, platform, channel_group, channel, "
        "campaign, ad_product, marketplace."
    )


_DSP_ROWS = {
    "marketplace_key": "amazon_us",
    "channel_key": "amazon_dsp",
    "ad_product_key": "amazon_dsp",
}


@pytest.mark.covers("ADR-109")
class TestDspReachIsServedOnlyOnCoveredDays:
    """Pack 4 (`amc-dsp-reach`) is the fifth writer of `fct_ad_exposure` (plan D6).

    DSP rows landed before it carry no reach, so a request pinning DSP rows plans only over days
    with a CURRENT `amc-dsp-reach` publication. A request pinning sponsored rows, or none, reads
    what it read before.
    """

    DAY = date(2026, 9, 14)

    def _reach(  # noqa: PLR0913, PLR0917 — the window, its coverage, and the question's scope
        self,
        window: DateRange,
        coverage: tuple[PackDayVersions, ...] | None,
        filters: dict[str, str] | None = None,
        grains: tuple[str, ...] = ("campaign",),
    ) -> PlanRequest:
        return PlanRequest(
            metric_name="reach_users",
            grains=grains,
            window=window,
            filters=_DSP_ROWS if filters is None else filters,
            pack_coverage=coverage,
        )

    @staticmethod
    def _covered(*days: date) -> tuple[PackDayVersions, ...]:
        return (PackDayVersions("amc-dsp-reach", dict.fromkeys(days, 1)),)

    def test_a_covered_dsp_campaign_day_plans_at_campaign_by_day(
        self, catalog: Catalog, exposure: FactShape
    ) -> None:
        one_day = DateRange(self.DAY, self.DAY)
        built = plan(
            catalog,
            self._reach(one_day, self._covered(self.DAY), grains=("date", "campaign")),
            exposure,
        )
        assert (built.fact, built.measures) == ("fct_ad_exposure", ("reach_users",))
        assert built.group_by == ("date_key", "campaign_key")

    def test_an_uncovered_day_is_refused_naming_the_first_covered_one(
        self, catalog: Catalog, exposure: FactShape
    ) -> None:
        one_day = DateRange(self.DAY, self.DAY)
        with pytest.raises(ValidationError) as refused:
            plan(catalog, self._reach(one_day, self._covered()), exposure)
        assert refused.value.code == CONTRACT_COVERAGE_REFUSED
        # Only a writer with no publication at all reads "yet".
        assert refused.value.description == (
            "Reach (distinct users) for this selection isn't measured yet, so it isn't answered."
        )
        two_days = DateRange(self.DAY - timedelta(days=1), self.DAY)
        with pytest.raises(ValidationError) as earlier:
            plan(
                catalog,
                self._reach(two_days, self._covered(self.DAY), grains=("date", "campaign")),
                exposure,
            )
        assert earlier.value.code == CONTRACT_COVERAGE_REFUSED
        assert earlier.value.description == (
            "Reach (distinct users) for this selection is measured from 14 Sep 2026. This window "
            "starts earlier, so it isn't answered. Ask for days from 14 Sep 2026 on."
        )
        assert earlier.value.context["contract"] == "amc-dsp-reach"
        assert earlier.value.context["first_covered_date"] == "2026-09-14"
        # Positive control: the same two days, both covered, plan.
        both = self._covered(self.DAY - timedelta(days=1), self.DAY)
        assert plan(
            catalog, self._reach(two_days, both, grains=("date", "campaign")), exposure
        ).group_by == ("date_key", "campaign_key")

    def test_a_day_before_pack_4_is_refused_naming_the_writer_s_first_day(
        self, catalog: Catalog, exposure: FactShape
    ) -> None:
        """The FEAT's own example: 14 Sep 2026, with DSP reach published from 4 Oct 2026."""
        first = date(2026, 10, 4)
        one_day = DateRange(self.DAY, self.DAY)
        before = (PackDayVersions("amc-dsp-reach", {self.DAY: None}, first_published=first),)
        with pytest.raises(ValidationError) as refused:
            plan(catalog, self._reach(one_day, before), exposure)
        assert refused.value.code == CONTRACT_COVERAGE_REFUSED
        assert refused.value.description == (
            "Reach (distinct users) for this selection is measured from 4 Oct 2026. This window "
            "is earlier, so it isn't answered. Ask for days from 4 Oct 2026 on."
        )
        assert refused.value.context["first_covered_date"] == "2026-10-04"
        # A day after the first publication that has none (a refused delivery) names no day:
        # earlier days were measured, so "from 4 Oct" would be wrong.
        gap = date(2026, 10, 6)
        missing = (PackDayVersions("amc-dsp-reach", {gap: None}, first_published=first),)
        with pytest.raises(ValidationError) as unmeasured:
            plan(catalog, self._reach(DateRange(gap, gap), missing), exposure)
        assert unmeasured.value.description == (
            "Reach (distinct users) for this selection isn't measured on any day of this window, "
            "so it isn't answered."
        )
        assert unmeasured.value.context["first_covered_date"] is None
        # Positive control: the first published day itself plans.
        covered = (PackDayVersions("amc-dsp-reach", {first: 1}),)
        assert plan(catalog, self._reach(DateRange(first, first), covered), exposure).measures == (
            "reach_users",
        )

    def test_dsp_cost_per_reach_is_refused_because_no_row_holds_spend_and_reach(
        self, catalog: Catalog, exposure: FactShape
    ) -> None:
        """`amc-dsp-reach` writes no currency, so its rows group apart from DSP spend (plan D7).

        Refused before coverage is read: no covered day makes it answerable.
        """
        one_day = DateRange(self.DAY, self.DAY)
        for coverage in (self._covered(self.DAY), None):
            with pytest.raises(ValidationError) as refused:
                plan(
                    catalog,
                    replace(self._reach(one_day, coverage), metric_name="cost_per_reach"),
                    exposure,
                )
            assert refused.value.code == SPLIT_WRITER_REFUSED
            assert refused.value.description == (
                "Cost per reached shopper isn't available for this selection. Its parts are "
                "measured by separate deliveries and only one of them carries the currency, so "
                "they can't be combined into one figure. Ask for each part on its own."
            )
            assert refused.value.context == {
                "metric": "cost_per_reach",
                "contract": "amc-dsp-reach",
                "column": "reach_users",
                "carried_not_written": ["currency_code"],
            }
        # Positive control: sponsored cost per reach, whose writer carries spend, reach and
        # currency on one row, still plans with its currency.
        sponsored = {
            "marketplace_key": "amazon_us",
            "channel_key": "amazon_sponsored_products",
            "ad_product_key": "sponsored_products",
        }
        sponsored_plan = plan(
            catalog,
            replace(self._reach(one_day, None, sponsored), metric_name="cost_per_reach"),
            exposure,
        )
        assert sponsored_plan.measures == ("spend", "reach_users")
        assert sponsored_plan.carried_columns == ("currency_code",)

    def test_dsp_average_frequency_plans_on_covered_days_only(
        self, catalog: Catalog, exposure: FactShape
    ) -> None:
        """A ratio carrying no currency is answerable over DSP rows: it groups by grain alone."""
        one_day = DateRange(self.DAY, self.DAY)
        frequency = plan(
            catalog,
            replace(self._reach(one_day, self._covered(self.DAY)), metric_name="avg_frequency"),
            exposure,
        )
        assert (frequency.measures, frequency.carried_columns) == (
            ("impressions", "reach_users"),
            (),
        )
        with pytest.raises(ValidationError) as refused:
            plan(
                catalog,
                replace(self._reach(one_day, self._covered()), metric_name="avg_frequency"),
                exposure,
            )
        assert refused.value.code == CONTRACT_COVERAGE_REFUSED

    def test_a_week_of_dsp_reach_is_refused_by_the_per_day_rule(
        self, catalog: Catalog, exposure: FactShape
    ) -> None:
        """Covered or not, a sum across days or campaigns is not reach (D-2).

        Both are grain refusals (`ADLY-CATALOG-0003`, grain not supported) raised on fully
        covered days, so no coverage refusal can stand in for them: days by the distinct-count
        rule, campaigns by the fact-grain rule (campaign makes an exposure row distinct).
        """
        week = DateRange(self.DAY, self.DAY + timedelta(days=6))
        covered = self._covered(*(self.DAY + timedelta(days=n) for n in range(7)))
        with pytest.raises(DomainError) as across_days:
            plan(catalog, self._reach(week, covered), exposure)
        assert type(across_days.value) is DomainError
        assert across_days.value.code == GRAIN_NOT_SUPPORTED_CODE
        assert across_days.value.context == {
            "requested": "reach_users",
            "reason": "grain_not_supported",
        }
        assert across_days.value.description == _distinct_count_refusal("7 days of date_key")
        # A sum across campaigns is refused the same way: campaign isn't grouped or pinned.
        one_day = DateRange(self.DAY, self.DAY)
        with pytest.raises(DomainError) as across_campaigns:
            plan(
                catalog,
                self._reach(one_day, self._covered(self.DAY), grains=("channel",)),
                exposure,
            )
        assert type(across_campaigns.value) is DomainError
        assert across_campaigns.value.code == GRAIN_NOT_SUPPORTED_CODE
        assert across_campaigns.value.context == {
            "requested": "reach_users",
            "reason": "grain_not_supported",
        }
        assert across_campaigns.value.description == (
            "reach_users over fct_ad_exposure would sum across campaign_key, which the fact's "
            "grain says makes a row distinct. Summing across a grain column is a claim that it "
            "is addition, and for an extraction-option column it is not — a MANUFACTURING pull "
            "and a SOURCING pull describe the same units under two lenses. Group by each column "
            "or filter it."
        )
        # Positive controls: the same covered day at campaign grain, and the same covered week
        # grouped by day, both plan.
        single = plan(catalog, self._reach(one_day, self._covered(self.DAY)), exposure)
        assert (single.fact, single.measures, single.group_by) == (
            "fct_ad_exposure",
            ("reach_users",),
            ("campaign_key",),
        )
        by_day = plan(catalog, self._reach(week, covered, grains=("date", "campaign")), exposure)
        assert by_day.group_by == ("date_key", "campaign_key")

    def test_a_runtime_without_coverage_refuses_dsp_reach_only(
        self, catalog: Catalog, exposure: FactShape
    ) -> None:
        one_day = DateRange(self.DAY, self.DAY)
        with pytest.raises(InvariantError) as unavailable:
            plan(catalog, self._reach(one_day, None), exposure)
        assert unavailable.value.code == PACK_COVERAGE_UNAVAILABLE
        assert unavailable.value.description == (
            "reach_users reads a column a later query pack or a later writer added, and this "
            "runtime supplied no pack coverage to check the window against."
        )
        assert unavailable.value.context == {"metric": "reach_users"}
        # Controls: sponsored reach and unpinned reach read no gated rows, so they plan unchanged.
        sponsored = {
            "marketplace_key": "amazon_us",
            "channel_key": "amazon_sponsored_products",
            "ad_product_key": "sponsored_products",
        }
        assert plan(catalog, self._reach(one_day, None, sponsored), exposure).group_by == (
            "campaign_key",
        )
        unpinned = {"marketplace_key": "amazon_us"}
        assert plan(
            catalog,
            self._reach(one_day, None, unpinned, ("channel", "campaign", "ad_product")),
            exposure,
        ).group_by == ("channel_key", "campaign_key", "ad_product_key")

    def test_the_ad_product_pin_is_gated_like_the_channel_pin(
        self, catalog: Catalog, exposure: FactShape
    ) -> None:
        one_day = DateRange(self.DAY, self.DAY)
        by_product = {"marketplace_key": "amazon_us", "ad_product_key": "amazon_dsp"}
        grains = ("channel", "campaign")
        with pytest.raises(ValidationError) as refused:
            plan(catalog, self._reach(one_day, self._covered(), by_product, grains), exposure)
        assert refused.value.code == CONTRACT_COVERAGE_REFUSED
        assert plan(
            catalog, self._reach(one_day, self._covered(self.DAY), by_product, grains), exposure
        ).measures == ("reach_users",)


@pytest.mark.covers("ADR-028")
class TestTheAggregatesAPlanCarries:
    """Three plan kinds over one fact, and the fourth refused.

    A plan used to carry one measure column, which made a ratio and a difference of sums both
    unanswerable — one was refused as "two aggregations" and the other as an ambiguous
    definition. Both are a single query over a single fact; only the arithmetic afterwards
    differs. So the plan widened rather than a second plan type appearing, which is what
    ADR-028's review trigger anticipated.
    """

    @staticmethod
    def _conversion_filters() -> dict[str, str]:
        """Pin every `fct_attributed_conversion` grain column a product breakdown leaves open."""
        return {
            "marketplace_key": "ATVPDKIKX0DER",
            "channel_key": "DSP",
            "campaign_key": "C1",
            "rows_suppressed": "false",
        }

    def _conversion_plan(
        self, catalog: Catalog, conversion: FactShape, metric_name: str
    ) -> QueryPlan:
        return plan(
            catalog,
            PlanRequest(
                metric_name=metric_name,
                grains=("product",),
                window=WEEK,
                filters=self._conversion_filters(),
            ),
            conversion,
        )

    def test_a_plain_measure_carries_one_aggregate(
        self, catalog: Catalog, retail_sales: FactShape
    ) -> None:
        built = plan(catalog, _by_asin(), retail_sales)
        assert built.combination is Combination.SINGLE
        assert built.measures == ("revenue_shipped",)

    def test_a_same_fact_ratio_carries_both_operands_in_order(
        self, catalog: Catalog, conversion: FactShape
    ) -> None:
        """Numerator first, because `_combine` reads them positionally."""
        built = self._conversion_plan(catalog, conversion, "ntb_rate_12mo")
        assert built.combination is Combination.QUOTIENT
        assert built.measures == ("purchases_new_to_brand_12mo", "purchases")

    def test_a_difference_of_sums_carries_both_terms(
        self, catalog: Catalog, conversion: FactShape
    ) -> None:
        """Halo revenue is the total minus the advertised-only portion.

        Amazon publishes that as an identity rather than as a column of its own.
        """
        built = self._conversion_plan(catalog, conversion, "attributed_revenue_halo_14d")
        assert built.combination is Combination.DIFFERENCE
        assert built.measures == ("revenue_attributed_incl_halo", "revenue_attributed")

    def test_the_multiplier_reaches_the_plan(self, catalog: Catalog, exposure: FactShape) -> None:
        """`cpm` is cost per *thousand*, and the thousand comes from the declaration."""
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
        assert built.multiplier == 1000
        assert built.combination is Combination.QUOTIENT

    def test_an_ordinary_metric_carries_no_multiplier(
        self, catalog: Catalog, retail_sales: FactShape
    ) -> None:
        assert plan(catalog, _by_asin(), retail_sales).multiplier == 1

    def test_a_plan_whose_aggregate_count_contradicts_its_combination_is_refused(
        self, catalog: Catalog, retail_sales: FactShape
    ) -> None:
        """Unreachable through `plan()`, and silent if it happened.

        A quotient built from one aggregate has nothing to divide by.
        """
        built = plan(catalog, _by_asin(), retail_sales)
        with pytest.raises(ValueError, match="needs exactly 2 measure"):
            replace(built, combination=Combination.QUOTIENT)


@pytest.mark.covers("ADR-032")
class TestTheMetricTaxonomyMatchesWhatIsDocumented:
    """The four combination classes partition the dictionary, and the counts are published.

    [ADR-032](../../docs/decisions/adr-032-tools-are-shaped-by-operation-not-by-metric.md)
    states how many metrics each class holds and that 43 of 53 are plannable. Those numbers were
    wrong when first written — several ratios that read as single-source turn out to span two
    facts, because Vendor Central splits sales and traffic into two reports. Asserting them here
    makes the published figures a gate rather than a claim someone re-derives by hand.
    """

    @staticmethod
    def _classify(catalog: Catalog) -> dict[str, list[str]]:
        """Group every declared metric by the plan kind it needs."""
        found: dict[str, list[str]] = {
            "simple": [],
            "difference": [],
            "same_fact": [],
            "cross": [],
            "product": [],
            "within_delivery": [],
            "baked_sum": [],
            "cross_delivery": [],
            "delivery_ratio": [],
        }
        # A sum of two spends in their own currencies (packs 6-7's window spend): the planner
        # parses only a single aggregate or a difference, so it and a ratio over it are refused
        # and the Reach and funnel bake adds them where the currencies agree.
        baked = {
            metric.name
            for metric in catalog.metrics
            if metric.kind is MetricKind.SIMPLE
            and len(references_in_definition(metric.definition)) > 1
            and "+" in metric.definition
        }
        for metric in catalog.metrics:
            if metric.delivery_ratio is not None:
                # A C2 share or index (packs 10-12): a segment row over its delivery's total row;
                # the measurement tool pairs them per segment, the planner refuses one query.
                found["delivery_ratio"].append(metric.name)
                continue
            if metric.cross_delivery is not None:
                # Pack 15 over pack 14 (D-8 (b)): the Retention bake pairs the two deliveries
                # under the cohort-size check; the planner refuses it, offering its operands.
                found["cross_delivery"].append(metric.name)
                continue
            if metric.within_delivery is not None:
                # Computed across one delivery's cells by the page bake; the planner refuses it.
                found["within_delivery"].append(metric.name)
                continue
            if metric.name in baked or {metric.numerator, metric.denominator} & baked:
                found["baked_sum"].append(metric.name)
                continue
            if metric.kind is MetricKind.SIMPLE:
                references = references_in_definition(metric.definition)
                found["difference" if len(references) > 1 else "simple"].append(metric.name)
                continue
            if metric.kind is MetricKind.PRODUCT:
                found["product"].append(metric.name)
                continue
            facts: set[str] = set()
            for operand in (metric.numerator, metric.denominator):
                referenced = catalog.metric(operand)
                assert referenced is not None, f"{metric.name} names undeclared {operand}"
                facts |= {table for table, _ in references_in_definition(referenced.definition)}
            found["same_fact" if len(facts) == 1 else "cross"].append(metric.name)
        return found

    def test_the_classes_partition_every_declared_metric(self, catalog: Catalog) -> None:
        classified = self._classify(catalog)
        total = sum(len(names) for names in classified.values())
        assert total == len(catalog.metrics)

    def test_the_published_counts_are_accurate(self, catalog: Catalog) -> None:
        classified = self._classify(catalog)
        assert {kind: len(names) for kind, names in classified.items()} == {
            "simple": 68,
            "difference": 2,
            "same_fact": 26,
            "cross": 12,
            "product": 1,
            "within_delivery": 4,
            "baked_sum": 2,
            "cross_delivery": 2,
            "delivery_ratio": 3,
        }

    def test_ninety_one_of_one_hundred_twelve_are_plannable(self, catalog: Catalog) -> None:
        classified = self._classify(catalog)
        plannable = len(classified["simple"] + classified["difference"] + classified["same_fact"])
        refused = (
            len(classified["cross"])
            + len(classified["product"])
            + len(classified["within_delivery"])
            + len(classified["baked_sum"])
            + len(classified["cross_delivery"])
            + len(classified["delivery_ratio"])
        )
        assert (plannable, refused) == (96, 24)

    def test_a_within_delivery_ratio_is_refused_offering_its_per_cell_operand(
        self, catalog: Catalog, retail_sales: FactShape
    ) -> None:
        """Pack 5's shares divide one delivery's cells, which plan() can't: typed refusal.

        Positive control: the per-cell operand it offers is a declared, plannable simple metric.
        """
        for name in self._classify(catalog)["within_delivery"]:
            metric = catalog.metric(name)
            assert metric is not None
            with pytest.raises(DomainError, match="computed across every exposure group"):
                plan(
                    catalog,
                    _by_asin(metric_name=name, grains=("marketplace", "exposure_group")),
                    retail_sales,
                )
            operand = catalog.metric(metric.numerator)
            assert operand is not None
            assert operand.name in {"shoppers_reached", "purchaser_rate"}

    def test_a_product_of_operands_is_refused_with_its_operands_offered(
        self, catalog: Catalog, retail_sales: FactShape
    ) -> None:
        """A product's honest figure is publish-time derived, so a live plan must refuse.

        The refusal names the operands, so a caller can measure those instead of
        reaching a dead end.
        """
        with pytest.raises(DomainError, match="customer_returns, asp_shipped"):
            plan(
                catalog,
                _by_asin(metric_name="returns_value_estimate_retail", grains=("date",)),
                retail_sales,
            )

    def test_the_refused_eleven_are_named(self, catalog: Catalog) -> None:
        """Listed rather than counted, so the gap is a list a reader can act on.

        Two of them surprise for the same reason twice: `conversion_rate_retail` and
        `revenue_per_glance_view` read as retail metrics over one source, but the vendor sales
        report and the vendor traffic report are two facts.
        """
        assert sorted(self._classify(catalog)["cross"]) == [
            "click_to_purchase_rate",
            "conversion_rate_retail",
            "cost_per_new_to_brand_buyer_12mo",
            "cost_per_ntb",
            "cost_per_purchase",
            "portfolio_roas_attributed_14d",
            "revenue_per_glance_view",
            "roas_attributed_14d",
            "roas_attributed_incl_halo_14d",
            "roas_blended",
            "roas_deduplicated_incl_halo_14d",
            "tacos",
        ]

    def test_cross_fact_ratios_without_required_selectors_refuse_as_a_drill_across(
        self, catalog: Catalog, retail_sales: FactShape
    ) -> None:
        """Refused for the drill-across reason specifically, not as some other failure.

        The shape matters: the refusal names both operands, so a caller who wanted ROAS can ask
        for revenue and spend separately instead of reaching a dead end.
        """
        for name in self._classify(catalog)["cross"]:
            metric = catalog.metric(name)
            assert metric is not None
            if metric.required_selectors:
                continue
            with pytest.raises(DomainError, match="drill-across"):
                plan(catalog, _by_asin(metric_name=name, grains=("date",)), retail_sales)


class TestAFactWrittenByManyContracts:
    """`fct_ad_exposure` is written by four contracts, and no one of them describes the table.

    Each supplies different measures, so a shape built from a single contract is a partial view.
    That was not merely incomplete — it was unsound: `plan()` validated filter keys against the
    fact's columns and never the *measure* columns, so a plan naming a column from a sibling
    contract was accepted, and the SQL then worked anyway because the union of the four is what
    defines the Iceberg table. Right by luck, and unverifiable.
    """

    @staticmethod
    def _contracts() -> list[DataContract]:
        return [c for _, c in load_contracts(ROOT)]

    def test_the_merged_shape_carries_every_contributed_measure(self) -> None:
        shape = FactShape.for_fact("fct_ad_exposure", self._contracts())
        assert {"clicks", "impressions", "spend", "viewable_impressions", "reach_users"} <= set(
            shape.columns
        )

    def test_no_single_contract_declares_both_ctr_operands(self) -> None:
        """The specific gap, pinned so the merge cannot be argued away as redundant."""
        contracts = {c.contract_id: c for _, c in load_contracts(ROOT)}
        impressions = FactShape.of(contracts["amc-dsp-impressions"])
        clicks = FactShape.of(contracts["amc-dsp-clicks"])
        assert "clicks" not in impressions.columns
        assert "impressions" not in clicks.columns

    def test_the_merged_grain_is_the_shared_one(self) -> None:
        """Required identical rather than merged.

        Four contracts writing one table already agree on it, so a disagreement means a contract
        was edited into conflict with its siblings.
        """
        shape = FactShape.for_fact("fct_ad_exposure", self._contracts())
        contracts = {c.contract_id: c for _, c in load_contracts(ROOT)}
        assert shape.grain_columns == FactShape.of(contracts["amc-dsp-clicks"]).grain_columns

    def test_a_fact_nothing_writes_cannot_be_described(self) -> None:
        with pytest.raises(ValueError, match="no contract writes"):
            FactShape.for_fact("fct_invented", self._contracts())

    def test_a_measure_the_shape_does_not_declare_is_refused(
        self, catalog: Catalog, retail_sales: FactShape
    ) -> None:
        """An adapter interpolates a measure column, so the plan is what proves its origin.

        `ctr` over the retail shape names two columns that fact has never heard of.
        """
        request = PlanRequest(
            metric_name="ctr", grains=("date",), window=WEEK, filters=ONE_POPULATION
        )
        with pytest.raises(ValidationError, match="does not declare"):
            plan(catalog, request, retail_sales)

    def test_the_merged_shape_plans_a_ratio_no_single_contract_could(
        self, catalog: Catalog, exposure: FactShape
    ) -> None:
        request = PlanRequest(
            metric_name="ctr",
            grains=("campaign",),
            window=WEEK,
            filters={
                "marketplace_key": "ATVPDKIKX0DER",
                "channel_key": "DSP",
                "ad_product_key": "DSP",
                "rows_suppressed": "false",
            },
        )
        assert plan(catalog, request, exposure).measures == ("clicks", "impressions")


@pytest.mark.covers("ADR-032")
class TestDrillAcross:
    """A ratio whose operands live in two facts, aggregated per side and joined.

    The nine cross-fact metrics include every attributed and blended ROAS, so this is the plan
    kind the product's headline question depends on. Each side accounts for **its own** grain,
    because the two facts do not share one — `fct_ad_exposure` needs a channel and an ad product
    pinned and has no ASIN at all, while `fct_attributed_conversion` has an ASIN that must be
    accounted for.
    """

    @staticmethod
    def _shapes(numerator: str, denominator: str) -> FactShapes:
        contracts = [c for _, c in load_contracts(ROOT)]
        return FactShapes(
            numerator=FactShape.for_fact(numerator, contracts),
            denominator=FactShape.for_fact(denominator, contracts),
        )

    @staticmethod
    def _amc_filters() -> dict[str, str]:
        return {
            "marketplace_key": "ATVPDKIKX0DER",
            "channel_key": "DSP",
            "ad_product_key": "DSP",
            "rows_suppressed": "false",
        }

    def _roas(self, catalog: Catalog, grains: tuple[str, ...]) -> QueryPlan:
        return plan(
            catalog,
            PlanRequest(
                metric_name="roas_attributed_14d",
                grains=grains,
                window=WEEK,
                filters=self._amc_filters(),
            ),
            self._shapes("fct_attributed_conversion", "fct_ad_exposure"),
        )

    def test_each_measure_sits_with_the_fact_that_declares_it(self, catalog: Catalog) -> None:
        """The reason the denominator is on `JoinSide` rather than in `measures`."""
        built = self._roas(catalog, ("campaign",))
        assert (built.fact, built.measures) == (
            "fct_attributed_conversion",
            ("revenue_attributed",),
        )
        assert built.join is not None
        assert (built.join.fact, built.join.measure) == ("fct_ad_exposure", "spend")

    def test_a_filter_on_only_one_operand_is_refused(self, catalog: Catalog) -> None:
        """Product-filtered revenue divided by all-product spend is a different population."""
        filters = {**self._amc_filters(), "amazon_asin": "B0TRAP00001"}
        with pytest.raises(DomainError, match="only one operand"):
            plan(
                catalog,
                PlanRequest("roas_attributed_14d", ("campaign",), WEEK, filters),
                self._shapes("fct_attributed_conversion", "fct_ad_exposure"),
            )

    def test_the_denominator_carries_its_own_suppression_columns(self, catalog: Catalog) -> None:
        """Load-bearing for the join: it is what tells a withheld side from an empty one."""
        built = self._roas(catalog, ("campaign",))
        assert built.join is not None
        assert built.join.suppression_columns[0] == "rows_suppressed"

    def test_both_facts_reach_the_answer(self, catalog: Catalog) -> None:
        built = self._roas(catalog, ("campaign",))
        assert built.facts_read == ("fct_attributed_conversion", "fct_ad_exposure")

    def test_a_grain_only_one_fact_carries_is_refused(self, catalog: Catalog) -> None:
        """D-8. Product-grain ROAS divides product revenue by spend broadcast across products."""
        with pytest.raises(DomainError, match="product breakdown is unsupported"):
            self._roas(catalog, ("product",))

    def test_one_shape_for_a_two_fact_ratio_is_refused(self, catalog: Catalog) -> None:
        """The opt-in. Without the second fact's grain there is nothing to account for it."""
        contracts = [c for _, c in load_contracts(ROOT)]
        with pytest.raises(DomainError, match="drill-across"):
            plan(
                catalog,
                PlanRequest(
                    metric_name="roas_attributed_14d",
                    grains=("campaign",),
                    window=WEEK,
                    filters=self._amc_filters(),
                ),
                FactShape.for_fact("fct_attributed_conversion", contracts),
            )

    def test_an_additive_product_axis_may_roll_up_on_one_side(self, catalog: Catalog) -> None:
        """Revenue sums across products before campaign-level ROAS is divided."""
        built = self._roas(catalog, ("campaign",))
        assert "amazon_asin" not in built.filters

    def test_a_vendor_side_declares_no_suppression(self, catalog: Catalog) -> None:
        """Vendor Central has no threshold mechanism, so nothing could have been withheld."""
        built = plan(
            catalog,
            PlanRequest(
                metric_name="conversion_rate_retail",
                grains=("product",),
                window=WEEK,
                filters=ONE_POPULATION,
            ),
            self._shapes("fct_retail_sales", "fct_product_page_views"),
        )
        assert built.join is not None
        assert built.join.suppression_columns == ()

    def test_two_shapes_describing_one_fact_are_refused(self) -> None:
        contracts = [c for _, c in load_contracts(ROOT)]
        shape = FactShape.for_fact("fct_retail_sales", contracts)
        with pytest.raises(ValueError, match="one grouped query rather than a join"):
            FactShapes(numerator=shape, denominator=shape)

    def test_declared_one_sided_selector_preserves_operand_orientation(
        self, catalog: Catalog
    ) -> None:
        filters = {
            "distributor_view": "MANUFACTURING",
            "selling_program": "RETAIL",
        }
        shapes = self._shapes("fct_retail_sales", "fct_ad_exposure")
        roas = plan(
            catalog,
            PlanRequest("roas_blended", ("marketplace",), WEEK, filters),
            shapes,
        )
        tacos = plan(
            catalog,
            PlanRequest("tacos", ("marketplace",), WEEK, filters),
            FactShapes(numerator=shapes.denominator, denominator=shapes.numerator),
        )
        roas_options = [
            item
            for item in roas.semantic_qualifiers
            if item.field is CompatibilityField.SOURCE_OPTION
        ]
        tacos_options = [
            item
            for item in tacos.semantic_qualifiers
            if item.field is CompatibilityField.SOURCE_OPTION
        ]
        assert {(item.left, item.right) for item in roas_options} == {
            ("MANUFACTURING", None),
            ("RETAIL", None),
        }
        assert {(item.left, item.right) for item in tacos_options} == {
            (None, "MANUFACTURING"),
            (None, "RETAIL"),
        }

    def test_declared_model_selector_applies_only_to_the_deduplicated_numerator(
        self, catalog: Catalog
    ) -> None:
        model_key = "first_touch_v1"
        filters = {
            **self._amc_filters(),
            "attribution_model_key": model_key,
        }
        built = plan(
            catalog,
            PlanRequest(
                "roas_deduplicated_incl_halo_14d",
                ("date",),
                WEEK,
                filters,
            ),
            self._shapes("fct_deduplicated_temporal_attribution", "fct_ad_exposure"),
        )

        assert built.filters["attribution_model_key"] == model_key
        assert built.carried_columns == ("currency_code",)
        assert built.join is not None
        assert built.join.carried_columns == ("currency_code",)
        assert "attribution_model_key" not in built.join.filters
        model_options = [
            item
            for item in built.semantic_qualifiers
            if item.field is CompatibilityField.SOURCE_OPTION and item.left == model_key
        ]
        assert [(item.left, item.right) for item in model_options] == [(model_key, None)]

    def test_the_result_aliases_interleave_measures_with_withholding(
        self, catalog: Catalog
    ) -> None:
        """A count cannot express this ordering, which is why the adapter reads names.

        Three per side since ADR-035: the attributable sum, the amount withheld, and how many rows
        were withheld. The amount is what lets an answer say *how much* rather than merely that
        something was; the row count is what distinguishes a withheld measure summing to zero from
        nothing having been withheld at all.
        """
        built = self._roas(catalog, ("campaign",))
        assert built.result_aliases == (
            "value_0",
            "withheld_0",
            "withheld_rows_0",
            "value_1",
            "withheld_1",
            "withheld_rows_1",
        )


@pytest.mark.covers("ADR-048")
def test_declared_compatibility_mode_controls_ratio_differences(
    catalog: Catalog, exposure: FactShape
) -> None:
    strict = replace(
        catalog,
        metrics=tuple(
            replace(metric, compatibility_mode=MetricCompatibilityMode.STRICT)
            if metric.name == "ctr"
            else metric
            for metric in catalog.metrics
        ),
    )
    request = PlanRequest(
        "ctr",
        ("campaign",),
        WEEK,
        {
            "marketplace_key": "ATVPDKIKX0DER",
            "channel_key": "DSP",
            "ad_product_key": "DSP",
            "rows_suppressed": "false",
        },
    )
    with pytest.raises(DomainError, match=r"strict|incompatible semantics"):
        plan(strict, request, exposure)

    qualified = plan(catalog, request, exposure)
    assert qualified.semantic_qualifiers
    assert all(
        hasattr(item, "left") and hasattr(item, "right") for item in qualified.semantic_qualifiers
    )
