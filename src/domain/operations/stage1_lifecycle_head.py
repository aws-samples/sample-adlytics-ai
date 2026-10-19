# SPDX-License-Identifier: MIT-0
"""Pure ADR-075 Stage-1 lifecycle-head proofs, execution succession, and replay classification.

This module is the opaque-proof constructor named by Load Path write discipline §3.1: it derives
every public identity, key, prior/resulting head tuple, retained ``EVENT#`` attribute input, and
outbox attribute that ``DynamoStage1LifecycleHeadStore`` persists. No Load Path caller supplies or
reproduces any of these values.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final

from domain.operations.stage1_operational_status import (
    EventIdentityRequest,
    InlineIdentityRequest,
    OutboxState,
    PlanIdentityRequest,
    PlanRequiredChildExpectations,
    PointerIdentityRequest,
    PriorOwnerEventSource,
    ReattestStage1EventRequest,
    Stage1AuthorityKind,
    Stage1EventAuthorityRequest,
    Stage1EventIdentityMember,
    Stage1FlowFamily,
    Stage1IdentityResult,
    Stage1OutboxAuthoritySnapshot,
    Stage1OutboxOrderRequest,
    Stage1RetainedEventProof,
    Stage1Surface,
    Stage1TopologyRequest,
    Stage1TopologyResult,
    TopologySourceKind,
    UnitIdentityRequest,
    VerifiedStage1EventAuthority,
    derive_event_id,
    derive_inline_identity,
    derive_plan_identity,
    derive_pointer_identity,
    derive_stage1_topology,
    derive_unit_identity,
    event_authority_digest,
    reattest_stage1_event,
    stage1_authority_time,
    stage1_outbox_order,
)
from domain.operations.status import (
    MEASUREMENT_REFS,
    AuthorityObservation,
    CompletionOutcome,
    EvaluationBinding,
    FlowState,
    OperationalDataFlowEvent,
    OperationalStatusError,
    Progress,
    canonical_authority_bytes,
    canonical_authority_digest,
    canonical_uuid,
    event_document,
)

HEAD_CONTRACT_VERSION: Final = 1
HEAD_RECORD_KEY: Final = "HEAD"
STATUS_FLOW_PREFIX: Final = "STATUS_FLOW#"
EVENT_RECORD_PREFIX: Final = "EVENT#"
COMPOUND_TERMINAL_TRANSITION: Final = "semantic_plan_terminal"
COMPOUND_SUCCESSOR_TRANSITION: Final = "semantic_plan_recorded"
AUTHORITY_KIND_BY_FAMILY: Final = {
    Stage1FlowFamily.PLAN: "write_plan_meta",
    Stage1FlowFamily.UNIT: "write_plan_unit",
    Stage1FlowFamily.INLINE: "inline_table_run",
    Stage1FlowFamily.POINTER: "current_servable_pointer",
}
_SHA256_HEX_DIGITS: Final = 64
_ROOT_KINDS_BY_FAMILY: Final = {
    Stage1FlowFamily.PLAN: frozenset({TopologySourceKind.LEGITIMATE_ROOT}),
    Stage1FlowFamily.UNIT: frozenset({TopologySourceKind.PLAN_ADMISSION}),
    Stage1FlowFamily.INLINE: frozenset({TopologySourceKind.LEGITIMATE_ROOT}),
    Stage1FlowFamily.POINTER: frozenset(
        {TopologySourceKind.SAME_TRANSACTION_TERMINAL, TopologySourceKind.SEMANTIC_ATTEMPT_SUCCESS}
    ),
}


class Stage1LifecycleHeadInvariantError(OperationalStatusError):
    """A Stage-1 lifecycle head or event authority violates its closed contract."""


def _invariant(detail: str) -> Stage1LifecycleHeadInvariantError:
    return Stage1LifecycleHeadInvariantError(
        f"Stage-1 lifecycle head proof violates its invariant: {detail}"
    )


@dataclass(frozen=True, slots=True)
class Stage1LifecycleHeadTuple:
    """The complete closed 12-field lifecycle ``HEAD`` item; no other attribute exists."""

    surface: Stage1Surface
    flow_id: str
    current_execution_id: str
    current_internal_execution_id: str
    predecessor_execution_id: str | None
    latest_event_id: str
    latest_authority_digest: str
    latest_transition_sequence: int
    latest_state: FlowState
    parent_flow_id: str | None
    causation_event_id: str | None
    head_contract_version: int = HEAD_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.head_contract_version != HEAD_CONTRACT_VERSION:
            raise _invariant("head contract version is not active")
        try:
            surface = Stage1Surface(self.surface)
            state = FlowState(self.latest_state)
        except ValueError as error:
            raise _invariant("unknown head surface or lifecycle state") from error
        object.__setattr__(self, "surface", surface)
        object.__setattr__(self, "latest_state", state)
        if surface is Stage1Surface.POINTER:
            raise _invariant("pointer flows are headless")
        for name in (
            "flow_id",
            "current_execution_id",
            "predecessor_execution_id",
            "latest_event_id",
            "parent_flow_id",
            "causation_event_id",
        ):
            value: str | None = getattr(self, name)
            if value is not None:
                canonical_uuid(value, name.replace("_", " "))
        _nonblank(self.current_internal_execution_id, "current internal execution id")
        _digest_text(self.latest_authority_digest, "latest authority digest")
        if self.latest_transition_sequence < 1:
            raise _invariant("head transition sequence must be positive")
        if self.predecessor_execution_id == self.current_execution_id:
            raise _invariant("head predecessor execution repeats the current execution")
        if self.latest_transition_sequence == 1 and self.predecessor_execution_id is not None:
            raise _invariant("a root head cannot name a predecessor execution")
        if (self.parent_flow_id is None) != (self.causation_event_id is None):
            raise _invariant("head parent and causation are immutable together")


def stage1_head_authority_document(head: Stage1LifecycleHeadTuple) -> dict[str, object]:
    """Render the complete old-head authority consumed by topology and head transitions."""
    return {
        "head_contract_version": head.head_contract_version,
        "surface": head.surface.value,
        "flow_id": head.flow_id,
        "current_execution_id": head.current_execution_id,
        "current_internal_execution_id": head.current_internal_execution_id,
        "predecessor_execution_id": head.predecessor_execution_id,
        "latest_event_id": head.latest_event_id,
        "latest_authority_digest": head.latest_authority_digest,
        "latest_transition_sequence": head.latest_transition_sequence,
        "latest_state": head.latest_state.value,
        "parent_flow_id": head.parent_flow_id,
        "causation_event_id": head.causation_event_id,
    }


@dataclass(frozen=True, slots=True)
class Stage1LifecycleTransition:
    """One owner transition's closed identity members."""

    transition_id: str
    lifecycle_state: FlowState
    completion_outcome: CompletionOutcome | None
    typed_reason: str | None

    def __post_init__(self) -> None:
        _nonblank(self.transition_id, "transition id")
        try:
            state = FlowState(self.lifecycle_state)
            outcome = (
                None
                if self.completion_outcome is None
                else CompletionOutcome(self.completion_outcome)
            )
        except ValueError as error:
            raise _invariant("unknown lifecycle state or completion outcome") from error
        object.__setattr__(self, "lifecycle_state", state)
        object.__setattr__(self, "completion_outcome", outcome)


