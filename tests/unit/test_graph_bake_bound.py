# SPDX-License-Identifier: MIT-0
"""D2 option (b) through the V3 bake harness (plan A5.1 Tests 1a, 8; rev5 obligation O5).

The certified bound, its precedence, prefetch, scope safety after a fan-out exception, and the
fan-out state after a byte-envelope refusal. Each passing check has a paired control.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from execution_graph_release_support import release_store
from graph_bake_v3_support import Bake, FakeRequest

from adapters.jobs.execution_graph import post_grant_consumer
from adapters.outbound.bake_sources import BakeClockMode, BakeReadMode
from domain.operations.execution_graph_dispatch import NodeState
from domain.operations.execution_graph_release import (
    BAKE_EXECUTIONS_OVER_CERTIFIED_BOUND,
    DURABLE_BAKE_SOURCE_PIN_ABSENT,
    BakeRoster,
    BakeRosterRow,
)
from domain.shared_kernel.errors import DependencyError, ErrorCode, InvariantError

pytestmark = [pytest.mark.unit]


def _roster(certified: int, count: int = 2) -> BakeRoster:
    return BakeRoster(
        tuple(
            BakeRosterRow("campaigns", f"{index + 1}d", 240, 16.6, 123, certified)
            for index in range(count)
        )
    )


def _dispatched(bake: Bake) -> str:
    run = bake.granted()
    assert bake.fan_out(run).node_state is NodeState.SUCCEEDED
    bake.dispatch(run)
    return run


def _pins(bake: Bake) -> list[str]:
    return sorted(
        sk for _pk, sk in bake.tables.items if sk == "BAKE_PIN" or sk.startswith("BAKE_SOURCE#")
    )


# --- the certified bound (I5) ----------------------------------------------------------------


def test_the_bound_plus_one_request_fails_the_task_before_it_executes() -> None:
    """The harness render issues two distinct requests: a bound of one refuses the second."""
    bake = Bake(roster=_roster(1))
    run = _dispatched(bake)
    first = bake.roster.keys[0]

    refused = bake.task(run, first)

    assert (refused.node_state, refused.failure_reason) == (
        NodeState.FAILED,
        BAKE_EXECUTIONS_OVER_CERTIFIED_BOUND,
    )
    assert bake.queries.calls.count(first) == 1  # n - 1 = 1 execution; the refused one never ran
    # Control: the same render at a bound of two executes both and succeeds.
    control = Bake(roster=_roster(2))
    control_run = _dispatched(control)
    assert control.task(control_run, first).node_state is NodeState.SUCCEEDED
    assert control.queries.calls.count(first) == 2


def test_a_source_that_swallows_the_refusal_still_fails_its_task() -> None:
    bake = Bake(roster=_roster(1))
    run = _dispatched(bake)
    first = bake.roster.keys[0]
    bake.sources.swallow.add(first)

    refused = bake.task(run, first)

    assert refused.failure_reason == BAKE_EXECUTIONS_OVER_CERTIFIED_BOUND
    # A second task on the same runtime runs with no limit left over.
    assert bake.task(run, bake.roster.keys[1]).failure_reason == (
        BAKE_EXECUTIONS_OVER_CERTIFIED_BOUND
    )
    assert bake.runtime.queries.capturing is False


def test_the_refusal_wins_over_a_raced_dependency_error_and_the_control_retries() -> None:
    bake = Bake(roster=_roster(1))
    run = _dispatched(bake)
    first = bake.roster.keys[0]
    bake.sources.gather.add(first)
    bake.queries.fail[first] = DependencyError(ErrorCode("ATHENA", 1), "throttled")

    refused = bake.task(run, first)

    assert refused.failure_reason == BAKE_EXECUTIONS_OVER_CERTIFIED_BOUND
    # Control: at a bound of two the same DependencyError is re-raised and retried, as today.
    control = Bake(roster=_roster(2))
    control_run = _dispatched(control)
    control.sources.gather.add(first)
    control.queries.fail[first] = DependencyError(ErrorCode("ATHENA", 1), "throttled")
    retried = control.task(control_run, first)
    assert retried.node_state is not NodeState.FAILED
    assert retried.acknowledge is False


def test_the_task_summary_records_the_executed_distinct_set() -> None:
    lines: list[dict[str, Any]] = []
    bake = Bake(roster=_roster(2))

    def log(phase: str, _outcome: str, _severity: object = None, **details: object) -> None:
        lines.append({"phase": phase, **details})

    object.__setattr__(bake.runtime, "log", log)
    run = _dispatched(bake)
    bake.task(run, bake.roster.keys[0])
    (summary,) = (line for line in lines if line["phase"] == "task_summary")
    assert summary["executed_distinct"] == 2
    keys = summary["executed_request_digests"]
    assert keys == sorted(keys)
    assert all(key.startswith("query:") for key in keys)


# --- prefetch and scope safety (Tests 1a, 8) ------------------------------------------------


def _clean(bake: Bake) -> None:
    """Assert the reused runtime is back to PASSTHROUGH, non-capturing, unlimited, unpinned."""
    reads, port = bake.runtime.reads, bake.runtime.queries
    assert reads.mode is BakeReadMode.PASSTHROUGH
    assert port.capturing is False
    assert reads.clock.mode is BakeClockMode.UNSET
    with pytest.raises(InvariantError):
        reads.clock()
    calls = len(bake.queries.calls)
    assert port.run(cast("Any", FakeRequest("after", 0, "x"))) is None  # reached the adapter
    assert len(bake.queries.calls) == calls + 1
    scope_reads = bake.current.current_calls
    scope = next(iter(bake.current.by_scope.values()))[0].partition.scope
    reads.current_for_scope(scope)
    assert bake.current.current_calls == scope_reads + 1  # reached the provider


@pytest.mark.parametrize("where", ["prefetch", "bake"])
def test_a_fan_out_exception_pins_nothing_and_leaves_the_runtime_clean(where: str) -> None:
    bake = Bake()
    run = bake.granted()
    key = bake.roster.keys[1]
    error = RuntimeError(f"{where} failed")
    if where == "prefetch":
        bake.sources.prefetch_raises[key] = error
    else:
        bake.sources.raises[key] = error

    result = bake.fan_out(run)

    assert result.node_state is not NodeState.SUCCEEDED
    assert _pins(bake) == []
    _clean(bake)


def test_a_prefetch_that_issues_a_query_runs_no_query_and_fails_only_its_own_task() -> None:
    """F2: the guard's invariant pins its row with zero requests; the task replays it, FAILED."""
    bake = Bake()
    run = bake.granted()
    guarded = bake.roster.keys[0]
    bake.sources.prefetch_query.add(guarded)

    result = bake.fan_out(run)

    assert result.node_state is NodeState.SUCCEEDED
    assert len(_pins(bake)) == len(bake.roster.rows) + 1
    pin = release_store(bake.tables).read_durable_bake_sources(run)
    assert pin is not None and pin.manifest.task(guarded).plan_count == 0
    bake.dispatch(run)
    failed = bake.task(run, guarded)
    assert failed.node_state is NodeState.FAILED  # the invariant stays fatal for its task
    assert all(call != guarded for call in bake.queries.calls)
    # Control: the same fan-out without the prefetch query pins every row.
    control = Bake()
    assert control.fan_out(control.granted()).node_state is NodeState.SUCCEEDED
    assert len(_pins(control)) == len(control.roster.rows) + 1


