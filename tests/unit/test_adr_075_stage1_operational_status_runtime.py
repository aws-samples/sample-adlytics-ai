# SPDX-License-Identifier: MIT-0
"""ADR-075 Stage-1 operational-status adapter and delivery runtime."""

from __future__ import annotations

import importlib
import json
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import boto3
import pytest
from botocore.exceptions import ClientError
from dynamodb_reserved_words import guard_reserved_words

from adapters import config
from adapters.outbound.dynamodb.operational_status import DynamoOperationalStatusStore
from adapters.outbound.dynamodb.stage1_operational_status import (
    OUTBOX_PENDING_INDEX,
    DynamoStage1OperationalStatus,
    DynamoStage1RetainedEventAuthorityReader,
)
from adapters.outbound.sqs.operational_status import (
    SqsOperationalStatusSink,
    parse_message,
    serialize_message,
)
from domain.operations.stage1_operational_status import (
    BlockErrorClass,
    BlockStage1OutboxRequest,
    ChildOutcomeV2,
    DependencyErrorClass,
    EventIdentityRequest,
    LifecycleEventAuthority,
    MarkStage1OutboxDeliveredRequest,
    OperationalDataFlowUsageContribution,
    OperationalDataFlowUsageEvidenceDisposition,
    OutboxState,
    PlanIdentityRequest,
    PlanRequiredChildExpectations,
    ProtectedStage1OutboxCase,
    RecordStage1AuthorityObservationRequest,
    RecordStage1OutboxDeliveryFailureRequest,
    RecoveryOutcome,
    RequeueStage1OutboxRequest,
    RequiredChildExpectation,
    SettleStage1AthenaUsageRequest,
    Stage1AuthorityKind,
    Stage1EventAuthorityRequest,
    Stage1OutboxAuthoritySnapshot,
    Stage1OutboxRecoveryInvocation,
    Stage1Surface,
    UnitIdentityRequest,
    VerifiedStage1EventAuthority,
    derive_event_id,
    derive_plan_identity,
    derive_unit_identity,
    event_authority_digest,
    settle_stage1_athena_usage,
    stage1_authority_time,
)
from domain.operations.status import (
    MEASUREMENT_REFS,
    AuthorityObservation,
    CompletionOutcome,
    EvaluationBinding,
    Evidence,
    FlowState,
    OperationalDataFlowEvent,
    OperationalStatusError,
    ReductionKind,
    canonical_authority_digest,
    event_document,
)
from ports.outbound.operational_status import (
    OperationalDataFlowSurfaceObservation,
    PlanTerminalProjectionRequest,
)
from ports.outbound.stage1_operational_status import (
    Stage1OutboxKey,
    Stage1PendingPage,
    Stage1RetainedAuthority,
)

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-075")]

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
FIRST_OBSERVATION = NOW + timedelta(seconds=1)
DIGEST = "a" * 64
CASE_REF = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
PLAN_FLOW_ID = "11111111-1111-4111-8111-111111111111"
CHILD_FLOW_ID = "22222222-2222-4222-8222-222222222222"
CHILD_EVENT_ID = "33333333-3333-4333-8333-333333333333"
CHILD_EXECUTION_ID = "44444444-4444-4444-8444-444444444444"


def _binding(surface: Stage1Surface) -> EvaluationBinding:
    return EvaluationBinding(MEASUREMENT_REFS[surface.value])


def _event(  # noqa: PLR0913 - closed event fixture overrides
    *,
    event_id: str = "55555555-5555-4555-8555-555555555555",
    flow_id: str = PLAN_FLOW_ID,
    parent_flow_id: str | None = None,
    execution_id: str = "66666666-6666-4666-8666-666666666666",
    sequence: int = 1,
    surface: Stage1Surface = Stage1Surface.PLAN,
    required_child_flow_ids: tuple[str, ...] = (),
) -> OperationalDataFlowEvent:
    return OperationalDataFlowEvent(
        event_id=event_id,
        predecessor_event_id=None,
        flow_id=flow_id,
        parent_flow_id=parent_flow_id,
        causation_event_id=None,
        execution_id=execution_id,
        predecessor_execution_id=None,
        transition_sequence=sequence,
        surface=surface.value,
        stage="projection_terminal",
        state=FlowState.COMPLETED,
        occurred_at=NOW,
        authority=AuthorityObservation("write_plan_meta", DIGEST, NOW),
        evaluation_binding=_binding(surface),
        outcome=CompletionOutcome.SUCCEEDED,
        required_child_flow_ids=required_child_flow_ids,
    )


def _settlement(**changes: object) -> SettleStage1AthenaUsageRequest:
    values: dict[str, object] = {
        "attempt_id": "attempt-fixture",
        "flow_id": CHILD_FLOW_ID,
        "execution_id": CHILD_EXECUTION_ID,
        "evaluation_binding": _binding(Stage1Surface.UNIT),
        "owner_disposition": "successful",
        "observed_at": NOW,
        "provider_terminal_state": "SUCCEEDED",
        "scanned_bytes": 0,
        "engine_ms": 0,
    }
    values.update(changes)
    return SettleStage1AthenaUsageRequest(**values)  # type: ignore[arg-type]


def _contribution() -> OperationalDataFlowUsageContribution:
    authority = settle_stage1_athena_usage(_settlement()).authority
    assert isinstance(authority, OperationalDataFlowUsageContribution)
    return authority


def _disposition() -> OperationalDataFlowUsageEvidenceDisposition:
    authority = settle_stage1_athena_usage(_settlement(engine_ms=None)).authority
    assert isinstance(authority, OperationalDataFlowUsageEvidenceDisposition)
    return authority


def _observation() -> OperationalDataFlowSurfaceObservation:
    return OperationalDataFlowSurfaceObservation(
        observation_id="77777777-7777-4777-8777-777777777777",
        surface=Stage1Surface.PLAN.value,
        observed_at=NOW,
        source_sequence=1,
        evidence=Evidence.INCOMPLETE,
        open_operational_case_count=1,
        reason_code="stage1_outbox_blocked_dependency",
    )


def _snapshot(  # noqa: PLR0913 - closed snapshot fixture overrides
    *,
    record_key: str = "EVENT#fixture",
    kind: Stage1AuthorityKind = Stage1AuthorityKind.LIFECYCLE_EVENT,
    attempts: int = 0,
    state: OutboxState = OutboxState.PENDING,
) -> Stage1OutboxAuthoritySnapshot:
    return Stage1OutboxAuthoritySnapshot(
        partition_key="WRITE_PLAN#fixture",
        record_key=record_key,
        immutable_payload_digest=DIGEST,
        authority_kind=kind,
        outbox_state=state,
        consecutive_failed_delivery_attempts=attempts,
        outbox_order=(f"{'0' * 20}#{DIGEST}" if state is OutboxState.PENDING else None),
    )


def _retained(
    event: OperationalDataFlowEvent | None = None, *, attempts: int = 0
) -> Stage1RetainedAuthority:
    return Stage1RetainedAuthority(
        _snapshot(attempts=attempts), LifecycleEventAuthority(event or _event())
    )


def _outcome(*, sequence: int = 1, generation: int = 7) -> ChildOutcomeV2:
    return ChildOutcomeV2(
        parent_flow_id=PLAN_FLOW_ID,
        child_flow_id=CHILD_FLOW_ID,
        child_event_id=CHILD_EVENT_ID,
        child_transition_sequence=sequence,
        child_execution_id=CHILD_EXECUTION_ID,
        owner_generation=generation,
        lifecycle_state=FlowState.COMPLETED,
        completion_outcome=CompletionOutcome.SUCCEEDED,
    )


