# SPDX-License-Identifier: MIT-0
"""The V3 GRAPH bake: fan-out, roster tasks and the finalizer (plan 4.6 under Addendum A3).

Every refusal names its exact typed signal and the layer that refused it, beside a paired
control that the same fixture one step away is accepted. Every identity is fictional.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field, replace
from typing import Any, cast

import pytest
from execution_graph_dispatch_support import NOW
from execution_graph_release_support import (
    release_store,
)
from graph_bake_v3_support import (
    Bake,
    SeedHistoryRequest,
    current_identity,
    final_transaction,
    roster,
)
from graph_consumer_support import expire_claim, precompute_contributed, sqs_record
from refusal_telemetry_support import LOGGER, Telemetry, capture

from adapters.jobs.execution_graph.post_grant_consumer import ConsumeResult
from adapters.jobs.insight_precompute.graph_precompute import PRECOMPUTE_CLAIM_LEASE
from adapters.jobs.preset_artifact_publisher import baker as baker_module
from adapters.jobs.preset_artifact_publisher import graph_bake
from adapters.jobs.preset_artifact_publisher.graph_bake import (
    BAKE_INTENT_INVARIANT,
    FINALIZE_ARMED_REDELIVERY_SECONDS,
    PRECOMPUTE_ARMED,
    task_node_name,
)
from adapters.jobs.preset_artifact_publisher.page_bake import ProcessBakeLedger
from adapters.jobs.preset_artifact_publisher.transport import BakeDelivery, parse_bake_v3_body
from adapters.outbound.athena.execution import QUERY_TIMED_OUT
from adapters.outbound.dynamodb import execution_graph_release as release_adapter
from adapters.outbound.dynamodb.execution_graph import due_shard
from adapters.outbound.dynamodb.execution_graph_release import DynamoGraphReleaseStore
from adapters.outbound.dynamodb.graph_transactions import Member
from adapters.outbound.telemetry.reporting import LogSeverity
from domain.operations.bake_refusals import (
    IsolatedRefusal,
    IsolatedRefusalReason,
    RefusalScope,
)
from domain.operations.bake_scheduling import (
    BakeTaskLoad,
    LanePlan,
    task_seconds,
    two_lane_partition,
)
from domain.operations.execution_graph_dispatch import (
    CLAIM_LEASE_STALE,
    GraphGuardError,
    NodeState,
)
from domain.operations.execution_graph_release import (
    BAKE_EXECUTIONS_OVER_CERTIFIED_BOUND,
    BAKE_PLAN_COUNT_MISMATCH,
    BakeEffectKind,
    BakeRoster,
    BakeRosterRow,
    binding_authorizes_serving,
    final_transaction_member_count,
)
from domain.operations.generated_execution_graphs import BAKE_TASKS, ROUTES
from domain.presentation.preset_artifacts import PresetArtifactBodyCeilingError
from domain.shared_kernel.errors import DependencyError, DomainError, ErrorCode, InvariantError
from ports.outbound.execution_graph_dispatch import GraphStoreUnavailableError
from ports.outbound.execution_graph_release import BakeFinalizeOutcome

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-075")]


def _keys(bake: Bake, run: str, prefix: str) -> list[str]:
    return sorted(
        sk for pk, sk in bake.tables.items if pk == f"RUN#{run}" and sk.startswith(prefix)
    )


def _outboxes(bake: Bake, run: str) -> list[str]:
    unit = bake.run_item(run)["conflict_unit"]["S"]
    return sorted(sk for pk, sk in bake.tables.items if pk == due_shard(unit) and "OUTBOX#" in sk)


# --- fan-out (S5, D11) ----------------------------------------------------------------------


def test_fan_out_pins_then_partitions_then_arms_every_task_on_two_lanes() -> None:
    bake = Bake()
    run = bake.granted()

    result = bake.fan_out(run)

    assert (result.acknowledge, result.node_state) == (True, NodeState.SUCCEEDED)
    assert _keys(bake, run, "BAKE_") == [
        "BAKE_PIN",
        *sorted(f"BAKE_SOURCE#{key}" for key in bake.roster.keys),
    ]
    tasks = [bake.node(run, task_node_name(key)) for key in bake.roster.keys]
    assert {task.state for task in tasks} == {NodeState.READY}
    assert {task.group_id for task in tasks} == {"preset-bake-lane-0", "preset-bake-lane-1"}
    assert bake.node(run, "bake_finalize").state is NodeState.PENDING
    # The bind pass ran every source under the capture: zero Athena executions in fan-out.
    assert bake.queries.calls == []
    assert bake.current.current_calls == len(bake.roster.rows)


def test_control_a_fan_out_whose_pin_is_absent_writes_no_outbox_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S5: the fan-out transaction ConditionChecks the stored pin; no pin, no outbox."""
    bake = Bake()
    run = bake.granted()
    before = _outboxes(bake, run)
    store = release_store(bake.tables)

    def no_pin(_request: object) -> None:
        return None

    monkeypatch.setattr(store, "pin_durable_bake_sources", no_pin)
    object.__setattr__(bake.graph, "store", store)

    result = bake.fan_out(run)

    assert result.acknowledge is False  # the refused transaction releases and retries
    assert _outboxes(bake, run) == before
    assert not any(sk.startswith("NODE#bake_task") for _pk, sk in bake.tables.items)


def test_selection_runs_at_most_once_per_winner_and_never_from_a_task() -> None:
    bake = Bake()
    run = bake.granted()
    bake.fan_out(run)
    selected = bake.current.current_calls

    bake.fan_out(run)  # a replayed fan-out adopts its pin and its tasks
    bake.all_tasks(run)

    assert bake.current.current_calls == selected
    assert bake.current.exact_calls == len(bake.roster.rows)


def test_a_reclaim_after_selection_without_a_pin_is_the_fatal_pin_absent_refusal() -> None:
    """DB21: process loss after selection and before the pin never selects CURRENT again."""
    bake = Bake()
    run = bake.granted()
    node = bake.node(run, "bake")
    bake.sources.raises[bake.roster.keys[0]] = RuntimeError("process lost mid-selection")
    first = bake.fan_out(run)
    assert first.acknowledge is False
    bake.sources.raises.clear()
    claim = bake.tables.items[(f"EXEC_CLAIM#{node.node_id}", "CLAIM")]
    claim["lease_expires_at"] = {"S": "2000-01-01T00:00:00+00:00"}
    calls = bake.current.current_calls

    second = bake.fan_out(run, receive_count=2)

    # The consumer invariant settles the fan-out FAILED: no second current selection.
    assert second.node_state is NodeState.FAILED
    assert bake.current.current_calls == calls
    assert not any(sk == "BAKE_PIN" for _pk, sk in bake.tables.items)


def test_the_partition_runs_on_certified_counts_not_on_the_pinned_bind_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A5.3: each row's load is its certified upper bound, never its pinned bind count.

    A result-dependent row's bind count undercounts its render, so two different bind-count sets
    give one partition.
    """
    from adapters.jobs.preset_artifact_publisher import graph_bake  # noqa: PLC0415

    plans: list[LanePlan] = []

    def spy(tasks: tuple[BakeTaskLoad, ...]) -> LanePlan:
        plans.append(two_lane_partition(tasks))
        return plans[-1]

    monkeypatch.setattr(graph_bake, "two_lane_partition", spy)
    contract = BakeRoster.from_contract()
    for counts in (
        (1,) * len(contract.rows),
        tuple(row.certified_plan_count for row in contract.rows),
    ):
        bake = Bake(roster=contract)
        bake.sources.queries = dict(zip(bake.roster.keys, counts, strict=True))
        run = bake.granted()
        bake.fan_out(run)
        pin = release_store(bake.tables).read_durable_bake_sources(run)
        assert pin is not None
        assert tuple(task.plan_count for task in pin.manifest.tasks) == counts
        assert [bake.node(run, task_node_name(key)).group_id for key in bake.roster.keys] == [
            f"preset-bake-lane-{plans[-1].lane_of(key)}" for key in bake.roster.keys
        ]
    expected = two_lane_partition(
        tuple(
            BakeTaskLoad(row.key, task_seconds(row.certified_plan_count, 10, row.write_seconds))
            for row in contract.rows
        )
    )
    assert plans == [expected, expected]
    # Control: the pinned bind counts would have partitioned differently.
    assert (
        two_lane_partition(
            tuple(
                BakeTaskLoad(row.key, task_seconds(1, 10, row.write_seconds))
                for row in contract.rows
            )
        )
        != expected
    )


# --- tasks (DB21, DB22, D18) ----------------------------------------------------------------


def test_a_task_replays_its_pin_runs_its_queries_and_settles_one_written_effect() -> None:
    bake = Bake(roster=roster(1))
    run = bake.granted()
    bake.fan_out(run)
    bake.dispatch(run)
    selected = bake.current.current_calls

    result = bake.task(run, bake.roster.keys[0])

    assert (result.acknowledge, result.node_state) == (True, NodeState.SUCCEEDED)
    assert bake.current.current_calls == selected  # replay, never CURRENT
    assert bake.queries.calls == [bake.roster.keys[0]] * 2
    assert bake.stage.staged == [bake.roster.keys[0]]
    assert bake.runtime.peak_live_payloads == 1
    assert bake.tables.pointer_writes() == []  # a task has no pointer capability


def test_a_bound_count_differing_from_the_pin_fails_plan_count_mismatch_with_zero_queries() -> None:
    bake = Bake(roster=roster(2))
    run = bake.granted()
    bake.fan_out(run)
    bake.dispatch(run)
    first, second = bake.roster.keys
    bake.sources.extra_on_task.add(first)

    refused = bake.task(run, first)
    assert bake.invariants == [BAKE_INTENT_INVARIANT]  # §2.11 row 5b counts it once
    accepted = bake.task(run, second)

    assert (refused.node_state, refused.failure_reason) == (
        NodeState.FAILED,
        BAKE_PLAN_COUNT_MISMATCH,
    )
    assert first not in bake.queries.calls
    unit = bake.run_item(run)["conflict_unit"]["S"]
    task = bake.node(run, task_node_name(first))
    recovery = bake.tables.items[(due_shard(unit), f"BAKE_RECOVERY#{task.node_id}")]
    assert recovery["reason"]["S"] == "fatal"
    assert "root_clock_started_at" in recovery
    # Control: the sibling whose bound set equals its pin runs, succeeds and counts nothing.
    assert accepted.node_state is NodeState.SUCCEEDED
    assert bake.invariants == [BAKE_INTENT_INVARIANT]


def test_d18_an_over_cap_body_is_a_final_task_failure_and_abandons_finalization() -> None:
    bake = Bake(roster=roster(2))
    run = bake.granted()
    bake.fan_out(run)
    bake.dispatch(run)
    first, second = bake.roster.keys
    bake.sources.raises[first] = PresetArtifactBodyCeilingError("retail_body_ceiling", 256_001)

    failed = bake.task(run, first)
    bake.task(run, second)

    assert (failed.node_state, failed.failure_reason) == (NodeState.FAILED, "retail_body_ceiling")
    assert bake.invariants == []  # the D18 ceiling is a typed final failure, not an invariant
    assert bake.stage.staged == [second]
    bake.arm()
    # B4: every task is terminal, so the finalizer arms and abandons the cohort withheld.
    assert bake.node(run, "bake_finalize").state is NodeState.READY
    bake.dispatch(run)
    abandoned = bake.deliver(run, "bake_finalize")
    assert (abandoned.outcome, abandoned.node_state, abandoned.failure_reason) == (
        "graph_bake_finalize_abandoned",
        NodeState.FAILED,
        "bake_task_failed",
    )
    assert bake.tables.pointer_writes() == []


def test_a_dependency_failure_retries_and_the_final_receive_is_the_pages_absence() -> None:
    """ADR-142 §3.3 step 3: the page's own dependency on its final receive is designed absence."""
    bake = Bake(roster=roster(1))
    run = bake.granted()
    bake.fan_out(run)
    bake.dispatch(run)
    (key,) = bake.roster.keys
    bake.queries.fail[key] = DependencyError(ErrorCode("FICTIONAL", 1), "athena throttled")
    retried = bake.task(run, key, receive_count=1)
    expire_claim(bake.tables, bake.node(run, task_node_name(key)))
    final = bake.task(run, key, receive_count=3)
    assert (retried.acknowledge, retried.node_state) == (False, NodeState.CLAIMED)
    assert (final.outcome, final.node_state) == ("graph_bake_task_unavailable", NodeState.SUCCEEDED)
    _effect, extra = graph_bake.read_task_effect(bake.node(run, task_node_name(key)))
    assert extra["isolated_refusals"] == [
        {
            "scope": "page",
            "reason": "dependency_unavailable",
            "refusal_class": "dependency",
            "count": 1,
            "context": {"error_code": "ADLY-FICTIONAL-0001"},
        }
    ]


