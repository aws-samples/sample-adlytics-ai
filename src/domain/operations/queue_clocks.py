# SPDX-License-Identifier: MIT-0
"""N2 queue clocks for semantic FIFO messages (design.md, "Identity, topology, and clocks").

A graph envelope's queue clocks come from graph events plus the SQS record's own attributes:
`SentTimestamp` is `queue_enqueued_at` and `ApproximateFirstReceiveTimestamp` is
`first_received_at`. A redelivery never replaces the first-receive value. For complete
predecessor evidence the enqueue-to-first-receive time is partitioned around the predecessor's
semantic terminal; with an authoritative provider release instant, around that release too. No
unknown time is guessed or moved: a missing attribute is `unknown_queue_clock`, a declared
predecessor with no terminal is `unknown_fifo_predecessor`, a negative interval is
`clock_invalid`, and the managed event-source mapping exposes no release instant, so its normal
result is `queue_release_clock_unavailable`.

Pure: no I/O, no clock, no AWS.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

__all__ = [
    "CLOCK_INVALID",
    "QUEUE_RELEASE_CLOCK_UNAVAILABLE",
    "UNKNOWN_FIFO_PREDECESSOR",
    "UNKNOWN_QUEUE_CLOCK",
    "QueueClockInputs",
    "QueueClocks",
    "first_receive",
    "queue_attributes",
    "queue_clocks",
]

UNKNOWN_QUEUE_CLOCK: Final = "unknown_queue_clock"
UNKNOWN_FIFO_PREDECESSOR: Final = "unknown_fifo_predecessor"
QUEUE_RELEASE_CLOCK_UNAVAILABLE: Final = "queue_release_clock_unavailable"
CLOCK_INVALID: Final = "clock_invalid"
_SENT: Final = "SentTimestamp"
_FIRST_RECEIVE: Final = "ApproximateFirstReceiveTimestamp"


def _epoch_ms(value: str | None) -> datetime | None:
    if value is None or not value.isdecimal():
        return None
    return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=int(value))


def queue_attributes(attributes: Mapping[str, str]) -> tuple[datetime | None, datetime | None]:
    """Return `(queue_enqueued_at, first_received_at)` from one SQS record's attributes."""
    return _epoch_ms(attributes.get(_SENT)), _epoch_ms(attributes.get(_FIRST_RECEIVE))


def first_receive(recorded: datetime | None, observed: datetime | None) -> datetime | None:
    """Keep the first recorded first-receive instant; a redelivery never replaces it."""
    return recorded if recorded is not None else observed


@dataclass(frozen=True, slots=True)
class QueueClockInputs:
    """One envelope's queue facts and its FIFO predecessor's evidence."""

    queue_enqueued_at: datetime | None
    first_received_at: datetime | None
    first_in_group: bool
    """The immutable dispatch roster marks this envelope first in its FIFO group."""
    predecessor_terminal_at: datetime | None
    """The preceding activity's semantic terminal, from its durable unit authority only."""
    queue_release_at: datetime | None = None
    """Only an authoritative provider receipt populates this (`provider_queue_release`)."""


@dataclass(frozen=True, slots=True)
class QueueClocks:
    """The partitioned enqueue-to-first-receive time and every typed gap."""

    semantic_predecessor_overlap_ms: int | None
    post_semantic_terminal_pre_receive_ms: int | None
    provider_head_of_line_block_ms: int | None
    post_queue_release_pre_receive_ms: int | None
    gaps: tuple[str, ...]


def _ms(delta: timedelta) -> int:
    return int(delta // timedelta(milliseconds=1))


def _partition(enqueued: datetime, received: datetime, boundary: datetime) -> tuple[int, int]:
    overlap = max(0, _ms(min(received, boundary) - enqueued))
    after = max(0, _ms(received - max(enqueued, boundary)))
    return overlap, after


def queue_clocks(inputs: QueueClockInputs) -> QueueClocks:
    """Compute the N2 partitions, or name the gap that prevents each one."""
    enqueued, received = inputs.queue_enqueued_at, inputs.first_received_at
    if enqueued is None or received is None:
        return QueueClocks(None, None, None, None, (UNKNOWN_QUEUE_CLOCK,))
    if received < enqueued:
        return QueueClocks(None, None, None, None, (CLOCK_INVALID,))
    gaps: list[str] = []
    overlap: int | None
    after: int | None
    if inputs.first_in_group:
        overlap, after = 0, _ms(received - enqueued)
    elif inputs.predecessor_terminal_at is None:
        overlap, after = None, None
        gaps.append(UNKNOWN_FIFO_PREDECESSOR)
    else:
        overlap, after = _partition(enqueued, received, inputs.predecessor_terminal_at)
    block: int | None = None
    post_release: int | None = None
    if inputs.queue_release_at is None:
        gaps.append(QUEUE_RELEASE_CLOCK_UNAVAILABLE)
    else:
        block, post_release = _partition(enqueued, received, inputs.queue_release_at)
    return QueueClocks(overlap, after, block, post_release, tuple(gaps))
