# SPDX-License-Identifier: MIT-0
"""DynamoDB adapters for immutable content-minimized Agent Quality audit data.

Each per-turn record family lives in its own table with exactly one writer (ADR-080 §1). Every
table keeps `audit_id` as its partition key and the item-key shapes below, and each store class
reads and writes only its own table's key space.

Every record item carries `expires_at`, the table's TTL attribute, outside its payload: the
receipt's creation time plus the declared retention (ADR-081 §5). A reader treats an item whose
expiry has passed as absent, because TTL deletion runs some time after expiry. An item written
before the retention existed has no expiry until the one-time backfill gives it one, and reads
as unexpired.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, NoReturn, cast

import boto3
from botocore.exceptions import ClientError

from adapters.inbound.conversation.contract_codec import contract_to_json, decode_contract
from domain.conversation.audit import (
    MANAGED_HARNESS_RUNTIME_V1,
    AuditAggregateMetricV2,
    AuditFindingV3,
    AuditTerminalOutcomeV2,
    ConversationAuditRecordV6,
    RecordRetention,
    TurnDeliveryRecordV1,
    require_utc_instant,
)
from domain.evaluation.linkage import (
    EvaluationAdmissionV1,
    EvaluationDeliveryReceiptV2,
    EvaluationResultV3,
    EvaluationWriterReceiptV1,
)
from domain.shared_kernel.errors import DependencyError, ErrorCode, InvariantError
from ports.outbound.audit import (
    AgentQualityAggregatePort,
    AgentQualityAuditStorePort,
    AuditCorrelationRequest,
    AuditFindingStorePort,
    AuditWriteDisposition,
    AuditWriteResult,
    EvaluationDeliveryPredatesGate,
    EvaluationDeliveryStorePort,
    EvaluationResultLinkRequest,
    EvaluationResultLinkResolution,
    EvaluationResultLinkResolutionState,
    EvaluationResultLinkResolverPort,
    EvaluationResultLinkTarget,
    EvaluationVerdictStorePort,
    EvaluationWriterReceiptStorePort,
    ReceiptTurnRow,
    ReceiptTurnsPage,
    ReceiptTurnsPageRequest,
    StoredRecord,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from types_boto3_dynamodb.client import DynamoDBClient
    from types_boto3_dynamodb.type_defs import AttributeValueTypeDef

AUDIT_ID = "audit_id"
ITEM_KEY = "item_key"
# Receipts table (DynamoAgentQualityAuditStore): written only by the conversation route.
RECORD = "RECORD"
DELIVERY = "DELIVERY"
EVALUATION_ADMISSION_PREFIX = "EVALUATION_ADMISSION#"
# Findings table (DynamoAuditFindingStore): written only by the agent-quality processor.
FINDING_PREFIX = "FINDING#"
# Evaluation deliveries table (DynamoEvaluationDeliveryStore): only the outbox dispatcher.
EVALUATION_DELIVERY_PREFIX = "EVALUATION_DELIVERY#"
# Writer receipts table (DynamoEvaluationWriterReceiptStore): only the evaluation writer.
EVALUATION_WRITER_PREFIX = "EVALUATION_WRITER#"
# Verdicts table (DynamoEvaluationVerdictStore): only the result linker.
EVALUATION_RESULT_PREFIX = "EVALUATION_RESULT#"
PAYLOAD = "payload"
PAYLOAD_DIGEST = "payload_digest"
CORRELATION_ID = "correlation_id"
CORRELATION_AUDIT_ID = "correlation_audit_id"
CORRELATION_INDEX = "correlation-audit"
# The sparse `turns-by-day` index (ADR-078 §5): only receipts written with these top-level
# attributes enter it. It projects the correlation, terminal outcome, and expiry, never the
# principal.
TURNS_INDEX = "turns-by-day"
TURNS_DAY = "turns_day"
TURNS_CREATED_AT = "turns_created_at"
TERMINAL_OUTCOME = "terminal_outcome"
METRIC_ID = "metric_id"
EXPIRES_AT = "expires_at"
"""The TTL attribute on every record table: whole epoch seconds, outside the payload."""
AUDIT_UNREADABLE = ErrorCode("AGENT_QUALITY", 1)
AUDIT_UNWRITABLE = ErrorCode("AGENT_QUALITY", 2)
AUDIT_CONFLICT = ErrorCode("AGENT_QUALITY", 3)
_EPOCH_SECONDS = re.compile(r"\A[1-9][0-9]{0,11}\Z")
_FINDINGS_BOUND = 20
_CORRELATION_BOUND = 2


def _text(value: str) -> AttributeValueTypeDef:
    return {"S": value}


def _with_expiry(
    item: dict[str, AttributeValueTypeDef], expires_at: int | None
) -> dict[str, AttributeValueTypeDef]:
    return item if expires_at is None else {**item, EXPIRES_AT: {"N": str(expires_at)}}


def _split_expiry(
    item: dict[str, AttributeValueTypeDef],
) -> tuple[dict[str, AttributeValueTypeDef], int | None]:
    """Separate the TTL attribute from the attested envelope; a malformed one is refused.

    Absent means an item written before retention existed, which is never expired. Present, it
    must be exactly one canonical positive whole number of epoch seconds.
    """
    if EXPIRES_AT not in item:
        return item, None
    value = item[EXPIRES_AT]
    number = value.get("N")
    if set(value) != {"N"} or not isinstance(number, str) or not _EPOCH_SECONDS.match(number):
        raise ValueError("stored agent quality item expiry is malformed.")
    return {name: part for name, part in item.items() if name != EXPIRES_AT}, int(number)


def _read_text(item: dict[str, AttributeValueTypeDef], name: str) -> str:
    value = item.get(name, {}).get("S")
    if not isinstance(value, str):
        raise ValueError(f"stored agent quality item has no string {name!r}.")
    return value


def _digest(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _key(audit_id: str, item_key: str) -> dict[str, AttributeValueTypeDef]:
    return {AUDIT_ID: _text(audit_id), ITEM_KEY: _text(item_key)}


@dataclass(frozen=True, slots=True, kw_only=True)
class _LinkageItemRequest:
    audit_id: str
    item_key: str
    record_type: str
    value: object
    expires_at: int | None = None


def _turns_attributes(record: ConversationAuditRecordV6) -> dict[str, AttributeValueTypeDef]:
    """Derive the three turns-index attributes from the receipt payload alone."""
    created = require_utc_instant(record.created_at, "audit creation time")
    return {
        TURNS_DAY: _text(created.astimezone(UTC).date().isoformat()),
        TURNS_CREATED_AT: _text(record.created_at),
        TERMINAL_OUTCOME: _text(record.terminal_outcome.value),
    }


def _record_item(
    record: ConversationAuditRecordV6, expires_at: int | None = None
) -> dict[str, AttributeValueTypeDef]:
    payload = contract_to_json(record)
    return _with_expiry(
        {
            AUDIT_ID: _text(record.audit_id),
            ITEM_KEY: _text(RECORD),
            "record_type": _text(RECORD),
            PAYLOAD: _text(payload),
            PAYLOAD_DIGEST: _text(_digest(payload)),
            CORRELATION_ID: _text(record.correlation_id),
            CORRELATION_AUDIT_ID: _text(record.audit_id),
            **_turns_attributes(record),
        },
        expires_at,
    )


def _finding_item(
    finding: AuditFindingV3, expires_at: int | None = None
) -> dict[str, AttributeValueTypeDef]:
    payload = contract_to_json(finding)
    return _with_expiry(
        {
            AUDIT_ID: _text(finding.audit_id),
            ITEM_KEY: _text(f"{FINDING_PREFIX}{finding.finding_id}"),
            "record_type": _text("FINDING"),
            PAYLOAD: _text(payload),
            PAYLOAD_DIGEST: _text(_digest(payload)),
        },
        expires_at,
    )


def _linkage_item(request: _LinkageItemRequest) -> dict[str, AttributeValueTypeDef]:
    payload = contract_to_json(request.value)
    return _with_expiry(
        {
            AUDIT_ID: _text(request.audit_id),
            ITEM_KEY: _text(request.item_key),
            "record_type": _text(request.record_type),
            PAYLOAD: _text(payload),
            PAYLOAD_DIGEST: _text(_digest(payload)),
        },
        request.expires_at,
    )


def audit_record_item(
    record: ConversationAuditRecordV6, expires_at: int
) -> dict[str, AttributeValueTypeDef]:
    """Build the immutable audit item for a cross-table transactional handoff."""
    return _record_item(record, expires_at)


def _delivery_item(
    record: TurnDeliveryRecordV1, expires_at: int | None = None
) -> dict[str, AttributeValueTypeDef]:
    payload = contract_to_json(record)
    return _with_expiry(
        {
            AUDIT_ID: _text(record.audit_id),
            ITEM_KEY: _text(DELIVERY),
            "record_type": _text(DELIVERY),
            PAYLOAD: _text(payload),
            PAYLOAD_DIGEST: _text(_digest(payload)),
        },
        expires_at,
    )


def _admission_item(
    admission: EvaluationAdmissionV1, expires_at: int | None
) -> dict[str, AttributeValueTypeDef]:
    return _linkage_item(
        _LinkageItemRequest(
            audit_id=admission.audit_id,
            item_key=f"{EVALUATION_ADMISSION_PREFIX}{admission.admission_ref}",
            record_type="EVALUATION_ADMISSION",
            value=admission,
            expires_at=expires_at,
        )
    )


def evaluation_admission_item(
    admission: EvaluationAdmissionV1, expires_at: int
) -> dict[str, AttributeValueTypeDef]:
    """Build the immutable admission item for a cross-table transactional handoff."""
    return _admission_item(admission, expires_at)


def evaluation_delivery_item(
    delivery: EvaluationDeliveryReceiptV2, expires_at: int
) -> dict[str, AttributeValueTypeDef]:
    """Build the immutable V2 delivery item for the dispatcher's outbox transaction."""
    return _linkage_item(
        _LinkageItemRequest(
            audit_id=delivery.audit_id,
            item_key=f"{EVALUATION_DELIVERY_PREFIX}{delivery.admission_ref}",
            record_type="EVALUATION_DELIVERY",
            value=delivery,
            expires_at=expires_at,
        )
    )


