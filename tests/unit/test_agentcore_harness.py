# SPDX-License-Identifier: MIT-0
"""Managed Harness streaming, stream grammar, and non-disclosing custody coverage (ADR-083)."""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from typing import cast

import pytest
from botocore.awsrequest import AWSPreparedRequest
from botocore.exceptions import ClientError, ReadTimeoutError
from fictional_tokens import fictional_jwt
from governed_evidence_fixture import FIXTURE_TOOL_RESULT_REF, shaped_response
from types_boto3_bedrock_agentcore.client import BedrockAgentCoreClient

from adapters.inbound.tool_handlers.gateway import to_json_safe
from adapters.outbound.agentcore import harness
from domain.conversation.answer_stream import MAX_RELEASED_TEXT_BYTES
from domain.conversation.managed import (
    HarnessTurnV2,
    ManagedHarnessActivityV1,
    ManagedHarnessEventV1,
    ManagedHarnessLifecycleStageV1,
    ManagedHarnessTextDeltaV1,
    ManagedHarnessToolActivityV1,
    ManagedHarnessToolCallStatusV1,
    ManagedHarnessToolStepV1,
    ManagedHarnessTurnEndedV1,
)
from domain.conversation.tool_custody import (
    AgentToolCustodyV1,
    HarnessStepDispositionV1,
    ToolCustodyDispositionV1,
    ValidatedToolResultV1,
    validate_harness_trajectory,
)
from domain.narrative.publication import RenderingKind, grounded_refusal
from domain.shared_kernel.errors import DependencyError

pytestmark = pytest.mark.unit

type Event = Mapping[str, object]
type ResultSpec = tuple[str, str, Mapping[str, object]]

_REFUSAL_CORRELATION_ID = "0f8fad5b-d9cb-469f-a165-70867728950e"
_REFUSAL_REASON = (
    "Incremental ROAS is a declared metric, but the facts behind it are not built yet, "
    "so no figure exists to return."
)
_SESSION_ID = "s" * 33
_QUESTION = "What can this workspace report?"
_CATALOG_LIST = "catalog___catalog_list_metrics"
_CATALOG_DESCRIBE = "catalog___catalog_describe_metric"
_PRIVATE_INPUT = '{"private_input_marker":"fictional"}'
_PRIVATE_ERROR = {"text": "Fictional schema error that remains private."}
_REQUESTED = ManagedHarnessLifecycleStageV1.GOVERNED_DATA_REQUESTED
_READY = ManagedHarnessLifecycleStageV1.GOVERNED_DATA_READY


class _Stream:
    """A service event stream that records how far the adapter has read it."""

    def __init__(
        self, events: tuple[Event, ...] = (), *, failure: BaseException | None = None
    ) -> None:
        self.events = events
        self.failure = failure
        self.consumed = 0
        self.closed = False

    def __iter__(self) -> Iterator[Event]:
        for event in self.events:
            self.consumed += 1
            yield event
        if self.failure is not None:
            raise self.failure

    def close(self) -> None:
        self.closed = True


class _Client:
    def __init__(self, stream: _Stream) -> None:
        self.request: dict[str, object] | None = None
        self.invocations = 0
        self._stream = stream

    def invoke_harness(self, **kwargs: object) -> dict[str, _Stream]:
        self.request = kwargs
        self.invocations += 1
        return {"stream": self._stream}


class _DeniedClient:
    def invoke_harness(self, **_kwargs: object) -> dict[str, _Stream]:
        raise ClientError(
            {
                "Error": {
                    "Code": "AccessDeniedException",
                    "Message": (
                        "User: arn:aws:sts::111122223333:assumed-role/example/function "
                        "is not authorized to perform: bedrock-agentcore:InvokeAgentRuntime "
                        "on resource: arn:aws:bedrock-agentcore:us-east-1:111122223333:"
                        "harness/example/harness-endpoint/DEFAULT because no policy allows it"
                    ),
                },
                "ResponseMetadata": {
                    "HTTPHeaders": {},
                    "HTTPStatusCode": 403,
                    "HostId": "host-123",
                    "RequestId": "request-123",
                    "RetryAttempts": 0,
                },
            },
            "InvokeHarness",
        )


def _turn() -> HarnessTurnV2:
    return HarnessTurnV2(
        question=_QUESTION,
        conversation_id="conversation-1",
        actor_id="verified-subject",
        correlation_id="123e4567-e89b-42d3-a456-426614174000",
    )


def _adapter(client: object, session_id: str = _SESSION_ID) -> harness.AgentCoreManagedHarness:
    return harness.AgentCoreManagedHarness(
        harness.ManagedHarnessTarget("arn:example:harness", "DEFAULT", 10),
        session_id_for=lambda _conversation_id: session_id,
        client=cast("BedrockAgentCoreClient", client),
    )


def _run(events: tuple[Event, ...]) -> tuple[ManagedHarnessEventV1, ...]:
    return tuple(_adapter(_Client(_Stream(events))).stream(_turn()))


def _refusal_detail(events: tuple[Event, ...]) -> str:
    """Run a stream the adapter must refuse; return the adapter's own contract sentence."""
    stream = _Stream(events)
    with pytest.raises(DependencyError) as caught:
        tuple(_adapter(_Client(stream)).stream(_turn()))
    assert caught.value.code == harness.HARNESS_UNREACHABLE
    assert caught.value.context["cause"] == "ValueError"
    assert stream.closed
    return cast("str", caught.value.context["detail"])


def _text_delta(text: str, index: int = 0) -> Event:
    return {"contentBlockDelta": {"contentBlockIndex": index, "delta": {"text": text}}}


