# SPDX-License-Identifier: MIT-0
"""Protected CloudWatch evaluation-result subscription projection."""

from __future__ import annotations

import base64
import gzip
import inspect
import json
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from aws_lambda_powertools import Logger

from adapters.jobs.evaluation_result_ingress.subscription import candidates_from_subscription
from adapters.jobs.evaluation_result_link.work import result_from_json
from adapters.outbound.telemetry.reporting import EvaluationIngressLog
from domain.evaluation.linkage import (
    EvaluationDeliveryFamilyV1,
    EvaluationDeliveryOutcomeV1,
    EvaluationDeliveryReceiptV2,
    EvaluationNotDispatchedReasonV1,
    EvaluationResultStateV1,
)
from domain.evaluation.outbox import evaluation_delivery_ref
from domain.evaluation.release import EvaluationFamilyV1, EvaluationReleaseEvaluatorV1
from ports.outbound.audit import (
    EvaluationDeliveryPredatesGate,
    EvaluationResultLinkResolution,
    EvaluationResultLinkResolutionState,
    EvaluationResultLinkTarget,
)

pytestmark = pytest.mark.unit

_V3_GROUP = "/aws/bedrock-agentcore/evaluations/results/test-v3"
_V4_GROUP = "/aws/bedrock-agentcore/evaluations/results/test-v4"
_GROUPS = frozenset({_V3_GROUP, _V4_GROUP})
_V3_CONFIG = "test-v3"
_V4_CONFIG = "test-v4"
_EVALUATOR_NAME = "adlytics_dev_helpfulness_v4"
_EVALUATOR_ARN = "arn:aws:bedrock-agentcore:us-east-1:111122223333:evaluator/released-evaluator-01"
_TRACE = "a" * 32
_CORRELATION = "00000000-0000-4000-8000-000000000000"


def _subscription(
    message: dict[str, object],
    *,
    message_type: str = "DATA_MESSAGE",
    log_group: str = _V3_GROUP,
) -> dict[str, object]:
    payload = {
        "messageType": message_type,
        "logGroup": log_group,
        "logStream": "service-result-stream",
        "logEvents": [
            {
                "id": "service-event-01",
                "timestamp": 1_788_200_000_000,
                "message": json.dumps(message),
            }
        ],
    }
    encoded = base64.b64encode(gzip.compress(json.dumps(payload).encode())).decode()
    return {"awslogs": {"data": encoded}}


def _message(**overrides: object) -> dict[str, object]:
    return {
        "traceId": _TRACE,
        "session.id": _CORRELATION,
        "onlineEvaluationConfigId": _V3_CONFIG,
        "aws.bedrock_agentcore.evaluator.arn": _EVALUATOR_ARN,
        "gen_ai.evaluation.name": _EVALUATOR_NAME,
        "gen_ai.evaluation.score.label": "Good",
        "gen_ai.evaluation.score.value": 0.75,
        "agentcore.invocation.user_prompt": "fictional protected question",
        "agentcore.invocation.agent_response": "fictional protected answer",
        **overrides,
    }


def _candidates(event: dict[str, object]):
    return candidates_from_subscription(event, expected_log_groups=_GROUPS)


def test_subscription_projection_retains_only_allowlisted_result_candidate_fields() -> None:
    candidates = _candidates(_subscription(_message()))

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.trace_id == _TRACE
    assert candidate.correlation_id == _CORRELATION
    assert candidate.config_id == _V3_CONFIG
    assert candidate.evaluator_ref == "released-evaluator-01"
    assert candidate.evaluator_name == _EVALUATOR_NAME
    assert candidate.state is EvaluationResultStateV1.COMPLETED
    assert candidate.label == "good"
    assert candidate.score == 0.75
    assert "fictional protected" not in repr(candidate)


