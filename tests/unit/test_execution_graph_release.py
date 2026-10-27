# SPDX-License-Identifier: MIT-0
"""Release (S4): BF-1, BF-8, V-16, V-28, V-29, apply, the re-drive and the one serial order.

Every test drives the real release adapter over `FakeDynamo`, so each refusal is the adapter's
own condition failing, and each "no effect" is read back from the table.
"""

from __future__ import annotations

import copy
import random
from dataclasses import dataclass, field, replace
from datetime import timedelta
from functools import partial
from types import SimpleNamespace
from typing import Any, cast

import pytest
from execution_graph_dispatch_support import (
    NOW,
    UNIT,
    now_text,
    seed_run,
    write_int,
    write_text,
)
from execution_graph_release_support import (
    WINDOW,
    FakeLegacy,
    bake_member,
    context,
    granted_run,
    granting_specs,
    head_item,
    keys,
    release_store,
    reserve_winner,
    seed_granting_run,
    settle_publish,
    unarmed_winner,
)
from execution_graph_support import FakeDynamo, FakeManifests

from adapters.inbound.run_requests.service import (
    AdmissionDependencies,
    RunRequest,
    admit_run_request,
)
from adapters.jobs.execution_graph.commands import (
    OperatorCommand,
    RunCommandKind,
    run_operator_command,
)
from adapters.jobs.execution_graph.instantiate import InstantiateRequest, instantiate_run
from adapters.jobs.execution_graph.promoter import PromoterContext, PromotionCounts, promote_unit
from adapters.jobs.execution_graph.release import (
    ReleaseContext,
    ReleaseCounts,
    ReleaseOutcome,
    apply_run,
    contribute,
    issue_grant,
    post_grant_specs,
    release_run,
    release_shard,
    settle_unarmed_cohort,
    try_complete_granted,
)
from adapters.outbound.dynamodb.execution_graph import DynamoExecutionGraphStore, due_shard
from adapters.outbound.dynamodb.execution_graph_dispatch import graph_envelope
from adapters.outbound.dynamodb.execution_graph_release import head_epoch
from adapters.outbound.telemetry.reporting import LogSeverity, Report, report_error
from domain.operations.execution_graph_dispatch import (
    RUN_GRANT_PENDING_APPLY,
    RUN_STATE_CHANGED,
    GraphGuardError,
    NodePhase,
    NodeState,
    PromoterBranch,
)
from domain.operations.execution_graph_release import (
    INGESTION_RELEASE_DOMAIN,
    INPUT_REVISION_SUPERSEDED,
    POST_GRANT_INVARIANT,
    RELEASE_GRANT_RESERVATION_PROOF_REQUIRED,
    CohortContribution,
    CohortMember,
    CohortOutcome,
    ReleaseRefusedError,
    UnarmedPrecomputeRefusedError,
    UnarmedPrecomputeView,
    binding_authorizes_serving,
    consumer_effect_record,
    release_conflict_domain,
    reservation_set,
    serving_generation,
    serving_withheld,
    settlement_for,
    unarmed_precompute_contribution,
)
from domain.operations.ingestion_root import window_identity
from domain.operations.run_request import RunState
from domain.operations.status import canonical_authority_digest
from ports.outbound.execution_graph_dispatch import GraphStoreUnavailableError
from ports.outbound.execution_graph_release import GrantRecord, ServingMutationResult

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-074")]

R1 = NOW - timedelta(hours=2)
R2 = NOW - timedelta(hours=1)
DOMAIN = INGESTION_RELEASE_DOMAIN


def _run_item(fake: FakeDynamo, run: str) -> dict[str, Any]:
    return fake.items[(f"RUN#{run}", "RUN")]


def _prepare(fake: FakeDynamo, run: str) -> int:
    """Acquire the domain ordinal, reserve every target and seal the proof (no grant)."""
    store = release_store(fake)
    record = store.read_run(run)
    assert record is not None
    order = store.read_release_order(DOMAIN)
    nodes = store.read_nodes(run)
    targets = tuple(sorted({n.lock_key for n in nodes if n.name != "grant_apply"}))
    reservation = reservation_set(run, DOMAIN, order.next_ordinal, targets)
    store.acquire_ordinal(order, record, reservation)
    for target in targets:
        store.reserve_target(reservation, target)
    store.seal_proof(reservation)
    return reservation.ordinal


def _grant(fake: FakeDynamo, run: str, legacy: FakeLegacy | None = None) -> str:
    ordinal = _prepare(fake, run)
    record = release_store(fake).read_run(run)
    assert record is not None
    grant = issue_grant(
        context(fake, legacy), run, ordinal=ordinal, expected_revision=record.revision
    )
    return grant.grant_id


# --- BF-1: the one reservation-first grant path ------------------------------------------------


def _release_writes(fake: FakeDynamo) -> list[tuple[str, str]]:
    return (
        keys(fake, "GRANT#") + keys(fake, "HEAD#") + keys(fake, "LINEAGE#") + keys(fake, "BLOCK#")
    )


def test_bf1_a_grant_without_the_active_ordinal_is_refused_with_no_effect() -> None:
    fake = FakeDynamo()
    seeded = seed_granting_run(fake, tables=("orders", "returns"))
    before = copy.deepcopy(fake.items)

    for ordinal in (None, 1):
        with pytest.raises(ReleaseRefusedError) as refused:
            issue_grant(
                context(fake),
                seeded.record.run_id,
                ordinal=ordinal,
                expected_revision=seeded.record.revision,
            )
        assert refused.value.signal == RELEASE_GRANT_RESERVATION_PROOF_REQUIRED

    assert fake.items == before
    assert _release_writes(fake) == []
    assert keys(fake, "TARGET#") == []


def test_bf1_a_grant_without_a_complete_proof_is_refused_with_no_effect() -> None:
    fake = FakeDynamo()
    seeded = seed_granting_run(fake, tables=("orders", "returns"))
    run = seeded.record.run_id
    store = release_store(fake)
    order = store.read_release_order(DOMAIN)
    reservation = reservation_set(
        run, DOMAIN, order.next_ordinal, ("fictional-db.orders", "fictional-db.returns")
    )
    store.acquire_ordinal(order, seeded.record, reservation)
    store.reserve_target(reservation, "fictional-db.orders")  # one of two targets only
    before = copy.deepcopy(fake.items)

    with pytest.raises(ReleaseRefusedError) as refused:
        issue_grant(context(fake), run, ordinal=1, expected_revision=seeded.record.revision)

    assert refused.value.signal == RELEASE_GRANT_RESERVATION_PROOF_REQUIRED
    assert fake.items == before
    assert _release_writes(fake) == []
    # The proof cannot be sealed over an incomplete roster either.
    with pytest.raises(GraphGuardError) as guard:
        store.seal_proof(reservation)
    assert guard.value.signal == RELEASE_GRANT_RESERVATION_PROOF_REQUIRED


def test_bf1_a_moved_run_revision_is_refused() -> None:
    fake = FakeDynamo()
    seeded = seed_granting_run(fake)
    ordinal = _prepare(fake, seeded.record.run_id)
    with pytest.raises(ReleaseRefusedError) as refused:
        issue_grant(
            context(fake),
            seeded.record.run_id,
            ordinal=ordinal,
            expected_revision=seeded.record.revision - 1,
        )
    assert refused.value.signal == RELEASE_GRANT_RESERVATION_PROOF_REQUIRED
    assert _release_writes(fake) == []


def test_bf1_positive_one_grant_one_head_and_lineage_advance_and_replay_returns_it() -> None:
    fake = FakeDynamo()
    seeded = seed_granting_run(fake, tables=("orders", "returns"))
    run = seeded.record.run_id

    assert release_run(context(fake), run) is ReleaseOutcome.APPLIED

    grants = keys(fake, "GRANT#")
    assert len(grants) == 1
    record = release_store(fake).read_run(run)
    assert record is not None and record.state is RunState.GRANTED
    assert record.grant_id is not None
    replay = issue_grant(context(fake), run, ordinal=None, expected_revision=None)
    assert replay.grant_id == record.grant_id
    assert release_run(context(fake), run) is ReleaseOutcome.ALREADY_APPLIED
    assert keys(fake, "GRANT#") == grants
    head = head_item(fake)
    assert head is not None and head["head_epoch"] == {"N": str(head_epoch(R2))}
    lineage = fake.items[(f"LINEAGE#{UNIT}", "HEAD")]
    assert lineage["lineage_seq"] == {"N": "1"}
    assert keys(fake, f"LINEAGE#{UNIT}") == [
        (f"LINEAGE#{UNIT}", "HEAD"),
        (f"LINEAGE#{UNIT}", "STEP#0000000001"),
    ]
    assert keys(fake, "RELEASE#") == [] and keys(fake, "APPLY#") == []


# --- Apply: release point 2, POST_GRANT nodes and the re-drive ---------------------------------


def test_apply_frees_the_slot_wakes_promote_idles_the_domain_and_settles_grant_apply() -> None:
    fake = FakeDynamo()
    seeded = seed_granting_run(fake)
    run = seeded.record.run_id
    assert release_run(context(fake), run) is ReleaseOutcome.APPLIED

    slot = fake.items[(f"SLOT#{UNIT}", "SLOT")]
    assert "active_run_id" not in slot
    assert fake.items[(due_shard(UNIT), f"PROMOTE#{UNIT}")]["wake_seq"] == {"N": "1"}
    order = release_store(fake).read_release_order(DOMAIN)
    assert (order.active_ordinal, order.active_run_id, order.next_ordinal) == (None, None, 2)
    grant_apply = fake.items[(f"RUN#{run}", "NODE#grant_apply")]
    assert grant_apply["state"] == {"S": "SUCCEEDED"}


