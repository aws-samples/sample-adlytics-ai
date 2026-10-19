# SPDX-License-Identifier: MIT-0
"""Rolling serving authority tests over the real DynamoDB release adapter fake."""

from __future__ import annotations

import copy
from dataclasses import replace
from typing import Any, cast

import pytest
from execution_graph_dispatch_support import NOW, TABLE, UNIT, write_int, write_text
from execution_graph_release_support import (
    context,
    granted_run,
    release_store,
    reserve_winner,
    seed_granting_run,
    settle_publish,
    unarmed_winner,
)
from execution_graph_support import FakeDynamo
from fixed_member_byte_fixture_support import (
    FINAL_REQUEST_ID,
    DrainActivationWorld,
    build_world,
    capture_final_transaction,
)

from adapters.jobs.execution_graph.release import (
    ReleaseOutcome,
    release_run,
    settle_unarmed_cohort,
)
from adapters.outbound.dynamodb.execution_graph import due_shard
from adapters.outbound.dynamodb.execution_graph_release import DynamoGraphReleaseStore
from adapters.outbound.dynamodb.graph_transactions import Member
from domain.operations.execution_graph_dispatch import (
    GraphGuardError,
    NodeState,
    ServingCandidate,
    election_transaction_member_count,
    serving_frontier_shard,
)
from domain.operations.execution_graph_release import (
    INGESTION_RELEASE_DOMAIN,
    BindingKind,
    CohortContribution,
    CohortMember,
    CohortOutcome,
    ServingBinding,
)
from domain.shared_kernel.errors import InvariantError
from ports.outbound.execution_graph_dispatch import (
    GraphStoreUnavailableError,
    GraphTransactionConflictError,
)
from ports.outbound.execution_graph_release import (
    DrainFinalAliasAndActivateNextRequest,
    DrainFinalAliasAndActivateNextResult,
    ServingDirectDrainRequest,
    ServingElectionOutcome,
    ServingMutationResult,
    UnarmedPrecomputeSettlement,
)

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-075")]


def _candidate(fake: FakeDynamo) -> ServingCandidate:
    run_id = granted_run(fake)
    settle_publish(fake, run_id)
    store = release_store(fake)
    run = store.read_run(run_id)
    assert run is not None and run.grant_id is not None and run.grant_ordinal is not None
    publish = store.read_node(run_id, "publish")
    assert publish is not None and publish.effect_digest is not None
    return ServingCandidate(
        run_id,
        run.grant_id,
        run.root_flow_id,
        INGESTION_RELEASE_DOMAIN,
        run.grant_ordinal,
        publish.effect_digest,
    )


def _binding(candidate: ServingCandidate) -> ServingBinding:
    return ServingBinding(
        binding_id="binding-direct",
        grant_id=candidate.grant_id,
        conflict_unit=UNIT,
        served_revision=NOW,
        artifact_revision="input-1",
        contributor_roots=(candidate.root_flow_id,),
        bound_at=NOW,
        unavailable=(),
        root_flow_id=candidate.root_flow_id,
        authority_epoch=1,
        domain=candidate.domain,
        cohort_revision="cohort-1",
        serving_input_revision="input-1",
        plan_digest="plan-1",
        binding_kind=BindingKind.DIRECT,
        winner_run_id=candidate.run_id,
        execution_attempts=1,
    )


def test_first_domain_use_bootstraps_head_and_all_32_frontier_shards() -> None:
    fake = FakeDynamo()
    run = seed_granting_run(fake).record.run_id

    assert release_run(context(fake), run) is ReleaseOutcome.APPLIED

    store = release_store(fake)
    authority = store.read_serving_domain(INGESTION_RELEASE_DOMAIN)
    assert authority.head is not None and authority.head.state == "IDLE"
    assert authority.frontier is not None
    assert authority.frontier.revisions == (0,) * 32
    complete = fake.items[(due_shard(UNIT), f"COMPLETE#{run}")]
    assert set(complete) >= {"run_id", "grant_id", "grant_ordinal", "domain"}
    assert "origin_day" not in complete and "cycle_epoch" not in complete
    assert not any(pk.startswith("SERVING_DAY#") for pk, _ in fake.items)