@pytest.mark.covers("ADR-059")
def test_a_v4_dimension_keeps_its_configuration_and_its_own_scale_label() -> None:
    """A V4 label is the evaluator's own scale label, normalized.

    A four-point helpfulness enum would misfile 'Partially Grounded' as an evaluator failure.
    """
    candidate = _candidates(
        _subscription(_grounded_message("Partially Grounded"), log_group=_V4_GROUP)
    )[0]
    assert candidate.config_id == _V4_CONFIG
    assert candidate.evaluator_ref == "grounded-01"
    assert candidate.evaluator_name == "adlytics_dev_grounded_correctness_v4_r2"
    assert candidate.state is EvaluationResultStateV1.COMPLETED
    assert candidate.label == "partially_grounded"
    assert candidate.score is None


def _grounded_message(label: str) -> dict[str, object]:
    """Build a categorical grounded-correctness verdict: a label and no score (ADR-082)."""
    message = _message(
        **{
            "onlineEvaluationConfigId": _V4_CONFIG,
            "aws.bedrock_agentcore.evaluator.arn": (
                "arn:aws:bedrock-agentcore:us-east-1:111122223333:evaluator/grounded-01"
            ),
            "gen_ai.evaluation.name": "adlytics_dev_grounded_correctness_v4_r2",
            "gen_ai.evaluation.score.label": label,
        }
    )
    del message["gen_ai.evaluation.score.value"]
    return message


@pytest.mark.covers("ADR-082")
def test_a_label_only_categorical_verdict_completes_including_not_applicable() -> None:
    candidate = _candidates(
        _subscription(_grounded_message("Not Applicable"), log_group=_V4_GROUP)
    )[0]
    assert candidate.state is EvaluationResultStateV1.COMPLETED
    assert candidate.label == "not_applicable"
    assert candidate.score is None


@pytest.mark.covers("ADR-082")
def test_a_present_but_non_numeric_score_is_an_evaluator_failure() -> None:
    message = _grounded_message("Grounded")
    message["gen_ai.evaluation.score.value"] = "high"
    candidate = _candidates(_subscription(message, log_group=_V4_GROUP))[0]
    assert candidate.state is EvaluationResultStateV1.EVALUATOR_FAILED
    assert candidate.label is None
    assert candidate.score is None


def test_subscription_projection_collects_unique_allowlisted_fields_from_a_nested_event() -> None:
    candidates = _candidates(
        _subscription({"attributes": _message(), "other": {"question": "ignored"}})
    )

    assert len(candidates) == 1
    assert candidates[0].state is EvaluationResultStateV1.COMPLETED


def test_subscription_projection_maps_invalid_scale_to_content_free_evaluator_failure() -> None:
    candidate = _candidates(_subscription(_message(**{"gen_ai.evaluation.score.value": 0.1})))[0]

    assert candidate.state is EvaluationResultStateV1.EVALUATOR_FAILED
    assert candidate.label is None
    assert candidate.score is None


@pytest.mark.covers("ADR-079")
def test_a_configuration_is_kept_from_the_record_and_judged_later_by_the_receipt() -> None:
    """Deployment configuration no longer decides admission; the turn's receipt does."""
    (candidate,) = _candidates(_subscription(_message(onlineEvaluationConfigId="other-config")))

    assert candidate.config_id == "other-config"


def test_control_message_and_malformed_configuration_or_evaluator_emit_no_candidate() -> None:
    assert _candidates(_subscription(_message(), message_type="CONTROL_MESSAGE")) == ()
    assert _candidates(_subscription(_message(onlineEvaluationConfigId="has#hash"))) == ()
    assert _candidates(_subscription(_message(onlineEvaluationConfigId=7))) == ()
    assert (
        _candidates(
            _subscription(
                {
                    name: value
                    for name, value in _message().items()
                    if name != "onlineEvaluationConfigId"
                }
            )
        )
        == ()
    )
    assert (
        _candidates(
            _subscription(
                _message(**{"aws.bedrock_agentcore.evaluator.arn": "not-an-evaluator-arn"})
            )
        )
        == ()
    )


