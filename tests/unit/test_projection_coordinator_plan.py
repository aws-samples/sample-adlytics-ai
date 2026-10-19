# SPDX-License-Identifier: MIT-0
"""ADR-075 §3.2 plan-intent/plan builders of ``DynamoProjectionCoordinator``."""

from __future__ import annotations

import uuid
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
    PutAction,
    Stage1DynamoByteOracle,
    TransactionEnvelopeRequest,
    UpdateAction,
)
from adapters.outbound.dynamodb.projection_coordinator import (
    DynamoProjectionCoordinator,
    _intent_meta_item,  # pyright: ignore[reportPrivateUsage] -- seeding the recording fake
    _low_item,  # pyright: ignore[reportPrivateUsage] -- seeding the recording fake
    _owner_item,  # pyright: ignore[reportPrivateUsage] -- seeding the recording fake
    _plain_item,  # pyright: ignore[reportPrivateUsage] -- frozen-map reconstruction
    _plan_meta_item,  # pyright: ignore[reportPrivateUsage] -- seeding the recording fake
    _plan_unit_item,  # pyright: ignore[reportPrivateUsage] -- seeding the recording fake
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
    LegitimateRootSource,
    PlanIdentityRequest,
    PlanRequiredChildExpectations,
    PriorOwnerEventSource,
    RootDiscriminator,
    Stage1OwnerTransition,
    Stage1TopologyRequest,
    derive_plan_identity,
)
from domain.operations.stage1_operational_status import (
    RequiredChildExpectation as StatusChildExpectation,
)
from domain.operations.status import FlowState
from domain.operations.write_plan import (
    CanonicalObjectEntry,
    CanonicalUnitEntry,
    IntentMeta,
    IntentOrigin,
    IntentOriginKind,
    IntentState,
    PlanMeta,
    PlanStatus,
    PlanUnit,
    PlanUnitAdmission,
    PlanUnitStatus,
    RequiredChildExpectation,
    SuccessorOwner,
    WritePlanError,
    bootstrap_owner,
    internal_execution_identity,
    plan_digest_for_source_delivery,
    plan_partition_key,
    ready_order_key,
    source_delivery_identity,
)
from domain.shared_kernel.errors import DomainError, InvariantError, ValidationError
from ports.outbound.projection_coordinator import (
    ActivePlanObservation,
    AdmitUnitOutcome,
    AdmitUnitRequest,
    AttestedUnitObservation,
    AuthorityModeGuard,
    ClaimPlanIntentOutcome,
    ClaimPlanIntentRequest,
    CompletePlanOutcome,
    CompletePlanRequest,
    FailAdmissionOutcome,
    FailAdmissionRequest,
    GraphAuthorityRefusedError,
    ModeGuardKind,
    PersistPlanOutcome,
    PersistPlanRequest,
    PlanHeadObservation,
    ReschedulePlanIntentOutcome,
    ReschedulePlanIntentRequest,
    SettledAttemptObservation,
    SubmitPlanIntentOutcome,
    SubmitPlanIntentRequest,
    UnitFlowBinding,
)

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-075")]

# Every identity below is fictional fixture data; none is copied from a live system.

TABLE = "publications-fixture"
ORACLE = Stage1DynamoByteOracle()
NOW = datetime(2026, 9, 19, 12, 34, 56, 789012, tzinfo=UTC)
INSTANT = "01764950400000000000"
CHECKSUM = "0123456789abcdef" * 4
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


UNITS = (_unit("db.alpha", 1), _unit("db.beta", 2))


def _flow(key: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"adlytics-test-unit-flow/{key}"))


def _submit_request(owner: SuccessorOwner | None) -> SubmitPlanIntentRequest:
    return SubmitPlanIntentRequest(
        plan_digest=PLAN_DIGEST,
        root_plan_key=None if owner is None else owner.root_plan_key,
        source_plan_key=None,
        origin=ORIGIN,
        units=UNITS,
        operation_instant=INSTANT,
        owner=owner,
    )


INTENT_ID = _submit_request(None).intent_id()
ROSTER_DIGEST = _submit_request(None).roster_digest()
INTERNAL_EXEC = internal_execution_identity(INTENT_ID, 0)
PLAN_KEY = plan_partition_key(PLAN_DIGEST, INTERNAL_EXEC)
FLOWS = {unit.unit_key: _flow(unit.unit_key) for unit in UNITS}


def _pending_intent() -> IntentMeta:
    return IntentMeta(
        root_plan_key=None,
        source_plan_key=None,
        plan_digest=PLAN_DIGEST,
        intent_id=INTENT_ID,
        roster_digest=ROSTER_DIGEST,
        origin=ORIGIN,
        state=IntentState.PENDING,
        claimed_generation=None,
        retry_count=0,
        due_at=INSTANT,
        ready_status="ready",
        ready_order=ready_order_key(INSTANT, INTENT_ID),
        created_at=INSTANT,
        last_transition_at=INSTANT,
    )


def _claimed_intent() -> IntentMeta:
    return replace(
        _pending_intent(),
        state=IntentState.CLAIMED,
        claimed_generation=0,
        ready_status=None,
        ready_order=None,
    )


def _reserved_owner() -> SuccessorOwner:
    return replace(
        bootstrap_owner(),
        revision=1,
        reserved_intent_id=INTENT_ID,
        reserved_intent_digest=INTENT_ID,
        reserved_generation=0,
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
        latest_internal_execution_id=INTERNAL_EXEC,
    )


def _expectations(flows: dict[str, str], generation: int) -> PlanRequiredChildExpectations:
    parent = derive_plan_identity(PlanIdentityRequest(PLAN_DIGEST, INTERNAL_EXEC)).flow_id
    members = tuple(
        sorted(
            (StatusChildExpectation(flow, generation) for flow in flows.values()),
            key=lambda member: member.child_flow_id.encode("utf-8"),
        )
    )
    return PlanRequiredChildExpectations(parent, members)


def _persist_proof() -> Stage1LifecycleProof:
    return derive_stage1_lifecycle_proof(
        Stage1LifecycleProofRequest(
            identity=PlanIdentityRequest(PLAN_DIGEST, INTERNAL_EXEC),
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
            required_child_expectations=_expectations(FLOWS, 0),
        )
    )


