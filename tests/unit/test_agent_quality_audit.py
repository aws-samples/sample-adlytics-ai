# SPDX-License-Identifier: MIT-0
"""Content-minimized Agent Quality audit contract, DynamoDB envelopes, and processor derivation."""

import importlib
import inspect
import json
import os
import sys
from collections.abc import Callable
from dataclasses import fields, replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from aws_lambda_powertools import Logger

os.environ.setdefault("ADL_AGENT_QUALITY_AUDIT_TABLE", "agent-quality-audit-test")
os.environ.setdefault("ADL_AGENT_QUALITY_FINDINGS_TABLE", "agent-quality-findings-test")
os.environ.setdefault("ADL_AGENT_QUALITY_AGGREGATE_TABLE", "agent-quality-aggregate-test")
os.environ.setdefault("ADL_RECEIPT_RETENTION_DAYS", "90")

from adapters.config import (
    quality_review_settings,
    settings_without_client_secret,
    tracing_settings,
)
from adapters.jobs.agent_quality_processor import handler as processor_handler
from adapters.jobs.agent_quality_processor.handler import aggregate_metric, derive_findings
from adapters.outbound.dynamodb.audit import (
    AUDIT_ID,
    CORRELATION_AUDIT_ID,
    CORRELATION_ID,
    ITEM_KEY,
    PAYLOAD,
    PAYLOAD_DIGEST,
    RECORD,
    TERMINAL_OUTCOME,
    TURNS_CREATED_AT,
    TURNS_DAY,
    _decode_finding_payload,  # pyright: ignore[reportPrivateUsage] - exact envelope test seam
    _decode_record,  # pyright: ignore[reportPrivateUsage] - exact envelope test seam
    _decode_result,  # pyright: ignore[reportPrivateUsage] - exact envelope test seam
    _digest,  # pyright: ignore[reportPrivateUsage] - exact legacy attestation test seam
    _finding_item,  # pyright: ignore[reportPrivateUsage] - exact envelope test seam
    _linkage_item,  # pyright: ignore[reportPrivateUsage] - exact envelope test seam
    _LinkageItemRequest,  # pyright: ignore[reportPrivateUsage] - exact envelope test seam
    _record_item,  # pyright: ignore[reportPrivateUsage] - exact envelope test seam
    _result_item_key,  # pyright: ignore[reportPrivateUsage] - exact key test seam
)
from adapters.outbound.telemetry.reporting import AuditProcessorLog, Report
from domain.conversation.audit import (
    MANAGED_HARNESS_RUNTIME_V1,
    MANAGED_HARNESS_RUNTIME_V2,
    MANAGED_HARNESS_RUNTIME_V3,
    AuditCompletionRefusalStageV2,
    AuditContextReasonV1,
    AuditContextStatusV1,
    AuditFindingV3,
    AuditOperationCategoryV2,
    AuditOperationOutcomeV2,
    AuditPublicationOutcomeV2,
    AuditPublicConversationBlockKindV1,
    AuditTerminalOutcomeV2,
    AuditToolStepDispositionV3,
    AuditToolStepV3,
    ConversationAuditRecordV6,
    TurnDeliveryDispositionV1,
    TurnDeliveryRecordV1,
)
from domain.evaluation.linkage import (
    EvaluationDeliveryFamilyV1,
    EvaluationDeliveryOutcomeV1,
    EvaluationDeliveryReceiptV2,
    EvaluationNotDispatchedReasonV1,
    EvaluationResultStateV1,
    EvaluationResultV3,
)
from domain.evaluation.outbox import evaluation_delivery_ref
from domain.evaluation.release import EvaluationFamilyV1, EvaluationReleaseEvaluatorV1
from domain.identity.session import ReviewAccessLevel
from domain.shared_kernel.errors import DependencyError, ErrorCode, InvariantError
from ports.outbound.audit import (
    AuditCorrelationRequest,
    EvaluationDeliveryPredatesGate,
    ReceiptTurnRow,
    ReceiptTurnsPage,
    ReceiptTurnsPageRequest,
    StoredRecord,
)

pytestmark = pytest.mark.unit

_CORRELATION_ID = "123e4567-e89b-42d3-a456-426614174000"
_TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
_RESULT_REF = "7a1c9f2e-3b4d-4e5f-8a6b-1c2d3e4f5a6b"
_DIGEST = "sha256:" + "a" * 64
_PRINCIPAL = "5fdbe1d5-7901-4fee-9d1d-518103789c94"
"""A fictional Cognito `sub`: the verified user the receipt attributes the turn to."""


def _governed(ordinal: int, *, selected: bool = True) -> AuditToolStepV3:
    return AuditToolStepV3(
        ordinal=ordinal,
        tool_name="catalog_list_metrics",
        disposition=AuditToolStepDispositionV3.SUCCEEDED,
        step_ref=_RESULT_REF,
        selected_into_plan=selected,
    )


def _rejected(ordinal: int) -> AuditToolStepV3:
    return AuditToolStepV3(
        ordinal=ordinal,
        tool_name="presentation_publish",
        disposition=AuditToolStepDispositionV3.REJECTED,
        step_ref="finalizer-message-0",
        violations=("unknown_rendering",),
    )


def _accepted(ordinal: int) -> AuditToolStepV3:
    return AuditToolStepV3(
        ordinal=ordinal,
        tool_name="presentation_publish",
        disposition=AuditToolStepDispositionV3.ACCEPTED,
        step_ref="finalizer-message-1",
    )


def _record() -> ConversationAuditRecordV6:
    return ConversationAuditRecordV6(
        audit_id="audit-01",
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
        evaluation_admission_ref=None,
        created_at="2026-09-02T12:00:00+00:00",
        trajectory=(_governed(1), _rejected(2), _accepted(3)),
        tool_custody_digest=_DIGEST,
        presentation_plan_digest=_DIGEST,
        public_block_kinds=(AuditPublicConversationBlockKindV1.PARAGRAPH,),
        public_block_count=1,
        guardrail_version="3",
        principal=_PRINCIPAL,
    )


def _payload(item: dict[str, Any]) -> dict[str, Any]:
    return cast("dict[str, Any]", json.loads(cast("dict[str, str]", item[PAYLOAD])["S"]))


def _v2_document(*, planner_slots_empty: bool = True) -> dict[str, Any]:
    """One receipt as the managed-harness.v1 runtime actually wrote it."""
    return {
        "schema_version": "ConversationAuditRecord.v2",
        "audit_id": "audit-01",
        "correlation_id": _CORRELATION_ID,
        "terminal_summary": {
            "proposed_objective_categories": [] if planner_slots_empty else ["explain"],
            "admitted_objective_categories": [] if planner_slots_empty else ["explain"],
            "authority_source": "none" if planner_slots_empty else "policy",
            "admitted_objective_count": 0 if planner_slots_empty else 1,
        },
        "objective_summaries": [],
        "operation_category": "composition",
        "operation_outcome": "succeeded",
        "publication_outcome": "published",
        "terminal_outcome": "complete",
        "runtime_version": MANAGED_HARNESS_RUNTIME_V1,
        "prompt_catalog_digest": _DIGEST,
        "page_schema_version": "presentation.v2",
        "response_contract_version": "public-block-stream.v3",
        "turn_evidence_ref": None,
        "prompt_receipt_ref": None,
        "epistemic_record_refs": [],
        "evaluation_admission_ref": None,
        "created_at": "2026-09-02T12:00:00+00:00",
        "initial_plan_refusal_code": None,
        "failure_stage": None,
        "failure_error_code": None,
        "failure_service_code": None,
        "completion_refusal_stage": None,
        "tool_trajectory": [
            {
                "ordinal": 1,
                "tool_name": "catalog_list_metrics",
                "disposition": "succeeded",
                "contract_version": "ShapedResponse.v1",
            }
        ],
        "tool_custody_digest": _DIGEST,
        "presentation_plan_digest": _DIGEST,
        "public_block_kinds": ["paragraph"],
        "public_block_count": 1,
        "guardrail_version": "3",
    }


def _stored_item(document: dict[str, Any]) -> dict[str, Any]:
    payload = json.dumps(
        document, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False
    )
    return {
        AUDIT_ID: {"S": document["audit_id"]},
        ITEM_KEY: {"S": RECORD},
        "record_type": {"S": RECORD},
        PAYLOAD: {"S": payload},
        PAYLOAD_DIGEST: {"S": _digest(payload)},
        CORRELATION_ID: {"S": document["correlation_id"]},
        CORRELATION_AUDIT_ID: {"S": document["audit_id"]},
    }


@pytest.mark.covers("ADR-062")
def test_audit_record_accepts_only_bounded_opaque_fields() -> None:
    record = _record()
    with pytest.raises(ValueError, match="opaque"):
        replace(record, page_schema_version="presentation v2 with request text")
    with pytest.raises(ValueError, match="SHA-256"):
        replace(record, prompt_catalog_digest="sha256:abc123")
    with pytest.raises(ValueError, match="UTC"):
        replace(record, created_at="2026-09-02T12:00:00+01:00")
    with pytest.raises(ValueError, match="trace ID"):
        replace(record, trace_id="1-68b1c2d3-abcdef")
    with pytest.raises(ValueError, match="managed Harness generation"):
        replace(record, runtime_version="runtime.v2")
    with pytest.raises(ValueError, match="declared response contract"):
        replace(record, response_contract_version="assistant-response.v2")
    with pytest.raises(ValueError, match="block count"):
        replace(record, public_block_count=2)


