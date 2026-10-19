# SPDX-License-Identifier: MIT-0
"""Content-free SQS admission and decoding for prompt calibration work."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from adapters.outbound.aws_errors import merged_aws_failure_context
from domain.presentation.insights import (
    DETERMINISTIC_SLOT_IDS,
    PROMPT_CALIBRATION_QUEUE_UNAVAILABLE,
    InsightSlotId,
)
from domain.shared_kernel.errors import DependencyError
from ports.outbound.insights import InsightPromptCalibrationQueuePort

if TYPE_CHECKING:  # pragma: no cover
    from types_boto3_sqs.client import SQSClient

_SCHEMA = "InsightPromptCalibrationWork.v1"


@dataclass(frozen=True, slots=True)
class InsightPromptCalibrationWork:
    """One immutable registry identity carried without prompt or model content."""

    slot_id: InsightSlotId
    version: int


def parse(value: object) -> InsightPromptCalibrationWork:
    """Decode one exact queue body; malformed owned work must reach the DLQ alarm."""
    if not isinstance(value, str):
        raise ValueError("prompt calibration work body must be JSON text.")
    try:
        document: object = json.loads(value)
    except json.JSONDecodeError as error:
        raise ValueError("prompt calibration work body is malformed.") from error
    if not isinstance(document, dict):
        raise ValueError("prompt calibration work body must be an object.")
    body = cast("dict[str, object]", document)
    if (
        set(body) != {"schema_version", "slot_id", "version"}
        or body.get("schema_version") != _SCHEMA
    ):
        raise ValueError("prompt calibration work fields are invalid.")
    slot = body.get("slot_id")
    version = body.get("version")
    if not isinstance(slot, str) or type(version) is not int or version < 1:
        raise ValueError("prompt calibration work identity is invalid.")
    try:
        slot_id = InsightSlotId(slot)
    except ValueError as error:
        raise ValueError("prompt calibration work slot is invalid.") from error
    if slot_id in DETERMINISTIC_SLOT_IDS:
        raise ValueError("deterministic slots cannot receive prompt calibration work.")
    return InsightPromptCalibrationWork(slot_id, version)


def encode(slot_id: InsightSlotId, version: int) -> str:
    """Encode one exact content-free work identity."""
    return json.dumps(
        {"schema_version": _SCHEMA, "slot_id": slot_id.value, "version": version},
        separators=(",", ":"),
        sort_keys=True,
    )


class SqsInsightPromptCalibrationQueue(InsightPromptCalibrationQueuePort):
    """Durably admit pending version identities to the calibration worker."""

    def __init__(self, queue_url: str, client: SQSClient | None = None) -> None:
        if not queue_url.strip():
            raise ValueError("prompt calibration queue URL is required.")
        self._queue_url = queue_url
        self._client = client

    @property
    def _sqs(self) -> SQSClient:
        if self._client is None:
            self._client = boto3.client("sqs")
        return self._client

    def enqueue(self, slot_id: InsightSlotId, version: int) -> str:
        """Return the provider message ID only after SQS accepted the work."""
        body = encode(slot_id, version)
        try:
            response = self._sqs.send_message(QueueUrl=self._queue_url, MessageBody=body)
        except (BotoCoreError, ClientError) as error:
            raise DependencyError(
                PROMPT_CALIBRATION_QUEUE_UNAVAILABLE,
                "Prompt calibration could not be queued.",
                context=merged_aws_failure_context(
                    error, {"slot": slot_id.value, "version": version}
                ),
            ) from None
        message_id: Any = response.get("MessageId")
        if not isinstance(message_id, str) or not message_id:
            raise DependencyError(
                PROMPT_CALIBRATION_QUEUE_UNAVAILABLE,
                "Prompt calibration could not be queued.",
                context={"slot": slot_id.value, "version": version},
            )
        return message_id
