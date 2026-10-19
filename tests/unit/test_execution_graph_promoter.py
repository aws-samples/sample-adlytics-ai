# SPDX-License-Identifier: MIT-0
"""S3 promoter: one promotion path, conditioned deletes and the lost-wakeup backstop (V-20, V-25).

The interleaving tests commit a competing transaction between the promoter's read and its
delete with the fake's `before_transaction` hook, so the promoter's own conditions are what
refuse it (`PROMOTE_WAKE_CHANGED`). Fictional data only.
"""

from __future__ import annotations

import contextlib
import copy
from collections.abc import Generator, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from execution_graph_dispatch_support import (
    NOW,
    ROOT_CLOCK,
    SHARD,
    UNIT,
    kinds,
    materialized_run,
    now_text,
    planner,
    seed_run,
    set_mode,
    store,
    write_bool,
    write_int,
    write_text,
)
from execution_graph_release_support import granted_run, release_store, settle_publish
from execution_graph_support import FakeDynamo
from fixed_member_byte_fixture_support import (
    DrainActivationWorld,
    FinalTransactionCapture,
    build_world,
    capture_final_transaction,
    complete_direct_winner,
    drain_nonfinal_aliases,
    grant_and_publish,
    promoter_pass,
    publish_rolling,
)

from adapters.jobs.execution_graph import promoter as promoter_module
from adapters.jobs.execution_graph.fence_settler import due_shards, settle_shard
from adapters.jobs.execution_graph.instantiate import ingestion_node_plan
from adapters.jobs.execution_graph.promoter import (
    ArmCounts,
    PromoterContext,
    PromotionCounts,
    arm_pass,
    arm_run,
    promote_shard,
    promote_unit,
    read_arms,
    walk_complete,
)
from adapters.jobs.trace_contract.contract import load_trace_contract
from adapters.outbound.dynamodb.execution_graph import (
    DynamoExecutionGraphStore,
    due_shard,
    receipt_item,
)
from domain.operations.execution_graph import AuthorityMode
from domain.operations.execution_graph_dispatch import (
    RUN_ARMING_OPEN,
    RUN_STATE_CHANGED,
    ArmScope,
    FenceReason,
    GraphGuardError,
    PromoterBranch,
)
from domain.operations.execution_graph_release import INGESTION_RELEASE_DOMAIN
from domain.operations.run_request import SLOT_REVISION_CHANGED, RunState, Waiter
from domain.shared_kernel.errors import InvariantError
from ports.outbound.execution_graph import (
    AdmissionCommit,
    AdmissionConflictError,
    EffectKind,
    ReceiptRecord,
)
from ports.outbound.execution_graph_dispatch import DueItem, FenceRunRequest
from ports.outbound.execution_graph_release import (
    ServingDomainElectionResult,
    ServingElectionOutcome,
)

pytestmark = pytest.mark.unit


def _context(fake: FakeDynamo) -> PromoterContext:
    return PromoterContext(release_store(fake), planner("write_a"), lambda: NOW)


def _receipt(
    request_id: str, retrieved: datetime, decision: str = "QUEUED_LATEST"
) -> ReceiptRecord:
    return ReceiptRecord(
        request_id=request_id,
        request_digest="d" * 64,
        decision=decision,
        reason=None,
        row=19,
        authority_epoch=1,
        mode=AuthorityMode.GRAPH,
        conflict_unit=UNIT,
        input_revision=retrieved.isoformat(),
        fingerprint="8" * 64,
        recorded_at=now_text(),
        protocol_version="v1",
        contract_revision=1,
        root_flow_id="root-waiter",
    )


def _seed_waiter(  # noqa: PLR0913 -- one fixture builder
    fake: FakeDynamo,
    *,
    held: bool = False,
    retrieved: datetime = NOW,
    request_id: str = "wait-0001",
) -> Waiter:
    fake.items[(f"RECEIPT#{request_id}", "RECEIPT")] = cast(
        "Any", receipt_item(_receipt(request_id, retrieved))
    )
    fake.items[(SHARD, f"WAITER#{UNIT}")] = {
        "pk": write_text(SHARD),
        "sk": write_text(f"WAITER#{UNIT}"),
        "item_kind": write_text("waiter"),
        "authority_epoch": write_int(1),
        "request_id": write_text(request_id),
        "fingerprint": write_text("8" * 64),
        "retrieved_at": write_text(retrieved.isoformat()),
        "held": write_bool(held),
        "revision": write_int(1),
        "due_at": write_text(now_text()),
    }
    return Waiter(request_id, "8" * 64, retrieved, held, 1)


def _active(fake: FakeDynamo) -> str:
    set_mode(fake, AuthorityMode.GRAPH)
    return seed_run(fake, instantiate=False).record.run_id


def _release(fake: FakeDynamo, run: str) -> None:
    current = store(fake).read_run(run)
    assert current is not None
    store(fake).fence_run(FenceRunRequest(current, FenceReason.CANCELLED, now_text()))


def _slot_run(fake: FakeDynamo) -> str | None:
    slot = fake.items.get((f"SLOT#{UNIT}", "SLOT"), {})
    value = slot.get("active_run_id")
    return None if value is None else str(value["S"])


