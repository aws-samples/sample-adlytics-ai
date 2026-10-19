# SPDX-License-Identifier: MIT-0
"""FEAT-011: gate 2 ("reservations fully accounted for") and the failed-unit variant.

The table is the transactional `FakeDynamo` of the TD-50 suite, which evaluates the command's
own condition and update expressions. Every identity is fictional; the STOP-1 fences (77/81,
83/90, 79/101) and counts (10 + 10 + 6 = 26) are mirrored only as numbers.
"""

from __future__ import annotations

# pyright: reportPrivateUsage=false
import copy
import hashlib
import itertools
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import test_projection_coordinator_fence as fence
from botocore.exceptions import ClientError
from test_supersede_orphan_units import (
    AT,
    TABLE,
    TARGETS,
    _by_plan,
    _drain,
    _hex,
    _low,
    _Plan,
    _queued,
    _Table,
    _World,
)

from adapters.jobs.supersede_orphan_units import __main__ as supersede
from adapters.jobs.supersede_orphan_units.evidence import (
    PlanRows,
    PlanShape,
    Unproven,
    orphan_plan,
)
from domain.operations.write_plan import META_RECORD_KEY, intent_partition_key, unit_record_key
from ports.outbound.projection_coordinator import BeginQueuedUnitOwner, QueuedUnitAttemptOwner

pytestmark = [pytest.mark.unit]

RESERVED = _Plan("7")
FAILED = _Plan("6")
ORPHAN_UNIT = "m_current.fct_alpha"
SIBLING_UNIT = "m_current.fct_sibling"
FAILED_UNIT = "m_current.fct_failed"
CANDIDATE_SIBLING = "a_candidate.fct_failed"
SOURCE_SIBLING = "z_source.sa_failed"


def _set(world: _World, partition: str, record: str, **attributes: object) -> None:
    item = world.item(partition, record)
    for name, value in attributes.items():
        item[name] = _low(value)


def _attested(table_kind: str, count: int | None) -> dict[str, object]:
    unit: dict[str, object] = {**_queued(table_kind, []), "status": "attested"}
    if count is not None:
        unit |= {"unit_reservation_count": count, "attempt_references": [_hex("e")] * count}
    return unit


def _reserved_world(*, sibling_count: int = 2, plan_count: int = 2) -> _World:
    """Build a class (a) plan: one attested sibling spent every reservation, one orphan."""
    world = _World()
    world.current("partition-alpha", "publication-newer", 9)
    world.plan(
        RESERVED,
        {
            ORPHAN_UNIT: _queued(
                "current", [world.canonical_object("partition-alpha", "publication-r", 4)]
            ),
            SIBLING_UNIT: _attested("current", sibling_count),
        },
    )
    _set(world, RESERVED.key, META_RECORD_KEY, plan_reservation_count=plan_count)
    return world


def _member(members: list[dict[str, Any]], kind: str, record: str) -> dict[str, Any]:
    (found,) = [
        member[kind]
        for member in members
        if kind in member and member[kind]["Key"]["record_key"]["S"] == record
    ]
    return found


# --- Gate 2: the sum identity over every spent-reservation shape ----------------------------------

_THIRD: dict[str, dict[str, object]] = {
    "never_begun": {},
    "queued_begun": {"unit_reservation_count": 1, "attempt_references": [_hex("a")]},
    "writing_bound": {"status": "writing", "unit_reservation_count": 1, "current_attempt_id": "b"},
    "failed_unaccounted": {"status": "failed", "unit_reservation_count": 1},
}


