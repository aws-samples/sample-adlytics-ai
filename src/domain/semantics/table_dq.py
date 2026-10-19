# SPDX-License-Identifier: MIT-0
"""Candidate-scoped executable table DQ derived from generated contract rules."""

from __future__ import annotations

import re
from dataclasses import dataclass

from domain.semantics.canonical_compiler import CanonicalRow
from domain.semantics.validation import (
    PrepublicationRuleFamily,
    PrepublicationRuleResult,
    RuleOutcome,
)

__all__ = ["CurrencyRule", "RuntimeTableRules", "evaluate_table_dq"]

_CURRENCY = re.compile(r"^[A-Z]{3}$")


@dataclass(frozen=True, slots=True)
class CurrencyRule:
    """Currency shape and whether contract-governed null is admissible."""

    name: str
    nullable: bool

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("currency DQ rule requires a column name.")


@dataclass(frozen=True, slots=True)
class RuntimeTableRules:
    """Generated uniqueness, completeness, and currency rules for one table."""

    table: str
    grain: tuple[str, ...]
    required_columns: tuple[str, ...]
    currency_columns: tuple[CurrencyRule, ...]
    unique_among_unsuppressed_only: bool

    def __post_init__(self) -> None:
        if not self.table.strip() or not self.grain:
            raise ValueError("runtime table DQ requires table and grain.")
        names = (
            *self.grain,
            *self.required_columns,
            *(item.name for item in self.currency_columns),
        )
        if any(not name.strip() for name in names):
            raise ValueError("runtime table DQ contains an empty column name.")


def _mapping(row: CanonicalRow) -> dict[str, object]:
    return dict(row)


def evaluate_table_dq(
    rules: RuntimeTableRules,
    rows: tuple[CanonicalRow, ...],
) -> PrepublicationRuleResult:
    """Evaluate exactly one candidate's rows; unknown, duplicate, or malformed data fails closed."""
    failures: list[str] = []
    mapped = tuple(_mapping(row) for row in rows)
    for index, row in enumerate(mapped):
        missing = [name for name in rules.required_columns if name not in row or row[name] is None]
        if missing:
            failures.append(f"row {index} missing required {', '.join(sorted(missing))}")
        for currency in rules.currency_columns:
            value = row.get(currency.name)
            if value is None and currency.nullable:
                continue
            if not isinstance(value, str) or _CURRENCY.fullmatch(value) is None:
                failures.append(f"row {index} has invalid currency {currency.name}")
    uniqueness_rows = (
        tuple(row for row in mapped if row.get("rows_suppressed") is not True)
        if rules.unique_among_unsuppressed_only
        else mapped
    )
    seen: set[tuple[object, ...]] = set()
    for row in uniqueness_rows:
        if any(name not in row for name in rules.grain):
            failures.append("candidate cannot evaluate uniqueness because a grain column is absent")
            break
        key = tuple(row[name] for name in rules.grain)
        if key in seen:
            failures.append("candidate repeats its declared grain")
            break
        seen.add(key)
    return PrepublicationRuleResult(
        family=PrepublicationRuleFamily.TABLE_DQ,
        rule_id=f"table_dq.{rules.table}.v1",
        outcome=RuleOutcome.FAIL if failures else RuleOutcome.PASSED,
        detail=(
            "; ".join(failures) if failures else "Candidate-scoped generated table rules passed."
        ),
    )