def test_v20_a_crash_after_the_release_leaves_promote_and_the_next_pass_promotes_once() -> None:
    fake = FakeDynamo()
    run = _active(fake)
    _seed_waiter(fake)
    _release(fake, run)  # the "crash": no promoter ran after the release
    assert kinds(fake, "PROMOTE#") == [(SHARD, f"PROMOTE#{UNIT}")]
    counts = promote_shard(_context(fake), SHARD)
    assert counts.branches == {"promote_waiter": 1}
    promoted = _slot_run(fake)
    assert promoted is not None and promoted != run
    promoted_run = store(fake).read_run(promoted)
    assert promoted_run is not None
    assert (promoted_run.state, promoted_run.promoted) == (RunState.OPEN, True)
    assert kinds(fake, "WAITER#") == kinds(fake, "PROMOTE#") == []
    assert counts.instantiated == 1
    again = promote_shard(_context(fake), SHARD)
    assert again.branches == {}
    assert _slot_run(fake) == promoted


def test_retired_publish_wait_rows_are_not_read_by_the_final_promoter() -> None:
    fake = FakeDynamo()
    run = _active(fake)
    _seed_waiter(fake)
    _release(fake, run)
    failures: list[BaseException] = []
    fake.fail_query = "PUBLISH_WAIT#"
    context = PromoterContext(
        release_store(fake), planner("write_a"), lambda: NOW, on_failure=failures.append
    )

    counts = promote_shard(context, SHARD)

    assert counts.failed == 0
    assert counts.branches == {"promote_waiter": 1}
    assert failures == []
    assert _slot_run(fake) not in {None, run}


def test_v20_a_waiter_behind_a_fencing_run_is_promoted_before_that_runs_terminal() -> None:
    fake = FakeDynamo()
    run = _active(fake)
    _seed_waiter(fake)
    _release(fake, run)
    promote_shard(_context(fake), SHARD)
    fencing = store(fake).read_run(run)
    assert fencing is not None and fencing.state is RunState.FENCING
    assert _slot_run(fake) not in {None, run}


def test_v20_a_held_waiter_is_not_promoted_while_the_hold_is_set_then_once_after() -> None:
    fake = FakeDynamo()
    set_mode(fake, AuthorityMode.GRAPH, hold=True)
    fake.items.pop((f"SLOT#{UNIT}", "SLOT"), None)
    _seed_waiter(fake, held=True)
    counts = PromotionCounts()
    assert promote_unit(_context(fake), UNIT, counts) is PromoterBranch.SKIP_HELD
    assert kinds(fake, "WAITER#") != []
    fake.items[("MODE#ingestion", "MODE")]["hold"] = write_bool(False)
    assert promote_unit(_context(fake), UNIT, counts) is PromoterBranch.PROMOTE_WAITER
    assert promote_unit(_context(fake), UNIT, counts) is PromoterBranch.NOTHING
    assert counts.branches["promote_waiter"] == 1


def test_v20_interleaving_1_a_release_between_read_and_delete_is_refused_then_promoted() -> None:
    fake = FakeDynamo()
    run = _active(fake)
    _seed_waiter(fake)
    # An earlier wakeup exists while Y (the run) still occupies the slot.
    fake.items[(SHARD, f"PROMOTE#{UNIT}")] = {
        "pk": write_text(SHARD),
        "sk": write_text(f"PROMOTE#{UNIT}"),
        "wake_seq": write_int(1),
        "woken_at": write_text(now_text()),
    }

    def release(_fake: object) -> None:
        _release(fake, run)

    fake.before_transaction = release
    counts = PromotionCounts()
    branch = promote_unit(_context(fake), UNIT, counts)
    assert counts.guards == 1  # the occupied-branch delete lost to the release
    assert branch is PromoterBranch.PROMOTE_WAITER
    assert counts.branches == {"promote_waiter": 1}
    assert kinds(fake, "PROMOTE#") == []


def test_v20_interleaving_2_a_held_waiter_receipt_between_read_and_delete_is_refused() -> None:
    fake = FakeDynamo()
    set_mode(fake, AuthorityMode.GRAPH, hold=True)
    fake.items[(SHARD, f"PROMOTE#{UNIT}")] = {
        "pk": write_text(SHARD),
        "sk": write_text(f"PROMOTE#{UNIT}"),
        "wake_seq": write_int(1),
    }

    def held_receipt(_fake: Any) -> None:
        receipt = _receipt("held-0001", NOW)
        commit = AdmissionCommit(
            receipt,
            EffectKind.QUEUE_WAITER,
            UNIT,
            0,
            new_waiter=Waiter("held-0001", "8" * 64, NOW, True, 1),
            wake_held=True,
        )
        DynamoExecutionGraphStore("t", fake).commit_admission(commit)  # type: ignore[arg-type]

    fake.before_transaction = held_receipt
    counts = PromotionCounts()
    branch = promote_unit(_context(fake), UNIT, counts)
    assert counts.guards == 1
    assert branch is PromoterBranch.SKIP_HELD
    assert fake.items[(SHARD, f"PROMOTE#{UNIT}")]["wake_seq"] == {"N": "2"}
    fake.items[("MODE#ingestion", "MODE")]["hold"] = write_bool(False)
    assert promote_unit(_context(fake), UNIT, counts) is PromoterBranch.PROMOTE_WAITER
    assert counts.branches["promote_waiter"] == 1