def test_apply_writes_the_post_grant_nodes_bound_to_the_exact_grant_and_revision() -> None:
    fake = FakeDynamo()
    seeded = seed_granting_run(fake)
    run = seeded.record.run_id
    assert release_run(context(fake), run) is ReleaseOutcome.APPLIED
    store = release_store(fake)
    record = store.read_run(run)
    assert record is not None and record.grant_id is not None
    post = {n.name: n for n in store.read_nodes(run) if n.phase is NodePhase.POST_GRANT}

    assert sorted(post) == ["bake", "precompute", "publish"]
    for node in post.values():
        assert record.grant_id in node.body and R2.isoformat() in node.body
    # S6 §2.2: apply arms only `publish` (its one predecessor is `grant_apply`); `bake` is
    # written PENDING for the arm pass and, under DB25, `precompute` WAITING_INPUT for the
    # roster effect join, each with no token, lock or outbox.
    armed = post["publish"]
    assert armed.state is NodeState.READY
    assert (f"TOKEN#{armed.node_id}", "TOKEN") in fake.items
    assert (f"LOCK#{armed.lock_key}", "LOCK") in fake.items
    assert (due_shard(UNIT), f"OUTBOX#{armed.node_id}") in fake.items
    assert fake.items[(f"RUN#{run}", "NODE#publish")]["armed_at"] == {"S": now_text()}
    for name, state in (("precompute", NodeState.WAITING_INPUT), ("bake", NodeState.PENDING)):
        pending = post[name]
        assert pending.state is state
        assert (f"TOKEN#{pending.node_id}", "TOKEN") not in fake.items
        assert (f"LOCK#{pending.lock_key}", "LOCK") not in fake.items
        assert (due_shard(UNIT), f"OUTBOX#{pending.node_id}") not in fake.items
    grant = store.read_grant(record.grant_id)
    assert grant is not None
    # Stable retry identity: a retried apply derives byte-identical nodes and dispatch ids.
    again = {
        spec.name: graph_envelope(run, spec).dispatch_id for spec in post_grant_specs(record, grant)
    }
    assert again == {name: node.dispatch_id for name, node in post.items()}


def test_instantiation_never_writes_a_post_grant_node() -> None:
    fake = FakeDynamo()
    seeded = seed_run(fake, retrieved=R2)
    name = seeded.record.run_id
    fake.items[(f"RUN#{name}", "RUN")]["window"] = write_text(WINDOW)

    def planner(run: Any, receipt: Any) -> Any:
        del receipt
        grant = GrantRecord(
            "g" * 64, run.run_id, DOMAIN, UNIT, 1, "s", "d", R2.isoformat(), None, 1, now_text()
        )
        return (*granting_specs(run.run_id, "orders"), *post_grant_specs(run, grant))

    instantiate_run(release_store(fake), InstantiateRequest(name, planner, now_text))

    names = {n.name for n in release_store(fake).read_nodes(name)}
    assert names == {"write_orders", "grant_apply"}


def test_a_committed_grant_that_did_not_apply_is_re_driven_by_its_apply_obligation() -> None:
    fake = FakeDynamo()
    seeded = seed_granting_run(fake)
    run = seeded.record.run_id
    grant = _grant(fake, run)
    assert keys(fake, "APPLY#") == [(due_shard(UNIT), f"APPLY#{run}")]
    assert keys(fake, "RELEASE#") == []
    record = release_store(fake).read_run(run)
    assert record is not None and record.state is RunState.OPEN and record.grant_id == grant

    counts: ReleaseCounts = release_shard(context(fake), due_shard(UNIT))

    assert counts.outcomes == {"applied": 1}
    record = release_store(fake).read_run(run)
    assert record is not None and record.state is RunState.GRANTED
    assert keys(fake, "APPLY#") == []


def _bind(fake: FakeDynamo, run: str, outcome: str = "complete") -> None:
    """Contribute both siblings with the shape that gives each `serving_outcome`.

    The run first becomes its cohort's reserved winner through the real store transitions
    (settled publish, then the election CAS), so `contribute` reads exact authority.
    """
    settle_publish(fake, run)
    reserve_winner(fake, run)
    store = release_store(fake)
    record = store.read_run(run)
    assert record is not None and record.grant_id is not None
    grant, root = record.grant_id, record.root_flow_id
    revision = serving_generation(grant)
    missing = (("campaigns#7d", "bake_budget_exhausted"),)
    bake = {
        "complete": CohortContribution(
            grant, CohortMember.BAKE, CohortOutcome.SUCCEEDED, revision, NOW, root
        ),
        "partial": CohortContribution(
            grant, CohortMember.BAKE, CohortOutcome.UNAVAILABLE, revision, NOW, root, missing
        ),
        "unavailable": CohortContribution(
            grant, CohortMember.BAKE, CohortOutcome.UNAVAILABLE, None, NOW, root, missing
        ),
        "no_eligible": CohortContribution(
            grant, CohortMember.BAKE, CohortOutcome.NO_ELIGIBLE, None, NOW, root
        ),
    }[outcome]
    precompute = CohortContribution(
        grant, CohortMember.PRECOMPUTE, CohortOutcome.NO_ELIGIBLE, None, NOW, root
    )
    contribute(store, precompute, record)
    contribute(store, bake, record)


def _terminal(fake: FakeDynamo, run: str) -> None:
    for name in ("publish", "precompute", "bake"):
        fake.items[(f"RUN#{run}", f"NODE#{name}")]["state"] = write_text("SUCCEEDED")


def test_a_granted_run_succeeds_once_every_post_grant_node_is_terminal() -> None:
    fake = FakeDynamo()
    seeded = seed_granting_run(fake)
    run = seeded.record.run_id
    release_run(context(fake), run)
    store = release_store(fake)
    _bind(fake, run)
    assert try_complete_granted(context(fake), run) is None
    assert (due_shard(UNIT), f"COMPLETE#{run}") in fake.items
    _terminal(fake, run)
    assert try_complete_granted(context(fake), run) is ServingMutationResult.COMMITTED
    record = store.read_run(run)
    assert record is not None and record.state is RunState.SUCCEEDED
    # S6 §2.2: completion is the one writer that deletes the run's `COMPLETE#` obligation.
    assert (due_shard(UNIT), f"COMPLETE#{run}") not in fake.items


# --- S6 §2.8 (AGG3-7): completion by the release sweep -------------------------------------


@pytest.mark.parametrize("outcome", ["complete", "partial", "unavailable", "no_eligible"])
def test_agg3_7_the_release_sweep_completes_with_each_serving_outcome(outcome: str) -> None:
    fake = FakeDynamo()
    seeded = seed_granting_run(fake)
    run = seeded.record.run_id
    release_run(context(fake), run)
    _bind(fake, run, outcome)
    _terminal(fake, run)

    counts = release_shard(context(fake), due_shard(UNIT))

    assert (counts.completed, counts.completion_pending) == (1, 0)
    item = _run_item(fake, run)
    assert (item["state"]["S"], item["serving_outcome"]["S"]) == ("SUCCEEDED", outcome)
    assert keys(fake, "COMPLETE#") == []


def test_agg3_7_a_failed_completion_is_retried_by_the_next_sweep() -> None:
    fake = FakeDynamo()
    seeded = seed_granting_run(fake)
    run = seeded.record.run_id
    release_run(context(fake), run)
    _bind(fake, run)
    _terminal(fake, run)

    def move(table: FakeDynamo) -> None:  # the run's revision moves between read and commit
        item = table.items[(f"RUN#{run}", "RUN")]
        item["revision"] = write_int(int(item["revision"]["N"]) + 1)

    fake.before_transaction = move
    # DB25: the winner's reservation already deleted its ARM#, so the drain checks below are
    # the binding, the marker and COMPLETE#.
    assert (due_shard(UNIT), f"ARM#{run}") not in fake.items
    first = release_shard(context(fake), due_shard(UNIT))

    assert (first.completed, first.completion_pending) == (0, 1)
    assert _run_item(fake, run)["state"]["S"] == "GRANTED"
    assert keys(fake, "COMPLETE#") == [(due_shard(UNIT), f"COMPLETE#{run}")]
    # The refused completion drained nothing: the direct drain shares its transaction.
    record = release_store(fake).read_run(run)
    assert record is not None and record.grant_id is not None
    assert "terminal_operation" not in fake.items[(f"SERVING#{record.grant_id}", "BINDING")]
    assert (f"SERVING_PUBLICATION#{run}", "MARKER") in fake.items
    second = release_shard(context(fake), due_shard(UNIT))
    assert second.completed == 1
    assert _run_item(fake, run)["state"]["S"] == "SUCCEEDED"


def test_agg3_7_control_with_complete_removed_the_run_stays_granted() -> None:
    fake = FakeDynamo()
    seeded = seed_granting_run(fake)
    run = seeded.record.run_id
    release_run(context(fake), run)
    _bind(fake, run)
    _terminal(fake, run)
    fake.items.pop((due_shard(UNIT), f"COMPLETE#{run}"))

    counts = release_shard(context(fake), due_shard(UNIT))

    assert (counts.completed, counts.completion_pending) == (0, 0)
    assert _run_item(fake, run)["state"]["S"] == "GRANTED"  # the obligation is load-bearing


def test_agg3_7_no_completion_without_a_binding() -> None:
    fake = FakeDynamo()
    seeded = seed_granting_run(fake)
    run = seeded.record.run_id
    release_run(context(fake), run)
    _terminal(fake, run)

    counts = release_shard(context(fake), due_shard(UNIT))

    assert (counts.completed, counts.completion_pending) == (0, 1)
    assert _run_item(fake, run)["state"]["S"] == "GRANTED"
    assert "serving_outcome" not in _run_item(fake, run)


