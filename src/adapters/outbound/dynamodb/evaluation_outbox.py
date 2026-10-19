# SPDX-License-Identifier: MIT-0
"""Atomic DynamoDB evaluation handoff and leased outbox dispatch state.

The route's handoff and the dispatcher's lease are the outbox's two writers. The handoff also
creates the receipt and admission in the receipts table; the dispatcher's delivery receipt goes to
the evaluation deliveries table, its only writer (ADR-080 §1).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, NoReturn

import boto3
from botocore.exceptions import ClientError

from adapters.inbound.conversation.contract_codec import contract_to_json, decode_contract
from adapters.outbound.dynamodb.audit import (
    DynamoAgentQualityAuditStore,
    DynamoEvaluationDeliveryStore,
    audit_record_item,
    evaluation_admission_item,
    evaluation_delivery_item,
)
from adapters.outbound.dynamodb.items import read_int, read_text, write_int, write_text
from domain.conversation.audit import RecordRetention
from domain.evaluation.linkage import EvaluationDeliveryOutcomeV1, EvaluationDeliveryReceiptV2
from domain.evaluation.outbox import (
    EvaluationOutboxDeliveryStateV1,
    EvaluationOutboxEnvelopeV1,
    EvaluationOutboxInitialRecordV1,
    EvaluationOutboxLeaseV1,
    EvaluationOutboxStateV1,
)
from domain.shared_kernel.errors import DependencyError, ErrorCode, InvariantError
from ports.outbound.evaluation_outbox import (
    EvaluationHandoffPort,
    EvaluationHandoffRequest,
    EvaluationOutboxClaimRequest,
    EvaluationOutboxDeliverRequest,
    EvaluationOutboxDispatchPort,
    EvaluationOutboxDueRequest,
    EvaluationOutboxReleaseRequest,
    EvaluationOutboxWithholdRequest,
    EvaluationOutboxWorkItem,
    EvaluationOutboxWriteDisposition,
    EvaluationOutboxWriteResult,
)

if TYPE_CHECKING:
    from types_boto3_dynamodb.client import DynamoDBClient
    from types_boto3_dynamodb.type_defs import AttributeValueTypeDef, TransactWriteItemTypeDef

ADMISSION_REF = "admission_ref"
RECORD_TYPE = "record_type"
OUTBOX_RECORD = "EVALUATION_OUTBOX"
PAYLOAD = "payload"
PAYLOAD_DIGEST = "payload_digest"
AUDIT_ID = "audit_id"
DELIVERY_REF = "delivery_ref"
STATE = "state"
ATTEMPT_COUNT = "attempt_count"
READY_AT = "ready_at"
READY_PARTITION = "ready_partition"
READY_SORT = "ready_sort"
READY_INDEX = "dispatch-ready"
DISPATCHABLE = "DISPATCHABLE"
LEASE_REF = "lease_ref"
LEASE_ACQUIRED_AT = "lease_acquired_at"
LEASE_EXPIRES_AT = "lease_expires_at"
DELIVERED_AT = "delivered_at"
DECIDED_AT = "decided_at"
EXPIRES_AT = "expires_at"
EVALUATION_OUTBOX_UNREADABLE = ErrorCode("EVALUATION", 3)
EVALUATION_OUTBOX_UNWRITABLE = ErrorCode("EVALUATION", 4)
EVALUATION_OUTBOX_CONFLICT = ErrorCode("EVALUATION", 5)


def _digest(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _initial_item(record: EvaluationOutboxInitialRecordV1) -> dict[str, AttributeValueTypeDef]:
    """Build one content-bearing pending item and content-free dispatch index projection."""
    envelope = record.envelope
    payload = contract_to_json(envelope)
    ready_at = record.state.available_at
    return {
        ADMISSION_REF: write_text(envelope.admission_ref),
        RECORD_TYPE: write_text(OUTBOX_RECORD),
        PAYLOAD: write_text(payload),
        PAYLOAD_DIGEST: write_text(_digest(payload)),
        AUDIT_ID: write_text(envelope.audit_id),
        DELIVERY_REF: write_text(envelope.delivery_ref),
        STATE: write_text(EvaluationOutboxDeliveryStateV1.PENDING.value),
        ATTEMPT_COUNT: write_int(0),
        READY_AT: write_text(ready_at),
        READY_PARTITION: write_text(DISPATCHABLE),
        READY_SORT: write_text(f"{ready_at}#{envelope.admission_ref}"),
    }


def _envelope(item: dict[str, AttributeValueTypeDef]) -> EvaluationOutboxEnvelopeV1:
    payload = read_text(item, PAYLOAD)
    if read_text(item, PAYLOAD_DIGEST) != _digest(payload):
        raise ValueError("stored evaluation outbox payload digest does not match.")
    envelope = decode_contract(payload, EvaluationOutboxEnvelopeV1)
    if (
        envelope.admission_ref != read_text(item, ADMISSION_REF)
        or envelope.audit_id != read_text(item, AUDIT_ID)
        or envelope.delivery_ref != read_text(item, DELIVERY_REF)
    ):
        raise ValueError("stored evaluation outbox custody does not match its payload.")
    return envelope


def _work_item(item: dict[str, AttributeValueTypeDef]) -> EvaluationOutboxWorkItem:
    envelope = _envelope(item)
    state = EvaluationOutboxDeliveryStateV1(read_text(item, STATE))
    available_at = read_text(item, READY_AT)
    attempt_count = read_int(item, ATTEMPT_COUNT)
    lease = None
    delivered_at = None
    decided_at = None
    if state is EvaluationOutboxDeliveryStateV1.LEASED:
        lease = EvaluationOutboxLeaseV1(
            audit_id=envelope.audit_id,
            admission_ref=envelope.admission_ref,
            delivery_ref=envelope.delivery_ref,
            lease_ref=read_text(item, LEASE_REF),
            acquired_at=read_text(item, LEASE_ACQUIRED_AT),
            expires_at=read_text(item, LEASE_EXPIRES_AT),
            attempt=attempt_count,
        )
    elif state is EvaluationOutboxDeliveryStateV1.DELIVERED:
        delivered_at = read_text(item, DELIVERED_AT)
    elif state is EvaluationOutboxDeliveryStateV1.NOT_DISPATCHED:
        decided_at = read_text(item, DECIDED_AT)
    return EvaluationOutboxWorkItem(
        envelope=envelope,
        state=EvaluationOutboxStateV1(
            audit_id=envelope.audit_id,
            admission_ref=envelope.admission_ref,
            delivery_ref=envelope.delivery_ref,
            state=state,
            available_at=available_at,
            attempt_count=attempt_count,
            lease=lease,
            delivered_at=delivered_at,
            decided_at=decided_at,
        ),
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class _Settlement:
    """One leased row's terminal transition and the receipt written with it."""

    work: EvaluationOutboxWorkItem
    lease_ref: str
    receipt: EvaluationDeliveryReceiptV2
    terminal: EvaluationOutboxDeliveryStateV1
    time_attribute: str
    expires_at_epoch: int


