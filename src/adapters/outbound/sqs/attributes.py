# SPDX-License-Identifier: MIT-0
"""The receipt-creation-time message attribute, sent and read on both evaluation queues.

The evaluation writer and the result linker never read the receipts table, so each receives
its turn's receipt creation time on the message it already consumes and computes its record's
expiry from it (ADR-080 §1, ADR-081 §5). The time rides as an SQS message attribute, so neither
queue's versioned body changes. Both consumers read it strictly: this one String attribute,
holding a UTC ISO instant, or the record is refused and retried. Other attributes are ignored.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from domain.conversation.audit import require_utc_instant

if TYPE_CHECKING:
    from collections.abc import Mapping

    from types_boto3_sqs.type_defs import MessageAttributeValueTypeDef

RECEIPT_CREATED_AT = "receipt_created_at"
_EVENT_ATTRIBUTE_KEYS = frozenset(
    {"stringValue", "stringListValues", "binaryListValues", "dataType"}
)


def receipt_created_at_message_attribute(
    receipt_created_at: str,
) -> dict[str, MessageAttributeValueTypeDef]:
    """Render the one attribute a producer sends, refusing anything but a UTC instant."""
    require_utc_instant(receipt_created_at, "receipt creation time")
    return {RECEIPT_CREATED_AT: {"DataType": "String", "StringValue": receipt_created_at}}


def receipt_created_at_attribute(record: Mapping[str, object]) -> str:
    """Read the attribute from one Lambda SQS event record, exactly as Lambda delivers it.

    Lambda renders each attribute as `stringValue`, `stringListValues`, `binaryListValues`, and
    `dataType` ([Lambda SQS event](https://docs.aws.amazon.com/lambda/latest/dg/with-sqs.html)).
    Another type, key, or a non-empty list value on this attribute is refused rather than
    ignored. Unrelated attributes are ignored: an instrumentation upgrade that starts injecting
    trace propagation into SQS sends must not dead-letter every message.
    """
    raw = record.get("messageAttributes")
    if not isinstance(raw, dict):
        raise ValueError("evaluation queue record carries no message attributes.")
    attributes = cast("dict[str, object]", raw)
    if RECEIPT_CREATED_AT not in attributes:
        raise ValueError("evaluation queue record carries no receipt creation time.")
    value = attributes[RECEIPT_CREATED_AT]
    if not isinstance(value, dict):
        raise ValueError("evaluation queue receipt creation time attribute is malformed.")
    attribute = cast("dict[str, object]", value)
    text = attribute.get("stringValue")
    if (
        not set(attribute) <= _EVENT_ATTRIBUTE_KEYS
        or attribute.get("dataType") != "String"
        or not isinstance(text, str)
        or attribute.get("stringListValues", []) != []
        or attribute.get("binaryListValues", []) != []
    ):
        raise ValueError("evaluation queue receipt creation time attribute is malformed.")
    require_utc_instant(text, "receipt creation time")
    return text
