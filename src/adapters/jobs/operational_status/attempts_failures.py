# SPDX-License-Identifier: MIT-0
r"""Operator disposition of the stream failure queues (operator-tools.md).

When a bounded stream consumer exhausts its retries on a stream batch, Lambda sends the batch's
stream metadata (`DDBStreamBatchInfo`: shard, sequence range, arrival times, size) to that
consumer's failure queue (`STREAM_FAILURE_QUEUE_SUFFIXES`), and its arrivals alarm pages once.
Nothing consumes those queues. `--consumer` names the one queue; it is required, never
defaulted. These two commands are its human disposition, each printing one `RESULT {json}` line:

- `status`, read-only: the queue's visible, in-flight and delayed counts.
- `drain --expected-count N --evidence <new file>`: refuses unless exactly N messages are visible
  and none is in flight; receives exactly N, refuses (and releases them) if any is not a
  stream-batch failure record, writes every record to the evidence file, and only then deletes
  them. There is no redrive: a message holds metadata, not the stream records, and the attempt
  authority and publications those records described are unaffected.

    python -m adapters.jobs.operational_status.attempts_failures status --consumer <c> \\
        --profile <p> --region <r> --expected-account <account> --env <env>
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final, cast

import boto3

from adapters.outbound.cloudformation_stack import stack_resource_summaries

__all__ = [
    "STREAM_FAILURE_QUEUE_STACKS",
    "STREAM_FAILURE_QUEUE_SUFFIXES",
    "DrainRefusedError",
    "drain",
    "main",
    "queue_status",
    "resolve_queue_url",
]

# Consumer -> the suffix of its failure queue name after `adlytics-<env>`
# (`infra/test/stream-failure-queues.test.ts` holds this equal to the synthesized queues).
STREAM_FAILURE_QUEUE_SUFFIXES: Final[Mapping[str, str]] = {
    "attempts": "-operational-status-attempts-failures",
    "graph-projector": "-graph-projector-stream-failures",
    "stage1-publications": "-stage1-publications-stream-failures",
    "agent-quality-audit": "-agent-quality-audit-stream-failures",
    "insight-precompute": "-insight-precompute-stream-failures",
    "publisher-enqueue": "-publisher-enqueue-stream-failures",
}
# Consumer -> the stack (before `-<env>`) that owns its failure queue.
STREAM_FAILURE_QUEUE_STACKS: Final[Mapping[str, str]] = {
    "attempts": "Adlytics-OperationalStatusProcessor",
    "graph-projector": "Adlytics-OperationalStatusProcessor",
    "stage1-publications": "Adlytics-OperationalStatusProcessor",
    "agent-quality-audit": "Adlytics-AgentQualityProcessor",
    "insight-precompute": "Adlytics-InsightPrecompute",
    "publisher-enqueue": "Adlytics-Publisher",
}
_RECEIVE_BATCH: Final = 10
_EMPTY_POLLS: Final = 3
_HOLD_SECONDS: Final = 120
EXPECTED_COUNT_MISMATCH: Final = "EXPECTED_COUNT_MISMATCH"
MALFORMED_FAILURE_RECORD: Final = "MALFORMED_FAILURE_RECORD"
EVIDENCE_EXISTS: Final = "EVIDENCE_EXISTS"


class DrainRefusedError(Exception):
    """A drain stopped before any delete; `reason` is one stable refusal name."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason


def queue_status(sqs: Any, queue_url: str) -> dict[str, int]:
    """Read the three counts (read-only)."""
    attributes = sqs.get_queue_attributes(
        QueueUrl=queue_url,
        AttributeNames=[
            "ApproximateNumberOfMessages",
            "ApproximateNumberOfMessagesNotVisible",
            "ApproximateNumberOfMessagesDelayed",
        ],
    )["Attributes"]
    return {
        "visible": int(attributes["ApproximateNumberOfMessages"]),
        "in_flight": int(attributes["ApproximateNumberOfMessagesNotVisible"]),
        "delayed": int(attributes["ApproximateNumberOfMessagesDelayed"]),
    }


def _record(message: Mapping[str, Any]) -> dict[str, object] | None:
    """Return the stream-batch failure record a message carries, or None if it is not one."""
    try:
        parsed: object = json.loads(str(message["Body"]))
    except KeyError, ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    body = cast("dict[str, object]", parsed)
    batch = body.get("DDBStreamBatchInfo")
    if not isinstance(batch, dict):
        return None
    info = cast("dict[str, object]", batch)
    if not {"shardId", "startSequenceNumber"} <= set(info):
        return None
    context = body.get("requestContext")
    condition = (
        cast("dict[str, object]", context).get("condition") if isinstance(context, dict) else None
    )
    return {
        "message_id": str(message.get("MessageId", "")),
        "condition": condition,
        "timestamp": body.get("timestamp"),
        "batch": info,
    }