def test_gate_two_admits_exactly_the_plans_whose_attested_siblings_spent_every_reservation() -> (
    None
):
    """Every reservation placement x every META count: admitted only when the sum is exact.

    Begin adds one to a unit's and the plan's counter together, so an invariant-respecting plan
    has META == the sum over all its units. The enumeration also covers torn and impossible
    states (META above, below, zero, absent, over the cap): none of them is admitted, and a
    reservation on any unit that is not attested refuses the plan whatever the META says.
    """
    admitted_shapes: set[tuple[object, ...]] = set()
    for a, b, third, n in itertools.product(
        (None, 1, 2, 10, 11), (None, 1, 3), _THIRD, (None, 0, *range(1, 16), 33)
    ):
        other = {**_queued("current", []), **_THIRD[third]}
        units = {
            "a_attested.one": _attested("current", a),
            "b_attested.two": _attested("current", b),
            "c_queued.three": other,
        }
        meta: dict[str, object] = {
            "status": "planned",
            "unit_count": 3,
            "admitted_count": 3,
            "attested_count": 2,
            "failed_count": 0,
        }
        if n is not None:
            meta["plan_reservation_count"] = n
        admitted, reason = orphan_plan(PlanRows("plan", meta, units, {"state": "admitted"}))
        spent = (a or 0) + (b or 0)
        # Begin writes the counter on the first reservation, so a present 0 is never valid, and
        # under gate 2 an attested sibling with no counter is torn (it passed Begin), not zero;
        # Begin refuses at the permanent per-unit cap of 10, so 11 is never written by it.
        counted = a is not None and b is not None and a <= 10
        expected = third == "never_begun" and (
            spent == 0 if n is None else counted and 1 <= n == spent
        )
        assert (admitted is not None) is expected, (a, b, third, n, reason)
        if admitted is not None:
            admitted_shapes.add((a, b, n))
            assert admitted.orphans == (other,)
            assert admitted.reservations == n
            assert admitted.shape is (
                PlanShape.NO_RESERVATION if n is None else PlanShape.RESERVATIONS_ACCOUNTED_FOR
            )
        elif third == "never_begun":
            assert reason is Unproven.RESERVATIONS_UNACCOUNTED
        else:
            assert reason in {
                Unproven.UNIT_SHAPE,
                Unproven.PLAN_SHAPE,
                Unproven.RESERVATIONS_UNACCOUNTED,
            }
    # Gate 1 (no counters at all) and every exact, fully counted gate-2 sum were admitted.
    assert (None, None, None) in admitted_shapes
    assert {(1, 1, 2), (1, 3, 4), (2, 3, 5), (10, 1, 11), (10, 3, 13)} <= admitted_shapes
    # An absent attested count is never read as zero to make the sum match.
    assert not {(2, None, 2), (10, None, 10), (None, 1, 1), (None, 3, 3)} & admitted_shapes
    # B4: an exact sum over a count above the per-unit cap is never admitted.
    assert not {(11, 1, 12), (11, 3, 14)} & admitted_shapes


def _capped_rows(role: str, count: int) -> PlanRows:
    """One spent unit carrying `count` (attested sibling, or the accounted failed unit)."""
    meta: dict[str, object] = {
        "status": "planned",
        "unit_count": 2,
        "admitted_count": 2,
        "attested_count": 1,
        "failed_count": 0,
        "plan_reservation_count": count,
    }
    if role == "attested":
        units = {
            "a_attested.one": _attested("current", count),
            "b_queued.two": _queued("current", []),
        }
    else:
        failed = {
            **_queued("current", []),
            "status": "failed",
            "unit_reservation_count": count,
            "attempt_references": [_hex("f")] * count,
        }
        units = {"a_failed.one": failed, "b_attested.two": _attested("current", 1)}
        meta |= {"failed_count": 1, "plan_reservation_count": count + 1}
    return PlanRows("plan", meta, units, {"state": "admitted"})


@pytest.mark.parametrize("role", ["attested", "failed"])
@pytest.mark.parametrize(("count", "admitted"), [(10, True), (11, False)])
def test_a_spent_unit_count_above_the_per_unit_cap_is_never_accounted_for(
    role: str, count: int, admitted: bool
) -> None:
    """B4: Begin refuses at 10, so 11 is refused even with an exact META; 10 is the control."""
    rows = _capped_rows(role, count)
    plan, reason = orphan_plan(rows, failed_unit_accounted=role == "failed")
    assert (plan is not None) is admitted, (role, count, reason)
    if plan is not None:
        assert plan.reservations == rows.meta["plan_reservation_count"]
        assert plan.shape is (
            PlanShape.RESERVATIONS_ACCOUNTED_FOR
            if role == "attested"
            else PlanShape.FAILED_UNIT_PROJECTED_PAST
        )
    else:
        assert reason is Unproven.RESERVATIONS_UNACCOUNTED


