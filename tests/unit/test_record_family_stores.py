# SPDX-License-Identifier: MIT-0
"""One table per audit and evaluation record family: exact requests, read-back, and attestation."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from dynamodb_reserved_words import guard_reserved_words

from adapters.outbound.dynamodb.audit import (
    EVALUATION_DELIVERY_PREFIX,
    EVALUATION_RESULT_PREFIX,
    EVALUATION_WRITER_PREFIX,
    FINDING_PREFIX,
    DynamoAgentQualityAuditStore,
    DynamoAuditFindingStore,
    DynamoEvaluationDeliveryStore,
    DynamoEvaluationResultLinkResolver,
    DynamoEvaluationVerdictStore,
    DynamoEvaluationWriterReceiptStore,
    audit_record_item,
    evaluation_delivery_item,
)
from adapters.outbound.dynamodb.evaluation_outbox import (
    ATTEMPT_COUNT,
    DECIDED_AT,
    LEASE_ACQUIRED_AT,
    LEASE_EXPIRES_AT,
    LEASE_REF,
    STATE,
    DynamoEvaluationHandoffStore,
    DynamoEvaluationOutboxDispatchStore,
    _initial_item,  # pyright: ignore[reportPrivateUsage] - exact outbox item seam
)
from domain.conversation.audit import (
    MANAGED_HARNESS_RUNTIME_V3,
    AuditFindingKindV2,
    AuditFindingSeverityV2,
    AuditFindingV3,
    AuditOperationCategoryV2,
    AuditOperationOutcomeV2,
    AuditPublicationOutcomeV2,
    AuditPublicConversationBlockKindV1,
    AuditTerminalOutcomeV2,
    AuditToolStepDispositionV3,
    AuditToolStepV3,
    ConversationAuditRecordV6,
    RecordRetention,
    TurnDeliveryDispositionV1,
    TurnDeliveryRecordV1,
)
from domain.evaluation.envelope import EvaluationEnvelope
from domain.evaluation.linkage import (
    EvaluationAdmissionV1,
    EvaluationDeliveryFamilyV1,
    EvaluationDeliveryOutcomeV1,
    EvaluationDeliveryReceiptV2,
    EvaluationNotDispatchedReasonV1,
    EvaluationResultStateV1,
    EvaluationResultV3,
    EvaluationWriterReceiptV1,
)
from domain.evaluation.outbox import (
    EvaluationOutboxDeliveryStateV1,
    EvaluationOutboxEnvelopeV1,
    EvaluationOutboxInitialRecordV1,
    EvaluationOutboxStateV1,
    evaluation_delivery_ref,
)
from domain.evaluation.release import EvaluationFamilyV1, EvaluationReleaseEvaluatorV1
from domain.shared_kernel.errors import DependencyError, ErrorCode, InvariantError
from ports.outbound.audit import (
    AuditCorrelationRequest,
    AuditWriteDisposition,
    EvaluationDeliveryPredatesGate,
    EvaluationResultLinkRequest,
    EvaluationResultLinkResolution,
    EvaluationResultLinkResolutionState,
    ReceiptTurnsPageRequest,
)
from ports.outbound.evaluation_outbox import (
    EvaluationHandoffRequest,
    EvaluationOutboxDeliverRequest,
    EvaluationOutboxReleaseRequest,
    EvaluationOutboxWithholdRequest,
    EvaluationOutboxWriteDisposition,
    EvaluationOutboxWriteResult,
)

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-080")]

_RECEIPTS = "receipts-test"
_FINDINGS = "findings-test"
_DELIVERIES = "deliveries-test"
_WRITER_RECEIPTS = "writer-receipts-test"
_VERDICTS = "verdicts-test"
_OUTBOX = "outbox-test"
_AUDIT_ID = "audit-01"
_ADMISSION_REF = "admission-01"
_CORRELATION_ID = "123e4567-e89b-42d3-a456-426614174000"
_TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
_DIGEST = "sha256:" + "a" * 64
_PRINCIPAL = "5fdbe1d5-7901-4fee-9d1d-518103789c94"
_AT = "2026-09-02T12:00:00+00:00"
_RETENTION = RecordRetention(90)
_EXPIRES_AT = 1_796_126_400
"""`_AT` plus 90 days, in whole epoch seconds: 2026-12-01T12:00:00Z (ADR-081 §5)."""


def _now() -> datetime:
    """Read inside the retention, so the suite does not start failing on a calendar date."""
    return datetime(2026, 9, 3, tzinfo=UTC)


def _fresh(text: str) -> str:
    """Copy text to an equal but distinct string, as botocore parses every response value."""
    return text[:1] + text[1:]


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": _fresh(code), "Message": code}}, "operation")


def _identity(item: dict[str, Any]) -> tuple[str, ...]:
    if "item_key" in item:
        return (item["audit_id"]["S"], item["item_key"]["S"])
    return (item["admission_ref"]["S"],)


@guard_reserved_words
@dataclass
class _RecordingDynamo:
    """Records which table each low-level call named; never a fake of DynamoDB semantics."""

    tables: dict[str, dict[tuple[str, ...], dict[str, Any]]] = field(
        default_factory=dict[str, dict[tuple[str, ...], dict[str, Any]]]
    )
    calls: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    requests: list[tuple[str, dict[str, Any]]] = field(
        default_factory=list[tuple[str, dict[str, Any]]]
    )
    denied_reads: bool = False
    cancel_transactions: bool = False
    failures: dict[str, Exception] = field(default_factory=dict[str, Exception])
    """An SDK failure to raise from the named operation, after recording its request."""
    query_responses: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    """Scripted Query responses, returned in order instead of matching seeded items."""

    def seed(self, table: str, item: dict[str, Any]) -> None:
        self.tables.setdefault(table, {})[_identity(item)] = dict(item)

    def _record(self, operation: str, request: dict[str, Any]) -> None:
        self.requests.append((operation, dict(request)))
        failure = self.failures.get(operation)
        if failure is not None:
            raise failure

    def requests_for(self, operation: str) -> list[dict[str, Any]]:
        return [request for name, request in self.requests if name == operation]

    def forget(self) -> None:
        self.calls.clear()
        self.requests.clear()

    def put_item(self, **request: Any) -> dict[str, Any]:
        table = cast("str", request["TableName"])
        self.calls.append(("PutItem", table))
        self._record("PutItem", request)
        assert request["ConditionExpression"] == "attribute_not_exists(item_key)"
        item = cast("dict[str, Any]", request["Item"])
        if _identity(item) in self.tables.get(table, {}):
            raise _client_error("ConditionalCheckFailedException")
        self.seed(table, item)
        return {}

    def get_item(self, **request: Any) -> dict[str, Any]:
        table = cast("str", request["TableName"])
        self.calls.append(("GetItem", table))
        self._record("GetItem", request)
        if self.denied_reads:
            raise _client_error("AccessDeniedException")
        found = self.tables.get(table, {}).get(_identity(cast("dict[str, Any]", request["Key"])))
        return {} if found is None else {"Item": dict(found)}

    def query(self, **request: Any) -> dict[str, Any]:
        table = cast("str", request["TableName"])
        values = cast("dict[str, dict[str, str]]", request["ExpressionAttributeValues"])
        items = list(self.tables.get(table, {}).values())
        if "IndexName" in request:
            self.calls.append(("Query", f"{table}/index/{request['IndexName']}"))
            self._record("Query", request)
            if self.query_responses:
                return self.query_responses.pop(0)
            correlation = values[":correlation"]["S"]
            matched = [
                {name: item[name] for name in ("audit_id", "item_key", "correlation_id")}
                for item in items
                if item.get("correlation_id", {}).get("S") == correlation
            ]
        else:
            self.calls.append(("Query", table))
            self._record("Query", request)
            if self.query_responses:
                return self.query_responses.pop(0)
            matched = [
                item
                for item in items
                if item["audit_id"]["S"] == values[":audit"]["S"]
                and item["item_key"]["S"].startswith(values[":prefix"]["S"])
            ]
        limit = cast("int", request.get("Limit", len(matched) + 1))
        response: dict[str, Any] = {"Items": matched[:limit]}
        if len(matched) > limit:
            response["LastEvaluatedKey"] = {"audit_id": matched[limit - 1]["audit_id"]}
        return response

    def transact_write_items(self, **request: Any) -> dict[str, Any]:
        operations = cast("list[dict[str, dict[str, Any]]]", request["TransactItems"])
        for operation in operations:
            (kind, body), *_ = operation.items()
            self.calls.append((f"Transact{kind}", cast("str", body["TableName"])))
        self._record("TransactWriteItems", request)
        if self.cancel_transactions:
            raise _client_error("TransactionCanceledException")
        for operation in operations:
            if "Put" in operation:
                put = operation["Put"]
                self.seed(put["TableName"], put["Item"])
            else:
                update = operation["Update"]
                values = update["ExpressionAttributeValues"]
                names = update["ExpressionAttributeNames"]
                row = self.tables[update["TableName"]][_identity(update["Key"])]
                row[STATE] = values[":terminal"]
                row[names["#at"]] = values[":at"]
        return {}

    def update_item(self, **request: Any) -> dict[str, Any]:
        self.calls.append(("UpdateItem", cast("str", request["TableName"])))
        self._record("UpdateItem", request)
        return {}

    def tables_called(self, operation: str) -> set[str]:
        return {table for name, table in self.calls if name == operation}


def _finding(created_at: str = _AT) -> AuditFindingV3:
    return AuditFindingV3(
        finding_id="finding-01",
        audit_id=_AUDIT_ID,
        kind=AuditFindingKindV2.GOVERNED_REFUSAL,
        severity=AuditFindingSeverityV2.INFO,
        created_at=created_at,
    )


_EVALUATOR = EvaluationReleaseEvaluatorV1(
    evaluator_id="helpful-01",
    evaluator_name="fictional_helpful",
    revision="r1",
    model_id="fictional.judge-model-v1:0",
    prompt_digest="b" * 64,
)
_FAMILY = EvaluationDeliveryFamilyV1(
    family=EvaluationFamilyV1.V4, config_id="fictional_released_v4-0001", evaluators=(_EVALUATOR,)
)


def _delivery(decided_at: str = _AT) -> EvaluationDeliveryReceiptV2:
    return EvaluationDeliveryReceiptV2(
        audit_id=_AUDIT_ID,
        admission_ref=_ADMISSION_REF,
        delivery_ref=evaluation_delivery_ref(_AUDIT_ID, _ADMISSION_REF),
        decided_at=decided_at,
        outcome=EvaluationDeliveryOutcomeV1.DISPATCHED,
        not_dispatched_reason=None,
        families=(_FAMILY,),
    )


def _withheld(
    reason: EvaluationNotDispatchedReasonV1 = EvaluationNotDispatchedReasonV1.ANSWER_NOT_RECEIVED,
    decided_at: str = _AT,
) -> EvaluationDeliveryReceiptV2:
    return replace(
        _delivery(decided_at),
        outcome=EvaluationDeliveryOutcomeV1.NOT_DISPATCHED,
        not_dispatched_reason=reason,
        families=(),
    )


def _writer_receipt(emitted_at: str = _AT) -> EvaluationWriterReceiptV1:
    return EvaluationWriterReceiptV1(
        audit_id=_AUDIT_ID,
        admission_ref=_ADMISSION_REF,
        trace_id=_TRACE_ID,
        span_id="00f067aa0ba902b7",
        emitted_at=emitted_at,
    )


def _verdict(evaluated_at: str = _AT) -> EvaluationResultV3:
    return EvaluationResultV3(
        audit_id=_AUDIT_ID,
        admission_ref=_ADMISSION_REF,
        evaluator_ref="helpful-01",
        evaluator_name="fictional_helpful",
        result_ref="result-01",
        state=EvaluationResultStateV1.COMPLETED,
        label="good",
        score=0.75,
        evaluated_at=evaluated_at,
        family=EvaluationFamilyV1.V4,
        config_id=_FAMILY.config_id,
        evaluator_revision="r1",
        model_id=_EVALUATOR.model_id,
        prompt_digest=_EVALUATOR.prompt_digest,
    )


@dataclass(frozen=True, slots=True)
class _Family:
    table: str
    prefix: str
    write: Callable[[_RecordingDynamo, str], object]
    item_key: str
    read: Callable[[_RecordingDynamo], object]


def _write_finding(client: _RecordingDynamo, at: str) -> object:
    return DynamoAuditFindingStore(
        _FINDINGS, cast("Any", client), retention=_RETENTION, clock=_now
    ).write_finding(_finding(at), _AT)


def _write_delivery(client: _RecordingDynamo, at: str) -> object:
    store = DynamoEvaluationDeliveryStore(
        _DELIVERIES, cast("Any", client), retention=_RETENTION, clock=_now
    )
    return store.write_evaluation_delivery(_delivery(at), _AT)


def _write_writer_receipt(client: _RecordingDynamo, at: str) -> object:
    store = DynamoEvaluationWriterReceiptStore(
        _WRITER_RECEIPTS, cast("Any", client), retention=_RETENTION, clock=_now
    )
    return store.write_evaluation_writer_receipt(_writer_receipt(at), _AT)


def _write_verdict(client: _RecordingDynamo, at: str) -> object:
    store = DynamoEvaluationVerdictStore(
        _VERDICTS, cast("Any", client), retention=_RETENTION, clock=_now
    )
    return store.write_evaluation_result(_verdict(at), _AT)


def _read_findings(client: _RecordingDynamo) -> object:
    return DynamoAuditFindingStore(
        _FINDINGS, cast("Any", client), retention=_RETENTION, clock=_now
    ).get_findings(_AUDIT_ID)


def _read_delivery(client: _RecordingDynamo) -> object:
    store = DynamoEvaluationDeliveryStore(
        _DELIVERIES, cast("Any", client), retention=_RETENTION, clock=_now
    )
    return store.get_evaluation_delivery(_AUDIT_ID, _ADMISSION_REF)


def _read_writer_receipt(client: _RecordingDynamo) -> object:
    store = DynamoEvaluationWriterReceiptStore(
        _WRITER_RECEIPTS, cast("Any", client), retention=_RETENTION, clock=_now
    )
    return store.get_evaluation_writer_receipt(_AUDIT_ID, _ADMISSION_REF)


def _read_verdicts(client: _RecordingDynamo) -> object:
    store = DynamoEvaluationVerdictStore(
        _VERDICTS, cast("Any", client), retention=_RETENTION, clock=_now
    )
    return store.get_evaluation_results(_AUDIT_ID, _ADMISSION_REF)


_FAMILIES = [
    _Family(_FINDINGS, FINDING_PREFIX, _write_finding, "FINDING#finding-01", _read_findings),
    _Family(
        _DELIVERIES,
        EVALUATION_DELIVERY_PREFIX,
        _write_delivery,
        "EVALUATION_DELIVERY#admission-01",
        _read_delivery,
    ),
    _Family(
        _WRITER_RECEIPTS,
        EVALUATION_WRITER_PREFIX,
        _write_writer_receipt,
        "EVALUATION_WRITER#admission-01",
        _read_writer_receipt,
    ),
    _Family(
        _VERDICTS,
        EVALUATION_RESULT_PREFIX,
        _write_verdict,
        "EVALUATION_RESULT#admission-01#helpful-01",
        _read_verdicts,
    ),
]
_IDS = ["findings", "deliveries", "writer-receipts", "verdicts"]


def test_a_delivery_and_verdict_stored_while_v3_ran_still_read_back() -> None:
    """ADR-079: V3 is retired, but records naming it decode until they expire."""
    retired = EvaluationDeliveryFamilyV1(
        family=EvaluationFamilyV1.V3,
        config_id="fictional_released_v3-0001",
        evaluators=(_EVALUATOR,),
    )
    delivery = replace(_delivery(), families=(retired,))
    verdict = replace(_verdict(), family=EvaluationFamilyV1.V3, config_id=retired.config_id)
    client = _RecordingDynamo()
    DynamoEvaluationDeliveryStore(
        _DELIVERIES, cast("Any", client), retention=_RETENTION, clock=_now
    ).write_evaluation_delivery(delivery, _AT)
    DynamoEvaluationVerdictStore(
        _VERDICTS, cast("Any", client), retention=_RETENTION, clock=_now
    ).write_evaluation_result(verdict, _AT)

    assert _read_delivery(client) == delivery
    assert _read_verdicts(client) == (verdict,)


@pytest.mark.parametrize("family", _FAMILIES, ids=_IDS)
def test_each_family_writer_conditionally_creates_only_in_its_own_table(family: _Family) -> None:
    client = _RecordingDynamo()

    result = family.write(client, _AT)

    assert cast("Any", result).disposition is AuditWriteDisposition.APPLIED
    assert client.calls == [("PutItem", family.table)]
    (key,) = client.tables[family.table]
    assert key[0] == _AUDIT_ID
    assert key[1].startswith(family.prefix)


@pytest.mark.parametrize("family", _FAMILIES, ids=_IDS)
def test_a_replayed_identical_write_reads_back_its_own_table_as_an_exact_replay(
    family: _Family,
) -> None:
    """At-least-once delivery replays a write; reading it back must not redrive it (ADR-080 §3)."""
    client = _RecordingDynamo()
    family.write(client, _AT)
    client.calls.clear()

    replay = family.write(client, _AT)

    assert cast("Any", replay).disposition is AuditWriteDisposition.EXACT_REPLAY
    assert client.calls == [("PutItem", family.table), ("GetItem", family.table)]


@pytest.mark.parametrize("family", _FAMILIES, ids=_IDS)
def test_a_replay_reads_back_with_a_strongly_consistent_get_of_its_exact_key(
    family: _Family,
) -> None:
    """An eventually consistent read-back can miss the first write and misreport a conflict."""
    client = _RecordingDynamo()
    family.write(client, _AT)
    client.forget()

    family.write(client, _AT)

    (put,) = client.requests_for("PutItem")
    assert set(put) == {"TableName", "Item", "ConditionExpression"}
    assert client.requests_for("GetItem") == [
        {
            "TableName": family.table,
            "Key": {"audit_id": {"S": _AUDIT_ID}, "item_key": {"S": family.item_key}},
            "ConsistentRead": True,
        }
    ]


@pytest.mark.parametrize("family", _FAMILIES, ids=_IDS)
def test_a_different_item_already_under_the_key_is_a_conflict_not_a_replay(
    family: _Family,
) -> None:
    client = _RecordingDynamo()
    family.write(client, "2026-09-02T12:00:01+00:00")

    with pytest.raises(InvariantError) as raised:
        family.write(client, _AT)

    assert raised.value.code == ErrorCode("AGENT_QUALITY", 3)


_WRITE_FAILURES = [
    _client_error("AccessDeniedException"),
    _client_error("ProvisionedThroughputExceededException"),
    EndpointConnectionError(endpoint_url="https://dynamodb.invalid"),
]
_WRITE_FAILURE_IDS = ["denied", "throttled", "unreachable"]


@pytest.mark.parametrize("failure", _WRITE_FAILURES, ids=_WRITE_FAILURE_IDS)
@pytest.mark.parametrize("family", _FAMILIES, ids=_IDS)
def test_a_write_failure_other_than_the_condition_is_unwritable_and_never_read_back(
    family: _Family, failure: Exception
) -> None:
    """Only a failed condition means the key exists; any other failure proves nothing about it."""
    client = _RecordingDynamo(failures={"PutItem": failure})

    with pytest.raises(DependencyError) as raised:
        family.write(client, _AT)

    assert raised.value.code == ErrorCode("AGENT_QUALITY", 2)
    assert raised.value.context == {"cause": type(failure).__name__}
    assert client.calls == [("PutItem", family.table)]


@pytest.mark.parametrize("family", _FAMILIES, ids=_IDS)
def test_a_family_read_that_fails_in_the_sdk_is_unreadable(family: _Family) -> None:
    client = _RecordingDynamo(
        failures={
            operation: EndpointConnectionError(endpoint_url="https://dynamodb.invalid")
            for operation in ("GetItem", "Query")
        }
    )

    with pytest.raises(DependencyError) as raised:
        family.read(client)

    assert raised.value.code == ErrorCode("AGENT_QUALITY", 1)
    assert raised.value.context == {"cause": "EndpointConnectionError"}


def _stored_item(table: str, write: Callable[[_RecordingDynamo], object]) -> dict[str, Any]:
    """Return the exact item a store writes, taken from a separate recording client."""
    scratch = _RecordingDynamo()
    write(scratch)
    (item,) = scratch.tables[table].values()
    return item


@pytest.mark.parametrize("family", _FAMILIES, ids=_IDS)
def test_a_retained_family_item_whose_digest_was_edited_is_an_invariant_conflict(
    family: _Family,
) -> None:
    """Findings once leaked the decoder's bare ValueError; every family maps it to a conflict."""
    client = _RecordingDynamo()
    item = _stored_item(family.table, lambda scratch: family.write(scratch, _AT))
    client.seed(family.table, {**item, "payload_digest": {"S": "0" * 64}})

    with pytest.raises(InvariantError) as raised:
        family.read(client)

    assert raised.value.code == ErrorCode("AGENT_QUALITY", 3)


