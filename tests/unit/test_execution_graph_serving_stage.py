# SPDX-License-Identifier: MIT-0
"""ADR-162: a serving election or drain larger than one pass still completes, and exactly.

On dev on 2026-11-13 the promoter read all 3,502 waiting candidates one at a time in a 60 s
pass (about 101.5 ms each), so every pass was killed before its commit and serving stalled; a
drain aliased one run a pass. These tests drive the real promoter and store over the
transactional fake with a metered Lambda clock charged per store call at about dev's rates, so
a path that reads past its budget is killed here as it was live. Each staged result is compared
with the same world elected by one unbounded pass: a lost, duplicated or reordered candidate
would change the elected roster or a digest. Fictional data only.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

import pytest
from execution_graph_dispatch_support import NOW, write_int
from execution_graph_release_support import release_store
from execution_graph_support import FakeDynamo
from fixed_member_byte_fixture_support import complete_direct_winner, grant_and_publish
from test_execution_graph_pass_budget import LambdaKilledError, Metered

from adapters.jobs.execution_graph.fence_settler import due_shards
from adapters.jobs.execution_graph.instantiate import ingestion_node_plan
from adapters.jobs.execution_graph.promoter import ArmCounts, PromoterContext, coalesce_serving
from adapters.outbound.dynamodb.execution_graph import due_shard
from domain.operations.execution_graph_release import INGESTION_RELEASE_DOMAIN
from ports.outbound.execution_graph_release import ServingElectionOutcome

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-162")]

HEAD = (f"SERVING_DOMAIN#{INGESTION_RELEASE_DOMAIN}", "HEAD")
LARGE = 1030
"""Twice the old 512 roster cap and about five passes of reads at dev's measured rates."""


def _published(count: int, tag: str = "s") -> tuple[FakeDynamo, tuple[str, ...]]:
    fake = FakeDynamo()
    return fake, tuple(grant_and_publish(fake, tag, index) for index in range(count))


@pytest.fixture(scope="module")
def large_world() -> tuple[FakeDynamo, tuple[str, ...]]:
    """1,030 granted, published, eligible runs on one IDLE head; built once (about 50 s)."""
    return _published(LARGE)


def _coalesce(
    fake: FakeDynamo, clock: Callable[[], int], request: str = "stage-test"
) -> tuple[ArmCounts, tuple[Any, ...], list[BaseException]]:
    failures: list[BaseException] = []
    context = PromoterContext(
        release_store(fake), ingestion_node_plan, lambda: NOW, on_failure=failures.append
    )
    counts = ArmCounts()
    results = coalesce_serving(context, due_shards(), counts, clock, request)
    return counts, results, failures


def _ample(fake: FakeDynamo) -> FakeDynamo:
    """Elect or drain a copy of the world with unbounded passes: the reference."""
    reference = copy.deepcopy(fake)
    for _ in range(4):
        _counts, _results, failures = _coalesce(reference, lambda: 60_000)
        assert failures == []
    return reference


def _metered(fake: FakeDynamo, call_ms: int = 10, query_ms: int = 15) -> Metered:
    """Dev's rates: about 10 ms a strong GetItem, so a candidate's 10 reads cost about 100 ms."""
    return Metered(fake, due_query_ms=query_ms, call_ms=call_ms)


def _pages(fake: FakeDynamo) -> list[tuple[str, str]]:
    return sorted(key for key in fake.items if key[1].startswith("ELECTION#"))


def _head_state(fake: FakeDynamo) -> str:
    return str(fake.items[HEAD]["state"]["S"])


def _until_active(metered: Metered, limit: int = 12) -> list[ArmCounts]:
    passes: list[ArmCounts] = []
    while _head_state(metered.fake) != "ACTIVE":
        assert len(passes) < limit, "the staged election did not converge"
        meter = metered.invocation()
        counts, _results, failures = _coalesce(metered.fake, meter.clock)
        assert failures == []
        assert meter.remaining > 0  # never killed: every pass stopped inside its budget
        passes.append(counts)
    metered.idle()
    return passes


