# SPDX-License-Identifier: MIT-0
"""Closed observation-only operational data-flow identities, events, and reducers."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final, cast

from domain.operations.generated_data_flows import (
    IDENTITY_CONTRACT,
    OBSERVATION_POLICY,
    REGISTRY_VERSION,
    SURFACES,
)
from domain.semantics.publication import AttemptEvent, AttemptEventKind

SCHEMA_VERSION: Final = "1"
MAX_CHILDREN: Final = 100
_SHA256_HEX_DIGITS: Final = 64
_EVENTS_WITH_TERMINAL: Final = 2
UUID_NAMESPACE: Final = uuid.UUID(cast("str", IDENTITY_CONTRACT["uuid_namespace"]))
FORBIDDEN_FIELD_PARTS: Final = ("objective", "target", "result")
EVENT_FIELDS: Final = frozenset(
    {
        "schema_version",
        "registry_version",
        "event_id",
        "predecessor_event_id",
        "flow_id",
        "parent_flow_id",
        "causation_event_id",
        "execution_id",
        "predecessor_execution_id",
        "transition_sequence",
        "surface",
        "stage",
        "state",
        "outcome",
        "occurred_at",
        "clock_started_at",
        "required_child_flow_ids",
        "optional_child_flow_ids",
        "progress",
        "authority",
        "reason_code",
        "evaluation_binding",
    }
)
SURFACE_IDS: Final = frozenset(cast("str", surface["id"]) for surface in SURFACES)
MEASUREMENT_REFS: Final = {
    cast("str", surface["id"]): tuple(
        (
            cast("str", measurement["measurement_ref"]),
            cast("str", measurement["measurement_version"]),
        )
        for measurement in cast("list[dict[str, object]]", surface["measurements"])
    )
    for surface in SURFACES
}


class OperationalStatusError(ValueError):
    """An operational event violates the closed observation contract."""


class FlowState(StrEnum):
    """Closed common lifecycle vocabulary."""

    UNKNOWN = "unknown"
    OBSERVED = "observed"
    QUEUED = "queued"
    RUNNING = "running"
    RETRYING = "retrying"
    DELAYED = "delayed"
    BLOCKED = "blocked"
    FAILED = "failed"
    COMPLETED = "completed"
    SUPERSEDED = "superseded"


class CompletionOutcome(StrEnum):
    """Authority-backed terminal decisions without attainment semantics."""

    SUCCEEDED = "succeeded"
    COMPLETED_NO_CHANGE = "completed_no_change"
    COMPLETED_REFUSED = "completed_refused"
    EXHAUSTED = "exhausted"
    SUPERSEDED = "superseded"


class Evidence(StrEnum):
    """Closed evidence completeness vocabulary."""

    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    STALE = "stale"
    UNAVAILABLE = "unavailable"


class ReductionKind(StrEnum):
    """Deterministic arrival classification after exact current-snapshot readback."""

    APPLY = "apply"
    PARK = "park"
    ALREADY_APPLIED = "already_applied"
    CONFLICT = "conflict"


@dataclass(frozen=True, slots=True)
class Progress:
    """Monotonic progress within one execution."""

    completed: int
    total: int
    unit: str

    def __post_init__(self) -> None:
        if self.completed < 0 or self.total < 1 or self.completed > self.total:
            raise OperationalStatusError("progress is outside its declared bound")
        if not self.unit.strip():
            raise OperationalStatusError("progress unit must be nonblank")


@dataclass(frozen=True, slots=True)
class AuthorityObservation:
    """Content-free evidence that an owning durable record was read exactly."""

    kind: str
    opaque_digest: str
    observed_at: datetime

    def __post_init__(self) -> None:
        if not self.kind.strip():
            raise OperationalStatusError("authority kind must be nonblank")
        if len(self.opaque_digest) != _SHA256_HEX_DIGITS or any(
            character not in "0123456789abcdef" for character in self.opaque_digest
        ):
            raise OperationalStatusError("authority digest must be canonical lowercase sha256")
        _utc(self.observed_at, "authority observed_at")


@dataclass(frozen=True, slots=True)
class EvaluationBinding:
    """The v1 observation-only measurement binding; objective references cannot be represented."""

    measurement_refs: tuple[tuple[str, str], ...]
    mode: str = "observation_only"

    def __post_init__(self) -> None:
        if self.mode != "observation_only":
            raise OperationalStatusError("v1 evaluation binding must be observation_only")
        if len(self.measurement_refs) != len(set(self.measurement_refs)):
            raise OperationalStatusError("measurement references must be unique")
        if any(not reference or not version for reference, version in self.measurement_refs):
            raise OperationalStatusError("measurement references must be versioned")


@dataclass(frozen=True, slots=True)
class OperationalDataFlowEvent:
    """One immutable authority-backed lifecycle transition."""

    event_id: str
    predecessor_event_id: str | None
    flow_id: str
    execution_id: str
    predecessor_execution_id: str | None
    transition_sequence: int
    surface: str
    stage: str
    state: FlowState
    occurred_at: datetime
    authority: AuthorityObservation
    evaluation_binding: EvaluationBinding
    outcome: CompletionOutcome | None = None
    reason_code: str | None = None
    parent_flow_id: str | None = None
    causation_event_id: str | None = None
    clock_started_at: datetime | None = None
    required_child_flow_ids: tuple[str, ...] = ()
    optional_child_flow_ids: tuple[str, ...] = ()
    progress: Progress | None = None
    registry_version: str = REGISTRY_VERSION
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:  # noqa: PLR0912 -- validates independent event invariants
        for name in (
            "event_id",
            "flow_id",
            "execution_id",
            "predecessor_event_id",
            "predecessor_execution_id",
            "parent_flow_id",
            "causation_event_id",
        ):
            value = getattr(self, name)
            if value is not None:
                canonical_uuid(value, name)
        if self.schema_version != SCHEMA_VERSION or self.registry_version != REGISTRY_VERSION:
            raise OperationalStatusError("event contract version is not active")
        if self.surface not in SURFACE_IDS:
            raise OperationalStatusError("event surface is not registered")
        if not self.stage.strip():
            raise OperationalStatusError("event stage must be nonblank")
        if self.transition_sequence < 1:
            raise OperationalStatusError("transition sequence must be positive")
        if self.transition_sequence == 1 and self.predecessor_event_id is not None:
            raise OperationalStatusError("root transition cannot name a predecessor event")
        if self.transition_sequence > 1 and self.predecessor_event_id is None:
            raise OperationalStatusError("non-root transition requires its predecessor event")
        _utc(self.occurred_at, "occurred_at")
        if self.clock_started_at is not None:
            _utc(self.clock_started_at, "clock_started_at")
            if self.clock_started_at > self.occurred_at:
                raise OperationalStatusError("flow clock cannot start after a transition")
        if self.state in {
            FlowState.BLOCKED,
            FlowState.FAILED,
            FlowState.COMPLETED,
            FlowState.SUPERSEDED,
        }:
            if self.outcome is None:
                raise OperationalStatusError(
                    "terminal lifecycle state requires a completion outcome"
                )
        elif self.outcome is not None:
            raise OperationalStatusError("nonterminal lifecycle state cannot carry an outcome")
        _children(self.required_child_flow_ids, "required")
        _children(self.optional_child_flow_ids, "optional")
        if set(self.required_child_flow_ids) & set(self.optional_child_flow_ids):
            raise OperationalStatusError("a child cannot be both required and optional")
        if self.evaluation_binding.measurement_refs != MEASUREMENT_REFS[self.surface]:
            raise OperationalStatusError("event measurement binding differs from the registry")


@dataclass(frozen=True, slots=True)
class FlowSnapshot:
    """Latest contiguous applied event for one logical flow."""

    latest_event_id: str
    latest_sequence: int
    flow_id: str
    execution_id: str
    predecessor_execution_id: str | None
    surface: str
    stage: str
    state: FlowState
    occurred_at: datetime
    clock_started_at: datetime | None
    authority: AuthorityObservation
    evaluation_binding: EvaluationBinding
    outcome: CompletionOutcome | None
    reason_code: str | None
    required_child_flow_ids: tuple[str, ...]
    optional_child_flow_ids: tuple[str, ...]

    @classmethod
    def from_event(cls, event: OperationalDataFlowEvent) -> FlowSnapshot:
        """Create a snapshot only from an already validated event."""
        return cls(
            latest_event_id=event.event_id,
            latest_sequence=event.transition_sequence,
            flow_id=event.flow_id,
            execution_id=event.execution_id,
            predecessor_execution_id=event.predecessor_execution_id,
            surface=event.surface,
            stage=event.stage,
            state=event.state,
            occurred_at=event.occurred_at,
            clock_started_at=event.clock_started_at,
            authority=event.authority,
            evaluation_binding=event.evaluation_binding,
            outcome=event.outcome,
            reason_code=event.reason_code,
            required_child_flow_ids=event.required_child_flow_ids,
            optional_child_flow_ids=event.optional_child_flow_ids,
        )


@dataclass(frozen=True, slots=True)
class ReductionDecision:
    """Arrival classification and resulting snapshot when it can apply."""

    kind: ReductionKind
    snapshot: FlowSnapshot | None = None
    reason: str = ""


@dataclass(frozen=True, slots=True)
class BaselineWindow:
    """Immutable descriptive window; its shape cannot carry attainment semantics."""

    window_id: str
    predecessor_window_id: str | None
    revision: int
    measurement_ref: str
    measurement_version: str
    window_start: datetime
    window_end: datetime
    population_cutoff: datetime
    finalized_at: datetime
    completeness: str
    late_sample_count: int
    sample_count: int

    def __post_init__(self) -> None:
        canonical_uuid(self.window_id, "window_id")
        if self.predecessor_window_id is not None:
            canonical_uuid(self.predecessor_window_id, "predecessor_window_id")
        if self.revision < 1 or self.late_sample_count < 0 or self.sample_count < 0:
            raise OperationalStatusError(
                "baseline counters must be nonnegative with positive revision"
            )
        if self.completeness not in {"complete", "incomplete"}:
            raise OperationalStatusError("baseline completeness is invalid")
        moments = (self.window_start, self.window_end, self.population_cutoff, self.finalized_at)
        for moment in moments:
            _utc(moment, "baseline instant")
        if not self.window_start < self.window_end <= self.population_cutoff <= self.finalized_at:
            raise OperationalStatusError("baseline instants are not monotonic")
        if self.revision == 1 and self.predecessor_window_id is not None:
            raise OperationalStatusError("initial baseline cannot name a predecessor")
        if self.revision > 1 and self.predecessor_window_id is None:
            raise OperationalStatusError("baseline correction requires its predecessor")


def canonical_uuid(value: str, name: str = "uuid") -> str:
    """Return canonical lowercase hyphenated UUID text or fail closed."""
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as error:
        raise OperationalStatusError(f"{name} must be a canonical UUID") from error
    if str(parsed) != value:
        raise OperationalStatusError(f"{name} must be lowercase canonical UUID text")
    return value


def canonical_authority_bytes(value: object) -> bytes:
    """Encode authority as the byte-exact canonical JSON form."""
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise OperationalStatusError("authority evidence is not canonical JSON") from error


def canonical_authority_digest(value: object) -> str:
    """Hash canonical JSON so internal authority selectors never enter status records."""
    return hashlib.sha256(canonical_authority_bytes(value)).hexdigest()


def derived_uuid(kind: str, *components: str) -> str:
    """Derive a stable v5 identity from the versioned namespace and canonical tuple."""
    if not kind.strip() or not components or any(not component for component in components):
        raise OperationalStatusError("UUID derivation requires nonblank canonical components")
    name = "\x1f".join(("operational-data-flow-v1", kind, *components))
    return str(uuid.uuid5(UUID_NAMESPACE, name))


def reduce_event(
    snapshot: FlowSnapshot | None, event: OperationalDataFlowEvent
) -> ReductionDecision:
    """Classify one event without I/O; only exact contiguous history may advance."""
    if snapshot is None:
        return _reduce_root(event)
    return _reduce_successor(snapshot, event)


def _reduce_root(event: OperationalDataFlowEvent) -> ReductionDecision:
    if event.transition_sequence == 1:
        if event.predecessor_execution_id is not None:
            return ReductionDecision(
                ReductionKind.CONFLICT, reason="root execution has predecessor"
            )
        return ReductionDecision(ReductionKind.APPLY, FlowSnapshot.from_event(event))
    return ReductionDecision(ReductionKind.PARK, reason="predecessor is not applied")


def _reduce_successor(snapshot: FlowSnapshot, event: OperationalDataFlowEvent) -> ReductionDecision:
    if snapshot.flow_id != event.flow_id or snapshot.surface != event.surface:
        return ReductionDecision(ReductionKind.CONFLICT, reason="flow identity or surface changed")
    if event.transition_sequence <= snapshot.latest_sequence:
        if (
            event.transition_sequence == snapshot.latest_sequence
            and event.event_id == snapshot.latest_event_id
        ):
            return ReductionDecision(ReductionKind.ALREADY_APPLIED, snapshot)
        return ReductionDecision(ReductionKind.CONFLICT, reason="history slot is already owned")
    if event.transition_sequence > snapshot.latest_sequence + 1:
        return ReductionDecision(ReductionKind.PARK, reason="predecessor is not applied")
    reason = _successor_conflict(snapshot, event)
    if reason is not None:
        return ReductionDecision(ReductionKind.CONFLICT, reason=reason)
    return ReductionDecision(ReductionKind.APPLY, FlowSnapshot.from_event(event))


def _successor_conflict(snapshot: FlowSnapshot, event: OperationalDataFlowEvent) -> str | None:
    if event.predecessor_event_id != snapshot.latest_event_id:
        return "predecessor event fence differs"
    if event.required_child_flow_ids != snapshot.required_child_flow_ids or (
        event.optional_child_flow_ids != snapshot.optional_child_flow_ids
    ):
        return "registered child set changed"
    if event.clock_started_at != snapshot.clock_started_at:
        return "flow clock changed"
    if event.evaluation_binding != snapshot.evaluation_binding:
        return "evaluation binding changed"
    return _execution_conflict(snapshot, event)


def _execution_conflict(snapshot: FlowSnapshot, event: OperationalDataFlowEvent) -> str | None:
    if event.execution_id == snapshot.execution_id:
        if event.predecessor_execution_id != snapshot.predecessor_execution_id:
            return "execution lineage changed"
        if event.progress is not None and snapshot.state in {
            FlowState.BLOCKED,
            FlowState.FAILED,
            FlowState.COMPLETED,
            FlowState.SUPERSEDED,
        }:
            return "progress followed terminal state"
        return None
    if event.predecessor_execution_id != snapshot.execution_id:
        return "retry skipped its predecessor execution"
    return None


def _attempt_groups(history: tuple[AttemptEvent, ...]) -> list[list[AttemptEvent]]:
    """Group exact history into per-execution STARTED-led pairs or fail closed."""
    if not history:
        raise OperationalStatusError("attempt history is empty")
    groups: list[list[AttemptEvent]] = []
    for source in history:
        if source.sequence == 0:
            groups.append([source])
        elif not groups or groups[-1][0].attempt_id != source.attempt_id:
            raise OperationalStatusError("terminal attempt event has no adjacent STARTED event")
        else:
            groups[-1].append(source)
    if any(len(group) not in {1, 2} for group in groups):
        raise OperationalStatusError("attempt history contains an invalid event count")
    for index, group in enumerate(groups):
        started = group[0]
        if started.kind is not AttemptEventKind.STARTED:
            raise OperationalStatusError(
                "attempt history does not begin each execution with STARTED"
            )
        expected_predecessor = None if index == 0 else groups[index - 1][0].attempt_id
        if started.predecessor != expected_predecessor:
            raise OperationalStatusError("attempt predecessor history is not exact")
        if any(event.partition != groups[0][0].partition for event in group):
            raise OperationalStatusError("attempt execution changed its partition")
        if len(group) == _EVENTS_WITH_TERMINAL and group[1].occurred_at < started.occurred_at:
            raise OperationalStatusError("terminal attempt predates STARTED")
    return groups


def map_attempt_history(history: tuple[AttemptEvent, ...]) -> tuple[OperationalDataFlowEvent, ...]:
    """Map exact oldest-to-newest attempt history into stable status events."""
    groups = _attempt_groups(history)
    root = groups[0][0]
    root_digest = canonical_authority_digest(
        {"attempt_id": root.attempt_id.value, "partition": root.partition.identity}
    )
    flow_id = derived_uuid("flow", "semantic_candidate_publication", root_digest)
    started_at = root.occurred_at.astimezone(UTC)
    mapped: list[OperationalDataFlowEvent] = []
    prior_event_id: str | None = None
    prior_execution_id: str | None = None
    sequence = 0
    for group in groups:
        execution_digest = canonical_authority_digest(
            {"attempt_id": group[0].attempt_id.value, "partition": group[0].partition.identity}
        )
        execution_id = derived_uuid("execution", flow_id, execution_digest)
        for source in group:
            sequence += 1
            state, outcome, stage, reason_code = _attempt_disposition(source.kind)
            digest = _attempt_authority_digest(source)
            event_id = derived_uuid(
                "event",
                "semantic_candidate_publication",
                digest,
                str(sequence),
                state.value,
            )
            mapped.append(
                OperationalDataFlowEvent(
                    event_id=event_id,
                    predecessor_event_id=prior_event_id,
                    flow_id=flow_id,
                    execution_id=execution_id,
                    predecessor_execution_id=prior_execution_id,
                    transition_sequence=sequence,
                    surface="semantic_candidate_publication",
                    stage=stage,
                    state=state,
                    outcome=outcome,
                    occurred_at=source.occurred_at.astimezone(UTC),
                    clock_started_at=started_at,
                    authority=AuthorityObservation(
                        kind="semantic_attempt",
                        opaque_digest=digest,
                        observed_at=source.occurred_at.astimezone(UTC),
                    ),
                    reason_code=reason_code,
                    evaluation_binding=EvaluationBinding(
                        MEASUREMENT_REFS["semantic_candidate_publication"]
                    ),
                )
            )
            prior_event_id = event_id
        prior_execution_id = execution_id
    return tuple(mapped)


def event_document(event: OperationalDataFlowEvent) -> dict[str, object]:
    """Serialize exactly the closed queue/projection event shape."""
    return {
        "schema_version": event.schema_version,
        "registry_version": event.registry_version,
        "event_id": event.event_id,
        "predecessor_event_id": event.predecessor_event_id,
        "flow_id": event.flow_id,
        "parent_flow_id": event.parent_flow_id,
        "causation_event_id": event.causation_event_id,
        "execution_id": event.execution_id,
        "predecessor_execution_id": event.predecessor_execution_id,
        "transition_sequence": event.transition_sequence,
        "surface": event.surface,
        "stage": event.stage,
        "state": event.state.value,
        "outcome": None if event.outcome is None else event.outcome.value,
        "occurred_at": _iso(event.occurred_at),
        "clock_started_at": None
        if event.clock_started_at is None
        else _iso(event.clock_started_at),
        "required_child_flow_ids": list(event.required_child_flow_ids),
        "optional_child_flow_ids": list(event.optional_child_flow_ids),
        "progress": (
            None
            if event.progress is None
            else {
                "completed": event.progress.completed,
                "total": event.progress.total,
                "unit": event.progress.unit,
            }
        ),
        "authority": {
            "kind": event.authority.kind,
            "opaque_digest": event.authority.opaque_digest,
            "observed_at": _iso(event.authority.observed_at),
        },
        "reason_code": event.reason_code,
        "evaluation_binding": {
            "mode": event.evaluation_binding.mode,
            "measurement_refs": [
                {"measurement_ref": reference, "measurement_version": version}
                for reference, version in event.evaluation_binding.measurement_refs
            ],
        },
    }


def event_from_document(value: object) -> OperationalDataFlowEvent:
    """Parse the exact event shape; unknown or attainment-like fields are rejected."""
    document = _mapping(value, "event")
    if frozenset(document) != EVENT_FIELDS:
        raise OperationalStatusError("event fields differ from the closed v1 contract")
    _reject_forbidden_fields(document)
    authority = _mapping(document["authority"], "authority")
    if frozenset(authority) != frozenset({"kind", "opaque_digest", "observed_at"}):
        raise OperationalStatusError("authority fields differ from the closed v1 contract")
    binding = _mapping(document["evaluation_binding"], "evaluation_binding")
    if frozenset(binding) != frozenset({"mode", "measurement_refs"}):
        raise OperationalStatusError("evaluation binding fields differ from observation-only v1")
    references = tuple(
        _measurement_reference(item)
        for item in _sequence(binding["measurement_refs"], "measurement_refs")
    )
    progress_raw = document["progress"]
    progress = None
    if progress_raw is not None:
        progress_data = _mapping(progress_raw, "progress")
        if frozenset(progress_data) != frozenset({"completed", "total", "unit"}):
            raise OperationalStatusError("progress fields differ from v1")
        progress = Progress(
            _integer(progress_data["completed"], "completed"),
            _integer(progress_data["total"], "total"),
            _text(progress_data["unit"], "unit"),
        )
    outcome_raw = document["outcome"]
    return OperationalDataFlowEvent(
        schema_version=_text(document["schema_version"], "schema_version"),
        registry_version=_text(document["registry_version"], "registry_version"),
        event_id=_text(document["event_id"], "event_id"),
        predecessor_event_id=_optional_text(document["predecessor_event_id"]),
        flow_id=_text(document["flow_id"], "flow_id"),
        parent_flow_id=_optional_text(document["parent_flow_id"]),
        causation_event_id=_optional_text(document["causation_event_id"]),
        execution_id=_text(document["execution_id"], "execution_id"),
        predecessor_execution_id=_optional_text(document["predecessor_execution_id"]),
        transition_sequence=_integer(document["transition_sequence"], "transition_sequence"),
        surface=_text(document["surface"], "surface"),
        stage=_text(document["stage"], "stage"),
        state=FlowState(_text(document["state"], "state")),
        outcome=None if outcome_raw is None else CompletionOutcome(_text(outcome_raw, "outcome")),
        occurred_at=_moment(document["occurred_at"], "occurred_at"),
        clock_started_at=(
            None
            if document["clock_started_at"] is None
            else _moment(document["clock_started_at"], "clock_started_at")
        ),
        required_child_flow_ids=_uuid_sequence(
            document["required_child_flow_ids"], "required_child_flow_ids"
        ),
        optional_child_flow_ids=_uuid_sequence(
            document["optional_child_flow_ids"], "optional_child_flow_ids"
        ),
        progress=progress,
        authority=AuthorityObservation(
            kind=_text(authority["kind"], "authority.kind"),
            opaque_digest=_text(authority["opaque_digest"], "authority.opaque_digest"),
            observed_at=_moment(authority["observed_at"], "authority.observed_at"),
        ),
        reason_code=_optional_text(document["reason_code"]),
        evaluation_binding=EvaluationBinding(
            measurement_refs=references,
            mode=_text(binding["mode"], "evaluation_binding.mode"),
        ),
    )


@dataclass(frozen=True, slots=True)
class BaselineFoldRequest:
    """One closed observation period folded for one versioned measurement."""

    measurement_ref: str
    measurement_version: str
    window_start: datetime
    window_end: datetime
    finalized_at: datetime
    sample_count: int

    def __post_init__(self) -> None:
        if not self.measurement_ref or not self.measurement_version:
            raise OperationalStatusError("baseline fold requires a versioned measurement")


def initial_baseline(request: BaselineFoldRequest) -> BaselineWindow:
    """Create the first immutable descriptive window over one closed observation period."""
    return BaselineWindow(
        window_id=derived_uuid(
            "baseline",
            request.measurement_ref,
            request.measurement_version,
            _iso(request.window_start),
            _iso(request.window_end),
            "1",
        ),
        predecessor_window_id=None,
        revision=1,
        measurement_ref=request.measurement_ref,
        measurement_version=request.measurement_version,
        window_start=request.window_start,
        window_end=request.window_end,
        population_cutoff=request.finalized_at,
        finalized_at=request.finalized_at,
        completeness="complete",
        late_sample_count=0,
        sample_count=request.sample_count,
    )


def window_sample_count(
    contributions: tuple[OperationalDataFlowEvent, ...],
    window_start: datetime,
    window_end: datetime,
) -> int:
    """Count terminal, clock-bearing samples whose completion instant closed in the window."""
    return sum(
        1
        for event in contributions
        if event.outcome is not None
        and event.clock_started_at is not None
        and window_start <= event.occurred_at < window_end
    )


def corrected_baseline(
    previous: BaselineWindow, *, finalized_at: datetime, sample_count: int
) -> BaselineWindow:
    """Create an immutable successor correction instead of mutating the closed window."""
    _utc(finalized_at, "finalized_at")
    if finalized_at < previous.finalized_at or sample_count < previous.sample_count:
        raise OperationalStatusError("baseline correction cannot move authority backward")
    revision = previous.revision + 1
    identity = derived_uuid(
        "baseline",
        previous.measurement_ref,
        previous.measurement_version,
        _iso(previous.window_start),
        _iso(previous.window_end),
        str(revision),
    )
    return replace(
        previous,
        window_id=identity,
        predecessor_window_id=previous.window_id,
        revision=revision,
        finalized_at=finalized_at,
        late_sample_count=previous.late_sample_count + (sample_count - previous.sample_count),
        sample_count=sample_count,
    )


def definitions_document() -> dict[str, object]:
    """Return the generated observation-only usage guide with no attainment fields."""
    return {
        "schema_version": SCHEMA_VERSION,
        "registry_version": REGISTRY_VERSION,
        "observation_policy": OBSERVATION_POLICY,
        "canonical_uuid": "lowercase_hyphenated",
        "relationships": {
            "flow_id": "logical movement activity",
            "execution_id": "one attempt within the flow",
            "predecessor_execution_id": "immediately prior attempt",
            "event_id": "immutable transition",
            "predecessor_event_id": "immediately prior transition",
            "parent_flow_id": "enclosing flow",
            "causation_event_id": "transition that created or triggered work",
        },
        "evaluation_modes": ["observation_only"],
        "states": [state.value for state in FlowState],
        "reasons": [
            "candidate_skipped",
            "candidate_rejected",
            "candidate_quarantined",
            "candidate_failed",
        ],
        "next_actions": [
            "observe",
            "inspect_diagnostic",
            "retry_via_owner",
            "escalate",
            "no_self_service",
        ],
        "surfaces": [dict(surface) for surface in SURFACES],
    }


def _attempt_disposition(
    kind: AttemptEventKind,
) -> tuple[FlowState, CompletionOutcome | None, str, str | None]:
    mapping = {
        AttemptEventKind.STARTED: (FlowState.RUNNING, None, "candidate_started", None),
        AttemptEventKind.SUCCEEDED: (
            FlowState.COMPLETED,
            CompletionOutcome.SUCCEEDED,
            "candidate_published",
            None,
        ),
        AttemptEventKind.SKIPPED: (
            FlowState.COMPLETED,
            CompletionOutcome.COMPLETED_NO_CHANGE,
            "candidate_skipped",
            "candidate_skipped",
        ),
        AttemptEventKind.REJECTED: (
            FlowState.COMPLETED,
            CompletionOutcome.COMPLETED_REFUSED,
            "candidate_rejected",
            "candidate_rejected",
        ),
        AttemptEventKind.QUARANTINED: (
            FlowState.BLOCKED,
            CompletionOutcome.COMPLETED_REFUSED,
            "candidate_quarantined",
            "candidate_quarantined",
        ),
        AttemptEventKind.FAILED: (
            FlowState.FAILED,
            CompletionOutcome.EXHAUSTED,
            "candidate_failed",
            "candidate_failed",
        ),
    }
    return mapping[kind]


def _attempt_authority_digest(event: AttemptEvent) -> str:
    return canonical_authority_digest(
        {
            "attempt_id": event.attempt_id.value,
            "partition": event.partition.identity,
            "kind": event.kind.value,
            "occurred_at": _iso(event.occurred_at),
            "sequence": event.sequence,
            "predecessor_attempt_id": (
                None if event.predecessor is None else event.predecessor.value
            ),
            "reason": event.reason,
        }
    )


def _children(values: tuple[str, ...], name: str) -> None:
    if (
        len(values) > MAX_CHILDREN
        or tuple(sorted(values)) != values
        or len(values) != len(set(values))
    ):
        raise OperationalStatusError(f"{name} children must be bounded, unique, and sorted")
    for value in values:
        canonical_uuid(value, f"{name}_child")


def _utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise OperationalStatusError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _iso(value: datetime) -> str:
    return _utc(value, "instant").isoformat()


def _moment(value: object, name: str) -> datetime:
    text = _text(value, name)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as error:
        raise OperationalStatusError(f"{name} must be an ISO instant") from error
    return _utc(parsed, name)


def _mapping(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise OperationalStatusError(f"{name} must be an object")
    return cast("dict[str, object]", value)


def _sequence(value: object, name: str) -> list[object]:
    if not isinstance(value, list):
        raise OperationalStatusError(f"{name} must be an array")
    return cast("list[object]", value)


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OperationalStatusError(f"{name} must be nonblank text")
    return value


def _optional_text(value: object) -> str | None:
    return None if value is None else _text(value, "optional field")


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise OperationalStatusError(f"{name} must be an integer")
    return value


def _uuid_sequence(value: object, name: str) -> tuple[str, ...]:
    values = tuple(_text(item, name) for item in _sequence(value, name))
    for item in values:
        canonical_uuid(item, name)
    return values


def _measurement_reference(value: object) -> tuple[str, str]:
    item = _mapping(value, "measurement reference")
    if frozenset(item) != frozenset({"measurement_ref", "measurement_version"}):
        raise OperationalStatusError("measurement reference fields differ from v1")
    return (
        _text(item["measurement_ref"], "measurement_ref"),
        _text(item["measurement_version"], "measurement_version"),
    )


def _reject_forbidden_fields(value: object) -> None:
    if isinstance(value, dict):
        for key, child in cast("dict[object, object]", value).items():
            if isinstance(key, str) and any(part in key.lower() for part in FORBIDDEN_FIELD_PARTS):
                raise OperationalStatusError(
                    "attainment fields are forbidden in observation-only v1"
                )
            _reject_forbidden_fields(child)
    elif isinstance(value, list):
        for child in cast("list[object]", value):
            _reject_forbidden_fields(child)