@pytest.mark.parametrize("family", _FAMILIES, ids=_IDS)
def test_a_writer_that_cannot_read_its_own_table_reports_its_replay_unreadable(
    family: _Family,
) -> None:
    """Without GetItem on its own table a replay is indistinguishable from a failed read."""
    client = _RecordingDynamo()
    family.write(client, _AT)
    client.denied_reads = True

    with pytest.raises(DependencyError):
        family.write(client, _AT)


def test_family_readers_query_or_get_only_their_own_tables() -> None:
    client = _RecordingDynamo()
    _write_finding(client, _AT)
    _write_delivery(client, _AT)
    _write_writer_receipt(client, _AT)
    _write_verdict(client, _AT)
    client.calls.clear()
    dynamodb = cast("Any", client)

    findings = DynamoAuditFindingStore(
        _FINDINGS, dynamodb, retention=_RETENTION, clock=_now
    ).get_findings(_AUDIT_ID)
    delivery = DynamoEvaluationDeliveryStore(
        _DELIVERIES, dynamodb, retention=_RETENTION, clock=_now
    ).get_evaluation_delivery(_AUDIT_ID, _ADMISSION_REF)
    writer = DynamoEvaluationWriterReceiptStore(
        _WRITER_RECEIPTS, dynamodb, retention=_RETENTION, clock=_now
    ).get_evaluation_writer_receipt(_AUDIT_ID, _ADMISSION_REF)
    verdicts = DynamoEvaluationVerdictStore(
        _VERDICTS, dynamodb, retention=_RETENTION, clock=_now
    ).get_evaluation_results(_AUDIT_ID, _ADMISSION_REF)

    assert findings == (_finding(),)
    assert delivery == _delivery()
    assert writer == _writer_receipt()
    assert verdicts == (_verdict(),)
    assert client.calls == [
        ("Query", _FINDINGS),
        ("GetItem", _DELIVERIES),
        ("GetItem", _WRITER_RECEIPTS),
        ("Query", _VERDICTS),
    ]


def _leased_outbox_item() -> dict[str, Any]:
    envelope = EvaluationOutboxEnvelopeV1(
        audit_id=_AUDIT_ID,
        admission_ref=_ADMISSION_REF,
        delivery_ref=evaluation_delivery_ref(_AUDIT_ID, _ADMISSION_REF),
        envelope=EvaluationEnvelope(
            question="What is the fictional governed result?",
            released_answer="The fictional released answer.",
            correlation_id=_CORRELATION_ID,
            audit_id=_AUDIT_ID,
            evaluation_admission_ref=_ADMISSION_REF,
        ),
        admitted_at=_AT,
    )
    item = cast(
        "dict[str, Any]",
        _initial_item(
            EvaluationOutboxInitialRecordV1(
                envelope=envelope,
                state=EvaluationOutboxStateV1(
                    audit_id=_AUDIT_ID,
                    admission_ref=_ADMISSION_REF,
                    delivery_ref=envelope.delivery_ref,
                    state=EvaluationOutboxDeliveryStateV1.PENDING,
                    available_at=_AT,
                    attempt_count=0,
                ),
            )
        ),
    )
    return {
        **item,
        STATE: {"S": EvaluationOutboxDeliveryStateV1.LEASED.value},
        LEASE_REF: {"S": "lease-01"},
        LEASE_ACQUIRED_AT: {"S": _AT},
        LEASE_EXPIRES_AT: {"S": "2026-09-02T12:02:00+00:00"},
        ATTEMPT_COUNT: {"N": "1"},
    }


def _deliver(client: _RecordingDynamo, delivered_at: str = _AT) -> None:
    DynamoEvaluationOutboxDispatchStore(
        _OUTBOX, _DELIVERIES, cast("Any", client), retention=_RETENTION
    ).deliver(
        EvaluationOutboxDeliverRequest(
            admission_ref=_ADMISSION_REF,
            lease_ref="lease-01",
            decided_at=delivered_at,
            expires_at_epoch=1_790_000_000,
            families=(_FAMILY,),
        )
    )