@dataclass(frozen=True, slots=True)
class Stage1LifecycleProofRequest:
    """Owner inputs for one ordinary Stage-1 lifecycle transition proof."""

    identity: Stage1EventIdentityMember
    internal_key: tuple[str | int, ...]
    topology: Stage1TopologyRequest
    transition: Stage1LifecycleTransition
    stage: str
    authority_at: datetime
    prior_head: Stage1LifecycleHeadTuple | None
    clock_started_at: datetime | None = None
    progress: Progress | None = None
    owner_generation: int | None = None
    required_child_expectations: PlanRequiredChildExpectations | None = None
    publication_partition_key: str | None = None

    def __post_init__(self) -> None:
        _nonblank(self.stage, "stage")
        _aware(self.authority_at, "authority_at")
        if self.clock_started_at is not None:
            _aware(self.clock_started_at, "clock_started_at")
        if self.prior_head is not None and self.clock_started_at is None:
            raise _invariant("a successor transition requires the flow's durable root clock")
        if self.prior_head is None and self.clock_started_at is not None:
            raise _invariant("a root transition's clock is its own occurred_at, never supplied")
        if isinstance(self.identity, PointerIdentityRequest):
            if self.publication_partition_key is None:
                raise _invariant("pointer proof requires its publication partition key")
            _nonblank(self.publication_partition_key, "publication partition key")
            if self.publication_partition_key.startswith(STATUS_FLOW_PREFIX):
                raise _invariant("pointer events never occupy a STATUS_FLOW# partition")
            if self.prior_head is not None:
                raise _invariant("pointer flows are headless single-transition roots")
        elif self.publication_partition_key is not None:
            raise _invariant("publication partition belongs only to pointer proofs")


