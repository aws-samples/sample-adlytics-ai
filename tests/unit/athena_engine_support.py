# SPDX-License-Identifier: MIT-0
"""Judge a composed statement against the generated canonical tables, as Athena would.

The unit tier composes SQL but never runs it, so a statement naming a column the table doesn't
have, or comparing a DATE column with a varchar parameter, passes every unit test and fails only
in the engine (COLUMN_NOT_FOUND, TYPE_MISMATCH). This reads `contracts/generated/v2-tables.json`,
the schema the deployed tables are built from, and refuses a statement the engine would refuse.
A fake query port calls `require_engine_accepts` before returning its rows.
"""

from __future__ import annotations

import json
import pathlib
import re

from adapters.outbound.athena.composition import ComposedQuery, compose
from ports.outbound.query import QueryRequest, statement_limit

__all__ = ["EngineRefusedError", "engine_refusal", "require_engine_accepts", "stored_columns"]

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_SCHEMA: dict[str, dict[str, str]] = {
    table["name"]: {column["name"]: column["type"] for column in table["columns"]}
    for table in json.loads((_ROOT / "contracts/generated/v2-tables.json").read_text())[
        "canonicalCurrentTables"
    ]
}
_TABLE = re.compile(r'FROM "([a-z0-9_]+)"')
_ALIAS = re.compile(r'AS "([a-z0-9_]+)"')
_IDENTIFIER = re.compile(r'"([a-z0-9_]+)"')
_VARCHAR_EQUALITY = re.compile(r'"([a-z0-9_]+)" = \?')
_CAST_EQUALITY = re.compile(r'"([a-z0-9_]+)" = CAST\(\? AS ([A-Z0-9(),]+)\)')
_ENGINE_NAMES = {"int": "integer"}
"""Generated type names that differ from the engine's spelling of the same type."""


class EngineRefusedError(AssertionError):
    """The engine would refuse this statement; the message is its error class and column."""


def stored_columns(table: str) -> frozenset[str]:
    """Return the columns the generated schema gives one canonical current table."""
    return frozenset(_SCHEMA[table])


def engine_refusal(sql: str) -> str | None:
    """Return the error the engine would raise for `sql`, or None when it would run it."""
    tables = _TABLE.findall(sql)
    unknown = [table for table in tables if table not in _SCHEMA]
    if unknown:
        return f"TABLE_NOT_FOUND: {unknown[0]}"
    columns: dict[str, str] = {}
    for table in tables:
        columns.update(_SCHEMA[table])
    named = {*tables, *columns, *_ALIAS.findall(sql)}
    missing = [name for name in _IDENTIFIER.findall(sql) if name not in named]
    if missing:
        return f"COLUMN_NOT_FOUND: {missing[0]}"
    typed = [
        name for name in _VARCHAR_EQUALITY.findall(sql) if columns.get(name, "string") != "string"
    ]
    if typed:
        return f"TYPE_MISMATCH: {typed[0]} {columns[typed[0]]} = varchar"
    for name, cast_to in _CAST_EQUALITY.findall(sql):
        if cast_to.lower() != _ENGINE_NAMES.get(columns.get(name, ""), columns.get(name, "")):
            return f"TYPE_MISMATCH: {name} {columns.get(name)} = {cast_to.lower()}"
    return None


def require_engine_accepts(request: QueryRequest) -> ComposedQuery:
    """Compose a request's statement and raise as the engine would when it can't run it.

    The limit is the adapter's own (`statement_limit`), so a grouped fold composes its
    unlimited form here exactly as `AthenaQueryAdapter` would, and a plan the composer can't
    write unlimited fails in the unit tier rather than in a bake.
    """
    composed = compose(request.plan, statement_limit(request.max_rows, request.ceiling))
    refusal = engine_refusal(composed.sql)
    if refusal is not None:
        raise EngineRefusedError(refusal)
    return composed
