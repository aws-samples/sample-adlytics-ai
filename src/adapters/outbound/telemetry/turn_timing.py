# SPDX-License-Identifier: MIT-0
"""Per-turn latency milestones for the Conversation API.

One timer per turn, bound to the request's context so the route, the runtime, and the Harness
adapter mark the same turn without threading a clock through their signatures. Each marks the
closed milestones it passes; the route emits one `conversation.turn` `timing` line when the turn
ends (observability §3.10).

Only elapsed milliseconds since the route received the request, closed milestone names, logical
tool names, counts, and the provider's numeric token usage are recorded. Never content, tool
inputs, or any identifier other than the turn's own correlation and audit IDs.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import StrEnum
from typing import cast

__all__ = [
    "ToolTiming",
    "TurnMilestone",
    "TurnTimer",
    "current_turn_timer",
    "turn_timer",
]

# Keys of the Harness stream's `metadata` member recorded as usage; every other key is ignored.
_USAGE_KEYS: Mapping[tuple[str, str], str] = {
    ("usage", "inputTokens"): "input_tokens",
    ("usage", "outputTokens"): "output_tokens",
    ("usage", "totalTokens"): "total_tokens",
    ("usage", "cacheReadInputTokens"): "cache_read_input_tokens",
    ("usage", "cacheWriteInputTokens"): "cache_write_input_tokens",
    ("metrics", "latencyMs"): "provider_latency_ms",
}
_MAX_TOOLS = 16
_MAX_ASSISTANT_MESSAGES = 16


class TurnMilestone(StrEnum):
    """The closed points a turn passes, in the order a complete tool-using turn passes them."""

    ADMITTED = "admitted"
    INPUT_ASSESSED = "input_assessed"
    HARNESS_TOKEN_EXCHANGED = "harness_token_exchanged"  # noqa: S105 - a milestone name
    RESPONSE_STARTED = "response_started"
    HARNESS_INVOKED = "harness_invoked"
    HARNESS_STREAM_OPENED = "harness_stream_opened"
    FIRST_HARNESS_EVENT = "first_harness_event"
    FIRST_MODEL_TEXT = "first_model_text"
    FIRST_TOOL_INPUT = "first_tool_input"
    LAST_TOOL_RESULT = "last_tool_result"
    FIRST_ANSWER_TEXT = "first_answer_text"
    END_TURN = "end_turn"
    RECEIPT_WRITTEN = "receipt_written"
    STREAM_CLOSED = "stream_closed"


@dataclass(slots=True)
class ToolTiming:
    """One governed tool call as the Harness stream showed it, in ms since the request."""

    tool: str
    input_started_ms: int
    requested_ms: int | None = None
    result_ms: int | None = None

    @property
    def projection(self) -> dict[str, str | int]:
        """Return the row's fixed fields; a milestone the stream never showed is omitted."""
        projected: dict[str, str | int] = {
            "tool": self.tool,
            "input_started_ms": self.input_started_ms,
        }
        if self.requested_ms is not None:
            projected["requested_ms"] = self.requested_ms
        if self.result_ms is not None:
            projected["result_ms"] = self.result_ms
        return projected


@dataclass(slots=True)
class TurnTimer:
    """Milestones of one turn; the first mark of a milestone wins unless it is marked `latest`."""

    clock: Callable[[], float] = time.monotonic
    started: float = field(init=False)
    marks: dict[TurnMilestone, int] = field(default_factory=lambda: {})
    tools: list[ToolTiming] = field(default_factory=lambda: [])
    assistant_messages_ms: list[int] = field(default_factory=lambda: [])
    usage: dict[str, int] = field(default_factory=lambda: {})
    metadata_events: int = 0
    text_deltas: int = 0
    audit_id: str = ""

    def __post_init__(self) -> None:
        self.started = self.clock()

    def elapsed_ms(self) -> int:
        """Return whole milliseconds since the route received the request."""
        return round((self.clock() - self.started) * 1000)

    def mark(self, milestone: TurnMilestone, *, latest: bool = False) -> None:
        """Record a milestone once, or overwrite it when `latest` (the last tool result)."""
        if latest or milestone not in self.marks:
            self.marks[milestone] = self.elapsed_ms()

    def tool_input_started(self, tool: str) -> ToolTiming | None:
        """Open one tool call's row; calls past the bound are counted by the Harness, not here."""
        self.mark(TurnMilestone.FIRST_TOOL_INPUT)
        if len(self.tools) >= _MAX_TOOLS:
            return None
        timing = ToolTiming(tool=tool, input_started_ms=self.elapsed_ms())
        self.tools.append(timing)
        return timing

    def assistant_message_started(self) -> None:
        """Record when a model round starts writing; each round is one model call."""
        if len(self.assistant_messages_ms) < _MAX_ASSISTANT_MESSAGES:
            self.assistant_messages_ms.append(self.elapsed_ms())

    def text_delta(self, *, after_tool_result: bool) -> None:
        """Count one text delta; the first after any tool result marks the answer's start."""
        self.text_deltas += 1
        self.mark(TurnMilestone.FIRST_MODEL_TEXT)
        if after_tool_result:
            self.mark(TurnMilestone.FIRST_ANSWER_TEXT)

    def metadata(self, value: Mapping[str, object]) -> None:
        """Add the numeric usage the Harness reports; anything else in `metadata` is ignored."""
        self.metadata_events += 1
        for (section, key), name in _USAGE_KEYS.items():
            container = value.get(section)
            if not isinstance(container, Mapping):
                continue
            number = cast("Mapping[str, object]", container).get(key)
            if isinstance(number, int) and not isinstance(number, bool) and number >= 0:
                self.usage[name] = self.usage.get(name, 0) + number


_CURRENT: ContextVar[TurnTimer | None] = ContextVar("adlytics_turn_timer", default=None)


def current_turn_timer() -> TurnTimer | None:
    """Return the timer of the turn running in this context, or None outside a timed turn."""
    return _CURRENT.get()


@contextmanager
def turn_timer(clock: Callable[[], float] = time.monotonic) -> Generator[TurnTimer]:
    """Bind a fresh timer to the current context for the duration of one turn."""
    timer = TurnTimer(clock=clock)
    token = _CURRENT.set(timer)
    try:
        yield timer
    finally:
        _CURRENT.reset(token)
