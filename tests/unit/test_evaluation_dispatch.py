# SPDX-License-Identifier: MIT-0
"""The ADR-079 dispatch rule: which admitted turns are sent, withheld, or waited for."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from domain.conversation.audit import TurnDeliveryDispositionV1
from domain.evaluation.dispatch import (
    EvaluationDispatchActionV1,
    EvaluationDispatchDecision,
    EvaluationDispatchRequest,
    decide_dispatch,
    enabled_delivery_families,
)
from domain.evaluation.linkage import (
    EvaluationDeliveryFamilyV1,
    EvaluationDeliveryOutcomeV1,
    EvaluationDeliveryReceiptV2,
    EvaluationNotDispatchedReasonV1,
)
from domain.evaluation.outbox import evaluation_delivery_ref
from domain.evaluation.release import (
    EvaluationFamilyV1,
    EvaluationReleaseEvaluatorV1,
    EvaluationReleaseV1,
)

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-079")]

_ADMITTED = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
_WAIT_SECONDS = 420
_EVALUATOR = EvaluationReleaseEvaluatorV1(
    evaluator_id="fictional-helpful-01",
    evaluator_name="fictional_helpful",
    revision="r1",
    model_id="fictional.judge-model-v1:0",
    prompt_digest="d" * 64,
)


def _release(family: EvaluationFamilyV1, *, enabled: bool) -> EvaluationReleaseV1:
    return EvaluationReleaseV1(
        family=family,
        enabled=enabled,
        config_id=f"fictional_released_{family.value}-0001" if enabled else None,
        evaluators=(_EVALUATOR,) if enabled else (),
        decided_at="2026-09-27T00:00:00+00:00",
    )


_V4_ONLY = enabled_delivery_families((_release(EvaluationFamilyV1.V4, enabled=True),))


def _decide(
    disposition: TurnDeliveryDispositionV1 | None,
    *,
    families: tuple[EvaluationDeliveryFamilyV1, ...] = _V4_ONLY,
    elapsed_seconds: int = 0,
) -> EvaluationDispatchDecision:
    return decide_dispatch(
        EvaluationDispatchRequest(
            families=families,
            disposition=disposition,
            admitted_at=_ADMITTED,
            now=_ADMITTED + timedelta(seconds=elapsed_seconds),
            delivery_wait_seconds=_WAIT_SECONDS,
        )
    )


def test_only_enabled_records_become_families_and_a_missing_record_is_not_enabled() -> None:
    families = enabled_delivery_families((_release(EvaluationFamilyV1.V4, enabled=True),))

    assert [(item.family, item.config_id) for item in families] == [
        (EvaluationFamilyV1.V4, "fictional_released_v4-0001")
    ]
    assert enabled_delivery_families((_release(EvaluationFamilyV1.V4, enabled=False),)) == ()
    assert enabled_delivery_families((None,)) == ()


def test_two_records_enabling_one_family_are_refused() -> None:
    release = _release(EvaluationFamilyV1.V4, enabled=True)

    with pytest.raises(ValueError, match="same evaluation family"):
        enabled_delivery_families((release, release))


@pytest.mark.parametrize(
    "disposition",
    [None, *TurnDeliveryDispositionV1],
    ids=["absent", *(item.value for item in TurnDeliveryDispositionV1)],
)
def test_no_enabled_family_withholds_the_row_whatever_its_delivery(
    disposition: TurnDeliveryDispositionV1 | None,
) -> None:
    decision = _decide(disposition, families=(), elapsed_seconds=_WAIT_SECONDS * 2)

    assert decision.action is EvaluationDispatchActionV1.WITHHOLD
    assert decision.reason is EvaluationNotDispatchedReasonV1.EVALUATION_NOT_ENABLED


def test_a_delivered_answer_is_sent_to_every_enabled_family() -> None:
    decision = _decide(TurnDeliveryDispositionV1.DELIVERED)

    assert decision.action is EvaluationDispatchActionV1.SEND
    assert decision.families == _V4_ONLY
    assert decision.reason is None


@pytest.mark.parametrize(
    "disposition", [TurnDeliveryDispositionV1.TRUNCATED, TurnDeliveryDispositionV1.ABORTED]
)
def test_an_answer_the_user_did_not_fully_receive_is_withheld(
    disposition: TurnDeliveryDispositionV1,
) -> None:
    """Evaluating a truncated stream would judge an answer the user never saw (ADR-063)."""
    decision = _decide(disposition)

    assert decision.action is EvaluationDispatchActionV1.WITHHOLD
    assert decision.reason is EvaluationNotDispatchedReasonV1.ANSWER_NOT_RECEIVED


@pytest.mark.parametrize("elapsed_seconds", [0, _WAIT_SECONDS - 1])
def test_an_absent_delivery_companion_inside_the_wait_is_waited_for(elapsed_seconds: int) -> None:
    decision = _decide(None, elapsed_seconds=elapsed_seconds)

    assert decision.action is EvaluationDispatchActionV1.WAIT
    assert (decision.families, decision.reason) == ((), None)


@pytest.mark.parametrize("elapsed_seconds", [_WAIT_SECONDS, _WAIT_SECONDS + 3600])
def test_an_absent_delivery_companion_at_or_past_the_wait_is_delivery_unknown(
    elapsed_seconds: int,
) -> None:
    decision = _decide(None, elapsed_seconds=elapsed_seconds)

    assert decision.action is EvaluationDispatchActionV1.WITHHOLD
    assert decision.reason is EvaluationNotDispatchedReasonV1.DELIVERY_UNKNOWN


def test_a_dispatch_request_refuses_a_non_positive_wait_and_a_naive_instant() -> None:
    with pytest.raises(ValueError, match="positive number of seconds"):
        EvaluationDispatchRequest(
            families=(),
            disposition=None,
            admitted_at=_ADMITTED,
            now=_ADMITTED,
            delivery_wait_seconds=0,
        )
    with pytest.raises(ValueError, match="must be UTC"):
        EvaluationDispatchRequest(
            families=(),
            disposition=None,
            admitted_at=_ADMITTED.replace(tzinfo=None),
            now=_ADMITTED,
            delivery_wait_seconds=_WAIT_SECONDS,
        )


def test_a_decision_cannot_send_without_families_or_withhold_without_a_reason() -> None:
    with pytest.raises(ValueError, match="names families"):
        EvaluationDispatchDecision(action=EvaluationDispatchActionV1.SEND)
    with pytest.raises(ValueError, match="names a reason"):
        EvaluationDispatchDecision(action=EvaluationDispatchActionV1.WITHHOLD)


# --- The receipt the decision is recorded in -------------------------------------------------


def _receipt(**changes: object) -> EvaluationDeliveryReceiptV2:
    fields: dict[str, object] = {
        "audit_id": "audit-01",
        "admission_ref": "admission-01",
        "delivery_ref": evaluation_delivery_ref("audit-01", "admission-01"),
        "decided_at": "2026-09-27T12:01:00+00:00",
        "outcome": EvaluationDeliveryOutcomeV1.DISPATCHED,
        "not_dispatched_reason": None,
        "families": _V4_ONLY,
        **changes,
    }
    return EvaluationDeliveryReceiptV2(**fields)  # pyright: ignore[reportArgumentType]


def test_a_dispatched_receipt_finds_the_family_and_evaluator_it_was_sent_to() -> None:
    receipt = _receipt()
    family = receipt.dispatched_family("fictional_released_v4-0001")

    assert family is not None
    assert family.evaluator("fictional-helpful-01") == _EVALUATOR
    assert family.evaluator("fictional-other-01") is None
    assert receipt.dispatched_family("fictional_released_v5-0001") is None


def test_a_receipt_written_while_v3_ran_still_names_it() -> None:
    """ADR-079: V3 is retired, but receipts that name it stay readable until they expire."""
    retired = EvaluationDeliveryFamilyV1(
        family=EvaluationFamilyV1.V3,
        config_id="fictional_released_v3-0001",
        evaluators=(_EVALUATOR,),
    )
    receipt = _receipt(families=(retired, *_V4_ONLY))

    assert receipt.dispatched_family("fictional_released_v3-0001") == retired
    assert receipt.dispatched_family("fictional_released_v4-0001") == _V4_ONLY[0]


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"families": ()}, "requires families and no reason"),
        (
            {"not_dispatched_reason": EvaluationNotDispatchedReasonV1.DELIVERY_UNKNOWN},
            "requires families and no reason",
        ),
        (
            {"outcome": EvaluationDeliveryOutcomeV1.NOT_DISPATCHED},
            "requires only its reason",
        ),
        (
            {
                "outcome": EvaluationDeliveryOutcomeV1.NOT_DISPATCHED,
                "not_dispatched_reason": EvaluationNotDispatchedReasonV1.DELIVERY_UNKNOWN,
            },
            "requires only its reason",
        ),
        ({"families": _V4_ONLY * 2}, "families must be unique"),
        ({"delivery_ref": "evaluation-delivery:other"}, "must be deterministic"),
    ],
    ids=[
        "dispatched-without-family",
        "dispatched-with-reason",
        "withheld-without-reason",
        "withheld-with-family",
        "family-twice",
        "foreign-delivery-ref",
    ],
)
def test_a_receipt_refuses_an_outcome_its_families_and_reason_contradict(
    changes: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        _receipt(**changes)


def test_a_not_dispatched_receipt_names_no_family_a_verdict_could_be_stamped_from() -> None:
    receipt = _receipt(
        outcome=EvaluationDeliveryOutcomeV1.NOT_DISPATCHED,
        not_dispatched_reason=EvaluationNotDispatchedReasonV1.EVALUATION_NOT_ENABLED,
        families=(),
    )

    assert receipt.dispatched_family("fictional_released_v4-0001") is None
