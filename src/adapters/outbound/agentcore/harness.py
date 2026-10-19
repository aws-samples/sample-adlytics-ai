# SPDX-License-Identifier: MIT-0
"""Typed InvokeHarness transport that streams the answer and decodes governed tool results.

Assistant text is forwarded delta by delta as the Harness delivers it (ADR-083); governed tool
results are decoded into custody the moment their message closes. Reasoning content, tool inputs,
and raw tool payloads never leave this adapter.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Generator, Iterable, Mapping
from dataclasses import dataclass, field, fields
from typing import TYPE_CHECKING, cast

import boto3
from botocore import UNSIGNED
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from adapters.inbound.conversation.contract_codec import decode_contract
from adapters.outbound.aws_errors import aws_failure_context
from adapters.outbound.telemetry.turn_timing import (
    ToolTiming,
    TurnMilestone,
    TurnTimer,
    current_turn_timer,
)
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
from domain.conversation.page_context import page_context_block
from domain.conversation.tool_custody import (
    HarnessStepDispositionV1,
    HarnessToolStepV1,
    ToolCustodyDispositionV1,
    ValidatedToolResultV1,
)
from domain.narrative.publication import GroundingSet
from domain.narrative.shaped_response import Metadata, ResponseData
from domain.shared_kernel.errors import DependencyError, ErrorCode
from ports.outbound.harness import ManagedHarnessPort

if TYPE_CHECKING:  # pragma: no cover
    from botocore.awsrequest import AWSPreparedRequest
    from types_boto3_bedrock_agentcore.client import BedrockAgentCoreClient
    from types_boto3_bedrock_agentcore.type_defs import (
        HarnessContentBlockTypeDef,
        InvokeHarnessRequestTypeDef,
    )

__all__ = [
    "HARNESS_UNREACHABLE",
    "AgentCoreManagedHarness",
    "ManagedHarnessTarget",
    "bearer_harness_client",
]

HARNESS_UNREACHABLE = ErrorCode("HARNESS", 1)
_MIN_SESSION_ID_LENGTH = 33
_MAX_SESSION_ID_LENGTH = 100
_MAX_RESULT_BYTES = 256 * 1024
_REFUSAL_KEYS = frozenset({"refused", "code", "reason", "correlation_id", "metadata"})
_MESSAGE_BREAK = "\n\n"


@dataclass(frozen=True, slots=True)
class ManagedHarnessTarget:
    """Exact managed Harness target and bounded SDK transport wait."""

    harness_arn: str
    qualifier: str
    read_timeout_seconds: int

    def __post_init__(self) -> None:
        if not self.harness_arn.strip() or not self.qualifier.strip():
            raise ValueError("Harness ARN and qualifier must be nonblank.")
        if (
            isinstance(self.read_timeout_seconds, bool)
            or not isinstance(cast("object", self.read_timeout_seconds), int)
            or self.read_timeout_seconds < 1
        ):
            raise ValueError("AgentCore Harness read timeout must be a positive integer.")


@dataclass(slots=True)
class _ResultBuffer:
    status: str
    text: list[str]
    json_values: list[Mapping[str, object]]
    encoded_bytes: int = 0


@dataclass(slots=True)
class _Normalizer:
    """Decode the Harness stream as it arrives, in the order the Harness sends it.

    Every accepted event is checked against the Converse message grammar: one message at a time,
    blocks opened before their deltas, and a stop reason that matches the message's content. Text
    deltas and completed governed results are queued for the caller to drain after each event.
    """

    uses: dict[str, str]
    result_order: list[str]
    results: dict[str, _ResultBuffer]
    active: dict[int, tuple[str, str]]
    closed_uses: set[str]
    message_use_ids: set[str]
    decoded_use_ids: set[str]
    pending: list[ManagedHarnessEventV1]
    timer: TurnTimer | None = None
    tool_timings: dict[str, ToolTiming] = field(default_factory=lambda: {})
    governed_count: int = 0
    step_count: int = 0
    text_bytes: int = 0
    text_sent: bool = False
    message_has_text: bool = False
    message_phase: str | None = None
    message_role: str | None = None
    message_open: bool = False
    terminal: bool = False

    @classmethod
    def create(cls, timer: TurnTimer | None = None) -> _Normalizer:
        return cls(
            uses={},
            result_order=[],
            results={},
            active={},
            closed_uses=set(),
            message_use_ids=set(),
            decoded_use_ids=set(),
            pending=[],
            timer=timer,
        )

    def accept(self, event: Mapping[str, object]) -> None:  # noqa: PLR0912
        keys = tuple(key for key, value in event.items() if value is not None)
        if len(keys) != 1:
            raise ValueError("Harness stream event must contain exactly one event member.")
        kind = keys[0]
        value = _object(event[kind], f"Harness {kind}")
        if kind in {"internalServerException", "validationException", "runtimeClientError"}:
            raise ValueError("Harness stream returned a service error event.")
        if self.terminal and kind != "metadata":
            raise ValueError("Harness stream contains an event after end_turn.")
        if kind == "messageStart":
            if self.message_open or self.active:
                raise ValueError("Harness started a message before the prior message stopped.")
            role = value.get("role")
            if not isinstance(role, str) or role not in {"assistant", "user"}:
                raise ValueError("Harness message start has an unsupported role.")
            self.message_open = True
            self.message_phase = None
            self.message_role = role
            if self.timer is not None and role == "assistant":
                self.timer.assistant_message_started()
            self.message_use_ids.clear()
            self.message_has_text = False
            return
        if kind == "contentBlockStart":
            self._require_open_message(kind)
            self._start(value)
            return
        if kind == "contentBlockDelta":
            self._require_open_message(kind)
            self._delta(value)
            return
        if kind == "contentBlockStop":
            self._require_open_message(kind)
            index = _index(value)
            if self.active.pop(index, None) is None:
                raise ValueError("Harness stopped an unknown content block.")
            return
        if kind == "messageStop":
            self._stop(value)
            return
        if kind == "metadata":
            if self.message_open or self.active:
                raise ValueError("Harness metadata occurred inside an open message.")
            if self.timer is not None:
                self.timer.metadata(value)
            return
        raise ValueError("Harness stream contains an unsupported event member.")

    def drain(self) -> tuple[ManagedHarnessEventV1, ...]:
        """Return and clear the projections produced by the events accepted so far."""
        drained = tuple(self.pending)
        self.pending.clear()
        return drained

    def finish(self) -> None:
        """Require a closed lifecycle: the turn ended and every tool call was answered."""
        if not self.terminal or self.message_open or self.active:
            raise ValueError("Harness tool lifecycle is incomplete.")
        if set(self.results) != set(self.uses):
            raise ValueError("Harness did not return every direct tool result.")

    def _stop(self, event: Mapping[str, object]) -> None:
        self._require_open_message("messageStop")
        phase = self.message_phase
        if self.active:
            implicit_text = (
                phase == "text"
                and self.message_role == "assistant"
                and all(kind == "assistant" for kind, _ in self.active.values())
            )
            if not implicit_text:
                raise ValueError("Harness stopped a message with an open content block.")
            self.active.clear()
        if phase is None:
            raise ValueError("Harness stopped an empty message.")
        expected_reason = {"use": "tool_use", "result": "tool_result", "text": "end_turn"}[phase]
        if event.get("stopReason") != expected_reason:
            raise ValueError("Harness message phase and stop reason do not match.")
        self.message_open = False
        self.message_role = None
        if phase == "use":
            self.closed_uses.update(self.message_use_ids)
            self._timed_requests()
            self.pending.extend(
                ManagedHarnessToolActivityV1(
                    tool_name=_logical_tool_name(self.uses[use_id]),
                    status=ManagedHarnessToolCallStatusV1.REQUESTED,
                )
                for use_id in self.uses
                if use_id in self.message_use_ids
            )
            self.pending.append(
                ManagedHarnessActivityV1(
                    stage=ManagedHarnessLifecycleStageV1.GOVERNED_DATA_REQUESTED
                )
            )
        elif phase == "result":
            self._stop_result_message()
        else:
            self.terminal = True
            if self.timer is not None:
                self.timer.mark(TurnMilestone.END_TURN)

    def _timed_requests(self) -> None:
        if self.timer is None:
            return
        now = self.timer.elapsed_ms()
        for use_id in self.message_use_ids:
            timing = self.tool_timings.get(use_id)
            if timing is not None:
                timing.requested_ms = now

    def _timed_result(self, use_id: str) -> None:
        if self.timer is None:
            return
        self.timer.mark(TurnMilestone.LAST_TOOL_RESULT, latest=True)
        timing = self.tool_timings.get(use_id)
        if timing is not None:
            timing.result_ms = self.timer.elapsed_ms()

    def _stop_result_message(self) -> None:
        """Decode every result the closed message completed, in the order the Harness sent them."""
        decoded_any = False
        for use_id in self.result_order:
            if use_id in self.decoded_use_ids:
                continue
            if self.results[use_id].status == "error":
                self._record_governed_error(use_id)
            else:
                self._decode_governed(use_id)
                decoded_any = True
        if decoded_any:
            self.pending.append(
                ManagedHarnessActivityV1(stage=ManagedHarnessLifecycleStageV1.GOVERNED_DATA_READY)
            )

    def _require_open_message(self, kind: str) -> None:
        # The event member is one of the stream's closed member names, never content, so naming
        # it is what makes an intermittent framing refusal diagnosable from the log alone.
        if not self.message_open:
            raise ValueError(
                f"Harness content occurred outside a message ({kind}, after "
                f"{len(self.uses)} tool uses)."
            )

    def _message_is(self, phase: str) -> None:
        if self.message_phase is None:
            self.message_phase = phase
        elif self.message_phase != phase:
            raise ValueError("Harness message mixed incompatible content phases.")

    def _start(self, event: Mapping[str, object]) -> None:
        index = _index(event)
        if index in self.active:
            raise ValueError("Harness reused an open content block index.")
        start = _object(event.get("start"), "Harness content block start")
        tool_use = _optional_object(start.get("toolUse"), "Harness tool use")
        tool_result = _optional_object(start.get("toolResult"), "Harness tool result")
        if tool_use is not None and tool_result is not None:
            raise ValueError("Harness content block cannot be use and result together.")
        if tool_use is not None:
            if self.message_phase not in {None, "text", "use"}:
                raise ValueError("Harness message mixed incompatible content phases.")
            self.message_phase = "use"
            use_id = _text(tool_use.get("toolUseId"), "Harness tool use ID")
            name = _text(tool_use.get("name"), "Harness tool name")
            logical = _logical_tool_name(name)
            if use_id in self.uses:
                raise ValueError("Harness repeated a tool use ID.")
            self.uses[use_id] = name
            if self.timer is not None:
                timing = self.timer.tool_input_started(logical)
                if timing is not None:
                    self.tool_timings[use_id] = timing
            self.message_use_ids.add(use_id)
            self.active[index] = ("use", use_id)
            return
        if tool_result is not None:
            self._message_is("result")
            use_id = _text(tool_result.get("toolUseId"), "Harness tool result ID")
            status = tool_result.get("status")
            if (
                use_id not in self.closed_uses
                or not isinstance(status, str)
                or use_id in self.results
            ):
                raise ValueError("Harness tool result start is incomplete or repeated.")
            if status not in {"success", "error"}:
                raise ValueError("Harness tool execution returned an unsupported status.")
            self.results[use_id] = _ResultBuffer(status=status, text=[], json_values=[])
            self.result_order.append(use_id)
            self.active[index] = ("result", use_id)
            return
        self.active[index] = ("assistant", "")

    def _delta(self, event: Mapping[str, object]) -> None:
        index = _index(event)
        delta = _object(event.get("delta"), "Harness content block delta")
        members = tuple(key for key, value in delta.items() if value is not None)
        if len(members) != 1:
            raise ValueError("Harness content delta must contain exactly one member.")
        active = self.active.get(index)
        if (
            active is None
            and self.message_role == "assistant"
            and self.message_phase is None
            and members == ("text",)
        ):
            active = ("assistant", "")
            self.active[index] = active
        if active is None:
            raise ValueError("Harness emitted a delta for an unknown content block.")
        block_kind, use_id = active
        if block_kind == "assistant":
            self._assistant_delta(delta, members[0])
        elif block_kind == "use":
            self._tool_use_delta(delta, members)
        else:
            self._tool_result_delta(delta, members, use_id)

    def _assistant_delta(self, delta: Mapping[str, object], member: str) -> None:
        """Forward the text the model writes for the user; drop its reasoning content."""
        if self.message_role != "assistant":
            raise ValueError("Harness assistant content has an unsupported role.")
        if member == "text":
            text = delta.get("text")
            if not isinstance(text, str):
                raise ValueError("Harness prose delta is malformed.")
            if self.message_phase is None:
                self.message_phase = "text"
            elif self.message_phase not in {"text", "use"}:
                raise ValueError("Harness message mixed incompatible content phases.")
            if not text:
                return
            if self.text_sent and not self.message_has_text:
                # Text from a later assistant message (the answer after a tool call) starts its
                # own paragraph, or it would run on from the sentence written before the call.
                self._forward(_MESSAGE_BREAK)
            self.message_has_text = True
            if self.timer is not None:
                self.timer.text_delta(after_tool_result=bool(self.decoded_use_ids))
            self._forward(text)
            return
        if member == "reasoningContent":
            reasoning = _object(delta.get("reasoningContent"), "Harness reasoning delta")
            if reasoning and set(reasoning) <= {"text", "redactedContent", "signature"}:
                return
        raise ValueError("Harness assistant delta is malformed.")

    def _forward(self, text: str) -> None:
        self.text_bytes += len(text.encode("utf-8"))
        if self.text_bytes > MAX_RELEASED_TEXT_BYTES:
            raise ValueError("Harness answer text exceeds its bounded size.")
        self.pending.append(ManagedHarnessTextDeltaV1(text=text))
        self.text_sent = True

    @staticmethod
    def _tool_use_delta(delta: Mapping[str, object], members: tuple[str, ...]) -> None:
        """Validate a tool-input fragment and discard it: tool inputs stay private."""
        tool_use = _optional_object(delta.get("toolUse"), "Harness tool use delta")
        if (
            members != ("toolUse",)
            or tool_use is None
            or not isinstance(tool_use.get("input"), str)
        ):
            raise ValueError("Harness tool-use delta is malformed.")

    def _tool_result_delta(
        self, delta: Mapping[str, object], members: tuple[str, ...], use_id: str
    ) -> None:
        if members == ("toolResultMetadata",):
            metadata = _object(
                delta.get("toolResultMetadata"), "Harness tool-result metadata delta"
            )
            if not isinstance(metadata.get("metadata"), str):
                raise ValueError("Harness tool-result metadata delta is malformed.")
            return
        fragments = delta.get("toolResult")
        if members != ("toolResult",) or not isinstance(fragments, list) or not fragments:
            raise ValueError("Harness tool-result delta is malformed.")
        buffer = self.results[use_id]
        for fragment_value in cast("list[object]", fragments):
            fragment = _object(fragment_value, "Harness tool-result fragment")
            text = fragment.get("text")
            json_value = fragment.get("json")
            if isinstance(text, str) == isinstance(json_value, Mapping):
                raise ValueError("Harness tool-result fragment must contain text or JSON.")
            if isinstance(text, str):
                buffer.text.append(text)
                encoded = text.encode("utf-8")
            else:
                value = cast("Mapping[str, object]", json_value)
                buffer.json_values.append(value)
                encoded = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode(
                    "utf-8"
                )
            buffer.encoded_bytes += len(encoded)
            if buffer.encoded_bytes > _MAX_RESULT_BYTES:
                raise ValueError("Harness direct tool result exceeds its bounded size.")

    def _next_step(
        self, tool_name: str, disposition: HarnessStepDispositionV1, ref: str | None
    ) -> HarnessToolStepV1:
        self.step_count += 1
        return HarnessToolStepV1(
            ordinal=self.step_count, tool_name=tool_name, disposition=disposition, step_ref=ref
        )

    def _record_governed_error(self, use_id: str) -> None:
        """Retain a recoverable failed attempt as trajectory only, never as evidence custody."""
        # Validate that Gateway still returned one bounded representation, then discard it.
        self._serialized_result(use_id)
        self.decoded_use_ids.add(use_id)
        self._timed_result(use_id)
        self.pending.append(
            ManagedHarnessToolStepV1(
                step=self._next_step(
                    _logical_tool_name(self.uses[use_id]), HarnessStepDispositionV1.ERRORED, None
                )
            )
        )

    def _decode_governed(self, use_id: str) -> None:
        """Decode one governed tool result into custody the moment its message closes."""
        tool_name = _logical_tool_name(self.uses[use_id])
        serialized = self._serialized_result(use_id)
        document = _object(json.loads(serialized), "Harness direct tool result")
        self.governed_count += 1
        if document.get("refused") is True:
            if frozenset(document) != _REFUSAL_KEYS:
                raise ValueError(
                    "Harness direct tool refusal does not use the exact governed shape."
                )
            reason = document.get("reason")
            if not isinstance(reason, str):
                raise ValueError("Harness direct tool refusal reason is malformed.")
            result = ValidatedToolResultV1(
                tool_use_ref=use_id,
                tool_name=tool_name,
                ordinal=self.governed_count,
                disposition=ToolCustodyDispositionV1.REFUSED,
                tool_result_ref=_result_reference(document.get("correlation_id")),
                grounding_set=_decoded_refusal_grounding(document),
                refusal_reason=reason,
            )
        else:
            if "refused" in document:
                raise ValueError("Harness direct tool success cannot carry a refusal state.")
            metadata = _object(document.get("metadata"), "Harness direct tool response metadata")
            result = ValidatedToolResultV1(
                tool_use_ref=use_id,
                tool_name=tool_name,
                ordinal=self.governed_count,
                disposition=ToolCustodyDispositionV1.SUCCEEDED,
                tool_result_ref=_result_reference(metadata.get("correlation_id")),
                grounding_set=_decoded_grounding(document),
            )
        self.decoded_use_ids.add(use_id)
        self._timed_result(use_id)
        self.pending.append(
            ManagedHarnessToolActivityV1(
                tool_name=tool_name, status=ManagedHarnessToolCallStatusV1.COMPLETED
            )
        )
        self.pending.append(
            ManagedHarnessToolStepV1(
                step=self._next_step(
                    tool_name,
                    HarnessStepDispositionV1(result.disposition.value),
                    result.tool_result_ref,
                ),
                result=result,
            )
        )

    def _serialized_result(self, use_id: str) -> str:
        buffer = self.results[use_id]
        if bool(buffer.text) == bool(buffer.json_values):
            raise ValueError("Harness tool result must use exactly one content representation.")
        if buffer.text:
            serialized = "".join(buffer.text)
        else:
            if len(buffer.json_values) != 1:
                raise ValueError("Harness tool result must contain one JSON document.")
            serialized = json.dumps(
                buffer.json_values[0], separators=(",", ":"), ensure_ascii=False
            )
        if len(serialized.encode("utf-8")) > _MAX_RESULT_BYTES:
            raise ValueError("Harness direct tool result exceeds its bounded size.")
        return serialized


def _result_reference(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("Harness direct tool result carries no correlation reference.")
    return value


def _decoded_refusal_grounding(document: Mapping[str, object]) -> GroundingSet:
    """Decode the grounded refusal projection every governed refusal is required to carry."""
    metadata = _object(document.get("metadata"), "Harness direct tool refusal metadata")
    grounding = metadata.get("grounding")
    if not isinstance(grounding, Mapping):
        raise ValueError("Harness direct tool refusal has no grounded refusal projection.")
    return decode_contract(
        json.dumps(grounding, ensure_ascii=False, separators=(",", ":")), GroundingSet
    )


def _decoded_grounding(document: Mapping[str, object]) -> GroundingSet:
    """Decode only the ShapedResponse projection that may reach public publication."""
    if set(document) != {"data", "narrative", "metadata", "recommendations"}:
        raise ValueError(
            "Harness direct tool result does not use the exact ShapedResponse envelope."
        )
    data = _object(document.get("data"), "Harness direct tool response data")
    metadata = _object(document.get("metadata"), "Harness direct tool response metadata")
    if set(data) != {field.name for field in fields(ResponseData)}:
        raise ValueError("Harness direct tool response data fields are not exact.")
    if set(metadata) != {field.name for field in fields(Metadata)}:
        raise ValueError("Harness direct tool response metadata fields are not exact.")
    if not isinstance(document.get("narrative"), list) or not isinstance(
        document.get("recommendations"), list
    ):
        raise ValueError("Harness direct tool response narrative shape is invalid.")
    grounding = metadata.get("grounding")
    if not isinstance(grounding, Mapping):
        raise ValueError("Harness direct tool response has no grounded projection.")
    return decode_contract(
        json.dumps(grounding, ensure_ascii=False, separators=(",", ":")), GroundingSet
    )


def _logical_tool_name(raw_name: str) -> str:
    target, delimiter, name = raw_name.rpartition("___")
    if not delimiter or not target or not name:
        raise ValueError("Harness tool name is not a declared Gateway tool name.")
    return name


def _object(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object.")
    return cast("Mapping[str, object]", value)


def _optional_object(value: object, label: str) -> Mapping[str, object] | None:
    return None if value is None else _object(value, label)


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty text.")
    return value


def _index(event: Mapping[str, object]) -> int:
    value = event.get("contentBlockIndex")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("Harness content block index is invalid.")
    return value


_INVOKE_HARNESS_BEFORE_SEND = "before-send.bedrock-agentcore.InvokeHarness"
_CLIENT_CREATION = threading.Lock()
"""Serializes per-request client creation. The conversation server is threaded, and creating
clients from boto3's shared default session is not thread-safe; using a built client is."""