def test_v20_occupied_with_no_concurrent_wakeup_deletes_and_promotes_nothing() -> None:
    fake = FakeDynamo()
    run = _active(fake)
    _seed_waiter(fake)
    fake.items[(SHARD, f"PROMOTE#{UNIT}")] = {
        "pk": write_text(SHARD),
        "sk": write_text(f"PROMOTE#{UNIT}"),
        "wake_seq": write_int(4),
    }
    counts = PromotionCounts()
    assert promote_unit(_context(fake), UNIT, counts) is PromoterBranch.OCCUPIED
    assert kinds(fake, "PROMOTE#") == []
    assert _slot_run(fake) == run
    assert kinds(fake, "WAITER#") != []


def test_v20_backstop_a_stranded_waiter_with_the_slot_free_is_promoted_once() -> None:
    fake = FakeDynamo()
    set_mode(fake, AuthorityMode.GRAPH)
    _seed_waiter(fake)
    assert kinds(fake, "PROMOTE#") == []
    counts = promote_shard(_context(fake), SHARD)
    assert counts.branches == {"promote_waiter": 1}
    assert promote_shard(_context(fake), SHARD).branches == {}


def test_v20_a_waiter_older_than_the_head_is_superseded_older_than_current() -> None:
    fake = FakeDynamo()
    set_mode(fake, AuthorityMode.GRAPH)
    waiter = _seed_waiter(fake, retrieved=NOW - timedelta(days=1))
    fake.items[(f"HEAD#{UNIT}", "HEAD")] = {
        "pk": write_text(f"HEAD#{UNIT}"),
        "sk": write_text("HEAD"),
        "retrieved_at": write_text(NOW.isoformat()),
    }
    counts = promote_shard(_context(fake), SHARD)
    assert counts.branches == {"supersede_waiter": 1}
    superseded = fake.items[(f"RECEIPT#{waiter.request_id}", "SUPERSEDED")]
    assert superseded["reason"] == {"S": "older_than_current"}
    assert _slot_run(fake) is None


def test_v20_row_19_a_waiter_behind_a_moved_slot_is_slot_revision_changed() -> None:
    fake = FakeDynamo()
    _active(fake)
    receipt = _receipt("late-0001", NOW)
    commit = AdmissionCommit(
        receipt,
        EffectKind.QUEUE_WAITER,
        UNIT,
        0,  # read before the slot moved to revision 1
        new_waiter=Waiter("late-0001", "8" * 64, NOW, False, 1),
    )
    with pytest.raises(AdmissionConflictError) as refused:
        DynamoExecutionGraphStore("t", fake).commit_admission(commit)  # type: ignore[arg-type]
    assert refused.value.signal == SLOT_REVISION_CHANGED
    assert kinds(fake, "WAITER#") == []


def test_v25_a_fencing_runs_terminal_cannot_clear_a_newer_runs_slot() -> None:
    fake = FakeDynamo()
    run = _active(fake)
    _seed_waiter(fake)
    _release(fake, run)
    promote_shard(_context(fake), SHARD)
    newer = _slot_run(fake)
    assert newer is not None and newer != run
    fenced = store(fake).read_run(run)
    assert fenced is not None
    from adapters.jobs.execution_graph.fence_settler import try_terminalize  # noqa: PLC0415

    assert try_terminalize(store(fake), run) is RunState.CANCELLED
    assert _slot_run(fake) == newer


def test_s3v9_a_hold_set_again_between_the_read_and_the_commit_refuses_the_promotion() -> None:
    fake = FakeDynamo()
    set_mode(fake, AuthorityMode.GRAPH, hold=True)
    mode = fake.items[("MODE#ingestion", "MODE")]
    mode["hold"] = write_bool(False)  # the operator cleared it: revision 2
    mode["hold_revision"] = write_int(2)
    fake.items.pop((f"SLOT#{UNIT}", "SLOT"), None)
    _seed_waiter(fake, held=True)

    def set_hold_again(_fake: Any) -> None:
        mode["hold"] = write_bool(True)
        mode["hold_revision"] = write_int(3)

    fake.before_transaction = set_hold_again
    counts = PromotionCounts()
    assert promote_unit(_context(fake), UNIT, counts) is PromoterBranch.SKIP_HELD
    assert counts.guards == 1
    assert kinds(fake, "WAITER#") != []
    assert _slot_run(fake) is None
    promotion = fake.transactions[-1] if fake.transactions else []
    assert not any("RUN#" in str(member) for member in promotion)


def test_s3v9_an_unheld_waiter_promotion_carries_no_mode_member() -> None:
    fake = FakeDynamo()
    set_mode(fake, AuthorityMode.GRAPH)
    fake.items.pop((f"SLOT#{UNIT}", "SLOT"), None)
    _seed_waiter(fake, held=False)
    counts = PromotionCounts()
    assert promote_unit(_context(fake), UNIT, counts) is PromoterBranch.PROMOTE_WAITER
    promotion = next(t for t in fake.transactions if any("WAITER#" in str(m) for m in t))
    assert not any("MODE#ingestion" in str(member) for member in promotion)


# --- S6 §2.2: the arm pass (units, the serving order, AGG4-1, AGG4-14) ------------------------

L_UNIT = "00000001" + "1" * 56
"""A conflict unit in `DUE#1`, walked before `X_UNIT`'s shard."""
X_UNIT = "00000009" + "9" * 56
"""A conflict unit in `DUE#9`."""


def _arm_context(fake: FakeDynamo) -> PromoterContext:
    return PromoterContext(release_store(fake), ingestion_node_plan, lambda: NOW)


