# SPDX-License-Identifier: MIT-0
"""Plan item 12: the promoter's `FreshElectionTimingV1` timing and conflict-injection seam.

The rehearsal world is the production-built rolling world of `fixed_member_byte_fixture_support`:
A drained down to its final alias with B published while A was ACTIVE. Fictional data only.
"""

from __future__ import annotations

import copy
import inspect
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest
from execution_graph_dispatch_support import NOW
from execution_graph_release_support import release_store
from execution_graph_support import FakeDynamo
from fixed_member_byte_fixture_support import (
    build_world,
    capture_final_transaction,
    complete_direct_winner,
    grant_and_publish,
    promoter_pass,
)

from adapters.jobs.execution_graph import promoter as promoter_module
from adapters.jobs.execution_graph.fence_settler import due_shards
from adapters.jobs.execution_graph.instantiate import ingestion_node_plan
from adapters.jobs.execution_graph.promoter import (
    FRESH_ELECTION_SEGMENTS,
    FreshElectionInjectedConflictError,
    FreshElectionRehearsal,
    FreshElectionRehearsalRefusedError,
    PromoterContext,
    arm_pass,
    coalesce_serving,
    rehearse_fresh_election,
)
from domain.operations.execution_graph_release import INGESTION_RELEASE_DOMAIN
from domain.operations.generated_execution_graphs import BOUNDS, SERVING_COALESCING
from ports.outbound.execution_graph_dispatch import GraphTransactionConflictError
from ports.outbound.execution_graph_release import ServingElectionOutcome

pytestmark = pytest.mark.unit

_MAX_REDECIDE = int(BOUNDS["max_redecide"])
_WORKERS = Path(promoter_module.__file__).with_name("workers.py")


