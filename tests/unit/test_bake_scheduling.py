# SPDX-License-Identifier: MIT-0
"""The pure GRAPH bake scheduling arithmetic (plan 3.2, A1.1, F6).

Every figure the task-scheduling decision certifies is reproduced from the generated roster,
and the partition is checked against an in-test brute force over all 2^N assignments. Each
refusal runs beside a paired control that the same call accepts.
"""

from __future__ import annotations

import itertools

import pytest

from domain.operations.bake_scheduling import (
    BAKE_ROSTER_EMPTY,
    BAKE_ROSTER_OVER_TRANSACTION_BOUND,
    BakeRosterBoundError,
    BakeTaskLoad,
    LanePlan,
    task_seconds,
    two_lane_partition,
)
from domain.operations.generated_execution_graphs import BAKE_TASKS

pytestmark = [pytest.mark.unit]

ROSTER = (
    "campaigns#1d",
    "campaigns#7d",
    "campaigns#14d",
    "campaigns#30d",
    "retail-health#1d",
    "retail-health#7d",
    "retail-health#14d",
)
"""The stage-1 seven-row roster the 2026-09 task-scheduling decision certified."""
DECISION_WRITE_SECONDS = {"campaigns": 16.6, "retail-health": 1.6}
"""That decision's write seconds; the D2 contract now carries the S0 certificate's values."""
P1_COUNTS = (22, 22, 26, 30, 46, 46, 46)
NO_P1_COUNTS = (26, 26, 30, 34, 46, 46, 46)


def _loads(counts: tuple[int, ...], q: int) -> tuple[BakeTaskLoad, ...]:
    """Return the roster's task loads from pinned post-fan-in query counts at cap `q`."""
    return tuple(
        BakeTaskLoad(
            task,
            task_seconds(count, q, DECISION_WRITE_SECONDS[task.split("#", 1)[0]]),
        )
        for task, count in zip(ROSTER, counts, strict=True)
    )


def _brute_force(tasks: tuple[BakeTaskLoad, ...]) -> tuple[float, tuple[int, ...]]:
    """Return the brute-force optimum over all 2^N assignments.

    That is the minimum makespan and, with the first task on lane 0, the minimum-imbalance
    then lexicographically smallest assignment achieving it.
    """
    tenths = [round(float(task.seconds) * 10) for task in tasks]
    best: tuple[int, int, tuple[int, ...]] | None = None
    for lanes in itertools.product((0, 1), repeat=len(tasks)):
        lane0 = sum(load for load, lane in zip(tenths, lanes, strict=True) if lane == 0)
        lane1 = sum(tenths) - lane0
        if lanes[0] == 1:  # the mirror of an assignment already visited
            continue
        key = (max(lane0, lane1), abs(lane0 - lane1), lanes)
        best = key if best is None or key < best else best
    assert best is not None
    return best[0] / 10, best[2]


def _brute_force_makespan(tasks: tuple[BakeTaskLoad, ...]) -> float:
    return _brute_force(tasks)[0]


def _family_fixed(tasks: tuple[BakeTaskLoad, ...]) -> LanePlan:
    """Return a deliberately wrong reference: one lane per page family (plan forbids it)."""
    first = tasks[0].task_id.split("#", 1)[0]
    lanes = tuple(0 if task.task_id.split("#", 1)[0] == first else 1 for task in tasks)
    tenths = [round(float(task.seconds) * 10) for task in tasks]
    lane0 = sum(load for load, lane in zip(tenths, lanes, strict=True) if lane == 0)
    return LanePlan(tuple(task.task_id for task in tasks), lanes, (lane0, sum(tenths) - lane0))


def test_task_seconds_is_query_waves_plus_writes_plus_the_tail() -> None:
    assert task_seconds(22, 10, 16.6) == 136.6
    assert task_seconds(46, 10, 1.6) == 181.6
    assert task_seconds(0, 10, 1.6) == 31.6


def test_p1_at_q10_reproduces_the_certified_lanes_makespan_and_aggregate() -> None:
    plan = two_lane_partition(_loads(P1_COUNTS, 10))
    assert plan.loads == (546.4, 544.8)
    assert plan.makespan == 546.4
    assert sum(plan.load_tenths) / 10 == 1091.2
    assert plan.lanes == (0, 0, 0, 0, 1, 1, 1)


def test_no_p1_at_q10_makespan_is_576_4() -> None:
    assert two_lane_partition(_loads(NO_P1_COUNTS, 10)).makespan == 576.4