@dataclass
class _RecordingSqs:
    requests: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])

    def send_message(self, **request: Any) -> dict[str, object]:
        self.requests.append(request)
        return {}


@dataclass
class _Queue:
    actions: list[str]
    fail_event_ids: set[str] = field(default_factory=set[str])
    requests: list[Any] = field(default_factory=list[Any])

    def send(self, request: Any) -> None:
        self.actions.append("send")
        self.requests.append(request)
        member = request.member
        event_id = member.event.event_id if isinstance(member, LifecycleEventAuthority) else ""
        if event_id in self.fail_event_ids:
            raise RuntimeError("fictional queue failure")


@dataclass
class _MemoryState:
    retained: dict[tuple[str, str], Stage1RetainedAuthority]
    actions: list[str] = field(default_factory=list[str])
    observations: list[datetime] = field(default_factory=list[datetime])
    blocks: list[Any] = field(default_factory=list[Any])
    attempted_keys: list[Stage1OutboxKey] = field(default_factory=list[Stage1OutboxKey])

    def snapshot(self, partition_key: str, record_key: str) -> Stage1OutboxAuthoritySnapshot | None:
        found = self.retained.get((partition_key, record_key))
        return None if found is None else found.snapshot

    def authority(self, partition_key: str, record_key: str) -> Stage1RetainedAuthority | None:
        key = Stage1OutboxKey(partition_key, record_key)
        self.attempted_keys.append(key)
        return self.retained.get((partition_key, record_key))

    def pending(self, limit: int) -> Stage1PendingPage:
        return Stage1PendingPage(
            tuple(Stage1OutboxKey(*key) for key in tuple(self.retained)[:limit])
        )

    def record_observation(
        self, request: RecordStage1AuthorityObservationRequest
    ) -> Stage1OutboxAuthoritySnapshot:
        self.actions.append("observe")
        self.observations.append(request.observed_at)
        key = (request.authority.partition_key, request.authority.record_key)
        retained = self.retained[key]
        persisted = retained.snapshot.authority_observed_at or request.observed_at
        snapshot = replace(retained.snapshot, authority_observed_at=persisted)
        assert isinstance(retained.member, LifecycleEventAuthority)
        event = replace(
            retained.member.event,
            authority=replace(retained.member.event.authority, observed_at=persisted),
        )
        self.retained[key] = Stage1RetainedAuthority(snapshot, LifecycleEventAuthority(event))
        return snapshot

    def mark_delivered(
        self, request: MarkStage1OutboxDeliveredRequest
    ) -> Stage1OutboxAuthoritySnapshot:
        self.actions.append("mark")
        key = (request.authority.partition_key, request.authority.record_key)
        retained = self.retained[key]
        delivered = replace(
            retained.snapshot, outbox_state=OutboxState.DELIVERED, outbox_order=None
        )
        self.retained[key] = Stage1RetainedAuthority(delivered, retained.member)
        return delivered

    def record_failure(
        self, request: RecordStage1OutboxDeliveryFailureRequest
    ) -> Stage1OutboxAuthoritySnapshot:
        self.actions.append("failure")
        key = (request.authority.partition_key, request.authority.record_key)
        retained = self.retained[key]
        failed = replace(
            retained.snapshot,
            consecutive_failed_delivery_attempts=(
                retained.snapshot.consecutive_failed_delivery_attempts + 1
            ),
        )
        self.retained[key] = Stage1RetainedAuthority(failed, retained.member)
        return failed

    def block(self, request: Any) -> ProtectedStage1OutboxCase:
        self.actions.append("block")
        self.blocks.append(request)
        key = (request.authority.partition_key, request.authority.record_key)
        retained = self.retained[key]
        blocked = replace(
            retained.snapshot,
            outbox_state=OutboxState.BLOCKED,
            outbox_order=None,
            consecutive_failed_delivery_attempts=(
                retained.snapshot.consecutive_failed_delivery_attempts + 1
            ),
        )
        self.retained[key] = Stage1RetainedAuthority(blocked, retained.member)
        return ProtectedStage1OutboxCase(
            request.case_ref,
            blocked,
            (request.selected_surface or Stage1Surface.PLAN,),
            request.blocked_at,
            "stage1_outbox_blocked_dependency",
        )

    def requeue(self, request: Any) -> Stage1OutboxAuthoritySnapshot:
        raise AssertionError(f"unexpected requeue: {request}")


@dataclass
class _RecordingProjection:
    messages: list[Any] = field(default_factory=list[Any])
    child_outcomes: list[ChildOutcomeV2] = field(default_factory=list[ChildOutcomeV2])
    plan_terminals: list[Any] = field(default_factory=list[Any])
    reduce_kind: ReductionKind = ReductionKind.APPLY

    def reduce(self, message: Any) -> Any:
        self.messages.append(message)
        return SimpleNamespace(kind=self.reduce_kind, applied_count=1)

    def project_child_outcome(self, outcome: ChildOutcomeV2) -> Any:
        self.child_outcomes.append(outcome)
        return SimpleNamespace(kind=ReductionKind.APPLY, applied_count=1)

    def reduce_plan_terminal(self, request: Any) -> Any:
        self.plan_terminals.append(request)
        return SimpleNamespace(kind=ReductionKind.APPLY, applied_count=1)