def _answer(*texts: str) -> tuple[Event, ...]:
    """One end_turn assistant message whose text arrives as the given deltas."""
    return (
        {"messageStart": {"role": "assistant"}},
        *(_text_delta(text) for text in texts),
        {"contentBlockStop": {"contentBlockIndex": 0}},
        {"messageStop": {"stopReason": "end_turn"}},
    )


def _tool_use(*calls: tuple[str, str], preface: str | None = None) -> tuple[Event, ...]:
    """One assistant tool-use message, optionally opened by text written before the calls."""
    events: list[Event] = [{"messageStart": {"role": "assistant"}}]
    offset = 0
    if preface is not None:
        events += [_text_delta(preface), {"contentBlockStop": {"contentBlockIndex": 0}}]
        offset = 1
    for index, (use_id, name) in enumerate(calls, start=offset):
        events += [
            {
                "contentBlockStart": {
                    "contentBlockIndex": index,
                    "start": {"toolUse": {"toolUseId": use_id, "name": name}},
                }
            },
            {
                "contentBlockDelta": {
                    "contentBlockIndex": index,
                    "delta": {"toolUse": {"input": _PRIVATE_INPUT}},
                }
            },
            {"contentBlockStop": {"contentBlockIndex": index}},
        ]
    events.append({"messageStop": {"stopReason": "tool_use"}})
    return tuple(events)


def _results(*results: ResultSpec) -> tuple[Event, ...]:
    """One user message returning each (tool use ID, status, content fragment) result."""
    events: list[Event] = [{"messageStart": {"role": "user"}}]
    for index, (use_id, status, fragment) in enumerate(results):
        events += [
            {
                "contentBlockStart": {
                    "contentBlockIndex": index,
                    "start": {"toolResult": {"toolUseId": use_id, "status": status}},
                }
            },
            {
                "contentBlockDelta": {
                    "contentBlockIndex": index,
                    "delta": {"toolResult": [fragment]},
                }
            },
            {"contentBlockStop": {"contentBlockIndex": index}},
        ]
    events.append({"messageStop": {"stopReason": "tool_result"}})
    return tuple(events)


def _success_document() -> dict[str, object]:
    return cast("dict[str, object]", to_json_safe(shaped_response()))


def _as_text(document: Mapping[str, object]) -> Mapping[str, object]:
    return {"text": json.dumps(document, separators=(",", ":"), ensure_ascii=False)}


def _refusal_document(*, include_grounding: bool = True) -> dict[str, object]:
    grounding = grounded_refusal(
        _REFUSAL_REASON, blocks_answer=True, identity_salt=_REFUSAL_CORRELATION_ID
    )
    return cast(
        "dict[str, object]",
        to_json_safe(
            {
                "refused": True,
                "code": "ADLY-CATALOG-0002",
                "reason": _REFUSAL_REASON,
                "correlation_id": _REFUSAL_CORRELATION_ID,
                "metadata": {"grounding": grounding} if include_grounding else {},
            }
        ),
    )


def _governed_turn() -> tuple[Event, ...]:
    """Text before one catalog call, its governed success, then the streamed answer."""
    return (
        *_tool_use(("private-use-id", _CATALOG_LIST), preface="Let me check the catalog."),
        *_results(("private-use-id", "success", _as_text(_success_document()))),
        *_answer("The workspace ", "reports one metric."),
    )


def _steps(events: tuple[ManagedHarnessEventV1, ...]) -> list[ManagedHarnessToolStepV1]:
    return [event for event in events if isinstance(event, ManagedHarnessToolStepV1)]


def _texts(events: tuple[ManagedHarnessEventV1, ...]) -> list[str]:
    return [event.text for event in events if isinstance(event, ManagedHarnessTextDeltaV1)]


def _stages(
    events: tuple[ManagedHarnessEventV1, ...],
) -> list[ManagedHarnessLifecycleStageV1]:
    return [event.stage for event in events if isinstance(event, ManagedHarnessActivityV1)]


# --- Streamed answer text -------------------------------------------------------------------


@pytest.mark.covers("ADR-083", "ADR-065")
def test_text_before_a_tool_call_and_after_its_result_streams_in_harness_order() -> None:
    stream = _Stream(_governed_turn())
    client = _Client(stream)

    events = tuple(_adapter(client).stream(_turn()))

    step = _steps(events)[0]
    assert events == (
        ManagedHarnessTextDeltaV1(text="Let me check the catalog."),
        ManagedHarnessToolActivityV1(
            tool_name="catalog_list_metrics", status=ManagedHarnessToolCallStatusV1.REQUESTED
        ),
        ManagedHarnessActivityV1(stage=_REQUESTED),
        ManagedHarnessToolActivityV1(
            tool_name="catalog_list_metrics", status=ManagedHarnessToolCallStatusV1.COMPLETED
        ),
        step,
        ManagedHarnessActivityV1(stage=_READY),
        # The answer is a later assistant message, so it opens its own paragraph.
        ManagedHarnessTextDeltaV1(text="\n\n"),
        ManagedHarnessTextDeltaV1(text="The workspace "),
        ManagedHarnessTextDeltaV1(text="reports one metric."),
        ManagedHarnessTurnEndedV1(),
    )
    # One invocation per turn: there is no server repair round.
    assert client.invocations == 1
    assert stream.closed


@pytest.mark.covers("ADR-083")
def test_each_projection_is_yielded_before_the_service_stream_is_read_further() -> None:
    answer = _answer("First", " second")
    stream = _Stream(answer)
    client = _Client(stream)
    generator = _adapter(client).stream(_turn())

    assert client.request is None
    assert stream.consumed == 0
    assert next(generator) == ManagedHarnessTextDeltaV1(text="First")
    assert stream.consumed == 2
    assert next(generator) == ManagedHarnessTextDeltaV1(text=" second")
    assert stream.consumed == 3
    assert next(generator) == ManagedHarnessTurnEndedV1()
    assert stream.consumed == len(answer)
    with pytest.raises(StopIteration):
        next(generator)


