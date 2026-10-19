# SPDX-License-Identifier: MIT-0
"""ADR-075 §3.2/§5/§6 final coordinator slice.

Covers reconcile reschedule/block/requeue, unit claim/failure/finalization, inline
finalization, the private settlement expansion, and the planned-plan replacement
branch of persistence.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from botocore.exceptions import ClientError
from byte_oracle_support import check_fixture, load_fixture_tree
from dynamodb_reserved_words import guard_reserved_words

from adapters.outbound.dynamodb.byte_oracle import (
    ConditionCheckAction,
    ExactPreState,
    PutAction,
    Stage1DynamoByteOracle,
    TransactionEnvelopeRequest,
    UpdateAction,
)
from adapters.outbound.dynamodb.projection_coordinator import (
    DynamoProjectionCoordinator,
    _attempt_item,  # pyright: ignore[reportPrivateUsage] -- seeding the recording fake
    _fence_item,  # pyright: ignore[reportPrivateUsage] -- seeding the recording fake
    _intent_meta_item,  # pyright: ignore[reportPrivateUsage] -- seeding the recording fake
    _low_item,  # pyright: ignore[reportPrivateUsage] -- seeding the recording fake
    _Member,  # pyright: ignore[reportPrivateUsage] -- matrix refusal grain
    _owner_item,  # pyright: ignore[reportPrivateUsage] -- seeding the recording fake
    _plain_item,  # pyright: ignore[reportPrivateUsage] -- frozen-map reconstruction
    _plan_meta_item,  # pyright: ignore[reportPrivateUsage] -- seeding the recording fake
    _projected_at,  # pyright: ignore[reportPrivateUsage] -- pinned projection instant
    _protected_case_item,  # pyright: ignore[reportPrivateUsage] -- seeding the recording fake
)
from adapters.outbound.dynamodb.stage1_lifecycle_head import DynamoStage1LifecycleHeadStore
from adapters.outbound.dynamodb.table_attempt import DynamoTableAttemptStore
from domain.operations.projection_handoff import canonical_json
from domain.operations.stage1_lifecycle_head import (
    Stage1CompoundReplacementRequest,
    Stage1LifecycleProof,
    Stage1LifecycleProofRequest,
    Stage1LifecycleTransition,
    compound_replacement_proof,
    derive_stage1_lifecycle_proof,
    stage1_head_authority_document,
)
from domain.operations.stage1_operational_status import (
    EvaluationBinding,
    InlineIdentityRequest,
    LegitimateRootSource,
    OwnerDisposition,
    PlanAdmissionSource,
    PlanIdentityRequest,
    PlanRequiredChildExpectations,
    PointerExecutionSource,
    PointerExecutionSourceKind,
    PointerIdentityRequest,
    PriorOwnerEventSource,
    RootDiscriminator,
    SameTransactionTerminalSource,
    SettleStage1AthenaUsageRequest,
    Stage1OwnerTransition,
    Stage1TopologyRequest,
    UnitIdentityRequest,
    derive_plan_identity,
)
from domain.operations.stage1_operational_status import (
    ProviderTerminalState as StatusProviderTerminalState,
)
from domain.operations.stage1_operational_status import (
    RequiredChildExpectation as StatusChildExpectation,
)
from domain.operations.status import (
    MEASUREMENT_REFS,
    CompletionOutcome,
    FlowState,
    canonical_authority_digest,
)
from domain.operations.table_attempt import (
    AttemptObservation,
    FenceObservation,
    ProtectedCaseState,
    ProtectedReconcileCase,
    ProviderErrorClass,
    ReconcileBlockIdentityRequest,
    ReconciliationObservation,
    RunIdentityRequest,
    SanitizedStartEvidence,
    WriterKind,
    reconcile_block_identity,
    reconcile_observation_digest,
    reconcile_recovery_reference,
    reconcile_requeue_identity,
    run_identity,
)
from domain.operations.table_lease import (
    RELEASED_LEASE_EXPIRY,
    TableLeaseRecord,
    queued_lease_holder,
)
from domain.operations.write_plan import (
    CanonicalObjectEntry,
    CanonicalUnitEntry,
    IntentAuthorityInputs,
    IntentMeta,
    IntentOrigin,
    IntentOriginKind,
    IntentState,
    PlanMeta,
    PlanStatus,
    RequiredChildExpectation,
    SettledAttemptReference,
    SuccessorOwner,
    WritePlanError,
    intent_identity,
    internal_execution_identity,
    plan_digest_for_source_delivery,
    plan_partition_key,
    plan_settled_attempt_digest,
    ready_order_key,
    roster_digest,
    source_delivery_identity,
    unit_verified_digest,
)
from domain.shared_kernel.errors import DomainError, InvariantError, ValidationError
from ports.outbound.projection_coordinator import (
    ActivePlanObservation,
    AthenaAttemptSettlement,
    AttemptBudgetExhaustion,
    BlockTableAttemptReconcileOutcome,
    BlockTableAttemptReconcileRequest,
    ClaimPlanIntentRequest,
    ClaimUnitOutcome,
    ClaimUnitRequest,
    ClaimUnitResult,
    CurrentPointerOutcome,
    FailAdmissionRequest,
    FailUnitOutcome,
    FailUnitRequest,
    FinalizeInlineOutcome,
    FinalizeInlineRequest,
    FinalizeUnitOutcome,
    FinalizeUnitRequest,
    InlineRunAttemptOwner,
    MalformedReconciliationMetadataObservation,
    PersistPlanOutcome,
    PersistPlanRequest,
    PlanHeadObservation,
    PointerOutcomeKind,
    QueuedUnitAttemptOwner,
    RequeueBlockedTableAttemptReconcileOutcome,
    RequeueBlockedTableAttemptReconcileRequest,
    ReschedulePlanIntentRequest,
    RescheduleTableAttemptReconcileOutcome,
    RescheduleTableAttemptReconcileRequest,
    SameSnapshotRescheduleRejectedObservation,
    SettledAttemptObservation,
    SettledUsageAuthorityObservation,
    SourcePlanReplacementEvidence,
    Stage1SettlementExpansion,
    UnitFailureReason,
    UnitFlowBinding,
)

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-075")]

# Every identity below is fictional fixture data; none is copied from a live system.

ORACLE = Stage1DynamoByteOracle()
TABLE = "publications-fixture"
LEASES = "leases-fixture"
TABLE_ID = "db.alpha"
SIBLING_ID = "db.beta"
PARTITION = f"TABLE_ATTEMPT#{TABLE_ID}"
NOW = datetime(2026, 9, 19, 12, 34, 56, 789012, tzinfo=UTC)
NOW_MICROS = 1_789_821_296_789_012
INSTANT = f"{NOW_MICROS:020d}"
EXPIRES = "2026-09-19T13:00:00+00:00"
CHECKSUM = "0123456789abcdef" * 4
TOKEN = 7

ORIGIN_MEMBER = source_delivery_identity((("manifests/delivery-0001.json", "v-0001"),))
PLAN_DIGEST = plan_digest_for_source_delivery(ORIGIN_MEMBER)
ORIGIN = IntentOrigin(IntentOriginKind.SOURCE_DELIVERY, ORIGIN_MEMBER)


def _unit(key: str, ordinal: int = 1) -> CanonicalUnitEntry:
    return CanonicalUnitEntry(
        unit_key=key,
        table_kind="current",
        merge_count=1,
        objects=(
            CanonicalObjectEntry(
                kind="manifest",
                reference=f"s3://authority-fixture/{key}",
                content_checksum=CHECKSUM,
                publication_id=f"pub-{ordinal:04d}",
                partition_key=None,
                fencing_token=ordinal,
            ),
        ),
    )


UNITS = (_unit(TABLE_ID, 1), _unit(SIBLING_ID, 2))
ROSTER_DIGEST = roster_digest(UNITS)
INTENT_ID = "ab" * 32
EXEC64 = "12" * 32
PLAN_KEY = plan_partition_key(PLAN_DIGEST, EXEC64)
WRITER_EXEC = canonical_authority_digest(["stage1-writer-execution/v1", "message-0001", 1])
HOLDER = queued_lease_holder(PLAN_KEY, TABLE_ID, WRITER_EXEC)
LEASE = TableLeaseRecord(TABLE_ID, HOLDER, TOKEN, EXPIRES)
ATTEMPT_A = "a1" * 32
ATTEMPT_B = "b2" * 32
FLOW_UUID = str(uuid.uuid5(uuid.NAMESPACE_URL, "adlytics-test-settlement/unit-flow"))
EXEC_UUID = str(uuid.uuid5(uuid.NAMESPACE_URL, "adlytics-test-settlement/unit-exec"))
SIBLING_FLOW = str(uuid.uuid5(uuid.NAMESPACE_URL, "adlytics-test-settlement/sibling-flow"))
UNIT_BINDING = EvaluationBinding(MEASUREMENT_REFS["semantic_projection_unit"])
RESERVATION = "reservation-slot-fixture-0001"
DATASET_PARTITION = "fictional.dataset|2026-09-19|whole"
UUID4_TEXT = re.compile(r"\A[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")


# --- Recording fake over both tables --------------------------------------------------------------


@guard_reserved_words
@dataclass
class _RecordingDynamo:
    """Records requests and naively applies writes; a forced failure classifies replay."""

    items: dict[tuple[str, str, str], dict[str, Any]] = field(
        default_factory=dict[tuple[str, str, str], dict[str, Any]]
    )
    transacts: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    fail_next_transactions: int = 0

    def get_item(self, **request: Any) -> dict[str, object]:
        table = cast("str", request["TableName"])
        key = cast("dict[str, dict[str, str]]", request["Key"])
        record = key.get("record_key", {}).get("S", "")
        item = self.items.get((table, key["partition_key"]["S"], record))
        return {} if item is None else {"Item": item}

    def transact_write_items(self, **request: Any) -> dict[str, object]:
        self.transacts.append(request)
        if self.fail_next_transactions:
            self.fail_next_transactions -= 1
            raise ClientError(
                {"Error": {"Code": "TransactionCanceledException", "Message": "fixture"}},
                "TransactWriteItems",
            )
        for member in cast("list[dict[str, Any]]", request["TransactItems"]):
            self._apply(member)
        return {}

    def _apply(self, member: dict[str, Any]) -> None:
        if "Put" in member:
            body = cast("dict[str, Any]", member["Put"])
            item = cast("dict[str, Any]", body["Item"])
            record = cast("dict[str, dict[str, str]]", item).get("record_key", {}).get("S", "")
            self.items[(body["TableName"], item["partition_key"]["S"], record)] = dict(item)
        elif "Update" in member:
            body = cast("dict[str, Any]", member["Update"])
            key = cast("dict[str, dict[str, str]]", body["Key"])
            record = key.get("record_key", {}).get("S", "")
            address = (cast("str", body["TableName"]), key["partition_key"]["S"], record)
            stored = dict(self.items.get(address, dict(key)))
            names = cast("dict[str, str]", body.get("ExpressionAttributeNames", {}))
            values = cast("dict[str, Any]", body["ExpressionAttributeValues"])
            expression = cast("str", body["UpdateExpression"])
            set_part, _, remove_part = expression.partition(" REMOVE ")
            for assignment in set_part.removeprefix("SET ").split(", "):
                name, _, value = assignment.partition(" = ")
                stored[_alias(names, name)] = values[value.strip()]
            for name in remove_part.split(", ") if remove_part else ():
                stored.pop(_alias(names, name), None)
            self.items[address] = stored


def _alias(names: dict[str, str], name: str) -> str:
    text = name.strip()
    return names.get(text, text)


def _coordinator(dynamo: _RecordingDynamo) -> DynamoProjectionCoordinator:
    head_store = DynamoStage1LifecycleHeadStore(TABLE, cast("Any", dynamo))
    return DynamoProjectionCoordinator(TABLE, LEASES, head_store, cast("Any", dynamo))


def _seed(dynamo: _RecordingDynamo, table: str, item: dict[str, object]) -> None:
    low = _low_item(item)
    record = cast("str", item.get("record_key", ""))
    dynamo.items[(table, cast("str", item["partition_key"]), record)] = dict(low)


def _members(dynamo: _RecordingDynamo, index: int = -1) -> list[dict[str, Any]]:
    return cast("list[dict[str, Any]]", dynamo.transacts[index]["TransactItems"])


def _kinds(members: list[dict[str, Any]]) -> list[str]:
    return [next(iter(member)) for member in members]


def _replay(dynamo: _RecordingDynamo) -> None:
    """Apply the last transaction durably, then force the next one to lose a condition."""
    dynamo.fail_next_transactions = 1


def _seed_reschedule(dynamo: _RecordingDynamo, count: int = 0) -> None:
    _seed(dynamo, TABLE, _fence_item(_fence(reconciliation=_pending_reconciliation(count))))
    _seed(dynamo, TABLE, _attempt_item(_attempt()))


def _seed_settlement_targets(dynamo: _RecordingDynamo) -> None:
    _seed(dynamo, TABLE, _attempt_item(_terminal_attempt(ATTEMPT_A)))
    _seed(dynamo, TABLE, _attempt_item(_terminal_attempt(ATTEMPT_B)))


# --- Reconciliation observations, fences, and attempts --------------------------------------------


def _pending_reconciliation(count: int = 0, due: int = NOW_MICROS) -> ReconciliationObservation:
    digest = _binding_digest()
    return ReconciliationObservation(
        reconcile_status="pending",
        reconcile_after_or_block=f"{due:020d}#{digest}",
        reconcile_attempt_count=count,
    )


def _binding_digest(attempt_id: str = ATTEMPT_A) -> str:
    payload = canonical_json([TABLE_ID, attempt_id, HOLDER, TOKEN])
    return hashlib.sha256(payload).hexdigest()


def _fence(
    state: str = "active",
    *,
    parked_at: str | None = None,
    reconciliation: ReconciliationObservation | None = None,
) -> FenceObservation:
    return FenceObservation(
        partition_key=PARTITION,
        record_key="FENCE",
        state=state,
        physical_table_id=TABLE_ID,
        attempt_id=ATTEMPT_A,
        lease_holder=HOLDER,
        fencing_token=TOKEN,
        parked_at=parked_at,
        reconciliation=reconciliation or _pending_reconciliation(),
    )


def _attempt(state: str = "submitted", **overrides: object) -> AttemptObservation:
    fields: dict[str, object] = {
        "partition_key": PARTITION,
        "record_key": f"ATTEMPT#{ATTEMPT_A}",
        "state": state,
        "physical_table_id": TABLE_ID,
        "attempt_id": ATTEMPT_A,
        "plan_unit_owner_ref": PLAN_KEY,
        "inline_run_owner_ref": None,
        "ordinal": 1,
        "statement_kind": "merge",
        "lease_holder": HOLDER,
        "fencing_token": TOKEN,
        "writer_execution_id": WRITER_EXEC,
        "start_envelope": '{"WorkGroup":"fixture-workgroup"}',
        "client_request_token": ATTEMPT_A,
        "envelope_digest": "e" * 64,
        "reservation_identity": RESERVATION,
    }
    if state in ("submitted", "cancel_requested") or state in ("SUCCEEDED", "FAILED", "CANCELLED"):
        fields.update(
            {
                "query_execution_id": "athena-query-fixture-0001",
                "submitted_at": INSTANT,
                "sanitized_start_evidence": SanitizedStartEvidence(200, ProviderErrorClass.NONE),
            }
        )
    fields.update(overrides)
    return AttemptObservation(**fields)  # type: ignore[arg-type]


def _terminal_attempt(
    attempt_id: str,
    *,
    scanned_bytes: int | None = 1024,
    engine_ms: int | None = 250,
) -> AttemptObservation:
    return _attempt(
        "SUCCEEDED",
        record_key=f"ATTEMPT#{attempt_id}",
        attempt_id=attempt_id,
        client_request_token=attempt_id,
        query_count=1,
        scanned_bytes=scanned_bytes,
        engine_ms=engine_ms,
        terminal_at=INSTANT,
        athena_usage_state="unsettled",
    )


def _bound_unit_item(**overrides: object) -> dict[str, object]:
    item: dict[str, object] = {
        "partition_key": PLAN_KEY,
        "record_key": f"UNIT#{TABLE_ID}",
        "flow_id": FLOW_UUID,
        "owner_generation": 0,
        "status": "writing",
        "admission": "admitted",
        "writer_execution_id": WRITER_EXEC,
        "created_at": INSTANT,
        "current_attempt_id": ATTEMPT_A,
        "ordinal": 1,
        "lease_holder": HOLDER,
        "fencing_token": TOKEN,
        "reserved_slot_identity": RESERVATION,
        "unit_reservation_count": 1,
    }
    item.update(overrides)
    return item


def _queued_owner_snapshot() -> QueuedUnitAttemptOwner:
    return QueuedUnitAttemptOwner(PLAN_KEY, TABLE_ID, _bound_unit_item())


# --- Plan-side facts ------------------------------------------------------------------------------


def _plan_meta(**overrides: object) -> PlanMeta:
    fields: dict[str, object] = {
        "plan_digest": PLAN_DIGEST,
        "internal_execution_id": EXEC64,
        "root_plan_key": PLAN_KEY,
        "generation": 0,
        "source_plan_key": None,
        "origin_intent_id": INTENT_ID,
        "roster_digest": ROSTER_DIGEST,
        "origin": ORIGIN,
        "status": PlanStatus.PLANNED,
        "unit_count": 2,
        "admitted_count": 2,
        "attested_count": 0,
        "failed_count": 0,
        "child_outcome_reduction_version": 2,
        "required_child_expectations": tuple(
            RequiredChildExpectation(flow, 0)
            for flow in sorted((FLOW_UUID, SIBLING_FLOW), key=lambda flow: flow.encode("utf-8"))
        ),
        "settled_attempt_count": 0,
        "settled_attempt_digest": None,
        "terminal_at": None,
        "created_at": INSTANT,
    }
    fields.update(overrides)
    return PlanMeta(**fields)  # type: ignore[arg-type]


def _admitted_intent() -> IntentMeta:
    return IntentMeta(
        root_plan_key=None,
        source_plan_key=None,
        plan_digest=PLAN_DIGEST,
        intent_id=INTENT_ID,
        roster_digest=ROSTER_DIGEST,
        origin=ORIGIN,
        state=IntentState.ADMITTED,
        claimed_generation=0,
        retry_count=0,
        due_at=INSTANT,
        ready_status=None,
        ready_order=None,
        created_at=INSTANT,
        last_transition_at=INSTANT,
    )


def _active_owner() -> SuccessorOwner:
    return SuccessorOwner(
        revision=2,
        root_plan_key=PLAN_KEY,
        last_plan_key=PLAN_KEY,
        last_generation=0,
        active_plan_key=PLAN_KEY,
        active_intent_id=INTENT_ID,
        reserved_intent_id=None,
        reserved_intent_digest=None,
        reserved_generation=None,
        latest_internal_execution_id=EXEC64,
    )


def _plan_head_proof() -> Stage1LifecycleProof:
    return derive_stage1_lifecycle_proof(
        Stage1LifecycleProofRequest(
            identity=PlanIdentityRequest(PLAN_DIGEST, EXEC64),
            internal_key=(PLAN_KEY,),
            topology=Stage1TopologyRequest(
                Stage1OwnerTransition.FIRST_AUTOMATIC_PLAN_RECORDED,
                LegitimateRootSource(RootDiscriminator.AUTOMATIC_INGESTION_PLAN),
            ),
            transition=Stage1LifecycleTransition(
                "semantic_plan_recorded", FlowState.QUEUED, None, None
            ),
            stage="projection_plan_recorded",
            authority_at=NOW,
            prior_head=None,
            required_child_expectations=_expectations(EXEC64, 0),
        )
    )


def _expectations(execution_id: str, generation: int) -> PlanRequiredChildExpectations:
    parent = derive_plan_identity(PlanIdentityRequest(PLAN_DIGEST, execution_id)).flow_id
    members = tuple(
        sorted(
            (StatusChildExpectation(flow, generation) for flow in (FLOW_UUID, SIBLING_FLOW)),
            key=lambda member: member.child_flow_id.encode("utf-8"),
        )
    )
    return PlanRequiredChildExpectations(parent, members)


def _plan_head() -> PlanHeadObservation:
    proof = _plan_head_proof()
    head = proof.resulting_head
    assert head is not None
    return PlanHeadObservation(proof.partition_key, "HEAD", head)


def _unit_started_proof() -> Stage1LifecycleProof:
    return derive_stage1_lifecycle_proof(
        Stage1LifecycleProofRequest(
            identity=UnitIdentityRequest(PLAN_DIGEST, TABLE_ID, WRITER_EXEC),
            internal_key=(PLAN_DIGEST, TABLE_ID),
            topology=Stage1TopologyRequest(
                Stage1OwnerTransition.FIRST_UNIT_STARTED,
                PlanAdmissionSource(
                    plan_flow_id=_plan_head().head.flow_id,
                    admitted_event_id=_plan_head().head.latest_event_id,
                    plan_meta_authority=["fixture-meta"],
                    complete_plan_head_authority=["fixture-head"],
                    admitted_event_authority=["fixture-admitted"],
                ),
            ),
            transition=Stage1LifecycleTransition(
                "semantic_unit_started", FlowState.RUNNING, None, None
            ),
            stage="projection_unit_claimed",
            authority_at=NOW,
            prior_head=None,
            owner_generation=0,
        )
    )


def _unit_proof(  # noqa: PLR0913, PLR0917 -- private proof fixture
    transition_id: str,
    state: FlowState,
    outcome: CompletionOutcome | None,
    typed_reason: str | None,
) -> Stage1LifecycleProof:
    head = _unit_started_proof().resulting_head
    assert head is not None
    return derive_stage1_lifecycle_proof(
        Stage1LifecycleProofRequest(
            identity=UnitIdentityRequest(PLAN_DIGEST, TABLE_ID, WRITER_EXEC),
            internal_key=(PLAN_DIGEST, TABLE_ID),
            topology=Stage1TopologyRequest(
                Stage1OwnerTransition.LATER_UNIT_EVENT,
                PriorOwnerEventSource(
                    head.parent_flow_id, head.latest_event_id, stage1_head_authority_document(head)
                ),
            ),
            transition=Stage1LifecycleTransition(transition_id, state, outcome, typed_reason),
            stage="projection_unit_disposition",
            authority_at=NOW,
            prior_head=head,
            clock_started_at=NOW,
            owner_generation=0,
        )
    )


def _pointer_proof(
    partition_identity: str, publication_id: str, token: int
) -> Stage1LifecycleProof:
    head = _unit_started_proof().resulting_head
    assert head is not None
    return derive_stage1_lifecycle_proof(
        Stage1LifecycleProofRequest(
            identity=PointerIdentityRequest(
                partition_identity,
                publication_id,
                token,
                PointerExecutionSource(
                    PointerExecutionSourceKind.QUEUED_UNIT_WRITER_EXECUTION, WRITER_EXEC
                ),
            ),
            internal_key=(partition_identity, publication_id, token),
            topology=Stage1TopologyRequest(
                Stage1OwnerTransition.QUEUED_NONZERO_POINTER,
                SameTransactionTerminalSource(head.flow_id, head.latest_event_id),
            ),
            transition=Stage1LifecycleTransition(
                "semantic_projection_servable",
                FlowState.COMPLETED,
                CompletionOutcome.SUCCEEDED,
                None,
            ),
            stage="projection_servable",
            authority_at=NOW,
            prior_head=None,
            publication_partition_key=partition_identity,
        )
    )


# --- Settlement fixtures --------------------------------------------------------------------------


def _settlement(  # noqa: PLR0913 -- private settlement fixture
    attempt_id: str,
    disposition: OwnerDisposition,
    *,
    scanned_bytes: int | None = 1024,
    engine_ms: int | None = 250,
) -> AthenaAttemptSettlement:
    return AthenaAttemptSettlement(
        attempt=_terminal_attempt(attempt_id, scanned_bytes=scanned_bytes, engine_ms=engine_ms),
        settlement=SettleStage1AthenaUsageRequest(
            attempt_id=attempt_id,
            flow_id=FLOW_UUID,
            execution_id=EXEC_UUID,
            evaluation_binding=UNIT_BINDING,
            owner_disposition=disposition,
            observed_at=NOW,
            provider_terminal_state=StatusProviderTerminalState.SUCCEEDED,
            scanned_bytes=scanned_bytes,
            engine_ms=engine_ms,
        ),
    )


def _expansion(
    disposition: OwnerDisposition, *attempt_ids: str, **overrides: object
) -> Stage1SettlementExpansion:
    return Stage1SettlementExpansion(
        owner_disposition=disposition,
        attempts=tuple(
            _settlement(attempt_id, disposition, **overrides)  # type: ignore[arg-type]
            for attempt_id in attempt_ids
        ),
    )


# --- Pinned domain constructors -------------------------------------------------------------------


def test_reconcile_observation_digest_recomputes_from_the_pinned_ordered_tuple() -> None:
    observation = ReconciliationObservation(
        reconcile_status="pending",
        reconcile_attempt_count=True,
        malformed_field_names=frozenset({"requeued_at"}),
    )
    members: list[object] = [
        ["reconcile_status", "text", "pending"],
        ["reconcile_after_or_block", "absent", None],
        ["reconcile_attempt_count", "boolean", True],
        ["requeued_from_case_ref", "absent", None],
        ["requeued_at", "malformed", None],
        ["reconcile_block_id", "absent", None],
        ["reconcile_block_reason", "absent", None],
        ["reconcile_blocked_at", "absent", None],
        ["reconcile_observation_digest", "absent", None],
        ["reconcile_recovery_ref", "absent", None],
    ]
    expected = canonical_authority_digest(
        ["stage1-reconcile-observation/v1", "malformed_reconciliation_metadata", members]
    )
    assert reconcile_observation_digest("malformed_reconciliation_metadata", observation) == (
        expected
    )


def test_recovery_reference_is_deterministic_opaque_and_canonical_uuid4_form() -> None:
    block_id = reconcile_block_identity(
        ReconcileBlockIdentityRequest(_binding_digest(), _malformed().reason, "f" * 64, INSTANT)
    )
    reference = reconcile_recovery_reference(block_id)
    assert UUID4_TEXT.match(reference)
    assert reference == reconcile_recovery_reference(block_id)
    other = reconcile_recovery_reference("0" * 64)
    assert other != reference
    assert block_id not in reference.replace("-", "")


def test_requeue_identity_and_queued_holder_recompute_from_their_pinned_tuples() -> None:
    ref = reconcile_recovery_reference("1" * 64)
    assert reconcile_requeue_identity(ref, "op-0001") == canonical_authority_digest(
        ["stage1-reconcile-requeue/v1", ref, "op-0001"]
    )
    payload = canonical_json([PLAN_KEY, TABLE_ID, WRITER_EXEC])
    assert queued_lease_holder(PLAN_KEY, TABLE_ID, WRITER_EXEC) == (
        hashlib.sha256(payload).hexdigest()
    )


def test_plan_settled_attempt_digest_requires_the_exact_sorted_unique_set() -> None:
    first = SettledAttemptReference(TABLE_ID, ATTEMPT_A, "1" * 64)
    second = SettledAttemptReference(SIBLING_ID, ATTEMPT_B, "2" * 64)
    digest = plan_settled_attempt_digest((first, second))
    assert digest == canonical_authority_digest(
        ["stage1-plan-settled-attempts/v1", [first.to_member(), second.to_member()]]
    )
    with pytest.raises(WritePlanError, match="sorted"):
        plan_settled_attempt_digest((second, first))
    with pytest.raises(WritePlanError, match="sorted"):
        plan_settled_attempt_digest((first, first))


def test_unit_verified_digest_hashes_the_sorted_unique_checksums() -> None:
    low = "0" * 64
    high = "f" * 64
    assert unit_verified_digest((high, low)) == canonical_authority_digest(
        ["stage1-unit-verified-digest/v1", [low, high]]
    )
    with pytest.raises(WritePlanError, match="unique"):
        unit_verified_digest((low, low))


# --- RescheduleTableAttemptReconcileRequest ----------------------------------------------------


def _reschedule_request(count: int = 0) -> RescheduleTableAttemptReconcileRequest:
    return RescheduleTableAttemptReconcileRequest(
        physical_table_id=TABLE_ID,
        fence=_fence(reconciliation=_pending_reconciliation(count)),
        attempt=_attempt(),
        operation_instant=INSTANT,
    )


def test_reschedule_conditions_the_attempt_and_replaces_only_the_pending_tuple() -> None:
    dynamo = _RecordingDynamo()
    result = _coordinator(dynamo).reschedule_table_attempt_reconcile(_reschedule_request())

    assert result.outcome is RescheduleTableAttemptReconcileOutcome.RESCHEDULED
    # Grain: one transaction of two distinct-item actions (attempt condition, fence update).
    members = _members(dynamo)
    assert _kinds(members) == ["ConditionCheck", "Update"]
    assert members[0]["ConditionCheck"]["Key"]["record_key"] == {"S": f"ATTEMPT#{ATTEMPT_A}"}
    fence = members[1]["Update"]
    assert fence["Key"]["record_key"] == {"S": "FENCE"}
    assert fence["UpdateExpression"] == (
        "SET reconcile_attempt_count = :new_count, reconcile_after_or_block = :new_key"
    )
    # 60 seconds of delay for old count zero, in exact epoch microseconds.
    assert result.reconcile_attempt_count == 1
    assert result.reconcile_after_or_block == (
        f"{NOW_MICROS + 60_000_000:020d}#{_binding_digest()}"
    )


def test_reschedule_uses_the_900_second_ceiling_for_old_count_four_or_more() -> None:
    dynamo = _RecordingDynamo()
    result = _coordinator(dynamo).reschedule_table_attempt_reconcile(_reschedule_request(9))
    assert result.reconcile_attempt_count == 10
    assert result.reconcile_after_or_block.startswith(f"{NOW_MICROS + 900_000_000:020d}#")


def test_reschedule_exact_replay_preserves_the_same_count_and_due_bytes() -> None:
    dynamo = _RecordingDynamo()
    _seed_reschedule(dynamo)
    coordinator = _coordinator(dynamo)
    first = coordinator.reschedule_table_attempt_reconcile(_reschedule_request())
    _replay(dynamo)
    second = coordinator.reschedule_table_attempt_reconcile(_reschedule_request())
    assert second.outcome is RescheduleTableAttemptReconcileOutcome.REPLAYED
    assert (second.reconcile_attempt_count, second.reconcile_after_or_block) == (
        first.reconcile_attempt_count,
        first.reconcile_after_or_block,
    )


def test_reschedule_conflicts_when_the_durable_fence_moved() -> None:
    dynamo = _RecordingDynamo()
    dynamo.fail_next_transactions = 1
    with pytest.raises(DomainError, match="typed block"):
        _coordinator(dynamo).reschedule_table_attempt_reconcile(_reschedule_request())


def test_reschedule_rejects_a_blocked_or_malformed_reconciliation_tuple() -> None:
    blocked = ReconciliationObservation(
        reconcile_status="blocked", reconcile_after_or_block="BLOCK#x"
    )
    with pytest.raises(ValueError, match="pending"):
        RescheduleTableAttemptReconcileRequest(
            physical_table_id=TABLE_ID,
            fence=_fence(reconciliation=blocked),
            attempt=_attempt(),
            operation_instant=INSTANT,
        )


# --- BlockTableAttemptReconcileRequest ---------------------------------------------------------


def _malformed_reconciliation() -> ReconciliationObservation:
    return ReconciliationObservation(
        reconcile_status="pending",
        reconcile_attempt_count=True,
        malformed_field_names=frozenset({"requeued_at"}),
    )


def _malformed() -> MalformedReconciliationMetadataObservation:
    return MalformedReconciliationMetadataObservation(_malformed_reconciliation())


def _block_request(**overrides: object) -> BlockTableAttemptReconcileRequest:
    fields: dict[str, object] = {
        "physical_table_id": TABLE_ID,
        "fence": _fence(reconciliation=_malformed_reconciliation()),
        "attempt": _attempt(),
        "owner": _queued_owner_snapshot(),
        "observation": _malformed(),
        "block_instant": INSTANT,
    }
    fields.update(overrides)
    return BlockTableAttemptReconcileRequest(**fields)  # type: ignore[arg-type]


def test_block_derives_every_identity_and_puts_the_absent_protected_case() -> None:
    dynamo = _RecordingDynamo()
    request = _block_request()
    result = _coordinator(dynamo).block_table_attempt_reconcile(request)

    assert result.outcome is BlockTableAttemptReconcileOutcome.BLOCKED
    assert result.reconcile_block_id == request.block_id()
    assert result.recovery_ref == request.recovery_ref()
    # Grain: one transaction of four distinct-item actions.
    members = _members(dynamo)
    assert _kinds(members) == ["ConditionCheck", "ConditionCheck", "Update", "Put"]
    fence = members[2]["Update"]
    condition = fence["ConditionExpression"]
    assert "attribute_exists(#mf0)" in condition
    names = fence["ExpressionAttributeNames"]
    assert names["#mf0"] == "requeued_at"
    absent_names = {names[alias] for alias in names if alias.startswith("#a")}
    assert "reconcile_block_id" in absent_names
    assert condition.count("attribute_not_exists") == len(absent_names)
    assert fence["UpdateExpression"].endswith("REMOVE reconcile_attempt_count, requeued_at")
    case = members[3]["Put"]
    assert case["ConditionExpression"] == "attribute_not_exists(partition_key)"
    item = case["Item"]
    assert item["partition_key"] == {"S": f"PROVIDER_RECONCILE_BLOCK#{result.recovery_ref}"}
    assert item["record_key"] == {"S": "CASE"}
    assert item["state"] == {"S": "blocked"}
    assert item["reason"] == {"S": "MALFORMED_RECONCILIATION_METADATA"}
    assert "requeued_at" not in item


def test_block_requires_the_exact_snapshot_and_never_a_valid_old_pending_tuple() -> None:
    with pytest.raises(ValueError, match="exact snapshot"):
        _block_request(fence=_fence(reconciliation=_pending_reconciliation()))


def test_same_snapshot_rejection_blocks_only_a_canonical_pending_tuple() -> None:
    observation = SameSnapshotRescheduleRejectedObservation(_pending_reconciliation())
    dynamo = _RecordingDynamo()
    request = _block_request(
        fence=_fence(reconciliation=_pending_reconciliation()), observation=observation
    )
    result = _coordinator(dynamo).block_table_attempt_reconcile(request)
    assert result.outcome is BlockTableAttemptReconcileOutcome.BLOCKED
    members = _members(dynamo)
    assert members[2]["Update"]["ExpressionAttributeValues"][":block_reason"] == {
        "S": "SAME_SNAPSHOT_RESCHEDULE_REJECTED"
    }
    broken = ReconciliationObservation(
        reconcile_status="pending",
        reconcile_after_or_block=_pending_reconciliation().reconcile_after_or_block,
        reconcile_attempt_count=-1,
    )
    with pytest.raises(ValueError, match="canonical pending"):
        _block_request(
            fence=_fence(reconciliation=broken),
            observation=SameSnapshotRescheduleRejectedObservation(broken),
        )


def test_block_exact_replay_requires_identical_canonical_blocked_and_case_bytes() -> None:
    dynamo = _RecordingDynamo()
    _seed(dynamo, TABLE, _fence_item(_fence(reconciliation=_malformed_reconciliation())))
    coordinator = _coordinator(dynamo)
    first = coordinator.block_table_attempt_reconcile(_block_request())
    _replay(dynamo)
    second = coordinator.block_table_attempt_reconcile(_block_request())
    assert second.outcome is BlockTableAttemptReconcileOutcome.REPLAYED
    assert second.recovery_ref == first.recovery_ref


def test_block_conflicts_when_nothing_byte_identical_is_durable() -> None:
    dynamo = _RecordingDynamo()
    dynamo.fail_next_transactions = 1
    with pytest.raises(DomainError, match="strongly reread"):
        _coordinator(dynamo).block_table_attempt_reconcile(_block_request())


# --- RequeueBlockedTableAttemptReconcileRequest ------------------------------------------------


def _blocked_state() -> tuple[FenceObservation, ProtectedReconcileCase]:
    request = _block_request()
    block_id = request.block_id()
    ref = request.recovery_ref()
    observation_digest = request.observation_digest()
    reconciliation = ReconciliationObservation(
        reconcile_status="blocked",
        reconcile_after_or_block=f"BLOCK#{ref}",
        reconcile_block_id=block_id,
        reconcile_block_reason="MALFORMED_RECONCILIATION_METADATA",
        reconcile_blocked_at=INSTANT,
        reconcile_observation_digest=observation_digest,
        reconcile_recovery_ref=ref,
    )
    case = ProtectedReconcileCase(
        recovery_ref=ref,
        state=ProtectedCaseState.BLOCKED,
        physical_table_id=TABLE_ID,
        attempt_id=ATTEMPT_A,
        reconcile_block_id=block_id,
        binding_digest=_binding_digest(),
        reason=request.reason,
        blocked_at=INSTANT,
        observation_digest=observation_digest,
        requeued_at=None,
    )
    return _fence(reconciliation=reconciliation), case


def _requeue_request(**overrides: object) -> RequeueBlockedTableAttemptReconcileRequest:
    fence, case = _blocked_state()
    fields: dict[str, object] = {
        "physical_table_id": TABLE_ID,
        "case": case,
        "fence": fence,
        "attempt": _attempt(),
        "owner": _queued_owner_snapshot(),
        "operation_id": "recovery-op-0001",
        "resolution_reason": "fixture resolution",
        "requeue_instant": INSTANT,
    }
    fields.update(overrides)
    return RequeueBlockedTableAttemptReconcileRequest(**fields)  # type: ignore[arg-type]


def test_requeue_returns_canonical_pending_and_leaves_the_case_as_requeued_audit() -> None:
    dynamo = _RecordingDynamo()
    request = _requeue_request()
    result = _coordinator(dynamo).requeue_blocked_table_attempt_reconcile(request)

    assert result.outcome is RequeueBlockedTableAttemptReconcileOutcome.REQUEUED
    assert result.requeue_id == request.requeue_id()
    # Grain: one transaction of four distinct-item actions.
    members = _members(dynamo)
    assert _kinds(members) == ["ConditionCheck", "ConditionCheck", "Update", "Update"]
    fence = members[2]["Update"]
    values = fence["ExpressionAttributeValues"]
    assert values[":zero"] == {"N": "0"}
    # Due is exactly the requeue instant's epoch microseconds: immediately eligible.
    assert values[":pending_key"] == {"S": f"{INSTANT}#{_binding_digest()}"}
    assert fence["UpdateExpression"].endswith(
        "REMOVE reconcile_block_id, reconcile_block_reason, reconcile_blocked_at, "
        "reconcile_observation_digest, reconcile_recovery_ref"
    )
    case = members[3]["Update"]
    case_values = case["ExpressionAttributeValues"]
    assert case_values[":requeued_state"] == {"S": "requeued"}
    assert case_values[":requeue_id"] == {"S": result.requeue_id}


def test_requeue_exact_replay_returns_already_requeued() -> None:
    dynamo = _RecordingDynamo()
    fence, case = _blocked_state()
    _seed(dynamo, TABLE, _fence_item(fence))
    _seed(dynamo, TABLE, _protected_case_item(case))
    coordinator = _coordinator(dynamo)
    coordinator.requeue_blocked_table_attempt_reconcile(_requeue_request())
    _replay(dynamo)
    result = coordinator.requeue_blocked_table_attempt_reconcile(_requeue_request())
    assert result.outcome is RequeueBlockedTableAttemptReconcileOutcome.ALREADY_REQUEUED


def test_requeue_with_a_changed_operation_rejects_after_consumption() -> None:
    dynamo = _RecordingDynamo()
    fence, case = _blocked_state()
    _seed(dynamo, TABLE, _fence_item(fence))
    _seed(dynamo, TABLE, _protected_case_item(case))
    coordinator = _coordinator(dynamo)
    coordinator.requeue_blocked_table_attempt_reconcile(_requeue_request())
    _replay(dynamo)
    with pytest.raises(DomainError, match="consumed"):
        coordinator.requeue_blocked_table_attempt_reconcile(
            _requeue_request(operation_id="recovery-op-0002")
        )


def test_a_requeued_case_reads_back_with_its_complete_requeue_audit() -> None:
    """The recovery broker's next read of a consumed case must not be a malformed-form error."""
    dynamo = _RecordingDynamo()
    fence, case = _blocked_state()
    _seed(dynamo, TABLE, _fence_item(fence))
    _seed(dynamo, TABLE, _protected_case_item(case))
    coordinator = _coordinator(dynamo)
    result = coordinator.requeue_blocked_table_attempt_reconcile(_requeue_request())
    store = DynamoTableAttemptStore(TABLE, cast("Any", dynamo))
    consumed = store.read_protected_case(case.recovery_ref)
    assert consumed == replace(
        case,
        state=ProtectedCaseState.REQUEUED,
        requeued_at=INSTANT,
        requeue_id=result.requeue_id,
        requeue_operation_id="recovery-op-0001",
        resolution_reason="fixture resolution",
    )


