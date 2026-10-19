# SPDX-License-Identifier: MIT-0
"""S3 instantiation under the `INSTANTIATE#` obligation (V-26, BF-6, FR-4).

A crash anywhere between admission and the last node leaves a durable obligation the sweep
completes; a run fenced while it instantiates settles every put node and deletes the
obligation; a pause continues under the new generation. Fictional data only.
"""

from __future__ import annotations

import copy
import json
from datetime import timedelta
from typing import Any, cast

import pytest
from execution_graph_dispatch_support import (
    NOW,
    ROOT_CLOCK,
    SHARD,
    SpySender,
    instantiate,
    kinds,
    materialized_run,
    node,
    now_text,
    planner,
    seed_run,
    set_mode,
    specs,
    store,
    unit_specs,
    write_text,
)
from execution_graph_release_support import release_store
from execution_graph_support import FakeDynamo

from adapters.jobs.execution_graph.commands import (
    OperatorCommand,
    RunCommandKind,
    run_operator_command,
)
from adapters.jobs.execution_graph.dispatcher import DispatchContext, dispatch_shard
from adapters.jobs.execution_graph.fence_settler import try_terminalize
from adapters.jobs.execution_graph.instantiate import (
    InstantiateRequest,
    InstantiationOutcome,
    ingestion_node_plan,
    instantiate_run,
)
from adapters.jobs.execution_graph.promoter import PromoterContext, promote_shard
from domain.operations.execution_graph import AuthorityMode, GraphCommandRefusedError
from domain.operations.execution_graph_dispatch import (
    PROVIDER_CEILING_BYTES,
    RUN_STATE_CHANGED,
    ArmScope,
    FenceReason,
    GraphGuardError,
    NodePhase,
    NodeState,
)
from domain.operations.run_request import RunState
from ports.outbound.execution_graph_dispatch import (
    FenceRunRequest,
    NodeSpec,
    RunRecord,
    SuccessorRequest,
)

pytestmark = pytest.mark.unit

OPERATOR = "arn:aws:iam::000000000000:user/operator"


def _seeded(fake: FakeDynamo) -> str:
    set_mode(fake, AuthorityMode.GRAPH)
    return seed_run(fake).record.run_id


def _fence(fake: FakeDynamo, run: str, reason: FenceReason) -> None:
    current = store(fake).read_run(run)
    assert current is not None
    store(fake).fence_run(FenceRunRequest(current, reason, now_text()))


def test_v26_a_normal_admission_instantiates_inline_and_deletes_the_obligation_once() -> None:
    fake = FakeDynamo()
    run = _seeded(fake)
    assert instantiate(fake, run, "write_a") is InstantiationOutcome.ARMED
    assert kinds(fake, "INSTANTIATE#") == []
    assert node(fake, run, "write_a").state is NodeState.READY
    assert instantiate(fake, run, "write_a") is InstantiationOutcome.ALREADY_DONE


def test_v26_a_crash_before_any_node_leaves_the_obligation_and_the_sweep_completes_it() -> None:
    fake = FakeDynamo()
    run = _seeded(fake)  # the admitting transaction committed; instantiation never ran
    assert kinds(fake, "INSTANTIATE#") == [(SHARD, f"INSTANTIATE#{run}")]
    assert store(fake).read_nodes(run) == ()
    context = PromoterContext(release_store(fake), planner("write_a"), lambda: NOW)
    counts = promote_shard(context, SHARD)
    assert counts.instantiated == 1
    assert kinds(fake, "INSTANTIATE#") == []
    assert node(fake, run, "write_a").state is NodeState.READY
    assert promote_shard(context, SHARD).instantiated == 0


def test_v26_a_crash_during_instantiation_resumes_idempotently() -> None:
    fake = FakeDynamo()
    run = _seeded(fake)
    current = store(fake).read_run(run)
    assert current is not None
    from ports.outbound.execution_graph_dispatch import PutNodesRequest  # noqa: PLC0415

    store(fake).put_nodes(PutNodesRequest(current, specs(run, "write_a"), now_text()))
    assert instantiate(fake, run, "write_a", "write_b") is InstantiationOutcome.ARMED
    assert {item.name for item in store(fake).read_nodes(run)} == {"write_a", "write_b"}
    assert kinds(fake, "INSTANTIATE#") == []


