# SPDX-License-Identifier: MIT-0
"""ADR-075 Stage-1 operational-status runtime against explicitly selected DynamoDB tables."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import boto3
import pytest
from botocore.exceptions import ClientError
from probe_identity import PROBE_PREFIX, probe_key, pytest_node_id

from adapters.outbound.dynamodb.items import read_int
from adapters.outbound.dynamodb.stage1_operational_status import (
    AGGREGATE_RECORD,
    CASE_RECORD,
    OPEN_CASES_PARTITION,
    DynamoStage1OperationalStatus,
)
from domain.operations.stage1_operational_status import (
    BlockErrorClass,
    BlockStage1OutboxRequest,
    DependencyErrorClass,
    EventIdentityRequest,
    LifecycleEventAuthority,
    MarkStage1OutboxDeliveredRequest,
    OutboxState,
    PlanIdentityRequest,
    RecordStage1AuthorityObservationRequest,
    RecordStage1OutboxDeliveryFailureRequest,
    RecoveryOutcome,
    Stage1EventAuthorityRequest,
    Stage1OutboxOrderRequest,
    Stage1OutboxRecoveryInvocation,
    Stage1Surface,
    UnitIdentityRequest,
    derive_event_id,
    derive_plan_identity,
    derive_unit_identity,
    event_authority_digest,
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
    canonical_authority_digest,
    event_document,
)

if TYPE_CHECKING:
    from types_boto3_dynamodb.client import DynamoDBClient
    from types_boto3_dynamodb.type_defs import AttributeValueTypeDef

pytestmark = [pytest.mark.integration, pytest.mark.covers("ADR-075")]

_LOGGER = logging.getLogger(__name__)

_PROD_LIKE = ("prod", "production", "prd")
_NON_PROD = ("dev", "test", "beta", "alpha", "staging", "sandbox")


@dataclass(frozen=True, slots=True)
class _Target:
    profile: str
    region: str
    expected_account: str
    environment: str
    publications_table: str
    status_table: str


def _target() -> _Target:
    names = {
        "profile": "ADL_ADR_075_INTEGRATION_PROFILE",
        "region": "ADL_ADR_075_INTEGRATION_REGION",
        "expected_account": "ADL_ADR_075_INTEGRATION_EXPECTED_ACCOUNT",
        "environment": "ADL_ADR_075_INTEGRATION_ENVIRONMENT",
        "publications_table": "ADL_ADR_075_INTEGRATION_PUBLICATIONS_TABLE",
        "status_table": "ADL_ADR_075_INTEGRATION_STATUS_TABLE",
    }
    values = {field: os.environ.get(name, "").strip() for field, name in names.items()}
    if any(not value for value in values.values()):
        pytest.skip(
            "ADR-075 integration requires explicit profile, account, region, env, and tables"
        )
    if values["profile"].lower() == "default":
        pytest.fail("ADR-075 integration refuses the default AWS profile")
    targets = (
        values["profile"],
        values["environment"],
        values["publications_table"],
        values["status_table"],
    )
    if any(token in target.lower() for target in targets for token in _PROD_LIKE):
        pytest.fail("ADR-075 integration refuses production-like targets")
    if any(
        not values[name].lower().startswith(f"{PROBE_PREFIX}-")
        for name in ("publications_table", "status_table")
    ):
        pytest.fail("ADR-075 integration requires dedicated integration-probe-prefixed tables")
    environment = values["environment"].lower()
    if not any(token in environment for token in _NON_PROD):
        pytest.fail("ADR-075 integration environment must be explicitly non-production")
    if any(
        environment not in values[name].lower() for name in ("publications_table", "status_table")
    ):
        pytest.fail("ADR-075 integration tables must exactly name the selected environment")
    if not values["expected_account"].isdigit() or len(values["expected_account"]) != 12:
        pytest.fail("ADR-075 integration expected account must contain exactly 12 digits")
    return _Target(**values)


@pytest.mark.parametrize("shared_table", ["publications_table", "status_table"])
def test_target_requires_dedicated_probe_prefixed_tables(
    monkeypatch: pytest.MonkeyPatch,
    shared_table: str,
) -> None:
    environment = {
        "ADL_ADR_075_INTEGRATION_PROFILE": "adr-075-test",
        "ADL_ADR_075_INTEGRATION_REGION": "us-east-1",
        "ADL_ADR_075_INTEGRATION_EXPECTED_ACCOUNT": "111122223333",
        "ADL_ADR_075_INTEGRATION_ENVIRONMENT": "test",
        "ADL_ADR_075_INTEGRATION_PUBLICATIONS_TABLE": (f"{PROBE_PREFIX}-test-publications"),
        "ADL_ADR_075_INTEGRATION_STATUS_TABLE": f"{PROBE_PREFIX}-test-status",
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    assert _target().publications_table.startswith(PROBE_PREFIX)
    environment_name = {
        "publications_table": "ADL_ADR_075_INTEGRATION_PUBLICATIONS_TABLE",
        "status_table": "ADL_ADR_075_INTEGRATION_STATUS_TABLE",
    }[shared_table]
    monkeypatch.setenv(environment_name, f"shared-test-{shared_table}")

    with pytest.raises(pytest.fail.Exception, match="dedicated integration-probe-prefixed"):
        _target()


def _case_ref(probe: str) -> str:
    raw = bytearray(hashlib.sha256(probe.encode()).digest()[:16])
    raw[6] = (raw[6] & 0x0F) | 0x40
    raw[8] = (raw[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(raw)))


def _plan_fixture(
    probe: str, observed_at: datetime
) -> tuple[OperationalDataFlowEvent, dict[str, AttributeValueTypeDef]]:
    """Derive one self-consistent retained plan EVENT# authority for this probe alone."""
    plan_digest = f"{probe}#plan-digest"
    internal_execution_id = f"{probe}#delivery-execution"
    identity = derive_plan_identity(PlanIdentityRequest(plan_digest, internal_execution_id))
    unit_identity = derive_unit_identity(
        UnitIdentityRequest(plan_digest, f"{probe}#unit", f"{probe}#writer-execution")
    )
    authority = Stage1EventAuthorityRequest(
        surface=Stage1Surface.PLAN,
        internal_key=(probe,),
        transition_id="semantic_plan_terminal",
        lifecycle_state=FlowState.COMPLETED,
        completion_outcome=CompletionOutcome.SUCCEEDED,
        typed_reason=None,
        authority_time=stage1_authority_time(observed_at),
    )
    digest = event_authority_digest(authority)
    event = OperationalDataFlowEvent(
        event_id=derive_event_id(
            EventIdentityRequest(Stage1Surface.PLAN, digest, 1, FlowState.COMPLETED)
        ),
        predecessor_event_id=None,
        flow_id=identity.flow_id,
        execution_id=identity.execution_id,
        predecessor_execution_id=None,
        transition_sequence=1,
        surface=Stage1Surface.PLAN.value,
        stage="integration_probe_terminal",
        state=FlowState.COMPLETED,
        occurred_at=observed_at,
        authority=AuthorityObservation("integration_probe", digest, observed_at),
        evaluation_binding=EvaluationBinding(MEASUREMENT_REFS[Stage1Surface.PLAN.value]),
        outcome=CompletionOutcome.SUCCEEDED,
        required_child_flow_ids=(unit_identity.flow_id,),
    )
    attributes: dict[str, AttributeValueTypeDef] = {
        "surface": {"S": Stage1Surface.PLAN.value},
        "internal_key": {"L": [{"S": probe}]},
        "transition_id": {"S": "semantic_plan_terminal"},
        "lifecycle_state": {"S": FlowState.COMPLETED.value},
        "completion_outcome": {"S": CompletionOutcome.SUCCEEDED.value},
        "authority_time": {"S": stage1_authority_time(observed_at)},
        "plan_digest": {"S": plan_digest},
        "internal_execution_id": {"S": internal_execution_id},
        "flow_id": {"S": event.flow_id},
        "execution_id": {"S": event.execution_id},
        "event_id": {"S": event.event_id},
        "transition_sequence": {"N": "1"},
        "child_outcome_reduction_version": {"N": "2"},
        "required_child_expectations": {
            "L": [
                {
                    "M": {
                        "child_flow_id": {"S": unit_identity.flow_id},
                        "owner_generation": {"N": "1"},
                    }
                }
            ]
        },
    }
    return event, attributes