@pytest.mark.covers("ADR-062")
def test_audit_trajectory_keeps_every_attempt_anchored_and_ends_accepted() -> None:
    record = _record()
    assert [step.disposition.value for step in record.trajectory] == [
        "succeeded",
        "rejected",
        "accepted",
    ]
    assert record.trajectory[1].violations == ("unknown_rendering",)
    with pytest.raises(ValueError, match="contiguous"):
        replace(record, trajectory=(_governed(1), _accepted(3)))
    with pytest.raises(ValueError, match="one accepted finalizer"):
        replace(record, trajectory=(_governed(1),))
    with pytest.raises(ValueError, match="one accepted finalizer"):
        replace(record, trajectory=(_accepted(1), _governed(2)))
    with pytest.raises(ValueError, match="reference and selection"):
        replace(
            record,
            trajectory=(
                AuditToolStepV3(
                    ordinal=1,
                    tool_name="catalog_list_metrics",
                    disposition=AuditToolStepDispositionV3.SUCCEEDED,
                    step_ref=None,
                ),
                _accepted(2),
            ),
        )


def test_audit_tool_step_rejects_untyped_or_contradictory_shapes() -> None:
    with pytest.raises(ValueError, match="typed codes"):
        replace(_rejected(1), violations=("Unsupported Numeric!",))
    with pytest.raises(ValueError, match="violation codes"):
        replace(_rejected(1), violations=())
    with pytest.raises(ValueError, match="no plan selection"):
        replace(_accepted(1), selected_into_plan=True)
    with pytest.raises(ValueError, match="no violations"):
        replace(_governed(1), violations=("x",))
    with pytest.raises(ValueError, match="no reference"):
        AuditToolStepV3(
            ordinal=1,
            tool_name="presentation_publish",
            disposition=AuditToolStepDispositionV3.ERRORED,
            step_ref="finalizer-message-0",
        )


def test_dependency_audit_record_accepts_bounded_failure_details() -> None:
    record = replace(
        _record(),
        operation_outcome=AuditOperationOutcomeV2.FAILED,
        publication_outcome=AuditPublicationOutcomeV2.NOT_APPLICABLE,
        terminal_outcome=AuditTerminalOutcomeV2.DEPENDENCY_FAILURE,
        trajectory=(),
        public_block_kinds=(),
        public_block_count=0,
        failure_error_code="ADLY-MANAGED_CONVERSATION-0001",
        failure_service_code="ThrottlingException",
    )

    assert record.failure_error_code == "ADLY-MANAGED_CONVERSATION-0001"
    assert record.failure_service_code == "ThrottlingException"
    with pytest.raises(ValueError, match="failure details"):
        replace(_record(), failure_error_code="ADLY-MANAGED_CONVERSATION-0001")
    with pytest.raises(ValueError, match="failure details"):
        replace(record, failure_error_code="not a code")


def test_completion_refusal_audit_record_accepts_only_its_bounded_stage() -> None:
    record = replace(
        _record(),
        operation_outcome=AuditOperationOutcomeV2.REFUSED,
        publication_outcome=AuditPublicationOutcomeV2.REFUSED,
        terminal_outcome=AuditTerminalOutcomeV2.GOVERNED_REFUSAL,
        public_block_kinds=(),
        public_block_count=0,
        completion_refusal_stage=AuditCompletionRefusalStageV2.PUBLISHER_RESPONSE_BOUND,
    )

    assert record.completion_refusal_stage is AuditCompletionRefusalStageV2.PUBLISHER_RESPONSE_BOUND
    with pytest.raises(ValueError, match="completion refusal stage"):
        replace(
            _record(),
            completion_refusal_stage=AuditCompletionRefusalStageV2.PUBLISHER_RESPONSE_BOUND,
        )


def test_audit_record_and_envelope_exclude_conversation_and_plan_content() -> None:
    record = _record()
    prohibited = {
        "prompt",
        "question",
        "answer",
        "page_context",
        "destination_ref",
        "artifact",
        "forecast",
        "model_plan",
        "plan_json",
        "released_answer",
    }
    assert {field.name for field in fields(record)}.isdisjoint(prohibited)
    payload = _payload(cast("dict[str, Any]", _record_item(record)))
    assert set(payload).isdisjoint(prohibited)
    assert payload["schema_version"] == "ConversationAuditRecord.v6"
    assert payload["principal"] == _PRINCIPAL
    assert payload["released_character_count"] == 0
    assert payload["trace_id"] == _TRACE_ID
    assert payload["trajectory"][1] == {
        "ordinal": 2,
        "tool_name": "presentation_publish",
        "disposition": "rejected",
        "step_ref": "finalizer-message-0",
        "violations": ["unknown_rendering"],
        "selected_into_plan": None,
    }


def test_record_envelope_is_canonical_and_correlation_indexed() -> None:
    record = _record()
    item = _record_item(record)
    assert item[AUDIT_ID] == {"S": record.audit_id}
    assert item[ITEM_KEY] == {"S": RECORD}
    assert item[CORRELATION_ID] == {"S": record.correlation_id}
    assert item[CORRELATION_AUDIT_ID] == {"S": record.audit_id}
    payload = cast("dict[str, str]", item[PAYLOAD])["S"]
    assert '"schema_version":"ConversationAuditRecord.v6"' in payload
    assert _decode_record(item, record.audit_id) == record


@pytest.mark.covers("ADR-078")
def test_a_new_receipt_carries_its_turns_index_attributes_derived_from_the_payload() -> None:
    record = _record()
    item = _record_item(record)
    assert (item[TURNS_DAY], item[TURNS_CREATED_AT], item[TERMINAL_OUTCOME]) == (
        {"S": "2026-09-02"},
        {"S": "2026-09-02T12:00:00+00:00"},
        {"S": "complete"},
    )
    assert _decode_record(item, record.audit_id) == record


@pytest.mark.covers("ADR-078")
@pytest.mark.parametrize(
    ("created_at", "day"),
    [
        ("2026-09-02T23:59:59.999999+00:00", "2026-09-02"),
        ("2026-09-03T00:00:00Z", "2026-09-03"),
    ],
)
def test_the_turns_day_is_the_utc_creation_date(created_at: str, day: str) -> None:
    item = _record_item(replace(_record(), created_at=created_at))
    assert item[TURNS_DAY] == {"S": day}
    assert item[TURNS_CREATED_AT] == {"S": created_at}


@pytest.mark.covers("ADR-078")
def test_a_receipt_written_before_the_turns_index_still_decodes_exactly() -> None:
    """It is not listed by `turns`, and `turn` still reads it until it expires."""
    record = _record()
    item = _record_item(record)
    for name in (TURNS_DAY, TURNS_CREATED_AT, TERMINAL_OUTCOME):
        del item[name]
    assert _decode_record(item, record.audit_id) == record


@pytest.mark.covers("ADR-078")
@pytest.mark.parametrize(
    ("name", "value"),
    [
        (TURNS_DAY, "2026-09-01"),
        (TURNS_CREATED_AT, "2026-09-02T11:00:00+00:00"),
        (TERMINAL_OUTCOME, "dependency_failure"),
    ],
)
def test_a_tampered_turns_attribute_is_refused(name: str, value: str) -> None:
    """An edited index attribute would misfile or misfilter a turn the payload says otherwise."""
    item = _record_item(_record())
    item[name] = {"S": value}
    with pytest.raises(ValueError, match="turns attributes are not exact"):
        _decode_record(item, "audit-01")


@pytest.mark.covers("ADR-078")
@pytest.mark.parametrize("name", ["turns_day", "turns_created_at", "terminal_outcome"])
def test_a_receipt_carrying_only_some_turns_attributes_is_refused(name: str) -> None:
    item = _record_item(_record())
    del item[name]
    with pytest.raises(ValueError, match="turns attributes are not exact"):
        _decode_record(item, "audit-01")


@pytest.mark.covers("ADR-062")
def test_stored_managed_v2_receipt_reads_as_v5_without_anchors_trace_or_principal() -> None:
    """Receipts already written by managed-harness.v1 stay readable.

    They are honest about what that runtime never recorded: no anchors, no trace.
    """
    item = _stored_item(_v2_document())

    record = _decode_record(item, "audit-01")

    assert record.schema_version == "ConversationAuditRecord.v6"
    assert record.runtime_version == MANAGED_HARNESS_RUNTIME_V1
    assert record.released_character_count == 0
    assert record.trace_id is None
    assert record.principal is None
    assert record.trajectory == (
        AuditToolStepV3(
            ordinal=1,
            tool_name="catalog_list_metrics",
            disposition=AuditToolStepDispositionV3.SUCCEEDED,
            step_ref=None,
        ),
    )
    assert record.tool_custody_digest == _DIGEST
    with pytest.raises(ValueError, match="no anchors or trace"):
        replace(record, trace_id=_TRACE_ID)


def _v3_document() -> dict[str, Any]:
    """One receipt as the managed-harness.v2 runtime wrote it under the V3 contract."""
    document = _v5_document()
    del document["principal"]
    del document["released_character_count"]
    return {
        **document,
        "schema_version": "ConversationAuditRecord.v3",
        "runtime_version": (MANAGED_HARNESS_RUNTIME_V2),
    }


