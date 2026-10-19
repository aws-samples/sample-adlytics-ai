# SPDX-License-Identifier: MIT-0
"""The receipt-creation-time message attribute both evaluation queues carry beside their body."""

from __future__ import annotations

import json
from typing import Any, cast

import pytest

from adapters.outbound.sqs.attributes import (
    receipt_created_at_attribute,
    receipt_created_at_message_attribute,
)
from adapters.outbound.sqs.evaluation import SqsEvaluationSink
from domain.evaluation.envelope import EvaluationEnvelope
from domain.shared_kernel.errors import ErrorCode, InvariantError

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-080", "ADR-081")]

_AT = "2026-09-02T12:00:00+00:00"


def _lambda_attribute(**overrides: object) -> dict[str, object]:
    """One attribute exactly as the Lambda SQS event renders it."""
    return {
        "stringValue": _AT,
        "stringListValues": [],
        "binaryListValues": [],
        "dataType": "String",
        **overrides,
    }


def test_a_producer_sends_exactly_one_string_attribute_holding_the_instant() -> None:
    assert receipt_created_at_message_attribute(_AT) == {
        "receipt_created_at": {"DataType": "String", "StringValue": _AT}
    }


@pytest.mark.parametrize("value", ["2026-09-02T12:00:00", "2026-09-02T14:00:00+02:00", "", "x"])
def test_a_producer_refuses_a_time_that_is_not_a_utc_instant(value: str) -> None:
    with pytest.raises(ValueError, match="receipt creation time"):
        receipt_created_at_message_attribute(value)


def test_a_consumer_reads_the_attribute_as_lambda_delivers_it() -> None:
    record = {"messageAttributes": {"receipt_created_at": _lambda_attribute()}}

    assert receipt_created_at_attribute(record) == _AT


def test_a_consumer_accepts_the_attribute_without_the_empty_list_keys() -> None:
    record = {
        "messageAttributes": {"receipt_created_at": {"stringValue": _AT, "dataType": "String"}}
    }

    assert receipt_created_at_attribute(record) == _AT


@pytest.mark.parametrize(
    "record",
    [
        {},
        {"messageAttributes": None},
        {"messageAttributes": {}},
        {"messageAttributes": {"other": _lambda_attribute()}},
        {"messageAttributes": {"receipt_created_at": _AT}},
        {"messageAttributes": {"receipt_created_at": _lambda_attribute(dataType="Number")}},
        {"messageAttributes": {"receipt_created_at": _lambda_attribute(dataType="String.x")}},
        {"messageAttributes": {"receipt_created_at": _lambda_attribute(stringValue=None)}},
        {"messageAttributes": {"receipt_created_at": _lambda_attribute(stringListValues=[_AT])}},
        {"messageAttributes": {"receipt_created_at": _lambda_attribute(binaryListValues=["AA=="])}},
        {"messageAttributes": {"receipt_created_at": _lambda_attribute(binaryValue="AA==")}},
        {
            "messageAttributes": {
                "receipt_created_at": _lambda_attribute(stringValue="2026-09-02T12:00:00")
            }
        },
    ],
    ids=[
        "absent",
        "null",
        "empty",
        "other-name",
        "bare-string",
        "number-type",
        "custom-type",
        "no-value",
        "string-list",
        "binary-list",
        "binary-value",
        "naive-time",
    ],
)
def test_a_consumer_refuses_anything_but_one_exact_string_instant(
    record: dict[str, object],
) -> None:
    """A tolerated variant would let a record expire at a time no receipt ever held."""
    with pytest.raises(ValueError):
        receipt_created_at_attribute(record)


def test_a_consumer_ignores_unrelated_attributes_beside_the_receipt_time() -> None:
    """Trace propagation an instrumentation upgrade might inject must not dead-letter messages."""
    record = {
        "messageAttributes": {
            "receipt_created_at": _lambda_attribute(),
            "traceparent": _lambda_attribute(stringValue="00-" + "1" * 32 + "-" + "2" * 16 + "-01"),
            "binary-other": {"binaryValue": "AA==", "dataType": "Binary"},
        }
    }

    assert receipt_created_at_attribute(record) == _AT


class _RecordingSqs:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def send_message(self, **request: Any) -> dict[str, Any]:
        self.requests.append(request)
        return {"MessageId": "message-01"}


def _envelope() -> EvaluationEnvelope:
    return EvaluationEnvelope(
        question="What is the fictional governed result?",
        released_answer="The fictional released answer.",
        correlation_id="123e4567-e89b-42d3-a456-426614174000",
        audit_id="audit-01",
        evaluation_admission_ref="admission-01",
    )


def test_the_evaluation_sink_sends_the_receipt_time_beside_an_unchanged_body() -> None:
    sqs = _RecordingSqs()
    sink = SqsEvaluationSink("https://sqs.test.invalid/queue", cast("Any", sqs))

    assert sink.enqueue(_envelope(), _AT) == "message-01"

    (request,) = sqs.requests
    assert request["MessageAttributes"] == {
        "receipt_created_at": {"DataType": "String", "StringValue": _AT}
    }
    body = json.loads(request["MessageBody"])
    assert body["schema_version"] == 3
    assert _AT not in request["MessageBody"]


def test_the_evaluation_sink_refuses_a_receipt_time_that_is_not_a_utc_instant() -> None:
    sqs = _RecordingSqs()
    sink = SqsEvaluationSink("https://sqs.test.invalid/queue", cast("Any", sqs))

    with pytest.raises(InvariantError) as raised:
        sink.enqueue(_envelope(), "2026-09-02T12:00:00")

    assert raised.value.code == ErrorCode("EVALUATION", 2)
    assert sqs.requests == []
