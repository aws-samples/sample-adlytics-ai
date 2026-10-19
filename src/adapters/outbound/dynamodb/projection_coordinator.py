# SPDX-License-Identifier: MIT-0
"""ADR-075 §3.2 ``DynamoProjectionCoordinator`` — plan-intent/plan typed builders.

The named narrow exception to ordinary adapter prefix ownership: every multi-item or
cross-family mutation is a private typed builder here. Builders privately construct
every table name, partition key, record key, condition/update expression, and
expression map; callers can never supply a raw key, expression, transaction member,
or attribute map. Before every SDK call each builder runs the shared closed member
validator and measures the assembled transaction with ``Stage1DynamoByteOracle``.

Lifecycle event puts and the head write are consumed opaquely from the existing
``Stage1LifecycleHeadStorePort.transaction_items`` machinery; the head
condition-check for claim/reschedule reuses that writer's exact prior-head condition
so the head schema is never re-derived here.

This adapter now implements all twenty-four §3.2 typed operations: the plan-intent
and plan builders, the table-fence/attempt family, the reconciler
reschedule/block/requeue family, the table-writer execution builders
(claim/fail/finalize), the inline finalizer, the private
``SettleStage1AthenaUsageRequest`` expansion consumed only inside the final
owner-disposition builders, and the planned-plan replacement branch of persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, cast

import boto3
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError

from adapters.outbound.dynamodb.byte_oracle import (
    ConditionCheckAction,
    ExactPreState,
    PutAction,
    Stage1DynamoByteOracle,
    TransactionAction,
    TransactionEnvelopeRequest,
    UpdateAction,
)
from adapters.outbound.dynamodb.semantic_state import (
    CURRENT_RECORD,
    PROJECTED_AT,
    PROJECTED_FENCING_TOKEN,
    PROJECTED_PUBLICATION_ID,
    SERVABLE_NONE,
)
from adapters.outbound.dynamodb.stage1_lifecycle_head import (
    _head_item,  # pyright: ignore[reportPrivateUsage] -- writer half of the head schema
    _prior_head_condition,  # pyright: ignore[reportPrivateUsage] -- exact prior-head condition
)
from adapters.outbound.sqs.operational_status import (
    usage_contribution_document,
    usage_evidence_disposition_document,
)
from domain.operations.cutover import (
    DRAIN_SEQ_PARTITION_KEY,
    DRAIN_SEQ_RECORD_KEY,
    writer_counts_drain,
)
from domain.operations.execution_graph import AuthorityMode
from domain.operations.generated_execution_graphs import STORE as GRAPH_STORE
from domain.operations.stage1_lifecycle_head import (
    EVENT_RECORD_PREFIX,
    HEAD_RECORD_KEY,
    STATUS_FLOW_PREFIX,
    Stage1LifecycleHeadTuple,
    Stage1LifecycleProof,
)
from domain.operations.stage1_operational_status import (
    OperationalDataFlowUsageContribution,
    Stage1AuthorityKind,
    Stage1OutboxOrderRequest,
    Stage1UsageAuthority,
    settle_stage1_athena_usage,
    stage1_outbox_order,
)
from domain.operations.status import canonical_authority_bytes, canonical_authority_digest
from domain.operations.table_attempt import (
    ATTEMPT_RECORD_PREFIX,
    FENCE_RECORD_KEY,
    PROTECTED_CASE_PREFIX,
    PROTECTED_CASE_RECORD_KEY,
    RUN_RECORD_PREFIX,
    TABLE_ATTEMPT_PREFIX,
    USAGE_RECORD_PREFIX,
    AttemptObservation,
    BindingDigestRequest,
    FenceObservation,
    InlineRunState,
    ProtectedCaseState,
    ProtectedReconcileCase,
    ReconciliationObservation,
    SanitizedStartEvidence,
    binding_digest,
    blocked_reconcile_key,
    pending_reconcile_key,
    protected_case_partition_key,
    reconciliation_valid,
    table_attempt_partition_key,
)
from domain.operations.table_lease import (
    RELEASED_LEASE_EXPIRY,
    TABLE_LEASE_PREFIX,
    TableLeaseRecord,
    canonical_lease_instant,
    lease_expiry_epoch_micros,
    table_lease_partition_key,
)
from domain.operations.write_plan import (
    META_RECORD_KEY,
    READY_STATUS_VALUE,
    SUCCESSOR_OWNER_RECORD_KEY,
    UNIT_RECORD_PREFIX,
    WRITE_PLAN_PREFIX,
    CanonicalUnitEntry,
    IntentMeta,
    IntentState,
    PlanIntentRetrySchedule,
    PlanMeta,
    PlanStatus,
    PlanUnit,
    PlanUnitAdmission,
    PlanUnitStatus,
    RequiredChildExpectation,
    SettledAttemptReference,
    SuccessorOwner,
    bootstrap_owner,
    intent_partition_key,
    owner_partition_key,
    plan_intent_retry_schedule,
    plan_settled_attempt_digest,
    ready_order_key,
    unit_record_key,
)
from domain.shared_kernel.errors import (
    DependencyError,
    DomainError,
    ErrorCode,
    InvariantError,
    ValidationError,
)
from ports.outbound.projection_coordinator import (
    AcquireTableFenceOutcome,
    AcquireTableFenceRequest,
    AcquireTableFenceResult,
    AdmitUnitOutcome,
    AdmitUnitRequest,
    AdmitUnitResult,
    AuthorityModeGuard,
    BeginQueuedUnitOwner,
    BeginTableAttemptOutcome,
    BeginTableAttemptRequest,
    BeginTableAttemptResult,
    BlockTableAttemptReconcileOutcome,
    BlockTableAttemptReconcileRequest,
    BlockTableAttemptReconcileResult,
    ClaimPlanIntentOutcome,
    ClaimPlanIntentRequest,
    ClaimPlanIntentResult,
    ClaimUnitOutcome,
    ClaimUnitRequest,
    ClaimUnitResult,
    CompletePlanOutcome,
    CompletePlanRequest,
    CompletePlanResult,
    CompleteTableAttemptOutcome,
    CompleteTableAttemptRequest,
    CompleteTableAttemptResult,
    CurrentPointerOutcome,
    FailAdmissionOutcome,
    FailAdmissionRequest,
    FailAdmissionResult,
    FailUnitOutcome,
    FailUnitRequest,
    FailUnitResult,
    FinalizeInlineOutcome,
    FinalizeInlineRequest,
    FinalizeInlineResult,
    FinalizeUnitOutcome,
    FinalizeUnitRequest,
    FinalizeUnitResult,
    GraphAuthorityRefusedError,
    GraphClaimFence,
    InlineRunAttemptOwner,
    ModeGuardKind,
    ParkAttemptOutcome,
    ParkAttemptRequest,
    ParkAttemptResult,
    PersistInlineRunOutcome,
    PersistInlineRunRequest,
    PersistInlineRunResult,
    PersistPlanOutcome,
    PersistPlanRequest,
    PersistPlanResult,
    PointerOutcomeKind,
    ProjectionCoordinatorPort,
    QueuedUnitAttemptOwner,
    RecordStage1RefusalOutcome,
    RecordStage1RefusalRequest,
    RecordStage1RefusalResult,
    RecoverParkedAttemptSubmissionOutcome,
    RecoverParkedAttemptSubmissionRequest,
    RecoverParkedAttemptSubmissionResult,
    ReleaseTableFenceOutcome,
    ReleaseTableFenceRequest,
    ReleaseTableFenceResult,
    RequestAttemptCancellationOutcome,
    RequestAttemptCancellationRequest,
    RequestAttemptCancellationResult,
    RequeueBlockedTableAttemptReconcileOutcome,
    RequeueBlockedTableAttemptReconcileRequest,
    RequeueBlockedTableAttemptReconcileResult,
    ReschedulePlanIntentOutcome,
    ReschedulePlanIntentRequest,
    ReschedulePlanIntentResult,
    RescheduleTableAttemptReconcileOutcome,
    RescheduleTableAttemptReconcileRequest,
    RescheduleTableAttemptReconcileResult,
    SourcePlanReplacementEvidence,
    Stage1RefusalTag,
    Stage1SettlementExpansion,
    SubmitPlanIntentOutcome,
    SubmitPlanIntentRequest,
    SubmitPlanIntentResult,
    TableAttemptOwnerSnapshot,
)
from ports.outbound.stage1_lifecycle_head import (
    Stage1LifecycleHeadStorePort,
    Stage1LifecycleTransactionItems,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from types_boto3_dynamodb.client import DynamoDBClient
    from types_boto3_dynamodb.type_defs import AttributeValueTypeDef, TransactWriteItemTypeDef

__all__ = ["DynamoProjectionCoordinator"]

type PlainItem = dict[str, object]

_CONFLICT: Final = ErrorCode("PLAN_COORD", 1)
_COLLISION: Final = ErrorCode("PLAN_COORD", 2)
_UNREACHABLE: Final = ErrorCode("PLAN_COORD", 3)
_MEMBER_OUTSIDE_MATRIX: Final = ErrorCode("PLAN_COORD", 4)
_ACTION_CAP_EXCEEDED: Final = ErrorCode("PLAN_COORD", 5)

_ABSENT: Final = "attribute_not_exists(partition_key)"
_CONDITIONAL_CODES: Final = frozenset(
    {"ConditionalCheckFailedException", "TransactionCanceledException"}
)

# Per-operation distinct-item action caps (§3.2 formulas/rows at the accepted profile).
# A ``#``-suffixed key is one operation's separately capped branch, never a new request.
_ACTION_CAPS: Final = {
    "SubmitPlanIntentRequest": 17,
    "ClaimPlanIntentRequest": 4,
    "ReschedulePlanIntentRequest": 3,
    "PersistPlanRequest": 20,
    "PersistPlanRequest#replacement": 100,
    "AdmitUnitRequest": 6,
    "FailAdmissionRequest": 5,
    "CompletePlanRequest": 51,
    "AcquireTableFenceRequest": 4,
    "ReleaseTableFenceRequest": 2,
    "BeginTableAttemptRequest": 5,
    "RecordStage1RefusalRequest": 5,
    "PersistInlineRunRequest": 3,
    "RecoverParkedAttemptSubmissionRequest": 2,
    "RequestAttemptCancellationRequest": 5,
    "ParkAttemptRequest": 3,
    "CompleteTableAttemptRequest": 5,
    "RescheduleTableAttemptReconcileRequest": 2,
    "BlockTableAttemptReconcileRequest": 4,
    "RequeueBlockedTableAttemptReconcileRequest": 4,
    "ClaimUnitRequest": 7,
    "ClaimUnitRequest#reclaim": 8,
    "FailUnitRequest": 26,
    "FailUnitRequest#attempt-33": 42,
    "FinalizeUnitRequest": 100,
    "FinalizeInlineRequest": 99,
}

# The stable fence/attempt/reconciliation attribute orders this adapter reconstructs.
_FENCE_FIELD_ORDER: Final = (
    "state",
    "physical_table_id",
    "attempt_id",
    "lease_holder",
    "fencing_token",
    "parked_at",
)
_RECONCILIATION_FIELD_ORDER: Final = (
    "reconcile_status",
    "reconcile_after_or_block",
    "reconcile_attempt_count",
    "requeued_from_case_ref",
    "requeued_at",
    "reconcile_block_id",
    "reconcile_block_reason",
    "reconcile_blocked_at",
    "reconcile_observation_digest",
    "reconcile_recovery_ref",
)
_ATTEMPT_TEXT_ORDER: Final = (
    "state",
    "physical_table_id",
    "attempt_id",
    "plan_unit_owner_ref",
    "inline_run_owner_ref",
    "statement_kind",
    "lease_holder",
    "writer_execution_id",
    "start_envelope",
    "client_request_token",
    "envelope_digest",
    "reservation_identity",
    "query_execution_id",
    "submitted_at",
    "cancel_requested_at",
    "parked_at",
    "terminal_at",
    "athena_usage_state",
)
_ATTEMPT_INT_ORDER: Final = (
    "ordinal",
    "fencing_token",
    "query_count",
    "scanned_bytes",
    "engine_ms",
)


class _ConditionalAbortError(Exception):
    """The transaction lost a condition; the builder classifies by strong reread."""


class _GraphMemberAbortError(Exception):
    """A graph-table member's condition failed; `position` indexes the graph members."""

    def __init__(self, position: int) -> None:
        super().__init__(position)
        self.position = position


_GRAPH_MODE_PK: Final = str(GRAPH_STORE["mode_key"]["pk"])
_GRAPH_MODE_SK: Final = str(GRAPH_STORE["mode_key"]["sk"])
_GRAPH_CLAIM_PREFIX: Final = "EXEC_CLAIM#"
_MAX_TRANSACTION_ITEMS: Final = 100
_AUTHORITY_MODE_DRAINING: Final = "AUTHORITY_MODE_DRAINING"
_AUTHORITY_MODE_CHANGED: Final = "AUTHORITY_MODE_CHANGED"
_GRAPH_CLAIM_SUPERSEDED: Final = "EXECUTION_SUPERSEDED"


@dataclass(frozen=True, slots=True)
class _Member:
    """One assembled transaction member paired with its byte-oracle action."""

    transact_item: dict[str, Any]
    action: TransactionAction


@dataclass(frozen=True, slots=True)
class _MemberShape:
    """One member's action kind and canonical two-part key for matrix validation."""

    kind: str
    partition: str
    record: str


def _conflict(operation: str, description: str) -> DomainError:
    return DomainError(_CONFLICT, description, context={"operation": operation})


def _collision(operation: str, description: str) -> InvariantError:
    return InvariantError(_COLLISION, description, context={"operation": operation})


def _dependency(error: Exception, description: str) -> DependencyError:
    service_code = (
        str(error.response.get("Error", {}).get("Code", ""))
        if isinstance(error, ClientError)
        else ""
    )
    return DependencyError(
        _UNREACHABLE,
        description,
        context={"cause": type(error).__name__, "service_code": service_code},
    )


def _is_conditional(error: ClientError) -> bool:
    return str(error.response.get("Error", {}).get("Code", "")) in _CONDITIONAL_CODES


# --- Plain item codecs (the exact stored forms this adapter owns) ---------------------------------


def _set_optional(item: PlainItem, name: str, value: object | None) -> None:
    if value is not None:
        item[name] = value


def _owner_item(plan_digest: str, owner: SuccessorOwner) -> PlainItem:
    item: PlainItem = {
        "partition_key": owner_partition_key(plan_digest),
        "record_key": SUCCESSOR_OWNER_RECORD_KEY,
        "revision": owner.revision,
        "last_generation": owner.last_generation,
    }
    _set_optional(item, "root_plan_key", owner.root_plan_key)
    _set_optional(item, "last_plan_key", owner.last_plan_key)
    _set_optional(item, "active_plan_key", owner.active_plan_key)
    _set_optional(item, "active_intent_id", owner.active_intent_id)
    _set_optional(item, "reserved_intent_id", owner.reserved_intent_id)
    _set_optional(item, "reserved_intent_digest", owner.reserved_intent_digest)
    _set_optional(item, "reserved_generation", owner.reserved_generation)
    _set_optional(item, "latest_internal_execution_id", owner.latest_internal_execution_id)
    return item


def _intent_meta_item(plan_digest: str, meta: IntentMeta) -> PlainItem:
    item: PlainItem = {
        "partition_key": intent_partition_key(plan_digest, meta.intent_id),
        "record_key": META_RECORD_KEY,
        "plan_digest": meta.plan_digest,
        "intent_id": meta.intent_id,
        "roster_digest": meta.roster_digest,
        "origin_kind": meta.origin.kind.value,
        "origin_member": meta.origin.member,
        "state": meta.state.value,
        "retry_count": meta.retry_count,
        "due_at": meta.due_at,
        "created_at": meta.created_at,
        "last_transition_at": meta.last_transition_at,
    }
    _set_optional(item, "root_plan_key", meta.root_plan_key)
    _set_optional(item, "source_plan_key", meta.source_plan_key)
    _set_optional(item, "claimed_generation", meta.claimed_generation)
    _set_optional(item, "ready_status", meta.ready_status)
    _set_optional(item, "ready_order", meta.ready_order)
    return item


def _unit_entry_attributes(unit: CanonicalUnitEntry) -> PlainItem:
    return {
        "unit_key": unit.unit_key,
        "table_kind": unit.table_kind,
        "merge_count": unit.merge_count,
        "objects": [entry.to_member() for entry in unit.objects],
    }


def _intent_unit_item(intent_partition: str, unit: CanonicalUnitEntry) -> PlainItem:
    item: PlainItem = {
        "partition_key": intent_partition,
        "record_key": unit_record_key(unit.unit_key),
    }
    item.update(_unit_entry_attributes(unit))
    return item


def _plan_meta_item(plan_key: str, meta: PlanMeta) -> PlainItem:
    item: PlainItem = {
        "partition_key": plan_key,
        "record_key": META_RECORD_KEY,
        "plan_digest": meta.plan_digest,
        "internal_execution_id": meta.internal_execution_id,
        "root_plan_key": meta.root_plan_key,
        "generation": meta.generation,
        "origin_intent_id": meta.origin_intent_id,
        "roster_digest": meta.roster_digest,
        "origin_kind": meta.origin.kind.value,
        "origin_member": meta.origin.member,
        "status": meta.status.value,
        "unit_count": meta.unit_count,
        "admitted_count": meta.admitted_count,
        "attested_count": meta.attested_count,
        "failed_count": meta.failed_count,
        "child_outcome_reduction_version": meta.child_outcome_reduction_version,
        "required_child_expectations": [
            {"child_flow_id": member.child_flow_id, "owner_generation": member.owner_generation}
            for member in meta.required_child_expectations
        ],
        "settled_attempt_count": meta.settled_attempt_count,
        "created_at": meta.created_at,
    }
    _set_optional(item, "source_plan_key", meta.source_plan_key)
    _set_optional(item, "settled_attempt_digest", meta.settled_attempt_digest)
    _set_optional(item, "terminal_at", meta.terminal_at)
    if meta.plan_reservation_count > 0:
        # Begin writes the counter on first reservation; a fresh plan carries no attribute.
        item["plan_reservation_count"] = meta.plan_reservation_count
    return item


def _plan_unit_item(plan_key: str, unit: PlanUnit) -> PlainItem:
    item: PlainItem = {
        "partition_key": plan_key,
        "record_key": unit_record_key(unit.unit.unit_key),
        "flow_id": unit.flow_id,
        "owner_generation": unit.owner_generation,
        "status": unit.status.value,
        "admission": unit.admission.value,
        "created_at": unit.created_at,
    }
    item.update(_unit_entry_attributes(unit.unit))
    _set_optional(item, "admission_deduplication_digest", unit.admission_deduplication_digest)
    _set_optional(item, "admitted_at", unit.admitted_at)
    return item


def _lease_item(lease: TableLeaseRecord) -> PlainItem:
    item: PlainItem = {
        "partition_key": lease.partition_key,
        "holder": lease.holder,
        "fencing_token": lease.fencing_token,
        "expires_at": lease.expires_at,
    }
    _set_optional(item, "parked_at", lease.parked_at)
    return item


def _evidence_plain(evidence: SanitizedStartEvidence) -> dict[str, object]:
    return {
        "provider_status_code": evidence.provider_status_code,
        "provider_error_class": evidence.provider_error_class.value,
    }


def _fence_item(observation: FenceObservation) -> PlainItem:
    """Reconstruct the exact stored fence bytes from one strongly read observation."""
    item: PlainItem = {
        "partition_key": observation.partition_key,
        "record_key": observation.record_key,
    }
    for name in _FENCE_FIELD_ORDER:
        _set_optional(item, name, getattr(observation, name))
    for name in _RECONCILIATION_FIELD_ORDER:
        _set_optional(item, name, getattr(observation.reconciliation, name))
    return item


def _attempt_item(observation: AttemptObservation) -> PlainItem:
    """Reconstruct the exact stored attempt bytes from one strongly read observation."""
    item: PlainItem = {
        "partition_key": observation.partition_key,
        "record_key": observation.record_key,
    }
    for name in _ATTEMPT_TEXT_ORDER:
        _set_optional(item, name, getattr(observation, name))
    for name in _ATTEMPT_INT_ORDER:
        _set_optional(item, name, getattr(observation, name))
    if observation.sanitized_start_evidence is not None:
        item["sanitized_start_evidence"] = _evidence_plain(observation.sanitized_start_evidence)
    return item


def _plain_fence_observation(item: PlainItem) -> FenceObservation:
    """Rebuild a fence observation from a strongly reread plain item for classification."""

    def _text(name: str) -> str | None:
        value = item.get(name)
        return value if isinstance(value, str) else None

    def _number(name: str) -> int | None:
        value = item.get(name)
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    raw: dict[str, str | int | bool | None] = {}
    for name in _RECONCILIATION_FIELD_ORDER:
        value = item.get(name)
        raw[name] = value if isinstance(value, str | int | bool) else None
    return FenceObservation(
        partition_key=_text("partition_key") or "",
        record_key=_text("record_key") or "",
        state=_text("state"),
        physical_table_id=_text("physical_table_id"),
        attempt_id=_text("attempt_id"),
        lease_holder=_text("lease_holder"),
        fencing_token=_number("fencing_token"),
        parked_at=_text("parked_at"),
        reconciliation=ReconciliationObservation(
            reconcile_status=raw["reconcile_status"],
            reconcile_after_or_block=raw["reconcile_after_or_block"],
            reconcile_attempt_count=raw["reconcile_attempt_count"],
            requeued_from_case_ref=raw["requeued_from_case_ref"],
            requeued_at=raw["requeued_at"],
            reconcile_block_id=raw["reconcile_block_id"],
            reconcile_block_reason=raw["reconcile_block_reason"],
            reconcile_blocked_at=raw["reconcile_blocked_at"],
            reconcile_observation_digest=raw["reconcile_observation_digest"],
            reconcile_recovery_ref=raw["reconcile_recovery_ref"],
        ),
    )


