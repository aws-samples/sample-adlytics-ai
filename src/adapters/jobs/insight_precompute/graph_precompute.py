# SPDX-License-Identifier: MIT-0
"""Readiness-bound GRAPH precompute from immutable bake artifacts (DB25)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Final, Protocol, cast

from adapters.jobs.execution_graph.post_grant_consumer import (
    ConsumerContext,
    Produced,
    PublishEffect,
    ReleaseLease,
    WorkOutcome,
)
from adapters.jobs.insight_precompute.materializer import ArtifactPrecomputePlan
from adapters.outbound.telemetry.span_context import correlated
from adapters.outbound.telemetry.tracing import (
    INSIGHT_PRECOMPUTE_PLAN,
    PRECOMPUTE_RUN,
    span,
    trace_boundary,
)
from domain.operations.execution_graph_release import (
    INSIGHT_SLOT_UNAVAILABLE,
    PRECOMPUTE_BUDGET_EXHAUSTED,
    BakeRoster,
    CohortMember,
    PrecomputeInputV1,
)
from domain.operations.generated_execution_graphs import BAKE_TASKS
from domain.presentation.insights import InsightSlotState
from domain.shared_kernel.errors import DependencyError, ErrorCode, InvariantError
from ports.outbound.execution_graph_release import BakeGraphStore, ServingGraphReleaseStore

if TYPE_CHECKING:
    from adapters.inbound.presentation_handlers.insights import InsightAssemblyRequest
    from domain.presentation.insights import InsightSlot

__all__ = [
    "PRECOMPUTE_CLAIM_LEASE",
    "PRECOMPUTE_EFFECT_UNVERIFIED",
    "PRECOMPUTE_FAN_OUT_EXCEEDED",
    "PRECOMPUTE_PLAN_KEY",
    "PRECOMPUTE_TAIL_RESERVE_SECONDS",
    "PRECOMPUTE_TIMEOUT_SECONDS",
    "GraphPrecomputeWork",
    "SlotAssembler",
    "StagedPlanner",
    "request_key",
]

PRECOMPUTE_TIMEOUT_SECONDS: Final = 30
PRECOMPUTE_CLAIM_LEASE: Final = timedelta(seconds=int(BAKE_TASKS["precompute_claim_lease_seconds"]))
"""The claim lease: the 30 s worker timeout plus the 30 s FIFO visibility margin (contract)."""
PRECOMPUTE_TAIL_RESERVE_SECONDS: Final = 5
PRECOMPUTE_PLAN_KEY: Final = "precompute#plan"
PRECOMPUTE_FAN_OUT_EXCEEDED: Final = ErrorCode("GRAPH_PRECOMPUTE", 1)
PRECOMPUTE_EFFECT_UNVERIFIED: Final = ErrorCode("GRAPH_PRECOMPUTE", 2)
_PRECOMPUTE_UNION_MAX: Final = 40
_RESERVE_MS: Final = PRECOMPUTE_TAIL_RESERVE_SECONDS * 1_000


class StagedPlanner(Protocol):
    """Derive requests from the exact stored readiness input without Athena."""

    def plan(self, inputs: PrecomputeInputV1) -> ArtifactPrecomputePlan:
        """Return requests and one request/no-request disposition per input row."""
        ...


class SlotAssembler(Protocol):
    """Assemble one bounded model/cache batch."""

    def many(
        self, requests: tuple[InsightAssemblyRequest, ...], *, correlation_id: str = ""
    ) -> tuple[InsightSlot, ...]:
        """Return slots in request order."""
        ...


def request_key(request: InsightAssemblyRequest) -> str:
    """Return one request's artifact key: its slot, subject and window."""
    key = request.key
    return "/".join((key.slot_id.value, key.subject, key.window_start, key.window_end))