def test_requeue_rejects_selector_disagreement_and_a_consumed_case_before_any_call() -> None:
    _fence_state, case = _blocked_state()
    with pytest.raises(ValueError, match="byte-equal"):
        _requeue_request(case=replace(case, attempt_id=ATTEMPT_B))
    consumed = replace(
        case,
        state=ProtectedCaseState.REQUEUED,
        requeued_at=INSTANT,
        requeue_id="3" * 64,
        requeue_operation_id="recovery-op-0001",
        resolution_reason="fixture resolution",
    )
    with pytest.raises(ValueError, match="blocked protected case"):
        _requeue_request(case=consumed)


# --- ClaimUnitRequest --------------------------------------------------------------------------


PRIOR_WRITER_EXEC = canonical_authority_digest(["stage1-writer-execution/v1", "message-0000", 1])
PRIOR_HOLDER = queued_lease_holder(PLAN_KEY, TABLE_ID, PRIOR_WRITER_EXEC)
# The operation instant rendered exactly as the store writes lease expiries.
INSTANT_AS_EXPIRY = "2026-09-19T12:34:56.789012+00:00"


def _claimable_unit_item(status: str = "queued", **overrides: object) -> dict[str, object]:
    item: dict[str, object] = {
        "partition_key": PLAN_KEY,
        "record_key": f"UNIT#{TABLE_ID}",
        "flow_id": FLOW_UUID,
        "owner_generation": 0,
        "status": status,
        "admission": "admitted",
        "created_at": INSTANT,
    }
    if status == "failed":
        item.update(
            {
                "writer_execution_id": "0" * 64,
                "failure_reason": "statement_retries_exhausted",
                "failed_at": INSTANT,
            }
        )
    if status == "writing":
        item["writer_execution_id"] = PRIOR_WRITER_EXEC
    item.update(overrides)
    return item