def _applied(
    snapshot: Mapping[str, object],
    sets: Mapping[str, object],
    removes: tuple[str, ...] = (),
) -> PlainItem:
    """Apply a SET/REMOVE map to a strongly read snapshot for post-state comparison."""
    item: PlainItem = {name: value for name, value in snapshot.items() if name not in removes}
    item.update(sets)
    return item


@dataclass(frozen=True, slots=True)
class _SnapshotUpdate:
    """One exact-snapshot-conditioned ``Update``: the snapshot plus its SET map."""

    snapshot: PlainItem
    update: str
    update_values: Mapping[str, object]
    set_attributes: Mapping[str, object]
    update_names: Mapping[str, str] = field(default_factory=dict[str, str])
    absent: tuple[str, ...] = ()


def _begin_binding(
    request: BeginTableAttemptRequest, attempt_id: str
) -> tuple[dict[str, object], dict[str, object]]:
    """Derive Begin's owner reservation-binding SET map and its expression values."""
    sets: dict[str, object] = {
        "current_attempt_id": attempt_id,
        "lease_holder": request.lease.holder,
        "fencing_token": request.lease.fencing_token,
        "reserved_slot_identity": request.reservation_identity,
    }
    values: dict[str, object] = {
        ":attempt": attempt_id,
        ":holder": request.lease.holder,
        ":token": request.lease.fencing_token,
        ":slot": request.reservation_identity,
    }
    return sets, values


def _exact_condition(
    item: Mapping[str, object], *, absent: tuple[str, ...] = ()
) -> tuple[str, dict[str, object], dict[str, str]]:
    """Derive one deterministic complete-snapshot equality condition over an exact item."""
    clauses: list[str] = []
    values: dict[str, object] = {}
    names: dict[str, str] = {}
    for index, name in enumerate(sorted(set(item) - {"partition_key", "record_key"})):
        names[f"#x{index}"] = name
        values[f":x{index}"] = item[name]
        clauses.append(f"#x{index} = :x{index}")
    for index, name in enumerate(sorted(absent)):
        names[f"#a{index}"] = name
        clauses.append(f"attribute_not_exists(#a{index})")
    return " AND ".join(clauses), values, names


# --- Low-level marshalling ------------------------------------------------------------------------

_SERIALIZER: Final = TypeSerializer()


def _low_value(value: object) -> AttributeValueTypeDef:
    return cast("AttributeValueTypeDef", _SERIALIZER.serialize(value))


def _low_item(item: Mapping[str, object]) -> dict[str, AttributeValueTypeDef]:
    return {name: _low_value(value) for name, value in item.items()}


def _plain_value(low: Mapping[str, object]) -> object:
    if len(low) != 1:
        raise _collision("readback", "a stored attribute value carries exactly one type tag.")
    tag, member = next(iter(low.items()))
    if tag == "S":
        return member
    if tag == "N":
        return int(cast("str", member))
    if tag == "BOOL":
        return member
    if tag == "NULL":
        return None
    if tag == "L":
        return [
            _plain_value(cast("Mapping[str, object]", child))
            for child in cast("list[object]", member)
        ]
    if tag == "M":
        return _plain_item(cast("Mapping[str, Mapping[str, object]]", member))
    raise _collision("readback", f"a stored attribute uses the unsupported type tag {tag!r}.")


def _plain_item(low: Mapping[str, Mapping[str, object]]) -> PlainItem:
    return {name: _plain_value(value) for name, value in low.items()}


def _key(partition_key: str, record_key: str) -> dict[str, str]:
    return {"partition_key": partition_key, "record_key": record_key}


# --- Member assembly helpers ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _PutSpec:
    """One derived ``Put``: complete item, its condition, and exact pre-state."""

    item: PlainItem
    condition: str
    pre_state: PlainItem | None = None


@dataclass(frozen=True, slots=True)
class _UpdateSpec:
    """One derived conditional ``Update`` with its oracle-visible SET map."""

    key: dict[str, str]
    pre_state: PlainItem | None
    condition: str
    update: str
    values: Mapping[str, object]
    set_attributes: Mapping[str, object]
    names: Mapping[str, str] | None = None


@dataclass(frozen=True, slots=True)
class _ConditionSpec:
    """One derived ``ConditionCheck`` over an exact strongly read pre-state."""

    key: dict[str, str]
    pre_state: PlainItem | None
    condition: str
    values: Mapping[str, object] | None = None
    names: Mapping[str, str] | None = None


def _put_member(table: str, spec: _PutSpec) -> _Member:
    key = {name: cast("str", spec.item[name]) for name in ("partition_key", "record_key")}
    put: dict[str, Any] = {
        "TableName": table,
        "Item": _low_item(spec.item),
        "ConditionExpression": spec.condition,
    }
    return _Member(
        transact_item={"Put": put},
        action=PutAction(
            table_name=table, key=key, item=spec.item, pre_state=ExactPreState(spec.pre_state)
        ),
    )


def _update_member(table: str, spec: _UpdateSpec) -> _Member:
    member: dict[str, Any] = {
        "TableName": table,
        "Key": _low_item(cast("PlainItem", dict(spec.key))),
        "ConditionExpression": spec.condition,
        "UpdateExpression": spec.update,
        "ExpressionAttributeValues": {
            name: _low_value(value) for name, value in spec.values.items()
        },
    }
    if spec.names:
        member["ExpressionAttributeNames"] = dict(spec.names)
    return _Member(
        transact_item={"Update": member},
        action=UpdateAction(
            table_name=table,
            key=spec.key,
            set_attributes=spec.set_attributes,
            pre_state=ExactPreState(spec.pre_state),
        ),
    )


def _condition_member(table: str, spec: _ConditionSpec) -> _Member:
    member: dict[str, Any] = {
        "TableName": table,
        "Key": _low_item(cast("PlainItem", dict(spec.key))),
        "ConditionExpression": spec.condition,
    }
    if spec.values:
        member["ExpressionAttributeValues"] = {
            name: _low_value(value) for name, value in spec.values.items()
        }
    if spec.names:
        member["ExpressionAttributeNames"] = dict(spec.names)
    return _Member(
        transact_item={"ConditionCheck": member},
        action=ConditionCheckAction(
            table_name=table, key=spec.key, pre_state=ExactPreState(spec.pre_state)
        ),
    )


# --- The coordinator ------------------------------------------------------------------------------


