# SPDX-License-Identifier: MIT-0
"""Pure ADR-075 Stage-1 operational-status identities and authority decisions."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final

from domain.operations.status import (
    CompletionOutcome,
    EvaluationBinding,
    Evidence,
    FlowState,
    OperationalDataFlowEvent,
    OperationalStatusError,
    canonical_authority_digest,
    canonical_uuid,
    derived_uuid,
)

SCHEMA_VERSION: Final = "1"
TOPOLOGY_CONTRACT: Final = "stage1-topology-v1"
CHILD_OUTCOME_REDUCTION_VERSION: Final = 2
OUTBOX_FAILURE_THRESHOLD: Final = 3
KEY_DECIMAL_DIGITS: Final = 20
SHA256_HEX_DIGITS: Final = 64
OUTBOX_ORDER_PARTS: Final = 2
UUID4_VERSION: Final = 4
MAX_KEY_INTEGER: Final = 10**KEY_DECIMAL_DIGITS - 1
MAX_REASON_LENGTH: Final = 256


class Stage1Surface(StrEnum):
    """The four and only four Stage-1 status surfaces."""

    INLINE_RUN = "semantic_inline_table_run"
    PLAN = "semantic_projection_plan"
    UNIT = "semantic_projection_unit"
    POINTER = "semantic_servable_pointer"


ALL_STAGE1_SURFACES: Final = tuple(sorted(Stage1Surface, key=lambda value: value.value))


class Stage1FlowFamily(StrEnum):
    """Closed Stage-1 owner families."""

    PLAN = "plan"
    UNIT = "unit"
    INLINE = "inline_run"
    POINTER = "pointer"


SURFACE_BY_FAMILY: Final = {
    Stage1FlowFamily.PLAN: Stage1Surface.PLAN,
    Stage1FlowFamily.UNIT: Stage1Surface.UNIT,
    Stage1FlowFamily.INLINE: Stage1Surface.INLINE_RUN,
    Stage1FlowFamily.POINTER: Stage1Surface.POINTER,
}


class PointerExecutionSourceKind(StrEnum):
    """Closed internal selectors for one pointer execution."""

    QUEUED_UNIT_WRITER_EXECUTION = "queued_unit_writer_execution"
    INLINE_RUN_EXECUTION = "inline_run_execution"
    SEMANTIC_PUBLICATION_ATTEMPT = "semantic_publication_attempt"


@dataclass(frozen=True, slots=True)
class PointerExecutionSource:
    """An internal-only pointer execution selector."""

    kind: PointerExecutionSourceKind
    internal_pointer_execution_id: str

    def __post_init__(self) -> None:
        try:
            normalized = PointerExecutionSourceKind(self.kind)
        except ValueError as error:
            raise OperationalStatusError("unknown pointer execution source kind") from error
        object.__setattr__(self, "kind", normalized)
        _text(self.internal_pointer_execution_id, "internal pointer execution id")


@dataclass(frozen=True, slots=True)
class Stage1IdentityResult:
    """Derived public flow and execution identity for one fixed family."""

    family: Stage1FlowFamily
    surface: Stage1Surface
    flow_id: str
    execution_id: str


@dataclass(frozen=True, slots=True)
class PlanIdentityRequest:
    """Exact plan tuple and delivery execution selector."""

    plan_digest: str
    internal_delivery_execution_id: str

    def __post_init__(self) -> None:
        _text(self.plan_digest, "plan digest")
        _text(self.internal_delivery_execution_id, "internal delivery execution id")


@dataclass(frozen=True, slots=True)
class UnitIdentityRequest:
    """Exact queued-unit tuple and writer execution selector."""

    plan_digest: str
    unit_key: str
    writer_execution_id: str

    def __post_init__(self) -> None:
        _text(self.plan_digest, "plan digest")
        _text(self.unit_key, "unit key")
        _text(self.writer_execution_id, "writer execution id")


@dataclass(frozen=True, slots=True)
class InlineIdentityRequest:
    """Exact inline-run tuple and immutable execution selector."""

    writer_kind: str
    operation_id: str
    physical_table_id: str
    internal_execution_id: str

    def __post_init__(self) -> None:
        _text(self.writer_kind, "writer kind")
        _text(self.operation_id, "operation id")
        _text(self.physical_table_id, "physical table id")
        _text(self.internal_execution_id, "internal execution id")


@dataclass(frozen=True, slots=True)
class PointerIdentityRequest:
    """Exact publication-pointer tuple and closed execution selector."""

    partition_identity: str
    publication_id: str
    fencing_token: int
    execution_source: PointerExecutionSource

    def __post_init__(self) -> None:
        _text(self.partition_identity, "partition identity")
        _text(self.publication_id, "publication id")
        _unsigned(self.fencing_token, "fencing token")


@dataclass(frozen=True, slots=True)
class EventIdentityRequest:
    """Exact public event identity members."""

    surface: Stage1Surface
    authority_digest: str
    transition_sequence: int
    state: FlowState

    def __post_init__(self) -> None:
        _digest(self.authority_digest, "authority digest")
        if _positive(self.transition_sequence, "transition sequence") > MAX_KEY_INTEGER:
            raise OperationalStatusError("transition sequence exceeds 20 decimal digits")


def derive_plan_identity(request: PlanIdentityRequest) -> Stage1IdentityResult:
    """Derive the fixed plan flow and execution UUIDs."""
    return _identity(
        Stage1FlowFamily.PLAN,
        [Stage1FlowFamily.PLAN.value, request.plan_digest],
        request.internal_delivery_execution_id,
    )


def derive_unit_identity(request: UnitIdentityRequest) -> Stage1IdentityResult:
    """Derive the fixed queued-unit flow and execution UUIDs."""
    return _identity(
        Stage1FlowFamily.UNIT,
        [Stage1FlowFamily.UNIT.value, request.plan_digest, request.unit_key],
        request.writer_execution_id,
    )


def derive_inline_identity(request: InlineIdentityRequest) -> Stage1IdentityResult:
    """Derive the fixed inline-run flow and execution UUIDs."""
    return _identity(
        Stage1FlowFamily.INLINE,
        [
            Stage1FlowFamily.INLINE.value,
            request.writer_kind,
            request.operation_id,
            request.physical_table_id,
        ],
        request.internal_execution_id,
    )


def derive_pointer_identity(request: PointerIdentityRequest) -> Stage1IdentityResult:
    """Derive the fixed headless pointer flow and execution UUIDs."""
    return _identity(
        Stage1FlowFamily.POINTER,
        [request.partition_identity, request.publication_id, request.fencing_token],
        request.execution_source.internal_pointer_execution_id,
    )


def derive_event_id(request: EventIdentityRequest) -> str:
    """Derive an event UUID without exposing internal owner selectors."""
    return derived_uuid(
        "event",
        request.surface.value,
        request.authority_digest,
        str(request.transition_sequence),
        request.state.value,
    )


def _identity(
    family: Stage1FlowFamily, identity_tuple: list[object], internal_execution_id: str
) -> Stage1IdentityResult:
    surface = SURFACE_BY_FAMILY[family]
    flow_id = derived_uuid("flow", surface.value, canonical_authority_digest(identity_tuple))
    execution_id = derived_uuid(
        "execution", flow_id, canonical_authority_digest([internal_execution_id])
    )
    return Stage1IdentityResult(family, surface, flow_id, execution_id)


class TopologySourceKind(StrEnum):
    """Closed Stage-1 topology source kinds."""

    LEGITIMATE_ROOT = "legitimate_root"
    PRIOR_OWNER_EVENT = "prior_owner_event"
    PLAN_ADMISSION = "plan_admission"
    SAME_TRANSACTION_TERMINAL = "same_transaction_terminal"
    SEMANTIC_ATTEMPT_SUCCESS = "semantic_attempt_success"


class RootDiscriminator(StrEnum):
    """Legitimate Stage-1 root authorities."""

    AUTOMATIC_INGESTION_PLAN = "automatic_ingestion_plan"
    TRANSITIONAL_DELIVERY_INLINE = "transitional_delivery_inline"
    INGEST_DELIVERY_OPERATOR_INLINE = "ingest_delivery_operator_inline"
    REPROCESS_SEMANTIC_V2_OPERATOR_INLINE = "reprocess_semantic_v2_operator_inline"
    GRAPH_UNIT_INLINE = "graph_unit_inline"


class Stage1OwnerTransition(StrEnum):
    """Owner transitions whose topology source is fixed by ADR-075."""

    FIRST_AUTOMATIC_PLAN_RECORDED = "first_automatic_plan_recorded"
    LATER_PLAN_EVENT = "later_plan_event"
    FIRST_TRANSITIONAL_INLINE_RECORDED = "first_transitional_inline_recorded"
    FIRST_INGEST_OPERATOR_INLINE_RECORDED = "first_ingest_operator_inline_recorded"
    FIRST_REPROCESS_INLINE_RECORDED = "first_reprocess_inline_recorded"
    FIRST_GRAPH_UNIT_INLINE_RECORDED = "first_graph_unit_inline_recorded"
    LATER_INLINE_EVENT = "later_inline_event"
    FIRST_UNIT_STARTED = "first_unit_started"
    LATER_UNIT_EVENT = "later_unit_event"
    QUEUED_NONZERO_POINTER = "queued_nonzero_pointer"
    INLINE_NONZERO_POINTER = "inline_nonzero_pointer"
    ZERO_ROW_POINTER = "zero_row_pointer"


@dataclass(frozen=True, slots=True)
class LegitimateRootSource:
    """A closed root source with no relationship or source digest."""

    root_discriminator: RootDiscriminator

    def __post_init__(self) -> None:
        try:
            normalized = RootDiscriminator(self.root_discriminator)
        except ValueError as error:
            raise OperationalStatusError("unknown Stage-1 root discriminator") from error
        object.__setattr__(self, "root_discriminator", normalized)


@dataclass(frozen=True, slots=True)
class PriorOwnerEventSource:
    """Complete prior-head authority for a later owner transition."""

    parent_flow_id: str | None
    latest_event_id: str
    complete_old_head_authority: object

    def __post_init__(self) -> None:
        if self.parent_flow_id is not None:
            canonical_uuid(self.parent_flow_id, "parent flow id")
        canonical_uuid(self.latest_event_id, "latest event id")
        canonical_authority_digest(self.complete_old_head_authority)


@dataclass(frozen=True, slots=True)
class PlanAdmissionSource:
    """Complete plan authority for a first queued-unit transition."""

    plan_flow_id: str
    admitted_event_id: str
    plan_meta_authority: object
    complete_plan_head_authority: object
    admitted_event_authority: object

    def __post_init__(self) -> None:
        canonical_uuid(self.plan_flow_id, "plan flow id")
        canonical_uuid(self.admitted_event_id, "admitted event id")
        canonical_authority_digest(self.plan_meta_authority)
        canonical_authority_digest(self.complete_plan_head_authority)
        canonical_authority_digest(self.admitted_event_authority)


@dataclass(frozen=True, slots=True)
class SameTransactionTerminalSource:
    """Exact terminal event created with a nonzero pointer."""

    parent_flow_id: str
    terminal_event_id: str

    def __post_init__(self) -> None:
        canonical_uuid(self.parent_flow_id, "parent flow id")
        canonical_uuid(self.terminal_event_id, "terminal event id")


@dataclass(frozen=True, slots=True)
class SemanticAttemptSuccessSource:
    """Complete validated successful-attempt authority for a zero-row pointer."""

    parent_flow_id: str
    successful_event_id: str
    complete_success_authority: object

    def __post_init__(self) -> None:
        canonical_uuid(self.parent_flow_id, "parent flow id")
        canonical_uuid(self.successful_event_id, "successful event id")
        canonical_authority_digest(self.complete_success_authority)


type Stage1TopologySource = (
    LegitimateRootSource
    | PriorOwnerEventSource
    | PlanAdmissionSource
    | SameTransactionTerminalSource
    | SemanticAttemptSuccessSource
)


@dataclass(frozen=True, slots=True)
class Stage1TopologyRequest:
    """One owner transition paired with its only permitted source variant."""

    transition: Stage1OwnerTransition
    source: Stage1TopologySource

    def __post_init__(self) -> None:
        try:
            normalized = Stage1OwnerTransition(self.transition)
        except ValueError as error:
            raise OperationalStatusError("unknown Stage-1 owner transition") from error
        object.__setattr__(self, "transition", normalized)


@dataclass(frozen=True, slots=True)
class Stage1TopologyResult:
    """Derived Stage-1 topology with no caller-selected proof fields."""

    topology_contract: str
    topology_source_kind: TopologySourceKind
    root_discriminator: RootDiscriminator | None
    parent_flow_id: str | None
    causation_event_id: str | None
    topology_source_digest: str | None


def derive_stage1_topology(request: Stage1TopologyRequest) -> Stage1TopologyResult:
    """Derive relationships and proof after enforcing the constructive source matrix."""
    source = request.source
    _validate_topology_matrix(request.transition, source)
    if isinstance(source, LegitimateRootSource):
        return Stage1TopologyResult(
            TOPOLOGY_CONTRACT,
            TopologySourceKind.LEGITIMATE_ROOT,
            source.root_discriminator,
            None,
            None,
            None,
        )
    if isinstance(source, PriorOwnerEventSource):
        return Stage1TopologyResult(
            TOPOLOGY_CONTRACT,
            TopologySourceKind.PRIOR_OWNER_EVENT,
            None,
            source.parent_flow_id,
            source.latest_event_id,
            canonical_authority_digest(
                ["stage1-topology-prior-owner-event/v1", source.complete_old_head_authority]
            ),
        )
    if isinstance(source, PlanAdmissionSource):
        return Stage1TopologyResult(
            TOPOLOGY_CONTRACT,
            TopologySourceKind.PLAN_ADMISSION,
            None,
            source.plan_flow_id,
            source.admitted_event_id,
            canonical_authority_digest(
                [
                    "stage1-topology-plan-admission/v1",
                    source.plan_meta_authority,
                    source.complete_plan_head_authority,
                    source.admitted_event_authority,
                ]
            ),
        )
    if isinstance(source, SameTransactionTerminalSource):
        return Stage1TopologyResult(
            TOPOLOGY_CONTRACT,
            TopologySourceKind.SAME_TRANSACTION_TERMINAL,
            None,
            source.parent_flow_id,
            source.terminal_event_id,
            None,
        )
    return Stage1TopologyResult(
        TOPOLOGY_CONTRACT,
        TopologySourceKind.SEMANTIC_ATTEMPT_SUCCESS,
        None,
        source.parent_flow_id,
        source.successful_event_id,
        canonical_authority_digest(
            ["stage1-topology-semantic-attempt-success/v1", source.complete_success_authority]
        ),
    )


def _validate_topology_matrix(
    transition: Stage1OwnerTransition, source: Stage1TopologySource
) -> None:
    roots = {
        Stage1OwnerTransition.FIRST_AUTOMATIC_PLAN_RECORDED: (
            RootDiscriminator.AUTOMATIC_INGESTION_PLAN
        ),
        Stage1OwnerTransition.FIRST_TRANSITIONAL_INLINE_RECORDED: (
            RootDiscriminator.TRANSITIONAL_DELIVERY_INLINE
        ),
        Stage1OwnerTransition.FIRST_INGEST_OPERATOR_INLINE_RECORDED: (
            RootDiscriminator.INGEST_DELIVERY_OPERATOR_INLINE
        ),
        Stage1OwnerTransition.FIRST_REPROCESS_INLINE_RECORDED: (
            RootDiscriminator.REPROCESS_SEMANTIC_V2_OPERATOR_INLINE
        ),
        Stage1OwnerTransition.FIRST_GRAPH_UNIT_INLINE_RECORDED: (
            RootDiscriminator.GRAPH_UNIT_INLINE
        ),
    }
    if transition in roots:
        if (
            not isinstance(source, LegitimateRootSource)
            or source.root_discriminator is not roots[transition]
        ):
            raise OperationalStatusError("topology source does not match the owner transition")
        return
    expected_type: type[object]
    if transition in {
        Stage1OwnerTransition.LATER_PLAN_EVENT,
        Stage1OwnerTransition.LATER_INLINE_EVENT,
        Stage1OwnerTransition.LATER_UNIT_EVENT,
    }:
        expected_type = PriorOwnerEventSource
    elif transition is Stage1OwnerTransition.FIRST_UNIT_STARTED:
        expected_type = PlanAdmissionSource
    elif transition in {
        Stage1OwnerTransition.QUEUED_NONZERO_POINTER,
        Stage1OwnerTransition.INLINE_NONZERO_POINTER,
    }:
        expected_type = SameTransactionTerminalSource
    else:
        expected_type = SemanticAttemptSuccessSource
    if not isinstance(source, expected_type):
        raise OperationalStatusError("topology source does not match the owner transition")


class UsageCategory(StrEnum):
    """Closed usage categories, including categories rejected for Stage 1 settlement."""

    PRODUCTIVE = "productive"
    RETRY = "retry"
    DUPLICATE_WASTE = "duplicate_waste"
    NO_CHANGE = "no_change"
    REFUSED = "refused"
    UNAVAILABLE = "unavailable"
    SUPERSEDED = "superseded"


class OwnerDisposition(StrEnum):
    """Durable Stage-1 owner dispositions that can settle accepted work."""

    SUCCESSFUL = "successful"
    NO_CHANGE = "no_change"
    SUPERSEDED = "superseded"
    RETRYABLE_FAILURE = "retryable_failure"
    EXHAUSTED_OR_UNAVAILABLE = "exhausted_or_unavailable"
    NO_ACCEPTED_REQUEST = "no_accepted_request"


class ProviderTerminalState(StrEnum):
    """Provider states that contain final Athena usage evidence."""

    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True, slots=True)
class AthenaUsage:
    """Complete Athena usage for exactly one accepted request."""

    query_count: int
    scanned_bytes: int
    engine_ms: int

    def __post_init__(self) -> None:
        if self.query_count != 1:
            raise OperationalStatusError("Stage-1 Athena query count must be exactly one")
        _uint64(self.scanned_bytes, "scanned bytes")
        _uint64(self.engine_ms, "engine milliseconds")


@dataclass(frozen=True, slots=True)
class KnownAthenaUsage:
    """Known Athena evidence when final quantities are unavailable."""

    query_count: int
    scanned_bytes: int | None = None
    engine_ms: int | None = None

    def __post_init__(self) -> None:
        if self.query_count != 1:
            raise OperationalStatusError("Stage-1 Athena query count must be exactly one")
        if self.scanned_bytes is not None:
            _uint64(self.scanned_bytes, "scanned bytes")
        if self.engine_ms is not None:
            _uint64(self.engine_ms, "engine milliseconds")


@dataclass(frozen=True, slots=True)
class OperationalDataFlowUsageContribution:
    """One final ordinary Stage-1 Athena contribution."""

    contribution_id: str
    flow_id: str
    execution_id: str
    evaluation_binding: EvaluationBinding
    category: UsageCategory
    usage: AthenaUsage
    observed_at: datetime
    resource: str = "athena"
    reconciles_disposition_id: str | None = None

    def __post_init__(self) -> None:
        canonical_uuid(self.contribution_id, "contribution id")
        canonical_uuid(self.flow_id, "flow id")
        canonical_uuid(self.execution_id, "execution id")
        _utc(self.observed_at, "observed_at")
        if self.resource != "athena":
            raise OperationalStatusError("Stage-1 usage authority must be Athena")
        if self.reconciles_disposition_id is not None:
            raise OperationalStatusError("Stage-1 reconciliation deltas are forbidden")
        if self.category in {UsageCategory.DUPLICATE_WASTE, UsageCategory.REFUSED}:
            raise OperationalStatusError("usage category is not emitted by Stage-1 settlement")


@dataclass(frozen=True, slots=True)
class OperationalDataFlowUsageEvidenceDisposition:
    """One final unavailable-evidence Stage-1 Athena disposition."""

    disposition_id: str
    flow_id: str
    execution_id: str
    evaluation_binding: EvaluationBinding
    category: UsageCategory
    known_usage: KnownAthenaUsage
    missing_quantities: tuple[str, ...]
    observed_at: datetime
    resource: str = "athena"
    evidence: Evidence = Evidence.UNAVAILABLE

    def __post_init__(self) -> None:
        canonical_uuid(self.disposition_id, "disposition id")
        canonical_uuid(self.flow_id, "flow id")
        canonical_uuid(self.execution_id, "execution id")
        _utc(self.observed_at, "observed_at")
        if self.resource != "athena":
            raise OperationalStatusError("Stage-1 usage authority must be Athena")
        if self.evidence is not Evidence.UNAVAILABLE:
            raise OperationalStatusError("Stage-1 incomplete usage is permanently unavailable")
        expected = tuple(
            name
            for name, value in (
                ("scanned_bytes", self.known_usage.scanned_bytes),
                ("engine_ms", self.known_usage.engine_ms),
            )
            if value is None
        )
        if not expected or self.missing_quantities != expected:
            raise OperationalStatusError(
                "missing Athena quantities must match final known evidence"
            )
        if self.category in {UsageCategory.DUPLICATE_WASTE, UsageCategory.REFUSED}:
            raise OperationalStatusError("usage category is not emitted by Stage-1 settlement")


@dataclass(frozen=True, slots=True)
class SettleStage1AthenaUsageRequest:
    """Final owner disposition and provider evidence for one attempt."""

    attempt_id: str
    flow_id: str
    execution_id: str
    evaluation_binding: EvaluationBinding
    owner_disposition: OwnerDisposition
    observed_at: datetime
    provider_terminal_state: ProviderTerminalState | None = None
    scanned_bytes: int | None = None
    engine_ms: int | None = None
    accepted: bool = True
    recovery_or_redrive_execution: bool = False
    athena_usage_state: str = "unsettled"
    resource: str = "athena"
    reconciles_disposition_id: str | None = None
    is_delta: bool = False

    def __post_init__(self) -> None:
        _text(self.attempt_id, "attempt id")
        try:
            disposition = OwnerDisposition(self.owner_disposition)
            provider_state = (
                None
                if self.provider_terminal_state is None
                else ProviderTerminalState(self.provider_terminal_state)
            )
        except ValueError as error:
            raise OperationalStatusError(
                "unknown Stage-1 usage disposition or provider state"
            ) from error
        object.__setattr__(self, "owner_disposition", disposition)
        object.__setattr__(self, "provider_terminal_state", provider_state)
        canonical_uuid(self.flow_id, "flow id")
        canonical_uuid(self.execution_id, "execution id")
        _utc(self.observed_at, "observed_at")
        if self.resource != "athena":
            raise OperationalStatusError("Stage-1 usage authority must be Athena")
        if self.athena_usage_state != "unsettled":
            raise OperationalStatusError("only unsettled Stage-1 usage can be settled")
        if self.reconciles_disposition_id is not None or self.is_delta:
            raise OperationalStatusError("Stage-1 reconciliation deltas are forbidden")
        if self.scanned_bytes is not None:
            _uint64(self.scanned_bytes, "scanned bytes")
        if self.engine_ms is not None:
            _uint64(self.engine_ms, "engine milliseconds")
        if not self.accepted:
            if self.owner_disposition is not OwnerDisposition.NO_ACCEPTED_REQUEST:
                raise OperationalStatusError("unaccepted work requires the no-request disposition")
            if self.provider_terminal_state is not None or any(
                value is not None for value in (self.scanned_bytes, self.engine_ms)
            ):
                raise OperationalStatusError("unaccepted work cannot carry provider usage")
        elif self.provider_terminal_state is None:
            raise OperationalStatusError(
                "accepted usage must be provider-terminal before settlement"
            )
        elif self.owner_disposition is OwnerDisposition.NO_ACCEPTED_REQUEST:
            raise OperationalStatusError("accepted work requires a final owner disposition")


type Stage1UsageAuthority = (
    OperationalDataFlowUsageContribution | OperationalDataFlowUsageEvidenceDisposition
)


@dataclass(frozen=True, slots=True)
class SettleStage1AthenaUsageResult:
    """The one final authority, or absence when no provider request was accepted."""

    authority: Stage1UsageAuthority | None


def settle_stage1_athena_usage(
    request: SettleStage1AthenaUsageRequest,
) -> SettleStage1AthenaUsageResult:
    """Settle accepted Athena work exactly once under owner-disposition precedence."""
    if not request.accepted:
        return SettleStage1AthenaUsageResult(None)
    category = _usage_category(request)
    if request.scanned_bytes is not None and request.engine_ms is not None:
        contribution = OperationalDataFlowUsageContribution(
            contribution_id=derived_uuid(
                "usage", "stage1-provider-attempt", request.attempt_id, category.value
            ),
            flow_id=request.flow_id,
            execution_id=request.execution_id,
            evaluation_binding=request.evaluation_binding,
            category=category,
            usage=AthenaUsage(1, request.scanned_bytes, request.engine_ms),
            observed_at=request.observed_at,
        )
        return SettleStage1AthenaUsageResult(contribution)
    known = KnownAthenaUsage(1, request.scanned_bytes, request.engine_ms)
    disposition = OperationalDataFlowUsageEvidenceDisposition(
        disposition_id=derived_uuid(
            "usage-evidence", "stage1-provider-attempt", request.attempt_id
        ),
        flow_id=request.flow_id,
        execution_id=request.execution_id,
        evaluation_binding=request.evaluation_binding,
        category=category,
        known_usage=known,
        missing_quantities=tuple(
            name
            for name, value in (
                ("scanned_bytes", request.scanned_bytes),
                ("engine_ms", request.engine_ms),
            )
            if value is None
        ),
        observed_at=request.observed_at,
    )
    return SettleStage1AthenaUsageResult(disposition)


def _usage_category(request: SettleStage1AthenaUsageRequest) -> UsageCategory:
    if request.recovery_or_redrive_execution:
        return UsageCategory.RETRY
    mapping = {
        OwnerDisposition.SUCCESSFUL: UsageCategory.PRODUCTIVE,
        OwnerDisposition.NO_CHANGE: UsageCategory.NO_CHANGE,
        OwnerDisposition.SUPERSEDED: UsageCategory.SUPERSEDED,
        OwnerDisposition.RETRYABLE_FAILURE: UsageCategory.RETRY,
        OwnerDisposition.EXHAUSTED_OR_UNAVAILABLE: UsageCategory.UNAVAILABLE,
    }
    return mapping[request.owner_disposition]


class Stage1AuthorityKind(StrEnum):
    """Closed retained Stage-1 authority kinds."""

    LIFECYCLE_EVENT = "lifecycle_event"
    USAGE_CONTRIBUTION = "usage_contribution"
    USAGE_EVIDENCE_DISPOSITION = "usage_evidence_disposition"


class OutboxState(StrEnum):
    """Retained authority delivery states."""

    PENDING = "pending"
    DELIVERED = "delivered"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class RequeuedFromBlock:
    """Exact generation-specific marker installed only by protected recovery."""

    case_ref: str
    affected_surfaces: tuple[Stage1Surface, ...]

    def __post_init__(self) -> None:
        _uuid4(self.case_ref, "case ref")
        if not self.affected_surfaces or self.affected_surfaces != tuple(
            sorted(set(self.affected_surfaces), key=lambda value: value.value)
        ):
            raise OperationalStatusError("affected surfaces must be nonempty, unique, and sorted")


@dataclass(frozen=True, slots=True)
class Stage1OutboxAuthoritySnapshot:
    """Technology-neutral immutable authority and delivery-state snapshot."""

    partition_key: str
    record_key: str
    immutable_payload_digest: str
    authority_kind: Stage1AuthorityKind
    outbox_state: OutboxState
    consecutive_failed_delivery_attempts: int
    outbox_order: str | None = None
    requeued_from_block: RequeuedFromBlock | None = None
    authority_observed_at: datetime | None = None

    def __post_init__(self) -> None:
        _text(self.partition_key, "partition key")
        _text(self.record_key, "record key")
        try:
            authority_kind = Stage1AuthorityKind(self.authority_kind)
            outbox_state = OutboxState(self.outbox_state)
        except ValueError as error:
            raise OperationalStatusError(
                "unknown Stage-1 authority kind or outbox state"
            ) from error
        object.__setattr__(self, "authority_kind", authority_kind)
        object.__setattr__(self, "outbox_state", outbox_state)
        _digest(self.immutable_payload_digest, "immutable payload digest")
        _unsigned(
            self.consecutive_failed_delivery_attempts,
            "consecutive failed delivery attempts",
        )
        _validate_prefix(self.record_key, self.authority_kind)
        if self.outbox_state is OutboxState.PENDING:
            if self.outbox_order is None:
                raise OperationalStatusError("pending authority requires its exact outbox order")
            _outbox_order(self.outbox_order)
        elif self.outbox_order is not None:
            raise OperationalStatusError("terminal outbox authority cannot remain indexed")
        if self.requeued_from_block is not None and self.outbox_state is not OutboxState.PENDING:
            raise OperationalStatusError("requeue marker belongs only to pending authority")
        if self.authority_observed_at is not None:
            if self.authority_kind is not Stage1AuthorityKind.LIFECYCLE_EVENT:
                raise OperationalStatusError("only lifecycle authority has an observation instant")
            _utc(self.authority_observed_at, "authority_observed_at")


@dataclass(frozen=True, slots=True)
class Stage1OutboxOrderRequest:
    """Durable authority members used to derive sparse-index order."""

    partition_key: str
    record_key: str
    immutable_payload_digest: str
    authority_at: datetime

    def __post_init__(self) -> None:
        _text(self.partition_key, "partition key")
        _text(self.record_key, "record key")
        _digest(self.immutable_payload_digest, "immutable payload digest")
        _utc(self.authority_at, "authority_at")


def stage1_outbox_order(request: Stage1OutboxOrderRequest) -> str:
    """Derive stable order from owner time and opaque immutable authority identity."""
    micros = _epoch_micros(request.authority_at)
    opaque = canonical_authority_digest(
        [request.partition_key, request.record_key, request.immutable_payload_digest]
    )
    return f"{micros:020d}#{opaque}"


@dataclass(frozen=True, slots=True)
class RecordStage1AuthorityObservationRequest:
    """Condition for writing the first lifecycle authority observation instant."""

    authority: Stage1OutboxAuthoritySnapshot
    observed_at: datetime

    def __post_init__(self) -> None:
        _pending(self.authority)
        if self.authority.authority_kind is not Stage1AuthorityKind.LIFECYCLE_EVENT:
            raise OperationalStatusError("only lifecycle authority receives an observation instant")
        _utc(self.observed_at, "observed_at")


@dataclass(frozen=True, slots=True)
class MarkStage1OutboxDeliveredRequest:
    """Condition for send-before-mark delivery and marker consumption."""

    authority: Stage1OutboxAuthoritySnapshot
    delivered_at: datetime

    def __post_init__(self) -> None:
        _pending(self.authority)
        _utc(self.delivered_at, "delivered_at")


class DependencyErrorClass(StrEnum):
    """Retryable outbox dependency failures."""

    QUEUE_SEND_FAILED = "queue_send_failed"
    DELIVERY_MARK_FAILED = "delivery_mark_failed"


class BlockErrorClass(StrEnum):
    """Every failure class that can terminalize an outbox generation."""

    QUEUE_SEND_FAILED = "queue_send_failed"
    DELIVERY_MARK_FAILED = "delivery_mark_failed"
    SCHEMA_VALIDATION_FAILED = "schema_validation_failed"
    IMMUTABLE_DIGEST_MISMATCH = "immutable_digest_mismatch"
    PREFIX_KIND_MISMATCH = "prefix_kind_mismatch"
    FORBIDDEN_STAGE1_DELTA = "forbidden_stage1_delta"
    STATE_INVARIANT_VIOLATION = "state_invariant_violation"


@dataclass(frozen=True, slots=True)
class RecordStage1OutboxDeliveryFailureRequest:
    """Condition for one below-threshold dependency failure."""

    authority: Stage1OutboxAuthoritySnapshot
    failed_at: datetime
    dependency_error_class: DependencyErrorClass

    def __post_init__(self) -> None:
        _pending(self.authority)
        _utc(self.failed_at, "failed_at")
        if self.authority.consecutive_failed_delivery_attempts >= OUTBOX_FAILURE_THRESHOLD - 1:
            raise OperationalStatusError("threshold-reaching failure must block the authority")


@dataclass(frozen=True, slots=True)
class BlockStage1OutboxRequest:
    """Condition for atomic poison closure and protected-case creation."""

    authority: Stage1OutboxAuthoritySnapshot
    blocked_at: datetime
    block_error_class: BlockErrorClass
    selected_surface: Stage1Surface | None
    case_ref: str

    def __post_init__(self) -> None:
        _pending(self.authority)
        _utc(self.blocked_at, "blocked_at")
        _uuid4(self.case_ref, "case ref")
        is_dependency = self.block_error_class in {
            BlockErrorClass.QUEUE_SEND_FAILED,
            BlockErrorClass.DELIVERY_MARK_FAILED,
        }
        if is_dependency and (
            self.authority.consecutive_failed_delivery_attempts != OUTBOX_FAILURE_THRESHOLD - 1
        ):
            raise OperationalStatusError("dependency poison blocks only at the exact threshold")


@dataclass(frozen=True, slots=True)
class Stage1BlockDisposition:
    """Derived safe block facts and complete affected-surface membership."""

    final_failed_delivery_attempts: int
    reason_code: str
    evidence: Evidence
    affected_surfaces: tuple[Stage1Surface, ...]


def block_stage1_outbox(request: BlockStage1OutboxRequest) -> Stage1BlockDisposition:
    """Map poison class and select one fixed surface or the safe four-surface fallback."""
    is_dependency = request.block_error_class in {
        BlockErrorClass.QUEUE_SEND_FAILED,
        BlockErrorClass.DELIVERY_MARK_FAILED,
    }
    final_count = (
        request.authority.consecutive_failed_delivery_attempts + 1
        if is_dependency
        else request.authority.consecutive_failed_delivery_attempts
    )
    return Stage1BlockDisposition(
        final_count,
        "stage1_outbox_blocked_dependency" if is_dependency else "stage1_outbox_blocked_invalid",
        Evidence.INCOMPLETE if is_dependency else Evidence.UNAVAILABLE,
        affected_surfaces(request.selected_surface),
    )


def affected_surfaces(surface: Stage1Surface | None) -> tuple[Stage1Surface, ...]:
    """Return one proven family surface or the exact conservative Stage-1 fallback."""
    return ALL_STAGE1_SURFACES if surface is None else (surface,)


@dataclass(frozen=True, slots=True)
class ProtectedStage1OutboxCase:
    """Protected recovery authority never exposed through the invocation contract."""

    case_ref: str
    authority: Stage1OutboxAuthoritySnapshot
    affected_surfaces: tuple[Stage1Surface, ...]
    blocked_at: datetime
    reason_code: str

    def __post_init__(self) -> None:
        _uuid4(self.case_ref, "case ref")
        if self.authority.outbox_state is not OutboxState.BLOCKED:
            raise OperationalStatusError("protected case must reference blocked authority")
        if not self.affected_surfaces or self.affected_surfaces != tuple(
            sorted(set(self.affected_surfaces), key=lambda value: value.value)
        ):
            raise OperationalStatusError("affected surfaces must be nonempty, unique, and sorted")
        _utc(self.blocked_at, "blocked_at")
        _sanitized_reason(self.reason_code)


@dataclass(frozen=True, slots=True)
class RequeueStage1OutboxRequest:
    """Server-resolved protected authority required for one blocked-to-pending transition."""

    protected_case: ProtectedStage1OutboxCase
    requeued_at: datetime
    resolution_reason: str

    def __post_init__(self) -> None:
        _utc(self.requeued_at, "requeued_at")
        if self.requeued_at < self.protected_case.blocked_at:
            raise OperationalStatusError("requeue instant cannot precede block authority")
        _sanitized_reason(self.resolution_reason)

    @property
    def marker(self) -> RequeuedFromBlock:
        """Return the exact marker recovery installs on the new pending generation."""
        return RequeuedFromBlock(
            self.protected_case.case_ref, self.protected_case.affected_surfaces
        )


class RecoveryOutcome(StrEnum):
    """Closed protected recovery outcomes."""

    REQUEUED = "requeued"
    ALREADY_REQUEUED = "already_requeued"


@dataclass(frozen=True, slots=True)
class Stage1OutboxRecoveryInvocation:
    """The complete non-disclosing protected recovery input contract."""

    case_ref: str
    resolution_reason: str
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise OperationalStatusError("recovery invocation schema version is not active")
        _uuid4(self.case_ref, "case ref")
        _sanitized_reason(self.resolution_reason)


@dataclass(frozen=True, slots=True)
class Stage1OutboxRecoveryResult:
    """The complete non-disclosing protected recovery result contract."""

    case_ref: str
    outcome: RecoveryOutcome
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise OperationalStatusError("recovery result schema version is not active")
        try:
            outcome = RecoveryOutcome(self.outcome)
        except ValueError as error:
            raise OperationalStatusError("unknown recovery outcome") from error
        object.__setattr__(self, "outcome", outcome)
        _uuid4(self.case_ref, "case ref")


@dataclass(frozen=True, slots=True)
class LifecycleEventAuthority:
    """A validated lifecycle member carried by one EVENT authority."""

    event: OperationalDataFlowEvent

    def __post_init__(self) -> None:
        if self.event.surface not in {surface.value for surface in Stage1Surface}:
            raise OperationalStatusError("lifecycle event is not a Stage-1 surface")


@dataclass(frozen=True, slots=True)
class SettledUsageContributionAuthority:
    """A fully owner-disposition-settled ordinary Athena authority."""

    contribution: OperationalDataFlowUsageContribution
    athena_usage_state: str = "settled"
    is_delta: bool = False

    def __post_init__(self) -> None:
        if self.athena_usage_state != "settled":
            raise OperationalStatusError("usage contribution authority is unsettled")
        if self.is_delta:
            raise OperationalStatusError("Stage-1 reconciliation deltas are forbidden")


@dataclass(frozen=True, slots=True)
class SettledUsageEvidenceDispositionAuthority:
    """A fully owner-disposition-settled unavailable Athena authority."""

    disposition: OperationalDataFlowUsageEvidenceDisposition
    athena_usage_state: str = "settled"
    is_delta: bool = False

    def __post_init__(self) -> None:
        if self.athena_usage_state != "settled":
            raise OperationalStatusError("usage evidence authority is unsettled")
        if self.is_delta:
            raise OperationalStatusError("Stage-1 reconciliation deltas are forbidden")


type Stage1AuthorityMember = (
    LifecycleEventAuthority
    | SettledUsageContributionAuthority
    | SettledUsageEvidenceDispositionAuthority
)


@dataclass(frozen=True, slots=True)
class DeliverStage1AuthorityRequest:
    """One pending outbox and exactly one closed authority member."""

    authority: Stage1OutboxAuthoritySnapshot
    delivered_at: datetime
    lifecycle_event: LifecycleEventAuthority | None = None
    usage_contribution: SettledUsageContributionAuthority | None = None
    usage_evidence_disposition: SettledUsageEvidenceDispositionAuthority | None = None

    def __post_init__(self) -> None:
        _pending(self.authority)
        _utc(self.delivered_at, "delivered_at")
        members = (
            self.lifecycle_event,
            self.usage_contribution,
            self.usage_evidence_disposition,
        )
        if sum(member is not None for member in members) != 1:
            raise OperationalStatusError("authority payload must contain exactly one member")


@dataclass(frozen=True, slots=True)
class SendStage1StatusMessageRequest:
    """Technology-neutral closed queue member and FIFO identity."""

    member: Stage1AuthorityMember
    message_group_id: str
    message_deduplication_id: str

    def __post_init__(self) -> None:
        canonical_uuid(self.message_group_id, "message group id")
        canonical_uuid(self.message_deduplication_id, "message deduplication id")


@dataclass(frozen=True, slots=True)
class DeliverStage1AuthorityResult:
    """Exact queue send and conditional delivery-mark requests."""

    send_request: SendStage1StatusMessageRequest
    mark_delivered_request: MarkStage1OutboxDeliveredRequest


def deliver_stage1_authority(
    request: DeliverStage1AuthorityRequest,
) -> DeliverStage1AuthorityResult:
    """Map one exact retained authority without I/O or identity allocation."""
    kind = request.authority.authority_kind
    member: Stage1AuthorityMember
    group_id: str
    deduplication_id: str
    if request.lifecycle_event is not None:
        if kind is not Stage1AuthorityKind.LIFECYCLE_EVENT:
            raise OperationalStatusError("authority prefix/kind/member mismatch")
        member = request.lifecycle_event
        group_id = member.event.flow_id
        deduplication_id = member.event.event_id
    elif request.usage_contribution is not None:
        if kind is not Stage1AuthorityKind.USAGE_CONTRIBUTION:
            raise OperationalStatusError("authority prefix/kind/member mismatch")
        member = request.usage_contribution
        group_id = member.contribution.flow_id
        deduplication_id = member.contribution.contribution_id
    else:
        if kind is not Stage1AuthorityKind.USAGE_EVIDENCE_DISPOSITION:
            raise OperationalStatusError("authority prefix/kind/member mismatch")
        if request.usage_evidence_disposition is None:
            raise OperationalStatusError("authority payload must contain exactly one member")
        member = request.usage_evidence_disposition
        group_id = member.disposition.flow_id
        deduplication_id = member.disposition.disposition_id
    return DeliverStage1AuthorityResult(
        SendStage1StatusMessageRequest(member, group_id, deduplication_id),
        MarkStage1OutboxDeliveredRequest(request.authority, request.delivered_at),
    )


@dataclass(frozen=True, slots=True)
class ChildOutcomeV2:
    """One immutable terminal required-unit outcome."""

    parent_flow_id: str
    child_flow_id: str
    child_event_id: str
    child_transition_sequence: int
    child_execution_id: str
    owner_generation: int
    lifecycle_state: FlowState
    completion_outcome: CompletionOutcome
    child_family: Stage1FlowFamily = Stage1FlowFamily.UNIT
    child_outcome_reduction_version: int = CHILD_OUTCOME_REDUCTION_VERSION

    def __post_init__(self) -> None:
        for name in (
            "parent_flow_id",
            "child_flow_id",
            "child_event_id",
            "child_execution_id",
        ):
            canonical_uuid(getattr(self, name), name.replace("_", " "))
        if self.child_outcome_reduction_version != CHILD_OUTCOME_REDUCTION_VERSION:
            raise OperationalStatusError("child outcome reduction version is not active")
        if self.child_family is not Stage1FlowFamily.UNIT:
            raise OperationalStatusError("only queued units enter Stage-1 child reduction")
        _key_integer(self.child_transition_sequence, "child transition sequence", positive=True)
        _key_integer(self.owner_generation, "owner generation")
        if self.lifecycle_state not in {
            FlowState.BLOCKED,
            FlowState.FAILED,
            FlowState.COMPLETED,
            FlowState.SUPERSEDED,
        }:
            raise OperationalStatusError("child outcome must be terminal")

    @property
    def partition_key(self) -> str:
        """Return the parent-flow reduction partition."""
        return f"FLOW#{self.parent_flow_id}"

    @property
    def history_sort_key(self) -> str:
        """Return the exact immutable history key."""
        return f"CHILD_OUTCOME_V2#{self.child_flow_id}#{self.child_transition_sequence:020d}"

    @property
    def generation_head_sort_key(self) -> str:
        """Return the exact generation-addressed head key."""
        return f"CHILD_OUTCOME_GENERATION_HEAD_V2#{self.child_flow_id}#{self.owner_generation:020d}"

    @property
    def latest_selector_sort_key(self) -> str:
        """Return the API-only latest selector key."""
        return f"CHILD_OUTCOME_LATEST_V2#{self.child_flow_id}"

    @property
    def outcome_digest(self) -> str:
        """Hash the exact nine-member v2 outcome tuple."""
        return canonical_authority_digest(
            [
                "operational-data-flow-child-outcome/v2",
                self.parent_flow_id,
                self.child_flow_id,
                self.child_event_id,
                self.child_transition_sequence,
                self.child_execution_id,
                self.owner_generation,
                self.lifecycle_state.value,
                self.completion_outcome.value,
            ]
        )

    @property
    def selector_tuple(self) -> tuple[int, int]:
        """Return the latest-selector comparison tuple."""
        return (self.owner_generation, self.child_transition_sequence)


class GenerationHeadDecisionKind(StrEnum):
    """Conditional generation-head actions."""

    CREATE = "create"
    ADVANCE = "advance"
    ALREADY_CURRENT = "already_current"
    CONFLICT = "conflict"


@dataclass(frozen=True, slots=True)
class GenerationHeadDecisionRequest:
    """Current exact generation head and candidate with retained contiguity proof."""

    candidate: ChildOutcomeV2
    current: ChildOutcomeV2 | None
    candidate_is_contiguous: bool


@dataclass(frozen=True, slots=True)
class GenerationHeadDecision:
    """Pure conditional action for one generation-addressed head."""

    kind: GenerationHeadDecisionKind
    selected: ChildOutcomeV2 | None


def decide_generation_head(request: GenerationHeadDecisionRequest) -> GenerationHeadDecision:
    """Advance only within one generation to a greater proven-contiguous transition."""
    candidate = request.candidate
    current = request.current
    if current is None:
        return GenerationHeadDecision(
            (
                GenerationHeadDecisionKind.CREATE
                if request.candidate_is_contiguous
                else GenerationHeadDecisionKind.CONFLICT
            ),
            candidate if request.candidate_is_contiguous else None,
        )
    if (current.parent_flow_id, current.child_flow_id, current.owner_generation) != (
        candidate.parent_flow_id,
        candidate.child_flow_id,
        candidate.owner_generation,
    ):
        return GenerationHeadDecision(GenerationHeadDecisionKind.CONFLICT, None)
    if current.child_transition_sequence == candidate.child_transition_sequence:
        if current == candidate:
            return GenerationHeadDecision(GenerationHeadDecisionKind.ALREADY_CURRENT, current)
        return GenerationHeadDecision(GenerationHeadDecisionKind.CONFLICT, None)
    if (
        candidate.child_transition_sequence < current.child_transition_sequence
        or not request.candidate_is_contiguous
    ):
        return GenerationHeadDecision(GenerationHeadDecisionKind.CONFLICT, None)
    return GenerationHeadDecision(GenerationHeadDecisionKind.ADVANCE, candidate)


class LatestSelectorDecisionKind(StrEnum):
    """Conditional API latest-selector actions."""

    CREATE = "create"
    ADVANCE = "advance"
    RETAIN = "retain"
    ALREADY_CURRENT = "already_current"
    CONFLICT = "conflict"


@dataclass(frozen=True, slots=True)
class LatestSelectorDecisionRequest:
    """Current exact selector and one validated generation-head candidate."""

    candidate: ChildOutcomeV2
    current: ChildOutcomeV2 | None = None


@dataclass(frozen=True, slots=True)
class LatestSelectorDecision:
    """Pure lexicographic selector action."""

    kind: LatestSelectorDecisionKind
    selected: ChildOutcomeV2 | None


def decide_latest_selector(request: LatestSelectorDecisionRequest) -> LatestSelectorDecision:
    """Advance solely by the higher owner-generation/transition tuple."""
    candidate = request.candidate
    current = request.current
    if current is None:
        return LatestSelectorDecision(LatestSelectorDecisionKind.CREATE, candidate)
    if (current.parent_flow_id, current.child_flow_id) != (
        candidate.parent_flow_id,
        candidate.child_flow_id,
    ):
        return LatestSelectorDecision(LatestSelectorDecisionKind.CONFLICT, None)
    if candidate.selector_tuple > current.selector_tuple:
        return LatestSelectorDecision(LatestSelectorDecisionKind.ADVANCE, candidate)
    if candidate.selector_tuple < current.selector_tuple:
        return LatestSelectorDecision(LatestSelectorDecisionKind.RETAIN, current)
    if candidate == current:
        return LatestSelectorDecision(LatestSelectorDecisionKind.ALREADY_CURRENT, current)
    return LatestSelectorDecision(LatestSelectorDecisionKind.CONFLICT, None)


@dataclass(frozen=True, slots=True)
class RequiredChildExpectation:
    """One exact queued-unit flow generation required by a plan."""

    child_flow_id: str
    owner_generation: int
    child_family: Stage1FlowFamily = Stage1FlowFamily.UNIT

    def __post_init__(self) -> None:
        canonical_uuid(self.child_flow_id, "child flow id")
        _key_integer(self.owner_generation, "owner generation")
        if self.child_family is not Stage1FlowFamily.UNIT:
            raise OperationalStatusError("plan expectations may name only queued-unit flows")


@dataclass(frozen=True, slots=True)
class PlanRequiredChildExpectations:
    """Sorted generation-bound required-unit authority for one plan root."""

    parent_flow_id: str
    required_child_expectations: tuple[RequiredChildExpectation, ...]
    optional_child_flow_ids: tuple[str, ...] = ()
    child_outcome_reduction_version: int = CHILD_OUTCOME_REDUCTION_VERSION

    def __post_init__(self) -> None:
        canonical_uuid(self.parent_flow_id, "parent flow id")
        if self.child_outcome_reduction_version != CHILD_OUTCOME_REDUCTION_VERSION:
            raise OperationalStatusError("child outcome reduction version is not active")
        if not self.required_child_expectations:
            raise OperationalStatusError("Stage-1 plan requires at least one queued unit")
        if self.optional_child_flow_ids:
            raise OperationalStatusError("Stage-1 plan optional children must be empty")
        expected = tuple(
            sorted(
                self.required_child_expectations,
                key=lambda item: item.child_flow_id.encode("utf-8"),
            )
        )
        if self.required_child_expectations != expected or len(
            {item.child_flow_id for item in self.required_child_expectations}
        ) != len(self.required_child_expectations):
            raise OperationalStatusError(
                "required child expectations must be unique and UTF-8 sorted"
            )

    @property
    def required_child_flow_ids(self) -> tuple[str, ...]:
        """Return the exact public required-child tuple."""
        return tuple(item.child_flow_id for item in self.required_child_expectations)

    @property
    def generation_head_keys(self) -> tuple[tuple[str, str], ...]:
        """Derive every exact generation-head key without another selector."""
        return tuple(
            (
                f"FLOW#{self.parent_flow_id}",
                f"CHILD_OUTCOME_GENERATION_HEAD_V2#{item.child_flow_id}#"
                f"{item.owner_generation:020d}",
            )
            for item in self.required_child_expectations
        )


class PlanReleaseDecisionKind(StrEnum):
    """Closed plan terminal release decisions."""

    RELEASE = "release"
    AWAIT_CHILDREN = "await_children"


@dataclass(frozen=True, slots=True)
class PlanReleaseRequest:
    """One plan terminal and exact expected-generation heads from the bounded read."""

    terminal_state: FlowState
    expectations: PlanRequiredChildExpectations
    generation_heads: tuple[ChildOutcomeV2, ...]

    def __post_init__(self) -> None:
        if self.terminal_state not in {
            FlowState.BLOCKED,
            FlowState.FAILED,
            FlowState.COMPLETED,
            FlowState.SUPERSEDED,
        }:
            raise OperationalStatusError("plan release request must carry a terminal state")
        keys = [
            (head.parent_flow_id, head.child_flow_id, head.owner_generation)
            for head in self.generation_heads
        ]
        if len(keys) != len(set(keys)):
            raise OperationalStatusError("generation head observations must be unique")


@dataclass(frozen=True, slots=True)
class PlanReleaseResult:
    """Release decision and exact unsatisfied required-unit IDs."""

    kind: PlanReleaseDecisionKind
    unsatisfied_child_flow_ids: tuple[str, ...]


def decide_plan_release(request: PlanReleaseRequest) -> PlanReleaseResult:
    """Gate completed plans on exact expected generations, never the latest selector."""
    if request.terminal_state is not FlowState.COMPLETED:
        return PlanReleaseResult(PlanReleaseDecisionKind.RELEASE, ())
    by_key = {
        (head.parent_flow_id, head.child_flow_id, head.owner_generation): head
        for head in request.generation_heads
    }
    unsatisfied = tuple(
        expectation.child_flow_id
        for expectation in request.expectations.required_child_expectations
        if not _eligible_success(
            by_key.get(
                (
                    request.expectations.parent_flow_id,
                    expectation.child_flow_id,
                    expectation.owner_generation,
                )
            )
        )
    )
    return PlanReleaseResult(
        PlanReleaseDecisionKind.AWAIT_CHILDREN if unsatisfied else PlanReleaseDecisionKind.RELEASE,
        unsatisfied,
    )


def _eligible_success(outcome: ChildOutcomeV2 | None) -> bool:
    return outcome is not None and (
        outcome.lifecycle_state is FlowState.COMPLETED
        and outcome.completion_outcome is CompletionOutcome.SUCCEEDED
    )


EVENT_AUTHORITY_CONTRACT: Final = "stage1-event-authority/v1"
TERMINAL_FLOW_STATES: Final = frozenset(
    {FlowState.BLOCKED, FlowState.FAILED, FlowState.COMPLETED, FlowState.SUPERSEDED}
)
_POINTER_INTERNAL_KEY_MEMBERS: Final = 3
_INTERNAL_KEY_TEXT_ARITY: Final = {
    Stage1Surface.PLAN: 1,
    Stage1Surface.UNIT: 2,
    Stage1Surface.INLINE_RUN: 4,
}


class Stage1AuthorityReattestationError(OperationalStatusError):
    """A retained Stage-1 authority failed exact identity re-attestation."""


def _reattestation_rejection(detail: str) -> Stage1AuthorityReattestationError:
    return Stage1AuthorityReattestationError(
        f"retained Stage-1 event authority violates its state invariant: {detail}"
    )


@dataclass(frozen=True, slots=True)
class Stage1EventAuthorityRequest:
    """Immutable owner transition members named by the one event authority constructor."""

    surface: Stage1Surface
    internal_key: tuple[str | int, ...]
    transition_id: str
    lifecycle_state: FlowState
    completion_outcome: CompletionOutcome | None
    typed_reason: str | None
    authority_time: str

    def __post_init__(self) -> None:
        try:
            surface = Stage1Surface(self.surface)
            lifecycle_state = FlowState(self.lifecycle_state)
            outcome = (
                None
                if self.completion_outcome is None
                else CompletionOutcome(self.completion_outcome)
            )
        except ValueError as error:
            raise OperationalStatusError("unknown Stage-1 event authority member") from error
        object.__setattr__(self, "surface", surface)
        object.__setattr__(self, "lifecycle_state", lifecycle_state)
        object.__setattr__(self, "completion_outcome", outcome)
        _validate_internal_key(surface, self.internal_key)
        _text(self.transition_id, "transition id")
        if lifecycle_state in TERMINAL_FLOW_STATES:
            if outcome is None:
                raise OperationalStatusError("terminal event authority requires its outcome")
        elif outcome is not None:
            raise OperationalStatusError("nonterminal event authority cannot carry an outcome")
        if self.typed_reason is not None:
            _text(self.typed_reason, "typed reason")
        if len(self.authority_time) != KEY_DECIMAL_DIGITS or any(
            character not in "0123456789" for character in self.authority_time
        ):
            raise OperationalStatusError(
                "authority time must be exact 20-digit epoch-microsecond text"
            )


def _validate_internal_key(surface: Stage1Surface, members: tuple[str | int, ...]) -> None:
    if surface is Stage1Surface.POINTER:
        if len(members) != _POINTER_INTERNAL_KEY_MEMBERS:
            raise OperationalStatusError("pointer internal key must contain exactly three members")
        _text(members[0], "partition identity")
        _text(members[1], "publication id")
        _unsigned(members[2], "fencing token")
        return
    if len(members) != _INTERNAL_KEY_TEXT_ARITY[surface] or any(
        not isinstance(member, str) for member in members
    ):
        raise OperationalStatusError("internal key members differ from the closed family array")
    for member in members:
        _text(member, "internal key member")


def event_authority_tuple(request: Stage1EventAuthorityRequest) -> list[object]:
    """Construct the one byte-exact event authority tuple over owner transition fields."""
    return [
        EVENT_AUTHORITY_CONTRACT,
        request.surface.value,
        list(request.internal_key),
        request.transition_id,
        [
            request.lifecycle_state.value,
            None if request.completion_outcome is None else request.completion_outcome.value,
            request.typed_reason,
        ],
        request.authority_time,
    ]


def event_authority_digest(request: Stage1EventAuthorityRequest) -> str:
    """Hash the exact event authority tuple; no other identity constructor exists."""
    return canonical_authority_digest(event_authority_tuple(request))


def stage1_authority_time(authority_at: datetime) -> str:
    """Render the durable owner instant as 20-digit UTC epoch-microsecond authority time."""
    return f"{_epoch_micros(authority_at):020d}"


@dataclass(frozen=True, slots=True)
class Stage1RetainedEventProof:
    """Owner-persisted public identity proof carried by one retained EVENT# item."""

    flow_id: str
    execution_id: str
    event_id: str
    transition_sequence: int
    predecessor_event_id: str | None
    predecessor_execution_id: str | None
    parent_flow_id: str | None
    causation_event_id: str | None

    def __post_init__(self) -> None:
        for name in (
            "flow_id",
            "execution_id",
            "event_id",
            "predecessor_event_id",
            "predecessor_execution_id",
            "parent_flow_id",
            "causation_event_id",
        ):
            value = getattr(self, name)
            if value is not None:
                canonical_uuid(value, name.replace("_", " "))
        _key_integer(self.transition_sequence, "transition sequence", positive=True)
        root = self.transition_sequence == 1
        if root and (
            self.predecessor_event_id is not None or self.predecessor_execution_id is not None
        ):
            raise _reattestation_rejection("root proof names a predecessor")
        if not root and self.predecessor_event_id is None:
            raise _reattestation_rejection("non-root proof has no predecessor event")
        if self.predecessor_event_id == self.event_id or (
            self.predecessor_execution_id == self.execution_id
        ):
            raise _reattestation_rejection("proof predecessors are not coherent")


