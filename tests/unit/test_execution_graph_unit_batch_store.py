# SPDX-License-Identifier: MIT-0
"""Slice 7 (1b) store calls: batch arming, permit and send, return, success sets, re-admission.

Design §2.2 ("Batch arming", "Dispatching a unit batch") and §2.5 ("Return", "Lock release
under 1b", step 6); impl-plan slice 7 tests 5, 7, 8, 1c (store) and 12 (store). Every test drives
the real adapter over the transactional fake, so each refusal is the adapter's own condition.
Fictional data only.
"""

from __future__ import annotations

import json
from typing import Any, cast

import pytest
from botocore.exceptions import ClientError
from execution_graph_dispatch_support import now_text, store
from execution_graph_release_support import release_store
from execution_graph_support import FakeDynamo
from unit_batch_support import (
    arm,
    claim,
    lock,
    member,
    node_item,
    node_of,
    open_members,
    outbox,
    permit_and_send,
    unit_run,
)

from adapters.outbound.dynamodb.execution_graph_dispatch import batch_row_shard
from domain.operations.execution_graph_dispatch import (
    DOMAIN_LOCK_HELD,
    NODE_STATE_CHANGED,
    GraphGuardError,
    NodeState,
    ReturnReason,
)
from ports.outbound.execution_graph_dispatch import (
    BatchPermitRequest,
    ReturnUnitRequest,
    UnitSetGuardError,
    UnitSetMember,
    UnitSetSettlement,
)

pytestmark = pytest.mark.unit


def _kinds(transaction: list[dict[str, Any]]) -> list[tuple[str, str, str]]:
    found: list[tuple[str, str, str]] = []
    for item in transaction:
        ((kind, body),) = item.items()
        key = body["Item"] if kind == "Put" else body["Key"]
        found.append((kind, str(key["pk"]["S"]), str(key["sk"]["S"])))
    return found


def _three_runs(fake: FakeDynamo, key: str = "orders") -> tuple[str, ...]:
    return tuple(unit_run(fake, index, key) for index in (1, 2, 3))


def test_arm_unit_batch_arms_three_runs_in_one_transaction_with_a_counted_lock() -> None:
    fake = FakeDynamo()
    runs = _three_runs(fake)
    before = len(fake.transactions)
    batch = arm(fake, "orders", runs)
    (transaction,) = fake.transactions[before:]
    assert len(transaction) == 2 + 4 * 3
    assert open_members(fake, "orders") == 3
    assert lock(fake, "orders")["holder_batch_id"] == {"S": batch}
    row = outbox(fake, batch)
    assert [ref.run_id for ref in row.members] == list(runs)
    assert (row.node_name, row.lock_key, row.state) == ("write_unit", "TABLE#orders", "OPEN")
    for run in runs:
        item = node_item(fake, run, "orders")
        assert (item["state"], item["batch_id"]) == ({"S": "READY"}, {"S": batch})
        assert not [key for key in fake.items if key[1] == f"ARM#{run}"]  # retired
    # Control: while the lock is held the next grant is DOMAIN_LOCK_HELD and writes nothing.
    late = unit_run(fake, 4, "orders")
    held = len(fake.transactions)
    with pytest.raises(GraphGuardError) as refused:
        arm(fake, "orders", (late,))
    assert refused.value.signal == DOMAIN_LOCK_HELD
    assert len(fake.transactions) == held
    assert node_item(fake, late, "orders")["state"] == {"S": "PENDING"}


def test_a_free_counted_lock_at_zero_is_granted_again() -> None:
    fake = FakeDynamo()
    (run,) = (unit_run(fake, 1, "orders"),)
    lock_item = {
        "pk": {"S": "LOCK#TABLE#orders"},
        "sk": {"S": "LOCK"},
        "holder_batch_id": {"S": "old"},
        "open_members": {"N": "0"},
    }
    fake.items[("LOCK#TABLE#orders", "LOCK")] = lock_item
    batch = arm(fake, "orders", (run,))
    assert (open_members(fake, "orders"), lock(fake, "orders")["holder_batch_id"]["S"]) == (
        1,
        batch,
    )


def test_more_than_the_member_bound_is_refused_before_any_write() -> None:
    fake = FakeDynamo()
    runs = tuple(unit_run(fake, index, "orders") for index in range(1, 26))
    before = len(fake.transactions)
    with pytest.raises(ValueError, match="1 to 24 members"):
        arm(fake, "orders", runs)
    assert len(fake.transactions) == before
    # Control: 24 members arm in one transaction of 2 + 4 x 24 = 98 actions.
    arm(fake, "orders", runs[:24])
    assert len(fake.transactions[-1]) == 98


def test_batch_permit_and_send_close_the_row_and_dispatch_every_member() -> None:
    fake = FakeDynamo()
    runs = _three_runs(fake)
    batch = arm(fake, "orders", runs)
    permit_and_send(fake, "orders", batch, runs)
    assert (batch_row_shard(batch), f"OUTBOX#{batch}") not in fake.items
    assert {node_of(fake, run, "orders").state for run in runs} == {NodeState.DISPATCHED}
    # Control: a permit naming no member is refused before any write.
    other = unit_run(fake, 9, "returns")
    second = arm(fake, "returns", (other,))
    with pytest.raises(ValueError, match="at least one permitted member"):
        store(fake).acquire_batch_permit(BatchPermitRequest(outbox(fake, second), (), now_text()))
    assert outbox(fake, second).state == "OPEN"