def _arm(fake: FakeDynamo) -> ArmCounts:
    return arm_pass(_arm_context(fake), due_shards(), lambda: 60_000, "test-request")


def _state(fake: FakeDynamo, run: str, name: str) -> str:
    return str(fake.items[(f"RUN#{run}", f"NODE#{name}")]["state"]["S"])


def _arm_item(fake: FakeDynamo, run: str, unit: str = UNIT) -> dict[str, Any] | None:
    return fake.items.get((due_shard(unit), f"ARM#{run}"))


def _members(transaction: list[dict[str, Any]]) -> list[tuple[str, str, str]]:
    found: list[tuple[str, str, str]] = []
    for member in transaction:
        ((kind, body),) = member.items()
        key = body.get("Key") or body["Item"]
        found.append((kind, str(key["pk"]["S"]), str(key["sk"]["S"])))
    return found


def test_units_arm_one_node_per_transaction_and_arm_retires_with_the_last() -> None:
    fake = FakeDynamo()
    run = materialized_run(fake, "orders", "returns")
    arm = _arm_item(fake, run)
    assert arm is not None and arm["scope"] == {"S": "units"}
    assert arm["root_clock_started_at"] == {"S": ROOT_CLOCK}
    before = len(fake.transactions)

    counts = _arm(fake)

    assert (counts.arms, counts.armed, counts.retired) == (1, 2, 1)
    armings = fake.transactions[before:]
    assert len(armings) == 2  # one node per transaction
    for name in ("write_unit:orders", "write_unit:returns"):
        item = fake.items[(f"RUN#{run}", f"NODE#{name}")]
        assert item["state"] == {"S": "READY"}
        assert item["armed_generation"] == {"N": "0"}
        assert item["armed_at"] == {"S": now_text()}
        assert (f"LOCK#TABLE#{name.split(':')[1]}", "LOCK") in fake.items
    # Control: only the transaction that arms the scope's last PENDING node deletes `ARM#`.
    arm_key = ("Delete", SHARD, f"ARM#{run}")
    assert arm_key not in _members(armings[0])
    assert arm_key in _members(armings[1])
    assert _arm_item(fake, run) is None
    assert ("Update", f"RUN#{run}", "RUN") in _members(armings[0])  # the revision bump


def test_a_held_table_lock_is_a_lock_wait_and_the_unit_arms_once_it_is_released() -> None:
    fake = FakeDynamo()
    run = materialized_run(fake, "dim_campaign", "orders")
    fake.items[("LOCK#TABLE#dim_campaign", "LOCK")] = {
        "pk": write_text("LOCK#TABLE#dim_campaign"),
        "sk": write_text("LOCK"),
        "holder_node_id": write_text("another-runs-unit"),
    }

    waiting = _arm(fake)

    assert (waiting.armed, waiting.lock_wait, waiting.retired) == (1, 1, 0)
    assert _state(fake, run, "write_unit:dim_campaign") == "PENDING"
    assert _state(fake, run, "write_unit:orders") == "READY"
    assert _arm_item(fake, run) is not None  # the durable wait: `ARM#` stays
    # No deadlock: the holder settles and releases, and the next pass arms the unit.
    fake.items.pop(("LOCK#TABLE#dim_campaign", "LOCK"))
    after = _arm(fake)
    assert (after.armed, after.lock_wait, after.retired) == (1, 0, 1)
    assert _state(fake, run, "write_unit:dim_campaign") == "READY"
    assert _arm_item(fake, run) is None


def test_a_run_that_is_not_live_deletes_arm_alone_and_the_fence_settler_settles_its_units() -> None:
    fake = FakeDynamo()
    run = materialized_run(fake, "orders")
    record = store(fake).read_run(run)
    assert record is not None
    store(fake).fence_run(FenceRunRequest(record, FenceReason.CANCELLED, now_text()))
    before = len(fake.transactions)

    counts = _arm(fake)

    assert (counts.armed, counts.retired) == (0, 1)
    (retire,) = fake.transactions[before:]
    assert _members(retire) == [
        ("Delete", SHARD, f"ARM#{run}"),
        ("ConditionCheck", f"RUN#{run}", "RUN"),
    ]
    assert _state(fake, run, "write_unit:orders") == "PENDING"
    fenced = settle_shard(store(fake), SHARD, now_text)
    assert fenced.fenced_unsent == 2  # the unit and `grant_apply`, both never armed
    assert _state(fake, run, "write_unit:orders") == "FENCED_UNSENT"
    assert _state(fake, run, "grant_apply") == "FENCED_UNSENT"
    final = store(fake).read_run(run)
    assert final is not None and final.state is RunState.CANCELLED


def test_fence_pending_refuses_while_arm_exists_and_settles_once_it_is_gone() -> None:
    fake = FakeDynamo()
    run = materialized_run(fake, "orders")
    record = store(fake).read_run(run)
    assert record is not None
    store(fake).fence_run(FenceRunRequest(record, FenceReason.CANCELLED, now_text()))
    fencing = store(fake).read_run(run)
    pending = next(n for n in store(fake).read_nodes(run) if n.name == "write_unit:orders")
    assert fencing is not None
    with pytest.raises(GraphGuardError) as refused:
        store(fake).fence_pending(fencing, pending)
    assert refused.value.signal == RUN_ARMING_OPEN
    assert _state(fake, run, "write_unit:orders") == "PENDING"
    with pytest.raises(GraphGuardError) as terminal:
        store(fake).terminalize_run(fencing, store(fake).read_nodes(run))
    assert terminal.value.signal == RUN_ARMING_OPEN
    # Control: with `ARM#` absent the same node settles.
    store(fake).retire_arm(fencing, ArmScope.UNITS)
    store(fake).fence_pending(fencing, pending)
    assert _state(fake, run, "write_unit:orders") == "FENCED_UNSENT"