def test_agg3_7_a_binding_gone_at_commit_refuses_the_completion() -> None:
    fake = FakeDynamo()
    seeded = seed_granting_run(fake)
    run = seeded.record.run_id
    release_run(context(fake), run)
    _bind(fake, run)
    _terminal(fake, run)
    record = release_store(fake).read_run(run)
    assert record is not None and record.grant_id is not None
    grant = record.grant_id

    def unbind(table: FakeDynamo) -> None:
        table.items.pop((f"SERVING#{grant}", "BINDING"))

    fake.before_transaction = unbind

    assert try_complete_granted(context(fake), run) is None
    assert _run_item(fake, run)["state"]["S"] == "GRANTED"


# --- S6: grant_ordinal on RUN# (AGG4-16), and apply's 15 members with COMPLETE# --------------


def test_agg4_16_the_grant_writes_grant_ordinal_on_run_equal_to_the_grants_ordinal() -> None:
    fake = FakeDynamo()
    seeded = seed_granting_run(fake)
    run = seeded.record.run_id
    # Control: before the grant the run carries no ordinal.
    assert seeded.record.grant_ordinal is None
    assert "grant_ordinal" not in _run_item(fake, run)

    grant_id = _grant(fake, run)

    grant = fake.items[(f"GRANT#{grant_id}", "GRANT")]
    assert _run_item(fake, run)["grant_ordinal"] == grant["active_ordinal"] == {"N": "1"}
    record = release_store(fake).read_run(run)
    assert record is not None and record.grant_ordinal == 1


def test_apply_commits_bootstrap_members_and_puts_complete_with_the_grants_order() -> None:
    fake = FakeDynamo()
    seeded = seed_granting_run(fake)
    run = seeded.record.run_id
    _run_item(fake, run)["root_clock_started_at"] = write_text(R1.isoformat())
    grant_id = _grant(fake, run)

    assert apply_run(context(fake), run) is ReleaseOutcome.APPLIED

    assert len(fake.transactions[-1]) == 48
    assert not any(
        member.get("ConditionCheck", {}).get("Key", {}).get("pk")
        == {"S": "MIGRATION#GRAPH_ROLLING_FRESHNESS"}
        for member in fake.transactions[-1]
    )
    complete = fake.items[(due_shard(UNIT), f"COMPLETE#{run}")]
    assert complete["item_kind"] == {"S": "complete"}
    assert complete["domain"] == {"S": DOMAIN}
    assert complete["grant_id"] == {"S": grant_id}
    assert complete["grant_ordinal"] == {"N": "1"}
    assert complete["root_clock_started_at"] == {"S": R1.isoformat()}


# --- V-16: a superseded run leaves no pointer --------------------------------------------------


@pytest.mark.parametrize("state", [RunState.FENCING, RunState.SUPERSEDED, RunState.CANCELLED])
def test_v16_units_completed_by_a_superseded_run_leave_no_pointer(state: RunState) -> None:
    fake = FakeDynamo()
    seeded = seed_granting_run(fake)
    run = seeded.record.run_id
    _run_item(fake, run)["state"] = write_text(state.value)
    _run_item(fake, run)["fence_reason"] = write_text("input_invalidated")

    assert release_run(context(fake), run) is ReleaseOutcome.NOT_LIVE
    with pytest.raises(ReleaseRefusedError):
        issue_grant(context(fake), run, ordinal=1, expected_revision=seeded.record.revision)

    assert _release_writes(fake) == []
    assert keys(fake, "RELEASE_ORDER#") == []
    assert (due_shard(UNIT), f"RELEASE#{run}") not in fake.items  # a terminal trigger retires
    assert [n for n in release_store(fake).read_nodes(run) if n.phase is NodePhase.POST_GRANT] == []
    assert "active_run_id" in fake.items[(f"SLOT#{UNIT}", "SLOT")]  # untouched by release


def test_v16_positive_a_succeeded_run_publishes() -> None:
    fake = FakeDynamo()
    seeded = seed_granting_run(fake)
    assert release_run(context(fake), seeded.record.run_id) is ReleaseOutcome.APPLIED
    assert head_item(fake) is not None


# --- V-28: the grant-time head check -----------------------------------------------------------


def _seed_head(fake: FakeDynamo, instant: Any, unit: str = UNIT) -> None:
    fake.items[(f"HEAD#{unit}", "HEAD")] = {
        "pk": write_text(f"HEAD#{unit}"),
        "sk": write_text("HEAD"),
        "retrieved_at": write_text(instant.isoformat()),
        "head_epoch": {"N": str(head_epoch(instant))},
    }


def _newer_head(table: FakeDynamo) -> None:
    _seed_head(table, R2)


def _hook(action: Any, *arguments: Any) -> Any:
    def run(_table: FakeDynamo) -> None:
        action(*arguments)

    return run


def _assert_superseded(fake: FakeDynamo, run: str) -> None:
    item = _run_item(fake, run)
    assert item["state"] == {"S": "FENCING"}
    assert item["fence_reason"] == {"S": "older_than_current"}
    assert "grant_id" not in item
    assert "active_run_id" not in fake.items[(f"SLOT#{UNIT}", "SLOT")]
    assert fake.items[(due_shard(UNIT), f"PROMOTE#{UNIT}")]["wake_seq"] == {"N": "1"}
    assert keys(fake, "GRANT#") == [] and keys(fake, "LINEAGE#") == []
    order = release_store(fake).read_release_order(DOMAIN)
    assert order.active_run_id is None


def test_v28_a_grant_older_than_the_graph_head_fences_the_run_older_than_current() -> None:
    fake = FakeDynamo()
    seeded = seed_granting_run(fake, retrieved=R1)
    _seed_head(fake, R2)
    with pytest.raises(ReleaseRefusedError) as refused:
        issue_grant(context(fake), seeded.record.run_id, ordinal=None, expected_revision=None)
    assert refused.value.signal == INPUT_REVISION_SUPERSEDED

    assert release_run(context(fake), seeded.record.run_id) is ReleaseOutcome.SUPERSEDED

    _assert_superseded(fake, seeded.record.run_id)
    assert head_item(fake)["head_epoch"] == {"N": str(head_epoch(R2))}  # type: ignore[index]


def test_v28_a_grant_older_than_the_legacy_head_is_refused_the_same_way() -> None:
    fake = FakeDynamo()
    seeded = seed_granting_run(fake, retrieved=R1)
    legacy = FakeLegacy(heads={WINDOW: R2})
    assert release_run(context(fake, legacy), seeded.record.run_id) is ReleaseOutcome.SUPERSEDED
    _assert_superseded(fake, seeded.record.run_id)
    assert legacy.calls == [(WINDOW, R1)]
    assert head_item(fake) is None


def test_v28_a_race_that_raises_the_head_after_the_read_refuses_the_grant_transaction() -> None:
    fake = FakeDynamo()
    seeded = seed_granting_run(fake, retrieved=R1)
    ordinal = _prepare(fake, seeded.record.run_id)
    fake.before_transaction = _newer_head
    with pytest.raises(GraphGuardError) as guard:
        issue_grant(
            context(fake),
            seeded.record.run_id,
            ordinal=ordinal,
            expected_revision=seeded.record.revision,
        )
    assert guard.value.signal == INPUT_REVISION_SUPERSEDED
    assert keys(fake, "GRANT#") == [] and keys(fake, "LINEAGE#") == []


def test_v28_positive_a_retrieval_at_the_head_grants() -> None:
    fake = FakeDynamo()
    seeded = seed_granting_run(fake, retrieved=R2)
    _seed_head(fake, R2)
    assert release_run(context(fake), seeded.record.run_id) is ReleaseOutcome.APPLIED


def test_v28_an_injected_apply_time_refusal_is_a_post_grant_lineage_failure() -> None:
    fake = FakeDynamo()
    seeded = seed_granting_run(fake, retrieved=R1)
    run = seeded.record.run_id
    grant = _grant(fake, run)
    fake.before_transaction = _newer_head  # fault injection

    assert apply_run(context(fake), run) is ReleaseOutcome.LINEAGE_FAILED

    block = release_store(fake).read_block(DOMAIN)
    assert block is not None and block.reason == "lineage_failed_owner_review"
    assert block.grant_id == grant
    item = _run_item(fake, run)
    assert item["state"] == {"S": "FAILED"} and item["lineage_failed"] == {"BOOL": True}
    assert fake.items[(f"LINEAGE#{UNIT}", "HEAD")]["failed_candidate"] == {"S": grant}
    assert keys(fake, "APPLY#") == []
    assert head_item(fake)["head_epoch"] == {"N": str(head_epoch(R2))}  # type: ignore[index]
    # The block, not the ordinal, holds the domain: clearing it at owner review is enough.
    order = release_store(fake).read_release_order(DOMAIN)
    assert (order.active_ordinal, order.active_run_id) == (None, None)
    # No undefined state: a second pass changes nothing.
    before = copy.deepcopy(fake.items)
    assert apply_run(context(fake), run) is ReleaseOutcome.LINEAGE_FAILED
    assert fake.items == before


# --- BF-8: monotonic publication per conflict unit ---------------------------------------------


def _finish_post_grant(fake: FakeDynamo, run: str) -> None:
    """Settle a granted run's POST_GRANT nodes, releasing their locks and tokens."""
    store = release_store(fake)
    for node in store.read_nodes(run):
        if node.phase is NodePhase.POST_GRANT:
            fake.items[(f"RUN#{run}", f"NODE#{node.name}")]["state"] = write_text("SUCCEEDED")
            fake.items.pop((f"LOCK#{node.lock_key}", "LOCK"), None)
            fake.items.pop((f"TOKEN#{node.node_id}", "TOKEN"), None)


def test_a_newer_grant_waits_for_the_previous_bake_of_its_window_then_applies() -> None:
    fake = FakeDynamo()
    first = seed_granting_run(fake, retrieved=R1, fingerprint="1" * 64, request_id="request-0001")
    assert release_run(context(fake), first.record.run_id) is ReleaseOutcome.APPLIED
    second = seed_granting_run(fake, retrieved=R2, fingerprint="2" * 64, request_id="request-0002")

    assert release_run(context(fake), second.record.run_id) is ReleaseOutcome.POST_GRANT_LOCK_WAIT
    assert keys(fake, "APPLY#") == [(due_shard(UNIT), f"APPLY#{second.record.run_id}")]
    _finish_post_grant(fake, first.record.run_id)
    assert release_shard(context(fake), due_shard(UNIT)).outcomes == {"applied": 1}


