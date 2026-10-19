# SPDX-License-Identifier: MIT-0
"""Period comparison and ranking arithmetic — `domain/measurement/comparison.py`.

Driven from hand-built `Measurement` objects rather than through the tool, because everything under
test here is arithmetic over figures that have already been read. That is the whole reason the
module sits downstream of `figures.measure`: no plan, no query and no contract is needed to assert
that a prior of zero yields no percentage.

Four rules carry most of the weight, and each is a specific wrong number:

- **A key present in only one period reports no change.** A `Measurement` counts how many rows
  carried no figure and never which, so an absent key is either a period with no rows or one whose
  rows Amazon withheld — and treating it as zero reports the second as growth from nothing.
- **A prior of zero yields no percentage but keeps its absolute change.** The two are separate
  fields precisely so one can be absent while the other is exact.
- **A grand total covers the population, not the rows shown.** A subtotal presented as a total is
  the reading `top_n` exists to prevent.
- **A metric that does not add gets no total at all**, rather than a number with no meaning.
"""

from __future__ import annotations

from decimal import Decimal
from typing import cast

import pytest

from domain.measurement.comparison import (
    CHANGE_ALIAS,
    CHANGE_PERCENT_ALIAS,
    CURRENT_ALIAS,
    PRIOR_ALIAS,
    RANK_ALIAS,
    Comparison,
    RankOrder,
    RankRequest,
    compare,
    percentage_change,
    rank,
)
from domain.measurement.figures import Figure, Measurement
from domain.narrative.shaped_response import UnitKind, Units
from domain.shared_kernel.errors import InvariantError, ValidationError
from domain.shared_kernel.values import Grain

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-032", "ADR-010")]

ASIN = Grain(("amazon_asin",))
USD = Units(kind=UnitKind.CURRENCY, currency_code="USD")
COUNT = Units(kind=UnitKind.COUNT)
RATIO = Units(kind=UnitKind.RATIO)


def _money(*pairs: tuple[str, str]) -> Measurement:
    """Build a monetary measurement at ASIN grain."""
    return Measurement(
        figures=tuple(
            Figure(dimensions=(("amazon_asin", asin),), value=Decimal(amount))
            for asin, amount in pairs
        ),
        units=USD if pairs else None,
        grain=ASIN,
    )


def _counted(*pairs: tuple[str, int]) -> Measurement:
    """Build a countable measurement at ASIN grain, keeping whole numbers whole."""
    return Measurement(
        figures=tuple(
            Figure(dimensions=(("amazon_asin", asin),), value=count) for asin, count in pairs
        ),
        units=COUNT if pairs else None,
        grain=ASIN,
    )


def _row(comparison: Comparison, asin: str) -> dict[str, object]:
    """Find one compared row by ASIN, so a test never depends on ordering it did not assert."""
    for candidate in comparison.rows:
        if dict(candidate.dimensions)["amazon_asin"] == asin:
            return dict(candidate.labelled())
    raise AssertionError(
        f"{asin} is not in the comparison: {[r.dimensions for r in comparison.rows]}"
    )


class TestAChangeBetweenTwoPeriods:
    def test_a_key_in_both_periods_carries_the_change_and_the_percentage(self) -> None:
        """Asserted with its units and its grain, never as a bare number."""
        comparison = compare(_money(("B0AAA", "120.0000")), _money(("B0AAA", "100.0000")))

        assert comparison.units == USD
        assert comparison.grain == ASIN
        assert _row(comparison, "B0AAA") == {
            "amazon_asin": "B0AAA",
            CURRENT_ALIAS: Decimal("120.0000"),
            PRIOR_ALIAS: Decimal("100.0000"),
            CHANGE_ALIAS: Decimal("20.0000"),
            CHANGE_PERCENT_ALIAS: "+20.00%",
        }

    def test_a_fall_carries_a_signed_percentage(self) -> None:
        comparison = compare(_money(("B0AAA", "75.0000")), _money(("B0AAA", "100.0000")))
        row = _row(comparison, "B0AAA")
        assert row[CHANGE_ALIAS] == Decimal("-25.0000")
        assert row[CHANGE_PERCENT_ALIAS] == "-25.00%"

    def test_the_percentage_is_a_pre_formatted_string_and_not_a_number(self) -> None:
        """The one pre-formatted number in the platform, and the reason is the envelope's units.

        `ResponseData.units` denominates the other three figures in the row. A bare `20.0` beside a
        monetary `120.0000` under `USD` units reads as twenty dollars.
        """
        comparison = compare(_money(("B0AAA", "120.0000")), _money(("B0AAA", "100.0000")))
        percent = _row(comparison, "B0AAA")[CHANGE_PERCENT_ALIAS]
        assert isinstance(percent, str)
        assert percent.endswith("%")

    def test_a_count_keeps_its_change_a_whole_number(self) -> None:
        """A whole number of impressions must not acquire four decimal places on the way out."""
        comparison = compare(_counted(("B0AAA", 900)), _counted(("B0AAA", 600)))
        change = _row(comparison, "B0AAA")[CHANGE_ALIAS]
        assert change == 300
        assert isinstance(change, int)


