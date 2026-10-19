# SPDX-License-Identifier: MIT-0
"""Strict loader for generated candidate-scoped table DQ rule specifications."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from domain.semantics.table_dq import CurrencyRule, RuntimeTableRules

__all__ = ["load_runtime_table_rules"]


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} is not an object.")
    return {str(key): item for key, item in cast("dict[object, object]", value).items()}


def _strings(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{label} is not an array of non-empty strings.")
    items = cast("list[object]", value)
    if not all(isinstance(item, str) and item for item in items):
        raise ValueError(f"{label} is not an array of non-empty strings.")
    return tuple(cast("list[str]", items))


def load_runtime_table_rules(path: Path) -> dict[str, RuntimeTableRules]:
    """Load every generated ruleset exactly once and reject missing executable semantics."""
    root = _object(json.loads(path.read_text()), "generated DQ artifact")
    raw_entries = root.get("rulesets")
    if not isinstance(raw_entries, list) or not raw_entries:
        raise ValueError("generated DQ artifact declares no rulesets.")
    loaded: dict[str, RuntimeTableRules] = {}
    for index, raw in enumerate(cast("list[object]", raw_entries)):
        entry = _object(raw, f"rulesets[{index}]")
        table = entry.get("table")
        if not isinstance(table, str) or not table:
            raise ValueError(f"rulesets[{index}] has no table.")
        if table in loaded:
            raise ValueError(f"generated DQ artifact repeats {table}.")
        runtime = _object(entry.get("runtimeRules"), f"{table}.runtimeRules")
        raw_currencies = runtime.get("currencyColumns")
        if not isinstance(raw_currencies, list):
            raise ValueError(f"{table}.runtimeRules.currencyColumns is not an array.")
        currencies: list[CurrencyRule] = []
        for position, raw_currency in enumerate(cast("list[object]", raw_currencies)):
            currency = _object(raw_currency, f"{table}.currencyColumns[{position}]")
            name = currency.get("name")
            nullable = currency.get("nullable")
            if not isinstance(name, str) or not name or not isinstance(nullable, bool):
                raise ValueError(f"{table}.currencyColumns[{position}] is invalid.")
            currencies.append(CurrencyRule(name, nullable))
        unique = runtime.get("uniqueAmongUnsuppressedOnly")
        if not isinstance(unique, bool):
            raise ValueError(f"{table}.runtimeRules has no uniqueness scope.")
        loaded[table] = RuntimeTableRules(
            table=table,
            grain=_strings(runtime.get("grain"), f"{table}.runtimeRules.grain"),
            required_columns=_strings(
                runtime.get("requiredColumns"), f"{table}.runtimeRules.requiredColumns"
            ),
            currency_columns=tuple(currencies),
            unique_among_unsuppressed_only=unique,
        )
    return loaded