@guard_reserved_words
@dataclass
class _RecordingDynamo:
    items: dict[tuple[str, str], dict[str, Any]] = field(
        default_factory=dict[tuple[str, str], dict[str, Any]]
    )
    queries: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    gets: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    updates: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    transactions: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    batches: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])

    @staticmethod
    def _key(request: dict[str, Any]) -> tuple[str, str]:
        key = cast("dict[str, dict[str, str]]", request["Key"])
        sort_name = "sort_key" if "sort_key" in key else "record_key"
        return key["partition_key"]["S"], key[sort_name]["S"]

    @staticmethod
    def item_key(item: dict[str, Any]) -> tuple[str, str]:
        sort_name = "sort_key" if "sort_key" in item else "record_key"
        return item["partition_key"]["S"], item[sort_name]["S"]

    def get_item(self, **request: Any) -> dict[str, object]:
        self.gets.append(request)
        item = self.items.get(self._key(request))
        return {} if item is None else {"Item": item}

    def query(self, **request: Any) -> dict[str, object]:
        self.queries.append(request)
        if request.get("IndexName") != OUTBOX_PENDING_INDEX:
            return {"Items": []}
        return {
            "Items": [
                {
                    "partition_key": {"S": "WRITE_PLAN#integration-probe-fixture"},
                    "record_key": {"S": "EVENT#integration-probe-fixture"},
                }
            ]
        }

    def batch_get_item(self, **request: Any) -> dict[str, object]:
        self.batches.append(request)
        request_items = cast("dict[str, dict[str, Any]]", request["RequestItems"])
        table, options = next(iter(request_items.items()))
        found: list[dict[str, Any]] = []
        keys = cast("list[dict[str, dict[str, str]]]", options["Keys"])
        for key in keys:
            item = self.items.get((key["partition_key"]["S"], key["sort_key"]["S"]))
            if item is not None:
                found.append(item)
        return {"Responses": {table: found}}

    def update_item(self, **request: Any) -> dict[str, object]:
        self.updates.append(request)
        key = self._key(request)
        stored = self.items[key]
        values = cast("dict[str, dict[str, Any]]", request["ExpressionAttributeValues"])
        expression = cast("str", request["UpdateExpression"])
        if "authority_observed_at" in stored and "authority_observed_at" in expression:
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException", "Message": "fixture"}},
                "UpdateItem",
            )
        if ":observed" in values:
            stored["authority_observed_at"] = values[":observed"]
        if ":next_count" in values:
            stored["consecutive_failed_delivery_attempts"] = values[":next_count"]
        if ":delivered" in values:
            stored["outbox_state"] = values[":delivered"]
            stored.pop("outbox_status", None)
            stored.pop("outbox_order", None)
        return {}

    def transact_write_items(self, **request: Any) -> dict[str, object]:
        self.transactions.append(request)
        for action in request["TransactItems"]:
            if "Put" in action:
                item = action["Put"]["Item"]
                self.items[self.item_key(item)] = item
            elif "Update" in action:
                update = action["Update"]
                key = self._key(update)
                item = self.items.setdefault(key, dict(update["Key"]))
                values = update["ExpressionAttributeValues"]
                if ":applied" in values:
                    item["disposition"] = values[":applied"]
                if ":blocked" in values and ":final_count" in values:
                    item["outbox_state"] = values[":blocked"]
                    item["consecutive_failed_delivery_attempts"] = values[":final_count"]
                    item.pop("outbox_status", None)
                    item.pop("outbox_order", None)
                if ":marker" in values:
                    item["outbox_state"] = values[":pending"]
                    item["outbox_status"] = values[":pending"]
                    item["outbox_order"] = values[":order"]
                    item["consecutive_failed_delivery_attempts"] = values[":zero"]
                    item["requeued_from_block"] = values[":marker"]
                if ":resolution" in values:
                    item["state"] = values[":requeued"]
                    item["requeued_at"] = values[":at"]
                    item["resolution_reason"] = values[":resolution"]
                    item["requeue_audit"] = values[":audit"]
                if ":dependency" in values:
                    item["blocked_dependency"] = values[":dependency"]
                    item["blocked_invalid"] = values[":invalid"]
                    item["requeued_pending"] = values[":requeued"]
                    item["source_sequence"] = values[":next_sequence"]
                    item["report_id"] = values[":report_id"]
            elif "Delete" in action:
                self.items.pop(self._key(action["Delete"]), None)
        return {}


PLAN_KEY_FIXTURE = "WRITE_PLAN#fixture"
PLAN_DIGEST_FIXTURE = "plan-fixture"
_PLAN_IDENTITY = derive_plan_identity(
    PlanIdentityRequest(PLAN_DIGEST_FIXTURE, "delivery-execution-fixture")
)
_UNIT_IDENTITY = derive_unit_identity(
    UnitIdentityRequest(PLAN_DIGEST_FIXTURE, "unit-fixture", "writer-execution-fixture")
)


def _derived_plan_authority() -> Stage1EventAuthorityRequest:
    return Stage1EventAuthorityRequest(
        surface=Stage1Surface.PLAN,
        internal_key=(PLAN_KEY_FIXTURE,),
        transition_id="semantic_plan_terminal",
        lifecycle_state=FlowState.COMPLETED,
        completion_outcome=CompletionOutcome.SUCCEEDED,
        typed_reason=None,
        authority_time=stage1_authority_time(NOW),
    )


def _derived_plan_event() -> OperationalDataFlowEvent:
    digest = event_authority_digest(_derived_plan_authority())
    return OperationalDataFlowEvent(
        event_id=derive_event_id(
            EventIdentityRequest(Stage1Surface.PLAN, digest, 1, FlowState.COMPLETED)
        ),
        predecessor_event_id=None,
        flow_id=_PLAN_IDENTITY.flow_id,
        execution_id=_PLAN_IDENTITY.execution_id,
        predecessor_execution_id=None,
        transition_sequence=1,
        surface=Stage1Surface.PLAN.value,
        stage="projection_terminal",
        state=FlowState.COMPLETED,
        occurred_at=NOW,
        authority=AuthorityObservation("stage1_lifecycle_head", digest, NOW),
        evaluation_binding=_binding(Stage1Surface.PLAN),
        outcome=CompletionOutcome.SUCCEEDED,
        required_child_flow_ids=(_UNIT_IDENTITY.flow_id,),
    )


def _publication_item() -> dict[str, Any]:
    lifecycle = _derived_plan_event()
    payload = event_document(lifecycle)
    snapshot = _snapshot()
    return {
        "partition_key": {"S": snapshot.partition_key},
        "record_key": {"S": snapshot.record_key},
        "immutable_payload_digest": {"S": canonical_authority_digest(payload)},
        "authority_kind": {"S": snapshot.authority_kind.value},
        "outbox_state": {"S": snapshot.outbox_state.value},
        "outbox_status": {"S": snapshot.outbox_state.value},
        "outbox_order": {"S": cast("str", snapshot.outbox_order)},
        "consecutive_failed_delivery_attempts": {"N": "0"},
        "payload": {
            "S": json.dumps(
                payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
            )
        },
        "surface": {"S": Stage1Surface.PLAN.value},
        "internal_key": {"L": [{"S": PLAN_KEY_FIXTURE}]},
        "transition_id": {"S": "semantic_plan_terminal"},
        "lifecycle_state": {"S": FlowState.COMPLETED.value},
        "completion_outcome": {"S": CompletionOutcome.SUCCEEDED.value},
        "authority_time": {"S": stage1_authority_time(NOW)},
        "plan_digest": {"S": PLAN_DIGEST_FIXTURE},
        "internal_execution_id": {"S": "delivery-execution-fixture"},
        "flow_id": {"S": lifecycle.flow_id},
        "execution_id": {"S": lifecycle.execution_id},
        "event_id": {"S": lifecycle.event_id},
        "transition_sequence": {"N": "1"},
        "child_outcome_reduction_version": {"N": "2"},
        "required_child_expectations": {
            "L": [
                {
                    "M": {
                        "child_flow_id": {"S": _UNIT_IDENTITY.flow_id},
                        "owner_generation": {"N": "7"},
                    }
                }
            ]
        },
    }


def _verified_from(
    event: OperationalDataFlowEvent,
    *,
    owner_generation: int | None = None,
    expectations: PlanRequiredChildExpectations | None = None,
) -> VerifiedStage1EventAuthority:
    return VerifiedStage1EventAuthority(
        surface=Stage1Surface(event.surface),
        authority_digest=event.authority.opaque_digest,
        flow_id=event.flow_id,
        execution_id=event.execution_id,
        event_id=event.event_id,
        transition_sequence=event.transition_sequence,
        predecessor_event_id=event.predecessor_event_id,
        predecessor_execution_id=event.predecessor_execution_id,
        parent_flow_id=event.parent_flow_id,
        causation_event_id=event.causation_event_id,
        lifecycle_state=event.state,
        completion_outcome=event.outcome,
        owner_generation=owner_generation,
        required_child_expectations=expectations,
    )


@dataclass
class _RecordingRetained:
    verified: VerifiedStage1EventAuthority | None = None
    requests: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])

    def retained_event_authority(
        self, flow_id: str, opaque_digest: str
    ) -> VerifiedStage1EventAuthority | None:
        self.requests.append((flow_id, opaque_digest))
        return self.verified