class TestWhatHasNoChangeToReport:
    def test_a_key_only_in_the_later_period_reports_no_change(self) -> None:
        """The rule the whole module turns on.

        A `Measurement` records how many rows carried no figure, never which, so this absence is
        either a period with no rows or one Amazon withheld. Reporting it as growth from zero would
        state the second as a fact.
        """
        comparison = compare(_money(("B0AAA", "40.0000")), _money())

        assert _row(comparison, "B0AAA") == {
            "amazon_asin": "B0AAA",
            CURRENT_ALIAS: Decimal("40.0000"),
            PRIOR_ALIAS: None,
            CHANGE_ALIAS: None,
            CHANGE_PERCENT_ALIAS: None,
        }
        assert comparison.one_sided == 1
        assert comparison.comparable == ()

    def test_a_key_only_in_the_earlier_period_is_still_carried(self) -> None:
        """A campaign that ran last month and not this one is what a comparison is looked at for.

        Dropping it would make the later period look unchanged rather than shorter.
        """
        comparison = compare(_money(), _money(("B0GONE", "90.0000")))
        row = _row(comparison, "B0GONE")
        assert row[CURRENT_ALIAS] is None
        assert row[PRIOR_ALIAS] == Decimal("90.0000")
        assert row[CHANGE_ALIAS] is None

    def test_a_prior_of_zero_keeps_its_change_and_loses_its_percentage(self) -> None:
        """Two separate absences, and only one of them applies here.

        The division is undefined, so there is no percentage. The subtraction is exact, so the
        change is reported — which is why they are separate fields rather than one.
        """
        comparison = compare(_money(("B0AAA", "40.0000")), _money(("B0AAA", "0.0000")))
        row = _row(comparison, "B0AAA")
        assert row[CHANGE_ALIAS] == Decimal("40.0000")
        assert row[CHANGE_PERCENT_ALIAS] is None
        # Still comparable: both periods produced a figure, so the change is real.
        assert comparison.one_sided == 0

    def test_a_zero_prior_is_tested_numerically_not_by_truthiness(self) -> None:
        """A canonical decimal text such as "0" is truthy; the zero test must not be.

        The deployed retail 1D page 500'd on exactly this: an artifact decoder re-attests
        each comparison's percentage with the wire's string values, and a zero prior day
        reached the division as the string "0".
        """
        assert percentage_change(Decimal("3.6000"), cast("Decimal", "0")) is None
        assert percentage_change(Decimal("3.6000"), cast("Decimal", "0.0000")) is None
        assert percentage_change(Decimal("3"), Decimal("0")) is None
        assert percentage_change(Decimal("3"), Decimal("2")) == "+50.00%"

    def test_two_empty_periods_carry_no_rows_and_no_units(self) -> None:
        """An absence rather than a zero. A denominated nothing claims a currency it cannot know."""
        comparison = compare(_money(), _money())
        assert comparison.is_empty
        assert comparison.units is None
        assert comparison.row_count == 0

    def test_an_empty_later_period_takes_its_units_from_the_earlier_one(self) -> None:
        comparison = compare(_money(), _money(("B0AAA", "90.0000")))
        assert comparison.units == USD
        assert comparison.one_sided == 1


class TestWhatAComparisonRefuses:
    def test_two_currencies_are_refused_rather_than_converted(self) -> None:
        """The only rate available here is today's, and ADR-004 requires the fact's own date."""
        euros = Measurement(
            figures=(Figure(dimensions=(("amazon_asin", "B0AAA"),), value=Decimal("90.0000")),),
            units=Units(kind=UnitKind.CURRENCY, currency_code="EUR"),
            grain=ASIN,
        )
        with pytest.raises(InvariantError, match="same units"):
            compare(_money(("B0AAA", "100.0000")), euros)

    def test_two_grains_are_refused(self) -> None:
        by_campaign = Measurement(
            figures=(Figure(dimensions=(("campaign_key", "c-1"),), value=Decimal("1.0000")),),
            units=USD,
            grain=Grain(("campaign_key",)),
        )
        with pytest.raises(InvariantError, match="different grains"):
            compare(_money(("B0AAA", "100.0000")), by_campaign)

    def test_rows_without_units_cannot_be_constructed(self) -> None:
        """The third statement of one rule: a label for figures is absent exactly when they are."""
        with pytest.raises(InvariantError, match="no units"):
            Comparison(
                rows=compare(_money(("B0AAA", "1.0000")), _money()).rows,
                units=None,
                grain=ASIN,
            )

    def test_units_without_rows_cannot_be_constructed(self) -> None:
        with pytest.raises(InvariantError, match="no rows"):
            Comparison(rows=(), units=USD, grain=ASIN)