def _metric_item(metric: AuditAggregateMetricV2) -> dict[str, AttributeValueTypeDef]:
    payload = contract_to_json(metric)
    return {
        METRIC_ID: _text(metric.metric_id),
        "record_type": _text("METRIC"),
        PAYLOAD: _text(payload),
        PAYLOAD_DIGEST: _text(_digest(payload)),
    }


_RECORD_V2 = "ConversationAuditRecord.v2"
_RECORD_V3 = "ConversationAuditRecord.v3"
_RECORD_V4 = "ConversationAuditRecord.v4"
_RECORD_V5 = "ConversationAuditRecord.v5"
_RECORD_V6 = "ConversationAuditRecord.v6"
_V6_FIELDS = ("page_context_reference", "context_status")
_FINDING_V3 = "AuditFinding.v3"
_EMPTY_PLANNER_SLOTS: dict[str, object] = {
    "terminal_summary": {
        "proposed_objective_categories": [],
        "admitted_objective_categories": [],
        "authority_source": "none",
        "admitted_objective_count": 0,
    },
    "objective_summaries": [],
    "turn_evidence_ref": None,
    "prompt_receipt_ref": None,
    "epistemic_record_refs": [],
    "initial_plan_refusal_code": None,
    "failure_stage": None,
}
_V2_ADDED_DEFAULTS: dict[str, object] = {
    "tool_trajectory": [],
    "tool_custody_digest": None,
    "presentation_plan_digest": None,
    "public_block_kinds": [],
    "public_block_count": 0,
    "guardrail_version": None,
}


def _canonical(document: object) -> str:
    return json.dumps(
        document, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False
    )


def _object_payload(payload: str, label: str) -> dict[str, object]:
    raw: object = json.loads(payload)
    if not isinstance(raw, dict):
        raise ValueError(f"stored agent quality {label} payload must be an object.")
    return cast("dict[str, object]", raw)


def _project_v2_record(document: dict[str, object]) -> dict[str, object]:
    """Read one managed-Harness V2 receipt as V3; planner-era shapes are retired, not read.

    The V1 runtime recorded neither step anchors nor a trace, so its trajectory projects with
    both absent and the V3 contract holds that generation to exactly that shape.
    """
    filled = {**_V2_ADDED_DEFAULTS, "initial_plan_refusal_code": None, **document}
    for name, empty in _EMPTY_PLANNER_SLOTS.items():
        if filled.pop(name, empty) != empty:
            raise ValueError("stored agent quality RECORD uses the retired planner-era shape.")
    if filled.get("runtime_version") != MANAGED_HARNESS_RUNTIME_V1:
        raise ValueError("stored agent quality RECORD names a retired runtime generation.")
    steps = filled.pop("tool_trajectory")
    if not isinstance(steps, list):
        raise ValueError("stored agent quality RECORD trajectory is malformed.")
    projected: list[dict[str, object]] = []
    for raw in cast("list[object]", steps):
        if not isinstance(raw, dict):
            raise ValueError("stored agent quality RECORD trajectory step is malformed.")
        step = cast("dict[str, object]", raw)
        projected.append(
            {
                "ordinal": step.get("ordinal"),
                "tool_name": step.get("tool_name"),
                "disposition": step.get("disposition"),
                "step_ref": None,
                "violations": [],
                "selected_into_plan": None,
            }
        )
    return {**filled, "schema_version": _RECORD_V3, "trace_id": None, "trajectory": projected}


def _project_v3_record(document: dict[str, object]) -> dict[str, object]:
    """Read one stored V3 receipt as V4: those runtimes recorded no principal (ADR-077 §6).

    A V3 document that already names a principal is not a V3 receipt this repository wrote.
    """
    if "principal" in document:
        raise ValueError("stored agent quality RECORD V3 cannot carry a principal.")
    return {**document, "schema_version": _RECORD_V4, "principal": None}


def _project_v4_record(document: dict[str, object]) -> dict[str, object]:
    """Read one stored V4 receipt as V5: those turns published blocks and streamed no text."""
    if "released_character_count" in document:
        raise ValueError("stored agent quality RECORD V4 cannot carry a released text count.")
    return {**document, "schema_version": _RECORD_V5, "released_character_count": 0}


def _project_v5_record(document: dict[str, object]) -> dict[str, object]:
    """Read one stored V5 receipt as V6: those turns were sent no page reference (ADR-120)."""
    if any(name in document for name in _V6_FIELDS):
        raise ValueError("stored agent quality RECORD V5 cannot carry page context.")
    return {
        **document,
        "schema_version": _RECORD_V6,
        **dict.fromkeys(_V6_FIELDS),
    }


def _reencoded_as_written(record: ConversationAuditRecordV6, version: str) -> str:
    """Re-encode a projected receipt exactly as its own version's writer did.

    A stored payload missing a field it always wrote then fails exactly as it did before the
    later versions existed.
    """
    reencoded = _object_payload(contract_to_json(record), "RECORD")
    for name in _V6_FIELDS:
        del reencoded[name]
    if version in {_RECORD_V3, _RECORD_V4}:
        del reencoded["released_character_count"]
    if version == _RECORD_V3:
        del reencoded["principal"]
    return _canonical({**reencoded, "schema_version": version})


def _decode_record_payload(payload: str) -> ConversationAuditRecordV6:
    """Decode a V6 receipt, or project a stored V5, V4, V3, or managed-Harness V2 one into V6.

    Either way the stored bytes must be the canonical encoding of what was read, so a payload
    that was edited in place fails here before its digest is even compared.
    """
    document = _object_payload(payload, "RECORD")
    version = document.get("schema_version")
    if version == _RECORD_V2:
        source = _project_v5_record(
            _project_v4_record(_project_v3_record(_project_v2_record(document)))
        )
    elif version == _RECORD_V3:
        source = _project_v5_record(_project_v4_record(_project_v3_record(document)))
    elif version == _RECORD_V4:
        source = _project_v5_record(_project_v4_record(document))
    elif version == _RECORD_V5:
        source = _project_v5_record(document)
    elif version == _RECORD_V6:
        source = document
    else:
        raise ValueError("stored agent quality RECORD schema version is unsupported.")
    record = decode_contract(_canonical(source), ConversationAuditRecordV6)
    if version == _RECORD_V6:
        canonical = contract_to_json(record)
    elif version in {_RECORD_V3, _RECORD_V4, _RECORD_V5}:
        canonical = _reencoded_as_written(record, cast("str", version))
    else:
        canonical = _canonical(document)
    if canonical != payload:
        raise ValueError("stored agent quality RECORD payload is not canonical.")
    return record


