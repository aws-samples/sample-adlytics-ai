# SPDX-License-Identifier: MIT-0
"""The public V4 conversation stream: streamed answer text and one terminal (ADR-083).

The agent's answer reaches the browser as the model writes it: `text` events carry its Markdown
deltas in a contiguous sequence, `tool` and `activity` events report progress in between, and one
`done` or `refused` event ends the stream. Nothing here inspects the meaning of the text; the
bounds are sizes and ordering only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from domain.conversation.contract_common import require_integer, require_text
from domain.conversation.page_context import ContextStatusReasonV1, ConversationContextStatusV1

__all__ = [
    "MAX_RELEASED_TEXT_BYTES",
    "ConversationActivityEventV4",
    "ConversationContextEventV4",
    "ConversationDoneEventV4",
    "ConversationRefusedEventV4",
    "ConversationStreamEventV4",
    "ConversationStreamValidator",
    "ConversationTextEventV4",
    "ConversationToolEventV4",
    "PublicActivityStageV4",
    "PublicToolCallStatusV4",
    "StreamRefusalReasonCodeV4",
    "validate_conversation_stream",
]

MAX_RELEASED_TEXT_BYTES = 64 * 1024
"""The bound on one turn's released answer text, unchanged from the V3 block stream."""
_MAX_DELTA_BYTES = 8 * 1024
_PUBLIC_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]{0,127}$")


class PublicActivityStageV4(StrEnum):
    """Fixed progress labels; no model wording crosses this boundary."""

    UNDERSTANDING_REQUEST = "understanding_request"
    RETRIEVING_GOVERNED_DATA = "retrieving_governed_data"


@dataclass(frozen=True, slots=True, kw_only=True)
class ConversationActivityEventV4:
    """One progress label. `understanding_request` is emitted before the first Harness byte."""

    schema_version: Literal["ConversationStream.v4"] = "ConversationStream.v4"
    kind: Literal["activity"] = "activity"
    stage: PublicActivityStageV4


class PublicToolCallStatusV4(StrEnum):
    """Closed public view of one governed tool call's progress."""

    REQUESTED = "requested"
    COMPLETED = "completed"


@dataclass(frozen=True, slots=True, kw_only=True)
class ConversationToolEventV4:
    """One governed tool call, named by this repository's declared logical tool name only."""

    schema_version: Literal["ConversationStream.v4"] = "ConversationStream.v4"
    kind: Literal["tool"] = "tool"
    tool_name: str
    status: PublicToolCallStatusV4

    def __post_init__(self) -> None:
        if not _PUBLIC_TOOL_NAME.fullmatch(self.tool_name):
            raise ValueError("a public tool event requires a declared logical tool name.")


@dataclass(frozen=True, slots=True, kw_only=True)
class ConversationTextEventV4:
    """One delta of the agent's Markdown answer, in the order the model wrote it."""

    schema_version: Literal["ConversationStream.v4"] = "ConversationStream.v4"
    kind: Literal["text"] = "text"
    sequence: int
    text: str

    def __post_init__(self) -> None:
        require_integer(self.sequence, "public text sequence", minimum=1)
        if not self.text or len(self.text.encode("utf-8")) > _MAX_DELTA_BYTES:
            raise ValueError("a public text delta must be non-empty and bounded.")


@dataclass(frozen=True, slots=True, kw_only=True)
class ConversationDoneEventV4:
    """Successful terminal, after the turn's receipt was written.

    `audit_id` is the receipt key the user can quote as a support reference (ADR-081 §2); it is
    null only when the receipt write failed.
    """

    schema_version: Literal["ConversationStream.v4"] = "ConversationStream.v4"
    kind: Literal["done"] = "done"
    audit_id: str | None = None

    def __post_init__(self) -> None:
        if self.audit_id is not None:
            require_text(self.audit_id, "stream support reference", maximum=128)


class StreamRefusalReasonCodeV4(StrEnum):
    """Closed post-open terminal reasons."""

    DEPENDENCY_UNAVAILABLE = "dependency_unavailable"
    NO_ANSWER = "no_answer"