@pytest.mark.covers("ADR-083")
def test_a_governed_result_is_yielded_when_its_result_message_closes() -> None:
    source = _governed_turn()
    result_closed_at = len(_tool_use(("u", _CATALOG_LIST), preface="p")) + len(
        _results(("u", "success", _as_text(_success_document())))
    )
    stream = _Stream(source)
    generator = _adapter(_Client(stream)).stream(_turn())

    for event in generator:
        if isinstance(event, ManagedHarnessToolStepV1):
            break
    else:  # pragma: no cover - the assertion below names the failure
        pytest.fail("no tool step was yielded")

    assert stream.consumed == result_closed_at
    generator.close()


@pytest.mark.covers("ADR-083")
def test_reasoning_content_never_leaves_the_adapter() -> None:
    events = _run(
        (
            {"messageStart": {"role": "assistant"}},
            {"contentBlockStart": {"contentBlockIndex": 0, "start": {}}},
            {
                "contentBlockDelta": {
                    "contentBlockIndex": 0,
                    "delta": {"reasoningContent": {"text": "Fictional private reasoning."}},
                }
            },
            {
                "contentBlockDelta": {
                    "contentBlockIndex": 0,
                    "delta": {"reasoningContent": {"signature": "fictional-signature"}},
                }
            },
            {"contentBlockStop": {"contentBlockIndex": 0}},
            _text_delta("The answer.", index=1),
            {"contentBlockStop": {"contentBlockIndex": 1}},
            {"messageStop": {"stopReason": "end_turn"}},
        )
    )

    assert events == (ManagedHarnessTextDeltaV1(text="The answer."), ManagedHarnessTurnEndedV1())
    assert "private reasoning" not in repr(events)
    assert "fictional-signature" not in repr(events)


@pytest.mark.parametrize(
    ("reasoning", "message"),
    [
        ({}, "assistant delta is malformed"),
        ({"toolUse": "fictional"}, "assistant delta is malformed"),
        ("fictional", "reasoning delta must be an object"),
    ],
)
def test_a_malformed_reasoning_delta_is_refused(reasoning: object, message: str) -> None:
    detail = _refusal_detail(
        (
            {"messageStart": {"role": "assistant"}},
            {"contentBlockStart": {"contentBlockIndex": 0, "start": {}}},
            {
                "contentBlockDelta": {
                    "contentBlockIndex": 0,
                    "delta": {"reasoningContent": reasoning},
                }
            },
        )
    )

    assert message in detail


def test_an_empty_text_delta_is_not_forwarded() -> None:
    assert _texts(_run(_answer("", "Done."))) == ["Done."]


@pytest.mark.covers("ADR-083")
def test_answer_text_at_its_byte_bound_is_forwarded() -> None:
    half = MAX_RELEASED_TEXT_BYTES // 2

    assert _texts(_run(_answer("x" * half, "y" * half))) == ["x" * half, "y" * half]