@dataclass(slots=True)
class GraphPrecomputeWork:
    """The winner's exact-roster precompute, with no publication or Athena fallback."""

    roster: BakeRoster
    planner: StagedPlanner
    assembler: SlotAssembler
    max_requests_per_publication: int
    observed_union_count: int = 0

    def __post_init__(self) -> None:
        if self.max_requests_per_publication != _PRECOMPUTE_UNION_MAX:
            raise ValueError("graph precompute union capacity must be exactly 40.")

    @property
    def member(self) -> CohortMember:
        """Contribute the cohort's precompute member."""
        return CohortMember.PRECOMPUTE

    @property
    def lease(self) -> timedelta:
        """Return the precompute claim lease."""
        return PRECOMPUTE_CLAIM_LEASE

    def eligible(self, effect: PublishEffect) -> bool:
        """Readiness exists only for a bake-eligible curation publication."""
        return effect.advanced_curation_count > 0

    def artifact_keys(self) -> tuple[str, ...]:
        """Return every readiness roster key for invariant settlement."""
        return self.roster.keys

    def counts(self) -> Mapping[str, int]:
        """Return the observed artifact-derived request union size."""
        return {"observed_union_count": self.observed_union_count}

    @trace_boundary(PRECOMPUTE_RUN, INSIGHT_PRECOMPUTE_PLAN)
    def run(self, context: ConsumerContext) -> WorkOutcome:
        """Attest exact readiness, derive artifact requests, and assemble once."""
        store = cast("BakeGraphStore", context.store)
        inputs = store.read_precompute_input(context.run.run_id)
        if inputs is None or tuple(entry.key for entry in inputs.entries) != self.roster.keys:
            raise DependencyError(
                PRECOMPUTE_EFFECT_UNVERIFIED,
                "the precompute delivery has no exact readiness-bound input roster.",
            )
        digest = inputs.digest
        cohort = context.node.cohort_revision
        if cohort is None and context.run.grant_id is not None:
            execution = cast("ServingGraphReleaseStore", store).read_serving_execution(
                context.run.grant_id, context.run.run_id
            )
            cohort = None if execution is None else execution.head.cohort_revision
        if cohort is None:
            raise ValueError("graph precompute requires a cohort revision.")
        with (
            correlated(context.run.run_id, context.node.node_id, cohort),
            span(
                PRECOMPUTE_RUN,
                run_id=context.run.run_id,
                task_id=context.node.node_id,
                cohort_revision=cohort,
                input_effect_roster_digest=digest,
                task_count=len(inputs.entries),
            ) as run_span,
        ):
            with span(
                INSIGHT_PRECOMPUTE_PLAN,
                run_id=context.run.run_id,
                task_id=context.node.node_id,
                cohort_revision=cohort,
                input_effect_roster_digest=digest,
            ) as plan_span:
                planned = self.planner.plan(inputs)
                keys = tuple(key for key, _ in planned.request_roster)
                if keys != self.roster.keys:
                    raise DependencyError(
                        PRECOMPUTE_EFFECT_UNVERIFIED,
                        "the artifact planner did not account for the exact readiness roster.",
                    )
                requests = planned.requests
                self.observed_union_count = len(requests)
                no_request = sum(
                    disposition == "no_request" for _, disposition in planned.request_roster
                )
                plan_span.record(
                    task_count=len(inputs.entries),
                    request_count=len(requests),
                    no_request_count=no_request,
                    outcome="derived",
                )
            if len(requests) > self.max_requests_per_publication:
                raise InvariantError(
                    PRECOMPUTE_FAN_OUT_EXCEEDED,
                    "the artifact-derived precompute union exceeded its configured capacity.",
                    context={"requests": len(requests)},
                )
            if requests and context.remaining_ms() < _RESERVE_MS:
                if not context.final:
                    run_span.record(
                        request_count=len(requests),
                        no_request_count=no_request,
                        outcome="lease_released",
                    )
                    return ReleaseLease("precompute_reserve_reached")
                exhausted = tuple(
                    (request_key(request), PRECOMPUTE_BUDGET_EXHAUSTED) for request in requests
                )
                run_span.record(
                    request_count=len(requests),
                    no_request_count=no_request,
                    outcome="budget_exhausted",
                )
                return Produced(
                    exhausted,
                    False,
                    len(requests),
                    digest,
                    planned.request_roster,
                )
            slots = self.assembler.many(requests, correlation_id=context.run.run_id)
            unavailable = tuple(
                (request_key(request), INSIGHT_SLOT_UNAVAILABLE)
                for request, slot in zip(requests, slots, strict=True)
                if slot.state is InsightSlotState.UNAVAILABLE
            )
            run_span.record(
                request_count=len(requests),
                no_request_count=no_request,
                outcome="completed" if not unavailable else "degraded",
            )
            return Produced(
                unavailable,
                bool(requests) and len(unavailable) < len(requests),
                len(requests),
                digest,
                planned.request_roster,
            )