def test_retire_arm_refuses_a_live_run_with_its_exact_signal() -> None:
    fake = FakeDynamo()
    run = materialized_run(fake, "orders")
    record = store(fake).read_run(run)
    assert record is not None
    with pytest.raises(GraphGuardError) as refused:
        store(fake).retire_arm(record, ArmScope.UNITS)
    assert refused.value.signal == RUN_STATE_CHANGED
    assert _arm_item(fake, run) is not None


def test_db25_election_arms_bake_and_leaves_precompute_waiting_without_an_arm() -> None:
    fake = FakeDynamo()
    run = granted_run(fake)
    settle_publish(fake, run)
    arm = _arm_item(fake, run)
    assert arm is not None and arm["scope"] == {"S": "serving"}
    before = len(fake.transactions)

    first = _arm(fake)

    assert (first.armed, first.retired) == (1, 1)
    assert _state(fake, run, "bake") == "READY"
    assert _state(fake, run, "precompute") == "WAITING_INPUT"
    assert _arm_item(fake, run) is None
    reservation = fake.transactions[before:][-1]
    assert ("Update", f"RUN#{run}", "NODE#bake") in _members(reservation)
    assert ("Delete", SHARD, f"ARM#{run}") in _members(reservation)


def _elect_in_one_pass(
    fake: FakeDynamo, monkeypatch: pytest.MonkeyPatch | None = None
) -> tuple[ArmCounts, list[BaseException]]:
    """Run one serving election inside one `arm_pass`.

    The shape of dev runs 69e71e5d and 1b1f664b: one granted run in its domain, publish
    settled, and its serving `ARM#` read by the same pass that elects it.
    """
    failures: list[BaseException] = []
    context = PromoterContext(
        release_store(fake), ingestion_node_plan, lambda: NOW, on_failure=failures.append
    )
    if monkeypatch is not None:  # the pre-fix arm loop: every ARM# read before the election

        def every_arm(_context: PromoterContext, arms: Sequence[DueItem]) -> list[DueItem]:
            return list(arms)

        monkeypatch.setattr(promoter_module, "unconsumed_arms", every_arm)
    return arm_pass(context, due_shards(), lambda: 60_000, "test-request"), failures


def test_rolling_promoter_rejects_a_missing_lambda_clock() -> None:
    with pytest.raises(InvariantError) as refused:
        arm_pass(_arm_context(FakeDynamo()), due_shards(), cast("Any", None), "test-request")

    assert str(refused.value.code) == "ADLY-EXEC_GRAPH-0034"


def test_rolling_promoter_rejects_a_nonmonotonic_remaining_clock() -> None:
    readings = iter((60_000, 60_001))
    read = promoter_module._remaining_reader(lambda: next(readings))  # pyright: ignore[reportPrivateUsage]

    assert read() == 60_000
    with pytest.raises(InvariantError) as refused:
        read()

    assert str(refused.value.code) == "ADLY-EXEC_GRAPH-0035"


def test_td290_an_election_in_the_same_arm_pass_reports_no_failure() -> None:
    fake = FakeDynamo()
    run = granted_run(fake)
    settle_publish(fake, run)
    counts, failures = _elect_in_one_pass(fake)
    assert (counts.armed, counts.retired, counts.failed, failures) == (1, 1, 0, [])
    assert _state(fake, run, "bake") == "READY"
    assert _arm_item(fake, run) is None


def test_final_arm_loop_does_not_reprocess_a_serving_arm() -> None:
    fake = FakeDynamo()
    run = granted_run(fake)
    settle_publish(fake, run)
    assert _elect_in_one_pass(fake)[0].failed == 0
    stale = _arm_item(fake, run)
    assert stale is None


def _two_grants(fake: FakeDynamo) -> tuple[str, str]:
    """Grant L, then X, in one co-release domain: L holds the lower ordinal."""
    lower = granted_run(fake, unit=L_UNIT, fingerprint="1" * 64, request_id="r-lower")
    higher = granted_run(fake, unit=X_UNIT, fingerprint="9" * 64, request_id="r-higher")
    ordinals = [
        fake.items[(due_shard(unit), f"COMPLETE#{run}")]["grant_ordinal"]["N"]
        for run, unit in ((lower, L_UNIT), (higher, X_UNIT))
    ]
    assert ordinals == ["1", "2"]
    return lower, higher


def test_agg4_14_every_arm_read_precedes_the_first_complete_shard_read_control_l_absent() -> None:
    fake = FakeDynamo()
    higher = granted_run(fake, unit=X_UNIT, fingerprint="9" * 64, request_id="r-higher")
    settle_publish(fake, higher)
    fake.queries.clear()

    counts = _arm(fake)

    arm_reads = [i for i, (_pk, prefix) in enumerate(fake.queries) if prefix == "ARM#"]
    walk_reads = [i for i, (_pk, prefix) in enumerate(fake.queries) if prefix == "COMPLETE#"]
    assert len(arm_reads) == 32
    assert len(walk_reads) == 32
    assert max(arm_reads[:16]) < min(walk_reads)
    # Control: with L absent, X's `ARM#` was read before the walk and its `COMPLETE#` is in it.
    assert counts.armed == 1
    assert _state(fake, higher, "bake") == "READY"
    assert _state(fake, higher, "precompute") == "WAITING_INPUT"