@dataclass(frozen=True, slots=True)
class Stage1LifecycleEventFragment:
    """Complete write inputs for one retained ``EVENT#`` authority and its outbox."""

    record_key: str
    reattest: ReattestStage1EventRequest
    authority_digest: str
    payload: str
    outbox: Stage1OutboxAuthoritySnapshot


@dataclass(frozen=True, slots=True)
class Stage1LifecycleProof:
    """The opaque typed lifecycle/event/``HEAD`` proof consumed by the head store."""

    family: Stage1FlowFamily
    surface: Stage1Surface
    flow_id: str
    execution_id: str
    event_id: str
    authority_digest: str
    partition_key: str
    head_record_key: str | None
    prior_head: Stage1LifecycleHeadTuple | None
    resulting_head: Stage1LifecycleHeadTuple | None
    events: tuple[Stage1LifecycleEventFragment, ...]


def derive_stage1_lifecycle_proof(request: Stage1LifecycleProofRequest) -> Stage1LifecycleProof:
    """Derive one ordinary transition's complete proof before its state transaction."""
    identity = _member_identity(request.identity)
    topology = _validated_topology(request, identity)
    succession = _succession(request.prior_head, identity, request.identity)
    partition_key = _partition_key(identity, request.publication_partition_key)
    fragment = _fragment(
        _FragmentInputs(
            identity=identity,
            identity_member=request.identity,
            internal_key=request.internal_key,
            transition=request.transition,
            stage=request.stage,
            authority_at=request.authority_at,
            clock_started_at=request.clock_started_at,
            progress=request.progress,
            owner_generation=request.owner_generation,
            required_child_expectations=request.required_child_expectations,
            partition_key=partition_key,
            succession=succession,
            parent_flow_id=topology.parent_flow_id,
            causation_event_id=topology.causation_event_id,
        )
    )
    resulting_head = _resulting_head(request, identity, topology, succession, fragment)
    return Stage1LifecycleProof(
        family=identity.family,
        surface=identity.surface,
        flow_id=identity.flow_id,
        execution_id=succession.execution_id,
        event_id=fragment.reattest.proof.event_id,
        authority_digest=fragment.authority_digest,
        partition_key=partition_key,
        head_record_key=None if identity.family is Stage1FlowFamily.POINTER else HEAD_RECORD_KEY,
        prior_head=request.prior_head,
        resulting_head=resulting_head,
        events=(fragment,),
    )


@dataclass(frozen=True, slots=True)
class Stage1CompoundReplacementRequest:
    """Owner inputs for the sole compound planned-plan replacement transition."""

    plan_digest: str
    prior_head: Stage1LifecycleHeadTuple
    source_internal_key: tuple[str | int, ...]
    target_internal_key: tuple[str | int, ...]
    successor_internal_delivery_execution_id: str
    terminal_transition: Stage1LifecycleTransition
    successor_transition: Stage1LifecycleTransition
    source_expectations: PlanRequiredChildExpectations
    target_expectations: PlanRequiredChildExpectations
    authority_at: datetime
    clock_started_at: datetime
    terminal_stage: str = "projection_plan_terminal"
    successor_stage: str = "projection_plan_recorded"

    def __post_init__(self) -> None:
        _nonblank(self.plan_digest, "plan digest")
        _nonblank(self.successor_internal_delivery_execution_id, "successor execution selector")
        _nonblank(self.terminal_stage, "terminal stage")
        _nonblank(self.successor_stage, "successor stage")
        _aware(self.authority_at, "authority_at")
        _aware(self.clock_started_at, "clock_started_at")
        if self.prior_head.surface is not Stage1Surface.PLAN:
            raise _invariant("compound replacement belongs only to plan flows")
        if self.terminal_transition.transition_id != COMPOUND_TERMINAL_TRANSITION or (
            self.terminal_transition.lifecycle_state is not FlowState.SUPERSEDED
            or self.terminal_transition.completion_outcome is not CompletionOutcome.SUPERSEDED
        ):
            raise _invariant("compound predecessor must be the superseded plan terminal")
        if self.successor_transition.transition_id != COMPOUND_SUCCESSOR_TRANSITION or (
            self.successor_transition.lifecycle_state is not FlowState.QUEUED
            or self.successor_transition.completion_outcome is not None
        ):
            raise _invariant("compound successor must be the queued recorded transition")
        if (
            self.successor_internal_delivery_execution_id
            == self.prior_head.current_internal_execution_id
        ):
            raise _invariant("compound successor requires a new internal execution selector")