def _withhold(
    client: _RecordingDynamo,
    reason: EvaluationNotDispatchedReasonV1 = EvaluationNotDispatchedReasonV1.ANSWER_NOT_RECEIVED,
) -> None:
    DynamoEvaluationOutboxDispatchStore(
        _OUTBOX, _DELIVERIES, cast("Any", client), retention=_RETENTION
    ).withhold(
        EvaluationOutboxWithholdRequest(
            admission_ref=_ADMISSION_REF,
            lease_ref="lease-01",
            decided_at=_AT,
            reason=reason,
            expires_at_epoch=1_790_000_000,
        )
    )


def test_dispatcher_delivery_spans_the_outbox_and_the_deliveries_table_only() -> None:
    client = _RecordingDynamo()
    client.seed(_OUTBOX, _leased_outbox_item())

    _deliver(client)

    assert client.tables_called("TransactUpdate") == {_OUTBOX}
    assert client.tables_called("TransactPut") == {_DELIVERIES}
    stored = DynamoEvaluationDeliveryStore(
        _DELIVERIES, cast("Any", client), retention=_RETENTION, clock=_now
    )
    assert stored.get_evaluation_delivery(_AUDIT_ID, _ADMISSION_REF) == _delivery()
    assert _RECEIPTS not in {table for _, table in client.calls}


def _release(client: _RecordingDynamo, *, count_attempt: bool) -> dict[str, Any]:
    DynamoEvaluationOutboxDispatchStore(
        _OUTBOX, _DELIVERIES, cast("Any", client), retention=_RETENTION
    ).release(
        EvaluationOutboxReleaseRequest(
            admission_ref=_ADMISSION_REF,
            lease_ref="lease-01",
            available_at=_AT,
            count_attempt=count_attempt,
        )
    )
    (request,) = client.requests_for("UpdateItem")
    return request


def test_a_counted_release_keeps_the_claims_attempt() -> None:
    request = _release(_RecordingDynamo(), count_attempt=True)

    assert request["ConditionExpression"] == "#state = :leased AND lease_ref = :lease"
    assert request["UpdateExpression"] == (
        "SET #state = :pending, ready_at = :ready_at, ready_sort = :ready_sort "
        "REMOVE lease_ref, lease_acquired_at, lease_expires_at"
    )
    assert ":one" not in request["ExpressionAttributeValues"]


def test_an_uncounted_release_takes_back_the_claims_attempt_under_the_same_lease() -> None:
    request = _release(_RecordingDynamo(), count_attempt=False)

    assert request["ConditionExpression"] == "#state = :leased AND lease_ref = :lease"
    assert request["UpdateExpression"] == (
        "SET #state = :pending, ready_at = :ready_at, ready_sort = :ready_sort, "
        "attempt_count = attempt_count - :one "
        "REMOVE lease_ref, lease_acquired_at, lease_expires_at"
    )
    assert request["ExpressionAttributeValues"][":one"] == {"N": "1"}
    assert request["ExpressionAttributeValues"][":lease"] == {"S": "lease-01"}


def test_dispatcher_classifies_a_replayed_delivery_by_reading_the_deliveries_table() -> None:
    client = _RecordingDynamo()
    client.seed(_OUTBOX, _leased_outbox_item())
    _deliver(client)
    client.calls.clear()
    client.cancel_transactions = True

    _deliver(client)

    assert client.tables_called("GetItem") == {_OUTBOX, _DELIVERIES}


def _admitted_receipt() -> ConversationAuditRecordV6:
    return ConversationAuditRecordV6(
        audit_id=_AUDIT_ID,
        correlation_id=_CORRELATION_ID,
        trace_id=_TRACE_ID,
        operation_category=AuditOperationCategoryV2.COMPOSITION,
        operation_outcome=AuditOperationOutcomeV2.SUCCEEDED,
        publication_outcome=AuditPublicationOutcomeV2.PUBLISHED,
        terminal_outcome=AuditTerminalOutcomeV2.COMPLETE,
        runtime_version=MANAGED_HARNESS_RUNTIME_V3,
        prompt_catalog_digest=_DIGEST,
        page_schema_version="presentation.v2",
        response_contract_version="public-block-stream.v3",
        evaluation_admission_ref=_ADMISSION_REF,
        created_at=_AT,
        trajectory=(
            AuditToolStepV3(
                ordinal=1,
                tool_name="presentation_publish",
                disposition=AuditToolStepDispositionV3.ACCEPTED,
                step_ref="finalizer-message-0",
            ),
        ),
        tool_custody_digest=_DIGEST,
        presentation_plan_digest=_DIGEST,
        public_block_kinds=(AuditPublicConversationBlockKindV1.PARAGRAPH,),
        public_block_count=1,
        guardrail_version="3",
        principal=_PRINCIPAL,
    )


def test_result_ingress_links_through_the_receipts_index_and_the_writer_receipts_table() -> None:
    client = _RecordingDynamo()
    client.seed(
        _RECEIPTS, cast("dict[str, Any]", audit_record_item(_admitted_receipt(), _EXPIRES_AT))
    )
    _write_writer_receipt(client, _AT)
    client.calls.clear()
    dynamodb = cast("Any", client)
    resolver = DynamoEvaluationResultLinkResolver(
        DynamoAgentQualityAuditStore(_RECEIPTS, dynamodb, retention=_RETENTION, clock=_now),
        DynamoEvaluationWriterReceiptStore(
            _WRITER_RECEIPTS, dynamodb, retention=_RETENTION, clock=_now
        ),
    )

    resolution = resolver.resolve_evaluation_result(
        EvaluationResultLinkRequest(correlation_id=_CORRELATION_ID, trace_id=_TRACE_ID)
    )

    assert resolution.state is EvaluationResultLinkResolutionState.LINKED
    assert resolution.target is not None
    assert (resolution.target.audit_id, resolution.target.admission_ref) == (
        _AUDIT_ID,
        _ADMISSION_REF,
    )
    assert client.calls == [
        ("Query", f"{_RECEIPTS}/index/correlation-audit"),
        ("GetItem", _RECEIPTS),
        ("GetItem", _WRITER_RECEIPTS),
    ]


def test_result_ingress_waits_while_the_writer_receipts_table_has_no_receipt() -> None:
    client = _RecordingDynamo()
    client.seed(
        _RECEIPTS, cast("dict[str, Any]", audit_record_item(_admitted_receipt(), _EXPIRES_AT))
    )
    dynamodb = cast("Any", client)
    resolver = DynamoEvaluationResultLinkResolver(
        DynamoAgentQualityAuditStore(_RECEIPTS, dynamodb, retention=_RETENTION, clock=_now),
        DynamoEvaluationWriterReceiptStore(
            _WRITER_RECEIPTS, dynamodb, retention=_RETENTION, clock=_now
        ),
    )

    resolution = resolver.resolve_evaluation_result(
        EvaluationResultLinkRequest(correlation_id=_CORRELATION_ID, trace_id=_TRACE_ID)
    )

    assert resolution.state is EvaluationResultLinkResolutionState.PENDING


def _receipts(client: _RecordingDynamo) -> DynamoAgentQualityAuditStore:
    return DynamoAgentQualityAuditStore(
        _RECEIPTS, cast("Any", client), retention=_RETENTION, clock=_now
    )


def _get_receipt(client: _RecordingDynamo) -> object:
    return _receipts(client).get(_AUDIT_ID)


def _get_delivery_disposition(client: _RecordingDynamo) -> object:
    return _receipts(client).get_delivery(_AUDIT_ID)


def _get_admission(client: _RecordingDynamo) -> object:
    return _receipts(client).get_evaluation_admission(_AUDIT_ID, _ADMISSION_REF)


def _discover(client: _RecordingDynamo) -> object:
    return _receipts(client).discover_bounded(
        AuditCorrelationRequest(correlation_id=_CORRELATION_ID), 2
    )


_RECEIPT_READS = [
    (_get_receipt, "RECORD"),
    (_get_delivery_disposition, "DELIVERY"),
    (_get_admission, "EVALUATION_ADMISSION#admission-01"),
]
_RECEIPT_READ_IDS = ["receipt", "delivery-disposition", "admission"]


@pytest.mark.parametrize(("read", "item_key"), _RECEIPT_READS, ids=_RECEIPT_READ_IDS)
def test_a_receipts_table_read_is_a_strong_get_of_its_exact_key_and_absent_reads_none(
    read: Callable[[_RecordingDynamo], object], item_key: str
) -> None:
    client = _RecordingDynamo()

    assert read(client) is None
    assert client.requests == [
        (
            "GetItem",
            {
                "TableName": _RECEIPTS,
                "Key": {"audit_id": {"S": _AUDIT_ID}, "item_key": {"S": item_key}},
                "ConsistentRead": True,
            },
        )
    ]


@pytest.mark.parametrize(
    "read",
    [_get_receipt, _get_delivery_disposition, _get_admission, _discover],
    ids=[*_RECEIPT_READ_IDS, "correlation-index"],
)
def test_a_receipts_table_read_that_fails_in_the_sdk_is_unreadable(
    read: Callable[[_RecordingDynamo], object],
) -> None:
    client = _RecordingDynamo(
        failures={
            operation: EndpointConnectionError(endpoint_url="https://dynamodb.invalid")
            for operation in ("GetItem", "Query")
        }
    )

    with pytest.raises(DependencyError) as raised:
        read(client)

    assert raised.value.code == ErrorCode("AGENT_QUALITY", 1)
    assert raised.value.context == {"cause": "EndpointConnectionError"}


def _blank_receipt(store: DynamoAgentQualityAuditStore) -> object:
    return store.get(" ")


def _blank_delivery(store: DynamoAgentQualityAuditStore) -> object:
    return store.get_delivery(" ")


def _blank_admission(store: DynamoAgentQualityAuditStore) -> object:
    return store.get_evaluation_admission(" ", _ADMISSION_REF)


@pytest.mark.parametrize(
    "read",
    [_blank_receipt, _blank_delivery, _blank_admission],
    ids=["get", "get_delivery", "get_evaluation_admission"],
)
def test_a_blank_audit_id_is_refused_before_any_read(
    read: Callable[[DynamoAgentQualityAuditStore], object],
) -> None:
    client = _RecordingDynamo()

    with pytest.raises(ValueError, match="audit ID is required"):
        read(_receipts(client))

    assert client.calls == []


def _delivery_disposition(audit_id: str = _AUDIT_ID) -> TurnDeliveryRecordV1:
    return TurnDeliveryRecordV1(
        audit_id=audit_id,
        disposition=TurnDeliveryDispositionV1.DELIVERED,
        emitted_block_count=1,
        recorded_at="2026-09-02T12:00:03+00:00",
    )


def _admission() -> EvaluationAdmissionV1:
    return EvaluationAdmissionV1(audit_id=_AUDIT_ID, admission_ref=_ADMISSION_REF, selected_at=_AT)


def test_a_recorded_delivery_disposition_and_admission_read_back_exactly() -> None:
    client = _RecordingDynamo()
    _receipts(client).record_delivery(_delivery_disposition(), _AT)
    _receipts(client).create_evaluation_admission(_admission(), _AT)

    assert _get_delivery_disposition(client) == _delivery_disposition()
    assert _get_admission(client) == _admission()


@pytest.mark.parametrize("stored_audit_id", ["audit-00", "audit-02"])
def test_a_delivery_disposition_retained_under_another_audit_id_is_malformed(
    stored_audit_id: str,
) -> None:
    """The companion's envelope is exact for its own receipt, but not for the key it sits under."""
    client = _RecordingDynamo()
    item = _stored_item(
        _RECEIPTS,
        lambda scratch: _receipts(scratch).record_delivery(
            _delivery_disposition(stored_audit_id), _AT
        ),
    )
    client.tables[_RECEIPTS] = {(_AUDIT_ID, "DELIVERY"): item}

    with pytest.raises(InvariantError, match="delivery disposition is malformed") as raised:
        _get_delivery_disposition(client)

    assert raised.value.code == ErrorCode("AGENT_QUALITY", 3)


def test_a_delivery_disposition_with_an_extra_attribute_is_malformed() -> None:
    client = _RecordingDynamo()
    item = _stored_item(
        _RECEIPTS, lambda scratch: _receipts(scratch).record_delivery(_delivery_disposition(), _AT)
    )
    client.seed(_RECEIPTS, {**item, "annotation": {"S": "edited"}})

    with pytest.raises(InvariantError, match="delivery disposition is malformed"):
        _get_delivery_disposition(client)


def _seed_receipt(client: _RecordingDynamo, receipt: ConversationAuditRecordV6) -> None:
    client.seed(_RECEIPTS, cast("dict[str, Any]", audit_record_item(receipt, _EXPIRES_AT)))


def _write_receipt(client: _RecordingDynamo) -> object:
    return _receipts(client).create(_admitted_receipt())


def _write_admission(client: _RecordingDynamo) -> object:
    return _receipts(client).create_evaluation_admission(_admission(), _AT)