def test_a_foreign_log_group_is_rejected_as_an_invalid_source() -> None:
    with pytest.raises(ValueError, match="source is invalid"):
        _candidates(_subscription(_message(), log_group="/aws/other"))


_HANDLER_ENVIRONMENT = {
    "AWS_DEFAULT_REGION": "us-east-1",
    "AWS_EC2_METADATA_DISABLED": "true",
    "ADL_ENVIRONMENT": "dev",
    "ADL_AWS_REGION": "us-east-1",
    "ADL_TRACING_ENABLED": "false",
    "ADL_TRACE_SAMPLE_RATIO": "1",
    "ADL_AGENT_QUALITY_AUDIT_TABLE": "agent-quality-audit-test",
    "ADL_EVALUATION_WRITER_RECEIPTS_TABLE": "evaluation-writer-receipts-test",
    "ADL_EVALUATION_DELIVERIES_TABLE": "evaluation-deliveries-test",
    "ADL_EVALUATION_RESULT_PROJECTION_QUEUE_URL": "https://sqs.us-east-1.amazonaws.com/111122223333/safe-result",
    "ADL_RELEASED_EVALUATION_RESULT_LOG_GROUPS": f"{_V3_GROUP},{_V4_GROUP}",
}
_PREVIOUS_ENVIRONMENT = {key: os.environ.get(key) for key in _HANDLER_ENVIRONMENT}
os.environ.update(_HANDLER_ENVIRONMENT)

from adapters.jobs.evaluation_result_ingress import handler as ingress_handler  # noqa: E402

for _key, _value in _PREVIOUS_ENVIRONMENT.items():
    if _value is None:
        os.environ.pop(_key, None)
    else:
        os.environ[_key] = _value


_RECEIPT_CREATED_AT = "2026-09-02T12:00:00+00:00"
_EVALUATOR = EvaluationReleaseEvaluatorV1(
    evaluator_id="released-evaluator-01",
    evaluator_name=_EVALUATOR_NAME,
    revision="r3",
    model_id="fictional.judge-model-v1:0",
    prompt_digest="f" * 64,
)


def _dispatched(
    config_id: str = _V3_CONFIG, evaluator_id: str = "released-evaluator-01"
) -> EvaluationDeliveryReceiptV2:
    evaluator = EvaluationReleaseEvaluatorV1(
        evaluator_id=evaluator_id,
        evaluator_name=_EVALUATOR.evaluator_name,
        revision=_EVALUATOR.revision,
        model_id=_EVALUATOR.model_id,
        prompt_digest=_EVALUATOR.prompt_digest,
    )
    return EvaluationDeliveryReceiptV2(
        audit_id="audit-01",
        admission_ref="admission-01",
        delivery_ref=evaluation_delivery_ref("audit-01", "admission-01"),
        decided_at="2026-09-02T12:00:30+00:00",
        outcome=EvaluationDeliveryOutcomeV1.DISPATCHED,
        not_dispatched_reason=None,
        families=(
            EvaluationDeliveryFamilyV1(
                family=EvaluationFamilyV1.V4, config_id=config_id, evaluators=(evaluator,)
            ),
        ),
    )


class _Deliveries:
    def __init__(
        self, receipt: EvaluationDeliveryReceiptV2 | EvaluationDeliveryPredatesGate | None
    ) -> None:
        self.receipt = receipt
        self.reads: list[tuple[str, str]] = []

    def get_evaluation_delivery(
        self, audit_id: str, admission_ref: str
    ) -> EvaluationDeliveryReceiptV2 | EvaluationDeliveryPredatesGate | None:
        self.reads.append((audit_id, admission_ref))
        return self.receipt


class _Store:
    def resolve_evaluation_result(self, _request: object) -> EvaluationResultLinkResolution:
        return EvaluationResultLinkResolution(
            state=EvaluationResultLinkResolutionState.LINKED,
            target=EvaluationResultLinkTarget(
                audit_id="audit-01",
                admission_ref="admission-01",
                receipt_created_at=_RECEIPT_CREATED_AT,
            ),
        )