def test_an_empty_batch_row_is_deleted_only_while_open() -> None:
    fake = FakeDynamo()
    run = unit_run(fake, 1, "orders")
    batch = arm(fake, "orders", (run,))
    store(fake).delete_batch_row(outbox(fake, batch))
    assert (batch_row_shard(batch), f"OUTBOX#{batch}") not in fake.items
    # Control: a SENDING row is not deleted by the empty-row path.
    other = unit_run(fake, 2, "returns")
    second = arm(fake, "returns", (other,))
    row = outbox(fake, second)
    store(fake).acquire_batch_permit(
        BatchPermitRequest(row, (member(fake, other, "returns"),), now_text())
    )
    with pytest.raises(GraphGuardError) as refused:
        store(fake).delete_batch_row(row)
    assert refused.value.signal == NODE_STATE_CHANGED


def _claimed(fake: FakeDynamo, key: str, runs: tuple[str, ...], **arming: Any) -> str:
    batch = arm(fake, key, runs, **arming)
    permit_and_send(fake, key, batch, runs)
    for run in runs:
        claim(fake, run, key, batch)
    return batch


def test_8_a_return_from_claimed_releases_its_share_lease_and_reputs_arm() -> None:
    fake = FakeDynamo()
    runs = _three_runs(fake)
    batch = _claimed(fake, "orders", runs)
    run = runs[1]
    assert not [key for key in fake.items if key[1] == f"ARM#{run}"]  # retired at arming
    item = member(fake, run, "orders")
    held = store(fake).read_claim(item.node.node_id)
    assert held is not None and held.batch_id == batch
    store(fake).return_unit(
        ReturnUnitRequest(item.run, item.node, ReturnReason.MEMBER_FAILED, now_text(), held)
    )
    returned = node_of(fake, run, "orders")
    assert (returned.state, returned.return_reason, returned.arm_alone) == (
        NodeState.PENDING,
        "member_failed",
        True,
    )
    assert (returned.batch_id, returned.permit_generation, returned.final_return) == (
        None,
        None,
        False,
    )
    assert open_members(fake, "orders") == 2
    released = store(fake).read_claim(item.node.node_id)
    assert released is not None and released.lease_expires_at == now_text()
    arms = [key for key in fake.items if key[1] == f"ARM#{run}"]
    assert len(arms) == 1 and fake.items[arms[0]]["scope"] == {"S": "units"}
    assert (f"TOKEN#{item.node.node_id}", "TOKEN") not in fake.items


def test_8_a_return_keeps_an_existing_arm_and_a_dispatcher_return_sets_neither_flag() -> None:
    fake = FakeDynamo()
    run = unit_run(fake, 1, "orders", "returns")
    batch = arm(fake, "orders", (run,), retire=())  # `returns` is still PENDING: ARM# stays
    arms = [key for key in fake.items if key[1] == f"ARM#{run}"]
    assert len(arms) == 1
    item = member(fake, run, "orders")
    store(fake).return_unit(
        ReturnUnitRequest(item.run, item.node, ReturnReason.RUN_PAUSED, now_text())
    )
    returned = node_of(fake, run, "orders")
    assert (returned.state, returned.arm_alone, returned.final_return) == (
        NodeState.PENDING,
        False,
        False,
    )
    assert open_members(fake, "orders") == 0
    assert fake.items[arms[0]]["scope"] == {"S": "units"}
    assert lock(fake, "orders")["holder_batch_id"] == {"S": batch}


def test_8_an_over_release_is_refused_and_an_exact_release_succeeds() -> None:
    fake = FakeDynamo()
    run = unit_run(fake, 1, "orders")
    arm(fake, "orders", (run,))
    lock(fake, "orders")["open_members"] = {"N": "0"}
    item = member(fake, run, "orders")
    with pytest.raises(GraphGuardError) as refused:
        store(fake).return_unit(
            ReturnUnitRequest(item.run, item.node, ReturnReason.RUN_PAUSED, now_text())
        )
    assert refused.value.signal == NODE_STATE_CHANGED
    assert node_of(fake, run, "orders").state is NodeState.READY
    # Control: the exact release succeeds.
    lock(fake, "orders")["open_members"] = {"N": "1"}
    store(fake).return_unit(
        ReturnUnitRequest(item.run, item.node, ReturnReason.RUN_PAUSED, now_text())
    )
    assert open_members(fake, "orders") == 0