def compound_replacement_proof(request: Stage1CompoundReplacementRequest) -> Stage1LifecycleProof:
    """Derive the two contiguous events and the single ``N→N+2`` head advance."""
    prior = request.prior_head
    terminal_member = PlanIdentityRequest(request.plan_digest, prior.current_internal_execution_id)
    successor_member = PlanIdentityRequest(
        request.plan_digest, request.successor_internal_delivery_execution_id
    )
    terminal_identity = _member_identity(terminal_member)
    successor_identity = _member_identity(successor_member)
    if terminal_identity.flow_id != prior.flow_id:
        raise _invariant("compound plan digest names another flow")
    if terminal_identity.execution_id != prior.current_execution_id:
        raise _invariant("persisted head execution disagrees with recomputation")
    partition_key = f"{STATUS_FLOW_PREFIX}{prior.flow_id}"
    terminal = _fragment(
        _FragmentInputs(
            identity=terminal_identity,
            identity_member=terminal_member,
            internal_key=request.source_internal_key,
            transition=request.terminal_transition,
            stage=request.terminal_stage,
            authority_at=request.authority_at,
            clock_started_at=request.clock_started_at,
            progress=None,
            owner_generation=None,
            required_child_expectations=request.source_expectations,
            partition_key=partition_key,
            succession=_Succession(
                execution_id=prior.current_execution_id,
                predecessor_execution_id=prior.predecessor_execution_id,
                transition_sequence=prior.latest_transition_sequence + 1,
                predecessor_event_id=prior.latest_event_id,
            ),
            parent_flow_id=prior.parent_flow_id,
            causation_event_id=prior.latest_event_id,
        )
    )
    successor = _fragment(
        _FragmentInputs(
            identity=successor_identity,
            identity_member=successor_member,
            internal_key=request.target_internal_key,
            transition=request.successor_transition,
            stage=request.successor_stage,
            authority_at=request.authority_at,
            clock_started_at=request.clock_started_at,
            progress=None,
            owner_generation=None,
            required_child_expectations=request.target_expectations,
            partition_key=partition_key,
            succession=_Succession(
                execution_id=successor_identity.execution_id,
                predecessor_execution_id=prior.current_execution_id,
                transition_sequence=prior.latest_transition_sequence + 2,
                predecessor_event_id=terminal.reattest.proof.event_id,
            ),
            parent_flow_id=prior.parent_flow_id,
            causation_event_id=prior.latest_event_id,
        )
    )
    resulting_head = Stage1LifecycleHeadTuple(
        surface=Stage1Surface.PLAN,
        flow_id=prior.flow_id,
        current_execution_id=successor_identity.execution_id,
        current_internal_execution_id=request.successor_internal_delivery_execution_id,
        predecessor_execution_id=prior.current_execution_id,
        latest_event_id=successor.reattest.proof.event_id,
        latest_authority_digest=successor.authority_digest,
        latest_transition_sequence=prior.latest_transition_sequence + 2,
        latest_state=request.successor_transition.lifecycle_state,
        parent_flow_id=prior.parent_flow_id,
        causation_event_id=prior.causation_event_id,
    )
    return Stage1LifecycleProof(
        family=Stage1FlowFamily.PLAN,
        surface=Stage1Surface.PLAN,
        flow_id=prior.flow_id,
        execution_id=successor_identity.execution_id,
        event_id=successor.reattest.proof.event_id,
        authority_digest=successor.authority_digest,
        partition_key=partition_key,
        head_record_key=HEAD_RECORD_KEY,
        prior_head=prior,
        resulting_head=resulting_head,
        events=(terminal, successor),
    )


