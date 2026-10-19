# SPDX-License-Identifier: MIT-0
"""Slice 7 (1b) pure batching rules: the member bound, identities, finality and D41's order.

Design §2.2 ("Batch arming") and §2.5 ("Return", "Final attempt"); decisions D32, D35 and D41.
Each refusal or negative case has its paired positive control. Fictional data only.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from domain.operations.execution_graph_dispatch import (
    MAX_BATCH_MEMBERS,
    SETTLE_CHUNK_MEMBERS,
    ClaimOutcome,
    ClaimView,
    NodePhase,
    NodeState,
    ReturnReason,
    batch_id_for,
    decide_claim,
    execution_set_id_for,
    is_write_unit,
    sets_arm_alone,
    sets_final_return,
    take_grant,
    unit_final_attempt,
    unit_may_return,
)
from domain.operations.generated_execution_graphs import BOUNDS
from domain.operations.run_request import RunState

pytestmark = pytest.mark.unit


@dataclass(frozen=True, slots=True)
class _Candidate:
    node_id: str
    run_id: str
    root_clock_started_at: str | None
    arm_alone: bool = False


def _fresh(index: int, *, alone: bool = False) -> _Candidate:
    return _Candidate(
        node_id=f"node-{index:02d}",
        run_id=f"run-{index:02d}",
        root_clock_started_at=f"2026-10-10T00:{index:02d}:00+00:00",
        arm_alone=alone,
    )


def test_the_member_bound_is_derived_from_the_contract() -> None:
    assert BOUNDS["max_transaction_items"] == 100
    assert MAX_BATCH_MEMBERS == 24 == (100 - 2) // 4
    assert SETTLE_CHUNK_MEMBERS == 33 == (100 - 1) // 3


def test_write_unit_names_are_the_dynamic_group_only() -> None:
    assert is_write_unit("write_unit:orders")
    for name in ("write_unit", "write_unit:", "write_a", "publish", "materialize_raw"):
        assert not is_write_unit(name)


def test_batch_id_differs_across_grants_and_repeats_for_the_same_inputs() -> None:
    first = batch_id_for("TABLE#orders", "2026-10-10T00:00:00+00:00", ("n1", "n2"))
    assert first == batch_id_for("TABLE#orders", "2026-10-10T00:00:00+00:00", ("n1", "n2"))
    assert first != batch_id_for("TABLE#orders", "2026-10-10T00:00:01+00:00", ("n1", "n2"))
    assert first != batch_id_for("TABLE#orders", "2026-10-10T00:00:00+00:00", ("n1",))
    assert execution_set_id_for(first, ("n1",)) == execution_set_id_for(first, ("n1",))
    assert execution_set_id_for(first, ("n1",)) != execution_set_id_for(first, ("n1", "n2"))


def test_finality_counts_admission_and_final_return() -> None:
    # Test 9: a returned member at admission 3 is final on a fresh message; final_return is
    # final at admission 1.
    assert unit_final_attempt(1, 3, final_return=False)
    assert unit_final_attempt(1, 1, final_return=True)
    # Control: receive 3 with admission 2 (a LIVE_DUPLICATE receive) is final, as built, and the
    # member may still be returned once.
    assert unit_final_attempt(3, 2, final_return=False)
    assert unit_may_return(final_return=False)
    # Control: attempts remain below the cap on both counts.
    assert not unit_final_attempt(2, 2, final_return=False)
    assert not unit_may_return(final_return=True)


def test_return_reasons_set_arm_alone_and_final_return_by_their_closed_rule() -> None:
    assert [reason.value for reason in ReturnReason] == [
        "member_failed",
        "final_unisolated",
        "run_paused",
        "generation_moved",
    ]
    assert {reason for reason in ReturnReason if sets_arm_alone(reason)} == {
        ReturnReason.MEMBER_FAILED,
        ReturnReason.FINAL_UNISOLATED,
    }
    assert {reason for reason in ReturnReason if sets_final_return(reason)} == {
        ReturnReason.FINAL_UNISOLATED
    }


def test_d41_a_solo_head_is_granted_alone() -> None:
    solo, fresh_a, fresh_b = _fresh(0, alone=True), _fresh(1), _fresh(2)
    assert take_grant([fresh_b, solo, fresh_a], MAX_BATCH_MEMBERS) == (solo,)
    # Control: a solo node behind two fresh ones waits; the fresh prefix is granted.
    late_solo = _fresh(3, alone=True)
    assert take_grant([late_solo, fresh_b, fresh_a], MAX_BATCH_MEMBERS) == (fresh_a, fresh_b)


def test_d41_the_grant_takes_at_most_the_bound_oldest_first() -> None:
    group = [_fresh(index) for index in range(25)]
    taken = take_grant(list(reversed(group)), MAX_BATCH_MEMBERS)
    assert taken == tuple(group[:24])
    # Control: 24 candidates are all taken.
    assert take_grant(group[:24], MAX_BATCH_MEMBERS) == tuple(group[:24])
    assert take_grant([], MAX_BATCH_MEMBERS) == ()
    with pytest.raises(ValueError, match="at least one member"):
        take_grant(group, 0)


def test_d41_ties_on_the_root_clock_break_by_run_id() -> None:
    a = _Candidate("na", "run-a", "2026-10-10T00:00:00+00:00")
    b = _Candidate("nb", "run-b", "2026-10-10T00:00:00+00:00")
    assert take_grant([b, a], 2) == (a, b)


def _view(*, batch_matches: bool) -> ClaimView:
    return ClaimView(
        run_state=RunState.OPEN,
        phase=NodePhase.PRE_GRANT,
        node_state=NodeState.DISPATCHED,
        binding_matches=True,
        claim_present=False,
        lease_expired=False,
        batch_matches=batch_matches,
    )


def test_a_batch_id_mismatch_is_a_binding_conflict_and_a_match_claims() -> None:
    assert decide_claim(_view(batch_matches=False)) is ClaimOutcome.BINDING_CONFLICT
    assert decide_claim(_view(batch_matches=True)) is ClaimOutcome.CLAIM