@pytest.mark.parametrize(("count", "decision"), [(10, "supersede"), (11, "left_untouched")])
def test_a_gate_two_plan_with_an_above_cap_sibling_is_left_untouched(
    count: int, decision: str
) -> None:
    """B4 end to end: count 11 (META 11) writes nothing; count 10 (META 10) is the control."""
    world = _reserved_world(sibling_count=count, plan_count=count)
    document, commit = supersede.disposition(world.clients(), TARGETS, apply=True, now=AT)
    plan = _by_plan(document)[RESERVED.key]
    assert plan["decision"] == decision
    assert commit is not None
    outcome = commit()
    if decision == "supersede":
        assert outcome["outcomes"] == {RESERVED.key: "superseded"}
        (members,) = world.table.transactions
        sibling = _member(members, "ConditionCheck", unit_record_key(SIBLING_UNIT))
        assert sibling["ExpressionAttributeValues"][":count"] == {"N": "10"}
    else:
        assert plan["unproven"] == "plan_reservations_not_accounted_for"
        assert outcome["outcomes"] == {}
        assert world.table.transactions == []
        assert world.item(RESERVED.key, META_RECORD_KEY)["status"] == {"S": "planned"}


def test_the_spent_check_refuses_to_pin_an_above_cap_count() -> None:
    """B4: the condition builder never pins 11; 10 is the paired control."""
    pinned: Any = supersede._spent_check(
        TABLE, "plan", "unit", _attested("current", 10), supersede.PlanUnitStatus.ATTESTED
    )
    assert pinned["ConditionCheck"]["ExpressionAttributeValues"][":count"] == {"N": "10"}
    with pytest.raises(ValueError, match="from 1 to the unit cap"):
        supersede._spent_check(
            TABLE, "plan", "unit", _attested("current", 11), supersede.PlanUnitStatus.ATTESTED
        )


@pytest.mark.parametrize(
    ("count", "admitted"),
    [("absent", False), (0, False), (-1, False), (True, False), ("2", False), (2, True)],
)
def test_gate_two_needs_a_present_positive_count_on_every_attested_sibling(
    count: object, admitted: bool
) -> None:
    """B1: a gate-2 sibling's count must be a plain positive integer; 2 is the paired control."""
    sibling: dict[str, object] = {**_queued("current", []), "status": "attested"}
    if count != "absent":
        sibling["unit_reservation_count"] = count
    units = {"a_attested.one": sibling, "b_queued.two": _queued("current", [])}
    meta: dict[str, object] = {
        "status": "planned",
        "unit_count": 2,
        "admitted_count": 2,
        "attested_count": 1,
        "failed_count": 0,
        "plan_reservation_count": 2,
    }
    plan, reason = orphan_plan(PlanRows("plan", meta, units, {"state": "admitted"}))
    assert (plan is not None) is admitted, count
    if not admitted:
        assert reason is Unproven.RESERVATIONS_UNACCOUNTED


def test_a_gate_two_plan_with_an_uncounted_attested_sibling_is_left_untouched() -> None:
    world = _reserved_world()
    del world.item(RESERVED.key, unit_record_key(SIBLING_UNIT))["unit_reservation_count"]
    document, commit = supersede.disposition(world.clients(), TARGETS, apply=True, now=AT)
    plan = _by_plan(document)[RESERVED.key]
    assert (plan["decision"], plan["unproven"]) == (
        "left_untouched",
        "plan_reservations_not_accounted_for",
    )
    assert commit is not None
    assert commit()["outcomes"] == {}
    assert world.table.transactions == []


def _drop_intent(world: _World, plan: _Plan) -> None:
    del world.table.items[intent_partition_key(plan.digest, plan.intent), META_RECORD_KEY]