def test_bf8_apply_r_then_r_plus_one_advances_once_each_and_r_after_r_plus_one_is_refused() -> None:
    fake = FakeDynamo()
    first = seed_granting_run(fake, retrieved=R1, fingerprint="1" * 64, request_id="request-0001")
    assert release_run(context(fake), first.record.run_id) is ReleaseOutcome.APPLIED
    _finish_post_grant(fake, first.record.run_id)
    second = seed_granting_run(fake, retrieved=R2, fingerprint="2" * 64, request_id="request-0002")
    assert release_run(context(fake), second.record.run_id) is ReleaseOutcome.APPLIED
    assert head_item(fake)["head_epoch"] == {"N": str(head_epoch(R2))}  # type: ignore[index]
    assert release_run(context(fake), second.record.run_id) is ReleaseOutcome.ALREADY_APPLIED

    late = seed_granting_run(fake, retrieved=R1, fingerprint="3" * 64, request_id="request-0003")
    assert release_run(context(fake), late.record.run_id) is ReleaseOutcome.SUPERSEDED
    assert head_item(fake)["head_epoch"] == {"N": str(head_epoch(R2))}  # type: ignore[index]
    assert len(keys(fake, "GRANT#")) == 2


def test_bf8_a_retrieval_of_another_window_on_the_same_table_is_not_refused() -> None:
    fake = FakeDynamo()
    newer = seed_granting_run(fake, retrieved=R2, fingerprint="1" * 64, request_id="request-0001")
    assert release_run(context(fake), newer.record.run_id) is ReleaseOutcome.APPLIED
    other_unit = canonical_authority_digest(["fictional-window", "amc", "other"])
    older = seed_granting_run(
        fake, retrieved=R1, fingerprint="2" * 64, request_id="request-0002", unit=other_unit
    )
    assert release_run(context(fake), older.record.run_id) is ReleaseOutcome.APPLIED
    assert head_item(fake, other_unit)["head_epoch"] == {"N": str(head_epoch(R1))}  # type: ignore[index]


# --- One serial order: the two-plan, two-target opposite-first race ----------------------------


def _tails(fake: FakeDynamo, target: str) -> list[str]:
    return [
        fake.items[(pk, sk)]["run_id"]["S"]
        for pk, sk in keys(fake, f"TARGET#{target}")
        if sk.startswith("RESERVATION#")
    ]


@pytest.mark.parametrize("race", [False, True])
def test_two_plans_on_two_targets_in_opposite_first_order_end_in_one_serial_order(
    race: bool,
) -> None:
    fake = FakeDynamo()
    unit_q = canonical_authority_digest(["fictional-window", "amc", "q"])
    p = seed_granting_run(fake, tables=("a", "b"), fingerprint="1" * 64, request_id="request-p")
    q = seed_granting_run(
        fake, tables=("b", "a"), fingerprint="2" * 64, request_id="request-q", unit=unit_q
    )
    p_run, q_run = p.record.run_id, q.record.run_id
    first, second = p_run, q_run
    if race:
        # Q's ordinal CAS commits between P's read of the idle head and P's own CAS.
        fake.before_transaction = partial(_prepare, run=q_run)
        assert release_run(context(fake), p_run) is ReleaseOutcome.WAITING_FOR_ORDER
        first, second = q_run, p_run

    assert release_run(context(fake), first) is ReleaseOutcome.APPLIED
    assert release_run(context(fake), second) is ReleaseOutcome.APPLIED

    assert _tails(fake, "fictional-db.a") == [first, second]
    assert _tails(fake, "fictional-db.b") == [first, second]
    assert release_conflict_domain("ingestion_manifest_to_served.v1") == DOMAIN


def test_a_second_run_waits_while_the_domain_ordinal_is_held() -> None:
    fake = FakeDynamo()
    unit_q = canonical_authority_digest(["fictional-window", "amc", "q"])
    p = seed_granting_run(fake, fingerprint="1" * 64, request_id="request-p")
    q = seed_granting_run(fake, fingerprint="2" * 64, request_id="request-q", unit=unit_q)
    _prepare(fake, p.record.run_id)
    before = keys(fake, "TARGET#")
    assert release_run(context(fake), q.record.run_id) is ReleaseOutcome.WAITING_FOR_ORDER
    assert keys(fake, "TARGET#") == before
    assert release_run(context(fake), p.record.run_id) is ReleaseOutcome.APPLIED
    assert release_run(context(fake), q.record.run_id) is ReleaseOutcome.APPLIED


# --- V-29: the grant-to-apply fence ------------------------------------------------------------

KEY_R1 = f"{WINDOW}retrieved={R1.strftime('%Y%m%dT%H%M%SZ')}/manifest.json"
KEY_R2 = f"{WINDOW}retrieved={R2.strftime('%Y%m%dT%H%M%SZ')}/manifest.json"
WINDOW_UNIT = window_identity(KEY_R1).conflict_unit


def _correction(request_id: str) -> RunRequest:
    return RunRequest(
        request_id=request_id,
        protocol_version="v1",
        caller_kind="on_demand",
        reason="data_correction",
        manifest_key=KEY_R2,
        version_id="version-2",
        requester="cognito-subject-digest-fictional",
        requester_is_iam=False,
        via_api_group=True,
    )


def _admission(fake: FakeDynamo) -> AdmissionDependencies:
    return AdmissionDependencies(
        store=DynamoExecutionGraphStore("execution-graph-probe-fixture", fake),  # type: ignore[arg-type]
        manifests=FakeManifests(retrieved_at=R2),  # type: ignore[arg-type]
        now=lambda: NOW,
        legacy=FakeLegacy(),
    )


def _seed_window_run(fake: FakeDynamo) -> str:
    seeded = seed_granting_run(fake, retrieved=R1, unit=WINDOW_UNIT, compute=True)
    return seeded.record.run_id


def _grant_now(fake: FakeDynamo, run: str, ordinal: int) -> None:
    record = release_store(fake).read_run(run)
    assert record is not None
    issue_grant(context(fake), run, ordinal=ordinal, expected_revision=record.revision)


def _command(kind: RunCommandKind, run: str, command_id: str) -> OperatorCommand:
    return OperatorCommand(
        kind, command_id, "arn:aws:iam::000000000000:user/operator", now_text, run
    )


SEEDS = range(100)


@pytest.mark.parametrize("seed", SEEDS)
def test_v29_a_grant_first_then_a_row_18_correction_queues_behind_it(seed: int) -> None:
    fake = FakeDynamo()
    run = _seed_window_run(fake)
    ordinal = _prepare(fake, run)
    between = random.Random(seed).random() < 0.5  # noqa: S311 -- seeded interleaving choice
    if between:
        fake.before_transaction = _hook(_grant_now, fake, run, ordinal)
    else:
        _grant_now(fake, run, ordinal)

    receipt = admit_run_request(_correction(f"request-c{seed:04d}"), _admission(fake))

    assert (receipt.decision, receipt.row) == ("QUEUED_LATEST", 19)
    assert fake.items[(f"SLOT#{WINDOW_UNIT}", "SLOT")]["active_run_id"] == {"S": run}
    assert release_run(context(fake), run) is ReleaseOutcome.APPLIED
    assert release_store(fake).read_block(DOMAIN) is None


@pytest.mark.parametrize("seed", SEEDS)
def test_v29_a_grant_first_then_pause_or_cancel_is_refused_pending_apply(seed: int) -> None:
    fake = FakeDynamo()
    run = _seed_window_run(fake)
    ordinal = _prepare(fake, run)
    rng = random.Random(seed)  # noqa: S311 -- seeded interleaving choice
    kind = rng.choice([RunCommandKind.PAUSE, RunCommandKind.CANCEL])
    if rng.random() < 0.5:
        fake.before_transaction = _hook(_grant_now, fake, run, ordinal)
        # The command reads the run before the grant; its own transaction then loses.
    else:
        _grant_now(fake, run, ordinal)
    before = copy.deepcopy(_run_item(fake, run))
    before.pop("grant_id", None)
    before.pop("granted_at", None)

    result = run_operator_command(release_store(fake), _command(kind, run, f"cmd-{seed:04d}"))

    assert not result.committed
    assert result.receipt.outcome == f"refused:{RUN_GRANT_PENDING_APPLY}"
    item = _run_item(fake, run)
    assert item["state"] == {"S": "OPEN"}
    assert item["dispatch_generation"] == before["dispatch_generation"]
    assert fake.items[(f"SLOT#{WINDOW_UNIT}", "SLOT")]["active_run_id"] == {"S": run}
    assert keys(fake, "COMMAND#") == [(f"COMMAND#cmd-{seed:04d}", "COMMAND")]
    assert release_run(context(fake), run) is ReleaseOutcome.APPLIED
    assert release_store(fake).read_block(DOMAIN) is None