def _stash(fake: FakeDynamo, lower: str, higher: str) -> dict[tuple[str, str], Any]:
    """Remove `COMPLETE#L`, `COMPLETE#X` and X's `ARM#`, to write back after L's shard read."""
    keys = [
        (due_shard(L_UNIT), f"COMPLETE#{lower}"),
        (due_shard(X_UNIT), f"COMPLETE#{higher}"),
        (due_shard(X_UNIT), f"ARM#{higher}"),
    ]
    return {key: fake.items.pop(key) for key in keys}


def _write_back_after_l(stashed: dict[tuple[str, str], Any]) -> Any:
    def hook(fake: FakeDynamo, pk: str, prefix: str) -> None:
        if pk == due_shard(L_UNIT) and prefix == "COMPLETE#":
            fake.after_query = None
            fake.items.update(stashed)

    return hook


def test_agg4_14_an_arm_written_during_the_walk_waits_and_the_lower_ordinal_holds_the_turn() -> (
    None
):
    fake = FakeDynamo()
    lower, higher = _two_grants(fake)
    settle_publish(fake, higher)
    stashed = _stash(fake, lower, higher)
    # A serving `ARM#` read before the walk, so this pass walks (an unrelated, waiting run).
    other = granted_run(fake, unit="0000000c" + "c" * 56, fingerprint="c" * 64, request_id="r-c")
    settle_publish(fake, other)
    fake.after_query = _write_back_after_l(stashed)

    _arm(fake)

    assert fake.after_query is None and set(stashed) <= set(fake.items)  # written mid-walk
    assert _state(fake, higher, "bake") == "PENDING"  # X's `ARM#` was not read
    second = _arm(fake)
    assert _state(fake, higher, "bake") == "PENDING"  # L holds the lowest ordinal
    assert second.armed == 0  # the complete lower gap blocks every election


def test_agg4_14_the_wrong_order_arms_x_so_the_fixture_can_fail() -> None:
    fake = FakeDynamo()
    lower, higher = _two_grants(fake)
    settle_publish(fake, higher)
    stashed = _stash(fake, lower, higher)
    fake.after_query = _write_back_after_l(stashed)
    context = _arm_context(fake)
    counts = ArmCounts()

    walk = walk_complete(context, due_shards())  # the walk first: it misses COMPLETE#L
    arms = read_arms(context, due_shards(), counts)  # then the ARM# reads: X's is there
    for item in arms:
        arm_run(context, item, walk, counts)

    assert walk is not None and [item.run_id for item in walk] == [higher]
    assert _state(fake, higher, "bake") == "PENDING"


def test_agg4_14_second_control_an_arm_whose_complete_the_walk_did_not_see_waits() -> None:
    fake = FakeDynamo()
    higher = granted_run(fake, unit=X_UNIT, fingerprint="9" * 64, request_id="r-higher")
    settle_publish(fake, higher)
    fake.items.pop((due_shard(X_UNIT), f"COMPLETE#{higher}"))

    counts = _arm(fake)

    assert counts.armed == 0
    assert _state(fake, higher, "bake") == "PENDING"
    assert _state(fake, higher, "precompute") == "WAITING_INPUT"
    assert _arm_item(fake, higher, X_UNIT) is not None


def test_a_failed_complete_shard_read_arms_no_serving_node_but_units_still_arm() -> None:
    fake = FakeDynamo()
    higher = granted_run(fake, unit=X_UNIT, fingerprint="9" * 64, request_id="r-higher")
    settle_publish(fake, higher)
    units = materialized_run(fake, "orders", unit=L_UNIT, fingerprint="2" * 64, request_id="r-u")
    failures: list[BaseException] = []
    original = fake.query

    def failing(**kwargs: Any) -> Any:
        if kwargs["ExpressionAttributeValues"][":prefix"]["S"] == "COMPLETE#":
            fake.fail_query = "COMPLETE#"
        return original(**kwargs)

    fake.query = failing  # type: ignore[method-assign]
    context = PromoterContext(
        release_store(fake), ingestion_node_plan, lambda: NOW, on_failure=failures.append
    )

    counts = arm_pass(context, due_shards(), lambda: 60_000, "test-request")

    assert counts.walk_failed == 2 and len(failures) == 2
    # The one armed node is the units batch member; no serving node is armed.
    assert (counts.armed, counts.batches_armed, counts.batch_members) == (1, 1, 1)
    assert _state(fake, higher, "bake") == "PENDING"
    assert _state(fake, higher, "precompute") == "WAITING_INPUT"
    # Control: the units scope needs no walk, so the same pass arms it.
    assert _state(fake, units, "write_unit:orders") == "READY"


# --- Rolling A-to-B election: publication during ACTIVE, one final transaction ----------------


def _rolling_head(fake: FakeDynamo) -> dict[str, Any]:
    return copy.deepcopy(fake.items[(f"SERVING_DOMAIN#{INGESTION_RELEASE_DOMAIN}", "HEAD")])


def _member_keys(capture: FinalTransactionCapture) -> list[tuple[str, str, str]]:
    return _members(capture.request["TransactItems"])


