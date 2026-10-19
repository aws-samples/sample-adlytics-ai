# SPDX-License-Identifier: MIT-0
"""Encode a value so Athena reads it as that value and nothing else.

**Athena's `ExecutionParameters` do not bind values. They are parsed as SQL expressions.**

[verified 2026-08-19] against the real engine, by selecting the parameter back. This contradicts
what the read path assumed until real rows existed to expose it — the empty tables meant no cast
was ever evaluated, so every probe passed:

    parameter sent            what Athena produced
    MANUFACTURING             the varchar `MANUFACTURING`, 13 characters
    'MANUFACTURING'           the same 13 characters — an outer quote pair is consumed
    'Levi''s'                 `Levi's`, 6 characters — SQL's own doubling is honoured
    2026-08-10                the integer 2008, because it parses as 2026 - 8 - 10
    upper('leak')             `LEAK` — a scalar function is *evaluated*
    'a' || 'b'                `ab` — an operator is evaluated
    x' OR 'a'='a              a parse error, not a widened predicate

The last two lines are the whole finding. A parameter is parsed as one scalar expression and
substituted as a single node, so it cannot restructure the statement around it: `'x' OR 1=1` in
a `WHERE col = ?` fails as a type error rather than matching every row. That bounds the damage,
and it is the only reason this was a correctness defect rather than an exfiltration one.

The correctness half is worse than it looks. `2026-08-10` became `2008` *silently* in the sense
that matters: no value was mangled and returned, but the query failed with
`INVALID_CAST_ARGUMENT: Value cannot be cast to date: 2008` — a message about a number nobody
sent. A shape that produced a plausible date instead would have answered the wrong window.

So the encoding is ours to do. Every value is wrapped in single quotes with embedded quotes
doubled, which makes Athena's parse of the parameter yield exactly a varchar; the statement then
casts it to the column's type. Quoting here rather than in each composer because this is the one
function every statement passes through on its way to the SDK, and a rule applied in one place
has no path around it.
"""

from __future__ import annotations

__all__ = ["sql_literal"]


def sql_literal(value: str) -> str:
    """Wrap a value as a SQL string literal, doubling any quote it contains.

    Doubling is SQL's own escape and Athena honours it, so the value round-trips unchanged
    rather than being sanitised into something that no longer matches the source data. An
    apostrophe in a brand name is data, and stripping it would silently return the wrong rows.

    The result is *always* a varchar to Athena, whatever the value looks like. That is the
    point: an unquoted `2026-08-10` is arithmetic and an unquoted `upper('x')` is a function
    call, and neither is a value anyone asked to send.
    """
    return "'" + value.replace("'", "''") + "'"
