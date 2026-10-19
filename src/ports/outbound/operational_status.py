# SPDX-License-Identifier: MIT-0
"""Outbound boundaries for the operational data-flow observation projection."""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime
from typing import Protocol, runtime_checkable

from domain.operations.generated_data_flows import REGISTRY_VERSION
from domain.operations.stage1_operational_status import (
    ChildOutcomeV2,
    OperationalDataFlowUsageContribution,
    OperationalDataFlowUsageEvidenceDisposition,
    PlanRequiredChildExpectations,
)
from domain.operations.status import (
    SCHEMA_VERSION,
    SURFACE_IDS,
    BaselineWindow,
    Evidence,
    OperationalDataFlowEvent,
    OperationalStatusError,
    ReductionKind,
    canonical_uuid,
)


@dataclass(frozen=True, slots=True)
class SurfaceTransportObservation:
    """Content-free queue transport counts for one registered surface."""

    visible: int
    in_flight: int
    delayed: int
    oldest_age_ms: int
    retrying: int
    dlq: int

    def __post_init__(self) -> None:
        _nonnegative_counts(self)


@dataclass(frozen=True, slots=True)
class SurfaceComputeObservation:
    """Content-free compute counts for one registered surface."""

    running: int
    errors: int
    throttles: int

    def __post_init__(self) -> None:
        _nonnegative_counts(self)


@dataclass(frozen=True, slots=True)
class SurfaceProviderLatencyObservation:
    """Bounded provider-latency aggregate without provider identity."""

    count: int
    total_ms: int
    max_ms: int

    def __post_init__(self) -> None:
        _nonnegative_counts(self)
        if self.count == 0 and (self.total_ms != 0 or self.max_ms != 0):
            raise OperationalStatusError("empty provider latency must contain zero durations")
        if self.count > 0 and self.max_ms > self.total_ms:
            raise OperationalStatusError("provider maximum latency exceeds total latency")


@dataclass(frozen=True, slots=True)
class OperationalDataFlowSurfaceObservation:
    """One immutable monotonic aggregate observation for a registered surface."""

    observation_id: str
    surface: str
    observed_at: datetime
    source_sequence: int
    evidence: Evidence
    open_operational_case_count: int
    transport: SurfaceTransportObservation | None = None
    compute: SurfaceComputeObservation | None = None
    provider_latency: SurfaceProviderLatencyObservation | None = None
    reason_code: str | None = None
    registry_version: str = REGISTRY_VERSION
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        canonical_uuid(self.observation_id, "observation id")
        if self.schema_version != SCHEMA_VERSION or self.registry_version != REGISTRY_VERSION:
            raise OperationalStatusError("surface observation contract version is not active")
        if self.surface not in SURFACE_IDS:
            raise OperationalStatusError("surface observation names an unregistered surface")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise OperationalStatusError("surface observation instant must be timezone-aware")
        _nonnegative(self.source_sequence, "source sequence")
        _nonnegative(self.open_operational_case_count, "open operational case count")
        if self.reason_code is not None and not self.reason_code.strip():
            raise OperationalStatusError("surface observation reason code must be nonblank")
        if (
            all(value is None for value in (self.transport, self.compute, self.provider_latency))
            and self.open_operational_case_count == 0
        ):
            raise OperationalStatusError("surface observation contains no aggregate evidence")


type OperationalStatusMessage = (
    OperationalDataFlowEvent
    | OperationalDataFlowSurfaceObservation
    | OperationalDataFlowUsageContribution
    | OperationalDataFlowUsageEvidenceDisposition
)


@dataclass(frozen=True, slots=True)
class ProjectionOutcome:
    """Durable queue-member disposition returned after consistent classification."""

    kind: ReductionKind
    applied_count: int = 0


@dataclass(frozen=True, slots=True)
class PendingPage:
    """A bounded deterministic page of live parked events."""

    events: tuple[OperationalDataFlowEvent, ...]


@dataclass(frozen=True, slots=True)
class EventPage:
    """A bounded applied-history page with a server-owned continuation token."""

    events: tuple[OperationalDataFlowEvent, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class PlanTerminalProjectionRequest:
    """A Stage-1 plan terminal plus its exact generation-bound expectation authority."""

    event: OperationalDataFlowEvent
    expectations: PlanRequiredChildExpectations

    def __post_init__(self) -> None:
        if self.event.flow_id != self.expectations.parent_flow_id:
            raise OperationalStatusError("plan event and expectation authority disagree")
        if self.event.required_child_flow_ids != self.expectations.required_child_flow_ids:
            raise OperationalStatusError("plan event child declaration differs from expectations")


@runtime_checkable
class OperationalStatusSink(Protocol):
    """Direct dedicated FIFO transport for the closed four-member union."""

    def send(self, message: OperationalStatusMessage) -> None:
        """Durably admit one validated status queue member."""
        ...


@runtime_checkable
class OperationalProjectionPort(Protocol):
    """Conditional operational queue-member reducer and bounded read model."""

    def reduce(self, message: OperationalStatusMessage) -> ProjectionOutcome:
        """Apply one member through the single v1 projection path."""
        ...

    def project_child_outcome(self, outcome: ChildOutcomeV2) -> ProjectionOutcome:
        """Create or advance one required-unit v2 outcome projection."""
        ...

    def reduce_plan_terminal(self, request: PlanTerminalProjectionRequest) -> ProjectionOutcome:
        """Apply or generation-gate one Stage-1 plan terminal."""
        ...

    def pending(self, limit: int) -> PendingPage:
        """Query a bounded page from the live pending index; never scan."""
        ...

    def flow(self, flow_id: str) -> dict[str, object] | None:
        """Read one exact flow snapshot consistently."""
        ...

    def events(self, flow_id: str, cursor: str, limit: int) -> EventPage | None:
        """Query immutable applied history in transition order."""
        ...

    def surface(self, surface: str, limit: int) -> tuple[dict[str, object], ...]:
        """Query a bounded registered-surface contribution page."""
        ...

    def window_contributions(
        self, surface: str, window_start: datetime, window_end: datetime
    ) -> tuple[OperationalDataFlowEvent, ...]:
        """Query one closed window's surface contributions in occurrence order."""
        ...

    def baselines(self, surface: str, measurement_ref: str) -> tuple[BaselineWindow, ...]:
        """Read one surface measurement's descriptive baseline windows, newest first."""
        ...

    def record_baseline(self, surface: str, window: BaselineWindow) -> bool:
        """Create one immutable baseline revision; False means it already exists."""
        ...


def _nonnegative(value: object, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise OperationalStatusError(f"{name} must be a nonnegative integer")


def _nonnegative_counts(
    value: SurfaceTransportObservation
    | SurfaceComputeObservation
    | SurfaceProviderLatencyObservation,
) -> None:
    for descriptor in fields(value):
        _nonnegative(getattr(value, descriptor.name), descriptor.name.replace("_", " "))