def test_partial_frontier_is_fatal_and_is_not_repaired() -> None:
    fake = FakeDynamo()
    run = seed_granting_run(fake).record.run_id
    assert release_run(context(fake), run) is ReleaseOutcome.APPLIED
    shard_key = next(key for key in fake.items if key[0].startswith("SERVING_FRONTIER#"))
    fake.items.pop(shard_key)
    before = len(fake.transactions)

    with pytest.raises(GraphStoreUnavailableError, match="serving_frontier_partial"):
        release_store(fake).read_serving_domain(INGESTION_RELEASE_DOMAIN)

    assert len(fake.transactions) == before
    assert shard_key not in fake.items


def test_publication_settlement_records_an_immutable_cumulative_coordinate() -> None:
    fake = FakeDynamo()
    candidate = _candidate(fake)
    shard = serving_frontier_shard(candidate.run_id)
    marker_key = (f"SERVING_PUBLICATION#{candidate.run_id}", "MARKER")
    marker = fake.items[marker_key]
    frontier = fake.items[(f"SERVING_FRONTIER#{candidate.domain}#{shard:02d}", "COUNTER")]

    assert marker["state"] == write_text("SETTLED_ELIGIBLE")
    assert marker["eligible_coordinate"] == frontier["eligible_count"]
    assert marker["eligible_revision"] == frontier["revision"]


def test_activation_freezes_frontier_candidate_and_run_snapshot() -> None:
    fake = FakeDynamo()
    candidate = _candidate(fake)
    reserve_winner(fake, candidate.run_id)
    head = release_store(fake).read_serving_domain(candidate.domain).head

    assert head is not None and head.state == "ACTIVE"
    assert head.elected_frontier is not None
    assert head.elected_frontier_digest == head.elected_frontier.digest
    assert head.elected_candidate_digest is not None
    assert head.elected_run_ids == (candidate.run_id,)


def test_direct_drain_preserves_publication_marker_and_cumulative_frontier() -> None:
    fake = FakeDynamo()
    candidate = _candidate(fake)
    reserve_winner(fake, candidate.run_id)
    store = release_store(fake)
    head = store.read_serving_domain(candidate.domain).head
    assert head is not None
    binding = _binding(candidate)
    store.bind_serving(binding)
    marker_key = (f"SERVING_PUBLICATION#{candidate.run_id}", "MARKER")
    before_marker = dict(fake.items[marker_key])
    shard = serving_frontier_shard(candidate.run_id)
    frontier_key = (f"SERVING_FRONTIER#{candidate.domain}#{shard:02d}", "COUNTER")
    before_frontier = dict(fake.items[frontier_key])

    result = store.drain_direct_binding(ServingDirectDrainRequest(head, binding, candidate))

    assert result is ServingMutationResult.COMMITTED
    assert fake.items[marker_key] == before_marker
    assert fake.items[frontier_key] == before_frontier
    assert (due_shard(UNIT), f"ARM#{candidate.run_id}") not in fake.items
    assert (due_shard(UNIT), f"COMPLETE#{candidate.run_id}") not in fake.items


def test_fixed_member_final_transaction_counts_are_roster_independent() -> None:
    assert election_transaction_member_count() == 51
    assert election_transaction_member_count(successor=False) == 42


def test_head_snapshot_is_immutable_when_live_frontier_advances() -> None:
    fake = FakeDynamo()
    candidate = _candidate(fake)
    reserve_winner(fake, candidate.run_id)
    store = release_store(fake)
    before = store.read_serving_domain(candidate.domain).head
    assert before is not None and before.elected_frontier is not None
    shard = serving_frontier_shard(candidate.run_id)
    frontier = fake.items[(f"SERVING_FRONTIER#{candidate.domain}#{shard:02d}", "COUNTER")]
    frontier["revision"] = {"N": str(int(frontier["revision"]["N"]) + 1)}
    frontier["visible_count"] = {"N": str(int(frontier["visible_count"]["N"]) + 1)}
    after = store.read_serving_domain(candidate.domain).head
    assert after == before


def test_direct_request_has_no_clock_field() -> None:
    assert "recorded_at" not in ServingDirectDrainRequest.__dataclass_fields__