@pytest.mark.parametrize("seed", SEEDS)
def test_v29_a_replace_or_cancel_first_then_the_grant_is_refused_on_run(seed: int) -> None:
    fake = FakeDynamo()
    run = _seed_window_run(fake)
    ordinal = _prepare(fake, run)
    rng = random.Random(seed)  # noqa: S311 -- seeded interleaving choice
    cancel = rng.random() < 0.5

    def first(_table: object = None) -> None:
        if cancel:
            assert run_operator_command(
                release_store(fake), _command(RunCommandKind.CANCEL, run, f"cmd-{seed:04d}")
            ).committed
        else:
            receipt = admit_run_request(_correction(f"request-c{seed:04d}"), _admission(fake))
            assert receipt.decision == "REPLACED_ON_INVALIDATION"

    if rng.random() < 0.5:
        fake.before_transaction = first
        with pytest.raises(GraphGuardError) as guard:
            _grant_now(fake, run, ordinal)
        assert guard.value.signal == RUN_STATE_CHANGED
    else:
        first()
        with pytest.raises(ReleaseRefusedError):
            _grant_now(fake, run, ordinal)

    assert keys(fake, "GRANT#") == [] and keys(fake, "LINEAGE#") == []
    assert head_item(fake, WINDOW_UNIT) is None
    assert release_run(context(fake), run) is ReleaseOutcome.NOT_LIVE
    assert release_store(fake).read_block(DOMAIN) is None
    # The refused run returned the domain ordinal it held and retired its trigger.
    order = release_store(fake).read_release_order(DOMAIN)
    assert (order.active_ordinal, order.active_run_id) == (None, None)
    assert (due_shard(WINDOW_UNIT), f"RELEASE#{run}") not in fake.items


def test_v29_positive_without_a_race_row_18_replaces_and_the_queued_correction_publishes() -> None:
    fake = FakeDynamo()
    run = _seed_window_run(fake)
    receipt = admit_run_request(_correction("request-replace"), _admission(fake))
    assert receipt.decision == "REPLACED_ON_INVALIDATION"

    fake2 = FakeDynamo()
    run2 = _seed_window_run(fake2)
    _grant_now(fake2, run2, _prepare(fake2, run2))
    queued = admit_run_request(_correction("request-queued"), _admission(fake2))
    assert queued.decision == "QUEUED_LATEST"
    assert release_run(context(fake2), run2) is ReleaseOutcome.APPLIED
    first_head = head_item(fake2, WINDOW_UNIT)
    assert first_head is not None and first_head["head_epoch"] == {"N": str(head_epoch(R1))}

    def plan(record: Any, receipt: Any) -> Any:
        del receipt
        return granting_specs(record.run_id, "orders")

    promoter = PromoterContext(release_store(fake2), plan, lambda: NOW)
    assert promote_unit(promoter, WINDOW_UNIT, PromotionCounts()) is PromoterBranch.PROMOTE_WAITER
    slot = fake2.items[(f"SLOT#{WINDOW_UNIT}", "SLOT")]
    promoted = slot["active_run_id"]["S"]
    record = release_store(fake2).read_run(promoted)
    assert record is not None and record.window == WINDOW
    fake2.items[(f"RUN#{promoted}", "NODE#write_orders")]["state"] = write_text("SUCCEEDED")
    _finish_post_grant(fake2, run2)
    assert release_run(context(fake2), promoted) is ReleaseOutcome.APPLIED
    assert head_item(fake2, WINDOW_UNIT)["head_epoch"] == {"N": str(head_epoch(R2))}  # type: ignore[index]
    assert _run_item(fake, run)["state"] == {"S": "FENCING"}


# --- The domain ordinal is never stranded by a holder that leaves OPEN ungranted ----------------

UNIT_Q = canonical_authority_digest(["fictional-window", "amc", "q"])


def _leave_open(fake: FakeDynamo, run: str, how: str) -> None:
    """Take the ordinal holder out of OPEN before its grant: cancel, pause or fence."""
    if how == "fence":
        _run_item(fake, run)["state"] = write_text(RunState.FENCING.value)
        _run_item(fake, run)["fence_reason"] = write_text("input_invalidated")
        return
    kind = RunCommandKind.CANCEL if how == "cancel" else RunCommandKind.PAUSE
    assert run_operator_command(release_store(fake), _command(kind, run, f"cmd-{how}")).committed


def _idle(fake: FakeDynamo) -> bool:
    order = release_store(fake).read_release_order(DOMAIN)
    return order.active_ordinal is None and order.active_run_id is None


@pytest.mark.parametrize("holder_pass_first", [True, False])
@pytest.mark.parametrize("how", ["cancel", "pause", "fence"])
def test_a_holder_that_leaves_open_before_its_grant_never_strands_the_domain(
    how: str, holder_pass_first: bool
) -> None:
    fake = FakeDynamo()
    p = seed_granting_run(fake, fingerprint="1" * 64, request_id="request-p").record.run_id
    q = seed_granting_run(
        fake, fingerprint="2" * 64, request_id="request-q", unit=UNIT_Q
    ).record.run_id
    _prepare(fake, p)
    _leave_open(fake, p, how)

    if holder_pass_first:
        # The holder's own next pass returns its ordinal.
        assert release_run(context(fake), p) is ReleaseOutcome.NOT_LIVE
        assert _idle(fake)
    # Otherwise the waiting run returns it for the holder (its trigger may be gone).
    assert release_run(context(fake), q) is ReleaseOutcome.APPLIED
    assert _idle(fake)
    assert keys(fake, "GRANT#") == [(f"GRANT#{release_store(fake).read_run(q).grant_id}", "GRANT")]  # type: ignore[union-attr]
    trigger = (due_shard(UNIT), f"RELEASE#{p}")
    # A terminal holder's trigger is retired; a paused holder keeps it so resume carries on.
    assert (trigger in fake.items) is (how == "pause")
    assert release_store(fake).read_block(DOMAIN) is None


def test_a_paused_holder_resumes_and_releases_after_the_run_that_took_its_turn() -> None:
    fake = FakeDynamo()
    p = seed_granting_run(
        fake, tables=("a",), fingerprint="1" * 64, request_id="request-p"
    ).record.run_id
    q = seed_granting_run(
        fake, tables=("a",), fingerprint="2" * 64, request_id="request-q", unit=UNIT_Q
    ).record.run_id
    _prepare(fake, p)
    _leave_open(fake, p, "pause")
    assert release_run(context(fake), p) is ReleaseOutcome.NOT_LIVE
    assert _idle(fake)

    assert release_run(context(fake), q) is ReleaseOutcome.APPLIED
    resumed = run_operator_command(
        release_store(fake), _command(RunCommandKind.RESUME, p, "cmd-resume")
    )
    assert resumed.committed
    assert release_shard(context(fake), due_shard(UNIT)).outcomes == {"applied": 1}

    # One serial order on the shared target: P's abandoned ordinal 1, then Q (2), then P (3).
    assert [sk for _, sk in keys(fake, "TARGET#fictional-db.a") if sk.startswith("RES")] == [
        "RESERVATION#0000000001",
        "RESERVATION#0000000002",
        "RESERVATION#0000000003",
    ]
    assert _tails(fake, "fictional-db.a") == [p, q, p]
    grant = release_store(fake).read_grant(release_store(fake).read_run(p).grant_id or "")  # type: ignore[union-attr]
    assert grant is not None and grant.ordinal == 3
    assert _idle(fake)


def test_a_holder_resumed_before_the_retire_commits_keeps_its_ordinal() -> None:
    fake = FakeDynamo()
    p = seed_granting_run(fake, fingerprint="1" * 64, request_id="request-p").record.run_id
    q = seed_granting_run(
        fake, fingerprint="2" * 64, request_id="request-q", unit=UNIT_Q
    ).record.run_id
    _prepare(fake, p)
    _leave_open(fake, p, "pause")
    resume = _hook(
        run_operator_command,
        release_store(fake),
        _command(RunCommandKind.RESUME, p, "cmd-resume"),
    )
    fake.before_transaction = resume  # the resume commits between Q's read and its retire

    assert release_run(context(fake), q) is ReleaseOutcome.WAITING_FOR_ORDER

    order = release_store(fake).read_release_order(DOMAIN)
    assert order.active_run_id == p
    assert _run_item(fake, p)["state"] == {"S": "OPEN"}
    fake.before_transaction = None
    assert release_run(context(fake), p) is ReleaseOutcome.APPLIED
    assert release_run(context(fake), q) is ReleaseOutcome.APPLIED


# --- S6 §2.8 (AGG3-1): the replay-safe contribution ------------------------------------------


def _bake_contribution(
    grant: str, root: str, unavailable: tuple[tuple[str, str], ...]
) -> CohortContribution:
    return CohortContribution(
        grant,
        CohortMember.BAKE,
        CohortOutcome.UNAVAILABLE,
        "r" * 64,
        NOW,
        root,
        unavailable,
    )


def test_agg3_1_a_replay_with_a_differently_computed_list_settles_from_the_stored_one() -> None:
    fake = FakeDynamo()
    seeded = seed_granting_run(fake)
    run = seeded.record.run_id
    release_run(context(fake), run)
    store = release_store(fake)
    record = store.read_run(run)
    assert record is not None and record.grant_id is not None
    stored = _bake_contribution(
        record.grant_id, record.root_flow_id, (("campaigns#7d", "bake_budget_exhausted"),)
    )
    assert contribute(store, stored, record).replayed is False
    later = _bake_contribution(
        record.grant_id, record.root_flow_id, (("campaigns#30d", "post_grant_invariant"),)
    )

    replay = contribute(store, later, record)

    assert (replay.replayed, replay.contribution) == (True, stored)
    assert settlement_for(replay.contribution) is NodeState.FAILED
    assert store.read_contributions(record.grant_id) == (stored,)
    # Control: a direct second record with another outcome is still refused.
    with pytest.raises(GraphStoreUnavailableError, match="two different terminals"):
        store.record_contribution(later)


# --- GRAPH bake V3 protocol (plan 4.1; DB21, DB22, A1.5, F8, F12) -----------------------------

from domain.operations.execution_graph_release import (  # noqa: E402
    BAKE_REVISION_CURRENT,
    BakeEffectKind,
    BakeRoster,
    BakeRosterRow,
    BakeTaskSource,
    DurableBakeEffectInvalidError,
    DurableBakeEffectV3,
    DurableBakeSourceManifest,
    DurableBakeSourceManifestInvalidError,
    PinRefusal,
    PrecomputeInputV1,
    bake_v3_digest,
    final_transaction_member_count,
    join_bake_effects,
    written_pointer_effects,
)

