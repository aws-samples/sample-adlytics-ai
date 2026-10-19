# SPDX-License-Identifier: MIT-0
"""Deterministic two-event companion flows for authority-backed workload surfaces."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from domain.operations.status import (
    MEASUREMENT_REFS,
    REGISTRY_VERSION,
    SCHEMA_VERSION,
    SURFACE_IDS,
    AuthorityObservation,
    CompletionOutcome,
    EvaluationBinding,
    FlowState,
    OperationalDataFlowEvent,
    OperationalStatusError,
    Progress,
    canonical_authority_digest,
    derived_uuid,
)

_START_SEQUENCE = 1
_TERMINAL_SEQUENCE = 2
_TERMINAL_STATES = frozenset({FlowState.BLOCKED, FlowState.FAILED, FlowState.COMPLETED})


@dataclass(frozen=True, slots=True)
class CompanionFlowRequest:
    """Identity of one workload unit whose lifecycle a companion producer reports."""

    surface: str
    authority_kind: str
    identity: dict[str, str]
    started_at: datetime
    start_state: FlowState = FlowState.RUNNING
    start_stage: str = "started"

    def __post_init__(self) -> None:
        if self.started_at.tzinfo is None or self.started_at.utcoffset() is None:
            raise OperationalStatusError("companion start instant must be timezone-aware")
        if self.surface not in SURFACE_IDS:
            raise OperationalStatusError("companion surface is not registered")
        if not self.identity:
            raise OperationalStatusError("companion identity requires canonical components")
        if self.start_state not in {FlowState.OBSERVED, FlowState.QUEUED, FlowState.RUNNING}:
            raise OperationalStatusError("companion start state must be nonterminal")


@dataclass(frozen=True, slots=True)
class CompanionTerminal:
    """The authority-attested terminal disposition of one companion flow."""

    state: FlowState
    outcome: CompletionOutcome
    stage: str
    occurred_at: datetime
    authority_digest_value: dict[str, str]
    reason_code: str | None = None
    progress: Progress | None = None

    def __post_init__(self) -> None:
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise OperationalStatusError("companion terminal instant must be timezone-aware")
        if self.state not in _TERMINAL_STATES:
            raise OperationalStatusError("companion terminal requires a terminal state")


@dataclass(frozen=True, slots=True)
class CompanionFlow:
    """Derived flow identity plus its deterministic start and terminal events."""

    flow_id: str
    execution_id: str
    start: OperationalDataFlowEvent
    _request: CompanionFlowRequest

    @property
    def started_at(self) -> datetime:
        """The flow's UTC-normalized start instant, for message-deterministic terminals.

        A terminal instant is identity-bearing, so an emitter without attested
        settlement time reuses this instant rather than reading a wall clock —
        a redelivery must reproduce byte-identical events.
        """
        return self._request.started_at.astimezone(UTC)

    def terminal(self, disposition: CompanionTerminal) -> OperationalDataFlowEvent:
        """Build the second, terminal event under the same flow and execution.

        The digest nests the flow's own identity beside the disposition so two flows
        that settle with byte-identical dispositions (the same refusal for the same
        page, on every message, forever) can never collide on one immutable event id.
        """
        occurred = disposition.occurred_at.astimezone(UTC)
        progress = disposition.progress
        digest = canonical_authority_digest(
            {
                "schema_version": SCHEMA_VERSION,
                "registry_version": REGISTRY_VERSION,
                "evaluation_mode": "observation_only",
                "flow_id": self.flow_id,
                "surface": self._request.surface,
                "identity": dict(self._request.identity),
                "state": disposition.state.value,
                "outcome": disposition.outcome.value,
                "stage": disposition.stage,
                "occurred_at": occurred.isoformat(),
                "authority": disposition.authority_digest_value,
                "reason_code": disposition.reason_code,
                "progress": (
                    None
                    if progress is None
                    else {
                        "completed": progress.completed,
                        "total": progress.total,
                        "unit": progress.unit,
                    }
                ),
                "measurement_refs": MEASUREMENT_REFS[self._request.surface],
            }
        )
        return OperationalDataFlowEvent(
            event_id=derived_uuid(
                "event",
                self._request.surface,
                digest,
                str(_TERMINAL_SEQUENCE),
                disposition.state.value,
            ),
            predecessor_event_id=self.start.event_id,
            flow_id=self.flow_id,
            execution_id=self.execution_id,
            predecessor_execution_id=None,
            transition_sequence=_TERMINAL_SEQUENCE,
            surface=self._request.surface,
            stage=disposition.stage,
            state=disposition.state,
            outcome=disposition.outcome,
            occurred_at=occurred,
            clock_started_at=self.start.clock_started_at,
            authority=AuthorityObservation(
                kind=self._request.authority_kind,
                opaque_digest=digest,
                observed_at=occurred,
            ),
            reason_code=disposition.reason_code,
            progress=disposition.progress,
            evaluation_binding=EvaluationBinding(MEASUREMENT_REFS[self._request.surface]),
        )


def companion_flow(request: CompanionFlowRequest) -> CompanionFlow:
    """Derive stable identities and the start event for one workload unit."""
    started = request.started_at.astimezone(UTC)
    identity_digest = canonical_authority_digest(
        {
            "schema_version": SCHEMA_VERSION,
            "registry_version": REGISTRY_VERSION,
            "evaluation_mode": "observation_only",
            "surface": request.surface,
            "authority_kind": request.authority_kind,
            "identity": dict(request.identity),
            "started_at": started.isoformat(),
            "start_state": request.start_state.value,
            "start_stage": request.start_stage,
            "measurement_refs": MEASUREMENT_REFS[request.surface],
        }
    )
    flow_id = derived_uuid("flow", request.surface, identity_digest)
    execution_id = derived_uuid("execution", flow_id, identity_digest)
    start = OperationalDataFlowEvent(
        event_id=derived_uuid(
            "event",
            request.surface,
            identity_digest,
            str(_START_SEQUENCE),
            request.start_state.value,
        ),
        predecessor_event_id=None,
        flow_id=flow_id,
        execution_id=execution_id,
        predecessor_execution_id=None,
        transition_sequence=_START_SEQUENCE,
        surface=request.surface,
        stage=request.start_stage,
        state=request.start_state,
        occurred_at=started,
        clock_started_at=started,
        authority=AuthorityObservation(
            kind=request.authority_kind,
            opaque_digest=identity_digest,
            observed_at=started,
        ),
        evaluation_binding=EvaluationBinding(MEASUREMENT_REFS[request.surface]),
    )
    return CompanionFlow(flow_id=flow_id, execution_id=execution_id, start=start, _request=request)
