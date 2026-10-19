# SPDX-License-Identifier: MIT-0
"""The shared-kernel value objects: Money, DateRange, Grain and AsinKey.

Each of these exists to make a rule structural rather than remembered. Money refuses to be
added across currencies because ADR-004 puts conversion in the semantic layer at a dated
rate; DateRange refuses to carry a timezone because ADR-004 also establishes that
re-bucketing a pre-aggregated day is fabrication; Grain refuses to be empty because a
correct figure at an unstated grain is this product's defining failure mode; AsinKey refuses
a bare ASIN because ADR-023 makes product identity a pair.

The money tests assert the scale factor and the currency, never a bare number — a wrong
divisor is a silent order-of-magnitude error and the assertion has to be able to see it.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from domain.curation import mapping
from domain.narrative import shaped_response
from domain.shared_kernel.values import (
    CURRENCY_PATTERN,
    FOUR_PLACES,
    MONETARY_DECIMAL_PLACES,
    AsinKey,
    DateRange,
    Grain,
    Money,
)

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-004", "ADR-021", "ADR-023")]

USD = "USD"
EUR = "EUR"


class TestMoneyRequiresACurrency:
    def test_an_amount_and_a_currency_construct(self) -> None:
        money = Money(Decimal("8240.00"), USD)

        assert money.amount == Decimal("8240.00")
        assert money.currency_code == USD

    @pytest.mark.parametrize("code", ["usd", "US", "USDX", "", "ATVPDKIKX0DER", "12A"])
    def test_anything_that_is_not_an_iso_4217_code_is_refused(self, code: str) -> None:
        """A currency we cannot convert is discovered in the semantic layer otherwise.

        By then the load that could have rejected it is long finished, so the failure
        surfaces as an unconvertible figure rather than a bad row.
        """
        with pytest.raises(ValueError, match="ISO 4217"):
            Money(Decimal("1.00"), code)

    def test_the_pattern_is_the_one_the_dq_ruleset_uses(self) -> None:
        """One definition, so a value the store accepts cannot be one this type refuses."""
        assert CURRENCY_PATTERN == "^[A-Z]{3}$"


class TestMoneyRefusesFloats:
    def test_a_float_amount_is_refused(self) -> None:
        """11.37 has no exact binary representation and the error survives every later sum.

        Money is a plain dataclass, so the annotation alone stops nothing at runtime — an
        untyped caller reaching this constructor with a float is exactly the path guarded.
        """
        with pytest.raises(ValueError, match="must be a Decimal"):
            Money(11.37, USD)  # pyright: ignore[reportArgumentType] — the guarded path

    @pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity"])
    def test_a_non_finite_amount_is_refused(self, bad: str) -> None:
        with pytest.raises(ValueError, match="not a finite quantity"):
            Money(Decimal(bad), USD)


class TestMoneyRejectsExcessPrecisionRatherThanRounding:
    """Rounding happens once, at the divisor, and nowhere else.

    `scaled()` rounds because division genuinely produces more places than `decimal(38,4)`
    holds. If Money rounded too, a value that had already lost precision somewhere upstream
    would be quietly tidied here instead of reported.
    """

    def test_four_places_is_accepted(self) -> None:
        assert Money(Decimal("7.8000"), USD).amount == Decimal("7.8000")

    def test_a_fifth_place_is_refused(self) -> None:
        with pytest.raises(ValueError, match="decimal places"):
            Money(Decimal("12.34567"), USD)

    def test_the_refusal_points_at_where_rounding_belongs(self) -> None:
        with pytest.raises(ValueError, match="scaled"):
            Money(Decimal("12.34567"), USD)

    def test_what_scaled_produces_is_always_acceptable(self) -> None:
        """The two must agree exactly, which is why the scale has one definition.

        1234567 millicents divided by 1e5 is 12.34567 — five places, which is precisely the
        value Money would reject if `scaled` did not round it first.
        """
        amount = mapping.scaled(1_234_567, 100_000)

        assert Money(amount, USD).amount == amount
        assert MONETARY_DECIMAL_PLACES == 4
        assert Decimal("0.0001") == FOUR_PLACES


class TestMoneyArithmeticRespectsCurrency:
    def test_two_amounts_in_one_currency_add(self) -> None:
        total = Money(Decimal("10.0000"), USD) + Money(Decimal("2.5000"), USD)

        assert total == Money(Decimal("12.5000"), USD)

    def test_adding_across_currencies_raises_rather_than_converting(self) -> None:
        """The rate available here is today's, which is the wrong one.

        ADR-004 requires the rate as of the fact's own date, so any conversion this type
        could perform would be indefensible — last quarter's revenue would move every
        morning. Refusing is what makes "convert late" structural.
        """
        with pytest.raises(ValueError, match="semantic layer"):
            _ = Money(Decimal("10.0000"), USD) + Money(Decimal("2.5000"), EUR)

    def test_subtracting_across_currencies_raises_too(self) -> None:
        with pytest.raises(ValueError, match="subtracted"):
            _ = Money(Decimal("10.0000"), USD) - Money(Decimal("2.5000"), EUR)

    def test_a_negative_amount_is_legitimate(self) -> None:
        """Vendor cancellations make units and revenue genuinely negative.

        There is no `abs()` anywhere in the pipeline for the same reason.
        """
        assert (-Money(Decimal("45.5000"), USD)).amount == Decimal("-45.5000")

    def test_a_total_sums_and_keeps_its_currency(self) -> None:
        amounts = (
            Money(Decimal("1.5000"), USD),
            Money(Decimal("2.2500"), USD),
            Money(Decimal("3.0000"), USD),
        )

        assert Money.total(amounts) == Money(Decimal("6.7500"), USD)

    def test_an_empty_total_raises_rather_than_returning_zero(self) -> None:
        """Zero and "no rows" are different answers, and a zero would need a currency."""
        with pytest.raises(ValueError, match="no rows"):
            Money.total(())

    def test_a_total_across_currencies_raises(self) -> None:
        with pytest.raises(ValueError, match="cannot be added"):
            Money.total((Money(Decimal("1.0000"), USD), Money(Decimal("1.0000"), EUR)))

    def test_zero_still_states_a_currency(self) -> None:
        assert Money.zero(USD) == Money(Decimal(0), USD)


class TestDateRange:
    def test_an_inverted_range_is_refused(self) -> None:
        with pytest.raises(ValueError, match="cannot precede"):
            DateRange(date(2026, 8, 16), date(2026, 8, 10))

    def test_a_single_day_is_a_valid_range(self) -> None:
        day = DateRange(date(2026, 8, 13), date(2026, 8, 13))

        assert day.days == 1

    def test_a_week_is_seven_days_because_both_ends_are_included(self) -> None:
        """`date_key` is a stored date, so a question about a week means seven of them.

        An exclusive end would answer "last week" with six days and a boundary, and the
        figure would look entirely plausible.
        """
        week = DateRange(date(2026, 8, 10), date(2026, 8, 16))

        assert week.days == 7

    def test_containment_is_coverage(self) -> None:
        available = DateRange(date(2026, 8, 1), date(2026, 8, 31))

        assert available.covers(DateRange(date(2026, 8, 5), date(2026, 8, 10))) is True

    def test_partial_overlap_is_not_coverage(self) -> None:
        available = DateRange(date(2026, 8, 1), date(2026, 8, 15))

        assert available.covers(DateRange(date(2026, 8, 10), date(2026, 8, 31))) is False

    def test_exact_boundaries_are_covered(self) -> None:
        available = DateRange(date(2026, 8, 1), date(2026, 8, 31))

        assert available.covers(available) is True

    def test_it_carries_no_timezone(self) -> None:
        """Deliberate, and the reason is ADR-004.

        A day range means different instants depending on which source bucketed the day —
        vendor is always PST, AMC is UTC — and re-bucketing a pre-aggregated day is
        fabrication. So the range stays as asked and the answer states which boundary
        applied, read from the fact's own `source_timezone` column.
        """
        fields = set(DateRange.__dataclass_fields__)

        assert fields == {"start", "end"}

    def test_it_renders_readably(self) -> None:
        assert str(DateRange(date(2026, 8, 10), date(2026, 8, 16))) == "2026-08-10..2026-08-16"


class TestGrainMovedToTheSharedKernel:
    def test_it_cannot_be_empty(self) -> None:
        with pytest.raises(ValueError, match="at least one dimension"):
            Grain(dimensions=())

    def test_it_renders_readably(self) -> None:
        assert str(Grain(("date", "campaign"))) == "date x campaign"

    def test_the_response_envelope_still_exports_it(self) -> None:
        """Moved for direction, not to break the envelope's vocabulary.

        `domain/catalog` was importing `Grain` from `domain/narrative` to describe a
        listing, which had narration owning a concept it only consumes. `shaped_response`
        re-exports it so the envelope still reads as one vocabulary at its boundary.
        """
        assert shaped_response.Grain is Grain
        assert "Grain" in shaped_response.__all__


class TestAsinKeyIsAPair:
    def test_both_halves_construct(self) -> None:
        key = AsinKey("B08N5WRWNW", "US")

        assert str(key) == "B08N5WRWNW@US"

    def test_a_missing_asin_is_refused(self) -> None:
        with pytest.raises(ValueError, match="amazon_asin is required"):
            AsinKey("", "US")

    def test_a_missing_marketplace_is_refused(self) -> None:
        """An ASIN alone is not a product identity, per ADR-023.

        Every fact joins `dim_product` on both, so a key that omitted the marketplace could
        not express the join it exists to describe.
        """
        with pytest.raises(ValueError, match="marketplace_key is required"):
            AsinKey("B08N5WRWNW", "")

    @pytest.mark.parametrize("asin", ["B08N5WRWNW", "0134685991", "B0TRAP00001"])
    def test_no_format_rule_is_invented(self, asin: str) -> None:
        """Book ASINs are ISBNs, so a `B0` prefix or a fixed length would reject real products.

        No source documentation in this repository states a format, and a rule invented here
        would encode our assumptions rather than Amazon's behaviour.
        """
        assert AsinKey(asin, "US").amazon_asin == asin