@pytest.mark.parametrize(
    ("read", "write"),
    [(_get_receipt, _write_receipt), (_get_admission, _write_admission)],
    ids=["receipt", "admission"],
)
def test_a_retained_receipts_table_item_whose_digest_was_edited_is_an_invariant_conflict(
    read: Callable[[_RecordingDynamo], object], write: Callable[[_RecordingDynamo], object]
) -> None:
    client = _RecordingDynamo()
    item = _stored_item(_RECEIPTS, write)
    client.seed(_RECEIPTS, {**item, "payload_digest": {"S": "0" * 64}})

    with pytest.raises(InvariantError) as raised:
        read(client)

    assert raised.value.code == ErrorCode("AGENT_QUALITY", 3)


def _index_item(
    audit_id: str = _AUDIT_ID,
    *,
    item_key: str = "RECORD",
    correlation_id: str = _CORRELATION_ID,
) -> dict[str, Any]:
    return {
        "audit_id": {"S": _fresh(audit_id)},
        "item_key": {"S": _fresh(item_key)},
        "correlation_id": {"S": _fresh(correlation_id)},
    }


def _receipt(
    audit_id: str = _AUDIT_ID,
    *,
    admission_ref: str | None = _ADMISSION_REF,
    correlation_id: str = _CORRELATION_ID,
) -> ConversationAuditRecordV6:
    return replace(
        _admitted_receipt(),
        audit_id=audit_id,
        evaluation_admission_ref=admission_ref,
        correlation_id=correlation_id,
    )


def test_a_correlation_index_item_parsed_from_a_response_attests_its_base_receipt() -> None:
    """Parsed values are equal to, never identical with, the adapter's key constants."""
    client = _RecordingDynamo(query_responses=[{"Items": [_index_item()]}])
    _seed_receipt(client, _receipt())

    assert _discover(client) == (_receipt(),)
    (query,) = client.requests_for("Query")
    assert query == {
        "TableName": _RECEIPTS,
        "IndexName": "correlation-audit",
        "KeyConditionExpression": "correlation_id = :correlation",
        "ExpressionAttributeValues": {":correlation": {"S": _CORRELATION_ID}},
        "ProjectionExpression": "audit_id, item_key, correlation_id",
        "Limit": 3,
    }


def test_a_correlation_index_that_exceeds_its_bound_is_reported_truncated_without_base_reads() -> (
    None
):
    client = _RecordingDynamo(
        query_responses=[
            {
                "Items": [_index_item("audit-01"), _index_item("audit-02")],
                "LastEvaluatedKey": _index_item("audit-02"),
            }
        ]
    )

    assert _discover(client) is None
    assert client.requests_for("GetItem") == []


_CORRELATION_BELOW = "023e4567-e89b-42d3-a456-426614174000"
_CORRELATION_ABOVE = "923e4567-e89b-42d3-a456-426614174000"


@pytest.mark.parametrize(
    ("index_item", "message"),
    [
        (
            {name: value for name, value in _index_item().items() if name != "correlation_id"},
            "malformed key",
        ),
        ({**_index_item(), "payload": {"S": "{}"}}, "malformed key"),
        (_index_item(item_key="DELIVERY"), "non-record item"),
        (_index_item(item_key="RECORD#shadow"), "non-record item"),
        (_index_item(correlation_id=_CORRELATION_BELOW), "non-record item"),
        (_index_item(correlation_id=_CORRELATION_ABOVE), "non-record item"),
    ],
    ids=[
        "missing-projection",
        "extra-attribute",
        "key-below-record",
        "key-above-record",
        "correlation-below",
        "correlation-above",
    ],
)
def test_a_correlation_index_item_that_is_not_an_exact_record_key_is_a_conflict(
    index_item: dict[str, Any], message: str
) -> None:
    client = _RecordingDynamo(query_responses=[{"Items": [index_item]}])
    _seed_receipt(client, _receipt())

    with pytest.raises(InvariantError, match=message) as raised:
        _discover(client)

    assert raised.value.code == ErrorCode("AGENT_QUALITY", 3)


@pytest.mark.parametrize(
    "base",
    [
        _receipt(correlation_id=_CORRELATION_BELOW),
        _receipt(correlation_id=_CORRELATION_ABOVE),
    ],
    ids=["correlation-below", "correlation-above"],
)
def test_a_correlation_index_item_whose_base_receipt_disagrees_fails_attestation(
    base: ConversationAuditRecordV6,
) -> None:
    """The index is a projection; only the strongly read base receipt proves the correlation."""
    client = _RecordingDynamo(query_responses=[{"Items": [_index_item()]}])
    _seed_receipt(client, base)

    with pytest.raises(InvariantError, match="base-record attestation"):
        _discover(client)


def test_a_correlation_index_key_whose_base_receipt_is_absent_reads_as_absent() -> None:
    """TTL deletes the receipt and its index entry separately, so the key can outlive it."""
    client = _RecordingDynamo(query_responses=[{"Items": [_index_item()]}])

    assert _discover(client) == ()


def test_findings_query_one_strong_bounded_prefix_of_the_audit_partition() -> None:
    client = _RecordingDynamo()

    assert _read_findings(client) == ()
    assert client.requests_for("Query") == [
        {
            "TableName": _FINDINGS,
            "KeyConditionExpression": "audit_id = :audit AND begins_with(item_key, :prefix)",
            "ExpressionAttributeValues": {
                ":audit": {"S": _AUDIT_ID},
                ":prefix": {"S": "FINDING#"},
            },
            "Limit": 21,
            "ConsistentRead": True,
        }
    ]


def test_verdicts_query_one_strong_bounded_prefix_of_the_admission() -> None:
    client = _RecordingDynamo()

    assert _read_verdicts(client) == ()
    assert client.requests_for("Query") == [
        {
            "TableName": _VERDICTS,
            "KeyConditionExpression": "audit_id = :audit AND begins_with(item_key, :prefix)",
            "ExpressionAttributeValues": {
                ":audit": {"S": _AUDIT_ID},
                ":prefix": {"S": "EVALUATION_RESULT#admission-01#"},
            },
            "Limit": 9,
            "ConsistentRead": True,
        }
    ]


@pytest.mark.parametrize(
    ("read", "message"),
    [
        (_read_findings, "finding query exceeded"),
        (_read_verdicts, "result query exceeded"),
    ],
    ids=["findings", "verdicts"],
)
def test_a_family_query_that_exceeds_its_bound_is_a_conflict_not_a_partial_read(
    read: Callable[[_RecordingDynamo], object], message: str
) -> None:
    client = _RecordingDynamo(
        query_responses=[{"Items": [], "LastEvaluatedKey": {"audit_id": {"S": _AUDIT_ID}}}]
    )

    with pytest.raises(InvariantError, match=message) as raised:
        read(client)

    assert raised.value.code == ErrorCode("AGENT_QUALITY", 3)


@pytest.mark.parametrize("stored_audit_id", ["audit-00", "audit-02"])
def test_a_finding_returned_from_another_audit_partition_is_a_conflict(
    stored_audit_id: str,
) -> None:
    item = _stored_item(
        _FINDINGS,
        lambda scratch: DynamoAuditFindingStore(
            _FINDINGS, cast("Any", scratch), retention=_RETENTION, clock=_now
        ).write_finding(replace(_finding(), audit_id=stored_audit_id), _AT),
    )
    client = _RecordingDynamo(query_responses=[{"Items": [item]}])

    with pytest.raises(InvariantError, match="crossed an audit partition"):
        _read_findings(client)


def test_the_verdict_prefix_excludes_a_sibling_admission_that_extends_the_reference() -> None:
    """`admission-010` begins with `admission-01`; the `#` that ends the prefix keeps it out."""
    sibling = _stored_item(
        _VERDICTS,
        lambda scratch: DynamoEvaluationVerdictStore(
            _VERDICTS, cast("Any", scratch), retention=_RETENTION, clock=_now
        ).write_evaluation_result(replace(_verdict(), admission_ref="admission-010"), _AT),
    )
    client = _RecordingDynamo()

    _read_verdicts(client)

    (query,) = client.requests_for("Query")
    prefix = cast("dict[str, dict[str, str]]", query["ExpressionAttributeValues"])[":prefix"]["S"]
    assert not cast("dict[str, str]", sibling["item_key"])["S"].startswith(prefix)


def test_a_verdict_from_another_admission_in_the_query_result_is_a_conflict() -> None:
    sibling = _stored_item(
        _VERDICTS,
        lambda scratch: DynamoEvaluationVerdictStore(
            _VERDICTS, cast("Any", scratch), retention=_RETENTION, clock=_now
        ).write_evaluation_result(replace(_verdict(), admission_ref="admission-010"), _AT),
    )
    client = _RecordingDynamo(query_responses=[{"Items": [sibling]}])

    with pytest.raises(InvariantError, match="evaluation result is malformed"):
        _read_verdicts(client)


def test_a_v1_verdict_is_malformed_in_the_verdicts_table() -> None:
    """The verdicts table postdates V2; a V1 item there was not written by its one writer."""
    document = {
        "schema_version": "EvaluationResult.v1",
        "audit_id": _AUDIT_ID,
        "admission_ref": _ADMISSION_REF,
        "evaluator_ref": "released-evaluator-01",
        "result_ref": "result-01",
        "state": "completed",
        "label": "good",
        "score": 0.75,
        "evaluated_at": _AT,
    }
    payload = json.dumps(document, separators=(",", ":"), sort_keys=True)
    legacy = {
        "audit_id": {"S": _AUDIT_ID},
        "item_key": {"S": "EVALUATION_RESULT#admission-01#released-evaluator-01"},
        "record_type": {"S": "EVALUATION_RESULT"},
        "payload": {"S": payload},
        "payload_digest": {"S": hashlib.sha256(payload.encode()).hexdigest()},
    }
    client = _RecordingDynamo(query_responses=[{"Items": [legacy]}])

    with pytest.raises(InvariantError, match="evaluation result is malformed"):
        _read_verdicts(client)


def _writer_for(audit_id: str, trace_id: str = _TRACE_ID) -> EvaluationWriterReceiptV1:
    return replace(_writer_receipt(), audit_id=audit_id, trace_id=trace_id)


def _linking(
    receipts: list[ConversationAuditRecordV6], writers: list[EvaluationWriterReceiptV1]
) -> tuple[_RecordingDynamo, EvaluationResultLinkResolution]:
    client = _RecordingDynamo(
        query_responses=[{"Items": [_index_item(receipt.audit_id) for receipt in receipts]}]
    )
    dynamodb = cast("Any", client)
    for receipt in receipts:
        _seed_receipt(client, receipt)
    writer_store = DynamoEvaluationWriterReceiptStore(
        _WRITER_RECEIPTS, dynamodb, retention=_RETENTION, clock=_now
    )
    for writer in writers:
        writer_store.write_evaluation_writer_receipt(writer, _AT)
    resolver = DynamoEvaluationResultLinkResolver(_receipts(client), writer_store)
    return client, resolver.resolve_evaluation_result(
        EvaluationResultLinkRequest(correlation_id=_CORRELATION_ID, trace_id=_TRACE_ID)
    )


def _linked(
    receipts: list[ConversationAuditRecordV6], writers: list[EvaluationWriterReceiptV1]
) -> EvaluationResultLinkResolution:
    return _linking(receipts, writers)[1]


def _target(resolution: EvaluationResultLinkResolution) -> tuple[str, str] | None:
    target = resolution.target
    return None if target is None else (target.audit_id, target.admission_ref)


def test_result_ingress_bounds_its_correlation_index_query_to_two_receipts() -> None:
    client, resolution = _linking([_receipt()], [_writer_for(_AUDIT_ID)])

    assert resolution.state is EvaluationResultLinkResolutionState.LINKED
    (query,) = client.requests_for("Query")
    assert (query["IndexName"], query["Limit"]) == ("correlation-audit", 3)


def test_result_ingress_links_the_admitted_receipt_after_an_unadmitted_one() -> None:
    resolution = _linked(
        [_receipt("audit-01", admission_ref=None), _receipt("audit-02")],
        [_writer_for("audit-02")],
    )

    assert resolution.state is EvaluationResultLinkResolutionState.LINKED
    assert _target(resolution) == ("audit-02", _ADMISSION_REF)


def test_result_ingress_links_past_an_admitted_receipt_still_awaiting_its_writer() -> None:
    resolution = _linked(
        [_receipt("audit-01"), _receipt("audit-02")],
        [_writer_for("audit-02")],
    )

    assert resolution.state is EvaluationResultLinkResolutionState.LINKED
    assert _target(resolution) == ("audit-02", _ADMISSION_REF)