def test_a_second_final_return_is_refused_by_the_store() -> None:
    fake = FakeDynamo()
    run = unit_run(fake, 1, "orders")
    arm(fake, "orders", (run,))
    first = member(fake, run, "orders")
    store(fake).return_unit(
        ReturnUnitRequest(first.run, first.node, ReturnReason.FINAL_UNISOLATED, now_text())
    )
    assert (node_of(fake, run, "orders").final_return, node_of(fake, run, "orders").arm_alone) == (
        True,
        True,
    )
    # Re-armed alone, then a second `final_unisolated` return is refused.
    arm(fake, "orders", (run,), armed_at="2026-10-07T09:05:00+00:00")
    again = member(fake, run, "orders")
    with pytest.raises(GraphGuardError) as refused:
        store(fake).return_unit(
            ReturnUnitRequest(again.run, again.node, ReturnReason.FINAL_UNISOLATED, now_text())
        )
    assert refused.value.signal == NODE_STATE_CHANGED
    assert node_of(fake, run, "orders").state is NodeState.READY


def _success(fake: FakeDynamo, key: str, runs: tuple[str, ...], batch: str, **extra: Any) -> Any:  # noqa: PLR0913, PLR0917 -- one fixture builder
    record = extra.pop("record", None)
    members: list[UnitSetMember] = []
    for run in runs:
        item = member(fake, run, key)
        members.append(
            UnitSetMember(
                item.run,
                item.node,
                "e" * 64,
                store(fake).read_claim(item.node.node_id),
                record,
            )
        )
    return UnitSetSettlement(f"TABLE#{key}", batch, tuple(members), now_text(), **extra)


def test_7_a_one_member_success_set_is_settle_members_plus_the_counted_decrement() -> None:
    fake = FakeDynamo()
    run = unit_run(fake, 1, "orders")
    batch = _claimed(fake, "orders", (run,))
    before = len(fake.transactions)
    committed = store(fake).settle_unit_set(_success(fake, "orders", (run,), batch, record="{}"))
    (transaction,) = fake.transactions[before:]
    node = node_of(fake, run, "orders")
    assert committed == (node.node_id,)
    assert [kind for kind, _, _ in _kinds(transaction)] == ["Update", "Delete", "Update", "Update"]
    assert _kinds(transaction)[-1] == ("Update", "LOCK#TABLE#orders", "LOCK")
    assert transaction[-1]["Update"]["ExpressionAttributeValues"][":minus"] == {"N": "-1"}
    assert (node.state, node.effect_digest, node.effect_record) == (
        NodeState.SUCCEEDED,
        "e" * 64,
        "{}",
    )
    assert open_members(fake, "orders") == 0


def test_7_three_members_settle_with_exactly_one_minus_three() -> None:
    fake = FakeDynamo()
    runs = _three_runs(fake)
    batch = _claimed(fake, "orders", runs)
    before = len(fake.transactions)
    store(fake).settle_unit_set(_success(fake, "orders", runs, batch))
    (transaction,) = fake.transactions[before:]
    locks = [item for item in transaction if "LOCK#" in str(item)]
    assert len(locks) == 1
    assert locks[0]["Update"]["ExpressionAttributeValues"][":minus"] == {"N": "-3"}
    assert open_members(fake, "orders") == 0


# --- F3 (FEAT-009 review): the 4 MB aggregate, measured with the shared byte oracle -----------

_PADDED_NODE = 250_000
"""A node item near a 256 KiB envelope (`PROVIDER_CEILING_BYTES`), as a padding attribute."""
_LARGE_EFFECT = 120_000
"""An effect record that keeps the settled node item under 400 KB."""


def _pad(fake: FakeDynamo, runs: tuple[str, ...], key: str, size: int) -> None:  # noqa: PLR0913, PLR0917 -- one fixture builder
    for run in runs:
        node_item(fake, run, key)["padding"] = {"S": "p" * size}


def _actual_bytes(fake: FakeDynamo, transaction: list[dict[str, Any]]) -> int:
    """Return the oracle size of every item the committed transaction left behind."""
    from adapters.outbound.dynamodb.graph_transaction_bytes import item_bytes  # noqa: PLC0415

    total = 0
    for _kind, pk, sk in _kinds(transaction):
        stored = fake.items.get((pk, sk))
        total += 0 if stored is None else item_bytes(cast("Any", stored))
    return total


def _large_set(fake: FakeDynamo, count: int) -> tuple[tuple[str, ...], str]:
    runs = tuple(unit_run(fake, index, "orders") for index in range(1, count + 1))
    _pad(fake, runs, "orders", _PADDED_NODE)
    return runs, _claimed(fake, "orders", runs)