# --- The fixed-member final drain and successor activation (one transaction) ------------------


def _final_world(b_size: int = 2) -> DrainActivationWorld:
    return build_world(3, b_size)


def _domain_items(fake: FakeDynamo) -> dict[tuple[str, str], Any]:
    return copy.deepcopy(fake.items)


def _cohort_rows(fake: FakeDynamo) -> list[tuple[str, str]]:
    return sorted(key for key in fake.items if key[1].startswith("COALESCE#"))


def test_the_final_transaction_drains_a_and_activates_b_in_51_actions() -> None:
    world = _final_world()
    fake, final = world.fake, world.final_alias
    a_head = release_store(fake).read_serving_domain(INGESTION_RELEASE_DOMAIN).head
    assert a_head is not None and a_head.state == "BOUND_DRAINING"
    marker = copy.deepcopy(fake.items[(f"SERVING_PUBLICATION#{final}", "MARKER")])

    capture = capture_final_transaction(world)

    assert capture is not None and len(capture.request["TransactItems"]) == 51
    head = release_store(fake).read_serving_domain(INGESTION_RELEASE_DOMAIN).head
    assert head is not None and head.state == "ACTIVE"
    assert head.winner_run_id == world.b_runs[-1]
    assert set(head.elected_run_ids) == set(world.b_runs)
    assert not set(head.elected_run_ids) & set(world.a_runs)
    assert head.cohort_revision != a_head.cohort_revision
    transition = release_store(fake).read_serving_promoter_transition(
        INGESTION_RELEASE_DOMAIN, cast("str", a_head.cohort_revision)
    )
    assert transition is not None
    assert transition.aws_request_id == FINAL_REQUEST_ID
    assert transition.elected_cohort_revision == head.cohort_revision
    assert transition.drained_at == transition.elected_at == NOW.isoformat()
    # A is fully drained and retired; its final marker stays immutable.
    record = release_store(fake).read_run(final)
    assert record is not None and record.grant_id is not None
    alias = fake.items[(f"SERVING#{record.grant_id}", "BINDING")]
    assert alias["terminal_operation"] == write_text("alias_drain")
    assert fake.items[(f"SERVING_PUBLICATION#{final}", "MARKER")] == marker
    record_unit = record.conflict_unit
    for prefix in ("ARM#", "COMPLETE#"):
        assert (due_shard(record_unit), f"{prefix}{final}") not in fake.items
    assert _cohort_rows(fake) == [
        (due_shard(INGESTION_RELEASE_DOMAIN), f"COALESCE#{head.cohort_revision}")
    ]
    winner = release_store(fake).read_node(world.b_runs[-1], "bake")
    assert winner is not None and winner.state is NodeState.READY
    assert set(winner.serving_run_ids) == set(world.b_runs)


def test_the_final_transaction_without_a_successor_is_42_actions_and_leaves_idle() -> None:
    world = _final_world(0)
    a_head = release_store(world.fake).read_serving_domain(INGESTION_RELEASE_DOMAIN).head
    assert a_head is not None

    capture = capture_final_transaction(world)

    assert capture is not None and len(capture.request["TransactItems"]) == 42
    head = release_store(world.fake).read_serving_domain(INGESTION_RELEASE_DOMAIN).head
    assert head is not None and head.state == "IDLE"
    transition = release_store(world.fake).read_serving_promoter_transition(
        INGESTION_RELEASE_DOMAIN, cast("str", a_head.cohort_revision)
    )
    assert transition is not None and transition.elected_cohort_revision is None
    assert _cohort_rows(world.fake) == []


@pytest.mark.parametrize("failure", ["conflict", "timeout_before_commit"])
def test_a_failed_final_transaction_keeps_the_alias_and_obligation_then_retries_fresh(
    failure: str,
) -> None:
    world = _final_world()
    fake = world.fake
    before = _domain_items(fake)
    committed = len(fake.transactions)
    if failure == "conflict":
        fake.conflicts_next = 1
        expected: type[Exception] = GraphTransactionConflictError
    else:
        fake.fail_next = True
        expected = GraphStoreUnavailableError

    with pytest.raises(expected):
        capture_final_transaction(world)

    assert fake.items == before
    assert len(fake.transactions) == committed
    retried = capture_final_transaction(world, request_id="fixture-next-invocation")
    assert retried is not None and len(retried.request["TransactItems"]) == 51
    head = release_store(fake).read_serving_domain(INGESTION_RELEASE_DOMAIN).head
    assert head is not None and head.winner_run_id == world.b_runs[-1]


