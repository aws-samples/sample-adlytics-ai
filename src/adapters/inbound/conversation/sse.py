# SPDX-License-Identifier: MIT-0
"""Canonical framing and ordered relay for validated V4 conversation-stream events."""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Generator, Iterable, Iterator
from dataclasses import dataclass
from enum import StrEnum

from adapters.inbound.conversation.contract_codec import contract_to_json
from domain.conversation.answer_stream import (
    ConversationRefusedEventV4,
    ConversationStreamEventV4,
    ConversationStreamValidator,
    StreamRefusalReasonCodeV4,
)

__all__ = [
    "HEARTBEAT",
    "MEDIA_TYPE",
    "RelayEnding",
    "RelayRefusalCause",
    "RelayRefused",
    "RelayResult",
    "encode",
    "encode_all",
    "refuse_runtime",
    "relay",
]

MEDIA_TYPE = "text/event-stream"
HEARTBEAT = b": keep-alive\n\n"
_DEPENDENCY_REFUSAL = "The assistant is temporarily unavailable. Please try again shortly."


class RelayRefusalCause(StrEnum):
    """Why the relay refused to carry the runtime's stream any further."""

    ILLEGAL_EVENT = "illegal_event"
    SOURCE_FAILED = "source_failed"
    SOURCE_ENDED_EARLY = "source_ended_early"
    RELAY_FAILED = "relay_failed"


class RelayRefused(Exception):  # noqa: N818 - a signal thrown into the runtime, not a failure name
    """Thrown into a live runtime generator so it records the refusal as its own terminal."""

    def __init__(self, cause: RelayRefusalCause, error_type: str | None = None) -> None:
        super().__init__(cause.value)
        self.cause = cause
        self.error_type = error_type


class RelayEnding(StrEnum):
    """How a relay ended."""

    TERMINAL = "terminal"
    """The runtime's own terminal event was relayed."""
    RELAY_REFUSED = "relay_refused"
    CLIENT_GONE = "client_gone"


@dataclass(frozen=True, slots=True)
class RelayResult:
    """Terminal state retained after an exact V4 event relay.

    `receipt_terminal` is true only for `relay_refused` when the refusal sent was the runtime's
    receipt-backed event, so the runtime has already reported the failure.
    """

    completed: bool
    refused: bool
    refusal_reason_code: StreamRefusalReasonCodeV4 | None
    ending: RelayEnding
    receipt_terminal: bool = False


def encode(event: ConversationStreamEventV4) -> bytes:
    """Encode one validated public V4 event as canonical UTF-8 SSE."""
    return f"data:{contract_to_json(event)}\n\n".encode()


def encode_all(events: Iterable[ConversationStreamEventV4]) -> Iterator[bytes]:
    """Frame structured events lazily without changing their V4 contract."""
    for event in events:
        yield encode(event)


def relay(
    first: ConversationStreamEventV4,
    remaining: Generator[ConversationStreamEventV4],
    emit: Callable[[bytes], None],
) -> RelayResult:
    """Validate and flush every event, completing a started stream with a safe terminal.

    An illegal event is refused through the runtime (`refuse_runtime`), so the turn's receipt
    records the refusal. A client that has gone (`emit` raising `OSError`) ends the relay with no
    further write, leaving the runtime to record the disconnect when the route closes it.
    """
    validator = ConversationStreamValidator()
    event = first
    while True:
        try:
            validator.accept(event)
        except ValueError as exc:
            offered = refuse_runtime(remaining, RelayRefusalCause.ILLEGAL_EVENT, type(exc).__name__)
            return _refuse(validator, emit, offered)
        try:
            emit(encode(event))
        except OSError:
            return RelayResult(
                completed=False,
                refused=False,
                refusal_reason_code=None,
                ending=RelayEnding.CLIENT_GONE,
            )
        if validator.terminal:
            return RelayResult(
                completed=validator.done,
                refused=isinstance(event, ConversationRefusedEventV4),
                refusal_reason_code=(
                    event.reason_code if isinstance(event, ConversationRefusedEventV4) else None
                ),
                ending=RelayEnding.TERMINAL,
            )
        try:
            event = next(remaining)
        # `source_ended_early` or `source_failed`: the generator has finished, so there is
        # nothing to throw into and the relay sends its own refusal.
        # error-propagation: that refusal is reported once by the route's _record_relay
        except Exception:
            return _refuse(validator, emit, None)


def refuse_runtime(
    remaining: Generator[ConversationStreamEventV4],
    cause: RelayRefusalCause,
    error_type: str | None = None,
) -> ConversationRefusedEventV4 | None:
    """Throw the refusal into the runtime; return its receipt-backed refused event, if any."""
    try:
        event = remaining.throw(RelayRefused(cause, error_type))
    # A finished or non-cooperating source offers no event.
    # error-propagation: on None every caller reports the triggering failure (server.py routes)
    except Exception:
        return None
    return event if isinstance(event, ConversationRefusedEventV4) else None


def _refuse(
    validator: ConversationStreamValidator,
    emit: Callable[[bytes], None],
    offered: ConversationRefusedEventV4 | None,
) -> RelayResult:
    """Close a started stream without exposing the triggering internal failure.

    The runtime's refused event is sent when the validator accepts it, because it carries the
    receipt's `audit_id`; otherwise the relay sends its own refusal. Text already sent stays on
    the user's screen; the refusal says the turn did not finish.
    """
    refusal: ConversationRefusedEventV4 | None = None
    if offered is not None:
        try:
            validator.accept(offered)
            refusal = offered
        except ValueError:
            refusal = None
    receipt_terminal = refusal is not None
    if refusal is None:
        refusal = ConversationRefusedEventV4(
            reason_code=StreamRefusalReasonCodeV4.DEPENDENCY_UNAVAILABLE,
            reason=_DEPENDENCY_REFUSAL,
        )
        validator.accept(refusal)
    with contextlib.suppress(OSError):
        emit(encode(refusal))
    return RelayResult(
        completed=False,
        refused=True,
        refusal_reason_code=refusal.reason_code,
        ending=RelayEnding.RELAY_REFUSED,
        receipt_terminal=receipt_terminal,
    )