def test_5_a_set_near_4_mb_settles_in_two_chunks_and_only_the_last_frees_the_lock() -> None:
    fake = FakeDynamo()
    runs, batch = _large_set(fake, 12)
    request = _success(fake, "orders", runs, batch, record="r" * _LARGE_EFFECT)
    seen: list[int] = []
    before = len(fake.transactions)

    def observe(_fake: FakeDynamo) -> None:
        seen.append(open_members(fake, "orders"))

    fake.before_transaction = observe
    committed = store(fake).settle_unit_set(request)
    first, second = fake.transactions[before:]
    assert committed == tuple(member(fake, run, "orders").node.node_id for run in runs)
    assert seen == [12]  # the hook runs once, before the first chunk
    assert open_members(fake, "orders") == 0
    # Near the limit: the first chunk's items, as the fake (DynamoDB's stand-in) measured them.
    assert 3_500_000 < _actual_bytes(fake, first) < 4_000_000
    assert (
        len([k for k in _kinds(first) if k[2].startswith("NODE#")])
        + len([k for k in _kinds(second) if k[2].startswith("NODE#")])
        == 12
    )
    # Control: the same members' writes in one transaction are refused by the 4 MB limit.
    one = FakeDynamo()
    one_runs, one_batch = _large_set(one, 12)
    whole = _success(one, "orders", one_runs, one_batch, record="r" * _LARGE_EFFECT)
    from adapters.outbound.dynamodb.execution_graph_dispatch import (  # noqa: PLC0415
        counted_release,
    )

    adapter = cast("Any", store(one))
    writes = [w for m in whole.members for w in adapter._unit_success_members(whole, m)]
    writes.append(counted_release("TABLE#orders", one_batch, 12))
    with pytest.raises(ClientError) as refused:
        one.transact_write_items(TransactItems=[adapter._bind(w).item for w in writes])
    assert refused.value.response.get("Error", {}).get("Code") == "ValidationException"
    # Control: small items settle in one chunk.
    small = FakeDynamo()
    small_runs = tuple(unit_run(small, index, "orders") for index in range(1, 13))
    small_batch = _claimed(small, "orders", small_runs)
    small_before = len(small.transactions)
    store(small).settle_unit_set(_success(small, "orders", small_runs, small_batch, record="{}"))
    assert len(small.transactions) - small_before == 1


def test_5_a_guard_in_a_later_chunk_names_what_committed() -> None:
    fake = FakeDynamo()
    runs, batch = _large_set(fake, 12)
    request = _success(fake, "orders", runs, batch, record="r" * _LARGE_EFFECT)
    # A takeover bumps the last member's lease before its chunk commits.
    last = member(fake, runs[-1], "orders").node.node_id
    claim_item = fake.items[(f"EXEC_CLAIM#{last}", "CLAIM")]
    claim_item["lease_version"] = {"N": str(int(claim_item["lease_version"]["N"]) + 1)}
    with pytest.raises(UnitSetGuardError) as refused:
        store(fake).settle_unit_set(request)
    done = len(refused.value.committed)
    assert 1 <= done < 12
    assert refused.value.committed == tuple(
        member(fake, run, "orders").node.node_id for run in runs[:done]
    )
    assert open_members(fake, "orders") == 12 - done
    assert node_of(fake, runs[-1], "orders").state is NodeState.CLAIMED


def test_f3_the_fake_refuses_a_transaction_over_4_mb_and_accepts_one_under() -> None:
    def puts(count: int) -> list[dict[str, Any]]:
        return [
            {
                "Put": {
                    "TableName": "t",
                    "Item": {
                        "pk": {"S": f"PAD#{index}"},
                        "sk": {"S": "PAD"},
                        "padding": {"S": "p" * 380_000},
                    },
                }
            }
            for index in range(count)
        ]

    fake = FakeDynamo()
    with pytest.raises(ClientError) as refused:
        fake.transact_write_items(TransactItems=puts(11))
    assert refused.value.response.get("Error", {}).get("Code") == "ValidationException"
    assert fake.transactions == []
    # Control: ten such items (3.8 MB) commit.
    fake.transact_write_items(TransactItems=puts(10))
    assert len(fake.transactions) == 1


def test_f3_a_grant_over_4_mb_is_refused_before_any_write_and_the_promoter_cuts_it() -> None:
    def padded(count: int) -> tuple[FakeDynamo, tuple[str, ...]]:
        fake = FakeDynamo()
        runs = tuple(
            unit_run(fake, index, "orders", clock=f"2026-10-07T{index:02d}:00:00+00:00")
            for index in range(1, count + 1)
        )
        _pad(fake, runs, "orders", 300_000)
        return fake, runs

    fake, runs = padded(16)
    before = len(fake.transactions)
    counts = _arm_pass(fake)
    taken = counts.batch_members
    assert 1 < taken < 16
    assert (counts.batches_armed, counts.lock_wait) == (1, 0)
    assert open_members(fake, "orders") == taken
    (armed,) = fake.transactions[before:]
    assert 3_000_000 < _actual_bytes(fake, armed) < 4_000_000
    states = [node_of(fake, run, "orders").state for run in runs]
    assert states == [NodeState.READY] * taken + [NodeState.PENDING] * (16 - taken)
    # Control: one more member than the promoter took is refused by the store, unwritten.
    over, over_runs = padded(16)
    held = len(over.transactions)
    with pytest.raises(ValueError, match="exceeds the 4 MB size bound"):
        arm(over, "orders", over_runs[: taken + 1])
    assert len(over.transactions) == held
    # Control: exactly the promoter's prefix arms; unpadded, all 16 arm in one grant.
    arm(over, "orders", over_runs[:taken])
    assert open_members(over, "orders") == taken
    small = FakeDynamo()
    for index in range(1, 17):
        unit_run(small, index, "orders")
    assert _arm_pass(small).batch_members == 16