def bearer_harness_client(target: ManagedHarnessTarget, token: str) -> BedrockAgentCoreClient:
    """Build a client that presents one turn's route-minted Harness token instead of signing.

    The Harness declares a `customJWTAuthorizer` that trusts only the token service's
    Harness-audience tokens for the route client, and refuses SigV4 (ADR-077 §2). Build one
    per turn: the token is bound to one authenticated caller, and a client cached across
    requests would present one user's token for another user's turn.

    `UNSIGNED` suppresses SigV4 rather than leaving both on the request: a call carrying a
    bearer `Authorization` header *and* a signature is ambiguous about which identity it
    asserts, and which one wins would be the service's choice rather than ours. The header is
    attached at `before-send`, after signing would have run, so nothing downstream rewrites it.
    Technique live-verified 2026-08-23 against the bearer-authorized Harness.

    The token is held only by the event hook's closure. It is never logged, and it appears in
    no exception raised here or by the adapter.
    """
    if not token.strip():
        # Refused at construction rather than sent. An empty token still produces an
        # `Authorization: Bearer` header, and because SigV4 is suppressed the service then
        # parses that header *as* a SigV4 credential and answers `AccessDeniedException:
        # Authorization header requires 'Credential' parameter`, which reads as a missing
        # IAM grant rather than a missing token.
        raise ValueError(
            "A bearer token is required. An empty bearer is parsed by the service as a "
            "SigV4 credential, and the Harness customJWTAuthorizer has no unauthenticated form."
        )
    with _CLIENT_CREATION:
        client = boto3.client(
            "bedrock-agentcore",
            config=Config(
                signature_version=UNSIGNED,
                read_timeout=target.read_timeout_seconds,
                retries={"mode": "standard", "total_max_attempts": 1},
            ),
        )
    authorization = f"Bearer {token}"

    def _attach_bearer(request: AWSPreparedRequest, **_: object) -> None:
        request.headers["Authorization"] = authorization

    client.meta.events.register_first(_INVOKE_HARNESS_BEFORE_SEND, _attach_bearer)
    return client


