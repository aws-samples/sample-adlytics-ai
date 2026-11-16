# SPDX-License-Identifier: MIT-0
"""Cut the sorted roster into shards of at most `shard_max_asins` (design §3c, §4b)."""

from __future__ import annotations

from dataclasses import dataclass

from domain.forecasting.policy import MAX_SHARDS


class RosterTooLargeError(ValueError):
    """More than 100 shards: `failed(input_build:ROSTER_TOO_LARGE)`."""

    code = "ROSTER_TOO_LARGE"


@dataclass(frozen=True)
class ShardPlanRequest:
    """The roster's canonical ASINs and the shard size."""

    canonical_asins: tuple[str, ...]
    shard_max_asins: int

    def __post_init__(self) -> None:
        if self.shard_max_asins < 1:
            raise ValueError("shard_max_asins must be at least 1")
        if list(self.canonical_asins) != sorted(set(self.canonical_asins)):
            raise ValueError("the roster must be sorted and distinct")


@dataclass(frozen=True)
class Shard:
    """One shard: its index, size and first and last canonical ASIN."""

    index: int
    asin_count: int
    first_canonical_asin: str
    last_canonical_asin: str


@dataclass(frozen=True)
class ShardPlan:
    """Contiguous shards from index 0 whose counts sum to the roster size."""

    shards: tuple[Shard, ...]


def plan_shards(request: ShardPlanRequest) -> ShardPlan:
    """Shard `k` holds roster positions `k * size` to `(k + 1) * size - 1`."""
    roster = request.canonical_asins
    size = request.shard_max_asins
    shards = tuple(
        Shard(
            index=start // size,
            asin_count=len(roster[start : start + size]),
            first_canonical_asin=roster[start],
            last_canonical_asin=roster[min(start + size, len(roster)) - 1],
        )
        for start in range(0, len(roster), size)
    )
    if len(shards) > MAX_SHARDS:
        raise RosterTooLargeError(f"{len(roster)} ASINs need {len(shards)} shards (max 100)")
    return ShardPlan(shards)
