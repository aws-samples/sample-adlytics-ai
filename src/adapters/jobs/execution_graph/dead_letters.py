# SPDX-License-Identifier: MIT-0
"""Dead-letter status and redrive for the four graph routes (S6 part 2, design §2.12).

The human-only operator CLI (`operator.py`) runs these under the bound operator profile; no
Lambda or agent principal holds the SQS actions they use (§2.11 row 10). Every graph route has
one dead-letter queue, addressed by its deterministic name `adlytics-<env>-<suffix>`:

| `--queue` | Source queue suffix | Dead-letter queue suffix |
|---|---|---|
| `delivery` | `ingestion` | `ingestion-dlq` |
| `semantic-write` | `semantic-write.fifo` | `semantic-write-dlq.fifo` |
| `bake` | `preset-bake.fifo` | `preset-bake-dlq.fifo` |
| `precompute` | `insight-precompute.fifo` | `insight-precompute-dlq.fifo` |

`dlq_status` is read-only: each dead-letter queue's visible and in-flight counts and its most
recent message-move task. `redrive` refuses an unknown queue, a visible count other than the
operator's `--expected-count`, any in-flight dead letter, and a latest move task that is still
`RUNNING` or `CANCELLING`; only a quiescent queue starts one SQS message-move task from the
dead-letter queue back to its source. The counts are SQS's approximate attributes and the move
carries no inspection token, so `--expected-count` is a precondition read just before the move,
not a compare-and-swap: a message arriving between the read and the move is moved with the rest.
The move-task statuses are `RUNNING`, `COMPLETED`, `CANCELLING`, `CANCELLED` and `FAILED` (SQS API
Reference, `ListMessageMoveTasksResultEntry`, read 2026-10-10 [verified]). SQS supports a move
task for a FIFO dead-letter queue to a FIFO destination, and gives each moved message a new
message ID and enqueue time (SQS Developer Guide, "Configure a dead-letter queue redrive", read
2026-10-09 [verified]), so no receive/re-send fallback is needed. A redriven envelope needs no
new graph state: the claim decision reads `RECLAIM`, `ALREADY_SETTLED` or the takeover path
(§2.12).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from domain.operations.execution_graph import GraphCommandRefusedError

__all__ = [
    "ACTIVE_MOVE_STATUSES",
    "DEAD_LETTER_QUEUES",
    "REDRIVE_COUNT_MISMATCH",
    "REDRIVE_IN_FLIGHT",
    "REDRIVE_MOVE_ACTIVE",
    "REDRIVE_QUEUE_UNKNOWN",
    "DeadLetterRoute",
    "dead_letter_route",
    "dlq_status",
    "redrive",
]

REDRIVE_QUEUE_UNKNOWN: Final = "REDRIVE_QUEUE_UNKNOWN"
REDRIVE_COUNT_MISMATCH: Final = "REDRIVE_COUNT_MISMATCH"
REDRIVE_IN_FLIGHT: Final = "REDRIVE_IN_FLIGHT"
REDRIVE_MOVE_ACTIVE: Final = "REDRIVE_MOVE_ACTIVE"
ACTIVE_MOVE_STATUSES: Final = frozenset({"RUNNING", "CANCELLING"})
"""The move-task statuses that are not terminal: a queue with one is not quiescent."""
DEAD_LETTER_QUEUES: Final = {
    "delivery": ("ingestion", "ingestion-dlq"),
    "semantic-write": ("semantic-write.fifo", "semantic-write-dlq.fifo"),
    "bake": ("preset-bake.fifo", "preset-bake-dlq.fifo"),
    "precompute": ("insight-precompute.fifo", "insight-precompute-dlq.fifo"),
}
"""`--queue` name to the (source, dead-letter) queue name suffixes after `adlytics-<env>-`."""
_COUNTS: Final = ("ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible")


@dataclass(frozen=True, slots=True)
class DeadLetterRoute:
    """One graph route's source queue and dead-letter queue, by deterministic name."""

    queue: str
    source: str
    dead_letter: str


