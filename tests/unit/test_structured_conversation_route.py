# SPDX-License-Identifier: MIT-0
"""Canonical V4 relay, framing, and terminal safety coverage."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Generator, Iterable, Iterator

import pytest

from adapters.inbound.conversation import sse
from domain.conversation.answer_stream import (
    ConversationActivityEventV4,
    ConversationDoneEventV4,
    ConversationRefusedEventV4,
    ConversationStreamEventV4,
    ConversationTextEventV4,
    ConversationToolEventV4,
    PublicActivityStageV4,
    PublicToolCallStatusV4,
    StreamRefusalReasonCodeV4,
)

pytestmark = pytest.mark.unit

_DEPENDENCY_REFUSAL_SUFFIX = (
    b'"reason_code":"dependency_unavailable","schema_version":"ConversationStream.v4"}\n\n'
)


def _understanding() -> ConversationActivityEventV4:
    return ConversationActivityEventV4(stage=PublicActivityStageV4.UNDERSTANDING_REQUEST)


def _text(sequence: int, text: str = "Fictional answer. ") -> ConversationTextEventV4:
    return ConversationTextEventV4(sequence=sequence, text=text)


def _relay(
    events: Iterable[ConversationStreamEventV4], emit: Callable[[bytes], None]
) -> sse.RelayResult:
    """Relay `events` the way the route does: the first event, then the live generator."""
    source = _source(events)
    return sse.relay(next(source), source, emit)


def _source(events: Iterable[ConversationStreamEventV4]) -> Generator[ConversationStreamEventV4]:
    yield from events


def _frames(emitted: list[bytes]) -> list[dict[str, object]]:
    decoded: list[dict[str, object]] = []
    for frame in emitted:
        assert frame.startswith(b"data:")
        assert frame.endswith(b"\n\n")
        decoded.append(json.loads(frame[len(b"data:") : -2]))
    return decoded


@pytest.mark.covers("ADR-083")
def test_v4_relay_flushes_progress_text_and_done_frames_in_order() -> None:
    emitted: list[bytes] = []
    events: tuple[ConversationStreamEventV4, ...] = (
        _understanding(),
        ConversationToolEventV4(
            tool_name="catalog_list_metrics", status=PublicToolCallStatusV4.REQUESTED
        ),
        _text(1),
        _text(2, "More **Markdown**."),
        ConversationDoneEventV4(audit_id="audit-01"),
    )

    result = _relay(events, emitted.append)

    assert emitted == [sse.encode(event) for event in events]
    assert [frame["kind"] for frame in _frames(emitted)] == [
        "activity",
        "tool",
        "text",
        "text",
        "done",
    ]
    assert (result.completed, result.refused, result.refusal_reason_code) == (True, False, None)
    assert result.ending is sse.RelayEnding.TERMINAL


def test_each_frame_is_canonical_compact_sorted_utf8_json() -> None:
    frame = sse.encode(_text(1, "Fictional ✓ answer."))

    assert (
        frame
        == (
            'data:{"kind":"text","schema_version":"ConversationStream.v4",'
            '"sequence":1,"text":"Fictional ✓ answer."}\n\n'
        ).encode()
    )


@pytest.mark.covers("ADR-083")
def test_each_event_is_flushed_before_the_producer_yields_the_next() -> None:
    emitted: list[bytes] = []
    seen_before_next: list[int] = []

    def events() -> Iterator[ConversationStreamEventV4]:
        yield _understanding()
        seen_before_next.append(len(emitted))
        yield _text(1)
        seen_before_next.append(len(emitted))
        yield ConversationDoneEventV4()

    _relay(events(), emitted.append)

    assert seen_before_next == [1, 2]


def test_v4_relay_preserves_an_explicit_refusal_terminal() -> None:
    emitted: list[bytes] = []
    refusal = ConversationRefusedEventV4(
        reason_code=StreamRefusalReasonCodeV4.NO_ANSWER,
        reason="Fictional no-answer refusal.",
        audit_id="audit-01",
    )

    result = _relay((_understanding(), refusal), emitted.append)

    assert (result.completed, result.refused) == (False, True)
    assert result.refusal_reason_code is StreamRefusalReasonCodeV4.NO_ANSWER
    assert emitted[-1] == sse.encode(refusal)


@pytest.mark.covers("ADR-083")
def test_a_producer_failure_after_text_ends_with_one_safe_refusal_after_the_text() -> None:
    emitted: list[bytes] = []

    def events() -> Iterator[ConversationStreamEventV4]:
        yield _understanding()
        yield _text(1)
        raise RuntimeError("fictional internal failure detail")

    result = _relay(events(), emitted.append)

    assert (result.completed, result.refused) == (False, True)
    assert result.refusal_reason_code is StreamRefusalReasonCodeV4.DEPENDENCY_UNAVAILABLE
    assert emitted[:2] == [sse.encode(_understanding()), sse.encode(_text(1))]
    assert len(emitted) == 3
    assert emitted[-1].endswith(_DEPENDENCY_REFUSAL_SUFFIX)
    assert b"fictional internal failure detail" not in b"".join(emitted)
    assert _frames(emitted)[-1]["audit_id"] is None
    assert (result.ending, result.receipt_terminal) == (sse.RelayEnding.RELAY_REFUSED, False)


@pytest.mark.covers("ADR-083")
@pytest.mark.parametrize(
    "illegal",
    [
        _text(3),
        ConversationDoneEventV4(),
    ],
    ids=["sequence-gap", "done-without-text"],
)
def test_an_illegal_event_is_never_emitted_and_ends_with_a_safe_refusal(
    illegal: ConversationStreamEventV4,
) -> None:
    emitted: list[bytes] = []
    events: tuple[ConversationStreamEventV4, ...] = (
        (_understanding(), _text(1), illegal)
        if isinstance(illegal, ConversationTextEventV4)
        else (_understanding(), illegal)
    )

    result = _relay(events, emitted.append)

    assert sse.encode(illegal) not in emitted
    assert (result.completed, result.refused) == (False, True)
    assert result.refusal_reason_code is StreamRefusalReasonCodeV4.DEPENDENCY_UNAVAILABLE
    assert emitted[-1].endswith(_DEPENDENCY_REFUSAL_SUFFIX)
    # A source that does not handle the thrown refusal offers no receipt-backed event.
    assert (result.ending, result.receipt_terminal) == (sse.RelayEnding.RELAY_REFUSED, False)


@pytest.mark.covers("ADR-083")
def test_a_stream_that_ends_without_a_terminal_gets_one_safe_refusal() -> None:
    emitted: list[bytes] = []

    result = _relay((_understanding(), _text(1)), emitted.append)

    assert (result.completed, result.refused) == (False, True)
    assert result.refusal_reason_code is StreamRefusalReasonCodeV4.DEPENDENCY_UNAVAILABLE
    assert len(emitted) == 3
    assert emitted[-1].endswith(_DEPENDENCY_REFUSAL_SUFFIX)


def test_v4_relay_stops_at_the_terminal_and_never_pulls_later_events() -> None:
    emitted: list[bytes] = []
    pulled_after_done: list[bool] = []

    def events() -> Iterator[ConversationStreamEventV4]:
        yield _text(1)
        yield ConversationDoneEventV4()
        pulled_after_done.append(True)
        yield _text(2)

    result = _relay(events(), emitted.append)

    assert result.completed is True
    assert emitted == [sse.encode(_text(1)), sse.encode(ConversationDoneEventV4())]
    assert pulled_after_done == []


def test_encode_all_frames_each_event_lazily() -> None:
    pulled: list[int] = []

    def events() -> Iterator[ConversationStreamEventV4]:
        pulled.append(1)
        yield _text(1)
        pulled.append(2)
        yield ConversationDoneEventV4()

    frames = sse.encode_all(events())

    assert pulled == []
    assert next(frames) == sse.encode(_text(1))
    assert pulled == [1]
    assert list(frames) == [sse.encode(ConversationDoneEventV4())]


@pytest.mark.covers("ADR-083")
def test_relay_validation_is_linear_so_a_ten_thousand_delta_answer_relays_quickly() -> None:
    count = 10_000
    emitted: list[bytes] = []

    def events() -> Iterator[ConversationStreamEventV4]:
        for sequence in range(1, count + 1):
            yield _text(sequence, "x")
        yield ConversationDoneEventV4()

    started = time.perf_counter()
    result = _relay(events(), emitted.append)
    elapsed = time.perf_counter() - started

    assert result.completed is True
    assert len(emitted) == count + 1
    # A quadratic re-validation of the prefix would take tens of seconds here.
    assert elapsed < 5.0


def test_a_client_that_has_gone_ends_the_relay_with_no_further_write() -> None:
    emitted: list[bytes] = []

    def emit(frame: bytes) -> None:
        if emitted:
            raise BrokenPipeError("fictional closed socket")
        emitted.append(frame)

    result = _relay((_understanding(), _text(1), ConversationDoneEventV4()), emit)

    assert emitted == [sse.encode(_understanding())]
    assert result.ending is sse.RelayEnding.CLIENT_GONE
    assert (result.completed, result.refused, result.receipt_terminal) == (False, False, False)


def test_refuse_runtime_returns_none_for_a_finished_source() -> None:
    source = _source(())
    with pytest.raises(StopIteration):
        next(source)

    assert sse.refuse_runtime(source, sse.RelayRefusalCause.RELAY_FAILED, "ValueError") is None


def test_refuse_runtime_returns_the_refused_event_a_source_yields_for_the_throw() -> None:
    refusal = ConversationRefusedEventV4(
        reason_code=StreamRefusalReasonCodeV4.DEPENDENCY_UNAVAILABLE,
        reason="Fictional receipt-backed refusal.",
        audit_id="audit-01",
    )
    causes: list[tuple[str, str | None]] = []

    def cooperating() -> Generator[ConversationStreamEventV4]:
        try:
            yield _understanding()
        except sse.RelayRefused as refused:
            causes.append((refused.cause.value, refused.error_type))
            yield refusal

    source = cooperating()
    next(source)

    assert sse.refuse_runtime(source, sse.RelayRefusalCause.ILLEGAL_EVENT, "ValueError") is refusal
    assert causes == [("illegal_event", "ValueError")]
