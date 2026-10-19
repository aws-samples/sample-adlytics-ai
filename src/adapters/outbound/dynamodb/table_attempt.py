# SPDX-License-Identifier: MIT-0
"""Table-scoped provider-attempt authority in the publications table (ADR-075 §3.1).

Item shapes (owned here; no other adapter writes these key spaces):
    partition_key = "TABLE_ATTEMPT#<database>.<table>"
        record_key = "FENCE"            — safety form plus reconciliation attributes
        record_key = "ATTEMPT#<id>"     — immutable core plus state-dependent fields;
                                          sanitized_start_evidence is one M attribute
        record_key = "RUN#<run id>"     — inline-run accounting item
    partition_key = "PROVIDER_RECONCILE_BLOCK#<uuid4>", record_key = "CASE"

Every read is strongly consistent and maps to the closed domain forms; a malformed
item is a typed invariant error, never repaired. The sole mutation this slice owns
is ``RecordSubmittedAttemptRequest``: one conditional ``UpdateItem`` per its §3.2
row, with conditional failure classified by a strong reread into exact-replay no-op
or typed invariant collision.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, cast

import boto3
from botocore.exceptions import ClientError

from domain.operations.table_attempt import (
    FENCE_RECORD_KEY,
    PROTECTED_CASE_RECORD_KEY,
    RECONCILIATION_FIELD_NAMES,
    AttemptObservation,
    FenceObservation,
    FenceRecord,
    InlineRun,
    InlineRunState,
    ProtectedCaseState,
    ProtectedReconcileCase,
    ReconcileBlockReason,
    ReconciliationObservation,
    SanitizedStartEvidence,
    TableAttempt,
    TableAttemptError,
    WriterKind,
    attempt_record_key,
    parse_attempt,
    parse_fence,
    parse_sanitized_evidence,
    protected_case_partition_key,
    run_record_key,
    table_attempt_partition_key,
)
from domain.shared_kernel.errors import DependencyError, ErrorCode, InvariantError
from ports.outbound.table_attempt import (
    RecordSubmittedAttemptRequest,
    RecordSubmittedAttemptResult,
    TableAttemptStore,
)

from .items import write_text

if TYPE_CHECKING:
    from collections.abc import Mapping

    from types_boto3_dynamodb.client import DynamoDBClient
    from types_boto3_dynamodb.type_defs import AttributeValueTypeDef

    Item = dict[str, AttributeValueTypeDef]

__all__ = [
    "DynamoTableAttemptStore",
    "attempt_observation_from_item",
    "fence_observation_from_item",
    "inline_run_from_item",
    "protected_case_from_item",
]

_MALFORMED: Final = ErrorCode("TABLE_ATTEMPT", 1)
_COLLISION: Final = ErrorCode("TABLE_ATTEMPT", 2)
_UNREACHABLE: Final = ErrorCode("TABLE_ATTEMPT", 3)
_CONDITIONAL: Final = "ConditionalCheckFailedException"

_KEY_NAMES: Final = frozenset({"partition_key", "record_key"})
_FENCE_TEXT_NAMES: Final = frozenset(
    {"state", "physical_table_id", "attempt_id", "lease_holder", "parked_at"}
)
_ATTEMPT_TEXT_NAMES: Final = frozenset(
    {
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
        "owner_disposition",
        "usage_category",
        "usage_authority_digest",
    }
)
_ATTEMPT_INT_NAMES: Final = frozenset(
    {"ordinal", "fencing_token", "query_count", "scanned_bytes", "engine_ms"}
)
_EVIDENCE_NAME: Final = "sanitized_start_evidence"

_RECORD_CONDITION: Final = (
    "#digest = :digest AND #token = :token AND "
    "((#state = :intent AND attribute_not_exists(#query)) OR "
    "(#state = :submitted_state AND #query = :query AND "
    "#submitted_at = :submitted_at AND #evidence = :evidence))"
)
_RECORD_UPDATE: Final = (
    "SET #state = :submitted_state, #query = :query, "
    "#submitted_at = :submitted_at, #evidence = :evidence"
)


def _decoded(value: AttributeValueTypeDef) -> str | int | bool | None:
    """Decode one raw attribute; ``None`` means outside the S/N/BOOL vocabulary."""
    if "S" in value:
        return value["S"]
    if "N" in value:
        try:
            return int(value["N"])
        except ValueError:
            return None
    if "BOOL" in value:
        return value["BOOL"]
    return None


def _evidence_attribute(evidence: SanitizedStartEvidence) -> AttributeValueTypeDef:
    code: AttributeValueTypeDef = (
        {"NULL": True}
        if evidence.provider_status_code is None
        else {"N": str(evidence.provider_status_code)}
    )
    return {
        "M": {
            "provider_status_code": code,
            "provider_error_class": {"S": evidence.provider_error_class.value},
        }
    }


def _decoded_evidence(value: AttributeValueTypeDef) -> SanitizedStartEvidence:
    members = value.get("M")
    if members is None:
        raise TableAttemptError("sanitized evidence must be one map attribute.")
    fields: dict[str, object] = {}
    for name, member in members.items():
        if member.get("NULL") is True:
            fields[name] = None
            continue
        decoded = _decoded(member)
        if decoded is None:
            raise TableAttemptError(f"sanitized evidence member {name} has a foreign type.")
        fields[name] = decoded
    return parse_sanitized_evidence(fields)


def _text_field(item: Item, name: str, malformed: set[str]) -> str | None:
    value = item.get(name)
    if value is None:
        return None
    text = value.get("S")
    if text is None:
        malformed.add(name)
        return None
    return text


def _int_field(item: Item, name: str, malformed: set[str]) -> int | None:
    value = item.get(name)
    if value is None:
        return None
    number = value.get("N")
    if number is None:
        malformed.add(name)
        return None
    try:
        return int(number)
    except ValueError:
        malformed.add(name)
        return None


def _reconciliation_observation(item: Item) -> ReconciliationObservation:
    malformed: set[str] = set()
    raw: dict[str, str | int | bool | None] = {}
    for name in RECONCILIATION_FIELD_NAMES:
        value = item.get(name)
        if value is None:
            raw[name] = None
            continue
        decoded = _decoded(value)
        if decoded is None:
            malformed.add(name)
            raw[name] = None
        else:
            raw[name] = decoded
    return ReconciliationObservation(
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
        malformed_field_names=frozenset(malformed),
    )


def _fence_observation(item: Item) -> FenceObservation:
    malformed: set[str] = set()
    known = _KEY_NAMES | _FENCE_TEXT_NAMES | {"fencing_token"} | RECONCILIATION_FIELD_NAMES
    unrecognized = frozenset(name for name in item if name not in known)
    text = {name: _text_field(item, name, malformed) for name in _FENCE_TEXT_NAMES}
    return FenceObservation(
        partition_key=_text_field(item, "partition_key", malformed) or "",
        record_key=_text_field(item, "record_key", malformed) or "",
        state=text["state"],
        physical_table_id=text["physical_table_id"],
        attempt_id=text["attempt_id"],
        lease_holder=text["lease_holder"],
        fencing_token=_int_field(item, "fencing_token", malformed),
        parked_at=text["parked_at"],
        reconciliation=_reconciliation_observation(item),
        malformed_field_names=frozenset(malformed),
        unrecognized_field_names=unrecognized,
    )


def _attempt_observation(item: Item) -> AttemptObservation:
    malformed: set[str] = set()
    known = _KEY_NAMES | _ATTEMPT_TEXT_NAMES | _ATTEMPT_INT_NAMES | {_EVIDENCE_NAME}
    unrecognized = frozenset(name for name in item if name not in known)
    text = {name: _text_field(item, name, malformed) for name in _ATTEMPT_TEXT_NAMES}
    numbers = {name: _int_field(item, name, malformed) for name in _ATTEMPT_INT_NAMES}
    evidence: SanitizedStartEvidence | None = None
    raw_evidence = item.get(_EVIDENCE_NAME)
    if raw_evidence is not None:
        try:
            evidence = _decoded_evidence(raw_evidence)
        except TableAttemptError:
            malformed.add(_EVIDENCE_NAME)
    return AttemptObservation(
        partition_key=_text_field(item, "partition_key", malformed) or "",
        record_key=_text_field(item, "record_key", malformed) or "",
        state=text["state"],
        physical_table_id=text["physical_table_id"],
        attempt_id=text["attempt_id"],
        plan_unit_owner_ref=text["plan_unit_owner_ref"],
        inline_run_owner_ref=text["inline_run_owner_ref"],
        ordinal=numbers["ordinal"],
        statement_kind=text["statement_kind"],
        lease_holder=text["lease_holder"],
        fencing_token=numbers["fencing_token"],
        writer_execution_id=text["writer_execution_id"],
        start_envelope=text["start_envelope"],
        client_request_token=text["client_request_token"],
        envelope_digest=text["envelope_digest"],
        reservation_identity=text["reservation_identity"],
        query_execution_id=text["query_execution_id"],
        submitted_at=text["submitted_at"],
        sanitized_start_evidence=evidence,
        cancel_requested_at=text["cancel_requested_at"],
        parked_at=text["parked_at"],
        query_count=numbers["query_count"],
        scanned_bytes=numbers["scanned_bytes"],
        engine_ms=numbers["engine_ms"],
        terminal_at=text["terminal_at"],
        athena_usage_state=text["athena_usage_state"],
        owner_disposition=text["owner_disposition"],
        usage_category=text["usage_category"],
        usage_authority_digest=text["usage_authority_digest"],
        malformed_field_names=frozenset(malformed),
        unrecognized_field_names=unrecognized,
    )


def _required_text(item: Item, name: str) -> str:
    value = item.get(name, {}).get("S")
    if value is None:
        raise TableAttemptError(f"{name} is missing or not a string.")
    return value


def _required_int(item: Item, name: str) -> int:
    value = item.get(name, {}).get("N")
    if value is None:
        raise TableAttemptError(f"{name} is missing or not a number.")
    try:
        return int(value)
    except ValueError as error:
        raise TableAttemptError(f"{name} is not an integer.") from error


def _optional_text(item: Item, name: str) -> str | None:
    if name not in item:
        return None
    return _required_text(item, name)


def _text_list(item: Item, name: str) -> tuple[str, ...]:
    value = item.get(name, {}).get("L")
    if value is None:
        raise TableAttemptError(f"{name} is missing or not a list.")
    members: list[str] = []
    for member in value:
        text = member.get("S")
        if text is None:
            raise TableAttemptError(f"{name} members must be strings.")
        members.append(text)
    return tuple(members)


def _inline_run(item: Item) -> InlineRun:
    return InlineRun(
        run_id=_required_text(item, "run_id"),
        writer_kind=WriterKind(_required_text(item, "writer_kind")),
        operation_id=_required_text(item, "operation_id"),
        execution_id=_required_text(item, "execution_id"),
        physical_table_id=_required_text(item, "physical_table_id"),
        status_flow_binding=_required_text(item, "status_flow_binding"),
        execution_binding=_required_text(item, "execution_binding"),
        table_kind=_required_text(item, "table_kind"),
        statement_intent_kinds=_text_list(item, "statement_intent_kinds"),
        pointer_finalization_payload=_optional_text(item, "pointer_finalization_payload"),
        created_at=_required_text(item, "created_at"),
        state=InlineRunState(_required_text(item, "state")),
        current_attempt_id=_optional_text(item, "current_attempt_id"),
        next_ordinal=_required_int(item, "next_ordinal"),
        accepted_attempt_reservations=_required_int(item, "accepted_attempt_reservations"),
        terminal_outcome=_optional_text(item, "terminal_outcome"),
        terminal_at=_optional_text(item, "terminal_at"),
        attempt_references=_text_list(item, "attempt_references"),
        status_outbox_disposition=_optional_text(item, "status_outbox_disposition"),
        usage_outbox_disposition=_optional_text(item, "usage_outbox_disposition"),
    )


def _protected_case(item: Item, recovery_ref: str) -> ProtectedReconcileCase:
    return ProtectedReconcileCase(
        recovery_ref=recovery_ref,
        state=ProtectedCaseState(_required_text(item, "state")),
        physical_table_id=_required_text(item, "physical_table_id"),
        attempt_id=_required_text(item, "attempt_id"),
        reconcile_block_id=_required_text(item, "reconcile_block_id"),
        binding_digest=_required_text(item, "binding_digest"),
        reason=ReconcileBlockReason(_required_text(item, "reason")),
        blocked_at=_required_text(item, "blocked_at"),
        observation_digest=_optional_text(item, "observation_digest"),
        requeued_at=_optional_text(item, "requeued_at"),
        requeue_id=_optional_text(item, "requeue_id"),
        requeue_operation_id=_optional_text(item, "requeue_operation_id"),
        resolution_reason=_optional_text(item, "resolution_reason"),
    )


# The read-only pre-switch verifier (write discipline §2.5) recomputes every reserved-namespace row
# from an all-page scan; it decodes items through exactly these owner decoders so its notion of
# "the closed form" is this adapter's, never a second reading.
fence_observation_from_item = _fence_observation
attempt_observation_from_item = _attempt_observation
inline_run_from_item = _inline_run
protected_case_from_item = _protected_case


class DynamoTableAttemptStore(TableAttemptStore):
    """Strongly consistent §3.1 authority reads plus the one direct §3.2 mutation."""

    def __init__(self, publications_table: str, client: DynamoDBClient | None = None) -> None:
        if not publications_table.strip():
            raise ValueError("table attempt store requires the publications table name.")
        self._table = publications_table
        self._client = client or boto3.client("dynamodb")

    def _get(self, partition_key: str, record_key: str) -> Item | None:
        try:
            response = self._client.get_item(
                TableName=self._table,
                Key={
                    "partition_key": write_text(partition_key),
                    "record_key": write_text(record_key),
                },
                ConsistentRead=True,
            )
        except Exception as error:
            raise _dependency(error, "The table-attempt authority could not be read.") from error
        item = response.get("Item")
        return None if item is None else dict(item)

    def read_fence(self, table_id: str) -> FenceRecord | None:
        """Strongly read one ``FENCE``; absent is ``None``; malformed is a typed error."""
        item = self._get(table_attempt_partition_key(table_id), FENCE_RECORD_KEY)
        if item is None:
            return None
        observation = _fence_observation(item)
        try:
            fence = parse_fence(observation)
        except TableAttemptError as error:
            raise _malformed(error, table_id, FENCE_RECORD_KEY) from error
        return FenceRecord(fence=fence, reconciliation=observation.reconciliation)

    def read_attempt(self, table_id: str, attempt_id: str) -> TableAttempt | None:
        """Strongly read one ``ATTEMPT#``; absent is ``None``; malformed is a typed error."""
        record = attempt_record_key(attempt_id)
        item = self._get(table_attempt_partition_key(table_id), record)
        if item is None:
            return None
        try:
            return parse_attempt(_attempt_observation(item))
        except TableAttemptError as error:
            raise _malformed(error, table_id, record) from error

    def read_run(self, table_id: str, run_id: str) -> InlineRun | None:
        """Strongly read one ``RUN#``; absent is ``None``; malformed is a typed error."""
        record = run_record_key(run_id)
        item = self._get(table_attempt_partition_key(table_id), record)
        if item is None:
            return None
        try:
            return _inline_run(item)
        except (TableAttemptError, ValueError) as error:
            raise _malformed(error, table_id, record) from error

    def read_protected_case(self, recovery_ref: str) -> ProtectedReconcileCase | None:
        """Strongly read one protected case; absent is ``None``; malformed is a typed error."""
        partition = protected_case_partition_key(recovery_ref)
        item = self._get(partition, PROTECTED_CASE_RECORD_KEY)
        if item is None:
            return None
        try:
            return _protected_case(item, recovery_ref)
        except (TableAttemptError, ValueError) as error:
            raise _malformed(error, recovery_ref, PROTECTED_CASE_RECORD_KEY) from error

    def record_submitted_attempt(
        self, request: RecordSubmittedAttemptRequest
    ) -> RecordSubmittedAttemptResult:
        """Conditionally record ``intent→submitted``; classify conditional failure by reread."""
        partition = table_attempt_partition_key(request.physical_table_id)
        record = attempt_record_key(request.attempt_id)
        evidence = _evidence_attribute(request.sanitized_start_evidence)
        try:
            response = self._client.update_item(
                TableName=self._table,
                Key={"partition_key": write_text(partition), "record_key": write_text(record)},
                UpdateExpression=_RECORD_UPDATE,
                ConditionExpression=_RECORD_CONDITION,
                ExpressionAttributeNames={
                    "#state": "state",
                    "#query": "query_execution_id",
                    "#submitted_at": "submitted_at",
                    "#evidence": _EVIDENCE_NAME,
                    "#digest": "envelope_digest",
                    "#token": "client_request_token",
                },
                ExpressionAttributeValues={
                    ":intent": write_text("intent"),
                    ":submitted_state": write_text("submitted"),
                    ":query": write_text(request.query_execution_id),
                    ":submitted_at": write_text(request.submitted_at),
                    ":evidence": evidence,
                    ":digest": write_text(request.envelope_digest),
                    ":token": write_text(request.client_request_token),
                },
                ReturnValues="ALL_OLD",
            )
        except Exception as error:
            if isinstance(error, ClientError) and _is_conditional(error):
                return self._classify_conditional_failure(request, partition, record)
            raise _dependency(error, "The attempt submission could not be recorded.") from error
        old = cast("Mapping[str, AttributeValueTypeDef]", response.get("Attributes", {}))
        old_state = old.get("state", {}).get("S")
        if old_state == "intent":
            return RecordSubmittedAttemptResult.RECORDED
        return RecordSubmittedAttemptResult.REPLAYED

    def _classify_conditional_failure(
        self,
        request: RecordSubmittedAttemptRequest,
        partition: str,
        record: str,
    ) -> RecordSubmittedAttemptResult:
        item = self._get(partition, record)
        if item is not None and _exact_replay(item, request):
            return RecordSubmittedAttemptResult.REPLAYED
        raise InvariantError(
            _COLLISION,
            "A submitted-attempt replay disagreed with the durable attempt bytes.",
            context={
                "physical_table_id": request.physical_table_id,
                "attempt_id": request.attempt_id,
                "stored_state": None if item is None else item.get("state", {}).get("S"),
            },
        )


def _exact_replay(item: Item, request: RecordSubmittedAttemptRequest) -> bool:
    expected: dict[str, AttributeValueTypeDef] = {
        "query_execution_id": write_text(request.query_execution_id),
        "submitted_at": write_text(request.submitted_at),
        _EVIDENCE_NAME: _evidence_attribute(request.sanitized_start_evidence),
        "envelope_digest": write_text(request.envelope_digest),
        "client_request_token": write_text(request.client_request_token),
    }
    return all(item.get(name) == value for name, value in expected.items())


def _malformed(error: Exception, identity: str, record_key: str) -> InvariantError:
    return InvariantError(
        _MALFORMED,
        "A retained table-attempt authority item is outside its closed form.",
        context={"cause": str(error), "identity": identity, "record_key": record_key},
    )


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
    return str(error.response.get("Error", {}).get("Code", "")) == _CONDITIONAL