def _successor_proof() -> Stage1LifecycleProof:
    head = _persist_proof().resulting_head
    if head is None:  # pragma: no cover -- plan proofs always carry a head
        raise AssertionError("plan proofs carry a resulting head")
    return derive_stage1_lifecycle_proof(
        Stage1LifecycleProofRequest(
            identity=PlanIdentityRequest(PLAN_DIGEST, INTERNAL_EXEC),
            internal_key=(PLAN_KEY,),
            topology=Stage1TopologyRequest(
                Stage1OwnerTransition.LATER_PLAN_EVENT,
                PriorOwnerEventSource(
                    head.parent_flow_id,
                    head.latest_event_id,
                    stage1_head_authority_document(head),
                ),
            ),
            transition=Stage1LifecycleTransition(
                "semantic_plan_admitted", FlowState.QUEUED, None, None
            ),
            stage="projection_plan_admitted",
            authority_at=NOW,
            prior_head=head,
            clock_started_at=NOW,
            required_child_expectations=_expectations(FLOWS, 0),
        )
    )


def _plan_head_observation() -> PlanHeadObservation:
    proof = _persist_proof()
    head = proof.resulting_head
    if head is None:  # pragma: no cover -- plan proofs always carry a head
        raise AssertionError("plan proofs carry a resulting head")
    return PlanHeadObservation(proof.partition_key, "HEAD", head)


def _plan_meta(admitted: int = 0) -> PlanMeta:
    return PlanMeta(
        plan_digest=PLAN_DIGEST,
        internal_execution_id=INTERNAL_EXEC,
        root_plan_key=PLAN_KEY,
        generation=0,
        source_plan_key=None,
        origin_intent_id=INTENT_ID,
        roster_digest=ROSTER_DIGEST,
        origin=ORIGIN,
        status=PlanStatus.PLANNED,
        unit_count=len(UNITS),
        admitted_count=admitted,
        attested_count=0,
        failed_count=0,
        child_outcome_reduction_version=2,
        required_child_expectations=tuple(
            RequiredChildExpectation(flow, 0)
            for flow in sorted(FLOWS.values(), key=lambda flow: flow.encode("utf-8"))
        ),
        settled_attempt_count=0,
        settled_attempt_digest=None,
        terminal_at=None,
        created_at=INSTANT,
    )


def _plan_unit(key: str) -> PlanUnit:
    return PlanUnit(
        unit=next(unit for unit in UNITS if unit.unit_key == key),
        flow_id=FLOWS[key],
        owner_generation=0,
        status=PlanUnitStatus.QUEUED,
        admission=PlanUnitAdmission.PENDING,
        admission_deduplication_digest=None,
        admitted_at=None,
        created_at=INSTANT,
    )


# --- Recording fake: evaluates nothing, applies writes, and can lose a condition ------------------


@guard_reserved_words
@dataclass
class _RecordingDynamo:
    """Records requests and naively applies writes; a forced failure classifies replay."""

    items: dict[tuple[str, str], dict[str, Any]] = field(
        default_factory=dict[tuple[str, str], dict[str, Any]]
    )
    transacts: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    gets: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    fail_next_transactions: int = 0

    def get_item(self, **request: Any) -> dict[str, object]:
        self.gets.append(request)
        key = cast("dict[str, dict[str, str]]", request["Key"])
        item = self.items.get((key["partition_key"]["S"], key["record_key"]["S"]))
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
            item = cast("dict[str, Any]", member["Put"]["Item"])
            self.items[(item["partition_key"]["S"], item["record_key"]["S"])] = dict(item)
        elif "Update" in member:
            body = cast("dict[str, Any]", member["Update"])
            key = cast("dict[str, dict[str, str]]", body["Key"])
            stored = dict(self.items[(key["partition_key"]["S"], key["record_key"]["S"])])
            names = cast("dict[str, str]", body.get("ExpressionAttributeNames", {}))
            values = cast("dict[str, Any]", body["ExpressionAttributeValues"])
            expression = cast("str", body["UpdateExpression"])
            set_part, _, remove_part = expression.partition(" REMOVE ")
            for assignment in set_part.removeprefix("SET ").split(", "):
                name, _, value = assignment.partition(" = ")
                stored[_alias(names, name)] = values[value.strip()]
            for name in remove_part.split(", ") if remove_part else ():
                stored.pop(_alias(names, name), None)
            self.items[(key["partition_key"]["S"], key["record_key"]["S"])] = stored


def _alias(names: dict[str, str], name: str) -> str:
    text = name.strip()
    return names.get(text, text)


def _coordinator(dynamo: _RecordingDynamo) -> DynamoProjectionCoordinator:
    head_store = DynamoStage1LifecycleHeadStore(TABLE, cast("Any", dynamo))
    return DynamoProjectionCoordinator(TABLE, "leases-fixture", head_store, cast("Any", dynamo))


def _seed(dynamo: _RecordingDynamo, item: dict[str, object]) -> None:
    low = _low_item(item)
    dynamo.items[(cast("str", item["partition_key"]), cast("str", item["record_key"]))] = dict(low)


def _members(dynamo: _RecordingDynamo, index: int = -1) -> list[dict[str, Any]]:
    return cast("list[dict[str, Any]]", dynamo.transacts[index]["TransactItems"])


def _kinds(members: list[dict[str, Any]]) -> list[str]:
    return [next(iter(member)) for member in members]


# --- SubmitPlanIntentRequest ------------------------------------------------------------------


def test_bootstrap_submit_puts_meta_units_and_the_absent_owner_form() -> None:
    dynamo = _RecordingDynamo()
    result = _coordinator(dynamo).submit_plan_intent(_submit_request(None))

    assert result.outcome is SubmitPlanIntentOutcome.SUBMITTED
    assert (result.intent_id, result.roster_digest) == (INTENT_ID, ROSTER_DIGEST)
    members = _members(dynamo)
    # Grain: one transaction; actions are DynamoDB transact members (2 + u for u=2 units).
    assert _kinds(members) == ["Put", "Put", "Put", "Put"]
    for member in members:
        assert member["Put"]["TableName"] == TABLE
        assert member["Put"]["ConditionExpression"] == "attribute_not_exists(partition_key)"
    meta_item = members[0]["Put"]["Item"]
    assert meta_item["state"] == {"S": "pending"}
    assert "claimed_generation" not in meta_item
    assert meta_item["ready_status"] == {"S": "ready"}
    assert meta_item["ready_order"] == {"S": f"{INSTANT}#{INTENT_ID}"}
    owner_item = members[3]["Put"]["Item"]
    assert owner_item["revision"] == {"N": "0"}
    assert owner_item["last_generation"] == {"N": "-1"}