def test_control_a_pin_read_dependency_on_the_final_receive_is_still_retry_exhausted() -> None:
    """Outside the page's work (§3.3 step 6): the task's pin read still settles FAILED."""
    bake = Bake(roster=roster(1))
    run = bake.granted()
    bake.fan_out(run)
    bake.dispatch(run)
    (key,) = bake.roster.keys
    bake.tables.fail_query = "{'S': 'BAKE_'}"  # the task's `read_durable_bake_sources`
    retried = bake.task(run, key, receive_count=1)
    expire_claim(bake.tables, bake.node(run, task_node_name(key)))
    exhausted = bake.task(run, key, receive_count=3)
    bake.tables.fail_query = None
    assert (retried.acknowledge, retried.node_state) == (False, NodeState.CLAIMED)
    assert exhausted.node_state is NodeState.FAILED
    unit = bake.run_item(run)["conflict_unit"]["S"]
    task = bake.node(run, task_node_name(key))
    recovery = bake.tables.items[(due_shard(unit), f"BAKE_RECOVERY#{task.node_id}")]
    assert recovery["reason"]["S"] == "retry_exhausted"


# --- the finalizer (DB7, DB15 under A3) -----------------------------------------------------


def _ready(bake: Bake) -> str:
    bake.seed_predecessors()  # every write supersedes: three pointer members each
    run = bake.granted()
    bake.fan_out(run)
    assert all(result.node_state is NodeState.SUCCEEDED for result in bake.all_tasks(run))
    return run


@pytest.mark.parametrize("count", [3, 9, 21])
def test_db25_exact_effect_roster_arms_precompute_and_stamps_complete(count: int) -> None:
    bake = Bake(roster=roster(count))
    run = _ready(bake)
    bake.arm()
    bake.dispatch(run)
    before = len(bake.tables.transactions)

    result = bake.deliver(run, "bake_finalize")

    assert (result.acknowledge, result.outcome) == (False, "graph_bake_precompute_armed")
    readiness = release_store(bake.tables).read_precompute_input(run)
    assert readiness is not None and len(readiness.entries) == count
    assert tuple(entry.key for entry in readiness.entries) == bake.roster.keys
    complete = bake.tables.items[
        (due_shard(bake.run_item(run)["conflict_unit"]["S"]), f"COMPLETE#{run}")
    ]
    assert complete["input_effect_roster_digest"]["S"] == readiness.digest
    transaction = next(
        members
        for members in bake.tables.transactions[before:]
        if "bake_readiness_at" in str(members)
    )
    assert len(transaction) == count + 8


def test_db25_source_identity_mismatch_is_refused_before_readiness_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bake = Bake(roster=roster(3))
    run = _ready(bake)
    original = graph_bake._precompute_input  # pyright: ignore[reportPrivateUsage]

    def mismatched(runtime: Any, parsed: Any) -> Any:
        inputs = original(runtime, parsed)
        first = replace(inputs.entries[0], source_manifest_digest="sha256:" + "e" * 64)
        return replace(inputs, entries=(first, *inputs.entries[1:]))

    monkeypatch.setattr(graph_bake, "_precompute_input", mismatched)
    before = len(bake.tables.transactions)

    refused = bake.finalize(run)

    assert (refused.acknowledge, refused.outcome) == (False, "graph_bake_finalize_retryable")
    assert bake.node(run, "precompute").state is NodeState.WAITING_INPUT
    assert release_store(bake.tables).read_precompute_input(run) is None
    assert bake.tables.pointer_writes(before) == []
    # Paired positive control: the unmodified source projection arms and finalizes.
    monkeypatch.setattr(graph_bake, "_precompute_input", original)
    control = Bake(roster=roster(3))
    assert control.finalize(_ready(control)).node_state is NodeState.SUCCEEDED


def test_db25_readiness_response_loss_adopts_the_exact_post_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bake = Bake(roster=roster(3))
    run = _ready(bake)
    bake.arm()
    bake.dispatch(run)
    original = bake.tables.transact_write_items
    injected = False

    def lose_readiness_response(**request: Any) -> dict[str, Any]:
        nonlocal injected
        if not injected and "bake_readiness_at" in str(request["TransactItems"]):
            injected = True
            bake.tables.ambiguous_next = True
        return original(**request)

    monkeypatch.setattr(bake.tables, "transact_write_items", lose_readiness_response)

    result = bake.deliver(run, "bake_finalize")

    assert injected is True
    assert (result.acknowledge, result.outcome) == (False, "graph_bake_precompute_armed")
    readiness = release_store(bake.tables).read_precompute_input(run)
    assert readiness is not None
    assert bake.node(run, "precompute").state is NodeState.READY


def test_db25_control_seven_of_nine_effects_never_arm_precompute() -> None:
    bake = Bake(roster=roster(9))
    run = bake.granted()
    bake.fan_out(run)
    bake.dispatch(run)
    for key in bake.roster.keys[:7]:
        assert bake.task(run, key).node_state is NodeState.SUCCEEDED
    before = len(bake.tables.transactions)

    bake.arm()

    assert bake.node(run, "precompute").state is NodeState.WAITING_INPUT
    assert release_store(bake.tables).read_precompute_input(run) is None
    assert len(bake.tables.transactions) == before


@pytest.mark.parametrize("count", [1, 20, 21])
def test_generated_final_terms_equal_the_built_transaction(count: int) -> None:
    """A5.3: the TransactItems the store's finalize_bake sends equal the generated prediction.

    The count comes from the in-memory tables, independent of the generated terms; every
    effect is written and superseding-free first writes are bounded by the same prediction.
    """
    from domain.operations.generated_execution_graphs import BAKE_TASKS  # noqa: PLC0415

    bake = Bake(roster=roster(count))
    run = _ready(bake)
    before = len(bake.tables.transactions)

    bake.finalize(run)

    built = len(final_transaction(bake.tables, before))
    predicted = (
        BAKE_TASKS["final_authority_members"]
        + BAKE_TASKS["final_task_condition_members"] * count
        + BAKE_TASKS["final_written_pointer_members"] * count
    )
    assert built <= predicted
    assert built == 10 + 4 * count
    # Control: the generator's old static estimate (13 authority members) is not the built list.
    assert built != 13 + 4 * count


@pytest.mark.parametrize("count", [3, 9])
def test_f8_finalization_over_n_tasks_commits_12_plus_4n_members_and_completes(
    count: int,
) -> None:
    bake = Bake(roster=roster(count))
    run = _ready(bake)
    before = len(bake.tables.transactions)

    result = bake.finalize(run)

    assert (result.acknowledge, result.node_state) == (True, NodeState.SUCCEEDED)
    final = final_transaction(bake.tables, before)
    assert len(final) == 10 + 4 * count  # every effect written: N conditions + 3N pointers
    item = bake.run_item(run)
    assert (item["state"]["S"], item["serving_outcome"]["S"]) == ("SUCCEEDED", "complete")
    marker = bake.tables.items[(f"SERVING_PUBLICATION#{run}", "MARKER")]
    assert marker["state"]["S"] == "FINALIZED"
    for key in bake.roster.keys:
        page, variant = key.split("#")
        pointer = bake.tables.page_pointer(page, variant)
        assert pointer is not None and pointer.grant_id is not None


def _final_values(members: list[dict[str, Any]], sk: str) -> tuple[str, dict[str, Any]]:
    """Return one final-transaction member's condition and bound values, by its sort key."""
    body = next(
        next(iter(member.values()))
        for member in members
        if next(iter(member.values())).get("Key", {}).get("sk", {}).get("S") == sk
    )
    return str(body.get("ConditionExpression")), dict(body.get("ExpressionAttributeValues", {}))


@pytest.mark.covers("ADR-140")
@pytest.mark.parametrize("digest", ["other", None])
def test_z3_a_stale_precompute_contribution_settles_withheld_with_zero_pointer_writes(
    digest: str | None,
) -> None:
    """ADR-140 (Z3): a stale precompute (another digest, or none) no longer dead-letters."""
    from domain.operations.execution_graph_release import (  # noqa: PLC0415
        CohortContribution,
        CohortMember,
        CohortOutcome,
        serving_withheld,
    )

    bake = Bake(roster=roster(3))
    signals = _Signals(bake)
    run = _ready(bake)
    bake.arm()
    bake.dispatch(run)
    first = bake.deliver(run, "bake_finalize")
    assert first.outcome == "graph_bake_precompute_armed"
    stored = release_store(bake.tables).read_precompute_input(run)
    assert stored is not None
    if digest is None:  # an older precompute's winner-only NO_ELIGIBLE: no readiness binding
        record = release_store(bake.tables).read_run(run)
        assert record is not None and record.grant_id is not None
        release_store(bake.tables).record_contribution(
            CohortContribution(
                record.grant_id,
                CohortMember.PRECOMPUTE,
                CohortOutcome.NO_ELIGIBLE,
                None,
                NOW,
                record.root_flow_id,
            )
        )
    else:
        precompute_contributed(bake.tables, run, "sha256:" + "d" * 64, bake.roster.keys)
    before = len(bake.tables.pointer_writes())
    pointers = _current_pointers(bake)
    since = len(bake.tables.transactions)

    settled = bake.deliver(run, "bake_finalize", 2)

    assert (settled.acknowledge, settled.outcome) == (True, "graph_bake_finalized")
    assert len(bake.tables.pointer_writes()) == before
    assert _current_pointers(bake) == pointers
    assert bake.run_item(run)["state"]["S"] == "SUCCEEDED"
    grant = bake.run_item(run)["grant_id"]["S"]
    binding = release_store(bake.tables).read_serving_binding(grant)
    assert binding is not None and serving_withheld(binding)
    assert not binding_authorizes_serving(binding)
    assert {reason for _key, reason in binding.unavailable} == {"post_grant_invariant"}
    assert signals.values == [1]
    (stale,) = signals.lines("stale_precompute")
    assert stale["has_digest"] is (digest is not None)
    # S1: the final transaction deletes COMPLETE# by the readiness stamp, and conditions the
    # stored precompute member exactly, by the absence of a digest it never carried.
    members = _final_commit(bake, since)
    _condition, deleted = _final_values(members, f"COMPLETE#{run}")
    assert deleted[":input_digest"] == {"S": stored.digest}
    condition, checked = _final_values(members, "CONTRIBUTION#precompute")
    if digest is None:
        assert condition.endswith("attribute_not_exists(input_effect_roster_digest)")
        assert ":input_digest" not in checked
    else:
        assert checked[":input_digest"] == {"S": "sha256:" + "d" * 64}


@pytest.mark.covers("ADR-140")
def test_z3_control_a_matching_precompute_authorizes_and_binds_the_one_readiness_digest() -> None:
    """Paired positive control: Z1's delete and check both bind the readiness digest, as before."""
    control = Bake(roster=roster(3))
    control_run = _ready(control)
    since = len(control.tables.transactions)
    assert control.finalize(control_run).node_state is NodeState.SUCCEEDED
    readiness = release_store(control.tables).read_precompute_input(control_run)
    assert readiness is not None
    members = _final_commit(control, since)
    assert _final_values(members, f"COMPLETE#{control_run}")[1][":input_digest"] == {
        "S": readiness.digest
    }
    assert _final_values(members, "CONTRIBUTION#precompute")[1][":input_digest"] == {
        "S": readiness.digest
    }


def test_a3_the_final_transaction_writes_no_generation_member() -> None:
    bake = Bake(roster=roster(3))
    run = _ready(bake)
    before = len(bake.tables.transactions)

    bake.finalize(run)

    committed = [
        member for transaction in bake.tables.transactions[before:] for member in transaction
    ]
    assert committed and not any("GENERATION#" in str(member) for member in committed)