def test_b_publishes_while_a_is_active_and_a_final_transaction_elects_b() -> None:
    fake = FakeDynamo()
    a_runs = tuple(grant_and_publish(fake, "a", index) for index in range(3))
    promoter_pass(fake, "elect-a")
    a_active = _rolling_head(fake)
    assert a_active["state"] == write_text("ACTIVE")
    a_store = release_store(fake).read_serving_domain(INGESTION_RELEASE_DOMAIN)
    assert a_store.head is not None and a_store.head.elected_frontier is not None
    a_eligible = a_store.head.elected_frontier.eligible_counts

    b_runs = tuple(grant_and_publish(fake, "b", index) for index in range(2))

    # Every B flip and settlement committed while A stayed ACTIVE, and A did not move.
    assert _rolling_head(fake) == a_active
    for run in b_runs:
        marker = fake.items[(f"SERVING_PUBLICATION#{run}", "MARKER")]
        assert marker["state"] == write_text("SETTLED_ELIGIBLE")
        shard = int(marker["frontier_shard"]["N"])
        assert int(marker["eligible_coordinate"]["N"]) > a_eligible[shard]
    a_cohort = a_store.head.cohort_revision
    a_obligation = fake.items[(due_shard(INGESTION_RELEASE_DOMAIN), f"COALESCE#{a_cohort}")]
    assert {value["S"] for value in a_obligation["run_ids"]["L"]} <= set(a_runs)
    assert not {value["S"] for value in a_obligation["run_ids"]["L"]} & set(b_runs)

    complete_direct_winner(fake, a_runs[-1])
    drain_nonfinal_aliases(fake)
    capture = capture_final_transaction(DrainActivationWorld(fake, a_runs, b_runs))

    # One transaction drains A's final alias and activates B: no post-drain loss window.
    assert capture is not None
    keys = _member_keys(capture)
    final = release_store(fake).read_run(a_runs[0])
    assert final is not None and final.grant_id is not None
    assert ("Put", f"SERVING#{final.grant_id}", "BINDING") in keys or (
        "Update",
        f"SERVING#{final.grant_id}",
        "BINDING",
    ) in keys
    assert ("Update", f"SERVING_DOMAIN#{INGESTION_RELEASE_DOMAIN}", "HEAD") in keys
    assert ("Delete", due_shard(INGESTION_RELEASE_DOMAIN), f"COALESCE#{a_cohort}") in keys
    head = release_store(fake).read_serving_domain(INGESTION_RELEASE_DOMAIN).head
    assert head is not None and head.state == "ACTIVE"
    assert head.winner_run_id == b_runs[-1]
    assert set(head.elected_run_ids) == set(b_runs)
    transition = release_store(fake).read_serving_promoter_transition(
        INGESTION_RELEASE_DOMAIN, cast("str", a_cohort)
    )
    assert transition is not None and transition.elected_cohort_revision == head.cohort_revision
    assert _state(fake, b_runs[-1], "bake") == "READY"
    assert _state(fake, b_runs[0], "bake") == "PENDING"


_GUARD_MS = 10_493
"""The measured guard (FreshElectionTimingV1, 2026-10-15), pinned so a contract change is seen."""


def test_below_the_election_guard_the_final_alias_waits_and_the_next_invocation_elects() -> None:
    world = build_world(3, 2)
    fake = world.fake
    before = copy.deepcopy(fake.items)

    waiting = capture_final_transaction(world, "low-clock", lambda: _GUARD_MS - 1)

    assert waiting is None
    assert fake.items == before  # the final alias and its COALESCE# stay durable
    elected = capture_final_transaction(world, "next-invocation", lambda: _GUARD_MS)
    assert elected is not None and len(elected.request["TransactItems"]) == 51


def test_a_publication_racing_the_final_transaction_fails_it_whole_and_joins_the_retry() -> None:
    world = build_world(3, 2)
    fake = world.fake
    late = granted_run(
        fake, unit="0000000d" + "d" * 56, fingerprint="d" * 64, request_id="r-late-arrival"
    )
    head_before = _rolling_head(fake)
    final = world.final_alias
    final_record = release_store(fake).read_run(final)
    assert final_record is not None and final_record.grant_id is not None
    alias_before = fake.items.get((f"SERVING#{final_record.grant_id}", "BINDING"))
    cohort = str(head_before["cohort_revision"]["S"])
    obligation = (due_shard(INGESTION_RELEASE_DOMAIN), f"COALESCE#{cohort}")
    obligation_before = copy.deepcopy(fake.items[obligation])

    def arrive(table: FakeDynamo) -> None:
        publish_rolling(table, late)

    fake.before_transaction = arrive
    failures: list[BaseException] = []
    raced = arm_pass(
        PromoterContext(
            release_store(fake), ingestion_node_plan, lambda: NOW, on_failure=failures.append
        ),
        due_shards(),
        lambda: 60_000,
        "raced",
    )
    # The domain's failure is isolated: reported exactly once and counted, never swallowed.
    assert [type(error) for error in failures] == [GraphGuardError]
    assert raced.failed == 1

    # The arrival committed first; the whole final transaction failed and drained nothing.
    assert fake.items[(f"SERVING_PUBLICATION#{late}", "MARKER")]["state"] == write_text(
        "SETTLED_ELIGIBLE"
    )
    assert _rolling_head(fake) == head_before
    assert fake.items.get((f"SERVING#{final_record.grant_id}", "BINDING")) == alias_before
    assert fake.items[obligation] == obligation_before
    retried = capture_final_transaction(world, "fresh-retry")
    assert retried is not None and len(retried.request["TransactItems"]) == 51
    head = release_store(fake).read_serving_domain(INGESTION_RELEASE_DOMAIN).head
    assert head is not None and set(head.elected_run_ids) == {*world.b_runs, late}
    assert head.winner_run_id == late


