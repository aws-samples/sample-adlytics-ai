# SPDX-License-Identifier: MIT-0
"""ADR-162: each promoter and release pass stops on its invocation's remaining time.

A pass that runs into its Lambda timeout loses everything it held in memory. On dev on
2026-11-13 the promoter collected every units `ARM#` item before its one commit, so with 3,455
open runs every pass was killed at 60 s and no write unit was armed for five hours: no Vendor
day past the first 100 could be served. These tests drive the real adapters over the
transactional fake with a metered Lambda clock that charges each store call and kills the
invocation at zero, so a pass that ignores its budget fails here the way it failed live.
Fictional data only.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, cast

import pytest
from execution_graph_dispatch_support import (
    NOW,
    now_text,
    seed_run,
    set_mode,
    store,
    write_bool,
    write_int,
    write_text,
)
from execution_graph_release_support import (
    context,
    granted_run,
    release_store,
    reserve_winner,
    seed_granting_run,
    settle_publish,
)
from execution_graph_support import FakeDynamo
from unit_batch_support import node_of, unit_run

from adapters.jobs.execution_graph.budget import (
    COMPLETE_RESERVE_MS,
    ITEM_RESERVE_MS,
    PROMOTE_RESERVE_MS,
    UNITS_COLLECT_RESERVE_MS,
    PassBudget,
)
from adapters.jobs.execution_graph.fence_settler import due_shards
from adapters.jobs.execution_graph.instantiate import ingestion_node_plan
from adapters.jobs.execution_graph.promoter import (
    ArmCounts,
    PromoterContext,
    arm_pass,
    promote_shard,
)
from adapters.jobs.execution_graph.release import (
    ReleaseCounts,
    ReleaseOutcome,
    contribute,
    issue_grant,
    release_pass,
    release_run,
)
from adapters.outbound.dynamodb.execution_graph import due_shard, receipt_item
from domain.operations.execution_graph import AuthorityMode
from domain.operations.execution_graph_dispatch import FenceReason, NodeState
from domain.operations.execution_graph_release import (
    INGESTION_RELEASE_DOMAIN,
    CohortContribution,
    CohortMember,
    CohortOutcome,
    reservation_set,
    serving_generation,
)
from domain.operations.generated_execution_graphs import BAKE_TASKS
from domain.shared_kernel.errors import InvariantError
from ports.outbound.execution_graph import ReceiptRecord
from ports.outbound.execution_graph_dispatch import FenceRunRequest

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-162")]

TABLE_KEY = "orders"
DUE_QUERY_MS = 100
"""What the promoter meter charges a `DUE#` shard query: cheap, as on dev (16 RCU a shard)."""
CALL_MS = 1_000
"""What the promoter meter charges every other store call: a per-run read or a transaction."""


class LambdaKilledError(BaseException):
    """The runtime ended the invocation. Not an `Exception`, so no item isolation absorbs it."""


@dataclass
class Meter:
    """One invocation's clock: 60 s, less each store call's charge; zero is the kill."""

    remaining: int = 60_000
    calls: int = 0
    kill_at: int | None = None
    """End the invocation just before this store call (1-based), whatever time remains."""
    runs_read: list[str] = field(default_factory=list[str])

    def clock(self) -> int:
        return self.remaining

    def charge(self, cost: int) -> None:
        self.calls += 1
        if self.kill_at is not None and self.calls >= self.kill_at:
            raise LambdaKilledError
        self.remaining -= cost
        if self.remaining <= 0:
            raise LambdaKilledError


@dataclass
class Metered:
    """The fake table with every client call charged to the current invocation's meter."""

    fake: FakeDynamo
    due_query_ms: int = DUE_QUERY_MS
    call_ms: int = CALL_MS
    meter: Meter = field(default_factory=Meter)

    def __post_init__(self) -> None:
        for name in ("get_item", "query", "transact_write_items", "put_item", "update_item"):
            setattr(self.fake, name, self._charged(getattr(self.fake, name), name))

    def _charged(self, call: Callable[..., Any], name: str) -> Callable[..., Any]:
        def charged(**kwargs: Any) -> Any:
            pk = ""
            if name == "query":
                pk = str(kwargs["ExpressionAttributeValues"][":pk"]["S"])
            if name == "get_item":
                self.meter.runs_read.append(str(kwargs["Key"]["pk"]["S"]))
            self.meter.charge(self.due_query_ms if pk.startswith("DUE#") else self.call_ms)
            return call(**kwargs)

        return charged

    def invocation(self, **meter: Any) -> Meter:
        """Start the next invocation with a fresh 60 s clock."""
        self.meter = Meter(**meter)
        return self.meter

    def idle(self) -> None:
        """Stop charging: the test's own reads after its passes are not an invocation's."""
        self.meter = Meter(remaining=10**12)