class Stage1ReplayClassification(StrEnum):
    """Closed strong-reread conflict classifications."""

    BYTE_IDENTICAL = "byte_identical"
    ADVANCED_THROUGH_CONTIGUOUS_CHAIN = "advanced_through_contiguous_chain"
    CONFLICT = "conflict"


@dataclass(frozen=True, slots=True)
class Stage1PersistedEventObservation:
    """One strongly reread retained ``EVENT#`` item's immutable members."""

    reattest: ReattestStage1EventRequest
    payload: str
    immutable_payload_digest: str


@dataclass(frozen=True, slots=True)
class Stage1ReplayObservation:
    """Strong-reread evidence aligned with one proof's event keys and head."""

    persisted_events: tuple[Stage1PersistedEventObservation | None, ...]
    persisted_head: Stage1LifecycleHeadTuple | None
    later_events: tuple[VerifiedStage1EventAuthority, ...] = ()


def classify_stage1_replay(
    proof: Stage1LifecycleProof, observation: Stage1ReplayObservation
) -> Stage1ReplayClassification:
    """Classify one strong reread as byte-exact replay, later-advanced replay, or conflict."""
    if not _events_byte_identical(proof, observation):
        return Stage1ReplayClassification.CONFLICT
    resulting = proof.resulting_head
    head = observation.persisted_head
    if resulting is None:
        return (
            Stage1ReplayClassification.CONFLICT
            if head is not None
            else Stage1ReplayClassification.BYTE_IDENTICAL
        )
    if head is None:
        return Stage1ReplayClassification.CONFLICT
    if head == resulting:
        return Stage1ReplayClassification.BYTE_IDENTICAL
    if _advanced_through_chain(resulting, head, observation.later_events):
        return Stage1ReplayClassification.ADVANCED_THROUGH_CONTIGUOUS_CHAIN
    return Stage1ReplayClassification.CONFLICT


def _events_byte_identical(
    proof: Stage1LifecycleProof, observation: Stage1ReplayObservation
) -> bool:
    if len(observation.persisted_events) != len(proof.events):
        return False
    return all(
        persisted is not None
        and persisted.payload == expected.payload
        and persisted.immutable_payload_digest == expected.outbox.immutable_payload_digest
        and persisted.reattest == expected.reattest
        for expected, persisted in zip(proof.events, observation.persisted_events, strict=True)
    )


def _advanced_through_chain(
    resulting: Stage1LifecycleHeadTuple,
    head: Stage1LifecycleHeadTuple,
    chain: tuple[VerifiedStage1EventAuthority, ...],
) -> bool:
    if (
        head.surface is not resulting.surface
        or head.flow_id != resulting.flow_id
        or head.parent_flow_id != resulting.parent_flow_id
        or head.causation_event_id != resulting.causation_event_id
        or head.latest_transition_sequence <= resulting.latest_transition_sequence
    ):
        return False
    expected_length = head.latest_transition_sequence - resulting.latest_transition_sequence
    if len(chain) != expected_length:
        return False
    previous_event_id = resulting.latest_event_id
    previous_sequence = resulting.latest_transition_sequence
    for link in chain:
        if (
            link.surface is not resulting.surface
            or link.flow_id != resulting.flow_id
            or link.transition_sequence != previous_sequence + 1
            or link.predecessor_event_id != previous_event_id
        ):
            return False
        previous_event_id = link.event_id
        previous_sequence = link.transition_sequence
    final = chain[-1]
    return (
        final.event_id == head.latest_event_id
        and final.authority_digest == head.latest_authority_digest
        and final.lifecycle_state is head.latest_state
    )


@dataclass(frozen=True, slots=True)
class _Succession:
    """One head-decided execution, sequence, and predecessor assignment."""

    execution_id: str
    predecessor_execution_id: str | None
    transition_sequence: int
    predecessor_event_id: str | None


@dataclass(frozen=True, slots=True)
class _FragmentInputs:
    """Shared members for one event-fragment derivation."""

    identity: Stage1IdentityResult
    identity_member: Stage1EventIdentityMember
    internal_key: tuple[str | int, ...]
    transition: Stage1LifecycleTransition
    stage: str
    authority_at: datetime
    clock_started_at: datetime | None
    progress: Progress | None
    owner_generation: int | None
    required_child_expectations: PlanRequiredChildExpectations | None
    partition_key: str
    succession: _Succession
    parent_flow_id: str | None
    causation_event_id: str | None