def test_a3_control_a_finalizer_that_adds_a_generation_member_is_refused_with_zero_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bake = Bake(roster=roster(3))
    run = _ready(bake)
    original = release_adapter.DynamoGraphReleaseStore._final_members  # pyright: ignore[reportPrivateUsage]

    def with_generation(self: Any, request: Any) -> list[Member]:
        members = original(self, request)
        members.append(
            Member(
                {
                    "Update": {
                        "TableName": "fictional-publications",
                        "Key": {
                            "partition_key": {"S": "GENERATION#fictional"},
                            "record_key": {"S": "POINTER"},
                        },
                        "UpdateExpression": "SET grant_id = :g",
                        "ExpressionAttributeValues": {":g": {"S": "g"}},
                    }
                },  # type: ignore[typeddict-item]
                "NODE_STATE_CHANGED",
            )
        )
        return members

    request = _final_request(bake, run)
    # Control: the same request without the added member is accepted by the same guard.
    release_adapter.refuse_generation_members(
        release_store(bake.tables)._final_members(request)  # pyright: ignore[reportPrivateUsage]
    )
    monkeypatch.setattr(release_adapter.DynamoGraphReleaseStore, "_final_members", with_generation)
    before = len(bake.tables.transactions)
    with pytest.raises(InvariantError) as refused:
        release_store(bake.tables).finalize_bake(request)

    # The refusing layer is the store's pre-commit guard (`refuse_generation_members`).
    assert refused.value.context["signal"] == "finalizer_generation_member_refused"
    assert refused.value.code == ErrorCode("GRAPH_BAKE_V3", 5)
    assert len(bake.tables.transactions) == before
    assert bake.run_item(run)["state"]["S"] == "GRANTED"


def _final_request(bake: Bake, run: str) -> Any:
    """Build the finalizer's request exactly as it does, without committing it."""
    from adapters.jobs.preset_artifact_publisher import graph_bake  # noqa: PLC0415

    store = release_store(bake.tables)
    record = store.read_run(run)
    assert record is not None
    bake.arm()
    bake.dispatch(run)
    node = bake.node(run, "bake_finalize")
    head = graph_bake._live_head(store, record)  # pyright: ignore[reportPrivateUsage]
    assert head is not None
    from ports.outbound.execution_graph_dispatch import ClaimRecord  # noqa: PLC0415

    bake.tables.items[(f"EXEC_CLAIM#{node.node_id}", "CLAIM")] = {
        "pk": {"S": f"EXEC_CLAIM#{node.node_id}"},
        "sk": {"S": "CLAIM"},
        "lease_version": {"N": "1"},
    }
    claim = ClaimRecord(node.node_id, run, 1, "2099-01-01T00:00:00+00:00", "d", "c")
    built = graph_bake._finalize_request(  # pyright: ignore[reportPrivateUsage]
        graph_bake.BakePorts(store, bake.runtime.now),
        graph_bake._FinalizeInputs(record, node, claim, head),  # pyright: ignore[reportPrivateUsage]
        bake.runtime,
    )
    if isinstance(built, graph_bake.ConsumeResult):
        assert built.outcome == "graph_bake_precompute_armed"
        readiness = store.read_precompute_input(run)
        assert readiness is not None
        precompute_contributed(bake.tables, run, readiness.digest, bake.roster.keys)
        built = graph_bake._finalize_request(  # pyright: ignore[reportPrivateUsage]
            graph_bake.BakePorts(store, bake.runtime.now),
            graph_bake._FinalizeInputs(record, node, claim, head),  # pyright: ignore[reportPrivateUsage]
            bake.runtime,
        )
    assert not isinstance(built, graph_bake.ConsumeResult)
    return built


def _active_head(bake: Bake, run: str) -> Any:
    from adapters.jobs.preset_artifact_publisher import graph_bake  # noqa: PLC0415

    store = release_store(bake.tables)
    record = store.read_run(run)
    assert record is not None
    head = graph_bake._live_head(store, record)  # pyright: ignore[reportPrivateUsage]
    assert head is not None and head.state == "ACTIVE"
    return head


def _head_member(final: list[dict[str, Any]], domain: str) -> dict[str, Any]:
    (member,) = (
        body
        for item in final
        for body in item.values()
        if body.get("Key", {}).get("pk") == {"S": f"SERVING_DOMAIN#{domain}"}
    )
    return member


def test_finalization_keys_on_the_immutable_cohort_snapshot_not_the_live_frontier() -> None:
    """Plan item 4: a wave-B settlement on the live frontier leaves A's finalization intact."""
    bake = Bake(roster=roster(3))
    run = _ready(bake)
    head = _active_head(bake, run)
    # Wave B settles on every live shard while A is ACTIVE: only the frontier moves.
    for (pk, _sk), item in bake.tables.items.items():
        if pk.startswith(f"SERVING_FRONTIER#{head.domain}#"):
            for name in ("revision", "visible_count", "eligible_count"):
                item[name] = {"N": str(int(item[name]["N"]) + 1)}
    before = len(bake.tables.transactions)

    result = bake.finalize(run)

    assert (result.acknowledge, result.node_state) == (True, NodeState.SUCCEEDED)
    final = final_transaction(bake.tables, before)
    member = _head_member(final, head.domain)
    condition = member["ConditionExpression"]
    for name in ("cohort_revision", "elected_frontier_digest", "elected_candidate_digest"):
        assert name in condition
    values = list(member["ExpressionAttributeValues"].values())
    for frozen in (
        head.cohort_revision,
        head.elected_frontier_digest,
        head.elected_candidate_digest,
    ):
        assert {"S": frozen} in values
    rendered = json.dumps(final)
    for retired in ("SERVING_FRONTIER#", "SERVING_DAY#", "CAPACITY_WAIT#", "cycle_epoch"):
        assert retired not in rendered
    assert bake.run_item(run)["state"]["S"] == "SUCCEEDED"


def test_control_a_changed_elected_snapshot_refuses_finalization_with_zero_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bake = Bake(roster=roster(3))
    run = _ready(bake)
    head = bake.tables.items[(f"SERVING_DOMAIN#{_active_head(bake, run).domain}", "HEAD")]
    frozen = dict(head["elected_candidate_digest"])
    original = release_adapter.DynamoGraphReleaseStore.finalize_bake
    refusals: list[str] = []

    def raced(self: Any, request: Any) -> BakeFinalizeOutcome:
        # The snapshot changes after the finalizer read it and before its one transaction.
        head["elected_candidate_digest"] = {"S": "f" * 64}
        try:
            return original(self, request)
        except GraphGuardError as refused:
            refusals.append(refused.signal)
            raise

    bake.arm()
    bake.dispatch(run)
    armed = bake.deliver(run, "bake_finalize")
    assert armed.outcome == "graph_bake_precompute_armed"
    readiness = release_store(bake.tables).read_precompute_input(run)
    assert readiness is not None
    precompute_contributed(bake.tables, run, readiness.digest, bake.roster.keys)
    monkeypatch.setattr(release_adapter.DynamoGraphReleaseStore, "finalize_bake", raced)
    before = len(bake.tables.transactions)

    refused = bake.deliver(run, "bake_finalize", 2)

    assert (refused.acknowledge, refused.outcome, refused.node_state) == (
        False,
        "graph_bake_finalize_conflict",
        NodeState.CLAIMED,
    )
    assert refusals == ["SERVING_HEAD_CHANGED"]
    assert bake.tables.pointer_writes(before) == []
    assert bake.run_item(run)["state"]["S"] == "GRANTED"
    # Paired control: the same finalizer over the unchanged snapshot commits.
    monkeypatch.setattr(release_adapter.DynamoGraphReleaseStore, "finalize_bake", original)
    head["elected_candidate_digest"] = frozen
    _expire_claim(bake, run)
    assert bake.deliver(run, "bake_finalize", 3).node_state is NodeState.SUCCEEDED
    assert bake.tables.pointer_writes(before)
    assert bake.run_item(run)["state"]["S"] == "SUCCEEDED"


def _expire_claim(bake: Bake, run: str) -> None:
    node = bake.node(run, "bake_finalize")
    claim = bake.tables.items.get((f"EXEC_CLAIM#{node.node_id}", "CLAIM"))
    if claim is not None:
        claim["lease_expires_at"] = {"S": "2000-01-01T00:00:00+00:00"}


def test_mixed_effects_finalize_with_pointers_only_for_written_effects() -> None:
    bake = Bake(roster=roster(3))
    unavailable, unchanged, written = bake.roster.keys
    bake.tables.seed_pointer(unchanged, current_identity(unchanged))  # already CURRENT
    bake.tables.seed_pointer(written)  # a predecessor: the write supersedes it
    unchanged_before = bake.tables.page_pointer(*unchanged.split("#"))
    run = bake.granted()
    bake.sources.unavailable.add(unavailable)
    bake.fan_out(run)
    bake.all_tasks(run)
    before = len(bake.tables.transactions)

    bake.finalize(run)

    final = final_transaction(bake.tables, before)
    assert len(final) == 10 + 3 + 3  # three effect conditions; pointers for `written` only
    kinds = {effect.key: effect.effect_kind for effect in _committed_effects(bake, run)}
    assert kinds == {
        unavailable: BakeEffectKind.UNAVAILABLE,
        unchanged: BakeEffectKind.UNCHANGED,
        written: BakeEffectKind.WRITTEN,
    }
    assert bake.tables.page_pointer(*unavailable.split("#")) is None
    assert bake.tables.page_pointer(*unchanged.split("#")) == unchanged_before
    assert bake.tables.page_pointer(*written.split("#")) is not None
    binding = release_store(bake.tables).read_serving_binding(bake.run_item(run)["grant_id"]["S"])
    assert binding is not None
    assert binding.unavailable == ((unavailable, "campaigns_materialization_unavailable"),)
    # ADR-139: a designed absence leaves the binding authorizing, so the written page moved.
    assert binding_authorizes_serving(binding)


def _committed_effects(bake: Bake, run: str) -> list[Any]:
    from adapters.jobs.preset_artifact_publisher.graph_bake import read_task_effect  # noqa: PLC0415

    return [read_task_effect(bake.node(run, task_node_name(key)))[0] for key in bake.roster.keys]


def test_finalization_is_refused_with_a_nonterminal_task_and_writes_nothing_visible() -> None:
    bake = Bake(roster=roster(2))
    run = bake.granted()
    bake.fan_out(run)
    bake.dispatch(run)
    bake.task(run, bake.roster.keys[0])  # the second task never ran
    bake.arm()

    assert bake.node(run, "bake_finalize").state is NodeState.PENDING
    assert bake.tables.pointer_writes() == []
    assert bake.run_item(run)["state"]["S"] == "GRANTED"
    # Control: once the second task succeeds the arm pass arms the finalizer and it commits.
    bake.task(run, bake.roster.keys[1])
    assert bake.finalize(run).node_state is NodeState.SUCCEEDED


def test_a_lost_final_response_with_the_exact_post_image_is_already_finalized() -> None:
    bake = Bake(roster=roster(2))
    run = _ready(bake)
    bake.tables.ambiguous_next = True

    first = bake.finalize(run)

    assert first.node_state is NodeState.SUCCEEDED
    assert bake.run_item(run)["state"]["S"] == "SUCCEEDED"


# --- the armed finalizer's shortened redelivery (convergence; graph_materialize's mechanism) ----

_BAKE_QUEUE_ARN = "arn:aws:sqs:us-east-1:000000000000:adlytics-fictional-preset-bake.fifo"
_BAKE_QUEUE_URL = (
    "https://sqs.us-east-1.amazonaws.com/000000000000/adlytics-fictional-preset-bake.fifo"
)
_QUEUE_VISIBILITY_SECONDS = int(BAKE_TASKS["visibility_seconds"])
_PRECOMPUTE_READY_SECONDS = 60 + 60 + int(PRECOMPUTE_CLAIM_LEASE.total_seconds())
"""The dispatcher's schedule wait and pass (60 s each) plus one precompute claim lease."""


def _receive(  # noqa: PLR0913 -- the world, its run, the receive and the record shape
    bake: Bake, run: str, receive_count: int, *, carried: bool
) -> tuple[BakeDelivery, ConsumeResult]:
    """Receive the finalizer through the Baker's record parse, as the Lambda event delivers it.

    `carried=False` is the pre-fix record shape: no receipt handle or source queue reaches the
    finalizer, so it cannot override the queue's visibility.
    """
    record = sqs_record(bake.node(run, "bake_finalize"), receive_count)
    if carried:
        record |= {"receiptHandle": f"receipt-{receive_count}", "eventSourceARN": _BAKE_QUEUE_ARN}
    body = parse_bake_v3_body(record["body"])
    assert body is not None
    delivery = baker_module._delivery(record, body)  # pyright: ignore[reportPrivateUsage]
    assert delivery is not None
    return delivery, bake.graph.consume_v3(delivery, bake.remaining_ms)