def _clock(index: int) -> str:
    return (NOW - timedelta(hours=10) + timedelta(minutes=index)).isoformat()


def _units_backlog(fake: FakeDynamo, count: int, *keys: str) -> list[str]:
    """Seed `count` OPEN runs with PENDING units, oldest first by index.

    Each `ARM#` carries its run's root clock, as every production writer stamps it.
    """
    runs: list[str] = []
    for index in range(1, count + 1):
        run = unit_run(fake, index, *keys, clock=_clock(index))
        (arm,) = [key for key in fake.items if key[1] == f"ARM#{run}"]
        fake.items[arm]["root_clock_started_at"] = write_text(_clock(index))
        runs.append(run)
    return runs


def _arm(fake: FakeDynamo, clock: Callable[[], int]) -> ArmCounts:
    return arm_pass(
        PromoterContext(release_store(fake), ingestion_node_plan, lambda: NOW),
        due_shards(),
        clock,
        "budget-test",
    )


def _settle_locks(fake: FakeDynamo) -> None:
    """Free every table lock, as the TableWriter finishing each armed batch between passes does."""
    for key, item in fake.items.items():
        if key[0].startswith("LOCK#TABLE#"):
            item["open_members"] = {"N": "0"}


def _rows(fake: FakeDynamo) -> dict[str, list[tuple[str, str]]]:
    """Every unit batch row's members as (run, node), by batch."""
    rows: dict[str, list[tuple[str, str]]] = {}
    for pk, sk in sorted(fake.items):
        if not sk.startswith("OUTBOX#"):
            continue
        row = store(fake).read_outbox(pk, sk.removeprefix("OUTBOX#"))
        if row is not None and row.members:
            rows[sk] = [(ref.run_id, ref.node_id) for ref in row.members]
    return rows


def _arms_left(fake: FakeDynamo) -> int:
    return sum(1 for _pk, sk in fake.items if sk.startswith("ARM#"))


# --- The promoter: units arming --------------------------------------------------------------


def test_a_units_backlog_larger_than_one_pass_drains_oldest_first_over_two_passes() -> None:
    """Live shape: 2 strong reads per run and the only commit after the loop (rca.md §2).

    Per pass the meter charges 64 shard queries (6.4 s), then 2 s per run collected. With 30
    runs the old loop needed 60 s of reads and was killed with zero commits; the bounded pass
    collects while 25 s remain (runs 1-15), grants them in one transaction and returns.
    """
    fake = FakeDynamo()
    runs = _units_backlog(fake, 30, TABLE_KEY)
    metered = Metered(fake)

    first_meter = metered.invocation()
    first = _arm(fake, first_meter.clock)
    first_left = first_meter.remaining
    first_rows = _rows(fake)
    _settle_locks(fake)
    second_meter = metered.invocation()
    second = _arm(fake, second_meter.clock)
    second_left = second_meter.remaining
    metered.idle()

    assert (first.batches_armed, first.batch_members, first.retired, first.deferred) == (
        1,
        15,
        15,
        15,
    )
    assert (second.batches_armed, second.batch_members, second.retired, second.deferred) == (
        1,
        15,
        15,
        0,
    )
    # Neither pass reached the kill: each returned with time left.
    assert (first_left, second_left) == (22_600, 22_600)
    # The first grant took exactly the 15 oldest runs, in D41 order; the second the rest.
    ((first_members),) = first_rows.values()
    assert [run for run, _node in first_members] == runs[:15]
    rows = _rows(fake)
    assert len(rows) == 2
    armed = [member for members in rows.values() for member in members]
    assert sorted(run for run, _node in armed) == sorted(runs)
    assert len(set(armed)) == 30  # no unit armed twice
    assert {node_of(fake, run, TABLE_KEY).state for run in runs} == {NodeState.READY}
    assert _arms_left(fake) == 0