def _decode_record(
    item: dict[str, AttributeValueTypeDef], audit_id: str
) -> ConversationAuditRecordV6:
    """Attest one receipt envelope exactly, including its turns-index attributes when present.

    A receipt written before the turns index existed carries none of the three attributes and
    stays readable until it expires; one that carries any of them must carry all three, each
    equal to the value derived from its payload, so an edited attribute cannot misfile a turn.
    """
    expected = {
        AUDIT_ID,
        ITEM_KEY,
        "record_type",
        PAYLOAD,
        PAYLOAD_DIGEST,
        CORRELATION_ID,
        CORRELATION_AUDIT_ID,
    }
    item, _ = _split_expiry(item)
    payload = _read_text(item, PAYLOAD)
    record = _decode_record_payload(payload)
    turns = _turns_attributes(record)
    if set(item) & set(turns):
        if {name: item.get(name) for name in turns} != turns:
            raise ValueError("stored agent quality RECORD turns attributes are not exact.")
        expected |= set(turns)
    if (
        set(item) != expected
        or _read_text(item, AUDIT_ID) != audit_id
        or _read_text(item, ITEM_KEY) != RECORD
        or _read_text(item, "record_type") != RECORD
        or _read_text(item, PAYLOAD_DIGEST) != _digest(payload)
        or record.audit_id != audit_id
        or _read_text(item, CORRELATION_ID) != record.correlation_id
        or _read_text(item, CORRELATION_AUDIT_ID) != record.audit_id
    ):
        raise ValueError("stored agent quality RECORD envelope is not exact.")
    return record


def _decode_finding_payload(payload: str) -> AuditFindingV3:
    """Decode a V3 finding: the findings table was created after V3, so it holds no other.

    Legacy V2 findings stayed in the receipts table, which is never read for findings
    (ADR-080 §5), so no V2 decoder exists.
    """
    document = _object_payload(payload, "FINDING")
    if document.get("schema_version") != _FINDING_V3:
        raise ValueError("stored agent quality FINDING schema version is unsupported.")
    finding = decode_contract(payload, AuditFindingV3)
    if contract_to_json(finding) != payload:
        raise ValueError("stored agent quality FINDING payload is not canonical.")
    return finding


def _decode_finding(item: dict[str, AttributeValueTypeDef], finding: AuditFindingV3) -> None:
    expected = {AUDIT_ID, ITEM_KEY, "record_type", PAYLOAD, PAYLOAD_DIGEST}
    item, _ = _split_expiry(item)
    payload = _read_text(item, PAYLOAD)
    if (
        set(item) != expected
        or _read_text(item, AUDIT_ID) != finding.audit_id
        or _read_text(item, ITEM_KEY) != f"{FINDING_PREFIX}{finding.finding_id}"
        or _read_text(item, "record_type") != "FINDING"
        or _read_text(item, PAYLOAD_DIGEST) != _digest(payload)
        or _decode_finding_payload(payload) != finding
    ):
        raise ValueError("stored agent quality FINDING envelope is not exact.")


def _decode_linkage(item: dict[str, AttributeValueTypeDef], request: _LinkageItemRequest) -> None:
    """Attest everything but the expiry, which a reader cannot recompute without retention."""
    envelope, _ = _split_expiry(item)
    if envelope != _linkage_item(request):
        raise ValueError(f"stored agent quality {request.record_type} envelope is not exact.")


def _decode_admission(
    item: dict[str, AttributeValueTypeDef], audit_id: str, admission_ref: str
) -> EvaluationAdmissionV1:
    payload = _read_text(item, PAYLOAD)
    value = decode_contract(payload, EvaluationAdmissionV1)
    _decode_linkage(
        item,
        _LinkageItemRequest(
            audit_id=audit_id,
            item_key=f"{EVALUATION_ADMISSION_PREFIX}{admission_ref}",
            record_type="EVALUATION_ADMISSION",
            value=value,
        ),
    )
    if value.audit_id != audit_id or value.admission_ref != admission_ref:
        raise ValueError("stored evaluation admission does not match its exact key.")
    return value


_DELIVERY_V1 = "EvaluationDeliveryReceipt.v1"
_DELIVERY_V2 = "EvaluationDeliveryReceipt.v2"


def _classify_legacy_delivery(
    item: dict[str, AttributeValueTypeDef], audit_id: str, admission_ref: str
) -> EvaluationDeliveryPredatesGate:
    """Attest a V1 receipt's envelope and classify it; its payload is never decoded (ADR-079).

    A V1 receipt records a delivery made before the gate existed, so it names no family or
    evaluator and nothing downstream may treat it as a V2 decision.
    """
    envelope, _ = _split_expiry(item)
    payload = _read_text(envelope, PAYLOAD)
    document = _object_payload(payload, "EVALUATION_DELIVERY")
    if (
        set(envelope) != {AUDIT_ID, ITEM_KEY, "record_type", PAYLOAD, PAYLOAD_DIGEST}
        or _read_text(envelope, AUDIT_ID) != audit_id
        or _read_text(envelope, ITEM_KEY) != f"{EVALUATION_DELIVERY_PREFIX}{admission_ref}"
        or _read_text(envelope, "record_type") != "EVALUATION_DELIVERY"
        or _read_text(envelope, PAYLOAD_DIGEST) != _digest(payload)
        or document.get("audit_id") != audit_id
        or document.get("admission_ref") != admission_ref
    ):
        raise ValueError("stored legacy evaluation delivery does not match its exact key.")
    return EvaluationDeliveryPredatesGate(audit_id=audit_id, admission_ref=admission_ref)


def _decode_delivery(
    item: dict[str, AttributeValueTypeDef], audit_id: str, admission_ref: str
) -> EvaluationDeliveryReceiptV2 | EvaluationDeliveryPredatesGate:
    """Decode a V2 receipt strictly, classify a V1 receipt, and refuse any other version."""
    payload = _read_text(item, PAYLOAD)
    version = _object_payload(payload, "EVALUATION_DELIVERY").get("schema_version")
    if version == _DELIVERY_V1:
        return _classify_legacy_delivery(item, audit_id, admission_ref)
    if version != _DELIVERY_V2:
        raise ValueError("stored evaluation delivery schema version is unsupported.")
    value = decode_contract(payload, EvaluationDeliveryReceiptV2)
    _decode_linkage(
        item,
        _LinkageItemRequest(
            audit_id=audit_id,
            item_key=f"{EVALUATION_DELIVERY_PREFIX}{admission_ref}",
            record_type="EVALUATION_DELIVERY",
            value=value,
        ),
    )
    if value.audit_id != audit_id or value.admission_ref != admission_ref:
        raise ValueError("stored evaluation delivery does not match its exact key.")
    return value


def _decode_writer(
    item: dict[str, AttributeValueTypeDef], audit_id: str, admission_ref: str
) -> EvaluationWriterReceiptV1:
    payload = _read_text(item, PAYLOAD)
    value = decode_contract(payload, EvaluationWriterReceiptV1)
    _decode_linkage(
        item,
        _LinkageItemRequest(
            audit_id=audit_id,
            item_key=f"{EVALUATION_WRITER_PREFIX}{admission_ref}",
            record_type="EVALUATION_WRITER",
            value=value,
        ),
    )
    if value.audit_id != audit_id or value.admission_ref != admission_ref:
        raise ValueError("stored evaluation writer receipt does not match its exact key.")
    return value


_RESULT_V2 = "EvaluationResult.v2"
_RESULT_V3 = "EvaluationResult.v3"
_MAX_RESULTS_PER_ADMISSION = 8


