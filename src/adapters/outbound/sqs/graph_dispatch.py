# SPDX-License-Identifier: MIT-0
"""SQS senders for the graph dispatcher (S3, BF-2; S6 §2.4 routing).

`SqsGraphSender` sends exactly the persisted envelope bytes to a FIFO queue with
`MessageGroupId=unit_key` and the node's one logical dispatch identity as
`MessageDeduplicationId`. `SqsStandardGraphSender` sends the same bytes to a standard queue (the
delivery queue, `route: delivery`) and omits both identifiers, which a standard queue does not
take. A provider rejection for size is a `ProviderOversizeError` (an immutable node failure, or
for a POST_GRANT node an invariant); any other failure is a dependency error, and the outbox row
stays `SENDING` for the next pass to re-send the same bytes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final, cast

from botocore.exceptions import BotoCoreError, ClientError

from adapters.jobs.execution_graph.dispatcher import ProviderOversizeError
from adapters.outbound.aws_errors import merged_aws_failure_context
from domain.shared_kernel.errors import DependencyError, ErrorCode
from ports.outbound.execution_graph_dispatch import GraphMessage

if TYPE_CHECKING:
    from types_boto3_sqs.client import SQSClient

__all__ = ["SqsGraphSender", "SqsStandardGraphSender"]

_UNAVAILABLE: Final = ErrorCode("EXEC_GRAPH", 25)
_OVERSIZE_CODES: Final = frozenset({"InvalidParameterValue", "MessageTooLong"})


def _send(client: SQSClient, arguments: dict[str, Any]) -> str:
    """Send one message; map an oversize rejection and every other failure to its type."""
    try:
        response = client.send_message(**arguments)
    except ClientError as error:
        body = cast("dict[str, Any]", error.response).get("Error", {})
        code, message = str(body.get("Code", "")), str(body.get("Message", ""))
        if code in _OVERSIZE_CODES and "shorter than" in message:
            raise ProviderOversizeError(code) from None
        raise DependencyError(
            _UNAVAILABLE,
            "The graph envelope could not be sent.",
            context=merged_aws_failure_context(error),
        ) from error
    except BotoCoreError as error:
        raise DependencyError(
            _UNAVAILABLE,
            "The graph envelope could not be sent.",
            context={"cause": type(error).__name__},
        ) from error
    return str(response.get("MessageId", ""))


def _queue_url(queue_url: str) -> str:
    if not queue_url.strip():
        raise ValueError("the graph dispatcher needs every route's queue URL.")
    return queue_url


class SqsGraphSender:
    """Send one node's envelope to a graph FIFO queue (semantic write, bake, precompute)."""

    def __init__(self, queue_url: str, client: SQSClient) -> None:
        self._queue_url = _queue_url(queue_url)
        self._client = client

    def send(self, message: GraphMessage) -> str:
        """Send the exact bytes with the group and deduplication identity; return the ID."""
        return _send(
            self._client,
            {
                "QueueUrl": self._queue_url,
                "MessageBody": message.body,
                "MessageGroupId": message.group_id,
                "MessageDeduplicationId": message.deduplication_id,
            },
        )


class SqsStandardGraphSender:
    """Send one node's envelope to a standard queue, with no group or deduplication ID."""

    def __init__(self, queue_url: str, client: SQSClient) -> None:
        self._queue_url = _queue_url(queue_url)
        self._client = client

    def send(self, message: GraphMessage) -> str:
        """Send the exact bytes; return the provider message ID."""
        return _send(self._client, {"QueueUrl": self._queue_url, "MessageBody": message.body})