def test_existing_owner_submit_condition_checks_the_exact_revision() -> None:
    dynamo = _RecordingDynamo()
    owner = replace(bootstrap_owner(), revision=6)
    _seed(dynamo, _owner_item(PLAN_DIGEST, owner))
    _coordinator(dynamo).submit_plan_intent(_submit_request(owner))

    check = _members(dynamo)[3]["ConditionCheck"]
    assert check["ConditionExpression"] == "revision = :revision"
    assert check["ExpressionAttributeValues"] == {":revision": {"N": "6"}}
    assert check["Key"]["partition_key"] == {"S": f"WRITE_PLAN#{PLAN_DIGEST}"}
    assert check["Key"]["record_key"] == {"S": "SUCCESSOR_OWNER"}


def test_exact_complete_authority_submit_replay_is_a_no_op() -> None:
    dynamo = _RecordingDynamo()
    coordinator = _coordinator(dynamo)
    coordinator.submit_plan_intent(_submit_request(None))
    snapshot = dict(dynamo.items)

    dynamo.fail_next_transactions = 1
    result = coordinator.submit_plan_intent(_submit_request(None))

    assert result.outcome is SubmitPlanIntentOutcome.REPLAYED
    assert dynamo.items == snapshot


def test_a_durable_intent_with_different_roster_bytes_is_a_collision() -> None:
    dynamo = _RecordingDynamo()
    coordinator = _coordinator(dynamo)
    coordinator.submit_plan_intent(_submit_request(None))
    unit_key = (f"WRITE_PLAN#{PLAN_DIGEST}#INTENT#{INTENT_ID}", "UNIT#db.alpha")
    dynamo.items[unit_key]["merge_count"] = {"N": "9"}

    dynamo.fail_next_transactions = 1
    with pytest.raises(InvariantError):
        coordinator.submit_plan_intent(_submit_request(None))


def test_a_lost_owner_condition_without_a_durable_intent_is_a_conflict() -> None:
    dynamo = _RecordingDynamo()
    dynamo.fail_next_transactions = 1
    with pytest.raises(DomainError):
        _coordinator(dynamo).submit_plan_intent(_submit_request(None))


def test_a_sixteen_unit_roster_is_refused_before_any_client_call() -> None:
    units = tuple(_unit(f"db.table_{index:02d}", index + 1) for index in range(16))
    with pytest.raises(WritePlanError):
        SubmitPlanIntentRequest(
            plan_digest=PLAN_DIGEST,
            root_plan_key=None,
            source_plan_key=None,
            origin=ORIGIN,
            units=units,
            operation_instant=INSTANT,
            owner=None,
        )


# --- ClaimPlanIntentRequest -----------------------------------------------------------------------


def _claim_request() -> ClaimPlanIntentRequest:
    return ClaimPlanIntentRequest(
        plan_digest=PLAN_DIGEST,
        intent=_pending_intent(),
        owner=bootstrap_owner(),
        operation_instant=INSTANT,
        active_plan=None,
        plan_head=None,
    )


def test_claim_cases_the_owner_and_assigns_last_generation_plus_one() -> None:
    dynamo = _RecordingDynamo()
    _seed(dynamo, _owner_item(PLAN_DIGEST, bootstrap_owner()))
    _seed(dynamo, _intent_meta_item(PLAN_DIGEST, _pending_intent()))
    result = _coordinator(dynamo).claim_plan_intent(_claim_request())

    assert result.outcome is ClaimPlanIntentOutcome.CLAIMED
    assert result.claimed_generation == 0  # owner.last_generation (-1) + 1
    members = _members(dynamo)
    assert _kinds(members) == ["Update", "Update"]
    owner_update = members[0]["Update"]
    assert owner_update["ConditionExpression"] == (
        "revision = :revision AND last_generation = :last_generation AND "
        "attribute_not_exists(reserved_intent_id)"
    )
    assert owner_update["UpdateExpression"] == (
        "SET revision = :new_revision, reserved_intent_id = :intent_id, "
        "reserved_intent_digest = :intent_id, reserved_generation = :claimed_generation"
    )
    intent_update = members[1]["Update"]
    assert intent_update["UpdateExpression"] == (
        "SET #state = :claimed, claimed_generation = :claimed_generation, "
        "last_transition_at = :instant REMOVE ready_status, ready_order"
    )
    stored = dynamo.items[(f"WRITE_PLAN#{PLAN_DIGEST}#INTENT#{INTENT_ID}", "META")]
    assert stored["state"] == {"S": "claimed"}
    assert stored["claimed_generation"] == {"N": "0"}
    assert "ready_status" not in stored and "ready_order" not in stored


def test_claim_with_an_active_plan_conditions_the_plan_meta_and_exact_head() -> None:
    dynamo = _RecordingDynamo()
    owner = _active_owner()
    request = ClaimPlanIntentRequest(
        plan_digest=PLAN_DIGEST,
        intent=_pending_intent(),
        owner=owner,
        operation_instant=INSTANT,
        active_plan=ActivePlanObservation(PLAN_KEY, _plan_meta(admitted=1)),
        plan_head=_plan_head_observation(),
    )
    _seed(dynamo, _owner_item(PLAN_DIGEST, owner))
    _seed(dynamo, _intent_meta_item(PLAN_DIGEST, request.intent))
    result = _coordinator(dynamo).claim_plan_intent(request)

    assert result.claimed_generation == 1  # owner.last_generation (0) + 1
    members = _members(dynamo)
    assert _kinds(members) == ["Update", "Update", "ConditionCheck", "ConditionCheck"]
    plan_check = members[2]["ConditionCheck"]
    assert plan_check["Key"]["partition_key"] == {"S": PLAN_KEY}
    assert plan_check["ConditionExpression"] == (
        "#status = :planned AND generation = :generation AND origin_intent_id = :origin_intent_id"
    )
    head_check = members[3]["ConditionCheck"]
    assert head_check["Key"]["record_key"] == {"S": "HEAD"}
    assert head_check["Key"]["partition_key"]["S"].startswith("STATUS_FLOW#")
    assert (
        "head_contract_version = :prior_head_contract_version"
        in (head_check["ConditionExpression"])
    )

    with pytest.raises(ValueError, match="active-plan observation"):
        replace(request, active_plan=None)


def test_claim_replay_returns_the_same_assignment_and_conflict_fails_closed() -> None:
    dynamo = _RecordingDynamo()
    _seed(dynamo, _owner_item(PLAN_DIGEST, bootstrap_owner()))
    _seed(dynamo, _intent_meta_item(PLAN_DIGEST, _pending_intent()))
    coordinator = _coordinator(dynamo)
    coordinator.claim_plan_intent(_claim_request())

    dynamo.fail_next_transactions = 1
    replayed = coordinator.claim_plan_intent(_claim_request())
    assert replayed.outcome is ClaimPlanIntentOutcome.REPLAYED
    assert replayed.claimed_generation == 0

    dynamo.items[(f"WRITE_PLAN#{PLAN_DIGEST}", "SUCCESSOR_OWNER")]["revision"] = {"N": "9"}
    dynamo.fail_next_transactions = 1
    with pytest.raises(DomainError):
        coordinator.claim_plan_intent(_claim_request())