def _result_item_key(admission_ref: str, evaluator_ref: str) -> str:
    return f"{EVALUATION_RESULT_PREFIX}{admission_ref}#{evaluator_ref}"


def _decode_result(
    item: dict[str, AttributeValueTypeDef], audit_id: str, admission_ref: str
) -> EvaluationResultV3 | None:
    """Decode one per-evaluator V3 result under its per-evaluator key; `None` skips a V2 one.

    ADR-079: verdicts linked before this change stay where they are and are not read, because
    they carry no provenance. A V2 item is therefore skipped by its schema version and never
    decoded. The verdicts table was created after V2, so it holds no V1 result and no
    admission-only key; those stayed in the receipts table, which is never read for verdicts
    (ADR-080 §5).
    """
    item, _ = _split_expiry(item)
    payload = _read_text(item, PAYLOAD)
    document = _object_payload(payload, "EVALUATION_RESULT")
    version = document.get("schema_version")
    if version == _RESULT_V2:
        return None
    if version != _RESULT_V3:
        raise ValueError("stored evaluation result schema version is unsupported.")
    value = decode_contract(payload, EvaluationResultV3)
    if contract_to_json(value) != payload:
        raise ValueError("stored evaluation result payload is not canonical.")
    expected = {AUDIT_ID, ITEM_KEY, "record_type", PAYLOAD, PAYLOAD_DIGEST}
    if (
        set(item) != expected
        or _read_text(item, AUDIT_ID) != audit_id
        or _read_text(item, ITEM_KEY) != _result_item_key(admission_ref, value.evaluator_ref)
        or _read_text(item, "record_type") != "EVALUATION_RESULT"
        or _read_text(item, PAYLOAD_DIGEST) != _digest(payload)
        or value.audit_id != audit_id
        or value.admission_ref != admission_ref
    ):
        raise ValueError("stored evaluation result does not match its exact key.")
    return value


def _decode_metric(item: dict[str, AttributeValueTypeDef], metric: AuditAggregateMetricV2) -> None:
    expected = {METRIC_ID, "record_type", PAYLOAD, PAYLOAD_DIGEST}
    payload = _read_text(item, PAYLOAD)
    decoded = decode_contract(payload, AuditAggregateMetricV2)
    if (
        set(item) != expected
        or _read_text(item, METRIC_ID) != metric.metric_id
        or _read_text(item, "record_type") != "METRIC"
        or _read_text(item, PAYLOAD_DIGEST) != _digest(payload)
        or decoded != metric
        or contract_to_json(decoded) != payload
    ):
        raise ValueError("stored agent quality METRIC envelope is not exact.")


def _within_bound(
    items: list[dict[str, AttributeValueTypeDef]],
    response: object,
    bound: int,
) -> list[dict[str, AttributeValueTypeDef]] | None:
    """Return a complete bounded read, or `None` when it holds more than `bound` items.

    The query asked for `bound + 1` items, so reaching the bound is not exceeding it: a
    partition with exactly `bound` items returns them all, and only a `bound + 1`th item proves
    the contract broken. A continuation key alongside `bound` or fewer items means DynamoDB cut
    the page by size before the limit, so the rest is unread and the read is not complete.
    """
    if len(items) > bound:
        return None
    if cast("dict[str, object]", response).get("LastEvaluatedKey") is not None:
        return None
    return items


_TURNS_ROW_ATTRIBUTES = frozenset(
    {AUDIT_ID, ITEM_KEY, TURNS_DAY, TURNS_CREATED_AT, CORRELATION_ID, TERMINAL_OUTCOME}
)
"""What the turns index returns for one receipt beside its expiry: the table and index keys
and the projected correlation and terminal outcome. Never the payload or the principal."""
_TURNS_KEY_ATTRIBUTES = frozenset({AUDIT_ID, ITEM_KEY, TURNS_DAY, TURNS_CREATED_AT})
"""The exact shape of a turns-index continuation key: table key plus index key."""
_TURNS_CURSOR_VERSION = 1
_MAX_TURNS_CURSOR_LENGTH = 2048
_SORT_KEY_SECOND = "%Y-%m-%dT%H:%M:%S"
_SORT_KEY_CEILING = "~"
"""Sorts after every character a stored instant can carry after its seconds (`.`, `+`, `-`,
`Z`), so a second-precision prefix plus this bounds every stored form of that second."""


def _window_days(request: ReceiptTurnsPageRequest) -> tuple[date, ...]:
    """Every UTC creation day the half-open window touches, oldest first."""
    first = request.window_from.astimezone(UTC).date()
    last = (request.window_to.astimezone(UTC) - timedelta(microseconds=1)).date()
    return tuple(first + timedelta(days=offset) for offset in range((last - first).days + 1))


def _sort_key_range(request: ReceiptTurnsPageRequest, day: date) -> tuple[str, str]:
    """Return a key range containing every receipt of `day` inside the window, possibly more.

    The stored sort key is the receipt's creation instant as written, so the range is widened to
    whole seconds and the exact half-open window is applied to each row after the read.
    """
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    low = max(request.window_from.astimezone(UTC), start)
    high = min(request.window_to.astimezone(UTC), start + timedelta(days=1))
    return low.strftime(_SORT_KEY_SECOND), high.strftime(_SORT_KEY_SECOND) + _SORT_KEY_CEILING


def _in_window(row: ReceiptTurnRow, request: ReceiptTurnsPageRequest) -> bool:
    created = require_utc_instant(row.created_at, "audit creation time")
    return request.window_from <= created < request.window_to


def _decode_turn_row(item: dict[str, AttributeValueTypeDef], day: date) -> ReceiptTurnRow:
    """Attest one index row's exact shape and its placement under the day that was queried."""
    envelope, expires_at = _split_expiry(item)
    if frozenset(envelope) != _TURNS_ROW_ATTRIBUTES or expires_at is None:
        raise ValueError("turns index row shape is not exact.")
    created_at = _read_text(envelope, TURNS_CREATED_AT)
    created = require_utc_instant(created_at, "audit creation time")
    correlation_id = _read_text(envelope, CORRELATION_ID)
    AuditCorrelationRequest(correlation_id=correlation_id)
    audit_id = _read_text(envelope, AUDIT_ID)
    if (
        not audit_id.strip()
        or _read_text(envelope, ITEM_KEY) != RECORD
        or _read_text(envelope, TURNS_DAY) != day.isoformat()
        or created.astimezone(UTC).date() != day
    ):
        raise ValueError("turns index row is not a receipt of the queried day.")
    return ReceiptTurnRow(
        audit_id=audit_id,
        correlation_id=correlation_id,
        created_at=created_at,
        terminal_outcome=AuditTerminalOutcomeV2(_read_text(envelope, TERMINAL_OUTCOME)),
        expires_at=expires_at,
    )


def _turns_start_key(raw: dict[str, AttributeValueTypeDef], day: date) -> dict[str, str]:
    """Reduce a continuation key to plain strings, refusing any shape but the index key's."""
    if frozenset(raw) != _TURNS_KEY_ATTRIBUTES:
        raise ValueError("turns continuation key shape is not exact.")
    return _checked_start_key({name: _read_text(raw, name) for name in raw}, day)


def _checked_start_key(key: dict[str, object], day: date) -> dict[str, str]:
    strings = {name: value for name, value in key.items() if isinstance(value, str) and value}
    if (
        frozenset(key) != _TURNS_KEY_ATTRIBUTES
        or len(strings) != len(key)
        or strings[ITEM_KEY] != RECORD
        or strings[TURNS_DAY] != day.isoformat()
    ):
        raise ValueError("turns continuation key is not a key of the queried day.")
    return strings


def _turns_query_digest(request: ReceiptTurnsPageRequest) -> str:
    """Bind a cursor to the window and filter set that produced it; the page limit may vary."""
    return _digest(
        _canonical(
            {
                "from": request.window_from.astimezone(UTC).isoformat(),
                "to": request.window_to.astimezone(UTC).isoformat(),
                "terminal_outcome": sorted(value.value for value in request.terminal_outcomes),
            }
        )
    )


