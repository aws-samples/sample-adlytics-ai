# SPDX-License-Identifier: MIT-0
"""The one numeric policy for forecasts (design §4d, `numeric` v1).

Callers may rely on: a stored value is a `Decimal` with exactly four places, rounded half to
even, and a zero is stored as `0.0000`, never `-0.0000`; a shown figure is a whole number
rounded half away from zero, applied once after any arithmetic on stored values, never `-0`.
The four places and the check order are the source engine's; the display rule is its whole-
number rule.
"""

from __future__ import annotations

import math
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal

_PLACES = Decimal("0.0001")
_WHOLE = Decimal(1)
_ZERO = Decimal("0.0000")


def stored_value(value: float | Decimal) -> Decimal:
    """Quantize a finite value to four places, half to even; zero is always `0.0000`."""
    if isinstance(value, Decimal):
        number = value
    else:
        if not math.isfinite(value):
            raise ValueError("a stored value must be finite")
        number = Decimal(repr(float(value)))
    if not number.is_finite():
        raise ValueError("a stored value must be finite")
    quantized = number.quantize(_PLACES, rounding=ROUND_HALF_EVEN)
    return _ZERO if quantized.is_zero() else quantized


def stored_text(value: Decimal) -> str:
    """Return a stored value as its decimal string, for example `12.5000`."""
    return f"{stored_value(value):f}"


def display_whole(value: Decimal) -> int:
    """Return the shown whole figure, half away from zero (`ROUND_HALF_UP` on Decimal)."""
    shown = int(value.quantize(_WHOLE, rounding=ROUND_HALF_UP))
    return shown or 0


def display_percent_change(current: Decimal, previous: Decimal) -> int | None:
    """Return the whole percent change from `previous`, or None when `previous` is zero."""
    if previous.is_zero():
        return None
    return display_whole((current - previous) / previous * 100)
