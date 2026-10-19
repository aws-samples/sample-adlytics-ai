# SPDX-License-Identifier: MIT-0
"""Pure durable evaluation outbox contracts and item projection coverage."""

from dataclasses import replace

import pytest

from adapters.outbound.dynamodb.evaluation_outbox import (
    ADMISSION_REF,
    ATTEMPT_COUNT,
    AUDIT_ID,
    DELIVERY_REF,
    DISPATCHABLE,
    PAYLOAD,
    READY_PARTITION,
    READY_SORT,
    STATE,
    _initial_item,  # pyright: ignore[reportPrivateUsage] - exact item projection seam
    _work_item,  # pyright: ignore[reportPrivateUsage] - exact item projection seam
)
from adapters.outbound.dynamodb.items import read_text
from domain.evaluation.envelope import EvaluationEnvelope
from domain.evaluation.outbox import (
    EvaluationOutboxDeliveryStateV1,
    EvaluationOutboxEnvelopeV1,
    EvaluationOutboxInitialRecordV1,
    EvaluationOutboxLeaseV1,
    EvaluationOutboxStateV1,
    evaluation_delivery_ref,
)

pytestmark = pytest.mark.unit

_AUDIT_ID = "audit-01"
_ADMISSION_REF = "admission-01"
_DELIVERY_REF = evaluation_delivery_ref(_AUDIT_ID, _ADMISSION_REF)
_ADMITTED_AT = "2026-09-02T12:00:00+00:00"


def _envelope() -> EvaluationOutboxEnvelopeV1:
    return EvaluationOutboxEnvelopeV1(
        audit_id=_AUDIT_ID,
        admission_ref=_ADMISSION_REF,
        delivery_ref=_DELIVERY_REF,
        envelope=EvaluationEnvelope(
            question="What is the fictional governed result?",
            released_answer="The fictional released answer.",
            correlation_id="123e4567-e89b-42d3-a456-426614174000",
            audit_id=_AUDIT_ID,
            evaluation_admission_ref=_ADMISSION_REF,
        ),
        admitted_at=_ADMITTED_AT,
    )


def _initial() -> EvaluationOutboxInitialRecordV1:
    return EvaluationOutboxInitialRecordV1(
        envelope=_envelope(),
        state=EvaluationOutboxStateV1(
            audit_id=_AUDIT_ID,
            admission_ref=_ADMISSION_REF,
            delivery_ref=_DELIVERY_REF,
            state=EvaluationOutboxDeliveryStateV1.PENDING,
            available_at=_ADMITTED_AT,
            attempt_count=0,
        ),
    )


def test_delivery_reference_is_deterministic_and_content_independent() -> None:
    assert evaluation_delivery_ref(_AUDIT_ID, _ADMISSION_REF) == _DELIVERY_REF
    assert evaluation_delivery_ref(_AUDIT_ID, "admission-02") != _DELIVERY_REF
    assert len(_DELIVERY_REF) <= 128


def test_initial_outbox_requires_matched_pending_custody_records() -> None:
    initial = _initial()
    assert initial.state.state is EvaluationOutboxDeliveryStateV1.PENDING
    with pytest.raises(ValueError, match="pending"):
        replace(
            initial,
            state=replace(
                initial.state,
                state=EvaluationOutboxDeliveryStateV1.DELIVERED,
                delivered_at="2026-09-02T12:01:00+00:00",
            ),
        )


def test_leased_and_delivered_state_shapes_are_closed() -> None:
    lease = EvaluationOutboxLeaseV1(
        audit_id=_AUDIT_ID,
        admission_ref=_ADMISSION_REF,
        delivery_ref=_DELIVERY_REF,
        lease_ref="lease-01",
        acquired_at="2026-09-02T12:00:00+00:00",
        expires_at="2026-09-02T12:05:00+00:00",
        attempt=1,
    )
    leased = EvaluationOutboxStateV1(
        audit_id=_AUDIT_ID,
        admission_ref=_ADMISSION_REF,
        delivery_ref=_DELIVERY_REF,
        state=EvaluationOutboxDeliveryStateV1.LEASED,
        available_at="2026-09-02T12:05:00+00:00",
        attempt_count=1,
        lease=lease,
    )
    assert leased.lease == lease
    with pytest.raises(ValueError, match="requires only a delivery time"):
        replace(leased, state=EvaluationOutboxDeliveryStateV1.DELIVERED, lease=None)