def _release(sqs: Any, queue_url: str, messages: Sequence[Mapping[str, Any]]) -> None:
    for message in messages:
        sqs.change_message_visibility(
            QueueUrl=queue_url, ReceiptHandle=message["ReceiptHandle"], VisibilityTimeout=0
        )


def drain(  # noqa: PLR0913 -- client, queue, and the two operator-stated drain inputs
    sqs: Any, queue_url: str, *, expected_count: int, evidence: Path
) -> dict[str, object]:
    """Record then delete exactly `expected_count` failure records, or delete nothing."""
    if evidence.exists():
        raise DrainRefusedError(EVIDENCE_EXISTS, "the evidence file already exists")
    before = queue_status(sqs, queue_url)
    if before["visible"] != expected_count or before["in_flight"] or before["delayed"]:
        raise DrainRefusedError(EXPECTED_COUNT_MISMATCH, f"the queue reads {before}")
    received: dict[str, Mapping[str, Any]] = {}
    empty = 0
    while len(received) < expected_count and empty < _EMPTY_POLLS:
        batch = sqs.receive_message(
            QueueUrl=queue_url,
            MaxNumberOfMessages=min(_RECEIVE_BATCH, expected_count - len(received)),
            VisibilityTimeout=_HOLD_SECONDS,
            WaitTimeSeconds=1,
        ).get("Messages", [])
        empty = empty + 1 if not batch else 0
        for message in batch:
            received[str(message["MessageId"])] = message
    messages = list(received.values())
    if len(messages) != expected_count:
        _release(sqs, queue_url, messages)
        raise DrainRefusedError(
            EXPECTED_COUNT_MISMATCH, f"received {len(messages)} of {expected_count}"
        )
    records = [_record(message) for message in messages]
    if any(record is None for record in records):
        _release(sqs, queue_url, messages)
        raise DrainRefusedError(MALFORMED_FAILURE_RECORD, "a message is not a stream-batch record")
    evidence.parent.mkdir(parents=True, exist_ok=True)
    with evidence.open("x") as handle:
        json.dump({"queue_url": queue_url, "before": before, "records": records}, handle, indent=1)
    for message in messages:
        sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=message["ReceiptHandle"])
    return {"drained": len(messages), "before": before, "after": queue_status(sqs, queue_url)}


def resolve_queue_url(session: Any, consumer: str, environment: str) -> str:
    """Resolve the one failure queue the consumer's exact owning stack holds."""
    stack = f"{STREAM_FAILURE_QUEUE_STACKS[consumer]}-{environment}"
    suffix = STREAM_FAILURE_QUEUE_SUFFIXES[consumer]
    queue = f"/adlytics-{environment}{suffix}"
    # A queue with no physical id yet is not the one queue, so the count below refuses it.
    urls = [
        url
        for item in stack_resource_summaries(session.client("cloudformation"), stack)
        if item["ResourceType"] == "AWS::SQS::Queue"
        and (url := item.get("PhysicalResourceId", "")).endswith(queue)
    ]
    if len(urls) != 1:
        raise PermissionError(f"the {consumer} failure queue is not exactly one stack resource")
    return urls[0]


def _print(document: Mapping[str, object]) -> None:
    sys.stdout.write("RESULT " + json.dumps(dict(document), sort_keys=True, default=str) + "\n")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("status", "drain"))
    parser.add_argument("--consumer", required=True, choices=tuple(STREAM_FAILURE_QUEUE_SUFFIXES))
    for name in ("--profile", "--region", "--expected-account", "--env"):
        parser.add_argument(name, required=True)
    parser.add_argument("--expected-count", type=int)
    parser.add_argument("--evidence", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run one command; a refused input or drain exits 2 and prints why, deleting nothing."""
    arguments = _parser().parse_args(argv)
    try:
        if str(arguments.profile).casefold() == "default":
            raise PermissionError("an explicit nondefault profile is required")
        if arguments.command == "drain" and (
            arguments.expected_count is None
            or arguments.expected_count < 1
            or not arguments.evidence
        ):
            raise PermissionError("drain needs --expected-count >= 1 and --evidence")
        session = boto3.Session(profile_name=arguments.profile, region_name=arguments.region)
        identity = session.client("sts").get_caller_identity()
        if str(identity.get("Account", "")) != str(arguments.expected_account):
            raise PermissionError("caller account does not match the explicitly expected account")
        queue_url = resolve_queue_url(session, str(arguments.consumer), str(arguments.env))
        sqs = session.client("sqs")
        if arguments.command == "status":
            _print({"command": "status", "queue_url": queue_url, **queue_status(sqs, queue_url)})
            return 0
        result = drain(
            sqs,
            queue_url,
            expected_count=int(arguments.expected_count),
            evidence=Path(arguments.evidence),
        )
        _print({"command": "drain", "queue_url": queue_url, **result})
        return 0
    except PermissionError as error:
        _print({"command": arguments.command, "refused": str(error)})
        return 2
    except DrainRefusedError as error:
        _print({"command": arguments.command, "refused": error.reason, "detail": str(error)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