def test_a_roster_twice_the_old_cap_is_staged_over_passes_and_elects_the_reference_exactly(
    large_world: tuple[FakeDynamo, tuple[str, ...]],
) -> None:
    world, runs = large_world
    reference = _ample(world)
    assert reference.items[HEAD]["winner_run_id"]["S"] == runs[-1]
    staged = copy.deepcopy(world)

    passes = _until_active(_metered(staged))

    assert len(passes) >= 3  # the roster is larger than one pass
    assert all(item.serving_staged > 0 for item in passes[:-1])
    assert sum(item.serving_staged for item in passes) < LARGE  # the last pass read the rest
    assert passes[-1].serving_unstaged == 0
    assert _pages(staged) == []  # the committed election deleted its pages
    # Same winner, roster, cohort and candidate digests, and every other item, byte for byte.
    assert staged.items == reference.items
    elected = reference.items[HEAD]["elected_run_ids"]["L"]
    assert len(elected) == LARGE


def test_control_a_small_roster_under_the_metered_clock_elects_in_one_pass_writing_no_page() -> (
    None
):
    world, _runs = _published(3)
    reference = _ample(world)
    metered = _metered(copy.deepcopy(world))
    writes_before = len(metered.fake.transactions)

    passes = _until_active(metered)

    assert len(passes) == 1
    assert (passes[0].serving_staged, passes[0].serving_unstaged) == (0, 0)
    assert len(metered.fake.transactions) == writes_before + 1  # the activation alone
    assert metered.fake.items == reference.items


SMALL = 24
"""At 250 ms a call a pass reads about 5 candidates, so 24 take about five passes."""


def _small_metered(fake: FakeDynamo) -> Metered:
    return _metered(fake, call_ms=250, query_ms=50)


def test_a_pass_killed_at_any_store_call_converges_to_the_reference_election() -> None:
    world, _runs = _published(SMALL)
    reference = _ample(world)
    probe = _small_metered(copy.deepcopy(world))
    calls = [_until_one_pass(probe), _until_one_pass(probe)]
    assert _head_state(probe.fake) == "IDLE" and _pages(probe.fake)  # two passes still staging

    # The first pass (no page yet) and the second (extending a live stage), each killed in turn
    # before every store call it makes.
    for killed_pass, count in enumerate(calls):
        for kill_at in range(1, count + 1):
            fake = copy.deepcopy(world)
            metered = _small_metered(fake)
            for _ in range(killed_pass):
                _until_one_pass(metered)
            meter = metered.invocation(kill_at=kill_at)
            with pytest.raises(LambdaKilledError):
                _coalesce(fake, meter.clock)
            _until_active(metered)
            assert fake.items == reference.items, f"pass {killed_pass + 1}, call {kill_at}"


def _until_one_pass(metered: Metered) -> int:
    meter = metered.invocation()
    _counts, _results, failures = _coalesce(metered.fake, meter.clock)
    assert failures == []
    return meter.calls


def test_a_publication_arriving_during_staging_joins_the_roster_and_wins() -> None:
    world, _runs = _published(SMALL)
    metered = _small_metered(world)
    _until_one_pass(metered)
    assert _pages(world)
    metered.idle()
    late = grant_and_publish(world, "late", 0)

    _until_active(metered)

    head = world.items[HEAD]
    assert head["winner_run_id"]["S"] == late
    elected = [value["S"] for value in head["elected_run_ids"]["L"]]
    assert len(elected) == SMALL + 1 and late in elected and len(set(elected)) == len(elected)
    assert _pages(world) == []