@pytest.fixture
def runtime_modules(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Any, Any, Any]]:
    monkeypatch.setattr(
        config,
        "stage1_lifecycle_producer_settings",
        lambda: SimpleNamespace(
            publications_table="publications-table",
            status_table="status-table",
            queue_url="https://sqs.test.invalid/status.fifo",
            reconcile_batch_size=10,
            max_consecutive_failed_delivery_attempts=3,
        ),
    )
    monkeypatch.setattr(
        config,
        "stage1_outbox_recovery_settings",
        lambda: SimpleNamespace(
            publications_table="publications-table", status_table="status-table"
        ),
    )
    monkeypatch.setattr(
        config,
        "operational_projector_settings",
        lambda: SimpleNamespace(
            status_table="status-table", publications_table="publications-table"
        ),
    )
    monkeypatch.setattr(
        config,
        "tracing_settings",
        lambda: SimpleNamespace(enabled=False, sample_ratio=0.0),
    )
    dynamo = _RecordingDynamo()
    sqs = _RecordingSqs()

    def recording_client(service: str, **_kwargs: object) -> object:
        return dynamo if service == "dynamodb" else sqs

    monkeypatch.setattr(boto3, "client", recording_client)
    names = (
        "adapters.jobs.operational_status.stage1_lifecycle",
        "adapters.jobs.operational_status.stage1_outbox_recovery",
        "adapters.jobs.operational_status.projector",
    )
    for name in names:
        sys.modules.pop(name, None)
    modules = tuple(importlib.import_module(name) for name in names)
    yield cast("tuple[Any, Any, Any]", modules)
    for name in names:
        sys.modules.pop(name, None)


def test_four_member_codec_and_fifo_identities_are_exact() -> None:
    client = _RecordingSqs()
    sink = SqsOperationalStatusSink("https://sqs.test.invalid/status.fifo", cast("Any", client))
    members = (_event(), _observation(), _contribution(), _disposition())

    for member in members:
        encoded = serialize_message(member)
        assert parse_message(encoded) == member
        sink.send(member)

    assert [request["MessageGroupId"] for request in client.requests] == [
        PLAN_FLOW_ID,
        f"surface#{Stage1Surface.PLAN.value}",
        CHILD_FLOW_ID,
        CHILD_FLOW_ID,
    ]
    assert [request["MessageDeduplicationId"] for request in client.requests] == [
        _event().event_id,
        _observation().observation_id,
        _contribution().contribution_id,
        _disposition().disposition_id,
    ]


def test_projector_dispatches_the_closed_public_union(
    runtime_modules: tuple[Any, Any, Any],
) -> None:
    projector_module = runtime_modules[2]
    store = _RecordingProjection()
    retained = _RecordingRetained()
    projector = projector_module.Projector(cast("Any", store), cast("Any", retained))
    nonterminal = replace(_event(), state=FlowState.RUNNING, outcome=None)
    members = (nonterminal, _observation(), _contribution(), _disposition())

    for member in members:
        projector.apply(serialize_message(member))

    assert store.messages == list(members)
    assert retained.requests == []


def test_projector_joins_unit_terminal_and_projects_child_outcome_after_reduce(
    runtime_modules: tuple[Any, Any, Any],
) -> None:
    projector_module = runtime_modules[2]
    unit_event = _event(
        event_id=CHILD_EVENT_ID,
        flow_id=CHILD_FLOW_ID,
        parent_flow_id=PLAN_FLOW_ID,
        execution_id=CHILD_EXECUTION_ID,
        surface=Stage1Surface.UNIT,
    )
    retained = _RecordingRetained(_verified_from(unit_event, owner_generation=7))
    store = _RecordingProjection()
    projector = projector_module.Projector(cast("Any", store), cast("Any", retained))

    projector.apply(serialize_message(unit_event))

    assert retained.requests == [(CHILD_FLOW_ID, DIGEST)]
    assert store.messages == [unit_event]
    assert store.plan_terminals == []
    assert store.child_outcomes == [
        ChildOutcomeV2(
            parent_flow_id=PLAN_FLOW_ID,
            child_flow_id=CHILD_FLOW_ID,
            child_event_id=CHILD_EVENT_ID,
            child_transition_sequence=1,
            child_execution_id=CHILD_EXECUTION_ID,
            owner_generation=7,
            lifecycle_state=FlowState.COMPLETED,
            completion_outcome=CompletionOutcome.SUCCEEDED,
        )
    ]


def test_projector_parks_unit_terminal_without_child_outcome(
    runtime_modules: tuple[Any, Any, Any],
) -> None:
    projector_module = runtime_modules[2]
    unit_event = _event(
        event_id=CHILD_EVENT_ID,
        flow_id=CHILD_FLOW_ID,
        parent_flow_id=PLAN_FLOW_ID,
        execution_id=CHILD_EXECUTION_ID,
        surface=Stage1Surface.UNIT,
    )
    retained = _RecordingRetained(_verified_from(unit_event, owner_generation=7))
    store = _RecordingProjection(reduce_kind=ReductionKind.PARK)
    projector = projector_module.Projector(cast("Any", store), cast("Any", retained))

    projector.apply(serialize_message(unit_event))

    assert store.messages == [unit_event]
    assert store.child_outcomes == []


@pytest.mark.parametrize(
    ("terminal_state", "completion_outcome"),
    [
        (FlowState.COMPLETED, CompletionOutcome.SUCCEEDED),
        (FlowState.BLOCKED, CompletionOutcome.COMPLETED_REFUSED),
        (FlowState.SUPERSEDED, CompletionOutcome.SUPERSEDED),
    ],
)
def test_projector_joins_plan_terminals_to_retained_expectations(
    runtime_modules: tuple[Any, Any, Any],
    terminal_state: FlowState,
    completion_outcome: CompletionOutcome,
) -> None:
    projector_module = runtime_modules[2]
    plan_event = replace(
        _event(required_child_flow_ids=(CHILD_FLOW_ID,)),
        state=terminal_state,
        outcome=completion_outcome,
    )
    expectations = PlanRequiredChildExpectations(
        PLAN_FLOW_ID, (RequiredChildExpectation(CHILD_FLOW_ID, 7),)
    )
    retained = _RecordingRetained(_verified_from(plan_event, expectations=expectations))
    store = _RecordingProjection()
    projector = projector_module.Projector(cast("Any", store), cast("Any", retained))

    projector.apply(serialize_message(plan_event))

    assert retained.requests == [(PLAN_FLOW_ID, DIGEST)]
    assert store.messages == []
    assert store.child_outcomes == []
    assert store.plan_terminals == [PlanTerminalProjectionRequest(plan_event, expectations)]


def test_projector_reduces_pointer_and_inline_terminals_without_retained_reads(
    runtime_modules: tuple[Any, Any, Any],
) -> None:
    projector_module = runtime_modules[2]
    retained = _RecordingRetained()
    store = _RecordingProjection()
    projector = projector_module.Projector(cast("Any", store), cast("Any", retained))
    members = (
        _event(surface=Stage1Surface.POINTER),
        _event(surface=Stage1Surface.INLINE_RUN),
    )

    for member in members:
        projector.apply(serialize_message(member))

    assert store.messages == list(members)
    assert retained.requests == []
    assert store.child_outcomes == []
    assert store.plan_terminals == []