@pytest.mark.covers("ADR-083")
@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("x" * (MAX_RELEASED_TEXT_BYTES // 2), "y" * (MAX_RELEASED_TEXT_BYTES // 2 + 1)),
        # Counted in UTF-8 bytes, not characters: 32 KiB + 1 characters of two bytes each.
        ("fictional", "é" * (MAX_RELEASED_TEXT_BYTES // 2 + 1)),
    ],
)
def test_answer_text_over_its_byte_bound_fails_after_the_released_prefix(
    first: str, second: str
) -> None:
    stream = _Stream(_answer(first, second))
    received: list[ManagedHarnessEventV1] = []

    with pytest.raises(DependencyError) as caught:
        received.extend(_adapter(_Client(stream)).stream(_turn()))

    assert received == [ManagedHarnessTextDeltaV1(text=first)]
    assert "answer text exceeds its bounded size" in cast("str", caught.value.context["detail"])
    assert stream.closed


@pytest.mark.covers("ADR-083")
def test_closing_the_generator_early_closes_the_service_stream_at_once() -> None:
    stream = _Stream(_governed_turn())
    generator = _adapter(_Client(stream)).stream(_turn())

    assert next(generator) == ManagedHarnessTextDeltaV1(text="Let me check the catalog.")
    generator.close()

    assert stream.closed
    assert stream.consumed == 2


def test_metadata_after_end_turn_is_accepted() -> None:
    events = _run((*_answer("Done."), {"metadata": {"usage": {"totalTokens": 1}}}))

    assert events[-1] == ManagedHarnessTurnEndedV1()


def test_an_implicit_final_text_block_is_closed_by_message_stop() -> None:
    events = _run(
        (
            {"messageStart": {"role": "assistant"}},
            _text_delta("Done."),
            {"messageStop": {"stopReason": "end_turn"}},
        )
    )

    assert events == (ManagedHarnessTextDeltaV1(text="Done."), ManagedHarnessTurnEndedV1())


# --- Governed tool steps and custody --------------------------------------------------------


@pytest.mark.covers("ADR-083", "ADR-062")
def test_a_governed_success_becomes_one_step_and_its_custody() -> None:
    events = _run(_governed_turn())

    (emitted,) = _steps(events)
    assert emitted.step.ordinal == 1
    assert emitted.step.tool_name == "catalog_list_metrics"
    assert emitted.step.disposition is HarnessStepDispositionV1.SUCCEEDED
    assert emitted.step.step_ref == FIXTURE_TOOL_RESULT_REF
    result = emitted.result
    assert result is not None
    assert result.tool_use_ref == "private-use-id"
    assert result.ordinal == 1
    assert result.disposition is ToolCustodyDispositionV1.SUCCEEDED
    assert result.tool_result_ref == FIXTURE_TOOL_RESULT_REF
    assert result.grounding_set == shaped_response().metadata.grounding
    # Tool inputs, raw Gateway names, and private tool-use IDs never leave the adapter.
    assert "private-use-id" not in repr(events)
    assert _CATALOG_LIST not in repr(events)
    assert "private_input_marker" not in repr(events)


def test_a_governed_result_may_arrive_as_one_json_document() -> None:
    events = _run(
        (
            *_tool_use(("use-json", _CATALOG_LIST)),
            *_results(("use-json", "success", {"json": _success_document()})),
            *_answer("Done."),
        )
    )

    assert _steps(events)[0].step.step_ref == FIXTURE_TOOL_RESULT_REF


@pytest.mark.covers("ADR-083", "ADR-062")
def test_a_governed_refusal_becomes_a_refused_step_with_its_grounded_custody() -> None:
    events = _run(
        (
            *_tool_use(("refused-id", _CATALOG_DESCRIBE)),
            *_results(("refused-id", "success", _as_text(_refusal_document()))),
            *_answer("That metric is not built yet."),
        )
    )

    (emitted,) = _steps(events)
    assert emitted.step.disposition is HarnessStepDispositionV1.REFUSED
    assert emitted.step.step_ref == _REFUSAL_CORRELATION_ID
    result = emitted.result
    assert result is not None
    assert result.disposition is ToolCustodyDispositionV1.REFUSED
    assert result.refusal_reason == _REFUSAL_REASON
    assert result.grounding_set is not None
    assert result.grounding_set.renderings[0].kind is RenderingKind.REFUSAL
    assert (
        ManagedHarnessToolActivityV1(
            tool_name="catalog_describe_metric", status=ManagedHarnessToolCallStatusV1.COMPLETED
        )
        in events
    )
    assert _stages(events) == [_REQUESTED, _READY]


@pytest.mark.covers("ADR-083", "ADR-064")
def test_a_governed_error_becomes_an_errored_step_with_no_custody_or_readiness() -> None:
    events = _run(
        (
            *_tool_use(("failed-id", _CATALOG_DESCRIBE)),
            *_results(("failed-id", "error", _PRIVATE_ERROR)),
            *_answer("I could not read that metric."),
        )
    )

    (emitted,) = _steps(events)
    assert emitted.step.disposition is HarnessStepDispositionV1.ERRORED
    assert emitted.step.tool_name == "catalog_describe_metric"
    assert emitted.step.step_ref is None
    assert emitted.result is None
    assert not any(
        isinstance(event, ManagedHarnessToolActivityV1)
        and event.status is ManagedHarnessToolCallStatusV1.COMPLETED
        for event in events
    )
    assert _stages(events) == [_REQUESTED]
    assert "schema error" not in repr(events)
    assert events[-1] == ManagedHarnessTurnEndedV1()


@pytest.mark.covers("ADR-083", "ADR-062")
def test_step_ordinals_are_contiguous_across_calls_in_result_arrival_order() -> None:
    events = _run(
        (
            *_tool_use(("failed-id", _CATALOG_DESCRIBE)),
            *_results(("failed-id", "error", _PRIVATE_ERROR)),
            *_tool_use(("list-id", _CATALOG_LIST), ("describe-id", _CATALOG_DESCRIBE)),
            # Parallel results arrive in the opposite order to their calls.
            *_results(
                ("describe-id", "success", _as_text(_refusal_document())),
                ("list-id", "success", _as_text(_success_document())),
            ),
            *_answer("Here is what I found."),
        )
    )

    emitted = _steps(events)
    steps = tuple(item.step for item in emitted)
    validate_harness_trajectory(steps)
    assert [(step.ordinal, step.tool_name, step.disposition.value) for step in steps] == [
        (1, "catalog_describe_metric", "errored"),
        (2, "catalog_describe_metric", "refused"),
        (3, "catalog_list_metrics", "succeeded"),
    ]
    results: tuple[ValidatedToolResultV1, ...] = tuple(
        item.result for item in emitted if item.result is not None
    )
    assert [result.ordinal for result in results] == [1, 2]
    assert AgentToolCustodyV1.from_results(results).results == results
    assert _stages(events) == [_REQUESTED, _REQUESTED, _READY]


def test_a_refusal_without_its_grounded_projection_is_refused() -> None:
    detail = _refusal_detail(
        (
            *_tool_use(("refused-id", _CATALOG_DESCRIBE)),
            *_results(
                ("refused-id", "success", _as_text(_refusal_document(include_grounding=False)))
            ),
        )
    )

    assert "grounded refusal projection" in detail


def test_a_refusal_envelope_with_an_extra_field_is_refused() -> None:
    document = {**_refusal_document(), "unexpected": "fictional"}

    detail = _refusal_detail(
        (
            *_tool_use(("refused-id", _CATALOG_DESCRIBE)),
            *_results(("refused-id", "success", _as_text(document))),
        )
    )

    assert "refusal does not use the exact governed shape" in detail


def test_a_success_envelope_carrying_a_refusal_state_is_refused() -> None:
    document = {**_success_document(), "refused": False}

    detail = _refusal_detail(
        (*_tool_use(("use-1", _CATALOG_LIST)), *_results(("use-1", "success", _as_text(document))))
    )

    assert "success cannot carry a refusal state" in detail


def test_a_success_envelope_with_an_extra_field_is_refused() -> None:
    document = {**_success_document(), "unexpected": "fictional"}

    detail = _refusal_detail(
        (*_tool_use(("use-1", _CATALOG_LIST)), *_results(("use-1", "success", _as_text(document))))
    )

    assert "exact ShapedResponse envelope" in detail


_REPRESENTATION_CASES: list[tuple[list[Mapping[str, object]], str]] = [
    ([{"text": "fictional", "json": {}}], "text or JSON"),
    ([{"text": "{}"}, {"json": {}}], "exactly one content representation"),
    ([{"json": {}}, {"json": {}}], "one JSON document"),
    ([], "tool-result delta is malformed"),
]


@pytest.mark.parametrize(("fragments", "message"), _REPRESENTATION_CASES)
def test_a_tool_result_must_use_one_bounded_representation(
    fragments: list[Mapping[str, object]], message: str
) -> None:
    detail = _refusal_detail(
        (
            *_tool_use(("use-1", _CATALOG_LIST)),
            {"messageStart": {"role": "user"}},
            {
                "contentBlockStart": {
                    "contentBlockIndex": 0,
                    "start": {"toolResult": {"toolUseId": "use-1", "status": "success"}},
                }
            },
            {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"toolResult": fragments}}},
            {"contentBlockStop": {"contentBlockIndex": 0}},
            {"messageStop": {"stopReason": "tool_result"}},
        )
    )

    assert message in detail