class DynamoProjectionCoordinator(ProjectionCoordinatorPort):
    """Private typed plan-intent/plan transaction builders over the publications table."""

    def __init__(  # noqa: PLR0913, PLR0917 - two tables plus injectable boundaries
        self,
        publications_table: str,
        leases_table: str,
        head_store: Stage1LifecycleHeadStorePort,
        client: DynamoDBClient | None = None,
        oracle: Stage1DynamoByteOracle | None = None,
        *,
        graph_table: str | None = None,
    ) -> None:
        if not publications_table.strip() or not leases_table.strip():
            raise ValueError(
                "the projection coordinator requires the publications and leases table names."
            )
        if publications_table == leases_table:
            raise ValueError("the leases table is a separate table from publications.")
        self._table = publications_table
        self._leases_table = leases_table
        self._head_store = head_store
        self._client = client or boto3.client("dynamodb")
        self._oracle = oracle or Stage1DynamoByteOracle()
        # S3 (V-13, V-9): the execution-graph table, for the mode-item ConditionCheck and the
        # graph-envelope claim fence. None composes no cross-table member.
        self._graph_table = graph_table
        # S6 (V-17b): set once per invocation from the writer's strong mode read. While it is
        # set, every transaction this adapter commits also adds 1 to `DRAIN_SEQ`.
        self._drain_counting = False

    def count_drain(self, observed: AuthorityMode) -> None:
        """Record this invocation's strong mode read; `LEGACY_DRAINING` turns counting on.

        Every legacy family-changing writer calls this once per invocation (or batch), before
        its first transaction, so a writer that read `LEGACY` or `SHADOW` adds nothing and HEAD
        contention is unchanged.
        """
        self._drain_counting = writer_counts_drain(observed)

    @property
    def drain_counting(self) -> bool:
        """Whether this invocation's transactions add 1 to `DRAIN_SEQ`."""
        return self._drain_counting

    # --- Shared dispatch --------------------------------------------------------------------------

    def _validate_members(self, operation: str, members: tuple[_Member, ...]) -> None:
        """Reject any member outside the §3.2 key-family/action matrices before the SDK."""
        cap = _ACTION_CAPS[operation]
        if len(members) > cap:
            raise ValidationError(
                _ACTION_CAP_EXCEEDED,
                f"{operation} exceeds its {cap}-action cap and is refused before mutation.",
                context={"operation": operation, "action_count": len(members)},
            )
        for member in members:
            self._validate_member(operation, member)

    def _validate_member(self, operation: str, member: _Member) -> None:
        (kind, body), *extra = member.transact_item.items()
        if extra or kind not in ("Put", "Update", "ConditionCheck"):
            raise self._outside(operation, "an unsupported transaction action kind")
        payload = cast("dict[str, Any]", body)
        partition = member.action.key.get("partition_key")
        if not isinstance(partition, str):
            raise self._outside(operation, "a key outside the canonical form")
        if payload.get("TableName") == self._leases_table:
            lease_shaped = (
                partition.startswith(TABLE_LEASE_PREFIX)
                and set(member.action.key) == {"partition_key"}
                and kind in ("Update", "ConditionCheck")
            )
            if not lease_shaped:
                raise self._outside(operation, "a leases-table member outside its matrix row")
            return
        if payload.get("TableName") != self._table:
            raise self._outside(operation, "a table outside the publications table")
        record = member.action.key.get("record_key")
        if not isinstance(record, str):
            raise self._outside(operation, "a key outside the canonical two-part form")
        self._validate_publications_member(operation, _MemberShape(kind, partition, record))

    def _validate_publications_member(self, operation: str, shape: _MemberShape) -> None:
        kind, partition, record = shape.kind, shape.partition, shape.record
        if partition.startswith(WRITE_PLAN_PREFIX):
            allowed = record in (SUCCESSOR_OWNER_RECORD_KEY, META_RECORD_KEY) or record.startswith(
                UNIT_RECORD_PREFIX
            )
            if not allowed:
                raise self._outside(operation, "a WRITE_PLAN# record key outside the closed forms")
        elif partition.startswith(STATUS_FLOW_PREFIX):
            if record != HEAD_RECORD_KEY and not record.startswith(EVENT_RECORD_PREFIX):
                raise self._outside(operation, "a STATUS_FLOW# record key outside HEAD/EVENT#")
            if kind == "ConditionCheck" and record != HEAD_RECORD_KEY:
                raise self._outside(operation, "a STATUS_FLOW# condition outside the plan HEAD")
        elif partition.startswith(TABLE_ATTEMPT_PREFIX):
            allowed = (
                record == FENCE_RECORD_KEY
                or record.startswith(ATTEMPT_RECORD_PREFIX)
                or record.startswith(RUN_RECORD_PREFIX)
                or record.startswith(USAGE_RECORD_PREFIX)
            )
            if not allowed:
                raise self._outside(
                    operation, "a TABLE_ATTEMPT# record key outside FENCE/ATTEMPT#/RUN#/USAGE#"
                )
        elif partition.startswith(PROTECTED_CASE_PREFIX):
            if record != PROTECTED_CASE_RECORD_KEY:
                raise self._outside(
                    operation, "a PROVIDER_RECONCILE_BLOCK# record key outside CASE"
                )
        else:
            self._validate_canonical_dataset_member(operation, shape)

    def _validate_canonical_dataset_member(self, operation: str, shape: _MemberShape) -> None:
        """Admit only the §3.2 canonical-dataset ``<dataset-key>|*`` Put/Update members."""
        kind, partition, record = shape.kind, shape.partition, shape.record
        if "|" not in partition or not partition.split("|", 1)[0].strip():
            raise self._outside(operation, "a partition prefix outside the coordinator matrix")
        if record != CURRENT_RECORD and not record.startswith(EVENT_RECORD_PREFIX):
            raise self._outside(operation, "a canonical-dataset record key outside CURRENT/EVENT#")
        if kind == "ConditionCheck":
            raise self._outside(
                operation, "a canonical-dataset condition outside the Put/Update matrix"
            )

    def _outside(self, operation: str, description: str) -> InvariantError:
        return InvariantError(
            _MEMBER_OUTSIDE_MATRIX,
            f"{operation} assembled {description}; the request fails before any SDK call.",
            context={"operation": operation},
        )

    def _execute(
        self,
        operation: str,
        members: tuple[_Member, ...],
        graph: tuple[_Member, ...] = (),
    ) -> None:
        """Validate and commit; `graph` members ride on the execution-graph table (S3).

        A cancellation whose failed member is a graph member raises `_GraphMemberAbortError`
        with its position, so the caller maps it to its typed signal instead of classifying a
        replay.
        """
        self._validate_members(operation, members)
        for member in graph:
            self._validate_graph_member(operation, member)
        everything = (*members, *graph)
        self._oracle.validate_envelope(
            TransactionEnvelopeRequest(tuple(member.action for member in everything))
        )
        items = [member.transact_item for member in everything]
        if self._drain_counting:
            # The counter rides last, unconditioned, so cancellation positions are unchanged.
            if len(items) + 1 > _MAX_TRANSACTION_ITEMS:
                raise ValidationError(
                    _ACTION_CAP_EXCEEDED,
                    f"{operation} cannot carry the DRAIN_SEQ counter within the item bound.",
                    context={"operation": operation, "action_count": len(items) + 1},
                )
            items.append(self._drain_seq_member())
        try:
            self._client.transact_write_items(
                TransactItems=cast("list[TransactWriteItemTypeDef]", items)
            )
        except ClientError as error:
            if _is_conditional(error):
                reasons = cast("dict[str, Any]", error.response).get("CancellationReasons", [])
                for position in range(len(graph)):
                    index = len(members) + position
                    if index < len(reasons) and reasons[index].get("Code") == (
                        "ConditionalCheckFailed"
                    ):
                        raise _GraphMemberAbortError(position) from error
                raise _ConditionalAbortError from error
            raise _dependency(error, f"{operation} could not reach DynamoDB.") from error
        except Exception as error:  # SDK errors must not cross the port
            raise _dependency(error, f"{operation} could not reach DynamoDB.") from error

    def _drain_seq_member(self) -> dict[str, Any]:
        """`ADD seq 1` on the one `DRAIN_SEQ` item in this table (V-17b)."""
        return {
            "Update": {
                "TableName": self._table,
                "Key": {
                    "partition_key": _low_value(DRAIN_SEQ_PARTITION_KEY),
                    "record_key": _low_value(DRAIN_SEQ_RECORD_KEY),
                },
                "UpdateExpression": "ADD seq :one",
                "ExpressionAttributeValues": {":one": _low_value(1)},
            }
        }

    def _validate_graph_member(self, operation: str, member: _Member) -> None:
        """Admit only the mode-item ConditionCheck and the `EXEC_CLAIM#` lease-fenced Update."""
        (kind, body), *extra = member.transact_item.items()
        payload = cast("dict[str, Any]", body)
        if extra or self._graph_table is None or payload.get("TableName") != self._graph_table:
            raise self._outside(operation, "a graph-table member without the graph table")
        partition = member.action.key.get("pk")
        mode = kind == "ConditionCheck" and partition == _GRAPH_MODE_PK
        claim = (
            kind == "Update"
            and isinstance(partition, str)
            and partition.startswith(_GRAPH_CLAIM_PREFIX)
        )
        if not (mode or claim):
            raise self._outside(operation, "a graph-table member outside the mode and claim rows")

    def _mode_guard_member(self, guard: AuthorityModeGuard) -> _Member:
        if self._graph_table is None:
            raise self._outside("mode guard", "a mode guard without the graph table")
        if guard.kind is ModeGuardKind.NEW_ROOT_ADMITTING:
            condition = "attribute_not_exists(pk) OR #mode = :legacy OR #mode = :shadow"
            values: dict[str, object] = {":legacy": "LEGACY", ":shadow": "SHADOW"}
        else:
            condition = "#mode = :legacy OR #mode = :shadow OR #mode = :draining"
            values = {":legacy": "LEGACY", ":shadow": "SHADOW", ":draining": "LEGACY_DRAINING"}
        return _condition_member(
            self._graph_table,
            _ConditionSpec(
                key={"pk": _GRAPH_MODE_PK, "sk": _GRAPH_MODE_SK},
                pre_state=None if guard.observed is None else dict(guard.observed),
                condition=condition,
                values=values,
                names={"#mode": "mode"},
            ),
        )

    def _claim_fence_member(
        self, fence: GraphClaimFence, attempt_id: str, table_id: str
    ) -> _Member:
        if self._graph_table is None:
            raise self._outside("claim fence", "a claim fence without the graph table")
        sets: dict[str, object] = {"table_id": table_id, "attempt_id": attempt_id}
        return _update_member(
            self._graph_table,
            _UpdateSpec(
                key={"pk": f"{_GRAPH_CLAIM_PREFIX}{fence.node_id}", "sk": "CLAIM"},
                pre_state=dict(fence.observed),
                condition="lease_version = :version",
                update="SET table_id = :table_id, attempt_id = :attempt_id",
                values={
                    ":version": fence.lease_version,
                    ":table_id": table_id,
                    ":attempt_id": attempt_id,
                },
                set_attributes=sets,
            ),
        )

    def _get_lease_plain(self, partition_key: str) -> PlainItem | None:
        try:
            response = self._client.get_item(
                TableName=self._leases_table,
                Key={"partition_key": _low_value(partition_key)},
                ConsistentRead=True,
            )
        except Exception as error:  # SDK errors must not cross the port
            raise _dependency(error, "the table lease could not be strongly read.") from error
        item = response.get("Item")
        if item is None:
            return None
        return _plain_item(cast("Mapping[str, Mapping[str, object]]", item))

    def _get_plain(self, partition_key: str, record_key: str) -> PlainItem | None:
        try:
            response = self._client.get_item(
                TableName=self._table,
                Key={
                    "partition_key": _low_value(partition_key),
                    "record_key": _low_value(record_key),
                },
                ConsistentRead=True,
            )
        except Exception as error:  # SDK errors must not cross the port
            raise _dependency(error, "the plan authority could not be strongly read.") from error
        item = response.get("Item")
        if item is None:
            return None
        return _plain_item(cast("Mapping[str, Mapping[str, object]]", item))

    def _replayed(self, expected: dict[tuple[str, str], PlainItem]) -> bool:
        """Whether every expected post-state item is byte-identically durable."""
        return all(
            self._get_plain(partition, record) == item
            for (partition, record), item in expected.items()
        )

    # --- Lifecycle proof members ------------------------------------------------------------------

    def _proof_members(self, proof: Stage1LifecycleProof) -> list[_Member]:
        """Embed the head store's exact conditioned fragments; never re-derive them."""
        fragments: Stage1LifecycleTransactionItems = self._head_store.transaction_items(proof)
        members: list[_Member] = []
        for event_put in fragments.event_puts:
            members.append(self._fragment_member(event_put, pre_state=None))
        if fragments.head_write is None:
            raise _collision(
                "lifecycle proof", "a plan-intent lifecycle proof always carries one head write."
            )
        prior = (
            None
            if proof.prior_head is None or proof.head_record_key is None
            else _plain_item(
                cast(
                    "Mapping[str, Mapping[str, object]]",
                    _head_item(proof.partition_key, proof.head_record_key, proof.prior_head),
                )
            )
        )
        members.append(self._fragment_member(fragments.head_write, pre_state=prior))
        return members

    def _fragment_member(
        self, fragment: Mapping[str, object], pre_state: PlainItem | None
    ) -> _Member:
        put = cast("dict[str, Any] | None", fragment.get("Put"))
        if put is None or put.get("TableName") != self._table:
            raise _collision(
                "lifecycle proof", "a head-store fragment is outside the expected Put form."
            )
        item = _plain_item(cast("Mapping[str, Mapping[str, object]]", put["Item"]))
        key = {name: item[name] for name in ("partition_key", "record_key")}
        return _Member(
            transact_item=dict(cast("dict[str, Any]", fragment)),
            action=PutAction(
                table_name=self._table, key=key, item=item, pre_state=ExactPreState(pre_state)
            ),
        )

    def _head_condition_member(
        self, partition_key: str, record_key: str, head: Stage1LifecycleHeadTuple
    ) -> _Member:
        condition, values = _prior_head_condition(head)
        pre_state = _plain_item(
            cast(
                "Mapping[str, Mapping[str, object]]",
                _head_item(partition_key, record_key, head),
            )
        )
        member: dict[str, Any] = {
            "TableName": self._table,
            "Key": _low_item(cast("PlainItem", _key(partition_key, record_key))),
            "ConditionExpression": condition,
            "ExpressionAttributeValues": dict(values),
        }
        return _Member(
            transact_item={"ConditionCheck": member},
            action=ConditionCheckAction(
                table_name=self._table,
                key=_key(partition_key, record_key),
                pre_state=ExactPreState(pre_state),
            ),
        )

    # --- SubmitPlanIntentRequest ------------------------------------------------------------------

    def submit_plan_intent(self, request: SubmitPlanIntentRequest) -> SubmitPlanIntentResult:
        """Create one pending intent and consume the owner; exact replay is a no-op."""
        intent_id = request.intent_id()
        digest = request.roster_digest()
        instant = request.operation_instant
        meta = IntentMeta(
            root_plan_key=request.root_plan_key,
            source_plan_key=request.source_plan_key,
            plan_digest=request.plan_digest,
            intent_id=intent_id,
            roster_digest=digest,
            origin=request.origin,
            state=IntentState.PENDING,
            claimed_generation=None,
            retry_count=0,
            due_at=instant,
            ready_status=READY_STATUS_VALUE,
            ready_order=ready_order_key(instant, intent_id),
            created_at=instant,
            last_transition_at=instant,
        )
        partition = intent_partition_key(request.plan_digest, intent_id)
        members = [
            _put_member(
                self._table,
                _PutSpec(item=_intent_meta_item(request.plan_digest, meta), condition=_ABSENT),
            )
        ]
        members.extend(
            _put_member(
                self._table, _PutSpec(item=_intent_unit_item(partition, unit), condition=_ABSENT)
            )
            for unit in request.units
        )
        if request.owner is None:
            members.append(
                _put_member(
                    self._table,
                    _PutSpec(
                        item=_owner_item(request.plan_digest, bootstrap_owner()),
                        condition=_ABSENT,
                    ),
                )
            )
        else:
            members.append(
                _condition_member(
                    self._table,
                    _ConditionSpec(
                        key=_key(
                            owner_partition_key(request.plan_digest), SUCCESSOR_OWNER_RECORD_KEY
                        ),
                        pre_state=_owner_item(request.plan_digest, request.owner),
                        condition="revision = :revision",
                        values={":revision": request.owner.revision},
                    ),
                )
            )
        guard = request.mode_guard
        graph = () if guard is None else (self._mode_guard_member(guard),)
        try:
            self._execute("SubmitPlanIntentRequest", tuple(members), graph)
        except _GraphMemberAbortError:
            # V-13(b): the drain CAS committed after this ingress read the mode.
            raise GraphAuthorityRefusedError(_AUTHORITY_MODE_DRAINING) from None
        except _ConditionalAbortError:
            self._classify_submit(request)
            return SubmitPlanIntentResult(SubmitPlanIntentOutcome.REPLAYED, intent_id, digest)
        return SubmitPlanIntentResult(SubmitPlanIntentOutcome.SUBMITTED, intent_id, digest)

    def _classify_submit(self, request: SubmitPlanIntentRequest) -> None:
        intent_id = request.intent_id()
        digest = request.roster_digest()
        partition = intent_partition_key(request.plan_digest, intent_id)
        stored = self._get_plain(partition, META_RECORD_KEY)
        if stored is None:
            raise _conflict(
                "SubmitPlanIntentRequest",
                "the owner snapshot lost its condition; reread the owner and retry.",
            )
        authority = {
            "root_plan_key": request.root_plan_key,
            "source_plan_key": request.source_plan_key,
            "plan_digest": request.plan_digest,
            "intent_id": intent_id,
            "roster_digest": digest,
            "origin_kind": request.origin.kind.value,
            "origin_member": request.origin.member,
        }
        if any(stored.get(name) != value for name, value in authority.items() if value is not None):
            raise _collision(
                "SubmitPlanIntentRequest",
                "a durable intent under this exact key disagrees with the derived authority.",
            )
        for unit in request.units:
            row = self._get_plain(partition, unit_record_key(unit.unit_key))
            expected = _unit_entry_attributes(unit)
            if row is None or any(row.get(name) != value for name, value in expected.items()):
                raise _collision(
                    "SubmitPlanIntentRequest",
                    "a durable intent unit row disagrees with the exact canonical entry.",
                )

    # --- ClaimPlanIntentRequest -------------------------------------------------------------------

    def claim_plan_intent(self, request: ClaimPlanIntentRequest) -> ClaimPlanIntentResult:
        """Atomically reserve the owner and claim the due pending intent."""
        generation = request.claimed_generation
        intent = request.intent
        owner_key = _key(owner_partition_key(request.plan_digest), SUCCESSOR_OWNER_RECORD_KEY)
        intent_key = _key(
            intent_partition_key(request.plan_digest, intent.intent_id), META_RECORD_KEY
        )
        members = [
            _update_member(
                self._table,
                _UpdateSpec(
                    key=owner_key,
                    pre_state=_owner_item(request.plan_digest, request.owner),
                    condition="revision = :revision AND last_generation = :last_generation AND "
                    "attribute_not_exists(reserved_intent_id)",
                    update="SET revision = :new_revision, reserved_intent_id = :intent_id, "
                    "reserved_intent_digest = :intent_id, "
                    "reserved_generation = :claimed_generation",
                    values={
                        ":revision": request.owner.revision,
                        ":last_generation": request.owner.last_generation,
                        ":new_revision": request.owner.revision + 1,
                        ":intent_id": intent.intent_id,
                        ":claimed_generation": generation,
                    },
                    set_attributes={
                        "revision": request.owner.revision + 1,
                        "reserved_intent_id": intent.intent_id,
                        "reserved_intent_digest": intent.intent_id,
                        "reserved_generation": generation,
                    },
                ),
            ),
            _update_member(
                self._table,
                _UpdateSpec(
                    key=intent_key,
                    pre_state=_intent_meta_item(request.plan_digest, intent),
                    condition="#state = :pending AND intent_id = :intent_id AND "
                    "attribute_not_exists(claimed_generation) AND ready_status = :ready AND "
                    "ready_order = :ready_order",
                    update="SET #state = :claimed, claimed_generation = :claimed_generation, "
                    "last_transition_at = :instant REMOVE ready_status, ready_order",
                    values={
                        ":pending": IntentState.PENDING.value,
                        ":intent_id": intent.intent_id,
                        ":ready": READY_STATUS_VALUE,
                        ":ready_order": ready_order_key(intent.due_at, intent.intent_id),
                        ":claimed": IntentState.CLAIMED.value,
                        ":claimed_generation": generation,
                        ":instant": request.operation_instant,
                    },
                    set_attributes={
                        "state": IntentState.CLAIMED.value,
                        "claimed_generation": generation,
                        "last_transition_at": request.operation_instant,
                    },
                    names={"#state": "state"},
                ),
            ),
        ]
        if request.active_plan is not None:
            meta = request.active_plan.meta
            members.append(
                _condition_member(
                    self._table,
                    _ConditionSpec(
                        key=_key(request.active_plan.plan_key, META_RECORD_KEY),
                        pre_state=_plan_meta_item(request.active_plan.plan_key, meta),
                        condition="#status = :planned AND generation = :generation AND "
                        "origin_intent_id = :origin_intent_id",
                        values={
                            ":planned": PlanStatus.PLANNED.value,
                            ":generation": meta.generation,
                            ":origin_intent_id": meta.origin_intent_id,
                        },
                        names={"#status": "status"},
                    ),
                )
            )
        if request.plan_head is not None:
            members.append(
                self._head_condition_member(
                    request.plan_head.partition_key,
                    request.plan_head.record_key,
                    request.plan_head.head,
                )
            )
        try:
            self._execute("ClaimPlanIntentRequest", tuple(members))
        except _ConditionalAbortError:
            claimed = replace(
                request.intent,
                state=IntentState.CLAIMED,
                claimed_generation=generation,
                ready_status=None,
                ready_order=None,
                last_transition_at=request.operation_instant,
            )
            reserved = replace(
                request.owner,
                revision=request.owner.revision + 1,
                reserved_intent_id=intent.intent_id,
                reserved_intent_digest=intent.intent_id,
                reserved_generation=generation,
            )
            expected = {
                (owner_key["partition_key"], owner_key["record_key"]): _owner_item(
                    request.plan_digest, reserved
                ),
                (intent_key["partition_key"], intent_key["record_key"]): _intent_meta_item(
                    request.plan_digest, claimed
                ),
            }
            if self._replayed(expected):
                return ClaimPlanIntentResult(ClaimPlanIntentOutcome.REPLAYED, generation)
            raise _conflict(
                "ClaimPlanIntentRequest",
                "a competing reservation or owner advance won; classify after a complete "
                "strong reread.",
            ) from None
        return ClaimPlanIntentResult(ClaimPlanIntentOutcome.CLAIMED, generation)

    # --- ReschedulePlanIntentRequest --------------------------------------------------------------

    def reschedule_plan_intent(
        self, request: ReschedulePlanIntentRequest
    ) -> ReschedulePlanIntentResult:
        """Consume the exact reservation and return the same intent to pending."""
        schedule = plan_intent_retry_schedule(request.intent.retry_count, request.operation_instant)
        members = self._reservation_release_members(request, schedule)
        if request.plan_head is not None:
            members.append(
                self._head_condition_member(
                    request.plan_head.partition_key,
                    request.plan_head.record_key,
                    request.plan_head.head,
                )
            )
        try:
            self._execute("ReschedulePlanIntentRequest", tuple(members))
        except _ConditionalAbortError:
            if self._replayed(self._rescheduled_post_state(request, schedule)):
                return ReschedulePlanIntentResult(
                    ReschedulePlanIntentOutcome.REPLAYED, schedule.retry_count, schedule.due_at
                )
            raise _conflict(
                "ReschedulePlanIntentRequest",
                "the exact reservation is gone or the intent moved; reread and classify.",
            ) from None
        return ReschedulePlanIntentResult(
            ReschedulePlanIntentOutcome.RESCHEDULED, schedule.retry_count, schedule.due_at
        )

    def _reservation_release_members(
        self, request: ReschedulePlanIntentRequest, schedule: PlanIntentRetrySchedule
    ) -> list[_Member]:
        intent = request.intent
        generation = intent.claimed_generation
        return [
            _update_member(
                self._table,
                _UpdateSpec(
                    key=_key(owner_partition_key(request.plan_digest), SUCCESSOR_OWNER_RECORD_KEY),
                    pre_state=_owner_item(request.plan_digest, request.owner),
                    condition="revision = :revision AND reserved_intent_id = :intent_id AND "
                    "reserved_intent_digest = :intent_id AND "
                    "reserved_generation = :claimed_generation",
                    update="SET revision = :new_revision REMOVE reserved_intent_id, "
                    "reserved_intent_digest, reserved_generation",
                    values={
                        ":revision": request.owner.revision,
                        ":intent_id": intent.intent_id,
                        ":claimed_generation": generation,
                        ":new_revision": request.owner.revision + 1,
                    },
                    set_attributes={"revision": request.owner.revision + 1},
                ),
            ),
            _update_member(
                self._table,
                _UpdateSpec(
                    key=_key(
                        intent_partition_key(request.plan_digest, intent.intent_id),
                        META_RECORD_KEY,
                    ),
                    pre_state=_intent_meta_item(request.plan_digest, intent),
                    condition="#state = :claimed AND intent_id = :intent_id AND "
                    "claimed_generation = :claimed_generation AND "
                    "roster_digest = :roster_digest",
                    update="SET #state = :pending, retry_count = :retry_count, "
                    "due_at = :due_at, ready_status = :ready, ready_order = :ready_order, "
                    "last_transition_at = :instant REMOVE claimed_generation",
                    values={
                        ":claimed": IntentState.CLAIMED.value,
                        ":intent_id": intent.intent_id,
                        ":claimed_generation": generation,
                        ":roster_digest": intent.roster_digest,
                        ":pending": IntentState.PENDING.value,
                        ":retry_count": schedule.retry_count,
                        ":due_at": schedule.due_at,
                        ":ready": READY_STATUS_VALUE,
                        ":ready_order": ready_order_key(schedule.due_at, intent.intent_id),
                        ":instant": request.operation_instant,
                    },
                    set_attributes={
                        "state": IntentState.PENDING.value,
                        "retry_count": schedule.retry_count,
                        "due_at": schedule.due_at,
                        "ready_status": READY_STATUS_VALUE,
                        "ready_order": ready_order_key(schedule.due_at, intent.intent_id),
                        "last_transition_at": request.operation_instant,
                    },
                    names={"#state": "state"},
                ),
            ),
        ]

    def _rescheduled_post_state(
        self, request: ReschedulePlanIntentRequest, schedule: PlanIntentRetrySchedule
    ) -> dict[tuple[str, str], PlainItem]:
        released = replace(
            request.owner,
            revision=request.owner.revision + 1,
            reserved_intent_id=None,
            reserved_intent_digest=None,
            reserved_generation=None,
        )
        pending = replace(
            request.intent,
            state=IntentState.PENDING,
            claimed_generation=None,
            retry_count=schedule.retry_count,
            due_at=schedule.due_at,
            ready_status=READY_STATUS_VALUE,
            ready_order=ready_order_key(schedule.due_at, request.intent.intent_id),
            last_transition_at=request.operation_instant,
        )
        return {
            (owner_partition_key(request.plan_digest), SUCCESSOR_OWNER_RECORD_KEY): _owner_item(
                request.plan_digest, released
            ),
            (
                intent_partition_key(request.plan_digest, request.intent.intent_id),
                META_RECORD_KEY,
            ): _intent_meta_item(request.plan_digest, pending),
        }

    # --- PersistPlanRequest -----------------------------------------------------------------------

    def persist_plan(self, request: PersistPlanRequest) -> PersistPlanResult:
        """Create the plan and atomically swap the owner; replay retains the generation."""
        if request.source_plan is not None:
            return self._persist_replacement(request)
        generation = request.claimed_generation
        plan_key = request.plan_key
        meta, units = _persisted_plan(request)
        owner_update = self._persist_owner_member(request)
        intent_condition = _condition_member(
            self._table,
            _ConditionSpec(
                key=_key(
                    intent_partition_key(request.plan_digest, request.intent.intent_id),
                    META_RECORD_KEY,
                ),
                pre_state=_intent_meta_item(request.plan_digest, request.intent),
                condition="#state = :claimed AND intent_id = :intent_id AND "
                "claimed_generation = :claimed_generation AND roster_digest = :roster_digest",
                values={
                    ":claimed": IntentState.CLAIMED.value,
                    ":intent_id": request.intent.intent_id,
                    ":claimed_generation": generation,
                    ":roster_digest": request.intent.roster_digest,
                },
                names={"#state": "state"},
            ),
        )
        members = [owner_update, intent_condition]
        members.append(
            _put_member(
                self._table, _PutSpec(item=_plan_meta_item(plan_key, meta), condition=_ABSENT)
            )
        )
        members.extend(
            _put_member(
                self._table, _PutSpec(item=_plan_unit_item(plan_key, unit), condition=_ABSENT)
            )
            for unit in units
        )
        members.extend(self._proof_members(request.proof))
        try:
            self._execute("PersistPlanRequest", tuple(members))
        except _ConditionalAbortError:
            if self._replayed(self._persisted_post_state(request, meta)):
                return PersistPlanResult(
                    PersistPlanOutcome.REPLAYED,
                    plan_key,
                    request.internal_execution_id,
                    generation,
                )
            raise _conflict(
                "PersistPlanRequest",
                "the reservation or claimed intent moved; reread and return the exact winner.",
            ) from None
        return PersistPlanResult(
            PersistPlanOutcome.PERSISTED, plan_key, request.internal_execution_id, generation
        )

    def _persist_owner_member(self, request: PersistPlanRequest) -> _Member:
        generation = request.claimed_generation
        plan_key = request.plan_key
        root = request.owner.root_plan_key or plan_key
        return _update_member(
            self._table,
            _UpdateSpec(
                key=_key(owner_partition_key(request.plan_digest), SUCCESSOR_OWNER_RECORD_KEY),
                pre_state=_owner_item(request.plan_digest, request.owner),
                condition="revision = :revision AND last_generation = :last_generation AND "
                "reserved_intent_id = :intent_id AND reserved_intent_digest = :intent_id AND "
                "reserved_generation = :claimed_generation AND "
                "attribute_not_exists(active_plan_key)",
                update="SET revision = :new_revision, root_plan_key = :root_plan_key, "
                "last_plan_key = :plan_key, last_generation = :claimed_generation, "
                "active_plan_key = :plan_key, active_intent_id = :intent_id, "
                "latest_internal_execution_id = :internal_execution_id "
                "REMOVE reserved_intent_id, reserved_intent_digest, reserved_generation",
                values={
                    ":revision": request.owner.revision,
                    ":last_generation": request.owner.last_generation,
                    ":intent_id": request.intent.intent_id,
                    ":claimed_generation": generation,
                    ":new_revision": request.owner.revision + 1,
                    ":root_plan_key": root,
                    ":plan_key": plan_key,
                    ":internal_execution_id": request.internal_execution_id,
                },
                set_attributes={
                    "revision": request.owner.revision + 1,
                    "root_plan_key": root,
                    "last_plan_key": plan_key,
                    "last_generation": generation,
                    "active_plan_key": plan_key,
                    "active_intent_id": request.intent.intent_id,
                    "latest_internal_execution_id": request.internal_execution_id,
                },
            ),
        )

    def _persisted_post_state(
        self, request: PersistPlanRequest, meta: PlanMeta
    ) -> dict[tuple[str, str], PlainItem]:
        generation = request.claimed_generation
        plan_key = request.plan_key
        root = request.owner.root_plan_key or plan_key
        swapped = replace(
            request.owner,
            revision=request.owner.revision + 1,
            root_plan_key=root,
            last_plan_key=plan_key,
            last_generation=generation,
            active_plan_key=plan_key,
            active_intent_id=request.intent.intent_id,
            latest_internal_execution_id=request.internal_execution_id,
            reserved_intent_id=None,
            reserved_intent_digest=None,
            reserved_generation=None,
        )
        return {
            (owner_partition_key(request.plan_digest), SUCCESSOR_OWNER_RECORD_KEY): _owner_item(
                request.plan_digest, swapped
            ),
            (plan_key, META_RECORD_KEY): _plan_meta_item(plan_key, meta),
        }

    # --- AdmitUnitRequest -------------------------------------------------------------------------

    def admit_unit(self, request: AdmitUnitRequest) -> AdmitUnitResult:
        """Record one queue admission exactly once; the last admission admits the intent."""
        members = self._admit_members(request)
        try:
            self._execute("AdmitUnitRequest", tuple(members))
        except _ConditionalAbortError:
            if self._replayed(self._admitted_post_state(request)):
                return AdmitUnitResult(AdmitUnitOutcome.REPLAYED, request.final)
            raise _conflict(
                "AdmitUnitRequest",
                "the unit, counters, or intent moved; reread and classify via §5.3.",
            ) from None
        return AdmitUnitResult(AdmitUnitOutcome.ADMITTED, request.final)

    def _admit_members(self, request: AdmitUnitRequest) -> list[_Member]:
        meta = request.plan_meta
        generation = meta.generation
        intent_key = _key(
            intent_partition_key(request.plan_digest, request.intent.intent_id), META_RECORD_KEY
        )
        intent_pre = _intent_meta_item(request.plan_digest, request.intent)
        intent_condition = (
            "#state = :claimed AND intent_id = :intent_id AND "
            "claimed_generation = :generation AND attribute_not_exists(ready_status) AND "
            "attribute_not_exists(ready_order)"
        )
        members = [
            _condition_member(
                self._table,
                _ConditionSpec(
                    key=_key(owner_partition_key(request.plan_digest), SUCCESSOR_OWNER_RECORD_KEY),
                    pre_state=_owner_item(request.plan_digest, request.owner),
                    condition="revision = :revision AND active_plan_key = :plan_key AND "
                    "active_intent_id = :intent_id",
                    values={
                        ":revision": request.owner.revision,
                        ":plan_key": request.plan_key,
                        ":intent_id": request.intent.intent_id,
                    },
                ),
            ),
            _update_member(
                self._table,
                _UpdateSpec(
                    key=_key(request.plan_key, META_RECORD_KEY),
                    pre_state=_plan_meta_item(request.plan_key, meta),
                    condition="#status = :planned AND generation = :generation AND "
                    "unit_count = :unit_count AND admitted_count = :admitted_count",
                    update="SET admitted_count = :new_admitted_count",
                    values={
                        ":planned": PlanStatus.PLANNED.value,
                        ":generation": generation,
                        ":unit_count": meta.unit_count,
                        ":admitted_count": meta.admitted_count,
                        ":new_admitted_count": meta.admitted_count + 1,
                    },
                    set_attributes={"admitted_count": meta.admitted_count + 1},
                    names={"#status": "status"},
                ),
            ),
            _update_member(
                self._table,
                _UpdateSpec(
                    key=_key(request.plan_key, unit_record_key(request.unit.unit.unit_key)),
                    pre_state=_plan_unit_item(request.plan_key, request.unit),
                    condition="admission = :pending AND #status = :queued AND "
                    "owner_generation = :generation",
                    update="SET admission = :admitted, "
                    "admission_deduplication_digest = :dedup, admitted_at = :instant",
                    values={
                        ":pending": PlanUnitAdmission.PENDING.value,
                        ":queued": PlanUnitStatus.QUEUED.value,
                        ":generation": generation,
                        ":admitted": PlanUnitAdmission.ADMITTED.value,
                        ":dedup": request.deduplication_digest,
                        ":instant": request.operation_instant,
                    },
                    set_attributes={
                        "admission": PlanUnitAdmission.ADMITTED.value,
                        "admission_deduplication_digest": request.deduplication_digest,
                        "admitted_at": request.operation_instant,
                    },
                    names={"#status": "status"},
                ),
            ),
        ]
        intent_values: dict[str, object] = {
            ":claimed": IntentState.CLAIMED.value,
            ":intent_id": request.intent.intent_id,
            ":generation": generation,
        }
        if request.final:
            members.append(
                _update_member(
                    self._table,
                    _UpdateSpec(
                        key=intent_key,
                        pre_state=intent_pre,
                        condition=intent_condition,
                        update="SET #state = :admitted_state, last_transition_at = :instant",
                        values={
                            **intent_values,
                            ":admitted_state": IntentState.ADMITTED.value,
                            ":instant": request.operation_instant,
                        },
                        set_attributes={
                            "state": IntentState.ADMITTED.value,
                            "last_transition_at": request.operation_instant,
                        },
                        names={"#state": "state"},
                    ),
                )
            )
            proof = request.proof
            if proof is None:  # pragma: no cover -- excluded by the request form
                raise _collision("AdmitUnitRequest", "final admission requires its proof.")
            members.extend(self._proof_members(proof))
        else:
            members.append(
                _condition_member(
                    self._table,
                    _ConditionSpec(
                        key=intent_key,
                        pre_state=intent_pre,
                        condition=intent_condition,
                        values=intent_values,
                        names={"#state": "state"},
                    ),
                )
            )
        return members

    def _admitted_post_state(self, request: AdmitUnitRequest) -> dict[tuple[str, str], PlainItem]:
        admitted_unit = replace(
            request.unit,
            admission=PlanUnitAdmission.ADMITTED,
            admission_deduplication_digest=request.deduplication_digest,
            admitted_at=request.operation_instant,
        )
        advanced = replace(request.plan_meta, admitted_count=request.plan_meta.admitted_count + 1)
        expected = {
            (
                request.plan_key,
                unit_record_key(request.unit.unit.unit_key),
            ): _plan_unit_item(request.plan_key, admitted_unit),
            (request.plan_key, META_RECORD_KEY): _plan_meta_item(request.plan_key, advanced),
        }
        if request.final:
            admitted_intent = replace(
                request.intent,
                state=IntentState.ADMITTED,
                last_transition_at=request.operation_instant,
            )
            expected[
                (
                    intent_partition_key(request.plan_digest, request.intent.intent_id),
                    META_RECORD_KEY,
                )
            ] = _intent_meta_item(request.plan_digest, admitted_intent)
        return expected

    # --- FailAdmissionRequest ---------------------------------------------------------------------

    def fail_admission(self, request: FailAdmissionRequest) -> FailAdmissionResult:
        """Terminalize the candidate and return the same intent to pending atomically."""
        schedule = plan_intent_retry_schedule(request.intent.retry_count, request.operation_instant)
        members = self._fail_admission_members(request, schedule)
        try:
            self._execute("FailAdmissionRequest", tuple(members))
        except _ConditionalAbortError:
            if self._replayed(self._failed_admission_post_state(request, schedule)):
                return FailAdmissionResult(
                    FailAdmissionOutcome.REPLAYED, schedule.retry_count, schedule.due_at
                )
            raise _conflict(
                "FailAdmissionRequest",
                "the candidate or intent moved; reread and classify via §5.3.",
            ) from None
        return FailAdmissionResult(
            FailAdmissionOutcome.FAILED, schedule.retry_count, schedule.due_at
        )

    def _fail_admission_members(
        self, request: FailAdmissionRequest, schedule: PlanIntentRetrySchedule
    ) -> list[_Member]:
        intent = request.intent
        generation = request.plan_meta.generation
        members = [
            _update_member(
                self._table,
                _UpdateSpec(
                    key=_key(owner_partition_key(request.plan_digest), SUCCESSOR_OWNER_RECORD_KEY),
                    pre_state=_owner_item(request.plan_digest, request.owner),
                    condition="revision = :revision AND active_plan_key = :plan_key AND "
                    "active_intent_id = :intent_id AND "
                    "latest_internal_execution_id = :internal_execution_id",
                    update="SET revision = :new_revision REMOVE active_plan_key, active_intent_id",
                    values={
                        ":revision": request.owner.revision,
                        ":plan_key": request.plan_key,
                        ":intent_id": intent.intent_id,
                        ":internal_execution_id": request.plan_meta.internal_execution_id,
                        ":new_revision": request.owner.revision + 1,
                    },
                    set_attributes={"revision": request.owner.revision + 1},
                ),
            ),
            _update_member(
                self._table,
                _UpdateSpec(
                    key=_key(request.plan_key, META_RECORD_KEY),
                    pre_state=_plan_meta_item(request.plan_key, request.plan_meta),
                    condition="#status = :planned AND generation = :generation AND "
                    "origin_intent_id = :intent_id",
                    update="SET #status = :admission_failed, terminal_at = :instant",
                    values={
                        ":planned": PlanStatus.PLANNED.value,
                        ":generation": generation,
                        ":intent_id": intent.intent_id,
                        ":admission_failed": PlanStatus.ADMISSION_FAILED.value,
                        ":instant": request.operation_instant,
                    },
                    set_attributes={
                        "status": PlanStatus.ADMISSION_FAILED.value,
                        "terminal_at": request.operation_instant,
                    },
                    names={"#status": "status"},
                ),
            ),
            _update_member(
                self._table,
                _UpdateSpec(
                    key=_key(
                        intent_partition_key(request.plan_digest, intent.intent_id),
                        META_RECORD_KEY,
                    ),
                    pre_state=_intent_meta_item(request.plan_digest, intent),
                    condition="#state = :claimed AND intent_id = :intent_id AND "
                    "claimed_generation = :generation AND roster_digest = :roster_digest AND "
                    "origin_kind = :origin_kind AND origin_member = :origin_member",
                    update="SET #state = :pending, retry_count = :retry_count, "
                    "due_at = :due_at, ready_status = :ready, ready_order = :ready_order, "
                    "last_transition_at = :instant REMOVE claimed_generation",
                    values={
                        ":claimed": IntentState.CLAIMED.value,
                        ":intent_id": intent.intent_id,
                        ":generation": generation,
                        ":roster_digest": intent.roster_digest,
                        ":origin_kind": intent.origin.kind.value,
                        ":origin_member": intent.origin.member,
                        ":pending": IntentState.PENDING.value,
                        ":retry_count": schedule.retry_count,
                        ":due_at": schedule.due_at,
                        ":ready": READY_STATUS_VALUE,
                        ":ready_order": ready_order_key(schedule.due_at, intent.intent_id),
                        ":instant": request.operation_instant,
                    },
                    set_attributes={
                        "state": IntentState.PENDING.value,
                        "retry_count": schedule.retry_count,
                        "due_at": schedule.due_at,
                        "ready_status": READY_STATUS_VALUE,
                        "ready_order": ready_order_key(schedule.due_at, intent.intent_id),
                        "last_transition_at": request.operation_instant,
                    },
                    names={"#state": "state"},
                ),
            ),
        ]
        members.extend(self._proof_members(request.proof))
        return members

    def _failed_admission_post_state(
        self, request: FailAdmissionRequest, schedule: PlanIntentRetrySchedule
    ) -> dict[tuple[str, str], PlainItem]:
        cleared = replace(
            request.owner,
            revision=request.owner.revision + 1,
            active_plan_key=None,
            active_intent_id=None,
        )
        terminal = replace(
            request.plan_meta,
            status=PlanStatus.ADMISSION_FAILED,
            terminal_at=request.operation_instant,
        )
        pending = replace(
            request.intent,
            state=IntentState.PENDING,
            claimed_generation=None,
            retry_count=schedule.retry_count,
            due_at=schedule.due_at,
            ready_status=READY_STATUS_VALUE,
            ready_order=ready_order_key(schedule.due_at, request.intent.intent_id),
            last_transition_at=request.operation_instant,
        )
        return {
            (owner_partition_key(request.plan_digest), SUCCESSOR_OWNER_RECORD_KEY): _owner_item(
                request.plan_digest, cleared
            ),
            (request.plan_key, META_RECORD_KEY): _plan_meta_item(request.plan_key, terminal),
            (
                intent_partition_key(request.plan_digest, request.intent.intent_id),
                META_RECORD_KEY,
            ): _intent_meta_item(request.plan_digest, pending),
        }

    # --- CompletePlanRequest ----------------------------------------------------------------------

    def complete_plan(self, request: CompletePlanRequest) -> CompletePlanResult:
        """Change the plan to completed and clear the owner active pair atomically."""
        members = self._complete_members(request)
        try:
            self._execute("CompletePlanRequest", tuple(members))
        except _ConditionalAbortError:
            if self._replayed(self._completed_post_state(request)):
                return CompletePlanResult(CompletePlanOutcome.REPLAYED)
            raise _conflict(
                "CompletePlanRequest",
                "a unit, attempt, owner, or plan condition moved; reread and classify.",
            ) from None
        return CompletePlanResult(CompletePlanOutcome.COMPLETED)

    def _complete_members(self, request: CompletePlanRequest) -> list[_Member]:
        meta = request.plan_meta
        members: list[_Member] = []
        for unit in request.units:
            item = dict(unit.item)
            if item.get("partition_key") != request.plan_key:
                raise _collision(
                    "CompletePlanRequest",
                    "a unit observation lies outside this exact plan partition.",
                )
            members.append(
                _condition_member(
                    self._table,
                    _ConditionSpec(
                        key=_key(request.plan_key, unit_record_key(unit.unit_key)),
                        pre_state=item,
                        condition="#status = :attested AND owner_generation = :generation AND "
                        "flow_id = :flow_id",
                        values={
                            ":attested": PlanUnitStatus.ATTESTED.value,
                            ":generation": meta.generation,
                            ":flow_id": unit.flow_id,
                        },
                        names={"#status": "status"},
                    ),
                )
            )
        for attempt in request.attempts:
            partition = table_attempt_partition_key(attempt.physical_table_id)
            record = f"{ATTEMPT_RECORD_PREFIX}{attempt.attempt_id}"
            item = dict(attempt.item)
            if item.get("partition_key") != partition or item.get("record_key") != record:
                raise _collision(
                    "CompletePlanRequest",
                    "an attempt observation lies outside its exact canonical keys.",
                )
            members.append(
                _condition_member(
                    self._table,
                    _ConditionSpec(
                        key=_key(partition, record),
                        pre_state=item,
                        condition="#state = :state AND athena_usage_state = :settled AND "
                        "usage_authority_digest = :digest",
                        values={
                            ":state": attempt.provider_state,
                            ":settled": "settled",
                            ":digest": attempt.usage_authority_digest,
                        },
                        names={"#state": "state"},
                    ),
                )
            )
        members.append(
            _update_member(
                self._table,
                _UpdateSpec(
                    key=_key(owner_partition_key(request.plan_digest), SUCCESSOR_OWNER_RECORD_KEY),
                    pre_state=_owner_item(request.plan_digest, request.owner),
                    condition="revision = :revision AND active_plan_key = :plan_key AND "
                    "active_intent_id = :intent_id AND "
                    "latest_internal_execution_id = :internal_execution_id",
                    update="SET revision = :new_revision REMOVE active_plan_key, active_intent_id",
                    values={
                        ":revision": request.owner.revision,
                        ":plan_key": request.plan_key,
                        ":intent_id": meta.origin_intent_id,
                        ":internal_execution_id": meta.internal_execution_id,
                        ":new_revision": request.owner.revision + 1,
                    },
                    set_attributes={"revision": request.owner.revision + 1},
                ),
            )
        )
        meta_values: dict[str, object] = {
            ":planned": PlanStatus.PLANNED.value,
            ":generation": meta.generation,
            ":unit_count": meta.unit_count,
            ":zero": 0,
            ":settled_count": meta.settled_attempt_count,
            ":completed": PlanStatus.COMPLETED.value,
            ":instant": request.operation_instant,
        }
        members.append(
            _update_member(
                self._table,
                _UpdateSpec(
                    key=_key(request.plan_key, META_RECORD_KEY),
                    pre_state=_plan_meta_item(request.plan_key, meta),
                    condition="#status = :planned AND generation = :generation AND "
                    "unit_count = :unit_count AND admitted_count = :unit_count AND "
                    "attested_count = :unit_count AND failed_count = :zero AND "
                    "settled_attempt_count = :settled_count",
                    update="SET #status = :completed, terminal_at = :instant",
                    values=meta_values,
                    set_attributes={
                        "status": PlanStatus.COMPLETED.value,
                        "terminal_at": request.operation_instant,
                    },
                    names={"#status": "status"},
                ),
            )
        )
        members.extend(self._proof_members(request.proof))
        return members

    def _completed_post_state(
        self, request: CompletePlanRequest
    ) -> dict[tuple[str, str], PlainItem]:
        cleared = replace(
            request.owner,
            revision=request.owner.revision + 1,
            active_plan_key=None,
            active_intent_id=None,
        )
        completed = replace(
            request.plan_meta,
            status=PlanStatus.COMPLETED,
            terminal_at=request.operation_instant,
        )
        return {
            (owner_partition_key(request.plan_digest), SUCCESSOR_OWNER_RECORD_KEY): _owner_item(
                request.plan_digest, cleared
            ),
            (request.plan_key, META_RECORD_KEY): _plan_meta_item(request.plan_key, completed),
        }

    # --- Shared table-fence member assembly -------------------------------------------------------

    def _fence_key(self, table_id: str) -> dict[str, str]:
        return _key(table_attempt_partition_key(table_id), FENCE_RECORD_KEY)

    def _absent_or_clear_fence_member(
        self, table_id: str, observation: FenceObservation | None
    ) -> _Member:
        if observation is None:
            return _condition_member(
                self._table,
                _ConditionSpec(key=self._fence_key(table_id), pre_state=None, condition=_ABSENT),
            )
        return _condition_member(
            self._table,
            _ConditionSpec(
                key=self._fence_key(table_id),
                pre_state=_fence_item(observation),
                condition="#state = :clear AND attribute_not_exists(attempt_id)",
                values={":clear": "clear"},
                names={"#state": "state"},
            ),
        )

    def _exact_snapshot_update(self, key: dict[str, str], spec: _SnapshotUpdate) -> _Member:
        condition, values, names = _exact_condition(spec.snapshot, absent=spec.absent)
        return _update_member(
            self._table,
            _UpdateSpec(
                key=key,
                pre_state=spec.snapshot,
                condition=condition,
                update=spec.update,
                values={**values, **spec.update_values},
                set_attributes=spec.set_attributes,
                names={**names, **spec.update_names},
            ),
        )

    def _exact_snapshot_condition(self, key: dict[str, str], snapshot: PlainItem) -> _Member:
        condition, values, names = _exact_condition(snapshot)
        return _condition_member(
            self._table,
            _ConditionSpec(
                key=key, pre_state=snapshot, condition=condition, values=values, names=names
            ),
        )

    def _expected_event_items(self, members: list[_Member]) -> dict[tuple[str, str], PlainItem]:
        expected: dict[tuple[str, str], PlainItem] = {}
        for member in members:
            action = member.action
            if isinstance(action, PutAction):
                record = str(action.key["record_key"])
                if record.startswith(EVENT_RECORD_PREFIX):
                    partition = str(action.key["partition_key"])
                    expected[(partition, record)] = dict(action.item)
        return expected

    # --- AcquireTableFenceRequest -----------------------------------------------------------------

    def acquire_table_fence(self, request: AcquireTableFenceRequest) -> AcquireTableFenceResult:
        """Acquire the table lease against an absent-or-clear fence, or classify busy."""
        busy = self._classify_acquire_refusal(
            request.observed_fence, request.observed_lease, request
        )
        if busy is not None:
            return busy
        members = [self._acquire_lease_member(request)]
        members.append(
            self._absent_or_clear_fence_member(request.physical_table_id, request.observed_fence)
        )
        if request.proof is not None:
            members.extend(self._proof_members(request.proof))
        try:
            self._execute("AcquireTableFenceRequest", tuple(members))
        except _ConditionalAbortError:
            return self._classify_acquire_abort(request)
        return AcquireTableFenceResult(
            AcquireTableFenceOutcome.ACQUIRED,
            fencing_token=request.new_fencing_token,
            expires_at=request.new_expires_at,
        )

    def _classify_acquire_refusal(
        self,
        fence: FenceObservation | None,
        lease: TableLeaseRecord | None,
        request: AcquireTableFenceRequest,
    ) -> AcquireTableFenceResult | None:
        if fence is not None and fence.state in ("active", "parked"):
            if not reconciliation_valid(fence):
                raise _collision(
                    "AcquireTableFenceRequest",
                    "an active or parked fence carries no exact valid pending or blocked "
                    "reconciliation disposition; the request fails before returning busy.",
                )
            return AcquireTableFenceResult(AcquireTableFenceOutcome.TABLE_ATTEMPT_PENDING)
        if (
            lease is not None
            and lease.holder != request.holder
            and lease.expiry_epoch_micros() > int(request.operation_instant)
        ):
            return AcquireTableFenceResult(
                AcquireTableFenceOutcome.TABLE_BUSY, held_until=lease.expires_at
            )
        return None

    def _acquire_lease_member(self, request: AcquireTableFenceRequest) -> _Member:
        key = {"partition_key": table_lease_partition_key(request.physical_table_id)}
        sets: dict[str, object] = {
            "holder": request.holder,
            "expires_at": request.new_expires_at,
            "fencing_token": request.new_fencing_token,
        }
        update = "SET holder = :holder, expires_at = :expires, fencing_token = :token"
        if request.observed_lease is None:
            return _update_member(
                self._leases_table,
                _UpdateSpec(
                    key=key,
                    pre_state=None,
                    condition=_ABSENT,
                    update=update,
                    values={
                        ":holder": request.holder,
                        ":expires": request.new_expires_at,
                        ":token": request.new_fencing_token,
                    },
                    set_attributes=sets,
                ),
            )
        observed = request.observed_lease
        return _update_member(
            self._leases_table,
            _UpdateSpec(
                key=key,
                pre_state=_lease_item(observed),
                condition="holder = :observed_holder AND fencing_token = :observed_token AND "
                "expires_at = :observed_expires",
                update=update,
                values={
                    ":observed_holder": observed.holder,
                    ":observed_token": observed.fencing_token,
                    ":observed_expires": observed.expires_at,
                    ":holder": request.holder,
                    ":expires": request.new_expires_at,
                    ":token": request.new_fencing_token,
                },
                set_attributes=sets,
            ),
        )

    def _classify_acquire_abort(self, request: AcquireTableFenceRequest) -> AcquireTableFenceResult:
        fence_item = self._get_plain(
            table_attempt_partition_key(request.physical_table_id), FENCE_RECORD_KEY
        )
        if fence_item is not None and fence_item.get("state") in ("active", "parked"):
            observation = _plain_fence_observation(fence_item)
            if not reconciliation_valid(observation):
                raise _collision(
                    "AcquireTableFenceRequest",
                    "a reread active or parked fence carries no valid reconciliation "
                    "disposition; the request fails before returning busy.",
                )
            return AcquireTableFenceResult(AcquireTableFenceOutcome.TABLE_ATTEMPT_PENDING)
        lease = self._get_lease_plain(table_lease_partition_key(request.physical_table_id))
        if lease is not None and lease.get("holder") != request.holder:
            expires = lease.get("expires_at")
            if isinstance(expires, str) and lease_expiry_epoch_micros(expires) > int(
                request.operation_instant
            ):
                return AcquireTableFenceResult(
                    AcquireTableFenceOutcome.TABLE_BUSY, held_until=expires
                )
        raise _conflict(
            "AcquireTableFenceRequest",
            "the exact observed lease or fence moved; strongly reread and reclassify.",
        )

    # --- ReleaseTableFenceRequest -----------------------------------------------------------------

    def release_table_fence(self, request: ReleaseTableFenceRequest) -> ReleaseTableFenceResult:
        """Set the lease expiry to the canonical released instant for the exact holder."""
        update = "SET expires_at = :released"
        if request.lease.parked_at is not None:
            update += " REMOVE parked_at"
        members = [
            _update_member(
                self._leases_table,
                _UpdateSpec(
                    key={"partition_key": request.lease.partition_key},
                    pre_state=_lease_item(request.lease),
                    condition="holder = :holder AND fencing_token = :token",
                    update=update,
                    values={
                        ":holder": request.lease.holder,
                        ":token": request.lease.fencing_token,
                        ":released": RELEASED_LEASE_EXPIRY,
                    },
                    set_attributes={"expires_at": RELEASED_LEASE_EXPIRY},
                ),
            ),
            self._absent_or_clear_fence_member(request.physical_table_id, request.observed_fence),
        ]
        try:
            self._execute("ReleaseTableFenceRequest", tuple(members))
        except _ConditionalAbortError:
            raise _conflict(
                "ReleaseTableFenceRequest",
                "the exact holder/token moved or the fence is no longer clear; release "
                "is impossible while a fence is active.",
            ) from None
        return ReleaseTableFenceResult(ReleaseTableFenceOutcome.RELEASED)

    # --- BeginTableAttemptRequest -----------------------------------------------------------------

    def begin_table_attempt(self, request: BeginTableAttemptRequest) -> BeginTableAttemptResult:
        """Atomically create the intent attempt, activate the fence, and reserve the owner."""
        identity = request.identity()
        attempt_item = self._begin_attempt_item(request)
        members = [
            self._lease_condition_member(request.lease),
            _put_member(self._table, _PutSpec(item=attempt_item, condition=_ABSENT)),
            self._begin_fence_member(request, identity.attempt_id),
        ]
        members.extend(self._begin_owner_members(request, identity.attempt_id))
        graph = tuple(
            self._claim_fence_member(fence, identity.attempt_id, request.physical_table_id)
            for fence in request.graph_claims
        )
        try:
            self._execute("BeginTableAttemptRequest", tuple(members), graph)
        except _GraphMemberAbortError:
            # V-9: a takeover bumped the claim lease; this invocation may begin no attempt.
            raise GraphAuthorityRefusedError(_GRAPH_CLAIM_SUPERSEDED) from None
        except _ConditionalAbortError:
            if self._replayed(self._begun_post_state(request)):
                return BeginTableAttemptResult(
                    BeginTableAttemptOutcome.REPLAYED,
                    identity.attempt_id,
                    identity.client_request_token,
                    identity.envelope_digest,
                    request.ordinal,
                )
            raise _conflict(
                "BeginTableAttemptRequest",
                "the lease, fence, or owner slot moved; strongly reread and reclassify.",
            ) from None
        return BeginTableAttemptResult(
            BeginTableAttemptOutcome.BEGUN,
            identity.attempt_id,
            identity.client_request_token,
            identity.envelope_digest,
            request.ordinal,
        )

    def _lease_condition_member(self, lease: TableLeaseRecord) -> _Member:
        return _condition_member(
            self._leases_table,
            _ConditionSpec(
                key={"partition_key": lease.partition_key},
                pre_state=_lease_item(lease),
                condition="holder = :holder AND fencing_token = :token AND "
                "expires_at = :expires AND attribute_not_exists(parked_at)",
                values={
                    ":holder": lease.holder,
                    ":token": lease.fencing_token,
                    ":expires": lease.expires_at,
                },
            ),
        )

    def _begin_attempt_item(self, request: BeginTableAttemptRequest) -> PlainItem:
        identity = request.identity()
        item: PlainItem = {
            "partition_key": table_attempt_partition_key(request.physical_table_id),
            "record_key": f"{ATTEMPT_RECORD_PREFIX}{identity.attempt_id}",
            "state": "intent",
            "physical_table_id": request.physical_table_id,
            "attempt_id": identity.attempt_id,
            "ordinal": request.ordinal,
            "statement_kind": request.statement_kind,
            "lease_holder": request.lease.holder,
            "fencing_token": request.lease.fencing_token,
            "writer_execution_id": request.writer_execution_id,
            "start_envelope": request.start_envelope,
            "client_request_token": identity.client_request_token,
            "envelope_digest": identity.envelope_digest,
            "reservation_identity": request.reservation_identity,
        }
        if isinstance(request.owner, BeginQueuedUnitOwner):
            item["plan_unit_owner_ref"] = request.owner.unit.plan_key
        else:
            item["inline_run_owner_ref"] = request.owner.run.run_id
        return item

    def _begin_fence_member(self, request: BeginTableAttemptRequest, attempt_id: str) -> _Member:
        digest = binding_digest(
            BindingDigestRequest(
                physical_table_id=request.physical_table_id,
                attempt_id=attempt_id,
                lease_holder=request.lease.holder,
                fencing_token=request.lease.fencing_token,
            )
        )
        pending_key = pending_reconcile_key(request.initial_reconcile_due_micros(), digest)
        condition = _ABSENT
        pre_state: PlainItem | None = None
        values: dict[str, object] = {}
        if request.observed_fence is not None:
            condition = "#state = :clear AND attribute_not_exists(attempt_id)"
            pre_state = _fence_item(request.observed_fence)
            values[":clear"] = "clear"
        sets: dict[str, object] = {
            "state": "active",
            "physical_table_id": request.physical_table_id,
            "attempt_id": attempt_id,
            "lease_holder": request.lease.holder,
            "fencing_token": request.lease.fencing_token,
            "reconcile_status": "pending",
            "reconcile_attempt_count": 0,
            "reconcile_after_or_block": pending_key,
        }
        return _update_member(
            self._table,
            _UpdateSpec(
                key=self._fence_key(request.physical_table_id),
                pre_state=pre_state,
                condition=condition,
                update="SET #state = :active, physical_table_id = :table, "
                "attempt_id = :attempt, lease_holder = :holder, fencing_token = :token, "
                "reconcile_status = :pending, reconcile_attempt_count = :zero, "
                "reconcile_after_or_block = :due_key",
                values={
                    **values,
                    ":active": "active",
                    ":table": request.physical_table_id,
                    ":attempt": attempt_id,
                    ":holder": request.lease.holder,
                    ":token": request.lease.fencing_token,
                    ":pending": "pending",
                    ":zero": 0,
                    ":due_key": pending_key,
                },
                set_attributes=sets,
                names={"#state": "state"},
            ),
        )

    def _count_clause(self, item: Mapping[str, object], name: str, value_alias: str) -> str:
        if name in item:
            return f"{name} = {value_alias}"
        return f"attribute_not_exists({name})"

    def _begin_owner_members(
        self, request: BeginTableAttemptRequest, attempt_id: str
    ) -> list[_Member]:
        binding_sets, binding_values = _begin_binding(request, attempt_id)
        if isinstance(request.owner, BeginQueuedUnitOwner):
            return self._begin_queued_owner_members(request, attempt_id)
        owner = request.owner
        run_item = dict(owner.run.item)
        count = owner.reservation_count
        condition = (
            "#state IN (:open, :reconciled) AND run_id = :run_id AND "
            "execution_id = :writer AND attribute_not_exists(current_attempt_id) AND "
            "next_ordinal = :ordinal AND "
            + self._count_clause(run_item, "accepted_attempt_reservations", ":count")
        )
        values: dict[str, object] = {
            ":open": InlineRunState.OPEN.value,
            ":reconciled": InlineRunState.RECONCILED.value,
            ":run_id": owner.run.run_id,
            ":writer": request.writer_execution_id,
            ":ordinal": request.ordinal,
            ":new_count": count + 1,
            **binding_values,
        }
        if "accepted_attempt_reservations" in run_item:
            values[":count"] = count
        return [
            _update_member(
                self._table,
                _UpdateSpec(
                    key=_key(str(run_item["partition_key"]), str(run_item["record_key"])),
                    pre_state=run_item,
                    condition=condition,
                    update="SET current_attempt_id = :attempt, lease_holder = :holder, "
                    "fencing_token = :token, reserved_slot_identity = :slot, "
                    "accepted_attempt_reservations = :new_count",
                    values=values,
                    set_attributes={
                        **binding_sets,
                        "accepted_attempt_reservations": count + 1,
                    },
                    names={"#state": "state"},
                ),
            )
        ]

    def _begin_queued_owner_members(
        self, request: BeginTableAttemptRequest, attempt_id: str
    ) -> list[_Member]:
        binding_sets, binding_values = _begin_binding(request, attempt_id)
        owner = cast("BeginQueuedUnitOwner", request.owner)
        ordinal = request.ordinal
        unit_item = dict(owner.unit.item)
        meta_item = dict(owner.meta_item)
        unit_count_observed = owner.unit_reservation_count
        plan_count_observed = owner.plan_reservation_count
        unit_condition = (
            "#status = :writing AND writer_execution_id = :writer AND "
            "attribute_not_exists(current_attempt_id) AND "
            + self._count_clause(unit_item, "unit_reservation_count", ":count")
        )
        unit_values: dict[str, object] = {
            ":writing": "writing",
            ":writer": str(unit_item.get("writer_execution_id", "")),
            ":ordinal": ordinal,
            ":new_count": unit_count_observed + 1,
            **binding_values,
        }
        if "unit_reservation_count" in unit_item:
            unit_values[":count"] = unit_count_observed
        meta_condition = (
            "#status = :planned AND unit_count = :unit_count AND "
            "admitted_count = :unit_count AND "
            + self._count_clause(meta_item, "plan_reservation_count", ":plan_count")
        )
        meta_values: dict[str, object] = {
            ":planned": PlanStatus.PLANNED.value,
            ":unit_count": meta_item.get("unit_count"),
            ":new_plan_count": plan_count_observed + 1,
        }
        if "plan_reservation_count" in meta_item:
            meta_values[":plan_count"] = plan_count_observed
        return [
            _update_member(
                self._table,
                _UpdateSpec(
                    key=_key(owner.unit.plan_key, owner.unit.unit_record_key),
                    pre_state=unit_item,
                    condition=unit_condition,
                    update="SET current_attempt_id = :attempt, ordinal = :ordinal, "
                    "lease_holder = :holder, fencing_token = :token, "
                    "reserved_slot_identity = :slot, unit_reservation_count = :new_count",
                    values=unit_values,
                    set_attributes={
                        **binding_sets,
                        "ordinal": ordinal,
                        "unit_reservation_count": unit_count_observed + 1,
                    },
                    names={"#status": "status"},
                ),
            ),
            _update_member(
                self._table,
                _UpdateSpec(
                    key=_key(owner.unit.plan_key, META_RECORD_KEY),
                    pre_state=meta_item,
                    condition=meta_condition,
                    update="SET plan_reservation_count = :new_plan_count",
                    values=meta_values,
                    set_attributes={"plan_reservation_count": plan_count_observed + 1},
                    names={"#status": "status"},
                ),
            ),
        ]

    def _begun_post_state(
        self, request: BeginTableAttemptRequest
    ) -> dict[tuple[str, str], PlainItem]:
        attempt_id = request.identity().attempt_id
        attempt_item = self._begin_attempt_item(request)
        partition = table_attempt_partition_key(request.physical_table_id)
        digest = binding_digest(
            BindingDigestRequest(
                physical_table_id=request.physical_table_id,
                attempt_id=attempt_id,
                lease_holder=request.lease.holder,
                fencing_token=request.lease.fencing_token,
            )
        )
        fence_post: PlainItem = {
            "partition_key": partition,
            "record_key": FENCE_RECORD_KEY,
            "state": "active",
            "physical_table_id": request.physical_table_id,
            "attempt_id": attempt_id,
            "lease_holder": request.lease.holder,
            "fencing_token": request.lease.fencing_token,
            "reconcile_status": "pending",
            "reconcile_attempt_count": 0,
            "reconcile_after_or_block": pending_reconcile_key(
                request.initial_reconcile_due_micros(), digest
            ),
        }
        binding_sets, _ = _begin_binding(request, attempt_id)
        expected: dict[tuple[str, str], PlainItem] = {
            (partition, str(attempt_item["record_key"])): attempt_item,
            (partition, FENCE_RECORD_KEY): fence_post,
        }
        if isinstance(request.owner, BeginQueuedUnitOwner):
            owner = request.owner
            expected[(owner.unit.plan_key, owner.unit.unit_record_key)] = _applied(
                owner.unit.item,
                {
                    **binding_sets,
                    "ordinal": request.ordinal,
                    "unit_reservation_count": owner.unit_reservation_count + 1,
                },
            )
            expected[(owner.unit.plan_key, META_RECORD_KEY)] = _applied(
                owner.meta_item,
                {"plan_reservation_count": owner.plan_reservation_count + 1},
            )
        else:
            run = request.owner.run
            expected[(str(run.item["partition_key"]), str(run.item["record_key"]))] = _applied(
                run.item,
                {
                    **binding_sets,
                    "accepted_attempt_reservations": request.owner.reservation_count + 1,
                },
            )
        return expected

    # --- RecordStage1RefusalRequest ---------------------------------------------------------------

    def record_stage1_refusal(
        self, request: RecordStage1RefusalRequest
    ) -> RecordStage1RefusalResult:
        """Put the deterministic refusal event and advance only its exact owner head."""
        members = self._refusal_owner_members(request)
        members.extend(self._proof_members(request.proof))
        expected_events = self._expected_event_items(members)
        try:
            self._execute("RecordStage1RefusalRequest", tuple(members))
        except _ConditionalAbortError:
            if expected_events and self._replayed(expected_events):
                return RecordStage1RefusalResult(RecordStage1RefusalOutcome.REPLAYED)
            raise _conflict(
                "RecordStage1RefusalRequest",
                "the owner observation is stale or the refusal payload changed; the "
                "event is never rebuilt against a different owner.",
            ) from None
        return RecordStage1RefusalResult(RecordStage1RefusalOutcome.RECORDED)

    def _refusal_owner_members(self, request: RecordStage1RefusalRequest) -> list[_Member]:
        if request.tag is Stage1RefusalTag.UNIT:
            unit = request.unit
            if unit is None:  # pragma: no cover -- excluded by the request form
                raise _collision("RecordStage1RefusalRequest", "the unit tag carries its facts.")
            unit_item = dict(unit.unit_item)
            meta_item = dict(unit.meta_item)
            return [
                _condition_member(
                    self._table,
                    _ConditionSpec(
                        key=_key(unit.plan_key, str(unit_item["record_key"])),
                        pre_state=unit_item,
                        condition="#status = :writing AND writer_execution_id = :writer",
                        values={":writing": "writing", ":writer": unit.writer_execution_id},
                        names={"#status": "status"},
                    ),
                ),
                _condition_member(
                    self._table,
                    _ConditionSpec(
                        key=_key(unit.plan_key, META_RECORD_KEY),
                        pre_state=meta_item,
                        condition="#status = :planned AND generation = :generation AND "
                        "origin_intent_id = :intent",
                        values={
                            ":planned": PlanStatus.PLANNED.value,
                            ":generation": unit.intent.claimed_generation,
                            ":intent": unit.intent.intent_id,
                        },
                        names={"#status": "status"},
                    ),
                ),
                _condition_member(
                    self._table,
                    _ConditionSpec(
                        key=_key(
                            intent_partition_key(unit.plan_digest, unit.intent.intent_id),
                            META_RECORD_KEY,
                        ),
                        pre_state=_intent_meta_item(unit.plan_digest, unit.intent),
                        condition="#state = :admitted AND intent_id = :intent",
                        values={
                            ":admitted": IntentState.ADMITTED.value,
                            ":intent": unit.intent.intent_id,
                        },
                        names={"#state": "state"},
                    ),
                ),
            ]
        inline = request.inline
        if inline is None:  # pragma: no cover -- excluded by the request form
            raise _collision("RecordStage1RefusalRequest", "the inline tag carries its run.")
        run_item = dict(inline.run.item)
        attempt_clause = (
            "current_attempt_id = :bound"
            if "current_attempt_id" in run_item
            else "attribute_not_exists(current_attempt_id)"
        )
        values: dict[str, object] = {
            ":run_id": inline.run.run_id,
            ":open": InlineRunState.OPEN.value,
            ":reconciled": InlineRunState.RECONCILED.value,
        }
        if "current_attempt_id" in run_item:
            values[":bound"] = run_item["current_attempt_id"]
        return [
            _condition_member(
                self._table,
                _ConditionSpec(
                    key=_key(str(run_item["partition_key"]), str(run_item["record_key"])),
                    pre_state=run_item,
                    condition="run_id = :run_id AND #state IN (:open, :reconciled) AND "
                    + attempt_clause,
                    values=values,
                    names={"#state": "state"},
                ),
            )
        ]

    # --- PersistInlineRunRequest ------------------------------------------------------------------

    def persist_inline_run(self, request: PersistInlineRunRequest) -> PersistInlineRunResult:
        """Atomically create the run, its recorded event authority, and the run head."""
        run_id = request.run_id()
        run_item = self._inline_run_item(request, run_id)
        members = [_put_member(self._table, _PutSpec(item=run_item, condition=_ABSENT))]
        members.extend(self._proof_members(request.proof))
        expected_events = self._expected_event_items(members)
        try:
            self._execute("PersistInlineRunRequest", tuple(members))
        except _ConditionalAbortError:
            self._classify_persist_run(run_item, expected_events)
            return PersistInlineRunResult(PersistInlineRunOutcome.REPLAYED, run_id)
        return PersistInlineRunResult(PersistInlineRunOutcome.PERSISTED, run_id)

    def _inline_run_item(self, request: PersistInlineRunRequest, run_id: str) -> PlainItem:
        item: PlainItem = {
            "partition_key": table_attempt_partition_key(request.physical_table_id),
            "record_key": f"{RUN_RECORD_PREFIX}{run_id}",
            "run_id": run_id,
            "writer_kind": request.writer_kind.value,
            "operation_id": request.operation_id,
            "execution_id": request.execution_id,
            "physical_table_id": request.physical_table_id,
            "status_flow_binding": request.proof.flow_id,
            "execution_binding": request.proof.execution_id,
            "table_kind": request.table_kind,
            "statement_intent_kinds": list(request.statement_intent_kinds),
            "created_at": request.created_at,
            "state": InlineRunState.OPEN.value,
            "next_ordinal": 1,
            "accepted_attempt_reservations": 0,
            "attempt_references": [],
        }
        _set_optional(item, "pointer_finalization_payload", request.pointer_finalization_payload)
        return item

    def _classify_persist_run(
        self,
        run_item: PlainItem,
        expected_events: dict[tuple[str, str], PlainItem],
    ) -> None:
        stored = self._get_plain(str(run_item["partition_key"]), str(run_item["record_key"]))
        immutable = (
            "run_id",
            "writer_kind",
            "operation_id",
            "execution_id",
            "physical_table_id",
            "status_flow_binding",
            "execution_binding",
            "table_kind",
            "statement_intent_kinds",
            "pointer_finalization_payload",
            "created_at",
        )
        if stored is None or any(stored.get(name) != run_item.get(name) for name in immutable):
            raise _collision(
                "PersistInlineRunRequest",
                "an occupied run key disagrees with the exact immutable run identity.",
            )
        for (partition, record), item in expected_events.items():
            if self._get_plain(partition, record) != item:
                raise _collision(
                    "PersistInlineRunRequest",
                    "an occupied event key disagrees with the derived recorded authority.",
                )

    # --- RecoverParkedAttemptSubmissionRequest ----------------------------------------------------

    def recover_parked_attempt_submission(
        self, request: RecoverParkedAttemptSubmissionRequest
    ) -> RecoverParkedAttemptSubmissionResult:
        """Attach the recovered identical submission to the exact parked pair atomically."""
        fence_snapshot = _fence_item(request.fence)
        attempt_snapshot = _attempt_item(request.attempt)
        absent = (
            ("query_execution_id", "submitted_at", "sanitized_start_evidence")
            if request.attempt.query_execution_id is None
            else ()
        )
        members = [
            self._exact_snapshot_condition(
                self._fence_key(request.physical_table_id), fence_snapshot
            ),
            self._exact_snapshot_update(
                _key(
                    str(attempt_snapshot["partition_key"]),
                    str(attempt_snapshot["record_key"]),
                ),
                _SnapshotUpdate(
                    snapshot=attempt_snapshot,
                    absent=absent,
                    update="SET query_execution_id = :query, submitted_at = :submitted_at, "
                    "sanitized_start_evidence = :evidence",
                    update_values={
                        ":query": request.query_execution_id,
                        ":submitted_at": request.submitted_at,
                        ":evidence": _evidence_plain(request.sanitized_start_evidence),
                    },
                    set_attributes={
                        "query_execution_id": request.query_execution_id,
                        "submitted_at": request.submitted_at,
                        "sanitized_start_evidence": _evidence_plain(
                            request.sanitized_start_evidence
                        ),
                    },
                ),
            ),
        ]
        try:
            self._execute("RecoverParkedAttemptSubmissionRequest", tuple(members))
        except _ConditionalAbortError:
            return self._classify_recover_abort(request, attempt_snapshot)
        return RecoverParkedAttemptSubmissionResult(RecoverParkedAttemptSubmissionOutcome.RECOVERED)

    def _classify_recover_abort(
        self, request: RecoverParkedAttemptSubmissionRequest, attempt_snapshot: PlainItem
    ) -> RecoverParkedAttemptSubmissionResult:
        stored = self._get_plain(
            str(attempt_snapshot["partition_key"]), str(attempt_snapshot["record_key"])
        )
        if stored is not None and stored.get("state") == "parked":
            triple_identical = (
                stored.get("query_execution_id") == request.query_execution_id
                and stored.get("submitted_at") == request.submitted_at
                and stored.get("sanitized_start_evidence")
                == _evidence_plain(request.sanitized_start_evidence)
            )
            if triple_identical:
                return RecoverParkedAttemptSubmissionResult(
                    RecoverParkedAttemptSubmissionOutcome.REPLAYED
                )
            if stored.get("query_execution_id") is not None:
                raise _collision(
                    "RecoverParkedAttemptSubmissionRequest",
                    "a durable submission disagrees with the recovered id, time, or "
                    "evidence bytes.",
                )
        raise _conflict(
            "RecoverParkedAttemptSubmissionRequest",
            "a concurrent fence-only reschedule, block, or requeue aborted recovery; "
            "strongly reread before any provider observation.",
        )

    # --- RequestAttemptCancellationRequest --------------------------------------------------------

    def request_attempt_cancellation(
        self, request: RequestAttemptCancellationRequest
    ) -> RequestAttemptCancellationResult:
        """Record the write-once cancellation instant against the exact nonterminal attempt."""
        attempt_snapshot = _attempt_item(request.attempt)
        submitted = request.attempt.state == "submitted"
        update = (
            "SET #state = :cancel_state, cancel_requested_at = :instant"
            if submitted
            else "SET cancel_requested_at = :instant"
        )
        sets: dict[str, object] = {"cancel_requested_at": request.cancel_requested_at}
        update_values: dict[str, object] = {":instant": request.cancel_requested_at}
        update_names: dict[str, str] = {}
        if submitted:
            sets["state"] = "cancel_requested"
            update_values[":cancel_state"] = "cancel_requested"
            update_names["#state"] = "state"
        members = [
            self._exact_snapshot_update(
                _key(
                    str(attempt_snapshot["partition_key"]),
                    str(attempt_snapshot["record_key"]),
                ),
                _SnapshotUpdate(
                    snapshot=attempt_snapshot,
                    absent=("cancel_requested_at",),
                    update=update,
                    update_values=update_values,
                    update_names=update_names,
                    set_attributes=sets,
                ),
            ),
            self._exact_snapshot_condition(
                self._fence_key(request.physical_table_id), _fence_item(request.fence)
            ),
            self._cancellation_owner_member(request),
        ]
        if request.proof is not None:
            members.extend(self._proof_members(request.proof))
        try:
            self._execute("RequestAttemptCancellationRequest", tuple(members))
        except _ConditionalAbortError:
            expected: dict[tuple[str, str], PlainItem] = {
                (
                    str(attempt_snapshot["partition_key"]),
                    str(attempt_snapshot["record_key"]),
                ): _applied(attempt_snapshot, sets)
            }
            if request.proof is not None and isinstance(request.owner, InlineRunAttemptOwner):
                run_item = dict(request.owner.item)
                expected[(str(run_item["partition_key"]), str(run_item["record_key"]))] = _applied(
                    run_item, {"state": "cancel_requested"}
                )
            if self._replayed(expected):
                return RequestAttemptCancellationResult(RequestAttemptCancellationOutcome.REPLAYED)
            raise _conflict(
                "RequestAttemptCancellationRequest",
                "the attempt, fence, or owner moved; the durable first cancellation "
                "instant is preserved.",
            ) from None
        return RequestAttemptCancellationResult(RequestAttemptCancellationOutcome.REQUESTED)

    def _cancellation_owner_member(self, request: RequestAttemptCancellationRequest) -> _Member:
        owner = request.owner
        item = dict(owner.item)
        attempt_id = str(request.attempt.attempt_id)
        if isinstance(owner, QueuedUnitAttemptOwner):
            return _condition_member(
                self._table,
                _ConditionSpec(
                    key=_key(owner.plan_key, owner.unit_record_key),
                    pre_state=item,
                    condition="#status = :writing AND current_attempt_id = :attempt",
                    values={":writing": "writing", ":attempt": attempt_id},
                    names={"#status": "status"},
                ),
            )
        if request.proof is not None:
            # §3.2: the inline tagged variant changes the run open|reconciled to
            # cancel_requested against the exact observed snapshot, so no further
            # Begin is accepted against the cancelled run.
            return self._exact_snapshot_update(
                _key(str(item["partition_key"]), str(item["record_key"])),
                _SnapshotUpdate(
                    snapshot=item,
                    update="SET #state = :cancel_state",
                    update_values={":cancel_state": "cancel_requested"},
                    update_names={"#state": "state"},
                    set_attributes={"state": "cancel_requested"},
                ),
            )
        return _condition_member(
            self._table,
            _ConditionSpec(
                key=_key(str(item["partition_key"]), str(item["record_key"])),
                pre_state=item,
                condition="#state = :observed AND current_attempt_id = :attempt",
                values={":observed": item.get("state"), ":attempt": attempt_id},
                names={"#state": "state"},
            ),
        )

    # --- ParkAttemptRequest -----------------------------------------------------------------------

    def park_attempt(self, request: ParkAttemptRequest) -> ParkAttemptResult:
        """Park the exact lease, attempt, and fence atomically at one captured instant."""
        if request.replays_parked_state:
            return ParkAttemptResult(ParkAttemptOutcome.REPLAYED)
        fence_snapshot = _fence_item(request.fence)
        attempt_snapshot = _attempt_item(request.attempt)
        new_count = request.old_reconcile_count + 1
        new_key = self._parked_reconcile_key(request)
        members = [
            _update_member(
                self._leases_table,
                _UpdateSpec(
                    key={"partition_key": request.lease.partition_key},
                    pre_state=_lease_item(request.lease),
                    condition="holder = :holder AND fencing_token = :token AND "
                    "expires_at = :expires AND attribute_not_exists(parked_at)",
                    update="SET parked_at = :instant",
                    values={
                        ":holder": request.lease.holder,
                        ":token": request.lease.fencing_token,
                        ":expires": request.lease.expires_at,
                        ":instant": request.park_instant,
                    },
                    set_attributes={"parked_at": request.park_instant},
                ),
            ),
            self._exact_snapshot_update(
                _key(
                    str(attempt_snapshot["partition_key"]),
                    str(attempt_snapshot["record_key"]),
                ),
                _SnapshotUpdate(
                    snapshot=attempt_snapshot,
                    absent=("parked_at",),
                    update="SET #state = :parked, parked_at = :instant",
                    update_values={":parked": "parked", ":instant": request.park_instant},
                    update_names={"#state": "state"},
                    set_attributes={"state": "parked", "parked_at": request.park_instant},
                ),
            ),
            self._exact_snapshot_update(
                self._fence_key(request.physical_table_id),
                _SnapshotUpdate(
                    snapshot=fence_snapshot,
                    update="SET #state = :parked, parked_at = :instant, "
                    "reconcile_attempt_count = :new_count, reconcile_after_or_block = :new_key",
                    update_values={
                        ":parked": "parked",
                        ":instant": request.park_instant,
                        ":new_count": new_count,
                        ":new_key": new_key,
                    },
                    update_names={"#state": "state"},
                    set_attributes={
                        "state": "parked",
                        "parked_at": request.park_instant,
                        "reconcile_attempt_count": new_count,
                        "reconcile_after_or_block": new_key,
                    },
                ),
            ),
        ]
        try:
            self._execute("ParkAttemptRequest", tuple(members))
        except _ConditionalAbortError:
            if self._replayed(self._parked_post_state(request, attempt_snapshot, fence_snapshot)):
                return ParkAttemptResult(ParkAttemptOutcome.REPLAYED)
            raise _conflict(
                "ParkAttemptRequest",
                "the lease, attempt, or fence moved; strongly reread and reclassify.",
            ) from None
        return ParkAttemptResult(ParkAttemptOutcome.PARKED)

    def _parked_reconcile_key(self, request: ParkAttemptRequest) -> str:
        attempt_id = str(request.fence.attempt_id)
        digest = binding_digest(
            BindingDigestRequest(
                physical_table_id=request.physical_table_id,
                attempt_id=attempt_id,
                lease_holder=request.lease.holder,
                fencing_token=request.lease.fencing_token,
            )
        )
        return pending_reconcile_key(request.new_reconcile_due_micros(), digest)

    def _parked_post_state(
        self, request: ParkAttemptRequest, attempt_snapshot: PlainItem, fence_snapshot: PlainItem
    ) -> dict[tuple[str, str], PlainItem]:
        parked_sets: dict[str, object] = {
            "state": "parked",
            "parked_at": request.park_instant,
        }
        return {
            (
                str(attempt_snapshot["partition_key"]),
                str(attempt_snapshot["record_key"]),
            ): _applied(attempt_snapshot, parked_sets),
            (str(fence_snapshot["partition_key"]), str(fence_snapshot["record_key"])): _applied(
                fence_snapshot,
                {
                    **parked_sets,
                    "reconcile_attempt_count": request.old_reconcile_count + 1,
                    "reconcile_after_or_block": self._parked_reconcile_key(request),
                },
            ),
        }

    # --- CompleteTableAttemptRequest --------------------------------------------------------------

    def complete_table_attempt(
        self, request: CompleteTableAttemptRequest
    ) -> CompleteTableAttemptResult:
        """Record the provider terminal, clear the exact fence, and advance the owner."""
        attempt_snapshot = _attempt_item(request.attempt)
        fence_snapshot = _fence_item(request.fence)
        terminal_sets = self._terminal_sets(request)
        members = [
            self._complete_attempt_member(request, attempt_snapshot, terminal_sets),
            self._complete_fence_member(request, fence_snapshot),
            self._complete_owner_member(request),
        ]
        if request.proof is not None:
            members.extend(self._proof_members(request.proof))
        try:
            self._execute("CompleteTableAttemptRequest", tuple(members))
        except _ConditionalAbortError:
            if self._replayed(self._completed_attempt_post_state(request)):
                return CompleteTableAttemptResult(CompleteTableAttemptOutcome.REPLAYED)
            raise _conflict(
                "CompleteTableAttemptRequest",
                "the attempt, fence, or owner moved; strongly reread and reclassify.",
            ) from None
        return CompleteTableAttemptResult(CompleteTableAttemptOutcome.COMPLETED)

    def _terminal_sets(self, request: CompleteTableAttemptRequest) -> dict[str, object]:
        sets: dict[str, object] = {
            "state": request.provider_state.value,
            "terminal_at": request.terminal_at,
            "athena_usage_state": "unsettled",
        }
        for name in ("query_count", "scanned_bytes", "engine_ms"):
            value: int | None = getattr(request, name)
            if value is not None:
                sets[name] = value
        return sets

    def _complete_attempt_member(
        self,
        request: CompleteTableAttemptRequest,
        attempt_snapshot: PlainItem,
        terminal_sets: dict[str, object],
    ) -> _Member:
        assignments = [
            "#state = :terminal",
            "terminal_at = :terminal_at",
            "athena_usage_state = :usage",
        ]
        update_values: dict[str, object] = {
            ":terminal": request.provider_state.value,
            ":terminal_at": request.terminal_at,
            ":usage": "unsettled",
        }
        for name in ("query_count", "scanned_bytes", "engine_ms"):
            if name in terminal_sets:
                assignments.append(f"{name} = :{name}")
                update_values[f":{name}"] = terminal_sets[name]
        return self._exact_snapshot_update(
            _key(str(attempt_snapshot["partition_key"]), str(attempt_snapshot["record_key"])),
            _SnapshotUpdate(
                snapshot=attempt_snapshot,
                update="SET " + ", ".join(assignments),
                update_values=update_values,
                update_names={"#state": "state"},
                set_attributes=terminal_sets,
            ),
        )

    def _complete_fence_member(
        self, request: CompleteTableAttemptRequest, fence_snapshot: PlainItem
    ) -> _Member:
        removed = tuple(
            sorted(
                name
                for name in fence_snapshot
                if name not in ("partition_key", "record_key", "state", "physical_table_id")
            )
        )
        condition, values, names = _exact_condition(fence_snapshot)
        return _update_member(
            self._table,
            _UpdateSpec(
                key=self._fence_key(request.physical_table_id),
                pre_state=fence_snapshot,
                condition=condition,
                update="SET #state = :clear REMOVE " + ", ".join(removed),
                values={**values, ":clear": "clear"},
                set_attributes={"state": "clear"},
                names={**names, "#state": "state"},
            ),
        )

    def _complete_owner_member(self, request: CompleteTableAttemptRequest) -> _Member:
        owner = request.owner
        item = dict(owner.item)
        attempt_id = str(request.attempt.attempt_id)
        references = cast("list[object]", item.get("attempt_references", []))
        new_references = [*references, attempt_id]
        if isinstance(owner, QueuedUnitAttemptOwner):
            ordinal = cast("int", item["ordinal"])
            return _update_member(
                self._table,
                _UpdateSpec(
                    key=_key(owner.plan_key, owner.unit_record_key),
                    pre_state=item,
                    condition="#status = :writing AND current_attempt_id = :attempt AND "
                    "reserved_slot_identity = :slot AND unit_reservation_count = :count",
                    update="SET ordinal = :new_ordinal, attempt_references = :references "
                    "REMOVE current_attempt_id",
                    values={
                        ":writing": "writing",
                        ":attempt": attempt_id,
                        ":slot": item.get("reserved_slot_identity"),
                        ":count": item.get("unit_reservation_count"),
                        ":new_ordinal": ordinal + 1,
                        ":references": new_references,
                    },
                    set_attributes={
                        "ordinal": ordinal + 1,
                        "attempt_references": new_references,
                    },
                    names={"#status": "status"},
                ),
            )
        ordinal = cast("int", item["next_ordinal"])
        return _update_member(
            self._table,
            _UpdateSpec(
                key=_key(str(item["partition_key"]), str(item["record_key"])),
                pre_state=item,
                condition="#state = :observed AND current_attempt_id = :attempt AND "
                "reserved_slot_identity = :slot AND accepted_attempt_reservations = :count AND "
                "next_ordinal = :ordinal",
                update="SET #state = :resulting, next_ordinal = :new_ordinal, "
                "attempt_references = :references REMOVE current_attempt_id",
                values={
                    ":observed": item.get("state"),
                    ":attempt": attempt_id,
                    ":slot": item.get("reserved_slot_identity"),
                    ":count": item.get("accepted_attempt_reservations"),
                    ":ordinal": ordinal,
                    ":resulting": request.resulting_run_state,
                    ":new_ordinal": ordinal + 1,
                    ":references": new_references,
                },
                set_attributes={
                    "state": request.resulting_run_state,
                    "next_ordinal": ordinal + 1,
                    "attempt_references": new_references,
                },
                names={"#state": "state"},
            ),
        )

    def _completed_attempt_post_state(
        self, request: CompleteTableAttemptRequest
    ) -> dict[tuple[str, str], PlainItem]:
        attempt_snapshot = _attempt_item(request.attempt)
        fence_snapshot = _fence_item(request.fence)
        terminal_sets = self._terminal_sets(request)
        owner = request.owner
        item = dict(owner.item)
        attempt_id = str(request.attempt.attempt_id)
        references = cast("list[object]", item.get("attempt_references", []))
        owner_sets: dict[str, object] = {"attempt_references": [*references, attempt_id]}
        if isinstance(owner, QueuedUnitAttemptOwner):
            owner_sets["ordinal"] = cast("int", item["ordinal"]) + 1
            owner_key = (owner.plan_key, owner.unit_record_key)
        else:
            owner_sets["state"] = request.resulting_run_state
            owner_sets["next_ordinal"] = cast("int", item["next_ordinal"]) + 1
            owner_key = (str(item["partition_key"]), str(item["record_key"]))
        fence_clear: PlainItem = {
            "partition_key": str(fence_snapshot["partition_key"]),
            "record_key": FENCE_RECORD_KEY,
            "state": "clear",
            "physical_table_id": request.physical_table_id,
        }
        return {
            (
                str(attempt_snapshot["partition_key"]),
                str(attempt_snapshot["record_key"]),
            ): _applied(attempt_snapshot, terminal_sets),
            (str(fence_snapshot["partition_key"]), FENCE_RECORD_KEY): fence_clear,
            owner_key: _applied(item, owner_sets, removes=("current_attempt_id",)),
        }

    # --- Private settlement expansion (never an independent port operation) -----------------------

    def _settlement_members(
        self, expansion: Stage1SettlementExpansion
    ) -> tuple[
        list[_Member], tuple[SettledAttemptReference, ...], dict[tuple[str, str], PlainItem]
    ]:
        """Expand ``SettleStage1AthenaUsageRequest`` inside one final owner disposition."""
        members: list[_Member] = []
        references: list[SettledAttemptReference] = []
        expected: dict[tuple[str, str], PlainItem] = {}
        for settled in expansion.attempts:
            snapshot = _attempt_item(settled.attempt)
            authority = settle_stage1_athena_usage(settled.settlement).authority
            if authority is None:  # pragma: no cover -- excluded by the request form
                raise _collision("settlement", "accepted settlement derives one authority.")
            document, kind = _usage_document(authority)
            payload = canonical_authority_bytes(document).decode("ascii")
            digest = canonical_authority_digest(document)
            partition = str(snapshot["partition_key"])
            attempt_record = str(snapshot["record_key"])
            usage_record = f"{USAGE_RECORD_PREFIX}{settled.settlement.attempt_id}"
            settled_sets: dict[str, object] = {
                "athena_usage_state": "settled",
                "owner_disposition": expansion.owner_disposition.value,
                "usage_category": authority.category.value,
                "usage_authority_digest": digest,
            }
            members.append(
                self._exact_snapshot_update(
                    _key(partition, attempt_record),
                    _SnapshotUpdate(
                        snapshot=snapshot,
                        update="SET athena_usage_state = :usage_state, "
                        "owner_disposition = :disposition, usage_category = :category, "
                        "usage_authority_digest = :authority_digest",
                        update_values={
                            ":usage_state": "settled",
                            ":disposition": expansion.owner_disposition.value,
                            ":category": authority.category.value,
                            ":authority_digest": digest,
                        },
                        set_attributes=settled_sets,
                    ),
                )
            )
            usage_item: PlainItem = {
                "partition_key": partition,
                "record_key": usage_record,
                "payload": payload,
                "immutable_payload_digest": digest,
                "authority_kind": kind.value,
                "outbox_state": "pending",
                "outbox_status": "pending",
                "outbox_order": stage1_outbox_order(
                    Stage1OutboxOrderRequest(
                        partition, usage_record, digest, settled.settlement.observed_at
                    )
                ),
                "consecutive_failed_delivery_attempts": 0,
                "athena_usage_state": "settled",
                "is_delta": False,
            }
            members.append(_put_member(self._table, _PutSpec(item=usage_item, condition=_ABSENT)))
            references.append(
                SettledAttemptReference(
                    str(settled.attempt.physical_table_id),
                    str(settled.attempt.attempt_id),
                    digest,
                )
            )
            expected[(partition, attempt_record)] = _applied(snapshot, settled_sets)
            expected[(partition, usage_record)] = usage_item
        return members, tuple(references), expected

    def _headless_event_members(self, proof: Stage1LifecycleProof) -> list[_Member]:
        """Embed one headless pointer proof's event puts; a head effect is a collision."""
        fragments: Stage1LifecycleTransactionItems = self._head_store.transaction_items(proof)
        if fragments.head_write is not None:
            raise _collision(
                "pointer proof", "pointer flows are headless; no head action may exist."
            )
        return [self._fragment_member(put, pre_state=None) for put in fragments.event_puts]

    def _pointer_members(
        self, pointer: CurrentPointerOutcome, projected_at: str
    ) -> tuple[list[_Member], dict[tuple[str, str], PlainItem]]:
        """Derive one pointer outcome's members and its expected advanced post-state."""
        members: list[_Member] = []
        expected: dict[tuple[str, str], PlainItem] = {}
        if pointer.kind is PointerOutcomeKind.ADVANCED:
            observed = pointer.observed_current
            if observed is None:  # pragma: no cover -- excluded by the request form
                raise _collision("pointer", "an advanced outcome carries its CURRENT snapshot.")
            current = dict(observed)
            sets: dict[str, object] = {
                PROJECTED_PUBLICATION_ID: pointer.projected_publication_id,
                PROJECTED_FENCING_TOKEN: pointer.projected_fencing_token,
                PROJECTED_AT: projected_at,
            }
            members.append(
                _update_member(
                    self._table,
                    _UpdateSpec(
                        key=_key(pointer.partition_identity, CURRENT_RECORD),
                        pre_state=current,
                        condition="attribute_exists(publication_id) AND "
                        f"(attribute_not_exists({PROJECTED_PUBLICATION_ID}) OR "
                        f"{PROJECTED_PUBLICATION_ID} = :none OR "
                        f"{PROJECTED_FENCING_TOKEN} < :projected_token)",
                        update=f"SET {PROJECTED_PUBLICATION_ID} = :projected, "
                        f"{PROJECTED_FENCING_TOKEN} = :projected_token, "
                        f"{PROJECTED_AT} = :projected_at",
                        values={
                            ":none": SERVABLE_NONE,
                            ":projected": pointer.projected_publication_id,
                            ":projected_token": pointer.projected_fencing_token,
                            ":projected_at": projected_at,
                        },
                        set_attributes=sets,
                    ),
                )
            )
            expected[(pointer.partition_identity, CURRENT_RECORD)] = _applied(current, sets)
        members.extend(self._headless_event_members(pointer.proof))
        return members, expected

    # --- RescheduleTableAttemptReconcileRequest ---------------------------------------------------

    def reschedule_table_attempt_reconcile(
        self, request: RescheduleTableAttemptReconcileRequest
    ) -> RescheduleTableAttemptReconcileResult:
        """Replace only the pending due key/count against the exact fence/attempt binding."""
        fence_snapshot = _fence_item(request.fence)
        attempt_snapshot = _attempt_item(request.attempt)
        new_count = request.old_reconcile_count + 1
        digest = binding_digest(
            BindingDigestRequest(
                physical_table_id=request.physical_table_id,
                attempt_id=str(request.fence.attempt_id),
                lease_holder=str(request.fence.lease_holder),
                fencing_token=int(cast("int", request.fence.fencing_token)),
            )
        )
        new_key = pending_reconcile_key(request.new_reconcile_due_micros(), digest)
        sets: dict[str, object] = {
            "reconcile_attempt_count": new_count,
            "reconcile_after_or_block": new_key,
        }
        members = [
            self._exact_snapshot_condition(
                _key(str(attempt_snapshot["partition_key"]), str(attempt_snapshot["record_key"])),
                attempt_snapshot,
            ),
            self._exact_snapshot_update(
                self._fence_key(request.physical_table_id),
                _SnapshotUpdate(
                    snapshot=fence_snapshot,
                    update="SET reconcile_attempt_count = :new_count, "
                    "reconcile_after_or_block = :new_key",
                    update_values={":new_count": new_count, ":new_key": new_key},
                    set_attributes=sets,
                ),
            ),
        ]
        try:
            self._execute("RescheduleTableAttemptReconcileRequest", tuple(members))
        except _ConditionalAbortError:
            expected = {
                (str(fence_snapshot["partition_key"]), FENCE_RECORD_KEY): _applied(
                    fence_snapshot, sets
                )
            }
            if self._replayed(expected):
                return RescheduleTableAttemptReconcileResult(
                    RescheduleTableAttemptReconcileOutcome.REPLAYED, new_count, new_key
                )
            raise _conflict(
                "RescheduleTableAttemptReconcileRequest",
                "the fence or attempt moved; strongly reread and reuse the same request "
                "or follow byte-identical rejection with the typed block.",
            ) from None
        return RescheduleTableAttemptReconcileResult(
            RescheduleTableAttemptReconcileOutcome.RESCHEDULED, new_count, new_key
        )

    # --- BlockTableAttemptReconcileRequest --------------------------------------------------------

    def block_table_attempt_reconcile(
        self, request: BlockTableAttemptReconcileRequest
    ) -> BlockTableAttemptReconcileResult:
        """Replace only reconciliation fields with the canonical blocked form plus its case."""
        block_id = request.block_id()
        recovery_ref = request.recovery_ref()
        blocked_sets: dict[str, object] = {
            "reconcile_status": "blocked",
            "reconcile_after_or_block": blocked_reconcile_key(recovery_ref),
            "reconcile_block_id": block_id,
            "reconcile_block_reason": request.reason.value,
            "reconcile_blocked_at": request.block_instant,
            "reconcile_observation_digest": request.observation_digest(),
            "reconcile_recovery_ref": recovery_ref,
        }
        case = ProtectedReconcileCase(
            recovery_ref=recovery_ref,
            state=ProtectedCaseState.BLOCKED,
            physical_table_id=request.physical_table_id,
            attempt_id=str(request.fence.attempt_id),
            reconcile_block_id=block_id,
            binding_digest=request.binding_digest(),
            reason=request.reason,
            blocked_at=request.block_instant,
            observation_digest=request.observation_digest(),
            requeued_at=None,
        )
        case_item = _protected_case_item(case)
        attempt_snapshot = _attempt_item(request.attempt)
        fence_member, fence_expected = self._block_fence_member(request, blocked_sets)
        members = [
            self._exact_snapshot_condition(
                _key(str(attempt_snapshot["partition_key"]), str(attempt_snapshot["record_key"])),
                attempt_snapshot,
            ),
            self._owner_snapshot_condition(request.owner),
            fence_member,
            _put_member(self._table, _PutSpec(item=case_item, condition=_ABSENT)),
        ]
        try:
            self._execute("BlockTableAttemptReconcileRequest", tuple(members))
        except _ConditionalAbortError:
            expected = {
                (str(case_item["partition_key"]), PROTECTED_CASE_RECORD_KEY): case_item,
                **fence_expected,
            }
            if self._replayed(expected):
                return BlockTableAttemptReconcileResult(
                    BlockTableAttemptReconcileOutcome.REPLAYED, block_id, recovery_ref
                )
            raise _conflict(
                "BlockTableAttemptReconcileRequest",
                "the fence, attempt, owner, or case moved; strongly reread and retry "
                "the same typed request until blocked, clear, or changed.",
            ) from None
        return BlockTableAttemptReconcileResult(
            BlockTableAttemptReconcileOutcome.BLOCKED, block_id, recovery_ref
        )

    def _owner_snapshot_condition(self, owner: TableAttemptOwnerSnapshot) -> _Member:
        item = dict(owner.item)
        return self._exact_snapshot_condition(
            _key(str(item["partition_key"]), str(item["record_key"])), item
        )

    def _block_fence_member(
        self, request: BlockTableAttemptReconcileRequest, blocked_sets: dict[str, object]
    ) -> tuple[_Member, dict[tuple[str, str], PlainItem]]:
        snapshot = _fence_item(request.fence)
        rec = request.fence.reconciliation
        absent = tuple(
            sorted(
                name
                for name in _RECONCILIATION_FIELD_ORDER
                if getattr(rec, name) is None and name not in rec.malformed_field_names
            )
        )
        condition, values, names = _exact_condition(snapshot, absent=absent)
        clauses = [condition] if condition else []
        malformed_names: dict[str, str] = {}
        for index, name in enumerate(sorted(rec.malformed_field_names)):
            alias = f"#mf{index}"
            malformed_names[alias] = name
            clauses.append(f"attribute_exists({alias})")
        removes = tuple(
            sorted(
                name
                for name in ("reconcile_attempt_count", "requeued_from_case_ref", "requeued_at")
                if getattr(rec, name) is not None or name in rec.malformed_field_names
            )
        )
        update = (
            "SET reconcile_status = :blocked_status, "
            "reconcile_after_or_block = :blocked_key, reconcile_block_id = :block_id, "
            "reconcile_block_reason = :block_reason, reconcile_blocked_at = :blocked_at, "
            "reconcile_observation_digest = :observation_digest, "
            "reconcile_recovery_ref = :recovery_ref"
        )
        if removes:
            update += " REMOVE " + ", ".join(removes)
        member = _update_member(
            self._table,
            _UpdateSpec(
                key=self._fence_key(request.physical_table_id),
                pre_state=snapshot,
                condition=" AND ".join(clauses),
                update=update,
                values={
                    **values,
                    ":blocked_status": "blocked",
                    ":blocked_key": blocked_sets["reconcile_after_or_block"],
                    ":block_id": blocked_sets["reconcile_block_id"],
                    ":block_reason": blocked_sets["reconcile_block_reason"],
                    ":blocked_at": blocked_sets["reconcile_blocked_at"],
                    ":observation_digest": blocked_sets["reconcile_observation_digest"],
                    ":recovery_ref": blocked_sets["reconcile_recovery_ref"],
                },
                set_attributes=blocked_sets,
                names={**names, **malformed_names},
            ),
        )
        expected = {
            (str(snapshot["partition_key"]), FENCE_RECORD_KEY): _applied(
                snapshot, blocked_sets, removes=removes
            )
        }
        return member, expected

    # --- RequeueBlockedTableAttemptReconcileRequest -----------------------------------------------

    def requeue_blocked_table_attempt_reconcile(
        self, request: RequeueBlockedTableAttemptReconcileRequest
    ) -> RequeueBlockedTableAttemptReconcileResult:
        """Return one canonical block output to canonical pending and audit the case."""
        requeue_id = request.requeue_id()
        pending_key = request.pending_reconcile_key()
        fence_snapshot = _fence_item(request.fence)
        attempt_snapshot = _attempt_item(request.attempt)
        case_snapshot = _protected_case_item(request.case)
        pending_sets: dict[str, object] = {
            "reconcile_status": "pending",
            "reconcile_attempt_count": 0,
            "reconcile_after_or_block": pending_key,
            "requeued_from_case_ref": request.case.recovery_ref,
            "requeued_at": request.requeue_instant,
        }
        block_removes = (
            "reconcile_block_id",
            "reconcile_block_reason",
            "reconcile_blocked_at",
            "reconcile_observation_digest",
            "reconcile_recovery_ref",
        )
        case_sets: dict[str, object] = {
            "state": ProtectedCaseState.REQUEUED.value,
            "requeued_at": request.requeue_instant,
            "requeue_id": requeue_id,
            "requeue_operation_id": request.operation_id,
            "resolution_reason": request.resolution_reason,
        }
        members = [
            self._exact_snapshot_condition(
                _key(str(attempt_snapshot["partition_key"]), str(attempt_snapshot["record_key"])),
                attempt_snapshot,
            ),
            self._owner_snapshot_condition(request.owner),
            self._exact_snapshot_update(
                self._fence_key(request.physical_table_id),
                _SnapshotUpdate(
                    snapshot=fence_snapshot,
                    update="SET reconcile_status = :pending_status, "
                    "reconcile_attempt_count = :zero, reconcile_after_or_block = :pending_key, "
                    "requeued_from_case_ref = :case_ref, requeued_at = :requeued_at "
                    "REMOVE " + ", ".join(block_removes),
                    update_values={
                        ":pending_status": "pending",
                        ":zero": 0,
                        ":pending_key": pending_key,
                        ":case_ref": request.case.recovery_ref,
                        ":requeued_at": request.requeue_instant,
                    },
                    set_attributes=pending_sets,
                ),
            ),
            self._exact_snapshot_update(
                _key(str(case_snapshot["partition_key"]), PROTECTED_CASE_RECORD_KEY),
                _SnapshotUpdate(
                    snapshot=case_snapshot,
                    update="SET #case_state = :requeued_state, requeued_at = :requeued_at, "
                    "requeue_id = :requeue_id, requeue_operation_id = :operation_id, "
                    "resolution_reason = :resolution_reason",
                    update_values={
                        ":requeued_state": ProtectedCaseState.REQUEUED.value,
                        ":requeued_at": request.requeue_instant,
                        ":requeue_id": requeue_id,
                        ":operation_id": request.operation_id,
                        ":resolution_reason": request.resolution_reason,
                    },
                    update_names={"#case_state": "state"},
                    set_attributes=case_sets,
                ),
            ),
        ]
        try:
            self._execute("RequeueBlockedTableAttemptReconcileRequest", tuple(members))
        except _ConditionalAbortError:
            expected = {
                (str(fence_snapshot["partition_key"]), FENCE_RECORD_KEY): _applied(
                    fence_snapshot, pending_sets, removes=block_removes
                ),
                (
                    str(case_snapshot["partition_key"]),
                    PROTECTED_CASE_RECORD_KEY,
                ): _applied(case_snapshot, case_sets),
            }
            if self._replayed(expected):
                return RequeueBlockedTableAttemptReconcileResult(
                    RequeueBlockedTableAttemptReconcileOutcome.ALREADY_REQUEUED, requeue_id
                )
            raise _conflict(
                "RequeueBlockedTableAttemptReconcileRequest",
                "the case or canonical blocked pair is stale, terminal, consumed, or "
                "changed; requeue rejects rather than repairing.",
            ) from None
        return RequeueBlockedTableAttemptReconcileResult(
            RequeueBlockedTableAttemptReconcileOutcome.REQUEUED, requeue_id
        )

    # --- ClaimUnitRequest -------------------------------------------------------------------------

    def claim_unit(self, request: ClaimUnitRequest) -> ClaimUnitResult:
        """Change the exact queued, failed, or dead-writer unit to writing under its owner facts."""
        members = self._claim_unit_members(request)
        expected = self._claimed_unit_post_state(request, members)
        operation = "ClaimUnitRequest#reclaim" if request.reclaim else "ClaimUnitRequest"
        try:
            self._execute(operation, tuple(members))
        except _ConditionalAbortError:
            if self._replayed(expected):
                return ClaimUnitResult(ClaimUnitOutcome.REPLAYED, request.redrive)
            if request.reclaim:
                live = self._classify_reclaim_abort(request)
                if live is not None:
                    return live
            raise _conflict(
                "ClaimUnitRequest",
                "the owner, plan, intent, or unit moved; strongly reread and follow "
                "the exact stale/retry classification.",
            ) from None
        return ClaimUnitResult(ClaimUnitOutcome.CLAIMED, request.redrive)

    def _classify_reclaim_abort(self, request: ClaimUnitRequest) -> ClaimUnitResult | None:
        """Strongly reread the lease; a live prior holder is the typed busy outcome."""
        lease = self._get_lease_plain(table_lease_partition_key(request.physical_table_id))
        if lease is None or lease.get("holder") != request.prior_lease_holder:
            return None
        expires = lease.get("expires_at")
        if isinstance(expires, str) and lease_expiry_epoch_micros(expires) > int(
            request.operation_instant
        ):
            return ClaimUnitResult(
                ClaimUnitOutcome.PRIOR_WRITER_LIVE, request.redrive, held_until=expires
            )
        return None

    def _reclaim_lease_member(self, request: ClaimUnitRequest) -> _Member:
        """Condition the prior holder's lease absent, moved to another holder, or expired.

        The condition is disjunctive and carries no observed pre-state, exactly like
        the absent-or-clear fence condition; the release path writes the canonical
        epoch expiry, so ``expires_at <= :instant`` also covers a released lease.
        """
        return _condition_member(
            self._leases_table,
            _ConditionSpec(
                key={"partition_key": table_lease_partition_key(request.physical_table_id)},
                pre_state=None,
                condition="attribute_not_exists(partition_key) OR holder <> :prior_holder "
                "OR expires_at <= :instant",
                values={
                    ":prior_holder": request.prior_lease_holder,
                    ":instant": canonical_lease_instant(request.operation_instant),
                },
            ),
        )

    def _claim_unit_members(self, request: ClaimUnitRequest) -> list[_Member]:
        meta = request.plan_meta
        unit_item = dict(request.unit_item)
        unit_sets: dict[str, object] = {
            "status": "writing",
            "writer_execution_id": request.writer_execution_id,
        }
        unit_removes = tuple(
            sorted(name for name in ("failure_reason", "failed_at") if name in unit_item)
        )
        unit_update = "SET #status = :writing, writer_execution_id = :writer"
        if unit_removes:
            unit_update += " REMOVE " + ", ".join(unit_removes)
        members = [
            _condition_member(
                self._table,
                _ConditionSpec(
                    key=_key(owner_partition_key(request.plan_digest), SUCCESSOR_OWNER_RECORD_KEY),
                    pre_state=_owner_item(request.plan_digest, request.owner),
                    condition="revision = :revision AND active_plan_key = :plan_key AND "
                    "active_intent_id = :intent_id AND "
                    "latest_internal_execution_id = :internal_execution_id",
                    values={
                        ":revision": request.owner.revision,
                        ":plan_key": request.plan_key,
                        ":intent_id": request.intent.intent_id,
                        ":internal_execution_id": meta.internal_execution_id,
                    },
                ),
            ),
            self._head_condition_member(
                request.plan_head.partition_key,
                request.plan_head.record_key,
                request.plan_head.head,
            ),
            _condition_member(
                self._table,
                _ConditionSpec(
                    key=_key(
                        intent_partition_key(request.plan_digest, request.intent.intent_id),
                        META_RECORD_KEY,
                    ),
                    pre_state=_intent_meta_item(request.plan_digest, request.intent),
                    condition="#state = :admitted AND intent_id = :intent_id",
                    values={
                        ":admitted": IntentState.ADMITTED.value,
                        ":intent_id": request.intent.intent_id,
                    },
                    names={"#state": "state"},
                ),
            ),
            self._claim_unit_meta_member(request),
            self._exact_snapshot_update(
                _key(request.plan_key, str(unit_item["record_key"])),
                _SnapshotUpdate(
                    snapshot=unit_item,
                    update=unit_update,
                    update_values={
                        ":writing": "writing",
                        ":writer": request.writer_execution_id,
                    },
                    update_names={"#status": "status"},
                    set_attributes=unit_sets,
                    # A reclaim durably proves the unit binds no nonterminal attempt.
                    absent=("current_attempt_id",) if request.reclaim else (),
                ),
            ),
        ]
        if request.reclaim:
            members.append(self._reclaim_lease_member(request))
        members.extend(self._proof_members(request.proof))
        return members

    def _claim_unit_meta_member(self, request: ClaimUnitRequest) -> _Member:
        meta = request.plan_meta
        condition = (
            "#status = :planned AND generation = :generation AND unit_count = :unit_count "
            "AND admitted_count = :unit_count AND failed_count = :failed_count"
        )
        values: dict[str, object] = {
            ":planned": PlanStatus.PLANNED.value,
            ":generation": meta.generation,
            ":unit_count": meta.unit_count,
            ":failed_count": meta.failed_count,
        }
        key = _key(request.plan_key, META_RECORD_KEY)
        pre_state = _plan_meta_item(request.plan_key, meta)
        if not request.redrive:
            return _condition_member(
                self._table,
                _ConditionSpec(
                    key=key,
                    pre_state=pre_state,
                    condition=condition,
                    values=values,
                    names={"#status": "status"},
                ),
            )
        return _update_member(
            self._table,
            _UpdateSpec(
                key=key,
                pre_state=pre_state,
                condition=condition,
                update="SET failed_count = :new_failed_count",
                values={**values, ":new_failed_count": meta.failed_count - 1},
                set_attributes={"failed_count": meta.failed_count - 1},
                names={"#status": "status"},
            ),
        )

    def _claimed_unit_post_state(
        self, request: ClaimUnitRequest, members: list[_Member]
    ) -> dict[tuple[str, str], PlainItem]:
        unit_item = dict(request.unit_item)
        unit_removes = tuple(
            sorted(name for name in ("failure_reason", "failed_at") if name in unit_item)
        )
        expected: dict[tuple[str, str], PlainItem] = {
            (request.plan_key, str(unit_item["record_key"])): _applied(
                unit_item,
                {"status": "writing", "writer_execution_id": request.writer_execution_id},
                removes=unit_removes,
            )
        }
        if request.redrive:
            redriven = replace(request.plan_meta, failed_count=request.plan_meta.failed_count - 1)
            expected[(request.plan_key, META_RECORD_KEY)] = _plan_meta_item(
                request.plan_key, redriven
            )
        expected.update(self._expected_event_items(members))
        return expected

    # --- FailUnitRequest --------------------------------------------------------------------------

    def fail_unit(self, request: FailUnitRequest) -> FailUnitResult:
        """Settle referenced attempts and record the closed unit failure disposition."""
        settlement_members, references, settlement_expected = self._settlement_members(
            request.settlement
        )
        complete = _complete_settled_references(request.prior_settled_attempts, references)
        new_digest = plan_settled_attempt_digest(complete) if complete else None
        if request.attempt_budget_exhausted:
            return self._fail_unit_budget(
                request, settlement_members, settlement_expected, len(complete), new_digest
            )
        return self._fail_unit_ordinary(
            request, settlement_members, settlement_expected, len(complete), new_digest
        )

    def _fail_unit_ordinary(  # noqa: PLR0913, PLR0917 -- private branch assembler
        self,
        request: FailUnitRequest,
        settlement_members: list[_Member],
        settlement_expected: dict[tuple[str, str], PlainItem],
        new_settled_count: int,
        new_digest: str | None,
    ) -> FailUnitResult:
        unit_item = dict(cast("Mapping[str, object]", request.unit_item))
        unit_sets: dict[str, object] = {
            "status": PlanUnitStatus.FAILED.value,
            "failure_reason": request.reason.value,
            "failed_at": request.operation_instant,
        }
        meta_member, meta_expected = self._fail_meta_member(
            request, new_settled_count, new_digest, terminalize=False
        )
        members = [
            self._lease_condition_member(request.lease),
            self._absent_or_clear_fence_member(request.physical_table_id, request.observed_fence),
            self._exact_snapshot_update(
                _key(request.plan_key, str(unit_item["record_key"])),
                _SnapshotUpdate(
                    snapshot=unit_item,
                    update="SET #status = :failed_status, failure_reason = :reason, "
                    "failed_at = :instant",
                    update_values={
                        ":failed_status": PlanUnitStatus.FAILED.value,
                        ":reason": request.reason.value,
                        ":instant": request.operation_instant,
                    },
                    update_names={"#status": "status"},
                    set_attributes=unit_sets,
                ),
            ),
            meta_member,
        ]
        members.extend(self._proof_members(request.proof))
        members.extend(settlement_members)
        try:
            self._execute("FailUnitRequest", tuple(members))
        except _ConditionalAbortError:
            expected: dict[tuple[str, str], PlainItem] = {
                (request.plan_key, str(unit_item["record_key"])): _applied(unit_item, unit_sets),
                **meta_expected,
                **settlement_expected,
                **self._expected_event_items(members),
            }
            if self._replayed(expected):
                return FailUnitResult(FailUnitOutcome.REPLAYED)
            raise _conflict(
                "FailUnitRequest",
                "the lease, fence, unit, or plan moved; strongly reread and classify.",
            ) from None
        return FailUnitResult(FailUnitOutcome.FAILED)

    def _fail_unit_budget(  # noqa: PLR0913, PLR0917 -- private branch assembler
        self,
        request: FailUnitRequest,
        settlement_members: list[_Member],
        settlement_expected: dict[tuple[str, str], PlainItem],
        new_settled_count: int,
        new_digest: str | None,
    ) -> FailUnitResult:
        successor_members, successor_meta = self._successor_intent_members(request)
        meta_member, meta_expected = self._fail_meta_member(
            request, new_settled_count, new_digest, terminalize=True
        )
        owner_member, owner_expected = self._consume_owner_member(request)
        members = [
            self._lease_condition_member(request.lease),
            self._absent_or_clear_fence_member(request.physical_table_id, request.observed_fence),
            meta_member,
            owner_member,
        ]
        members.extend(successor_members)
        members.extend(self._proof_members(request.proof))
        members.extend(settlement_members)
        successor_id = successor_meta.intent_id
        try:
            self._execute("FailUnitRequest#attempt-33", tuple(members))
        except _ConditionalAbortError:
            partition = intent_partition_key(request.plan_digest, successor_id)
            expected: dict[tuple[str, str], PlainItem] = {
                **meta_expected,
                **owner_expected,
                (partition, META_RECORD_KEY): _intent_meta_item(
                    request.plan_digest, successor_meta
                ),
                **settlement_expected,
                **self._expected_event_items(members),
            }
            exhaustion = request.budget_exhaustion
            if exhaustion is not None:
                for unit in exhaustion.successor_units:
                    expected[(partition, unit_record_key(unit.unit_key))] = _intent_unit_item(
                        partition, unit
                    )
            if self._replayed(expected):
                return FailUnitResult(FailUnitOutcome.REPLAYED, successor_id)
            raise _conflict(
                "FailUnitRequest",
                "the attempt-33 transaction lost a condition against different durable "
                "bytes; replay cannot fan out and the reread returns the winner.",
            ) from None
        return FailUnitResult(FailUnitOutcome.FAILED, successor_id)

    def _fail_meta_member(  # noqa: PLR0913 -- private branch assembler
        self,
        request: FailUnitRequest,
        new_settled_count: int,
        new_digest: str | None,
        *,
        terminalize: bool,
    ) -> tuple[_Member, dict[tuple[str, str], PlainItem]]:
        meta = request.plan_meta
        digest_clause = (
            "settled_attempt_digest = :old_digest"
            if meta.settled_attempt_digest is not None
            else "attribute_not_exists(settled_attempt_digest)"
        )
        condition = (
            "#status = :planned AND generation = :generation AND unit_count = :unit_count "
            "AND admitted_count = :unit_count AND failed_count = :failed_count AND "
            "settled_attempt_count = :settled_count AND " + digest_clause
        )
        values: dict[str, object] = {
            ":planned": PlanStatus.PLANNED.value,
            ":generation": meta.generation,
            ":unit_count": meta.unit_count,
            ":failed_count": meta.failed_count,
            ":settled_count": meta.settled_attempt_count,
            ":new_failed_count": meta.failed_count + 1,
        }
        if meta.settled_attempt_digest is not None:
            values[":old_digest"] = meta.settled_attempt_digest
        assignments = ["failed_count = :new_failed_count"]
        sets: dict[str, object] = {"failed_count": meta.failed_count + 1}
        if new_settled_count != meta.settled_attempt_count:
            assignments.append("settled_attempt_count = :new_settled_count")
            values[":new_settled_count"] = new_settled_count
            sets["settled_attempt_count"] = new_settled_count
            if new_digest is not None:
                assignments.append("settled_attempt_digest = :new_digest")
                values[":new_digest"] = new_digest
                sets["settled_attempt_digest"] = new_digest
        if terminalize:
            assignments.append("#status = :superseded")
            assignments.append("terminal_at = :terminal_at")
            values[":superseded"] = PlanStatus.SUPERSEDED.value
            values[":terminal_at"] = request.operation_instant
            sets["status"] = PlanStatus.SUPERSEDED.value
            sets["terminal_at"] = request.operation_instant
        member = _update_member(
            self._table,
            _UpdateSpec(
                key=_key(request.plan_key, META_RECORD_KEY),
                pre_state=_plan_meta_item(request.plan_key, meta),
                condition=condition,
                update="SET " + ", ".join(assignments),
                values=values,
                set_attributes=sets,
                names={"#status": "status"},
            ),
        )
        expected = {
            (request.plan_key, META_RECORD_KEY): _applied(
                _plan_meta_item(request.plan_key, meta), sets
            )
        }
        return member, expected

    def _consume_owner_member(
        self, request: FailUnitRequest
    ) -> tuple[_Member, dict[tuple[str, str], PlainItem]]:
        member = _update_member(
            self._table,
            _UpdateSpec(
                key=_key(owner_partition_key(request.plan_digest), SUCCESSOR_OWNER_RECORD_KEY),
                pre_state=_owner_item(request.plan_digest, request.owner),
                condition="revision = :revision AND active_plan_key = :plan_key AND "
                "active_intent_id = :intent_id AND "
                "latest_internal_execution_id = :internal_execution_id",
                update="SET revision = :new_revision REMOVE active_plan_key, active_intent_id",
                values={
                    ":revision": request.owner.revision,
                    ":plan_key": request.plan_key,
                    ":intent_id": request.plan_meta.origin_intent_id,
                    ":internal_execution_id": request.plan_meta.internal_execution_id,
                    ":new_revision": request.owner.revision + 1,
                },
                set_attributes={"revision": request.owner.revision + 1},
            ),
        )
        cleared = replace(
            request.owner,
            revision=request.owner.revision + 1,
            active_plan_key=None,
            active_intent_id=None,
        )
        expected = {
            (owner_partition_key(request.plan_digest), SUCCESSOR_OWNER_RECORD_KEY): _owner_item(
                request.plan_digest, cleared
            )
        }
        return member, expected

    def _successor_intent_members(
        self, request: FailUnitRequest
    ) -> tuple[list[_Member], IntentMeta]:
        exhaustion = request.budget_exhaustion
        if exhaustion is None:  # pragma: no cover -- excluded by the request form
            raise _collision("FailUnitRequest", "the attempt-33 branch carries its roster.")
        intent_id = request.successor_intent_id()
        instant = request.operation_instant
        meta = IntentMeta(
            root_plan_key=request.owner.root_plan_key,
            source_plan_key=request.plan_key,
            plan_digest=request.plan_digest,
            intent_id=intent_id,
            roster_digest=request.plan_meta.roster_digest,
            origin=request.successor_origin(),
            state=IntentState.PENDING,
            claimed_generation=None,
            retry_count=0,
            due_at=instant,
            ready_status=READY_STATUS_VALUE,
            ready_order=ready_order_key(instant, intent_id),
            created_at=instant,
            last_transition_at=instant,
        )
        partition = intent_partition_key(request.plan_digest, intent_id)
        members = [
            _put_member(
                self._table,
                _PutSpec(item=_intent_meta_item(request.plan_digest, meta), condition=_ABSENT),
            )
        ]
        members.extend(
            _put_member(
                self._table, _PutSpec(item=_intent_unit_item(partition, unit), condition=_ABSENT)
            )
            for unit in exhaustion.successor_units
        )
        return members, meta

    # --- FinalizeUnitRequest ----------------------------------------------------------------------

    def finalize_unit(self, request: FinalizeUnitRequest) -> FinalizeUnitResult:
        """Advance pointers, attest the unit, and settle attempts in one transaction."""
        settlement_members, references, settlement_expected = self._settlement_members(
            request.settlement
        )
        complete = _complete_settled_references(request.prior_settled_attempts, references)
        new_digest = plan_settled_attempt_digest(complete) if complete else None
        unit_item = dict(request.unit_item)
        unit_sets: dict[str, object] = {
            "status": PlanUnitStatus.ATTESTED.value,
            "verified_digest": request.verified_digest(),
            "attested_at": request.operation_instant,
        }
        meta_member, meta_expected = self._finalize_meta_member(request, len(complete), new_digest)
        members = [
            self._lease_condition_member(request.lease),
            self._absent_or_clear_fence_member(request.physical_table_id, request.observed_fence),
            self._exact_snapshot_update(
                _key(request.plan_key, str(unit_item["record_key"])),
                _SnapshotUpdate(
                    snapshot=unit_item,
                    update="SET #status = :attested_status, verified_digest = :verified, "
                    "attested_at = :instant",
                    update_values={
                        ":attested_status": PlanUnitStatus.ATTESTED.value,
                        ":verified": request.verified_digest(),
                        ":instant": request.operation_instant,
                    },
                    update_names={"#status": "status"},
                    set_attributes=unit_sets,
                ),
            ),
            meta_member,
        ]
        members.extend(self._proof_members(request.proof))
        projected_at = _projected_at(request.operation_instant)
        pointer_expected: dict[tuple[str, str], PlainItem] = {}
        for pointer in request.pointers:
            pointer_members, advanced = self._pointer_members(pointer, projected_at)
            members.extend(pointer_members)
            pointer_expected.update(advanced)
        members.extend(settlement_members)
        guard = request.mode_guard
        graph = () if guard is None else (self._mode_guard_member(guard),)
        try:
            self._execute("FinalizeUnitRequest", tuple(members), graph)
        except _GraphMemberAbortError:
            # V-13(a): the GRAPH CAS committed after this batch read LEGACY_DRAINING.
            raise GraphAuthorityRefusedError(_AUTHORITY_MODE_CHANGED) from None
        except _ConditionalAbortError:
            expected: dict[tuple[str, str], PlainItem] = {
                (request.plan_key, str(unit_item["record_key"])): _applied(unit_item, unit_sets),
                **meta_expected,
                **pointer_expected,
                **settlement_expected,
                **self._expected_event_items(members),
            }
            if self._replayed(expected):
                return FinalizeUnitResult(FinalizeUnitOutcome.REPLAYED)
            raise _conflict(
                "FinalizeUnitRequest",
                "the lease, fence, unit, plan, pointer, or attempt moved; strongly "
                "reread and classify omit versus advance while holding the lease.",
            ) from None
        return FinalizeUnitResult(FinalizeUnitOutcome.FINALIZED)

    def _finalize_meta_member(
        self, request: FinalizeUnitRequest, new_settled_count: int, new_digest: str | None
    ) -> tuple[_Member, dict[tuple[str, str], PlainItem]]:
        meta = request.plan_meta
        digest_clause = (
            "settled_attempt_digest = :old_digest"
            if meta.settled_attempt_digest is not None
            else "attribute_not_exists(settled_attempt_digest)"
        )
        condition = (
            "#status = :planned AND generation = :generation AND unit_count = :unit_count "
            "AND admitted_count = :unit_count AND attested_count = :attested_count AND "
            "settled_attempt_count = :settled_count AND " + digest_clause
        )
        values: dict[str, object] = {
            ":planned": PlanStatus.PLANNED.value,
            ":generation": meta.generation,
            ":unit_count": meta.unit_count,
            ":attested_count": meta.attested_count,
            ":settled_count": meta.settled_attempt_count,
            ":new_attested_count": meta.attested_count + 1,
        }
        if meta.settled_attempt_digest is not None:
            values[":old_digest"] = meta.settled_attempt_digest
        assignments = ["attested_count = :new_attested_count"]
        sets: dict[str, object] = {"attested_count": meta.attested_count + 1}
        if new_settled_count != meta.settled_attempt_count:
            assignments.append("settled_attempt_count = :new_settled_count")
            values[":new_settled_count"] = new_settled_count
            sets["settled_attempt_count"] = new_settled_count
            if new_digest is not None:
                assignments.append("settled_attempt_digest = :new_digest")
                values[":new_digest"] = new_digest
                sets["settled_attempt_digest"] = new_digest
        member = _update_member(
            self._table,
            _UpdateSpec(
                key=_key(request.plan_key, META_RECORD_KEY),
                pre_state=_plan_meta_item(request.plan_key, meta),
                condition=condition,
                update="SET " + ", ".join(assignments),
                values=values,
                set_attributes=sets,
                names={"#status": "status"},
            ),
        )
        expected = {
            (request.plan_key, META_RECORD_KEY): _applied(
                _plan_meta_item(request.plan_key, meta), sets
            )
        }
        return member, expected

    # --- FinalizeInlineRequest --------------------------------------------------------------------

    def finalize_inline(self, request: FinalizeInlineRequest) -> FinalizeInlineResult:
        """Advance pointers, terminalize the run, and settle attempts in one transaction."""
        settlement_members, _references, settlement_expected = self._settlement_members(
            request.settlement
        )
        run_item = dict(request.run.item)
        run_sets: dict[str, object] = {
            "state": InlineRunState.TERMINAL.value,
            "terminal_outcome": request.settlement.owner_disposition.value,
            "terminal_at": request.operation_instant,
        }
        members = [
            self._lease_condition_member(request.lease),
            self._absent_or_clear_fence_member(request.physical_table_id, request.observed_fence),
            self._exact_snapshot_update(
                _key(str(run_item["partition_key"]), str(run_item["record_key"])),
                _SnapshotUpdate(
                    snapshot=run_item,
                    update="SET #state = :terminal_state, terminal_outcome = :outcome, "
                    "terminal_at = :instant",
                    update_values={
                        ":terminal_state": InlineRunState.TERMINAL.value,
                        ":outcome": request.settlement.owner_disposition.value,
                        ":instant": request.operation_instant,
                    },
                    update_names={"#state": "state"},
                    set_attributes=run_sets,
                ),
            ),
        ]
        members.extend(self._proof_members(request.proof))
        projected_at = _projected_at(request.operation_instant)
        pointer_expected: dict[tuple[str, str], PlainItem] = {}
        for pointer in request.pointers:
            pointer_members, advanced = self._pointer_members(pointer, projected_at)
            members.extend(pointer_members)
            pointer_expected.update(advanced)
        members.extend(settlement_members)
        try:
            self._execute("FinalizeInlineRequest", tuple(members))
        except _ConditionalAbortError:
            expected: dict[tuple[str, str], PlainItem] = {
                (str(run_item["partition_key"]), str(run_item["record_key"])): _applied(
                    run_item, run_sets
                ),
                **pointer_expected,
                **settlement_expected,
                **self._expected_event_items(members),
            }
            if self._replayed(expected):
                return FinalizeInlineResult(FinalizeInlineOutcome.REPLAYED)
            raise _conflict(
                "FinalizeInlineRequest",
                "the lease, fence, run, pointer, or attempt moved; strongly reread "
                "and classify omit versus advance while holding the lease.",
            ) from None
        return FinalizeInlineResult(FinalizeInlineOutcome.FINALIZED)

    # --- PersistPlanRequest planned-plan replacement branch ---------------------------------------

    def _persist_replacement(self, request: PersistPlanRequest) -> PersistPlanResult:
        source = request.source_plan
        if source is None:  # pragma: no cover -- excluded by the request form
            raise _collision("PersistPlanRequest", "replacement carries its source evidence.")
        generation = request.claimed_generation
        plan_key = request.plan_key
        meta, units = _persisted_plan(request)
        source_sets: dict[str, object] = {
            "status": PlanStatus.SUPERSEDED.value,
            "terminal_at": request.operation_instant,
        }
        members = [
            self._replacement_owner_member(request, source.plan_key),
            self._replacement_source_meta_member(request, source, source_sets),
            _put_member(
                self._table, _PutSpec(item=_plan_meta_item(plan_key, meta), condition=_ABSENT)
            ),
        ]
        members.extend(
            _put_member(
                self._table, _PutSpec(item=_plan_unit_item(plan_key, unit), condition=_ABSENT)
            )
            for unit in units
        )
        for unit_item in source.unit_items:
            members.append(
                self._exact_snapshot_condition(
                    _key(source.plan_key, str(unit_item["record_key"])), dict(unit_item)
                )
            )
        for attempt in source.attempts:
            partition = table_attempt_partition_key(attempt.physical_table_id)
            record = f"{ATTEMPT_RECORD_PREFIX}{attempt.attempt_id}"
            item = dict(attempt.item)
            if item.get("partition_key") != partition or item.get("record_key") != record:
                raise _collision(
                    "PersistPlanRequest",
                    "an attempt observation lies outside its exact canonical keys.",
                )
            members.append(
                _condition_member(
                    self._table,
                    _ConditionSpec(
                        key=_key(partition, record),
                        pre_state=item,
                        condition="#state = :state AND athena_usage_state = :settled AND "
                        "usage_authority_digest = :digest",
                        values={
                            ":state": attempt.provider_state,
                            ":settled": "settled",
                            ":digest": attempt.usage_authority_digest,
                        },
                        names={"#state": "state"},
                    ),
                )
            )
        for usage in source.usage_authorities:
            partition = table_attempt_partition_key(usage.physical_table_id)
            members.append(
                _condition_member(
                    self._table,
                    _ConditionSpec(
                        key=_key(partition, f"{USAGE_RECORD_PREFIX}{usage.attempt_id}"),
                        pre_state=dict(usage.item),
                        condition="payload = :payload AND "
                        "immutable_payload_digest = :payload_digest",
                        values={
                            ":payload": usage.payload,
                            ":payload_digest": usage.immutable_payload_digest,
                        },
                    ),
                )
            )
        members.extend(self._proof_members(request.proof))
        try:
            self._execute("PersistPlanRequest#replacement", tuple(members))
        except _ConditionalAbortError:
            expected = self._persisted_post_state(request, meta)
            expected[(source.plan_key, META_RECORD_KEY)] = _applied(
                _plan_meta_item(source.plan_key, source.meta), source_sets
            )
            if self._replayed(expected):
                return PersistPlanResult(
                    PersistPlanOutcome.REPLAYED,
                    plan_key,
                    request.internal_execution_id,
                    generation,
                )
            raise _conflict(
                "PersistPlanRequest",
                "the reservation, source plan, or aggregates moved; reread and return "
                "the exact winner or fail closed.",
            ) from None
        return PersistPlanResult(
            PersistPlanOutcome.PERSISTED, plan_key, request.internal_execution_id, generation
        )

    def _replacement_owner_member(self, request: PersistPlanRequest, source_key: str) -> _Member:
        generation = request.claimed_generation
        plan_key = request.plan_key
        root = request.owner.root_plan_key or plan_key
        return _update_member(
            self._table,
            _UpdateSpec(
                key=_key(owner_partition_key(request.plan_digest), SUCCESSOR_OWNER_RECORD_KEY),
                pre_state=_owner_item(request.plan_digest, request.owner),
                condition="revision = :revision AND last_generation = :last_generation AND "
                "reserved_intent_id = :intent_id AND reserved_intent_digest = :intent_id AND "
                "reserved_generation = :claimed_generation AND "
                "active_plan_key = :source_plan_key AND "
                "active_intent_id = :source_intent_id",
                update="SET revision = :new_revision, root_plan_key = :root_plan_key, "
                "last_plan_key = :plan_key, last_generation = :claimed_generation, "
                "active_plan_key = :plan_key, active_intent_id = :intent_id, "
                "latest_internal_execution_id = :internal_execution_id "
                "REMOVE reserved_intent_id, reserved_intent_digest, reserved_generation",
                values={
                    ":revision": request.owner.revision,
                    ":last_generation": request.owner.last_generation,
                    ":intent_id": request.intent.intent_id,
                    ":claimed_generation": generation,
                    ":source_plan_key": source_key,
                    ":source_intent_id": request.owner.active_intent_id,
                    ":new_revision": request.owner.revision + 1,
                    ":root_plan_key": root,
                    ":plan_key": plan_key,
                    ":internal_execution_id": request.internal_execution_id,
                },
                set_attributes={
                    "revision": request.owner.revision + 1,
                    "root_plan_key": root,
                    "last_plan_key": plan_key,
                    "last_generation": generation,
                    "active_plan_key": plan_key,
                    "active_intent_id": request.intent.intent_id,
                    "latest_internal_execution_id": request.internal_execution_id,
                },
            ),
        )

    def _replacement_source_meta_member(
        self,
        request: PersistPlanRequest,
        source: SourcePlanReplacementEvidence,
        source_sets: dict[str, object],
    ) -> _Member:
        meta = source.meta
        digest_clause = (
            "settled_attempt_digest = :source_settled_digest"
            if meta.settled_attempt_digest is not None
            else "attribute_not_exists(settled_attempt_digest)"
        )
        values: dict[str, object] = {
            ":planned": PlanStatus.PLANNED.value,
            ":source_generation": meta.generation,
            ":source_intent_id": meta.origin_intent_id,
            ":source_roster_digest": meta.roster_digest,
            ":source_settled_count": meta.settled_attempt_count,
            ":superseded": PlanStatus.SUPERSEDED.value,
            ":terminal_at": request.operation_instant,
        }
        if meta.settled_attempt_digest is not None:
            values[":source_settled_digest"] = meta.settled_attempt_digest
        return _update_member(
            self._table,
            _UpdateSpec(
                key=_key(source.plan_key, META_RECORD_KEY),
                pre_state=_plan_meta_item(source.plan_key, meta),
                condition="#status = :planned AND generation = :source_generation AND "
                "origin_intent_id = :source_intent_id AND "
                "roster_digest = :source_roster_digest AND "
                "settled_attempt_count = :source_settled_count AND " + digest_clause,
                update="SET #status = :superseded, terminal_at = :terminal_at",
                values=values,
                set_attributes=source_sets,
                names={"#status": "status"},
            ),
        )