# --- obligation O5: the fan-out state after a byte-envelope refusal --------------------------


def test_a_byte_envelope_refusal_keeps_the_selection_marker_and_redelivery_is_pin_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Zero `BAKE_SOURCE#` / `BAKE_PIN` writes; the already-committed selection marker remains."""
    bake = Bake()
    run = bake.granted()
    node = bake.node(run, "bake")
    store = release_store(bake.tables)

    def over_envelope(_request: object, _members: object) -> None:
        raise InvariantError(
            ErrorCode("GRAPH_BAKE_V3", 10),
            "the bake source pin exceeds the transaction byte envelope.",
            context={"signal": "bake_source_pin_over_byte_envelope"},
        )

    monkeypatch.setattr(type(store), "_guard_bake_pin", staticmethod(over_envelope))
    object.__setattr__(bake.graph, "store", store)

    first = bake.fan_out(run)

    assert (first.node_state, first.failure_reason) == (NodeState.FAILED, "post_grant_invariant")
    claim = bake.tables.items[(f"EXEC_CLAIM#{node.node_id}", "CLAIM")]
    assert claim["bake_selection_started"] == {"BOOL": True}
    assert _pins(bake) == []
    # A redelivery reads no CURRENT and pins nothing: the node settled FAILED with the marker
    # left in place. The next test drives the reclaim whose settlement was lost.
    calls = bake.current.current_calls
    second = bake.fan_out(run, receive_count=2)
    assert second.acknowledge is True
    assert bake.current.current_calls == calls
    assert _pins(bake) == []


def test_a_byte_envelope_refusal_whose_settlement_is_lost_reclaims_as_pin_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rev5 O5: marker written, pin refused, FAILED settlement lost; the reclaim is pin-absent.

    The first delivery is never settled: the process is lost between the byte-envelope refusal
    and the FAILED settlement, so the node stays CLAIMED with only the selection marker. The
    reclaim must take DB21's fatal `durable_bake_source_pin_absent` path with zero CURRENT reads
    and zero pin-family writes (the pin guard is never reached again).
    """
    bake = Bake()
    run = bake.granted()
    node = bake.node(run, "bake")
    store = release_store(bake.tables)
    guarded: list[object] = []

    def over_envelope(request: object, _members: object) -> None:
        guarded.append(request)
        raise InvariantError(
            ErrorCode("GRAPH_BAKE_V3", 10),
            "the bake source pin exceeds the transaction byte envelope.",
            context={"signal": "bake_source_pin_over_byte_envelope"},
        )

    monkeypatch.setattr(type(store), "_guard_bake_pin", staticmethod(over_envelope))
    object.__setattr__(bake.graph, "store", store)
    settle = post_grant_consumer._invariant_settlement  # pyright: ignore[reportPrivateUsage]
    signals: list[object] = []

    def lost(_state: object, _run: object, error: BaseException) -> object:
        signals.append(getattr(error, "context", {}).get("signal"))
        raise RuntimeError("process lost before the FAILED settlement")

    monkeypatch.setattr(post_grant_consumer, "_invariant_settlement", lost)

    first = bake.fan_out(run)

    # Not pre-settled: the refusal never reached a settlement, so the node is still claimed.
    assert first.acknowledge is False
    assert bake.node(run, "bake").state is not NodeState.FAILED
    claim = bake.tables.items[(f"EXEC_CLAIM#{node.node_id}", "CLAIM")]
    assert claim["bake_selection_started"] == {"BOOL": True}
    assert _pins(bake) == []

    def recorded(state: Any, run_record: Any, error: BaseException) -> object:
        signals.append(getattr(error, "context", {}).get("signal"))
        return settle(state, run_record, error)

    monkeypatch.setattr(post_grant_consumer, "_invariant_settlement", recorded)
    claim["lease_expires_at"] = {"S": "2000-01-01T00:00:00+00:00"}
    calls = bake.current.current_calls

    second = bake.fan_out(run, receive_count=2)

    assert signals == ["bake_source_pin_over_byte_envelope", DURABLE_BAKE_SOURCE_PIN_ABSENT]
    assert second.node_state is NodeState.FAILED
    assert bake.current.current_calls == calls
    assert len(guarded) == 1
    assert _pins(bake) == []