def _fragment(inputs: _FragmentInputs) -> Stage1LifecycleEventFragment:
    transition = inputs.transition
    authority = Stage1EventAuthorityRequest(
        surface=inputs.identity.surface,
        internal_key=inputs.internal_key,
        transition_id=transition.transition_id,
        lifecycle_state=transition.lifecycle_state,
        completion_outcome=transition.completion_outcome,
        typed_reason=transition.typed_reason,
        authority_time=stage1_authority_time(inputs.authority_at),
    )
    digest = event_authority_digest(authority)
    event_id = derive_event_id(
        EventIdentityRequest(
            inputs.identity.surface,
            digest,
            inputs.succession.transition_sequence,
            transition.lifecycle_state,
        )
    )
    proof = Stage1RetainedEventProof(
        flow_id=inputs.identity.flow_id,
        execution_id=inputs.succession.execution_id,
        event_id=event_id,
        transition_sequence=inputs.succession.transition_sequence,
        predecessor_event_id=inputs.succession.predecessor_event_id,
        predecessor_execution_id=inputs.succession.predecessor_execution_id,
        parent_flow_id=inputs.parent_flow_id,
        causation_event_id=inputs.causation_event_id,
    )
    reattest = ReattestStage1EventRequest(
        authority,
        inputs.identity_member,
        proof,
        inputs.owner_generation,
        inputs.required_child_expectations,
    )
    reattest_stage1_event(reattest)
    record_key = f"{EVENT_RECORD_PREFIX}{digest}"
    document = event_document(_wire_event(inputs, proof, digest))
    payload = canonical_authority_bytes(document).decode("ascii")
    payload_digest = canonical_authority_digest(document)
    outbox = Stage1OutboxAuthoritySnapshot(
        partition_key=inputs.partition_key,
        record_key=record_key,
        immutable_payload_digest=payload_digest,
        authority_kind=Stage1AuthorityKind.LIFECYCLE_EVENT,
        outbox_state=OutboxState.PENDING,
        consecutive_failed_delivery_attempts=0,
        outbox_order=stage1_outbox_order(
            Stage1OutboxOrderRequest(
                inputs.partition_key, record_key, payload_digest, inputs.authority_at
            )
        ),
    )
    return Stage1LifecycleEventFragment(record_key, reattest, digest, payload, outbox)


def _wire_event(
    inputs: _FragmentInputs, proof: Stage1RetainedEventProof, digest: str
) -> OperationalDataFlowEvent:
    surface = inputs.identity.surface
    expectations = inputs.required_child_expectations
    return OperationalDataFlowEvent(
        event_id=proof.event_id,
        predecessor_event_id=proof.predecessor_event_id,
        flow_id=proof.flow_id,
        execution_id=proof.execution_id,
        predecessor_execution_id=proof.predecessor_execution_id,
        transition_sequence=proof.transition_sequence,
        surface=surface.value,
        stage=inputs.stage,
        state=inputs.transition.lifecycle_state,
        occurred_at=inputs.authority_at,
        authority=AuthorityObservation(
            AUTHORITY_KIND_BY_FAMILY[inputs.identity.family], digest, inputs.authority_at
        ),
        evaluation_binding=EvaluationBinding(MEASUREMENT_REFS[surface.value]),
        outcome=inputs.transition.completion_outcome,
        reason_code=inputs.transition.typed_reason,
        parent_flow_id=proof.parent_flow_id,
        causation_event_id=proof.causation_event_id,
        clock_started_at=(
            inputs.authority_at if inputs.clock_started_at is None else inputs.clock_started_at
        ),
        required_child_flow_ids=(
            () if expectations is None else expectations.required_child_flow_ids
        ),
        progress=inputs.progress,
    )


def _member_identity(identity: Stage1EventIdentityMember) -> Stage1IdentityResult:
    if isinstance(identity, PlanIdentityRequest):
        return derive_plan_identity(identity)
    if isinstance(identity, UnitIdentityRequest):
        return derive_unit_identity(identity)
    if isinstance(identity, InlineIdentityRequest):
        return derive_inline_identity(identity)
    return derive_pointer_identity(identity)