def _usage_document(
    authority: Stage1UsageAuthority,
) -> tuple[dict[str, object], Stage1AuthorityKind]:
    if isinstance(authority, OperationalDataFlowUsageContribution):
        return usage_contribution_document(authority), Stage1AuthorityKind.USAGE_CONTRIBUTION
    return (
        usage_evidence_disposition_document(authority),
        Stage1AuthorityKind.USAGE_EVIDENCE_DISPOSITION,
    )


def _protected_case_item(case: ProtectedReconcileCase) -> PlainItem:
    item: PlainItem = {
        "partition_key": protected_case_partition_key(case.recovery_ref),
        "record_key": PROTECTED_CASE_RECORD_KEY,
        "state": case.state.value,
        "physical_table_id": case.physical_table_id,
        "attempt_id": case.attempt_id,
        "reconcile_block_id": case.reconcile_block_id,
        "binding_digest": case.binding_digest,
        "reason": case.reason.value,
        "blocked_at": case.blocked_at,
    }
    _set_optional(item, "observation_digest", case.observation_digest)
    _set_optional(item, "requeued_at", case.requeued_at)
    _set_optional(item, "requeue_id", case.requeue_id)
    _set_optional(item, "requeue_operation_id", case.requeue_operation_id)
    _set_optional(item, "resolution_reason", case.resolution_reason)
    return item