class _Queue:
    def __init__(self) -> None:
        self.bodies: list[str] = []
        self.attributes: list[object] = []

    def send_message(self, **kwargs: object) -> SimpleNamespace:
        body = kwargs.get("MessageBody")
        assert isinstance(body, str)
        self.bodies.append(body)
        self.attributes.append(kwargs.get("MessageAttributes"))
        return SimpleNamespace()


def test_ingress_attests_then_enqueues_only_canonical_safe_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue = _Queue()
    lifecycle: list[dict[str, str | int | float]] = []

    def capture_lifecycle(event: EvaluationIngressLog, _logger: Logger) -> None:
        lifecycle.append(event.projection)

    monkeypatch.setattr(ingress_handler, "_RESOLVER", _Store())
    monkeypatch.setattr(ingress_handler, "_DELIVERIES", _Deliveries(_dispatched()))
    monkeypatch.setattr(ingress_handler, "_SQS", queue)
    monkeypatch.setattr(ingress_handler, "report_lifecycle", capture_lifecycle)

    assert ingress_handler._ingest(_subscription(_message())) == (1, 0)  # pyright: ignore[reportPrivateUsage]
    assert vars(ingress_handler.handler)["__trace_boundaries__"] == (
        ingress_handler.EVALUATION_RESULT_INGRESS,
    )

    assert len(queue.bodies) == 1
    # The linker never reads receipts, so the receipt's creation time rides beside the canonical
    # body, never inside it (ADR-080 §1).
    assert queue.attributes == [
        {"receipt_created_at": {"DataType": "String", "StringValue": _RECEIPT_CREATED_AT}}
    ]
    assert _RECEIPT_CREATED_AT not in queue.bodies[0]
    result = result_from_json(queue.bodies[0])
    assert result.audit_id == "audit-01"
    assert result.admission_ref == "admission-01"
    assert result.evaluator_ref == "released-evaluator-01"
    assert result.evaluator_name == _EVALUATOR_NAME
    # Provenance is stamped from the turn's delivery receipt, never from the record (ADR-079).
    assert (result.family, result.config_id, result.evaluator_revision) == (
        EvaluationFamilyV1.V4,
        _V3_CONFIG,
        "r3",
    )
    assert (result.model_id, result.prompt_digest) == (_EVALUATOR.model_id, "f" * 64)
    assert "fictional protected" not in queue.bodies[0]
    assert _TRACE not in queue.bodies[0]
    assert _CORRELATION not in queue.bodies[0]
    assert lifecycle == [
        {
            "operation": "evaluation_result.ingress",
            "component": "evaluation_ingress",
            "phase": "processing",
            "outcome": "linked",
            "linked_count": 1,
            "ignored_count": 0,
            "audit_id": "audit-01",
            "correlation_id": _CORRELATION,
            "admission_ref": "admission-01",
            "evaluator_ref": "released-evaluator-01",
            "evaluator_name": _EVALUATOR_NAME,
            "result_state": "completed",
            "quality_label": "good",
            "quality_score": 0.75,
            "investigation_next": "inspect_evaluation_chain",
        }
    ]


def test_ingress_handler_root_span_records_aggregate_terminal_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    traced = MagicMock()
    process_span = MagicMock()
    traced.return_value.__enter__.return_value = process_span

    def aggregate_counts(_event: object) -> tuple[int, int]:
        return 2, 1

    monkeypatch.setattr(ingress_handler, "span", traced)
    monkeypatch.setattr(ingress_handler, "_ingest", aggregate_counts)

    assert inspect.unwrap(ingress_handler.handler)({}, object()) is None

    traced.assert_called_once_with(
        ingress_handler.EVALUATION_RESULT_INGRESS,
        operation="evaluation_result.ingress",
    )
    process_span.record.assert_called_once_with(
        outcome="completed",
        linked_count=2,
        ignored_count=1,
    )