def dead_letter_route(environment: str, queue: str) -> DeadLetterRoute:
    """Return the named route's queues, or refuse a name outside the four graph routes."""
    names = DEAD_LETTER_QUEUES.get(queue)
    if names is None:
        raise GraphCommandRefusedError(
            REDRIVE_QUEUE_UNKNOWN,
            f"the queue must be one of {', '.join(sorted(DEAD_LETTER_QUEUES))}.",
        )
    source, dead_letter = names
    return DeadLetterRoute(
        queue, f"adlytics-{environment}-{source}", f"adlytics-{environment}-{dead_letter}"
    )


def _attributes(sqs: Any, name: str) -> dict[str, str]:
    url = str(sqs.get_queue_url(QueueName=name)["QueueUrl"])
    attributes = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=[*_COUNTS, "QueueArn"])
    return {str(key): str(value) for key, value in attributes.get("Attributes", {}).items()}


def _latest_move(sqs: Any, source_arn: str) -> dict[str, object] | None:
    tasks = sqs.list_message_move_tasks(SourceArn=source_arn, MaxResults=1).get("Results", [])
    if not tasks:
        return None
    task = tasks[0]
    return {
        name: task.get(field)
        for name, field in (
            ("status", "Status"),
            ("moved", "ApproximateNumberOfMessagesMoved"),
            ("to_move", "ApproximateNumberOfMessagesToMove"),
            ("started_at_ms", "StartedTimestamp"),
            ("failure_reason", "FailureReason"),
        )
        if task.get(field) is not None
    }


def dlq_status(sqs: Any, environment: str) -> dict[str, object]:
    """Read each route's dead-letter counts and latest move task; never writes."""
    queues: dict[str, object] = {}
    for queue in sorted(DEAD_LETTER_QUEUES):
        route = dead_letter_route(environment, queue)
        attributes = _attributes(sqs, route.dead_letter)
        queues[queue] = {
            "dead_letter_queue": route.dead_letter,
            "source_queue": route.source,
            "visible": int(attributes[_COUNTS[0]]),
            "in_flight": int(attributes[_COUNTS[1]]),
            "latest_move_task": _latest_move(sqs, attributes["QueueArn"]),
        }
    return {"command": "dlq-status", "queues": queues}


def redrive(sqs: Any, route: DeadLetterRoute, expected_count: int) -> dict[str, object]:
    """Move one route's dead letters back to its source, only from a quiescent queue.

    Quiescent: the visible count equals `expected_count`, nothing is in flight, and the latest
    move task (if any) is terminal. Each is read immediately before the move; none is a lock.
    """
    attributes = _attributes(sqs, route.dead_letter)
    visible, in_flight = int(attributes[_COUNTS[0]]), int(attributes[_COUNTS[1]])
    if visible != expected_count:
        raise GraphCommandRefusedError(
            REDRIVE_COUNT_MISMATCH,
            f"{route.dead_letter} holds {visible} visible messages, not {expected_count}.",
        )
    if in_flight:
        raise GraphCommandRefusedError(
            REDRIVE_IN_FLIGHT,
            f"{route.dead_letter} has {in_flight} in-flight messages; retry once they settle.",
        )
    latest = _latest_move(sqs, attributes["QueueArn"])
    if latest is not None and latest.get("status") in ACTIVE_MOVE_STATUSES:
        raise GraphCommandRefusedError(
            REDRIVE_MOVE_ACTIVE,
            f"{route.dead_letter}'s latest move task is {latest['status']}; wait for it to end.",
        )
    result: dict[str, object] = {
        "command": "redrive",
        "queue": route.queue,
        "dead_letter_queue": route.dead_letter,
        "source_queue": route.source,
        "visible": visible,
        "in_flight": in_flight,
        "latest_move_task": latest,
    }
    if visible == 0:
        return {**result, "started": False, "task_handle": None}
    source = _attributes(sqs, route.source)
    task = sqs.start_message_move_task(
        SourceArn=attributes["QueueArn"], DestinationArn=source["QueueArn"]
    )
    return {**result, "started": True, "task_handle": str(task["TaskHandle"])}
