# SPDX-License-Identifier: MIT-0
"""N2 queue clocks: graph events plus SQS attributes, with every gap named (S3)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from domain.operations.queue_clocks import (
    CLOCK_INVALID,
    QUEUE_RELEASE_CLOCK_UNAVAILABLE,
    UNKNOWN_FIFO_PREDECESSOR,
    UNKNOWN_QUEUE_CLOCK,
    QueueClockInputs,
    first_receive,
    queue_attributes,
    queue_clocks,
)

pytestmark = pytest.mark.unit

T0 = datetime(2026, 10, 7, 9, 0, tzinfo=UTC)


def _ms(value: int) -> datetime:
    return T0 + timedelta(milliseconds=value)


def test_sqs_attributes_map_to_enqueue_and_first_receive() -> None:
    epoch = int(T0.timestamp() * 1000)
    enqueued, received = queue_attributes(
        {"SentTimestamp": str(epoch), "ApproximateFirstReceiveTimestamp": str(epoch + 250)}
    )
    assert enqueued == T0
    assert received == _ms(250)


def test_a_redelivery_never_replaces_the_first_receive() -> None:
    assert first_receive(_ms(100), _ms(900)) == _ms(100)
    assert first_receive(None, _ms(900)) == _ms(900)


def test_the_partition_around_the_predecessor_terminal_sums_to_enqueue_to_receive() -> None:
    clocks = queue_clocks(QueueClockInputs(_ms(0), _ms(1000), False, _ms(400)))
    assert clocks.semantic_predecessor_overlap_ms == 400
    assert clocks.post_semantic_terminal_pre_receive_ms == 600
    assert clocks.provider_head_of_line_block_ms is None
    assert clocks.gaps == (QUEUE_RELEASE_CLOCK_UNAVAILABLE,)


def test_a_first_in_group_marker_makes_the_overlap_zero() -> None:
    clocks = queue_clocks(QueueClockInputs(_ms(0), _ms(700), True, None))
    assert clocks.semantic_predecessor_overlap_ms == 0
    assert clocks.post_semantic_terminal_pre_receive_ms == 700


def test_a_predecessor_terminal_after_the_receive_is_all_overlap() -> None:
    clocks = queue_clocks(QueueClockInputs(_ms(0), _ms(500), False, _ms(800)))
    assert clocks.semantic_predecessor_overlap_ms == 500
    assert clocks.post_semantic_terminal_pre_receive_ms == 0


def test_an_authoritative_release_instant_partitions_the_provider_components() -> None:
    clocks = queue_clocks(QueueClockInputs(_ms(0), _ms(1000), True, None, _ms(300)))
    assert clocks.provider_head_of_line_block_ms == 300
    assert clocks.post_queue_release_pre_receive_ms == 700
    assert clocks.gaps == ()


@pytest.mark.parametrize(
    ("inputs", "gap"),
    [
        (QueueClockInputs(None, _ms(10), True, None), UNKNOWN_QUEUE_CLOCK),
        (QueueClockInputs(_ms(10), None, True, None), UNKNOWN_QUEUE_CLOCK),
        (QueueClockInputs(_ms(10), _ms(5), True, None), CLOCK_INVALID),
        (QueueClockInputs(_ms(0), _ms(5), False, None), UNKNOWN_FIFO_PREDECESSOR),
    ],
)
def test_unknown_time_is_named_never_guessed(inputs: QueueClockInputs, gap: str) -> None:
    clocks = queue_clocks(inputs)
    assert gap in clocks.gaps
    assert clocks.semantic_predecessor_overlap_ms is None