def test_claim_rejects_an_owner_that_already_carries_a_reservation() -> None:
    with pytest.raises(ValueError, match="unreserved owner"):
        ClaimPlanIntentRequest(
            plan_digest=PLAN_DIGEST,
            intent=_pending_intent(),
            owner=_reserved_owner(),
            operation_instant=INSTANT,
            active_plan=None,
            plan_head=None,
        )


# --- ReschedulePlanIntentRequest --------------------------------------------------------------


def _reschedule_request(head: PlanHeadObservation | None = None) -> ReschedulePlanIntentRequest:
    owner = _reserved_owner()
    if head is not None:
        owner = replace(
            owner,
            root_plan_key=PLAN_KEY,
            last_plan_key=PLAN_KEY,
            last_generation=0,
            latest_internal_execution_id=INTERNAL_EXEC,
            reserved_generation=1,
        )
        intent = replace(_claimed_intent(), claimed_generation=1)
        return ReschedulePlanIntentRequest(
            plan_digest=PLAN_DIGEST,
            intent=intent,
            owner=owner,
            operation_instant=INSTANT,
            plan_head=head,
        )
    return ReschedulePlanIntentRequest(
        plan_digest=PLAN_DIGEST,
        intent=_claimed_intent(),
        owner=owner,
        operation_instant=INSTANT,
        plan_head=None,
    )


def test_reschedule_is_the_exact_three_action_map_when_a_plan_exists() -> None:
    dynamo = _RecordingDynamo()
    request = _reschedule_request(_plan_head_observation())
    _seed(dynamo, _owner_item(PLAN_DIGEST, request.owner))
    _seed(dynamo, _intent_meta_item(PLAN_DIGEST, request.intent))
    result = _coordinator(dynamo).reschedule_plan_intent(request)

    assert result.outcome is ReschedulePlanIntentOutcome.RESCHEDULED
    # Grain: seconds of delay from the pre-increment retry count 0 -> exactly 60 s.
    assert int(result.due_at) - int(INSTANT) == 60 * 1_000_000
    assert result.retry_count == 1
    members = _members(dynamo)
    assert _kinds(members) == ["Update", "Update", "ConditionCheck"]
    head_check = members[2]["ConditionCheck"]
    assert head_check["Key"]["record_key"] == {"S": "HEAD"}
    assert head_check["Key"]["partition_key"]["S"].startswith("STATUS_FLOW#")
    intent_update = members[1]["Update"]
    assert intent_update["UpdateExpression"] == (
        "SET #state = :pending, retry_count = :retry_count, due_at = :due_at, "
        "ready_status = :ready, ready_order = :ready_order, "
        "last_transition_at = :instant REMOVE claimed_generation"
    )
    stored = dynamo.items[(f"WRITE_PLAN#{PLAN_DIGEST}#INTENT#{INTENT_ID}", "META")]
    assert stored["state"] == {"S": "pending"}
    assert "claimed_generation" not in stored
    assert stored["ready_order"] == {"S": f"{result.due_at}#{INTENT_ID}"}


def test_reschedule_replay_preserves_bytes_and_conflict_fails_closed() -> None:
    dynamo = _RecordingDynamo()
    request = _reschedule_request()
    _seed(dynamo, _owner_item(PLAN_DIGEST, request.owner))
    _seed(dynamo, _intent_meta_item(PLAN_DIGEST, request.intent))
    coordinator = _coordinator(dynamo)
    coordinator.reschedule_plan_intent(request)

    dynamo.fail_next_transactions = 1
    replayed = coordinator.reschedule_plan_intent(request)
    assert replayed.outcome is ReschedulePlanIntentOutcome.REPLAYED

    dynamo.items[(f"WRITE_PLAN#{PLAN_DIGEST}", "SUCCESSOR_OWNER")]["revision"] = {"N": "9"}
    dynamo.fail_next_transactions = 1
    with pytest.raises(DomainError):
        coordinator.reschedule_plan_intent(request)


def test_reschedule_rejects_a_reservation_naming_another_intent() -> None:
    with pytest.raises(ValueError, match="reservation"):
        ReschedulePlanIntentRequest(
            plan_digest=PLAN_DIGEST,
            intent=replace(_claimed_intent(), claimed_generation=5),
            owner=_reserved_owner(),
            operation_instant=INSTANT,
            plan_head=None,
        )


# --- PersistPlanRequest ---------------------------------------------------------------------------


def _persist_request() -> PersistPlanRequest:
    return PersistPlanRequest(
        plan_digest=PLAN_DIGEST,
        intent=_claimed_intent(),
        units=UNITS,
        owner=_reserved_owner(),
        unit_flows=tuple(UnitFlowBinding(key, flow) for key, flow in FLOWS.items()),
        proof=_persist_proof(),
        operation_instant=INSTANT,
    )


def test_ordinary_persistence_is_the_exact_five_plus_units_action_map() -> None:
    dynamo = _RecordingDynamo()
    request = _persist_request()
    _seed(dynamo, _owner_item(PLAN_DIGEST, request.owner))
    _seed(dynamo, _intent_meta_item(PLAN_DIGEST, request.intent))
    result = _coordinator(dynamo).persist_plan(request)

    assert result.outcome is PersistPlanOutcome.PERSISTED
    assert (result.plan_key, result.internal_execution_id) == (PLAN_KEY, INTERNAL_EXEC)
    assert result.generation == 0
    members = _members(dynamo)
    # Grain: DynamoDB transact members; u + 5 for u=2 units.
    assert _kinds(members) == [
        "Update",
        "ConditionCheck",
        "Put",
        "Put",
        "Put",
        "Put",
        "Put",
    ]
    owner_update = members[0]["Update"]
    assert owner_update["UpdateExpression"] == (
        "SET revision = :new_revision, root_plan_key = :root_plan_key, "
        "last_plan_key = :plan_key, last_generation = :claimed_generation, "
        "active_plan_key = :plan_key, active_intent_id = :intent_id, "
        "latest_internal_execution_id = :internal_execution_id "
        "REMOVE reserved_intent_id, reserved_intent_digest, reserved_generation"
    )
    assert owner_update["ExpressionAttributeValues"][":root_plan_key"] == {"S": PLAN_KEY}
    meta_item = members[2]["Put"]["Item"]
    assert meta_item["partition_key"] == {"S": PLAN_KEY}
    assert meta_item["status"] == {"S": "planned"}
    assert meta_item["generation"] == {"N": "0"}
    assert meta_item["child_outcome_reduction_version"] == {"N": "2"}
    head_put = members[6]["Put"]["Item"]
    assert head_put["record_key"] == {"S": "HEAD"}
    owner_stored = dynamo.items[(f"WRITE_PLAN#{PLAN_DIGEST}", "SUCCESSOR_OWNER")]
    assert owner_stored["active_plan_key"] == {"S": PLAN_KEY}
    assert "reserved_intent_id" not in owner_stored