class TestTheOrderIsStable:
    def test_rows_are_ordered_by_dimension_and_not_by_movement(self) -> None:
        """Ordering a comparison by movement would answer the ranking question unasked.

        And a ranking carries the grand total that makes it honest, which a comparison does not.
        """
        comparison = compare(
            _money(("B0CCC", "10.0000"), ("B0AAA", "500.0000"), ("B0BBB", "1.0000")),
            _money(("B0AAA", "1.0000"), ("B0BBB", "1.0000"), ("B0CCC", "1.0000")),
        )
        assert [dict(row.dimensions)["amazon_asin"] for row in comparison.rows] == [
            "B0AAA",
            "B0BBB",
            "B0CCC",
        ]


class TestARankingByValue:
    def test_it_returns_the_largest_n_numbered_from_one(self) -> None:
        ranking = rank(
            RankRequest(
                measurement=_money(
                    ("B0AAA", "10.0000"), ("B0BBB", "300.0000"), ("B0CCC", "200.0000")
                ),
                n=2,
                additive=True,
            )
        )

        assert ranking.units == USD
        assert ranking.grain == ASIN
        assert [row[RANK_ALIAS] for row in ranking.rows] == [1, 2]
        assert [row["amazon_asin"] for row in ranking.rows] == ["B0BBB", "B0CCC"]

    def test_the_total_covers_the_population_and_the_subtotal_covers_the_rows(self) -> None:
        """Both pre-computed, because the reader who would otherwise add them up is the model.

        A grand total alone only answers "is that most of my spend" if somebody sums the rows, and
        the model must never sum, divide or infer a figure.
        """
        ranking = rank(
            RankRequest(
                measurement=_money(
                    ("B0AAA", "10.0000"), ("B0BBB", "300.0000"), ("B0CCC", "200.0000")
                ),
                n=2,
                additive=True,
            )
        )

        assert ranking.total == Decimal("510.0000")
        assert ranking.shown == Decimal("500.0000")
        assert ranking.considered == 3
        assert ranking.excluded == 1

    def test_a_monetary_total_keeps_four_decimal_places(self) -> None:
        """A wrong scale on money is a silent order-of-magnitude error."""
        ranking = rank(RankRequest(measurement=_money(("B0AAA", "11.3700")), n=1, additive=True))
        assert ranking.total == Decimal("11.3700")
        assert str(ranking.total) == "11.3700"

    def test_a_size_larger_than_the_population_returns_the_population(self) -> None:
        ranking = rank(RankRequest(measurement=_money(("B0AAA", "10.0000")), n=50, additive=True))
        assert len(ranking.rows) == 1
        assert ranking.excluded == 0

    def test_equal_figures_come_back_in_a_stable_order(self) -> None:
        """Without a tiebreak the same question could return a different last row on each call."""
        tied = _money(("B0CCC", "5.0000"), ("B0AAA", "5.0000"), ("B0BBB", "5.0000"))
        first = rank(RankRequest(measurement=tied, n=2, additive=True))
        second = rank(RankRequest(measurement=tied, n=2, additive=True))
        assert [row["amazon_asin"] for row in first.rows] == ["B0AAA", "B0BBB"]
        assert first.rows == second.rows

    def test_an_empty_population_states_no_units(self) -> None:
        ranking = rank(RankRequest(measurement=_money(), n=5, additive=True))
        assert ranking.rows == ()
        assert ranking.units is None
        assert ranking.total is None


