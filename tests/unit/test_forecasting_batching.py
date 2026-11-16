# SPDX-License-Identifier: MIT-0
"""Cross-learning batch membership (design §4c): unstable batches change forecasts run to run."""

from __future__ import annotations

import pytest

from domain.forecasting.batching import (
    Origin,
    SeriesKeyRequest,
    SeriesSortKey,
    plan_batches,
    series_sort_key,
)

pytestmark = pytest.mark.unit


def _key(
    asin: str, target: str = "units_ordered", origin: Origin = Origin.FORECAST
) -> SeriesSortKey:
    return series_sort_key(
        SeriesKeyRequest(target, origin, "amazon_us", "MANUFACTURING", "RETAIL", asin)
    )


def _asins(keys: list[SeriesSortKey], batches: tuple[tuple[int, ...], ...]) -> list[list[str]]:
    return [[keys[i].canonical_asin for i in batch] for batch in batches]


def test_batches_are_consecutive_runs_of_the_sorted_keys_whatever_the_input_order() -> None:
    keys = [_key(f"amazon_asin:b0000000{n:02d}") for n in (4, 1, 3, 0, 2)]
    batches = plan_batches(keys, 2).batches
    assert _asins(keys, batches) == [
        ["amazon_asin:b000000000", "amazon_asin:b000000001"],
        ["amazon_asin:b000000002", "amazon_asin:b000000003"],
        ["amazon_asin:b000000004"],
    ]
    # Control: the reversed input gives the same membership.
    reversed_keys = list(reversed(keys))
    assert _asins(reversed_keys, plan_batches(reversed_keys, 2).batches) == _asins(keys, batches)


def test_a_batch_never_spans_a_target_or_an_origin() -> None:
    keys = [
        _key("amazon_asin:b000000001", origin=Origin.BACKTEST),
        _key("amazon_asin:b000000002", origin=Origin.FORECAST),
        _key("amazon_asin:b000000003", target="units_shipped", origin=Origin.FORECAST),
    ]
    batches = plan_batches(keys, 256).batches
    assert len(batches) == 3
    assert {len(batch) for batch in batches} == {1}


def test_duplicate_keys_and_a_zero_batch_size_are_refused() -> None:
    key = _key("amazon_asin:b000000001")
    with pytest.raises(ValueError, match="distinct"):
        plan_batches([key, key], 2)
    with pytest.raises(ValueError, match="at least 1"):
        plan_batches([key], 0)