@pytest.mark.parametrize("status", ["success", "error"])
def test_a_tool_result_is_bounded_before_it_is_decoded_or_discarded(status: str) -> None:
    detail = _refusal_detail(
        (
            *_tool_use(("use-1", _CATALOG_LIST)),
            *_results(("use-1", status, {"text": "x" * (256 * 1024 + 1)})),
        )
    )

    assert "direct tool result exceeds its bounded size" in detail


def test_a_tool_result_with_an_unsupported_status_is_refused() -> None:
    detail = _refusal_detail(
        (*_tool_use(("use-1", _CATALOG_LIST)), *_results(("use-1", "pending", _PRIVATE_ERROR)))
    )

    assert "unsupported status" in detail


@pytest.mark.parametrize("use_id", ["unknown-id", "use-1"])
def test_a_result_for_an_unknown_or_already_answered_call_is_refused(use_id: str) -> None:
    answered = _results(("use-1", "success", _as_text(_success_document())))
    repeated = _results((use_id, "success", _as_text(_success_document())))

    detail = _refusal_detail((*_tool_use(("use-1", _CATALOG_LIST)), *answered, *repeated))

    assert "result start is incomplete or repeated" in detail


def test_malformed_tool_result_metadata_is_refused() -> None:
    detail = _refusal_detail(
        (
            *_tool_use(("use-1", _CATALOG_LIST)),
            {"messageStart": {"role": "user"}},
            {
                "contentBlockStart": {
                    "contentBlockIndex": 0,
                    "start": {"toolResult": {"toolUseId": "use-1", "status": "success"}},
                }
            },
            {
                "contentBlockDelta": {
                    "contentBlockIndex": 0,
                    "delta": {"toolResultMetadata": {"metadata": 1}},
                }
            },
        )
    )

    assert "metadata delta is malformed" in detail


# --- Stream grammar -------------------------------------------------------------------------


def test_an_event_with_multiple_members_is_refused() -> None:
    detail = _refusal_detail(({"messageStart": {"role": "assistant"}, "metadata": {}},))

    assert "exactly one event member" in detail


def test_content_outside_a_message_is_refused_naming_the_member_and_the_turn_position() -> None:
    """The refusal's text is the only diagnosis a live occurrence leaves, so it names the member."""
    detail = _refusal_detail((_text_delta("Fictional."),))

    assert "outside a message (contentBlockDelta, after 0 tool uses)" in detail


@pytest.mark.parametrize(
    "event",
    [
        {"internalServerException": {"message": "fictional"}},
        {"validationException": {"message": "fictional"}},
        {"runtimeClientError": {"message": "fictional"}},
    ],
)
def test_a_service_error_event_is_refused(event: Event) -> None:
    assert "service error event" in _refusal_detail((event,))


def test_an_unsupported_event_member_is_refused() -> None:
    assert "unsupported event member" in _refusal_detail(({"fictionalEvent": {}},))


def test_content_after_end_turn_is_refused() -> None:
    detail = _refusal_detail((*_answer("Done."), {"messageStart": {"role": "assistant"}}))

    assert "after end_turn" in detail


def test_a_message_started_inside_another_message_is_refused() -> None:
    detail = _refusal_detail(
        ({"messageStart": {"role": "assistant"}}, {"messageStart": {"role": "assistant"}})
    )

    assert "before the prior message stopped" in detail


@pytest.mark.parametrize("role", ["system", 1])
def test_a_message_with_an_unsupported_role_is_refused(role: object) -> None:
    assert "unsupported role" in _refusal_detail(({"messageStart": {"role": role}},))


def test_an_empty_message_is_refused() -> None:
    detail = _refusal_detail(
        ({"messageStart": {"role": "assistant"}}, {"messageStop": {"stopReason": "end_turn"}})
    )

    assert "stopped an empty message" in detail


def test_metadata_inside_an_open_message_is_refused() -> None:
    detail = _refusal_detail(({"messageStart": {"role": "assistant"}}, {"metadata": {}}))

    assert "metadata occurred inside an open message" in detail


def test_a_stop_reason_that_does_not_match_the_message_phase_is_refused() -> None:
    events = list(_tool_use(("use-1", _CATALOG_LIST)))
    events[-1] = {"messageStop": {"stopReason": "end_turn"}}

    assert "phase and stop reason" in _refusal_detail(tuple(events))


def test_a_message_stopped_with_an_open_tool_block_is_refused() -> None:
    use = _tool_use(("use-1", _CATALOG_LIST))
    events = [event for event in use if "contentBlockStop" not in event]

    assert "open content block" in _refusal_detail(tuple(events))


