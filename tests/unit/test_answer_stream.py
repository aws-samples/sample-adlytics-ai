# SPDX-License-Identifier: MIT-0
"""The V4 public conversation stream: event contracts and the incremental validator."""

from __future__ import annotations

from dataclasses import replace

import pytest

from adapters.inbound.conversation.contract_codec import (
    contract_to_json,
    decode_public_conversation_stream_event,
)
from domain.conversation.answer_stream import (
    MAX_RELEASED_TEXT_BYTES,
    ConversationActivityEventV4,
    ConversationDoneEventV4,
    ConversationRefusedEventV4,
    ConversationStreamEventV4,
    ConversationStreamValidator,
    ConversationTextEventV4,
    ConversationToolEventV4,
    PublicActivityStageV4,
    PublicToolCallStatusV4,
    StreamRefusalReasonCodeV4,
    validate_conversation_stream,
)

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-083")]

_DELTA_BOUND = 8 * 1024


def _text(sequence: int, text: str = "Fictional **answer** delta. ") -> ConversationTextEventV4:
    return ConversationTextEventV4(sequence=sequence, text=text)


def _activity(
    stage: PublicActivityStageV4 = PublicActivityStageV4.RETRIEVING_GOVERNED_DATA,
) -> ConversationActivityEventV4:
    return ConversationActivityEventV4(stage=stage)


def _tool(status: PublicToolCallStatusV4) -> ConversationToolEventV4:
    return ConversationToolEventV4(tool_name="catalog_list_metrics", status=status)


def _refused(
    reason_code: StreamRefusalReasonCodeV4 = StreamRefusalReasonCodeV4.DEPENDENCY_UNAVAILABLE,
) -> ConversationRefusedEventV4:
    return ConversationRefusedEventV4(reason_code=reason_code, reason="Fictional refusal.")


# --- Event contracts --------------------------------------------------------------------------


def test_every_event_names_the_v4_stream_and_its_kind() -> None:
    events: tuple[ConversationStreamEventV4, ...] = (
        _activity(),
        _tool(PublicToolCallStatusV4.REQUESTED),
        _text(1),
        ConversationDoneEventV4(),
        _refused(),
    )

    assert {event.schema_version for event in events} == {"ConversationStream.v4"}
    assert [event.kind for event in events] == ["activity", "tool", "text", "done", "refused"]


def test_activity_and_refusal_vocabularies_are_closed() -> None:
    assert {stage.value for stage in PublicActivityStageV4} == {
        "understanding_request",
        "retrieving_governed_data",
    }
    assert {status.value for status in PublicToolCallStatusV4} == {"requested", "completed"}
    assert {code.value for code in StreamRefusalReasonCodeV4} == {
        "dependency_unavailable",
        "no_answer",
    }


@pytest.mark.parametrize(
    "name",
    ["", "Catalog_list_metrics", "gateway___catalog_list_metrics!", "1catalog", "a" * 129],
)
def test_a_tool_event_names_only_a_declared_logical_tool(name: str) -> None:
    with pytest.raises(ValueError, match="declared logical tool name"):
        ConversationToolEventV4(tool_name=name, status=PublicToolCallStatusV4.REQUESTED)


