# SPDX-License-Identifier: MIT-0
"""Generated candidate-scoped table DQ execution."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from domain.semantics.table_dq import CurrencyRule, RuntimeTableRules, evaluate_table_dq
from domain.semantics.validation import RuleOutcome

pytestmark = [pytest.mark.unit]


def _set_attribute(value: object, name: str, replacement: object) -> None:
    setattr(value, name, replacement)


def _rules() -> RuntimeTableRules:
    return RuntimeTableRules(
        table="fct_probe",
        grain=("marketplace_key", "amazon_asin"),
        required_columns=("marketplace_key", "amazon_asin", "amount"),
        currency_columns=(CurrencyRule("currency_code", False),),
        unique_among_unsuppressed_only=False,
    )


@pytest.mark.covers("ADR-049")
def test_complete_unique_candidate_with_currency_passes() -> None:
    result = evaluate_table_dq(
        _rules(),
        (
            (
                ("amazon_asin", "amazon.asin:b0probe0001"),
                ("amount", Decimal("1.25")),
                ("currency_code", "USD"),
                ("marketplace_key", "amazon_us"),
            ),
        ),
    )
    assert result.outcome is RuleOutcome.PASSED


@pytest.mark.covers("ADR-049")
def test_duplicate_grain_fails_closed() -> None:
    row = (
        ("amazon_asin", "amazon.asin:b0probe0001"),
        ("amount", Decimal("1.25")),
        ("currency_code", "USD"),
        ("marketplace_key", "amazon_us"),
    )
    result = evaluate_table_dq(_rules(), (row, row))
    assert result.outcome is RuleOutcome.FAIL
    assert "repeats its declared grain" in result.detail


@pytest.mark.covers("ADR-049")
def test_missing_required_and_invalid_currency_fail_closed() -> None:
    result = evaluate_table_dq(
        _rules(),
        (
            (
                ("amazon_asin", "amazon.asin:b0probe0001"),
                ("currency_code", "usd"),
                ("marketplace_key", "amazon_us"),
            ),
        ),
    )
    assert result.outcome is RuleOutcome.FAIL
    assert "missing required amount" in result.detail
    assert "invalid currency currency_code" in result.detail


@pytest.mark.covers("ADR-049")
def test_nullable_currency_accepts_only_actual_null() -> None:
    rules = RuntimeTableRules(
        table="fct_probe",
        grain=("id",),
        required_columns=("id",),
        currency_columns=(CurrencyRule("currency_code", True),),
        unique_among_unsuppressed_only=False,
    )
    passed = evaluate_table_dq(rules, ((("id", "one"), ("currency_code", None)),))
    failed = evaluate_table_dq(rules, ((("id", "one"), ("currency_code", "")),))
    assert passed.outcome is RuleOutcome.PASSED
    assert failed.outcome is RuleOutcome.FAIL


@pytest.mark.covers("ADR-049")
def test_required_null_is_missing_even_when_column_exists() -> None:
    result = evaluate_table_dq(
        _rules(),
        (
            (
                ("amazon_asin", "amazon.asin:b0probe0001"),
                ("amount", None),
                ("currency_code", "USD"),
                ("marketplace_key", "amazon_us"),
            ),
        ),
    )
    assert result.outcome is RuleOutcome.FAIL
    assert "missing required amount" in result.detail


@pytest.mark.covers("ADR-049")
def test_uniqueness_excludes_only_boolean_true_suppression() -> None:
    rules = RuntimeTableRules(
        table="fct_probe",
        grain=("id",),
        required_columns=("id",),
        currency_columns=(),
        unique_among_unsuppressed_only=True,
    )
    suppressed = (("id", "one"), ("rows_suppressed", True))
    integer_flag = (("id", "two"), ("rows_suppressed", 1))
    assert evaluate_table_dq(rules, (suppressed, suppressed)).outcome is RuleOutcome.PASSED
    assert evaluate_table_dq(rules, (integer_flag, integer_flag)).outcome is RuleOutcome.FAIL


@pytest.mark.covers("ADR-049")
def test_absent_grain_column_fails_instead_of_comparing_partial_keys() -> None:
    rules = RuntimeTableRules(
        table="fct_probe",
        grain=("id", "day"),
        required_columns=("id",),
        currency_columns=(),
        unique_among_unsuppressed_only=False,
    )
    result = evaluate_table_dq(rules, ((("id", "one"),), (("id", "one"),)))
    assert result.outcome is RuleOutcome.FAIL
    assert result.detail.count("grain column is absent") == 1


@pytest.mark.covers("ADR-049")
@pytest.mark.parametrize(
    ("table", "grain", "required"),
    (("", ("id",), ("id",)), ("fct_probe", (), ("id",)), ("fct_probe", ("",), ("id",))),
)
def test_runtime_rule_identity_and_column_names_are_required(
    table: str, grain: tuple[str, ...], required: tuple[str, ...]
) -> None:
    with pytest.raises(ValueError):
        RuntimeTableRules(table, grain, required, (), False)


@pytest.mark.covers("ADR-049")
def test_currency_rule_name_is_required() -> None:
    with pytest.raises(ValueError, match="column name"):
        CurrencyRule("", False)


@pytest.mark.covers("ADR-049")
def test_runtime_rules_and_currency_rules_are_frozen_slotted_values() -> None:
    currency = CurrencyRule("currency_code", True)
    rules = RuntimeTableRules("fct_probe", ("id",), ("id",), (currency,), False)
    with pytest.raises(FrozenInstanceError):
        _set_attribute(currency, "name", "changed")
    with pytest.raises(FrozenInstanceError):
        _set_attribute(rules, "table", "changed")
    assert not hasattr(currency, "__dict__")
    assert not hasattr(rules, "__dict__")


@pytest.mark.covers("ADR-049")
def test_nullable_currency_does_not_skip_following_currency_rules() -> None:
    rules = RuntimeTableRules(
        "fct_probe",
        ("id",),
        ("id",),
        (CurrencyRule("optional_currency", True), CurrencyRule("required_currency", False)),
        False,
    )
    result = evaluate_table_dq(
        rules,
        ((("id", "one"), ("optional_currency", None), ("required_currency", "usd")),),
    )
    assert result.outcome is RuleOutcome.FAIL
    assert "invalid currency required_currency" in result.detail


@pytest.mark.covers("ADR-049")
def test_duplicate_grain_is_reported_once_even_when_repeated_more_than_twice() -> None:
    row = (
        ("amazon_asin", "amazon.asin:b0probe0001"),
        ("amount", Decimal("1.25")),
        ("currency_code", "USD"),
        ("marketplace_key", "amazon_us"),
    )
    result = evaluate_table_dq(_rules(), (row, row, row))
    assert result.outcome is RuleOutcome.FAIL
    assert result.detail.count("repeats its declared grain") == 1