@pytest.mark.parametrize(("q", "makespan"), [(4, 1101.4), (8, 634.8), (16, 426.4), (24, 336.4)])
def test_raw_makespans_across_query_caps(q: int, makespan: float) -> None:
    tasks = _loads(P1_COUNTS, q)
    plan = two_lane_partition(tasks)
    assert plan.makespan == makespan
    assert plan.makespan == _brute_force_makespan(tasks)


@pytest.mark.parametrize(("q", "loads"), [(4, (1069.8, 1101.4)), (24, (304.8, 336.4))])
def test_q4_and_q24_loads_mix_page_families(q: int, loads: tuple[float, float]) -> None:
    plan = two_lane_partition(_loads(P1_COUNTS, q))
    assert tuple(sorted(plan.loads)) == loads
    families = {
        lane: {
            task.split("#", 1)[0]
            for task, owner in zip(ROSTER, plan.lanes, strict=True)
            if owner == lane
        }
        for lane in (0, 1)
    }
    assert families[0] & families[1]  # no lane is one family


def test_control_a_family_fixed_assignment_is_not_what_the_partition_returns() -> None:
    # At no-P1 Q10 the family split happens to be the optimum (576.4), so it cannot tell the
    # two apart; at Q4 it is 1,174.8 against the partition's 1,101.4.
    assert _family_fixed(_loads(NO_P1_COUNTS, 10)).makespan == 576.4
    family = _family_fixed(_loads(P1_COUNTS, 4))
    plan = two_lane_partition(_loads(P1_COUNTS, 4))
    assert family.makespan == 1174.8
    assert (family.lanes, family.makespan) != (plan.lanes, plan.makespan)


def test_n3_fixture_has_exact_lanes_and_brute_force_makespan() -> None:
    tasks = (BakeTaskLoad("a#1", 30.0), BakeTaskLoad("b#1", 20.0), BakeTaskLoad("c#1", 10.0))
    plan = two_lane_partition(tasks)
    assert plan.lanes == (0, 1, 1)
    assert plan.loads == (30.0, 30.0)
    assert (plan.makespan, plan.lanes) == _brute_force(tasks)


def test_n9_fixture_has_exact_lanes_and_brute_force_makespan() -> None:
    seconds = (90.5, 81.0, 72.3, 60.0, 55.5, 41.1, 33.3, 20.0, 12.4)
    tasks = tuple(BakeTaskLoad(f"page#{index}", value) for index, value in enumerate(seconds))
    plan = two_lane_partition(tasks)
    assert (plan.makespan, plan.lanes) == _brute_force(tasks)
    assert plan.lanes == (0, 1, 1, 1, 0, 0, 0, 1, 0)
    assert plan.loads == (232.8, 233.3)
    assert plan.makespan == 233.3


def test_the_ceiling_of_21_tasks_partitions_exactly() -> None:
    tasks = tuple(BakeTaskLoad(f"page#{index}", 10.0) for index in range(21))
    plan = two_lane_partition(tasks)
    assert plan.makespan == 110.0
    assert sorted(plan.loads) == [100.0, 110.0]
    assert BAKE_TASKS["max_tasks"] == 21


def test_22_tasks_are_refused_before_enumeration_and_21_are_accepted() -> None:
    tasks = tuple(BakeTaskLoad(f"page#{index}", 10.0) for index in range(22))
    with pytest.raises(BakeRosterBoundError) as refused:
        two_lane_partition(tasks)
    assert (refused.value.reason, refused.value.observed, refused.value.limit) == (
        BAKE_ROSTER_OVER_TRANSACTION_BOUND,
        22,
        21,
    )
    assert isinstance(refused.value, ValueError)
    # Paired control: one task fewer is the accepted ceiling.
    assert two_lane_partition(tasks[:21]).makespan == 110.0


def test_an_empty_roster_is_refused_and_one_task_is_accepted() -> None:
    with pytest.raises(BakeRosterBoundError) as refused:
        two_lane_partition(())
    assert refused.value.reason == BAKE_ROSTER_EMPTY
    assert two_lane_partition((BakeTaskLoad("page#1", 10.0),)).lanes == (0,)


def test_a_load_with_two_decimal_places_is_refused_and_one_decimal_is_accepted() -> None:
    with pytest.raises(ValueError, match="one decimal place"):
        BakeTaskLoad("page#1", 16.65)
    assert BakeTaskLoad("page#1", 16.6).seconds == 16.6