_D = "sha256:" + "a" * 64
_E = "sha256:" + "b" * 64


def _v3_effect(  # noqa: PLR0913 -- one effect and its three optional axes
    kind: BakeEffectKind,
    *,
    page: str = "campaigns",
    variant: str = "7d",
    overrides: dict[str, object] | None = None,
) -> DurableBakeEffectV3:
    defaults: dict[str, object] = {
        BakeEffectKind.WRITTEN: {
            "reason_code": None,
            "artifact_revision": "c" * 64,
            "output_manifest_digest": _D,
            "pointer_candidate_digest": _E,
        },
        BakeEffectKind.UNCHANGED: {
            "reason_code": BAKE_REVISION_CURRENT,
            "artifact_revision": "c" * 64,
            "output_manifest_digest": None,
            "pointer_candidate_digest": None,
        },
        BakeEffectKind.UNAVAILABLE: {
            "reason_code": f"{page.replace('-', '_')}_materialization_unavailable",
            "artifact_revision": None,
            "output_manifest_digest": None,
            "pointer_candidate_digest": None,
        },
    }[kind]  # type: ignore[assignment]
    fields = {
        "task_id": f"task-{page}-{variant}",
        "run_id": "run-fictional",
        "grant_id": "grant-fictional",
        "head_version": 3,
        "page": page,
        "variant": variant,
        "effect_kind": kind,
        "source_manifest_digest": _D,
        **defaults,  # type: ignore[dict-item]
        **(overrides or {}),
    }
    return DurableBakeEffectV3(**fields)  # type: ignore[arg-type]


@pytest.mark.parametrize("kind", list(BakeEffectKind))
def test_v3_each_closed_effect_kind_constructs_and_round_trips_its_record(
    kind: BakeEffectKind,
) -> None:
    effect = _v3_effect(kind)
    assert DurableBakeEffectV3.from_record(effect.to_record()) == effect
    assert effect.effect_digest.startswith("sha256:")


@pytest.mark.parametrize(
    ("kind", "overrides"),
    [
        (BakeEffectKind.WRITTEN, {"output_manifest_digest": None}),
        (BakeEffectKind.WRITTEN, {"reason_code": "revision_current"}),
        (BakeEffectKind.WRITTEN, {"pointer_candidate_digest": "not-a-digest"}),
        (BakeEffectKind.UNCHANGED, {"reason_code": None}),
        (BakeEffectKind.UNCHANGED, {"output_manifest_digest": _D}),
        (BakeEffectKind.UNCHANGED, {"artifact_revision": None}),
        (BakeEffectKind.UNAVAILABLE, {"source_manifest_digest": None}),
        (BakeEffectKind.UNAVAILABLE, {"artifact_revision": "c" * 64}),
        (BakeEffectKind.UNAVAILABLE, {"reason_code": "page_baker_unavailable"}),
        (BakeEffectKind.UNAVAILABLE, {"reason_code": "retail_health_materialization_unavailable"}),
        (BakeEffectKind.UNAVAILABLE, {"reason_code": "campaign_materialization_unavailable"}),
    ],
)
def test_v3_every_forbidden_field_presence_or_reason_is_refused_before_any_use(
    kind: BakeEffectKind, overrides: dict[str, object]
) -> None:
    """Paired with the construct test above: one field away from a valid effect is refused.

    The last case is the old literal `campaign_materialization_unavailable` (F12 control).
    """
    with pytest.raises(DurableBakeEffectInvalidError) as refused:
        _v3_effect(kind, overrides=overrides)
    assert refused.value.signal == "durable_bake_effect_invalid"


def test_v3_an_unknown_or_missing_record_field_is_refused() -> None:
    record = _v3_effect(BakeEffectKind.WRITTEN).to_record()
    with pytest.raises(DurableBakeEffectInvalidError):
        DurableBakeEffectV3.from_record({**record, "extra": 1})
    del record["page"]
    with pytest.raises(DurableBakeEffectInvalidError):
        DurableBakeEffectV3.from_record(record)


def _roster(count: int) -> BakeRoster:
    return BakeRoster(
        tuple(
            BakeRosterRow("campaigns", f"{index + 1}d", 240, 16.6, 123, 26)
            for index in range(count)
        )
    )


def _effects(roster: BakeRoster, *, kinds: tuple[BakeEffectKind, ...] | None = None):
    chosen = kinds or tuple(BakeEffectKind.WRITTEN for _ in roster.rows)
    return tuple(
        _v3_effect(kind, page=row.page, variant=row.variant)
        for row, kind in zip(roster.rows, chosen, strict=True)
    )


@pytest.mark.parametrize("count", [3, 9])
def test_f8_the_join_and_member_count_are_n_generic(count: int) -> None:
    """A1.5/F8: N = 3 and N = 9 join in roster order with 12 + 4N members (A3, no GENERATION#)."""
    roster = _roster(count)
    effects = _effects(roster)

    joined = join_bake_effects(roster, reversed(effects))

    assert tuple(effect.key for effect in joined) == roster.keys
    assert final_transaction_member_count(joined) == 10 + 4 * count


def test_f8_control_a_nine_row_roster_with_seven_effects_is_refused() -> None:
    roster = _roster(9)
    with pytest.raises(DurableBakeEffectInvalidError, match="durable bake effect"):
        join_bake_effects(roster, _effects(roster)[:7])
    duplicated = (*_effects(roster)[:8], _effects(roster)[0])
    with pytest.raises(DurableBakeEffectInvalidError):
        join_bake_effects(roster, duplicated)


def test_v3_mixed_effects_join_and_only_written_contribute_pointer_members() -> None:
    roster = _roster(3)
    kinds = (BakeEffectKind.UNAVAILABLE, BakeEffectKind.WRITTEN, BakeEffectKind.UNCHANGED)
    joined = join_bake_effects(roster, _effects(roster, kinds=kinds))

    assert [effect.key for effect in written_pointer_effects(joined)] == ["campaigns#2d"]
    assert final_transaction_member_count(joined) == 10 + 3 + 3
    projected = PrecomputeInputV1.from_effects(joined)
    assert [entry.effect_kind for entry in projected.entries] == list(kinds)
    assert projected.digest != PrecomputeInputV1.from_effects(joined[::-1]).digest


def test_the_contract_roster_is_the_20_row_d2_roster_and_at_most_the_bound() -> None:
    roster = BakeRoster.from_contract()
    assert len(roster.rows) == 20
    assert roster.pages == (
        "campaigns",
        "retail-health",
        "scorecard",
        "new-customers",
        "reach",
        "retention",
        "audiences",
    )
    assert all(row.certified_plan_count >= 1 for row in roster.rows)
    with pytest.raises(ValueError, match="max_tasks"):
        _roster(22)
    with pytest.raises(ValueError, match="repeats"):
        BakeRoster((roster.rows[0], roster.rows[0]))


def test_db21_the_manifest_pins_n_counts_and_refuses_another_roster() -> None:
    roster = _roster(3)
    tasks = tuple(
        BakeTaskSource(row.page, row.variant, bake_v3_digest("reads", row.key), ("f" * 64,), 1)
        for row in roster.rows
    )
    manifest = DurableBakeSourceManifest("run-fictional", "grant-fictional", roster.digest, tasks)

    assert manifest.aligned_to(roster).plan_counts == (1, 1, 1)
    assert (
        manifest.digest
        != DurableBakeSourceManifest(
            "run-fictional", "grant-fictional", roster.digest, tasks[::-1]
        ).digest
    )
    with pytest.raises(DurableBakeSourceManifestInvalidError):
        manifest.aligned_to(_roster(4))
    with pytest.raises(DurableBakeSourceManifestInvalidError):
        BakeTaskSource("campaigns", "1d", _D, ("f" * 64,), 2)


_D2_VARIANTS = (
    "7d",
    "30d",
    "last_complete_week",
    "last_complete_month",
    "last_complete_quarter",
    "latest_with_current_publication",
)


@pytest.mark.parametrize("variant", _D2_VARIANTS)
def test_o1_the_roster_variant_grammar_accepts_every_d2_variant(variant: str) -> None:
    """A5.2 (O1): only the variant grammar widens; the underscore calendar ids are verbatim."""
    row = BakeRosterRow("retention", variant, 90, 16.6, 832, 26)
    assert row.key == f"retention#{variant}"


@pytest.mark.parametrize(
    ("page", "variant"),
    [
        ("retail_health", "7d"),  # pages stay hyphen-only (reason normalization)
        ("campaigns", "Last-month"),  # uppercase is refused
        ("campaigns", "_7d"),  # a leading underscore is refused
        ("campaigns", "a" * 81),  # the length cap still applies to variants
    ],
)
def test_o1_controls_a_page_underscore_uppercase_and_an_81_character_variant_are_refused(
    page: str, variant: str
) -> None:
    with pytest.raises(ValueError, match="lowercase identifier"):
        BakeRosterRow(page, variant, 90, 16.6, 123, 26)
    assert BakeRosterRow("a" * 80, "b_" * 40, 90, 16.6, 123, 26).variant == "b_" * 40


# --- ADR-140: a bake that skips the finalizer still settles its cohort -----------------------


def _grant_of(fake: FakeDynamo, run: str) -> str:
    record = release_store(fake).read_run(run)
    assert record is not None and record.grant_id is not None
    return record.grant_id