class _Ticks:
    """A clock that advances one millisecond per reading, so every segment is measurable."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        self.now += 0.001
        return self.now


def _context(fake: FakeDynamo) -> PromoterContext:
    return PromoterContext(release_store(fake), ingestion_node_plan, lambda: NOW)


def _fresh_invocation(remaining: int = 60_000) -> Callable[[], Callable[[], int]]:
    return lambda: lambda: remaining


def _transition_target(member: dict[str, Any]) -> bool:
    ((kind, body),) = member.items()
    key = cast("dict[str, dict[str, str]]", body["Item"] if kind == "Put" else body["Key"])
    return key["pk"]["S"].startswith("SERVING_TRANSITION#")


def _final_transactions(fake: FakeDynamo) -> int:
    """Count committed multi-member transactions that write a `SERVING_TRANSITION#` row."""
    return sum(
        1
        for members in fake.transactions
        if len(members) > 1 and any(_transition_target(member) for member in members)
    )


def _rehearse(fake: FakeDynamo, rehearsal: FreshElectionRehearsal, remaining: int = 60_000) -> Any:
    return rehearse_fresh_election(
        _context(fake),
        due_shards(),
        INGESTION_RELEASE_DOMAIN,
        rehearsal,
        _fresh_invocation(remaining),
        "fresh-election-rehearsal",
    )


def test_one_sample_runs_the_full_conflict_envelope_before_one_real_activation() -> None:
    world = build_world(3, 2)
    fake = world.fake
    committed = _final_transactions(fake)
    rehearsal = FreshElectionRehearsal(conflicts=_MAX_REDECIDE, clock=_Ticks())

    result = _rehearse(fake, rehearsal)

    assert result.outcome is ServingElectionOutcome.ELECTED
    assert [attempt.injected_conflict for attempt in rehearsal.attempts] == [True] * (
        _MAX_REDECIDE
    ) + [False]
    assert _final_transactions(fake) == committed + 1  # injected conflicts wrote nothing
    for attempt in rehearsal.attempts:
        assert set(attempt.segments_ms) == set(FRESH_ELECTION_SEGMENTS)
        assert all(value > 0 for value in attempt.segments_ms.values())
        assert attempt.total_ms >= sum(attempt.segments_ms.values())
    assert rehearsal.attempts[0].retry_ms == 0
    assert all(attempt.retry_ms > 0 for attempt in rehearsal.attempts[1:])
    head = release_store(fake).read_serving_domain(INGESTION_RELEASE_DOMAIN).head
    assert head is not None and head.state == "ACTIVE"
    assert set(head.elected_run_ids) == set(world.b_runs)
    assert result.transition is not None
    assert result.transition.aws_request_id == "fresh-election-rehearsal"


def test_a_real_conflict_on_the_activation_is_not_retried_and_writes_nothing() -> None:
    world = build_world(3, 2)
    fake = world.fake
    before = copy.deepcopy(fake.items)
    # The two injected attempts call no store write, so the next transaction is the real one.
    fake.conflicts_next = 1
    rehearsal = FreshElectionRehearsal(conflicts=2, clock=_Ticks())
    with pytest.raises(GraphTransactionConflictError) as raised:
        _rehearse(fake, rehearsal)

    assert not isinstance(raised.value, FreshElectionInjectedConflictError)
    assert fake.conflicts_raised == 1
    assert [attempt.injected_conflict for attempt in rehearsal.attempts] == [True, True]
    assert fake.items == before


def test_a_cohort_above_its_final_alias_is_refused_before_any_write() -> None:
    fake = FakeDynamo()
    a_runs = tuple(grant_and_publish(fake, "a", index) for index in range(3))
    promoter_pass(fake, "elect-a")
    complete_direct_winner(fake, a_runs[-1])
    before = copy.deepcopy(fake.items)

    with pytest.raises(FreshElectionRehearsalRefusedError):
        _rehearse(fake, FreshElectionRehearsal(conflicts=1, clock=_Ticks()))

    assert fake.items == before


def test_a_remaining_time_below_the_guard_is_refused_without_writing() -> None:
    world = build_world(3, 2)
    before = copy.deepcopy(world.fake.items)
    rehearsal = FreshElectionRehearsal(conflicts=1, clock=_Ticks())

    with pytest.raises(FreshElectionRehearsalRefusedError, match="drain_incomplete"):
        _rehearse(
            world.fake,
            rehearsal,
            remaining=int(SERVING_COALESCING["next_election_min_remaining_ms"]) - 1,
        )

    assert rehearsal.attempts == []
    assert world.fake.items == before


def test_a_domain_that_is_not_draining_is_refused() -> None:
    fake = FakeDynamo()
    tuple(grant_and_publish(fake, "a", index) for index in range(2))
    promoter_pass(fake, "elect-a")  # ACTIVE, not draining
    with pytest.raises(FreshElectionRehearsalRefusedError, match="not draining"):
        _rehearse(fake, FreshElectionRehearsal(conflicts=0, clock=_Ticks()))


def test_the_rehearsal_input_is_validated() -> None:
    with pytest.raises(ValueError, match="nonnegative"):
        FreshElectionRehearsal(conflicts=-1)
    with pytest.raises(TypeError):
        rehearse_fresh_election(
            _context(FakeDynamo()),
            due_shards(),
            INGESTION_RELEASE_DOMAIN,
            object(),  # type: ignore[arg-type]
            _fresh_invocation(),
            "request",
        )
    rehearsal = FreshElectionRehearsal(conflicts=0)
    with pytest.raises(ValueError, match="unknown rehearsal segment"):
        rehearsal.timed("bake", lambda: None)
    with pytest.raises(FreshElectionRehearsalRefusedError):
        rehearsal.before_activation()  # no passing guard check opened an attempt


def test_the_seam_is_inert_in_every_runtime_path(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the rehearsal seam ran outside a rehearsal")

    for name in ("timed", "guard_passed", "before_activation", "end_attempt"):
        monkeypatch.setattr(FreshElectionRehearsal, name, forbidden)
    world = build_world(3, 2)

    capture = capture_final_transaction(world)

    assert capture is not None and len(capture.request["TransactItems"]) == 51
    # No runtime entry point accepts a rehearsal, and the Lambda composition never names one.
    for entry in (arm_pass, coalesce_serving):
        assert "rehearsal" not in inspect.signature(entry).parameters
    assert "Rehearsal" not in _WORKERS.read_text(encoding="utf-8")
    assert "rehearse_fresh_election" not in _WORKERS.read_text(encoding="utf-8")