def _wait_before_next_receive(bake: Bake, delivery: BakeDelivery) -> int:
    """SQS: an unacknowledged message returns after its last override, else the queue's 960 s."""
    applied = [
        seconds
        for url, handle, seconds in bake.visibility_calls
        if (url, handle) == (delivery.queue_url, delivery.receipt_handle)
    ]
    return applied[-1] if applied else _QUEUE_VISIBILITY_SECONDS


def _within_shortened_window(wait: int) -> bool:
    """Gate a wait: long enough for the armed precompute to contribute, below the queue's."""
    return _PRECOMPUTE_READY_SECONDS <= wait < _QUEUE_VISIBILITY_SECONDS


def _armed(bake: Bake) -> str:
    run = _ready(bake)
    bake.arm()
    bake.dispatch(run)
    return run


def _contribute(bake: Bake, run: str) -> None:
    stored = release_store(bake.tables).read_precompute_input(run)
    assert stored is not None
    precompute_contributed(bake.tables, run, stored.digest, bake.roster.keys)


def test_the_armed_finalizer_is_redelivered_within_the_shortened_window_and_finalizes() -> None:
    bake = Bake(roster=roster(3))
    run = _armed(bake)

    first_delivery, first = _receive(bake, run, 1, carried=True)

    assert (first.acknowledge, first.outcome) == (False, PRECOMPUTE_ARMED)
    assert first_delivery.queue_url == _BAKE_QUEUE_URL
    assert bake.visibility_calls == [(_BAKE_QUEUE_URL, "receipt-1", 180)]
    wait = _wait_before_next_receive(bake, first_delivery)
    assert wait == FINALIZE_ARMED_REDELIVERY_SECONDS
    assert _within_shortened_window(wait)
    _contribute(bake, run)
    second_delivery, second = _receive(bake, run, 2, carried=True)
    assert (second.acknowledge, second.node_state) == (True, NodeState.SUCCEEDED)
    assert not second_delivery.final  # the receive budget: one receive is still left
    assert bake.run_item(run)["state"]["S"] == "SUCCEEDED"
    assert len(bake.visibility_calls) == 1  # the acknowledged receive overrides nothing

    # Paired control, the pre-fix behaviour: the record carries no receipt handle or queue, so
    # the armed nack keeps the queue's visibility and the gate refuses the 960 s wait.
    control = Bake(roster=roster(3))
    control_run = _armed(control)
    control_delivery, control_first = _receive(control, control_run, 1, carried=False)
    assert control_first.outcome == PRECOMPUTE_ARMED
    assert control.visibility_calls == []
    control_wait = _wait_before_next_receive(control, control_delivery)
    assert control_wait == _QUEUE_VISIBILITY_SECONDS
    assert not _within_shortened_window(control_wait)


def test_only_the_armed_nack_is_shortened_so_a_blocked_retry_keeps_the_full_cycle() -> None:
    bake = Bake(roster=roster(3))
    run = _armed(bake)
    armed_delivery, _ = _receive(bake, run, 1, carried=True)
    assert _within_shortened_window(_wait_before_next_receive(bake, armed_delivery))

    # The precompute has not contributed by the shortened redelivery: blocked, no override.
    blocked_delivery, blocked = _receive(bake, run, 2, carried=True)

    assert (blocked.acknowledge, blocked.outcome) == (False, "graph_bake_finalize_blocked")
    assert _wait_before_next_receive(bake, blocked_delivery) == _QUEUE_VISIBILITY_SECONDS
    assert len(bake.visibility_calls) == 1
    # Control: the final receive, a full cycle later, still finalizes once precompute lands.
    _contribute(bake, run)
    final_delivery, final = _receive(bake, run, 3, carried=True)
    assert final_delivery.final
    assert (final.acknowledge, final.node_state) == (True, NodeState.SUCCEEDED)


@dataclass
class _RecordingLogger:
    lines: list[tuple[str, dict[str, object]]] = field(
        default_factory=list[tuple[str, dict[str, object]]]
    )

    def info(self, _message: str, *, extra: dict[str, object]) -> None:
        self.lines.append(("INFO", extra))

    def warning(self, _message: str, *, extra: dict[str, object]) -> None:
        self.lines.append(("WARNING", extra))

    def error(self, _message: str, *, extra: dict[str, object]) -> None:
        self.lines.append(("ERROR", extra))


def test_a_failed_override_keeps_the_armed_nack_and_is_logged_warning_with_its_cause() -> None:
    bake = Bake(roster=roster(3))
    logger = _RecordingLogger()
    bake.runtime.logger = cast("Any", logger)

    def unavailable(_url: str, _handle: str, _seconds: int) -> None:
        raise ConnectionError("fictional SQS outage")

    bake.graph = replace(bake.graph, visibility=unavailable)
    run = _armed(bake)

    delivery, armed = _receive(bake, run, 1, carried=True)

    assert (armed.acknowledge, armed.outcome, armed.node_state) == (
        False,
        PRECOMPUTE_ARMED,
        NodeState.CLAIMED,
    )
    assert _wait_before_next_receive(bake, delivery) == _QUEUE_VISIBILITY_SECONDS
    outcomes = [(level, extra["outcome"], extra.get("cause")) for level, extra in logger.lines]
    assert ("WARNING", "graph_bake_finalize_redelivery_unshortened", "ConnectionError") in outcomes
    # Control: the working override logs the redelivery at INFO with its seconds.
    working = Bake(roster=roster(3))
    working_logger = _RecordingLogger()
    working.runtime.logger = cast("Any", working_logger)
    _receive(working, _armed(working), 1, carried=True)
    applied = [
        extra
        for level, extra in working_logger.lines
        if level == "INFO" and extra["outcome"] == "graph_bake_finalize_redelivery"
    ]
    assert [extra["visibility_seconds"] for extra in applied] == [180]


def test_a_malformed_event_source_arn_is_refused_before_the_finalizer() -> None:
    bake = Bake(roster=roster(2))
    run = _armed(bake)
    record = sqs_record(bake.node(run, "bake_finalize"))
    body = parse_bake_v3_body(record["body"])
    assert body is not None

    malformed: dict[str, object] = {
        **record,
        "receiptHandle": "receipt-1",
        "eventSourceARN": "arn:aws:sns:x",
    }
    assert baker_module._delivery(malformed, body) is None  # pyright: ignore[reportPrivateUsage]
    # Control: the well-formed source names the bake queue.
    valid: dict[str, object] = {
        **record,
        "receiptHandle": "receipt-1",
        "eventSourceARN": _BAKE_QUEUE_ARN,
    }
    parsed = baker_module._delivery(valid, body)  # pyright: ignore[reportPrivateUsage]
    assert parsed is not None
    assert (parsed.queue_url, parsed.receipt_handle) == (_BAKE_QUEUE_URL, "receipt-1")


def test_the_member_count_function_is_the_committed_shape() -> None:
    bake = Bake(roster=roster(3))
    run = _ready(bake)
    request = _final_request(bake, run)
    assert final_transaction_member_count(request.effects) == 10 + 4 * 3
    assert {effect.effect_kind for effect in request.effects} == {BakeEffectKind.WRITTEN}


# --- ADR-139: CURRENT moves only with a binding presentation accepts; withheld is signalled ----

_EXHAUSTED = "precompute_budget_exhausted"


@dataclass
class _Signals:
    """The `GraphServingWithheld` datapoints and lifecycle lines one Bake world emitted."""

    bake: Bake
    values: list[int] = field(default_factory=list[int])
    logger: _RecordingLogger = field(default_factory=_RecordingLogger)

    def __post_init__(self) -> None:
        self.bake.runtime.serving_signal = self.values.append
        self.bake.runtime.logger = cast("Any", self.logger)

    def lines(self, outcome: str) -> list[dict[str, object]]:
        return [
            extra
            for level, extra in self.logger.lines
            if level == "WARNING" and extra["outcome"] == outcome
        ]


def _contribute_with(bake: Bake, run: str, reason: str | None) -> None:
    """Record the precompute contribution, each roster key carrying `reason` when given."""
    from domain.operations.execution_graph_release import (  # noqa: PLC0415
        CohortContribution,
        CohortMember,
        CohortOutcome,
    )

    store = release_store(bake.tables)
    stored = store.read_precompute_input(run)
    record = store.read_run(run)
    assert stored is not None and record is not None and record.grant_id is not None
    if reason is None:
        precompute_contributed(bake.tables, run, stored.digest, bake.roster.keys)
        return
    store.record_contribution(
        CohortContribution(
            record.grant_id,
            CohortMember.PRECOMPUTE,
            CohortOutcome.UNAVAILABLE,
            None,
            NOW,
            record.root_flow_id,
            tuple((f"precompute#{key}", reason) for key in bake.roster.keys),
            input_effect_roster_digest=stored.digest,
            request_roster=tuple((key, "requested") for key in bake.roster.keys),
        )
    )


def _armed_with(bake: Bake, reason: str | None) -> str:
    """Return a winner whose finalizer armed precompute, then received its contribution."""
    run = _ready(bake)
    bake.arm()
    bake.dispatch(run)
    armed = bake.deliver(run, "bake_finalize", 1)
    assert armed.outcome == PRECOMPUTE_ARMED
    _contribute_with(bake, run, reason)
    return run


def _final_commit(bake: Bake, since: int) -> list[dict[str, Any]]:
    """Return the one final transaction from `since` on: the one that completes the run."""
    return next(
        members for members in bake.tables.transactions[since:] if "serving_outcome" in str(members)
    )


def _lose_final_response(bake: Bake, monkeypatch: pytest.MonkeyPatch) -> None:
    """Commit the final transaction, then raise as if its response were lost."""
    original = bake.tables.transact_write_items

    def lose(**request: Any) -> dict[str, Any]:
        if "serving_outcome" in str(request["TransactItems"]):
            bake.tables.ambiguous_next = True
        return original(**request)

    monkeypatch.setattr(bake.tables, "transact_write_items", lose)


def _current_pointers(bake: Bake) -> dict[str, object]:
    return {key: bake.tables.page_pointer(*key.split("#")) for key in bake.roster.keys}


@pytest.mark.covers("ADR-139")
def test_a_withheld_cohort_completes_moves_no_page_and_signals_withheld_once() -> None:
    bake = Bake(roster=roster(3))
    signals = _Signals(bake)
    run = _armed_with(bake, _EXHAUSTED)
    before_pointers = _current_pointers(bake)
    before = len(bake.tables.transactions)

    result = bake.deliver(run, "bake_finalize", 2)

    assert (result.acknowledge, result.outcome) == (True, "graph_bake_finalized")
    final = _final_commit(bake, before)
    assert len(final) == 10 + 3  # N effect conditions, zero pointer members
    assert _current_pointers(bake) == before_pointers
    item = bake.run_item(run)
    assert (item["state"]["S"], item["serving_outcome"]["S"]) == ("SUCCEEDED", "partial")
    binding = release_store(bake.tables).read_serving_binding(item["grant_id"]["S"])
    assert binding is not None and binding.artifact_revision is not None  # stored for audit
    assert signals.values == [1]
    (line,) = signals.lines("serving_withheld")
    assert {
        name: line[name]
        for name in (
            "run_id",
            "failure_reasons",
            "unavailable_count",
            "binding_kind",
            "execution_attempts",
            "stale",
        )
    } == {
        "run_id": run,
        "failure_reasons": [_EXHAUSTED],
        "unavailable_count": 3,
        "binding_kind": "direct",
        "execution_attempts": 1,
        "stale": False,
    }

    # Control: the same cohort with only empty insight slots writes its pointers and clears.
    control = Bake(roster=roster(3))
    control_signals = _Signals(control)
    control_run = _armed_with(control, "insight_slot_unavailable")
    control_before = len(control.tables.transactions)
    assert control.deliver(control_run, "bake_finalize", 2).outcome == "graph_bake_finalized"
    assert len(_final_commit(control, control_before)) == 10 + 4 * 3
    assert control_signals.values == [0]
    assert control_signals.lines("serving_withheld") == []