def test_a_publication_settling_during_slow_reads_is_read_again_and_that_pass_commits() -> None:
    """Without the second read, every slow pass loses its frontier race to a new settlement.

    On dev about 22 publications settle a minute, so a pass that reads for 20 s almost always
    sees one; its commit's frontier ConditionCheck then fails and the election never commits.
    """
    world, _runs = _published(3)
    remaining = {"ms": 60_000}
    late: list[str] = []
    original = world.get_item

    def get_item(**kwargs: Any) -> Any:
        if str(kwargs["Key"]["pk"]["S"]).startswith("SERVING_PUBLICATION#") and not late:
            world.get_item = original  # type: ignore[method-assign]
            late.append(grant_and_publish(world, "late", 0))
            remaining["ms"] -= 6_000  # the reads outlast one fresh iteration (5,246 ms)
        return original(**kwargs)

    world.get_item = get_item  # type: ignore[method-assign]

    counts, _results, failures = _coalesce(world, lambda: remaining["ms"])

    assert failures == []
    head = world.items[HEAD]
    assert head["state"]["S"] == "ACTIVE" and head["winner_run_id"]["S"] == late[0]
    assert len(head["elected_run_ids"]["L"]) == 4
    assert counts.serving_unstaged == 0
    assert _pages(world) == []


def test_a_head_version_change_makes_the_stage_dead_and_its_pages_are_deleted() -> None:
    world, runs = _published(SMALL)
    metered = _small_metered(world)
    _until_one_pass(metered)
    dead = _pages(world)
    assert dead
    metered.idle()
    # A transition of this promoter's own moves the head version (simulated here).
    version = int(world.items[HEAD]["version"]["N"])
    world.items[HEAD]["version"] = write_int(version + 1)

    passes = _until_active(metered)

    assert not set(dead) & set(world.items)
    assert passes[0].serving_staged > 0  # the dead stage's candidates were read again
    assert world.items[HEAD]["winner_run_id"]["S"] == runs[-1]
    assert len(world.items[HEAD]["elected_run_ids"]["L"]) == SMALL


def _draining(world: FakeDynamo, runs: tuple[str, ...]) -> FakeDynamo:
    fake = _ample(world)
    assert fake.items[HEAD]["winner_run_id"]["S"] == runs[-1]
    complete_direct_winner(fake, runs[-1])
    assert _head_state(fake) == "BOUND_DRAINING"
    return fake


def _coalesce_runs(fake: FakeDynamo) -> set[str]:
    cohort = str(fake.items[HEAD]["cohort_revision"]["S"])
    item = fake.items.get((due_shard(INGESTION_RELEASE_DOMAIN), f"COALESCE#{cohort}"))
    return set() if item is None else {value["S"] for value in item["run_ids"]["L"]}


def test_a_cohort_of_1029_aliases_drains_several_aliases_a_pass_reading_only_those_runs(
    large_world: tuple[FakeDynamo, tuple[str, ...]],
) -> None:
    world, runs = large_world
    fake = _draining(world, runs)
    metered = _metered(fake)
    per_pass: list[int] = []
    first_read: set[str] = set()
    first_aliased: set[str] = set()

    while _head_state(fake) == "BOUND_DRAINING" and len(per_pass) < 12:
        before = _coalesce_runs(fake)
        meter = metered.invocation()
        counts, results, failures = _coalesce(fake, meter.clock)
        assert failures == [] and meter.remaining > 0
        per_pass.append(counts.coalesced)
        if len(per_pass) == 1:
            first_aliased = before - _coalesce_runs(fake)
            first_read = {pk.removeprefix("RUN#") for pk in meter.runs_read} & set(runs)
            assert results[0].outcome is ServingElectionOutcome.DRAIN_INCOMPLETE
    metered.idle()

    assert _head_state(fake) == "IDLE"  # every alias and the final one drained
    assert sum(per_pass) == LARGE - 1
    assert len(per_pass) >= 3 and all(count >= 2 for count in per_pass)
    assert first_read == first_aliased  # no read of the cohort it did not alias
    assert _coalesce_runs(fake) == set()
