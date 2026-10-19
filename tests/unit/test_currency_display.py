# SPDX-License-Identifier: MIT-0
"""Two-place monetary display (ADR-084): rounding, sign, grouping, and the tolerant decode rule.

Every value here is a monetary amount in USD major units (dollars), at the scale named in each
test; the display scale is always two places (cents).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from domain.presentation.currency_display import (
    CurrencyTextStyle,
    canonical_matches,
    currency_display_text,
    currency_display_value,
    display_value_mode,
    without_display_values,
)

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-084")]


@pytest.mark.parametrize(
    ("dollars", "display"),
    [
        (Decimal("0.125"), "0.13"),
        (Decimal("-0.125"), "-0.13"),
        (Decimal("0.135"), "0.14"),
        (Decimal("75459.6400"), "75459.64"),
        (Decimal("75459.6450"), "75459.65"),
        (Decimal("-12.3"), "-12.30"),
    ],
    ids=["tie-up", "negative-tie-away-from-zero", "tie-odd", "four-place", "four-place-tie", "neg"],
)
def test_ties_round_away_from_zero_at_two_places(dollars: Decimal, display: str) -> None:
    assert currency_display_value(dollars) == display


@pytest.mark.parametrize(
    "dollars", [Decimal("-0.004"), Decimal("0.004"), Decimal("-0.0000"), 0, "0"]
)
def test_a_value_that_rounds_to_zero_displays_unsigned_zero(dollars: Decimal | int | str) -> None:
    assert currency_display_value(dollars) == "0.00"


def test_a_whole_dollar_amount_displays_two_places() -> None:
    assert currency_display_value(1234) == "1234.00"


def test_a_38_digit_amount_keeps_full_precision() -> None:
    """A decimal(38,4) magnitude in dollars, which the default 28-digit context cannot quantize."""
    dollars = Decimal("1234567890123456789012345678901234.5678")

    assert currency_display_value(dollars) == "1234567890123456789012345678901234.57"


@pytest.mark.parametrize("dollars", [Decimal("NaN"), Decimal("Infinity"), "-Infinity", "x"])
def test_a_non_finite_or_non_decimal_amount_is_refused(dollars: Decimal | str) -> None:
    with pytest.raises(ValueError, match="currency display value"):
        currency_display_value(dollars)


def test_coded_text_groups_digits_and_appends_the_code() -> None:
    assert currency_display_text(Decimal("75459.6400"), "USD") == "75,459.64 USD"
    assert currency_display_text(Decimal("-1234.5"), "EUR") == "-1,234.50 EUR"
    assert currency_display_text(Decimal("0.004"), "USD") == "0.00 USD"


@pytest.mark.parametrize(
    ("dollars", "text"),
    [
        (Decimal("0.004"), "0.00"),
        (Decimal("-0.004"), "0.00"),
        (0, "0.00"),
        (Decimal("0.005"), "+0.01"),
        (Decimal("-0.125"), "-0.13"),
        (Decimal("1234.5"), "+1,234.50"),
    ],
)
def test_signed_bare_text_decides_the_sign_on_the_rounded_value(
    dollars: Decimal | int, text: str
) -> None:
    assert currency_display_text(dollars, None, CurrencyTextStyle.SIGNED_BARE) == text


@pytest.mark.parametrize("code", [None, ""])
def test_coded_text_refuses_a_missing_currency_code(code: str | None) -> None:
    with pytest.raises(ValueError, match="currency code"):
        currency_display_text(Decimal("1.00"), code)


def _metric(value: str, display: str | None = None, *, carry: bool = True) -> dict[str, object]:
    item: dict[str, object] = {
        "basis": "ad_attributed",
        "grain": "campaign",
        "period": "current",
        "units": {"kind": "currency", "currency_code": "USD"},
        "value": value,
    }
    if carry:
        item["display_value"] = display
    return item


def test_display_value_mode_reports_present_absent_and_none() -> None:
    assert display_value_mode({"totals": [_metric("1.0000", "1.00")]}) == "present"
    assert display_value_mode({"totals": [_metric("1.0000", carry=False)]}) == "absent"
    assert display_value_mode({"rows": [], "label": "Fictional"}) == "none"


def test_display_value_mode_refuses_a_mixed_body() -> None:
    body = {"a": _metric("1.0000", "1.00"), "b": [_metric("2.0000", carry=False)]}

    with pytest.raises(ValueError, match="mixes display values"):
        display_value_mode(body)


def test_stripping_touches_only_metric_value_objects() -> None:
    label = {"display_value": "Fictional label", "key": "fictional"}
    body = {"label": label, "values": [_metric("1.0000", "1.00")], "pair": (_metric("2", "2.00"),)}

    stripped = without_display_values(body)

    assert stripped == {
        "label": label,
        "values": [_metric("1.0000", carry=False)],
        "pair": (_metric("2", carry=False),),
    }
    assert body["values"] == [_metric("1.0000", "1.00")]


def test_canonical_matches_exactly_or_after_stripping_a_pre_display_input() -> None:
    canonical = {"values": [_metric("75459.6400", "75459.64")]}

    assert canonical_matches(canonical, {"values": [_metric("75459.6400", "75459.64")]})
    assert canonical_matches(canonical, {"values": [_metric("75459.6400", carry=False)]})
    assert not canonical_matches(canonical, {"values": [_metric("75459.6400", "75459.65")]})
    assert not canonical_matches(canonical, {"values": [_metric("75459.6500", carry=False)]})
