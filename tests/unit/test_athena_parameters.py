# SPDX-License-Identifier: MIT-0
"""Encoding a value so Athena reads it as that value.

The whole reason this module exists is a measurement: Athena parses an `ExecutionParameters`
entry as a SQL expression rather than binding it as an opaque value, so an unquoted entry is
*evaluated*. `2026-08-10` becomes the integer 2008 and `upper('x')` runs. `parameters.py`
records the probes.

That makes `sql_literal` the last thing standing between a caller's value and the engine, so
the cases here are the ones a real value will actually take rather than a sweep of the space:
a date, an apostrophe, and the shapes that get evaluated when they are not quoted.
"""

from __future__ import annotations

import pytest

from adapters.outbound.athena.parameters import sql_literal

pytestmark = [pytest.mark.unit, pytest.mark.covers("F-14")]


class TestQuoting:
    def test_a_plain_value_is_wrapped_in_single_quotes(self) -> None:
        assert sql_literal("MANUFACTURING") == "'MANUFACTURING'"

    def test_an_iso_date_is_quoted_so_it_is_not_arithmetic(self) -> None:
        """The defect this function was written for.

        Unquoted, `2026-08-10` parses as 2026 - 8 - 10 and the engine reports
        `Value cannot be cast to date: 2008` — a failure about a number nobody sent. Quoted, it
        is a ten-character string the statement can cast.
        """
        assert sql_literal("2026-08-10") == "'2026-08-10'"

    def test_a_function_call_becomes_inert_text(self) -> None:
        # Unquoted, `upper('leak')` is evaluated by Athena and returns LEAK. Quoted, it is a
        # string that matches nothing, which is what a value that looks like SQL should do.
        assert sql_literal("upper('leak')") == "'upper(''leak'')'"

    def test_a_concatenation_becomes_inert_text(self) -> None:
        assert sql_literal("'a' || 'b'") == "'''a'' || ''b'''"


class TestEmbeddedQuotes:
    def test_a_single_quote_is_doubled(self) -> None:
        """An apostrophe in a brand or product title is data, not an attack.

        Doubling is SQL's own escape and Athena honours it, so the value round-trips: stripping
        the character instead would silently match different rows.
        """
        assert sql_literal("Levi's") == "'Levi''s'"

    def test_a_quote_at_each_end_is_doubled_too(self) -> None:
        # Not stripped as if the caller had pre-quoted it. A value is taken literally, and
        # unwrapping here would let a caller control the quoting.
        assert sql_literal("'x'") == "'''x'''"

    def test_a_breakout_attempt_stays_one_string(self) -> None:
        """The classic payload, and what makes the result safe is that it has no odd quote.

        Unescaped, `x' OR 'a'='a` is parsed by Athena as an expression — verified, it fails with
        `Logical expression term must evaluate to a boolean`. Escaped, every quote is doubled, so
        the literal closes exactly where it opened.
        """
        encoded = sql_literal("x' OR 'a'='a")
        assert encoded == "'x'' OR ''a''=''a'"
        # The property that matters, stated independently of the exact string: the quotes
        # inside are all doubled, so the literal cannot terminate early.
        assert encoded.startswith("'")
        assert encoded.endswith("'")
        assert encoded[1:-1].count("'") % 2 == 0

    def test_a_comment_marker_is_not_a_comment(self) -> None:
        assert sql_literal("' OR 1=1 --") == "''' OR 1=1 --'"


class TestEdges:
    def test_an_empty_value_is_an_empty_string_literal(self) -> None:
        # Not omitted and not a NULL: an empty filter value is a value that matches nothing,
        # and turning it into NULL would silently match nothing for a different reason.
        assert sql_literal("") == "''"

    def test_a_value_that_is_only_quotes_stays_balanced(self) -> None:
        encoded = sql_literal("''")
        assert encoded[1:-1].count("'") % 2 == 0