@dataclass(frozen=True, slots=True, kw_only=True)
class ConversationRefusedEventV4:
    """Terminal for a turn that could not finish; any text already sent stays on screen."""

    schema_version: Literal["ConversationStream.v4"] = "ConversationStream.v4"
    kind: Literal["refused"] = "refused"
    reason_code: StreamRefusalReasonCodeV4
    reason: str
    audit_id: str | None = None

    def __post_init__(self) -> None:
        require_text(self.reason, "stream refusal reason", maximum=4_000)
        if self.audit_id is not None:
            require_text(self.audit_id, "stream support reference", maximum=128)


@dataclass(frozen=True, slots=True, kw_only=True)
class ConversationContextEventV4:
    """Whether the turn used the page context it was sent (ADR-120 decision 5).

    Emitted once, before any answer text, and only when the request carried a reference, so the
    stream of a request without one never contains it. When context wasn't used, both server
    lines come with it: the browser refreshes the page, then shows the one that fits.
    """

    schema_version: Literal["ConversationStream.v4"] = "ConversationStream.v4"
    kind: Literal["context"] = "context"
    used: bool
    reason: ContextStatusReasonV1 | None = None
    display_text: str | None = None
    display_text_refresh_failed: str | None = None

    def __post_init__(self) -> None:
        ConversationContextStatusV1(
            used=self.used,
            reason=self.reason,
            display_text=self.display_text,
            display_text_refresh_failed=self.display_text_refresh_failed,
        )

    @classmethod
    def of(cls, status: ConversationContextStatusV1) -> ConversationContextEventV4:
        """Project one turn's context status onto the public stream."""
        return cls(
            used=status.used,
            reason=status.reason,
            display_text=status.display_text,
            display_text_refresh_failed=status.display_text_refresh_failed,
        )


ConversationStreamEventV4 = (
    ConversationActivityEventV4
    | ConversationContextEventV4
    | ConversationToolEventV4
    | ConversationTextEventV4
    | ConversationDoneEventV4
    | ConversationRefusedEventV4
)


class ConversationStreamValidator:
    """Check a V4 stream one event at a time, in constant time per event.

    Progress and text may interleave in any order; exactly one terminal ends the stream; the
    text sequence is contiguous from one and the released text stays within its bound. A `done`
    requires released text. A stateful checker, not a value: one instance follows one stream.
    """

    __slots__ = ("context_seen", "done", "parts", "released_bytes", "terminal")

    def __init__(self) -> None:
        self.parts: list[str] = []
        self.released_bytes = 0
        self.terminal = False
        self.done = False
        self.context_seen = False

    def accept(self, event: ConversationStreamEventV4) -> None:
        """Admit one more event or raise, leaving the state unchanged on refusal."""
        if self.terminal:
            raise ValueError("a stream cannot contain an event after its terminal event.")
        if isinstance(event, ConversationContextEventV4):
            if self.context_seen or self.parts:
                raise ValueError("a stream states its context status once, before any text.")
            self.context_seen = True
        elif isinstance(event, ConversationTextEventV4):
            if event.sequence != len(self.parts) + 1:
                raise ValueError("public text must use a contiguous monotonic sequence.")
            size = len(event.text.encode("utf-8"))
            if self.released_bytes + size > MAX_RELEASED_TEXT_BYTES:
                raise ValueError("released answer text exceeds its 64 KiB bound.")
            self.released_bytes += size
            self.parts.append(event.text)
        elif isinstance(event, ConversationDoneEventV4):
            if not self.parts:
                raise ValueError("done requires released answer text.")
            self.terminal = True
            self.done = True
        elif isinstance(event, ConversationRefusedEventV4):
            self.terminal = True

    @property
    def released_text(self) -> str | None:
        """The released answer when the stream ended with `done`, otherwise `None`."""
        return "".join(self.parts) if self.done else None


def validate_conversation_stream(
    events: tuple[ConversationStreamEventV4, ...], *, closed: bool
) -> str | None:
    """Validate a whole stream; return its released text when it ended with `done`."""
    validator = ConversationStreamValidator()
    for event in events:
        validator.accept(event)
    if closed and not validator.terminal:
        raise ValueError("transport closed before a legal terminal sequence.")
    if not closed and validator.terminal:
        raise ValueError("a terminal stream must be closed.")
    return validator.released_text