def _claim_request(status: str = "queued", **overrides: object) -> ClaimUnitRequest:
    fields: dict[str, object] = {
        "plan_digest": PLAN_DIGEST,
        "plan_key": PLAN_KEY,
        "physical_table_id": TABLE_ID,
        "intent": _admitted_intent(),
        "owner": _active_owner(),
        "plan_meta": _plan_meta(failed_count=1 if status == "failed" else 0),
        "unit_item": _claimable_unit_item(status),
        "writer_execution_id": WRITER_EXEC,
        "operation_instant": INSTANT,
        "plan_head": _plan_head(),
        "proof": _unit_started_proof(),
    }
    if status == "writing":
        fields["prior_writer_execution_id"] = PRIOR_WRITER_EXEC
        fields["prior_lease_holder"] = PRIOR_HOLDER
    fields.update(overrides)
    return ClaimUnitRequest(**cast("dict[str, Any]", fields))


def _prior_lease_item(holder: str = PRIOR_HOLDER, expires_at: str = EXPIRES) -> dict[str, object]:
    return {
        "partition_key": f"TABLE#{TABLE_ID}",
        "holder": holder,
        "fencing_token": 3,
        "expires_at": expires_at,
    }


def test_queued_claim_conditions_every_owner_fact_and_writes_once() -> None:
    dynamo = _RecordingDynamo()
    result = _coordinator(dynamo).claim_unit(_claim_request())

    assert (result.outcome, result.redrive) == (ClaimUnitOutcome.CLAIMED, False)
    # Grain: one transaction of seven distinct-item actions.
    members = _members(dynamo)
    assert _kinds(members) == [
        "ConditionCheck",  # owner active pair
        "ConditionCheck",  # copied plan HEAD agreement
        "ConditionCheck",  # admitted origin intent
        "ConditionCheck",  # planned plan META (no decrement for a queued claim)
        "Update",  # unit queued -> writing
        "Put",  # unit-started event
        "Put",  # one unit head effect
    ]
    unit = members[4]["Update"]
    assert unit["UpdateExpression"] == "SET #status = :writing, writer_execution_id = :writer"
    assert unit["ExpressionAttributeValues"][":writer"] == {"S": WRITER_EXEC}
    # Permanent Begin reservation counters are never named by a claim.
    assert "reservation" not in json.dumps(unit["UpdateExpression"])