def _crash_once(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Make the first `_signal_serving` call die after the commit, as a killed Lambda would."""
    original = graph_bake._signal_serving  # pyright: ignore[reportPrivateUsage]
    calls: list[int] = []

    def crashing(*args: Any, **kwargs: Any) -> None:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("fictional Lambda timeout after the commit")
        original(*args, **kwargs)

    monkeypatch.setattr(graph_bake, "_signal_serving", crashing)
    return calls


@pytest.mark.covers("ADR-139")
def test_m3_a_crash_after_commit_is_repaired_by_the_settled_redelivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bake = Bake(roster=roster(3))
    signals = _Signals(bake)
    run = _armed_with(bake, _EXHAUSTED)
    _crash_once(monkeypatch)

    with pytest.raises(RuntimeError, match="after the commit"):
        bake.deliver(run, "bake_finalize", 2)  # unacknowledged: the message is redelivered
    assert bake.run_item(run)["state"]["S"] == "SUCCEEDED"
    assert signals.values == []

    redelivered = bake.deliver(run, "bake_finalize", 3)

    # The redelivery takes the claim path: the committed finalizer node is terminal.
    assert (redelivered.acknowledge, redelivered.outcome) == (True, "graph_already_settled")
    assert signals.values == [1]
    assert len(signals.lines("serving_withheld")) == 1


@pytest.mark.covers("ADR-139")
def test_m3_a_node_settled_between_claim_and_read_also_resignals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bake = Bake(roster=roster(3))
    signals = _Signals(bake)
    run = _armed_with(bake, _EXHAUSTED)
    _crash_once(monkeypatch)
    with pytest.raises(RuntimeError):
        bake.deliver(run, "bake_finalize", 2)
    from ports.outbound.execution_graph_dispatch import ClaimRecord  # noqa: PLC0415

    node = bake.node(run, "bake_finalize")
    claimed = ClaimRecord(node.node_id, run, 9, "2099-01-01T00:00:00+00:00", "d", "c")

    def claim(_ports: object, _delivery: object) -> tuple[ClaimRecord, ConsumeResult]:
        return claimed, ConsumeResult(True, "claimed")

    monkeypatch.setattr(graph_bake, "_claim", claim)

    settled = bake.deliver(run, "bake_finalize", 3)

    assert (settled.acknowledge, settled.outcome, settled.node_state) == (
        True,
        "graph_already_settled",
        NodeState.SUCCEEDED,
    )
    assert signals.values == [1]


@pytest.mark.covers("ADR-139")
def test_m3_an_already_finalized_withheld_commit_signals_and_a_non_withheld_replay_never_clears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bake = Bake(roster=roster(3))
    signals = _Signals(bake)
    run = _armed_with(bake, _EXHAUSTED)
    _lose_final_response(bake, monkeypatch)  # the post-image is exact: already finalized

    lost = bake.deliver(run, "bake_finalize", 2)

    assert lost.outcome == "graph_bake_already_finalized"
    assert signals.values == [1]
    # Control: a non-withheld cohort's ALREADY_FINALIZED and settled redelivery emit no 0,
    # because only a fresh FINALIZED commit is known to be the newest bound cohort.
    control = Bake(roster=roster(3))
    control_signals = _Signals(control)
    control_run = _armed_with(control, None)
    _lose_final_response(control, monkeypatch)
    assert control.deliver(control_run, "bake_finalize", 2).outcome == (
        "graph_bake_already_finalized"
    )
    assert control.deliver(control_run, "bake_finalize", 3).outcome == "graph_already_settled"
    assert control_signals.values == []


@pytest.mark.covers("ADR-139")
def test_m3_a_failed_reread_after_the_crash_is_retried_not_acknowledged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bake = Bake(roster=roster(3))
    signals = _Signals(bake)
    run = _armed_with(bake, _EXHAUSTED)
    _crash_once(monkeypatch)
    with pytest.raises(RuntimeError):
        bake.deliver(run, "bake_finalize", 2)
    store_type = type(bake.graph.store)
    original = store_type.read_serving_binding
    failures = [DependencyError(ErrorCode("GRAPH_STORE", 1), "fictional read outage")]

    def unreadable_once(self: Any, grant_id: str) -> Any:
        if failures:
            raise failures.pop()
        return original(self, grant_id)

    monkeypatch.setattr(store_type, "read_serving_binding", unreadable_once)

    unread = bake.deliver(run, "bake_finalize", 3)

    assert (unread.acknowledge, unread.outcome) == (False, "graph_bake_signal_retryable")
    assert unread.node_state is NodeState.SUCCEEDED
    assert len(signals.lines("serving_signal_unread")) == 1
    assert signals.values == []
    # The next redelivery reads the stored binding and emits the withheld signal.
    assert bake.deliver(run, "bake_finalize", 4).outcome == "graph_already_settled"
    assert signals.values == [1]


@pytest.mark.covers("ADR-139")
def test_m3_a_missing_binding_after_the_crash_is_retried_not_acknowledged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bake = Bake(roster=roster(3))
    signals = _Signals(bake)
    run = _armed_with(bake, _EXHAUSTED)
    _crash_once(monkeypatch)
    with pytest.raises(RuntimeError):
        bake.deliver(run, "bake_finalize", 2)
    store_type = type(bake.graph.store)
    original = store_type.read_serving_binding
    missing = [None]

    def missing_once(self: Any, grant_id: str) -> Any:
        if missing:
            return missing.pop()
        return original(self, grant_id)

    monkeypatch.setattr(store_type, "read_serving_binding", missing_once)

    unread = bake.deliver(run, "bake_finalize", 3)

    assert (unread.acknowledge, unread.outcome) == (False, "graph_bake_signal_retryable")
    assert unread.node_state is NodeState.SUCCEEDED
    assert [line["cause"] for line in signals.lines("serving_signal_unread")] == ["binding_missing"]
    assert signals.values == []
    # Once the committed binding reads back, the redelivery emits the withheld signal.
    assert bake.deliver(run, "bake_finalize", 4).outcome == "graph_already_settled"
    assert signals.values == [1]


@pytest.mark.covers("ADR-139")
def test_r1_an_all_designed_absence_cohort_is_not_withheld_and_clears() -> None:
    bake = Bake(roster=roster(3))
    signals = _Signals(bake)
    bake.seed_predecessors()
    run = bake.granted()
    bake.sources.unavailable.update(bake.roster.keys)
    bake.fan_out(run)
    bake.all_tasks(run)
    before_pointers = _current_pointers(bake)
    before = len(bake.tables.transactions)

    result = bake.finalize(run)

    assert (result.acknowledge, result.outcome) == (True, "graph_bake_finalized")
    assert len(_final_commit(bake, before)) == 10 + 3
    assert _current_pointers(bake) == before_pointers
    binding = release_store(bake.tables).read_serving_binding(bake.run_item(run)["grant_id"]["S"])
    assert binding is not None and binding.artifact_revision is None
    assert signals.values == [0]
    assert signals.lines("serving_withheld") == []
    # Its settled redelivery emits nothing.
    assert bake.deliver(run, "bake_finalize", 9).outcome == "graph_already_settled"
    assert signals.values == [0]


@pytest.mark.covers("ADR-139")
def test_the_store_refuses_pointer_members_under_a_binding_that_does_not_authorize_serving() -> (
    None
):
    bake = Bake(roster=roster(3))
    run = _ready(bake)
    request = _final_request(bake, run)
    assert request.pointer_items
    withheld = replace(
        request,
        binding=replace(request.binding, unavailable=(("precompute#x", _EXHAUSTED),)),
    )
    before = len(bake.tables.transactions)

    with pytest.raises(InvariantError) as refused:
        release_store(bake.tables).finalize_bake(withheld)

    assert refused.value.code == ErrorCode("GRAPH_BAKE_V3", 3)
    assert len(bake.tables.transactions) == before
    # Control: the same withheld binding with zero pointer members passes the shape check; the
    # refusal it then meets is the transaction's own guard, not GRAPH_BAKE_V3/3.
    with pytest.raises(GraphGuardError):
        release_store(bake.tables).finalize_bake(replace(withheld, pointer_items=()))


# --- ADR-140 C1: bake eligibility is decided for the cohort ----------------------------------


def _cohort(bake: Bake, loser_curation: int, winner_curation: int) -> tuple[str, str]:
    """Publish a loser, then a winner (the later grant), and dispatch the winner's fan-out."""
    from graph_consumer_support import arm_and_dispatch, publish_settled  # noqa: PLC0415

    from domain.operations.status import canonical_authority_digest  # noqa: PLC0415

    loser, winner = (
        publish_settled(
            bake.tables,
            advanced_curation=curation,
            unit=canonical_authority_digest(["fictional-cohort", name]),
            fingerprint=name * 64,
            request_id=f"request-{name}",
        )
        for name, curation in (("a", loser_curation), ("b", winner_curation))
    )
    arm_and_dispatch(bake.tables, winner, "bake")
    return loser, winner


def _bake_member(bake: Bake, run: str) -> Any:
    from domain.operations.execution_graph_release import CohortMember  # noqa: PLC0415

    grant = bake.run_item(run)["grant_id"]["S"]
    return next(
        (
            item
            for item in release_store(bake.tables).read_contributions(grant)
            if item.member is CohortMember.BAKE
        ),
        None,
    )


@pytest.mark.covers("ADR-140")
def test_c1_a_curated_loser_makes_a_zero_curation_winner_fan_out() -> None:
    bake = Bake(roster=roster(2))
    _loser, winner = _cohort(bake, 1, 0)

    result = bake.fan_out(winner)

    assert (result.outcome, result.node_state) == ("graph_bake_succeeded", NodeState.SUCCEEDED)
    assert _keys(bake, winner, "NODE#bake_task:") != []
    assert _bake_member(bake, winner) is None  # the finalizer contributes, not the fan-out
    # Control: a cohort with no curation anywhere is U1 (NO_ELIGIBLE, no fan-out).
    control = Bake(roster=roster(2))
    _quiet, alone = _cohort(control, 0, 0)
    assert control.fan_out(alone).node_state is NodeState.SUCCEEDED
    assert _keys(control, alone, "NODE#bake_task:") == []
    assert _bake_member(control, alone).outcome.value == "no_eligible"


@pytest.mark.covers("ADR-140")
def test_c1_an_unverifiable_member_effect_is_a_consumer_invariant_u2() -> None:
    bake = Bake(roster=roster(2))
    loser, winner = _cohort(bake, 1, 0)
    bake.tables.items[(f"RUN#{loser}", "NODE#publish")]["effect_digest"] = {"S": "tampered"}

    result = bake.fan_out(winner)

    assert result.node_state is NodeState.FAILED
    member = _bake_member(bake, winner)
    assert member.outcome.value == "unavailable"
    assert {reason for _key, reason in member.unavailable} == {"post_grant_invariant"}
    assert bake.node(winner, "bake").failure_reason == "post_grant_invariant"
    assert _keys(bake, winner, "NODE#bake_task:") == []


@pytest.mark.covers("ADR-140")
def test_c1_a_curated_winner_reads_no_other_member() -> None:
    bake = Bake(roster=roster(2))
    loser, winner = _cohort(bake, 0, 1)
    reads: list[str] = []
    original = bake.tables.get_item

    def recording(**kwargs: Any) -> dict[str, Any]:
        reads.append(str(kwargs["Key"]["pk"]["S"]))
        return original(**kwargs)

    bake.tables.get_item = recording  # type: ignore[method-assign]

    assert bake.fan_out(winner).node_state is NodeState.SUCCEEDED
    assert f"RUN#{winner}" in reads
    assert f"RUN#{loser}" not in reads


# --- D2 hardening: a pin-time refusal pins one row refused (F2, §1.1) ------------------------


def test_h2_a_pin_refused_row_pins_zero_requests_and_its_task_re_refuses_with_zero_queries() -> (
    None
):
    bake = Bake(roster=roster(2))
    run = bake.granted()
    refused, control = bake.roster.keys
    bake.sources.prefetch_raises[refused] = DomainError(
        ErrorCode("FICTIONAL", 31), "a suppressed prefetch"
    )

    assert bake.fan_out(run).node_state is NodeState.SUCCEEDED

    pin = release_store(bake.tables).read_durable_bake_sources(run)
    assert pin is not None
    assert (pin.manifest.task(refused).plan_count, pin.manifest.task(control).plan_count) == (0, 2)
    bake.dispatch(run)
    prefetched = len(bake.sources.prefetched)
    result = bake.task(run, refused)
    assert (result.outcome, result.node_state) == (
        "graph_bake_task_unavailable",
        NodeState.SUCCEEDED,
    )
    assert bake.sources.prefetched[prefetched:] == [refused]  # the plan replays the prefetch
    assert refused not in bake.queries.calls
    effect, _extra = graph_bake.read_task_effect(bake.node(run, task_node_name(refused)))
    assert (effect.effect_kind, effect.reason_code) == (
        BakeEffectKind.UNAVAILABLE,
        "campaigns_materialization_unavailable",
    )
    # Control: the sibling's pinned plan is unchanged; it runs its two queries and is written.
    written = bake.task(run, control)
    assert (written.outcome, written.node_state) == ("graph_bake_task_written", NodeState.SUCCEEDED)
    assert bake.queries.calls == [control] * 2


def test_h2_control_a_pin_time_dependency_error_is_retried_then_pins_the_row_refused() -> None:
    """ADR-142 §3.3: a row's throttle no longer withholds the cohort (test_graph_bake_pin)."""
    bake = Bake(roster=roster(2))
    run = bake.granted()
    refused, control = bake.roster.keys[1], bake.roster.keys[0]
    bake.sources.prefetch_raises[refused] = DependencyError(
        ErrorCode("FICTIONAL", 32), "a throttled CURRENT read"
    )
    result = bake.fan_out(run)
    assert (result.acknowledge, result.node_state) == (True, NodeState.SUCCEEDED)
    pin = release_store(bake.tables).read_durable_bake_sources(run)
    assert pin is not None
    assert pin.manifest.task(refused).pin_refusal is not None
    assert pin.manifest.task(control).pin_refusal is None


def test_h2_replay_of_87067199_a_bake_pass_plan_refusal_pins_siblings_and_redelivery_adopts() -> (
    None
):
    """Live replay (cohort 87067199): one row's PLAN refusal at pin, then a redelivery.

    Live, receive 1 refused one reach row with ADLY-PLAN-0008, wrote no pin and retried;
    receive 2 then raised V3-0010 `durable_bake_source_pin_absent`. With F2 the refusal pins
    that row refused, the siblings stay pinned, and a redelivery adopts the pin.
    """
    bake = Bake(roster=roster(3))
    run = bake.granted()
    refused, *siblings = bake.roster.keys
    bake.sources.raises[refused] = DomainError(
        ErrorCode("PLAN", 8), "Campaign names can't be read in this runtime"
    )
    assert bake.fan_out(run).node_state is NodeState.SUCCEEDED
    pin = release_store(bake.tables).read_durable_bake_sources(run)
    assert pin is not None
    assert pin.manifest.task(refused).plan_count == 0
    assert all(pin.manifest.task(key).plan_count == 2 for key in siblings)
    selected = bake.current.current_calls
    replay = bake.fan_out(run, receive_count=2)
    # The redelivery is acknowledged as settled, never the V3-0010 FAILED refusal.
    assert (replay.acknowledge, replay.outcome) == (True, "graph_already_settled")
    assert bake.current.current_calls == selected
    bake.dispatch(run)
    result = bake.task(run, refused)
    assert (result.outcome, result.node_state) == (
        "graph_bake_task_unavailable",
        NodeState.SUCCEEDED,
    )
    assert refused not in bake.queries.calls
    effect, _extra = graph_bake.read_task_effect(bake.node(run, task_node_name(refused)))
    assert (effect.effect_kind, effect.reason_code) == (
        BakeEffectKind.UNAVAILABLE,
        "campaigns_materialization_unavailable",
    )


# --- D2 hardening: the finalizer settles withheld on its own error (F5, §2.2) ----------------

_FINAL = int(ROUTES["bake"]["max_receive_count"])


def _bake_contribution(bake: Bake, run: str) -> Any:
    from domain.operations.execution_graph_release import CohortMember  # noqa: PLC0415

    grant = bake.run_item(run)["grant_id"]["S"]
    return next(
        (
            item
            for item in release_store(bake.tables).read_contributions(grant)
            if item.member is CohortMember.BAKE
        ),
        None,
    )


def _binding_of(bake: Bake, run: str) -> Any:
    return release_store(bake.tables).read_serving_binding(bake.run_item(run)["grant_id"]["S"])


def _invariant(minor: int) -> InvariantError:
    return InvariantError(ErrorCode("GRAPH_BAKE_V3", minor), "a fictional finalizer invariant")


def _swap_store(bake: Bake, **methods: Any) -> Any:
    store = release_store(bake.tables)
    for name, method in methods.items():
        object.__setattr__(store, name, method)
    object.__setattr__(bake.graph, "store", store)
    return store


@pytest.mark.covers("ADR-140")
def test_h5a_a_pre_arm_finalizer_invariant_on_the_final_receive_abandons_and_settles_u3(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import replace as replaced  # noqa: PLC0415

    from execution_graph_release_support import context  # noqa: PLC0415

    from adapters.jobs.execution_graph.fence_settler import due_shards  # noqa: PLC0415
    from adapters.jobs.execution_graph.release import release_shard  # noqa: PLC0415
    from domain.operations.execution_graph_release import (  # noqa: PLC0415
        CohortOutcome,
        serving_withheld,
    )

    bake = Bake(roster=roster(3))
    signals = _Signals(bake)
    run = _ready(bake)
    bake.arm()
    bake.dispatch(run)

    def raising(*_args: object) -> None:
        raise _invariant(4)

    monkeypatch.setattr(graph_bake, "_precompute_input", raising)
    # Control: an earlier receive retries with no write.
    retried = bake.deliver(run, "bake_finalize", 1)
    assert (retried.acknowledge, retried.outcome) == (False, "graph_bake_finalize_retryable")
    assert _bake_contribution(bake, run) is None
    _expire_claim(bake, run)
    since = len(bake.tables.transactions)

    abandoned = bake.deliver(run, "bake_finalize", _FINAL)

    assert (abandoned.acknowledge, abandoned.outcome, abandoned.node_state) == (
        True,
        "graph_bake_finalize_abandoned",
        NodeState.FAILED,
    )
    bake_member = _bake_contribution(bake, run)
    assert bake_member is not None and bake_member.outcome is CohortOutcome.UNAVAILABLE
    assert {reason for _key, reason in bake_member.unavailable} == {"post_grant_invariant"}
    assert bake.node(run, "precompute").state is NodeState.WAITING_INPUT
    assert bake.tables.pointer_writes(since) == []
    (line,) = signals.lines("abandoned")
    assert (line["cause"], line["failure_code"]) == ("finalize_error", "ADLY-GRAPH_BAKE_V3-0004")
    release_signals: list[int] = []
    released = replaced(context(bake.tables), serving_signal=release_signals.append)
    for shard in due_shards():
        release_shard(released, shard)
    assert bake.node(run, "precompute").state is NodeState.SUCCEEDED
    binding = _binding_of(bake, run)
    assert binding is not None and serving_withheld(binding)
    assert bake.run_item(run)["state"]["S"] == "SUCCEEDED"
    assert release_signals == [1]


@pytest.mark.covers("ADR-140")
def test_h5b_a_final_transaction_invariant_after_the_contribution_binds_stale_and_withholds() -> (
    None
):
    from domain.operations.execution_graph_release import (  # noqa: PLC0415
        CohortOutcome,
        serving_withheld,
    )

    bake = Bake(roster=roster(3))
    signals = _Signals(bake)
    run = _armed_with(bake, None)

    def failing(_request: object) -> None:
        raise _invariant(3)

    _swap_store(bake, finalize_bake=failing)
    pointers = _current_pointers(bake)
    # Control: an earlier receive is retryable and binds nothing.
    retried = bake.deliver(run, "bake_finalize", 2)
    assert (retried.acknowledge, retried.outcome) == (False, "graph_bake_finalize_retryable")
    stored = _bake_contribution(bake, run)
    assert stored is not None and stored.outcome is CohortOutcome.SUCCEEDED
    assert _binding_of(bake, run) is None
    _expire_claim(bake, run)
    since = len(bake.tables.transactions)

    abandoned = bake.deliver(run, "bake_finalize", _FINAL)

    assert (abandoned.outcome, abandoned.node_state) == (
        "graph_bake_finalize_abandoned",
        NodeState.FAILED,
    )
    assert _bake_contribution(bake, run) == stored  # contributions are write-once
    binding = _binding_of(bake, run)
    assert binding is not None and binding.stale
    assert not binding_authorizes_serving(binding) and serving_withheld(binding)
    assert signals.values == [1]
    assert bake.tables.pointer_writes(since) == []
    assert _current_pointers(bake) == pointers


def _authorizing_bind(bake: Bake, *, first: bool) -> None:
    """Make precompute's authorizing binding win: stored before the final receive, or raced."""
    store = release_store(bake.tables)
    real_bind = store.bind_serving

    def failing(request: Any) -> None:
        if first:
            real_bind(request.binding)  # precompute bound the real cohort before this receive
        raise _invariant(3)

    def racing(binding: Any) -> None:
        real_bind(replace(binding, stale=False))  # precompute's authorizing bind lands first
        real_bind(binding)

    _swap_store(bake, finalize_bake=failing, **({} if first else {"bind_serving": racing}))


@pytest.mark.covers("ADR-140")
@pytest.mark.parametrize("order", ["stored", "raced"])
def test_h5d_an_authorizing_binding_is_never_abandoned_and_the_receive_retries(order: str) -> None:
    bake = Bake(roster=roster(3))
    signals = _Signals(bake)
    run = _armed_with(bake, None)
    _authorizing_bind(bake, first=order == "stored")
    since = len(bake.tables.pointer_writes())

    refused = bake.deliver(run, "bake_finalize", _FINAL)

    assert (refused.acknowledge, refused.outcome, refused.node_state) == (
        False,
        "graph_bake_finalize_retryable",
        NodeState.CLAIMED,
    )
    assert bake.node(run, "bake_finalize").state not in {NodeState.FAILED, NodeState.SUCCEEDED}
    binding = _binding_of(bake, run)
    assert binding is not None and binding_authorizes_serving(binding)
    assert len(bake.tables.pointer_writes()) == since
    assert bake.run_item(run)["state"]["S"] == "GRANTED"
    (line,) = signals.lines("abandon_refused")
    assert (line["cause"], line["reason"]) == ("finalize_error", "authorizing_binding")


# --- BSORT-DEV-1: a revision the pointer history already holds (ADR-140 amendment) -----------


def _finalize_refusals(
    monkeypatch: pytest.MonkeyPatch, *, race: dict[str, Any] | None = None
) -> list[tuple[type[GraphGuardError], str]]:
    """Record every guard the store raises; with `race`, move the live head inside each call.

    The head moves after this receive's reads (its own head check passed) and is restored, so
    every receive reads the original snapshot and loses it again in its one transaction.
    """
    original = release_adapter.DynamoGraphReleaseStore.finalize_bake
    frozen = dict(race["elected_candidate_digest"]) if race is not None else None
    refusals: list[tuple[type[GraphGuardError], str]] = []

    def wrapped(self: Any, request: Any) -> BakeFinalizeOutcome:
        if race is not None:
            race["elected_candidate_digest"] = {"S": "f" * 64}
        try:
            return original(self, request)
        except GraphGuardError as refused:
            refusals.append((type(refused), refused.signal))
            raise
        finally:
            if race is not None:
                race["elected_candidate_digest"] = frozen

    monkeypatch.setattr(release_adapter.DynamoGraphReleaseStore, "finalize_bake", wrapped)
    return refusals


def _candidate(bake: Bake, run: str, key: str) -> dict[str, Any]:
    """Return a task's stored CURRENT-envelope pointer candidate item."""
    candidate = cast("dict[str, Any]", _stored(bake, run, key)["pointer"])["item"]
    assert "custody_digest" in candidate  # no case can pass on a custody-less record
    return cast("dict[str, Any]", candidate)


@pytest.mark.covers("ADR-140")
def test_bsort_dev_1_an_identical_existing_revision_abandons_on_the_final_receive_withheld() -> (
    None
):
    """Retrying a revision the history already holds never succeeds.

    Without the abandon the message dead-letters while its run holds the serving domain.
    """
    from domain.operations.execution_graph_release import serving_withheld  # noqa: PLC0415

    bake = Bake(roster=roster(3))
    signals = _Signals(bake)
    run = _armed_with(bake, None)
    revision = bake.tables.seed_history(SeedHistoryRequest(_candidate(bake, run, "campaigns#1d")))
    history_key = ("PTR|campaigns#1d", f"REVISION#{revision}")
    seeded = copy.deepcopy(bake.tables.items[history_key])
    pointers = _current_pointers(bake)
    finalizer = bake.node(run, "bake_finalize").node_id

    retried = bake.deliver(run, "bake_finalize", 2)
    assert (retried.acknowledge, retried.outcome, retried.node_state) == (
        False,
        "graph_bake_finalize_conflict",
        NodeState.CLAIMED,
    )
    _expire_claim(bake, run)
    since = len(bake.tables.transactions)

    abandoned = bake.deliver(run, "bake_finalize", _FINAL)

    assert (abandoned.acknowledge, abandoned.outcome, abandoned.node_state) == (
        True,
        "graph_bake_finalize_abandoned",
        NodeState.FAILED,
    )
    lines = signals.lines("pointer_revision_exists")
    assert [line["disposition"] for line in lines] == ["retry", "abandon"]
    assert [line["receive_count"] for line in lines] == [2, 3]
    for line in lines:
        assert (line["run_id"], line["task_id"], line["existing_count"]) == (run, finalizer, 1)
        assert line["existing"] == [{"page": "campaigns", "variant": "1d", "revision": revision}]
    (line,) = signals.lines("abandoned")
    assert (line["cause"], line["failure_code"], line["bake_outcome"], line["bound"]) == (
        "pointer_revision_exists",
        "pointer_revision_exists",
        "succeeded",
        True,
    )
    assert bake.node(run, "bake_finalize").failure_code == "pointer_revision_exists"
    binding = _binding_of(bake, run)
    assert binding is not None and binding.stale
    assert not binding_authorizes_serving(binding) and serving_withheld(binding)
    assert signals.values == [1]
    assert bake.tables.pointer_writes(since) == []
    assert _current_pointers(bake) == pointers  # every page stays on last-good
    assert bake.tables.items[history_key] == seeded


def _genuine_conflict(bake: Bake, run: str, case: str) -> dict[str, Any] | None:
    """Seed one genuine conflict; return the live head item when the case races it."""
    candidate = _candidate(bake, run, "campaigns#1d")
    seeds = {
        "different_content": SeedHistoryRequest(candidate, body_digest="c" * 64),
        "own_grant": SeedHistoryRequest(candidate, grant=bake.run_item(run)["grant_id"]["S"]),
        "custody_absent": SeedHistoryRequest(candidate, drop_custody=True),
    }
    bake.tables.seed_history(seeds.get(case, SeedHistoryRequest(candidate)))
    if case == "current_moved":  # another writer advanced CURRENT, so this CURRENT fails too
        bake.tables.items[("PTR|campaigns#1d", "CURRENT")]["forward_token"] = {"N": "99"}
    if case == "one_page_different":
        other = _candidate(bake, run, "campaigns#2d")
        bake.tables.seed_history(SeedHistoryRequest(other, body_digest="c" * 64))
    if case == "head_moved":
        # Moving the head before a receive trips the finalizer's own head check (an invariant
        # F5 abandons), so it moves inside the store call instead.
        return bake.tables.items[(f"SERVING_DOMAIN#{_active_head(bake, run).domain}", "HEAD")]
    return None


@pytest.mark.covers("ADR-140")
@pytest.mark.parametrize(
    ("case", "signal"),
    [
        ("different_content", "NODE_STATE_CHANGED"),
        ("current_moved", "NODE_STATE_CHANGED"),
        ("head_moved", "SERVING_HEAD_CHANGED"),
        ("one_page_different", "NODE_STATE_CHANGED"),
        ("own_grant", "NODE_STATE_CHANGED"),
        ("custody_absent", "NODE_STATE_CHANGED"),
    ],
)
def test_bsort_dev_1_control_a_genuine_pointer_conflict_is_refused_retried_and_never_abandoned(
    case: str, signal: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    bake = Bake(roster=roster(3))
    signals = _Signals(bake)
    run = _armed_with(bake, None)
    refusals = _finalize_refusals(monkeypatch, race=_genuine_conflict(bake, run, case))
    pointers = _current_pointers(bake)

    first = bake.deliver(run, "bake_finalize", 2)
    _expire_claim(bake, run)
    since = len(bake.tables.transactions)
    final = bake.deliver(run, "bake_finalize", _FINAL)

    assert refusals == [(GraphGuardError, signal)] * 2  # the base guard, never the subclass
    for refused in (first, final):
        assert (refused.acknowledge, refused.outcome, refused.node_state) == (
            False,
            "graph_bake_finalize_conflict",
            NodeState.CLAIMED,
        )
    assert bake.node(run, "bake_finalize").state not in {NodeState.FAILED, NodeState.SUCCEEDED}
    assert bake.run_item(run)["state"]["S"] == "GRANTED"
    assert _binding_of(bake, run) is None
    assert bake.tables.pointer_writes(since) == []
    assert _current_pointers(bake) == pointers
    assert signals.lines("pointer_revision_exists") == []
    assert signals.lines("abandoned") == []
    assert signals.values == []
    if case != "different_content":
        return
    # Paired positive control: without the seeded record the same final receive commits.
    revision = str(_candidate(bake, run, "campaigns#1d")["revision"]["S"])
    del bake.tables.items[("PTR|campaigns#1d", f"REVISION#{revision}")]
    _expire_claim(bake, run)
    since = len(bake.tables.transactions)

    committed = bake.deliver(run, "bake_finalize", _FINAL)

    assert (committed.acknowledge, committed.outcome, committed.node_state) == (
        True,
        "graph_bake_finalized",
        NodeState.SUCCEEDED,
    )
    assert any(
        "Put" in member
        and member["Put"]["Item"]["page_variant_key"] == {"S": "campaigns#1d"}
        and member["Put"]["Item"]["record_key"] == {"S": f"REVISION#{revision}"}
        for member in bake.tables.pointer_writes(since)
    )
    assert bake.run_item(run)["state"]["S"] == "SUCCEEDED"


# --- isolation on the graph entry (ADR-142, design 02 §3.3, §9) ----------------------------


def _isolating(
    monkeypatch: pytest.MonkeyPatch, rows: BakeRoster | None = None
) -> tuple[Bake, Telemetry]:
    """Return a bake world whose one emitter is captured: spans, lines and datapoints."""
    telemetry = capture(monkeypatch)
    bake = Bake(roster=rows or roster(2))
    bake.runtime.logger = LOGGER
    bake.runtime.refusal_metric = telemetry.metric
    bake.runtime.over_budget_metric = telemetry.budget
    return bake, telemetry


def _ran(bake: Bake) -> str:
    run = bake.granted()
    assert bake.fan_out(run).node_state is NodeState.SUCCEEDED
    bake.dispatch(run)
    return run


def _stored(bake: Bake, run: str, key: str) -> dict[str, object]:
    _effect, extra = graph_bake.read_task_effect(bake.node(run, task_node_name(key)))
    return extra


_ROWS_OVER = IsolatedRefusal(
    IsolatedRefusalReason.ROWS_OVER_CEILING, RefusalScope.PAGE, rows_read=10_001, max_rows=10_000
)


def _history_gaps(count: int) -> tuple[IsolatedRefusal, ...]:
    return tuple(
        IsolatedRefusal(
            IsolatedRefusalReason.HISTORY_GAP, RefusalScope.ITEM, section="campaign-audiences"
        )
        for _ in range(count)
    )


def test_a_page_refusal_settles_unavailable_and_its_sibling_is_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bake, telemetry = _isolating(monkeypatch)
    run = _ran(bake)
    refused, sibling = bake.roster.keys
    bake.sources.render_raises[refused] = _ROWS_OVER
    result = bake.task(run, refused)
    assert (result.outcome, result.node_state) == (
        "graph_bake_task_unavailable",
        NodeState.SUCCEEDED,
    )
    assert bake.task(run, sibling).outcome == "graph_bake_task_written"
    assert telemetry.triples() == [("page", "rows_over_ceiling", 1)]
    assert telemetry.datapoints == [("limit", 1)]
    (line,) = telemetry.refusal_lines()
    assert set(line.details) == {
        "entry",
        "run_id",
        "task_id",
        "page",
        "variant",
        "scope",
        "refusal_class",
        "count",
        "replayed",
        "rows_read",
        "max_rows",
        "operator_action",
        "memory_mib",
    }
    assert line.details["task_id"] == bake.node(run, task_node_name(refused)).node_id


def test_item_refusals_publish_the_body_and_the_plan_pass_notes_are_discarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bake, telemetry = _isolating(monkeypatch, roster(1, page="audiences"))
    (key,) = bake.roster.keys
    # The source notes on every bake call: the fan-out's pass, the task's plan replay and its
    # render; only the render's are collected.
    bake.sources.notes[key] = _history_gaps(3)
    run = _ran(bake)
    assert bake.task(run, key).outcome == "graph_bake_task_written"
    assert telemetry.triples() == [("item", "history_gap", 3)]
    assert _stored(bake, run, key)["isolated_refusals"] == [
        {
            "scope": "item",
            "reason": "history_gap",
            "refusal_class": "data",
            "count": 3,
            "context": {},
        }
    ]


@pytest.mark.parametrize("where", ["pin", "render"])
def test_one_refusal_is_one_emission_across_pin_and_task(
    monkeypatch: pytest.MonkeyPatch, where: str
) -> None:
    """Round-1 H4: the pin only logs; the task is the one emission site (paired: render only)."""
    bake, telemetry = _isolating(monkeypatch)
    refused, _ = bake.roster.keys
    untyped = DomainError(ErrorCode("FICTIONAL", 61), "a suppressed read")
    if where == "pin":
        bake.sources.raises[refused] = untyped
    else:
        bake.sources.render_raises[refused] = untyped
    run = _ran(bake)
    assert telemetry.spans == []  # nothing from the fan-out
    assert bake.task(run, refused).outcome == "graph_bake_task_unavailable"
    # Round-1 H5: an untyped DomainError is source_refused, one span, line and datapoint.
    assert telemetry.triples() == [("page", "source_refused", 1)]
    assert telemetry.datapoints == [("data", 1)] and len(telemetry.refusal_lines()) == 1
    assert telemetry.refusal_lines()[0].details["error_code"] == "ADLY-FICTIONAL-0061"


def test_a_bake_wait_timeout_settles_unavailable_on_receive_1_and_another_policy_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bake, telemetry = _isolating(monkeypatch)
    run = _ran(bake)
    timed_out, control = bake.roster.keys
    bake.queries.fail[timed_out] = DependencyError(
        QUERY_TIMED_OUT,
        "slow",
        context={
            "wait_policy": "bake_invocation",
            "waited_seconds": 790.0,
            "wait_limit_seconds": 790.0,
        },
    )
    result = bake.task(run, timed_out)
    assert (result.acknowledge, result.outcome) == (True, "graph_bake_task_unavailable")
    assert telemetry.triples() == [("page", "statement_time_over_invocation", 1)]
    # Paired: a timeout under another wait policy is retried, as any dependency.
    bake.queries.fail[control] = DependencyError(
        QUERY_TIMED_OUT, "slow", context={"wait_policy": "interactive"}
    )
    retried = bake.task(run, control)
    assert (retried.acknowledge, retried.node_state) == (False, NodeState.CLAIMED)
    assert len(telemetry.spans) == 1


def test_a_retryable_or_claim_lost_receive_emits_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bake, telemetry = _isolating(monkeypatch)
    run = _ran(bake)
    key, other = bake.roster.keys
    bake.sources.notes[key] = _history_gaps(1)
    bake.queries.fail[key] = DependencyError(ErrorCode("FICTIONAL", 62), "throttled")
    assert bake.task(run, key).outcome == "graph_bake_task_retryable"

    def stale(_store: Any, _request: Any) -> None:
        raise GraphGuardError(CLAIM_LEASE_STALE)

    monkeypatch.setattr(DynamoGraphReleaseStore, "settle_node", stale)
    assert bake.task(run, other).outcome == "graph_claim_lost"
    assert telemetry.spans == [] and telemetry.datapoints == []


def test_h1a_a_retryable_stage_error_emits_nothing_and_the_settling_redelivery_emits_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bake, telemetry = _isolating(monkeypatch, roster(1, page="audiences"))
    (key,) = bake.roster.keys
    bake.sources.notes[key] = _history_gaps(3)
    run = _ran(bake)
    original = bake.stage.stage
    failures: list[int] = []

    def flaky(request: Any, **window: Any) -> Any:
        if not failures:
            failures.append(1)
            raise DependencyError(ErrorCode("FICTIONAL", 63), "s3 throttled")
        return original(request, **window)

    monkeypatch.setattr(bake.stage, "stage", flaky)
    assert bake.task(run, key).outcome == "graph_bake_task_retryable"
    assert telemetry.spans == []
    expire_claim(bake.tables, bake.node(run, task_node_name(key)))
    assert bake.task(run, key, receive_count=2).outcome == "graph_bake_task_written"
    assert telemetry.triples() == [("item", "history_gap", 3)]


def test_h1b_collected_item_refusals_then_over_count_settle_failed_and_emit_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = BakeRoster((BakeRosterRow("audiences", "1d", 240, 16.6, 123, 1),))
    bake, telemetry = _isolating(monkeypatch, rows)
    (key,) = bake.roster.keys
    bake.sources.notes[key] = _history_gaps(3)
    run = _ran(bake)  # the harness render runs two distinct requests over a bound of one
    failed = bake.task(run, key)
    assert (failed.node_state, failed.failure_reason) == (
        NodeState.FAILED,
        BAKE_EXECUTIONS_OVER_CERTIFIED_BOUND,
    )
    assert telemetry.spans == [] and telemetry.datapoints == []


def _replay_world(
    monkeypatch: pytest.MonkeyPatch, *, notes: bool
) -> tuple[Bake, Telemetry, str, str]:
    """Settle one task whose settle response is lost: the write lands, then it raises."""
    bake, telemetry = _isolating(monkeypatch, roster(1, page="audiences"))
    (key,) = bake.roster.keys
    if notes:
        bake.sources.notes[key] = _history_gaps(2)
    run = _ran(bake)
    original = DynamoGraphReleaseStore.settle_node

    def lands_then_raises(store: Any, request: Any) -> None:
        original(store, request)
        raise GraphStoreUnavailableError("injected lost settle response")

    with monkeypatch.context() as patched:
        patched.setattr(DynamoGraphReleaseStore, "settle_node", lands_then_raises)
        with pytest.raises(GraphStoreUnavailableError):
            bake.task(run, key)
    assert bake.node(run, task_node_name(key)).state is NodeState.SUCCEEDED
    assert telemetry.spans == []  # a settle that raised emits nothing
    return bake, telemetry, run, key


def test_m1_a_lost_settlement_is_replayed_once_at_the_claim_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bake, telemetry, run, key = _replay_world(monkeypatch, notes=True)
    replay = bake.task(run, key, receive_count=2)
    assert (replay.acknowledge, replay.outcome) == (True, "graph_already_settled")
    assert telemetry.triples() == [("item", "history_gap", 2)]
    assert all(item["replayed"] is True for item in telemetry.spans)
    assert telemetry.refusal_lines()[0].details["replayed"] is True
    assert telemetry.datapoints == [("data", 2)]
    # Paired: a stored SUCCEEDED node with no groups replays nothing.
    quiet, quiet_telemetry, quiet_run, quiet_key = _replay_world(monkeypatch, notes=False)
    assert quiet.task(quiet_run, quiet_key, receive_count=2).outcome == "graph_already_settled"
    assert quiet_telemetry.spans == []


def test_m1_a_settled_node_found_after_the_claim_is_replayed_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bake, telemetry, run, key = _replay_world(monkeypatch, notes=True)

    # The claim races the settlement: it is granted, then the node reads settled.
    def granted(_ports: Any, _delivery: Any) -> tuple[object, ConsumeResult]:
        return object(), ConsumeResult(True, "claimed")

    monkeypatch.setattr(graph_bake, "_claim", granted)
    replay = bake.task(run, key, receive_count=2)
    assert (replay.outcome, replay.node_state) == ("graph_already_settled", NodeState.SUCCEEDED)
    assert telemetry.triples() == [("item", "history_gap", 2)]
    assert telemetry.spans[0]["replayed"] is True


def test_the_settlement_stores_its_groups_without_changing_its_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bake, _telemetry = _isolating(monkeypatch, roster(1, page="audiences"))
    (key,) = bake.roster.keys
    bake.sources.notes[key] = _history_gaps(1)
    run = _ran(bake)
    bake.task(run, key)
    node = bake.node(run, task_node_name(key))
    effect, extra = graph_bake.read_task_effect(node)  # still parses
    assert effect.effect_digest == node.effect_digest
    assert set(extra) == {
        "schema_version",
        "node",
        "effect",
        "pointer",
        "isolated_refusals",
        "over_budget",
    }
    assert extra["over_budget"] is None
    # The digest covers the effect only: the same effect without the stored groups re-hashes.
    bare = graph_bake.task_effect_record(effect, {"pointer": extra["pointer"]})
    assert graph_bake.read_task_effect(replace(node, effect_record=bare))[0] == effect


def test_a_stored_list_that_does_not_validate_is_reported_and_acknowledged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bake, telemetry, run, key = _replay_world(monkeypatch, notes=True)
    node = bake.node(run, task_node_name(key))
    parsed = json.loads(cast("str", node.effect_record))
    parsed["isolated_refusals"][0]["context"] = {"campaign_id": "fictional"}
    (item,) = [
        stored
        for (pk, _sk), stored in bake.tables.items.items()
        if pk == f"RUN#{run}" and stored.get("effect_record", {}).get("S") == node.effect_record
    ]
    item["effect_record"] = {"S": json.dumps(parsed, sort_keys=True, separators=(",", ":"))}
    replay = bake.task(run, key, receive_count=2)
    assert (replay.acknowledge, replay.outcome) == (True, "graph_already_settled")
    assert bake.invariants == [BAKE_INTENT_INVARIANT]  # the alarmed baker invariant signal
    assert telemetry.spans == []


def _restored(bake: Bake, task: tuple[str, str], record: object) -> None:
    """Overwrite a settled task node's (run, key) stored effect record with `record`."""
    run, key = task
    node = bake.node(run, task_node_name(key))
    (item,) = [
        stored
        for (pk, _sk), stored in bake.tables.items.items()
        if pk == f"RUN#{run}" and stored.get("effect_record", {}).get("S") == node.effect_record
    ]
    item["effect_record"] = {"S": json.dumps(record, sort_keys=True, separators=(",", ":"))}


@pytest.mark.parametrize("shape", ["duplicated_group", "array_record"])
def test_a_stored_record_outside_one_sorted_group_per_pair_is_the_stored_refusal_defect(
    monkeypatch: pytest.MonkeyPatch, shape: str
) -> None:
    """A duplicate would replay the alarm twice; an array record escaped the typed invariant."""
    bake, telemetry, run, key = _replay_world(monkeypatch, notes=True)
    parsed = json.loads(cast("str", bake.node(run, task_node_name(key)).effect_record))
    (group,) = parsed["isolated_refusals"]
    if shape == "duplicated_group":
        parsed["isolated_refusals"] = [group, group]
        _restored(bake, (run, key), parsed)
    else:
        _restored(bake, (run, key), [parsed])
    replay = bake.task(run, key, receive_count=2)
    assert (replay.acknowledge, replay.outcome) == (True, "graph_already_settled")
    assert bake.invariants == [BAKE_INTENT_INVARIANT]  # the alarmed baker invariant signal
    assert telemetry.spans == [] and telemetry.datapoints == []
    # Paired: the record rewritten with its one stored group replays it once (2 data items).
    control, control_telemetry, control_run, control_key = _replay_world(monkeypatch, notes=True)
    kept = json.loads(
        cast("str", control.node(control_run, task_node_name(control_key)).effect_record)
    )
    assert kept["isolated_refusals"] == [group]
    _restored(control, (control_run, control_key), kept)
    assert control.task(control_run, control_key, receive_count=2).outcome == (
        "graph_already_settled"
    )
    assert control.invariants == []
    assert control_telemetry.triples() == [("item", "history_gap", 2)]
    assert control_telemetry.datapoints == [("data", 2)]


def test_a_node_read_that_raises_on_replay_is_a_batch_item_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bake, _telemetry, run, key = _replay_world(monkeypatch, notes=True)
    original = graph_bake._node  # pyright: ignore[reportPrivateUsage]
    calls: list[int] = []

    def failing(store: Any, run_id: str, node_id: str) -> Any:
        del store, run_id, node_id
        calls.append(1)
        raise GraphStoreUnavailableError("injected node read failure")

    monkeypatch.setattr(graph_bake, "_node", failing)
    with pytest.raises(GraphStoreUnavailableError):
        bake.task(run, key, receive_count=2)
    assert calls == [1]
    monkeypatch.setattr(graph_bake, "_node", original)


def test_the_stage_write_deadline_settles_bodies_over_invocation_and_moves_no_pointer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bake, telemetry = _isolating(monkeypatch)
    run = _ran(bake)
    expired, control = bake.roster.keys

    def deadline(request: Any, *, remaining_ms: Any, tail_seconds: float) -> Any:
        del request, remaining_ms, tail_seconds
        raise IsolatedRefusal(
            IsolatedRefusalReason.BODIES_OVER_INVOCATION, RefusalScope.PAGE, written=3, bodies=9
        )

    original = bake.stage.stage

    def stage(request: Any, *, remaining_ms: Any, tail_seconds: float) -> Any:
        if request.body.variant.value == expired.split("#")[1]:
            return deadline(request, remaining_ms=remaining_ms, tail_seconds=tail_seconds)
        return original(request, remaining_ms=remaining_ms, tail_seconds=tail_seconds)

    monkeypatch.setattr(bake.stage, "stage", stage)
    result = bake.task(run, expired)
    assert result.outcome == "graph_bake_task_unavailable"
    effect, extra = graph_bake.read_task_effect(bake.node(run, task_node_name(expired)))
    assert (effect.effect_kind, extra["pointer"]) == (BakeEffectKind.UNAVAILABLE, None)
    (line,) = telemetry.refusal_lines()
    assert (line.outcome, line.details["written"], line.details["bodies"]) == (
        "bodies_over_invocation",
        3,
        9,
    )
    # Paired: a stage that finishes before its deadline publishes, with the window it was given.
    assert bake.task(run, control).outcome == "graph_bake_task_written"
    assert bake.stage.calls[-1] == (600_000, 30.0)


def test_over_budget_reports_once_on_the_settling_receive_and_leaves_the_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = BakeRoster((BakeRosterRow("campaigns", "1d", 1e-9, 16.6, 123, 26),))
    bake, telemetry = _isolating(monkeypatch, rows)
    (key,) = bake.roster.keys
    run = _ran(bake)
    assert bake.task(run, key).outcome == "graph_bake_task_written"
    assert telemetry.over_budget == [1]
    (line,) = telemetry.budget_lines()
    assert line.details["budget_seconds"] == 1e-9 and line.details["replayed"] is False
    stored = _stored(bake, run, key)["over_budget"]
    assert isinstance(stored, dict) and set(cast("dict[str, object]", stored)) == {
        "seconds",
        "budget_seconds",
    }
    # Paired: a task inside its budget reports nothing.
    within, quiet = _isolating(monkeypatch)
    within_run = _ran(within)
    within.task(within_run, within.roster.keys[0])
    assert (
        quiet.over_budget == []
        and _stored(within, within_run, within.roster.keys[0])["over_budget"] is None
    )


def test_the_task_summary_carries_the_memory_and_process_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bake, _telemetry = _isolating(monkeypatch)
    bake.runtime.process = ProcessBakeLedger(peak=iter([100.0, 140.0, 150.0]).__next__)
    lines: list[tuple[str, dict[str, object]]] = []
    original = bake.runtime.log

    def log(phase: str, outcome: str, severity: Any = LogSeverity.INFO, **details: object) -> None:
        lines.append((phase, details))
        original(phase, outcome, severity, **details)

    monkeypatch.setattr(bake.runtime, "log", log)
    run = _ran(bake)
    bake.task(run, bake.roster.keys[0])
    (summary,) = [details for phase, details in lines if phase == "task_summary"]
    assert {
        key: summary[key]
        for key in (
            "rss_before_mb",
            "process_baseline_mb",
            "process_bake_ordinal",
            "memory_peak_mb",
        )
    } == {
        "rss_before_mb": 100.0,
        "process_baseline_mb": 100.0,
        "process_bake_ordinal": 1,
        "memory_peak_mb": 140.0,
    }
    assert isinstance(summary["process_id"], str)