class _EvaluationOutboxTable:
    """Exact reads of the content-bearing outbox table shared by its two writers (ADR-059)."""

    def __init__(self, outbox_table_name: str, client: DynamoDBClient | None = None) -> None:
        if not outbox_table_name.strip():
            raise ValueError("evaluation outbox table name is required.")
        self._outbox_table = outbox_table_name
        self._client = client

    @property
    def _dynamodb(self) -> DynamoDBClient:
        if self._client is None:
            self._client = boto3.client("dynamodb")
        return self._client

    def _get_item(self, admission_ref: str) -> dict[str, AttributeValueTypeDef] | None:
        try:
            response = self._dynamodb.get_item(
                TableName=self._outbox_table,
                Key={ADMISSION_REF: write_text(admission_ref)},
                ConsistentRead=True,
            )
        except Exception as error:
            self._read_error(error)
        found = response.get("Item")
        return None if found is None else dict(found)

    def _get_work(self, admission_ref: str) -> EvaluationOutboxWorkItem | None:
        item = self._get_item(admission_ref)
        if item is None:
            return None
        try:
            return _work_item(item)
        except TypeError, ValueError:
            self._conflict("Retained evaluation outbox item is malformed.")

    @staticmethod
    def _read_error(error: Exception) -> NoReturn:
        raise DependencyError(
            EVALUATION_OUTBOX_UNREADABLE,
            "Evaluation outbox data could not be read.",
            context={"cause": type(error).__name__},
        ) from None

    @staticmethod
    def _write_error(error: Exception) -> NoReturn:
        raise DependencyError(
            EVALUATION_OUTBOX_UNWRITABLE,
            "Evaluation outbox data could not be written.",
            context={"cause": type(error).__name__},
        ) from None

    @staticmethod
    def _conflict(description: str) -> NoReturn:
        raise InvariantError(EVALUATION_OUTBOX_CONFLICT, description)