def _ingest_with(
    monkeypatch: pytest.MonkeyPatch, deliveries: _Deliveries
) -> tuple[tuple[int, int], _Queue, list[dict[str, str | int | float]]]:
    queue = _Queue()
    lifecycle: list[dict[str, str | int | float]] = []

    def capture_lifecycle(event: EvaluationIngressLog, _logger: Logger) -> None:
        lifecycle.append(event.projection)

    monkeypatch.setattr(ingress_handler, "_RESOLVER", _Store())
    monkeypatch.setattr(ingress_handler, "_DELIVERIES", deliveries)
    monkeypatch.setattr(ingress_handler, "_SQS", queue)
    monkeypatch.setattr(ingress_handler, "report_lifecycle", capture_lifecycle)
    counts = ingress_handler._ingest(_subscription(_message()))  # pyright: ignore[reportPrivateUsage]
    return counts, queue, lifecycle


_WITHHELD = EvaluationDeliveryReceiptV2(
    audit_id="audit-01",
    admission_ref="admission-01",
    delivery_ref=evaluation_delivery_ref("audit-01", "admission-01"),
    decided_at="2026-09-02T12:00:30+00:00",
    outcome=EvaluationDeliveryOutcomeV1.NOT_DISPATCHED,
    not_dispatched_reason=EvaluationNotDispatchedReasonV1.EVALUATION_NOT_ENABLED,
    families=(),
)


@pytest.mark.covers("ADR-079")
@pytest.mark.parametrize(
    "receipt",
    [
        _dispatched(config_id="other-config"),
        _dispatched(evaluator_id="other-evaluator-01"),
        _WITHHELD,
        EvaluationDeliveryPredatesGate(audit_id="audit-01", admission_ref="admission-01"),
    ],
    ids=["other-configuration", "other-evaluator", "not-dispatched", "predates-gate"],
)
def test_a_result_the_turn_was_not_dispatched_to_is_ignored_and_never_enqueued(
    monkeypatch: pytest.MonkeyPatch,
    receipt: EvaluationDeliveryReceiptV2 | EvaluationDeliveryPredatesGate,
) -> None:
    """A result from a configuration or evaluator the receipt never named has no provenance."""
    deliveries = _Deliveries(receipt)

    counts, queue, lifecycle = _ingest_with(monkeypatch, deliveries)

    assert counts == (0, 1)
    assert queue.bodies == []
    assert deliveries.reads == [("audit-01", "admission-01")]
    assert [(event["outcome"], event["ignored_count"]) for event in lifecycle] == [("ignored", 1)]