def test_projector_rejects_absent_or_mismatched_retained_authority_into_batch_failures(
    runtime_modules: tuple[Any, Any, Any],
) -> None:
    projector_module = runtime_modules[2]
    plan_event = _event(required_child_flow_ids=(CHILD_FLOW_ID,))
    expectations = PlanRequiredChildExpectations(
        PLAN_FLOW_ID, (RequiredChildExpectation(CHILD_FLOW_ID, 7),)
    )
    mismatched = replace(
        _verified_from(plan_event, expectations=expectations), authority_digest="b" * 64
    )
    for verified in (None, mismatched):
        store = _RecordingProjection()
        projector = projector_module.Projector(
            cast("Any", store), cast("Any", _RecordingRetained(verified))
        )

        failures = projector_module.apply_records(
            {"Records": [{"messageId": "m1", "body": serialize_message(plan_event)}]},
            projector,
        )

        assert failures == ("m1",)
        assert store.messages == []
        assert store.plan_terminals == []


def test_delivery_uses_candidate_observation_then_sends_before_mark(
    runtime_modules: tuple[Any, Any, Any],
) -> None:
    lifecycle = runtime_modules[0]
    retained = _retained()
    key = (retained.snapshot.partition_key, retained.snapshot.record_key)
    actions: list[str] = []
    state = _MemoryState({key: retained}, actions)
    queue = _Queue(actions)
    delivery = lifecycle.Stage1AuthorityDelivery(state, queue, 3)

    result = delivery.deliver(Stage1OutboxKey(*key), FIRST_OBSERVATION)

    assert result.outcome is lifecycle.DeliveryOutcome.DELIVERED
    assert actions == ["observe", "send", "mark"]
    assert state.observations == [FIRST_OBSERVATION]
    sent = queue.requests[0].member
    assert isinstance(sent, LifecycleEventAuthority)
    assert sent.event.authority.observed_at == FIRST_OBSERVATION


def test_observation_replay_reuses_the_persisted_instant_even_under_a_fresh_candidate() -> None:
    """Prevent retried lifecycle deliveries from blocking themselves as poison.

    The set-once instant is immutable authority: a redelivery's fresh wall instant reuses it.
    """
    client = _RecordingDynamo()
    item = _publication_item()
    key = _RecordingDynamo.item_key(item)
    client.items[key] = item
    store = DynamoStage1OperationalStatus("publications-table", "status-table", cast("Any", client))
    original = store.authority(*key)
    assert original is not None

    first = store.record_observation(
        RecordStage1AuthorityObservationRequest(original.snapshot, FIRST_OBSERVATION)
    )
    second = store.record_observation(
        RecordStage1AuthorityObservationRequest(first, FIRST_OBSERVATION)
    )
    fresh_candidate = store.record_observation(
        RecordStage1AuthorityObservationRequest(first, FIRST_OBSERVATION + timedelta(minutes=1))
    )
    reconstructed = store.authority(*key)

    assert first.authority_observed_at == FIRST_OBSERVATION
    assert second.authority_observed_at == FIRST_OBSERVATION
    assert fresh_candidate.authority_observed_at == FIRST_OBSERVATION
    assert reconstructed is not None
    assert isinstance(reconstructed.member, LifecycleEventAuthority)
    assert reconstructed.member.event.authority.observed_at == FIRST_OBSERVATION
    assert all(request["ConsistentRead"] is True for request in client.gets)


def test_observation_conflict_with_a_changed_authority_fails_closed() -> None:
    client = _RecordingDynamo()
    item = _publication_item()
    key = _RecordingDynamo.item_key(item)
    client.items[key] = item
    store = DynamoStage1OperationalStatus("publications-table", "status-table", cast("Any", client))
    original = store.authority(*key)
    assert original is not None
    store.record_observation(
        RecordStage1AuthorityObservationRequest(original.snapshot, FIRST_OBSERVATION)
    )

    client.items[key]["immutable_payload_digest"] = {"S": "ab" * 32}

    with pytest.raises(OperationalStatusError, match="lost a changed authority"):
        store.record_observation(
            RecordStage1AuthorityObservationRequest(original.snapshot, FIRST_OBSERVATION)
        )


def test_observation_conflict_with_a_corrupted_persisted_instant_fails_closed() -> None:
    """Never return a replay snapshot built on corrupt state.

    A corrupt persisted instant fails the snapshot reparse inside the same-authority check.
    """
    client = _RecordingDynamo()
    item = _publication_item()
    key = _RecordingDynamo.item_key(item)
    client.items[key] = item
    store = DynamoStage1OperationalStatus("publications-table", "status-table", cast("Any", client))
    original = store.authority(*key)
    assert original is not None
    store.record_observation(
        RecordStage1AuthorityObservationRequest(original.snapshot, FIRST_OBSERVATION)
    )

    client.items[key]["authority_observed_at"] = {"S": "not-an-instant"}

    with pytest.raises(OperationalStatusError, match="lost a changed authority"):
        store.record_observation(
            RecordStage1AuthorityObservationRequest(original.snapshot, FIRST_OBSERVATION)
        )


def test_stream_and_schedule_share_delivery_and_stream_uses_sequence_number(
    runtime_modules: tuple[Any, Any, Any],
) -> None:
    lifecycle = runtime_modules[0]
    event = _event()
    retained = _retained(event)
    key = (retained.snapshot.partition_key, retained.snapshot.record_key)
    state = _MemoryState({key: retained})
    queue = _Queue([], {event.event_id})
    delivery = lifecycle.Stage1AuthorityDelivery(state, queue, 3)
    stream: dict[str, Any] = {
        "Records": [
            {
                "eventName": "INSERT",
                "dynamodb": {
                    "SequenceNumber": "987654321",
                    "NewImage": {
                        "partition_key": {"S": key[0]},
                        "record_key": {"S": key[1]},
                    },
                },
            }
        ]
    }

    assert lifecycle.apply_stream(stream, delivery) == ("987654321",)
    assert lifecycle.apply_schedule(delivery, 10)["retry"] == 1

    stream["Records"][0]["dynamodb"].pop("SequenceNumber")
    assert lifecycle.apply_stream(stream, delivery) == ()


def test_failure_counts_zero_one_two_then_blocks_at_configured_three(
    runtime_modules: tuple[Any, Any, Any],
) -> None:
    lifecycle = runtime_modules[0]
    event = _event()
    retained = _retained(event)
    key = (retained.snapshot.partition_key, retained.snapshot.record_key)
    state = _MemoryState({key: retained})
    delivery = lifecycle.Stage1AuthorityDelivery(state, _Queue([], {event.event_id}), 3)

    outcomes = [delivery.deliver(Stage1OutboxKey(*key), NOW).outcome for _ in range(3)]

    assert outcomes == [
        lifecycle.DeliveryOutcome.RETRY,
        lifecycle.DeliveryOutcome.RETRY,
        lifecycle.DeliveryOutcome.BLOCKED,
    ]
    assert [action for action in state.actions if action in {"failure", "block"}] == [
        "failure",
        "failure",
        "block",
    ]


