# SPDX-License-Identifier: MIT-0
"""Safe result-projection persistence from the encrypted queue."""

from __future__ import annotations

import os
from dataclasses import replace
from unittest.mock import MagicMock

import pytest
from aws_lambda_powertools import Logger

from adapters.jobs.evaluation_result_link.work import result_to_json
from adapters.outbound.telemetry.reporting import EvaluationPersisterLog
from domain.evaluation.linkage import (
    EvaluationResultStateV1,
    EvaluationResultV3,
)
from domain.evaluation.quality import VerdictCount
from domain.evaluation.release import EvaluationFamilyV1
from ports.outbound.audit import AuditWriteDisposition, AuditWriteResult

_HANDLER_ENVIRONMENT = {
    "AWS_DEFAULT_REGION": "us-east-1",
    "AWS_EC2_METADATA_DISABLED": "true",
    "ADL_ENVIRONMENT": "dev",
    "ADL_AWS_REGION": "us-east-1",
    "ADL_TRACING_ENABLED": "false",
    "ADL_TRACE_SAMPLE_RATIO": "1",
    "ADL_EVALUATION_VERDICTS_TABLE": "evaluation-verdicts-test",
    "ADL_RECEIPT_RETENTION_DAYS": "90",
}
_PREVIOUS_ENVIRONMENT = {key: os.environ.get(key) for key in _HANDLER_ENVIRONMENT}
os.environ.update(_HANDLER_ENVIRONMENT)

from adapters.jobs.evaluation_result_linker import handler as persister_handler  # noqa: E402

for _key, _value in _PREVIOUS_ENVIRONMENT.items():
    if _value is None:
        os.environ.pop(_key, None)
    else:
        os.environ[_key] = _value

pytestmark = pytest.mark.unit


_RECEIPT_CREATED_AT = "2026-09-02T12:00:00+00:00"
_ATTRIBUTES: dict[str, dict[str, object]] = {
    "receipt_created_at": {
        "stringValue": _RECEIPT_CREATED_AT,
        "stringListValues": [],
        "binaryListValues": [],
        "dataType": "String",
    }
}


class _Store:
    def __init__(self, disposition: AuditWriteDisposition = AuditWriteDisposition.APPLIED) -> None:
        self.results: list[EvaluationResultV3] = []
        self.receipt_times: list[str] = []
        self.disposition = disposition

    def write_evaluation_result(
        self, result: EvaluationResultV3, receipt_created_at: str
    ) -> AuditWriteResult:
        self.results.append(result)
        self.receipt_times.append(receipt_created_at)
        return AuditWriteResult(disposition=self.disposition)


class _Context:
    function_name = "evaluation-result-persister"
    function_version = "$LATEST"
    invoked_function_arn = (
        "arn:aws:lambda:us-east-1:111122223333:function:evaluation-result-persister"
    )
    memory_limit_in_mb = 512
    aws_request_id = "request-1"


def _result() -> EvaluationResultV3:
    return EvaluationResultV3(
        audit_id="audit-01",
        admission_ref="evaluation-admission-01",
        evaluator_ref="released-evaluator-01",
        evaluator_name="adlytics_dev_helpfulness_v4",
        result_ref="result-01",
        state=EvaluationResultStateV1.COMPLETED,
        label="good",
        score=0.75,
        evaluated_at="2026-09-04T12:00:00+00:00",
        family=EvaluationFamilyV1.V4,
        config_id="adlytics_dev_released_v4-0001",
        evaluator_revision="r1",
        model_id="fictional.judge-model-v1:0",
        prompt_digest="a" * 64,
    )


def test_safe_projection_is_persisted_once_without_any_logs_reader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _Store()
    lifecycle: list[dict[str, str | int]] = []
    traced = MagicMock()
    process_span = MagicMock()
    traced.return_value.__enter__.return_value = process_span

    def capture_lifecycle(event: EvaluationPersisterLog, _logger: Logger) -> None:
        lifecycle.append(event.projection)

    monkeypatch.setattr(persister_handler, "_VERDICTS", store)
    monkeypatch.setattr(persister_handler, "report_lifecycle", capture_lifecycle)
    monkeypatch.setattr(persister_handler, "span", traced)

    result = persister_handler.handler(
        {
            "Records": [
                {
                    "messageId": "message-1",
                    "body": result_to_json(_result()),
                    "messageAttributes": _ATTRIBUTES,
                }
            ]
        },
        _Context(),
    )

    assert result == {"batchItemFailures": []}
    assert vars(persister_handler.handler)["__trace_boundaries__"] == (
        persister_handler.EVALUATION_RESULT_PERSIST,
    )
    traced.assert_called_once_with(
        persister_handler.EVALUATION_RESULT_PERSIST,
        operation="evaluation_result.persist",
    )
    process_span.record.assert_called_once_with(
        outcome="completed",
        record_count=1,
        failure_count=0,
    )
    assert store.results == [_result()]
    # The verdict expires with its receipt: the time comes from the message, never a receipt read.
    assert store.receipt_times == [_RECEIPT_CREATED_AT]
    assert not hasattr(persister_handler, "_RESULT_READER")
    assert lifecycle == [
        {
            "operation": "evaluation_result.persist",
            "component": "evaluation_persister",
            "phase": "persistence",
            "outcome": "persisted",
            "persisted_count": 1,
            "audit_id": "audit-01",
            "investigation_next": "inspect_evaluation_chain",
        }
    ]