def _internal_execution_selector(identity: Stage1EventIdentityMember) -> str:
    if isinstance(identity, PlanIdentityRequest):
        return identity.internal_delivery_execution_id
    if isinstance(identity, UnitIdentityRequest):
        return identity.writer_execution_id
    if isinstance(identity, InlineIdentityRequest):
        return identity.internal_execution_id
    return identity.execution_source.internal_pointer_execution_id


def _partition_key(identity: Stage1IdentityResult, publication_partition_key: str | None) -> str:
    if identity.family is Stage1FlowFamily.POINTER:
        if publication_partition_key is None:
            raise _invariant("pointer proof requires its publication partition key")
        return publication_partition_key
    return f"{STATUS_FLOW_PREFIX}{identity.flow_id}"


def _validated_topology(
    request: Stage1LifecycleProofRequest, identity: Stage1IdentityResult
) -> Stage1TopologyResult:
    topology = derive_stage1_topology(request.topology)
    prior = request.prior_head
    if prior is None:
        if topology.topology_source_kind not in _ROOT_KINDS_BY_FAMILY[identity.family]:
            raise _invariant("root topology source does not belong to this family")
        return topology
    source = request.topology.source
    if topology.topology_source_kind is not TopologySourceKind.PRIOR_OWNER_EVENT or not isinstance(
        source, PriorOwnerEventSource
    ):
        raise _invariant("a successor transition requires the prior-owner-event source")
    if (
        topology.parent_flow_id != prior.parent_flow_id
        or topology.causation_event_id != prior.latest_event_id
        or source.complete_old_head_authority != stage1_head_authority_document(prior)
    ):
        raise _invariant("topology source disagrees with the complete prior head")
    if prior.flow_id != identity.flow_id or prior.surface is not identity.surface:
        raise _invariant("prior head belongs to another flow")
    return topology


def _succession(
    prior: Stage1LifecycleHeadTuple | None,
    identity: Stage1IdentityResult,
    member: Stage1EventIdentityMember,
) -> _Succession:
    if prior is None:
        return _Succession(identity.execution_id, None, 1, None)
    selector = _internal_execution_selector(member)
    if selector == prior.current_internal_execution_id:
        if identity.execution_id != prior.current_execution_id:
            raise _invariant("persisted head execution disagrees with recomputation")
        return _Succession(
            prior.current_execution_id,
            prior.predecessor_execution_id,
            prior.latest_transition_sequence + 1,
            prior.latest_event_id,
        )
    if identity.execution_id == prior.current_execution_id:
        raise _invariant("a new internal selector cannot reuse the persisted execution")
    return _Succession(
        identity.execution_id,
        prior.current_execution_id,
        prior.latest_transition_sequence + 1,
        prior.latest_event_id,
    )


def _resulting_head(  # noqa: PLR0913, PLR0917 -- private proof assembler
    request: Stage1LifecycleProofRequest,
    identity: Stage1IdentityResult,
    topology: Stage1TopologyResult,
    succession: _Succession,
    fragment: Stage1LifecycleEventFragment,
) -> Stage1LifecycleHeadTuple | None:
    if identity.family is Stage1FlowFamily.POINTER:
        return None
    prior = request.prior_head
    parent_flow_id = topology.parent_flow_id if prior is None else prior.parent_flow_id
    causation_event_id = topology.causation_event_id if prior is None else prior.causation_event_id
    return Stage1LifecycleHeadTuple(
        surface=identity.surface,
        flow_id=identity.flow_id,
        current_execution_id=succession.execution_id,
        current_internal_execution_id=_internal_execution_selector(request.identity),
        predecessor_execution_id=succession.predecessor_execution_id,
        latest_event_id=fragment.reattest.proof.event_id,
        latest_authority_digest=fragment.authority_digest,
        latest_transition_sequence=succession.transition_sequence,
        latest_state=request.transition.lifecycle_state,
        parent_flow_id=parent_flow_id,
        causation_event_id=causation_event_id,
    )


def _nonblank(value: str, name: str) -> str:
    if not value.strip():
        raise _invariant(f"{name} must be nonblank text")
    return value


def _digest_text(value: str, name: str) -> str:
    if len(value) != _SHA256_HEX_DIGITS or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise _invariant(f"{name} must be canonical lowercase sha256")
    return value


def _aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise _invariant(f"{name} must be timezone-aware")
    return value