type Stage1EventIdentityMember = (
    PlanIdentityRequest | UnitIdentityRequest | InlineIdentityRequest | PointerIdentityRequest
)

_IDENTITY_TYPE_BY_SURFACE: Final[dict[Stage1Surface, type[object]]] = {
    Stage1Surface.PLAN: PlanIdentityRequest,
    Stage1Surface.UNIT: UnitIdentityRequest,
    Stage1Surface.INLINE_RUN: InlineIdentityRequest,
    Stage1Surface.POINTER: PointerIdentityRequest,
}


@dataclass(frozen=True, slots=True)
class ReattestStage1EventRequest:
    """Retained internal fields plus persisted public proof for exact recomputation."""

    authority: Stage1EventAuthorityRequest
    identity: Stage1EventIdentityMember
    proof: Stage1RetainedEventProof
    owner_generation: int | None = None
    required_child_expectations: PlanRequiredChildExpectations | None = None

    def __post_init__(self) -> None:
        surface = self.authority.surface
        if type(self.identity) is not _IDENTITY_TYPE_BY_SURFACE[surface]:
            raise _reattestation_rejection("identity family differs from the event surface")
        self._validate_identity_members()
        if surface is Stage1Surface.UNIT:
            if self.owner_generation is None:
                raise _reattestation_rejection("unit authority has no owner generation")
            _key_integer(self.owner_generation, "owner generation")
        elif self.owner_generation is not None:
            raise _reattestation_rejection("owner generation belongs only to unit authority")
        if surface is Stage1Surface.PLAN:
            if self.required_child_expectations is None:
                raise _reattestation_rejection("plan authority has no required child expectations")
            if self.required_child_expectations.parent_flow_id != self.proof.flow_id:
                raise _reattestation_rejection("plan expectations name another flow")
        elif self.required_child_expectations is not None:
            raise _reattestation_rejection("child expectations belong only to plan authority")

    def _validate_identity_members(self) -> None:
        key = self.authority.internal_key
        identity = self.identity
        coherent = True
        if isinstance(identity, UnitIdentityRequest):
            coherent = key[1] == identity.unit_key
        elif isinstance(identity, InlineIdentityRequest):
            coherent = key[:3] == (
                identity.writer_kind,
                identity.operation_id,
                identity.physical_table_id,
            )
        elif isinstance(identity, PointerIdentityRequest):
            coherent = key == (
                identity.partition_identity,
                identity.publication_id,
                identity.fencing_token,
            )
        if not coherent:
            raise _reattestation_rejection("internal key differs from the identity tuple")