@pytest.mark.covers("ADR-079")
def test_an_in_flight_projection_stamped_with_the_retired_v3_family_is_still_persisted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A V3 result queued before the retirement deploy must not dead-letter (ADR-079)."""
    store = _Store()
    retired = replace(
        _result(), family=EvaluationFamilyV1.V3, config_id="adlytics_dev_released_v3-0001"
    )
    monkeypatch.setattr(persister_handler, "_VERDICTS", store)

    def ignore_lifecycle(_event: EvaluationPersisterLog, _logger: Logger) -> None:
        return None

    monkeypatch.setattr(persister_handler, "report_lifecycle", ignore_lifecycle)

    result = persister_handler.handler(
        {
            "Records": [
                {
                    "messageId": "message-1",
                    "body": result_to_json(retired),
                    "messageAttributes": _ATTRIBUTES,
                }
            ]
        },
        _Context(),
    )

    assert result == {"batchItemFailures": []}
    assert store.results == [retired]


def _persist(
    monkeypatch: pytest.MonkeyPatch, store: _Store, result: EvaluationResultV3
) -> list[VerdictCount]:
    counted: list[VerdictCount] = []
    monkeypatch.setattr(persister_handler, "_VERDICTS", store)
    monkeypatch.setattr(persister_handler, "report_evaluation_verdict", counted.append)

    def ignore_lifecycle(_event: EvaluationPersisterLog, _logger: Logger) -> None:
        return None

    monkeypatch.setattr(persister_handler, "report_lifecycle", ignore_lifecycle)
    record = {
        "messageId": "message-1",
        "body": result_to_json(result),
        "messageAttributes": _ATTRIBUTES,
    }
    assert persister_handler.handler({"Records": [record]}, _Context()) == {"batchItemFailures": []}
    return counted


@pytest.mark.covers("ADR-079")
def test_a_newly_linked_verdict_is_counted_once_by_its_closed_dimension_and_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = replace(_result(), evaluator_name="adlytics_dev_helpfulness_v4_4576eb06")
    assert _persist(monkeypatch, _Store(), current) == [
        VerdictCount(dimension="helpfulness", label="good")
    ]


@pytest.mark.covers("ADR-079")
def test_a_replayed_verdict_is_not_counted_again(monkeypatch: pytest.MonkeyPatch) -> None:
    replayed = _Store(AuditWriteDisposition.EXACT_REPLAY)
    assert _persist(monkeypatch, replayed, _result()) == []


def test_malformed_safe_projection_retries_only_that_safe_queue_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lifecycle: list[dict[str, str | int]] = []

    def capture_lifecycle(event: EvaluationPersisterLog, _logger: Logger) -> None:
        lifecycle.append(event.projection)

    monkeypatch.setattr(persister_handler, "_VERDICTS", _Store())
    monkeypatch.setattr(persister_handler, "report_lifecycle", capture_lifecycle)

    result = persister_handler.handler(
        {"Records": [{"messageId": "message-1", "body": "{}"}]},
        _Context(),
    )

    assert result == {"batchItemFailures": [{"itemIdentifier": "message-1"}]}
    assert lifecycle == [
        {
            "operation": "evaluation_result.persist",
            "component": "evaluation_persister",
            "phase": "persistence",
            "outcome": "retry",
            "persisted_count": 0,
            "investigation_next": "inspect_evaluation_chain",
        }
    ]


def _ignore_lifecycle(_event: EvaluationPersisterLog, _logger: Logger) -> None:
    return None


@pytest.mark.covers("ADR-081")
@pytest.mark.parametrize(
    "attributes",
    [
        None,
        {},
        {"extra": _ATTRIBUTES["receipt_created_at"]},
        {"receipt_created_at": {**_ATTRIBUTES["receipt_created_at"], "dataType": "Number"}},
        {
            "receipt_created_at": {
                **_ATTRIBUTES["receipt_created_at"],
                "stringValue": "2026-09-02T12:00:00",
            }
        },
    ],
    ids=["absent", "empty", "only-another-attribute", "number-type", "not-utc"],
)
def test_a_projection_without_one_exact_receipt_time_is_retried_and_never_persisted(
    monkeypatch: pytest.MonkeyPatch, attributes: dict[str, object] | None
) -> None:
    """A verdict whose expiry cannot be computed exactly must never be written without one."""
    store = _Store()
    monkeypatch.setattr(persister_handler, "_VERDICTS", store)
    monkeypatch.setattr(persister_handler, "report_lifecycle", _ignore_lifecycle)
    record: dict[str, object] = {"messageId": "message-1", "body": result_to_json(_result())}
    if attributes is not None:
        record["messageAttributes"] = attributes

    result = persister_handler.handler({"Records": [record]}, _Context())

    assert result == {"batchItemFailures": [{"itemIdentifier": "message-1"}]}
    assert store.results == []