@pytest.mark.covers("ADR-077")
def test_a_current_receipt_carries_its_verified_principal_through_the_store() -> None:
    record = _record()
    decoded = _decode_record(_record_item(record), record.audit_id)

    assert decoded.principal == _PRINCIPAL
    assert decoded == record
    assert _PRINCIPAL not in repr(record)


@pytest.mark.covers("ADR-077")
def test_the_current_runtime_generation_cannot_write_a_receipt_without_a_principal() -> None:
    with pytest.raises(ValueError, match="must name its verified principal"):
        replace(_record(), principal=None)
    with pytest.raises(ValueError, match="opaque bounded reference"):
        replace(_record(), principal="not a sub")


@pytest.mark.covers("ADR-077")
def test_an_earlier_runtime_generation_can_never_claim_a_principal() -> None:
    """A principal must come from the route that verified it, never be back-filled onto history."""
    with pytest.raises(ValueError, match="records no principal"):
        replace(_record(), runtime_version=MANAGED_HARNESS_RUNTIME_V2)


@pytest.mark.covers("ADR-077")
def test_a_stored_v3_receipt_reads_as_v5_with_no_principal() -> None:
    record = _decode_record(_stored_item(_v3_document()), "audit-01")

    assert record.schema_version == "ConversationAuditRecord.v6"
    assert record.released_character_count == 0
    assert record.runtime_version == MANAGED_HARNESS_RUNTIME_V2
    assert record.principal is None
    assert record.trace_id == _TRACE_ID


def _with_back_filled_principal(document: dict[str, Any]) -> dict[str, Any]:
    return {**document, "principal": _PRINCIPAL}


def _without_guardrail_version(document: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in document.items() if key != "guardrail_version"}