def _complete_settled_references(
    prior: tuple[SettledAttemptReference, ...], new: tuple[SettledAttemptReference, ...]
) -> tuple[SettledAttemptReference, ...]:
    return tuple(
        sorted(
            (*prior, *new),
            key=lambda ref: (
                ref.physical_table_id.encode("utf-8"),
                ref.attempt_id.encode("utf-8"),
            ),
        )
    )


def _projected_at(operation_instant: str) -> str:
    """Render the pointer projection instant from the captured epoch-microseconds."""
    moment = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=int(operation_instant))
    return moment.isoformat()


# --- Persistence form derivation ------------------------------------------------------------------


def _persisted_plan(request: PersistPlanRequest) -> tuple[PlanMeta, tuple[PlanUnit, ...]]:
    generation = request.claimed_generation
    root = request.owner.root_plan_key or request.plan_key
    flows = {binding.unit_key: binding.flow_id for binding in request.unit_flows}
    expectations = tuple(
        RequiredChildExpectation(flow, generation)
        for flow in sorted(
            (binding.flow_id for binding in request.unit_flows),
            key=lambda flow: flow.encode("utf-8"),
        )
    )
    meta = PlanMeta(
        plan_digest=request.plan_digest,
        internal_execution_id=request.internal_execution_id,
        root_plan_key=root,
        generation=generation,
        source_plan_key=request.intent.source_plan_key,
        origin_intent_id=request.intent.intent_id,
        roster_digest=request.intent.roster_digest,
        origin=request.intent.origin,
        status=PlanStatus.PLANNED,
        unit_count=len(request.units),
        admitted_count=0,
        attested_count=0,
        failed_count=0,
        child_outcome_reduction_version=2,
        required_child_expectations=expectations,
        settled_attempt_count=0,
        settled_attempt_digest=None,
        terminal_at=None,
        created_at=request.operation_instant,
    )
    units = tuple(
        PlanUnit(
            unit=unit,
            flow_id=flows[unit.unit_key],
            owner_generation=generation,
            status=PlanUnitStatus.QUEUED,
            admission=PlanUnitAdmission.PENDING,
            admission_deduplication_digest=None,
            admitted_at=None,
            created_at=request.operation_instant,
        )
        for unit in request.units
    )
    return meta, units