@dataclass(frozen=True, slots=True)
class VerifiedStage1EventAuthority:
    """Recomputed and verified public identity for one retained EVENT# authority."""

    surface: Stage1Surface
    authority_digest: str
    flow_id: str
    execution_id: str
    event_id: str
    transition_sequence: int
    predecessor_event_id: str | None
    predecessor_execution_id: str | None
    parent_flow_id: str | None
    causation_event_id: str | None
    lifecycle_state: FlowState
    completion_outcome: CompletionOutcome | None
    owner_generation: int | None = None
    required_child_expectations: PlanRequiredChildExpectations | None = None


def reattest_stage1_event(request: ReattestStage1EventRequest) -> VerifiedStage1EventAuthority:
    """Recompute the exact public identity from retained internal fields; reject, never adopt."""
    identity = _member_identity(request.identity)
    digest = event_authority_digest(request.authority)
    event_id = derive_event_id(
        EventIdentityRequest(
            request.authority.surface,
            digest,
            request.proof.transition_sequence,
            request.authority.lifecycle_state,
        )
    )
    recomputed = (identity.flow_id, identity.execution_id, event_id)
    persisted = (request.proof.flow_id, request.proof.execution_id, request.proof.event_id)
    if recomputed != persisted:
        raise _reattestation_rejection("recomputed public identity differs from persisted proof")
    proof = request.proof
    return VerifiedStage1EventAuthority(
        surface=request.authority.surface,
        authority_digest=digest,
        flow_id=proof.flow_id,
        execution_id=proof.execution_id,
        event_id=proof.event_id,
        transition_sequence=proof.transition_sequence,
        predecessor_event_id=proof.predecessor_event_id,
        predecessor_execution_id=proof.predecessor_execution_id,
        parent_flow_id=proof.parent_flow_id,
        causation_event_id=proof.causation_event_id,
        lifecycle_state=request.authority.lifecycle_state,
        completion_outcome=request.authority.completion_outcome,
        owner_generation=request.owner_generation,
        required_child_expectations=request.required_child_expectations,
    )