def test_control_a_small_backlog_under_the_metered_clock_arms_exactly_as_with_an_ample_one() -> (
    None
):
    metered_world, ample_world = FakeDynamo(), FakeDynamo()
    _units_backlog(metered_world, 3, TABLE_KEY, "returns")
    _units_backlog(ample_world, 3, TABLE_KEY, "returns")
    meter = Metered(metered_world).invocation()

    bounded = _arm(metered_world, meter.clock)
    ample = _arm(ample_world, lambda: 60_000)

    assert bounded == ample
    assert (bounded.batches_armed, bounded.batch_members, bounded.retired) == (2, 6, 3)
    assert (bounded.deferred, bounded.grants_deferred) == (0, 0)
    assert metered_world.items == ample_world.items


def test_a_pass_killed_before_any_store_call_loses_no_unit_and_arms_none_twice() -> None:
    """A kill may land between any two store calls; the next passes must converge regardless."""
    probe = FakeDynamo()
    _units_backlog(probe, 4, TABLE_KEY, "returns")
    meter = Metered(probe).invocation()
    _arm(probe, meter.clock)
    calls = meter.calls
    assert calls == 74  # 64 shard queries, 2 reads for each of 4 runs, 2 table grants

    for kill_at in range(1, calls + 1):
        fake = FakeDynamo()
        runs = _units_backlog(fake, 4, TABLE_KEY, "returns")
        metered = Metered(fake)
        with pytest.raises(LambdaKilledError):
            _arm(fake, metered.invocation(kill_at=kill_at).clock)
        for _ in range(2):
            _settle_locks(fake)
            _arm(fake, metered.invocation().clock)

        armed = [member for members in _rows(fake).values() for member in members]
        expected = sorted(
            (run, node_of(fake, run, key).node_id) for run in runs for key in (TABLE_KEY, "returns")
        )
        assert (kill_at, sorted(armed)) == (kill_at, expected)
        states = {node_of(fake, run, key).state for run in runs for key in (TABLE_KEY, "returns")}
        assert (kill_at, states, _arms_left(fake)) == (kill_at, {NodeState.READY}, 0)


def test_tables_are_granted_oldest_head_first_and_the_rest_wait_for_the_next_pass() -> None:
    """No table name can starve behind another: the oldest head is granted first."""
    fake = FakeDynamo()
    young = unit_run(fake, 1, "aaa", clock=_clock(9))
    old = unit_run(fake, 2, "zzz", clock=_clock(1))
    for run, minute in ((young, 9), (old, 1)):
        (arm,) = [key for key in fake.items if key[1] == f"ARM#{run}"]
        fake.items[arm]["root_clock_started_at"] = write_text(_clock(minute))
    # The pass reads its budget before each units item (2), then before each table grant: the
    # first grant starts at 30 s, the second would start at 9.999 s, under the 10 s reserve.
    readings = iter((60_000, 60_000, 30_000, 9_999))

    counts = _arm(fake, lambda: next(readings))

    assert (counts.batches_armed, counts.grants_deferred) == (1, 1)
    assert node_of(fake, old, "zzz").state is NodeState.READY
    assert node_of(fake, young, "aaa").state is NodeState.PENDING
    assert [key for key in fake.items if key[1] == f"ARM#{young}"]  # it stays for the next pass


# --- The release ------------------------------------------------------------------------------


def _unit(shard: int, index: int) -> str:
    """Return a fictional conflict unit in `DUE#<shard>` (its first 8 hex digits mod 16)."""
    return f"{16 * index + shard:08x}" + "c" * 56


def _granted_unapplied(fake: FakeDynamo, unit: str) -> str:
    """Seed a run whose grant committed but whose apply did not: `APPLY#` is its re-drive."""
    run = seed_granting_run(fake, unit=unit, fingerprint="a" * 64, request_id="r-apply").record
    store_ = release_store(fake)
    order = store_.read_release_order(INGESTION_RELEASE_DOMAIN)
    targets = tuple(
        sorted({n.lock_key for n in store_.read_nodes(run.run_id)} - {f"grant_apply#{run.run_id}"})
    )
    reservation = reservation_set(run.run_id, INGESTION_RELEASE_DOMAIN, order.next_ordinal, targets)
    store_.acquire_ordinal(order, run, reservation)
    for target in targets:
        store_.reserve_target(reservation, target)
    store_.seal_proof(reservation)
    current = store_.read_run(run.run_id)
    assert current is not None
    issue_grant(
        context(fake), run.run_id, ordinal=reservation.ordinal, expected_revision=current.revision
    )
    return run.run_id