@pytest.mark.covers("ADR-079")
def test_a_result_whose_delivery_receipt_is_not_yet_written_is_retried_not_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dispatcher sends before it writes the receipt, so a result can arrive first."""
    queue = _Queue()
    monkeypatch.setattr(ingress_handler, "_RESOLVER", _Store())
    monkeypatch.setattr(ingress_handler, "_DELIVERIES", _Deliveries(None))
    monkeypatch.setattr(ingress_handler, "_SQS", queue)

    with pytest.raises(ingress_handler._PendingLinkError, match="delivery receipt is pending"):  # pyright: ignore[reportPrivateUsage]
        ingress_handler._ingest(_subscription(_message()))  # pyright: ignore[reportPrivateUsage]

    assert queue.bodies == []


_PENDING_CORRELATION = "00000000-0000-4000-8000-000000000001"


def _discard_lifecycle(_event: EvaluationIngressLog, _logger: Logger) -> None:
    return None


def _batch(*messages: dict[str, object]) -> dict[str, object]:
    payload = {
        "messageType": "DATA_MESSAGE",
        "logGroup": _V3_GROUP,
        "logStream": "service-result-stream",
        "logEvents": [
            {
                "id": f"service-event-{index:02d}",
                "timestamp": 1_788_200_000_000 + index,
                "message": json.dumps(message),
            }
            for index, message in enumerate(messages)
        ],
    }
    encoded = base64.b64encode(gzip.compress(json.dumps(payload).encode())).decode()
    return {"awslogs": {"data": encoded}}


class _TwoTurnStore:
    """Resolves the pending correlation to its own turn and every other one to audit-01."""

    def resolve_evaluation_result(self, request: object) -> EvaluationResultLinkResolution:
        pending = getattr(request, "correlation_id", "") == _PENDING_CORRELATION
        return EvaluationResultLinkResolution(
            state=EvaluationResultLinkResolutionState.LINKED,
            target=EvaluationResultLinkTarget(
                audit_id="audit-02" if pending else "audit-01",
                admission_ref="admission-02" if pending else "admission-01",
                receipt_created_at=_RECEIPT_CREATED_AT,
            ),
        )


class _ReceiptsByTurn:
    def __init__(self, receipts: dict[str, EvaluationDeliveryReceiptV2 | None]) -> None:
        self.receipts = receipts
        self.reads: list[str] = []

    def get_evaluation_delivery(
        self, audit_id: str, _admission_ref: str
    ) -> EvaluationDeliveryReceiptV2 | None:
        self.reads.append(audit_id)
        return self.receipts.get(audit_id)


@pytest.mark.covers("ADR-079")
def test_a_pending_result_does_not_cost_the_batch_its_admissible_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The admissible result is enqueued before the pending one requests the retry."""
    queue = _Queue()
    deliveries = _ReceiptsByTurn({"audit-01": _dispatched(), "audit-02": None})
    monkeypatch.setattr(ingress_handler, "_RESOLVER", _TwoTurnStore())
    monkeypatch.setattr(ingress_handler, "_DELIVERIES", deliveries)
    monkeypatch.setattr(ingress_handler, "_SQS", queue)
    monkeypatch.setattr(ingress_handler, "report_lifecycle", _discard_lifecycle)
    event = _batch(
        _message(**{"session.id": _PENDING_CORRELATION, "traceId": "b" * 32}),
        _message(),
    )

    with pytest.raises(ingress_handler._PendingLinkError, match="delivery receipt is pending"):  # pyright: ignore[reportPrivateUsage]
        ingress_handler._ingest(event)  # pyright: ignore[reportPrivateUsage]

    assert deliveries.reads == ["audit-02", "audit-01"]
    assert len(queue.bodies) == 1
    assert result_from_json(queue.bodies[0]).audit_id == "audit-01"


@pytest.mark.covers("ADR-079")
def test_a_pending_writer_receipt_is_retried_after_the_rest_of_the_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _WriterPendingFirst(_TwoTurnStore):
        def resolve_evaluation_result(self, request: object) -> EvaluationResultLinkResolution:
            if getattr(request, "correlation_id", "") == _PENDING_CORRELATION:
                return EvaluationResultLinkResolution(
                    state=EvaluationResultLinkResolutionState.PENDING, target=None
                )
            return super().resolve_evaluation_result(request)

    queue = _Queue()
    monkeypatch.setattr(ingress_handler, "_RESOLVER", _WriterPendingFirst())
    monkeypatch.setattr(
        ingress_handler, "_DELIVERIES", _ReceiptsByTurn({"audit-01": _dispatched()})
    )
    monkeypatch.setattr(ingress_handler, "_SQS", queue)
    monkeypatch.setattr(ingress_handler, "report_lifecycle", _discard_lifecycle)
    event = _batch(
        _message(**{"session.id": _PENDING_CORRELATION, "traceId": "b" * 32}),
        _message(),
    )

    with pytest.raises(ingress_handler._PendingLinkError, match="writer receipt is pending"):  # pyright: ignore[reportPrivateUsage]
        ingress_handler._ingest(event)  # pyright: ignore[reportPrivateUsage]

    assert len(queue.bodies) == 1