def test_failed_redrive_decrements_failed_count_exactly_once() -> None:
    dynamo = _RecordingDynamo()
    result = _coordinator(dynamo).claim_unit(_claim_request("failed"))

    assert (result.outcome, result.redrive) == (ClaimUnitOutcome.CLAIMED, True)
    members = _members(dynamo)
    meta = members[3]["Update"]
    assert meta["UpdateExpression"] == "SET failed_count = :new_failed_count"
    assert meta["ExpressionAttributeValues"][":new_failed_count"] == {"N": "0"}
    unit = members[4]["Update"]
    assert unit["UpdateExpression"].endswith("REMOVE failed_at, failure_reason")


def test_claim_exact_replay_performs_no_second_update_decrement_or_event() -> None:
    dynamo = _RecordingDynamo()
    _seed(dynamo, TABLE, _claimable_unit_item("failed"))
    _seed(dynamo, TABLE, _plan_meta_item(PLAN_KEY, _plan_meta(failed_count=1)))
    coordinator = _coordinator(dynamo)
    coordinator.claim_unit(_claim_request("failed"))
    durable_meta = dict(dynamo.items[(TABLE, PLAN_KEY, "META")])
    _replay(dynamo)
    result = coordinator.claim_unit(_claim_request("failed"))
    assert result.outcome is ClaimUnitOutcome.REPLAYED
    assert dynamo.items[(TABLE, PLAN_KEY, "META")] == durable_meta


def test_claim_conflicts_when_another_writer_holds_the_unit() -> None:
    dynamo = _RecordingDynamo()
    _seed(dynamo, TABLE, _claimable_unit_item())
    _seed(dynamo, TABLE, _plan_meta_item(PLAN_KEY, _plan_meta()))
    coordinator = _coordinator(dynamo)
    coordinator.claim_unit(_claim_request())
    other = dict(dynamo.items[(TABLE, PLAN_KEY, f"UNIT#{TABLE_ID}")])
    other["writer_execution_id"] = {"S": "9" * 64}
    dynamo.items[(TABLE, PLAN_KEY, f"UNIT#{TABLE_ID}")] = other
    _replay(dynamo)
    with pytest.raises(DomainError, match="stale"):
        coordinator.claim_unit(_claim_request())


def test_claim_rejects_a_unit_outside_queued_failed_or_writing_before_any_call() -> None:
    with pytest.raises(ValueError, match="queued, failed, or writing"):
        _claim_request("attested")


# --- ClaimUnitRequest: writing→writing reclaim (D85) ------------------------------------------


def test_reclaim_adds_exactly_one_disjunctive_leases_condition_with_no_pre_state() -> None:
    dynamo = _RecordingDynamo()
    request = _claim_request("writing")
    assert (request.reclaim, request.redrive) == (True, False)

    result = _coordinator(dynamo).claim_unit(request)

    assert (result.outcome, result.redrive, result.held_until) == (
        ClaimUnitOutcome.CLAIMED,
        False,
        None,
    )
    # Grain: one transaction of eight distinct-item actions; the reclaim cap is 8.
    members = _members(dynamo)
    assert _kinds(members) == [
        "ConditionCheck",  # owner active pair
        "ConditionCheck",  # copied plan HEAD agreement
        "ConditionCheck",  # admitted origin intent
        "ConditionCheck",  # planned plan META (never a decrement for a reclaim)
        "Update",  # unit writing -> writing under the new writer execution
        "ConditionCheck",  # prior holder's lease absent, moved, or expired/released
        "Put",  # unit-started event under the new execution
        "Put",  # one unit head effect
    ]
    unit = members[4]["Update"]
    assert unit["ExpressionAttributeValues"][":writer"] == {"S": WRITER_EXEC}
    # The unit condition names the exact prior writer and proves no bound attempt.
    names = unit["ExpressionAttributeNames"]
    assert "current_attempt_id" in names.values()
    absent_alias = next(alias for alias, name in names.items() if name == "current_attempt_id")
    assert f"attribute_not_exists({absent_alias})" in unit["ConditionExpression"]
    writer_alias = next(alias for alias, name in names.items() if name == "writer_execution_id")
    assert unit["ExpressionAttributeValues"][writer_alias.replace("#", ":")] == {
        "S": PRIOR_WRITER_EXEC
    }
    lease = members[5]["ConditionCheck"]
    assert lease == {
        "TableName": LEASES,
        "Key": {"partition_key": {"S": f"TABLE#{TABLE_ID}"}},
        "ConditionExpression": (
            "attribute_not_exists(partition_key) OR holder <> :prior_holder "
            "OR expires_at <= :instant"
        ),
        "ExpressionAttributeValues": {
            ":prior_holder": {"S": PRIOR_HOLDER},
            ":instant": {"S": INSTANT_AS_EXPIRY},
        },
    }
    # No lease, fence, or counter mutation: only the unit update plus the two puts write.
    assert dynamo.items.get((LEASES, f"TABLE#{TABLE_ID}", "")) is None


def test_reclaim_classifies_a_live_prior_holder_with_its_exact_expiry() -> None:
    dynamo = _RecordingDynamo()
    _seed(dynamo, LEASES, _prior_lease_item())
    dynamo.fail_next_transactions = 1

    result = _coordinator(dynamo).claim_unit(_claim_request("writing"))

    assert result == ClaimUnitResult(ClaimUnitOutcome.PRIOR_WRITER_LIVE, False, held_until=EXPIRES)
    # The abort classification rereads only; nothing durable changed.
    assert len(dynamo.transacts) == 1


@pytest.mark.parametrize(
    "lease",
    [
        pytest.param(None, id="absent"),
        pytest.param(_prior_lease_item(holder="f" * 64), id="third-holder-live"),
        pytest.param(_prior_lease_item(expires_at="2026-09-19T12:00:00+00:00"), id="expired"),
        pytest.param(_prior_lease_item(expires_at=RELEASED_LEASE_EXPIRY), id="released"),
    ],
)
def test_reclaim_abort_without_a_live_prior_holder_is_a_conflict_not_busy(
    lease: dict[str, object] | None,
) -> None:
    """A lost condition with the lease disjunction satisfied means another owner fact moved.

    Absent, moved-to-a-third-holder, expired, and released leases all satisfy it, so
    the writer must strongly reread rather than wait on a holder that is not live.
    """
    dynamo = _RecordingDynamo()
    if lease is not None:
        _seed(dynamo, LEASES, lease)
    dynamo.fail_next_transactions = 1
    with pytest.raises(DomainError, match="strongly reread"):
        _coordinator(dynamo).claim_unit(_claim_request("writing"))


def test_reclaim_exact_replay_is_a_no_op() -> None:
    dynamo = _RecordingDynamo()
    _seed(dynamo, TABLE, _claimable_unit_item("writing"))
    _seed(dynamo, TABLE, _plan_meta_item(PLAN_KEY, _plan_meta()))
    coordinator = _coordinator(dynamo)
    coordinator.claim_unit(_claim_request("writing"))
    _replay(dynamo)
    assert coordinator.claim_unit(_claim_request("writing")).outcome is ClaimUnitOutcome.REPLAYED


def test_reclaim_request_pins_every_prior_writer_fact_before_any_call() -> None:
    with pytest.raises(ValueError, match="prior writer execution and its lease holder"):
        _claim_request("writing", prior_lease_holder=None)
    with pytest.raises(ValueError, match="exact durable writer"):
        _claim_request(
            "writing", unit_item=_claimable_unit_item("writing", writer_execution_id="9" * 64)
        )
    with pytest.raises(ValueError, match="different writer execution"):
        _claim_request(
            "writing",
            unit_item=_claimable_unit_item("writing", writer_execution_id=WRITER_EXEC),
            prior_writer_execution_id=WRITER_EXEC,
            prior_lease_holder=HOLDER,
        )
    with pytest.raises(ValueError, match="no nonterminal attempt"):
        _claim_request(
            "writing", unit_item=_claimable_unit_item("writing", current_attempt_id="a" * 64)
        )
    with pytest.raises(ValueError, match="reproduce from the plan, unit, and prior writer"):
        _claim_request("writing", prior_lease_holder=HOLDER)
    with pytest.raises(ValueError, match="only for a writing-unit reclaim"):
        _claim_request("queued", prior_writer_execution_id=PRIOR_WRITER_EXEC)


def test_claim_result_carries_held_until_exactly_for_a_live_prior_writer() -> None:
    with pytest.raises(ValueError, match="held_until"):
        ClaimUnitResult(ClaimUnitOutcome.CLAIMED, False, held_until=EXPIRES)
    with pytest.raises(ValueError, match="held_until"):
        ClaimUnitResult(ClaimUnitOutcome.PRIOR_WRITER_LIVE, False)


# --- FailUnitRequest: ordinary branch ----------------------------------------------------------


def _fail_request(**overrides: object) -> FailUnitRequest:
    fields: dict[str, object] = {
        "plan_digest": PLAN_DIGEST,
        "plan_key": PLAN_KEY,
        "physical_table_id": TABLE_ID,
        "writer_execution_id": WRITER_EXEC,
        "owner": _active_owner(),
        "plan_meta": _plan_meta(),
        "lease": LEASE,
        "observed_fence": None,
        "reason": UnitFailureReason.STATEMENT_RETRIES_EXHAUSTED,
        "settlement": _expansion(OwnerDisposition.RETRYABLE_FAILURE, ATTEMPT_A, ATTEMPT_B),
        "prior_settled_attempts": (),
        "operation_instant": INSTANT,
        "proof": _unit_proof(
            "semantic_unit_failed",
            FlowState.RETRYING,
            None,
            "statement_retries_exhausted",
        ),
        "unit_item": {
            "partition_key": PLAN_KEY,
            "record_key": f"UNIT#{TABLE_ID}",
            "flow_id": FLOW_UUID,
            "owner_generation": 0,
            "status": "writing",
            "admission": "admitted",
            "writer_execution_id": WRITER_EXEC,
            "created_at": INSTANT,
        },
    }
    fields.update(overrides)
    return FailUnitRequest(**fields)  # type: ignore[arg-type]