@pytest.mark.covers("ADR-077")
@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (_with_back_filled_principal, "cannot carry a principal"),
        (_without_guardrail_version, "not canonical"),
    ],
    ids=["a back-filled principal", "a field its writer always wrote, removed"],
)
def test_a_stored_v3_receipt_that_its_writer_could_not_have_written_is_refused(
    edit: Callable[[dict[str, Any]], dict[str, Any]], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        _decode_record(_stored_item(edit(_v3_document())), "audit-01")


def _v4_document() -> dict[str, Any]:
    """One block-stream receipt as the managed-harness.v3 runtime wrote it under the V4 contract."""
    document = _v5_document()
    del document["released_character_count"]
    return {**document, "schema_version": "ConversationAuditRecord.v4"}


def _v5_document() -> dict[str, Any]:
    """One receipt as the V5 writer wrote it, before turns carried page context (ADR-120)."""
    document = _payload(cast("dict[str, Any]", _record_item(_record())))
    del document["page_context_reference"]
    del document["context_status"]
    return {**document, "schema_version": "ConversationAuditRecord.v5"}


@pytest.mark.covers("ADR-120")
def test_a_stored_v5_receipt_reads_as_v6_with_no_page_context() -> None:
    record = _decode_record(_stored_item(_v5_document()), "audit-01")

    assert record == _record()
    assert record.schema_version == "ConversationAuditRecord.v6"
    assert record.page_context_reference is None
    assert record.context_status is None


@pytest.mark.covers("ADR-120")
def test_a_stored_v5_receipt_with_page_context_is_refused() -> None:
    document = {**_v5_document(), "page_context_reference": None, "context_status": None}

    with pytest.raises(ValueError, match="cannot carry page context"):
        _decode_record(_stored_item(document), "audit-01")


@pytest.mark.covers("ADR-120")
def test_a_receipt_records_the_page_reference_and_status_but_no_label_text() -> None:
    record = replace(
        _record(),
        page_context_reference="R" * 43,
        context_status=AuditContextStatusV1(used=False, reason=AuditContextReasonV1.EXPIRED),
    )
    payload = _payload(cast("dict[str, Any]", _record_item(record)))

    assert payload["page_context_reference"] == "R" * 43
    assert payload["context_status"] == {"reason": "expired", "used": False}
    assert _decode_record(_stored_item(payload), "audit-01") == record
    with pytest.raises(ValueError, match="exactly when a reference was sent"):
        replace(_record(), page_context_reference="R" * 43)


@pytest.mark.covers("ADR-083")
def test_a_stored_v4_receipt_reads_as_v5_with_no_released_text() -> None:
    record = _decode_record(_stored_item(_v4_document()), "audit-01")

    assert record == _record()
    assert record.schema_version == "ConversationAuditRecord.v6"
    assert record.released_character_count == 0
    assert record.streamed is False
    assert record.principal == _PRINCIPAL


def _with_released_count(document: dict[str, Any]) -> dict[str, Any]:
    return {**document, "released_character_count": 0}


@pytest.mark.covers("ADR-083")
@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (_with_released_count, "cannot carry a released text count"),
        (_without_guardrail_version, "not canonical"),
    ],
    ids=["a back-filled released count", "a field its writer always wrote, removed"],
)
def test_a_stored_v4_receipt_that_its_writer_could_not_have_written_is_refused(
    edit: Callable[[dict[str, Any]], dict[str, Any]], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        _decode_record(_stored_item(edit(_v4_document())), "audit-01")


def _streamed_record() -> ConversationAuditRecordV6:
    """One receipt as the streamed-answer runtime writes it (ADR-083)."""
    return ConversationAuditRecordV6(
        audit_id="audit-01",
        correlation_id=_CORRELATION_ID,
        trace_id=_TRACE_ID,
        operation_category=AuditOperationCategoryV2.COMPOSITION,
        operation_outcome=AuditOperationOutcomeV2.SUCCEEDED,
        publication_outcome=AuditPublicationOutcomeV2.PUBLISHED,
        terminal_outcome=AuditTerminalOutcomeV2.COMPLETE,
        runtime_version=MANAGED_HARNESS_RUNTIME_V3,
        prompt_catalog_digest=_DIGEST,
        page_schema_version="answer.markdown.v1",
        response_contract_version="conversation-stream.v4",
        evaluation_admission_ref="admission-01",
        created_at="2026-09-02T12:00:00+00:00",
        trajectory=(
            AuditToolStepV3(
                ordinal=1,
                tool_name="catalog_list_metrics",
                disposition=AuditToolStepDispositionV3.SUCCEEDED,
                step_ref=_RESULT_REF,
            ),
            AuditToolStepV3(
                ordinal=2,
                tool_name="catalog_describe_metric",
                disposition=AuditToolStepDispositionV3.ERRORED,
                step_ref=None,
            ),
        ),
        tool_custody_digest=_DIGEST,
        guardrail_version="3",
        principal=_PRINCIPAL,
        released_character_count=42,
    )


@pytest.mark.covers("ADR-083")
def test_a_streamed_receipt_round_trips_canonically_through_the_store_encoding() -> None:
    record = _streamed_record()
    item = _record_item(record)

    payload = _payload(cast("dict[str, Any]", item))
    assert payload["schema_version"] == "ConversationAuditRecord.v6"
    assert payload["response_contract_version"] == "conversation-stream.v4"
    assert payload["released_character_count"] == 42
    assert (payload["presentation_plan_digest"], payload["public_block_kinds"]) == (None, [])
    assert _decode_record(item, record.audit_id) == record
    assert record.streamed is True


@pytest.mark.covers("ADR-083")
def test_a_stored_streamed_receipt_missing_its_released_count_is_not_canonical() -> None:
    document = _payload(cast("dict[str, Any]", _record_item(_streamed_record())))
    del document["released_character_count"]

    with pytest.raises(ValueError, match="not canonical"):
        _decode_record(_stored_item(document), "audit-01")


_UNANCHORED_GOVERNED = AuditToolStepV3(
    ordinal=1,
    tool_name="catalog_list_metrics",
    disposition=AuditToolStepDispositionV3.SUCCEEDED,
    step_ref=None,
)


@pytest.mark.covers("ADR-083")
@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"presentation_plan_digest": _DIGEST}, "no plan and no blocks"),
        (
            {
                "public_block_kinds": (AuditPublicConversationBlockKindV1.PARAGRAPH,),
                "public_block_count": 1,
            },
            "no plan and no blocks",
        ),
        (
            {
                "operation_outcome": AuditOperationOutcomeV2.REFUSED,
                "publication_outcome": AuditPublicationOutcomeV2.REFUSED,
                "terminal_outcome": AuditTerminalOutcomeV2.GOVERNED_REFUSAL,
                "completion_refusal_stage": AuditCompletionRefusalStageV2.PUBLISHER_RESPONSE_BOUND,
            },
            "no plan and no blocks",
        ),
        ({"trajectory": (_governed(1, selected=False), _accepted(2))}, "only governed calls"),
        ({"trajectory": (_rejected(1),)}, "only governed calls"),
        ({"trajectory": (_governed(1),)}, "only governed calls"),
        ({"trajectory": (_UNANCHORED_GOVERNED,)}, "only governed calls"),
        ({"released_character_count": -1}, "released characters"),
    ],
    ids=[
        "plan-digest",
        "block-kinds",
        "completion-refusal-stage",
        "accepted-finalizer",
        "rejected-finalizer",
        "plan-selection",
        "unanchored-governed-step",
        "negative-count",
    ],
)
def test_a_streamed_receipt_records_no_plan_blocks_or_finalizer_steps(
    changes: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        replace(_streamed_record(), **changes)


@pytest.mark.covers("ADR-083")
def test_a_block_stream_receipt_records_no_streamed_characters() -> None:
    with pytest.raises(ValueError, match="no streamed characters"):
        replace(_record(), released_character_count=1)


@pytest.mark.covers("ADR-082")
def test_a_stored_receipt_carrying_the_retired_numeric_literal_code_still_decodes() -> None:
    """A retired violation code stays readable on the receipts that recorded it.

    Audit violation codes are stored as bounded strings, not decoded into the verifier's enum.
    """
    document = _payload(cast("dict[str, Any]", _record_item(_record())))
    document["trajectory"][1]["violations"] = ["unsupported_numeric_literal"]
    record = _decode_record(_stored_item(document), "audit-01")
    assert record.trajectory[1].violations == ("unsupported_numeric_literal",)


def test_stored_planner_era_v2_receipt_is_retired_rather_than_read() -> None:
    with pytest.raises(ValueError, match="retired planner-era shape"):
        _decode_record(_stored_item(_v2_document(planner_slots_empty=False)), "audit-01")


def test_record_attestation_rejects_substituted_edited_or_v1_payload() -> None:
    item = _record_item(_record())
    item[PAYLOAD] = {"S": '{"schema_version":"ConversationAuditRecord.v1"}'}
    with pytest.raises(ValueError, match="unsupported"):
        _decode_record(item, "audit-01")
    edited = _stored_item(_v2_document())
    edited[PAYLOAD] = {"S": cast("dict[str, str]", edited[PAYLOAD])["S"].replace('"3"', '"9"')}
    with pytest.raises(ValueError):
        _decode_record(edited, "audit-01")


def test_finding_envelope_is_immutable_and_uses_audit_keyspace() -> None:
    finding = derive_findings(_record())[0]
    item = _finding_item(finding)
    assert item[AUDIT_ID] == {"S": "audit-01"}
    item_key = item[ITEM_KEY].get("S")
    assert isinstance(item_key, str)
    assert item_key.startswith("FINDING#")
    assert _decode_finding_payload(cast("dict[str, str]", item[PAYLOAD])["S"]) == finding


@pytest.mark.covers("ADR-080")
def test_a_v2_finding_is_refused_because_the_findings_table_holds_only_v3() -> None:
    """Legacy V2 findings stayed in the receipts table, which is never read for findings."""
    document = {
        "schema_version": "AuditFinding.v2",
        "finding_id": "finding-01",
        "audit_id": "audit-01",
        "kind": "governed_refusal",
        "severity": "info",
        "created_at": "2026-09-02T12:00:00+00:00",
    }
    payload = json.dumps(document, separators=(",", ":"), sort_keys=True)

    with pytest.raises(ValueError, match="schema version is unsupported"):
        _decode_finding_payload(payload)


@pytest.mark.covers("ADR-059")
def test_processor_derives_step_anchored_and_turn_level_findings() -> None:
    record = _record()

    findings = derive_findings(record)

    assert [(item.kind.value, item.step_ordinal) for item in findings] == [
        ("finalizer_repair", 2),
    ]
    assert derive_findings(record) == findings
    unselected = replace(record, trajectory=(_governed(1, selected=False), _accepted(2)))
    assert [(item.kind.value, item.step_ordinal) for item in derive_findings(unselected)] == [
        ("unselected_governed_result", 1),
    ]
    refused = replace(
        record,
        operation_outcome=AuditOperationOutcomeV2.REFUSED,
        publication_outcome=AuditPublicationOutcomeV2.REFUSED,
        terminal_outcome=AuditTerminalOutcomeV2.GOVERNED_REFUSAL,
        public_block_kinds=(),
        public_block_count=0,
        trajectory=(_governed(1), _accepted(2)),
    )
    assert [(item.kind.value, item.step_ordinal) for item in derive_findings(refused)] == [
        ("governed_refusal", None),
    ]
    metric = aggregate_metric(record)
    assert metric.value == 1
    assert metric.metric_id == aggregate_metric(record).metric_id


def test_processor_finding_identities_differ_per_anchored_step() -> None:
    record = replace(_record(), trajectory=(_governed(1), _rejected(2), _rejected(3), _accepted(4)))

    findings = derive_findings(record)

    assert [item.step_ordinal for item in findings] == [2, 3]
    assert len({item.finding_id for item in findings}) == 2


class _ProcessorContext:
    function_name = "agent-quality-processor"
    function_version = "$LATEST"
    invoked_function_arn = "arn:aws:lambda:us-east-1:111122223333:function:processor"
    memory_limit_in_mb = 512
    aws_request_id = "request-1"


class _ProcessorAuditStore:
    """The receipts table: the processor only reads the receipt here."""

    def __init__(self, record: ConversationAuditRecordV6 | Exception) -> None:
        self.record = record

    def get(self, audit_id: str) -> ConversationAuditRecordV6 | None:
        assert audit_id == "audit-01"
        if isinstance(self.record, Exception):
            raise self.record
        return self.record


class _ProcessorFindingStore:
    """The findings table, whose only writer is the processor (ADR-080 §1)."""

    def __init__(self) -> None:
        self.findings: list[object] = []
        self.receipt_times: list[str] = []

    def write_finding(self, finding: object, receipt_created_at: str) -> None:
        self.findings.append(finding)
        self.receipt_times.append(receipt_created_at)


class _ProcessorAggregateStore:
    def __init__(self) -> None:
        self.metrics: list[object] = []

    def write_metric(self, metric: object) -> None:
        self.metrics.append(metric)


def _stream_record() -> dict[str, object]:
    return {
        "eventID": "stream-event-1",
        "dynamodb": {
            "Keys": {
                AUDIT_ID: {"S": "audit-01"},
                ITEM_KEY: {"S": RECORD},
            }
        },
    }


def test_processor_attests_key_only_record_and_logs_only_bounded_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audit_store = _ProcessorAuditStore(_record())
    finding_store = _ProcessorFindingStore()
    aggregate_store = _ProcessorAggregateStore()
    lifecycle: list[dict[str, str | int | bool]] = []
    traced = MagicMock()
    process_span = MagicMock()
    traced.return_value.__enter__.return_value = process_span

    def capture_lifecycle(event: AuditProcessorLog, _logger: Logger) -> None:
        lifecycle.append(event.projection)

    monkeypatch.setattr(processor_handler, "_AUDIT_STORE", audit_store)
    monkeypatch.setattr(processor_handler, "_FINDINGS_STORE", finding_store)
    monkeypatch.setattr(processor_handler, "_AGGREGATE_STORE", aggregate_store)
    monkeypatch.setattr(processor_handler, "report_lifecycle", capture_lifecycle)
    monkeypatch.setattr(processor_handler, "span", traced)

    result = processor_handler.handler({"Records": [_stream_record()]}, _ProcessorContext())

    assert result == {"batchItemFailures": []}
    assert vars(processor_handler.handler)["__trace_boundaries__"] == (
        processor_handler.AGENT_QUALITY_PROCESS,
    )
    traced.assert_called_once_with(
        processor_handler.AGENT_QUALITY_PROCESS,
        operation="agent_quality.processor",
    )
    process_span.record.assert_called_once_with(
        outcome="completed",
        record_count=1,
        processed_count=1,
        failure_count=0,
    )
    assert len(finding_store.findings) == 1
    # ADR-081 §5: a finding expires with its receipt, never from its own creation time.
    assert finding_store.receipt_times == [_record().created_at]
    assert len(aggregate_store.metrics) == 1
    assert lifecycle == [
        {
            "operation": "agent_quality.processor",
            "component": "audit_processor",
            "phase": "processing",
            "outcome": "processed",
            "audit_id": "audit-01",
            "finding_count": 1,
            "aggregate_written": True,
            "correlation_id": _record().correlation_id,
            "investigation_next": "inspect_terminal_audit",
        }
    ]


def test_processor_reports_dependency_before_partial_batch_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = DependencyError(ErrorCode("AGENT_QUALITY", 1), "Receipt read failed.")
    audit_store = _ProcessorAuditStore(error)
    reported: list[tuple[BaseException, Report]] = []
    traced = MagicMock()
    process_span = MagicMock()
    traced.return_value.__enter__.return_value = process_span

    def capture(captured: BaseException, report: Report) -> None:
        reported.append((captured, report))

    monkeypatch.setattr(processor_handler, "_AUDIT_STORE", audit_store)
    monkeypatch.setattr(processor_handler, "report_error", capture)
    monkeypatch.setattr(processor_handler, "span", traced)

    result = processor_handler.handler({"Records": [_stream_record()]}, _ProcessorContext())

    assert result == {"batchItemFailures": [{"itemIdentifier": "stream-event-1"}]}
    assert len(reported) == 1
    captured, report = reported[0]
    assert captured is error
    assert report.operation == "agent_quality.processor"
    assert report.correlation_id == ""
    process_span.note_error.assert_called_once_with(error)
    process_span.record.assert_called_once_with(
        outcome="retry",
        record_count=1,
        processed_count=0,
        failure_count=1,
    )


def test_correlation_request_refuses_non_normalized_values() -> None:
    AuditCorrelationRequest(correlation_id="123e4567-e89b-42d3-a456-426614174000")
    with pytest.raises(ValueError, match="normalized"):
        AuditCorrelationRequest(correlation_id="A" * 36)


_RECEIPT_EXPIRES_AT = 1_796_126_400
"""A fictional stored expiry in whole epoch seconds; the review API reads it, never computes it."""
_DELIVERY_EXPIRES_AT = _RECEIPT_EXPIRES_AT
_FINDING_EXPIRES_AT = _RECEIPT_EXPIRES_AT - 1


class _ReviewStore:
    """Stands in for the receipts and findings tables, returning stored expiries as given."""

    def __init__(
        self,
        delivery: TurnDeliveryRecordV1 | None,
        *,
        record: ConversationAuditRecordV6 | None = None,
    ) -> None:
        self.delivery = delivery
        self.record = record
        self.pages: list[ReceiptTurnsPageRequest] = []

    def get_delivery_with_expiry(self, audit_id: str) -> StoredRecord[TurnDeliveryRecordV1] | None:
        assert audit_id == "audit-01"
        if self.delivery is None:
            return None
        return StoredRecord(record=self.delivery, expires_at=_DELIVERY_EXPIRES_AT)

    def get_findings_with_expiry(self, audit_id: str) -> tuple[StoredRecord[AuditFindingV3], ...]:
        assert audit_id == "audit-01"
        return ()

    def get_with_expiry(self, audit_id: str) -> StoredRecord[ConversationAuditRecordV6] | None:
        if self.record is None or audit_id != self.record.audit_id:
            return None
        return StoredRecord(record=self.record, expires_at=_RECEIPT_EXPIRES_AT)

    def discover(self, request: AuditCorrelationRequest) -> tuple[ConversationAuditRecordV6, ...]:
        if self.record is None or request.correlation_id != self.record.correlation_id:
            return ()
        return (self.record,)

    def list_turns(self, request: ReceiptTurnsPageRequest) -> ReceiptTurnsPage:
        self.pages.append(request)
        return ReceiptTurnsPage(
            turns=(
                ReceiptTurnRow(
                    audit_id="audit-01",
                    correlation_id=_CORRELATION_ID,
                    created_at="2026-09-02T12:00:00+00:00",
                    terminal_outcome=AuditTerminalOutcomeV2.COMPLETE,
                    expires_at=_RECEIPT_EXPIRES_AT,
                ),
            ),
            cursor="opaque-next",
        )


_RESULT_DEADLINE_SECONDS = 3600


def _quality_review_module(
    monkeypatch: pytest.MonkeyPatch, *, result_deadline: bool = True, evaluation: bool = True
) -> Any:
    for key, value in {
        "ADL_ENVIRONMENT": "test",
        "ADL_AWS_REGION": "us-east-1",
        "ADL_COGNITO_USER_POOL_ID": "unused-quality-review",
        "ADL_COGNITO_CLIENT_ID": "unused-quality-review",
        "ADL_COGNITO_LOGIN_DOMAIN": "auth.test.invalid",
        "ADL_APP_BASE_URL": "https://app.test.invalid",
        "ADL_API_BASE_URL": "https://api.test.invalid",
        "ADL_COOKIE_SCOPE": "host_only",
        "ADL_REFRESH_COOKIE_MAX_AGE_SECONDS": "2592000",
        "ADL_ATHENA_WORKGROUP": "unused-quality-review",
        "ADL_SEMANTIC_ATTEMPTS_TABLE": "unused-quality-review",
        "ADL_SEMANTIC_LEASES_TABLE": "unused-quality-review",
        "ADL_SEMANTIC_PUBLICATIONS_TABLE": "unused-quality-review",
        "ADL_ATHENA_RESULT_REUSE_MAX_AGE_MINUTES": "0",
        "ADL_ATHENA_QUERY_BATCH_MAX_CONCURRENCY": "8",
        "ADL_AGENT_QUALITY_AUDIT_TABLE": "agent-quality-audit-test",
        "ADL_AGENT_QUALITY_FINDINGS_TABLE": "agent-quality-findings-test",
        "ADL_EVALUATION_DELIVERIES_TABLE": "evaluation-deliveries-test",
        "ADL_EVALUATION_WRITER_RECEIPTS_TABLE": "evaluation-writer-receipts-test",
        "ADL_EVALUATION_VERDICTS_TABLE": "evaluation-verdicts-test",
        "ADL_EVALUATION_RESULT_DEADLINE_SECONDS": str(_RESULT_DEADLINE_SECONDS),
    }.items():
        monkeypatch.setenv(key, value)
    if not result_deadline:
        monkeypatch.delenv("ADL_EVALUATION_RESULT_DEADLINE_SECONDS")
    if not evaluation:
        for key in (
            "ADL_EVALUATION_DELIVERIES_TABLE",
            "ADL_EVALUATION_WRITER_RECEIPTS_TABLE",
            "ADL_EVALUATION_VERDICTS_TABLE",
            "ADL_EVALUATION_RESULT_DEADLINE_SECONDS",
        ):
            monkeypatch.delenv(key, raising=False)
    _clear_quality_review_caches()
    sys.modules.pop("adapters.inbound.quality_review.handler", None)
    return cast("Any", importlib.import_module("adapters.inbound.quality_review.handler"))


def _clear_quality_review_caches() -> None:
    settings_without_client_secret.cache_clear()
    quality_review_settings.cache_clear()
    tracing_settings.cache_clear()


def _unload_quality_review() -> None:
    sys.modules.pop("adapters.inbound.quality_review.handler", None)
    _clear_quality_review_caches()


_REVIEWER = frozenset({ReviewAccessLevel.QUALITY_REVIEWER})
_ATTRIBUTOR = frozenset({ReviewAccessLevel.QUALITY_REVIEWER, ReviewAccessLevel.AUDIT_ATTRIBUTION})


def _stored(record: ConversationAuditRecordV6) -> StoredRecord[ConversationAuditRecordV6]:
    return StoredRecord(record=record, expires_at=_RECEIPT_EXPIRES_AT)


def _review(
    monkeypatch: pytest.MonkeyPatch, levels: frozenset[ReviewAccessLevel]
) -> dict[str, Any]:
    try:
        module = _quality_review_module(monkeypatch)
        monkeypatch.setattr(module, "STORE", _ReviewStore(None))
        monkeypatch.setattr(module, "FINDINGS", _ReviewStore(None))
        return cast("dict[str, Any]", module._turn_review(_stored(_record()), levels))
    finally:
        _unload_quality_review()


@pytest.mark.covers("ADR-059", "ADR-077", "ADR-081")
def test_no_reviewer_section_exposes_the_principal(monkeypatch: pytest.MonkeyPatch) -> None:
    """The principal attributes the turn; a reviewer reads what happened, never who asked."""
    review = _review(monkeypatch, _REVIEWER)
    assert set(review) == {"schema_version", "audit", "findings", "evaluation"}
    assert review["schema_version"] == "QualityTurnReview.v1"
    assert _PRINCIPAL not in json.dumps(review)


@pytest.mark.covers("ADR-078", "ADR-081")
def test_attribution_is_returned_only_to_a_caller_holding_both_levels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    review = _review(monkeypatch, _ATTRIBUTOR)
    assert review["attribution"] == {"principal": _PRINCIPAL, "expires_at": _RECEIPT_EXPIRES_AT}
    for section in ("audit", "findings", "evaluation"):
        assert _PRINCIPAL not in json.dumps(review[section])


@pytest.mark.covers("ADR-078")
def test_the_section_recheck_returns_nothing_without_the_reviewer_level(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    review = _review(monkeypatch, frozenset({ReviewAccessLevel.AUDIT_ATTRIBUTION}))
    assert review == {"schema_version": "QualityTurnReview.v1"}


@pytest.mark.covers("ADR-060", "ADR-062", "ADR-063", "ADR-081")
def test_quality_review_audit_projects_identity_provenance_trajectory_and_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delivery = TurnDeliveryRecordV1(
        audit_id="audit-01",
        disposition=TurnDeliveryDispositionV1.DELIVERED,
        emitted_block_count=1,
        recorded_at="2026-09-02T12:00:03+00:00",
    )
    try:
        module = _quality_review_module(monkeypatch)
        monkeypatch.setattr(module, "STORE", _ReviewStore(delivery))
        projection = cast("dict[str, Any]", module._audit_projection(_stored(_record())))
    finally:
        _unload_quality_review()

    assert projection["schema_version"] == "QualityAuditReview.v3"
    assert projection["expires_at"] == _RECEIPT_EXPIRES_AT
    assert vars(module.handler)["__trace_boundaries__"] == (module.QUALITY_REVIEW_REQUEST,)
    assert projection["identity"] == {
        "audit_id": "audit-01",
        "correlation_id": _CORRELATION_ID,
        "trace_id": _TRACE_ID,
        "created_at": "2026-09-02T12:00:00+00:00",
    }
    assert projection["provenance"] == {
        "runtime_version": MANAGED_HARNESS_RUNTIME_V3,
        "prompt_catalog_digest": _DIGEST,
        "guardrail_version": "3",
        "page_schema_version": "presentation.v2",
        "response_contract_version": "public-block-stream.v3",
    }
    assert projection["outcome"]["terminal_outcome"] == "complete"
    assert projection["outcome"]["failure_error_code"] is None
    assert [step["disposition"] for step in projection["trajectory"]] == [
        "succeeded",
        "rejected",
        "accepted",
    ]
    assert projection["trajectory"][0]["step_ref"] == _RESULT_REF
    assert projection["trajectory"][0]["selected_into_plan"] is True
    assert projection["trajectory"][1]["violations"] == ["unknown_rendering"]
    assert projection["publication"]["public_block_kinds"] == ["paragraph"]
    assert projection["delivery"] == {
        "disposition": "delivered",
        "emitted_block_count": 1,
        "recorded_at": "2026-09-02T12:00:03+00:00",
        "expires_at": _DELIVERY_EXPIRES_AT,
    }
    assert {"prompt", "question", "answer", "page_context", "destination_ref"}.isdisjoint(
        json.dumps(projection)
    )


@pytest.mark.covers("ADR-063")
def test_quality_review_audit_reports_an_unwritten_delivery_as_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    try:
        module = _quality_review_module(monkeypatch)
        monkeypatch.setattr(module, "STORE", _ReviewStore(None))
        projection = cast("dict[str, Any]", module._audit_projection(_stored(_record())))
    finally:
        _unload_quality_review()

    assert projection["delivery"] is None


_EVALUATOR_PROMPT_DIGEST = "b" * 64


def _released(evaluator_ref: str) -> EvaluationReleaseEvaluatorV1:
    return EvaluationReleaseEvaluatorV1(
        evaluator_id=evaluator_ref,
        evaluator_name=f"fictional_{evaluator_ref}",
        revision="r2",
        model_id="fictional.judge-model-v1:0",
        prompt_digest=_EVALUATOR_PROMPT_DIGEST,
    )


def _result_v3(evaluator_ref: str, label: str, score: float) -> EvaluationResultV3:
    released = _released(evaluator_ref)
    return EvaluationResultV3(
        audit_id="audit-01",
        admission_ref="admission-01",
        evaluator_ref=evaluator_ref,
        evaluator_name=released.evaluator_name,
        result_ref=f"result-{evaluator_ref}",
        state=EvaluationResultStateV1.COMPLETED,
        label=label,
        score=score,
        evaluated_at="2026-09-02T12:05:00+00:00",
        family=EvaluationFamilyV1.V4,
        config_id="fictional_released_v4-0001",
        evaluator_revision=released.revision,
        model_id=released.model_id,
        prompt_digest=released.prompt_digest,
    )


@pytest.mark.covers("ADR-059")
def test_evaluation_results_are_keyed_per_evaluator_and_read_back_together() -> None:
    """Four judges score one admission; a per-admission key made the second one a conflict."""
    grounded = _result_v3("grounded-01", "partially_grounded", 0.5)
    helpful = _result_v3("helpful-01", "good", 0.75)

    items = {
        _result_item_key("admission-01", item.evaluator_ref): _linkage_item(
            _LinkageItemRequest(
                audit_id="audit-01",
                item_key=_result_item_key("admission-01", item.evaluator_ref),
                record_type="EVALUATION_RESULT",
                value=item,
            )
        )
        for item in (grounded, helpful)
    }

    assert set(items) == {
        "EVALUATION_RESULT#admission-01#grounded-01",
        "EVALUATION_RESULT#admission-01#helpful-01",
    }
    decoded = [_decode_result(item, "audit-01", "admission-01") for item in items.values()]
    assert decoded == [grounded, helpful]


@pytest.mark.covers("ADR-080")
def test_a_v1_result_under_its_admission_only_key_is_refused_by_the_verdicts_decoder() -> None:
    """Legacy V1 results stayed in the receipts table, which is never read for verdicts."""
    document = {
        "schema_version": "EvaluationResult.v1",
        "audit_id": "audit-01",
        "admission_ref": "admission-01",
        "evaluator_ref": "released-evaluator-01",
        "result_ref": "result-01",
        "state": "completed",
        "label": "good",
        "score": 0.75,
        "evaluated_at": "2026-09-02T12:05:00+00:00",
    }
    payload = json.dumps(document, separators=(",", ":"), sort_keys=True)
    item = {
        AUDIT_ID: {"S": "audit-01"},
        ITEM_KEY: {"S": "EVALUATION_RESULT#admission-01"},
        "record_type": {"S": "EVALUATION_RESULT"},
        PAYLOAD: {"S": payload},
        PAYLOAD_DIGEST: {"S": _digest(payload)},
    }

    with pytest.raises(ValueError, match="schema version is unsupported"):
        _decode_result(cast("Any", item), "audit-01", "admission-01")


class _AdmissionReviewStore:
    """The receipts table: only the admission of the evaluation chain lives here."""

    def get_evaluation_admission_with_expiry(
        self, _audit_id: str, _admission_ref: str
    ) -> StoredRecord[object]:
        return StoredRecord(
            record=SimpleNamespace(selected_at="2026-09-02T12:00:00+00:00"),
            expires_at=_RECEIPT_EXPIRES_AT,
        )


_Receipt = EvaluationDeliveryReceiptV2 | EvaluationDeliveryPredatesGate | None


def _receipt(
    reason: EvaluationNotDispatchedReasonV1 | None = None,
) -> EvaluationDeliveryReceiptV2:
    return EvaluationDeliveryReceiptV2(
        audit_id="audit-01",
        admission_ref="admission-01",
        delivery_ref=evaluation_delivery_ref("audit-01", "admission-01"),
        decided_at="2026-09-02T12:00:01+00:00",
        outcome=(
            EvaluationDeliveryOutcomeV1.DISPATCHED
            if reason is None
            else EvaluationDeliveryOutcomeV1.NOT_DISPATCHED
        ),
        not_dispatched_reason=reason,
        families=(
            ()
            if reason is not None
            else (
                EvaluationDeliveryFamilyV1(
                    family=EvaluationFamilyV1.V4,
                    config_id="fictional_released_v4-0001",
                    evaluators=(_released("grounded-01"), _released("helpful-01")),
                ),
            )
        ),
    )


class _EvaluationReviewStore:
    """Stands in for each of the three evaluation record tables in turn."""

    def __init__(
        self,
        results: tuple[EvaluationResultV3, ...],
        *,
        receipt: _Receipt,
        writer: bool = True,
    ) -> None:
        self.results = results
        self.receipt = receipt
        self.writer = writer

    def get_evaluation_delivery_with_expiry(
        self, _audit_id: str, _admission_ref: str
    ) -> StoredRecord[object] | None:
        if self.receipt is None:
            return None
        return StoredRecord(record=self.receipt, expires_at=_RECEIPT_EXPIRES_AT - 2)

    def get_evaluation_writer_receipt_with_expiry(
        self, _audit_id: str, _admission_ref: str
    ) -> StoredRecord[object] | None:
        if not self.writer:
            return None
        return StoredRecord(
            record=SimpleNamespace(emitted_at="2026-09-02T12:00:02+00:00", trace_id=_TRACE_ID),
            expires_at=_RECEIPT_EXPIRES_AT - 3,
        )

    def get_evaluation_results_with_expiry(
        self, _audit_id: str, _admission_ref: str
    ) -> tuple[StoredRecord[EvaluationResultV3], ...]:
        return tuple(
            StoredRecord(record=result, expires_at=_RECEIPT_EXPIRES_AT - 4)
            for result in self.results
        )


def _evaluation_stores(
    results: tuple[EvaluationResultV3, ...] = (),
    *,
    receipt: _Receipt = None,
    writer: bool = True,
) -> SimpleNamespace:
    store = _EvaluationReviewStore(
        results, receipt=_receipt() if receipt is None else receipt, writer=writer
    )
    return SimpleNamespace(deliveries=store, writer_receipts=store, verdicts=store)


_RECEIPT_DECIDED_AT = datetime(2026, 9, 2, 12, 0, 1, tzinfo=UTC)
"""The fictional dispatched receipt's decision time; the result deadline counts from it."""


def _project_evaluation(
    monkeypatch: pytest.MonkeyPatch,
    stores: SimpleNamespace | None,
    *,
    now: datetime = _RECEIPT_DECIDED_AT,
) -> dict[str, Any]:
    record = replace(_record(), evaluation_admission_ref="admission-01")
    try:
        module = _quality_review_module(monkeypatch)
        monkeypatch.setattr(module, "STORE", _AdmissionReviewStore())
        monkeypatch.setattr(module, "EVALUATION", stores)
        monkeypatch.setattr(module, "_now", lambda: now)
        return cast("dict[str, Any]", module._evaluation_projection(record))
    finally:
        _unload_quality_review()


@pytest.mark.covers("ADR-059", "ADR-061", "ADR-079")
def test_quality_review_evaluation_projects_turn_outcome_chain_and_one_result_per_evaluator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    results = (
        _result_v3("grounded-01", "partially_grounded", 0.5),
        _result_v3("helpful-01", "good", 0.75),
    )

    projection = _project_evaluation(monkeypatch, _evaluation_stores(results))
    pending = _project_evaluation(monkeypatch, _evaluation_stores())

    assert projection["schema_version"] == "QualityEvaluationReview.v3"
    assert projection["turn_outcome"] == {
        "terminal_outcome": "complete",
        "publication_outcome": "published",
        "public_block_count": 1,
        "released_character_count": 0,
    }
    assert (projection["state"], projection["reason"]) == ("results_available", None)
    chain = projection["chain"]
    assert chain["input_trace_id"] == _TRACE_ID
    assert (chain["delivery_outcome"], chain["delivered_to_queue"]) == (
        "dispatched",
        "2026-09-02T12:00:01+00:00",
    )
    assert chain["families"] == [
        {
            "family": "v4",
            "config_id": "fictional_released_v4-0001",
            "evaluators": [
                {
                    "evaluator_id": evaluator_id,
                    "evaluator_name": f"fictional_{evaluator_id}",
                    "revision": "r2",
                    "model_id": "fictional.judge-model-v1:0",
                    "prompt_digest": _EVALUATOR_PROMPT_DIGEST,
                }
                for evaluator_id in ("grounded-01", "helpful-01")
            ],
        }
    ]
    # Each score is on its evaluator's own declared scale, per evaluator per admitted turn.
    assert [
        (item["evaluator_ref"], item["label"], item["score"]) for item in projection["results"]
    ] == [
        ("grounded-01", "partially_grounded", 0.5),
        ("helpful-01", "good", 0.75),
    ]
    assert {
        (
            item["family"],
            item["config_id"],
            item["evaluator_revision"],
            item["model_id"],
            item["prompt_digest"],
        )
        for item in projection["results"]
    } == {("v4", "fictional_released_v4-0001", "r2", "fictional.judge-model-v1:0", "b" * 64)}
    assert chain["expires_at"] == {
        "admission": _RECEIPT_EXPIRES_AT,
        "delivery": _RECEIPT_EXPIRES_AT - 2,
        "writer": _RECEIPT_EXPIRES_AT - 3,
    }
    assert {item["expires_at"] for item in projection["results"]} == {_RECEIPT_EXPIRES_AT - 4}
    assert (pending["state"], pending["reason"]) == ("pending_evaluators", None)
    assert pending["results"] == []


@pytest.mark.covers("ADR-059", "ADR-079")
@pytest.mark.parametrize(
    ("receipt", "expected"),
    [
        (None, ("pending_delivery", None, None)),
        ("dispatched", ("pending_writer", None, "dispatched")),
        (
            EvaluationDeliveryPredatesGate(audit_id="audit-01", admission_ref="admission-01"),
            ("not_selected", "predates_evaluation_gate", None),
        ),
        *(
            (reason, ("not_selected", reason.value, "not_dispatched"))
            for reason in EvaluationNotDispatchedReasonV1
        ),
    ],
    ids=[
        "no-receipt",
        "dispatched-no-writer",
        "predates-gate",
        *(f"withheld-{reason.value}" for reason in EvaluationNotDispatchedReasonV1),
    ],
)
def test_quality_review_evaluation_state_follows_the_delivery_receipt(
    monkeypatch: pytest.MonkeyPatch,
    receipt: object,
    expected: tuple[str, str | None, str | None],
) -> None:
    """A turn the dispatcher did not send is not selected, never reported as pending."""
    if receipt is None:
        stored: _Receipt = None
    elif receipt == "dispatched":
        stored = _receipt()
    elif isinstance(receipt, EvaluationNotDispatchedReasonV1):
        stored = _receipt(receipt)
    else:
        stored = cast("EvaluationDeliveryPredatesGate", receipt)
    store = _EvaluationReviewStore((), receipt=stored, writer=False)
    stores = SimpleNamespace(deliveries=store, writer_receipts=store, verdicts=store)

    projection = _project_evaluation(monkeypatch, stores)

    state, reason, outcome = expected
    assert (projection["state"], projection["reason"]) == (state, reason)
    assert projection["chain"]["delivery_outcome"] == outcome
    if outcome != "dispatched":
        assert projection["chain"]["families"] == []
        assert projection["chain"]["delivered_to_queue"] is None


_AT_DEADLINE = _RECEIPT_DECIDED_AT + timedelta(seconds=_RESULT_DEADLINE_SECONDS)


@pytest.mark.covers("ADR-059", "ADR-079")
@pytest.mark.parametrize(
    ("results", "writer", "now", "expected"),
    [
        (("grounded-01", "helpful-01"), True, _AT_DEADLINE, ("results_available", 2)),
        (("grounded-01",), True, _RECEIPT_DECIDED_AT, ("partial", 1)),
        (("grounded-01",), True, _AT_DEADLINE + timedelta(days=1), ("partial", 1)),
        ((), False, _AT_DEADLINE + timedelta(days=1), ("pending_writer", 0)),
        ((), True, _AT_DEADLINE, ("pending_evaluators", 0)),
        ((), True, _AT_DEADLINE + timedelta(seconds=1), ("overdue", 0)),
        (("other-evaluator-01",), True, _RECEIPT_DECIDED_AT, ("pending_evaluators", 0)),
    ],
    ids=[
        "every-evaluator",
        "one-of-two",
        "one-of-two-past-deadline",
        "no-writer",
        "none-at-deadline",
        "none-past-deadline",
        "only-an-undispatched-evaluator",
    ],
)
def test_quality_review_evaluation_states_are_honest_about_how_many_evaluators_reported(  # noqa: PLR0913, PLR0917
    monkeypatch: pytest.MonkeyPatch,
    results: tuple[str, ...],
    writer: bool,
    now: datetime,
    expected: tuple[str, int],
) -> None:
    """One of two verdicts is partial, and a turn nobody judged becomes overdue, not healthy."""
    stored = tuple(_result_v3(ref, "good", 0.75) for ref in results)

    projection = _project_evaluation(
        monkeypatch, _evaluation_stores(stored, writer=writer), now=now
    )

    state, reported = expected
    assert (projection["state"], projection["reason"]) == (state, None)
    assert projection["expected_evaluator_count"] == 2
    assert projection["reported_evaluator_count"] == reported


@pytest.mark.covers("ADR-059", "ADR-079")
def test_quality_review_evaluation_counts_evaluators_across_every_dispatched_family(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = _receipt()
    # A receipt written while the retired V3 family ran names it beside V4 (ADR-079); its
    # evaluators still count toward the turn's expected set until the receipt expires.
    v3 = EvaluationDeliveryFamilyV1(
        family=EvaluationFamilyV1.V3,
        config_id="fictional_released_v3-0001",
        evaluators=(_released("quality-01"),),
    )
    both = replace(receipt, families=(*receipt.families, v3))
    stored = tuple(_result_v3(ref, "good", 0.75) for ref in ("grounded-01", "helpful-01"))

    projection = _project_evaluation(monkeypatch, _evaluation_stores(stored, receipt=both))

    assert projection["state"] == "partial"
    assert (projection["expected_evaluator_count"], projection["reported_evaluator_count"]) == (
        3,
        2,
    )


@pytest.mark.covers("ADR-059", "ADR-079")
def test_the_review_function_refuses_to_start_without_a_result_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a deadline an unjudged turn would read as pending forever, so it fails closed."""
    try:
        with pytest.raises(InvariantError) as caught:
            _quality_review_module(monkeypatch, result_deadline=False)
    finally:
        _unload_quality_review()
    assert caught.value.context["problems"] == [
        "ADL_EVALUATION_RESULT_DEADLINE_SECONDS is required by the quality review API"
    ]


@pytest.mark.covers("ADR-059", "ADR-080")
def test_an_admitted_turn_is_unavailable_where_the_evaluation_plane_is_not_provisioned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An admitted turn whose evaluation records cannot be read is never reported as pending."""
    record = replace(_record(), evaluation_admission_ref="admission-01")
    try:
        module = _quality_review_module(monkeypatch, evaluation=False)
        assert (module.EVALUATION, module.RESULT_DEADLINE) == (None, None)
        monkeypatch.setattr(module, "STORE", _AdmissionReviewStore())
        with pytest.raises(InvariantError, match="not configured"):
            module._evaluation_projection(record)
        not_selected = cast("dict[str, Any]", module._evaluation_projection(_record()))
    finally:
        _unload_quality_review()
    assert (not_selected["state"], not_selected["reason"]) == ("not_selected", "not_admitted")
    assert (not_selected["chain"], not_selected["results"]) == (None, [])


class _FindingsReviewStore:
    def __init__(self, findings: tuple[AuditFindingV3, ...]) -> None:
        self.findings = findings

    def get_findings_with_expiry(self, _audit_id: str) -> tuple[StoredRecord[AuditFindingV3], ...]:
        return tuple(
            StoredRecord(record=finding, expires_at=_FINDING_EXPIRES_AT)
            for finding in self.findings
        )


@pytest.mark.covers("ADR-059", "ADR-062")
def test_quality_review_findings_anchor_each_defect_to_its_trajectory_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _record()
    findings = derive_findings(record)
    try:
        module = _quality_review_module(monkeypatch)
        monkeypatch.setattr(module, "FINDINGS", _FindingsReviewStore(findings))
        projection = cast("dict[str, Any]", module._findings_projection(record))
    finally:
        _unload_quality_review()

    assert projection["schema_version"] == "QualityFindingsReview.v3"
    assert projection["terminal_outcome"] == "complete"
    assert projection["provenance"] == {
        "runtime_version": MANAGED_HARNESS_RUNTIME_V3,
        "prompt_catalog_digest": _DIGEST,
        "guardrail_version": "3",
    }
    assert projection["findings"] == [
        {
            "kind": "finalizer_repair",
            "severity": "info",
            "created_at": "2026-09-02T12:00:00+00:00",
            "expires_at": _FINDING_EXPIRES_AT,
            "step": {
                "ordinal": 2,
                "tool_name": "presentation_publish",
                "disposition": "rejected",
                "violations": ["unknown_rendering"],
                "step_ref": "finalizer-message-0",
            },
        }
    ]


def _event(
    route_key: str | None,
    body: object,
    *,
    levels: str | None = "quality_reviewer",
) -> dict[str, Any]:
    authorizer: dict[str, object] = {"subject": _PRINCIPAL}
    if levels is not None:
        authorizer["review_levels"] = levels
    event: dict[str, Any] = {
        "body": body if isinstance(body, str) else json.dumps(body),
        "requestContext": {"authorizer": {"lambda": authorizer}},
    }
    if route_key is not None:
        event["routeKey"] = route_key
    return event


_TURN = "POST /api/quality-review/turn"
_TURNS = "POST /api/quality-review/turns"
_WINDOW = {"from": "2026-09-01T00:00:00+00:00", "to": "2026-09-08T00:00:00+00:00"}


def _serve(
    monkeypatch: pytest.MonkeyPatch, event: dict[str, Any], store: _ReviewStore | None = None
) -> tuple[int, dict[str, Any]]:
    try:
        module = _quality_review_module(monkeypatch)
        reviewed = store or _ReviewStore(None, record=_record())
        monkeypatch.setattr(module, "STORE", reviewed)
        monkeypatch.setattr(module, "FINDINGS", reviewed)
        monkeypatch.setattr(module, "report_operation", MagicMock())
        response = inspect.unwrap(module.handler)(event, object())
    finally:
        _unload_quality_review()
    return response["statusCode"], cast("dict[str, Any]", json.loads(response["body"]))


@pytest.mark.covers("ADR-078", "ADR-081")
@pytest.mark.parametrize(
    ("levels", "sections"),
    [
        ("quality_reviewer", {"audit", "findings", "evaluation"}),
        ("audit_attribution,quality_reviewer", {"audit", "findings", "evaluation", "attribution"}),
    ],
)
def test_turn_returns_the_sections_the_verified_levels_allow(
    monkeypatch: pytest.MonkeyPatch, levels: str, sections: set[str]
) -> None:
    status, body = _serve(
        monkeypatch, _event(_TURN, {"selector": {"audit_id": "audit-01"}}, levels=levels)
    )
    assert status == 200
    assert body["schema_version"] == "QualityTurnReview.v1"
    assert set(body) - {"schema_version"} == sections
    assert body["audit"]["expires_at"] == _RECEIPT_EXPIRES_AT


@pytest.mark.covers("ADR-078")
def test_turn_resolves_a_unique_correlation_selector(monkeypatch: pytest.MonkeyPatch) -> None:
    status, body = _serve(
        monkeypatch, _event(_TURN, {"selector": {"correlation_id": _CORRELATION_ID}})
    )
    assert status == 200
    assert body["audit"]["identity"]["audit_id"] == "audit-01"


@pytest.mark.covers("ADR-078")
@pytest.mark.parametrize("route_key", [_TURN, _TURNS])
@pytest.mark.parametrize("levels", ["audit_attribution", "", "prompt_admin"])
def test_a_caller_without_the_reviewer_level_receives_the_non_disclosing_outcome(
    monkeypatch: pytest.MonkeyPatch, route_key: str, levels: str
) -> None:
    """`audit_attribution` alone must not learn whether a turn exists."""
    present = _serve(
        monkeypatch,
        _event(route_key, {"selector": {"audit_id": "audit-01"}}, levels=levels),
    )
    absent = _serve(
        monkeypatch,
        _event(_TURN, {"selector": {"audit_id": "absent-01"}}),
    )
    assert present == absent == (404, {"error": "not_found"})


@pytest.mark.covers("ADR-078")
@pytest.mark.parametrize(
    "event",
    [
        _event(_TURN, {"selector": {"audit_id": "audit-01", "correlation_id": _CORRELATION_ID}}),
        _event(_TURN, {"selector": {}}),
        _event(_TURN, "not json"),
    ],
    ids=["dual-selector", "empty-selector", "malformed-body"],
)
def test_turn_refuses_a_malformed_request(
    monkeypatch: pytest.MonkeyPatch, event: dict[str, Any]
) -> None:
    assert _serve(monkeypatch, event) == (400, {"error": "invalid_request"})


@pytest.mark.covers("ADR-078")
@pytest.mark.parametrize(
    "event",
    [
        _event(_TURN, {"selector": {"audit_id": "audit-01"}}, levels=None),
        _event(_TURN, {"selector": {"audit_id": "audit-01"}}, levels="unknown_level"),
    ],
    ids=["no-levels", "unknown-level"],
)
def test_missing_or_malformed_authorizer_context_is_a_fault_not_a_client_error(
    monkeypatch: pytest.MonkeyPatch, event: dict[str, Any]
) -> None:
    """Only a route detached from its authorizer reaches this, so it must page, not read as 400."""
    assert _serve(monkeypatch, event) == (503, {"error": "unavailable"})


@pytest.mark.covers("ADR-078")
def test_an_unknown_route_key_reaching_the_function_discloses_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = _event("POST /api/quality-review/audit", {"selector": {"audit_id": "audit-01"}})
    assert _serve(monkeypatch, event) == (404, {"error": "not_found"})


@pytest.mark.covers("ADR-078", "ADR-081")
def test_turns_returns_a_page_of_rows_without_the_principal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _ReviewStore(None, record=_record())
    status, body = _serve(
        monkeypatch,
        _event(
            _TURNS,
            {
                "window": _WINDOW,
                "filters": {"terminal_outcome": ["complete", "dependency_failure"]},
                "limit": 10,
                "cursor": "opaque-previous",
            },
        ),
        store,
    )
    assert status == 200
    assert body == {
        "schema_version": "QualityTurnsPage.v1",
        "turns": [
            {
                "audit_id": "audit-01",
                "correlation_id": _CORRELATION_ID,
                "created_at": "2026-09-02T12:00:00+00:00",
                "terminal_outcome": "complete",
                "expires_at": _RECEIPT_EXPIRES_AT,
            }
        ],
        "cursor": "opaque-next",
    }
    assert _PRINCIPAL not in json.dumps(body)
    (request,) = store.pages
    assert request.window_from == datetime(2026, 9, 1, tzinfo=UTC)
    assert request.window_to == datetime(2026, 9, 8, tzinfo=UTC)
    assert request.terminal_outcomes == {
        AuditTerminalOutcomeV2.COMPLETE,
        AuditTerminalOutcomeV2.DEPENDENCY_FAILURE,
    }
    assert (request.limit, request.cursor) == (10, "opaque-previous")


def test_turns_defaults_to_fifty_rows_and_no_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _ReviewStore(None, record=_record())
    _serve(monkeypatch, _event(_TURNS, {"window": _WINDOW}), store)
    (request,) = store.pages
    assert (request.limit, request.terminal_outcomes, request.cursor) == (50, frozenset(), None)


_OVER_31_DAYS = {"from": "2026-09-01T00:00:00+00:00", "to": "2026-10-02T00:00:01+00:00"}


@pytest.mark.covers("ADR-078")
@pytest.mark.parametrize(
    "body",
    [
        {},
        {"window": _WINDOW, "extra": 1},
        {"window": {"from": _WINDOW["from"]}},
        {"window": {**_WINDOW, "at": _WINDOW["to"]}},
        {"window": {"from": _WINDOW["to"], "to": _WINDOW["from"]}},
        {"window": {"from": _WINDOW["from"], "to": _WINDOW["from"]}},
        {"window": _OVER_31_DAYS},
        {"window": {"from": "2026-09-01T00:00:00", "to": _WINDOW["to"]}},
        {"window": {"from": "2026-09-01T02:00:00+02:00", "to": _WINDOW["to"]}},
        {"window": {"from": 0, "to": _WINDOW["to"]}},
        {"window": _WINDOW, "filters": {}},
        {"window": _WINDOW, "filters": {"terminal_outcome": []}},
        {"window": _WINDOW, "filters": {"terminal_outcome": ["complete", "complete"]}},
        {"window": _WINDOW, "filters": {"terminal_outcome": ["unknown"]}},
        {"window": _WINDOW, "filters": {"terminal_outcome": "complete"}},
        {"window": _WINDOW, "filters": {"terminal_outcome": ["complete"], "other": []}},
        {"window": _WINDOW, "limit": 0},
        {"window": _WINDOW, "limit": 101},
        {"window": _WINDOW, "limit": True},
        {"window": _WINDOW, "limit": 5.0},
        {"window": _WINDOW, "cursor": None},
        {"window": _WINDOW, "cursor": ""},
        {"selector": {"audit_id": "audit-01"}},
    ],
)
def test_turns_refuses_anything_but_its_exact_request_members(
    monkeypatch: pytest.MonkeyPatch, body: dict[str, object]
) -> None:
    assert _serve(monkeypatch, _event(_TURNS, body)) == (400, {"error": "invalid_request"})


def test_a_31_day_window_is_the_longest_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    window = {"from": "2026-09-01T00:00:00+00:00", "to": "2026-10-02T00:00:00+00:00"}
    status, _ = _serve(monkeypatch, _event(_TURNS, {"window": window}))
    assert status == 200


def test_quality_review_handler_root_span_records_safe_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    try:
        module = _quality_review_module(monkeypatch)
        traced = MagicMock()
        process_span = MagicMock()
        traced.return_value.__enter__.return_value = process_span
        monkeypatch.setattr(module, "span", traced)
        monkeypatch.setattr(module, "report_operation", MagicMock())
        response = inspect.unwrap(module.handler)(_event(_TURN, "not json"), object())
    finally:
        _unload_quality_review()

    assert response["statusCode"] == 400
    traced.assert_called_once_with(
        module.QUALITY_REVIEW_REQUEST,
        operation="quality_review.turn",
    )
    process_span.record.assert_called_once_with(outcome="refused", status=400)
