# SPDX-License-Identifier: MIT-0
"""Readiness-bound GRAPH precompute and exact-roster contribution controls."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
from execution_graph_dispatch_support import NOW, SpySender, now_text
from execution_graph_release_support import release_store
from execution_graph_support import FakeDynamo
from graph_consumer_support import (
    Clock,
    arm_and_dispatch,
    envelope,
    ports,
    publish_settled,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from adapters.jobs.execution_graph.dispatcher import DispatchContext, dispatch_shard
from adapters.jobs.execution_graph.post_grant_consumer import (
    CONSUMER_INVARIANT,
    consume_post_grant,
    max_receives,
)
from adapters.jobs.insight_precompute.graph_precompute import (
    PRECOMPUTE_TAIL_RESERVE_SECONDS,
    GraphPrecomputeWork,
)
from adapters.jobs.insight_precompute.materializer import ArtifactPrecomputePlan
from adapters.outbound.dynamodb.execution_graph import due_shard
from adapters.outbound.dynamodb.execution_graph_dispatch import arming_members
from adapters.outbound.telemetry import tracing
from adapters.outbound.telemetry.span_context import correlation
from adapters.outbound.telemetry.tracing import INSIGHT_PRECOMPUTE_PLAN, PRECOMPUTE_RUN
from domain.operations.execution_graph_dispatch import GraphGuardError, NodeState
from domain.operations.execution_graph_release import (
    BakeEffectKind,
    BakeRoster,
    BakeRosterRow,
    CohortMember,
    CohortOutcome,
    PrecomputeInputEntry,
    PrecomputeInputV1,
)
from domain.operations.status import canonical_authority_digest
from domain.presentation.insights import InsightSlotState
from domain.shared_kernel.errors import DependencyError, ErrorCode
from ports.outbound.execution_graph_dispatch import GraphTransactionConflictError

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-075")]


def _roster(count: int) -> BakeRoster:
    return BakeRoster(
        tuple(
            BakeRosterRow("campaigns", f"{index + 1}d", 240, 16.6, 123, 26)
            for index in range(count)
        )
    )


def _inputs(roster: BakeRoster) -> PrecomputeInputV1:
    return PrecomputeInputV1(
        tuple(
            PrecomputeInputEntry(
                row.page,
                row.variant,
                BakeEffectKind.UNAVAILABLE,
                f"sha256:{index + 1:064x}",
                None,
                None,
                "campaigns_materialization_unavailable",
            )
            for index, row in enumerate(roster.rows)
        )
    )


def _request(subject: str) -> Any:
    return SimpleNamespace(
        key=SimpleNamespace(
            slot_id=SimpleNamespace(value="portfolio"),
            subject=subject,
            window_start="2026-09-01",
            window_end="2026-09-07",
        )
    )


@dataclass
class Planner:
    requests: tuple[Any, ...] = ()
    drop_last: int = 0
    calls: list[PrecomputeInputV1] = field(default_factory=list[PrecomputeInputV1])

    def plan(self, inputs: PrecomputeInputV1) -> ArtifactPrecomputePlan:
        self.calls.append(inputs)
        entries = inputs.entries[: len(inputs.entries) - self.drop_last or None]
        roster = tuple(
            (entry.key, "requested" if self.requests else "no_request") for entry in entries
        )
        return ArtifactPrecomputePlan(self.requests, roster)


@dataclass
class Assembler:
    unavailable: set[str] = field(default_factory=set[str])
    calls: list[tuple[Any, ...]] = field(default_factory=list[tuple[Any, ...]])

    def many(self, requests: tuple[Any, ...], *, correlation_id: str = "") -> tuple[Any, ...]:
        del correlation_id
        self.calls.append(requests)
        return tuple(
            SimpleNamespace(
                state=(
                    InsightSlotState.UNAVAILABLE
                    if request.key.subject in self.unavailable
                    else InsightSlotState.FILLED
                )
            )
            for request in requests
        )


@dataclass
class BoundaryAssembler(Assembler):
    """Open the model boundary where the production assembler invokes generation."""

    def many(self, requests: tuple[Any, ...], *, correlation_id: str = "") -> tuple[Any, ...]:
        corr = correlation()
        with tracing.span(tracing.INSIGHT_GENERATION) as recording:
            recording.record(
                run_id=None if corr is None else corr.run_id,
                task_id=None if corr is None else corr.task_id,
                cohort_revision=None if corr is None else corr.cohort_revision,
            )
            return super().many(requests, correlation_id=correlation_id)


@dataclass
class World:
    roster: BakeRoster
    inputs: PrecomputeInputV1
    fake: FakeDynamo = field(default_factory=FakeDynamo)
    planner: Planner = field(default_factory=Planner)
    assembler: Assembler = field(default_factory=Assembler)
    clock: Clock = field(default_factory=Clock)
    cohort: tuple[int, int] | None = None
    """ADR-140: (loser, winner) curation counts of a two-member cohort; None is a sole winner."""
    metrics: list[str] = field(default_factory=list[str])
    """Every POST_GRANT invariant signal the consumer counted."""
    run: str = field(init=False)
    node: Any = field(init=False)

    def __post_init__(self) -> None:
        if self.cohort is None:
            self.run = publish_settled(self.fake)
        else:
            *_, self.run = (
                publish_settled(
                    self.fake,
                    advanced_curation=curation,
                    unit=canonical_authority_digest(["fictional-cohort", name]),
                    fingerprint=name * 64,
                    request_id=f"request-{name}",
                )
                for name, curation in zip("ab", self.cohort, strict=True)
            )
        arm_and_dispatch(self.fake, self.run, "bake")
        store = release_store(self.fake)
        record = store.read_run(self.run)
        waiting = store.read_node(self.run, "precompute")
        assert record is not None and waiting is not None
        assert waiting.state is NodeState.WAITING_INPUT
        store._commit(  # pyright: ignore[reportPrivateUsage]
            arming_members(
                record,
                waiting,
                NOW.isoformat(),
                attributes={
                    "precompute_input": {"S": self.inputs.to_json()},
                    "input_effect_roster_digest": {"S": self.inputs.digest},
                },
            )
        )
        dispatch_shard(
            DispatchContext(release_store(self.fake), SpySender(), now_text),
            due_shard(record.conflict_unit),
        )
        self.node = store.read_node(self.run, "precompute")
        assert self.node is not None and self.node.state is NodeState.DISPATCHED

    def consume(self, receive: int = 1):
        return consume_post_grant(
            ports(self.fake, self.clock, self.metrics),
            envelope(self.node, CohortMember.PRECOMPUTE, receive),
            GraphPrecomputeWork(self.roster, self.planner, self.assembler, 40),
        )

    def contribution(self):
        run = release_store(self.fake).read_run(self.run)
        assert run is not None and run.grant_id is not None
        return next(
            (
                item
                for item in release_store(self.fake).read_contributions(run.grant_id)
                if item.member is CohortMember.PRECOMPUTE
            ),
            None,
        )


@pytest.mark.parametrize("count", [3, 9])
def test_n_entry_readiness_produces_an_n_entry_contribution(count: int) -> None:
    roster = _roster(count)
    world = World(roster, _inputs(roster))
    world.planner.requests = tuple(_request(f"campaign-{index}") for index in range(count))

    result = world.consume()

    assert result.node_state is NodeState.SUCCEEDED
    contribution = world.contribution()
    assert contribution is not None and contribution.outcome is CohortOutcome.SUCCEEDED
    assert contribution.input_effect_roster_digest == world.inputs.digest
    assert contribution.request_roster == tuple((row.key, "requested") for row in roster.rows)
    assert len(world.assembler.calls[0]) == count


def test_seven_of_nine_readiness_is_refused_without_a_contribution() -> None:
    roster = _roster(9)
    world = World(roster, _inputs(BakeRoster(roster.rows[:7])))

    result = world.consume()

    assert result.acknowledge is False
    assert result.outcome == "graph_precompute_retryable"
    assert world.contribution() is None
    assert world.planner.calls == []
    assert world.assembler.calls == []


def test_unavailable_inputs_are_no_request_entries_and_never_call_the_model() -> None:
    roster = _roster(3)
    world = World(roster, _inputs(roster))

    result = world.consume()

    assert result.node_state is NodeState.SUCCEEDED
    contribution = world.contribution()
    assert contribution is not None and contribution.outcome is CohortOutcome.NO_ELIGIBLE
    assert contribution.request_roster == tuple((row.key, "no_request") for row in roster.rows)
    assert world.assembler.calls == [()]


def test_a_partial_planner_roster_is_refused_without_a_contribution() -> None:
    roster = _roster(9)
    world = World(roster, _inputs(roster))
    world.planner.drop_last = 2

    result = world.consume()

    assert result.acknowledge is False
    assert world.contribution() is None
    assert world.assembler.calls == []


def test_final_receive_budget_exhaustion_is_readiness_bound() -> None:
    roster = _roster(3)
    world = World(roster, _inputs(roster))
    world.planner.requests = (_request("campaign-a"),)
    world.clock.remaining = [PRECOMPUTE_TAIL_RESERVE_SECONDS * 1_000 - 1]

    result = world.consume(receive=3)

    assert result.node_state is NodeState.FAILED
    contribution = world.contribution()
    assert contribution is not None
    assert contribution.input_effect_roster_digest == world.inputs.digest
    assert world.assembler.calls == []


def test_precompute_run_and_artifact_plan_spans_share_run_correlation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(tracing, "TRACER", provider.get_tracer("test"))
    roster = _roster(3)
    world = World(roster, _inputs(roster))
    world.planner.requests = (_request("campaign-a"),)
    world.assembler = BoundaryAssembler()

    world.consume()

    spans = {item.name: item for item in exporter.get_finished_spans()}
    assert {PRECOMPUTE_RUN, INSIGHT_PRECOMPUTE_PLAN, tracing.INSIGHT_GENERATION} <= set(spans)
    for name in (PRECOMPUTE_RUN, INSIGHT_PRECOMPUTE_PLAN):
        attributes = dict(spans[name].attributes or {})
        assert attributes["run_id"] == world.run
        assert attributes["task_id"] == world.node.node_id
        assert attributes["input_effect_roster_digest"] == world.inputs.digest

    run_span = spans[PRECOMPUTE_RUN]
    plan_span = spans[INSIGHT_PRECOMPUTE_PLAN]
    model_span = spans[tracing.INSIGHT_GENERATION]
    model_attributes = dict(model_span.attributes or {})
    expected_correlation = {"run_id": world.run, "task_id": world.node.node_id}
    actual_correlation = {name: model_attributes[name] for name in expected_correlation}
    assert actual_correlation == expected_correlation
    # Paired failing control: another task id cannot satisfy the GRAPH trace contract.
    with pytest.raises(AssertionError):
        assert {**actual_correlation, "task_id": "another-task"} == expected_correlation

    plan_parent = plan_span.parent
    model_parent = model_span.parent
    run_context = run_span.context
    assert plan_parent is not None
    assert model_parent is not None
    assert run_context is not None
    lineage = {
        INSIGHT_PRECOMPUTE_PLAN: plan_parent.span_id,
        tracing.INSIGHT_GENERATION: model_parent.span_id,
    }
    expected = {
        INSIGHT_PRECOMPUTE_PLAN: run_context.span_id,
        tracing.INSIGHT_GENERATION: run_context.span_id,
    }
    assert lineage == expected

    # Paired failing control: model generation nested under planning violates the boundary.
    plan_context = plan_span.context
    assert plan_context is not None
    with pytest.raises(AssertionError):
        assert {**lineage, tracing.INSIGHT_GENERATION: plan_context.span_id} == expected


@pytest.mark.covers("ADR-140")
def test_c1_precompute_agrees_with_bake_on_cohort_eligibility() -> None:
    roster = _roster(3)
    world = World(roster, _inputs(roster), cohort=(1, 0))  # curated loser, zero-curation winner

    result = world.consume()

    assert result.node_state is NodeState.SUCCEEDED
    assert len(world.planner.calls) == 1  # the work ran: the cohort is eligible
    contribution = world.contribution()
    assert contribution is not None
    assert contribution.input_effect_roster_digest == world.inputs.digest
    # Control: with no curation anywhere in the cohort, precompute contributes NO_ELIGIBLE.
    quiet = World(roster, _inputs(roster), cohort=(0, 0))
    assert quiet.consume().node_state is NodeState.SUCCEEDED
    assert quiet.planner.calls == []
    found = quiet.contribution()
    assert found is not None and found.outcome is CohortOutcome.NO_ELIGIBLE
    assert found.input_effect_roster_digest is None


# --- D2 hardening: a deterministic error on the final receive settles (F4) --------------------

_FINAL = max_receives("precompute")


@dataclass
class _RaisingPlanner(Planner):
    """A planner whose work raises one scripted error on every receive."""

    error: Exception = field(default_factory=lambda: ValueError("a fictional planner defect"))

    def plan(self, inputs: PrecomputeInputV1) -> ArtifactPrecomputePlan:
        self.calls.append(inputs)
        raise self.error


def _raising_world(error: Exception) -> World:
    roster = _roster(3)
    return World(roster, _inputs(roster), planner=_RaisingPlanner(error=error))


@pytest.mark.covers("ADR-140")
@pytest.mark.parametrize(
    "error",
    [
        ValueError("a fictional planner defect"),
        RuntimeError("a fictional unlisted planner defect"),
        AttributeError("a fictional missing attribute"),
    ],
    ids=["value", "runtime", "attribute"],
)
def test_h4_a_deterministic_error_on_the_final_receive_settles_every_key_post_grant_invariant(
    error: Exception,
) -> None:
    world = _raising_world(error)
    # Control: an earlier receive is retryable and contributes nothing.
    retried = world.consume(receive=1)
    assert (retried.acknowledge, retried.outcome) == (False, "graph_precompute_retryable")
    assert world.contribution() is None
    assert world.metrics == []

    settled = world.consume(receive=_FINAL)

    assert (settled.acknowledge, settled.node_state) == (True, NodeState.FAILED)
    contribution = world.contribution()
    assert contribution is not None and contribution.outcome is CohortOutcome.UNAVAILABLE
    assert contribution.unavailable == tuple(
        (key, "post_grant_invariant") for key in sorted(world.roster.keys)
    )
    assert world.metrics == [CONSUMER_INVARIANT]


@pytest.mark.covers("ADR-140")
@pytest.mark.parametrize(
    "error",
    [
        DependencyError(ErrorCode("FICTIONAL", 41), "a throttled model"),
        GraphGuardError("fictional_claim_fence"),
        GraphTransactionConflictError("a fictional concurrent transaction"),
    ],
    ids=["dependency", "guard", "conflict"],
)
def test_h4_control_a_dependency_guard_or_conflict_error_on_the_final_receive_still_retries(
    error: Exception,
) -> None:
    world = _raising_world(error)

    result = world.consume(receive=_FINAL)

    assert (result.acknowledge, result.outcome) == (False, "graph_precompute_retryable")
    assert world.contribution() is None
    assert world.metrics == []