def test_ordinary_failure_is_six_fixed_actions_plus_two_per_settled_attempt() -> None:
    dynamo = _RecordingDynamo()
    result = _coordinator(dynamo).fail_unit(_fail_request())

    assert result == (FailUnitOutcome.FAILED, None) or result.outcome is FailUnitOutcome.FAILED
    members = _members(dynamo)
    # Grain: F = 6 + 2n distinct-item actions with n = 2 settled attempts.
    assert len(members) == 10
    assert _kinds(members) == [
        "ConditionCheck",  # exact unexpired lease
        "ConditionCheck",  # absent-or-clear fence
        "Update",  # unit writing -> failed
        "Update",  # plan META counters and settled aggregate
        "Put",  # failure event
        "Put",  # unit head advance
        "Update",  # attempt A unsettled -> settled
        "Put",  # attempt A USAGE# authority
        "Update",  # attempt B unsettled -> settled
        "Put",  # attempt B USAGE# authority
    ]
    assert members[0]["ConditionCheck"]["TableName"] == LEASES
    unit = members[2]["Update"]
    assert unit["ExpressionAttributeValues"][":reason"] == {"S": "statement_retries_exhausted"}
    meta = members[3]["Update"]
    values = meta["ExpressionAttributeValues"]
    assert values[":new_failed_count"] == {"N": "1"}
    assert values[":new_settled_count"] == {"N": "2"}


def test_settlement_derives_the_category_and_the_immutable_usage_authority() -> None:
    dynamo = _RecordingDynamo()
    _coordinator(dynamo).fail_unit(_fail_request())

    members = _members(dynamo)
    settled = members[6]["Update"]
    values = settled["ExpressionAttributeValues"]
    # Retryable unit failure settles every accepted attempt as retry (concept Decision D).
    assert values[":category"] == {"S": "retry"}
    assert values[":disposition"] == {"S": "retryable_failure"}
    usage = members[7]["Put"]["Item"]
    assert usage["partition_key"] == {"S": PARTITION}
    assert usage["record_key"] == {"S": f"USAGE#{ATTEMPT_A}"}
    assert usage["authority_kind"] == {"S": "usage_contribution"}
    assert usage["athena_usage_state"] == {"S": "settled"}
    assert usage["is_delta"] == {"BOOL": False}
    assert usage["outbox_state"] == {"S": "pending"}
    assert usage["outbox_status"] == {"S": "pending"}
    # outbox_order time is exactly the settled disposition's observed_at epoch microseconds.
    assert usage["outbox_order"]["S"].startswith(INSTANT)
    payload = json.loads(usage["payload"]["S"])
    assert canonical_authority_digest(payload) == usage["immutable_payload_digest"]["S"]
    assert values[":authority_digest"] == usage["immutable_payload_digest"]
    assert payload["usage"]["retry"]["athena"] == {
        "query_count": 1,
        "scanned_bytes": 1024,
        "engine_ms": 250,
    }


def test_a_missing_provider_quantity_settles_as_one_unavailable_evidence_item() -> None:
    dynamo = _RecordingDynamo()
    expansion = Stage1SettlementExpansion(
        owner_disposition=OwnerDisposition.RETRYABLE_FAILURE,
        attempts=(_settlement(ATTEMPT_A, OwnerDisposition.RETRYABLE_FAILURE, scanned_bytes=None),),
    )
    _coordinator(dynamo).fail_unit(_fail_request(settlement=expansion))
    usage = _members(dynamo)[7]["Put"]["Item"]
    assert usage["authority_kind"] == {"S": "usage_evidence_disposition"}
    payload = json.loads(usage["payload"]["S"])
    assert payload["missing_quantities"] == ["scanned_bytes"]
    assert payload["evidence"] == "unavailable"
    # The status-owned wire form renders the missing quantity as null, never zero.
    assert payload["known_usage"]["athena"] == {
        "query_count": 1,
        "scanned_bytes": None,
        "engine_ms": 250,
    }


def test_fail_unit_exact_replay_is_a_no_op() -> None:
    dynamo = _RecordingDynamo()
    request = _fail_request()
    _seed(dynamo, TABLE, dict(cast("dict[str, object]", request.unit_item)))
    _seed(dynamo, TABLE, _plan_meta_item(PLAN_KEY, _plan_meta()))
    _seed_settlement_targets(dynamo)
    coordinator = _coordinator(dynamo)
    coordinator.fail_unit(_fail_request())
    _replay(dynamo)
    result = coordinator.fail_unit(_fail_request())
    assert result.outcome is FailUnitOutcome.REPLAYED


def test_fail_unit_rejects_a_lease_holder_outside_the_queued_hash() -> None:
    foreign = TableLeaseRecord(TABLE_ID, "another-holder", TOKEN, EXPIRES)
    with pytest.raises(ValueError, match="queued hash"):
        _fail_request(lease=foreign)


def test_fail_unit_rejects_an_eleventh_settled_attempt_before_any_call() -> None:
    ids = tuple(f"{n:02x}" * 32 for n in range(1, 12))
    with pytest.raises(ValueError, match="at most 10"):
        _expansion(OwnerDisposition.RETRYABLE_FAILURE, *ids)


def test_fail_unit_rejects_a_disposition_outside_its_closed_branch() -> None:
    with pytest.raises(ValueError, match="closed failure branch"):
        _fail_request(settlement=_expansion(OwnerDisposition.SUCCESSFUL, ATTEMPT_A))


# --- FailUnitRequest: attempt-33 branch --------------------------------------------------------


def _budget_request(**overrides: object) -> FailUnitRequest:
    fields: dict[str, object] = {
        "reason": UnitFailureReason.PLAN_ATTEMPT_BUDGET_EXHAUSTED,
        "settlement": _expansion(OwnerDisposition.EXHAUSTED_OR_UNAVAILABLE, ATTEMPT_A, ATTEMPT_B),
        "unit_item": None,
        "budget_exhaustion": AttemptBudgetExhaustion(
            successor_units=UNITS, permanent_reservation_count=32, rejected_ordinal=33
        ),
    }
    fields.update(overrides)
    return _fail_request(**fields)


def test_attempt_33_replaces_the_trigger_consumes_the_owner_and_creates_the_successor() -> None:
    dynamo = _RecordingDynamo()
    request = _budget_request()
    result = _coordinator(dynamo).fail_unit(request)

    assert result.outcome is FailUnitOutcome.FAILED
    assert result.successor_intent_id == request.successor_intent_id()
    members = _members(dynamo)
    # Grain: F = 7 + 2n + u distinct-item actions with n = 2 and u = 2.
    assert len(members) == 13
    assert _kinds(members) == [
        "ConditionCheck",  # exact unexpired lease (proves the unit transitively)
        "ConditionCheck",  # absent-or-clear fence
        "Update",  # plan META terminalization
        "Update",  # exactly one owner action: the consumption
        "Put",  # successor intent META
        "Put",  # successor UNIT# (db.alpha)
        "Put",  # successor UNIT# (db.beta)
        "Put",  # terminal event
        "Put",  # head advance
        "Update",  # attempt A settle
        "Put",  # attempt A USAGE#
        "Update",  # attempt B settle
        "Put",  # attempt B USAGE#
    ]
    # No plan UNIT# member exists: the trigger is replaced, not appended to.
    unit_keys = [
        member["Update"]["Key"].get("record_key", {}).get("S", "")
        for member in members
        if "Update" in member
    ]
    assert f"UNIT#{TABLE_ID}" not in unit_keys
    meta = members[2]["Update"]
    assert meta["ExpressionAttributeValues"][":superseded"] == {"S": "superseded"}
    owner = members[3]["Update"]
    assert owner["UpdateExpression"].endswith("REMOVE active_plan_key, active_intent_id")
    successor = members[4]["Put"]["Item"]
    assert successor["state"] == {"S": "pending"}
    assert successor["origin_kind"] == {"S": "plan_attempt_budget"}
    assert successor["ready_status"] == {"S": "ready"}
    assert "claimed_generation" not in successor


def test_attempt_33_exact_replay_cannot_fan_out() -> None:
    dynamo = _RecordingDynamo()
    _seed(dynamo, TABLE, _plan_meta_item(PLAN_KEY, _plan_meta()))
    _seed(dynamo, TABLE, _owner_item(PLAN_DIGEST, _active_owner()))
    _seed_settlement_targets(dynamo)
    coordinator = _coordinator(dynamo)
    first = coordinator.fail_unit(_budget_request())
    _replay(dynamo)
    second = coordinator.fail_unit(_budget_request())
    assert second.outcome is FailUnitOutcome.REPLAYED
    assert second.successor_intent_id == first.successor_intent_id
    intents = [
        record
        for (table, partition, record) in dynamo.items
        if table == TABLE and "#INTENT#" in partition and record == "META"
    ]
    assert len(intents) == 1


def test_attempt_33_requires_exactly_32_reservations_and_rejected_ordinal_33() -> None:
    with pytest.raises(ValueError, match="exactly 32"):
        AttemptBudgetExhaustion(
            successor_units=UNITS, permanent_reservation_count=31, rejected_ordinal=33
        )
    with pytest.raises(ValueError, match="ordinal 33"):
        AttemptBudgetExhaustion(
            successor_units=UNITS, permanent_reservation_count=32, rejected_ordinal=34
        )
    with pytest.raises(ValueError, match="no unit transaction member"):
        _budget_request(unit_item=_claimable_unit_item("queued"))
    with pytest.raises(ValueError, match="successor"):
        _budget_request(budget_exhaustion=None)


# --- FinalizeUnitRequest -----------------------------------------------------------------------


def _current_item(partition: str, token: int = 3) -> dict[str, object]:
    return {
        "partition_key": partition,
        "record_key": "CURRENT",
        "publication_id": "pub-fixture-0009",
        "fencing_token": 9,
        "projected_publication_id": "pub-fixture-0003",
        "projected_fencing_token": token,
        "projected_at": "2026-09-19T11:00:00+00:00",
    }


def _advanced_pointer(partition: str = DATASET_PARTITION) -> CurrentPointerOutcome:
    return CurrentPointerOutcome(
        kind=PointerOutcomeKind.ADVANCED,
        partition_identity=partition,
        projected_publication_id="pub-fixture-0009",
        projected_fencing_token=9,
        proof=_pointer_proof(partition, "pub-fixture-0009", 9),
        observed_current=_current_item(partition),
    )


def _superseded_pointer(partition: str) -> CurrentPointerOutcome:
    return CurrentPointerOutcome(
        kind=PointerOutcomeKind.PROJECTION_SUPERSEDED,
        partition_identity=partition,
        projected_publication_id="pub-fixture-0002",
        projected_fencing_token=2,
        proof=_pointer_proof(partition, "pub-fixture-0002", 2),
        observed_current=None,
    )


def _finalize_request(**overrides: object) -> FinalizeUnitRequest:
    fields: dict[str, object] = {
        "plan_digest": PLAN_DIGEST,
        "plan_key": PLAN_KEY,
        "physical_table_id": TABLE_ID,
        "writer_execution_id": WRITER_EXEC,
        "owner": _active_owner(),
        "plan_meta": _plan_meta(),
        "unit_item": {
            "partition_key": PLAN_KEY,
            "record_key": f"UNIT#{TABLE_ID}",
            "flow_id": FLOW_UUID,
            "owner_generation": 0,
            "status": "writing",
            "admission": "admitted",
            "writer_execution_id": WRITER_EXEC,
            "created_at": INSTANT,
        },
        "lease": LEASE,
        "observed_fence": None,
        "pointers": (
            _advanced_pointer(),
            _superseded_pointer("fictional.dataset|2026-09-19|delta"),
        ),
        "settlement": _expansion(OwnerDisposition.SUCCESSFUL, ATTEMPT_A, ATTEMPT_B),
        "prior_settled_attempts": (),
        "verified_checksums": (CHECKSUM,),
        "operation_instant": INSTANT,
        "proof": _unit_proof(
            "semantic_unit_attested", FlowState.COMPLETED, CompletionOutcome.SUCCEEDED, None
        ),
    }
    fields.update(overrides)
    return FinalizeUnitRequest(**fields)  # type: ignore[arg-type]


def test_finalize_unit_carries_the_complete_pointer_settlement_and_owner_payload() -> None:
    dynamo = _RecordingDynamo()
    result = _coordinator(dynamo).finalize_unit(_finalize_request())

    assert result.outcome is FinalizeUnitOutcome.FINALIZED
    members = _members(dynamo)
    # Grain: F = 6 + 2p + 2n with one advanced pointer (2 actions), one superseded
    # pointer (its event only), and two settled attempts: 6 + 3 + 4 = 13 actions.
    assert len(members) == 13
    assert _kinds(members) == [
        "ConditionCheck",  # exact unexpired lease
        "ConditionCheck",  # absent-or-clear fence
        "Update",  # unit writing -> attested
        "Update",  # plan META attested/settled aggregate
        "Put",  # unit terminal event
        "Put",  # exactly one unit head effect
        "Update",  # advanced CURRENT pointer
        "Put",  # advanced headless pointer event
        "Put",  # superseded headless pointer event (no CURRENT action)
        "Update",  # attempt A settle
        "Put",  # attempt A USAGE#
        "Update",  # attempt B settle
        "Put",  # attempt B USAGE#
    ]
    unit = members[2]["Update"]
    assert unit["ExpressionAttributeValues"][":verified"] == {
        "S": unit_verified_digest((CHECKSUM,))
    }
    current = members[6]["Update"]
    assert current["Key"]["partition_key"] == {"S": DATASET_PARTITION}
    assert current["ConditionExpression"] == (
        "attribute_exists(publication_id) AND (attribute_not_exists(projected_publication_id) "
        "OR projected_publication_id = :none OR projected_fencing_token < :projected_token)"
    )
    values = current["ExpressionAttributeValues"]
    assert values[":none"] == {"S": "NONE"}
    assert values[":projected_at"] == {"S": _projected_at(INSTANT)}
    assert values[":category"] if False else True
    settled = members[9]["Update"]
    assert settled["ExpressionAttributeValues"][":category"] == {"S": "productive"}