@pytest.mark.parametrize(
    "trace_id",
    # pragma: allowlist nextline secret (A-ENTROPY-IDENTIFIER-001)
    ["0bf92f3577b34da6a3ce929d0e0e4736", "fbf92f3577b34da6a3ce929d0e0e4736"],
    ids=["trace-below", "trace-above"],
)
def test_result_ingress_does_not_link_a_writer_receipt_for_another_trace(trace_id: str) -> None:
    resolution = _linked([_receipt()], [_writer_for(_AUDIT_ID, trace_id)])

    assert resolution.state is EvaluationResultLinkResolutionState.UNMATCHED
    assert resolution.target is None


def test_result_ingress_refuses_to_choose_between_two_receipts_linked_to_one_trace() -> None:
    resolution = _linked(
        [_receipt("audit-01"), _receipt("audit-02")],
        [_writer_for("audit-01"), _writer_for("audit-02")],
    )

    assert resolution.state is EvaluationResultLinkResolutionState.UNMATCHED
    assert resolution.target is None


_TRANSACTION_FAILURES = [
    _client_error("AccessDeniedException"),
    _client_error("ValidationException"),
    EndpointConnectionError(endpoint_url="https://dynamodb.invalid"),
]


@pytest.mark.parametrize("failure", _TRANSACTION_FAILURES, ids=_WRITE_FAILURE_IDS)
def test_dispatcher_reports_a_non_cancellation_failure_unwritable_without_reading_back(
    failure: Exception,
) -> None:
    client = _RecordingDynamo(failures={"TransactWriteItems": failure})
    client.seed(_OUTBOX, _leased_outbox_item())

    with pytest.raises(DependencyError) as raised:
        _deliver(client)

    assert raised.value.code == ErrorCode("EVALUATION", 4)
    assert raised.value.context == {"cause": type(failure).__name__}
    assert client.tables_called("GetItem") == {_OUTBOX}
    assert len(client.requests_for("GetItem")) == 1


def test_dispatcher_treats_a_cancelled_delivery_whose_lease_is_still_open_as_a_conflict() -> None:
    """A retained receipt alone is not delivery: the outbox row must also have left its lease."""
    client = _RecordingDynamo(cancel_transactions=True)
    client.seed(_OUTBOX, _leased_outbox_item())
    client.seed(
        _DELIVERIES, cast("dict[str, Any]", evaluation_delivery_item(_delivery(), _EXPIRES_AT))
    )

    with pytest.raises(InvariantError) as raised:
        _deliver(client)

    assert raised.value.code == ErrorCode("EVALUATION", 5)


def test_dispatcher_treats_a_cancelled_delivery_with_a_different_receipt_as_a_conflict() -> None:
    client = _RecordingDynamo()
    client.seed(_OUTBOX, _leased_outbox_item())
    _deliver(client)
    client.cancel_transactions = True

    with pytest.raises(InvariantError) as raised:
        _deliver(client, "2026-09-02T12:00:09+00:00")

    assert raised.value.code == ErrorCode("EVALUATION", 5)


# --- Query bounds: request one more than the bound; only more than the bound exceeds it ---------


def _seed_findings(client: _RecordingDynamo, count: int) -> None:
    store = DynamoAuditFindingStore(_FINDINGS, cast("Any", client), retention=_RETENTION)
    for index in range(count):
        store.write_finding(replace(_finding(), finding_id=f"finding-{index:02d}"), _AT)


def _seed_verdicts(client: _RecordingDynamo, count: int) -> None:
    store = DynamoEvaluationVerdictStore(_VERDICTS, cast("Any", client), retention=_RETENTION)
    for index in range(count):
        store.write_evaluation_result(replace(_verdict(), evaluator_ref=f"evaluator-{index}"), _AT)


def _seed_correlated_receipts(client: _RecordingDynamo, count: int) -> None:
    for index in range(count):
        _seed_receipt(client, _receipt(f"audit-{index:02d}"))


def test_a_findings_partition_holding_exactly_twenty_reads_complete() -> None:
    """A Limit equal to the bound returned a continuation key at exactly the bound (defect)."""
    client = _RecordingDynamo()
    _seed_findings(client, 20)

    findings = cast("tuple[AuditFindingV3, ...]", _read_findings(client))

    assert len(findings) == 20
    assert client.requests_for("Query")[0]["Limit"] == 21


def test_a_findings_partition_holding_twenty_one_exceeds_its_bound() -> None:
    client = _RecordingDynamo()
    _seed_findings(client, 21)

    with pytest.raises(InvariantError, match="finding query exceeded"):
        _read_findings(client)


def test_an_admission_holding_exactly_eight_verdicts_reads_complete() -> None:
    client = _RecordingDynamo()
    _seed_verdicts(client, 8)

    verdicts = cast("tuple[EvaluationResultV3, ...]", _read_verdicts(client))

    assert len(verdicts) == 8
    assert client.requests_for("Query")[0]["Limit"] == 9


def test_an_admission_holding_nine_verdicts_exceeds_its_bound() -> None:
    client = _RecordingDynamo()
    _seed_verdicts(client, 9)

    with pytest.raises(InvariantError, match="result query exceeded"):
        _read_verdicts(client)


def test_a_correlation_holding_exactly_two_receipts_attests_both() -> None:
    client = _RecordingDynamo()
    _seed_correlated_receipts(client, 2)

    records = cast("tuple[ConversationAuditRecordV6, ...]", _discover(client))

    assert [record.audit_id for record in records] == ["audit-00", "audit-01"]


def test_a_correlation_holding_three_receipts_is_truncated_without_base_reads() -> None:
    client = _RecordingDynamo()
    _seed_correlated_receipts(client, 3)

    assert _discover(client) is None
    assert client.requests_for("GetItem") == []


# --- Retention: every write carries expires_at from the receipt's creation time (ADR-081 §5) ----


def _receipt_writes() -> list[tuple[str, Callable[[_RecordingDynamo], object]]]:
    return [
        (_RECEIPTS, _write_receipt),
        (_RECEIPTS, lambda client: _receipts(client).record_delivery(_delivery_disposition(), _AT)),
        (_RECEIPTS, _write_admission),
    ]


@pytest.mark.covers("ADR-081")
@pytest.mark.parametrize("family", _FAMILIES, ids=_IDS)
def test_a_family_record_expires_at_its_receipt_time_plus_retention_not_its_own_time(
    family: _Family,
) -> None:
    """The record's own timestamp is later than its receipt's; only the receipt's counts."""
    item = _stored_item(
        family.table, lambda scratch: family.write(scratch, "2026-09-05T08:00:00+00:00")
    )

    assert item["expires_at"] == {"N": "1796126400"}


@pytest.mark.covers("ADR-081")
@pytest.mark.parametrize("index", [0, 1, 2], ids=["receipt", "delivery-disposition", "admission"])
def test_a_receipts_table_record_expires_at_its_receipt_time_plus_retention(index: int) -> None:
    table, write = _receipt_writes()[index]

    item = _stored_item(table, write)

    assert item["expires_at"] == {"N": "1796126400"}


@pytest.mark.covers("ADR-081")
def test_record_retention_truncates_a_fractional_receipt_second_toward_the_past() -> None:
    """Every writer computes the same whole second from the same receipt, so replays agree."""
    assert RecordRetention(90).expires_at("2026-09-02T12:00:00.999999+00:00") == 1_796_126_400
    assert RecordRetention(1).expires_at("2026-09-02T12:00:00+00:00") == 1_788_436_800


@pytest.mark.covers("ADR-081")
@pytest.mark.parametrize("days", [0, -1, True, 1_000_000])
def test_record_retention_refuses_anything_but_a_positive_whole_day_count(days: int) -> None:
    with pytest.raises(ValueError, match="record retention days"):
        RecordRetention(days)


@pytest.mark.covers("ADR-081")
@pytest.mark.parametrize("created_at", ["2026-09-02T12:00:00", "2026-09-02T14:00:00+02:00", ""])
def test_record_retention_refuses_a_receipt_time_that_is_not_a_utc_instant(
    created_at: str,
) -> None:
    with pytest.raises(ValueError, match="receipt creation time"):
        RecordRetention(90).expires_at(created_at)


def _findings_without_retention(client: _RecordingDynamo) -> object:
    return DynamoAuditFindingStore(_FINDINGS, cast("Any", client)).write_finding(_finding(), _AT)


def _verdicts_without_retention(client: _RecordingDynamo) -> object:
    store = DynamoEvaluationVerdictStore(_VERDICTS, cast("Any", client))
    return store.write_evaluation_result(_verdict(), _AT)


def _receipt_without_retention(client: _RecordingDynamo) -> object:
    return DynamoAgentQualityAuditStore(_RECEIPTS, cast("Any", client)).create(_admitted_receipt())


@pytest.mark.covers("ADR-081")
@pytest.mark.parametrize(
    "write",
    [_findings_without_retention, _verdicts_without_retention, _receipt_without_retention],
    ids=["findings", "verdicts", "receipt"],
)
def test_a_writer_without_a_declared_retention_writes_nothing(
    write: Callable[[_RecordingDynamo], object],
) -> None:
    """A record that never expires would outlive the retention the ADR declares."""
    client = _RecordingDynamo()

    with pytest.raises(ValueError, match="require a declared record retention"):
        write(client)

    assert client.calls == []


def _replay_with_retention(family: _Family, days: int) -> Callable[[_RecordingDynamo, str], object]:
    retention = RecordRetention(days)

    def write(client: _RecordingDynamo, at: str) -> object:
        dynamodb = cast("Any", client)
        if family.table == _FINDINGS:
            return DynamoAuditFindingStore(_FINDINGS, dynamodb, retention=retention).write_finding(
                _finding(at), _AT
            )
        if family.table == _DELIVERIES:
            store = DynamoEvaluationDeliveryStore(_DELIVERIES, dynamodb, retention=retention)
            return store.write_evaluation_delivery(_delivery(at), _AT)
        if family.table == _WRITER_RECEIPTS:
            store = DynamoEvaluationWriterReceiptStore(
                _WRITER_RECEIPTS, dynamodb, retention=retention
            )
            return store.write_evaluation_writer_receipt(_writer_receipt(at), _AT)
        store = DynamoEvaluationVerdictStore(_VERDICTS, dynamodb, retention=retention)
        return store.write_evaluation_result(_verdict(at), _AT)

    return write


@pytest.mark.covers("ADR-081")
@pytest.mark.parametrize("family", _FAMILIES, ids=_IDS)
def test_a_replay_whose_expiry_differs_is_a_conflict_not_an_exact_replay(family: _Family) -> None:
    """The expiry is part of the replay comparison, so identical payloads are not enough."""
    client = _RecordingDynamo()
    family.write(client, _AT)

    with pytest.raises(InvariantError) as raised:
        _replay_with_retention(family, 91)(client, _AT)

    assert raised.value.code == ErrorCode("AGENT_QUALITY", 3)


@pytest.mark.covers("ADR-081")
def test_a_receipt_replay_whose_expiry_differs_is_a_conflict() -> None:
    client = _RecordingDynamo()
    _write_receipt(client)

    with pytest.raises(InvariantError):
        DynamoAgentQualityAuditStore(
            _RECEIPTS, cast("Any", client), retention=RecordRetention(91)
        ).create(_admitted_receipt())


# --- Expired as absent: TTL deletes some time after expiry, so readers must not wait for it -----


def _at_epoch(seconds: int) -> Callable[[], datetime]:
    return lambda: datetime.fromtimestamp(seconds, UTC)


def _read_family(
    family: _Family, client: _RecordingDynamo, clock: Callable[[], datetime]
) -> object:
    dynamodb = cast("Any", client)
    if family.table == _FINDINGS:
        return DynamoAuditFindingStore(_FINDINGS, dynamodb, clock=clock).get_findings(_AUDIT_ID)
    if family.table == _DELIVERIES:
        return DynamoEvaluationDeliveryStore(
            _DELIVERIES, dynamodb, clock=clock
        ).get_evaluation_delivery(_AUDIT_ID, _ADMISSION_REF)
    if family.table == _WRITER_RECEIPTS:
        return DynamoEvaluationWriterReceiptStore(
            _WRITER_RECEIPTS, dynamodb, clock=clock
        ).get_evaluation_writer_receipt(_AUDIT_ID, _ADMISSION_REF)
    return DynamoEvaluationVerdictStore(_VERDICTS, dynamodb, clock=clock).get_evaluation_results(
        _AUDIT_ID, _ADMISSION_REF
    )


_ABSENT: dict[str, object] = {
    _FINDINGS: (),
    _DELIVERIES: None,
    _WRITER_RECEIPTS: None,
    _VERDICTS: (),
}


