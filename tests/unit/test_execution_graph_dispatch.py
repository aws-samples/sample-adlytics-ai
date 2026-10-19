# SPDX-License-Identifier: MIT-0
"""S3 dispatch, claim, settlement and slot release (BF-2, V-9, V-10, V-11, V-25; R2-7).

Each test drives the real dispatch adapter over the transactional fake, so every refusal is the
exact typed signal the adapter's own conditions produce, and every "no effect" is read back from
the stored items. Fictional data only.
"""

from __future__ import annotations

import copy
import random
from dataclasses import replace
from datetime import timedelta
from typing import Any, cast

import pytest
from execution_graph_dispatch_support import (
    NOW,
    SHARD,
    TEMPLATE,
    UNIT,
    SpySender,
    instantiate,
    kinds,
    node,
    now_text,
    seed_run,
    set_mode,
    store,
)
from execution_graph_release_support import granted_run
from execution_graph_support import FakeDynamo

from adapters.inbound.ingestion_handlers.graph_claim import (
    MAX_PARALLEL_CLAIMS,
    ClaimRetryExhaustedError,
    GraphClaimInput,
    claim_graph_envelope,
    release_claim_lease,
)
from adapters.jobs.execution_graph.commands import (
    OperatorCommand,
    RunCommandKind,
    run_operator_command,
)
from adapters.jobs.execution_graph.dispatcher import (
    UNROUTED_GRAPH_NODE,
    DispatchContext,
    RoutedGraphSender,
    dispatch_shard,
)
from adapters.jobs.execution_graph.fence_settler import settle_shard, try_terminalize
from adapters.jobs.execution_graph.instantiate import InstantiationOutcome
from adapters.jobs.execution_graph.node_verifier import (
    POST_GRANT_TAKEOVER_UNREACHABLE,
    UnreachablePostGrantFacts,
    VerifierContext,
    verify_shard,
)
from adapters.outbound.dynamodb.execution_graph import (
    DynamoExecutionGraphStore,
    build_admission_transaction,
)
from adapters.outbound.dynamodb.execution_graph_dispatch import build_fence_run, build_terminal
from adapters.outbound.sqs.graph_dispatch import SqsGraphSender, SqsStandardGraphSender
from domain.operations.execution_graph import AuthorityMode
from domain.operations.execution_graph_dispatch import (
    CLAIM_LEASE_LIVE,
    CLAIM_LEASE_STALE,
    PLAN_DISPATCH_FENCED,
    SLOT_NOT_OWNED,
    AttemptFact,
    CompleteObligation,
    FenceReason,
    GraphGuardError,
    NodePhase,
    NodeState,
    ServingAuthorityIdentity,
    ServingCandidate,
    ServingFrontierVector,
    election_transaction_member_count,
    newest_eligible_candidate,
    require_serving_authority_identity,
    serving_frontier_shard,
    serving_gap,
    validate_serving_candidates,
)
from domain.operations.generated_execution_graphs import ROUTES
from domain.operations.run_request import ACTIVE_COMPUTE_NOT_REPLACEABLE, RunState
from domain.shared_kernel.errors import InvariantError
from ports.outbound.execution_graph import (
    AdmissionCommit,
    AdmissionConflictError,
    EffectKind,
    NewRun,
    ReceiptRecord,
    ReplacedRun,
)
from ports.outbound.execution_graph_dispatch import (
    ClaimRecord,
    FenceRunRequest,
    GraphMessage,
    NodeRecord,
    OutboxRecord,
    RunRecord,
)

pytestmark = pytest.mark.unit

OPERATOR = "arn:aws:iam::000000000000:user/operator"


def _member_key(member: object) -> dict[str, Any]:
    body = cast("dict[str, dict[str, Any]]", member)
    (inner,) = body.values()
    return cast("dict[str, Any]", inner.get("Key") or inner["Item"])


def _member_pk(member: object) -> str:
    return str(_member_key(member)["pk"]["S"])


def _member_sk(member: object) -> str:
    return str(_member_key(member)["sk"]["S"])


def _dispatch(fake: FakeDynamo, sender: SpySender) -> Any:
    return dispatch_shard(DispatchContext(store(fake), sender, now_text), SHARD)


def _command(kind: RunCommandKind, run: str, command_id: str) -> OperatorCommand:
    return OperatorCommand(kind, command_id, OPERATOR, now_text, run_id=run)


def _armed(fake: FakeDynamo, *names: str) -> str:
    set_mode(fake, AuthorityMode.GRAPH)
    seeded = seed_run(fake)
    assert instantiate(fake, seeded.record.run_id, *names) is InstantiationOutcome.ARMED
    return seeded.record.run_id


def _claim_input(fake: FakeDynamo, run: str, name: str, **overrides: Any) -> GraphClaimInput:
    item = node(fake, run, name)
    base = GraphClaimInput(
        run_id=run,
        node_id=item.node_id,
        dispatch_id=item.dispatch_id,
        body=item.body,
        now=lambda: NOW,
        lease=timedelta(minutes=16),
    )
    return replace(base, **overrides)


class _NoFacts:
    """Attempt facts for a node whose table attempt never began (S6 owns real attempts)."""

    def __init__(self, fact: AttemptFact = AttemptFact.NONE) -> None:
        self.fact = fact

    def attempt(
        self, node: NodeRecord, claim: ClaimRecord | None
    ) -> tuple[AttemptFact, str | None]:
        del node, claim
        return self.fact, None if self.fact in {AttemptFact.NONE, AttemptFact.OPEN} else "e" * 64


# --- BF-2: READY/outbox atomicity and the send permit ---------------------------------------------


def test_bf2_arming_commits_ready_token_lock_and_outbox_in_one_transaction() -> None:
    fake = FakeDynamo()
    run = _armed(fake, "write_a")
    armed = node(fake, run, "write_a")
    arming = fake.transactions[-1]
    kinds_in = sorted(next(iter(member)) for member in arming)
    # The RUN# member is an Update: arming bumps the run revision (S3V-6).
    assert kinds_in == ["Delete", "Put", "Put", "Put", "Update", "Update"]
    assert armed.state is NodeState.READY
    assert armed.armed_generation == 0
    assert kinds(fake, "TOKEN#") and kinds(fake, "LOCK#")
    assert kinds(fake, "OUTBOX#") == [(SHARD, f"OUTBOX#{armed.node_id}")]
    assert kinds(fake, "INSTANTIATE#") == []