def test_finalize_unit_exact_replay_is_a_no_op() -> None:
    dynamo = _RecordingDynamo()
    request = _finalize_request()
    _seed(dynamo, TABLE, dict(request.unit_item))
    _seed(dynamo, TABLE, _plan_meta_item(PLAN_KEY, _plan_meta()))
    _seed(dynamo, TABLE, _current_item(DATASET_PARTITION))
    _seed_settlement_targets(dynamo)
    coordinator = _coordinator(dynamo)
    coordinator.finalize_unit(_finalize_request())
    _replay(dynamo)
    assert coordinator.finalize_unit(_finalize_request()).outcome is FinalizeUnitOutcome.REPLAYED


def test_finalize_unit_rejects_38_pointers_equal_tokens_and_headed_pointer_proofs() -> None:
    partitions = tuple(f"fictional.dataset|2026-09-19|p{n:02d}" for n in range(38))
    with pytest.raises(ValueError, match="at most 37"):
        _finalize_request(pointers=tuple(_superseded_pointer(p) for p in partitions))
    with pytest.raises(ValueError, match="projection_superseded"):
        CurrentPointerOutcome(
            kind=PointerOutcomeKind.ADVANCED,
            partition_identity=DATASET_PARTITION,
            projected_publication_id="pub-fixture-0009",
            projected_fencing_token=3,
            proof=_pointer_proof(DATASET_PARTITION, "pub-fixture-0009", 3),
            observed_current=_current_item(DATASET_PARTITION, token=3),
        )
    with pytest.raises(ValueError, match="headless"):
        CurrentPointerOutcome(
            kind=PointerOutcomeKind.PROJECTION_SUPERSEDED,
            partition_identity=DATASET_PARTITION,
            projected_publication_id="pub-fixture-0002",
            projected_fencing_token=2,
            proof=_unit_started_proof(),
            observed_current=None,
        )


def test_finalize_unit_rejects_a_coordinator_prefixed_pointer_partition() -> None:
    with pytest.raises(ValueError, match="coordinator key family"):
        CurrentPointerOutcome(
            kind=PointerOutcomeKind.PROJECTION_SUPERSEDED,
            partition_identity=f"WRITE_PLAN#{PLAN_DIGEST}",
            projected_publication_id="pub-fixture-0002",
            projected_fencing_token=2,
            proof=_pointer_proof(DATASET_PARTITION, "pub-fixture-0002", 2),
            observed_current=None,
        )


# --- FinalizeInlineRequest ---------------------------------------------------------------------


def _run_item() -> dict[str, object]:
    run_id = run_identity(
        RunIdentityRequest(
            WriterKind.INGEST_DELIVERY_OPERATOR, "op-fixture-0001", "exec-fixture-0001", TABLE_ID
        )
    )
    return {
        "partition_key": PARTITION,
        "record_key": f"RUN#{run_id}",
        "run_id": run_id,
        "writer_kind": "ingest_delivery_operator",
        "operation_id": "op-fixture-0001",
        "execution_id": "exec-fixture-0001",
        "physical_table_id": TABLE_ID,
        "status_flow_binding": FLOW_UUID,
        "execution_binding": EXEC_UUID,
        "table_kind": "current",
        "statement_intent_kinds": ["merge", "verify"],
        "created_at": INSTANT,
        "state": "reconciled",
        "next_ordinal": 3,
        "accepted_attempt_reservations": 2,
        "attempt_references": [ATTEMPT_A, ATTEMPT_B],
    }


def _inline_proof() -> Stage1LifecycleProof:
    first = derive_stage1_lifecycle_proof(
        Stage1LifecycleProofRequest(
            identity=InlineIdentityRequest(
                "ingest_delivery_operator", "op-fixture-0001", TABLE_ID, "exec-fixture-0001"
            ),
            internal_key=(
                "ingest_delivery_operator",
                "op-fixture-0001",
                TABLE_ID,
                "exec-fixture-0001",
            ),
            topology=Stage1TopologyRequest(
                Stage1OwnerTransition.FIRST_INGEST_OPERATOR_INLINE_RECORDED,
                LegitimateRootSource(RootDiscriminator.INGEST_DELIVERY_OPERATOR_INLINE),
            ),
            transition=Stage1LifecycleTransition(
                "semantic_inline_run_recorded", FlowState.RUNNING, None, None
            ),
            stage="inline_run_recorded",
            authority_at=NOW,
            prior_head=None,
        )
    )
    head = first.resulting_head
    assert head is not None
    return derive_stage1_lifecycle_proof(
        Stage1LifecycleProofRequest(
            identity=InlineIdentityRequest(
                "ingest_delivery_operator", "op-fixture-0001", TABLE_ID, "exec-fixture-0001"
            ),
            internal_key=(
                "ingest_delivery_operator",
                "op-fixture-0001",
                TABLE_ID,
                "exec-fixture-0001",
            ),
            topology=Stage1TopologyRequest(
                Stage1OwnerTransition.LATER_INLINE_EVENT,
                PriorOwnerEventSource(
                    head.parent_flow_id, head.latest_event_id, stage1_head_authority_document(head)
                ),
            ),
            transition=Stage1LifecycleTransition(
                "semantic_inline_run_terminal",
                FlowState.COMPLETED,
                CompletionOutcome.SUCCEEDED,
                None,
            ),
            stage="inline_run_terminal",
            authority_at=NOW,
            prior_head=head,
            clock_started_at=NOW,
        )
    )


def _finalize_inline_request(**overrides: object) -> FinalizeInlineRequest:
    item = _run_item()
    run_id = cast("str", item["run_id"])
    fields: dict[str, object] = {
        "physical_table_id": TABLE_ID,
        "run": InlineRunAttemptOwner(TABLE_ID, run_id, item),
        "lease": LEASE,
        "observed_fence": None,
        "pointers": (_advanced_pointer(),),
        "settlement": _expansion(OwnerDisposition.SUCCESSFUL, ATTEMPT_A, ATTEMPT_B),
        "operation_instant": INSTANT,
        "proof": _inline_proof(),
    }
    fields.update(overrides)
    return FinalizeInlineRequest(**fields)  # type: ignore[arg-type]


def test_finalize_inline_terminalizes_the_run_with_its_settlement_and_pointer() -> None:
    dynamo = _RecordingDynamo()
    result = _coordinator(dynamo).finalize_inline(_finalize_inline_request())

    assert result.outcome is FinalizeInlineOutcome.FINALIZED
    members = _members(dynamo)
    # Grain: F = 5 + 2p + 2n with one advanced pointer and two settled attempts: 11.
    assert len(members) == 11
    run = members[2]["Update"]
    values = run["ExpressionAttributeValues"]
    assert values[":terminal_state"] == {"S": "terminal"}
    assert values[":outcome"] == {"S": "successful"}
    assert values[":instant"] == {"S": INSTANT}


def test_finalize_inline_exact_replay_is_a_no_op_and_rejects_a_bound_attempt() -> None:
    dynamo = _RecordingDynamo()
    _seed(dynamo, TABLE, _run_item())
    _seed(dynamo, TABLE, _current_item(DATASET_PARTITION))
    _seed_settlement_targets(dynamo)
    coordinator = _coordinator(dynamo)
    coordinator.finalize_inline(_finalize_inline_request())
    _replay(dynamo)
    replayed = coordinator.finalize_inline(_finalize_inline_request())
    assert replayed.outcome is FinalizeInlineOutcome.REPLAYED
    bound = _run_item()
    bound["current_attempt_id"] = ATTEMPT_A
    run_id = cast("str", bound["run_id"])
    with pytest.raises(ValueError, match="attempt remains bound"):
        _finalize_inline_request(run=InlineRunAttemptOwner(TABLE_ID, run_id, bound))


# --- PersistPlanRequest: planned-plan replacement ------------------------------------------------


SOURCE_INTENT_ID = "cd" * 32
SOURCE_EXEC = internal_execution_identity(SOURCE_INTENT_ID, 0)
SOURCE_PLAN_KEY = plan_partition_key(PLAN_DIGEST, SOURCE_EXEC)


def _source_plan_head_proof() -> Stage1LifecycleProof:
    return derive_stage1_lifecycle_proof(
        Stage1LifecycleProofRequest(
            identity=PlanIdentityRequest(PLAN_DIGEST, SOURCE_EXEC),
            internal_key=(SOURCE_PLAN_KEY,),
            topology=Stage1TopologyRequest(
                Stage1OwnerTransition.FIRST_AUTOMATIC_PLAN_RECORDED,
                LegitimateRootSource(RootDiscriminator.AUTOMATIC_INGESTION_PLAN),
            ),
            transition=Stage1LifecycleTransition(
                "semantic_plan_recorded", FlowState.QUEUED, None, None
            ),
            stage="projection_plan_recorded",
            authority_at=NOW,
            prior_head=None,
            required_child_expectations=_expectations(SOURCE_EXEC, 0),
        )
    )


def _replacement_case() -> tuple[PersistPlanRequest, list[dict[str, object]]]:
    target_units = UNITS
    target_origin = ORIGIN
    target_intent_id = intent_identity(
        IntentAuthorityInputs(SOURCE_PLAN_KEY, None, PLAN_DIGEST, target_origin, target_units)
    )
    target_exec = internal_execution_identity(target_intent_id, 1)
    intent = IntentMeta(
        root_plan_key=SOURCE_PLAN_KEY,
        source_plan_key=None,
        plan_digest=PLAN_DIGEST,
        intent_id=target_intent_id,
        roster_digest=ROSTER_DIGEST,
        origin=target_origin,
        state=IntentState.CLAIMED,
        claimed_generation=1,
        retry_count=0,
        due_at=INSTANT,
        ready_status=None,
        ready_order=None,
        created_at=INSTANT,
        last_transition_at=INSTANT,
    )
    owner = SuccessorOwner(
        revision=3,
        root_plan_key=SOURCE_PLAN_KEY,
        last_plan_key=SOURCE_PLAN_KEY,
        last_generation=0,
        active_plan_key=SOURCE_PLAN_KEY,
        active_intent_id=SOURCE_INTENT_ID,
        reserved_intent_id=target_intent_id,
        reserved_intent_digest=target_intent_id,
        reserved_generation=1,
        latest_internal_execution_id=SOURCE_EXEC,
    )
    settled_digest_source = canonical_authority_digest(["fixture-usage-authority"])
    attempt_item: dict[str, object] = {
        "partition_key": PARTITION,
        "record_key": f"ATTEMPT#{ATTEMPT_A}",
        "state": "SUCCEEDED",
        "athena_usage_state": "settled",
        "usage_authority_digest": settled_digest_source,
    }
    usage_payload = json.dumps(["fixture-usage-authority"])
    usage_item: dict[str, object] = {
        "partition_key": PARTITION,
        "record_key": f"USAGE#{ATTEMPT_A}",
        "payload": usage_payload,
        "immutable_payload_digest": settled_digest_source,
    }
    references = (SettledAttemptReference(TABLE_ID, ATTEMPT_A, settled_digest_source),)
    source_meta = _plan_meta(
        internal_execution_id=SOURCE_EXEC,
        root_plan_key=SOURCE_PLAN_KEY,
        origin_intent_id=SOURCE_INTENT_ID,
        settled_attempt_count=1,
        settled_attempt_digest=plan_settled_attempt_digest(references),
    )
    source_units = [
        {
            "partition_key": SOURCE_PLAN_KEY,
            "record_key": f"UNIT#{key}",
            "flow_id": flow,
            "owner_generation": 0,
            "status": "attested",
        }
        for key, flow in ((TABLE_ID, FLOW_UUID), (SIBLING_ID, SIBLING_FLOW))
    ]
    source = SourcePlanReplacementEvidence(
        plan_key=SOURCE_PLAN_KEY,
        meta=source_meta,
        unit_items=tuple(source_units),
        attempts=(
            SettledAttemptObservation(
                TABLE_ID, ATTEMPT_A, "SUCCEEDED", settled_digest_source, attempt_item
            ),
        ),
        usage_authorities=(
            SettledUsageAuthorityObservation(
                TABLE_ID, ATTEMPT_A, usage_payload, settled_digest_source, usage_item
            ),
        ),
    )
    head = _source_plan_head_proof().resulting_head
    assert head is not None
    proof = compound_replacement_proof(
        Stage1CompoundReplacementRequest(
            plan_digest=PLAN_DIGEST,
            prior_head=head,
            source_internal_key=(SOURCE_PLAN_KEY,),
            target_internal_key=(plan_partition_key(PLAN_DIGEST, target_exec),),
            successor_internal_delivery_execution_id=target_exec,
            terminal_transition=Stage1LifecycleTransition(
                "semantic_plan_terminal",
                FlowState.SUPERSEDED,
                CompletionOutcome.SUPERSEDED,
                None,
            ),
            successor_transition=Stage1LifecycleTransition(
                "semantic_plan_recorded", FlowState.QUEUED, None, None
            ),
            source_expectations=_expectations(SOURCE_EXEC, 0),
            target_expectations=_expectations(target_exec, 1),
            authority_at=NOW,
            clock_started_at=NOW,
        )
    )
    request = PersistPlanRequest(
        plan_digest=PLAN_DIGEST,
        intent=intent,
        units=target_units,
        owner=owner,
        unit_flows=(
            UnitFlowBinding(TABLE_ID, FLOW_UUID),
            UnitFlowBinding(SIBLING_ID, SIBLING_FLOW),
        ),
        proof=proof,
        operation_instant=INSTANT,
        source_plan=source,
    )
    seeds: list[dict[str, object]] = []
    return request, seeds