def test_a_content_block_that_is_both_use_and_result_is_refused() -> None:
    detail = _refusal_detail(
        (
            {"messageStart": {"role": "assistant"}},
            {
                "contentBlockStart": {
                    "contentBlockIndex": 0,
                    "start": {
                        "toolUse": {"toolUseId": "use-1", "name": _CATALOG_LIST},
                        "toolResult": {"toolUseId": "use-1", "status": "success"},
                    },
                }
            },
        )
    )

    assert "use and result together" in detail


def test_a_repeated_tool_use_id_is_refused() -> None:
    detail = _refusal_detail(_tool_use(("use-1", _CATALOG_LIST), ("use-1", _CATALOG_LIST)))

    assert "repeated a tool use ID" in detail


def test_a_reused_open_content_block_index_is_refused() -> None:
    start: Event = {
        "contentBlockStart": {
            "contentBlockIndex": 0,
            "start": {"toolUse": {"toolUseId": "use-1", "name": _CATALOG_LIST}},
        }
    }

    detail = _refusal_detail(({"messageStart": {"role": "assistant"}}, start, start))

    assert "reused an open content block index" in detail


def test_a_result_before_its_tool_use_message_closes_is_refused() -> None:
    use = _tool_use(("use-1", _CATALOG_LIST))
    result = _results(("use-1", "success", _as_text(_success_document())))

    detail = _refusal_detail((*use[:-1], result[1]))

    assert "mixed incompatible content phases" in detail


@pytest.mark.parametrize(
    "event",
    [
        {"contentBlockStop": {"contentBlockIndex": 99}},
        {"contentBlockDelta": {"contentBlockIndex": 99, "delta": {"toolUse": {"input": "{}"}}}},
    ],
)
def test_unknown_content_block_transitions_are_refused(event: Event) -> None:
    detail = _refusal_detail(({"messageStart": {"role": "assistant"}}, event))

    assert "unknown content block" in detail


def test_assistant_text_under_the_user_role_is_refused() -> None:
    detail = _refusal_detail(
        (
            {"messageStart": {"role": "user"}},
            {"contentBlockStart": {"contentBlockIndex": 0, "start": {}}},
            _text_delta("Fictional user prose."),
        )
    )

    assert "unsupported role" in detail


def test_a_nonstring_tool_input_is_refused() -> None:
    events = list(_tool_use(("use-1", _CATALOG_LIST)))
    events[2] = {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"toolUse": {"input": {}}}}}

    assert "tool-use delta is malformed" in _refusal_detail(tuple(events))


@pytest.mark.parametrize(
    ("event", "message"),
    [
        (
            {
                "contentBlockStart": {
                    "contentBlockIndex": 0,
                    "start": {"toolUse": {"toolUseId": "use-1", "name": "catalog_list_metrics"}},
                }
            },
            "declared Gateway tool name",
        ),
        (
            {
                "contentBlockStart": {
                    "contentBlockIndex": True,
                    "start": {"toolUse": {"toolUseId": "use-1", "name": _CATALOG_LIST}},
                }
            },
            "content block index is invalid",
        ),
        (
            {
                "contentBlockStart": {
                    "contentBlockIndex": 0,
                    "start": {"toolUse": {"toolUseId": " ", "name": _CATALOG_LIST}},
                }
            },
            "tool use ID must be non-empty text",
        ),
    ],
)
def test_invalid_tool_wire_scalars_are_refused(event: Event, message: str) -> None:
    assert message in _refusal_detail(({"messageStart": {"role": "assistant"}}, event))


# --- Turn completion ------------------------------------------------------------------------


@pytest.mark.covers("ADR-083")
def test_a_stream_that_ends_without_end_turn_fails_after_the_steps_it_reached() -> None:
    stream = _Stream(
        (
            *_tool_use(("use-1", _CATALOG_LIST)),
            *_results(("use-1", "success", _as_text(_success_document()))),
        )
    )
    received: list[ManagedHarnessEventV1] = []

    with pytest.raises(DependencyError) as caught:
        received.extend(_adapter(_Client(stream)).stream(_turn()))

    assert "tool lifecycle is incomplete" in cast("str", caught.value.context["detail"])
    # A turn that fails later still records the steps it reached.
    assert len(_steps(tuple(received))) == 1
    assert ManagedHarnessTurnEndedV1() not in received
    assert stream.closed


@pytest.mark.covers("ADR-083")
def test_a_stream_that_ends_inside_a_message_is_incomplete() -> None:
    detail = _refusal_detail(({"messageStart": {"role": "assistant"}}, _text_delta("Partial")))

    assert "tool lifecycle is incomplete" in detail


@pytest.mark.covers("ADR-083")
def test_a_turn_that_ends_with_an_unanswered_tool_call_fails() -> None:
    detail = _refusal_detail((*_tool_use(("use-1", _CATALOG_LIST)), *_answer("Done.")))

    assert "did not return every direct tool result" in detail


# --- Transport, errors, and request framing -------------------------------------------------


def test_actionable_aws_denial_metadata_is_preserved() -> None:
    with pytest.raises(DependencyError) as caught:
        tuple(_adapter(_DeniedClient()).stream(_turn()))

    assert caught.value.code == harness.HARNESS_UNREACHABLE
    assert caught.value.context == {
        "cause": "ClientError",
        "provider": "aws",
        "provider_operation": "InvokeHarness",
        "service_code": "AccessDeniedException",
        "provider_message": (
            "User: arn:aws:sts::111122223333:assumed-role/example/function "
            "is not authorized to perform: bedrock-agentcore:InvokeAgentRuntime "
            "on resource: arn:aws:bedrock-agentcore:us-east-1:111122223333:"
            "harness/example/harness-endpoint/DEFAULT because no policy allows it"
        ),
        "principal": "arn:aws:sts::111122223333:assumed-role/example/function",
        "provider_action": "bedrock-agentcore:InvokeAgentRuntime",
        "resource": (
            "arn:aws:bedrock-agentcore:us-east-1:111122223333:"
            "harness/example/harness-endpoint/DEFAULT"
        ),
        "status": 403,
        "request_id": "request-123",
    }