def test_persist_replay_retains_the_assigned_generation() -> None:
    dynamo = _RecordingDynamo()
    request = _persist_request()
    _seed(dynamo, _owner_item(PLAN_DIGEST, request.owner))
    _seed(dynamo, _intent_meta_item(PLAN_DIGEST, request.intent))
    coordinator = _coordinator(dynamo)
    coordinator.persist_plan(request)

    dynamo.fail_next_transactions = 1
    replayed = coordinator.persist_plan(request)
    assert replayed.outcome is PersistPlanOutcome.REPLAYED
    assert replayed.generation == 0
    assert replayed.plan_key == PLAN_KEY


def test_persistence_with_an_owner_active_plan_is_refused_in_this_slice() -> None:
    """Planned-plan replacement belongs to a later slice; it must fail closed here."""
    with pytest.raises(ValueError, match="replacement"):
        PersistPlanRequest(
            plan_digest=PLAN_DIGEST,
            intent=replace(_claimed_intent(), claimed_generation=1),
            units=UNITS,
            owner=replace(
                _active_owner(),
                reserved_intent_id=INTENT_ID,
                reserved_intent_digest=INTENT_ID,
                reserved_generation=1,
            ),
            unit_flows=tuple(UnitFlowBinding(key, flow) for key, flow in FLOWS.items()),
            proof=_persist_proof(),
            operation_instant=INSTANT,
        )


def test_persistence_rejects_a_roster_that_cannot_reconstruct_the_intent() -> None:
    changed = (replace(UNITS[0], merge_count=9), UNITS[1])
    with pytest.raises(ValueError, match="roster"):
        PersistPlanRequest(
            plan_digest=PLAN_DIGEST,
            intent=_claimed_intent(),
            units=changed,
            owner=_reserved_owner(),
            unit_flows=tuple(UnitFlowBinding(key, flow) for key, flow in FLOWS.items()),
            proof=_persist_proof(),
            operation_instant=INSTANT,
        )


# --- AdmitUnitRequest -------------------------------------------------------------------------


def _admit_request(*, admitted: int, proof: Stage1LifecycleProof | None) -> AdmitUnitRequest:
    return AdmitUnitRequest(
        plan_digest=PLAN_DIGEST,
        plan_key=PLAN_KEY,
        intent=_claimed_intent(),
        owner=_active_owner(),
        plan_meta=_plan_meta(admitted),
        unit=_plan_unit("db.alpha"),
        deduplication_digest=CHECKSUM,
        operation_instant=INSTANT,
        proof=proof,
    )


def test_a_non_final_admission_is_four_condition_bound_members() -> None:
    dynamo = _RecordingDynamo()
    request = _admit_request(admitted=0, proof=None)
    _seed(dynamo, _owner_item(PLAN_DIGEST, request.owner))
    _seed(dynamo, _plan_meta_item(PLAN_KEY, request.plan_meta))
    _seed(dynamo, _plan_unit_item(PLAN_KEY, request.unit))
    _seed(dynamo, _intent_meta_item(PLAN_DIGEST, request.intent))
    result = _coordinator(dynamo).admit_unit(request)

    assert result.outcome is AdmitUnitOutcome.ADMITTED
    assert result.final is False
    members = _members(dynamo)
    assert _kinds(members) == ["ConditionCheck", "Update", "Update", "ConditionCheck"]
    unit_update = members[2]["Update"]
    assert unit_update["ConditionExpression"] == (
        "admission = :pending AND #status = :queued AND owner_generation = :generation"
    )
    stored = dynamo.items[(PLAN_KEY, "UNIT#db.alpha")]
    assert stored["admission"] == {"S": "admitted"}
    assert stored["admission_deduplication_digest"] == {"S": CHECKSUM}
    meta_stored = dynamo.items[(PLAN_KEY, "META")]
    assert meta_stored["admitted_count"] == {"N": "1"}


def test_the_final_admission_admits_the_intent_and_writes_one_event_and_head() -> None:
    dynamo = _RecordingDynamo()
    request = _admit_request(admitted=1, proof=_successor_proof())
    _seed(dynamo, _owner_item(PLAN_DIGEST, request.owner))
    _seed(dynamo, _plan_meta_item(PLAN_KEY, request.plan_meta))
    _seed(dynamo, _plan_unit_item(PLAN_KEY, request.unit))
    _seed(dynamo, _intent_meta_item(PLAN_DIGEST, request.intent))
    result = _coordinator(dynamo).admit_unit(request)

    assert result.final is True
    members = _members(dynamo)
    assert _kinds(members) == ["ConditionCheck", "Update", "Update", "Update", "Put", "Put"]
    intent_stored = dynamo.items[(f"WRITE_PLAN#{PLAN_DIGEST}#INTENT#{INTENT_ID}", "META")]
    assert intent_stored["state"] == {"S": "admitted"}
    assert intent_stored["claimed_generation"] == {"N": "0"}  # retained by admission
    assert "ready_status" not in intent_stored


def test_admission_replay_is_a_no_op_and_requires_the_proof_only_when_final() -> None:
    dynamo = _RecordingDynamo()
    request = _admit_request(admitted=0, proof=None)
    _seed(dynamo, _owner_item(PLAN_DIGEST, request.owner))
    _seed(dynamo, _plan_meta_item(PLAN_KEY, request.plan_meta))
    _seed(dynamo, _plan_unit_item(PLAN_KEY, request.unit))
    _seed(dynamo, _intent_meta_item(PLAN_DIGEST, request.intent))
    coordinator = _coordinator(dynamo)
    coordinator.admit_unit(request)

    dynamo.fail_next_transactions = 1
    assert coordinator.admit_unit(request).outcome is AdmitUnitOutcome.REPLAYED
    with pytest.raises(ValueError, match="proof"):
        _admit_request(admitted=0, proof=_successor_proof())
    with pytest.raises(ValueError, match="proof"):
        _admit_request(admitted=1, proof=None)