@dataclass(frozen=True, slots=True)
class _TurnsCursor:
    """Where the next turns page starts: one creation day and, within it, a continuation key.

    Encoded as unpadded base64url of the canonical JSON object
    `{"d": "YYYY-MM-DD", "k": <key or null>, "q": <window and filter digest>, "v": 1}`.
    """

    day: date
    start_key: dict[str, str] | None

    @staticmethod
    def start(_request: ReceiptTurnsPageRequest, days: tuple[date, ...]) -> _TurnsCursor:
        return _TurnsCursor(day=days[-1], start_key=None)

    @staticmethod
    def decode(request: ReceiptTurnsPageRequest, days: tuple[date, ...]) -> _TurnsCursor:
        """Decode a cursor strictly; one from another window or filter set is refused."""
        raw = request.cursor or ""
        try:
            digest, cursor = _TurnsCursor._parse(raw, days)
        except (ValueError, TypeError, KeyError) as error:
            raise ValueError("turns cursor is malformed.") from error
        if digest != _turns_query_digest(request):
            raise ValueError("turns cursor belongs to another window or filter set.")
        if cursor.encode(request) != raw:
            raise ValueError("turns cursor is not canonical.")
        if cursor.start_key is not None:
            # DynamoDB refuses a start key outside the key condition as a service error, which
            # would read as a dependency failure; a key the range excludes is a client error.
            low, high = _sort_key_range(request, cursor.day)
            if not low <= cursor.start_key[TURNS_CREATED_AT] <= high:
                raise ValueError("turns cursor key is outside its day's window range.")
        return cursor

    @staticmethod
    def _parse(raw: str, days: tuple[date, ...]) -> tuple[object, _TurnsCursor]:
        if len(raw) > _MAX_TURNS_CURSOR_LENGTH:
            raise ValueError("turns cursor is too long.")
        decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8")
        document = _object_payload(decoded, "turns cursor")
        if set(document) != {"d", "k", "q", "v"} or document["v"] != _TURNS_CURSOR_VERSION:
            raise ValueError("turns cursor shape is not exact.")
        day_text = document["d"]
        if not isinstance(day_text, str):
            raise ValueError("turns cursor day is malformed.")
        day = date.fromisoformat(day_text)
        if day not in days or day.isoformat() != day_text:
            raise ValueError("turns cursor day is outside the window.")
        key_value = document["k"]
        if key_value is not None and not isinstance(key_value, dict):
            raise ValueError("turns cursor key is malformed.")
        start_key = (
            None
            if key_value is None
            else _checked_start_key(cast("dict[str, object]", key_value), day)
        )
        return document["q"], _TurnsCursor(day=day, start_key=start_key)

    def encode(self, request: ReceiptTurnsPageRequest) -> str:
        document = {
            "d": self.day.isoformat(),
            "k": self.start_key,
            "q": _turns_query_digest(request),
            "v": _TURNS_CURSOR_VERSION,
        }
        return base64.urlsafe_b64encode(_canonical(document).encode("utf-8")).decode().rstrip("=")

    def next(
        self,
        request: ReceiptTurnsPageRequest,
        days: tuple[date, ...],
        last: dict[str, str] | None,
    ) -> str | None:
        """Continue this day while it has more, then the previous day, then stop."""
        if last is not None:
            return _TurnsCursor(day=self.day, start_key=last).encode(request)
        if self.day == days[0]:
            return None
        return _TurnsCursor(day=self.day - timedelta(days=1), start_key=None).encode(request)


class _Expired:
    """A receipt that exists but whose expiry has passed; absent to readers, not missing."""


_EXPIRED = _Expired()