@pytest.mark.covers("ADR-081")
@pytest.mark.parametrize("family", _FAMILIES, ids=_IDS)
def test_a_family_record_past_its_expiry_reads_as_absent(family: _Family) -> None:
    client = _RecordingDynamo()
    family.write(client, _AT)

    assert _read_family(family, client, _at_epoch(_EXPIRES_AT + 1)) == _ABSENT[family.table]


@pytest.mark.covers("ADR-081")
@pytest.mark.parametrize("family", _FAMILIES, ids=_IDS)
def test_a_family_record_at_its_expiry_second_is_still_present(family: _Family) -> None:
    client = _RecordingDynamo()
    family.write(client, _AT)

    assert _read_family(family, client, _at_epoch(_EXPIRES_AT)) != _ABSENT[family.table]


@pytest.mark.covers("ADR-081")
@pytest.mark.parametrize("family", _FAMILIES, ids=_IDS)
def test_a_family_record_written_before_retention_never_reads_as_expired(family: _Family) -> None:
    """Until the one-time backfill gives it one, a legacy item has no expiry and is kept."""
    client = _RecordingDynamo()
    item = _stored_item(family.table, lambda scratch: family.write(scratch, _AT))
    client.seed(family.table, {name: value for name, value in item.items() if name != "expires_at"})

    assert _read_family(family, client, _at_epoch(4_000_000_000)) != _ABSENT[family.table]


def _receipt_readers(
    clock: Callable[[], datetime],
) -> list[Callable[[_RecordingDynamo], object]]:
    def store(client: _RecordingDynamo) -> DynamoAgentQualityAuditStore:
        return DynamoAgentQualityAuditStore(_RECEIPTS, cast("Any", client), clock=clock)

    return [
        lambda client: store(client).get(_AUDIT_ID),
        lambda client: store(client).get_delivery(_AUDIT_ID),
        lambda client: store(client).get_evaluation_admission(_AUDIT_ID, _ADMISSION_REF),
        lambda client: store(client).discover(
            AuditCorrelationRequest(correlation_id=_CORRELATION_ID)
        ),
        lambda client: store(client).discover_bounded(
            AuditCorrelationRequest(correlation_id=_CORRELATION_ID), 2
        ),
    ]


_RECEIPT_READER_IDS = ["receipt", "delivery-disposition", "admission", "discover", "bounded"]
_RECEIPT_ABSENT: list[object] = [None, None, None, (), ()]


def _seed_every_receipts_family(client: _RecordingDynamo) -> None:
    _write_receipt(client)
    _receipts(client).record_delivery(_delivery_disposition(), _AT)
    _write_admission(client)


@pytest.mark.covers("ADR-081")
@pytest.mark.parametrize("index", range(5), ids=_RECEIPT_READER_IDS)
def test_a_receipts_table_record_past_its_expiry_reads_as_absent(index: int) -> None:
    """An expired receipt is absent even while its correlation index key has not been deleted."""
    client = _RecordingDynamo()
    _seed_every_receipts_family(client)

    read = _receipt_readers(_at_epoch(_EXPIRES_AT + 1))[index]

    assert read(client) == _RECEIPT_ABSENT[index]


@pytest.mark.covers("ADR-081")
@pytest.mark.parametrize("index", range(5), ids=_RECEIPT_READER_IDS)
def test_a_receipts_table_record_inside_its_retention_reads_back(index: int) -> None:
    client = _RecordingDynamo()
    _seed_every_receipts_family(client)

    read = _receipt_readers(_at_epoch(_EXPIRES_AT))[index]

    assert read(client) != _RECEIPT_ABSENT[index]


@pytest.mark.covers("ADR-081")
def test_a_legacy_receipt_without_expiry_is_attested_under_any_clock() -> None:
    client = _RecordingDynamo()
    item = cast("dict[str, Any]", audit_record_item(_admitted_receipt(), _EXPIRES_AT))
    client.seed(_RECEIPTS, {name: value for name, value in item.items() if name != "expires_at"})

    store = DynamoAgentQualityAuditStore(
        _RECEIPTS, cast("Any", client), clock=_at_epoch(4_000_000_000)
    )

    assert store.get(_AUDIT_ID) == _admitted_receipt()


def _streamed_receipt() -> ConversationAuditRecordV6:
    """One receipt as the streamed-answer runtime writes it (ADR-083): governed steps only."""
    return replace(
        _admitted_receipt(),
        page_schema_version="answer.markdown.v1",
        response_contract_version="conversation-stream.v4",
        trajectory=(
            AuditToolStepV3(
                ordinal=1,
                tool_name="catalog_list_metrics",
                disposition=AuditToolStepDispositionV3.SUCCEEDED,
                step_ref="3b2f7d0e-6c4a-4f9e-9b6d-2a1c8e7f5d40",
            ),
        ),
        presentation_plan_digest=None,
        public_block_kinds=(),
        public_block_count=0,
        released_character_count=512,
    )


@pytest.mark.covers("ADR-083")
def test_a_streamed_receipt_round_trips_through_the_receipts_table_exactly() -> None:
    client = _RecordingDynamo()
    record = _streamed_receipt()

    assert _receipts(client).create(record).disposition is AuditWriteDisposition.APPLIED
    assert _receipts(client).create(record).disposition is AuditWriteDisposition.EXACT_REPLAY
    stored = _receipts(client).get(_AUDIT_ID)
    assert stored == record
    assert stored is not None
    assert (stored.streamed, stored.released_character_count) == (True, 512)