@dataclass
class _Released:
    """A release context that records its withheld signals and its reported failures."""

    fake: FakeDynamo
    signals: list[int] = field(default_factory=list[int])
    failures: list[BaseException] = field(default_factory=list[BaseException])
    lines: list[Any] = field(default_factory=list[Any])
    logged: list[tuple[str, dict[str, object]]] = field(
        default_factory=list[tuple[str, dict[str, object]]]
    )

    def context(self) -> ReleaseContext:
        return replace(
            context(self.fake),
            serving_signal=self.signals.append,
            report=self.lines.append,
            on_failure=self._report,
        )

    def _report(self, error: BaseException) -> None:
        self.failures.append(error)
        report_error(
            error,
            Report(
                operation="execution_graph.release",
                correlation_id="execution_graph.release",
                logger=cast("Any", self),
            ),
        )

    def error(self, message: str, *, extra: dict[str, object]) -> None:
        self.logged.append((message, extra))

    def sweep(self) -> ReleaseCounts:
        return release_shard(self.context(), due_shard(UNIT))


# D1: the pure rule.


def _view(**changes: Any) -> UnarmedPrecomputeView:
    base: dict[str, Any] = {
        "run_id": "run-a",
        "grant_id": "grant-a",
        "root_flow_id": "root-a",
        "nodes": (
            ("grant_apply", NodeState.SUCCEEDED),
            ("publish", NodeState.SUCCEEDED),
            ("precompute", NodeState.WAITING_INPUT),
            ("bake", NodeState.SUCCEEDED),
        ),
        "bake": bake_member("grant-a", "root-a", "u1"),
        "precompute": None,
        "at": NOW,
    }
    return UnarmedPrecomputeView(**{**base, **changes})


def _with(view: UnarmedPrecomputeView, **states: NodeState) -> tuple[tuple[str, NodeState], ...]:
    return tuple((name, states.get(name, state)) for name, state in view.nodes)


@pytest.mark.covers("ADR-140")
@pytest.mark.parametrize(("shape", "bake_state"), [("u1", "SUCCEEDED"), ("u2", "FAILED")])
def test_adr140_d1_u1_and_u2_settle_precompute_no_eligible_exactly(
    shape: str, bake_state: str
) -> None:
    view = _view(bake=bake_member("grant-a", "root-a", shape))
    view = replace(view, nodes=_with(view, bake=NodeState(bake_state)))

    assert unarmed_precompute_contribution(view) == CohortContribution(
        "grant-a", CohortMember.PRECOMPUTE, CohortOutcome.NO_ELIGIBLE, None, NOW, "root-a"
    )


@pytest.mark.covers("ADR-140")
@pytest.mark.parametrize(
    "change",
    [
        "armed",
        "terminal_precompute",
        "pending_bake",
        "claimed_bake",
        "no_bake_member",
        "pending_finalizer",
        "claimed_finalizer",
    ],
)
def test_adr140_d1_the_rule_waits_on_every_unready_shape(change: str) -> None:
    view = _view()
    fanned_out = (*view.nodes, ("bake_task:campaigns#7d", NodeState.FAILED))
    waiting = {
        "armed": _with(view, precompute=NodeState.READY),
        "terminal_precompute": _with(view, precompute=NodeState.SUCCEEDED),
        "pending_bake": _with(view, bake=NodeState.PENDING),
        "claimed_bake": _with(view, bake=NodeState.CLAIMED),
        "no_bake_member": view.nodes,
        # ADR-140 (B4): the finalizer arms once every task is terminal, then abandons.
        "pending_finalizer": (*fanned_out, ("bake_finalize", NodeState.PENDING)),
        "claimed_finalizer": (*fanned_out, ("bake_finalize", NodeState.CLAIMED)),
    }[change]
    bake = None if change == "no_bake_member" else view.bake
    assert unarmed_precompute_contribution(replace(view, nodes=waiting, bake=bake)) is None


@pytest.mark.covers("ADR-140")
@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("revision", "bake_shape:succeeded"),
        ("designed", "bake_shape:unavailable"),
        ("mixed", "bake_shape:unavailable"),
        ("fan_out", "fan_out_present"),
        ("finalize_node", "fan_out_present"),
        ("u1_failed_node", "bake_node:FAILED"),
        ("u2_succeeded_node", "bake_node:SUCCEEDED"),
        ("precompute_stored", "precompute_contributed"),
        ("no_precompute_node", "node_absent:precompute"),
        ("no_bake_node", "node_absent:bake"),
    ],
)
def test_adr140_d1_every_other_shape_is_refused_with_its_reason(change: str, reason: str) -> None:
    view = _view()
    precompute = CohortContribution(
        "grant-a", CohortMember.PRECOMPUTE, CohortOutcome.NO_ELIGIBLE, None, NOW, "root-a"
    )
    changed = {
        "revision": replace(view, bake=bake_member("grant-a", "root-a", "revision")),
        "designed": replace(
            view,
            bake=bake_member("grant-a", "root-a", "designed"),
            nodes=_with(view, bake=NodeState.FAILED),
        ),
        "mixed": replace(
            view,
            bake=bake_member("grant-a", "root-a", "mixed"),
            nodes=_with(view, bake=NodeState.FAILED),
        ),
        "fan_out": replace(view, nodes=(*view.nodes, ("bake_task:campaigns#7d", NodeState.FAILED))),
        "finalize_node": replace(view, nodes=(*view.nodes, ("bake_finalize", NodeState.SUCCEEDED))),
        "u1_failed_node": replace(view, nodes=_with(view, bake=NodeState.FAILED)),
        "u2_succeeded_node": replace(view, bake=bake_member("grant-a", "root-a", "u2")),
        "precompute_stored": replace(view, precompute=precompute),
        "no_precompute_node": replace(
            view, nodes=tuple(item for item in view.nodes if item[0] != "precompute")
        ),
        "no_bake_node": replace(
            view, nodes=tuple(item for item in view.nodes if item[0] != "bake")
        ),
    }[change]

    with pytest.raises(UnarmedPrecomputeRefusedError) as refused:
        unarmed_precompute_contribution(changed)

    assert str(refused.value.code) == "ADLY-GRAPH_PRECOMPUTE-0007"
    assert refused.value.context == {"run_id": "run-a", "grant_id": "grant-a", "reason": reason}


@pytest.mark.covers("ADR-140")
def test_adr140_d1_the_view_refuses_another_members_contribution_or_a_naive_clock() -> None:
    with pytest.raises(ValueError, match="another member"):
        _view(bake=bake_member("grant-b", "root-a", "u1"))
    with pytest.raises(ValueError, match="UTC"):
        _view(at=NOW.replace(tzinfo=None))


# R1-R3: the Release's COMPLETE# loop.


@pytest.mark.covers("ADR-140")
def test_adr140_r1_one_pass_settles_binds_and_completes_the_u1_winner() -> None:
    fake = FakeDynamo()
    run = unarmed_winner(fake, "u1")
    grant = _grant_of(fake, run)
    released = _Released(fake)

    counts = released.sweep()

    assert (counts.unarmed_settled, counts.completed, counts.failed) == (1, 1, 0)
    store = release_store(fake)
    precompute = next(
        item for item in store.read_contributions(grant) if item.member is CohortMember.PRECOMPUTE
    )
    assert (precompute.outcome, precompute.artifact_revision) == (CohortOutcome.NO_ELIGIBLE, None)
    node = store.read_node(run, "precompute")
    assert node is not None and node.state is NodeState.SUCCEEDED
    record, digest = consumer_effect_record(
        CohortMember.PRECOMPUTE, precompute, {"observed_union_count": 0}
    )
    assert (node.effect_record, node.effect_digest) == (record, digest)
    binding = store.read_serving_binding(grant)
    assert binding is not None and binding.artifact_revision is None
    assert not binding_authorizes_serving(binding) and not serving_withheld(binding)
    item = _run_item(fake, run)
    assert (item["state"]["S"], item["serving_outcome"]["S"]) == ("SUCCEEDED", "no_eligible")
    assert keys(fake, "COMPLETE#") == []
    head = fake.items[(f"SERVING_DOMAIN#{DOMAIN}", "HEAD")]
    assert head["state"]["S"] == "IDLE"  # the sole-member cohort drained
    assert released.signals == [] and released.failures == []
    # A second pass is a no-op: precompute is terminal, COMPLETE# is gone.
    before = copy.deepcopy(fake.items)
    assert released.sweep().unarmed_settled == 0
    assert fake.items == before


@pytest.mark.covers("ADR-140")
@pytest.mark.parametrize("control", ["fanned_out", "armed", "unelected"])
def test_adr140_r1_controls_settle_nothing_with_zero_writes(control: str) -> None:
    fake = FakeDynamo()
    if control == "unelected":
        run = granted_run(fake)  # bake PENDING, precompute WAITING_INPUT: a candidate or loser
    else:
        run = unarmed_winner(fake, "u1")
        grant = _grant_of(fake, run)
        if control == "fanned_out":  # the fan-out settled without a contribution (N3)
            fake.items.pop((f"COHORT#{grant}", "CONTRIBUTION#bake"))
        else:  # the finalizer armed precompute
            fake.items[(f"RUN#{run}", "NODE#precompute")]["state"] = write_text("READY")
    before = copy.deepcopy(fake.items)
    released = _Released(fake)

    counts = released.sweep()

    assert (counts.unarmed_settled, counts.completed, counts.failed) == (0, 0, 0)
    assert settle_unarmed_cohort(released.context(), run) is None
    assert fake.items == before


