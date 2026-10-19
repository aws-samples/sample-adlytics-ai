# SPDX-License-Identifier: MIT-0
"""ADR-075 §3.2/§5 table-fence and provider-attempt builders of the coordinator."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from botocore.exceptions import ClientError
from byte_oracle_support import load_fixture_tree
from dynamodb_reserved_words import guard_reserved_words

from adapters.outbound.dynamodb.byte_oracle import (
    ConditionCheckAction,
    ExactPreState,
    Stage1DynamoByteOracle,
    TransactionEnvelopeRequest,
    UpdateAction,
)
from adapters.outbound.dynamodb.projection_coordinator import (
    DynamoProjectionCoordinator,
    _attempt_item,  # pyright: ignore[reportPrivateUsage] -- seeding the recording fake
    _fence_item,  # pyright: ignore[reportPrivateUsage] -- seeding the recording fake
    _low_item,  # pyright: ignore[reportPrivateUsage] -- seeding the recording fake
    _plain_item,  # pyright: ignore[reportPrivateUsage] -- frozen-map reconstruction
)
from adapters.outbound.dynamodb.stage1_lifecycle_head import DynamoStage1LifecycleHeadStore
from domain.operations.stage1_lifecycle_head import (
    Stage1LifecycleProof,
    Stage1LifecycleProofRequest,
    Stage1LifecycleTransition,
    derive_stage1_lifecycle_proof,
    stage1_head_authority_document,
)
from domain.operations.stage1_operational_status import (
    InlineIdentityRequest,
    LegitimateRootSource,
    PlanAdmissionSource,
    PriorOwnerEventSource,
    RootDiscriminator,
    Stage1OwnerTransition,
    Stage1TopologyRequest,
    UnitIdentityRequest,
)
from domain.operations.status import FlowState
from domain.operations.table_attempt import (
    AttemptIdentityRequest,
    AttemptObservation,
    AttemptOwnerKind,
    BindingDigestRequest,
    FenceObservation,
    ProviderErrorClass,
    ProviderTerminalState,
    ReconciliationObservation,
    RunIdentityRequest,
    SanitizedStartEvidence,
    WriterKind,
    attempt_identity,
    attempt_reservation_identity,
    binding_digest,
    parse_attempt,
    pending_reconcile_key,
    recompute_attempt_identity,
    run_identity,
)
from domain.operations.table_lease import (
    RELEASED_LEASE_EXPIRY,
    TableLeaseRecord,
    lease_expiry_epoch_micros,
)
from domain.operations.write_plan import (
    IntentMeta,
    IntentOrigin,
    IntentOriginKind,
    IntentState,
    SuccessorOwner,
    plan_digest_for_source_delivery,
    source_delivery_identity,
)
from domain.shared_kernel.errors import DomainError, InvariantError, ValidationError
from ports.outbound.projection_coordinator import (
    AcquireTableFenceOutcome,
    AcquireTableFenceRequest,
    BeginInlineRunOwner,
    BeginQueuedUnitOwner,
    BeginTableAttemptOutcome,
    BeginTableAttemptRequest,
    CompleteTableAttemptOutcome,
    CompleteTableAttemptRequest,
    GraphAuthorityRefusedError,
    GraphClaimFence,
    InlineRunAttemptOwner,
    ParkAttemptOutcome,
    ParkAttemptRequest,
    PersistInlineRunOutcome,
    PersistInlineRunRequest,
    QueuedUnitAttemptOwner,
    RecordStage1RefusalOutcome,
    RecordStage1RefusalRequest,
    RecoverParkedAttemptSubmissionOutcome,
    RecoverParkedAttemptSubmissionRequest,
    RefusedInlineRunObservation,
    RefusedUnitObservation,
    ReleaseTableFenceOutcome,
    ReleaseTableFenceRequest,
    RequestAttemptCancellationOutcome,
    RequestAttemptCancellationRequest,
    Stage1RefusalReason,
    Stage1RefusalTag,
)

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-075")]

# Every identity below is fictional fixture data; none is copied from a live system.

TABLE = "publications-fixture"
LEASES = "leases-fixture"
TABLE_ID = "db.alpha"
PARTITION = f"TABLE_ATTEMPT#{TABLE_ID}"
HOLDER = "holder-fixture-0001"
TOKEN = 7
EXPIRES = "2026-09-19T13:00:00+00:00"
EXPIRY_MICROS = lease_expiry_epoch_micros(EXPIRES)
INSTANT = f"{EXPIRY_MICROS - 900_000_000:020d}"
NEW_EXPIRES = "2026-09-19T13:20:00+00:00"
NOW = datetime(2026, 9, 19, 12, 34, 56, 789012, tzinfo=UTC)
LEASE = TableLeaseRecord(TABLE_ID, HOLDER, TOKEN, EXPIRES)

WRITER = WriterKind.INGEST_DELIVERY_OPERATOR
OPERATION_ID = "op-fixture-0001"
EXECUTION_ID = "exec-fixture-0001"
RUN_ID = run_identity(RunIdentityRequest(WRITER, OPERATION_ID, EXECUTION_ID, TABLE_ID))
RESERVATION = "reservation-slot-fixture-0001"
ATTEMPT_ID = "a1" * 32
ENVELOPE = '{"WorkGroup":"fixture-workgroup"}'
QUERY_ID = "athena-query-fixture-0001"
EVIDENCE = SanitizedStartEvidence(200, ProviderErrorClass.NONE)

ORIGIN_MEMBER = source_delivery_identity((("manifests/delivery-0001.json", "v-0001"),))
PLAN_DIGEST = plan_digest_for_source_delivery(ORIGIN_MEMBER)
ORIGIN = IntentOrigin(IntentOriginKind.SOURCE_DELIVERY, ORIGIN_MEMBER)
INTENT_ID = "ab" * 32
ROSTER_DIGEST = "cd" * 32
EXEC64 = "12" * 32
PLAN_KEY = f"WRITE_PLAN#{PLAN_DIGEST}#{EXEC64}"
WRITER_EXEC = "writer-exec-0001"
UNIT_FLOW = str(uuid.uuid5(uuid.NAMESPACE_URL, "adlytics-test-unit-flow/db.alpha"))


def _flow_uuid(name: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"adlytics-test-fence/{name}"))


# --- Lifecycle proofs -----------------------------------------------------------------------------


def _inline_identity() -> InlineIdentityRequest:
    return InlineIdentityRequest(WRITER.value, OPERATION_ID, TABLE_ID, EXECUTION_ID)


def _inline_proof(
    transition_id: str,
    *,
    typed_reason: str | None = None,
    first: bool = False,
) -> Stage1LifecycleProof:
    state = (
        FlowState.RETRYING
        if transition_id.endswith(("_refused", "_reconciled"))
        else (FlowState.RUNNING)
    )
    if first:
        topology = Stage1TopologyRequest(
            Stage1OwnerTransition.FIRST_INGEST_OPERATOR_INLINE_RECORDED,
            LegitimateRootSource(RootDiscriminator.INGEST_DELIVERY_OPERATOR_INLINE),
        )
        prior = None
    else:
        head = _inline_proof("semantic_inline_run_recorded", first=True).resulting_head
        assert head is not None
        topology = Stage1TopologyRequest(
            Stage1OwnerTransition.LATER_INLINE_EVENT,
            PriorOwnerEventSource(
                head.parent_flow_id, head.latest_event_id, stage1_head_authority_document(head)
            ),
        )
        prior = head
    return derive_stage1_lifecycle_proof(
        Stage1LifecycleProofRequest(
            identity=_inline_identity(),
            internal_key=(WRITER.value, OPERATION_ID, TABLE_ID, EXECUTION_ID),
            topology=topology,
            transition=Stage1LifecycleTransition(transition_id, state, None, typed_reason),
            stage="inline_run_fixture_stage",
            authority_at=NOW,
            prior_head=prior,
            clock_started_at=None if prior is None else NOW,
        )
    )


RECORDED_PROOF = _inline_proof("semantic_inline_run_recorded", first=True)


def _unit_started_proof() -> Stage1LifecycleProof:
    return derive_stage1_lifecycle_proof(
        Stage1LifecycleProofRequest(
            identity=UnitIdentityRequest(PLAN_DIGEST, TABLE_ID, WRITER_EXEC),
            internal_key=(PLAN_DIGEST, TABLE_ID),
            topology=Stage1TopologyRequest(
                Stage1OwnerTransition.FIRST_UNIT_STARTED,
                PlanAdmissionSource(
                    plan_flow_id=_flow_uuid("plan"),
                    admitted_event_id=_flow_uuid("admitted"),
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


def _unit_proof(transition_id: str, typed_reason: str | None) -> Stage1LifecycleProof:
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
            transition=Stage1LifecycleTransition(
                transition_id, FlowState.RETRYING, None, typed_reason
            ),
            stage="projection_unit_fixture_stage",
            authority_at=NOW,
            prior_head=head,
            clock_started_at=NOW,
            owner_generation=0,
        )
    )


# --- Observation builders -------------------------------------------------------------------------


def _pending_reconciliation(
    attempt_id: str = ATTEMPT_ID, due_micros: int = EXPIRY_MICROS, count: int = 0
) -> ReconciliationObservation:
    digest = binding_digest(BindingDigestRequest(TABLE_ID, attempt_id, HOLDER, TOKEN))
    return ReconciliationObservation(
        reconcile_status="pending",
        reconcile_after_or_block=pending_reconcile_key(due_micros, digest),
        reconcile_attempt_count=count,
    )


def _clear_fence() -> FenceObservation:
    return FenceObservation(
        partition_key=PARTITION,
        record_key="FENCE",
        state="clear",
        physical_table_id=TABLE_ID,
        attempt_id=None,
        lease_holder=None,
        fencing_token=None,
        parked_at=None,
    )


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
        attempt_id=ATTEMPT_ID,
        lease_holder=HOLDER,
        fencing_token=TOKEN,
        parked_at=parked_at,
        reconciliation=reconciliation or _pending_reconciliation(),
    )


def _attempt(state: str = "submitted", **overrides: object) -> AttemptObservation:
    fields: dict[str, object] = {
        "partition_key": PARTITION,
        "record_key": f"ATTEMPT#{ATTEMPT_ID}",
        "state": state,
        "physical_table_id": TABLE_ID,
        "attempt_id": ATTEMPT_ID,
        "plan_unit_owner_ref": None,
        "inline_run_owner_ref": RUN_ID,
        "ordinal": 1,
        "statement_kind": "merge",
        "lease_holder": HOLDER,
        "fencing_token": TOKEN,
        "writer_execution_id": EXECUTION_ID,
        "start_envelope": ENVELOPE,
        "client_request_token": ATTEMPT_ID,
        "envelope_digest": "e" * 64,
        "reservation_identity": RESERVATION,
    }
    if state in ("submitted", "cancel_requested"):
        fields.update(
            {
                "query_execution_id": QUERY_ID,
                "submitted_at": INSTANT,
                "sanitized_start_evidence": EVIDENCE,
            }
        )
    fields.update(overrides)
    return AttemptObservation(**fields)  # type: ignore[arg-type]


def _queued_attempt(state: str = "submitted", **overrides: object) -> AttemptObservation:
    return _attempt(
        state,
        plan_unit_owner_ref=PLAN_KEY,
        inline_run_owner_ref=None,
        writer_execution_id=WRITER_EXEC,
        **overrides,
    )


def _run_item(**overrides: object) -> dict[str, object]:
    item: dict[str, object] = {
        "partition_key": PARTITION,
        "record_key": f"RUN#{RUN_ID}",
        "run_id": RUN_ID,
        "writer_kind": WRITER.value,
        "operation_id": OPERATION_ID,
        "execution_id": EXECUTION_ID,
        "physical_table_id": TABLE_ID,
        "status_flow_binding": RECORDED_PROOF.flow_id,
        "execution_binding": RECORDED_PROOF.execution_id,
        "table_kind": "current",
        "statement_intent_kinds": ["merge", "verify"],
        "created_at": INSTANT,
        "state": "open",
        "next_ordinal": 1,
        "accepted_attempt_reservations": 0,
        "attempt_references": [],
    }
    item.update(overrides)
    return item


def _bound_run_item(**overrides: object) -> dict[str, object]:
    return _run_item(
        current_attempt_id=ATTEMPT_ID,
        lease_holder=HOLDER,
        fencing_token=TOKEN,
        reserved_slot_identity=RESERVATION,
        accepted_attempt_reservations=1,
        **overrides,
    )


def _unit_item(**overrides: object) -> dict[str, object]:
    item: dict[str, object] = {
        "partition_key": PLAN_KEY,
        "record_key": f"UNIT#{TABLE_ID}",
        "flow_id": UNIT_FLOW,
        "owner_generation": 0,
        "status": "writing",
        "admission": "admitted",
        "writer_execution_id": WRITER_EXEC,
        "created_at": INSTANT,
    }
    item.update(overrides)
    return item


def _bound_unit_item(**overrides: object) -> dict[str, object]:
    return _unit_item(
        current_attempt_id=ATTEMPT_ID,
        ordinal=1,
        lease_holder=HOLDER,
        fencing_token=TOKEN,
        reserved_slot_identity=RESERVATION,
        unit_reservation_count=1,
        **overrides,
    )


def _meta_item(**overrides: object) -> dict[str, object]:
    item: dict[str, object] = {
        "partition_key": PLAN_KEY,
        "record_key": "META",
        "status": "planned",
        "generation": 0,
        "origin_intent_id": INTENT_ID,
        "unit_count": 2,
        "admitted_count": 2,
    }
    item.update(overrides)
    return item


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


def _seed(dynamo: _RecordingDynamo, table: str, item: Mapping[str, object]) -> None:
    low = _low_item(item)
    record = cast("str", item.get("record_key", ""))
    dynamo.items[(table, cast("str", item["partition_key"]), record)] = dict(low)


def _members(dynamo: _RecordingDynamo, index: int = -1) -> list[dict[str, Any]]:
    return cast("list[dict[str, Any]]", dynamo.transacts[index]["TransactItems"])


def _kinds(members: list[dict[str, Any]]) -> list[str]:
    return [next(iter(member)) for member in members]


# --- AcquireTableFenceRequest -----------------------------------------------------------------


def _acquire_request(**overrides: object) -> AcquireTableFenceRequest:
    fields: dict[str, object] = {
        "physical_table_id": TABLE_ID,
        "holder": HOLDER,
        "operation_instant": INSTANT,
        "new_expires_at": NEW_EXPIRES,
        "observed_lease": None,
        "observed_fence": None,
    }
    fields.update(overrides)
    return AcquireTableFenceRequest(**fields)  # type: ignore[arg-type]


def test_bootstrap_acquire_creates_the_lease_at_token_one_against_an_absent_fence() -> None:
    dynamo = _RecordingDynamo()
    result = _coordinator(dynamo).acquire_table_fence(_acquire_request())

    assert result.outcome is AcquireTableFenceOutcome.ACQUIRED
    assert (result.fencing_token, result.expires_at) == (1, NEW_EXPIRES)
    members = _members(dynamo)
    # Grain: one transaction of two distinct-item actions (lease update, fence condition).
    assert _kinds(members) == ["Update", "ConditionCheck"]
    lease = members[0]["Update"]
    assert lease["TableName"] == LEASES
    assert lease["Key"] == {"partition_key": {"S": f"TABLE#{TABLE_ID}"}}
    assert lease["ConditionExpression"] == "attribute_not_exists(partition_key)"
    assert lease["UpdateExpression"] == (
        "SET holder = :holder, expires_at = :expires, fencing_token = :token"
    )
    assert lease["ExpressionAttributeValues"][":token"] == {"N": "1"}
    fence = members[1]["ConditionCheck"]
    assert fence["TableName"] == TABLE
    assert fence["Key"]["record_key"] == {"S": "FENCE"}
    assert fence["ConditionExpression"] == "attribute_not_exists(partition_key)"


def test_acquire_cases_the_exact_observed_lease_and_increments_the_token() -> None:
    dynamo = _RecordingDynamo()
    result = _coordinator(dynamo).acquire_table_fence(
        _acquire_request(observed_lease=LEASE, observed_fence=_clear_fence())
    )

    assert result.fencing_token == TOKEN + 1
    lease = _members(dynamo)[0]["Update"]
    assert lease["ConditionExpression"] == (
        "holder = :observed_holder AND fencing_token = :observed_token AND "
        "expires_at = :observed_expires"
    )
    assert lease["ExpressionAttributeValues"][":observed_token"] == {"N": str(TOKEN)}
    fence = _members(dynamo)[1]["ConditionCheck"]
    assert fence["ConditionExpression"] == "#state = :clear AND attribute_not_exists(attempt_id)"


def test_acquire_returns_attempt_pending_for_a_valid_active_fence_without_any_write() -> None:
    dynamo = _RecordingDynamo()
    result = _coordinator(dynamo).acquire_table_fence(_acquire_request(observed_fence=_fence()))

    assert result.outcome is AcquireTableFenceOutcome.TABLE_ATTEMPT_PENDING
    assert dynamo.transacts == []


def test_acquire_fails_as_an_invariant_before_busy_for_an_invalid_reconciliation() -> None:
    broken = _fence(reconciliation=ReconciliationObservation(reconcile_status="pending"))
    dynamo = _RecordingDynamo()
    with pytest.raises(InvariantError, match="before returning busy"):
        _coordinator(dynamo).acquire_table_fence(_acquire_request(observed_fence=broken))
    assert dynamo.transacts == []


def test_acquire_returns_table_busy_for_another_holders_unexpired_lease() -> None:
    incumbent = TableLeaseRecord(TABLE_ID, "holder-fixture-0002", 9, EXPIRES)
    dynamo = _RecordingDynamo()
    result = _coordinator(dynamo).acquire_table_fence(_acquire_request(observed_lease=incumbent))

    assert result.outcome is AcquireTableFenceOutcome.TABLE_BUSY
    assert result.held_until == EXPIRES
    assert dynamo.transacts == []


def test_the_tagged_acquire_variant_puts_only_the_lease_acquired_event_and_head() -> None:
    proof = _unit_proof("semantic_unit_lease_acquired", None)
    dynamo = _RecordingDynamo()
    result = _coordinator(dynamo).acquire_table_fence(_acquire_request(proof=proof))

    assert result.outcome is AcquireTableFenceOutcome.ACQUIRED
    members = _members(dynamo)
    assert _kinds(members) == ["Update", "ConditionCheck", "Put", "Put"]
    event_key = cast("dict[str, Any]", members[2]["Put"]["Item"])["record_key"]["S"]
    assert event_key.startswith("EVENT#")


def test_the_tagged_acquire_variant_rejects_any_other_event() -> None:
    with pytest.raises(ValueError, match="semantic_unit_lease_acquired"):
        _acquire_request(proof=_unit_proof("semantic_unit_refused", "table_busy"))


def test_the_tagged_acquire_variant_is_forbidden_for_an_unclaimed_unit() -> None:
    """A first-event proof has no prior unit head, which is what a claim creates."""
    rootless = derive_stage1_lifecycle_proof(
        Stage1LifecycleProofRequest(
            identity=UnitIdentityRequest(PLAN_DIGEST, TABLE_ID, WRITER_EXEC),
            internal_key=(PLAN_DIGEST, TABLE_ID),
            topology=Stage1TopologyRequest(
                Stage1OwnerTransition.FIRST_UNIT_STARTED,
                PlanAdmissionSource(
                    plan_flow_id=_flow_uuid("plan"),
                    admitted_event_id=_flow_uuid("admitted"),
                    plan_meta_authority=["fixture-meta"],
                    complete_plan_head_authority=["fixture-head"],
                    admitted_event_authority=["fixture-admitted"],
                ),
            ),
            transition=Stage1LifecycleTransition(
                "semantic_unit_lease_acquired", FlowState.RUNNING, None, None
            ),
            stage="projection_unit_lease_acquired",
            authority_at=NOW,
            prior_head=None,
            owner_generation=0,
        )
    )
    with pytest.raises(ValueError, match="unclaimed"):
        _acquire_request(proof=rootless)


def test_a_lost_acquire_condition_reclassifies_by_strong_reread() -> None:
    dynamo = _RecordingDynamo()
    dynamo.fail_next_transactions = 1
    fence_item = {
        "partition_key": PARTITION,
        "record_key": "FENCE",
        "state": "active",
        "physical_table_id": TABLE_ID,
        "attempt_id": ATTEMPT_ID,
        "lease_holder": HOLDER,
        "fencing_token": TOKEN,
        "reconcile_status": "pending",
        "reconcile_attempt_count": 0,
        "reconcile_after_or_block": pending_reconcile_key(
            EXPIRY_MICROS,
            binding_digest(BindingDigestRequest(TABLE_ID, ATTEMPT_ID, HOLDER, TOKEN)),
        ),
    }
    _seed(dynamo, TABLE, fence_item)
    result = _coordinator(dynamo).acquire_table_fence(_acquire_request())
    assert result.outcome is AcquireTableFenceOutcome.TABLE_ATTEMPT_PENDING


def test_a_lost_acquire_condition_with_no_cause_is_a_typed_conflict() -> None:
    dynamo = _RecordingDynamo()
    dynamo.fail_next_transactions = 1
    with pytest.raises(DomainError, match="reclassify"):
        _coordinator(dynamo).acquire_table_fence(_acquire_request())


# --- ReleaseTableFenceRequest -----------------------------------------------------------------


def test_release_sets_only_the_released_expiry_conditioned_on_the_exact_holder() -> None:
    dynamo = _RecordingDynamo()
    result = _coordinator(dynamo).release_table_fence(
        ReleaseTableFenceRequest(TABLE_ID, LEASE, None)
    )

    assert result.outcome is ReleaseTableFenceOutcome.RELEASED
    members = _members(dynamo)
    assert _kinds(members) == ["Update", "ConditionCheck"]
    lease = members[0]["Update"]
    assert lease["ConditionExpression"] == "holder = :holder AND fencing_token = :token"
    assert lease["UpdateExpression"] == "SET expires_at = :released"
    assert lease["ExpressionAttributeValues"][":released"] == {"S": RELEASED_LEASE_EXPIRY}


def test_release_removes_the_park_instant_exactly_when_present() -> None:
    parked = replace(LEASE, parked_at=INSTANT)
    dynamo = _RecordingDynamo()
    _coordinator(dynamo).release_table_fence(ReleaseTableFenceRequest(TABLE_ID, parked, None))
    assert (
        _members(dynamo)[0]["Update"]["UpdateExpression"]
        == "SET expires_at = :released REMOVE parked_at"
    )


def test_release_is_impossible_while_the_fence_is_active() -> None:
    with pytest.raises(ValueError, match="impossible while a fence"):
        ReleaseTableFenceRequest(TABLE_ID, LEASE, _fence())


def test_a_lost_release_condition_is_a_typed_conflict_not_a_retry() -> None:
    dynamo = _RecordingDynamo()
    dynamo.fail_next_transactions = 1
    with pytest.raises(DomainError, match="impossible while a fence is active"):
        _coordinator(dynamo).release_table_fence(ReleaseTableFenceRequest(TABLE_ID, LEASE, None))


# --- BeginTableAttemptRequest -----------------------------------------------------------------


def _begin_inline_request(**overrides: object) -> BeginTableAttemptRequest:
    fields: dict[str, object] = {
        "physical_table_id": TABLE_ID,
        "lease": LEASE,
        "operation_instant": INSTANT,
        "observed_fence": None,
        "owner": BeginInlineRunOwner(InlineRunAttemptOwner(TABLE_ID, RUN_ID, _run_item())),
        "writer_execution_id": EXECUTION_ID,
        "statement_kind": "merge",
        "start_envelope": ENVELOPE,
    }
    fields.update(overrides)
    return BeginTableAttemptRequest(**fields)  # type: ignore[arg-type]


def _begin_queued_request(**overrides: object) -> BeginTableAttemptRequest:
    owner = BeginQueuedUnitOwner(
        QueuedUnitAttemptOwner(PLAN_KEY, TABLE_ID, _unit_item()), _meta_item()
    )
    return _begin_inline_request(owner=owner, writer_execution_id=WRITER_EXEC, **overrides)


def test_queued_begin_assembles_the_exact_five_member_transaction() -> None:
    dynamo = _RecordingDynamo()
    result = _coordinator(dynamo).begin_table_attempt(_begin_queued_request())

    assert result.outcome is BeginTableAttemptOutcome.BEGUN
    members = _members(dynamo)
    # Grain: lease condition, attempt put, fence update, unit update, plan META update.
    assert _kinds(members) == ["ConditionCheck", "Put", "Update", "Update", "Update"]
    lease = members[0]["ConditionCheck"]
    assert lease["TableName"] == LEASES
    assert lease["ConditionExpression"] == (
        "holder = :holder AND fencing_token = :token AND expires_at = :expires AND "
        "attribute_not_exists(parked_at)"
    )
    attempt = members[1]["Put"]
    assert attempt["ConditionExpression"] == "attribute_not_exists(partition_key)"
    assert attempt["Item"]["state"] == {"S": "intent"}
    assert attempt["Item"]["plan_unit_owner_ref"] == {"S": PLAN_KEY}
    assert attempt["Item"]["ordinal"] == {"N": "1"}
    unit = members[3]["Update"]
    assert unit["ConditionExpression"] == (
        "#status = :writing AND writer_execution_id = :writer AND "
        "attribute_not_exists(current_attempt_id) AND "
        "attribute_not_exists(unit_reservation_count)"
    )
    assert unit["ExpressionAttributeValues"][":new_count"] == {"N": "1"}
    meta = members[4]["Update"]
    assert meta["ConditionExpression"] == (
        "#status = :planned AND unit_count = :unit_count AND "
        "admitted_count = :unit_count AND attribute_not_exists(plan_reservation_count)"
    )
    assert meta["UpdateExpression"] == "SET plan_reservation_count = :new_plan_count"


def test_begin_derives_the_attempt_identity_through_the_pinned_d80_constructor() -> None:
    dynamo = _RecordingDynamo()
    result = _coordinator(dynamo).begin_table_attempt(_begin_queued_request())

    expected = attempt_identity(
        AttemptIdentityRequest(
            owner_kind=AttemptOwnerKind.QUEUED_UNIT,
            owner_ref=PLAN_KEY,
            physical_table_id=TABLE_ID,
            lease_holder=HOLDER,
            fencing_token=TOKEN,
            ordinal=1,
            start_envelope=ENVELOPE,
        )
    )
    assert result.attempt_id == expected.attempt_id
    assert result.client_request_token == expected.attempt_id
    put = _members(dynamo)[1]["Put"]["Item"]
    assert put["attempt_id"] == {"S": expected.attempt_id}
    assert put["client_request_token"] == {"S": expected.client_request_token}
    assert put["envelope_digest"] == {"S": expected.envelope_digest}
    # The stored core recomputes byte-exactly through the production clause-4 hook.
    parsed = parse_attempt(
        _queued_attempt(
            "intent",
            record_key=f"ATTEMPT#{expected.attempt_id}",
            attempt_id=expected.attempt_id,
            client_request_token=expected.client_request_token,
            envelope_digest=expected.envelope_digest,
            query_execution_id=None,
            submitted_at=None,
            sanitized_start_evidence=None,
        )
    )
    recomputed = recompute_attempt_identity(parsed.core)
    assert recomputed.attempt_id == expected.attempt_id
    assert recomputed.envelope_digest == expected.envelope_digest


def test_begin_writes_the_derived_reservation_identity_to_the_attempt_and_owner() -> None:
    """§5 step 2: the permanent-slot bytes come only from the pinned constructor."""
    dynamo = _RecordingDynamo()
    result = _coordinator(dynamo).begin_table_attempt(_begin_inline_request())

    derived = attempt_reservation_identity(result.attempt_id)
    members = _members(dynamo)
    attempt = members[1]["Put"]["Item"]
    assert attempt["reservation_identity"] == {"S": derived}
    run = members[3]["Update"]
    assert run["ExpressionAttributeValues"][":slot"] == {"S": derived}


def test_begin_activates_the_fence_with_the_initial_due_at_the_lease_expiry() -> None:
    dynamo = _RecordingDynamo()
    result = _coordinator(dynamo).begin_table_attempt(_begin_inline_request())

    fence = _members(dynamo)[2]["Update"]
    assert fence["ConditionExpression"] == "attribute_not_exists(partition_key)"
    digest = binding_digest(BindingDigestRequest(TABLE_ID, result.attempt_id, HOLDER, TOKEN))
    assert fence["ExpressionAttributeValues"][":due_key"] == {
        "S": pending_reconcile_key(EXPIRY_MICROS, digest)
    }
    assert fence["ExpressionAttributeValues"][":zero"] == {"N": "0"}


def test_inline_begin_reserves_the_run_slot_at_its_current_ordinal() -> None:
    dynamo = _RecordingDynamo()
    result = _coordinator(dynamo).begin_table_attempt(_begin_inline_request())

    assert result.ordinal == 1
    run = _members(dynamo)[3]["Update"]
    assert run["ConditionExpression"] == (
        "#state IN (:open, :reconciled) AND run_id = :run_id AND execution_id = :writer AND "
        "attribute_not_exists(current_attempt_id) AND next_ordinal = :ordinal AND "
        "accepted_attempt_reservations = :count"
    )
    assert run["ExpressionAttributeValues"][":new_count"] == {"N": "1"}


def test_no_begin_occurs_at_or_beyond_either_reservation_cap() -> None:
    with pytest.raises(ValueError, match="10-reservation"):
        _begin_inline_request(
            owner=BeginInlineRunOwner(
                InlineRunAttemptOwner(TABLE_ID, RUN_ID, _run_item(accepted_attempt_reservations=10))
            )
        )
    with pytest.raises(ValueError, match="10-reservation"):
        BeginQueuedUnitOwner(
            QueuedUnitAttemptOwner(PLAN_KEY, TABLE_ID, _unit_item(unit_reservation_count=10)),
            _meta_item(),
        )
    with pytest.raises(ValueError, match="32-reservation"):
        BeginQueuedUnitOwner(
            QueuedUnitAttemptOwner(PLAN_KEY, TABLE_ID, _unit_item()),
            _meta_item(plan_reservation_count=32),
        )


def test_begin_rejects_an_expired_lease_before_any_write() -> None:
    late = f"{EXPIRY_MICROS:020d}"
    with pytest.raises(ValueError, match="unexpired"):
        _begin_inline_request(operation_instant=late)


def test_begin_requires_full_admission_to_prove_the_origin_intent() -> None:
    with pytest.raises(ValueError, match="admitted"):
        BeginQueuedUnitOwner(
            QueuedUnitAttemptOwner(PLAN_KEY, TABLE_ID, _unit_item()),
            _meta_item(admitted_count=1),
        )


def test_exact_begin_replay_observes_the_occupied_slot_and_increments_nothing() -> None:
    dynamo = _RecordingDynamo()
    _seed(dynamo, TABLE, _run_item())
    coordinator = _coordinator(dynamo)
    coordinator.begin_table_attempt(_begin_inline_request())
    applied = dict(dynamo.items)

    retry = _RecordingDynamo(items=applied)
    retry.fail_next_transactions = 1
    result = _coordinator(retry).begin_table_attempt(_begin_inline_request())

    assert result.outcome is BeginTableAttemptOutcome.REPLAYED
    stored_run = retry.items[(TABLE, PARTITION, f"RUN#{RUN_ID}")]
    assert stored_run["accepted_attempt_reservations"] == {"N": "1"}


def test_a_lost_begin_condition_with_a_different_slot_is_a_typed_conflict() -> None:
    dynamo = _RecordingDynamo()
    dynamo.fail_next_transactions = 1
    with pytest.raises(DomainError, match="reclassify"):
        _coordinator(dynamo).begin_table_attempt(_begin_inline_request())


# --- RecordStage1RefusalRequest ---------------------------------------------------------------


def _unit_refusal_request(
    reason: Stage1RefusalReason = Stage1RefusalReason.TABLE_BUSY,
) -> RecordStage1RefusalRequest:
    return RecordStage1RefusalRequest(
        tag=Stage1RefusalTag.UNIT,
        reason=reason,
        refusal_instant=INSTANT,
        proof=_unit_proof("semantic_unit_refused", reason.value.lower()),
        unit=RefusedUnitObservation(
            plan_digest=PLAN_DIGEST,
            plan_key=PLAN_KEY,
            intent=_admitted_intent(),
            owner=_active_owner(),
            writer_execution_id=WRITER_EXEC,
            unit_item=_unit_item(),
            meta_item=_meta_item(),
        ),
    )


def _inline_refusal_request() -> RecordStage1RefusalRequest:
    return RecordStage1RefusalRequest(
        tag=Stage1RefusalTag.INLINE,
        reason=Stage1RefusalReason.TABLE_ATTEMPT_PENDING,
        refusal_instant=INSTANT,
        proof=_inline_proof("semantic_inline_run_refused", typed_reason="table_attempt_pending"),
        inline=RefusedInlineRunObservation(InlineRunAttemptOwner(TABLE_ID, RUN_ID, _run_item())),
    )


def test_the_unit_refusal_conditions_owner_items_and_puts_only_the_event_and_head() -> None:
    dynamo = _RecordingDynamo()
    result = _coordinator(dynamo).record_stage1_refusal(_unit_refusal_request())

    assert result.outcome is RecordStage1RefusalOutcome.RECORDED
    members = _members(dynamo)
    # Grain: three condition-only owner members plus the event and head puts.
    assert _kinds(members) == ["ConditionCheck", "ConditionCheck", "ConditionCheck", "Put", "Put"]
    unit = members[0]["ConditionCheck"]
    assert unit["ConditionExpression"] == ("#status = :writing AND writer_execution_id = :writer")
    intent = members[2]["ConditionCheck"]
    assert intent["ConditionExpression"] == "#state = :admitted AND intent_id = :intent"


def test_the_inline_refusal_conditions_the_exact_open_or_reconciled_run() -> None:
    dynamo = _RecordingDynamo()
    _coordinator(dynamo).record_stage1_refusal(_inline_refusal_request())

    members = _members(dynamo)
    assert _kinds(members) == ["ConditionCheck", "Put", "Put"]
    run = members[0]["ConditionCheck"]
    assert run["ConditionExpression"] == (
        "run_id = :run_id AND #state IN (:open, :reconciled) AND "
        "attribute_not_exists(current_attempt_id)"
    )


def test_a_refusal_proof_with_the_wrong_typed_reason_is_rejected() -> None:
    with pytest.raises(ValueError, match="typed reason"):
        RecordStage1RefusalRequest(
            tag=Stage1RefusalTag.UNIT,
            reason=Stage1RefusalReason.TABLE_ATTEMPT_PENDING,
            refusal_instant=INSTANT,
            proof=_unit_proof("semantic_unit_refused", "table_busy"),
            unit=_unit_refusal_request().unit,
        )


def test_a_byte_identical_refusal_event_replay_is_a_no_op() -> None:
    dynamo = _RecordingDynamo()
    coordinator = _coordinator(dynamo)
    coordinator.record_stage1_refusal(_inline_refusal_request())

    retry = _RecordingDynamo(items=dict(dynamo.items))
    retry.fail_next_transactions = 1
    result = _coordinator(retry).record_stage1_refusal(_inline_refusal_request())
    assert result.outcome is RecordStage1RefusalOutcome.REPLAYED


def test_a_lost_refusal_condition_without_the_durable_event_conflicts() -> None:
    dynamo = _RecordingDynamo()
    dynamo.fail_next_transactions = 1
    with pytest.raises(DomainError, match="never rebuilt"):
        _coordinator(dynamo).record_stage1_refusal(_inline_refusal_request())


# --- PersistInlineRunRequest ------------------------------------------------------------------


def _persist_run_request(**overrides: object) -> PersistInlineRunRequest:
    fields: dict[str, object] = {
        "writer_kind": WRITER,
        "operation_id": OPERATION_ID,
        "execution_id": EXECUTION_ID,
        "physical_table_id": TABLE_ID,
        "table_kind": "current",
        "statement_intent_kinds": ("merge", "verify"),
        "pointer_finalization_payload": None,
        "created_at": INSTANT,
        "proof": RECORDED_PROOF,
    }
    fields.update(overrides)
    return PersistInlineRunRequest(**fields)  # type: ignore[arg-type]


def test_persist_inline_run_creates_the_run_event_and_head_atomically() -> None:
    dynamo = _RecordingDynamo()
    result = _coordinator(dynamo).persist_inline_run(_persist_run_request())

    assert result.outcome is PersistInlineRunOutcome.PERSISTED
    assert result.run_id == RUN_ID
    members = _members(dynamo)
    assert _kinds(members) == ["Put", "Put", "Put"]
    run = members[0]["Put"]
    assert run["ConditionExpression"] == "attribute_not_exists(partition_key)"
    item = run["Item"]
    assert item["record_key"] == {"S": f"RUN#{RUN_ID}"}
    assert item["state"] == {"S": "open"}
    assert item["next_ordinal"] == {"N": "1"}
    assert item["accepted_attempt_reservations"] == {"N": "0"}
    # Public bindings come only from the linked event-projection proof.
    assert item["status_flow_binding"] == {"S": RECORDED_PROOF.flow_id}
    assert item["execution_binding"] == {"S": RECORDED_PROOF.execution_id}


def test_persist_inline_run_rejects_a_proof_over_a_different_run_tuple() -> None:
    with pytest.raises(ValueError, match="immutable run tuple"):
        _persist_run_request(operation_id="op-fixture-0002")


def test_a_byte_identical_run_and_event_replay_is_a_no_op() -> None:
    dynamo = _RecordingDynamo()
    _coordinator(dynamo).persist_inline_run(_persist_run_request())

    retry = _RecordingDynamo(items=dict(dynamo.items))
    retry.fail_next_transactions = 1
    result = _coordinator(retry).persist_inline_run(_persist_run_request())
    assert result.outcome is PersistInlineRunOutcome.REPLAYED


def test_an_occupied_run_key_with_changed_identity_is_an_invariant_collision() -> None:
    dynamo = _RecordingDynamo()
    _coordinator(dynamo).persist_inline_run(_persist_run_request())

    retry = _RecordingDynamo(items=dict(dynamo.items))
    retry.fail_next_transactions = 1
    with pytest.raises(InvariantError, match="immutable run identity"):
        _coordinator(retry).persist_inline_run(
            _persist_run_request(statement_intent_kinds=("merge",))
        )


# --- RecoverParkedAttemptSubmissionRequest ----------------------------------------------------


def _parked_pair() -> tuple[FenceObservation, AttemptObservation]:
    fence = _fence("parked", parked_at=INSTANT)
    attempt = _attempt(
        "parked",
        parked_at=INSTANT,
        query_execution_id=None,
        submitted_at=None,
        sanitized_start_evidence=None,
    )
    return fence, attempt


def _recover_request(**overrides: object) -> RecoverParkedAttemptSubmissionRequest:
    fence, attempt = _parked_pair()
    fields: dict[str, object] = {
        "physical_table_id": TABLE_ID,
        "fence": fence,
        "attempt": attempt,
        "query_execution_id": QUERY_ID,
        "submitted_at": INSTANT,
        "sanitized_start_evidence": EVIDENCE,
    }
    fields.update(overrides)
    return RecoverParkedAttemptSubmissionRequest(**fields)  # type: ignore[arg-type]


def test_recovery_condition_checks_the_complete_parked_fence_snapshot() -> None:
    dynamo = _RecordingDynamo()
    result = _coordinator(dynamo).recover_parked_attempt_submission(_recover_request())

    assert result.outcome is RecoverParkedAttemptSubmissionOutcome.RECOVERED
    members = _members(dynamo)
    assert _kinds(members) == ["ConditionCheck", "Update"]
    fence = members[0]["ConditionCheck"]
    named = set(cast("dict[str, str]", fence["ExpressionAttributeNames"]).values())
    # The complete snapshot: safety binding, park instant, and the pending tuple.
    assert named == {
        "attempt_id",
        "fencing_token",
        "lease_holder",
        "parked_at",
        "physical_table_id",
        "reconcile_after_or_block",
        "reconcile_attempt_count",
        "reconcile_status",
        "state",
    }
    attempt = members[1]["Update"]
    assert attempt["UpdateExpression"] == (
        "SET query_execution_id = :query, submitted_at = :submitted_at, "
        "sanitized_start_evidence = :evidence"
    )
    assert "attribute_not_exists(#a0)" in attempt["ConditionExpression"]


def test_recovery_rejects_a_changed_recovered_identity_before_any_write() -> None:
    fence, _ = _parked_pair()
    durable = _attempt(
        "parked",
        parked_at=INSTANT,
        query_execution_id=QUERY_ID,
        submitted_at=INSTANT,
        sanitized_start_evidence=EVIDENCE,
    )
    with pytest.raises(ValueError, match="identical recovered bytes"):
        _recover_request(
            fence=fence, attempt=durable, query_execution_id="athena-query-fixture-0002"
        )


def test_an_identical_durable_triple_replay_is_a_no_op() -> None:
    dynamo = _RecordingDynamo()
    fence, attempt = _parked_pair()
    _seed(dynamo, TABLE, _fence_item(fence))
    _seed(dynamo, TABLE, _attempt_item(attempt))
    _coordinator(dynamo).recover_parked_attempt_submission(_recover_request())

    retry = _RecordingDynamo(items=dict(dynamo.items))
    retry.fail_next_transactions = 1
    result = _coordinator(retry).recover_parked_attempt_submission(_recover_request())
    assert result.outcome is RecoverParkedAttemptSubmissionOutcome.REPLAYED


def test_a_fence_only_race_aborts_recovery_as_a_typed_conflict() -> None:
    dynamo = _RecordingDynamo()
    parked_item = {
        "partition_key": PARTITION,
        "record_key": f"ATTEMPT#{ATTEMPT_ID}",
        "state": "parked",
    }
    _seed(dynamo, TABLE, parked_item)
    dynamo.fail_next_transactions = 1
    with pytest.raises(DomainError, match="fence-only"):
        _coordinator(dynamo).recover_parked_attempt_submission(_recover_request())


def test_a_changed_durable_submission_is_an_invariant_collision() -> None:
    dynamo = _RecordingDynamo()
    stored = {
        "partition_key": PARTITION,
        "record_key": f"ATTEMPT#{ATTEMPT_ID}",
        "state": "parked",
        "query_execution_id": "athena-query-fixture-0002",
        "submitted_at": INSTANT,
    }
    _seed(dynamo, TABLE, stored)
    dynamo.fail_next_transactions = 1
    with pytest.raises(InvariantError, match="disagrees with the recovered"):
        _coordinator(dynamo).recover_parked_attempt_submission(_recover_request())


# --- RequestAttemptCancellationRequest --------------------------------------------------------


def _cancel_request(**overrides: object) -> RequestAttemptCancellationRequest:
    fields: dict[str, object] = {
        "physical_table_id": TABLE_ID,
        "fence": _fence(),
        "attempt": _attempt("submitted"),
        "cancel_requested_at": INSTANT,
        "owner": InlineRunAttemptOwner(TABLE_ID, RUN_ID, _bound_run_item()),
    }
    fields.update(overrides)
    return RequestAttemptCancellationRequest(**fields)  # type: ignore[arg-type]


def test_cancellation_changes_submitted_to_cancel_requested_write_once() -> None:
    dynamo = _RecordingDynamo()
    result = _coordinator(dynamo).request_attempt_cancellation(_cancel_request())

    assert result.outcome is RequestAttemptCancellationOutcome.REQUESTED
    members = _members(dynamo)
    assert _kinds(members) == ["Update", "ConditionCheck", "ConditionCheck"]
    attempt = members[0]["Update"]
    assert attempt["UpdateExpression"] == (
        "SET #state = :cancel_state, cancel_requested_at = :instant"
    )
    assert "attribute_not_exists(#a0)" in attempt["ConditionExpression"]
    owner = members[2]["ConditionCheck"]
    assert owner["ConditionExpression"] == "#state = :observed AND current_attempt_id = :attempt"


def test_a_parked_cancellation_keeps_state_parked() -> None:
    parked = _attempt(
        "parked",
        parked_at=INSTANT,
        query_execution_id=QUERY_ID,
        submitted_at=INSTANT,
        sanitized_start_evidence=EVIDENCE,
    )
    dynamo = _RecordingDynamo()
    _coordinator(dynamo).request_attempt_cancellation(
        _cancel_request(fence=_fence("parked", parked_at=INSTANT), attempt=parked)
    )
    update = _members(dynamo)[0]["Update"]
    assert update["UpdateExpression"] == "SET cancel_requested_at = :instant"


def test_an_intent_attempt_cannot_be_cancelled() -> None:
    intent = _attempt(
        "intent",
        query_execution_id=None,
        submitted_at=None,
        sanitized_start_evidence=None,
    )
    with pytest.raises(ValueError, match="acceptance is ambiguous"):
        _cancel_request(attempt=intent)


def test_a_parked_cancellation_without_the_durable_identity_is_rejected() -> None:
    bare = _attempt(
        "parked",
        parked_at=INSTANT,
        query_execution_id=None,
        submitted_at=None,
        sanitized_start_evidence=None,
    )
    with pytest.raises(ValueError, match="durable valid query identity"):
        _cancel_request(fence=_fence("parked", parked_at=INSTANT), attempt=bare)


def test_the_inline_tagged_cancellation_puts_only_the_registered_event() -> None:
    proof = _inline_proof("semantic_inline_run_cancel_requested")
    dynamo = _RecordingDynamo()
    _coordinator(dynamo).request_attempt_cancellation(_cancel_request(proof=proof))
    assert _kinds(_members(dynamo)) == ["Update", "ConditionCheck", "Update", "Put", "Put"]


def test_the_inline_tagged_cancellation_updates_the_run_against_its_exact_snapshot() -> None:
    """§3.2: the run changes open|reconciled to cancel_requested, so no further Begin."""
    proof = _inline_proof("semantic_inline_run_cancel_requested")
    dynamo = _RecordingDynamo()
    _coordinator(dynamo).request_attempt_cancellation(_cancel_request(proof=proof))

    run = _members(dynamo)[2]["Update"]
    assert run["UpdateExpression"] == "SET #state = :cancel_state"
    assert run["ExpressionAttributeValues"][":cancel_state"] == {"S": "cancel_requested"}
    # The condition is the complete exact observed open snapshot, nothing weaker.
    names = cast("dict[str, str]", run["ExpressionAttributeNames"])
    conditioned = {names[alias] for alias in names if alias.startswith("#x")}
    assert conditioned == set(_bound_run_item()) - {"partition_key", "record_key"}
    state_alias = next(
        alias for alias, name in names.items() if alias.startswith("#x") and name == "state"
    )
    assert f"{state_alias} = {state_alias.replace('#', ':')}" in run["ConditionExpression"]
    assert run["ExpressionAttributeValues"][state_alias.replace("#", ":")] == {"S": "open"}


def test_the_inline_tagged_cancellation_rejects_a_run_already_cancel_requested() -> None:
    proof = _inline_proof("semantic_inline_run_cancel_requested")
    with pytest.raises(ValueError, match="open or reconciled"):
        _cancel_request(
            owner=InlineRunAttemptOwner(
                TABLE_ID, RUN_ID, _bound_run_item(state="cancel_requested")
            ),
            proof=proof,
        )


def test_the_inline_tagged_cancellation_rejects_a_terminal_run() -> None:
    proof = _inline_proof("semantic_inline_run_cancel_requested")
    with pytest.raises(ValueError, match="nonterminal"):
        _cancel_request(
            owner=InlineRunAttemptOwner(TABLE_ID, RUN_ID, _bound_run_item(state="terminal")),
            proof=proof,
        )


def test_a_queued_cancellation_never_invents_a_lifecycle_event() -> None:
    proof = _inline_proof("semantic_inline_run_cancel_requested")
    with pytest.raises(ValueError, match="inline tagged variant"):
        _cancel_request(
            fence=_fence(),
            attempt=_queued_attempt("submitted"),
            owner=QueuedUnitAttemptOwner(PLAN_KEY, TABLE_ID, _bound_unit_item()),
            proof=proof,
        )


def test_exact_cancellation_replay_preserves_the_first_instant() -> None:
    dynamo = _RecordingDynamo()
    _seed(dynamo, TABLE, _attempt_item(_attempt("submitted")))
    _coordinator(dynamo).request_attempt_cancellation(_cancel_request())

    retry = _RecordingDynamo(items=dict(dynamo.items))
    retry.fail_next_transactions = 1
    result = _coordinator(retry).request_attempt_cancellation(_cancel_request())
    assert result.outcome is RequestAttemptCancellationOutcome.REPLAYED


def test_an_exact_tagged_cancellation_replay_is_a_replayed_no_op() -> None:
    """The byte-identical durable post-state includes the cancelled run, not just the attempt."""
    proof = _inline_proof("semantic_inline_run_cancel_requested")
    dynamo = _RecordingDynamo()
    _seed(dynamo, TABLE, _attempt_item(_attempt("submitted")))
    _seed(dynamo, TABLE, _bound_run_item())
    _coordinator(dynamo).request_attempt_cancellation(_cancel_request(proof=proof))

    retry = _RecordingDynamo(items=dict(dynamo.items))
    retry.fail_next_transactions = 1
    result = _coordinator(retry).request_attempt_cancellation(_cancel_request(proof=proof))
    assert result.outcome is RequestAttemptCancellationOutcome.REPLAYED


def test_a_tagged_cancellation_retry_against_an_uncancelled_run_is_a_typed_conflict() -> None:
    """An attempt-only match must not classify as replay once the run transition is owed."""
    proof = _inline_proof("semantic_inline_run_cancel_requested")
    dynamo = _RecordingDynamo()
    _seed(dynamo, TABLE, _attempt_item(_attempt("submitted")))
    _seed(dynamo, TABLE, _bound_run_item())
    _coordinator(dynamo).request_attempt_cancellation(_cancel_request())

    retry = _RecordingDynamo(items=dict(dynamo.items))
    retry.fail_next_transactions = 1
    with pytest.raises(DomainError, match="preserved"):
        _coordinator(retry).request_attempt_cancellation(_cancel_request(proof=proof))


# --- ParkAttemptRequest -----------------------------------------------------------------------


def _park_request(**overrides: object) -> ParkAttemptRequest:
    fields: dict[str, object] = {
        "physical_table_id": TABLE_ID,
        "lease": LEASE,
        "fence": _fence(),
        "attempt": _attempt("submitted"),
        "park_instant": INSTANT,
    }
    fields.update(overrides)
    return ParkAttemptRequest(**fields)  # type: ignore[arg-type]


def test_park_updates_lease_attempt_and_fence_atomically_with_the_exact_backoff() -> None:
    dynamo = _RecordingDynamo()
    result = _coordinator(dynamo).park_attempt(_park_request())

    assert result.outcome is ParkAttemptOutcome.PARKED
    members = _members(dynamo)
    assert _kinds(members) == ["Update", "Update", "Update"]
    lease = members[0]["Update"]
    assert lease["TableName"] == LEASES
    assert lease["UpdateExpression"] == "SET parked_at = :instant"
    fence = members[2]["Update"]
    digest = binding_digest(BindingDigestRequest(TABLE_ID, ATTEMPT_ID, HOLDER, TOKEN))
    # Old count 0 gives exactly a 60-second delay in whole microseconds.
    expected_key = pending_reconcile_key(int(INSTANT) + 60_000_000, digest)
    assert fence["ExpressionAttributeValues"][":new_key"] == {"S": expected_key}
    assert fence["ExpressionAttributeValues"][":new_count"] == {"N": "1"}
    attempt = members[1]["Update"]
    assert attempt["UpdateExpression"] == "SET #state = :parked, parked_at = :instant"


def test_an_exact_parked_replay_preserves_the_original_bytes_without_a_write() -> None:
    request = _park_request(
        lease=replace(LEASE, parked_at=INSTANT),
        fence=_fence("parked", parked_at=INSTANT, reconciliation=_pending_reconciliation(count=1)),
        attempt=_attempt(
            "parked",
            parked_at=INSTANT,
            query_execution_id=None,
            submitted_at=None,
            sanitized_start_evidence=None,
        ),
    )
    dynamo = _RecordingDynamo()
    result = _coordinator(dynamo).park_attempt(request)
    assert result.outcome is ParkAttemptOutcome.REPLAYED
    assert dynamo.transacts == []


def test_a_parked_replay_with_a_different_instant_is_rejected() -> None:
    other = f"{int(INSTANT) + 1:020d}"
    with pytest.raises(ValueError, match="original park instant"):
        _park_request(
            lease=replace(LEASE, parked_at=other),
            fence=_fence("parked", parked_at=other),
            attempt=_attempt(
                "parked",
                parked_at=other,
                query_execution_id=None,
                submitted_at=None,
                sanitized_start_evidence=None,
            ),
            park_instant=INSTANT,
        )


def test_a_lost_park_condition_is_a_typed_conflict() -> None:
    dynamo = _RecordingDynamo()
    dynamo.fail_next_transactions = 1
    with pytest.raises(DomainError, match="reclassify"):
        _coordinator(dynamo).park_attempt(_park_request())


# --- CompleteTableAttemptRequest --------------------------------------------------------------


def _complete_request(**overrides: object) -> CompleteTableAttemptRequest:
    fields: dict[str, object] = {
        "physical_table_id": TABLE_ID,
        "fence": _fence(),
        "attempt": _attempt("submitted"),
        "provider_state": ProviderTerminalState.SUCCEEDED,
        "terminal_at": INSTANT,
        "owner": InlineRunAttemptOwner(TABLE_ID, RUN_ID, _bound_run_item()),
        "query_count": 1,
        "scanned_bytes": 1024,
        "engine_ms": 250,
    }
    fields.update(overrides)
    return CompleteTableAttemptRequest(**fields)  # type: ignore[arg-type]


def test_completion_records_the_terminal_clears_the_fence_and_advances_the_run() -> None:
    dynamo = _RecordingDynamo()
    result = _coordinator(dynamo).complete_table_attempt(_complete_request())

    assert result.outcome is CompleteTableAttemptOutcome.COMPLETED
    members = _members(dynamo)
    assert _kinds(members) == ["Update", "Update", "Update"]
    attempt = members[0]["Update"]
    assert attempt["UpdateExpression"] == (
        "SET #state = :terminal, terminal_at = :terminal_at, athena_usage_state = :usage, "
        "query_count = :query_count, scanned_bytes = :scanned_bytes, engine_ms = :engine_ms"
    )
    assert attempt["ExpressionAttributeValues"][":usage"] == {"S": "unsettled"}
    fence = members[1]["Update"]
    assert fence["UpdateExpression"] == (
        "SET #state = :clear REMOVE attempt_id, fencing_token, lease_holder, "
        "reconcile_after_or_block, reconcile_attempt_count, reconcile_status"
    )
    run = members[2]["Update"]
    assert run["UpdateExpression"] == (
        "SET #state = :resulting, next_ordinal = :new_ordinal, "
        "attempt_references = :references REMOVE current_attempt_id"
    )
    assert run["ExpressionAttributeValues"][":resulting"] == {"S": "open"}
    assert run["ExpressionAttributeValues"][":new_ordinal"] == {"N": "2"}
    # Permanent reservation counters never decrement: the count is condition-only.
    assert ":count" in run["ExpressionAttributeValues"]
    assert "accepted_attempt_reservations" not in run["UpdateExpression"]


def test_missing_quantities_stay_absent_rather_than_becoming_zero() -> None:
    dynamo = _RecordingDynamo()
    _coordinator(dynamo).complete_table_attempt(
        _complete_request(
            provider_state=ProviderTerminalState.CANCELLED,
            query_count=None,
            scanned_bytes=None,
            engine_ms=None,
        )
    )
    update = _members(dynamo)[0]["Update"]["UpdateExpression"]
    assert "query_count" not in update
    assert "scanned_bytes" not in update
    assert "engine_ms" not in update


def test_the_reconciler_variant_puts_the_reconciled_event_and_resumes_reconciled() -> None:
    proof = _inline_proof("semantic_inline_run_reconciled", typed_reason="SUCCEEDED")
    dynamo = _RecordingDynamo()
    _coordinator(dynamo).complete_table_attempt(_complete_request(proof=proof))

    members = _members(dynamo)
    assert _kinds(members) == ["Update", "Update", "Update", "Put", "Put"]
    assert members[2]["Update"]["ExpressionAttributeValues"][":resulting"] == {"S": "reconciled"}


def test_a_queued_completion_advances_the_unit_slot_without_any_event() -> None:
    dynamo = _RecordingDynamo()
    _coordinator(dynamo).complete_table_attempt(
        _complete_request(
            attempt=_queued_attempt("submitted"),
            owner=QueuedUnitAttemptOwner(PLAN_KEY, TABLE_ID, _bound_unit_item()),
        )
    )
    members = _members(dynamo)
    assert _kinds(members) == ["Update", "Update", "Update"]
    unit = members[2]["Update"]
    assert unit["ConditionExpression"] == (
        "#status = :writing AND current_attempt_id = :attempt AND "
        "reserved_slot_identity = :slot AND unit_reservation_count = :count"
    )
    assert unit["UpdateExpression"] == (
        "SET ordinal = :new_ordinal, attempt_references = :references REMOVE current_attempt_id"
    )


def test_completion_requires_a_durable_valid_query_identity() -> None:
    intent = _attempt(
        "intent",
        query_execution_id=None,
        submitted_at=None,
        sanitized_start_evidence=None,
    )
    with pytest.raises(ValueError, match="beyond intent"):
        _complete_request(attempt=intent)


def test_a_queued_completion_rejects_any_lifecycle_event() -> None:
    proof = _inline_proof("semantic_inline_run_reconciled", typed_reason="SUCCEEDED")
    with pytest.raises(ValueError, match="no lifecycle event"):
        _complete_request(
            attempt=_queued_attempt("submitted"),
            owner=QueuedUnitAttemptOwner(PLAN_KEY, TABLE_ID, _bound_unit_item()),
            proof=proof,
        )


def test_exact_completion_replay_is_a_no_op() -> None:
    dynamo = _RecordingDynamo()
    _seed(dynamo, TABLE, _attempt_item(_attempt("submitted")))
    _seed(dynamo, TABLE, _fence_item(_fence()))
    _seed(dynamo, TABLE, _bound_run_item())
    _coordinator(dynamo).complete_table_attempt(_complete_request())

    retry = _RecordingDynamo(items=dict(dynamo.items))
    retry.fail_next_transactions = 1
    result = _coordinator(retry).complete_table_attempt(_complete_request())
    assert result.outcome is CompleteTableAttemptOutcome.REPLAYED


def test_a_lost_completion_condition_with_a_different_state_is_a_conflict() -> None:
    dynamo = _RecordingDynamo()
    dynamo.fail_next_transactions = 1
    with pytest.raises(DomainError, match="reclassify"):
        _coordinator(dynamo).complete_table_attempt(_complete_request())


# --- Member-matrix and cap enforcement --------------------------------------------------------


def test_every_operation_measures_its_transaction_with_the_byte_oracle() -> None:
    """An oversized member refuses before mutation with no split fallback."""
    oversized = _persist_run_request(pointer_finalization_payload="x" * 400_001)
    dynamo = _RecordingDynamo()
    with pytest.raises(ValidationError, match="400000"):
        _coordinator(dynamo).persist_inline_run(oversized)
    assert dynamo.transacts == []


# --- Committed §3.2 byte/action fixtures ------------------------------------------------------

ORACLE = Stage1DynamoByteOracle()
RECOVER_OPERATION = "RecoverParkedAttemptSubmissionRequest"


@pytest.fixture(scope="module")
def recover_fixtures() -> dict[str, dict[str, Any]]:
    return load_fixture_tree()[RECOVER_OPERATION]


def _fixture_recover_request(fixture: dict[str, Any]) -> RecoverParkedAttemptSubmissionRequest:
    typed = cast("dict[str, Any]", fixture["typed_input"])
    members = cast("list[dict[str, Any]]", fixture["expected_request"]["TransactItems"])
    targets = cast("list[dict[str, Any]]", fixture["targets"])
    fence_item = _plain_item(targets[0]["expected_pre_state_map"])
    attempt_item = _plain_item(targets[1]["expected_pre_state_map"])
    del members  # the builder must re-derive the request; only pre-states are inputs
    fence = FenceObservation(
        partition_key=cast("str", fence_item["partition_key"]),
        record_key="FENCE",
        state=cast("str", fence_item["state"]),
        physical_table_id=cast("str", fence_item["physical_table_id"]),
        attempt_id=cast("str", fence_item["attempt_id"]),
        lease_holder=cast("str", fence_item["lease_holder"]),
        fencing_token=cast("int", fence_item["fencing_token"]),
        parked_at=cast("str", fence_item["parked_at"]),
        reconciliation=ReconciliationObservation(
            reconcile_status=cast("str", fence_item["reconcile_status"]),
            reconcile_after_or_block=cast("str", fence_item["reconcile_after_or_block"]),
            reconcile_attempt_count=cast("int", fence_item["reconcile_attempt_count"]),
        ),
    )
    attempt = AttemptObservation(
        partition_key=cast("str", attempt_item["partition_key"]),
        record_key=cast("str", attempt_item["record_key"]),
        state="parked",
        physical_table_id=cast("str", attempt_item["physical_table_id"]),
        attempt_id=cast("str", attempt_item["attempt_id"]),
        plan_unit_owner_ref=None,
        inline_run_owner_ref=cast("str", attempt_item["inline_run_owner_ref"]),
        ordinal=cast("int", attempt_item["ordinal"]),
        statement_kind=cast("str", attempt_item["statement_kind"]),
        lease_holder=cast("str", attempt_item["lease_holder"]),
        fencing_token=cast("int", attempt_item["fencing_token"]),
        writer_execution_id=cast("str", attempt_item["writer_execution_id"]),
        start_envelope=cast("str", attempt_item["start_envelope"]),
        client_request_token=cast("str", attempt_item["client_request_token"]),
        envelope_digest=cast("str", attempt_item["envelope_digest"]),
        reservation_identity=cast("str", attempt_item["reservation_identity"]),
        parked_at=cast("str", attempt_item["parked_at"]),
    )
    return RecoverParkedAttemptSubmissionRequest(
        physical_table_id=cast("str", typed["physical_table_id"]),
        fence=fence,
        attempt=attempt,
        query_execution_id=cast("str", typed["query_execution_id"]),
        submitted_at=cast("str", typed["submitted_at"]),
        sanitized_start_evidence=EVIDENCE,
    )


def _fixture_recover_actions(fixture: dict[str, Any]) -> TransactionEnvelopeRequest:
    """Rebuild the oracle actions from the frozen maps alone."""
    targets = cast("list[dict[str, Any]]", fixture["targets"])
    fence_pre = _plain_item(targets[0]["expected_pre_state_map"])
    attempt_pre = _plain_item(targets[1]["expected_pre_state_map"])
    attempt_result = _plain_item(targets[1]["expected_result_map"])
    set_attributes = {
        name: value for name, value in attempt_result.items() if attempt_pre.get(name) != value
    }
    return TransactionEnvelopeRequest(
        actions=(
            ConditionCheckAction(
                table_name=cast("str", fixture["table_name"]),
                key={
                    "partition_key": fence_pre["partition_key"],
                    "record_key": fence_pre["record_key"],
                },
                pre_state=ExactPreState(fence_pre),
            ),
            UpdateAction(
                table_name=cast("str", fixture["table_name"]),
                key={
                    "partition_key": attempt_pre["partition_key"],
                    "record_key": attempt_pre["record_key"],
                },
                set_attributes=set_attributes,
                pre_state=ExactPreState(attempt_pre),
            ),
        )
    )


def test_the_maximum_recovery_fixture_matches_the_production_builder_and_oracle(
    recover_fixtures: dict[str, dict[str, Any]],
) -> None:
    fixture = recover_fixtures["maximum"]
    dynamo = _RecordingDynamo()
    _coordinator(dynamo).recover_parked_attempt_submission(_fixture_recover_request(fixture))

    assert dynamo.transacts[0] == fixture["expected_request"]
    assert ORACLE.wire_json_bytes(dynamo.transacts[0]) == fixture["expected"]["wire_json_bytes"]
    accepted = ORACLE.validate_envelope(_fixture_recover_actions(fixture))
    assert accepted.action_count == fixture["expected"]["action_count"]
    assert accepted.aggregate_upper_bound == fixture["expected"]["aggregate_upper_bound"]
    assert accepted.targets[1].result_bytes == 400_000


def test_the_one_beyond_recovery_fixture_is_refused_by_the_production_oracle(
    recover_fixtures: dict[str, dict[str, Any]],
) -> None:
    """One conservative item byte beyond 400000 refuses before mutation, never splits."""
    fixture = recover_fixtures["one-beyond"]
    assert fixture["expected"]["outcome"] == "refused"
    assert fixture["targets"][1]["result_conservative_bytes"] == 400_001
    with pytest.raises(ValidationError):
        ORACLE.validate_envelope(_fixture_recover_actions(fixture))
    dynamo = _RecordingDynamo()
    with pytest.raises(ValidationError):
        _coordinator(dynamo).recover_parked_attempt_submission(_fixture_recover_request(fixture))
    assert dynamo.transacts == []


# --- S3: the graph-envelope Begin is fenced on the claim lease version (V-9) ----------------------

_GRAPH_TABLE = "execution-graph-fixture"
_CLAIM_ITEM: dict[str, object] = {
    "pk": "EXEC_CLAIM#" + "n" * 64,
    "sk": "CLAIM",
    "lease_version": 1,
    "run_id": "r" * 64,
}


def _fenced_coordinator(dynamo: _RecordingDynamo) -> DynamoProjectionCoordinator:
    head_store = DynamoStage1LifecycleHeadStore(TABLE, cast("Any", dynamo))
    return DynamoProjectionCoordinator(
        TABLE, LEASES, head_store, cast("Any", dynamo), graph_table=_GRAPH_TABLE
    )


def _fenced_begin() -> BeginTableAttemptRequest:
    return replace(
        _begin_queued_request(), graph_claims=(GraphClaimFence("n" * 64, 1, _CLAIM_ITEM),)
    )


def test_v9_a_graph_begin_conditions_on_the_claim_lease_and_records_the_attempt() -> None:
    dynamo = _RecordingDynamo()
    recorded: list[dict[str, Any]] = []
    dynamo.transact_write_items = lambda **request: recorded.append(request) or {}  # type: ignore[method-assign]
    result = _fenced_coordinator(dynamo).begin_table_attempt(_fenced_begin())
    assert result.outcome is BeginTableAttemptOutcome.BEGUN
    members = recorded[-1]["TransactItems"]
    fence = members[-1]["Update"]
    assert fence["TableName"] == _GRAPH_TABLE
    assert fence["Key"] == {"pk": {"S": "EXEC_CLAIM#" + "n" * 64}, "sk": {"S": "CLAIM"}}
    assert fence["ConditionExpression"] == "lease_version = :version"
    assert fence["ExpressionAttributeValues"][":attempt_id"] == {"S": result.attempt_id}
    assert fence["ExpressionAttributeValues"][":table_id"] == {"S": TABLE_ID}


def test_v9_after_a_takeover_bumped_the_lease_the_first_invocations_begin_is_refused() -> None:
    dynamo = _RecordingDynamo()

    def cancelled(**request: Any) -> dict[str, object]:
        count = len(request["TransactItems"])
        reasons = [{"Code": "None"}] * (count - 1) + [{"Code": "ConditionalCheckFailed"}]
        response: Any = {
            "Error": {"Code": "TransactionCanceledException", "Message": "fixture"},
            "CancellationReasons": reasons,
        }
        raise ClientError(response, "TransactWriteItems")

    dynamo.transact_write_items = cancelled  # type: ignore[method-assign]
    with pytest.raises(GraphAuthorityRefusedError) as refused:
        _fenced_coordinator(dynamo).begin_table_attempt(_fenced_begin())
    assert refused.value.signal == "EXECUTION_SUPERSEDED"
    assert dynamo.items == {}


def _claim_item(node: str) -> dict[str, object]:
    return {**_CLAIM_ITEM, "pk": "EXEC_CLAIM#" + node * 64}


def test_slice7_12f_a_begin_refuses_a_repeated_node_fence_and_writes_one_per_member() -> None:
    fence = GraphClaimFence("n" * 64, 1, _CLAIM_ITEM)
    with pytest.raises(ValueError, match="each node's claim fence once"):
        replace(_begin_queued_request(), graph_claims=(fence, fence))
    # Control: three distinct fences give three claim-fence members, in member order.
    fences = tuple(GraphClaimFence(node * 64, 1, _claim_item(node)) for node in "abc")
    dynamo = _RecordingDynamo()
    recorded: list[dict[str, Any]] = []
    dynamo.transact_write_items = lambda **request: recorded.append(request) or {}  # type: ignore[method-assign]
    _fenced_coordinator(dynamo).begin_table_attempt(
        replace(_begin_queued_request(), graph_claims=fences)
    )
    graph = [
        member["Update"]["Key"]["pk"]["S"]
        for member in recorded[-1]["TransactItems"]
        if "Update" in member and member["Update"]["TableName"] == _GRAPH_TABLE
    ]
    assert graph == ["EXEC_CLAIM#" + node * 64 for node in "abc"]