def _user_content(turn: HarnessTurnV2) -> list[HarnessContentBlockTypeDef]:
    """Return the question, then any redeemed page context as a second quoted-data block.

    ADR-120 decision 6: one fixed framing line and JSON-encoded labels, never interpolated
    into the question, so an advertiser-authored campaign name stays data.
    """
    content: list[HarnessContentBlockTypeDef] = [{"text": turn.question}]
    if turn.page_context is not None:
        content.append({"text": page_context_block(turn.page_context)})
    return content


class AgentCoreManagedHarness(ManagedHarnessPort):
    """Stream the answer text, governed tool progress, and the end of the turn.

    Takes its client explicitly and has no default: the only production client is
    `bearer_harness_client`, so a Harness can never be built that would sign with the
    function's own role instead of carrying the user's identity (ADR-077 §1).
    """

    def __init__(
        self,
        target: ManagedHarnessTarget,
        session_id_for: Callable[[str], str],
        *,
        client: BedrockAgentCoreClient,
    ) -> None:
        self._target = target
        self._session_id_for = session_id_for
        self._client = client

    def stream(self, turn: HarnessTurnV2) -> Generator[ManagedHarnessEventV1]:
        """Send one user message and yield each projection the moment the Harness produces it."""
        session_id = self._session_id_for(turn.conversation_id)
        if not (_MIN_SESSION_ID_LENGTH <= len(session_id) <= _MAX_SESSION_ID_LENGTH):
            raise ValueError("conversation session mapper returned an invalid Harness session ID.")
        invocation: InvokeHarnessRequestTypeDef = {
            "harnessArn": self._target.harness_arn,
            "qualifier": self._target.qualifier,
            "runtimeSessionId": session_id,
            "actorId": turn.actor_id,
            "messages": [{"role": "user", "content": _user_content(turn)}],
        }
        trace = self._trace_propagation()
        if trace.get("traceParent"):
            invocation["traceParent"] = trace["traceParent"]
        timer = current_turn_timer()
        normalizer = _Normalizer.create(timer)
        try:
            if timer is not None:
                timer.mark(TurnMilestone.HARNESS_INVOKED)
            response = self._client.invoke_harness(**invocation)
            stream = response["stream"]
            if timer is not None:
                timer.mark(TurnMilestone.HARNESS_STREAM_OPENED)
            try:
                for event in cast("Iterable[Mapping[str, object]]", stream):
                    if timer is not None:
                        timer.mark(TurnMilestone.FIRST_HARNESS_EVENT)
                    normalizer.accept(event)
                    yield from normalizer.drain()
                normalizer.finish()
            finally:
                stream.close()
            yield ManagedHarnessTurnEndedV1()
        except (BotoCoreError, ClientError) as error:
            raise self._failure(aws_failure_context(error)) from error
        except (KeyError, TypeError, ValueError) as error:
            # The message is one of this adapter's own fixed contract sentences, never
            # provider or customer content, and it is what an investigator needs first.
            raise self._failure(
                {"cause": type(error).__name__, "detail": str(error)[:200]}
            ) from error

    @staticmethod
    def _trace_propagation() -> dict[str, str]:
        carrier: dict[str, str] = {}
        TraceContextTextMapPropagator().inject(carrier)
        traceparent = carrier.get("traceparent")
        return {"traceParent": traceparent} if traceparent else {}

    @staticmethod
    def _failure(context: Mapping[str, object]) -> DependencyError:
        return DependencyError(
            HARNESS_UNREACHABLE,
            "The conversation service is temporarily unavailable.",
            context=dict(context),
        )
