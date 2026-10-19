# SPDX-License-Identifier: MIT-0
"""Per-turn latency milestones: the timer, its Harness marks, and the closed timing line."""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from typing import cast

import pytest
from governed_evidence_fixture import shaped_response
from types_boto3_bedrock_agentcore.client import BedrockAgentCoreClient

from adapters.inbound.tool_handlers.gateway import to_json_safe
from adapters.outbound.agentcore import harness
from adapters.outbound.telemetry.reporting import ConversationTurnTimingLog
from adapters.outbound.telemetry.turn_timing import (
    TurnMilestone,
    TurnTimer,
    current_turn_timer,
    turn_timer,
)
from domain.conversation.managed import HarnessTurnV2

pytestmark = pytest.mark.unit

type Event = Mapping[str, object]


class _Clock:
    """A clock the stream advances by 100 ms per event, so each mark lands on a known value."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float = 0.1) -> None:
        self.now += seconds


def test_the_first_mark_wins_unless_the_milestone_is_marked_latest() -> None:
    clock = _Clock()
    timer = TurnTimer(clock=clock)
    clock.advance(0.25)
    timer.mark(TurnMilestone.ADMITTED)
    timer.mark(TurnMilestone.LAST_TOOL_RESULT, latest=True)
    clock.advance(0.5)
    timer.mark(TurnMilestone.ADMITTED)
    timer.mark(TurnMilestone.LAST_TOOL_RESULT, latest=True)

    assert timer.marks == {TurnMilestone.ADMITTED: 250, TurnMilestone.LAST_TOOL_RESULT: 750}


def test_metadata_keeps_only_the_named_numeric_usage() -> None:
    timer = TurnTimer()
    timer.metadata(
        {
            "usage": {
                "inputTokens": 1200,
                "outputTokens": 80,
                "cacheReadInputTokens": 900,
                "note": "fictional content that must not be recorded",
                "totalTokens": True,
            },
            "metrics": {"latencyMs": 640},
            "trace": {"inputTokens": 5},
        }
    )
    timer.metadata({"usage": {"inputTokens": 300, "outputTokens": -1}})

    assert timer.usage == {
        "input_tokens": 1500,
        "output_tokens": 80,
        "cache_read_input_tokens": 900,
        "provider_latency_ms": 640,
    }
    assert timer.metadata_events == 2


def test_the_timer_is_bound_to_its_turn_and_released_after() -> None:
    assert current_turn_timer() is None
    with turn_timer() as timer:
        assert current_turn_timer() is timer
    assert current_turn_timer() is None


def test_tool_rows_are_bounded_while_the_first_request_is_still_marked() -> None:
    timer = TurnTimer()
    rows = [timer.tool_input_started("catalog_list_metrics") for _ in range(20)]

    assert sum(row is not None for row in rows) == 16
    assert TurnMilestone.FIRST_TOOL_INPUT in timer.marks


class _TimedStream:
    def __init__(self, events: tuple[Event, ...], clock: _Clock) -> None:
        self.events = events
        self.clock = clock

    def __iter__(self) -> Iterator[Event]:
        for event in self.events:
            self.clock.advance()
            yield event

    def close(self) -> None:
        return None


class _TimedClient:
    def __init__(self, stream: _TimedStream, clock: _Clock) -> None:
        self.stream = stream
        self.clock = clock

    def invoke_harness(self, **_kwargs: object) -> dict[str, _TimedStream]:
        self.clock.advance(1.0)
        return {"stream": self.stream}


def _delta(text: str) -> Event:
    return {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": text}}}


def _governed_turn() -> tuple[Event, ...]:
    document = to_json_safe(shaped_response())
    return (
        {"messageStart": {"role": "assistant"}},  # 0.1 s after the stream opens
        _delta("Let me check."),  # 0.2
        {"contentBlockStop": {"contentBlockIndex": 0}},  # 0.3
        {
            "contentBlockStart": {
                "contentBlockIndex": 1,
                "start": {
                    "toolUse": {"toolUseId": "use-1", "name": "catalog___catalog_list_metrics"}
                },
            }
        },  # 0.4
        {"contentBlockDelta": {"contentBlockIndex": 1, "delta": {"toolUse": {"input": "{}"}}}},
        {"contentBlockStop": {"contentBlockIndex": 1}},  # 0.6
        {"messageStop": {"stopReason": "tool_use"}},  # 0.7
        {"messageStart": {"role": "user"}},  # 0.8
        {
            "contentBlockStart": {
                "contentBlockIndex": 0,
                "start": {"toolResult": {"toolUseId": "use-1", "status": "success"}},
            }
        },  # 0.9
        {
            "contentBlockDelta": {
                "contentBlockIndex": 0,
                "delta": {"toolResult": [{"text": json.dumps(document)}]},
            }
        },  # 1.0
        {"contentBlockStop": {"contentBlockIndex": 0}},  # 1.1
        {"messageStop": {"stopReason": "tool_result"}},  # 1.2
        {"messageStart": {"role": "assistant"}},  # 1.3
        _delta("The workspace reports one metric."),  # 1.4
        {"contentBlockStop": {"contentBlockIndex": 0}},  # 1.5
        {"messageStop": {"stopReason": "end_turn"}},  # 1.6
        {"metadata": {"usage": {"inputTokens": 4000, "outputTokens": 120}}},  # 1.7
    )


def test_the_harness_adapter_marks_each_milestone_its_stream_passes() -> None:
    clock = _Clock()
    with turn_timer(clock) as timer:
        adapter = harness.AgentCoreManagedHarness(
            harness.ManagedHarnessTarget("arn:example:harness", "DEFAULT", 10),
            session_id_for=lambda _conversation_id: "s" * 33,
            client=cast(
                "BedrockAgentCoreClient",
                _TimedClient(_TimedStream(_governed_turn(), clock), clock),
            ),
        )
        clock.advance(0.5)
        tuple(
            adapter.stream(
                HarnessTurnV2(
                    question="What can this workspace report?",
                    conversation_id="conversation-1",
                    actor_id="verified-subject",
                    correlation_id="123e4567-e89b-42d3-a456-426614174000",
                )
            )
        )

    # Invoked at 0.5 s; the call returns 1.0 s later; then 100 ms per stream event.
    assert {milestone.value: ms for milestone, ms in timer.marks.items()} == {
        "harness_invoked": 500,
        "harness_stream_opened": 1500,
        "first_harness_event": 1600,
        "first_model_text": 1700,
        "first_tool_input": 1900,
        "last_tool_result": 2700,
        "first_answer_text": 2900,
        "end_turn": 3100,
    }
    assert [row.projection for row in timer.tools] == [
        {
            "tool": "catalog_list_metrics",
            "input_started_ms": 1900,
            "requested_ms": 2200,
            "result_ms": 2700,
        }
    ]
    assert timer.assistant_messages_ms == [1600, 2800]
    assert timer.usage == {"input_tokens": 4000, "output_tokens": 120}
    # The paragraph break between messages is the adapter's, not a model delta.
    assert timer.text_deltas == 2


def test_a_harness_stream_outside_a_timed_turn_records_nothing() -> None:
    clock = _Clock()
    adapter = harness.AgentCoreManagedHarness(
        harness.ManagedHarnessTarget("arn:example:harness", "DEFAULT", 10),
        session_id_for=lambda _conversation_id: "s" * 33,
        client=cast(
            "BedrockAgentCoreClient", _TimedClient(_TimedStream(_governed_turn(), clock), clock)
        ),
    )
    events = tuple(
        adapter.stream(
            HarnessTurnV2(
                question="What can this workspace report?",
                conversation_id="conversation-1",
                actor_id="verified-subject",
                correlation_id="123e4567-e89b-42d3-a456-426614174000",
            )
        )
    )
    assert events
    assert current_turn_timer() is None


def test_the_timing_line_flattens_milestones_and_usage_under_fixed_names() -> None:
    projection = ConversationTurnTimingLog(
        correlation_id="123e4567-e89b-42d3-a456-426614174000",
        audit_id="audit-1",
        cold_start=False,
        milestones_ms={"admitted": 40, "first_model_text": 5100},
        tools=({"tool": "catalog_list_metrics", "input_started_ms": 5200},),
        assistant_messages_ms=(4900,),
        text_deltas=3,
        metadata_events=1,
        usage={"input_tokens": 4000},
    ).projection

    assert projection == {
        "operation": "conversation.turn",
        "component": "conversation_api",
        "phase": "timing",
        "outcome": "completed",
        "correlation_id": "123e4567-e89b-42d3-a456-426614174000",
        "audit_id": "audit-1",
        "cold_start": False,
        "text_deltas": 3,
        "metadata_events": 1,
        "tool_calls": 1,
        "assistant_messages": 1,
        "ms_admitted": 40,
        "ms_first_model_text": 5100,
        "usage_input_tokens": 4000,
        "tools": [{"tool": "catalog_list_metrics", "input_started_ms": 5200}],
        "assistant_messages_ms": [4900],
    }