class _ImmutableRecordTable:
    """One retained per-turn record table with exact strong reads and first-writer-wins creates.

    Each subclass owns exactly one table and only the key prefixes declared for it above
    (ADR-080 §1). Its writer holds `GetItem` on that same table, so a replayed conditional write
    reads the stored item back and is classified as an exact replay (ADR-080 §3).
    """

    _LABEL = "agent quality audit"

    def __init__(  # noqa: PLR0913 - one table plus injectable client, retention, and clock
        self,
        table_name: str,
        client: DynamoDBClient | None = None,
        *,
        retention: RecordRetention | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Bind one table; its writer declares the retention, and every reader reads the clock.

        A reader needs no retention: it compares a stored expiry with `clock`. A writer that
        omits the retention cannot write, rather than writing a record that never expires.
        """
        if not table_name.strip():
            raise ValueError(f"{self._LABEL} table name is required.")
        self._table = table_name
        self._client = client
        self._retention = retention
        self._clock = clock or (lambda: datetime.now(UTC))

    @property
    def _dynamodb(self) -> DynamoDBClient:
        if self._client is None:
            self._client = boto3.client("dynamodb")
        return self._client

    def _expires_at(self, receipt_created_at: str) -> int:
        if self._retention is None:
            raise ValueError(f"{self._LABEL} writes require a declared record retention.")
        return self._retention.expires_at(receipt_created_at)

    def _expired(self, item: dict[str, AttributeValueTypeDef]) -> bool:
        """Report whether the stored expiry has passed; TTL deletes the item some time later."""
        _, expires_at = _split_expiry(item)
        return expires_at is not None and expires_at < self._clock().timestamp()

    def _get_item(self, audit_id: str, item_key: str) -> dict[str, AttributeValueTypeDef] | None:
        try:
            response = self._dynamodb.get_item(
                TableName=self._table,
                Key=_key(audit_id, item_key),
                ConsistentRead=True,
            )
        except Exception as error:
            self._read_error(error)
        found = response.get("Item")
        return None if found is None else dict(found)

    def _put(self, item: dict[str, AttributeValueTypeDef]) -> None:
        try:
            self._dynamodb.put_item(
                TableName=self._table,
                Item=item,
                ConditionExpression=f"attribute_not_exists({ITEM_KEY})",
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                raise
            self._write_error(error)
        except Exception as error:
            self._write_error(error)

    def holds_exactly(self, item: dict[str, AttributeValueTypeDef]) -> bool:
        """Report whether the table holds exactly `item`, expiry included: an exact replay.

        For a record written inside another adapter's transaction, which cannot use this
        store's own conditional write to recognise its replay.
        """
        stored = self._get_item(_read_text(item, AUDIT_ID), _read_text(item, ITEM_KEY))
        return stored == item

    def _write_exact(  # noqa: PLR0913, PLR0917
        self, item: dict[str, AttributeValueTypeDef], audit_id: str, item_key: str, conflict: str
    ) -> AuditWriteResult:
        try:
            self._put(item)
        except ClientError:
            existing = self._get_item(audit_id, item_key)
            if existing == item:
                return AuditWriteResult(disposition=AuditWriteDisposition.EXACT_REPLAY)
            self._conflict(conflict)
        return AuditWriteResult(disposition=AuditWriteDisposition.APPLIED)

    def _query_prefix(
        self, audit_id: str, prefix: str, bound: int
    ) -> list[dict[str, AttributeValueTypeDef]] | None:
        """Strongly query one audit partition's key prefix; `None` when the bound is exceeded.

        Expired items still count toward the bound: they occupy the partition until TTL deletes
        them. See `_within_bound` for why one more item than the bound is requested.
        """
        try:
            response = self._dynamodb.query(
                TableName=self._table,
                KeyConditionExpression=(
                    f"{AUDIT_ID} = :audit AND begins_with({ITEM_KEY}, :prefix)"
                ),
                ExpressionAttributeValues={
                    ":audit": _text(audit_id),
                    ":prefix": _text(prefix),
                },
                Limit=bound + 1,
                ConsistentRead=True,
            )
        except Exception as error:
            self._read_error(error)
        return _within_bound([dict(value) for value in response.get("Items", [])], response, bound)

    @staticmethod
    def _read_error(error: Exception) -> NoReturn:
        raise DependencyError(
            AUDIT_UNREADABLE,
            "Agent quality audit data could not be read.",
            context={"cause": type(error).__name__},
        ) from None

    @staticmethod
    def _write_error(error: Exception) -> NoReturn:
        raise DependencyError(
            AUDIT_UNWRITABLE,
            "Agent quality audit data could not be written.",
            context={"cause": type(error).__name__},
        ) from None

    @staticmethod
    def _conflict(description: str) -> NoReturn:
        raise InvariantError(AUDIT_CONFLICT, description)


class DynamoAgentQualityAuditStore(_ImmutableRecordTable, AgentQualityAuditStorePort):
    """The receipts table: receipts, delivery dispositions, and admissions; never scans."""

    def create(self, record: ConversationAuditRecordV6) -> AuditWriteResult:
        """Conditionally create one exact immutable audit receipt."""
        return self._write_exact(
            _record_item(record, self._expires_at(record.created_at)),
            record.audit_id,
            RECORD,
            "An audit ID names different immutable receipt bytes.",
        )

    def record_delivery(
        self, record: TurnDeliveryRecordV1, receipt_created_at: str
    ) -> AuditWriteResult:
        """Conditionally write one immutable delivery disposition companion (ADR-063)."""
        return self._write_exact(
            _delivery_item(record, self._expires_at(receipt_created_at)),
            record.audit_id,
            DELIVERY,
            "An audit ID names different immutable delivery bytes.",
        )

    def get_delivery(self, audit_id: str) -> TurnDeliveryRecordV1 | None:
        """Strongly load and attest the delivery companion, or report it absent (ADR-063)."""
        stored = self.get_delivery_with_expiry(audit_id)
        return None if stored is None else stored.record

    def get_delivery_with_expiry(self, audit_id: str) -> StoredRecord[TurnDeliveryRecordV1] | None:
        """Load the delivery companion as `get_delivery` does, with its stored expiry."""
        if not audit_id.strip():
            raise ValueError("audit ID is required.")
        item = self._get_item(audit_id, DELIVERY)
        if item is None:
            return None
        try:
            if self._expired(item):
                return None
            envelope, expires_at = _split_expiry(item)
            record = decode_contract(_read_text(envelope, PAYLOAD), TurnDeliveryRecordV1)
            if record.audit_id != audit_id or envelope != _delivery_item(record):
                raise ValueError("stored delivery envelope is not exact.")
        except TypeError, ValueError:
            self._conflict("Retained delivery disposition is malformed.")
        return StoredRecord(record=record, expires_at=expires_at)

    def _read_record(
        self, audit_id: str
    ) -> StoredRecord[ConversationAuditRecordV6] | _Expired | None:
        item = self._get_item(audit_id, RECORD)
        if item is None:
            return None
        try:
            if self._expired(item):
                return _EXPIRED
            _, expires_at = _split_expiry(item)
            return StoredRecord(record=_decode_record(item, audit_id), expires_at=expires_at)
        except TypeError, ValueError:
            self._conflict("Retained agent quality receipt is malformed.")

    def get(self, audit_id: str) -> ConversationAuditRecordV6 | None:
        """Strongly load and attest one exact audit receipt; an expired one reads as absent."""
        stored = self.get_with_expiry(audit_id)
        return None if stored is None else stored.record

    def get_with_expiry(self, audit_id: str) -> StoredRecord[ConversationAuditRecordV6] | None:
        """Load one receipt as `get` does, with the expiry read from its stored item."""
        if not audit_id.strip():
            raise ValueError("audit ID is required.")
        stored = self._read_record(audit_id)
        return None if isinstance(stored, _Expired) else stored

    def _attest_correlated(
        self, correlation_id: str, items: list[dict[str, AttributeValueTypeDef]]
    ) -> tuple[ConversationAuditRecordV6, ...]:
        """Attest each index key by its strongly read base receipt, skipping expired receipts.

        An expired receipt is absent to every reader even while TTL has not yet deleted it and
        its index key. A missing base receipt is absent too: TTL deletes the base item and its
        index entry separately, so an index key can briefly outlive the receipt it projects. Only
        a base receipt that exists and names another correlation fails attestation.
        """
        records: list[ConversationAuditRecordV6] = []
        for item in items:
            if set(item) != {AUDIT_ID, ITEM_KEY, CORRELATION_ID}:
                self._conflict("Agent quality correlation query returned a malformed key.")
            if (
                _read_text(item, ITEM_KEY) != RECORD
                or _read_text(item, CORRELATION_ID) != correlation_id
            ):
                self._conflict("Agent quality correlation query returned a non-record item.")
            audit_id = _read_text(item, AUDIT_ID)
            if not audit_id.strip():
                self._conflict("Agent quality correlation query returned a malformed key.")
            stored = self._read_record(audit_id)
            if stored is None or isinstance(stored, _Expired):
                continue
            if stored.record.correlation_id != correlation_id:
                self._conflict("Agent quality correlation index failed base-record attestation.")
            records.append(stored.record)
        return tuple(records)

    def discover(self, request: AuditCorrelationRequest) -> tuple[ConversationAuditRecordV6, ...]:
        """Query a single normalized correlation and attest each discovered receipt."""
        items: list[dict[str, AttributeValueTypeDef]] = []
        start: dict[str, AttributeValueTypeDef] | None = None
        while True:
            values = {":correlation": _text(request.correlation_id)}
            try:
                response = (
                    self._dynamodb.query(
                        TableName=self._table,
                        IndexName=CORRELATION_INDEX,
                        KeyConditionExpression=f"{CORRELATION_ID} = :correlation",
                        ExpressionAttributeValues=values,
                        ProjectionExpression=f"{AUDIT_ID}, {ITEM_KEY}, {CORRELATION_ID}",
                    )
                    if start is None
                    else self._dynamodb.query(
                        TableName=self._table,
                        IndexName=CORRELATION_INDEX,
                        KeyConditionExpression=f"{CORRELATION_ID} = :correlation",
                        ExpressionAttributeValues=values,
                        ProjectionExpression=f"{AUDIT_ID}, {ITEM_KEY}, {CORRELATION_ID}",
                        ExclusiveStartKey=start,
                    )
                )
            except Exception as error:
                self._read_error(error)
            items.extend(dict(item) for item in response.get("Items", []))
            last = response.get("LastEvaluatedKey")
            start = None if last is None else dict(last)
            if start is None:
                break
        records = self._attest_correlated(request.correlation_id, items)
        return tuple(sorted(records, key=lambda record: (record.created_at, record.audit_id)))

    def discover_bounded(
        self, request: AuditCorrelationRequest, limit: int
    ) -> tuple[ConversationAuditRecordV6, ...] | None:
        """Attest at most `limit` receipts for one correlation; `None` when the bound is hit.

        Expired receipts count toward the bound, as their index keys remain until TTL deletes
        them, and are then left out of the attested result.
        """
        try:
            response = self._dynamodb.query(
                TableName=self._table,
                IndexName=CORRELATION_INDEX,
                KeyConditionExpression=f"{CORRELATION_ID} = :correlation",
                ExpressionAttributeValues={":correlation": _text(request.correlation_id)},
                ProjectionExpression=f"{AUDIT_ID}, {ITEM_KEY}, {CORRELATION_ID}",
                Limit=limit + 1,
            )
        except Exception as error:
            self._read_error(error)
        items = _within_bound([dict(item) for item in response.get("Items", [])], response, limit)
        if items is None:
            return None
        return self._attest_correlated(request.correlation_id, items)

    def create_evaluation_admission(
        self, admission: EvaluationAdmissionV1, receipt_created_at: str
    ) -> AuditWriteResult:
        """Persist one selected admission before its protected SQS send attempt."""
        item_key = f"{EVALUATION_ADMISSION_PREFIX}{admission.admission_ref}"
        return self._write_exact(
            _admission_item(admission, self._expires_at(receipt_created_at)),
            admission.audit_id,
            item_key,
            "An evaluation admission reference names different immutable bytes.",
        )

    def get_evaluation_admission(
        self, audit_id: str, admission_ref: str
    ) -> EvaluationAdmissionV1 | None:
        """Strongly load one exact content-free evaluation admission."""
        stored = self.get_evaluation_admission_with_expiry(audit_id, admission_ref)
        return None if stored is None else stored.record

    def get_evaluation_admission_with_expiry(
        self, audit_id: str, admission_ref: str
    ) -> StoredRecord[EvaluationAdmissionV1] | None:
        """Load one admission as `get_evaluation_admission` does, with its stored expiry."""
        if not audit_id.strip():
            raise ValueError("audit ID is required.")
        item = self._get_item(audit_id, f"{EVALUATION_ADMISSION_PREFIX}{admission_ref}")
        if item is None:
            return None
        try:
            if self._expired(item):
                return None
            _, expires_at = _split_expiry(item)
            return StoredRecord(
                record=_decode_admission(item, audit_id, admission_ref), expires_at=expires_at
            )
        except TypeError, ValueError:
            self._conflict("Retained evaluation admission is malformed.")

    def list_turns(self, request: ReceiptTurnsPageRequest) -> ReceiptTurnsPage:
        """Read one page of the turns index: one bounded query of one creation day (ADR-078 §5).

        Days are read from the window's last day backwards, each newest first. The filters and
        the expired-record rule apply after the read, so a page may hold fewer rows than its
        limit, or none, while a cursor is still returned; the caller continues until the cursor
        is `None`.
        """
        days = _window_days(request)
        position = (
            _TurnsCursor.start(request, days)
            if request.cursor is None
            else (_TurnsCursor.decode(request, days))
        )
        items, last = self._query_turns_day(request, position)
        rows: list[ReceiptTurnRow] = []
        for item in items:
            try:
                expired = self._expired(item)
                row = _decode_turn_row(item, position.day)
            except TypeError, ValueError:
                self._conflict("Agent quality turns index returned a malformed row.")
            if expired or not _in_window(row, request):
                continue
            if request.terminal_outcomes and row.terminal_outcome not in request.terminal_outcomes:
                continue
            rows.append(row)
        return ReceiptTurnsPage(turns=tuple(rows), cursor=position.next(request, days, last))

    def _query_turns_day(
        self, request: ReceiptTurnsPageRequest, position: _TurnsCursor
    ) -> tuple[list[dict[str, AttributeValueTypeDef]], dict[str, str] | None]:
        low, high = _sort_key_range(request, position.day)
        try:
            response = (
                self._dynamodb.query(
                    TableName=self._table,
                    IndexName=TURNS_INDEX,
                    KeyConditionExpression=(
                        f"{TURNS_DAY} = :day AND {TURNS_CREATED_AT} BETWEEN :low AND :high"
                    ),
                    ExpressionAttributeValues={
                        ":day": _text(position.day.isoformat()),
                        ":low": _text(low),
                        ":high": _text(high),
                    },
                    ScanIndexForward=False,
                    Limit=request.limit,
                )
                if position.start_key is None
                else self._dynamodb.query(
                    TableName=self._table,
                    IndexName=TURNS_INDEX,
                    KeyConditionExpression=(
                        f"{TURNS_DAY} = :day AND {TURNS_CREATED_AT} BETWEEN :low AND :high"
                    ),
                    ExpressionAttributeValues={
                        ":day": _text(position.day.isoformat()),
                        ":low": _text(low),
                        ":high": _text(high),
                    },
                    ScanIndexForward=False,
                    Limit=request.limit,
                    ExclusiveStartKey={
                        name: _text(value) for name, value in position.start_key.items()
                    },
                )
            )
        except Exception as error:
            self._read_error(error)
        raw_last = response.get("LastEvaluatedKey")
        try:
            last = None if raw_last is None else _turns_start_key(dict(raw_last), position.day)
        except TypeError, ValueError:
            self._conflict("Agent quality turns index returned a malformed continuation key.")
        return [dict(item) for item in response.get("Items", [])], last


class DynamoAuditFindingStore(_ImmutableRecordTable, AuditFindingStorePort):
    """The findings table: only the processor's deterministic structural findings."""

    _LABEL = "agent quality findings"

    def get_findings(self, audit_id: str) -> tuple[AuditFindingV3, ...]:
        """Query only bounded immutable finding keys after an exact audit read."""
        return tuple(stored.record for stored in self.get_findings_with_expiry(audit_id))

    def get_findings_with_expiry(self, audit_id: str) -> tuple[StoredRecord[AuditFindingV3], ...]:
        """Read findings as `get_findings` does, each with the expiry of its stored item."""
        items = self._query_prefix(audit_id, FINDING_PREFIX, _FINDINGS_BOUND)
        if items is None:
            self._conflict("Agent quality finding query exceeded its bounded result contract.")
        findings: list[StoredRecord[AuditFindingV3]] = []
        for item in items:
            try:
                if self._expired(item):
                    continue
                finding = _decode_finding_payload(_read_text(item, PAYLOAD))
                _decode_finding(item, finding)
                _, expires_at = _split_expiry(item)
            except TypeError, ValueError:
                self._conflict("Retained agent quality finding is malformed.")
            if finding.audit_id != audit_id:
                self._conflict("Agent quality finding query crossed an audit partition.")
            findings.append(StoredRecord(record=finding, expires_at=expires_at))
        return tuple(
            sorted(
                findings, key=lambda stored: (stored.record.created_at, stored.record.finding_id)
            )
        )

    def write_finding(self, finding: AuditFindingV3, receipt_created_at: str) -> AuditWriteResult:
        """Conditionally create one deterministic immutable finding."""
        item = _finding_item(finding, self._expires_at(receipt_created_at))
        try:
            self._put(item)
        except ClientError:
            existing = self._get_item(finding.audit_id, f"{FINDING_PREFIX}{finding.finding_id}")
            try:
                if existing is not None and existing.get(EXPIRES_AT) == item[EXPIRES_AT]:
                    _decode_finding(existing, finding)
                    return AuditWriteResult(disposition=AuditWriteDisposition.EXACT_REPLAY)
            except TypeError, ValueError:
                pass
            self._conflict("A finding ID names different immutable structural finding bytes.")
        return AuditWriteResult(disposition=AuditWriteDisposition.APPLIED)


class DynamoEvaluationDeliveryStore(_ImmutableRecordTable, EvaluationDeliveryStorePort):
    """The evaluation deliveries table: only the outbox dispatcher's delivery receipts."""

    _LABEL = "evaluation deliveries"

    def write_evaluation_delivery(
        self, delivery: EvaluationDeliveryReceiptV2, receipt_created_at: str
    ) -> AuditWriteResult:
        """Persist one exact dispatch decision receipt."""
        item_key = f"{EVALUATION_DELIVERY_PREFIX}{delivery.admission_ref}"
        return self._write_exact(
            evaluation_delivery_item(delivery, self._expires_at(receipt_created_at)),
            delivery.audit_id,
            item_key,
            "An evaluation delivery reference names different immutable bytes.",
        )

    def get_evaluation_delivery(
        self, audit_id: str, admission_ref: str
    ) -> EvaluationDeliveryReceiptV2 | EvaluationDeliveryPredatesGate | None:
        """Strongly load one exact receipt; a V1 receipt is classified as predating the gate."""
        stored = self.get_evaluation_delivery_with_expiry(audit_id, admission_ref)
        return None if stored is None else stored.record

    def get_evaluation_delivery_with_expiry(
        self, audit_id: str, admission_ref: str
    ) -> StoredRecord[EvaluationDeliveryReceiptV2 | EvaluationDeliveryPredatesGate] | None:
        """Load one receipt as `get_evaluation_delivery` does, with its stored expiry."""
        item = self._get_item(audit_id, f"{EVALUATION_DELIVERY_PREFIX}{admission_ref}")
        if item is None:
            return None
        try:
            if self._expired(item):
                return None
            _, expires_at = _split_expiry(item)
            return StoredRecord(
                record=_decode_delivery(item, audit_id, admission_ref), expires_at=expires_at
            )
        except TypeError, ValueError:
            self._conflict("Retained evaluation delivery receipt is malformed.")


class DynamoEvaluationWriterReceiptStore(_ImmutableRecordTable, EvaluationWriterReceiptStorePort):
    """The writer receipts table: only the evaluation writer's protected-span receipts."""

    _LABEL = "evaluation writer receipts"

    def write_evaluation_writer_receipt(
        self, receipt: EvaluationWriterReceiptV1, receipt_created_at: str
    ) -> AuditWriteResult:
        """Persist one exact protected-span writer receipt after span emission."""
        item_key = f"{EVALUATION_WRITER_PREFIX}{receipt.admission_ref}"
        return self._write_exact(
            _linkage_item(
                _LinkageItemRequest(
                    audit_id=receipt.audit_id,
                    item_key=item_key,
                    record_type="EVALUATION_WRITER",
                    value=receipt,
                    expires_at=self._expires_at(receipt_created_at),
                )
            ),
            receipt.audit_id,
            item_key,
            "An evaluation writer reference names different immutable bytes.",
        )

    def get_evaluation_writer_receipt(
        self, audit_id: str, admission_ref: str
    ) -> EvaluationWriterReceiptV1 | None:
        """Strongly load one exact protected-span writer receipt."""
        stored = self.get_evaluation_writer_receipt_with_expiry(audit_id, admission_ref)
        return None if stored is None else stored.record

    def get_evaluation_writer_receipt_with_expiry(
        self, audit_id: str, admission_ref: str
    ) -> StoredRecord[EvaluationWriterReceiptV1] | None:
        """Load one writer receipt as `get_evaluation_writer_receipt` does, with its expiry."""
        item = self._get_item(audit_id, f"{EVALUATION_WRITER_PREFIX}{admission_ref}")
        if item is None:
            return None
        try:
            if self._expired(item):
                return None
            _, expires_at = _split_expiry(item)
            return StoredRecord(
                record=_decode_writer(item, audit_id, admission_ref), expires_at=expires_at
            )
        except TypeError, ValueError:
            self._conflict("Retained evaluation writer receipt is malformed.")


class DynamoEvaluationVerdictStore(_ImmutableRecordTable, EvaluationVerdictStorePort):
    """The verdicts table: only the result linker's per-evaluator results."""

    _LABEL = "evaluation verdicts"

    def write_evaluation_result(
        self, result: EvaluationResultV3, receipt_created_at: str
    ) -> AuditWriteResult:
        """Persist one exact allowlisted evaluator result with its provenance."""
        item_key = _result_item_key(result.admission_ref, result.evaluator_ref)
        return self._write_exact(
            _linkage_item(
                _LinkageItemRequest(
                    audit_id=result.audit_id,
                    item_key=item_key,
                    record_type="EVALUATION_RESULT",
                    value=result,
                    expires_at=self._expires_at(receipt_created_at),
                )
            ),
            result.audit_id,
            item_key,
            "An evaluation result reference names different immutable bytes.",
        )

    def get_evaluation_results(
        self, audit_id: str, admission_ref: str
    ) -> tuple[EvaluationResultV3, ...]:
        """Strongly load every evaluator's V3 result for one admission, in evaluator order.

        A stored V2 verdict still counts toward the partition bound but is skipped: verdicts
        linked before ADR-079 carry no provenance and are not read.
        """
        return tuple(
            stored.record
            for stored in self.get_evaluation_results_with_expiry(audit_id, admission_ref)
        )

    def get_evaluation_results_with_expiry(
        self, audit_id: str, admission_ref: str
    ) -> tuple[StoredRecord[EvaluationResultV3], ...]:
        """Read results as `get_evaluation_results` does, each with its stored item's expiry."""
        # Every key is per-evaluator. References cannot contain `#`, so this prefix names exactly
        # one admission's results and never a sibling whose reference merely extends this one.
        per_evaluator = _result_item_key(admission_ref, "")
        items = self._query_prefix(audit_id, per_evaluator, _MAX_RESULTS_PER_ADMISSION)
        if items is None:
            self._conflict("Evaluation result query exceeded its bounded result contract.")
        results: list[StoredRecord[EvaluationResultV3]] = []
        for item in items:
            try:
                if self._expired(item):
                    continue
                result = _decode_result(item, audit_id, admission_ref)
                _, expires_at = _split_expiry(item)
            except TypeError, ValueError:
                self._conflict("Retained evaluation result is malformed.")
            if result is not None:
                results.append(StoredRecord(record=result, expires_at=expires_at))
        return tuple(sorted(results, key=lambda stored: stored.record.evaluator_ref))


class DynamoEvaluationResultLinkResolver(EvaluationResultLinkResolverPort):
    """Resolve one service result through the receipts table and the writer receipts table."""

    def __init__(
        self,
        receipts: DynamoAgentQualityAuditStore,
        writer_receipts: DynamoEvaluationWriterReceiptStore,
    ) -> None:
        self._receipts = receipts
        self._writer_receipts = writer_receipts

    def resolve_evaluation_result(
        self, request: EvaluationResultLinkRequest
    ) -> EvaluationResultLinkResolution:
        """Bound the correlation lookup and strongly attest exactly one audit/writer pair.

        The linked target carries the receipt's creation time, so the linker, which never reads
        the receipts table, computes its verdict's expiry from it (ADR-080 §1).
        """
        records = self._receipts.discover_bounded(
            AuditCorrelationRequest(correlation_id=request.correlation_id), _CORRELATION_BOUND
        )
        if records is None:
            return EvaluationResultLinkResolution(
                state=EvaluationResultLinkResolutionState.UNMATCHED
            )
        targets: list[EvaluationResultLinkTarget] = []
        pending = False
        for record in records:
            admission_ref = record.evaluation_admission_ref
            if admission_ref is None:
                continue
            writer = self._writer_receipts.get_evaluation_writer_receipt(
                record.audit_id, admission_ref
            )
            if writer is None:
                pending = True
                continue
            if writer.trace_id == request.trace_id:
                targets.append(
                    EvaluationResultLinkTarget(
                        audit_id=record.audit_id,
                        admission_ref=admission_ref,
                        receipt_created_at=record.created_at,
                    )
                )
        if len(targets) == 1:
            return EvaluationResultLinkResolution(
                state=EvaluationResultLinkResolutionState.LINKED,
                target=targets[0],
            )
        return EvaluationResultLinkResolution(
            state=(
                EvaluationResultLinkResolutionState.PENDING
                if pending
                else EvaluationResultLinkResolutionState.UNMATCHED
            )
        )


class DynamoAgentQualityAggregateStore(AgentQualityAggregatePort):
    """Immutable aggregate-ready measurements in their dedicated retained table."""

    def __init__(self, table_name: str, client: DynamoDBClient | None = None) -> None:
        if not table_name.strip():
            raise ValueError("agent quality aggregate table name is required.")
        self._table = table_name
        self._client = client

    @property
    def _dynamodb(self) -> DynamoDBClient:
        if self._client is None:
            self._client = boto3.client("dynamodb")
        return self._client

    def write_metric(self, metric: AuditAggregateMetricV2) -> AuditWriteResult:
        """Conditionally create one immutable aggregate-ready measurement."""
        item = _metric_item(metric)
        try:
            self._dynamodb.put_item(
                TableName=self._table,
                Item=item,
                ConditionExpression=f"attribute_not_exists({METRIC_ID})",
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise DependencyError(
                    AUDIT_UNWRITABLE,
                    "Agent quality aggregate could not be written.",
                    context={"cause": type(error).__name__},
                ) from None
            try:
                response = self._dynamodb.get_item(
                    TableName=self._table,
                    Key={METRIC_ID: _text(metric.metric_id)},
                    ConsistentRead=True,
                )
                found = response.get("Item")
                if found is not None:
                    _decode_metric(dict(found), metric)
                    return AuditWriteResult(disposition=AuditWriteDisposition.EXACT_REPLAY)
            except TypeError, ValueError:
                pass
            except Exception as read_error:
                raise DependencyError(
                    AUDIT_UNREADABLE,
                    "Agent quality aggregate could not be read.",
                    context={"cause": type(read_error).__name__},
                ) from None
            raise InvariantError(
                AUDIT_CONFLICT, "A metric ID names different immutable aggregate bytes."
            ) from None
        except Exception as error:
            raise DependencyError(
                AUDIT_UNWRITABLE,
                "Agent quality aggregate could not be written.",
                context={"cause": type(error).__name__},
            ) from None
        return AuditWriteResult(disposition=AuditWriteDisposition.APPLIED)
