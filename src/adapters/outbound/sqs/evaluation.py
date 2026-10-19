# SPDX-License-Identifier: MIT-0
"""SQS admission for released-response evaluation envelopes."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import boto3

from adapters.outbound.sqs.attributes import receipt_created_at_message_attribute
from domain.evaluation.envelope import EvaluationEnvelope
from domain.shared_kernel.errors import DependencyError, ErrorCode, InvariantError
from ports.outbound.evaluation import EvaluationSinkPort

if TYPE_CHECKING:
    from types_boto3_sqs import SQSClient

EVALUATION_UNWRITABLE = ErrorCode("EVALUATION", 1)
EVALUATION_INVALID = ErrorCode("EVALUATION", 2)
MAX_MESSAGE_BYTES = 256 * 1024


class SqsEvaluationSink(EvaluationSinkPort):
    """Durably enqueue an exact post-release pair without logging content."""

    def __init__(self, queue_url: str, client: SQSClient | None = None) -> None:
        if not queue_url.strip():
            raise ValueError("evaluation queue URL is required.")
        self._queue_url = queue_url
        self._client = client

    @property
    def _sqs(self) -> SQSClient:
        if self._client is None:
            self._client = boto3.client("sqs")
        return self._client

    def enqueue(self, envelope: EvaluationEnvelope, receipt_created_at: str) -> str:
        """Serialize one versioned envelope and return its opaque SQS delivery reference.

        The receipt creation time is a message attribute, so the versioned body is unchanged.
        """
        try:
            attributes = receipt_created_at_message_attribute(receipt_created_at)
        except ValueError as error:
            raise InvariantError(
                EVALUATION_INVALID, "Evaluation receipt creation time is not a UTC instant."
            ) from error
        try:
            document: dict[str, str | int] = {
                "audit_id": envelope.audit_id,
                "correlation_id": envelope.correlation_id,
                "evaluation_admission_ref": envelope.evaluation_admission_ref,
                "grounded_context": envelope.grounded_context,
                "question": envelope.question,
                "released_answer": envelope.released_answer,
                "schema_version": 3,
            }
            body = json.dumps(
                document,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
        except (TypeError, ValueError) as error:
            raise InvariantError(
                EVALUATION_INVALID, "Evaluation envelope is not serializable."
            ) from error
        if len(body.encode("utf-8")) > MAX_MESSAGE_BYTES:
            raise InvariantError(EVALUATION_INVALID, "Evaluation envelope exceeds queue capacity.")
        try:
            response = self._sqs.send_message(
                QueueUrl=self._queue_url,
                MessageBody=body,
                MessageAttributes=attributes,
            )
            delivery_ref = response.get("MessageId")
        except Exception as error:
            raise DependencyError(
                EVALUATION_UNWRITABLE,
                "Released response could not be admitted for evaluation.",
                context={"cause": type(error).__name__},
            ) from None
        if not delivery_ref:
            raise DependencyError(
                EVALUATION_UNWRITABLE,
                "Released response admission returned no delivery reference.",
                context={"cause": "missing_message_id"},
            )
        return delivery_ref