def test_gate_two_and_the_variant_refuse_an_absent_intent_while_gate_one_keeps_its_rule() -> None:
    """B2: only gate 1 may supersede with no origin intent; the new shapes need it admitted."""
    meta: dict[str, object] = {
        "status": "planned",
        "unit_count": 2,
        "admitted_count": 2,
        "attested_count": 1,
        "failed_count": 0,
    }
    units = {"a_attested.one": _attested("current", 2), "b_queued.two": _queued("current", [])}
    gate_two = PlanRows("plan", {**meta, "plan_reservation_count": 2}, units, None)
    assert orphan_plan(gate_two) == (None, Unproven.INTENT_ABSENT)
    admitted, _ = orphan_plan(replace(gate_two, intent={"state": "admitted"}))
    assert admitted is not None
    assert admitted.shape is PlanShape.RESERVATIONS_ACCOUNTED_FOR
    gate_one = PlanRows("plan", meta, {**units, "a_attested.one": _attested("current", None)}, None)
    admitted, reason = orphan_plan(gate_one)
    assert reason is None
    assert admitted is not None
    assert admitted.shape is PlanShape.NO_RESERVATION

    world = _reserved_world()
    _drop_intent(world, RESERVED)
    document, commit = supersede.disposition(world.clients(), TARGETS, apply=True, now=AT)
    plan = _by_plan(document)[RESERVED.key]
    assert (plan["decision"], plan["unproven"]) == ("left_untouched", "origin_intent_absent")
    assert commit is not None
    assert commit()["outcomes"] == {}
    assert world.table.transactions == []

    failed_world, accounting = _failed_world()
    _drop_intent(failed_world, FAILED)
    document, _ = _failed_run(failed_world, accounting)
    plan = _by_plan(document)[FAILED.key]
    assert (plan["decision"], plan["unproven"]) == ("left_untouched", "origin_intent_absent")
    assert document["failed_unit_accounting"]["plan_proven"] is False


def test_an_intent_removed_after_the_read_refuses_a_gate_two_plan() -> None:
    """B2 at commit: the transaction pins a present admitted intent, never its absence."""
    world = _reserved_world()
    _, commit = supersede.disposition(world.clients(), TARGETS, apply=True, now=AT)
    assert commit is not None

    def race(_table: Any) -> None:
        _drop_intent(world, RESERVED)

    world.table.before_transaction = race
    assert commit()["outcomes"] == {RESERVED.key: "refused_state_moved"}
    assert world.table.transactions == []
    assert world.item(RESERVED.key, META_RECORD_KEY)["status"] == {"S": "planned"}


def test_apply_supersedes_a_gate_two_plan_with_its_counter_and_siblings_pinned() -> None:
    world = _reserved_world()
    sibling_before = copy.deepcopy(world.item(RESERVED.key, unit_record_key(SIBLING_UNIT)))
    document, commit = supersede.disposition(world.clients(), TARGETS, apply=True, now=AT)
    plan = _by_plan(document)[RESERVED.key]
    assert (plan["decision"], plan["plan_shape"]) == ("supersede", "reservations_accounted_for")
    assert plan["plan_evidence"]["reservations"] == {
        "plan_reservation_count": 2,
        "unit_reservation_counts": {SIBLING_UNIT: 2},
    }
    assert commit is not None
    outcome = commit()
    assert (outcome["outcomes"], outcome["complete"]) == ({RESERVED.key: "superseded"}, True)
    (members,) = world.table.transactions
    meta = _member(members, "Update", META_RECORD_KEY)
    assert "plan_reservation_count = :reservations" in meta["ConditionExpression"]
    assert "attribute_not_exists(plan_reservation_count)" not in meta["ConditionExpression"]
    assert meta["ExpressionAttributeValues"][":reservations"] == {"N": "2"}
    sibling = _member(members, "ConditionCheck", unit_record_key(SIBLING_UNIT))
    assert sibling["ConditionExpression"] == "#status = :status AND unit_reservation_count = :count"
    assert sibling["ExpressionAttributeValues"][":count"] == {"N": "2"}
    stored = world.item(RESERVED.key, META_RECORD_KEY)
    assert stored["status"] == {"S": "superseded"}
    assert stored["supersession_evidence_digest"] == {"S": plan["plan_evidence_digest"]}
    assert stored["plan_reservation_count"] == {"N": "2"}
    assert world.item(RESERVED.key, unit_record_key(ORPHAN_UNIT))["status"] == {"S": "superseded"}
    assert world.item(RESERVED.key, unit_record_key(SIBLING_UNIT)) == sibling_before
    evidence = _drain(world)
    assert not evidence.nonterminal_plans
    assert not evidence.nonterminal_units