@pytest.mark.covers("ADR-083")
def test_a_transport_failure_mid_stream_follows_the_text_already_released() -> None:
    failure = ReadTimeoutError(endpoint_url="https://bedrock-agentcore.example.invalid")
    stream = _Stream(
        ({"messageStart": {"role": "assistant"}}, _text_delta("Partial")), failure=failure
    )
    received: list[ManagedHarnessEventV1] = []

    with pytest.raises(DependencyError) as caught:
        received.extend(_adapter(_Client(stream)).stream(_turn()))

    assert received == [ManagedHarnessTextDeltaV1(text="Partial")]
    assert caught.value.code == harness.HARNESS_UNREACHABLE
    assert caught.value.context["cause"] == "ReadTimeoutError"
    assert caught.value.context["provider"] == "aws"
    assert caught.value.__cause__ is failure
    assert stream.closed


def test_a_malformed_provider_event_maps_to_a_dependency_failure_with_its_detail() -> None:
    stream = _Stream(({"messageStart": {"role": "assistant"}, "metadata": {}},))

    with pytest.raises(DependencyError) as caught:
        tuple(_adapter(_Client(stream)).stream(_turn()))

    assert set(caught.value.context) == {"cause", "detail"}
    assert caught.value.context["cause"] == "ValueError"
    detail = caught.value.context["detail"]
    assert isinstance(detail, str)
    # The detail is the adapter's own fixed contract sentence, never provider content.
    assert "exactly one event member" in detail
    assert isinstance(caught.value.__cause__, ValueError)
    assert stream.closed


def test_a_response_without_a_stream_maps_to_a_dependency_failure() -> None:
    class _StreamlessClient:
        def invoke_harness(self, **_kwargs: object) -> dict[str, object]:
            return {}

    with pytest.raises(DependencyError) as caught:
        tuple(_adapter(_StreamlessClient()).stream(_turn()))

    assert caught.value.context["cause"] == "KeyError"


@pytest.mark.parametrize("session_id", ["s" * 32, "s" * 101])
def test_an_out_of_range_session_id_is_refused_before_invocation(session_id: str) -> None:
    stream = _Stream(_answer("Done."))
    client = _Client(stream)

    with pytest.raises(ValueError, match="invalid Harness session ID"):
        tuple(_adapter(client, session_id).stream(_turn()))

    assert client.request is None
    assert stream.closed is False


@pytest.mark.parametrize("session_id", ["s" * 33, "s" * 100])
def test_a_session_id_at_either_bound_is_sent(session_id: str) -> None:
    client = _Client(_Stream(_answer("Done.")))

    tuple(_adapter(client, session_id).stream(_turn()))

    assert client.request is not None
    assert client.request["runtimeSessionId"] == session_id


@pytest.mark.parametrize("timeout", [True, 0, -1, 1.5])
def test_the_target_requires_a_positive_integer_timeout(timeout: object) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        harness.ManagedHarnessTarget("arn:example:harness", "DEFAULT", cast("int", timeout))


@pytest.mark.parametrize(("arn", "qualifier"), [(" ", "DEFAULT"), ("arn:example:harness", "")])
def test_the_target_requires_a_nonblank_arn_and_qualifier(arn: str, qualifier: str) -> None:
    with pytest.raises(ValueError, match="nonblank"):
        harness.ManagedHarnessTarget(arn, qualifier, 10)


def test_harness_propagates_only_traceparent(monkeypatch: pytest.MonkeyPatch) -> None:
    def inject(_self: object, carrier: dict[str, str]) -> None:
        carrier.update(
            {
                "traceparent": "00-00000000000000000000000000000001-0000000000000001-01",
                "tracestate": "vendor=opaque",
                "baggage": "customer=prohibited",
            }
        )

    monkeypatch.setattr(harness.TraceContextTextMapPropagator, "inject", inject)
    client = _Client(_Stream(_governed_turn()))

    tuple(_adapter(client).stream(_turn()))

    assert client.request is not None
    assert client.request["traceParent"] == (
        "00-00000000000000000000000000000001-0000000000000001-01"
    )
    assert {"traceState", "baggage"}.isdisjoint(client.request)
    # Exact key set: the caller's bearer is a transport header set by the client, never an
    # invocation field, so nothing token-bearing can ride in the payload.
    assert set(client.request) == {
        "harnessArn",
        "qualifier",
        "runtimeSessionId",
        "actorId",
        "messages",
        "traceParent",
    }


