# SPDX-License-Identifier: MIT-0
"""How Athena spells a canonical value in a statement.

Athena knowledge rather than any one job's, which is why it sits here: a `date` is written
`DATE '2026-08-13'` and a null needs its type, and both are true of every statement this
repository will ever send. It was written first inside the dev seed and moved here when the
dimension merge needed the same thing — a second implementation of "how Athena spells a
decimal" is how a seeded table and a loaded table come to disagree about a fourth decimal
place.

**A literal is not a parameter, and the difference is deliberate.** Everything a *caller*
supplies travels as an `ExecutionParameters` entry, encoded once in `execution.start` — see
`parameters.py` for why that encoding exists at all. What is rendered here is data this
repository already holds: a mapped row on its way into a table, or a validity window the SCD2
merge computed. Those cannot be parameters, because Athena parses a parameter as a scalar
expression and so has no way to receive a typed `date` or an untyped `NULL` at all.

Two properties matter more than the type table:

- **Types are written so Athena parses them into the column's own type**, not as strings beside
  it. A date column written from `'2026-08-13'` is rejected outright, which is survivable; the
  dangerous case is a value that inserts and then compares wrongly in a window predicate.
- **Nulls stay null, and carry their type.** Four different absences mean four different things
  in this model, so a `0` or an `''` here would erase a distinction the whole null-meaning
  vocabulary exists to preserve. The type is needed because an untyped `NULL` fails to plan
  when every value in a column is null — which is exactly what a SOURCING-view delivery
  produces.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal

from domain.data_contracts.schema import CanonicalType, TargetColumn

__all__ = ["LiteralError", "literal_of", "quoted", "read_bool"]


class LiteralError(Exception):
    """A value cannot be expressed as a literal of its column's type."""


def quoted(identifier: str) -> str:
    """Quote a canonical identifier for Athena.

    No escaping of an embedded quote, and that is not an oversight: every identifier reaching
    a statement comes from a data contract, and the contract gate constrains a column name to
    lowercase, digits and underscores. An embedded quote would mean that validation had already
    failed, so escaping it here would hide the failure rather than contain it.

    Quoting at all is reserved-word protection. `date_key` is fine today; a future canonical
    column called `order` or `end` would parse as syntax without this.
    """
    return f'"{identifier}"'


def literal_of(value: object, column: TargetColumn) -> str:
    """Render one value as a SQL literal of that column's type."""
    if value is None:
        return f"CAST(NULL AS {column.type.value})"

    render = _RENDERERS.get(column.type)
    if render is None:  # pragma: no cover — every CanonicalType is in the table below
        raise LiteralError(f"{column.name}: no literal form for {column.type}.")
    return render(value)


def _string(value: object) -> str:
    """Quote a string, doubling any embedded quote as SQL itself does.

    A value containing one is not hostile; it is an apostrophe in a product title. Doubling is
    SQL's own escape, so the value round-trips rather than being sanitised into something that
    no longer matches the source document.
    """
    return "'" + str(value).replace("'", "''") + "'"


_RENDERERS: dict[CanonicalType, Callable[[object], str]] = {
    CanonicalType.STRING: _string,
    CanonicalType.BOOLEAN: lambda value: "true" if read_bool(value) else "false",
    CanonicalType.INT: lambda value: str(int(str(value))),
    CanonicalType.BIGINT: lambda value: str(int(str(value))),
    CanonicalType.DOUBLE: lambda value: str(float(str(value))),
    CanonicalType.DECIMAL: lambda value: f"DECIMAL '{Decimal(str(value))}'",
    CanonicalType.DATE: lambda value: f"DATE '{_as_date(value).isoformat()}'",
    CanonicalType.TIMESTAMP: lambda value: f"TIMESTAMP '{_as_timestamp(value)}'",
}


def read_bool(value: object) -> bool:
    """Read a boolean that may have arrived as the string a CSV or a query result carries.

    Public because reading a value back out of a result set needs the same conversion as writing
    one in, and a second copy of "which strings mean true" would eventually disagree with this
    one about `is_current`.
    """
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"true", "1"}:
        return True
    if text in {"false", "0"}:
        return False
    # Not defaulted to False. On `is_current` that would write "superseded" over an unknown,
    # and hide a whole version of a product from every join.
    raise LiteralError(f"{value!r} is not a boolean.")


def _as_date(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _as_timestamp(value: object) -> str:
    """Render a timestamp the way Athena's literal parser expects.

    Space-separated, no `T`, and no offset: Athena's `timestamp` is naive, so an offset is
    rejected. The instant is preserved by normalising to **UTC** rather than dropped, because
    these values are provenance and validity windows, and shifting one misdates a load or a
    version.

    UTC explicitly, never the local zone. `astimezone(None)` converts to whatever zone the
    running machine is in, so the same row would be written as 19:00 on a laptop in PST and
    02:00 in a container in UTC — and a load that is not reproducible cannot be re-run to prove
    it changed nothing.

    A naive value is taken as already-UTC rather than localised, for the same reason: guessing a
    zone here would silently shift an instant. Which zone a *measurement* belongs to is a
    separate matter, carried per fact in `source_timezone` (ADR-004).
    """
    moment = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if moment.tzinfo is not None:
        moment = moment.astimezone(UTC).replace(tzinfo=None)
    return moment.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