@pytest.mark.covers("ADR-083")
def test_a_stored_v4_receipt_reads_through_the_store_as_v6_with_no_released_text() -> None:
    client = _RecordingDynamo()
    item = cast("dict[str, Any]", audit_record_item(_admitted_receipt(), _EXPIRES_AT))
    document = json.loads(item["payload"]["S"])
    del document["released_character_count"]
    del document["page_context_reference"]
    del document["context_status"]
    payload = json.dumps(
        {**document, "schema_version": "ConversationAuditRecord.v4"},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    client.seed(_RECEIPTS, {**item, "payload": {"S": payload}, "payload_digest": {"S": digest}})

    record = _receipts(client).get(_AUDIT_ID)

    assert record == _admitted_receipt()
    assert record is not None
    assert (record.schema_version, record.released_character_count) == (
        "ConversationAuditRecord.v6",
        0,
    )


@pytest.mark.covers("ADR-081")
def test_result_ingress_does_not_link_a_turn_whose_receipt_has_expired() -> None:
    client = _RecordingDynamo()
    _seed_receipt(client, _admitted_receipt())
    _write_writer_receipt(client, _AT)
    dynamodb = cast("Any", client)
    clock = _at_epoch(_EXPIRES_AT + 1)
    resolver = DynamoEvaluationResultLinkResolver(
        DynamoAgentQualityAuditStore(_RECEIPTS, dynamodb, clock=clock),
        DynamoEvaluationWriterReceiptStore(_WRITER_RECEIPTS, dynamodb, clock=clock),
    )

    resolution = resolver.resolve_evaluation_result(
        EvaluationResultLinkRequest(correlation_id=_CORRELATION_ID, trace_id=_TRACE_ID)
    )

    assert resolution.state is EvaluationResultLinkResolutionState.UNMATCHED
    assert resolution.target is None


@pytest.mark.covers("ADR-081")
def test_result_ingress_carries_the_receipt_creation_time_on_its_link_target() -> None:
    """The linker computes its verdict's expiry from this, without reading receipts."""
    client, resolution = _linking([_receipt()], [_writer_for(_AUDIT_ID)])

    assert resolution.target is not None
    assert resolution.target.receipt_created_at == _AT
    assert _RECEIPTS in {table for _, table in client.calls}


_MALFORMED_EXPIRIES = [
    {"N": "01796126400"},
    {"N": "1796126400.5"},
    {"N": "0"},
    {"N": "-1"},
    {"S": "1796126400"},
    {"N": "1796126400", "S": "1796126400"},
]
_MALFORMED_EXPIRY_IDS = ["leading-zero", "fraction", "zero", "negative", "string", "two-types"]


@pytest.mark.covers("ADR-081")
@pytest.mark.parametrize("expiry", _MALFORMED_EXPIRIES, ids=_MALFORMED_EXPIRY_IDS)
@pytest.mark.parametrize("family", _FAMILIES, ids=_IDS)
def test_a_family_record_with_a_malformed_expiry_is_an_invariant_conflict(
    family: _Family, expiry: dict[str, str]
) -> None:
    client = _RecordingDynamo()
    item = _stored_item(family.table, lambda scratch: family.write(scratch, _AT))
    client.seed(family.table, {**item, "expires_at": expiry})

    with pytest.raises(InvariantError) as raised:
        family.read(client)

    assert raised.value.code == ErrorCode("AGENT_QUALITY", 3)


@pytest.mark.covers("ADR-081")
@pytest.mark.parametrize("expiry", _MALFORMED_EXPIRIES, ids=_MALFORMED_EXPIRY_IDS)
def test_a_receipt_with_a_malformed_expiry_is_an_invariant_conflict(
    expiry: dict[str, str],
) -> None:
    client = _RecordingDynamo()
    item = _stored_item(_RECEIPTS, _write_receipt)
    client.seed(_RECEIPTS, {**item, "expires_at": expiry})

    with pytest.raises(InvariantError, match="receipt is malformed"):
        _get_receipt(client)


# --- The route's handoff transaction and the dispatcher's delivery carry the same expiry --------


def _handoff_request() -> EvaluationHandoffRequest:
    outbox = _initial_outbox()
    return EvaluationHandoffRequest(
        audit=_admitted_receipt(),
        admission=_admission(),
        outbox=outbox,
    )


def _initial_outbox() -> EvaluationOutboxInitialRecordV1:
    envelope = EvaluationOutboxEnvelopeV1(
        audit_id=_AUDIT_ID,
        admission_ref=_ADMISSION_REF,
        delivery_ref=evaluation_delivery_ref(_AUDIT_ID, _ADMISSION_REF),
        envelope=EvaluationEnvelope(
            question="What is the fictional governed result?",
            released_answer="The fictional released answer.",
            correlation_id=_CORRELATION_ID,
            audit_id=_AUDIT_ID,
            evaluation_admission_ref=_ADMISSION_REF,
        ),
        admitted_at=_AT,
    )
    return EvaluationOutboxInitialRecordV1(
        envelope=envelope,
        state=EvaluationOutboxStateV1(
            audit_id=_AUDIT_ID,
            admission_ref=_ADMISSION_REF,
            delivery_ref=envelope.delivery_ref,
            state=EvaluationOutboxDeliveryStateV1.PENDING,
            available_at=_AT,
            attempt_count=0,
        ),
    )


def _handoff(client: _RecordingDynamo, days: int = 90) -> object:
    return DynamoEvaluationHandoffStore(
        _RECEIPTS, _OUTBOX, cast("Any", client), retention=RecordRetention(days)
    ).persist(_handoff_request())


@pytest.mark.covers("ADR-081")
def test_a_handoff_admitting_its_outbox_row_later_than_its_receipt_is_refused() -> None:
    """The dispatcher and the writer take their records' expiry from the row's admission time."""
    outbox = _initial_outbox()
    later = replace(outbox.envelope, admitted_at="2026-09-02T12:00:01+00:00")

    with pytest.raises(ValueError, match="admission time must be the receipt's"):
        EvaluationHandoffRequest(
            audit=_admitted_receipt(),
            admission=_admission(),
            outbox=replace(outbox, envelope=later),
        )


@pytest.mark.covers("ADR-081")
def test_the_handoff_gives_its_receipt_and_admission_the_receipt_expiry_only() -> None:
    """The outbox row keeps its own delivered-row expiry; the records take the receipt's."""
    client = _RecordingDynamo()

    _handoff(client)

    receipts = client.tables[_RECEIPTS]
    assert receipts[(_AUDIT_ID, "RECORD")]["expires_at"] == {"N": "1796126400"}
    assert receipts[(_AUDIT_ID, "EVALUATION_ADMISSION#admission-01")]["expires_at"] == {
        "N": "1796126400"
    }
    assert "expires_at" not in client.tables[_OUTBOX][(_ADMISSION_REF,)]


@pytest.mark.covers("ADR-081")
def test_a_cancelled_handoff_holding_exactly_its_items_is_an_exact_replay() -> None:
    client = _RecordingDynamo()
    _handoff(client)
    client.cancel_transactions = True

    replay = cast("EvaluationOutboxWriteResult", _handoff(client))

    assert replay.disposition is EvaluationOutboxWriteDisposition.EXACT_REPLAY


@pytest.mark.covers("ADR-081")
def test_a_cancelled_handoff_whose_retained_expiry_differs_is_a_conflict() -> None:
    client = _RecordingDynamo()
    _handoff(client)
    client.cancel_transactions = True

    with pytest.raises(InvariantError) as raised:
        _handoff(client, days=91)

    assert raised.value.code == ErrorCode("EVALUATION", 5)


@pytest.mark.covers("ADR-081")
def test_the_dispatcher_expires_a_delivery_receipt_from_the_row_admission_time() -> None:
    """`admitted_at` is the receipt's creation time; the later delivery time never counts."""
    client = _RecordingDynamo()
    client.seed(_OUTBOX, _leased_outbox_item())

    _deliver(client, delivered_at="2026-09-02T12:05:00+00:00")

    (item,) = client.tables[_DELIVERIES].values()
    assert item["expires_at"] == {"N": "1796126400"}


# --- ADR-079: V2 dispatch receipts, the V1 classification, and V3 verdict provenance -----------


def _legacy_item(document: Mapping[str, object], item_key: str, record_type: str) -> dict[str, Any]:
    payload = json.dumps(document, separators=(",", ":"), sort_keys=True)
    return {
        "audit_id": {"S": _AUDIT_ID},
        "item_key": {"S": item_key},
        "record_type": {"S": record_type},
        "payload": {"S": payload},
        "payload_digest": {"S": hashlib.sha256(payload.encode()).hexdigest()},
        "expires_at": {"N": str(_EXPIRES_AT)},
    }


def _v1_delivery_document(admission_ref: str = _ADMISSION_REF) -> dict[str, object]:
    return {
        "schema_version": "EvaluationDeliveryReceipt.v1",
        "audit_id": _AUDIT_ID,
        "admission_ref": admission_ref,
        "delivery_ref": evaluation_delivery_ref(_AUDIT_ID, _ADMISSION_REF),
        "admitted_at": _AT,
    }


@pytest.mark.covers("ADR-079")
def test_a_stored_v1_delivery_receipt_reads_as_predating_the_gate_not_as_a_v2_decision() -> None:
    """A V1 receipt names no evaluator set, so no verdict may be stamped from it."""
    client = _RecordingDynamo()
    client.seed(
        _DELIVERIES,
        _legacy_item(
            _v1_delivery_document(), "EVALUATION_DELIVERY#admission-01", "EVALUATION_DELIVERY"
        ),
    )

    read = _read_delivery(client)

    assert read == EvaluationDeliveryPredatesGate(audit_id=_AUDIT_ID, admission_ref=_ADMISSION_REF)


@pytest.mark.covers("ADR-079")
@pytest.mark.parametrize(
    "document",
    [
        _v1_delivery_document("admission-02"),
        {**_v1_delivery_document(), "schema_version": "EvaluationDeliveryReceipt.v3"},
        {**_v1_delivery_document(), "schema_version": None},
    ],
    ids=["v1-other-admission", "unknown-version", "no-version"],
)
def test_a_delivery_receipt_that_is_neither_v2_nor_an_exact_v1_is_a_conflict(
    document: dict[str, object],
) -> None:
    client = _RecordingDynamo()
    client.seed(
        _DELIVERIES,
        _legacy_item(document, "EVALUATION_DELIVERY#admission-01", "EVALUATION_DELIVERY"),
    )

    with pytest.raises(InvariantError, match="delivery receipt is malformed"):
        _read_delivery(client)


@pytest.mark.covers("ADR-079")
def test_a_not_dispatched_receipt_round_trips_with_its_reason_and_no_family() -> None:
    client = _RecordingDynamo()
    store = DynamoEvaluationDeliveryStore(
        _DELIVERIES, cast("Any", client), retention=_RETENTION, clock=_now
    )

    store.write_evaluation_delivery(_withheld(), _AT)

    assert store.get_evaluation_delivery(_AUDIT_ID, _ADMISSION_REF) == _withheld()


@pytest.mark.covers("ADR-079")
def test_a_stored_v2_verdict_is_skipped_not_read_and_a_v3_one_beside_it_is_returned() -> None:
    """Verdicts linked before ADR-079 carry no provenance; they stay but are never read."""
    document = {
        "schema_version": "EvaluationResult.v2",
        "audit_id": _AUDIT_ID,
        "admission_ref": _ADMISSION_REF,
        "evaluator_ref": "grounded-01",
        "evaluator_name": "fictional_grounded",
        "result_ref": "result-00",
        "state": "completed",
        "label": "grounded",
        "score": 1.0,
        "evaluated_at": _AT,
    }
    client = _RecordingDynamo()
    client.seed(
        _VERDICTS,
        _legacy_item(document, "EVALUATION_RESULT#admission-01#grounded-01", "EVALUATION_RESULT"),
    )
    _write_verdict(client, _AT)

    assert _read_verdicts(client) == (_verdict(),)


@pytest.mark.covers("ADR-079")
def test_a_verdict_carries_the_provenance_it_was_stamped_with_through_the_store() -> None:
    client = _RecordingDynamo()
    _write_verdict(client, _AT)

    (verdict,) = cast("tuple[EvaluationResultV3, ...]", _read_verdicts(client))

    assert (verdict.family, verdict.config_id, verdict.evaluator_revision) == (
        EvaluationFamilyV1.V4,
        "fictional_released_v4-0001",
        "r1",
    )
    assert (verdict.model_id, verdict.prompt_digest) == (_EVALUATOR.model_id, "b" * 64)


@pytest.mark.covers("ADR-079")
def test_withhold_ends_the_owned_lease_and_writes_its_receipt_in_one_transaction() -> None:
    client = _RecordingDynamo()
    client.seed(_OUTBOX, _leased_outbox_item())

    _withhold(client)

    (request,) = client.requests_for("TransactWriteItems")
    update, put = cast("list[dict[str, Any]]", request["TransactItems"])
    assert update["Update"]["TableName"] == _OUTBOX
    assert update["Update"]["ConditionExpression"] == "#state = :leased AND lease_ref = :lease"
    assert update["Update"]["ExpressionAttributeValues"][":lease"] == {"S": "lease-01"}
    assert update["Update"]["ExpressionAttributeValues"][":terminal"] == {"S": "not_dispatched"}
    assert update["Update"]["ExpressionAttributeNames"]["#at"] == DECIDED_AT
    # Removing the index keys takes the row out of dispatch for good; a re-enable never sends it.
    assert "ready_partition, ready_sort" in update["Update"]["UpdateExpression"]
    # The row keeps the delivered-row expiry in whole epoch seconds, as a delivered row does.
    assert update["Update"]["ExpressionAttributeValues"][":expires_at"] == {"N": "1790000000"}
    assert put["Put"]["TableName"] == _DELIVERIES
    assert put["Put"]["ConditionExpression"] == "attribute_not_exists(item_key)"
    assert _read_delivery(client) == _withheld()
    assert client.tables[_OUTBOX][(_ADMISSION_REF,)][DECIDED_AT] == {"S": _AT}
    assert _RECEIPTS not in {table for _, table in client.calls}


@pytest.mark.covers("ADR-079")
def test_a_withheld_row_reads_back_in_its_terminal_state_with_its_decision_time() -> None:
    client = _RecordingDynamo()
    client.seed(_OUTBOX, _leased_outbox_item())
    _withhold(client)
    store = DynamoEvaluationOutboxDispatchStore(
        _OUTBOX, _DELIVERIES, cast("Any", client), retention=_RETENTION
    )

    work = store._get_work(_ADMISSION_REF)  # pyright: ignore[reportPrivateUsage] - exact read seam

    assert work is not None
    assert work.state.state is EvaluationOutboxDeliveryStateV1.NOT_DISPATCHED
    assert (work.state.decided_at, work.state.lease, work.state.delivered_at) == (_AT, None, None)


@pytest.mark.covers("ADR-079")
def test_a_cancelled_withhold_that_already_holds_its_exact_receipt_is_an_exact_replay() -> None:
    client = _RecordingDynamo()
    client.seed(_OUTBOX, _leased_outbox_item())
    _withhold(client)
    client.calls.clear()
    client.cancel_transactions = True

    _withhold(client)

    assert client.tables_called("GetItem") == {_OUTBOX, _DELIVERIES}


@pytest.mark.covers("ADR-079")
def test_a_cancelled_withhold_with_another_reason_is_a_conflict() -> None:
    client = _RecordingDynamo()
    client.seed(_OUTBOX, _leased_outbox_item())
    _withhold(client)
    client.cancel_transactions = True

    with pytest.raises(InvariantError) as raised:
        _withhold(client, EvaluationNotDispatchedReasonV1.DELIVERY_UNKNOWN)

    assert raised.value.code == ErrorCode("EVALUATION", 5)


@pytest.mark.covers("ADR-079")
def test_a_cancelled_withhold_whose_row_is_still_leased_is_a_conflict() -> None:
    """A lost lease leaves the row leased; the receipt alone does not make it withheld."""
    client = _RecordingDynamo(cancel_transactions=True)
    client.seed(_OUTBOX, _leased_outbox_item())
    client.seed(
        _DELIVERIES, cast("dict[str, Any]", evaluation_delivery_item(_withheld(), _EXPIRES_AT))
    )

    with pytest.raises(InvariantError) as raised:
        _withhold(client)

    assert raised.value.code == ErrorCode("EVALUATION", 5)


@pytest.mark.covers("ADR-079")
def test_a_delivered_row_cannot_be_replayed_as_withheld() -> None:
    client = _RecordingDynamo()
    client.seed(_OUTBOX, _leased_outbox_item())
    _deliver(client)
    client.cancel_transactions = True

    with pytest.raises(InvariantError):
        _withhold(client)


# --- ADR-078 §5 and ADR-081 §5: stored expiries on reads, and the turns index -----------------


@pytest.mark.covers("ADR-081")
def test_every_review_read_returns_the_expiry_stored_on_its_item() -> None:
    client = _RecordingDynamo()
    _seed_receipt(client, _admitted_receipt())
    receipts = _receipts(client)
    receipts.record_delivery(_delivery_disposition(), _AT)
    receipts.create_evaluation_admission(_admission(), _AT)
    _write_finding(client, _AT)
    _write_delivery(client, _AT)
    _write_writer_receipt(client, _AT)
    _write_verdict(client, _AT)
    dynamodb = cast("Any", client)

    stored = receipts.get_with_expiry(_AUDIT_ID)
    assert stored is not None
    assert (stored.record, stored.expires_at) == (_admitted_receipt(), _EXPIRES_AT)
    delivery = receipts.get_delivery_with_expiry(_AUDIT_ID)
    admission = receipts.get_evaluation_admission_with_expiry(_AUDIT_ID, _ADMISSION_REF)
    findings = DynamoAuditFindingStore(_FINDINGS, dynamodb, clock=_now).get_findings_with_expiry(
        _AUDIT_ID
    )
    evaluation_delivery = DynamoEvaluationDeliveryStore(
        _DELIVERIES, dynamodb, clock=_now
    ).get_evaluation_delivery_with_expiry(_AUDIT_ID, _ADMISSION_REF)
    writer = DynamoEvaluationWriterReceiptStore(
        _WRITER_RECEIPTS, dynamodb, clock=_now
    ).get_evaluation_writer_receipt_with_expiry(_AUDIT_ID, _ADMISSION_REF)
    verdicts = DynamoEvaluationVerdictStore(
        _VERDICTS, dynamodb, clock=_now
    ).get_evaluation_results_with_expiry(_AUDIT_ID, _ADMISSION_REF)
    assert delivery is not None and admission is not None
    assert evaluation_delivery is not None and writer is not None
    assert [
        delivery.expires_at,
        admission.expires_at,
        *(item.expires_at for item in findings),
        evaluation_delivery.expires_at,
        writer.expires_at,
        *(item.expires_at for item in verdicts),
    ] == [_EXPIRES_AT] * 6


@pytest.mark.covers("ADR-081")
def test_a_receipt_written_before_retention_reads_with_no_expiry() -> None:
    client = _RecordingDynamo()
    item = cast("dict[str, Any]", audit_record_item(_admitted_receipt(), _EXPIRES_AT))
    del item["expires_at"]
    client.seed(_RECEIPTS, item)
    stored = _receipts(client).get_with_expiry(_AUDIT_ID)
    assert stored is not None
    assert stored.expires_at is None


_TURNS_INDEX = f"{_RECEIPTS}/index/turns-by-day"


def _turns_request(**overrides: Any) -> ReceiptTurnsPageRequest:
    values: dict[str, Any] = {
        "window_from": datetime(2026, 9, 1, 6, 0, tzinfo=UTC),
        "window_to": datetime(2026, 9, 3, 6, 0, tzinfo=UTC),
        "limit": 2,
    }
    return ReceiptTurnsPageRequest(**{**values, **overrides})


def _row(  # noqa: PLR0913 - the four values a fictional index row varies by
    audit_id: str,
    created_at: str,
    *,
    outcome: str = "complete",
    expires_at: int = _EXPIRES_AT,
) -> dict[str, Any]:
    return {
        "audit_id": {"S": audit_id},
        "item_key": {"S": "RECORD"},
        "turns_day": {"S": created_at[:10]},
        "turns_created_at": {"S": created_at},
        "correlation_id": {"S": _CORRELATION_ID},
        "terminal_outcome": {"S": outcome},
        "expires_at": {"N": str(expires_at)},
    }


def _continuation(row: dict[str, Any]) -> dict[str, Any]:
    return {name: row[name] for name in ("audit_id", "item_key", "turns_day", "turns_created_at")}


def _page(*rows: dict[str, Any], last: dict[str, Any] | None = None) -> dict[str, Any]:
    response: dict[str, Any] = {"Items": list(rows)}
    if last is not None:
        response["LastEvaluatedKey"] = _continuation(last)
    return response


def _list(
    client: _RecordingDynamo, request: ReceiptTurnsPageRequest
) -> tuple[list[str], str | None]:
    page = _receipts(client).list_turns(request)
    return [row.audit_id for row in page.turns], page.cursor


@pytest.mark.covers("ADR-078")
def test_the_first_page_is_one_newest_first_query_of_the_window_last_day() -> None:
    newest = _row("audit-c", "2026-09-03T05:00:00+00:00")
    client = _RecordingDynamo(query_responses=[_page(newest)])
    ids, _ = _list(client, _turns_request())
    assert ids == ["audit-c"]
    assert client.requests_for("Query") == [
        {
            "TableName": _RECEIPTS,
            "IndexName": "turns-by-day",
            "KeyConditionExpression": (
                "turns_day = :day AND turns_created_at BETWEEN :low AND :high"
            ),
            "ExpressionAttributeValues": {
                ":day": {"S": "2026-09-03"},
                ":low": {"S": "2026-09-03T00:00:00"},
                ":high": {"S": "2026-09-03T06:00:00~"},
            },
            "ScanIndexForward": False,
            "Limit": 2,
        }
    ]
    assert client.tables_called("Query") == {_TURNS_INDEX}
    assert client.tables_called("GetItem") == set()


@pytest.mark.covers("ADR-078")
def test_following_the_cursor_returns_every_matching_receipt_once_across_days() -> None:
    """Short and empty pages still carry a cursor; only the window's first day ends the walk."""
    day3 = [_row("audit-f", "2026-09-03T05:00:00+00:00")]
    day2 = [
        _row("audit-e", "2026-09-02T20:00:00+00:00"),
        _row("audit-d", "2026-09-02T10:00:00+00:00"),
        _row("audit-c", "2026-09-02T09:00:00+00:00"),
    ]
    day1 = [_row("audit-b", "2026-09-01T07:00:00+00:00")]
    client = _RecordingDynamo(
        query_responses=[
            _page(*day3),
            _page(day2[0], day2[1], last=day2[1]),
            _page(day2[2], last=day2[2]),
            _page(),
            _page(*day1),
        ]
    )
    request = _turns_request()
    seen: list[str] = []
    pages: list[list[str]] = []
    cursor: str | None = None
    for _ in range(6):
        ids, cursor = _list(client, replace(request, cursor=cursor))
        pages.append(ids)
        seen.extend(ids)
        if cursor is None:
            break
    # A short page and an empty day-2 continuation each still carry a cursor, then day 1.
    assert pages == [["audit-f"], ["audit-e", "audit-d"], ["audit-c"], [], ["audit-b"]]
    assert seen == ["audit-f", "audit-e", "audit-d", "audit-c", "audit-b"]
    assert cursor is None
    days = [
        query["ExpressionAttributeValues"][":day"]["S"] for query in client.requests_for("Query")
    ]
    assert days == ["2026-09-03", "2026-09-02", "2026-09-02", "2026-09-02", "2026-09-01"]
    starts = [query.get("ExclusiveStartKey") for query in client.requests_for("Query")]
    assert starts == [None, None, _continuation(day2[1]), _continuation(day2[2]), None]


@pytest.mark.covers("ADR-078")
def test_rows_outside_the_half_open_window_are_dropped_after_the_read() -> None:
    at_end = _row("audit-end", "2026-09-03T06:00:00+00:00")
    inside = _row("audit-in", "2026-09-03T05:59:59.999999+00:00")
    client = _RecordingDynamo(query_responses=[_page(at_end, inside)])
    ids, cursor = _list(client, _turns_request())
    assert ids == ["audit-in"]
    assert cursor is not None


@pytest.mark.covers("ADR-078")
def test_the_window_start_bounds_the_first_day() -> None:
    before = _row("audit-early", "2026-09-01T05:59:59+00:00")
    at_start = _row("audit-start", "2026-09-01T06:00:00+00:00")
    request = _turns_request(
        window_from=datetime(2026, 9, 1, 6, 0, tzinfo=UTC),
        window_to=datetime(2026, 9, 1, 18, 0, tzinfo=UTC),
    )
    client = _RecordingDynamo(query_responses=[_page(at_start, before)])
    ids, cursor = _list(client, request)
    assert (ids, cursor) == (["audit-start"], None)
    values = client.requests_for("Query")[0]["ExpressionAttributeValues"]
    assert (values[":low"], values[":high"]) == (
        {"S": "2026-09-01T06:00:00"},
        {"S": "2026-09-01T18:00:00~"},
    )


@pytest.mark.covers("ADR-078")
def test_filters_and_the_expired_record_rule_apply_after_the_read() -> None:
    expired = _row("audit-expired", "2026-09-03T05:00:00+00:00", expires_at=1_788_000_000)
    refused = _row("audit-refused", "2026-09-03T04:00:00+00:00", outcome="governed_refusal")
    complete = _row("audit-complete", "2026-09-03T03:00:00+00:00")
    client = _RecordingDynamo(query_responses=[_page(expired, refused, complete)])
    request = _turns_request(terminal_outcomes=frozenset({AuditTerminalOutcomeV2.COMPLETE}))
    ids, cursor = _list(client, request)
    assert ids == ["audit-complete"]
    assert cursor is not None


@pytest.mark.covers("ADR-078", "ADR-081")
def test_a_turns_row_never_carries_the_principal() -> None:
    client = _RecordingDynamo(query_responses=[_page(_row("audit-a", "2026-09-03T05:00:00+00:00"))])
    (row,) = _receipts(client).list_turns(_turns_request()).turns
    assert set(row.__dataclass_fields__) == {
        "audit_id",
        "correlation_id",
        "created_at",
        "terminal_outcome",
        "expires_at",
    }


def _first_cursor() -> str:
    client = _RecordingDynamo(query_responses=[_page()])
    cursor = _receipts(client).list_turns(_turns_request()).cursor
    assert cursor is not None
    return cursor


@pytest.mark.covers("ADR-078")
@pytest.mark.parametrize(
    "changed",
    [
        {"window_from": datetime(2026, 9, 1, 7, 0, tzinfo=UTC)},
        {"window_to": datetime(2026, 9, 3, 7, 0, tzinfo=UTC)},
        {"terminal_outcomes": frozenset({AuditTerminalOutcomeV2.COMPLETE})},
    ],
    ids=["window-start", "window-end", "filters"],
)
def test_a_cursor_is_refused_with_another_window_or_filter_set(changed: dict[str, Any]) -> None:
    client = _RecordingDynamo()
    with pytest.raises(ValueError, match="another window or filter set"):
        _receipts(client).list_turns(_turns_request(cursor=_first_cursor(), **changed))
    assert client.requests == []


def test_a_cursor_stays_valid_with_another_page_limit() -> None:
    client = _RecordingDynamo(query_responses=[_page()])
    _receipts(client).list_turns(_turns_request(cursor=_first_cursor(), limit=50))
    assert client.requests_for("Query")[0]["ExpressionAttributeValues"][":day"] == {
        "S": "2026-09-02"
    }


def _reencoded(cursor: str, **edits: object) -> str:
    document = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
    document.update(edits)
    encoded = json.dumps(document, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(encoded).decode().rstrip("=")


_CURSOR_TAMPERS: list[Callable[[str], str]] = [
    lambda cursor: cursor + "A",
    lambda _cursor: "not base64 !",
    lambda cursor: _reencoded(cursor, d="2026-08-31"),
    lambda cursor: _reencoded(cursor, d="2026-9-2"),
    lambda cursor: _reencoded(cursor, v=2),
    lambda cursor: _reencoded(cursor, extra=1),
    lambda cursor: _reencoded(cursor, k=["audit-a"]),
    lambda cursor: _reencoded(
        cursor,
        k={
            "audit_id": "a",
            "item_key": "RECORD",
            "turns_day": "2026-09-01",
            "turns_created_at": "t",
        },
    ),
    lambda cursor: _reencoded(
        cursor,
        k={
            "audit_id": "a",
            "item_key": "DELIVERY",
            "turns_day": "2026-09-02",
            "turns_created_at": "t",
        },
    ),
    lambda cursor: _reencoded(cursor, k={"audit_id": "a"}),
    lambda _cursor: "A" * 3000,
    lambda cursor: _reencoded(
        cursor,
        k={
            "audit_id": "a",
            "item_key": "RECORD",
            "turns_day": "2026-09-02",
            "turns_created_at": "2026-09-03T00:00:01+00:00",
        },
    ),
    lambda cursor: _reencoded(
        cursor,
        k={
            "audit_id": "a",
            "item_key": "RECORD",
            "turns_day": "2026-09-02",
            "turns_created_at": "2026-09-01T23:59:59+00:00",
        },
    ),
]
_CURSOR_TAMPER_IDS = [
    "trailing-bytes",
    "not-base64",
    "day-outside-window",
    "non-canonical-day",
    "version",
    "extra-member",
    "key-not-object",
    "key-of-another-day",
    "key-of-another-item",
    "partial-key",
    "too-long",
    "key-after-day-range",
    "key-before-day-range",
]


@pytest.mark.covers("ADR-078")
@pytest.mark.parametrize("tamper", _CURSOR_TAMPERS, ids=_CURSOR_TAMPER_IDS)
def test_a_tampered_or_malformed_cursor_is_refused_before_any_read(
    tamper: Callable[[str], str],
) -> None:
    client = _RecordingDynamo()
    with pytest.raises(ValueError, match="cursor"):
        _receipts(client).list_turns(_turns_request(cursor=tamper(_first_cursor())))
    assert client.requests == []


_ROW_EDITS: list[Callable[[dict[str, Any]], dict[str, Any]]] = [
    lambda row: {**row, "payload": {"S": "{}"}},
    lambda row: {**row, "principal": {"S": _PRINCIPAL}},
    lambda row: {name: value for name, value in row.items() if name != "expires_at"},
    lambda row: {**row, "turns_day": {"S": "2026-09-02"}},
    lambda row: {**row, "item_key": {"S": "DELIVERY"}},
    lambda row: {**row, "terminal_outcome": {"S": "unknown"}},
    lambda row: {**row, "correlation_id": {"S": "not-normalized"}},
]
_ROW_EDIT_IDS = [
    "payload",
    "principal",
    "no-expiry",
    "other-day",
    "not-a-receipt",
    "outcome",
    "correlation",
]


@pytest.mark.covers("ADR-078")
@pytest.mark.parametrize("edit", _ROW_EDITS, ids=_ROW_EDIT_IDS)
def test_a_malformed_turns_row_is_an_invariant_conflict(
    edit: Callable[[dict[str, Any]], dict[str, Any]],
) -> None:
    row = edit(_row("audit-a", "2026-09-03T05:00:00+00:00"))
    client = _RecordingDynamo(query_responses=[_page(row)])
    with pytest.raises(InvariantError, match="malformed row") as raised:
        _receipts(client).list_turns(_turns_request())
    assert raised.value.code == ErrorCode("AGENT_QUALITY", 3)


@pytest.mark.covers("ADR-078")
def test_a_malformed_continuation_key_is_an_invariant_conflict() -> None:
    row = _row("audit-a", "2026-09-03T05:00:00+00:00")
    client = _RecordingDynamo(
        query_responses=[{"Items": [row], "LastEvaluatedKey": {"audit_id": {"S": "audit-a"}}}]
    )
    with pytest.raises(InvariantError, match="continuation key"):
        _receipts(client).list_turns(_turns_request())


def test_a_turns_read_that_fails_in_the_sdk_is_unreadable() -> None:
    client = _RecordingDynamo(
        failures={"Query": EndpointConnectionError(endpoint_url="https://dynamodb.invalid")}
    )
    with pytest.raises(DependencyError) as raised:
        _receipts(client).list_turns(_turns_request())
    assert raised.value.code == ErrorCode("AGENT_QUALITY", 1)
