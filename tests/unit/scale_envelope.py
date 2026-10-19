# SPDX-License-Identifier: MIT-0
"""Assert a size bound at the governed maximum, never at a smaller hand-picked size.

P3: a publication record of 33,762 bytes against a 32,000-byte item bound was found only at
real-shaped volume, because the tests measured a size someone picked rather than the largest the
system is declared to carry. `assert_within_bound` takes the declared maximum (for example
`BULK_CAMPAIGN_COUNT`) and measures there, exactly once, so a bound test cannot pass by measuring
a smaller estate. The failure message names the unit and the grain (meta-testing-strategy,
Assertions), because a correct figure at the wrong grain is the failure this product must not
have.
"""

from __future__ import annotations

from collections.abc import Callable


def assert_within_bound(  # noqa: PLR0913 -- keyword-only: each call names its unit and grain
    *,
    measure: Callable[[int], int],
    declared_maximum: int,
    bound: int,
    unit: str,
    grain: str,
) -> int:
    """Measure at exactly `declared_maximum`, assert the result is within `bound`, and return it.

    Raises `AssertionError` naming the measurement, the bound, the unit and the grain when the
    measurement exceeds the bound, and `ValueError` for a non-positive maximum or a measure that
    does not return an integer.
    """
    if isinstance(declared_maximum, bool) or declared_maximum <= 0:
        raise ValueError(f"declared_maximum must be a positive integer, got {declared_maximum!r}")
    measured = measure(declared_maximum)
    if isinstance(measured, bool) or not isinstance(measured, int):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise ValueError(f"measure must return an integer {unit} count, got {measured!r}")
    if measured > bound:
        raise AssertionError(
            f"{measured:,} {unit} per {grain} at the declared maximum of {declared_maximum:,} "
            f"exceeds the bound of {bound:,} {unit} per {grain}"
        )
    return measured
