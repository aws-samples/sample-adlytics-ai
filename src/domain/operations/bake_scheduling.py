# SPDX-License-Identifier: MIT-0
"""Pure GRAPH bake scheduling arithmetic (plan 3.2, A1.1, F6): no clock, no I/O.

- `task_seconds` is one roster task's budgeted duration from its pinned post-fan-in query count:
  `ceil(query_count / q) * per_query_seconds + write_seconds + tail_seconds`.
- `two_lane_partition` is the exact minimum-makespan split of N roster tasks over the two
  fixed bake lanes. It never assigns by page family. The first declared task is pinned to lane 0,
  so the 2^(N-1) canonical partitions are visited once each, in Gray-code order with an O(1)
  lane-load update per step. Loads are summed in integer tenths of a second (every input carries
  at most one decimal place), so sums are exact and ties are decidable. Ties break by imbalance,
  then by the declared-roster lane-bit order (the lexicographically smallest assignment).
- N is bounded by `BAKE_TASKS["max_tasks"]` (21), the finalization transaction bound
  `13 + 4N <= max_transaction_items`; above it, and for an empty roster, `BakeRosterBoundError`
  is raised before anything is enumerated. At the ceiling that is 2^20 constant-time steps.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from domain.operations.generated_execution_graphs import BAKE_TASKS

__all__ = [
    "BAKE_ROSTER_EMPTY",
    "BAKE_ROSTER_OVER_TRANSACTION_BOUND",
    "PRECOMPUTE_READY_SECONDS",
    "BakeRosterBoundError",
    "BakeTaskLoad",
    "LanePlan",
    "task_seconds",
    "two_lane_partition",
]

BAKE_ROSTER_OVER_TRANSACTION_BOUND: Final = "bake_roster_over_transaction_bound"
BAKE_ROSTER_EMPTY: Final = "bake_roster_empty"
PRECOMPUTE_READY_SECONDS: Final = (
    int(BAKE_TASKS["worker_schedule_seconds"])
    + int(BAKE_TASKS["worker_pass_seconds"])
    + int(BAKE_TASKS["precompute_claim_lease_seconds"])
)
"""The designed longest healthy gap from precompute readiness to its contribution (180 s).

An armed precompute waits for one graph-dispatcher schedule, one dispatcher pass, and one
precompute claim lease. The armed finalizer's redelivery and both DB26 bake thresholds derive
from it, so a changed schedule, pass or lease moves all three together.
"""
_TENTHS: Final = 10


class BakeRosterBoundError(ValueError):
    """The roster is empty or longer than the finalization transaction allows."""

    def __init__(self, reason: str, observed: int, limit: int) -> None:
        super().__init__(f"{reason}: {observed} roster tasks against a limit of {limit}.")
        self.reason = reason
        self.observed = observed
        self.limit = limit


def _tenths(seconds: float | Decimal) -> int:
    """Return `seconds` in exact integer tenths, refusing a value with finer precision."""
    if isinstance(seconds, bool) or seconds < 0:
        raise ValueError("a bake task load is a non-negative number of seconds.")
    scaled = Decimal(str(seconds)) * _TENTHS
    if scaled != scaled.to_integral_value():
        raise ValueError("a bake task load carries at most one decimal place.")
    return int(scaled)


@dataclass(frozen=True, slots=True)
class BakeTaskLoad:
    """One roster task and its budgeted seconds, at most one decimal place."""

    task_id: str
    seconds: float | Decimal

    def __post_init__(self) -> None:
        if not self.task_id.strip():
            raise ValueError("a bake task needs a nonblank id.")
        _tenths(self.seconds)


@dataclass(frozen=True, slots=True)
class LanePlan:
    """Each task's lane (0 or 1), in roster order, and both lane loads in integer tenths."""

    task_ids: tuple[str, ...]
    lanes: tuple[int, ...]
    load_tenths: tuple[int, int]

    @property
    def loads(self) -> tuple[float, float]:
        """Return both lane loads in seconds."""
        return (self.load_tenths[0] / _TENTHS, self.load_tenths[1] / _TENTHS)

    @property
    def makespan(self) -> float:
        """Return the longer lane's load in seconds."""
        return max(self.load_tenths) / _TENTHS

    def lane_of(self, task_id: str) -> int:
        """Return the lane one roster task runs on."""
        return self.lanes[self.task_ids.index(task_id)]


def task_seconds(query_count: int, q: int, write_seconds: float | Decimal) -> float:
    """Return one task's budgeted seconds: query waves, bounded writes, and the tail."""
    if query_count < 0 or q < 1:
        raise ValueError("a query count is non-negative and the query cap positive.")
    waves = math.ceil(query_count / q) * int(BAKE_TASKS["per_query_seconds"])
    tenths = (waves + int(BAKE_TASKS["tail_seconds"])) * _TENTHS + _tenths(write_seconds)
    return tenths / _TENTHS


def two_lane_partition(tasks: tuple[BakeTaskLoad, ...]) -> LanePlan:
    """Return the exact minimum-makespan two-lane assignment of the roster tasks."""
    limit = int(BAKE_TASKS["max_tasks"])
    count = len(tasks)
    if count > limit:
        raise BakeRosterBoundError(BAKE_ROSTER_OVER_TRANSACTION_BOUND, count, limit)
    if count == 0:
        raise BakeRosterBoundError(BAKE_ROSTER_EMPTY, count, limit)
    loads = [_tenths(task.seconds) for task in tasks]
    # Mask bit j holds task (count - 1 - j)'s lane, so a smaller mask is a lexicographically
    # smaller assignment in roster order; task 0 is the implicit most significant bit, lane 0.
    lane0, lane1, mask = sum(loads), 0, 0
    best_key = (max(lane0, lane1), abs(lane0 - lane1), mask)
    best_loads = (lane0, lane1)
    for step in range(1, 1 << (count - 1)):
        bit = (step & -step).bit_length() - 1
        load = loads[count - 1 - bit]
        mask ^= 1 << bit
        if mask & (1 << bit):
            lane0, lane1 = lane0 - load, lane1 + load
        else:
            lane0, lane1 = lane0 + load, lane1 - load
        key = (max(lane0, lane1), abs(lane0 - lane1), mask)
        if key < best_key:
            best_key, best_loads = key, (lane0, lane1)
    best_mask = best_key[2]
    lanes = tuple(
        0 if index == 0 else (best_mask >> (count - 1 - index)) & 1 for index in range(count)
    )
    return LanePlan(tuple(task.task_id for task in tasks), lanes, best_loads)