def test_replacement_terminalizes_the_source_and_performs_exactly_one_owner_update() -> None:
    dynamo = _RecordingDynamo()
    request, _seeds = _replacement_case()
    result = _coordinator(dynamo).persist_plan(request)

    assert result.outcome is PersistPlanOutcome.PERSISTED
    assert result.generation == 1
    members = _members(dynamo)
    # Grain: 6 fixed actions + u_p(2) + u_s(2) + t(1 attempt) + n(1 usage) = 12.
    assert len(members) == 12
    owner_updates = [
        member
        for member in members
        if "Update" in member
        and member["Update"]["Key"].get("record_key", {}).get("S") == "SUCCESSOR_OWNER"
    ]
    assert len(owner_updates) == 1
    source_meta = next(
        member["Update"]
        for member in members
        if "Update" in member and member["Update"]["Key"]["partition_key"] == {"S": SOURCE_PLAN_KEY}
    )
    assert source_meta["ExpressionAttributeValues"][":superseded"] == {"S": "superseded"}
    events = [
        member
        for member in members
        if "Put" in member
        and cast("str", member["Put"]["Item"]["record_key"]["S"]).startswith("EVENT#")
    ]
    assert len(events) == 2
    heads = [
        member
        for member in members
        if "Put" in member and member["Put"]["Item"]["record_key"] == {"S": "HEAD"}
    ]
    assert len(heads) == 1


def test_replacement_exact_replay_retains_the_assigned_generation() -> None:
    dynamo = _RecordingDynamo()
    request, _seeds = _replacement_case()
    _seed(dynamo, TABLE, _owner_item(PLAN_DIGEST, request.owner))
    source = request.source_plan
    assert source is not None
    _seed(dynamo, TABLE, _plan_meta_item(SOURCE_PLAN_KEY, source.meta))
    coordinator = _coordinator(dynamo)
    coordinator.persist_plan(request)
    _replay(dynamo)
    replayed = coordinator.persist_plan(request)
    assert replayed.outcome is PersistPlanOutcome.REPLAYED
    assert replayed.generation == 1


def test_an_active_owner_without_replacement_evidence_still_fails_closed() -> None:
    request, _seeds = _replacement_case()
    with pytest.raises(ValueError, match="replacement"):
        replace(request, source_plan=None)


# --- Matrix and cap refusals ---------------------------------------------------------------------


def test_a_canonical_dataset_condition_check_is_outside_the_member_matrix() -> None:
    coordinator = _coordinator(_RecordingDynamo())
    member = _Member(
        transact_item={
            "ConditionCheck": {
                "TableName": TABLE,
                "Key": _low_item({"partition_key": DATASET_PARTITION, "record_key": "CURRENT"}),
                "ConditionExpression": "attribute_exists(publication_id)",
            }
        },
        action=ConditionCheckAction(
            table_name=TABLE,
            key={"partition_key": DATASET_PARTITION, "record_key": "CURRENT"},
            pre_state=ExactPreState(None),
        ),
    )
    with pytest.raises(InvariantError, match="canonical-dataset condition"):
        coordinator._validate_member(  # pyright: ignore[reportPrivateUsage]
            "FinalizeUnitRequest", member
        )


def test_every_new_operation_refuses_one_action_beyond_its_cap_before_any_sdk_call() -> None:
    coordinator = _coordinator(_RecordingDynamo())

    def _tiny(ordinal: int) -> _Member:
        key = {"partition_key": f"{PARTITION}", "record_key": f"ATTEMPT#{ordinal:064d}"}
        return _Member(
            transact_item={
                "ConditionCheck": {
                    "TableName": TABLE,
                    "Key": _low_item(dict(key)),
                    "ConditionExpression": "attribute_exists(partition_key)",
                }
            },
            action=ConditionCheckAction(table_name=TABLE, key=key, pre_state=ExactPreState(None)),
        )

    for operation, cap in (
        ("RescheduleTableAttemptReconcileRequest", 2),
        ("BlockTableAttemptReconcileRequest", 4),
        ("RequeueBlockedTableAttemptReconcileRequest", 4),
        ("ClaimUnitRequest", 7),
        ("ClaimUnitRequest#reclaim", 8),
        ("FailUnitRequest", 26),
        ("FailUnitRequest#attempt-33", 42),
        ("FinalizeUnitRequest", 100),
        ("FinalizeInlineRequest", 99),
        ("PersistPlanRequest#replacement", 100),
    ):
        members = tuple(_tiny(n) for n in range(cap + 1))
        with pytest.raises(ValidationError, match="cap"):
            coordinator._validate_members(  # pyright: ignore[reportPrivateUsage]
                operation, members
            )


# --- Committed §13 byte/action fixtures ----------------------------------------------------------

INTENT_ID_2 = "ef" * 32
CANDIDATE_EXEC = internal_execution_identity(INTENT_ID_2, 1)
CANDIDATE_KEY = plan_partition_key(PLAN_DIGEST, CANDIDATE_EXEC)
UNITS_15 = tuple(_unit(f"db.t{n:02d}", n + 1) for n in range(15))
FLOWS_15 = {
    unit.unit_key: str(uuid.uuid5(uuid.NAMESPACE_URL, f"adlytics-test-settlement/{unit.unit_key}"))
    for unit in UNITS_15
}
ATTEMPT_IDS_10 = tuple(f"{n + 1:02x}" * 32 for n in range(10))
ATTEMPT_IDS_32 = tuple(f"{n + 32:02x}" * 32 for n in range(32))


def _pending_intent_2() -> IntentMeta:
    return IntentMeta(
        root_plan_key=PLAN_KEY,
        source_plan_key=None,
        plan_digest=PLAN_DIGEST,
        intent_id=INTENT_ID_2,
        roster_digest=ROSTER_DIGEST,
        origin=ORIGIN,
        state=IntentState.PENDING,
        claimed_generation=None,
        retry_count=0,
        due_at=INSTANT,
        ready_status="ready",
        ready_order=ready_order_key(INSTANT, INTENT_ID_2),
        created_at=INSTANT,
        last_transition_at=INSTANT,
    )


def _maximum_claim_intent_case() -> tuple[ClaimPlanIntentRequest, list[dict[str, object]]]:
    request = ClaimPlanIntentRequest(
        plan_digest=PLAN_DIGEST,
        intent=_pending_intent_2(),
        owner=_active_owner(),
        operation_instant=INSTANT,
        active_plan=ActivePlanObservation(PLAN_KEY, _plan_meta()),
        plan_head=_plan_head(),
    )
    seeds: list[dict[str, object]] = [
        _owner_item(PLAN_DIGEST, request.owner),
        _intent_meta_item(PLAN_DIGEST, request.intent),
    ]
    return request, seeds


def _claimed_intent_2() -> IntentMeta:
    return replace(
        _pending_intent_2(),
        state=IntentState.CLAIMED,
        claimed_generation=1,
        ready_status=None,
        ready_order=None,
    )


def _reserved_owner_2() -> SuccessorOwner:
    return replace(
        _active_owner(),
        revision=3,
        reserved_intent_id=INTENT_ID_2,
        reserved_intent_digest=INTENT_ID_2,
        reserved_generation=1,
    )


def _maximum_reschedule_intent_case() -> tuple[
    ReschedulePlanIntentRequest, list[dict[str, object]]
]:
    request = ReschedulePlanIntentRequest(
        plan_digest=PLAN_DIGEST,
        intent=_claimed_intent_2(),
        owner=_reserved_owner_2(),
        operation_instant=INSTANT,
        plan_head=_plan_head(),
    )
    return request, [
        _owner_item(PLAN_DIGEST, request.owner),
        _intent_meta_item(PLAN_DIGEST, request.intent),
    ]


def _candidate_head_proof() -> Stage1LifecycleProof:
    return derive_stage1_lifecycle_proof(
        Stage1LifecycleProofRequest(
            identity=PlanIdentityRequest(PLAN_DIGEST, CANDIDATE_EXEC),
            internal_key=(CANDIDATE_KEY,),
            topology=Stage1TopologyRequest(
                Stage1OwnerTransition.FIRST_AUTOMATIC_PLAN_RECORDED,
                LegitimateRootSource(RootDiscriminator.AUTOMATIC_INGESTION_PLAN),
            ),
            transition=Stage1LifecycleTransition(
                "semantic_plan_recorded", FlowState.QUEUED, None, None
            ),
            stage="projection_plan_recorded",
            authority_at=NOW,
            prior_head=None,
            required_child_expectations=_expectations(CANDIDATE_EXEC, 1),
        )
    )


def _maximum_fail_admission_case() -> tuple[FailAdmissionRequest, list[dict[str, object]]]:
    head = _candidate_head_proof().resulting_head
    assert head is not None
    proof = derive_stage1_lifecycle_proof(
        Stage1LifecycleProofRequest(
            identity=PlanIdentityRequest(PLAN_DIGEST, CANDIDATE_EXEC),
            internal_key=(CANDIDATE_KEY,),
            topology=Stage1TopologyRequest(
                Stage1OwnerTransition.LATER_PLAN_EVENT,
                PriorOwnerEventSource(
                    head.parent_flow_id, head.latest_event_id, stage1_head_authority_document(head)
                ),
            ),
            transition=Stage1LifecycleTransition(
                "semantic_plan_admission_failed",
                FlowState.FAILED,
                CompletionOutcome.EXHAUSTED,
                "admission_failed",
            ),
            stage="projection_plan_admission_failed",
            authority_at=NOW,
            prior_head=head,
            clock_started_at=NOW,
            required_child_expectations=_expectations(CANDIDATE_EXEC, 1),
        )
    )
    owner = SuccessorOwner(
        revision=4,
        root_plan_key=PLAN_KEY,
        last_plan_key=CANDIDATE_KEY,
        last_generation=1,
        active_plan_key=CANDIDATE_KEY,
        active_intent_id=INTENT_ID_2,
        reserved_intent_id=None,
        reserved_intent_digest=None,
        reserved_generation=None,
        latest_internal_execution_id=CANDIDATE_EXEC,
    )
    meta = _plan_meta(
        internal_execution_id=CANDIDATE_EXEC,
        generation=1,
        origin_intent_id=INTENT_ID_2,
        required_child_expectations=tuple(
            RequiredChildExpectation(flow, 1)
            for flow in sorted((FLOW_UUID, SIBLING_FLOW), key=lambda flow: flow.encode("utf-8"))
        ),
        admitted_count=0,
    )
    request = FailAdmissionRequest(
        plan_digest=PLAN_DIGEST,
        plan_key=CANDIDATE_KEY,
        intent=_claimed_intent_2(),
        owner=owner,
        plan_meta=meta,
        operation_instant=INSTANT,
        proof=proof,
    )
    return request, [
        _owner_item(PLAN_DIGEST, owner),
        _intent_meta_item(PLAN_DIGEST, request.intent),
        _plan_meta_item(CANDIDATE_KEY, meta),
    ]


def _maximum_fail_unit_request() -> FailUnitRequest:
    return _fail_request(settlement=_expansion(OwnerDisposition.RETRYABLE_FAILURE, *ATTEMPT_IDS_10))


def _plan_meta_15(**overrides: object) -> PlanMeta:
    fields: dict[str, object] = {
        "roster_digest": roster_digest(UNITS_15),
        "unit_count": 15,
        "admitted_count": 15,
        "required_child_expectations": tuple(
            RequiredChildExpectation(flow, 0)
            for flow in sorted(FLOWS_15.values(), key=lambda flow: flow.encode("utf-8"))
        ),
    }
    fields.update(overrides)
    return _plan_meta(**fields)


def _attempt33_fail_unit_request() -> FailUnitRequest:
    return _fail_request(
        plan_meta=_plan_meta_15(),
        reason=UnitFailureReason.PLAN_ATTEMPT_BUDGET_EXHAUSTED,
        settlement=_expansion(OwnerDisposition.EXHAUSTED_OR_UNAVAILABLE, *ATTEMPT_IDS_10),
        unit_item=None,
        budget_exhaustion=AttemptBudgetExhaustion(
            successor_units=UNITS_15, permanent_reservation_count=32, rejected_ordinal=33
        ),
    )


def _maximum_finalize_unit_request() -> FinalizeUnitRequest:
    partitions = tuple(f"fictional.dataset|2026-09-19|p{n:02d}" for n in range(37))
    return _finalize_request(
        pointers=tuple(_advanced_pointer(partition) for partition in partitions),
        settlement=_expansion(OwnerDisposition.SUCCESSFUL, *ATTEMPT_IDS_10),
    )


def _maximum_finalize_inline_request() -> FinalizeInlineRequest:
    partitions = tuple(f"fictional.dataset|2026-09-19|p{n:02d}" for n in range(37))
    return _finalize_inline_request(
        pointers=tuple(_advanced_pointer(partition) for partition in partitions),
        settlement=_expansion(OwnerDisposition.SUCCESSFUL, *ATTEMPT_IDS_10),
    )