def test_harness_omits_trace_context_when_there_is_no_active_span(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def inject(_self: object, _carrier: dict[str, str]) -> None:
        """Inject nothing, as the propagator does when no span is active."""

    monkeypatch.setattr(harness.TraceContextTextMapPropagator, "inject", inject)
    client = _Client(_Stream(_answer("Done.")))

    tuple(_adapter(client).stream(_turn()))

    assert client.request is not None
    assert "traceParent" not in client.request


@pytest.mark.covers("ADR-077")
def test_invoke_harness_carries_exactly_the_route_built_request_and_no_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-077 §2: any InvokeHarness caller may override prompt, tools, skills, or model.

    The route is the only caller and sends none of them. Pinning the whole request, not a
    denylist, means any added key (`systemPrompt`, `tools`, `allowedTools`, `skills`, `model`,
    a limit, or a token) fails here.
    """

    def inject(_self: object, carrier: dict[str, str]) -> None:
        carrier["traceparent"] = "00-00000000000000000000000000000001-0000000000000001-01"

    monkeypatch.setattr(harness.TraceContextTextMapPropagator, "inject", inject)
    client = _Client(_Stream(_governed_turn()))

    tuple(_adapter(client).stream(_turn()))

    assert client.request == {
        "harnessArn": "arn:example:harness",
        "qualifier": "DEFAULT",
        "runtimeSessionId": _SESSION_ID,
        "actorId": "verified-subject",
        "messages": [{"role": "user", "content": [{"text": _QUESTION}]}],
        "traceParent": "00-00000000000000000000000000000001-0000000000000001-01",
    }


# --- Bearer client --------------------------------------------------------------------------

_BEARER_TARGET = harness.ManagedHarnessTarget(
    "arn:aws:bedrock-agentcore:us-east-1:111122223333:harness/example", "DEFAULT", 10
)
_FICTIONAL_BEARER = fictional_jwt()


class _RequestCaptured(Exception):  # noqa: N818 - a control-flow sentinel, not an error
    """Stops the SDK after the prepared request is inspected, so nothing reaches the network."""


@pytest.fixture
def hermetic_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve a region from the environment and nothing else: no profile, no shared files."""
    for name in ("AWS_PROFILE", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", "/nonexistent/aws-config")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/nonexistent/aws-credentials")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setattr(harness.boto3, "DEFAULT_SESSION", None)


def _sent_request(client: BedrockAgentCoreClient) -> tuple[dict[str, str], bytes]:
    """Return the exact headers and body the SDK would have sent for one InvokeHarness call.

    Registered with `register` (not `register_first`) so it runs after every handler the
    client under test attached, on the fully prepared request.
    """
    captured: dict[str, object] = {}

    def inspect(request: object, **_: object) -> None:
        prepared = cast("AWSPreparedRequest", request)
        captured["headers"] = {
            str(name): value.decode() if isinstance(value, bytes) else str(value)
            for name, value in prepared.headers.items()
        }
        body = prepared.body
        captured["body"] = body if isinstance(body, bytes) else str(body or "").encode()
        raise _RequestCaptured

    client.meta.events.register("before-send.bedrock-agentcore.InvokeHarness", inspect)
    with pytest.raises(_RequestCaptured):
        client.invoke_harness(
            harnessArn=_BEARER_TARGET.harness_arn,
            qualifier=_BEARER_TARGET.qualifier,
            runtimeSessionId="s" * 33,
            actorId="actor-fictional",
            messages=[{"role": "user", "content": [{"text": "Fictional question"}]}],
        )
    return cast("dict[str, str]", captured["headers"]), cast("bytes", captured["body"])


@pytest.mark.covers("ADR-077")
@pytest.mark.parametrize("token", ["", "  "])
def test_bearer_harness_client_refuses_a_blank_token(token: str) -> None:
    with pytest.raises(ValueError, match="empty bearer is parsed by the service as a SigV4"):
        harness.bearer_harness_client(_BEARER_TARGET, token)


@pytest.mark.covers("ADR-077")
@pytest.mark.usefixtures("hermetic_sdk")
def test_bearer_harness_client_sends_exactly_the_bearer_and_no_sigv4() -> None:
    headers, body = _sent_request(harness.bearer_harness_client(_BEARER_TARGET, _FICTIONAL_BEARER))
    lowered = {name.lower(): value for name, value in headers.items()}

    assert lowered["authorization"] == f"Bearer {_FICTIONAL_BEARER}"
    assert not lowered["authorization"].startswith("AWS4-HMAC-SHA256")
    assert "x-amz-date" not in lowered
    assert "x-amz-security-token" not in lowered
    assert "x-amz-content-sha256" not in lowered
    assert _FICTIONAL_BEARER.encode() not in body
    assert b"actor-fictional" in body


@pytest.mark.covers("ADR-077")
@pytest.mark.usefixtures("hermetic_sdk")
def test_the_request_capture_does_observe_sigv4_on_a_signing_client() -> None:
    """Positive control for the bearer test's SigV4 absence checks.

    The same capture sees signing when it happens, so its absence there is a property of the
    bearer client rather than of the capture.
    """
    signing = harness.boto3.client(
        "bedrock-agentcore",
        # pragma: allowlist nextline secret (A-AWS-DOC-EXAMPLE-001)
        aws_access_key_id="AKIAIOSFODNN7EXAMPLE",
        # pragma: allowlist nextline secret (A-TEST-LITERALS-001)
        aws_secret_access_key="fictional-secret-access-key",
        aws_session_token="fictional-session-token",
    )

    headers, _body = _sent_request(signing)
    lowered = {name.lower(): value for name, value in headers.items()}

    # pragma: allowlist nextline secret (A-AWS-DOC-EXAMPLE-001)
    assert lowered["authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/")
    assert "x-amz-date" in lowered
    assert lowered["x-amz-security-token"] == "fictional-session-token"


@pytest.mark.covers("ADR-077")
@pytest.mark.usefixtures("hermetic_sdk")
def test_each_bearer_client_presents_only_its_own_callers_token() -> None:
    first = harness.bearer_harness_client(_BEARER_TARGET, "fictional-token-caller-a")
    second = harness.bearer_harness_client(_BEARER_TARGET, "fictional-token-caller-b")

    first_headers, _ = _sent_request(first)
    second_headers, _ = _sent_request(second)

    assert first is not second
    assert first_headers["Authorization"] == "Bearer fictional-token-caller-a"
    assert second_headers["Authorization"] == "Bearer fictional-token-caller-b"


@pytest.mark.covers("ADR-083")
def test_text_within_one_message_is_never_split_by_a_break() -> None:
    assert _texts(_run(_answer("One ", "message."))) == ["One ", "message."]