def test_a_lost_response_resolves_by_equal_replay_and_refuses_another_request_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _final_world()
    fake = world.fake
    requests: list[DrainFinalAliasAndActivateNextRequest] = []
    original = DynamoGraphReleaseStore.drain_final_alias_and_activate_next

    def spy(
        self: DynamoGraphReleaseStore, request: DrainFinalAliasAndActivateNextRequest
    ) -> DrainFinalAliasAndActivateNextResult:
        requests.append(request)
        return original(self, request)

    monkeypatch.setattr(DynamoGraphReleaseStore, "drain_final_alias_and_activate_next", spy)
    fake.ambiguous_next = True
    with pytest.raises(GraphStoreUnavailableError):
        capture_final_transaction(world)
    (request,) = requests
    committed = len(fake.transactions)
    after = _domain_items(fake)

    replay = release_store(fake).drain_final_alias_and_activate_next(request)

    assert replay.mutation is ServingMutationResult.REPLAY
    assert replay.outcome is ServingElectionOutcome.ELECTED
    assert replay.transition.aws_request_id == FINAL_REQUEST_ID
    assert replay.activated_head is not None
    assert replay.activated_head.winner_run_id == world.b_runs[-1]
    other = replace(request, aws_request_id="another-invocation")
    with pytest.raises(GraphStoreUnavailableError, match="request id"):
        release_store(fake).drain_final_alias_and_activate_next(other)
    assert len(fake.transactions) == committed
    assert fake.items == after


def _lost_final_request(
    world: DrainActivationWorld, monkeypatch: pytest.MonkeyPatch
) -> DrainFinalAliasAndActivateNextRequest:
    """Commit A's final transaction behind a lost response and return its exact request."""
    requests: list[DrainFinalAliasAndActivateNextRequest] = []
    original = DynamoGraphReleaseStore.drain_final_alias_and_activate_next

    def spy(
        self: DynamoGraphReleaseStore, request: DrainFinalAliasAndActivateNextRequest
    ) -> DrainFinalAliasAndActivateNextResult:
        requests.append(request)
        return original(self, request)

    monkeypatch.setattr(DynamoGraphReleaseStore, "drain_final_alias_and_activate_next", spy)
    world.fake.ambiguous_next = True
    with pytest.raises(GraphStoreUnavailableError):
        capture_final_transaction(world)
    monkeypatch.setattr(DynamoGraphReleaseStore, "drain_final_alias_and_activate_next", original)
    (request,) = requests
    return request


def _refused_replay(
    fake: FakeDynamo, request: DrainFinalAliasAndActivateNextRequest, fact: str
) -> None:
    committed = len(fake.transactions)
    before = _domain_items(fake)

    with pytest.raises(GraphStoreUnavailableError, match=f"replay changed {fact}"):
        release_store(fake).drain_final_alias_and_activate_next(request)

    assert len(fake.transactions) == committed
    assert fake.items == before