@pytest.mark.covers("ADR-079")
def test_a_not_dispatched_state_holds_only_its_decision_time() -> None:
    pending = _initial().state
    withheld = replace(
        pending,
        state=EvaluationOutboxDeliveryStateV1.NOT_DISPATCHED,
        decided_at="2026-09-02T12:01:00+00:00",
    )

    assert withheld.decided_at == "2026-09-02T12:01:00+00:00"
    with pytest.raises(ValueError, match="requires only its decision time"):
        replace(withheld, decided_at=None)
    with pytest.raises(ValueError, match="requires only its decision time"):
        replace(withheld, delivered_at="2026-09-02T12:01:00+00:00")
    with pytest.raises(ValueError, match="requires only a delivery time"):
        replace(
            withheld,
            state=EvaluationOutboxDeliveryStateV1.DELIVERED,
            delivered_at="2026-09-02T12:01:00+00:00",
        )
    with pytest.raises(ValueError, match="UTC ISO instant"):
        replace(withheld, decided_at="2026-09-02T12:01:00")


def test_dynamodb_item_keeps_content_in_payload_and_projects_content_free_ready_index() -> None:
    item = _initial_item(_initial())

    assert set(item).isdisjoint({"question", "released_answer", "correlation_id"})
    assert item[ADMISSION_REF] == {"S": _ADMISSION_REF}
    assert item[AUDIT_ID] == {"S": _AUDIT_ID}
    assert item[DELIVERY_REF] == {"S": _DELIVERY_REF}
    assert item[STATE] == {"S": "pending"}
    assert item[ATTEMPT_COUNT] == {"N": "0"}
    assert item[READY_PARTITION] == {"S": DISPATCHABLE}
    assert item[READY_SORT] == {"S": f"{_ADMITTED_AT}#{_ADMISSION_REF}"}
    assert "fictional governed result" in read_text(item, PAYLOAD)
    assert _work_item(item).envelope == _envelope()


@pytest.mark.covers("ADR-079")
def test_a_not_dispatched_item_projects_its_terminal_state_without_its_stale_lease() -> None:
    item = _initial_item(_initial())
    item[STATE] = {"S": "not_dispatched"}
    item["decided_at"] = {"S": "2026-09-02T12:01:00+00:00"}

    state = _work_item(item).state

    assert state.state is EvaluationOutboxDeliveryStateV1.NOT_DISPATCHED
    assert (state.decided_at, state.lease, state.delivered_at) == (
        "2026-09-02T12:01:00+00:00",
        None,
        None,
    )


@pytest.mark.parametrize(
    ("question", "answer"),
    [("", "released"), ("question", ""), ("   ", "released"), ("question", "   ")],
)
def test_evaluation_envelope_requires_nonblank_released_pair(question: str, answer: str) -> None:
    with pytest.raises(ValueError, match="released question and answer"):
        EvaluationEnvelope(
            question=question,
            released_answer=answer,
            correlation_id="123e4567-e89b-42d3-a456-426614174000",
            audit_id=_AUDIT_ID,
            evaluation_admission_ref=_ADMISSION_REF,
        )


@pytest.mark.parametrize(
    "correlation_id",
    ["not-a-uuid", "123e4567-e89b-42d3-a456-426614174000".upper()],
)
def test_evaluation_envelope_requires_canonical_lowercase_uuid4(correlation_id: str) -> None:
    with pytest.raises(ValueError, match="canonical UUID4"):
        EvaluationEnvelope(
            question="question",
            released_answer="answer",
            correlation_id=correlation_id,
            audit_id=_AUDIT_ID,
            evaluation_admission_ref=_ADMISSION_REF,
        )


def test_evaluation_envelope_rejects_invalid_opaque_references_and_oversized_pair() -> None:
    with pytest.raises(ValueError, match="audit ID"):
        EvaluationEnvelope(
            question="question",
            released_answer="answer",
            correlation_id="123e4567-e89b-42d3-a456-426614174000",
            audit_id="audit id with spaces",
            evaluation_admission_ref=_ADMISSION_REF,
        )
    with pytest.raises(ValueError, match="aggregate delivery bound"):
        EvaluationEnvelope(
            question="question",
            released_answer="x" * (241 * 1024),
            correlation_id="123e4567-e89b-42d3-a456-426614174000",
            audit_id=_AUDIT_ID,
            evaluation_admission_ref=_ADMISSION_REF,
        )


def test_evaluation_envelope_rejects_grounded_context_over_its_exact_limit() -> None:
    with pytest.raises(ValueError, match="grounded context exceeds"):
        EvaluationEnvelope(
            question="question",
            released_answer="answer",
            correlation_id="123e4567-e89b-42d3-a456-426614174000",
            audit_id=_AUDIT_ID,
            evaluation_admission_ref=_ADMISSION_REF,
            grounded_context="x" * ((192 * 1024) + 1),
        )