# --- Promoter-domain children: one typed child per attempted cohort, isolated failure ---------


class _DomainSpans:
    """A `span` stand-in recording each child's attributes and whether it was marked failed."""

    def __init__(self) -> None:
        self.opened: list[dict[str, object]] = []

    @contextlib.contextmanager
    def __call__(self, name: str, **attributes: object) -> Generator[Any]:
        carried: dict[str, object] = {"name": name, **attributes, "errored": False}
        self.opened.append(carried)

        def note_failure(error: BaseException) -> None:
            carried["errored"] = True
            carried["error_type"] = type(error).__name__

        try:
            yield SimpleNamespace(record=carried.update, note_failure=note_failure)
        except BaseException:
            carried["errored"] = True
            raise


def _two_draining_domains(
    monkeypatch: pytest.MonkeyPatch, failing: str, spans: _DomainSpans
) -> tuple[ArmCounts, list[BaseException], list[str]]:
    """Two BOUND_DRAINING domains; the first one's drain raises, the second one waits."""
    walk = tuple(
        SimpleNamespace(domain=domain, run_id=f"run-{domain}") for domain in ("dom-a", "dom-b")
    )
    decided: list[str] = []

    def iteration(*_args: object) -> tuple[tuple[DueItem, ...], tuple[Any, ...]]:
        return (), walk

    def read_serving_domain(domain: str) -> Any:
        head = SimpleNamespace(
            state="BOUND_DRAINING", direct_grant_id=f"grant-{domain}", cohort_revision=f"c-{domain}"
        )
        return SimpleNamespace(head=head, frontier=object())

    def drain(_state: object, domain: str, _authority: object) -> Any:
        decided.append(domain)
        if failing == "clock":
            raise InvariantError(promoter_module._PROMOTER_CLOCK_INVALID, "fictional clock.")  # pyright: ignore[reportPrivateUsage]
        if domain == failing:
            raise InvariantError(promoter_module._ARM_INVARIANT, "fictional malformed domain.")  # pyright: ignore[reportPrivateUsage]
        return ServingDomainElectionResult(
            domain, ServingElectionOutcome.DRAIN_INCOMPLETE, drained_cohort_revision=f"c-{domain}"
        )

    monkeypatch.setattr(promoter_module, "_read_serving_iteration", iteration)
    monkeypatch.setattr(promoter_module, "_drain_bound", drain)
    monkeypatch.setattr(promoter_module, "span", spans)
    failures: list[BaseException] = []
    context = PromoterContext(
        cast("Any", SimpleNamespace(read_serving_domain=read_serving_domain)),
        ingestion_node_plan,
        lambda: NOW,
        on_failure=failures.append,
    )
    counts = ArmCounts()
    results = promoter_module.coalesce_serving(
        context, due_shards(), counts, lambda: 60_000, "request-1"
    )
    assert [item.domain for item in results] == [d for d in ("dom-a", "dom-b") if d != failing]
    return counts, failures, decided


def test_an_isolated_domain_failure_marks_one_child_reports_once_and_the_next_domain_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spans = _DomainSpans()

    counts, failures, decided = _two_draining_domains(monkeypatch, "dom-a", spans)

    assert decided == ["dom-a", "dom-b"]
    assert [type(error) for error in failures] == [InvariantError]
    assert counts.failed == 1
    children = [item for item in spans.opened if item["name"] == "execution_graph.promoter_domain"]
    assert [(item["domain"], item["cohort_revision"]) for item in children] == [
        ("dom-a", "c-dom-a"),
        ("dom-b", "c-dom-b"),
    ]
    assert all(item["aws_request_id"] == "request-1" for item in children)
    failed, waited = children
    assert (failed["errored"], failed["outcome"], failed["error_type"]) == (
        True,
        "failed",
        "InvariantError",
    )
    # An expected wait is a closed, non-error outcome.
    assert (waited["errored"], waited["outcome"]) == (
        False,
        ServingElectionOutcome.DRAIN_INCOMPLETE,
    )


def test_every_domain_attempt_records_a_declared_closed_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spans = _DomainSpans()

    counts, failures, _decided = _two_draining_domains(monkeypatch, "none", spans)

    assert (counts.failed, failures) == (0, [])
    closed = {item.value for item in ServingElectionOutcome} | {"failed"}
    (declaration,) = [
        span
        for span in load_trace_contract(Path(__file__).resolve().parents[2]).spans
        if span.name == "execution_graph.promoter_domain"
    ]
    declared = set(declaration.allowed_attributes)
    for child in spans.opened:
        assert child["outcome"] in closed
        assert set(child) - {"name", "errored"} <= declared


def test_an_invalid_remaining_clock_ends_the_pass_instead_of_being_isolated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spans = _DomainSpans()

    with pytest.raises(InvariantError) as refused:
        _two_draining_domains(monkeypatch, "clock", spans)
    assert str(refused.value.code) == "ADLY-EXEC_GRAPH-0035"
    # The pass ends at the first domain; the second is never attempted.
    assert [item["domain"] for item in spans.opened] == ["dom-a"]