def test_f3_every_unread_fixed_shape_item_is_under_its_bound() -> None:
    """`UNREAD_ITEM_BYTES` bounds each item a batch transaction counts without reading it."""
    from adapters.outbound.dynamodb.graph_transaction_bytes import (  # noqa: PLC0415
        UNREAD_ITEM_BYTES,
        item_bytes,
    )
    from ports.outbound.execution_graph_dispatch import ClaimRequest  # noqa: PLC0415

    fake = FakeDynamo()
    runs = tuple(unit_run(fake, index, "orders") for index in range(1, 25))
    arms = [item for key, item in fake.items.items() if key[1].startswith("ARM#")]
    batch = _claimed(fake, "orders", runs)
    item = member(fake, runs[0], "orders")
    fake.items[(f"RUN#{runs[0]}", "RUN")]["state"] = {"S": "FENCING"}
    fenced = store(fake).read_run(runs[0])
    assert fenced is not None
    store(fake).await_provider_terminal(
        ClaimRequest(
            fenced,
            item.node,
            store(fake).read_claim(item.node.node_id),
            "2026-10-07T09:30:00+00:00",
            now_text(),
        )
    )
    claims = [v for k, v in fake.items.items() if k[0].startswith("EXEC_CLAIM#")]
    # Begin later sets table_id and attempt_id on a claim: bound them at 255 characters each.
    begun = [{**c, "table_id": {"S": "t" * 255}, "attempt_id": {"S": "a" * 255}} for c in claims]
    verify = [v for k, v in fake.items.items() if k[1].startswith("VERIFY#")]
    tokens = [v for k, v in fake.items.items() if k[0].startswith("TOKEN#")]
    measured = {
        "lock_24_members": [lock(fake, "orders")],
        "arm": arms,
        "claim": begun,
        "verify": verify,
        "token": tokens,
    }
    assert all(measured.values()), measured.keys()
    assert len(lock(fake, "orders")["members"]["L"]) == 24
    for kind, items in measured.items():
        assert max(item_bytes(cast("Any", found)) for found in items) * 2 < UNREAD_ITEM_BYTES, kind
    del batch


def test_1c_a_reclaim_adds_one_admission_and_a_settlement_bump_adds_none() -> None:
    fake = FakeDynamo()
    run = unit_run(fake, 1, "orders")
    batch = _claimed(fake, "orders", (run,))
    node = node_of(fake, run, "orders")
    first = store(fake).read_claim(node.node_id)
    assert first is not None and first.admission == 1
    store(fake).release_claim_lease(first, now_text())
    claim(fake, run, "orders", batch)
    second = store(fake).read_claim(node.node_id)
    assert second is not None and (second.admission, second.batch_id) == (2, batch)
    # Control: a settlement's lease bump is not an admission.
    store(fake).settle_unit_set(_success(fake, "orders", (run,), batch))
    settled = store(fake).read_claim(node.node_id)
    assert settled is not None and settled.admission == 2
    assert settled.lease_version == second.lease_version + 1


def test_p1_a_returned_rearmed_member_is_readmitted_and_a_plain_reclaim_checks_claimed() -> None:
    fake = FakeDynamo()
    run = unit_run(fake, 1, "orders")
    first_batch = _claimed(fake, "orders", (run,))
    item = member(fake, run, "orders")
    held = store(fake).read_claim(item.node.node_id)
    store(fake).return_unit(
        ReturnUnitRequest(item.run, item.node, ReturnReason.MEMBER_FAILED, now_text(), held)
    )
    second_batch = _claimed(fake, "orders", (run,), armed_at="2026-10-07T09:05:00+00:00")
    assert second_batch != first_batch
    readmitted = store(fake).read_claim(item.node.node_id)
    assert readmitted is not None and (readmitted.admission, readmitted.batch_id) == (
        2,
        second_batch,
    )
    assert fake.items[(f"EXEC_CLAIM#{item.node.node_id}", "CLAIM")]["batch_id"] == {
        "S": second_batch
    }
    assert node_of(fake, run, "orders").state is NodeState.CLAIMED
    # Control: a plain RECLAIM of a CLAIMED node keeps the ConditionCheck on `:claimed`.
    assert readmitted is not None
    store(fake).release_claim_lease(readmitted, now_text())
    before = len(fake.transactions)
    claim(fake, run, "orders", second_batch)
    (transaction,) = fake.transactions[before:]
    node_member = [item for item in transaction if "NODE#" in str(item)]
    assert list(node_member[0]) == ["ConditionCheck"]


# --- Test 7: every settle path releases one counted share (D33, F9) -----------------------------