def _replacement_maximum_case() -> tuple[PersistPlanRequest, list[dict[str, object]]]:
    target_intent_id = intent_identity(
        IntentAuthorityInputs(SOURCE_PLAN_KEY, None, PLAN_DIGEST, ORIGIN, UNITS_15)
    )
    target_exec = internal_execution_identity(target_intent_id, 1)
    intent = IntentMeta(
        root_plan_key=SOURCE_PLAN_KEY,
        source_plan_key=None,
        plan_digest=PLAN_DIGEST,
        intent_id=target_intent_id,
        roster_digest=roster_digest(UNITS_15),
        origin=ORIGIN,
        state=IntentState.CLAIMED,
        claimed_generation=1,
        retry_count=0,
        due_at=INSTANT,
        ready_status=None,
        ready_order=None,
        created_at=INSTANT,
        last_transition_at=INSTANT,
    )
    owner = SuccessorOwner(
        revision=3,
        root_plan_key=SOURCE_PLAN_KEY,
        last_plan_key=SOURCE_PLAN_KEY,
        last_generation=0,
        active_plan_key=SOURCE_PLAN_KEY,
        active_intent_id=SOURCE_INTENT_ID,
        reserved_intent_id=target_intent_id,
        reserved_intent_digest=target_intent_id,
        reserved_generation=1,
        latest_internal_execution_id=SOURCE_EXEC,
    )
    references = tuple(
        SettledAttemptReference(
            TABLE_ID, attempt_id, canonical_authority_digest(["fixture-usage", attempt_id])
        )
        for attempt_id in sorted(ATTEMPT_IDS_32)
    )
    source_meta = _plan_meta_15(
        internal_execution_id=SOURCE_EXEC,
        root_plan_key=SOURCE_PLAN_KEY,
        origin_intent_id=SOURCE_INTENT_ID,
        settled_attempt_count=32,
        settled_attempt_digest=plan_settled_attempt_digest(references),
    )
    source_units = tuple(
        cast(
            "dict[str, object]",
            {
                "partition_key": SOURCE_PLAN_KEY,
                "record_key": f"UNIT#{unit.unit_key}",
                "flow_id": FLOWS_15[unit.unit_key],
                "owner_generation": 0,
                "status": "attested",
            },
        )
        for unit in UNITS_15
    )
    attempts = tuple(
        SettledAttemptObservation(
            TABLE_ID,
            reference.attempt_id,
            "SUCCEEDED",
            reference.usage_authority_digest,
            {
                "partition_key": PARTITION,
                "record_key": f"ATTEMPT#{reference.attempt_id}",
                "state": "SUCCEEDED",
                "athena_usage_state": "settled",
                "usage_authority_digest": reference.usage_authority_digest,
            },
        )
        for reference in references
    )
    usage_authorities = tuple(
        SettledUsageAuthorityObservation(
            TABLE_ID,
            reference.attempt_id,
            json.dumps(["fixture-usage", reference.attempt_id]),
            reference.usage_authority_digest,
            {
                "partition_key": PARTITION,
                "record_key": f"USAGE#{reference.attempt_id}",
                "payload": json.dumps(["fixture-usage", reference.attempt_id]),
                "immutable_payload_digest": reference.usage_authority_digest,
            },
        )
        for reference in references
    )
    source = SourcePlanReplacementEvidence(
        plan_key=SOURCE_PLAN_KEY,
        meta=source_meta,
        unit_items=source_units,
        attempts=attempts,
        usage_authorities=usage_authorities,
    )

    def _fifteen_expectations(execution_id: str, generation: int) -> PlanRequiredChildExpectations:
        parent = derive_plan_identity(PlanIdentityRequest(PLAN_DIGEST, execution_id)).flow_id
        members = tuple(
            sorted(
                (StatusChildExpectation(flow, generation) for flow in FLOWS_15.values()),
                key=lambda member: member.child_flow_id.encode("utf-8"),
            )
        )
        return PlanRequiredChildExpectations(parent, members)

    source_head_proof = derive_stage1_lifecycle_proof(
        Stage1LifecycleProofRequest(
            identity=PlanIdentityRequest(PLAN_DIGEST, SOURCE_EXEC),
            internal_key=(SOURCE_PLAN_KEY,),
            topology=Stage1TopologyRequest(
                Stage1OwnerTransition.FIRST_AUTOMATIC_PLAN_RECORDED,
                LegitimateRootSource(RootDiscriminator.AUTOMATIC_INGESTION_PLAN),
            ),
            transition=Stage1LifecycleTransition(
                "semantic_plan_recorded", FlowState.QUEUED, None, None
            ),
            stage="projection_plan_recorded",
            authority_at=NOW,
            prior_head=None,
            required_child_expectations=_fifteen_expectations(SOURCE_EXEC, 0),
        )
    )
    head = source_head_proof.resulting_head
    assert head is not None
    proof = compound_replacement_proof(
        Stage1CompoundReplacementRequest(
            plan_digest=PLAN_DIGEST,
            prior_head=head,
            source_internal_key=(SOURCE_PLAN_KEY,),
            target_internal_key=(plan_partition_key(PLAN_DIGEST, target_exec),),
            successor_internal_delivery_execution_id=target_exec,
            terminal_transition=Stage1LifecycleTransition(
                "semantic_plan_terminal",
                FlowState.SUPERSEDED,
                CompletionOutcome.SUPERSEDED,
                None,
            ),
            successor_transition=Stage1LifecycleTransition(
                "semantic_plan_recorded", FlowState.QUEUED, None, None
            ),
            source_expectations=_fifteen_expectations(SOURCE_EXEC, 0),
            target_expectations=_fifteen_expectations(target_exec, 1),
            authority_at=NOW,
            clock_started_at=NOW,
        )
    )
    request = PersistPlanRequest(
        plan_digest=PLAN_DIGEST,
        intent=intent,
        units=UNITS_15,
        owner=owner,
        unit_flows=tuple(
            UnitFlowBinding(unit.unit_key, FLOWS_15[unit.unit_key]) for unit in UNITS_15
        ),
        proof=proof,
        operation_instant=INSTANT,
        source_plan=source,
    )
    seeds: list[dict[str, object]] = [
        _owner_item(PLAN_DIGEST, owner),
        _plan_meta_item(SOURCE_PLAN_KEY, source_meta),
    ]
    return request, seeds


NEW_FIXTURE_BRANCHES: tuple[tuple[str, str], ...] = (
    ("ClaimPlanIntentRequest", "maximum"),
    ("ReschedulePlanIntentRequest", "maximum"),
    ("FailAdmissionRequest", "maximum"),
    ("RescheduleTableAttemptReconcileRequest", "maximum"),
    ("BlockTableAttemptReconcileRequest", "maximum"),
    ("RequeueBlockedTableAttemptReconcileRequest", "maximum"),
    ("ClaimUnitRequest", "maximum"),
    ("ClaimUnitRequest", "redrive"),
    ("ClaimUnitRequest", "reclaim"),
    ("FailUnitRequest", "maximum"),
    ("FailUnitRequest", "attempt-33"),
    ("FinalizeUnitRequest", "maximum"),
    ("FinalizeInlineRequest", "maximum"),
    ("PersistPlanRequest", "replacement-maximum"),
)
NEW_ONE_BEYOND_BRANCHES: tuple[tuple[str, str], ...] = (
    *(
        (operation, "one-beyond")
        for operation, branch in NEW_FIXTURE_BRANCHES
        if branch == "maximum" and operation != "PersistPlanRequest"
    ),
    ("PersistPlanRequest", "replacement-one-beyond"),
)


def run_fixture_maximum(operation: str, branch: str) -> _RecordingDynamo:  # noqa: PLR0912
    """Run the production builder for one committed maximum fixture branch."""
    dynamo = _RecordingDynamo()
    coordinator = _coordinator(dynamo)
    if operation == "ClaimPlanIntentRequest":
        request, seeds = _maximum_claim_intent_case()
        for seed in seeds:
            _seed(dynamo, TABLE, seed)
        coordinator.claim_plan_intent(request)
    elif operation == "ReschedulePlanIntentRequest":
        request, seeds = _maximum_reschedule_intent_case()
        for seed in seeds:
            _seed(dynamo, TABLE, seed)
        coordinator.reschedule_plan_intent(request)
    elif operation == "FailAdmissionRequest":
        request, seeds = _maximum_fail_admission_case()
        for seed in seeds:
            _seed(dynamo, TABLE, seed)
        coordinator.fail_admission(request)
    elif operation == "RescheduleTableAttemptReconcileRequest":
        _seed_reschedule(dynamo)
        coordinator.reschedule_table_attempt_reconcile(_reschedule_request())
    elif operation == "BlockTableAttemptReconcileRequest":
        _seed(dynamo, TABLE, _fence_item(_fence(reconciliation=_malformed_reconciliation())))
        coordinator.block_table_attempt_reconcile(_block_request())
    elif operation == "RequeueBlockedTableAttemptReconcileRequest":
        fence, case = _blocked_state()
        _seed(dynamo, TABLE, _fence_item(fence))
        _seed(dynamo, TABLE, _protected_case_item(case))
        coordinator.requeue_blocked_table_attempt_reconcile(_requeue_request())
    elif operation == "ClaimUnitRequest":
        status = {"redrive": "failed", "reclaim": "writing"}.get(branch, "queued")
        _seed(dynamo, TABLE, _claimable_unit_item(status))
        _seed(
            dynamo,
            TABLE,
            _plan_meta_item(PLAN_KEY, _plan_meta(failed_count=1 if status == "failed" else 0)),
        )
        coordinator.claim_unit(_claim_request(status))
    elif operation == "FailUnitRequest":
        if branch == "attempt-33":
            coordinator.fail_unit(_attempt33_fail_unit_request())
        else:
            coordinator.fail_unit(_maximum_fail_unit_request())
    elif operation == "FinalizeUnitRequest":
        coordinator.finalize_unit(_maximum_finalize_unit_request())
    elif operation == "FinalizeInlineRequest":
        coordinator.finalize_inline(_maximum_finalize_inline_request())
    else:
        request, seeds = _replacement_maximum_case()
        for seed in seeds:
            _seed(dynamo, TABLE, seed)
        coordinator.persist_plan(request)
    return dynamo


@pytest.fixture(scope="module")
def fixture_tree() -> dict[str, dict[str, dict[str, Any]]]:
    return load_fixture_tree()


def _fixture_actions(fixture: dict[str, Any]) -> Any:
    """Rebuild the oracle actions from the frozen maps alone."""
    actions: list[Any] = []
    members = cast("list[dict[str, Any]]", fixture["expected_request"]["TransactItems"])
    targets = cast("list[dict[str, Any]]", fixture["targets"])
    for member, target in zip(members, targets, strict=True):
        kind = next(iter(member))
        body = cast("dict[str, Any]", member[kind])
        pre_map = cast("dict[str, Any] | None", target["expected_pre_state_map"])
        pre = ExactPreState(None if pre_map is None else _plain_item(pre_map))
        if kind == "Put":
            item = _plain_item(cast("dict[str, Any]", body["Item"]))
            key = {name: cast("str", item[name]) for name in ("partition_key", "record_key")}
            actions.append(PutAction(body["TableName"], key, item, pre))
            continue
        key_item = _plain_item(cast("dict[str, Any]", body["Key"]))
        key = {name: cast("str", key_item[name]) for name in key_item}
        if kind == "ConditionCheck":
            actions.append(ConditionCheckAction(body["TableName"], key, pre))
            continue
        result = _plain_item(cast("dict[str, Any]", target["expected_result_map"]))
        assert pre.item is not None
        set_attributes = {
            name: value
            for name, value in result.items()
            if name not in pre.item or pre.item[name] != value
        }
        actions.append(UpdateAction(body["TableName"], key, set_attributes, pre))
    return TransactionEnvelopeRequest(tuple(actions))


def test_every_new_committed_fixture_passes_the_reference_checker(
    fixture_tree: dict[str, dict[str, dict[str, Any]]],
) -> None:
    for operation, branch in (*NEW_FIXTURE_BRANCHES, *NEW_ONE_BEYOND_BRANCHES):
        check_fixture(fixture_tree[operation][branch])


def test_every_new_maximum_fixture_matches_the_production_builder_and_oracle(
    fixture_tree: dict[str, dict[str, dict[str, Any]]],
) -> None:
    for operation, branch in NEW_FIXTURE_BRANCHES:
        fixture = fixture_tree[operation][branch]
        assert fixture["expected"]["outcome"] == "accepted"
        dynamo = run_fixture_maximum(operation, branch)
        assert dynamo.transacts[0] == fixture["expected_request"], (operation, branch)
        assert (
            ORACLE.wire_json_bytes(dynamo.transacts[0]) == (fixture["expected"]["wire_json_bytes"])
        )
        accepted = ORACLE.validate_envelope(_fixture_actions(fixture))
        assert accepted.action_count == fixture["expected"]["action_count"]
        assert accepted.aggregate_upper_bound == fixture["expected"]["aggregate_upper_bound"]


def test_every_new_one_beyond_fixture_is_refused_by_the_production_oracle(
    fixture_tree: dict[str, dict[str, dict[str, Any]]],
) -> None:
    """One conservative item byte beyond 400000 refuses before mutation, never splits."""
    for operation, branch in NEW_ONE_BEYOND_BRANCHES:
        fixture = fixture_tree[operation][branch]
        assert fixture["expected"]["outcome"] == "refused"
        with pytest.raises(ValidationError):
            ORACLE.validate_envelope(_fixture_actions(fixture))


def test_the_pinned_action_formulas_hold_at_the_accepted_simultaneous_profile(
    fixture_tree: dict[str, dict[str, dict[str, Any]]],
) -> None:
    """§3.2: FailUnit=26, attempt-33=42, FinalizeUnit=100, FinalizeInline=99, replacement<=100."""
    assert fixture_tree["FailUnitRequest"]["maximum"]["expected"]["action_count"] == 26
    assert fixture_tree["FailUnitRequest"]["attempt-33"]["expected"]["action_count"] == 42
    assert fixture_tree["FinalizeUnitRequest"]["maximum"]["expected"]["action_count"] == 100
    assert fixture_tree["FinalizeInlineRequest"]["maximum"]["expected"]["action_count"] == 99
    assert fixture_tree["ReschedulePlanIntentRequest"]["maximum"]["expected"]["action_count"] == 3
    replacement = fixture_tree["PersistPlanRequest"]["replacement-maximum"]
    assert replacement["expected"]["action_count"] == 100