@pytest.mark.covers("ADR-140")
def test_adr140_r2_a_u2_winner_signals_withheld_once_per_attempt_and_u1_never() -> None:
    fake = FakeDynamo()
    run = unarmed_winner(fake, "u2")
    released = _Released(fake)
    calls = {"count": 0}

    def crash_completion(table: FakeDynamo) -> None:
        del table
        raise RuntimeError("fictional crash after the withheld emission")

    original = fake.transact_write_items

    def second_transaction_crashes(**request: Any) -> dict[str, Any]:
        calls["count"] += 1
        if calls["count"] == 2:
            fake.before_transaction = crash_completion
        return original(**request)

    fake.transact_write_items = second_transaction_crashes  # type: ignore[method-assign]

    first = released.sweep()

    assert (first.unarmed_settled, first.completed, first.failed) == (1, 0, 1)
    assert released.signals == [1]
    assert _run_item(fake, run)["state"]["S"] == "GRANTED"
    second = released.sweep()
    assert (second.unarmed_settled, second.completed) == (0, 1)
    assert released.signals == [1, 1]  # at least once: re-emitted until the completion commits
    (line, _again) = released.lines
    assert (line.phase, line.outcome, line.severity) == (
        "complete",
        "serving_withheld",
        LogSeverity.WARNING,
    )
    assert line.details["failure_reasons"] == [POST_GRANT_INVARIANT]
    binding = release_store(fake).read_serving_binding(_grant_of(fake, run))
    assert binding is not None and serving_withheld(binding)
    # Control: the U1 winner completes with no withheld signal.
    control = FakeDynamo()
    unarmed_winner(control, "u1")
    quiet = _Released(control)
    assert quiet.sweep().completed == 1
    assert quiet.signals == []


@pytest.mark.covers("ADR-140")
@pytest.mark.parametrize(
    ("image", "reason"),
    [
        ("revision", "bake_shape:succeeded"),
        ("mixed", "bake_shape:unavailable"),
        ("fan_out", "fan_out_present"),
        ("u1_failed_node", "bake_node:FAILED"),
        ("precompute_stored", "precompute_contributed"),
        ("no_head", "authority_absent:active_head"),
        ("no_publish_digest", "authority_absent:publish_effect"),
    ],
)
def test_adr140_r3_a_b8_image_fails_loud_every_pass_with_zero_writes(
    image: str, reason: str
) -> None:
    fake = FakeDynamo()
    shape = image if image in {"revision", "mixed"} else "u1"
    run = unarmed_winner(fake, shape, "FAILED" if image in {"mixed", "u1_failed_node"} else None)
    grant = _grant_of(fake, run)
    if image == "fan_out":
        fake.items[(f"RUN#{run}", "NODE#bake_finalize")] = {
            **copy.deepcopy(fake.items[(f"RUN#{run}", "NODE#precompute")]),
            "sk": write_text("NODE#bake_finalize"),
            "node": write_text("bake_finalize"),
            "state": write_text("SUCCEEDED"),
        }
    elif image == "precompute_stored":
        release_store(fake).record_contribution(
            CohortContribution(
                grant, CohortMember.PRECOMPUTE, CohortOutcome.NO_ELIGIBLE, None, NOW, "root"
            )
        )
    elif image == "no_head":
        fake.items[(f"SERVING_DOMAIN#{DOMAIN}", "HEAD")]["state"] = write_text("IDLE")
    elif image == "no_publish_digest":
        fake.items[(f"RUN#{run}", "NODE#publish")].pop("effect_digest")
    before = copy.deepcopy(fake.items)
    released = _Released(fake)

    for _ in range(2):
        counts = released.sweep()
        assert (counts.failed, counts.unarmed_settled, counts.completed) == (1, 0, 0)
    assert fake.items == before
    assert len(released.failures) == 2
    message, extra = released.logged[0]
    assert message == "operation failed"
    assert extra["error_code"] == "ADLY-GRAPH_PRECOMPUTE-0007"
    assert cast("dict[str, object]", extra["error_context"])["run_id"] == run
    assert cast("dict[str, object]", extra["error_context"])["reason"] == reason


# H6 (ADR-140, B4): the abandoned fan-out's U3 shape and the one binder per grant.


def _abandoned(**states: NodeState) -> UnarmedPrecomputeView:
    """Return a fanned-out winner whose finalizer abandoned before precompute armed (U3)."""
    view = _view(bake=bake_member("grant-a", "root-a", "u2"))
    nodes = (
        *view.nodes,
        ("bake_task:campaigns#7d", NodeState.FAILED),
        ("bake_task:campaigns#30d", NodeState.SUCCEEDED),
        ("bake_finalize", NodeState.FAILED),
    )
    return replace(view, nodes=tuple((name, states.get(name, state)) for name, state in nodes))


@pytest.mark.covers("ADR-140")
def test_h6_u3_an_abandoned_fan_out_settles_precompute_no_eligible_exactly() -> None:
    assert unarmed_precompute_contribution(_abandoned()) == CohortContribution(
        "grant-a", CohortMember.PRECOMPUTE, CohortOutcome.NO_ELIGIBLE, None, NOW, "root-a"
    )


@pytest.mark.covers("ADR-140")
@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("finalizer_succeeded", "fan_out_present"),
        ("bake_failed", "bake_node:FAILED"),
        ("revision", "bake_shape:succeeded"),
        ("designed", "bake_shape:unavailable"),
        ("precompute_stored", "precompute_contributed"),
    ],
)
def test_h6_u3_controls_every_other_abandoned_shape_is_refused(change: str, reason: str) -> None:
    precompute = CohortContribution(
        "grant-a", CohortMember.PRECOMPUTE, CohortOutcome.NO_ELIGIBLE, None, NOW, "root-a"
    )
    changed = {
        "finalizer_succeeded": _abandoned(bake_finalize=NodeState.SUCCEEDED),
        "bake_failed": _abandoned(bake=NodeState.FAILED),
        "revision": replace(_abandoned(), bake=bake_member("grant-a", "root-a", "revision")),
        "designed": replace(_abandoned(), bake=bake_member("grant-a", "root-a", "designed")),
        "precompute_stored": replace(_abandoned(), precompute=precompute),
    }[change]

    with pytest.raises(UnarmedPrecomputeRefusedError) as refused:
        unarmed_precompute_contribution(changed)

    assert refused.value.context["reason"] == reason


@pytest.mark.covers("ADR-140")
def test_h6_u3_a_non_terminal_finalizer_waits() -> None:
    for state in (NodeState.PENDING, NodeState.READY, NodeState.CLAIMED):
        assert unarmed_precompute_contribution(_abandoned(bake_finalize=state)) is None


@dataclass
class _Binder:
    """A store whose bind loses to a binding a sibling already wrote (or to nothing)."""

    stored: Any = None
    binds: list[Any] = field(default_factory=list[Any])

    def bind_serving(self, binding: Any) -> None:
        self.binds.append(binding)
        raise GraphGuardError("fictional_binding_exists")

    def read_serving_binding(self, grant_id: str) -> Any:
        del grant_id
        return self.stored


@pytest.mark.covers("ADR-140")
def test_h6_bind_once_adopts_the_stored_binding_after_a_lost_bind_and_re_raises_otherwise() -> None:
    from adapters.jobs.execution_graph.release import bind_once  # noqa: PLC0415

    mine = SimpleNamespace(grant_id="grant-a", stale=True)
    theirs = SimpleNamespace(grant_id="grant-a", stale=False)

    assert bind_once(cast("Any", _Binder(stored=theirs)), cast("Any", mine)) is theirs
    with pytest.raises(GraphGuardError):
        bind_once(cast("Any", _Binder()), cast("Any", mine))


@pytest.mark.covers("ADR-140")
def test_h6_contribute_after_a_lost_bind_returns_the_stored_binding() -> None:
    fake = FakeDynamo()
    run_id = unarmed_winner(fake, "u2")
    store = release_store(fake)
    run = store.read_run(run_id)
    assert run is not None and run.grant_id is not None
    real = store.bind_serving

    def racing(binding: Any) -> None:
        real(replace(binding, stale=True))  # the abandoning finalizer binds first
        real(binding)

    object.__setattr__(store, "bind_serving", racing)
    precompute = CohortContribution(
        run.grant_id,
        CohortMember.PRECOMPUTE,
        CohortOutcome.NO_ELIGIBLE,
        None,
        NOW,
        run.root_flow_id,
    )

    contributed = contribute(store, precompute, run)

    stored = store.read_serving_binding(run.grant_id)
    assert stored is not None and stored.stale
    assert contributed.binding == stored


def test_the_roster_contains_exactly_its_keys_and_row_keeps_its_invariant() -> None:
    """ADR-144: the legacy entry asks `contains` first, so `row` never sees a non-roster key."""
    roster = BakeRoster.from_contract()
    assert all(roster.contains(key) for key in roster.keys)
    assert roster.contains("workspace#7d") is False
    # Paired: `row` still refuses the same key, so only the branch keeps it acknowledged.
    with pytest.raises(DurableBakeEffectInvalidError):
        roster.row("workspace#7d")


# --- the pin's refusal (ADR-142, design 02 §3.3, round-2 N5) --------------------------------


def test_a_slice_without_a_pin_refusal_hashes_exactly_as_before_and_a_refused_one_differs() -> None:
    plain = BakeTaskSource("campaigns", "1d", _D, (), 0)
    # The pre-change digest: the five terms only, so a cohort pinned before the deploy re-hashes.
    assert plain.digest == bake_v3_digest(
        "graph-durable-bake-task-source/v1", ["campaigns", "1d", _D, list[str](), 0]
    )
    refused = BakeTaskSource("campaigns", "1d", _D, (), 0, PinRefusal(3))
    assert refused.digest != plain.digest
    assert refused.digest != BakeTaskSource("campaigns", "1d", _D, (), 0, PinRefusal(2)).digest


def test_a_pin_refusal_carries_no_requests_and_its_reason_and_attempts_are_closed() -> None:
    cases = (
        (
            lambda: BakeTaskSource("campaigns", "1d", _D, ("f" * 64,), 1, PinRefusal(3)),
            "no requests",
        ),
        (lambda: PinRefusal(0), "at least one attempt"),
        (lambda: PinRefusal(3, "throttled"), "closed"),
    )
    for build, signal in cases:
        with pytest.raises(DurableBakeSourceManifestInvalidError) as raised:
            build()
        assert signal in str(raised.value.context["detail"])
    # Paired: a refusal with zero requests constructs.
    assert BakeTaskSource("campaigns", "1d", _D, (), 0, PinRefusal(1)).pin_refusal == PinRefusal(1)