def test_an_already_admitted_unit_is_rejected_before_any_client_call() -> None:
    admitted_unit = replace(
        _plan_unit("db.alpha"),
        admission=PlanUnitAdmission.ADMITTED,
        admission_deduplication_digest=CHECKSUM,
        admitted_at=INSTANT,
    )
    with pytest.raises(ValueError, match="pending"):
        replace(_admit_request(admitted=0, proof=None), unit=admitted_unit)


# --- FailAdmissionRequest -------------------------------------------------------------------------


def _fail_request() -> FailAdmissionRequest:
    return FailAdmissionRequest(
        plan_digest=PLAN_DIGEST,
        plan_key=PLAN_KEY,
        intent=_claimed_intent(),
        owner=_active_owner(),
        plan_meta=_plan_meta(admitted=1),
        operation_instant=INSTANT,
        proof=_successor_proof(),
    )


def test_admission_failure_terminalizes_the_candidate_and_restores_pending() -> None:
    dynamo = _RecordingDynamo()
    request = _fail_request()
    _seed(dynamo, _owner_item(PLAN_DIGEST, request.owner))
    _seed(dynamo, _plan_meta_item(PLAN_KEY, request.plan_meta))
    _seed(dynamo, _intent_meta_item(PLAN_DIGEST, request.intent))
    result = _coordinator(dynamo).fail_admission(request)

    assert result.outcome is FailAdmissionOutcome.FAILED
    # Grain: seconds of delay from the pre-increment retry count 0 -> exactly 60 s.
    assert int(result.due_at) - int(INSTANT) == 60 * 1_000_000
    members = _members(dynamo)
    assert _kinds(members) == ["Update", "Update", "Update", "Put", "Put"]
    intent_update = members[2]["Update"]
    assert ":origin_kind" in intent_update["ExpressionAttributeValues"]
    assert intent_update["ExpressionAttributeValues"][":origin_member"] == {"S": ORIGIN_MEMBER}
    plan_stored = dynamo.items[(PLAN_KEY, "META")]
    assert plan_stored["status"] == {"S": "admission_failed"}
    owner_stored = dynamo.items[(f"WRITE_PLAN#{PLAN_DIGEST}", "SUCCESSOR_OWNER")]
    assert "active_plan_key" not in owner_stored
    intent_stored = dynamo.items[(f"WRITE_PLAN#{PLAN_DIGEST}#INTENT#{INTENT_ID}", "META")]
    assert intent_stored["state"] == {"S": "pending"}
    assert "claimed_generation" not in intent_stored
    assert intent_stored["ready_status"] == {"S": "ready"}


def test_admission_failure_replay_preserves_the_durable_transfer() -> None:
    dynamo = _RecordingDynamo()
    request = _fail_request()
    _seed(dynamo, _owner_item(PLAN_DIGEST, request.owner))
    _seed(dynamo, _plan_meta_item(PLAN_KEY, request.plan_meta))
    _seed(dynamo, _intent_meta_item(PLAN_DIGEST, request.intent))
    coordinator = _coordinator(dynamo)
    coordinator.fail_admission(request)

    dynamo.fail_next_transactions = 1
    assert coordinator.fail_admission(request).outcome is FailAdmissionOutcome.REPLAYED


# --- CompletePlanRequest --------------------------------------------------------------------------


def _attested_unit_item(key: str) -> dict[str, object]:
    unit = replace(
        _plan_unit(key),
        status=PlanUnitStatus.ATTESTED,
        admission=PlanUnitAdmission.ADMITTED,
        admission_deduplication_digest=CHECKSUM,
        admitted_at=INSTANT,
    )
    return _plan_unit_item(PLAN_KEY, unit)


def _attempt_item(table: str, attempt_id: str) -> dict[str, object]:
    return {
        "partition_key": f"TABLE_ATTEMPT#{table}",
        "record_key": f"ATTEMPT#{attempt_id}",
        "state": "SUCCEEDED",
        "athena_usage_state": "settled",
        "usage_authority_digest": CHECKSUM,
        "scanned_bytes": 1024,
    }


def _complete_request() -> CompletePlanRequest:
    meta = replace(
        _plan_meta(admitted=2),
        attested_count=2,
        settled_attempt_count=2,
        settled_attempt_digest=CHECKSUM,
    )
    return CompletePlanRequest(
        plan_digest=PLAN_DIGEST,
        plan_key=PLAN_KEY,
        owner=_active_owner(),
        plan_meta=meta,
        units=tuple(
            AttestedUnitObservation(key, FLOWS[key], 0, _attested_unit_item(key))
            for key in ("db.alpha", "db.beta")
        ),
        attempts=(
            SettledAttemptObservation(
                "db.alpha",
                "attempt-fixture-0001",
                "SUCCEEDED",
                CHECKSUM,
                _attempt_item("db.alpha", "attempt-fixture-0001"),
            ),
            SettledAttemptObservation(
                "db.beta",
                "attempt-fixture-0002",
                "SUCCEEDED",
                CHECKSUM,
                _attempt_item("db.beta", "attempt-fixture-0002"),
            ),
        ),
        proof=_successor_proof(),
        operation_instant=INSTANT,
    )


def test_completion_conditions_every_unit_and_settled_attempt_exactly_once() -> None:
    dynamo = _RecordingDynamo()
    request = _complete_request()
    _seed(dynamo, _owner_item(PLAN_DIGEST, request.owner))
    _seed(dynamo, _plan_meta_item(PLAN_KEY, request.plan_meta))
    result = _coordinator(dynamo).complete_plan(request)

    assert result.outcome is CompletePlanOutcome.COMPLETED
    members = _members(dynamo)
    # Grain: u + t + 4 members for u=2 units and t=2 attempts.
    assert _kinds(members) == [
        "ConditionCheck",
        "ConditionCheck",
        "ConditionCheck",
        "ConditionCheck",
        "Update",
        "Update",
        "Put",
        "Put",
    ]
    attempt_check = members[2]["ConditionCheck"]
    assert attempt_check["ConditionExpression"] == (
        "#state = :state AND athena_usage_state = :settled AND usage_authority_digest = :digest"
    )
    assert attempt_check["Key"]["partition_key"] == {"S": "TABLE_ATTEMPT#db.alpha"}
    plan_stored = dynamo.items[(PLAN_KEY, "META")]
    assert plan_stored["status"] == {"S": "completed"}
    assert plan_stored["terminal_at"] == {"S": INSTANT}
    owner_stored = dynamo.items[(f"WRITE_PLAN#{PLAN_DIGEST}", "SUCCESSOR_OWNER")]
    assert "active_plan_key" not in owner_stored
    assert owner_stored["latest_internal_execution_id"] == {"S": INTERNAL_EXEC}


