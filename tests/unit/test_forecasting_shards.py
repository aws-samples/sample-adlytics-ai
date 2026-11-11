# SPDX-License-Identifier: MIT-0
"""Shard planning: a wrong cut would forecast an ASIN twice or never, or exceed 100 shards."""

from __future__ import annotations

import pytest

from domain.forecasting.shards import RosterTooLargeError, ShardPlanRequest, plan_shards

pytestmark = pytest.mark.unit


def _roster(count: int) -> tuple[str, ...]:
    return tuple(f"amazon_asin:b{n:09d}" for n in range(count))


@pytest.mark.parametrize(
    ("count", "sizes"),
    [
        (1999, [1999]),
        (2000, [2000]),
        (2001, [2000, 1]),
        (50000, [2000] * 25),
    ],
)
def test_block_boundaries(count: int, sizes: list[int]) -> None:
    roster = _roster(count)
    plan = plan_shards(ShardPlanRequest(roster, 2000))
    assert [shard.asin_count for shard in plan.shards] == sizes
    assert [shard.index for shard in plan.shards] == list(range(len(sizes)))
    assert plan.shards[0].first_canonical_asin == roster[0]
    assert plan.shards[-1].last_canonical_asin == roster[-1]


def test_the_hundredth_shard_is_allowed_and_the_hundred_first_refused() -> None:
    assert len(plan_shards(ShardPlanRequest(_roster(50000), 500)).shards) == 100
    with pytest.raises(RosterTooLargeError):
        plan_shards(ShardPlanRequest(_roster(50001), 500))


def test_an_unsorted_roster_is_refused() -> None:
    with pytest.raises(ValueError, match="sorted and distinct"):
        ShardPlanRequest(("amazon_asin:b000000002", "amazon_asin:b000000001"), 2000)