def _member_identity(identity: Stage1EventIdentityMember) -> Stage1IdentityResult:
    if isinstance(identity, PlanIdentityRequest):
        return derive_plan_identity(identity)
    if isinstance(identity, UnitIdentityRequest):
        return derive_unit_identity(identity)
    if isinstance(identity, InlineIdentityRequest):
        return derive_inline_identity(identity)
    return derive_pointer_identity(identity)


def verify_stage1_wire_member(
    event: OperationalDataFlowEvent, verified: VerifiedStage1EventAuthority
) -> None:
    """Reject any public member whose identity differs from its reattested authority."""
    matches = (
        event.surface == verified.surface.value
        and event.authority.opaque_digest == verified.authority_digest
        and event.flow_id == verified.flow_id
        and event.execution_id == verified.execution_id
        and event.event_id == verified.event_id
        and event.transition_sequence == verified.transition_sequence
        and event.predecessor_event_id == verified.predecessor_event_id
        and event.predecessor_execution_id == verified.predecessor_execution_id
        and event.parent_flow_id == verified.parent_flow_id
        and event.causation_event_id == verified.causation_event_id
        and event.state is verified.lifecycle_state
        and event.outcome is verified.completion_outcome
    )
    if not matches:
        raise _reattestation_rejection("public member identity differs from reattested authority")