def test_7_settle_node_refuses_a_write_unit_success_before_any_write() -> None:
    from domain.shared_kernel.errors import (  # noqa: PLC0415
        WRITE_UNIT_SETTLE_PATH_CODE,
        InvariantError,
    )
    from ports.outbound.execution_graph_dispatch import NodeSettlement  # noqa: PLC0415

    fake = FakeDynamo()
    run = unit_run(fake, 1, "orders")
    batch = _claimed(fake, "orders", (run,))
    item = member(fake, run, "orders")
    before = len(fake.transactions)
    with pytest.raises(InvariantError) as refused:
        store(fake).settle_node(
            NodeSettlement(item.run, item.node, NodeState.SUCCEEDED, now_text(), effect_digest="e")
        )
    assert refused.value.code == WRITE_UNIT_SETTLE_PATH_CODE
    assert len(fake.transactions) == before  # no transaction sent
    # Control: the one path settles it, and settle_node still settles materialize_raw.
    store(fake).settle_unit_set(_success(fake, "orders", (run,), batch))
    assert node_of(fake, run, "orders").state is NodeState.SUCCEEDED


def test_7_a_failed_settlement_and_fence_unsent_each_release_one_share() -> None:
    from domain.operations.execution_graph_dispatch import FenceReason  # noqa: PLC0415
    from ports.outbound.execution_graph_dispatch import (  # noqa: PLC0415
        FenceRunRequest,
        MaterializationSettlement,
        NodeSettlement,
    )

    fake = FakeDynamo()
    runs = (unit_run(fake, 1, "orders"), unit_run(fake, 2, "orders"))
    batch = arm(fake, "orders", runs)
    permit_and_send(fake, "orders", batch, runs[:1])  # only the first is sent and claimed
    claim(fake, runs[0], "orders", batch)
    first = member(fake, runs[0], "orders")
    held = store(fake).read_claim(first.node.node_id)
    store(fake).settle_materialization(
        MaterializationSettlement(
            NodeSettlement(
                first.run,
                first.node,
                NodeState.FAILED,
                now_text(),
                claim=held,
                failure_reason="statement_failed",
                failure_code="ADLY-FICTIONAL-0001",
            ),
            fence=FenceRunRequest(first.run, FenceReason.NODE_FAILED, now_text()),
        )
    )
    assert open_members(fake, "orders") == 1
    assert node_item(fake, runs[0], "orders")["failure_code"] == {"S": "ADLY-FICTIONAL-0001"}
    second = member(fake, runs[1], "orders")
    store(fake).fence_run(FenceRunRequest(second.run, FenceReason.CANCELLED, now_text()))
    fenced = member(fake, runs[1], "orders")
    store(fake).fence_unsent(fenced.run, fenced.node)
    assert open_members(fake, "orders") == 0  # the last decrement frees the lock
    assert node_of(fake, runs[1], "orders").state is NodeState.FENCED_UNSENT


def test_7_control_a_materialize_lock_is_still_deleted_by_its_holder() -> None:
    from execution_graph_dispatch_support import materialized_run  # noqa: PLC0415

    fake = FakeDynamo()
    run = materialized_run(fake, "orders")
    materialize = next(n for n in store(fake).read_nodes(run) if n.name == "materialize_raw")
    assert materialize.lock_key.startswith("MATERIALIZE#")
    assert (f"LOCK#{materialize.lock_key}", "LOCK") not in fake.items  # released by delete


# --- Test 2: arming, one grant per table (D30-D33, D41, N6) ----------------------------------


def _arm_pass(fake: FakeDynamo) -> Any:
    from execution_graph_dispatch_support import NOW  # noqa: PLC0415

    from adapters.jobs.execution_graph.fence_settler import due_shards  # noqa: PLC0415
    from adapters.jobs.execution_graph.instantiate import ingestion_node_plan  # noqa: PLC0415
    from adapters.jobs.execution_graph.promoter import PromoterContext, arm_pass  # noqa: PLC0415

    return arm_pass(
        PromoterContext(release_store(fake), ingestion_node_plan, lambda: NOW),
        due_shards(),
        lambda: 60_000,
        "test-request",
    )


def test_2_three_runs_on_one_table_arm_in_one_transaction_oldest_first() -> None:
    fake = FakeDynamo()
    runs = tuple(
        unit_run(fake, index, "orders", clock=f"2026-10-07T0{9 - index}:00:00+00:00")
        for index in (1, 2, 3)
    )
    before = len(fake.transactions)
    counts = _arm_pass(fake)
    assert (counts.batches_armed, counts.batch_members, counts.retired) == (1, 3, 3)
    assert len(fake.transactions) - before == 1
    assert open_members(fake, "orders") == 3
    (row,) = [
        store(fake).read_outbox(k[0], k[1].removeprefix("OUTBOX#"))
        for k in fake.items
        if k[1].startswith("OUTBOX#")
    ]
    assert row is not None and [ref.run_id for ref in row.members] == list(reversed(runs))
    # Control: with the lock held, three lock waits and every member stays PENDING.
    held = FakeDynamo()
    for index in (1, 2, 3):
        unit_run(held, index, "orders")
    held.items[("LOCK#TABLE#orders", "LOCK")] = {
        "pk": {"S": "LOCK#TABLE#orders"},
        "sk": {"S": "LOCK"},
        "holder_batch_id": {"S": "other"},
        "open_members": {"N": "1"},
    }
    waiting = _arm_pass(held)
    assert (waiting.lock_wait, waiting.armed) == (3, 0)