@pytest.mark.parametrize(
    ("moved", "record", "attributes"),
    [
        ("a Begin reserved after the read", META_RECORD_KEY, {"plan_reservation_count": 3}),
        ("the sibling's count moved", unit_record_key(SIBLING_UNIT), {"unit_reservation_count": 3}),
        ("the orphan was claimed", unit_record_key(ORPHAN_UNIT), {"status": "writing"}),
    ],
)
def test_a_gate_two_plan_whose_reservations_moved_is_refused_untouched(
    moved: str, record: str, attributes: dict[str, object]
) -> None:
    world = _reserved_world()
    _, commit = supersede.disposition(world.clients(), TARGETS, apply=True, now=AT)
    assert commit is not None

    def race(_table: Any) -> None:
        _set(world, RESERVED.key, record, **attributes)

    world.table.before_transaction = race
    outcome = commit()
    assert outcome["outcomes"] == {RESERVED.key: "refused_state_moved"}, moved
    assert world.table.transactions == []
    assert world.item(RESERVED.key, META_RECORD_KEY)["status"] == {"S": "planned"}


def test_a_superseded_meta_refuses_the_real_begin_queued_unit_reservation() -> None:
    """(i): BeginQueuedUnit built from a read taken before the disposition cannot reserve.

    The coordinator's own Begin transaction is built from a stale `planned` snapshot (the worst
    race) and its plan `META` member is replayed against this table. The control shows the same
    member takes the reservation against the state read; after the disposition it is refused.
    """
    snapshot = fence._meta_item(plan_reservation_count=2)
    owner = BeginQueuedUnitOwner(
        QueuedUnitAttemptOwner(fence.PLAN_KEY, fence.TABLE_ID, fence._unit_item()), snapshot
    )
    recording = fence._RecordingDynamo()
    fence._coordinator(recording).begin_table_attempt(
        fence._begin_inline_request(owner=owner, writer_execution_id=fence.WRITER_EXEC)
    )
    begin = copy.deepcopy(recording.transacts[-1]["TransactItems"][4]["Update"])
    assert begin["UpdateExpression"] == "SET plan_reservation_count = :new_plan_count"
    assert begin["ConditionExpression"].startswith("#status = :planned AND ")
    begin |= {
        "TableName": TABLE,
        "Key": {"partition_key": {"S": RESERVED.key}, "record_key": {"S": META_RECORD_KEY}},
    }
    world = _reserved_world()
    control = _Table(key_names=("partition_key", "record_key"))
    control.items = copy.deepcopy(world.table.items)
    control.transact_write_items(TransactItems=[{"Update": copy.deepcopy(begin)}])
    assert control.items[RESERVED.key, META_RECORD_KEY]["plan_reservation_count"] == {"N": "3"}

    _, commit = supersede.disposition(world.clients(), TARGETS, apply=True, now=AT)
    assert commit is not None
    assert commit()["outcomes"] == {RESERVED.key: "superseded"}
    with pytest.raises(ClientError) as refused:
        world.table.transact_write_items(TransactItems=[{"Update": begin}])
    assert refused.value.response.get("CancellationReasons") == [{"Code": "ConditionalCheckFailed"}]
    meta = world.item(RESERVED.key, META_RECORD_KEY)
    assert (meta["status"], meta["plan_reservation_count"]) == ({"S": "superseded"}, {"N": "2"})


# --- The failed-unit variant (class b) ------------------------------------------------------------

_FENCES = {"2026-08-20": (83, 90), "2026-08-21": (79, 101), "2026-08-22": (77, 81)}


def _failed_world(*, behind: bool = False) -> tuple[_World, supersede.FailedUnitAccounting]:
    """Build a class (b) plan: attested siblings (10 + 6), one failed unit (10), META 26."""
    world = _World()
    objects: list[list[object]] = []
    for day, (token, projected) in _FENCES.items():
        partition = f"partition-{day}"
        world.current(partition, f"publication-current-{day}", token - 1 if behind else projected)
        objects.append(world.canonical_object(partition, f"publication-failed-{day}", token))
    failed = {
        **_queued("current", objects),
        "status": "failed",
        "unit_reservation_count": 10,
        "attempt_references": [_hex("f")] * 10,
    }
    world.plan(
        FAILED,
        {
            CANDIDATE_SIBLING: _attested("candidate", 10),
            FAILED_UNIT: failed,
            SOURCE_SIBLING: _attested("source_aligned", 6),
        },
    )
    _set(world, FAILED.key, META_RECORD_KEY, failed_count=1, plan_reservation_count=26)
    accounting = supersede.FailedUnitAccounting(
        FAILED.key,
        FAILED_UNIT,
        {"archive": "stop-1-dlq-message-fictional.json", "message_id": "message-fictional"},
    )
    return world, accounting