def _delete_if_owned(  # noqa: PLR0913, PLR0917 - closed conditional cleanup
    client: DynamoDBClient,
    table: str,
    key: dict[str, AttributeValueTypeDef],
    condition: str,
    values: dict[str, AttributeValueTypeDef],
) -> None:
    try:
        client.delete_item(
            TableName=table,
            Key=key,
            ConditionExpression=condition,
            ExpressionAttributeValues=values,
        )
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise


def test_full_conditional_outbox_lifecycle_on_isolated_explicit_tables(  # noqa: PLR0915
    request: pytest.FixtureRequest,
) -> None:
    target = _target()
    session = boto3.Session(profile_name=target.profile, region_name=target.region)
    identity = session.client("sts").get_caller_identity()
    assert identity["Account"] == target.expected_account
    client = session.client("dynamodb")
    probe = probe_key(pytest_node_id(request))
    now = datetime.now(tz=UTC)
    lifecycle_event, retained_attributes = _plan_fixture(probe, now)
    payload = event_document(lifecycle_event)
    digest = canonical_authority_digest(payload)
    partition_key = probe
    record_key = f"EVENT#{probe}"
    publication_key: dict[str, AttributeValueTypeDef] = {
        "partition_key": {"S": partition_key},
        "record_key": {"S": record_key},
    }
    case_ref = _case_ref(probe)
    blocked_at = now + timedelta(seconds=4)
    blocked_micros = int(blocked_at.timestamp() * 1_000_000)
    case_key: dict[str, AttributeValueTypeDef] = {
        "partition_key": {"S": f"CASE#{case_ref}"},
        "sort_key": {"S": CASE_RECORD},
    }
    membership_key: dict[str, AttributeValueTypeDef] = {
        "partition_key": {"S": OPEN_CASES_PARTITION},
        "sort_key": {"S": f"OPEN#{blocked_micros:020d}#{case_ref}"},
    }
    aggregate_key: dict[str, AttributeValueTypeDef] = {
        "partition_key": {"S": f"STATUS_OBSERVATION#{Stage1Surface.PLAN.value}"},
        "sort_key": {"S": AGGREGATE_RECORD},
    }
    existing_aggregate = client.get_item(
        TableName=target.status_table,
        Key=aggregate_key,
        ConsistentRead=True,
    ).get("Item")
    if existing_aggregate is not None:
        pytest.fail("ADR-075 integration status table is not isolated for its exact aggregate key")
    client.put_item(
        TableName=target.status_table,
        Item={
            **aggregate_key,
            "probe_owner": {"S": probe},
            "blocked_dependency": {"N": "0"},
            "blocked_invalid": {"N": "0"},
            "requeued_pending": {"N": "0"},
            "source_sequence": {"N": "0"},
        },
        ConditionExpression=(
            "attribute_not_exists(partition_key) AND attribute_not_exists(sort_key)"
        ),
    )

    order = stage1_outbox_order(Stage1OutboxOrderRequest(partition_key, record_key, digest, now))
    item: dict[str, AttributeValueTypeDef] = {
        **publication_key,
        "immutable_payload_digest": {"S": digest},
        "authority_kind": {"S": "lifecycle_event"},
        "outbox_state": {"S": "pending"},
        "outbox_status": {"S": "pending"},
        "outbox_order": {"S": order},
        "consecutive_failed_delivery_attempts": {"N": "0"},
        "payload": {
            "S": json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        },
        **retained_attributes,
    }
    store = DynamoStage1OperationalStatus(
        target.publications_table,
        target.status_table,
        client,
        lambda: now + timedelta(seconds=5),
    )
    try:
        client.put_item(
            TableName=target.publications_table,
            Item=item,
            ConditionExpression=(
                "attribute_not_exists(partition_key) AND attribute_not_exists(record_key)"
            ),
        )
        snapshot = store.snapshot(partition_key, record_key)
        retained = store.authority(partition_key, record_key)
        assert snapshot is not None
        assert retained is not None
        assert isinstance(retained.member, LifecycleEventAuthority)

        first_observation = now + timedelta(seconds=1)
        observed = store.record_observation(
            RecordStage1AuthorityObservationRequest(snapshot, first_observation)
        )
        replayed_observation = store.record_observation(
            RecordStage1AuthorityObservationRequest(observed, first_observation)
        )
        fresh_candidate_observation = store.record_observation(
            RecordStage1AuthorityObservationRequest(
                observed, first_observation + timedelta(seconds=1)
            )
        )
        retained = store.authority(partition_key, record_key)
        assert retained is not None
        assert isinstance(retained.member, LifecycleEventAuthority)
        assert replayed_observation.authority_observed_at == first_observation
        assert fresh_candidate_observation.authority_observed_at == first_observation
        assert retained.member.event.authority.observed_at == first_observation

        one = store.record_failure(
            RecordStage1OutboxDeliveryFailureRequest(
                observed,
                now + timedelta(seconds=2),
                DependencyErrorClass.QUEUE_SEND_FAILED,
            )
        )
        two = store.record_failure(
            RecordStage1OutboxDeliveryFailureRequest(
                one,
                now + timedelta(seconds=3),
                DependencyErrorClass.DELIVERY_MARK_FAILED,
            )
        )
        protected = store.block(
            BlockStage1OutboxRequest(
                two,
                blocked_at,
                BlockErrorClass.QUEUE_SEND_FAILED,
                Stage1Surface.PLAN,
                case_ref,
            )
        )
        assert protected.authority.outbox_state is OutboxState.BLOCKED

        invocation = Stage1OutboxRecoveryInvocation(case_ref, "integration probe resolved")
        first_recovery = store.invoke(invocation)
        replayed_recovery = store.invoke(invocation)
        assert first_recovery.outcome is RecoveryOutcome.REQUEUED
        assert replayed_recovery.outcome is RecoveryOutcome.ALREADY_REQUEUED

        pending = store.authority(partition_key, record_key)
        assert pending is not None
        assert pending.snapshot.requeued_from_block is not None
        delivered = store.mark_delivered(
            MarkStage1OutboxDeliveredRequest(pending.snapshot, now + timedelta(seconds=6))
        )
        assert delivered.outbox_state is OutboxState.DELIVERED
        with pytest.raises(OperationalStatusError, match="no longer pending"):
            store.invoke(invocation)
        aggregate = client.get_item(
            TableName=target.status_table,
            Key=aggregate_key,
            ConsistentRead=True,
        ).get("Item")
        assert aggregate is not None
        aggregate_item = dict(aggregate)
        assert read_int(aggregate_item, "blocked_dependency") == 0
        assert read_int(aggregate_item, "blocked_invalid") == 0
        assert read_int(aggregate_item, "requeued_pending") == 0
    finally:
        _delete_if_owned(
            client,
            target.publications_table,
            publication_key,
            "immutable_payload_digest = :digest",
            {":digest": {"S": digest}},
        )
        _delete_if_owned(
            client,
            target.status_table,
            case_key,
            "blocked_partition_key = :partition AND immutable_payload_digest = :digest",
            {
                ":partition": {"S": partition_key},
                ":digest": {"S": digest},
            },
        )
        _delete_if_owned(
            client,
            target.status_table,
            membership_key,
            "case_ref = :case_ref",
            {":case_ref": {"S": case_ref}},
        )
        try:
            client.delete_item(
                TableName=target.status_table,
                Key=aggregate_key,
                ConditionExpression="probe_owner = :probe",
                ExpressionAttributeValues={":probe": {"S": probe}},
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
            _LOGGER.warning(
                "ADR-075 aggregate cleanup left the item in place: probe_owner differs",
                extra={"probe": probe},
            )