def test_completion_replay_is_a_no_op_and_the_intent_is_never_a_member() -> None:
    dynamo = _RecordingDynamo()
    request = _complete_request()
    _seed(dynamo, _owner_item(PLAN_DIGEST, request.owner))
    _seed(dynamo, _plan_meta_item(PLAN_KEY, request.plan_meta))
    coordinator = _coordinator(dynamo)
    coordinator.complete_plan(request)

    intent_partition = f"WRITE_PLAN#{PLAN_DIGEST}#INTENT#{INTENT_ID}"
    for member in _members(dynamo):
        body = next(iter(member.values()))
        key = cast("dict[str, dict[str, str]] | None", body.get("Key"))
        partition = (
            key["partition_key"]["S"] if key is not None else body["Item"]["partition_key"]["S"]
        )
        assert partition != intent_partition

    # The replay classification reread proves the durable completion bytes.
    dynamo.fail_next_transactions = 1
    assert coordinator.complete_plan(request).outcome is CompletePlanOutcome.REPLAYED


def test_completion_requires_full_admission_attestation_and_zero_failures() -> None:
    incomplete = replace(_plan_meta(admitted=2), attested_count=1)
    with pytest.raises(ValueError, match="full admission"):
        replace(_complete_request(), plan_meta=incomplete)


def test_an_over_envelope_observation_is_refused_before_any_client_call() -> None:
    """The byte oracle measures the assembled transaction before every SDK call."""
    dynamo = _RecordingDynamo()
    request = _complete_request()
    oversized = dict(_attempt_item("db.alpha", "attempt-fixture-0001"))
    oversized["padding"] = "x" * 400_001
    swapped = (
        SettledAttemptObservation(
            "db.alpha", "attempt-fixture-0001", "SUCCEEDED", CHECKSUM, oversized
        ),
        request.attempts[1],
    )
    with pytest.raises(ValidationError):
        _coordinator(dynamo).complete_plan(replace(request, attempts=swapped))
    assert not dynamo.transacts


# --- §3.2 action-count formulas at the accepted simultaneous profile ------------------------------


def _maximum_world() -> tuple[tuple[CanonicalUnitEntry, ...], dict[str, str]]:
    units = tuple(_unit(f"db.table_{index:02d}", index + 1) for index in range(15))
    return units, {unit.unit_key: _flow(unit.unit_key) for unit in units}


def _maximum_submit_request() -> SubmitPlanIntentRequest:
    units, _ = _maximum_world()
    return SubmitPlanIntentRequest(
        plan_digest=PLAN_DIGEST,
        root_plan_key=None,
        source_plan_key=None,
        origin=ORIGIN,
        units=units,
        operation_instant=INSTANT,
        owner=None,
    )


def _maximum_persist_case() -> tuple[PersistPlanRequest, list[dict[str, object]]]:
    """Return the maximum ordinary persistence request plus its exact seed pre-states."""
    units, flows = _maximum_world()
    submit = _maximum_submit_request()
    intent_id = submit.intent_id()
    internal_exec = internal_execution_identity(intent_id, 0)
    plan_key = plan_partition_key(PLAN_DIGEST, internal_exec)
    intent = replace(_claimed_intent(), intent_id=intent_id, roster_digest=submit.roster_digest())
    owner = replace(
        bootstrap_owner(),
        revision=1,
        reserved_intent_id=intent_id,
        reserved_intent_digest=intent_id,
        reserved_generation=0,
    )
    parent = derive_plan_identity(PlanIdentityRequest(PLAN_DIGEST, internal_exec)).flow_id
    expectations = PlanRequiredChildExpectations(
        parent,
        tuple(
            sorted(
                (StatusChildExpectation(flow, 0) for flow in flows.values()),
                key=lambda member: member.child_flow_id.encode("utf-8"),
            )
        ),
    )
    proof = derive_stage1_lifecycle_proof(
        Stage1LifecycleProofRequest(
            identity=PlanIdentityRequest(PLAN_DIGEST, internal_exec),
            internal_key=(plan_key,),
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
            required_child_expectations=expectations,
        )
    )
    request = PersistPlanRequest(
        plan_digest=PLAN_DIGEST,
        intent=intent,
        units=units,
        owner=owner,
        unit_flows=tuple(UnitFlowBinding(key, flow) for key, flow in flows.items()),
        proof=proof,
        operation_instant=INSTANT,
    )
    seeds = [_owner_item(PLAN_DIGEST, owner), _intent_meta_item(PLAN_DIGEST, intent)]
    return request, seeds


def _maximum_complete_case() -> tuple[CompletePlanRequest, list[dict[str, object]]]:
    """Return the maximum 51-action completion request plus its exact seed pre-states."""
    units, flows = _maximum_world()
    meta = replace(
        _plan_meta(admitted=0),
        unit_count=15,
        admitted_count=15,
        attested_count=15,
        required_child_expectations=tuple(
            RequiredChildExpectation(flow, 0)
            for flow in sorted(flows.values(), key=lambda flow: flow.encode("utf-8"))
        ),
        settled_attempt_count=32,
        settled_attempt_digest=CHECKSUM,
    )
    unit_observations = tuple(
        AttestedUnitObservation(
            unit.unit_key,
            flows[unit.unit_key],
            0,
            _plan_unit_item(
                PLAN_KEY,
                PlanUnit(
                    unit=unit,
                    flow_id=flows[unit.unit_key],
                    owner_generation=0,
                    status=PlanUnitStatus.ATTESTED,
                    admission=PlanUnitAdmission.ADMITTED,
                    admission_deduplication_digest=CHECKSUM,
                    admitted_at=INSTANT,
                    created_at=INSTANT,
                ),
            ),
        )
        for unit in units
    )
    attempts = tuple(
        SettledAttemptObservation(
            "db.alpha",
            f"attempt-fixture-{ordinal:04d}",
            "SUCCEEDED",
            CHECKSUM,
            _attempt_item("db.alpha", f"attempt-fixture-{ordinal:04d}"),
        )
        for ordinal in range(32)
    )
    request = replace(
        _complete_request(), plan_meta=meta, units=unit_observations, attempts=attempts
    )
    seeds = [_owner_item(PLAN_DIGEST, request.owner), _plan_meta_item(PLAN_KEY, request.plan_meta)]
    return request, seeds