def test_bf2_an_injected_failure_in_the_arming_transaction_commits_none_of_it() -> None:
    fake = FakeDynamo()
    set_mode(fake, AuthorityMode.GRAPH)
    seeded = seed_run(fake)
    run = seeded.record.run_id
    fake.fail_next = False
    # Let the node put commit, then fail the arming transaction.
    original = fake.transact_write_items
    calls = {"n": 0}

    def failing(**kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 2:
            fake.fail_next = True
        return original(**kwargs)

    fake.transact_write_items = failing  # type: ignore[method-assign]
    with pytest.raises(Exception, match="graph transaction failed"):
        instantiate(fake, run, "write_a")
    assert node(fake, run, "write_a").state is NodeState.PENDING
    assert kinds(fake, "TOKEN#") == kinds(fake, "LOCK#") == kinds(fake, "OUTBOX#") == []
    assert kinds(fake, "INSTANTIATE#") != []


def test_bf2_negative_a_pause_before_the_permit_refuses_it_and_the_sender_is_never_called() -> None:
    fake = FakeDynamo()
    run = _armed(fake, "write_a")
    paused = run_operator_command(store(fake), _command(RunCommandKind.PAUSE, run, "pause-0001"))
    assert paused.committed
    sender = SpySender()
    counts = _dispatch(fake, sender)
    assert sender.calls == []
    assert counts.fenced == 1
    assert counts.permits == 0
    item = node(fake, run, "write_a")
    assert item.state is NodeState.READY
    assert item.permit_generation is None
    with pytest.raises(GraphGuardError) as refused:
        outbox = store(fake).read_outbox(SHARD, item.node_id)
        assert outbox is not None
        current = store(fake).read_run(run)
        assert current is not None
        store(fake).acquire_permit(current, item, outbox)
    assert refused.value.signal == PLAN_DISPATCH_FENCED


def test_bf2_positive_a_permit_before_the_pause_finishes_its_send_and_resume_sends_once() -> None:
    fake = FakeDynamo()
    run = _armed(fake, "write_a", "write_b")
    dispatch = store(fake)
    first = node(fake, run, "write_a")
    outbox = dispatch.read_outbox(SHARD, first.node_id)
    current = dispatch.read_run(run)
    assert outbox is not None and current is not None
    dispatch.acquire_permit(current, first, outbox)
    run_operator_command(dispatch, _command(RunCommandKind.PAUSE, run, "pause-0001"))
    sender = SpySender()
    counts = _dispatch(fake, sender)
    # The permitted node's send finishes (the SENDING row is re-sent with its exact bytes);
    # the other READY node is fenced by the advanced generation.
    assert [call[2] for call in sender.calls] == [first.dispatch_id]
    assert counts.fenced == 1
    assert node(fake, run, "write_a").state is NodeState.DISPATCHED
    assert node(fake, run, "write_b").state is NodeState.READY
    resumed = run_operator_command(dispatch, _command(RunCommandKind.RESUME, run, "resume-001"))
    assert resumed.committed
    _dispatch(fake, sender)
    _dispatch(fake, sender)
    second = node(fake, run, "write_b")
    assert [call[2] for call in sender.calls] == [first.dispatch_id, second.dispatch_id]
    assert second.state is NodeState.DISPATCHED
    assert kinds(fake, "OUTBOX#") == []
    resumed_run = dispatch.read_run(run)
    assert resumed_run is not None and resumed_run.dispatch_generation == 2


def test_bf2_the_dispatcher_sends_the_exact_persisted_bytes_with_unit_key_group() -> None:
    fake = FakeDynamo()
    run = _armed(fake, "write_a")
    sender = SpySender()
    _dispatch(fake, sender)
    item = node(fake, run, "write_a")
    assert sender.calls == [(item.body, item.group_id, item.dispatch_id)]
    assert item.group_id == item.lock_key


def test_a_crash_after_the_permit_resends_the_same_bytes_and_deduplication_identity() -> None:
    fake = FakeDynamo()
    run = _armed(fake, "write_a")
    item = node(fake, run, "write_a")
    dispatch = store(fake)
    outbox = dispatch.read_outbox(SHARD, item.node_id)
    current = dispatch.read_run(run)
    assert outbox is not None and current is not None
    dispatch.acquire_permit(current, item, outbox)
    sender = SpySender()
    _dispatch(fake, sender)
    assert sender.calls == [(item.body, item.group_id, item.dispatch_id)]
    assert node(fake, run, "write_a").state is NodeState.DISPATCHED


def test_a_provider_oversize_rejection_fails_the_node_and_fences_the_run() -> None:
    fake = FakeDynamo()
    run = _armed(fake, "write_a")
    counts = _dispatch(fake, SpySender(oversize=True))
    assert counts.oversize == 1
    assert node(fake, run, "write_a").state is NodeState.FAILED
    fenced = store(fake).read_run(run)
    assert fenced is not None
    assert (fenced.state, fenced.fence_reason) == (RunState.FENCING, FenceReason.OVERSIZE)
    assert "active_run_id" not in fake.items[(f"SLOT#{UNIT}", "SLOT")]
    assert kinds(fake, "LOCK#") == kinds(fake, "TOKEN#") == []
    assert try_terminalize(store(fake), run) is RunState.FAILED


# --- V-9: claim refusal and takeover --------------------------------------------------------------


def _replace_before_compute(fake: FakeDynamo, run: str) -> None:
    current = store(fake).read_run(run)
    assert current is not None
    store(fake).fence_run(FenceRunRequest(current, FenceReason.CANCELLED, now_text()))


def test_v9_a_claim_after_the_run_fenced_settles_superseded_unclaimed_and_acknowledges() -> None:
    fake = FakeDynamo()
    run = _armed(fake, "write_a")
    _dispatch(fake, SpySender())
    _replace_before_compute(fake, run)
    before = {key: value for key, value in fake.items.items() if not key[0].startswith("RUN#")}
    disposition = claim_graph_envelope(store(fake), _NoFacts(), _claim_input(fake, run, "write_a"))
    assert disposition.acknowledge
    assert disposition.signal == "EXECUTION_SUPERSEDED"
    item = node(fake, run, "write_a")
    assert item.state is NodeState.SUPERSEDED_UNCLAIMED
    assert not item.claim_record_present
    assert kinds(fake, "LOCK#") == kinds(fake, "TOKEN#") == kinds(fake, "EXEC_CLAIM#") == []
    after = {key: value for key, value in fake.items.items() if not key[0].startswith("RUN#")}
    removed = set(before) - set(after)
    # Zero semantic, lifecycle or product effect: only the lock, the token and (the last node
    # having settled) the run's FENCE# obligation are gone.
    assert all(
        key[0].startswith(("LOCK#", "TOKEN#")) or key[1].startswith("FENCE#") for key in removed
    )
    current = store(fake).read_run(run)
    assert current is not None and current.compute_started_at is None
    assert current.state is RunState.CANCELLED


def test_v9_positive_a_claim_on_an_open_run_sets_compute_started_once() -> None:
    fake = FakeDynamo()
    run = _armed(fake, "write_a", "write_b")
    _dispatch(fake, SpySender())
    first = claim_graph_envelope(store(fake), _NoFacts(), _claim_input(fake, run, "write_a"))
    started = store(fake).read_run(run)
    assert first.claim is not None and started is not None
    assert started.compute_started_at == NOW.isoformat()
    later = replace(_claim_input(fake, run, "write_b"), now=lambda: NOW + timedelta(seconds=5))
    second = claim_graph_envelope(store(fake), _NoFacts(), later)
    again = store(fake).read_run(run)
    assert second.claim is not None and again is not None
    assert again.compute_started_at == NOW.isoformat()
    # A graph claim transaction holds no mode-item member (V-13, r3).
    claim_members = fake.transactions[-1]
    assert all(_member_pk(member) != "MODE#ingestion" for member in claim_members)


def test_v9_a_live_claim_leaves_the_duplicate_unacknowledged() -> None:
    fake = FakeDynamo()
    run = _armed(fake, "write_a")
    _dispatch(fake, SpySender())
    claim_graph_envelope(store(fake), _NoFacts(), _claim_input(fake, run, "write_a"))
    duplicate = claim_graph_envelope(store(fake), _NoFacts(), _claim_input(fake, run, "write_a"))
    assert not duplicate.acknowledge
    assert duplicate.signal == "CLAIM_LEASE_LIVE"


def _expired_claim(fake: FakeDynamo, run: str) -> None:
    _dispatch(fake, SpySender())
    claim_graph_envelope(
        store(fake), _NoFacts(), _claim_input(fake, run, "write_a", lease=timedelta(seconds=1))
    )
    _replace_before_compute_after_compute(fake, run)


def _replace_before_compute_after_compute(fake: FakeDynamo, run: str) -> None:
    current = store(fake).read_run(run)
    assert current is not None
    store(fake).fence_run(FenceRunRequest(current, FenceReason.INVALIDATED, now_text()))


def test_v9_takeover_with_no_attempt_settles_superseded_unclaimed_with_the_claim_present() -> None:
    fake = FakeDynamo()
    run = _armed(fake, "write_a")
    _expired_claim(fake, run)
    later = _claim_input(fake, run, "write_a", now=lambda: NOW + timedelta(minutes=1))
    disposition = claim_graph_envelope(store(fake), _NoFacts(), later)
    assert disposition.acknowledge
    item = node(fake, run, "write_a")
    assert item.state is NodeState.SUPERSEDED_UNCLAIMED
    assert item.claim_record_present
    claim = store(fake).read_claim(item.node_id)
    assert claim is not None and claim.lease_version == 2
    assert kinds(fake, "LOCK#") == []


def test_v9_takeover_with_an_open_attempt_parks_under_verify_then_the_verifier_settles() -> None:
    fake = FakeDynamo()
    run = _armed(fake, "write_a")
    _expired_claim(fake, run)
    later = _claim_input(fake, run, "write_a", now=lambda: NOW + timedelta(minutes=1))
    parked = claim_graph_envelope(store(fake), _NoFacts(AttemptFact.OPEN), later)
    assert parked.acknowledge
    item = node(fake, run, "write_a")
    assert item.state is NodeState.AWAITING_PROVIDER_TERMINAL
    assert kinds(fake, "VERIFY#") == [(SHARD, f"VERIFY#{item.node_id}")]
    assert kinds(fake, "LOCK#") != []
    claim = store(fake).read_claim(item.node_id)
    assert claim is not None and claim.lease_version == 2
    # Still open: the verifier waits; the lock is held.
    waiting = verify_shard(
        VerifierContext(store(fake), _NoFacts(AttemptFact.OPEN), now_text), SHARD
    )
    assert waiting.waiting == 1
    # The provider terminal arrives through fence recovery, with no redelivery of the message.
    settled = verify_shard(
        VerifierContext(store(fake), _NoFacts(AttemptFact.SUCCEEDED), now_text), SHARD
    )
    assert settled.settled == 1
    final = node(fake, run, "write_a")
    assert final.state is NodeState.SUCCEEDED
    assert final.effect_digest == "e" * 64
    assert kinds(fake, "VERIFY#") == kinds(fake, "LOCK#") == kinds(fake, "TOKEN#") == []
    terminal = store(fake).read_run(run)
    assert terminal is not None and terminal.state is RunState.SUPERSEDED


# --- V-10: one CAS record decides compute start ---------------------------------------------------


def _replace_commit(fake: FakeDynamo, run: str) -> AdmissionCommit:
    current = store(fake).read_run(run)
    assert current is not None
    receipt = ReceiptRecord(
        request_id="request-0002",
        request_digest="e" * 64,
        decision="REPLACED_BEFORE_COMPUTE",
        reason=None,
        row=17,
        authority_epoch=1,
        mode=AuthorityMode.GRAPH,
        conflict_unit=UNIT,
        input_revision=NOW.isoformat(),
        fingerprint="9" * 64,
        recorded_at=now_text(),
        protocol_version="v1",
        contract_revision=1,
    )
    return AdmissionCommit(
        receipt,
        EffectKind.REPLACE_RUN,
        UNIT,
        1,
        new_run=NewRun("new-run", "new-graph", "9" * 64, NOW.isoformat(), "root", 0, "t"),
        replaced=ReplacedRun(run, invalidating=False),
    )


def _replace(fake: FakeDynamo, run: str) -> str | None:
    commit = _replace_commit(fake, run)
    try:
        DynamoExecutionGraphStore("execution-graph-probe-fixture", fake).commit_admission(  # type: ignore[arg-type]
            commit
        )
    except AdmissionConflictError as conflict:
        return conflict.signal
    return None


def test_v10_claim_first_then_replace_is_refused_active_compute_not_replaceable() -> None:
    fake = FakeDynamo()
    run = _armed(fake, "write_a")
    _dispatch(fake, SpySender())
    claim_graph_envelope(store(fake), _NoFacts(), _claim_input(fake, run, "write_a"))
    assert _replace(fake, run) == ACTIVE_COMPUTE_NOT_REPLACEABLE


def test_v10_replace_first_then_claim_is_execution_superseded() -> None:
    fake = FakeDynamo()
    run = _armed(fake, "write_a")
    _dispatch(fake, SpySender())
    assert _replace(fake, run) is None
    replaced = store(fake).read_run(run)
    assert replaced is not None
    assert (replaced.state, replaced.fence_reason) == (RunState.FENCING, FenceReason.REPLACED)
    assert kinds(fake, "FENCE#") == [(SHARD, f"FENCE#{run}")]
    disposition = claim_graph_envelope(store(fake), _NoFacts(), _claim_input(fake, run, "write_a"))
    assert disposition.signal == "EXECUTION_SUPERSEDED"


@pytest.mark.parametrize("seed", range(100))
def test_v10_never_both_compute_started_and_replaced_before_compute(seed: int) -> None:
    fake = FakeDynamo()
    run = _armed(fake, "write_a")
    _dispatch(fake, SpySender())
    claim_first = random.Random(seed).random() < 0.5  # noqa: S311 -- seeded interleaving
    if claim_first:
        claim_graph_envelope(store(fake), _NoFacts(), _claim_input(fake, run, "write_a"))
        signal = _replace(fake, run)
    else:
        signal = _replace(fake, run)
        claim_graph_envelope(store(fake), _NoFacts(), _claim_input(fake, run, "write_a"))
    current = store(fake).read_run(run)
    assert current is not None
    started = current.compute_started_at is not None
    replaced = current.fence_reason is FenceReason.REPLACED
    assert started != replaced
    assert (signal == ACTIVE_COMPUTE_NOT_REPLACEABLE) == claim_first


# --- R2-7: RUN# transaction conflicts under parallel claims ---------------------------------------


def test_parallel_claims_retry_run_conflicts_within_the_bound_and_count_them() -> None:
    fake = FakeDynamo()
    run = _armed(fake, "write_a")
    _dispatch(fake, SpySender())
    fake.conflicts_next = 3
    disposition = claim_graph_envelope(store(fake), _NoFacts(), _claim_input(fake, run, "write_a"))
    assert disposition.claim is not None
    assert disposition.conflicts == 3


def test_a_claim_that_exhausts_its_run_conflict_retry_is_the_stop_rule() -> None:
    fake = FakeDynamo()
    run = _armed(fake, "write_a")
    _dispatch(fake, SpySender())
    fake.conflicts_next = MAX_PARALLEL_CLAIMS
    with pytest.raises(ClaimRetryExhaustedError) as exhausted:
        claim_graph_envelope(store(fake), _NoFacts(), _claim_input(fake, run, "write_a"))
    assert exhausted.value.conflicts == MAX_PARALLEL_CLAIMS
    assert kinds(fake, "EXEC_CLAIM#") == []


# --- V-11: fence settlement -----------------------------------------------------------------------


def test_v11_a_ready_permitless_node_settles_fenced_unsent_and_releases_its_lock() -> None:
    fake = FakeDynamo()
    run = _armed(fake, "write_a")
    run_operator_command(store(fake), _command(RunCommandKind.CANCEL, run, "cancel-0001"))
    counts = settle_shard(store(fake), SHARD, now_text)
    assert counts.fenced_unsent == 1
    assert counts.terminal == 1
    assert node(fake, run, "write_a").state is NodeState.FENCED_UNSENT
    assert kinds(fake, "LOCK#") == kinds(fake, "TOKEN#") == kinds(fake, "OUTBOX#") == []
    cancelled = store(fake).read_run(run)
    assert cancelled is not None and cancelled.state is RunState.CANCELLED
    assert kinds(fake, "FENCE#") == []


def test_v11_a_permit_after_the_generation_advance_is_refused() -> None:
    fake = FakeDynamo()
    run = _armed(fake, "write_a")
    item = node(fake, run, "write_a")
    outbox = store(fake).read_outbox(SHARD, item.node_id)
    assert outbox is not None
    run_operator_command(store(fake), _command(RunCommandKind.CANCEL, run, "cancel-0001"))
    fenced = store(fake).read_run(run)
    assert fenced is not None
    with pytest.raises(GraphGuardError) as refused:
        store(fake).acquire_permit(fenced, item, outbox)
    assert refused.value.signal == PLAN_DISPATCH_FENCED


def test_v11_positive_a_replacement_run_acquires_the_released_lock() -> None:
    fake = FakeDynamo()
    run = _armed(fake, "write_a")
    run_operator_command(store(fake), _command(RunCommandKind.CANCEL, run, "cancel-0001"))
    newer = seed_run(fake, fingerprint="7" * 64, request_id="request-0002", slot=False)
    assert instantiate(fake, newer.record.run_id, "write_a") is InstantiationOutcome.LOCK_WAIT
    settle_shard(store(fake), SHARD, now_text)
    assert instantiate(fake, newer.record.run_id, "write_a") is InstantiationOutcome.ARMED
    assert node(fake, newer.record.run_id, "write_a").state is NodeState.READY


# --- V-25: slot ownership and the two release points ----------------------------------------------


def test_v25_a_stale_release_against_a_newer_slot_owner_is_slot_not_owned() -> None:
    fake = FakeDynamo()
    run = _armed(fake, "write_a")
    fake.items[(f"SLOT#{UNIT}", "SLOT")]["active_run_id"] = {"S": "newer-run"}
    current = store(fake).read_run(run)
    assert current is not None
    slot_before = dict(fake.items[(f"SLOT#{UNIT}", "SLOT")])
    with pytest.raises(GraphGuardError) as refused:
        store(fake).fence_run(FenceRunRequest(current, FenceReason.CANCELLED, now_text()))
    assert refused.value.signal == SLOT_NOT_OWNED
    assert fake.items[(f"SLOT#{UNIT}", "SLOT")] == slot_before
    assert kinds(fake, "PROMOTE#") == []


def test_v25_release_point_one_clears_its_own_slot_and_wakes_promote_once() -> None:
    fake = FakeDynamo()
    run = _armed(fake, "write_a")
    run_operator_command(store(fake), _command(RunCommandKind.CANCEL, run, "cancel-0001"))
    slot = fake.items[(f"SLOT#{UNIT}", "SLOT")]
    assert "active_run_id" not in slot
    assert slot["revision"] == {"N": "2"}
    assert fake.items[(SHARD, f"PROMOTE#{UNIT}")]["wake_seq"] == {"N": "1"}


def test_v25_the_terminal_transaction_touches_no_slot_and_needs_fencing() -> None:
    fake = FakeDynamo()
    run = _armed(fake, "write_a")
    current = store(fake).read_run(run)
    assert current is not None
    with pytest.raises(ValueError, match="only from FENCING"):
        build_terminal(current, store(fake).read_nodes(run))
    fenced = replace(current, state=RunState.FENCING, fence_reason=FenceReason.CANCELLED)
    members = build_terminal(fenced, store(fake).read_nodes(run))
    keys = [_member_pk(member.item) for member in members]
    assert not any(key.startswith("SLOT#") for key in keys)
    release = build_fence_run(FenceRunRequest(current, FenceReason.CANCELLED, now_text()))
    assert f"SLOT#{UNIT}" in [_member_pk(member.item) for member in release]


def test_v25_rows_17_and_18_hand_the_slot_over_with_no_promote() -> None:
    fake = FakeDynamo()
    run = _armed(fake, "write_a")
    members = build_admission_transaction("t", _replace_commit(fake, run))
    assert not any(_member_sk(member).startswith("PROMOTE#") for member in members)
    assert any(_member_pk(member).startswith("SLOT#") for member in members)


def test_a_terminal_attempted_while_instantiation_is_open_is_refused() -> None:
    fake = FakeDynamo()
    set_mode(fake, AuthorityMode.GRAPH)
    seeded = seed_run(fake)
    run = seeded.record.run_id
    store(fake).fence_run(FenceRunRequest(seeded.record, FenceReason.CANCELLED, now_text()))
    fenced = store(fake).read_run(run)
    assert fenced is not None
    with pytest.raises(GraphGuardError) as refused:
        store(fake).terminalize_run(fenced, ())
    assert refused.value.signal == "RUN_INSTANTIATION_OPEN"
    still = store(fake).read_run(run)
    assert still is not None and still.state is RunState.FENCING


def test_the_graph_authority_is_absent_where_it_is_not_provisioned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from adapters import config, graph_authority  # noqa: PLC0415

    monkeypatch.delenv("ADL_EXECUTION_GRAPH_TABLE", raising=False)
    monkeypatch.setenv("ADL_RAW_BUCKET", "fictional-raw")
    config.run_request_api_settings.cache_clear()
    try:
        assert graph_authority.provisioned_graph_table() is None
        monkeypatch.setenv("ADL_EXECUTION_GRAPH_TABLE", "fictional-graph")
        config.run_request_api_settings.cache_clear()
        assert graph_authority.provisioned_graph_table() == "fictional-graph"
    finally:
        config.run_request_api_settings.cache_clear()


# --- S3 iteration 2 (s3-verdict.json) -------------------------------------------------------------


def test_s3v1_v11_a_fenced_run_with_a_successor_settles_every_node_and_turns_terminal() -> None:
    fake = FakeDynamo()
    set_mode(fake, AuthorityMode.GRAPH)
    run = seed_run(fake).record.run_id
    assert instantiate(fake, run, "write_a", successors=("publish",)) is InstantiationOutcome.ARMED
    assert node(fake, run, "publish").state is NodeState.PENDING
    assert node(fake, run, "publish").after == ("write_a",)
    run_operator_command(store(fake), _command(RunCommandKind.CANCEL, run, "cancel-0001"))
    counts = settle_shard(store(fake), SHARD, now_text)
    assert counts.fenced_unsent == 2
    assert counts.terminal == 1
    assert node(fake, run, "write_a").state is NodeState.FENCED_UNSENT
    assert node(fake, run, "publish").state is NodeState.FENCED_UNSENT
    final = store(fake).read_run(run)
    assert final is not None and final.state is RunState.CANCELLED
    assert kinds(fake, "FENCE#") == kinds(fake, "LOCK#") == kinds(fake, "TOKEN#") == []


def test_s3v1_a_successor_after_a_root_superseded_at_claim_still_settles() -> None:
    fake = FakeDynamo()
    set_mode(fake, AuthorityMode.GRAPH)
    run = seed_run(fake).record.run_id
    instantiate(fake, run, "write_a", successors=("publish",))
    _dispatch(fake, SpySender())
    _replace_before_compute(fake, run)
    disposition = claim_graph_envelope(store(fake), _NoFacts(), _claim_input(fake, run, "write_a"))
    assert disposition.signal == "EXECUTION_SUPERSEDED"
    fenced = store(fake).read_run(run)
    assert fenced is not None and fenced.state is RunState.FENCING  # the successor is PENDING
    settle_shard(store(fake), SHARD, now_text)
    assert node(fake, run, "publish").state is NodeState.FENCED_UNSENT
    final = store(fake).read_run(run)
    assert final is not None and final.state is RunState.CANCELLED


def test_s3v1_a_pending_node_is_not_settled_while_instantiation_is_open() -> None:
    fake = FakeDynamo()
    set_mode(fake, AuthorityMode.GRAPH)
    seeded = seed_run(fake)
    run = seeded.record.run_id
    from ports.outbound.execution_graph_dispatch import PutNodesRequest  # noqa: PLC0415

    store(fake).put_nodes(PutNodesRequest(seeded.record, specs_for(run), now_text()))
    _replace_before_compute(fake, run)
    fenced = store(fake).read_run(run)
    assert fenced is not None
    counts = settle_shard(store(fake), SHARD, now_text)
    assert counts.fenced_unsent == 0  # instantiation owns its put nodes while INSTANTIATE# exists
    assert node(fake, run, "write_a").state is NodeState.PENDING
    with pytest.raises(GraphGuardError) as refused:
        store(fake).fence_pending(fenced, node(fake, run, "write_a"))
    assert refused.value.signal == "RUN_INSTANTIATION_OPEN"


def specs_for(run: str) -> Any:
    from execution_graph_dispatch_support import specs  # noqa: PLC0415

    return specs(run, "write_a")


def test_s3v5_a_provider_oversize_on_a_fenced_run_fails_the_node_and_frees_its_lock() -> None:
    fake = FakeDynamo()
    run = _armed(fake, "write_a")
    dispatch = store(fake)
    item = node(fake, run, "write_a")
    outbox = dispatch.read_outbox(SHARD, item.node_id)
    current = dispatch.read_run(run)
    assert outbox is not None and current is not None
    dispatch.acquire_permit(current, item, outbox)
    run_operator_command(dispatch, _command(RunCommandKind.CANCEL, run, "cancel-0001"))
    counts = _dispatch(fake, SpySender(oversize=True))
    assert counts.oversize == 1
    assert counts.guards == 0
    failed = node(fake, run, "write_a")
    assert failed.state is NodeState.FAILED
    assert kinds(fake, "OUTBOX#") == kinds(fake, "LOCK#") == kinds(fake, "TOKEN#") == []
    fenced = dispatch.read_run(run)
    assert fenced is not None
    assert (fenced.state, fenced.fence_reason) == (RunState.FENCING, FenceReason.CANCELLED)
    assert try_terminalize(dispatch, run) is RunState.CANCELLED
    # A second pass sends nothing: the SENDING row is gone.
    assert _dispatch(fake, SpySender(oversize=True)).rows == 0


def test_s3v6_a_resume_that_read_before_an_arming_rereads_and_rearms_that_root() -> None:
    from ports.outbound.execution_graph import CommandReceipt  # noqa: PLC0415
    from ports.outbound.execution_graph_dispatch import RunCommand  # noqa: PLC0415

    fake = FakeDynamo()
    set_mode(fake, AuthorityMode.GRAPH)
    run = seed_run(fake).record.run_id
    dispatch = store(fake)
    assert run_operator_command(
        dispatch, _command(RunCommandKind.PAUSE, run, "pause-0001")
    ).committed
    paused = dispatch.read_run(run)
    assert paused is not None
    stale = RunCommand(
        paused,
        CommandReceipt(
            "resume-stale",
            "resume_plan",
            OPERATOR,
            1,
            AuthorityMode.GRAPH,
            "committed",
            now_text(),
            "v1",
            {"run_id": run},
        ),
        (),
    )
    # Instantiation arms the root under the paused generation after resume's read.
    assert instantiate(fake, run, "write_a") is InstantiationOutcome.ARMED
    with pytest.raises(GraphGuardError) as refused:
        dispatch.resume_run(stale)
    assert refused.value.signal == "RUN_STATE_CHANGED"
    resumed = run_operator_command(dispatch, _command(RunCommandKind.RESUME, run, "resume-001"))
    assert resumed.committed
    sender = SpySender()
    counts = _dispatch(fake, sender)
    assert counts.fenced == 0
    assert len(sender.calls) == 1
    assert node(fake, run, "write_a").state is NodeState.DISPATCHED


class _FailingOutboxStore:
    """The real store, except that reading one outbox row fails (a malformed item)."""

    def __init__(self, fake: FakeDynamo, poison: str) -> None:
        self._inner = store(fake)
        self._poison = poison

    def read_outbox(self, shard: str, node_id: str) -> Any:
        if node_id == self._poison:
            from ports.outbound.execution_graph_dispatch import (  # noqa: PLC0415
                GraphStoreUnavailableError,
            )

            raise GraphStoreUnavailableError("a graph item is missing generation.")
        return self._inner.read_outbox(shard, node_id)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def test_s3v7_one_failing_row_is_reported_once_and_the_pass_sends_every_other_row() -> None:
    fake = FakeDynamo()
    run = _armed(fake, "write_a", "write_b", "write_c")
    poison = node(fake, run, "write_b").node_id
    failures: list[BaseException] = []
    sender = SpySender()
    counts = dispatch_shard(
        DispatchContext(
            cast("Any", _FailingOutboxStore(fake, poison)), sender, now_text, failures.append
        ),
        SHARD,
    )
    assert counts.failed == 1
    assert len(failures) == 1
    assert len(sender.calls) == 2
    assert node(fake, run, "write_b").state is NodeState.READY  # left for the next pass


def test_s3v7_a_transaction_conflict_on_a_row_is_retried_and_counted() -> None:
    fake = FakeDynamo()
    run = _armed(fake, "write_a")
    fake.conflicts_next = 2
    sender = SpySender()
    counts = _dispatch(fake, sender)
    assert counts.conflicts == 2
    assert counts.failed == 0
    assert len(sender.calls) == 1
    assert node(fake, run, "write_a").state is NodeState.DISPATCHED


def test_s3v7_a_settler_conflict_does_not_end_the_pass() -> None:
    fake = FakeDynamo()
    run = _armed(fake, "write_a")
    run_operator_command(store(fake), _command(RunCommandKind.CANCEL, run, "cancel-0001"))
    fake.conflicts_next = 1
    counts = settle_shard(store(fake), SHARD, now_text)
    assert counts.conflicts == 1
    assert counts.failed == 0
    final = store(fake).read_run(run)
    assert final is not None and final.state is RunState.CANCELLED


def test_s3v10_guards_are_not_conflicts_and_never_raise_the_stop_rule() -> None:
    fake = FakeDynamo()
    run = _armed(fake, "write_a")
    _dispatch(fake, SpySender())

    class _AlwaysMoved:
        def __init__(self) -> None:
            self._inner = store(fake)

        def claim(self, request: Any) -> Any:
            del request
            raise GraphGuardError("NODE_STATE_CHANGED")

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

    with pytest.raises(GraphGuardError) as moved:
        claim_graph_envelope(
            cast("Any", _AlwaysMoved()), _NoFacts(), _claim_input(fake, run, "write_a")
        )
    assert moved.value.signal == "NODE_STATE_CHANGED"
    assert kinds(fake, "EXEC_CLAIM#") == []


def test_s3v10_a_claim_reports_its_attempts_and_backs_off_between_conflicts() -> None:
    fake = FakeDynamo()
    run = _armed(fake, "write_a")
    _dispatch(fake, SpySender())
    fake.conflicts_next = 3
    waits: list[int] = []
    disposition = claim_graph_envelope(
        store(fake), _NoFacts(), _claim_input(fake, run, "write_a", sleep_ms=waits.append)
    )
    assert disposition.claim is not None
    assert (disposition.conflicts, disposition.attempts) == (3, 4)
    assert len(waits) == 3
    assert all(0 < wait <= 160 for wait in waits)


def test_s3v12_an_exact_command_replay_returns_its_receipt_and_writes_nothing() -> None:
    fake = FakeDynamo()
    run = _armed(fake, "write_a")
    dispatch = store(fake)
    first = run_operator_command(dispatch, _command(RunCommandKind.PAUSE, run, "pause-0001"))
    assert first.committed
    writes = len(fake.transactions)
    replay = run_operator_command(dispatch, _command(RunCommandKind.PAUSE, run, "pause-0001"))
    assert replay.committed
    assert replay.receipt == first.receipt
    assert len(fake.transactions) == writes
    from domain.operations.execution_graph import GraphCommandRefusedError  # noqa: PLC0415

    with pytest.raises(GraphCommandRefusedError) as reused:
        run_operator_command(dispatch, _command(RunCommandKind.CANCEL, run, "pause-0001"))
    assert reused.value.signal == "COMMAND_ID_CONFLICT"
    assert len(fake.transactions) == writes


def test_s3v12_a_cancel_racing_a_claims_revision_bump_rereads_and_commits() -> None:
    fake = FakeDynamo()
    run = _armed(fake, "write_a", "write_b")
    _dispatch(fake, SpySender())

    def first_claim(_fake: Any) -> None:
        claim_graph_envelope(store(fake), _NoFacts(), _claim_input(fake, run, "write_a"))

    fake.before_transaction = first_claim
    cancelled = run_operator_command(
        store(fake), _command(RunCommandKind.CANCEL, run, "cancel-0001")
    )
    assert cancelled.committed
    fenced = store(fake).read_run(run)
    assert fenced is not None and fenced.state is RunState.FENCING
    assert fenced.compute_started_at is not None


def _command_receipts(fake: FakeDynamo) -> list[dict[str, Any]]:
    return [item for (pk, _sk), item in fake.items.items() if pk.startswith("COMMAND#")]


def test_s3v2_3_a_pause_racing_its_committed_twin_returns_the_twins_receipt() -> None:
    fake = FakeDynamo()
    run = _armed(fake, "write_a")
    command = _command(RunCommandKind.PAUSE, run, "pause-dup-1")
    twin: list[Any] = []

    def committed_twin(_fake: Any) -> None:
        twin.append(run_operator_command(store(fake), command))

    # Both read the command ID as absent; the twin commits between this read and this commit.
    fake.before_transaction = committed_twin
    result = run_operator_command(store(fake), command)
    assert twin[0].committed
    assert result.committed
    assert result.receipt == twin[0].receipt
    assert len(_command_receipts(fake)) == 1
    paused = store(fake).read_run(run)
    assert paused is not None
    assert (paused.state, paused.dispatch_generation) == (RunState.PAUSED, 1)


def test_s3v2_3_a_refusal_racing_its_twins_refusal_returns_the_twins_receipt() -> None:
    fake = FakeDynamo()
    set_mode(fake, AuthorityMode.GRAPH)
    run = seed_run(fake, grant=True).record.run_id
    command = _command(RunCommandKind.CANCEL, run, "cancel-dup-1")
    twin: list[Any] = []

    def committed_twin(_fake: Any) -> None:
        twin.append(run_operator_command(store(fake), command))

    fake.before_transaction = committed_twin
    result = run_operator_command(store(fake), command)
    assert (result.committed, result.details) == (False, {"signal": "RUN_GRANT_PENDING_APPLY"})
    assert result.receipt == twin[0].receipt
    assert len(_command_receipts(fake)) == 1


def test_s3v2_3_a_hold_racing_its_committed_twin_returns_the_twins_receipt() -> None:
    fake = FakeDynamo()
    set_mode(fake, AuthorityMode.GRAPH)
    command = OperatorCommand(RunCommandKind.SET_HOLD, "hold-dup-1", OPERATOR, now_text)
    twin: list[Any] = []

    def committed_twin(_fake: Any) -> None:
        twin.append(run_operator_command(store(fake), command))

    fake.before_transaction = committed_twin
    result = run_operator_command(store(fake), command)
    assert twin[0].committed
    assert result.committed
    assert result.receipt == twin[0].receipt
    assert len(_command_receipts(fake)) == 1
    mode = store(fake).read_mode()
    assert (mode.hold, mode.hold_revision) == (True, 1)


def test_fault_matrix_a_delayed_duplicate_after_settlement_is_acknowledged_with_zero_effect() -> (
    None
):
    fake = FakeDynamo()
    run = _armed(fake, "write_a")
    _dispatch(fake, SpySender())
    _replace_before_compute(fake, run)
    first = claim_graph_envelope(store(fake), _NoFacts(), _claim_input(fake, run, "write_a"))
    assert first.outcome == "execution_superseded"
    before = {key: dict(value) for key, value in fake.items.items()}
    writes = len(fake.transactions)
    # The same bytes redelivered after the FIFO deduplication window closed.
    late = claim_graph_envelope(
        store(fake),
        _NoFacts(),
        _claim_input(fake, run, "write_a", now=lambda: NOW + timedelta(hours=6)),
    )
    assert (late.acknowledge, late.outcome) == (True, "graph_already_settled")
    assert len(fake.transactions) == writes
    assert {key: dict(value) for key, value in fake.items.items()} == before


# --- S6 §2.7 (D13): the POST_GRANT permit -----------------------------------------------------


def _publish_row(fake: FakeDynamo, run: str) -> tuple[RunRecord, NodeRecord, OutboxRecord]:
    publish = node(fake, run, "publish")
    outbox = store(fake).read_outbox(SHARD, publish.node_id)
    current = store(fake).read_run(run)
    assert outbox is not None and current is not None
    return current, publish, outbox


def test_d13_a_post_grant_row_on_a_granted_run_is_permitted_and_sent() -> None:
    fake = FakeDynamo()
    run = granted_run(fake)
    sender = SpySender()

    counts = _dispatch(fake, sender)

    publish = node(fake, run, "publish")
    assert (counts.permits, counts.sent, counts.fenced) == (1, 1, 0)
    assert publish.state is NodeState.DISPATCHED
    assert sender.calls == [(publish.body, publish.group_id, publish.dispatch_id)]


def test_d13_a_pre_grant_row_on_a_granted_run_is_refused_control_post_grant_permitted() -> None:
    fake = FakeDynamo()
    run = granted_run(fake)
    current, publish, outbox = _publish_row(fake, run)
    before = copy.deepcopy(fake.items)

    with pytest.raises(GraphGuardError) as refused:
        store(fake).acquire_permit(current, replace(publish, phase=NodePhase.PRE_GRANT), outbox)

    assert refused.value.signal == PLAN_DISPATCH_FENCED
    assert fake.items == before
    store(fake).acquire_permit(current, publish, outbox)
    assert node(fake, run, "publish").state is NodeState.PERMITTED


@pytest.mark.parametrize("state", [RunState.OPEN, RunState.FAILED])
def test_d13_a_post_grant_row_on_an_open_or_failed_run_is_refused(state: RunState) -> None:
    fake = FakeDynamo()
    run = granted_run(fake)
    fake.items[(f"RUN#{run}", "RUN")]["state"] = {"S": state.value}
    current, publish, outbox = _publish_row(fake, run)
    before = copy.deepcopy(fake.items)

    with pytest.raises(GraphGuardError) as refused:
        store(fake).acquire_permit(current, publish, outbox)

    assert refused.value.signal == PLAN_DISPATCH_FENCED
    assert fake.items == before
    # Control: back on GRANTED, the same row is permitted.
    fake.items[(f"RUN#{run}", "RUN")]["state"] = {"S": RunState.GRANTED.value}
    store(fake).acquire_permit(current, publish, outbox)
    assert node(fake, run, "publish").state is NodeState.PERMITTED


# --- S6 §2.7 (D23): a POST_GRANT oversize is an invariant, never a second run exit ------------


def test_d23_a_post_grant_oversize_keeps_sending_and_permitted_and_emits_on_every_pass() -> None:
    fake = FakeDynamo()
    run = granted_run(fake)
    seen: list[str] = []
    context = DispatchContext(
        store(fake),
        SpySender(oversize=True),
        now_text,
        on_post_grant_invariant=lambda item: seen.append(item.name),
    )

    first = dispatch_shard(context, SHARD)
    second = dispatch_shard(context, SHARD)

    assert (first.post_grant_invariant, second.post_grant_invariant) == (1, 1)
    assert (first.oversize, second.oversize) == (0, 0)
    assert seen == ["publish", "publish"]
    publish = node(fake, run, "publish")
    assert publish.state is NodeState.PERMITTED
    outbox = store(fake).read_outbox(SHARD, publish.node_id)
    assert outbox is not None and outbox.state == "SENDING"
    granted = store(fake).read_run(run)
    assert granted is not None and granted.state is RunState.GRANTED
    assert (f"LOCK#{publish.lock_key}", "LOCK") in fake.items


def test_d23_control_a_pre_grant_oversize_still_fails_the_node_and_fences_oversize() -> None:
    fake = FakeDynamo()
    run = _armed(fake, "write_a")
    seen: list[str] = []
    context = DispatchContext(
        store(fake),
        SpySender(oversize=True),
        now_text,
        on_post_grant_invariant=lambda item: seen.append(item.name),
    )

    counts = dispatch_shard(context, SHARD)

    assert (counts.oversize, counts.post_grant_invariant) == (1, 0)
    assert seen == []
    assert node(fake, run, "write_a").state is NodeState.FAILED
    fenced = store(fake).read_run(run)
    assert fenced is not None
    assert (fenced.state, fenced.fence_reason) == (RunState.FENCING, FenceReason.OVERSIZE)


# --- S6 §2.1: routing from the generated contract routes ---------------------------------------


def _record(name: str) -> NodeRecord:
    return NodeRecord(
        run_id="run-fictional",
        name=name,
        node_id=f"node-{name}",
        state=NodeState.PERMITTED,
        revision=2,
        phase=NodePhase.PRE_GRANT,
        after=(),
        lock_key=f"lock-{name}",
        group_id=f"group-{name}",
        body='{"fictional":true}',
        envelope_digest="e" * 64,
        measured_bytes=18,
        dispatch_id=f"dispatch-{name}",
    )


def test_each_contract_route_goes_to_its_own_sender() -> None:
    senders = {route: SpySender() for route in ROUTES}
    routed = RoutedGraphSender(TEMPLATE, senders)
    expected = {
        "materialize_raw": "delivery",
        "write_unit:orders": "semantic_write",
        "publish": "semantic_write",
        "precompute": "precompute",
        "bake": "bake",
    }
    for name in expected:
        routed.send(GraphMessage.of(_record(name)))
    for route, sender in senders.items():
        names = sorted(name for name, target in expected.items() if target == route)
        assert sorted(call[2].removeprefix("dispatch-") for call in sender.calls) == names


def test_a_node_without_a_contract_route_is_never_sent() -> None:
    senders = {route: SpySender() for route in ROUTES}
    routed = RoutedGraphSender(TEMPLATE, senders)
    with pytest.raises(InvariantError) as refused:
        routed.send(GraphMessage.of(_record("grant_apply")))
    assert refused.value.code == UNROUTED_GRAPH_NODE
    assert all(sender.calls == [] for sender in senders.values())
    # Control: a routed node is sent.
    routed.send(GraphMessage.of(_record("bake")))
    assert len(senders["bake"].calls) == 1


def test_slice7_a_unit_batch_message_routes_to_the_semantic_write_sender() -> None:
    """Test 12 "Batch routing" (pass 5, M2): one send path, routed by the message's node."""
    senders = {route: SpySender() for route in ROUTES}
    routed = RoutedGraphSender(TEMPLATE, senders)
    body = '{"batch_id":"b","lock_key":"TABLE#orders","members":[],"node":"write_unit_batch"}'
    message = GraphMessage("write_unit", body, "orders", "dedup-batch")
    assert routed.send(message) == "message-1"
    assert senders["semantic_write"].calls == [(body, "orders", "dedup-batch")]
    assert senders["semantic_write"].nodes == ["write_unit"]
    assert all(sender.calls == [] for route, sender in senders.items() if route != "semantic_write")
    # Control: a message naming no routed template node is the built invariant.
    with pytest.raises(InvariantError) as refused:
        routed.send(GraphMessage("grant_apply", body, "orders", "dedup-batch"))
    assert refused.value.code == UNROUTED_GRAPH_NODE


def test_the_router_needs_exactly_one_sender_per_contract_route() -> None:
    partial = {route: SpySender() for route in ROUTES if route != "precompute"}
    with pytest.raises(ValueError, match="exactly one sender per contract route"):
        RoutedGraphSender(TEMPLATE, partial)
    # Control: the full route set builds.
    RoutedGraphSender(TEMPLATE, {route: SpySender() for route in ROUTES})


_ROUTE_ENV = {
    "ADL_GRAPH_DELIVERY_QUEUE_URL": "https://queue.fictional/delivery",
    "ADL_SEMANTIC_WRITE_QUEUE_URL": "https://queue.fictional/semantic-write.fifo",
    "ADL_GRAPH_BAKE_QUEUE_URL": "https://queue.fictional/bake.fifo",
    "ADL_GRAPH_PRECOMPUTE_QUEUE_URL": "https://queue.fictional/precompute.fifo",
}


def test_the_dispatcher_settings_refuse_a_missing_route_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from adapters import config  # noqa: PLC0415

    for name, value in _ROUTE_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("ADL_GRAPH_PRECOMPUTE_QUEUE_URL")
    config.graph_dispatcher_settings.cache_clear()
    try:
        with pytest.raises(InvariantError) as refused:
            config.graph_dispatcher_settings()
        assert refused.value.code == config.CONFIG_UNRESOLVED
        assert refused.value.context == {"missing": ["graph_precompute_queue_url"]}
        # Control: with every route's queue set, the settings resolve.
        monkeypatch.setenv(
            "ADL_GRAPH_PRECOMPUTE_QUEUE_URL", _ROUTE_ENV["ADL_GRAPH_PRECOMPUTE_QUEUE_URL"]
        )
        config.graph_dispatcher_settings.cache_clear()
        resolved = config.graph_dispatcher_settings()
        assert resolved.delivery_queue_url == _ROUTE_ENV["ADL_GRAPH_DELIVERY_QUEUE_URL"]
        assert resolved.precompute_queue_url == _ROUTE_ENV["ADL_GRAPH_PRECOMPUTE_QUEUE_URL"]
    finally:
        config.graph_dispatcher_settings.cache_clear()


class _Sqs:
    """A fake SQS client: records every `send_message` call's arguments."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def send_message(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"MessageId": f"message-{len(self.calls)}"}


def test_the_standard_sender_sends_no_group_or_deduplication_id() -> None:
    client = _Sqs()
    item = _record("materialize_raw")
    standard = SqsStandardGraphSender("https://queue.fictional/delivery", cast("Any", client))
    assert standard.send(GraphMessage.of(item)) == "message-1"
    assert client.calls == [
        {"QueueUrl": "https://queue.fictional/delivery", "MessageBody": item.body}
    ]
    # Control: the FIFO sender sends both.
    fifo = _Sqs()
    SqsGraphSender("https://queue.fictional/semantic.fifo", cast("Any", fifo)).send(
        GraphMessage.of(item)
    )
    assert fifo.calls[0]["MessageGroupId"] == item.group_id
    assert fifo.calls[0]["MessageDeduplicationId"] == item.dispatch_id


# --- S6 §2.3: lease release, and the claim's admission counter (§2.10) ------------------------


def _claim_item(fake: FakeDynamo, run: str, name: str) -> dict[str, Any]:
    return fake.items[(f"EXEC_CLAIM#{node(fake, run, name).node_id}", "CLAIM")]


def test_s23_a_released_lease_makes_the_redelivery_reclaim() -> None:
    fake = FakeDynamo()
    run = _armed(fake, "write_a")
    _dispatch(fake, SpySender())
    first = claim_graph_envelope(store(fake), _NoFacts(), _claim_input(fake, run, "write_a"))
    assert first.claim is not None

    assert release_claim_lease(store(fake), first.claim, NOW) is True

    (call,) = fake.update_calls  # one UpdateItem, conditioned on the held lease version
    assert call["ConditionExpression"] == "lease_version = :version"
    assert call["UpdateExpression"] == "SET lease_expires_at = :released"
    again = claim_graph_envelope(store(fake), _NoFacts(), _claim_input(fake, run, "write_a"))
    assert (again.acknowledge, again.outcome, again.signal) == (False, "graph_claimed", None)
    assert again.claim is not None and again.claim.lease_version == 2


def test_s23_control_without_a_release_the_redelivery_is_a_live_duplicate() -> None:
    fake = FakeDynamo()
    run = _armed(fake, "write_a")
    _dispatch(fake, SpySender())
    claim_graph_envelope(store(fake), _NoFacts(), _claim_input(fake, run, "write_a"))

    again = claim_graph_envelope(store(fake), _NoFacts(), _claim_input(fake, run, "write_a"))

    assert (again.acknowledge, again.signal) == (False, CLAIM_LEASE_LIVE)


def test_s23_a_release_on_a_stale_lease_version_is_refused_and_changes_nothing() -> None:
    fake = FakeDynamo()
    run = _armed(fake, "write_a")
    _dispatch(fake, SpySender())
    first = claim_graph_envelope(store(fake), _NoFacts(), _claim_input(fake, run, "write_a"))
    assert first.claim is not None
    release_claim_lease(store(fake), first.claim, NOW)
    reclaimed = claim_graph_envelope(store(fake), _NoFacts(), _claim_input(fake, run, "write_a"))
    assert reclaimed.claim is not None
    before = copy.deepcopy(_claim_item(fake, run, "write_a"))

    with pytest.raises(GraphGuardError) as refused:
        store(fake).release_claim_lease(first.claim, NOW.isoformat())

    assert refused.value.signal == CLAIM_LEASE_STALE
    assert _claim_item(fake, run, "write_a") == before
    assert release_claim_lease(store(fake), first.claim, NOW) is False  # ignored by claimers
    # Control: the current holder's release commits.
    assert release_claim_lease(store(fake), reclaimed.claim, NOW) is True


def test_admission_rises_on_claim_and_reclaim_and_not_on_a_lease_bump() -> None:
    fake = FakeDynamo()
    run = _armed(fake, "write_a")
    _dispatch(fake, SpySender())
    first = claim_graph_envelope(store(fake), _NoFacts(), _claim_input(fake, run, "write_a"))
    assert first.claim is not None
    item = _claim_item(fake, run, "write_a")
    record = store(fake).read_run(run)
    assert record is not None
    assert (item["admission"], item["admitted_at"]) == ({"N": "1"}, {"S": NOW.isoformat()})
    assert item["root_flow_id"] == {"S": record.root_flow_id}
    assert item["authority_epoch"] == {"N": "1"}
    release_claim_lease(store(fake), first.claim, NOW)
    later = NOW + timedelta(seconds=5)
    reclaim = _claim_input(fake, run, "write_a", now=lambda: later, lease=timedelta(seconds=1))
    second = claim_graph_envelope(store(fake), _NoFacts(), reclaim)
    assert second.claim is not None and second.claim.admission == 2
    item = _claim_item(fake, run, "write_a")
    assert (item["admission"], item["admitted_at"]) == ({"N": "2"}, {"S": later.isoformat()})
    # Control: a takeover's lease bump on the fenced run is not an admission.
    _replace_before_compute_after_compute(fake, run)
    takeover = _claim_input(fake, run, "write_a", now=lambda: later + timedelta(minutes=1))
    assert claim_graph_envelope(store(fake), _NoFacts(), takeover).acknowledge
    claim = store(fake).read_claim(node(fake, run, "write_a").node_id)
    assert claim is not None
    assert (claim.lease_version, claim.admission) == (3, 2)


# --- S6 §2.7 (G2-17): a POST_GRANT takeover is unreachable and fails closed --------------------


def test_g2_17_a_post_grant_takeover_raises_the_invariant_with_zero_effect() -> None:
    fake = FakeDynamo()
    run = granted_run(fake)
    _dispatch(fake, SpySender())
    short = _claim_input(fake, run, "publish", lease=timedelta(seconds=1))
    # Control: on its live (GRANTED) run the claim never asks for takeover facts.
    claimed = claim_graph_envelope(store(fake), UnreachablePostGrantFacts(), short)
    assert claimed.outcome == "graph_claimed"
    fake.items[(f"RUN#{run}", "RUN")]["state"] = {"S": RunState.SUCCEEDED.value}  # unreachable
    before = copy.deepcopy(fake.items)
    later = _claim_input(fake, run, "publish", now=lambda: NOW + timedelta(minutes=1))

    with pytest.raises(InvariantError) as refused:
        claim_graph_envelope(store(fake), UnreachablePostGrantFacts(), later)

    assert refused.value.code == POST_GRANT_TAKEOVER_UNREACHABLE
    assert fake.items == before


# --- Slice 7 (1b): record parsing and the fake's transaction limits (P7) ----------------------


def test_slice7_a_node_item_with_the_batch_attributes_parses_and_without_them_defaults() -> None:
    from adapters.outbound.dynamodb.execution_graph_dispatch import (  # noqa: PLC0415
        read_node_item,
    )

    fake = FakeDynamo()
    run = _armed(fake, "write_a")
    key = (f"RUN#{run}", "NODE#write_a")
    plain = read_node_item(cast("Any", fake.items[key]))
    assert (plain.batch_id, plain.return_reason, plain.arm_alone, plain.final_return) == (
        None,
        None,
        False,
        False,
    )
    assert (plain.failure_code, plain.returned_at) == (None, None)
    item = copy.deepcopy(fake.items[key])
    item.update(
        {
            "batch_id": {"S": "b" * 64},
            "returned_at": {"S": now_text()},
            "return_reason": {"S": "member_failed"},
            "arm_alone": {"BOOL": True},
            "final_return": {"BOOL": True},
            "failure_code": {"S": "ADLY-TABLE_WRITER-0006"},
        }
    )
    parsed = read_node_item(cast("Any", item))
    assert (parsed.batch_id, parsed.return_reason, parsed.arm_alone, parsed.final_return) == (
        "b" * 64,
        "member_failed",
        True,
        True,
    )
    assert (parsed.failure_code, parsed.returned_at) == ("ADLY-TABLE_WRITER-0006", now_text())


def _put(index: int, pk: str | None = None) -> dict[str, Any]:
    return {
        "Put": {
            "TableName": "fictional",
            "Item": {"pk": {"S": pk or f"ITEM#{index}"}, "sk": {"S": "X"}},
        }
    }


def test_slice7_the_fake_refuses_more_than_100_actions_and_accepts_100() -> None:
    from botocore.exceptions import ClientError  # noqa: PLC0415

    fake = FakeDynamo()
    with pytest.raises(ClientError) as refused:
        fake.transact_write_items(TransactItems=[_put(index) for index in range(101)])
    assert cast("dict[str, Any]", refused.value.response)["Error"]["Code"] == "ValidationException"
    assert fake.items == {}
    fake.transact_write_items(TransactItems=[_put(index) for index in range(100)])
    assert len(fake.items) == 100


def test_slice7_the_fake_refuses_two_actions_on_one_item_and_accepts_distinct_keys() -> None:
    from botocore.exceptions import ClientError  # noqa: PLC0415

    fake = FakeDynamo()
    with pytest.raises(ClientError) as refused:
        fake.transact_write_items(TransactItems=[_put(0, "SAME"), _put(1, "SAME")])
    assert cast("dict[str, Any]", refused.value.response)["Error"]["Code"] == "ValidationException"
    assert fake.items == {}
    fake.transact_write_items(TransactItems=[_put(0, "ONE"), _put(1, "TWO")])
    assert len(fake.items) == 2


# --- GRAPH serving coalescing Slice 1: pure roster, election, and capacity rules --------------


def _serving_candidate(ordinal: int, **overrides: Any) -> ServingCandidate:
    values: dict[str, Any] = {
        "run_id": f"run-{ordinal:04d}",
        "grant_id": f"grant-{ordinal:04d}",
        "root_flow_id": f"root-{ordinal:04d}",
        "domain": "domain-fictional",
        "grant_ordinal": ordinal,
        "publication_effect_digest": f"effect-{ordinal:04d}",
    }
    values.update(overrides)
    return ServingCandidate(**values)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("run_id", "other-run"),
        ("grant_id", "other-grant"),
        ("grant_ordinal", 8),
        ("domain", "other-domain"),
    ],
)
def test_serving_authority_identity_equality_refuses_each_mutation(
    field: str, value: object
) -> None:
    expected = ServingAuthorityIdentity("run-0007", "grant-0007", 7, "domain-fictional")
    actual = replace(expected, **{field: value})

    with pytest.raises(ValueError, match="serving_authority_identity_mismatch"):
        require_serving_authority_identity(expected, actual)


def test_serving_candidates_refuse_malformed_duplicate_and_oversize_rosters() -> None:
    with pytest.raises(ValueError, match="identity"):
        validate_serving_candidates((_serving_candidate(1, run_id=""),))
    with pytest.raises(ValueError, match="unique"):
        validate_serving_candidates((_serving_candidate(1), _serving_candidate(1, run_id="other")))
    with pytest.raises(ValueError, match="dedicated protocol bound"):
        validate_serving_candidates(tuple(_serving_candidate(index) for index in range(513)))
    with pytest.raises(ValueError, match="worker nodes"):
        validate_serving_candidates((_serving_candidate(1, bake_state=NodeState.READY),))


def test_an_open_lower_complete_obligation_is_a_gap_and_a_settled_candidate_closes_it() -> None:
    newest = _serving_candidate(2)
    lower = CompleteObligation("run-0001", newest.domain, "grant-0001", 1)
    assert serving_gap((lower,), (newest,)) == lower
    assert serving_gap((lower,), (_serving_candidate(1), newest)) is None


def test_a_candidate_run_does_not_hide_a_lower_obligation_with_mismatched_authority() -> None:
    older = _serving_candidate(1)
    newest = _serving_candidate(2)
    mismatched_grant = CompleteObligation(older.run_id, older.domain, "grant-old", 1)
    mismatched_ordinal = CompleteObligation(older.run_id, older.domain, older.grant_id, 0)
    assert serving_gap((mismatched_grant,), (older, newest)) == mismatched_grant
    assert serving_gap((mismatched_ordinal,), (older, newest)) == mismatched_ordinal


def test_ordinal_collision_is_fatal_and_newest_winner_is_deterministic() -> None:
    candidates = (_serving_candidate(7), _serving_candidate(9), _serving_candidate(8))
    assert newest_eligible_candidate(tuple(reversed(candidates))) == _serving_candidate(9)
    with pytest.raises(ValueError, match="unique"):
        newest_eligible_candidate((_serving_candidate(9), _serving_candidate(9, run_id="x")))


def test_election_transactions_have_fixed_final_member_counts() -> None:
    vector = ServingFrontierVector(
        (0,) * 32,
        (97, *(0 for _ in range(31))),
        (97, *(0 for _ in range(31))),
    )
    assert len(vector.digest) == 64
    assert election_transaction_member_count() == 51
    assert election_transaction_member_count(successor=False) == 42


# --- Rolling election guards: predicate-specific positive/negative pairs ------------------------


def _vector(
    revisions: tuple[int, ...] = (0,) * 32,
    visible: tuple[int, ...] = (0,) * 32,
    eligible: tuple[int, ...] = (0,) * 32,
) -> ServingFrontierVector:
    return ServingFrontierVector(revisions, visible, eligible)


@pytest.mark.parametrize("length", [31, 33])
@pytest.mark.parametrize("field", ["revisions", "visible", "eligible"])
def test_a_frontier_vector_refuses_any_shard_count_but_the_protocol_32(
    field: str, length: int
) -> None:
    with pytest.raises(ValueError, match="every protocol shard"):
        _vector(**{field: (0,) * length})


@pytest.mark.parametrize("field", ["revisions", "visible", "eligible"])
def test_a_frontier_vector_refuses_a_negative_revision_or_count(field: str) -> None:
    with pytest.raises(ValueError, match="cannot be negative"):
        _vector(**{field: (-1, *(0,) * 31)})


def test_a_frontier_vector_accepts_eligible_below_visible_and_refuses_eligible_above() -> None:
    lower = _vector(visible=(2, *(0,) * 31), eligible=(1, *(0,) * 31))
    assert (lower.visible_count, lower.eligible_count) == (2, 1)
    with pytest.raises(ValueError, match="eligible <= visible"):
        _vector(visible=(1, *(0,) * 31), eligible=(2, *(0,) * 31))


def test_a_full_512_candidate_roster_is_accepted_at_its_bound() -> None:
    roster = tuple(_serving_candidate(index) for index in range(512))
    assert len(validate_serving_candidates(roster)) == 512


def test_a_roster_spanning_two_domains_is_refused() -> None:
    with pytest.raises(ValueError, match="exactly one domain"):
        validate_serving_candidates(
            (_serving_candidate(1), _serving_candidate(2, domain="domain-other"))
        )


def test_ordinal_zero_is_eligible_and_a_negative_ordinal_is_refused() -> None:
    assert validate_serving_candidates((_serving_candidate(0),)) == (_serving_candidate(0),)
    with pytest.raises(ValueError, match="non-negative and unique"):
        validate_serving_candidates((_serving_candidate(-1),))


@pytest.mark.parametrize("state", [state for state in RunState if state is not RunState.GRANTED])
def test_every_non_granted_candidate_run_state_is_refused(state: RunState) -> None:
    with pytest.raises(ValueError, match="must be GRANTED"):
        validate_serving_candidates((_serving_candidate(1, run_state=state),))


@pytest.mark.parametrize("field", ["arm_matches", "complete_matches"])
def test_a_candidate_missing_either_arm_or_complete_authority_is_refused(field: str) -> None:
    with pytest.raises(ValueError, match="ARM and COMPLETE"):
        validate_serving_candidates((_serving_candidate(1, **{field: False}),))


@pytest.mark.parametrize(
    "state", [state for state in NodeState if state is not NodeState.SUCCEEDED]
)
def test_every_unsettled_candidate_publication_state_is_refused(state: NodeState) -> None:
    with pytest.raises(ValueError, match="publication must be SUCCEEDED"):
        validate_serving_candidates((_serving_candidate(1, publish_state=state),))


@pytest.mark.parametrize(
    ("field", "required"),
    [("precompute_state", NodeState.WAITING_INPUT), ("bake_state", NodeState.PENDING)],
)
def test_every_started_candidate_worker_node_state_is_refused(
    field: str, required: NodeState
) -> None:
    for state in NodeState:
        candidate = _serving_candidate(1, **{field: state})
        if state is required:
            assert validate_serving_candidates((candidate,)) == (candidate,)
            continue
        with pytest.raises(ValueError, match="worker nodes"):
            validate_serving_candidates((candidate,))


@pytest.mark.parametrize("field", ["runtime_state_present", "binding_present"])
def test_a_candidate_holding_either_runtime_or_binding_authority_is_refused(field: str) -> None:
    with pytest.raises(ValueError, match="runtime or binding"):
        validate_serving_candidates((_serving_candidate(1, **{field: True}),))


def test_only_same_domain_lower_obligations_are_gaps() -> None:
    newest = _serving_candidate(2)
    # A runtime-built domain is equal to, never identical with, the candidate's literal.
    same = CompleteObligation("run-0001", "".join(["domain-", "fictional"]), "grant-0001", 1)
    assert same.domain is not newest.domain
    assert serving_gap((same,), (newest,)) == same
    for domain in ("aaa-domain", "zzz-domain"):
        foreign = CompleteObligation("run-0001", domain, "grant-0001", 1)
        assert serving_gap((foreign,), (newest,)) is None


def test_duplicate_open_obligation_ordinals_are_fatal() -> None:
    newest = _serving_candidate(3)
    walk = (
        CompleteObligation("run-0001", newest.domain, "grant-0001", 1),
        CompleteObligation("run-other", newest.domain, "grant-other", 1),
    )
    with pytest.raises(ValueError, match="collide on a grant ordinal"):
        serving_gap(walk, (newest,))


def test_more_than_256_unique_open_obligations_compare_counts_by_value() -> None:
    # Counts above CPython's small-int cache are distinct objects, so only == is correct here.
    newest = _serving_candidate(300)
    walk = tuple(
        CompleteObligation(f"run-{ordinal:04d}", newest.domain, f"grant-{ordinal:04d}", ordinal)
        for ordinal in range(300)
    )
    assert serving_gap(walk, (newest,)) == walk[0]


def test_obligations_at_or_above_the_winner_ordinal_are_never_gaps() -> None:
    newest = _serving_candidate(2)
    equal = CompleteObligation("run-other", newest.domain, "grant-other", 2)
    higher = CompleteObligation("run-0003", newest.domain, "grant-0003", 3)
    assert serving_gap((equal,), (newest,)) is None
    assert serving_gap((higher,), (newest,)) is None


def test_the_lowest_ordinal_gap_is_selected_deterministically() -> None:
    newest = _serving_candidate(5)
    walk = tuple(
        CompleteObligation(f"run-{ordinal:04d}", newest.domain, f"grant-{ordinal:04d}", ordinal)
        for ordinal in (3, 1, 4)
    )
    assert serving_gap(walk, (newest,)) == walk[1]
    assert serving_gap(tuple(reversed(walk)), (newest,)) == walk[1]


# --- The persistent SERVING_FRONTIER# shard mapping, pinned by literal values ------------------


@pytest.mark.parametrize(
    ("run_id", "shard"),
    [
        ("run-0001", 6),
        ("run-0002", 18),
        ("run-0003", 31),
        ("run-0004", 15),
        ("run-0005", 23),
        ("run-0006", 26),
        ("run-0007", 16),
        ("run-0008", 23),
    ],
)
def test_the_frontier_shard_mapping_is_pinned_for_fixed_run_ids(run_id: str, shard: int) -> None:
    assert serving_frontier_shard(run_id) == shard


def test_a_blank_run_id_has_no_frontier_shard() -> None:
    with pytest.raises(ValueError, match="requires a run id"):
        serving_frontier_shard("")


@pytest.mark.parametrize("count", [20, 24])
def test_a_non_power_of_two_shard_count_is_refused(
    monkeypatch: pytest.MonkeyPatch, count: int
) -> None:
    monkeypatch.setattr(
        "domain.operations.execution_graph_dispatch._SERVING_FRONTIER_SHARDS", count
    )
    with pytest.raises(ValueError, match="power of two"):
        serving_frontier_shard("run-0001")


@pytest.mark.parametrize(
    ("count", "expected"),
    [
        (1, {"run-0001": 0, "run-0003": 0, "run-0004": 0}),
        (64, {"run-0001": 12, "run-0003": 63, "run-0004": 31}),
    ],
)
def test_other_power_of_two_shard_counts_use_their_own_leading_bits(
    monkeypatch: pytest.MonkeyPatch, count: int, expected: dict[str, int]
) -> None:
    monkeypatch.setattr(
        "domain.operations.execution_graph_dispatch._SERVING_FRONTIER_SHARDS", count
    )
    assert {run_id: serving_frontier_shard(run_id) for run_id in expected} == expected


# --- H9 (ADR-140, B4): a FAILED roster task arms the finalizer once every task is terminal --


def _armable(name: str, state: NodeState, after: tuple[str, ...] = ()) -> Any:
    from types import SimpleNamespace  # noqa: PLC0415

    return SimpleNamespace(name=name, phase=NodePhase.POST_GRANT, state=state, after=after)


@pytest.mark.covers("ADR-140")
def test_h9_a_failed_task_among_terminal_tasks_arms_the_finalizer_and_a_live_one_does_not() -> None:
    from domain.operations.execution_graph_dispatch import ArmScope, arm_eligible  # noqa: PLC0415
    from domain.operations.generated_execution_graphs import TEMPLATES  # noqa: PLC0415

    (template,) = TEMPLATES
    finalize = _armable("bake_finalize", NodeState.PENDING, ("bake_task",))
    succeeded = [_armable(f"bake_task:campaigns#{day}d", NodeState.SUCCEEDED) for day in (1, 7)]

    failed = arm_eligible(
        ArmScope.SERVING,
        template,
        [*succeeded, _armable("bake_task:campaigns#30d", NodeState.FAILED), finalize],
    )
    live = arm_eligible(
        ArmScope.SERVING,
        template,
        [*succeeded, _armable("bake_task:campaigns#30d", NodeState.READY), finalize],
    )
    publish_failed = arm_eligible(
        ArmScope.SERVING,
        template,
        [
            _armable("publish", NodeState.FAILED),
            _armable("bake", NodeState.PENDING, ("publish",)),
        ],
    )

    assert [node.name for node in failed] == ["bake_finalize"]
    assert live == ()
    assert publish_failed == ()