def test_2_the_member_bound_takes_24_of_25() -> None:
    fake = FakeDynamo()
    for index in range(1, 26):
        unit_run(fake, index, "orders")
    counts = _arm_pass(fake)
    assert (counts.armed, counts.batch_members) == (24, 24)
    pending = [
        k
        for k, v in fake.items.items()
        if k[1] == "NODE#write_unit:orders" and v["state"] == {"S": "PENDING"}
    ]
    assert len(pending) == 1
    # Control: 24 candidates are all armed.
    control = FakeDynamo()
    for index in range(1, 25):
        unit_run(control, index, "orders")
    assert _arm_pass(control).armed == 24


def test_2_a_paused_runs_unit_waits_and_an_open_runs_arms() -> None:
    fake = FakeDynamo()
    paused = unit_run(fake, 1, "orders")
    fake.items[(f"RUN#{paused}", "RUN")]["state"] = {"S": "PAUSED"}
    counts = _arm_pass(fake)
    assert (counts.paused_wait, counts.armed) == (1, 0)
    assert node_of(fake, paused, "orders").state is NodeState.PENDING
    fake.items[(f"RUN#{paused}", "RUN")]["state"] = {"S": "OPEN"}
    assert _arm_pass(fake).armed == 1


def test_2_d41_a_solo_head_is_armed_alone_and_a_late_solo_waits() -> None:
    fake = FakeDynamo()
    solo = unit_run(fake, 1, "orders", clock="2026-10-07T01:00:00+00:00")
    fresh = (
        unit_run(fake, 2, "orders", clock="2026-10-07T02:00:00+00:00"),
        unit_run(fake, 3, "orders", clock="2026-10-07T03:00:00+00:00"),
    )
    node_item(fake, solo, "orders")["arm_alone"] = {"BOOL": True}
    counts = _arm_pass(fake)
    assert (counts.batch_members, node_of(fake, solo, "orders").state) == (1, NodeState.READY)
    # Control: [fresh, fresh, solo] grants the two fresh nodes; the solo waits for its turn.
    control = FakeDynamo()
    first = (
        unit_run(control, 1, "orders", clock="2026-10-07T01:00:00+00:00"),
        unit_run(control, 2, "orders", clock="2026-10-07T02:00:00+00:00"),
    )
    late = unit_run(control, 3, "orders", clock="2026-10-07T03:00:00+00:00")
    node_item(control, late, "orders")["arm_alone"] = {"BOOL": True}
    assert _arm_pass(control).batch_members == 2
    assert node_of(control, late, "orders").state is NodeState.PENDING
    assert {node_of(control, run, "orders").state for run in first} == {NodeState.READY}
    del fresh


def test_2_n6_arm_retires_with_the_runs_last_unit_across_groups() -> None:
    fake = FakeDynamo()
    run = unit_run(fake, 1, "orders", "returns")
    fake.items[("LOCK#TABLE#returns", "LOCK")] = {
        "pk": {"S": "LOCK#TABLE#returns"},
        "sk": {"S": "LOCK"},
        "holder_batch_id": {"S": "other"},
        "open_members": {"N": "1"},
    }
    counts = _arm_pass(fake)
    assert (counts.armed, counts.lock_wait, counts.retired) == (1, 1, 0)
    assert [k for k in fake.items if k[1] == f"ARM#{run}"]  # control: ARM# stays
    fake.items[("LOCK#TABLE#returns", "LOCK")]["open_members"] = {"N": "0"}
    after = _arm_pass(fake)
    assert (after.armed, after.retired) == (1, 1)
    assert not [k for k in fake.items if k[1] == f"ARM#{run}"]


# --- Test 12: the dispatcher's batch path (§2.2, F7) -------------------------------------------


def _dispatch(fake: FakeDynamo, sender: Any) -> Any:
    from adapters.jobs.execution_graph.dispatcher import (  # noqa: PLC0415
        DispatchContext,
        dispatch_shard,
    )
    from adapters.jobs.execution_graph.fence_settler import due_shards  # noqa: PLC0415

    totals: list[Any] = []
    for shard in due_shards():
        totals.append(dispatch_shard(DispatchContext(store(fake), sender, now_text), shard))
    return totals


def test_12_a_paused_member_is_returned_and_the_body_lists_only_the_permitted() -> None:
    from execution_graph_dispatch_support import SpySender  # noqa: PLC0415

    fake = FakeDynamo()
    runs = _three_runs(fake)
    arm(fake, "orders", runs)
    fake.items[(f"RUN#{runs[1]}", "RUN")]["state"] = {"S": "PAUSED"}
    sender = SpySender()
    counts = _dispatch(fake, sender)
    ((body, group, _dedup),) = sender.calls
    listed = [m["run_id"] for m in json.loads(body)["members"]]
    assert listed == [runs[0], runs[2]] and group == "orders"
    assert sum(c.returned for c in counts) == 1
    returned = node_of(fake, runs[1], "orders")
    assert (returned.state, returned.return_reason, returned.arm_alone) == (
        NodeState.PENDING,
        "run_paused",
        False,
    )
    assert open_members(fake, "orders") == 2
    # Control: an all-OPEN batch lists every member.
    control = FakeDynamo()
    all_open = _three_runs(control)
    arm(control, "orders", all_open)
    spy = SpySender()
    _dispatch(control, spy)
    assert len(json.loads(spy.calls[0][0])["members"]) == 3


