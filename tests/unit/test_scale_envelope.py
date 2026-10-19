# SPDX-License-Identifier: MIT-0
"""The scale-envelope helper measures at the declared maximum and names unit and grain."""

from __future__ import annotations

import pytest
from scale_envelope import assert_within_bound

pytestmark = pytest.mark.unit


def test_a_measure_within_its_bound_returns_the_measurement_taken_at_the_maximum() -> None:
    sizes: list[int] = []

    def measure(size: int) -> int:
        sizes.append(size)
        return size * 3

    measured = assert_within_bound(
        measure=measure, declared_maximum=1_000, bound=3_000, unit="bytes", grain="record"
    )

    assert (measured, sizes) == (3_000, [1_000])


def test_a_measure_over_its_bound_fails_naming_unit_and_grain() -> None:
    with pytest.raises(AssertionError) as raised:
        assert_within_bound(
            measure=lambda size: size * 33,
            declared_maximum=1_000,
            bound=32_000,
            unit="bytes",
            grain="publication record",
        )

    assert str(raised.value) == (
        "33,000 bytes per publication record at the declared maximum of 1,000 "
        "exceeds the bound of 32,000 bytes per publication record"
    )


@pytest.mark.parametrize("maximum", [0, -1, True])
def test_a_non_positive_maximum_is_refused(maximum: int) -> None:
    with pytest.raises(ValueError, match="declared_maximum must be a positive integer"):
        assert_within_bound(
            measure=lambda size: size, declared_maximum=maximum, bound=1, unit="bytes", grain="x"
        )


def test_a_measure_that_does_not_return_an_integer_is_refused() -> None:
    def measure(size: int) -> int:
        return float(size)  # type: ignore[return-value]

    with pytest.raises(ValueError, match="measure must return an integer bytes count"):
        assert_within_bound(
            measure=measure, declared_maximum=1, bound=10, unit="bytes", grain="record"
        )