@pytest.mark.parametrize(
    ("change", "fact"),
    [
        ("terminal_at", "terminal time"),
        ("no_eligible", "outcome"),
        ("cohort_revision", "successor cohort revision"),
        ("input_revision", "resulting head"),
        ("candidate_digest", "resulting head"),
        ("live_frontier", "resulting head"),
        ("winner", "resulting head"),
        ("plan_digest", "successor winner"),
    ],
)
def test_a_same_id_replay_with_changed_elected_facts_is_refused_without_writes(
    monkeypatch: pytest.MonkeyPatch, change: str, fact: str
) -> None:
    world = _final_world()
    request = _lost_final_request(world, monkeypatch)
    frontier = request.live_frontier
    bumped = replace(frontier, revisions=(frontier.revisions[0] + 1, *frontier.revisions[1:]))
    changed = {
        "terminal_at": replace(request, terminal_at="2026-10-15T00:00:01+00:00"),
        "no_eligible": replace(
            request,
            successor_candidates=(),
            successor_winner=None,
            successor_cohort_revision=None,
            successor_input_revision=None,
            successor_plan_digest=None,
        ),
        "cohort_revision": replace(request, successor_cohort_revision="cohort-other"),
        "input_revision": replace(request, successor_input_revision="input-other"),
        "candidate_digest": replace(request, successor_candidate_digest="digest-other"),
        "live_frontier": replace(request, live_frontier=bumped),
        "winner": replace(request, successor_winner=request.successor_candidates[0]),
        "plan_digest": replace(request, successor_plan_digest="plan-other"),
    }[change]
    assert request.successor_winner != request.successor_candidates[0]

    _refused_replay(world.fake, changed, fact)


def test_a_same_id_replay_refuses_a_changed_resulting_head_without_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _final_world()
    request = _lost_final_request(world, monkeypatch)
    head_key = next(
        key for key in world.fake.items if key[0].startswith("SERVING_DOMAIN#") and key[1] == "HEAD"
    )
    world.fake.items[head_key]["elected_candidate_digest"] = write_text("digest-rewritten")

    _refused_replay(world.fake, request, "resulting head")


@pytest.mark.parametrize("drift", ["BOUND_DRAINING", "IDLE", "version"])
def test_a_same_id_elected_replay_refuses_a_later_head_state_or_version_without_writes(
    monkeypatch: pytest.MonkeyPatch, drift: str
) -> None:
    world = _final_world()
    request = _lost_final_request(world, monkeypatch)
    head_key = next(
        key for key in world.fake.items if key[0].startswith("SERVING_DOMAIN#") and key[1] == "HEAD"
    )
    head = world.fake.items[head_key]
    assert head["state"] == write_text("ACTIVE")
    assert head["version"] == {"N": str(request.head.version + 1)}
    if drift == "version":
        head["version"] = {"N": str(request.head.version + 2)}
    else:
        head["state"] = write_text(drift)

    _refused_replay(world.fake, request, "resulting head")


def test_a_same_id_no_eligible_replay_proves_the_idle_head_and_refuses_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _final_world(0)
    fake = world.fake
    request = _lost_final_request(world, monkeypatch)
    committed = len(fake.transactions)

    replay = release_store(fake).drain_final_alias_and_activate_next(request)

    assert replay.mutation is ServingMutationResult.REPLAY
    assert replay.outcome is ServingElectionOutcome.NO_ELIGIBLE
    assert len(fake.transactions) == committed
    _refused_replay(
        fake, replace(request, terminal_at="2026-10-15T00:00:01+00:00"), "terminal time"
    )
    elected = replace(
        request,
        successor_winner=request.final_candidate,
        successor_cohort_revision="cohort-other",
    )
    _refused_replay(fake, elected, "outcome")
    head_key = next(
        key for key in fake.items if key[0].startswith("SERVING_DOMAIN#") and key[1] == "HEAD"
    )
    fake.items[head_key]["version"] = {"N": str(request.head.version + 2)}
    _refused_replay(fake, request, "resulting head")


def test_a_fixed_member_count_drift_refuses_before_any_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _final_world()
    fake = world.fake
    original = DynamoGraphReleaseStore._selected_winner_members  # pyright: ignore[reportPrivateUsage]

    def one_more(
        self: DynamoGraphReleaseStore, request: DrainFinalAliasAndActivateNextRequest
    ) -> list[Member]:
        members = original(self, request)
        return [*members, members[0]]

    monkeypatch.setattr(DynamoGraphReleaseStore, "_selected_winner_members", one_more)
    before = _domain_items(fake)
    committed = len(fake.transactions)

    with pytest.raises(GraphStoreUnavailableError, match="52 actions, expected 51"):
        capture_final_transaction(world)

    assert len(fake.transactions) == committed
    assert fake.items == before


# --- ADR-140 S1: the unarmed settlement's one transaction ------------------------------------