def test_12_a_fencing_member_is_skipped_and_an_empty_row_is_deleted() -> None:
    from execution_graph_dispatch_support import SpySender  # noqa: PLC0415

    fake = FakeDynamo()
    run = unit_run(fake, 1, "orders")
    batch = arm(fake, "orders", (run,))
    fake.items[(f"RUN#{run}", "RUN")]["state"] = {"S": "FENCING"}
    sender = SpySender()
    _dispatch(fake, sender)
    assert sender.calls == []
    assert (batch_row_shard(batch), f"OUTBOX#{batch}") not in fake.items  # the empty row
    assert node_of(fake, run, "orders").state is NodeState.READY  # left for fence_unsent


def test_12_a_sending_row_is_resent_byte_identical_and_drops_a_fenced_member() -> None:
    from execution_graph_dispatch_support import SpySender  # noqa: PLC0415

    fake = FakeDynamo()
    runs = _three_runs(fake)
    batch = arm(fake, "orders", runs)
    row = outbox(fake, batch)
    store(fake).acquire_batch_permit(
        BatchPermitRequest(row, tuple(member(fake, r, "orders") for r in runs), now_text())
    )
    first = SpySender()
    from adapters.outbound.dynamodb.execution_graph_dispatch import (  # noqa: PLC0415
        batch_body,
        batch_dispatch_id,
    )

    sending = outbox(fake, batch)
    expected = batch_body(batch, "TABLE#orders", sending.members)
    # A member is fenced (and settled FENCED_UNSENT is impossible once PERMITTED), so it reads
    # not PERMITTED: simulate its settlement since.
    node_item(fake, runs[2], "orders")["state"] = {"S": "SUCCEEDED"}
    _dispatch(fake, first)
    ((body, _group, dedup),) = first.calls
    assert (body, dedup) == (expected, batch_dispatch_id(batch, expected))
    assert (batch_row_shard(batch), f"OUTBOX#{batch}") not in fake.items  # the row closes
    assert [node_of(fake, r, "orders").state for r in runs[:2]] == [NodeState.DISPATCHED] * 2


def test_12_the_deduplication_id_differs_across_rearms() -> None:
    from adapters.outbound.dynamodb.execution_graph_dispatch import (  # noqa: PLC0415
        batch_body,
        batch_dispatch_id,
    )
    from ports.outbound.execution_graph_dispatch import BatchMemberRef  # noqa: PLC0415

    refs = (BatchMemberRef("r", "n", "d", 0),)
    one = batch_dispatch_id("a" * 64, batch_body("a" * 64, "TABLE#orders", refs))
    two = batch_dispatch_id("b" * 64, batch_body("b" * 64, "TABLE#orders", refs))
    assert one != two
    # Control: the same row's body always gives the same identity.
    assert one == batch_dispatch_id("a" * 64, batch_body("a" * 64, "TABLE#orders", refs))


# --- Test 12b: takeover and verifier settle a member through the one path (F8) ----------------


def test_12b_the_verifier_settles_a_parked_member_through_settle_unit_set() -> None:
    from adapters.jobs.execution_graph.fence_settler import due_shards  # noqa: PLC0415
    from adapters.jobs.execution_graph.node_verifier import (  # noqa: PLC0415
        VerifierContext,
        verify_shard,
    )
    from domain.operations.execution_graph_dispatch import AttemptFact  # noqa: PLC0415
    from ports.outbound.execution_graph_dispatch import ClaimRequest  # noqa: PLC0415

    fake = FakeDynamo()
    run = unit_run(fake, 1, "orders")
    _claimed(fake, "orders", (run,))
    item = member(fake, run, "orders")
    fake.items[(f"RUN#{run}", "RUN")]["state"] = {"S": "FENCING"}
    held = store(fake).read_claim(item.node.node_id)
    fenced_run = store(fake).read_run(run)
    assert fenced_run is not None
    store(fake).await_provider_terminal(
        ClaimRequest(fenced_run, item.node, held, "2026-10-07T09:30:00+00:00", now_text())
    )

    class _Facts:
        def attempt(self, *_args: object) -> tuple[Any, str]:
            return AttemptFact.SUCCEEDED, "f" * 64

    for shard in due_shards():
        verify_shard(VerifierContext(store(fake), cast("Any", _Facts()), now_text), shard)
    settled = node_of(fake, run, "orders")
    assert (settled.state, settled.effect_digest, settled.effect_record) == (
        NodeState.SUCCEEDED,
        "f" * 64,
        None,
    )
    assert open_members(fake, "orders") == 0
    assert not [k for k in fake.items if k[1].startswith("VERIFY#")]