def _failed_run(
    world: _World, accounting: supersede.FailedUnitAccounting | None
) -> tuple[Any, Any]:
    return supersede.disposition(
        world.clients(), TARGETS, apply=True, now=AT, failed_unit=accounting
    )


def test_a_failed_plan_is_left_untouched_unless_its_dead_letter_is_accounted_for() -> None:
    world, _ = _failed_world()
    document, commit = _failed_run(world, None)
    plan = _by_plan(document)[FAILED.key]
    assert (plan["decision"], plan["unproven"]) == ("left_untouched", "plan_counters_not_orphan")
    assert commit is not None
    assert commit()["outcomes"] == {}
    assert world.table.transactions == []


def test_the_failed_variant_supersedes_only_the_meta_and_records_the_failed_units_proof() -> None:
    world, accounting = _failed_world()
    failed_before = copy.deepcopy(world.item(FAILED.key, unit_record_key(FAILED_UNIT)))
    document, commit = _failed_run(world, accounting)
    plan = _by_plan(document)[FAILED.key]
    assert (plan["decision"], plan["plan_shape"]) == ("supersede", "failed_unit_projected_past")
    assert plan["units"] == []
    proof = plan["failed_unit"]["evidence"]
    assert proof["rule"] == "projection_at_or_past"
    assert sorted(
        (item["fencing_token"], item["projected_fencing_token"]) for item in proof["objects"]
    ) == [(77, 81), (79, 101), (83, 90)]
    record = plan["plan_evidence"]
    assert record["failed_unit"] == {
        "unit_key": FAILED_UNIT,
        "unit_reservation_count": 10,
        "evidence_digest": plan["failed_unit"]["evidence_digest"],
    }
    assert record["dead_letter_accounting"] == dict(accounting.record)
    assert record["reservations"] == {
        "plan_reservation_count": 26,
        "unit_reservation_counts": {CANDIDATE_SIBLING: 10, FAILED_UNIT: 10, SOURCE_SIBLING: 6},
    }
    assert document["failed_unit_accounting"]["plan_proven"] is True
    assert document["complete"] is True
    assert commit is not None
    assert commit()["outcomes"] == {FAILED.key: "superseded"}
    (members,) = world.table.transactions
    # No unit is written: the META is the plan's only Update besides the owner.
    updated = {
        member["Update"]["Key"]["record_key"]["S"] for member in members if "Update" in member
    }
    assert updated == {META_RECORD_KEY, "SUCCESSOR_OWNER"}
    meta = _member(members, "Update", META_RECORD_KEY)
    assert "failed_count = :failed" in meta["ConditionExpression"]
    values = meta["ExpressionAttributeValues"]
    assert (values[":failed"], values[":reservations"]) == ({"N": "1"}, {"N": "26"})
    check = _member(members, "ConditionCheck", unit_record_key(FAILED_UNIT))
    assert check["ConditionExpression"] == (
        "#status = :status AND unit_reservation_count = :count AND admission = :admitted "
        "AND attribute_not_exists(current_attempt_id)"
    )
    assert check["ExpressionAttributeValues"][":status"] == {"S": "failed"}
    assert world.item(FAILED.key, unit_record_key(FAILED_UNIT)) == failed_before
    stored = world.item(FAILED.key, META_RECORD_KEY)
    assert stored["status"] == {"S": "superseded"}
    assert stored["supersession_evidence_digest"] == {"S": plan["plan_evidence_digest"]}
    evidence = _drain(world)
    assert not evidence.nonterminal_plans
    assert not evidence.nonterminal_units