def _completable(fake: FakeDynamo, unit: str) -> str:
    """Seed a granted, applied, bound run with terminal POST_GRANT nodes: `COMPLETE#` is due."""
    run = seed_granting_run(fake, unit=unit, fingerprint="b" * 64, request_id="r-complete").record
    assert release_run(context(fake), run.run_id) is ReleaseOutcome.APPLIED
    settle_publish(fake, run.run_id)
    reserve_winner(fake, run.run_id)
    store_ = release_store(fake)
    record = store_.read_run(run.run_id)
    assert record is not None and record.grant_id is not None
    grant, root = record.grant_id, record.root_flow_id
    contribute(
        store_,
        CohortContribution(
            grant, CohortMember.PRECOMPUTE, CohortOutcome.NO_ELIGIBLE, None, NOW, root
        ),
        record,
    )
    contribute(
        store_,
        CohortContribution(
            grant, CohortMember.BAKE, CohortOutcome.SUCCEEDED, serving_generation(grant), NOW, root
        ),
        record,
    )
    for name in ("publish", "precompute", "bake"):
        fake.items[(f"RUN#{run.run_id}", f"NODE#{name}")]["state"] = write_text("SUCCEEDED")
    return run.run_id


def _not_ready(fake: FakeDynamo, count: int, shard: int) -> list[str]:
    """`count` OPEN runs in `DUE#<shard>` whose writes have not settled, oldest first."""
    runs: list[str] = []
    for index in range(count):
        run = seed_granting_run(
            fake,
            unit=_unit(shard, index + 1),
            fingerprint=f"{index + 16:02x}" * 32,
            request_id=f"r-wait-{index:03d}",
            settled=False,
        ).record.run_id
        fake.items[(due_shard(_unit(shard, index + 1)), f"RELEASE#{run}")][
            "root_clock_started_at"
        ] = write_text(_clock(index))
        runs.append(run)
    return runs


def _release(fake: FakeDynamo, clock: Callable[[], int]) -> ReleaseCounts:
    return release_pass(context(fake), due_shards(), PassBudget.of(clock))


def test_a_not_ready_backlog_in_the_first_shard_no_longer_starves_the_last_shards_work() -> None:
    """Live shape: 3,455 `NOT_READY` triggers starved the tail shards' applies (rca.md §2).

    The old pass polled every trigger in fixed `DUE#0`-to-`DUE#15` order.

    The meter charges 25 ms a shard query and 250 ms a call, so each trigger costs 1 s. The
    old order would poll all 40 triggers in `DUE#0` (40 s) before reaching `DUE#15`.
    """
    fake = FakeDynamo()
    completing = _completable(fake, _unit(15, 1))
    applying = _granted_unapplied(fake, _unit(15, 2))
    waiting = _not_ready(fake, 40, 0)
    metered = Metered(fake, due_query_ms=25, call_ms=250)
    meter = metered.invocation()

    counts = _release(fake, meter.clock)

    left = meter.remaining
    metered.idle()
    assert counts.outcomes == {"applied": 1, "not_ready": 10}
    # The winner completed; the run just applied is a waiting `COMPLETE#` reached at 19.95 s,
    # under the 35 s `COMPLETE#` reserve, so it and 30 triggers wait for the next pass.
    assert (counts.completed, counts.completion_pending, counts.deferred) == (1, 0, 31)
    assert (counts.failed, counts.failed_shards) == (0, 0)
    assert str(fake.items[(f"RUN#{completing}", "RUN")]["state"]["S"]) == "SUCCEEDED"
    assert str(fake.items[(f"RUN#{applying}", "RUN")]["state"]["S"]) == "GRANTED"
    # The triggers polled were the oldest 10 by root clock, whatever their run IDs.
    polled = [run for run in waiting if f"RUN#{run}" in meter.runs_read]
    assert polled == waiting[:10]
    # The 11th trigger would have started under the 10 s reserve; the pass returned instead.
    assert left == 9_550