class _Capture(DynamoGraphReleaseStore):
    """The real adapter, except the unarmed settlement is captured instead of committed."""

    captured: list[UnarmedPrecomputeSettlement]

    def settle_unarmed_precompute(
        self, settlement: UnarmedPrecomputeSettlement
    ) -> ServingMutationResult:
        self.captured.append(settlement)
        return ServingMutationResult.COMMITTED


def _settlement(fake: FakeDynamo) -> tuple[str, UnarmedPrecomputeSettlement]:
    """Return the exact settlement the Release builds for the live U1 wedge, uncommitted."""
    run = unarmed_winner(fake, "u1")
    capture = _Capture(TABLE, cast("Any", fake))
    capture.captured = []
    assert settle_unarmed_cohort(replace(context(fake), store=capture), run) is not None
    (settlement,) = capture.captured
    return run, settlement


@pytest.mark.covers("ADR-140")
def test_adr140_s1_the_settlement_is_exactly_six_members_and_writes_the_consumer_node_shape() -> (
    None
):
    fake = FakeDynamo()
    run, settlement = _settlement(fake)
    node_before = copy.deepcopy(fake.items[(f"RUN#{run}", "NODE#precompute")])
    store = release_store(fake)

    assert store.settle_unarmed_precompute(settlement) is ServingMutationResult.COMMITTED

    members = fake.transactions[-1]
    assert [next(iter(member)) for member in members] == [
        "ConditionCheck",
        "ConditionCheck",
        "ConditionCheck",
        "Put",
        "Update",
        "Put",
    ]
    node = fake.items[(f"RUN#{run}", "NODE#precompute")]
    changed = {name for name in node if node.get(name) != node_before.get(name)}
    # The consumer's `settle_node` terminal attributes, with no failure_reason (NIT 4).
    assert changed == {"state", "revision", "settled_at", "effect_digest", "effect_record"}
    assert node["state"]["S"] == "SUCCEEDED"
    assert int(node["revision"]["N"]) == int(node_before["revision"]["N"]) + 1
    assert (node["effect_record"]["S"], node["effect_digest"]["S"]) == (
        settlement.effect_record,
        settlement.effect_digest,
    )
    grant = settlement.binding.grant_id
    assert store.read_serving_binding(grant) == settlement.binding
    assert settlement.precompute in store.read_contributions(grant)


@pytest.mark.covers("ADR-140")
def test_adr140_s1_a_replay_and_a_lost_response_are_replay_and_a_failed_pre_image_retries() -> None:
    fake = FakeDynamo()
    _run, settlement = _settlement(fake)
    store = release_store(fake)
    fake.ambiguous_next = True  # commits, then the response is lost

    assert store.settle_unarmed_precompute(settlement) is ServingMutationResult.REPLAY
    written = len(fake.transactions)
    assert store.settle_unarmed_precompute(settlement) is ServingMutationResult.REPLAY
    assert len(fake.transactions) == written  # an exact replay writes nothing
    # A failure that wrote nothing is the pre-image: the one retry commits.
    retried = FakeDynamo()
    _run, again = _settlement(retried)
    retried.fail_next = True
    assert release_store(retried).settle_unarmed_precompute(again) is (
        ServingMutationResult.COMMITTED
    )


def _move_run(fake: FakeDynamo, run: str, grant: str) -> None:
    del grant
    item = fake.items[(f"RUN#{run}", "RUN")]
    item["revision"] = write_int(int(item["revision"]["N"]) + 1)


def _move_head(fake: FakeDynamo, run: str, grant: str) -> None:
    del run, grant
    item = fake.items[(f"SERVING_DOMAIN#{INGESTION_RELEASE_DOMAIN}", "HEAD")]
    item["version"] = write_int(int(item["version"]["N"]) + 1)


def _move_bake(fake: FakeDynamo, run: str, grant: str) -> None:
    del run
    fake.items[(f"COHORT#{grant}", "CONTRIBUTION#bake")]["settled_at"] = write_text(
        "2026-10-16T00:00:00+00:00"
    )


def _put_precompute(fake: FakeDynamo, run: str, grant: str) -> None:
    del run
    release_store(fake).record_contribution(
        CohortContribution(
            grant, CohortMember.PRECOMPUTE, CohortOutcome.NO_ELIGIBLE, None, NOW, "other-root"
        )
    )