@pytest.mark.parametrize("reason", [FenceReason.CANCELLED, FenceReason.REPLACED])
def test_v26_a_fence_during_instantiation_settles_put_nodes_and_deletes_the_obligation(
    reason: FenceReason,
) -> None:
    fake = FakeDynamo()
    run = _seeded(fake)
    current = store(fake).read_run(run)
    assert current is not None
    from ports.outbound.execution_graph_dispatch import PutNodesRequest  # noqa: PLC0415

    store(fake).put_nodes(PutNodesRequest(current, specs(run, "write_a"), now_text()))
    _fence(fake, run, reason)
    assert instantiate(fake, run, "write_a") is InstantiationOutcome.FENCED
    assert node(fake, run, "write_a").state is NodeState.FENCED_UNSENT
    assert kinds(fake, "INSTANTIATE#") == []
    assert kinds(fake, "LOCK#") == kinds(fake, "TOKEN#") == kinds(fake, "OUTBOX#") == []
    assert try_terminalize(store(fake), run) is not None


def test_v26_a_fence_racing_the_arming_transaction_is_run_not_instantiable_then_settles() -> None:
    fake = FakeDynamo()
    run = _seeded(fake)
    calls = {"n": 0}

    def fence_before_arming(_fake: Any) -> None:
        _fence(fake, run, FenceReason.CANCELLED)

    original = fake.transact_write_items

    def counted(**kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 2:
            fake.before_transaction = fence_before_arming
        return original(**kwargs)

    fake.transact_write_items = counted  # type: ignore[method-assign]
    assert instantiate(fake, run, "write_a") is InstantiationOutcome.FENCED
    assert node(fake, run, "write_a").state is NodeState.FENCED_UNSENT
    assert kinds(fake, "LOCK#") == kinds(fake, "TOKEN#") == kinds(fake, "OUTBOX#") == []
    assert kinds(fake, "INSTANTIATE#") == []


def test_v26_a_terminal_is_refused_while_the_obligation_exists() -> None:
    fake = FakeDynamo()
    run = _seeded(fake)
    _fence(fake, run, FenceReason.CANCELLED)
    assert try_terminalize(store(fake), run) is None
    still = store(fake).read_run(run)
    assert still is not None and still.state is RunState.FENCING
    instantiate(fake, run, "write_a")
    assert try_terminalize(store(fake), run) is RunState.CANCELLED


def test_v26_a_pause_during_instantiation_continues_under_the_new_generation() -> None:
    fake = FakeDynamo()
    run = _seeded(fake)
    original = fake.transact_write_items
    calls = {"n": 0}

    def pause_before_arming(_fake: Any) -> None:
        run_operator_command(
            store(fake),
            OperatorCommand(RunCommandKind.PAUSE, "pause-0001", OPERATOR, now_text, run_id=run),
        )

    def counted(**kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 2:
            fake.before_transaction = pause_before_arming
        return original(**kwargs)

    fake.transact_write_items = counted  # type: ignore[method-assign]
    assert instantiate(fake, run, "write_a") is InstantiationOutcome.ARMED
    armed = node(fake, run, "write_a")
    assert armed.state is NodeState.READY
    assert armed.armed_generation == 1
    sender = SpySender()
    dispatch_shard(DispatchContext(store(fake), sender, now_text), SHARD)
    assert sender.calls == []  # PAUSED: the armed root's permit is refused until resume
    run_operator_command(
        store(fake),
        OperatorCommand(RunCommandKind.RESUME, "resume-001", OPERATOR, now_text, run_id=run),
    )
    dispatch_shard(DispatchContext(store(fake), sender, now_text), SHARD)
    assert len(sender.calls) == 1


def test_fr4_an_envelope_above_the_provider_ceiling_fences_the_run_oversize() -> None:
    fake = FakeDynamo()
    run = _seeded(fake)

    def oversize(record: RunRecord, _receipt: Any) -> tuple[NodeSpec, ...]:
        (spec,) = specs(record.run_id, "write_a")
        return (
            NodeSpec(
                spec.name,
                spec.after,
                spec.phase,
                spec.lock_key,
                spec.group_id,
                json.dumps({"roster": "x" * (PROVIDER_CEILING_BYTES + 1)}),
            ),
        )

    outcome = instantiate_run(store(fake), InstantiateRequest(run, oversize, now_text))
    assert outcome is InstantiationOutcome.OVERSIZE
    fenced = store(fake).read_run(run)
    assert fenced is not None and fenced.fence_reason is FenceReason.OVERSIZE
    assert store(fake).read_nodes(run) == ()


def test_the_ingestion_plan_is_the_static_spine_and_only_materialize_raw_is_a_root() -> None:
    """Revision 2 (design §2.1, G1): the spine is `materialize_raw` and `grant_apply`."""
    fake = FakeDynamo()
    run = _seeded(fake)
    record = store(fake).read_run(run)
    assert record is not None
    plan = ingestion_node_plan(record, cast("Any", None))
    assert [(spec.name, spec.after, spec.phase) for spec in plan] == [
        ("materialize_raw", (), NodePhase.PRE_GRANT),
        ("grant_apply", ("write_unit",), NodePhase.PRE_GRANT),
    ]
    assert plan[0].lock_key == f"MATERIALIZE#{record.conflict_unit}"
    assert json.loads(plan[0].body) == {
        "schema_version": "1",
        "node": "materialize_raw",
        "conflict_unit": record.conflict_unit,
        "root_flow_id": record.root_flow_id,
    }
    outcome = instantiate_run(store(fake), InstantiateRequest(run, ingestion_node_plan, now_text))
    assert outcome is InstantiationOutcome.ARMED
    assert kinds(fake, "INSTANTIATE#") == []
    assert node(fake, run, "materialize_raw").state is NodeState.READY
    assert node(fake, run, "grant_apply").state is NodeState.PENDING


def test_instantiation_drops_post_grant_specs_from_the_ingestion_plan() -> None:
    """Control: a planner that also returns POST_GRANT specs still puts only the spine."""
    fake = FakeDynamo()
    run = _seeded(fake)

    def with_post_grant(record: RunRecord, receipt: Any) -> tuple[NodeSpec, ...]:
        spine = ingestion_node_plan(record, receipt)
        post = NodeSpec("publish", ("grant_apply",), NodePhase.POST_GRANT, "p", "p", "{}")
        return (*spine, post)

    instantiate_run(store(fake), InstantiateRequest(run, with_post_grant, now_text))
    assert {n.name for n in store(fake).read_nodes(run)} == {"materialize_raw", "grant_apply"}


def test_bf6_instantiation_without_an_admitting_receipt_is_refused() -> None:
    fake = FakeDynamo()
    run = _seeded(fake)
    fake.items[("RECEIPT#request-0001", "RECEIPT")]["decision"] = write_text("QUEUED_LATEST")
    with pytest.raises(GraphCommandRefusedError) as refused:
        instantiate(fake, run, "write_a")
    assert refused.value.signal == "RUN_REQUEST_RECEIPT_REQUIRED"
    assert store(fake).read_nodes(run) == ()


def test_the_sweep_leaves_a_fresh_obligation_to_its_inline_caller() -> None:
    fake = FakeDynamo()
    run = _seeded(fake)
    fake.items[(SHARD, f"INSTANTIATE#{run}")]["due_at"] = write_text(NOW.isoformat())
    context = PromoterContext(
        release_store(fake), planner("write_a"), lambda: NOW + timedelta(seconds=10)
    )
    assert promote_shard(context, SHARD).instantiated == 0
    assert kinds(fake, "INSTANTIATE#") != []


def test_s3v2_sizing_measures_the_bound_envelope_not_the_owner_body() -> None:
    fake = FakeDynamo()
    run = _seeded(fake)

    def near_ceiling(record: RunRecord, _receipt: Any) -> tuple[NodeSpec, ...]:
        (spec,) = specs(record.run_id, "write_a")
        filler = PROVIDER_CEILING_BYTES - len(json.dumps({"roster": ""})) - 8
        body = json.dumps({"roster": "x" * filler})
        assert len(body.encode()) < PROVIDER_CEILING_BYTES  # the owner body alone fits
        return (NodeSpec(spec.name, (), spec.phase, spec.lock_key, spec.group_id, body),)

    outcome = instantiate_run(store(fake), InstantiateRequest(run, near_ceiling, now_text))
    assert outcome is InstantiationOutcome.OVERSIZE


def test_s3v2_the_stored_envelope_carries_a_binding_derived_from_its_owner_body() -> None:
    from adapters.outbound.dynamodb.execution_graph_dispatch import (  # noqa: PLC0415
        dispatch_id_for,
        envelope_digest,
    )

    fake = FakeDynamo()
    run = _seeded(fake)
    assert instantiate(fake, run, "write_a") is InstantiationOutcome.ARMED
    stored = node(fake, run, "write_a")
    body = json.loads(stored.body)
    assert {body["dispatch_id"], body["run_id"], body["node_id"]} == {
        stored.dispatch_id,
        run,
        stored.node_id,
    }
    assert envelope_digest(stored.body) == stored.envelope_digest
    assert dispatch_id_for(stored.node_id, stored.envelope_digest) == stored.dispatch_id
    assert stored.measured_bytes == len(stored.body.encode())
    (spec,) = specs(run, "write_a")
    with pytest.raises(ValueError, match="binding fields"):
        from adapters.outbound.dynamodb.execution_graph_dispatch import (  # noqa: PLC0415
            graph_envelope,
        )

        graph_envelope(
            run, NodeSpec(spec.name, (), spec.phase, spec.lock_key, spec.group_id, stored.body)
        )


def test_s3v1_v26_a_fence_during_a_multi_node_instantiation_settles_every_node() -> None:
    from ports.outbound.execution_graph_dispatch import PutNodesRequest  # noqa: PLC0415

    fake = FakeDynamo()
    run = _seeded(fake)
    current = store(fake).read_run(run)
    assert current is not None
    graph = specs(run, "write_a", "write_b", successors=("publish",))
    store(fake).put_nodes(PutNodesRequest(current, graph, now_text()))
    _fence(fake, run, FenceReason.CANCELLED)
    plan = planner("write_a", "write_b", successors=("publish",))
    assert instantiate_run(store(fake), InstantiateRequest(run, plan, now_text)) is (
        InstantiationOutcome.FENCED
    )
    assert {item.state for item in store(fake).read_nodes(run)} == {NodeState.FENCED_UNSENT}
    assert try_terminalize(store(fake), run) is RunState.CANCELLED


# --- S6 §2.2: armed_at, PENDING successors, ARM# and RELEASE# --------------------------------


def test_arm_roots_stamps_armed_at_beside_the_armed_generation() -> None:
    fake = FakeDynamo()
    run = _seeded(fake)
    assert (
        instantiate_run(store(fake), InstantiateRequest(run, ingestion_node_plan, now_text))
        is InstantiationOutcome.ARMED
    )
    root = fake.items[(f"RUN#{run}", "NODE#materialize_raw")]
    assert (root["armed_generation"], root["armed_at"]) == ({"N": "0"}, {"S": now_text()})
    # Control: the never-armed `grant_apply` carries neither.
    spine = fake.items[(f"RUN#{run}", "NODE#grant_apply")]
    assert "armed_at" not in spine and "armed_generation" not in spine


def test_successors_are_put_pending_in_the_node_shape_instantiation_writes() -> None:
    fake = FakeDynamo()
    run = materialized_run(fake, "orders")
    put = fake.items[(f"RUN#{run}", "NODE#write_unit:orders")]
    spine = fake.items[(f"RUN#{run}", "NODE#grant_apply")]  # put PENDING by instantiation
    assert set(put) == set(spine)
    assert put["state"] == {"S": "PENDING"} and put["after"] == {"L": [{"S": "materialize_raw"}]}
    unit = node(fake, run, "write_unit:orders")
    # The node item only: no token, lock or outbox until the arm pass.
    assert (f"TOKEN#{unit.node_id}", "TOKEN") not in fake.items
    assert ("LOCK#TABLE#orders", "LOCK") not in fake.items
    assert (SHARD, f"OUTBOX#{unit.node_id}") not in fake.items
    arm = fake.items[(SHARD, f"ARM#{run}")]
    release = fake.items[(SHARD, f"RELEASE#{run}")]
    assert (arm["scope"], arm["root_clock_started_at"]) == ({"S": "units"}, {"S": ROOT_CLOCK})
    # S5-10: the release trigger is stamped with the run's root clock.
    assert release["root_clock_started_at"] == {"S": ROOT_CLOCK}
    assert release["run_id"] == {"S": run}


def test_successors_on_a_run_that_is_not_live_are_refused_with_zero_effect() -> None:
    fake = FakeDynamo()
    run = _seeded(fake)
    instantiate_run(store(fake), InstantiateRequest(run, ingestion_node_plan, now_text))
    live = copy.deepcopy(fake)
    _fence(fake, run, FenceReason.CANCELLED)
    fenced = store(fake).read_run(run)
    assert fenced is not None
    before = copy.deepcopy(fake.items)
    request = SuccessorRequest(fenced, unit_specs(run, "orders"), ArmScope.UNITS, True, now_text())

    with pytest.raises(GraphGuardError) as refused:
        store(fake).put_successors(request)

    assert refused.value.signal == RUN_STATE_CHANGED
    assert fake.items == before
    # Control: on the same run still live, the same request commits.
    live_run = store(live).read_run(run)
    assert live_run is not None
    store(live).put_successors(
        SuccessorRequest(live_run, unit_specs(run, "orders"), ArmScope.UNITS, True, now_text())
    )
    assert node(live, run, "write_unit:orders").state is NodeState.PENDING
    assert (SHARD, f"ARM#{run}") in live.items and (SHARD, f"RELEASE#{run}") in live.items