class DynamoEvaluationHandoffStore(_EvaluationOutboxTable, EvaluationHandoffPort):
    """The route's atomic handoff: receipt and admission in receipts, pending row in outbox."""

    def __init__(  # noqa: PLR0913 - two tables plus injectable client and retention
        self,
        audit_table_name: str,
        outbox_table_name: str,
        client: DynamoDBClient | None = None,
        *,
        retention: RecordRetention,
    ) -> None:
        if not audit_table_name.strip():
            raise ValueError("evaluation handoff audit table name is required.")
        super().__init__(outbox_table_name, client)
        self._audit_table = audit_table_name
        self._retention = retention

    def persist(self, request: EvaluationHandoffRequest) -> EvaluationOutboxWriteResult:
        """Atomically create audit, admission, and pending content-bearing outbox records.

        The receipt and its admission both expire at the receipt's creation time plus the
        declared retention (ADR-081 §5); the outbox row keeps its own delivered-row expiry.
        """
        expires_at = self._retention.expires_at(request.audit.created_at)
        audit_item = audit_record_item(request.audit, expires_at)
        admission_item = evaluation_admission_item(request.admission, expires_at)
        outbox_item = _initial_item(request.outbox)
        transaction: list[TransactWriteItemTypeDef] = [
            {
                "Put": {
                    "TableName": self._audit_table,
                    "Item": audit_item,
                    "ConditionExpression": "attribute_not_exists(item_key)",
                }
            },
            {
                "Put": {
                    "TableName": self._audit_table,
                    "Item": admission_item,
                    "ConditionExpression": "attribute_not_exists(item_key)",
                }
            },
            {
                "Put": {
                    "TableName": self._outbox_table,
                    "Item": outbox_item,
                    "ConditionExpression": f"attribute_not_exists({ADMISSION_REF})",
                }
            },
        ]
        try:
            self._dynamodb.transact_write_items(
                TransactItems=transaction,
                ClientRequestToken=hashlib.sha256(
                    request.admission.admission_ref.encode()
                ).hexdigest()[:32],
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") != "TransactionCanceledException":
                self._write_error(error)
            if self._is_exact_replay(request, (audit_item, admission_item, outbox_item)):
                return EvaluationOutboxWriteResult(
                    disposition=EvaluationOutboxWriteDisposition.EXACT_REPLAY,
                    delivery_ref=request.outbox.envelope.delivery_ref,
                )
            self._conflict("Evaluation handoff transaction conflicts with retained records.")
        except Exception as error:
            self._write_error(error)
        return EvaluationOutboxWriteResult(
            disposition=EvaluationOutboxWriteDisposition.APPLIED,
            delivery_ref=request.outbox.envelope.delivery_ref,
        )

    def _is_exact_replay(
        self,
        request: EvaluationHandoffRequest,
        expected: tuple[
            dict[str, AttributeValueTypeDef],
            dict[str, AttributeValueTypeDef],
            dict[str, AttributeValueTypeDef],
        ],
    ) -> bool:
        """Recognise a replay by the exact receipt and admission items, expiry included."""
        expected_audit, expected_admission, expected_outbox = expected
        audit = DynamoAgentQualityAuditStore(self._audit_table, self._dynamodb)
        if not audit.holds_exactly(expected_audit) or not audit.holds_exactly(expected_admission):
            return False
        outbox_item = self._get_item(request.admission.admission_ref)
        if outbox_item is None:
            return False
        try:
            return _envelope(outbox_item) == request.outbox.envelope and all(
                outbox_item.get(name) == value
                for name, value in expected_outbox.items()
                if name
                in {ADMISSION_REF, RECORD_TYPE, PAYLOAD, PAYLOAD_DIGEST, AUDIT_ID, DELIVERY_REF}
            )
        except TypeError, ValueError:
            return False


class DynamoEvaluationOutboxDispatchStore(_EvaluationOutboxTable, EvaluationOutboxDispatchPort):
    """The dispatcher's leased outbox transitions and its delivery receipts table (ADR-080 §1)."""

    def __init__(  # noqa: PLR0913 - two tables plus injectable client and retention
        self,
        outbox_table_name: str,
        deliveries_table_name: str,
        client: DynamoDBClient | None = None,
        *,
        retention: RecordRetention,
    ) -> None:
        if not deliveries_table_name.strip():
            raise ValueError("evaluation deliveries table name is required.")
        super().__init__(outbox_table_name, client)
        self._deliveries_table = deliveries_table_name
        self._retention = retention

    def due(self, request: EvaluationOutboxDueRequest) -> tuple[str, ...]:
        """Query bounded due references through the KEYS_ONLY dispatch index."""
        try:
            response = self._dynamodb.query(
                TableName=self._outbox_table,
                IndexName=READY_INDEX,
                KeyConditionExpression=(
                    f"{READY_PARTITION} = :partition AND {READY_SORT} <= :ready"
                ),
                ExpressionAttributeValues={
                    ":partition": write_text(DISPATCHABLE),
                    ":ready": write_text(f"{request.ready_at_or_before}#\uffff"),
                },
                ProjectionExpression=ADMISSION_REF,
                Limit=request.limit,
            )
        except Exception as error:
            self._read_error(error)
        return tuple(read_text(dict(item), ADMISSION_REF) for item in response.get("Items", []))

    def claim(self, request: EvaluationOutboxClaimRequest) -> EvaluationOutboxWorkItem | None:
        """Lease one pending or expired item without overwriting a concurrent owner."""
        try:
            response = self._dynamodb.update_item(
                TableName=self._outbox_table,
                Key={ADMISSION_REF: write_text(request.admission_ref)},
                ConditionExpression=(
                    "#state = :pending OR (#state = :leased AND ready_at <= :acquired)"
                ),
                UpdateExpression=(
                    "SET #state = :leased, lease_ref = :lease, lease_acquired_at = :acquired, "
                    "lease_expires_at = :expires, ready_at = :expires, ready_sort = :ready, "
                    "attempt_count = if_not_exists(attempt_count, :zero) + :one"
                ),
                ExpressionAttributeNames={"#state": STATE},
                ExpressionAttributeValues={
                    ":pending": write_text(EvaluationOutboxDeliveryStateV1.PENDING.value),
                    ":leased": write_text(EvaluationOutboxDeliveryStateV1.LEASED.value),
                    ":lease": write_text(request.lease_ref),
                    ":acquired": write_text(request.acquired_at),
                    ":expires": write_text(request.expires_at),
                    ":ready": write_text(f"{request.expires_at}#{request.admission_ref}"),
                    ":zero": write_int(0),
                    ":one": write_int(1),
                },
                ReturnValues="ALL_NEW",
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return None
            self._write_error(error)
        except Exception as error:
            self._write_error(error)
        attributes = response["Attributes"]
        try:
            return _work_item(dict(attributes))
        except TypeError, ValueError:
            self._conflict("Leased evaluation outbox item is malformed.")

    def release(self, request: EvaluationOutboxReleaseRequest) -> None:
        """Return one owned lease to pending with its next availability.

        An uncounted release also takes back the attempt its claim added; the claim guarantees
        the count is at least one, and the lease condition makes the decrement the owner's.
        """
        values: dict[str, AttributeValueTypeDef] = {
            ":leased": write_text(EvaluationOutboxDeliveryStateV1.LEASED.value),
            ":pending": write_text(EvaluationOutboxDeliveryStateV1.PENDING.value),
            ":lease": write_text(request.lease_ref),
            ":ready_at": write_text(request.available_at),
            ":ready_sort": write_text(f"{request.available_at}#{request.admission_ref}"),
        }
        assignments = "SET #state = :pending, ready_at = :ready_at, ready_sort = :ready_sort"
        if not request.count_attempt:
            assignments += ", attempt_count = attempt_count - :one"
            values[":one"] = write_int(1)
        try:
            self._dynamodb.update_item(
                TableName=self._outbox_table,
                Key={ADMISSION_REF: write_text(request.admission_ref)},
                ConditionExpression="#state = :leased AND lease_ref = :lease",
                UpdateExpression=(
                    f"{assignments} REMOVE lease_ref, lease_acquired_at, lease_expires_at"
                ),
                ExpressionAttributeNames={"#state": STATE},
                ExpressionAttributeValues=values,
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return
            self._write_error(error)
        except Exception as error:
            self._write_error(error)

    def deliver(self, request: EvaluationOutboxDeliverRequest) -> None:
        """Atomically finalize one owned lease as delivered with its dispatched receipt.

        The receipt names every family the row was sent to (ADR-079). The route admits a row
        with the receipt's own creation time as `admitted_at`, so the delivery receipt expires
        with its receipt without the dispatcher reading receipts.
        """
        work = self._settlement_target(request.admission_ref)
        self._settle(
            _Settlement(
                work=work,
                lease_ref=request.lease_ref,
                receipt=EvaluationDeliveryReceiptV2(
                    audit_id=work.envelope.audit_id,
                    admission_ref=work.envelope.admission_ref,
                    delivery_ref=work.envelope.delivery_ref,
                    decided_at=request.decided_at,
                    outcome=EvaluationDeliveryOutcomeV1.DISPATCHED,
                    not_dispatched_reason=None,
                    families=request.families,
                ),
                terminal=EvaluationOutboxDeliveryStateV1.DELIVERED,
                time_attribute=DELIVERED_AT,
                expires_at_epoch=request.expires_at_epoch,
            )
        )

    def withhold(self, request: EvaluationOutboxWithholdRequest) -> None:
        """Atomically end one owned lease as not dispatched with its not-dispatched receipt.

        The row leaves the dispatch index and takes the delivered-row expiry, so its content is
        held no longer than a delivered row's and a later re-enable never sends it (ADR-079).
        """
        work = self._settlement_target(request.admission_ref)
        self._settle(
            _Settlement(
                work=work,
                lease_ref=request.lease_ref,
                receipt=EvaluationDeliveryReceiptV2(
                    audit_id=work.envelope.audit_id,
                    admission_ref=work.envelope.admission_ref,
                    delivery_ref=work.envelope.delivery_ref,
                    decided_at=request.decided_at,
                    outcome=EvaluationDeliveryOutcomeV1.NOT_DISPATCHED,
                    not_dispatched_reason=request.reason,
                    families=(),
                ),
                terminal=EvaluationOutboxDeliveryStateV1.NOT_DISPATCHED,
                time_attribute=DECIDED_AT,
                expires_at_epoch=request.expires_at_epoch,
            )
        )

    def _settlement_target(self, admission_ref: str) -> EvaluationOutboxWorkItem:
        work = self._get_work(admission_ref)
        if work is None:
            self._conflict("Evaluation outbox settlement target is absent.")
        return work

    def _settle(self, settlement: _Settlement) -> None:
        """Move one owned lease to its terminal state and write its receipt in one transaction.

        The update asserts the lease this dispatcher holds, so an expired lease cannot settle
        work another dispatcher now owns; the receipt put asserts first-writer-wins.
        """
        work = settlement.work
        decided_at = settlement.receipt.decided_at
        receipt_item = evaluation_delivery_item(
            settlement.receipt, self._retention.expires_at(work.envelope.admitted_at)
        )
        transaction: list[TransactWriteItemTypeDef] = [
            {
                "Update": {
                    "TableName": self._outbox_table,
                    "Key": {ADMISSION_REF: write_text(work.envelope.admission_ref)},
                    "ConditionExpression": "#state = :leased AND lease_ref = :lease",
                    "UpdateExpression": (
                        "SET #state = :terminal, #at = :at, "
                        "expires_at = :expires_at, ready_at = :at "
                        "REMOVE lease_ref, lease_acquired_at, lease_expires_at, "
                        "ready_partition, ready_sort"
                    ),
                    "ExpressionAttributeNames": {
                        "#state": STATE,
                        "#at": settlement.time_attribute,
                    },
                    "ExpressionAttributeValues": {
                        ":leased": write_text(EvaluationOutboxDeliveryStateV1.LEASED.value),
                        ":terminal": write_text(settlement.terminal.value),
                        ":lease": write_text(settlement.lease_ref),
                        ":at": write_text(decided_at),
                        ":expires_at": write_int(settlement.expires_at_epoch),
                    },
                }
            },
            {
                "Put": {
                    "TableName": self._deliveries_table,
                    "Item": receipt_item,
                    "ConditionExpression": "attribute_not_exists(item_key)",
                }
            },
        ]
        try:
            self._dynamodb.transact_write_items(TransactItems=transaction)
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") != "TransactionCanceledException":
                self._write_error(error)
            if self._settled_exact(settlement, receipt_item):
                return
            self._conflict("Evaluation outbox settlement transaction conflicted.")
        except Exception as error:
            self._write_error(error)

    def _settled_exact(
        self,
        settlement: _Settlement,
        receipt_item: dict[str, AttributeValueTypeDef],
    ) -> bool:
        """Recognise a replay: the row already holds this terminal state and this exact receipt."""
        current = self._get_work(settlement.work.envelope.admission_ref)
        deliveries = DynamoEvaluationDeliveryStore(self._deliveries_table, self._dynamodb)
        return (
            current is not None
            and current.state.state is settlement.terminal
            and deliveries.holds_exactly(receipt_item)
        )
