# SPDX-License-Identifier: MIT-0
"""How Athena spells a canonical value, asserted exactly.

`literals.py` is where a value becomes SQL text rather than a parameter. Athena cannot receive a
typed `date` or an untyped `NULL` as a parameter at all — it parses a parameter as a scalar
expression — so a statement that writes data has to render it, and the rendering is the only
thing standing between a mapped row and a column of the wrong type.

Asserted character by character for two reasons. A date written as a quoted string inserts into a
`date` column only by accident, and the dangerous near-miss is a value that inserts and then
compares wrongly in a window predicate. And a null written as `0` would erase a distinction the
trap fixtures were hand-built to carry: four different absences mean four different things here.

Originally written inside the dev seed, and moved with the code when the dimension merge needed
the same rendering.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from adapters.outbound.athena.literals import LiteralError, literal_of, quoted, read_bool
from domain.data_contracts.schema import CanonicalType, TargetColumn

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-004")]


def _column(name: str, canonical: CanonicalType, *, nullable: bool = False) -> TargetColumn:
    return TargetColumn(name=name, type=canonical, nullable=nullable, provenance="test")


class TestLiteralsMatchTheColumnType:
    """A seeded value has to arrive in the column's own type, not as text beside it.

    A date column seeded from `'2026-08-13'` fails the insert outright, which is survivable.
    The dangerous case is the near-miss: a value that inserts and then compares wrongly in a
    window predicate, so the seed looks fine and every query over it is quietly short.
    """

    def test_a_date_is_a_date_literal(self) -> None:
        rendered = literal_of(date(2026, 8, 13), _column("date_key", CanonicalType.DATE))
        assert rendered == "DATE '2026-08-13'"

    def test_a_date_delivered_as_text_still_becomes_a_date_literal(self) -> None:
        # Readers hand back what the document carried. A JSON date is a string.
        rendered = literal_of("2026-08-13", _column("date_key", CanonicalType.DATE))
        assert rendered == "DATE '2026-08-13'"

    def test_a_timestamp_is_space_separated_with_no_offset(self) -> None:
        # Athena's `timestamp` is naive: a `T` separator or a trailing offset fails to parse.
        rendered = literal_of(
            datetime(2026, 8, 19, 2, 0, tzinfo=UTC),
            _column("retrieved_at", CanonicalType.TIMESTAMP),
        )
        assert rendered == "TIMESTAMP '2026-08-19 02:00:00.000'"

    def test_an_offset_timestamp_is_normalised_to_utc_not_to_the_local_zone(self) -> None:
        """The instant is preserved, and preserved identically on every machine.

        `astimezone(None)` would convert to whatever zone the runner sits in, so the same
        fixture would seed 19:00 on a laptop in PST and 02:00 in a container in UTC. The seed's
        claim is that re-running it writes byte-identical rows, and a machine-dependent
        rendering breaks that claim without failing anything.
        """
        pacific = datetime(2026, 8, 18, 19, 0, tzinfo=UTC).astimezone(
            datetime.now(UTC).astimezone().tzinfo
        )
        rendered = literal_of(pacific, _column("retrieved_at", CanonicalType.TIMESTAMP))
        assert rendered == "TIMESTAMP '2026-08-18 19:00:00.000'"

    def test_a_decimal_keeps_its_scale_and_is_never_floated(self) -> None:
        # A monetary amount through `float` is how a fourth decimal place disappears.
        rendered = literal_of(Decimal("40.0000"), _column("shipped_revenue", CanonicalType.DECIMAL))
        assert rendered == "DECIMAL '40.0000'"

    def test_a_negative_decimal_survives(self) -> None:
        # Negative units are a real vendor case, not a defect to sanitise away.
        rendered = literal_of(Decimal("-12.5000"), _column("m", CanonicalType.DECIMAL))
        assert rendered == "DECIMAL '-12.5000'"

    def test_a_boolean_is_a_sql_keyword_not_a_quoted_string(self) -> None:
        column = _column("is_current", CanonicalType.BOOLEAN)
        assert literal_of(value=True, column=column) == "true"
        assert literal_of(value=False, column=column) == "false"

    def test_a_boolean_delivered_as_text_is_read_as_a_boolean(self) -> None:
        column = _column("is_current", CanonicalType.BOOLEAN)
        assert literal_of("false", column) == "false"

    def test_an_unreadable_boolean_fails_rather_than_defaulting(self) -> None:
        # Falling back to `False` here would write "not current" over an unknown.
        with pytest.raises(LiteralError, match=r"not a boolean"):
            literal_of("maybe", _column("is_current", CanonicalType.BOOLEAN))

    def test_an_integer_is_unquoted(self) -> None:
        assert literal_of(7, _column("units", CanonicalType.INT)) == "7"

    def test_a_string_is_quoted(self) -> None:
        assert literal_of("B0TRAP00007", _column("amazon_asin", CanonicalType.STRING)) == (
            "'B0TRAP00007'"
        )


class TestNullsStayNull:
    """Four different absences mean four different things, per the null_meaning vocabulary.

    A seed that wrote `0` for a withheld measure or `''` for an inapplicable dimension would
    destroy the distinction the trap fixtures were hand-built to carry, and every downstream
    test of suppression handling would then be testing against fabricated zeroes.
    """

    def test_a_null_measure_is_a_typed_null_not_a_zero(self) -> None:
        rendered = literal_of(None, _column("ordered_revenue", CanonicalType.DECIMAL))
        assert rendered == "CAST(NULL AS decimal(38,4))"

    def test_a_null_string_is_a_typed_null_not_an_empty_string(self) -> None:
        rendered = literal_of(None, _column("suppression_reason", CanonicalType.STRING))
        assert rendered == "CAST(NULL AS string)"

    def test_the_null_carries_its_type(self) -> None:
        """An untyped `NULL` fails to plan when every value in a column is null.

        Athena has nothing to infer the column type from at that point, so the insert is
        rejected — and the all-null column is exactly what a SOURCING-view fixture produces.
        """
        rendered = literal_of(None, _column("d", CanonicalType.DATE))
        assert rendered == "CAST(NULL AS date)"


class TestIdentifiers:
    def test_an_identifier_is_double_quoted(self) -> None:
        # Reserved-word protection rather than escaping: `date_key` is fine today, a future
        # canonical column called `order` or `end` would parse as syntax without this.
        assert quoted("date_key") == '"date_key"'


class TestReadingAValueBack:
    """The same conversion in the other direction, for a value coming out of a result set.

    Athena returns every value as a string, so `is_current` arrives as `"true"`. One
    implementation of "which strings mean true" rather than two, because two would eventually
    disagree about exactly this column — and disagreeing about `is_current` means either hiding
    every version of a product or showing two.
    """

    @pytest.mark.parametrize("text", ["true", "TRUE", " True "])
    def test_the_forms_athena_returns_are_read_as_true(self, text: str) -> None:
        assert read_bool(text) is True

    @pytest.mark.parametrize("text", ["false", "FALSE", "0"])
    def test_the_forms_athena_returns_are_read_as_false(self, text: str) -> None:
        assert read_bool(text) is False

    def test_an_actual_bool_passes_through(self) -> None:
        assert read_bool(value=True) is True

    def test_an_unreadable_value_raises_rather_than_defaulting(self) -> None:
        # Defaulting to False on `is_current` would write "superseded" over an unknown and hide a
        # whole version of a product from every join.
        with pytest.raises(LiteralError):
            read_bool("perhaps")