def test_control_the_same_release_world_with_an_ample_clock_polls_every_trigger() -> None:
    fake = FakeDynamo()
    _completable(fake, _unit(15, 1))
    _granted_unapplied(fake, _unit(15, 2))
    _not_ready(fake, 40, 0)

    counts = _release(fake, lambda: 60_000)

    assert counts.outcomes == {"applied": 1, "not_ready": 40}
    # The run applied in the APPLY# phase is visited by the COMPLETE# phase that follows it.
    assert (counts.completed, counts.completion_pending, counts.deferred) == (1, 1, 0)


def _waiting_on_serving(fake: FakeDynamo, count: int, shard: int) -> list[str]:
    """`count` granted, applied runs in `DUE#<shard>` waiting on serving, lowest ordinal first.

    Each `COMPLETE#` is a poll for the release: the run is completed by the promoter's alias
    drain, not by the release.
    """
    return [
        granted_run(
            fake,
            unit=_unit(shard, index + 1),
            fingerprint=f"{index + 96:02x}" * 32,
            request_id=f"r-serve-{index:03d}",
        )
        for index in range(count)
    ]


def _complete_world(waiting: int) -> tuple[FakeDynamo, list[str], str, str]:
    """Seed `waiting` runs in `DUE#0`, a completable winner in `DUE#15`, a trigger in `DUE#3`."""
    fake = FakeDynamo()
    runs = _waiting_on_serving(fake, waiting, 0)
    winner = _completable(fake, _unit(15, 1))
    ready = seed_granting_run(
        fake, unit=_unit(3, 1), fingerprint="d" * 64, request_id="r-ready"
    ).record.run_id
    return fake, runs, winner, ready


def _run_state(fake: FakeDynamo, run: str) -> str:
    return str(fake.items[(f"RUN#{run}", "RUN")]["state"]["S"])


def test_a_complete_backlog_larger_than_one_pass_still_completes_the_winner_and_grants() -> None:
    """Review round 1, AGG-1: 60 waiting runs in `DUE#0` cost 60 s to poll in shard order.

    The round-1 pass polled them first and reached neither the winner in `DUE#15` nor the
    trigger, so no run was completed or granted and the drain stopped again. The meter charges
    25 ms a shard query and 250 ms a call, so a waiting run's poll (4 calls) costs 1 s.
    """
    fake, runs, winner, ready = _complete_world(60)
    metered = Metered(fake, due_query_ms=25, call_ms=250)
    meter = metered.invocation()

    counts = _release(fake, meter.clock)

    left = meter.remaining
    metered.idle()
    assert counts.outcomes == {"applied": 1}
    assert (counts.completed, counts.completion_pending, counts.deferred) == (1, 5, 55)
    assert (counts.failed, counts.failed_shards) == (0, 0)
    assert (_run_state(fake, winner), _run_state(fake, ready)) == ("SUCCEEDED", "GRANTED")
    # The waiting runs polled were the 5 lowest grant ordinals; the rest wait for the next pass.
    assert [run for run in runs if f"RUN#{run}" in meter.runs_read] == runs[:5]
    assert {_run_state(fake, run) for run in runs} == {"GRANTED"}
    # The winner started at 50.95 s and the waiting runs until 35.7 s; the trigger, the last
    # item, started at 34.3 s, above the 10 s reserve, and the pass returned before the kill.
    assert left == 8_550


def test_control_the_same_complete_world_with_an_ample_clock_visits_every_item() -> None:
    fake, runs, winner, ready = _complete_world(60)

    counts = _release(fake, lambda: 60_000)

    assert counts.outcomes == {"applied": 1}
    assert (counts.completed, counts.completion_pending, counts.deferred) == (1, 60, 0)
    assert (_run_state(fake, winner), _run_state(fake, ready)) == ("SUCCEEDED", "GRANTED")
    assert {_run_state(fake, run) for run in runs} == {"GRANTED"}