def test_an_accounting_that_names_another_unit_admits_nothing() -> None:
    world, accounting = _failed_world()
    other = supersede.FailedUnitAccounting(FAILED.key, CANDIDATE_SIBLING, accounting.record)
    document, commit = _failed_run(world, other)
    plan = _by_plan(document)[FAILED.key]
    assert plan["unproven"] == "failed_unit_dead_letter_not_accounted_for"
    assert document["failed_unit_accounting"]["plan_proven"] is False
    assert document["complete"] is False
    assert commit is not None
    assert commit()["outcomes"] == {}


def test_a_failed_unit_not_projected_past_leaves_its_plan_untouched() -> None:
    world, accounting = _failed_world(behind=True)
    document, _ = _failed_run(world, accounting)
    plan = _by_plan(document)[FAILED.key]
    assert plan["decision"] == "left_untouched"
    assert plan["failed_unit"]["unproven"] == "projection_behind_publication"
    assert document["failed_unit_accounting"]["plan_proven"] is False


def test_a_redrive_of_the_failed_unit_after_the_read_refuses_the_variant() -> None:
    world, accounting = _failed_world()
    _, commit = _failed_run(world, accounting)
    assert commit is not None

    def redrive(_table: Any) -> None:
        # `ClaimUnit` with `redrive`: the failed unit goes to `writing`, failed_count to 0.
        _set(world, FAILED.key, unit_record_key(FAILED_UNIT), status="writing")
        _set(world, FAILED.key, META_RECORD_KEY, failed_count=0)

    world.table.before_transaction = redrive
    assert commit()["outcomes"] == {FAILED.key: "refused_state_moved"}
    assert world.item(FAILED.key, META_RECORD_KEY)["status"] == {"S": "planned"}


# --- The dead-letter accounting and its flags -----------------------------------------------------


def _archive(tmp_path: Path, plan_key: str) -> tuple[Path, Path]:
    body = json.dumps({"plan_key": plan_key, "unit_key": FAILED_UNIT, "schema_version": "1"})
    archive = tmp_path / "stop-1-dlq-message.json"
    archive.write_text(
        json.dumps(
            {
                "queue": "adlytics-test-semantic-write-dlq.fifo",
                "message_id": "message-fictional-0001",
                "body": body,
            }
        ),
        encoding="utf-8",
    )
    deletion = tmp_path / "baseline.md"
    deletion.write_text(
        "Deleted MessageId `message-fictional-0001` (approved).\n", encoding="utf-8"
    )
    return archive, deletion


def test_the_accounting_must_name_the_plan_its_unit_and_the_deleted_message(
    tmp_path: Path,
) -> None:
    archive, deletion = _archive(tmp_path, FAILED.key)
    accounting = supersede.failed_unit_accounting(FAILED.key, archive, deletion)
    assert (accounting.plan_key, accounting.unit_key) == (FAILED.key, FAILED_UNIT)
    assert accounting.record["archive_sha256"] == hashlib.sha256(archive.read_bytes()).hexdigest()
    assert accounting.record["message_id"] == "message-fictional-0001"
    with pytest.raises(ValueError, match="another plan"):
        supersede.failed_unit_accounting(RESERVED.key, archive, deletion)
    deletion.write_text("no message named here\n", encoding="utf-8")
    with pytest.raises(ValueError, match="message id"):
        supersede.failed_unit_accounting(FAILED.key, archive, deletion)


def _argv(tmp_path: Path, *extra: str) -> list[str]:
    return [
        "--profile",
        "operator",
        "--env",
        "test",
        "--region",
        "us-east-1",
        "--expected-account",
        "000000000000",
        "--evidence-dir",
        str(tmp_path / "evidence"),
        *extra,
    ]


def test_the_variant_flags_go_together_and_an_unaccounted_dead_letter_stops_first(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit):
        supersede.main(_argv(tmp_path, "--failed-unit-plan", FAILED.key))
    archive, deletion = _archive(tmp_path, RESERVED.key)

    def no_session(**_kwargs: object) -> object:
        pytest.fail("the dead-letter accounting is checked before any session exists")

    code = supersede.main(
        _argv(
            tmp_path,
            "--failed-unit-plan",
            FAILED.key,
            "--dlq-archive",
            str(archive),
            "--dlq-deletion-record",
            str(deletion),
        ),
        session_factory=no_session,
    )
    assert code == 1
    assert json.loads(capsys.readouterr().out) == {
        "stage": "dead_letter_accounting",
        "error_class": "ValueError",
    }