def test_schedule_continues_after_block_and_stops_only_below_threshold_retry(
    runtime_modules: tuple[Any, Any, Any],
) -> None:
    lifecycle = runtime_modules[0]
    blocked_event = _event(event_id="88888888-8888-4888-8888-888888888888")
    delivered_event = _event(event_id="99999999-9999-4999-8999-999999999999")
    retry_event = _event(event_id="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
    untouched_event = _event(event_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
    retained_values = (
        _retained(blocked_event, attempts=2),
        _retained(delivered_event),
        _retained(retry_event),
        _retained(untouched_event),
    )
    retained = {
        (value.snapshot.partition_key, f"EVENT#{index}"): Stage1RetainedAuthority(
            replace(value.snapshot, record_key=f"EVENT#{index}"), value.member
        )
        for index, value in enumerate(retained_values)
    }
    state = _MemoryState(retained)
    queue = _Queue([], {blocked_event.event_id, retry_event.event_id})
    delivery = lifecycle.Stage1AuthorityDelivery(state, queue, 3)

    result = lifecycle.apply_schedule(delivery, 10)

    assert result == {
        "queried": 4,
        "delivered": 1,
        "blocked": 1,
        "terminal_noop": 0,
        "retry": 1,
    }
    assert state.attempted_keys[-1].record_key == "EVENT#2"


def test_terminal_authority_is_a_noop(runtime_modules: tuple[Any, Any, Any]) -> None:
    lifecycle = runtime_modules[0]
    retained = _retained()
    terminal = Stage1RetainedAuthority(
        replace(retained.snapshot, outbox_state=OutboxState.DELIVERED, outbox_order=None),
        retained.member,
    )
    key = (terminal.snapshot.partition_key, terminal.snapshot.record_key)
    delivery = lifecycle.Stage1AuthorityDelivery(_MemoryState({key: terminal}), _Queue([]), 3)

    assert (
        delivery.deliver(Stage1OutboxKey(*key), NOW).outcome
        is lifecycle.DeliveryOutcome.TERMINAL_NOOP
    )


def test_pending_query_has_fixed_index_and_no_cursor() -> None:
    client = _RecordingDynamo()
    store = DynamoStage1OperationalStatus("publications-table", "status-table", cast("Any", client))

    page = store.pending(10)

    assert page.keys[0].record_key == "EVENT#integration-probe-fixture"
    assert client.queries == [
        {
            "TableName": "publications-table",
            "IndexName": OUTBOX_PENDING_INDEX,
            "KeyConditionExpression": "outbox_status = :pending",
            "ExpressionAttributeValues": {":pending": {"S": "pending"}},
            "Limit": 10,
            "ScanIndexForward": True,
        }
    ]
    assert "ExclusiveStartKey" not in client.queries[0]


def test_recovery_replay_requires_exact_pending_marker(
    runtime_modules: tuple[Any, Any, Any],
) -> None:
    recovery_module = runtime_modules[1]
    client = _RecordingDynamo()
    blocked = _snapshot(state=OutboxState.BLOCKED, attempts=3)
    case_key = (f"CASE#{CASE_REF}", "STAGE1_OUTBOX")
    client.items[case_key] = {
        "partition_key": {"S": case_key[0]},
        "sort_key": {"S": case_key[1]},
        "state": {"S": "requeued"},
        "affected_surfaces": {"L": [{"S": Stage1Surface.PLAN.value}]},
        "blocked_partition_key": {"S": blocked.partition_key},
        "blocked_record_key": {"S": blocked.record_key},
        "immutable_payload_digest": {"S": blocked.immutable_payload_digest},
        "authority_kind": {"S": blocked.authority_kind.value},
        "failed_delivery_attempts": {"N": "3"},
        "blocked_at": {"S": NOW.isoformat()},
        "reason_code": {"S": "stage1_outbox_blocked_dependency"},
        "resolution_reason": {"S": "dependency resolved"},
        "requeued_at": {"S": FIRST_OBSERVATION.isoformat()},
        "requeue_audit": {
            "L": [
                {
                    "M": {
                        "state": {"S": "requeued"},
                        "at": {"S": FIRST_OBSERVATION.isoformat()},
                        "reason": {"S": "dependency resolved"},
                    }
                }
            ]
        },
    }
    publication_key = (blocked.partition_key, blocked.record_key)
    publication = {
        "partition_key": {"S": blocked.partition_key},
        "record_key": {"S": blocked.record_key},
        "immutable_payload_digest": {"S": blocked.immutable_payload_digest},
        "authority_kind": {"S": blocked.authority_kind.value},
        "outbox_state": {"S": OutboxState.PENDING.value},
        "outbox_status": {"S": OutboxState.PENDING.value},
        "outbox_order": {"S": f"{'0' * 20}#{DIGEST}"},
        "consecutive_failed_delivery_attempts": {"N": "0"},
        "requeued_from_block": {
            "M": {
                "case_ref": {"S": CASE_REF},
                "affected_surfaces": {"L": [{"S": Stage1Surface.PLAN.value}]},
            }
        },
    }
    client.items[publication_key] = publication
    store = DynamoStage1OperationalStatus("publications-table", "status-table", cast("Any", client))
    document = {
        "schema_version": "1",
        "case_ref": CASE_REF,
        "resolution_reason": "dependency resolved",
    }
    invocation = Stage1OutboxRecoveryInvocation(CASE_REF, "dependency resolved")

    assert recovery_module.parse_invocation(document) == invocation
    assert store.invoke(invocation).outcome is RecoveryOutcome.ALREADY_REQUEUED

    for marker in (
        {
            "case_ref": {"S": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"},
            "affected_surfaces": {"L": [{"S": Stage1Surface.PLAN.value}]},
        },
        {
            "case_ref": {"S": CASE_REF},
            "affected_surfaces": {"L": [{"S": Stage1Surface.UNIT.value}]},
        },
    ):
        publication["requeued_from_block"] = {"M": marker}
        with pytest.raises(OperationalStatusError, match="no longer pending"):
            store.invoke(invocation)

    publication["outbox_state"] = {"S": OutboxState.DELIVERED.value}
    publication.pop("outbox_status")
    publication.pop("outbox_order")
    publication.pop("requeued_from_block")
    with pytest.raises(OperationalStatusError, match="no longer pending"):
        store.invoke(invocation)

    publication["outbox_state"] = {"S": OutboxState.BLOCKED.value}
    publication["consecutive_failed_delivery_attempts"] = {"N": "3"}
    with pytest.raises(OperationalStatusError, match="no longer pending"):
        store.invoke(invocation)

    assert client.transactions == []
    assert {request["TableName"] for request in client.gets} == {
        "publications-table",
        "status-table",
    }
    with pytest.raises(OperationalStatusError, match="audit differs"):
        store.invoke(Stage1OutboxRecoveryInvocation(CASE_REF, "different resolution"))
    with pytest.raises(OperationalStatusError, match="unavailable"):
        store.invoke(
            Stage1OutboxRecoveryInvocation(
                "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", "dependency resolved"
            )
        )
    with pytest.raises(OperationalStatusError, match="fields differ"):
        recovery_module.parse_invocation({**document, "blocked_record_key": "private"})


def test_outbox_mutations_and_block_members_are_conditional() -> None:
    client = _RecordingDynamo()
    item = _publication_item()
    key = _RecordingDynamo.item_key(item)
    client.items[key] = item
    store = DynamoStage1OperationalStatus("publications-table", "status-table", cast("Any", client))
    retained = store.authority(*key)
    assert retained is not None

    observed = store.record_observation(
        RecordStage1AuthorityObservationRequest(retained.snapshot, FIRST_OBSERVATION)
    )
    failed = store.record_failure(
        RecordStage1OutboxDeliveryFailureRequest(
            observed, FIRST_OBSERVATION, DependencyErrorClass.QUEUE_SEND_FAILED
        )
    )
    store.mark_delivered(MarkStage1OutboxDeliveredRequest(failed, FIRST_OBSERVATION))

    assert all("ConditionExpression" in request for request in client.updates)
    assert "attribute_not_exists(authority_observed_at)" in client.updates[0]["ConditionExpression"]
    assert (
        "consecutive_failed_delivery_attempts = :failed_count"
        in client.updates[1]["ConditionExpression"]
    )
    assert "attribute_not_exists(requeued_from_block)" in client.updates[2]["ConditionExpression"]


def test_block_and_requeue_transactions_condition_every_member() -> None:
    client = _RecordingDynamo()
    item = _publication_item()
    key = _RecordingDynamo.item_key(item)
    client.items[key] = item
    store = DynamoStage1OperationalStatus("publications-table", "status-table", cast("Any", client))
    retained = store.authority(*key)
    assert retained is not None
    one = store.record_failure(
        RecordStage1OutboxDeliveryFailureRequest(
            retained.snapshot, NOW, DependencyErrorClass.QUEUE_SEND_FAILED
        )
    )
    two = store.record_failure(
        RecordStage1OutboxDeliveryFailureRequest(
            one, NOW + timedelta(seconds=1), DependencyErrorClass.QUEUE_SEND_FAILED
        )
    )

    protected = store.block(
        BlockStage1OutboxRequest(
            two,
            NOW + timedelta(seconds=2),
            BlockErrorClass.QUEUE_SEND_FAILED,
            Stage1Surface.PLAN,
            CASE_REF,
        )
    )
    requeued = store.requeue(
        RequeueStage1OutboxRequest(protected, NOW + timedelta(seconds=3), "dependency resolved")
    )

    assert requeued.outbox_state is OutboxState.PENDING
    assert requeued.requeued_from_block is not None
    for transaction in client.transactions:
        assert all(
            "ConditionExpression" in next(iter(action.values()))
            for action in transaction["TransactItems"]
        )


@pytest.mark.parametrize(
    "surface",
    [Stage1Surface.POINTER, Stage1Surface.INLINE_RUN, Stage1Surface.PLAN],
)
def test_child_head_rejects_applied_nonunit_surfaces_without_writes(
    surface: Stage1Surface,
) -> None:
    client = _RecordingDynamo()
    store = DynamoOperationalStatusStore("status-table", cast("Any", client))
    child_event = _event(
        event_id=CHILD_EVENT_ID,
        flow_id=CHILD_FLOW_ID,
        parent_flow_id=PLAN_FLOW_ID,
        execution_id=CHILD_EXECUTION_ID,
        sequence=1,
        surface=surface,
    )
    applied_key = (f"EVENT#{CHILD_EVENT_ID}", "APPLIED")
    client.items[applied_key] = {
        "partition_key": {"S": applied_key[0]},
        "sort_key": {"S": applied_key[1]},
        "payload": {
            "S": json.dumps(event_document(child_event), sort_keys=True, separators=(",", ":"))
        },
    }

    with pytest.raises(OperationalStatusError, match="head conflicts"):
        store.project_child_outcome(_outcome())

    assert client.transactions == []


def test_child_head_requires_applied_contiguity_and_conditional_writes() -> None:
    client = _RecordingDynamo()
    store = DynamoOperationalStatusStore("status-table", cast("Any", client))
    outcome = _outcome()

    with pytest.raises(OperationalStatusError, match="head conflicts"):
        store.project_child_outcome(outcome)

    child_event = _event(
        event_id=CHILD_EVENT_ID,
        flow_id=CHILD_FLOW_ID,
        parent_flow_id=PLAN_FLOW_ID,
        execution_id=CHILD_EXECUTION_ID,
        sequence=1,
        surface=Stage1Surface.UNIT,
    )
    applied_key = (f"EVENT#{CHILD_EVENT_ID}", "APPLIED")
    client.items[applied_key] = {
        "partition_key": {"S": applied_key[0]},
        "sort_key": {"S": applied_key[1]},
        "payload": {
            "S": json.dumps(event_document(child_event), sort_keys=True, separators=(",", ":"))
        },
    }

    assert store.project_child_outcome(outcome).kind is ReductionKind.APPLY
    actions = client.transactions[-1]["TransactItems"]
    assert all("ConditionExpression" in next(iter(action.values())) for action in actions)


@pytest.mark.parametrize(
    ("terminal_state", "completion_outcome"),
    [
        (FlowState.BLOCKED, CompletionOutcome.COMPLETED_REFUSED),
        (FlowState.FAILED, CompletionOutcome.EXHAUSTED),
        (FlowState.SUPERSEDED, CompletionOutcome.SUPERSEDED),
    ],
)
def test_noncompleted_plan_terminals_apply_without_generation_head_reads(
    terminal_state: FlowState,
    completion_outcome: CompletionOutcome,
) -> None:
    client = _RecordingDynamo()
    store = DynamoOperationalStatusStore("status-table", cast("Any", client))
    plan = replace(
        _event(required_child_flow_ids=(CHILD_FLOW_ID,)),
        state=terminal_state,
        outcome=completion_outcome,
    )
    request = PlanTerminalProjectionRequest(
        plan,
        PlanRequiredChildExpectations(PLAN_FLOW_ID, (RequiredChildExpectation(CHILD_FLOW_ID, 7),)),
    )

    result = store.reduce_plan_terminal(request)

    assert result.kind is ReductionKind.APPLY
    assert client.batches == []
    assert (
        f"FLOW#{PLAN_FLOW_ID}",
        "AWAITING_CHILDREN#00000000000000000001",
    ) not in client.items
    assert client.items[(f"EVENT#{plan.event_id}", "RECEIPT")]["disposition"] == {"S": "APPLIED"}


def test_exact_awaiting_plan_releases_by_head_bytes_and_deletes_awaiting_owner() -> None:
    client = _RecordingDynamo()
    store = DynamoOperationalStatusStore("status-table", cast("Any", client))
    plan = _event(required_child_flow_ids=(CHILD_FLOW_ID,))
    expectations = PlanRequiredChildExpectations(
        PLAN_FLOW_ID, (RequiredChildExpectation(CHILD_FLOW_ID, 7),)
    )
    request = PlanTerminalProjectionRequest(plan, expectations)

    assert store.reduce_plan_terminal(request).kind is ReductionKind.PARK
    awaiting_key = (f"FLOW#{PLAN_FLOW_ID}", "AWAITING_CHILDREN#00000000000000000001")
    assert awaiting_key in client.items
    assert "expectation_authority" in client.items[awaiting_key]

    child_event = _event(
        event_id=CHILD_EVENT_ID,
        flow_id=CHILD_FLOW_ID,
        parent_flow_id=PLAN_FLOW_ID,
        execution_id=CHILD_EXECUTION_ID,
        sequence=1,
        surface=Stage1Surface.UNIT,
    )
    applied_key = (f"EVENT#{CHILD_EVENT_ID}", "APPLIED")
    client.items[applied_key] = {
        "partition_key": {"S": applied_key[0]},
        "sort_key": {"S": applied_key[1]},
        "payload": {
            "S": json.dumps(event_document(child_event), sort_keys=True, separators=(",", ":"))
        },
    }
    store.project_child_outcome(_outcome())

    released = store.reduce_plan_terminal(request)

    assert released.kind is ReductionKind.APPLY
    assert awaiting_key not in client.items
    release_actions = client.transactions[-1]["TransactItems"]
    assert release_actions[0]["ConditionCheck"]["ConditionExpression"] == (
        "payload = :expected_payload"
    )
    deletes = [action["Delete"] for action in release_actions if "Delete" in action]
    assert deletes[0]["Key"]["sort_key"]["S"] == awaiting_key[1]
    assert "expectation_authority = :expectations" in deletes[0]["ConditionExpression"]
    assert all("ConditionExpression" in next(iter(action.values())) for action in release_actions)
    assert store.reduce_plan_terminal(request).kind is ReductionKind.ALREADY_APPLIED

    changed = PlanTerminalProjectionRequest(
        plan,
        PlanRequiredChildExpectations(PLAN_FLOW_ID, (RequiredChildExpectation(CHILD_FLOW_ID, 8),)),
    )
    with pytest.raises(OperationalStatusError, match="differs from exact authority"):
        store.reduce_plan_terminal(changed)


def test_child_first_plan_release_persists_expectations_and_fences_exact_head() -> None:
    client = _RecordingDynamo()
    store = DynamoOperationalStatusStore("status-table", cast("Any", client))
    child_event = _event(
        event_id=CHILD_EVENT_ID,
        flow_id=CHILD_FLOW_ID,
        parent_flow_id=PLAN_FLOW_ID,
        execution_id=CHILD_EXECUTION_ID,
        sequence=1,
        surface=Stage1Surface.UNIT,
    )
    applied_key = (f"EVENT#{CHILD_EVENT_ID}", "APPLIED")
    client.items[applied_key] = {
        "partition_key": {"S": applied_key[0]},
        "sort_key": {"S": applied_key[1]},
        "payload": {
            "S": json.dumps(event_document(child_event), sort_keys=True, separators=(",", ":"))
        },
    }
    assert store.project_child_outcome(_outcome()).kind is ReductionKind.APPLY
    plan = _event(required_child_flow_ids=(CHILD_FLOW_ID,))
    request = PlanTerminalProjectionRequest(
        plan,
        PlanRequiredChildExpectations(
            PLAN_FLOW_ID,
            (RequiredChildExpectation(CHILD_FLOW_ID, 7),),
        ),
    )

    released = store.reduce_plan_terminal(request)

    assert released.kind is ReductionKind.APPLY
    receipt = client.items[(f"EVENT#{plan.event_id}", "RECEIPT")]
    assert receipt["disposition"] == {"S": "APPLIED"}
    assert "expectation_authority" in receipt
    release_actions = client.transactions[-1]["TransactItems"]
    assert release_actions[0]["ConditionCheck"]["Key"]["sort_key"] == {
        "S": _outcome().generation_head_sort_key
    }
    assert all("ConditionExpression" in next(iter(action.values())) for action in release_actions)


def test_retained_usage_requires_explicit_settled_non_delta_authority() -> None:
    client = _RecordingDynamo()
    contribution = _contribution()
    document = json.loads(serialize_message(contribution))["usage_contribution"]
    snapshot = _snapshot(
        record_key="USAGE#fixture",
        kind=Stage1AuthorityKind.USAGE_CONTRIBUTION,
    )
    key = (snapshot.partition_key, snapshot.record_key)
    item: dict[str, Any] = {
        "partition_key": {"S": snapshot.partition_key},
        "record_key": {"S": snapshot.record_key},
        "immutable_payload_digest": {"S": canonical_authority_digest(document)},
        "authority_kind": {"S": snapshot.authority_kind.value},
        "outbox_state": {"S": snapshot.outbox_state.value},
        "outbox_status": {"S": snapshot.outbox_state.value},
        "outbox_order": {"S": cast("str", snapshot.outbox_order)},
        "consecutive_failed_delivery_attempts": {"N": "0"},
        "athena_usage_state": {"S": "settled"},
        "payload": {
            "S": json.dumps(
                document,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        },
    }
    client.items[key] = item
    store = DynamoStage1OperationalStatus("publications-table", "status-table", cast("Any", client))

    with pytest.raises(OperationalStatusError, match="not owner-settled"):
        store.authority(*key)
    item["is_delta"] = {"BOOL": False}
    assert store.authority(*key) is not None
    item["is_delta"] = {"BOOL": True}
    with pytest.raises(OperationalStatusError, match="unsettled or a delta"):
        store.authority(*key)


def test_retained_event_reader_uses_status_flow_key_and_reattests() -> None:
    client = _RecordingDynamo()
    event = _derived_plan_event()
    digest = event.authority.opaque_digest
    key = (f"STATUS_FLOW#{event.flow_id}", f"EVENT#{digest}")
    item = _publication_item()
    item["partition_key"] = {"S": key[0]}
    item["record_key"] = {"S": key[1]}
    client.items[key] = item
    reader = DynamoStage1RetainedEventAuthorityReader("publications-table", cast("Any", client))

    verified = reader.retained_event_authority(event.flow_id, digest)

    assert verified is not None
    assert verified.authority_digest == digest
    assert (verified.flow_id, verified.execution_id, verified.event_id) == (
        event.flow_id,
        event.execution_id,
        event.event_id,
    )
    assert verified.required_child_expectations == PlanRequiredChildExpectations(
        event.flow_id, (RequiredChildExpectation(_UNIT_IDENTITY.flow_id, 7),)
    )
    assert all(request["ConsistentRead"] is True for request in client.gets)
    assert client.gets[0]["Key"] == {
        "partition_key": {"S": key[0]},
        "record_key": {"S": key[1]},
    }

    assert reader.retained_event_authority(PLAN_FLOW_ID, digest) is None

    item["event_id"] = {"S": CHILD_EVENT_ID}
    with pytest.raises(OperationalStatusError, match="state invariant"):
        reader.retained_event_authority(event.flow_id, digest)


def test_delivery_reattests_and_carries_the_identity_digest_on_the_wire() -> None:
    client = _RecordingDynamo()
    item = _publication_item()
    key = _RecordingDynamo.item_key(item)
    client.items[key] = item
    store = DynamoStage1OperationalStatus("publications-table", "status-table", cast("Any", client))

    retained = store.authority(*key)

    assert retained is not None
    assert isinstance(retained.member, LifecycleEventAuthority)
    expected = event_authority_digest(_derived_plan_authority())
    assert retained.member.event.authority.opaque_digest == expected
    assert retained.member.event.event_id == _derived_plan_event().event_id


def test_persisted_identity_drift_blocks_with_state_invariant_violation(
    runtime_modules: tuple[Any, Any, Any],
) -> None:
    lifecycle = runtime_modules[0]
    client = _RecordingDynamo()
    drifted = replace(_derived_plan_event(), event_id=CHILD_EVENT_ID)
    payload = event_document(drifted)
    item = _publication_item()
    item["event_id"] = {"S": CHILD_EVENT_ID}
    item["payload"] = {
        "S": json.dumps(
            payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
    }
    item["immutable_payload_digest"] = {"S": canonical_authority_digest(payload)}
    key = _RecordingDynamo.item_key(item)
    client.items[key] = item
    store = DynamoStage1OperationalStatus("publications-table", "status-table", cast("Any", client))
    delivery = lifecycle.Stage1AuthorityDelivery(store, _Queue([]), 3)

    with pytest.raises(OperationalStatusError, match="state invariant"):
        store.authority(*key)
    result = delivery.deliver(Stage1OutboxKey(*key), NOW)

    assert result.outcome is lifecycle.DeliveryOutcome.BLOCKED
    cases = [
        stored for (_, sort_key), stored in client.items.items() if sort_key == "STAGE1_OUTBOX"
    ]
    assert len(cases) == 1
    assert cases[0]["block_error_class"] == {"S": BlockErrorClass.STATE_INVARIANT_VIOLATION.value}
