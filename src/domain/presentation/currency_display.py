# SPDX-License-Identifier: MIT-0
"""Two-place display of monetary values, derived once on the server (ADR-084).

A currency value keeps its exact stored value; beside it the server serves a `display_value`
rounded to two places with `ROUND_HALF_UP` (ties away from zero). The rounding is display only
and terminal: nothing sums or re-derives a display value, so stored derived values keep their
own `ROUND_HALF_EVEN`. Every displayed monetary figure, served or written into server text,
comes from `currency_display_value`, so the text and the served value always agree.

The tolerant canonical rule below lets the domain decoders read retained bodies written before
display values existed: a body is either wholly pre-display or wholly current.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Context, Decimal, InvalidOperation
from enum import StrEnum
from typing import Literal, cast

from domain.narrative.shaped_response import UnitKind, Units

__all__ = [
    "CURRENCY_DISPLAY_PLACES",
    "CURRENCY_DISPLAY_ROUNDING",
    "DISPLAY_VALUE_KEY",
    "CurrencyTextStyle",
    "canonical_matches",
    "carries_display_value",
    "currency_display_text",
    "currency_display_value",
    "display_value_mode",
    "metric_display_value",
    "without_display_values",
]

CURRENCY_DISPLAY_PLACES = Decimal("0.01")
CURRENCY_DISPLAY_ROUNDING = ROUND_HALF_UP
_CONTEXT = Context(prec=40)
"""Wide enough for a decimal(38,4) magnitude, where the default 28-digit context raises."""

DISPLAY_VALUE_KEY = "display_value"
_METRIC_VALUE_KEYS = frozenset({"basis", "grain", "period", "units", "value"})


def _quantized(value: Decimal | int | str) -> Decimal:
    try:
        exact = Decimal(value)
    except InvalidOperation as error:
        raise ValueError("a currency display value needs a decimal value.") from error
    if not exact.is_finite():
        raise ValueError("a currency display value needs a finite value.")
    quantized = exact.quantize(
        CURRENCY_DISPLAY_PLACES, rounding=CURRENCY_DISPLAY_ROUNDING, context=_CONTEXT
    )
    # A negative value under half a cent rounds to -0.00, which displays as 0.00.
    return quantized.copy_abs() if quantized.is_zero() else quantized


def currency_display_value(value: Decimal | int | str) -> str:
    """Return the canonical two-place, ungrouped display string, for example ``"-12.30"``."""
    return format(_quantized(value), "f")


def metric_display_value(units: Units, value: Decimal | int | str | None) -> str | None:
    """Return a metric value's served `display_value`: two places for currency, else `None`."""
    if units.kind is not UnitKind.CURRENCY or value is None:
        return None
    return currency_display_value(value)


class CurrencyTextStyle(StrEnum):
    """How server-authored text writes a monetary figure."""

    CODED = "coded"
    """Unsigned unless negative, with the ISO code: ``"1,234.50 USD"`` (facts and movers)."""
    SIGNED_BARE = "signed_bare"
    """Signed on the rounded value, no code: ``"+1,234.50"`` (a currency day move)."""


def currency_display_text(
    value: Decimal | int | str,
    currency_code: str | None,
    style: CurrencyTextStyle = CurrencyTextStyle.CODED,
) -> str:
    """Write a monetary figure for server text, grouped, from the same rounding as the display.

    The sign is decided on the rounded value: a value that rounds to zero reads ``"0.00"``.
    `SIGNED_BARE` writes no code, so it takes `None`; `CODED` refuses a missing code.
    """
    quantized = _quantized(value)
    grouped = format(quantized, ",f")
    if style is CurrencyTextStyle.SIGNED_BARE:
        return f"+{grouped}" if quantized > 0 else grouped
    if not currency_code:
        raise ValueError("coded currency text needs a currency code.")
    return f"{grouped} {currency_code}"


def carries_display_value(node: object) -> bool:
    """Return whether a decoded object carries the `display_value` key."""
    return isinstance(node, dict) and DISPLAY_VALUE_KEY in cast("dict[str, object]", node)


def _is_metric_value(node: object) -> bool:
    """Return whether `node` is a metric-value object: the five keys, optionally display_value."""
    if not isinstance(node, dict):
        return False
    keys = frozenset(cast("dict[str, object]", node))
    return keys - {DISPLAY_VALUE_KEY} == _METRIC_VALUE_KEYS


def _metric_values(payload: object) -> list[dict[str, object]]:
    found: list[dict[str, object]] = []
    pending: list[object] = [payload]
    while pending:
        node = pending.pop()
        if _is_metric_value(node):
            found.append(cast("dict[str, object]", node))
        elif isinstance(node, dict):
            pending.extend(cast("dict[str, object]", node).values())
        elif isinstance(node, list | tuple):
            pending.extend(cast("list[object] | tuple[object, ...]", node))
    return found


def display_value_mode(payload: object) -> Literal["present", "absent", "none"]:
    """Say whether every metric value carries a display value, none does, or there are none.

    Raises `ValueError` when some carry it and some do not: only a writer bug produces that.
    """
    values = _metric_values(payload)
    if not values:
        return "none"
    carrying = sum(DISPLAY_VALUE_KEY in value for value in values)
    if carrying == len(values):
        return "present"
    if carrying == 0:
        return "absent"
    raise ValueError("a payload mixes display values.")


def without_display_values(payload: object) -> object:
    """Return a deep copy with `display_value` removed from metric-value objects only."""
    if _is_metric_value(payload):
        node = cast("dict[str, object]", payload)
        return {
            key: without_display_values(value)
            for key, value in node.items()
            if key != DISPLAY_VALUE_KEY
        }
    if isinstance(payload, dict):
        node = cast("dict[str, object]", payload)
        return {key: without_display_values(value) for key, value in node.items()}
    if isinstance(payload, list):
        return [without_display_values(item) for item in cast("list[object]", payload)]
    if isinstance(payload, tuple):
        return tuple(without_display_values(item) for item in cast("tuple[object, ...]", payload))
    return payload


def canonical_matches(canonical: object, item: object) -> bool:
    """Compare a re-encoded payload with its input, accepting a wholly pre-display input."""
    if display_value_mode(item) == "absent":
        return without_display_values(canonical) == item
    return canonical == item