def _move_node(fake: FakeDynamo, run: str, grant: str) -> None:
    del grant
    item = fake.items[(f"RUN#{run}", "NODE#precompute")]
    item["revision"] = write_int(int(item["revision"]["N"]) + 1)


@pytest.mark.covers("ADR-140")
@pytest.mark.parametrize("move", [_move_run, _move_head, _move_bake, _put_precompute, _move_node])
def test_adr140_s1_each_moved_condition_refuses_with_zero_writes(move: Any) -> None:
    fake = FakeDynamo()
    run, settlement = _settlement(fake)
    move(fake, run, settlement.binding.grant_id)
    before = copy.deepcopy(fake.items)

    with pytest.raises(GraphGuardError):
        release_store(fake).settle_unarmed_precompute(settlement)

    assert fake.items == before


@pytest.mark.covers("ADR-140")
def test_adr140_s1_a_mixed_image_after_a_lost_response_is_graph_precompute_6() -> None:
    fake = FakeDynamo()
    run, settlement = _settlement(fake)

    def half_written(table: FakeDynamo) -> None:
        _move_node(table, run, settlement.binding.grant_id)
        table.fail_next = True

    fake.before_transaction = half_written

    with pytest.raises(InvariantError) as mixed:
        release_store(fake).settle_unarmed_precompute(settlement)

    assert str(mixed.value.code) == "ADLY-GRAPH_PRECOMPUTE-0006"


# --- H7 (ADR-140, B4): the completion deletes each FAILED task's recovery obligation ---------


def _completion_members(fake: FakeDynamo, run: str, *, failed_tasks: int) -> list[Any]:
    """Settle a U2 winner, add `failed_tasks` FAILED roster tasks, and capture its completion."""
    from adapters.jobs.execution_graph.release import try_complete_granted  # noqa: PLC0415

    assert settle_unarmed_cohort(context(fake), run) is not None
    template = fake.items[(f"RUN#{run}", "NODE#precompute")]
    for index in range(failed_tasks):
        name = f"bake_task:campaigns#{index + 1}d"
        node_id = f"fictional-task-{index}"
        fake.items[(f"RUN#{run}", f"NODE#{name}")] = {
            **copy.deepcopy(template),
            "sk": write_text(f"NODE#{name}"),
            "node": write_text(name),
            "node_id": write_text(node_id),
            "state": write_text("FAILED"),
        }
        fake.items[(due_shard(UNIT), f"BAKE_RECOVERY#{node_id}")] = {
            "pk": write_text(due_shard(UNIT)),
            "sk": write_text(f"BAKE_RECOVERY#{node_id}"),
            "item_kind": write_text("bake_recovery"),
        }
    since = len(fake.transactions)
    assert try_complete_granted(context(fake), run) is ServingMutationResult.COMMITTED
    (members,) = fake.transactions[since:]
    return members


def _recovery_deletes(members: list[Any]) -> list[str]:
    return sorted(
        str(member["Delete"]["Key"]["sk"]["S"])
        for member in members
        if "Delete" in member
        and str(member["Delete"]["Key"]["sk"]["S"]).startswith("BAKE_RECOVERY#")
    )


@pytest.mark.covers("ADR-140")
def test_h7_completion_deletes_exactly_the_failed_tasks_recovery_obligations() -> None:
    fake = FakeDynamo()
    run = unarmed_winner(fake, "u2")

    members = _completion_members(fake, run, failed_tasks=20)

    assert _recovery_deletes(members) == sorted(
        f"BAKE_RECOVERY#fictional-task-{index}" for index in range(20)
    )
    assert len(members) <= 100
    assert not any(sk.startswith("BAKE_RECOVERY#") for _pk, sk in fake.items)


@pytest.mark.covers("ADR-140")
def test_h7_control_an_all_succeeded_completion_has_no_recovery_member() -> None:
    fake = FakeDynamo()
    run = unarmed_winner(fake, "u2")

    members = _completion_members(fake, run, failed_tasks=0)

    assert _recovery_deletes(members) == []