def _validate_prefix(record_key: str, authority_kind: Stage1AuthorityKind) -> None:
    required = "EVENT#" if authority_kind is Stage1AuthorityKind.LIFECYCLE_EVENT else "USAGE#"
    if not record_key.startswith(required):
        raise OperationalStatusError("authority prefix and kind disagree")


def _pending(authority: Stage1OutboxAuthoritySnapshot) -> None:
    if authority.outbox_state is not OutboxState.PENDING:
        raise OperationalStatusError("operation requires pending outbox authority")


def _outbox_order(value: str) -> None:
    parts = value.split("#")
    if (
        len(parts) != OUTBOX_ORDER_PARTS
        or len(parts[0]) != KEY_DECIMAL_DIGITS
        or not parts[0].isdigit()
    ):
        raise OperationalStatusError("outbox order must contain exact epoch-microsecond text")
    _digest(parts[1], "outbox order digest")


def _epoch_micros(value: datetime) -> int:
    instant = _utc(value, "authority_at")
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    if instant < epoch:
        raise OperationalStatusError("authority time cannot precede the Unix epoch")
    delta = instant - epoch
    micros = (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds
    if micros > MAX_KEY_INTEGER:
        raise OperationalStatusError("authority epoch microseconds exceed 20 decimal digits")
    return micros


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OperationalStatusError(f"{name} must be nonblank text")
    return value


def _sanitized_reason(value: str) -> str:
    _text(value, "reason")
    if (
        value != value.strip()
        or len(value) > MAX_REASON_LENGTH
        or any(not character.isprintable() for character in value)
    ):
        raise OperationalStatusError("reason must be bounded sanitized text")
    return value


def _digest(value: str, name: str) -> str:
    if len(value) != SHA256_HEX_DIGITS or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise OperationalStatusError(f"{name} must be canonical lowercase sha256")
    return value


def _uuid4(value: str, name: str) -> str:
    canonical_uuid(value, name)
    if uuid.UUID(value).version != UUID4_VERSION:
        raise OperationalStatusError(f"{name} must be a canonical UUIDv4")
    return value


def _unsigned(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise OperationalStatusError(f"{name} must be a nonnegative integer")
    return value


def _positive(value: int, name: str) -> int:
    if _unsigned(value, name) == 0:
        raise OperationalStatusError(f"{name} must be positive")
    return value


def _key_integer(value: int, name: str, *, positive: bool = False) -> int:
    checked = _positive(value, name) if positive else _unsigned(value, name)
    if checked > MAX_KEY_INTEGER:
        raise OperationalStatusError(f"{name} exceeds 20 decimal digits")
    return checked


def _uint64(value: int, name: str) -> int:
    checked = _unsigned(value, name)
    if checked > 2**64 - 1:
        raise OperationalStatusError(f"{name} exceeds uint64")
    return checked


def _utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise OperationalStatusError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)