def _finalizer_world(waiting: int) -> tuple[FakeDynamo, list[str], str]:
    """Seed `waiting` serving `ARM#` items in `DUE#0..14` and the ACTIVE winner in `DUE#15`.

    The winner's bake has fanned out: its one task is terminal and its finalizer PENDING, and
    its serving `ARM#` is re-put, as `fan_out_bake_tasks` does, so the arm pass can arm the
    finalizer. A waiting run's arm is a no-op: it is not its execution's winner.
    """
    fake = FakeDynamo()
    runs: list[str] = []
    for index in range(waiting):
        run = granted_run(
            fake,
            unit=_unit(index % 15, index // 15 + 1),
            fingerprint=f"{index + 96:02x}" * 32,
            request_id=f"r-serve-{index:03d}",
        )
        settle_publish(fake, run)
        runs.append(run)
    unit = _unit(15, 1)
    winner = granted_run(fake, unit=unit, fingerprint="e" * 64, request_id="r-winner")
    settle_publish(fake, winner)
    arm_key = (due_shard(unit), f"ARM#{winner}")
    arm = copy.deepcopy(fake.items[arm_key])
    reserve_winner(fake, winner)
    template = fake.items[(f"RUN#{winner}", "NODE#precompute")]
    for name, state, after, node_id in (
        ("bake_task:campaigns#7d", "SUCCEEDED", "bake", "a" * 64),
        ("bake_finalize", "PENDING", "bake_task", "b" * 64),
    ):
        node = copy.deepcopy(template)
        node.update(
            sk=write_text(f"NODE#{name}"),
            node=write_text(name),
            node_id=write_text(node_id),
            state=write_text(state),
            after={"L": [write_text(after)]},
            lock_key=write_text(f"POST_GRANT#{name}#{unit}"),
            group_id=write_text(f"{name}#{unit}"),
        )
        fake.items[(f"RUN#{winner}", f"NODE#{name}")] = node
    fake.items[arm_key] = arm
    return fake, runs, winner


def _finalizer(fake: FakeDynamo, run: str) -> str:
    return str(fake.items[(f"RUN#{run}", "NODE#bake_finalize")]["state"]["S"])


def test_the_active_winners_finalizer_is_armed_first_behind_a_serving_backlog() -> None:
    """Review round 1, AGG-2: 45 waiting serving `ARM#` items in `DUE#0..14` fill one pass.

    The round-1 pass armed them in shard order and stopped at the reserve before the winner in
    `DUE#15`, so its finalizer never armed, its head stayed ACTIVE and serving stopped. The
    meter charges 10 ms a shard query and 250 ms a call; a waiting item costs about 1 s.
    """
    fake, _runs, winner = _finalizer_world(45)
    metered = Metered(fake, due_query_ms=10, call_ms=250)
    meter = metered.invocation()

    counts = _arm(fake, meter.clock)

    left = meter.remaining
    metered.idle()
    assert (counts.armed, counts.retired, counts.failed, counts.deferred) == (1, 1, 0, 5)
    assert _finalizer(fake, winner) == "READY"
    assert (due_shard(_unit(15, 1)), f"ARM#{winner}") not in fake.items
    assert _arms_left(fake) == 45  # every waiting item stays for a later pass
    # The winner was armed at 51.11 s; 40 waiting items followed until 10.46 s remained.
    assert left == 9_450


def test_control_the_same_finalizer_world_with_an_ample_clock_arms_the_same_finalizer() -> None:
    fake, _runs, winner = _finalizer_world(45)

    counts = _arm(fake, lambda: 60_000)

    assert (counts.armed, counts.retired, counts.failed, counts.deferred) == (1, 1, 0, 0)
    assert _finalizer(fake, winner) == "READY"
    assert _arms_left(fake) == 45


def _released_waiter(fake: FakeDynamo, unit: str, tag: str) -> None:
    """Seed a unit whose active run was released with one waiter queued, for the next promote."""
    set_mode(fake, AuthorityMode.GRAPH)
    active = seed_run(
        fake, unit=unit, fingerprint=f"{tag}1" * 32, request_id=f"active-{tag}", instantiate=False
    ).record
    request_id = f"wait-{tag}"
    fake.items[(f"RECEIPT#{request_id}", "RECEIPT")] = cast(
        "Any",
        receipt_item(
            ReceiptRecord(
                request_id=request_id,
                request_digest="d" * 64,
                decision="QUEUED_LATEST",
                reason=None,
                row=19,
                authority_epoch=1,
                mode=AuthorityMode.GRAPH,
                conflict_unit=unit,
                input_revision=NOW.isoformat(),
                fingerprint=f"{tag}8" * 32,
                recorded_at=now_text(),
                protocol_version="v1",
                contract_revision=1,
                root_flow_id=f"root-{tag}",
            )
        ),
    )
    fake.items[(due_shard(unit), f"WAITER#{unit}")] = {
        "pk": write_text(due_shard(unit)),
        "sk": write_text(f"WAITER#{unit}"),
        "item_kind": write_text("waiter"),
        "authority_epoch": write_int(1),
        "request_id": write_text(request_id),
        "fingerprint": write_text(f"{tag}8" * 32),
        "retrieved_at": write_text(NOW.isoformat()),
        "held": write_bool(False),
        "revision": write_int(1),
        "due_at": write_text(now_text()),
    }
    store(fake).fence_run(FenceRunRequest(active, FenceReason.CANCELLED, now_text()))


def test_a_later_shards_waiter_left_by_the_promote_reserve_is_counted_and_promoted_next() -> None:
    """Review round 1, AGG-3: a shard the budget stopped before was not read or counted.

    The terminal `deferred` was 0 while a waiter was left unstarted in a later shard, so a
    backlog could look drained.
    """
    fake = FakeDynamo()
    first, later = _unit(2, 1), _unit(9, 1)
    _released_waiter(fake, first, "a")
    _released_waiter(fake, later, "b")
    promoter = PromoterContext(release_store(fake), ingestion_node_plan, lambda: NOW)
    readings = iter((40_000, 40_000, 40_000, 40_000))
    # The entries of `DUE#0`, `DUE#1` and `DUE#2` and `DUE#2`'s one unit see 40 s; every later
    # reading is 39.999 s, under the reserve.
    budget = PassBudget.of(lambda: next(readings, 39_999))

    passes = [promote_shard(promoter, shard, budget) for shard in due_shards()]

    assert sum(item.deferred for item in passes) == 1  # what the handler's terminal line sums
    assert [item.branches for item in passes if item.branches] == [{"promote_waiter": 1}]
    assert (due_shard(first), f"WAITER#{first}") not in fake.items
    assert (due_shard(later), f"WAITER#{later}") in fake.items
    # The next pass, with its time, promotes the later shard's waiter once.
    again = [
        promote_shard(promoter, shard, PassBudget.of(lambda: 60_000)) for shard in due_shards()
    ]
    assert sum(item.deferred for item in again) == 0
    assert [item.branches for item in again if item.branches] == [{"promote_waiter": 1}]
    assert (due_shard(later), f"WAITER#{later}") not in fake.items


# --- The budget itself ------------------------------------------------------------------------


@pytest.mark.parametrize("reading", [0, -1, True, 1.5, "60000"])
def test_an_invalid_remaining_reading_ends_the_release_pass_with_exec_graph_35(
    reading: object,
) -> None:
    fake = FakeDynamo()
    _not_ready(fake, 1, 0)
    with pytest.raises(InvariantError) as refused:
        release_pass(context(fake), due_shards(), PassBudget.of(lambda: reading))
    assert str(refused.value.code) == "ADLY-EXEC_GRAPH-0035"


def test_a_rising_remaining_reading_ends_the_pass_and_a_falling_one_is_accepted() -> None:
    readings = iter((30_000, 20_000, 20_001))
    budget = PassBudget.of(lambda: next(readings))
    assert budget.allows(10_000) is True
    assert budget.allows(20_000) is True
    with pytest.raises(InvariantError) as refused:
        budget.allows(10_000)
    assert str(refused.value.code) == "ADLY-EXEC_GRAPH-0035"


def test_the_reserves_are_ordered_and_each_is_under_one_deployed_pass() -> None:
    """A reserve at or over the Lambda timeout would stop every pass before its first item."""
    pass_ms = int(BAKE_TASKS["worker_pass_seconds"]) * 1000
    assert pass_ms == 60_000
    reserves = (ITEM_RESERVE_MS, UNITS_COLLECT_RESERVE_MS, COMPLETE_RESERVE_MS, PROMOTE_RESERVE_MS)
    assert reserves == (10_000, 25_000, 35_000, 40_000)
    assert ITEM_RESERVE_MS < UNITS_COLLECT_RESERVE_MS < COMPLETE_RESERVE_MS < PROMOTE_RESERVE_MS
    assert pass_ms > PROMOTE_RESERVE_MS