class TestWhenNoTotalIsHonest:
    def test_a_ratio_gets_no_total(self) -> None:
        """A sum of ROAS across campaigns has no denominator and no meaning."""
        ratios = Measurement(
            figures=(
                Figure(dimensions=(("campaign_key", "c-1"),), value=Decimal("2.5000")),
                Figure(dimensions=(("campaign_key", "c-2"),), value=Decimal("1.5000")),
            ),
            units=RATIO,
            grain=Grain(("campaign_key",)),
        )
        ranking = rank(RankRequest(measurement=ratios, n=2, additive=True))

        assert len(ranking.rows) == 2
        assert ranking.total is None
        assert ranking.shown is None

    def test_a_non_additive_metric_gets_no_total_even_in_countable_units(self) -> None:
        """`reach_users` is a distinct count: two campaigns' reach is not their sum.

        The unit kind does not say so, which is why the caller resolves it from the dictionary.
        """
        ranking = rank(
            RankRequest(measurement=_counted(("B0AAA", 900), ("B0BBB", 100)), n=2, additive=False)
        )
        assert len(ranking.rows) == 2
        assert ranking.total is None

    def test_the_same_counts_do_total_when_the_metric_adds(self) -> None:
        ranking = rank(
            RankRequest(measurement=_counted(("B0AAA", 900), ("B0BBB", 100)), n=1, additive=True)
        )
        assert ranking.total == 1_000
        assert ranking.shown == 900


class TestARankingByChange:
    def test_it_orders_by_the_largest_increase(self) -> None:
        comparison = compare(
            _money(("B0AAA", "110.0000"), ("B0BBB", "300.0000"), ("B0CCC", "90.0000")),
            _money(("B0AAA", "100.0000"), ("B0BBB", "100.0000"), ("B0CCC", "100.0000")),
        )
        ranking = rank(
            RankRequest(
                measurement=_money(
                    ("B0AAA", "110.0000"), ("B0BBB", "300.0000"), ("B0CCC", "90.0000")
                ),
                n=2,
                additive=True,
                order_by=RankOrder.CHANGE,
                comparison=comparison,
            )
        )

        assert [row["amazon_asin"] for row in ranking.rows] == ["B0BBB", "B0AAA"]
        assert [row[CHANGE_ALIAS] for row in ranking.rows] == [
            Decimal("200.0000"),
            Decimal("10.0000"),
        ]

    def test_a_key_in_only_one_period_is_excluded_and_counted(self) -> None:
        """It has no change to rank by, and placing it at either end would assert a direction."""
        population = _money(("B0AAA", "110.0000"), ("B0NEW", "500.0000"))
        comparison = compare(population, _money(("B0AAA", "100.0000")))
        ranking = rank(
            RankRequest(
                measurement=population,
                n=5,
                additive=True,
                order_by=RankOrder.CHANGE,
                comparison=comparison,
            )
        )

        assert [row["amazon_asin"] for row in ranking.rows] == ["B0AAA"]
        assert ranking.not_comparable == 1
        assert ranking.considered == 2

    def test_the_totals_are_the_later_periods_figures_and_not_the_changes(self) -> None:
        """A total of changes is a net movement, which is a misleading denominator for growth.

        Ten campaigns growing a hundred each net the same as one growing a thousand while nine
        stood still.
        """
        population = _money(("B0AAA", "110.0000"), ("B0BBB", "300.0000"))
        comparison = compare(population, _money(("B0AAA", "100.0000"), ("B0BBB", "100.0000")))
        ranking = rank(
            RankRequest(
                measurement=population,
                n=1,
                additive=True,
                order_by=RankOrder.CHANGE,
                comparison=comparison,
            )
        )

        assert ranking.total == Decimal("410.0000")
        assert ranking.shown == Decimal("300.0000")

    def test_nothing_comparable_states_no_units(self) -> None:
        """Reachable, not defensive: the comparison knows its currency and has no rows to state.

        The envelope refuses that pairing one layer later with no idea which tool produced it.
        """
        population = _money(("B0NEW", "500.0000"))
        ranking = rank(
            RankRequest(
                measurement=population,
                n=5,
                additive=True,
                order_by=RankOrder.CHANGE,
                comparison=compare(population, _money()),
            )
        )
        assert ranking.rows == ()
        assert ranking.units is None
        assert ranking.not_comparable == 1


class TestWhatARankingRefuses:
    def test_a_size_below_one_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="at least 1"):
            RankRequest(measurement=_money(("B0AAA", "1.0000")), n=0, additive=True)

    def test_ordering_by_change_without_a_comparison_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="prior period"):
            RankRequest(
                measurement=_money(("B0AAA", "1.0000")),
                n=1,
                additive=True,
                order_by=RankOrder.CHANGE,
            )

    def test_ordering_by_value_with_a_comparison_is_refused(self) -> None:
        """Refused rather than ignored: no row would report the prior period that was supplied."""
        with pytest.raises(ValidationError, match="orders by value"):
            RankRequest(
                measurement=_money(("B0AAA", "1.0000")),
                n=1,
                additive=True,
                order_by=RankOrder.VALUE,
                comparison=compare(_money(("B0AAA", "1.0000")), _money()),
            )