def test_a_text_delta_is_non_empty_bounded_and_sequenced_from_one() -> None:
    assert _text(1, "x" * _DELTA_BOUND).text == "x" * _DELTA_BOUND
    # The bound is on UTF-8 bytes, not characters.
    with pytest.raises(ValueError, match="non-empty and bounded"):
        _text(1, "é" * (_DELTA_BOUND // 2 + 1))
    with pytest.raises(ValueError, match="non-empty and bounded"):
        _text(1, "")
    with pytest.raises(ValueError):
        _text(0)


def test_a_text_delta_keeps_markdown_and_whitespace_exactly() -> None:
    delta = "\n\n| Metric | Value |\n|---|---|\n| ROAS | 3.2 |\n"

    assert _text(1, delta).text == delta


@pytest.mark.parametrize("audit_id", [None, "audit-01"])
def test_terminals_carry_an_optional_bounded_support_reference(audit_id: str | None) -> None:
    assert ConversationDoneEventV4(audit_id=audit_id).audit_id == audit_id
    assert replace(_refused(), audit_id=audit_id).audit_id == audit_id


def test_a_support_reference_or_refusal_reason_out_of_bounds_is_refused() -> None:
    with pytest.raises(ValueError):
        ConversationDoneEventV4(audit_id="")
    with pytest.raises(ValueError):
        ConversationDoneEventV4(audit_id="a" * 129)
    with pytest.raises(ValueError):
        replace(_refused(), reason="")
    with pytest.raises(ValueError):
        replace(_refused(), reason="r" * 4_001)


@pytest.mark.parametrize(
    "event",
    [
        _activity(PublicActivityStageV4.UNDERSTANDING_REQUEST),
        _tool(PublicToolCallStatusV4.COMPLETED),
        _text(3, "Fictional ✓ delta.\n"),
        ConversationDoneEventV4(audit_id="audit-01"),
        replace(_refused(StreamRefusalReasonCodeV4.NO_ANSWER), audit_id="audit-01"),
    ],
    ids=["activity", "tool", "text", "done", "refused"],
)
def test_every_event_round_trips_through_the_strict_codec(
    event: ConversationStreamEventV4,
) -> None:
    assert decode_public_conversation_stream_event(contract_to_json(event)) == event


@pytest.mark.parametrize(
    "serialized",
    [
        '{"kind":"done","schema_version":"ConversationStream.v3"}',
        '{"kind":"block","schema_version":"ConversationStream.v4","sequence":1}',
        '{"kind":"text","schema_version":"ConversationStream.v4","sequence":1}',
        '{"kind":"text","schema_version":"ConversationStream.v4","sequence":1,"text":"x",'
        '"extra":true}',
    ],
    ids=["v3", "retired-kind", "missing-field", "unknown-field"],
)
def test_the_codec_refuses_anything_but_an_exact_v4_event(serialized: str) -> None:
    with pytest.raises(ValueError):
        decode_public_conversation_stream_event(serialized)


# --- Stream validation ------------------------------------------------------------------------


def test_progress_and_text_interleave_freely_and_done_releases_the_joined_text() -> None:
    events: tuple[ConversationStreamEventV4, ...] = (
        _activity(PublicActivityStageV4.UNDERSTANDING_REQUEST),
        _text(1, "Let me check. "),
        _tool(PublicToolCallStatusV4.REQUESTED),
        _activity(),
        _tool(PublicToolCallStatusV4.COMPLETED),
        _text(2, "ROAS was "),
        _text(3, "3.2."),
        ConversationDoneEventV4(audit_id="audit-01"),
    )

    assert validate_conversation_stream(events, closed=True) == "Let me check. ROAS was 3.2."


def test_a_refused_stream_releases_nothing_even_after_text() -> None:
    assert validate_conversation_stream((_text(1), _refused()), closed=True) is None
    assert validate_conversation_stream((_refused(),), closed=True) is None


def test_done_requires_released_text() -> None:
    with pytest.raises(ValueError, match="done requires released answer text"):
        validate_conversation_stream((_activity(), ConversationDoneEventV4()), closed=True)


@pytest.mark.parametrize(
    "sequences", [(2,), (1, 1), (1, 3), (2, 1)], ids=["late-start", "repeat", "gap", "reversed"]
)
def test_text_sequence_must_be_contiguous_from_one(sequences: tuple[int, ...]) -> None:
    with pytest.raises(ValueError, match="contiguous monotonic sequence"):
        validate_conversation_stream(tuple(_text(sequence) for sequence in sequences), closed=False)


def test_nothing_may_follow_a_terminal() -> None:
    for terminal in (ConversationDoneEventV4(), _refused()):
        with pytest.raises(ValueError, match="after its terminal"):
            validate_conversation_stream((_text(1), terminal, _activity()), closed=True)


def test_a_closed_stream_needs_a_terminal_and_an_open_one_must_not_have_one() -> None:
    with pytest.raises(ValueError, match="closed before a legal terminal"):
        validate_conversation_stream((_activity(), _text(1)), closed=True)
    with pytest.raises(ValueError, match="terminal stream must be closed"):
        validate_conversation_stream((_text(1), ConversationDoneEventV4()), closed=False)
    assert validate_conversation_stream((_activity(), _text(1)), closed=False) is None


def test_released_text_is_bounded_in_utf8_bytes() -> None:
    full = MAX_RELEASED_TEXT_BYTES // _DELTA_BOUND
    within = tuple(_text(index + 1, "x" * _DELTA_BOUND) for index in range(full))

    released = validate_conversation_stream((*within, ConversationDoneEventV4()), closed=True)

    assert released is not None
    assert len(released.encode("utf-8")) == MAX_RELEASED_TEXT_BYTES
    with pytest.raises(ValueError, match="64 KiB"):
        validate_conversation_stream((*within, _text(full + 1, "x")), closed=False)


def test_a_refused_event_leaves_the_incremental_validator_unchanged() -> None:
    validator = ConversationStreamValidator()
    validator.accept(_text(1, "first "))

    with pytest.raises(ValueError):
        validator.accept(_text(3, "skipped"))
    validator.accept(_text(2, "second"))
    validator.accept(ConversationDoneEventV4())

    assert (validator.terminal, validator.done) == (True, True)
    assert validator.released_text == "first second"
    assert validator.released_bytes == len(b"first second")


def test_the_incremental_validator_reports_its_terminal_state() -> None:
    validator = ConversationStreamValidator()
    assert (validator.terminal, validator.done, validator.released_text) == (False, False, None)

    validator.accept(_text(1))
    assert validator.released_text is None  # not released until `done`

    validator.accept(_refused())
    assert (validator.terminal, validator.done, validator.released_text) == (True, False, None)


def test_validation_is_linear_in_the_number_of_deltas() -> None:
    """Ten thousand one-character deltas validate in one pass, one constant step each."""
    count = 10_000
    validator = ConversationStreamValidator()

    for sequence in range(1, count + 1):
        validator.accept(_text(sequence, "x"))
    validator.accept(ConversationDoneEventV4())

    assert validator.released_text == "x" * count