def test_a_maximum_submission_is_exactly_seventeen_actions() -> None:
    dynamo = _RecordingDynamo()
    _coordinator(dynamo).submit_plan_intent(_maximum_submit_request())
    assert len(_members(dynamo)) == 17  # F = 2 + u at u = 15


def test_maximum_ordinary_persistence_is_exactly_twenty_actions() -> None:
    dynamo = _RecordingDynamo()
    request, seeds = _maximum_persist_case()
    for seed in seeds:
        _seed(dynamo, seed)
    _coordinator(dynamo).persist_plan(request)
    assert len(_members(dynamo)) == 20  # F = u + 5 at u = 15


def test_maximum_completion_is_exactly_fifty_one_actions() -> None:
    dynamo = _RecordingDynamo()
    request, seeds = _maximum_complete_case()
    for seed in seeds:
        _seed(dynamo, seed)
    _coordinator(dynamo).complete_plan(request)
    assert len(_members(dynamo)) == 51  # F = u + t + 4 at u = 15 and t = 32

    beyond = SettledAttemptObservation(
        "db.alpha",
        "attempt-fixture-0033",
        "SUCCEEDED",
        CHECKSUM,
        _attempt_item("db.alpha", "attempt-fixture-0033"),
    )
    with pytest.raises(ValueError, match="at most 32"):
        replace(request, attempts=(*request.attempts, beyond))


# --- Committed §3.2 byte/action fixtures ------------------------------------------------------


@pytest.fixture(scope="module")
def fixture_tree() -> dict[str, dict[str, dict[str, Any]]]:
    return load_fixture_tree()


def _run_maximum(operation: str) -> _RecordingDynamo:
    dynamo = _RecordingDynamo()
    if operation == "SubmitPlanIntentRequest":
        _coordinator(dynamo).submit_plan_intent(_maximum_submit_request())
        return dynamo
    if operation == "PersistPlanRequest":
        request, seeds = _maximum_persist_case()
        for seed in seeds:
            _seed(dynamo, seed)
        _coordinator(dynamo).persist_plan(request)
        return dynamo
    request, seeds = _maximum_complete_case()
    for seed in seeds:
        _seed(dynamo, seed)
    _coordinator(dynamo).complete_plan(request)
    return dynamo


def _fixture_actions(fixture: dict[str, Any]) -> TransactionEnvelopeRequest:
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
        key = {name: cast("str", key_item[name]) for name in ("partition_key", "record_key")}
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


def test_every_maximum_fixture_matches_the_production_builder_and_oracle(
    fixture_tree: dict[str, dict[str, dict[str, Any]]],
) -> None:
    for operation in ("SubmitPlanIntentRequest", "PersistPlanRequest", "CompletePlanRequest"):
        fixture = fixture_tree[operation]["maximum"]
        dynamo = _run_maximum(operation)
        assert dynamo.transacts[0] == fixture["expected_request"]
        assert (
            ORACLE.wire_json_bytes(dynamo.transacts[0]) == (fixture["expected"]["wire_json_bytes"])
        )
        accepted = ORACLE.validate_envelope(_fixture_actions(fixture))
        assert accepted.action_count == fixture["expected"]["action_count"]
        assert accepted.aggregate_upper_bound == fixture["expected"]["aggregate_upper_bound"]


def test_every_one_beyond_fixture_is_refused_by_the_production_oracle(
    fixture_tree: dict[str, dict[str, dict[str, Any]]],
) -> None:
    """One conservative item byte beyond 400000 refuses before mutation, never splits."""
    for operation in ("SubmitPlanIntentRequest", "PersistPlanRequest", "CompletePlanRequest"):
        fixture = fixture_tree[operation]["one-beyond"]
        assert fixture["expected"]["outcome"] == "refused"
        with pytest.raises(ValidationError):
            ORACLE.validate_envelope(_fixture_actions(fixture))


# --- S3: the new-root mode ConditionCheck (V-13(b)) ---------------------------------------------

_GRAPH_TABLE = "execution-graph-fixture"
_MODE_ITEM: dict[str, object] = {
    "pk": "MODE#ingestion",
    "sk": "MODE",
    "mode": "SHADOW",
    "revision": 1,
}


def _graph_coordinator(dynamo: _RecordingDynamo) -> DynamoProjectionCoordinator:
    head_store = DynamoStage1LifecycleHeadStore(TABLE, cast("Any", dynamo))
    return DynamoProjectionCoordinator(
        TABLE, "leases-fixture", head_store, cast("Any", dynamo), graph_table=_GRAPH_TABLE
    )


def _guarded(observed: dict[str, object] | None) -> SubmitPlanIntentRequest:
    return replace(
        _submit_request(None),
        mode_guard=AuthorityModeGuard(ModeGuardKind.NEW_ROOT_ADMITTING, observed),
    )


def test_v13b_a_source_delivery_submission_condition_checks_the_admitting_modes() -> None:
    dynamo = _RecordingDynamo()
    _graph_coordinator(dynamo).submit_plan_intent(_guarded(_MODE_ITEM))
    members = _members(dynamo)
    check = members[-1]["ConditionCheck"]
    assert check["TableName"] == _GRAPH_TABLE
    assert check["Key"] == {"pk": {"S": "MODE#ingestion"}, "sk": {"S": "MODE"}}
    assert check["ConditionExpression"] == (
        "attribute_not_exists(pk) OR #mode = :legacy OR #mode = :shadow"
    )
    assert check["ExpressionAttributeNames"] == {"#mode": "mode"}
    assert all(next(iter(m.values()))["TableName"] == TABLE for m in members[:-1])


def test_v13b_without_the_race_an_absent_mode_item_still_commits() -> None:
    dynamo = _RecordingDynamo()
    result = _graph_coordinator(dynamo).submit_plan_intent(_guarded(None))
    assert result.outcome is SubmitPlanIntentOutcome.SUBMITTED


def test_v13b_the_drain_cas_between_read_and_submit_is_authority_mode_draining() -> None:
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
        _graph_coordinator(dynamo).submit_plan_intent(_guarded(_MODE_ITEM))
    assert refused.value.signal == "AUTHORITY_MODE_DRAINING"


def test_v13b_a_mode_guard_without_the_graph_table_is_refused_before_the_sdk() -> None:
    dynamo = _RecordingDynamo()
    with pytest.raises(InvariantError):
        _coordinator(dynamo).submit_plan_intent(_guarded(_MODE_ITEM))
    assert dynamo.transacts == []
